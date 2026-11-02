"""Request limits cover streamed bodies before parsing and bounded media reads."""
import json
from io import BytesIO

import pytest
from fastapi import FastAPI, File, Request, UploadFile
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from request_limits import RequestBodyLimitMiddleware


async def _request(app, chunks, *, headers=(), path="/", method="POST"):
    """Drive raw ASGI frames: TestClient may coalesce a generator into one frame."""
    messages = iter([
        {"type": "http.request", "body": chunk,
         "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ] or [{"type": "http.request", "body": b"", "more_body": False}])
    received = []
    sent = []

    async def receive():
        message = next(messages, {"type": "http.disconnect"})
        received.append(message)
        return message

    async def send(message):
        sent.append(message)

    await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
               "method": method, "path": path, "raw_path": path.encode(),
               "root_path": "", "query_string": b"", "headers": list(headers),
               "scheme": "http", "server": ("testserver", 80),
               "client": ("testclient", 123)}, receive, send)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, sent
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return starts[0]["status"], body, received


async def _consumer(scope, receive, send):
    while True:
        message = await receive()
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"accepted"})


@pytest.mark.parametrize("headers", [(), ((b"content-length", b"1"),),
                                    ((b"transfer-encoding", b"chunked"),)])
async def test_actual_bytes_reject_oversized_streams(headers):
    app = RequestBodyLimitMiddleware(_consumer, json_limit=8)
    status, body, received = await _request(app, [b"1234", b"5678", b"9", b"unread"],
                                          headers=headers)
    assert status == 413
    assert json.loads(body) == {"detail": "Request body too large"}
    assert len(received) == 3, "stop pulling input at the first over-limit frame"


async def test_exact_limit_accepts_all_frames():
    app = RequestBodyLimitMiddleware(_consumer, json_limit=8)
    status, body, _ = await _request(app, [b"1234", b"5678"])
    assert (status, body) == (200, b"accepted")


@pytest.mark.parametrize("length", [b"9", b"9" * 5000])
async def test_declared_oversize_refuses_without_receiving(length):
    app = RequestBodyLimitMiddleware(_consumer, json_limit=8)
    status, _, received = await _request(app, [b"unread"],
                                         headers=[(b"content-length", length)])
    assert status == 413
    assert received == []


@pytest.mark.parametrize("length", [b"-1", b"+1", b"", b"1, 1", b"nan"])
async def test_malformed_length_is_a_bad_request(length):
    app = RequestBodyLimitMiddleware(_consumer, json_limit=8)
    status, body, received = await _request(app, [b"unread"],
                                          headers=[(b"content-length", length)])
    assert status == 400
    assert json.loads(body) == {"detail": "Invalid Content-Length"}
    assert received == []


async def test_conflicting_lengths_are_rejected_and_identical_ones_work():
    app = RequestBodyLimitMiddleware(_consumer, json_limit=8)
    status, _, _ = await _request(app, [b"1234"], headers=[
        (b"content-length", b"4"), (b"content-length", b"5")])
    assert status == 400
    status, _, _ = await _request(app, [b"1234"], headers=[
        (b"content-length", b"4"), (b"content-length", b"004")])
    assert status == 200


async def test_multipart_path_override_preserves_a_larger_import_limit():
    app = RequestBodyLimitMiddleware(_consumer, json_limit=8, multipart_limit=8,
                                    multipart_limits={"/import": 32})
    headers = [(b"content-type", b"MULTIPART/form-data; boundary=boundary")]
    status, _, _ = await _request(app, [b"x" * 32], path="/import", headers=headers)
    assert status == 200
    status, _, _ = await _request(app, [b"x" * 33], path="/import", headers=headers)
    assert status == 413
    status, _, _ = await _request(app, [b"x" * 9], path="/json", headers=headers)
    assert status == 413, "a multipart label must not unlock larger JSON requests"


async def test_fastapi_multipart_parser_obeys_total_limit_before_the_handler():
    app = FastAPI()
    entered = []

    @app.post("/")
    async def upload(file: UploadFile = File(...)):
        entered.append(True)
        return {"size": file.size}

    app.add_middleware(RequestBodyLimitMiddleware, json_limit=8, multipart_limit=256)
    prefix = (b'--boundary\r\nContent-Disposition: form-data; name="file"; '
              b'filename="audio.webm"\r\nContent-Type: audio/webm\r\n\r\n')
    headers = [(b"content-type", b"multipart/form-data; boundary=boundary")]
    status, body, _ = await _request(app, [prefix, b"a" * 200, b"\r\n--boundary--\r\n"],
                                   headers=headers)
    assert status == 413
    assert json.loads(body) == {"detail": "Request body too large"}
    assert entered == []


