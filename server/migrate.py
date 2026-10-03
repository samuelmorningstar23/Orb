#!/usr/bin/env python3
"""Orb Hospital OS — versioned schema migration runner.

This is a THIN, HONEST wrapper around the migration logic that already lives in
``database.init_db()``. It does NOT reimplement or duplicate any schema: the real
additive, idempotent migration blocks are owned by ``database.py`` and keyed off
the ``SCHEMA_VERSION`` constant plus the ``schema_version`` ledger table. This
runner simply gives operators a clear, scriptable CLI around them:

    python server/migrate.py            # apply migrations (calls init_db once)
    python server/migrate.py --status   # print current vs target version only
    python -m server.migrate            # same, if run as a module

Because ``init_db()`` is idempotent (every migration block is guarded by PRAGMA
introspection), running this repeatedly is safe. The target database is selected
exactly like the app selects it — via the ``ORB_DB_PATH`` environment variable
(falling back to ``server/orb.db``), resolved inside ``config.py``.

Exit codes:
    0  success (status printed, or migration applied and DB is at SCHEMA_VERSION)
    1  migration ran but the resulting version does not match SCHEMA_VERSION
    2  an unexpected error occurred while migrating
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from db_backend import DBError
import sys

# ── Path setup ──
# database.py does `from config import DB_PATH` (bare, non-package imports), so the
# server/ directory itself must be importable. This mirrors how tests/conftest.py
# prepends the server root to sys.path before importing `database`.
_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from config import DB_PATH  # noqa: E402
from database import SCHEMA_VERSION, init_db  # noqa: E402


def _current_db_version() -> int | None:
    """Return the highest applied schema version recorded in the DB, or None.

    None means the DB either does not exist yet or has never had init_db() run
    against it (no schema_version ledger table). We open our own short-lived
    connection so this works even against a brand-new/empty database file, and
    we never mutate anything here.
    """
    if not os.path.exists(str(DB_PATH)):
        return None
    try:
        # Route through database.get_db so an encrypted (SQLCipher) DB is opened
        # with its key applied — a plain sqlite3.connect would fail to read the
        # ledger and misreport an encrypted DB as uninitialized.
        from database import get_db
        conn = get_db()
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])
    except DBError:
        # schema_version table absent (pre-migration / fresh DB).
        return None
    finally:
        conn.close()


def _fmt(version: int | None) -> str:
    return "none (uninitialized)" if version is None else str(version)


def _print_status(current: int | None) -> None:
    print(f"  Database file : {DB_PATH}")
    print(f"  Current schema version : {_fmt(current)}")
    print(f"  Target schema version  : {SCHEMA_VERSION}")
    if current is None:
        print("  Status: database not yet initialized — run without --status to migrate.")
    elif current < SCHEMA_VERSION:
        print(f"  Status: BEHIND by {SCHEMA_VERSION - current} version(s) — migration needed.")
    elif current > SCHEMA_VERSION:
        print("  Status: AHEAD of this code's target — DB was migrated by a newer build.")
    else:
        print("  Status: up to date.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate.py",
        description=(
            "Versioned schema migration runner for Orb. Orchestrates the existing "
            "idempotent migrations in database.init_db(); it does not define schema."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current vs target schema version and exit WITHOUT migrating.",
    )
    args = parser.parse_args(argv)

    print("═══ Orb schema migration runner ═══")
    current = _current_db_version()

    if args.status:
        _print_status(current)
        return 0

    # ── Apply migrations by orchestrating the existing, idempotent init_db() ──
    print(f"  Database file : {DB_PATH}")
    print(f"  Current schema version : {_fmt(current)}")
    print(f"  Target schema version  : {SCHEMA_VERSION}")

    # Standard migration-runner semantics: if the ledger already records the target
    # version, there is nothing to apply and we do NOT re-run init_db(). This mirrors
    # Alembic/Flyway (applied migrations are not re-run) and keeps the runner a safe
    # no-op on an up-to-date DB. The app itself still calls init_db() on every boot,
    # so this runner never needs to force a re-application.
    if current is not None and current >= SCHEMA_VERSION:
        print("  Already at target version — nothing to apply.")
        print("  Done — database is up to date.")
        return 0

    print("  Applying migrations via database.init_db() ...")
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 — surface any migration failure clearly
        print(f"  ERROR: migration failed: {exc}", file=sys.stderr)
        return 2

    resulting = _current_db_version()
    print(f"  Resulting schema version : {_fmt(resulting)}")

    if resulting != SCHEMA_VERSION:
        print(
            f"  WARNING: resulting version ({_fmt(resulting)}) does not match target "
            f"({SCHEMA_VERSION}).",
            file=sys.stderr,
        )
        return 1

    print("  Done — database is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
