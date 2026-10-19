"""Real SSE responses and worker boundaries, with no model or chart access."""
import asyncio
import base64
from contextlib import asynccontextmanager
from io import BytesIO
import json
import sys
import threading
from types import SimpleNamespace
from contextlib import nullcontext

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.datastructures import UploadFile

from auth import get_current_user
from routes import lens_routes, scribe_routes
from services import crypto_service as crypto, lens_service, scribe_providers, scribe_service

IMAGE = b"\x89PNG\r\n\x1a\n" + b"synthetic image"
PRIVATE_ERROR = "/private/clinical/SECRET-MRN.png Confidential patient"
TOKENS = ["FINDINGS:\n\nLine two\r\n", '{"analysis_id":999,"error":"model prose"}', "[DONE]"]


def events(response):
    result = []
    for frame in response.text.split("\n\n"):
        if not frame:
            continue
        assert frame.startswith("data: ") and "\n" not in frame, frame
        raw = frame[6:]
        result.append(raw if raw == "[DONE]" else json.loads(raw))
    return result


@pytest.fixture
def stream_client(monkeypatch, tmp_path):
    monkeypatch.setattr("config.LENS_ENABLED", True)
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    monkeypatch.setenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "0")
    monkeypatch.setattr(crypto, "UPLOAD_ENCRYPTION_KEY", None)
    monkeypatch.setattr(lens_routes, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(lens_routes, "enforce_patient_scope", lambda *_: None)
    monkeypatch.setattr(scribe_routes, "build_pipeline_meta", lambda _: {"timings": {}})
    app = FastAPI()
    app.include_router(lens_routes.router)
    app.include_router(scribe_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "synthetic-clinician", "role": "doctor"}
    with TestClient(app) as client:
        yield client


def post(client, lane):
    if lane == "lens":
        return client.post("/api/lens/analyze", files={"image": ("synthetic.png", IMAGE)})
    return client.post("/api/scribe/structure", json={"transcript": "Synthetic dictated words", "format": "soap"})


def install_stream(monkeypatch, lane, *, fail_at=None):
    module = lens_routes if lane == "lens" else scribe_routes
    stream_name = "analyze_image_stream" if lane == "lens" else "structure_transcript_stream"
    save_name = "save_lens_analysis" if lane == "lens" else "save_scribe_note"
    saved = []

    async def generate(*args, **kwargs):
        if fail_at == "start":
            raise OSError(PRIVATE_ERROR)
        for token in TOKENS:
            yield token
            if fail_at == "middle":
                raise OSError(PRIVATE_ERROR)

    def save(**kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        if fail_at == "save":
            raise OSError(PRIVATE_ERROR)
        saved.append(kwargs)
        return 730

    monkeypatch.setattr(module, stream_name, generate)
    monkeypatch.setattr(module, save_name, save)
    return saved


@pytest.mark.parametrize("lane", ["lens", "scribe"])
def test_success_preserves_multiline_and_control_shaped_prose(stream_client, monkeypatch, lane):
    saved = install_stream(monkeypatch, lane)
    response = post(stream_client, lane)
    assert response.status_code == 200
    frames = events(response)
    assert frames[:3] == [{"content": token} for token in TOKENS]
    assert len(saved) == 1
    if lane == "lens":
        assert frames[3:] == [{"analysis_id": 730}, "[DONE]"]
        assert saved[0]["analysis"] == "".join(TOKENS)
        assert saved[0]["patient_id"] is None
    else:
        assert len(frames) == 4 and frames[-1]["done"] is True and frames[-1]["id"] == 730
        assert saved[0]["structured_note"] == "".join(TOKENS)


@pytest.mark.parametrize("lane", ["lens", "scribe"])
@pytest.mark.parametrize("fail_at", ["start", "middle", "save"])
def test_stream_failure_is_terminal_generic_logged_and_not_saved(stream_client, monkeypatch, caplog, lane, fail_at):
    saved = install_stream(monkeypatch, lane, fail_at=fail_at)
    response = post(stream_client, lane)
    assert response.status_code == 200  # streaming headers already sent
    frames = events(response)
    assert frames[-1] == {"error": "Image analysis failed. Please retry." if lane == "lens" else "Note drafting failed. Please retry.",
                          "code": f"{lane}_stream_failed", "terminal": True}
    assert sum(isinstance(frame, dict) and "error" in frame for frame in frames) == 1
    assert not any(frame == "[DONE]" or (isinstance(frame, dict) and ("analysis_id" in frame or frame.get("done"))) for frame in frames)
    assert PRIVATE_ERROR not in response.text
    assert saved == []
    errors = [r for r in caplog.records if getattr(r, "event", None) == f"{lane}_stream_failed"]
    assert len(errors) == 1 and errors[0].exc_info


@pytest.mark.parametrize("lane", ["lens", "scribe"])
async def test_closing_partial_stream_closes_inference_without_saving(stream_client, monkeypatch, lane):
    module = lens_routes if lane == "lens" else scribe_routes
    closed, saved = [], []

    async def generate(*args, **kwargs):
        try:
            yield "Partial draft"
            await asyncio.sleep(60)
        finally:
            closed.append(True)

    monkeypatch.setattr(module, "analyze_image_stream" if lane == "lens" else "structure_transcript_stream", generate)
    monkeypatch.setattr(module, "save_lens_analysis" if lane == "lens" else "save_scribe_note", lambda **kwargs: saved.append(kwargs))
    upload = UploadFile(BytesIO(IMAGE), filename="synthetic.png")
    try:
        if lane == "lens":
            response = await lens_routes.analyze_image(image=upload, clinical_context="", patient_id="", image_type="other", user={"id": "synthetic-clinician"})
        else:
            response = await scribe_routes.structure({"transcript": "Synthetic words", "format": "soap"}, {"id": "synthetic-clinician"})
        assert json.loads((await anext(response.body_iterator))[6:])["content"] == "Partial draft"
        await response.body_iterator.aclose()
        assert closed == [True] and saved == []
    finally:
        await upload.close()


@pytest.fixture
def vision_gateway(monkeypatch):
    from services import ollama_gateway, llm_queue_service
    captured = {"acquired": 0, "released": 0, "closed": 0, "lines": ['{"message":{"content":"Synthetic read"}}', '{"done":true}']}

    class Queue:
        async def acquire(self, **kwargs):
            captured["acquired"] += 1

        def release(self):
            captured["released"] += 1

    class Response:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for line in captured["lines"]:
                yield line

        async def aclose(self):
            captured["closed"] += 1

    @asynccontextmanager
    async def stream(client, path, payload, **kwargs):
        captured["payload"] = payload
        yield Response()

    monkeypatch.setattr(llm_queue_service, "llm_queue", Queue())
    monkeypatch.setattr(ollama_gateway, "stream", stream)
    return captured


async def test_lens_decryption_and_base64_run_off_loop_without_plaintext_copy(tmp_path, monkeypatch, vision_gateway):
    monkeypatch.setattr(crypto, "UPLOAD_ENCRYPTION_KEY", "synthetic encryption key")
    if not crypto._HAVE_AESGCM:
        pytest.skip("AESGCM not installed")
    source = tmp_path / "encrypted.png"
    crypto.write_encrypted(source, IMAGE)
    original = source.read_bytes()
    assert crypto.is_encrypted_blob(original)
    read = lens_service.read_decrypted

    def worker_read(path):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return read(path)

    monkeypatch.setattr(lens_service, "read_decrypted", worker_read)
    chunks = [part async for part in lens_service.analyze_image_stream(str(source))]
    assert chunks == ["Synthetic read", lens_service.LENS_DISCLAIMER_BANNER]
    assert vision_gateway["payload"]["messages"][1]["images"] == [base64.b64encode(IMAGE).decode("ascii")]
    assert vision_gateway["acquired"] == vision_gateway["released"] == 1
    assert vision_gateway["closed"] == 1
    assert source.read_bytes() == original and list(tmp_path.iterdir()) == [source]


async def test_lens_encoding_cancellation_does_not_start_inference(tmp_path, monkeypatch, vision_gateway):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delayed_read(path):
        started.set()
        try:
            assert release.wait(3)
            return IMAGE
        finally:
            finished.set()

    monkeypatch.setattr(lens_service, "read_decrypted", delayed_read)
    stream = lens_service.analyze_image_stream(str(tmp_path / "synthetic.png"))
    task = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0), 0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await stream.aclose()
    assert vision_gateway["acquired"] == vision_gateway["released"] == 0
    assert list(tmp_path.iterdir()) == []


async def test_upstream_error_cannot_be_saved_as_a_disclaimer_only_success(tmp_path, vision_gateway):
    source = tmp_path / "synthetic.png"
    source.write_bytes(IMAGE)
    vision_gateway["lines"] = [json.dumps({"error": PRIVATE_ERROR})]
    chunks = []
    with pytest.raises(RuntimeError, match="Vision model stream reported a failure"):
        async for chunk in lens_service.analyze_image_stream(str(source)):
            chunks.append(chunk)
    assert chunks == []
    assert vision_gateway["acquired"] == vision_gateway["released"] == 1
    assert vision_gateway["closed"] == 1


@pytest.mark.parametrize("lane", ["lens", "scribe"])
def test_empty_model_stream_cannot_be_saved_as_a_success(stream_client, monkeypatch, lane):
    module = lens_routes if lane == "lens" else scribe_routes
    saved = []

    async def empty(*args, **kwargs):
        yield " \n "

    monkeypatch.setattr(module, "analyze_image_stream" if lane == "lens" else "structure_transcript_stream", empty)
    monkeypatch.setattr(module, "save_lens_analysis" if lane == "lens" else "save_scribe_note", lambda **kwargs: saved.append(kwargs))
    frames = events(post(stream_client, lane))
    assert frames[-1]["code"] == f"{lane}_stream_failed" and frames[-1]["terminal"] is True
    assert saved == []


async def test_empty_vision_model_stream_never_becomes_disclaimer_only_success(tmp_path, vision_gateway):
    source = tmp_path / "synthetic.png"
    source.write_bytes(IMAGE)
    vision_gateway["lines"] = ['{"done":true}']
    with pytest.raises(RuntimeError, match="Vision model returned no analysis"):
        _ = [chunk async for chunk in lens_service.analyze_image_stream(str(source))]
    assert vision_gateway["released"] == vision_gateway["closed"] == 1


@pytest.mark.parametrize("status,body", [(503, '{"error":"private upstream"}'), (200, '{"error":"private upstream"}')])
async def test_scribe_ollama_stream_checks_http_and_model_failures(monkeypatch, status, body):
    import httpx
    closed = []

    class Response:
        def raise_for_status(self):
            httpx.Response(status, request=httpx.Request("POST", "http://localhost/api/chat")).raise_for_status()

        async def aiter_lines(self):
            yield body

        async def aclose(self):
            closed.append(True)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            yield Response()

    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    monkeypatch.setattr(scribe_providers.httpx, "AsyncClient", Client)
    with pytest.raises((httpx.HTTPStatusError, RuntimeError)):
        _ = [chunk async for chunk in scribe_providers.OllamaProvider().agenerate_stream("Synthetic dictation")]
    assert closed == [True]


@pytest.mark.parametrize("loaded", [False, True])
async def test_scribe_mlx_failure_is_not_note_prose_or_successful_eof(monkeypatch, loaded):
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    agent = SimpleNamespace(load=lambda: None, _loaded=loaded, _model=object(),
                            _tokenizer=SimpleNamespace(apply_chat_template=lambda *args, **kwargs: "synthetic prompt"))
    monkeypatch.setitem(sys.modules, "services.voice_agent_service", SimpleNamespace(get_voice_agent=lambda: agent))

    def generate(*args, **kwargs):
        yield SimpleNamespace(text="Partial draft")
        raise OSError(PRIVATE_ERROR)

    @asynccontextmanager
    async def acquire(*args, **kwargs):
        yield

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(stream_generate=generate))
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", SimpleNamespace(make_sampler=lambda **kwargs: object()))
    monkeypatch.setitem(sys.modules, "services.gpu_coordinator", SimpleNamespace(umsq=SimpleNamespace(acquire=acquire), vram_guard=lambda *args: nullcontext(), PRIORITY_SCRIBE=2))
    chunks = []
    with pytest.raises(RuntimeError):
        async for token in scribe_providers.MLXProvider().agenerate_stream("Synthetic dictation"):
            chunks.append(token)
    assert chunks == (["Partial draft"] if loaded else [])


