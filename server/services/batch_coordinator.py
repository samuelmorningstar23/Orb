"""Orb — Micro-batching coordinator for ambient LLM work.

The single-GPU slot (llm_queue.max_concurrent=1 behind the GPU gate) is the hard
concurrency wall. Under a Relay message burst, each ambient classification used
to acquire and release the queue INDIVIDUALLY at background priority, so N
messages meant N separate acquisitions all contending with interactive Sage.

This coordinator coalesces ambient jobs that arrive within a short window into
ONE queue acquisition: it buffers submitted jobs for up to AMBIENT_BATCH_WINDOW_MS
(or until AMBIENT_BATCH_MAX accumulate), then acquires the LLM queue ONCE at
background priority and runs the buffered jobs back-to-back while holding the
slot, before releasing. The interactive lane still preempts between batches (the
queue is priority-ordered and a batch holds the slot only for its own jobs), so
clinicians are never blocked behind a long ambient backlog — but the per-job
acquire/release churn (and its contention) collapses from N to 1 per window.

Each job is an async callable that performs its own model call; the coordinator
only owns the queue lifecycle and result fan-out. A job that raises has its
exception delivered to its own awaiter — one bad job never sinks the batch.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("orb.batch")


class _AdmittedJob:
    """Keep a cancelled submitter's still-running job inside the admission cap."""

    def __init__(self, owner: MicroBatchCoordinator, job: Callable[[], Awaitable[Any]]) -> None:
        self.owner = owner
        self.job = job
        self.queued = False
        self.retired = False

    def retire(self) -> None:
        if not self.retired:
            self.retired = True
            self.owner._submitted -= 1

    async def __call__(self) -> Any:
        try:
            return await self.job()
        finally:
            self.retire()


