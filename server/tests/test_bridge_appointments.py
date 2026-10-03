"""A patient's follow-up request must reach the people who can act on it.

THE FINDING (pilot dry run, 2026-08-14). A patient asking for a follow-up wrote
a row into `patient_appointment_requests`. No staff route, service or screen has
ever read that table. The API answered "Your appointment request has been
submitted. The care team will confirm a time." — and nobody was ever told. The
loop the PATIENT can see went nowhere.

The invariants:
  1. VISIBILITY — a request a patient makes appears on the appointment surface
     staff actually work from (/api/forecast/appointments, which feeds the
     Appointments panel) and in the clinician list for that patient. This is the
     test that fails if the write ever drifts back into a private table.
  2. HONEST WORDING — every claim in the patient-facing confirmation is either
     a fact about the stored row or a statement of what the system does. It must
     not promise a confirmation, a callback or a contact, because nothing in the
     product performs one.
  3. ONE ROW, TWO VIEWS — what staff do to the row (move, cancel) is what the
     patient sees, and a patient's cancellation removes it from the staff list.
  4. Ownership: a patient can only ever see or cancel their own.
"""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from database import db_session
from main import app

client = TestClient(app)

PATIENT_USER = "BAPT-PAT"
PATIENT_ID = "PAT-BAPT-1"
OTHER_PATIENT_USER = "BAPT-PAT2"
OTHER_PATIENT_ID = "PAT-BAPT-2"
CLINICIAN_USER = "BAPT-DOC"
WARD = "General Medicine"

TOMORROW = (date.today() + timedelta(days=1)).isoformat()
DAY_AFTER = (date.today() + timedelta(days=2)).isoformat()

# Phrases the confirmation may never contain: each one promises an action no
# code performs. This list is the wording contract.
FORBIDDEN_PROMISES = (
    "will confirm",
    "will be confirmed",
    "will contact",
    "will call",
    "will get back",
    "we will let you know",
    "has been submitted",
)


def _setup():
    """Two patients and a clinician. The clinician is seeded WITHOUT a
    department so ward scoping does not mask a visibility failure as a 404."""
    from auth import hash_password
    with db_session() as db:
        for uid, pid, name, mrn in (
            (PATIENT_USER, PATIENT_ID, "Bridge Appt Test", "MRN-BAPT-1"),
            (OTHER_PATIENT_USER, OTHER_PATIENT_ID, "Bridge Appt Other", "MRN-BAPT-2"),
        ):
            db.execute(
                "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
                (uid, name, "patient", hash_password("testpass123")),
            )
            db.execute(
                """INSERT OR IGNORE INTO patients
                       (id, user_id, name, mrn, age, sex, ward, bed, diagnosis, status)
                   VALUES (?, ?, ?, ?, 54, 'M', ?, 'B-1', 'Test diagnosis', 'admitted')""",
                (pid, uid, name, mrn, WARD),
            )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (CLINICIAN_USER, "Bridge Appt Doctor", "doctor", hash_password("testpass123")),
        )
        # Own this module's rows: a leftover from an earlier run would let a
        # visibility assertion pass on stale data.
        db.execute("DELETE FROM appointments WHERE patient_id IN (?, ?)",
                   (PATIENT_ID, OTHER_PATIENT_ID))


def _token(user_id: str) -> str:
    resp = client.post(
        "/api/auth/login", json={"employee_id": user_id, "password": "testpass123"}
    )
    assert resp.status_code == 200, f"Login failed for {user_id}: {resp.text}"
    return resp.json()["token"]


def _headers(user_id: str) -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def _request_followup(body: dict, user_id: str = PATIENT_USER):
    return client.post("/api/bridge/appointments", json=body, headers=_headers(user_id))


# ── 1. Visibility: the request reaches staff ─────────────────────────────────

