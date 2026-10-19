"""Failed ASR attempts cannot masquerade as silence or leak private errors."""
import asyncio
import json
import subprocess
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from auth import get_current_user
from routes import scribe_routes
from services import crypto_service as crypto, scribe_service as svc
from ws import scribe_stream

PRIVATE = "/private/audio/SECRET-MRN.wav Confidential dictation"
CFG = {"model": "synthetic-local-model", "batch_size": 1}


@pytest.fixture(autouse=True)
def models_on(monkeypatch):
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)


def worker(monkeypatch, stdout, *, exit_code=0):
    def run(command, **kwargs):
        assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["timeout"] == 120
        return SimpleNamespace(returncode=exit_code, stdout=stdout, stderr=PRIVATE)
    monkeypatch.setattr(svc.subprocess, "run", run)


@pytest.mark.parametrize("stdout,exit_code,code", [
    ('{"text":"ignored"}', 1, "asr_worker_failed"),
    ("", 2, "asr_worker_failed"),
    (PRIVATE, 0, "asr_worker_protocol_failed"),
    ("{}", 0, "asr_worker_protocol_failed"),
    ('{"text":null}', 0, "asr_worker_protocol_failed"),
    ('{"text":7}', 0, "asr_worker_protocol_failed"),
    ("[]", 0, "asr_worker_protocol_failed"),
    ('{"text":"","error":"private model error"}', 0, "asr_worker_protocol_failed"),
])
def test_failed_workers_raise_typed_generic_failure(monkeypatch, stdout, exit_code, code):
    worker(monkeypatch, stdout, exit_code=exit_code)
    with pytest.raises(svc.TranscriptionError) as exc:
        svc._transcribe_via_subprocess("synthetic.wav", "", CFG)
    assert exc.value.code == code
    assert str(exc.value) == "Speech transcription could not be completed. Please retry."
    assert PRIVATE not in str(exc.value)


@pytest.mark.parametrize("failure,code", [(subprocess.TimeoutExpired("synthetic", 120), "asr_worker_timeout"), (OSError(PRIVATE), "asr_worker_unavailable")])
def test_timeout_and_process_failure_are_not_silence(monkeypatch, failure, code):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(svc.subprocess, "run", fail)
    with pytest.raises(svc.TranscriptionError) as exc:
        svc._transcribe_via_subprocess("synthetic.wav", "", CFG)
    assert exc.value.code == code and PRIVATE not in str(exc.value)


@pytest.mark.parametrize("framed", [False, True])
@pytest.mark.parametrize("text", ["", "Synthetic dictated words", "Literal ###RESULT### inside dictation"])
def test_valid_silence_and_text_survive_both_worker_protocols(monkeypatch, framed, text):
    output = json.dumps({"text": text})
    if framed:
        output = "Library startup log\n###RESULT###" + output + "###RESULT###\n"
    worker(monkeypatch, output)
    assert svc._transcribe_via_subprocess("synthetic.wav", "", CFG) == text


def test_successful_silence_skips_correction_but_worker_failure_propagates(monkeypatch):
    monkeypatch.setattr(svc, "_transcribe_via_daemon", lambda *args: None)
    monkeypatch.setattr(svc, "_apply_rxnorm_correction", lambda *_: pytest.fail("silence must not enter correction"))
    worker(monkeypatch, '{"text":""}')
    assert svc.transcribe_audio("synthetic.wav", apply_correction=False) == ""
    worker(monkeypatch, PRIVATE, exit_code=2)
    with pytest.raises(svc.TranscriptionError) as exc:
        svc.transcribe_audio("synthetic.wav", apply_correction=False)
    assert exc.value.code == "asr_worker_failed"


