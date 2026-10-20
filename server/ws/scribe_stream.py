"""Orb Scribe — Real-Time Audio Streaming via Socket.IO

Receives PCM16 audio chunks from the client, accumulates them per utterance,
transcribes via the warm Whisper daemon (or subprocess fallback), applies
RxNorm phonetic correction, and streams transcript results back.

Protocol:
    scribe:start_session  → client sends {patient_id?, hints?}
    scribe:audio_chunk    → client sends binary PCM16 audio data
    scribe:end_utterance  → client tells server VAD detected silence
    scribe:stop_session   → cleanup

    scribe:transcript     → server emits {text, duration_ms, is_final, utterance_index}
    scribe:status         → server emits {status}
    scribe:error          → server emits {error}
"""

import asyncio
import logging
import os
import tempfile
import time
import wave

from ws.realtime import sio

logger = logging.getLogger("orb.ws.scribe")

# Per-session state
_sessions: dict[str, dict] = {}
# PCM16 mono at 16 kHz: 60 seconds per utterance, bounded across clients.
MAX_AUDIO_BYTES = 16000 * 2 * 60
MAX_CHUNK_BYTES = 64 * 1024
MAX_SESSIONS = 16
MAX_SESSION_SECONDS = 3600
MAX_JOBS = 4
_jobs: set[asyncio.Task] = set()
_inference_slots = asyncio.Semaphore(2)


async def _authorized(sid: str, patient_id: str = "") -> bool:
    from database import run_db
    from permissions import SCRIBE_CREATE_DRAFT, enforce_patient_scope, has_permission

    try:
        principal = await sio.get_session(sid)
        if not principal or not principal.get("user_id"):
            return False
        if not has_permission(principal.get("role", ""), SCRIBE_CREATE_DRAFT):
            return False
        if patient_id:
            await run_db(
                enforce_patient_scope, {**principal, "id": principal["user_id"]}, patient_id
            )
        return True
    except Exception:
        return False


async def _emit(sid: str, session: dict, event: str, data: dict):
    if _sessions.get(sid) is session and session["is_active"]:
        await sio.emit(event, {**data, "stream_id": session.get("stream_id", "")}, to=sid)


async def _fail(sid: str, session: dict, message: str, code: str):
    await _emit(sid, session, "scribe:error", {"error": message, "code": code, "retryable": False})
    if _sessions.get(sid) is session:
        _destroy_session(sid)


async def _active_session(sid: str) -> dict | None:
    session = _sessions.get(sid)
    if not session or not session["is_active"]:
        return None
    if not await _authorized(sid, session["patient_id"]):
        await _fail(
            sid, session, "Dictation is not authorized for this patient.", "scribe_forbidden"
        )
        return None
    if time.monotonic() - session["created_at"] > MAX_SESSION_SECONDS:
        await _fail(
            sid,
            session,
            "Dictation session expired. Start a new recording.",
            "scribe_session_limit",
        )
        return None
    if _sessions.get(sid) is not session:
        return None
    return session


async def _require_authenticated(sid: str) -> bool:
    """Ensure the Socket.IO client completed the authenticated handshake.

    connect() in ws/realtime.py rejects unauthenticated clients and stores the
    user in the sio session, so an absent/empty session means no valid JWT.
    Scribe streams carry patient audio (PHI), so reject unauthenticated sids.
    """
    try:
        session = await sio.get_session(sid)
    except Exception:
        session = None
    return bool(session and session.get("user_id"))


def _get_session(sid: str) -> dict | None:
    """Get session state for a Socket.IO client."""
    return _sessions.get(sid)


def _create_session(sid: str, patient_id: str = "", hints: str = "", language: str = "") -> dict:
    """Initialize a new streaming session."""
    session = {
        "patient_id": patient_id,
        "hints": hints,
        "language": language,  # selected Whisper language for this live session
        "audio_buffer": bytearray(),
        "utterance_index": 0,
        "is_active": True,
        "created_at": time.monotonic(),
        "stream_id": "",
        "pending": 0,
        "tail": None,
    }
    _sessions[sid] = session
    return session


def _destroy_session(sid: str):
    """Cleanup a session."""
    session = _sessions.pop(sid, None)
    if session:
        session["is_active"] = False
        session["audio_buffer"].clear()
    # Running threads own their WAV until completion. Do not cancel/release their
    # slot early: reconnect loops must not create unlimited native workers.


