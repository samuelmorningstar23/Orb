"""The NDPS register (grn-13): services/ndps_service.py, the register leg in
pharmacy_stock_service.post_movement and /api/pharmacy-store/ndps/*.

Pinned (docs/his_design/grn.json, grn-13 tests, and the strict defaults the
service docstring lists for Samuel):
  * with the switch off, a narcotic dispense never calls into the register;
  * ORB_PHARMACY_NDPS_REGISTER without ORB_PHARMACY_NDPS_ON_FAILURE leaves the
    register off and logs an ERROR at boot (ruling GRN-RS7 held);
  * 'block': a register failure rolls the dispense back, nothing changes;
  * 'record': a register failure commits the dispense, writes no register
    row, one variance and one 'security' audit row, all in the dispense's
    transaction; a negative balance is recorded the same way;
  * every classified movement writes exactly one register entry (a first
    movement into an empty store opens the register at zero), none on
    rollback, and the register's balance equals SUM(lots) per drug and store;
  * a dispense entry names its patient, order and prescriber;
  * stock already on the shelf is never vouched for unopened: the movement is
    refused until a witnessed opening; a reclassified drug holding stock opens
    in the same transaction;
  * an opening or a check over a difference raises a variance for every part
    of it no variance has recorded yet, so the variances raised against a
    register add up to its difference from the shelf and a second shortfall
    is never dropped by an opening;
  * the witness: self, a wrong PIN, an ineligible person and lockout are
    refused; write-offs, adjustments, vault transfers need one;
  * live entries survive a reseed, practice entries are cleared; erasure and
    the retention review keep live entries;
  * the register view and the export are audited; the export is refused when
    its audit row cannot be written; a patient outside the reader's department
    is not named;
  * routes need pharmacy_store.ndps and answer every role cleanly.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token, hash_password
from database import db_session
from db_backend import DBDatabaseError
from main import app
from services import audit_service, helix_service, ndps_service
from services import pharmacy_stock_service as stock
from services import pharmacy_transfer_service as transfers
from services import pin_security
from services import procurement_service as procurement
from services.audit_service import AuditWriteError
from services.helix_db import init_helix_db

client = TestClient(app)
BASE = "/api/pharmacy-store"
PIN = "5173"
SERVER = Path(__file__).resolve().parents[1]


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
    # The register's lane follows the deployment profile (ndps_service.current_lane).
    # Rows written to the shared test database must be 'non_production', which the
    # fixtures can delete ('live' rows are append-only), so pin a non-prod profile
    # whatever profile the suite runs under. The live lane is tested on scratch
    # databases by the tests that set the profile themselves.
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")


def _register(monkeypatch, mode):
    """Switch the register on with a failure mode (or off with None)."""
    monkeypatch.setattr(config, "PHARMACY_NDPS_REGISTER_ENABLED", mode is not None)
    monkeypatch.setattr(config, "PHARMACY_NDPS_ON_FAILURE", mode)


def _user(role="pharmacist", *, pin=PIN, department=None):
    uid = f"USRN{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, pin_hash, department) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, hash_password("secretpass"),
                    pin_security.hash_pin(pin) if pin else None, department))
    return {"id": uid, "role": role, "department": department,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


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
        for drug in m.drugs:
            db.execute("DELETE FROM ndps_register WHERE helix_medication_id = ? "
                       "AND lane = 'non_production'", (drug,))
            db.execute("DELETE FROM ndps_variances WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_transfer_lines WHERE helix_medication_id = ?",
                       (drug,))
            db.execute("DELETE FROM helix_stock_movements WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_lot_events WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_inventory_lots WHERE medication_id = ?", (drug,))
            for (oid,) in db.execute("SELECT id FROM helix_patient_med_orders WHERE "
                                     "medication_id = ?", (drug,)).fetchall():
                db.execute("DELETE FROM helix_transitions WHERE order_id = ?", (oid,))
            db.execute("DELETE FROM helix_patient_med_orders WHERE medication_id = ?", (drug,))
        for store_id in m.stores:
            db.execute("DELETE FROM pharmacy_transfers WHERE to_store_id = ? OR from_store_id = ?",
                       (store_id, store_id))
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def _drug(m, **fields) -> str:
    med = f"MEDNR{_uid()}"
    cols = {"id": med, "name": f"Ndpstest {med[-6:]} 10 mg", "generic_name": "ndpsol",
            "form": "injection", "strength": "10 mg", "drug_schedule": "X",
            "ndps_category": "narcotic", "pack_size_units": 10} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    m.drugs.append(med)
    return med


def _store(m, kind="ndps_vault") -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(db, code=f"ND{_uid()}", name=f"Ndps {kind} {_uid()}",
                                             domain="pharmacy", pharmacy_kind=kind)
    m.stores.append(row["id"])
    return dict(row)


@pytest.fixture
def vault(made):
    return _store(made, "ndps_vault")


@pytest.fixture
def dispensing(made):
    with db_session() as db:
        row = db.execute("SELECT * FROM stores WHERE pharmacy_kind = 'dispensing' "
                         "AND is_active = 1").fetchone()
    if row is not None:
        return dict(row)
    return _store(made, "dispensing")


def _receive(drug, store_id, units, *, actor="tester", expiry=None, batch=None):
    """Units in by a 'receipt' movement (the register's receipt entry)."""
    with db_session(immediate=True) as db:
        return stock.open_or_merge_lot(
            db, medication_id=drug, lot_number=batch or f"B{_uid()}",
            expiration_date=expiry or _days(300), units=units, actor_id=actor,
            store_id=store_id, movement_type="receipt", reference_type="pharmacy_grn",
            reference_id="999")["lot"]


def _order(m, drug, prescriber="D001", *, department="Ndps Ward"):
    pid = f"PATNR{_uid()}"
    oid = f"ORDNR{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'discharged')",
                   (pid, f"Ndps Case {pid[-4:]}", f"MRN-{pid}"))
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, status, "
                   "admitted_by) VALUES (?, ?, ?, ?, 'discharged', 'D001')",
                   (f"ADM-{pid}", pid, department, department))
        db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, dosage, "
                   "route, frequency, status, prescribed_by) "
                   "VALUES (?, ?, ?, '10 mg', 'IV', 'STAT', 'scheduled', ?)",
                   (oid, pid, drug, prescriber))
    m.patients.append(pid)
    return oid, pid


def _dispense(oid, actor):
    return helix_service.transition_order_status(order_id=oid, target_status="dispensed",
                                                 actor_id=actor,
                                                 idempotency_key=f"nd-{oid}")


def _q(sql, *params):
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _entries(drug):
    return _q("SELECT * FROM ndps_register WHERE helix_medication_id = ? ORDER BY store_id, seq",
              drug)


def _variances(drug):
    return _q("SELECT * FROM ndps_variances WHERE helix_medication_id = ? ORDER BY id", drug)


def _audits(action, **meta):
    rows = _q("SELECT * FROM audit_logs WHERE action_type = ? ORDER BY id", action)
    out = []
    for r in rows:
        m = json.loads(r["metadata_json"] or "{}")
        if all(m.get(k) == v for k, v in meta.items()):
            out.append(r | {"meta": m})
    return out


def _qty(lot_id):
    return _q("SELECT quantity FROM helix_inventory_lots WHERE id = ?", lot_id)[0]["quantity"]


def _balance_row(drug, store_id):
    rows = [r for r in ndps_service.balances(drug_id=drug)["rows"] if r["store_id"] == store_id]
    return rows[0] if rows else None


def _err(resp):
    return resp.json()["detail"]["error"]


