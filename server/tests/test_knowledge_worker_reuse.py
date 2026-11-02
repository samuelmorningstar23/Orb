"""Exact BGE output reuse never caches clinical retrieval/approval decisions."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest

from services import knowledge_worker as kw


class ControlledPool:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple, Future]] = []
        self.lock = threading.Lock()

    def submit(self, fn: Any, *args: Any) -> Future:
        future: Future = Future()
        with self.lock:
            self.calls.append((fn, args, future))
        return future


@pytest.fixture
def pool(monkeypatch: Any) -> ControlledPool:
    fake = ControlledPool()
    kw._clear_output_reuse()
    kw._clear_embedding_batches()
    monkeypatch.setenv("ORB_KNOWLEDGE_QUERY_MICROBATCH", "1")
    monkeypatch.setattr(kw, "_pool_failures", 0)
    monkeypatch.setattr(kw, "_ensure_pool", lambda: fake)
    monkeypatch.setattr(kw, "_reuse_counts", dict.fromkeys(kw._reuse_counts, 0))
    yield fake
    kw._clear_output_reuse()
    kw._clear_embedding_batches()


def until(predicate: Any) -> None:
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.001)


def test_duplicate_burst_submits_once_and_returns_independent_lists(pool: ControlledPool) -> None:
    with ThreadPoolExecutor(max_workers=16) as callers:
        futures = [
            callers.submit(kw.embed_texts_isolated, ["private synthetic input"], "same-model")
            for _ in range(200)
        ]
        until(lambda: kw.output_reuse_metrics()["coalesced"] == 15)
        assert len(pool.calls) == 1
        pool.calls[0][2].set_result([b"unchanged-vector"])
        outputs = [future.result(timeout=3) for future in futures]
    assert outputs == [[b"unchanged-vector"]] * 200
    outputs[0].append(b"caller mutation")
    assert outputs[1] == [b"unchanged-vector"]
    assert kw.embed_texts_isolated(["private synthetic input"], "same-model") == [
        b"unchanged-vector"
    ]
    metrics = kw.output_reuse_metrics()
    assert metrics["submitted"] == 1
    assert metrics["coalesced"] + metrics["hits"] == 200
    assert "private" not in repr(metrics)
    assert "private" not in repr(kw._reuse_results)


@pytest.mark.parametrize(
    "changed",
    [
        {"texts": ["a", "bc"]},
        {"texts": ["ab", "c "]},
        {"texts": ["c", "ab"]},
        {"model_name": "model-b"},
        {"batch_size": 1},
        {"max_length": 256},
    ],
)
def test_exact_embedding_identity_includes_boundaries_order_model_and_config(
    pool: ControlledPool, changed: dict
) -> None:
    initial = dict(texts=["ab", "c"], model_name="model-a", batch_size=8, max_length=512)
    baseline = kw._output_key("embed", "model-a", initial["texts"], 8, 512)
    updated = {**initial, **changed}
    different = kw._output_key(
        "embed", updated["model_name"], updated["texts"], updated["batch_size"],
        updated["max_length"],
    )
    assert baseline != different


def test_candidate_edits_acl_subset_and_order_get_fresh_rerank_scores(
    pool: ControlledPool, monkeypatch: Any
) -> None:
    monkeypatch.setenv("SAGE_RERANK_MODEL", "reranker-a")
    with ThreadPoolExecutor(max_workers=1) as callers:
        for query, passages, expected in [
            ("q", ["allowed-a", "allowed-b"], [0.1, 0.9]),
            ("q", ["allowed-a"], [0.1]),
            ("q", ["allowed-b", "allowed-a"], [0.9, 0.1]),
            ("q", ["edited-a", "allowed-b"], [0.2, 0.9]),
            ("different q", ["allowed-a", "allowed-b"], [0.4, 0.5]),
        ]:
            future = callers.submit(kw.rerank_scores_isolated, query, passages)
            until(lambda: len(pool.calls) == len(kw._reuse_results) + 1)
            assert pool.calls[-1][1] == (query, passages, "reranker-a")
            pool.calls[-1][2].set_result(expected)
            assert future.result(timeout=3) == expected
        assert kw.rerank_scores_isolated("q", ["allowed-a", "allowed-b"]) == [0.1, 0.9]
    assert len(pool.calls) == 5


def test_failures_are_shared_but_never_cached(pool: ControlledPool) -> None:
    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(kw.rerank_scores_isolated, "q", ["p"])
        second = callers.submit(kw.rerank_scores_isolated, "q", ["p"])
        until(lambda: kw.output_reuse_metrics()["coalesced"] == 1)
        pool.calls[0][2].set_exception(ValueError("synthetic scoring failure"))
        for future in (first, second):
            with pytest.raises(kw.KnowledgeWorkerError, match="synthetic scoring failure"):
                future.result(timeout=3)
        retry = callers.submit(kw.rerank_scores_isolated, "q", ["p"])
        until(lambda: len(pool.calls) == 2)
        pool.calls[1][2].set_result([0.7])
        assert retry.result(timeout=3) == [0.7]
    assert kw.output_reuse_metrics()["entries"] == 1


def test_waiter_timeout_never_cancels_shared_physical_work(
    pool: ControlledPool, monkeypatch: Any
) -> None:
    monkeypatch.setattr(kw, "EMBED_TIMEOUT_S", 0.01)
    with pytest.raises(kw.KnowledgeWorkerError):
        kw.embed_texts_isolated(["x"], "model")
    assert not pool.calls[0][2].cancelled()
    assert kw.output_reuse_metrics()["pending"] == 1
    pool.calls[0][2].set_result([b"vector"])
    assert kw.embed_texts_isolated(["x"], "model") == [b"vector"]
    assert len(pool.calls) == 1


def test_ttl_lru_byte_and_pending_bounds(pool: ControlledPool, monkeypatch: Any) -> None:
    clock = [100.0]
    monkeypatch.setattr(kw.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(kw, "_REUSE_MAX_ENTRIES", 2)
    monkeypatch.setattr(kw, "_REUSE_MAX_BYTES", 300)
    for number in range(3):
        future = kw._shared_output_future(pool, (number,), len, ())
        future.set_result([b"a" * 10])
    assert kw.output_reuse_metrics()["entries"] == 2
    assert kw.output_reuse_metrics()["bytes"] == 276
    assert kw.output_reuse_metrics()["evicted"] == 1
    oversized = kw._shared_output_future(pool, ("big",), len, ())
    oversized.set_result([b"a" * 301])
    assert kw.output_reuse_metrics()["entries"] == 2
    clock[0] += kw._REUSE_TTL_S + 1
    kw._shared_output_future(pool, ("pending",), len, ())
    assert kw.output_reuse_metrics()["entries"] == 0
    assert kw.output_reuse_metrics()["bytes"] == 0
    monkeypatch.setattr(kw, "_REUSE_MAX_PENDING", 1)
    with pytest.raises(kw.KnowledgeWorkerError, match="pending request bound"):
        kw._shared_output_future(pool, ("another",), len, ())


def test_clear_discards_old_pool_results_and_new_pool_has_fresh_identity(
    pool: ControlledPool,
) -> None:
    stale = kw._shared_output_future(pool, ("same",), len, ())
    kw._clear_output_reuse()
    stale.set_result([1.0])
    assert kw.output_reuse_metrics()["entries"] == 0
    new_pool = ControlledPool()
    fresh = kw._shared_output_future(new_pool, ("same",), len, ())
    fresh.set_result([2.0])
    assert len(new_pool.calls) == 1
    assert kw._shared_output_future(new_pool, ("same",), len, ()).result() == [2.0]


def test_completed_cache_still_checks_appliance_model_switch(
    pool: ControlledPool, monkeypatch: Any
) -> None:
    key = kw._output_key("embed", "model", ["x"], 8, 512)
    kw._shared_output_future(pool, key, len, ()).set_result([b"v"])

    def off() -> None:
        raise kw.KnowledgeWorkerError("models are switched off")

    monkeypatch.setattr(kw, "_ensure_pool", off)
    with pytest.raises(kw.KnowledgeWorkerError, match="switched off"):
        kw.embed_texts_isolated(["x"], "model")


def test_loaded_model_names_are_pinned_until_worker_restart(monkeypatch: Any) -> None:
    monkeypatch.setattr(kw, "_w_embedder", object())
    monkeypatch.setattr(kw, "_w_embedder_name", "loaded-embedder")
    monkeypatch.setattr(kw, "_w_reranker", lambda _: [])
    monkeypatch.setattr(kw, "_w_reranker_name", "loaded-reranker")
    with pytest.raises(RuntimeError, match="changed; restart"):
        kw._worker_get_embedder("different-embedder")
    with pytest.raises(RuntimeError, match="changed; restart"):
        kw._worker_get_reranker("different-reranker")


def test_opt_in_batches_four_distinct_queries_and_scatters_exact_vectors(
    pool: ControlledPool, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ORB_KNOWLEDGE_QUERY_MICROBATCH", "4")
    monkeypatch.setattr(kw, "_EMBED_BATCH_WINDOW_S", 10)
    with ThreadPoolExecutor(max_workers=4) as callers:
        requests = [callers.submit(kw.embed_texts_isolated, [f"exact unique query {number}"],
                                   "same-model", 8, 512) for number in range(4)]
        until(lambda: len(pool.calls) == 1)
        fn, args, physical = pool.calls[0]
        assert fn is kw._worker_embed
        assert set(args[0]) == {f"exact unique query {number}" for number in range(4)}
        assert args[1:] == ("same-model", 8, 512)
        physical.set_result([text.encode() for text in args[0]])
        assert [request.result(timeout=3) for request in requests] == [
            [f"exact unique query {number}".encode()] for number in range(4)
        ]
    metrics = kw.output_reuse_metrics()
    assert metrics["embedding_batch_pending"] == 0
    assert metrics["embedding_batch_max"] <= 4


def test_microbatch_timeout_flush_preserves_model_and_token_configuration(
    pool: ControlledPool, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ORB_KNOWLEDGE_QUERY_MICROBATCH", "4")
    with ThreadPoolExecutor(max_workers=1) as callers:
        request = callers.submit(kw.embed_texts_isolated, ["verbatim"], "model", 8, 256)
        until(lambda: len(pool.calls) == 1)
        assert pool.calls[0][1] == (["verbatim"], "model", 8, 256)
        pool.calls[0][2].set_result([b"result"])
        assert request.result(timeout=3) == [b"result"]


def test_microbatch_never_merges_models_configs_or_corpus_batches(
    pool: ControlledPool, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ORB_KNOWLEDGE_QUERY_MICROBATCH", "4")
    monkeypatch.setattr(kw, "_EMBED_BATCH_WINDOW_S", 10)
    futures = [
        kw._submit_worker_output(pool, kw._worker_embed, (["a"], "model-a", 8, 512)),
        kw._submit_worker_output(pool, kw._worker_embed, (["b"], "model-b", 8, 512)),
        kw._submit_worker_output(pool, kw._worker_embed, (["c"], "model-a", 8, 256)),
    ]
    assert len(kw._embedding_batches) == 3
    assert len(pool.calls) == 0
    corpus = kw._submit_worker_output(pool, kw._worker_embed, (["a", "b"], "model-a", 8, 512))
    assert pool.calls[0][1] == (["a", "b"], "model-a", 8, 512)
    pool.calls[0][2].set_result([b"a", b"b"])
    assert corpus.result() == [b"a", b"b"]
    kw._clear_embedding_batches()
    for future in futures:
        with pytest.raises(kw.KnowledgeWorkerError, match="shut down"):
            future.result()


def test_microbatch_errors_reach_every_waiter_without_partial_vectors(
    pool: ControlledPool, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ORB_KNOWLEDGE_QUERY_MICROBATCH", "4")
    monkeypatch.setattr(kw, "_EMBED_BATCH_WINDOW_S", 10)
    futures = [kw._submit_worker_output(pool, kw._worker_embed, ([str(number)], "model", 8, 512))
               for number in range(4)]
    pool.calls[0][2].set_result([b"only-one-vector"])
    for future in futures:
        with pytest.raises(kw.KnowledgeWorkerError, match="incomplete vectors"):
            future.result()


def test_fifth_arrival_cannot_expand_detached_batch_or_be_flushed_by_its_old_timer(
    pool: ControlledPool, monkeypatch: Any
) -> None:
    monkeypatch.setenv("ORB_KNOWLEDGE_QUERY_MICROBATCH", "4")
    monkeypatch.setattr(kw, "_EMBED_BATCH_WINDOW_S", 10)
    args = lambda number: ([str(number)], "model", 8, 512)
    first = [kw._submit_worker_output(pool, kw._worker_embed, args(number)) for number in range(3)]
    key = next(iter(kw._embedding_batches))
    old_timer = kw._embedding_batches[key][0]
    entered, release = threading.Event(), threading.Event()
    dispatch = kw._dispatch_embedding_batch

    def delayed(key: tuple, group: Any) -> None:
        entered.set()
        assert release.wait(3)
        dispatch(key, group)

    monkeypatch.setattr(kw, "_dispatch_embedding_batch", delayed)
    with ThreadPoolExecutor(max_workers=1) as thread:
        fourth = thread.submit(kw._submit_worker_output, pool, kw._worker_embed, args(3))
        assert entered.wait(3)
        fifth = kw._submit_worker_output(pool, kw._worker_embed, args(4))
        assert len(kw._embedding_batches[key][1]) == 1
        kw._flush_embedding_batch(key, old_timer)
        assert len(kw._embedding_batches[key][1]) == 1
        release.set()
        fourth_result = fourth.result(timeout=3)
    assert len(pool.calls[0][1][0]) == 4
    pool.calls[0][2].set_result([b"a", b"b", b"c", b"d"])
    assert [future.result() for future in first] == [[b"a"], [b"b"], [b"c"]]
    assert fourth_result.result() == [b"d"]
    kw._clear_embedding_batches()
    with pytest.raises(kw.KnowledgeWorkerError):
        fifth.result()
