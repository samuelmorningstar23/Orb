"""Exercise normal stack startup with an owned fake daemon/backend, no network."""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def stack(tmp_path):
    checkout = tmp_path / "stack fixture with spaces"
    commands = tmp_path / "commands"
    commands.mkdir()
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "server" / ".venv" / "bin").mkdir(parents=True)
    for path in ("run-orb.sh", "scripts/serve-ollama.sh"):
        shutil.copyfile(ROOT / path, checkout / path)
        (checkout / path).chmod(0o755)
    config = checkout / "config" / "ollama-xdg" / "llama.cpp" / "config.ini"
    config.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "config" / "ollama-xdg" / "llama.cpp" / "config.ini", config)
    launcher = checkout / "scripts" / "serve-ollama.sh"
    launcher.write_text(launcher.read_text().replace(
        "system_config='/etc/llama.cpp/config.ini'",
        "system_config=" + shlex.quote(str(tmp_path / "system" / "config.ini")),
    ))
    home = tmp_path / "home"
    home.mkdir()
    capture = tmp_path / "daemon.json"
    trace = tmp_path / "trace.jsonl"
    ready = tmp_path / "ready"
    backend = tmp_path / "backend.json"
    fake = f'''#!{sys.executable}
import json, os, pathlib, signal, sys, time
name = pathlib.Path(sys.argv[0]).name
with open(os.environ['STACK_TRACE'], 'a') as stream:
    stream.write(json.dumps({{'tool':name, 'args':sys.argv[1:], 'host':os.environ.get('OLLAMA_HOST')}}) + '\\n')
if name == 'ollama':
    if sys.argv[1:] == ['--version']:
        print('ollama version is ' + os.environ.get('FAKE_VERSION', '0.35.1'))
    elif sys.argv[1:] == ['serve']:
        pathlib.Path(os.environ['STACK_CAPTURE']).write_text(json.dumps({{
            'pid':os.getpid(), 'host':os.environ['OLLAMA_HOST'],
            'swa':os.environ.get('LLAMA_ARG_SWA_FULL'),
            'xdg':os.environ.get('XDG_CONFIG_HOME'), 'ram':os.environ.get('LLAMA_ARG_CACHE_RAM'),
            'unified':os.environ.get('LLAMA_ARG_KV_UNIFIED'),
            'cloud':os.environ['OLLAMA_NO_CLOUD'], 'parallel':os.environ['OLLAMA_NUM_PARALLEL'],
        }}))
        if os.environ.get('STACK_NEVER_READY') != '1':
            pathlib.Path(os.environ['STACK_READY']).touch()
        if os.environ.get('STACK_IGNORE_TERM') == '1':
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30)
    else:
        sys.exit('No model pulls, model calls, or shared process controls are allowed')
elif name == 'uname':
    print('Darwin' if sys.argv[1] == '-s' else 'arm64')
elif name == 'sysctl':
    print(64 * 2**30)
elif name == 'curl':
    assert sys.argv[-1] == os.environ['EXPECTED_BASE_URL'] + '/api/tags'
    sys.exit(0 if pathlib.Path(os.environ['STACK_READY']).exists() else 1)
elif name == 'lsof':
    pid = int(sys.argv[sys.argv.index('-p') + 1])
    assert '-iTCP:' + os.environ['EXPECTED_PORT'] in sys.argv
    payload = json.loads(pathlib.Path(os.environ['STACK_CAPTURE']).read_text())
    assert pid == payload['pid']
    if os.environ.get('STACK_FOREIGN_LISTENER') != '1':
        os.kill(pid, 0)
        print(pid)
elif name == 'python':
    assert sys.argv[1:] == ['main.py']
    pathlib.Path(os.environ['STACK_BACKEND']).write_text(json.dumps({{'pid':os.getpid()}}))
    sys.exit(int(os.environ.get('STACK_BACKEND_EXIT', '0')))
'''
    for name in ("ollama", "uname", "sysctl", "curl", "lsof"):
        path = commands / name
        path.write_text(fake)
        path.chmod(0o755)
    python = checkout / "server" / ".venv" / "bin" / "python"
    python.write_text(fake)
    python.chmod(0o755)
    sleep = commands / "sleep"
    # Allow the synthetic Python version/hardware commands to finish while
    # keeping failed-startup and TERM-escalation paths bounded and fast.
    sleep.write_text("#!/bin/sh\nif [ \"$1\" = 1 ]; then exec /bin/sleep 0.3; fi\nexec /bin/sleep 0.02\n")
    sleep.chmod(0o755)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("OLLAMA_", "LLAMA_", "ORB_"))
        and key not in ("BASH_ENV", "ENV", "XDG_CONFIG_HOME")
    }
    environment.update({
        "PATH": f"{commands}:/usr/bin:/bin", "TMPDIR": str(tmp_path), "HOME": str(home),
        "STACK_CAPTURE": str(capture), "STACK_TRACE": str(trace),
        "STACK_READY": str(ready), "STACK_BACKEND": str(backend),
        "EXPECTED_BASE_URL": "http://localhost:11434", "EXPECTED_PORT": "11434",
    })

    def run(overrides=None, *, endpoint=None, existing=False, missing=False, interrupt=None):
        values = {**environment, **(overrides or {})}
        if endpoint is not None:
            values["EXPECTED_BASE_URL"] = endpoint.rstrip("/")
            port_token = endpoint.rstrip("/").rsplit(":", 1)[-1]
            values["EXPECTED_PORT"] = port_token if port_token.isdigit() else "80"
        (checkout / ".env").write_text(
            "ORB_OLLAMA_SWA_FULL=" + shlex.quote(values.get("ORB_OLLAMA_SWA_FULL", "auto")) + "\n"
            + ("ORB_OLLAMA_BASE_URL=" + shlex.quote(endpoint) + "\n" if endpoint else "")
        )
        if existing:
            ready.touch()
        if missing:
            (commands / "ollama").unlink()
        command = ["bash", str(checkout / "run-orb.sh")]
        if interrupt is None:
            result = subprocess.run(command, env=values, capture_output=True, text=True, timeout=12)
        else:
            process = subprocess.Popen(command, env=values, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 8
                while not capture.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert capture.exists(), "signal scenario requires an actual owned synthetic daemon"
                process.send_signal(interrupt)
                stdout, stderr = process.communicate(timeout=12)
                result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=12)
        payload = json.loads(capture.read_text()) if capture.exists() else None
        calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        if interrupt is None:
            assert backend.exists(), "optional AI failure must not prevent backend startup"
        else:
            assert not backend.exists(), "interrupted startup must not resume and launch the backend"
        return result, payload, calls

    run.directory = tmp_path
    run.config = config
    return run


