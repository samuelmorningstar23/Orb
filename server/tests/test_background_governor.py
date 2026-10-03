"""Background LLM volume governor (roadmap #3).

Measured 2026-08-01: ambient 4B work held 76% GPU duty (554 calls, peak 57/min)
and degraded interactive Sage decode ~26% during alert storms. The reserved
interactive lane fixed queue WAIT; the governor bounds background VOLUME at
admission — a rolling-60s ceiling, a yield-to-clinician wait with a bounded
rejection, and a quiet tail after interactive traffic drains.

All governor timing is simulated through an injected clock + sleep — no test
here spends real wall-clock time on governor waits. The suite-wide config
default keeps the governor OFF under pytest (it is a wall-clock limiter and the
suite compresses hours of traffic into a real minute), so every test that
exercises it enables it explicitly.

Mirrors tests/test_llm_queue_lanes.py: queue-integration tests drive their own
loop via asyncio.run and reset the loop-bound singletons first.
"""
import asyncio
import time

import pytest

import config
import services.gpu_coordinator as gpu
import services.llm_queue_service as llmq
from services.batch_coordinator import MicroBatchCoordinator
from services.gpu_coordinator import (
    umsq,
    gpu_gate,
    PRIORITY_SAGE,
    PRIORITY_SCRIBE,
    PRIORITY_BACKGROUND,
)
from services.llm_queue_service import (
    BackgroundAdmissionRejected,
    BackgroundGovernor,
    PriorityLLMQueue,
)


def _reset_singletons():
    """Reset loop-bound globals so they re-init on the current asyncio.run loop."""
    gpu._gpu_gate = None
    gpu._gpu_gate_owner = None
    umsq._guard_lock = None
    umsq._queue = None
    umsq._active = False
    umsq._active_task = ""
    umsq._counter = 0