# ═════════════════════════════════════════════════════════════════════════════
#  The switch and the failure mode (GRN-RS7)
# ═════════════════════════════════════════════════════════════════════════════

def test_with_the_switch_off_a_narcotic_dispense_never_calls_the_register(monkeypatch, made,
                                                                           vault):
    _register(monkeypatch, None)
    called = []

    def spy(conn, movement):
        called.append(movement)
        raise AssertionError("the register was called with the switch off")

    monkeypatch.setattr(ndps_service, "write_movement_entry", spy)
    monkeypatch.setattr(ndps_service, "is_controlled", spy)
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    oid, _ = _order(made, drug)
    result = _dispense(oid, "D001")
    assert result["order"]["status"] == "dispensed"
    assert called == [] and _entries(drug) == [] and _variances(drug) == []
    assert _qty(lot["id"]) == 4


def test_the_register_asked_for_without_a_mode_stays_off(monkeypatch, made, vault):
    monkeypatch.setattr(config, "PHARMACY_NDPS_REGISTER_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_NDPS_ON_FAILURE", None)
    assert config.feature_enabled("ndps_register") is False
    assert ndps_service.register_on() is False
    drug = _drug(made)
    _receive(drug, vault["id"], 3)
    assert _entries(drug) == []


def test_boot_with_the_register_asked_for_and_no_mode_logs_an_error():
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"),
           "ORB_DEPLOYMENT_PROFILE": "dev",
           "ORB_JWT_SECRET": "suite-only-secret-0123456789abcdef0123456789",
           "ORB_PHARMACY_STORE": "1", "ORB_PHARMACY_NDPS_REGISTER": "1"}
    out = subprocess.run(
        [sys.executable, "-c",
         "import config; print('ON' if config.feature_enabled('ndps_register') else 'OFF')"],
        cwd=str(SERVER), env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "OFF"
    assert "NDPS register stays OFF" in out.stderr
    assert "ORB_PHARMACY_NDPS_ON_FAILURE" in out.stderr


def test_block_mode_a_register_failure_rolls_the_dispense_back(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    entries_before = len(_entries(drug))

    def broken(conn, movement):
        raise RuntimeError("register disk fault")

    monkeypatch.setattr(ndps_service, "write_movement_entry", broken)
    oid, _ = _order(made, drug)
    with pytest.raises(ValueError) as caught:
        _dispense(oid, "D001")
    assert "NDPS register" in str(caught.value)
    assert _qty(lot["id"]) == 5
    assert _q("SELECT status FROM helix_patient_med_orders WHERE id = ?", oid)[0]["status"] \
        == "scheduled"
    assert _q("SELECT * FROM helix_stock_movements WHERE order_id = ?", oid) == []
    assert _q("SELECT * FROM helix_transitions WHERE order_id = ?", oid) == []
    assert len(_entries(drug)) == entries_before and _variances(drug) == []


def test_record_mode_a_register_failure_commits_the_dispense_with_one_variance(monkeypatch, made,
                                                                               vault):
    _register(monkeypatch, "record")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    before = len(_entries(drug))
    assert before == 2, "the first receipt into the vault opened the register at zero"
    real = ndps_service.write_movement_entry

    def broken(conn, movement):
        real(conn, movement)          # writes rows, then fails: all of them go
        raise RuntimeError("register disk fault")

    monkeypatch.setattr(ndps_service, "write_movement_entry", broken)
    oid, _ = _order(made, drug)
    result = _dispense(oid, "D001")
    assert result["order"]["status"] == "dispensed"
    moves = _q("SELECT * FROM helix_stock_movements WHERE order_id = ?", oid)
    assert len(moves) == 1 and _qty(lot["id"]) == 4
    assert len(_entries(drug)) == before, "the register leg rolled back on its own"
    variances = _variances(drug)
    assert len(variances) == 1
    v = variances[0]
    assert v["variance_kind"] == "register_write_failed" and v["status"] == "open"
    assert v["helix_order_id"] == oid and v["movement_ref"] == moves[0]["id"]
    assert v["lane"] == "non_production" and "register disk fault" in v["detail"]
    assert v["expected_units"] == 4 and v["found_units"] == 4
    audits = _audits("ndps_variance_raised", variance_id=str(v["id"]))
    assert len(audits) == 1 and audits[0]["event_category"] == "security"
    assert audits[0]["meta"]["failure_mode"] == "record"
    # The dispense's own audit row is there too: the same transaction committed.
    assert _q("SELECT 1 FROM audit_logs WHERE action_type = 'helix_dispense_recorded' "
              "AND target_id = ?", oid)


def test_record_mode_a_negative_balance_is_a_variance_and_block_mode_refuses(monkeypatch, made,
                                                                             vault):
    drug = _drug(made)
    _register(monkeypatch, None)
    lot = _receive(drug, vault["id"], 2)          # on the shelf before the register
    _register(monkeypatch, "record")
    with db_session(immediate=True) as db:        # an opening that undercounts the shelf
        ndps_service._insert_entry(db, lane="non_production", store_id=vault["id"],
                                   drug_id=drug, entry_type="opening", quantity=0,
                                   balance_after=0, actor_id="tester",
                                   occurred_at="2026-10-09T00:00:00Z")
    oid, _ = _order(made, drug)
    assert _dispense(oid, "D001")["order"]["status"] == "dispensed"
    variances = _variances(drug)
    assert [v["variance_kind"] for v in variances] == ["negative_balance"]
    assert len(_entries(drug)) == 1
    assert _qty(lot["id"]) == 1

    _register(monkeypatch, "block")
    oid2, _ = _order(made, drug)
    with pytest.raises(ValueError):
        _dispense(oid2, "D001")
    assert _qty(lot["id"]) == 1 and len(_variances(drug)) == 1


def test_record_mode_applies_to_dispenses_only(monkeypatch, made, vault):
    """Any other movement blocks in both modes: nothing waits on it at a bed."""
    _register(monkeypatch, "record")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 4)
    with pytest.raises(stock.StockError) as caught:
        with db_session(immediate=True) as db:
            stock.post_movement(db, lot_id=lot["id"], movement_type="write_off",
                                quantity_change=-1, actor_id="tester")
    assert "witness" in str(caught.value)
    assert _qty(lot["id"]) == 4 and _variances(drug) == []


# ═════════════════════════════════════════════════════════════════════════════
#  One entry per movement, the running balance, the dispense entry
# ═════════════════════════════════════════════════════════════════════════════

def test_every_classified_movement_writes_exactly_one_entry(monkeypatch, made, vault, dispensing):
    _register(monkeypatch, "block")
    ph_a, ph_b, ph_c = _user(), _user(), _user()
    drug = _drug(made)
    plain = _drug(made, ndps_category="none", drug_schedule="H")
    lot = _receive(drug, vault["id"], 10)
    _receive(plain, dispensing["id"], 10)
    assert _entries(plain) == [], "a drug that is not controlled has no register"
    rows = _entries(drug)
    assert [(r["entry_type"], r["quantity_units"], r["balance_after_units"]) for r in rows] == [
        ("opening", 0, 0), ("receipt", 10, 10)]
    assert rows[1]["seq"] == 2 and rows[1]["lane"] == "non_production"

    oid, pid = _order(made, drug, prescriber="D002")
    _dispense(oid, "D001")
    dispense = _entries(drug)[-1]
    assert (dispense["entry_type"], dispense["quantity_units"], dispense["balance_after_units"]) \
        == ("dispense", -1, 9)
    assert (dispense["patient_id"], dispense["helix_order_id"], dispense["prescriber_id"]) == (
        pid, oid, "D002")
    assert dispense["counterparty_type"] == "patient" and dispense["actor_id"] == "D001"
    assert dispense["lot_id"] == lot["id"] and dispense["batch_no"] == lot["lot_number"]

    # A vault transfer: one entry per leg, each with its witness.
    doc = transfers.create_transfer(
        from_store_code=vault["code"], to_store_code=dispensing["code"],
        lines=[{"helix_medication_id": drug, "quantity_units": 3}], actor=ph_a)["transfer"]
    transfers.dispatch_transfer(doc["id"], actor=ph_a, witness_id=ph_c["id"], witness_pin=PIN)
    sent = transfers.get_transfer(doc["id"])
    transfers.receive_transfer(doc["id"], actor=ph_b, witness_id=ph_c["id"], witness_pin=PIN,
                               lines=[{"line_id": line["id"],
                                       "received_units": line["quantity_units"]}
                                      for line in sent["lines"]])
    vault_rows = [r for r in _entries(drug) if r["store_id"] == vault["id"]]
    shelf_rows = [r for r in _entries(drug) if r["store_id"] == dispensing["id"]]
    assert vault_rows[-1]["entry_type"] == "transfer_out" and vault_rows[-1]["quantity_units"] == -3
    assert vault_rows[-1]["witness_id"] == ph_c["id"]
    assert vault_rows[-1]["counterparty_type"] == "store"
    assert vault_rows[-1]["counterparty_ref"] == str(dispensing["id"])
    assert [(r["entry_type"], r["quantity_units"], r["balance_after_units"])
            for r in shelf_rows] == [("opening", 0, 0), ("transfer_in", 3, 3)]
    assert shelf_rows[-1]["witness_id"] == ph_c["id"]

    # A witnessed wastage.
    with db_session(immediate=True) as db:
        with ndps_service.entry_context(witness_id=ph_b["id"], entry_type="wastage"):
            stock.post_movement(db, lot_id=lot["id"], movement_type="write_off",
                                quantity_change=-1, actor_id=ph_a["id"], remarks="Ampoule broke")
    wastage = [r for r in _entries(drug) if r["store_id"] == vault["id"]][-1]
    assert (wastage["entry_type"], wastage["witness_id"]) == ("wastage", ph_b["id"])

    # Each movement after the openings wrote exactly one entry.
    moves = _q("SELECT id FROM helix_stock_movements WHERE medication_id = ?", drug)
    non_opening = [r for r in _entries(drug) if r["entry_type"] != "opening"]
    assert len(non_opening) == len(moves)
    assert {r["movement_ref"] for r in non_opening} == {m["id"] for m in moves}
    # The register's balance equals what the lots hold, per drug and store.
    for store in (vault, dispensing):
        row = _balance_row(drug, store["id"])
        units = _q("SELECT COALESCE(SUM(quantity), 0) AS n FROM helix_inventory_lots WHERE "
                   "medication_id = ? AND store_id = ?", drug, store["id"])[0]["n"]
        assert row["status"] == "matches", row
        assert row["register_units"] == row["shelf_units"] == units
    with db_session() as db:
        assert stock.reconcile(db, lot_ids=[r["id"] for r in _q(
            "SELECT id FROM helix_inventory_lots WHERE medication_id = ?", drug)])["clean"]


def test_no_entry_survives_a_rolled_back_movement(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 6)
    count = len(_entries(drug))
    with pytest.raises(RuntimeError):
        with db_session(immediate=True) as db:
            with ndps_service.entry_context(witness_id="SOMEONE"):
                stock.post_movement(db, lot_id=lot["id"], movement_type="adjustment",
                                    quantity_change=-2, actor_id="tester")
            assert len(db.execute("SELECT id FROM ndps_register WHERE helix_medication_id = ?",
                                  (drug,)).fetchall()) == count + 1
            raise RuntimeError("the caller's transaction fails later")
    assert len(_entries(drug)) == count and _qty(lot["id"]) == 6


def test_a_movement_needing_a_witness_is_refused_without_one(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 6)
    for movement_type, change in (("adjustment", -1), ("count_variance", 1),
                                  ("write_off", -1)):
        with pytest.raises(stock.StockError) as caught:
            with db_session(immediate=True) as db:
                stock.post_movement(db, lot_id=lot["id"], movement_type=movement_type,
                                    quantity_change=change, actor_id="tester")
        assert "witness" in str(caught.value), movement_type
    with pytest.raises(stock.StockError):
        with db_session(immediate=True) as db:
            with ndps_service.entry_context(witness_id="tester"):     # the actor themself
                stock.post_movement(db, lot_id=lot["id"], movement_type="adjustment",
                                    quantity_change=-1, actor_id="tester")
    assert _qty(lot["id"]) == 6


def test_a_movement_with_no_register_entry_is_refused(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 6)
    with pytest.raises(stock.StockError) as caught:
        with db_session(immediate=True) as db:
            stock.post_movement(db, lot_id=lot["id"], movement_type="consumption",
                                quantity_change=-1, actor_id="tester")
    assert "no entry" in str(caught.value)
    assert _qty(lot["id"]) == 6


def test_a_legacy_lot_outside_any_store_cannot_be_entered(monkeypatch, made):
    _register(monkeypatch, None)
    drug = _drug(made)
    with db_session(immediate=True) as db:
        lot = stock.open_or_merge_lot(db, medication_id=drug, lot_number=f"L{_uid()}",
                                      expiration_date=_days(200), units=3,
                                      actor_id="tester")["lot"]
    _register(monkeypatch, "block")
    oid, _ = _order(made, drug)
    with pytest.raises(ValueError) as caught:
        _dispense(oid, "D001")
    assert "not in a pharmacy store" in str(caught.value)
    assert _qty(lot["id"]) == 3


# ═════════════════════════════════════════════════════════════════════════════
#  Opening the register
# ═════════════════════════════════════════════════════════════════════════════

def test_stock_already_on_the_shelf_is_refused_until_a_witnessed_opening(monkeypatch, made,
                                                                         vault):
    drug = _drug(made)
    _register(monkeypatch, None)
    lot = _receive(drug, vault["id"], 8)
    _register(monkeypatch, "block")
    assert _balance_row(drug, vault["id"])["status"] == "not_opened"
    oid, _ = _order(made, drug)
    with pytest.raises(ValueError) as caught:
        _dispense(oid, "D001")
    assert "not been opened" in str(caught.value)
    assert _qty(lot["id"]) == 8

    ph_a, ph_b = _user(), _user()
    body = {"reason": "Register switched on", "witness_id": ph_b["id"], "witness_pin": PIN,
            "store_id": vault["id"], "drug_id": drug}
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"], json=body)
    assert resp.status_code == 200, resp.text
    opened = resp.json()["opened"]
    assert [(o["drug_id"], o["units"]) for o in opened] == [(drug, 8)]
    entry = _entries(drug)[-1]
    assert (entry["entry_type"], entry["balance_after_units"], entry["witness_id"]) == (
        "opening", 8, ph_b["id"])
    assert _audits("ndps_register_opened", witness_id=ph_b["id"])
    _dispense(oid, "D001")
    assert _balance_row(drug, vault["id"])["status"] == "matches"
    assert _entries(drug)[-1]["balance_after_units"] == 7


def test_opening_with_the_register_off_is_409(monkeypatch, made, vault):
    _register(monkeypatch, None)
    ph_a, ph_b = _user(), _user()
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json={"reason": "x", "witness_id": ph_b["id"], "witness_pin": PIN})
    assert resp.status_code == 409 and _err(resp) == "register_off"


