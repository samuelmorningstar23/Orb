"""A message pushed live must be the message a reload shows.

WHY THIS FILE EXISTS (2026-10-03). Relay badges an automated post with an AUTO
chip from `is_bot` (RelayPanel). The history a reload fetches
(relay_service.get_channel_messages) carries `is_bot` and `source`, and so does
everything sent through relay_service.send_message. But four writers of
automated alerts hand-built their own 8-key `message:new` body without either
field:

  * ingestion_service._persist_relay_alert (the monitor-feed, nurse obs and
    escalation lanes all post through it),
  * bot_service._persist_alerts (the simulator lane),
  * the surgical team alert in routes/surgery_routes.py,
  * the Sage surgical handoff in services/sage_handoff_service.py.

So every live Vigil alert appeared in an open case room as if a person had
typed it, and gained its AUTO chip only after a reload. Found in the browser
while verifying the nurse lane's live push.

The rule: a live body is relay_service._message_row_to_dict(row), the same
serializer send_message uses. The source guard at the bottom keeps a fifth
hand-built copy from appearing.
"""
from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from database import db_session
from services import bot_service, ingestion_service
from services.relay_service import get_channel_messages

SERVER = Path(__file__).resolve().parent.parent


@pytest.fixture()
def patient_room():
    pid = f"PATLA{uuid.uuid4().hex[:6].upper()}"
    room = f"larm-{pid.lower()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
            (pid, f"Live Alert {pid}", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            (room, f"Case room {pid}", pid),
        )
    yield pid, room
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _as_reloaded(room: str, msg_id: int) -> dict:
    (row,) = [m for m in get_channel_messages(room) if m["id"] == msg_id]
    return row


def test_the_shared_alert_writer_pushes_what_a_reload_shows(patient_room):
    """Monitor feed, nurse obs and escalation all post through this writer."""
    pid, room = patient_room
    logged, msg_id, channel_id, msg_data, _name = ingestion_service._persist_relay_alert(
        pid, "VIGIL ALERT [TEST]: shape check")

    assert logged and channel_id == room
    assert msg_data == _as_reloaded(room, msg_id)
    assert msg_data["is_bot"] is True
    assert msg_data["source"] == "bot"


def test_the_simulator_alert_writer_pushes_what_a_reload_shows(patient_room):
    pid, room = patient_room
    out = bot_service._persist_alerts(
        [{"pid": pid, "content": "VIGIL ALERT: shape check", "risk_level": "critical"}])

    (item,) = [o for o in out if o["alert"]["pid"] == pid]
    assert item["msg_data"] == _as_reloaded(room, item["msg_id"])
    assert item["msg_data"]["is_bot"] is True


def test_resolving_an_alert_keeps_it_the_message_a_reload_shows(patient_room, monkeypatch):
    """RelayPanel REPLACES the message with the `message:update` body, so a body
    without `is_bot` stripped the AUTO chip from a resolved alert in every open
    room."""
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import ws.realtime as realtime
    from auth import create_token
    from routes.relay_routes import router as relay_router

    pid, room = patient_room
    uid = f"DOCLA{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Dr Live Alert', 'doctor', 'x', 'Live Alert Ward')", (uid,))
        msg_id = db.execute(
            "INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, "
            "message_type, is_bot, source) VALUES (?, 'system', 'Orb Vigil', 'intelligence', ?, "
            "'alert', 1, 'bot')",
            (room, json.dumps({"title": "NEWS2 9", "status": "active", "patient_id": pid})),
        ).lastrowid

    updates = []

    async def _record(event, data=None, room=None, **kw):
        if event == "message:update":
            updates.append(data)

    monkeypatch.setattr(realtime.sio, "emit", _record)
    app = FastAPI()
    app.include_router(relay_router)
    r = TestClient(app).post(
        f"/api/relay/alerts/{msg_id}/resolve", json={"action": "confirm"},
        headers={"Authorization": f"Bearer {create_token(uid, 'doctor')}"})
    assert r.status_code == 200, r.text

    (body,) = updates
    assert body == _as_reloaded(room, msg_id)
    assert body["is_bot"] is True
    assert json.loads(body["content"])["status"] == "resolved"


# ── source guard ─────────────────────────────────────────────────────────────
#: The keys every hand-built message body shares. A dict literal holding all of
#: them is a message body, and a message body without these two is the bug.
_MESSAGE_KEYS = {"id", "channel_id", "sender_id", "sender_name", "content",
                 "message_type", "timestamp"}
_PROVENANCE = {"is_bot", "source"}


def test_no_message_body_is_built_without_its_provenance():
    offenders = []
    for path in sorted((SERVER / "services").glob("*.py")) + sorted((SERVER / "routes").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if _MESSAGE_KEYS <= keys and not _PROVENANCE <= keys:
                offenders.append(f"{path.relative_to(SERVER)}:{node.lineno}")
    assert offenders == [], (
        "message bodies without is_bot/source (use relay_service._message_row_to_dict): "
        + ", ".join(offenders))