@pytest.fixture
def audio_client(monkeypatch, tmp_path):
    monkeypatch.setattr(scribe_routes, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(scribe_routes, "enforce_patient_scope", lambda *_: None)
    monkeypatch.setattr(svc, "_transcribe_via_daemon", lambda *args: None)
    monkeypatch.setattr(crypto, "UPLOAD_ENCRYPTION_KEY", None)
    monkeypatch.setenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "0")
    app = FastAPI()
    app.include_router(scribe_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "synthetic-clinician", "role": "doctor"}
    with TestClient(app) as client:
        yield client


def test_real_worker_failure_fails_http_chunk_and_removes_owned_audio(audio_client, monkeypatch, tmp_path):
    worker(monkeypatch, PRIVATE, exit_code=2)
    response = audio_client.post("/api/scribe/transcribe-chunk", files={"audio": ("synthetic.webm", b"synthetic audio" * 10)})
    assert response.status_code == 500
    assert response.json() == {"detail": "Chunk transcription failed."}
    assert PRIVATE not in response.text and list(tmp_path.iterdir()) == []


@pytest.fixture
def live_audio(monkeypatch, tmp_path):
    create = scribe_stream.tempfile.mkstemp
    monkeypatch.setattr(scribe_stream.tempfile, "mkstemp", lambda **kwargs: create(dir=tmp_path, **kwargs))
    monkeypatch.setattr(svc, "_transcribe_via_daemon", lambda *args: None)
    monkeypatch.setattr(svc, "_apply_rxnorm_correction", lambda text: text)
    sent = []

    async def emit(event, data, to=None):
        sent.append((event, data, to))
    monkeypatch.setattr(scribe_stream.sio, "emit", emit)
    session = scribe_stream._create_session("synthetic-session")
    session["audio_buffer"] = bytearray(b"\x01\x00" * 2000)
    async def active(_sid):
        return session
    monkeypatch.setattr(scribe_stream, "_active_session", active)
    return sent, session


async def test_live_worker_failure_is_generic_logged_and_cleans_wav(live_audio, monkeypatch, tmp_path, caplog):
    sent, session = live_audio
    worker(monkeypatch, PRIVATE, exit_code=2)
    await scribe_stream._transcribe_utterance("synthetic-session", session)
    assert [item[0] for item in sent] == ["scribe:status", "scribe:error", "scribe:status"]
    assert sent[1] == ("scribe:error", {"error": "Speech transcription failed. Please retry.", "code": "scribe_transcription_failed", "retryable": True, "stream_id": ""}, "synthetic-session")
    assert PRIVATE not in str(sent) and list(tmp_path.iterdir()) == []
    assert any(getattr(r, "event", None) == "scribe_live_transcription_failed" and r.exc_info for r in caplog.records)


async def test_live_silence_remains_ready_without_a_failure(live_audio, monkeypatch, tmp_path):
    sent, session = live_audio
    worker(monkeypatch, '{"text":""}')
    await scribe_stream._transcribe_utterance("synthetic-session", session)
    assert [item[0] for item in sent] == ["scribe:status", "scribe:status"]
    assert sent[-1][1] == {"status": "ready", "stream_id": ""} and list(tmp_path.iterdir()) == []


async def test_cancelled_live_worker_cleans_audio_created_after_cancellation(live_audio, monkeypatch, tmp_path):
    sent, session = live_audio
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    write, transcribe = scribe_stream._write_wav, scribe_stream._transcribe_buffer

    def delayed_write(data):
        started.set()
        assert release.wait(3)
        return write(data)

    def observe(*args):
        try:
            return transcribe(*args)
        finally:
            finished.set()

    monkeypatch.setattr(scribe_stream, "_write_wav", delayed_write)
    monkeypatch.setattr(scribe_stream, "_transcribe_buffer", observe)
    worker(monkeypatch, '{"text":"Synthetic words"}')
    task = asyncio.create_task(scribe_stream._transcribe_utterance("synthetic-session", session))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert [item[0] for item in sent] == ["scribe:status"]
    assert list(tmp_path.iterdir()) == []
