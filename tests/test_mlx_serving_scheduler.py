"""CPU-only request lifecycle fakes; no MLX import, listener or model."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import queue
import threading
import time
from pathlib import Path
from queue import Empty as QueueEmpty
from queue import Queue
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("orb_mlx_serving_scheduler", Path(__file__).resolve().parents[1] / "scripts/mlx_serving_scheduler.py")
assert SPEC is not None and SPEC.loader is not None
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)


def fixture(monkeypatch):
    class Context:
        def __init__(self):
            self.stops = 0
        def stop(self):
            self.stops += 1

    class ResponseGenerator:
        def __init__(self):
            self.requests = Queue()
            self._is_distributed = False
            self._rank = 0
        def generation_available(self):
            return True
        def _next_request(self, timeout=None):
            try:
                if timeout is not None:
                    return self.requests.get(timeout=timeout)
                return self.requests.get_nowait()
            except QueueEmpty:
                return None
        def _generate(self):
            raise AssertionError("Fake generation is never run")
        def _await_response(self, response_queue):
            return response_queue.get(timeout=1.0)
        def generate(self, request, generation_args, progress_callback=None):
            response_queue = Queue()
            self.requests.put((response_queue, request, generation_args))
            def _inner():
                while True:
                    response = self._await_response(response_queue)
                    if response is None:
                        break
                    if isinstance(response, Exception):
                        raise response
                    if isinstance(response, tuple):
                        if progress_callback is not None:
                            progress_callback(*response)
                        continue
                    yield response
            ctx = self._await_response(response_queue)
            if isinstance(ctx, Exception):
                raise ctx
            return ctx, _inner()

    class APIHandler:
        def __init__(self, generator, connection):
            self.response_generator, self.connection = generator, connection
        def handle_completion(self, request, stop_words):
            ctx, response = self.response_generator.generate(request, stop_words)
            try:
                return list(response)
            finally:
                ctx.stop()

    module = SimpleNamespace(__name__=scheduler.MODULE_NAME, ResponseGenerator=ResponseGenerator,
                             APIHandler=APIHandler, GenerationContext=Context)
    pins = {name: hashlib.sha256(inspect.getsource(getattr(ResponseGenerator, name)).encode()).hexdigest()
            for name in ("_next_request", "_generate", "_await_response", "generate")}
    pins["handle_completion"] = hashlib.sha256(inspect.getsource(APIHandler.handle_completion).encode()).hexdigest()
    monkeypatch.setattr(scheduler, "SOURCE_PINS", pins)
    monkeypatch.setattr(scheduler, "socket_disconnected", lambda connection: connection.closed)
    return module


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.005)


def handler_thread(module, generator, connection, request, answers, errors):
    def work():
        try:
            answers.append(module.APIHandler(generator, connection).handle_completion(request, []))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=work)
    thread.start()
    return thread


def test_burst_yields_to_unchanged_generation_after_eight_fifo_admissions(monkeypatch):
    module = fixture(monkeypatch)
    generator = module.ResponseGenerator()
    originals = [module.ResponseGenerator._generate, module.ResponseGenerator._await_response]
    bodies = [object() for _ in range(200)]
    for index, body in enumerate(bodies):
        generator.requests.put((Queue(), body, index))
    with scheduler.install_serving_scheduler(module, admissions_per_step=8):
        admitted, yields, since_yield = [], 0, 0
        while len(admitted) < 200:
            result = generator._next_request(timeout=None)
            if result is None:
                assert since_yield == 8
                yields, since_yield = yields + 1, 0
            else:
                admitted.append(result)
                since_yield += 1
                assert since_yield <= 8
        assert [row[1] for row in admitted] == bodies
        assert [row[2] for row in admitted] == list(range(200))
        assert yields == 24
        assert [module.ResponseGenerator._generate, module.ResponseGenerator._await_response] == originals


def test_queued_reader_cancels_before_context_and_only_its_own_fifo_item(monkeypatch):
    module = fixture(monkeypatch)
    generator, connection = module.ResponseGenerator(), SimpleNamespace(closed=False)
    answers, errors, events = [], [], []
    with scheduler.install_serving_scheduler(module, emit=events.append):
        thread = handler_thread(module, generator, connection, "cancel-this", answers, errors)
        wait_until(lambda: generator.requests.qsize() == 1)
        connection.closed = True
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], scheduler.BenchmarkClientDisconnected)
        other_body, other_args = object(), object()
        generator.requests.put((Queue(), other_body, other_args))
        selected = generator._next_request(timeout=.01)
        assert selected[1] is other_body and selected[2] is other_args
        assert answers == [] and generator.requests.empty()
    assert events[-1]["cancelled_readers"] == 1
    assert events[-1]["cancelled_queued_requests"] == 1


def test_active_disconnect_stops_its_native_context_and_other_reader_completes(monkeypatch):
    module = fixture(monkeypatch)
    generator = module.ResponseGenerator()
    first, second = SimpleNamespace(closed=False), SimpleNamespace(closed=False)
    first_answers, first_errors, second_answers, second_errors = [], [], [], []
    with scheduler.install_serving_scheduler(module):
        t1 = handler_thread(module, generator, first, "first", first_answers, first_errors)
        wait_until(lambda: generator.requests.qsize() == 1)
        q1, _, _ = generator._next_request(.01)
        c1 = module.GenerationContext()
        q1.put(c1)
        t2 = handler_thread(module, generator, second, "second", second_answers, second_errors)
        wait_until(lambda: generator.requests.qsize() == 1)
        q2, _, _ = generator._next_request(.01)
        c2 = module.GenerationContext()
        q2.put(c2)
        first.closed = True
        t1.join(timeout=1)
        assert not t1.is_alive() and c1.stops >= 1
        assert c2.stops == 0 and not q2.cancelled
        q2.put("unchanged full answer")
        q2.put(None)
        t2.join(timeout=1)
        assert not t2.is_alive()
        assert second_answers == [["unchanged full answer"]] and second_errors == []
        assert c2.stops == 1 and len(first_errors) == 1


def test_context_arriving_after_cancel_is_stopped_without_touching_other_context(monkeypatch):
    module = fixture(monkeypatch)
    first = scheduler._TrackedResponseQueue(lambda: True, module.GenerationContext, lambda: None)
    second = scheduler._TrackedResponseQueue(lambda: False, module.GenerationContext, lambda: None)
    with pytest.raises(scheduler.BenchmarkClientDisconnected):
        first.get(timeout=.01)
    c1, c2 = module.GenerationContext(), module.GenerationContext()
    first.put(c1)
    second.put(c2)
    assert c1.stops == 1 and c2.stops == 0
    assert second.get(timeout=.01) is c2


def test_header_failure_before_native_context_finally_still_stops_own_context(monkeypatch):
    module = fixture(monkeypatch)
    def failing_handler(self, request, stop_words):
        self.response_generator.generate(request, stop_words)
        raise BrokenPipeError("synthetic header failure before native finally")
    module.APIHandler.handle_completion = failing_handler
    monkeypatch.setitem(scheduler.SOURCE_PINS, "handle_completion", hashlib.sha256(inspect.getsource(failing_handler).encode()).hexdigest())
    generator, connection = module.ResponseGenerator(), SimpleNamespace(closed=False)
    answers, errors = [], []
    with scheduler.install_serving_scheduler(module):
        thread = handler_thread(module, generator, connection, "first", answers, errors)
        wait_until(lambda: generator.requests.qsize() == 1)
        response_queue, _, _ = generator._next_request(.01)
        context = module.GenerationContext()
        response_queue.put(context)
        thread.join(timeout=1)
        assert not thread.is_alive() and context.stops == 1 and response_queue.cancelled
        assert len(errors) == 1 and isinstance(errors[0], BrokenPipeError)


def test_direct_generate_keeps_native_queue_without_handler_scope(monkeypatch):
    module = fixture(monkeypatch)
    generator, result = module.ResponseGenerator(), []
    with scheduler.install_serving_scheduler(module):
        thread = threading.Thread(target=lambda: result.append(generator.generate("body", "args")))
        thread.start()
        wait_until(lambda: generator.requests.qsize() == 1)
        response_queue, body, args = generator._next_request(.01)
        assert type(response_queue) is Queue and (body, args) == ("body", "args")
        ctx = module.GenerationContext()
        response_queue.put(ctx)
        thread.join(timeout=1)
        assert result[0][0] is ctx


def test_nested_scope_restores_only_own_wrappers_and_refuses_configuration_drift(monkeypatch):
    module = fixture(monkeypatch)
    old_generate, old_next, old_handler = module.ResponseGenerator.generate, module.ResponseGenerator._next_request, module.APIHandler.handle_completion
    first = scheduler.install_serving_scheduler(module)
    second = scheduler.install_serving_scheduler(module)
    wrapper = module.ResponseGenerator._next_request
    with pytest.raises(scheduler.ServingSchedulerRefused, match="foreign"):
        scheduler.install_serving_scheduler(module, admissions_per_step=4)
    first.close()
    first.close()
    assert module.ResponseGenerator._next_request is wrapper
    second.close()
    assert module.ResponseGenerator.generate is old_generate
    assert module.ResponseGenerator._next_request is old_next
    assert module.APIHandler.handle_completion is old_handler
    handle = scheduler.install_serving_scheduler(module)
    foreign = lambda *args: None
    module.ResponseGenerator._next_request = foreign
    with pytest.raises(scheduler.ServingSchedulerRefused, match="foreign"):
        scheduler.install_serving_scheduler(module)
    handle.close()
    assert module.ResponseGenerator._next_request is foreign


def test_source_drift_and_bad_arguments_fail_before_any_mutation(monkeypatch):
    module = fixture(monkeypatch)
    original = module.ResponseGenerator.generate
    for count in (0, 33, True, 1.0):
        with pytest.raises(scheduler.ServingSchedulerRefused):
            scheduler.install_serving_scheduler(module, admissions_per_step=count)
    monkeypatch.setitem(scheduler.SOURCE_PINS, "_await_response", "0" * 64)
    with pytest.raises(scheduler.ServingSchedulerRefused, match="differs"):
        scheduler.install_serving_scheduler(module)
    assert module.ResponseGenerator.generate is original


def test_queue_get_keeps_timeout_and_nonblocking_contract(monkeypatch):
    module = fixture(monkeypatch)
    response = scheduler._TrackedResponseQueue(lambda: False, module.GenerationContext, lambda: None)
    with pytest.raises(queue.Empty):
        response.get(block=False)
    with pytest.raises(queue.Empty):
        response.get(timeout=.001)
    with pytest.raises(ValueError):
        response.get(timeout=-1)
    response.put("item")
    assert response.get(timeout=0) == "item"


def test_observer_failure_cannot_break_install_or_restore(monkeypatch):
    module = fixture(monkeypatch)
    original = module.ResponseGenerator.generate
    def fail(event):
        raise RuntimeError("observer")
    with scheduler.install_serving_scheduler(module, emit=fail):
        assert module.ResponseGenerator.generate is not original
    assert module.ResponseGenerator.generate is original


def test_socket_observation_never_consumes_bytes_or_confuses_readable_data_with_disconnect(monkeypatch):
    class Connection:
        value = b"request bytes"
        def fileno(self):
            return 4
        def recv(self, count, flags):
            assert count == 1
            assert flags == scheduler.socket.MSG_PEEK | scheduler.socket.MSG_DONTWAIT
            return self.value
    connection = Connection()
    monkeypatch.setattr(scheduler.socket, "socket", Connection)
    monkeypatch.setattr(scheduler.select, "select", lambda *args: ([connection], [], []))
    assert not scheduler.socket_disconnected(connection)
    connection.value = b""
    assert scheduler.socket_disconnected(connection)
    monkeypatch.setattr(scheduler.select, "select", lambda *args: ([], [], []))
    assert not scheduler.socket_disconnected(connection)


def test_socket_observation_only_expected_hangup_errors_cancel(monkeypatch):
    class Connection:
        error = OSError(scheduler.errno.EWOULDBLOCK, "not ready")
        def fileno(self):
            return 4
        def recv(self, count, flags):
            raise self.error
    connection = Connection()
    monkeypatch.setattr(scheduler.socket, "socket", Connection)
    monkeypatch.setattr(scheduler.select, "select", lambda *args: ([connection], [], []))
    assert not scheduler.socket_disconnected(connection)
    connection.error = ConnectionResetError("reader reset")
    assert scheduler.socket_disconnected(connection)


def test_installed_lifecycle_source_pins_without_importing_mlx():
    path = Path("/private/tmp/orb-mlx-benchmark-20261021/venv/lib/python3.12/site-packages/mlx_lm/server.py")
    if not path.is_file():
        pytest.skip("Pinned local benchmark software is not provisioned")
    source, observed = path.read_text(), {}
    lines = source.splitlines(keepends=True)
    for cls in ast.parse(source).body:
        if isinstance(cls, ast.ClassDef) and cls.name in {"ResponseGenerator", "APIHandler"}:
            for method in cls.body:
                if isinstance(method, ast.FunctionDef) and method.name in scheduler.SOURCE_PINS:
                    observed[method.name] = hashlib.sha256("".join(lines[method.lineno - 1:method.end_lineno]).encode()).hexdigest()
    assert observed == scheduler.SOURCE_PINS
