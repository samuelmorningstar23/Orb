"""A patient's red flag reaches a clinician, and the reply claims it only when true.

WHY THIS FILE EXISTS (found 2026-10-03). Bridge screens every portal question
for emergency and self-harm phrases before any model sees it, and on a match it
told the patient "Your care team has been notified". Nobody was:

  * the bridge.emergency.flag event carried patient_id None, because the /ask
    context came from get_patient_status_simple, whose dict has no id key;
  * the event's only subscriber is the ward-brief narrator, which rebuilds a
    summary card and tells no one;
  * the model's audit row for an ordinary Bridge answer had patient_id None for
    the same reason.

Now the flag is an alert in the patient's Relay case room from the Orb Bridge
bot, pushed live as message:new with relay_service's own serializer, and the
reply leaves out "Your care team has been notified" when the post did not land.
"""
import asyncio
import contextlib
import logging
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from database import SERVICE_ACCOUNT_IDS, db_session
from main import app
from services import bridge_service

client = TestClient(app)

PASSWORD = "testpass123"


# ── fixtures ────────────────────────────────────────────────────────────────

def _make_patient(*, case_room: bool = True, bed: str | None = "B-12") -> dict:
    from auth import hash_password
    tag = uuid.uuid4().hex[:8].upper()
    p = {"user_id": f"BRF-U-{tag}", "patient_id": f"BRF-P-{tag}",
         "channel_id": f"case-BRF-{tag}" if case_room else None, "bed": bed}
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, 'patient', ?)",
            (p["user_id"], "Red Flag Test", hash_password(PASSWORD)),
        )
        db.execute(
            "INSERT INTO patients (id, user_id, name, mrn, bed, ward) VALUES (?, ?, ?, ?, ?, ?)",
            (p["patient_id"], p["user_id"], "Red Flag Test", f"MRN-BRF-{tag}", bed,
             "General Medicine"),
        )
        if case_room:
            db.execute(
                "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                (p["channel_id"], "Case Room: Red Flag Test", p["patient_id"]),
            )
    return p


def _drop_patient(p: dict) -> None:
    with db_session() as db:
        if p["channel_id"]:
            db.execute("DELETE FROM messages WHERE channel_id = ?", (p["channel_id"],))
            db.execute("DELETE FROM channels WHERE id = ?", (p["channel_id"],))
        db.execute("DELETE FROM patients WHERE id = ?", (p["patient_id"],))
        db.execute("DELETE FROM users WHERE id = ?", (p["user_id"],))


@pytest.fixture
def patient():
    p = _make_patient()
    yield p
    _drop_patient(p)


@pytest.fixture
def patient_without_case_room():
    p = _make_patient(case_room=False)
    yield p
    _drop_patient(p)


@pytest.fixture
def emits(monkeypatch):
    """Socket emits, recorded instead of sent."""
    from ws import realtime
    sent = []

    async def _emit(event, data=None, room=None, **kw):
        sent.append((event, data, room))
    monkeypatch.setattr(realtime.sio, "emit", _emit)
    return sent


@pytest.fixture
def published(monkeypatch):
    """bridge.emergency.flag events, recorded instead of published."""
    from services.event_bus import event_bus
    seen = []

    async def _publish(event):
        seen.append(event)
    monkeypatch.setattr(event_bus, "publish", _publish)
    return seen


@pytest.fixture
def no_model(monkeypatch):
    """A red flag must never reach the model."""
    from services import ollama_gateway

    def _boom(*a, **kw):
        raise AssertionError("a red-flag question reached the model")
    monkeypatch.setattr(ollama_gateway, "stream", _boom)
    monkeypatch.setattr(ollama_gateway, "post", _boom)


