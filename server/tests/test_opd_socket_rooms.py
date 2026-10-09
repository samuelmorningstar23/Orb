"""Live queue nudges for desk accounts (opd-9b, W8-B) and the desk queue's
read audit (ruling OPD-N12 (b)).

What this file defends:

  1. ROOMS. While the 'opd' feature is on, a registration.read holder's
     socket joins 'opd:<their department>' ('opd:__all__' with none). The
     receptionist joins only that room (and its own user room): no vitals
     room, so the desk never receives the census. A nurse (no
     registration.read) joins no opd room. With the feature off nobody joins
     one, and every room is exactly what it was.
  2. THE NUDGE carries {department} and nothing else, in ONE emit to the
     clinic's vitals rooms and opd rooms, so a socket in two of them hears it
     once; with the feature off nothing is pushed.
  3. THE DESK STOPS POLLING: the OPD-mode queue answer says whether this
     caller hears the nudge ('live_updates'); off, the answer is unchanged.
  4. OPD-N12 (b): an unchanged desk queue re-read writes no new phi_access
     row for 10 minutes; a change to the visible visits writes one at once,
     with the visit ids; a refused audit write never silences the next read;
     with the feature off every non-empty read is audited, as before.

tests/test_ws_origin.py is untouched by this increment and stays green.
"""
from __future__ import annotations

import asyncio
import json
import types
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from routes import frontdesk_routes
from services import opd_queue_service as oq
from services.hospital_time import hospital_today
from ws import realtime

client = TestClient(app)

DEPT = "Socket Room Cardiology"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def opd_on(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    assert config.feature_enabled("opd")
    oq.reset_queue_read_audit()
    yield
    oq.reset_queue_read_audit()


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    assert not config.feature_enabled("opd")


def _mk_user(role, department):
    uid = f"SKR{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, f"Socket {role} {uid[-4:]}", role, department),
        )
    return {"id": uid, "name": f"Socket {role}", "role": role, "department": department}


def _connect(monkeypatch, user) -> set:
    """Run realtime.connect for `user` and return the rooms it joined."""
    rooms: set = set()

    async def save_session(sid, session):
        return None

    async def enter_room(sid, room):
        rooms.add(room)

    monkeypatch.setattr(realtime, "_authenticate", lambda auth, environ=None: dict(user))
    monkeypatch.setattr(realtime.sio, "save_session", save_session)
    monkeypatch.setattr(realtime.sio, "enter_room", enter_room)
    accepted = asyncio.run(realtime.connect("sid-" + user["id"], {}, {"token": "x"}))
    assert accepted is True
    return rooms


def _auth(user):
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


def _outpatient():
    pid = f"SKP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status) "
            "VALUES (?, ?, ?, 44, 'F', 'registered')",
            (pid, f"Socket Patient {pid[-4:]}", f"UH-{uuid.uuid4().hex[:8].upper()}"))
    return pid


_TOKENS = iter(range(100, 10_000))


def _waiting_visit(department=DEPT):
    vid = f"SKV{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
            "status, registered_at) VALUES (?, ?, ?, ?, ?, 'waiting', ?)",
            (vid, _outpatient(), department, hospital_today().isoformat(), next(_TOKENS),
             "2026-10-10T03:00:00+00:00"),
        )
    return vid


def _queue_rows(user):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'opd_queue_viewed' "
            "AND actor_user_id = ? ORDER BY id", (user["id"],)).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Rooms
# ═════════════════════════════════════════════════════════════════════════════

def test_the_receptionist_joins_its_opd_room_and_no_vitals_room(monkeypatch, opd_on):
    recep = _mk_user("receptionist", DEPT)
    rooms = _connect(monkeypatch, recep)
    assert rooms == {f"user:{recep['id']}", f"opd:{DEPT}"}
    assert not any(r.startswith("dept:") for r in rooms)


def test_a_central_desk_joins_the_all_clinics_opd_room(monkeypatch, opd_on):
    recep = _mk_user("receptionist", None)
    assert _connect(monkeypatch, recep) == {f"user:{recep['id']}", "opd:__all__"}


