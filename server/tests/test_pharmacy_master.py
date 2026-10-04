"""Pharmacy masters (grn-5): /api/pharmacy-store drugs, classification, stores,
par levels and vendors, behind ORB_PHARMACY_STORE.

Pinned (docs/his_design/grn.json, grn-5 tests):
  * with the module off every masters route answers 404 module_disabled;
  * name, generic name, form and strength cannot be changed here (422); a
    schedule, NDPS or storage value outside its set is refused (422);
  * an unclassified drug reads 'Not classified', never 'none';
  * a classification that adds control applies at once; one that removes it is
    a pending request (GRN-R20), which the requester cannot decide, which a
    different approver applies, and which is refused while NDPS vault stock
    exists, at request time and again at approval;
  * a second active dispensing store is refused; creating the dispensing store
    adopts every legacy lot in the same transaction, with reconcile() clean;
  * a ward store is refused with the ward-stock switch off, and an unknown
    department is refused with it on;
  * a store holding stock is never deactivated;
  * par levels and vendors (with drug licence status) through the shared core,
    working with Procurement switched off;
  * every write leaves a 'security' audit row whose target_id is a string, in
    the same transaction as the change.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token, hash_password
from database import db_session
from main import app
from services import pharmacy_master_service as masters
from services import pharmacy_stock_service as stock
from services.helix_db import init_helix_db

client = TestClient(app)
BASE = "/api/pharmacy-store"
LATER = (date.today() + timedelta(days=300)).isoformat()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture(autouse=True)
def module_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)


def _user(role, department=None):
    uid = f"UPM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, department, hash_password("secretpass")))
    return {"id": uid, "role": role,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


@pytest.fixture
def pharmacist():
    return _user("pharmacist")


@pytest.fixture
def admin():
    return _user("admin")


@pytest.fixture
def admin2():
    return _user("admin")


def _drug(**fields):
    med = f"MEDPM{uuid.uuid4().hex[:6].upper()}"
    cols = {"id": med, "name": f"Mastertest {med[-6:]} 10 mg", "generic_name": "mastermol",
            "form": "tablet", "strength": "10 mg"} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    return med


def _row(med):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM helix_medication_master WHERE id = ?",
                               (med,)).fetchone())


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(target_id))).fetchall()]


class _Made(list):
    """Store ids a test created, and the legacy lots a dispensing store adopted."""

    def __init__(self):
        super().__init__()
        self.adopted: set[str] = set()


@pytest.fixture
def stores_made():
    """Pharmacy stores created by a test, removed afterwards. Every lot a
    dispensing store adopted (this module's or any other module's) goes back
    to store NULL exactly as it was; only lots a test opened directly in one
    of its stores are deleted. So the shared database never keeps a
    dispensing store, and no other test's lot is moved or lost."""
    made = _Made()
    yield made
    with db_session() as db:
        for lot_id in made.adopted:
            db.execute("UPDATE helix_inventory_lots SET store_id = NULL WHERE id = ?", (lot_id,))
        for store_id in made:
            for (lot_id,) in db.execute("SELECT id FROM helix_inventory_lots WHERE store_id = ?",
                                        (store_id,)).fetchall():
                db.execute("DELETE FROM helix_stock_movements WHERE lot_id = ?", (lot_id,))
                db.execute("DELETE FROM pharmacy_lot_events WHERE lot_id = ?", (lot_id,))
                db.execute("DELETE FROM helix_inventory_lots WHERE id = ?", (lot_id,))
            db.execute("DELETE FROM pharmacy_par_levels WHERE store_id = ?", (store_id,))
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def _create_store(user, made, kind, **extra):
    code = f"PM{uuid.uuid4().hex[:6].upper()}"
    resp = client.post(f"{BASE}/stores", headers=user["hdr"],
                       json={"code": code, "name": f"Store {code}", "pharmacy_kind": kind}
                       | extra)
    if resp.status_code == 200:
        made.append(resp.json()["store"]["id"])
        adoption = resp.json().get("adoption") or {}
        made.adopted.update(adoption.get("adopted", []))
    return resp