def _token(user_id: str) -> str:
    r = client.post("/api/auth/login", json={"employee_id": user_id, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _ask(user_id: str, question: str) -> str:
    """POST /api/bridge/ask and return the reply text the portal would show."""
    import json
    r = client.post("/api/bridge/ask", json={"question": question},
                    headers={"Authorization": f"Bearer {_token(user_id)}"})
    assert r.status_code == 200, r.text
    parts = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            data = json.loads(line[len("data: "):])
            parts.append(data.get("content", ""))
    return "".join(parts)


def _room_messages(channel_id: str) -> list:
    with db_session() as db:
        return db.execute(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY id", (channel_id,)
        ).fetchall()


def _answer(question: str, context: dict) -> list:
    async def _collect():
        return [c async for c in bridge_service.answer_patient_question(question, context)]
    return asyncio.run(_collect())


# ── the id ──────────────────────────────────────────────────────────────────

def test_ask_context_carries_the_patient_id(patient):
    ctx = bridge_service.patient_ask_context(patient["user_id"])
    assert ctx["patient_id"] == patient["patient_id"]
    assert ctx["bed"] == "B-12"


def test_portal_status_payload_is_unchanged(patient):
    """The id is added for /ask only. /status returns the status dict to the
    portal as is, so it must not start carrying an id."""
    r = client.get("/api/bridge/status",
                   headers={"Authorization": f"Bearer {_token(patient['user_id'])}"})
    assert r.status_code == 200, r.text
    assert "patient_id" not in r.json() and "id" not in r.json()


# ── delivery, end to end through the route ──────────────────────────────────

def test_emergency_reaches_the_case_room(patient, emits, published, no_model):
    reply = _ask(patient["user_id"], "Nurse please my chest pain is really bad and spreading")
    assert reply == bridge_service.EMERGENCY_RESPONSE
    assert reply.endswith(bridge_service.CARE_TEAM_NOTIFIED)

    rows = _room_messages(patient["channel_id"])
    assert len(rows) == 1, [dict(r) for r in rows]
    msg = rows[0]
    assert msg["sender_id"] == bridge_service.BRIDGE_BOT_ID
    assert msg["sender_name"] == "Orb Bridge"
    assert msg["message_type"] == "alert"
    assert msg["is_bot"] == 1 and msg["source"] == "bot"
    assert "possible medical emergency" in msg["content"]
    assert '"chest pain"' in msg["content"]
    assert "bed B-12" in msg["content"]
    assert "does not page" in msg["content"]
    # Only the matched phrase, never the patient's free text.
    assert "spreading" not in msg["content"]


def test_live_push_is_the_relay_serializer_to_the_case_room(patient, emits, published, no_model):
    from services.relay_service import _message_row_to_dict
    _ask(patient["user_id"], "I can't breathe")
    row = _room_messages(patient["channel_id"])[0]
    pushes = [e for e in emits if e[0] == "message:new"]
    assert pushes == [("message:new", _message_row_to_dict(row), patient["channel_id"])]
    assert pushes[0][1]["is_bot"] is True and pushes[0][1]["source"] == "bot"


def test_the_event_carries_the_real_patient_id(patient, emits, published, no_model):
    _ask(patient["user_id"], "I think I'm having a heart attack")
    flags = [e for e in published if e.topic == "bridge.emergency.flag"]
    assert len(flags) == 1
    assert flags[0].patient_id == patient["patient_id"]
    assert flags[0].payload["category"] == "emergency"


def test_self_harm_reaches_the_case_room(patient, emits, published, no_model):
    reply = _ask(patient["user_id"], "honestly I want to die")
    assert reply == bridge_service.SELF_HARM_RESPONSE
    rows = _room_messages(patient["channel_id"])
    assert len(rows) == 1
    assert "self-harm or suicide" in rows[0]["content"]
    assert '"want to die"' in rows[0]["content"]
    assert "honestly" not in rows[0]["content"]
    flags = [e for e in published if e.topic == "bridge.emergency.flag"]
    assert flags[0].patient_id == patient["patient_id"]
    assert flags[0].payload["category"] == "self_harm"


def test_an_ordinary_question_posts_nothing(patient, emits, published, monkeypatch):
    """And the model's audit row now names the patient."""
    from services import model_router, ollama_gateway
    from services.llm_queue_service import llm_queue
    from services.model_router import ModelChoice

    async def _select(preferred=None, **kw):
        return ModelChoice("medgemma:27b", False, "medgemma:27b")
    monkeypatch.setattr(model_router, "select_chat_model", _select)
    seen = []

    class _Stream:
        async def aiter_lines(self):
            yield '{"message": {"content": "Hello"}, "done": true}'

        async def aclose(self):
            return None

    @contextlib.asynccontextmanager
    async def _stream(client_, path, payload, **kw):
        seen.append(kw)
        yield _Stream()

    async def _acquire(*a, **k):
        return None
    monkeypatch.setattr(ollama_gateway, "stream", _stream)
    monkeypatch.setattr(llm_queue, "acquire", _acquire)
    monkeypatch.setattr(llm_queue, "release", lambda *a, **k: None)

    assert _ask(patient["user_id"], "When is lunch served?") == "Hello"
    assert _room_messages(patient["channel_id"]) == []
    assert not [e for e in emits if e[0] == "message:new"]
    assert seen[0]["purpose"] == "bridge_patient_answer"
    assert seen[0]["patient_id"] == patient["patient_id"]


# ── when it does not land, the reply does not claim it ──────────────────────

def test_no_case_room_means_no_claim(patient_without_case_room, emits, published,
                                     no_model, caplog):
    p = patient_without_case_room
    with caplog.at_level(logging.ERROR, logger="services.bridge_service"):
        reply = _ask(p["user_id"], "my face is drooping")
    assert reply == bridge_service.EMERGENCY_RESPONSE_UNSENT
    assert bridge_service.CARE_TEAM_NOTIFIED not in reply
    assert "call button" in reply
    assert any("NO case room" in r.getMessage() for r in caplog.records)
    # The narrator still hears it, with the right patient.
    assert published[0].patient_id == p["patient_id"]


def test_no_patient_record_means_no_claim(emits, published, no_model, caplog):
    with caplog.at_level(logging.ERROR, logger="services.bridge_service"):
        chunks = _answer("I can't breathe", {"status": "unknown"})
    assert chunks == [bridge_service.EMERGENCY_RESPONSE_UNSENT]
    assert any("no patient record" in r.getMessage() for r in caplog.records)


def test_a_failed_post_means_no_claim(patient, emits, published, no_model, monkeypatch):
    def _fail(*a, **kw):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(bridge_service, "_store_red_flag", _fail)
    ctx = bridge_service.patient_ask_context(patient["user_id"])
    assert _answer("I can't breathe", ctx) == [bridge_service.EMERGENCY_RESPONSE_UNSENT]


def test_a_slow_post_does_not_hold_the_reply_and_still_lands(patient, emits, published,
                                                             no_model, monkeypatch):
    real = bridge_service._store_red_flag

    def _slow(*a, **kw):
        time.sleep(0.4)
        return real(*a, **kw)
    monkeypatch.setattr(bridge_service, "_store_red_flag", _slow)
    monkeypatch.setattr(bridge_service, "_FLAG_DELIVERY_WAIT_SECONDS", 0.05)
    ctx = bridge_service.patient_ask_context(patient["user_id"])

    async def scenario():
        started = time.monotonic()
        chunks = [c async for c in bridge_service.answer_patient_question("I can't breathe", ctx)]
        waited = time.monotonic() - started
        await asyncio.gather(*list(bridge_service._FLAG_TASKS))
        return chunks, waited

    chunks, waited = asyncio.run(scenario())
    assert chunks == [bridge_service.EMERGENCY_RESPONSE_UNSENT]
    assert waited < 0.35, waited
    assert len(_room_messages(patient["channel_id"])) == 1


def test_closing_the_page_does_not_cancel_the_post(patient, emits, published, no_model,
                                                   monkeypatch):
    """The streaming response is cancelled when the patient's browser goes away.
    The delivery is its own task, so it carries on."""
    real = bridge_service._store_red_flag

    def _slow(*a, **kw):
        time.sleep(0.3)
        return real(*a, **kw)
    monkeypatch.setattr(bridge_service, "_store_red_flag", _slow)
    ctx = bridge_service.patient_ask_context(patient["user_id"])

    async def scenario():
        gen = bridge_service.answer_patient_question("I can't breathe", ctx)
        consumer = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)
        assert not consumer.done()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        await asyncio.gather(*list(bridge_service._FLAG_TASKS))

    asyncio.run(scenario())
    rows = _room_messages(patient["channel_id"])
    assert len(rows) == 1 and rows[0]["sender_id"] == bridge_service.BRIDGE_BOT_ID
    assert [e[0] for e in emits] == ["message:new"]


# ── the posting identity ────────────────────────────────────────────────────

def test_the_bridge_bot_is_a_service_account():
    """A bot row has role 'doctor' and a sentinel hash; one admin password reset
    would mint a login (database.SERVICE_ACCOUNT_IDS). It must be on the list."""
    assert bridge_service.BRIDGE_BOT_ID in SERVICE_ACCOUNT_IDS


def test_the_unsent_replies_are_the_full_replies_minus_the_claim():
    assert bridge_service.EMERGENCY_RESPONSE == (
        f"{bridge_service.EMERGENCY_RESPONSE_UNSENT} {bridge_service.CARE_TEAM_NOTIFIED}")
    assert bridge_service.SELF_HARM_RESPONSE == (
        f"{bridge_service.SELF_HARM_RESPONSE_UNSENT} {bridge_service.CARE_TEAM_NOTIFIED}")
    for text in (bridge_service.EMERGENCY_RESPONSE_UNSENT,
                 bridge_service.SELF_HARM_RESPONSE_UNSENT):
        assert "notified" not in text
