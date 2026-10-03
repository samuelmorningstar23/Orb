"""Orb Hospital OS — event-loop starvation watchdog (out-of-band, observe-only).

WHY THIS EXISTS
───────────────
On 2026-08-01 the appliance froze PERMANENTLY in ~30 s under concurrent load on
/api/sage/ward-brief + /api/vigil/patients + /api/health/models during a
vigil.alert.* storm. Three `kill -USR1` faulthandler dumps taken 8 s apart were
byte-identical — a true deadlock, not slowness:

  MAIN (uvloop)  uv__run_timers -> PyGILState_Ensure -> take_gil -> cvwait
                 (the loop was starved of the GIL, not blocked in Orb code)
  THREAD A       event_bus._mark_delivered -> db_session.__exit__ -> conn.close()
                 -> sqlite3WalClose -> unixLock -> __psynch_mutexwait
  THREAD B       sage_narrator._build_ward_snapshot -> get_db -> openDatabase
                 -> unixOpen -> findReusableFd -> __psynch_mutexwait  (SAME mutex)
  THREAD C       downtime nightly FHIR export, json.dump -> _Py_write -> take_gil

The process sat at 0.2% CPU and needed `kill -9`. The trigger (multi-thread
open/close churn on a 213 MB SQLCipher DB) has since been removed by enabling
ORB_DB_POOL=1, and a 15-minute 60-bed soak passes every SLO in pool mode. But
the deadlock is not *proven* impossible, and the class of failure — "the loop
stops being scheduled at all" — is one no in-process HTTP check can catch.

  /api/health CANNOT detect this. It is SERVED BY the loop, so when the loop
  dies the check dies with it. A watchdog that shares the fate of the thing it
  watches is not a watchdog.

So this module watches the loop from OUTSIDE the loop, in two independent lanes,
and its only job is to make the next freeze arrive PRE-DIAGNOSED instead of
being discovered by a clinician at a bedside.

THE TWO LANES
─────────────
Lane 1 — DEAD-MAN TIMER (C level, does NOT need the GIL).  ★ the important one
    An asyncio task ON THE LOOP re-arms `faulthandler.dump_traceback_later()`
    every ORB_WATCHDOG_DEADMAN_REARM_S seconds. CPython's faulthandler timer is
    a raw pthread that waits on a lock and, on timeout, calls
    _Py_DumpTracebackThreads() directly — it never acquires the GIL and runs no
    Python bytecode. If the loop stops running it stops re-arming, the timer
    expires, and every thread's Python stack is written to a durable file even
    though the interpreter itself is wedged. This is the only lane that survives
    the exact failure captured above, where the main thread was blocked *inside*
    take_gil. `exit=False` always: it dumps, it never kills the process.
    With repeat=True the dump recurs every timeout while the freeze lasts, which
    is precisely the evidence that made the original incident conclusive
    (identical dumps N seconds apart ⇒ deadlock, not slowness).

Lane 2 — PYTHON DAEMON THREAD (richer evidence, needs the GIL).
    The loop stamps a monotonic timestamp every beat; a daemon thread compares
    it against wall time. On a stall it writes a timestamped incident file with
    a header we control: pid, stall duration, a full thread inventory, and an
    explicit note of WHICH THREADS ARE IN THE DB LAYER (the signature of the
    known incident). It also updates metrics gauges and a durable status.json,
    and logs CRITICAL. This lane produces the better artefact but can only run
    when the GIL is at least intermittently available.

WHAT THIS CAN AND CANNOT CATCH — stated plainly
───────────────────────────────────────────────
CAN catch:
  * The loop blocked in a long synchronous call (sync SQLite on the loop, a
    getproxies()/configd IPC hang, torch.load, a wedged subprocess wait). The
    GIL is released or handed round, so Lane 2 runs and produces a full report.
  * The loop starved of the GIL by other threads. Lane 2 may or may not get
    scheduled; Lane 1 fires regardless and names the blocking frames.
  * A loop that dies silently (task crash, uvloop wedge) — the beat simply stops.
  * A partial/dropping loop: `beat_age` is exported continuously, so degradation
    is visible as a trend before it is an outage.

CANNOT catch (be honest about these):
  * A hang below the interpreter, e.g. every thread stuck in an uninterruptible
    kernel wait or the whole process SIGSTOPped. Lane 1's pthread is scheduled by
    the OS, so it survives GIL starvation but not the OS refusing to run it.
  * SIGSTOP/SIGCONT or a laptop suspend looks exactly like a stall. That is a
    false positive we accept: it logs and dumps, it never acts.
  * Lane 2's evidence gathering (sys._current_frames, threading.enumerate, JSON
    writes) all require the GIL. Under total starvation Lane 2 contributes
    nothing and Lane 1 is the whole product.
  * Neither lane can page anybody by itself. It writes files, gauges and logs;
    something outside the process (the pilot's outbound heartbeat) has to read
    them. status.json is written by the DAEMON THREAD, never by the loop,
    precisely so it still moves while the loop is dead — and when even that
    stops, its stale mtime is itself the alarm.
  * A wedged process may also have wedged its DISK. Every write here is a plain
    stdlib write to an already-open fd or a small file; nothing touches the DB.
  * Lane 1's file can be deleted out from under it without any error — the C
    timer keeps writing into an unlinked inode, so the lane looks armed while
    its dumps go nowhere. The monitor thread re-creates a vanished file within
    one poll, but the gap until the loop's next re-arm is genuinely unguarded.

SAFETY — the non-negotiable
───────────────────────────
This module NEVER takes the appliance down. No auto-restart, no os.kill, no
faulthandler `exit=True`, no cancelling of application tasks. A watchdog that
restarts a clinical system on a false positive is more dangerous than the bug it
was written for. It observes, records and reports. Recovery is a human decision.

CONFIGURATION (all optional; defaults are the justified ones)
─────────────────────────────────────────────────────────────
  ORB_WATCHDOG_ENABLED          default on ("0" disables entirely)
  ORB_WATCHDOG_DIR              default <server>/ops/watchdog
  ORB_WATCHDOG_BEAT_S           1.0   loop stamp interval
  ORB_WATCHDOG_POLL_S           2.0   daemon-thread poll interval
  ORB_WATCHDOG_STALL_S          30.0  Lane 2 stall threshold  (justified below)
  ORB_WATCHDOG_DEADMAN_S        60.0  Lane 1 dead-man timeout (justified below)
  ORB_WATCHDOG_DEADMAN_REARM_S  15.0  Lane 1 re-arm cadence
  ORB_WATCHDOG_DEADMAN_REPEAT   1     keep dumping while frozen
  ORB_WATCHDOG_GRACE_S          30.0  ignore stalls this soon after start
  ORB_WATCHDOG_STATUS_WRITE_S   30.0  status.json refresh cadence

WHY 30 s AND 60 s — the thresholds, from measured behaviour
───────────────────────────────────────────────────────────
Upper bound on "normal": under the 16-worker repro load /api/health served
p50 22.5 ms, p95 40.3 ms, p99 68.6 ms, max 86.4 ms. So a healthy loop under
the worst load we can generate is late by <0.1 s.

Lower bound on "must not fire": legitimate long blocking work exists. Measured
on this appliance — the CfC + forecast torch warmup takes ~12 s of lazy first
load, the clinical-knowledge corpus embed and the nightly FHIR self-export
(283 × ~1 MB json.dump) are multi-second GIL-heavy jobs, and SQLite's own
busy_timeout is 30 s, so a contended writer can legitimately sit for ~30 s.

30 s is ~350× the observed p99 and above every measured legitimate stall except
a full busy_timeout expiry, which is itself worth a dump. A GC pause is 3 orders
of magnitude smaller; a "legitimate 2 s query" is 15× under the bar. The failure
being detected is PERMANENT, so nothing is lost by waiting: 30 s costs us
nothing and buys immunity to the entire normal-jitter distribution.

Lane 1 sits at 60 s = 2× Lane 2, deliberately. When the GIL is available Lane 2
wins the race and produces the annotated report; Lane 1 only fires when Lane 2
could not — which is exactly the hard case it exists for, and seeing which lane
fired is itself diagnostic. Re-arming costs ~16 µs (p50, measured; max 36 µs)
on the loop every 15 s, which is free.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("orb.watchdog")

# ── Frames that mean "this thread is in the DB layer" ────────────────────────
# The known deadlock had two threads wedged in SQLite's per-inode VFS mutex, one
# closing a connection and one opening one. Those C frames are invisible to a
# Python traceback, so we fingerprint the Python frames immediately above them.
_DB_FILE_MARKERS = (
    "database.py",
    "db_backend.py",
    "sqlite3",
    "sqlcipher",
    "pysqlcipher",
)
# Names that mean "DB work" even in a file we do not recognise (a service that
# imported run_db, a vendored driver). Deliberately specific — a bare "close" or
# "execute" would flag every file handle and every subprocess in the dump.
_DB_FUNC_MARKERS = (
    "db_session",
    "get_db",
    "run_db",
    "_open_raw_connection",
    "_build_connection",
    "_mark_delivered",
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _default_dir() -> Path:
    override = os.getenv("ORB_WATCHDOG_DIR", "").strip()
    if override:
        return Path(override)
    # server/ — resolved from this file so it does not depend on cwd, and does
    # NOT import config (config import has side effects: it probes Ollama).
    return Path(__file__).resolve().parent.parent / "ops" / "watchdog"


class LoopWatchdog:
    """Detects and records event-loop stalls. Observe-only; never acts."""

    def __init__(self) -> None:
        self.beat_s = _env_float("ORB_WATCHDOG_BEAT_S", 1.0)
        self.poll_s = _env_float("ORB_WATCHDOG_POLL_S", 2.0)
        self.stall_s = _env_float("ORB_WATCHDOG_STALL_S", 30.0)
        self.deadman_s = _env_float("ORB_WATCHDOG_DEADMAN_S", 60.0)
        self.deadman_rearm_s = _env_float("ORB_WATCHDOG_DEADMAN_REARM_S", 15.0)
        self.deadman_repeat = _env_flag("ORB_WATCHDOG_DEADMAN_REPEAT", True)
        self.grace_s = _env_float("ORB_WATCHDOG_GRACE_S", 30.0)
        self.status_write_s = _env_float("ORB_WATCHDOG_STATUS_WRITE_S", 30.0)

        # Misconfiguration guards. A dead-man timeout that is not comfortably
        # longer than the re-arm cadence would fire on a HEALTHY loop, and a
        # watchdog that cries wolf gets switched off — which is how a real freeze
        # goes unnoticed. Clamp rather than refuse to start: a degraded watchdog
        # still beats none, and the clamp is logged.
        if self.deadman_s > 0 and self.deadman_rearm_s > self.deadman_s / 2.0:
            clamped = max(1.0, self.deadman_s / 4.0)
            logger.warning(
                "Watchdog re-arm interval %.1fs is too close to the dead-man "
                "timeout %.1fs (would fire on a healthy loop) — clamping to %.1fs",
                self.deadman_rearm_s, self.deadman_s, clamped,
            )
            self.deadman_rearm_s = clamped
        if self.stall_s <= self.beat_s * 3:
            logger.warning(
                "Watchdog stall threshold %.1fs is within noise of the beat "
                "interval %.1fs — expect false positives", self.stall_s, self.beat_s,
            )

        self.dir = _default_dir()
        self.status_path = self.dir / "status.json"
        self.deadman_path: Optional[Path] = None

        # `_last_beat` is written ONLY by the loop and read ONLY by the daemon
        # thread. A float store/load is atomic under the GIL, so no lock: taking
        # a lock on the loop is exactly the kind of coupling this must avoid.
        self._last_beat = time.monotonic()
        self._beat_count = 0
        self._started_monotonic = time.monotonic()
        self._started_wall = time.time()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._beat_task: Optional[asyncio.Task] = None
        self._deadman_file = None  # kept open for the C timer's fd
        self._deadman_bytes = 0
        self._retired_files: list = []  # superseded handles, see _ensure_deadman_file

        # Incident bookkeeping (daemon thread owns these).
        self.stall_active = False
        self.stalls_total = 0
        self.max_stall_s = 0.0
        self.last_stall_wall: Optional[float] = None
        self.last_stall_duration_s: Optional[float] = None
        self.last_dump_path: Optional[str] = None
        self.deadman_fired = False
        self.last_error: Optional[str] = None

    # ── Loop side ───────────────────────────────────────────────────────────
    def beat(self) -> None:
        """Stamp liveness. Called ONLY from the event loop thread."""
        self._last_beat = time.monotonic()
        self._beat_count += 1

    def _arm_deadman(self) -> None:
        """(Re-)arm the C-level dead-man timer. Called ONLY from the loop.

        Must run on the loop: it is the *loop's* failure to re-arm that trips the
        timer. Doing this from a helper thread would make a frozen loop look
        healthy — the classic broken-watchdog mistake.

        Cost on the loop: 16 µs p50 / 36 µs max (measured, 200 samples) — the
        call cancels the previous timer thread and starts a new one. The one case
        where it is slower is a re-arm that lands while the old timer is mid-dump,
        where it waits out that write; that only happens during a stall we are
        already recording.
        """
        if self._deadman_file is None or self.deadman_s <= 0:
            return
        try:
            faulthandler.dump_traceback_later(
                self.deadman_s,
                repeat=self.deadman_repeat,
                file=self._deadman_file,
                exit=False,  # NEVER kill the appliance. Dump and live.
            )
        except Exception as exc:  # pragma: no cover - platform edge
            self.last_error = f"deadman arm failed: {exc}"
            logger.warning("Watchdog dead-man timer could not be armed: %s", exc)

    async def _beat_loop(self) -> None:
        """Heartbeat task: stamps the clock and re-arms the dead-man timer."""
        next_rearm = 0.0
        try:
            while True:
                self.beat()
                now = time.monotonic()
                if now >= next_rearm:
                    self._arm_deadman()
                    next_rearm = now + self.deadman_rearm_s
                await asyncio.sleep(self.beat_s)
        except asyncio.CancelledError:
            raise
        finally:
            # Shutting down cleanly is not a stall — disarm so a slow, orderly
            # shutdown never writes a bogus dump.
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                pass

    # ── Daemon-thread side ──────────────────────────────────────────────────
    def _monitor(self) -> None:
        last_status_write = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                age = now - self._last_beat
                since_start = now - self._started_monotonic

                self._publish_gauges(age)
                self._ensure_deadman_file()
                self._check_deadman_growth()

                if age >= self.stall_s and since_start >= self.grace_s:
                    if not self.stall_active:
                        self._on_stall_start(age)
                elif self.stall_active and age < self.stall_s:
                    self._on_recovery(age)

                if (now - last_status_write) >= self.status_write_s:
                    self._write_status(age)
                    last_status_write = now
            except Exception as exc:  # never let the watchdog die of its own bug
                self.last_error = repr(exc)
                logger.exception("Loop watchdog monitor iteration failed")
            self._stop.wait(self.poll_s)

    def _on_stall_start(self, age: float) -> None:
        self.stall_active = True
        self.stalls_total += 1
        # Back-date to when the loop ACTUALLY went quiet, not when we noticed:
        # reporting from detection time silently understates every stall by the
        # threshold, which is exactly the number an operator will quote later.
        self.last_stall_wall = time.time() - age
        dump_path = None
        try:
            dump_path = self._write_incident_dump(age)
        except Exception as exc:
            self.last_error = f"dump failed: {exc!r}"
            logger.exception("Loop watchdog failed to write incident dump")
        self.last_dump_path = str(dump_path) if dump_path else None
        logger.critical(
            "EVENT LOOP STALLED: no heartbeat for %.1fs (threshold %.0fs). "
            "The API is not serving requests. Thread dump: %s . "
            "Orb has NOT restarted anything — this is an observation only.",
            age, self.stall_s, self.last_dump_path or "<unavailable>",
        )
        self._write_status(age)

    def _on_recovery(self, age: float) -> None:
        stalled_for = (time.time() - self.last_stall_wall) if self.last_stall_wall else None
        self.stall_active = False
        self.last_stall_duration_s = stalled_for
        if stalled_for and stalled_for > self.max_stall_s:
            self.max_stall_s = stalled_for
        logger.critical(
            "EVENT LOOP RECOVERED after ~%.1fs stalled (beat age now %.2fs). "
            "Evidence retained at %s",
            stalled_for or -1.0, age, self.last_dump_path or "<none>",
        )
        self._write_status(age)

    # ── Evidence ────────────────────────────────────────────────────────────
    def _thread_inventory(self) -> tuple[list[dict], list[str]]:
        """Per-thread top frames + the names of threads sitting in the DB layer.

        Uses only sys._current_frames()/threading.enumerate(); touches nothing
        that could itself block (emphatically not the database — the database is
        a prime suspect in the failure we are recording).
        """
        names = {t.ident: t.name for t in threading.enumerate()}
        daemons = {t.ident: t.daemon for t in threading.enumerate()}
        inventory: list[dict] = []
        db_threads: list[str] = []
        for tid, frame in sys._current_frames().items():
            stack: list[str] = []
            in_db = False
            f = frame
            depth = 0
            while f is not None and depth < 40:
                code = f.f_code
                fname = code.co_filename
                stack.append(f"{fname}:{f.f_lineno} in {code.co_name}")
                lowered = fname.lower()
                if any(m in lowered for m in _DB_FILE_MARKERS):
                    in_db = True
                elif code.co_name in _DB_FUNC_MARKERS:
                    in_db = True
                f = f.f_back
                depth += 1
            label = names.get(tid, "?")
            entry = {
                "thread_id": tid,
                "name": label,
                "daemon": daemons.get(tid),
                "in_db_layer": in_db,
                "top_frame": stack[0] if stack else None,
                "stack_head": stack[:6],
            }
            inventory.append(entry)
            if in_db:
                db_threads.append(f"{label} (tid={tid})")
        return inventory, db_threads

    def _write_incident_dump(self, age: float) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"stall-{_utc_stamp()}-pid{os.getpid()}.txt"
        inventory, db_threads = self._thread_inventory()
        header = [
            "═" * 78,
            "ORB EVENT-LOOP STALL: automatic capture (observe-only, nothing was restarted)",
            "═" * 78,
            f"captured_utc      : {datetime.now(timezone.utc).isoformat()}",
            f"pid               : {os.getpid()}",
            f"beat_age_seconds  : {age:.2f}",
            f"threshold_seconds : {self.stall_s:.1f}",
            f"process_uptime_s  : {time.monotonic() - self._started_monotonic:.1f}",
            f"beats_recorded    : {self._beat_count}",
            f"stall_number      : {self.stalls_total}",
            f"ORB_DB_POOL       : {os.getenv('ORB_DB_POOL', '<unset>')}",
            f"python            : {sys.version.split()[0]}",
            "",
            "THREADS IN THE DB LAYER AT CAPTURE TIME "
            "(the 2026-08-01 deadlock had two, one closing a connection, one opening one):",
        ]
        if db_threads:
            header += [f"  ⚠ {t}" for t in db_threads]
        else:
            header.append("  (none: the stall is NOT in the SQLite open/close path)")
        header += ["", "THREAD INVENTORY (top frame per thread):"]
        for entry in sorted(inventory, key=lambda e: (not e["in_db_layer"], str(e["name"]))):
            flag = "DB" if entry["in_db_layer"] else "  "
            header.append(
                f"  [{flag}] {entry['name']} (tid={entry['thread_id']},"
                f" daemon={entry['daemon']}) :: {entry['top_frame']}"
            )
        header += [
            "",
            "═" * 78,
            "FULL PYTHON STACKS FOR EVERY THREAD (faulthandler.dump_traceback):",
            "═" * 78,
            "",
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(header) + "\n")
            fh.flush()
            # Same primitive as `kill -USR1`, written straight to this fd.
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.flush()
            fh.write(
                "\n"
                + "═" * 78
                + "\nNOTE: Python stacks only. If a thread's top frame is a C call "
                  "(conn.close(),\njson.dump, take_gil) run `sample <pid> 3` for C "
                  "frames while the process is\nstill alive. Do NOT kill -9 before "
                  "sampling. The C stack is the other half of\nthe 2026-08-01 "
                  "diagnosis.\n"
            )
        return path

    def _deadman_header(self) -> str:
        return (
            f"# Orb dead-man traceback lane, pid {os.getpid()} armed "
            f"{datetime.now(timezone.utc).isoformat()} "
            f"(timeout {self.deadman_s:.0f}s, repeat={self.deadman_repeat}).\n"
            f"# Anything below this line was written by CPython's faulthandler "
            f"timer thread WITHOUT the GIL,\n# because the event loop stopped "
            f"re-arming it. That is a frozen appliance. Nothing was restarted.\n"
        )

    def _ensure_deadman_file(self) -> None:
        """Re-create the dead-man file if it has vanished underneath us.

        Deleting the file does NOT fail the C timer — it goes on writing into an
        unlinked inode, so the next freeze would dump its evidence into nowhere
        and nobody would ever know the lane was dead. Log rotation, a tidy-up
        script or an operator clearing disk space is enough to cause it, and this
        module found out the hard way during its own development.

        Checked from the monitor thread; the loop's next re-arm (≤ re-arm
        interval later) binds the C timer to the new handle.
        """
        if self.deadman_path is None or self.deadman_s <= 0:
            return
        try:
            if self.deadman_path.exists():
                return
        except OSError:
            return
        try:
            new = open(self.deadman_path, "a", buffering=1, encoding="utf-8")
            new.write(self._deadman_header())
            # Deliberately NOT closing the old handle: faulthandler's C timer is
            # still holding its raw fd and may be mid-dump. Closing it would turn
            # an in-flight dump into an EBADF and lose the very evidence we are
            # here to keep. Park a bounded number instead.
            if self._deadman_file is not None:
                self._retired_files.append(self._deadman_file)
                del self._retired_files[:-3]
            self._deadman_file = new
            self._deadman_bytes = self.deadman_path.stat().st_size
            logger.warning(
                "Watchdog dead-man file %s had disappeared — re-created. The "
                "GIL-free lane was writing to an unlinked file and would have "
                "lost a freeze dump.", self.deadman_path,
            )
        except Exception as exc:
            self.last_error = f"deadman re-open failed: {exc!r}"

    def _check_deadman_growth(self) -> None:
        """Notice that the C-level lane fired (it cannot tell us itself).

        The faulthandler timer thread runs without the GIL and has no way to call
        back into Python. Watching the size of its output file is how a dump it
        wrote during a GIL blackout becomes visible once Python runs again.
        """
        if self.deadman_path is None:
            return
        try:
            size = self.deadman_path.stat().st_size
        except OSError:
            return
        if size > self._deadman_bytes:
            grew = size - self._deadman_bytes
            self._deadman_bytes = size
            self.deadman_fired = True
            try:
                from services.metrics_service import set_gauge
                set_gauge(
                    "orb_loop_deadman_dump_bytes", size,
                    help_text="Bytes written by the GIL-free dead-man thread dump file.",
                )
            except Exception:
                pass
            logger.critical(
                "DEAD-MAN TIMER FIRED: the event loop failed to re-arm within %.0fs, "
                "so the GIL-free faulthandler thread dumped %d bytes of stacks to %s. "
                "This is the hard case — the interpreter was too wedged for the "
                "Python watchdog to run. Read that file first.",
                self.deadman_s, grew, self.deadman_path,
            )

    # ── Surfacing ───────────────────────────────────────────────────────────
    def _publish_gauges(self, age: float) -> None:
        """Push state onto the existing Prometheus surface (services/metrics_service).

        Reusing /metrics rather than inventing a channel — but note the honest
        limit: /metrics is served BY the loop, so during a stall the scrape
        simply fails. That scrape failure is itself the page; these gauges are
        what tells the operator what happened once it answers again.
        """
        try:
            from services.metrics_service import set_gauge
        except Exception:
            return
        set_gauge("orb_loop_watchdog_running", 1,
                  help_text="1 when the event-loop stall watchdog thread is alive.")
        set_gauge("orb_loop_beat_age_seconds", age,
                  help_text="Seconds since the event loop last stamped its heartbeat.")
        set_gauge("orb_loop_stall_active", 1 if self.stall_active else 0,
                  help_text="1 while the event loop is stalled beyond the watchdog threshold.")
        set_gauge("orb_loop_stalls_total", self.stalls_total,
                  help_text="Number of event-loop stalls observed since process start.")
        set_gauge("orb_loop_stall_seconds_max", self.max_stall_s,
                  help_text="Longest observed event-loop stall in seconds.")
        set_gauge("orb_loop_last_stall_unixtime", self.last_stall_wall or 0,
                  help_text="Unix time of the most recent event-loop stall (0 = never).")
        set_gauge("orb_loop_deadman_armed", 1 if self._deadman_file is not None else 0,
                  help_text="1 when the GIL-free dead-man traceback timer is armed.")

    def snapshot(self, age: Optional[float] = None) -> dict[str, Any]:
        """Current watchdog state as a plain dict (safe to call from anywhere)."""
        if age is None:
            age = time.monotonic() - self._last_beat
        return {
            "watchdog": "loop-starvation",
            "healthy": (not self.stall_active) and age < self.stall_s,
            "stall_active": self.stall_active,
            "beat_age_seconds": round(age, 3),
            "beat_count": self._beat_count,
            "stall_threshold_seconds": self.stall_s,
            "deadman_timeout_seconds": self.deadman_s,
            "deadman_file": str(self.deadman_path) if self.deadman_path else None,
            "deadman_fired": self.deadman_fired,
            "deadman_bytes": self._deadman_bytes,
            "stalls_total": self.stalls_total,
            "max_stall_seconds": round(self.max_stall_s, 2),
            "last_stall_unixtime": self.last_stall_wall,
            "last_stall_duration_seconds": self.last_stall_duration_s,
            "last_dump_path": self.last_dump_path,
            "process_pid": os.getpid(),
            "process_started_unixtime": self._started_wall,
            "db_pool_enabled": os.getenv("ORB_DB_POOL", "") or None,
            "last_error": self.last_error,
            # Written by the daemon thread, never by the loop — so a stale
            # `written_unixtime` here means even the watchdog thread is starved,
            # which is itself the alarm.
            "written_unixtime": time.time(),
            "written_utc": datetime.now(timezone.utc).isoformat(),
        }

    def _write_status(self, age: Optional[float] = None) -> None:
        """Atomically refresh status.json — the out-of-process alarm surface.

        The pilot's outbound heartbeat runs as a separate process; it can read
        this file even while the API is frozen, which is the whole point. Written
        atomically (tmp + os.replace) so a reader never sees a half file, and by
        the DAEMON THREAD only.
        """
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            payload = self.snapshot(age)
            tmp = self.status_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.status_path)
        except Exception as exc:
            self.last_error = f"status write failed: {exc!r}"

    # ── Lifecycle ───────────────────────────────────────────────────────────
    def _prune(self, glob: str, keep: int) -> None:
        """Keep only the newest `keep` evidence files matching `glob`.

        Bounded, never aggressive: stall dumps are the whole point of this
        module, so the retention is generous and only trims what a long-running
        or crash-looping appliance would otherwise pile up forever.
        """
        try:
            files = sorted(
                self.dir.glob(glob), key=lambda p: p.stat().st_mtime, reverse=True
            )
            for stale in files[keep:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
        except Exception:
            pass

    def start(self) -> asyncio.Task:
        """Open the dead-man file, start the daemon thread, return the beat task.

        Must be called from inside the running event loop (the beat task has to
        belong to the loop being watched).
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        self._prune("deadman-*.log", keep=20)
        self._prune("stall-*.txt", keep=100)
        self.deadman_path = (
            self.dir / f"deadman-pid{os.getpid()}-{_utc_stamp()}.log"
        )
        try:
            # Line-buffered append. faulthandler writes to the raw fd, so this
            # object exists to own the fd for the process lifetime — closing it
            # would leave the C timer writing to a dead descriptor.
            self._deadman_file = open(self.deadman_path, "a", buffering=1, encoding="utf-8")
            self._deadman_file.write(self._deadman_header())
            self._deadman_bytes = self.deadman_path.stat().st_size
        except Exception as exc:
            self._deadman_file = None
            self.last_error = f"deadman file open failed: {exc!r}"
            logger.warning("Watchdog dead-man file unavailable: %s", exc)

        self._started_monotonic = time.monotonic()
        self._started_wall = time.time()
        self._last_beat = time.monotonic()
        self._stop.clear()

        self._thread = threading.Thread(
            target=self._monitor,
            name="orb-loop-watchdog",
            daemon=True,  # must never hold up interpreter shutdown
        )
        self._thread.start()

        self._beat_task = asyncio.get_running_loop().create_task(self._beat_loop())
        # status.json is written by the monitor thread's first iteration — never
        # from the loop, so the file's freshness stays an honest signal about a
        # thread that is independent of the loop.
        logger.info(
            "Loop watchdog armed — stall threshold %.0fs (python lane), dead-man %.0fs "
            "(GIL-free lane, re-armed every %.0fs). Evidence dir: %s",
            self.stall_s, self.deadman_s, self.deadman_rearm_s, self.dir,
        )
        return self._beat_task

    def stop(self) -> None:
        self._stop.set()
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        if self._beat_task is not None and not self._beat_task.done():
            self._beat_task.cancel()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_s + 1.0)
        for handle in [self._deadman_file, *self._retired_files]:
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:
                pass
        self._deadman_file = None
        self._retired_files.clear()
        # A dead-man file that only ever held its own header is noise. Removing
        # it on a clean shutdown means the presence of one of these files is
        # itself meaningful: either a process is running, or it froze.
        if self.deadman_path is not None and not self.deadman_fired:
            try:
                if self.deadman_path.stat().st_size <= self._deadman_bytes:
                    self.deadman_path.unlink()
            except OSError:
                pass


