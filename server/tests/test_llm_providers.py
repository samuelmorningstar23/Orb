"""Provider-layer tests: the Sage router serves every turn from the local
Ollama lane, through the llm_queue."""
from unittest import mock

import pytest

from services import llm_providers


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
