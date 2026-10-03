"""Reserved interactive lane in the LLM queue (Lever C).

Proves the property the single-slot queue could not give us: an interactive
Sage turn NEVER waits for a background holder to finish (measured 31 s on the
appliance), while keeping every existing safety property intact —

  * the GPU gate is held whenever any LLM lane is active, so in-process ML
    (umsq: CfC / LSTM / MLX) can never run on the GPU during an Ollama stream,
  * the gate is released exactly once, by the LAST lane to let go,
  * a timeout or cancellation in either lane leaks neither a lane slot nor a
    gate reference.

Pure asyncio (no GPU, no model). Mirrors tests/test_gpu_gate.py: each test
drives its own loop via asyncio.run and resets the lazily-loop-bound global
singletons first so locks bind to the fresh loop.
"""
import asyncio
import time

import pytest

import services.gpu_coordinator as gpu
import services.llm_queue_service as llmq
from services.gpu_coordinator import (
    umsq,
    gpu_gate,
    PRIORITY_SAGE,
    PRIORITY_SCRIBE,
    PRIORITY_BACKGROUND,
)
from services.llm_queue_service import PriorityLLMQueue


def _reset_singletons():
    """Reset loop-bound globals so they re-init on the current asyncio.run loop."""
    gpu._gpu_gate = None
    gpu._gpu_gate_owner = None
    umsq._guard_lock = None
    umsq._queue = None
    umsq._active = False
    umsq._active_task = ""
    umsq._counter = 0


async def _until(predicate, timeout=1.0):
    """Wait until predicate() is true; raise TimeoutError if it never is."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.002)


def _assert_clean(q):
    """No lane slot, no gate ref, no queued waiter, gate physically free."""
    assert q.active_workers == 0, f"lane slot leaked: active_workers={q.active_workers}"
    assert q.interactive_active == 0
    assert q.interactive_waiting == 0
    assert q.background_active == 0
    assert q.gate_refs == 0, f"gate ref leaked: {q.gate_refs}"
    assert q.queue.qsize() == 0
    assert not gpu_gate().locked(), "GPU gate left locked"
    assert not q.preempt_requested


class _Holder:
    """Acquires a lane in its OWN task and holds it until told to let go.

    Separate tasks matter: release() infers the lane from a per-task ContextVar,
    so this is the shape production runs in (one request = one task).
    """

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
        except asyncio.CancelledError:
            # Re-raise so the task is genuinely cancelled (the test awaits it).
            self.finished.set()
            raise
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
# 1. The whole point: interactive never queues behind background
# ══════════════════════════════════════════════════════════════════════

def test_interactive_never_waits_for_background():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        bg = _Holder(q, PRIORITY_BACKGROUND).start()
        await bg.wait_acquired()
        assert q.background_active == 1
        assert gpu_gate().locked()

        # The old single-slot queue made this wait for bg to FINISH. It must
        # now resolve immediately — well inside a timeout far shorter than any
        # background stream (narrator 45 s, timeline 120 s).
        t0 = time.monotonic()
        await asyncio.wait_for(q.acquire(priority=PRIORITY_SAGE, timeout=5.0), timeout=0.25)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"interactive waited {elapsed:.3f}s for background"

        # Background is still holding — interactive did not evict or wait for it.
        assert q.background_active == 1
        assert q.interactive_active == 1
        assert q.gate_refs == 2, "both lanes must be counted on the gate"

        q.release()                  # interactive lets go
        assert gpu_gate().locked(), "gate must stay held while background runs"
        await bg.stop()
        _assert_clean(q)

    asyncio.run(_t())


def test_scribe_and_background_still_share_one_lane():
    """Only PRIORITY_SAGE gets the reserved lane. P2/P3 keep the single
    background slot (and its priority ordering) exactly as before."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        bg = _Holder(q, PRIORITY_BACKGROUND).start()
        await bg.wait_acquired()

        with pytest.raises(asyncio.TimeoutError):
            await q.acquire(priority=PRIORITY_SCRIBE, timeout=0.15)
        assert q.background_active == 1

        await bg.stop()
        # Slot is free again for the background lane.
        await q.acquire(priority=PRIORITY_SCRIBE, timeout=1.0)
        assert q.background_active == 1
        q.release()
        _assert_clean(q)

    asyncio.run(_t())


