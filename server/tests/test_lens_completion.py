"""Vision partial/error streams cannot become successful saved analyses."""
from contextlib import asynccontextmanager
import json

import httpx
import pytest

from services import lens_service as lens


@pytest.fixture
def upstream(monkeypatch):
    state = {"lines": [], "status": 200, "closed": 0, "released": 0}
    class Queue:
        async def acquire(self, **kwargs):
            pass
        def release(self):
            state["released"] += 1
    class Response:
        def raise_for_status(self):
            httpx.Response(state["status"], request=httpx.Request("POST", "http://127.0.0.1/api/chat")).raise_for_status()
        async def aiter_lines(self):
            for line in state["lines"]:
                yield line
        async def aclose(self):
            state["closed"] += 1
    @asynccontextmanager
    async def stream(*args, **kwargs):
        yield Response()
    monkeypatch.setattr("services.llm_queue_service.llm_queue", Queue())
    monkeypatch.setattr("services.ollama_gateway.stream", stream)
    monkeypatch.setattr(lens, "_encode_image", lambda _: "synthetic")
    return state


@pytest.mark.parametrize("tail", [
    [], ["{"], ["[]"], ['{"error":"private"}'], ['{"message":null}'],
    ['{"message":{"content":7}}'], ['{"done":"true"}'],
    ['{"done":true,"done_reason":"length"}'],
])
async def test_incomplete_or_invalid_frames_fail_without_success_disclaimer(upstream, tail):
    upstream["lines"] = ['{"message":{"content":"Partial"}}', *tail]
    received = []
    with pytest.raises(RuntimeError):
        async for chunk in lens.analyze_image_stream("synthetic"):
            received.append(chunk)
    assert received == ["Partial"]
    assert upstream["closed"] == upstream["released"] == 1


async def test_explicit_complete_nonempty_stream_gets_disclaimer(upstream):
    upstream["lines"] = ["", json.dumps({"message": {"content": "Complete"}, "done": False}),
                         json.dumps({"message": {"content": " analysis"}, "done": True, "done_reason": "stop"})]
    assert [chunk async for chunk in lens.analyze_image_stream("synthetic")] == ["Complete", " analysis", lens.LENS_DISCLAIMER_BANNER]
    assert upstream["closed"] == upstream["released"] == 1


async def test_http_error_closes_and_releases(upstream):
    upstream["status"] = 503
    with pytest.raises(httpx.HTTPStatusError):
        _ = [chunk async for chunk in lens.analyze_image_stream("synthetic")]
    assert upstream["closed"] == upstream["released"] == 1


async def test_cancel_does_not_append_disclaimer(upstream):
    upstream["lines"] = ['{"message":{"content":"Partial"}}', '{"done":true}']
    stream = lens.analyze_image_stream("synthetic")
    assert await anext(stream) == "Partial"
    await stream.aclose()
    assert upstream["closed"] == upstream["released"] == 1


@pytest.mark.parametrize("tail", [[], ["{"], ['{"done":true,"done_reason":"length"}']])
def test_actual_lens_stream_failure_is_not_persisted_and_removes_upload(upstream, monkeypatch, tmp_path, tail):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auth import get_current_user
    from routes import lens_routes
    from services import crypto_service
    monkeypatch.setattr("config.LENS_ENABLED", True)
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    monkeypatch.setattr(lens_routes, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(crypto_service, "UPLOAD_ENCRYPTION_KEY", None)
    monkeypatch.setenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "0")
    saved = []
    monkeypatch.setattr(lens_routes, "save_lens_analysis", lambda **kwargs: saved.append(kwargs))
    upstream["lines"] = ['{"message":{"content":"Partial"}}', *tail]
    app = FastAPI()
    app.include_router(lens_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "synthetic", "role": "doctor"}
    with TestClient(app) as client:
        response = client.post('/api/lens/analyze', files={"image": ("synthetic.png", b"\x89PNG\r\n\x1a\nsynthetic")})
    assert response.status_code == 200
    assert 'lens_stream_failed' in response.text and '[DONE]' not in response.text
    assert saved == [] and list(tmp_path.iterdir()) == []
