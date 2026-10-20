"""Case-room posts: the one writer of an automated message into a patient's
case room (lis-15).

WHY ONE WRITER. Vigil's deterioration alert (ingestion_service.
_persist_relay_alert, which the monitor feed, the nurse observation lane and
the escalation ladder all post through), the laboratory's critical-result
post (services/lab_critical_service.py) and the laboratory's plain notices
(a rejected sample, an amended or withdrawn result) all do the same three
things: find the patient's case room, insert one message row, and build the
body the live push carries. Each used to do it in its own words. This module
is that step once, so a fix to it (the live body's provenance, the room
lookup) reaches every lane.

WHAT IT DOES NOT DO
  * It opens no connection and never commits. Every function takes the
    caller's open transaction (`db`), so the message and whatever the caller
    records about it (a shadow event, a notification's delivery status)
    commit together or not at all.
  * It decides nothing about who should be told. A discharged patient's case
    room is hidden from every clinician (relay_service.get_channels_for_user),
    so a caller for whom that matters (the laboratory) checks the patient
    first; Vigil's alert never posted for a discharged patient anyway, and its
    behaviour here is unchanged.
  * It never pushes inside the transaction. push_message_new and
    emit_message_new are for AFTER the commit: a socket emit awaited while
    the write lock is held parks every other writer behind it.

THE LIVE BODY is relay_service._message_row_to_dict(row), the serializer the
history endpoint and send_message share, so a pushed message carries is_bot
and source exactly as a reload shows them (tests/test_live_alert_message_shape).

PER-TIER IDEMPOTENCY. A caller that must never post the same thing twice
(each tier of a laboratory critical) passes an idempotency key. The key is
stored on the message (migration 2's unique messages.idempotency_key index),
and message_for_key finds a post that already went out, so a retry after a
crash between the post and its bookkeeping never doubles the post.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("orb.case_room_alerts")


def case_room_id(db, patient_id: str) -> str | None:
    """The patient's case room, or None when the patient has none.

    One room per patient is the writer's rule (W6-B); the order makes the
    answer deterministic if an old database ever holds two."""
    if not patient_id:
        return None
    row = db.execute(
        "SELECT id FROM channels WHERE type = 'case_room' AND patient_id = ? "
        "ORDER BY created_at, id LIMIT 1",
        (patient_id,),
    ).fetchone()
    return row["id"] if row else None


def message_for_key(db, idempotency_key: str | None) -> dict | None:
    """The message already posted under this key, as the live body, or None."""
    if not idempotency_key:
        return None
    row = db.execute(
        "SELECT * FROM messages WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    if row is None:
        return None
    from services.relay_service import _message_row_to_dict

    return _message_row_to_dict(row)


def insert_message(
    db,
    channel_id: str,
    content: str,
    *,
    sender_id: str,
    sender_name: str,
    sender_role: str,
    is_bot: bool,
    source: str,
    message_type: str = "alert",
    idempotency_key: str | None = None,
) -> tuple[int, dict | None]:
    """Insert one message into `channel_id` on the caller's transaction.
    Returns (message id, the live body or None if the row cannot be read
    back). messages.sender_id must name a users row."""
    if idempotency_key:
        cursor = db.execute(
            """INSERT INTO messages
               (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot,
                source, idempotency_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                channel_id,
                sender_id,
                sender_name,
                sender_role,
                content,
                message_type,
                1 if is_bot else 0,
                source,
                idempotency_key,
            ),
        )
    else:
        cursor = db.execute(
            """INSERT INTO messages
               (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot,
                source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                channel_id,
                sender_id,
                sender_name,
                sender_role,
                content,
                message_type,
                1 if is_bot else 0,
                source,
            ),
        )
    message_id = int(cursor.lastrowid)
    row = db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    msg_data = None
    if row:
        from services.relay_service import _message_row_to_dict

        msg_data = _message_row_to_dict(row)
    return message_id, msg_data


def persist_case_room_alert(
    patient_id: str,
    content: str,
    *,
    sender_id: str,
    sender_name: str,
    sender_role: str,
    is_bot: bool,
    source: str,
    db,
    message_type: str = "alert",
    idempotency_key: str | None = None,
) -> tuple[bool, int | None, str | None, dict | None]:
    """Post `content` into the patient's case room on the caller's transaction.

    Returns (logged, message_id, channel_id, msg_data):
      * logged False, everything else None: the patient has no case room, so
        nothing was written and nobody was told;
      * otherwise the stored message and its live body, for the caller to
        push AFTER its commit (push_message_new / emit_message_new).
    With an idempotency key already used, the earlier message is returned
    and nothing new is written."""
    channel_id = case_room_id(db, patient_id)
    if channel_id is None:
        return False, None, None, None
    if idempotency_key:
        earlier = message_for_key(db, idempotency_key)
        if earlier is not None:
            return True, int(earlier["id"]), earlier.get("channel_id") or channel_id, earlier
    message_id, msg_data = insert_message(
        db,
        channel_id,
        content,
        sender_id=sender_id,
        sender_name=sender_name,
        sender_role=sender_role,
        is_bot=is_bot,
        source=source,
        message_type=message_type,
        idempotency_key=idempotency_key,
    )
    return True, message_id, channel_id, msg_data


def push_message_new(
    msg_data: dict | None, channel_id: str | None, *, what: str = "case-room post"
) -> bool:
    """Push a committed message live as 'message:new' to its room, from SYNC
    code (a def route's worker thread). Never raises; returns True when the
    emit was scheduled. With no event loop reachable (a script, a unit test
    calling a service directly) the push is skipped: the row is stored and
    shows on the next load."""
    if not msg_data or not channel_id:
        return False
    try:
        from ws import realtime

        return realtime.schedule_on_loop(
            lambda: realtime.sio.emit("message:new", msg_data, room=channel_id),
            f"message:new for {what} {msg_data.get('id')}",
        )
    except Exception:
        logger.warning("%s not pushed live", what, exc_info=True)
        return False


async def emit_message_new(
    msg_data: dict | None, channel_id: str | None, *, what: str = "case-room post"
) -> bool:
    """The same push from async code already on the event loop (a background
    sweep, after its run_db returned). Never raises."""
    if not msg_data or not channel_id:
        return False
    try:
        from ws import realtime

        await realtime.sio.emit("message:new", msg_data, room=channel_id)
        return True
    except Exception:
        logger.warning("%s not pushed live", what, exc_info=True)
        return False
