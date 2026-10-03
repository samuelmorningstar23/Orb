"""F043: simulated vitals are labelled, and they do not count as care delivered.

`vitals.source` was never serialised, so nothing on the chart could say a row
was synthetic, and the nurse board anchored the obs-due clock on the newest row
of any kind. Under simulation a reading every 5 s re-anchored it, so "obs
overdue" never fired for any patient.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from routes.vigil_routes import router as vigil_router
from services import nurse_shift_service as nurse

DEPT = "Provenance Test Ward"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture()
def patient():
    pid = f"PATPV{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status) "
            "VALUES (?, ?, ?, 'P-1', 'PV', 58, 'F', 'Cellulitis', 'admitted')",
            (pid, f"Provenance {pid}", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('PVTESTADM', 'Provenance Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'PVTESTADM')",
            (pid, DEPT),
        )
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))


def _vitals(pid: str, when: datetime, source: str) -> None:
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                                   spo2, temperature, respiratory_rate, consciousness, on_oxygen, source)
               VALUES (?, ?, 72, 122, 78, 98, 36.8, 16, 'alert', 0, ?)""",
            (pid, _iso(when), source),
        )


def _card(pid: str) -> dict:
    board = nurse.build_board(DEPT)
    return next(c for c in board["patients"] if c["patient_id"] == pid)


def test_a_simulated_reading_does_not_reset_the_obs_due_clock(patient):
    now = datetime.now(timezone.utc)
    # A stable set a nurse took 13 hours ago (12-hourly band): overdue by an hour.
    _vitals(patient, now - timedelta(hours=13), "nurse_manual")
    # The simulator wrote a reading a second ago.
    _vitals(patient, now - timedelta(seconds=1), "simulated")

    card = _card(patient)
    assert card["obs_overdue"] is True, "a simulated row re-anchored the obs-due clock"
    assert card["last_obs_source"] == "nurse_manual"
    assert card["vitals_source"] == "simulated"


def test_only_simulated_readings_means_no_observation_taken(patient):
    _vitals(patient, datetime.now(timezone.utc), "simulated")
    card = _card(patient)
    assert card["last_obs_at"] is None
    assert card["last_obs_source"] is None
    assert card["obs_overdue"] is True


def test_a_recent_nurse_set_is_not_overdue(patient):
    _vitals(patient, datetime.now(timezone.utc) - timedelta(minutes=5), "nurse_manual")
    card = _card(patient)
    assert card["obs_overdue"] is False
    assert card["vitals_source"] == "nurse_manual"


def _client_and_auth():
    app = FastAPI()
    app.include_router(vigil_router)
    uid = f"DPV{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, 'doctor', 'x', ?)", (uid, "Dr Provenance", DEPT))
    return TestClient(app), {"Authorization": f"Bearer {create_token(uid, 'doctor')}"}


def test_the_chart_serves_each_rows_source_and_flags_a_simulated_latest_row(patient):
    now = datetime.now(timezone.utc)
    _vitals(patient, now - timedelta(minutes=30), "nurse_manual")
    _vitals(patient, now - timedelta(seconds=1), "simulated")
    client, hdr = _client_and_auth()

    body = client.get(f"/api/vigil/patients/{patient}", headers=hdr).json()
    assert [v["source"] for v in body["vitals"][:2]] == ["simulated", "nurse_manual"]
    assert body["simulated_vitals"] is True

    rows = client.get(f"/api/vigil/patients/{patient}/vitals?hours=6", headers=hdr).json()
    assert {r["source"] for r in rows} == {"simulated", "nurse_manual"}


def test_the_chart_does_not_flag_a_real_latest_row(patient):
    _vitals(patient, datetime.now(timezone.utc), "nurse_manual")
    client, hdr = _client_and_auth()
    body = client.get(f"/api/vigil/patients/{patient}", headers=hdr).json()
    assert body["simulated_vitals"] is False


def test_the_feed_mode_endpoint_says_whether_the_simulator_is_writing():
    client, hdr = _client_and_auth()
    from config import INGESTION_MODE
    body = client.get("/api/vigil/vitals-feed", headers=hdr).json()
    assert body == {"mode": INGESTION_MODE, "simulated": INGESTION_MODE != "ingestion"}
