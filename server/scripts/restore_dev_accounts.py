"""Re-activate the seeded DEV accounts that bootstrap enforcement deactivated.

WHY THIS EXISTS (2026-08-11). A benchmark run was started with
ORB_DEPLOYMENT_PROFILE=prod against the *development* database. The prod profile
enables bootstrap credential enforcement, whose job is exactly this: any account
still holding a shipped default password ("password") gets DEACTIVATED. Against
real PHI that is correct and load-bearing. Against the dev seed data it locked
all 12 demo logins — including DOC001 and the only admin — out of the app.

WHAT IT DOES. Sets is_active = 1 for a FIXED list of seeded demo staff ids, and
nothing else. It does not touch passwords, roles, permissions, MFA, patients, or
any other account. It is a no-op for any id already active.

WHAT IT DOES NOT DO. It does not make those accounts safe. They still hold the
default password, so a prod-profile boot will deactivate them again — correctly.
This script is for restoring a DEVELOPMENT box, and it refuses to run when the
prod profile is active.

THE REAL FIX for anything holding PHI is scripts/create_admin.py, which mints an
admin with a policy-checked password (12+ chars, no demo defaults) and optional
MFA enrolment.

Usage (from server/):
    set -a; source ../.env; set +a
    .venv/bin/python scripts/restore_dev_accounts.py --check     # report only
    .venv/bin/python scripts/restore_dev_accounts.py             # re-activate
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The seeded demo staff. Deliberately a literal list: this script must never be
# able to re-activate an account it was not written to re-activate.
SEEDED_DEMO_ACCOUNTS = (
    "ADM001", "DOC001", "DOC002", "DOC004", "DOC005", "DOC006", "DOC007",
    "NUR001", "PHA001", "anesthetist_1", "anesthetist_2", "dr_rathore",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="report activation state and exit without writing")
    args = ap.parse_args()

    from config import DEPLOYMENT_PROFILE
    if DEPLOYMENT_PROFILE == "prod" and not args.check:
        print("REFUSING: ORB_DEPLOYMENT_PROFILE=prod. These accounts hold "
              "default passwords; re-activating them on a prod-profile install "
              "would undo a credential control. Use scripts/create_admin.py.")
        return 2

    from database import _open_raw_connection
    placeholders = ",".join("?" * len(SEEDED_DEMO_ACCOUNTS))
    db = _open_raw_connection()
    try:
        rows = db.execute(
            f"SELECT id, is_active FROM users WHERE id IN ({placeholders})",
            SEEDED_DEMO_ACCOUNTS,
        ).fetchall()
        inactive = [r[0] for r in rows if not r[1]]

        if args.check:
            for uid, active in sorted(rows):
                print(f"  {uid:<16} {'active' if active else 'INACTIVE'}")
            print(f"\n{len(inactive)} inactive of {len(rows)} found "
                  f"({len(SEEDED_DEMO_ACCOUNTS)} known).")
            return 0

        if not inactive:
            print("Nothing to do — every seeded demo account is already active.")
            return 0

        cur = db.execute(
            f"UPDATE users SET is_active = 1 "
            f"WHERE id IN ({placeholders}) AND COALESCE(is_active, 0) = 0",
            SEEDED_DEMO_ACCOUNTS,
        )
        db.commit()
        print(f"Re-activated {cur.rowcount}: {', '.join(sorted(inactive))}")
        print("\nThese accounts still hold the default password. A prod-profile "
              "boot will deactivate them again, by design.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
