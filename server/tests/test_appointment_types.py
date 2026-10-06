"""Appointments: a fixed type list, the band from the latest vitals, and the
sickest patient first within a day (2026-10-06).

  * POST /api/appointments took any text as the type, so the same visit was
    spelled four ways and nothing could count the post-discharge checks due.
    A clinician now books one of four types; an unknown type is refused, and an
    old free-text row still reads (as "other", with its own text as the label).
  * The badge came off risk_scores, which the Vigil board stopped reading on 3
    October because it goes stale. It is now the NEWS2 of the latest vitals,
    with the number.
  * Within a day the list was ordered by time then name. It is now highest
    NEWS2 first, then time.
"""
from datetime import timedelta
import uuid

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services.appointment_types import parse_type, type_key, type_label
from services.hospital_time import hospital_today

client = TestClient(app)

DOCTOR = "APTY-DOC"
TAG = "APTY"


def _auth(user_id: str = DOCTOR, role: str = "doctor") -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _day(offset: int) -> str:
    return (hospital_today() + timedelta(days=offset)).isoformat()


def _patient(name: str, *, vitals: dict | None = None) -> str:
    pid = f"{TAG}-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, 'Appt Types Doctor', 'doctor', 'x')",
            (DOCTOR,),
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status)
               VALUES (?, ?, ?, 60, 'F', 'GM', 'B-1', 'Test', 'discharged')""",
            (pid, name, f"MRN-{pid}"),
        )
        if vitals:
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                       spo2, temperature, respiratory_rate, consciousness, on_oxygen)
                   VALUES (?, '2026-10-06T08:00:00+00:00', ?, ?, 70, ?, ?, ?, 'alert', 0)""",
                (pid, vitals["hr"], vitals["sbp"], vitals["spo2"], vitals["temp"], vitals["rr"]),
            )
    return pid


def _book(pid: str, day: str, time: str, appointment_type: str = "Follow-up") -> None:
    with db_session() as db:
        db.execute(
            """INSERT INTO appointments (id, patient_id, booked_by_user_id, scheduled_date,
                   scheduled_time, appointment_type, reason, status, origin)
               VALUES (?, ?, ?, ?, ?, ?, 'Review', 'booked', 'staff')""",
            (str(uuid.uuid4()), pid, DOCTOR, day, time, appointment_type),
        )


# ── The list itself ──────────────────────────────────────────────────────────

def test_the_list_takes_keys_and_labels_and_reads_old_text_as_other():
    assert parse_type("post_discharge_check") == "post_discharge_check"
    assert parse_type("Post-discharge check") == "post_discharge_check"
    assert parse_type("Follow-up") == "follow_up"
    assert parse_type(None) == "follow_up"
    assert parse_type("Clinical Review") is None
    assert type_key("Clinical Review") == "other"
    assert type_label("Clinical Review") == "Clinical Review"
    assert type_key("Medication review") == "medication_review"
    assert type_label("medication_review") == "Medication review"


# ── The booking route ────────────────────────────────────────────────────────

def test_the_route_refuses_an_unknown_type():
    pid = _patient("Unknown Type")
    r = client.post(
        "/api/appointments",
        json={"patient_id": pid, "scheduled_date": _day(3), "scheduled_time": "10:00",
              "appointment_type": "Coffee with the consultant"},
        headers=_auth(),
    )
    assert r.status_code == 422, r.text
    assert "post_discharge_check" in r.json()["detail"]


def test_the_route_books_a_listed_type_and_says_its_key():
    pid = _patient("Listed Type")
    r = client.post(
        "/api/appointments",
        json={"patient_id": pid, "scheduled_date": _day(3), "scheduled_time": "10:00",
              "appointment_type": "post_discharge_check", "reason": "Wound check after discharge"},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["appointment_type"] == "Post-discharge check"
    assert body["appointment_type_key"] == "post_discharge_check"
    assert body["reason"] == "Wound check after discharge"


def test_an_old_free_text_row_still_reads():
    pid = _patient("Old Row")
    _book(pid, _day(2), "09:00", "Clinical Review")
    r = client.get(f"/api/appointments?patient_id={pid}", headers=_auth())
    assert r.status_code == 200, r.text
    (row,) = r.json()["appointments"]
    assert row["appointment_type_key"] == "other"
    assert row["appointment_type_label"] == "Clinical Review"


# ── The day list ─────────────────────────────────────────────────────────────

def test_the_badge_is_the_latest_vitals_and_the_sickest_comes_first():
    day = _day(1)
    calm = _patient("Calm Early", vitals={"hr": 72, "sbp": 124, "spo2": 98, "temp": 36.8, "rr": 16})
    sick = _patient("Sick Late", vitals={"hr": 125, "sbp": 95, "spo2": 91, "temp": 38.6, "rr": 26})
    none = _patient("Never Scored")
    _book(calm, day, "09:00")
    _book(sick, day, "15:00", "Post-discharge check")
    _book(none, day, "08:00")

    from services.forecast_service import get_appointments
    rows = [r for r in get_appointments(days_ahead=3) if r["patient_id"] in {calm, sick, none}]
    assert [r["patient_id"] for r in rows] == [sick, calm, none]
    by_id = {r["patient_id"]: r for r in rows}
    assert by_id[sick]["acuity"] == "critical"
    assert by_id[sick]["news2"] >= 7
    assert by_id[sick]["type_key"] == "post_discharge_check"
    assert by_id[calm]["news2"] == 0 and by_id[calm]["acuity"] == "stable"
    # Nothing recorded is not stable, and has no number.
    assert by_id[none]["acuity"] is None and by_id[none]["news2"] is None
