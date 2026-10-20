"""Migration 58: where an admission is, and how it ended (adt-3, W3-B).

Until this migration an admission's ward and bed were free text (admissions.
ward, admissions.bed, mirrored on patients.ward and patients.bed), and an end
of stay was only a status flip. The beds and admissions module (ORB_ADT,
default off) needs the place as a record and the end as a fact, so admissions
gains seven columns:

  ward_id          wards.id of the ward the patient is on, when the typed or
                   chosen ward is one the bed master knows. NULL otherwise.
  bed_id           housekeeping_units.id of the master bed the patient lies in.
                   NULL means 'awaiting a bed' (no bed text either) or
                   'unlinked' (bed text that matches no master bed).
  admitted_at      when the patient actually arrived (it may be back-timed).
                   NULL on every row written before ADT; stay start is then
                   created_at.
  presence_status  NULL means on the ward. 'on_leave' or 'absconded' otherwise.
  presence_since   when the current presence began.
  discharge_type   how the stay ended: routine, referred_out, transferred_out,
                   dama, lama, absconded or death. NULL on every stay that
                   ended before ADT, and on an end whose type nobody stated.
  discharged_by    users.id of whoever recorded the end of stay.

There is NO bed_shared_reason column. The design's interim (a deliberate shared
bed with a typed reason) was replaced by ruling ADT-S1 (a): an occupied bed is
refused and an extra bed is a temporary bed with its own label. The bed lock
below therefore has no exemption.

THE BED LOCK. idx_admissions_active_bed is a partial UNIQUE index on bed_id over
active admissions: a second active admission in one master bed is refused by
the database whatever the code above it does. Discharging frees the bed
because the row leaves the index when its status leaves 'active'. Safe to
build on any database: every bed_id is NULL on the day it is created.

VOCABULARIES BY TRIGGER, NOT CHECK. A CHECK can only be widened by rebuilding
the table, and admissions is read by about 22 status filters, so presence_status
and discharge_type are guarded by BEFORE INSERT and BEFORE UPDATE OF <column>
triggers instead. An UPDATE that does not name the column (every existing
status flip) never fires them. Widening a vocabulary later is DROP TRIGGER then
CREATE TRIGGER in a new migration.

A NOTE FOR ANY FUTURE ADMISSIONS REBUILD (kept here, beside the objects it is
about, rather than in database.py). admissions is not an FK parent today, so
nothing rebuilds it. If a later migration ever rebuilds it, through database.
_atomic_table_rebuild or otherwise, the new table must carry the four
vocabulary triggers and the two indexes this file defines (ADMISSION_TRIGGERS
and ADMISSION_INDEXES below): a rebuild that drops them silently removes the
bed lock and lets any spelling into presence_status and discharge_type. Pass
them in post_sql and assert them afterwards.

Additive throughout: ALTER TABLE ADD COLUMN with no default (every column is
NULL on existing rows), guarded by PRAGMA table_info, one statement per execute,
no foreign key. With ORB_ADT off nothing writes any of these columns, so the
rows every existing lane writes are unchanged; SELECT * readers of admissions
carry seven more NULL keys.
"""

from migrations import Migration

#: The columns admissions gains, in order. No constant default is needed: NULL
#: is the honest value on every row written before ADT.
ADMISSION_COLUMNS: tuple = (
    ("ward_id", "TEXT"),
    ("bed_id", "TEXT"),
    ("admitted_at", "TIMESTAMP"),
    ("presence_status", "TEXT"),
    ("presence_since", "TIMESTAMP"),
    ("discharge_type", "TEXT"),
    ("discharged_by", "TEXT"),
)

#: NULL means on the ward.
PRESENCE_STATUSES: tuple = ("on_leave", "absconded")

#: How a stay ended. The same words as nabh_discharge_dispositions, less
#: 'death_within_48h', which is derived from the stay times, never stated.
DISCHARGE_TYPES: tuple = (
    "routine",
    "referred_out",
    "transferred_out",
    "dama",
    "lama",
    "absconded",
    "death",
)

_PRESENCE_LIST = ", ".join(f"'{v}'" for v in PRESENCE_STATUSES)
_DISCHARGE_LIST = ", ".join(f"'{v}'" for v in DISCHARGE_TYPES)

ADMISSION_TRIGGERS: tuple = (
    "CREATE TRIGGER IF NOT EXISTS trg_admissions_presence_insert "
    "BEFORE INSERT ON admissions "
    f"WHEN NEW.presence_status IS NOT NULL AND NEW.presence_status NOT IN ({_PRESENCE_LIST}) "
    "BEGIN SELECT RAISE(ABORT, 'unknown admissions.presence_status'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_admissions_presence_update "
    "BEFORE UPDATE OF presence_status ON admissions "
    f"WHEN NEW.presence_status IS NOT NULL AND NEW.presence_status NOT IN ({_PRESENCE_LIST}) "
    "BEGIN SELECT RAISE(ABORT, 'unknown admissions.presence_status'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_admissions_discharge_type_insert "
    "BEFORE INSERT ON admissions "
    f"WHEN NEW.discharge_type IS NOT NULL AND NEW.discharge_type NOT IN ({_DISCHARGE_LIST}) "
    "BEGIN SELECT RAISE(ABORT, 'unknown admissions.discharge_type'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_admissions_discharge_type_update "
    "BEFORE UPDATE OF discharge_type ON admissions "
    f"WHEN NEW.discharge_type IS NOT NULL AND NEW.discharge_type NOT IN ({_DISCHARGE_LIST}) "
    "BEGIN SELECT RAISE(ABORT, 'unknown admissions.discharge_type'); END",
)
TRIGGER_NAMES: frozenset = frozenset(
    {
        "trg_admissions_presence_insert",
        "trg_admissions_presence_update",
        "trg_admissions_discharge_type_insert",
        "trg_admissions_discharge_type_update",
    }
)

ADMISSION_INDEXES: tuple = (
    # The bed lock (ruling ADT-S1 (a)): one active admission per master bed.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_admissions_active_bed ON admissions(bed_id) "
    "WHERE status = 'active' AND bed_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_admissions_ward_status ON admissions(ward_id, status)",
)
INDEX_NAMES: frozenset = frozenset({"idx_admissions_active_bed", "idx_admissions_ward_status"})


def add_admission_location_columns(conn) -> None:
    """The seven columns, the four triggers and the two indexes. Idempotent:
    a column already present is skipped, and the triggers and indexes are
    IF NOT EXISTS. A database with no admissions table is left alone."""
    present = {r[1] for r in conn.execute("PRAGMA table_info(admissions)").fetchall()}
    if not present:
        return
    for name, decl in ADMISSION_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE admissions ADD COLUMN {name} {decl}")
    for statement in ADMISSION_TRIGGERS:
        conn.execute(statement)
    for statement in ADMISSION_INDEXES:
        conn.execute(statement)


def apply(conn) -> None:
    add_admission_location_columns(conn)


MIGRATION = Migration(58, "adt_admission_location", apply)
