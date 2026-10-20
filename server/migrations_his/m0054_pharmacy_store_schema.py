"""Migration 54: the pharmacy store schema (grn-3, docs/his_design/grn.json section C).

Additive only: ALTER TABLE ADD COLUMN, new tables, indexes and two triggers. No
table is rebuilt and no FOREIGN KEY is declared (the migration 20 rule). Nothing
here changes what any existing screen or route does: every new column has a
constant default or is nullable, every new table starts empty, and the module
that writes them (ORB_PHARMACY_STORE) is off by default. The one data change is
the drug-ledger backfill at the bottom, which only ADDS rows and fills new
columns.

RULE 1. NO CHECK ON A COLUMN ADDED TO A HELIX TABLE.
helix_medication_master and helix_inventory_lots are the declared FK parents of
lots, movements and orders, so neither can ever be rebuilt inside the migration
transaction (see migrations._rebuild_users_role_check), and a CHECK added by
ALTER would be frozen for good. helix_stock_movements is a child, but its type
list will grow (op_sale, consumption). So lot status, drug schedule, NDPS
category, storage condition, identity status and movement type are validated
by the pharmacy services against their own constants. CHECKs appear only on the
new pharmacy_* and ndps_* tables and on columns added to the migration 20
tables, none of which has FK children.

RULE 2. NAMES THE RESEED SWEEP CANNOT MISREAD.
scripts/reset_db_and_seed.patient_scoped_tables reads an undeclared
`<stem>_id` column as a link to `<stem>s`. So a patient-order link is
`helix_order_id`, a drug is `helix_medication_id`, and a link to a procurement
document (whose tables a reseed wipes) ends in `_ref`: receipt_line_ref,
source_indent_ref, source_indent_line_ref, helix_lot_ref, source_receipt_ref.
tests/test_reseed_orphans.py asserts the wipe or keep membership of every new
table by name.

THE NDPS REGISTER is append-only by trigger. trg_ndps_register_no_update refuses
every UPDATE; trg_ndps_register_no_delete refuses a DELETE unless the row's
lane is 'non_production'. ndps_service writes the lane from the deployment
profile ('prod' writes 'live'), so a development reseed can clear development
rows (reset_db_and_seed._CONDITIONAL_DELETE_PREDICATES) and a live row can
never be deleted. Live rows are kept through a patient's erasure by
consent_service.ERASURE_RETAINED_TABLES. Nothing writes the register until the
NDPS switch is on, and that switch cannot turn on until ruling GRN-RS7 sets a
failure mode.

THE LEDGER BACKFILL. Until now helix_stock_movements held untyped dispense rows
and nothing tied a lot's quantity to its movements (a lot seeded or typed in
had no movement at all). The backfill makes SUM(quantity_change) per lot equal
the lot's quantity, which is the invariant pharmacy_stock_service.reconcile()
checks from now on:
  * occurred_at: the legacy `timestamp` in the canonical UTC spelling
    (services/clinical_time.to_utc_iso, naive read as UTC because
    CURRENT_TIMESTAMP is UTC). An unreadable value stays NULL and is counted.
  * a legacy 'dispense' row becomes movement_type 'dispense', reference_type
    'helix_order', reference_id = its order. Any other legacy reason is left
    untyped for reconcile() to report, never guessed.
  * one 'opening' row per lot whose quantity differs from its movements,
    dated one second before the lot's first movement (or now, when it has
    none), remark "Balance brought forward when the stock ledger began". If
    the lot's history would otherwise dip below zero part way through, the
    opening is raised to keep every running balance at or above zero and one
    closing 'adjustment' row, dated after the last movement, brings the total
    back to the shelf quantity. Real histories (dispenses only) never need it.
  * seq: the ledger's ordering key (rowid on this TEXT-keyed table can change
    on VACUUM). Opening rows first, then the legacy rows by (occurred_at,
    rowid), then any closing adjustment; balance_after_units is the running
    sum per lot in seq order.
Lots themselves are never modified.

Idempotent: every column, table, index and trigger is guarded or IF NOT EXISTS,
the series rows are INSERT OR IGNORE, and a second backfill finds every lot
balanced and every row sequenced. One statement per conn.execute, never
executescript (which would commit the runner's transaction). A table that does
not exist is skipped rather than failed: in the runner every table this
touches exists (migration 20 and 49 run first), and the skip keeps the
migration usable on the bare Helix database tests/test_migrations.py builds.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from migrations import Migration

logger = logging.getLogger("orb.migrations")

# ── Columns added to the Helix tables (RULE 1: no CHECK) ──────────────────────
_HELIX_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # The drug master's commercial and regulatory fields. NULL means "not
    # entered": an unset GST rate is 'Not configured', and an unclassified
    # schedule or NDPS category is never read as 'none'.
    ("helix_medication_master", "pack_size_units", "INTEGER"),
    ("helix_medication_master", "purchase_uom", "TEXT"),
    ("helix_medication_master", "dispense_unit", "TEXT"),
    ("helix_medication_master", "hsn_code", "TEXT"),
    ("helix_medication_master", "manufacturer", "TEXT"),
    ("helix_medication_master", "gst_rate_bp", "INTEGER"),
    ("helix_medication_master", "drug_schedule", "TEXT"),
    ("helix_medication_master", "ndps_category", "TEXT"),
    ("helix_medication_master", "is_essential_narcotic", "INTEGER NOT NULL DEFAULT 0"),
    ("helix_medication_master", "storage_condition", "TEXT"),
    ("helix_medication_master", "reorder_level_units", "INTEGER"),
    ("helix_medication_master", "reorder_qty_units", "INTEGER"),
    ("helix_medication_master", "lead_time_days", "INTEGER"),
    ("helix_medication_master", "is_active", "INTEGER NOT NULL DEFAULT 1"),
    ("helix_medication_master", "identity_status", "TEXT NOT NULL DEFAULT 'active'"),
    ("helix_medication_master", "updated_by", "TEXT"),
    ("helix_medication_master", "updated_at", "TIMESTAMP"),
    # Lots gain a location, a status and their purchase facts. store_id NULL is
    # a legacy lot, held before the dispensing store adopted it; status
    # 'available' is today's meaning, so every existing lot reads exactly as
    # it did. created_at is nullable because ALTER cannot default to
    # CURRENT_TIMESTAMP; the stock service always sets it.
    ("helix_inventory_lots", "store_id", "INTEGER"),
    ("helix_inventory_lots", "status", "TEXT NOT NULL DEFAULT 'available'"),
    ("helix_inventory_lots", "units_per_pack", "INTEGER"),
    ("helix_inventory_lots", "mrp_pack_paise", "INTEGER"),
    ("helix_inventory_lots", "unit_cost_paise", "INTEGER"),
    ("helix_inventory_lots", "mfg_date", "DATE"),
    ("helix_inventory_lots", "vendor_id", "INTEGER"),
    ("helix_inventory_lots", "receipt_line_ref", "INTEGER"),
    ("helix_inventory_lots", "received_at", "TIMESTAMP"),
    ("helix_inventory_lots", "created_at", "TIMESTAMP"),
    # The drug ledger becomes a ledger: typed, located, attributed, ordered.
    ("helix_stock_movements", "movement_type", "TEXT"),
    ("helix_stock_movements", "store_id", "INTEGER"),
    ("helix_stock_movements", "balance_after_units", "INTEGER"),
    ("helix_stock_movements", "unit_cost_paise", "INTEGER"),
    ("helix_stock_movements", "reference_type", "TEXT"),
    ("helix_stock_movements", "reference_id", "TEXT"),
    ("helix_stock_movements", "actor_id", "TEXT"),
    ("helix_stock_movements", "remarks", "TEXT"),
    ("helix_stock_movements", "idempotency_key", "TEXT"),
    ("helix_stock_movements", "occurred_at", "TEXT"),
    ("helix_stock_movements", "seq", "INTEGER"),
)

# ── Columns added to the migration 20 tables (no FK children, CHECKs allowed) ──
_PROCUREMENT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # One set of document tables, two domains. Every existing row is a stores
    # document, and the stores routes see only the stores domain
    # (procurement_service domain guards).
    (
        "purchase_orders",
        "domain",
        "TEXT NOT NULL DEFAULT 'stores' CHECK (domain IN ('stores', 'pharmacy'))",
    ),
    (
        "goods_receipts",
        "domain",
        "TEXT NOT NULL DEFAULT 'stores' CHECK (domain IN ('stores', 'pharmacy'))",
    ),
    (
        "indents",
        "domain",
        "TEXT NOT NULL DEFAULT 'stores' CHECK (domain IN ('stores', 'pharmacy'))",
    ),
    # A purchase indent asks for something to be bought (every stores indent
    # today); an issue indent asks another store to hand stock over.
    (
        "indents",
        "indent_kind",
        "TEXT NOT NULL DEFAULT 'purchase' CHECK (indent_kind IN ('purchase', 'issue'))",
    ),
    ("indents", "issuing_store_id", "INTEGER"),
    ("purchase_order_lines", "helix_medication_id", "TEXT"),
    (
        "purchase_order_lines",
        "units_per_pack",
        "INTEGER CHECK (units_per_pack IS NULL OR units_per_pack > 0)",
    ),
    ("goods_receipt_lines", "helix_medication_id", "TEXT"),
    (
        "goods_receipt_lines",
        "units_per_pack",
        "INTEGER CHECK (units_per_pack IS NULL OR units_per_pack > 0)",
    ),
    (
        "goods_receipt_lines",
        "free_quantity_milli",
        "INTEGER NOT NULL DEFAULT 0 CHECK (free_quantity_milli >= 0)",
    ),
    (
        "goods_receipt_lines",
        "mrp_pack_paise",
        "INTEGER CHECK (mrp_pack_paise IS NULL OR mrp_pack_paise >= 0)",
    ),
    ("goods_receipt_lines", "mfg_date", "DATE"),
    ("goods_receipt_lines", "helix_lot_ref", "TEXT"),
    ("indent_lines", "helix_medication_id", "TEXT"),
    (
        "indent_lines",
        "units_per_pack",
        "INTEGER CHECK (units_per_pack IS NULL OR units_per_pack > 0)",
    ),
    (
        "indent_lines",
        "issued_quantity_milli",
        "INTEGER NOT NULL DEFAULT 0 CHECK (issued_quantity_milli >= 0)",
    ),
    ("vendors", "drug_licence_numbers", "TEXT"),
    ("vendors", "drug_licence_valid_to", "DATE"),
    ("vendors", "ndps_licence_number", "TEXT"),
    # NULL is a stores store. A non-NULL kind makes the row a pharmacy store,
    # which the stores routes cannot list, edit or move stock through.
    (
        "stores",
        "pharmacy_kind",
        "TEXT CHECK (pharmacy_kind IS NULL OR pharmacy_kind IN ('dispensing', 'sub_store', "
        "'ndps_vault', 'returns_quarantine', 'ward_imprest', 'crash_cart'))",
    ),
)

# ── New tables (no FKs; CHECKs on every enum) ─────────────────────────────────
_TABLES: tuple[str, ...] = (
    # Per-store stock levels for a drug. A master: kept by a reseed.
    """CREATE TABLE IF NOT EXISTS pharmacy_par_levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL,
    helix_medication_id TEXT NOT NULL,
    par_units INTEGER NOT NULL CHECK (par_units >= 0),
    reorder_level_units INTEGER CHECK (reorder_level_units IS NULL OR reorder_level_units >= 0),
    max_units INTEGER CHECK (max_units IS NULL OR max_units >= par_units),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    updated_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (store_id, helix_medication_id)
)""",
    # Movement of drug stock between two of the hospital's own stores. The
    # receiver is never the dispatcher, and units in transit count in neither.
    """CREATE TABLE IF NOT EXISTS pharmacy_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_number TEXT,
    from_store_id INTEGER NOT NULL,
    to_store_id INTEGER NOT NULL,
    source_indent_ref INTEGER,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'dispatched', 'partially_received', 'received', 'cancelled')),
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    dispatched_by TEXT,
    dispatched_at TIMESTAMP,
    received_by TEXT,
    received_at TIMESTAMP,
    cancelled_by TEXT,
    cancelled_at TIMESTAMP,
    cancel_reason TEXT,
    idempotency_key TEXT UNIQUE,
    remarks TEXT,
    CHECK (from_store_id <> to_store_id),
    CHECK (received_by IS NULL OR dispatched_by IS NULL OR received_by <> dispatched_by)
)""",
    """CREATE TABLE IF NOT EXISTS pharmacy_transfer_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pharmacy_transfer_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    source_indent_line_ref INTEGER,
    helix_medication_id TEXT NOT NULL,
    from_lot_id TEXT,
    to_lot_id TEXT,
    batch_no TEXT,
    expiry_date DATE,
    quantity_units INTEGER NOT NULL CHECK (quantity_units > 0),
    received_units INTEGER CHECK (received_units IS NULL OR (
        received_units >= 0 AND received_units <= quantity_units)),
    discrepancy_reason TEXT,
    UNIQUE (pharmacy_transfer_id, line_no)
)""",
    # Every lot status change (quarantine, recall, write-off ...), appended by
    # the stock service and never edited.
    """CREATE TABLE IF NOT EXISTS pharmacy_lot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL,
    helix_medication_id TEXT,
    store_id INTEGER,
    from_status TEXT CHECK (from_status IS NULL OR from_status IN (
        'available', 'quarantined', 'recalled', 'nsq', 'pending_inspection',
        'written_off', 'returned_to_vendor')),
    to_status TEXT NOT NULL CHECK (to_status IN (
        'available', 'quarantined', 'recalled', 'nsq', 'pending_inspection',
        'written_off', 'returned_to_vendor')),
    reason TEXT,
    actor_id TEXT NOT NULL,
    second_actor_id TEXT,
    reference_type TEXT,
    reference_id TEXT,
    occurred_at TEXT NOT NULL,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (second_actor_id IS NULL OR second_actor_id <> actor_id)
)""",
    # Stock take. The approver is never the counter.
    """CREATE TABLE IF NOT EXISTS pharmacy_stock_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    count_number TEXT,
    store_id INTEGER NOT NULL,
    is_blind INTEGER NOT NULL DEFAULT 0 CHECK (is_blind IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'submitted', 'approved', 'rejected', 'cancelled')),
    counted_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    submitted_at TIMESTAMP,
    approved_by TEXT,
    approved_at TIMESTAMP,
    decision_reason TEXT,
    idempotency_key TEXT UNIQUE,
    remarks TEXT,
    CHECK (approved_by IS NULL OR counted_by IS NULL OR approved_by <> counted_by)
)""",
    """CREATE TABLE IF NOT EXISTS pharmacy_stock_count_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pharmacy_stock_count_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    lot_id TEXT,
    helix_medication_id TEXT NOT NULL,
    batch_no TEXT,
    expiry_date DATE,
    system_units INTEGER NOT NULL,
    counted_units INTEGER CHECK (counted_units IS NULL OR counted_units >= 0),
    variance_units INTEGER,
    reason TEXT,
    adjustment_movement_ref TEXT,
    UNIQUE (pharmacy_stock_count_id, line_no)
)""",
    # A dispensed medicine brought back from a ward. Patient data: registered
    # for retention as (patient_id, created_at) and wiped by a reseed through
    # patient_id. Returned units wait in returns quarantine for inspection and
    # are never dispensable while they do.
    """CREATE TABLE IF NOT EXISTS pharmacy_ward_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_number TEXT,
    patient_id TEXT NOT NULL,
    helix_order_id TEXT NOT NULL,
    helix_medication_id TEXT NOT NULL,
    lot_id TEXT,
    store_id INTEGER,
    department TEXT,
    quantity_units INTEGER NOT NULL CHECK (quantity_units > 0),
    seal_intact INTEGER CHECK (seal_intact IS NULL OR seal_intact IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'pending_inspection' CHECK (status IN (
        'pending_inspection', 'restocked', 'written_off')),
    returned_by TEXT,
    received_by TEXT NOT NULL,
    occurred_at TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    inspected_by TEXT,
    inspected_at TIMESTAMP,
    inspection_note TEXT,
    note TEXT,
    idempotency_key TEXT UNIQUE
)""",
    """CREATE TABLE IF NOT EXISTS pharmacy_vendor_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_number TEXT,
    vendor_id INTEGER NOT NULL,
    store_id INTEGER NOT NULL,
    source_receipt_ref INTEGER,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'posted', 'credited', 'cancelled')),
    reason TEXT,
    vendor_credit_note_ref TEXT,
    total_paise INTEGER NOT NULL DEFAULT 0 CHECK (total_paise >= 0),
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    posted_by TEXT,
    posted_at TIMESTAMP,
    credited_at TIMESTAMP,
    cancelled_by TEXT,
    cancelled_at TIMESTAMP,
    cancel_reason TEXT,
    idempotency_key TEXT UNIQUE,
    remarks TEXT
)""",
    """CREATE TABLE IF NOT EXISTS pharmacy_vendor_return_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pharmacy_vendor_return_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    lot_id TEXT NOT NULL,
    helix_medication_id TEXT NOT NULL,
    batch_no TEXT,
    expiry_date DATE,
    quantity_units INTEGER NOT NULL CHECK (quantity_units > 0),
    unit_cost_paise INTEGER CHECK (unit_cost_paise IS NULL OR unit_cost_paise >= 0),
    value_paise INTEGER CHECK (value_paise IS NULL OR value_paise >= 0),
    reason TEXT,
    UNIQUE (pharmacy_vendor_return_id, line_no)
)""",
    # Two-person governance of the drug master (GRN-RS1, GRN-R20): a request,
    # then a decision by somebody else. A master: kept by a reseed.
    """CREATE TABLE IF NOT EXISTS pharmacy_master_change_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    helix_medication_id TEXT NOT NULL,
    change_kind TEXT NOT NULL CHECK (change_kind IN (
        'classification_downgrade', 'identity_create', 'identity_correction', 'retire')),
    field TEXT,
    from_value TEXT,
    to_value TEXT,
    reason TEXT,
    requested_by TEXT NOT NULL,
    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN (
        'pending', 'approved', 'refused', 'withdrawn')),
    decided_by TEXT,
    decided_at TIMESTAMP,
    decision_reason TEXT,
    CHECK (decision NOT IN ('approved', 'refused')
           OR (decided_by IS NOT NULL AND decided_by <> requested_by)),
    CHECK (change_kind = 'identity_create'
           OR (reason IS NOT NULL AND TRIM(reason) <> ''))
)""",
    # A controlled-drug discrepancy waiting for same-day reconciliation
    # (GRN-RS7 'record' mode, count mismatches, reconciliation failures).
    """CREATE TABLE IF NOT EXISTS ndps_variances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lane TEXT NOT NULL CHECK (lane IN ('live', 'non_production')),
    store_id INTEGER,
    helix_medication_id TEXT NOT NULL,
    lot_id TEXT,
    helix_order_id TEXT,
    movement_ref TEXT,
    variance_kind TEXT NOT NULL CHECK (variance_kind IN (
        'register_write_failed', 'negative_balance', 'count_mismatch',
        'reconciliation_mismatch')),
    expected_units INTEGER,
    found_units INTEGER,
    variance_units INTEGER,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'reconciled')),
    raised_by TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    reconciled_by TEXT,
    reconciled_at TEXT,
    reconciliation_note TEXT,
    incident_ref TEXT,
    CHECK (status <> 'reconciled' OR (
        reconciled_by IS NOT NULL AND reconciliation_note IS NOT NULL
        AND TRIM(reconciliation_note) <> ''))
)""",
    # The narcotic and psychotropic drug register: one running balance per drug
    # and store, append-only by trigger. A dispense entry names its patient and
    # order; a witness is never the person making the entry; the balance never
    # goes below zero.
    """CREATE TABLE IF NOT EXISTS ndps_register (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lane TEXT NOT NULL CHECK (lane IN ('live', 'non_production')),
    store_id INTEGER NOT NULL,
    helix_medication_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq > 0),
    lot_id TEXT,
    batch_no TEXT,
    entry_type TEXT NOT NULL CHECK (entry_type IN (
        'opening', 'receipt', 'dispense', 'transfer_in', 'transfer_out', 'ward_return',
        'vendor_return', 'wastage', 'destruction', 'write_off', 'adjustment',
        'count_variance')),
    quantity_units INTEGER NOT NULL CHECK (quantity_units <> 0 OR entry_type = 'opening'),
    balance_after_units INTEGER NOT NULL CHECK (balance_after_units >= 0),
    counterparty_type TEXT CHECK (counterparty_type IS NULL OR counterparty_type IN (
        'patient', 'vendor', 'store', 'destruction', 'none')),
    counterparty_ref TEXT,
    patient_id TEXT,
    helix_order_id TEXT,
    prescriber_id TEXT,
    document_ref TEXT,
    movement_ref TEXT,
    actor_id TEXT NOT NULL,
    witness_id TEXT,
    occurred_at TEXT NOT NULL,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    remarks TEXT,
    UNIQUE (store_id, helix_medication_id, seq),
    CHECK (witness_id IS NULL OR witness_id <> actor_id),
    CHECK (entry_type <> 'dispense' OR (patient_id IS NOT NULL AND helix_order_id IS NOT NULL))
)""",
)

TABLE_NAMES: tuple[str, ...] = (
    "pharmacy_par_levels",
    "pharmacy_transfers",
    "pharmacy_transfer_lines",
    "pharmacy_lot_events",
    "pharmacy_stock_counts",
    "pharmacy_stock_count_lines",
    "pharmacy_ward_returns",
    "pharmacy_vendor_returns",
    "pharmacy_vendor_return_lines",
    "pharmacy_master_change_requests",
    "ndps_variances",
    "ndps_register",
)

# (index name, table, key, WHERE clause or None, unique). Created only when
# the table and every column the key names exist. None of these names starts
# with idx_helix, the prefix of the four HELIX_SCHEMA indexes
# tests/test_migrations.py pins.
_INDEXES: tuple[tuple[str, str, str, str | None, bool], ...] = (
    (
        "idx_pharmacy_lots_dispensable",
        "helix_inventory_lots",
        "(medication_id, store_id, status, expiration_date)",
        None,
        False,
    ),
    (
        "ux_pharmacy_lots_store_batch",
        "helix_inventory_lots",
        "(medication_id, store_id, lot_number, expiration_date)",
        "store_id IS NOT NULL",
        True,
    ),
    (
        "ux_pharmacy_movements_idempotency",
        "helix_stock_movements",
        "(idempotency_key)",
        "idempotency_key IS NOT NULL",
        True,
    ),
    ("ux_pharmacy_movements_seq", "helix_stock_movements", "(seq)", "seq IS NOT NULL", True),
    (
        "idx_pharmacy_movements_med_store_seq",
        "helix_stock_movements",
        "(medication_id, store_id, seq)",
        None,
        False,
    ),
    ("idx_pharmacy_movements_lot_seq", "helix_stock_movements", "(lot_id, seq)", None, False),
    ("idx_po_domain_status", "purchase_orders", "(domain, status, order_date)", None, False),
    ("idx_grn_domain_status", "goods_receipts", "(domain, status, received_at)", None, False),
    ("idx_indents_domain_status", "indents", "(domain, status, requested_at)", None, False),
    # One active dispensing store (GRN-R17: one dispensing pharmacy per site in
    # v1; the multi-site epic relaxes this to one per facility).
    (
        "ux_stores_one_active_dispensing",
        "stores",
        "(pharmacy_kind)",
        "pharmacy_kind = 'dispensing' AND is_active = 1",
        True,
    ),
    ("idx_stores_pharmacy_kind", "stores", "(pharmacy_kind, is_active)", None, False),
    ("idx_pharmacy_par_levels_med", "pharmacy_par_levels", "(helix_medication_id)", None, False),
    (
        "ux_pharmacy_transfers_number",
        "pharmacy_transfers",
        "(transfer_number)",
        "transfer_number IS NOT NULL",
        True,
    ),
    (
        "idx_pharmacy_transfers_status",
        "pharmacy_transfers",
        "(status, to_store_id, from_store_id)",
        None,
        False,
    ),
    ("idx_pharmacy_lot_events_lot", "pharmacy_lot_events", "(lot_id, id)", None, False),
    (
        "ux_pharmacy_stock_counts_number",
        "pharmacy_stock_counts",
        "(count_number)",
        "count_number IS NOT NULL",
        True,
    ),
    ("idx_pharmacy_stock_counts_store", "pharmacy_stock_counts", "(store_id, status)", None, False),
    (
        "ux_pharmacy_ward_returns_number",
        "pharmacy_ward_returns",
        "(return_number)",
        "return_number IS NOT NULL",
        True,
    ),
    ("idx_pharmacy_ward_returns_order", "pharmacy_ward_returns", "(helix_order_id)", None, False),
    (
        "idx_pharmacy_ward_returns_patient",
        "pharmacy_ward_returns",
        "(patient_id, created_at)",
        None,
        False,
    ),
    (
        "idx_pharmacy_ward_returns_status",
        "pharmacy_ward_returns",
        "(status, store_id)",
        None,
        False,
    ),
    (
        "ux_pharmacy_vendor_returns_number",
        "pharmacy_vendor_returns",
        "(return_number)",
        "return_number IS NOT NULL",
        True,
    ),
    (
        "idx_pharmacy_vendor_returns_vendor",
        "pharmacy_vendor_returns",
        "(vendor_id, status)",
        None,
        False,
    ),
    (
        "idx_pharmacy_change_requests_med",
        "pharmacy_master_change_requests",
        "(helix_medication_id, decision)",
        None,
        False,
    ),
    # One open request per drug, kind and field: a second one would let two
    # approvers act on the same change.
    (
        "ux_pharmacy_change_requests_pending",
        "pharmacy_master_change_requests",
        "(helix_medication_id, change_kind, COALESCE(field, ''))",
        "decision = 'pending'",
        True,
    ),
    (
        "idx_ndps_variances_open",
        "ndps_variances",
        "(status, store_id, helix_medication_id)",
        None,
        False,
    ),
    (
        "idx_ndps_register_drug",
        "ndps_register",
        "(store_id, helix_medication_id, seq)",
        None,
        False,
    ),
    ("idx_ndps_register_patient", "ndps_register", "(patient_id)", None, False),
)

_TRIGGERS: tuple[str, ...] = (
    """CREATE TRIGGER IF NOT EXISTS trg_ndps_register_no_update
    BEFORE UPDATE ON ndps_register
    BEGIN
        SELECT RAISE(ABORT, 'ndps_register is append-only: record a correcting entry instead of editing one');
    END""",
    """CREATE TRIGGER IF NOT EXISTS trg_ndps_register_no_delete
    BEFORE DELETE ON ndps_register
    WHEN OLD.lane <> 'non_production'
    BEGIN
        SELECT RAISE(ABORT, 'ndps_register: a live entry is a statutory record and cannot be deleted');
    END""",
)

# Pharmacy document series (GRN-R11), each a placeholder until the site sets
# its own prefix, exactly as migration 20's series are.
SERIES: tuple[tuple[str, str, str], ...] = (
    ("PHPO", "pharmacy_purchase_order", "PHPO-"),
    ("PHGRN", "pharmacy_goods_receipt", "PHGRN-"),
    ("PHIND", "pharmacy_indent", "PHIND-"),
    ("PHTRF", "pharmacy_transfer", "PHTRF-"),
    ("PHVR", "pharmacy_vendor_return", "PHVR-"),
    ("PHRET", "pharmacy_ward_return", "PHRET-"),
    ("PHCNT", "pharmacy_stock_count", "PHCNT-"),
)

OPENING_REMARK = "Balance brought forward when the stock ledger began"
CLOSING_REMARK = (
    "Correction made when the stock ledger began: the earlier entries for "
    "this lot did not add up to the quantity on the shelf"
)

# The legacy columns the backfill reads. Every Helix database has them
# (HELIX_SCHEMA has carried them since the table was introduced); they are
# added here, nullable, only if a hand-built table lacks one, so the backfill
# can always run.
_LEGACY_MOVEMENT_COLUMNS = (
    ("lot_id", "TEXT"),
    ("medication_id", "TEXT"),
    ("order_id", "TEXT"),
    ("quantity_change", "INTEGER"),
    ("reason", "TEXT"),
    ("timestamp", "TIMESTAMP"),
)


def _table_exists(conn, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _columns(conn, table: str) -> dict[str, tuple]:
    """{column name: PRAGMA table_info row} for an existing table, else {}."""
    return {r[1]: tuple(r) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _add_columns(conn, wanted) -> None:
    for table, column, decl in wanted:
        if not _table_exists(conn, table):
            continue
        if column not in _columns(conn, table):
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {decl}')


def _create_indexes(conn) -> None:
    for name, table, key, where, unique in _INDEXES:
        if not _table_exists(conn, table):
            continue
        # The plain column names in the key (an expression such as
        # COALESCE(field, '') contributes the column inside it).
        named = {word for word in re.findall(r"[A-Za-z_]+", key) if word.upper() != "COALESCE"}
        if not named <= set(_columns(conn, table)):
            continue
        kind = "UNIQUE INDEX" if unique else "INDEX"
        clause = f" WHERE {where}" if where else ""
        conn.execute(f"CREATE {kind} IF NOT EXISTS {name} ON {table} {key}{clause}")


def apply(conn) -> None:
    _add_columns(conn, _HELIX_COLUMNS)
    _add_columns(conn, _PROCUREMENT_COLUMNS)
    for ddl in _TABLES:
        conn.execute(ddl)
    for ddl in _TRIGGERS:
        conn.execute(ddl)
    _create_indexes(conn)
    if _table_exists(conn, "document_number_series"):
        conn.executemany(
            "INSERT OR IGNORE INTO document_number_series "
            "(series_code, document_type, prefix, next_number, is_configured) "
            "VALUES (?, ?, ?, 1, 0)",
            SERIES,
        )
    if _table_exists(conn, "helix_stock_movements") and _table_exists(conn, "helix_inventory_lots"):
        backfill_stock_ledger(conn)


# ── The ledger backfill ───────────────────────────────────────────────────────


def _legacy_stamp(dt: datetime) -> str:
    """SQLite's CURRENT_TIMESTAMP spelling (UTC, seconds), for the legacy
    `timestamp` column the existing Helix readers order by."""
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def backfill_stock_ledger(conn, *, now: datetime | None = None) -> dict:
    """Make every lot's quantity equal the sum of its movements; see the module
    docstring. Runs in the caller's transaction and never commits. Returns
    counts. `now` is for tests: the instant an opening row of a lot with no
    movements is dated."""
    from services.clinical_time import format_utc, parse_to_utc

    now = (now or datetime.now(UTC)).astimezone(UTC)
    _add_columns(conn, [("helix_stock_movements", c, d) for c, d in _LEGACY_MOVEMENT_COLUMNS])
    _add_columns(conn, [c for c in _HELIX_COLUMNS if c[0] == "helix_stock_movements"])
    cols = _columns(conn, "helix_stock_movements")
    # An INTEGER primary key is assigned by SQLite; the TEXT key Helix uses
    # takes a uuid, as helix_service writes it.
    id_is_integer = "INT" in str(cols.get("id", (None, None, ""))[2]).upper()

    counts = {
        "occurred_at_set": 0,
        "occurred_at_unreadable": 0,
        "typed_dispense": 0,
        "opening_rows": 0,
        "closing_rows": 0,
        "sequenced": 0,
        "balanced": 0,
        "negative_lots": 0,
    }

    # 1. occurred_at, canonical UTC, from the legacy timestamp.
    updates = []
    for rowid, raw in conn.execute(
        "SELECT rowid, timestamp FROM helix_stock_movements WHERE occurred_at IS NULL"
    ).fetchall():
        try:
            updates.append((format_utc(parse_to_utc(raw, naive_tz=UTC)), rowid))
        except (TypeError, ValueError):
            counts["occurred_at_unreadable"] += 1
    if updates:
        conn.executemany(
            "UPDATE helix_stock_movements SET occurred_at = ? WHERE rowid = ?", updates
        )
    counts["occurred_at_set"] = len(updates)

    # 2. Type the legacy dispense rows. Nothing else is guessed.
    cur = conn.execute(
        "UPDATE helix_stock_movements SET movement_type = 'dispense', "
        "reference_type = CASE WHEN order_id IS NOT NULL THEN 'helix_order' END, "
        "reference_id = order_id "
        "WHERE movement_type IS NULL AND reason = 'dispense'"
    )
    counts["typed_dispense"] = max(cur.rowcount, 0)

    # 3. Plan an opening (and, rarely, a closing) row per unbalanced lot, from
    #    the rows that have no seq yet, in the order they will be sequenced.
    order_sql = "ORDER BY COALESCE(occurred_at, ''), rowid"
    legacy: dict[str, list[int]] = {}
    first_at: dict[str, str] = {}
    last_at: dict[str, str] = {}
    for lot_id, change, occurred in conn.execute(
        "SELECT lot_id, quantity_change, occurred_at FROM helix_stock_movements "
        f"WHERE seq IS NULL AND lot_id IS NOT NULL {order_sql}"
    ).fetchall():
        legacy.setdefault(lot_id, []).append(int(change or 0))
        if occurred:
            first_at.setdefault(lot_id, occurred)
            last_at[lot_id] = occurred
    sequenced_sum = {
        lot_id: int(total or 0)
        for lot_id, total in conn.execute(
            "SELECT lot_id, SUM(COALESCE(quantity_change, 0)) FROM helix_stock_movements "
            "WHERE seq IS NOT NULL AND lot_id IS NOT NULL GROUP BY lot_id"
        ).fetchall()
    }

    openings: list[tuple] = []
    closings: list[tuple] = []
    for lot_id, medication_id, quantity in conn.execute(
        "SELECT id, medication_id, COALESCE(quantity, 0) FROM helix_inventory_lots ORDER BY id"
    ).fetchall():
        changes = legacy.get(lot_id, [])
        total = sequenced_sum.get(lot_id, 0) + sum(changes)
        quantity = int(quantity)
        if quantity < 0:
            counts["negative_lots"] += 1
        if total == quantity and not changes:
            continue
        running, lowest = sequenced_sum.get(lot_id, 0), sequenced_sum.get(lot_id, 0)
        for change in changes:
            running += change
            lowest = min(lowest, running)
        opening = max(quantity - total, -min(lowest, 0))
        closing = quantity - total - opening
        if opening == 0 and closing == 0:
            continue
        if opening:
            if lot_id in first_at:
                at = parse_to_utc(first_at[lot_id]) - timedelta(seconds=1)
            else:
                at = now
            openings.append((lot_id, medication_id, opening, at))
        if closing:
            at = parse_to_utc(last_at[lot_id]) + timedelta(seconds=1) if lot_id in last_at else now
            closings.append((lot_id, medication_id, closing, at))

    next_seq = (
        int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM helix_stock_movements").fetchone()[0])
        + 1
    )

    def _insert(lot_id, medication_id, change, at, movement_type, remark, seq):
        values = {
            "lot_id": lot_id,
            "medication_id": medication_id,
            "order_id": None,
            "quantity_change": change,
            "reason": movement_type,
            "timestamp": _legacy_stamp(at),
            "movement_type": movement_type,
            "store_id": None,
            "actor_id": "system",
            "remarks": remark,
            "occurred_at": format_utc(at),
            "seq": seq,
        }
        if not id_is_integer:
            values = {"id": str(uuid.uuid4()), **values}
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO helix_stock_movements ({names}) VALUES ({marks})", tuple(values.values())
        )

    # 4. Sequence: openings, then the legacy rows in time order, then closings.
    for lot_id, medication_id, change, at in openings:
        _insert(lot_id, medication_id, change, at, "opening", OPENING_REMARK, next_seq)
        next_seq += 1
    counts["opening_rows"] = len(openings)
    pending = [
        r[0]
        for r in conn.execute(
            f"SELECT rowid FROM helix_stock_movements WHERE seq IS NULL {order_sql}"
        ).fetchall()
    ]
    conn.executemany(
        "UPDATE helix_stock_movements SET seq = ? WHERE rowid = ?",
        [(next_seq + i, rowid) for i, rowid in enumerate(pending)],
    )
    next_seq += len(pending)
    counts["sequenced"] = len(pending)
    for lot_id, medication_id, change, at in closings:
        _insert(lot_id, medication_id, change, at, "adjustment", CLOSING_REMARK, next_seq)
        next_seq += 1
    counts["closing_rows"] = len(closings)

    # 5. balance_after_units: the running sum per lot in seq order, written
    #    only where it is missing.
    running_by_lot: dict[str, int] = {}
    balances = []
    for rowid, lot_id, change, balance in conn.execute(
        "SELECT rowid, lot_id, quantity_change, balance_after_units "
        "FROM helix_stock_movements WHERE lot_id IS NOT NULL ORDER BY seq"
    ).fetchall():
        running_by_lot[lot_id] = running_by_lot.get(lot_id, 0) + int(change or 0)
        if balance is None:
            balances.append((running_by_lot[lot_id], rowid))
    if balances:
        conn.executemany(
            "UPDATE helix_stock_movements SET balance_after_units = ? WHERE rowid = ?", balances
        )
    counts["balanced"] = len(balances)

    if counts["occurred_at_unreadable"]:
        logger.warning(
            "pharmacy ledger backfill: %d movement timestamp(s) could not be read "
            "and keep no occurred_at",
            counts["occurred_at_unreadable"],
        )
    if counts["negative_lots"]:
        logger.warning(
            "pharmacy ledger backfill: %d lot(s) hold a negative quantity", counts["negative_lots"]
        )
    if counts["opening_rows"] or counts["closing_rows"] or counts["typed_dispense"]:
        _audit_backfill(conn, counts)
    logger.info("pharmacy ledger backfill: %s", counts)
    return counts


def _audit_backfill(conn, counts: dict) -> None:
    """One PHI-free data_management audit row (counts only), as migration 43
    writes for its own backfill. Skipped on a database with no audit table
    (the bare test databases); every real database has one."""
    if not _table_exists(conn, "audit_logs"):
        return
    from services.audit_service import log_audit_event

    log_audit_event(
        correlation_id="migration-pharmacy_store_schema",
        event_category="data_management",
        action_type="pharmacy_stock_ledger_opened",
        outcome="success",
        db=conn,
        summary_message=(
            "Drug stock ledger opened: "
            f"{counts['opening_rows']} opening entries, {counts['closing_rows']} "
            f"closing corrections, {counts['typed_dispense']} dispense entries typed, "
            f"{counts['sequenced']} entries sequenced"
        ),
        actor_user_id="system",
        metadata={"counts": counts},
    )


MIGRATION = Migration(54, "pharmacy_store_schema", apply)