def _vault_lot(made, admin, med, units=4):
    resp = _create_store(admin, made, "ndps_vault")
    assert resp.status_code == 200, resp.text
    vault = resp.json()["store"]["id"]
    with db_session(immediate=True) as db:
        stock.open_or_merge_lot(db, medication_id=med, lot_number=f"V{uuid.uuid4().hex[:5]}",
                                expiration_date=LATER, units=units, actor_id="tester",
                                store_id=vault)
    return vault


# ═════════════════════════════════════════════════════════════════════════════
#  The switch and the tiers
# ═════════════════════════════════════════════════════════════════════════════

def test_every_masters_route_is_404_with_the_module_off(monkeypatch, pharmacist):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    for method, path in (("get", "/drugs"), ("get", "/stores"), ("get", "/vendors"),
                         ("get", "/par-levels"), ("get", "/classification-requests"),
                         ("patch", "/drugs/X"), ("post", "/stores")):
        resp = getattr(client, method)(f"{BASE}{path}", headers=pharmacist["hdr"],
                                       **({} if method == "get" else {"json": {}}))
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"


def test_the_tiers_hold(pharmacist, admin):
    doctor = _user("doctor", "Cardiology")
    med = _drug()
    assert client.get(f"{BASE}/drugs", headers=doctor["hdr"]).status_code == 403
    # Approve does not imply write, and write does not imply approve.
    assert client.patch(f"{BASE}/drugs/{med}", headers=admin["hdr"],
                        json={"manufacturer": "Acme"}).status_code == 403
    assert client.post(f"{BASE}/stores", headers=pharmacist["hdr"],
                       json={"code": "PMX", "name": "X", "pharmacy_kind": "sub_store"}
                       ).status_code == 403
    assert client.post(f"{BASE}/classification-requests/1/decision", headers=pharmacist["hdr"],
                       json={"decision": "approved"}).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
#  Drug fields
# ═════════════════════════════════════════════════════════════════════════════

def test_an_unclassified_drug_reads_not_classified(pharmacist):
    med = _drug()
    body = client.get(f"{BASE}/drugs", headers=pharmacist["hdr"],
                      params={"search": med}).json()
    drug = next(d for d in body["drugs"] if d["id"] == med)
    assert drug["schedule_label"] == drug["ndps_label"] == "Not classified"
    assert drug["gst_label"] == drug["storage_label"] == "Not configured"
    assert drug["drug_schedule"] is None and drug["ndps_category"] is None
    assert [o["value"] for o in body["options"]["drug_schedules"]] == list(stock.DRUG_SCHEDULES)


