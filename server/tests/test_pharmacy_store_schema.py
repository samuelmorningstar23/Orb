"""Migration 54, pharmacy_store_schema (grn-3, docs/his_design/grn.json section C).

Driven directly against databases built in tmp_path, following the vintage-DB
convention in test_migrations.py: migration 20 (the procurement tables) and 49
(the Helix tables) build the schema this migration meets on a live appliance,
legacy rows are written in the shape the old code wrote them, then migration
54 runs. Nothing here touches the shared test database.

What is pinned:
  * every new column, table, index, trigger and series row lands, and a second
    run changes nothing;
  * the Helix tables gain no CHECK (they are FK parents and can never be
    rebuilt), the new tables and the migration 20 columns refuse bad values;
  * the ledger backfill: for every lot SUM(quantity_change) equals the
    quantity, no running balance is negative in seq order, seq is unique, and
    occurred_at is the canonical UTC spelling;
  * existing procurement rows read domain 'stores', and the is_pharmacy CHECK
    still refuses a drug in the stores item master;
  * the NDPS register refuses every UPDATE and the DELETE of a live row, and
    allows the DELETE of a non-production row.
"""
from __future__ import annotations

import importlib
import sqlite3
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatchcase

import pytest

import migrations
from db_backend import DBIntegrityError
from services.clinical_time import CANONICAL_GLOB


def _m54():
    return importlib.import_module("migrations_his.m0054_pharmacy_store_schema")


def _m49():
    return importlib.import_module("migrations_his.m0049_helix_tables_in_runner")


def _canonical(value) -> bool:
    # CANONICAL_GLOB is a SQLite GLOB; fnmatch speaks the same character classes.
    return bool(value) and fnmatchcase(value, CANONICAL_GLOB)


# ── A database as a live appliance holds it before migration 54 ──────────────

def _pre54(tmp_path, name="legacy.db") -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / name))
    migrations._m20_hospital_admin_spine(conn)
    _m49().MIGRATION.apply(conn)
    conn.commit()
    return conn


def _drug(conn, med_id: str, name: str) -> None:
    conn.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength) "
                 "VALUES (?, ?, ?, 'tablet', '500 mg')", (med_id, name, name.lower()))


def _lot(conn, lot_id: str, med_id: str, quantity, expiry="2030-12-31") -> None:
    conn.execute("INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, "
                 "expiration_date) VALUES (?, ?, ?, ?, ?)",
                 (lot_id, med_id, f"B-{lot_id}", quantity, expiry))


