#!/usr/bin/env python3
"""Build the audit log's facility index, once, in a maintenance window.

WHY THIS EXISTS (multisite-11, W12-C, 20 October 2026)
Samuel ruled on 3 October 2026 (multi-site M6, audit (a)) that an audit row's
facility lives in its hashed metadata, so the hash chain never changes shape.
Reading one facility's rows then means reading
json_extract(metadata_json, '$.facility_id') for every row, and audit_logs is
the largest table on the appliance. idx_audit_facility indexes that expression
(and the time), so a facility's audit screens stay quick once more than one
facility is on record. Adding a second facility is refused until it exists
(facility_identity.can_create_facility, 'audit_index_missing').

It is not a migration on purpose: building it reads every audit row, and
inside the boot-time migration transaction on the encrypted database that
would hold the server's start (and every write) for as long as it takes. So it
is built here, by someone at the appliance, with Orb stopped, after timing it
on a backup copy first (--db).

WHAT IT DOES
  --check (the default)  counts the audit rows, says whether the index is
                         there, and checks that every row's metadata can be
                         read by SQLite's JSON functions. Changes nothing.
  --build --yes          builds the index in one transaction, times it, checks
                         that a facility read now uses it, and writes one
                         site_admin audit row (in the same database) saying so.
A row whose metadata SQLite cannot read as JSON would make the build fail, so
the build is refused while any exists, and the rows are named (audit rows are
never edited; tell Orb support).

Usage (from server/, with the same ORB_DB_PATH and ORB_DB_ENCRYPTION_KEY as
the server):
    python scripts/build_audit_facility_index.py --db /path/to/backup-copy.db --build --yes
    python scripts/build_audit_facility_index.py --check
    python scripts/build_audit_facility_index.py --build --yes

Exit codes:
    0  done, already built, or checked
    1  refused (the reason is printed)
    2  the database could not be opened or read
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Ensure server root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.audit_facility import FACILITY_SQL, INDEX_NAME, INDEX_SQL  # noqa: E402

#: How many unreadable rows the report names by id.
SHOW_UNREADABLE = 20

STOP_ORB_NOTE = (
    "Build it with Orb stopped: the build holds the database's write lock for its whole "
    "run, and while Orb cannot write audit rows it refuses to open patient records."
)


class IndexRefused(RuntimeError):
    """A build this command will not do. The message is for the operator."""


class CannotOpen(RuntimeError):
    """The database could not be opened or read."""


def _operator() -> dict[str, str | None]:
    """Who is at the console, for the audit row. Never a credential."""
    try:
        user: str | None = getpass.getuser()
    except Exception:
        user = None
    try:
        host: str | None = platform.node() or None
    except Exception:
        host = None
    return {"os_user": user, "host": host}


def open_database(db_path: Path | None) -> Any:
    """A connection to the configured database, or to `db_path` (a backup
    copy), opened the way the server opens it (the encryption key applied when
    one is set). Refuses a path that does not exist rather than create one."""
    import database

    if db_path is not None and not db_path.is_file():
        raise CannotOpen(f"There is no database file at {db_path}.")
    try:
        if db_path is None:
            conn = database._open_raw_connection()
        else:
            conn = database._open_raw_connection_at(db_path, apply_key=True)
        conn.row_factory = database._row_factory_for(conn)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except CannotOpen:
        raise
    except Exception as e:
        raise CannotOpen(
            f"The database could not be read ({e}). Use the same ORB_DB_ENCRYPTION_KEY as "
            "the server."
        ) from e
    return conn


def _index_exists(conn: Any) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (INDEX_NAME,)
        ).fetchone()
        is not None
    )


def _unreadable_rows(conn: Any) -> list[int]:
    """Ids of rows whose metadata SQLite's JSON functions refuse. json_valid
    is strict JSON, so each row it doubts (a NaN, which Python writes and
    SQLite reads) is tried with the very expression the index uses."""
    bad: list[int] = []
    for row in conn.execute(
        "SELECT id, metadata_json FROM audit_logs "
        "WHERE metadata_json IS NOT NULL AND json_valid(metadata_json) = 0 ORDER BY id"
    ).fetchall():
        try:
            conn.execute("SELECT json_extract(?, '$.facility_id')", (row[1],)).fetchone()
        except Exception:
            bad.append(int(row[0]))
    return bad


def survey(conn: Any) -> dict[str, Any]:
    """What the build would work on: {audit_rows, highest_id, index_exists,
    unreadable_rows, unreadable_ids}. Reads only."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_logs'"
    ).fetchone():
        raise IndexRefused("This database has no audit log table, so it is not an Orb database.")
    count, highest = conn.execute("SELECT COUNT(*), MAX(id) FROM audit_logs").fetchone()
    bad = _unreadable_rows(conn)
    return {
        "audit_rows": int(count or 0),
        "highest_id": int(highest or 0),
        "index_exists": _index_exists(conn),
        "unreadable_rows": len(bad),
        "unreadable_ids": bad[:SHOW_UNREADABLE],
    }


