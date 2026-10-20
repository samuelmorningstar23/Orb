"""Orb LLM Execution Queue — Single-Model Concurrency Manager

Two lanes share one local model:

    interactive (PRIORITY_SAGE)   ─ BoundedSemaphore(MAX_INTERACTIVE=1) ─┐
                                                                         ├─ GPU gate
    background  (everything else) ─ priority queue, max_concurrent=1  ───┘  (ref-counted)

The lanes are independent: an interactive turn NEVER waits for a background
holder to finish. The shared GPU gate (services/gpu_coordinator) keeps
in-process torch/MLX work off the GPU while any Ollama stream is running; it is
ref-counted across the two lanes, so it does not serialize Ollama against
itself.
"""

import asyncio
import collections
import contextvars
import logging
import time

from config import (
    BG_LLM_GOVERNOR_ENABLED,
    BG_LLM_MAX_PER_MIN,
    BG_LLM_QUIET_AFTER_INTERACTIVE_SECONDS,
    BG_LLM_YIELD_WAIT_SECONDS,
)

from services.gpu_coordinator import (
    PRIORITY_BACKGROUND,
    PRIORITY_SAGE,
    PRIORITY_SCRIBE,
    acquire_gpu_gate,
    release_gpu_gate,
)

logger = logging.getLogger(__name__)

LANE_INTERACTIVE = "interactive"
LANE_BACKGROUND = "background"

# One GPU serves one 27B stream well, so the interactive lane is bounded at one.
# A SECOND interactive turn waits on this semaphore — never behind background
# work.
MAX_INTERACTIVE = 1


class _QueueEvent(asyncio.Event):
    _is_cancelled: bool = False


class BackgroundAdmissionRejected(asyncio.TimeoutError):
    """The BackgroundGovernor shed a background-lane admission.

    Subclasses asyncio.TimeoutError DELIBERATELY: acquire(priority=3) can
    already raise asyncio.TimeoutError today, so every ambient caller has a
    handler that takes its deterministic fallback (the narrator skips the
    refresh, Vigil's garnish returns the static sentence, the ambient
    classifier returns None, the timeline renders the heuristic projection).
    A shed therefore needs NO new caller-side handling; the distinct type
    exists so tests and logs can tell a governor shed from a queue timeout.
    """


