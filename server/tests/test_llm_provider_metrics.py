"""Actual provider completion logs use engine cache counters, never PHI estimates."""
import json
import logging

import httpx
import pytest

from services import llm_providers


async def completed_stream(monkeypatch, counters):
    original = httpx.AsyncClient
    terminal = {
        "message": {"content": "Synthetic complete answer."},
        "done": True,
        "done_reason": "stop",
        **counters,
    }

    def respond(request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["messages"][0]["content"] == "PRIVATE QUERY NEVER LOGGED " * 400
        return httpx.Response(200, content=json.dumps(terminal) + "\n")

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(llm_providers.httpx, "AsyncClient", client)
    monkeypatch.setattr("services.model_switch.refuse_when_models_off", lambda *_: None)
    provider = llm_providers.OllamaChatProvider(base_url="http://127.0.0.1:11434")
    return [chunk async for chunk in provider.astream_chat([
        {"role": "user", "content": "PRIVATE QUERY NEVER LOGGED " * 400},
    ])]


@pytest.mark.asyncio
@pytest.mark.parametrize("cached,expected_prefill,expected_rate", [
    (600, 100, 50),
    (0, 700, 350),
    (700, 0, 0),
])
async def test_native_total_and_cached_counts_determine_actual_prefill(
    monkeypatch, caplog, cached, expected_prefill, expected_rate,
):
    caplog.set_level(logging.INFO, logger="orb.llm_providers")
    chunks = await completed_stream(monkeypatch, {
        "prompt_eval_count": 700,
        "prompt_eval_cached_count": cached,
        "prompt_eval_duration": 2_000_000_000,
        "eval_count": 20,
        "eval_duration": 1_000_000_000,
    })
    assert chunks == ["Synthetic complete answer."]
    split = [record.message for record in caplog.records if "llm split" in record.message]
    assert len(split) == 1
    assert f"prefill={expected_prefill} tok in 2000 ms ({expected_rate} tok/s)" in split[0]
    assert "decode=20 tok in 1000 ms (20.0 tok/s)" in split[0]
    assert f"prompt_total=700 tok, prompt_cached={cached} tok" in split[0]
    assert "PRIVATE QUERY" not in caplog.text
    assert "cache_reused~" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [None, -1, 701, True, "SENSITIVE optional metric", 600.0])
async def test_missing_or_invalid_cache_counter_stays_unknown_without_breaking_answer(
    monkeypatch, caplog, cached,
):
    caplog.set_level(logging.INFO, logger="orb.llm_providers")
    counters = {"prompt_eval_count": 700, "prompt_eval_duration": 2_000_000_000}
    if cached is not None:
        counters["prompt_eval_cached_count"] = cached
    assert await completed_stream(monkeypatch, counters) == ["Synthetic complete answer."]
    assert "prefill=unknown tok in 2000 ms (unknown tok/s)" in caplog.text
    assert "prompt_total=700 tok, prompt_cached=unknown tok" in caplog.text
    assert "PRIVATE QUERY" not in caplog.text
    assert "SENSITIVE" not in caplog.text


@pytest.mark.asyncio
async def test_malformed_optional_metrics_do_not_turn_complete_answer_into_stream_error(
    monkeypatch, caplog,
):
    caplog.set_level(logging.INFO, logger="orb.llm_providers")
    assert await completed_stream(monkeypatch, {
        "prompt_eval_count": 10**400,
        "prompt_eval_cached_count": 1,
        "prompt_eval_duration": 10**400,
        "eval_count": "SENSITIVE engine diagnostics",
        "eval_duration": {"private": "details"},
    }) == ["Synthetic complete answer."]
    assert "prefill=unknown tok in 0 ms (unknown tok/s)" in caplog.text
    assert "decode=unknown tok in 0 ms (unknown tok/s)" in caplog.text
    assert "prompt_total=unknown tok, prompt_cached=unknown tok" in caplog.text
    assert "PRIVATE QUERY" not in caplog.text
    assert "SENSITIVE" not in caplog.text
    assert "private" not in caplog.text
