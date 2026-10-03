#!/usr/bin/env python3
"""Migrate a plaintext orb.db to SQLCipher (encryption at rest) — or verify one.

Orb is the system of record for PHI, so this migration is written to be safe
rather than quick: it refuses to run against a live server, keeps the plaintext
original until the encrypted copy has been verified, and verifies the things
that actually matter clinically — row counts per table, the tamper-evident
audit hash chain, vector search, and full-text search.

    # look, change nothing
    server/.venv/bin/python scripts/encrypt_db.py --check

    # migrate (server must be stopped)
    server/.venv/bin/python scripts/encrypt_db.py --migrate

    # migrate and keep the plaintext copy on disk (default: keep)
    server/.venv/bin/python scripts/encrypt_db.py --migrate --no-keep-plaintext

Key material comes from ORB_DB_ENCRYPTION_KEY and is derived to a 32-byte raw
key by database._sqlcipher_key_hex, so this script and the app agree byte-for-byte.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SQLITE_PLAINTEXT_MAGIC = b"SQLite format 3\x00"

# Tables whose row counts must match exactly after migration. Chosen because
# each one losing rows silently would be a clinical or compliance failure.
#
# These fourteen are the BASELINE set, from before the migration framework. Note
# for anyone reading a cross-reference to this list elsewhere: scripts/backup.sh,
# restore-drill.sh and standby-restore.sh each used to carry a comment saying
# their 4-table row-count floor was "kept in agreement with" this list. It never
# was — four is not fourteen, and the two lists answer different questions
# (theirs: "does this backup hold clinical data at all"; this one: "did the
# encryption copy lose any rows"). Those three now share
# scripts/lib/verify_restore.py and the stale claim is gone.
VERIFY_TABLES = [
    "patients", "vitals", "risk_scores", "medications", "messages", "channels",
    "users", "audit_logs", "scribe_notes", "clinical_documents", "orders",
    "admissions", "sage_insights", "clinical_knowledge",
]


def _with_migrated_tables(tables: list[str]) -> list[str]:
    """Add every table the migrations create, from the restore verifier's manifest.

    LANE PARITY (2026-08-14). The row-count comparison here is the only thing
    standing between a botched `sqlcipher_export` and an encrypted database that
    is quietly missing rows — and it was covering fourteen tables out of the
    ~99 this schema now has. A site running the encryption migration today on a
    v26 plaintext database would have had billing, payments, claims, procurement,
    the incident register and the rest verified by nothing at all.

    Absent tables are not an error: `_counts` returns None for a table that does
    not exist, and None == None compares equal, so a genuinely pre-spine
    plaintext database still migrates cleanly. Falls back to the baseline list if
    the manifest cannot be loaded, because failing to encrypt is worse than
    verifying less.
    """
    try:
        import importlib.util

        manifest_path = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "verify_restore.py"
        spec = importlib.util.spec_from_file_location("orb_restore_manifest", manifest_path)
        if spec is None or spec.loader is None:
            return tables
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        known = set(tables)
        return tables + [t for t in module.ALL_MIGRATED_TABLES if t not in known]
    except Exception as exc:  # noqa: BLE001 - never block the migration on this
        print(f"  (note) could not load the restore manifest ({exc}); "
              f"verifying the {len(tables)} baseline tables only")
        return tables


VERIFY_TABLES = _with_migrated_tables(VERIFY_TABLES)


def _is_plaintext(path: Path) -> bool:
    """True if the file starts with SQLite's plaintext magic header."""
    if not path.exists() or path.stat().st_size < 16:
        return False
    with path.open("rb") as fh:
        return fh.read(16) == SQLITE_PLAINTEXT_MAGIC


