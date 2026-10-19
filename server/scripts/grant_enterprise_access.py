#!/usr/bin/env python3
"""Give, revoke or list group-wide (enterprise) access, at the appliance's console.

WHY THIS EXISTS (multisite-6, W9-H, 10 October 2026)
Samuel ruled on 3 October 2026 that one Orb appliance can serve several
facilities of one hospital group, and that once more than one facility is on
record each account sees only its own facility's patients (multi-site M1, M2).
Group-wide access is a named grant with a reason, held only by an
administrator (ruling MS-R6 (a)), and only an administrator who already holds
one may give or revoke one in User Management. That leaves the first grant:
nobody holds one yet. It is made here, by someone at the appliance's own
keyboard, and it is audited like the screen's grants.

WHAT IT CHECKS
  * Granting needs ORB_MULTI_SITE on (the switch gates growth), an interactive
    terminal, and the account id typed back to confirm. The account must exist,
    be active, be an administrator, and not be one of Orb's system accounts. A
    reason of at least 3 characters is required. One live grant per account.
  * Revoking works whatever the switch says (a revoke only narrows) and asks
    for the same typed confirmation, or --yes.
  * Every grant and revoke is one transaction with its security audit row
    (fail-closed: if the audit row cannot be written, nothing is saved).

The running server reads grants through a cache that refreshes every 30
seconds, so a grant or revoke made here takes effect there within 30 seconds.
A revoke made in User Management takes effect on the next request.

Usage:
    python server/scripts/grant_enterprise_access.py --list
    python server/scripts/grant_enterprise_access.py --grant ADM001 --reason "Group CIO"
    python server/scripts/grant_enterprise_access.py --revoke ADM001 --reason "Left the group"

Run it with the same ORB_DB_PATH and ORB_DB_ENCRYPTION_KEY as the server.

Exit codes:
    0  done, or listed
    1  refused (the reason is printed), or the audit trail was not available
    2  granting or revoking without a terminal to confirm at
"""
from __future__ import annotations

import argparse
import getpass
import os
import platform
import sys
import uuid

# Ensure server root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database import SERVICE_ACCOUNT_IDS, db_session  # noqa: E402
from db_backend import DBIntegrityError  # noqa: E402

MIN_REASON_LEN = 3
CACHE_NOTE = "The running server applies this within 30 seconds."


class GrantRefused(RuntimeError):
    """A grant or revoke this script will not make. The message is for the
    operator."""


def _operator() -> dict:
    """Who is at the console, for the audit row. Never a credential."""
    try:
        user = getpass.getuser()
    except Exception:
        user = None
    try:
        host = platform.node() or None
    except Exception:
        host = None
    return {"os_user": user, "host": host}


def _has_table(db, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                      (name,)).fetchone() is not None


def _require_tables(db) -> None:
    if not _has_table(db, "enterprise_access_grants"):
        raise GrantRefused("This database has no group-wide access table yet. Start Orb "
                           "once so it is brought up to date (migration 70), then retry.")


def _reason(raw: str | None) -> str:
    reason = (raw or "").strip()
    if len(reason) < MIN_REASON_LEN:
        raise GrantRefused(f"Give a reason of at least {MIN_REASON_LEN} characters (--reason).")
    return reason


def _audit(db, action_type: str, summary: str, user_id: str, metadata: dict) -> None:
    from services.audit_service import log_audit_event
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action_type,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=None,
        actor_role="console",
        source_type="console",
        target_type="user",
        target_id=user_id,
        metadata={**metadata, "operator": _operator(),
                  "script": "grant_enterprise_access.py"},
    )


def list_grants() -> list[dict]:
    """Every grant, live first, then the revoked history."""
    with db_session() as db:
        _require_tables(db)
        rows = db.execute(
            "SELECT g.id, g.user_id, u.name AS user_name, u.role AS user_role, "
            "g.granted_by, g.granted_at, g.reason, g.revoked_at, g.revoked_by, "
            "g.revoke_reason FROM enterprise_access_grants g "
            "LEFT JOIN users u ON u.id = g.user_id "
            "ORDER BY (g.revoked_at IS NOT NULL), g.granted_at DESC, g.id DESC").fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def grant(user_id: str, reason: str | None) -> dict:
    """Give `user_id` group-wide access. Raises GrantRefused, or
    AuditWriteError when the audit row could not be written (nothing saved)."""
    import config
    from services import facility_service

    user_id = (user_id or "").strip()
    reason = _reason(reason)
    if not config.feature_enabled("multi_site"):
        raise GrantRefused("Multi-site is switched off (ORB_MULTI_SITE), so no group-wide "
                           "access can be given.")
    uid = user_id.lower()
    if uid in SERVICE_ACCOUNT_IDS or uid.startswith("bot_"):
        raise GrantRefused(f"{user_id} is one of Orb's system accounts. It has no login "
                           "and cannot hold group-wide access.")
    try:
        with db_session(immediate=True) as db:
            _require_tables(db)
            row = db.execute(
                "SELECT id, role, COALESCE(is_active, 1) AS is_active FROM users WHERE id = ?",
                (user_id,)).fetchone()
            if row is None:
                raise GrantRefused(f"There is no account {user_id}.")
            if row["role"] not in facility_service.ENTERPRISE_GRANT_ROLES:
                raise GrantRefused(f"{user_id} is a {row['role']}. Group-wide access is only "
                                   "for administrators.")
            if not row["is_active"]:
                raise GrantRefused(f"{user_id} is switched off. Activate it first.")
            live = db.execute(
                "SELECT id FROM enterprise_access_grants WHERE user_id = ? "
                "AND revoked_at IS NULL", (user_id,)).fetchone()
            if live is not None:
                raise GrantRefused(f"{user_id} already has group-wide access (grant {live['id']}).")
            cur = db.execute(
                "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
                "VALUES (?, 'console', CURRENT_TIMESTAMP, ?)", (user_id, reason))
            grant_id = cur.lastrowid
            _audit(db, "enterprise_access_granted",
                   f"Group-wide access granted to user {user_id} at the console",
                   user_id, {"grant_id": grant_id, "reason": reason})
    except DBIntegrityError:
        raise GrantRefused(f"{user_id} already has group-wide access.")
    facility_service.invalidate_cache()
    return {"grant_id": grant_id, "user_id": user_id, "reason": reason}


