"""Orb Hospital OS — Admin User Management Routes.

CRUD + lifecycle for user accounts (previously only creatable via the seed
script). All admin endpoints are gated on the ``admin.overview`` permission —
the same permission that guards the admin governance dashboard, held only by the
``admin`` role — so user management is admin-only.

Endpoints (prefix ``/api/admin/users``):
  GET    ""                 list users (no hashes/secrets ever returned)
  POST   ""                 create a user (bcrypt password via auth.hash_password)
  POST   "/{id}/deactivate" disable login + revoke sessions (bump token_version)
  POST   "/{id}/activate"   re-enable login
  POST   "/{id}/reset-password"  set a new bcrypt password + revoke sessions
  POST   "/{id}/pin"        provision a signing PIN (admin for anyone; a user may
                            set THEIR OWN pin by re-supplying their password)
  PATCH  "/{id}"            change role / department
  POST   "/me/password"     SELF-SERVICE: any authenticated user changes own
                            password; revokes ALL sessions incl. the current one.

Service identities (database.SERVICE_ACCOUNT_IDS: 'system' and the Relay bots)
are not listed and every mutation on them answers 409: their sentinel hash is
the only thing stopping them being logins, and bot_vigil is a department-less
'doctor', so a single password reset used to mint a hospital-wide doctor
account. Patient portal accounts are created at admission, never here.

Every mutation is audit-logged via services.audit_service.log_audit_event,
mirroring the standalone (post-commit) pattern used across the other routes.

SECURITY NOTE (cross-file, auth_routes.py — owned by another cluster): the login
handler does NOT currently reject is_active=0 users. Deactivation here bumps
token_version so outstanding JWTs die immediately, but until auth_routes gates on
is_active a deactivated user could still obtain a *fresh* token by logging in
again. See the schema notes returned by this task.
"""
from __future__ import annotations

import re
import sqlite3
from db_backend import DBIntegrityError
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import get_current_user, hash_password, verify_password
from database import SERVICE_ACCOUNT_IDS, db_session
from permissions import DEPARTMENT_REQUIRED_ROLES, has_permission, require_permission
from roles import STORABLE_ROLES
from services.audit_service import log_audit_event

router = APIRouter(prefix="/api/admin/users", tags=["admin-users"])

# Roles permitted by the users.role CHECK constraint. Both render from
# roles.STORABLE_ROLES, so a role the DB would reject is refused here with a
# clean 400 instead of reaching the constraint — and the staffing path can
# never trail the schema again (the drift that made 'cashier' unstaffable
# until migration 33; see roles.py).
ALLOWED_ROLES = set(STORABLE_ROLES)

# Minimum password length enforced on create / reset / self-change. Bcrypt caps
# effective input at 72 bytes; we do not pre-truncate (hash_password handles it).
_MIN_PASSWORD_LEN = 8


# ──────────────────────────────────────────────────────────────────────────────
#  Request bodies
# ──────────────────────────────────────────────────────────────────────────────
class CreateUserBody(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    role: str
    department: str | None = Field(default=None, max_length=200)
    password: str = Field(..., min_length=_MIN_PASSWORD_LEN, max_length=200)


class ResetPasswordBody(BaseModel):
    new_password: str = Field(..., min_length=_MIN_PASSWORD_LEN, max_length=200)


class PatchUserBody(BaseModel):
    role: str | None = None
    department: str | None = Field(default=None, max_length=200)


class SelfPasswordBody(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=_MIN_PASSWORD_LEN, max_length=200)


class SetPinBody(BaseModel):
    pin: str = Field(..., min_length=4, max_length=8)
    # Required on the self-service path only: the caller re-proves possession of
    # their password before a signing credential is (re)provisioned.
    password: str | None = Field(default=None, max_length=200)


# ──────────────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _audit(request: Request, actor: dict, action_type: str, outcome: str,
           summary: str, target_id: str | None, metadata: dict | None = None) -> None:
    """Standalone audit write (post-commit), mirroring the other routes."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="user_management",
        action_type=action_type,
        outcome=outcome,
        summary_message=summary,
        actor_user_id=actor.get("id"),
        actor_role=actor.get("role"),
        target_type="user",
        target_id=target_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata=metadata,
    )


def _validate_role(role: str) -> None:
    if role not in ALLOWED_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{role}'. Allowed: {', '.join(sorted(ALLOWED_ROLES))}",
        )


def _is_reserved_id(user_id: str) -> bool:
    """A service identity, or an id a future bot would be seeded under: bots are
    seeded with INSERT OR IGNORE, so a person created first as 'bot_x' would
    silently become the bot's sender."""
    uid = user_id.strip().lower()
    return uid in SERVICE_ACCOUNT_IDS or uid.startswith("bot_")