# ══════════════════════════════════════════════════════════════════════
# 2. The interactive lane is bounded (MAX_INTERACTIVE = 1)
# ══════════════════════════════════════════════════════════════════════

def test_second_interactive_waits_for_the_first():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        assert q.max_interactive == llmq.MAX_INTERACTIVE == 1

        first = _Holder(q, PRIORITY_SAGE).start()
        await first.wait_acquired()

        # A second interactive turn must NOT be admitted while the first streams.
        with pytest.raises(asyncio.TimeoutError):
            await q.acquire(priority=PRIORITY_SAGE, timeout=0.15)
        assert q.interactive_active == 1
        assert q.gate_refs == 1, "the rejected turn must not leave a gate ref"

        # A patient second turn waits on the semaphore and is granted the
        # instant the first releases.
        second = _Holder(q, PRIORITY_SAGE).start()
        await _until(lambda: q.interactive_waiting == 1)
        assert not second.acquired.is_set()
        assert q.interactive_active == 1

        await first.stop()
        await second.wait_acquired()
        assert q.interactive_active == 1
        assert q.interactive_waiting == 0
        assert q.gate_refs == 1

        await second.stop()
        _assert_clean(q)

    asyncio.run(_t())


def test_preempt_requested_tracks_interactive_traffic():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        assert q.preempt_requested is False

        bg = _Holder(q, PRIORITY_BACKGROUND).start()
        await bg.wait_acquired()
        assert q.preempt_requested is False, "background work must not set preempt"

        sage = _Holder(q, PRIORITY_SAGE).start()
        await sage.wait_acquired()
        assert q.preempt_requested is True

        # A queued (not yet granted) interactive turn also raises the flag, so a
        # long background stream can bail the moment a clinician asks.
        waiting = _Holder(q, PRIORITY_SAGE).start()
        await _until(lambda: q.interactive_waiting == 1)
        await sage.stop()
        assert q.preempt_requested is True
        await waiting.wait_acquired()
        await waiting.stop()

        assert q.preempt_requested is False
        await bg.stop()
        _assert_clean(q)

    asyncio.run(_t())


# ══════════════════════════════════════════════════════════════════════
# 3. GPU gate: ref-counted across lanes, released exactly once
# ══════════════════════════════════════════════════════════════════════

def test_gate_held_until_last_lane_releases_and_never_double_released(monkeypatch):
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        calls = {"acquire": 0, "release": 0}
        real_acquire = llmq.acquire_gpu_gate
        real_release = llmq.release_gpu_gate

        async def counting_acquire(owner, timeout=None):
            calls["acquire"] += 1
            return await real_acquire(owner, timeout=timeout)

        def counting_release(owner):
            calls["release"] += 1
            return real_release(owner)

        monkeypatch.setattr(llmq, "acquire_gpu_gate", counting_acquire)
        monkeypatch.setattr(llmq, "release_gpu_gate", counting_release)

        bg = _Holder(q, PRIORITY_BACKGROUND).start()
        await bg.wait_acquired()
        assert (calls["acquire"], calls["release"]) == (1, 0)
        assert q.gate_refs == 1 and gpu_gate().locked()

        sage = _Holder(q, PRIORITY_SAGE).start()
        await sage.wait_acquired()
        # Second lane JOINS the hold — it must not try to take the lock again
        # (that is the deadlock the ref-count exists to avoid).
        assert calls["acquire"] == 1, "second lane re-acquired the exclusive gate"
        assert q.gate_refs == 2 and gpu_gate().locked()

        await sage.stop()
        assert q.gate_refs == 1
        assert calls["release"] == 0, "gate released while a lane was still active"
        assert gpu_gate().locked()

        await bg.stop()
        assert q.gate_refs == 0
        assert calls["release"] == 1, "last releaser must release exactly once"
        assert not gpu_gate().locked()

        # A spurious extra release() (double-release from a finally block) must
        # not touch the gate again.
        q.release()
        assert calls["release"] == 1
        assert q.gate_refs == 0
        assert not gpu_gate().locked()
        _assert_clean(q)

    asyncio.run(_t())