def _legacy_movement(conn, lot_id, med_id, change, when, reason="dispense", order_id="O-1"):
    """A movement exactly as helix_service._atomic_dispense wrote one: a uuid
    key, no type, and SQLite's CURRENT_TIMESTAMP spelling (naive UTC)."""
    conn.execute(
        "INSERT INTO helix_stock_movements (id, lot_id, medication_id, order_id, "
        "quantity_change, reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), lot_id, med_id, order_id, change, reason, when))


@pytest.fixture
def legacy(tmp_path):
    """Four lots, the shapes a live appliance actually has:
      L-SEEDED  seeded with 40 units, never dispensed (no movements at all)
      L-USED    seeded with 30, dispensed three times, so it holds 27
      L-EMPTY   seeded with 2, dispensed twice, so it holds 0
      L-EXACT   a lot whose movements already add up (a +5 then -1, holding 4)
    and stores documents raised before the migration."""
    conn = _pre54(tmp_path)
    _drug(conn, "MED-A", "Amoxicillin")
    _drug(conn, "MED-B", "Paracetamol")
    _lot(conn, "L-SEEDED", "MED-A", 40)
    _lot(conn, "L-USED", "MED-A", 27)
    _lot(conn, "L-EMPTY", "MED-B", 0)
    _lot(conn, "L-EXACT", "MED-B", 4)
    for when in ("2026-10-01 08:00:00", "2026-10-01 09:30:00", "2026-10-02 07:15:00"):
        _legacy_movement(conn, "L-USED", "MED-A", -1, when)
    for when in ("2026-09-30 22:00:00", "2026-10-01 01:00:00"):
        _legacy_movement(conn, "L-EMPTY", "MED-B", -1, when, order_id="O-2")
    _legacy_movement(conn, "L-EXACT", "MED-B", 5, "2026-09-29 10:00:00", reason="restock",
                     order_id=None)
    _legacy_movement(conn, "L-EXACT", "MED-B", -1, "2026-09-29 11:00:00")

    conn.execute("INSERT INTO vendors (code, name) VALUES ('V1', 'Supplier')")
    conn.execute("INSERT INTO indents (store_id, status) VALUES (1, 'draft')")
    conn.execute("INSERT INTO indent_lines (indent_id, line_no, item_code, description, "
                 "quantity_milli) VALUES (1, 1, 'GLOVE', 'Gloves', 1000)")
    conn.execute("INSERT INTO purchase_orders (vendor_id, store_id) VALUES (1, 1)")
    conn.execute("INSERT INTO purchase_order_lines (purchase_order_id, line_no, item_code, "
                 "description, quantity_milli) VALUES (1, 1, 'GLOVE', 'Gloves', 1000)")
    conn.execute("INSERT INTO goods_receipts (purchase_order_id, vendor_id, store_id) "
                 "VALUES (1, 1, 1)")
    conn.execute("INSERT INTO goods_receipt_lines (goods_receipt_id, line_no, item_code, "
                 "description, quantity_milli) VALUES (1, 1, 'GLOVE', 'Gloves', 1000)")
    conn.commit()
    yield conn
    conn.close()


NOW = datetime(2026, 10, 4, 6, 0, 0, tzinfo=timezone.utc)


def _migrate(conn):
    """apply() as the runner calls it, with the backfill's 'now' pinned so the
    opening row of a lot with no movements has a known date."""
    m54 = _m54()
    original = m54.backfill_stock_ledger
    m54.backfill_stock_ledger = lambda c, **kw: original(c, now=NOW)
    try:
        m54.MIGRATION.apply(conn)
    finally:
        m54.backfill_stock_ledger = original
    conn.commit()


def _cols(conn, table):
    return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ═════════════════════════════════════════════════════════════════════════════
#  Shape
# ═════════════════════════════════════════════════════════════════════════════

def test_the_migration_is_registered_as_54():
    m54 = _m54()
    assert m54.MIGRATION.version == 54 and m54.MIGRATION.name == "pharmacy_store_schema"
    assert any(m.version == 54 and m.name == "pharmacy_store_schema"
               for m in migrations.MIGRATIONS)


def test_every_new_column_lands(legacy):
    _migrate(legacy)
    m54 = _m54()
    for table, column, _decl in m54._HELIX_COLUMNS + m54._PROCUREMENT_COLUMNS:
        assert column in _cols(legacy, table), f"{table}.{column} missing"


def test_every_new_table_trigger_and_series_row_lands(legacy):
    _migrate(legacy)
    m54 = _m54()
    tables = {r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(m54.TABLE_NAMES) <= tables
    triggers = {r[0] for r in legacy.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='ndps_register'")}
    assert triggers == {"trg_ndps_register_no_update", "trg_ndps_register_no_delete"}
    indexes = {r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {name for name, *_ in m54._INDEXES} <= indexes
    series = {r[0]: (r[1], r[2], r[3]) for r in legacy.execute(
        "SELECT series_code, prefix, next_number, is_configured FROM document_number_series "
        "WHERE series_code LIKE 'PH%'")}
    assert series == {code: (prefix, 1, 0) for code, _kind, prefix in m54.SERIES}
    assert set(series) == {"PHPO", "PHGRN", "PHIND", "PHTRF", "PHVR", "PHRET", "PHCNT"}


def test_no_check_is_added_to_a_helix_table(legacy):
    """RULE 1: the Helix tables are FK parents and can never be rebuilt, so an
    enum frozen into a CHECK could never change. Their DDL stays CHECK-free and
    an out-of-set value is the services' job to refuse."""
    _migrate(legacy)
    for table in ("helix_medication_master", "helix_inventory_lots", "helix_stock_movements"):
        sql = legacy.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        assert "CHECK" not in sql.upper(), f"{table} gained a CHECK: {sql}"
    legacy.execute("UPDATE helix_inventory_lots SET status = 'a status added later' "
                   "WHERE id = 'L-SEEDED'")
    legacy.execute("UPDATE helix_medication_master SET drug_schedule = 'C1' WHERE id = 'MED-A'")


def test_existing_rows_read_as_today(legacy):
    """Every existing lot is available and unlocated, every drug active and
    unclassified, every procurement document a stores purchase document."""
    _migrate(legacy)
    assert {r[0] for r in legacy.execute("SELECT status FROM helix_inventory_lots")} == {"available"}
    assert {r[0] for r in legacy.execute("SELECT store_id FROM helix_inventory_lots")} == {None}
    assert set(legacy.execute(
        "SELECT is_active, identity_status, drug_schedule, ndps_category, gst_rate_bp "
        "FROM helix_medication_master").fetchall()) == {(1, "active", None, None, None)}
    for table in ("purchase_orders", "goods_receipts", "indents"):
        assert {r[0] for r in legacy.execute(f"SELECT domain FROM {table}")} == {"stores"}
    assert legacy.execute("SELECT indent_kind FROM indents").fetchone()[0] == "purchase"
    assert legacy.execute("SELECT free_quantity_milli FROM goods_receipt_lines").fetchone()[0] == 0
    assert legacy.execute("SELECT issued_quantity_milli FROM indent_lines").fetchone()[0] == 0
    assert {r[0] for r in legacy.execute("SELECT pharmacy_kind FROM stores")} == {None}


def test_the_is_pharmacy_check_still_refuses_a_drug(legacy):
    _migrate(legacy)
    with pytest.raises(DBIntegrityError):
        legacy.execute("INSERT INTO inventory_items (code, name, is_pharmacy) "
                       "VALUES ('AMOX', 'Amoxicillin 500 mg', 1)")


# ═════════════════════════════════════════════════════════════════════════════
#  The ledger backfill
# ═════════════════════════════════════════════════════════════════════════════

def _ledger(conn):
    return conn.execute(
        "SELECT lot_id, quantity_change, balance_after_units, seq, occurred_at, movement_type, "
        "reason, reference_type, reference_id, remarks, actor_id, timestamp "
        "FROM helix_stock_movements ORDER BY seq").fetchall()


def _assert_ledger_invariants(conn):
    rows = _ledger(conn)
    seqs = [r[3] for r in rows]
    assert None not in seqs, "a movement was left without a seq"
    assert len(seqs) == len(set(seqs)), "seq is not unique"
    running: dict = {}
    for lot_id, change, balance, *_ in rows:
        running[lot_id] = running.get(lot_id, 0) + change
        assert balance == running[lot_id], (lot_id, balance, running[lot_id])
        assert balance >= 0, f"{lot_id} runs negative in seq order"
    for lot_id, quantity in conn.execute("SELECT id, quantity FROM helix_inventory_lots"):
        total = conn.execute("SELECT COALESCE(SUM(quantity_change), 0) FROM helix_stock_movements "
                             "WHERE lot_id = ?", (lot_id,)).fetchone()[0]
        assert total == quantity, f"{lot_id}: ledger {total} vs shelf {quantity}"
    for row in rows:
        assert _canonical(row[4]), f"occurred_at {row[4]!r} is not canonical UTC"


def test_after_the_backfill_every_lot_equals_its_ledger(legacy):
    _migrate(legacy)
    _assert_ledger_invariants(legacy)


def test_opening_rows_carry_the_brought_forward_balance_and_come_first(legacy):
    _migrate(legacy)
    m54 = _m54()
    openings = legacy.execute(
        "SELECT lot_id, quantity_change, occurred_at, timestamp, remarks, actor_id, seq, "
        "medication_id, reason, order_id FROM helix_stock_movements "
        "WHERE movement_type = 'opening' ORDER BY seq").fetchall()
    by_lot = {r[0]: r for r in openings}
    # L-SEEDED had no movement: its whole quantity is brought forward, dated now.
    # L-USED held 27 after three dispenses, so 30 came forward. L-EMPTY: 2.
    # L-EXACT already balanced and gets none.
    assert {lot: r[1] for lot, r in by_lot.items()} == {"L-SEEDED": 40, "L-USED": 30, "L-EMPTY": 2}
    assert by_lot["L-SEEDED"][2] == "2026-10-04T06:00:00.000000+00:00"
    # One second before the lot's first movement, in both spellings.
    assert by_lot["L-USED"][2] == "2026-10-01T07:59:59.000000+00:00"
    assert by_lot["L-USED"][3] == "2026-10-01 07:59:59"
    assert by_lot["L-EMPTY"][2] == "2026-09-30T21:59:59.000000+00:00"
    for r in openings:
        assert r[4] == m54.OPENING_REMARK
        assert r[5] == "system"
        assert r[8] == "opening" and r[9] is None
    assert by_lot["L-USED"][7] == "MED-A"
    # Openings are sequenced before every legacy row.
    first_legacy = legacy.execute(
        "SELECT MIN(seq) FROM helix_stock_movements WHERE movement_type IS NOT 'opening'"
    ).fetchone()[0]
    assert max(r[6] for r in openings) < first_legacy


def test_legacy_rows_are_typed_and_sequenced_in_time_order(legacy):
    _migrate(legacy)
    rows = legacy.execute(
        "SELECT occurred_at, movement_type, reference_type, reference_id, reason "
        "FROM helix_stock_movements WHERE movement_type IS NOT 'opening' ORDER BY seq").fetchall()
    stamps = [r[0] for r in rows]
    assert stamps == sorted(stamps), "legacy rows are not sequenced in time order"
    assert stamps[0] == "2026-09-29T10:00:00.000000+00:00"
    dispenses = [r for r in rows if r[4] == "dispense"]
    assert dispenses and all(r[1] == "dispense" and r[2] == "helix_order" and r[3] for r in dispenses)
    # A reason the migration does not know is left untyped for reconcile().
    restock = [r for r in rows if r[4] == "restock"]
    assert len(restock) == 1 and restock[0][1] is None and restock[0][2] is None


def test_a_history_that_would_run_negative_gets_a_raised_opening_and_a_correction(tmp_path):
    """Ledger rows that, read in time order, would take the lot below zero (a
    receipt typed after the dispenses it paid for). The opening is raised to
    keep every balance at zero or above, and one closing adjustment brings the
    total back to the shelf."""
    conn = _pre54(tmp_path)
    _drug(conn, "MED-C", "Ceftriaxone")
    _lot(conn, "L-ODD", "MED-C", 1)
    _legacy_movement(conn, "L-ODD", "MED-C", -3, "2026-10-01 08:00:00")
    _legacy_movement(conn, "L-ODD", "MED-C", 5, "2026-10-01 09:00:00", reason="restock")
    conn.commit()
    _migrate(conn)
    _assert_ledger_invariants(conn)
    rows = [(r[1], r[2], r[5]) for r in _ledger(conn)]
    assert rows == [(3, 3, "opening"), (-3, 0, "dispense"), (5, 5, None), (-4, 1, "adjustment")]
    closing = conn.execute("SELECT remarks, occurred_at FROM helix_stock_movements "
                           "WHERE movement_type = 'adjustment'").fetchone()
    assert closing == (_m54().CLOSING_REMARK, "2026-10-01T09:00:01.000000+00:00")


def test_an_unreadable_timestamp_is_left_blank_not_guessed(tmp_path):
    conn = _pre54(tmp_path)
    _drug(conn, "MED-D", "Dexamethasone")
    _lot(conn, "L-D", "MED-D", 4)
    _legacy_movement(conn, "L-D", "MED-D", -1, "not a time")
    conn.commit()
    counts = _m54().backfill_stock_ledger(conn, now=NOW)
    assert counts["occurred_at_unreadable"] == 1
    row = conn.execute("SELECT occurred_at, seq, balance_after_units FROM helix_stock_movements "
                       "WHERE timestamp = 'not a time'").fetchone()
    assert row[0] is None and row[1] is not None and row[2] == 4
    total = conn.execute("SELECT SUM(quantity_change) FROM helix_stock_movements").fetchone()[0]
    assert total == 4


def test_an_integer_keyed_movement_table_is_backfilled_too(tmp_path):
    """A hand-built ledger keyed by INTEGER (the vintage shape in
    test_fresh_database_init) takes its opening rows without a uuid."""
    conn = sqlite3.connect(str(tmp_path / "intkey.db"))
    conn.execute("CREATE TABLE helix_medication_master (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("CREATE TABLE helix_inventory_lots (id TEXT PRIMARY KEY, medication_id TEXT, "
                 "lot_number TEXT NOT NULL, quantity INTEGER DEFAULT 0, expiration_date DATE)")
    conn.execute("CREATE TABLE helix_stock_movements (id INTEGER PRIMARY KEY, order_id TEXT, "
                 "reason TEXT)")
    conn.execute("INSERT INTO helix_inventory_lots (id, lot_number, quantity) VALUES ('L1', 'B', 6)")
    conn.commit()
    _migrate(conn)
    assert conn.execute("SELECT id, lot_id, quantity_change, movement_type, seq "
                        "FROM helix_stock_movements").fetchall() == [(1, "L1", 6, "opening", 1)]


def test_lots_themselves_are_never_modified(legacy):
    before = legacy.execute("SELECT id, medication_id, lot_number, quantity, expiration_date "
                            "FROM helix_inventory_lots ORDER BY id").fetchall()
    _migrate(legacy)
    after = legacy.execute("SELECT id, medication_id, lot_number, quantity, expiration_date "
                           "FROM helix_inventory_lots ORDER BY id").fetchall()
    assert before == after


# ═════════════════════════════════════════════════════════════════════════════
#  Re-running
# ═════════════════════════════════════════════════════════════════════════════

def test_a_second_run_changes_nothing(legacy):
    _migrate(legacy)
    schema = legacy.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    ledger = _ledger(legacy)
    series = legacy.execute("SELECT * FROM document_number_series ORDER BY id").fetchall()
    _migrate(legacy)
    assert legacy.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    assert _ledger(legacy) == ledger
    assert legacy.execute("SELECT * FROM document_number_series ORDER BY id").fetchall() == series


def test_a_failure_part_way_leaves_nothing_behind(legacy, monkeypatch):
    """One statement per execute and no executescript, so the runner's
    transaction holds the whole migration: a failure in the backfill rolls back
    every column and table added before it."""
    m54 = _m54()

    def boom(conn, **kw):
        raise RuntimeError("backfill failed")

    monkeypatch.setattr(m54, "backfill_stock_ledger", boom)
    legacy.isolation_level = None
    legacy.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError):
        m54.MIGRATION.apply(legacy)
    legacy.execute("ROLLBACK")
    assert "domain" not in _cols(legacy, "purchase_orders")
    assert "status" not in _cols(legacy, "helix_inventory_lots")
    assert legacy.execute("SELECT name FROM sqlite_master WHERE name = 'ndps_register'").fetchone() is None


def test_the_helix_parts_apply_to_a_bare_helix_database(tmp_path):
    """tests/test_migrations.py compares the frozen migration 49 shape, after
    this migration, with HELIX_SCHEMA on a database holding only the Helix
    tables; the procurement parts are skipped there, never failed."""
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    _m49().MIGRATION.apply(conn)
    _migrate(conn)
    assert "status" in _cols(conn, "helix_inventory_lots")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'purchase_orders'").fetchone() is None


# ═════════════════════════════════════════════════════════════════════════════
#  CHECKs on the new tables and the migration 20 columns
# ═════════════════════════════════════════════════════════════════════════════

BAD_VALUES = [
    ("purchase_orders domain", "INSERT INTO purchase_orders (vendor_id, domain) VALUES (1, 'drugs')"),
    ("goods_receipts domain", "INSERT INTO goods_receipts (vendor_id, domain) VALUES (1, 'x')"),
    ("indents domain", "INSERT INTO indents (domain) VALUES ('x')"),
    ("indents kind", "INSERT INTO indents (indent_kind) VALUES ('loan')"),
    ("stores pharmacy_kind", "INSERT INTO stores (code, name, pharmacy_kind) VALUES ('X', 'X', 'shop')"),
    ("free quantity", "INSERT INTO goods_receipt_lines (goods_receipt_id, line_no, item_code, "
                      "description, quantity_milli, free_quantity_milli) VALUES (1, 9, 'A', 'A', 1, -1)"),
    ("units per pack", "INSERT INTO purchase_order_lines (purchase_order_id, line_no, item_code, "
                       "description, quantity_milli, units_per_pack) VALUES (1, 9, 'A', 'A', 1, 0)"),
    ("par level", "INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units) "
                  "VALUES (1, 'MED-A', -1)"),
    ("transfer status", "INSERT INTO pharmacy_transfers (from_store_id, to_store_id, status) "
                        "VALUES (1, 2, 'lost')"),
    ("transfer to itself", "INSERT INTO pharmacy_transfers (from_store_id, to_store_id) VALUES (1, 1)"),
    ("transfer self-receipt", "INSERT INTO pharmacy_transfers (from_store_id, to_store_id, "
                              "dispatched_by, received_by) VALUES (1, 2, 'P1', 'P1')"),
    ("transfer over-receipt", "INSERT INTO pharmacy_transfer_lines (pharmacy_transfer_id, line_no, "
                              "helix_medication_id, quantity_units, received_units) "
                              "VALUES (1, 1, 'MED-A', 2, 3)"),
    ("lot event status", "INSERT INTO pharmacy_lot_events (lot_id, to_status, actor_id, occurred_at) "
                         "VALUES ('L', 'destroyed', 'P1', 'now')"),
    ("lot event self-witness", "INSERT INTO pharmacy_lot_events (lot_id, to_status, actor_id, "
                               "second_actor_id, occurred_at) VALUES ('L', 'recalled', 'P1', 'P1', 'now')"),
    ("count status", "INSERT INTO pharmacy_stock_counts (store_id, status) VALUES (1, 'done')"),
    ("count self-approval", "INSERT INTO pharmacy_stock_counts (store_id, counted_by, approved_by) "
                            "VALUES (1, 'P1', 'P1')"),
    ("ward return status", "INSERT INTO pharmacy_ward_returns (patient_id, helix_order_id, "
                           "helix_medication_id, quantity_units, received_by, status) "
                           "VALUES ('P', 'O', 'M', 1, 'U', 'resold')"),
    ("ward return quantity", "INSERT INTO pharmacy_ward_returns (patient_id, helix_order_id, "
                             "helix_medication_id, quantity_units, received_by) "
                             "VALUES ('P', 'O', 'M', 0, 'U')"),
    ("vendor return status", "INSERT INTO pharmacy_vendor_returns (vendor_id, store_id, status) "
                             "VALUES (1, 1, 'lost')"),
    ("change kind", "INSERT INTO pharmacy_master_change_requests (helix_medication_id, change_kind, "
                    "reason, requested_by) VALUES ('M', 'rename', 'why', 'P1')"),
    ("change self-approval", "INSERT INTO pharmacy_master_change_requests (helix_medication_id, "
                             "change_kind, reason, requested_by, decision, decided_by) "
                             "VALUES ('M', 'classification_downgrade', 'why', 'P1', 'approved', 'P1')"),
    ("change without reason", "INSERT INTO pharmacy_master_change_requests (helix_medication_id, "
                              "change_kind, requested_by) VALUES ('M', 'retire', 'P1')"),
    ("variance kind", "INSERT INTO ndps_variances (lane, helix_medication_id, variance_kind, "
                      "raised_by, raised_at) VALUES ('live', 'M', 'theft', 'P1', 'now')"),
    ("variance closed without a note", "INSERT INTO ndps_variances (lane, helix_medication_id, "
                                       "variance_kind, raised_by, raised_at, status, reconciled_by) "
                                       "VALUES ('live', 'M', 'count_mismatch', 'P1', 'now', "
                                       "'reconciled', 'P2')"),
    ("register lane", "INSERT INTO ndps_register (lane, store_id, helix_medication_id, seq, "
                      "entry_type, quantity_units, balance_after_units, actor_id, occurred_at) "
                      "VALUES ('test', 1, 'M', 1, 'receipt', 5, 5, 'P1', 'now')"),
    ("register entry type", "INSERT INTO ndps_register (lane, store_id, helix_medication_id, seq, "
                            "entry_type, quantity_units, balance_after_units, actor_id, occurred_at) "
                            "VALUES ('live', 1, 'M', 1, 'loan', 5, 5, 'P1', 'now')"),
    ("register negative balance", "INSERT INTO ndps_register (lane, store_id, helix_medication_id, "
                                  "seq, entry_type, quantity_units, balance_after_units, patient_id, "
                                  "helix_order_id, actor_id, occurred_at) VALUES ('live', 1, 'M', 1, "
                                  "'dispense', -1, -1, 'P', 'O', 'P1', 'now')"),
    ("register self-witness", "INSERT INTO ndps_register (lane, store_id, helix_medication_id, seq, "
                              "entry_type, quantity_units, balance_after_units, actor_id, witness_id, "
                              "occurred_at) VALUES ('live', 1, 'M', 1, 'wastage', -1, 0, 'P1', 'P1', "
                              "'now')"),
    ("register dispense without a patient", "INSERT INTO ndps_register (lane, store_id, "
                                            "helix_medication_id, seq, entry_type, quantity_units, "
                                            "balance_after_units, actor_id, occurred_at) VALUES "
                                            "('live', 1, 'M', 1, 'dispense', -1, 0, 'P1', 'now')"),
]


@pytest.mark.parametrize("label,sql", BAD_VALUES, ids=[b[0] for b in BAD_VALUES])
def test_a_bad_value_is_refused(legacy, label, sql):
    _migrate(legacy)
    with pytest.raises(DBIntegrityError):
        legacy.execute(sql)


def test_good_rows_are_accepted(legacy):
    """The other side of every CHECK above: a well-formed row of each kind lands."""
    _migrate(legacy)
    for sql in (
        "INSERT INTO stores (code, name, pharmacy_kind) VALUES ('PH-DISP', 'Pharmacy', 'dispensing')",
        "INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units) VALUES (2, 'M', 10)",
        "INSERT INTO pharmacy_transfers (from_store_id, to_store_id, dispatched_by, received_by) "
        "VALUES (1, 2, 'P1', 'P2')",
        "INSERT INTO pharmacy_lot_events (lot_id, from_status, to_status, actor_id, occurred_at) "
        "VALUES ('L', 'available', 'quarantined', 'P1', 'now')",
        "INSERT INTO pharmacy_master_change_requests (helix_medication_id, change_kind, reason, "
        "requested_by, decision, decided_by) VALUES ('M', 'classification_downgrade', 'why', "
        "'P1', 'approved', 'P2')",
        "INSERT INTO pharmacy_master_change_requests (helix_medication_id, change_kind, "
        "requested_by, decision, decided_by) VALUES ('M2', 'identity_create', 'P1', 'withdrawn', 'P1')",
        "INSERT INTO ndps_register (lane, store_id, helix_medication_id, seq, entry_type, "
        "quantity_units, balance_after_units, actor_id, occurred_at) "
        "VALUES ('live', 2, 'M', 1, 'opening', 0, 0, 'P1', 'now')",
        "INSERT INTO ndps_register (lane, store_id, helix_medication_id, seq, entry_type, "
        "quantity_units, balance_after_units, actor_id, witness_id, occurred_at) "
        "VALUES ('live', 2, 'M', 2, 'receipt', 1, 1, 'P1', 'P2', 'now')",
        "INSERT INTO ndps_register (lane, store_id, helix_medication_id, seq, entry_type, "
        "quantity_units, balance_after_units, patient_id, helix_order_id, actor_id, occurred_at) "
        "VALUES ('live', 2, 'M', 3, 'dispense', -1, 0, 'P', 'O', 'P1', 'now')",
    ):
        legacy.execute(sql)


def test_only_one_active_dispensing_store(legacy):
    _migrate(legacy)
    legacy.execute("INSERT INTO stores (code, name, pharmacy_kind) VALUES ('D1', 'One', 'dispensing')")
    with pytest.raises(DBIntegrityError):
        legacy.execute("INSERT INTO stores (code, name, pharmacy_kind) VALUES ('D2', 'Two', 'dispensing')")
    legacy.execute("UPDATE stores SET is_active = 0 WHERE code = 'D1'")
    legacy.execute("INSERT INTO stores (code, name, pharmacy_kind) VALUES ('D2', 'Two', 'dispensing')")


def test_one_open_change_request_per_drug_kind_and_field(legacy):
    _migrate(legacy)
    sql = ("INSERT INTO pharmacy_master_change_requests (helix_medication_id, change_kind, field, "
           "reason, requested_by) VALUES ('M', 'classification_downgrade', ?, 'why', 'P1')")
    legacy.execute(sql, ("ndps_category",))
    with pytest.raises(DBIntegrityError):
        legacy.execute(sql, ("ndps_category",))
    legacy.execute(sql, ("drug_schedule",))
    retire = ("INSERT INTO pharmacy_master_change_requests (helix_medication_id, change_kind, "
              "reason, requested_by) VALUES ('M', 'retire', 'why', 'P1')")
    legacy.execute(retire)
    with pytest.raises(DBIntegrityError):
        legacy.execute(retire)


def test_two_lots_of_one_batch_in_one_store_are_refused_but_legacy_lots_are_not(legacy):
    _migrate(legacy)
    # Same drug, batch and expiry as L-SEEDED: legal while both are legacy
    # (store NULL), refused once both sit in one store.
    _lot(legacy, "L-DUP", "MED-A", 3)
    legacy.execute("UPDATE helix_inventory_lots SET lot_number = 'B-L-SEEDED' WHERE id = 'L-DUP'")
    legacy.execute("UPDATE helix_inventory_lots SET store_id = 7 WHERE id IN ('L-SEEDED')")
    with pytest.raises(DBIntegrityError):
        legacy.execute("UPDATE helix_inventory_lots SET store_id = 7 WHERE id = 'L-DUP'")


# ═════════════════════════════════════════════════════════════════════════════
#  The NDPS register is append-only by trigger
# ═════════════════════════════════════════════════════════════════════════════

def _register(conn, lane, seq=1):
    conn.execute(
        "INSERT INTO ndps_register (lane, store_id, helix_medication_id, seq, entry_type, "
        "quantity_units, balance_after_units, actor_id, occurred_at) "
        "VALUES (?, 1, 'MORPHINE', ?, 'receipt', 10, 10, 'PH1', ?)",
        (lane, seq, "2026-10-04T06:00:00.000000+00:00"))
    return conn.execute("SELECT MAX(id) FROM ndps_register").fetchone()[0]


@pytest.mark.parametrize("lane", ["live", "non_production"])
def test_the_register_refuses_every_update(legacy, lane):
    _migrate(legacy)
    row_id = _register(legacy, lane)
    with pytest.raises(DBIntegrityError, match="append-only"):
        legacy.execute("UPDATE ndps_register SET remarks = 'edited' WHERE id = ?", (row_id,))


def test_the_register_refuses_to_delete_a_live_row(legacy):
    _migrate(legacy)
    row_id = _register(legacy, "live")
    with pytest.raises(DBIntegrityError, match="statutory"):
        legacy.execute("DELETE FROM ndps_register WHERE id = ?", (row_id,))
    assert legacy.execute("SELECT COUNT(*) FROM ndps_register").fetchone()[0] == 1


def test_the_register_lets_a_non_production_row_go(legacy):
    _migrate(legacy)
    live = _register(legacy, "live", seq=1)
    dev = _register(legacy, "non_production", seq=2)
    legacy.execute("DELETE FROM ndps_register WHERE lane = 'non_production'")
    assert [r[0] for r in legacy.execute("SELECT id FROM ndps_register")] == [live]
    assert dev != live


def test_the_reseed_reads_the_register_as_conditionally_protected(legacy):
    """scripts/reset_db_and_seed.delete_protected_tables reads the triggers:
    the no-update trigger is not a delete guard, and the delete guard is
    conditional, so the reseed's predicate applies."""
    from scripts.reset_db_and_seed import _CONDITIONAL_DELETE_PREDICATES, delete_protected_tables

    _migrate(legacy)
    protection = delete_protected_tables(legacy)
    assert protection["ndps_register"] == "lane = 'non_production'"
    assert _CONDITIONAL_DELETE_PREDICATES["ndps_register"] == "lane = 'non_production'"