def test_a_drug_classified_narcotic_while_holding_stock_opens_its_register(monkeypatch, made,
                                                                           dispensing):
    from services import pharmacy_master_service as masters

    drug = _drug(made, ndps_category="none", drug_schedule="H")
    _receive(drug, dispensing["id"], 12)
    _register(monkeypatch, "block")
    ph = _user()
    out = masters.classify_drug(drug, {"ndps_category": "narcotic"}, actor=ph,
                                reason="Reclassified by the formulary committee")
    assert out["applied"] == ["ndps_category"]
    rows = _entries(drug)
    assert [(r["store_id"], r["entry_type"], r["balance_after_units"]) for r in rows] == [
        (dispensing["id"], "opening", 12)]
    assert rows[0]["document_ref"] == f"classification:{drug}"
    assert _balance_row(drug, dispensing["id"])["status"] == "matches"
    assert _variances(drug) == [], "a first opening has no old register to disagree with"


def test_classification_with_the_register_off_writes_nothing(monkeypatch, made, dispensing):
    from services import pharmacy_master_service as masters

    _register(monkeypatch, None)
    drug = _drug(made, ndps_category="none", drug_schedule="H")
    _receive(drug, dispensing["id"], 4)
    masters.classify_drug(drug, {"ndps_category": "psychotropic"}, actor=_user(), reason="x")
    assert _entries(drug) == []


