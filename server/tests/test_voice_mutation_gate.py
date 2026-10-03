"""The voice lane gets the chat lane's mutation gate, and /history shows what the
clinician read on a mutation turn (F034, guardrail lane parity).

  * A spoken "add paracetamol 1g" used to go to the model with only the phrase
    strip behind it, so a wording outside the forbidden list ("has been added")
    could be spoken. The voice lane now runs the same pre-LLM gate as the chat
    lane and says the chat lane's fixed message without generating.
  * /history projected the stored model narration on a mutation turn although
    the clinician had been shown Orb's safe message.
  * Spoken answers carried no source trail; the retrieved documents are now
    collected per request and recorded in provenance.
"""
import json
import uuid
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from routes import sage_routes
from routes.sage_routes import CHART_MUTATION_SAFE_MESSAGE, _strip_forbidden

client = TestClient(app)


@pytest.fixture()
def doctor():
    uid = f"VOXDOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Dr Voice', 'doctor', 'General Medicine', 'x')", (uid,))
    yield uid
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (uid,))


def _headers(uid):
    return {"Authorization": f"Bearer {create_token(uid, 'doctor')}"}


def _sse(response) -> list:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


class _ChattyAgent:
    """Would narrate a chart change the forbidden list does not cover."""
    called = False

    async def generate_stream(self, q, user, patient_id=None, conversation_history=None):
        _ChattyAgent.called = True
        yield "Paracetamol 1 g has been added to the chart."


def test_the_safe_message_survives_the_guardrail_strip():
    assert _strip_forbidden(CHART_MUTATION_SAFE_MESSAGE) == CHART_MUTATION_SAFE_MESSAGE


def test_a_spoken_chart_change_gets_the_chat_lanes_message_and_no_generation(doctor):
    _ChattyAgent.called = False
    with mock.patch("services.voice_agent_service.get_voice_agent", lambda: _ChattyAgent()), \
         mock.patch("config.VOICE", {"conversational": {"enabled": True,
                                                        "model_path": "models/voice-4b"}}):
        r = client.post("/api/sage/voice", headers=_headers(doctor),
                        json={"query": "add paracetamol 1g"})
    assert r.status_code == 200, r.text
    events = _sse(r)
    spoken = "".join(e.get("token", "") for e in events)
    assert spoken == CHART_MUTATION_SAFE_MESSAGE
    assert "has been added" not in spoken
    assert _ChattyAgent.called is False, "a mutation turn must never reach the model"
    assert events[-1]["done"] is True

    with db_session() as db:
        stored = [dict(x) for x in db.execute(
            "SELECT role, content, sources FROM sage_conversations WHERE user_id = ? ORDER BY id",
            (doctor,)).fetchall()]
    assert stored[-1]["content"] == f"[voice] {CHART_MUTATION_SAFE_MESSAGE}"
    assert json.loads(stored[-1]["sources"])["proposed_action"] is True


def test_a_spoken_question_still_reaches_the_model(doctor):
    _ChattyAgent.called = False
    with mock.patch("services.voice_agent_service.get_voice_agent", lambda: _ChattyAgent()), \
         mock.patch("config.VOICE", {"conversational": {"enabled": True,
                                                        "model_path": "models/voice-4b"}}):
        r = client.post("/api/sage/voice", headers=_headers(doctor),
                        json={"query": "what is the paracetamol dose?"})
    assert r.status_code == 200, r.text
    assert _ChattyAgent.called is True


def test_both_lanes_share_one_gate(monkeypatch):
    """The chat lane and the voice lane ask the same function."""
    import inspect
    chat = inspect.getsource(sage_routes.sage_query)
    voice = inspect.getsource(sage_routes.sage_voice_query)
    assert "_mutation_gate(query)" in chat
    assert "_mutation_gate(query)" in voice


def _store_turn(uid, question, answer, blob):
    cid = uuid.uuid4().hex
    with db_session() as db:
        db.execute("INSERT INTO sage_conversations (user_id, role, content, conversation_id) "
                   "VALUES (?, 'user', ?, ?)", (uid, question, cid))
        db.execute("INSERT INTO sage_conversations (user_id, role, content, sources, conversation_id) "
                   "VALUES (?, 'assistant', ?, ?, ?)", (uid, answer, json.dumps(blob), cid))


def test_history_shows_the_safe_message_on_a_mutation_turn(doctor):
    _store_turn(doctor, "add paracetamol 1g to bed 4",
                "NOACT| I'll add paracetamol 1g for bed 4 now.",
                {"proposed_action": True, "safe_answer": CHART_MUTATION_SAFE_MESSAGE})
    r = client.get("/api/sage/history", headers=_headers(doctor))
    assert r.status_code == 200, r.text
    answers = [m["content"] for m in r.json()["messages"] if m["role"] == "assistant"]
    assert answers == [CHART_MUTATION_SAFE_MESSAGE]


def test_history_reads_the_legacy_safe_message_only_on_a_mutation_turn(doctor):
    # Older rows: the safe message lived in parsed_response.concise_answer, and
    # the deterministic gate on the QUESTION is what marks the turn.
    _store_turn(doctor, "add paracetamol 1g",
                "I'll add paracetamol now.",
                {"parsed_response": {"action_intent": "None.",
                                     "concise_answer": CHART_MUTATION_SAFE_MESSAGE}})
    _store_turn(doctor, "what is the paracetamol dose?",
                "1 g every 6 hours, maximum 4 g a day.",
                {"parsed_response": {"action_intent": "None.",
                                     "concise_answer": "1 g every 6 hours."}})
    r = client.get("/api/sage/history", headers=_headers(doctor))
    answers = [m["content"] for m in r.json()["messages"] if m["role"] == "assistant"]
    assert answers[0] == CHART_MUTATION_SAFE_MESSAGE
    assert answers[1] == "1 g every 6 hours, maximum 4 g a day."


def test_the_voice_agent_hands_its_retrieved_sources_to_the_turn(monkeypatch):
    from services import voice_agent_service as vas

    retrieved = [{"document_id": "SEP-001", "title": "Sepsis pathway"}]

    def _ctx(query, user, patient_id=None, *a, **kw):
        return "context", "clinical", retrieved, "high", False, None, "CLINICAL"
    monkeypatch.setattr("services.sage_service.get_relevant_context", _ctx)

    agent = vas.VoiceAgent.__new__(vas.VoiceAgent)
    agent.hospital_name = "Test Hospital"
    bucket = vas.collect_voice_sources()
    agent._build_system_prompt("sepsis bundle?", {"id": "u", "role": "doctor"})
    assert bucket == retrieved
