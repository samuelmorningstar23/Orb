#!/usr/bin/env python3
"""Orb — pilot staff access review.

WHAT THIS IS FOR
────────────────
Before a ward goes live, somebody has to be able to answer, on one page: who can
sign in to this appliance, what does each of them reach, who is missing a second
factor, and is anything left over from a demo. That question is asked by the
hospital's CIO, and "let me query the database" is not an answer you want to give
in that meeting.

It reads only the users table and the central permission map — never a patient
row, never a chart. The output carries staff identifiers (that is the point) and
no PHI, so it can be pasted into an email to the hospital.

Checks it raises:
  * an admin (holds admin.overview) with no TOTP factor;
  * an account still holding one of Orb's shipped demo passwords (needs --deep;
    this is the same bcrypt sweep the server runs at boot, and it is slow);
  * a legacy non-bcrypt password hash (pre-migration accounts);
  * deactivated accounts that are still listed as staff;
  * ZERO login-capable admins — the bricked-appliance state.

Usage:
    server/.venv/bin/python server/scripts/access_review.py
    server/.venv/bin/python server/scripts/access_review.py --deep      # + credential sweep
    server/.venv/bin/python server/scripts/access_review.py --json
    server/.venv/bin/python server/scripts/access_review.py --strict    # non-zero on findings

Exit codes:
    0  reviewed; no findings (or findings present without --strict)
    1  --strict and at least one finding
    2  no login-capable administrator exists (always non-zero, --strict or not)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database import db_session  # noqa: E402
from services.bootstrap_service import (  # noqa: E402
    admin_inventory,
    review_accounts,
    scan_default_credentials,
)


def _fmt_table(accounts: list[dict]) -> str:
    headers = ("ID", "NAME", "ROLE", "ACTIVE", "MFA", "PERMS", "FINDINGS")
    rows = [
        (
            a["id"],
            (a["name"] or "")[:28],
            a["role"],
            "yes" if a["is_active"] else "NO",
            "yes" if a["mfa_enrolled"] else ("MISSING" if a["privileged"] else "no"),
            str(a["permission_count"]),
            "; ".join(a["findings"]) or "-",
        )
        for a in accounts
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review Orb staff accounts, roles and MFA posture.")
    parser.add_argument("--deep", action="store_true",
                        help="Also bcrypt-sweep every staff account for Orb's shipped default passwords (slow).")
    parser.add_argument("--budget", type=float, default=300.0,
                        help="Wall-clock seconds allowed for --deep (default 300).")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any finding is present.")
    args = parser.parse_args(argv)

    with db_session() as db:
        review = review_accounts(db)
        inv = admin_inventory(db)
        sweep = None
        if args.deep:
            sweep = scan_default_credentials(db, budget_s=args.budget)

    default_ids = [f["id"] for f in sweep["findings"]] if sweep else []
    for acc in review["accounts"]:
        if acc["id"] in default_ids:
            acc["findings"].append("SHIPPED DEFAULT PASSWORD")

    findings_total = sum(len(a["findings"]) for a in review["accounts"])
    payload = {
        **review,
        "admins_active": inv["admins_active"],
        "admins_with_mfa": inv["admins_with_mfa"],
        "default_credential_accounts": default_ids,
        "deep_sweep": sweep,
        "findings_total": findings_total,
    }

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print("Orb — pilot staff access review")
        print(f"generated {review['generated_utc']}\n")
        print(_fmt_table(review["accounts"]) if review["accounts"] else "  (no staff accounts)")
        print()
        print(f"  staff accounts            : {review['staff_accounts']}")
        print(f"  login-capable admins      : {inv['admins_active']}"
              f"{'   ← NOBODY CAN ADMINISTER THIS APPLIANCE' if inv['admins_active'] == 0 else ''}")
        print(f"  admins with a TOTP factor : {inv['admins_with_mfa']}")
        if review["privileged_without_mfa"]:
            print(f"  admins WITHOUT MFA        : {', '.join(review['privileged_without_mfa'])}")
        if sweep:
            print(f"  credential sweep          : {sweep['checked']} bcrypt checks in "
                  f"{sweep['elapsed_s']}s"
                  f"{' (BUDGET EXHAUSTED — incomplete)' if sweep['budget_exhausted'] else ''}")
            print(f"  shipped default passwords : {', '.join(default_ids) or 'none'}")
        else:
            print("  credential sweep          : not run (pass --deep)")
        print(f"  total findings            : {findings_total}")

    if inv["admins_active"] == 0:
        return 2
    return 1 if (args.strict and findings_total) else 0


if __name__ == "__main__":
    sys.exit(main())