@pytest.mark.parametrize("field", ["name", "generic_name", "form", "strength"])
def test_clinical_identity_cannot_be_changed_here(pharmacist, field):
    med = _drug()
    before = _row(med)
    resp = client.patch(f"{BASE}/drugs/{med}", headers=pharmacist["hdr"],
                        json={field: "something else", "manufacturer": "Acme"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "identity_locked"
    assert _row(med) == before


@pytest.mark.parametrize("body, code", [
    ({"storage_condition": "fridge"}, "bad_value"),
    ({"gst_rate_bp": 5000}, "bad_value"),
    ({"hsn_code": "30A4"}, "bad_value"),
    ({"pack_size_units": 0}, "bad_value"),
    ({"is_active": "yes"}, "bad_value"),
    ({"drug_schedule": "H"}, "unknown_field"),
    ({"identity_status": "retired"}, "identity_locked"),
    ({"colour": "blue"}, "unknown_field"),
    ({}, "bad_value"),
])
def test_out_of_set_and_unknown_fields_are_refused(pharmacist, body, code):
    med = _drug()
    before = _row(med)
    resp = client.patch(f"{BASE}/drugs/{med}", headers=pharmacist["hdr"], json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code
    assert _row(med) == before


def test_the_write_tier_edits_commercial_fields_and_it_is_audited(pharmacist):
    med = _drug()
    resp = client.patch(f"{BASE}/drugs/{med}", headers=pharmacist["hdr"], json={
        "pack_size_units": 10, "purchase_uom": "strip", "dispense_unit": "tablet",
        "hsn_code": "30049099", "gst_rate_bp": 500, "manufacturer": "Acme Pharma",
        "storage_condition": "cool", "reorder_level_units": 50, "reorder_qty_units": 200,
        "lead_time_days": 7})
    assert resp.status_code == 200, resp.text
    drug = resp.json()["drug"]
    assert drug["gst_label"] == "5%" and drug["storage_label"] == "Cool place"
    row = _row(med)
    assert (row["pack_size_units"], row["hsn_code"], row["updated_by"]) == (
        10, "30049099", pharmacist["id"])
    audits = _audits("pharmacy_drug_master_updated", med)
    assert len(audits) == 1
    assert audits[0]["event_category"] == "security"
    assert isinstance(audits[0]["target_id"], str)
    assert json.loads(audits[0]["metadata_json"])["changes"]["gst_rate_bp"] == {
        "from": None, "to": 500}
    # Clearing an optional field, and a no-op edit that writes no audit row.
    client.patch(f"{BASE}/drugs/{med}", headers=pharmacist["hdr"], json={"gst_rate_bp": None})
    assert _row(med)["gst_rate_bp"] is None
    client.patch(f"{BASE}/drugs/{med}", headers=pharmacist["hdr"], json={"gst_rate_bp": None})
    assert len(_audits("pharmacy_drug_master_updated", med)) == 2


def test_an_unknown_drug_is_404(pharmacist):
    resp = client.patch(f"{BASE}/drugs/NO-SUCH-DRUG", headers=pharmacist["hdr"],
                        json={"manufacturer": "X"})
    assert resp.status_code == 404
    assert client.get(f"{BASE}/drugs/NO-SUCH-DRUG", headers=pharmacist["hdr"]).status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
#  Classification and the two-person downgrade
# ═════════════════════════════════════════════════════════════════════════════

def _classify(user, med, **body):
    return client.post(f"{BASE}/drugs/{med}/classification", headers=user["hdr"], json=body)


def test_a_first_classification_and_an_upgrade_apply_at_once(pharmacist):
    med = _drug()
    resp = _classify(pharmacist, med, drug_schedule="H", ndps_category="none")
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] == ["drug_schedule", "ndps_category"]
    assert resp.json()["requested"] == []
    resp = _classify(pharmacist, med, drug_schedule="X", ndps_category="psychotropic")
    assert resp.json()["applied"] == ["drug_schedule", "ndps_category"]
    row = _row(med)
    assert (row["drug_schedule"], row["ndps_category"]) == ("X", "psychotropic")
    audit = _audits("pharmacy_classification_changed", med)[-1]
    assert json.loads(audit["metadata_json"])["changes"]["drug_schedule"]["kind"] == "upgrade"


@pytest.mark.parametrize("body", [{"drug_schedule": "Z"}, {"ndps_category": "opioid"},
                                  {"name": "x"}])
def test_an_out_of_set_classification_is_refused(pharmacist, body):
    med = _drug()
    assert _classify(pharmacist, med, **body).status_code == 422


def test_a_downgrade_waits_for_a_different_approver(pharmacist, admin):
    med = _drug(drug_schedule="H1", ndps_category="none")
    assert _classify(pharmacist, med, drug_schedule="G").json()["detail"]["error"] \
        == "missing_reason"
    resp = _classify(pharmacist, med, drug_schedule="G", reason="Reclassified by notification")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] == [] and len(body["requested"]) == 1
    request = body["requested"][0]
    assert (request["field"], request["from_value"], request["to_value"]) == ("drug_schedule",
                                                                              "H1", "G")
    assert _row(med)["drug_schedule"] == "H1", "a downgrade never applies on one person's word"
    assert _audits("pharmacy_classification_change_requested", request["id"])

    # A second request for the same field waits behind the first.
    again = _classify(pharmacist, med, drug_schedule="none", reason="again")
    assert again.status_code == 409 and again.json()["detail"]["error"] == "request_pending"

    # The requester cannot decide it, whatever their tier.
    with pytest.raises(masters.MasterError) as exc:
        masters.decide_change_request(request["id"], approve=True,
                                      actor={"id": pharmacist["id"], "role": "admin"})
    assert exc.value.code == "same_person"

    listed = client.get(f"{BASE}/classification-requests", headers=admin["hdr"]).json()
    assert any(r["id"] == request["id"] for r in listed["requests"])
    decided = client.post(f"{BASE}/classification-requests/{request['id']}/decision",
                          headers=admin["hdr"], json={"decision": "approved"})
    assert decided.status_code == 200, decided.text
    assert decided.json()["request"]["decision"] == "approved"
    assert decided.json()["request"]["decided_by"] == admin["id"]
    assert _row(med)["drug_schedule"] == "G"
    assert _audits("pharmacy_classification_change_decided", request["id"])


def test_a_downgrade_is_refused_while_vault_stock_exists(pharmacist, admin, stores_made):
    med = _drug(drug_schedule="X", ndps_category="narcotic")
    _vault_lot(stores_made, admin, med)
    resp = _classify(pharmacist, med, ndps_category="none", reason="Wrongly classified")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "vault_stock"
    assert _row(med)["ndps_category"] == "narcotic"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM pharmacy_master_change_requests "
                          "WHERE helix_medication_id = ?", (med,)).fetchone()[0] == 0


