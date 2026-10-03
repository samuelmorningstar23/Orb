"""The dose guard on the CPOE lane (POST /api/orders), carried over from F061.

The route screened a medication order for allergy and interaction and never
handed the gate a dose, so an order carrying a structured dose in
detail.medication (the shape the order-set lane writes) skipped the dose guard
entirely: 8 g of paracetamol went in as a draft while the chart lane refused
the same order. The refusal must now be the chart lane's, in shape and in who
may clear it.
"""
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app

client = TestClient(app)

GOOD_REASON = "loading dose agreed with the consultant, weight 120 kg, LFTs normal"


def _user(role: str) -> str:
    uid = f"CPD{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, pin_hash) VALUES (?,?,?,?,?)",
            (uid, f"{role} {uid}", role, hash_password("secretpass"),
             hashlib.sha256(b"0000").hexdigest()),
        )
    return uid


def _hdr(uid: str) -> dict:
    resp = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture(autouse=True, scope="module")
def helix_tables():
    # The formulary the dose guard resolves brands through lives in a table the
    # session fixture does not create.
    from services.helix_db import init_helix_db
    init_helix_db()


@pytest.fixture()
def patient():
    pid = f"PATCPD{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
                   (pid, "CPOE Dose Test", f"MRN-{uuid.uuid4().hex[:6]}"))
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _order(hdr, pid, *, dose="8 g", ack=None, detail="default"):
    body = {
        "patient_id": pid, "order_type": "medication",
        "description": "Paracetamol", "code": "ORB-MED-PARACETAMOL",
        "code_system": "LOCAL",
    }
    if detail == "default":
        body["detail"] = {"medication": {"drug_name": "Paracetamol", "dose": dose,
                                         "route": "Oral", "frequency": "QDS"}}
    elif detail is not None:
        body["detail"] = detail
    if ack is not None:
        body["safety_ack"] = ack
    return client.post("/api/orders", json=body, headers=hdr)


def _cpoe_orders(pid: str) -> int:
    with db_session() as db:
        return db.execute(
            "SELECT count(*) FROM orders WHERE patient_id = ? AND order_type = 'medication'",
            (pid,)).fetchone()[0]


def _assert_dose_refusal(resp):
    """The refusal test_dose_guard_order_lanes.py pins for the chart lane."""
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "drug_safety_acknowledgement_required"
    safety = detail["safety"]
    assert safety["blocking"] is True
    check = safety["dose_check"]
    assert check["decision"] == "block"
    assert check["override_requires_permission"] == "medication.prescribe"
    assert any("at or above" in (f["message"] or "") for f in check["findings"])
    return detail


def test_an_8g_paracetamol_cpoe_order_is_refused_like_the_chart_lane(patient):
    hdr = _hdr(_user("doctor"))
    detail = _assert_dose_refusal(_order(hdr, patient))
    assert "dose check stopped this order" in detail["message"]
    assert _cpoe_orders(patient) == 0


def test_the_same_order_is_refused_identically_on_the_chart_lane(patient):
    """Lane parity, asserted side by side rather than by memory."""
    hdr = _hdr(_user("doctor"))
    cpoe = _assert_dose_refusal(_order(hdr, patient))
    chart = _assert_dose_refusal(client.post(
        f"/api/vigil/patients/{patient}/medications", headers=hdr,
        json={"drug_name": "Paracetamol", "dosage": "8 g", "route": "Oral",
              "frequency": "QDS"}))
    assert cpoe["safety"]["dose_check"]["decision"] == chart["safety"]["dose_check"]["decision"]
    assert cpoe["message"] == chart["message"]


def test_an_ordinary_cpoe_dose_is_untouched(patient):
    hdr = _hdr(_user("doctor"))
    resp = _order(hdr, patient, dose="1 g")
    assert resp.status_code == 200, resp.text
    assert resp.json()["safety"]["dose_check"]["decision"] == "allow"
    assert _cpoe_orders(patient) == 1


def test_an_order_with_no_structured_dose_is_screened_as_before(patient):
    """No dose means no dose check: allergy and interaction only, as today."""
    hdr = _hdr(_user("doctor"))
    resp = _order(hdr, patient, detail=None)
    assert resp.status_code == 200, resp.text
    assert "dose_check" not in resp.json()["safety"]


def test_a_nurse_cannot_clear_a_cpoe_dose_block(patient):
    hdr = _hdr(_user("nurse"))
    detail = _assert_dose_refusal(
        _order(hdr, patient, ack={"acknowledged": True, "reason": GOOD_REASON}))
    assert "Only a prescriber" in detail["safety"]["ack_refused"]
    assert _cpoe_orders(patient) == 0


def test_a_prescriber_clears_it_with_a_reason_and_it_is_audited(patient):
    hdr = _hdr(_user("doctor"))
    resp = _order(hdr, patient, ack={"acknowledged": True, "reason": GOOD_REASON})
    assert resp.status_code == 200, resp.text
    assert resp.json()["safety"]["dose_check"]["override"]["reason"] == GOOD_REASON
    assert _cpoe_orders(patient) == 1
    with db_session() as db:
        log = db.execute(
            "SELECT outcome FROM audit_logs WHERE action_type = 'dose_guard_override' "
            "AND patient_id = ? ORDER BY id DESC LIMIT 1", (patient,)).fetchone()
    assert log is not None and log["outcome"] == "partial_success"