def _refuse_service_account(request: Request, actor: dict, user_id: str,
                            action: str) -> None:
    """409 (audited) for any change to a service identity."""
    if user_id not in SERVICE_ACCOUNT_IDS:
        return
    _audit(
        request, actor, "user_service_account_change_denied", "denied",
        f"Refused {action} on service account {user_id}", user_id,
        metadata={"action": action},
    )
    raise HTTPException(
        status_code=409,
        detail="This is a system account used by Orb's automation. "
               "It has no login and cannot be changed here.",
    )


def _require_department(role: str, department: str | None) -> None:
    """A blank department on a doctor or nurse reads every ward
    (permissions.department_scope), so it is refused rather than stored."""
    if role in DEPARTMENT_REQUIRED_ROLES and not (department or "").strip():
        raise HTTPException(status_code=400,
                            detail="Department is required for clinical roles")


def _known_departments(db: sqlite3.Connection) -> list[str]:
    """Departments a clinical account can be scoped to: those patients are
    admitted under, plus the Relay department rooms. A department outside this
    list scopes its account to no patient at all (the 'Pharmacy' defect)."""
    names: set[str] = set()
    for sql in (
        "SELECT DISTINCT department AS d FROM admissions WHERE department IS NOT NULL",
        "SELECT DISTINCT name AS d FROM channels WHERE type = 'department'",
    ):
        try:
            names.update((r["d"] or "").strip() for r in db.execute(sql).fetchall())
        except Exception:   # table absent on a partial schema: offer what we have
            continue
    names.discard("")
    return sorted(names, key=str.lower)


