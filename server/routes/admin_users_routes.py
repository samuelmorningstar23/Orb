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
  POST   "/{id}/facility"   move a department-less account's home facility
                            (multisite-6; ORB_MULTI_SITE, 404 while off)
  GET    "/enterprise-grants"               group-wide access grants
  POST   "/enterprise-grants"               grant group-wide access (ORB_MULTI_SITE)
  POST   "/enterprise-grants/{id}/revoke"   revoke it
  The three grant routes answer only an administrator who holds a live grant
  (ruling MS-R6 (a)). They sit under this router's prefix because main.py
  registers this router once; the design named /api/admin/enterprise-grants.

FACILITIES (multisite-6, W9-H, 10 October 2026). With more than one facility on
record, an administrator manages only the accounts of the facilities in their
own scope (facility_service.facility_scope_for): every change to another
facility's account is a 403 with an authorization audit row. A home facility
row is written only here, in the same transaction as the account change, with
a fail-closed security audit row. With one facility on record and ORB_MULTI_SITE
off (every site today) no facility row exists, none is written, and every
response keeps its old shape. A row that does exist (left from a time the
switch was on) is still kept true: a department given to its account revokes
its home row, and a role change away from admin revokes a live grant.

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
import uuid
from datetime import UTC
from typing import Any

import config
from auth import get_current_user, hash_password, verify_password
from database import SERVICE_ACCOUNT_IDS, db_session
from db_backend import DBIntegrityError
from fastapi import APIRouter, Depends, HTTPException, Request
from permissions import (
    DEPARTMENT_REQUIRED_ROLES,
    department_scope,
    has_permission,
    require_permission,
)
from pydantic import BaseModel, Field
from roles import STORABLE_ROLES, role_switches
from services.audit_service import AuditWriteError, log_audit_event

from routes.opt_in_gates import feature_gate
from services import facility_service

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
    # multisite-6: the home facility of an account with no department. Refused
    # with a department (the department's facility is the only source).
    facility_id: int | None = Field(default=None, ge=1)


class MoveFacilityBody(BaseModel):
    facility_id: int = Field(..., ge=1)
    reason: str | None = Field(default=None, max_length=500)


class GrantEnterpriseBody(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(..., min_length=1, max_length=500)


class RevokeEnterpriseBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


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
def _audit(
    request: Request,
    actor: dict,
    action_type: str,
    outcome: str,
    summary: str,
    target_id: str | int | None,
    metadata: dict | None = None,
) -> None:
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
        target_id=str(target_id) if target_id is not None else None,
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


# Plain words for the refusal below: what is switched off, and the account
# that therefore cannot be made. Keyed by role; every ROLE_FEATURE role has one
# (tests/test_role_batch.py checks it).
_ROLE_SWITCH_OFF_WORDS: dict[str, str] = {
    "receptionist": (
        "The registration desk is switched off on this site, so a "
        "receptionist account cannot be created."
    ),
    # Either switch lets a ward clerk be staffed (roles.ROLE_FEATURE_ALSO).
    "ward_clerk": (
        "Beds and admissions and Orb Ward Record are both switched off "
        "on this site. Ward clerks need Beds and admissions or Orb Ward "
        "Record switched on."
    ),
    "lab_technician": (
        "The laboratory module is switched off on this site, so a "
        "lab technician account cannot be created."
    ),
    "pathologist": (
        "The laboratory module is switched off on this site, so a "
        "pathologist account cannot be created."
    ),
    "phlebotomist": (
        "The laboratory module is switched off on this site, so a "
        "phlebotomist account cannot be created."
    ),
}


def _require_role_switch(role: str) -> None:
    """Refuse a role whose part of Orb is switched off (roles.ROLE_FEATURE).

    Read at request time, so turning a module on takes effect without a
    restart of this check. An account that already holds the role is not
    touched: this guards creating one, and moving an account into one."""
    switches = role_switches(role)
    if not switches or any(config.switch_enabled(s) for s in switches):
        return
    raise HTTPException(
        status_code=400,
        detail=_ROLE_SWITCH_OFF_WORDS.get(
            role,
            "That part of Orb is switched off on this site, so this account cannot be created.",
        ),
    )


def _is_reserved_id(user_id: str) -> bool:
    """A service identity, or an id a future bot would be seeded under: bots are
    seeded with INSERT OR IGNORE, so a person created first as 'bot_x' would
    silently become the bot's sender."""
    uid = user_id.strip().lower()
    return uid in SERVICE_ACCOUNT_IDS or uid.startswith("bot_")


def _refuse_service_account(request: Request, actor: dict, user_id: str, action: str) -> None:
    """409 (audited) for any change to a service identity."""
    if user_id not in SERVICE_ACCOUNT_IDS:
        return
    _audit(
        request,
        actor,
        "user_service_account_change_denied",
        "denied",
        f"Refused {action} on service account {user_id}",
        user_id,
        metadata={"action": action},
    )
    raise HTTPException(
        status_code=409,
        detail="This is a system account used by Orb's automation. "
        "It has no login and cannot be changed here.",
    )


def _require_department(role: str, department: str | None) -> None:
    """A blank department on a doctor or nurse reads every ward
    (permissions.department_scope), so it is refused rather than stored. The
    same holds for a ward clerk, whose bed board would otherwise be every
    ward's; the clerk is not a clinical role, so the words say so."""
    if role in DEPARTMENT_REQUIRED_ROLES and not (department or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Department is required for a ward clerk"
                if role == "ward_clerk"
                else "Department is required for clinical roles"
            ),
        )


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
        except Exception:  # table absent on a partial schema: offer what we have
            continue
    names.discard("")
    return sorted(names, key=str.lower)


