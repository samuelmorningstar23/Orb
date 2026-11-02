"""Synthetic worker admission; no model, network, patient record or download."""
import asyncio
import contextvars
import json

import pytest

from services.local_inference_pool import (
    InferenceDeadlineExceeded,
    InferenceOverloaded,
    InferenceWorkerUnavailable,
    LocalInferencePool,
    WorkerSpec,
    configured_local_pool,
)


def worker(name="one", port=11434, *, models=("synthetic",), capacity=1,
           lanes=("interactive",), reserve=0):
    return WorkerSpec(name, f"http://127.0.0.1:{port}", models, capacity,
                      lanes, reserve, "synthetic-test-qualification")


def test_pool_requires_explicit_valid_qualification_and_stays_loopback_only():
    assert configured_local_pool({}) is None
    entry = {"name": "one", "origin": "http://127.0.0.1:11434", "models": ["synthetic"],
             "capacity": 1, "qualified": True, "qualification": "test-report"}
    assert configured_local_pool({"ORB_LLM_WORKERS": json.dumps([entry])}).interactive_capacity == 1
    for mutation in ({"origin": "https://secret:token@example.com"}, {"qualified": False},
                     {"qualification": ""}, {"capacity": 200.0}, {"capacity": 201},
                     {"extra": "typo"}, {"models": ["synthetic", "synthetic"]}):
        with pytest.raises(ValueError) as exc:
            configured_local_pool({"ORB_LLM_WORKERS": json.dumps([{**entry, **mutation}])})
        assert "secret" not in str(exc.value)


def test_aliases_of_one_loopback_daemon_cannot_manufacture_capacity():
    alias = WorkerSpec("alias", "http://localhost:11434", ("synthetic",), qualification="test")
    with pytest.raises(ValueError, match="distinct"):
        LocalInferencePool([worker(), alias])


def test_unsupported_model_is_refused_without_waiting_or_rerouting():
    async def run():
        pool = LocalInferencePool([worker()])
        with pytest.raises(InferenceWorkerUnavailable):
            await pool.acquire(model="another-model", timeout=1)
        assert pool.active == pool.waiting == 0
    asyncio.run(run())


def test_200_request_burst_is_bounded_and_201st_is_explicitly_rejected():
    async def run():
        pool = LocalInferencePool([worker(capacity=8)], max_inflight=200)
        finish = asyncio.Event()
        acquired = 0

        async def request():
            nonlocal acquired
            lease = await pool.acquire(model="synthetic", timeout=2)
            async with lease:
                acquired += 1
                await finish.wait()

        tasks = [asyncio.create_task(request()) for _ in range(200)]
        await asyncio.sleep(0)
        assert pool.active == 8
        assert pool.waiting == 192
        with pytest.raises(InferenceOverloaded):
            await pool.acquire(model="synthetic", timeout=2)
        assert pool.snapshot()["rejected"] == 1
        finish.set()
        await asyncio.gather(*tasks)
        assert acquired == 200
        assert pool.active == pool.waiting == 0
    asyncio.run(run())


def test_cancelled_waiter_and_cancelled_holder_release_every_slot():
    async def run():
        pool = LocalInferencePool([worker()])
        holder = await pool.acquire(model="synthetic")
        pending = asyncio.create_task(pool.acquire(model="synthetic"))
        await asyncio.sleep(0)
        assert pool.waiting == 1
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert pool.active == 1 and pool.waiting == 0
        holder.release()

        async def holding():
            async with await pool.acquire(model="synthetic"):
                await asyncio.Event().wait()
        task = asyncio.create_task(holding())
        await asyncio.sleep(0)
        assert pool.active == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.active == pool.waiting == 0
    asyncio.run(run())


def test_cancel_after_slot_wakeup_retires_assigned_lease_not_someone_elses():
    async def run():
        pool = LocalInferencePool([worker()])
        holder = await pool.acquire(model="synthetic")
        pending = asyncio.create_task(pool.acquire(model="synthetic"))
        await asyncio.sleep(0)
        holder.release()  # synchronous dispatch assigned the pending future
        pending.cancel()  # cancellation lands before the waiter resumed
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert pool.active == pool.waiting == 0
        async with await pool.acquire(model="synthetic", timeout=0):
            assert pool.active == 1
    asyncio.run(run())


def test_deadline_releases_waiter_and_does_not_cancel_current_generation():
    async def run():
        pool = LocalInferencePool([worker()])
        holder = await pool.acquire(model="synthetic")
        with pytest.raises(InferenceDeadlineExceeded):
            await pool.acquire(model="synthetic", timeout=0.002)
        assert pool.active == 1 and pool.waiting == 0
        assert not holder.released
        holder.release()
    asyncio.run(run())


def test_shared_worker_keeps_interactive_reservation_and_prioritises_waiters():
    async def run():
        pool = LocalInferencePool([worker(capacity=2, lanes=("interactive", "background"), reserve=1)])
        background = await pool.acquire(model="synthetic", lane="background", priority=3)
        pending_background = asyncio.create_task(pool.acquire(model="synthetic", lane="background", priority=3))
        await asyncio.sleep(0)
        foreground = await pool.acquire(model="synthetic", lane="interactive", timeout=0)
        assert pool.active == 2 and pool.waiting == 1
        foreground.release()
        assert not pending_background.done(), "background consumed the reserved interactive capacity"
        background.release()
        other = await pending_background
        other.release()
        assert pool.active == pool.waiting == 0
    asyncio.run(run())