class MicroBatchCoordinator:
    def __init__(self, priority: int, window_ms: int, max_batch: int, timeout: float = 45.0,
                 *, model: str | None = None, max_pending: int = 200):
        self._priority = priority
        self._window = max(0, window_ms) / 1000.0
        self._max = max(1, max_batch)
        self._timeout = timeout
        self._model = model
        if type(max_pending) is not int or not 1 <= max_pending <= 200:
            raise ValueError("Ambient batch admission must be bounded at 1..200 jobs")
        self._max_pending = max_pending
        self._submitted = 0
        self._pending: list[tuple[_AdmittedJob, asyncio.Future[Any]]] = []
        self._flush_task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None
        # Strong refs to in-flight flush tasks. The event loop only keeps WEAK
        # references to tasks; `_flush_now` clears `self._flush_task` while the
        # flush task is still running, so without this set a GC pass during the
        # flush's awaits can destroy the task mid-flight — its batch futures
        # then never resolve and every submitted job awaits forever (observed
        # as the test suite hanging at 0% CPU; the same hang could hit ambient
        # callers in production).
        self._live_flushes: set[asyncio.Task] = set()

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def run(self, job: Callable[[], Awaitable]):
        """Submit an async job; returns its result once the batch runs. If
        batching is disabled (window 0) the job runs immediately under its own
        queue acquisition."""
        from services.local_inference_pool import InferenceOverloaded

        # Bound all live job futures, including jobs in a running batch; a
        # small number of worker leases must not hide an unbounded job backlog.
        if self._submitted >= self._max_pending:
            raise InferenceOverloaded("Ambient batch admission is full; retry later")
        self._submitted += 1
        admitted = _AdmittedJob(self, job)
        try:
            return await self._run(admitted)
        finally:
            if not admitted.queued:
                admitted.retire()

    async def _run(self, job: _AdmittedJob) -> Any:
        if self._window == 0:
            return await self._run_solo(job)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        async with self.lock:
            self._pending.append((job, fut))
            job.queued = True
            over_max = len(self._pending) >= self._max
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_after_window())
                self._live_flushes.add(self._flush_task)
                self._flush_task.add_done_callback(self._live_flushes.discard)
        if over_max:
            # Trip an immediate flush rather than waiting out the window.
            await self._flush_now()
        return await fut

    async def _flush_after_window(self):
        await asyncio.sleep(self._window)
        await self._flush_now()

    async def _flush_now(self):
        async with self.lock:
            batch = self._pending
            self._pending = []
            # Cancel a PENDING timer-flush when an over_max flush preempts it —
            # but never the task we are currently running inside. In the timer
            # path `self._flush_task` IS the current task; cancelling it left a
            # pending self-cancellation that landed at a later await (often the
            # job loop), where CancelledError slipped past `except Exception`
            # and stranded every batch future — callers then awaited forever.
            current = asyncio.current_task()
            if self._flush_task and self._flush_task is not current and not self._flush_task.done():
                self._flush_task.cancel()
            self._flush_task = None
        if not batch:
            return

        from services.llm_queue_service import BackgroundAdmissionRejected, llm_queue

        acquired = False
        ticket = None
        try:
            model_kw = {"model": self._model} if self._model is not None else {}
            ticket = await llm_queue.acquire(priority=self._priority, timeout=self._timeout, **model_kw)
            acquired = True
        except BackgroundAdmissionRejected as e:
            # The governor shed the COALESCED acquisition. Must be caught
            # before the TimeoutError arm below (it is a subclass): every
            # batched future gets THE rejection, so callers can tell a
            # governor shed from a busy slot — and none is ever stranded.
            for job, fut in batch:
                job.retire()
                if not fut.done():
                    fut.set_exception(e)
            return
        except asyncio.CancelledError:
            # We were CANCELLED while waiting for the slot — shutdown, almost
            # always. Resolve the batch (strand nothing) and then RE-RAISE.
            # This arm used to be merged with the TimeoutError one below and
            # `return`ed, which swallowed the cancellation: the flush task then
            # refused to die, whoever awaited it (a stop(), a shutdown, an
            # `asyncio.gather`) waited forever, and the process hung at
            # "Waiting for application shutdown." with the listen socket still
            # held. CancelledError must ALWAYS propagate.
            for job, fut in batch:
                job.retire()
                if not fut.done():
                    # Never hand a CancelledError to the submitter: their own
                    # task was not cancelled, and awaiting a future that raises
                    # one makes them look cancelled to everything above them.
                    fut.set_exception(
                        TimeoutError("ambient batch cancelled while acquiring LLM slot")
                    )
            raise
        except TimeoutError:
            # Couldn't get the slot in time — fail every job in the batch (they
            # are background work; the caller decides what to do with None).
            for job, fut in batch:
                job.retire()
                if not fut.done():
                    fut.set_exception(TimeoutError("ambient batch could not acquire LLM slot"))
            return
        except BaseException as e:
            # ANY other acquire failure must also fail the batch's futures —
            # otherwise the flush task dies with the exception while every
            # submitted job awaits its future forever (a silent permanent hang
            # for ambient callers; also froze the test suite). It is re-raised
            # unchanged: a BaseException (KeyboardInterrupt, SystemExit) is the
            # process going down and is never this coordinator's to absorb.
            for job, fut in batch:
                job.retire()
                if not fut.done():
                    fut.set_exception(
                        e
                        if isinstance(e, Exception)
                        else RuntimeError(f"ambient batch aborted: {type(e).__name__}")
                    )
            raise

        try:
            logger.debug("[Batch] running %s ambient job(s) under one acquisition", len(batch))
            for job, fut in batch:
                if fut.cancelled():
                    job.retire()
                    continue
                try:
                    result = await job()
                    if not fut.done():
                        fut.set_result(result)
                except Exception as e:
                    # Exception, NOT BaseException, on purpose: one bad job
                    # must not sink the batch, but a CancelledError arriving
                    # here is not this job failing — it is us being shut down,
                    # and it belongs to the arm below. (It is a BaseException
                    # in 3.12, so it already passes this arm by; spelling that
                    # out because widening this to BaseException would silently
                    # make the flush task uncancellable.)
                    if not fut.done():
                        fut.set_exception(e)
        except BaseException as e:
            # CancelledError (or any other BaseException) escaping the job loop
            # must still resolve the remaining futures — otherwise their
            # submitters await forever — and must then be RE-RAISED, or this
            # task cannot be cancelled and whoever awaits it hangs for good.
            for job, fut in batch:
                job.retire()
                if not fut.done():
                    fut.set_exception(
                        e
                        if isinstance(e, Exception)
                        else TimeoutError("ambient batch flush was cancelled")
                    )
            raise
        finally:
            if acquired:
                llm_queue.release(ticket)

    async def _run_solo(self, job: Callable[[], Awaitable[Any]]) -> Any:
        from services.llm_queue_service import llm_queue

        model_kw = {"model": self._model} if self._model is not None else {}
        ticket = await llm_queue.acquire(priority=self._priority, timeout=self._timeout, **model_kw)
        try:
            return await job()
        finally:
            llm_queue.release(ticket)


# Singleton for the ambient lane (background priority). Lazily configured from
# env at first import so tests can monkeypatch config before use.
_ambient_coordinator: MicroBatchCoordinator | None = None


def ambient_coordinator() -> MicroBatchCoordinator:
    global _ambient_coordinator
    if _ambient_coordinator is None:
        from config import AMBIENT_BATCH_MAX, AMBIENT_BATCH_WINDOW_MS, AMBIENT_LLM_MODEL
        from services.gpu_coordinator import PRIORITY_BACKGROUND

        _ambient_coordinator = MicroBatchCoordinator(
            priority=PRIORITY_BACKGROUND,
            window_ms=AMBIENT_BATCH_WINDOW_MS,
            max_batch=AMBIENT_BATCH_MAX,
            model=AMBIENT_LLM_MODEL,
        )
    return _ambient_coordinator