class FakeClock:
    """Injected clock + sleep: sleeping advances simulated time and yields the
    loop exactly once, so governor waits cost zero wall-clock time."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        self.now += seconds
        self.slept += seconds
        await asyncio.sleep(0)


def _governor(clock, **kw):
    kw.setdefault("enabled", True)
    return BackgroundGovernor(clock=clock, sleep=clock.sleep, **kw)


class _Holder:
    """Acquires a lane in its OWN task and holds it until told to let go
    (release() attributes the lane via a per-task ContextVar)."""

    def __init__(self, q, priority, timeout=5.0):
        self.q = q
        self.priority = priority
        self.timeout = timeout
        self.acquired = asyncio.Event()
        self.let_go = asyncio.Event()
        self.finished = asyncio.Event()
        self.error = None
        self.task = None

    async def _run(self):
        try:
            await self.q.acquire(priority=self.priority, timeout=self.timeout)
        except BaseException as exc:          # noqa: BLE001 — recorded for assertions
            self.error = exc
            self.finished.set()
            return
        self.acquired.set()
        try:
            await self.let_go.wait()
        finally:
            self.q.release()
            self.finished.set()

    def start(self):
        self.task = asyncio.create_task(self._run())
        return self

    async def wait_acquired(self, timeout=1.0):
        await asyncio.wait_for(self.acquired.wait(), timeout=timeout)

    async def stop(self, timeout=2.0):
        self.let_go.set()
        await asyncio.wait_for(self.finished.wait(), timeout=timeout)


# ══════════════════════════════════════════════════════════════════════
# 1. Rolling-60s admission ceiling
# ══════════════════════════════════════════════════════════════════════

def test_ceiling_enforced_across_a_simulated_minute():
    async def _t():
        clock = FakeClock()
        g = _governor(clock, max_per_min=12)

        # One admission every 4 simulated seconds: 12 admit, the 13th (t=48,
        # 12 already inside the window) is shed immediately — never queued.
        for _ in range(12):
            await g.admit()
            clock.now += 4.0
        with pytest.raises(BackgroundAdmissionRejected):
            await g.admit()
        assert clock.slept == 0.0, "an over-ceiling admission must shed, not wait"

        # The window ROLLS: at t=61 the t=0 admission has aged out — one slot.
        clock.now = 1000.0 + 61.0
        await g.admit()
        with pytest.raises(BackgroundAdmissionRejected):
            await g.admit()

        # A full minute of silence clears the whole window.
        clock.now += 60.0
        for _ in range(12):
            await g.admit()

    asyncio.run(_t())


def test_concurrent_admissions_never_exceed_ceiling():
    """The check-then-await guard: with one slot left, N concurrent admits must
    admit exactly ONE. An await slipped between the ceiling check and the
    record (the race class that got the previous reserved-lane attempt
    reverted) lets every checker through — this test goes red on that."""
    async def _t():
        clock = FakeClock()
        g = _governor(clock, max_per_min=12)
        for _ in range(11):
            await g.admit()

        results = await asyncio.gather(
            *[g.admit() for _ in range(10)], return_exceptions=True
        )
        admitted = [r for r in results if r is None]
        shed = [r for r in results if isinstance(r, BackgroundAdmissionRejected)]
        assert len(admitted) == 1, f"{len(admitted)} admissions took the last slot"
        assert len(shed) == 9
        assert len(g._admitted) == 12

    asyncio.run(_t())


# ══════════════════════════════════════════════════════════════════════
# 2. Yield to interactive traffic, quiet tail
# ══════════════════════════════════════════════════════════════════════

def test_yield_waits_then_rejects_while_interactive_stays_active():
    async def _t():
        clock = FakeClock()
        g = _governor(clock, yield_wait=3.0, preempt=lambda: True)

        t0 = clock.now
        with pytest.raises(BackgroundAdmissionRejected):
            await g.admit()
        waited = clock.now - t0
        assert 3.0 <= waited <= 3.0 + g.PREEMPT_POLL_SECONDS, (
            f"waited {waited:.2f}s simulated; yield_wait is 3.0"
        )
        assert len(g._admitted) == 0

    asyncio.run(_t())


def test_yield_wait_is_bounded_by_the_callers_own_timeout():
    async def _t():
        clock = FakeClock()
        g = _governor(clock, yield_wait=3.0, preempt=lambda: True)

        t0 = clock.now
        with pytest.raises(BackgroundAdmissionRejected):
            await g.admit(timeout=1.0)   # Vigil's garnish passes timeout=5.0-style budgets
        waited = clock.now - t0
        assert waited <= 1.0 + g.PREEMPT_POLL_SECONDS, (
            f"governor waited {waited:.2f}s past the caller's 1.0s budget"
        )

    asyncio.run(_t())


def test_admission_resumes_after_interactive_ends_plus_quiet_window():
    async def _t():
        clock = FakeClock()
        busy = {"on": True}
        g = _governor(clock, yield_wait=30.0, quiet_after=2.0,
                      preempt=lambda: busy["on"])

        async def end_interactive_at(t):
            while clock.now < t:
                await asyncio.sleep(0)
            busy["on"] = False
            g.note_interactive_end()      # what the queue does at drain

        t0 = clock.now
        ender = asyncio.create_task(end_interactive_at(t0 + 1.0))
        await g.admit()
        await ender
        waited = clock.now - t0
        # ~1.0s of interactive + the full 2.0s quiet tail, not a tick less.
        assert waited >= 3.0, f"admitted {waited:.2f}s in — quiet tail not respected"
        assert waited <= 3.0 + 2 * g.PREEMPT_POLL_SECONDS
        assert len(g._admitted) == 1

    asyncio.run(_t())


def test_quiet_window_respected_and_expires():
    async def _t():
        clock = FakeClock()
        g = _governor(clock, yield_wait=30.0, quiet_after=2.0)

        g.note_interactive_end()          # quiet until t+2.0
        clock.now += 0.5
        t0 = clock.now
        await g.admit()
        assert clock.now - t0 == pytest.approx(1.5), "must sleep out the quiet remainder"

        # Past the tail there is no wait at all.
        clock.now += 10.0
        t0 = clock.now
        await g.admit()
        assert clock.now == t0

    asyncio.run(_t())


# ══════════════════════════════════════════════════════════════════════
# 3. Queue integration: enforcement point, lane exemptions, wiring
# ══════════════════════════════════════════════════════════════════════

def test_interactive_admission_is_never_governed_and_p2_is_exempt():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        clock = FakeClock()
        # Worst-case governor: sheds every governed admission instantly.
        q.governor = _governor(clock, max_per_min=0,
                               preempt=lambda: q.preempt_requested)

        calls = {"n": 0}
        real_admit = q.governor.admit

        async def counting_admit(timeout=None):
            calls["n"] += 1
            return await real_admit(timeout)

        q.governor.admit = counting_admit

        t0 = time.monotonic()
        await q.acquire(priority=PRIORITY_SAGE, timeout=5.0)
        assert time.monotonic() - t0 < 0.1, "interactive admission was delayed"
        assert calls["n"] == 0, "the governor was consulted for an interactive turn"
        q.release()

        # PRIORITY_SCRIBE (clinician-triggered report gen, no deterministic
        # fallback) shares the background lane but is NOT governed.
        await q.acquire(priority=PRIORITY_SCRIBE, timeout=5.0)
        assert calls["n"] == 0
        q.release()

        # PRIORITY_BACKGROUND is — and the zero ceiling sheds it.
        with pytest.raises(BackgroundAdmissionRejected):
            await q.acquire(priority=PRIORITY_BACKGROUND, timeout=5.0)
        assert calls["n"] == 1

        # A shed admission never touched the lane: nothing queued, nothing held.
        assert q.background_active == 0
        assert q.queue.qsize() == 0
        assert q.gate_refs == 0
        assert not gpu_gate().locked()

    asyncio.run(_t())


def test_background_sheds_during_interactive_and_quiet_tail_via_the_queue():
    """Full path: a real interactive holder raises preempt through the queue,
    the governed background acquire yields then sheds; after release the
    queue's synchronous drain-stamp starts the quiet tail on the FAKE clock."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        clock = FakeClock()
        q.governor = _governor(clock, yield_wait=3.0, quiet_after=2.0,
                               preempt=lambda: q.preempt_requested)

        sage = _Holder(q, PRIORITY_SAGE).start()
        await sage.wait_acquired()

        t0 = clock.now
        with pytest.raises(BackgroundAdmissionRejected):
            await q.acquire(priority=PRIORITY_BACKGROUND, timeout=30.0)
        assert clock.now - t0 >= 3.0, "background did not yield before shedding"

        await sage.stop()
        # _retire stamped the tail with the governor's own clock.
        assert q.governor._quiet_until == pytest.approx(clock.now + 2.0)

        t0 = clock.now
        await q.acquire(priority=PRIORITY_BACKGROUND, timeout=30.0)
        assert clock.now - t0 == pytest.approx(2.0), (
            "background admitted inside the quiet tail"
        )
        q.release()

        assert q.active_workers == 0
        assert q.gate_refs == 0
        assert not gpu_gate().locked()

    asyncio.run(_t())


