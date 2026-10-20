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

  * facility_scope_for (multisite-5a, W8-K): the set of facilities whose
    patients an account may see, or None for no facility limit. It reads only
    the account's id, role and department, plus in-process maps of the
    departments master, live home rows and live enterprise grants, so every
    loader that builds a user dict straight from SELECT * FROM users gets the
    same answer. With one facility on record it answers None before reading
    anything, so nothing changes. permissions.facility_scope and the
    facilities= argument of the scope helpers are built on it.
  * user_facility: the facility an account works at, for UserOut and
    /api/auth/me.
  * visiting access (multisite-19, W13-E): a doctor's live 'visiting' row
    lets one session work at another facility, checked on every request
    (visiting_access_problem, visiting_session_user, switch_options_on).

The identity editor arrives in multisite-2.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

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
# The facility scope maps (multisite-5a), read only while more than one
# facility is on record: maps {departments, homes, grants}; at: _clock().
_maps_state: dict[str, Any] = {"maps": None, "at": None}
# Facility identity for labels: rows {id: {id, code, name}}; at: _clock().
_identity_state: dict[str, Any] = {"rows": None, "at": None}


class FacilitySpineError(RuntimeError):
    """The facilities table has no active facility 1. Orb must not start."""


class FacilityScopeError(RuntimeError):
    """More than one facility is on record and the scope maps could not be read
    (and none was read before). The request fails rather than guess a scope."""


def _db_session():
    # Looked up at call time, so a test that patches database.db_session sees
    # exactly the calls this module makes.
    import database

    return database.db_session()