def test_simultaneous_lane_starts_take_the_gate_exactly_once(monkeypatch):
    """REGRESSION for the bug that got the previous attempt reverted.

    Both lanes start while the gate refcount is 0→1 in flight. With a
    check-then-await refcount, BOTH tasks observe `refs == 0` and both try to
    take the exclusive lock — the second then blocks behind the first for the
    whole session (deadlock in practice). The gate acquire is slowed here to
    hold that window wide open on purpose.
    """
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        calls = {"n": 0}
        real_acquire = llmq.acquire_gpu_gate

        async def slow_acquire(owner, timeout=None):
            calls["n"] += 1
            await asyncio.sleep(0.05)      # widen the 0 → 1 transition window
            return await real_acquire(owner, timeout=timeout)

        monkeypatch.setattr(llmq, "acquire_gpu_gate", slow_acquire)

        sage = _Holder(q, PRIORITY_SAGE, timeout=2.0).start()
        bg = _Holder(q, PRIORITY_BACKGROUND, timeout=2.0).start()

        # Both must be granted; a racy refcount leaves the loser stuck behind
        # the winner's exclusive hold until it times out.
        await asyncio.wait_for(
            asyncio.gather(sage.acquired.wait(), bg.acquired.wait()), timeout=1.0
        )
        assert calls["n"] == 1, (
            f"the exclusive GPU gate was acquired {calls['n']}x for one hold — "
            "check-then-await race"
        )
        assert q.gate_refs == 2
        assert q.interactive_active == 1 and q.background_active == 1

        await sage.stop()
        await bg.stop()
        _assert_clean(q)

    asyncio.run(_t())


