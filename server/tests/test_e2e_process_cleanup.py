"""The disposable browser harness owns and stops only its synthetic children."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


_path = Path(__file__).resolve().parents[2] / "scripts/e2e_server.py"
_spec = importlib.util.spec_from_file_location("orb_e2e_process_cleanup", _path)
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)


def test_owned_child_preserves_the_command_exit_status(tmp_path):
    assert e2e.run_owned_child([sys.executable, "-c", "raise SystemExit(7)"],
                               cwd=tmp_path, env=dict(os.environ)) == 7


def test_checked_seed_failure_is_reported(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as failure:
        e2e.run_owned_child([sys.executable, "-c", "raise SystemExit(7)"],
                            cwd=tmp_path, env=dict(os.environ), check=True)
    assert failure.value.returncode == 7


def test_status_stream_failure_closes_pipes_before_any_process_starts(tmp_path, monkeypatch):
    opened = []
    pipe = os.pipe

    def capture_pipe():
        descriptors = pipe()
        opened.extend(descriptors)
        return descriptors

    def fail_stream(*args, **kwargs):
        raise OSError("injected status stream failure")

    def unexpected_process(*args, **kwargs):
        raise AssertionError("status channel must exist before a process starts")

    monkeypatch.setattr(e2e.os, "pipe", capture_pipe)
    monkeypatch.setattr(e2e.os, "fdopen", fail_stream)
    monkeypatch.setattr(e2e.subprocess, "Popen", unexpected_process)
    with pytest.raises(OSError, match="status stream failure"):
        e2e.run_owned_child([sys.executable, "-c", "pass"], cwd=tmp_path, env=dict(os.environ))
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_cancelled_command_kills_its_term_resistant_descendants(tmp_path, monkeypatch):
    ready = tmp_path / "child.pid"
    descendant = tmp_path / "descendant.pid"
    descendant_code = (
        "import os,pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "pathlib.Path('descendant.pid').write_text(str(os.getpid()))\n"
        "while True:\n pathlib.Path('descendant.heartbeat').write_text(str(time.monotonic_ns())); time.sleep(0.01)"
    )
    code = (
        "import os, pathlib, signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
        "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
        "while True:\n pathlib.Path('child.heartbeat').write_text(str(time.monotonic_ns())); time.sleep(0.01)"
    )
    fdopen = os.fdopen

    class InterruptedStatus:
        def __init__(self, *args, **kwargs):
            self.stream = fdopen(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def readline(self):
            deadline = time.monotonic() + 5
            while not (ready.exists() and descendant.exists()
                       and (tmp_path / "child.heartbeat").exists()
                       and (tmp_path / "descendant.heartbeat").exists()):
                if time.monotonic() >= deadline:
                    raise AssertionError("synthetic child did not start")
                time.sleep(0.01)
            raise KeyboardInterrupt

    monkeypatch.setattr(e2e.os, "fdopen", InterruptedStatus)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        e2e.run_owned_child([sys.executable, "-c", code], cwd=tmp_path,
                            env=dict(os.environ), grace_seconds=0.1)
    assert time.monotonic() - started < 5
    # No process-table access is needed (restricted on local macOS sandboxes).
    # Both TERM-resistant writers must stop after the group KILL, including the
    # grandchild that a direct-child-only cleanup would leave running.
    heartbeats = [tmp_path / "child.heartbeat", tmp_path / "descendant.heartbeat"]
    stopped = [marker.read_text() for marker in heartbeats]
    time.sleep(0.15)
    assert [marker.read_text() for marker in heartbeats] == stopped


@pytest.mark.parametrize("interrupt", [None, "seed", "server"])
def test_seed_and_server_share_cleanup_boundary_and_temporary_checkout(
    tmp_path, monkeypatch, interrupt,
):
    root = tmp_path / "source"
    (root / "dist").mkdir(parents=True)
    (root / "dist/index.html").write_text("synthetic app")
    monkeypatch.setattr(e2e, "ROOT", root)
    monkeypatch.setattr(e2e, "copy_application", lambda _root, target: (target / "server").mkdir())
    monkeypatch.setattr(e2e.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["e2e_server.py"])
    calls = []

    def owned(command, **kwargs):
        stage = "seed" if "scripts/reset_db_and_seed.py" in command else "server"
        calls.append((stage, kwargs))
        assert kwargs["cwd"].is_dir()
        assert kwargs["env"]["ORB_DB_PATH"].startswith(str(kwargs["cwd"].parent))
        if stage == interrupt:
            raise KeyboardInterrupt
        return 0 if stage == "seed" else 42

    monkeypatch.setattr(e2e, "run_owned_child", owned)
    assert e2e.main() == (130 if interrupt else 42)
    assert [stage for stage, _kwargs in calls] == (["seed"] if interrupt == "seed" else ["seed", "server"])
    assert calls[0][1]["check"] is True
    assert all(not kwargs["cwd"].parent.exists() for _stage, kwargs in calls)
