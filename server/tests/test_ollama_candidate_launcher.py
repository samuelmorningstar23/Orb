"""Candidate safety tests use only synthetic metadata and mocked subprocesses."""
from __future__ import annotations

import errno
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "serve_ollama_candidate.py"
SPEC = importlib.util.spec_from_file_location("orb_ollama_candidate_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
candidate: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = candidate
SPEC.loader.exec_module(candidate)


def test_environment_isolated_without_changing_home(tmp_path: Path) -> None:
    inherited = {"HOME": "/original/home", "PATH": "/usr/bin", "HTTPS_PROXY": "private",
                 "LLAMA_ARG_N_PREDICT": "1", "LLAMA_ARG_CTX_SIZE": "32",
                 "OLLAMA_DEBUG_LOG_REQUESTS": "1", "OLLAMA_FLASH_ATTENTION": "auto",
                 "GGML_RPC_SERVERS": "outside", "XDG_CONFIG_HOME": "/original/config"}
    env = candidate.controlled_environment(inherited, models_dir=tmp_path / "models",
        run_dir=tmp_path / "run", port=11438, parallel=2, swa_full=True,
        flash_attention="auto")
    assert inherited["XDG_CONFIG_HOME"] == "/original/config"
    assert env["HOME"] == "/original/home"
    assert "HTTPS_PROXY" not in env and "GGML_RPC_SERVERS" not in env
    assert "LLAMA_ARG_N_PREDICT" not in env and "LLAMA_ARG_CTX_SIZE" not in env
    assert "OLLAMA_FLASH_ATTENTION" not in env
    assert env["OLLAMA_DEBUG_LOG_REQUESTS"] == "0"
    assert env["OLLAMA_HOST"] == "127.0.0.1:11438"
    assert env["OLLAMA_NUM_PARALLEL"] == "2"
    assert env["OLLAMA_CONTEXT_LENGTH"] == "8192"
    assert env["OLLAMA_KV_CACHE_TYPE"] == "f16"
    assert env["LLAMA_ARG_SWA_FULL"] == "1"
    assert env["LLAMA_ARG_CONT_BATCHING"] == "true"
    assert "LLAMA_ARG_CONTEXT_SHIFT" not in env
    assert env["OLLAMA_NO_CLOUD"] == env["OLLAMA_NOPRUNE"] == "1"
    assert env["LLAMA_ARG_CACHE_RAM"] == "8192"


@pytest.mark.parametrize("prefix_cache_mib", [0, 8192])
def test_cross_request_storage_policy_preserves_active_context_and_f16_kv(tmp_path, prefix_cache_mib):
    env = candidate.controlled_environment({}, models_dir=tmp_path, run_dir=tmp_path / "run",
        port=11438, parallel=2, swa_full=False, flash_attention="auto", prefix_cache_mib=prefix_cache_mib)
    assert env["LLAMA_ARG_CACHE_RAM"] == str(prefix_cache_mib)
    expected = "true" if prefix_cache_mib else "false"
    assert env["LLAMA_ARG_CACHE_IDLE_SLOTS"] == env["LLAMA_ARG_CACHE_PROMPT"] == expected
    assert env["OLLAMA_CONTEXT_LENGTH"] == "8192" and env["OLLAMA_KV_CACHE_TYPE"] == "f16"
    assert env["LLAMA_ARG_SWA_FULL"] == "0" and env["OLLAMA_NUM_PARALLEL"] == "2"
    assert env["LLAMA_ARG_CONT_BATCHING"] == "true"


@pytest.mark.parametrize("invalid", [-1, 1, 4096, True, "0"])
def test_native_prefix_storage_refuses_unqualified_values(tmp_path, invalid):
    with pytest.raises(candidate.CandidateRefused):
        candidate.controlled_environment({}, models_dir=tmp_path, run_dir=tmp_path / "run",
            port=11438, parallel=2, swa_full=False, flash_attention="auto", prefix_cache_mib=invalid)


def test_executable_version_is_not_confused_with_daemon_version() -> None:
    assert candidate.executable_version("ollama version is 0.40.3\nWarning: client version is 0.40.4\n") == "0.40.4"
    assert candidate.executable_version("ollama version is 0.40.4\nWarning: client version is 0.40.5\n") == "0.40.5"
    with pytest.raises(candidate.CandidateRefused):
        candidate.executable_version("unidentified version")


@pytest.mark.parametrize(("setting", "expected"), [("on", "1"), ("off", "0")])
def test_flash_attention_uses_boolean_ollama_setting(tmp_path: Path, setting: str, expected: str) -> None:
    env = candidate.controlled_environment({}, models_dir=tmp_path, run_dir=tmp_path / "run",
        port=11438, parallel=1, swa_full=False, flash_attention=setting)
    assert env["OLLAMA_FLASH_ATTENTION"] == expected
    assert env["LLAMA_ARG_SWA_FULL"] == "0"


def test_memory_snapshot_and_multislot_headroom_guard() -> None:
    memory = candidate.parse_memory_snapshot(
        "The system has 68719476736 (4194304 pages with a page size of 16384).\n"
        "System-wide memory free percentage: 75%\n",
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 1.")
    model = candidate.ModelSnapshot("medgemma:27b", candidate.MODEL_DIGEST,
                                    17397211520, "synthetic", "Q4_K_M")
    assert memory.estimated_available_bytes == 48 * candidate.GIB
    assert candidate.required_available_bytes(model, 2) < memory.estimated_available_bytes
    assert candidate.required_available_bytes(model, 4) > memory.estimated_available_bytes
    with pytest.raises(candidate.CandidateRefused):
        candidate.required_available_bytes(model, 200)


@pytest.mark.parametrize("pressure", ["", "The system has 1 \nSystem-wide memory free percentage: 75%",
                                      "The system has 68719476736 \nSystem-wide memory free percentage: 101%"])
def test_uninspectable_memory_fails_closed(pressure: str) -> None:
    with pytest.raises(candidate.CandidateRefused):
        candidate.parse_memory_snapshot(pressure, "page size 16384")


@pytest.mark.parametrize("port", [0, 80, 11434, 65536])
def test_does_not_bind_shared_or_invalid_ports(port: int) -> None:
    with pytest.raises(candidate.CandidateRefused):
        candidate.check_port(port)


def test_occupied_port_refused_without_contacting_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    class OccupiedSocket:
        def __enter__(self) -> "OccupiedSocket":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 11438)
            raise OSError(errno.EADDRINUSE, "synthetic occupied port")

    monkeypatch.setattr(candidate.socket, "socket", OccupiedSocket)
    with pytest.raises(candidate.CandidateRefused, match="occupied"):
        candidate.check_port(11438)


@pytest.fixture
def model_cache(tmp_path: Path) -> Path:
    models = tmp_path / "models"
    (models / "blobs").mkdir(parents=True)
    manifest = models / "manifests" / "registry.ollama.ai" / "library" / "medgemma" / "27b"
    manifest.parent.mkdir(parents=True)
    config_digest = "sha256:" + "c" * 64
    (models / "blobs" / config_digest.replace(":", "-")).write_text(json.dumps({
        "model_family": "gemma3", "model_type": "27.4B", "file_type": "Q4_K_M"}))
    (models / "blobs" / candidate.MODEL_DIGEST.replace(":", "-")).write_bytes(b"unchanged synthetic weight")
    manifest.write_text(json.dumps({"config": {"digest": config_digest}, "layers": [{
        "mediaType": "application/vnd.ollama.image.model", "digest": candidate.MODEL_DIGEST,
        "size": 26}]}))
    return models


def test_model_metadata_pins_same_weights_and_precision(model_cache: Path) -> None:
    before = {path: path.stat().st_mtime_ns for path in model_cache.rglob("*") if path.is_file()}
    model = candidate.inspect_model(model_cache)
    assert model.quantization == "Q4_K_M" and model.weight_bytes == 26
    assert model.model_digest == candidate.MODEL_DIGEST
    assert before == {path: path.stat().st_mtime_ns for path in model_cache.rglob("*") if path.is_file()}


def test_model_cache_rejects_silent_replacement(model_cache: Path) -> None:
    manifest = model_cache / "manifests" / "registry.ollama.ai" / "library" / "medgemma" / "27b"
    data = json.loads(manifest.read_text())
    data["layers"][0]["digest"] = "sha256:" + "d" * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(candidate.CandidateRefused, match="weight digest"):
        candidate.inspect_model(model_cache)


def test_model_cache_rejects_wrong_precision(model_cache: Path) -> None:
    config = model_cache / "blobs" / ("sha256-" + "c" * 64)
    config.write_text(json.dumps({"model_family": "gemma3", "model_type": "27.4B", "file_type": "Q3_K_M"}))
    with pytest.raises(candidate.CandidateRefused, match="metadata"):
        candidate.inspect_model(model_cache)


def test_missing_blob_refused(model_cache: Path) -> None:
    (model_cache / "blobs" / candidate.MODEL_DIGEST.replace(":", "-")).unlink()
    with pytest.raises(candidate.CandidateRefused, match="absent"):
        candidate.inspect_model(model_cache)


@pytest.fixture
def launch_environment(tmp_path: Path, model_cache: Path,
                       monkeypatch: pytest.MonkeyPatch) -> list[str]:
    binary = tmp_path / "ollama"
    runner = tmp_path / "llama-server"
    binary.touch()
    runner.touch()
    monkeypatch.setattr(candidate.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(candidate.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(candidate, "check_port", lambda _port: None)
    monkeypatch.setattr(candidate, "inspect_memory", lambda: candidate.MemorySnapshot(
        64 * candidate.GIB, 75, 48 * candidate.GIB, "synthetic memory snapshot"))

    def output(command: list[str], **_kwargs: object) -> str:
        assert command[1:] == ["--version"]
        if Path(command[0]).name == "ollama":
            return "Warning: could not connect to a running Ollama instance\nWarning: client version is 0.40.4\n"
        return "version: 0.6.0-dev (build 1, commit b42b7e6d3)\n"

    monkeypatch.setattr(candidate, "command_output", output)
    return ["--ollama", str(binary), "--models-dir", str(model_cache), "--run-dir",
            str(tmp_path / "new-run"), "--port", "11438", "--parallel", "2"]


def test_inspection_does_not_create_configuration(launch_environment: list[str],
                                                capsys: pytest.CaptureFixture[str]) -> None:
    assert candidate.main(launch_environment) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "candidate_not_qualified"
    assert plan["context_per_request"] == 8192 and plan["parallel"] == 2
    run_dir = Path(launch_environment[launch_environment.index("--run-dir") + 1])
    assert not run_dir.exists()


def test_execute_owns_only_new_config_and_passes_fixed_arguments(
    launch_environment: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def execute(path: Path, argv: list[str], env: dict[str, str]) -> None:
        captured.update({"path": path, "argv": argv, "env": env})

    monkeypatch.setattr(candidate.os, "execve", execute)
    assert candidate.main([*launch_environment, "--execute"]) == 0
    assert captured["argv"] == [str(captured["path"]), "serve"]
    assert captured["env"]["OLLAMA_NUM_PARALLEL"] == "2"
    run_dir = Path(launch_environment[launch_environment.index("--run-dir") + 1])
    assert (run_dir / "xdg" / "llama.cpp" / "config.ini").read_text() == candidate.RUNNER_CONFIG
    assert json.loads((run_dir / "candidate.json").read_text())["memory_guard_passed"] is True


def test_changed_runtime_or_low_headroom_never_launches(
    launch_environment: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(candidate, "inspect_memory", lambda: candidate.MemorySnapshot(
        64 * candidate.GIB, 1, candidate.GIB, "synthetic low memory"))
    assert candidate.main([*launch_environment, "--execute"]) == 2
    run_dir = Path(launch_environment[launch_environment.index("--run-dir") + 1])
    assert not run_dir.exists()
    monkeypatch.setattr(candidate, "command_output", lambda *_args, **_kwargs: "ollama version is 0.40.5\n")
    assert candidate.main([*launch_environment, "--execute"]) == 2
    assert not run_dir.exists()
