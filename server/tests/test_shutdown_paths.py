"""Shutdown must always FINISH.

The defect these tests exist for: the server sat at "Waiting for application
shutdown." forever. The reloader was blocked in `process.join()` while still
holding the listen socket, so every HTTP request — /api/health included — hung
with no error at all, while the worker it was waiting for kept running the
vitals simulator and the alert bot and writing to the encrypted database. A
previously orphaned worker (PPID 1) had been left behind the same way.

Two mechanisms produced it, and both are asserted here:

  1. **A swallowed `CancelledError` makes a task uncancellable.** In 3.12 it is
     a `BaseException`, so `except Exception` misses it — but
     `except (TimeoutError, CancelledError): return` catches it and turns
     "stop" into "carry on". Whoever awaits that task then waits for ever.

  2. **An unbounded await on a task.** `await task` after `task.cancel()` is
     only as bounded as the task's willingness to die. Every stop path needs a
     deadline and the nerve to abandon a task that ignores it.

This matters beyond the dev reloader: the appliance restarts for upgrades,
power events and launchd. A shutdown that can hang forever is an appliance that
does not come back up — and an orphaned worker still writing to the DB while a
new one starts is the multi-writer hazard this repo has already been burned by.

Timing note: every assertion here waits on a wrapper task with
`asyncio.wait(..., timeout=...)` rather than `asyncio.wait_for`. If a
regression reintroduces the hang, the test must FAIL, not hang — and `wait_for`
would itself block, because its timeout works by cancelling the awaiting task,
which just forwards the cancel into the task that is refusing to die.
"""
import asyncio
import logging
import time

import pytest

from services.batch_coordinator import MicroBatchCoordinator
from services.gpu_coordinator import PRIORITY_BACKGROUND
import services.llm_queue_service as q
from services.vitals_source.base import STOP_TIMEOUT_SECONDS, stop_task
from services.vitals_source.simulator_source import SimulatorSource

# Short enough to keep the suite quick, long enough that a machine under load
# does not mistake a slow scheduler for a refusal to die.
BOUND = 0.3
# How long we give a bounded stop to come back before calling it a hang.
SLACK = 2.0


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _immortal(release: asyncio.Event, swallow_only: int = -1):
    """A task body that ignores cancellation, exactly like the real chain did.

    `swallow_only=n` swallows the first n cancels and then behaves; the default
    (-1) swallows every one. `release` is the test's own way out — a task that
    truly could not be stopped would outlive the event loop and spin.
    """
    async def body():
        swallowed = 0
        while not release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                swallowed += 1
                if swallow_only >= 0 and swallowed > swallow_only:
                    raise
                continue      # ← THE defect: a cancel that goes nowhere
        return "released"
    return body()


async def _finish(task: asyncio.Task, release: asyncio.Event) -> None:
    """Let an abandoned immortal task go, so it cannot outlive its test."""
    release.set()
    if not task.done():
        await asyncio.wait({task}, timeout=1.0)


async def _completes_within(coro, seconds: float) -> asyncio.Task:
    """Run `coro` as a task and assert it RETURNS within `seconds`."""
    task = asyncio.ensure_future(coro)
    done, _pending = await asyncio.wait({task}, timeout=seconds)
    assert task in done, (
        f"call did not return within {seconds}s — shutdown is blocked, which is "
        "the whole defect"
    )
    return task


# ══════════════════════════════════════════════════════════════════════════════
#  stop_task — the bounded-wait contract every stop path now uses
# ══════════════════════════════════════════════════════════════════════════════

