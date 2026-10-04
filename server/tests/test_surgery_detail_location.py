"""GET /api/surgery/{id} says where the patient is.

The Surgical Suite's case header reads `Bed {bed}`, but the detail query
selected no p.bed (nor p.ward or p.admission_date), so every case read
"Bed undefined". `risk_drivers` came back as the stored JSON text where the
client expects a list. The service-layer detail (surgery_service) already
joined all three and parsed the drivers; the route now does the same.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)


@pytest.fixture()
def doctor():
    uid = f"SDDOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'doctor', 'x')",
            (uid, f"Doctor {uid}"),
        )
    return uid


@pytest.fixture()
def made():
    """Patients and cases this module creates, removed afterwards."""
    pids = []
    yield pids
    with db_session() as db:
        for pid in pids:
            db.execute("DELETE FROM surgery_team_assignments WHERE surgery_id IN "
                       "(SELECT id FROM surgeries WHERE patient_id = ?)", (pid,))
            db.execute("DELETE FROM surgeries WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM risk_scores WHERE patient_id = ?", (pid,))
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _case(made, doctor, bed, ward):
    pid = f"PATSD{uuid.uuid4().hex[:6].upper()}"
    sid = f"SURG{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, admission_date) "
            "VALUES (?, 'Located Patient', ?, ?, ?, 'Perforated viscus', '2026-10-03 09:30:00')",
            (pid, f"MRN-{uuid.uuid4().hex[:8]}", ward, bed),
        )
        db.execute(
            """INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis,
                                      scheduled_date, scheduled_time, status)
               VALUES (?, ?, ?, 'Laparotomy', 'Perforation', '2026-08-14', '09:00', 'scheduled')""",
            (sid, pid, doctor),
        )
        db.execute(
            "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) "
            "VALUES (?, ?, 'Surgeon', 'confirmed')", (sid, doctor),
        )
    made.append(pid)
    return pid, sid


def _detail(sid, doctor):
    r = client.get(f"/api/surgery/{sid}",
                   headers={"Authorization": f"Bearer {create_token(doctor, 'doctor')}"})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_case_detail_carries_bed_ward_and_admission(doctor, made):
    _, sid = _case(made, doctor, bed="A-4", ward="Ward A")

    body = _detail(sid, doctor)

    assert body["bed"] == "A-4"
    assert body["ward"] == "Ward A"
    assert body["admission_date"] == "2026-10-03 09:30:00"


def test_a_patient_with_no_bed_is_sent_as_null_not_missing(doctor, made):
    _, sid = _case(made, doctor, bed=None, ward=None)

    body = _detail(sid, doctor)

    assert "bed" in body and body["bed"] is None
    assert "ward" in body and body["ward"] is None


def test_risk_drivers_arrive_as_a_list(doctor, made):
    pid, sid = _case(made, doctor, bed="A-5", ward="Ward A")
    drivers = ["RR 24 (+2)", "SpO2 93% (+2)"]
    with db_session() as db:
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level, drivers) VALUES (?, 5, 'elevated', ?)",
            (pid, json.dumps(drivers)),
        )

    assert _detail(sid, doctor)["risk_drivers"] == drivers