def _table_exists(db, table: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


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
            count,
            "ON" if value else "off",
        )
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
    facility write in this process calls this after its commit, and so does
    every write to the departments master, user_facilities or
    enterprise_access_grants (the scope maps below are forgotten too)."""
    with _lock:
        _state.update(value=None, count=None, at=None)
        _maps_state.update(maps=None, at=None)
        _identity_state.update(rows=None, at=None)


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


def get_facility(facility_id: int, *, db=None) -> dict | None:
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


def _site_name() -> str | None:
    try:
        from services.site_config_service import current_site_config

        name = current_site_config().site_name
    except Exception:
        return None
    name = (name or "").strip() if isinstance(name, str) else ""
    return name or None


def display_name(
    facility_id: int = MAIN_FACILITY_ID, *, db=None, facility: dict | None = None
) -> str:
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


def facility_one_problem(db) -> str | None:
    """None when facility 1 exists and is active, else what is wrong, in
    words for the operator. Nothing else about facilities refuses a boot: not
    the switch, not a second row, not an unfinished screen."""
    if not _table_exists(db, "facilities"):
        return (
            "The facilities table is missing, so this database has not been "
            "brought up to date (migration 70)."
        )
    row = db.execute(
        "SELECT is_active FROM facilities WHERE id = ?", (MAIN_FACILITY_ID,)
    ).fetchone()
    if row is None:
        return (
            "Facility 1 (MAIN) is missing from the facilities table. Orb "
            "creates it and never deletes it, so the database has been changed "
            "by hand."
        )
    if not row[0]:
        return (
            "Facility 1 (MAIN) is marked inactive. Facility 1 can never be "
            "deactivated, so the database has been changed by hand."
        )
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
            "backup, or put facility 1 back as it was."
        )


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

#: (table, column, extra WHERE clauses, label): where department names are in
#: use. Each clause names the column it needs and is left out on a table
#: without that column. A patient portal login (role 'patient') is not a staff
#: account: admission writes the patient's ward into its department column,
#: so a ward name there is not a department in use.
_DEPARTMENT_USES: tuple[tuple[str, str, tuple[tuple[str, str], ...], str], ...] = (
    (
        "users",
        "department",
        (
            ("is_active", "AND COALESCE(is_active, 1) = 1"),
            ("role", "AND COALESCE(role, '') != 'patient'"),
        ),
        "staff accounts",
    ),
    ("admissions", "department", (), "admissions"),
    ("wards", "department", (), "wards"),
)


def facility_report(db: Any) -> dict:
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
    report["active_facilities"] = int(
        db.execute("SELECT COUNT(*) FROM facilities WHERE is_active = 1").fetchone()[0]
    )

    for table, column, label in _FACILITY_ID_COLUMNS:
        if column not in _columns(db, table):
            continue
        n = int(
            db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL "
                f"OR {column} NOT IN (SELECT id FROM facilities)"
            ).fetchone()[0]
        )
        if n:
            report["unknown_facility_ids"][label] = n

    master: dict[str, str] = {}
    if _table_exists(db, "departments"):
        for (name,) in db.execute("SELECT name FROM departments").fetchall():
            master[name.strip().casefold()] = name
    report["department_master"] = len(master)

    for table, column, clauses, label in _DEPARTMENT_USES:
        cols = _columns(db, table)
        if column not in cols:
            continue
        extra = " ".join(clause for needed, clause in clauses if needed in cols)
        rows = db.execute(
            f"SELECT {column} AS name, COUNT(*) AS n FROM {table} "
            f"WHERE {column} IS NOT NULL AND TRIM({column}) != '' {extra} "
            f"GROUP BY {column} ORDER BY {column}"
        ).fetchall()
        for row in rows:
            spelling, n = row["name"], int(row["n"])
            canonical = master.get(spelling.strip().casefold())
            if canonical is None:
                report["departments_not_in_master"].append(
                    {"where": label, "name": spelling, "rows": n}
                )
            elif canonical != spelling:
                report["department_variants"].append(
                    {"where": label, "spelling": spelling, "master_spelling": canonical, "rows": n}
                )

    if "facility_id" in _columns(db, "admissions"):
        report["patients_at_several_facilities"] = int(
            db.execute(
                "SELECT COUNT(*) FROM (SELECT patient_id FROM admissions "
                "GROUP BY patient_id HAVING COUNT(DISTINCT facility_id) > 1)"
            ).fetchone()[0]
        )
    return report


# ═════════════════════════════════════════════════════════════════════════════
#  Facility scope (multisite-5a, W8-K, 10 October 2026)
# ═════════════════════════════════════════════════════════════════════════════
# facility_scope_for(user) answers "which facilities' patients may this account
# see?" as a frozenset of facility ids, or None for no facility limit. The
# department scope (permissions.department_scope) still applies on top of it.
#
#   1. One facility on record: None, before anything else is read. Every
#      install today runs exactly as before: no statement, no metadata key.
#   2. A service account (database.SERVICE_ACCOUNT_IDS) or the patient role:
#      None. A patient's portal is scoped to their own record elsewhere.
#   3. An admin holding a live enterprise grant: None (group-wide). Ruling
#      MS-R6 (a) keeps group reach to the admin role, so a grant row on any
#      other account is ignored and that account resolves by the rules below.
#   4. A department on the account: {that department's facility}, from the
#      departments master (names compared ignoring case and outer spaces). A
#      department the master does not list gives frozenset(): the account sees
#      no patient, and the operations status page lists the name.
#   5. No department: {the facility of the live home row}, or {1} with none.
#      Every script that creates staff creates facility-1 staff, so they need
#      no row; the staffing screen that can put a department-less account at
#      another facility writes the row in the same transaction (multisite-6).
#
# It reads ONLY the id, role and department of the user it is given, never a
# joined or computed field, so the six places that build a user dict straight
# from SELECT * FROM users (auth._load_session_context, the password and PIN
# logins, auth_routes._load_user_row, admin_users_routes._get_user_row and the
# socket handshake) all get the same answer without being edited
# (tests/test_facility_resolver_loaders.py).
#
# The maps are cached in process for CACHE_SECONDS and forgotten by
# invalidate_cache(), which every write to departments, user_facilities or
# enterprise_access_grants in this process calls after its commit. A revoke
# made by a script outside the process takes effect within CACHE_SECONDS.

#: Roles whose sessions are never facility-bound.
UNBOUND_ROLES = frozenset({"patient"})

#: The only role an enterprise grant widens (ruling MS-R6 (a)).
ENTERPRISE_GRANT_ROLES = frozenset({"admin"})


def _field(user, key: str):
    """One field of a user dict or a sqlite3.Row; None when absent."""
    if user is None:
        return None
    getter = getattr(user, "get", None)
    if callable(getter):
        return getter(key)
    try:
        return user[key]
    except (IndexError, KeyError):
        return None


def _department_of(user) -> str | None:
    """The account's department exactly as permissions.department_scope
    reads it (the one derivation of ward scope)."""
    from permissions import department_scope

    return department_scope({"department": _field(user, "department")})


def _department_key(name: str) -> str:
    return name.strip().casefold()


def _service_account(user_id) -> bool:
    if user_id is None:
        return False
    from database import SERVICE_ACCOUNT_IDS

    return str(user_id) in SERVICE_ACCOUNT_IDS


def _load_scope_maps(db) -> dict:
    departments: dict[str, int] = {}
    if _table_exists(db, "departments"):
        for row in db.execute("SELECT name, facility_id FROM departments").fetchall():
            name, fid = row[0], row[1]
            if isinstance(name, str) and name.strip() and fid is not None:
                departments[_department_key(name)] = int(fid)
    homes: dict[str, int] = {}
    if _table_exists(db, "user_facilities"):
        for row in db.execute(
            "SELECT user_id, facility_id FROM user_facilities "
            "WHERE kind = 'home' AND revoked_at IS NULL"
        ).fetchall():
            if row[0] is not None and row[1] is not None:
                homes[str(row[0])] = int(row[1])
    grants: frozenset = frozenset()
    if _table_exists(db, "enterprise_access_grants"):
        grants = frozenset(
            str(row[0])
            for row in db.execute(
                "SELECT user_id FROM enterprise_access_grants WHERE revoked_at IS NULL"
            ).fetchall()
            if row[0] is not None
        )
    return {"departments": departments, "homes": homes, "grants": grants}


def scope_maps(*, db=None) -> dict:
    """{departments: {name key: facility id}, homes: {user id: facility id},
    grants: frozenset of user ids}, cached for CACHE_SECONDS. BLOCKING on a
    cache miss. A failed read keeps the last maps; with none it raises
    FacilityScopeError, because guessing a scope could widen one."""
    with _lock:
        at, maps = _maps_state["at"], _maps_state["maps"]
        if maps is not None and at is not None and (_clock() - at) < CACHE_SECONDS:
            return maps
    try:
        if db is not None:
            maps = _load_scope_maps(db)
        else:
            with _db_session() as own:
                maps = _load_scope_maps(own)
    except Exception as e:
        logger.warning("Could not read the facility scope maps: %s", e)
        with _lock:
            if _maps_state["maps"] is not None:
                return _maps_state["maps"]
        raise FacilityScopeError(
            "More than one facility is on record and the departments, home "
            "facility and enterprise access lists could not be read."
        ) from e
    with _lock:
        _maps_state.update(maps=maps, at=_clock())
    return maps


def facility_scope_for(user, *, db=None) -> frozenset | None:
    """The facilities whose patients `user` may see, or None for no facility
    limit (see the block comment above). Reads only the user's id, role and
    department. BLOCKING on a cache miss: call it from plain-def code, or from
    async code through facility_scope_for_async."""
    if not multi_facility_data(db=db):
        return None
    user_id = _field(user, "id")
    role = _field(user, "role")
    if role in UNBOUND_ROLES or _service_account(user_id):
        return None
    maps = scope_maps(db=db)
    uid = None if user_id is None else str(user_id)
    if role in ENTERPRISE_GRANT_ROLES and uid is not None and uid in maps["grants"]:
        return None
    dept = _department_of(user)
    if dept is not None:
        fid = maps["departments"].get(_department_key(dept))
        return frozenset() if fid is None else frozenset({fid})
    return frozenset({maps["homes"].get(uid, MAIN_FACILITY_ID)})


async def facility_scope_for_async(user) -> frozenset | None:
    """facility_scope_for for async callers. With one facility on record and
    a warm cache it answers None with no executor hop and no statement."""
    if not await multi_facility_data_async():
        return None
    from database import run_db

    return await run_db(facility_scope_for, user)


def department_facility(name: str | None, *, db=None) -> int | None:
    """The facility that owns a department in the master, or None when the
    master does not list it. Names compare ignoring case and outer spaces."""
    if not isinstance(name, str) or not name.strip():
        return None
    return scope_maps(db=db)["departments"].get(_department_key(name))


def home_facility(user_id: str | None, *, db=None) -> int:
    """The facility of a department-less account: its live home row, else 1."""
    if user_id is None:
        return MAIN_FACILITY_ID
    return scope_maps(db=db)["homes"].get(str(user_id), MAIN_FACILITY_ID)


def has_enterprise_grant(user_id: str | None, *, db=None) -> bool:
    """A live enterprise grant row for this account, whatever its role. Only
    an admin's grant widens the scope (ENTERPRISE_GRANT_ROLES)."""
    if user_id is None:
        return False
    return str(user_id) in scope_maps(db=db)["grants"]