def _get_user_row(db: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return row


# ──────────────────────────────────────────────────────────────────────────────
#  List
# ──────────────────────────────────────────────────────────────────────────────
@router.get("")
def list_users(include_patients: bool = False,
               user: dict = Depends(require_permission("admin.overview"))):
    """List staff accounts with recent auth activity.

    Service identities are never listed (they are not logins and cannot be
    changed). Patient portal accounts are left out unless ``include_patients``
    is set, so the default list is the hospital's staff. ``departments`` is the
    set a clinical account can usefully be scoped to.

    Never returns password_hash / pin_hash / totp_secret."""
    from datetime import datetime, timedelta, timezone

    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    service_ids = sorted(SERVICE_ACCOUNT_IDS)
    patient_clause = "" if include_patients else " AND role != 'patient'"
    with db_session() as db:
        rows = db.execute(
            f"""SELECT id, name, role, department, registration_number,
                      COALESCE(is_active, 1) AS is_active,
                      COALESCE(totp_enabled, 0) AS totp_enabled,
                      created_at
               FROM users
               WHERE id NOT IN ({",".join("?" * len(service_ids))}){patient_clause}
               ORDER BY role, name""",
            service_ids,
        ).fetchall()
        departments = _known_departments(db)

        # auth events use event_category='authentication' with
        # action_type='login_success'/'login_failure' (routes/auth_routes.py)
        last_logins = {
            r["actor_user_id"]: r["last_login"]
            for r in db.execute(
                """SELECT actor_user_id, MAX(timestamp) AS last_login
                   FROM audit_logs
                   WHERE event_category = 'authentication'
                     AND action_type = 'login_success'
                   GROUP BY actor_user_id"""
            ).fetchall()
        }
        # login_failure rows carry NO actor_user_id (the account may not exist);
        # the attempted id is in metadata_json.attempted_id. Grouping by
        # actor_user_id would bucket every failure under NULL and report 0 for
        # every real user — defeating the brute-force column. Key on the
        # attempted id instead.
        failed_24h = {
            r["attempted_id"]: r["cnt"]
            for r in db.execute(
                """SELECT json_extract(metadata_json, '$.attempted_id') AS attempted_id,
                          COUNT(*) AS cnt
                   FROM audit_logs
                   WHERE event_category = 'authentication'
                     AND action_type = 'login_failure' AND timestamp >= ?
                   GROUP BY attempted_id""",
                (cutoff_24h,),
            ).fetchall()
        }

    return {
        "users": [
            {
                "id": r["id"],
                "name": r["name"],
                "role": r["role"],
                "department": r["department"],
                "registration_number": r["registration_number"],
                "is_active": bool(r["is_active"]),
                "totp_enabled": bool(r["totp_enabled"]),
                "created_at": r["created_at"],
                "last_login": last_logins.get(r["id"]),
                "failed_logins_24h": failed_24h.get(r["id"], 0),
                # The panel hides Deactivate on the admin's own row (the
                # server refuses it anyway, see deactivate_user).
                "is_self": r["id"] == user.get("id"),
            }
            for r in rows
        ],
        "departments": departments,
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Create
# ──────────────────────────────────────────────────────────────────────────────
@router.post("", status_code=201)
def create_user(
    body: CreateUserBody,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """Create a new user with a bcrypt-hashed password."""
    user_id = body.id.strip()
    name = body.name.strip()
    if not user_id or not name:
        raise HTTPException(status_code=400, detail="id and name are required")
    if _is_reserved_id(user_id):
        raise HTTPException(status_code=409,
                            detail="This ID is reserved for a system account. Choose another.")
    _validate_role(body.role)
    if body.role == "patient":
        # A portal login made here is linked to no patient record (admission
        # links patients.user_id); it would be an orphan account.
        raise HTTPException(
            status_code=400,
            detail="Patient portal accounts are created when the patient is admitted, not here",
        )
    department = (body.department.strip() or None) if body.department else None
    _require_department(body.role, department)

    password_hash = hash_password(body.password)

    try:
        with db_session() as db:
            # Guard against duplicate id up front for a clean 409; the PK also
            # backstops this if two admins race.
            existing = db.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail="A user with this id already exists")
            db.execute(
                """INSERT INTO users (id, name, role, department, password_hash, is_active, token_version)
                   VALUES (?, ?, ?, ?, ?, 1, 0)""",
                (user_id, name, body.role, department, password_hash),
            )
    except DBIntegrityError:
        # Lost the race, or CHECK constraint tripped despite validation.
        raise HTTPException(status_code=409, detail="A user with this id already exists")

    _audit(
        request, user, "user_created", "success",
        f"Created user {user_id} ({body.role})", user_id,
        metadata={"role": body.role, "department": department},
    )
    return {
        "id": user_id,
        "name": name,
        "role": body.role,
        "department": department,
        "is_active": True,
        "totp_enabled": False,
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Deactivate / Activate
# ──────────────────────────────────────────────────────────────────────────────
@router.post("/{user_id}/deactivate")
def deactivate_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """Disable login for a user and revoke all outstanding JWTs.

    An admin cannot deactivate themselves (that would lock the operator out of
    their own live session and could orphan the last admin account)."""
    if user_id == user.get("id"):
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    _refuse_service_account(request, user, user_id, "deactivate")

    with db_session() as db:
        _get_user_row(db, user_id)
        # is_active -> 0 AND bump token_version so every existing token for this
        # user is rejected by get_current_user immediately.
        db.execute(
            """UPDATE users
               SET is_active = 0,
                   token_version = COALESCE(token_version, 0) + 1
               WHERE id = ?""",
            (user_id,),
        )

    _audit(
        request, user, "user_deactivated", "success",
        f"Deactivated user {user_id}; outstanding sessions revoked", user_id,
    )
    return {"status": "deactivated", "id": user_id}


@router.post("/{user_id}/activate")
def activate_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """Re-enable login for a previously deactivated user."""
    _refuse_service_account(request, user, user_id, "activate")
    with db_session() as db:
        _get_user_row(db, user_id)
        db.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))

    _audit(
        request, user, "user_activated", "success",
        f"Activated user {user_id}", user_id,
    )
    return {"status": "activated", "id": user_id}


# ──────────────────────────────────────────────────────────────────────────────
#  Admin reset password
# ──────────────────────────────────────────────────────────────────────────────
@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: str,
    body: ResetPasswordBody,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """Set a new bcrypt password for a user and revoke their existing sessions."""
    _refuse_service_account(request, user, user_id, "password reset")
    new_hash = hash_password(body.new_password)
    with db_session() as db:
        _get_user_row(db, user_id)
        db.execute(
            """UPDATE users
               SET password_hash = ?,
                   token_version = COALESCE(token_version, 0) + 1
               WHERE id = ?""",
            (new_hash, user_id),
        )

    _audit(
        request, user, "user_password_reset", "success",
        f"Reset password for user {user_id}; sessions revoked", user_id,
    )
    return {"status": "password_reset", "id": user_id}


# ──────────────────────────────────────────────────────────────────────────────
#  Signing-PIN provisioning
# ──────────────────────────────────────────────────────────────────────────────
@router.post("/{user_id}/pin")
def set_user_pin(
    user_id: str,
    body: SetPinBody,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Provision (or replace) a user's signing PIN — the second factor for
    agentic order confirmation. Universal PIN seeding was removed, so pin_hash
    is NULL until this endpoint runs.

    Two authorization paths:
      * admin (``admin.overview``) may set any user's PIN;
      * any authenticated user may set THEIR OWN PIN, but must re-supply their
        password (a signing credential must not be mintable from a stolen JWT
        alone).
    """
    is_self = user_id == user.get("id")
    if not is_self and not has_permission(user.get("role", ""), "admin.overview"):
        # Audit-log the denial, mirroring require_permission's behaviour.
        _audit(
            request, user, "user_pin_provision_denied", "denied",
            f"Refused PIN provisioning for {user_id}: not admin and not self", user_id,
        )
        raise HTTPException(status_code=403, detail="Only an admin can set another user's PIN")
    _refuse_service_account(request, user, user_id, "signing PIN")

    if is_self:
        # 400 (not 401) so the web client's session-expired interceptor is not
        # tripped — same convention as the password endpoints above.
        if not body.password:
            raise HTTPException(status_code=400, detail="Setting your own PIN requires your password")
        if not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="Password is incorrect")

    # re.ASCII: \d alone also matches Unicode digits (e.g. '١٢٣٤').
    if not re.fullmatch(r"\d{4,8}", body.pin, re.ASCII):
        raise HTTPException(status_code=400, detail="PIN must be 4-8 digits")

    from services import pin_security
    pin_hash = pin_security.hash_pin(body.pin)
    with db_session() as db:
        _get_user_row(db, user_id)
        db.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (pin_hash, user_id))
    # A fresh PIN invalidates any stale lockout state from the old one.
    pin_security.clear_pin_failures(user_id)

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type="user_pin_provisioned",
        outcome="success",
        summary_message=(
            f"Signing PIN self-provisioned by user {user_id}" if is_self
            else f"Signing PIN provisioned by admin {user.get('id')} for user {user_id}"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type="user",
        target_id=user_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"self_service": is_self},
    )
    return {"status": "pin_set", "id": user_id}


# ──────────────────────────────────────────────────────────────────────────────
#  Patch role / department
# ──────────────────────────────────────────────────────────────────────────────
@router.patch("/{user_id}")
def patch_user(
    user_id: str,
    body: PatchUserBody,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """Update a user's role and/or department. Role is validated against the
    users.role CHECK constraint's allowed set. A patient portal account and a
    staff account never cross into each other's role, and a doctor or nurse is
    never left without a department."""
    if body.role is None and body.department is None:
        raise HTTPException(status_code=400, detail="Provide role and/or department to update")
    _refuse_service_account(request, user, user_id, "role change")

    sets: list[str] = []
    params: list[object] = []
    changed: dict[str, object] = {}
    if body.role is not None:
        _validate_role(body.role)
        sets.append("role = ?")
        params.append(body.role)
        changed["role"] = body.role
    if body.department is not None:
        dept = body.department.strip() or None
        sets.append("department = ?")
        params.append(dept)
        changed["department"] = dept

    params.append(user_id)
    with db_session() as db:
        current = _get_user_row(db, user_id)
        new_role = body.role if body.role is not None else current["role"]
        if (current["role"] == "patient") != (new_role == "patient"):
            # Promoting a portal login hands the patient (who holds its
            # password) a staff role; demoting staff makes an unlinked portal.
            raise HTTPException(
                status_code=400,
                detail="A patient portal account and a staff account cannot be converted into each other",
            )
        new_dept = changed["department"] if "department" in changed else current["department"]
        _require_department(new_role, new_dept)
        db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        row = db.execute(
            "SELECT id, name, role, department FROM users WHERE id = ?", (user_id,)
        ).fetchone()

    _audit(
        request, user, "user_updated", "success",
        f"Updated user {user_id}: {', '.join(changed.keys())}", user_id,
        metadata=changed,
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "department": row["department"],
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Self-service password change
# ──────────────────────────────────────────────────────────────────────────────
@router.post("/me/password")
def change_own_password(
    body: SelfPasswordBody,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Any authenticated user changes their OWN password.

    Verifies the current password, stores a bcrypt hash of the new one, and bumps
    token_version — which revokes every session for this user INCLUDING the one
    making this request. We return {relogin: true} so the client can explain that
    the user must sign in again."""
    # 400 (not 401) on a wrong current password: the caller is already
    # authenticated (get_current_user passed), so a 401 would trip the web
    # client's global "session expired" interceptor instead of an inline error.
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current one")

    new_hash = hash_password(body.new_password)
    with db_session() as db:
        db.execute(
            """UPDATE users
               SET password_hash = ?,
                   token_version = COALESCE(token_version, 0) + 1
               WHERE id = ?""",
            (new_hash, user["id"]),
        )

    _audit(
        request, user, "user_self_password_change", "success",
        "User changed own password; all sessions revoked", user["id"],
    )
    return {"status": "password_changed", "relogin": True}
