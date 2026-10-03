"""Orb GPU Coordinator — Unified VRAM & Metal Queue Serialization (Apple Silicon)

Solves three problems on the shared Apple Silicon GPU pipeline:
  1. Metal command queue contention: PyTorch MPS and MLX both submit Metal
     command buffers to the same hardware queue. Concurrent submissions cause
     driver-level context switching that degrades throughput.
  2. MPS allocator fragmentation: Without explicit cache flushing after each
     inference, the MPS allocator's fragment count grows monotonically.
  3. MLX cache bloat: MLX keeps internal command buffer pools alive. Explicit
     clearing reclaims ~50-100 MB per inference cycle.

Components:
  VramGuard      — Sync context manager wrapping GPU inference with pre/post
                   memory hygiene (gc.collect → MPS flush → MLX flush).
  MetalQueueCoordinator (UMSQ) — Priority-aware async lock ensuring only one
                   major model inference runs at any instant. Sage (real-time
                   voice) always preempts background scoring.

Priority Levels:
  1 = SAGE      — Real-time clinician voice interaction (latency-critical)
  2 = SCRIBE    — User-initiated document generation (can tolerate ~1s wait)
  3 = BACKGROUND — Vigil CfC scoring, Forecast LSTM (nobody is staring)
"""
import gc
import time
import asyncio
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# VramGuard — Sync context manager for GPU memory hygiene
# ══════════════════════════════════════════════════════════════════════

@contextmanager
def vram_guard(label: str = ""):
    """Wrap a GPU inference block with pre/post memory cleanup.

    Usage:
        with vram_guard("Vigil CfC"):
            output = model(input_tensor)

    On exit, unconditionally runs:
      1. gc.collect() — reclaim Python-side references to GPU tensors
      2. torch.mps.empty_cache() — flush MPS allocator (PyTorch)
      3. mx.metal.clear_cache() — flush MLX command buffer pool
      4. mx.clean_eval() — clean MLX evaluation graphs
    """
    t0 = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # 1. Python GC — reclaim tensor reference cycles
        gc.collect()

        # 2. PyTorch MPS allocator flush
        try:
            import torch
            if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
                torch.mps.empty_cache()
        except (ImportError, RuntimeError):
            pass

        # 3. MLX Metal cache & clean evaluation flush
        try:
            import mlx.core as mx
            if hasattr(mx, "metal") and hasattr(mx.metal, "clear_cache"):
                mx.metal.clear_cache()
            if hasattr(mx, "clean_eval"):
                mx.clean_eval()
        except (ImportError, AttributeError, RuntimeError):
            pass

        if label:
            logger.debug("[GPU] %s completed in %sms — VRAM cleaned", label, elapsed_ms)


# ══════════════════════════════════════════════════════════════════════
# MetalQueueCoordinator (UMSQ) — Priority-aware async GPU lock
# ══════════════════════════════════════════════════════════════════════

# Priority constants — CANONICAL definition for the whole backend.
# Both GPU serializers share these so "interactive" vs "background" means the
# same thing everywhere: services/llm_queue_service.py imports these constants
# (instead of redefining 1/2/3) so the Ollama queue and the Metal/MLX queue
# agree on priority ordering. Lower number = higher priority.
PRIORITY_SAGE = 1        # Real-time voice / interactive — always wins
PRIORITY_SCRIBE = 2      # User-initiated document gen
PRIORITY_BACKGROUND = 3  # Vigil CfC, Forecast LSTM (nobody is staring)

# ══════════════════════════════════════════════════════════════════════
# Unified hardware admission gate
# ══════════════════════════════════════════════════════════════════════
# Single mutual-exclusion gate for the ONE physical Apple-Silicon GPU. BOTH
# serializers hold it for the duration of a GPU session: umsq (in-process
# MPS/MLX) via its acquire() context, and llm_queue (Ollama) via its
# acquire()/release(). This closes the gap where a P3 background CfC run under
# umsq could execute concurrently with a P1 Sage Ollama stream under llm_queue —
# now at most one GPU job (LLM *or* ML) runs at a time. Each queue keeps its own
# priority ordering WITHIN its lane; the gate itself is FIFO across lanes.
_gpu_gate: "asyncio.Lock | None" = None
# Identity of the acquirer that currently holds the gate. Ownership is explicit
# (asyncio.Lock has no owner concept) so a lane can only release ITS OWN hold —
# a queued/timed-out waiter can never release the active holder's gate.
_gpu_gate_owner: object | None = None


def gpu_gate() -> "asyncio.Lock":
    """The process-wide GPU admission lock (lazy-init on the owning loop)."""
    global _gpu_gate
    if _gpu_gate is None:
        _gpu_gate = asyncio.Lock()
    return _gpu_gate


async def acquire_gpu_gate(owner: object, timeout: float | None = None) -> bool:
    """Acquire the shared GPU gate on behalf of `owner`, optionally bounded by
    `timeout`. `owner` is any unique per-acquisition token (an object identity).

    Returns True on success. Raises asyncio.TimeoutError if the timeout elapses
    (the caller must then release its own lane slot). On timeout the gate is NOT
    held and ownership is unchanged.
    """
    global _gpu_gate_owner
    gate = gpu_gate()
    if timeout is None:
        await gate.acquire()
    else:
        await asyncio.wait_for(gate.acquire(), timeout=timeout)
    _gpu_gate_owner = owner
    return True