# ── The facility an account works at (UserOut and /api/auth/me) ─────────────


def _patient_record_facility(user_id, db) -> int | None:
    from permissions import FACILITY_PATIENT_SQL

    row = db.execute(
        f"SELECT {FACILITY_PATIENT_SQL} AS fid FROM patients p "
        "WHERE p.user_id = ? ORDER BY p.rowid DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def user_facility_id(user, *, db=None) -> int | None:
    """The facility an account belongs to, for labels: facility 1 while only
    one is on record; otherwise the department's facility (None when the
    master does not list it), the home row or 1 for a department-less account
    (an enterprise admin included: the grant widens what they see, not where
    they work), and for a patient login the facility of their own record."""
    if not multi_facility_data(db=db):
        return MAIN_FACILITY_ID
    user_id = _field(user, "id")
    if _field(user, "role") == "patient":
        if user_id is None:
            return None
        if db is not None:
            return _patient_record_facility(user_id, db)
        with _db_session() as own:
            return _patient_record_facility(user_id, own)
    dept = _department_of(user)
    if dept is not None:
        return department_facility(dept, db=db)
    return home_facility(user_id, db=db)


def _load_identity_rows(db) -> dict:
    if not _table_exists(db, "facilities"):
        return {}
    return {
        int(r[0]): {"id": int(r[0]), "code": r[1], "name": r[2]}
        for r in db.execute("SELECT id, code, name FROM facilities").fetchall()
    }


def facility_identity(facility_id: int | None, *, db=None) -> dict | None:
    """{id, code, name} for a label, cached for CACHE_SECONDS. name is the
    display name (facility 1 falls back to the site settings file). A
    facility id no row holds gives {id, code None, name None}."""
    if facility_id is None:
        return None
    with _lock:
        at, rows = _identity_state["at"], _identity_state["rows"]
        fresh = rows is not None and at is not None and (_clock() - at) < CACHE_SECONDS
    if not fresh:
        if db is not None:
            rows = _load_identity_rows(db)
        else:
            with _db_session() as own:
                rows = _load_identity_rows(own)
        with _lock:
            _identity_state.update(rows=rows, at=_clock())
    row = (rows or {}).get(int(facility_id))
    if row is None:
        return {"id": int(facility_id), "code": None, "name": None}
    return {"id": row["id"], "code": row["code"], "name": display_name(row["id"], facility=row)}


def user_facility(user, *, db=None) -> dict | None:
    """{id, code, name} of the facility an account belongs to, or None (a
    department the master does not list, or a patient login with no record).
    BLOCKING on a cache miss."""
    return facility_identity(user_facility_id(user, db=db), db=db)


# ═════════════════════════════════════════════════════════════════════════════
#  Visiting access: one account, one facility per session (multisite-19)
# ═════════════════════════════════════════════════════════════════════════════
# Ruling MS-R7 (a), 3 October 2026: a consultant who works at two facilities
# of the group keeps ONE account, and each session works at ONE facility at a
# time; the switch is audited. Separate accounts stay the option for the first
# go-live. Built behind ORB_MULTI_SITE, and it does nothing until a live
# 'visiting' user_facilities row exists (no screen writes one yet).
#
#   * A visiting row names a facility and the department the consultant works
#     in there. That department must be listed in the departments master at
#     that same facility (MS-R4 (a): names are unique across the group).
#   * POST /api/auth/facility moves a session there. The new session token
#     carries the claim 'fac' (the facility id). A token without it is a home
#     session, exactly as every token minted before this change.
#   * auth.authenticate_session_token checks the claim on EVERY request, in
#     the same database session as the users row, with an uncached read of the
#     row (visiting_access_problem): anything short of a live, valid row is a
#     401, so a revoked row ends the session at its next request, socket
#     deliveries included.
#   * While the claim holds, the session's user dict carries the row's
#     department in place of the account's own (visiting_session_user), so
#     permissions.department_scope, facility_scope_for and every scope helper
#     answer for the visiting facility without being edited. The account's own
#     department rides along under HOME_DEPARTMENT_KEY, for audit only.
#   * Strict defaults: doctors only (VISITING_ROLES), never on a shared ward
#     tablet's PIN session, never while ORB_MULTI_SITE is off or one facility
#     is on record, never at the account's own facility. Any of these ends a
#     visiting session at its next request.
#
# Lanes that read an account's department from the users table by its id,
# rather than from the session's user dict (relay_service, the facility_lanes
# and facility_stamp account helpers, audit_facility.actor_facility and a few
# others), still answer for the account's own department in a visiting
# session. Each of them fails closed for the visiting facility.

#: Roles that may hold a visiting session. MS-R7 names visiting consultants;
#: the builder's strict default is doctors only.
VISITING_ROLES = frozenset({"doctor"})

#: Keys a visiting session adds to its user dict (visiting_session_user).
SESSION_FACILITY_KEY = "session_facility_id"
HOME_DEPARTMENT_KEY = "home_department"
VISITING_ACCESS_KEY = "visiting_access_id"

#: What a session hears when its visiting access no longer holds.
VISITING_ENDED_MESSAGE = "Your visiting access has ended. Sign in again."

_UNSET: Any = object()

_VISITING_ROWS_SQL = (
    "SELECT uf.id AS id, uf.facility_id AS facility_id, "
    "TRIM(uf.department) AS department, f.is_active AS facility_active, "
    "d.facility_id AS department_facility_id, d.is_active AS department_active "
    "FROM user_facilities uf "
    "LEFT JOIN facilities f ON f.id = uf.facility_id "
    "LEFT JOIN departments d ON d.name = TRIM(uf.department) COLLATE NOCASE "
    "WHERE uf.user_id = ? AND uf.kind = 'visiting' AND uf.revoked_at IS NULL"
)


def visiting_switch_on() -> bool:
    """Is ORB_MULTI_SITE on right now? A visiting session needs it: the switch
    gates growth, so turning it off ends every visiting session at its next
    request, while isolation keeps following the data. Read at call time."""
    import config

    try:
        return bool(config.feature_enabled("multi_site"))
    except Exception:
        return False


def visiting_rows_on(db: Any, user_id: Any, facility_id: int | None = None) -> list[dict]:
    """The account's live visiting rows, read on the caller's connection, each
    {id, facility_id, department, facility_active, department_facility_id,
    department_active}. Never cached: a revoke must hold at the next request."""
    if user_id is None or not _table_exists(db, "user_facilities"):
        return []
    sql = _VISITING_ROWS_SQL
    params: tuple = (str(user_id),)
    if facility_id is not None:
        sql += " AND uf.facility_id = ?"
        params = (*params, int(facility_id))
    rows = db.execute(sql + " ORDER BY uf.facility_id, uf.id", params).fetchall()
    return [_row_dict(r) for r in rows]


def visiting_access_on(db: Any, user_id: Any, facility_id: int) -> dict | None:
    """The account's live visiting row at one facility, or None. The partial
    unique index on user_facilities allows at most one."""
    rows = visiting_rows_on(db, user_id, facility_id)
    return rows[0] if rows else None


def visiting_access_problem(
    account: Any,
    access: dict | None,
    facility_id: int,
    *,
    db: Any = None,
    home_facility_id: Any = _UNSET,
) -> str | None:
    """None when `access` (a live visiting row) lets a session of `account`
    (its users row, never a session dict) work at `facility_id`; otherwise
    why not, as a short code:

      switch_off             ORB_MULTI_SITE is off
      single_facility        one facility on record
      role                   not a doctor (VISITING_ROLES), or a system account
      inactive               the account is deactivated
      no_access              no live visiting row at that facility
      facility_inactive      the facility is closed, or not on record
      department_not_listed  the row's department is not in the master at
                             that facility
      department_inactive    the master marks the department inactive
      home_facility          the account's own facility is that facility
    """
    if not visiting_switch_on():
        return "switch_off"
    if not multi_facility_data(db=db):
        return "single_facility"
    uid = _field(account, "id")
    if _field(account, "role") not in VISITING_ROLES or uid is None or _service_account(uid):
        return "role"
    active = _field(account, "is_active")
    if active is not None and not active:
        return "inactive"
    if access is None or int(access["facility_id"]) != int(facility_id):
        return "no_access"
    if not access.get("facility_active"):
        return "facility_inactive"
    dept_facility = access.get("department_facility_id")
    if not access.get("department") or dept_facility is None:
        return "department_not_listed"
    if int(dept_facility) != int(facility_id):
        return "department_not_listed"
    if not access.get("department_active"):
        return "department_inactive"
    home = user_facility_id(account, db=db) if home_facility_id is _UNSET else home_facility_id
    if home is not None and int(home) == int(facility_id):
        return "home_facility"
    return None


def visiting_session_user(user: dict, access: dict) -> dict:
    """The session's user dict while a visiting claim holds: the row's
    department in place of the account's own, so department_scope and the
    facility scope answer for the visiting facility; the account's own
    department under HOME_DEPARTMENT_KEY; the facility and row ids."""
    overlaid = dict(user)
    overlaid[HOME_DEPARTMENT_KEY] = user.get("department")
    overlaid["department"] = access["department"]
    overlaid[SESSION_FACILITY_KEY] = int(access["facility_id"])
    overlaid[VISITING_ACCESS_KEY] = int(access["id"])
    return overlaid


def session_facility_id(user: Any) -> int | None:
    """The facility a visiting session works at, or None for a home session."""
    value = _field(user, SESSION_FACILITY_KEY)
    return None if value is None else int(value)


def session_audit_fields(user: Any) -> dict:
    """Audit metadata naming a visiting session's facility, access row and the
    account's own department. {} for a home session, so every row a home
    session writes stays exactly as it was."""
    fid = session_facility_id(user)
    if fid is None:
        return {}
    return {
        SESSION_FACILITY_KEY: fid,
        VISITING_ACCESS_KEY: _field(user, VISITING_ACCESS_KEY),
        HOME_DEPARTMENT_KEY: _field(user, HOME_DEPARTMENT_KEY),
    }


def switch_options_on(db: Any, account: Any) -> dict:
    """Where a session of `account` (its users row) may work, read on the
    caller's connection: {home_facility_id (None for a department the master
    does not list), home_department, visiting: [live rows that pass
    visiting_access_problem]}. The visiting list is empty unless the switch is
    on, more than one facility is on record and the account is a doctor."""
    home = user_facility_id(account, db=db)
    visiting: list[dict] = []
    if (
        visiting_switch_on()
        and multi_facility_data(db=db)
        and _field(account, "role") in VISITING_ROLES
    ):
        for access in visiting_rows_on(db, _field(account, "id")):
            fid = int(access["facility_id"])
            if visiting_access_problem(account, access, fid, db=db, home_facility_id=home) is None:
                visiting.append(access)
    return {
        "home_facility_id": home,
        "home_department": _department_of(account),
        "visiting": visiting,
    }


def reachable_facilities_for(account: Any, *, db: Any = None) -> frozenset | None:
    """The facilities whose patients `account` (a users row: id, role,
    department) can open in some session: its facility scope plus the
    facility of each live, valid visiting row. For deciding whether a named
    attending can be paged (escalation_service), never for what a session
    sees: a session sees its own facility only (facility_scope_for). Equal to
    facility_scope_for while the switch is off or for any role but doctor."""
    scope = facility_scope_for(account, db=db)
    if scope is None or _field(account, "role") not in VISITING_ROLES:
        return scope
    if not visiting_switch_on():
        return scope
    if db is None:
        with _db_session() as own:
            options = switch_options_on(own, account)
    else:
        options = switch_options_on(db, account)
    return scope | frozenset(int(a["facility_id"]) for a in options["visiting"])
