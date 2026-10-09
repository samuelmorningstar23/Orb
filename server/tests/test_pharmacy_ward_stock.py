"""Ward indents, ward imprest stores and ward receipt (grn-21, W8-F): the
/api/pharmacy-store/ward routes, behind ORB_PHARMACY_STORE and
ORB_PHARMACY_WARD_STOCK.

Pinned (docs/his_design/grn.json, grn-21 tests, and the strict defaults the
service note lists for Samuel to confirm under GRN-RS5):
  * with either switch off every ward route answers 404, and a ward store
    cannot be created;
  * Helix dispense never draws a ward lot;
  * a nurse sees only her own department's ward store; another ward's
    indent and transfer are 404; a ward account with no department sees none;
  * the person who dispatched never receives (403), and the pharmacy desk
    never receives a ward transfer for the ward;
  * a drug not on the store's list (no par row), a barred high-alert drug or
    concentrated electrolyte, and NDPS stock cannot be asked for, approved,
    issued or sent to a ward; a drug that becomes barred on its way lands
    quarantined;
  * a drug classified narcotic or psychotropic on its way never strands the
    transfer: the ward receives it with a witness holding the NDPS
    permission (never the nurse, never the person who sent it, whatever
    NDPS_WITNESS_ALWAYS says), the NDPS line lands quarantined, the indent
    closes, and with the register on the ward's register entry names the
    witness;
  * helix.receive alone reaches none of these routes;
  * the stores desk's indent routes cannot see an issue indent;
  * the whole chain balances: raise, decide (never by the raiser), plan,
    issue, dispatch, ward receipt, the indent closed, the ledger reconciled;
  * an approved indent never sticks on one line: naming the lines that can go
    issues them alone, a short line sends at most what is usable, the rest is
    free to ask for again once the delivery is received, and the desk (never
    the ward, never while a draft is open) cancels an approved indent with a
    reason;
  * every write commits with its audit row or not at all.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import config
import permissions
from auth import create_token, hash_password
from database import db_session
from main import app
from services import audit_service, pin_security
from services import pharmacy_master_service as masters
from services import pharmacy_stock_service as stock
from services import pharmacy_transfer_service as transfers
from services import procurement_service as procurement
from services.helix_db import init_helix_db

client = TestClient(app)
BASE = "/api/pharmacy-store"
WARD_DEPT = "Cardiology"
OTHER_DEPT = "Oncology"


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


PIN = "4826"


def _user(role, *, dept=None, pin=None):
    uid = f"USRW{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash, pin_hash) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, dept, hash_password("secretpass"),
                    pin_security.hash_pin(pin) if pin else None))
    return {"id": uid, "role": role, "department": dept,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


class _Made:
    def __init__(self):
        self.stores: list[int] = []
        self.drugs: list[str] = []


@pytest.fixture
def made():
    """Everything a test makes, removed afterwards: its drugs' lots, ledger,
    par rows and indent lines; the transfers and indents touching its stores;
    then its stores."""
    m = _Made()
    yield m
    with db_session() as db:
        for drug in m.drugs:
            # The NDPS register rows a test with the register on wrote (the
            # suite's profile is not prod, so they are 'non_production').
            db.execute("DELETE FROM ndps_register WHERE helix_medication_id = ? "
                       "AND lane = 'non_production'", (drug,))
            db.execute("DELETE FROM ndps_variances WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_transfer_lines WHERE helix_medication_id = ?",
                       (drug,))
            db.execute("DELETE FROM helix_stock_movements WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_lot_events WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_inventory_lots WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_par_levels WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM indent_lines WHERE helix_medication_id = ?", (drug,))
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
        for store_id in m.stores:
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def _drug(m: _Made, **fields) -> str:
    med = f"MEDWS{_uid()}"
    cols = {"id": med, "name": f"Wardtest {med[-6:]} 5 mg", "generic_name": "wardtestol",
            "form": "tablet", "strength": "5 mg", "drug_schedule": "H",
            "ndps_category": "none", "pack_size_units": 10} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    m.drugs.append(med)
    return med


def _store(m: _Made, kind="ward_imprest", dept=WARD_DEPT) -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(
            db, code=f"WS{_uid()}", name=f"{kind} {dept or 'pharmacy'} {_uid()}",
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


@pytest.fixture(autouse=True)
def _a_dispensing_store(dispensing):
    """Every ward indent asks the active dispensing pharmacy, so one exists."""
    return dispensing


def _par(store_id, drug, par_units=20, max_units=None):
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


def _err(resp):
    return resp.json()["detail"]["error"]


def _raise(user, store, lines, **extra):
    return client.post(f"{BASE}/ward/indents", headers=user["hdr"],
                       json={"store_code": store["code"],
                             "lines": [{"helix_medication_id": d, "quantity_units": u}
                                       for d, u in lines], **extra})


def _raised(user, store, lines, **extra) -> dict:
    resp = _raise(user, store, lines, **extra)
    assert resp.status_code == 200, resp.text
    return resp.json()["indent"]


def _decide(user, indent_id, **body):
    return client.post(f"{BASE}/ward/indents/{indent_id}/decision", headers=user["hdr"],
                       json=body)


def _issue(user, indent_id, **body):
    return client.post(f"{BASE}/ward/indents/{indent_id}/issue", headers=user["hdr"],
                       json=body)


def _dispatch(user, tid):
    return client.post(f"{BASE}/transfers/{tid}/dispatch", headers=user["hdr"], json={})


def _ward_receive(user, tid, lines):
    return client.post(f"{BASE}/ward/transfers/{tid}/receive", headers=user["hdr"],
                       json={"lines": lines})


def _full(doc):
    return [{"line_id": line["id"], "received_units": line["quantity_units"]}
            for line in doc["lines"]]


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(target_id))).fetchall()]


@pytest.fixture
def nurse():
    return _user("nurse", dept=WARD_DEPT)


@pytest.fixture
def ph_a():
    return _user("pharmacist")


@pytest.fixture
def ph_b():
    return _user("pharmacist")


def _sent(made, dispensing, nurse, ph_a, ph_b, *, units=6, stock_units=30):
    """A drug on a ward store's list, asked for by the nurse, approved by one
    pharmacist, issued and sent by the other. Returns (ward, drug, transfer,
    indent)."""
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug, par_units=20)
    _lot(drug, dispensing["id"], stock_units, _days(200))
    indent = _raised(nurse, ward, [(drug, units)])
    assert _decide(ph_b, indent["id"]).status_code == 200
    resp = _issue(ph_a, indent["id"])
    assert resp.status_code == 200, resp.text
    transfer = resp.json()["transfer"]
    resp = _dispatch(ph_a, transfer["id"])
    assert resp.status_code == 200, resp.text
    return ward, drug, resp.json()["transfer"], indent


# ═════════════════════════════════════════════════════════════════════════════
#  The switches
# ═════════════════════════════════════════════════════════════════════════════

WARD_ROUTES = (
    ("get", "/ward/stock"), ("get", "/ward/transfers/awaiting"), ("get", "/ward/indents"),
    ("get", "/ward/indents/1"), ("get", "/ward/indents/1/issue-plan"),
    ("post", "/ward/indents"), ("post", "/ward/indents/1/decision"),
    ("post", "/ward/indents/1/cancel"), ("post", "/ward/indents/1/issue"),
    ("post", "/ward/transfers/1/receive"),
)


def test_every_ward_route_is_404_with_ward_stock_off(monkeypatch, nurse, ph_a):
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", False)
    for user in (nurse, ph_a):
        for method, path in WARD_ROUTES:
            resp = getattr(client, method)(f"{BASE}{path}", headers=user["hdr"],
                                           **({"json": {}} if method == "post" else {}))
            assert resp.status_code == 404, (path, resp.text)
            assert resp.json()["detail"]["error"] == "feature_disabled"


def test_every_ward_route_is_404_with_the_module_off(monkeypatch, nurse):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    for method, path in WARD_ROUTES:
        resp = getattr(client, method)(f"{BASE}{path}", headers=nurse["hdr"],
                                       **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"


def test_a_ward_store_cannot_be_created_with_ward_stock_off(monkeypatch):
    from services import pharmacy_master_service as masters
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", False)
    with pytest.raises(masters.MasterError) as exc:
        masters.create_store(code=f"WS{_uid()}", name="Ward", pharmacy_kind="ward_imprest",
                             department=WARD_DEPT, actor={"id": "ADM001", "role": "admin"})
    assert exc.value.code == "ward_stock_off"


def test_the_service_refuses_writes_while_ward_stock_is_off(monkeypatch, made, nurse):
    ward = _store(made)
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", False)
    with pytest.raises(transfers.TransferError) as exc:
        transfers.raise_ward_indent(store_code=ward["code"], lines=[
            {"helix_medication_id": "X", "quantity_units": 1}], actor=nurse)
    assert exc.value.code == "ward_stock_off"


def test_helix_receive_alone_reaches_none_of_the_ward_routes(monkeypatch):
    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "nurse", {permissions.HELIX_RECEIVE})
    only_receive = _user("nurse", dept=WARD_DEPT)
    for method, path in WARD_ROUTES:
        resp = getattr(client, method)(f"{BASE}{path}", headers=only_receive["hdr"],
                                       **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 403, (path, resp.text)


def test_a_role_with_no_ward_or_pharmacy_grant_is_refused():
    doctor = _user("doctor", dept=WARD_DEPT)
    assert client.get(f"{BASE}/ward/stock", headers=doctor["hdr"]).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
#  The whole chain
# ═════════════════════════════════════════════════════════════════════════════

def test_raise_decide_issue_dispatch_receive_balances_and_closes_the_indent(
        made, dispensing, nurse, ph_a, ph_b):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug, par_units=20)
    soon = _lot(drug, dispensing["id"], 4, _days(60))
    late = _lot(drug, dispensing["id"], 30, _days(300))

    indent = _raised(nurse, ward, [(drug, 10)], priority="urgent")
    assert indent["status"] == "submitted" and indent["indent_kind"] == "issue"
    assert indent["domain"] == "pharmacy"
    assert indent["requested_by"] == nurse["id"]
    assert indent["lines"][0]["requested_units"] == 10
    assert _audits("pharmacy_ward_indent_raised", indent["id"])

    resp = _decide(ph_b, indent["id"], approvals=[{"line_no": 1, "units": 8}])
    assert resp.status_code == 200, resp.text
    decided = resp.json()["indent"]
    assert decided["status"] == "partially_approved"
    assert decided["lines"][0]["approved_units"] == 8
    assert _audits("pharmacy_ward_indent_decided", indent["id"])

    plan = client.get(f"{BASE}/ward/indents/{indent['id']}/issue-plan", headers=ph_a["hdr"])
    assert plan.status_code == 200, plan.text
    line = plan.json()["lines"][0]
    assert line["remaining_units"] == 8 and line["problem"] is None
    assert [(p["lot_id"], p["units"]) for p in line["plan"]] == [(soon["id"], 4), (late["id"], 4)]

    resp = _issue(ph_a, indent["id"])
    assert resp.status_code == 200, resp.text
    draft = resp.json()["transfer"]
    assert draft["status"] == "draft" and draft["source_indent_ref"] == indent["id"]
    assert draft["to_store_kind"] == "ward_imprest"
    # A draft moves nothing.
    with db_session() as db:
        assert db.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                          (soon["id"],)).fetchone()[0] == 4

    resp = _dispatch(ph_a, draft["id"])
    assert resp.status_code == 200, resp.text
    sent = resp.json()["transfer"]
    assert sent["status"] == "dispatched" and sent["in_transit_units"] == 8
    after = client.get(f"{BASE}/ward/indents/{indent['id']}", headers=nurse["hdr"]).json()
    assert after["indent"]["status"] == "converted"
    assert after["indent"]["lines"][0]["issued_units"] == 8

    awaiting = client.get(f"{BASE}/ward/transfers/awaiting", headers=nurse["hdr"])
    assert [t["id"] for t in awaiting.json()["transfers"]] == [sent["id"]]

    resp = _ward_receive(nurse, sent["id"], _full(sent))
    assert resp.status_code == 200, resp.text
    got = resp.json()["transfer"]
    assert got["status"] == "received" and got["received_by"] == nurse["id"]
    closed = client.get(f"{BASE}/ward/indents/{indent['id']}", headers=nurse["hdr"]).json()
    assert closed["indent"]["status"] == "closed"
    assert _audits("pharmacy_transfer_received", sent["id"])

    with db_session() as db:
        landed = db.execute("SELECT SUM(quantity) FROM helix_inventory_lots WHERE store_id = ? "
                            "AND medication_id = ? AND status = 'available'",
                            (ward["id"], drug)).fetchone()[0]
        assert landed == 8
        assert transfers.transfer_balance(db, sent["id"])["balanced"]
        lot_ids = {r[0] for r in db.execute("SELECT id FROM helix_inventory_lots "
                                            "WHERE medication_id = ?", (drug,)).fetchall()}
        assert stock.reconcile(db, lot_ids=list(lot_ids))["clean"]

    view = client.get(f"{BASE}/ward/stock", headers=nurse["hdr"]).json()
    store_view = [s for s in view["stores"] if s["store"]["id"] == ward["id"]][0]
    row = [d for d in store_view["drugs"] if d["drug_id"] == drug][0]
    assert row["on_hand_units"] == 8 and row["par_units"] == 20 and row["listed"]
    assert row["below_par"] and row["top_up_units"] == 12


def test_helix_dispense_never_draws_a_ward_lot(made, dispensing, nurse, ph_a, ph_b):
    ward, drug, sent, _ = _sent(made, dispensing, nurse, ph_a, ph_b, units=6, stock_units=6)
    assert _ward_receive(nurse, sent["id"], _full(sent)).status_code == 200
    with db_session() as db:
        on_ward = db.execute("SELECT SUM(quantity) FROM helix_inventory_lots WHERE store_id = ? "
                             "AND medication_id = ?", (ward["id"], drug)).fetchone()[0]
        assert on_ward == 6
        assert stock.pick_fefo_lot(db, drug) is None
        assert stock.dispensable_units(db, drug) == 0


def test_a_short_ward_receipt_needs_a_reason(made, dispensing, nurse, ph_a, ph_b):
    _, _, sent, _ = _sent(made, dispensing, nurse, ph_a, ph_b, units=6)
    line = sent["lines"][0]
    resp = _ward_receive(nurse, sent["id"], [{"line_id": line["id"], "received_units": 4}])
    assert resp.status_code == 422 and _err(resp) == "discrepancy_reason_required"
    resp = _ward_receive(nurse, sent["id"], [{"line_id": line["id"], "received_units": 4,
                                              "discrepancy_reason": "Two strips torn"}])
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        assert transfers.transfer_balance(db, sent["id"])["balanced"]


# ═════════════════════════════════════════════════════════════════════════════
#  Who sees and who acts
# ═════════════════════════════════════════════════════════════════════════════

def test_a_nurse_sees_only_her_departments_store(made, dispensing, nurse, ph_a, ph_b):
    mine = _store(made)
    theirs = _store(made, dept=OTHER_DEPT)
    drug = _drug(made)
    for store in (mine, theirs):
        _par(store["id"], drug)
    _lot(drug, dispensing["id"], 40, _days(200))
    other_nurse = _user("nurse", dept=OTHER_DEPT)
    their_indent = _raised(other_nurse, theirs, [(drug, 5)])

    view = client.get(f"{BASE}/ward/stock", headers=nurse["hdr"]).json()
    seen = {s["store"]["id"] for s in view["stores"]}
    assert mine["id"] in seen and theirs["id"] not in seen
    listed = client.get(f"{BASE}/ward/indents", headers=nurse["hdr"]).json()["indents"]
    assert their_indent["id"] not in {i["id"] for i in listed}
    resp = client.get(f"{BASE}/ward/indents/{their_indent['id']}", headers=nurse["hdr"])
    assert resp.status_code == 404
    # She cannot raise on another ward's store either: it is not found.
    resp = _raise(nurse, theirs, [(drug, 2)])
    assert resp.status_code == 404 and _err(resp) == "not_found"
    # The pharmacy desk sees both.
    desk = client.get(f"{BASE}/ward/stock", headers=ph_a["hdr"]).json()
    assert {mine["id"], theirs["id"]} <= {s["store"]["id"] for s in desk["stores"]}


def test_another_wards_transfer_is_404(made, dispensing, ph_a, ph_b):
    other_nurse = _user("nurse", dept=OTHER_DEPT)
    theirs = _store(made, dept=OTHER_DEPT)
    drug = _drug(made)
    _par(theirs["id"], drug)
    _lot(drug, dispensing["id"], 20, _days(200))
    indent = _raised(other_nurse, theirs, [(drug, 3)])
    assert _decide(ph_b, indent["id"]).status_code == 200
    draft = _issue(ph_a, indent["id"]).json()["transfer"]
    sent = _dispatch(ph_a, draft["id"]).json()["transfer"]
    cardiology_nurse = _user("nurse", dept=WARD_DEPT)
    resp = _ward_receive(cardiology_nurse, sent["id"], _full(sent))
    assert resp.status_code == 404 and _err(resp) == "not_found"
    awaiting = client.get(f"{BASE}/ward/transfers/awaiting",
                          headers=cardiology_nurse["hdr"]).json()["transfers"]
    assert sent["id"] not in {t["id"] for t in awaiting}


def test_a_ward_account_with_no_department_sees_no_store(made):
    ward = _store(made)
    lost = _user("nurse", dept=None)
    view = client.get(f"{BASE}/ward/stock", headers=lost["hdr"]).json()
    assert ward["id"] not in {s["store"]["id"] for s in view["stores"]}
    assert view["stores"] == []


def test_the_dispatcher_receiving_gets_403(monkeypatch, made, dispensing, ph_b):
    # A nurse who also holds the pharmacy desk's grants (patched) sends a
    # transfer to her own ward and tries to receive it.
    grants = set(permissions.ROLE_PERMISSIONS["nurse"]) | {
        permissions.PHARMACY_STORE_READ, permissions.PHARMACY_STORE_WRITE}
    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "nurse", grants)
    both = _user("nurse", dept=WARD_DEPT)
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug)
    _lot(drug, dispensing["id"], 20, _days(200))
    indent = _raised(ph_b, ward, [(drug, 3)])
    assert _decide(both, indent["id"]).status_code == 200
    draft = _issue(both, indent["id"]).json()["transfer"]
    sent = _dispatch(both, draft["id"]).json()["transfer"]
    resp = _ward_receive(both, sent["id"], _full(sent))
    assert resp.status_code == 403 and _err(resp) == "same_person"


def test_the_pharmacy_desk_cannot_receive_a_ward_transfer(made, dispensing, nurse, ph_a, ph_b):
    _, _, sent, _ = _sent(made, dispensing, nurse, ph_a, ph_b)
    resp = client.post(f"{BASE}/transfers/{sent['id']}/receive", headers=ph_b["hdr"],
                       json={"lines": _full(sent)})
    assert resp.status_code == 422 and _err(resp) == "received_on_the_ward"
    resp = _ward_receive(ph_b, sent["id"], _full(sent))
    assert resp.status_code == 403, "ward receipt needs ward_stock.receive"


def test_the_raiser_never_decides_their_own_indent(made, dispensing, ph_a):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug)
    indent = _raised(ph_a, ward, [(drug, 2)])
    resp = _decide(ph_a, indent["id"])
    assert resp.status_code == 403 and _err(resp) == "same_person"


def test_a_nurse_cannot_decide_or_issue(made, dispensing, nurse):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug)
    indent = _raised(nurse, ward, [(drug, 2)])
    assert _decide(nurse, indent["id"]).status_code == 403
    assert _issue(nurse, indent["id"]).status_code == 403


def test_a_free_transfer_still_cannot_reach_a_ward_store(made, dispensing, ph_a):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug)
    _lot(drug, dispensing["id"], 10, _days(200))
    resp = client.post(f"{BASE}/transfers", headers=ph_a["hdr"], json={
        "from_store_code": dispensing["code"], "to_store_code": ward["code"],
        "lines": [{"helix_medication_id": drug, "quantity_units": 2}]})
    assert resp.status_code == 422 and _err(resp) == "store_not_transferable"


# ═════════════════════════════════════════════════════════════════════════════
#  What may be ward stock (GRN-RS5 (b), strictest)
# ═════════════════════════════════════════════════════════════════════════════

def test_a_drug_not_on_the_stock_list_cannot_be_asked_for(made, nurse):
    ward = _store(made)
    drug = _drug(made)
    resp = _raise(nurse, ward, [(drug, 2)])
    assert resp.status_code == 422 and _err(resp) == "not_on_imprest_list"
    _par(ward["id"], drug, par_units=0)
    resp = _raise(nurse, ward, [(drug, 2)])
    assert resp.status_code == 422 and _err(resp) == "not_on_imprest_list"


def test_a_drug_taken_off_the_list_cannot_be_issued_or_sent(made, dispensing, nurse,
                                                            ph_a, ph_b):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug)
    _lot(drug, dispensing["id"], 20, _days(200))
    indent = _raised(nurse, ward, [(drug, 4)])
    assert _decide(ph_b, indent["id"]).status_code == 200
    draft = _issue(ph_a, indent["id"]).json()["transfer"]
    with db_session() as db:
        db.execute("UPDATE pharmacy_par_levels SET is_active = 0 WHERE store_id = ? "
                   "AND helix_medication_id = ?", (ward["id"], drug))
    resp = _dispatch(ph_a, draft["id"])
    assert resp.status_code == 422 and _err(resp) == "not_on_imprest_list"
    client.post(f"{BASE}/transfers/{draft['id']}/cancel", headers=ph_a["hdr"],
                json={"reason": "Taken off the list"})
    resp = _issue(ph_a, indent["id"])
    assert resp.status_code == 422 and _err(resp) == "not_on_imprest_list"


@pytest.mark.parametrize("fields", [
    {"name": "Insulin Testlog 100 units/mL", "generic_name": "insulin"},
    {"name": "Heparin Testsodium 5000 units/mL", "generic_name": "heparin"},
    {"name": "Potassium Chloride 15% Injection", "generic_name": "potassium chloride"},
    {"name": "Sodium Chloride 3% Infusion", "generic_name": "sodium chloride", "strength": "3%"},
    {"name": "Magnesium Sulphate 50% Injection", "generic_name": "magnesium sulphate"},
], ids=["insulin", "heparin", "kcl", "hypertonic-saline", "magnesium"])
def test_a_barred_drug_is_refused_even_on_the_list(made, nurse, fields):
    ward = _store(made)
    drug = _drug(made, **fields)
    _par(ward["id"], drug)
    resp = _raise(nurse, ward, [(drug, 2)])
    assert resp.status_code == 422 and _err(resp) == "high_alert_barred", resp.text


def test_the_barred_list_starts_from_the_ismp_rules():
    from services import drug_safety_service as safety
    cited = [r for r in (*safety.DOSE_RULES, *safety.ROUTE_RULES) if "ISMP" in r["source"]]
    assert cited and len(transfers._ismp_rules()) == len(cited)
    for rule in cited:
        name = rule["match"][0]
        assert transfers.barred_reason({"name": f"{name} test", "generic_name": name,
                                        "strength": ""}), rule["id"]
    # Normal saline and paracetamol are not barred.
    for name in ("Sodium chloride 0.9% infusion", "Paracetamol 650mg Oral Tablet"):
        assert transfers.barred_reason({"name": name, "generic_name": None,
                                        "strength": "0.9%"}) is None


def test_ndps_stock_is_never_ward_stock(made, nurse):
    ward = _store(made)
    drug = _drug(made, ndps_category="narcotic", drug_schedule="X")
    _par(ward["id"], drug)
    resp = _raise(nurse, ward, [(drug, 1)])
    assert resp.status_code == 422 and _err(resp) == "ndps_not_ward_stock"


def test_an_unclassified_or_unconfirmed_drug_is_refused(made, nurse):
    ward = _store(made)
    unclassified = _drug(made, drug_schedule=None)
    draft_drug = _drug(made, identity_status="draft")
    for drug in (unclassified, draft_drug):
        _par(ward["id"], drug)
    assert _err(_raise(nurse, ward, [(unclassified, 1)])) == "drug_unclassified"
    assert _err(_raise(nurse, ward, [(draft_drug, 1)])) == "drug_not_confirmed"


def test_a_line_asks_no_more_than_the_par_ceiling_and_once(made, nurse):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug, par_units=10, max_units=12)
    resp = _raise(nurse, ward, [(drug, 13)])
    assert resp.status_code == 422 and _err(resp) == "over_par"
    _raised(nurse, ward, [(drug, 12)])
    resp = _raise(nurse, ward, [(drug, 1)])
    assert resp.status_code == 409 and _err(resp) == "already_requested"


def test_a_drug_barred_on_its_way_lands_quarantined(made, dispensing, nurse, ph_a, ph_b):
    ward, drug, sent, _ = _sent(made, dispensing, nurse, ph_a, ph_b, units=5)
    with db_session() as db:
        db.execute("UPDATE helix_medication_master SET name = 'Insulin wardtest' WHERE id = ?",
                   (drug,))
    resp = _ward_receive(nurse, sent["id"], _full(sent))
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        statuses = {r[0] for r in db.execute(
            "SELECT status FROM helix_inventory_lots WHERE store_id = ? AND medication_id = ?",
            (ward["id"], drug)).fetchall()}
    assert statuses == {"quarantined"}
    row = _audits("pharmacy_transfer_received", sent["id"])[-1]
    assert "high-alert" in row["metadata_json"]


def _ward_receive_witnessed(user, tid, lines, witness, pin=PIN):
    return client.post(f"{BASE}/ward/transfers/{tid}/receive", headers=user["hdr"],
                       json={"lines": lines, "witness_id": witness["id"], "witness_pin": pin})


def _indent_status(indent_id):
    with db_session() as db:
        return db.execute("SELECT status FROM indents WHERE id = ?", (indent_id,)).fetchone()[0]


def _made_ndps_on_its_way(made, dispensing, nurse, ph_a, ph_b, category="psychotropic"):
    """A drug sent to a ward, then classified NDPS through the drug master
    while its units are on their way (classify_drug applies an upgrade at
    once, stock or no stock)."""
    ward, drug, sent, indent = _sent(made, dispensing, nurse, ph_a, ph_b, units=5)
    out = masters.classify_drug(drug, {"ndps_category": category}, actor=ph_b,
                                reason="Reclassified by the formulary committee")
    assert out["applied"] == ["ndps_category"]
    return ward, drug, sent, indent


def test_a_drug_made_ndps_on_its_way_is_received_witnessed_and_held(made, dispensing, nurse,
                                                                    ph_a, ph_b):
    ward, drug, sent, indent = _made_ndps_on_its_way(made, dispensing, nurse, ph_a, ph_b)
    witness = _user("pharmacist", pin=PIN)

    # The ward sees it is controlled now, and who may witness: never the
    # nurse, never the pharmacist who sent it.
    resp = client.get(f"{BASE}/ward/transfers/awaiting", headers=nurse["hdr"])
    assert resp.status_code == 200, resp.text
    doc = next(t for t in resp.json()["transfers"] if t["id"] == sent["id"])
    assert doc["controlled"] is True and doc["witness_required"] is True
    ids = {w["id"] for w in doc["witnesses"]}
    assert witness["id"] in ids
    assert nurse["id"] not in ids and ph_a["id"] not in ids

    # Never refused for a grant a ward role cannot hold; a witness is asked for.
    resp = _ward_receive(nurse, sent["id"], _full(sent))
    assert resp.status_code == 422 and _err(resp) == "witness_required", resp.text
    resp = _ward_receive_witnessed(nurse, sent["id"], _full(sent), ph_a)
    assert resp.status_code == 403 and _err(resp) == "witness_sent_it"
    resp = _ward_receive_witnessed(nurse, sent["id"], _full(sent), nurse)
    assert resp.status_code == 403 and _err(resp) == "witness_same_person"
    other_nurse = _user("nurse", dept=WARD_DEPT, pin=PIN)
    resp = _ward_receive_witnessed(nurse, sent["id"], _full(sent), other_nurse)
    assert resp.status_code == 403 and _err(resp) == "witness_not_eligible"
    resp = _ward_receive_witnessed(nurse, sent["id"], _full(sent), witness, pin="0000")
    assert resp.status_code == 403 and _err(resp) == "witness_pin_wrong"
    # The pharmacy desk still never receives for the ward.
    resp = client.post(f"{BASE}/transfers/{sent['id']}/receive", headers=witness["hdr"],
                       json={"lines": _full(sent)})
    assert resp.status_code == 422 and _err(resp) == "received_on_the_ward"
    with db_session() as db:
        assert db.execute("SELECT status FROM pharmacy_transfers WHERE id = ?",
                          (sent["id"],)).fetchone()[0] == "dispatched"
    assert _indent_status(indent["id"]) == "converted"

    resp = _ward_receive_witnessed(nurse, sent["id"], _full(sent), witness)
    assert resp.status_code == 200, resp.text
    assert resp.json()["transfer"]["status"] == "received"
    with db_session() as db:
        lots = [dict(r) for r in db.execute(
            "SELECT status, quantity FROM helix_inventory_lots WHERE store_id = ? "
            "AND medication_id = ?", (ward["id"], drug)).fetchall()]
    assert lots == [{"status": "quarantined", "quantity": 5}], lots
    assert _indent_status(indent["id"]) == "closed"
    row = _audits("pharmacy_transfer_received", sent["id"])[-1]
    assert witness["id"] in row["metadata_json"]
    assert '"controlled": true' in row["metadata_json"]
    assert "narcotic or psychotropic" in row["metadata_json"]
    with db_session() as db:
        assert transfers.transfer_balance(db, sent["id"])["balanced"]
        lot_ids = {r[0] for r in db.execute("SELECT id FROM helix_inventory_lots "
                                            "WHERE medication_id = ?", (drug,)).fetchall()}
        assert stock.reconcile(db, lot_ids=list(lot_ids))["clean"]

    # The indent no longer holds the drug: asking again is refused for what
    # the drug now is, not as already asked for.
    resp = _raise(nurse, ward, [(drug, 2)])
    assert resp.status_code == 422 and _err(resp) == "ndps_not_ward_stock"


def test_a_ward_receipt_of_controlled_stock_needs_a_witness_whatever_the_setting(
        monkeypatch, made, dispensing, nurse, ph_a, ph_b):
    """NDPS_WITNESS_ALWAYS off and the register off would let the pharmacy
    lane go unwitnessed; the ward lane never does, since the witness is the
    only person on it holding the NDPS permission."""
    monkeypatch.setattr(transfers, "NDPS_WITNESS_ALWAYS", False)
    monkeypatch.setattr(config, "PHARMACY_NDPS_REGISTER_ENABLED", False)
    _, _, sent, indent = _made_ndps_on_its_way(made, dispensing, nurse, ph_a, ph_b,
                                               category="narcotic")
    assert transfers.get_transfer(sent["id"])["witness_required"] is True
    resp = _ward_receive(nurse, sent["id"], _full(sent))
    assert resp.status_code == 422 and _err(resp) == "witness_required"
    resp = _ward_receive_witnessed(nurse, sent["id"], _full(sent), _user("pharmacist", pin=PIN))
    assert resp.status_code == 200, resp.text
    assert _indent_status(indent["id"]) == "closed"


def test_a_ward_receipt_short_of_a_drug_made_ndps_closes_with_its_reason(
        made, dispensing, nurse, ph_a, ph_b):
    """The witnessed count may find units missing: booked short with a
    reason, the indent closes, nothing stays in transit."""
    ward, drug, sent, indent = _made_ndps_on_its_way(made, dispensing, nurse, ph_a, ph_b)
    line = sent["lines"][0]
    resp = _ward_receive_witnessed(
        nurse, sent["id"], [{"line_id": line["id"], "received_units": 3,
                             "discrepancy_reason": "Two ampoules broken in the carrier"}],
        _user("pharmacist", pin=PIN))
    assert resp.status_code == 200, resp.text
    doc = resp.json()["transfer"]
    assert doc["status"] == "received" and doc["in_transit_units"] == 0
    assert _indent_status(indent["id"]) == "closed"
    with db_session() as db:
        assert {r[0] for r in db.execute(
            "SELECT status FROM helix_inventory_lots WHERE store_id = ? AND medication_id = ?",
            (ward["id"], drug)).fetchall()} == {"quarantined"}


def test_a_ward_receipt_of_a_drug_made_ndps_writes_the_register_with_its_witness(
        monkeypatch, made, dispensing, nurse, ph_a, ph_b):
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setattr(config, "PHARMACY_NDPS_REGISTER_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_NDPS_ON_FAILURE", "block")
    from services import ndps_service

    assert ndps_service.register_on() is True
    ward, drug, sent, indent = _made_ndps_on_its_way(made, dispensing, nurse, ph_a, ph_b)
    witness = _user("pharmacist", pin=PIN)
    resp = _ward_receive_witnessed(nurse, sent["id"], _full(sent), witness)
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT entry_type, quantity_units, balance_after_units, actor_id, witness_id, "
            "counterparty_type, counterparty_ref FROM ndps_register WHERE store_id = ? "
            "AND helix_medication_id = ? ORDER BY seq", (ward["id"], drug)).fetchall()]
    assert [r["entry_type"] for r in rows] == ["opening", "transfer_in"], rows
    entry = rows[-1]
    assert (entry["quantity_units"], entry["balance_after_units"]) == (5, 5)
    assert entry["actor_id"] == nurse["id"] and entry["witness_id"] == witness["id"]
    assert (entry["counterparty_type"], entry["counterparty_ref"]) == (
        "store", str(dispensing["id"]))
    assert _indent_status(indent["id"]) == "closed"


def _ward_lots(ward, drug):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, status, quantity FROM helix_inventory_lots WHERE store_id = ? "
            "AND medication_id = ? ORDER BY rowid", (ward["id"], drug)).fetchall()]


def test_a_drug_made_ndps_on_its_way_onto_the_wards_own_batch_holds_both(
        made, dispensing, nurse, ph_a, ph_b):
    """The ward already holds this batch as available stock (an earlier top-up
    from the same lot). Units are never merged across statuses and the ward
    has no screen to hold a lot, so instead of a 409 that would keep the
    delivery on its way for good, the ward's lot is held with the arriving
    units: one quarantined lot, a lot event naming the transfer."""
    ward, drug, sent, indent = _sent(made, dispensing, nurse, ph_a, ph_b, units=5)
    line = sent["lines"][0]
    twin = _lot(drug, ward["id"], 4, line["expiry_date"], batch=line["batch_no"])
    masters.classify_drug(drug, {"ndps_category": "narcotic"}, actor=ph_b,
                          reason="Reclassified by the formulary committee")
    witness = _user("pharmacist", pin=PIN)
    resp = _ward_receive_witnessed(nurse, sent["id"], _full(sent), witness)
    assert resp.status_code == 200, resp.text
    assert _ward_lots(ward, drug) == [{"id": twin["id"], "status": "quarantined", "quantity": 9}]
    assert _indent_status(indent["id"]) == "closed"
    with db_session() as db:
        event = dict(db.execute(
            "SELECT * FROM pharmacy_lot_events WHERE lot_id = ? ORDER BY id DESC LIMIT 1",
            (twin["id"],)).fetchone())
        lot_ids = {r[0] for r in db.execute("SELECT id FROM helix_inventory_lots "
                                            "WHERE medication_id = ?", (drug,)).fetchall()}
        assert stock.reconcile(db, lot_ids=list(lot_ids))["clean"]
    assert (event["from_status"], event["to_status"]) == ("available", "quarantined")
    assert (event["reference_type"], event["reference_id"]) == (
        transfers.REFERENCE_TYPE, str(sent["id"]))
    assert event["actor_id"] == nurse["id"]
    row = _audits("pharmacy_transfer_received", sent["id"])[-1]
    assert '"ward_held_units": 4' in row["metadata_json"]


def test_an_ordinary_delivery_onto_a_held_batch_lands_held_never_releasing_it(
        made, dispensing, nurse, ph_a, ph_b):
    """The ward holds this batch quarantined: the arriving units of the same
    batch land quarantined with it (the stronger hold), never the reverse."""
    ward, drug, sent, indent = _sent(made, dispensing, nurse, ph_a, ph_b, units=5)
    line = sent["lines"][0]
    twin = _lot(drug, ward["id"], 2, line["expiry_date"], batch=line["batch_no"],
                status="quarantined")
    resp = _ward_receive(nurse, sent["id"], _full(sent))
    assert resp.status_code == 200, resp.text
    assert _ward_lots(ward, drug) == [{"id": twin["id"], "status": "quarantined", "quantity": 7}]
    received = resp.json()["transfer"]["lines"][0]
    assert received["to_lot_status"] == "quarantined"
    assert _indent_status(indent["id"]) == "closed"
    row = _audits("pharmacy_transfer_received", sent["id"])[-1]
    assert "already held in this ward store as quarantined" in row["metadata_json"]


def test_the_pharmacy_lane_still_refuses_a_batch_held_under_another_status(
        made, dispensing, ph_a, ph_b):
    """The same-batch rule is the ward's alone: between the pharmacy's own
    stores a receipt onto a batch held under another status stays a 409."""
    sub = _store(made, "sub_store", dept=None)
    drug = _drug(made)
    lot = _lot(drug, dispensing["id"], 10, _days(200))
    _lot(drug, sub["id"], 1, _days(200), batch=lot["lot_number"], status="quarantined")
    resp = client.post(f"{BASE}/transfers", headers=ph_a["hdr"],
                       json={"from_store_code": dispensing["code"], "to_store_code": sub["code"],
                             "lines": [{"helix_medication_id": drug, "quantity_units": 3}]})
    assert resp.status_code == 200, resp.text
    resp = _dispatch(ph_a, resp.json()["transfer"]["id"])
    assert resp.status_code == 200, resp.text
    sent = resp.json()["transfer"]
    resp = client.post(f"{BASE}/transfers/{sent['id']}/receive", headers=ph_b["hdr"],
                       json={"lines": _full(sent)})
    assert resp.status_code == 409 and _err(resp) == "lot_status_conflict"


def test_an_ordinary_ward_receipt_still_needs_no_witness(made, dispensing, nurse, ph_a, ph_b):
    """Nothing controlled: no witness asked for, none listed, and one sent
    along is not used."""
    _, _, sent, _ = _sent(made, dispensing, nurse, ph_a, ph_b, units=4)
    resp = client.get(f"{BASE}/ward/transfers/awaiting", headers=nurse["hdr"])
    doc = next(t for t in resp.json()["transfers"] if t["id"] == sent["id"])
    assert doc["controlled"] is False and doc["witness_required"] is False
    assert "witnesses" not in doc
    resp = _ward_receive_witnessed(nurse, sent["id"], _full(sent), ph_a, pin="0000")
    assert resp.status_code == 200, resp.text
    row = _audits("pharmacy_transfer_received", sent["id"])[-1]
    assert '"witness_id": null' in row["metadata_json"]


# ═════════════════════════════════════════════════════════════════════════════
#  The stores desk, cancelling, and audit
# ═════════════════════════════════════════════════════════════════════════════

def test_the_stores_desk_cannot_see_an_issue_indent(made, nurse):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug)
    indent = _raised(nurse, ward, [(drug, 2)])
    admin = _user("admin")
    listed = client.get("/api/procurement/indents?limit=500", headers=admin["hdr"])
    assert listed.status_code == 200
    assert indent["id"] not in {i["id"] for i in listed.json()["indents"]}
    one = client.get(f"/api/procurement/indents/{indent['id']}", headers=admin["hdr"])
    assert one.status_code == 404
    with pytest.raises(procurement.ProcurementError) as exc:
        with db_session(immediate=True) as db:
            procurement._decide_indent_core(db, indent["id"], actor="X")
    assert exc.value.code == "not_found"


def test_the_ward_withdraws_before_the_decision_only(made, dispensing, nurse, ph_b):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug)
    first = _raised(nurse, ward, [(drug, 2)])
    resp = client.post(f"{BASE}/ward/indents/{first['id']}/cancel", headers=nurse["hdr"],
                       json={})
    assert resp.status_code == 422 and _err(resp) == "missing_field"
    resp = client.post(f"{BASE}/ward/indents/{first['id']}/cancel", headers=nurse["hdr"],
                       json={"reason": "Asked twice"})
    assert resp.status_code == 200 and resp.json()["indent"]["status"] == "cancelled"
    second = _raised(nurse, ward, [(drug, 2)])
    assert _decide(ph_b, second["id"]).status_code == 200
    resp = client.post(f"{BASE}/ward/indents/{second['id']}/cancel", headers=nurse["hdr"],
                       json={"reason": "Changed my mind"})
    assert resp.status_code == 409 and _err(resp) == "illegal_transition"


def test_one_live_transfer_per_indent(made, dispensing, nurse, ph_a, ph_b):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug)
    _lot(drug, dispensing["id"], 20, _days(200))
    indent = _raised(nurse, ward, [(drug, 4)])
    assert _decide(ph_b, indent["id"]).status_code == 200
    assert _issue(ph_a, indent["id"]).status_code == 200
    resp = _issue(ph_a, indent["id"])
    assert resp.status_code == 409 and _err(resp) == "transfer_open"


def test_an_indent_with_no_audit_row_is_not_written(monkeypatch, made, nurse):
    ward = _store(made)
    drug = _drug(made)
    _par(ward["id"], drug)

    def broken(*args, **kwargs):
        raise audit_service.AuditWriteError("audit store unavailable")

    monkeypatch.setattr(audit_service, "log_audit_event", broken)
    resp = _raise(nurse, ward, [(drug, 2)])
    assert resp.status_code == 503 and _err(resp) == "audit_unavailable"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM indents WHERE store_id = ?",
                          (ward["id"],)).fetchone()[0] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  An approved indent never sticks on one line (what the pharmacy screen uses)
# ═════════════════════════════════════════════════════════════════════════════

def test_a_mixed_indent_issues_the_lines_that_can_go_and_frees_the_rest(
        made, dispensing, nurse, ph_a, ph_b):
    ward = _store(made)
    plenty = _drug(made)
    short = _drug(made)
    _par(ward["id"], plenty)
    _par(ward["id"], short)
    _lot(plenty, dispensing["id"], 30, _days(200))
    _lot(short, dispensing["id"], 1, _days(200))
    indent = _raised(nurse, ward, [(plenty, 6), (short, 5)])
    assert _decide(ph_b, indent["id"]).status_code == 200

    plan = client.get(f"{BASE}/ward/indents/{indent['id']}/issue-plan", headers=ph_a["hdr"])
    assert plan.status_code == 200, plan.text
    lines = {line["line_no"]: line for line in plan.json()["lines"]}
    assert lines[1]["problem"] is None
    assert lines[2]["problem"]["code"] == "insufficient_stock"
    assert lines[2]["usable_units"] == 1

    # Everything at once is refused, as before.
    resp = _issue(ph_a, indent["id"])
    assert resp.status_code == 409 and _err(resp) == "insufficient_stock"
    # Naming the line that can go drafts that line alone.
    resp = _issue(ph_a, indent["id"], lines=[{"line_no": 1, "units": 6}])
    assert resp.status_code == 200, resp.text
    draft = resp.json()["transfer"]
    assert {line["helix_medication_id"] for line in draft["lines"]} == {plenty}

    # The left-out drug waits on this indent until the delivery is in ...
    resp = _raise(nurse, ward, [(short, 2)])
    assert resp.status_code == 409 and _err(resp) == "already_requested"
    resp = _dispatch(ph_a, draft["id"])
    assert resp.status_code == 200, resp.text
    sent = resp.json()["transfer"]
    assert _ward_receive(nurse, sent["id"], _full(sent)).status_code == 200
    closed = client.get(f"{BASE}/ward/indents/{indent['id']}", headers=nurse["hdr"]).json()
    assert closed["indent"]["status"] == "closed"
    by_line = {line["line_no"]: line for line in closed["indent"]["lines"]}
    assert by_line[1]["issued_units"] == 6 and by_line[2]["issued_units"] == 0
    # ... and then the ward asks for it again.
    again = _raised(nurse, ward, [(short, 2)])
    assert again["status"] == "submitted"


def test_a_short_line_sends_what_there_is_and_never_more(made, dispensing, nurse, ph_a, ph_b):
    ward = _store(made)
    short = _drug(made)
    _par(ward["id"], short)
    _lot(short, dispensing["id"], 1, _days(200))
    indent = _raised(nurse, ward, [(short, 5)])
    assert _decide(ph_b, indent["id"]).status_code == 200
    resp = _issue(ph_a, indent["id"], lines=[{"line_no": 1, "units": 2}])
    assert resp.status_code == 409 and _err(resp) == "insufficient_stock"
    resp = _issue(ph_a, indent["id"], lines=[{"line_no": 1, "units": 1}])
    assert resp.status_code == 200, resp.text
    assert sum(line["quantity_units"] for line in resp.json()["transfer"]["lines"]) == 1


def test_the_desk_cancels_an_approved_indent_and_the_ward_asks_again(
        made, dispensing, nurse, ph_a, ph_b):
    ward = _store(made)
    short = _drug(made)
    _par(ward["id"], short)
    _lot(short, dispensing["id"], 1, _days(200))
    indent = _raised(nurse, ward, [(short, 5)])
    assert _decide(ph_b, indent["id"]).status_code == 200
    url = f"{BASE}/ward/indents/{indent['id']}/cancel"

    # The ward itself withdraws only before the decision.
    resp = client.post(url, headers=nurse["hdr"], json={"reason": "No longer needed"})
    assert resp.status_code == 409 and _err(resp) == "illegal_transition"
    # Never while a draft for it is open.
    draft = _issue(ph_a, indent["id"], lines=[{"line_no": 1, "units": 1}]).json()["transfer"]
    resp = client.post(url, headers=ph_a["hdr"], json={"reason": "Out of stock"})
    assert resp.status_code == 409 and _err(resp) == "transfer_open"
    assert client.post(f"{BASE}/transfers/{draft['id']}/cancel", headers=ph_a["hdr"],
                       json={"reason": "Not sent"}).status_code == 200
    # A reason is required.
    resp = client.post(url, headers=ph_a["hdr"], json={})
    assert resp.status_code == 422 and _err(resp) == "missing_field"

    resp = client.post(url, headers=ph_a["hdr"], json={"reason": "Out of stock until Monday"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["indent"]["status"] == "cancelled"
    assert _audits("pharmacy_ward_indent_cancelled", indent["id"])
    again = _raised(nurse, ward, [(short, 2)])
    assert again["status"] == "submitted"
