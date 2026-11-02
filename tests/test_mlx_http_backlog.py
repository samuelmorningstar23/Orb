"""Fake constructors only: no network socket, MLX import or model."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("orb_mlx_http_backlog", Path(__file__).resolve().parents[1] / "scripts/mlx_http_backlog.py")
assert SPEC is not None and SPEC.loader is not None
backlog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backlog)


def server_fixture(monkeypatch):
    calls = []
    class ThreadingHTTPServer:
        request_queue_size = 5
        address_family = "original-family"
    def constructor(host, port, response_generator, server_class=ThreadingHTTPServer, handler_class=None):
        calls.append((host, port, response_generator, server_class, handler_class))
        server_class.address_family = "owned-family"
        return server_class
    module = SimpleNamespace(__name__=backlog.MODULE_NAME, ThreadingHTTPServer=ThreadingHTTPServer,
                             _run_http_server=constructor)
    monkeypatch.setattr(backlog, "ORIGINAL_CONSTRUCTOR_SHA256", hashlib.sha256(inspect.getsource(constructor).encode()).hexdigest())
    return module, calls


def test_owned_subclass_replaces_captured_default_without_global_class_or_arguments_changes(monkeypatch):
    module, calls = server_fixture(monkeypatch)
    original_fn, original_class = module._run_http_server, module.ThreadingHTTPServer
    generator, handler, events = object(), object(), []
    with backlog.install_owned_http_backlog(module, events.append):
        owned = module._run_http_server("127.0.0.1", 12345, generator, handler_class=handler)
        assert owned is not original_class and issubclass(owned, original_class)
        assert owned.request_queue_size == 256 and original_class.request_queue_size == 5
        assert original_class.address_family == "original-family"
        assert calls == [("127.0.0.1", 12345, generator, owned, handler)]
        assert module.ThreadingHTTPServer is original_class
        module._run_http_server("127.0.0.1", 12345, generator, original_class, handler)
    assert module._run_http_server is original_fn and len(events) == 1


def test_nested_scope_closes_idempotently_and_restores_only_owned_function(monkeypatch):
    module, _ = server_fixture(monkeypatch)
    original = module._run_http_server
    first = backlog.install_owned_http_backlog(module)
    wrapper = module._run_http_server
    second = backlog.install_owned_http_backlog(module)
    first.close()
    first.close()
    assert module._run_http_server is wrapper
    second.close()
    second.close()
    assert module._run_http_server is original
    handle = backlog.install_owned_http_backlog(module)
    foreign = lambda *args, **kwargs: None
    module._run_http_server = foreign
    with pytest.raises(backlog.BacklogPatchRefused, match="foreign"):
        backlog.install_owned_http_backlog(module)
    handle.close()
    assert module._run_http_server is foreign


def test_class_replacement_backlog_drift_and_custom_constructor_class_fail_closed(monkeypatch):
    module, calls = server_fixture(monkeypatch)
    with backlog.install_owned_http_backlog(module):
        with pytest.raises(backlog.BacklogPatchRefused, match="unexpected"):
            module._run_http_server("127.0.0.1", 12345, object(), server_class=object)
        module.ThreadingHTTPServer.request_queue_size = 99
        with pytest.raises(backlog.BacklogPatchRefused, match="foreign"):
            module._run_http_server("127.0.0.1", 12345, object())
        with pytest.raises(backlog.BacklogPatchRefused, match="foreign"):
            backlog.install_owned_http_backlog(module)
    assert calls == [] and module.ThreadingHTTPServer.request_queue_size == 99


def test_source_pin_failure_and_emitter_errors_do_not_change_original_class(monkeypatch):
    module, calls = server_fixture(monkeypatch)
    original = module._run_http_server
    expected = backlog.ORIGINAL_CONSTRUCTOR_SHA256
    monkeypatch.setattr(backlog, "ORIGINAL_CONSTRUCTOR_SHA256", "0" * 64)
    with pytest.raises(backlog.BacklogPatchRefused, match="differs"):
        backlog.install_owned_http_backlog(module)
    assert module._run_http_server is original
    monkeypatch.setattr(backlog, "ORIGINAL_CONSTRUCTOR_SHA256", expected)
    def failure(event):
        raise RuntimeError("synthetic diagnostic failure")
    with backlog.install_owned_http_backlog(module, failure):
        assert module._run_http_server("127.0.0.1", 12345, None).request_queue_size == 256
    assert len(calls) == 1 and module.ThreadingHTTPServer.request_queue_size == 5


def test_original_constructor_errors_are_not_swallowed(monkeypatch):
    module, _ = server_fixture(monkeypatch)
    def failure(host, port, response_generator, server_class=module.ThreadingHTTPServer, handler_class=None):
        raise OSError("synthetic bind failure")
    module._run_http_server = failure
    monkeypatch.setattr(backlog, "ORIGINAL_CONSTRUCTOR_SHA256", hashlib.sha256(inspect.getsource(failure).encode()).hexdigest())
    with pytest.raises(OSError, match="bind failure"), backlog.install_owned_http_backlog(module):
        module._run_http_server("127.0.0.1", 12345, None)
    assert module._run_http_server is failure


def test_installed_construction_source_pin_without_importing_mlx():
    path = Path("/private/tmp/orb-mlx-benchmark-20261021/venv/lib/python3.12/site-packages/mlx_lm/server.py")
    if not path.is_file():
        pytest.skip("Pinned local benchmark software is not provisioned")
    source = path.read_text()
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_run_http_server")
    selected = "".join(source.splitlines(keepends=True)[function.lineno - 1:function.end_lineno])
    assert hashlib.sha256(selected.encode()).hexdigest() == backlog.ORIGINAL_CONSTRUCTOR_SHA256