def _shortfall_with_the_register_off(monkeypatch, lot_id, units):
    """Stock leaves the shelf while the register is off: the register keeps
    its old balance and now differs from the shelf."""
    _register(monkeypatch, None)
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot_id, movement_type="adjustment",
                            quantity_change=-units, actor_id="tester")
    _register(monkeypatch, "block")


def _opening_body(witness, store_id, drug, reason="Register opened again after an outage"):
    return {"reason": reason, "witness_id": witness["id"], "witness_pin": PIN,
            "store_id": store_id, "drug_id": drug}


def test_opening_again_over_a_difference_raises_a_variance_first(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    _shortfall_with_the_register_off(monkeypatch, lot["id"], 2)
    row = _balance_row(drug, vault["id"])
    assert row["status"] == "differs" and (row["register_units"], row["shelf_units"]) == (5, 3)

    ph_a, ph_b = _user(), _user()
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=_opening_body(ph_b, vault["id"], drug))
    assert resp.status_code == 200, resp.text
    [opened] = [o for o in resp.json()["opened"] if o["drug_id"] == drug]
    assert (opened["previous_status"], opened["previous_register_units"], opened["units"],
            opened["variance_raised"]) == ("differs", 5, 3, True)

    # The shortfall stays in reconciliation: one open variance, old register
    # balance expected, the shelf found.
    [variance] = _variances(drug)
    assert opened["variance_id"] == variance["id"]
    assert (variance["variance_kind"], variance["expected_units"], variance["found_units"],
            variance["variance_units"], variance["status"], variance["raised_by"]) == (
        "reconciliation_mismatch", 5, 3, -2, "open", ph_a["id"])
    assert "opened again" in variance["detail"]
    [raised] = _audits("ndps_variance_raised", variance_id=str(variance["id"]))
    assert raised["event_category"] == "security" and raised["meta"]["opened_by"] == "ndps_opening"
    assert (raised["meta"]["register_units"], raised["meta"]["shelf_units"]) == (5, 3)
    opening_audit = [a for a in _audits("ndps_register_opened", witness_id=ph_b["id"])
                     if any(o["drug_id"] == drug for o in a["meta"]["openings"])]
    assert opening_audit and opening_audit[-1]["meta"]["variances_raised"] == 1
    row = _balance_row(drug, vault["id"])
    assert row["status"] == "matches" and row["open_variances"] == 1

    # Opening again over a register that matches raises nothing.
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=_opening_body(ph_b, vault["id"], drug))
    assert resp.status_code == 200, resp.text
    [opened] = [o for o in resp.json()["opened"] if o["drug_id"] == drug]
    assert (opened["previous_status"], opened["variance_id"], opened["variance_raised"]) == (
        "matches", None, False)
    assert len(_variances(drug)) == 1

    # A second shortfall while the first variance is still open belongs to
    # the register the last opening started: this opening records it in a
    # variance of its own, so the open variances add up to the whole loss.
    _shortfall_with_the_register_off(monkeypatch, lot["id"], 1)
    row = _balance_row(drug, vault["id"])
    assert (row["status"], row["register_units"], row["shelf_units"]) == ("differs", 3, 2)
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=_opening_body(ph_b, vault["id"], drug))
    assert resp.status_code == 200, resp.text
    [opened] = [o for o in resp.json()["opened"] if o["drug_id"] == drug]
    assert (opened["previous_status"], opened["previous_register_units"], opened["units"],
            opened["variance_raised"]) == ("differs", 3, 2, True)
    first, second = _variances(drug)
    assert first["id"] == variance["id"] and opened["variance_id"] == second["id"]
    assert (first["expected_units"], first["found_units"], first["variance_units"]) == (5, 3, -2)
    assert (second["variance_kind"], second["expected_units"], second["found_units"],
            second["variance_units"], second["status"]) == (
        "reconciliation_mismatch", 3, 2, -1, "open")
    assert sum(v["variance_units"] for v in _variances(drug) if v["status"] == "open") == -3
    assert _qty(lot["id"]) == 2, "5 received, 3 left the shelf unrecorded"
    [raised] = _audits("ndps_variance_raised", variance_id=str(second["id"]))
    assert raised["meta"]["variance_units"] == -1 and "1 unit short" in raised["summary_message"]
    row = _balance_row(drug, vault["id"])
    assert row["status"] == "matches" and row["open_variances"] == 2


def _reconcile_check(user):
    resp = client.post(f"{BASE}/ndps/reconcile", headers=user["hdr"])
    assert resp.status_code == 200, resp.text
    return resp.json()["raised"]


