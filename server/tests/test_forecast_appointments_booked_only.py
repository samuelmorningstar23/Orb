"""The Appointments panel shows bookings, and only the caller's.

THE FINDING (F013). services/forecast_service.get_appointments sampled three to
five admitted patients a day with a date-seeded RNG and gave each a random type,
reason and clock time, including "Post-Discharge Review" for patients still in a
bed. The panel rendered those rows like bookings and counted them in every tile.
The route was also unscoped and unaudited: any forecast.read holder saw every
booking in the hospital and left no trace.

The invariants:
  1. Only rows from the appointments table come back. Nothing synthesised.
  2. A ward-scoped caller sees their own ward's patients, plus pure outpatients
     (no admission at all) whose booking names their department.
  3. The read is audited as a PHI read.
  4. "The next 7 days" is seven hospital-local dates, today first.
"""
from datetime import timedelta
import uuid

from fastapi.testclient import TestClient

from database import db_session
from main import app
from services.hospital_time import hospital_today

client = TestClient(app)

DEPT_A = "ApptScope Cardiology"
DEPT_B = "ApptScope Oncology"
DOC_A = "APSC-DOC-A"
GOV = "APSC-GOV"
TAG = "APSC"


def _today(offset: int = 0) -> str:
    return (hospital_today() + timedelta(days=offset)).isoformat()


def _setup() -> dict:
    from auth import hash_password
    pw = hash_password("testpass123")
    ids = {}
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Appt Scope Doctor', 'doctor', ?, ?)", (DOC_A, DEPT_A, pw),
        )
        # No department: the hospital-wide governance convention.
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Appt Scope Governance', 'doctor', NULL, ?)", (GOV, pw),
        )
        db.execute("DELETE FROM appointments WHERE patient_id LIKE ?", (f"{TAG}-%",))

        def patient(key: str, *, admitted_in: str | None, status: str = "admitted") -> str:
            pid = f"{TAG}-{key}-{uuid.uuid4().hex[:6].upper()}"
            db.execute(
                """INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status)
                   VALUES (?, ?, ?, 60, 'F', 'Bay 1', 'B-1', 'Test diagnosis', ?)""",
                (pid, f"Appt {key}", f"MRN-{pid}", status),
            )
            if admitted_in:
                db.execute(
                    "INSERT INTO admissions (id, patient_id, department, status, admitted_by) "
                    "VALUES (?, ?, ?, 'active', ?)",
                    (f"ADM-{pid}", pid, admitted_in, DOC_A),
                )
            ids[key] = pid
            return pid

        patient("ward_a", admitted_in=DEPT_A)
        patient("ward_b", admitted_in=DEPT_B)
        # patients.status has no outpatient value; what makes these pure
        # outpatients is that no admission row exists for them.
        patient("outpt_a", admitted_in=None, status="discharged")
        patient("outpt_b", admitted_in=None, status="discharged")
        # Admitted with no booking at all: the old synthesis would have
        # invented a visit for exactly this patient.
        patient("unbooked", admitted_in=DEPT_A)

        def book(key: str, day: str, time: str, department: str | None = None) -> None:
            db.execute(
                """INSERT INTO appointments (id, patient_id, booked_by_user_id, scheduled_date,
                       scheduled_time, appointment_type, reason, status, origin, department)
                   VALUES (?, ?, ?, ?, ?, 'Follow-up', 'Review', 'booked', 'staff', ?)""",
                (str(uuid.uuid4()), ids[key], DOC_A, day, time, department),
            )

        book("ward_a", _today(1), "10:00")
        book("ward_b", _today(1), "10:30")
        book("outpt_a", _today(2), "11:00", DEPT_A)
        book("outpt_b", _today(2), "11:30", DEPT_B)
        # Horizon edges: six days out is inside "the next 7 days", seven is not.
        book("ward_a", _today(6), "09:00")
        book("ward_a", _today(7), "09:00")
    return ids


def _headers(user_id: str) -> dict:
    resp = client.post("/api/auth/login",
                       json={"employee_id": user_id, "password": "testpass123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _mine(rows: list, ids: dict) -> list:
    wanted = set(ids.values())
    return [r for r in rows if r["patient_id"] in wanted]


def test_only_booked_rows_are_returned_never_a_synthesised_schedule():
    ids = _setup()
    resp = client.get("/api/forecast/appointments", headers=_headers(GOV))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert all(r["source"] == "booked" for r in rows), (
        "A synthesised row reached the schedule: nobody booked it"
    )
    assert not any(r.get("source") == "predicted" for r in rows)
    mine = _mine(rows, ids)
    assert ids["unbooked"] not in {r["patient_id"] for r in mine}, (
        "An admitted patient with no booking appeared on the appointment list"
    )
    with db_session() as db:
        booked_total = db.execute(
            """SELECT COUNT(*) AS c FROM appointments
                WHERE status = 'booked' AND scheduled_date >= ? AND scheduled_date < ?""",
            (_today(0), _today(7)),
        ).fetchone()["c"]
    assert len(rows) == booked_total, "Every row must be a booking in the table, and nothing else"


def test_the_horizon_is_seven_local_dates_starting_today():
    ids = _setup()
    rows = _mine(client.get("/api/forecast/appointments", headers=_headers(GOV)).json(), ids)
    dates = {r["date"] for r in rows if r["patient_id"] == ids["ward_a"]}
    assert _today(6) in dates
    assert _today(7) not in dates


def test_a_ward_scoped_caller_sees_their_ward_and_their_outpatients_only():
    ids = _setup()
    rows = _mine(client.get("/api/forecast/appointments", headers=_headers(DOC_A)).json(), ids)
    seen = {r["patient_id"] for r in rows}
    assert ids["ward_a"] in seen
    assert ids["outpt_a"] in seen, "An outpatient booked into this clinic must reach it"
    assert ids["ward_b"] not in seen, "Another ward's inpatient leaked onto this list"
    assert ids["outpt_b"] not in seen, "Another clinic's outpatient leaked onto this list"


def test_the_read_is_audited_as_a_phi_read():
    ids = _setup()
    with db_session() as db:
        before = db.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM audit_logs"
        ).fetchone()["m"]
    resp = client.get("/api/forecast/appointments", headers=_headers(DOC_A))
    assert resp.status_code == 200
    with db_session() as db:
        row = db.execute(
            """SELECT event_category, actor_user_id, metadata_json FROM audit_logs
                WHERE id > ? AND action_type = 'appointments_schedule_viewed'
                ORDER BY id DESC LIMIT 1""",
            (before,),
        ).fetchone()
    assert row is not None, "Reading a list of named patients left no audit trace"
    assert row["event_category"] == "phi_access"
    assert row["actor_user_id"] == DOC_A
    assert ids["ward_a"] in (row["metadata_json"] or "")


def test_acuity_is_never_defaulted_to_stable():
    ids = _setup()
    rows = _mine(client.get("/api/forecast/appointments", headers=_headers(GOV)).json(), ids)
    # None of these patients has a risk score, so none may be called stable.
    assert rows and all(r["acuity"] is None for r in rows)
