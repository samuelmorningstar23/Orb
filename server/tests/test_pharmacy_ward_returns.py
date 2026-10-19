"""Ward returns of dispensed medicines (grn-15, W9-D): /api/pharmacy-store/
ward-returns, behind ORB_PHARMACY_STORE.

Pinned (docs/his_design/grn.json, grn-15 tests, and the strict defaults the
service lists for Samuel):
  * with the module off every route answers 404 module_disabled;
  * a return names a DELIVERED order and never exceeds the units its dispense
    took minus the units already returned (409 over_return, nothing written);
  * the units land in a returns_quarantine store as a 'pending_inspection'
    lot of the same batch by a 'ward_return' movement, and are excluded from
    every dispensable count;
  * the return, its movement and its clinical_access audit row commit together
    or not at all (an unwritable audit row is 503 and leaves nothing behind);
  * the nurse records from her own ward only: another ward's order is 404 and
    writes a patient_scope_denied row; she cannot inspect; the administrator's
    read tier reaches nothing (every answer names a patient);
  * inspection writes off by a 'write_off' movement, never by the person who
    took the return in (403 same_person, a strict default for Samuel: grn.json
    section F's write-off separation; a return the nurse took in on the ward is
    any pharmacist's to write off); restock is refused while
    ruling GRN-RS3 is held (RETURN_RESTOCK_ALLOWED off), and with option (b)
    switched on only a sealed, in-date, non cold-chain, non-NDPS return goes
    back into its original batch, inspected by someone other than the person
    who took it in;
  * a narcotic return needs pharmacy_store.ndps and a witness's PIN, register
    on or off; with the register on it is entered there;
  * replaying a request key returns the first return; reusing it differently
    is 409;
  * the credit candidate grn-16 reads is units times the batch MRP per unit.
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
from services import audit_service
from services import pharmacy_stock_service as stock
from services import pharmacy_store_service as returns_service
from services import pin_security
from services import procurement_service as procurement
from services.audit_service import AuditWriteError
from services.helix_db import init_helix_db

client = TestClient(app)
BASE = "/api/pharmacy-store"
PIN = "7351"
DEPT = "W9D Returns Ward"
OTHER_DEPT = "W9D Other Returns Ward"


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


def _days(n: int) -> str:
    return (date.fromisoformat(stock.site_today()) + timedelta(days=n)).isoformat()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture(autouse=True)
def module_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setattr(stock, "RETURN_RESTOCK_ALLOWED", False)


def _user(role, *, department=None, pin=PIN):
    uid = f"USWR{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, pin_hash) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, hash_password("secretpass"), department,
                    pin_security.hash_pin(pin) if pin else None))
    return {"id": uid, "role": role,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


@pytest.fixture
def ph():
    return _user("pharmacist")


@pytest.fixture
def ph_b():
    return _user("pharmacist")


@pytest.fixture
def nurse():
    return _user("nurse", department=DEPT)


class _Made:
    def __init__(self):
        self.stores: list[int] = []
        self.drugs: list[str] = []
        self.patients: list[str] = []


@pytest.fixture
def made():
    m = _Made()
    yield m
    with db_session() as db:
        for pid in m.patients:
            db.execute("DELETE FROM pharmacy_ward_returns WHERE patient_id = ?", (pid,))
        for drug in m.drugs:
            db.execute("DELETE FROM ndps_register WHERE helix_medication_id = ? "
                       "AND lane = 'non_production'", (drug,))
            db.execute("DELETE FROM ndps_variances WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_stock_movements WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_lot_events WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_inventory_lots WHERE medication_id = ?", (drug,))
        for pid in m.patients:
            db.execute("DELETE FROM helix_patient_med_orders WHERE patient_id = ?", (pid,))
        for store_id in m.stores:
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def _drug(m, **fields) -> str:
    med = f"MEDWR{_uid()}"
    cols = {"id": med, "name": f"Returnable {med[-6:]} 5 mg", "generic_name": "returnamol",
            "form": "tablet", "strength": "5 mg", "drug_schedule": "H",
            "ndps_category": "none", "pack_size_units": 10,
            "storage_condition": "room_temperature"} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    m.drugs.append(med)
    return med


def _store(m, kind="returns_quarantine") -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(db, code=f"WR{_uid()}", name=f"Ret {kind} {_uid()}",
                                             domain="pharmacy", pharmacy_kind=kind)
    m.stores.append(row["id"])
    return dict(row)


def _dispensing_store(m) -> int:
    with db_session() as db:
        row = db.execute("SELECT id FROM stores WHERE pharmacy_kind = 'dispensing' "
                         "AND is_active = 1 ORDER BY id LIMIT 1").fetchone()
    if row is not None:
        return int(row["id"])
    return int(_store(m, "dispensing")["id"])


def _lot(drug, store_id, units, *, expiry=None, mrp=1000, per_pack=10):
    with db_session(immediate=True) as db:
        return stock.open_or_merge_lot(
            db, medication_id=drug, lot_number=f"WB{_uid()}",
            expiration_date=expiry or _days(300), units=units, actor_id="tester",
            store_id=store_id, unit_cost_paise=80, mrp_pack_paise=mrp,
            units_per_pack=per_pack, movement_type="receipt")["lot"]


_ADMITTER: list[str] = []


def _admitter() -> str:
    if not _ADMITTER:
        _ADMITTER.append(_user("doctor", department=DEPT)["id"])
    return _ADMITTER[0]


# An invented name: a name holding an ordinary word ("Patient", "Admitted") is read by
# Sage's name matcher in later test files, so "the patient" there would match it.
def _patient(m, department=DEPT) -> str:
    pid = f"PATWR{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Retunna {pid[-4:]}", f"MRN-{pid}"))
        db.execute("INSERT INTO admissions (patient_id, department, status, admitted_by) "
                   "VALUES (?, ?, 'active', ?)", (pid, department, _admitter()))
    m.patients.append(pid)
    return pid


def _order(m, drug, lot_id, units, *, department=DEPT, status="delivered", receiver=None):
    """A Helix order whose dispense took `units` from `lot_id`, then `status`."""
    pid = _patient(m, department)
    oid = f"ORDWR{uuid.uuid4().hex[:8].upper()}"
    with db_session(immediate=True) as db:
        db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, dosage, "
                   "route, frequency, status, received_by, received_at) "
                   "VALUES (?, ?, ?, '5 mg', 'PO', 'OD', ?, ?, CURRENT_TIMESTAMP)",
                   (oid, pid, drug, status, receiver))
        stock.post_movement(db, lot_id=lot_id, movement_type="dispense", quantity_change=-units,
                            actor_id="tester", order_id=oid, reference_type="helix_order",
                            reference_id=oid, idempotency_key=f"helix_order:{oid}:dispense")
    return oid, pid


def _qty(lot_id) -> int:
    with db_session() as db:
        return int(db.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                              (lot_id,)).fetchone()[0])


def _err(resp):
    body = resp.json().get("detail")
    return body.get("error") if isinstance(body, dict) else body


def _record(user, oid, units, store, **extra):
    body = {"helix_order_id": oid, "quantity_units": units, "store_code": store["code"]} | extra
    return client.post(f"{BASE}/ward-returns", headers=user["hdr"], json=body)


def _recorded(user, oid, units, store, **extra) -> dict:
    resp = _record(user, oid, units, store, **extra)
    assert resp.status_code == 200, resp.text
    return resp.json()["ward_return"]


def _inspect(user, rid, decision, reason="Opened strip, cannot be reissued", **extra):
    return client.post(f"{BASE}/ward-returns/{rid}/inspect", headers=user["hdr"],
                       json={"decision": decision, "reason": reason} | extra)


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(target_id))).fetchall()]


def _returns_for(oid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM pharmacy_ward_returns WHERE helix_order_id = ?", (oid,)).fetchall()]


def _movements(rid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM helix_stock_movements WHERE reference_type = 'pharmacy_ward_return' "
            "AND reference_id = ? ORDER BY seq", (str(rid),)).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
#  The switch
# ═════════════════════════════════════════════════════════════════════════════

def test_module_off_answers_404_on_every_route(monkeypatch, ph):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    for method, path in (("get", "/ward-returns"), ("get", "/ward-returns/options"),
                         ("get", "/ward-returns/returnable"), ("get", "/ward-returns/1"),
                         ("post", "/ward-returns"), ("post", "/ward-returns/1/inspect")):
        resp = getattr(client, method)(f"{BASE}{path}", headers=ph["hdr"],
                                       **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 404 and _err(resp) == "module_disabled", (path, resp.text)


# ═════════════════════════════════════════════════════════════════════════════
#  Taking a return in
# ═════════════════════════════════════════════════════════════════════════════

def test_the_counter_takes_units_into_returns_quarantine(ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, pid = _order(made, drug, lot["id"], 3)
    before = _qty(lot["id"])

    ret = _recorded(ph, oid, 2, quarantine, seal_intact=True, note="Course stopped")
    assert ret["status"] == "pending_inspection" and ret["status_label"] == "Waiting for inspection"
    assert ret["patient_id"] == pid and ret["helix_order_id"] == oid
    assert ret["original_lot_id"] == lot["id"] and ret["quantity_units"] == 2
    assert ret["seal_intact"] is True and ret["store_code"] == quarantine["code"]
    assert ret["return_number"] and ret["return_number"].startswith("PHRET")
    assert ret["returned_by"] is None and ret["received_by"] == ph["id"], \
        "the counter lane names who took it in, not who brought it"
    assert _qty(lot["id"]) == before, "the original batch is untouched until inspection"

    with db_session() as db:
        qlot = dict(db.execute("SELECT * FROM helix_inventory_lots WHERE id = ?",
                               (ret["quarantine_lot_id"],)).fetchone())
    assert qlot["store_id"] == quarantine["id"] and qlot["status"] == "pending_inspection"
    assert (qlot["lot_number"], qlot["expiration_date"], qlot["quantity"]) == (
        lot["lot_number"], lot["expiration_date"], 2)
    [move] = _movements(ret["id"])
    assert (move["movement_type"], move["quantity_change"], move["lot_id"]) == (
        "ward_return", 2, qlot["id"])

    [row] = _audits("pharmacy_ward_return_recorded", ret["id"])
    meta = json.loads(row["metadata_json"])
    assert row["event_category"] == "clinical_access" and row["patient_id"] == pid
    assert meta["helix_order_id"] == oid and meta["quantity_units"] == 2
    assert meta["lane"] == "pharmacy" and meta["movement_id"] == move["id"]


def test_returned_units_are_never_dispensable(ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 4)
    oid, _ = _order(made, drug, lot["id"], 4)
    with db_session() as db:
        assert stock.dispensable_units(db, drug) == 0
    ret = _recorded(ph, oid, 3, quarantine)
    with db_session() as db:
        assert stock.dispensable_units(db, drug) == 0, "a return never adds dispensable stock"
        assert stock.pick_fefo_lot(db, drug, 1) is None
        assert stock.dispensable_lots(db, drug) == []
        with pytest.raises(stock.StockError) as caught:
            stock.choose_dispense_lot(db, drug, 1, lot_id=ret["quarantine_lot_id"])
        assert caught.value.code == "lot_not_dispensable"


def test_over_return_is_refused_and_writes_nothing(ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, _ = _order(made, drug, lot["id"], 3)
    resp = _record(ph, oid, 4, quarantine)
    assert resp.status_code == 409 and _err(resp) == "over_return"
    assert resp.json()["detail"]["returnable_units"] == 3
    _recorded(ph, oid, 2, quarantine)
    resp = _record(ph, oid, 2, quarantine)
    assert resp.status_code == 409 and _err(resp) == "over_return", \
        "the units already returned count against the order"
    assert resp.json()["detail"]["returned_units"] == 2
    assert len(_returns_for(oid)) == 1
    assert _record(ph, oid, 1, quarantine).status_code == 200


def test_only_a_delivered_order_comes_back(ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 5)
    oid, _ = _order(made, drug, lot["id"], 1, status="dispensed")
    resp = _record(ph, oid, 1, quarantine)
    assert resp.status_code == 409 and _err(resp) == "order_not_delivered"
    resp = _record(ph, "ORD-NO-SUCH-ORDER", 1, quarantine)
    assert resp.status_code == 404 and _err(resp) == "not_found"
    for bad in (0, -2, "2", 1.5, True):
        resp = _record(ph, oid, bad, quarantine)
        assert resp.status_code in (409, 422), (bad, resp.text)
    assert _returns_for(oid) == []


def test_a_site_without_a_returns_store_is_told_so(ph, made, monkeypatch):
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 5)
    oid, _ = _order(made, drug, lot["id"], 2)
    monkeypatch.setattr(returns_service, "_returns_stores", lambda conn: [])
    resp = client.post(f"{BASE}/ward-returns", headers=ph["hdr"],
                       json={"helix_order_id": oid, "quantity_units": 1})
    assert resp.status_code == 409 and _err(resp) == "no_returns_store"
    assert _returns_for(oid) == []


def test_an_unwritable_audit_row_rolls_the_return_back(ph, made, monkeypatch):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 6)
    oid, _ = _order(made, drug, lot["id"], 2)
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "pharmacy_ward_return_recorded":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    resp = _record(ph, oid, 2, quarantine)
    assert resp.status_code == 503 and _err(resp) == "audit_unavailable"
    assert _returns_for(oid) == []
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM helix_inventory_lots WHERE store_id = ?",
                          (quarantine["id"],)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM helix_stock_movements WHERE medication_id = ? "
                          "AND movement_type = 'ward_return'", (drug,)).fetchone()[0] == 0


def test_a_request_key_replays_once_and_is_never_reused(ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 8)
    oid, _ = _order(made, drug, lot["id"], 4)
    key = f"wr-{uuid.uuid4().hex}"
    first = client.post(f"{BASE}/ward-returns", headers={**ph["hdr"], "Idempotency-Key": key},
                        json={"helix_order_id": oid, "quantity_units": 2,
                              "store_code": quarantine["code"]})
    again = client.post(f"{BASE}/ward-returns", headers={**ph["hdr"], "Idempotency-Key": key},
                        json={"helix_order_id": oid, "quantity_units": 2,
                              "store_code": quarantine["code"]})
    assert first.status_code == again.status_code == 200
    assert again.json()["replayed"] is True
    assert first.json()["ward_return"]["id"] == again.json()["ward_return"]["id"]
    assert len(_returns_for(oid)) == 1 and len(_movements(first.json()["ward_return"]["id"])) == 1
    other = client.post(f"{BASE}/ward-returns", headers={**ph["hdr"], "Idempotency-Key": key},
                        json={"helix_order_id": oid, "quantity_units": 1,
                              "store_code": quarantine["code"]})
    assert other.status_code == 409 and _err(other) == "idempotency_conflict"


def test_the_credit_candidate_is_units_at_the_batch_mrp(ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10, mrp=1005, per_pack=10)
    oid, pid = _order(made, drug, lot["id"], 5)
    ret = _recorded(ph, oid, 3, quarantine)
    assert ret["credit"] == {"units": 3, "unit_mrp_paise": 101, "amount_paise": 302,
                             "basis": "batch_mrp", "helix_order_id": oid, "patient_id": pid}
    assert returns_service.credit_candidate({"quantity_units": 2}, {"mrp_pack_paise": None,
                                                                    "units_per_pack": 10}
                                            )["amount_paise"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  The ward lane and who may do what
# ═════════════════════════════════════════════════════════════════════════════

def test_the_nurse_records_for_her_own_ward_only(nurse, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    own, _ = _order(made, drug, lot["id"], 2, department=DEPT, receiver=nurse["id"])
    other, other_pid = _order(made, drug, lot["id"], 2, department=OTHER_DEPT)

    ret = _recorded(nurse, own, 1, quarantine, seal_intact=False)
    assert ret["returned_by"] == nurse["id"] and ret["received_by"] == nurse["id"]
    assert ret["seal_intact"] is False
    meta = json.loads(_audits("pharmacy_ward_return_recorded", ret["id"])[0]["metadata_json"])
    assert meta["lane"] == "ward"

    resp = _record(nurse, other, 1, quarantine)
    assert resp.status_code == 404 and _err(resp) == "not_found", \
        "another ward's order answers exactly like a missing one"
    assert _returns_for(other) == []
    with db_session() as db:
        denied = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'patient_scope_denied' "
            "AND target_id = ? AND actor_user_id = ?", (other, nurse["id"])).fetchall()
    assert len(denied) == 1 and denied[0]["patient_id"] == other_pid

    rows = client.get(f"{BASE}/ward-returns", headers=nurse["hdr"]).json()["ward_returns"]
    assert {r["helix_order_id"] for r in rows} <= {own} | {
        r["helix_order_id"] for r in rows if r.get("department") == DEPT}
    assert other not in {r["helix_order_id"] for r in rows}

    listed = client.get(f"{BASE}/ward-returns/returnable", headers=nurse["hdr"]).json()["orders"]
    ids = {o["id"] for o in listed}
    assert own in ids and other not in ids
    [mine] = [o for o in listed if o["id"] == own]
    assert mine["batches"][0]["returnable_units"] == 1


def test_the_nurse_cannot_inspect_and_the_read_tier_reaches_nothing(nurse, ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, _ = _order(made, drug, lot["id"], 2)
    ret = _recorded(ph, oid, 1, quarantine)
    resp = _inspect(nurse, ret["id"], "write_off")
    assert resp.status_code == 403
    admin = _user("admin")
    for path in ("/ward-returns", "/ward-returns/options", "/ward-returns/returnable",
                 f"/ward-returns/{ret['id']}"):
        assert client.get(f"{BASE}{path}", headers=admin["hdr"]).status_code == 403, path
    assert _record(admin, oid, 1, quarantine).status_code == 403


def test_every_ward_return_get_answers_cleanly_for_every_role():
    paths = ("/ward-returns", "/ward-returns?status=written_off", "/ward-returns/options",
             "/ward-returns/returnable", "/ward-returns/1", "/ward-returns/999999999")
    for role in ("admin", "doctor", "nurse", "pharmacist", "ward_clerk", "cashier",
                 "receptionist", "lab_technician"):
        who = _user(role, department=DEPT if role in ("doctor", "nurse", "ward_clerk") else None)
        for path in paths:
            resp = client.get(f"{BASE}{path}", headers=who["hdr"])
            assert resp.status_code < 500, (role, path, resp.text)
            if role not in ("nurse", "pharmacist"):
                assert resp.status_code == 403, (role, path, resp.status_code)


# ═════════════════════════════════════════════════════════════════════════════
#  Inspection
# ═════════════════════════════════════════════════════════════════════════════

def test_write_off_takes_the_units_out_of_quarantine(ph, ph_b, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, pid = _order(made, drug, lot["id"], 3)
    ret = _recorded(ph, oid, 3, quarantine)
    # ph took it in at the counter; a second pharmacist writes it off.
    resp = _inspect(ph_b, ret["id"], "write_off")
    assert resp.status_code == 200, resp.text
    done = resp.json()["ward_return"]
    assert done["status"] == "written_off" and done["inspected_by"] == ph_b["id"]
    assert done["quarantine_units"] == 0
    with db_session() as db:
        assert db.execute("SELECT status FROM helix_inventory_lots WHERE id = ?",
                          (ret["quarantine_lot_id"],)).fetchone()[0] == "pending_inspection", \
            "the quarantine lot stays open so the next return of this batch can land"
    kinds = [(m["movement_type"], m["quantity_change"]) for m in _movements(ret["id"])]
    assert kinds == [("ward_return", 3), ("write_off", -3)]
    [row] = _audits("pharmacy_ward_return_inspected", ret["id"])
    assert row["event_category"] == "clinical_access" and row["patient_id"] == pid
    assert json.loads(row["metadata_json"])["decision"] == "write_off"

    again = _inspect(ph_b, ret["id"], "write_off")
    assert again.status_code == 409 and _err(again) == "already_inspected"
    again = _inspect(ph, ret["id"], "write_off")
    assert again.status_code == 409 and _err(again) == "already_inspected"
    resp = _inspect(ph_b, ret["id"], "write_off", reason="  ")
    assert resp.status_code in (409, 422)
    # A second return of the same batch lands in the same open quarantine lot.
    oid2, _ = _order(made, drug, lot["id"], 2)
    second = _recorded(ph, oid2, 1, quarantine)
    assert second["quarantine_lot_id"] == ret["quarantine_lot_id"]


def test_the_person_who_took_a_return_in_cannot_write_it_off(ph, ph_b, made, monkeypatch):
    """Strict default for Samuel (grn.json section F, grn-10): the write-off
    approver differs from the requester, so a pharmacist cannot take a return
    in at the counter and write it off alone."""
    assert returns_service.RETURN_WRITE_OFF_NEEDS_SECOND_PERSON is True
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, _ = _order(made, drug, lot["id"], 3)
    ret = _recorded(ph, oid, 2, quarantine)

    opts = client.get(f"{BASE}/ward-returns/options", headers=ph["hdr"]).json()
    assert opts["write_off_needs_second_person"] is True, "the screen hides Write off from the receiver"
    resp = _inspect(ph, ret["id"], "write_off", reason="Strip damaged")
    assert resp.status_code == 403 and _err(resp) == "same_person"
    assert resp.json()["detail"]["message"].startswith(
        "The person who took this return in cannot also write it off.")
    kinds = [(m["movement_type"], m["quantity_change"]) for m in _movements(ret["id"])]
    assert kinds == [("ward_return", 2)], "nothing left the books"
    assert _audits("pharmacy_ward_return_inspected", ret["id"]) == []
    [row] = _returns_for(oid)
    assert row["status"] == "pending_inspection" and row["inspected_by"] is None

    # A second pharmacist can.
    resp = _inspect(ph_b, ret["id"], "write_off", reason="Strip damaged")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ward_return"]["inspected_by"] == ph_b["id"]

    # Relaxed only if Samuel rules it so: the receiver then writes off alone.
    monkeypatch.setattr(returns_service, "RETURN_WRITE_OFF_NEEDS_SECOND_PERSON", False)
    opts = client.get(f"{BASE}/ward-returns/options", headers=ph["hdr"]).json()
    assert opts["write_off_needs_second_person"] is False, "so the screen offers it too"
    oid2, _ = _order(made, drug, lot["id"], 2)
    ret2 = _recorded(ph, oid2, 1, quarantine)
    resp = _inspect(ph, ret2["id"], "write_off")
    assert resp.status_code == 200, resp.text


def test_a_controlled_write_off_by_the_receiver_is_refused_before_the_witness_pin(
        ph, ph_b, made, monkeypatch):
    quarantine = _store(made)
    drug = _drug(made, ndps_category="psychotropic")
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, _ = _order(made, drug, lot["id"], 2)
    ret = _recorded(ph, oid, 1, quarantine, witness_id=ph_b["id"], witness_pin=PIN)
    called: list = []
    real = returns_service._witness
    monkeypatch.setattr(returns_service, "_witness",
                        lambda *a, **k: called.append(k) or real(*a, **k))
    resp = _inspect(ph, ret["id"], "write_off", witness_id=ph_b["id"], witness_pin=PIN)
    assert resp.status_code == 403 and _err(resp) == "same_person"
    assert called == [], "the witness is never asked for a PIN on a refused write-off"
    assert [m["movement_type"] for m in _movements(ret["id"])] == ["ward_return"]


def test_a_return_the_nurse_took_in_is_any_pharmacists_to_write_off(nurse, ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, _ = _order(made, drug, lot["id"], 2, department=DEPT, receiver=nurse["id"])
    ret = _recorded(nurse, oid, 1, quarantine)
    assert ret["received_by"] == nurse["id"]
    resp = _inspect(ph, ret["id"], "write_off", reason="Opened on the ward")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ward_return"]["status"] == "written_off"


def test_a_write_off_needs_a_reason(ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, _ = _order(made, drug, lot["id"], 2)
    ret = _recorded(ph, oid, 1, quarantine)
    resp = _inspect(ph, ret["id"], "write_off", reason="")
    assert resp.status_code == 422 and _err(resp) == "missing_field"
    resp = _inspect(ph, ret["id"], "throw_away")
    assert resp.status_code == 422 and _err(resp) == "bad_value"


def test_restock_is_refused_while_the_ruling_is_held(ph, ph_b, made):
    assert stock.RETURN_RESTOCK_ALLOWED is False
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, _ = _order(made, drug, lot["id"], 3)
    ret = _recorded(ph, oid, 2, quarantine, seal_intact=True)
    assert ret["restock"]["allowed"] is False and ret["restock"]["code"] == "restock_not_allowed"
    original = _qty(lot["id"])
    resp = _inspect(ph_b, ret["id"], "restock", reason="Sealed strip", seal_intact=True)
    assert resp.status_code == 409 and _err(resp) == "restock_not_allowed"
    assert _qty(lot["id"]) == original
    assert [m["movement_type"] for m in _movements(ret["id"])] == ["ward_return"]
    assert _returns_for(oid)[0]["status"] == "pending_inspection"
    opts = client.get(f"{BASE}/ward-returns/options", headers=ph["hdr"]).json()
    assert opts["restock_allowed"] is False


def test_restock_under_option_b_goes_back_into_the_original_batch(ph, ph_b, made,
                                                                    monkeypatch):
    monkeypatch.setattr(stock, "RETURN_RESTOCK_ALLOWED", True)
    quarantine = _store(made)
    shelf = _dispensing_store(made)
    drug = _drug(made)
    lot = _lot(drug, shelf, 10)
    oid, _ = _order(made, drug, lot["id"], 4)
    ret = _recorded(ph, oid, 3, quarantine, seal_intact=True)
    assert ret["restock"]["allowed"] is True

    resp = _inspect(ph, ret["id"], "restock", reason="Sealed", seal_intact=True)
    assert resp.status_code == 403 and _err(resp) == "same_person", \
        "the person who took it in does not also restock it"
    resp = _inspect(ph_b, ret["id"], "restock", reason="Seal broken?", seal_intact=False)
    assert resp.status_code == 422 and _err(resp) == "restock_refused"

    with db_session() as db:
        before = stock.dispensable_units(db, drug)
    resp = _inspect(ph_b, ret["id"], "restock", reason="Sealed strip, in date",
                    seal_intact=True)
    assert resp.status_code == 200, resp.text
    done = resp.json()["ward_return"]
    assert done["status"] == "restocked" and done["quarantine_units"] == 0
    with db_session() as db:
        assert stock.dispensable_units(db, drug) == before + 3
    kinds = [(m["movement_type"], m["quantity_change"], m["lot_id"])
             for m in _movements(ret["id"])]
    assert kinds == [("ward_return", 3, ret["quarantine_lot_id"]),
                     ("transfer_out", -3, ret["quarantine_lot_id"]),
                     ("transfer_in", 3, lot["id"])]


@pytest.mark.parametrize("hazard,code_reason", [
    ("cold_chain", "refrigerated"),
    ("ndps", "narcotic"),
    ("expired", "expired"),
    ("recalled", "recalled"),
])
def test_restock_option_b_refuses_each_hazard(hazard, code_reason, ph, ph_b, made,
                                               monkeypatch):
    monkeypatch.setattr(stock, "RETURN_RESTOCK_ALLOWED", True)
    quarantine = _store(made)
    drug = _drug(made, **({"storage_condition": "refrigerated"} if hazard == "cold_chain"
                          else {}))
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, _ = _order(made, drug, lot["id"], 2)
    ret = _recorded(ph, oid, 1, quarantine, seal_intact=True)
    with db_session(immediate=True) as db:
        if hazard == "ndps":
            db.execute("UPDATE helix_medication_master SET ndps_category = 'narcotic' "
                       "WHERE id = ?", (drug,))
        elif hazard == "expired":
            db.execute("UPDATE helix_inventory_lots SET expiration_date = ? WHERE id = ?",
                       (_days(-1), lot["id"]))
        elif hazard == "recalled":
            stock.set_lot_status(db, lot["id"], "recalled", actor_id="tester",
                                 reason="Manufacturer recall")
    resp = _inspect(ph_b, ret["id"], "restock", reason="Inspected", seal_intact=True)
    assert resp.status_code in (409, 422), resp.text
    assert _err(resp) == "restock_refused" and code_reason in resp.json()["detail"]["message"]
    assert _returns_for(oid)[0]["status"] == "pending_inspection"


# ═════════════════════════════════════════════════════════════════════════════
#  Controlled drugs
# ═════════════════════════════════════════════════════════════════════════════

def test_a_narcotic_return_needs_the_ndps_permission_and_a_witness(nurse, ph, ph_b, made,
                                                                    monkeypatch):
    quarantine = _store(made)
    drug = _drug(made, ndps_category="narcotic")
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, pid = _order(made, drug, lot["id"], 3, receiver=nurse["id"])

    resp = _record(nurse, oid, 1, quarantine)
    assert resp.status_code == 403 and _err(resp) == "ndps_permission_needed"
    resp = _record(ph, oid, 1, quarantine)
    assert resp.status_code == 422 and _err(resp) == "witness_required"
    resp = _record(ph, oid, 1, quarantine, witness_id=ph["id"], witness_pin=PIN)
    assert resp.status_code == 403 and _err(resp) == "witness_same_person"
    resp = _record(ph, oid, 1, quarantine, witness_id=ph_b["id"], witness_pin="0000")
    assert resp.status_code == 403 and _err(resp) == "witness_pin_wrong"
    assert _returns_for(oid) == []

    monkeypatch.setattr(config, "PHARMACY_NDPS_REGISTER_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_NDPS_ON_FAILURE", "block")
    ret = _recorded(ph, oid, 2, quarantine, witness_id=ph_b["id"], witness_pin=PIN)
    assert ret["controlled"] is True
    with db_session() as db:
        entry = dict(db.execute(
            "SELECT * FROM ndps_register WHERE helix_medication_id = ? AND store_id = ? "
            "ORDER BY seq DESC LIMIT 1", (drug, quarantine["id"])).fetchone())
    assert (entry["entry_type"], entry["quantity_units"], entry["balance_after_units"]) == (
        "ward_return", 2, 2)
    assert entry["witness_id"] == ph_b["id"]
    assert (entry["counterparty_type"], entry["counterparty_ref"]) == ("patient", pid)
    assert entry["document_ref"] == f"pharmacy_ward_return:{ret['id']}"

    # ph took it in, so a third pharmacist writes it off, with ph_b witnessing.
    resp = _inspect(ph, ret["id"], "write_off", witness_id=ph_b["id"], witness_pin=PIN)
    assert resp.status_code == 403 and _err(resp) == "same_person"
    ph_c = _user("pharmacist")
    resp = _inspect(ph_c, ret["id"], "write_off")
    assert resp.status_code == 422 and _err(resp) == "witness_required"
    resp = _inspect(ph_c, ret["id"], "write_off", witness_id=ph_b["id"], witness_pin=PIN)
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        entry = dict(db.execute(
            "SELECT * FROM ndps_register WHERE helix_medication_id = ? AND store_id = ? "
            "ORDER BY seq DESC LIMIT 1", (drug, quarantine["id"])).fetchone())
    assert (entry["entry_type"], entry["quantity_units"], entry["balance_after_units"]) == (
        "write_off", -2, 0)


def test_another_wards_return_is_404_and_recorded(nurse, ph, made):
    quarantine = _store(made)
    drug = _drug(made)
    lot = _lot(drug, _dispensing_store(made), 10)
    oid, pid = _order(made, drug, lot["id"], 2, department=OTHER_DEPT)
    ret = _recorded(ph, oid, 1, quarantine)
    resp = client.get(f"{BASE}/ward-returns/{ret['id']}", headers=nurse["hdr"])
    assert resp.status_code == 404 and _err(resp) == "not_found"
    with db_session() as db:
        denied = db.execute(
            "SELECT patient_id FROM audit_logs WHERE action_type = 'patient_scope_denied' "
            "AND actor_user_id = ? AND target_id = ?", (nurse["id"], oid)).fetchall()
    assert [r["patient_id"] for r in denied] == [pid]
    seen = client.get(f"{BASE}/ward-returns/{ret['id']}", headers=ph["hdr"])
    assert seen.status_code == 200 and seen.json()["ward_return"]["id"] == ret["id"]
    with db_session() as db:
        read = db.execute(
            "SELECT event_category FROM audit_logs WHERE action_type = 'pharmacy_ward_return_viewed' "
            "AND actor_user_id = ? ORDER BY id DESC LIMIT 1", (ph["id"],)).fetchone()
    assert read is not None and read["event_category"] == "phi_access"
