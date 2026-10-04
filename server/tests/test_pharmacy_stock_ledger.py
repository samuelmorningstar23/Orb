"""The drug stock ledger's one writer: services/pharmacy_stock_service (grn-4).

Most of this runs against a database built in tmp_path with exactly the schema
a live appliance has (migration 20's stores, migration 49's Helix tables,
migration 54's pharmacy columns), so lots and stores here never touch the
shared test database and a dispensing store can be created freely. The tests
that go through helix_service's own sessions (pre-flight, the formulary list)
use the shared database and remove every store they add.

Pinned:
  * a quantity changes only by a relative, guarded update, and every change is
    a typed, sequenced ledger row whose balance_after equals the lot;
  * outside a transaction nothing is written; below zero nothing is written;
    an idempotent replay writes nothing;
  * the dispensable predicate: quarantined, sub-store and returns-quarantine
    lots, and lots of an inactive store, are never picked or counted; an NDPS
    vault lot is dispensable for a narcotic and not for anything else;
  * lots open at zero and fill by a movement; a batch merges, never across
    statuses;
  * lot status rules (GRN-RS2 and GRN-RS3 strict defaults);
  * adoption of legacy lots, merging duplicates by paired movements;
  * reconcile() is clean after service writes and loud about everything else;
  * a full Helix seed plus the pharmacy queue seed reconciles clean.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from fnmatch import fnmatchcase

import pytest

import migrations
from database import db_session
from services import helix_service
from services import pharmacy_stock_service as stock
from services.clinical_time import CANONICAL_GLOB

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATER = (date.today() + timedelta(days=400)).isoformat()
SOON = (date.today() + timedelta(days=40)).isoformat()
PAST = (date.today() - timedelta(days=3)).isoformat()


# ═════════════════════════════════════════════════════════════════════════════
#  An isolated database with the live schema
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "stock.db"))
    conn.row_factory = sqlite3.Row
    migrations._m20_hospital_admin_spine(conn)
    importlib.import_module("migrations_his.m0049_helix_tables_in_runner").MIGRATION.apply(conn)
    importlib.import_module("migrations_his.m0054_pharmacy_store_schema").MIGRATION.apply(conn)
    conn.commit()
    yield conn
    conn.close()


@contextmanager
def txn(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def drug(conn, med_id="MED-A", *, ndps=None, storage=None, generic="amoxicillin"):
    conn.execute(
        "INSERT INTO helix_medication_master (id, name, generic_name, form, strength, "
        "ndps_category, storage_condition) VALUES (?, ?, ?, 'tablet', '500 mg', ?, ?)",
        (med_id, f"Drug {med_id}", generic, ndps, storage))
    conn.commit()
    return med_id


def store(conn, kind, *, code=None, active=1):
    code = code or f"S{uuid.uuid4().hex[:6].upper()}"
    cur = conn.execute(
        "INSERT INTO stores (code, name, store_type, is_active, pharmacy_kind) "
        "VALUES (?, ?, 'sub_store', ?, ?)", (code, f"Store {code}", active, kind))
    conn.commit()
    return cur.lastrowid


def opened(conn, med_id, units, *, expiry=LATER, store_id=None, status="available",
           batch=None, lot_id=None):
    with txn(conn):
        return stock.open_or_merge_lot(
            conn, medication_id=med_id, lot_number=batch or f"B{uuid.uuid4().hex[:6]}",
            expiration_date=expiry, units=units, actor_id="tester", store_id=store_id,
            status=status, lot_id=lot_id)["lot"]["id"]


def legacy_lot(conn, med_id, units, *, batch, expiry=LATER):
    """A lot as the code before the ledger could leave it: a second row for a
    batch another lot already holds (open_or_merge_lot would merge it). Its
    units arrive through the ledger, so reconcile() stays the judge."""
    lot_id = f"LEG-{uuid.uuid4().hex[:8].upper()}"
    with txn(conn):
        conn.execute("INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, "
                     "expiration_date) VALUES (?, ?, ?, 0, ?)", (lot_id, med_id, batch, expiry))
        if units:
            stock.post_movement(conn, lot_id=lot_id, movement_type="opening",
                                quantity_change=units, actor_id="system")
    return lot_id


def qty(conn, lot_id):
    return conn.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                        (lot_id,)).fetchone()[0]


def movements(conn, lot_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM helix_stock_movements WHERE lot_id = ? ORDER BY seq", (lot_id,))]


# ═════════════════════════════════════════════════════════════════════════════
#  post_movement
# ═════════════════════════════════════════════════════════════════════════════

def test_a_lot_opens_at_zero_and_fills_by_one_opening_movement(db):
    med = drug(db)
    lot = opened(db, med, 12)
    rows = movements(db, lot)
    assert qty(db, lot) == 12
    assert len(rows) == 1
    row = rows[0]
    assert row["movement_type"] == "opening" and row["reason"] == "opening"
    assert row["quantity_change"] == 12 and row["balance_after_units"] == 12
    assert row["actor_id"] == "tester" and row["seq"] >= 1
    assert fnmatchcase(row["occurred_at"], CANONICAL_GLOB)
    assert row["timestamp"]
    assert stock.reconcile(db)["clean"] is True


def test_a_decrement_is_relative_sequenced_and_balanced(db):
    med = drug(db)
    lot = opened(db, med, 3)
    with txn(db):
        first = stock.post_movement(db, lot_id=lot, movement_type="dispense",
                                    quantity_change=-1, actor_id="pharm")
        second = stock.post_movement(db, lot_id=lot, movement_type="adjustment",
                                     quantity_change=-1, actor_id="pharm", remarks="broken")
    assert qty(db, lot) == 1
    assert (first["balance_after_units"], second["balance_after_units"]) == (2, 1)
    assert second["seq"] == first["seq"] + 1
    assert first["store_id"] is None and first["medication_id"] == med
    assert stock.reconcile(db, lot_ids=[lot])["clean"] is True


def test_nothing_is_written_outside_a_transaction(db):
    med = drug(db)
    lot = opened(db, med, 5)
    assert not db.in_transaction
    with pytest.raises(RuntimeError, match="inside the caller's transaction"):
        stock.post_movement(db, lot_id=lot, movement_type="dispense", quantity_change=-1,
                            actor_id="pharm")
    with pytest.raises(RuntimeError):
        stock.open_or_merge_lot(db, medication_id=med, lot_number="X", expiration_date=LATER,
                                units=1, actor_id="pharm")
    assert qty(db, lot) == 5 and len(movements(db, lot)) == 1


def test_below_zero_is_refused_and_changes_nothing(db):
    med = drug(db)
    lot = opened(db, med, 1)
    with txn(db):
        with pytest.raises(stock.InsufficientStock):
            stock.post_movement(db, lot_id=lot, movement_type="dispense", quantity_change=-2,
                                actor_id="pharm")
    assert qty(db, lot) == 1 and len(movements(db, lot)) == 1


def test_an_idempotent_replay_writes_no_second_movement(db):
    med = drug(db)
    lot = opened(db, med, 4)
    with txn(db):
        first = stock.post_movement(db, lot_id=lot, movement_type="dispense",
                                    quantity_change=-1, actor_id="pharm",
                                    idempotency_key="order-1:dispense")
        again = stock.post_movement(db, lot_id=lot, movement_type="dispense",
                                    quantity_change=-1, actor_id="pharm",
                                    idempotency_key="order-1:dispense")
    assert again["replayed"] is True and again["id"] == first["id"]
    assert qty(db, lot) == 3 and len(movements(db, lot)) == 2
    with txn(db):
        with pytest.raises(stock.StockError) as exc:
            stock.post_movement(db, lot_id=lot, movement_type="dispense", quantity_change=-2,
                                actor_id="pharm", idempotency_key="order-1:dispense")
    assert exc.value.code == "idempotency_conflict"


@pytest.mark.parametrize("kind, change, code", [
    ("teleport", -1, "bad_movement_type"),
    ("dispense", 1, "bad_quantity"),
    ("receipt", -1, "bad_quantity"),
    ("adjustment", 0, "bad_quantity"),
])
def test_movement_types_and_directions_are_validated(db, kind, change, code):
    med = drug(db)
    lot = opened(db, med, 5)
    with txn(db):
        with pytest.raises(stock.StockError) as exc:
            stock.post_movement(db, lot_id=lot, movement_type=kind, quantity_change=change,
                                actor_id="pharm")
    assert exc.value.code == code
    assert qty(db, lot) == 5


def test_a_movement_hook_runs_in_the_same_transaction_and_can_roll_it_back(db):
    med = drug(db)
    lot = opened(db, med, 5)
    seen = []
    stock.register_movement_hook("test", lambda conn, mv: seen.append(
        (conn.in_transaction, mv["movement_type"], mv["quantity_change"])))
    try:
        with txn(db):
            stock.post_movement(db, lot_id=lot, movement_type="dispense", quantity_change=-1,
                                actor_id="pharm")
        assert seen == [(True, "dispense", -1)]

        def broken(conn, movement):
            raise RuntimeError("register unavailable")
        stock.register_movement_hook("test", broken)
        with pytest.raises(RuntimeError, match="register unavailable"):
            with txn(db):
                stock.post_movement(db, lot_id=lot, movement_type="dispense",
                                    quantity_change=-1, actor_id="pharm")
        assert qty(db, lot) == 4 and len(movements(db, lot)) == 2
    finally:
        stock.register_movement_hook("test", None)
    assert "test" not in stock.registered_movement_hooks()


# ═════════════════════════════════════════════════════════════════════════════
#  The dispensable predicate
# ═════════════════════════════════════════════════════════════════════════════

def test_legacy_lots_behave_exactly_as_before(db):
    """No store, status available: in date, FEFO, one unit."""
    med = drug(db)
    later = opened(db, med, 5, expiry=LATER)
    sooner = opened(db, med, 5, expiry=SOON)
    opened(db, med, 50, expiry=PAST)
    undated = opened(db, med, 0, expiry=None)
    assert stock.pick_fefo_lot(db, med)["id"] == sooner
    assert stock.dispensable_units(db, med) == 10
    assert stock.undispensable_shelf_units(db, med) == 50
    assert undated and later


@pytest.mark.parametrize("kind", ["sub_store", "returns_quarantine"])
def test_stock_off_the_dispensing_shelf_is_never_picked_or_counted(db, kind):
    med = drug(db)
    elsewhere = store(db, kind)
    opened(db, med, 30, store_id=elsewhere)
    assert stock.pick_fefo_lot(db, med) is None
    assert stock.dispensable_units(db, med) == 0
    assert stock.undispensable_shelf_units(db, med) == 0


def test_a_quarantined_lot_is_never_picked_or_counted(db):
    med = drug(db)
    lot = opened(db, med, 30)
    with txn(db):
        stock.set_lot_status(db, lot, "quarantined", actor_id="p1", reason="damp cartons")
    assert stock.pick_fefo_lot(db, med) is None
    assert stock.dispensable_units(db, med) == 0


def test_the_active_dispensing_store_is_the_shelf_and_an_inactive_one_is_not(db):
    med = drug(db)
    shelf = store(db, "dispensing")
    lot = opened(db, med, 8, store_id=shelf)
    assert stock.pick_fefo_lot(db, med)["id"] == lot
    db.execute("UPDATE stores SET is_active = 0 WHERE id = ?", (shelf,))
    db.commit()
    assert stock.pick_fefo_lot(db, med) is None
    assert stock.dispensable_units(db, med) == 0


def test_an_ndps_vault_lot_is_dispensable_only_for_a_controlled_drug(db):
    narcotic = drug(db, "MED-MORPH", ndps="narcotic", generic="morphine")
    plain = drug(db, "MED-PARA", ndps="none", generic="paracetamol")
    unclassified = drug(db, "MED-NULL", generic="unknown")
    vault = store(db, "ndps_vault")
    morph_lot = opened(db, narcotic, 10, store_id=vault)
    opened(db, plain, 10, store_id=vault)
    opened(db, unclassified, 10, store_id=vault)
    assert stock.pick_fefo_lot(db, narcotic)["id"] == morph_lot
    assert stock.dispensable_units(db, narcotic) == 10
    assert stock.dispensable_units(db, plain) == 0
    assert stock.dispensable_units(db, unclassified) == 0


def test_a_named_lot_must_qualify_and_hold_enough(db):
    med = drug(db)
    lot = opened(db, med, 2)
    assert stock.pick_fefo_lot(db, med, 2, lot_id=lot)["id"] == lot
    assert stock.pick_fefo_lot(db, med, 3, lot_id=lot) is None
    other = drug(db, "MED-B")
    assert stock.pick_fefo_lot(db, other, 1, lot_id=lot) is None


# ═════════════════════════════════════════════════════════════════════════════
#  open_or_merge_lot
# ═════════════════════════════════════════════════════════════════════════════

def test_the_same_batch_in_the_same_store_merges(db):
    med = drug(db)
    sub = store(db, "sub_store")
    first = opened(db, med, 5, store_id=sub, batch="B1")
    again = opened(db, med, 7, store_id=sub, batch="B1")
    assert again == first and qty(db, first) == 12
    assert [m["quantity_change"] for m in movements(db, first)] == [5, 7]
    elsewhere = opened(db, med, 1, store_id=store(db, "sub_store"), batch="B1")
    assert elsewhere != first


def test_a_batch_is_never_merged_across_statuses(db):
    med = drug(db)
    sub = store(db, "returns_quarantine")
    opened(db, med, 2, store_id=sub, batch="R1", status="pending_inspection")
    with pytest.raises(stock.StockError) as exc:
        opened(db, med, 2, store_id=sub, batch="R1", status="available")
    assert exc.value.code == "lot_status_conflict"


def test_lot_ids_and_batches_are_not_reused(db):
    med = drug(db)
    sub = store(db, "sub_store")
    opened(db, med, 1, store_id=sub, batch="B1", lot_id="LOT-X")
    with pytest.raises(stock.StockError) as exc:
        opened(db, med, 1, store_id=sub, batch="B2", lot_id="LOT-X")
    assert exc.value.code == "lot_id_taken"
    with pytest.raises(stock.StockError) as exc:
        opened(db, med, 1, store_id=sub, batch="B1", lot_id="LOT-Y")
    assert exc.value.code == "batch_exists"


def test_bad_lot_input_is_refused(db):
    med = drug(db)
    for kwargs, code in (({"expiration_date": "31/12/2030"}, "bad_date"),
                         ({"lot_number": " "}, "missing_batch"),
                         ({"medication_id": "MED-NOPE"}, "drug_not_found"),
                         ({"store_id": 999}, "store_not_found"),
                         ({"status": "lost"}, "bad_status"),
                         ({"movement_type": "dispense"}, "bad_movement_type")):
        args = {"medication_id": med, "lot_number": "B", "expiration_date": LATER,
                "units": 1, "actor_id": "p"} | kwargs
        with txn(db):
            with pytest.raises(stock.StockError) as exc:
                stock.open_or_merge_lot(db, **args)
        assert exc.value.code == code


# ═════════════════════════════════════════════════════════════════════════════
#  Lot status
# ═════════════════════════════════════════════════════════════════════════════

def test_a_quarantined_lot_is_never_released_until_grn_rs2_is_ruled(db):
    """Strictest GRN-RS2 option (c): nobody releases a quarantined lot."""
    assert stock.QUARANTINE_RELEASE_ALLOWED is False
    med = drug(db)
    lot = opened(db, med, 4)
    with txn(db):
        stock.set_lot_status(db, lot, "quarantined", actor_id="p1", reason="seal broken")
    for person in ("p1", "p2"):
        with txn(db):
            with pytest.raises(stock.StockError) as exc:
                stock.set_lot_status(db, lot, "available", actor_id=person, reason="inspected")
        assert exc.value.code == "release_not_allowed"
    assert db.execute("SELECT status FROM helix_inventory_lots WHERE id = ?",
                      (lot,)).fetchone()[0] == "quarantined"
    assert stock.dispensable_units(db, med) == 0
    # It can still leave the shelf for good, once its units have gone.
    with txn(db):
        stock.post_movement(db, lot_id=lot, movement_type="write_off", quantity_change=-4,
                            actor_id="p1")
        stock.set_lot_status(db, lot, "written_off", actor_id="p1", reason="seal broken")


def test_with_release_ruled_b_another_person_releases(db, monkeypatch):
    """GRN-RS2 option (b), one switch away: a different person releases."""
    monkeypatch.setattr(stock, "QUARANTINE_RELEASE_ALLOWED", True)
    med = drug(db)
    lot = opened(db, med, 4)
    with txn(db):
        stock.set_lot_status(db, lot, "quarantined", actor_id="p1", reason="seal broken")
    with txn(db):
        with pytest.raises(stock.StockError) as exc:
            stock.set_lot_status(db, lot, "available", actor_id="p1", reason="looked fine")
    assert exc.value.code == "same_person_release"
    with txn(db):
        out = stock.set_lot_status(db, lot, "available", actor_id="p2", reason="inspected")
    assert out["lot"]["status"] == "available"
    events = [dict(r) for r in db.execute(
        "SELECT from_status, to_status, actor_id, reason FROM pharmacy_lot_events "
        "WHERE lot_id = ? ORDER BY id", (lot,))]
    assert events == [
        {"from_status": "available", "to_status": "quarantined", "actor_id": "p1",
         "reason": "seal broken"},
        {"from_status": "quarantined", "to_status": "available", "actor_id": "p2",
         "reason": "inspected"},
    ]


@pytest.mark.parametrize("terminal", ["recalled", "nsq"])
def test_recalled_and_nsq_lots_never_return_to_the_shelf(db, terminal):
    med = drug(db)
    lot = opened(db, med, 4)
    with txn(db):
        stock.set_lot_status(db, lot, terminal, actor_id="p1", reason="notice")
    with txn(db):
        with pytest.raises(stock.StockError) as exc:
            stock.set_lot_status(db, lot, "available", actor_id="p2", reason="mistake")
    assert exc.value.code == "illegal_lot_transition"


def test_a_lot_is_closed_only_once_its_units_have_left(db):
    med = drug(db)
    lot = opened(db, med, 2)
    with txn(db):
        with pytest.raises(stock.StockError) as exc:
            stock.set_lot_status(db, lot, "written_off", actor_id="p1", reason="expired")
    assert exc.value.code == "lot_not_empty"
    with txn(db):
        stock.post_movement(db, lot_id=lot, movement_type="write_off", quantity_change=-2,
                            actor_id="p1")
        stock.set_lot_status(db, lot, "written_off", actor_id="p1", reason="expired")
    assert stock.reconcile(db, lot_ids=[lot])["clean"] is True


def test_a_return_is_never_restocked_until_grn_rs3_is_ruled(db):
    """Strictest GRN-RS3 option (c): even a sealed, in-date return stays off
    the shelf."""
    assert stock.RETURN_RESTOCK_ALLOWED is False
    med = drug(db, ndps="none", storage="room_temperature")
    lot = opened(db, med, 1, expiry=LATER, status="pending_inspection",
                 store_id=store(db, "returns_quarantine"))
    with txn(db):
        with pytest.raises(stock.StockError) as exc:
            stock.set_lot_status(db, lot, "available", actor_id="p1", reason="sealed")
    assert exc.value.code == "restock_not_allowed"
    assert db.execute("SELECT status FROM helix_inventory_lots WHERE id = ?",
                      (lot,)).fetchone()[0] == "pending_inspection"


def test_with_restock_ruled_b_a_good_return_goes_back(db, monkeypatch):
    monkeypatch.setattr(stock, "RETURN_RESTOCK_ALLOWED", True)
    med = drug(db, ndps="none", storage="room_temperature")
    lot = opened(db, med, 1, expiry=LATER, status="pending_inspection",
                 store_id=store(db, "returns_quarantine"))
    with txn(db):
        out = stock.set_lot_status(db, lot, "available", actor_id="p1", reason="sealed")
    assert out["lot"]["status"] == "available"


@pytest.mark.parametrize("ndps, storage, expiry", [
    ("narcotic", None, LATER),
    ("none", "refrigerated", LATER),
    ("none", None, PAST),
])
def test_a_return_is_never_restocked_if_controlled_cold_chain_or_expired(db, monkeypatch, ndps,
                                                                         storage, expiry):
    # Even with GRN-RS3 ruled (b), these never go back on the shelf.
    monkeypatch.setattr(stock, "RETURN_RESTOCK_ALLOWED", True)
    med = drug(db, ndps=ndps, storage=storage)
    lot = opened(db, med, 1, expiry=expiry, status="pending_inspection",
                 store_id=store(db, "returns_quarantine"))
    with txn(db):
        with pytest.raises(stock.StockError) as exc:
            stock.set_lot_status(db, lot, "available", actor_id="p1", reason="sealed")
    assert exc.value.code == "restock_refused"


def test_a_status_change_needs_a_reason_and_a_real_second_person(db):
    med = drug(db)
    lot = opened(db, med, 1)
    with txn(db):
        with pytest.raises(stock.StockError) as exc:
            stock.set_lot_status(db, lot, "quarantined", actor_id="p1", reason="  ")
        assert exc.value.code == "missing_reason"
        with pytest.raises(stock.StockError) as exc:
            stock.set_lot_status(db, lot, "quarantined", actor_id="p1", reason="x",
                                 second_actor_id="p1")
        assert exc.value.code == "same_person"


# ═════════════════════════════════════════════════════════════════════════════
#  Adoption of legacy lots
# ═════════════════════════════════════════════════════════════════════════════

def test_adoption_moves_every_legacy_lot_and_merges_duplicates_by_movements(db):
    a, b = drug(db, "MED-A"), drug(db, "MED-B")
    keep = legacy_lot(db, a, 10, batch="DUP")
    dup = legacy_lot(db, a, 4, batch="DUP")      # same drug, batch, expiry
    single = opened(db, b, 6, batch="ONE")
    empty = opened(db, b, 0, batch="ZERO")
    shelf = store(db, "dispensing")
    with txn(db):
        result = stock.adopt_legacy_lots(db, shelf, actor_id="admin")
    assert sorted(result["adopted"]) == sorted([keep, single, empty])
    assert result["merged"] == [{"from": dup, "into": keep, "units": 4}]
    assert result["conflicts"] == []
    assert qty(db, keep) == 14 and qty(db, dup) == 0
    assert db.execute("SELECT store_id FROM helix_inventory_lots WHERE id = ?",
                      (keep,)).fetchone()[0] == shelf
    assert [m["movement_type"] for m in movements(db, dup)] == ["opening", "transfer_out"]
    assert [m["movement_type"] for m in movements(db, keep)] == ["opening", "transfer_in"]
    assert stock.reconcile(db)["clean"] is True
    assert stock.dispensable_units(db, a) == 14
    # The emptied duplicate stays as history, with an event naming its lot.
    event = db.execute("SELECT reason, reference_id FROM pharmacy_lot_events WHERE lot_id = ?",
                       (dup,)).fetchone()
    assert event["reference_id"] == keep and keep in event["reason"]
    # The emptied duplicate is history now, not a lot waiting for a store.
    waiting = db.execute(f"SELECT id FROM helix_inventory_lots l "
                         f"WHERE {stock.awaiting_adoption_sql('l')}").fetchall()
    assert waiting == []
    # A second adoption has nothing to move, and writes no second merge event.
    with txn(db):
        again = stock.adopt_legacy_lots(db, shelf, actor_id="admin")
    assert again["adopted"] == [] and again["merged"] == [] and again["conflicts"] == []
    assert db.execute("SELECT COUNT(*) FROM pharmacy_lot_events WHERE lot_id = ?",
                      (dup,)).fetchone()[0] == 1
    assert stock.reconcile(db)["clean"] is True


def test_adoption_never_merges_a_batch_held_in_two_statuses(db):
    med = drug(db)
    good = legacy_lot(db, med, 5, batch="SPLIT")
    bad = legacy_lot(db, med, 5, batch="SPLIT")
    with txn(db):
        stock.set_lot_status(db, bad, "quarantined", actor_id="p1", reason="leaking")
    shelf = store(db, "dispensing")
    with txn(db):
        result = stock.adopt_legacy_lots(db, shelf, actor_id="admin")
    assert result["adopted"] == [] and result["merged"] == []
    assert result["conflicts"][0]["lot_ids"] == [good, bad]
    assert (qty(db, good), qty(db, bad)) == (5, 5)


def test_only_the_active_dispensing_store_adopts(db):
    for kind, active in (("sub_store", 1), ("dispensing", 0)):
        target = store(db, kind, active=active)
        with txn(db):
            with pytest.raises(stock.StockError) as exc:
                stock.adopt_legacy_lots(db, target, actor_id="admin")
        assert exc.value.code == "not_dispensing_store"


# ═════════════════════════════════════════════════════════════════════════════
#  reconcile
# ═════════════════════════════════════════════════════════════════════════════

def test_reconcile_reports_every_kind_of_drift(db):
    med = drug(db)
    lot = opened(db, med, 5)
    assert stock.reconcile(db)["clean"] is True
    # A write that bypassed the service: the quantity no longer matches.
    db.execute("UPDATE helix_inventory_lots SET quantity = 9, status = 'misplaced' WHERE id = ?",
               (lot,))
    db.execute("INSERT INTO helix_stock_movements (id, lot_id, medication_id, quantity_change, "
               "reason) VALUES ('raw-1', ?, ?, 0, 'restock')", (lot, med))
    db.execute("UPDATE helix_medication_master SET drug_schedule = 'Z' WHERE id = ?", (med,))
    db.commit()
    report = stock.reconcile(db)
    assert report["clean"] is False
    assert report["mismatched_lots"] == [{"lot_id": lot, "medication_id": med, "quantity": 9,
                                          "ledger_units": 5}]
    assert report["bad_lot_statuses"] == [{"lot_id": lot, "status": "misplaced"}]
    assert report["untyped_movements"] == 1 and report["unsequenced_movements"] == 1
    assert report["bad_master_values"] == [{"medication_id": med, "field": "drug_schedule",
                                            "value": "Z"}]


def test_the_enumerations_are_the_documented_sets():
    assert stock.LOT_STATUSES == ("available", "quarantined", "recalled", "nsq",
                                  "pending_inspection", "written_off", "returned_to_vendor")
    assert stock.DRUG_SCHEDULES == ("none", "G", "H", "H1", "X")
    assert stock.NDPS_CATEGORIES == ("none", "narcotic", "psychotropic")
    assert stock.IDENTITY_STATUSES == ("draft", "active", "retired")
    assert set(stock.INWARD_MOVEMENT_TYPES) | set(stock.OUTWARD_MOVEMENT_TYPES) | {
        "adjustment", "count_variance"} == set(stock.MOVEMENT_TYPES)
    # Every lot status has its legal moves, and the migration 54 CHECK on
    # pharmacy_lot_events accepts every one of them.
    assert set(stock.LOT_STATUS_TRANSITIONS) == set(stock.LOT_STATUSES)


def test_site_today_is_what_helix_uses():
    assert helix_service._expiry_cutoff() == stock.site_today()


# ═════════════════════════════════════════════════════════════════════════════
#  helix_service through the shared database: pre-flight and the formulary
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def shared_stores():
    """Stores added to the shared test database, removed afterwards with their
    lots, so no other module ever meets them."""
    made: list[int] = []

    def make(kind, active=1):
        code = f"LEDG{uuid.uuid4().hex[:6].upper()}"
        with db_session() as conn:
            made.append(conn.execute(
                "INSERT INTO stores (code, name, store_type, is_active, pharmacy_kind) "
                "VALUES (?, ?, 'sub_store', ?, ?)",
                (code, f"Ledger test {kind}", active, kind)).lastrowid)
        return made[-1]

    yield make
    with db_session() as conn:
        for store_id in made:
            lots = [r[0] for r in conn.execute(
                "SELECT id FROM helix_inventory_lots WHERE store_id = ?", (store_id,))]
            for lot_id in lots:
                conn.execute("DELETE FROM helix_stock_movements WHERE lot_id = ?", (lot_id,))
                conn.execute("DELETE FROM pharmacy_lot_events WHERE lot_id = ?", (lot_id,))
                conn.execute("DELETE FROM helix_inventory_lots WHERE id = ?", (lot_id,))
            conn.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def _shared_drug(ndps=None):
    med = f"MEDLEDG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as conn:
        conn.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength, "
                     "ndps_category) VALUES (?, ?, 'ledgermol', 'tablet', '5 mg', ?)",
                     (med, f"Ledger {med}", ndps))
    return med


def _shared_lot(med, units, store_id=None, status="available"):
    with db_session(immediate=True) as conn:
        return stock.open_or_merge_lot(
            conn, medication_id=med, lot_number=f"B{uuid.uuid4().hex[:6]}",
            expiration_date=LATER, units=units, actor_id="tester", store_id=store_id,
            status=status)["lot"]["id"]


def test_pre_flight_and_the_formulary_count_only_the_shelf(shared_stores):
    med = _shared_drug()
    _shared_lot(med, 30, shared_stores("sub_store"))
    _shared_lot(med, 20, shared_stores("returns_quarantine"))
    quarantined = _shared_lot(med, 10)
    with db_session(immediate=True) as conn:
        stock.set_lot_status(conn, quarantined, "quarantined", actor_id="p1", reason="test")
    assert helix_service.pre_flight_inventory_check(med) == {
        "medication_id": med, "in_stock": False, "total_quantity": 0}
    assert [l for l in helix_service.get_inventory() if l["medication_id"] == med] == []

    shelf = _shared_lot(med, 3)
    assert helix_service.pre_flight_inventory_check(med)["total_quantity"] == 3
    listed = [l for l in helix_service.get_inventory() if l["medication_id"] == med]
    assert [(l["id"], l["in_date"]) for l in listed] == [(shelf, 1)]


def test_pre_flight_counts_a_narcotic_in_the_vault(shared_stores):
    narcotic, plain = _shared_drug("narcotic"), _shared_drug("none")
    vault = shared_stores("ndps_vault")
    _shared_lot(narcotic, 6, vault)
    _shared_lot(plain, 6, vault)
    assert helix_service.pre_flight_inventory_check(narcotic)["total_quantity"] == 6
    assert helix_service.pre_flight_inventory_check(plain)["total_quantity"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  A full Helix seed, then the pharmacy queue seed's dispense, reconciles clean
# ═════════════════════════════════════════════════════════════════════════════

_SEED_SCRIPT = """
import json
from datetime import datetime, timedelta, timezone
import database
database.init_db()
from database import db_session
from scripts.seed_helix import seed_helix

