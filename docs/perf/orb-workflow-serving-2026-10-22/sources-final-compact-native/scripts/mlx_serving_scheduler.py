"""Scoped fairness and disconnect cancellation for the owned MLX benchmark.

Only the source-pinned MLX-LM 0.32.2 HTTP request lifecycle is changed. The
unchanged generation loop gets a turn after a bounded number of FIFO
admissions. A disconnected HTTP reader cancels its own queued request or
sets its own native GenerationContext.stop flag. Tokens, sampling, model
weights, context limits, caches and BatchGenerator computation are untouched.
Cancellation is cooperative; an already submitted GPU kernel is not aborted.
No MLX import, socket, model or patch is created by importing this module.
"""
from __future__ import annotations

import ast
import errno
import hashlib
import inspect
import queue
import select
import socket
import textwrap
import threading
import time
import weakref
from collections.abc import Callable
from functools import wraps
from typing import Any, Self

MODULE_NAME = "mlx_lm.server"
SOURCE_PINS = {
    "_next_request": "3d336154b808ee025445beb93cb6fb7c69a4c44aea52c577b4a568e643cf08d7",
    "_generate": "3a4259353eae53417f6cc455c0c1263639b514cb2b62e21be6cb4e72cb49cd55",
    "_await_response": "1c7d2351b4c63dabf6adcb9f327a3f6b88b7bc4dcea8c2dc1432d44b9c5757f5",
    "generate": "b16df1fda857f4ed9b2c46c629173fc99a8c51f2424ab70be21fd57a7fc8052d",
    "handle_completion": "90c9f5f662e952e53bddf4ea62d016b775f98181153e2e19f3285defe83abe8d",
}
POLL_SECONDS = 0.05


class ServingSchedulerRefused(ValueError):
    """The installed request lifecycle does not match the pinned contract."""


class BenchmarkClientDisconnected(RuntimeError):
    """A single owned benchmark HTTP reader closed its connection."""


def socket_disconnected(connection: Any) -> bool:
    """Observe a plain HTTP socket without consuming any request bytes."""
    if not isinstance(connection, socket.socket):
        return False
    if connection.fileno() < 0:
        return True
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        return connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except (BrokenPipeError, ConnectionResetError):
        return True
    except OSError as exc:
        # A descriptor closed concurrently with the observation is a hangup;
        # an unusual transient or unsupported observation is not cancellation.
        return exc.errno in {errno.EBADF, errno.ENOTCONN, errno.ECONNRESET}
    except ValueError:
        return connection.fileno() < 0


class _TrackedResponseQueue(queue.Queue):
    def __init__(self, checker: Callable[[], bool], context_type: type,
                 on_cancel: Callable[[], None]):
        super().__init__()
        self._orb_checker = checker
        self._orb_context_type = context_type
        self._orb_on_cancel = on_cancel
        self._orb_lock = threading.Lock()
        self._orb_cancelled = False
        self._orb_finished = False
        self._orb_context: Any = None

    @property
    def cancelled(self) -> bool:
        with self._orb_lock:
            return self._orb_cancelled

    @property
    def finished(self) -> bool:
        with self._orb_lock:
            return self._orb_finished

    def _received(self, value: Any) -> Any:
        if value is None:
            with self._orb_lock:
                self._orb_finished = True
        return value

    def cancel(self) -> None:
        with self._orb_lock:
            first = not self._orb_cancelled
            self._orb_cancelled = True
            context = self._orb_context
        if context is not None:
            context.stop()
        if first:
            self._orb_on_cancel()

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        if isinstance(item, self._orb_context_type):
            with self._orb_lock:
                self._orb_context = item
                cancelled = self._orb_cancelled
            if cancelled:
                context: Any = item
                context.stop()
        super().put(item, block=block, timeout=timeout)

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        if block and timeout is not None and timeout < 0:
            raise ValueError("'timeout' must be a non-negative number")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._orb_checker():
                self.cancel()
            if self.cancelled:
                raise BenchmarkClientDisconnected("Owned benchmark HTTP reader disconnected")
            if not block:
                return self._received(super().get(block=False))
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return self._received(super().get(block=False))
            try:
                return self._received(super().get(timeout=POLL_SECONDS if remaining is None else min(POLL_SECONDS, remaining)))
            except queue.Empty:
                if deadline is not None and time.monotonic() >= deadline:
                    raise


