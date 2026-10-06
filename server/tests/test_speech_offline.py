"""Speech stays on the appliance (owner ruling, 4 October 2026: Orb is strictly
offline).

  1. NO CLOUD SPEECH SWITCH. The AI posture names the local model and whether
     local transcription can run; there is no cloud-speech flag to turn on.
  2. THE PROBE LOOKS WHERE THE WEIGHTS LIVE. local_asr_available reads
     server/mlx_models/<model>/, the folder the daemon and the worker load from.
     It used to look for a "whisper" folder under server/models, where nothing
     loads from.
  3. NOTHING DOWNLOADS A MODEL. The subprocess worker and the warm daemon used
     to construct LightningWhisperMLX, whose constructor fetches the weights
     from Hugging Face. Without local weights they now refuse with "speech
     model not installed" before the library is even imported, and with them
     they hand the local folder straight to transcribe_audio.
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

import config
from services import asr_model_files
from services.ai_governance import ai_posture, local_asr_available

SERVER_DIR = Path(__file__).resolve().parent.parent


def _install(models_dir: Path, name: str = "large-v3", files=asr_model_files.REQUIRED_FILES) -> Path:
    folder = models_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    for f in files:
        (folder / f).write_bytes(b"x")
    return folder


def _load_isolated(monkeypatch, rel_path: str, name: str):
    """Execute a script module under a private name, without registering it in
    sys.modules, and with sys.path and the Hugging Face switches restored after
    the test (both modules change them at import)."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    spec = importlib.util.spec_from_file_location(name, SERVER_DIR / rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def models_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_model_files, "MLX_MODELS_DIR", tmp_path)
    return tmp_path


# ── 1. posture ───────────────────────────────────────────────────────────────

def test_posture_has_no_cloud_speech_switch():
    posture = ai_posture()
    assert set(posture) == {"local_model", "local_asr_available"}
    assert not hasattr(config, "CLOUD_SPEECH_ALLOWED")


# ── 2. the probe ─────────────────────────────────────────────────────────────

def test_local_asr_available_reads_the_folder_the_worker_loads_from(models_dir, monkeypatch):
    monkeypatch.setitem(config.VOICE["daemon"], "enabled", True)
    monkeypatch.setitem(config.VOICE["daemon"], "model", "large-v3")
    monkeypatch.setitem(config.SCRIBE["asr"], "model", "large-v3")
    monkeypatch.setitem(config.SCRIBE["asr"], "quant", None)

    assert local_asr_available() is False
    _install(models_dir, files=("config.json",))
    assert local_asr_available() is False, "half a model is not a model"
    _install(models_dir)
    assert local_asr_available() is True


def test_local_asr_available_stays_off_when_the_operator_disabled_it(models_dir, monkeypatch):
    _install(models_dir)
    monkeypatch.setitem(config.VOICE["daemon"], "enabled", False)
    assert local_asr_available() is False


def test_quantised_distil_models_use_the_library_folder_name():
    assert asr_model_files.model_dir_name("distil-large-v3", "4bit") == "distil-large-v3-4-bit"
    assert asr_model_files.model_dir_name("distil-large-v3", "8bit") == "distil-large-v3-8-bit"
    assert asr_model_files.model_dir_name("large-v3", "4bit") == "large-v3"


# ── 3. no runtime download ───────────────────────────────────────────────────

def test_worker_refuses_without_local_weights(models_dir, monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, "lightning_whisper_mlx", raising=False)
    worker = _load_isolated(monkeypatch, "scripts/transcribe_worker.py", "_orb_transcribe_worker_test")
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"

    def no_conversion(_path):
        raise AssertionError("audio converted before the model was found")
    monkeypatch.setattr(worker, "convert_to_wav", no_conversion)
    monkeypatch.setattr(sys, "argv", ["transcribe_worker.py", "/tmp/none.webm", "large-v3"])

    with pytest.raises(SystemExit) as exit_info:
        worker.main()
    assert exit_info.value.code == 2
    assert "speech model not installed" in capsys.readouterr().err
    assert "lightning_whisper_mlx" not in sys.modules


def test_worker_hands_transcribe_audio_the_local_folder(models_dir, monkeypatch, capsys):
    folder = _install(models_dir)
    seen = {}

    class NoDownloads:
        def __init__(self, *a, **k):
            raise AssertionError("LightningWhisperMLX downloads weights; it must not be built")

    def fake_transcribe_audio(path, path_or_hf_repo, **kwargs):
        seen["path"] = path_or_hf_repo
        return {"text": "bp 120 over 80"}

    package = types.ModuleType("lightning_whisper_mlx")
    package.LightningWhisperMLX = NoDownloads
    transcribe = types.ModuleType("lightning_whisper_mlx.transcribe")
    transcribe.transcribe_audio = fake_transcribe_audio
    monkeypatch.setitem(sys.modules, "lightning_whisper_mlx", package)
    monkeypatch.setitem(sys.modules, "lightning_whisper_mlx.transcribe", transcribe)

    worker = _load_isolated(monkeypatch, "scripts/transcribe_worker.py", "_orb_transcribe_worker_test")
    monkeypatch.setattr(worker, "convert_to_wav", lambda p: p)
    monkeypatch.setattr(sys, "argv", ["transcribe_worker.py", "/tmp/a.wav", "large-v3"])

    worker.main()
    assert seen["path"] == str(folder)
    assert "bp 120 over 80" in capsys.readouterr().out


def test_daemon_refuses_without_local_weights(models_dir, monkeypatch):
    # The daemon imports zmq at module level; the suite's venv need not have it.
    monkeypatch.setitem(sys.modules, "zmq", types.ModuleType("zmq"))
    monkeypatch.delitem(sys.modules, "lightning_whisper_mlx", raising=False)
    daemon = _load_isolated(monkeypatch, "services/whisper_daemon.py", "_orb_whisper_daemon_test")
    assert os.environ["HF_HUB_OFFLINE"] == "1"

    with pytest.raises(asr_model_files.SpeechModelNotInstalled, match="speech model not installed"):
        daemon.load_model("large-v3")
    assert "lightning_whisper_mlx" not in sys.modules
    assert daemon._whisper_model is None