def _get_user_row(db: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return row


# ──────────────────────────────────────────────────────────────────────────────
#  Facilities and group-wide access (multisite-6, W9-H, 10 October 2026)
# ──────────────────────────────────────────────────────────────────────────────
# Samuel ruled on 3 October 2026 that a hospital group runs one appliance with a
# facility id on its rows (M1), and that isolation follows the DATA (M2): once
# more than one facilities row exists, an account sees only its facilities'
# patients, and an administrator manages only its facilities' staff.
# ORB_MULTI_SITE gates growth only: a home facility other than facility 1, a
# move between facilities, and a new group-wide grant.
#
# Where an account works (facility_service.facility_scope_for):
#   * with a department: that department's facility in the departments master.
#     A home row for such an account is refused here, so the department stays
#     the only source;
#   * without one: its live home row in user_facilities, else facility 1.
# Home rows are written only by this file, in the same transaction as the
# account change, each with a fail-closed security audit row. A move revokes
# the old row and inserts the new one; rows are never deleted.
#
# Group-wide (enterprise) access is a named grant with a reason, held only by
# an administrator (ruling MS-R6 (a)). Only a holder may see, give or revoke
# one; the first is made at the appliance's console with
# scripts/grant_enterprise_access.py. Every write here calls
# facility_service.invalidate_cache() after its commit, so a revoke narrows the
# scope on the very next request.

_HOME_ROW_REFUSED = (
    "A home facility is only for staff without a department. Staff "
    "with a department work at their department's facility."
)
_OUTSIDE_SCOPE = (
    "This account works at another facility. Only an administrator at "
    "that facility, or one with group-wide access, can change it."
)
_GRANT_HOLDERS_ONLY = (
    "Only an administrator with group-wide access can see or change group-wide access."
)
_HOLDER_ACCOUNT = (
    "This administrator has access to all facilities. Only an "
    "administrator who also has it can change their account."
)
_MIN_REASON_LEN = 3


class _Refusal(Exception):
    """A refusal decided inside a transaction. The route lets the transaction
    roll back first and only then writes the denial's audit row, so that row is
    never rolled back with the change it refused."""

    def __init__(
        self, status: int, error: str, message: str, *, audit: tuple[str, str, dict] | None = None
    ):
        super().__init__(message)
        self.status, self.error, self.message, self.audit = status, error, message, audit


def _audit_denial(
    request: Request,
    actor: dict,
    action_type: str,
    summary: str,
    target_id: str | None,
    metadata: dict | None = None,
) -> None:
    """An authorization row for a refused facility or grant action (written
    after any transaction has rolled back)."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type=action_type,
        outcome="denied",
        summary_message=summary,
        actor_user_id=actor.get("id"),
        actor_role=actor.get("role"),
        target_type="user",
        target_id=str(target_id) if target_id is not None else None,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata=metadata,
    )


def _refused(
    request: Request, actor: dict, target_id: str | None, refusal: _Refusal
) -> HTTPException:
    if refusal.audit is not None:
        action_type, summary, metadata = refusal.audit
        _audit_denial(request, actor, action_type, summary, target_id, metadata)
    return HTTPException(
        status_code=refusal.status, detail={"error": refusal.error, "message": refusal.message}
    )


def _security_row(
    db,
    request: Request,
    actor: dict,
    action_type: str,
    summary: str,
    *,
    target_id: str,
    metadata: dict,
) -> None:
    """A security audit row inside the caller's transaction. Fail-closed: a
    failed write raises AuditWriteError, the transaction rolls back with it,
    and the route answers 503 with nothing saved."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action_type,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=actor.get("id"),
        actor_role=actor.get("role"),
        target_type="user",
        target_id=str(target_id) if target_id is not None else None,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata=metadata,
    )


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": (
                "Nothing was saved, because the audit trail is not available. "
                "Try again in a moment."
            ),
        },
    )


def _multi_site_on() -> bool:
    """ORB_MULTI_SITE, read at request time."""
    return bool(config.feature_enabled("multi_site"))


def _facility_view_on(db=None) -> bool:
    """Show facilities at all: the switch is on, or more than one facility is
    on record (labels are needed then whatever the switch says)."""
    return _multi_site_on() or facility_service.multi_facility_data(db=db)


