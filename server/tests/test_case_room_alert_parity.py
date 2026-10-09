"""The shared case-room writer (lis-15): Vigil's alert is unchanged, the
laboratory's post is its own kind, and both go through one writer.

services/case_room_alerts.py was extracted from
ingestion_service._persist_relay_alert so the laboratory's critical-result
post could use the same room lookup, message row and live body. Pinned here:

  * the Vigil wrapper still writes exactly the row it always wrote (Orb
    Vigil, 'intelligence', 'alert', is_bot 1, source 'bot', no idempotency
    key), returns the same five values, and still emits its shadow events
    on its own transaction: alert.persisted with a room, alert.dropped
    without one;
  * it reaches the room only through case_room_alerts (no second copy of
    the insert in ingestion_service), and so does the laboratory's
    rejection notice;
  * a laboratory post through the same writer is stored with is_bot 0 and
    source 'lab', and an idempotency key never posts twice;
  * the live push is 'message:new' to the room, with the body a reload
    shows.

tests/test_ingestion_alert_parity.py (the feed lane end to end) is unchanged
and must stay green beside this file.
"""
from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path

import pytest

from auth import hash_password
from database import db_session
from services import case_room_alerts, ingestion_service, shadow_service
from services.relay_service import get_channel_messages

SERVER = Path(__file__).resolve().parent.parent


@pytest.fixture()
def room():
    pid = f"PATCRA{uuid.uuid4().hex[:6].upper()}"
    channel = f"crap-{pid.lower()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Parity {pid}", f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO channels (id, name, type, patient_id) "
                   "VALUES (?, ?, 'case_room', ?)", (channel, f"Case room {pid}", pid))
    yield pid, channel
    with db_session() as db:
        db.execute("DELETE FROM messages WHERE channel_id = ?", (channel,))
        db.execute("DELETE FROM channels WHERE id = ?", (channel,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


@pytest.fixture()
def roomless():
    pid = f"PATCRN{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"No room {pid}", f"MRN-{uuid.uuid4().hex[:8]}"))
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


@pytest.fixture()
def technologist():
    uid = f"CRATECH{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
                   (uid, f"Tech {uid}", "lab_technician", hash_password("password")))
    return uid


def _row(message_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())


def _reloaded(channel, message_id):
    (row,) = [m for m in get_channel_messages(channel) if m["id"] == message_id]
    return row


# ── Vigil's alert, unchanged ─────────────────────────────────────────────────

def test_the_vigil_alert_row_is_what_it_always_was(room):
    pid, channel = room
    logged, msg_id, channel_id, msg_data, name = ingestion_service._persist_relay_alert(
        pid, "VIGIL ALERT [TEST]: parity")
    assert (logged, channel_id, name) == (True, channel, f"Parity {pid}")
    row = _row(msg_id)
    assert (row["sender_id"], row["sender_name"], row["sender_role"]) == (
        ingestion_service.VIGIL_BOT_ID, "Orb Vigil", "intelligence")
    assert (row["message_type"], row["is_bot"], row["source"]) == ("alert", 1, "bot")
    assert row["idempotency_key"] is None
    assert row["content"] == "VIGIL ALERT [TEST]: parity"
    assert msg_data == _reloaded(channel, msg_id)


def test_with_no_case_room_nothing_is_written_and_the_answer_is_unchanged(roomless):
    assert ingestion_service._persist_relay_alert(roomless, "VIGIL ALERT: nobody") == (
        False, None, None, None, "Unknown")


def test_the_shadow_events_stay_on_the_wrapper(room, roomless, monkeypatch):
    seen = []

    def record(kind, episode_id, lane, **kwargs):
        seen.append((kind, episode_id, lane, kwargs.get("db") is not None,
                     kwargs.get("payload")))

    monkeypatch.setattr(shadow_service, "emit", record)
    pid, channel = room
    _, msg_id, _, _, _ = ingestion_service._persist_relay_alert(
        pid, "VIGIL ALERT: shadow", "EP-1", "critical", "corr-1")
    ingestion_service._persist_relay_alert(roomless, "VIGIL ALERT: shadow", "EP-2", "critical",
                                           "corr-2")
    assert seen[0] == ("alert.persisted", "EP-1", "ingestion", True,
                       {"message_id": msg_id, "channel_id": channel})
    assert seen[1] == ("alert.dropped", "EP-2", "ingestion", True,
                       {"reason": "no_case_room", "level": "critical"})


# ── One writer ───────────────────────────────────────────────────────────────

def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} has no {name}")


