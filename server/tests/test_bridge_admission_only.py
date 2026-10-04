"""Bridge is for the admission only (ruling R029, 4 October 2026).

A patient's Bridge login works while they are admitted and until the end of the
hospital-local day of their latest discharge. After that every lane refuses it:
password sign-in (403, with the reason), every authenticated request on a token
minted earlier (401, so the app returns to sign-in), and the live socket.

Why: after discharge Relay hides the patient's case room, so a Bridge emergency
message reached nobody while the reply said "Your care team has been notified".
The rule is judged from the record each time, so a file readmitted later
(status 'admitted' again) opens again without anyone switching it back on.

The clock is pinned through services.hospital_time._utc_now, and the hospital
zone to Asia/Kolkata, so the IST day boundary (18:30 UTC) is exercised.
"""
import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from database import db_session
from main import app
from services import hospital_time
from services.patient_portal_access import PORTAL_CLOSED_MESSAGE

client = TestClient(app)
PASSWORD = "bridge-admission-only-1"
IST = ZoneInfo("Asia/Kolkata")
NOTIFIED = "Your care team has been notified."


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    """Pin the hospital to IST and let a test move 'now'."""
    monkeypatch.setattr(hospital_time, "hospital_timezone", lambda: IST)
    state = {"now": _utc("2026-10-04 10:00:00")}
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: state["now"])

    def _set(text: str) -> None:
        state["now"] = _utc(text)
    return _set


@pytest.fixture
def make_patient():
    made = []

    def _make(status="admitted", discharged_at=None, *, linked=True, case_room=False):
        from auth import hash_password
        tag = uuid.uuid4().hex[:8].upper()
        p = {"user_id": f"BAO-U-{tag}", "patient_id": f"BAO-P-{tag}",
             "channel_id": f"case-BAO-{tag}" if case_room else None}
        with db_session() as db:
            db.execute(
                "INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, 'patient', ?)",
                (p["user_id"], "Admission Only Test", hash_password(PASSWORD)),
            )
            if linked:
                db.execute(
                    "INSERT INTO patients (id, user_id, name, mrn, bed, ward, status) "
                    "VALUES (?, ?, ?, ?, 'B-7', 'General Medicine', ?)",
                    (p["patient_id"], p["user_id"], "Admission Only Test", f"MRN-BAO-{tag}", status),
                )
                if discharged_at:
                    db.execute(
                        "INSERT INTO discharge_summaries (patient_id, author_id, summary, discharged_at) "
                        "VALUES (?, 'DOC001', 'Test summary.', ?)",
                        (p["patient_id"], discharged_at),
                    )
                if case_room:
                    db.execute(
                        "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                        (p["channel_id"], "Case Room: Admission Only Test", p["patient_id"]),
                    )
        made.append(p)
        return p

    yield _make
    with db_session() as db:
        for p in made:
            if p["channel_id"]:
                db.execute("DELETE FROM messages WHERE channel_id = ?", (p["channel_id"],))
                db.execute("DELETE FROM channels WHERE id = ?", (p["channel_id"],))
            db.execute("DELETE FROM discharge_summaries WHERE patient_id = ?", (p["patient_id"],))
            db.execute("DELETE FROM patients WHERE id = ?", (p["patient_id"],))
            db.execute("DELETE FROM users WHERE id = ?", (p["user_id"],))


def _login(user_id: str):
    return client.post("/api/auth/login", json={"employee_id": user_id, "password": PASSWORD})


def _status(token: str):
    return client.get("/api/bridge/status", headers={"Authorization": f"Bearer {token}"})


def _set_status(patient_id: str, status: str) -> None:
    with db_session() as db:
        db.execute("UPDATE patients SET status = ? WHERE id = ?", (status, patient_id))


# ── Admitted ─────────────────────────────────────────────────────────────────

def test_an_admitted_patient_signs_in_and_uses_bridge(clock, make_patient):
    p = make_patient("admitted")
    r = _login(p["user_id"])
    assert r.status_code == 200, r.text
    assert _status(r.json()["token"]).status_code == 200


# ── The discharge day ────────────────────────────────────────────────────────

def test_bridge_stays_open_until_the_end_of_the_discharge_day(clock, make_patient):
    # Discharged at 22:30 IST on 4 October; the IST day ends at 18:30 UTC.
    p = make_patient("discharged", "2026-10-04 17:00:00")
    clock("2026-10-04 18:00:00")  # 23:30 IST, still the discharge day
    r = _login(p["user_id"])
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert _status(token).status_code == 200

    clock("2026-10-04 18:31:00")  # 00:01 IST, the next day
    late = _status(token)
    assert late.status_code == 401, "an open session outlived the discharge day"
    assert late.json()["detail"] == PORTAL_CLOSED_MESSAGE
    assert client.post("/api/auth/refresh",
                       headers={"Authorization": f"Bearer {token}"}).status_code == 401

    denied = _login(p["user_id"])
    assert denied.status_code == 403
    assert denied.json()["detail"] == PORTAL_CLOSED_MESSAGE


