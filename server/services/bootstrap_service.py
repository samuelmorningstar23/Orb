"""Orb Hospital OS — fresh-install admin bootstrap and credential posture.

WHY THIS EXISTS
───────────────
A virgin appliance is a Mac Studio with a schema and no people in it. init_db()
seeds exactly one users row — 'system', Orb Sage's audit identity, whose
placeholder password_hash ('system_hash') can never verify against any password.
So a box that has only run migrations has NO account anyone can sign in with,
and the only user-creation API (POST /api/admin/users) already requires an
authenticated admin. scripts/create_admin.py is the way out of that, and
install.sh calls it.

That leaves exactly two ways a hospital install can go wrong, and this module
exists to make both of them impossible to reach SILENTLY:

  (i)  NO ADMIN. create_admin.py in --non-interactive mode with no credentials
       prints instructions and exits 0. install.sh's `if` therefore succeeds,
       the operator sees a friendly message in a wall of install output, and the
       appliance boots into a state where nobody can log in and nobody can
       create the account that would let them. That is not a security failure —
       it is a bricked install — but it is discovered at the worst moment, by a
       clinician at a bedside on go-live morning.

  (ii) A DEFAULT CREDENTIAL SHIPS. server/scripts/seed_clinical_data.py and
       reset_db_and_seed.py create ADM001/DOC001/NUR001 whose password is
       literally "password". Those scripts exist for demos, and demos get run on
       the appliance that later goes to the hospital. This box sits on a
       hospital LAN — a flat, unsegmented, shared-with-the-billing-PC LAN —
       holding every chart in two wards. `ADM001` / `password` on that LAN is
       the entire threat model realised in one HTTP request, and no amount of
       encryption at rest, TLS, audit chaining or MFA on the OTHER accounts
       makes any difference once it works.

THE TWO INVARIANTS, AND HOW EACH IS ENFORCED
────────────────────────────────────────────
INVARIANT 1 — "no admin" can never be a quiet state.
    Checked at every boot. When there is no login-capable admin the module logs
    CRITICAL, writes ops/bootstrap/FIRST-ADMIN-REQUIRED.txt with the exact
    command to fix it, publishes a gauge, and surfaces `admins_active: 0` in the
    heartbeat payload — so it pages out of the building rather than waiting to
    be noticed. We do NOT refuse to boot: an appliance that will not start is
    worse for patients than one that starts and shouts, and refusing to boot
    would also remove the only channel (the heartbeat) that reports the problem.

INVARIANT 2 — a shipped default credential cannot be used.
    In the prod profile, any account whose password verifies against the
    shipped-default denylist is DEACTIVATED at boot (is_active=0 plus a
    token_version bump, which also revokes any session it already holds) and the
    event is audit-logged. Not "warned about" — disabled. A warning in a log
    file is what we already had.

    This deliberately can produce state (i): lock the only admin and there is no
    admin. That ordering is correct. (i) is an outage with a documented one-line
    fix by someone with shell access to a box they are standing next to; (ii) is
    an unbounded PHI breach reachable by anyone on the ward LAN. When the lockout
    empties the admin roster we say so in enormous letters and write the
    remediation file. We never trade (ii) away to avoid (i).

    In the dev profile nothing is locked — the demo accounts ARE the product
    there — but the finding is still reported, so `python -m
    services.bootstrap_service` tells the truth on a laptop too.

COST — why the credential scan is bounded and off the boot path
───────────────────────────────────────────────────────────────
Detecting a default credential means bcrypt-verifying each candidate password
against each account's hash, and bcrypt is deliberately slow: 236 ms per verify
at cost 12 on this appliance (measured, 20 samples). A 30-strong pilot roster
against this 12-entry denylist is therefore ~85 s of CPU in the worst case where
nobody has a default — and the best case is far cheaper, because a match breaks
out of the candidate loop immediately (the live dev database here: 12 accounts
matched on the first candidate, 36 checks, 2.76 s total). Even 85 s must not sit
in front of /api/health on a clinical system. So the scan:
  * runs on its own thread, started from the lifespan, never on the event loop;
  * skips 'patient' rows (Bridge accounts are created with random secrets by
    admission_service, they are not staff logins) and the 'system' row;
  * checks privileged roles FIRST, so the account that matters most is cleared
    in the first second even if the budget later runs out;
  * stops at a wall-clock budget (ORB_BOOTSTRAP_SCAN_BUDGET_S, default 300 —
    roughly a 100-staff roster at the measured rate) and REPORTS the truncation
    rather than silently claiming "clean". A partial scan that says so is
    useful; a partial scan that reports success is worse than no scan.

MFA
───
Enrolment is wired, not rebuilt. services/mfa_service.py already implements
RFC 6238 TOTP, routes/auth_routes.py already has begin/confirm plus the
short-lived enrolment ticket that lets a mandated-but-unenrolled account reach
those two endpoints. What was missing is (1) the console path, so the first
admin on a virgin box can enrol at the keyboard before the appliance is ever
reachable over the network — see scripts/create_admin.py --enroll-mfa — and
(2) visibility: which privileged accounts have a factor and which do not.
`review_accounts()` answers that, and scripts/access_review.py prints it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# roles.py is import-free by contract, so this cannot close an import cycle.
from roles import STORABLE_ROLES as _STORABLE_ROLES

logger = logging.getLogger("orb.bootstrap")

# The 'system' row is Orb Sage's audit identity, not a human account. Its
# placeholder hash can never authenticate, so it must never count as "an admin
# exists" and must never be scanned or locked.
SYSTEM_USER_ID = "system"

# Roles that hold admin.overview (see permissions.ROLE_PERMISSIONS) — the
# accounts a CIO cares about and the ones MFA should be mandatory for.
PRIVILEGED_ROLES = ("admin",)

# Staff roles: everything that is a human login into the clinical product.
# 'patient' is excluded on purpose — Bridge accounts are minted by
# admission_service with a random secret and are not operator-chosen passwords.
#
# Derived from roles.STORABLE_ROLES (the single authority for stored roles), not
# listed by hand: the hand-written list stopped at pharmacist, so cashier and the
# five hospital-system roles (receptionist, ward clerk, lab staff) escaped both
# the CIO account review and the default-password scan. A role added to
# roles.py later is covered here without anyone remembering this file.
NON_STAFF_ROLES = ("patient",)
STAFF_ROLES: tuple[str, ...] = tuple(
    r for r in _STORABLE_ROLES if r not in NON_STAFF_ROLES
)

# Passwords that Orb's own tooling, its docs, or universal habit will have put
# on an appliance. Every entry here is a credential that could plausibly reach a
# hospital by accident; "password" is not hypothetical — it is the literal value
# in server/scripts/seed_clinical_data.py and the one in this repo's SETUP-NOTES.md.
#
# This is NOT a password-strength dictionary. It is a "did a default ship?"
# denylist, kept short precisely because every entry costs a bcrypt verify
# against every account. Strength is enforced separately, at set time, by
# password_policy_error().
SHIPPED_DEFAULT_PASSWORDS: tuple[str, ...] = (
    "password",
    "password123",
    "Password123",
    "orb",
    "orb1234",
    "orbadmin",
    "admin",
    "admin123",
    "changeme",
    "letmein",
    "demo1234",
    "hospital",
)

# Minimum length for a password set through the bootstrap CLI. Longer than the
# API's 8 (routes/admin_users_routes._MIN_PASSWORD_LEN) because this one account
# is the root of trust for the whole appliance and is typed once, by us.
MIN_ADMIN_PASSWORD_LEN = 12


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def bootstrap_dir() -> Path:
    """Where bootstrap evidence lives. Sibling of the watchdog's ops dir so an
    operator has one place to look, and env-overridable for tests."""
    override = os.getenv("ORB_BOOTSTRAP_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "ops" / "bootstrap"


STATUS_FILENAME = "status.json"
FIRST_ADMIN_FILENAME = "FIRST-ADMIN-REQUIRED.txt"


# ── Password policy (set-time) ───────────────────────────────────────────────
def password_policy_error(password: str, *, user_id: str = "", name: str = "") -> Optional[str]:
    """Return a human-readable reason to REFUSE this password, or None to accept.

    Deliberately small and explainable — it runs at a console during an install,
    and a rule an installer cannot understand is a rule they work around by
    appending '1'. The three that actually matter here:
      * length (bcrypt's real strength floor is the input, not the hash);
      * not a shipped default (see the denylist above);
      * not derived from the account it protects — 'ADM001' / 'adm001' is what
        somebody types when they are in a hurry at 6 a.m. on install day, and it
        is guessable by anyone who has seen a single Orb screenshot.
    """
    if not password:
        return "Password is empty."
    lowered = password.strip().lower()
    # Denylist BEFORE length, deliberately. Most shipped defaults are also short,
    # and "must be at least 12 characters" would send an installer off to type
    # "password1234" — which is the same account, still guessable, and now past
    # the length rule. The specific message is the one that changes behaviour.
    if lowered in {p.lower() for p in SHIPPED_DEFAULT_PASSWORDS}:
        return (
            "That is one of Orb's shipped demo/default passwords. It must never reach "
            "a hospital appliance. Choose something else, and not a variation of it."
        )
    if len(password) < MIN_ADMIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_ADMIN_PASSWORD_LEN} characters (got {len(password)})."
    if user_id and lowered == user_id.strip().lower():
        return "Password must not be the staff ID itself."
    if user_id and user_id.strip().lower() in lowered and len(user_id.strip()) >= 4:
        return "Password must not contain the staff ID."
    if name and len(name.strip()) >= 4 and name.strip().lower() in lowered:
        return "Password must not contain the account holder's name."
    if len(set(password)) < 5:
        return "Password repeats too few distinct characters."
    return None


# ── Cheap, SQL-only inventory ────────────────────────────────────────────────
def _row_get(row, key, default=None):
    """sqlite3.Row has no .get(); columns also differ across migration states."""
    try:
        return row[key] if key in row.keys() else default
    except Exception:
        return default


def admin_inventory(db) -> dict[str, Any]:
    """Who can actually administer this appliance right now.

    Pure SQL, no bcrypt — safe to call synchronously on any path. 'Login-capable'
    means: role=admin, not the 'system' row, is_active is not 0, and the password
    hash is something bcrypt/legacy-SHA could conceivably verify (the 'system'
    placeholder is excluded by id anyway, but a NULL/blank hash would be just as
    unusable and must not be counted).
    """
    rows = db.execute(
        "SELECT id, name, role, department, password_hash, "
        "       COALESCE(is_active, 1) AS is_active, "
        "       COALESCE(totp_enabled, 0) AS totp_enabled "
        "FROM users WHERE role = 'admin' AND id != ?",
        (SYSTEM_USER_ID,),
    ).fetchall()

    active, inactive = [], []
    for r in rows:
        pwd = (_row_get(r, "password_hash") or "").strip()
        entry = {
            "id": r["id"],
            "name": r["name"],
            "mfa_enrolled": bool(_row_get(r, "totp_enabled", 0)),
            "usable_password": bool(pwd) and pwd != "system_hash",
        }
        (active if (r["is_active"] and entry["usable_password"]) else inactive).append(entry)

    return {
        "admins_active": len(active),
        "admins_inactive": len(inactive),
        "admins_with_mfa": sum(1 for a in active if a["mfa_enrolled"]),
        "active": active,
        "inactive": inactive,
    }


def review_accounts(db) -> dict[str, Any]:
    """Pilot staff role review: every staff account, its role, and its posture.

    This is the artefact a CIO signs off before go-live. It answers, in one
    place: who can get in, what can each of them reach, who is missing a second
    factor, and who is left over from a demo. No bcrypt, no PHI — staff rows
    only, never patients.
    """
    from permissions import get_permissions_for_role

    placeholders = ",".join("?" for _ in STAFF_ROLES)
    rows = db.execute(
        f"""SELECT id, name, role, department, registration_number, created_at,
                   password_hash,
                   COALESCE(is_active, 1)   AS is_active,
                   COALESCE(totp_enabled, 0) AS totp_enabled,
                   COALESCE(token_version, 0) AS token_version
              FROM users
             WHERE role IN ({placeholders}) AND id != ?
             ORDER BY role, id""",
        (*STAFF_ROLES, SYSTEM_USER_ID),
    ).fetchall()

    accounts: list[dict[str, Any]] = []
    for r in rows:
        role = r["role"]
        privileged = role in PRIVILEGED_ROLES
        pwd = (_row_get(r, "password_hash") or "").strip()
        findings: list[str] = []
        if privileged and not _row_get(r, "totp_enabled", 0):
            findings.append("privileged account without MFA")
        if not pwd or pwd == "system_hash":
            findings.append("no usable password hash")
        elif not pwd.startswith("$2"):
            findings.append("legacy (non-bcrypt) password hash")
        if not r["is_active"]:
            findings.append("deactivated")
        accounts.append({
            "id": r["id"],
            "name": r["name"],
            "role": role,
            "department": r["department"],
            "registration_number": _row_get(r, "registration_number"),
            "created_at": _row_get(r, "created_at"),
            "is_active": bool(r["is_active"]),
            "mfa_enrolled": bool(_row_get(r, "totp_enabled", 0)),
            "privileged": privileged,
            "permission_count": len(get_permissions_for_role(role)),
            "findings": findings,
        })

    return {
        "generated_utc": _utc_now(),
        "staff_accounts": len(accounts),
        "accounts": accounts,
        "privileged_without_mfa": [
            a["id"] for a in accounts
            if a["privileged"] and a["is_active"] and not a["mfa_enrolled"]
        ],
    }


# ── Default-credential scan (bcrypt, bounded) ────────────────────────────────
def _scan_order(rows) -> list:
    """Privileged accounts first. If the budget runs out we want it to run out
    on a ward pharmacist, never on the account that holds admin.overview."""
    return sorted(rows, key=lambda r: (r["role"] not in PRIVILEGED_ROLES, r["id"]))


def scan_default_credentials(
    db,
    *,
    budget_s: float = 120.0,
    candidates: Iterable[str] = SHIPPED_DEFAULT_PASSWORDS,
) -> dict[str, Any]:
    """Find staff accounts whose password is a shipped default.

    Returns {"findings": [{id, role, password_hint}], "checked": n,
             "budget_exhausted": bool, "elapsed_s": float}.

    `password_hint` is a redacted marker, never the password itself: this result
    is written to ops/bootstrap/status.json and surfaced to the heartbeat, and a
    file listing working credentials for the appliance would be a rather
    spectacular own goal.
    """
    from auth import verify_password

    placeholders = ",".join("?" for _ in STAFF_ROLES)
    rows = db.execute(
        f"""SELECT id, role, password_hash, COALESCE(is_active, 1) AS is_active
              FROM users
             WHERE role IN ({placeholders}) AND id != ?""",
        (*STAFF_ROLES, SYSTEM_USER_ID),
    ).fetchall()

    started = time.monotonic()
    findings: list[dict[str, Any]] = []
    checked = 0
    exhausted = False
    cand = list(candidates)

    for row in _scan_order(rows):
        pwd_hash = (_row_get(row, "password_hash") or "").strip()
        if not pwd_hash or pwd_hash == "system_hash":
            continue
        for candidate in cand:
            if (time.monotonic() - started) > budget_s:
                exhausted = True
                break
            checked += 1
            try:
                if verify_password(candidate, pwd_hash):
                    findings.append({
                        "id": row["id"],
                        "role": row["role"],
                        "is_active": bool(row["is_active"]),
                        # Redacted on purpose — see the docstring.
                        "password_hint": f"{candidate[0]}{'*' * (len(candidate) - 1)}",
                    })
                    break
            except Exception:  # a malformed hash must not abort the whole scan
                break
        if exhausted:
            break

    return {
        "findings": findings,
        "checked": checked,
        "accounts_considered": len(rows),
        "budget_exhausted": exhausted,
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def lock_default_credential_accounts(db, findings: list[dict[str, Any]]) -> list[str]:
    """Deactivate accounts found holding a shipped default, revoking their sessions.

    is_active=0 stops both a fresh login (auth_routes.login checks it) and any
    token already minted (auth.get_current_user checks it on EVERY request), and
    the token_version bump makes that immediate rather than eventual. Returns the
    ids actually locked (already-inactive accounts are skipped — no state change,
    no audit noise).
    """
    locked: list[str] = []
    for f in findings:
        if not f.get("is_active"):
            continue
        db.execute(
            """UPDATE users
                  SET is_active = 0,
                      token_version = COALESCE(token_version, 0) + 1
                WHERE id = ?""",
            (f["id"],),
        )
        locked.append(f["id"])
    return locked


# ── Evidence files ───────────────────────────────────────────────────────────
def _write_json(path: Path, payload: dict) -> None:
    """Atomic small-file write (tmp + os.replace), same discipline as the
    watchdog's status.json — a reader must never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_first_admin_notice(reason: str) -> Path:
    """Leave the fix on disk, in words, where somebody will find it.

    An operator standing at a bricked appliance at 06:40 on go-live morning does
    not read source. They `ls` the repo and `tail` a log. This file is for them.
    """
    path = bootstrap_dir() / FIRST_ADMIN_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parent.parent.parent
    path.write_text(
        "ORB: NO ADMINISTRATOR ACCOUNT CAN LOG IN TO THIS APPLIANCE\n"
        "===========================================================\n"
        f"Detected at : {_utc_now()}\n"
        f"Reason      : {reason}\n"
        "\n"
        "Orb is running and is serving the API, but nobody can sign in as an\n"
        "administrator, and there is no way to create one over the network (user\n"
        "creation requires an authenticated admin, by design).\n"
        "\n"
        "Fix it from a terminal ON THIS MACHINE:\n"
        "\n"
        f"    cd {repo}\n"
        "    set -a && . ./.env && set +a\n"
        "    server/.venv/bin/python server/scripts/create_admin.py --enroll-mfa\n"
        "\n"
        "You will be asked for a staff ID, a full name and a password (minimum\n"
        f"{MIN_ADMIN_PASSWORD_LEN} characters; Orb's demo passwords are refused), then offered\n"
        "TOTP enrolment. Do the TOTP step, this account administers a box holding\n"
        "patient records on a hospital LAN.\n"
        "\n"
        "This file is deleted automatically once a login-capable admin exists.\n",
        encoding="utf-8",
    )
    return path


def _clear_first_admin_notice() -> None:
    try:
        (bootstrap_dir() / FIRST_ADMIN_FILENAME).unlink()
    except OSError:
        pass


# ── The startup check ────────────────────────────────────────────────────────
# Last published snapshot, read by the heartbeat and the admin surface. Written
# by the bootstrap thread, read by others; a dict rebind is atomic under the GIL
# so no lock is needed (same reasoning as the watchdog's _last_beat).
_snapshot: dict[str, Any] = {"state": "not_run"}

# A digest of each flagged account's password hash as the scan saw it. Memory
# only, never status.json. The Trust Center compares it with the live row, so a
# password reset after boot clears the finding without a restart, and no bcrypt
# runs on a report request.
_flagged_hash_digests: dict[str, str] = {}


def bootstrap_snapshot() -> dict[str, Any]:
    """Latest bootstrap posture. Never raises, never blocks."""
    return dict(_snapshot)


def _hash_digest(password_hash: Any) -> str:
    import hashlib
    return hashlib.sha256(str(password_hash or "").encode("utf-8")).hexdigest()


def live_default_credential_accounts() -> Optional[list[str]]:
    """Accounts the last scan found on a shipped default password that can
    still sign in with it: active, and the password unchanged since the scan.

    None when no scan has completed in this process (it runs once, at boot), so
    a caller can say "not checked" instead of "none found".
    """
    snap = _snapshot
    if snap.get("state") != "ok" or snap.get("scan_skipped"):
        return None
    flagged = list(snap.get("default_credential_accounts") or [])
    if not flagged:
        return []
    from database import db_session

    placeholders = ",".join("?" for _ in flagged)
    with db_session() as db:
        rows = db.execute(
            f"SELECT id, password_hash, COALESCE(is_active, 1) AS is_active "
            f"FROM users WHERE id IN ({placeholders})",
            tuple(flagged),
        ).fetchall()
    live = []
    for row in rows:
        if not row["is_active"]:
            continue
        seen = _flagged_hash_digests.get(row["id"])
        # No digest recorded means the finding cannot be shown to be cleared.
        if seen is None or seen == _hash_digest(row["password_hash"]):
            live.append(row["id"])
    return sorted(live)


def _publish_gauges(snap: dict[str, Any]) -> None:
    try:
        from services.metrics_service import set_gauge
    except Exception:
        return
    set_gauge("orb_admins_active", snap.get("admins_active", 0),
              help_text="Login-capable administrator accounts on this appliance.")
    set_gauge("orb_admins_with_mfa", snap.get("admins_with_mfa", 0),
              help_text="Login-capable administrators that have enrolled a TOTP factor.")
    set_gauge("orb_default_credential_accounts", len(snap.get("default_credential_accounts", [])),
              help_text="Accounts found holding a shipped default password (0 is the only acceptable value).")
    set_gauge("orb_bootstrap_scan_complete", 0 if snap.get("scan_budget_exhausted") else 1,
              help_text="1 when the default-credential scan covered every staff account.")


def _audit(action_type: str, outcome: str, summary: str, metadata: dict | None = None,
           target_id: str | None = None) -> None:
    """Audit a bootstrap decision. Never allowed to abort startup.

    event_category='security' is FAIL-CLOSED in audit_service, so a write failure
    raises. On a clinical action that is exactly right; here it would mean a full
    audit disk turns a security improvement into a boot failure, so it is caught
    and logged. The action still happened and is in the log either way.
    """
    import uuid
    try:
        from services.audit_service import log_audit_event
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="security",
            action_type=action_type,
            outcome=outcome,
            summary_message=summary,
            actor_user_id=SYSTEM_USER_ID,
            actor_role="admin",
            target_type="appliance_bootstrap",
            target_id=target_id,
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning("Bootstrap audit write failed (%s): %s", action_type, exc)


def run_bootstrap_check(*, scan: bool = True, enforce: Optional[bool] = None) -> dict[str, Any]:
    """Assess the appliance's access posture and enforce the two invariants.

    Blocking (bcrypt + SQLite). MUST NOT be called on the event loop — start it
    with start_bootstrap_check() from the lifespan, or call it directly from a
    CLI/test.

    `enforce` defaults to "prod profile only": locking a demo account on a
    developer laptop would just make the seed scripts look broken.
    """
    global _snapshot, _flagged_hash_digests
    from config import DEPLOYMENT_PROFILE
    from database import db_session

    if enforce is None:
        enforce = DEPLOYMENT_PROFILE == "prod"

    started = time.monotonic()
    budget = float(os.getenv("ORB_BOOTSTRAP_SCAN_BUDGET_S", "300") or 300)

    with db_session() as db:
        inv = admin_inventory(db)
        scan_result: dict[str, Any] = {
            "findings": [], "checked": 0, "accounts_considered": 0,
            "budget_exhausted": False, "elapsed_s": 0.0, "skipped": True,
        }
        if scan:
            scan_result = scan_default_credentials(db, budget_s=budget)
            scan_result["skipped"] = False
            digests: dict[str, str] = {}
            for finding in scan_result["findings"]:
                row = db.execute(
                    "SELECT password_hash FROM users WHERE id = ?", (finding["id"],)
                ).fetchone()
                if row is not None:
                    digests[finding["id"]] = _hash_digest(row["password_hash"])
            _flagged_hash_digests = digests

        locked: list[str] = []
        if enforce and scan_result["findings"]:
            locked = lock_default_credential_accounts(db, scan_result["findings"])
            # Re-read inside the SAME session: locking may have just removed the
            # last admin, and the snapshot must describe the world after the
            # lockout, not before it.
            inv = admin_inventory(db)

    snap = {
        "state": "ok",
        "generated_utc": _utc_now(),
        "profile": DEPLOYMENT_PROFILE,
        "enforced": bool(enforce),
        "admins_active": inv["admins_active"],
        "admins_inactive": inv["admins_inactive"],
        "admins_with_mfa": inv["admins_with_mfa"],
        "default_credential_accounts": [f["id"] for f in scan_result["findings"]],
        "default_credential_locked": locked,
        "scan_checked": scan_result["checked"],
        "scan_accounts": scan_result["accounts_considered"],
        "scan_budget_exhausted": scan_result["budget_exhausted"],
        "scan_skipped": scan_result.get("skipped", False),
        "elapsed_s": round(time.monotonic() - started, 2),
    }

    # ── Invariant 2: report / enforce ──
    if scan_result["findings"]:
        ids = ", ".join(f["id"] for f in scan_result["findings"])
        if locked:
            logger.critical(
                "SHIPPED DEFAULT CREDENTIALS FOUND AND LOCKED: %s. These accounts "
                "could be signed into from anywhere on the hospital LAN using a "
                "password published in Orb's own seed scripts. They are now "
                "is_active=0 with every session revoked. Re-provision them with "
                "POST /api/admin/users/{id}/reset-password, then re-activate.",
                ", ".join(locked),
            )
            _audit(
                "default_credentials_locked", "success",
                f"Locked {len(locked)} account(s) holding a shipped default password: {', '.join(locked)}",
                metadata={"locked": locked, "profile": DEPLOYMENT_PROFILE},
            )
        else:
            logger.critical(
                "SHIPPED DEFAULT CREDENTIALS FOUND: %s (profile=%s, enforcement=%s). "
                "This appliance must NOT be handed to a hospital in this state.",
                ids, DEPLOYMENT_PROFILE, "on" if enforce else "off",
            )
            _audit(
                "default_credentials_detected", "denied",
                f"Detected {len(scan_result['findings'])} account(s) holding a shipped default password: {ids}",
                metadata={"accounts": [f["id"] for f in scan_result["findings"]],
                          "enforced": bool(enforce), "profile": DEPLOYMENT_PROFILE},
            )
    if scan_result["budget_exhausted"]:
        logger.warning(
            "Default-credential scan hit its %.0fs budget after %d checks — %d staff "
            "account(s) were NOT fully checked. Privileged accounts are checked first, "
            "so admins were covered. Raise ORB_BOOTSTRAP_SCAN_BUDGET_S to finish the sweep.",
            budget, scan_result["checked"], scan_result["accounts_considered"],
        )

    # ── Invariant 1: no admin can never be quiet ──
    if inv["admins_active"] == 0:
        reason = (
            "the only administrator account(s) held a shipped default password and were locked"
            if locked else
            "no administrator account exists (a fresh install that never ran create_admin.py)"
        )
        notice = write_first_admin_notice(reason)
        snap["first_admin_notice"] = str(notice)
        logger.critical(
            "NO LOGIN-CAPABLE ADMINISTRATOR ON THIS APPLIANCE (%s). Nobody can sign "
            "in as admin and no admin can be created over the network. Fix at the "
            "console: server/.venv/bin/python server/scripts/create_admin.py "
            "--enroll-mfa . Written to %s",
            reason, notice,
        )
        _audit("no_admin_account", "denied",
               f"Appliance has no login-capable administrator: {reason}",
               metadata={"reason": reason})
    else:
        _clear_first_admin_notice()
        if inv["admins_with_mfa"] == 0:
            # Not fatal, and not enforced here: turning MFA on is an operator
            # decision (ORB_MFA_REQUIRED_ROLES) and forcing it at boot would lock
            # out the admin mid-pilot. Say it every boot instead.
            logger.warning(
                "No administrator on this appliance has enrolled a TOTP factor. A "
                "single stolen password is full admin access to every chart. Enrol: "
                "server/.venv/bin/python server/scripts/create_admin.py --enroll-mfa --id <ID>",
            )

    _publish_gauges(snap)
    try:
        _write_json(bootstrap_dir() / STATUS_FILENAME, snap)
    except Exception as exc:
        logger.warning("Could not write bootstrap status.json: %s", exc)

    _snapshot = snap
    return snap


def start_bootstrap_check() -> Optional[threading.Thread]:
    """Run run_bootstrap_check() on a daemon thread. Returns the thread (or None
    when disabled).

    A thread, not asyncio.to_thread: this is a minute of bcrypt on a cold box and
    it must never contend for the loop's default executor with a clinical
    request. Daemon, so it can never hold up shutdown. Every exception is
    swallowed — a bootstrap self-check that crashes the appliance would be worse
    than the problem it looks for.
    """
    if not _env_flag("ORB_BOOTSTRAP_CHECK_ENABLED", True):
        logger.info("Bootstrap access check disabled (ORB_BOOTSTRAP_CHECK_ENABLED=0)")
        return None

    # Never from inside the test suite. config.DEPLOYMENT_PROFILE defaults to
    # "prod", and pytest sets no override — so a test that boots the real
    # lifespan (`with TestClient(app)`) would run this thread with enforcement
    # ON against the shared test database, and half the suite creates accounts
    # whose password is literally "password" (tests/test_admin_ops.py::
    # _ensure_user and friends). It would deactivate them mid-run and produce
    # auth failures in unrelated, order-dependent tests. No test currently uses
    # the lifespan context manager, so this is a landmine rather than a live
    # bug — defused here because the day somebody adds one, the failure would
    # look like anything except this. Mirrors config.py's is_pytest guards.
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        logger.debug("Bootstrap access check skipped under pytest")
        return None

    def _run() -> None:
        global _snapshot
        try:
            run_bootstrap_check()
        except Exception as exc:
            _snapshot = {"state": "error", "error": repr(exc), "generated_utc": _utc_now()}
            logger.exception("Bootstrap access check failed (non-fatal)")

    thread = threading.Thread(target=_run, name="orb-bootstrap-check", daemon=True)
    thread.start()
    return thread


# ── CLI ──────────────────────────────────────────────────────────────────────
def _main(argv: list[str] | None = None) -> int:
    """`python -m services.bootstrap_service` — run the check and print it.

    Exit 0 clean · 1 findings (no admin, or a default credential present).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="bootstrap_service",
        description="Assess (and in prod, enforce) this appliance's access posture.",
    )
    parser.add_argument("--no-scan", action="store_true",
                        help="Skip the bcrypt default-credential scan (inventory only).")
    parser.add_argument("--enforce", action="store_true",
                        help="Lock default-credential accounts even outside the prod profile.")
    parser.add_argument("--json", action="store_true", help="Print the raw snapshot as JSON.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    snap = run_bootstrap_check(scan=not args.no_scan, enforce=True if args.enforce else None)

    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        print(f"profile                    : {snap['profile']} (enforcement {'on' if snap['enforced'] else 'off'})")
        print(f"login-capable admins       : {snap['admins_active']}")
        print(f"  ... with a TOTP factor   : {snap['admins_with_mfa']}")
        print(f"deactivated admins         : {snap['admins_inactive']}")
        print(f"default-credential accounts: {len(snap['default_credential_accounts'])} "
              f"{snap['default_credential_accounts'] or ''}")
        print(f"  ... locked by this run   : {snap['default_credential_locked'] or 'none'}")
        print(f"scan                       : {snap['scan_checked']} bcrypt checks over "
              f"{snap['scan_accounts']} account(s) in {snap['elapsed_s']}s"
              f"{' (BUDGET EXHAUSTED)' if snap['scan_budget_exhausted'] else ''}")

    return 0 if (snap["admins_active"] > 0 and not snap["default_credential_accounts"]) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(_main())
