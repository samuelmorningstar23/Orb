#!/usr/bin/env python3
"""Create the first login-capable admin account (production bootstrap).

Why this exists: init_db() seeds only the 'system' row, whose placeholder
password_hash ('system_hash') can never verify against any password — so a fresh
install that runs migrations alone has NO account anyone can sign in with, and
the only user-creation API (POST /api/admin/users) already requires an
authenticated admin. Without this script the only way in is the demo seeder,
which wipes/floods the patient tables and creates accounts whose password is
literally "password" — unacceptable against real PHI.

Idempotent: it does nothing (exit 0) when a real admin already exists, so
install.sh can call it on every re-run.

PASSWORD POLICY. Enforced by services.bootstrap_service.password_policy_error:
a 12-character minimum, no Orb demo/default password, nothing derived from the
staff ID or the holder's name. The same denylist is swept at every boot, and in
the prod profile any account still holding one of those passwords is
DEACTIVATED — so a password refused here would not have survived anyway.

MFA. --enroll-mfa enrols a TOTP factor for the account AT THE CONSOLE, using the
existing services/mfa_service.py (RFC 6238, stdlib-only) and the same two-phase
contract as the HTTP flow: the secret is stored, a live code must be entered, and
only then is totp_enabled flipped on. Doing it here matters because this is the
one moment the operator is physically at the appliance — before it is plugged
into a hospital LAN, and before the account that administers every chart in two
wards is reachable over the network with a single factor. It works on an
account that already exists too (--id ADM001 --enroll-mfa).

FACILITY (multisite-6, 10 October 2026). --facility CODE (env ORB_ADMIN_FACILITY,
default MAIN) names the facility the new admin works at. MAIN, facility 1, writes
nothing extra: an account with no home facility row works at facility 1, as
every account did before multi-site. Any other facility needs ORB_MULTI_SITE on,
must exist and be open, and is refused for an admin given a --department (that
account works at its department's facility). Its home row is written in the
same transaction as the account, with a fail-closed security audit row.

Usage:
    python server/scripts/create_admin.py                      # interactive prompts
    python server/scripts/create_admin.py --enroll-mfa         # + TOTP at the console
    python server/scripts/create_admin.py --id ADM001 --name "Jane Doe"
    python server/scripts/create_admin.py --id ADM001 --enroll-mfa   # enrol an existing admin
    python server/scripts/create_admin.py --facility SAT1      # admin of facility SAT1
    ORB_ADMIN_ID=ADM001 ORB_ADMIN_NAME="Jane Doe" ORB_ADMIN_PASSWORD=... \
        python server/scripts/create_admin.py --non-interactive

Exit codes:
    0  admin created (and enrolled, if asked), or a real admin already existed
    1  bad input (weak/denylisted password, duplicate id, failed MFA confirmation)
    2  nothing could be done and NO admin exists — the appliance has no way in.
       (Non-interactive with no credentials supplied. Deliberately non-zero: the
       previous exit 0 let install.sh's `if` succeed and print a friendly note in
       a wall of output, which is how an appliance reaches a hospital with no
       administrator at all.)
"""

import argparse
import getpass
import os
import sys

# Ensure server root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


from auth import hash_password  # noqa: E402
from database import db_session, init_db  # noqa: E402
from db_backend import DBIntegrityError  # noqa: E402
from services.bootstrap_service import (  # noqa: E402
    MIN_ADMIN_PASSWORD_LEN,
    SYSTEM_USER_ID,
    password_policy_error,
)

MIN_PASSWORD_LEN = MIN_ADMIN_PASSWORD_LEN


def _existing_admin(db) -> str | None:
    """Id of any login-capable admin, or None.

    'system' is Orb Sage's audit identity, not a human account — its placeholder
    hash can never authenticate, so it must not count as "an admin exists".
    """
    row = db.execute(
        "SELECT id FROM users WHERE role = 'admin' AND id != ? "
        "AND COALESCE(is_active, 1) = 1 LIMIT 1",
        (SYSTEM_USER_ID,),
    ).fetchone()
    return row[0] if row else None


MAIN_FACILITY_CODE = "MAIN"


def resolve_facility(db, code: str | None, department: str | None) -> tuple[int | None, str | None]:
    """(facility id for a home row, or None for none; a refusal in words, or
    None). MAIN (facility 1, the default) needs no row. Any other facility
    needs ORB_MULTI_SITE on, must exist and be open, and is refused for an
    account with a department (it works at its department's facility)."""
    import config

    wanted = (code or MAIN_FACILITY_CODE).strip().upper()
    if wanted == MAIN_FACILITY_CODE:
        return None, None
    if department:
        return None, (
            "A home facility is only for an admin without a department. "
            "Leave --department out, or leave --facility at MAIN."
        )
    if not config.feature_enabled("multi_site"):
        return None, (
            "Multi-site is switched off (ORB_MULTI_SITE), so the first admin "
            "works at facility MAIN."
        )
    has_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facilities'"
    ).fetchone()
    row = (
        db.execute(
            "SELECT id, is_active FROM facilities WHERE code = ? COLLATE NOCASE", (wanted,)
        ).fetchone()
        if has_table
        else None
    )
    if row is None:
        return None, f"There is no facility with the code {wanted}."
    if not row["is_active"]:
        return None, f"Facility {wanted} is closed."
    return int(row["id"]), None


