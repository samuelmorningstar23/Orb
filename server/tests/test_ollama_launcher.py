"""Real launcher/setup stanzas with synthetic executables: no models or network."""

import json
import os
import shlex
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def launcher(tmp_path):
    checkout = tmp_path / "checkout with spaces"
    commands = tmp_path / "commands"
    commands.mkdir()
    (checkout / "scripts").mkdir(parents=True)
    script = checkout / "scripts" / "serve-ollama.sh"
    shutil.copyfile(ROOT / "scripts" / "serve-ollama.sh", script)
    script.chmod(0o755)
    config = checkout / "config" / "ollama-xdg" / "llama.cpp" / "config.ini"
    config.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "config" / "ollama-xdg" / "llama.cpp" / "config.ini", config)
    home = tmp_path / "home"
    home.mkdir()
    system_config = tmp_path / "system" / "llama.cpp" / "config.ini"
    # Never consult the host's system configuration in synthetic launcher tests.
    script.write_text(script.read_text().replace(
        "system_config='/etc/llama.cpp/config.ini'",
        "system_config=" + shlex.quote(str(system_config)),
    ))
    capture = tmp_path / "served.json"
    ready = tmp_path / "ready"
    trace = tmp_path / "commands.jsonl"
    fake = f'''#!{sys.executable}
import json, os, pathlib, sys, time
name = pathlib.Path(sys.argv[0]).name
if name == 'ollama':
    with open(os.environ['LAUNCH_TRACE'], 'a') as stream:
        stream.write(json.dumps(sys.argv[1:]) + '\\n')
    if sys.argv[1:] == ['--version']:
        print(os.environ['FAKE_VERSION'])
    elif sys.argv[1:] == ['serve']:
        keys = ['OLLAMA_HOST', 'OLLAMA_NO_CLOUD', 'OLLAMA_NOPRUNE',
                'OLLAMA_NUM_PARALLEL', 'OLLAMA_KV_CACHE_TYPE',
                'LLAMA_ARG_SWA_FULL', 'LLAMA_ARG_KV_UNIFIED', 'LLAMA_ARG_CACHE_RAM',
                'XDG_CONFIG_HOME', 'OLLAMA_FLASH_ATTENTION',
                'LLAMA_ARG_BATCH', 'LLAMA_ARG_UBATCH']
        pathlib.Path(os.environ['LAUNCH_CAPTURE']).write_text(json.dumps({{
            'argv':sys.argv[1:], 'pid':os.getpid(), 'env':{{key:os.environ.get(key) for key in keys}},
        }}))
        if os.environ.get('SETUP_READY_ON_SERVE') == '1':
            pathlib.Path(os.environ['SETUP_READY']).touch()
            time.sleep(0.5)
        if os.environ.get('SETUP_HANG_ON_SERVE') == '1':
            time.sleep(30)
    else:
        sys.exit('Unexpected command: no model pull/run/stop is allowed')
elif name == 'uname':
    print(os.environ['FAKE_OS' if sys.argv[1] == '-s' else 'FAKE_ARCH'])
elif name == 'sysctl':
    assert sys.argv[1:] == ['-n', 'hw.memsize']
    print(os.environ['FAKE_MEMORY'])
elif name == 'curl':
    assert sys.argv[-1] == 'http://127.0.0.1:11434/api/tags'
    sys.exit(0 if pathlib.Path(os.environ['SETUP_READY']).exists() else 1)
elif name == 'lsof':
    pid = int(sys.argv[sys.argv.index('-p') + 1])
    payload = json.loads(pathlib.Path(os.environ['LAUNCH_CAPTURE']).read_text())
    assert pid == payload['pid']
    if os.environ.get('SETUP_FOREIGN_LISTENER') != '1':
        os.kill(pid, 0)
        print(pid)
'''
    for command in ("ollama", "uname", "sysctl", "curl", "lsof"):
        path = commands / command
        path.write_text(fake)
        path.chmod(0o755)
    # Bounded setup polling only yields to our owned synthetic child.
    sleep = commands / "sleep"
    sleep.write_text("#!/bin/sh\nexec /bin/sleep 0.02\n")
    sleep.chmod(0o755)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("OLLAMA_", "LLAMA_", "ORB_OLLAMA_"))
        and key not in ("BASH_ENV", "ENV", "XDG_CONFIG_HOME")
    }
    environment.update({
        "PATH": f"{commands}:/usr/bin:/bin",
        "FAKE_VERSION": "ollama version is 0.35.1",
        "FAKE_OS": "Darwin", "FAKE_ARCH": "arm64", "FAKE_MEMORY": str(64 * 2**30),
        "LAUNCH_CAPTURE": str(capture), "LAUNCH_TRACE": str(trace),
        "SETUP_READY": str(ready), "TMPDIR": str(tmp_path),
        "REPO_ROOT": str(checkout), "HOME": str(home),
    })

    def run(overrides=None, *, setup=False, arguments=()):
        env = {**environment, **(overrides or {})}
        if setup:
            source = (ROOT / "setup-mac.sh").read_text()
            start = source.index("ensure_setup_ollama() {")
            end = source.index("\n}\n", start) + 3
            helper = source[start:end]
            command = ["bash", "-c", "set -euo pipefail\n"
                       "info() { printf '%s\\n' \"$*\"; }\n"
                       "ok() { printf '%s\\n' \"$*\"; }\n"
                       "warn() { printf '%s\\n' \"$*\" >&2; }\n"
                       "err() { printf '%s\\n' \"$*\" >&2; }\n"
                       + helper + "\nensure_setup_ollama\nprintf 'setup-host=%s\\n' \"$OLLAMA_HOST\"\nprintf 'startup completed\\n'\n"]
        else:
            command = ["bash", str(script), *arguments]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        payload = json.loads(capture.read_text()) if capture.exists() else None
        calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        return result, payload, calls

    run.ready = ready
    run.directory = tmp_path
    run.config = config
    run.home = home
    run.system_config = system_config
    return run


