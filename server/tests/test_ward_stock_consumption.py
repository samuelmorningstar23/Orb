"""Ward stock used on an eMAR dose (grn-17, W10-C), behind ORB_PHARMACY_STORE
and ORB_PHARMACY_WARD_STOCK.

Pinned (docs/his_design/grn.json, grn-17 tests, and the strict readings the
service notes for Samuel to confirm):
  * after a dose commits as 'given', an order given from ward stock (its
    supply source) and bound to a drug in the master takes one unit off the
    patient's ward imprest store, FEFO among available in-date batches, in a
    transaction of its own, with a fail-closed clinical_access audit row;
  * the dose is never blocked: a stock write that fails (fault injection), an
    audit outage on the booking, or a route call all leave the dose charted
    and audited as before, and nothing is booked;
  * nothing is booked with either switch off, for a 'not given' record, an
    unlinked ward row, an order with no drug-master binding, an order supplied
    by pharmacy or the patient's own medicines, a drug outside the ward's
    stock, another department's store, a crash cart, or two stores the
    patient's ward cannot tell apart;
  * an idempotent replay of the dose books it once;
  * a dose given when the books hold no in-date unit is booked below zero on
    the ward's own batch, shown on the ward stock overview and by reconcile(),
    never refused; held and expired units are never taken; only that one
    movement may take a lot below zero;
  * the recall trace reader attributes consumed units to patients, and marks a
    use booked below zero as not certain for the batch.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth import create_token, hash_password
from database import db_session
from routes.emar_routes import router as emar_router
from services import audit_service, emar_service
from services import pharmacy_stock_service as stock
from services import pharmacy_transfer_service as transfers
from services import procurement_service as procurement
from services.audit_service import AuditWriteError
from services.helix_db import init_helix_db
from services.medorders import core

DEPT = "W10C Kestrel Ward"
OTHER_DEPT = "W10C Osprey Ward"
WARD = "W10C-1"
PATIENT_NAMES = ("Orsolya Quandt", "Ilkka Vrabec", "Dagny Ostrowe", "Teodor Lindqvar",
                 "Marit Zelenko", "Oskari Brandvik")


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


def _today() -> date:
    return date.fromisoformat(stock.site_today())


def _days(n: int) -> str:
    return (_today() + timedelta(days=n)).isoformat()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture(autouse=True)
def switches_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", True)


class _World:
    def __init__(self):
        self.stores: list[int] = []
        self.drugs: list[str] = []
        self.patients: list[str] = []
        self.users: list[str] = []
        self.names = iter(PATIENT_NAMES * 4)


@pytest.fixture
def world():
    """Everything a test makes, taken away afterwards: its drugs' ledger,
    lots and par rows, its stores, its orders, and its patients sent home."""
    w = _World()
    w.nurse = _user(w, "nurse", DEPT)
    w.doctor = _user(w, "doctor", DEPT)
    yield w
    with db_session() as db:
        for drug in w.drugs:
            db.execute("DELETE FROM helix_stock_movements WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_lot_events WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_inventory_lots WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_par_levels WHERE helix_medication_id = ?", (drug,))
        for store_id in w.stores:
            db.execute("DELETE FROM pharmacy_par_levels WHERE store_id = ?", (store_id,))
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))
        if w.patients:
            marks = ", ".join("?" for _ in w.patients)
            db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", w.patients)
            db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", w.patients)
            db.execute(f"UPDATE medications SET med_order_id = NULL, status = 'discontinued' "
                       f"WHERE patient_id IN ({marks})", w.patients)
            db.execute(f"UPDATE admissions SET status = 'discharged' "
                       f"WHERE patient_id IN ({marks})", w.patients)
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})",
                       w.patients)
        for drug in w.drugs:
            db.execute("DELETE FROM helix_medication_master WHERE id = ?", (drug,))


def _user(w: _World, role: str, dept=None) -> str:
    uid = f"W10C{role[:3].upper()}{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role.title()} {uid}", role, dept, hash_password("secretpass")))
    w.users.append(uid)
    return uid


def _patient(w: _World, *, dept=DEPT, ward=WARD, admitted=True) -> str:
    pid = f"PW10C{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'admitted', ?, '7')",
                   (pid, next(w.names), f"MRN-W10C-{uuid.uuid4().hex[:8]}", ward))
        if admitted:
            db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, "
                       "bed, status) VALUES (?, ?, ?, ?, ?, '7', 'active')",
                       (f"ADM-W10C-{uuid.uuid4().hex[:8]}", pid, w.doctor, dept, ward))
    w.patients.append(pid)
    return pid


def _drug(w: _World) -> str:
    med = f"MEDW10C{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength, "
                   "drug_schedule, ndps_category, pack_size_units) "
                   "VALUES (?, ?, 'quorvexin', 'tablet', '5 mg', 'H', 'none', 10)",
                   (med, f"Quorvexin {med[-6:]} 5 mg"))
    w.drugs.append(med)
    return med


def _store(w: _World, *, kind="ward_imprest", dept=DEPT, ward=WARD) -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(
            db, code=f"WC{_uid()}", name=f"{kind} {dept or 'pharmacy'} {_uid()}",
            store_type="ward" if dept else "central",
            department=dept, ward=ward, domain="pharmacy", pharmacy_kind=kind)
    w.stores.append(row["id"])
    return dict(row)


def _par(store_id: int, drug: str, par_units: int = 20) -> None:
    with db_session() as db:
        db.execute("INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units, "
                   "updated_by) VALUES (?, ?, ?, 'tester')", (store_id, drug, par_units))


def _lot(drug: str, store_id: int, units: int, expiry: str, *, status="available",
         batch=None) -> dict:
    with db_session(immediate=True) as db:
        out = stock.open_or_merge_lot(db, medication_id=drug, lot_number=batch or f"B{_uid()}",
                                      expiration_date=expiry, units=units, actor_id="tester",
                                      store_id=store_id, status=status, units_per_pack=10)
    return out["lot"]


def _order(w: _World, pid: str, drug: str | None, *, supply_source="ward_stock") -> dict:
    """A live order bound to the drug master, given from ward stock, with its
    ward row, linked both ways as the store's lanes write them."""
    with db_session(immediate=True) as conn:
        write = core.create_in_txn(
            conn, patient_id=pid, drug_name="Quorvexin 5 mg", source="chart",
            status="prescribed", entered_by=w.doctor, entered_by_role="doctor",
            actor_id=w.doctor, actor_role="doctor", entry_point="test.ward_consumption",
            formulary_id=drug, dose="5 mg", route="Oral", frequency="OD",
            prescriber_id=w.doctor, supply_source=supply_source)
        med_id = conn.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, 'Quorvexin 5 mg', '5 mg', 'Oral', 'OD', "
            "'active', ?, 'clinician')", (pid, w.doctor)).lastrowid
        core.link_projection_in_txn(conn, write.order["id"], med_id)
    return {"order_id": write.order["id"], "med_id": med_id, "patient_id": pid}


