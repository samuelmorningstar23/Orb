"""No models or network: request-scoped stream cancellation contracts."""
from __future__ import annotations

import asyncio
import contextvars
from contextlib import aclosing
from types import SimpleNamespace

import anyio
import pytest
from services.disconnect_stream import ClientDisconnected, stream_until_disconnect


class Request:
    disconnected = False

    async def is_disconnected(self):
        return self.disconnected


@pytest.mark.asyncio
async def test_blocked_provider_before_first_token_cleans_up_and_releases_slot():
    request, entered, cleaned = Request(), asyncio.Event(), asyncio.Event()
    slot = asyncio.Semaphore(1)
    async def source():
        async with slot:
            try:
                entered.set()
                await asyncio.Event().wait()
                yield "unreachable"
            finally:
                cleaned.set()
    task = asyncio.create_task(anext(stream_until_disconnect(source(), request, poll_interval=.005)))
    await entered.wait()
    request.disconnected = True
    with pytest.raises(ClientDisconnected):
        await asyncio.wait_for(task, 1)
    assert cleaned.is_set()
    await asyncio.wait_for(slot.acquire(), 1)
    slot.release()


@pytest.mark.asyncio
async def test_disconnect_only_cancels_its_own_generator():
    first, second, ready, release = Request(), Request(), asyncio.Event(), asyncio.Event()
    ended = set()
    async def source(name):
        try:
            ready.set()
            await release.wait()
            yield name
        finally:
            ended.add(name)
    t1 = asyncio.create_task(anext(stream_until_disconnect(source("first"), first, poll_interval=.005)))
    iterator2 = stream_until_disconnect(source("second"), second, poll_interval=.005)
    t2 = asyncio.create_task(anext(iterator2))
    await ready.wait()
    first.disconnected = True
    with pytest.raises(ClientDisconnected):
        await asyncio.wait_for(t1, 1)
    assert not t2.done() and "second" not in ended
    release.set()
    assert await asyncio.wait_for(t2, 1) == "second"
    await iterator2.aclose()
    assert ended == {"first", "second"}


@pytest.mark.asyncio
async def test_external_cancellation_propagates_and_closes_source():
    entered, closed = asyncio.Event(), asyncio.Event()
    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            closed.set()
    task = asyncio.create_task(anext(stream_until_disconnect(source(), Request(), poll_interval=.005)))
    await entered.wait()
    task.cancel("shutdown")
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert not isinstance(error.value, ClientDisconnected)
    assert error.value.args == ("shutdown",) and closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["truthy-string", "exception", "sync-bool", "hung", "cancelled-probe"])
async def test_malformed_connection_probe_fails_safe_with_request_scoped_cancellation(mode):
    closed = asyncio.Event()
    async def source():
        try:
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            closed.set()
    async def probe():
        if mode == "truthy-string":
            return "false"
        if mode == "exception":
            raise RuntimeError("probe failure")
        if mode == "cancelled-probe":
            raise asyncio.CancelledError()
        await asyncio.Event().wait()
    request = SimpleNamespace(is_disconnected=(lambda: False) if mode == "sync-bool" else probe)
    with pytest.raises(ClientDisconnected):
        await asyncio.wait_for(anext(stream_until_disconnect(source(), request, poll_interval=.005)), 1)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_normal_stream_preserves_values_and_completion_and_closes_monitor():
    request, closed = Request(), asyncio.Event()
    objects = [object(), object(), "unchanged"]
    async def source():
        try:
            for value in objects:
                await asyncio.sleep(0)
                yield value
        finally:
            closed.set()
    assert [value async for value in stream_until_disconnect(source(), request, poll_interval=.005)] == objects
    assert closed.is_set()
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("orb-request-")]


@pytest.mark.asyncio
async def test_early_consumer_break_with_aclosing_retires_only_its_iterator():
    closed = asyncio.Event()
    async def source():
        try:
            yield "first"
            await asyncio.Event().wait()
        finally:
            closed.set()
    async with aclosing(stream_until_disconnect(source(), Request(), poll_interval=.005)) as stream:
        async for value in stream:
            assert value == "first"
            break
    assert closed.is_set()


@pytest.mark.asyncio
async def test_source_error_propagates_without_becoming_disconnect():
    async def source():
        raise RuntimeError("source error")
        yield "unreachable"
    with pytest.raises(RuntimeError, match="source error"):
        await anext(stream_until_disconnect(source(), Request(), poll_interval=.005))


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_replace_external_cancellation():
    entered = asyncio.Event()
    class Iterator:
        def __aiter__(self):
            return self
        async def __anext__(self):
            entered.set()
            await asyncio.Event().wait()
        async def aclose(self):
            raise RuntimeError("cleanup failure")
    task = asyncio.create_task(anext(stream_until_disconnect(Iterator(), Request(), poll_interval=.005)))
    await entered.wait()
    task.cancel("shutdown")
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert error.value.args == ("shutdown",)


