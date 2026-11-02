"""Pooled Scribe correction and backend boundaries, synthetic dictation only."""
import asyncio
from types import SimpleNamespace

import pytest

from services import scribe_service
from services.llm_queue_service import llm_queue


TEXT = "Synthetic dictation for admission testing. " * 8


@pytest.fixture
def correction_config(monkeypatch):
    cfg = {"enabled": True, "provider": "ollama", "model": "synthetic-correction",
           "temperature": 0.0, "max_tokens": 80, "ctx_size": 1024}
    monkeypatch.setitem(scribe_service.SCRIBE, "correction", cfg)
    monkeypatch.setattr(llm_queue, "worker_pool", object())

    async def audit(**kwargs):
        pass
    monkeypatch.setattr("services.ai_governance.record_inference_async", audit)
    return cfg


def test_sync_correction_cannot_bypass_pool_from_asr_thread(monkeypatch, correction_config):
    monkeypatch.setattr(scribe_service, "get_provider", lambda *_: pytest.fail("sync correction reached a provider"))
    assert scribe_service.correct_transcript(TEXT) == TEXT


@pytest.mark.asyncio
async def test_async_correction_holds_exact_model_and_releases_its_ticket(monkeypatch, correction_config):
    calls = []
    ticket = object()

    async def acquire(**kwargs):
        calls.append(("acquire", kwargs))
        return ticket

    async def generate(**kwargs):
        calls.append(("generate", kwargs["model"]))
        return "Synthetic corrected dictation."

    monkeypatch.setattr(llm_queue, "acquire", acquire)
    monkeypatch.setattr(llm_queue, "release", lambda owned: calls.append(("release", owned)))
    monkeypatch.setattr(scribe_service, "get_provider", lambda *_: SimpleNamespace(agenerate=generate))
    assert await scribe_service.correct_transcript_async(TEXT) == "Synthetic corrected dictation."
    assert calls[0][1]["model"] == "synthetic-correction"
    assert calls[1] == ("generate", "synthetic-correction")
    assert calls[2] == ("release", ticket)


@pytest.mark.asyncio
async def test_correction_busy_keeps_actual_dictation_and_never_releases_unowned_slot(monkeypatch, correction_config):
    async def acquire(**_):
        raise TimeoutError

    monkeypatch.setattr(llm_queue, "acquire", acquire)
    monkeypatch.setattr(llm_queue, "release", lambda *_: pytest.fail("unowned release"))
    monkeypatch.setattr(scribe_service, "get_provider", lambda *_: SimpleNamespace())
    assert await scribe_service.correct_transcript_async(TEXT) == TEXT


@pytest.mark.asyncio
async def test_correction_cancel_releases_slot_and_propagates(monkeypatch, correction_config):
    released = []
    ticket = object()

    async def acquire(**_):
        return ticket

    async def generate(**_):
        raise asyncio.CancelledError

    monkeypatch.setattr(llm_queue, "acquire", acquire)
    monkeypatch.setattr(llm_queue, "release", lambda owned: released.append(owned))
    monkeypatch.setattr(scribe_service, "get_provider", lambda *_: SimpleNamespace(agenerate=generate))
    with pytest.raises(asyncio.CancelledError):
        await scribe_service.correct_transcript_async(TEXT)
    assert released == [ticket]


@pytest.mark.asyncio
async def test_inprocess_mlx_cannot_claim_an_ollama_worker_lease(monkeypatch, correction_config):
    correction_config["provider"] = "mlx"
    monkeypatch.setattr(scribe_service, "get_provider", lambda *_: pytest.fail("unqualified MLX provider reached"))
    assert await scribe_service.correct_transcript_async(TEXT) == TEXT
    draft = {**scribe_service.SCRIBE["draft"], "provider": "mlx"}
    monkeypatch.setitem(scribe_service.SCRIBE, "draft", draft)
    # Drafting must fail explicitly instead of returning plausible prose.
    monkeypatch.setattr(scribe_service, "get_provider", lambda *_: SimpleNamespace())
    with pytest.raises(RuntimeError, match="qualified Ollama"):
        await scribe_service.structure_transcript(TEXT)
