"""The ward clerk's own patient list (Orb Ward Record, 9 October 2026).

GET /api/nurse/transcribe/patients feeds the clerk's Observations screen. It
used to read the bed board (GET /api/adt/board), which answers only while the
beds module (ORB_ADT) is switched on; this list works with it off. It names
her own department's admitted patients, with ward, bed and when the stay began,
and nothing clinical. Scope comes from the signed in user, as on the board.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from routes.nurse_routes import router as nurse_router
from services.clinical_time import parse_to_utc

DEPT_A = "Clerk List Medicine"
DEPT_B = "Clerk List Surgery"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role, department):
    uid = f"WCL{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Clerk list {role}", role, department),
        )
    return uid


def _mk_patient(department, *, bed="B-1", admitted_at=None, status="active"):
    pid = f"WCLP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WCLADM', 'Clerk list admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, ?, 'CL-1', 70, 'F', 'Secret diagnosis', ?, ?)""",
            (pid, f"Clerk List {pid}", f"UHID-{pid}", bed,
             "admitted" if status == "active" else "discharged",
             datetime.now(timezone.utc).isoformat()),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
            "admitted_by, admitted_at) VALUES (?, ?, ?, 'CL-1', ?, ?, 'WCLADM', ?)",
            (f"ADM-{pid}", pid, department, bed, status, admitted_at),
        )
    return pid


def test_the_clerk_sees_her_departments_admitted_patients_with_bed_and_admission_time(client):
    arrived = (datetime.now(timezone.utc) - timedelta(hours=30)).replace(microsecond=0)
    mine = _mk_patient(DEPT_A, bed="B-7", admitted_at=arrived.isoformat())
    other_ward = _mk_patient(DEPT_B)
    gone = _mk_patient(DEPT_A, status="discharged")
    clerk = _mk_user("ward_clerk", DEPT_A)

    r = client.get("/api/nurse/transcribe/patients", headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["department"] == DEPT_A
    ids = {p["patient_id"] for p in body["patients"]}
    assert mine in ids
    assert other_ward not in ids
    assert gone not in ids
    (row,) = [p for p in body["patients"] if p["patient_id"] == mine]
    assert row["bed"] == "B-7"
    assert row["ward"] == "CL-1"
    assert row["mrn"] == f"UHID-{mine}"
    assert parse_to_utc(row["admitted_at"]) == arrived
    # Nothing clinical travels on this list.
    assert set(row) == {"patient_id", "name", "mrn", "ward", "bed", "admitted_at"}
    assert "Secret diagnosis" not in r.text


def test_an_admission_written_before_the_beds_module_starts_when_it_was_written(client):
    pid = _mk_patient(DEPT_A, admitted_at=None)
    clerk = _mk_user("ward_clerk", DEPT_A)
    with db_session() as db:
        written = db.execute("SELECT created_at FROM admissions WHERE patient_id = ?",
                             (pid,)).fetchone()[0]
    r = client.get("/api/nurse/transcribe/patients", headers=_auth(clerk, "ward_clerk"))
    (row,) = [p for p in r.json()["patients"] if p["patient_id"] == pid]
    assert parse_to_utc(row["admitted_at"]) == parse_to_utc(written)


def test_it_answers_with_the_beds_module_switched_off(client, monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False, raising=False)
    assert not config.switch_enabled("adt")
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    r = client.get("/api/nurse/transcribe/patients", headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200, r.text
    assert pid in {p["patient_id"] for p in r.json()["patients"]}


@pytest.mark.parametrize("role", ["nurse", "doctor", "admin", "pharmacist"])
def test_only_an_account_that_transcribes_may_read_it(client, role):
    uid = _mk_user(role, DEPT_A)
    r = client.get("/api/nurse/transcribe/patients", headers=_auth(uid, role))
    assert r.status_code == 403


def test_a_query_parameter_cannot_widen_her_scope(client):
    other = _mk_patient(DEPT_B)
    clerk = _mk_user("ward_clerk", DEPT_A)
    r = client.get(f"/api/nurse/transcribe/patients?department={DEPT_B}&all_departments=true",
                   headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200
    assert other not in {p["patient_id"] for p in r.json()["patients"]}


def test_every_read_is_audited(client):
    clerk = _mk_user("ward_clerk", DEPT_A)
    client.get("/api/nurse/transcribe/patients", headers=_auth(clerk, "ward_clerk"))
    with db_session() as db:
        n = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'transcribe_patients_viewed' "
            "AND actor_user_id = ? AND event_category = 'phi_access'", (clerk,)).fetchone()[0]
    assert n == 1