def test_approval_rechecks_vault_stock_and_staleness(pharmacist, admin, stores_made):
    med = _drug(drug_schedule="X", ndps_category="narcotic")
    request = _classify(pharmacist, med, ndps_category="psychotropic",
                        reason="Reclassified").json()["requested"][0]
    _vault_lot(stores_made, admin, med)
    resp = client.post(f"{BASE}/classification-requests/{request['id']}/decision",
                       headers=admin["hdr"], json={"decision": "approved"})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "vault_stock"
    assert _row(med)["ndps_category"] == "narcotic"

    other = _drug(drug_schedule="H")
    stale = _classify(pharmacist, other, drug_schedule="G", reason="r").json()["requested"][0]
    assert _classify(pharmacist, other, drug_schedule="X").json()["applied"] == ["drug_schedule"]
    resp = client.post(f"{BASE}/classification-requests/{stale['id']}/decision",
                       headers=admin["hdr"], json={"decision": "approved"})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "request_stale"
    assert _row(other)["drug_schedule"] == "X"


def test_refusal_needs_a_reason_and_withdrawal_is_the_requesters(pharmacist, admin):
    med = _drug(drug_schedule="H")
    request = _classify(pharmacist, med, drug_schedule="none",
                        reason="r").json()["requested"][0]
    resp = client.post(f"{BASE}/classification-requests/{request['id']}/decision",
                       headers=admin["hdr"], json={"decision": "refused"})
    assert resp.status_code == 422
    other_pharmacist = _user("pharmacist")
    resp = client.post(f"{BASE}/classification-requests/{request['id']}/withdraw",
                       headers=other_pharmacist["hdr"])
    assert resp.status_code == 403
    resp = client.post(f"{BASE}/classification-requests/{request['id']}/withdraw",
                       headers=pharmacist["hdr"])
    assert resp.status_code == 200 and resp.json()["request"]["decision"] == "withdrawn"
    resp = client.post(f"{BASE}/classification-requests/{request['id']}/decision",
                       headers=admin["hdr"], json={"decision": "approved"})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "decided"
    assert _row(med)["drug_schedule"] == "H"