def test_a_difference_that_grows_records_only_its_rest_and_is_never_doubled(monkeypatch, made,
                                                                             vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    ph_a, ph_b = _user(), _user()

    # The check records the first shortfall; a check with nothing new raises nothing.
    _shortfall_with_the_register_off(monkeypatch, lot["id"], 2)
    [first] = [r for r in _reconcile_check(ph_a) if r["drug_id"] == drug]
    assert (first["expected_units"], first["found_units"], first["variance_units"]) == (5, 3, -2)
    assert [r for r in _reconcile_check(ph_a) if r["drug_id"] == drug] == []

    # The register has not been opened again, so a further shortfall is the
    # same register's: the next check records only the rest.
    _shortfall_with_the_register_off(monkeypatch, lot["id"], 1)
    [rest] = [r for r in _reconcile_check(ph_a) if r["drug_id"] == drug]
    assert (rest["register_units"], rest["shelf_units"]) == (5, 2)
    assert (rest["expected_units"], rest["found_units"], rest["variance_units"]) == (3, 2, -1)
    v1, v2 = _variances(drug)
    assert "record 2 units short; this one records the rest, 1 unit short" in v2["detail"]
    assert v1["movement_ref"] == v2["movement_ref"]

    # The first is closed, witnessed, while the register still differs. A
    # reconciled variance no longer counts: the check raises its part again,
    # so the open variances still record the whole difference (the strict
    # option: a closed loss can be recorded twice, never left out).
    path = f"{BASE}/ndps/variances/{v1['id']}/reconcile"
    resp = client.post(path, headers=ph_a["hdr"],
                       json={"note": "Two ampoules taken with the register off",
                             "witness_id": ph_b["id"], "witness_pin": PIN})
    assert resp.status_code == 200, resp.text
    [again] = [r for r in _reconcile_check(ph_a) if r["drug_id"] == drug]
    assert (again["register_units"], again["shelf_units"]) == (5, 2)
    assert (again["expected_units"], again["found_units"], again["variance_units"]) == (4, 2, -2)
    v3 = _variances(drug)[-1]
    assert v3["id"] == again["variance_id"] and v3["status"] == "open"
    assert "still open for this register record 1 unit short; this one records the rest, " \
           "2 units short" in v3["detail"]
    assert [r for r in _reconcile_check(ph_a) if r["drug_id"] == drug] == []

    # Opening over the register whose whole difference the open variances
    # record raises nothing more and names the newest open one.
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=_opening_body(ph_b, vault["id"], drug))
    assert resp.status_code == 200, resp.text
    [opened] = [o for o in resp.json()["opened"] if o["drug_id"] == drug]
    assert (opened["previous_status"], opened["units"], opened["variance_id"],
            opened["variance_raised"]) == ("differs", 2, v3["id"], False)
    assert len(_variances(drug)) == 3
    assert sum(v["variance_units"] for v in _variances(drug)
               if v["status"] == "open") == -3 == _qty(lot["id"]) - 5

    # Units back on the shelf with the register off are recorded as a surplus.
    _register(monkeypatch, None)
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot["id"], movement_type="adjustment",
                            quantity_change=1, actor_id="tester")
    _register(monkeypatch, "block")
    [over] = [r for r in _reconcile_check(ph_a) if r["drug_id"] == drug]
    assert (over["expected_units"], over["found_units"], over["variance_units"]) == (2, 3, 1)
    assert _variances(drug)[-1]["movement_ref"] != v2["movement_ref"], "a new register"
    assert ndps_service._units_words(1) == "1 unit over"
    assert ndps_service._units_words(-3) == "3 units short"
    assert ndps_service._units_words(0) == "no units"


def _move_with_the_register_off(monkeypatch, lot_id, change):
    """Units leave (or come back to) the shelf while the register is off."""
    _register(monkeypatch, None)
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot_id, movement_type="adjustment",
                            quantity_change=change, actor_id="tester")
    _register(monkeypatch, "block")


def _close_variance(variance_id, user, witness, note="Found the two ampoules in the fridge"):
    resp = client.post(f"{BASE}/ndps/variances/{variance_id}/reconcile", headers=user["hdr"],
                       json={"note": note, "witness_id": witness["id"], "witness_pin": PIN})
    assert resp.status_code == 200, resp.text


def _mine(raised, drug):
    return [r for r in raised if r["drug_id"] == drug]


def test_a_loss_after_a_recovered_and_reconciled_variance_is_raised(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    ph_a, ph_b = _user(), _user()
    _move_with_the_register_off(monkeypatch, lot["id"], -2)          # 2 go missing
    [v1] = _mine(_reconcile_check(ph_a), drug)
    assert v1["variance_units"] == -2
    _move_with_the_register_off(monkeypatch, lot["id"], +2)          # and come back
    row = _balance_row(drug, vault["id"])
    assert (row["status"], row["register_units"], row["shelf_units"]) == ("matches", 5, 5)
    assert _mine(_reconcile_check(ph_a), drug) == []
    _close_variance(v1["variance_id"], ph_a, ph_b)                    # closed as found

    # Two other units leave. The reconciled variance never absorbs them.
    _move_with_the_register_off(monkeypatch, lot["id"], -2)
    [v2] = _mine(_reconcile_check(ph_a), drug)
    assert (v2["register_units"], v2["shelf_units"]) == (5, 3)
    assert (v2["expected_units"], v2["found_units"], v2["variance_units"]) == (5, 3, -2)
    assert v2["variance_id"] != v1["variance_id"]
    [raised] = [a for a in _audits("ndps_variance_raised")
                if any(v.get("variance_id") == v2["variance_id"]
                       for v in a["meta"].get("variances", []))]
    assert raised["event_category"] == "security"

    # The opening finds the loss recorded by the open variance: it raises
    # nothing more and names that one, not the reconciled one.
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=_opening_body(ph_b, vault["id"], drug))
    assert resp.status_code == 200, resp.text
    [opened] = [o for o in resp.json()["opened"] if o["drug_id"] == drug]
    assert (opened["previous_status"], opened["units"], opened["variance_id"],
            opened["variance_raised"]) == ("differs", 3, v2["variance_id"], False)
    variances = _variances(drug)
    assert [(v["status"], v["expected_units"], v["found_units"], v["variance_units"])
            for v in variances] == [("reconciled", 5, 3, -2), ("open", 5, 3, -2)]
    assert sum(v["variance_units"] for v in variances if v["status"] == "open") == -2
    assert _qty(lot["id"]) == 3


def test_a_new_loss_is_never_recorded_as_a_surplus(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    ph_a, ph_b = _user(), _user()

    # The first variance recovered and reconciled, then 1 unit leaves: a
    # shortage of 1, never "1 unit over".
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    _move_with_the_register_off(monkeypatch, lot["id"], -2)
    [v1] = _mine(_reconcile_check(ph_a), drug)
    _move_with_the_register_off(monkeypatch, lot["id"], +2)
    _close_variance(v1["variance_id"], ph_a, ph_b)
    _move_with_the_register_off(monkeypatch, lot["id"], -1)
    [new] = _mine(_reconcile_check(ph_a), drug)
    assert (new["expected_units"], new["found_units"], new["variance_units"]) == (5, 4, -1)
    assert "over" not in _variances(drug)[-1]["detail"]

    # The same with the first variance left open: it already records more
    # than the 1 unit short, so nothing is netted against it and no surplus
    # is raised; once it is reconciled, the 1 unit short is raised.
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    _move_with_the_register_off(monkeypatch, lot["id"], -2)
    [v1] = _mine(_reconcile_check(ph_a), drug)
    _move_with_the_register_off(monkeypatch, lot["id"], +2)
    _move_with_the_register_off(monkeypatch, lot["id"], -1)
    row = _balance_row(drug, vault["id"])
    assert (row["status"], row["register_units"], row["shelf_units"]) == ("differs", 5, 4)
    assert _mine(_reconcile_check(ph_a), drug) == []
    assert [v["variance_units"] for v in _variances(drug)] == [-2]
    _close_variance(v1["variance_id"], ph_a, ph_b, note="Two ampoules found, one still missing")
    [new] = _mine(_reconcile_check(ph_a), drug)
    assert (new["expected_units"], new["found_units"], new["variance_units"]) == (5, 4, -1)
    assert all(v["variance_units"] < 0 for v in _variances(drug))

    # An opening over a register its open variance over-records raises
    # nothing and names that open variance.
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    _move_with_the_register_off(monkeypatch, lot["id"], -2)
    [v1] = _mine(_reconcile_check(ph_a), drug)
    _move_with_the_register_off(monkeypatch, lot["id"], +1)
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=_opening_body(ph_b, vault["id"], drug))
    assert resp.status_code == 200, resp.text
    [opened] = [o for o in resp.json()["opened"] if o["drug_id"] == drug]
    assert (opened["previous_status"], opened["units"], opened["variance_id"],
            opened["variance_raised"]) == ("differs", 4, v1["variance_id"], False)
    assert [(v["status"], v["variance_units"]) for v in _variances(drug)] == [("open", -2)]


