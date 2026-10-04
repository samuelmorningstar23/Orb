"""The knowledge worker must not outlive the process that owns its pool.

On 2026-10-04 thirty-five orphaned workers (``python ... --multiprocessing-fork``,
PPID 1) held 62 GB between them and exhausted swap. Each was a pool worker whose
owner (a dev-reload app process, or a killed pytest run) had died while the
worker was still bootstrapping: the old parent-watchdog captured os.getppid()
at initializer time, saw 1 already, and never fired. These tests pin the two
properties that close that class:

  * a worker exits within seconds of its owner's death, whether the owner was
    SIGKILLed after the worker was ready or while it was still bootstrapping;
  * _shutdown_pool() really terminates a busy worker (the old terminate loop
    ran after ProcessPoolExecutor.shutdown() had nulled _processes, so it was
    dead code).

The SIGKILL cases drive a throwaway owner subprocess so the test can kill it.
Every pid checked is one the owner itself reported, never a machine-wide scan,
so processes from other runs on the machine cannot make these flaky.
"""
import os
import select
import signal
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The owner: builds the real pool, reports its worker pid, then idles until
# the test kills it. Re-executed as __mp_main__ by the spawn child, hence the
# sys.path line at the top and the __main__ guard.
OWNER_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, {server_dir!r})
    os.environ.pop("ORB_KNOWLEDGE_INPROCESS", None)
    from services import knowledge_worker as kw

    if __name__ == "__main__":
        pool = kw._ensure_pool()
        fut = pool.submit(os.getpid)
        print("spawned", *sorted(pool._processes), flush=True)
        if sys.argv[1] == "ready":
            print("worker", fut.result(timeout=120), flush=True)
        time.sleep(120)
    """
)


def _alive(pid: int) -> bool:
    """True while `pid` exists and is not a zombie."""
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _wait_gone(pid: int, timeout: float) -> float | None:
    """Seconds until `pid` disappeared, or None if it survived `timeout`."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if not _alive(pid):
            return time.monotonic() - start
        time.sleep(0.2)
    return None


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _read_line(proc: subprocess.Popen, prefix: str, timeout: float) -> list[str]:
    """Next stdout line starting with `prefix`, split into fields."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"owner never printed {prefix!r} within {timeout}s"
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        line = proc.stdout.readline()
        assert line, f"owner exited before printing {prefix!r}"
        if line.startswith(prefix):
            return line.split()


@pytest.fixture
def owner(tmp_path):
    """Start the owner in `mode` and yield (proc, worker_pid); reap both."""
    started: list = []

    def _start(mode: str):
        script = tmp_path / "pool_owner.py"
        script.write_text(OWNER_SCRIPT.format(server_dir=SERVER_DIR))
        env = dict(os.environ)
        env.pop("ORB_KNOWLEDGE_INPROCESS", None)
        proc = subprocess.Popen(
            [sys.executable, str(script), mode],
            stdout=subprocess.PIPE,
            stderr=open(tmp_path / "owner.err", "w"),
            text=True,
            env=env,
            cwd=SERVER_DIR,
        )
        fields = _read_line(proc, "spawned", timeout=60)
        assert len(fields) == 2, f"expected one worker, got {fields}"
        worker_pid = int(fields[1])
        if mode == "ready":
            assert _read_line(proc, "worker", timeout=150) == ["worker", str(worker_pid)]
        started.append((proc, worker_pid))
        return proc, worker_pid

    yield _start
    for proc, worker_pid in started:
        _kill_quietly(proc.pid)
        proc.wait(timeout=10)
        _kill_quietly(worker_pid)


def test_worker_exits_when_ready_owner_is_sigkilled(owner):
    """Steady state: the worker is initialised and idle, the owner is
    SIGKILLed (no atexit, no lifespan teardown). The worker's sentinel reads
    EOF and it exits on its own."""
    proc, worker_pid = owner("ready")
    assert _alive(worker_pid)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    gone_after = _wait_gone(worker_pid, timeout=10.0)
    assert gone_after is not None, (
        f"worker {worker_pid} outlived its SIGKILLed owner by more than 10 s"
    )


def test_worker_exits_when_owner_dies_during_bootstrap(owner):
    """The dev-reload shape: the owner dies 1-5 s after spawning the worker,
    while the worker is still importing. The old watchdog captured ppid 1 at
    that point and never fired; now the child finds its owner already gone
    the moment its initializer runs."""
    proc, worker_pid = owner("early")
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    gone_after = _wait_gone(worker_pid, timeout=30.0)
    assert gone_after is not None, (
        f"worker {worker_pid}, orphaned during bootstrap, survived 30 s"
    )


@pytest.mark.real_knowledge_pool
def test_shutdown_pool_terminates_a_busy_worker():
    """Orderly shutdown (lifespan teardown, atexit) with a job in flight must
    reap the worker: the sentinel the executor sends would sit unread behind
    the running job, and the old terminate loop was dead code."""
    from services import knowledge_worker as kw

    kw._shutdown_pool()  # a clean slate whatever an earlier opt-in test left
    pool = kw._ensure_pool()
    worker_pid = None
    try:
        pool.submit(time.sleep, 120)  # the job that would block the sentinel
        pids = list(pool._processes)
        assert len(pids) == 1, pids
        worker_pid = pids[0]
        assert _alive(worker_pid)

        started = time.monotonic()
        kw._shutdown_pool()
        took = time.monotonic() - started

        assert kw._pool is None
        assert _wait_gone(worker_pid, timeout=5.0) is not None, (
            f"_shutdown_pool left worker {worker_pid} alive"
        )
        assert took < kw._SHUTDOWN_GRACE_S + 5.0, f"shutdown blocked for {took:.1f}s"
    finally:
        kw._shutdown_pool()
        if worker_pid is not None:
            _kill_quietly(worker_pid)
