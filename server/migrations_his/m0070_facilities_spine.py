"""Migration 70: the facility spine (multisite-1, W7-J, 9 October 2026).

Samuel ruled on 3 October 2026 (multi-site M1) that a hospital group runs one
Orb appliance per legal entity, with a row-level facility id in the one
encrypted database. This migration lays the spine that every later multi-site
increment builds on. It changes no behaviour: with one facilities row (every
install today, the Ward Record, the demos, every reseed) every reader runs
exactly as before.

WHAT IT ADDS
  * facilities: one row per hospital site of the legal entity. Row 1 is seeded
    here with code MAIN and no name; its display name falls back to the site
    settings file's [site] name, then "This hospital"
    (services/facility_service.display_name). Rows are never deleted, so a
    closed satellite's records stay isolated. audit_epoch_id is the highest
    audit row id when a facility is created (NULL for facility 1): audit
    history up to the first expansion belongs to facility 1.
  * departments: the departments master, owned by multi-site (the ADT epic owns
    wards and beds). Seeded so the spelling staff are recorded under wins: the
    departments of active staff accounts first (doctors and nurses before
    other roles, each in the order the front desk's covering-department
    resolver reads them), then the site settings file's [[departments]], then
    admissions, ADT wards, Relay department channels and outpatient visits. A
    name that differs from a seeded one only by capital letters is the same
    department (the name is unique ignoring case); such spellings are reported
    by the operations status page and never rewritten here.
  * user_facilities: which facility a department-less account (admin,
    pharmacist, cashier) belongs to, and the facilities a visiting consultant
    may switch into. No backfill: an account with no row belongs to facility 1.
  * enterprise_access_grants: named, audited group-wide access (MS-R6, admin
    only in v1). Empty.
  * patients.registered_facility_id and admissions.facility_id, both INTEGER
    NOT NULL DEFAULT 1, so every existing row reads facility 1.
  * idx_admissions_patient_status and idx_admissions_facility_status, so the
    facility predicates later increments add to the scope subqueries do not
    scan admissions at 200 beds.

RULES THIS FILE KEEPS
  * No REFERENCES anywhere: SQLite refuses ADD COLUMN ... REFERENCES with a
    non-NULL default, migrations 20 and 23 forbid foreign keys from
    administrative tables, and a new child of users or patients would complicate
    every later FK-parent rebuild. Facility integrity rests on the services and
    on the operations status page (unknown facility ids are reported).
  * No users column: migrations._rebuild_users_role_check copies a frozen
    column list and aborts on a column it does not know.
  * No triggers on clinical tables. Every ALTER is ADD COLUMN with a constant
    default, which SQLite applies without rewriting the table.
  * One statement per conn.execute, idempotent throughout: tables and indexes
    use IF NOT EXISTS, columns are guarded by PRAGMA table_info, seeds use
    INSERT OR IGNORE.

A NOTE FOR ANY FUTURE ADMISSIONS REBUILD, beside m0058's and m0065's: the new
table must carry facility_id and ADMISSION_INDEXES below as well.
A NOTE FOR ANY FUTURE PATIENTS REBUILD: migration 52's frozen column list does
not know registered_facility_id; a later rebuild must add it to its own list.
"""
from __future__ import annotations

import uuid

from migrations import Migration

#: Facility 1's code. The display name is not stored (NULL): it comes from the
#: site settings file so a site that renames itself there sees the new name.
MAIN_CODE = "MAIN"

FACILITIES_DDL = """CREATE TABLE IF NOT EXISTS facilities (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE
        CHECK (length(code) BETWEEN 2 AND 12 AND code NOT GLOB '*[^A-Z0-9]*'),
    name TEXT CHECK (id = 1 OR (name IS NOT NULL AND trim(name) != '')),
    short_name TEXT,
    legal_name TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    city TEXT,
    district TEXT,
    state TEXT,
    state_code TEXT,
    pincode TEXT,
    phone TEXT,
    email TEXT,
    gstin TEXT,
    pan TEXT,
    hfr_id TEXT,
    abdm_hip_id TEXT,
    nabh_accreditation_no TEXT,
    nabh_valid_until TEXT,
    clinical_establishment_reg_no TEXT,
    timezone TEXT,
    audit_epoch_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT,
    updated_at TEXT,
    updated_by TEXT
)"""

DEPARTMENTS_DDL = """CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY,
    facility_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (name = trim(name) AND name != ''),
    display_name TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    seeded_from TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT
)"""

USER_FACILITIES_DDL = """CREATE TABLE IF NOT EXISTS user_facilities (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    facility_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('home', 'visiting')),
    department TEXT,
    granted_by TEXT,
    granted_at TEXT NOT NULL,
    reason TEXT,
    revoked_at TEXT,
    revoked_by TEXT,
    revoke_reason TEXT,
    CHECK ((kind = 'home' AND department IS NULL)
           OR (kind = 'visiting' AND department IS NOT NULL AND trim(department) != ''))
)"""

ENTERPRISE_ACCESS_GRANTS_DDL = """CREATE TABLE IF NOT EXISTS enterprise_access_grants (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    granted_by TEXT,
    granted_at TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (trim(reason) != ''),
    revoked_at TEXT,
    revoked_by TEXT,
    revoke_reason TEXT
)"""