@pytest.mark.asyncio
async def test_one_producer_preserves_contextvars_across_yields_and_resets_in_own_finally():
    facility = contextvars.ContextVar("test_facility", default="outside")
    observed, cleanup = [], []
    async def source():
        token = facility.set("request-facility")
        try:
            observed.append(("before-first", facility.get(), asyncio.current_task()))
            yield "first"
            await asyncio.sleep(0)
            observed.append(("before-second", facility.get(), asyncio.current_task()))
            yield "second"
        finally:
            cleanup.append((facility.get(), asyncio.current_task()))
            facility.reset(token)
            cleanup.append((facility.get(), asyncio.current_task()))
    assert [item async for item in stream_until_disconnect(source(), Request(), poll_interval=.005)] == ["first", "second"]
    assert [row[1] for row in observed] == ["request-facility", "request-facility"]
    producer = observed[0][2]
    assert producer is not asyncio.current_task()
    assert observed[1][2] is producer
    assert cleanup == [("request-facility", producer), ("outside", producer)]
    assert facility.get() == "outside"


@pytest.mark.asyncio
async def test_disconnect_resets_source_contextvar_and_releases_lease_inside_same_producer():
    worker = contextvars.ContextVar("test_worker_origin", default="production")
    request, entered, cleaned = Request(), asyncio.Event(), asyncio.Event()
    tasks, values = [], []
    async def source():
        token = worker.set("owned-benchmark-worker")
        try:
            tasks.append(asyncio.current_task())
            yield worker.get()
            values.append(worker.get())
            entered.set()
            await asyncio.Event().wait()
        finally:
            tasks.append(asyncio.current_task())
            values.append(worker.get())
            worker.reset(token)
            values.append(worker.get())
            cleaned.set()
    stream = stream_until_disconnect(source(), request, poll_interval=.005)
    assert await anext(stream) == "owned-benchmark-worker"
    await entered.wait()
    pending = asyncio.create_task(anext(stream))
    request.disconnected = True
    with pytest.raises(ClientDisconnected):
        await asyncio.wait_for(pending, 1)
    assert cleaned.is_set() and tasks[0] is tasks[1]
    assert tasks[0] is not asyncio.current_task()
    assert values == ["owned-benchmark-worker", "owned-benchmark-worker", "production"]
    assert worker.get() == "production"


@pytest.mark.asyncio
async def test_concurrent_streams_do_not_share_worker_context_or_cleanup_tokens():
    worker = contextvars.ContextVar("test_scoped_worker", default="production")
    release = asyncio.Event()
    observed = []
    async def source(origin):
        token = worker.set(origin)
        try:
            yield worker.get()
            await release.wait()
            yield worker.get()
        finally:
            observed.append((origin, worker.get()))
            worker.reset(token)
    first = stream_until_disconnect(source("worker-one"), Request(), poll_interval=.005)
    second = stream_until_disconnect(source("worker-two"), Request(), poll_interval=.005)
    assert await anext(first) == "worker-one"
    assert await anext(second) == "worker-two"
    release.set()
    assert await anext(first) == "worker-one"
    assert await anext(second) == "worker-two"
    await first.aclose()
    await second.aclose()
    assert sorted(observed) == [("worker-one", "worker-one"), ("worker-two", "worker-two")]
    assert worker.get() == "production"


@pytest.mark.asyncio
async def test_source_cancelled_error_remains_cancelled_error_without_hanging_on_empty_buffer():
    async def source():
        raise asyncio.CancelledError("provider cancelled")
        yield "unreachable"
    with pytest.raises(asyncio.CancelledError) as error:
        await asyncio.wait_for(anext(stream_until_disconnect(source(), Request(), poll_interval=.005)), 1)
    assert error.value.args == ("provider cancelled",)


@pytest.mark.asyncio
async def test_anyio_scope_cancellation_waits_for_owned_producer_cleanup():
    entered, cleaned = asyncio.Event(), asyncio.Event()
    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            await asyncio.sleep(.01)
            cleaned.set()
    async def consume():
        async for _ in stream_until_disconnect(source(), Request(), poll_interval=.005):
            pass
    async with anyio.create_task_group() as group:
        group.start_soon(consume)
        await entered.wait()
        group.cancel_scope.cancel()
    assert cleaned.is_set()
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("orb-request-")]


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [0, -1, 2, True, "0.05", float("inf"), float("nan")])
async def test_invalid_polling_interval_refuses_before_consuming_source(interval):
    touched = False
    async def source():
        nonlocal touched
        touched = True
        yield "unreachable"
    with pytest.raises(ValueError):
        await anext(stream_until_disconnect(source(), Request(), poll_interval=interval))
    assert not touched
