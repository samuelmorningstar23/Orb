"""The acceptance runner owns its output and children; never use live data."""

import importlib.util
import io
import signal
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "backend_runner", Path(__file__).with_name("run_backend.py")
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_source_copy_excludes_live_data_environment_models_and_links(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    out = tmp_path / "owned"
    names = [
        "server/a.py",
        "server/.env",
        "server/site_config/site.toml",
        "server/uploads/private.json",
        "server/ops/live.json",
        "server/models/private.pt",
        "server/models/model.py",
        "server/a.db",
        "server/link.py",
        "scripts/lib/types.d.mts",
    ]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith("link.py"):
            path.symlink_to(root / "server/a.py")
        else:
            path.write_text("synthetic only")
    (root / "dist").mkdir()
    (root / "dist/index.html").write_text("test app")
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "OUT", out)
    monkeypatch.setattr(
        runner.subprocess, "check_output", lambda *a, **k: "\0".join(names).encode()
    )
    source = runner.prepare()
    assert (source / "scripts/lib/types.d.mts").exists()
    assert (source / "server/a.py").exists() and (source / "server/models/model.py").exists()
    for name in names[1:]:
        if name not in {"server/models/model.py", "scripts/lib/types.d.mts"}:
            assert not (source / name).exists(), name
    assert (source / "dist/index.html").read_text() == "test app"
    with pytest.raises(FileExistsError):
        runner.prepare()


def test_output_inside_the_checkout_is_refused_before_copying(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runner", "--output", str(runner.ROOT / "tmp/unsafe")])
    monkeypatch.setattr(runner, "prepare", lambda: pytest.fail("unsafe destination reached copy"))
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2


def test_cleanup_targets_only_the_owned_still_running_process_group(monkeypatch):
    signals = []
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    class Child:
        pid = 12345

        def poll(self):
            return None

        def wait(self, timeout):
            if timeout == 15:
                raise subprocess.TimeoutExpired("owned", timeout)

    log = io.StringIO()
    runner._stop_owned_children([(Child(), log, {}, 0)])
    assert signals == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)] and log.closed

    class Done(Child):
        def poll(self):
            return 0

    signals.clear()
    log = io.StringIO()
    runner._stop_owned_children([(Done(), log, {}, 0)])
    assert signals == [] and log.closed


def test_monitor_retains_exit_failures_and_waits_for_its_own_children(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "OUT", tmp_path)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    class Child:
        values = iter([None, 7])

        def poll(self):
            return next(self.values)

    log = io.StringIO()
    row = {"index": 1}
    report = {"shards": [row]}
    runner._monitor_children([(Child(), log, row, runner.time.monotonic())], report)
    assert row["exit_code"] == 7 and log.closed and (tmp_path / "manifest.json").exists()


def test_environment_discards_appliance_paths_and_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_DB_PATH", "/live/patient.db")
    monkeypatch.setenv("ORB_JWT_SECRET", "live-secret")
    monkeypatch.setenv("HF_HOME", "/live/models")
    monkeypatch.setenv("OPENAI_API_KEY", "live-token")
    env = runner.environment(tmp_path)
    assert env["ORB_DB_PATH"] == str(tmp_path / "orb_test_shard.db")
    assert env["ORB_JWT_SECRET"] != "live-secret"
    assert env["HF_HOME"] == str(tmp_path / "hf-cache")
    assert env["HF_HUB_OFFLINE"] == "1"
    assert "OPENAI_API_KEY" not in env


def test_junit_counts_separates_explicit_skips_and_expected_failures(tmp_path):
    report = tmp_path / "result.xml"
    report.write_text(
        "<testsuite><testcase/><testcase><failure/></testcase>"
        '<testcase><error/></testcase><testcase><skipped type="pytest.skip"/></testcase>'
        '<testcase><skipped type="pytest.xfail"/></testcase></testsuite>'
    )
    assert runner.junit_counts([report]) == {
        "passed": 1,
        "failed": 1,
        "error": 1,
        "skipped": 1,
        "expected_failure": 1,
    }
