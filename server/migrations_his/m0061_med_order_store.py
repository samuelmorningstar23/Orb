"""Migration 61: the medication-order store (medorders-1, docs/his_design/medorders.json).

Decision A1 (Samuel, 3 October 2026): one authoritative medication order,
med_orders, with its ledger, med_order_events. The four tables that hold
medication facts today stay and each keeps one job (a strangler, nothing is
renamed or rebuilt):

  medications               the ward projection the chart, the eMAR and about
                            twenty readers keep reading
  helix_patient_med_orders  a supply request, many per order
  orders (medication)       the CPOE view of a draft
  medication_administrations  the MAR

Nothing writes these tables until a site turns ORB_MED_ORDER_STORE on (and the
lifecycle, ORB_MED_ORDER_LIFECYCLE, on top of it). Every column added to an
existing table is NULL on every existing row, so with both switches off every
screen and every bedside check behaves exactly as before.

1. med_orders. One row per medication order. No FOREIGN KEY is declared, so it
   never becomes an FK parent and a later CHECK widening is an ordinary
   in-transaction rebuild. The CHECKs hold the state machine's invariants
   (services/medorders/core.py is the only writer):
     * status is one of the eight states; held_from is set exactly while the
       order is held, and only to the state a resume returns to;
     * projection_status (what the ward row shows, 'active' or 'prn') is NULL
       whenever the order is not live (a draft, or a finished order);
     * a verified order names its verifier.
   source, kind and the other vocabularies are validated in the service, so a
   later lane can add a value without a rebuild.

2. med_order_events. The ledger, append-only by design: an UPDATE is refused by
   trigger. A DELETE is deliberately NOT refused (no DELETE-abort trigger):
     * a patient's erasure must remove drug names, doses and reasons with the
       rest of the medication record (ruling MO-R13 (a)); a DELETE-abort table
       would have to be kept after erasure, or would break the erase plan
       (tests/test_erase_plan_retained.py);
     * the development reseed keeps every DELETE-abort table, so every reseed
       would leave orphan events on reseeded patient ids;
     * medication_administrations is append-only by design with no trigger,
       the same precedent.
   Tamper evidence comes from the hash-chained clinical_access audit row the
   service writes beside every event.

3. medications gains med_order_id (one ward row per order: a unique partial
   index), held_at and hold_reason. held_at is the marker every reader that
   drives an administration checks; it is NULL on every existing row.

4. helix_patient_med_orders gains med_order_id, guarded on the table existing
   (the migration 35 pattern). The column is also in helix_db.HELIX_SCHEMA's
   CREATE TABLE. Its index is created here and in a column-guarded step of
   init_helix_db, never inside the HELIX_SCHEMA script: on a legacy table
   without the column, an unguarded CREATE INDEX there would abort the rest of
   that script.

The rules every migration here follows: one statement per conn.execute (never
executescript, which commits the runner's transaction), IF NOT EXISTS and
PRAGMA guards so a second run changes nothing, ALTER TABLE ADD COLUMN with no
default, no FOREIGN KEY, no CHECK on a column added to a Helix table, and no
CURRENT_TIMESTAMP default (the writer stamps every instant in the canonical
spelling from services/clinical_time).
"""

from migrations import Migration

STATUSES = (
    "draft",
    "prescribed",
    "verified",
    "held",
    "stopped",
    "completed",
    "cancelled",
    "rejected",
)

MED_ORDERS_DDL = """CREATE TABLE IF NOT EXISTS med_orders (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    admission_id TEXT,
    encounter_type TEXT NOT NULL DEFAULT 'ip'
        CHECK (encounter_type IN ('ip', 'op', 'discharge')),
    status TEXT NOT NULL
        CHECK (status IN ('draft', 'prescribed', 'verified', 'held', 'stopped',
                          'completed', 'cancelled', 'rejected')),
    held_from TEXT CHECK (held_from IS NULL OR held_from IN ('prescribed', 'verified')),
    version INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    needs_verification INTEGER NOT NULL DEFAULT 0 CHECK (needs_verification IN (0, 1)),
    drug_name TEXT NOT NULL,
    formulary_id TEXT,
    dose TEXT,
    route TEXT,
    frequency TEXT,
    schedule_kind TEXT,
    is_prn INTEGER NOT NULL DEFAULT 0 CHECK (is_prn IN (0, 1)),
    prn_indication TEXT,
    priority TEXT CHECK (priority IS NULL OR priority IN ('routine', 'urgent', 'stat')),
    start_at TEXT,
    end_at TEXT,
    duration_text TEXT,
    indication TEXT,
    notes TEXT,
    supply_source TEXT
        CHECK (supply_source IS NULL OR supply_source IN ('pharmacy', 'ward_stock', 'patient_own')),
    entered_by TEXT NOT NULL,
    entered_by_role TEXT,
    prescriber_id TEXT,
    prescribed_at TEXT,
    verified_by TEXT,
    verified_at TEXT,
    status_reason TEXT,
    status_changed_by TEXT,
    status_changed_at TEXT,
    last_safety_json TEXT,
    last_screened_at TEXT,
    screen_fingerprint TEXT,
    projection_status TEXT CHECK (projection_status IS NULL OR projection_status IN ('active', 'prn')),
    legacy_medication_id INTEGER,
    cpoe_order_id TEXT,
    order_set_application_id INTEGER,
    sage_action_id TEXT,
    sage_handoff_id TEXT,
    scribe_note_id TEXT,
    idempotency_key TEXT,
    facility_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((status = 'held') = (held_from IS NOT NULL)),
    CHECK (projection_status IS NULL OR status IN ('prescribed', 'verified', 'held')),
    CHECK (status != 'verified' OR verified_by IS NOT NULL)
)"""

