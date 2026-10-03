"""Deferred C — relay message idempotency.

Offline-queued relay messages replay with the same Idempotency-Key. The server
must dedupe (return the original message, not double-post) so a lost-response
retry can't spam a channel — the same guarantee already given to notes and
medication orders.
"""
import uuid

from fastapi.testclient import TestClient
from main import app
from database import db_session
from auth import hash_password

client = TestClient(app)

DOCTOR_ID = "D-RELAY-IDEMP"


def _setup():
    suffix = uuid.uuid4().hex[:6].upper()
    channel_id = f"chan-idemp-{suffix}"
    with db_session() as db:
        db.execute(
            """INSERT OR IGNORE INTO users (id, name, role, department, password_hash)
               VALUES (?, 'Dr. Relay', 'doctor', 'General Medicine', ?)""",
            (DOCTOR_ID, hash_password("testpass123")),
        )
        # Channel name must overlap the sender's department: the post path enforces
        # channel membership (403 otherwise), and department channels are visible
        # only when the user's department matches the channel name.
        db.execute(
            "INSERT INTO channels (id, name, type) VALUES (?, 'General Medicine Idemp Room', 'department')",
            (channel_id,),
        )
    resp = client.post("/api/auth/login", json={"employee_id": DOCTOR_ID, "password": "testpass123"})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    return channel_id, {"Authorization": f"Bearer {token}"}


def _count_messages(channel_id: str, content: str) -> int:
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id = ? AND content = ?",
            (channel_id, content),
        ).fetchone()[0]


def test_repeated_key_dedupes_to_one_message():
    channel_id, headers = _setup()
    key = str(uuid.uuid4())
    content = "Patient stable, continue current plan."

    first = client.post(
        f"/api/relay/channels/{channel_id}/messages",
        headers={**headers, "Idempotency-Key": key},
        json={"content": content, "message_type": "text"},
    )
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    # Same key (offline replay) → same message, no duplicate row.
    second = client.post(
        f"/api/relay/channels/{channel_id}/messages",
        headers={**headers, "Idempotency-Key": key},
        json={"content": content, "message_type": "text"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first_id
    # The replay marker is internal — it must not leak to the client.
    assert "idempotent_replay" not in second.json()
    assert _count_messages(channel_id, content) == 1


def test_no_key_never_dedupes():
    """Ordinary live sends (no key) post every time — identical text twice = two rows."""
    channel_id, headers = _setup()
    content = "Repeat with no idempotency key."
    for _ in range(2):
        resp = client.post(
            f"/api/relay/channels/{channel_id}/messages",
            headers=headers,
            json={"content": content, "message_type": "text"},
        )
        assert resp.status_code == 200, resp.text
    assert _count_messages(channel_id, content) == 2
