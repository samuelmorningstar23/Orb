"""Unused allocator cache release cannot alter model tensors or CPU results."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from services import knowledge_worker as kw

GIB = 1024 ** 3


class Allocator:
    def __init__(self, driver: int = 3 * GIB, live: int = GIB,
                 *, releasable: bool = True, error: Exception | None = None) -> None:
        self.driver = driver
        self.live = live
        self.releasable = releasable
        self.error = error
        self.calls: list[str] = []
        self.model_tensors = object()

    def driver_allocated_memory(self) -> int:
        self.calls.append("driver")
        return self.driver

    def current_allocated_memory(self) -> int:
        self.calls.append("live")
        return self.live

    def empty_cache(self) -> None:
        self.calls.append("empty")
        if self.error:
            raise self.error
        if self.releasable:
            self.driver = self.live + GIB // 4


def install(monkeypatch: Any, allocator: Allocator, available: bool = True) -> None:
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        mps=allocator, backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: available))))


def test_release_frees_only_unused_buffers_and_logs_measured_bytes(monkeypatch: Any, caplog: Any) -> None:
    allocator = Allocator()
    tensors = allocator.model_tensors
    install(monkeypatch, allocator)
    kw._release_unused_mps_cache()
    assert allocator.calls == ["driver", "live", "empty", "driver", "live"]
    assert allocator.live == GIB
    assert allocator.model_tensors is tensors
    assert "driver_before_bytes=3221225472" in caplog.text
    assert "live_before_bytes=1073741824" in caplog.text
    assert "driver_after_bytes=1342177280" in caplog.text
    assert "released_bytes=1879048192" in caplog.text


@pytest.mark.parametrize("driver,live", [(2 * GIB, GIB), (GIB, GIB), (GIB // 2, GIB)])
def test_no_release_below_or_at_one_gib_trigger(monkeypatch: Any, driver: int, live: int) -> None:
    allocator = Allocator(driver, live)
    install(monkeypatch, allocator)
    kw._release_unused_mps_cache()
    assert allocator.calls == ["driver", "live"]


def test_cpu_path_never_touches_mps_allocator(monkeypatch: Any) -> None:
    allocator = Allocator()
    install(monkeypatch, allocator, available=False)
    kw._release_unused_mps_cache()
    assert allocator.calls == []


def test_nonreleasable_framework_overhead_is_not_claimed_as_freed(monkeypatch: Any, caplog: Any) -> None:
    allocator = Allocator(releasable=False)
    install(monkeypatch, allocator)
    kw._release_unused_mps_cache()
    assert allocator.calls.count("empty") == 1
    assert allocator.driver == 3 * GIB
    assert "released_bytes=0" in caplog.text


@pytest.mark.parametrize("operation", ["embed", "rerank"])
def test_cpu_results_and_all_model_call_parameters_stay_exact(
    monkeypatch: Any, operation: str,
) -> None:
    allocator = Allocator()
    install(monkeypatch, allocator)
    calls = []
    if operation == "embed":
        vector = np.array([.125, -.25, .75], dtype=np.float32)

        class Embedder:
            def encode(self, texts: list, **kwargs: Any) -> dict:
                calls.append((texts, kwargs))
                return {"dense_vecs": [vector]}

        embedder = Embedder()
        monkeypatch.setattr(kw, "_worker_get_embedder", lambda name: embedder)
        assert kw._worker_embed(["exact synthetic text"], "same-installed-model", 8, 512) == [
            vector.tobytes()]
        assert calls == [(["exact synthetic text"], {"batch_size": 8, "max_length": 512})]
    else:
        scores = [.125, .987654321]

        def scorer(pairs: list) -> list:
            calls.append(pairs)
            return scores

        monkeypatch.setattr(kw, "_worker_get_reranker", lambda name: scorer)
        assert kw._worker_rerank("exact query", ["passage a", "passage b"], "same-model") == scores
        assert calls == [[["exact query", "passage a"], ["exact query", "passage b"]]]
    assert allocator.calls.count("empty") == 1


@pytest.mark.parametrize("model_fails", [False, True])
def test_cleanup_failure_cannot_discard_cpu_results_or_replace_original_model_error(
    monkeypatch: Any, caplog: Any, model_fails: bool,
) -> None:
    install(monkeypatch, Allocator(error=RuntimeError("NEVER-PERSIST-ALLOCATOR-DETAIL")))
    original = ValueError("the original model error")

    def scorer(pairs: list) -> list:
        if model_fails:
            raise original
        return [.25]

    monkeypatch.setattr(kw, "_worker_get_reranker", lambda name: scorer)
    if model_fails:
        with pytest.raises(ValueError) as raised:
            kw._worker_rerank("synthetic", ["passage"], "same-model")
        assert raised.value is original
    else:
        assert kw._worker_rerank("synthetic", ["passage"], "same-model") == [.25]
    assert "error_type=RuntimeError" in caplog.text
    assert "NEVER-PERSIST" not in caplog.text


def test_diagnostic_logging_failure_cannot_replace_model_error(monkeypatch: Any) -> None:
    install(monkeypatch, Allocator())
    original = ValueError("original")

    def scorer(pairs: list) -> list:
        raise original

    def broken_logging(*args: Any) -> None:
        raise OSError("diagnostic writer failed")

    monkeypatch.setattr(kw, "_worker_get_reranker", lambda name: scorer)
    monkeypatch.setattr(kw.logger, "warning", broken_logging)
    with pytest.raises(ValueError) as raised:
        kw._worker_rerank("synthetic", ["passage"])
    assert raised.value is original
