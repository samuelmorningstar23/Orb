#!/usr/bin/env python3
"""Re-seal the audit-log hash chain (DEMO / maintenance utility).

The audit hash-chain can accumulate broken links on a DB that was written by the
pre-fix version of log_audit_event (a read-then-write race let concurrent writers
chain off the same prev_hash). Those breaks are NOT tampering — they are stale
links on synthetic/dev data — but they make the Trust Center show a red
"chain broken" badge in a demo.

This tool recomputes prev_hash + row_hash for every row in id order so the chain
is internally consistent again. It is EXPLICITLY a demo/dev convenience:
  * it must NEVER be run against a production audit log that holds real events —
    re-sealing there would erase genuine tamper evidence. It refuses unless
    --force is passed AND ORB_DEPLOYMENT_PROFILE=dev (or --i-understand).
  * the append-only triggers are dropped and recreated around the rewrite.

Usage:
    ORB_DB_PATH=server/orb.db ORB_DEPLOYMENT_PROFILE=dev \
        python server/scripts/reseal_audit_chain.py --force

Run it with the same ORB_DB_ENCRYPTION_KEY as the server: the DB is opened through
database._open_raw_connection(), so an encrypted-at-rest orb.db is unreadable
without the key (SQLCipher reports "file is not a database").
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="actually rewrite (else dry-run)")
    ap.add_argument("--i-understand", action="store_true",
                    help="bypass the dev-profile guard (you are re-sealing real audit data)")
    args = ap.parse_args()

    from config import DEPLOYMENT_PROFILE
    from database import _open_raw_connection, _row_factory_for
    from services.audit_service import compute_audit_row_hash
    from services.flight_recorder_service import _AUDIT_SELECT

    if args.force and DEPLOYMENT_PROFILE != "dev" and not args.i_understand:
        print("REFUSING: not in dev profile. Re-sealing a production audit log erases "
              "tamper evidence. Pass --i-understand only if you truly intend this.")
        return 2

    # Codec-aware open: with ORB_DB_ENCRYPTION_KEY set, orb.db is a SQLCipher file
    # that the stdlib sqlite3 driver cannot read at all, and the seeder runs this
    # script (reset_db_and_seed.py) — so it must go through the app's own opener,
    # which applies PRAGMA key before any other statement. The row class must come
    # from the same driver: sqlite3.Row rejects a SQLCipher cursor with TypeError,
    # and every `dict(r)` below depends on it.
    conn = _open_raw_connection()
    conn.row_factory = _row_factory_for(conn)
    rows = conn.execute(f"SELECT id, {_AUDIT_SELECT} FROM audit_logs ORDER BY id ASC").fetchall()

    # Dry-run: report how many links are currently broken.
    last = None
    broken = 0
    for r in rows:
        d = dict(r)
        exp = compute_audit_row_hash(d, d.get("prev_hash"))
        if d["row_hash"] is None:
            last = None
            continue
        if exp != d["row_hash"] or (last is not None and d.get("prev_hash") != last):
            broken += 1
        last = d["row_hash"]
    print(f"Rows: {len(rows)} | currently broken links: {broken}")

    if not args.force:
        print("Dry run (pass --force to re-seal).")
        return 0

    # Save + drop the append-only triggers, rewrite, then restore them.
    trg = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_audit_logs_%'"
    ).fetchall()
    for t in trg:
        conn.execute(f"DROP TRIGGER IF EXISTS {t['name']}")

    prev_hash = None
    resealed = 0
    for r in rows:
        d = dict(r)
        row_hash = compute_audit_row_hash(d, prev_hash)
        conn.execute("UPDATE audit_logs SET prev_hash = ?, row_hash = ? WHERE id = ?",
                     (prev_hash, row_hash, d["id"]))
        prev_hash = row_hash
        resealed += 1

    for t in trg:
        if t["sql"]:
            conn.execute(t["sql"])
    conn.commit()
    conn.close()
    print(f"Re-sealed {resealed} rows. Chain is now internally consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
