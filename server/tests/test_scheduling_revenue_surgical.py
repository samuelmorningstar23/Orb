"""Phase-3 hardening — Scheduling, Revenue Integrity and Surgical.

Each test here pins a defect found by reading and exercising these three modules
against the clinical-safety bar the rest of the app is held to. The comment on
each test says what the behaviour was BEFORE the fix, so a regression is
recognisable as a regression rather than as a puzzling assertion.

Grouped:
  SURGICAL   — state-machine guards (start/acknowledge), team-roster validation,
               action-verb validation, unbounded schedule window, audit coverage,
               and the loop-blocking DB read inside the SSE risk generator.
  SCHEDULING — past-time booking, double-booking, terminal-state transitions,
               and the previously unreachable 'completed' status.
  REVENUE    — unknown-patient 404, NULL medication status, ICD-10-only money
               math, and the PHI-access audit event.
"""
import asyncio
import inspect
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import coding_service, terminology_service

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _seed_terminology():
    """coding_service only emits codes that exist in the `terminology` table, and
    conftest initialises the schema without seeding it (same as test_coding.py)."""
    terminology_service.seed_terminology()


# ── fixtures ──────────────────────────────────────────────────────────────────

def _mk_user(role: str, prefix: str) -> str:
    uid = f"{prefix}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
            (uid, f"{role.title()} {prefix}", role),
        )
    return uid


@pytest.fixture()
def doctor():
    return _mk_user("doctor", "H3DOC")


@pytest.fixture()
def nurse():
    return _mk_user("nurse", "H3NUR")


@pytest.fixture()
def patient():
    pid = f"PATH3{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis) "
            "VALUES (?, ?, ?, 'Ward A', 'A-1', 'Sepsis')",
            (pid, "Hardening Patient", f"MRN-{uuid.uuid4().hex[:8]}"),
        )
    yield pid
    # surgery_prep_requests.surgery_id is a FK to surgeries, and several other
    # modules in this suite wipe `surgeries` in their setup — a prep row left
    # pointing at one of our cases would make THEIR DELETE fail. Clean up after
    # ourselves rather than leaving cross-module debris.
    with db_session() as db:
        db.execute("DELETE FROM surgery_prep_requests WHERE patient_id = ?", (pid,))


