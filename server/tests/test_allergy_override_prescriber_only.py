"""A severe allergy block clears only for a prescriber, in every order lane.

The bedside interlock (routes/emar_routes.py) has refused a nurse's allergy
override since 2026-08. At order time the gate (services/medication_safety_gate)
accepted an acknowledgement from any caller, while only the dose block checked
``medication.prescribe``; nurses hold order.write and medication.write, so a
nurse could type a reason and chart a drug the patient is allergic to.

Pinned here: a nurse's acknowledgement is refused with the same 428 shape the
dose block uses (``safety.ack_refused``) and nothing is written; a doctor's
acknowledgement goes through and the audit entry names the doctor by id AND
name; and the pharmacy step only confirms a prescriber's override already on
the chain, never makes one.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app
from services import order_sets_service
from services.clinical_records_service import add_allergy
from services.medication_safety_gate import ALLERGY_ACK_REFUSAL, enforce_medication_safety
from services.terminology_service import seed_terminology

client = TestClient(app)

DEPT = "Allergy Override Prescriber Ward"
_RXNORM_AMOX = ("RXNORM", "723", "Amoxicillin")
ACK = {"acknowledged": True, "reason": "Supervised desensitisation protocol under the allergist."}


@pytest.fixture(scope="module", autouse=True)
def _terminology():
    seed_terminology()
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO terminology (code_system, code, display, is_active) "
                   "VALUES (?, ?, ?, 1)", _RXNORM_AMOX)


def _user(role, name=None):
    uid = f"AOP{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) VALUES (?,?,?,?,?)",
            (uid, name or f"{role.title()} {uid}", role, hash_password("secretpass"), DEPT),
        )
    return uid


def _hdr(uid):
    resp = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture()
def patient():
    pid = f"PAOP{uuid.uuid4().hex[:6].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('AOPADM', 'Allergy Override Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, status, admission_date) "
            "VALUES (?, ?, ?, 'B-2', ?, 'admitted', ?)",
            (pid, "Allergy Override Patient", f"MRN-{pid}", DEPT, now),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'B-2', 'active', 'AOPADM')",
            (pid, DEPT, DEPT),
        )
    add_allergy(pid, "amoxicillin", reaction="anaphylaxis", severity="severe")
    return pid


def _count(table, pid):
    with db_session() as db:
        return db.execute(f"SELECT count(*) FROM {table} WHERE patient_id = ?", (pid,)).fetchone()[0]


def _overrides(pid):
    with db_session() as db:
        return db.execute(
            "SELECT actor_user_id, metadata_json FROM audit_logs "
            "WHERE action_type = 'drug_safety_override' AND patient_id = ? ORDER BY id",
            (pid,)).fetchall()


def _cpoe(hdr, pid, ack=None):
    body = {"patient_id": pid, "order_type": "medication", "description": "Amoxicillin 500mg PO",
            "code": _RXNORM_AMOX[1], "code_system": _RXNORM_AMOX[0]}
    if ack:
        body["safety_ack"] = ack
    return client.post("/api/orders", json=body, headers=hdr)


def test_a_nurse_acknowledging_a_severe_allergy_at_order_time_is_refused(patient):
    hdr = _hdr(_user("nurse"))
    resp = _cpoe(hdr, patient, ack=ACK)
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "drug_safety_acknowledgement_required"
    assert detail["safety"]["ack_refused"] == ALLERGY_ACK_REFUSAL
    assert detail["safety"]["override_requires_permission"] == "medication.prescribe"
    assert _count("orders", patient) == 0
    assert _count("medications", patient) == 0
    assert _overrides(patient) == []


def test_a_nurse_cannot_override_on_the_chart_lane_either(patient):
    hdr = _hdr(_user("nurse"))
    resp = client.post(f"/api/vigil/patients/{patient}/medications",
                       json={"drug_name": "Amoxicillin 500mg", "dosage": "500mg",
                             "frequency": "TDS", "safety_ack": ACK},
                       headers=hdr)
    assert resp.status_code == 428, resp.text
    assert resp.json()["detail"]["safety"]["ack_refused"] == ALLERGY_ACK_REFUSAL
    assert _count("medications", patient) == 0
    assert _count("orders", patient) == 0


def test_a_doctors_override_is_recorded_with_their_name(patient):
    doctor = _user("doctor", name="Dr Meera Iyer")
    resp = _cpoe(_hdr(doctor), patient, ack=ACK)
    assert resp.status_code == 200, resp.text
    override = resp.json()["safety"]["override"]
    assert override["by"] == doctor and override["by_name"] == "Dr Meera Iyer"
    assert _count("orders", patient) == 1
    rows = _overrides(patient)
    assert len(rows) == 1 and rows[0]["actor_user_id"] == doctor
    meta = json.loads(rows[0]["metadata_json"])
    assert meta["override_by"]["user_id"] == doctor
    assert meta["override_by"]["display_name"] == "Dr Meera Iyer"
    assert meta["override_by"]["role"] == "doctor"


def test_an_order_set_applied_by_a_nurse_does_not_spend_an_allergy_override(patient):
    add_allergy(patient, "ceftriaxone", reaction="anaphylaxis", severity="severe")
    definition = order_sets_service.get_set("sepsis_six")
    ceftriaxone = next(i for i in definition["items"] if i["drug_name"] == "Ceftriaxone")
    nurse = _user("nurse")
    with pytest.raises(order_sets_service.OrderSetBlocked) as caught:
        order_sets_service.apply_set(
            patient, "sepsis_six", actor_id=nurse, actor_role="nurse",
            selections=[{"index": ceftriaxone["index"], "safety_ack": ACK}])
    stopped = next(b for b in caught.value.blocked if b["drug_name"] == "Ceftriaxone")
    assert stopped["block_reason"].startswith("Only a prescriber can override an allergy block.")
    assert _count("orders", patient) == 0
    assert _overrides(patient) == []


# ── the pharmacy step confirms, never decides ────────────────────────────────
def _gate(pid, actor, role, entry, prescriber_id=None):
    return enforce_medication_safety(
        pid, "Amoxicillin 500mg", actor_id=actor, actor_role=role,
        entry_point=entry, safety_ack=ACK, order_prescriber_id=prescriber_id)


def test_a_pharmacist_confirms_the_prescribers_own_override(patient):
    doctor, pharmacist = _user("doctor", name="Dr Arun Rao"), _user("pharmacist")
    _gate(patient, doctor, "doctor", "relay.confirm_agentic")
    safety = _gate(patient, pharmacist, "pharmacist", "helix.verified", prescriber_id=doctor)
    assert safety["override"]["by"] == pharmacist
    meta = json.loads(_overrides(patient)[-1]["metadata_json"])
    assert meta["override_by"]["confirms_prescriber_override"]["prescriber_id"] == doctor


def test_a_pharmacist_cannot_override_when_no_prescriber_did(patient):
    doctor, pharmacist = _user("doctor"), _user("pharmacist")
    with pytest.raises(HTTPException) as caught:
        _gate(patient, pharmacist, "pharmacist", "helix.verified", prescriber_id=doctor)
    assert caught.value.status_code == 428
    assert caught.value.detail["safety"]["ack_refused"] == ALLERGY_ACK_REFUSAL
    with pytest.raises(HTTPException):
        _gate(patient, pharmacist, "pharmacist", "helix.verified")
    assert _overrides(patient) == []
