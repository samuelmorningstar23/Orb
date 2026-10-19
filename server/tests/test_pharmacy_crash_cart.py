"""Crash-cart stores and checks (grn-23): the /api/pharmacy-store/ward/crash-carts
routes, behind ORB_PHARMACY_STORE and ORB_PHARMACY_WARD_STOCK (ruling GRN-RS5,
held), and the crash cart riding the ward lane of grn-21.

Pinned (docs/his_design/grn.json, grn-23 tests, and the strict defaults
services/pharmacy_crash_cart_service lists for Samuel to confirm):
  * with either switch off every crash-cart route answers 404, the service
    refuses every write, and a crash cart's list cannot change;
  * a crash cart's list (its par levels) never takes a drug no ward store may
    hold: unclassified, NDPS, a barred high-alert drug or concentrated
    electrolyte; taking a drug off the list is always allowed; ward imprest
    par levels are unchanged;
  * a crash cart is filled by the ward lane (indent, decision, issue,
    dispatch, ward receipt), never by a free transfer, and its lots are never
    dispensed by Helix;
  * the top-up after use asks only for listed drugs, counts units a check
    found short before the pharmacy confirms them, and asks once;
  * a full check counts every batch in the cart and seals it again; a count
    that differs needs a note and waits for the pharmacy; the checker never
    confirms; confirming takes the units found short off the cart's lots by
    count_variance movements and the ledger reconciles; units found over are
    never added; refusing moves nothing and asks for a recount;
  * a seal check finding the seal broken or a different number, an old last
    check, a recount, or stock put in after the last full check makes a check
    due;
  * expired and expiring cart lots are on the cart overview and on the
    pharmacy's near-expiry report;
  * which carts a batch went to, with the units that left a cart with no
    patient recorded (the cart half of the recall trace);
  * a ward account sees only its own department's carts;
  * every write commits with its audit row or not at all, and a replayed
    request key records once.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token, hash_password
from database import db_session
from main import app
from services import audit_service
from services import pharmacy_crash_cart_service as carts
from services import pharmacy_master_service as masters
from services import pharmacy_stock_reports as reports
from services import pharmacy_stock_service as stock
from services import pharmacy_transfer_service as transfers
from services import procurement_service as procurement
from services.helix_db import init_helix_db

client = TestClient(app)
BASE = "/api/pharmacy-store"
CART_DEPT = "Cardiology"
OTHER_DEPT = "Oncology"
LONG_AGO = "2000-01-01 00:00:00"


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
def switches_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", True)


def _user(role, *, dept=None):
    uid = f"USRC{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, dept, hash_password("secretpass")))
    return {"id": uid, "role": role, "department": dept,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


class _Made:
    def __init__(self):
        self.stores: list[int] = []
        self.drugs: list[str] = []


@pytest.fixture
def made():
    """Everything a test makes, removed afterwards."""
    m = _Made()
    yield m
    with db_session() as db:
        for store_id in m.stores:
            db.execute("DELETE FROM pharmacy_stock_count_lines WHERE pharmacy_stock_count_id IN "
                       "(SELECT id FROM pharmacy_stock_counts WHERE store_id = ?)", (store_id,))
            db.execute("DELETE FROM pharmacy_stock_counts WHERE store_id = ?", (store_id,))
        for drug in m.drugs:
            db.execute("DELETE FROM pharmacy_transfer_lines WHERE helix_medication_id = ?",
                       (drug,))
            db.execute("DELETE FROM helix_stock_movements WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_lot_events WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_inventory_lots WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_par_levels WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM indent_lines WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_medication_master WHERE id = ?", (drug,))
        for store_id in m.stores:
            db.execute("DELETE FROM pharmacy_transfer_lines WHERE pharmacy_transfer_id IN "
                       "(SELECT id FROM pharmacy_transfers WHERE to_store_id = ? "
                       "OR from_store_id = ?)", (store_id, store_id))
            db.execute("DELETE FROM pharmacy_transfers WHERE to_store_id = ? OR from_store_id = ?",
                       (store_id, store_id))
            db.execute("DELETE FROM indent_lines WHERE indent_id IN (SELECT id FROM indents "
                       "WHERE store_id = ? OR issuing_store_id = ?)", (store_id, store_id))
            db.execute("DELETE FROM indents WHERE store_id = ? OR issuing_store_id = ?",
                       (store_id, store_id))
            db.execute("DELETE FROM pharmacy_par_levels WHERE store_id = ?", (store_id,))
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def _drug(m: _Made, **fields) -> str:
    med = f"MEDCC{_uid()}"
    cols = {"id": med, "name": f"Carttest {med[-6:]} 1 mg", "generic_name": "carttestine",
            "form": "injection", "strength": "1 mg", "drug_schedule": "H",
            "ndps_category": "none", "pack_size_units": 10} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    m.drugs.append(med)
    return med


def _store(m: _Made, kind="crash_cart", dept=CART_DEPT) -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(
            db, code=f"CC{_uid()}", name=f"{kind} {dept or 'pharmacy'} {_uid()}",
            store_type="ward" if dept else "central", department=dept,
            ward=f"{dept[:3].upper()}-1" if dept else None, domain="pharmacy",
            pharmacy_kind=kind)
    m.stores.append(row["id"])
    return dict(row)


@pytest.fixture
def dispensing(made):
    with db_session() as db:
        row = db.execute("SELECT * FROM stores WHERE pharmacy_kind = 'dispensing' "
                         "AND is_active = 1").fetchone()
    if row is not None:
        return dict(row)
    return _store(made, "dispensing", dept=None)


def _par(store_id, drug, par_units=5, max_units=None):
    with db_session() as db:
        db.execute("INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units, "
                   "max_units, updated_by) VALUES (?, ?, ?, ?, 'tester')",
                   (store_id, drug, par_units, max_units))


def _lot(drug, store_id, units, expiry, *, status="available", batch=None):
    with db_session(immediate=True) as db:
        out = stock.open_or_merge_lot(db, medication_id=drug, lot_number=batch or f"B{_uid()}",
                                      expiration_date=expiry, units=units, actor_id="tester",
                                      store_id=store_id, status=status, units_per_pack=10)
    return out["lot"]


def _lot_units(lot_id) -> int:
    with db_session() as db:
        return int(db.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                              (lot_id,)).fetchone()[0])


def _err(resp):
    return resp.json()["detail"]["error"]


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(target_id))).fetchall()]


def _cart(user, store) -> dict:
    resp = client.get(f"{BASE}/ward/crash-carts", headers=user["hdr"],
                      params={"store_code": store["code"]})
    assert resp.status_code == 200, resp.text
    (cart,) = resp.json()["carts"]
    return cart


def _check(user, store, **body):
    return client.post(f"{BASE}/ward/crash-carts/{store['code']}/checks", headers=user["hdr"],
                       json=body)


def _checked(user, store, **body) -> dict:
    resp = _check(user, store, **body)
    assert resp.status_code == 200, resp.text
    return resp.json()["check"]


def _full(user, store, counts: dict, *, seal="S-100", **extra) -> dict:
    return _checked(user, store, kind="full", seal_found=None, seal_intact=False,
                    seal_applied=seal,
                    lines=[{"lot_id": k, "counted_units": v} for k, v in counts.items()],
                    **extra)


def _decide(user, check_id, **body):
    return client.post(f"{BASE}/ward/crash-carts/checks/{check_id}/decision",
                       headers=user["hdr"], json=body)


def _age_checks(store_id, when=LONG_AGO):
    with db_session() as db:
        db.execute("UPDATE pharmacy_stock_counts SET created_at = ? WHERE store_id = ?",
                   (when, store_id))


@pytest.fixture
def nurse():
    return _user("nurse", dept=CART_DEPT)


@pytest.fixture
def ph_a():
    return _user("pharmacist")


@pytest.fixture
def ph_b():
    return _user("pharmacist")


@pytest.fixture
def stocked(made):
    """A crash cart with one listed drug (par 5) holding 5 in-date units."""
    cart = _store(made)
    drug = _drug(made)
    _par(cart["id"], drug, par_units=5)
    lot = _lot(drug, cart["id"], 5, _days(400), batch="ADR-1")
    return cart, drug, lot


# ═════════════════════════════════════════════════════════════════════════════
#  The switches
# ═════════════════════════════════════════════════════════════════════════════

CART_ROUTES = (
    ("get", "/ward/crash-carts"), ("get", "/ward/crash-carts/checks"),
    ("get", "/ward/crash-carts/checks/1"),
    ("get", "/ward/crash-carts/batch?drug_id=X&batch_no=Y"),
    ("post", "/ward/crash-carts/CC1/checks"), ("post", "/ward/crash-carts/checks/1/decision"),
    ("post", "/ward/crash-carts/CC1/top-up"),
)


@pytest.mark.parametrize("switch", ["PHARMACY_WARD_STOCK_ENABLED", "PHARMACY_STORE_ENABLED"])
def test_every_crash_cart_route_is_404_with_a_switch_off(monkeypatch, switch, nurse, ph_a):
    monkeypatch.setattr(config, switch, False)
    for user in (nurse, ph_a):
        for method, path in CART_ROUTES:
            resp = getattr(client, method)(f"{BASE}{path}", headers=user["hdr"],
                                           **({"json": {}} if method == "post" else {}))
            assert resp.status_code == 404, (path, resp.text)
            assert _err(resp) in ("feature_disabled", "module_disabled")


def test_the_service_refuses_every_write_with_ward_stock_off(monkeypatch, stocked, nurse,
                                                             ph_a):
    cart, drug, lot = stocked
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", False)
    with pytest.raises(transfers.TransferError) as exc:
        carts.record_check(cart["code"], actor=nurse, kind="seal", seal_found="S1",
                           seal_intact=True)
    assert exc.value.code == "ward_stock_off"
    with pytest.raises(transfers.TransferError) as exc:
        carts.decide_check(1, actor=ph_a, confirm=True)
    assert exc.value.code == "ward_stock_off"
    with pytest.raises(transfers.TransferError) as exc:
        carts.request_top_up(cart["code"], actor=nurse)
    assert exc.value.code == "ward_stock_off"
    with pytest.raises(masters.MasterError) as exc:
        masters.upsert_par_level(store_id=cart["id"], helix_medication_id=drug, par_units=6,
                                 actor=ph_a)
    assert exc.value.code == "ward_stock_off"
    with pytest.raises(masters.MasterError) as exc:
        masters.create_store(code=f"CC{_uid()}", name="Cart", pharmacy_kind="crash_cart",
                             actor=ph_a, department=CART_DEPT)
    assert exc.value.code == "ward_stock_off"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM pharmacy_stock_counts WHERE store_id = ?",
                          (cart["id"],)).fetchone()[0] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  The cart's list (GRN-RS5)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("fields, code", [
    ({"name": "Potassium chloride 15% injection", "generic_name": "potassium chloride"},
     "high_alert_barred"),
    ({"name": "Heparin 5000 IU injection", "generic_name": "heparin"}, "high_alert_barred"),
    ({"name": "Midazolam 5 mg injection", "generic_name": "midazolam",
      "ndps_category": "psychotropic"}, "ndps_not_ward_stock"),
    ({"drug_schedule": None}, "drug_unclassified"),
    ({"is_active": 0}, "drug_inactive"),
])
def test_a_crash_cart_list_never_takes_a_drug_no_ward_may_hold(made, ph_a, fields, code):
    cart = _store(made)
    drug = _drug(made, **fields)
    resp = client.put(f"{BASE}/par-levels", headers=ph_a["hdr"],
                      json={"store_id": cart["id"], "helix_medication_id": drug, "par_units": 2})
    assert resp.status_code == 422, resp.text
    assert _err(resp) == code
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM pharmacy_par_levels WHERE store_id = ?",
                          (cart["id"],)).fetchone()[0] == 0
    # Taking a drug off the list (0) is always allowed.
    resp = client.put(f"{BASE}/par-levels", headers=ph_a["hdr"],
                      json={"store_id": cart["id"], "helix_medication_id": drug, "par_units": 0})
    assert resp.status_code == 200, resp.text


def test_a_listed_drug_takes_a_crash_cart_par_level(made, ph_a):
    cart = _store(made)
    drug = _drug(made, name="Adrenaline 1 mg/ml injection", generic_name="adrenaline")
    resp = client.put(f"{BASE}/par-levels", headers=ph_a["hdr"],
                      json={"store_id": cart["id"], "helix_medication_id": drug, "par_units": 4})
    assert resp.status_code == 200, resp.text
    assert resp.json()["par_level"]["par_units"] == 4


def test_ward_imprest_par_levels_are_unchanged(made, ph_a):
    """The refusal is the crash cart's; an imprest store's list still refuses
    a barred drug at the indent, as W8-F built it."""
    ward = _store(made, kind="ward_imprest")
    drug = _drug(made, name="Heparin 5000 IU injection", generic_name="heparin")
    resp = client.put(f"{BASE}/par-levels", headers=ph_a["hdr"],
                      json={"store_id": ward["id"], "helix_medication_id": drug, "par_units": 2})
    assert resp.status_code == 200, resp.text


# ═════════════════════════════════════════════════════════════════════════════
#  Filling a cart, and Helix never dispensing from it
# ═════════════════════════════════════════════════════════════════════════════

def _fill(made, dispensing, nurse, ph_a, ph_b, cart, drug, units):
    resp = client.post(f"{BASE}/ward/indents", headers=nurse["hdr"],
                       json={"store_code": cart["code"], "priority": "urgent",
                             "lines": [{"helix_medication_id": drug, "quantity_units": units}]})
    assert resp.status_code == 200, resp.text
    indent = resp.json()["indent"]
    assert client.post(f"{BASE}/ward/indents/{indent['id']}/decision", headers=ph_b["hdr"],
                       json={}).status_code == 200
    resp = client.post(f"{BASE}/ward/indents/{indent['id']}/issue", headers=ph_a["hdr"], json={})
    assert resp.status_code == 200, resp.text
    tid = resp.json()["transfer"]["id"]
    resp = client.post(f"{BASE}/transfers/{tid}/dispatch", headers=ph_a["hdr"], json={})
    assert resp.status_code == 200, resp.text
    doc = resp.json()["transfer"]
    assert doc["to_store_kind"] == "crash_cart"
    resp = client.post(f"{BASE}/ward/transfers/{tid}/receive", headers=nurse["hdr"],
                       json={"lines": [{"line_id": line["id"],
                                        "received_units": line["quantity_units"]}
                                       for line in doc["lines"]]})
    assert resp.status_code == 200, resp.text
    return tid, indent


def test_a_cart_is_filled_by_the_ward_lane_and_never_dispensed_by_helix(
        made, dispensing, nurse, ph_a, ph_b):
    cart = _store(made)
    drug = _drug(made)
    _par(cart["id"], drug, par_units=5)
    _lot(drug, dispensing["id"], 5, _days(300), batch="FILL-1")
    _fill(made, dispensing, nurse, ph_a, ph_b, cart, drug, 5)
    with db_session() as db:
        in_cart = db.execute("SELECT quantity, status FROM helix_inventory_lots "
                             "WHERE medication_id = ? AND store_id = ?",
                             (drug, cart["id"])).fetchall()
        assert [(r["quantity"], r["status"]) for r in in_cart] == [(5, "available")]
        # Every unit is in the cart now; Helix sees none of it.
        assert stock.dispensable_units(db, drug) == 0
        assert stock.pick_fefo_lot(db, drug, 1) is None
        (cart_lot,) = [r[0] for r in db.execute(
            "SELECT id FROM helix_inventory_lots WHERE medication_id = ? AND store_id = ?",
            (drug, cart["id"])).fetchall()]
        # The pharmacist naming the cart's batch at a dispense is refused too.
        with pytest.raises(stock.StockError) as exc:
            stock.choose_dispense_lot(db, drug, 1, lot_id=cart_lot)
        assert exc.value.code == "lot_not_dispensable"
        assert "crash cart" in str(exc.value)
        lot_ids = [r[0] for r in db.execute(
            "SELECT id FROM helix_inventory_lots WHERE medication_id = ?", (drug,)).fetchall()]
        assert stock.reconcile(db, lot_ids=lot_ids)["clean"]
    nurse_view = client.get(f"{BASE}/ward/stock", headers=nurse["hdr"]).json()
    mine = [s for s in nurse_view["stores"] if s["store"]["id"] == cart["id"]]
    assert mine and mine[0]["store"]["pharmacy_kind"] == "crash_cart"
    assert mine[0]["store"]["kind_label"] == "Crash cart"


def test_a_free_transfer_never_reaches_a_crash_cart(made, dispensing, ph_a):
    cart = _store(made)
    drug = _drug(made)
    _par(cart["id"], drug)
    _lot(drug, dispensing["id"], 5, _days(300))
    resp = client.post(f"{BASE}/transfers", headers=ph_a["hdr"],
                       json={"from_store_code": dispensing["code"], "to_store_code": cart["code"],
                             "lines": [{"helix_medication_id": drug, "quantity_units": 1}]})
    assert resp.status_code == 422, resp.text
    assert _err(resp) == "store_not_transferable"


# ═════════════════════════════════════════════════════════════════════════════
#  Top-up after use
# ═════════════════════════════════════════════════════════════════════════════

def test_the_top_up_asks_only_for_listed_drugs(made, dispensing, nurse, stocked):
    cart, drug, lot = stocked
    unlisted = _drug(made)
    barred = _drug(made, name="Heparin 5000 IU injection", generic_name="heparin")
    _par(cart["id"], barred, par_units=3)  # on the list by hand: still barred
    resp = client.post(f"{BASE}/ward/indents", headers=nurse["hdr"],
                       json={"store_code": cart["code"],
                             "lines": [{"helix_medication_id": unlisted, "quantity_units": 1}]})
    assert resp.status_code == 422 and _err(resp) == "not_on_imprest_list"
    # Two units used: a matching check is not needed for the suggestion
    # when the books already show the use (here, set directly).
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot["id"], movement_type="count_variance",
                            quantity_change=-2, actor_id="tester")
    view = _cart(nurse, cart)
    assert view["top_up"] == [{"drug_id": drug, "drug_name": view["top_up"][0]["drug_name"],
                               "units": 2, "dispense_unit": None}]
    assert [d["drug_id"] for d in view["not_allowed"]] == [barred]
    resp = client.post(f"{BASE}/ward/crash-carts/{cart['code']}/top-up", headers=nurse["hdr"],
                       json={})
    assert resp.status_code == 200, resp.text
    indent = resp.json()["indent"]
    assert indent["priority"] == "urgent"
    assert [(line["drug_id"], line["requested_units"]) for line in indent["lines"]] == [(drug, 2)]
    # Asked once: nothing left to ask for.
    resp = client.post(f"{BASE}/ward/crash-carts/{cart['code']}/top-up", headers=nurse["hdr"],
                       json={})
    assert resp.status_code == 422 and _err(resp) == "nothing_to_top_up"
    assert _cart(nurse, cart)["top_up"] == []


def test_expired_cart_units_are_replaced_by_the_top_up(made, nurse, stocked):
    cart, drug, lot = stocked
    _lot(drug, cart["id"], 2, _days(-3), batch="OLD-1")
    view = _cart(nurse, cart)
    # 5 good units already meet par 5: the expired ones are flagged, not counted.
    assert view["top_up"] == []
    assert [(e["batch_no"], e["units"]) for e in view["expired"]] == [("OLD-1", 2)]
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot["id"], movement_type="count_variance",
                            quantity_change=-1, actor_id="tester")
    assert _cart(nurse, cart)["top_up"][0]["units"] == 1


# ═════════════════════════════════════════════════════════════════════════════
#  The seal-and-check record
# ═════════════════════════════════════════════════════════════════════════════

def test_a_matching_full_check_closes_at_once_and_seals_the_cart(nurse, stocked):
    cart, drug, lot = stocked
    check = _full(nurse, cart, {lot["id"]: 5}, seal="S-200")
    assert check["status"] == "approved" and check["decided_by"] is None
    assert check["short_units"] == 0 and check["over_units"] == 0
    assert check["seal_applied"] == "S-200"
    assert [(ln["lot_id"], ln["system_units"], ln["counted_units"]) for ln in check["lines"]] \
        == [(lot["id"], 5, 5)]
    view = _cart(nurse, cart)
    assert view["seal"] == "S-200" and view["seal_state"] == "sealed"
    assert view["full_check_due"] is False and view["seal_check_due"] is False
    assert view["pending_check"] is None
    assert _lot_units(lot["id"]) == 5
    assert _audits("pharmacy_cart_check_recorded", check["id"])


def test_a_full_check_counts_every_batch_in_the_cart(nurse, stocked):
    cart, drug, lot = stocked
    other = _lot(drug, cart["id"], 1, _days(500), batch="ADR-2")
    resp = _check(nurse, cart, kind="full", seal_intact=False, seal_applied="S-1",
                  lines=[{"lot_id": lot["id"], "counted_units": 5}])
    assert resp.status_code == 409 and _err(resp) == "check_stale"
    resp = _check(nurse, cart, kind="full", seal_intact=False, seal_applied="S-1",
                  lines=[{"lot_id": lot["id"], "counted_units": 5},
                         {"lot_id": other["id"], "counted_units": 1},
                         {"lot_id": "NOT-A-LOT", "counted_units": 0}])
    assert resp.status_code == 409 and _err(resp) == "check_stale"
    resp = _check(nurse, cart, kind="full", seal_intact=False,
                  lines=[{"lot_id": lot["id"], "counted_units": 5},
                         {"lot_id": other["id"], "counted_units": 1}])
    assert resp.status_code == 422 and _err(resp) == "missing_field"
    resp = _check(nurse, cart, kind="seal", seal_found="S-1", seal_intact=True,
                  seal_applied="S-2")
    assert resp.status_code == 422 and _err(resp) == "bad_value"
    resp = _check(nurse, cart, kind="seal", seal_intact=True)
    assert resp.status_code == 422 and _err(resp) == "missing_field"


def test_a_shortfall_needs_a_note_waits_for_the_pharmacy_and_is_topped_up_at_once(
        nurse, ph_a, ph_b, stocked):
    cart, drug, lot = stocked
    resp = _check(nurse, cart, kind="full", seal_found="S-100", seal_intact=False,
                  seal_applied="S-101", lines=[{"lot_id": lot["id"], "counted_units": 3}])
    assert resp.status_code == 422 and _err(resp) == "reason_required"
    check = _full(nurse, cart, {lot["id"]: 3}, seal="S-101",
                  note="Two used at a cardiac arrest")
    assert check["status"] == "submitted" and check["short_units"] == 2
    view = _cart(nurse, cart)
    assert view["pending_check"]["id"] == check["id"]
    (item,) = [d for d in view["drugs"] if d["drug_id"] == drug]
    assert item["on_hand_units"] == 5 and item["found_short_units"] == 2
    assert view["top_up"][0]["units"] == 2
    # One check waits at a time.
    resp = _check(nurse, cart, kind="full", seal_intact=False, seal_applied="S-102",
                  lines=[{"lot_id": lot["id"], "counted_units": 3}], note="again")
    assert resp.status_code == 409 and _err(resp) == "check_pending"
    # Nothing has left the books yet.
    assert _lot_units(lot["id"]) == 5


def test_the_checker_never_confirms_and_a_nurse_never_decides(nurse, ph_a, ph_b, stocked):
    cart, drug, lot = stocked
    check = _full(ph_a, cart, {lot["id"]: 4}, note="One missing")
    resp = _decide(ph_a, check["id"], confirm=True)
    assert resp.status_code == 403 and _err(resp) == "same_person"
    resp = _decide(nurse, check["id"], confirm=True)
    assert resp.status_code == 403
    assert _lot_units(lot["id"]) == 5


def test_confirming_takes_the_shortfall_off_the_cart_and_the_ledger_reconciles(
        nurse, ph_b, stocked):
    cart, drug, lot = stocked
    check = _full(nurse, cart, {lot["id"]: 3}, note="Two used at a cardiac arrest")
    resp = _decide(ph_b, check["id"], confirm=True, reason="Matches the code sheet")
    assert resp.status_code == 200, resp.text
    done = resp.json()["check"]
    assert done["status"] == "approved" and done["decided_by"] == ph_b["id"]
    assert done["status_label"] == "Confirmed by the pharmacy"
    assert _lot_units(lot["id"]) == 3
    with db_session() as db:
        moves = db.execute("SELECT * FROM helix_stock_movements WHERE lot_id = ? "
                           "AND movement_type = 'count_variance'", (lot["id"],)).fetchall()
        assert [(m["quantity_change"], m["reference_type"], m["reference_id"]) for m in moves] \
            == [(-2, "pharmacy_stock_count", str(check["id"]))]
        line = db.execute("SELECT adjustment_movement_ref FROM pharmacy_stock_count_lines "
                          "WHERE pharmacy_stock_count_id = ?", (check["id"],)).fetchone()
        assert line["adjustment_movement_ref"] == moves[0]["id"]
        assert stock.reconcile(db, lot_ids=[lot["id"]])["clean"]
    assert _audits("pharmacy_cart_check_confirmed", check["id"])
    view = _cart(nurse, cart)
    assert view["pending_check"] is None
    (item,) = [d for d in view["drugs"] if d["drug_id"] == drug]
    assert item["on_hand_units"] == 3 and item["found_short_units"] == 0
    assert view["top_up"][0]["units"] == 2
    # Decided once.
    resp = _decide(ph_b, check["id"], confirm=True)
    assert resp.status_code == 409 and _err(resp) == "illegal_transition"


def test_units_found_over_are_recorded_and_never_added(nurse, ph_b, made, stocked):
    cart, drug, lot = stocked
    stray = _drug(made)
    check = _full(nurse, cart, {lot["id"]: 6}, note="One extra vial found",
                  extra=[{"drug_id": stray, "units": 2, "batch_no": "X9"}])
    assert check["status"] == "submitted" and check["over_units"] == 3
    assert [ln["found_extra"] for ln in check["lines"]] == [False, True]
    assert _decide(ph_b, check["id"], confirm=True).status_code == 200
    assert _lot_units(lot["id"]) == 5
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM helix_inventory_lots WHERE medication_id = ?",
                          (stray,)).fetchone()[0] == 0


def test_refusing_needs_a_reason_moves_nothing_and_asks_for_a_recount(nurse, ph_b, stocked):
    cart, drug, lot = stocked
    check = _full(nurse, cart, {lot["id"]: 1}, note="Four missing")
    resp = _decide(ph_b, check["id"], confirm=False)
    assert resp.status_code == 422 and _err(resp) == "missing_field"
    resp = _decide(ph_b, check["id"], confirm=False, reason="Count again with the in-charge")
    assert resp.status_code == 200, resp.text
    assert resp.json()["check"]["status"] == "rejected"
    assert _lot_units(lot["id"]) == 5
    assert _audits("pharmacy_cart_check_refused", check["id"])
    view = _cart(nurse, cart)
    assert view["full_check_due"] is True
    assert "No full check is on record." in view["due_reasons"]
    # The refused count no longer holds back the top-up.
    assert view["top_up"] == []


# ═════════════════════════════════════════════════════════════════════════════
#  Seal checks and what makes a check due
# ═════════════════════════════════════════════════════════════════════════════

def test_a_seal_check_before_any_full_check_says_a_full_check_is_due(nurse, stocked):
    cart, drug, lot = stocked
    check = _checked(nurse, cart, kind="seal", seal_found="S-9", seal_intact=True)
    assert check["status"] == "approved" and check["lines"] == []
    view = _cart(nurse, cart)
    assert view["seal_check_due"] is False
    assert view["full_check_due"] is True
    assert view["due_reasons"] == ["No full check is on record."]


def test_a_broken_or_different_seal_makes_a_full_check_due(nurse, stocked):
    cart, drug, lot = stocked
    _full(nurse, cart, {lot["id"]: 5}, seal="S-300")
    assert _cart(nurse, cart)["full_check_due"] is False
    ok = _checked(nurse, cart, kind="seal", seal_found=" s-300 ", seal_intact=True)
    assert ok["seal_matches"] is True
    assert _cart(nurse, cart)["full_check_due"] is False
    wrong = _checked(nurse, cart, kind="seal", seal_found="S-301", seal_intact=True)
    assert wrong["seal_matches"] is False and wrong["seal_expected"] == "S-300"
    view = _cart(nurse, cart)
    assert view["full_check_due"] is True and view["seal_state"] == "broken"
    assert any("seal" in r for r in view["due_reasons"])
    # A new full check seals the cart again.
    _full(nurse, cart, {lot["id"]: 5}, seal="S-302")
    view = _cart(nurse, cart)
    assert view["seal"] == "S-302" and view["seal_state"] == "sealed"
    assert view["full_check_due"] is False


def test_a_broken_seal_found_at_a_seal_check_is_recorded(nurse, stocked):
    cart, drug, lot = stocked
    _full(nurse, cart, {lot["id"]: 5}, seal="S-400")
    broken = _checked(nurse, cart, kind="seal", seal_found="S-400", seal_intact=False,
                      note="Opened for an emergency")
    assert broken["outcome"].startswith("Seal found broken")
    assert _cart(nurse, cart)["seal_state"] == "broken"


def test_old_checks_make_checks_due(nurse, stocked):
    cart, drug, lot = stocked
    _full(nurse, cart, {lot["id"]: 5}, seal="S-500")
    _age_checks(cart["id"], (datetime.now(timezone.utc) - timedelta(hours=25))
                .strftime("%Y-%m-%d %H:%M:%S"))
    view = _cart(nurse, cart)
    assert view["seal_check_due"] is True and view["full_check_due"] is False
    _age_checks(cart["id"])
    view = _cart(nurse, cart)
    assert view["full_check_due"] is True
    assert f"The last full check is more than {carts.FULL_CHECK_DUE_DAYS} days old." \
        in view["due_reasons"]


def test_stock_put_in_after_the_last_full_check_makes_one_due(
        made, dispensing, nurse, ph_a, ph_b, stocked):
    cart, drug, lot = stocked
    _full(nurse, cart, {lot["id"]: 5}, seal="S-600")
    _age_checks(cart["id"], (datetime.now(timezone.utc) - timedelta(hours=2))
                .strftime("%Y-%m-%d %H:%M:%S"))
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot["id"], movement_type="count_variance",
                            quantity_change=-1, actor_id="tester")
    _lot(drug, dispensing["id"], 3, _days(300), batch="TOP-1")
    _fill(made, dispensing, nurse, ph_a, ph_b, cart, drug, 1)
    view = _cart(nurse, cart)
    assert "Stock was put into the cart at or after the last full check." in view["due_reasons"]


# ═════════════════════════════════════════════════════════════════════════════
#  Expiry on the overview
# ═════════════════════════════════════════════════════════════════════════════

def test_expiring_and_expired_cart_lots_are_on_the_overviews(nurse, ph_a, stocked):
    cart, drug, lot = stocked
    soon = _lot(drug, cart["id"], 2, _days(10), batch="SOON-1")
    gone = _lot(drug, cart["id"], 1, _days(-1), batch="GONE-1")
    held = _lot(drug, cart["id"], 1, _days(200), batch="HELD-1", status="quarantined")
    view = _cart(ph_a, cart)
    assert [(e["lot_id"], e["days_left"]) for e in view["expiring"]] == [(soon["id"], 10)]
    assert [e["lot_id"] for e in view["expired"]] == [gone["id"]]
    assert [e["lot_id"] for e in view["held"]] == [held["id"]]
    near = reports.near_expiry(days=30)
    mine = [x for x in near["lots"] if x["id"] == soon["id"]]
    assert mine and mine[0]["store_kind"] == "crash_cart"
    assert any(x["id"] == gone["id"] for x in near["expired"])
    assert reports.overview()["near_expiry_lots"] >= 1


# ═════════════════════════════════════════════════════════════════════════════
#  Which carts a batch went to
# ═════════════════════════════════════════════════════════════════════════════

def test_the_batch_trace_lists_cart_issues_with_patients_not_recorded(
        made, dispensing, nurse, ph_a, ph_b):
    cart = _store(made)
    drug = _drug(made)
    _par(cart["id"], drug, par_units=5)
    _lot(drug, dispensing["id"], 5, _days(300), batch="rc 77")
    _fill(made, dispensing, nurse, ph_a, ph_b, cart, drug, 4)
    with db_session() as db:
        (lot_id,) = [r[0] for r in db.execute(
            "SELECT id FROM helix_inventory_lots WHERE store_id = ? AND medication_id = ?",
            (cart["id"], drug)).fetchall()]
    check = _full(nurse, cart, {lot_id: 1}, note="Three used at an arrest")
    assert _decide(ph_b, check["id"], confirm=True).status_code == 200
    resp = client.get(f"{BASE}/ward/crash-carts/batch", headers=ph_a["hdr"],
                      params={"drug_id": drug, "batch_no": "RC77"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (entry,) = body["carts"]
    assert entry["store_id"] == cart["id"]
    assert (entry["sent_units"], entry["received_units"], entry["in_cart_units"],
            entry["unattributed_units"]) == (4, 4, 1, 3)
    assert entry["label"].endswith("stock, patients not recorded")
    assert body["totals"]["unattributed_units"] == 3
    # The pharmacy desk only.
    resp = client.get(f"{BASE}/ward/crash-carts/batch", headers=nurse["hdr"],
                      params={"drug_id": drug, "batch_no": "RC77"})
    assert resp.status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
#  Who sees which cart
# ═════════════════════════════════════════════════════════════════════════════

def test_a_ward_account_sees_only_its_own_departments_carts(made, nurse, stocked):
    cart, drug, lot = stocked
    imprest = _store(made, kind="ward_imprest")
    elsewhere = _store(made, dept=OTHER_DEPT)
    view = client.get(f"{BASE}/ward/crash-carts", headers=nurse["hdr"]).json()
    ids = {c["store"]["id"] for c in view["carts"]}
    assert cart["id"] in ids and elsewhere["id"] not in ids and imprest["id"] not in ids
    resp = _check(nurse, elsewhere, kind="seal", seal_found="S1", seal_intact=True)
    assert resp.status_code == 404
    other_nurse = _user("nurse", dept=OTHER_DEPT)
    check = _full(nurse, cart, {lot["id"]: 4}, note="One used")
    resp = client.get(f"{BASE}/ward/crash-carts/checks/{check['id']}",
                      headers=other_nurse["hdr"])
    assert resp.status_code == 404
    no_dept = _user("nurse")
    assert client.get(f"{BASE}/ward/crash-carts", headers=no_dept["hdr"]).json()["carts"] == []
    listed = client.get(f"{BASE}/ward/crash-carts/checks", headers=nurse["hdr"],
                        params={"store_code": cart["code"]}).json()["checks"]
    assert [c["id"] for c in listed] == [check["id"]]


def test_a_role_without_a_ward_grant_cannot_record_a_check(made, stocked):
    cart, drug, lot = stocked
    doctor = _user("doctor", dept=CART_DEPT)
    resp = _check(doctor, cart, kind="seal", seal_found="S1", seal_intact=True)
    assert resp.status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
#  Audit and replay
# ═════════════════════════════════════════════════════════════════════════════

def test_a_check_with_no_audit_row_is_not_written(monkeypatch, nurse, stocked):
    cart, drug, lot = stocked
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "pharmacy_cart_check_recorded":
            raise audit_service.AuditWriteError("audit store down")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    resp = _check(nurse, cart, kind="seal", seal_found="S1", seal_intact=True)
    assert resp.status_code == 503 and _err(resp) == "audit_unavailable"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM pharmacy_stock_counts WHERE store_id = ?",
                          (cart["id"],)).fetchone()[0] == 0


def test_a_confirmation_with_no_audit_row_moves_nothing(monkeypatch, nurse, ph_b, stocked):
    cart, drug, lot = stocked
    check = _full(nurse, cart, {lot["id"]: 3}, note="Two used")
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "pharmacy_cart_check_confirmed":
            raise audit_service.AuditWriteError("audit store down")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    resp = _decide(ph_b, check["id"], confirm=True)
    assert resp.status_code == 503
    assert _lot_units(lot["id"]) == 5
    with db_session() as db:
        assert db.execute("SELECT status FROM pharmacy_stock_counts WHERE id = ?",
                          (check["id"],)).fetchone()[0] == "submitted"


def test_a_replayed_request_key_records_once(nurse, made, stocked):
    cart, drug, lot = stocked
    key = f"cart-check-{_uid()}"
    first = _check(nurse, cart, kind="seal", seal_found="S1", seal_intact=True,
                   idempotency_key=key)
    second = _check(nurse, cart, kind="seal", seal_found="S1", seal_intact=True,
                    idempotency_key=key)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["check"]["id"] == first.json()["check"]["id"]
    other = _user("nurse", dept=CART_DEPT)
    resp = _check(other, cart, kind="seal", seal_found="S1", seal_intact=True,
                  idempotency_key=key)
    assert resp.status_code == 409 and _err(resp) == "idempotency_conflict"
