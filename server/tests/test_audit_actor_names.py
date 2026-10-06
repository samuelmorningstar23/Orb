"""The log shows names: resolved when it is read, never written into the chain.

The audit chain stores ``actor_user_id`` and ``actor_role`` and hashes exactly
those. The read routes add ``actor_name`` from users, so a person reads as a
person; a user who no longer exists reads as None. The pharmacist's queue names
who ordered and who checked a medicine the same way.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import pharmacy_verification_service as pharmacy
from services.audit_service import log_audit_event

client = TestClient(app)


def _user(role, name):
    uid = f"AAN{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?,?,?, 'x')",
                   (uid, name, role))
    return uid


def _hdr(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture()
def admin():
    return _user("admin", "Audit Reader")


def _event(actor, correlation):
    log_audit_event(correlation_id=correlation, event_category="clinical_safety",
                    action_type="actor_name_probe", outcome="success",
                    actor_user_id=actor, actor_role="doctor",
                    summary_message="actor name probe")


def test_audit_logs_carry_the_actor_name_and_none_for_a_gone_user(admin):
    doctor = _user("doctor", "Dr Kavya Menon")
    correlation = f"aan-{uuid.uuid4().hex}"
    _event(doctor, correlation)
    _event("GONE-USER-404", correlation)
    with db_session() as db:
        stored = db.execute("SELECT * FROM audit_logs WHERE correlation_id = ?",
                            (correlation,)).fetchall()
    assert all("actor_name" not in dict(r) for r in stored), "the chained row must not change"

    resp = client.get("/api/audit/logs", params={"correlation_id": correlation},
                      headers=_hdr(admin, "admin"))
    assert resp.status_code == 200, resp.text
    by_actor = {r["actor_user_id"]: r for r in resp.json()["logs"]}
    assert by_actor[doctor]["actor_name"] == "Dr Kavya Menon"
    assert by_actor["GONE-USER-404"]["actor_name"] is None


def test_flight_recorder_timeline_and_detail_carry_the_actor_name(admin):
    doctor = _user("doctor", "Dr Farah Khan")
    pid = f"PAAN{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
                   (pid, "Actor Name Patient", f"MRN-{pid}"))
    log_audit_event(correlation_id=f"aan-{uuid.uuid4().hex}", event_category="clinical_safety",
                    action_type="actor_name_probe", outcome="success",
                    actor_user_id=doctor, actor_role="doctor", patient_id=pid,
                    summary_message="actor name probe")
    resp = client.get("/api/flight-recorder/timeline", params={"patient_id": pid},
                      headers=_hdr(admin, "admin"))
    assert resp.status_code == 200, resp.text
    audit = [e for e in resp.json()["events"] if e["source"] == "audit"]
    assert audit and audit[-1]["actor"] == doctor
    assert audit[-1]["actor_name"] == "Dr Farah Khan"

    detail = client.get(f"/api/flight-recorder/event/audit/{audit[-1]['id']}",
                        headers=_hdr(admin, "admin"))
    assert detail.status_code == 200, detail.text
    assert detail.json()["event"]["actor_name"] == "Dr Farah Khan"


def test_chain_still_verifies_after_names_are_read(admin):
    resp = client.get("/api/flight-recorder/verify", headers=_hdr(admin, "admin"))
    assert resp.status_code == 200
    assert "ok" in resp.json()


def test_the_verify_queue_names_who_ordered_and_who_checked():
    doctor = _user("doctor", "Dr Neel Shah")
    pharmacist = _user("pharmacist", "Priya Nair")
    pid = f"PAANQ{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, "Queue Names Patient", f"MRN-{pid}"))
        med_id = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source)
               VALUES (?, 'Paracetamol', 'REVIEW REQUIRED', 'Review', 'REVIEW REQUIRED',
                       'active', ?, 'llm_extracted')""", (pid, doctor)).lastrowid
    row = next(p for p in pharmacy.list_pending_verification() if p["id"] == med_id)
    assert row["ordered_by_name"] == "Dr Neel Shah"
    assert row["verified_by_name"] is None

    result = pharmacy.verify_medication(med_id, pharmacist, dosage="500mg",
                                        route="Oral", frequency="TDS")
    assert result["medication"]["verified_by"] == pharmacist
    assert result["medication"]["verified_by_name"] == "Priya Nair"
    assert result["medication"]["ordered_by_name"] == "Dr Neel Shah"