def test_doctor_admin_and_nurse_rooms(monkeypatch, opd_on):
    doctor = _mk_user("doctor", DEPT)
    assert _connect(monkeypatch, doctor) == {f"user:{doctor['id']}", f"dept:{DEPT}",
                                             f"opd:{DEPT}"}
    admin = _mk_user("admin", None)
    assert _connect(monkeypatch, admin) == {f"user:{admin['id']}", "dept:__all__",
                                            "opd:__all__"}
    nurse = _mk_user("nurse", DEPT)
    assert _connect(monkeypatch, nurse) == {f"user:{nurse['id']}", f"dept:{DEPT}"}
    cashier = _mk_user("cashier", None)
    assert _connect(monkeypatch, cashier) == {f"user:{cashier['id']}"}


def test_with_the_feature_off_nobody_joins_an_opd_room(monkeypatch, opd_off):
    admin = _mk_user("admin", None)
    assert _connect(monkeypatch, admin) == {f"user:{admin['id']}", "dept:__all__"}
    doctor = _mk_user("doctor", DEPT)
    assert _connect(monkeypatch, doctor) == {f"user:{doctor['id']}", f"dept:{DEPT}"}


# ═════════════════════════════════════════════════════════════════════════════
# 2. The nudge
# ═════════════════════════════════════════════════════════════════════════════

def test_one_content_free_emit_reaches_the_desk_and_doctor_rooms(monkeypatch, opd_on):
    emitted: list = []

    async def fake_emit(event, payload, room=None, **kw):
        emitted.append((event, payload, room))

    monkeypatch.setattr(realtime.sio, "emit", fake_emit)
    asyncio.run(oq._emit_queue_changed(DEPT))
    assert emitted == [("opd:queue_changed", {"department": DEPT},
                        [f"dept:{DEPT}", "dept:__all__", f"opd:{DEPT}", "opd:__all__"])]


def test_a_socket_in_two_of_the_rooms_hears_the_nudge_once():
    """python-socketio resolves a list of rooms to one set of participants."""
    from socketio.base_manager import BaseManager

    mgr = BaseManager()   # the server's AsyncManager inherits get_participants
    mgr.set_server(types.SimpleNamespace(eio=types.SimpleNamespace(
        generate_id=lambda: uuid.uuid4().hex)))
    mgr.initialize()
    sid = mgr.connect("eio-1", "/")
    mgr.basic_enter_room(sid, "/", f"dept:{DEPT}")
    mgr.basic_enter_room(sid, "/", f"opd:{DEPT}")
    other = mgr.connect("eio-2", "/")
    mgr.basic_enter_room(other, "/", "opd:__all__")
    heard = [s for s, _ in mgr.get_participants("/", realtime.queue_nudge_rooms(DEPT))]
    assert sorted(heard) == sorted([sid, other])


def test_nothing_is_pushed_with_the_feature_off(monkeypatch, opd_off):
    seen: list = []
    monkeypatch.setattr(realtime, "schedule_on_loop",
                        lambda make_coro, what: seen.append(what) or True)
    assert oq.nudge_queue_changed(DEPT) is False
    assert seen == []


def test_who_hears_the_nudge(opd_on, monkeypatch):
    assert realtime.receives_queue_nudges("receptionist") is True
    assert realtime.receives_queue_nudges("doctor") is True
    assert realtime.receives_queue_nudges("nurse") is True
    assert realtime.receives_queue_nudges("cashier") is False
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    assert realtime.receives_queue_nudges("receptionist") is False
    assert realtime.receives_queue_nudges("admin") is True


# ═════════════════════════════════════════════════════════════════════════════
# 3. The desk stops polling
# ═════════════════════════════════════════════════════════════════════════════

def test_the_opd_queue_says_the_desk_hears_the_nudge(opd_on):
    recep = _mk_user("receptionist", DEPT)
    _waiting_visit()
    body = client.get("/api/frontdesk/opd/queue", headers=_auth(recep)).json()
    assert body["opd_mode"] is True and body["live_updates"] is True


def test_the_flag_off_queue_answer_is_unchanged(opd_off):
    admin = _mk_user("admin", None)
    body = client.get("/api/frontdesk/opd/queue", headers=_auth(admin)).json()
    assert "live_updates" not in body and "opd_mode" not in body