def _server_running(port: int = 8000) -> bool:
    """Refuse to migrate underneath a running server: it holds WAL state and
    open handles, and swapping the file beneath it corrupts both copies."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _driver():
    try:
        from pysqlcipher3 import dbapi2 as drv  # type: ignore
        return drv, "pysqlcipher3"
    except Exception:
        try:
            import sqlcipher3 as drv  # type: ignore
            return drv, "sqlcipher3"
        except Exception:
            return None, None


def _counts(conn, tables) -> dict:
    out = {}
    for t in tables:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            out[t] = None  # table absent in this build — not an error
    return out


def _open_encrypted(path: Path, key_hex: str):
    drv, _ = _driver()
    conn = drv.connect(str(path), timeout=30)
    conn.execute(f"PRAGMA key = \"x'{key_hex}'\"")
    return conn


def _verify(conn, expected: dict) -> list[str]:
    """Return a list of problems; empty means the encrypted copy is sound."""
    problems: list[str] = []

    got = _counts(conn, list(expected))
    for t, want in expected.items():
        if want != got.get(t):
            problems.append(f"row count mismatch in {t}: plaintext={want} encrypted={got.get(t)}")

    # Vector + full-text search are loadable-extension / virtual-table features;
    # they are the parts most likely to survive a naive copy in name only.
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        n = conn.execute("SELECT COUNT(*) FROM clinical_knowledge_vec_rowids").fetchone()[0]
        print(f"  vector index rows: {n}")
    except Exception as e:
        problems.append(f"vector index unusable: {e}")

    try:
        conn.execute("SELECT rowid FROM clinical_knowledge_fts WHERE clinical_knowledge_fts MATCH 'sepsis' LIMIT 1").fetchall()
        print("  FTS5 query: OK")
    except Exception as e:
        problems.append(f"FTS5 unusable: {e}")

    # The audit hash chain is the compliance artefact; a broken chain after
    # migration would be indistinguishable from tampering. Verify LINKAGE
    # (each row's prev_hash == the previous row's row_hash), not just that the
    # rows are readable.
    try:
        rows = conn.execute(
            "SELECT id, prev_hash, row_hash FROM audit_logs ORDER BY id"
        ).fetchall()
        breaks = 0
        prev = None
        for r in rows:
            rid, prev_hash, row_hash = r[0], r[1], r[2]
            if prev is not None and prev_hash != prev:
                breaks += 1
            prev = row_hash
        if breaks:
            problems.append(f"audit hash chain has {breaks} broken link(s) after migration")
        else:
            print(f"  audit chain: {len(rows)} rows, linkage intact")
    except Exception as e:
        problems.append(f"audit_logs unreadable: {e}")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--migrate", action="store_true", help="perform the migration")
    ap.add_argument("--check", action="store_true", help="report state only, change nothing")
    ap.add_argument("--no-keep-plaintext", action="store_true",
                    help="delete the plaintext backup after successful verification")
    args = ap.parse_args()
    if not (args.migrate or args.check):
        ap.error("pass --check or --migrate")

    from config import DB_ENCRYPTION_KEY, DB_PATH
    from database import _sqlcipher_key_hex

    db = Path(DB_PATH)
    drv, drv_name = _driver()

    print(f"database:        {db}")
    print(f"exists:          {db.exists()} ({db.stat().st_size/1048576:.1f} MB)" if db.exists() else "exists:          False")
    print(f"on-disk state:   {'PLAINTEXT' if _is_plaintext(db) else 'encrypted or unknown'}")
    print(f"sqlcipher driver:{' ' + drv_name if drv_name else ' NOT INSTALLED'}")
    print(f"key configured:  {bool(DB_ENCRYPTION_KEY)}")

    if args.check:
        if not DB_ENCRYPTION_KEY:
            print("\n→ ORB_DB_ENCRYPTION_KEY is unset; the app will run PLAINTEXT.")
        elif drv is None:
            print("\n→ Key set but no driver: the app falls back to PLAINTEXT with a warning.")
            print("  Install with: uv pip install --python server/.venv/bin/python sqlcipher3-wheels")
        elif _is_plaintext(db):
            print("\n→ Key + driver present but the FILE IS STILL PLAINTEXT.")
            print("  The app cannot open it with SQLCipher. Run: --migrate")
        else:
            print("\n→ Encrypted at rest and the driver is present.")
        return 0

    # ── migrate ──
    if not DB_ENCRYPTION_KEY:
        print("\nREFUSING: ORB_DB_ENCRYPTION_KEY is not set.", file=sys.stderr)
        return 2
    if drv is None:
        print("\nREFUSING: no SQLCipher driver installed.", file=sys.stderr)
        return 2
    if not db.exists():
        print("\nNothing to migrate — no database file. A fresh DB is created encrypted automatically.")
        return 0
    if not _is_plaintext(db):
        print("\nAlready encrypted (or not a plaintext SQLite file). Nothing to do.")
        return 0
    if _server_running():
        print("\nREFUSING: something is listening on :8000 — stop the server first.\n"
              "  Migrating under a live server corrupts both copies.", file=sys.stderr)
        return 2

    key_hex = _sqlcipher_key_hex(DB_ENCRYPTION_KEY)
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = db.with_suffix(db.suffix + f".plaintext-{ts}")
    encrypted = db.with_suffix(db.suffix + ".encrypted-tmp")
    for stale in (encrypted,):
        if stale.exists():
            stale.unlink()

    print(f"\n1/5 backing up plaintext → {backup.name}")
    # Checkpoint WAL into the main file first so the backup is self-contained.
    import sqlite3
    src = sqlite3.connect(str(db), timeout=30)
    src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    expected = _counts(src, VERIFY_TABLES)
    src.close()
    shutil.copy2(db, backup)

    print("2/5 exporting into an encrypted database (sqlcipher_export)")
    # A SQLCipher connection with NO key reads plaintext; ATTACH-ing a keyed DB
    # and calling sqlcipher_export copies schema + data + virtual tables.
    conn = drv.connect(str(db), timeout=60)
    try:
        conn.execute(f"ATTACH DATABASE '{encrypted}' AS enc KEY \"x'{key_hex}'\"")
        conn.execute("SELECT sqlcipher_export('enc')")
        conn.execute("DETACH DATABASE enc")
    except Exception as e:
        print(f"\nEXPORT FAILED: {e}\nOriginal untouched. Backup at {backup}", file=sys.stderr)
        conn.close()
        return 3
    conn.close()

    print("3/5 verifying the encrypted copy")
    chk = _open_encrypted(encrypted, key_hex)
    problems = _verify(chk, expected)
    chk.close()
    if problems:
        print("\nVERIFICATION FAILED — original left in place:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        encrypted.unlink(missing_ok=True)
        return 4
    print("  all checks passed")

    print("4/5 swapping encrypted file into place")
    # Old WAL/SHM belong to the plaintext file and must not be inherited.
    for sidecar in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        sidecar.unlink(missing_ok=True)
    os.replace(encrypted, db)

    print("5/5 confirming the app's own connection path opens it")
    try:
        from database import _build_connection
        c = _build_connection()
        n = c.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
        c.close()
        print(f"  _build_connection() opened the encrypted DB, patients={n}")
    except Exception as e:
        print(f"\nPOST-SWAP OPEN FAILED: {e}\n  Restore with: cp {backup} {db}", file=sys.stderr)
        return 5

    if args.no_keep_plaintext:
        backup.unlink()
        print(f"\nplaintext backup deleted ({backup.name})")
    else:
        print(f"\nplaintext backup KEPT at {backup}")
        print("  → It contains unencrypted PHI. Delete it once you are satisfied:")
        print(f"     rm {backup}")

    print("\nDone. orb.db is now encrypted at rest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
