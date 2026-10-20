"""Orb Hospital OS — Helix Database Layer"""

import logging

from database import db_session

logger = logging.getLogger("orb.helix_db")

HELIX_SCHEMA = """
-- Helix: Medication Master Catalog
CREATE TABLE IF NOT EXISTS helix_medication_master (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    generic_name TEXT,
    form TEXT,
    strength TEXT,
    -- Migration 54 (pharmacy store). No CHECK on any of these: this table is
    -- an FK parent and can never be rebuilt, so the pharmacy services
    -- validate the values instead.
    pack_size_units INTEGER,
    purchase_uom TEXT,
    dispense_unit TEXT,
    hsn_code TEXT,
    manufacturer TEXT,
    gst_rate_bp INTEGER,
    drug_schedule TEXT,
    ndps_category TEXT,
    is_essential_narcotic INTEGER NOT NULL DEFAULT 0,
    storage_condition TEXT,
    reorder_level_units INTEGER,
    reorder_qty_units INTEGER,
    lead_time_days INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    identity_status TEXT NOT NULL DEFAULT 'active',
    updated_by TEXT,
    updated_at TIMESTAMP
);

-- Helix: Inventory Lots
CREATE TABLE IF NOT EXISTS helix_inventory_lots (
    id TEXT PRIMARY KEY,
    medication_id TEXT REFERENCES helix_medication_master(id),
    lot_number TEXT NOT NULL,
    quantity INTEGER DEFAULT 0,
    expiration_date DATE,
    -- Migration 54 (pharmacy store), no CHECK for the same reason. store_id
    -- NULL is a legacy lot; status 'available' is today's meaning.
    store_id INTEGER,
    status TEXT NOT NULL DEFAULT 'available',
    units_per_pack INTEGER,
    mrp_pack_paise INTEGER,
    unit_cost_paise INTEGER,
    mfg_date DATE,
    vendor_id INTEGER,
    receipt_line_ref INTEGER,
    received_at TIMESTAMP,
    created_at TIMESTAMP
);

-- Helix: Patient Medication Orders (extended for full workflow)
CREATE TABLE IF NOT EXISTS helix_patient_med_orders (
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
    -- The ward's own attestation that the drug arrived. Written only by
    -- POST /helix/orders/{id}/receive, which is gated on `helix.receive` —
    -- a permission the dispensing pharmacist does not hold. NULL on a
    -- 'delivered' row means it predates migration 35.
    received_by TEXT REFERENCES users(id),
    received_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Migration 61 (medication-order store): the medication order this supply
    -- request belongs to; NULL on every row written before the store, and on
    -- every row while ORB_MED_ORDER_STORE is off. No CHECK and no REFERENCES
    -- (med_orders is never an FK parent). Its index is created by migration 61
    -- and by the column-guarded step in init_helix_db, never in this script.
    med_order_id TEXT
);

-- Helix: Pharmacy Verifications
CREATE TABLE IF NOT EXISTS helix_pharmacy_verifications (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES helix_patient_med_orders(id),
    pharmacist_id TEXT REFERENCES users(id),
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

-- Helix: Stock Movements (audit trail)
CREATE TABLE IF NOT EXISTS helix_stock_movements (
    id TEXT PRIMARY KEY,
    lot_id TEXT REFERENCES helix_inventory_lots(id),
    medication_id TEXT REFERENCES helix_medication_master(id),
    order_id TEXT REFERENCES helix_patient_med_orders(id),
    quantity_change INTEGER,
    reason TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Migration 54 (pharmacy store), which also owns this ledger's indexes.
    -- seq is the ordering key; occurred_at is canonical UTC.
    movement_type TEXT,
    store_id INTEGER,
    balance_after_units INTEGER,
    unit_cost_paise INTEGER,
    reference_type TEXT,
    reference_id TEXT,
    actor_id TEXT,
    remarks TEXT,
    idempotency_key TEXT,
    occurred_at TEXT,
    seq INTEGER
);

-- Helix: Transition Log (idempotency + audit)
CREATE TABLE IF NOT EXISTS helix_transitions (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES helix_patient_med_orders(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    actor_id TEXT REFERENCES users(id),
    payload TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_helix_orders_patient ON helix_patient_med_orders(patient_id);
CREATE INDEX IF NOT EXISTS idx_helix_orders_status ON helix_patient_med_orders(status);
CREATE INDEX IF NOT EXISTS idx_helix_transitions_order ON helix_transitions(order_id);
CREATE INDEX IF NOT EXISTS idx_helix_transitions_idem ON helix_transitions(idempotency_key);
"""


