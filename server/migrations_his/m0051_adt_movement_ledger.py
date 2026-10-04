"""Migration 51: the patient movement ledger as a numbered migration (adt-2).

patient_movements was created lazily by services/frontdesk_service.
ensure_frontdesk_schema, with a CHECK that allowed only 'transfer', 'admission'
and 'discharge'. The bed and admission work (ADT epic) writes more kinds of
movement: a bed assigned, a bed change or swap, leave, absconding, the link of
an old admission to a bed, a correction. A CHECK can only be widened by
rebuilding the table, so the vocabulary moves to a BEFORE INSERT trigger (and a
BEFORE UPDATE OF movement_type twin), which a later migration widens with DROP
TRIGGER and CREATE TRIGGER.

Three cases, decided by the live table:
  * absent: create it in the new shape;
  * the old shape (the lazy CREATE, or any table missing a new column): rebuild
    it here, inside the runner's transaction. patient_movements is not an FK
    parent (nothing references it), so this is the in-transaction rebuild of
    migration 3 (rename aside, create, copy, drop), not the FK-parent dance of
    database._atomic_table_rebuild. The rowid is copied EXPLICITLY: the front
    desk's history and the bed-day billing break ties on rowid, and a copy that
    renumbered rows would silently reorder two movements recorded in the same
    instant;
  * the new shape: nothing to rebuild; indexes and triggers are ensured.

The new columns are NULL on every copied row and on every row the front desk's
transfer writes. Nothing reads them yet; the ADT writer (adt_service.
record_movement_on, a later increment) fills them. Billing reprices a day only
from movement types that change where the patient lies (billing_service.
_admission_moves), so admission, leave and link rows never reprice a day.

Flag-independent: runs on every install, Ward Record and demo databases
included, before anything writes the new movement types.
"""
import re

from migrations import Migration

LEDGER_TABLE = "patient_movements"

#: The columns the lazy CREATE defined, in order. The front desk's API projects
#: movements onto exactly these, so the new columns change no existing screen.
LEGACY_COLUMNS: tuple = (
    "id", "patient_id", "admission_id", "movement_type",
    "from_department", "from_ward", "from_bed",
    "to_department", "to_ward", "to_bed",
    "reason", "moved_at", "moved_by", "moved_by_name", "notes", "idempotency_key",
)

#: The columns this migration adds, in order.
ADDED_COLUMNS: tuple = (
    "from_ward_id", "from_bed_id", "to_ward_id", "to_bed_id", "end_type",
    "discharge_summary_id", "corrects_movement_id", "recorded_at", "detail_json",
)

CANONICAL_COLUMNS: tuple = LEGACY_COLUMNS + ADDED_COLUMNS

#: Every movement type the ledger accepts. Widened only by a new migration that
#: drops and recreates the two vocabulary triggers.
MOVEMENT_TYPES: tuple = (
    "admission", "bed_assigned", "transfer", "bed_change", "bed_swap",
    "leave_start", "leave_end", "absconded", "absconded_return", "discharge",
    "location_linked", "correction",
)

# The new shape. admission_id is the admission the patient STAYS on: a transfer
# never opens a new one, because clinical continuity (orders, eMAR, vitals, the
# case room) hangs off that id. movement_type keeps its NOT NULL and its
# 'transfer' default;
# its vocabulary is the trigger pair below, not a CHECK. moved_at is when the
# movement happened (it may be back-timed); recorded_at is the server clock
# when it was written. ward and bed ids are wards.id and housekeeping_units.id
# (both TEXT); discharge_summary_id is discharge_summaries.id (INTEGER).
LEDGER_DDL = """CREATE TABLE IF NOT EXISTS patient_movements (
        id TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        movement_type TEXT NOT NULL DEFAULT 'transfer',
        from_department TEXT,
        from_ward TEXT,
        from_bed TEXT,
        to_department TEXT,
        to_ward TEXT,
        to_bed TEXT,
        reason TEXT NOT NULL,
        moved_at TIMESTAMP NOT NULL,
        moved_by TEXT,
        moved_by_name TEXT,
        notes TEXT,
        idempotency_key TEXT,
        from_ward_id TEXT,
        from_bed_id TEXT,
        to_ward_id TEXT,
        to_bed_id TEXT,
        end_type TEXT,
        discharge_summary_id INTEGER,
        corrects_movement_id TEXT,
        recorded_at TIMESTAMP,
        detail_json TEXT
    )"""

#: The three indexes the lazy path always created. They need only legacy
#: columns, so the lazy fallback may create them on an old-shape table too.
LEGACY_INDEXES: tuple = (
    "CREATE INDEX IF NOT EXISTS idx_movements_patient ON patient_movements("
    "patient_id, moved_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_movements_admission ON patient_movements("
    "admission_id, moved_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_movements_idempotency ON patient_movements("
    "idempotency_key) WHERE idempotency_key IS NOT NULL",
)

#: The three this migration adds: what came into and out of a bed, and every
#: movement of one type in time order (the census reads these).
NEW_INDEXES: tuple = (
    "CREATE INDEX IF NOT EXISTS idx_movements_to_bed ON patient_movements("
    "to_bed_id, moved_at)",
    "CREATE INDEX IF NOT EXISTS idx_movements_from_bed ON patient_movements("
    "from_bed_id, moved_at)",
    "CREATE INDEX IF NOT EXISTS idx_movements_type ON patient_movements("
    "movement_type, moved_at)",
)

INDEXES: tuple = LEGACY_INDEXES + NEW_INDEXES
INDEX_NAMES: frozenset = frozenset({
    "idx_movements_patient", "idx_movements_admission", "idx_movements_idempotency",
    "idx_movements_to_bed", "idx_movements_from_bed", "idx_movements_type",
})