def test_a_surplus_is_never_recorded_as_a_shortage_and_a_crossing_is_raised(monkeypatch, made,
                                                                             vault):
    _register(monkeypatch, "block")
    ph_a, ph_b = _user(), _user()

    # 2 units appear, 1 goes again while that variance is open: nothing is
    # raised against it (it records more than the 1 unit over); once it is
    # reconciled the 1 unit over is raised, as a surplus.
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    _move_with_the_register_off(monkeypatch, lot["id"], +2)
    [up] = _mine(_reconcile_check(ph_a), drug)
    assert up["variance_units"] == 2
    _move_with_the_register_off(monkeypatch, lot["id"], -1)
    assert _mine(_reconcile_check(ph_a), drug) == []
    _close_variance(up["variance_id"], ph_a, ph_b, note="Two ampoules returned unrecorded")
    [over] = _mine(_reconcile_check(ph_a), drug)
    assert (over["expected_units"], over["found_units"], over["variance_units"]) == (5, 6, 1)

    # A shortage that turns into a surplus while its variance is open: the
    # rest is raised in the surplus's direction, so the open variances add
    # up to the difference.
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    _move_with_the_register_off(monkeypatch, lot["id"], -2)
    [down] = _mine(_reconcile_check(ph_a), drug)
    _move_with_the_register_off(monkeypatch, lot["id"], +3)
    [rest] = _mine(_reconcile_check(ph_a), drug)
    assert (rest["register_units"], rest["shelf_units"]) == (5, 6)
    assert (rest["expected_units"], rest["found_units"], rest["variance_units"]) == (3, 6, 3)
    assert sum(v["variance_units"] for v in _variances(drug) if v["status"] == "open") == 1
    assert down["variance_id"] != rest["variance_id"]


def test_opening_again_over_a_broken_running_balance_raises_a_variance(monkeypatch, made,
                                                                       vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    _receive(drug, vault["id"], 5)
    with db_session(immediate=True) as db:          # an entry that breaks the running balance
        ndps_service._insert_entry(
            db, lane=ndps_service.current_lane(), store_id=vault["id"], drug_id=drug,
            entry_type="adjustment", quantity=-1, balance_after=5, actor_id="tester",
            occurred_at=ndps_service.utc_iso_now(), counterparty_type="none",
            witness_id="SOMEONE")
    assert _balance_row(drug, vault["id"])["status"] == "chain_broken"
    ph_a, ph_b = _user(), _user()
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=_opening_body(ph_b, vault["id"], drug))
    assert resp.status_code == 200, resp.text
    [opened] = [o for o in resp.json()["opened"] if o["drug_id"] == drug]
    assert (opened["previous_status"], opened["variance_raised"]) == ("chain_broken", True)
    [variance] = _variances(drug)
    assert (variance["variance_kind"], variance["status"]) == ("reconciliation_mismatch", "open")
    assert _audits("ndps_variance_raised", variance_id=str(variance["id"]))


def test_an_opening_whose_variance_cannot_be_audited_changes_nothing(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    _shortfall_with_the_register_off(monkeypatch, lot["id"], 2)
    entries = _entries(drug)
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "ndps_variance_raised":
            raise AuditWriteError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    ph_a, ph_b = _user(), _user()
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=_opening_body(ph_b, vault["id"], drug))
    assert resp.status_code == 503 and _err(resp) == "audit_unavailable"
    assert _entries(drug) == entries and _variances(drug) == []
    assert _balance_row(drug, vault["id"])["status"] == "differs"


def test_a_drug_classified_controlled_again_keeps_its_old_difference(monkeypatch, made,
                                                                     dispensing):
    from services import pharmacy_master_service as masters

    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, dispensing["id"], 5)
    ph_a, ph_b = _user(), _user()
    # Control removed (a second person approves), then stock moves unrecorded.
    out = masters.classify_drug(drug, {"ndps_category": "none"}, actor=ph_a,
                                reason="Formulary committee removed control")
    masters.decide_change_request(out["requested"][0]["id"], approve=True, actor=ph_b)
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot["id"], movement_type="adjustment",
                            quantity_change=-2, actor_id="tester")
    row = _balance_row(drug, dispensing["id"])
    assert (row["status"], row["register_units"], row["shelf_units"]) == ("differs", 5, 3)

    out = masters.classify_drug(drug, {"ndps_category": "narcotic"}, actor=ph_a,
                                reason="Control restored")
    assert out["applied"] == ["ndps_category"]
    last = _entries(drug)[-1]
    assert (last["entry_type"], last["balance_after_units"], last["document_ref"]) == (
        "opening", 3, f"classification:{drug}")
    [variance] = _variances(drug)
    assert (variance["variance_kind"], variance["expected_units"], variance["found_units"],
            variance["store_id"], variance["status"], variance["raised_by"]) == (
        "reconciliation_mismatch", 5, 3, dispensing["id"], "open", ph_a["id"])
    assert "classified narcotic again" in variance["detail"]
    [raised] = _audits("ndps_variance_raised", variance_id=str(variance["id"]))
    assert raised["event_category"] == "security"
    assert raised["meta"]["opened_by"] == "classification"
    row = _balance_row(drug, dispensing["id"])
    assert row["status"] == "matches" and row["open_variances"] == 1


# ═════════════════════════════════════════════════════════════════════════════
#  The witness
# ═════════════════════════════════════════════════════════════════════════════

def test_self_witness_wrong_pin_ineligible_and_lockout_are_refused(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    _receive(drug, vault["id"], 5)
    ph_a, ph_b = _user(), _user()
    body = {"reason": "Opening", "store_id": vault["id"], "drug_id": drug}
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"], json=body)
    assert resp.status_code == 422 and _err(resp) == "witness_required"
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=body | {"witness_id": ph_a["id"], "witness_pin": PIN})
    assert resp.status_code == 403 and _err(resp) == "witness_same_person"
    nurse = _user("nurse")
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=body | {"witness_id": nurse["id"], "witness_pin": PIN})
    assert resp.status_code == 403 and _err(resp) == "witness_not_eligible"
    no_pin = _user(pin=None)
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=body | {"witness_id": no_pin["id"], "witness_pin": PIN})
    assert resp.status_code == 403 and _err(resp) == "witness_no_pin"
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=body | {"witness_id": ph_b["id"], "witness_pin": "0000"})
    assert resp.status_code == 403 and _err(resp) == "witness_pin_wrong"
    assert _audits("ndps_witness_refused", witness_id=ph_b["id"])
    for _ in range(pin_security.MAX_PIN_ATTEMPTS):
        client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                    json=body | {"witness_id": ph_b["id"], "witness_pin": "0000"})
    resp = client.post(f"{BASE}/ndps/openings", headers=ph_a["hdr"],
                       json=body | {"witness_id": ph_b["id"], "witness_pin": PIN})
    assert resp.status_code == 429 and _err(resp) == "witness_locked"
    assert _entries(drug)[-1]["entry_type"] == "receipt", "nothing was opened"


# ═════════════════════════════════════════════════════════════════════════════
#  Reseed, erasure and retention
# ═════════════════════════════════════════════════════════════════════════════

