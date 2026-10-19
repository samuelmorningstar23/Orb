"""Live dictation bounds, scoped authorization and stale-result isolation."""
import asyncio
import threading

import pytest

from ws import scribe_stream as live


@pytest.fixture
def stream(monkeypatch):
    events = []
    principal = {"user_id": "synthetic-doctor", "role": "doctor", "department": "synthetic"}
    async def get_session(sid):
        return principal
    async def emit(event, data, to=None):
        events.append((event, data, to))
    monkeypatch.setattr(live.sio, "get_session", get_session)
    monkeypatch.setattr(live.sio, "emit", emit)
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    monkeypatch.setattr(live, "_sessions", {})
    monkeypatch.setattr(live, "_jobs", set())
    monkeypatch.setattr(live, "_inference_slots", asyncio.Semaphore(2))
    return events, principal


async def start(sid="live-1", **kwargs):
    await live.handle_start_session(sid, {"stream_id": sid, **kwargs})
    return live._get_session(sid)


async def drain():
    if live._jobs:
        await asyncio.gather(*list(live._jobs))


@pytest.mark.parametrize("role", ["patient", "pharmacist", "registration", "unknown"])
async def test_live_requires_scribe_capability_before_hint_read(stream, monkeypatch, role):
    events, principal = stream
    principal["role"] = role
    monkeypatch.setattr("services.scribe_service.build_whisper_hints", lambda *_: pytest.fail("unauthorized hint read"))
    assert await start(patient_id="synthetic-patient") is None
    assert events[-1][1]["code"] == "scribe_forbidden"


async def test_patient_scope_precedes_hint_read_and_is_rechecked(stream, monkeypatch):
    from fastapi import HTTPException
    events, principal = stream
    allowed = True
    def scope(user, pid):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        assert user["id"] == principal["user_id"] and pid == "synthetic-patient"
        if not allowed:
            raise HTTPException(404)
    monkeypatch.setattr("permissions.enforce_patient_scope", scope)
    def hints(pid):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return "synthetic vocabulary"
    monkeypatch.setattr("services.scribe_service.build_whisper_hints", hints)
    session = await start(patient_id="synthetic-patient")
    assert session and "synthetic vocabulary" in session["hints"]
    allowed = False
    await live.handle_audio_chunk("live-1", b"\0\0" * 2000)
    assert live._get_session("live-1") is None and not session["audio_buffer"]
    assert events[-1][1]["code"] == "scribe_forbidden"
    monkeypatch.setattr("services.scribe_service.build_whisper_hints", lambda *_: pytest.fail("foreign hint read"))
    assert await start(patient_id="synthetic-patient") is None


@pytest.mark.parametrize("payload", [[], {"audio": [1, 2]}, b"x", b"\0" * (live.MAX_CHUNK_BYTES + 2)])
async def test_malformed_and_oversized_chunks_stop_session(stream, payload):
    events, _ = stream
    session = await start()
    await live.handle_audio_chunk("live-1", payload)
    assert live._get_session("live-1") is None and not session["audio_buffer"]
    assert events[-1][0] == "scribe:error"


async def test_audio_session_and_client_counts_are_bounded(stream, monkeypatch):
    events, _ = stream
    monkeypatch.setattr(live, "MAX_AUDIO_BYTES", 4000)
    session = await start()
    await live.handle_audio_chunk("live-1", b"\0" * 4000)
    await live.handle_audio_chunk("live-1", b"\0\0")
    assert not session["audio_buffer"] and events[-1][1]["code"] == "scribe_audio_limit"
    monkeypatch.setattr(live, "MAX_SESSIONS", 1)
    assert await start("first")
    assert await start("second") is None
    live._sessions["first"]["created_at"] -= live.MAX_SESSION_SECONDS + 1
    await live.handle_end_utterance("first")
    assert live._get_session("first") is None
    assert events[-1][1]["code"] == "scribe_session_limit"


async def test_inference_queue_is_bounded_and_disconnect_keeps_native_slots(stream, monkeypatch):
    events, _ = stream
    entered, release = threading.Event(), threading.Event()
    running = 0
    peak = 0
    lock = threading.Lock()
    def transcribe(*args):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        entered.set()
        assert release.wait(5)
        with lock:
            running -= 1
        return "synthetic words", 1
    monkeypatch.setattr(live, "_transcribe_buffer", transcribe)
    try:
        for idx in range(live.MAX_JOBS):
            sid = f"client-{idx}"
            await start(sid)
            await live.handle_audio_chunk(sid, b"\0" * 4000)
            await live.handle_end_utterance(sid)
        assert await asyncio.to_thread(entered.wait, 2)
        await live.handle_scribe_disconnect("client-0")
        assert len(live._jobs) == live.MAX_JOBS
        await start("overflow")
        await live.handle_audio_chunk("overflow", b"\0" * 4000)
        await live.handle_end_utterance("overflow")
        assert events[-1][1]["code"] == "scribe_busy"
        assert live._get_session("overflow") is None and len(live._jobs) == live.MAX_JOBS
    finally:
        release.set()
        await drain()
    assert peak <= 2
    assert not any(e[0] == "scribe:transcript" and e[2] == "client-0" for e in events)


async def test_replacement_session_never_receives_retired_transcript(stream, monkeypatch):
    events, _ = stream
    entered, release = threading.Event(), threading.Event()
    def transcribe(*args):
        entered.set()
        assert release.wait(5)
        return "retired private words", 1
    monkeypatch.setattr(live, "_transcribe_buffer", transcribe)
    await start()
    await live.handle_audio_chunk("live-1", b"\0" * 4000)
    await live.handle_end_utterance("live-1")
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await live.handle_scribe_disconnect("live-1")
        replacement = await start()
        assert replacement
    finally:
        release.set()
        await drain()
    assert not any(e[0] == "scribe:transcript" for e in events)


async def test_stop_flushes_in_order_with_stream_identity(stream, monkeypatch):
    events, _ = stream
    monkeypatch.setattr(live, "_transcribe_buffer", lambda audio, *_: (str(audio[0]), 1))
    await start()
    await live.handle_audio_chunk("live-1", b"\1\0" * 2000)
    await live.handle_end_utterance("live-1")
    await live.handle_audio_chunk("live-1", b"\2\0" * 2000)
    await live.handle_stop_session("live-1")
    transcripts = [data for event, data, _ in events if event == "scribe:transcript"]
    assert [x["text"] for x in transcripts] == ["1", "2"]
    assert [x["utterance_index"] for x in transcripts] == [0, 1]
    assert all(x["stream_id"] == "live-1" for x in transcripts)
    assert events[-1][1]["status"] == "stopped" and live._get_session("live-1") is None


async def test_expired_idle_sessions_cannot_exhaust_session_admission(stream, monkeypatch):
    events, _ = stream
    monkeypatch.setattr(live, "MAX_SESSIONS", 1)
    old = await start("idle")
    old["created_at"] -= live.MAX_SESSION_SECONDS + 1
    assert await start("replacement") is not None
    assert set(live._sessions) == {"replacement"}
    assert any(data.get("code") == "scribe_session_limit" for _, data, _ in events)