def _auth(user_id: str, role: str = "doctor") -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _mk_surgery(patient_id, surgeon_id, status="scheduled", scheduled_date="2026-08-05"):
    sid = f"SURG{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis,
                                      scheduled_date, scheduled_time, status)
               VALUES (?, ?, ?, 'Laparotomy', 'Perforation', ?, '09:00', ?)""",
            (sid, patient_id, surgeon_id, scheduled_date, status),
        )
    return sid


def _mk_case_room(patient_id):
    cid = f"chan_{uuid.uuid4().hex[:12]}"
    with db_session() as db:
        # Surgical alerts post as sender_id='system', and messages.sender_id is a
        # FK to users. init_db seeds that row, but other modules in this suite
        # rebuild the users table (same reason conftest re-seeds D001/N001), so
        # re-ensure it here rather than depending on cross-module ordering.
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES ('system', 'Orb Sage', 'admin', NULL, 'system_hash')"
        )
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, 'Case Room', 'case_room', ?)",
            (cid, patient_id),
        )
    return cid


def _assign(surgery_id, user_id, status="alerted", role_name="Anesthetist"):
    with db_session() as db:
        db.execute(
            "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) "
            "VALUES (?, ?, ?, ?)",
            (surgery_id, user_id, role_name, status),
        )


def _status(surgery_id):
    with db_session() as db:
        return db.execute("SELECT status FROM surgeries WHERE id = ?",
                          (surgery_id,)).fetchone()["status"]


def _audit(action_type, target_id):
    with db_session() as db:
        return db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (action_type, target_id),
        ).fetchone()


# ══════════════════════════════════════════════════════════════════════════
#  SURGICAL
# ══════════════════════════════════════════════════════════════════════════

def test_start_unknown_surgery_is_404_not_a_false_success(doctor):
    """Was: no row was read, the UPDATE matched nothing, and the caller got
    {"status": "success"} for a surgery that does not exist."""
    resp = client.post("/api/surgery/SURG-NOPE/start", headers=_auth(doctor))
    assert resp.status_code == 404


def test_start_cannot_reopen_a_completed_surgery(doctor, patient):
    """Was: a blind UPDATE dragged 'completed' back to 'in_progress', detaching
    the case from its post-op note (which /complete refuses to write twice)."""
    sid = _mk_surgery(patient, doctor, status="completed")
    _assign(sid, doctor, status="confirmed")
    resp = client.post(f"/api/surgery/{sid}/start", headers=_auth(doctor))
    assert resp.status_code == 409
    assert _status(sid) == "completed"


def test_start_is_idempotent_for_an_already_running_case(doctor, patient):
    sid = _mk_surgery(patient, doctor, status="in_progress")
    _assign(sid, doctor, status="confirmed")
    resp = client.post(f"/api/surgery/{sid}/start", headers=_auth(doctor))
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_in_progress"


def test_start_blocks_on_pending_assignments_not_only_alerted(doctor, nurse, patient):
    """Was: only status='alerted' blocked the start. 'pending' is the schema
    DEFAULT for surgery_team_assignments, so a roster written with the default
    let a case start with nobody having actually confirmed."""
    sid = _mk_surgery(patient, doctor)
    _assign(sid, doctor, status="confirmed", role_name="Surgeon")
    _assign(sid, nurse, status="pending", role_name="Scrub Nurse")
    resp = client.post(f"/api/surgery/{sid}/start", headers=_auth(doctor))
    assert resp.status_code == 400
    assert _status(sid) == "scheduled"


def test_start_succeeds_when_every_assignment_is_confirmed_and_is_audited(doctor, patient):
    sid = _mk_surgery(patient, doctor)
    _assign(sid, doctor, status="confirmed", role_name="Surgeon")
    resp = client.post(f"/api/surgery/{sid}/start", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    assert _status(sid) == "in_progress"

    row = _audit("surgery_started", sid)
    assert row is not None, "starting an operation must be on the audit chain"
    assert row["patient_id"] == patient
    assert json.loads(row["metadata_json"])["to_status"] == "in_progress"


def test_acknowledge_unknown_emergency_is_404(doctor):
    """Was: a blind UPDATE — acknowledging a non-existent case returned success."""
    resp = client.post("/api/surgery/emergency/SURG-NOPE/acknowledge", headers=_auth(doctor))
    assert resp.status_code == 404


def test_acknowledge_refuses_a_completed_case(doctor, patient):
    """Was: acknowledging a completed case set status='emergency_acknowledged',
    dragging a finished operation back into the emergency queue."""
    sid = _mk_surgery(patient, doctor, status="completed")
    resp = client.post(f"/api/surgery/emergency/{sid}/acknowledge", headers=_auth(doctor))
    assert resp.status_code == 409
    assert _status(sid) == "completed"


def test_acknowledge_emergency_succeeds_and_is_audited(doctor, patient):
    sid = _mk_surgery(patient, doctor, status="emergency_pending")
    resp = client.post(f"/api/surgery/emergency/{sid}/acknowledge", headers=_auth(doctor))
    assert resp.status_code == 200
    assert _status(sid) == "emergency_acknowledged"
    assert _audit("emergency_surgery_acknowledged", sid) is not None
    # Second call is a no-op, not a 409.
    assert client.post(f"/api/surgery/emergency/{sid}/acknowledge",
                       headers=_auth(doctor)).json()["status"] == "already_acknowledged"


def test_team_response_rejects_anything_that_is_not_confirm_or_decline(doctor, patient):
    """Was: `'confirmed' if action == 'confirm' else 'declined'` — a typo, a
    missing field or "Confirm" silently DECLINED the assignment and removed a
    surgeon from an emergency case."""
    sid = _mk_surgery(patient, doctor)
    _mk_case_room(patient)
    _assign(sid, doctor, status="alerted")

    for bad in ({}, {"action": "yes"}, {"action": None}, {"action": "declined!"}):
        resp = client.post(f"/api/surgery/{sid}/team-response", json=bad,
                           headers=_auth(doctor))
        assert resp.status_code == 422, f"{bad} should not be accepted"

    with db_session() as db:
        row = db.execute(
            "SELECT status FROM surgery_team_assignments WHERE surgery_id = ?", (sid,)
        ).fetchone()
    assert row["status"] == "alerted", "a rejected verb must not change the roster"

    # Case/whitespace variants of a KNOWN verb are still honoured — the point is
    # that an unrecognised verb never silently means "decline".
    ok = client.post(f"/api/surgery/{sid}/team-response",
                     json={"action": " Confirm "}, headers=_auth(doctor))
    assert ok.status_code == 200
    with db_session() as db:
        assert db.execute(
            "SELECT status FROM surgery_team_assignments WHERE surgery_id = ?", (sid,)
        ).fetchone()["status"] == "confirmed"


def test_team_response_confirm_records_and_audits(doctor, patient):
    sid = _mk_surgery(patient, doctor)
    _mk_case_room(patient)
    _assign(sid, doctor, status="alerted")
    resp = client.post(f"/api/surgery/{sid}/team-response",
                       json={"action": "confirm"}, headers=_auth(doctor))
    assert resp.status_code == 200
    with db_session() as db:
        assert db.execute(
            "SELECT status FROM surgery_team_assignments WHERE surgery_id = ?", (sid,)
        ).fetchone()["status"] == "confirmed"
    assert _audit("surgical_team_response", sid) is not None


def test_alert_team_rejects_a_member_with_no_user_id(doctor, patient):
    """Was: NOT NULL / FK violations surfaced as a 500 — after the DELETE had
    already wiped the roster that was there."""
    sid = _mk_surgery(patient, doctor)
    _mk_case_room(patient)
    _assign(sid, doctor, status="confirmed", role_name="Surgeon")

    resp = client.post(f"/api/surgery/{sid}/alert-team",
                       json={"team": [{"role": "Anesthetist"}]}, headers=_auth(doctor))
    assert resp.status_code == 422

    with db_session() as db:
        remaining = db.execute(
            "SELECT COUNT(*) c FROM surgery_team_assignments WHERE surgery_id = ?", (sid,)
        ).fetchone()["c"]
    assert remaining == 1, "a rejected alert must not destroy the existing roster"


def test_alert_team_rejects_a_user_id_that_is_not_an_orb_user(doctor, patient):
    sid = _mk_surgery(patient, doctor)
    _mk_case_room(patient)
    resp = client.post(f"/api/surgery/{sid}/alert-team",
                       json={"team": [{"user_id": "GHOST", "role": "Anesthetist"}]},
                       headers=_auth(doctor))
    assert resp.status_code == 422
    assert "GHOST" in resp.json()["detail"]


def test_alert_team_rejects_the_same_member_twice(doctor, nurse, patient):
    sid = _mk_surgery(patient, doctor)
    _mk_case_room(patient)
    resp = client.post(f"/api/surgery/{sid}/alert-team",
                       json={"team": [{"user_id": nurse, "role": "Scrub Nurse"},
                                      {"user_id": nurse, "role": "Circulating Nurse"}]},
                       headers=_auth(doctor))
    assert resp.status_code == 422


def test_alert_team_happy_path_writes_roster_and_audits(doctor, nurse, patient):
    sid = _mk_surgery(patient, doctor, status="emergency_pending")
    _mk_case_room(patient)
    resp = client.post(f"/api/surgery/{sid}/alert-team",
                       json={"team": [{"user_id": nurse, "role": "Scrub Nurse"}]},
                       headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    assert _status(sid) == "emergency_ready"
    row = _audit("surgical_team_alerted", sid)
    assert row is not None
    assert json.loads(row["metadata_json"])["alerted_user_ids"] == [nurse]


def test_schedule_window_is_bounded(doctor):
    """Was: `days` was an unbounded int — days=100000 built a 100000-entry list
    and scanned a 274-year date range on a DB worker."""
    assert client.get("/api/surgery/schedule?days=100000",
                      headers=_auth(doctor)).status_code == 422
    assert client.get("/api/surgery/schedule?days=0",
                      headers=_auth(doctor)).status_code == 422
    assert client.get("/api/surgery/schedule?days=-5",
                      headers=_auth(doctor)).status_code == 422
    assert client.get("/api/surgery/schedule?days=7",
                      headers=_auth(doctor)).status_code == 200


def test_schedule_marks_today_in_hospital_local_terms(doctor, patient, monkeypatch):
    """Two defects in one response.

    `is_today` was NEVER sent — SurgicalSuitePanel renders a 'Today' badge and a
    --today card modifier off it, so both were unreachable and every card in the
    week view looked alike.

    And the window opened on `datetime.now(timezone.utc).date()`. Pinned here to
    01:30 IST (20:00 UTC the previous day) — the UTC date is still yesterday, so
    the week view led with YESTERDAY's list, and a badge computed the same way
    would have sat on it. Same hospital-local convention as the Forecast day-0
    row (services/hospital_time.py), so the two surfaces cannot disagree.
    """
    from datetime import datetime, timezone as _tz
    from services import hospital_time
    from services.vitals_source import hl7v2

    frozen = datetime(2027, 3, 14, 20, 0, tzinfo=_tz.utc)  # 2027-03-15 01:30 IST
    hl7v2.set_default_timezone("Asia/Kolkata")
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: frozen)
    try:
        yesterday_local = _mk_surgery(patient, doctor, scheduled_date="2027-03-14")
        today_local = _mk_surgery(patient, doctor, scheduled_date="2027-03-15")

        schedule = client.get("/api/surgery/schedule?days=3",
                              headers=_auth(doctor)).json()

        assert schedule[0]["date"] == "2027-03-15", "week opens on the ward's today"
        assert schedule[0]["is_today"] is True
        assert [d["is_today"] for d in schedule] == [True, False, False]

        ids_today = [s["id"] for s in schedule[0]["surgeries"]]
        assert today_local in ids_today
        # 2027-03-14 is yesterday HERE even though it is still today in UTC.
        assert yesterday_local not in [
            s["id"] for day in schedule for s in day["surgeries"]
        ]
    finally:
        hl7v2.set_default_timezone(None)
        with db_session() as db:
            db.execute("DELETE FROM surgeries WHERE patient_id = ?", (patient,))


def test_prep_for_unknown_surgery_is_404_not_500(doctor):
    """Was: surgery_service.trigger_surgery_prep raised a bare ValueError that
    nothing caught, so the route 500'd."""
    resp = client.post("/api/surgery/SURG-NOPE/prep", headers=_auth(doctor))
    assert resp.status_code == 404


