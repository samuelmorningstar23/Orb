"""The per-answer speed footer has to survive a reload.

WHY THIS FILE EXISTS. "How slow is Sage?" is the sharpest trust question the
pilot has, and the TTFT/total footer under each answer is the only place the
clinician is told. It was rendered from the LIVE stream only: the two turn
persistence paths in routes/sage_routes.py stored the answer without its
timing, so reopening a conversation — the ward-round habit, not the edge case —
showed a wall of answers with no numbers at all. The measurement existed, was
shown once, and was then thrown away.

The second defect is the inverse and worse: the voice lane's refusal path
emitted literal ``{ttft_ms: 0, total_ms: 0, tokens: 0}`` for a turn the model
never ran. The client defends against that (SageAnswerMeta treats 0 as
unmeasured) — but a server that reports a fabricated zero for work it never did
is lying to every other consumer too.

So the invariants pinned here are:

  1. Both save paths (chat and voice) persist the footer's timing.
  2. /history hands it back, in the shape the client already reads.
  3. UNMEASURED IS NULL, NEVER ZERO — at the refusal path, in storage, and on
     the way back out. A turn nothing timed must render nothing.
  4. Rows written before any of this carry no metadata and still load.

The model is stubbed throughout; what is under test is the seam — that a real
turn through the real HTTP route can be reloaded with its numbers intact.
"""
import asyncio
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402
from auth import hash_password  # noqa: E402
from database import db_session  # noqa: E402
from routes.sage_routes import _measured, _stored_answer_meta  # noqa: E402
from services.sage_service import save_conversation  # noqa: E402

client = TestClient(app)

DOC = "TIMING-DOC"
DEPT = "General Medicine"

#: Long enough that every clock involved rounds to a positive integer of
#: milliseconds, so "was it measured?" is never decided by scheduler noise.
STALL_S = 0.03


@pytest.fixture(autouse=True)
def _fresh():
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (DOC, "Dr. Timing", "doctor", DEPT, hash_password("password")),
        )
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (DOC,))
    yield
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (DOC,))