def test_qualified_auto_profile_reaches_actual_exec_with_unchanged_batching(launcher):
    result, payload, calls = launcher({"OLLAMA_FLASH_ATTENTION": "auto"})
    assert result.returncode == 0, result.stderr
    assert calls == [["--version"], ["serve"]]
    assert payload["argv"] == ["serve"]
    assert payload["env"] == {
        "OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_NOPRUNE": "1", "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_KV_CACHE_TYPE": "f16", "LLAMA_ARG_SWA_FULL": "1",
        "LLAMA_ARG_KV_UNIFIED": "false", "LLAMA_ARG_CACHE_RAM": "8192",
        "XDG_CONFIG_HOME": str(launcher.config.parents[1]),
        "OLLAMA_FLASH_ATTENTION": "auto", "LLAMA_ARG_BATCH": None,
        "LLAMA_ARG_UBATCH": None,
    }


@pytest.mark.parametrize("overrides", [
    {"FAKE_VERSION": "ollama version is 0.35.0"},
    {"FAKE_VERSION": "ollama version is 0.36.0"},
    {"FAKE_VERSION": "ollama version is 0.35.1-rc1"},
    {"FAKE_OS": "Linux"}, {"FAKE_ARCH": "x86_64"},
    {"FAKE_MEMORY": str(64 * 2**30 - 1)}, {"FAKE_MEMORY": "unknown"},
    {"FAKE_MEMORY": "9999999999999999999999999999999999"},
])
def test_unqualified_auto_profile_does_not_silently_enable_swa(launcher, overrides):
    result, payload, calls = launcher(overrides)
    assert result.returncode == 0, result.stderr
    assert payload["env"]["LLAMA_ARG_SWA_FULL"] is None
    assert "config.ini may independently enable" in result.stderr
    assert calls == [["--version"], ["serve"]]