def _has_table(db, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _live_home_row(db, user_id: str):
    if not _has_table(db, "user_facilities"):
        return None
    return db.execute(
        "SELECT id, facility_id FROM user_facilities "
        "WHERE user_id = ? AND kind = 'home' AND revoked_at IS NULL",
        (user_id,),
    ).fetchone()


def _live_grant(db, user_id: str | None):
    if user_id is None or not _has_table(db, "enterprise_access_grants"):
        return None
    return db.execute(
        "SELECT id, user_id FROM enterprise_access_grants WHERE user_id = ? AND revoked_at IS NULL",
        (user_id,),
    ).fetchone()


def _holds_grant(db, actor: dict) -> bool:
    """An administrator with a live grant, read from the database, never the
    cache: only such an account may see, give or revoke group-wide access."""
    return (
        actor.get("role") in facility_service.ENTERPRISE_GRANT_ROLES
        and _live_grant(db, actor.get("id")) is not None
    )


def _facility_row(db, facility_id: int | None):
    if facility_id is None or not _has_table(db, "facilities"):
        return None
    return db.execute(
        "SELECT id, code, is_active FROM facilities WHERE id = ?", (facility_id,)
    ).fetchone()


def _in_scope(scope, facility_id) -> bool:
    """scope None is no facility limit (one facility, or group-wide access)."""
    return scope is None or (facility_id is not None and facility_id in scope)


def _scope_list(scope) -> list | None:
    return None if scope is None else sorted(scope)


def _write_home_row(
    db, request: Request, actor: dict, user_id: str, facility_id: int, reason: str
) -> None:
    cur = db.execute(
        "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, reason) "
        "VALUES (?, ?, 'home', ?, CURRENT_TIMESTAMP, ?)",
        (user_id, facility_id, actor.get("id"), reason),
    )
    _security_row(
        db,
        request,
        actor,
        "user_facility_assigned",
        f"Home facility {facility_id} set for user {user_id}",
        target_id=user_id,
        metadata={"facility_id": facility_id, "user_facility_id": cur.lastrowid, "reason": reason},
    )


def _revoke_home_row(db, request: Request, actor: dict, user_id: str, live, reason: str) -> None:
    db.execute(
        "UPDATE user_facilities SET revoked_at = CURRENT_TIMESTAMP, revoked_by = ?, "
        "revoke_reason = ? WHERE id = ? AND revoked_at IS NULL",
        (actor.get("id"), reason, live["id"]),
    )
    _security_row(
        db,
        request,
        actor,
        "user_facility_revoked",
        f"Home facility {live['facility_id']} revoked for user {user_id}",
        target_id=user_id,
        metadata={
            "facility_id": int(live["facility_id"]),
            "user_facility_id": live["id"],
            "reason": reason,
        },
    )


def _revoke_grant(db, request: Request, actor: dict, grant, reason: str) -> None:
    db.execute(
        "UPDATE enterprise_access_grants SET revoked_at = CURRENT_TIMESTAMP, revoked_by = ?, "
        "revoke_reason = ? WHERE id = ? AND revoked_at IS NULL",
        (actor.get("id"), reason, grant["id"]),
    )
    _security_row(
        db,
        request,
        actor,
        "enterprise_access_revoked",
        f"Group-wide access revoked for user {grant['user_id']}",
        target_id=grant["user_id"],
        metadata={"grant_id": grant["id"], "reason": reason},
    )


def _holder_target_refused(target_id: str, action: str) -> _Refusal:
    return _Refusal(
        403,
        "enterprise_grant_required",
        _HOLDER_ACCOUNT,
        audit=(
            "enterprise_access_denied",
            f"Refused {action} on user {target_id}: the account has group-wide access",
            {"action": action, "target_holds_grant": True},
        ),
    )


def _require_target_in_scope(request: Request, actor: dict, user_id: str, action: str) -> None:
    """Refuse (403, audited) a change to another administrator's account that
    holds group-wide access unless the actor holds it too: otherwise any
    administrator could reset that account's password and take the access
    over. With more than one facility on record, also refuse a change to an
    account that works outside the administrator's facilities."""
    multi = facility_service.multi_facility_data()
    with db_session() as db:
        row = db.execute(
            "SELECT id, role, department FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return  # the route's own lookup answers 404
        holder_target = (
            user_id != actor.get("id")
            and row["role"] in facility_service.ENTERPRISE_GRANT_ROLES
            and _live_grant(db, user_id) is not None
        )
        refused_holder = holder_target and not _holds_grant(db, actor)
        if multi and not refused_holder:
            scope = facility_service.facility_scope_for(actor, db=db)
            facility_id = facility_service.user_facility_id(row, db=db)
        else:
            scope, facility_id = None, facility_service.MAIN_FACILITY_ID
    if refused_holder:
        raise _refused(request, actor, user_id, _holder_target_refused(user_id, action))
    if _in_scope(scope, facility_id):
        return
    _audit_denial(
        request,
        actor,
        "user_outside_facility_scope",
        f"Refused {action} on user {user_id}: the account works at another facility",
        user_id,
        {"action": action, "facility_id": facility_id, "facility_scope": _scope_list(scope)},
    )
    raise HTTPException(
        status_code=403, detail={"error": "facility_scope", "message": _OUTSIDE_SCOPE}
    )


def _require_department_in_scope(
    request: Request, actor: dict, user_id: str, department: str | None, action: str
) -> None:
    """With more than one facility on record, the department an account is put
    in must belong to one of the administrator's facilities. A department the
    master does not list has no facility, so only an administrator with
    group-wide access may use it (the account would see no patient)."""
    if department is None or not facility_service.multi_facility_data():
        return
    scope = facility_service.facility_scope_for(actor)
    facility_id = facility_service.department_facility(department)
    if _in_scope(scope, facility_id):
        return
    _audit_denial(
        request,
        actor,
        "user_outside_facility_scope",
        f"Refused {action} of user {user_id}: department outside the administrator's facilities",
        user_id,
        {
            "action": action,
            "department": department,
            "facility_id": facility_id,
            "facility_scope": _scope_list(scope),
        },
    )
    if facility_id is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "department_not_listed",
                "message": (
                    "That department is not on your facility's list of departments. "
                    "Choose one from the list."
                ),
            },
        )
    raise HTTPException(
        status_code=403,
        detail={
            "error": "facility_scope",
            "message": (
                "That department belongs to another facility. Only an administrator "
                "at that facility, or one with group-wide access, can put staff there."
            ),
        },
    )


def _master_spelling(department: str | None) -> str | None:
    """multisite-3: with more than one facility on record, a department given
    to an account is stored in the departments master's own spelling (case and
    spacing folded), so the exact comparison every scope makes
    (admissions.department against users.department) holds, and the account
    resolves to the department's facility. Unchanged with one facility, and
    for a department the master does not list (only a group-wide
    administrator may use one, see _require_department_in_scope)."""
    if department is None or not facility_service.multi_facility_data():
        return department
    from services.department_master import canonical_name

    return canonical_name(department) or department


