"""Bounded admission for explicitly qualified, loopback-only model workers.

This is a capacity scheduler, not a claim that an unmeasured machine can run
200 generations. The default is disabled. Each configured capacity must come
from an operator's load qualification; increasing the HTTP admission ceiling
does not increase model throughput. No prompts, patient data or HTTP clients
are retained here. All mutations occur synchronously on one asyncio loop.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Self
from urllib.parse import urlsplit

from offline_policy import local_model_origin


class InferenceOverloaded(asyncio.TimeoutError):
    """Bounded admission is full; callers should return an explicit busy result."""


class InferenceDeadlineExceeded(asyncio.TimeoutError):
    """No qualified slot was available within the caller's remaining budget."""


class InferenceWorkerUnavailable(asyncio.TimeoutError):
    """The requested model/lane has no healthy configured worker."""


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    origin: str
    models: tuple[str, ...]
    capacity: int = 1
    lanes: tuple[str, ...] = ("interactive",)
    interactive_reserve: int = 0
    qualification: str = ""

    def __post_init__(self) -> None:
        # Generic errors deliberately never echo a URL, environment value,
        # token, model payload or operator-supplied qualification document.
        invalid = ValueError("Invalid qualified local inference worker configuration")
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.name):
            raise invalid
        object.__setattr__(self, "origin", local_model_origin(self.origin))
        if (
            not isinstance(self.models, (tuple, list))
            or not self.models
            or any(
                not isinstance(m, str)
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@+-]{0,159}", m)
                for m in self.models
            )
            or len(set(self.models)) != len(self.models)
            or type(self.capacity) is not int
            or not 1 <= self.capacity <= 200
            or not isinstance(self.lanes, (tuple, list))
            or not self.lanes
            or set(self.lanes) - {"interactive", "background"}
            or len(set(self.lanes)) != len(self.lanes)
            or type(self.interactive_reserve) is not int
            or not 0 <= self.interactive_reserve <= self.capacity
            or not isinstance(self.qualification, str)
            or not self.qualification.strip()
            or len(self.qualification) > 256
        ):
            raise invalid
        if set(self.lanes) == {"interactive", "background"} and self.interactive_reserve < 1:
            raise invalid
        if "interactive" not in self.lanes and self.interactive_reserve:
            raise invalid
        object.__setattr__(self, "models", tuple(self.models))
        object.__setattr__(self, "lanes", tuple(self.lanes))


@dataclass
class _WorkerState:
    spec: WorkerSpec
    active: int = 0
    background: int = 0
    failures: int = 0
    unavailable_until: float = 0.0
    probe_active: bool = False


@dataclass
class _Waiter:
    model: str
    lane: str
    priority: int
    sequence: int
    future: asyncio.Future[WorkerLease]


class WorkerLease:
    """One idempotently released physical worker slot, held through stream close."""

    def __init__(
        self,
        pool: LocalInferencePool,
        state: _WorkerState,
        model: str,
        lane: str,
        probe: bool,
    ) -> None:
        self._pool = pool
        self._state = state
        self.model = model
        self.lane = lane
        self.probe = probe
        self.released = False
        self._outcome_recorded = False

    @property
    def worker(self) -> WorkerSpec:
        return self._state.spec

    @property
    def origin(self) -> str:
        return self.worker.origin

    def record_failure(self) -> None:
        """Transport/daemon failure only; never classify clinical content here."""
        if self.released or self._outcome_recorded:
            return
        self._outcome_recorded = True
        state = self._state
        state.failures += 1
        if state.failures >= self._pool.failure_threshold:
            state.unavailable_until = self._pool.clock() + self._pool.cooldown_seconds

    def record_success(self) -> None:
        if self.released or self._outcome_recorded:
            return
        self._outcome_recorded = True
        if not self.probe and self._state.failures >= self._pool.failure_threshold:
            # An older concurrent request finishing after the circuit opened
            # is not a recovery probe and cannot close that circuit early.
            return
        self._state.failures = 0
        self._state.unavailable_until = 0.0

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        state = self._state
        state.active -= 1
        if self.lane == "background":
            state.background -= 1
        if self.probe:
            state.probe_active = False
            if not self._outcome_recorded:
                # A cancelled probe is not evidence that the daemon recovered.
                state.unavailable_until = self._pool.clock() + self._pool.cooldown_seconds
        self._pool._dispatch()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.release()