MED_ORDER_EVENTS_DDL = """CREATE TABLE IF NOT EXISTS med_order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    med_order_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor_id TEXT,
    actor_role TEXT,
    entry_point TEXT,
    reason TEXT,
    ref_type TEXT,
    ref_id TEXT,
    payload_json TEXT,
    idempotency_key TEXT,
    created_at TEXT NOT NULL
)"""

TABLE_NAMES = ("med_orders", "med_order_events")

#: Columns added to existing tables: (table, column, SQL type). No defaults, so
#: every existing row reads NULL and every existing reader is unchanged.
ADDED_COLUMNS = (
    ("medications", "med_order_id", "TEXT"),
    ("medications", "held_at", "TEXT"),
    ("medications", "hold_reason", "TEXT"),
    ("helix_patient_med_orders", "med_order_id", "TEXT"),
)

#: The Helix supply link's index. services/helix_db.init_helix_db creates the
#: same index, guarded on the column, for a database whose Helix tables were
#: made after this migration ran (never inside its HELIX_SCHEMA script).
HELIX_MED_ORDER_INDEX = (
    "idx_helix_orders_med_order",
    "helix_patient_med_orders",
    ("med_order_id",),
    "med_order_id IS NOT NULL",
    False,
)

# (index name, table, columns, WHERE clause or "", unique)
_INDEXES = (
    ("idx_med_orders_patient_status", "med_orders", ("patient_id", "status"), "", False),
    (
        "idx_med_orders_status_verification",
        "med_orders",
        ("status", "needs_verification"),
        "",
        False,
    ),
    ("idx_med_orders_cpoe", "med_orders", ("cpoe_order_id",), "cpoe_order_id IS NOT NULL", False),
    (
        "idx_med_orders_admission",
        "med_orders",
        ("admission_id",),
        "admission_id IS NOT NULL",
        False,
    ),
    (
        "ux_med_orders_legacy_medication",
        "med_orders",
        ("legacy_medication_id",),
        "legacy_medication_id IS NOT NULL",
        True,
    ),
    (
        "ux_med_orders_idempotency",
        "med_orders",
        ("idempotency_key",),
        "idempotency_key IS NOT NULL",
        True,
    ),
    (
        "idx_med_order_events_order",
        "med_order_events",
        ("med_order_id", "created_at", "id"),
        "",
        False,
    ),
    ("idx_med_order_events_patient", "med_order_events", ("patient_id", "created_at"), "", False),
    (
        "ux_med_order_events_idempotency",
        "med_order_events",
        ("idempotency_key",),
        "idempotency_key IS NOT NULL",
        True,
    ),
    (
        "ux_medications_med_order",
        "medications",
        ("med_order_id",),
        "med_order_id IS NOT NULL",
        True,
    ),
    HELIX_MED_ORDER_INDEX,
)

NO_UPDATE_TRIGGER = "trg_med_order_events_no_update"


def _columns(conn, table: str) -> set:
    """Column names of an existing table; empty for a table that is not there."""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def create_index_sql(name, table, columns, where, unique) -> str:
    kind = "UNIQUE INDEX" if unique else "INDEX"
    clause = f" WHERE {where}" if where else ""
    return f"CREATE {kind} IF NOT EXISTS {name} ON {table}({', '.join(columns)}){clause}"


def apply(conn) -> None:
    conn.execute(MED_ORDERS_DDL)
    conn.execute(MED_ORDER_EVENTS_DDL)
    for table, column, sql_type in ADDED_COLUMNS:
        existing = _columns(conn, table)
        # medications is a baseline table and the Helix tables come from
        # migration 49, but an unusual legacy database may lack either: then
        # there is nothing to link and nothing to fail.
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    for name, table, columns, where, unique in _INDEXES:
        if set(columns) <= _columns(conn, table):
            conn.execute(create_index_sql(name, table, columns, where, unique))
    # The ledger is append-only: a recorded event is never rewritten.
    conn.execute(
        f"CREATE TRIGGER IF NOT EXISTS {NO_UPDATE_TRIGGER} "
        "BEFORE UPDATE ON med_order_events "
        "BEGIN SELECT RAISE(ABORT, 'med_order_events is append-only: record a new event instead'); END"
    )


MIGRATION = Migration(61, "med_order_store", apply)
