"""Relay orders messages by when they were sent, whatever spelling the
timestamp was stored in.

messages.timestamp holds SQLite's CURRENT_TIMESTAMP ("2026-10-01 09:00:00")
beside ISO strings the seed and the admission path wrote
("2026-10-01T08:00:00+00:00"). As text 'T' sorts after ' ', so an 08:00
welcome line outranked a 09:00 message: the department room showed the welcome
as its latest message and the thread put it last.
"""
import uuid

from database import db_session
from services.relay_service import get_channel_messages, get_channels_for_user


def _setup():
    stem = f"MTS{uuid.uuid4().hex[:6].upper()}"
    uid, cid = f"{stem}U", f"{stem}-DM"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Mixed Stamp Reader', 'doctor', 'General Medicine', 'x')", (uid,))
        db.execute("INSERT INTO channels (id, name, type) VALUES (?, 'Mixed stamps', 'direct')", (cid,))
        db.execute("INSERT INTO channel_members (channel_id, user_id, source) VALUES (?, ?, 'added')",
                   (cid, uid))
    return uid, cid


def _post(cid, content, ts):
    with db_session() as db:
        db.execute(
            "INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, timestamp) "
            "VALUES (?, 'system', 'System', 'system', ?, ?)", (cid, content, ts))


def test_thread_and_room_list_follow_the_instant_not_the_spelling():
    uid, cid = _setup()
    _post(cid, "welcome at 08:00", "2026-10-01T08:00:00.123456+00:00")
    _post(cid, "ward round at 09:00", "2026-10-01 09:00:00")
    _post(cid, "handover at 08:30 IST-stamped", "2026-10-01T14:00:00+05:30")  # 08:30 UTC

    thread = [m["content"] for m in get_channel_messages(cid)]
    assert thread == ["welcome at 08:00", "handover at 08:30 IST-stamped", "ward round at 09:00"]

    room = next(c for c in get_channels_for_user(uid, "doctor") if c["id"] == cid)
    assert room["last_message"] == "ward round at 09:00"
    assert room["last_activity"] == "2026-10-01 09:00:00"


def test_same_second_messages_keep_their_send_order():
    _uid, cid = _setup()
    for i in range(3):
        _post(cid, f"msg {i}", "2026-10-01 10:00:00")
    assert [m["content"] for m in get_channel_messages(cid)] == ["msg 0", "msg 1", "msg 2"]


def test_history_rows_carry_the_shared_serializer_fields():
    _uid, cid = _setup()
    _post(cid, "hello", "2026-10-01 10:00:00")
    (msg,) = get_channel_messages(cid)
    assert set(msg) >= {"id", "channel_id", "sender_id", "sender_name", "sender_role",
                        "content", "message_type", "timestamp", "is_bot", "source"}