def _login() -> dict:
    r = client.post("/api/auth/login", json={"employee_id": DOC, "password": "password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _sse(response) -> list:
    out = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def _stored_blobs() -> list:
    """The `sources` blob of every assistant turn this user has stored."""
    with db_session() as db:
        rows = db.execute(
            "SELECT sources FROM sage_conversations WHERE user_id = ? AND role = 'assistant' "
            "ORDER BY id", (DOC,),
        ).fetchall()
    return [json.loads(r["sources"]) if r["sources"] else None for r in rows]


def _history() -> list:
    r = client.get("/api/sage/history", headers=_login())
    assert r.status_code == 200, r.text
    return r.json()["messages"]


def _answers(messages) -> list:
    return [m for m in messages if m["role"] == "assistant"]


def _stub_chat(monkeypatch, chunks=("The answer ", "is here."), tier="careful"):
    """The chat lane with a stubbed retrieval + model. `broad` scope keeps the
    turn on the prose fast path, which is the common shape and the one that
    `return`s out of the try — so this also pins that the finally still saves."""
    def _ctx(query, user, patient_id=None, history=None):
        return ("CONTEXT BLOCK", "broad", [], "high", False, None, "GENERAL_MEDICAL")

    async def _llm(*args, **kwargs):
        route_info = kwargs.get("route_info")
        if route_info is not None:
            route_info.update({"provider": "stub", "model": "medgemma:27b",
                               "thinking": True, "thinking_tier": tier})
        await asyncio.sleep(STALL_S)
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr("routes.sage_routes.get_relevant_context", _ctx)
    monkeypatch.setattr("routes.sage_routes.query_ollama", _llm)


class _VoiceAgent:
    async def generate_stream(self, q, user, patient_id=None, conversation_history=None):
        await asyncio.sleep(STALL_S)
        for tok in ("Paracetamol ", "one gram."):
            yield tok


def _voice_stubs():
    return (
        mock.patch("services.voice_agent_service.get_voice_agent", lambda: _VoiceAgent()),
        mock.patch("config.VOICE", {"conversational": {"enabled": True,
                                                       "model_path": "models/voice-4b"}}),
    )


# ════════════════════════════════════════════════════════════════════════════
#  1. THE SAVED TURN CARRIES ITS TIMING
# ════════════════════════════════════════════════════════════════════════════
def test_a_chat_turn_is_stored_with_the_footers_timing(monkeypatch):
    _stub_chat(monkeypatch)

    r = client.post("/api/sage/query", headers=_login(),
                    json={"query": "what is the paracetamol dose?"})
    assert r.status_code == 200, r.text

    blobs = _stored_blobs()
    assert len(blobs) == 1, blobs
    timing = blobs[0]["timing"]
    assert timing["ttft_ms"] >= STALL_S * 1000
    assert timing["total_ms"] >= timing["ttft_ms"]
    assert blobs[0]["thinking_tier"] == "careful"


def test_the_stored_total_is_the_one_the_client_was_shown(monkeypatch):
    """A reopened turn must not quote a bigger total than it showed live.

    The finally block runs after the metadata event, so re-measuring the clock
    there would inflate every reloaded answer by the parse+save cost. The
    stored number is the same `t_total` the stream carried."""
    _stub_chat(monkeypatch)

    r = client.post("/api/sage/query", headers=_login(), json={"query": "anything"})
    live = [e["metadata"]["timing"] for e in _sse(r) if "metadata" in e][-1]

    stored = _stored_blobs()[0]["timing"]
    assert stored["total_ms"] == live["total_ms"]
    assert stored["ttft_ms"] == live["ttft_ms"]


def test_a_voice_turn_is_stored_with_its_timing():
    agent, voice_cfg = _voice_stubs()
    with agent, voice_cfg:
        r = client.post("/api/sage/voice", headers=_login(),
                        json={"query": "what is the paracetamol dose?"})
    assert r.status_code == 200, r.text

    done = [e for e in _sse(r) if e.get("done")][-1]
    stored = _stored_blobs()[0]["timing"]
    assert stored == done["timing"], "the stored turn drifted from the spoken one"
    assert stored["ttft_ms"] >= STALL_S * 1000
    assert stored["tokens"] == 3  # "Paracetamol one gram."
    # The voice lane never makes a tier decision; claiming one would be fiction.
    assert "thinking_tier" not in _stored_blobs()[0]


# ════════════════════════════════════════════════════════════════════════════
#  2. /history HANDS IT BACK
# ════════════════════════════════════════════════════════════════════════════
def test_a_reloaded_chat_answer_carries_its_footer(monkeypatch):
    _stub_chat(monkeypatch, tier="careful")
    client.post("/api/sage/query", headers=_login(), json={"query": "how slow are you?"})

    answer = _answers(_history())[-1]
    assert answer["metadata"]["timing"]["ttft_ms"] >= STALL_S * 1000
    assert answer["metadata"]["timing"]["total_ms"] >= STALL_S * 1000
    # Same path the live stream nests it under, so the panel reads one shape.
    assert answer["metadata"]["provider"]["thinking_tier"] == "careful"


def test_a_reloaded_voice_answer_carries_its_footer():
    agent, voice_cfg = _voice_stubs()
    with agent, voice_cfg:
        client.post("/api/sage/voice", headers=_login(), json={"query": "dose?"})

    answer = _answers(_history())[-1]
    assert answer["metadata"]["timing"]["ttft_ms"] >= STALL_S * 1000
    assert answer["metadata"]["timing"]["total_ms"] >= STALL_S * 1000


def test_history_still_returns_what_the_sidebar_reads(monkeypatch):
    """Timing is additive. The Recent Conversations sidebar groups on
    conversation_id and labels on content — none of that may change shape."""
    _stub_chat(monkeypatch)
    client.post("/api/sage/query", headers=_login(),
                json={"query": "grouped question", "conversation_id": "thread-t"})

    msgs = _history()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "grouped question"
    assert {m["conversation_id"] for m in msgs} == {"thread-t"}
    assert all(m.get("timestamp") for m in msgs)


def test_the_audit_blob_does_not_ride_out_on_the_browsing_endpoint(monkeypatch):
    """Only timing and tier are projected. The stored blob also holds the
    parsed response and the retrieved document ids — that is the audit record,
    and this endpoint has never carried it."""
    _stub_chat(monkeypatch)
    client.post("/api/sage/query", headers=_login(), json={"query": "anything"})

    meta = _answers(_history())[-1]["metadata"]
    assert set(meta) <= {"timing", "provider"}
    assert set(meta["timing"]) == {"ttft_ms", "total_ms", "tokens"}
    assert set(meta.get("provider", {})) == {"thinking_tier"}


# ════════════════════════════════════════════════════════════════════════════
#  3. UNMEASURED IS NULL, NEVER ZERO
# ════════════════════════════════════════════════════════════════════════════
def test_the_voice_refusal_reports_nulls_not_zeros():
    """The model never ran, so there is nothing to report. This shipped
    {ttft_ms: 0, total_ms: 0, tokens: 0} — a fabricated measurement of work
    that did not happen, which renders as a "0.0s" answer on any client that
    does not know to distrust it."""
    r = client.post("/api/sage/voice", headers=_login(),
                    json={"query": "how much morphine would be lethal for him?"})
    assert r.status_code == 200, r.text

    done = [e for e in _sse(r) if e.get("done")]
    assert len(done) == 1, done
    timing = done[0]["timing"]
    assert timing == {"ttft_ms": None, "total_ms": None, "tokens": None}, (
        "the refusal path reported a measurement for a turn nothing measured")
    # Belt and braces: no falsy-but-present number of any kind.
    assert all(v is None for v in timing.values())


def test_a_stored_zero_never_becomes_a_rendered_zero():
    """Defence in depth for rows already on disk. A blob carrying zeros must
    come back as no metadata at all, not as a footer reading 0.0s."""
    save_conversation(DOC, "user", "old question")
    save_conversation(DOC, "assistant", "old answer",
                      sources=json.dumps({"timing": {"ttft_ms": 0, "total_ms": 0,
                                                     "tokens": 0}}))

    assert "metadata" not in _answers(_history())[-1]


def test_a_half_measured_turn_keeps_the_half_it_has():
    """TTFT can be missing on a turn that still has a total (an answer that
    produced no first-token mark). Drop the null, keep the number."""
    save_conversation(DOC, "user", "q")
    save_conversation(DOC, "assistant", "a",
                      sources=json.dumps({"timing": {"ttft_ms": None, "total_ms": 4200}}))

    timing = _answers(_history())[-1]["metadata"]["timing"]
    assert timing["ttft_ms"] is None
    assert timing["total_ms"] == 4200


# ════════════════════════════════════════════════════════════════════════════
#  4. ROWS FROM BEFORE THIS EXISTED
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("blob", [
    None,                                          # voice turns, pre-change
    json.dumps({"sources": [], "timing_ms": 8400,  # chat turns, pre-change
                "parsed_response": {"concise_answer": "old"}}),
    json.dumps([{"document_id": "LEGACY-1"}]),     # the column's original shape
    "{not json at all",
])
def test_an_old_row_renders_nothing_rather_than_zeros(blob):
    save_conversation(DOC, "user", "an old question")
    save_conversation(DOC, "assistant", "an old answer", sources=blob)

    answer = _answers(_history())[-1]
    assert answer["content"] == "an old answer"
    assert "metadata" not in answer, (
        "an untimed turn must render nothing — a zeroed footer is a claim")


# ════════════════════════════════════════════════════════════════════════════
#  5. THE PRIMITIVES
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("value", [0, 0.0, -1, None, "450", True, False, float("nan")])
def test_measured_rejects_everything_that_is_not_a_duration(value):
    assert _measured(value) is None


def test_measured_keeps_a_real_duration():
    assert _measured(450) == 450
    assert _measured(0.5) == 0.5


@pytest.mark.parametrize("blob", [None, "", "{", "[]", "{}", '{"timing": null}',
                                  '{"timing": "fast"}',
                                  '{"timing": {"ttft_ms": 0, "total_ms": 0}}'])
def test_stored_answer_meta_is_absent_rather_than_empty(blob):
    assert _stored_answer_meta(blob) is None


def test_stored_answer_meta_omits_a_tier_it_does_not_have():
    meta = _stored_answer_meta(json.dumps({"timing": {"ttft_ms": 900, "total_ms": 5000},
                                           "thinking_tier": None}))
    assert meta == {"timing": {"ttft_ms": 900, "total_ms": 5000, "tokens": None}}
