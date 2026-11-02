"""Cancel one streaming operation when its HTTP reader disconnects.

The watcher runs while awaiting the next item, including queue admission and
model prefill before the first token. Every task and iterator belongs to this
one call. It never cancels a shared worker or another request's generation.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, TypeVar

import anyio

T = TypeVar("T")


class ClientDisconnected(asyncio.CancelledError):
    """The request ended, or its connection state could not be established."""


class _WatchControl:
    scope: anyio.CancelScope | None = None


async def _watch_disconnect(request: Any, interval: float, control: _WatchControl) -> None:
    # Request.is_disconnected and BaseHTTPMiddleware use nested AnyIO cancel
    # scopes/task groups. A raw Task.cancel can be swallowed during their
    # teardown. Cancel this containing AnyIO scope instead: its level-triggered
    # cancellation remains visible until all nested receive tasks have exited.
    with anyio.CancelScope() as scope:
        control.scope = scope
        while True:
            try:
                # A malformed/hung probe must not leave inference unobserved.
                with anyio.fail_after(interval):
                    disconnected = await request.is_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ClientDisconnected("HTTP connection state unavailable") from exc
            if type(disconnected) is not bool:
                raise ClientDisconnected("HTTP connection state unavailable")
            if disconnected:
                raise ClientDisconnected("HTTP reader disconnected")
            await asyncio.sleep(interval)


async def stream_until_disconnect(source: AsyncIterator[T], request: Any, *,
                                  poll_interval: float = 0.05) -> AsyncIterator[T]:
    """Yield unchanged source items; cooperatively close it on reader loss.

    Use ``aclosing`` at the call site when the caller may break out of the
    iteration early. ``ClientDisconnected`` is a ``CancelledError`` subclass
    so a disconnected request is never mistaken for a model failure.
    """
    if type(poll_interval) not in (int, float) or not 0 < poll_interval <= 1:
        raise ValueError("Disconnect polling interval must be greater than0 and at most1second")
    # Async-generator bodies and their aclose/finally must remain in ONE task.
    # Local inference leases and facility routing use ContextVars; making a new
    # task per anext loses mutations made before the preceding yield and makes
    # resetting the generator's original ContextVar tokens invalid.
    buffer: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1)

    async def produce() -> None:
        iterator = aiter(source)
        failure: BaseException | None = None
        try:
            async for item in iterator:
                await buffer.put(("item", item))
        except BaseException as exc:
            failure = exc
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                try:
                    # Cleanup runs in the same context as every source yield.
                    await close()
                except Exception as exc:
                    if failure is None:
                        failure = exc
        if isinstance(failure, asyncio.CancelledError):
            raise failure
        await buffer.put(("error", failure) if failure is not None else ("end", None))

    producer = asyncio.create_task(produce(), name="orb-request-stream-producer")
    control = _WatchControl()
    monitor = asyncio.create_task(_watch_disconnect(request, poll_interval, control), name="orb-request-disconnect")
    pending: asyncio.Task[tuple[str, Any]] | None = None

    try:
        while True:
            pending = asyncio.create_task(buffer.get(), name="orb-request-stream-buffer")
            await asyncio.wait({monitor, pending, producer}, return_when=asyncio.FIRST_COMPLETED)
            if monitor.done():
                # An independently cancelled/malformed watcher is not proof the
                # reader remains connected. External cancellation of THIS
                # coroutine propagates through the finally below unchanged.
                if monitor.cancelled():
                    raise ClientDisconnected("HTTP connection watcher stopped")
                monitor.result()
                raise ClientDisconnected("HTTP connection watcher stopped")
            if producer.done():
                # A source may itself cancel. Surface that original cancellation
                # rather than waiting for a terminal buffer item it cannot send.
                producer.result()
            kind, item = await pending
            pending = None
            if kind == "end":
                return
            if kind == "error":
                raise item
            yield item
    finally:
        owned = [monitor, producer] + ([pending] if pending is not None else [])
        for task in owned:
            if not task.done():
                if task is monitor and control.scope is not None:
                    control.scope.cancel()
                else:
                    task.cancel()
        # Never aclose the raw source here: its ContextVar tokens belong to the
        # producer. Cancellation enters that task's finally and closes it there.
        # StreamingResponse can cancel its containing AnyIO scope repeatedly.
        # Shield only this teardown so a second cancellation cannot interrupt
        # the producer's lease release or abandon a nested receive task.
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*owned, return_exceptions=True)
