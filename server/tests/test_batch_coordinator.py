"""Micro-batch coordinator tests: concurrent ambient jobs coalesce into ONE
queue acquisition, and per-job failures are isolated.
"""
import asyncio

import pytest

from services.batch_coordinator import MicroBatchCoordinator
from services.gpu_coordinator import PRIORITY_BACKGROUND
import services.llm_queue_service as q


@pytest.mark.asyncio
async def test_concurrent_jobs_share_one_acquisition(monkeypatch):
    acquisitions = {"n": 0}
    real_acquire = q.llm_queue.acquire

    async def counting_acquire(*a, **k):
        acquisitions["n"] += 1
        return await real_acquire(*a, **k)

    monkeypatch.setattr(q.llm_queue, "acquire", counting_acquire)
    q.llm_queue.active_workers = 0

    coord = MicroBatchCoordinator(priority=PRIORITY_BACKGROUND, window_ms=80, max_batch=16)

    async def job(i):
        async def _run():
            await asyncio.sleep(0.001)
            return i * 10
        return await coord.run(_run)

    # Fire 6 jobs "simultaneously" — they should batch into one acquisition.
    results = await asyncio.gather(*[job(i) for i in range(6)])
    assert sorted(results) == [0, 10, 20, 30, 40, 50]
    assert acquisitions["n"] == 1, f"expected 1 coalesced acquisition, got {acquisitions['n']}"


@pytest.mark.asyncio
async def test_one_failing_job_does_not_sink_the_batch(monkeypatch):
    q.llm_queue.active_workers = 0
    coord = MicroBatchCoordinator(priority=PRIORITY_BACKGROUND, window_ms=60, max_batch=16)

    async def good():
        return "ok"

    async def bad():
        raise ValueError("boom")

    async def run(fn):
        return await coord.run(fn)

    results = await asyncio.gather(run(good), run(bad), run(good), return_exceptions=True)
    assert results[0] == "ok"
    assert isinstance(results[1], ValueError)
    assert results[2] == "ok"


@pytest.mark.asyncio
async def test_window_zero_runs_solo(monkeypatch):
    acquisitions = {"n": 0}
    real_acquire = q.llm_queue.acquire

    async def counting_acquire(*a, **k):
        acquisitions["n"] += 1
        return await real_acquire(*a, **k)

    monkeypatch.setattr(q.llm_queue, "acquire", counting_acquire)
    q.llm_queue.active_workers = 0

    coord = MicroBatchCoordinator(priority=PRIORITY_BACKGROUND, window_ms=0, max_batch=8)

    async def job():
        return 1

    r = await coord.run(job)
    assert r == 1
    assert acquisitions["n"] == 1  # solo path acquires once for the single job


@pytest.mark.asyncio
async def test_max_batch_triggers_early_flush(monkeypatch):
    acquisitions = {"n": 0}
    real_acquire = q.llm_queue.acquire

    async def counting_acquire(*a, **k):
        acquisitions["n"] += 1
        return await real_acquire(*a, **k)

    monkeypatch.setattr(q.llm_queue, "acquire", counting_acquire)
    q.llm_queue.active_workers = 0

    # Long window, but max_batch=3 forces a flush at 3 without waiting.
    coord = MicroBatchCoordinator(priority=PRIORITY_BACKGROUND, window_ms=5000, max_batch=3)

    async def job(i):
        async def _run():
            return i
        return await coord.run(_run)

    # 3 jobs hit max → one flush; should NOT wait the full 5s window.
    results = await asyncio.wait_for(asyncio.gather(*[job(i) for i in range(3)]), timeout=2.0)
    assert sorted(results) == [0, 1, 2]
    assert acquisitions["n"] == 1


@pytest.mark.asyncio
async def test_batch_admission_is_bounded_before_worker_acquisition(monkeypatch):
    from services.local_inference_pool import InferenceOverloaded

    q.llm_queue.active_workers = 0
    coord = MicroBatchCoordinator(priority=PRIORITY_BACKGROUND, window_ms=5000, max_batch=8, max_pending=2)

    async def job():
        return "unused"

    tasks = [asyncio.create_task(coord.run(job)) for _ in range(2)]
    await asyncio.sleep(0)
    with pytest.raises(InferenceOverloaded):
        await coord.run(job)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # Resolve cancelled batch futures without running their closures.
    await coord._flush_now()
    assert coord._submitted == 0


@pytest.mark.asyncio
async def test_batch_declares_exact_model_and_releases_its_owned_ticket(monkeypatch):
    calls = []
    ticket = object()

    async def acquire(**kwargs):
        calls.append(kwargs)
        return ticket

    monkeypatch.setattr(q.llm_queue, "acquire", acquire)
    monkeypatch.setattr(q.llm_queue, "release", lambda owned: calls.append(owned))
    coord = MicroBatchCoordinator(priority=PRIORITY_BACKGROUND, window_ms=0, max_batch=1, model="synthetic-small")

    async def job():
        return "ok"

    assert await coord.run(job) == "ok"
    assert calls[0]["model"] == "synthetic-small"
    assert calls[1] is ticket


@pytest.mark.asyncio
async def test_cancelled_submitter_does_not_hide_its_running_job_from_admission(monkeypatch):
    from services.local_inference_pool import InferenceOverloaded

    q.llm_queue.active_workers = 0
    coord = MicroBatchCoordinator(priority=PRIORITY_BACKGROUND, window_ms=1, max_batch=8, max_pending=1)
    started, finish = asyncio.Event(), asyncio.Event()

    async def job():
        started.set()
        await finish.wait()

    submitter = asyncio.create_task(coord.run(job))
    await asyncio.wait_for(started.wait(), 1)
    submitter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submitter
    assert coord._submitted == 1
    with pytest.raises(InferenceOverloaded):
        await coord.run(job)
    flushes = tuple(coord._live_flushes)
    finish.set()
    await asyncio.gather(*flushes)
    assert coord._submitted == 0