def test_prep_tolerates_a_staff_roster_written_as_plain_strings(doctor, patient):
    """Was: `[s["name"] for s in staff_required]` assumed a list of dicts. A
    generator that wrote a list of NAMES raised TypeError and the whole
    Prep-for-Surgery action 500'd; the `"id" in staff` fallback was also a
    substring test against a string."""
    from services.surgery_service import _staff_display_names
    assert _staff_display_names(["Dr Rao", "Sr Meena"]) == ["Dr Rao", "Sr Meena"]
    assert _staff_display_names([{"role": "Surgeon", "name": "Dr Rao"}]) == ["Dr Rao"]
    assert _staff_display_names([{"role": "Anesthetist"}]) == ["Anesthetist"]
    assert _staff_display_names(None) == []
    assert _staff_display_names("Dr Rao") == []


def test_who_checklist_on_unknown_surgery_is_404(doctor):
    resp = client.post("/api/surgery/SURG-NOPE/checklist",
                       json={"item_id": "si_identity", "checked": True},
                       headers=_auth(doctor))
    assert resp.status_code == 404


def test_who_checklist_item_is_audited(doctor, patient):
    """The WHO checklist is the OR's safety attestation — it was persisted on the
    surgeries row but never written to the tamper-evident audit chain."""
    sid = _mk_surgery(patient, doctor)
    resp = client.post(f"/api/surgery/{sid}/checklist",
                       json={"item_id": "si_identity", "checked": True},
                       headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    row = _audit("who_checklist_item_set", sid)
    assert row is not None
    assert json.loads(row["metadata_json"])["item_id"] == "si_identity"


def test_risk_generator_does_no_blocking_db_work_on_the_event_loop(doctor, patient, monkeypatch):
    """Was: generate_risk_assessment() opened a db_session on its first step, and
    an async generator body runs ON the event loop. That load fans out to
    sage_service._assemble_patient_context (~8 encrypted queries), so every
    risk-panel open stalled the whole API. The detail is now loaded by the route
    via run_db and handed in.

    Asserted structurally (no LLM in the test env): with `detail` supplied, the
    generator must reach its first yield without ever calling database.db_session.
    """
    import database
    import services.model_router as model_router
    from services.surgery_service import generate_risk_assessment

    sid = _mk_surgery(patient, doctor)
    assert "detail" in inspect.signature(generate_risk_assessment).parameters

    # No Ollama in the test env: short-circuit the model selection so the
    # generator drops straight to its offline fallback instead of spending the
    # httpx timeout. What is under test is the DB access pattern, not the LLM.
    async def _no_model():
        raise RuntimeError("no LLM in tests")
    monkeypatch.setattr(model_router, "select_chat_model", _no_model)

    calls = []
    real_session = database.db_session

    def tripwire(*a, **kw):
        calls.append(1)
        return real_session(*a, **kw)

    detail = {
        "patient_name": "X", "age": 50, "sex": "M", "mrn": "M1",
        "patient_diagnosis": "Sepsis", "procedure_name": "Laparotomy",
        "diagnosis": "Perforation", "co_morbidities": ["DM"],
        "risk_level": "critical", "risk_score": 9, "estimated_duration_min": 120,
        "possible_complications": ["bleeding"], "success_probability": 72.5,
    }

    async def drain():
        agen = generate_risk_assessment(sid, detail=detail)
        chunks = []
        async for chunk in agen:
            chunks.append(chunk)
        return chunks

    database.db_session = tripwire
    try:
        chunks = asyncio.run(drain())
    finally:
        database.db_session = real_session

    assert chunks and chunks[-1] == "data: [DONE]\n\n"
    assert calls == [], "the SSE generator must not open a DB session on the loop"


def test_emergency_case_is_assigned_to_a_real_doctor_not_a_demo_seed_id(patient, doctor):
    """Was: `surgeon_id = "DOC004"` — an id that exists only in
    scripts/seed_clinical_data.py. surgeries.surgeon_id is a FK, so at a real
    site confirming an emergency prep died with an IntegrityError and the whole
    Relay action rolled back with a 500 in front of the clinician."""
    from services import surgery_escalation_service as esc

    with db_session() as db:
        db.execute("UPDATE patients SET attending_doctor_id = ? WHERE id = ?", (doctor, patient))
    cid = _mk_case_room(patient)

    prep = esc.create_surgery_prep_request(
        patient_id=patient, channel_id=cid, requested_by=doctor,
        requested_by_name="Dr Test", procedure_name="Laparotomy",
        indication="Perforation", urgency="emergency", clinical_context="{}")

    result = esc.confirm_surgery_prep(prep["id"], doctor, "Dr Test", prep["idempotency_key"])
    assert result["status"] == "created"
    with db_session() as db:
        row = db.execute("SELECT surgeon_id FROM surgeries WHERE id = ?",
                         (result["surgery_id"],)).fetchone()
    assert row["surgeon_id"] == doctor, "the patient's attending doctor should own the case"

    # Idempotent on replay of the same key.
    replay = esc.confirm_surgery_prep(prep["id"], doctor, "Dr Test", prep["idempotency_key"])
    assert replay == {"surgery_id": result["surgery_id"], "status": "already_created"}


def test_two_prep_requests_on_one_case_room_in_the_same_second_do_not_collide(patient, doctor):
    """Was: the idempotency key was channel_id + whole-second timestamp against a
    UNIQUE column, so the second request in the same second raised IntegrityError."""
    from services import surgery_escalation_service as esc
    cid = _mk_case_room(patient)
    keys = {
        esc.create_surgery_prep_request(
            patient_id=patient, channel_id=cid, requested_by=doctor,
            requested_by_name="Dr Test", procedure_name="Laparotomy",
            indication="Perforation", urgency="emergency", clinical_context="{}",
        )["idempotency_key"]
        for _ in range(3)
    }
    assert len(keys) == 3


def test_surgery_routes_reject_a_patient_role(patient):
    """Surgical endpoints must stay clinician-scoped."""
    puid = _mk_user("patient", "H3PAT")
    headers = _auth(puid, "patient")
    assert client.get("/api/surgery/schedule", headers=headers).status_code == 403
    assert client.post("/api/surgery/SURG-X/start", headers=headers).status_code == 403
    assert client.post("/api/surgery/SURG-X/team-response",
                       json={"action": "confirm"}, headers=headers).status_code == 403


# ══════════════════════════════════════════════════════════════════════════
#  SCHEDULING (appointments)
# ══════════════════════════════════════════════════════════════════════════

def _book(doctor_id, patient_id, day, time="10:00", **extra):
    body = {"patient_id": patient_id, "scheduled_date": day, "scheduled_time": time}
    body.update(extra)
    return client.post("/api/appointments", json=body, headers=_auth(doctor_id))


def _future(days=3):
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


def test_cannot_book_a_slot_earlier_today(doctor, patient):
    """Was: only the DATE was compared, so 'today at 00:01' booked cleanly at 4pm
    and then showed under the patient's 'upcoming' list until midnight."""
    from datetime import date
    resp = _book(doctor, patient, date.today().isoformat(), "00:01")
    assert resp.status_code == 422
    assert "passed" in resp.json()["detail"]


def test_cannot_book_beyond_the_horizon(doctor, patient):
    """A typo'd year used to book a slot no view would ever show again."""
    resp = _book(doctor, patient, "2206-03-04")
    assert resp.status_code == 422


def test_double_booking_the_same_slot_returns_the_existing_appointment(doctor, patient):
    """Was: the Idempotency-Key only deduped a REPLAY of one request. Two taps of
    'Schedule Follow-Up' are two distinct requests, and both created a row —
    the same patient twice in the same minute."""
    day = _future()
    first = _book(doctor, patient, day, "11:30")
    second = _book(doctor, patient, day, "11:30")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    with db_session() as db:
        n = db.execute(
            "SELECT COUNT(*) c FROM appointments WHERE patient_id = ? AND scheduled_date = ? "
            "AND scheduled_time = ? AND status = 'booked'",
            (patient, day, "11:30"),
        ).fetchone()["c"]
    assert n == 1


def test_a_cancelled_slot_can_be_rebooked(doctor, patient):
    """The duplicate guard must only look at LIVE bookings."""
    day = _future(4)
    first = _book(doctor, patient, day, "12:00").json()
    client.delete(f"/api/appointments/{first['id']}", headers=_auth(doctor))
    second = _book(doctor, patient, day, "12:00")
    assert second.status_code == 200
    assert second.json()["id"] != first["id"]


def test_overlong_reason_is_rejected(doctor, patient):
    resp = _book(doctor, patient, _future(5), "13:00", reason="x" * 5000)
    assert resp.status_code == 422


def test_appointment_can_be_marked_completed_and_is_audited(doctor, patient):
    """Was: the schema allowed status='completed' but no route could ever set it,
    so every attended visit stayed 'booked' forever — indistinguishable from a
    no-show."""
    appt = _book(doctor, patient, _future(6), "14:00").json()
    resp = client.post(f"/api/appointments/{appt['id']}/complete", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
    # Idempotent.
    assert client.post(f"/api/appointments/{appt['id']}/complete",
                       headers=_auth(doctor)).json()["status"] == "completed"
    assert _audit("appointment_completed", appt["id"]) is not None


def test_completed_appointment_cannot_be_cancelled(doctor, patient):
    """Was: cancelling a completed appointment silently flipped it to
    'cancelled' and the visit vanished from the record."""
    appt = _book(doctor, patient, _future(7), "15:00").json()
    client.post(f"/api/appointments/{appt['id']}/complete", headers=_auth(doctor))
    resp = client.delete(f"/api/appointments/{appt['id']}", headers=_auth(doctor))
    assert resp.status_code == 409


def test_cancelled_appointment_cannot_be_completed(doctor, patient):
    appt = _book(doctor, patient, _future(8), "16:00").json()
    client.delete(f"/api/appointments/{appt['id']}", headers=_auth(doctor))
    resp = client.post(f"/api/appointments/{appt['id']}/complete", headers=_auth(doctor))
    assert resp.status_code == 409


def test_completing_an_appointment_requires_patient_write(patient, doctor):
    appt = _book(doctor, patient, _future(9), "17:00").json()
    puid = _mk_user("patient", "H3PA2")
    resp = client.post(f"/api/appointments/{appt['id']}/complete",
                       headers=_auth(puid, "patient"))
    assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
#  REVENUE INTEGRITY (coding / CDI)
# ══════════════════════════════════════════════════════════════════════════

def test_unknown_patient_is_404_not_a_clean_bill_of_health(doctor):
    """Was: an unknown id returned the same all-zero report a fully-documented
    patient returns — a typo read as 'no capture gaps found'."""
    resp = client.get("/api/coding/patient/PAT-NOPE", headers=_auth(doctor))
    assert resp.status_code == 404


def test_known_patient_returns_a_report_and_is_phi_audited(doctor, patient):
    resp = client.get(f"/api/coding/patient/{patient}", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patient_found"] is True
    assert "reimbursement" in body

    row = _audit("coding_cdi_report_viewed", patient)
    assert row is not None, "a CDI report over a named patient is a PHI read"
    assert row["event_category"] == "phi_access"
    assert row["actor_user_id"] == doctor


def test_medication_with_null_status_still_drives_cdi_inference(patient, doctor):
    """Was: `status != 'discontinued'` dropped rows whose status is NULL (SQL
    three-valued logic), so a whole class of charted drugs was invisible to CDI
    and capture gaps were under-reported."""
    with db_session() as db:
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status) "
            "VALUES (?, 'Metformin', '500mg', 'Oral', 'BD', NULL)",
            (patient,),
        )
    ctx = coding_service._load_patient_context(patient)
    assert "Metformin" in ctx["medications"]

    result = coding_service.suggest_codes(patient)
    # A drug that implies an undocumented condition is a documentation query
    # (2026-10-06), not a code suggestion or a flag.
    queries = {(q["code"], q["medication"]) for q in result["documentation_queries"]}
    assert ("E11.9", "Metformin") in queries


def test_discontinued_medication_is_still_excluded(patient):
    with db_session() as db:
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status) "
            "VALUES (?, 'Warfarin', '5mg', 'Oral', 'OD', 'discontinued')",
            (patient,),
        )
    ctx = coding_service._load_patient_context(patient)
    assert "Warfarin" not in ctx["medications"]


