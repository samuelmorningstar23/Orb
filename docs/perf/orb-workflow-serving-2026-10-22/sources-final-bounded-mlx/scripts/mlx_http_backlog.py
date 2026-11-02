"""Scoped backlog256 for the owned MLX benchmark HTTP server only.

No MLX/network import, listener, model or patch is created on import. Installed
MLX-LM0.32.2 _run_http_server constructs ThreadingHTTPServer with Python3.12's
inherited backlog5. Its default class is captured in a function default, so
replacing a module class name alone would not fix the listener. This scope
substitutes an owned subclass only at the pinned construction call; the global
stdlib class, inference queue, model, clinical gates and deadlines stay intact.
This does not assert that every connection failure was caused by backlog5.
"""
from __future__ import annotations

import hashlib
import inspect
import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, Self

MODULE_NAME = "mlx_lm.server"
ORIGINAL_CONSTRUCTOR_SHA256 = "a95fdf1d166a42df1f666179b237c0089a4300e6c566754c211c01d342ace8bd"
BACKLOG = 256
ORIGINAL_BACKLOG = 5


class BacklogPatchRefused(ValueError):
    """The pinned construction function or original server class changed."""


class _State:
    def __init__(self, server: Any, original: Callable[..., Any], original_class: type,
                 signature: inspect.Signature, emit: Callable[[dict[str, Any]], Any] | None):
        self.server, self.original, self.original_class = server, original, original_class
        self.references = 0
        self.emit = emit
        self.emitted = False
        self.lock = threading.Lock()

        class OwnedBenchmarkHTTPServer(original_class):
            request_queue_size = BACKLOG

        self.owned_class = OwnedBenchmarkHTTPServer

        @wraps(original)
        def construct(*args: Any, **kwargs: Any) -> Any:
            self.verify_class()
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            if bound.arguments["server_class"] is not original_class:
                raise BacklogPatchRefused("An unexpected HTTP server class cannot receive the owned backlog policy.")
            bound.arguments["server_class"] = OwnedBenchmarkHTTPServer
            if not self.emitted:
                with self.lock:
                    first = not self.emitted
                    self.emitted = True
                if first and emit is not None:
                    try:
                        emit({"event": "mlx_owned_http_backlog", "original_backlog": ORIGINAL_BACKLOG,
                              "owned_backlog": BACKLOG, "original_constructor_sha256": ORIGINAL_CONSTRUCTOR_SHA256,
                              "global_stdlib_class_modified": False, "model_or_deadlines_changed": False})
                    except Exception:  # noqa: BLE001, S110 - Optional observation cannot prevent construction.
                        pass
            return original(*bound.args, **bound.kwargs)

        self.wrapper = construct

    def verify_class(self) -> None:
        if (self.server.ThreadingHTTPServer is not self.original_class
                or self.original_class.request_queue_size != ORIGINAL_BACKLOG
                or self.owned_class.request_queue_size != BACKLOG):
            raise BacklogPatchRefused("The original or owned HTTP server class was changed by foreign instrumentation.")


_LOCK = threading.RLock()
_ACTIVE: dict[int, _State] = {}


class OwnedBacklogHandle:
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
                if state.server._run_http_server is state.wrapper:
                    state.server._run_http_server = state.original
                if _ACTIVE.get(id(state.server)) is state:
                    del _ACTIVE[id(state.server)]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def install_owned_http_backlog(server: Any, emit: Callable[[dict[str, Any]], Any] | None = None) -> OwnedBacklogHandle:
    """Scope only this explicitly supplied MLX server's construction function."""
    original = getattr(server, "_run_http_server", None)
    original_class = getattr(server, "ThreadingHTTPServer", None)
    if (getattr(server, "__name__", None) != MODULE_NAME or not callable(original)
            or not isinstance(original_class, type) or (emit is not None and not callable(emit))):
        raise BacklogPatchRefused("A pinned mlx_lm.server construction function and original HTTP class are required.")
    with _LOCK:
        state = _ACTIVE.get(id(server))
        if state is not None:
            if original is not state.wrapper:
                raise BacklogPatchRefused("The active HTTP construction scope was replaced by foreign instrumentation.")
            state.verify_class()
        else:
            if hasattr(original, "__wrapped__"):
                raise BacklogPatchRefused("Original HTTP construction must be undecorated for source pinning.")
            try:
                actual = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
            except (OSError, TypeError) as exc:
                raise BacklogPatchRefused("Original HTTP construction source is unavailable.") from exc
            signature = inspect.signature(original)
            if (actual != ORIGINAL_CONSTRUCTOR_SHA256
                    or tuple(signature.parameters) != ("host", "port", "response_generator", "server_class", "handler_class")
                    or signature.parameters["server_class"].default is not original_class
                    or original_class.request_queue_size != ORIGINAL_BACKLOG):
                raise BacklogPatchRefused("The source-pinned original HTTP construction or backlog5 class differs.")
            state = _State(server, original, original_class, signature, emit)
            server._run_http_server = state.wrapper
            _ACTIVE[id(server)] = state
        state.references += 1
        return OwnedBacklogHandle(state)