@pytest.mark.parametrize("overrides", [
    {"FAKE_VERSION": "ollama version is 0.35.0"},
    {"FAKE_OS": "Linux"}, {"FAKE_ARCH": "x86_64"},
    {"FAKE_MEMORY": str(32 * 2**30)}, {"FAKE_MEMORY": "unknown"},
])
def test_forced_profile_rejects_unqualified_runtime_before_serve(launcher, overrides):
    result, payload, calls = launcher({**overrides, "ORB_OLLAMA_SWA_FULL": "on"})
    assert result.returncode == 1
    assert "Full SWA requires tested" in result.stderr
    assert payload is None
    assert calls == [["--version"]]


@pytest.mark.parametrize("actual,server,enabled", [
    ("0.35.1", "0.99.0", True), ("0.34.0", "0.35.1", False),
])
def test_version_guard_uses_executable_version_instead_of_running_server(launcher, actual, server, enabled):
    result, payload, _ = launcher({
        "FAKE_VERSION": f"ollama version is {server}\nWarning: client version is {actual}",
    })
    assert result.returncode == 0, result.stderr
    assert (payload["env"]["LLAMA_ARG_SWA_FULL"] == "1") is enabled


def test_disconnected_client_version_output_is_supported(launcher):
    result, payload, _ = launcher({
        "FAKE_VERSION": "Warning: could not connect to a running Ollama instance\nWarning: client version is 0.35.1",
    })
    assert result.returncode == 0, result.stderr
    assert payload["env"]["LLAMA_ARG_SWA_FULL"] == "1"


@pytest.mark.parametrize("explicit", ["1", "true", "on", "enabled", "0", "false", "off", "disabled"])
def test_explicit_swa_setting_is_preserved(launcher, explicit):
    result, payload, _ = launcher({"LLAMA_ARG_SWA_FULL": explicit})
    assert result.returncode == 0, result.stderr
    assert payload["env"]["LLAMA_ARG_SWA_FULL"] == explicit


@pytest.mark.parametrize("profile,explicit", [("on", "0"), ("off", "1")])
def test_conflicting_explicit_swa_settings_fail_before_serve(launcher, profile, explicit):
    result, payload, calls = launcher({"ORB_OLLAMA_SWA_FULL": profile, "LLAMA_ARG_SWA_FULL": explicit})
    assert result.returncode == 1
    assert "conflicts" in result.stderr
    assert payload is None
    assert calls == [["--version"]]


def test_off_does_not_claim_to_override_runner_configuration(launcher):
    result, payload, _ = launcher({"ORB_OLLAMA_SWA_FULL": "off"})
    assert result.returncode == 0, result.stderr
    assert payload["env"]["LLAMA_ARG_SWA_FULL"] is None
    assert "config.ini may independently enable" in result.stderr


@pytest.mark.parametrize("overrides", [
    {"ORB_OLLAMA_SWA_FULL": "yes"}, {"LLAMA_ARG_SWA_FULL": "surprise"},
    {"FAKE_VERSION": "unrecognized output"},
    {"OLLAMA_KV_CACHE_TYPE": "q8_0"},
])
def test_malformed_or_quality_changing_settings_are_rejected(launcher, overrides):
    result, payload, _ = launcher(overrides)
    assert result.returncode == 1
    assert payload is None


@pytest.mark.parametrize("host", [
    "0.0.0.0:11434", "https://127.0.0.1:11434", "example.com:11434",
    "127.0.0.1.evil:11434", "127.0.0.1:0", "127.0.0.1:65536",
    "127.0.0.1:9999999999999999999999999", "127.0.0.1:11434/path",
])
def test_nonloopback_and_malformed_hosts_fail_before_even_version_contact(launcher, host):
    result, payload, calls = launcher({"OLLAMA_HOST": host})
    assert result.returncode == 1
    assert payload is None
    assert calls == []


@pytest.mark.parametrize("host", ["127.0.0.1:11436", "http://localhost:11434", "[::1]:11434"])
def test_supported_loopback_hosts_are_preserved(launcher, host):
    result, payload, _ = launcher({"OLLAMA_HOST": host})
    assert result.returncode == 0, result.stderr
    assert payload["env"]["OLLAMA_HOST"] == host


