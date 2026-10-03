"""Unified GPU admission gate — proves LLM (Ollama) and in-process ML (umsq)
sessions are mutually exclusive on the single physical GPU.

Pure asyncio (no GPU needed). Mirrors test_gpu_coordinator.py: each test drives
its own loop via asyncio.run and resets the lazily-loop-bound global singletons
first so locks bind to the fresh loop.
"""
import asyncio

import services.gpu_coordinator as gpu
from services.gpu_coordinator import umsq, gpu_gate, PRIORITY_BACKGROUND
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


def test_umsq_holds_gate_during_session():
    async def _t():
        _reset_singletons()
        assert not gpu_gate().locked()
        async with umsq.acquire("ml-task", PRIORITY_BACKGROUND):
            assert gpu_gate().locked()   # gate held during ML session
        assert not gpu_gate().locked()   # released on exit
    asyncio.run(_t())


def test_llm_cannot_run_while_ml_holds_gate():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        async with umsq.acquire("ml-task", PRIORITY_BACKGROUND):
            assert gpu_gate().locked()
            raised = False
            try:
                await q.acquire(priority=1, timeout=0.2)  # must block on gate
            except asyncio.TimeoutError:
                raised = True
            assert raised, "LLM acquired the GPU while ML held it"
        # After ML releases, the LLM lane can acquire.
        await q.acquire(priority=1, timeout=1.0)
        assert gpu_gate().locked()
        q.release()
        assert not gpu_gate().locked()
    asyncio.run(_t())


def test_ml_cannot_run_while_llm_holds_gate():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        await q.acquire(priority=1, timeout=1.0)
        assert gpu_gate().locked()
        raised = False
        try:
            await asyncio.wait_for(
                umsq.acquire("ml", PRIORITY_BACKGROUND).__aenter__(), timeout=0.2
            )
        except asyncio.TimeoutError:
            raised = True
        finally:
            q.release()
        assert raised, "ML acquired the GPU while the LLM held it"
        assert not gpu_gate().locked()
    asyncio.run(_t())


def test_queued_llm_timeout_does_not_release_active_holder():
    """Regression (review finding #1): a queued LLM waiter that times out must NOT
    release the ACTIVE worker's gate — ownership is per-acquisition, not shared."""
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        # Worker A acquires the internal slot + gate.
        await q.acquire(priority=1, timeout=1.0)
        assert gpu_gate().locked()
        # Worker B queues behind A on the SAME singleton and times out.
        raised = False
        try:
            await q.acquire(priority=1, timeout=0.2)
        except asyncio.TimeoutError:
            raised = True
        assert raised
        # A's gate must still be held — B's timeout must not have released it.
        assert gpu_gate().locked()
        q.release()  # A releases its own hold
        assert not gpu_gate().locked()
    asyncio.run(_t())


def test_umsq_queued_timeout_does_not_reset_active_holder():
    """Regression (review finding #3): a queued umsq waiter with the SAME task_name
    that is cancelled must not reset the running holder's active state."""
    async def _t():
        _reset_singletons()
        # Holder acquires with a shared name.
        holder = umsq.acquire("Sage Voice", PRIORITY_BACKGROUND)
        await holder.__aenter__()
        assert umsq.is_busy and gpu_gate().locked()
        # A same-named waiter queues then is cancelled while waiting.
        waiter = umsq.acquire("Sage Voice", PRIORITY_BACKGROUND)
        try:
            await asyncio.wait_for(waiter.__aenter__(), timeout=0.15)
        except asyncio.TimeoutError:
            pass
        # The holder must still be active and the gate still held.
        assert umsq.is_busy, "holder was wrongly reset by a same-named queued waiter"
        assert gpu_gate().locked()
        await holder.__aexit__(None, None, None)
        assert not umsq.is_busy and not gpu_gate().locked()
    asyncio.run(_t())


def test_llm_release_frees_gate_no_leak():
    async def _t():
        _reset_singletons()
        q = PriorityLLMQueue()
        await q.acquire(priority=2, timeout=1.0)
        assert gpu_gate().locked()
        q.release()
        assert not gpu_gate().locked()
        # Fresh acquire works after release (no leaked slot/gate).
        await q.acquire(priority=2, timeout=1.0)
        assert gpu_gate().locked()
        q.release()
        assert not gpu_gate().locked()
    asyncio.run(_t())
