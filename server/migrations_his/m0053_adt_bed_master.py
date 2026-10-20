"""Migration 53: the bed master (adt-1, beds and admissions epic, W2-B).

Until this migration Orb had no ward, room or bed as a record. Wards and beds
were free text on patients, admissions and housekeeping_units, and
housekeeping_units was the only per-bed register (any nurse could type into
it). This migration adds the master the beds and admissions module (ORB_ADT,
default off) reads and writes:

  wards              one row per ward. `code` is the label written to
                     admissions.ward and patients.ward; it is unique without
                     regard to capital letters or surrounding spaces ('gm-1'
                     beside 'GM-1' is refused), and it is immutable once
                     anything refers to it (retire and recreate instead).
                     `facility_code` is a plain label, 'MAIN' on every row,
                     until the multi-site epic adds a facilities table. Codes
                     are unique across the whole group, not per facility
                     (multi-site ruling MS-R4 (a)), so the unique index is on
                     the code alone.
  ward_rooms         rooms and bays inside a ward; an isolation room drives
                     terminal cleans.
  bed_classes        general, private, ICU and so on, with the bed-day charge
                     code billing will price by (adt-15). No seed: a hospital
                     types its own.
  bed_master_events  who changed the master, and when. Append-only, enforced
                     by triggers. No patient data.

And five columns on housekeeping_units, which becomes the physical bed row:
ward_id, room_id, bed_class, is_temporary, retired_at. A unit with a ward_id is
a bed the master registered; its unit_code is '<ward code>/<bed label>' because
idx_hk_units_code is global. A partial unique index keeps a bed label unique
inside its ward, again without regard to capital letters.

THE LAZY PATH. services/housekeeping_service.ensure_housekeeping_schema
creates the housekeeping tables from migrations.HOUSEKEEPING_TABLES (the
migration 22 shape) and then calls add_housekeeping_unit_columns below, so a
database whose housekeeping tables were made lazily gains the same columns in
the same order, and the lazy path can never recreate the old shape.
migrations.py is not edited (it is frozen from wave 2 on), so
HOUSEKEEPING_TABLES itself keeps the migration 22 shape and this function is
the one place the five columns are defined.

Additive throughout: new tables with no foreign keys, ALTER TABLE ADD COLUMN
with constant defaults, guarded by PRAGMA table_info, one statement per
execute. Existing housekeeping rows are untouched: every new column is NULL
(is_temporary 0) on them, so with ORB_ADT off nothing reads or writes any of
this and behaviour is unchanged.
"""

from migrations import Migration

#: The facility label every ward carries until the multi-site epic.
FACILITY_CODE = "MAIN"

WARD_TYPES: tuple = (
    "general",
    "icu",
    "hdu",
    "nicu",
    "picu",
    "maternity",
    "daycare",
    "emergency",
    "isolation",
    "other",
)
ROOM_TYPES: tuple = ("bay", "private", "semi_private", "isolation", "icu_cubicle", "other")
ENTITY_TYPES: tuple = ("ward", "room", "bed", "bed_class")

BED_MASTER_TABLES: list = [
    """CREATE TABLE IF NOT EXISTS wards (
        id TEXT PRIMARY KEY,
        facility_code TEXT NOT NULL DEFAULT 'MAIN',
        code TEXT NOT NULL,
        name TEXT,
        department TEXT NOT NULL,
        ward_type TEXT NOT NULL DEFAULT 'general'
            CHECK (ward_type IN ('general', 'icu', 'hdu', 'nicu', 'picu', 'maternity',
                                 'daycare', 'emergency', 'isolation', 'other')),
        default_bed_class TEXT,
        cleaning_tracked INTEGER NOT NULL DEFAULT 1,
        register_complete INTEGER NOT NULL DEFAULT 0,
        register_complete_at TEXT,
        register_complete_by TEXT,
        ledger_complete_from TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS ward_rooms (
        id TEXT PRIMARY KEY,
        ward_id TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        room_type TEXT NOT NULL DEFAULT 'bay'
            CHECK (room_type IN ('bay', 'private', 'semi_private', 'isolation',
                                 'icu_cubicle', 'other')),
        is_isolation INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS bed_classes (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        bed_day_service_code TEXT,
        is_critical_care INTEGER NOT NULL DEFAULT 0,
        counts_in_census INTEGER NOT NULL DEFAULT 1,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS bed_master_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL
            CHECK (entity_type IN ('ward', 'room', 'bed', 'bed_class')),
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        detail_json TEXT,
        reason TEXT,
        actor_user_id TEXT,
        occurred_at TEXT NOT NULL
    )""",
]

