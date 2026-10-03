"""Proof that the event-loop starvation watchdog actually fires.

Context — the failure being detected (2026-08-01): under concurrent load the
whole process froze permanently in ~30 s. /api/health was dead, the process sat
at 0.2% CPU, three `kill -USR1` dumps 8 s apart were identical, and it needed
kill -9. The main thread was blocked inside `take_gil` — starved of the GIL by
two threads deadlocked on SQLite's per-inode VFS mutex.

/api/health cannot detect that: it is served BY the loop. So services/loop_watchdog
watches from outside the loop in two lanes, and this file proves BOTH of them,
because each catches a failure the other cannot:

  test_python_lane_*  — the loop blocks in a call that releases the GIL
                        (time.sleep). Other Python threads still run, so the
                        daemon-thread lane fires and writes the annotated dump.
  test_deadman_lane_* — the loop blocks in a single C call that HOLDS the GIL
                        for its whole duration (math.factorial). No Python
                        thread can be scheduled at all — including the watchdog's
                        own — and only the GIL-free faulthandler timer thread can
                        still write. This is the shape of the real incident.

Both scenarios run in a SUBPROCESS: a multi-second GIL hog inside the pytest
process would stall pytest itself, and a stall test that stalls its own runner
proves nothing. No database is touched — the watchdog imports no DB module, by
design, since the DB is a prime suspect in the failure it records.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent

# One driver script, two modes. Written to the tmp dir at run time so the frame
# names the test asserts on are unmistakable in the dump.
_DRIVER = textwrap.dedent(
    '''
    import asyncio, json, math, os, sys, threading, time
    sys.path.insert(0, {server!r})

    MODE, OUTDIR = sys.argv[1], sys.argv[2]
    os.environ["ORB_WATCHDOG_DIR"] = OUTDIR

    from services.loop_watchdog import start_watchdog, stop_watchdog, get_watchdog


    class Canary(threading.Thread):
        """Pure-Python ticker. It can only advance when the GIL is available, so
        a flat count across the stall window is proof of GIL starvation — and
        therefore proof that the Python watchdog lane could not have run."""

        def __init__(self):
            super().__init__(daemon=True, name="canary")
            self.ticks = 0

        def run(self):
            while True:
                self.ticks += 1
                time.sleep(0.01)


    def orb_test_blocking_sleep(seconds):
        time.sleep(seconds)          # releases the GIL


    def orb_test_gil_hog(n):
        return math.factorial(n)     # one C call, holds the GIL throughout


    async def main():
        task = start_watchdog()
        canary = Canary(); canary.start()
        await asyncio.sleep(2.0)             # let beats land

        recreated = None
        if MODE == "vanish":
            # Someone deletes the evidence file out from under the C timer.
            import pathlib
            p = pathlib.Path(get_watchdog().deadman_path)
            p.unlink()
            await asyncio.sleep(3.0)         # monitor thread should notice
            recreated = p.exists()

        before = canary.ticks
        t0 = time.time()
        if MODE == "sleep":
            orb_test_blocking_sleep(8.0)
        else:
            orb_test_gil_hog(1_200_000)
        wall = time.time() - t0
        ticks = canary.ticks - before

        await asyncio.sleep(6.0)             # let the monitor see recovery
        wd = get_watchdog()
        out = {{
            "mode": MODE,
            "stall_wall_seconds": wall,
            "canary_ticks_during_stall": ticks,
            "canary_ticks_per_second": ticks / wall,
            "deadman_recreated": recreated,
            "snapshot": wd.snapshot(),
        }}
        stop_watchdog()
        if task is not None and not task.done():
            task.cancel()
        print("SUMMARY_JSON_START")
        print(json.dumps(out, default=str))
        print("SUMMARY_JSON_END")

    asyncio.run(main())
    '''
)


def _run_scenario(tmp_path: Path, mode: str, env_overrides: dict) -> tuple[dict, Path, str]:
    outdir = tmp_path / f"wd_{mode}"
    outdir.mkdir(parents=True, exist_ok=True)
    driver = tmp_path / f"driver_{mode}.py"
    driver.write_text(_DRIVER.format(server=str(SERVER_DIR)))

    env = dict(os.environ)
    env.pop("ORB_WATCHDOG_ENABLED", None)
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, str(driver), mode, str(outdir)],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(SERVER_DIR),
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    blob = proc.stdout.split("SUMMARY_JSON_START")[1].split("SUMMARY_JSON_END")[0]
    return json.loads(blob), outdir, proc.stderr


# Fast thresholds so the test is seconds, not minutes. The SHAPE of the
# behaviour is what is under test; the production numbers (30 s / 60 s) are
# justified in the module docstring against measured p99 and the known
# multi-second legitimate blockers.
_FAST = {
    "ORB_WATCHDOG_BEAT_S": "0.2",
    "ORB_WATCHDOG_POLL_S": "0.5",
    "ORB_WATCHDOG_STALL_S": "3",
    "ORB_WATCHDOG_GRACE_S": "0",
}


@pytest.fixture(scope="module")
def sleep_run(tmp_path_factory):
    # Dead-man far out of the way so this exercises the Python lane alone.
    env = dict(_FAST, ORB_WATCHDOG_DEADMAN_S="120", ORB_WATCHDOG_DEADMAN_REARM_S="30")
    return _run_scenario(tmp_path_factory.mktemp("sleep"), "sleep", env)


@pytest.fixture(scope="module")
def gilhog_run(tmp_path_factory):
    env = dict(_FAST, ORB_WATCHDOG_DEADMAN_S="4", ORB_WATCHDOG_DEADMAN_REARM_S="1")
    return _run_scenario(tmp_path_factory.mktemp("gilhog"), "gilhog", env)


@pytest.fixture(scope="module")
def vanish_run(tmp_path_factory):
    env = dict(_FAST, ORB_WATCHDOG_DEADMAN_S="4", ORB_WATCHDOG_DEADMAN_REARM_S="1")
    return _run_scenario(tmp_path_factory.mktemp("vanish"), "vanish", env)


# ── Lane 2: Python daemon thread ─────────────────────────────────────────────
def test_python_lane_detects_a_blocked_loop(sleep_run):
    summary, _, stderr = sleep_run
    snap = summary["snapshot"]
    assert snap["stalls_total"] == 1, "watchdog did not record the stall"
    # Duration is back-dated to when the loop actually went quiet, so it must
    # reflect the real 8 s block, not 8 s minus the detection threshold.
    assert snap["max_stall_seconds"] >= 7.5, snap
    assert "EVENT LOOP STALLED" in stderr
    assert "EVENT LOOP RECOVERED" in stderr


def test_python_lane_dump_names_the_blocking_frame(sleep_run):
    """The dump is only worth writing if it says WHAT was blocking."""
    _, outdir, _ = sleep_run
    dumps = list(outdir.glob("stall-*.txt"))
    assert len(dumps) == 1, f"expected exactly one incident dump, got {dumps}"
    text = dumps[0].read_text()
    assert "orb_test_blocking_sleep" in text, "dump does not name the blocking function"
    # ...and that it was blocking THE LOOP, not some worker thread.
    assert "asyncio/events.py" in text and "_run" in text
    # Header we control, so the next incident arrives pre-diagnosed.
    assert "beat_age_seconds" in text
    assert "THREADS IN THE DB LAYER AT CAPTURE TIME" in text
    assert "THREAD INVENTORY" in text
    assert "orb-loop-watchdog" in text


def test_status_json_is_readable_out_of_process(sleep_run):
    """The channel that survives a frozen loop: a file, written by the daemon
    thread, that an outbound heartbeat process can read without asking the API."""
    _, outdir, _ = sleep_run
    status = json.loads((outdir / "status.json").read_text())
    assert status["watchdog"] == "loop-starvation"
    assert status["stalls_total"] == 1
    assert status["last_dump_path"].endswith(".txt")
    assert status["written_unixtime"] > 0
    for key in ("beat_age_seconds", "stall_active", "max_stall_seconds",
                "deadman_file", "process_pid"):
        assert key in status, key


# ── Lane 1: GIL-free C-level dead-man timer ──────────────────────────────────
def test_gil_really_was_starved(gilhog_run):
    """Guard the guard: if the canary kept ticking, the scenario did not
    reproduce GIL starvation and the dead-man assertions below prove nothing."""
    summary, _, _ = gilhog_run
    assert summary["stall_wall_seconds"] > 5, summary
    assert summary["canary_ticks_per_second"] < 2.0, (
        "canary kept running — this was not a GIL blackout, so the test is "
        f"not exercising the hard case: {summary}"
    )


def test_deadman_lane_fires_without_the_gil(gilhog_run):
    """The most valuable behaviour in the module.

    faulthandler's timer is a raw pthread that dumps every thread's stack
    WITHOUT acquiring the GIL. The loop re-arms it; a frozen loop stops
    re-arming; the dump happens anyway. Two dumps ~4 s apart inside a ~10 s
    blackout is the signature — and it is the same "identical dumps N seconds
    apart" evidence that made the original incident conclusively a deadlock.
    """
    summary, outdir, stderr = gilhog_run
    logs = list(outdir.glob("deadman-*.log"))
    assert len(logs) == 1, f"expected one dead-man file, got {logs}"
    text = logs[0].read_text()
    assert text.count("Timeout (") >= 2, (
        "the dead-man timer did not fire repeatedly during the blackout:\n" + text
    )
    assert "orb_test_gil_hog" in text, "dead-man dump does not name the blocking frame"
    assert summary["snapshot"]["deadman_fired"] is True
    assert "DEAD-MAN TIMER FIRED" in stderr


def test_deadman_dump_shows_the_python_lane_was_itself_starved(gilhog_run):
    """Honesty check on the module's own docstring.

    Under a GIL blackout the Python watchdog thread is parked in its poll wait
    and contributes nothing. The dead-man dump captures that thread too, which
    is the direct evidence for the 'CANNOT catch' section of the docstring —
    and the reason Lane 1 has to exist at all.
    """
    _, outdir, _ = gilhog_run
    text = list(outdir.glob("deadman-*.log"))[0].read_text()
    assert "loop_watchdog.py" in text and "_monitor" in text


def test_deadman_survives_its_evidence_file_being_deleted(vanish_run):
    """Silent-failure guard, found the hard way during development.

    Deleting the dead-man file does not error — the C timer happily keeps
    writing into the unlinked inode, so the lane looks armed while its output
    goes nowhere. A log-rotation script or an operator clearing disk space is
    enough to cause it. The monitor thread must notice and re-create the file,
    and the timer must then dump into the NEW one.
    """
    summary, outdir, stderr = vanish_run
    assert summary["deadman_recreated"] is True, "file was not re-created"
    assert "had disappeared — re-created" in stderr
    logs = list(outdir.glob("deadman-*.log"))
    assert len(logs) == 1
    text = logs[0].read_text()
    assert text.count("Timeout (") >= 1, (
        "the re-created dead-man file received no dump — the lane was writing "
        "into a ghost inode:\n" + text
    )
    assert "orb_test_gil_hog" in text


# ── The non-negotiable ───────────────────────────────────────────────────────
def test_watchdog_never_takes_the_appliance_down(gilhog_run):
    """A watchdog that restarts a clinical system on a false positive is more
    dangerous than the bug. Both scenarios stalled hard; both processes exited 0
    of their own accord, and the source contains no kill/restart path."""
    summary, _, _ = gilhog_run
    assert summary["snapshot"]["stalls_total"] >= 1  # it definitely fired...

    # Asserted over the PARSED module, not the text: the docstring talks about
    # os.kill and kill -9 at length, and a grep-based check would either trip on
    # the prose or be quietly satisfied by deleting a comment.
    import ast

    tree = ast.parse((SERVER_DIR / "services" / "loop_watchdog.py").read_text())
    forbidden = {
        ("os", "kill"), ("os", "_exit"), ("os", "abort"),
        ("sys", "exit"), ("signal", "SIGKILL"), ("signal", "SIGTERM"),
    }
    dumps_later = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert (node.value.id, node.attr) not in forbidden, (
                f"watchdog must not be able to act: {node.value.id}.{node.attr}"
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            for n in names:
                assert not n.split(".")[0] in ("subprocess", "signal"), (
                    f"watchdog must not import an execution/signalling surface: {n}"
                )
        # Every faulthandler timer must be armed with exit=False. exit=True would
        # make this module kill the appliance on a stall — the one thing it must
        # never do.
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "dump_traceback_later":
                dumps_later += 1
                kw = {k.arg: k.value for k in node.keywords}
                assert "exit" in kw and kw["exit"].value is False, (
                    "dump_traceback_later must pass exit=False"
                )
    assert dumps_later == 1, "expected exactly one dead-man arming site"


def test_disabled_by_env_returns_none():
    """ORB_WATCHDOG_ENABLED=0 must be a real off switch, not a no-op."""
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(
            f"""
            import asyncio, sys
            sys.path.insert(0, {str(SERVER_DIR)!r})
            from services.loop_watchdog import start_watchdog, watchdog_status
            async def m():
                assert start_watchdog() is None
                assert watchdog_status() == {{"watchdog": "loop-starvation", "enabled": False}}
            asyncio.run(m()); print("OK")
            """
        )],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, ORB_WATCHDOG_ENABLED="0"), cwd=str(SERVER_DIR),
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