def test_setup_uses_canonical_launcher_and_private_log_before_ready(launcher):
    result, payload, calls = launcher({"SETUP_READY_ON_SERVE": "1"}, setup=True)
    assert result.returncode == 0, result.stderr
    assert "startup completed" in result.stdout
    assert payload["env"]["LLAMA_ARG_SWA_FULL"] == "1"
    assert calls == [["--version"], ["serve"]]
    logs = list(launcher.directory.glob("orb-ollama-serve.*"))
    assert len(logs) == 1
    assert logs[0].stat().st_mode & 0o777 == 0o600


def test_setup_reuses_existing_daemon_without_claiming_profile_applied(launcher):
    launcher.ready.touch()
    result, payload, calls = launcher(setup=True)
    assert result.returncode == 0, result.stderr
    assert "cache profile was NOT applied" in result.stderr
    assert payload is None
    assert calls == []
    assert list(launcher.directory.glob("orb-ollama-serve.*")) == []


@pytest.mark.parametrize("inherited_host", ["127.0.0.1:11436", "https://remote.invalid:11434"])
def test_setup_exports_one_canonical_loopback_for_all_following_model_commands(launcher, inherited_host):
    launcher.ready.touch()
    result, payload, calls = launcher({"OLLAMA_HOST": inherited_host}, setup=True)
    assert result.returncode == 0, result.stderr
    assert "setup-host=127.0.0.1:11434" in result.stdout
    assert payload is None
    assert calls == []
    source = (ROOT / "setup-mac.sh").read_text()
    assert source.index("  ensure_setup_ollama\n") < source.index('  ok "ollama $(ollama --version')