def _scratch(tmp_path):
    import migrations
    from migrations_his.m0049_helix_tables_in_runner import MIGRATION as M49
    from migrations_his.m0054_pharmacy_store_schema import MIGRATION as M54

    conn = sqlite3.connect(str(tmp_path / "ndps.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE patients (id TEXT PRIMARY KEY, name TEXT, mrn TEXT)")
    migrations._m20_hospital_admin_spine(conn)
    M49.apply(conn)
    M54.apply(conn)
    conn.execute("INSERT INTO stores (id, code, name, store_type, is_active, pharmacy_kind) "
                 "VALUES (9001, 'NDPSVAULT', 'Vault', 'sub_store', 1, 'ndps_vault')")
    conn.execute("INSERT INTO patients (id, name, mrn) VALUES ('P1', 'Erased Patient', 'M1')")
    for drug in ("MORPH", "PETH"):
        conn.execute("INSERT INTO helix_medication_master (id, name, ndps_category, "
                     "drug_schedule) VALUES (?, ?, 'narcotic', 'X')", (drug, drug.title()))
        conn.execute("INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, "
                     "expiration_date, store_id, status) VALUES (?, ?, 'B1', 5, '2099-01-01', 9001, "
                     "'available')", (f"LOT-{drug}", drug))
        conn.execute("INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, "
                     "status, prescribed_by) VALUES (?, 'P1', ?, 'dispensed', 'D001')",
                     (f"O-{drug}", drug))
    conn.commit()
    return conn


def _movement(drug, change, movement_type, **extra):
    return {"id": f"MV-{uuid.uuid4().hex[:8]}", "lot_id": f"LOT-{drug}", "medication_id": drug,
            "movement_type": movement_type, "quantity_change": change, "store_id": 9001,
            "actor_id": "PH1", "order_id": None, "reference_type": None, "reference_id": None,
            "occurred_at": "2026-10-09T08:00:00Z", "remarks": None} | extra


def test_live_entries_survive_a_reseed_and_practice_entries_are_cleared(monkeypatch, tmp_path):
    from scripts.reset_db_and_seed import wipe_patient_tables

    conn = _scratch(tmp_path)
    conn.execute("BEGIN")
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "prod")
    ndps_service.write_movement_entry(conn, _movement("MORPH", 5, "receipt"))
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")
    ndps_service.write_movement_entry(conn, _movement("PETH", 5, "receipt"))
    conn.commit()
    lanes = [(r["helix_medication_id"], r["lane"]) for r in conn.execute(
        "SELECT * FROM ndps_register ORDER BY id")]
    assert lanes == [("MORPH", "live"), ("MORPH", "live"),
                     ("PETH", "non_production"), ("PETH", "non_production")]
    with pytest.raises(DBDatabaseError):
        conn.execute("DELETE FROM ndps_register WHERE lane = 'live'")
    with pytest.raises(DBDatabaseError):
        conn.execute("UPDATE ndps_register SET remarks = 'edited'")
    conn.rollback()
    wipe_patient_tables(conn)
    assert [r["lane"] for r in conn.execute("SELECT lane FROM ndps_register")] == ["live", "live"]
    conn.close()


def test_erasure_and_retention_review_keep_live_entries(monkeypatch, tmp_path):
    from routes import admin_routes
    from services import consent_service

    assert "ndps_register" in consent_service.ERASURE_RETAINED_TABLES
    assert "ndps_register" in admin_routes._ERASE_RETAINED
    assert all(table != "ndps_register" for sources in consent_service._RETENTION_SOURCES.values()
               for table, _, _ in sources), "the retention review never ages the register"
    conn = _scratch(tmp_path)
    conn.execute("BEGIN")
    for drug, profile in (("MORPH", "prod"), ("PETH", "dev")):
        monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", profile)
        ndps_service.write_movement_entry(conn, _movement(drug, 5, "receipt"))
        ndps_service.write_movement_entry(conn, _movement(drug, -1, "dispense",
                                                          order_id=f"O-{drug}"))
    conn.commit()
    dispensed = [(r["helix_medication_id"], r["lane"], r["patient_id"]) for r in conn.execute(
        "SELECT * FROM ndps_register WHERE entry_type = 'dispense' ORDER BY id")]
    assert dispensed == [("MORPH", "live", "P1"), ("PETH", "non_production", "P1")]
    kept = consent_service._retained_after_erasure(conn, "P1")
    register = [k for k in kept if k["table"] == "ndps_register"]
    assert register and register[0]["rows"] == 1, "only the live dispense entry is kept"
    conn.close()


# ═════════════════════════════════════════════════════════════════════════════
#  Routes: tiers, the view, the export, variances
# ═════════════════════════════════════════════════════════════════════════════

