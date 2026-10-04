"""Migration 59: discharge dispositions the end of stay can write without
doubling a death (adt-8a, W3-B).

nabh_discharge_dispositions (migration 26) is the only record Orb computes
mortality and LAMA from. Until the beds and admissions module (ORB_ADT, default
off) only the records desk wrote it, keyed on (patient_id, occurred_at). The
end of stay now writes it too, and two writers keyed on an instant would give
one death two rows whenever their clocks differ by a second, doubling the
mortality numerator. So the table gains:

  source                'admin' (the records desk, every existing row) or 'adt'
                        (written by the end of stay).
  voided_at, voided_by, void_reason
                        a disposition recorded in error (a stay reopened, adt-8b)
                        is stamped void, never deleted. NABH readers count only
                        rows with voided_at NULL.
  needs_reconciliation  1 when the end of stay found a disposition the records
                        desk had already keyed for the same admission with a
                        different type. The desk's value is kept and the
                        conflict is flagged for a person; nothing is inserted
                        twice.

and an index on admission_id, which is how the end of stay finds the row the
records desk may already have keyed for this stay.

The source vocabulary is held by a BEFORE INSERT and BEFORE UPDATE OF source
trigger pair (a CHECK cannot be added to an existing column).

Additive: ALTER TABLE ADD COLUMN with constant defaults, PRAGMA-guarded, one
statement per execute. Every existing row becomes source 'admin', not void,
not flagged, which is exactly what it was. With ORB_ADT off nothing writes the
new columns.
"""
from migrations import Migration

DISPOSITION_TABLE = "nabh_discharge_dispositions"

#: Columns added, in order. Constant defaults only.
DISPOSITION_COLUMNS: tuple = (
    ("source", "TEXT NOT NULL DEFAULT 'admin'"),
    ("voided_at", "TEXT"),
    ("voided_by", "TEXT"),
    ("void_reason", "TEXT"),
    ("needs_reconciliation", "INTEGER NOT NULL DEFAULT 0"),
)

SOURCES: tuple = ("admin", "adt")
_SOURCE_LIST = ", ".join(f"'{s}'" for s in SOURCES)

DISPOSITION_TRIGGERS: tuple = (
    "CREATE TRIGGER IF NOT EXISTS trg_nabh_disposition_source_insert "
    "BEFORE INSERT ON nabh_discharge_dispositions "
    f"WHEN NEW.source IS NULL OR NEW.source NOT IN ({_SOURCE_LIST}) "
    "BEGIN SELECT RAISE(ABORT, 'unknown nabh_discharge_dispositions.source'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_nabh_disposition_source_update "
    "BEFORE UPDATE OF source ON nabh_discharge_dispositions "
    f"WHEN NEW.source IS NULL OR NEW.source NOT IN ({_SOURCE_LIST}) "
    "BEGIN SELECT RAISE(ABORT, 'unknown nabh_discharge_dispositions.source'); END",
)

DISPOSITION_INDEXES: tuple = (
    "CREATE INDEX IF NOT EXISTS idx_nabh_disposition_admission "
    "ON nabh_discharge_dispositions(admission_id)",
)


def add_disposition_columns(conn) -> None:
    """Idempotent and guarded. A database with no dispositions table (a
    migration 26 that has not run) is left alone."""
    present = {r[1] for r in conn.execute(
        f"PRAGMA table_info({DISPOSITION_TABLE})").fetchall()}
    if not present:
        return
    for name, decl in DISPOSITION_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE {DISPOSITION_TABLE} ADD COLUMN {name} {decl}")
    for statement in DISPOSITION_TRIGGERS:
        conn.execute(statement)
    for statement in DISPOSITION_INDEXES:
        conn.execute(statement)


def apply(conn) -> None:
    add_disposition_columns(conn)


MIGRATION = Migration(59, "adt_discharge_dispositions", apply)