def _queue_substitution(original: Callable[..., Any], factory: Callable[[], queue.Queue]) -> Callable[..., Any]:
    """Replace only generate()'s one response Queue construction, source pinned."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
    selected = [node for node in ast.walk(tree)
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "response_queue"]
    if (len(selected) != 1 or not isinstance(selected[0].value, ast.Call)
            or not isinstance(selected[0].value.func, ast.Name)
            or selected[0].value.func.id != "Queue" or selected[0].value.args or selected[0].value.keywords):
        raise ServingSchedulerRefused("The native response Queue construction differs")
    selected[0].value.func.id = "_orb_benchmark_response_queue"
    namespace = {**original.__globals__, "_orb_benchmark_response_queue": factory}
    exec(compile(ast.fix_missing_locations(tree), inspect.getfile(original), "exec"), namespace)
    replacement = namespace[original.__name__]
    return wraps(original)(replacement)


class _State:
    def __init__(self, server: Any, admissions: int, emit: Callable[[dict[str, Any]], Any] | None):
        self.server, self.admissions, self.emit = server, admissions, emit
        self.references = 0
        self.local = threading.local()
        self.lock = threading.Lock()
        self.counts: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
        self.statistics = {"fairness_yields": 0, "cancelled_readers": 0, "cancelled_queued_requests": 0}
        self.original_generate = server.ResponseGenerator.generate
        self.original_next = server.ResponseGenerator._next_request
        self.original_handler = server.APIHandler.handle_completion

        def note(name: str) -> None:
            with self.lock:
                self.statistics[name] += 1

        def factory() -> queue.Queue:
            checker = getattr(self.local, "checker", None)
            if checker is None:
                return queue.Queue()
            response = _TrackedResponseQueue(checker, server.GenerationContext,
                                             lambda: note("cancelled_readers"))
            self.local.queues.append(response)
            return response

        self.generate_wrapper = _queue_substitution(self.original_generate, factory)

        @wraps(self.original_handler)
        def handle(handler: Any, *args: Any, **kwargs: Any) -> Any:
            previous, previous_queues = getattr(self.local, "checker", None), getattr(self.local, "queues", None)
            self.local.checker = lambda: socket_disconnected(handler.connection)
            self.local.queues = []
            try:
                return self.original_handler(handler, *args, **kwargs)
            finally:
                # Native header writes occur after generate returns a context
                # but before its ctx.stop() finally begins. Any early header
                # failure must still retire only this handler's unfinished work.
                for response in self.local.queues:
                    if not response.finished:
                        response.cancel()
                if previous is None:
                    del self.local.checker
                    del self.local.queues
                else:
                    self.local.checker = previous
                    self.local.queues = previous_queues

        @wraps(self.original_next)
        def next_request(generator: Any, timeout: float | None = None) -> Any:
            # Distributed generation broadcasts native queue items. This
            # benchmark scope deliberately leaves that path entirely native.
            if generator._is_distributed:
                return self.original_next(generator, timeout)
            count = self.counts.get(generator, 0)
            if timeout is None and count >= self.admissions:
                self.counts[generator] = 0
                note("fairness_yields")
                return None
            while True:
                result = self.original_next(generator, timeout)
                if result is None:
                    self.counts[generator] = 0
                    return None
                response_queue = result[0]
                if isinstance(response_queue, _TrackedResponseQueue) and response_queue.cancelled:
                    note("cancelled_queued_requests")
                    # Once a queued item was available, never wait a fresh
                    # timeout for each cancelled item behind it.
                    timeout = None
                    continue
                self.counts[generator] = count + 1
                return result

        self.handler_wrapper, self.next_wrapper = handle, next_request

    def observe(self, event: str) -> None:
        if self.emit is not None:
            try:
                self.emit({"event": event, "admissions_per_step": self.admissions,
                           "weights_or_prompt_or_sampling_changed": False,
                           "cancellation": "request_scoped_cooperative_http_disconnect",
                           **self.statistics})
            except Exception:  # noqa: BLE001 - Optional content-free observation.
                pass


_LOCK = threading.RLock()
_ACTIVE: dict[int, _State] = {}


class ServingSchedulerHandle:
    def __init__(self, state: _State):
        self._state, self._closed = state, False

    def close(self) -> None:
        with _LOCK:
            if self._closed:
                return
            self._closed = True
            state = self._state
            state.references -= 1
            if state.references:
                return
            for cls, name, wrapper, original in (
                (state.server.ResponseGenerator, "generate", state.generate_wrapper, state.original_generate),
                (state.server.ResponseGenerator, "_next_request", state.next_wrapper, state.original_next),
                (state.server.APIHandler, "handle_completion", state.handler_wrapper, state.original_handler),
            ):
                if getattr(cls, name) is wrapper:
                    setattr(cls, name, original)
            if _ACTIVE.get(id(state.server)) is state:
                del _ACTIVE[id(state.server)]
            state.observe("mlx_serving_scheduler_closed")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _verify_source(method: Any, name: str) -> None:
    if not callable(method) or hasattr(method, "__wrapped__"):
        raise ServingSchedulerRefused("The source-pinned native request lifecycle must be undecorated")
    try:
        actual = hashlib.sha256(inspect.getsource(method).encode()).hexdigest()
    except (OSError, TypeError) as exc:
        raise ServingSchedulerRefused("The native request lifecycle source is unavailable") from exc
    if actual != SOURCE_PINS[name]:
        raise ServingSchedulerRefused("The source-pinned native request lifecycle differs")


def install_serving_scheduler(server: Any, *, admissions_per_step: int = 8,
                              emit: Callable[[dict[str, Any]], Any] | None = None) -> ServingSchedulerHandle:
    """Scope fairness/cancellation to the explicitly supplied owned server."""
    if (getattr(server, "__name__", None) != MODULE_NAME
            or type(admissions_per_step) is not int or not 1 <= admissions_per_step <= 32
            or (emit is not None and not callable(emit))):
        raise ServingSchedulerRefused("A pinned owned server and admissions1..32 are required")
    with _LOCK:
        state = _ACTIVE.get(id(server))
        if state is not None:
            if (admissions_per_step != state.admissions
                    or server.ResponseGenerator.generate is not state.generate_wrapper
                    or server.ResponseGenerator._next_request is not state.next_wrapper
                    or server.APIHandler.handle_completion is not state.handler_wrapper):
                raise ServingSchedulerRefused("The active scheduler scope was changed by foreign instrumentation")
        else:
            for name in ("_next_request", "_generate", "_await_response", "generate"):
                _verify_source(getattr(server.ResponseGenerator, name, None), name)
            _verify_source(getattr(server.APIHandler, "handle_completion", None), "handle_completion")
            state = _State(server, admissions_per_step, emit)
            server.ResponseGenerator.generate = state.generate_wrapper
            server.ResponseGenerator._next_request = state.next_wrapper
            server.APIHandler.handle_completion = state.handler_wrapper
            _ACTIVE[id(server)] = state
            state.observe("mlx_serving_scheduler_installed")
        state.references += 1
        return ServingSchedulerHandle(state)