def test_rejection_is_catchable_as_asyncio_timeout():
    """Load-bearing for zero caller changes: every ambient caller already
    catches asyncio.TimeoutError from acquire() and takes its deterministic
    fallback, so the distinct rejection MUST remain that superclass."""
    assert issubclass(BackgroundAdmissionRejected, asyncio.TimeoutError)

    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        q.governor = _governor(FakeClock(), max_per_min=0)
        try:
            await q.acquire(priority=PRIORITY_BACKGROUND, timeout=5.0)
        except asyncio.TimeoutError as e:
            assert isinstance(e, BackgroundAdmissionRejected)
        else:
            pytest.fail("shed admission did not raise")

    asyncio.run(_t())


def test_singleton_governor_follows_config():
    g = llmq.llm_queue.governor
    assert isinstance(g, BackgroundGovernor)
    assert g.enabled == config.BG_LLM_GOVERNOR_ENABLED
    assert g.max_per_min == config.BG_LLM_MAX_PER_MIN
    assert g.yield_wait == config.BG_LLM_YIELD_WAIT_SECONDS
    assert g.quiet_after == config.BG_LLM_QUIET_AFTER_INTERACTIVE_SECONDS


# ══════════════════════════════════════════════════════════════════════
# 4. Batched futures are never stranded by a shed
# ══════════════════════════════════════════════════════════════════════

