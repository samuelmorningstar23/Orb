"""A reseed must not leave rows pointing at parents it just deleted.

`scripts/reset_db_and_seed.py` derives its wipe set from the live schema instead
of a hand-maintained constant, precisely because the constant went stale and left
orphans. That derivation had one blind spot: it followed only DECLARED foreign
keys, so a table that references a patient-scoped parent through a plain
`<parent>_id` column with no constraint was unreachable. Measured on 2026-09-07,
after a real reseed: 7 of 7 `invoice_lines` rows orphaned against `invoices`, and
56 of 61 `claim_events` rows orphaned against `claims`.

The invariant below is deliberately a PROPERTY over the whole schema rather than
an assertion about those two tables. A list of the tables that happened to be
broken on one day is the same mistake the constant was.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from database import db_session
from scripts.reset_db_and_seed import (  # noqa: E402
    TABLES_TO_KEEP,
    patient_scoped_tables,
)


def _schema(conn):
    names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    cols, fk_target, tables = {}, {}, set()
    for t in names:
        try:
            cols[t] = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
            fks = conn.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
        except Exception:
            continue  # virtual table with no module loaded; holds no rows
        tables.add(t)
        fk_target[t] = {r[3]: r[2] for r in fks}   # column -> referenced table
    return tables, cols, fk_target


def _referenced_table(table, col, cols, fk_target, tables):
    """Where `col` points: the declaration if there is one, else the name match.

    A DECLARATION ALWAYS WINS. `helix_inventory_lots.medication_id` reads like
    `medications` (patient-scoped) but declares `helix_medication_master` (not),
    so a bare name match would wipe the pharmacy catalogue on every reseed.
    """
    declared = fk_target.get(table, {}).get(col)
    if declared:
        return declared
    stem = col[:-3]
    for candidate in (stem + "s", stem, stem + "es"):
        if candidate in tables and candidate != table:
            return candidate
    return None


def test_no_surviving_table_references_a_wiped_parent():
    """THE INVARIANT. If a table survives a reseed, nothing it points at may be
    deleted by that reseed, or the surviving rows are orphans the moment it runs.

    This is what catches the NEXT `invoice_lines` without anyone editing a list.
    """
    with db_session() as conn:
        wiped = set(patient_scoped_tables(conn))
        tables, cols, fk_target = _schema(conn)

    offenders = []
    for table in sorted(tables - wiped):
        # audit_logs is the sole member of TABLES_TO_KEEP and is EXEMPT on purpose:
        # a reseed re-seals it rather than wiping it, so audit rows about deleted
        # patients are correct. An audit log that forgot its subject on deletion
        # would be the actual defect.
        if table in TABLES_TO_KEEP:
            continue
        for col in cols.get(table, []):
            if col == "id" or not col.endswith("_id"):
                continue
            parent = _referenced_table(table, col, cols, fk_target, tables)
            if parent in wiped:
                offenders.append(f"{table}.{col} -> {parent}")

    assert not offenders, (
        "these tables SURVIVE a reseed but reference a table it deletes, so every "
        "such row is orphaned the moment the reseed runs:\n  "
        + "\n  ".join(offenders)
        + "\nEither add the table to the wipe set (ideally by making "
          "patient_scoped_tables derive it) or declare the foreign key."
    )


def test_the_known_orphan_tables_are_in_the_wipe_set():
    """The three concrete cases, named so a regression says which one broke.

    payment_allocations held zero rows the day this was found and would have been
    invisible to any data-driven check; it is here because the derivation reaches
    it, which is the point of deriving rather than listing.
    """
    with db_session() as conn:
        wiped = set(patient_scoped_tables(conn))
    for table in ("invoice_lines", "claim_events", "payment_allocations"):
        assert table in wiped, f"{table} would survive a reseed and orphan its rows"


def test_a_declared_foreign_key_beats_the_name_match():
    """The safety property that makes the undeclared-reference walk usable.

    `helix_inventory_lots.medication_id` name-matches `medications`, which IS
    wiped. Only its declaration against `helix_medication_master` keeps the
    hospital's stock and catalogue out of the wipe set.
    """
    with db_session() as conn:
        wiped = set(patient_scoped_tables(conn))
    assert "helix_inventory_lots" not in wiped
    assert "helix_medication_master" not in wiped


# ── Migration 54: every pharmacy store table, by name ────────────────────────
#
# Each new table's fate is a decision, so each is named. seed_helix deletes
# every drug lot on a reseed, so documents about lots are wiped with them; the
# two masters are kept, as the stores masters are; the NDPS register is wiped
# only where its trigger allows (non-production rows).

PHARMACY_STORE_WIPED = {
    "pharmacy_transfers", "pharmacy_transfer_lines", "pharmacy_lot_events",
    "pharmacy_stock_counts", "pharmacy_stock_count_lines", "pharmacy_ward_returns",
    "pharmacy_vendor_returns", "pharmacy_vendor_return_lines", "ndps_variances",
}
PHARMACY_STORE_KEPT = {"pharmacy_par_levels", "pharmacy_master_change_requests"}


def test_every_pharmacy_store_table_has_its_intended_reseed_fate():
    from migrations_his.m0054_pharmacy_store_schema import TABLE_NAMES
    from scripts.reset_db_and_seed import delete_protected_tables

    assert set(TABLE_NAMES) == PHARMACY_STORE_WIPED | PHARMACY_STORE_KEPT | {"ndps_register"}, (
        "migration 54 creates a table this test does not classify; decide whether a "
        "reseed wipes it and add it here")
    with db_session() as conn:
        wiped = set(patient_scoped_tables(conn))
        protected = delete_protected_tables(conn)
    assert PHARMACY_STORE_WIPED <= wiped, sorted(PHARMACY_STORE_WIPED - wiped)
    assert not (PHARMACY_STORE_KEPT & wiped), sorted(PHARMACY_STORE_KEPT & wiped)
    # The register is in the wipe set (it has a patient_id) but guarded: only
    # its non-production rows may go.
    assert "ndps_register" in wiped
    assert protected["ndps_register"] == "lane = 'non_production'"
    # Lots and the drug master still survive, now with their new columns
    # (store_id names stores, vendor_id names vendors, both kept).
    assert "helix_inventory_lots" not in wiped
    assert "helix_medication_master" not in wiped
    # Pharmacy purchase orders, receipts and indents live in the procurement
    # tables, which a reseed already wipes.
    assert {"purchase_orders", "goods_receipts", "indents"} <= wiped


def test_the_real_wipe_keeps_live_register_rows_and_the_masters(tmp_path):
    """wipe_patient_tables on a scratch database holding the pharmacy schema:
    the live register row and the two masters survive, a development register
    row and every pharmacy document go."""
    import sqlite3

    import migrations
    from migrations_his.m0049_helix_tables_in_runner import MIGRATION as M49
    from migrations_his.m0054_pharmacy_store_schema import MIGRATION as M54
    from scripts.reset_db_and_seed import wipe_patient_tables

    conn = sqlite3.connect(str(tmp_path / "wipe.db"))
    conn.execute("CREATE TABLE patients (id TEXT PRIMARY KEY, name TEXT)")
    migrations._m20_hospital_admin_spine(conn)
    M49.apply(conn)
    M54.apply(conn)
    register = ("INSERT INTO ndps_register (lane, store_id, helix_medication_id, seq, entry_type, "
                "quantity_units, balance_after_units, actor_id, occurred_at) "
                "VALUES (?, 1, 'MORPHINE', ?, 'receipt', 10, 10, 'PH1', 'now')")
    conn.execute(register, ("live", 1))
    conn.execute(register, ("non_production", 2))
    conn.execute("INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units) "
                 "VALUES (1, 'MORPHINE', 10)")
    conn.execute("INSERT INTO pharmacy_master_change_requests (helix_medication_id, change_kind, "
                 "reason, requested_by) VALUES ('MORPHINE', 'retire', 'Withdrawn', 'PH1')")
    conn.execute("INSERT INTO pharmacy_transfers (from_store_id, to_store_id) VALUES (1, 2)")
    conn.execute("INSERT INTO pharmacy_lot_events (lot_id, to_status, actor_id, occurred_at) "
                 "VALUES ('L1', 'quarantined', 'PH1', 'now')")
    conn.execute("INSERT INTO ndps_variances (lane, helix_medication_id, variance_kind, "
                 "raised_by, raised_at) VALUES ('non_production', 'MORPHINE', 'count_mismatch', "
                 "'PH1', 'now')")
    conn.execute("INSERT INTO pharmacy_ward_returns (patient_id, helix_order_id, "
                 "helix_medication_id, quantity_units, received_by) "
                 "VALUES ('P1', 'O1', 'MORPHINE', 1, 'PH1')")
    conn.commit()

    wipe_patient_tables(conn)

    count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    assert [r[0] for r in conn.execute("SELECT lane FROM ndps_register")] == ["live"]
    assert count("pharmacy_par_levels") == 1
    assert count("pharmacy_master_change_requests") == 1
    for table in ("pharmacy_transfers", "pharmacy_lot_events", "ndps_variances",
                  "pharmacy_ward_returns"):
        assert count(table) == 0, table
    conn.close()