def test_patient_request_appears_on_the_staff_appointment_queue():
    """The panel a clinician actually opens must show the request.

    /api/forecast/appointments is what src/pages/panels/AppointmentsPanel.tsx
    fetches. If the request is written anywhere that surface does not read, this
    fails — which is precisely the defect.
    """
    _setup()
    resp = _request_followup(
        {"requested_date": TOMORROW, "requested_time": "11:30",
         "department": "Cardiology", "reason": "Chest tightness on the stairs"}
    )
    assert resp.status_code == 200, resp.text
    appt_id = resp.json()["id"]

    queue = client.get("/api/forecast/appointments", headers=_headers(CLINICIAN_USER))
    assert queue.status_code == 200, queue.text
    mine = [
        a for a in queue.json()
        if a["patient_id"] == PATIENT_ID and a["date"] == TOMORROW and a["time"] == "11:30"
    ]
    assert mine, (
        "The patient's follow-up request is not on the staff appointment queue — "
        "the patient was told it was on the list and nobody can see it."
    )
    entry = mine[0]
    assert entry["source"] == "booked", "The request must be a real row, not a synthesized one"
    # Staff must be able to tell an ASK from an agreed slot, and see what for.
    assert "Patient request" in entry["type"]
    assert "Chest tightness on the stairs" in entry["reason"]

    # ...and in the per-patient clinician list, with the origin recorded.
    listing = client.get(
        f"/api/appointments?patient_id={PATIENT_ID}", headers=_headers(CLINICIAN_USER)
    )
    assert listing.status_code == 200, listing.text
    rows = [a for a in listing.json()["appointments"] if a["id"] == appt_id]
    assert rows, "The request is missing from the clinician's list for this patient"
    assert rows[0]["origin"] == "patient"
    assert rows[0]["department"] == "Cardiology"
    assert rows[0]["status"] == "booked"