# A pharmacist's own entries on a seeded drug must survive a reseed.
seed_helix()
with db_session() as db:
    db.execute("UPDATE helix_medication_master SET gst_rate_bp = 500, drug_schedule = 'H', "
               "storage_condition = 'cool' WHERE id = 'MED-PARA'")
seed_helix()

admit = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
with db_session() as db:
    for uid, role in (("DOC001", "doctor"), ("DOC002", "doctor"), ("PHA001", "pharmacist")):
        db.execute("INSERT OR REPLACE INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, NULL, 'x')", (uid, "User " + uid, role))
    for i, name in enumerate(("Kavitha Menon", "Ananya Kapoor", "Lakshmi Devi")):
        pid = "PAT-LEDG-%d" % i
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, attending_doctor_id, "
            "diagnosis, admission_date, status, created_at) "
            "VALUES (?, ?, ?, 60, 'Female', 'GM-1', ?, 'DOC001', 'x', ?, 'admitted', ?)",
            (pid, name, "MRN-" + pid, "GM-10%d" % i, admit, admit))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
            "attending_doctor_id, status, created_at) "
            "VALUES (?, ?, 'DOC001', 'General Medicine', 'GM-1', ?, 'DOC001', 'active', ?)",
            ("ADM-" + pid, pid, "GM-10%d" % i, admit))