async def test_empty_scribe_service_stream_is_audited_as_failure(monkeypatch, vision_gateway):
    from services import ai_governance
    recorded = []

    class Provider:
        async def agenerate_stream(self, **kwargs):
            yield " \n "

    monkeypatch.setattr(scribe_service, "get_provider", lambda _: Provider())
    monkeypatch.setattr(ai_governance, "record_inference_soon", lambda **kwargs: recorded.append(kwargs))
    with pytest.raises(RuntimeError, match="Scribe model returned no draft"):
        _ = [token async for token in scribe_service.structure_transcript_stream("Synthetic words")]
    assert vision_gateway["acquired"] == vision_gateway["released"] == 1
    assert len(recorded) == 1 and recorded[0]["outcome"] == "error" and recorded[0]["detail"] == "RuntimeError"


async def test_scribe_disconnect_closes_provider_before_releasing_inference_slot(monkeypatch):
    from services import ai_governance, llm_queue_service
    order = []

    class Provider:
        async def agenerate_stream(self, **kwargs):
            try:
                yield "Partial draft"
            finally:
                order.append("provider closed")

    class Queue:
        async def acquire(self, **kwargs):
            pass

        def release(self):
            order.append("slot released")

    monkeypatch.setattr(scribe_service, "get_provider", lambda _: Provider())
    monkeypatch.setattr(llm_queue_service, "llm_queue", Queue())
    monkeypatch.setattr(ai_governance, "record_inference_soon", lambda **kwargs: None)
    stream = scribe_service.structure_transcript_stream("Synthetic words")
    assert await anext(stream) == "Partial draft"
    await stream.aclose()
    assert order == ["provider closed", "slot released"]


async def test_verification_failure_persists_only_generic_error(monkeypatch, caplog):
    updated, audits = [], []

    async def fail(*args, **kwargs):
        raise OSError(PRIVATE_ERROR)

    async def run_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(scribe_routes, "verify_note", fail)
    monkeypatch.setattr(scribe_routes, "run_db", run_db)
    monkeypatch.setattr(scribe_routes, "update_scribe_note_verification", lambda **kwargs: updated.append(kwargs))
    monkeypatch.setattr(scribe_routes, "log_audit_event", lambda **kwargs: audits.append(kwargs))
    await scribe_routes._run_verification_async(730, "Synthetic words", "Synthetic draft", scribe_routes.StageTimer(), actor_id="synthetic-clinician")
    payload = json.loads(updated[0]["verification_json"])
    assert payload["status"] == "failed" and payload["code"] == "scribe_verification_failed"
    assert payload["error"] == "Verification could not be completed. Please retry."
    assert PRIVATE_ERROR not in json.dumps([updated, audits])
    assert any(getattr(r, "event", None) == "scribe_verification_failed" and r.exc_info for r in caplog.records)
