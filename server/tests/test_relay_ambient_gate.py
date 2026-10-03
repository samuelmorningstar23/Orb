"""Relay: clinicians post text, the server posts everything else.

Until 2026-10-03 any clinician could post a message of any type the schema
allows (system, alert, surgical_alert, ambient_action) on REST and on the
socket, empty content was stored and broadcast, and the deprecated
POST /api/relay/actions/{message_id}/execute route, which committed medication
changes and discharges from an ambient_action row, was still live. These tests
pin the other side of each:

  * the execute route is gone,
  * a non-text message_type is refused on REST and ignored on the socket,
  * content is stripped and must be 1 to MAX_MESSAGE_CHARS on both lanes,
  * a legacy ambient_action row stays inert.
"""
import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from database import db_session
from main import app
from models.message import MAX_MESSAGE_CHARS
from ws import realtime

client = TestClient(app)

DOCTOR_ID = "D-AMBIENT-DOC"


@pytest.fixture()
def room():
    """A doctor, an admitted patient and their case room, with unique ids so
    tests do not collide with other files sharing the session DB."""
    from auth import hash_password
    suffix = uuid.uuid4().hex[:6].upper()
    patient_id = f"PAT-AMB-{suffix}"
    channel_id = f"case-{patient_id}"

    with db_session() as db:
        # OR IGNORE (not OR REPLACE): messages from earlier tests reference this
        # user, and REPLACE's delete would trip their FK.
        db.execute(
            """INSERT OR IGNORE INTO users (id, name, role, department, password_hash)
               VALUES (?, 'Dr. Ambient', 'doctor', 'General Medicine', ?)""",
            (DOCTOR_ID, hash_password("testpass123")),
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, 'Ambient Patient', ?, 'admitted')",
            (patient_id, f"MRN-AMB-{suffix}"),
        )
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, 'Ambient Case Room', 'case_room', ?)",
            (channel_id, patient_id),
        )

    resp = client.post("/api/auth/login", json={"employee_id": DOCTOR_ID, "password": "testpass123"})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return {
        "patient_id": patient_id,
        "channel_id": channel_id,
        "headers": {"Authorization": f"Bearer {resp.json()['token']}"},
    }


def _messages(channel_id: str) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY id", (channel_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def _active_meds(patient_id: str) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM medications WHERE patient_id = ? AND status = 'active'",
            (patient_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _post(room, body):
    return client.post(f"/api/relay/channels/{room['channel_id']}/messages",
                       json=body, headers=room["headers"])


# ── the execute route is gone ────────────────────────────────────────────────

def test_execute_route_is_gone_and_a_legacy_card_stays_inert(room):
    """A pending legacy card that would have charted a drug cannot be executed:
    the route does not exist, and the card and the chart are untouched."""
    content = json.dumps({"status": "pending", "action": "add_medication",
                          "params": {"drug_name": "Amoxicillin 500mg", "dosage": "500mg"}})
    with db_session() as db:
        msg_id = db.execute(
            """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type)
               VALUES (?, ?, 'Sage Ambient', 'system', ?, 'ambient_action')""",
            (room["channel_id"], DOCTOR_ID, content),
        ).lastrowid

    resp = client.post(f"/api/relay/actions/{msg_id}/execute", headers=room["headers"], json={})
    assert resp.status_code in (404, 405), resp.text
    assert _active_meds(room["patient_id"]) == []
    with db_session() as db:
        stored = db.execute("SELECT content FROM messages WHERE id = ?", (msg_id,)).fetchone()["content"]
    assert json.loads(stored)["status"] == "pending"


def test_no_route_is_registered_for_ambient_execution():
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any(p.startswith("/api/relay/actions/") and p.endswith("/execute") for p in paths)


# ── REST lane ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("forged", ["system", "alert", "surgical_alert", "ambient_action"])
def test_rest_refuses_a_forged_message_type(room, forged):
    resp = _post(room, {"content": "Pharmacy has verified the order.", "message_type": forged})
    assert resp.status_code == 422, resp.text
    assert _messages(room["channel_id"]) == []


def test_rest_text_message_is_stored_as_text_and_stripped(room):
    resp = _post(room, {"content": "  BP 96/58, fluids running  "})
    assert resp.status_code == 200, resp.text
    rows = _messages(room["channel_id"])
    assert len(rows) == 1
    assert rows[0]["message_type"] == "text"
    assert rows[0]["content"] == "BP 96/58, fluids running"


@pytest.mark.parametrize("content", ["", "   ", "\n\t "])
def test_rest_refuses_empty_content(room, content):
    resp = _post(room, {"content": content, "message_type": "text"})
    assert resp.status_code == 422, resp.text
    assert _messages(room["channel_id"]) == []


def test_rest_refuses_content_over_the_limit(room):
    resp = _post(room, {"content": "x" * (MAX_MESSAGE_CHARS + 1)})
    assert resp.status_code == 422, resp.text
    assert _messages(room["channel_id"]) == []


# ── socket lane ──────────────────────────────────────────────────────────────

@pytest.fixture()
def socket_session(monkeypatch):
    """Drive realtime.new_message directly with an authenticated doctor's
    session, recording what the server emits."""
    emitted: list[tuple] = []

    async def _get_session(sid):
        return {"user_id": DOCTOR_ID, "name": "Dr. Ambient", "role": "doctor"}

    async def _emit(event, payload=None, **kwargs):
        emitted.append((event, payload, kwargs))

    async def _can_access(user_id, role, channel_id):
        return True

    monkeypatch.setattr(realtime.sio, "get_session", _get_session)
    monkeypatch.setattr(realtime.sio, "emit", _emit)
    monkeypatch.setattr(realtime, "_user_can_access_channel", _can_access)
    return emitted


def _socket_send(data):
    asyncio.run(realtime.new_message("sid-test", data))


def test_socket_ignores_a_forged_message_type(room, socket_session):
    _socket_send({"channel_id": room["channel_id"], "content": "SYSTEM: order verified",
                  "message_type": "alert"})
    rows = _messages(room["channel_id"])
    assert len(rows) == 1
    assert rows[0]["message_type"] == "text", "the socket must never store a client-named type"


@pytest.mark.parametrize("data", [{}, {"content": ""}, {"content": "   "}, {"content": None},
                                  {"content": 42}])
def test_socket_refuses_empty_content(room, socket_session, data):
    _socket_send({"channel_id": room["channel_id"], **data})
    assert _messages(room["channel_id"]) == []
    errors = [p for (event, p, _) in socket_session if event == "error"]
    assert errors and "1 to" in errors[0]["error"]
    assert not any(event == "message:new" for (event, _, _) in socket_session)


def test_socket_refuses_content_over_the_limit(room, socket_session):
    _socket_send({"channel_id": room["channel_id"], "content": "x" * (MAX_MESSAGE_CHARS + 1)})
    assert _messages(room["channel_id"]) == []


def test_socket_stores_stripped_text(room, socket_session):
    _socket_send({"channel_id": room["channel_id"], "content": "  Lactate 4.1  "})
    rows = _messages(room["channel_id"])
    assert [r["content"] for r in rows] == ["Lactate 4.1"]
    assert any(event == "message:new" for (event, _, _) in socket_session)