def test_normal_start_uses_canonical_profile_and_stops_owned_daemon_after_backend_exit(stack):
    result, payload, calls = stack()
    assert result.returncode == 0, result.stdout + result.stderr
    assert payload["host"] == "http://localhost:11434"
    assert payload["swa"] == payload["cloud"] == payload["parallel"] == "1"
    assert payload["unified"] == "false"
    assert payload["ram"] == "8192"
    assert payload["xdg"] == str(stack.config.parents[1])
    assert [call["args"] for call in calls if call["tool"] == "ollama"] == [["--version"], ["serve"]]
    with pytest.raises(ProcessLookupError):
        os.kill(payload["pid"], 0)
    logs = list(stack.directory.glob("orb-ollama-serve.*"))
    assert len(logs) == 1
    assert logs[0].stat().st_mode & 0o777 == 0o600


def test_custom_loopback_endpoint_matches_backend_and_ignores_inherited_other_host(stack):
    result, payload, calls = stack({"OLLAMA_HOST": "https://remote.invalid:11434"}, endpoint="http://127.0.0.1:11436/")
    assert result.returncode == 0, result.stdout + result.stderr
    assert payload["host"] == "http://127.0.0.1:11436"
    assert {call["host"] for call in calls if call["tool"] == "ollama"} == {"http://127.0.0.1:11436"}
    assert {call["args"][-1] for call in calls if call["tool"] == "curl"} == {"http://127.0.0.1:11436/api/tags"}