def test_the_day_is_the_hospitals_not_utcs(clock, make_patient):
    """00:30 IST on 5 October is still 4 October in UTC. The portal must stay
    open through the whole IST day of 5 October, not close at UTC midnight."""
    p = make_patient("discharged", "2026-10-04 19:00:00")
    clock("2026-10-05 18:00:00")  # 23:30 IST on 5 October
    assert _login(p["user_id"]).status_code == 200
    clock("2026-10-05 18:30:00")  # 00:00 IST on 6 October
    assert _login(p["user_id"]).status_code == 403


def test_the_latest_discharge_counts(clock, make_patient):
    p = make_patient("discharged", "2026-09-01 08:00:00")
    with db_session() as db:
        db.execute("INSERT INTO discharge_summaries (patient_id, author_id, summary, discharged_at) "
                   "VALUES (?, 'DOC001', 'Second stay.', '2026-10-04 08:00:00')", (p["patient_id"],))
    clock("2026-10-04 12:00:00")
    assert _login(p["user_id"]).status_code == 200


def test_a_discharge_with_no_time_on_record_closes_the_portal(clock, make_patient):
    p = make_patient("discharged", None)
    r = _login(p["user_id"])
    assert r.status_code == 403
    assert r.json()["detail"] == PORTAL_CLOSED_MESSAGE


@pytest.mark.parametrize("status", ["transferred", "registered"])
def test_no_portal_without_an_admission(clock, make_patient, status):
    p = make_patient(status)
    assert _login(p["user_id"]).status_code == 403


def test_the_refusal_is_audited(clock, make_patient):
    p = make_patient("discharged", "2026-09-01 08:00:00")
    assert _login(p["user_id"]).status_code == 403
    with db_session() as db:
        row = db.execute(
            "SELECT summary_message FROM audit_logs WHERE actor_user_id = ? "
            "AND action_type = 'login_denied_portal_closed' ORDER BY rowid DESC LIMIT 1",
            (p["user_id"],),
        ).fetchone()
    assert row is not None and "discharged" in row["summary_message"]


# ── Readmission of the same file ─────────────────────────────────────────────

def test_a_readmitted_file_opens_again_on_its_own(clock, make_patient):
    """HIS_BUILD_PLAN W6-B readmits a returning patient into the same file.
    Nothing is switched off at discharge, so nothing has to be switched back on."""
    p = make_patient("discharged", "2026-09-01 08:00:00")
    assert _login(p["user_id"]).status_code == 403
    _set_status(p["patient_id"], "admitted")
    assert _login(p["user_id"]).status_code == 200


def test_an_account_with_no_patient_record_is_unchanged(clock, make_patient):
    p = make_patient(linked=False)
    assert _login(p["user_id"]).status_code == 200


# ── The live socket ──────────────────────────────────────────────────────────

def test_the_socket_refuses_a_closed_portal(clock, make_patient):
    from ws import realtime
    p = make_patient("discharged", "2026-10-04 08:00:00")
    clock("2026-10-04 12:00:00")
    token = _login(p["user_id"]).json()["token"]
    assert realtime._authenticate({"token": token}) is not None
    clock("2026-10-05 12:00:00")
    assert realtime._authenticate({"token": token}) is None


# ── Emergencies on the discharge day ─────────────────────────────────────────

def _ask(token: str, question: str) -> str:
    r = client.post("/api/bridge/ask", json={"question": question},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return "".join(
        json.loads(line[len("data: "):]).get("content", "")
        for line in r.text.splitlines() if line.startswith("data: ")
    )


@pytest.fixture
def quiet(monkeypatch):
    """No model, no socket, no event bus: only the stored post and the reply matter."""
    from services import ollama_gateway
    from services.event_bus import event_bus
    from ws import realtime

    def _boom(*a, **kw):
        raise AssertionError("a red-flag question reached the model")

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(ollama_gateway, "stream", _boom)
    monkeypatch.setattr(ollama_gateway, "post", _boom)
    monkeypatch.setattr(realtime.sio, "emit", _noop)
    monkeypatch.setattr(event_bus, "publish", _noop)


def test_a_discharged_patients_emergency_never_claims_the_team_was_told(clock, make_patient, quiet):
    """Relay hides a discharged patient's case room, so the post reaches nobody.
    The reply must still say what to do, and must not say help is coming."""
    p = make_patient("discharged", "2026-10-04 08:00:00", case_room=True)
    clock("2026-10-04 12:00:00")
    token = _login(p["user_id"]).json()["token"]
    reply = _ask(token, "I have crushing chest pain and cannot breathe")
    assert NOTIFIED not in reply, reply
    assert "emergency number" in reply
    with db_session() as db:
        stored = db.execute("SELECT COUNT(*) c FROM messages WHERE channel_id = ?",
                            (p["channel_id"],)).fetchone()["c"]
    assert stored == 1, "the post is still kept for the record"


def test_an_admitted_patients_emergency_still_says_the_team_was_told(clock, make_patient, quiet):
    p = make_patient("admitted", case_room=True)
    token = _login(p["user_id"]).json()["token"]
    assert NOTIFIED in _ask(token, "I have crushing chest pain and cannot breathe")
