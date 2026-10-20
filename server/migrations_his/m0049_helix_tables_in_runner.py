"""Migration 49: create the six Helix pharmacy tables inside the migration runner.

Until now they were created only by services/helix_db.init_helix_db(), which
main.py calls AFTER init_db() has run every migration. So no migration could
ALTER a Helix table and be sure it existed: on a fresh appliance the table
appeared only later, in the shape HELIX_SCHEMA happened to have (migration 35
returns early for exactly that reason). From this migration on, the tables
exist by the time later migrations run, on fresh and existing databases alike,
and the pharmacy store work can add columns to them in numbered migrations.

A FROZEN copy of today's DDL (3 October 2026), not an import of
helix_db.HELIX_SCHEMA. A shipped migration must create the same tables forever;
HELIX_SCHEMA will keep changing in step with later migrations and stays as
init_helix_db's idempotent fallback.

One statement per conn.execute, never executescript: executescript COMMITs the
runner's transaction first (see migrations.run_pending_migrations).

Idempotent and safe on old databases: every table is CREATE TABLE IF NOT
EXISTS, so an existing Helix table is never touched (an old narrow
helix_patient_med_orders is still widened by init_helix_db, as before). Each
index is created only when its table has the indexed column, so an unusual
legacy table cannot make boot fail here.
"""

from migrations import Migration

# The six tables, exactly as services/helix_db.HELIX_SCHEMA wrote them on
# 3 October 2026, ward-receipt columns (migration 35) included.
_TABLES = (
    """CREATE TABLE IF NOT EXISTS helix_medication_master (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    generic_name TEXT,
    form TEXT,
    strength TEXT
)""",
    """CREATE TABLE IF NOT EXISTS helix_inventory_lots (
    id TEXT PRIMARY KEY,
    medication_id TEXT REFERENCES helix_medication_master(id),
    lot_number TEXT NOT NULL,
    quantity INTEGER DEFAULT 0,
    expiration_date DATE
)""",
    """CREATE TABLE IF NOT EXISTS helix_patient_med_orders (
    id TEXT PRIMARY KEY,
    patient_id TEXT REFERENCES patients(id),
    medication_id TEXT REFERENCES helix_medication_master(id),
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'reviewing', 'verified', 'rejected', 'substitute_suggested', 'scheduled', 'dispensed', 'delivered')),
    prescribed_by TEXT REFERENCES users(id),
    dosage TEXT,
    frequency TEXT,
    route TEXT,
    substitute_medication_id TEXT REFERENCES helix_medication_master(id),
    pharmacist_notes TEXT,
    reviewed_by TEXT REFERENCES users(id),
    reviewed_at TIMESTAMP,
    scheduled_at TIMESTAMP,
    last_transition_id TEXT,
    received_by TEXT REFERENCES users(id),
    received_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",
    """CREATE TABLE IF NOT EXISTS helix_pharmacy_verifications (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES helix_patient_med_orders(id),
    pharmacist_id TEXT REFERENCES users(id),
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
)""",
    """CREATE TABLE IF NOT EXISTS helix_stock_movements (
    id TEXT PRIMARY KEY,
    lot_id TEXT REFERENCES helix_inventory_lots(id),
    medication_id TEXT REFERENCES helix_medication_master(id),
    order_id TEXT REFERENCES helix_patient_med_orders(id),
    quantity_change INTEGER,
    reason TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",
    """CREATE TABLE IF NOT EXISTS helix_transitions (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES helix_patient_med_orders(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    actor_id TEXT REFERENCES users(id),
    payload TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",
)

# (index name, table, column): the four HELIX_SCHEMA indexes.
_INDEXES = (
    ("idx_helix_orders_patient", "helix_patient_med_orders", "patient_id"),
    ("idx_helix_orders_status", "helix_patient_med_orders", "status"),
    ("idx_helix_transitions_order", "helix_transitions", "order_id"),
    ("idx_helix_transitions_idem", "helix_transitions", "idempotency_key"),
)

TABLE_NAMES = (
    "helix_medication_master",
    "helix_inventory_lots",
    "helix_patient_med_orders",
    "helix_pharmacy_verifications",
    "helix_stock_movements",
    "helix_transitions",
)


def apply(conn) -> None:
    for ddl in _TABLES:
        conn.execute(ddl)
    for name, table, column in _INDEXES:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in cols:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({column})")


MIGRATION = Migration(49, "helix_tables_in_runner", apply)