BED_MASTER_TRIGGERS: list = [
    """CREATE TRIGGER IF NOT EXISTS trg_bed_master_events_no_update
       BEFORE UPDATE ON bed_master_events
       BEGIN
           SELECT RAISE(ABORT, 'bed_master_events is append-only: a change to the master is a new row, never an edit');
       END""",
    """CREATE TRIGGER IF NOT EXISTS trg_bed_master_events_no_delete
       BEFORE DELETE ON bed_master_events
       BEGIN
           SELECT RAISE(ABORT, 'bed_master_events is append-only: DELETE is forbidden');
       END""",
]

BED_MASTER_INDEXES: list = [
    # Unique across the group, not per facility (MS-R4 (a)). Compared on the
    # trimmed, lower-cased code, so 'gm-1' beside 'GM-1' is refused.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_wards_code_ci ON wards(LOWER(TRIM(code)))",
    "CREATE INDEX IF NOT EXISTS idx_wards_department ON wards(department, is_active)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ward_rooms_code_ci ON ward_rooms("
    "ward_id, LOWER(TRIM(code)))",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_bed_classes_code_ci ON bed_classes(LOWER(TRIM(code)))",
    "CREATE INDEX IF NOT EXISTS idx_bed_master_events_entity ON bed_master_events("
    "entity_type, entity_id, id)",
]

#: The five columns housekeeping_units gains, in order. Constant defaults only.
HOUSEKEEPING_UNIT_COLUMNS: tuple = (
    ("ward_id", "TEXT"),
    ("room_id", "TEXT"),
    ("bed_class", "TEXT"),
    ("is_temporary", "INTEGER NOT NULL DEFAULT 0"),
    ("retired_at", "TEXT"),
)

HOUSEKEEPING_UNIT_INDEXES: list = [
    "CREATE INDEX IF NOT EXISTS idx_hk_units_ward_id ON housekeeping_units(ward_id, is_active)",
    # A bed label is unique inside its ward without regard to capital letters.
    # Only master beds carry a ward_id, so legacy typed units are unconstrained
    # and every existing row satisfies it on the day it is built.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_hk_units_ward_label ON housekeeping_units("
    "ward_id, LOWER(TRIM(bed_label))) WHERE ward_id IS NOT NULL AND bed_label IS NOT NULL",
]


def add_housekeeping_unit_columns(conn) -> None:
    """Give housekeeping_units the bed master columns and their indexes.

    Idempotent and guarded: a column already present is skipped, and a
    database with no housekeeping_units table yet is left alone (migration 22
    or the lazy path creates it, and the lazy path then calls this). Shared by
    this migration and services/housekeeping_service.ensure_housekeeping_schema
    so the two paths cannot drift."""
    present = {r[1] for r in conn.execute("PRAGMA table_info(housekeeping_units)").fetchall()}
    if not present:
        return
    for name, decl in HOUSEKEEPING_UNIT_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE housekeeping_units ADD COLUMN {name} {decl}")
    for statement in HOUSEKEEPING_UNIT_INDEXES:
        conn.execute(statement)


def apply(conn) -> None:
    for statement in BED_MASTER_TABLES:
        conn.execute(statement)
    for statement in BED_MASTER_TRIGGERS:
        conn.execute(statement)
    for statement in BED_MASTER_INDEXES:
        conn.execute(statement)
    add_housekeeping_unit_columns(conn)


MIGRATION = Migration(53, "adt_bed_master", apply)
