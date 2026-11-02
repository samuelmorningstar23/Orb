"""Exercise the real local provider's HTTP/NDJSON boundary without inference."""
import json

import httpx
import pytest

from services import llm_providers


class OwnedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def install_response(monkeypatch, chunks, status=200):
    stream = OwnedStream(chunks)
    original = httpx.AsyncClient

    def respond(request):
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        body = json.loads(request.content)
        assert body["stream"] is True
        return httpx.Response(status, stream=stream)

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(llm_providers.httpx, "AsyncClient", client)
    monkeypatch.setattr("services.model_switch.refuse_when_models_off", lambda *_: None)
    return stream


def frame(value):
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


async def consume():
    provider = llm_providers.OllamaChatProvider(base_url="http://127.0.0.1:11434")
    return [part async for part in provider.astream_chat([{"role": "user", "content": "Synthetic query"}])]


@pytest.mark.asyncio
async def test_fragmented_utf8_and_terminal_content_are_preserved(monkeypatch):
    body = frame({"message": {"content": "Résumé "}, "done": False}) + frame({
        "message": {"content": "complete."}, "done": True, "done_reason": "stop",
    })
    stream = install_response(monkeypatch, [body[:45], body[45:53], body[53:]])
    assert "".join(await consume()) == "Résumé complete."
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks,status", [
    ([frame({"error": "SENSITIVE engine details"})], 404),
    ([frame({"error": "SENSITIVE engine details"})], 500),
    ([frame({"error": "SENSITIVE engine details"})], 200),
    ([b"SENSITIVE malformed record\n"], 200),
    ([b"[]\n"], 200),
    ([frame({"message": "SENSITIVE", "done": True})], 200),
    ([frame({"message": {"content": 1}, "done": True})], 200),
    ([frame({"message": {"content": "Partial answer."}})], 200),
    ([frame({"done": True})], 200),
    ([frame({"message": {"content": " \t"}, "done": True})], 200),
    ([frame({"message": {"content": "Partial answer."}, "done": "true"})], 200),
    ([frame({"message": {"content": "Partial answer."}, "done": True, "done_reason": "length"})], 200),
])
async def test_failure_empty_or_truncated_answer_cannot_count_as_completion(monkeypatch, chunks, status):
    stream = install_response(monkeypatch, chunks, status)
    with pytest.raises(llm_providers.LocalModelStreamError) as error:
        await consume()
    assert "SENSITIVE" not in str(error.value)
    assert stream.closed


@pytest.mark.asyncio
async def test_consumer_cancellation_closes_owned_response(monkeypatch):
    stream = install_response(monkeypatch, [
        frame({"message": {"content": "First sentence."}, "done": False}),
        frame({"message": {"content": "Second sentence."}, "done": True}),
    ])
    provider = llm_providers.OllamaChatProvider(base_url="http://127.0.0.1:11434")
    iterator = provider.astream_chat([])
    assert await anext(iterator) == "First sentence."
    await iterator.aclose()
    assert stream.closed