def init_helix_db():
    """Initialize Helix schema and migrations additively.

    Since migration 49 (migrations_his/m0049_helix_tables_in_runner.py) the
    migration runner creates these tables inside init_db(), so on any database
    that has run migration 49 this is an idempotent fallback. HELIX_SCHEMA must
    stay in step with the migrations that ALTER Helix tables: a column a later
    migration adds goes into HELIX_SCHEMA as well (tests/
    test_fresh_database_init.py compares the two shapes)."""
    with db_session() as conn:
        # The users table is NOT touched here. Its role CHECK belongs to
        # migration 33 (roles.py is the one list), which init_db() runs before
        # main.py calls this. A trial INSERT used to probe the CHECK and, on ANY
        # failure (SQLITE_BUSY during a backup, not only a CHECK), fell back to
        # an executescript RENAME of users: executescript commits first, the
        # 7-column copy then fails on the modern table, and every staff login
        # was left in users_old while boot carried on (this init is non-fatal).

        # Drop old narrow tables and recreate with full schema
        # This is safe because Helix tables are Helix-owned
        _migrate_helix_orders(conn)
        _migrate_stock_movements(conn)
        conn.executescript(HELIX_SCHEMA)
        _index_med_order_link(conn)


def _index_med_order_link(conn):
    """The supply-to-order link's index (migration 61), created only when the
    column is there.

    Deliberately NOT a line of HELIX_SCHEMA: that script runs with
    executescript, and on a legacy helix_patient_med_orders that predates the
    column a CREATE INDEX naming it would abort every statement after it. The
    index definition is migration 61's own, so the two can never disagree."""
    import migrations  # noqa: F401  (collects migrations_his before a file is imported)
    from migrations_his.m0061_med_order_store import (
        HELIX_MED_ORDER_INDEX,
        create_index_sql,
    )

    name, table, columns, where, unique = HELIX_MED_ORDER_INDEX
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if set(columns) <= cols:
        conn.execute(create_index_sql(name, table, columns, where, unique))


def _migrate_helix_orders(conn):
    """Safely migrate helix_patient_med_orders if schema is outdated."""
    try:
        # Nothing to migrate before the table exists. On a fresh database this
        # runs BEFORE the HELIX_SCHEMA executescript a few lines up the call
        # stack creates the table, and PRAGMA table_info returns [] for a table
        # that is not there rather than raising, so every column below reads as
        # "missing" and the rename goes at nothing. That failed, was caught by
        # the handler at the bottom, and logged a first-boot ERROR with a
        # traceback saying the schema was left in an unknown state, which it was
        # not: the table is created correctly moments later. Ask sqlite_master,
        # exactly as migration 35 does for the same reason.
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='helix_patient_med_orders'"
            ).fetchone()
            is None
        ):
            return

        # Check if the new columns exist
        cols = [
            row[1] for row in conn.execute("PRAGMA table_info(helix_patient_med_orders)").fetchall()
        ]
        if "substitute_medication_id" not in cols:
            logger.info("Migrating helix_patient_med_orders for extended workflow")
            # Rename old, create new, copy, drop old
            conn.executescript("""
                ALTER TABLE helix_patient_med_orders RENAME TO _helix_orders_old;
            """)
            conn.executescript(HELIX_SCHEMA)
            # Copy data from old table, filling new columns with defaults
            conn.execute("""
                INSERT OR IGNORE INTO helix_patient_med_orders (id, patient_id, medication_id, status, prescribed_by, dosage, frequency, route, created_at)
                SELECT id, patient_id, medication_id,
                       CASE WHEN status IN ('pending','verified','dispensed','cancelled') THEN
                            CASE status WHEN 'cancelled' THEN 'rejected' ELSE status END
                       ELSE 'pending' END,
                       prescribed_by, dosage, frequency, route, created_at
                FROM _helix_orders_old
            """)
            conn.execute("DROP TABLE IF EXISTS _helix_orders_old")
            conn.commit()
    except Exception as e:
        # Log loudly: a failed migration leaves the schema in an unknown state.
        logger.error("helix_patient_med_orders migration failed: %s", e, exc_info=True)


def _migrate_stock_movements(conn):
    """Safely add missing columns to helix_stock_movements."""
    try:
        # Nothing to migrate before the table exists (W027). PRAGMA table_info
        # returns [] for an absent table, so without this every first boot that
        # reached here before the table existed logged "Migrating
        # helix_stock_movements" and sent two ALTERs at nothing. Same guard as
        # _migrate_helix_orders above and migration 35. Since migration 49 the
        # runner creates the table before this runs, so this is the fallback.
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='helix_stock_movements'"
            ).fetchone()
            is None
        ):
            return
        cols = [
            row[1] for row in conn.execute("PRAGMA table_info(helix_stock_movements)").fetchall()
        ]
        if "medication_id" not in cols:
            logger.info("Migrating helix_stock_movements")
            try:
                conn.execute("ALTER TABLE helix_stock_movements ADD COLUMN medication_id TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE helix_stock_movements ADD COLUMN order_id TEXT")
            except Exception:
                pass
            conn.commit()
    except Exception as e:
        logger.error("helix_stock_movements migration failed: %s", e, exc_info=True)
