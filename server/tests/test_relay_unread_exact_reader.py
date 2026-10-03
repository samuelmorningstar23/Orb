"""Relay unread badge: a read counts only for the user who read it (F087).

messages.read_by is a JSON array of user ids. The badge counted with
read_by NOT LIKE '%<id>%', so 'DOC1' matched inside 'DOC10': once DOC10 opened a
room, DOC1's badge for it read zero although DOC1 had read nothing.
"""
import uuid

from database import db_session
from services.relay_service import get_channels_for_user, mark_channel_read


def _user(uid: str) -> None:
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, 'doctor', 'General Medicine', 'x')", (uid, f"User {uid}"))


def _dm(*members: str) -> str:
    cid = f"UNREAD-{uuid.uuid4().hex[:10].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO channels (id, name, type) VALUES (?, 'DM', 'direct')", (cid,))
        for m in members:
            db.execute("INSERT INTO channel_members (channel_id, user_id, source) "
                       "VALUES (?, ?, 'added')", (cid, m))
    return cid


def _post(cid: str, sender: str, read_by: str = "[]") -> None:
    with db_session() as db:
        db.execute(
            "INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, "
            "content, read_by) VALUES (?, ?, 'Sender', 'doctor', 'Obs due', ?)",
            (cid, sender, read_by))


def _unread(cid: str, uid: str) -> int:
    rooms = {c["id"]: c for c in get_channels_for_user(uid, "doctor")}
    return rooms[cid]["unread_count"]


def test_a_read_by_doc10_does_not_clear_doc1s_badge():
    stem = f"UR{uuid.uuid4().hex[:6].upper()}"
    doc1, doc10, sender = f"{stem}1", f"{stem}10", f"{stem}S"
    for uid in (doc1, doc10, sender):
        _user(uid)
    cid = _dm(doc1, doc10, sender)
    _post(cid, sender)
    _post(cid, sender)

    assert mark_channel_read(cid, doc10) == 2

    assert _unread(cid, doc10) == 0
    assert _unread(cid, doc1) == 2, "DOC10's read was counted as DOC1's"

    mark_channel_read(cid, doc1)
    assert _unread(cid, doc1) == 0


def test_unreadable_read_by_still_counts_as_unread():
    """NULL or malformed read_by is 'nobody has read this', as
    mark_channel_read already reads it, and must not break the list."""
    stem = f"UR{uuid.uuid4().hex[:6].upper()}"
    reader, sender = f"{stem}R", f"{stem}S"
    for uid in (reader, sender):
        _user(uid)
    cid = _dm(reader, sender)
    _post(cid, sender, read_by=None)
    _post(cid, sender, read_by="not json")
    _post(cid, sender, read_by=f'["{reader}"]')

    assert _unread(cid, reader) == 2