from scripts.reset_db_and_seed import seed_pharmacy_queue
seed_pharmacy_queue()

from services import pharmacy_stock_service as stock
with db_session() as db:
    report = stock.reconcile(db)
    para = dict(db.execute("SELECT gst_rate_bp, drug_schedule, storage_condition, name "
                           "FROM helix_medication_master WHERE id = 'MED-PARA'").fetchone())
    dispensed = [dict(r) for r in db.execute(
        "SELECT movement_type, quantity_change, balance_after_units, lot_id, order_id "
        "FROM helix_stock_movements WHERE movement_type = 'dispense'").fetchall()]
    monocef = db.execute("SELECT quantity FROM helix_inventory_lots "
                         "WHERE id = 'LOT-MONOCEF-1'").fetchone()[0]
    audits = db.execute("SELECT COUNT(*) FROM audit_logs "
                        "WHERE action_type = 'helix_dispense_recorded'").fetchone()[0]
print("RESULT" + json.dumps({"report": report, "para": para, "dispensed": dispensed,
                              "monocef": monocef, "audits": audits}))
"""


def test_a_full_helix_seed_and_the_queue_dispense_reconcile_clean(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "ORB_DEPLOYMENT_PROFILE"}
    env.update({"ORB_DB_PATH": str(tmp_path / "orb.db"), "ORB_DEPLOYMENT_PROFILE": "dev",
                "PYTHONPATH": SERVER_DIR})
    proc = subprocess.run([sys.executable, "-c", _SEED_SCRIPT], capture_output=True, text=True,
                          env=env, cwd=SERVER_DIR, timeout=300)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    result = json.loads(line[len("RESULT"):])
    assert result["report"]["clean"] is True, result["report"]
    assert result["para"] == {"gst_rate_bp": 500, "drug_schedule": "H",
                              "storage_condition": "cool",
                              "name": "Paracetamol 650mg Oral Tablet"}
    assert len(result["dispensed"]) == 1
    move = result["dispensed"][0]
    assert move["quantity_change"] == -1 and move["lot_id"] == "LOT-MONOCEF-1"
    assert move["balance_after_units"] == result["monocef"] == 29
    assert result["audits"] == 1