def _write_home_row(db, user_id: str, facility_id: int, code: str) -> None:
    """The home row and its security audit row, inside the caller's
    transaction. Fail-closed: a failed audit write raises AuditWriteError and
    the account is rolled back with it."""
    import getpass as _getpass
    import uuid

    from services.audit_service import log_audit_event

    reason = "Home facility chosen when the first admin was created at the console"
    cur = db.execute(
        "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, reason) "
        "VALUES (?, ?, 'home', ?, CURRENT_TIMESTAMP, ?)",
        (user_id, facility_id, "console", reason),
    )
    try:
        operator = _getpass.getuser()
    except Exception:
        operator = None
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type="user_facility_assigned",
        outcome="success",
        db=db,
        summary_message=f"Home facility {code} set for user {user_id} at the console",
        actor_user_id=None,
        actor_role="console",
        source_type="console",
        target_type="user",
        target_id=user_id,
        metadata={
            "facility_id": facility_id,
            "user_facility_id": cur.lastrowid,
            "reason": reason,
            "operator": operator,
            "script": "create_admin.py",
        },
    )


def enroll_mfa(user_id: str, *, account_label: str | None = None) -> bool:
    """Console TOTP enrolment for `user_id`. Returns True once confirmed.

    Mirrors POST /api/auth/mfa/enroll/begin + /confirm exactly — secret first,
    totp_enabled only after a live code verifies — so an operator who mis-scans
    the QR cannot lock the appliance's only admin out of it. The secret is
    printed as text as well as an otpauth:// URI because a headless appliance
    over SSH has no screen to show a QR code on.
    """
    from config import MFA_ISSUER

    from services import mfa_service

    if not sys.stdin.isatty():
        print(
            "[warn] --enroll-mfa needs an interactive terminal; skipping enrolment.",
            file=sys.stderr,
        )
        return False

    with db_session() as db:
        row = db.execute(
            "SELECT id, name, registration_number, COALESCE(totp_enabled, 0) AS totp_enabled "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        print(f"[fail] No such user: {user_id}", file=sys.stderr)
        return False
    if row["totp_enabled"]:
        print(f"[ ok ] {user_id} already has a TOTP factor — nothing to enrol.")
        return True

    secret = mfa_service.generate_secret()
    label = account_label or row["registration_number"] or row["name"] or user_id
    uri = mfa_service.provisioning_uri(secret, str(label), MFA_ISSUER)

    with db_session() as db:
        db.execute(
            "UPDATE users SET totp_secret = ?, totp_enabled = 0 WHERE id = ?",
            (secret, user_id),
        )

    print("\n  ── MFA enrolment ─────────────────────────────────────────────")
    print(f"  Account : {user_id} ({label})")
    print(f"  Secret  : {secret}")
    print(f"  URI     : {uri}")
    print("\n  Add it to an authenticator app (Google Authenticator, 1Password,")
    print("  Authy...). Either scan the URI as a QR code or type the secret in")
    print("  manually. Then enter the 6-digit code it shows.\n")

    for attempt in range(3):
        code = input("  Code from your authenticator (blank to skip): ").strip()
        if not code:
            print(
                "[warn] MFA enrolment skipped. The account exists but has ONE factor.",
                file=sys.stderr,
            )
            print(
                f"       Re-run: python server/scripts/create_admin.py --id {user_id} --enroll-mfa",
                file=sys.stderr,
            )
            return False
        step = mfa_service.verify_code_step(secret, code)
        if step is not None:
            with db_session() as db:
                # Record the consumed step alongside enabling, exactly as the
                # HTTP path's consume_totp() does — otherwise this same code
                # could be replayed at the login prompt seconds later.
                db.execute(
                    "UPDATE users SET totp_enabled = 1, totp_last_step = ? WHERE id = ?",
                    (step, user_id),
                )
            print(f"[ ok ] MFA enabled for {user_id}. Keep the secret somewhere safe —")
            print("       if the authenticator is lost, another admin must run")
            print("       POST /api/auth/mfa/reset/{user_id} to clear the factor.")
            return True
        print(
            f"  Code did not verify ({2 - attempt} attempt(s) left). "
            "Check the device clock is correct."
        )

    print("[fail] MFA enrolment not confirmed; totp_enabled left OFF.", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the first Orb admin account.")
    parser.add_argument(
        "--id", default=os.getenv("ORB_ADMIN_ID"), help="Staff ID used to sign in (e.g. ADM001)"
    )
    parser.add_argument(
        "--name", default=os.getenv("ORB_ADMIN_NAME"), help="Full name shown in the UI"
    )
    parser.add_argument(
        "--department", default=os.getenv("ORB_ADMIN_DEPARTMENT"), help="Optional department"
    )
    parser.add_argument(
        "--facility",
        default=os.getenv("ORB_ADMIN_FACILITY") or MAIN_FACILITY_CODE,
        help="Code of the facility the admin works at (default MAIN, facility 1).",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; take values from flags/env or exit 2 with instructions.",
    )
    parser.add_argument(
        "--enroll-mfa",
        action="store_true",
        help="Enrol a TOTP second factor at the console after creating (or for --id, on an existing account).",
    )
    args = parser.parse_args()

    # Safe to call repeatedly — install.sh has already run the migration runner,
    # but this keeps the script usable standalone.
    init_db()

    with db_session() as db:
        existing = _existing_admin(db)
    if existing:
        # --enroll-mfa on an existing box is a legitimate, separate job: an admin
        # created before MFA was wired, or a CIO account added later.
        if args.enroll_mfa:
            target = (args.id or "").strip() or existing
            return 0 if enroll_mfa(target) else 1
        print(f"[ ok ] Admin account already exists ({existing}) — nothing to do.")
        return 0

    interactive = not args.non_interactive and sys.stdin.isatty()

    user_id = (args.id or "").strip()
    name = (args.name or "").strip()
    password = os.getenv("ORB_ADMIN_PASSWORD", "")

    if interactive:
        if not user_id:
            user_id = input("Admin staff ID (used to sign in, e.g. ADM001): ").strip()
        if not name:
            name = input("Full name: ").strip()
        if not password:
            password = getpass.getpass(f"Password (min {MIN_PASSWORD_LEN} chars): ")
            if password != getpass.getpass("Confirm password: "):
                print("[fail] Passwords did not match.", file=sys.stderr)
                return 1

    if not user_id or not name or not password:
        # Exit 2, NOT 0. There is no admin on this appliance and this run did not
        # create one; a zero exit here is how an install "succeeds" into a box
        # nobody can log in to. install.sh branches on this.
        print(
            "NO ADMIN ACCOUNT EXISTS and no credentials were supplied.\n"
            "This appliance currently has NO WAY FOR ANYONE TO SIGN IN.\n"
            "Create one before the first sign-in:\n"
            "    python server/scripts/create_admin.py --enroll-mfa\n"
            "or non-interactively:\n"
            "    ORB_ADMIN_ID=ADM001 ORB_ADMIN_NAME='Jane Doe' ORB_ADMIN_PASSWORD='<strong password>' \\\n"
            "        python server/scripts/create_admin.py --non-interactive",
            file=sys.stderr,
        )
        return 2

    policy_error = password_policy_error(password, user_id=user_id, name=name)
    if policy_error:
        print(f"[fail] {policy_error}", file=sys.stderr)
        print(
            "       This account administers an appliance holding patient records on a",
            file=sys.stderr,
        )
        print("       hospital LAN. Pick a password worth that.", file=sys.stderr)
        return 1

    department = (args.department or "").strip() or None
    with db_session() as db:
        facility_id, facility_problem = resolve_facility(db, args.facility, department)
    if facility_problem:
        print(f"[fail] {facility_problem}", file=sys.stderr)
        return 1
    password_hash = hash_password(password)

    from services.audit_service import AuditWriteError

    try:
        with db_session() as db:
            # Re-check inside the transaction so two concurrent runs cannot both
            # create a first admin.
            if _existing_admin(db):
                print("[ ok ] Admin account already exists — nothing to do.")
                return 0
            db.execute(
                """INSERT INTO users (id, name, role, department, password_hash, is_active, token_version)
                   VALUES (?, ?, 'admin', ?, ?, 1, 0)""",
                (user_id, name, department, password_hash),
            )
            if facility_id is not None:
                _write_home_row(db, user_id, facility_id, args.facility.strip().upper())
    except DBIntegrityError:
        print(f"[fail] A user with id '{user_id}' already exists.", file=sys.stderr)
        return 1
    except AuditWriteError as e:
        print(
            f"[fail] The audit trail could not record the home facility, so nothing was saved: {e}",
            file=sys.stderr,
        )
        return 1
    if facility_id is not None:
        print(f"[ ok ] Home facility {args.facility.strip().upper()} set for '{user_id}'.")

    print(
        f"[ ok ] Admin account '{user_id}' created. Sign in with that ID and the password you set."
    )

    if args.enroll_mfa:
        if not enroll_mfa(user_id, account_label=name):
            # The account exists and is usable — refusing it now would leave the
            # appliance with no admin, which is the worse failure. Report and
            # exit non-zero so a script notices the factor is missing.
            return 1
    else:
        print("       No second factor is enrolled. Add one before go-live:")
        print(f"         python server/scripts/create_admin.py --id {user_id} --enroll-mfa")

    return 0


if __name__ == "__main__":
    sys.exit(main())