def _multi_site_off() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error": "multi_site_off",
            "message": (
                "Multi-site is switched off on this site, so staff work at the first facility only."
            ),
        },
    )


def _home_facility_for_new_account(
    request: Request, actor: dict, user_id: str, department: str | None, requested: int | None
) -> int | None:
    """The home facility row to write with a new account, or None for no row
    (facility 1, as for every account made before multi-site). Raises the
    refusal: a home facility with a department (400), an unknown or closed
    facility (400), a facility other than 1 with ORB_MULTI_SITE off (400), and
    a facility, or a department, outside the administrator's scope (403,
    audited)."""
    if department is not None:
        if requested is not None:
            raise HTTPException(
                status_code=400,
                detail={"error": "home_facility_refused", "message": _HOME_ROW_REFUSED},
            )
        _require_department_in_scope(request, actor, user_id, department, "create")
        return None
    multi = facility_service.multi_facility_data()
    site_on = _multi_site_on()
    if not multi and not site_on:
        # One facility and the switch off: exactly as before multi-site.
        if requested not in (None, facility_service.MAIN_FACILITY_ID):
            raise _multi_site_off()
        return None
    target = requested
    if target is None:
        if not multi:
            return None
        # A department-less account an administrator makes works where they do.
        target = facility_service.user_facility_id(actor)
        if target is None:
            _audit_denial(
                request,
                actor,
                "user_outside_facility_scope",
                f"Refused create of user {user_id}: the administrator's own "
                "department is on no facility's list",
                user_id,
                {"action": "create", "facility_id": None},
            )
            raise HTTPException(
                status_code=403, detail={"error": "facility_scope", "message": _OUTSIDE_SCOPE}
            )
    with db_session() as db:
        facility = _facility_row(db, target)
    if facility is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "facility_not_found",
                "message": "There is no facility with that number.",
            },
        )
    if not facility["is_active"]:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "facility_inactive",
                "message": "That facility is closed, so no staff can be added there.",
            },
        )
    if target != facility_service.MAIN_FACILITY_ID and not site_on:
        raise _multi_site_off()
    scope = facility_service.facility_scope_for(actor) if multi else None
    if not _in_scope(scope, target):
        _audit_denial(
            request,
            actor,
            "user_outside_facility_scope",
            f"Refused create of user {user_id} at facility {target}: outside the "
            "administrator's facilities",
            user_id,
            {"action": "create", "facility_id": target, "facility_scope": _scope_list(scope)},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "facility_scope",
                "message": (
                    "That facility is not yours. Only an administrator at that facility, "
                    "or one with group-wide access, can add staff there."
                ),
            },
        )
    return target


def _settle_facility_rows(
    db,
    request: Request,
    actor: dict,
    current,
    new_role: str,
    new_dept: str | None,
    carry_facility: int | None,
) -> bool:
    """Keep the facility rows true to an account's new role and department,
    inside the PATCH's own transaction. Returns True when a row changed.

      * now with a department: a live home row is revoked (the department's
        facility is the only source);
      * department removed, with more than one facility on record: a home row
        keeps the account at the facility it worked at (carry_facility);
      * role no longer admin: a live group-wide grant is revoked (MS-R6 (a)),
        so a later return to admin does not bring group-wide access back."""
    if current["role"] == "patient":
        return False
    user_id = current["id"]
    changed = False
    live = _live_home_row(db, user_id)
    if department_scope({"department": new_dept}) is not None:
        if live is not None:
            _revoke_home_row(
                db,
                request,
                actor,
                user_id,
                live,
                "The account now has a department, so it works at the department's facility",
            )
            changed = True
    elif (
        department_scope({"department": current["department"]}) is not None
        and live is None
        and carry_facility is not None
    ):
        _write_home_row(
            db,
            request,
            actor,
            user_id,
            carry_facility,
            "Kept at the same facility when the department was removed",
        )
        changed = True
    if (
        current["role"] in facility_service.ENTERPRISE_GRANT_ROLES
        and new_role not in facility_service.ENTERPRISE_GRANT_ROLES
    ):
        grant = _live_grant(db, user_id)
        if grant is not None:
            _revoke_grant(
                db, request, actor, grant, f"Role changed from {current['role']} to {new_role}"
            )
            changed = True
    return changed


def _staff_facility_view(db, actor: dict, rows) -> dict | None:
    """What User Management shows about facilities, or None while there is one
    facility on record and ORB_MULTI_SITE is off (the list then keeps its old
    shape). With more than one facility on record, a facility-bound
    administrator lists only the accounts working at their facilities."""
    multi = facility_service.multi_facility_data(db=db)
    if not multi and not _multi_site_on():
        return None
    scope = facility_service.facility_scope_for(actor, db=db) if multi else None
    row_facility = {r["id"]: facility_service.user_facility_id(r, db=db) for r in rows}
    labels: dict = {}
    for fid in set(row_facility.values()):
        labels[fid] = facility_service.facility_identity(fid, db=db)
    facilities = [
        facility_service.facility_identity(int(f["id"]), db=db)
        for f in facility_service.list_facilities(db=db)
        if f.get("is_active") and _in_scope(scope, int(f["id"]))
    ]
    master: dict[str, int] = {}
    department_facilities: dict[str, int] = {}
    if _has_table(db, "departments"):
        for r in db.execute(
            "SELECT name, facility_id, is_active FROM departments ORDER BY name COLLATE NOCASE"
        ).fetchall():
            master[r["name"].strip().casefold()] = int(r["facility_id"])
            if r["is_active"] and _in_scope(scope, int(r["facility_id"])):
                department_facilities[r["name"]] = int(r["facility_id"])
    manageable = _holds_grant(db, actor)
    grants: dict = {}
    if manageable:
        grants = {
            r["user_id"]: r["id"]
            for r in db.execute(
                "SELECT id, user_id FROM enterprise_access_grants WHERE revoked_at IS NULL"
            ).fetchall()
        }
    return {
        "scope": scope,
        "visible": {
            uid
            for uid, fid in row_facility.items()
            if uid == actor.get("id") or _in_scope(scope, fid)
        },
        "user_facility": {uid: labels.get(fid) for uid, fid in row_facility.items()},
        "facilities": facilities,
        "master": master,
        "department_facilities": department_facilities,
        "own_facility_id": facility_service.user_facility_id(actor, db=db),
        "grants_manageable": manageable,
        "grants": grants,
    }