def revoke(user_id: str, reason: str | None) -> dict:
    """Revoke `user_id`'s live grant. Works whatever ORB_MULTI_SITE says.
    Raises GrantRefused, or AuditWriteError (nothing saved)."""
    from services import facility_service

    user_id = (user_id or "").strip()
    reason = _reason(reason)
    with db_session(immediate=True) as db:
        _require_tables(db)
        live = db.execute(
            "SELECT id FROM enterprise_access_grants WHERE user_id = ? AND revoked_at IS NULL",
            (user_id,)).fetchone()
        if live is None:
            raise GrantRefused(f"{user_id} holds no group-wide access.")
        db.execute(
            "UPDATE enterprise_access_grants SET revoked_at = CURRENT_TIMESTAMP, "
            "revoked_by = 'console', revoke_reason = ? WHERE id = ? AND revoked_at IS NULL",
            (reason, live["id"]))
        _audit(db, "enterprise_access_revoked",
               f"Group-wide access revoked for user {user_id} at the console",
               user_id, {"grant_id": live["id"], "reason": reason})
    facility_service.invalidate_cache()
    return {"grant_id": live["id"], "user_id": user_id, "reason": reason}


def _confirm(user_id: str, verb: str) -> bool:
    print(f"  You are about to {verb} group-wide access for {user_id}.")
    print("  Group-wide access opens every facility's patients to this account.")
    typed = input(f"  Type the account id ({user_id}) to confirm: ").strip()
    return typed == user_id


def _print_grants(rows: list[dict]) -> None:
    if not rows:
        print("No group-wide access has been given.")
        return
    for r in rows:
        state = "live" if r["revoked_at"] is None else f"revoked {r['revoked_at']}"
        print(f"  {r['id']:>4}  {r['user_id']:<12} {(r['user_name'] or '')[:28]:<28} "
              f"{state:<28} granted {r['granted_at']} by {r['granted_by'] or '?'}: {r['reason']}")


def main(argv: list[str] | None = None) -> int:
    from services.audit_service import AuditWriteError

    parser = argparse.ArgumentParser(
        description="Give, revoke or list group-wide access at the appliance's console.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", help="List every grant.")
    action.add_argument("--grant", metavar="USER_ID", help="Give this administrator group-wide access.")
    action.add_argument("--revoke", metavar="USER_ID", help="Revoke this account's group-wide access.")
    parser.add_argument("--reason", help="Why (required for --grant and --revoke).")
    parser.add_argument("--yes", action="store_true",
                        help="Revoke without the typed confirmation (never for --grant).")
    args = parser.parse_args(argv)

    try:
        if args.list:
            _print_grants(list_grants())
            return 0
        target = (args.grant or args.revoke or "").strip()
        _reason(args.reason)
        if args.grant or not args.yes:
            if not sys.stdin.isatty():
                print("[fail] Run this at the appliance's console: it asks you to type the "
                      "account id to confirm.", file=sys.stderr)
                return 2
            if not _confirm(target, "give" if args.grant else "revoke"):
                print("[fail] The id did not match. Nothing was changed.", file=sys.stderr)
                return 1
        if args.grant:
            done = grant(target, args.reason)
            print(f"[ ok ] Group-wide access given to {target} (grant {done['grant_id']}). "
                  f"{CACHE_NOTE}")
        else:
            done = revoke(target, args.reason)
            print(f"[ ok ] Group-wide access revoked for {target} (grant {done['grant_id']}). "
                  f"{CACHE_NOTE}")
        return 0
    except GrantRefused as refused:
        print(f"[fail] {refused}", file=sys.stderr)
        return 1
    except AuditWriteError as e:
        print(f"[fail] The audit trail could not record this, so nothing was saved: {e}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