@pytest.mark.parametrize("endpoint,host", [
    ("http://localhost", "http://localhost:80"), ("http://[::1]", "http://[::1]:80"),
])
def test_http_endpoint_without_port_uses_explicit_port80_for_actual_daemon(stack, endpoint, host):
    result, payload, calls = stack(endpoint=endpoint)
    assert result.returncode == 0, result.stdout + result.stderr
    assert payload["host"] == host
    assert {call["args"][-1] for call in calls if call["tool"] == "curl"} == {endpoint + "/api/tags"}


def test_reused_daemon_is_untouched_and_profile_unaffected(stack):
    # This owned synthetic stand-in represents an independently managed daemon.
    external = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        result, payload, calls = stack(existing=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "cache profile is unaffected" in result.stdout
        assert payload is None
        assert not [call for call in calls if call["tool"] in ("ollama", "lsof")]
        assert external.poll() is None
        assert list(stack.directory.glob("orb-ollama-serve.*")) == []
    finally:
        external.terminate()
        external.wait(timeout=5)


@pytest.mark.parametrize("endpoint", [
    "http://remote.invalid:11434", "http://0.0.0.0:11434",
    "https://localhost:11434", "http://localhost:0", "http://localhost:65536",
    "http://localhost:999999999999999999999", "http://localhost:11434/path",
])
def test_invalid_or_remote_endpoint_is_not_probed_and_boot_still_succeeds(stack, endpoint):
    result, payload, calls = stack(endpoint=endpoint)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Optional AI startup skipped" in result.stdout
    assert payload is None
    assert not [call for call in calls if call["tool"] in ("curl", "ollama", "lsof")]


def test_missing_optional_ollama_does_not_block_backend(stack):
    result, payload, calls = stack(missing=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ollama is unavailable" in result.stdout
    assert payload is None
    assert not [call for call in calls if call["tool"] == "ollama"]


def test_rejected_launcher_profile_does_not_block_backend(stack):
    result, payload, calls = stack({"FAKE_VERSION": "0.35.0", "ORB_OLLAMA_SWA_FULL": "on"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Optional AI engine did not become ready" in result.stdout
    assert payload is None
    assert [call["args"] for call in calls if call["tool"] == "ollama"] == [["--version"]]


@pytest.mark.parametrize("overrides", [
    {"STACK_NEVER_READY": "1"}, {"STACK_FOREIGN_LISTENER": "1"},
    {"STACK_IGNORE_TERM": "1"},
])
def test_owned_daemon_cleanup_is_bounded_for_failure_race_and_shutdown(stack, overrides):
    result, payload, _ = stack(overrides)
    assert result.returncode == 0, result.stdout + result.stderr
    assert payload is not None
    with pytest.raises(ProcessLookupError):
        os.kill(payload["pid"], 0)


def test_backend_failure_still_cleans_owned_daemon(stack):
    result, payload, _ = stack({"STACK_BACKEND_EXIT": "17"})
    assert result.returncode == 17
    with pytest.raises(ProcessLookupError):
        os.kill(payload["pid"], 0)


@pytest.mark.parametrize("interrupt,exit_code", [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_signal_during_optional_startup_exits_without_starting_backend(stack, interrupt, exit_code):
    result, payload, _ = stack({"STACK_NEVER_READY": "1"}, interrupt=interrupt)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert result.stdout.count("Shutting down") == 1
    with pytest.raises(ProcessLookupError):
        os.kill(payload["pid"], 0)