class BackgroundGovernor:
    """Volume governor for ambient (PRIORITY_BACKGROUND) admissions.

    Design and measurements: config.py's BG_LLM_* block. The reserved
    interactive lane fixed queue WAIT; this bounds background VOLUME, which is
    what actually degrades interactive decode (~26% under an alert storm).

    Three behaviours, all decided at admission:
      * rolling-60s ceiling: at most `max_per_min` admissions; over the ceiling
        the call is SHED immediately, never queued — parked storm work would
        resume at the worst possible moment (see the yield-wait note in
        config.py), and every governed caller degrades deterministically.
      * yield: while `preempt` reports interactive traffic, and through the
        quiet tail stamped by note_interactive_end(), admissions wait up to
        `yield_wait` seconds (also bounded by the caller's own timeout), then
        are shed.
      * disabled: admit() returns immediately — exactly-old behaviour.

    Only PRIORITY_BACKGROUND (3) is governed. PRIORITY_SCRIBE (2) work is
    clinician-triggered with a human waiting (report generation, Lens,
    discharge summaries) and has no deterministic fallback to shed to; it was
    also never part of the measured storm (554 ambient calls, peak 57/min).

    RACE DISCIPLINE (the reason the previous reserved-lane attempt was
    reverted): every admission decision — the yield/quiet check, the rolling
    window trim + ceiling check, and the recording of the admission — happens
    in ONE synchronous block per loop iteration, with no await between check
    and record, so it is atomic on the single-threaded event loop. Every await
    is followed by a full re-evaluation from scratch. Two governed callers can
    therefore never both observe "room for one more" and both record.
    tests/test_background_governor.py reintroduces an await into that window
    to prove the test catches it.

    `clock` and `sleep` are injectable so tests simulate minutes without real
    sleeps; production uses time.monotonic / asyncio.sleep. No asyncio
    primitives are held, so the governor never loop-binds.
    """

    WINDOW_SECONDS = 60.0
    # While preempt is up the block has no known end, so waiters re-check on a
    # short poll; the quiet tail and the yield deadline are exact timestamps
    # and are slept to directly.
    PREEMPT_POLL_SECONDS = 0.05

    def __init__(
        self,
        *,
        enabled=None,
        max_per_min=None,
        yield_wait=None,
        quiet_after=None,
        preempt=None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.enabled = BG_LLM_GOVERNOR_ENABLED if enabled is None else enabled
        self.max_per_min = BG_LLM_MAX_PER_MIN if max_per_min is None else max_per_min
        self.yield_wait = BG_LLM_YIELD_WAIT_SECONDS if yield_wait is None else yield_wait
        self.quiet_after = (
            BG_LLM_QUIET_AFTER_INTERACTIVE_SECONDS if quiet_after is None else quiet_after
        )
        self._preempt = preempt if preempt is not None else (lambda: False)
        self._clock = clock
        self._sleep = sleep
        self._admitted: collections.deque[float] = (
            collections.deque()
        )  # monotonic stamps, oldest first
        self._quiet_until = float("-inf")

    def reset(self) -> None:
        """Drop rolling-window and quiet-tail state (test/admin hatch)."""
        self._admitted.clear()
        self._quiet_until = float("-inf")

    def note_interactive_end(self) -> None:
        """Interactive traffic just drained to zero — start the quiet tail."""
        self._quiet_until = self._clock() + self.quiet_after

    def _blocked_until(self, now: float):
        """Timestamp to wait toward before re-checking, or None if admissible."""
        if self._preempt():
            return now + self.PREEMPT_POLL_SECONDS
        if now < self._quiet_until:
            return self._quiet_until
        return None

    async def admit(self, timeout: float | None = None) -> None:
        """Admit one background acquisition or raise BackgroundAdmissionRejected.

        `timeout` is the caller's own acquire() budget; the yield wait never
        exceeds it. Uses the governor's own clock (not the caller's deadline
        arithmetic) so injected clocks stay self-consistent.
        """
        if not self.enabled:
            return
        reject_at = self._clock() + self.yield_wait
        if timeout is not None:
            reject_at = min(reject_at, self._clock() + timeout)
        while True:
            now = self._clock()
            blocked_until = self._blocked_until(now)
            if blocked_until is None:
                # Decision block: trim, check, record — no await in between.
                while self._admitted and now - self._admitted[0] >= self.WINDOW_SECONDS:
                    self._admitted.popleft()
                if len(self._admitted) >= self.max_per_min:
                    raise BackgroundAdmissionRejected(
                        f"background LLM admission shed: {len(self._admitted)} "
                        f"admitted in the last 60s (ceiling {self.max_per_min})"
                    )
                self._admitted.append(now)
                return
            if now >= reject_at:
                raise BackgroundAdmissionRejected(
                    "background LLM admission shed: yielding to interactive "
                    f"traffic (waited {self.yield_wait:.1f}s max)"
                )
            await self._sleep(min(blocked_until, reject_at) - now)


class _LaneTicket:
    """One live acquisition.

    release() takes no arguments (it is called from `finally` blocks all over
    the codebase), so the ticket is carried per-task in a ContextVar: acquire()
    sets it, release() reads and clears it. `owner` pins the ticket to the queue
    instance that issued it so a release() on a different instance (tests build
    their own PriorityLLMQueue) can never retire another instance's holder.
    """

    __slots__ = ("owner", "lane", "priority", "released", "token", "t0", "watchdog")

    def __init__(self, owner, lane: str, priority: int):
        self.owner = owner
        self.lane = lane
        self.priority = priority
        self.released = False
        self.token: contextvars.Token[_LaneTicket | None] | None = (
            None  # contextvars.Token for exact reset
        )
        self.t0 = time.monotonic()
        self.watchdog: asyncio.TimerHandle | None = (
            None  # loop.call_later handle; see _install_ticket
        )


_lane_ticket: contextvars.ContextVar = contextvars.ContextVar("orb_llm_lane_ticket", default=None)


class PriorityLLMQueue:
    """Reserved-lane admission control for the single local model.

    ── WHY TWO LANES (measured 2026-07-31) ──
    The queue used to be one slot, priority-ordered but NON-preemptive. Priority
    decided who went NEXT; it did not evict the current holder, so an
    interactive Sage turn arriving while a background job held the slot waited
    for that job to FINISH. Background holders are long — the ward narrator
    streams up to 45 s, timeline projection up to 120 s. Measured on the
    appliance: a clinician's question sat **31 s** in this queue before
    generation began, against a model that decodes the answer in ~2 s.

    An earlier reserved-lane attempt was REVERTED the same day because the
    ref-counted GPU gate had a CHECK-THEN-AWAIT race (two tasks both observed
    `refs == 0`, both tried to take the exclusive lock). This implementation
    closes that hole structurally:

      * Every mutation of `_gate_refs` is SYNCHRONOUS (no await inside), so it
        is atomic on a single-threaded event loop.
      * The one place that awaits — acquiring the underlying gate lock when the
        refcount is transitioning 0 → 1 — holds `self.lock` across BOTH the
        decision and the await, and only publishes the ref AFTER the gate is
        actually held. A second acquirer therefore blocks on `self.lock`, and
        when it wakes it re-reads a settled refcount: either 1 (first acquirer
        won → just bump) or 0 (first acquirer failed → try itself). There is no
        window in which two tasks both believe they must acquire.
      * `_gate_refs > 0` implies a holder exists, so a synchronous
        `_gate_ref_release()` can never race the awaiting acquirer (during that
        await the refcount is 0 and release is a no-op).

    Deadlock safety: `self.lock` is held across the gate await ONLY when the
    refcount is 0, i.e. when the gate is free or held by umsq (in-process ML).
    umsq's release path never touches `self.lock`, so the wait always resolves.
    The cost is nil in practice: a lane that must wait for an ML session would
    have had to wait for it anyway.

    Lane assignment: `priority <= PRIORITY_SAGE` (1) is interactive; everything
    else is background. Both lanes hold the GPU gate while running, so
    in-process ML still cannot run concurrently with an Ollama stream.

    `preempt_requested` is True while an interactive turn is in flight (waiting
    or streaming). Long background streams may poll it and bail early — every
    background caller has a deterministic fallback; callers own that decision.
    The BackgroundGovernor reads it at ADMISSION time only: it sheds or delays
    NEW background work. A P3 job already holding the slot gives it back only
    if its caller opted in, through yield_reason() and
    ollama_gateway.post(yield_to_clinician=True), which also enforces the P3
    hold cap.
    """

    def __init__(self):
        self._lock = None
        self._queue = None
        self._interactive_sem = None
        self._loop = None

        self.counter = 0
        self.max_concurrent = 1  # background lane: 1 stream
        self.max_interactive = MAX_INTERACTIVE

        self._bg_active = 0  # background holders (0..max_concurrent)
        self._interactive_active = 0  # interactive holders (0..max_interactive)
        self._interactive_waiting = 0  # interactive acquires not yet granted

        self._gate_refs = 0  # holders of the shared GPU gate (both lanes)
        self._gate_token = None  # ownership token for the current gate hold
        self._holders: list = []  # live tickets, oldest first
        # Count of holds that outran _hold_warn_seconds(). Monotonic for the
        # process lifetime and exported as a metric, so "the lane was occupied"
        # is answerable from /metrics instead of from the log.
        self.long_holds = 0

        # Governs PRIORITY_BACKGROUND admissions (see the class docstring for
        # why P2 is exempt). The lambda keeps preempt_requested live — the
        # governor must see a waiting clinician the same tick the counter moves.
        self.governor = BackgroundGovernor(preempt=lambda: self.preempt_requested)

    # ── loop-bound primitives ────────────────────────────────────────────
    # asyncio.Lock/Queue/Semaphore bind to whichever loop first awaits them.
    # Production runs one uvicorn loop, so the bind happens once; the test suite
    # gives every test a fresh loop, and a primitive left bound to a dead loop
    # would raise "bound to a different event loop" forever. Re-binding on a
    # loop change makes the singleton self-healing without callers having to
    # know about it.

    def _bind_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop is loop:
            return
        if self._loop is not None and (
            self._bg_active or self._interactive_active or self._gate_refs
        ):
            logger.warning(
                "[LLMQueue] event loop changed while state was held "
                "(bg=%s, interactive=%s, gate_refs=%s) — resetting lane state",
                self._bg_active,
                self._interactive_active,
                self._gate_refs,
            )
        self._loop = loop
        self._lock = None
        self._queue = None
        self._interactive_sem = None
        self.counter = 0
        self._bg_active = 0
        self._interactive_active = 0
        self._interactive_waiting = 0
        self._gate_refs = 0
        self._gate_token = None
        self._holders.clear()

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def queue(self) -> asyncio.PriorityQueue:
        """Background lane's waiting room (interactive does not queue here)."""
        if self._queue is None:
            self._queue = asyncio.PriorityQueue()
        return self._queue

    @property
    def interactive_sem(self) -> asyncio.BoundedSemaphore:
        if self._interactive_sem is None:
            # Bounded: an over-release (a stray release() that could not be
            # attributed) raises instead of silently letting two 27B streams
            # share the lane.
            self._interactive_sem = asyncio.BoundedSemaphore(self.max_interactive)
        return self._interactive_sem

    # ── observable state ─────────────────────────────────────────────────

    @property
    def active_workers(self) -> int:
        """In-flight LLM workers across BOTH lanes (metrics read this)."""
        return self._bg_active + self._interactive_active

    @active_workers.setter
    def active_workers(self, value: int) -> None:
        """Administrative reset hatch (tests/conftest do `active_workers = 0`).

        Assigning 0 zeroes both lanes and drops any stale gate bookkeeping;
        the semaphore and queue are rebuilt lazily. Never used in production.
        """
        v = max(0, int(value))
        self._bg_active = v
        self._interactive_active = 0
        if v == 0:
            self._interactive_waiting = 0
            self._gate_refs = 0
            self._gate_token = None
            for t in self._holders:
                # These tickets are being abandoned, not retired, so disarm
                # their watchdogs by hand — an armed timer on a discarded ticket
                # would warn about a hold nobody is holding.
                self._disarm_hold_watchdog(t)
            self._holders.clear()
            self._interactive_sem = None
            self.governor.reset()

    @property
    def interactive_active(self) -> int:
        return self._interactive_active

    @property
    def interactive_waiting(self) -> int:
        return self._interactive_waiting

    @property
    def background_active(self) -> int:
        return self._bg_active

    @property
    def gate_refs(self) -> int:
        """Number of lane holders currently counted against the GPU gate."""
        return self._gate_refs

    @property
    def preempt_requested(self) -> bool:
        """True while a clinician's turn is waiting or streaming.

        Long background streams SHOULD poll this and stop early (they all have
        a deterministic fallback); that decision stays with callers. The
        BackgroundGovernor reads it at background ADMISSION only.
        """
        return (self._interactive_active + self._interactive_waiting) > 0

    def _p3_hold_cap_seconds(self) -> float:
        """Longest a yielding P3 holder may keep the background lane.

        Same 60 s as the hold-warn budget and for the same measured reason: it
        sits above every legitimate background hold on this appliance, so it
        only ever cuts a wedged or storm-stalled job. The callers' own HTTP
        budgets (up to 120 s) are stall detectors, not hold limits.
        """
        import os

        try:
            return max(1.0, float(os.getenv("ORB_LLM_P3_HOLD_CAP", "60")))
        except (TypeError, ValueError):
            return 60.0

    def yield_reason(self) -> str | None:
        """Why the calling task should give its background hold back now.

        None unless the caller holds the background lane at PRIORITY_BACKGROUND:
        P2 work has a clinician waiting on it and no fallback to take. A P3
        holder yields when a clinician's turn is waiting or streaming, or when
        its hold has reached the P3 cap. Read through the per-task ticket, so a
        caller that never acquired (or acquired elsewhere) never yields.
        ollama_gateway.post(yield_to_clinician=True) polls this while waiting.
        """
        ticket = _lane_ticket.get()
        if (
            ticket is None
            or ticket.owner is not self
            or ticket.released
            or ticket.lane != LANE_BACKGROUND
            or ticket.priority < PRIORITY_BACKGROUND
        ):
            return None
        if self.preempt_requested:
            return "yielded to a clinician's turn"
        cap = self._p3_hold_cap_seconds()
        if time.monotonic() - ticket.t0 >= cap:
            return f"background hold reached the {cap:.0f} s cap"
        return None

    # ── acquire ──────────────────────────────────────────────────────────

    @staticmethod
    def _remaining(deadline):
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    async def acquire(self, priority: int = PRIORITY_SCRIBE, timeout: float = 120.0):
        """Request access to the LLM. Lower number = higher priority.

        Priority constants are shared with the GPU coordinator
        (gpu_coordinator.PRIORITY_*) so both serializers agree on what
        "interactive" vs "background" means:
        1 = PRIORITY_SAGE       (Interactive: Ambient Actions, SAGE Chat)
        2 = PRIORITY_SCRIBE     (Report generation, Summaries, Lens, Scribe)
        3 = PRIORITY_BACKGROUND (Background bots: Vigil monitoring)

        `priority <= PRIORITY_SAGE` takes the reserved interactive lane and
        never queues behind background work. Everything else takes the
        background lane, which keeps its own priority ordering.

        Raises asyncio.TimeoutError if the slot (or the GPU gate) is not
        available within `timeout`; on ANY failure path neither a lane slot nor
        a gate reference is leaked.
        """
        self._bind_loop()
        if priority <= PRIORITY_SAGE:
            return await self._acquire_interactive(priority, timeout)
        return await self._acquire_background(priority, timeout)

    async def _acquire_interactive(self, priority: int, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        sem = self.interactive_sem
        self._interactive_waiting += 1  # synchronous — preempt_requested is live NOW
        slot_held = False
        gate_held = False
        granted = False
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self._remaining(deadline))
            slot_held = True
            await self._gate_ref_acquire(deadline)
            gate_held = True

            # No await between here and `granted = True`, so no observer can
            # ever see this acquisition counted in two places at once.
            self._install_ticket(LANE_INTERACTIVE, priority)
            self._interactive_active += 1
            granted = True
            logger.info(
                "[LLMQueue] P%s acquired INTERACTIVE lane (interactive=%s, bg=%s, gate_refs=%s)",
                priority,
                self._interactive_active,
                self._bg_active,
                self._gate_refs,
            )
            return True
        finally:
            # Balanced against the single increment above, on every path.
            self._interactive_waiting = max(0, self._interactive_waiting - 1)
            if not self.preempt_requested:
                # A failed/cancelled interactive attempt was still interactive
                # activity; when granted, _interactive_active keeps preempt up
                # and _retire stamps the tail instead.
                self.governor.note_interactive_end()
            if not granted:
                # Unwind is fully synchronous: no await can be interrupted by a
                # second cancellation, so nothing leaks.
                if gate_held:
                    self._gate_ref_release()
                if slot_held:
                    self._free_interactive_slot()
                logger.warning(
                    "[LLMQueue] P%s interactive acquire failed/cancelled "
                    "(interactive=%s, waiting=%s, gate_refs=%s)",
                    priority,
                    self._interactive_active,
                    self._interactive_waiting,
                    self._gate_refs,
                )

    async def _acquire_background(self, priority: int, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        if priority >= PRIORITY_BACKGROUND:
            # Governed BEFORE anything enters the waiting room: a shed call
            # never queues, so nothing needs unwinding. May raise
            # BackgroundAdmissionRejected (an asyncio.TimeoutError). This is
            # the single choke point — direct ambient callers and the batch
            # coordinator's coalesced acquisition both pass through here.
            await self.governor.admit(timeout)
        event = _QueueEvent()
        async with self.lock:
            self.counter += 1
            await self.queue.put((priority, self.counter, event))
            logger.info(
                "[LLMQueue] P%s request queued (bg_active=%s, queued=%s)",
                priority,
                self._bg_active,
                self.queue.qsize(),
            )
            self._maybe_wake()

        slot_held = False
        gate_held = False
        granted = False
        try:
            await asyncio.wait_for(event.wait(), timeout=self._remaining(deadline))
            slot_held = True  # _maybe_wake() incremented _bg_active on our behalf
            await self._gate_ref_acquire(deadline)
            gate_held = True

            self._install_ticket(LANE_BACKGROUND, priority)
            granted = True
            logger.info(
                "[LLMQueue] P%s acquired BACKGROUND lane (bg=%s, interactive=%s, gate_refs=%s)",
                priority,
                self._bg_active,
                self._interactive_active,
                self._gate_refs,
            )
            return True
        finally:
            if not granted:
                if gate_held:
                    self._gate_ref_release()
                # Mark the queued item dead so _maybe_wake() skips it, and give
                # back the slot if we had already been granted one.
                event._is_cancelled = True
                if slot_held or event.is_set():
                    self._bg_active = max(0, self._bg_active - 1)
                    logger.warning(
                        "[LLMQueue] P%s timed out/cancelled post-acquisition; "
                        "released background slot. bg_active=%s",
                        priority,
                        self._bg_active,
                    )
                else:
                    logger.warning(
                        "[LLMQueue] P%s timed out/cancelled in queue; removed. bg_active=%s",
                        priority,
                        self._bg_active,
                    )
                self._maybe_wake()

    # ── GPU gate refcount ────────────────────────────────────────────────

    async def _gate_ref_acquire(self, deadline) -> None:
        """Take one reference on the shared GPU gate (bounded by `deadline`,
        a monotonic timestamp or None for "wait indefinitely").

        Atomic: on return the gate is held on this queue's behalf and our
        reference is counted; on exception nothing changed. The refcount check
        AND the (only) await both happen under `self.lock` — this closes the
        check-then-await race that sank the previous attempt.

        The lock is acquired OUTSIDE the try, so a timeout waiting for it can
        never release a lock we do not hold. Both waits draw down the same
        deadline, so the caller's timeout is honoured end to end.
        """
        remaining = self._remaining(deadline)
        if remaining is None:
            await self.lock.acquire()
        else:
            await asyncio.wait_for(self.lock.acquire(), timeout=remaining)
        try:
            if self._gate_refs > 0:
                # Another lane already holds the physical gate — join it. No
                # await between this read and the write, so it is atomic.
                self._gate_refs += 1
                return
            token = object()
            # refs stays 0 for the whole await: a competitor blocks on the lock,
            # and a concurrent release() is a no-op while refs == 0.
            await acquire_gpu_gate(token, timeout=self._remaining(deadline))
            self._gate_token = token
            self._gate_refs = 1
        finally:
            self.lock.release()

    def _gate_ref_release(self) -> None:
        """Drop one gate reference; the LAST releaser releases the gate.

        Fully synchronous, so it is atomic with respect to the event loop and
        can never double-release: the token is cleared in the same breath.
        """
        if self._gate_refs <= 0:
            return
        self._gate_refs -= 1
        if self._gate_refs == 0 and self._gate_token is not None:
            token, self._gate_token = self._gate_token, None
            release_gpu_gate(token)

    # ── tickets ──────────────────────────────────────────────────────────

    def _install_ticket(self, lane: str, priority: int) -> _LaneTicket:
        ticket = _LaneTicket(self, lane, priority)
        self._holders.append(ticket)
        try:
            ticket.token = _lane_ticket.set(ticket)
        except Exception:  # pragma: no cover — ContextVar.set does not fail
            ticket.token = None
        self._arm_hold_watchdog(ticket)
        return ticket

    # ── lane-hold watchdog ───────────────────────────────────────────────
    # WHY THIS EXISTS. The background lane is ONE slot wide, so the only thing
    # that matters to every waiter is how long the current holder keeps it — and
    # until this, nothing anywhere reported that. `sage_assessments` records
    # `queue_timeout` on the WAITER, which says "I never got the slot" and is
    # identical whether the queue was fifty deep or one caller sat on an empty
    # lane for five minutes. Those are opposite problems with opposite fixes,
    # and telling them apart on 2026-08-15 took reconstructing acquire/release
    # pairs out of 26k log lines by hand. It should cost one WARNING.
    #
    # IT WARNS AND DOES NOT SEIZE. Force-releasing would let a second stream on
    # to a lane whose entire purpose is `max_concurrent=1` — two 27B streams on
    # one GPU is worse than the stall it would be reporting. The holder still
    # owns its own deadline; this only makes an over-run audible.

    def _hold_warn_seconds(self) -> float:
        """Hold time after which a background holder is reported as over-long.

        60 s sits above every legitimate hold measured on this appliance — the
        Tier-2 brief lane runs a median 11.6 s / p95 19.6 s, and the warm 27B
        worst case end-to-end was 53.8 s — and below the 120-300 s plateaus that
        only a wedged engine produces. Tier 3's 120 s generation budget can
        legitimately trip it; that is accepted, because a WARNING is an
        instrument rather than an error and a two-minute occupancy of the one
        background slot is worth a line either way.
        """
        import os

        try:
            return max(1.0, float(os.getenv("ORB_LLM_LANE_HOLD_WARN", "60")))
        except (TypeError, ValueError):
            return 60.0

    def _arm_hold_watchdog(self, ticket: _LaneTicket) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover — no loop (sync test helper)
            return
        budget = self._hold_warn_seconds()

        def _warn():
            if ticket.released:
                return
            self.long_holds += 1
            logger.warning(
                "[LLMQueue] P%s has held the %s lane for over %.0fs — the lane "
                "is %s slot(s) wide, so everything behind it is timing out "
                "(queued=%s, gate_refs=%s). This is lane occupancy, not queue "
                "depth: waiters will record a queue timeout either way.",
                ticket.priority,
                ticket.lane,
                budget,
                self.max_concurrent if ticket.lane == LANE_BACKGROUND else self.max_interactive,
                self.queue.qsize(),
                self._gate_refs,
            )

        ticket.watchdog = loop.call_later(budget, _warn)

    @staticmethod
    def _disarm_hold_watchdog(ticket: _LaneTicket) -> None:
        handle, ticket.watchdog = ticket.watchdog, None
        if handle is not None:
            handle.cancel()

    def _clear_ticket(self, ticket: _LaneTicket) -> None:
        token, ticket.token = ticket.token, None
        try:
            if token is not None:
                _lane_ticket.reset(token)  # restores any outer/nested value
            else:
                _lane_ticket.set(None)
        except ValueError:
            # Token created in a different Context (release() ran in a task that
            # did not perform the acquire) — fall back to a plain clear.
            try:
                _lane_ticket.set(None)
            except Exception:  # pragma: no cover
                pass

    def _infer_holder(self):
        """Best-effort lane attribution when the ContextVar is unavailable.

        This happens only when release() runs in a context that did not perform
        the acquire — in practice an async generator finalized by the event
        loop's asyncgen shutdown hook. The only acquire/release pair inside an
        async generator is Sage's own streaming turn, which is INTERACTIVE, so
        that is the tiebreak when both lanes are busy.

        Bounded blast radius: every release() retires at most ONE live ticket
        and drops at most ONE gate reference, and the task whose ticket was
        taken by this fallback returns early instead of retiring a second one.
        So a wrong guess can only free the two lanes in the wrong ORDER (one
        lane is let go slightly early); the counters, the semaphore and the gate
        still return to zero. Covered by
        tests/test_llm_queue_lanes.py::test_release_without_context_*.
        """
        if not self._holders:
            return None
        interactive = [t for t in self._holders if t.lane == LANE_INTERACTIVE]
        background = [t for t in self._holders if t.lane == LANE_BACKGROUND]
        if interactive and not background:
            return interactive[0]
        if background and not interactive:
            return background[0]
        return interactive[0]

    # ── release ──────────────────────────────────────────────────────────

    def release(self):
        """Release this task's lane slot so the next waiter can proceed.

        Takes no arguments by design — callers invoke it from `finally` blocks,
        often inside streaming async generators — so the lane is recovered from
        the per-task ContextVar set by acquire(), with a documented fallback.

        This MUST run synchronously. It previously scheduled
        `asyncio.create_task(self._safe_release())` and returned immediately;
        during client disconnect / loop teardown that fire-and-forget task could
        be cancelled or never scheduled (RuntimeError: no running event loop),
        and a single dropped release wedged the queue until process restart.
        Everything below is plain attribute mutation plus `Event.set()` /
        `Semaphore.release()`, all safe to run inline on the owning loop.
        """
        ticket = _lane_ticket.get()
        if ticket is not None and ticket.owner is self:
            self._clear_ticket(ticket)
            if ticket.released:
                # Already retired (see the fallback below, or a double release
                # from a caller). THIS task's acquisition is fully accounted
                # for, so stop here rather than retiring somebody else's.
                return
            self._retire(ticket)
            return
        if ticket is not None:
            ticket = None  # belongs to a different queue instance — ignore it

        ticket = self._infer_holder()
        if ticket is not None:
            logger.warning(
                "[LLMQueue] release() had no lane ticket in context; "
                "attributing to the %s lane by fallback",
                ticket.lane,
            )

        if ticket is None:
            logger.warning(
                "[LLMQueue] release() with no attributable holder — ignoring "
                "(bg=%s, interactive=%s, gate_refs=%s)",
                self._bg_active,
                self._interactive_active,
                self._gate_refs,
            )
            return

        self._retire(ticket)

    def _retire(self, ticket: _LaneTicket) -> None:
        if ticket.released:
            return
        ticket.released = True
        self._disarm_hold_watchdog(ticket)
        try:
            self._holders.remove(ticket)
        except ValueError:
            pass

        self._gate_ref_release()

        if ticket.lane == LANE_INTERACTIVE:
            self._interactive_active = max(0, self._interactive_active - 1)
            if not self.preempt_requested:
                # LAST interactive activity just ended — stamp the quiet tail
                # synchronously, in the same tick the counters drain, so a
                # background admit that wakes next re-checks against it.
                self.governor.note_interactive_end()
            self._free_interactive_slot()
        else:
            self._bg_active = max(0, self._bg_active - 1)
            self._maybe_wake()

        logger.info(
            "[LLMQueue] Released %s lane after %sms "
            "(bg=%s, interactive=%s, queued=%s, gate_refs=%s)",
            ticket.lane,
            int((time.monotonic() - ticket.t0) * 1000),
            self._bg_active,
            self._interactive_active,
            self.queue.qsize(),
            self._gate_refs,
        )

    def _free_interactive_slot(self) -> None:
        sem = self._interactive_sem
        if sem is None:
            return
        try:
            sem.release()
        except ValueError:
            # BoundedSemaphore guard: an unattributable release() tried to hand
            # back a slot nobody held. Log loudly; never raise out of a finally.
            logger.error(
                "[LLMQueue] interactive semaphore over-release suppressed (interactive_active=%s)",
                self._interactive_active,
            )
        except RuntimeError:  # pragma: no cover — loop teardown
            pass

    def _maybe_wake(self) -> None:
        """Grant the background slot to the highest-priority waiter.

        Interactive traffic does NOT gate this: the lanes are independent, which
        is the entire point — a background job that is already streaming cannot
        be evicted, so making new background work wait on interactive would only
        add timeouts without giving the clinician the GPU any sooner.
        MUST be called from the queue's owning event loop.
        """
        while self._bg_active < self.max_concurrent and not self.queue.empty():
            item = self.queue.get_nowait()
            event = item[2]
            if getattr(event, "_is_cancelled", False):
                continue
            self._bg_active += 1
            event.set()


# Singleton instance for the entire FastAPI worker
llm_queue = PriorityLLMQueue()
