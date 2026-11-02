"""Provider/gateway boundaries use their own worker and record real transport outcomes."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services import llm_providers, llm_queue_service, ollama_gateway, scribe_providers


@pytest.fixture
def worker(monkeypatch):
    lease = Mock()
    monkeypatch.setattr(llm_queue_service.llm_queue, "current_worker", lambda: lease)
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    monkeypatch.setattr(ollama_gateway, "_record", AsyncMock())
    return lease


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"message":{"content":"Partial synthetic answer"},"done":false}\n'
        raise httpx.ReadError("Synthetic transport broke")


@pytest.mark.asyncio
async def test_gateway_stream_200_headers_do_not_hide_later_transport_failure(worker):
    def respond(request):
        return httpx.Response(200, stream=BrokenStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.ReadError):
            async with ollama_gateway.stream(client, "/api/chat", {"model": "synthetic"}, purpose="test") as response:
                _ = [line async for line in response.aiter_lines()]
    worker.record_success.assert_not_called()
    worker.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_scribe_stream_records_connection_loss_without_marking_probe_healthy(worker, monkeypatch):
    original = httpx.AsyncClient

    def respond(request):
        return httpx.Response(200, stream=BrokenStream())

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(httpx.ReadError):
        _ = [chunk async for chunk in scribe_providers.OllamaProvider().agenerate_stream("Synthetic", model="synthetic")]
    worker.record_success.assert_not_called()
    worker.record_failure.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("body,healthy", [
    ({"message": {"content": "Synthetic complete draft"}, "done": True}, True),
    ({"message": {"content": "Partial"}, "done": False}, False),
    ({"message": {"content": "Partial"}, "done": True, "done_reason": "length"}, False),
])
async def test_nonstream_scribe_requires_complete_output_before_healthy_recovery(worker, monkeypatch, body, healthy):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kw))
    answer = await scribe_providers.OllamaProvider().agenerate("Synthetic", model="synthetic")
    assert bool(answer) is healthy
    assert worker.record_success.call_count == int(healthy)


@pytest.mark.asyncio
async def test_sage_provider_routes_through_exact_task_lease_not_shared_base(monkeypatch):
    from services.local_inference_pool import LocalInferencePool, WorkerSpec

    pool = LocalInferencePool([WorkerSpec("leased", "http://127.0.0.1:11435", ("synthetic",), qualification="test fixture")])
    queue = llm_queue_service.PriorityLLMQueue(pool=pool)
    monkeypatch.setattr(llm_queue_service, "llm_queue", queue)
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    original = httpx.AsyncClient
    visited = []

    def respond(request):
        visited.append(request.url.port)
        return httpx.Response(200, content=b'{"message":{"content":"Synthetic answer"},"done":false}\n{"done":true}\n')

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    provider = llm_providers.OllamaChatProvider(base_url="http://127.0.0.1:11434", model="synthetic")
    ticket = await queue.acquire(priority=1, model="synthetic")
    try:
        assert [s async for s in provider.astream_chat([])] == ["Synthetic answer"]
    finally:
        queue.release(ticket)
    assert visited == [11435]
    assert provider.base_url == "http://127.0.0.1:11434"
    assert pool.active == 0