def test_ml_cannot_run_while_either_llm_lane_is_active():
    """Mirrors tests/test_gpu_gate.py: in-process ML (umsq) must never get the
    GPU while an Ollama stream holds a lane — for BOTH lanes, and while both
    are active at once."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        async def ml_blocked():
            try:
                await asyncio.wait_for(
                    umsq.acquire("ml", PRIORITY_BACKGROUND).__aenter__(), timeout=0.15
                )
            except asyncio.TimeoutError:
                return True
            return False

        # (a) interactive lane alone
        await q.acquire(priority=PRIORITY_SAGE, timeout=1.0)
        assert gpu_gate().locked()
        assert await ml_blocked(), "ML acquired the GPU during an interactive stream"
        q.release()
        assert not gpu_gate().locked()

        # (b) background lane alone
        await q.acquire(priority=PRIORITY_BACKGROUND, timeout=1.0)
        assert await ml_blocked(), "ML acquired the GPU during a background stream"
        q.release()
        assert not gpu_gate().locked()

        # (c) both lanes at once — the ref-counted gate must still exclude ML
        bg = _Holder(q, PRIORITY_BACKGROUND).start()
        await bg.wait_acquired()
        sage = _Holder(q, PRIORITY_SAGE).start()
        await sage.wait_acquired()
        assert q.gate_refs == 2
        assert await ml_blocked(), "ML acquired the GPU while both lanes ran"
        await sage.stop()
        await bg.stop()

        # And once every lane is done, ML can proceed.
        async with umsq.acquire("ml", PRIORITY_BACKGROUND):
            assert gpu_gate().locked()
        _assert_clean(q)

    asyncio.run(_t())


def test_lanes_blocked_while_ml_holds_the_gate_and_recover_cleanly():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        async with umsq.acquire("ml-task", PRIORITY_BACKGROUND):
            assert gpu_gate().locked()
            with pytest.raises(asyncio.TimeoutError):
                await q.acquire(priority=PRIORITY_SAGE, timeout=0.15)
            with pytest.raises(asyncio.TimeoutError):
                await q.acquire(priority=PRIORITY_BACKGROUND, timeout=0.15)
            # Neither failed acquire may keep a slot or a gate ref.
            assert q.active_workers == 0
            assert q.interactive_waiting == 0
            assert q.gate_refs == 0

        # Both lanes work again after ML lets go.
        await q.acquire(priority=PRIORITY_SAGE, timeout=1.0)
        assert q.gate_refs == 1
        q.release()
        await q.acquire(priority=PRIORITY_BACKGROUND, timeout=1.0)
        assert q.gate_refs == 1
        q.release()
        _assert_clean(q)

    asyncio.run(_t())


# ══════════════════════════════════════════════════════════════════════
# 4. Nothing leaks on timeout or cancellation
# ══════════════════════════════════════════════════════════════════════

def test_interactive_timeout_leaks_nothing():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        holder = _Holder(q, PRIORITY_SAGE).start()
        await holder.wait_acquired()

        for _ in range(3):
            with pytest.raises(asyncio.TimeoutError):
                await q.acquire(priority=PRIORITY_SAGE, timeout=0.05)
            assert q.interactive_waiting == 0
            assert q.interactive_active == 1
            assert q.gate_refs == 1

        await holder.stop()
        _assert_clean(q)

        # A later acquire still succeeds (the lane was not wedged).
        await q.acquire(priority=PRIORITY_SAGE, timeout=1.0)
        q.release()
        _assert_clean(q)

    asyncio.run(_t())


def test_background_timeout_leaks_nothing():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        holder = _Holder(q, PRIORITY_BACKGROUND).start()
        await holder.wait_acquired()

        for _ in range(3):
            with pytest.raises(asyncio.TimeoutError):
                await q.acquire(priority=PRIORITY_BACKGROUND, timeout=0.05)
            assert q.background_active == 1
            assert q.gate_refs == 1

        await holder.stop()
        _assert_clean(q)

        await q.acquire(priority=PRIORITY_BACKGROUND, timeout=1.0)
        q.release()
        _assert_clean(q)

    asyncio.run(_t())


def test_cancellation_in_each_lane_leaks_nothing():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        for priority in (PRIORITY_SAGE, PRIORITY_BACKGROUND):
            holder = _Holder(q, priority).start()
            await holder.wait_acquired()

            waiter = _Holder(q, priority, timeout=30.0).start()
            if priority == PRIORITY_SAGE:
                await _until(lambda: q.interactive_waiting == 1)
            else:
                await _until(lambda: q.queue.qsize() == 1)

            waiter.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter.task

            assert q.interactive_waiting == 0
            assert q.gate_refs == 1, "cancelled waiter took a gate ref"
            assert q.active_workers == 1

            await holder.stop()
            _assert_clean(q)

            # The lane is still usable after a cancellation.
            await q.acquire(priority=priority, timeout=1.0)
            q.release()
            _assert_clean(q)

    asyncio.run(_t())


def test_cancelled_gate_wait_leaks_nothing():
    """Cancel a lane while it is blocked on the GPU gate (held by in-process
    ML) — the hardest unwind path, because the lane slot is already taken."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        ml = umsq.acquire("ml-task", PRIORITY_BACKGROUND)
        await ml.__aenter__()
        assert gpu_gate().locked()

        for priority in (PRIORITY_SAGE, PRIORITY_BACKGROUND):
            waiter = _Holder(q, priority, timeout=30.0).start()
            await asyncio.sleep(0.05)          # let it reach the gate wait
            assert not waiter.acquired.is_set()
            waiter.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter.task
            assert q.active_workers == 0, "lane slot leaked while blocked on the gate"
            assert q.interactive_waiting == 0
            assert q.gate_refs == 0

        await ml.__aexit__(None, None, None)
        assert not gpu_gate().locked()

        await q.acquire(priority=PRIORITY_SAGE, timeout=1.0)
        q.release()
        _assert_clean(q)

    asyncio.run(_t())


# ══════════════════════════════════════════════════════════════════════
# 5. release() lane attribution
# ══════════════════════════════════════════════════════════════════════

