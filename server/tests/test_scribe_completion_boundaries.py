"""A local model's partial draft must never look like successful completion."""
import httpx
import pytest

from services.scribe_providers import OllamaProvider


@pytest.fixture
def upstream(monkeypatch):
    state = {"frames": [], "status": 200}
    original = httpx.AsyncClient
    def response(request):
        return httpx.Response(state["status"], content='\n'.join(state["frames"]))
    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(response), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr('config.MODELS_ENABLED', True)
    monkeypatch.setattr('config.WARD_RECORD', False)
    return state


@pytest.mark.parametrize('tail', [[], ['{'], ['[]'], ['{"done":"true"}'],
    ['{"message":null}'], ['{"message":{"content":7}}'],
    ['{"done":true,"done_reason":"length"}'], ['{"error":"private"}']])
async def test_partial_or_invalid_stream_never_completes(upstream, tail):
    upstream['frames'] = ['{"message":{"content":"Partial"}}', *tail]
    received = []
    with pytest.raises(RuntimeError):
        async for chunk in OllamaProvider().agenerate_stream('synthetic'):
            received.append(chunk)
    assert received == ['Partial']


async def test_nonempty_explicitly_completed_draft_succeeds(upstream):
    upstream['frames'] = ['', '{"message":{"content":"Complete"},"done":false}', '{"done":true}']
    assert [chunk async for chunk in OllamaProvider().agenerate_stream('synthetic')] == ['Complete']


async def test_empty_draft_and_http_failure_are_errors(upstream):
    upstream['frames'] = ['{"done":true}']
    with pytest.raises(RuntimeError):
        _ = [chunk async for chunk in OllamaProvider().agenerate_stream('synthetic')]
    upstream['status'] = 503
    with pytest.raises(httpx.HTTPStatusError):
        _ = [chunk async for chunk in OllamaProvider().agenerate_stream('synthetic')]


def test_provider_cannot_override_the_offline_destination():
    with pytest.raises(ValueError, match='loopback'):
        OllamaProvider('https://outside.example')
