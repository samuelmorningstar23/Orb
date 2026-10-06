"""Pharmacy stock reports (grn-9): /api/pharmacy-store stock, lots, ledger,
reorder, near-expiry, overview and reconcile, behind ORB_PHARMACY_STORE.

Pinned (docs/his_design/grn.json, grn-9 tests):
  * with the module off every stock route answers 404 module_disabled;
  * stock per store and drug splits into dispensable, usable, expired,
    quarantined and in transit, and a sub-store's stock is never dispensable;
  * an unset reorder level reads 'No reorder level set', never 0;
  * reorder counts dispensable (usable) units only, never expired or
    quarantined ones, and adds what is in transit and on order;
  * zero-balance lots are dropped from near-expiry; value at cost and at MRP;
    'Rate not configured' when a lot's cost is NULL;
  * the ledger pages by seq, newest first, and a page read again after new
    movements is the same page; a movement tied to a patient's order shows
    neither the order id nor its remarks.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token, hash_password
from database import db_session
from main import app
from services import pharmacy_stock_reports as reports
from services import pharmacy_stock_service as stock
from services import procurement_service as procurement
from services.helix_db import init_helix_db

client = TestClient(app)
BASE = "/api/pharmacy-store"


def _today() -> date:
    return date.fromisoformat(stock.site_today())


def _days(n: int) -> str:
    return (_today() + timedelta(days=n)).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture(autouse=True)
def module_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)


def _user(role):
    uid = f"USR{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, hash_password("secretpass")))
    return {"id": uid, "role": role,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


@pytest.fixture
def pharmacist():
    return _user("pharmacist")


def _drug(**fields):
    med = f"MEDSR{_uid()}"
    cols = {"id": med, "name": f"Reporttest {med[-6:]} 5 mg", "generic_name": "reportol",
            "form": "tablet", "strength": "5 mg", "drug_schedule": "H",
            "ndps_category": "none", "pack_size_units": 10} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    return med


class _Made(list):
    pass


@pytest.fixture
def stores_made():
    made = _Made()
    yield made
    with db_session() as db:
        for store_id in made:
            for (lot_id,) in db.execute("SELECT id FROM helix_inventory_lots WHERE store_id = ?",
                                        (store_id,)).fetchall():
                db.execute("DELETE FROM helix_stock_movements WHERE lot_id = ?", (lot_id,))
                db.execute("DELETE FROM pharmacy_lot_events WHERE lot_id = ?", (lot_id,))
                db.execute("DELETE FROM helix_inventory_lots WHERE id = ?", (lot_id,))
            db.execute("DELETE FROM pharmacy_par_levels WHERE store_id = ?", (store_id,))
            db.execute("DELETE FROM pharmacy_transfer_lines WHERE pharmacy_transfer_id IN "
                       "(SELECT id FROM pharmacy_transfers WHERE to_store_id = ? "
                       "OR from_store_id = ?)", (store_id, store_id))
            db.execute("DELETE FROM pharmacy_transfers WHERE to_store_id = ? OR from_store_id = ?",
                       (store_id, store_id))
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def _store(made, kind="sub_store") -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(db, code=f"SR{_uid()}", name=f"Report {kind} store",
                                             domain="pharmacy", pharmacy_kind=kind)
    made.append(row["id"])
    return row


@pytest.fixture
def dispensing(stores_made):
    """The active dispensing store: the one already there, or a new one made
    WITHOUT adopting legacy lots (so no other test's stock moves), removed
    afterwards."""
    with db_session() as db:
        row = db.execute("SELECT * FROM stores WHERE pharmacy_kind = 'dispensing' "
                         "AND is_active = 1").fetchone()
    if row is not None:
        return dict(row)
    return _store(stores_made, "dispensing")


def _lot(drug, store_id, units, expiry, *, status="available", cost=None, mrp=None, per_pack=None):
    with db_session(immediate=True) as db:
        out = stock.open_or_merge_lot(db, medication_id=drug, lot_number=f"B{_uid()}",
                                      expiration_date=expiry, units=units, actor_id="tester",
                                      store_id=store_id, status=status, unit_cost_paise=cost,
                                      mrp_pack_paise=mrp, units_per_pack=per_pack)
    return out["lot"]


def _transfer(to_store, from_store, drug, units, *, status="dispatched", received=None):
    with db_session() as db:
        tid = db.execute("INSERT INTO pharmacy_transfers (from_store_id, to_store_id, status) "
                         "VALUES (?, ?, ?)", (from_store, to_store, status)).lastrowid
        db.execute("INSERT INTO pharmacy_transfer_lines (pharmacy_transfer_id, line_no, "
                   "helix_medication_id, quantity_units, received_units) VALUES (?, 1, ?, ?, ?)",
                   (tid, drug, units, received))
    return tid


def _row(rows, drug, store_id):
    hits = [r for r in rows if r["drug_id"] == drug and r["store_id"] == store_id]
    assert len(hits) == 1, hits
    return hits[0]


# ═════════════════════════════════════════════════════════════════════════════
#  The switch and the tier
# ═════════════════════════════════════════════════════════════════════════════

def test_every_stock_route_is_404_with_the_module_off(monkeypatch, pharmacist):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    for path in ("/stock", "/stock/overview", "/stock/lots", "/ledger", "/reorder",
                 "/near-expiry", "/reconcile"):
        resp = client.get(f"{BASE}{path}", headers=pharmacist["hdr"])
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"


def test_only_the_read_tier_sees_stock():
    nurse = _user("nurse")
    for path in ("/stock", "/ledger", "/reorder", "/near-expiry"):
        assert client.get(f"{BASE}{path}", headers=nurse["hdr"]).status_code == 403, path
    admin = _user("admin")
    assert client.get(f"{BASE}/stock/overview", headers=admin["hdr"]).status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
#  Stock per store and drug
# ═════════════════════════════════════════════════════════════════════════════

def test_stock_splits_into_its_buckets(pharmacist, stores_made, dispensing):
    drug = _drug()
    sub = _store(stores_made)
    _lot(drug, sub["id"], 10, _days(200))
    _lot(drug, sub["id"], 3, _days(-5))
    _lot(drug, sub["id"], 2, None)
    _lot(drug, sub["id"], 4, _days(200), status="quarantined")
    _lot(drug, dispensing["id"], 7, _days(200))
    _transfer(sub["id"], dispensing["id"], drug, 6)
    _transfer(sub["id"], dispensing["id"], drug, 9, status="received", received=9)

    body = client.get(f"{BASE}/stock", headers=pharmacist["hdr"],
                      params={"drug_id": drug}).json()
    in_sub = _row(body["rows"], drug, sub["id"])
    assert in_sub["usable_units"] == 10
    assert in_sub["dispensable_units"] == 0, "a sub-store's stock is never dispensable"
    assert in_sub["expired_units"] == 5 and in_sub["undated_units"] == 2
    assert in_sub["quarantined_units"] == 4
    assert in_sub["in_transit_units"] == 6, "a received line is no longer in transit"
    assert in_sub["on_hand_units"] == 19 and in_sub["other_units"] == 0
    shelf = _row(body["rows"], drug, dispensing["id"])
    assert shelf["dispensable_units"] == shelf["usable_units"] == 7
    assert shelf["store_kind"] == "dispensing"
    with db_session() as db:
        assert stock.dispensable_units(db, drug) == 7


def test_units_in_transit_alone_still_make_a_row(pharmacist, stores_made, dispensing):
    drug = _drug()
    sub = _store(stores_made)
    _transfer(sub["id"], dispensing["id"], drug, 12)
    row = _row(client.get(f"{BASE}/stock", headers=pharmacist["hdr"],
                          params={"store_id": sub["id"]}).json()["rows"], drug, sub["id"])
    assert row["in_transit_units"] == 12 and row["on_hand_units"] == 0


def test_lots_read_rate_not_configured_never_zero(pharmacist, stores_made):
    drug = _drug()
    sub = _store(stores_made)
    _lot(drug, sub["id"], 5, _days(300))
    _lot(drug, sub["id"], 4, _days(20), cost=150, mrp=2000, per_pack=10)
    lots = client.get(f"{BASE}/stock/lots", headers=pharmacist["hdr"],
                      params={"drug_id": drug}).json()["lots"]
    assert [lot["quantity"] for lot in lots] == [4, 5], "earliest expiry first"
    priced, unpriced = lots
    assert priced["cost_value_paise"] == 600 and priced["mrp_value_paise"] == 800
    assert priced["expiry_state"] == "near_expiry"
    assert unpriced["cost_value_paise"] is None
    assert unpriced["cost_label"] == "Rate not configured"
    assert unpriced["mrp_label"] == "MRP not recorded"


# ═════════════════════════════════════════════════════════════════════════════
#  Reorder
# ═════════════════════════════════════════════════════════════════════════════

def test_an_unset_level_reads_no_reorder_level_set_never_zero(pharmacist, dispensing):
    drug = _drug()
    rows = client.get(f"{BASE}/reorder", headers=pharmacist["hdr"]).json()["rows"]
    mine = [r for r in rows if r["drug_id"] == drug]
    assert len(mine) == 1
    assert mine[0]["basis"] == "none"
    assert mine[0]["reorder_level_units"] is None
    assert mine[0]["level_label"] == "No reorder level set"
    assert mine[0]["needs_reorder"] is False
    hidden = client.get(f"{BASE}/reorder", headers=pharmacist["hdr"],
                        params={"include_unset": False}).json()["rows"]
    assert all(r["drug_id"] != drug for r in hidden)


def test_the_drug_level_counts_dispensable_units_only(pharmacist, stores_made, dispensing):
    drug = _drug(reorder_level_units=50, reorder_qty_units=95, pack_size_units=10)
    _lot(drug, dispensing["id"], 30, _days(200))
    _lot(drug, dispensing["id"], 40, _days(-1))
    _lot(drug, dispensing["id"], 40, _days(200), status="quarantined")
    row = next(r for r in client.get(f"{BASE}/reorder", headers=pharmacist["hdr"]).json()["rows"]
               if r["drug_id"] == drug)
    assert row["basis"] == "drug_default" and row["store_id"] == dispensing["id"]
    assert row["stock_units"] == 30, "expired and quarantined units are not counted"
    assert row["needs_reorder"] is True
    assert row["suggested_units"] == 95 and row["suggested_packs"] == 10

    no_qty = _drug(reorder_level_units=50)
    row = next(r for r in client.get(f"{BASE}/reorder", headers=pharmacist["hdr"]).json()["rows"]
               if r["drug_id"] == no_qty)
    assert row["needs_reorder"] is True and row["suggested_units"] is None
    assert row["note"] == "Reorder quantity not set"


def test_a_store_par_level_adds_transit_and_orders(pharmacist, stores_made, dispensing):
    drug = _drug(pack_size_units=12)
    sub = _store(stores_made)
    _lot(drug, sub["id"], 8, _days(200))
    _lot(drug, sub["id"], 50, _days(-3))
    with db_session() as db:
        db.execute("INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units, "
                   "reorder_level_units, max_units) VALUES (?, ?, 40, 20, 60)", (sub["id"], drug))
    _transfer(sub["id"], dispensing["id"], drug, 5)
    row = _row(client.get(f"{BASE}/reorder", headers=pharmacist["hdr"],
                          params={"store_id": sub["id"]}).json()["rows"], drug, sub["id"])
    assert row["basis"] == "par" and row["reorder_level_units"] == 20
    assert row["stock_units"] == 8 and row["in_transit_units"] == 5
    assert row["position_units"] == 13 and row["needs_reorder"] is True
    assert row["suggested_units"] == 47 and row["suggested_packs"] == 4

    with db_session() as db:
        db.execute("UPDATE pharmacy_par_levels SET reorder_level_units = 10 WHERE store_id = ?",
                   (sub["id"],))
    row = _row(client.get(f"{BASE}/reorder", headers=pharmacist["hdr"],
                          params={"store_id": sub["id"]}).json()["rows"], drug, sub["id"])
    assert row["needs_reorder"] is False and row["suggested_units"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  Near expiry
# ═════════════════════════════════════════════════════════════════════════════

def test_near_expiry_drops_empty_lots_and_values_at_cost_and_mrp(pharmacist, stores_made,
                                                                 monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_NEAR_EXPIRY_DAYS", 90)
    drug = _drug()
    sub = _store(stores_made)
    soon = _lot(drug, sub["id"], 5, _days(10), cost=200, mrp=1000, per_pack=10)
    unpriced = _lot(drug, sub["id"], 3, _days(40))
    empty = _lot(drug, sub["id"], 0, _days(5))
    later = _lot(drug, sub["id"], 9, _days(200))
    held = _lot(drug, sub["id"], 2, _days(15), status="quarantined")
    gone = _lot(drug, sub["id"], 4, _days(-2), cost=100)

    body = client.get(f"{BASE}/near-expiry", headers=pharmacist["hdr"],
                      params={"store_id": sub["id"]}).json()
    assert body["days"] == 90
    ids = [lot["id"] for lot in body["lots"]]
    assert ids == [soon["id"], unpriced["id"]]
    assert empty["id"] not in ids and later["id"] not in ids and held["id"] not in ids
    first, second = body["lots"]
    assert first["cost_value_paise"] == 1000 and first["mrp_value_paise"] == 500
    assert second["cost_value_paise"] is None and second["cost_label"] == "Rate not configured"
    assert body["totals"]["near_expiry"] == {
        "lots": 2, "units": 8, "cost_value_paise": 1000, "cost_unpriced_lots": 1,
        "mrp_value_paise": 500, "mrp_unpriced_lots": 1}
    assert [lot["id"] for lot in body["expired"]] == [gone["id"]]

    narrow = client.get(f"{BASE}/near-expiry", headers=pharmacist["hdr"],
                        params={"store_id": sub["id"], "days": 20}).json()
    assert [lot["id"] for lot in narrow["lots"]] == [soon["id"]]
    assert client.get(f"{BASE}/near-expiry", headers=pharmacist["hdr"],
                      params={"days": 0}).status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
#  The ledger
# ═════════════════════════════════════════════════════════════════════════════

def test_the_ledger_pages_by_seq_and_a_page_is_stable(pharmacist, stores_made):
    drug = _drug()
    sub = _store(stores_made)
    lot = _lot(drug, sub["id"], 10, _days(300))
    with db_session(immediate=True) as db:
        for _ in range(4):
            stock.post_movement(db, lot_id=lot["id"], movement_type="adjustment",
                                quantity_change=-1, actor_id=pharmacist["id"],
                                remarks="Counted short")
    url = f"{BASE}/ledger"
    first = client.get(url, headers=pharmacist["hdr"],
                       params={"lot_id": lot["id"], "limit": 2}).json()
    seqs = [m["seq"] for m in first["movements"]]
    assert seqs == sorted(seqs, reverse=True) and len(seqs) == 2
    assert first["has_more"] is True and first["next_before_seq"] == seqs[-1]
    assert first["movements"][0]["actor_name"] == f"pharmacist {pharmacist['id']}"
    assert first["movements"][0]["movement_label"] == "Adjustment"
    assert first["movements"][0]["balance_after_units"] == 6
    second = client.get(url, headers=pharmacist["hdr"],
                        params={"lot_id": lot["id"], "limit": 2,
                                "before_seq": first["next_before_seq"]}).json()

    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot["id"], movement_type="adjustment",
                            quantity_change=-1, actor_id=pharmacist["id"])
    again = client.get(url, headers=pharmacist["hdr"],
                       params={"lot_id": lot["id"], "limit": 2,
                               "before_seq": first["next_before_seq"]}).json()
    assert [m["id"] for m in again["movements"]] == [m["id"] for m in second["movements"]]
    last = client.get(url, headers=pharmacist["hdr"],
                      params={"lot_id": lot["id"], "limit": 2,
                              "before_seq": second["next_before_seq"]}).json()
    assert [m["movement_type"] for m in last["movements"]] == ["opening"]
    assert last["has_more"] is False and last["next_before_seq"] is None


def test_a_patient_order_movement_hides_its_reference(pharmacist, stores_made):
    drug = _drug()
    sub = _store(stores_made)
    lot = _lot(drug, sub["id"], 3, _days(300))
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot["id"], movement_type="dispense", quantity_change=-1,
                            actor_id=pharmacist["id"], reference_type="helix_order",
                            reference_id="ORDER-SECRET-1", remarks="for bed 4")
    rows = client.get(f"{BASE}/ledger", headers=pharmacist["hdr"],
                      params={"lot_id": lot["id"]}).json()["movements"]
    dispense = next(m for m in rows if m["movement_type"] == "dispense")
    assert dispense["patient_linked"] is True
    assert dispense["reference_id"] is None and dispense["remarks"] is None
    assert dispense["reference_label"] == "A patient's medicine order"
    assert "ORDER-SECRET-1" not in str(rows) and "bed 4" not in str(rows)


# ═════════════════════════════════════════════════════════════════════════════
#  Overview and reconcile
# ═════════════════════════════════════════════════════════════════════════════

def test_overview_and_reconcile_answer(pharmacist, stores_made):
    drug = _drug()
    sub = _store(stores_made)
    _lot(drug, sub["id"], 3, _days(5))
    body = client.get(f"{BASE}/stock/overview", headers=pharmacist["hdr"]).json()
    for key in ("below_reorder_level", "without_reorder_level", "near_expiry_lots",
                "near_expiry_units", "expired_on_shelf_units", "quarantined_units",
                "in_transit_units", "dispensable_units", "legacy_lots", "purchase_orders"):
        assert key in body, key
    assert body["near_expiry_lots"] >= 1
    assert set(body["purchase_orders"]) == {"draft", "awaiting_approval", "approved",
                                            "open_with_vendor"}
    rec = client.get(f"{BASE}/reconcile", headers=pharmacist["hdr"]).json()
    assert "clean" in rec and "mismatched_lots" in rec
    assert not any(r["lot_id"] in {lot["id"] for lot in reports.list_lots(
        drug_id=drug)["lots"]} for r in rec["mismatched_lots"])