class TestBoundedStopContract:
    async def test_a_task_that_refuses_to_die_is_abandoned_within_the_bound(self, caplog):
        release = asyncio.Event()
        task = asyncio.create_task(_immortal(release), name="wont-die")
        try:
            with caplog.at_level(logging.ERROR):
                t0 = time.monotonic()
                stopper = await _completes_within(
                    stop_task(task, label="test", timeout=BOUND), BOUND + SLACK)
                elapsed = time.monotonic() - t0

            assert stopper.result() is False, "an abandoned task must be reported, not hidden"
            assert elapsed < BOUND + SLACK
            assert any("ABANDONED" in r.getMessage() for r in caplog.records), (
                "abandoning a task is a real fault and must be logged loudly")
            assert not task.done(), "the task genuinely ignored the cancel (fixture check)"
        finally:
            await _finish(task, release)

    async def test_a_cooperative_task_is_actually_awaited(self):
        async def sleeper():
            await asyncio.sleep(30)

        task = asyncio.create_task(sleeper(), name="cooperative")
        stopper = await _completes_within(
            stop_task(task, label="test", timeout=BOUND), BOUND + SLACK)
        assert stopper.result() is True
        assert task.cancelled()

    async def test_a_task_that_swallows_the_first_cancel_still_dies(self):
        """The second cancel lands at a different await. Cheap, and it rescues
        exactly the shape that caused the hang."""
        release = asyncio.Event()
        task = asyncio.create_task(_immortal(release, swallow_only=1), name="stubborn")
        try:
            stopper = await _completes_within(
                stop_task(task, label="test", timeout=BOUND), BOUND + SLACK)
            assert stopper.result() is True
            assert task.done()
        finally:
            await _finish(task, release)

    async def test_our_own_cancellation_is_re_raised_never_swallowed(self):
        """If the shutdown that called us is itself cancelled, `stop_task` must
        propagate — otherwise it becomes the next uncancellable thing."""
        release = asyncio.Event()
        victim = asyncio.create_task(_immortal(release), name="wont-die")
        try:
            stopper = asyncio.ensure_future(
                stop_task(victim, label="test", timeout=30.0))
            await asyncio.sleep(0.05)          # let it reach the bounded wait
            stopper.cancel()
            done, _ = await asyncio.wait({stopper}, timeout=SLACK)
            assert stopper in done, "stop_task ignored its own cancellation"
            assert stopper.cancelled(), (
                "CancelledError aimed at us must propagate, not become a return value")
        finally:
            await _finish(victim, release)

    async def test_a_task_that_died_of_an_error_is_reaped_not_re_raised(self, caplog):
        async def explodes():
            raise RuntimeError("bot exploded")

        task = asyncio.create_task(explodes(), name="exploder")
        await asyncio.sleep(0.01)
        with caplog.at_level(logging.WARNING):
            assert await stop_task(task, label="test", timeout=BOUND) is True
        assert any("bot exploded" in r.getMessage() for r in caplog.records)

    async def test_the_default_bound_is_finite(self):
        assert 0 < STOP_TIMEOUT_SECONDS < 60


# ══════════════════════════════════════════════════════════════════════════════
#  SimulatorSource.stop — the loop that kept writing vitals through shutdown
# ══════════════════════════════════════════════════════════════════════════════

class TestSimulatorSourceStop:
    async def test_stop_returns_even_when_the_tick_will_not_cancel(self, caplog):
        """The observed appliance defect, end to end: the tick swallows the
        cancel (as the ambient/LLM chain below it did), so the loop never dies —
        and stop() must STILL come back."""
        release = asyncio.Event()
        entered = asyncio.Event()

        async def wedged_tick():
            entered.set()
            while not release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue                      # swallowed, as downstream did

        source = SimulatorSource(tick=wedged_tick, interval_seconds=0.01,
                                 stop_timeout=BOUND)
        await source.start()
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        loop_task = source._task
        try:
            with caplog.at_level(logging.ERROR):
                stopper = await _completes_within(source.stop(), BOUND + SLACK)
            stopper.result()                      # re-raise anything it hid

            assert source.running is False
            assert any("ABANDONED" in r.getMessage() for r in caplog.records)
            assert "abandoned" in (source.stats.last_error or "").lower(), (
                "an abandoned writer must show on the admin surface, not only in logs")
        finally:
            await _finish(loop_task, release)

    async def test_a_tick_that_swallows_every_cancel_still_ends_the_loop(self):
        """The common real shape — a helper below the tick catches
        CancelledError and returns — must not keep the loop alive.

        Every cancel this loop is sent is eaten inside the tick, so cancellation
        alone can never stop it. `_loop` therefore re-checks `_running` before
        it sleeps: an exit that does not depend on cancellation working all the
        way down. Without it the loop survives stop(), gets ABANDONED, and keeps
        writing synthetic vitals into the DB of a process that is on its way
        out — the orphaned-writer half of the observed defect.
        """
        ticks = {"n": 0}

        async def swallowing_tick():
            ticks["n"] += 1
            try:
                await asyncio.sleep(0.5)          # where the cancel will land
            except asyncio.CancelledError:
                return                            # ← swallowed, then returns

        source = SimulatorSource(tick=swallowing_tick, interval_seconds=0.0,
                                 stop_timeout=BOUND)
        await source.start()
        while ticks["n"] == 0:
            await asyncio.sleep(0.01)
        loop_task = source._task

        stopper = await _completes_within(source.stop(), BOUND + SLACK)
        stopper.result()
        assert loop_task.done(), "the tick loop outlived stop()"
        assert source.stats.last_error is None, (
            "this loop stopped cleanly; nothing should be reported as abandoned")

    async def test_a_normal_source_still_stops_cleanly_and_quickly(self):
        ticks = {"n": 0}

        async def tick():
            ticks["n"] += 1

        source = SimulatorSource(tick=tick, interval_seconds=0.01)
        await source.start()
        await asyncio.sleep(0.05)
        loop_task = source._task

        t0 = time.monotonic()
        stopper = await _completes_within(source.stop(), SLACK)
        stopper.result()
        assert time.monotonic() - t0 < 1.0, "a healthy loop must not wait out the bound"
        assert loop_task.done() and source.running is False
        assert ticks["n"] >= 1
        await source.stop()          # idempotent