def test_each_task_releases_its_own_lane():
    """A background release() must not hand back the interactive slot (and
    vice versa) — release() takes no arguments, so this is contextvar work."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        bg = _Holder(q, PRIORITY_BACKGROUND).start()
        sage = _Holder(q, PRIORITY_SAGE).start()
        await bg.wait_acquired()
        await sage.wait_acquired()
        assert q.gate_refs == 2

        await bg.stop()
        assert q.background_active == 0
        assert q.interactive_active == 1, "background release freed the interactive lane"
        assert q.gate_refs == 1
        assert gpu_gate().locked()

        await sage.stop()
        _assert_clean(q)

    asyncio.run(_t())


def test_nested_acquire_release_in_one_task_is_lifo_safe():
    """Some callers acquire a second lane inside the first (a background flush
    that also answers a Sage turn). The ContextVar token restore must give each
    release() the right lane."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        await q.acquire(priority=PRIORITY_BACKGROUND, timeout=1.0)
        await q.acquire(priority=PRIORITY_SAGE, timeout=1.0)
        assert (q.background_active, q.interactive_active, q.gate_refs) == (1, 1, 2)

        q.release()   # innermost (interactive)
        assert (q.background_active, q.interactive_active, q.gate_refs) == (1, 0, 1)
        q.release()   # outer (background)
        _assert_clean(q)

    asyncio.run(_t())


def test_release_without_context_falls_back_and_never_corrupts_the_gate():
    """release() called from a task that did not acquire (an async generator
    finalized by the loop's asyncgen hook). It must attribute the lane by
    fallback rather than wedge, and must never double-release the gate."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        # (a) fully spurious release on an idle queue: no crash, no gate damage.
        q.release()
        _assert_clean(q)

        # (b) one interactive holder, release() from a foreign task.
        holder = _Holder(q, PRIORITY_SAGE).start()
        await holder.wait_acquired()

        async def foreign_release():
            q.release()

        await asyncio.create_task(foreign_release())
        assert q.interactive_active == 0
        assert q.gate_refs == 0
        assert not gpu_gate().locked()

        holder.let_go.set()
        await asyncio.wait_for(holder.finished.wait(), timeout=2.0)
        # The holder's own release() is now a no-op (its ticket is retired).
        assert q.gate_refs == 0
        assert not gpu_gate().locked()

        # The lane is not wedged: a fresh interactive turn still gets in.
        await q.acquire(priority=PRIORITY_SAGE, timeout=1.0)
        q.release()
        _assert_clean(q)

    asyncio.run(_t())


def test_release_without_context_with_both_lanes_busy_still_balances():
    """The ambiguous fallback case: both lanes busy and a release() arrives with
    no ticket. It may free the lanes in the wrong ORDER, but every counter, the
    semaphore and the gate must still land on zero, and neither holder may end
    up retiring a second ticket."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        bg = _Holder(q, PRIORITY_BACKGROUND).start()
        sage = _Holder(q, PRIORITY_SAGE).start()
        await bg.wait_acquired()
        await sage.wait_acquired()
        assert q.gate_refs == 2

        async def foreign_release():
            q.release()

        await asyncio.create_task(foreign_release())
        assert q.gate_refs == 1
        assert q.active_workers == 1

        await sage.stop()
        await bg.stop()
        _assert_clean(q)

        # Both lanes still work afterwards.
        await q.acquire(priority=PRIORITY_SAGE, timeout=1.0)
        q.release()
        await q.acquire(priority=PRIORITY_BACKGROUND, timeout=1.0)
        q.release()
        _assert_clean(q)

    asyncio.run(_t())


