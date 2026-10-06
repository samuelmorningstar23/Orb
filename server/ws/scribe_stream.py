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
import logging
import os
import wave
import struct
import asyncio
import time
import tempfile
from typing import Optional

from ws.realtime import sio

logger = logging.getLogger("orb.ws.scribe")

# Per-session state
_sessions: dict[str, dict] = {}


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


def _get_session(sid: str) -> Optional[dict]:
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
        "created_at": time.time(),
    }
    _sessions[sid] = session
    return session


def _destroy_session(sid: str):
    """Cleanup a session."""
    _sessions.pop(sid, None)


def _write_wav(pcm_data: bytes, sample_rate: int = 16000) -> str:
    """Write raw PCM16 data to a WAV file and return the path."""
    wav_path = tempfile.mktemp(suffix=".wav", prefix="orb_stream_")
    with wave.open(wav_path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)  # 16-bit
        f.setframerate(sample_rate)
        f.writeframes(pcm_data)
    return wav_path


async def _transcribe_utterance(sid: str, session: dict):
    """Transcribe accumulated audio buffer and emit result."""
    audio_data = bytes(session["audio_buffer"])
    session["audio_buffer"] = bytearray()
    utterance_idx = session["utterance_index"]
    session["utterance_index"] += 1

    if len(audio_data) < 3200:  # Less than 0.1s of audio at 16kHz
        return

    # Emit processing status
    await sio.emit("scribe:status", {"status": "processing"}, to=sid)

    loop = asyncio.get_event_loop()
    wav_path = None

    try:
        # Write WAV file (blocking I/O — run in executor)
        wav_path = await loop.run_in_executor(None, _write_wav, audio_data)

        # Import transcription functions
        from services.scribe_service import (
            _transcribe_via_daemon,
            _transcribe_via_subprocess,
            _apply_rxnorm_correction,
            _resolve_language,
        )
        from config import SCRIBE

        # Resolve the session's transcription language (falls back to the
        # configured default) so the live ambient path honours the selection
        # instead of always defaulting to English.
        lang = _resolve_language(session.get("language"))

        # Try daemon first, fallback to subprocess
        t0 = time.time()

        result = await loop.run_in_executor(
            None, _transcribe_via_daemon, wav_path, session.get("hints", ""), lang
        )

        if result is None:
            # Fallback to subprocess
            asr_cfg = SCRIBE["asr"]
            result = await loop.run_in_executor(
                None, _transcribe_via_subprocess, wav_path, session.get("hints", ""), asr_cfg, lang
            )

        if result:
            # Apply RxNorm correction
            result = await loop.run_in_executor(
                None, _apply_rxnorm_correction, result
            )

        duration_ms = int((time.time() - t0) * 1000)
        text = result.strip() if result else ""

        if text:
            await sio.emit("scribe:transcript", {
                "text": text,
                "duration_ms": duration_ms,
                "is_final": True,
                "utterance_index": utterance_idx,
            }, to=sid)

        # Ready for next utterance
        await sio.emit("scribe:status", {"status": "ready"}, to=sid)

    except Exception as e:
        logger.error("[ScribeStream] Transcription error for %s: %s", sid, e)
        await sio.emit("scribe:error", {"error": str(e)}, to=sid)
        await sio.emit("scribe:status", {"status": "ready"}, to=sid)

    finally:
        # Cleanup temp WAV
        if wav_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass


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
    patient_id = data.get("patient_id", "")
    hints = data.get("hints", "")
    language = data.get("language", "")

    # Build clinical hints from patient data if available
    if patient_id:
        try:
            from services.scribe_service import build_whisper_hints
            db_hints = build_whisper_hints(patient_id)
            if db_hints:
                hints = f"{hints}, {db_hints}" if hints else db_hints
        except Exception:
            pass

    session = _create_session(sid, patient_id=patient_id, hints=hints, language=language)
    logger.info("[ScribeStream] Session started: %s (patient=%s)", sid, patient_id or "none")

    await sio.emit("scribe:status", {"status": "ready"}, to=sid)


@sio.on("scribe:audio_chunk")
async def handle_audio_chunk(sid, data):
    """Receive a binary audio chunk from the client (PCM16, 16kHz mono)."""
    session = _get_session(sid)
    if not session or not session["is_active"]:
        return

    if isinstance(data, (bytes, bytearray)):
        session["audio_buffer"].extend(data)
    elif isinstance(data, dict) and "audio" in data:
        # Some clients send as {audio: ArrayBuffer}
        session["audio_buffer"].extend(data["audio"])


@sio.on("scribe:end_utterance")
async def handle_end_utterance(sid, data=None):
    """Client-side VAD detected end of speech — transcribe accumulated audio."""
    session = _get_session(sid)
    if not session or not session["is_active"]:
        return

    # Run transcription asynchronously (don't block the event loop)
    asyncio.create_task(_transcribe_utterance(sid, session))


@sio.on("scribe:stop_session")
async def handle_stop_session(sid, data=None):
    """Client ends the streaming session."""
    session = _get_session(sid)
    if session:
        # Process any remaining audio
        if len(session["audio_buffer"]) > 3200:
            await _transcribe_utterance(sid, session)
        session["is_active"] = False
        _destroy_session(sid)
        logger.info("[ScribeStream] Session stopped: %s", sid)


@sio.on("disconnect")
async def handle_scribe_disconnect(sid):
    """Clean up session on disconnect."""
    if sid in _sessions:
        _destroy_session(sid)
        logger.info("[ScribeStream] Session cleaned up on disconnect: %s", sid)