def _unlinked_row(w: _World, pid: str) -> int:
    with db_session() as db:
        return db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, 'Quorvexin 5 mg', '5 mg', 'Oral', 'OD', "
            "'active', ?, 'clinician')", (pid, w.doctor)).lastrowid


def _give(w: _World, med_id: int, **kw) -> dict:
    return emar_service.record_administration(med_id, w.nurse, status="given", **kw)


def _consumptions(drug: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM helix_stock_movements WHERE medication_id = ? "
            "AND movement_type = 'consumption' ORDER BY seq", (drug,)).fetchall()]


def _qty(lot_id: str) -> int:
    with db_session() as db:
        return int(db.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                              (lot_id,)).fetchone()[0])


def _audits(action: str, target_id) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(target_id))).fetchall()]


def _mar(admin_id: int) -> dict | None:
    with db_session() as db:
        row = db.execute("SELECT * FROM medication_administrations WHERE id = ?",
                         (admin_id,)).fetchone()
    return dict(row) if row else None


def _stocked(w: _World, *, units=5, expiry_days=60, batch=None):
    """A drug on the ward's list with one in-date batch, and the store."""
    drug = _drug(w)
    store = _store(w)
    _par(store["id"], drug)
    lot = _lot(drug, store["id"], units, _days(expiry_days), batch=batch)
    return drug, store, lot