# ── Module-level singleton + helpers used by main.py ─────────────────────────
_watchdog: Optional[LoopWatchdog] = None
_start_lock = threading.Lock()


def start_watchdog() -> Optional[asyncio.Task]:
    """Start the watchdog and return its loop-side beat task (or None if off).

    Call from the FastAPI lifespan AFTER the synchronous startup work (init_db,
    the terminology/interaction seeds) so a slow first boot cannot be mistaken
    for a stall. Append the returned task to the lifespan's task list so
    shutdown cancels it deterministically.
    """
    global _watchdog
    if not _env_flag("ORB_WATCHDOG_ENABLED", True):
        logger.info("Loop watchdog disabled (ORB_WATCHDOG_ENABLED=0)")
        return None
    with _start_lock:
        if _watchdog is not None:
            return _watchdog._beat_task
        wd = LoopWatchdog()
        task = wd.start()
        _watchdog = wd
        return task


def stop_watchdog() -> None:
    global _watchdog
    with _start_lock:
        if _watchdog is None:
            return
        _watchdog.stop()
        _watchdog = None


def get_watchdog() -> Optional[LoopWatchdog]:
    return _watchdog


def watchdog_status() -> dict[str, Any]:
    """Read-only status, for an admin/health surface. Never raises."""
    wd = _watchdog
    if wd is None:
        return {"watchdog": "loop-starvation", "enabled": False}
    try:
        snap = wd.snapshot()
        snap["enabled"] = True
        return snap
    except Exception as exc:  # pragma: no cover
        return {"watchdog": "loop-starvation", "enabled": True, "error": repr(exc)}
