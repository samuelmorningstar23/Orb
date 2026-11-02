"""Scoped1GiB unused-allocator-cache budget; importing this imports no MLX.

Installed MLX0.32.3 core/__init__.pyi:859-906 documents that get_cache_memory
counts unused buffers, and set_cache_limit returns the PREVIOUS limit. Free
cache above the limit is reclaimed on the NEXT allocation; this is not an
immediate purge or a cap on active arrays, model weights, KV/prefix context,
wired memory, total RAM or GPU memory. No get_cache_limit API is exposed.

This scope changes only that unused-cache limit. It never evaluates arrays,
clears caches, changes weights/precision/sampling/context or alters memory/
wired limits. Native version/source identity is pinned by the caller policy.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Self

MODULE_NAME = "mlx.core"
CACHE_LIMIT_BYTES = 1 << 30


class AllocatorBudgetRefused(ValueError):
    """The native limit API or an active scope's policy changed unexpectedly."""


def _limit(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise AllocatorBudgetRefused("set_cache_limit must return the previous nonnegative integer byte limit.")
    return value


def _emit(emit: Callable[[dict[str, Any]], Any] | None, event: dict[str, Any]) -> None:
    if emit is not None:
        try:
            emit(event)
        except Exception:  # noqa: BLE001, S110 - Optional diagnostics cannot change allocator policy.
            pass


class _State:
    def __init__(self, mx: Any, setter: Callable[[int], Any], previous: int,
                 emit: Callable[[dict[str, Any]], Any] | None):
        self.mx, self.setter, self.previous, self.emit = mx, setter, previous, emit
        self.references = 0

    def restore(self) -> None:
        if self.mx.set_cache_limit is not self.setter:
            _emit(self.emit, {"event": "mlx_allocator_cache_budget_restore_refused", "reason": "foreign_api_replacement"})
            raise AllocatorBudgetRefused("The active cache-limit API was replaced; refusing to call foreign instrumentation.")
        # No getter exists. The setter reports the limit it replaced.
        # If foreign code changed it, put that foreign value back immediately
        # instead of leaving our saved previous policy in its place.
        replaced = _limit(self.setter(self.previous))
        if replaced != CACHE_LIMIT_BYTES:
            rollback_replaced = _limit(self.setter(replaced))
            _emit(self.emit, {"event": "mlx_allocator_cache_budget_restore_refused",
                              "reason": "foreign_limit_change", "foreign_limit_preserved": rollback_replaced == self.previous})
            if rollback_replaced != self.previous:
                raise AllocatorBudgetRefused("The cache limit changed concurrently during restoration.")
            raise AllocatorBudgetRefused("Foreign cache-limit change preserved; saved previous limit was not left in force.")
        _emit(self.emit, {"event": "mlx_allocator_cache_budget_restored", "restored_limit_bytes": self.previous,
                          "replaced_limit_bytes": replaced, "forced_evaluation": False, "cache_cleared": False})


_LOCK = threading.RLock()
_ACTIVE: dict[int, _State] = {}


class AllocatorCacheBudgetHandle:
    """Closable scoped limit; nested handles share one native setter pair."""

    def __init__(self, state: _State):
        self._state, self._closed = state, False

    def close(self) -> None:
        with _LOCK:
            if self._closed:
                return
            self._closed = True
            state = self._state
            state.references -= 1
            if state.references == 0:
                try:
                    state.restore()
                finally:
                    if _ACTIVE.get(id(state.mx)) is state:
                        del _ACTIVE[id(state.mx)]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, traceback: object) -> None:
        try:
            self.close()
        except AllocatorBudgetRefused as restoration_error:
            if exc is None:
                raise
            # Preserve the actual inference/guard failure if restoration also
            # detects foreign policy interference during teardown.
            if callable(getattr(exc, "add_note", None)):
                exc.add_note(str(restoration_error))


def install_allocator_cache_budget(mx: Any, emit: Callable[[dict[str, Any]], Any] | None = None) -> AllocatorCacheBudgetHandle:
    """Set only the explicit supplied MLX core's unused-cache budget to1GiB.

    The native function itself is not patched. A setter returning an invalid
    previous value is refused immediately: no reliable restoration value is
    available, so the owned caller must abort instead of guessing a limit.
    """
    setter = getattr(mx, "set_cache_limit", None)
    if getattr(mx, "__name__", None) != MODULE_NAME or not callable(setter) or (emit is not None and not callable(emit)):
        raise AllocatorBudgetRefused("Explicit pinned mlx.core with a callable cache-limit API is required.")
    with _LOCK:
        state = _ACTIVE.get(id(mx))
        if state is not None:
            if setter is not state.setter:
                raise AllocatorBudgetRefused("The active cache-limit API was replaced by foreign instrumentation.")
        else:
            previous = _limit(setter(CACHE_LIMIT_BYTES))
            state = _State(mx, setter, previous, emit)
            _ACTIVE[id(mx)] = state
            _emit(emit, {"event": "mlx_allocator_cache_budget_installed", "cache_limit_bytes": CACHE_LIMIT_BYTES,
                         "previous_limit_bytes": previous, "scope": "unused_allocator_cache_only",
                         "reclamation": "on_next_allocation", "active_or_kv_memory_capped": False,
                         "memory_or_wired_limit_changed": False, "forced_evaluation": False, "cache_cleared": False})
        state.references += 1
        return AllocatorCacheBudgetHandle(state)
