"""Both speech paths use private synthetic audio files and clean failures."""
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import types

import pytest

from services import audio_conversion


def _load_asr_module(monkeypatch, relative_path, name):
    # Import scripts without starting a daemon or importing speech weights.
    monkeypatch.setitem(sys.modules, "zmq", types.ModuleType("zmq"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def daemon(monkeypatch):
    return _load_asr_module(monkeypatch, "services/whisper_daemon.py", "orb_whisper_private_files")


@pytest.fixture
def worker(monkeypatch):
    return _load_asr_module(monkeypatch, "scripts/transcribe_worker.py", "orb_worker_private_files")


@pytest.fixture(params=["daemon", "worker"])
def converter(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def created(monkeypatch, tmp_path):
    files = []
    create = audio_conversion.tempfile.mkstemp

    def capture(**kwargs):
        fd, path = create(dir=tmp_path, **kwargs)
        files.append((fd, path, stat.S_IMODE(os.fstat(fd).st_mode)))
        return fd, path

    monkeypatch.setattr(audio_conversion.tempfile, "mkstemp", capture)
    return files


def test_conversion_is_private_before_ffmpeg_writes_and_descriptor_is_closed(
    converter, created, monkeypatch,
):
    def convert(command, **kwargs):
        assert kwargs == {"capture_output": True, "timeout": 30, "check": True}
        fd, path, mode = created[0]
        assert command[-1] == path and command[:3] == ["ffmpeg", "-y", "-i"]
        assert mode == 0o600
        with pytest.raises(OSError):
            os.fstat(fd)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        Path(path).write_bytes(b"synthetic audio" * 32)

    monkeypatch.setattr(audio_conversion.subprocess, "run", convert)
    previous_umask = os.umask(0)
    try:
        result = converter.convert_to_wav("synthetic.webm")
    finally:
        os.umask(previous_umask)
    assert result == created[0][1]
    assert stat.S_IMODE(os.stat(result).st_mode) == 0o600
    os.remove(result)


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "write", "short"])
def test_failed_conversion_removes_partial_audio_and_falls_back_to_input(
    converter, created, monkeypatch, tmp_path, failure,
):
    def convert(command, **kwargs):
        path = Path(command[-1])
        path.write_bytes(b"partial synthetic audio" * (1 if failure == "short" else 32))
        if failure == "nonzero":
            assert kwargs["check"] is True
            raise subprocess.CalledProcessError(1, command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        if failure == "write":
            raise OSError("injected converter write failure")

    monkeypatch.setattr(audio_conversion.subprocess, "run", convert)
    assert converter.convert_to_wav("synthetic.webm") == "synthetic.webm"
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(OSError):
        os.fstat(created[0][0])


def test_temporary_file_allocation_failure_preserves_the_input(converter, monkeypatch):
    def unavailable(**kwargs):
        raise OSError("temporary storage unavailable")

    monkeypatch.setattr(audio_conversion.tempfile, "mkstemp", unavailable)
    assert converter.convert_to_wav("synthetic.webm") == "synthetic.webm"


def test_interrupted_conversion_removes_partial_audio(converter, created, monkeypatch, tmp_path):
    def interrupt(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial synthetic audio" * 32)
        raise KeyboardInterrupt

    monkeypatch.setattr(audio_conversion.subprocess, "run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        converter.convert_to_wav("synthetic.webm")
    assert list(tmp_path.iterdir()) == []


def test_wav_input_bypasses_conversion_and_allocation(daemon, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("a WAV needs no temporary copy or ffmpeg")

    monkeypatch.setattr(audio_conversion.tempfile, "mkstemp", unexpected)
    monkeypatch.setattr(audio_conversion.subprocess, "run", unexpected)
    assert daemon.convert_to_wav("synthetic.wav") == "synthetic.wav"


def test_worker_preserves_its_wav_reencoding_behavior(worker, created, monkeypatch):
    def convert(command, **kwargs):
        assert command[3] == "synthetic.wav"
        Path(command[-1]).write_bytes(b"synthetic audio" * 32)

    monkeypatch.setattr(audio_conversion.subprocess, "run", convert)
    result = worker.convert_to_wav("synthetic.wav")
    assert result == created[0][1]
    os.remove(result)


@pytest.mark.parametrize("outcome", ["import", "transcribe", "result", "interrupt", "success"])
def test_worker_always_removes_converted_audio_without_deleting_input(
    worker, created, monkeypatch, tmp_path, capsys, outcome,
):
    original = tmp_path / "input.webm"
    original.write_bytes(b"original synthetic audio")
    monkeypatch.setattr(worker, "require_installed", lambda *_args: tmp_path / "fake-model")
    monkeypatch.setattr(sys, "argv", ["transcribe_worker.py", str(original), "large-v3",
                                      "--stdin-hints", "12", "", "auto"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hints":"synthetic hint"}'))

    def convert(command, **kwargs):
        Path(command[-1]).write_bytes(b"synthetic audio" * 32)

    def transcribe(path, **kwargs):
        assert path == created[0][1] and Path(path).exists()
        assert kwargs["path_or_hf_repo"] == str(tmp_path / "fake-model")
        assert kwargs["language"] is None
        assert "synthetic hint" in kwargs["initial_prompt"]
        if outcome == "transcribe":
            raise RuntimeError("injected transcription failure")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        return {} if outcome == "result" else {"text": "synthetic transcript"}

    package = types.ModuleType("lightning_whisper_mlx")
    package.__path__ = []
    module = types.ModuleType("lightning_whisper_mlx.transcribe")
    module.transcribe_audio = transcribe
    monkeypatch.setitem(sys.modules, "lightning_whisper_mlx", package)
    monkeypatch.setitem(sys.modules, "lightning_whisper_mlx.transcribe",
                        None if outcome == "import" else module)
    monkeypatch.setattr(audio_conversion.subprocess, "run", convert)

    exception = {"import": ImportError, "transcribe": RuntimeError,
                 "result": KeyError, "interrupt": KeyboardInterrupt}.get(outcome)
    if exception:
        with pytest.raises(exception):
            worker.main()
    else:
        worker.main()
        assert "###RESULT###{\"text\": \"synthetic transcript\"}###RESULT###" in capsys.readouterr().out
    assert original.read_bytes() == b"original synthetic audio"
    assert len(created) == 1 and not Path(created[0][1]).exists()
