"""Relay: older messages are reachable page by page (F088).

GET /api/relay/channels/{id}/messages returned the newest 50 and the client
sent no offset, so a critical patient's case room hid everything before its
last 50 messages. `before_id` now returns the page just before a given message
(a cursor, so live arrivals cannot shift the page edge), oldest first, in the
same message shape as every other Relay read.
"""
import uuid

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)


def _room_with_messages(n: int):
    channel = f"ch-older-{uuid.uuid4().hex[:8]}"
    uid = f"RLYOLD{uuid.uuid4().hex[:6].upper()}"
    ids = []
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Dr Older', 'admin', 'x')", (uid,))
        db.execute("INSERT INTO channels (id, name, type) VALUES (?, 'Older messages room', 'department')", (channel,))
        for i in range(n):
            cur = db.execute(
                "INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, timestamp) "
                "VALUES (?, ?, 'Dr Older', 'doctor', ?, ?)",
                (channel, uid, f"note {i:03d}", f"2026-10-01 00:{i // 60:02d}:{i % 60:02d}"),
            )
            ids.append(int(cur.lastrowid))
    return channel, uid, ids


def _get(channel, uid, query=""):
    return client.get(f"/api/relay/channels/{channel}/messages{query}",
                      headers={"Authorization": f"Bearer {create_token(uid, 'admin')}"})


def test_a_second_page_is_the_messages_just_before_the_first():
    channel, uid, ids = _room_with_messages(120)
    first = _get(channel, uid, "?limit=50")
    assert first.status_code == 200, first.text
    first_ids = [m["id"] for m in first.json()]
    assert first_ids == ids[-50:]

    second = _get(channel, uid, f"?limit=50&before_id={first_ids[0]}")
    assert second.status_code == 200, second.text
    assert [m["id"] for m in second.json()] == ids[20:70]

    third = _get(channel, uid, f"?limit=50&before_id={ids[20]}")
    assert [m["id"] for m in third.json()] == ids[:20]     # a short page: the start


def test_the_page_carries_the_relay_message_shape():
    channel, uid, ids = _room_with_messages(3)
    page = _get(channel, uid, f"?before_id={ids[-1]}").json()
    assert [m["content"] for m in page] == ["note 000", "note 001"]
    assert {"is_bot", "source", "sender_name", "timestamp"} <= set(page[0])


def test_an_anchor_from_another_channel_returns_nothing():
    channel, uid, _ = _room_with_messages(3)
    _other, _uid, other_ids = _room_with_messages(3)
    assert _get(channel, uid, f"?before_id={other_ids[-1]}").json() == []


def test_a_new_message_does_not_shift_an_earlier_page():
    channel, uid, ids = _room_with_messages(60)
    before = [m["id"] for m in _get(channel, uid, f"?limit=10&before_id={ids[30]}").json()]
    with db_session() as db:
        db.execute(
            "INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, timestamp) "
            "VALUES (?, ?, 'Dr Older', 'doctor', 'arrived while reading', '2026-10-01 01:30:00')",
            (channel, uid),
        )
    after = [m["id"] for m in _get(channel, uid, f"?limit=10&before_id={ids[30]}").json()]
    assert before == after == ids[20:30]