def test_the_essential_narcotic_mark_eases_control_so_it_waits_for_approval(pharmacist):
    plain = _drug(ndps_category="psychotropic")
    resp = _classify(pharmacist, plain, is_essential_narcotic=True, reason="r")
    assert resp.status_code == 422, "only a narcotic can be an essential narcotic"
    narcotic = _drug(ndps_category="narcotic")
    resp = _classify(pharmacist, narcotic, is_essential_narcotic=True, reason="Listed as END")
    assert resp.status_code == 200
    assert resp.json()["requested"][0]["field"] == "is_essential_narcotic"
    assert _row(narcotic)["is_essential_narcotic"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  Stores and adoption
# ═════════════════════════════════════════════════════════════════════════════

def _legacy_lot(med, units, batch):
    lot_id = f"LEG{uuid.uuid4().hex[:8].upper()}"
    with db_session(immediate=True) as db:
        db.execute("INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, "
                   "expiration_date) VALUES (?, ?, ?, 0, ?)", (lot_id, med, batch, LATER))
        if units:
            stock.post_movement(db, lot_id=lot_id, movement_type="opening",
                                quantity_change=units, actor_id="system")
    return lot_id


def test_creating_the_dispensing_store_adopts_every_legacy_lot(admin, stores_made):
    med = _drug()
    keep = _legacy_lot(med, 9, "ADOPT1")
    dup = _legacy_lot(med, 3, "ADOPT1")
    resp = _create_store(admin, stores_made, "dispensing")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    store_id = body["store"]["id"]
    assert keep in body["adoption"]["adopted"] and dup not in body["adoption"]["adopted"]
    assert {"from": dup, "into": keep, "units": 3} in body["adoption"]["merged"]
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM helix_inventory_lots WHERE store_id IS NULL "
                          "AND quantity > 0").fetchone()[0] == 0
        assert db.execute("SELECT store_id, quantity FROM helix_inventory_lots WHERE id = ?",
                          (keep,)).fetchone()[:] == (store_id, 12)
        assert stock.reconcile(db, lot_ids=[keep, dup])["clean"] is True
        assert stock.dispensable_units(db, med) == 12
    assert _audits("pharmacy_store_upserted", store_id)
    adoption_audit = _audits("pharmacy_legacy_lots_adopted", store_id)
    assert len(adoption_audit) == 1 and isinstance(adoption_audit[0]["target_id"], str)
    # The emptied duplicate is not reported as a lot still waiting for a
    # store: only a batch adoption had to leave alone (a conflict) is.
    left_alone = [lot for c in body["adoption"]["conflicts"] for lot in c["lot_ids"]]
    listing = client.get(f"{BASE}/stores", headers=admin["hdr"]).json()
    assert dup not in left_alone
    assert listing["legacy_lots"] == len(left_alone)

    # One dispensing pharmacy per site (GRN-R17).
    second = _create_store(admin, stores_made, "dispensing")
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"] == "dispensing_store_exists"