# ──────────────────────────────────────────────────────────────────────────────
#  List
# ──────────────────────────────────────────────────────────────────────────────
_LIST_USERS_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.get("")
def list_users(include_patients: bool = False, user: dict = _LIST_USERS_USER_DEFAULT):
    """List staff accounts with recent auth activity.

    Service identities are never listed (they are not logins and cannot be
    changed). Patient portal accounts are left out unless ``include_patients``
    is set, so the default list is the hospital's staff. ``departments`` is the
    set a clinical account can usefully be scoped to.

    Never returns password_hash / pin_hash / totp_secret."""
    from datetime import datetime, timedelta

    cutoff_24h = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
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
        # multisite-6: None (and nothing read) with one facility and the switch off.
        facility_view = _staff_facility_view(db, user, rows)

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

    body: dict[str, Any] = {
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
    if facility_view is None:
        return body

    # multisite-6: each account's facility, the facilities and departments this
    # administrator may place staff at, and (for a holder only) who holds
    # group-wide access. A facility-bound administrator lists only the
    # accounts working at their facilities.
    view = facility_view
    body["users"] = [u for u in body["users"] if u["id"] in view["visible"]]
    for u in body["users"]:
        u["facility"] = view["user_facility"].get(u["id"])
        if view["grants_manageable"]:
            grant_id = view["grants"].get(u["id"])
            u["enterprise_access"] = grant_id is not None
            u["enterprise_grant_id"] = grant_id
    if view["scope"] is not None:
        body["departments"] = [
            d
            for d in departments
            if _in_scope(view["scope"], view["master"].get(d.strip().casefold()))
        ]
    body["facilities"] = view["facilities"]
    body["department_facilities"] = view["department_facilities"]
    body["own_facility_id"] = view["own_facility_id"]
    body["grants_manageable"] = view["grants_manageable"]
    return body


# ──────────────────────────────────────────────────────────────────────────────
#  Create
# ──────────────────────────────────────────────────────────────────────────────
_CREATE_USER_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("", status_code=201)
def create_user(
    body: CreateUserBody,
    request: Request,
    user: dict = _CREATE_USER_USER_DEFAULT,
):
    """Create a new user with a bcrypt-hashed password."""
    user_id = body.id.strip()
    name = body.name.strip()
    if not user_id or not name:
        raise HTTPException(status_code=400, detail="id and name are required")
    if _is_reserved_id(user_id):
        raise HTTPException(
            status_code=409, detail="This ID is reserved for a system account. Choose another."
        )
    _validate_role(body.role)
    if body.role == "patient":
        # A portal login made here is linked to no patient record (admission
        # links patients.user_id); it would be an orphan account.
        raise HTTPException(
            status_code=400,
            detail="Patient portal accounts are created when the patient is admitted, not here",
        )
    _require_role_switch(body.role)
    department = (body.department.strip() or None) if body.department else None
    _require_department(body.role, department)
    # multisite-6: the home facility row to write in the same transaction, or
    # None (facility 1, unchanged) with one facility and the switch off.
    home_facility = _home_facility_for_new_account(
        request, user, user_id, department, body.facility_id
    )
    department = _master_spelling(department)

    password_hash = hash_password(body.password)

    try:
        with db_session() as db:
            # Guard against duplicate id up front for a clean 409; the PK also
            # backstops this if two admins race.
            existing = db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail="A user with this id already exists")
            db.execute(
                """INSERT INTO users (id, name, role, department, password_hash, is_active, token_version)
                   VALUES (?, ?, ?, ?, ?, 1, 0)""",
                (user_id, name, body.role, department, password_hash),
            )
            if home_facility is not None:
                _write_home_row(
                    db,
                    request,
                    user,
                    user_id,
                    home_facility,
                    "Home facility chosen when the account was created",
                )
    except DBIntegrityError:
        # Lost the race, or CHECK constraint tripped despite validation.
        raise HTTPException(status_code=409, detail="A user with this id already exists") from None
    except AuditWriteError:
        # The home row's security record could not be written: the account
        # and its row were rolled back together.
        raise _audit_unavailable() from None
    if home_facility is not None:
        facility_service.invalidate_cache()

    _audit(
        request,
        user,
        "user_created",
        "success",
        f"Created user {user_id} ({body.role})",
        user_id,
        metadata={
            "role": body.role,
            "department": department,
            **({"facility_id": home_facility} if home_facility is not None else {}),
        },
    )
    out: dict[str, Any] = {
        "id": user_id,
        "name": name,
        "role": body.role,
        "department": department,
        "is_active": True,
        "totp_enabled": False,
    }
    if home_facility is not None or _facility_view_on():
        out["facility"] = facility_service.user_facility(
            {"id": user_id, "role": body.role, "department": department}
        )
    return out


