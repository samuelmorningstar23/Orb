"""Orb Whisper Daemon — Persistent Warm ASR Worker

Keeps Whisper loaded in Apple Silicon Unified Memory permanently.
Accepts audio via ZeroMQ IPC socket, returns transcription instantly.

Usage:
    python whisper_daemon.py                    # Start daemon (foreground)
    python whisper_daemon.py --test audio.wav   # Quick test transcription

Protocol (ZMQ REQ/REP):
    Request:  JSON {"audio_path": "/path/to/file.wav", "hints": "Patient: ...", "language": "en"}
    Response: JSON {"text": "transcription...", "duration_ms": 234, "status": "ok"}
    Error:    JSON {"text": "", "error": "message", "status": "error"}
"""
import os
import sys
import json
import time
import signal
from pathlib import Path

# Add parent dirs to path
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from services.asr_model_files import (
    MLX_MODELS_DIR, SpeechModelNotInstalled, force_hub_offline, require_installed,
)
from services.audio_conversion import convert_to_wav as _convert_to_wav

# Orb is offline: no Hugging Face lookup from this process, ever. Set before
# anything imports huggingface_hub, which reads it once at import time.
force_hub_offline()

import zmq

# ── Configuration ──
DEFAULT_SOCKET = os.getenv("ORB_WHISPER_SOCKET", "ipc:///tmp/orb-whisper.sock")
DEFAULT_MODEL = os.getenv("ORB_WHISPER_STREAMING_MODEL", "large-v3")

# Global model reference (kept warm)
_whisper_model = None
_model_name = None


def convert_to_wav(input_path: str) -> str:
    """Convert audio to 16kHz mono WAV using ffmpeg for best Whisper accuracy."""
    return _convert_to_wav(input_path, skip_wav=True)


def load_model(model_name: str):
    """Load Whisper model into memory (one-time cost on startup).

    The weights must already be on the appliance: a missing model raises
    SpeechModelNotInstalled and the daemon does not start. It used to build a
    LightningWhisperMLX here, whose constructor downloads the weights from
    Hugging Face; Orb is offline, so it is not called at all."""
    global _whisper_model, _model_name
    
    if _whisper_model is not None and _model_name == model_name:
        return _whisper_model
    
    model_path = require_installed(model_name)
    print(f"[WhisperDaemon] Loading model '{model_name}' into Unified Memory...")
    t0 = time.time()
    
    # Imported here so a missing runtime fails at start, not on the first request.
    import lightning_whisper_mlx.transcribe  # noqa: F401
    _whisper_model = model_path
    _model_name = model_name
    
    elapsed = time.time() - t0
    print(f"[WhisperDaemon] Model loaded in {elapsed:.1f}s — warm and ready")
    return _whisper_model


def transcribe(audio_path: str, hints: str = "", language: str = "en") -> dict:
    """Run transcription on a warm model. Returns result dict."""
    global _whisper_model, _model_name
    
    if _whisper_model is None or _model_name is None:
        return {"text": "", "error": "Model not loaded", "status": "error"}
    
    t0 = time.time()
    
    # Convert if needed
    converted_path = convert_to_wav(audio_path)
    
    # Build clinical prompt bias
    clinical_prompt = (
        "Medical dictation: Patient admitted to ward, bed, IV. "
        "Fosfomycin, Meropenem, Vancomycin, Cefepime, Piperacillin/Tazobactam. "
    )
    if hints:
        clinical_prompt += f"Specific context: {hints}"
    
    try:
        from lightning_whisper_mlx.transcribe import transcribe_audio
        
        model_path = str(MLX_MODELS_DIR / _model_name)
        # 'auto' → let Whisper detect the language (pass None) for code-switching,
        # matching the subprocess worker.
        whisper_language = None if language == "auto" else language
        result = transcribe_audio(
            converted_path,
            path_or_hf_repo=model_path,
            language=whisper_language,
            batch_size=12,
            initial_prompt=clinical_prompt,
        )
        
        elapsed_ms = int((time.time() - t0) * 1000)
        text = result.get("text", "").strip()
        
        return {
            "text": text,
            "duration_ms": elapsed_ms,
            "status": "ok",
        }
    except Exception as e:
        return {"text": "", "error": str(e), "status": "error"}
    finally:
        # Clean up converted file
        if converted_path != audio_path:
            try:
                os.remove(converted_path)
            except OSError:
                pass


def run_daemon(socket_addr: str, model_name: str):
    """Main daemon loop — binds ZMQ socket and processes requests."""
    # Clean up stale socket file
    if socket_addr.startswith("ipc://"):
        sock_path = socket_addr.replace("ipc://", "")
        if os.path.exists(sock_path):
            os.remove(sock_path)
    
    # Load model (one-time warmup)
    load_model(model_name)
    
    # Bind ZMQ socket
    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind(socket_addr)
    
    print(f"[WhisperDaemon] Listening on {socket_addr}")
    print(f"[WhisperDaemon] Model: {model_name} | PID: {os.getpid()}")
    print(f"[WhisperDaemon] Ready for transcription requests\n")
    
    # Graceful shutdown
    def shutdown(signum, frame):
        print(f"\n[WhisperDaemon] Received signal {signum}, shutting down...")
        socket.close()
        context.term()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    
    request_count = 0
    
    while True:
        try:
            # Wait for request
            raw_msg = socket.recv_string()
            request_count += 1
            
            try:
                req = json.loads(raw_msg)
            except json.JSONDecodeError:
                socket.send_string(json.dumps({
                    "text": "", "error": "Invalid JSON request", "status": "error"
                }))
                continue
            
            audio_path = req.get("audio_path", "")
            hints = req.get("hints", "")
            language = req.get("language", "en")
            
            if not audio_path or not os.path.exists(audio_path):
                socket.send_string(json.dumps({
                    "text": "", "error": f"Audio file not found: {audio_path}", "status": "error"
                }))
                continue
            
            print(f"[WhisperDaemon] Request #{request_count}: {os.path.basename(audio_path)}")
            
            # Transcribe on warm model
            result = transcribe(audio_path, hints=hints, language=language)
            
            print(f"[WhisperDaemon]   → {result.get('duration_ms', '?')}ms | "
                  f"{len(result.get('text', ''))} chars | {result['status']}")
            
            socket.send_string(json.dumps(result))
            
        except zmq.ZMQError as e:
            if e.errno == zmq.ETERM:
                break
            print(f"[WhisperDaemon] ZMQ error: {e}")
        except Exception as e:
            print(f"[WhisperDaemon] Unexpected error: {e}")
            try:
                socket.send_string(json.dumps({
                    "text": "", "error": str(e), "status": "error"
                }))
            except Exception:
                pass


def test_transcribe(audio_path: str):
    """Quick standalone test (no daemon, just load and transcribe)."""
    load_model(DEFAULT_MODEL)
    result = transcribe(audio_path)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--test":
            if len(sys.argv) < 3:
                print("Usage: python whisper_daemon.py --test <audio_file>")
                sys.exit(1)
            test_transcribe(sys.argv[2])
        else:
            model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
            socket = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SOCKET
            run_daemon(socket, model)
    except SpeechModelNotInstalled as e:
        print(f"[WhisperDaemon] {e}", file=sys.stderr)
        sys.exit(2)