def _uses_index(conn: Any) -> bool:
    plan = conn.execute(
        f"EXPLAIN QUERY PLAN SELECT id FROM audit_logs WHERE {FACILITY_SQL} IN (1)"
    ).fetchall()
    return any(INDEX_NAME in str(step[-1]) for step in plan)


def build(conn: Any, facts: dict[str, Any], *, target: str) -> dict[str, Any]:
    """Build the index in one transaction with its audit row. Returns
    {seconds, uses_index}. Refuses while any row's metadata is unreadable."""
    if facts["unreadable_rows"]:
        raise IndexRefused(
            f"{facts['unreadable_rows']} audit row(s) hold metadata SQLite cannot read as JSON "
            f"(ids {', '.join(str(i) for i in facts['unreadable_ids'])}), so the index cannot "
            "be built. Audit rows are never edited. Tell Orb support."
        )
    from services.audit_service import log_audit_event

    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            started = time.perf_counter()
            conn.execute(INDEX_SQL)
            seconds = time.perf_counter() - started
            log_audit_event(
                correlation_id=str(uuid.uuid4()),
                event_category="site_admin",
                action_type="audit_facility_index_built",
                outcome="success",
                db=conn,
                summary_message=(
                    f"Audit facility index built over {facts['audit_rows']} rows in {seconds:.2f} s"
                ),
                actor_user_id=None,
                actor_role="console",
                target_type="database_index",
                target_id=INDEX_NAME,
                source_type="console",
                metadata={
                    "index": INDEX_NAME,
                    "rows": facts["audit_rows"],
                    "seconds": round(seconds, 3),
                    "database": target,
                    "operator": _operator(),
                    "script": "build_audit_facility_index.py",
                },
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = previous
    return {"seconds": round(seconds, 3), "uses_index": _uses_index(conn)}


def run(db_path: Path | None, *, do_build: bool, yes: bool) -> dict[str, Any]:
    """Check, or build. Raises IndexRefused or CannotOpen."""
    target = "the configured database" if db_path is None else f"the copy at {db_path}"
    conn = open_database(db_path)
    try:
        facts = survey(conn)
        result: dict[str, Any] = {"database": target, **facts, "built": False}
        if facts["index_exists"] or not do_build:
            return result
        if not yes:
            raise IndexRefused(f"Pass --yes to build the index. {STOP_ORB_NOTE}")
        result.update(build(conn, facts, target=target), built=True, index_exists=True)
        return result
    finally:
        conn.close()


def _report(result: dict[str, Any], *, do_build: bool) -> str:
    lines = [
        f"Database: {result['database']}.",
        f"Audit rows: {result['audit_rows']:,} (highest id {result['highest_id']:,}).",
    ]
    if result["unreadable_rows"]:
        lines.append(
            f"{result['unreadable_rows']} row(s) hold metadata SQLite cannot read as JSON: "
            f"ids {', '.join(str(i) for i in result['unreadable_ids'])}."
        )
    else:
        lines.append("Every row's metadata can be read.")
    if result["built"]:
        lines.append(f"Built {INDEX_NAME} in {result['seconds']:.2f} s.")
        lines.append(
            "Facility audit reads now use it."
            if result["uses_index"]
            else "SQLite does not choose it for a facility read yet; run ANALYZE once."
        )
    elif result["index_exists"]:
        lines.append(f"{INDEX_NAME} is already built. Nothing to do.")
    else:
        lines.append(f"{INDEX_NAME} is not built yet.")
        if not do_build:
            lines.append(STOP_ORB_NOTE)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the audit log's facility index (a maintenance step, never a migration)."
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="work on this database file instead of the configured one (a backup copy, to time "
        "the build first)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report only; change nothing (default)")
    mode.add_argument("--build", action="store_true", help="build the index")
    parser.add_argument("--yes", action="store_true", help="confirm --build (Orb is stopped)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    try:
        result = run(args.db, do_build=bool(args.build), yes=bool(args.yes))
    except IndexRefused as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 1
    except CannotOpen as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else _report(result, do_build=args.build))
    return 0


if __name__ == "__main__":
    sys.exit(main())