# ──────────────────────────────────────────────────────────────────────────────
#  Deactivate / Activate
# ──────────────────────────────────────────────────────────────────────────────
_DEACTIVATE_USER_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/{user_id}/deactivate")
def deactivate_user(
    user_id: str,
    request: Request,
    user: dict = _DEACTIVATE_USER_USER_DEFAULT,
):
    """Disable login for a user and revoke all outstanding JWTs.

    An admin cannot deactivate themselves (that would lock the operator out of
    their own live session and could orphan the last admin account)."""
    if user_id == user.get("id"):
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    _refuse_service_account(request, user, user_id, "deactivate")
    _require_target_in_scope(request, user, user_id, "deactivate")

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
        request,
        user,
        "user_deactivated",
        "success",
        f"Deactivated user {user_id}; outstanding sessions revoked",
        user_id,
    )
    return {"status": "deactivated", "id": user_id}


_ACTIVATE_USER_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/{user_id}/activate")
def activate_user(
    user_id: str,
    request: Request,
    user: dict = _ACTIVATE_USER_USER_DEFAULT,
):
    """Re-enable login for a previously deactivated user."""
    _refuse_service_account(request, user, user_id, "activate")
    _require_target_in_scope(request, user, user_id, "activate")
    with db_session() as db:
        _get_user_row(db, user_id)
        db.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))

    _audit(
        request,
        user,
        "user_activated",
        "success",
        f"Activated user {user_id}",
        user_id,
    )
    return {"status": "activated", "id": user_id}


# ──────────────────────────────────────────────────────────────────────────────
#  Admin reset password
# ──────────────────────────────────────────────────────────────────────────────
_RESET_PASSWORD_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: str,
    body: ResetPasswordBody,
    request: Request,
    user: dict = _RESET_PASSWORD_USER_DEFAULT,
):
    """Set a new bcrypt password for a user and revoke their existing sessions."""
    _refuse_service_account(request, user, user_id, "password reset")
    _require_target_in_scope(request, user, user_id, "password reset")
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
        request,
        user,
        "user_password_reset",
        "success",
        f"Reset password for user {user_id}; sessions revoked",
        user_id,
    )
    return {"status": "password_reset", "id": user_id}


# ──────────────────────────────────────────────────────────────────────────────
#  Signing-PIN provisioning
# ──────────────────────────────────────────────────────────────────────────────
_SET_USER_PIN_USER_DEFAULT = Depends(get_current_user)


@router.post("/{user_id}/pin")
def set_user_pin(
    user_id: str,
    body: SetPinBody,
    request: Request,
    user: dict = _SET_USER_PIN_USER_DEFAULT,
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
            request,
            user,
            "user_pin_provision_denied",
            "denied",
            f"Refused PIN provisioning for {user_id}: not admin and not self",
            user_id,
        )
        raise HTTPException(status_code=403, detail="Only an admin can set another user's PIN")
    _refuse_service_account(request, user, user_id, "signing PIN")
    if not is_self:
        _require_target_in_scope(request, user, user_id, "signing PIN")

    if is_self:
        # 400 (not 401) so the web client's session-expired interceptor is not
        # tripped — same convention as the password endpoints above.
        if not body.password:
            raise HTTPException(
                status_code=400, detail="Setting your own PIN requires your password"
            )
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
            f"Signing PIN self-provisioned by user {user_id}"
            if is_self
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
_PATCH_USER_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.patch("/{user_id}")
def patch_user(
    user_id: str,
    body: PatchUserBody,
    request: Request,
    user: dict = _PATCH_USER_USER_DEFAULT,
):
    """Update a user's role and/or department. Role is validated against the
    users.role CHECK constraint's allowed set. A patient portal account and a
    staff account never cross into each other's role, and a doctor or nurse is
    never left without a department."""
    if body.role is None and body.department is None:
        raise HTTPException(status_code=400, detail="Provide role and/or department to update")
    _refuse_service_account(request, user, user_id, "role change")
    _require_target_in_scope(request, user, user_id, "role change")

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
        # multisite-6: the new department must belong to this administrator's
        # facilities (more than one facility on record only).
        _require_department_in_scope(request, user, user_id, dept, "role change")
        # multisite-3: stored in the master's spelling (more than one only).
        dept = _master_spelling(dept)
        sets.append("department = ?")
        params.append(dept)
        changed["department"] = dept

    params.append(user_id)
    try:
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
            if new_role != current["role"]:
                _require_role_switch(new_role)
            new_dept = changed["department"] if "department" in changed else current["department"]
            assert new_dept is None or isinstance(new_dept, str)
            _require_department(new_role, new_dept)
            # Where the account works now, so a removed department leaves it at
            # the same facility (more than one facility on record only).
            carry_facility = (
                facility_service.user_facility_id(current, db=db)
                if facility_service.multi_facility_data(db=db)
                else None
            )
            db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
            facility_rows_changed = _settle_facility_rows(
                db, request, user, current, new_role, new_dept, carry_facility
            )
            row = db.execute(
                "SELECT id, name, role, department FROM users WHERE id = ?", (user_id,)
            ).fetchone()
    except AuditWriteError:
        # A facility row's or grant's security record could not be written:
        # the whole change was rolled back.
        raise _audit_unavailable() from None
    if facility_rows_changed:
        facility_service.invalidate_cache()

    _audit(
        request,
        user,
        "user_updated",
        "success",
        f"Updated user {user_id}: {', '.join(changed.keys())}",
        user_id,
        metadata=changed,
    )
    out: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "department": row["department"],
    }
    if _facility_view_on():
        out["facility"] = facility_service.user_facility(row)
    return out


