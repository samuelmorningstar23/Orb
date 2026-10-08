"""The Vigil chart add through the medication-order store (medorders-3, W5-E).

Pinned here:
  * store off: the chart add writes exactly what it wrote before, and no order;
  * store on: ONE transaction holds the ward row, its order (prescribed, the
    person charting it as prescriber, their role recorded), the 'created'
    event, the clinical_access audit row and the link both ways; 'prn' gives a
    prn order and 'discontinued' a stopped one; the active admission is named;
  * a nurse still charts a live drug on this lane (held rulings R010, R011);
  * a 428 from the gate writes nothing; the cross-ward 404 is unchanged;
  * a fault after the ward row INSERT rolls back the row and the order, and an
    Idempotency-Key replay then returns one row, one order and one event;
  * with the store on the gate runs off the event loop (defect F-D);
  * a blank drug name is refused before anything is stored.
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import medication_safety_gate
from services.clinical_records_service import add_allergy
from services.medorders import chart_lane, core

client = TestClient(app)
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_store_rows():
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL WHERE patient_id IN ({marks})",
                   _PATIENTS)


def _user(role, department=None):
    uid = f"MOCL{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"Chart lane {role}", role, department))
    return uid


def _hdr(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _patient(department=None):
    pid = f"PMOCL{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, "Chart Lane Test", f"MRN-{uuid.uuid4().hex[:8]}"))
        if department:
            db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                       "VALUES ('MOCLADM', 'Chart lane admitter', 'doctor', 'x')")
            db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                       "admitted_by) VALUES (?, ?, ?, ?, 'B-1', 'active', 'MOCLADM')",
                       (f"ADM-{pid}", pid, department, department))
    _PATIENTS.append(pid)
    return pid


@pytest.fixture()
def store_on(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)


@pytest.fixture()
def doctor():
    uid = _user("doctor")
    return uid, _hdr(uid, "doctor")


def _add(hdr, pid, *, drug="Paracetamol", dosage="500 mg", frequency="TDS", status=None,
         key=None, ack=None, http=client):
    body = {"drug_name": drug, "dosage": dosage, "frequency": frequency}
    if status:
        body["status"] = status
    if ack is not None:
        body["safety_ack"] = ack
    headers = dict(hdr)
    if key:
        headers["Idempotency-Key"] = key
    return http.post(f"/api/vigil/patients/{pid}/medications", json=body, headers=headers)


def _rows(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM medications WHERE patient_id = ? ORDER BY id", (pid,)).fetchall()]


def _orders(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM med_orders WHERE patient_id = ? ORDER BY created_at", (pid,)).fetchall()]


def _events(order_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM med_order_events WHERE med_order_id = ? ORDER BY id",
            (order_id,)).fetchall()]


def _audits(order_id, action=None):
    sql = "SELECT * FROM audit_logs WHERE target_type = 'med_order' AND target_id = ?"
    args = [order_id]
    if action:
        sql += " AND action_type = ?"
        args.append(action)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY rowid", args).fetchall()]


# ── Store off: exactly as before ──────────────────────────────────────────────

def test_store_off_charts_exactly_as_before_and_writes_no_order(doctor):
    _, hdr = doctor
    pid = _patient()
    resp = _add(hdr, pid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"status", "id", "safety"}
    (row,) = _rows(pid)
    assert row["id"] == body["id"] and row["source"] == "clinician"
    assert row["med_order_id"] is None
    assert _orders(pid) == []


_WARD_FACTS = ("drug_name", "dosage", "route", "frequency", "status", "prescribed_by",
               "source", "notes", "verified_by", "verified_at")


@pytest.mark.parametrize("route", [None, "IV", ""], ids=["no route sent", "IV", "blank"])
def test_the_ward_row_is_the_same_row_with_the_store_off_and_on(doctor, monkeypatch, route):
    """Whatever route the body sends (none, so the form's 'Oral'; a route; a
    blank), the store lane writes the ward row the store-off lane writes."""
    uid, hdr = doctor
    rows = []
    for store in (False, True):
        monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", store)
        pid = _patient()
        body = {"drug_name": "Paracetamol", "dosage": "500 mg", "frequency": "TDS"}
        if route is not None:
            body["route"] = route
        resp = client.post(f"/api/vigil/patients/{pid}/medications", json=body, headers=hdr)
        assert resp.status_code == 200, resp.text
        (row,) = _rows(pid)
        rows.append({k: row[k] for k in _WARD_FACTS})
    assert rows[0] == rows[1]
    assert rows[0]["route"] == ("Oral" if route is None else route)


# ── Store on: one transaction ─────────────────────────────────────────────────

def test_store_on_writes_the_row_its_order_event_and_audit_together(store_on, doctor):
    uid, hdr = doctor
    pid = _patient()
    resp = _add(hdr, pid, drug="Ibuprofen", dosage="400 mg", frequency="TDS")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (row,) = _rows(pid)
    (order,) = _orders(pid)
    assert body["id"] == row["id"] and body["med_order_id"] == order["id"]
    assert body["safety"]["safe"] is True
    assert row["med_order_id"] == order["id"] and row["source"] == "clinician"
    assert row["status"] == "active" and row["prescribed_by"] == uid
    assert order["status"] == "prescribed" and order["source"] == "chart"
    assert order["legacy_medication_id"] == row["id"]
    assert order["prescriber_id"] == uid and order["entered_by"] == uid
    assert order["entered_by_role"] == "doctor"
    assert order["projection_status"] == "active" and order["is_prn"] == 0
    assert (order["dose"], order["route"], order["frequency"]) == ("400 mg", "Oral", "TDS")
    assert order["needs_verification"] == 0
    assert json.loads(order["last_safety_json"])["max_severity"] == body["safety"]["max_severity"]
    (event,) = _events(order["id"])
    assert (event["kind"], event["to_status"], event["entry_point"]) == (
        "created", "prescribed", chart_lane.ENTRY_CHART_ADD)
    (audit,) = _audits(order["id"], "med_order_created")
    assert audit["event_category"] == "clinical_access" and audit["patient_id"] == pid
    meta = json.loads(audit["metadata_json"])
    assert meta["entry_point"] == "vigil.med_add" and meta["lane"] == "chart"
    assert meta["legacy_medication_id"] == str(row["id"])
    assert audit["request_path"] == f"/api/vigil/patients/{pid}/medications"


def test_the_active_admission_is_named_on_the_order(store_on, doctor):
    _, hdr = doctor
    pid = _patient("Cardiology")
    assert _add(hdr, pid).status_code == 200
    (order,) = _orders(pid)
    assert order["admission_id"] == f"ADM-{pid}"


def test_prn_gives_a_prn_order_and_discontinued_a_stopped_one(store_on, doctor):
    _, hdr = doctor
    pid = _patient()
    assert _add(hdr, pid, drug="Ondansetron", dosage="4 mg", frequency="PRN",
                status="prn").status_code == 200
    assert _add(hdr, pid, drug="Ranitidine", dosage="150 mg", frequency="BD",
                status="discontinued").status_code == 200
    rows = {r["drug_name"]: r for r in _rows(pid)}
    orders = {o["drug_name"]: o for o in _orders(pid)}
    prn = orders["Ondansetron"]
    assert (prn["status"], prn["is_prn"], prn["projection_status"]) == ("prescribed", 1, "prn")
    assert rows["Ondansetron"]["status"] == "prn"
    stopped = orders["Ranitidine"]
    assert stopped["status"] == "stopped" and stopped["projection_status"] is None
    assert stopped["status_reason"] == chart_lane.DISCONTINUED_AT_ADD_REASON
    assert rows["Ranitidine"]["status"] == "discontinued"
    assert rows["Ranitidine"]["med_order_id"] == stopped["id"]


def test_a_nurse_still_charts_a_live_drug_and_her_role_is_recorded(store_on):
    """Held rulings R010 and R011: the chart lane keeps today's nurse
    behaviour. The store records who entered it and as what."""
    pid = _patient("Cardiology")
    nurse = _user("nurse", "Cardiology")
    resp = _add(_hdr(nurse, "nurse"), pid)
    assert resp.status_code == 200, resp.text
    (order,) = _orders(pid)
    assert order["status"] == "prescribed"
    assert order["entered_by_role"] == "nurse" and order["prescriber_id"] == nurse


def test_a_cross_ward_chart_add_is_still_404_and_writes_nothing(store_on):
    pid = _patient("Oncology")
    nurse = _user("nurse", "Cardiology")
    resp = _add(_hdr(nurse, "nurse"), pid)
    assert resp.status_code == 404
    assert _rows(pid) == [] and _orders(pid) == []


def test_a_428_from_the_gate_writes_nothing(store_on, doctor):
    _, hdr = doctor
    pid = _patient()
    add_allergy(pid, "amoxicillin", reaction="anaphylaxis", severity="severe")
    resp = _add(hdr, pid, drug="Amoxicillin 500mg")
    assert resp.status_code == 428, resp.text
    assert resp.json()["detail"]["error"] == "drug_safety_acknowledgement_required"
    assert _rows(pid) == [] and _orders(pid) == []


def test_an_acknowledged_prescriber_override_is_kept_on_the_order(store_on, doctor):
    _, hdr = doctor
    pid = _patient()
    add_allergy(pid, "amoxicillin", reaction="anaphylaxis", severity="severe")
    resp = _add(hdr, pid, drug="Amoxicillin 500mg",
                ack={"acknowledged": True, "reason": "desensitisation protocol"})
    assert resp.status_code == 200, resp.text
    (order,) = _orders(pid)
    assert json.loads(order["last_safety_json"])["override"]["reason"] == "desensitisation protocol"
    (audit,) = _audits(order["id"], "med_order_created")
    assert json.loads(audit["metadata_json"])["safety"]["overridden"] is True


def test_a_blank_drug_name_is_refused_before_anything_is_stored(store_on, doctor):
    _, hdr = doctor
    pid = _patient()
    resp = _add(hdr, pid, drug="   ")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "drug_name_required"
    assert _rows(pid) == [] and _orders(pid) == []


# ── Faults and replays ────────────────────────────────────────────────────────

def test_a_fault_after_the_ward_row_insert_rolls_back_the_row_and_the_order(
        store_on, doctor, monkeypatch):
    _, hdr = doctor
    pid = _patient()
    key = f"chart-{uuid.uuid4()}"
    real_link = core.link_projection_in_txn

    def _boom(*a, **k):
        raise RuntimeError("injected fault after the ward row, the order and its event")

    monkeypatch.setattr(core, "link_projection_in_txn", _boom)
    faulty = TestClient(app, raise_server_exceptions=False)
    resp = _add(hdr, pid, key=key, http=faulty)
    assert resp.status_code == 500
    assert _rows(pid) == [] and _orders(pid) == []
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM medications WHERE idempotency_key = ?",
                          (key,)).fetchone()[0] == 0

    monkeypatch.setattr(core, "link_projection_in_txn", real_link)
    first = _add(hdr, pid, key=key)
    again = _add(hdr, pid, key=key)
    assert first.status_code == again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert first.json()["med_order_id"] == again.json()["med_order_id"]
    assert len(_rows(pid)) == 1
    (order,) = _orders(pid)
    assert len(_events(order["id"])) == 1
    assert len(_audits(order["id"], "med_order_created")) == 1


def test_a_replay_of_a_row_charted_before_the_store_returns_it_unlinked(doctor, monkeypatch):
    """A row written with the store off has no order. Its replay with the
    store on returns it as it is (adoption links it later, never a replay)."""
    _, hdr = doctor
    pid = _patient()
    key = f"chart-{uuid.uuid4()}"
    first = _add(hdr, pid, key=key)
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    again = _add(hdr, pid, key=key)
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["med_order_id"] is None
    assert _orders(pid) == []


# ── Off the event loop (F-D) ──────────────────────────────────────────────────

def test_with_the_store_on_the_gate_runs_off_the_event_loop(store_on, doctor, monkeypatch):
    _, hdr = doctor
    pid = _patient()
    seen = {}
    real_gate = medication_safety_gate.enforce_medication_safety

    def _spy(*a, **k):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        seen["thread"] = threading.current_thread().name
        return real_gate(*a, **k)

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety", _spy)
    assert _add(hdr, pid).status_code == 200
    assert seen["on_loop"] is False
    assert seen["thread"].startswith("orb-db")