def test_every_ndps_route_is_404_with_the_module_off(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    ph = _user()
    for method, path in (("get", "/ndps/options"), ("get", "/ndps/balances"),
                         ("get", "/ndps/register"), ("get", "/ndps/variances"),
                         ("get", "/ndps/export.csv"), ("post", "/ndps/openings"),
                         ("post", "/ndps/reconcile"), ("post", "/ndps/variances/1/reconcile"),
                         ("post", "/ndps/variances/1/incident")):
        resp = getattr(client, method)(f"{BASE}{path}", headers=ph["hdr"],
                                       **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 404, (path, resp.text)
        assert _err(resp) == "module_disabled"


def test_every_ndps_get_answers_cleanly_for_every_role():
    paths = ("/ndps/options", "/ndps/balances", "/ndps/register", "/ndps/variances",
             "/ndps/variances?status=all", "/ndps/export.csv")
    for role in ("admin", "doctor", "nurse", "pharmacist", "ward_clerk", "cashier",
                 "receptionist", "lab_technician"):
        who = _user(role)
        for path in paths:
            resp = client.get(f"{BASE}{path}", headers=who["hdr"])
            assert resp.status_code < 500, (role, path, resp.text)
            if role != "pharmacist":
                assert resp.status_code == 403, (role, path, resp.status_code)


def test_the_register_view_is_audited_and_names_only_patients_in_scope(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    _receive(drug, vault["id"], 5)
    oid, pid = _order(made, drug, department="Ndps Oncology")
    _dispense(oid, "D001")
    ph = _user()
    resp = client.get(f"{BASE}/ndps/register", headers=ph["hdr"],
                      params={"store_id": vault["id"], "drug_id": drug})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lane"] == "non_production"
    dispense = [e for e in body["entries"] if e["entry_type"] == "dispense"][0]
    assert dispense["patient_id"] == pid and dispense["patient_name"].startswith("Ndps Case")
    assert dispense["helix_order_id"] == oid and dispense["prescriber_name"]
    viewed = [a for a in _audits("ndps_register_viewed") if pid in a["meta"]["patient_ids"]]
    assert viewed and viewed[-1]["event_category"] == "phi_access"

    scoped = _user(department="Ndps Cardiology")
    body = client.get(f"{BASE}/ndps/register", headers=scoped["hdr"],
                      params={"store_id": vault["id"], "drug_id": drug}).json()
    dispense = [e for e in body["entries"] if e["entry_type"] == "dispense"][0]
    assert dispense["patient_id"] is None and dispense["patient_name"] is None
    assert dispense["counterparty"] == ndps_service.OUT_OF_SCOPE_PATIENT
    assert dispense["helix_order_id"] is None


def test_a_register_read_with_no_audit_row_shows_nothing(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    _receive(drug, vault["id"], 5)
    oid, _ = _order(made, drug)
    _dispense(oid, "D001")
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") in ("ndps_register_viewed", "ndps_register_exported"):
            raise AuditWriteError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    ph = _user()
    resp = client.get(f"{BASE}/ndps/register", headers=ph["hdr"], params={"drug_id": drug})
    assert resp.status_code == 503 and _err(resp) == "audit_unavailable"
    resp = client.get(f"{BASE}/ndps/export.csv", headers=ph["hdr"], params={"drug_id": drug})
    assert resp.status_code == 503 and "text/csv" not in resp.headers.get("content-type", "")


def test_the_export_is_audited(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    _receive(drug, vault["id"], 5)
    oid, pid = _order(made, drug)
    _dispense(oid, "D001")
    ph = _user()
    resp = client.get(f"{BASE}/ndps/export.csv", headers=ph["hdr"],
                      params={"drug_id": drug, "from": "2000-01-01"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("Entry,Time (UTC),Store,Drug")
    assert len(lines) == 1 + 3, "opening, receipt and dispense"
    assert f"MRN-{pid}" in resp.text
    exported = _audits("ndps_register_exported", drug_id=drug)
    assert exported and exported[-1]["event_category"] == "security"
    assert exported[-1]["meta"]["entries"] == 3 and pid in exported[-1]["meta"]["patient_ids"]
    resp = client.get(f"{BASE}/ndps/export.csv", headers=ph["hdr"],
                      params={"from": "2026-10-09", "to": "2026-10-01"})
    assert resp.status_code == 422 and _err(resp) == "bad_date"


def test_a_csv_cell_is_never_a_formula():
    assert ndps_service._csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert ndps_service._csv_cell("@sum") == "'@sum"
    assert ndps_service._csv_cell(-3) == -3
    assert ndps_service._csv_cell(None) == ""


def test_admin_holds_no_ndps_permission(made):
    admin = _user("admin")
    for path in ("/ndps/options", "/ndps/register", "/ndps/export.csv"):
        assert client.get(f"{BASE}{path}", headers=admin["hdr"]).status_code == 403
    resp = client.post(f"{BASE}/ndps/reconcile", headers=admin["hdr"])
    assert resp.status_code == 403


def test_reconciliation_raises_a_variance_that_is_closed_witnessed(monkeypatch, made, vault):
    _register(monkeypatch, "block")
    drug = _drug(made)
    lot = _receive(drug, vault["id"], 5)
    _register(monkeypatch, None)                  # stock moves while the register is off
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=lot["id"], movement_type="adjustment",
                            quantity_change=-2, actor_id="tester")
    _register(monkeypatch, "block")
    row = _balance_row(drug, vault["id"])
    assert row["status"] == "differs" and (row["register_units"], row["shelf_units"]) == (5, 3)
    ph_a, ph_b = _user(), _user()
    resp = client.post(f"{BASE}/ndps/reconcile", headers=ph_a["hdr"])
    assert resp.status_code == 200, resp.text
    raised = [r for r in resp.json()["raised"] if r["drug_id"] == drug]
    assert len(raised) == 1
    again = client.post(f"{BASE}/ndps/reconcile", headers=ph_a["hdr"]).json()
    assert [r for r in again["raised"] if r["drug_id"] == drug] == [], "one open variance"
    variance = _variances(drug)[0]
    assert (variance["variance_kind"], variance["expected_units"], variance["found_units"],
            variance["variance_units"]) == ("reconciliation_mismatch", 5, 3, -2)

    listed = client.get(f"{BASE}/ndps/variances", headers=ph_a["hdr"]).json()["variances"]
    assert variance["id"] in {v["id"] for v in listed}
    path = f"{BASE}/ndps/variances/{variance['id']}/reconcile"
    resp = client.post(path, headers=ph_a["hdr"], json={"note": "Found the adjustment"})
    assert resp.status_code == 422 and _err(resp) == "witness_required"
    resp = client.post(path, headers=ph_a["hdr"],
                       json={"note": "Adjustment made with the register off; opened again",
                             "witness_id": ph_b["id"], "witness_pin": PIN})
    assert resp.status_code == 200, resp.text
    closed = _variances(drug)[0]
    assert closed["status"] == "reconciled" and closed["reconciled_by"] == ph_a["id"]
    assert _audits("ndps_variance_reconciled", variance_id=str(variance["id"]))
    resp = client.post(path, headers=ph_a["hdr"],
                       json={"note": "again", "witness_id": ph_b["id"], "witness_pin": PIN})
    assert resp.status_code == 409 and _err(resp) == "already_reconciled"


def test_a_variance_is_reported_to_the_incident_register_only_when_it_is_on(monkeypatch, made,
                                                                            vault):
    drug = _drug(made)
    _register(monkeypatch, "record")
    with db_session(immediate=True) as db:
        vid = ndps_service._insert_variance(db, kind="count_mismatch", store_id=vault["id"],
                                            drug_id=drug, raised_by="tester", expected=5,
                                            found=4, detail="One ampoule missing")
    ph = _user()
    monkeypatch.setattr(config, "NABH_ENABLED", False)
    resp = client.post(f"{BASE}/ndps/variances/{vid}/incident", headers=ph["hdr"])
    assert resp.status_code == 409 and _err(resp) == "incident_register_off"
    monkeypatch.setattr(config, "NABH_ENABLED", True)
    resp = client.post(f"{BASE}/ndps/variances/{vid}/incident", headers=ph["hdr"])
    assert resp.status_code == 200, resp.text
    ref = resp.json()["incident_ref"]
    try:
        assert _variances(drug)[0]["incident_ref"] == ref
        incident = _q("SELECT * FROM nabh_incidents WHERE reference = ?", ref)[0]
        assert incident["incident_type"] == "security" and incident["patient_id"] is None
        assert incident["reported_by"] == ph["id"]
        resp = client.post(f"{BASE}/ndps/variances/{vid}/incident", headers=ph["hdr"])
        assert resp.status_code == 409 and _err(resp) == "already_reported"
    finally:
        with db_session() as db:
            for (iid,) in db.execute("SELECT id FROM nabh_incidents WHERE reference = ?",
                                     (ref,)).fetchall():
                db.execute("DELETE FROM nabh_incident_events WHERE incident_id = ?", (iid,))
                db.execute("DELETE FROM nabh_incidents WHERE id = ?", (iid,))


def test_a_short_controlled_transfer_raises_a_count_variance(monkeypatch, made, vault,
                                                            dispensing):
    _register(monkeypatch, "block")
    ph_a, ph_b, ph_c = _user(), _user(), _user()
    drug = _drug(made)
    _receive(drug, vault["id"], 10)
    doc = transfers.create_transfer(
        from_store_code=vault["code"], to_store_code=dispensing["code"],
        lines=[{"helix_medication_id": drug, "quantity_units": 4}], actor=ph_a)["transfer"]
    transfers.dispatch_transfer(doc["id"], actor=ph_a, witness_id=ph_c["id"], witness_pin=PIN)
    sent = transfers.get_transfer(doc["id"])
    transfers.receive_transfer(doc["id"], actor=ph_b, witness_id=ph_c["id"], witness_pin=PIN,
                               lines=[{"line_id": sent["lines"][0]["id"], "received_units": 3,
                                       "discrepancy_reason": "One ampoule broken in transit"}])
    variances = _variances(drug)
    assert [(v["variance_kind"], v["expected_units"], v["found_units"]) for v in variances] == [
        ("count_mismatch", 4, 3)]
    assert _audits("ndps_variance_raised", transfer_id=str(doc["id"]))
    shelf = [r for r in _entries(drug) if r["store_id"] == dispensing["id"]]
    assert shelf[-1]["quantity_units"] == 3


def test_the_options_say_whether_the_register_is_on(monkeypatch):
    ph = _user()
    _register(monkeypatch, None)
    body = client.get(f"{BASE}/ndps/options", headers=ph["hdr"]).json()
    assert body["register_on"] is False and body["failure_mode"] is None
    assert body["lane"] == "non_production" and "Practice" in body["lane_label"]
    assert ph["id"] not in {w["id"] for w in body["witnesses"]}
    _register(monkeypatch, "record")
    body = client.get(f"{BASE}/ndps/options", headers=ph["hdr"]).json()
    assert body["register_on"] is True and body["failure_mode"] == "record"
    meta = client.get(f"{BASE}/meta", headers=ph["hdr"]).json()
    assert meta["features"]["ndps_register"] is True
