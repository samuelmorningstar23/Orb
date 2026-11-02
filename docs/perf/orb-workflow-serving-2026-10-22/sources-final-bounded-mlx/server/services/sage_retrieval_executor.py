"""Bounded synchronous retrieval, isolated from HTTP and durable-write pools.

Only ``workers`` jobs are submitted to ThreadPoolExecutor at once. Remaining
jobs live in an application-owned bounded queue, so cancelling queued work
actually removes its context/arguments instead of leaving unbounded cancelled
work items in ThreadPoolExecutor's private queue. Running synchronous work
cannot be interrupted: its admission slot remains held until physical finish.
No clinical input, result or callable name is retained in metrics.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar, cast

P = ParamSpec("P")
T = TypeVar("T")


class SageRetrievalBusy(RuntimeError):
    """The active plus queued retrieval bound has been reached."""


class SageRetrievalClosed(RuntimeError):
    """The owning application has closed this retrieval executor."""


@dataclass(eq=False)
class _Job:
    result: Future[Any]
    submitted_at: float
    invoke: Callable[[], Any] | None
    phase: str = "queued"
    worker: Future[Any] | None = None
    started_at: float | None = None


class SageRetrievalExecutor:
    """A loop-independent, per-application executor with at most 200 jobs."""

    def __init__(self, workers: int = 1, max_outstanding: int = 200) -> None:
        if type(workers) is not int or not 1 <= workers <= 8:
            raise ValueError("Sage retrieval workers must be an integer from 1 to 8")
        if type(max_outstanding) is not int or not 1 <= max_outstanding <= 200:
            raise ValueError("Sage retrieval admission must be an integer from 1 to 200")
        self.workers = workers
        self.max_outstanding = max_outstanding
        self._lock = threading.RLock()
        self._pool: ThreadPoolExecutor | None = None
        self._closed = False
        self._queue: deque[_Job] = deque()
        self._jobs: set[_Job] = set()
        self._active = 0
        self._admitted = 0
        self._rejected = 0
        self._completed = 0
        self._failed = 0
        self._cancelled_queued = 0
        self._cancelled_running = 0
        self._started = 0
        self._queue_wait_ms = 0.0
        self._queue_wait_max_ms = 0.0
        self._service_ms = 0.0
        self._service_max_ms = 0.0

    @classmethod
    def from_env(cls) -> SageRetrievalExecutor:
        """Fail closed on invalid explicit settings; leave thread startup lazy."""
        # Full-RAG comparison: width 4 completed the 200-request burst in
        # 26.821s versus width 1's 27.943s, with unchanged retrieval quality.
        # The chart-only width-1 result did not include blocking knowledge
        # worker I/O. This default still fails the requested latency targets;
        # deployments must qualify their own complete workload and hardware.
        raw = os.environ.get("ORB_SAGE_RETRIEVAL_WORKERS", "4").strip()
        if raw not in {str(number) for number in range(1, 9)}:
            raise ValueError("ORB_SAGE_RETRIEVAL_WORKERS must be an integer from 1 to 8")
        return cls(workers=int(raw))

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    async def run(self, function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        """Run the unchanged synchronous retrieval with the caller's context.

        Cancelling the await removes queued work. A dispatched/running thread
        keeps its bound until physical completion, even after its caller has
        gone. No async function, coroutine result or async generator is accepted.
        """
        if (
            not callable(function)
            or inspect.iscoroutinefunction(function)
            or inspect.isasyncgenfunction(function)
            or inspect.iscoroutinefunction(type(function).__call__)
            or inspect.isasyncgenfunction(type(function).__call__)
        ):
            raise TypeError("Sage retrieval requires a synchronous callable")
        context = contextvars.copy_context()
        call = functools.partial(function, *args, **kwargs)
        job = _Job(Future(), time.monotonic(), functools.partial(context.run, call))
        with self._lock:
            if self._closed:
                raise SageRetrievalClosed("Sage retrieval is closed")
            if len(self._jobs) >= self.max_outstanding:
                self._rejected += 1
                raise SageRetrievalBusy("Sage retrieval admission is full")
            self._admitted += 1
            self._jobs.add(job)
            self._queue.append(job)
            job.result.add_done_callback(lambda future: self._logical_done(job, future))
            self._dispatch_locked()
        wrapped = asyncio.wrap_future(job.result)
        try:
            return cast(T, await asyncio.shield(wrapped))
        except asyncio.CancelledError:
            with self._lock:
                if not job.result.cancel() and job in self._jobs:
                    self._cancelled_running += 1
            # Consume the loop-side future as well. The physical worker owns
            # admission release, so cancelling this wrapper cannot free its slot.
            wrapped.cancel()
            raise

    def _dispatch_locked(self) -> None:
        while not self._closed and self._queue and self._active < self.workers:
            job = self._queue.popleft()
            if job.result.cancelled():
                continue
            job.phase = "dispatched"
            self._active += 1
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self.workers, thread_name_prefix="orb-sage-retrieval"
                )
            try:
                job.worker = self._pool.submit(self._invoke, job)
            except BaseException as exc:  # noqa: BLE001 - transfer and release every failed Future
                job.result.set_exception(exc)
                self._failed += 1
                self._finish_locked(job)
                continue
            job.worker.add_done_callback(functools.partial(self._worker_done, job))

    def _invoke(self, job: _Job) -> Any:
        with self._lock:
            if not job.result.set_running_or_notify_cancel():
                return None
            job.phase = "running"
            job.started_at = time.monotonic()
            wait_ms = (job.started_at - job.submitted_at) * 1000
            self._started += 1
            self._queue_wait_ms += wait_ms
            self._queue_wait_max_ms = max(self._queue_wait_max_ms, wait_ms)
            call = job.invoke
        if call is None:
            raise RuntimeError("Sage retrieval invocation was unavailable")
        value = call()
        if inspect.isawaitable(value) or inspect.isasyncgen(value):
            if inspect.iscoroutine(value):
                value.close()
            raise TypeError("Sage retrieval callable returned asynchronous work")
        return value

    def _logical_done(self, job: _Job, result: Future[Any]) -> None:
        if not result.cancelled():
            return  # A physical completion callback publishes success/errors.
        with self._lock:
            if job not in self._jobs:
                return
            if job.phase == "queued":
                self._queue.remove(job)
                self._cancelled_queued += 1
                self._finish_locked(job)
            elif job.worker is not None:
                # If the pool has not started it, its callback finishes the
                # physical slot. Otherwise _invoke observes this cancellation.
                job.worker.cancel()

    def _worker_done(self, job: _Job, worker: Future[Any]) -> None:
        with self._lock:
            if job not in self._jobs:
                return
            if job.started_at is not None:
                service_ms = (time.monotonic() - job.started_at) * 1000
                self._service_ms += service_ms
                self._service_max_ms = max(self._service_max_ms, service_ms)
            if not job.result.cancelled():
                try:
                    value = worker.result()
                except BaseException as exc:  # noqa: BLE001 - a Future may contain BaseException
                    self._failed += 1
                    job.result.set_exception(exc)
                else:
                    self._completed += 1
                    job.result.set_result(value)
            else:
                self._cancelled_queued += 1
            self._finish_locked(job)

    def _finish_locked(self, job: _Job) -> None:
        if job not in self._jobs:
            return
        self._jobs.remove(job)
        if job.phase != "queued":
            self._active -= 1
        job.phase = "finished"
        job.invoke = None
        job.worker = None
        self._dispatch_locked()

    def metrics(self) -> dict[str, int | float | bool]:
        """Coarse counts and timing only; no query, patient or callable labels."""
        with self._lock:
            return {
                "workers": self.workers,
                "max_outstanding": self.max_outstanding,
                "active": self._active,
                "queued": len(self._queue),
                "outstanding": len(self._jobs),
                "closed": self._closed,
                "admitted": self._admitted,
                "rejected": self._rejected,
                "completed": self._completed,
                "failed": self._failed,
                "cancelled_queued": self._cancelled_queued,
                "cancelled_running_waiters": self._cancelled_running,
                "started": self._started,
                "queue_wait_avg_ms": self._queue_wait_ms / self._started if self._started else 0.0,
                "queue_wait_max_ms": self._queue_wait_max_ms,
                "service_total_ms": self._service_ms,
                "service_max_ms": self._service_max_ms,
            }

    def shutdown(self, *, wait: bool = True) -> None:
        """Close admission, remove queued jobs, and optionally join real workers."""
        with self._lock:
            self._closed = True
            for job in tuple(self._jobs):
                # A submitted pool wrapper can still be waiting to acquire
                # this lock before it marks the logical Future running. It
                # must also skip the clinical function when shutdown wins.
                if job.phase in ("queued", "dispatched"):
                    job.result.cancel()
            pool = self._pool
        if pool is not None:
            pool.shutdown(wait=wait, cancel_futures=True)

    async def aclose(self) -> None:
        """Keep shutdown/join off the application's event loop."""
        self.shutdown(wait=False)
        pool = self._pool
        if pool is not None:
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


_app_state_lock = threading.Lock()


def get_sage_retrieval_executor(app: Any) -> SageRetrievalExecutor:
    """Lazy per-app state also supports clients that do not run ASGI lifespan."""
    with _app_state_lock:
        executor = getattr(app.state, "sage_retrieval_executor", None)
        # A cancelled lifespan may still have an uninterruptible clinical
        # reader finishing. Do not manufacture a second pool and admission
        # budget while those physical jobs still exist.
        if (
            isinstance(executor, SageRetrievalExecutor)
            and executor.closed
            and executor.metrics()["outstanding"]
        ):
            return executor
        if not isinstance(executor, SageRetrievalExecutor) or executor.closed:
            executor = SageRetrievalExecutor.from_env()
            app.state.sage_retrieval_executor = executor
        return executor