def _calls(node: ast.AST) -> set[str]:
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            out.add(func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else "")
    return out


def test_the_vigil_wrapper_reaches_the_room_only_through_the_shared_writer():
    path = SERVER / "services" / "ingestion_service.py"
    wrapper = _function(path, "_persist_relay_alert")
    assert "persist_case_room_alert" in _calls(wrapper)
    assert "INSERT INTO messages" not in path.read_text(encoding="utf-8"), (
        "a second copy of the case-room insert came back into ingestion_service")


def test_the_laboratory_rejection_notice_uses_the_shared_writer_too():
    path = SERVER / "services" / "lab_order_service.py"
    assert "insert_message" in _calls(_function(path, "_insert_ward_notice"))
    assert "INSERT INTO messages" not in path.read_text(encoding="utf-8")


# ── The laboratory's post through the same writer ────────────────────────────

def test_a_laboratory_post_is_stored_as_the_laboratorys_with_its_own_provenance(
        room, technologist):
    pid, channel = room
    with db_session() as db:
        logged, msg_id, channel_id, msg_data = case_room_alerts.persist_case_room_alert(
            pid, "Critical laboratory result: test", sender_id=technologist,
            sender_name="Laboratory", sender_role="laboratory", is_bot=False, source="lab",
            db=db, message_type="alert", idempotency_key=f"lab-critical:T-{pid}:t1")
    assert logged and channel_id == channel
    row = _row(msg_id)
    assert (row["sender_id"], row["sender_name"], row["sender_role"]) == (
        technologist, "Laboratory", "laboratory")
    assert (row["message_type"], row["is_bot"], row["source"]) == ("alert", 0, "lab")
    assert msg_data == _reloaded(channel, msg_id)
    assert msg_data["is_bot"] is False and msg_data["source"] == "lab"


def test_an_idempotency_key_never_posts_twice(room, technologist):
    pid, channel = room
    key = f"lab-critical:T-{pid}:t2"
    with db_session() as db:
        first = case_room_alerts.persist_case_room_alert(
            pid, "once", sender_id=technologist, sender_name="Laboratory",
            sender_role="laboratory", is_bot=False, source="lab", db=db, idempotency_key=key)
    with db_session() as db:
        again = case_room_alerts.persist_case_room_alert(
            pid, "twice", sender_id=technologist, sender_name="Laboratory",
            sender_role="laboratory", is_bot=False, source="lab", db=db, idempotency_key=key)
        assert case_room_alerts.message_for_key(db, key)["id"] == first[1]
    assert again[1] == first[1]
    with db_session() as db:
        count = db.execute("SELECT COUNT(*) FROM messages WHERE channel_id = ?",
                           (channel,)).fetchone()[0]
    assert count == 1


def test_the_writer_changes_nothing_with_no_case_room(roomless, technologist):
    with db_session() as db:
        assert case_room_alerts.persist_case_room_alert(
            roomless, "nobody", sender_id=technologist, sender_name="Laboratory",
            sender_role="laboratory", is_bot=False, source="lab", db=db) == (
            False, None, None, None)


# ── The live push ────────────────────────────────────────────────────────────

def test_the_push_is_message_new_to_the_room(monkeypatch):
    from ws import realtime

    sent = []

    async def emit(event, data=None, room=None, **kwargs):
        sent.append((event, data, room))

    monkeypatch.setattr(realtime.sio, "emit", emit)
    body = {"id": 7, "channel_id": "room-7", "is_bot": False, "source": "lab"}

    async def scenario():
        assert case_room_alerts.push_message_new(body, "room-7", what="parity") is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert await case_room_alerts.emit_message_new(body, "room-7") is True

    asyncio.run(scenario())
    assert sent == [("message:new", body, "room-7"), ("message:new", body, "room-7")]


def test_nothing_is_pushed_without_a_body_or_a_room():
    assert case_room_alerts.push_message_new(None, "room") is False
    assert case_room_alerts.push_message_new({"id": 1}, None) is False