TABLES = ("facilities", "departments", "user_facilities", "enterprise_access_grants")

SPINE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_departments_facility ON departments(facility_id)",
    # One live home facility per account, and one live row per account and
    # facility. Rows are revoked, never deleted.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_facilities_one_home "
    "ON user_facilities(user_id) WHERE kind = 'home' AND revoked_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_facilities_live "
    "ON user_facilities(user_id, facility_id) WHERE revoked_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_enterprise_access_grants_live "
    "ON enterprise_access_grants(user_id) WHERE revoked_at IS NULL",
)

#: Columns this migration adds to existing tables: (table, column, declaration).
FACILITY_COLUMNS = (
    ("patients", "registered_facility_id", "INTEGER NOT NULL DEFAULT 1"),
    ("admissions", "facility_id", "INTEGER NOT NULL DEFAULT 1"),
)

ADMISSION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_admissions_patient_status "
    "ON admissions(patient_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_admissions_facility_status "
    "ON admissions(facility_id, status)",
)
ADMISSION_INDEX_NAMES = frozenset({"idx_admissions_patient_status",
                                   "idx_admissions_facility_status"})

#: The roles whose department the front desk treats as clinical cover
#: (frontdesk_service._COVERING_ROLES). Their spelling is read first.
COVERING_ROLES = ("doctor", "clinician", "nurse")


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _clean(name) -> str | None:
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name or None


def _site_config_departments() -> list[str]:
    """The site settings file's [[departments]] names, as this process loaded
    them. Empty when the settings service is not importable (the restore
    verifier loads this file by path, with server/ not on sys.path) or no
    file was loaded."""
    try:
        from services.site_config_service import current_site_config
        return [d.get("name") for d in (current_site_config().departments or [])]
    except Exception:
        return []


def department_sources(conn) -> list[tuple[str, list[str]]]:
    """(source, names) in seeding order; within a source, the order the
    canonical spelling is chosen in. Absent tables and columns are skipped."""
    sources: list[tuple[str, list[str]]] = []
    users = _columns(conn, "users")
    if "department" in users:
        active = "AND COALESCE(is_active, 1) = 1 " if "is_active" in users else ""
        marks = ", ".join("?" for _ in COVERING_ROLES)
        covering = [r[0] for r in conn.execute(
            "SELECT DISTINCT department FROM users WHERE department IS NOT NULL "
            f"AND TRIM(department) != '' {active}AND role IN ({marks}) ORDER BY 1",
            COVERING_ROLES).fetchall()]
        others = [r[0] for r in conn.execute(
            "SELECT DISTINCT department FROM users WHERE department IS NOT NULL "
            f"AND TRIM(department) != '' {active}AND role NOT IN ({marks}) ORDER BY 1",
            COVERING_ROLES).fetchall()]
        sources.append(("users", covering + others))
    sources.append(("site_config", _site_config_departments()))
    for table, source, where in (
        ("admissions", "admissions", ""),
        ("wards", "wards", ""),
        ("channels", "relay_channel", "AND type = 'department'"),
        ("opd_visits", "opd_visits", ""),
    ):
        cols = _columns(conn, table)
        column = "name" if table == "channels" else "department"
        if column not in cols or (table == "channels" and "type" not in cols):
            continue
        sources.append((source, [r[0] for r in conn.execute(
            f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL "
            f"AND TRIM({column}) != '' {where} ORDER BY 1").fetchall()]))
    return sources


def seed_departments(conn) -> dict:
    """Insert every department name not already in the master (ignoring
    capital letters), first source first. Returns {source: rows added}."""
    added: dict[str, int] = {}
    for source, names in department_sources(conn):
        count = 0
        for raw in names:
            name = _clean(raw)
            if name is None:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO departments (facility_id, name, seeded_from, created_by) "
                "VALUES (1, ?, ?, 'migration_70')", (name, source))
            count += max(0, cur.rowcount or 0)
        added[source] = count
    return added


def seed_facility_one(conn) -> bool:
    """Facility 1, code MAIN, no stored name. False when it already exists."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO facilities (id, uuid, code, name, is_active, created_by) "
        "VALUES (1, ?, ?, NULL, 1, 'migration_70')", (str(uuid.uuid4()), MAIN_CODE))
    return bool(cur.rowcount)


def add_facility_columns(conn) -> list[str]:
    """The two ADD COLUMNs, each only when its table exists and lacks it."""
    added = []
    for table, column, declaration in FACILITY_COLUMNS:
        cols = _columns(conn, table)
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            added.append(f"{table}.{column}")
    return added


def apply(conn) -> None:
    conn.execute(FACILITIES_DDL)
    conn.execute(DEPARTMENTS_DDL)
    conn.execute(USER_FACILITIES_DDL)
    conn.execute(ENTERPRISE_ACCESS_GRANTS_DDL)
    for ddl in SPINE_INDEXES:
        conn.execute(ddl)
    seed_facility_one(conn)
    add_facility_columns(conn)
    if "facility_id" in _columns(conn, "admissions"):
        for ddl in ADMISSION_INDEXES:
            conn.execute(ddl)
    seed_departments(conn)


MIGRATION = Migration(70, "facilities_spine", apply)
