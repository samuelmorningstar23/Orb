"""Unexpected uploaded-media failures stay server-side and visibly fail HTTP."""
import asyncio
from contextlib import contextmanager
from io import BytesIO
import os
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.datastructures import UploadFile

from auth import get_current_user
from routes import lens_routes, scribe_routes
from services import crypto_service as crypto


@pytest.fixture
def media_client(monkeypatch, tmp_path):
    monkeypatch.setattr(lens_routes, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(scribe_routes, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("config.LENS_ENABLED", True)
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    monkeypatch.setattr(crypto, "UPLOAD_ENCRYPTION_KEY", None)
    monkeypatch.setenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "0")

    async def no_patient_scope(fn, user, patient_id):
        assert fn is scribe_routes.enforce_patient_scope
        assert patient_id == ""

    monkeypatch.setattr(scribe_routes, "run_db", no_patient_scope)
    app = FastAPI()
    app.include_router(lens_routes.router)
    app.include_router(scribe_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "test-clinician", "role": "doctor"}
    return TestClient(app)


@pytest.mark.parametrize("lane,fault,event,detail", [
    ("lens", "storage", "lens_upload_failed", "Image upload failed."),
    ("scribe", "storage", "scribe_transcription_failed", "Transcription failed."),
    ("scribe", "materialize", "scribe_transcription_failed", "Transcription failed."),
    ("scribe", "asr", "scribe_transcription_failed", "Transcription failed."),
    ("chunk", "storage", "scribe_chunk_failed", "Chunk transcription failed."),
    ("chunk", "materialize", "scribe_chunk_failed", "Chunk transcription failed."),
    ("chunk", "asr", "scribe_chunk_failed", "Chunk transcription failed."),
])
def test_media_failure_is_generic_logged_and_chunk_cleanup_runs(
    media_client, tmp_path, monkeypatch, caplog, lane, fault, event, detail,
):
    private = "/private/records/scan.png Jane Confidential MRN-SECRET"

    def fail(*args, **kwargs):
        # Crypto/filesystem and ASR work must not run on the event loop.
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        raise OSError(private)

    @contextmanager
    def fail_materialize(*args, **kwargs):
        fail()
        yield

    if fault == "storage":
        monkeypatch.setattr(crypto, "write_encrypted", fail)
    elif fault == "materialize":
        monkeypatch.setattr(crypto, "materialize_plaintext", fail_materialize)
    else:
        monkeypatch.setattr(scribe_routes, "transcribe_audio", fail)

    if lane == "lens":
        path, field, filename, content = "/api/lens/analyze", "image", "scan.png", b"\x89PNG\r\n\x1a\n" + b"x" * 128
    else:
        path = "/api/scribe/transcribe-chunk" if lane == "chunk" else "/api/scribe/transcribe"
        field, filename, content = "audio", "voice.webm", b"x" * 128
    response = media_client.post(path, files={field: (filename, content)})
    assert response.status_code == 500
    assert response.json() == {"detail": detail}
    assert private not in response.text and "Confidential" not in response.text
    failures = [record for record in caplog.records if getattr(record, "event", None) == event]
    assert len(failures) == 1 and failures[0].exc_info
    if lane == "chunk":
        assert list(tmp_path.iterdir()) == []


async def test_cancelled_chunk_still_cleans_up_after_its_worker_finishes(
    media_client, tmp_path, monkeypatch,
):
    started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    write = crypto.write_encrypted
    remove = os.remove

    def delayed_write(path, data):
        started.set()
        assert release.wait(5), "test must release the cancelled request's worker"
        write(path, data)

    def observe_cleanup(path):
        remove(path)
        cleaned.set()

    monkeypatch.setattr(crypto, "write_encrypted", delayed_write)
    monkeypatch.setattr(scribe_routes.os, "remove", observe_cleanup)
    monkeypatch.setattr(scribe_routes, "transcribe_audio", lambda *args, **kwargs: "dictated words")
    audio = UploadFile(BytesIO(b"x" * 128), filename="chunk.webm")
    task = asyncio.create_task(scribe_routes.transcribe_chunk(
        audio=audio, chunk_index=0, hints="", patient_id="", user={"id": "test-clinician"},
    ))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(cleaned.wait, 2)
        await audio.close()
    assert list(tmp_path.iterdir()) == []
