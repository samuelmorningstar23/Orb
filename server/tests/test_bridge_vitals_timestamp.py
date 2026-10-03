"""Patient-facing vitals carry their measurement timestamp (DEMO_READINESS §14.3).

THE FINDING: the Bridge portal showed "73.9 bpm — In normal range" from readings
that were seven days old, with no indication of WHEN they were measured. The
VitalTrend contract from /api/bridge/vitals-trends carried no timestamp at all.

The invariant is HONESTY about time:
  1. every vital that has a reading carries `measured_at` — the timestamp of the
     row that supplied `current`;
  2. vitals rows are sparse, so `measured_at` is per-vital: a vital whose latest
     value came from an OLDER row must report that older row's timestamp, never
     the newest row's (and never "now");
  3. a vital with no readings reports `measured_at` None — absence, not a guess.
"""
from fastapi.testclient import TestClient

from database import db_session
from main import app

client = TestClient(app)

PATIENT_USER = "BVTS-PAT"
PATIENT_ID = "PAT-BVTS-1"

# Distinctive timestamps so assertions cannot be satisfied by another module's
# fixture rows in the shared session DB.
OLDER_TS = "2026-08-01T08:15:00"
NEWER_TS = "2026-08-09T14:32:00"


def _setup_patient_with_sparse_vitals():
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (PATIENT_USER, "Bridge Vitals TS Test", "patient", hash_password("testpass123")),
        )
        db.execute(
            "INSERT OR IGNORE INTO patients (id, user_id, name, mrn) VALUES (?, ?, ?, ?)",
            (PATIENT_ID, PATIENT_USER, "Bridge Vitals TS Test", "MRN-BVTS-1"),
        )
        # Idempotent within the session: this module owns this patient's vitals.
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (PATIENT_ID,))
        # Older row: full set except respiratory_rate (never measured at all).
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, spo2, temperature)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (PATIENT_ID, OLDER_TS, 72.0, 121.0, 97.0, 36.8),
        )
        # Newer row: heart rate only — the sparse-row case.
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate) VALUES (?, ?, ?)",
            (PATIENT_ID, NEWER_TS, 80.0),
        )


def _patient_trends() -> dict:
    _setup_patient_with_sparse_vitals()
    resp = client.post(
        "/api/auth/login", json={"employee_id": PATIENT_USER, "password": "testpass123"}
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    token = resp.json()["token"]
    resp = client.get(
        "/api/bridge/vitals-trends", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["trends"]


def test_every_measured_vital_carries_measured_at():
    trends = _patient_trends()
    for key in ("heart_rate", "systolic_bp", "spo2", "temperature"):
        assert "measured_at" in trends[key], f"{key} trend has no measured_at field"
        assert trends[key]["measured_at"] is not None, f"{key} has a reading but no timestamp"


def test_measured_at_is_the_timestamp_of_the_row_that_supplied_current():
    trends = _patient_trends()
    # Heart rate's latest value came from the newer (sparse) row.
    assert trends["heart_rate"]["current"] == 80.0
    assert trends["heart_rate"]["measured_at"] == NEWER_TS
    # SpO2's latest value came from the OLDER row — its timestamp must be the
    # older one, not the newest row's and not the request time.
    assert trends["spo2"]["current"] == 97.0
    assert trends["spo2"]["measured_at"] == OLDER_TS
    assert trends["systolic_bp"]["measured_at"] == OLDER_TS
    assert trends["temperature"]["measured_at"] == OLDER_TS


def test_unmeasured_vital_reports_no_timestamp():
    trends = _patient_trends()
    assert trends["respiratory_rate"]["current"] is None
    # No "status" since 2026-10-03: the portal gives no verdict on vitals
    # (test_bridge_vitals_no_verdict.py).
    assert "status" not in trends["respiratory_rate"]
    assert trends["respiratory_rate"]["measured_at"] is None