# ──────────────────────────────────────────────────────────────────────────────
#  Home facility move (multisite-6)
# ──────────────────────────────────────────────────────────────────────────────
_MOVE_USER_FACILITY_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/{user_id}/facility", dependencies=[Depends(feature_gate("multi_site"))])
def move_user_facility(
    user_id: str,
    body: MoveFacilityBody,
    request: Request,
    user: dict = _MOVE_USER_FACILITY_USER_DEFAULT,
):
    """Move an account with no department to another home facility. The live
    home row is revoked and a new one inserted in ONE transaction, each with a
    fail-closed security audit row; rows are never deleted.

    404 while ORB_MULTI_SITE is off. Refused: a service account (409), a
    patient portal account (400), an account with a department (409: it works
    at its department's facility), a facility that does not exist or is
    closed (400), and an account, or a facility, outside the administrator's
    facilities (403, audited)."""
    _refuse_service_account(request, user, user_id, "facility move")
    reason = (body.reason or "").strip() or None
    target = body.facility_id
    try:
        with db_session(immediate=True) as db:
            current = _get_user_row(db, user_id)
            if current["role"] == "patient":
                raise _Refusal(
                    400, "patient_account", "A patient portal account has no home facility."
                )
            if (
                user_id != user.get("id")
                and current["role"] in facility_service.ENTERPRISE_GRANT_ROLES
                and _live_grant(db, user_id) is not None
                and not _holds_grant(db, user)
            ):
                raise _holder_target_refused(user_id, "facility move")
            if department_scope({"department": current["department"]}) is not None:
                raise _Refusal(409, "home_facility_refused", _HOME_ROW_REFUSED)
            facility = _facility_row(db, target)
            if facility is None:
                raise _Refusal(400, "facility_not_found", "There is no facility with that number.")
            if not facility["is_active"]:
                raise _Refusal(
                    400,
                    "facility_inactive",
                    "That facility is closed, so no staff can be moved there.",
                )
            live = _live_home_row(db, user_id)
            from_facility = (
                int(live["facility_id"]) if live is not None else facility_service.MAIN_FACILITY_ID
            )
            scope = facility_service.facility_scope_for(user, db=db)
            if not (_in_scope(scope, from_facility) and _in_scope(scope, target)):
                raise _Refusal(
                    403,
                    "facility_scope",
                    _OUTSIDE_SCOPE,
                    audit=(
                        "user_outside_facility_scope",
                        f"Refused facility move of user {user_id}: outside the "
                        "administrator's facilities",
                        {
                            "action": "facility move",
                            "from_facility_id": from_facility,
                            "to_facility_id": target,
                            "facility_scope": _scope_list(scope),
                        },
                    ),
                )
            moved = from_facility != target
            if moved:
                words = reason or f"Moved to facility {facility['code']}"
                if live is not None:
                    _revoke_home_row(db, request, user, user_id, live, words)
                _write_home_row(db, request, user, user_id, target, words)
    except _Refusal as refusal:
        raise _refused(request, user, user_id, refusal) from refusal
    except AuditWriteError:
        raise _audit_unavailable() from None
    except DBIntegrityError:
        # A live row for this account at that facility already exists (a
        # visiting row, multisite-19).
        raise HTTPException(
            status_code=409,
            detail={
                "error": "facility_row_exists",
                "message": "This account already has a live row at that facility.",
            },
        ) from None
    if moved:
        facility_service.invalidate_cache()
    return {"id": user_id, "moved": moved, "facility": facility_service.facility_identity(target)}


# ──────────────────────────────────────────────────────────────────────────────
#  Group-wide (enterprise) access grants (multisite-6, ruling MS-R6 (a))
# ──────────────────────────────────────────────────────────────────────────────
def _grant_out(row) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "user_name": row["user_name"],
        "user_role": row["user_role"],
        "granted_by": row["granted_by"],
        "granted_at": row["granted_at"],
        "reason": row["reason"],
        "revoked_at": row["revoked_at"],
        "revoked_by": row["revoked_by"],
        "revoke_reason": row["revoke_reason"],
        "live": row["revoked_at"] is None,
    }


_GRANT_SELECT = (
    "SELECT g.id, g.user_id, u.name AS user_name, u.role AS user_role, g.granted_by, "
    "g.granted_at, g.reason, g.revoked_at, g.revoked_by, g.revoke_reason "
    "FROM enterprise_access_grants g LEFT JOIN users u ON u.id = g.user_id"
)


def _not_a_holder(action: str) -> _Refusal:
    return _Refusal(
        403,
        "enterprise_grant_required",
        _GRANT_HOLDERS_ONLY,
        audit=(
            "enterprise_access_denied",
            f"Refused {action}: the administrator holds no group-wide access",
            {"action": action},
        ),
    )


def _reason_or_400(raw: str) -> str:
    reason = (raw or "").strip()
    if len(reason) < _MIN_REASON_LEN:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "reason_required",
                "message": f"Give a reason of at least {_MIN_REASON_LEN} characters.",
            },
        )
    return reason