# ══════════════════════════════════════════════════════════════════════════════
#  MicroBatchCoordinator — CancelledError propagates, futures never strand
# ══════════════════════════════════════════════════════════════════════════════

def _coordinator() -> MicroBatchCoordinator:
    # A window long enough that nothing auto-flushes: the test drives the flush.
    return MicroBatchCoordinator(priority=PRIORITY_BACKGROUND,
                                 window_ms=30_000, max_batch=99)


async def _submit(coord: MicroBatchCoordinator, job) -> asyncio.Task:
    """Submit a job and wait until it is really queued for the next flush."""
    runner = asyncio.ensure_future(coord.run(job))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if coord._pending:
            return runner
    raise AssertionError("job never reached the pending batch")


class TestBatchCoordinatorCancellation:
    async def test_a_cancelled_acquire_re_raises_and_strands_nothing(self, monkeypatch):
        """This arm used to be `except (TimeoutError, CancelledError): return`.
        Returning normally on a cancel is what made the flush task refuse to
        die — and anything awaiting it hang for good."""
        q.llm_queue.active_workers = 0
        coord = _coordinator()

        async def job():
            return "never runs"

        runner = await _submit(coord, job)

        async def cancelled_acquire(*a, **k):
            raise asyncio.CancelledError()

        monkeypatch.setattr(q.llm_queue, "acquire", cancelled_acquire)

        with pytest.raises(asyncio.CancelledError):
            await coord._flush_now()

        result = (await asyncio.gather(runner, return_exceptions=True))[0]
        assert isinstance(result, Exception), "the submitter was stranded"
        assert not isinstance(result, asyncio.CancelledError), (
            "handing a CancelledError to the submitter makes THEIR task look "
            "cancelled to everything above it")

    async def test_a_cancelled_job_loop_resolves_every_future_and_re_raises(self, monkeypatch):
        """Cancellation mid-batch: the strand-nothing rule this file already
        carries must hold, and the cancel must still leave the coordinator."""
        q.llm_queue.active_workers = 0
        coord = _coordinator()

        async def cancels():
            raise asyncio.CancelledError()

        async def never_reached():
            return "second"

        first = await _submit(coord, cancels)
        second = await _submit(coord, never_reached)

        with pytest.raises(asyncio.CancelledError):
            await coord._flush_now()

        results = await asyncio.gather(first, second, return_exceptions=True)
        for r in results:
            assert isinstance(r, Exception), "a batched future was left unresolved"
            assert not isinstance(r, asyncio.CancelledError)

    async def test_the_llm_slot_is_released_when_a_flush_is_cancelled(self, monkeypatch):
        """A cancel must not leak the single GPU slot — that would wedge the
        next process just as thoroughly as the hang did."""
        q.llm_queue.active_workers = 0
        released = {"n": 0}
        real_release = q.llm_queue.release

        def counting_release(*a, **k):
            released["n"] += 1
            return real_release(*a, **k)

        monkeypatch.setattr(q.llm_queue, "release", counting_release)
        coord = _coordinator()

        async def cancels():
            raise asyncio.CancelledError()

        runner = await _submit(coord, cancels)
        with pytest.raises(asyncio.CancelledError):
            await coord._flush_now()
        await asyncio.gather(runner, return_exceptions=True)
        assert released["n"] == 1

    async def test_an_ordinary_timeout_still_fails_the_batch_without_raising(self, monkeypatch):
        """The TimeoutError arm keeps its old behaviour — background work that
        could not get the slot is dropped, not escalated."""
        q.llm_queue.active_workers = 0
        coord = _coordinator()

        async def job():
            return "never runs"

        runner = await _submit(coord, job)

        async def timing_out_acquire(*a, **k):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(q.llm_queue, "acquire", timing_out_acquire)
        await coord._flush_now()                  # must NOT raise

        result = (await asyncio.gather(runner, return_exceptions=True))[0]
        assert isinstance(result, TimeoutError)
