"""Retrieval admission follows real thread lifetime, not HTTP waiter lifetime."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from services.sage_retrieval_executor import (
    SageRetrievalBusy,
    SageRetrievalClosed,
    SageRetrievalExecutor,
    get_sage_retrieval_executor,
)


async def until(predicate: Any) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("value", ["0", "9", "true", "1.0", "", "01", "-1", "200"])
def test_invalid_explicit_worker_setting_is_refused(monkeypatch: Any, value: str) -> None:
    monkeypatch.setenv("ORB_SAGE_RETRIEVAL_WORKERS", value)
    with pytest.raises(ValueError, match="ORB_SAGE_RETRIEVAL_WORKERS"):
        SageRetrievalExecutor.from_env()


def test_app_default_is_four_generic_constructor_is_serial_and_threads_are_lazy(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("ORB_SAGE_RETRIEVAL_WORKERS", raising=False)
    executor = SageRetrievalExecutor.from_env()
    assert executor.workers == 4
    assert executor.max_outstanding == 200
    assert executor._pool is None
    executor.shutdown()
    generic = SageRetrievalExecutor()
    assert generic.workers == 1
    assert generic._pool is None
    generic.shutdown()
    for width in range(1, 9):
        monkeypatch.setenv("ORB_SAGE_RETRIEVAL_WORKERS", str(width))
        executor = SageRetrievalExecutor.from_env()
        assert executor.workers == width
        executor.shutdown()


@pytest.mark.parametrize(
    "option,value",
    [
        ("workers", True),
        ("workers", 1.2),
        ("workers", 0),
        ("workers", 9),
        ("max_outstanding", 0),
        ("max_outstanding", 201),
        ("max_outstanding", True),
    ],
)
def test_constructor_does_not_coerce_invalid_limits(option: str, value: Any) -> None:
    with pytest.raises(ValueError):
        SageRetrievalExecutor(**{option: value})


@pytest.mark.asyncio
async def test_200_bound_survives_running_cancellation_and_queued_jobs_never_execute() -> None:
    executor = SageRetrievalExecutor()
    entered, release = threading.Event(), threading.Event()
    ran: list[int] = []
    tasks: list[asyncio.Task[int]] = []

    def work(number: int) -> int:
        ran.append(number)
        entered.set()
        assert release.wait(3)
        return number

    try:
        tasks = [asyncio.create_task(executor.run(work, number)) for number in range(200)]
        await until(lambda: entered.is_set() and executor.metrics()["outstanding"] == 200)
        assert executor.metrics()["active"] == 1
        assert executor.metrics()["queued"] == 199
        with pytest.raises(SageRetrievalBusy):
            await executor.run(lambda: 201)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert executor.metrics()["outstanding"] == 200
        with pytest.raises(SageRetrievalBusy):
            await executor.run(lambda: 202)
        for task in tasks[1:]:
            task.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        assert executor.metrics()["queued"] == 0
        assert executor.metrics()["outstanding"] == 1
        assert ran == [0]
        release.set()
        await until(lambda: executor.metrics()["outstanding"] == 0)
        assert await executor.run(lambda: 203) == 203
        assert executor.metrics()["cancelled_queued"] == 199
        assert executor.metrics()["cancelled_running_waiters"] == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await executor.aclose()


@pytest.mark.asyncio
async def test_dispatched_cancellation_retains_slot_until_pool_wrapper_finishes(
    monkeypatch: Any,
) -> None:
    executor = SageRetrievalExecutor(max_outstanding=1)
    entered, release = threading.Event(), threading.Event()
    original = executor._invoke
    called: list[str] = []

    def delayed(job: Any) -> Any:
        entered.set()
        assert release.wait(3)
        return original(job)

    monkeypatch.setattr(executor, "_invoke", delayed)
    task = asyncio.create_task(executor.run(lambda: called.append("must not run")))
    try:
        await until(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.metrics()["outstanding"] == 1
        with pytest.raises(SageRetrievalBusy):
            await executor.run(lambda: None)
        release.set()
        await until(lambda: executor.metrics()["outstanding"] == 0)
        assert called == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await executor.aclose()


@pytest.mark.asyncio
async def test_shutdown_stops_dispatched_but_not_started_clinical_work(monkeypatch: Any) -> None:
    executor = SageRetrievalExecutor()
    entered, release = threading.Event(), threading.Event()
    original = executor._invoke
    calls: list[str] = []

    def delayed(job: Any) -> Any:
        entered.set()
        assert release.wait(3)
        return original(job)

    monkeypatch.setattr(executor, "_invoke", delayed)
    task = asyncio.create_task(executor.run(lambda: calls.append("must not run")))
    try:
        await until(entered.is_set)
        executor.shutdown(wait=False)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.metrics()["outstanding"] == 1
        release.set()
        await until(lambda: executor.metrics()["outstanding"] == 0)
        assert calls == []
        assert executor.metrics()["failed"] == 0
        assert executor.metrics()["cancelled_queued"] == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await executor.aclose()


def test_context_is_copied_and_executor_is_safe_across_event_loops() -> None:
    identity: contextvars.ContextVar[str] = contextvars.ContextVar(
        "synthetic-identity", default="outside"
    )
    executor = SageRetrievalExecutor(workers=2)

    async def scenario(prefix: str) -> None:
        async def call(number: int) -> tuple[str, str]:
            value = f"{prefix}-{number}"
            token = identity.set(value)
            try:
                return value, await executor.run(identity.get)
            finally:
                identity.reset(token)

        results = await asyncio.gather(*(call(number) for number in range(200)))
        assert all(expected == actual for expected, actual in results)
        assert await executor.run(identity.get) == "outside"

    try:
        asyncio.run(scenario("loop-one"))
        asyncio.run(scenario("loop-two"))
        assert executor.metrics()["completed"] == 402
    finally:
        executor.shutdown()


@pytest.mark.asyncio
async def test_errors_release_capacity_without_poisoning_later_work() -> None:
    executor = SageRetrievalExecutor(max_outstanding=1)

    def fail() -> None:
        raise ValueError("synthetic failure")

    try:
        with pytest.raises(ValueError, match="synthetic failure"):
            await executor.run(fail)
        assert executor.metrics()["outstanding"] == 0
        assert await executor.run(lambda: ("unchanged", {"clinical": "synthetic"})) == (
            "unchanged",
            {"clinical": "synthetic"},
        )
        metrics = executor.metrics()
        assert metrics["failed"] == 1
        assert "synthetic" not in str(metrics)
        assert metrics["queue_wait_avg_ms"] >= 0
        assert metrics["service_total_ms"] >= 0
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_async_functions_and_accidental_awaitable_results_are_rejected() -> None:
    executor = SageRetrievalExecutor()

    async def coroutine() -> int:
        return 1

    async def generator() -> Any:
        yield 1

    class AsyncCallable:
        async def __call__(self) -> int:
            return 1

    try:
        for function in (coroutine, generator, AsyncCallable()):
            with pytest.raises(TypeError, match="synchronous callable"):
                await executor.run(function)
        assert executor._pool is None
        with pytest.raises(TypeError, match="asynchronous work"):
            await executor.run(lambda: coroutine())
        with pytest.raises(TypeError, match="asynchronous work"):
            await executor.run(lambda: generator())
        assert executor.metrics()["outstanding"] == 0
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_close_cancels_queue_keeps_running_slot_and_does_not_block_loop() -> None:
    executor = SageRetrievalExecutor()
    entered, release = threading.Event(), threading.Event()

    def held() -> str:
        entered.set()
        assert release.wait(3)
        return "finished"

    running = asyncio.create_task(executor.run(held))
    queued: asyncio.Task[None] | None = None
    try:
        await until(entered.is_set)
        queued = asyncio.create_task(executor.run(lambda: None))
        await until(lambda: executor.metrics()["queued"] == 1)
        closing = asyncio.create_task(executor.aclose())
        await until(lambda: executor.closed)
        with pytest.raises(asyncio.CancelledError):
            await queued
        with pytest.raises(SageRetrievalClosed):
            await executor.run(lambda: None)
        assert executor.metrics()["outstanding"] == 1
        assert not closing.done()
        # Reaching this suspension confirms join has not blocked the loop.
        await asyncio.sleep(0)
        release.set()
        assert await running == "finished"
        await closing
        assert executor.metrics()["outstanding"] == 0
        await executor.aclose()
    finally:
        release.set()
        await asyncio.gather(running, *((queued,) if queued else ()), return_exceptions=True)
        await executor.aclose()


def test_lazy_app_state_is_isolated_and_reopens_after_lifespan_close(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("ORB_SAGE_RETRIEVAL_WORKERS", "1")
    first_app = SimpleNamespace(state=SimpleNamespace())
    second_app = SimpleNamespace(state=SimpleNamespace())
    first = get_sage_retrieval_executor(first_app)
    second = get_sage_retrieval_executor(second_app)
    assert first is get_sage_retrieval_executor(first_app)
    assert first is not second
    first.shutdown()
    replacement = get_sage_retrieval_executor(first_app)
    assert replacement is not first
    assert not replacement.closed
    replacement.shutdown()
    second.shutdown()


@pytest.mark.asyncio
async def test_app_reopen_cannot_multiply_capacity_while_old_threads_are_finishing() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    executor = get_sage_retrieval_executor(app)
    entered, release = threading.Event(), threading.Event()

    def held() -> None:
        entered.set()
        assert release.wait(3)

    task = asyncio.create_task(executor.run(held))
    try:
        await until(entered.is_set)
        executor.shutdown(wait=False)
        assert get_sage_retrieval_executor(app) is executor
        with pytest.raises(SageRetrievalClosed):
            await get_sage_retrieval_executor(app).run(lambda: None)
        release.set()
        await task
        replacement = get_sage_retrieval_executor(app)
        assert replacement is not executor
        await replacement.aclose()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await executor.aclose()
