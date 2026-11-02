"""Provider-layer tests: the Sage router serves every turn from the local
Ollama lane, through the llm_queue."""
from unittest import mock

import pytest

from services import llm_providers


@pytest.mark.asyncio
async def test_sage_busy_admission_does_not_start_provider_or_release_someone_elses_slot(monkeypatch):
    import config
    from services import sage_service
    from services.llm_queue_service import llm_queue

    provider = mock.Mock()
    provider.name = "ollama"
    choice = mock.Mock(model="synthetic", downgraded=False, preferred="synthetic")
    monkeypatch.setattr("services.thinking_mode.select_provider", mock.AsyncMock(return_value=(choice, provider)))
    monkeypatch.setattr(config, "SAGE_QUEUE_WAIT_SECONDS", 0.02)
    acquire = mock.AsyncMock(side_effect=TimeoutError)
    release = mock.Mock()
    monkeypatch.setattr(llm_queue, "acquire", acquire)
    monkeypatch.setattr(llm_queue, "release", release)
    info = {}
    with pytest.raises(sage_service.SageCapacityBusy):
        _ = [chunk async for chunk in sage_service.query_ollama("Synthetic general query", route_info=info)]
    acquire.assert_awaited_once_with(priority=1, model="synthetic", timeout=0.02)
    provider.astream_chat.assert_not_called()
    release.assert_not_called()
    assert info["queue_wait_ms"] >= 0


@pytest.mark.asyncio
async def test_router_serves_every_turn_from_local_lane():
    """Local inference is the only lane: every turn — PHI-free or not — is
    streamed by the local provider and reported as such in route_info."""
    from services import sage_service

    local_chunks = ["local ", "answer"]

    async def fake_local_stream(messages, options=None):
        for c in local_chunks:
            yield c

    with mock.patch.object(
        llm_providers.local_provider,
        "astream_chat",
        side_effect=lambda m, o=None: fake_local_stream(m, o),
    ):
        route_info = {}
        got = []
        async for chunk in sage_service.query_ollama(
            "what are the side effects of metoprolol",
            scope="simple",
            route_info=route_info,
        ):
            got.append(chunk)

    assert "".join(got) == "local answer"
    assert route_info["lane"] == "local"
    assert route_info["provider"] == "ollama"


# ── WHO OWNS THE GATE MARKER is stamped by query_ollama ──────────────────────
# The streamer's side of this is pinned in test_fast_contract_marker_source.py
# and the route's side in test_sage_streaming.py. This is the join: the decision
# is a property of the MODEL, which is not known until the thinking-mode and
# residency routers have run inside query_ollama, and route_info is the only
# channel back to the route. If this stamp stops happening the fix is inert in
# production and every other test in the chain still passes.
@pytest.mark.asyncio
@pytest.mark.parametrize("served_model, asserted", [
    ("medgemma:27b", False),   # the careful tier: measured 9/9, its marker is real
    ("medgemma:4b", True),     # the fast tier: measured 0/9, Orb emits it instead
])
async def test_query_ollama_stamps_who_owns_the_marker(monkeypatch, served_model, asserted):
    from services import sage_service, thinking_mode
    from services.model_router import ModelChoice

    monkeypatch.delenv("ORB_SAGE_MARKER_SOURCE", raising=False)
    monkeypatch.delenv("ORB_SAGE_XML_CONTRACT", raising=False)
    monkeypatch.setattr(sage_service, "LLM_MODEL", "medgemma:27b")

    async def _fake_select(query, context=None, patient_id=None, classification=None,
                           route_info=None, thinking=None):
        return (ModelChoice(served_model, False, served_model),
                llm_providers.local_provider)

    async def _fake_stream(messages, options=None):
        yield "an answer"

    monkeypatch.setattr(thinking_mode, "select_provider", _fake_select)
    with mock.patch.object(llm_providers.local_provider, "astream_chat",
                           side_effect=lambda m, o=None: _fake_stream(m, o)):
        route_info = {}
        async for _ in sage_service.query_ollama(
                "what is hypoxia", scope="simple", route_info=route_info):
            pass

    assert route_info["model"] == served_model
    assert route_info["marker_asserted"] is asserted


@pytest.mark.asyncio
async def test_a_prose_scope_never_claims_a_marker(monkeypatch):
    """broad/aggregate carry no marker contract and are streamed by the plain
    sanitizer, which has no gate to open. Claiming one there would have the
    route call a method the prose lane's streamer does not have."""
    from services import sage_service, thinking_mode
    from services.model_router import ModelChoice

    monkeypatch.delenv("ORB_SAGE_MARKER_SOURCE", raising=False)

    async def _fake_select(query, context=None, patient_id=None, classification=None,
                           route_info=None, thinking=None):
        return ModelChoice("medgemma:4b", False, "medgemma:4b"), llm_providers.local_provider

    async def _fake_stream(messages, options=None):
        yield "an answer"

    monkeypatch.setattr(thinking_mode, "select_provider", _fake_select)
    with mock.patch.object(llm_providers.local_provider, "astream_chat",
                           side_effect=lambda m, o=None: _fake_stream(m, o)):
        route_info = {}
        async for _ in sage_service.query_ollama(
                "how many patients are admitted", scope="broad", route_info=route_info):
            pass

    assert route_info["marker_asserted"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["complete", "failure", "cancelled"])
async def test_inference_audit_requires_successful_stream_completion(monkeypatch, termination):
    from services import ai_governance, sage_service, thinking_mode
    from services.llm_queue_service import llm_queue
    from services.model_router import ModelChoice

    recorded = []
    acquired = []
    released = []
    teardown = []

    async def select(*args, **kwargs):
        return ModelChoice("medgemma:27b", False, "medgemma:27b"), llm_providers.local_provider

    async def stream(messages, options):
        try:
            yield "partial answer"
            if termination == "failure":
                raise llm_providers.LocalModelStreamError("Local model did not complete an answer.")
            yield " completed"
        finally:
            teardown.append("provider closed")

    async def acquire(*args, **kwargs):
        acquired.append(True)

    def release():
        released.append(True)
        teardown.append("queue released")

    monkeypatch.setattr(thinking_mode, "select_provider", select)
    monkeypatch.setattr(llm_providers.local_provider, "astream_chat", stream)
    monkeypatch.setattr(llm_queue, "acquire", acquire)
    monkeypatch.setattr(llm_queue, "release", release)
    monkeypatch.setattr(ai_governance, "record_inference_soon", lambda **kwargs: recorded.append(kwargs))
    response = sage_service.query_ollama("What is hypoxia?", user_id="DOC001")
    assert await anext(response) == "partial answer"
    if termination == "cancelled":
        await response.aclose()
    elif termination == "failure":
        with pytest.raises(llm_providers.LocalModelStreamError):
            await anext(response)
    else:
        assert [chunk async for chunk in response] == [" completed"]

    assert acquired == released == [True]
    assert teardown == ["provider closed", "queue released"]
    assert len(recorded) == 1
    assert recorded[0]["outcome"] == ("served" if termination == "complete" else "error")
    assert recorded[0]["user_id"] == "DOC001"