def _write_wav(pcm_data: bytes, sample_rate: int = 16000) -> str:
    """Write PCM16 to a private, exclusively created temporary WAV file."""
    fd: int | None
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="orb_stream_")
    try:
        raw = os.fdopen(fd, "wb")
        fd = None  # raw owns the descriptor from here, including on errors.
        with raw:
            with wave.open(raw, "wb") as f:
                f.setnchannels(1)
                f.setsampwidth(2)  # 16-bit
                f.setframerate(sample_rate)
                f.writeframes(pcm_data)
        return wav_path
    except BaseException:
        # The caller receives no path when creation fails, so it cannot perform
        # its usual transcription cleanup. Do not orphan partial patient audio.
        try:
            os.remove(wav_path)
        except OSError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)


def _transcribe_buffer(audio_data: bytes, hints: str, language: str) -> tuple[str, int]:
    """Own the temporary WAV for the entire worker, including cancellation.

    Cancelling an await cannot stop a running thread. Its cleanup must therefore
    stay in this worker, even when the request never receives the created path.
    """
    wav_path = None
    try:
        wav_path = _write_wav(audio_data)
        from config import SCRIBE
        from services.scribe_service import (
            _apply_rxnorm_correction,
            _resolve_language,
            _transcribe_via_daemon,
            _transcribe_via_subprocess,
        )

        lang = _resolve_language(language)
        started = time.time()
        result = _transcribe_via_daemon(wav_path, hints, lang)
        if result is None:
            result = _transcribe_via_subprocess(wav_path, hints, SCRIBE["asr"], lang)
        if result:
            result = _apply_rxnorm_correction(result)
        return result.strip() if result else "", int((time.time() - started) * 1000)
    finally:
        if wav_path:
            try:
                os.remove(wav_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning(
                    "Scribe audio cleanup failed",
                    exc_info=True,
                    extra={"event": "scribe_audio_cleanup_failed"},
                )


async def _transcribe_utterance(sid: str, session: dict, audio_data: bytes | None = None):
    """Run one captured utterance; never publish into a replacement session."""
    if audio_data is None:
        audio_data = bytes(session["audio_buffer"])
        session["audio_buffer"].clear()
    utterance_idx = session["utterance_index"]
    session["utterance_index"] += 1
    if len(audio_data) < 3200:
        return
    if await _active_session(sid) is not session:
        return
    await _emit(sid, session, "scribe:status", {"status": "processing"})
    try:
        text, duration_ms = await asyncio.to_thread(
            _transcribe_buffer,
            audio_data,
            session.get("hints", ""),
            session.get("language", ""),
        )
        if await _active_session(sid) is not session:
            return
        if text:
            await _emit(
                sid,
                session,
                "scribe:transcript",
                {
                    "text": text,
                    "duration_ms": duration_ms,
                    "is_final": True,
                    "utterance_index": utterance_idx,
                },
            )
        await _emit(sid, session, "scribe:status", {"status": "ready"})
    except Exception:
        logger.exception(
            "Scribe live transcription failed", extra={"event": "scribe_live_transcription_failed"}
        )
        await _emit(
            sid,
            session,
            "scribe:error",
            {
                "error": "Speech transcription failed. Please retry.",
                "code": "scribe_transcription_failed",
                "retryable": True,
            },
        )
        await _emit(sid, session, "scribe:status", {"status": "ready"})


async def _queue_utterance(sid: str, session: dict):
    if len(session["audio_buffer"]) < 3200:
        session["audio_buffer"].clear()
        return
    if len(_jobs) >= MAX_JOBS or session["pending"] >= 2:
        await _fail(
            sid, session, "Dictation is busy. Stop and retry this utterance.", "scribe_busy"
        )
        return
    audio = bytes(session["audio_buffer"])
    session["audio_buffer"].clear()
    previous = session["tail"]
    session["pending"] += 1

    async def run():
        try:
            if previous:
                await previous
            async with _inference_slots:
                await _transcribe_utterance(sid, session, audio)
        finally:
            session["pending"] -= 1

    task = asyncio.create_task(run())
    session["tail"] = task
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)


# ── Socket.IO Event Handlers ──