def test_timeout_is_drawn_down_across_both_wait_stages(monkeypatch):
    """One acquire can wait twice: for the internal lock, then for the GPU gate.
    Both must draw on ONE deadline — otherwise a `timeout=120` caller can sit
    for 240 s, and Vigil's 5 s load-shed budget silently becomes 10 s.

    Staged so both waits actually happen: ML holds the gate for the whole test,
    a background acquire parks on the gate WHILE HOLDING the internal lock, and
    the interactive acquire under test therefore waits for the lock first and
    the gate second.
    """
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        seen = []
        real_acquire = llmq.acquire_gpu_gate

        async def recording_acquire(owner, timeout=None):
            seen.append(timeout)
            return await real_acquire(owner, timeout=timeout)

        monkeypatch.setattr(llmq, "acquire_gpu_gate", recording_acquire)

        ml = umsq.acquire("ml-task", PRIORITY_BACKGROUND)
        await ml.__aenter__()                       # gate held for the whole test

        blocker = _Holder(q, PRIORITY_BACKGROUND, timeout=30.0).start()
        await _until(lambda: q.lock.locked())       # parked on the gate, holding the lock

        t0 = time.monotonic()
        victim = _Holder(q, PRIORITY_SAGE, timeout=0.3).start()
        await asyncio.sleep(0.2)                    # 0.2 s of the 0.3 s spent on the lock
        blocker.task.cancel()                       # lock handed back, gate still ML's
        with pytest.raises(asyncio.CancelledError):
            await blocker.task

        await asyncio.wait_for(victim.finished.wait(), timeout=2.0)
        elapsed = time.monotonic() - t0
        assert isinstance(victim.error, asyncio.TimeoutError)
        assert elapsed < 0.45, (
            f"acquire(timeout=0.3) took {elapsed:.2f}s — the deadline was spent twice"
        )
        assert seen[-1] < 0.2, (
            f"the gate wait was handed a fresh budget ({seen[-1]}s) instead of the remainder"
        )

        await ml.__aexit__(None, None, None)
        _assert_clean(q)

    asyncio.run(_t())


def test_active_workers_contract_for_metrics_and_test_resets():
    """metrics_service reads `active_workers`; conftest and several tests assign
    `active_workers = 0` as a reset. Both must keep working."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        assert q.active_workers == 0
        bg = _Holder(q, PRIORITY_BACKGROUND).start()
        await bg.wait_acquired()
        assert q.active_workers == 1
        sage = _Holder(q, PRIORITY_SAGE).start()
        await sage.wait_acquired()
        assert q.active_workers == 2, "metrics must see BOTH lanes"

        await sage.stop()
        await bg.stop()
        assert q.active_workers == 0

        # Administrative reset hatch, then the queue is still usable.
        q.active_workers = 0
        assert q.active_workers == 0
        assert q.gate_refs == 0
        await q.acquire(priority=PRIORITY_SAGE, timeout=1.0)
        q.release()
        _assert_clean(q)

    asyncio.run(_t())


# ══════════════════════════════════════════════════════════════════════
# 6. Stress
# ══════════════════════════════════════════════════════════════════════

def test_stress_mixed_priorities_all_complete_and_counters_return_to_zero():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        seen = {"max_interactive": 0, "max_background": 0, "max_gate_refs": 0}
        completed = []

        async def worker(i, priority):
            await q.acquire(priority=priority, timeout=20.0)
            try:
                seen["max_interactive"] = max(seen["max_interactive"], q.interactive_active)
                seen["max_background"] = max(seen["max_background"], q.background_active)
                seen["max_gate_refs"] = max(seen["max_gate_refs"], q.gate_refs)
                assert gpu_gate().locked(), "gate not held during an active lane"
                await asyncio.sleep(0.002)
                completed.append(i)
            finally:
                q.release()

        priorities = [PRIORITY_SAGE, PRIORITY_SCRIBE, PRIORITY_BACKGROUND, PRIORITY_SAGE,
                      PRIORITY_BACKGROUND, PRIORITY_SCRIBE]
        jobs = [worker(i, priorities[i % len(priorities)]) for i in range(48)]
        await asyncio.wait_for(asyncio.gather(*jobs), timeout=30.0)

        assert len(completed) == 48
        assert seen["max_interactive"] <= 1, "interactive lane exceeded its bound"
        assert seen["max_background"] <= 1, "background lane exceeded its bound"
        assert seen["max_gate_refs"] <= 2, "gate refs exceeded one per lane"
        assert seen["max_gate_refs"] == 2, "lanes never actually overlapped — no reserved lane"
        _assert_clean(q)

    asyncio.run(_t())


def test_stress_with_ml_contention_never_overlaps_ml_and_llm():
    """Mixed LLM lanes plus in-process ML: the invariant that matters clinically
    is that ML and Ollama never share the GPU."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        state = {"ml_running": 0, "llm_running": 0, "violations": 0}

        def check():
            if state["ml_running"] and state["llm_running"]:
                state["violations"] += 1

        async def llm_worker(priority):
            await q.acquire(priority=priority, timeout=20.0)
            state["llm_running"] += 1
            check()
            try:
                await asyncio.sleep(0.002)
                check()
            finally:
                state["llm_running"] -= 1
                q.release()

        async def ml_worker():
            async with umsq.acquire("cfc", PRIORITY_BACKGROUND):
                state["ml_running"] += 1
                check()
                await asyncio.sleep(0.002)
                check()
                state["ml_running"] -= 1

        jobs = []
        for i in range(20):
            jobs.append(llm_worker(PRIORITY_SAGE if i % 3 == 0 else PRIORITY_BACKGROUND))
            jobs.append(ml_worker())
        await asyncio.wait_for(asyncio.gather(*jobs), timeout=30.0)

        assert state["violations"] == 0, "ML and LLM ran on the GPU concurrently"
        _assert_clean(q)
        assert not umsq.is_busy

    asyncio.run(_t())


