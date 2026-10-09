"""Orb: the facility spine's read helpers (multisite-1, W7-J, 9 October 2026).

Samuel ruled on 3 October 2026 (multi-site M1) that a hospital group runs one
appliance per legal entity, with a row-level facility id in the one encrypted
database, and (M2) that isolation is keyed on DATA, not on the ORB_MULTI_SITE
switch: once the facilities table holds more than one row, active or not,
facility scope applies whatever the switch says. Rows are never deleted, so a
closed satellite's records stay isolated, and a switched-off restore of a
two-facility backup cannot leak.

This module answers the questions the rest of Orb asks about that state:

  * multi_facility_data(): more than one facilities row? Cached in process for
    CACHE_SECONDS (30 s) and refreshed sooner by invalidate_cache(), which any
    facility write in this process must call. One process today (W029), so a
    row inserted by a script is seen within 30 s.
  * cached_multi_facility_data(): the last answer, never touching the
    database. /api/health uses it, because health must not open a connection
    on the event loop.
  * get_facility, list_facilities, display_name: facility identity. Facility
    1's name is not stored (NULL); it reads the site settings file's [site]
    name, then "This hospital".
  * facility_one_problem / assert_facility_one: the ONE boot refusal of the
    spine, for a facilities table without an active facility 1, which only
    tampering can produce. No boot refusal depends on the switch.
  * facility_report: the facts the operations status page prints (unknown
    facility ids, department spellings that differ from the master,
    departments in use but not in the master, and patients with admissions at
    more than one facility). Counts and department names only, never a
    patient.

Nothing here changes what any user can see. The scope helpers
(facility_scope_for and the permissions facility arguments) arrive in
multisite-5a; the identity editor in multisite-2.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("orb.facility")

#: Facility 1: the site every install starts as (code MAIN, migration 70).
MAIN_FACILITY_ID = 1
MAIN_FACILITY_CODE = "MAIN"

#: How long a multi_facility_data answer is reused before the table is read
#: again. Writes made by this process call invalidate_cache() instead of
#: waiting; writes made outside it (a script, a restore) wait at most this long.
CACHE_SECONDS = 30.0

#: The name shown for facility 1 when neither it nor the site settings file
#: names it.
FALLBACK_DISPLAY_NAME = "This hospital"

#: Patched by tests to move time forward without sleeping.
_clock = time.monotonic

_lock = threading.Lock()
# value: the last answer (None until the table was first read);
# count: the number of facilities rows read; at: _clock() when read.
_state: dict[str, Any] = {"value": None, "count": None, "at": None}


class FacilitySpineError(RuntimeError):
    """The facilities table has no active facility 1. Orb must not start."""


def _db_session():
    # Looked up at call time, so a test that patches database.db_session sees
    # exactly the calls this module makes.
    import database
    return database.db_session()


def _table_exists(db, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _columns(db, table: str) -> set:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


# ═════════════════════════════════════════════════════════════════════════════
#  Is there more than one facility on record?
# ═════════════════════════════════════════════════════════════════════════════

def facility_count(db) -> int:
    """facilities rows, active or not. 0 when the table does not exist."""
    if not _table_exists(db, "facilities"):
        return 0
    return int(db.execute("SELECT COUNT(*) FROM facilities").fetchone()[0])


def _fresh(now: float) -> bool:
    at = _state["at"]
    return at is not None and _state["value"] is not None and (now - at) < CACHE_SECONDS


def _store(count: int) -> bool:
    value = count > 1
    with _lock:
        previous = _state["value"]
        _state.update(value=value, count=count, at=_clock())
    if previous is not None and previous != value:
        logger.warning(
            "Facilities on record changed: %d row(s). Multi-facility isolation is now %s.",
            count, "ON" if value else "off")
    return value


def multi_facility_data(*, db=None) -> bool:
    """True when the facilities table holds more than one row, active or not.

    BLOCKING on a cache miss (one COUNT query at most every CACHE_SECONDS):
    call it from plain-def code, or from async code through
    multi_facility_data_async. A failed read keeps the last answer, or answers
    False when there has never been one: a database that cannot be read fails
    every patient route on its own, and a guess of True would turn the whole
    appliance into 503s on a passing lock."""
    with _lock:
        if _fresh(_clock()):
            return bool(_state["value"])
    try:
        if db is not None:
            count = facility_count(db)
        else:
            with _db_session() as own:
                count = facility_count(own)
    except Exception as e:
        logger.warning("Could not read the facilities table: %s", e)
        with _lock:
            return bool(_state["value"])
    return _store(count)


async def multi_facility_data_async() -> bool:
    """multi_facility_data for async callers: a cache hit costs no executor
    hop, and a miss reads the table off the event loop (database.run_db)."""
    with _lock:
        if _fresh(_clock()):
            return bool(_state["value"])
    from database import run_db
    return bool(await run_db(multi_facility_data))


def cached_multi_facility_data() -> bool:
    """The last answer, without touching the database (False until the table
    has been read once). For /api/health, which must open no connection."""
    with _lock:
        return bool(_state["value"])


def invalidate_cache() -> None:
    """Forget the cached answer, so the next question reads the table. Every
    facility write in this process calls this after its commit."""
    with _lock:
        _state.update(value=None, count=None, at=None)


def prime_cache(db=None) -> bool:
    """Read the table now (boot), whatever the cache holds."""
    invalidate_cache()
    return multi_facility_data(db=db)


def platform_flags() -> dict:
    """The /api/health 'platform' object: the ORB_MULTI_SITE switch and
    whether more than one facility is on record. Two booleans, no PHI, read
    from config and the cache only."""
    import config
    return {
        "multi_site": bool(config.feature_enabled("multi_site")),
        "multi_facility_data": cached_multi_facility_data(),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Facility identity
# ═════════════════════════════════════════════════════════════════════════════

def _row_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def get_facility(facility_id: int, *, db=None) -> Optional[dict]:
    """One facilities row as a dict, or None."""
    if db is None:
        with _db_session() as own:
            return get_facility(facility_id, db=own)
    if not _table_exists(db, "facilities"):
        return None
    row = db.execute("SELECT * FROM facilities WHERE id = ?", (facility_id,)).fetchone()
    return _row_dict(row) if row else None


def list_facilities(*, db=None) -> list[dict]:
    """Every facilities row, active or not, by id."""
    if db is None:
        with _db_session() as own:
            return list_facilities(db=own)
    if not _table_exists(db, "facilities"):
        return []
    return [_row_dict(r) for r in db.execute("SELECT * FROM facilities ORDER BY id").fetchall()]


def _site_name() -> Optional[str]:
    try:
        from services.site_config_service import current_site_config
        name = current_site_config().site_name
    except Exception:
        return None
    name = (name or "").strip() if isinstance(name, str) else ""
    return name or None


def display_name(facility_id: int = MAIN_FACILITY_ID, *, db=None,
                 facility: Optional[dict] = None) -> str:
    """The name to print for a facility: its stored name; for facility 1
    without one, the site settings file's [site] name; then "This hospital"
    (facility 1) or its code (any other facility, whose name the schema
    requires)."""
    row = facility if facility is not None else get_facility(facility_id, db=db)
    stored = ((row or {}).get("name") or "").strip()
    if stored:
        return stored
    if facility_id == MAIN_FACILITY_ID:
        return _site_name() or FALLBACK_DISPLAY_NAME
    return ((row or {}).get("code") or "").strip() or FALLBACK_DISPLAY_NAME


# ═════════════════════════════════════════════════════════════════════════════
#  The one boot refusal
# ═════════════════════════════════════════════════════════════════════════════

def facility_one_problem(db) -> Optional[str]:
    """None when facility 1 exists and is active, else what is wrong, in
    words for the operator. Nothing else about facilities refuses a boot: not
    the switch, not a second row, not an unfinished screen."""
    if not _table_exists(db, "facilities"):
        return ("The facilities table is missing, so this database has not been "
                "brought up to date (migration 70).")
    row = db.execute("SELECT is_active FROM facilities WHERE id = ?",
                     (MAIN_FACILITY_ID,)).fetchone()
    if row is None:
        return ("Facility 1 (MAIN) is missing from the facilities table. Orb "
                "creates it and never deletes it, so the database has been changed "
                "by hand.")
    if not row[0]:
        return ("Facility 1 (MAIN) is marked inactive. Facility 1 can never be "
                "deactivated, so the database has been changed by hand.")
    return None


def assert_facility_one(db=None) -> None:
    """Refuse to start without an active facility 1 (FacilitySpineError)."""
    if db is None:
        with _db_session() as own:
            return assert_facility_one(own)
    problem = facility_one_problem(db)
    if problem:
        raise FacilitySpineError(
            f"Orb will not start: {problem} Restore the most recent verified "
            "backup, or put facility 1 back as it was.")


# ═════════════════════════════════════════════════════════════════════════════
#  What the operations status page prints
# ═════════════════════════════════════════════════════════════════════════════

#: (table, facility column, label) checked for ids no facilities row holds.
_FACILITY_ID_COLUMNS = (
    ("admissions", "facility_id", "admissions"),
    ("patients", "registered_facility_id", "patient records"),
    ("departments", "facility_id", "departments"),
    ("user_facilities", "facility_id", "staff facility rows"),
)

#: (table, column, extra WHERE, label): where department names are in use.
_DEPARTMENT_USES = (
    ("users", "department", "AND COALESCE(is_active, 1) = 1", "staff accounts"),
    ("admissions", "department", "", "admissions"),
    ("wards", "department", "", "wards"),
)


def facility_report(db) -> dict:
    """Facts for the operations status page. Counts and department names only.

    Keys: facilities (count), active_facilities, facility_one_problem,
    unknown_facility_ids {label: rows}, department_master (count),
    department_variants [{where, spelling, master_spelling, rows}],
    departments_not_in_master [{where, name, rows}],
    patients_at_several_facilities (count; must be 0)."""
    report: dict[str, Any] = {
        "facilities": facility_count(db),
        "active_facilities": 0,
        "facility_one_problem": facility_one_problem(db),
        "unknown_facility_ids": {},
        "department_master": 0,
        "department_variants": [],
        "departments_not_in_master": [],
        "patients_at_several_facilities": 0,
    }
    if not _table_exists(db, "facilities"):
        return report
    report["active_facilities"] = int(db.execute(
        "SELECT COUNT(*) FROM facilities WHERE is_active = 1").fetchone()[0])

    for table, column, label in _FACILITY_ID_COLUMNS:
        if column not in _columns(db, table):
            continue
        n = int(db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL "
            f"OR {column} NOT IN (SELECT id FROM facilities)").fetchone()[0])
        if n:
            report["unknown_facility_ids"][label] = n

    master: dict[str, str] = {}
    if _table_exists(db, "departments"):
        for (name,) in db.execute("SELECT name FROM departments").fetchall():
            master[name.strip().casefold()] = name
    report["department_master"] = len(master)

    for table, column, extra, label in _DEPARTMENT_USES:
        cols = _columns(db, table)
        if column not in cols:
            continue
        if "is_active" not in cols:
            extra = ""
        rows = db.execute(
            f"SELECT {column} AS name, COUNT(*) AS n FROM {table} "
            f"WHERE {column} IS NOT NULL AND TRIM({column}) != '' {extra} "
            f"GROUP BY {column} ORDER BY {column}").fetchall()
        for row in rows:
            spelling, n = row["name"], int(row["n"])
            canonical = master.get(spelling.strip().casefold())
            if canonical is None:
                report["departments_not_in_master"].append(
                    {"where": label, "name": spelling, "rows": n})
            elif canonical != spelling:
                report["department_variants"].append(
                    {"where": label, "spelling": spelling, "master_spelling": canonical,
                     "rows": n})

    if "facility_id" in _columns(db, "admissions"):
        report["patients_at_several_facilities"] = int(db.execute(
            "SELECT COUNT(*) FROM (SELECT patient_id FROM admissions "
            "GROUP BY patient_id HAVING COUNT(DISTINCT facility_id) > 1)").fetchone()[0])
    return report