def release_gpu_gate(owner: object) -> None:
    """Release the shared GPU gate ONLY if `owner` currently holds it. Never
    raises (a spurious release during loop teardown must not wedge cleanup)."""
    global _gpu_gate_owner
    gate = gpu_gate()
    try:
        if _gpu_gate_owner is owner and gate.locked():
            _gpu_gate_owner = None
            gate.release()
    except RuntimeError:
        pass


class MetalQueueCoordinator:
    """Priority-aware serialization lock for Apple Silicon's single GPU.

    When multiple services need the GPU simultaneously, the highest-priority
    waiter gets the lock next (lowest number = highest priority). This
    guarantees that a clinician's Sage voice query always preempts background
    Vigil scoring, even if 30 patients are queued for CfC inference.

    Usage:
        async with umsq.acquire("Sage Voice", priority=PRIORITY_SAGE):
            with vram_guard("Sage MLX"):
                # GPU-bound MLX inference here
    """

    def __init__(self):
        self._queue: asyncio.PriorityQueue | None = None
        self._guard_lock: asyncio.Lock | None = None
        self._counter = 0
        self._active = False
        self._active_task: str = ""

    @property
    def queue(self) -> asyncio.PriorityQueue:
        """Lazy-init to avoid creating in wrong event loop."""
        if self._queue is None:
            self._queue = asyncio.PriorityQueue()
        return self._queue

    @property
    def guard_lock(self) -> asyncio.Lock:
        """Lazy-init to avoid creating in wrong event loop."""
        if self._guard_lock is None:
            self._guard_lock = asyncio.Lock()
        return self._guard_lock

    class _AcquireContext:
        """Async context manager returned by acquire()."""

        def __init__(self, coordinator: "MetalQueueCoordinator", task_name: str, priority: int):
            self._coord = coordinator
            self._task_name = task_name
            self._priority = priority
            self._t0: float = 0
            self._acquired = False
            self._gate_held = False
            self._event = None

        async def __aenter__(self):
            self._event = asyncio.Event()

            async with self._coord.guard_lock:
                self._coord._counter += 1
                seq = self._coord._counter
                await self._coord.queue.put((self._priority, seq, self._event, self._task_name))

                if self._coord._active:
                    logger.info(
                        "[UMSQ] %s (P%s) queued — GPU busy with '%s'",
                        self._task_name, self._priority, self._coord._active_task,
                    )
                else:
                    self._coord._wake_next()

            try:
                # Wait until we're granted the GPU by our own lane...
                await self._event.wait()
                self._acquired = True
                # ...then hold the shared hardware gate so no LLM (Ollama) stream
                # runs on the GPU concurrently with this in-process ML session.
                # This context instance is the ownership token.
                await acquire_gpu_gate(self)
                self._gate_held = True
                self._t0 = time.monotonic()
                return self
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Clean up if timed out or cancelled during event.wait()/gate wait.
                if self._gate_held:
                    release_gpu_gate(self)
                    self._gate_held = False
                async with self._coord.guard_lock:
                    self._event._is_cancelled = True
                    # Only clear the active slot if THIS context actually became
                    # the holder (task_name is not unique — a queued waiter with
                    # the same name must not reset the running holder's state).
                    if self._acquired:
                        self._coord._active = False
                        self._coord._active_task = ""
                    self._coord._wake_next()
                raise

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if not self._acquired:
                return False

            elapsed_ms = int((time.monotonic() - self._t0) * 1000)
            logger.info("[UMSQ] %s released GPU after %sms", self._task_name, elapsed_ms)

            if self._gate_held:
                release_gpu_gate(self)
                self._gate_held = False

            async with self._coord.guard_lock:
                self._coord._active = False
                self._coord._active_task = ""
                self._coord._wake_next()

            return False  # Don't suppress exceptions

    def _wake_next(self):
        """Wake the highest-priority waiter. Must be called under guard_lock."""
        while not self.queue.empty() and not self._active:
            priority, seq, event, task_name = self.queue.get_nowait()
            if getattr(event, "_is_cancelled", False):
                continue
            self._active = True
            self._active_task = task_name
            logger.info("[UMSQ] GPU granted to '%s' (P%s)", task_name, priority)
            event.set()

    def acquire(self, task_name: str = "unknown", priority: int = PRIORITY_BACKGROUND):
        """Return an async context manager that acquires the GPU lock.

        Args:
            task_name: Human-readable label for logging.
            priority: 1=Sage (highest), 2=Scribe, 3=Background (lowest).

        Usage:
            async with umsq.acquire("Sage Voice", PRIORITY_SAGE):
                # GPU work here
        """
        return self._AcquireContext(self, task_name, priority)

    @property
    def is_busy(self) -> bool:
        """Check if the GPU is currently in use."""
        return self._active

    @property
    def active_task(self) -> str:
        """Get the name of the currently running GPU task."""
        return self._active_task


# ── Singleton ──
umsq = MetalQueueCoordinator()