def test_a_store_holding_stock_is_never_closed(admin, stores_made):
    med = _drug(ndps_category="narcotic")
    vault = _vault_lot(stores_made, admin, med, units=2)
    code = next(s for s in masters.list_stores() if s["id"] == vault)["code"]
    resp = client.put(f"{BASE}/stores/{code}", headers=admin["hdr"], json={"is_active": False})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "store_holds_stock"
    empty = _create_store(admin, stores_made, "sub_store").json()["store"]
    resp = client.put(f"{BASE}/stores/{empty['code']}", headers=admin["hdr"],
                      json={"is_active": False, "name": "Old sub-store"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["store"]["is_active"] == 0
    assert resp.json()["store"]["name"] == "Old sub-store"


def test_ward_stores_need_the_switch_and_a_known_department(monkeypatch, admin, stores_made):
    resp = _create_store(admin, stores_made, "ward_imprest", department="Cardiology")
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "ward_stock_off"
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", True)
    resp = _create_store(admin, stores_made, "ward_imprest", department="Nowhere Ward")
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "department_unknown"
    resp = _create_store(admin, stores_made, "ward_imprest")
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "department_required"
    dept = f"Pharmtest {uuid.uuid4().hex[:4]}"
    _user("nurse", dept)
    resp = _create_store(admin, stores_made, "ward_imprest", department=dept.lower())
    assert resp.status_code == 200, resp.text
    assert resp.json()["store"]["department"] == dept


def test_an_unknown_store_kind_or_store_is_refused(admin, stores_made):
    assert _create_store(admin, stores_made, "shop").status_code == 422
    resp = client.put(f"{BASE}/stores/NOPE123", headers=admin["hdr"], json={"name": "x"})
    assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
#  Par levels
# ═════════════════════════════════════════════════════════════════════════════

def test_par_levels_are_set_per_store_and_drug(pharmacist, admin, stores_made):
    med = _drug()
    store_id = _create_store(admin, stores_made, "sub_store").json()["store"]["id"]
    body = {"store_id": store_id, "helix_medication_id": med, "par_units": 40,
            "reorder_level_units": 15, "max_units": 80}
    resp = client.put(f"{BASE}/par-levels", headers=pharmacist["hdr"], json=body)
    assert resp.status_code == 200, resp.text
    first = resp.json()["par_level"]
    resp = client.put(f"{BASE}/par-levels", headers=pharmacist["hdr"],
                      json=body | {"par_units": 50})
    assert resp.json()["par_level"]["id"] == first["id"]
    assert resp.json()["par_level"]["par_units"] == 50
    listed = client.get(f"{BASE}/par-levels", headers=pharmacist["hdr"],
                        params={"store_id": store_id}).json()["par_levels"]
    assert [p["par_units"] for p in listed] == [50]
    assert len(_audits("pharmacy_par_level_upserted", first["id"])) == 2
    bad = client.put(f"{BASE}/par-levels", headers=pharmacist["hdr"],
                     json=body | {"max_units": 10})
    assert bad.status_code == 422
    with db_session() as db:
        stores_store = db.execute(
            "INSERT INTO stores (code, name) VALUES (?, 'Linen')",
            (f"PMS{uuid.uuid4().hex[:5]}",)).lastrowid
    try:
        resp = client.put(f"{BASE}/par-levels", headers=pharmacist["hdr"],
                          json=body | {"store_id": stores_store})
        assert resp.status_code == 404, "a stores store is not a pharmacy store"
    finally:
        with db_session() as db:
            db.execute("DELETE FROM stores WHERE id = ?", (stores_store,))


# ═════════════════════════════════════════════════════════════════════════════
#  Vendors
# ═════════════════════════════════════════════════════════════════════════════

def test_vendors_and_licences_work_with_procurement_switched_off(monkeypatch, pharmacist):
    monkeypatch.setattr(config, "PROCUREMENT_ENABLED", False)
    code = f"PMV{uuid.uuid4().hex[:5].upper()}"
    soon = (date.fromisoformat(stock.site_today()) + timedelta(days=10)).isoformat()
    resp = client.post(f"{BASE}/vendors", headers=pharmacist["hdr"], json={
        "code": code, "name": "Lifeline Distributors", "vendor_type": "distributor",
        "gstin": "29ABCDE1234F1Z5", "drug_licence_numbers": "KA-B1-12345, KA-B2-12346",
        "drug_licence_valid_to": soon})
    assert resp.status_code == 200, resp.text
    vendor = resp.json()["vendor"]
    assert vendor["licence_status"] == "expiring"
    assert "account_last4" not in vendor
    assert _audits("pharmacy_vendor_upserted", vendor["id"])[0]["event_category"] == "security"

    again = client.post(f"{BASE}/vendors", headers=pharmacist["hdr"],
                        json={"code": code, "name": "Someone else"})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_exists"

    later = (date.fromisoformat(stock.site_today()) + timedelta(days=400)).isoformat()
    resp = client.put(f"{BASE}/vendors/{code}/drug-licence", headers=pharmacist["hdr"],
                      json={"drug_licence_valid_to": later, "ndps_licence_number": "NDPS-77"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["vendor"]["licence_status"] == "valid"
    assert resp.json()["vendor"]["ndps_licence_number"] == "NDPS-77"
    assert resp.json()["vendor"]["name"] == "Lifeline Distributors"

    resp = client.put(f"{BASE}/vendors/{code}", headers=pharmacist["hdr"],
                      json={"contact_phone": "080 5550 1234"})
    assert resp.status_code == 200 and resp.json()["vendor"]["contact_phone"] == "080 5550 1234"
    listed = client.get(f"{BASE}/vendors", headers=pharmacist["hdr"],
                        params={"search": code}).json()["vendors"]
    assert [v["code"] for v in listed] == [code]

    assert client.put(f"{BASE}/vendors/NOPE{code}", headers=pharmacist["hdr"],
                      json={"notes": "x"}).status_code == 404
    bad = client.post(f"{BASE}/vendors", headers=pharmacist["hdr"],
                      json={"code": f"X{code}", "name": "Y", "account_last4": "1234"})
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "unknown_field"


def test_licence_status_reads_the_hospital_date():
    today = stock.site_today()
    plus = lambda days: (date.fromisoformat(today) + timedelta(days=days)).isoformat()  # noqa: E731
    assert masters.licence_status(None) == "none"
    assert masters.licence_status(plus(-1), today=today) == "expired"
    assert masters.licence_status(today, today=today) == "expiring"
    assert masters.licence_status(plus(config.PHARMACY_LICENCE_WARN_DAYS + 1),
                                  today=today) == "valid"