def test_snomed_problem_entries_do_not_pollute_the_reimbursement_assumptions(patient):
    """Was: SNOMED codes on the problem list landed in `all_potential` and were
    published in the `code_weights` assumption block with weight 0.0 — reading as
    'this code is worth nothing' rather than 'this model does not price SNOMED'.
    DEFAULT_CODE_WEIGHTS is ICD-10-keyed, so the money math is ICD-10 only."""
    with db_session() as db:
        db.execute(
            "INSERT INTO problem_list (patient_id, problem, snomed_code, status) "
            "VALUES (?, 'Type 2 diabetes mellitus', '73211009', 'active')",
            (patient,),
        )
    result = coding_service.suggest_codes(patient)
    weights = result["reimbursement"]["assumptions"]["code_weights"]
    assert "73211009" not in weights


def test_coded_icd10_problem_counts_as_captured(patient):
    with db_session() as db:
        db.execute(
            "INSERT INTO problem_list (patient_id, problem, icd10_code, status) "
            "VALUES (?, 'Sepsis', 'A41.9', 'active')",
            (patient,),
        )
    result = coding_service.suggest_codes(patient)
    expected = coding_service.DEFAULT_CODE_WEIGHTS["A41.9"] * coding_service.BASE_RATE_INR
    assert result["reimbursement"]["captured_inr"] == pytest.approx(expected)


def test_coding_endpoint_rejects_a_patient_role(patient):
    puid = _mk_user("patient", "H3PA3")
    resp = client.get(f"/api/coding/patient/{patient}", headers=_auth(puid, "patient"))
    assert resp.status_code == 403