# ═══════════════════════════════════════════════════════════════════════════
#  LANE-HOLD WATCHDOG
#
#  The background lane is ONE slot wide, so what starves every waiter is how
#  long the current holder keeps it — and nothing reported that until 2026-08-15.
#  A waiter records `queue_timeout` identically whether the queue was fifty deep
#  or one caller sat on an otherwise IDLE lane for five minutes; those are
#  opposite problems with opposite fixes. Three 300 s holds and thirty-three
#  120 s ones were found only by reconstructing acquire/release pairs out of the
#  log by hand. These tests pin the instrument that replaces that.
# ═══════════════════════════════════════════════════════════════════════════


def test_an_over_long_hold_is_reported_while_it_is_still_being_held(monkeypatch):
    """The warning fires DURING the stall, not after the holder finally lets go.

    After-the-fact is useless for the case that matters: a lane held so long
    that the process is restarted before it releases reports nothing at all.
    """
    monkeypatch.setenv("ORB_LLM_LANE_HOLD_WARN", "1")

    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        assert q.long_holds == 0

        holder = _Holder(q, PRIORITY_BACKGROUND).start()
        await holder.wait_acquired()

        # Still inside the budget: silence.
        await asyncio.sleep(0.3)
        assert q.long_holds == 0

        # Past it, and the holder has NOT released — the point of the test.
        await asyncio.sleep(1.0)
        assert q.long_holds == 1, "an over-long hold was not reported while held"
        assert q.background_active == 1, "the watchdog must not seize the lane"

        await holder.stop()
        _assert_clean(q)

    asyncio.run(_t())


def test_the_watchdog_never_seizes_the_lane_from_a_live_holder(monkeypatch):
    """It warns and does nothing else.

    Force-releasing would admit a second stream on to a lane whose whole purpose
    is max_concurrent=1 — two 27B streams on one GPU is worse than the stall it
    would be reporting.
    """
    monkeypatch.setenv("ORB_LLM_LANE_HOLD_WARN", "1")

    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        holder = _Holder(q, PRIORITY_BACKGROUND).start()
        await holder.wait_acquired()
        await asyncio.sleep(1.3)
        assert q.long_holds == 1

        # The lane is still exclusively the holder's: a second background
        # acquire must still time out rather than be let in alongside it.
        with pytest.raises(asyncio.TimeoutError):
            await q.acquire(priority=PRIORITY_BACKGROUND, timeout=0.05)
        assert q.background_active == 1
        assert q.gate_refs == 1

        await holder.stop()
        _assert_clean(q)

    asyncio.run(_t())


def test_a_normal_hold_is_never_reported(monkeypatch):
    """No false positives: the counter is only useful if a quiet lane reads 0."""
    monkeypatch.setenv("ORB_LLM_LANE_HOLD_WARN", "30")

    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()

        for _ in range(5):
            await q.acquire(priority=PRIORITY_SCRIBE, timeout=1.0)
            await asyncio.sleep(0.01)
            q.release()
        _assert_clean(q)
        assert q.long_holds == 0

    asyncio.run(_t())