# ═════════════════════════════════════════════════════════════════════════════
#  The booking
# ═════════════════════════════════════════════════════════════════════════════

def test_a_dose_given_from_ward_stock_takes_one_unit_fefo(world):
    drug = _drug(world)
    store = _store(world)
    _par(store["id"], drug)
    early = _lot(drug, store["id"], 5, _days(20))
    late = _lot(drug, store["id"], 5, _days(200))
    expired = _lot(drug, store["id"], 4, _days(-3))
    held = _lot(drug, store["id"], 3, _days(10), status="quarantined")
    o = _order(world, _patient(world), drug)

    record = _give(world, o["med_id"])

    assert record["replayed"] is False and record["status"] == "given"
    assert "ward_stock" not in record          # the dose's answer keeps its shape
    [move] = _consumptions(drug)
    assert move["lot_id"] == early["id"]
    assert move["quantity_change"] == -1 and move["balance_after_units"] == 4
    assert move["store_id"] == store["id"]
    assert move["reference_type"] == "medication_administration"
    assert move["reference_id"] == str(record["id"])
    assert move["actor_id"] == world.nurse
    assert move["order_id"] is None
    assert move["idempotency_key"] == stock.consumption_key(record["id"])
    assert "not certain" not in (move["remarks"] or "")
    assert (_qty(early["id"]), _qty(late["id"]), _qty(expired["id"]), _qty(held["id"])) == (
        4, 5, 4, 3)

    [audit] = _audits("ward_stock_consumed", record["id"])
    assert audit["event_category"] == "clinical_access"
    assert audit["patient_id"] == o["patient_id"]
    assert audit["target_type"] == "medication_administration"
    meta = json.loads(audit["metadata_json"])
    assert meta["lot_id"] == early["id"] and meta["below_zero"] is False
    assert meta["med_order_id"] == o["order_id"] and meta["helix_medication_id"] == drug
    assert meta["movement_id"] == move["id"] and meta["units"] == 1
    # The movement row itself names no patient.
    assert o["patient_id"] not in json.dumps(move)

    with db_session() as db:
        assert stock.reconcile(db, lot_ids=[early["id"], late["id"]])["clean"] is True


def test_the_dose_is_charted_exactly_as_before_and_audited_once(world):
    drug, _store_row, _lot_row = _stocked(world)
    o = _order(world, _patient(world), drug)
    record = _give(world, o["med_id"])
    assert _mar(record["id"])["status"] == "given"
    assert len(_audits("medication_administered", o["med_id"])) == 1


# ═════════════════════════════════════════════════════════════════════════════
#  Never blocks the dose
# ═════════════════════════════════════════════════════════════════════════════

def test_the_dose_stands_when_the_stock_write_fails(world, monkeypatch):
    drug, _s, lot = _stocked(world)
    o = _order(world, _patient(world), drug)

    def broken(*args, **kwargs):
        raise RuntimeError("stock ledger unavailable")

    monkeypatch.setattr(stock, "post_movement", broken)
    record = _give(world, o["med_id"], idempotency_key=f"w10c-{uuid.uuid4().hex}")

    assert record["status"] == "given" and record["replayed"] is False
    assert _mar(record["id"]) is not None
    assert len(_audits("medication_administered", o["med_id"])) == 1
    assert _consumptions(drug) == [] and _qty(lot["id"]) == 5
    assert _audits("ward_stock_consumed", record["id"]) == []


