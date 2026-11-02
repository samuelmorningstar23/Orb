"""query_ollama closes the model stream before it frees the queue, and audits a
turn as served only when the stream finished.

Adopted with the 12 October pass on Sage stream failure (docs/
ML_SECURITY_AND_SAGE_LATENCY_2026-10.md, "Sage correctness and measurement"):
"Cancellation closes the provider before releasing the queue; the inference
audit records failure unless the stream finishes successfully." Before it the
provider's stream was left for the garbage collector to close after the queue
slot was already given away, and every turn was audited "served", including one
the engine broke off halfway or the clinician walked away from.
"""
import asyncio

import pytest
from services.model_router import ModelChoice

from services import sage_service


class _Provider:
    name = "fake-local"

    def __init__(self, events, chunks, fail_after=None):
        self.events, self.chunks, self.fail_after = events, chunks, fail_after

    async def astream_chat(self, messages, options):
        try:
            for i, chunk in enumerate(self.chunks):
                if self.fail_after is not None and i == self.fail_after:
                    raise RuntimeError("engine broke off")
                yield chunk
        finally:
            self.events.append("provider closed")


@pytest.fixture
def harness(monkeypatch):
    events, audits = [], []
    state = {}
    lease = object()

    async def select_provider(prompt, context, patient_id, classification, route_info=None):
        return ModelChoice(model="test:27b", downgraded=False, preferred="test:27b"), state["provider"]

    async def acquire(priority=1, *, model=None, timeout=None):
        assert model == "test:27b"
        assert timeout is not None and timeout > 0
        events.append("queue acquired")
        return lease

    def release(ticket):
        assert ticket is lease, "release must own the exact request's admission ticket"
        events.append("queue released")

    from services.llm_queue_service import llm_queue

    from services import ai_governance, thinking_mode
    monkeypatch.setattr(thinking_mode, "select_provider", select_provider)
    monkeypatch.setattr(llm_queue, "acquire", acquire)
    monkeypatch.setattr(llm_queue, "release", release)
    monkeypatch.setattr(ai_governance, "record_inference_soon", lambda **kw: audits.append(kw))

    def run(chunks, fail_after=None, stop_after=None):
        state["provider"] = _Provider(events, chunks, fail_after)

        async def consume():
            got = []
            stream = sage_service.query_ollama("what is tachycardia?", "", None, "simple")
            try:
                async for chunk in stream:
                    got.append(chunk)
                    if stop_after is not None and len(got) == stop_after:
                        break
            finally:
                await stream.aclose()
            return got
        return asyncio.run(consume())
    return run, events, audits


def test_a_finished_stream_is_audited_as_served(harness):
    run, events, audits = harness
    assert run(["NOACT|", "Fast heart rate."]) == ["NOACT|", "Fast heart rate."]
    assert [a["outcome"] for a in audits] == ["served"]
    assert events.index("provider closed") < events.index("queue released")


def test_an_engine_failure_is_audited_as_an_error(harness):
    run, events, audits = harness
    with pytest.raises(RuntimeError):
        run(["NOACT|", "Fast", " heart rate."], fail_after=2)
    assert [a["outcome"] for a in audits] == ["error"]
    assert events.index("provider closed") < events.index("queue released")


def test_a_clinician_who_walks_away_closes_the_engine_before_the_queue(harness):
    """The route stops reading when the client disconnects. The provider must be
    closed before the queue slot goes to the next turn, and the turn is not
    "served": the answer never finished."""
    run, events, audits = harness
    assert run(["NOACT|", "Fast", " heart rate."], stop_after=1) == ["NOACT|"]
    assert [a["outcome"] for a in audits] == ["error"]
    assert events.index("provider closed") < events.index("queue released")