#: The lazy CREATE's CHECK on movement_type, which marks the old shape.
_OLD_TYPE_CHECK = re.compile(r"CHECK\s*\(\s*movement_type\s+IN\b", re.I)

_TYPE_LIST = ", ".join(f"'{t}'" for t in MOVEMENT_TYPES)

TRIGGERS: tuple = (
    "CREATE TRIGGER IF NOT EXISTS trg_patient_movements_type_insert "
    "BEFORE INSERT ON patient_movements "
    f"WHEN NEW.movement_type IS NULL OR NEW.movement_type NOT IN ({_TYPE_LIST}) "
    "BEGIN SELECT RAISE(ABORT, 'unknown patient_movements.movement_type'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_patient_movements_type_update "
    "BEFORE UPDATE OF movement_type ON patient_movements "
    f"WHEN NEW.movement_type IS NULL OR NEW.movement_type NOT IN ({_TYPE_LIST}) "
    "BEGIN SELECT RAISE(ABORT, 'unknown patient_movements.movement_type'); END",
)
TRIGGER_NAMES: frozenset = frozenset({
    "trg_patient_movements_type_insert", "trg_patient_movements_type_update",
})


def _columns(conn) -> list:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{LEDGER_TABLE}")').fetchall()]


def is_new_shape(conn) -> bool:
    """True when the ledger exists with every column of the new shape and no
    CHECK on movement_type."""
    cols = set(_columns(conn))
    if not cols or not set(CANONICAL_COLUMNS) <= cols:
        return False
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (LEDGER_TABLE,)
    ).fetchone()
    ddl = (row[0] or "") if row else ""
    return _OLD_TYPE_CHECK.search(ddl) is None


def _ensure_indexes_and_triggers(conn) -> None:
    for statement in INDEXES:
        conn.execute(statement)
    for statement in TRIGGERS:
        conn.execute(statement)


def _rebuild_old_shape(conn) -> None:
    """Old shape to new, inside the caller's open transaction. Refuses, before
    touching anything, a live column, index or trigger it does not know how to
    carry across, rather than drop it silently."""
    if not getattr(conn, "in_transaction", True):
        raise RuntimeError(
            "patient_movements rebuild needs an open transaction (the migration "
            "runner holds one); refusing to rebuild in autocommit"
        )
    old_cols = _columns(conn)
    unknown = [c for c in old_cols if c not in CANONICAL_COLUMNS]
    if unknown:
        raise RuntimeError(f"patient_movements rebuild would drop live column(s) {unknown}")
    missing_legacy = [c for c in ("id", "patient_id", "reason", "moved_at") if c not in old_cols]
    if missing_legacy:
        raise RuntimeError(
            f"patient_movements has no {missing_legacy}; not a ledger this migration knows"
        )
    assoc = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE tbl_name=? "
        "AND type IN ('index', 'trigger') AND name NOT LIKE 'sqlite_autoindex%'",
        (LEDGER_TABLE,),
    ).fetchall()
    strangers = [(r[0], r[1]) for r in assoc
                 if r[0] not in INDEX_NAMES and r[0] not in TRIGGER_NAMES]
    if strangers:
        raise RuntimeError(
            "patient_movements rebuild would drop associated objects it does not "
            f"know how to preserve: {strangers}"
        )
    # Nothing else may name the table (a view, or a trigger on another table):
    # the rename below would rewrite or break it.
    dependants = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('view', 'trigger') "
        "AND tbl_name != ? AND sql LIKE '%patient_movements%'",
        (LEDGER_TABLE,),
    ).fetchall()
    if dependants:
        raise RuntimeError(
            "patient_movements rebuild: other schema objects name the table "
            f"{[(r[0], r[1]) for r in dependants]}; refusing to rename it"
        )

    shared = ", ".join(c for c in CANONICAL_COLUMNS if c in old_cols)
    before = conn.execute("SELECT COUNT(*) FROM patient_movements").fetchone()[0]
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'patient_movements_aside'"
    ).fetchone():
        raise RuntimeError(
            "patient_movements_aside already exists; it may hold ledger rows from an "
            "interrupted rebuild. Inspect it before booting again; nothing was changed."
        )
    conn.execute("ALTER TABLE patient_movements RENAME TO patient_movements_aside")
    conn.execute(LEDGER_DDL)
    conn.execute(
        f"INSERT INTO patient_movements (rowid, {shared}) "
        f"SELECT rowid, {shared} FROM patient_movements_aside"
    )
    after = conn.execute("SELECT COUNT(*) FROM patient_movements").fetchone()[0]
    if after != before:
        raise RuntimeError(
            f"patient_movements rebuild aborted: copied {after} of {before} rows"
        )
    # The old indexes and any vocabulary triggers go with the aside table.
    conn.execute("DROP TABLE patient_movements_aside")


def ensure_ledger_on(conn, *, rebuild: bool = False) -> None:
    """The ledger in its migration 51 shape where that is safe.

    Absent: created in the new shape. New shape: indexes and triggers ensured.
    Old shape: rebuilt only when ``rebuild`` is True (the migration runner,
    which holds a transaction); the lazy fallback in frontdesk_service passes
    False and gets only the three legacy indexes, leaving the rebuild to this
    migration at the next boot."""
    if not _columns(conn):
        conn.execute(LEDGER_DDL)
    elif not is_new_shape(conn):
        if not rebuild:
            for statement in LEGACY_INDEXES:
                conn.execute(statement)
            return
        _rebuild_old_shape(conn)
    _ensure_indexes_and_triggers(conn)


def apply(conn) -> None:
    ensure_ledger_on(conn, rebuild=True)


MIGRATION = Migration(51, "adt_movement_ledger", apply)