@pytest.mark.parametrize("foreign_listener", [False, True])
def test_failed_owned_startup_is_cleaned_up_without_accepting_another_listener(launcher, foreign_listener):
    result, payload, calls = launcher({
        "SETUP_HANG_ON_SERVE": "1",
        "SETUP_READY_ON_SERVE": "1" if foreign_listener else "0",
        "SETUP_FOREIGN_LISTENER": "1" if foreign_listener else "0",
    }, setup=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "no model pulls were attempted" in result.stderr
    assert calls == [["--version"], ["serve"]]
    assert payload is not None
    with pytest.raises(ProcessLookupError):
        os.kill(payload["pid"], 0)


def test_setup_stops_before_pulls_if_launcher_rejects_profile(launcher):
    result, payload, calls = launcher({
        "ORB_OLLAMA_SWA_FULL": "on", "FAKE_VERSION": "ollama version is 0.35.0",
    }, setup=True)
    assert result.returncode == 1
    assert "no model pulls were attempted" in result.stderr
    assert "startup completed" not in result.stdout
    assert payload is None
    assert calls == [["--version"]]
    logs = list(launcher.directory.glob("orb-ollama-serve.*"))
    assert len(logs) == 1
    assert "Full SWA requires tested" in logs[0].read_text()


@pytest.mark.parametrize("explicit", ["0", "false", "off", "disabled"])
def test_qualified_profile_canonicalizes_separate_kv_and_preserves_one_slot(launcher, explicit):
    result, payload, _ = launcher({
        "LLAMA_ARG_KV_UNIFIED": explicit, "LLAMA_ARG_CACHE_RAM": "8192",
        "OLLAMA_NUM_PARALLEL": "2",
    })
    assert result.returncode == 0, result.stderr
    assert payload["env"]["OLLAMA_NUM_PARALLEL"] == "1"
    assert payload["env"]["LLAMA_ARG_KV_UNIFIED"] == "false"
    assert payload["env"]["LLAMA_ARG_CACHE_RAM"] == "8192"
    assert "One inference slot; prompt similarity=0.6" in result.stderr


@pytest.mark.parametrize("overrides", [
    {"LLAMA_ARG_KV_UNIFIED": value} for value in ("1", "true", "on", "enabled", "", "surprise")
] + [
    {"LLAMA_ARG_CACHE_RAM": value} for value in ("0", "-1", "4096", "", "surprise")
])
def test_qualified_profile_rejects_cache_settings_that_break_retention(launcher, overrides):
    result, payload, calls = launcher(overrides)
    assert result.returncode == 1
    assert next(iter(overrides)) in result.stderr
    assert payload is None
    assert calls == [["--version"]]


@pytest.mark.parametrize("profile", ["auto", "on"])
@pytest.mark.parametrize("location", ["home", "xdg", "system"])
@pytest.mark.parametrize("kind", ["file", "dangling_symlink"])
def test_custom_runner_config_is_never_displaced(launcher, profile, location, kind):
    overrides = {"ORB_OLLAMA_SWA_FULL": profile}
    if location == "system":
        custom = launcher.system_config
    elif location == "xdg":
        xdg = launcher.directory / "custom xdg"
        overrides["XDG_CONFIG_HOME"] = str(xdg)
        custom = xdg / "llama.cpp" / "config.ini"
    else:
        custom = launcher.home / ".config" / "llama.cpp" / "config.ini"
    custom.parent.mkdir(parents=True)
    if kind == "file":
        custom.write_text("[default]\nslot-prompt-similarity=0.9\n")
        original = custom.read_bytes()
    else:
        custom.symlink_to(custom.parent / "missing.ini")
        original = custom.readlink()
    result, payload, calls = launcher(overrides)
    expected_calls = [["--version"], ["serve"]] if profile == "auto" else [["--version"]]
    assert calls == expected_calls
    if profile == "auto":
        assert result.returncode == 0, result.stderr
        assert payload["env"]["XDG_CONFIG_HOME"] == overrides.get("XDG_CONFIG_HOME")
        assert payload["env"]["LLAMA_ARG_SWA_FULL"] is None
        assert payload["env"]["LLAMA_ARG_KV_UNIFIED"] is None
        assert payload["env"]["LLAMA_ARG_CACHE_RAM"] is None
        assert payload["env"]["OLLAMA_NUM_PARALLEL"] == "1"
        assert "Optimized cache profile skipped" in result.stderr
    else:
        assert result.returncode == 1
        assert payload is None
        assert "Cannot enable the optimized cache profile" in result.stderr
    assert (custom.read_bytes() if kind == "file" else custom.readlink()) == original


@pytest.mark.parametrize("profile", ["auto", "on"])
@pytest.mark.parametrize("problem", ["relative", "missing_home", "dangling_root", "unsearchable"])
def test_unknown_original_config_location_is_preserved_or_refused(launcher, profile, problem):
    overrides = {"ORB_OLLAMA_SWA_FULL": profile}
    restore = None
    if problem == "relative":
        overrides["XDG_CONFIG_HOME"] = "relative-config"
    elif problem == "missing_home":
        overrides["HOME"] = ""
    elif problem == "dangling_root":
        path = launcher.directory / "missing-config-link"
        path.symlink_to(launcher.directory / "absent-directory", target_is_directory=True)
        overrides["XDG_CONFIG_HOME"] = str(path)
    else:
        restore = launcher.home / ".config"
        restore.mkdir()
        restore.chmod(0)
    try:
        result, payload, _ = launcher(overrides)
        if profile == "auto":
            assert result.returncode == 0, result.stderr
            assert payload["env"]["XDG_CONFIG_HOME"] == overrides.get("XDG_CONFIG_HOME")
            assert payload["env"]["LLAMA_ARG_SWA_FULL"] is None
            assert "Optimized cache profile skipped" in result.stderr
        else:
            assert result.returncode == 1
            assert payload is None
            assert "Cannot enable the optimized cache profile" in result.stderr
    finally:
        if restore is not None:
            restore.chmod(0o700)


@pytest.mark.parametrize("content", [
    "", "[default]\nslot-prompt-similarity=0.1\n", "[other]\nslot-prompt-similarity=0.6\n",
    "[default]\nslot-prompt-similarity=0.6\nparallel=2\n",
])
def test_owned_runner_config_requires_exact_narrow_content(launcher, content):
    launcher.config.write_text(content)
    result, payload, calls = launcher()
    assert result.returncode == 1
    assert "unexpected content" in result.stderr
    assert payload is None
    assert calls == [["--version"]]


@pytest.mark.parametrize("kind", ["missing", "unreadable", "symlink", "directory"])
def test_owned_runner_config_must_be_readable_regular_source(launcher, kind):
    if kind == "unreadable":
        launcher.config.chmod(0)
    else:
        launcher.config.unlink()
        if kind == "symlink":
            target = launcher.directory / "replacement.ini"
            target.write_text("[default]\nslot-prompt-similarity=0.6\n")
            launcher.config.symlink_to(target)
        elif kind == "directory":
            launcher.config.mkdir()
    try:
        result, payload, calls = launcher()
        assert result.returncode == 1
        assert "missing, unreadable, or a symlink" in result.stderr
        assert payload is None
        assert calls == [["--version"]]
    finally:
        if kind == "unreadable":
            launcher.config.chmod(0o600)


@pytest.mark.parametrize("overrides", [
    {"ORB_OLLAMA_SWA_FULL": "off"}, {"LLAMA_ARG_SWA_FULL": "false"},
    {"FAKE_OS": "Linux"}, {"FAKE_VERSION": "ollama version is 0.36.0"},
])
def test_disabled_or_unqualified_profile_does_not_rebind_config_or_cache_settings(launcher, overrides):
    xdg = launcher.directory / "independent-config"
    custom = xdg / "llama.cpp" / "config.ini"
    custom.parent.mkdir(parents=True)
    custom.write_text("[default]\nslot-prompt-similarity=0.9\n")
    result, payload, _ = launcher({
        **overrides, "XDG_CONFIG_HOME": str(xdg),
        "LLAMA_ARG_KV_UNIFIED": "true", "LLAMA_ARG_CACHE_RAM": "0",
    })
    assert result.returncode == 0, result.stderr
    assert payload["env"]["XDG_CONFIG_HOME"] == str(xdg)
    assert payload["env"]["LLAMA_ARG_KV_UNIFIED"] == "true"
    assert payload["env"]["LLAMA_ARG_CACHE_RAM"] == "0"
    assert payload["env"]["OLLAMA_NUM_PARALLEL"] == "1"
    assert custom.read_text() == "[default]\nslot-prompt-similarity=0.9\n"


def test_auto_skip_preserves_explicit_swa_without_claiming_owned_profile(launcher):
    custom = launcher.home / ".config" / "llama.cpp" / "config.ini"
    custom.parent.mkdir(parents=True)
    custom.write_text("[default]\nslot-prompt-similarity=0.9\n")
    result, payload, _ = launcher({"LLAMA_ARG_SWA_FULL": "1"})
    assert result.returncode == 0, result.stderr
    assert payload["env"]["LLAMA_ARG_SWA_FULL"] == "1"
    assert payload["env"]["XDG_CONFIG_HOME"] is None
    assert "Full SWA cache enabled for" not in result.stderr


def test_empty_xdg_value_uses_home_config_conflict_check(launcher):
    custom = launcher.home / ".config" / "llama.cpp" / "config.ini"
    custom.parent.mkdir(parents=True)
    custom.write_text("[default]\nslot-prompt-similarity=0.9\n")
    result, payload, _ = launcher({"XDG_CONFIG_HOME": ""})
    assert result.returncode == 0, result.stderr
    assert payload["env"]["XDG_CONFIG_HOME"] == ""
    assert payload["env"]["LLAMA_ARG_SWA_FULL"] is None
    assert "Optimized cache profile skipped" in result.stderr


def test_help_describes_one_slot_config_boundary_without_executing_commands(launcher):
    result, payload, calls = launcher(arguments=("--help",))
    assert result.returncode == 0
    assert "one inference slot" in result.stdout
    assert "prompt similarity 0.6" in result.stdout
    assert "auto skips this profile" in result.stdout
    assert payload is None
    assert calls == []
