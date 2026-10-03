"""The patient portal shows vitals as charted, with no colour and no verdict.

WHY THIS FILE EXISTS (found 2026-10-03, ruled by Samuel the same day). The
portal's /vitals-trends judged each vital against its own "normal range" table
and the card coloured the word with the status FILL tokens. Measured against
the NEWS2 scorer the ward actually runs:

  * SBP 90 scores 3 (red on the Vigil bar) and the portal told the patient
    "In normal range" (its band was `< 90` low);
  * HR 100, SBP 91, RR 20, Temp 37.0, SpO2 94 on oxygen is NEWS2 6 (elevated)
    and the portal called every one of them normal;
  * it compared the RAW value, so Temp 37.84 (charted 37.8) read "Above
    normal" and the card printed "73.9 bpm" for a heart rate charted as 74.

Any patient-facing verdict either disagrees with the ward or borrows NEWS2, a
clinician tool that needs the patient's SpO2 scale and oxygen status and still
misses aggregate-only risk. The patient can act on a colour only by talking to
the team. So the ruling: the route returns the number AS CHARTED (the same
`as_charted` the ward prints), when it was measured, and the series for a
neutral sparkline; no status, no normal range.
"""
from fastapi.testclient import TestClient

from database import db_session
from main import app

client = TestClient(app)

PATIENT_USER = "BVNV-PAT"
PATIENT_ID = "PAT-BVNV-1"

EARLIER_TS = "2026-08-20T08:00:00"
LATEST_TS = "2026-08-20T12:00:00"


def _trends(rows) -> dict:
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (PATIENT_USER, "Bridge No Verdict Test", "patient", hash_password("testpass123")),
        )
        db.execute(
            "INSERT OR IGNORE INTO patients (id, user_id, name, mrn) VALUES (?, ?, ?, ?)",
            (PATIENT_ID, PATIENT_USER, "Bridge No Verdict Test", "MRN-BVNV-1"),
        )
        # This module owns this patient's vitals.
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (PATIENT_ID,))
        for ts, hr, sbp, spo2, temp, rr in rows:
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                       spo2, temperature, respiratory_rate)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (PATIENT_ID, ts, hr, sbp, spo2, temp, rr),
            )
    resp = client.post(
        "/api/auth/login", json={"employee_id": PATIENT_USER, "password": "testpass123"}
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    resp = client.get(
        "/api/bridge/vitals-trends",
        headers={"Authorization": f"Bearer {resp.json()['token']}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["trends"]


def test_no_vital_carries_a_status_or_a_normal_range():
    """SBP 90 was "In normal range" to the patient and red on the ward."""
    trends = _trends([(LATEST_TS, 100.0, 90.0, 94.0, 37.0, 20.0)])
    for key, t in trends.items():
        for field in ("status", "normal_low", "normal_high"):
            assert field not in t, f"{key} still carries {field}: {t}"


def test_current_is_the_value_as_charted():
    """The number NEWS2 scored and the ward prints: half-up to chart precision."""
    trends = _trends([(LATEST_TS, 73.9, 89.6, 95.5, 37.85, 20.5)])
    assert trends["heart_rate"]["current"] == 74
    assert trends["systolic_bp"]["current"] == 90
    assert trends["spo2"]["current"] == 96
    assert trends["temperature"]["current"] == 37.9
    assert trends["respiratory_rate"]["current"] == 21


def test_series_is_charted_too():
    trends = _trends([
        (EARLIER_TS, 72.4, 120.5, 97.0, 36.84, 16.0),
        (LATEST_TS, 73.9, 118.0, 96.0, 37.05, 18.0),
    ])
    assert trends["heart_rate"]["series"] == [72, 74]
    assert trends["systolic_bp"]["series"] == [121, 118]
    assert trends["temperature"]["series"] == [36.8, 37.1]


def test_a_same_timestamp_correction_supplies_current():
    """Two rows at the same observation time: the later write (higher id) is the
    correction, so it is the one the patient sees."""
    trends = _trends([
        (LATEST_TS, 130.0, 120.0, 97.0, 37.0, 16.0),
        (LATEST_TS, 82.0, 120.0, 97.0, 37.0, 16.0),
    ])
    assert trends["heart_rate"]["current"] == 82
    assert trends["heart_rate"]["series"][-1] == 82


def test_an_unmeasured_vital_has_no_current_and_no_time():
    trends = _trends([(LATEST_TS, 80.0, None, None, None, None)])
    assert trends["respiratory_rate"]["current"] is None
    assert trends["respiratory_rate"]["measured_at"] is None
    assert trends["respiratory_rate"]["series"] == []