def test_request_with_no_time_is_listed_and_says_so_on_both_sides():
    """A patient may name only a date. The slot is a placement, not a choice —
    and the staff row and the patient are told the same thing."""
    _setup()
    resp = _request_followup({"requested_date": DAY_AFTER, "reason": "Review my tablets"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["time_chosen_by_patient"] is False
    assert body["requested_time"], "A listed row always has a time; it must be reported back"

    listing = client.get(
        f"/api/appointments?patient_id={PATIENT_ID}", headers=_headers(CLINICIAN_USER)
    )
    row = [a for a in listing.json()["appointments"] if a["id"] == body["id"]][0]
    assert row["scheduled_time"] == body["requested_time"]
    assert "did not choose a time" in row["reason"].lower(), (
        "Staff are shown a time the patient never asked for, with nothing saying so"
    )
    assert "did not choose a time" in body["message"].lower()


# ── 2. The confirmation says only what happens ───────────────────────────────

def test_confirmation_wording_promises_nothing_the_system_does_not_do():
    _setup()
    body = _request_followup(
        {"requested_date": TOMORROW, "requested_time": "09:45"}
    ).json()
    message = body["message"].lower()
    for phrase in FORBIDDEN_PROMISES:
        assert phrase not in message, (
            f"The patient is promised {phrase!r}; nothing in the system performs it."
        )
    # It must say the true thing instead: where the request went, and that the
    # time is not agreed.
    assert TOMORROW in body["message"]
    assert "09:45" in body["message"]
    assert "appointment list" in message
    assert "agreed" in message


def test_reported_status_is_the_row_the_database_actually_holds():
    """The response may not invent a state. It used to answer status='requested'
    for a row in a table nobody read; the status returned must be the status a
    staff surface would read back."""
    _setup()
    body = _request_followup({"requested_date": TOMORROW, "requested_time": "10:15"}).json()
    with db_session() as db:
        row = db.execute(
            "SELECT status, scheduled_date, scheduled_time FROM appointments WHERE id = ?",
            (body["id"],),
        ).fetchone()
    assert row is not None, "The confirmation named an id that is not in the appointments table"
    assert body["status"] == row["status"]
    assert body["requested_date"] == row["scheduled_date"]
    assert body["requested_time"] == row["scheduled_time"]


def test_patient_list_describes_a_request_as_unagreed_and_a_booking_as_scheduled():
    _setup()
    asked = _request_followup({"requested_date": TOMORROW, "requested_time": "14:00"}).json()
    # A slot the hospital itself put in the diary, for the same patient.
    booked = client.post(
        "/api/appointments",
        json={"patient_id": PATIENT_ID, "scheduled_date": DAY_AFTER,
              "scheduled_time": "15:00", "appointment_type": "Follow-up"},
        headers=_headers(CLINICIAN_USER),
    )
    assert booked.status_code == 200, booked.text

    mine = client.get("/api/bridge/appointments", headers=_headers(PATIENT_USER)).json()
    by_id = {a["id"]: a for a in mine["appointments"]}
    assert by_id[asked["id"]]["requested_by_patient"] is True
    assert "nobody has agreed" in by_id[asked["id"]]["status_note"].lower()
    assert by_id[booked.json()["id"]]["requested_by_patient"] is False
    assert "scheduled by the care team" in by_id[booked.json()["id"]]["status_note"].lower()


# ── 3. One row, two views ────────────────────────────────────────────────────

def test_staff_moving_the_slot_is_what_the_patient_sees():
    _setup()
    asked = _request_followup({"requested_date": TOMORROW, "requested_time": "12:00"}).json()
    with db_session() as db:
        db.execute(
            "UPDATE appointments SET scheduled_time = '16:30' WHERE id = ?", (asked["id"],)
        )
    mine = client.get("/api/bridge/appointments", headers=_headers(PATIENT_USER)).json()
    row = [a for a in mine["appointments"] if a["id"] == asked["id"]][0]
    assert row["requested_time"] == "16:30", (
        "The ward moved the appointment and the patient is still shown the old time"
    )


def test_patient_cancelling_removes_it_from_the_staff_queue():
    _setup()
    asked = _request_followup({"requested_date": TOMORROW, "requested_time": "13:15"}).json()
    cancel = client.delete(
        f"/api/bridge/appointments/{asked['id']}", headers=_headers(PATIENT_USER)
    )
    assert cancel.status_code == 200, cancel.text

    queue = client.get("/api/forecast/appointments", headers=_headers(CLINICIAN_USER)).json()
    still_listed = [
        a for a in queue
        if a["patient_id"] == PATIENT_ID and a["date"] == TOMORROW and a["time"] == "13:15"
    ]
    assert not still_listed, "A cancelled request is still on the ward's list"


def test_patient_cannot_cancel_someone_elses_appointment():
    _setup()
    asked = _request_followup({"requested_date": TOMORROW, "requested_time": "17:00"}).json()
    resp = client.delete(
        f"/api/bridge/appointments/{asked['id']}", headers=_headers(OTHER_PATIENT_USER)
    )
    assert resp.status_code == 404, "Ownership leak — or a 403 that confirms the id exists"


def test_patient_list_shows_only_their_own():
    _setup()
    asked = _request_followup({"requested_date": TOMORROW, "requested_time": "18:00"}).json()
    other = client.get("/api/bridge/appointments", headers=_headers(OTHER_PATIENT_USER)).json()
    assert asked["id"] not in {a["id"] for a in other["appointments"]}


# ── 4. Validation still holds on the shared table ────────────────────────────

def test_a_request_cannot_be_parked_in_the_past_or_beyond_the_horizon():
    _setup()
    past = _request_followup(
        {"requested_date": (date.today() - timedelta(days=1)).isoformat()}
    )
    assert past.status_code == 400
    far = _request_followup(
        {"requested_date": (date.today() + timedelta(days=900)).isoformat()}
    )
    assert far.status_code == 400, (
        "A request 900 days out sits on a list no clinician will ever open"
    )


def test_double_tap_does_not_create_two_slots():
    _setup()
    first = _request_followup({"requested_date": TOMORROW, "requested_time": "08:30"}).json()
    second = _request_followup({"requested_date": TOMORROW, "requested_time": "08:30"}).json()
    assert first["id"] == second["id"]
    assert "nothing new was added" in second["message"].lower()


def test_asking_for_a_slot_the_team_already_booked_is_not_called_unagreed():
    """The reply describes the row that exists. Resolving onto a team booking and
    still saying "nobody has agreed that time" is the same untruth in reverse."""
    _setup()
    booked = client.post(
        "/api/appointments",
        json={"patient_id": PATIENT_ID, "scheduled_date": TOMORROW,
              "scheduled_time": "07:45", "appointment_type": "Follow-up"},
        headers=_headers(CLINICIAN_USER),
    )
    assert booked.status_code == 200, booked.text
    asked = _request_followup({"requested_date": TOMORROW, "requested_time": "07:45"}).json()
    assert asked["id"] == booked.json()["id"]
    assert asked["requested_by_patient"] is False
    assert "nobody has agreed" not in asked["message"].lower()
    assert "already has an appointment" in asked["message"].lower()


# ── 5. The requests already stranded in the dead table ───────────────────────

def test_migration_carries_stranded_requests_onto_the_staff_list():
    """Migration 28 is the only thing that keeps the promise already made to
    patients who asked BEFORE the fix. Their rows must arrive on the staff list,
    keep their id (so the patient's cancel link still works) and keep a
    cancellation they had already made."""
    from migrations import _m28_patient_appointment_requests_bridge

    _setup()
    with db_session() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS patient_appointment_requests (
                   id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, requested_date TEXT NOT NULL,
                   requested_time TEXT, department TEXT, reason TEXT,
                   status TEXT NOT NULL DEFAULT 'requested',
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
        )
        db.execute("DELETE FROM patient_appointment_requests WHERE patient_id = ?", (PATIENT_ID,))
        db.execute(
            """INSERT INTO patient_appointment_requests
                   (id, patient_id, requested_date, requested_time, department, reason, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("APPT-STRANDED1", PATIENT_ID, TOMORROW, "10:45", "Cardiology",
             "Asked before the fix", "requested"),
        )
        db.execute(
            """INSERT INTO patient_appointment_requests
                   (id, patient_id, requested_date, requested_time, department, reason, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("APPT-STRANDED2", PATIENT_ID, DAY_AFTER, None, None,
             "Changed my mind", "cancelled"),
        )
        # Idempotent by construction (INSERT OR IGNORE on the request id), so
        # running it here — on a DB where it has already been applied — is safe.
        _m28_patient_appointment_requests_bridge(db)

    listing = client.get(
        f"/api/appointments?patient_id={PATIENT_ID}", headers=_headers(CLINICIAN_USER)
    ).json()["appointments"]
    by_id = {a["id"]: a for a in listing}

    carried = by_id.get("APPT-STRANDED1")
    assert carried, "A request a patient already made is still invisible to staff"
    assert carried["status"] == "booked"
    assert carried["origin"] == "patient"
    assert carried["scheduled_time"] == "10:45"
    assert carried["department"] == "Cardiology"

    closed = by_id.get("APPT-STRANDED2")
    assert closed, "A cancelled request vanished from the patient's own history"
    assert closed["status"] == "cancelled"
    assert "did not choose a time" in (closed["reason"] or "").lower()

    # And re-running changes nothing (the ledger is not the only thing keeping
    # this safe — the migration itself must be re-runnable).
    with db_session() as db:
        _m28_patient_appointment_requests_bridge(db)
        count = db.execute(
            "SELECT COUNT(*) c FROM appointments WHERE id IN ('APPT-STRANDED1','APPT-STRANDED2')"
        ).fetchone()["c"]
    assert count == 2
