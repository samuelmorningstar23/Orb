"""services/ollama_gateway: one audit row per inference, and the P3 yield.

F038 (2026-10-03): about eleven LLM lanes posted to Ollama with no audit row,
and no row anywhere carried a prompt hash. Every gateway call now writes
exactly one ai_inference row with hash_prompt() of what the model was sent.

F036: a P3 background request that opts in gives the GPU back as soon as a
clinician's turn is waiting or streaming, or when its hold reaches the P3 cap.
The request is abandoned within one poll interval and the caller sees a
TimeoutError (LaneYielded), which every background caller already turns into
its deterministic fallback.
"""
import asyncio
import json
import time

import pytest

from database import db_session
from services import ollama_gateway
from services.ai_governance import hash_prompt
from services.llm_queue_service import PriorityLLMQueue, llm_queue

HEX = set("0123456789abcdef")


def _rows(purpose: str) -> list[dict]:
    """Every ai_inference row written for `purpose`, oldest first."""
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE event_category = 'ai_inference' ORDER BY id"
        ).fetchall()]
    out = []
    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        if meta.get("purpose") == purpose:
            r["meta"] = meta
            out.append(r)
    return out


class _Resp:
    def __init__(self, status=200, body=None, lines=()):
        self.status_code = status
        self._body = body or {"message": {"content": "ok"}}
        self._lines = list(lines)

    def json(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Client:
    """A stand-in httpx.AsyncClient that records what it was asked."""

    def __init__(self, status=200, lines=()):
        self.status = status
        self.lines = lines
        self.posts: list = []

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return _Resp(self.status)

    def stream(self, method, url, json=None):
        client = self

        class _Ctx:
            async def __aenter__(self):
                client.posts.append((url, json))
                return _Resp(client.status, lines=client.lines)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class _HangingClient:
    """A request the model never answers, so only a yield can end it."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def post(self, url, json=None):
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return _Resp()


CHAT = {
    "model": "medgemma:4b",
    "messages": [{"role": "system", "content": "Be brief."},
                 {"role": "user", "content": "HR 132, BP 82/49"}],
    "stream": False,
}


# ── hash_prompt ──────────────────────────────────────────────────────────────

def test_the_prompt_hash_is_the_same_whichever_endpoint_carried_it():
    generate = {"model": "x", "system": "Be brief.", "prompt": "HR 132, BP 82/49"}
    assert hash_prompt(CHAT) == hash_prompt(generate)
    assert len(hash_prompt(CHAT)) == 64 and set(hash_prompt(CHAT)) <= HEX


def test_the_prompt_hash_ignores_model_and_sampling_but_not_one_character_of_prompt():
    other_model = dict(CHAT, model="medgemma:27b", options={"temperature": 0.9})
    assert hash_prompt(other_model) == hash_prompt(CHAT)
    edited = dict(CHAT, messages=[CHAT["messages"][0],
                                  {"role": "user", "content": "HR 131, BP 82/49"}])
    assert hash_prompt(edited) != hash_prompt(CHAT)


# ── one audit row per call ───────────────────────────────────────────────────

def test_a_post_writes_one_row_with_the_prompt_hash_and_never_the_prompt():
    client = _Client()
    resp = asyncio.run(ollama_gateway.post(
        client, "/api/chat", CHAT, purpose="gw_test_post",
        patient_id="P001", user_id="DOC001"))
    assert resp.status_code == 200
    assert client.posts[0][0].endswith("/api/chat")

    rows = _rows("gw_test_post")
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "success"
    assert row["patient_id"] == "P001"
    assert row["actor_user_id"] == "DOC001"
    assert row["meta"]["model"] == "medgemma:4b"
    assert row["meta"]["prompt_sha256"] == hash_prompt(CHAT)
    assert "HR 132" not in json.dumps(row, default=str)


def test_a_failed_post_is_still_audited_as_an_error():
    asyncio.run(ollama_gateway.post(_Client(status=500), "/api/generate",
                                    {"model": "m", "prompt": "p"}, purpose="gw_test_http_500"))
    rows = _rows("gw_test_http_500")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["meta"]["detail"] == "HTTP 500"


def test_a_stream_writes_one_row_when_the_block_exits():
    client = _Client(lines=['{"message": {"content": "ok"}}'])

    async def run():
        async with ollama_gateway.stream(client, "/api/chat", CHAT,
                                         purpose="gw_test_stream", patient_id="P002") as r:
            assert [line async for line in r.aiter_lines()]
        # Exactly one row, written on exit.
        return _rows("gw_test_stream")

    rows = asyncio.run(run())
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert rows[0]["patient_id"] == "P002"
    assert rows[0]["meta"]["prompt_sha256"] == hash_prompt(CHAT)


# ── the P3 yield ─────────────────────────────────────────────────────────────

def test_a_clinicians_turn_aborts_a_p3_request_within_one_poll(monkeypatch):
    monkeypatch.setattr(llm_queue.governor, "enabled", False)
    client = _HangingClient()
    turn_got_slot = []
    background_done = asyncio.Event()

    async def clinician():
        # A real turn streams for seconds; hold the interactive lane until the
        # background request has let go (bounded, so a failure cannot hang).
        await client.started.wait()
        await llm_queue.acquire(priority=1, timeout=5)
        turn_got_slot.append(time.monotonic())
        try:
            await asyncio.wait_for(background_done.wait(), timeout=5)
        finally:
            llm_queue.release()

    async def run():
        await llm_queue.acquire(priority=3, timeout=5)
        turn = asyncio.create_task(clinician())
        try:
            t0 = time.monotonic()
            with pytest.raises(ollama_gateway.LaneYielded) as exc:
                await ollama_gateway.post(client, "/api/generate",
                                          {"model": "m", "prompt": "summarise"},
                                          purpose="gw_test_yield", yield_to_clinician=True)
            elapsed = time.monotonic() - t0
        finally:
            background_done.set()
            llm_queue.release()
        await turn
        return elapsed, str(exc.value)

    elapsed, reason = asyncio.run(run())
    # One poll is 0.1 s; the request was 30 s from answering.
    assert elapsed < 1.0, elapsed
    assert client.cancelled, "the abandoned request was left running on the GPU"
    assert "clinician" in reason
    assert turn_got_slot, "the clinician's turn never ran"
    # A yield is a TimeoutError, so every caller's existing fallback takes it.
    assert issubclass(ollama_gateway.LaneYielded, asyncio.TimeoutError)
    row = _rows("gw_test_yield")[-1]
    assert row["outcome"] == "failure"
    assert "clinician" in row["meta"]["detail"]


def test_a_p3_hold_is_cut_at_the_cap(monkeypatch):
    monkeypatch.setattr(llm_queue.governor, "enabled", False)
    monkeypatch.setattr(llm_queue, "_p3_hold_cap_seconds", lambda: 0.3)
    client = _HangingClient()

    async def run():
        await llm_queue.acquire(priority=3, timeout=5)
        try:
            t0 = time.monotonic()
            with pytest.raises(ollama_gateway.LaneYielded) as exc:
                await ollama_gateway.post(client, "/api/generate",
                                          {"model": "m", "prompt": "p"},
                                          purpose="gw_test_cap", yield_to_clinician=True)
            return time.monotonic() - t0, str(exc.value)
        finally:
            llm_queue.release()

    elapsed, reason = asyncio.run(run())
    assert elapsed < 1.5, elapsed
    assert "cap" in reason
    assert client.cancelled


def test_only_a_p3_holder_yields(monkeypatch):
    """P2 work has a clinician waiting on it and no fallback, so it never
    yields; a caller holding no lane never yields either."""
    monkeypatch.setattr(llm_queue.governor, "enabled", False)
    monkeypatch.setattr(PriorityLLMQueue, "preempt_requested", property(lambda self: True))

    async def held(priority):
        await llm_queue.acquire(priority=priority, timeout=5)
        try:
            return llm_queue.yield_reason()
        finally:
            llm_queue.release()

    assert asyncio.run(held(2)) is None
    assert asyncio.run(held(3)) == "yielded to a clinician's turn"
    assert llm_queue.yield_reason() is None


def test_a_request_that_does_not_opt_in_is_never_cut(monkeypatch):
    """yield_to_clinician defaults off: P2 lanes (Lens, discharge, Bridge) and
    the deliberate P3 exceptions (prewarm, signed-note extraction) run to the
    end even while a clinician's turn is in flight."""
    monkeypatch.setattr(llm_queue.governor, "enabled", False)
    monkeypatch.setattr(PriorityLLMQueue, "preempt_requested", property(lambda self: True))

    class _Slow(_Client):
        async def post(self, url, json=None):
            await asyncio.sleep(0.3)
            return await super().post(url, json)

    async def run():
        await llm_queue.acquire(priority=3, timeout=5)
        try:
            return await ollama_gateway.post(_Slow(), "/api/chat", CHAT,
                                             purpose="gw_test_no_yield")
        finally:
            llm_queue.release()

    assert asyncio.run(run()).status_code == 200