async def test_rejection_resolves_all_batched_futures(monkeypatch):
    """The coordinator coalesces ambient jobs into ONE governed acquisition; a
    shed must resolve EVERY batched future with THE rejection (a stranded
    future once hung the whole suite — see _flush_now's history)."""
    monkeypatch.setattr(llmq.llm_queue.governor, "enabled", True)
    monkeypatch.setattr(llmq.llm_queue.governor, "max_per_min", 0)
    llmq.llm_queue.active_workers = 0

    # max_batch=3 trips the immediate over-max flush — no real window wait.
    coord = MicroBatchCoordinator(priority=PRIORITY_BACKGROUND, window_ms=5000, max_batch=3)

    async def job():
        return "ran"

    results = await asyncio.wait_for(
        asyncio.gather(*[coord.run(job) for _ in range(3)], return_exceptions=True),
        timeout=5.0,
    )
    assert len(results) == 3
    for r in results:
        assert isinstance(r, BackgroundAdmissionRejected), (
            f"batched future resolved with {type(r).__name__}, not the rejection"
        )
    assert llmq.llm_queue.background_active == 0
    assert llmq.llm_queue.queue.qsize() == 0


async def test_batch_runs_normally_when_governor_admits(monkeypatch):
    monkeypatch.setattr(llmq.llm_queue.governor, "enabled", True)
    monkeypatch.setattr(llmq.llm_queue.governor, "max_per_min", 12)
    llmq.llm_queue.governor.reset()
    llmq.llm_queue.active_workers = 0

    coord = MicroBatchCoordinator(priority=PRIORITY_BACKGROUND, window_ms=5000, max_batch=3)

    async def job():
        return "ran"

    results = await asyncio.wait_for(
        asyncio.gather(*[coord.run(job) for _ in range(3)]), timeout=5.0
    )
    assert results == ["ran", "ran", "ran"]
    # The whole batch consumed ONE admission — coalescing is the volume win.
    assert len(llmq.llm_queue.governor._admitted) == 1
    llmq.llm_queue.governor.reset()


# ══════════════════════════════════════════════════════════════════════
# 5. Disabled flag restores exactly-old behaviour
# ══════════════════════════════════════════════════════════════════════

def test_disabled_governor_restores_old_behaviour():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        clock = FakeClock()
        # Every lever armed to block/shed — disabled must ignore them all.
        q.governor = BackgroundGovernor(
            enabled=False, max_per_min=0, yield_wait=3.0, quiet_after=2.0,
            preempt=lambda: q.preempt_requested, clock=clock, sleep=clock.sleep,
        )

        sage = _Holder(q, PRIORITY_SAGE).start()
        await sage.wait_acquired()

        # Old behaviour: the lanes are independent — background admits
        # immediately even while a clinician streams, no wait, no shed.
        t0 = time.monotonic()
        await q.acquire(priority=PRIORITY_BACKGROUND, timeout=5.0)
        assert time.monotonic() - t0 < 0.1
        assert clock.slept == 0.0, "disabled governor slept"
        assert len(q.governor._admitted) == 0, "disabled governor recorded state"
        q.release()

        for _ in range(20):   # far past the ceiling — nothing is shed
            await q.acquire(priority=PRIORITY_BACKGROUND, timeout=5.0)
            q.release()

        await sage.stop()
        assert q.active_workers == 0
        assert q.gate_refs == 0
        assert not gpu_gate().locked()

    asyncio.run(_t())