# ═════════════════════════════════════════════════════════════════════════════
# 4. OPD-N12 (b)
# ═════════════════════════════════════════════════════════════════════════════

def test_an_unchanged_queue_writes_one_row_and_a_change_writes_another(opd_on, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(oq, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    dept = f"Dedupe Clinic {uuid.uuid4().hex[:6]}"
    recep = _mk_user("receptionist", dept)
    first = _waiting_visit(dept)

    for _ in range(5):
        clock[0] += 30
        assert client.get("/api/frontdesk/opd/queue", headers=_auth(recep)).status_code == 200
    rows = _queue_rows(recep)
    assert len(rows) == 1
    meta = json.loads(rows[0]["metadata_json"])
    assert meta["visit_ids"] == [first]
    assert rows[0]["event_category"] == "phi_access"

    # A new walk-in changes what the desk sees: a new row at once.
    second = _waiting_visit(dept)
    clock[0] += 30
    client.get("/api/frontdesk/opd/queue", headers=_auth(recep))
    rows = _queue_rows(recep)
    assert len(rows) == 2
    assert json.loads(rows[1]["metadata_json"])["visit_ids"] == sorted([first, second])

    # Ten minutes on, the same view is recorded again.
    clock[0] += oq.QUEUE_READ_AUDIT_WINDOW_SECONDS
    client.get("/api/frontdesk/opd/queue", headers=_auth(recep))
    assert len(_queue_rows(recep)) == 3


def test_the_dedupe_is_per_user(opd_on):
    dept = f"Dedupe Clinic {uuid.uuid4().hex[:6]}"
    one, two = _mk_user("receptionist", dept), _mk_user("receptionist", dept)
    _waiting_visit(dept)
    for user in (one, two, one, two):
        client.get("/api/frontdesk/opd/queue", headers=_auth(user))
    assert len(_queue_rows(one)) == 1 and len(_queue_rows(two)) == 1


def test_a_refused_audit_write_does_not_silence_the_next_read(opd_on, monkeypatch):
    dept = f"Dedupe Clinic {uuid.uuid4().hex[:6]}"
    recep = _mk_user("receptionist", dept)
    _waiting_visit(dept)
    real = frontdesk_routes.log_phi_read

    def refuse(**_kw):
        raise RuntimeError("audit chain unavailable")

    monkeypatch.setattr(frontdesk_routes, "log_phi_read", refuse)
    failing = TestClient(app, raise_server_exceptions=False)
    assert failing.get("/api/frontdesk/opd/queue", headers=_auth(recep)).status_code == 500
    assert _queue_rows(recep) == []
    monkeypatch.setattr(frontdesk_routes, "log_phi_read", real)
    assert client.get("/api/frontdesk/opd/queue", headers=_auth(recep)).status_code == 200
    assert len(_queue_rows(recep)) == 1


def test_with_the_feature_off_every_read_is_audited(opd_off):
    dept = f"Dedupe Clinic {uuid.uuid4().hex[:6]}"
    nurse = _mk_user("nurse", dept)
    _waiting_visit(dept)
    for _ in range(3):
        assert client.get("/api/frontdesk/opd/queue", headers=_auth(nurse)).status_code == 200
    rows = _queue_rows(nurse)
    assert len(rows) == 3
    assert "visit_ids" not in json.loads(rows[0]["metadata_json"])


def test_the_claim_helpers():
    oq.reset_queue_read_audit()
    claim = oq.claim_queue_read_audit("U1", ["b", "a"], now=10.0)
    assert claim is not None
    assert oq.claim_queue_read_audit("U1", ["a", "b", "a"], now=20.0) is None
    assert oq.claim_queue_read_audit("U1", ["a"], now=20.0) is not None
    oq.release_queue_read_audit(claim)
    assert oq.claim_queue_read_audit("U1", ["a", "b"], now=30.0) is not None
    assert oq.claim_queue_read_audit("", ["a"], now=30.0) is not None
    assert oq.claim_queue_read_audit("", ["a"], now=31.0) is not None
    oq.reset_queue_read_audit()