_LIST_ENTERPRISE_GRANTS_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.get("/enterprise-grants")
def list_enterprise_grants(
    request: Request,
    user: dict = _LIST_ENTERPRISE_GRANTS_USER_DEFAULT,
):
    """Every group-wide access grant, live first, then the revoked history.
    Only an administrator who holds a live grant may read it (403, audited)."""
    try:
        with db_session() as db:
            if not _holds_grant(db, user):
                raise _not_a_holder("list group-wide access")
            rows = db.execute(
                f"{_GRANT_SELECT} ORDER BY (g.revoked_at IS NOT NULL), g.granted_at DESC, "
                "g.id DESC LIMIT 500"
            ).fetchall()
    except _Refusal as refusal:
        raise _refused(request, user, None, refusal) from refusal
    return {"grants": [_grant_out(r) for r in rows]}


_GRANT_ENTERPRISE_ACCESS_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post(
    "/enterprise-grants", status_code=201, dependencies=[Depends(feature_gate("multi_site"))]
)
def grant_enterprise_access(
    body: GrantEnterpriseBody,
    request: Request,
    user: dict = _GRANT_ENTERPRISE_ACCESS_USER_DEFAULT,
):
    """Give an administrator group-wide access, with a reason. 404 while
    ORB_MULTI_SITE is off. Only a holder may grant (403, audited); only an
    active administrator may hold one (400, audited); a service account is
    refused (409). The grant and its fail-closed security audit row are one
    transaction."""
    target_id = body.user_id.strip()
    reason = _reason_or_400(body.reason)
    _refuse_service_account(request, user, target_id, "group-wide access")
    if _is_reserved_id(target_id):
        raise HTTPException(status_code=409, detail="This ID is reserved for a system account.")
    try:
        with db_session(immediate=True) as db:
            if not _holds_grant(db, user):
                raise _not_a_holder("grant group-wide access")
            target = db.execute(
                "SELECT id, role, COALESCE(is_active, 1) AS is_active FROM users WHERE id = ?",
                (target_id,),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail="User not found")
            if target["role"] not in facility_service.ENTERPRISE_GRANT_ROLES:
                raise _Refusal(
                    400,
                    "enterprise_grant_admin_only",
                    "Group-wide access is only for administrators.",
                    audit=(
                        "enterprise_access_denied",
                        f"Refused group-wide access for user {target_id}: role {target['role']}",
                        {"action": "grant group-wide access", "target_role": target["role"]},
                    ),
                )
            if not target["is_active"]:
                raise _Refusal(
                    400, "account_inactive", "This account is switched off. Activate it first."
                )
            if _live_grant(db, target_id) is not None:
                raise _Refusal(
                    409, "already_granted", "This administrator already has group-wide access."
                )
            cur = db.execute(
                "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
                "VALUES (?, ?, CURRENT_TIMESTAMP, ?)",
                (target_id, user.get("id"), reason),
            )
            grant_id = cur.lastrowid
            _security_row(
                db,
                request,
                user,
                "enterprise_access_granted",
                f"Group-wide access granted to user {target_id}",
                target_id=str(target_id) if target_id is not None else None,
                metadata={"grant_id": grant_id, "reason": reason},
            )
            row = db.execute(f"{_GRANT_SELECT} WHERE g.id = ?", (grant_id,)).fetchone()
    except _Refusal as refusal:
        raise _refused(request, user, target_id, refusal) from refusal
    except AuditWriteError:
        raise _audit_unavailable() from None
    except DBIntegrityError:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_granted",
                "message": "This administrator already has group-wide access.",
            },
        ) from None
    facility_service.invalidate_cache()
    return _grant_out(row)


_REVOKE_ENTERPRISE_ACCESS_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/enterprise-grants/{grant_id}/revoke")
def revoke_enterprise_access(
    grant_id: int,
    body: RevokeEnterpriseBody,
    request: Request,
    user: dict = _REVOKE_ENTERPRISE_ACCESS_USER_DEFAULT,
):
    """Revoke a group-wide access grant, with a reason. Works whatever
    ORB_MULTI_SITE says (a revoke only narrows). Only a holder may revoke
    (403, audited). The revoke and its fail-closed security audit row are one
    transaction, and the scope cache is forgotten after the commit, so the
    account is facility-bound again on its next request."""
    reason = _reason_or_400(body.reason)
    try:
        with db_session(immediate=True) as db:
            if not _holds_grant(db, user):
                raise _not_a_holder("revoke group-wide access")
            grant = db.execute(
                "SELECT id, user_id, revoked_at FROM enterprise_access_grants WHERE id = ?",
                (grant_id,),
            ).fetchone()
            if grant is None:
                raise HTTPException(status_code=404, detail="Grant not found")
            if grant["revoked_at"] is not None:
                raise _Refusal(409, "already_revoked", "This grant was already revoked.")
            _revoke_grant(db, request, user, grant, reason)
            row = db.execute(f"{_GRANT_SELECT} WHERE g.id = ?", (grant_id,)).fetchone()
    except _Refusal as refusal:
        raise _refused(request, user, None, refusal) from refusal
    except AuditWriteError:
        raise _audit_unavailable() from None
    facility_service.invalidate_cache()
    return _grant_out(row)


# ──────────────────────────────────────────────────────────────────────────────
#  Self-service password change
# ──────────────────────────────────────────────────────────────────────────────
_CHANGE_OWN_PASSWORD_USER_DEFAULT = Depends(get_current_user)


@router.post("/me/password")
def change_own_password(
    body: SelfPasswordBody,
    request: Request,
    user: dict = _CHANGE_OWN_PASSWORD_USER_DEFAULT,
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
        request,
        user,
        "user_self_password_change",
        "success",
        "User changed own password; all sessions revoked",
        user["id"],
    )
    return {"status": "password_changed", "relogin": True}