async def test_multipart_within_total_limit_is_parsed_normally():
    app = FastAPI()

    @app.post("/")
    async def upload(file: UploadFile = File(...)):
        return {"size": file.size}

    app.add_middleware(RequestBodyLimitMiddleware, json_limit=8, multipart_limit=256)
    body = (b'--boundary\r\nContent-Disposition: form-data; name="file"; '
            b'filename="audio.webm"\r\n\r\nhello\r\n--boundary--\r\n')
    status, response, _ = await _request(app, [body], headers=[
        (b"content-type", b"multipart/form-data; boundary=boundary")])
    assert status == 200
    assert json.loads(response) == {"size": 5}


async def test_fastapi_json_parsing_and_response_streams_remain_intact():
    app = FastAPI()

    @app.post("/")
    async def echo(request: Request):
        data = await request.json()

        async def stream():
            yield data["value"].encode()
            yield b"large response is not a request body" * 10

        return StreamingResponse(stream())

    app.add_middleware(RequestBodyLimitMiddleware, json_limit=20)
    status, body, _ = await _request(app, [b'{"value":', b'"ok"}'])
    assert status == 200
    assert body.startswith(b"oklarge response")
    status, body, _ = await _request(app, [b'{"value":"', b"a" * 30, b'"}'])
    assert status == 413
    assert json.loads(body)["detail"] == "Request body too large"


async def test_non_http_scopes_pass_through_without_receiving():
    scopes = []

    async def passthrough(scope, receive, send):
        scopes.append(scope)

    async def unused():
        raise AssertionError("middleware must not read websocket/lifespan messages")

    app = RequestBodyLimitMiddleware(passthrough)
    for scope in [{"type": "websocket"}, {"type": "lifespan"}]:
        await app(scope, unused, unused)
    assert scopes == [{"type": "websocket"}, {"type": "lifespan"}]


async def test_full_app_streamed_json_limit_keeps_security_headers():
    from main import _MAX_JSON_BODY_BYTES, app

    response_headers = {}

    async def capture_headers(scope, receive, send):
        async def capture(message):
            if message["type"] == "http.response.start":
                response_headers.update(dict(message["headers"]))
            await send(message)
        await app(scope, receive, capture)

    status, body, _ = await _request(capture_headers, [b"x" * _MAX_JSON_BODY_BYTES, b"x"],
                                   path="/api/auth/login", headers=[
                                       (b"host", b"testserver"),
                                       (b"content-type", b"application/json")])
    assert status == 413
    assert json.loads(body)["detail"] == "Request body too large"
    assert response_headers[b"x-content-type-options"] == b"nosniff"
    assert response_headers[b"referrer-policy"] == b"no-referrer"


def test_every_file_upload_route_has_a_limit_matching_its_service():
    from fastapi.params import File as FileInfo
    from fastapi.routing import APIRoute
    from main import _MULTIPART_BODY_LIMITS, app
    from request_limits import MAX_AUDIO_BYTES, MAX_IMAGE_BYTES, MULTIPART_FRAMING_BYTES
    from services import history_import_service, roster_import

    upload_paths = {route.path for route in app.routes if isinstance(route, APIRoute)
                    and any(isinstance(field.field_info, FileInfo)
                            for field in route.dependant.body_params)}
    assert upload_paths == set(_MULTIPART_BODY_LIMITS)
    for path, limit in _MULTIPART_BODY_LIMITS.items():
        file_cap = (history_import_service.MAX_BYTES if "history-import" in path else
                    roster_import.MAX_CSV_BYTES if "roster-import" in path else
                    MAX_IMAGE_BYTES if "/lens/" in path else MAX_AUDIO_BYTES)
        assert limit == file_cap + MULTIPART_FRAMING_BYTES


@pytest.mark.parametrize("lane", ["lens", "scribe", "chunk"])
async def test_media_routes_read_at_most_the_file_cap_plus_one(monkeypatch, lane):
    from routes import lens_routes, scribe_routes

    async def allowed_scope(*args, **kwargs):
        return None

    monkeypatch.setattr(lens_routes, "run_db", allowed_scope)
    monkeypatch.setattr(scribe_routes, "run_db", allowed_scope)
    monkeypatch.setattr("config.LENS_ENABLED", True)
    monkeypatch.setattr(lens_routes, "MAX_IMAGE_BYTES", 128)
    monkeypatch.setattr(scribe_routes, "MAX_AUDIO_BYTES", 128)
    upload = StarletteUploadFile(BytesIO(b"x" * 1000), filename="test.webm")
    reads = []
    original_read = upload.read

    async def tracked_read(size=-1):
        reads.append(size)
        return await original_read(size)

    upload.read = tracked_read
    try:
        with pytest.raises(HTTPException) as exc:
            if lane == "lens":
                await lens_routes.analyze_image(image=upload, clinical_context="",
                                                patient_id="", image_type="other", user={})
            elif lane == "scribe":
                await scribe_routes.transcribe(audio=upload, patient_id="", user={})
            else:
                await scribe_routes.transcribe_chunk(audio=upload, patient_id="", user={})
        assert getattr(exc.value, "status_code", None) == 413
        assert reads == [129]
        assert upload.file.tell() == 129
    finally:
        await upload.close()
