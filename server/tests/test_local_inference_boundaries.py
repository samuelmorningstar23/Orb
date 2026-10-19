"""Offline origin and private speech-worker framing contracts (no models/network)."""
import io
import json
from types import SimpleNamespace

import pytest

from offline_policy import local_model_origin
from services.asr_worker_protocol import MAX_HINT_CHARS, MAX_HINT_WIRE_CHARS, encode_hints, read_hints


@pytest.mark.parametrize("origin, expected", [
    (" http://localhost:11434/ ", "http://localhost:11434"),
    ("https://LOCALHOST", "https://localhost"),
    ("http://127.0.0.1:1", "http://127.0.0.1:1"),
    ("http://127.24.2.3", "http://127.24.2.3"),
    ("http://[::1]:11434/", "http://[::1]:11434"),
    ("https://[0:0:0:0:0:0:0:1]", "https://[::1]"),
])
def test_local_origins_are_normalized_without_dns(origin, expected, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: pytest.fail("validation performed DNS"))
    assert local_model_origin(origin) == expected


@pytest.mark.parametrize("origin", [
    "https://example.org", "http://192.168.1.20:11434", "http://0.0.0.0:11434",
    "http://[::]", "http://[::ffff:192.168.1.3]", "ftp://127.0.0.1", "localhost:11434",
    "http://localhost/private", "http://localhost?token=secret", "http://localhost#fragment",
    "http://alice:secret@localhost", "http://alice@localhost", "http://localhost:65536",
    "http://localhost:bad", "http://[broken", "http://localhost.evil.test", "http://", "", None,
])
def test_nonlocal_or_ambiguous_origins_fail_without_disclosing_credentials(origin):
    with pytest.raises(ValueError, match="^Orb inference requires an HTTP\\(S\\) loopback origin$"):
        local_model_origin(origin)


@pytest.mark.parametrize("hints", ["", "Synthetic patient name", "Hindi: नमस्ते\n'quote'", "x" * MAX_HINT_CHARS, "😀" * MAX_HINT_CHARS])
def test_hints_roundtrip_only_through_bounded_stdin(hints):
    assert read_hints(io.StringIO(encode_hints(hints))) == hints


@pytest.mark.parametrize("hints", [None, 12, [], "x" * (MAX_HINT_CHARS + 1)])
def test_invalid_hint_values_are_refused(hints):
    with pytest.raises(ValueError, match="^Invalid speech hint payload$"):
        encode_hints(hints)


@pytest.mark.parametrize("wire", ["", "{", "[]", "{}", '{"hints":null}', '{"hints":7}',
    '{"hints":"x","extra":"private"}', '"private"', "x" * (MAX_HINT_WIRE_CHARS + 1)])
def test_invalid_hint_envelopes_fail_generically(wire):
    with pytest.raises(ValueError, match="^Invalid speech hint payload$"):
        read_hints(io.StringIO(wire))


def test_hint_reader_does_not_read_an_unbounded_pipe():
    class Pipe:
        def read(self, count):
            assert count == MAX_HINT_WIRE_CHARS + 1
            return '{"hints":""}'
    assert read_hints(Pipe()) == ""


def test_speech_worker_argv_and_failure_logs_never_contain_hints(monkeypatch, caplog):
    from services import scribe_service
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    private = "SYNTHETIC-MRN-PRIVATE-NAME"
    def run(argv, **kwargs):
        assert private not in str(argv)
        assert argv[4] == "--stdin-hints"
        assert json.loads(kwargs["input"]) == {"hints": private}
        return SimpleNamespace(returncode=2, stderr=private, stdout="")
    monkeypatch.setattr(scribe_service.subprocess, "run", run)
    with pytest.raises(scribe_service.TranscriptionError):
        scribe_service._transcribe_via_subprocess("synthetic.wav", private, {"model": "local", "batch_size": 1})
    assert private not in caplog.text


def test_gateway_refuses_runtime_remote_origin_before_http(monkeypatch):
    from services import ollama_gateway
    monkeypatch.setattr("config.OLLAMA_BASE_URL", "https://outside.example")
    with pytest.raises(ValueError, match="loopback"):
        ollama_gateway._url("/api/chat")


def test_gateway_configuration_rejects_remote_origin_at_startup(monkeypatch):
    import os
    from pathlib import Path
    import subprocess
    import sys
    env = {**os.environ, "ORB_OLLAMA_BASE_URL": "https://outside.example"}
    result = subprocess.run([sys.executable, '-c', 'import config'],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'Orb inference requires an HTTP(S) loopback origin' in result.stderr
    assert 'outside.example' not in result.stderr


def test_speech_worker_refuses_legacy_hint_arguments_before_model_lookup(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    import sys
    path = Path(__file__).resolve().parents[1] / 'scripts/transcribe_worker.py'
    spec = importlib.util.spec_from_file_location('_orb_stdin_worker', path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    private = 'SYNTHETIC-PRIVATE-NAME'
    monkeypatch.setattr(sys, 'argv', ['worker.py', 'synthetic.wav', 'local', private])
    monkeypatch.setattr(worker, 'require_installed', lambda *_: pytest.fail('legacy hints reached the model'))
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert 'Invalid speech hint transport' in captured.err
    assert private not in captured.err + captured.out