class LocalInferencePool:
    def __init__(
        self,
        workers: Sequence[WorkerSpec],
        *,
        max_inflight: int = 200,
        failure_threshold: int = 2,
        cooldown_seconds: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not workers
            or len(workers) > 64
            or type(max_inflight) is not int
            or not 1 <= max_inflight <= 200
            or type(failure_threshold) is not int
            or failure_threshold < 1
            or not isinstance(cooldown_seconds, (int, float))
            or not math.isfinite(cooldown_seconds)
            or cooldown_seconds <= 0
        ):
            raise ValueError("Invalid local inference pool limits")
        daemon_ports = {
            urlsplit(w.origin).port or (443 if urlsplit(w.origin).scheme == "https" else 80)
            for w in workers
        }
        if len({w.name for w in workers}) != len(workers) or len(daemon_ports) != len(workers):
            # Two aliases for one daemon must never manufacture extra slots.
            raise ValueError("Local inference workers must have distinct names and loopback ports")
        if not any("interactive" in w.lanes for w in workers):
            raise ValueError("A local inference pool requires an interactive worker")
        self._workers = [_WorkerState(w) for w in workers]
        self.max_inflight = max_inflight
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = float(cooldown_seconds)
        self.clock = clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: list[_Waiter] = []
        self._sequence = 0
        self.rejected = 0
        self.deadlines = 0

    @property
    def interactive_capacity(self) -> int:
        return min(
            self.max_inflight,
            sum(w.spec.capacity for w in self._workers if "interactive" in w.spec.lanes),
        )

    @property
    def background_capacity(self) -> int:
        return min(
            self.max_inflight,
            sum(
                w.spec.capacity - w.spec.interactive_reserve
                for w in self._workers
                if "background" in w.spec.lanes
            ),
        )

    @property
    def active(self) -> int:
        return sum(w.active for w in self._workers)

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def snapshot(self) -> dict[str, object]:
        """Operational counts only: never prompt, patient, URL or credentials."""
        return {
            "configured": True,
            "qualified_capacity": self.interactive_capacity,
            "max_inflight": self.max_inflight,
            "active": self.active,
            "waiting": self.waiting,
            "rejected": self.rejected,
            "deadlines": self.deadlines,
            "workers": [
                {
                    "name": w.spec.name,
                    "capacity": w.spec.capacity,
                    "active": w.active,
                    "background": w.background,
                    "circuit_open": w.unavailable_until > self.clock(),
                }
                for w in self._workers
            ],
        }

    def _matching(self, waiter: _Waiter) -> list[_WorkerState]:
        return [
            w
            for w in self._workers
            if waiter.model in w.spec.models and waiter.lane in w.spec.lanes
        ]

    def _dispatch(self) -> None:
        now = self.clock()
        for waiter in sorted(self._waiters, key=lambda x: (x.priority, x.sequence)):
            if waiter.future.done():
                self._waiters.remove(waiter)
                continue
            matching = self._matching(waiter)
            healthy = [w for w in matching if w.unavailable_until <= now and not w.probe_active]
            if not healthy:
                self._waiters.remove(waiter)
                waiter.future.set_exception(
                    InferenceWorkerUnavailable("No healthy local inference worker is available")
                )
                continue
            candidates = [w for w in healthy if w.active < w.spec.capacity]
            if waiter.lane == "background":
                candidates = [
                    w
                    for w in candidates
                    if w.background < w.spec.capacity - w.spec.interactive_reserve
                ]
            if not candidates:
                continue
            # Prefer dedicated lanes before shared workers. Then distribute
            # proportionally to qualified capacity, deterministically.
            state = min(
                candidates,
                key=lambda w: (
                    len(w.spec.lanes),
                    w.active / w.spec.capacity,
                    w.spec.name,
                ),
            )
            probe = state.failures >= self.failure_threshold
            if probe:
                state.probe_active = True
            state.active += 1
            if waiter.lane == "background":
                state.background += 1
            lease = WorkerLease(self, state, waiter.model, waiter.lane, probe)
            self._waiters.remove(waiter)
            waiter.future.set_result(lease)

    async def acquire(
        self,
        *,
        model: str,
        lane: str = "interactive",
        timeout: float | None = 120.0,
        priority: int = 1,
    ) -> WorkerLease:
        if lane not in {"interactive", "background"} or not isinstance(model, str) or not model:
            raise ValueError("Invalid local inference admission")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Invalid local inference deadline")
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            if self.active or self.waiting:
                raise RuntimeError("A live local inference pool cannot move event loops")
            self._loop = loop
        if self.active + self.waiting >= self.max_inflight:
            self.rejected += 1
            raise InferenceOverloaded("Local inference admission is full; retry later")
        self._sequence += 1
        future = loop.create_future()
        waiter = _Waiter(model, lane, priority, self._sequence, future)
        self._waiters.append(waiter)
        self._dispatch()
        granted = False
        try:
            if future.done():
                lease = future.result()
            else:
                lease = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            granted = True
            return lease
        except InferenceWorkerUnavailable:
            raise
        except TimeoutError:
            self.deadlines += 1
            raise InferenceDeadlineExceeded(
                "Local inference admission exceeded its deadline"
            ) from None
        finally:
            if not granted:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                if future.done() and not future.cancelled() and future.exception() is None:
                    future.result().release()
                elif not future.done():
                    future.cancel()
                self._dispatch()


def configured_local_pool(
    environ: Mapping[str, str] | None = None,
) -> LocalInferencePool | None:
    """Explicit opt-in; absent ORB_LLM_WORKERS preserves the existing appliance."""
    env = os.environ if environ is None else environ
    raw = env.get("ORB_LLM_WORKERS", "")
    if not raw.strip():
        return None
    try:
        if len(raw) > 65536:
            raise ValueError
        entries = json.loads(raw)
        if not isinstance(entries, list) or not entries or len(entries) > 64:
            raise ValueError
        workers = []
        allowed = {
            "name",
            "origin",
            "models",
            "capacity",
            "lanes",
            "interactive_reserve",
            "qualified",
            "qualification",
        }
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) - allowed
                or entry.get("qualified") is not True
            ):
                raise ValueError
            workers.append(WorkerSpec(**{k: v for k, v in entry.items() if k != "qualified"}))
        return LocalInferencePool(
            workers, max_inflight=int(env.get("ORB_LLM_POOL_MAX_INFLIGHT", "200"))
        )
    except (ValueError, TypeError, KeyError):
        raise ValueError("Invalid qualified local inference worker configuration") from None