def test_distinct_models_route_only_to_their_own_worker_and_failures_open_circuit():
    async def run():
        now = [0.0]
        pool = LocalInferencePool([
            worker("small", 11434, models=("small",)),
            worker("large", 11435, models=("large",)),
        ], clock=lambda: now[0], cooldown_seconds=10)
        for _ in range(2):
            lease = await pool.acquire(model="large")
            assert lease.origin.endswith(":11435")
            lease.record_failure()
            lease.release()
            lease.release()  # idempotent even after failure
        with pytest.raises(InferenceWorkerUnavailable):
            await pool.acquire(model="large")
        async with await pool.acquire(model="small") as lease:
            assert lease.origin.endswith(":11434")
        now[0] = 11.0
        probe = await pool.acquire(model="large")
        assert probe.probe
        probe.record_success()
        probe.release()
        async with await pool.acquire(model="large") as lease:
            assert not lease.probe
        assert pool.active == pool.waiting == 0
    asyncio.run(run())


def test_qualified_queue_routes_per_task_and_releases_gpu_gate_on_cancellation():
    from services import gpu_coordinator as gpu
    from services.llm_queue_service import PriorityLLMQueue

    async def run():
        gpu._gpu_gate = None
        gpu._gpu_gate_owner = None
        pool = LocalInferencePool([worker("small", 11434, models=("small",)),
                                   worker("large", 11435, models=("large",))])
        q = PriorityLLMQueue(pool=pool)
        held = asyncio.Event()
        count = 0

        async def request(model, port):
            nonlocal count
            ticket = await q.acquire(priority=1, model=model)
            try:
                assert q.current_worker().model == model
                assert q.route_url("/api/chat", model=model).endswith(f":{port}/api/chat")
                with pytest.raises(RuntimeError, match="model"):
                    q.route_url("/api/chat", model="someone-elses-model")
                count += 1
                if count == 2:
                    held.set()
                await asyncio.Event().wait()
            finally:
                q.release(ticket)

        tasks = [asyncio.create_task(request("small", 11434)), asyncio.create_task(request("large", 11435))]
        await asyncio.wait_for(held.wait(), 1)
        assert q.interactive_active == pool.active == q.gate_refs == 2
        assert gpu.gpu_gate().locked()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert q.active_workers == pool.active == pool.waiting == q.gate_refs == 0
        assert not gpu.gpu_gate().locked()
        assert q.current_worker() is None
    asyncio.run(run())


def test_pooled_cross_context_cleanup_requires_exact_ticket_and_never_guesses():
    from services import gpu_coordinator as gpu
    from services.llm_queue_service import PriorityLLMQueue

    async def run():
        gpu._gpu_gate = None
        gpu._gpu_gate_owner = None
        pool = LocalInferencePool([worker(capacity=2)])
        q = PriorityLLMQueue(pool=pool)
        ticket = await q.acquire(priority=1, model="synthetic")
        empty_context = contextvars.Context()
        empty_context.run(q.release)
        assert q.active_workers == pool.active == 1
        empty_context.run(q.release, ticket)
        assert q.active_workers == pool.active == 0
        q.release(ticket)
        assert q.gate_refs == 0
    asyncio.run(run())


def test_pool_enabled_inference_without_task_lease_is_refused():
    from services.llm_queue_service import PriorityLLMQueue
    q = PriorityLLMQueue(pool=LocalInferencePool([worker()]))
    with pytest.raises(RuntimeError, match="task-owned"):
        q.route_url("/api/chat", model="synthetic")


def test_late_concurrent_success_cannot_close_an_open_circuit():
    async def run():
        pool = LocalInferencePool([worker(capacity=3)])
        leases = [await pool.acquire(model="synthetic") for _ in range(3)]
        leases[0].record_failure()
        leases[1].record_failure()
        leases[2].record_success()  # An old request, not a recovery probe.
        for lease in leases:
            lease.release()
        with pytest.raises(InferenceWorkerUnavailable):
            await pool.acquire(model="synthetic")
        assert pool.active == 0
    asyncio.run(run())


def test_queue_bounds_governor_waiters_before_they_reach_worker_pool():
    from services import gpu_coordinator as gpu
    from services.llm_queue_service import BackgroundGovernor, PriorityLLMQueue

    async def run():
        gpu._gpu_gate = None
        gpu._gpu_gate_owner = None
        pool = LocalInferencePool([
            worker("foreground", 11434),
            worker("background", 11435, lanes=("background",)),
        ], max_inflight=3)
        q = PriorityLLMQueue(pool=pool)
        q.governor = BackgroundGovernor(enabled=True, max_per_min=20, yield_wait=2,
                                        quiet_after=0, preempt=lambda: q.preempt_requested)
        foreground = await q.acquire(priority=1, model="synthetic")

        async def background():
            ticket = await q.acquire(priority=3, model="synthetic")
            q.release(ticket)

        # Use an empty context: these are independent requests, not nested
        # inference inside the foreground turn's existing worker lease.
        pending = [asyncio.create_task(background(), context=contextvars.Context()) for _ in range(2)]
        await asyncio.sleep(0)
        assert q._pool_admitting == 2
        assert pool.waiting == 0  # Both are still yielding in the governor.
        rejected = asyncio.create_task(background(), context=contextvars.Context())
        with pytest.raises(InferenceOverloaded):
            await rejected
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        q.release(foreground)
        assert q._pool_admitting == q.active_workers == pool.active == 0
        assert not gpu.gpu_gate().locked()
    asyncio.run(run())