@sio.on("scribe:start_session")
async def handle_start_session(sid, data=None):
    """Client starts a new streaming transcription session."""
    if not await _require_authenticated(sid):
        await sio.emit("scribe:error", {"error": "unauthenticated"}, to=sid)
        return
    # Speech transcription is a model: none starts while the models are
    # switched off (ORB_MODELS_ENABLED, or the Orb Ward Record).
    from services.model_switch import SWITCHED_OFF_DETAIL, models_enabled

    if not models_enabled():
        await sio.emit("scribe:error", {"error": SWITCHED_OFF_DETAIL}, to=sid)
        return
    data = data or {}
    if not isinstance(data, dict):
        return
    patient_id = data.get("patient_id") or ""
    hints = data.get("hints") or ""
    language = data.get("language") or ""
    stream_id = data.get("stream_id") or ""
    if (
        any(not isinstance(x, str) for x in (patient_id, hints, language, stream_id))
        or len(patient_id) > 128
        or len(hints) > 8192
        or len(language) > 32
        or len(stream_id) > 128
    ):
        await sio.emit(
            "scribe:error",
            {
                "error": "Invalid dictation settings.",
                "stream_id": stream_id if isinstance(stream_id, str) else "",
            },
            to=sid,
        )
        return
    if not await _authorized(sid, patient_id):
        await sio.emit(
            "scribe:error",
            {
                "error": "Dictation is not authorized for this patient.",
                "code": "scribe_forbidden",
                "stream_id": stream_id,
                "retryable": False,
            },
            to=sid,
        )
        return
    # Expired idle clients must not occupy the admission cap indefinitely.
    for old_sid, old_session in list(_sessions.items()):
        if time.monotonic() - old_session["created_at"] > MAX_SESSION_SECONDS:
            if old_session["is_active"]:
                await _fail(
                    old_sid,
                    old_session,
                    "Dictation session expired. Start a new recording.",
                    "scribe_session_limit",
                )
            else:
                _destroy_session(old_sid)
    if sid in _sessions or len(_sessions) >= MAX_SESSIONS:
        await sio.emit(
            "scribe:error",
            {
                "error": "Dictation is busy. Stop and retry.",
                "code": "scribe_busy",
                "stream_id": stream_id,
                "retryable": False,
            },
            to=sid,
        )
        return
    session = _create_session(sid, patient_id=patient_id, hints=hints, language=language)
    session["stream_id"] = stream_id
    session["is_active"] = False
    try:
        if patient_id:
            from database import run_db
            from services.scribe_service import build_whisper_hints

            db_hints = await run_db(build_whisper_hints, patient_id)
            if db_hints:
                session["hints"] = (f"{hints}, {db_hints}" if hints else db_hints)[:8192]
        if _sessions.get(sid) is not session:
            return
        session["is_active"] = True
        if await _active_session(sid) is session:
            await _emit(sid, session, "scribe:status", {"status": "ready"})
    except Exception:
        session["is_active"] = True
        await _fail(sid, session, "Dictation could not start. Please retry.", "scribe_start_failed")


@sio.on("scribe:audio_chunk")
async def handle_audio_chunk(sid, data):
    """Receive a binary audio chunk from the client (PCM16, 16kHz mono)."""
    session = await _active_session(sid)
    if session is None:
        return
    if session.get("stopping"):
        return
    audio = data.get("audio") if isinstance(data, dict) else data
    if not isinstance(audio, (bytes, bytearray)) or len(audio) % 2:
        await _fail(sid, session, "Invalid audio data. Please retry.", "scribe_invalid_audio")
        return
    if len(audio) > MAX_CHUNK_BYTES or len(session["audio_buffer"]) + len(audio) > MAX_AUDIO_BYTES:
        await _fail(
            sid,
            session,
            "Dictation limit reached. Use shorter utterances and retry.",
            "scribe_audio_limit",
        )
        return
    session["audio_buffer"].extend(audio)


@sio.on("scribe:end_utterance")
async def handle_end_utterance(sid, data=None):
    session = await _active_session(sid)
    if session is not None and not session.get("stopping"):
        await _queue_utterance(sid, session)


@sio.on("scribe:stop_session")
async def handle_stop_session(sid, data=None):
    session = _get_session(sid)
    if not session:
        return
    if not session["is_active"]:
        _destroy_session(sid)
        return
    if await _active_session(sid) is session:
        session["stopping"] = True
        await _queue_utterance(sid, session)
        tail = session["tail"]
        if tail:
            await asyncio.shield(tail)
    if _sessions.get(sid) is session:
        await _emit(sid, session, "scribe:status", {"status": "stopped"})
        _destroy_session(sid)


async def handle_scribe_disconnect(sid):
    """Clean up session on disconnect."""
    if sid in _sessions:
        _destroy_session(sid)
        logger.info("[ScribeStream] Session cleaned up on disconnect: %s", sid)