def test_the_dose_stands_when_the_decision_itself_raises(world, monkeypatch):
    drug, _s, lot = _stocked(world)
    o = _order(world, _patient(world), drug)

    def broken(*args, **kwargs):
        raise stock.StockError("the books are being rebuilt", code="ledger_busy")

    monkeypatch.setattr(stock, "record_ward_consumption_in_txn", broken)
    record = _give(world, o["med_id"])
    assert _mar(record["id"])["status"] == "given"
    assert _consumptions(drug) == [] and _qty(lot["id"]) == 5


def test_an_audit_outage_rolls_the_booking_back_and_the_dose_stands(world, monkeypatch):
    drug, _s, lot = _stocked(world)
    o = _order(world, _patient(world), drug)
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "ward_stock_consumed":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    record = _give(world, o["med_id"])
    assert _mar(record["id"])["status"] == "given"
    assert len(_audits("medication_administered", o["med_id"])) == 1
    assert _consumptions(drug) == [] and _qty(lot["id"]) == 5


def test_the_route_answers_the_dose_when_the_booking_fails(world, monkeypatch):
    drug, _s, lot = _stocked(world)
    o = _order(world, _patient(world), drug)
    app = FastAPI()
    app.include_router(emar_router)
    client = TestClient(app)

    def broken(*args, **kwargs):
        raise RuntimeError("stock ledger unavailable")

    monkeypatch.setattr(stock, "post_movement", broken)
    resp = client.post(f"/api/emar/medications/{o['med_id']}/administer",
                       json={"status": "given"},
                       headers={"Authorization": f"Bearer {create_token(world.nurse, 'nurse')}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["administration"]["status"] == "given"
    assert _consumptions(drug) == [] and _qty(lot["id"]) == 5

    monkeypatch.undo()
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", True)
    resp = client.post(f"/api/emar/medications/{o['med_id']}/administer",
                       json={"status": "given"},
                       headers={"Authorization": f"Bearer {create_token(world.nurse, 'nurse')}"})
    assert resp.status_code == 200, resp.text
    assert len(_consumptions(drug)) == 1 and _qty(lot["id"]) == 4


# ═════════════════════════════════════════════════════════════════════════════
#  When nothing is booked
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("store_on, ward_on", [(True, False), (False, True), (False, False)])
def test_with_either_switch_off_nothing_is_booked_or_opened(world, monkeypatch, store_on,
                                                           ward_on):
    drug, _s, lot = _stocked(world)
    o = _order(world, _patient(world), drug)
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", store_on)
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", ward_on)
    calls = []
    monkeypatch.setattr(stock, "record_ward_consumption_in_txn",
                        lambda *a, **k: calls.append(1) or {"status": "skipped"})
    record = _give(world, o["med_id"])
    assert record["status"] == "given"
    assert calls == []
    assert _consumptions(drug) == [] and _qty(lot["id"]) == 5


@pytest.mark.parametrize("source", [None, "pharmacy", "patient_own"])
def test_only_an_order_given_from_ward_stock_is_booked(world, source):
    drug, _s, lot = _stocked(world)
    o = _order(world, _patient(world), drug, supply_source=source)
    _give(world, o["med_id"])
    assert _consumptions(drug) == [] and _qty(lot["id"]) == 5


def test_a_not_given_record_and_an_unlinked_row_book_nothing(world):
    drug, _s, lot = _stocked(world)
    pid = _patient(world)
    o = _order(world, pid, drug)
    emar_service.record_administration(o["med_id"], world.nurse, status="not_given",
                                       not_given_reason="Patient asleep, try later")
    _give(world, _unlinked_row(world, pid))
    assert _consumptions(drug) == [] and _qty(lot["id"]) == 5


def test_an_order_with_no_drug_master_binding_books_nothing(world):
    drug, _s, lot = _stocked(world)
    o = _order(world, _patient(world), None)
    _give(world, o["med_id"])
    assert _consumptions(drug) == [] and _qty(lot["id"]) == 5


def test_a_drug_outside_the_wards_stock_is_not_booked(world):
    # Not on this ward's list and none held here.
    unlisted = _drug(world)
    store = _store(world)
    other_drug = _drug(world)
    _par(store["id"], other_drug)
    _lot(other_drug, store["id"], 5, _days(60))
    o = _order(world, _patient(world), unlisted)
    record = _give(world, o["med_id"])
    assert _consumptions(unlisted) == []
    with db_session() as db:
        plan = stock.plan_ward_consumption(db, record["id"], med_order_id=o["order_id"])
        unlinked = stock.plan_ward_consumption(db, record["id"], med_order_id=None)
    assert plan["reason"] == "not_ward_stock" and unlinked["reason"] == "not_linked"

    # Another department's store lists and holds it: not this patient's ward.
    elsewhere = _drug(world)
    away = _store(world, dept=OTHER_DEPT, ward="W10C-9")
    _par(away["id"], elsewhere)
    away_lot = _lot(elsewhere, away["id"], 5, _days(60))
    _give(world, _order(world, _patient(world), elsewhere)["med_id"])
    assert _consumptions(elsewhere) == [] and _qty(away_lot["id"]) == 5

    # A crash cart in the department lists and holds it: never booked by a dose.
    cart_drug = _drug(world)
    cart = _store(world, kind="crash_cart")
    _par(cart["id"], cart_drug)
    cart_lot = _lot(cart_drug, cart["id"], 5, _days(60))
    _give(world, _order(world, _patient(world), cart_drug)["med_id"])
    assert _consumptions(cart_drug) == [] and _qty(cart_lot["id"]) == 5


def test_a_patient_with_no_department_on_record_books_nothing(world):
    drug, _s, lot = _stocked(world)
    o = _order(world, _patient(world, admitted=False), drug)
    record = _give(world, o["med_id"])
    with db_session() as db:
        plan = stock.plan_ward_consumption(db, record["id"], med_order_id=o["order_id"])
    assert plan["status"] == "skipped" and plan["reason"] == "no_department"
    assert _consumptions(drug) == [] and _qty(lot["id"]) == 5


def test_two_ward_stores_in_one_department_are_told_apart_by_the_ward(world):
    drug = _drug(world)
    here = _store(world, ward=WARD)
    there = _store(world, ward="W10C-2")
    for s in (here, there):
        _par(s["id"], drug)
    here_lot = _lot(drug, here["id"], 5, _days(60))
    there_lot = _lot(drug, there["id"], 5, _days(30))
    _give(world, _order(world, _patient(world, ward=WARD), drug)["med_id"])
    assert (_qty(here_lot["id"]), _qty(there_lot["id"])) == (4, 5)

    # A patient on a third ward of the department: nothing is guessed.
    o = _order(world, _patient(world, ward="W10C-3"), drug)
    record = _give(world, o["med_id"])
    with db_session() as db:
        plan = stock.plan_ward_consumption(db, record["id"], med_order_id=o["order_id"])
    assert plan["reason"] == "store_ambiguous"
    assert (_qty(here_lot["id"]), _qty(there_lot["id"])) == (4, 5)


def test_held_and_expired_units_are_never_booked(world):
    drug = _drug(world)
    store = _store(world)
    _par(store["id"], drug)
    held = _lot(drug, store["id"], 3, _days(40), status="quarantined")
    expired = _lot(drug, store["id"], 2, _days(-1))
    o = _order(world, _patient(world), drug)
    record = _give(world, o["med_id"])
    assert _mar(record["id"])["status"] == "given"
    assert _consumptions(drug) == []
    assert (_qty(held["id"]), _qty(expired["id"])) == (3, 2)
    with db_session() as db:
        plan = stock.plan_ward_consumption(db, record["id"], med_order_id=o["order_id"])
    assert plan["reason"] == "no_ward_lot"


# ═════════════════════════════════════════════════════════════════════════════
#  Replay
# ═════════════════════════════════════════════════════════════════════════════

def test_an_idempotent_replay_of_the_dose_books_it_once(world):
    drug, _s, lot = _stocked(world)
    o = _order(world, _patient(world), drug)
    key = f"w10c-{uuid.uuid4().hex}"
    first = _give(world, o["med_id"], idempotency_key=key)
    again = _give(world, o["med_id"], idempotency_key=key)
    assert again["replayed"] is True and again["id"] == first["id"]
    assert len(_consumptions(drug)) == 1 and _qty(lot["id"]) == 4

    # Asked again for the same administration, the books answer the first.
    with db_session(immediate=True) as conn:
        out = stock.record_ward_consumption_in_txn(conn, first["id"],
                                                   med_order_id=o["order_id"],
                                                   actor_id=world.nurse)
    assert out["status"] == "already"
    assert len(_consumptions(drug)) == 1 and _qty(lot["id"]) == 4
    assert len(_audits("ward_stock_consumed", first["id"])) == 1


# ═════════════════════════════════════════════════════════════════════════════
#  A negative ward balance: booked and shown, never refused
# ═════════════════════════════════════════════════════════════════════════════

def _overview_drug(store_code: str, drug: str) -> dict:
    out = transfers.ward_stock(actor={"id": "W10CDESK", "role": "pharmacist"},
                               store_code=store_code)
    [store] = out["stores"]
    return next(d for d in store["drugs"] if d["drug_id"] == drug)


def test_a_negative_balance_is_booked_and_shown_not_refused(world):
    drug = _drug(world)
    store = _store(world)
    _par(store["id"], drug)
    empty = _lot(drug, store["id"], 0, _days(90), batch="NB0")
    o = _order(world, _patient(world), drug)

    first = _give(world, o["med_id"])
    assert _mar(first["id"])["status"] == "given"
    [move] = _consumptions(drug)
    assert move["lot_id"] == empty["id"] and move["balance_after_units"] == -1
    assert "not certain" in move["remarks"] and "Count this drug" in move["remarks"]
    assert _qty(empty["id"]) == -1
    meta = json.loads(_audits("ward_stock_consumed", first["id"])[0]["metadata_json"])
    assert meta["below_zero"] is True and meta["balance_after_units"] == -1

    shown = _overview_drug(store["code"], drug)
    assert shown["below_zero_units"] == 1 and shown["on_hand_units"] == 0
    [lot_entry] = [entry for entry in shown["lots"] if entry["lot_id"] == empty["id"]]
    assert lot_entry["units"] == -1 and lot_entry["status_label"] == "below zero on the books"

    with db_session() as db:
        checked = stock.reconcile(db, lot_ids=[empty["id"]])
    # The books still add up (quantity equals its ledger), and the gap is named.
    assert checked["negative_lots"] == [empty["id"]]
    assert checked["mismatched_lots"] == [] and checked["balance_breaks"] == []

    _give(world, o["med_id"])
    assert _qty(empty["id"]) == -2
    assert _overview_drug(store["code"], drug)["below_zero_units"] == 2

    # Units arriving on the ward are used FEFO before the books go further below.
    fresh = _lot(drug, store["id"], 3, _days(120))
    _give(world, o["med_id"])
    assert (_qty(empty["id"]), _qty(fresh["id"])) == (-2, 2)


def test_a_batch_booked_below_zero_still_takes_its_next_delivery(world):
    drug = _drug(world)
    store = _store(world)
    _par(store["id"], drug)
    lot = _lot(drug, store["id"], 0, _days(90), batch="ND1")
    o = _order(world, _patient(world), drug)
    for _ in range(3):
        _give(world, o["med_id"])
    assert _qty(lot["id"]) == -3
    # The same batch arriving on the ward merges into it, short of the gap ...
    assert _lot(drug, store["id"], 2, _days(90), batch="ND1")["id"] == lot["id"]
    assert _qty(lot["id"]) == -1
    # ... and past it.
    _lot(drug, store["id"], 5, _days(90), batch="ND1")
    assert _qty(lot["id"]) == 4
    assert _overview_drug(store["code"], drug)["below_zero_units"] == 0
    with db_session() as db:
        assert stock.reconcile(db, lot_ids=[lot["id"]])["clean"] is True


def test_a_ward_store_with_no_negative_lot_shows_nothing_new(world):
    drug, store, _lot_row = _stocked(world)
    shown = _overview_drug(store["code"], drug)
    assert shown["below_zero_units"] == 0 and shown["on_hand_units"] == 5


def test_only_a_dose_may_take_a_lot_below_zero(world):
    drug, store, lot = _stocked(world, units=1)
    with db_session(immediate=True) as db:
        with pytest.raises(stock.StockError) as refused:
            stock.post_movement(db, lot_id=lot["id"], movement_type="write_off",
                                quantity_change=-3, actor_id="tester", allow_negative=True)
    assert refused.value.code == "negative_not_allowed"
    with db_session(immediate=True) as db:
        with pytest.raises(stock.InsufficientStock):
            stock.post_movement(db, lot_id=lot["id"], movement_type="consumption",
                                quantity_change=-3, actor_id="tester")
    sub = _store(world, kind="sub_store", dept=None, ward=None)
    sub_lot = _lot(drug, sub["id"], 1, _days(60))
    with db_session(immediate=True) as db:
        with pytest.raises(stock.StockError) as refused:
            stock.post_movement(db, lot_id=sub_lot["id"], movement_type="consumption",
                                quantity_change=-3, actor_id="tester", allow_negative=True)
    assert refused.value.code == "negative_not_allowed"
    assert (_qty(lot["id"]), _qty(sub_lot["id"])) == (1, 1)


# ═════════════════════════════════════════════════════════════════════════════
#  The recall trace
# ═════════════════════════════════════════════════════════════════════════════

def test_the_recall_trace_attributes_consumed_units_to_patients(world):
    drug = _drug(world)
    store = _store(world)
    _par(store["id"], drug)
    batch = _lot(drug, store["id"], 2, _days(60), batch="RC77A")
    other = _lot(drug, store["id"], 5, _days(300), batch="ZZ900")
    first, second, third = (_patient(world) for _ in range(3))
    given = []
    for pid in (first, second, third):
        given.append(_give(world, _order(world, pid, drug)["med_id"]))
    # FEFO: two from RC77A, then RC77A is empty and ZZ900 has units.
    assert (_qty(batch["id"]), _qty(other["id"])) == (0, 4)
    with db_session(immediate=True) as db:
        db.execute("UPDATE helix_inventory_lots SET status = 'quarantined' WHERE id = ?",
                   (other["id"],))
    fourth = _patient(world)
    given.append(_give(world, _order(world, fourth, drug)["med_id"]))
    assert _qty(batch["id"]) == -1          # booked below zero on the ward's own batch

    with db_session() as db:
        trace = stock.ward_consumption_for_batch(db, drug, "rc 77a")
        others = stock.ward_consumption_for_batch(db, drug, "ZZ900")
        assert stock.ward_consumption_for_batch(db, drug, "") == []
    assert [t["patient_id"] for t in trace] == [first, second, fourth]
    assert [t["administration_id"] for t in trace] == [given[0]["id"], given[1]["id"],
                                                       given[3]["id"]]
    assert [t["batch_certain"] for t in trace] == [True, True, False]
    assert all(t["units"] == 1 and t["store_id"] == store["id"] for t in trace)
    assert trace[0]["batch_no"] == "RC77A" and trace[0]["department"] == DEPT
    assert [t["patient_id"] for t in others] == [third]
