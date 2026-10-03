"""Process-isolation bridge for the clinical knowledge models.

The embedder/reranker used to run inside the API process, where their
GIL-heavy loads and inference starved the uvloop event loop — /api/health
timed out for minutes whenever the critical-alert prefetch storm ran
(2026-07-30 incident). These tests pin the routing + degradation contract:

  * with isolation ON, the parent process must NEVER load the heavy models —
    embedding/reranking route to services.knowledge_worker;
  * every worker failure degrades exactly like the legacy fallbacks
    (lexical-only retrieval, lexical reranker), never an exception upward.
"""
import os

import pytest

from services import clinical_knowledge as ck
from services import knowledge_worker as kw


@pytest.fixture
def isolated(monkeypatch):
    """Force isolation semantics regardless of the suite's in-process default.

    Also clears the module-level model caches: other suite tests may have
    populated them (e.g. the skip-mode MockEmbedder), and a cache hit returns
    before the parent-load guards run — the guards protect LOADS, not caches.
    """
    monkeypatch.delenv("ORB_KNOWLEDGE_INPROCESS", raising=False)
    monkeypatch.delenv("ORB_KNOWLEDGE_WORKER", raising=False)
    monkeypatch.delenv("ORB_SKIP_KNOWLEDGE_BASE", raising=False)
    monkeypatch.setattr(ck, "_embed_model", None)
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", False)
    yield


def test_embed_routes_to_worker(isolated, monkeypatch):
    sentinel = [b"vec-a", b"vec-b"]
    calls = {}

    def _fake(texts, model_name, batch_size=8, max_length=512):
        calls["texts"] = list(texts)
        calls["model"] = model_name
        return sentinel

    monkeypatch.setattr(ck, "embed_texts_isolated", _fake)
    out = ck._embed_texts(["alpha", "beta"])
    assert out == sentinel
    assert calls["texts"] == ["alpha", "beta"]
    assert calls["model"] == ck.EMBEDDING_MODEL


def test_parent_never_loads_embedder_when_isolated(isolated):
    with pytest.raises(RuntimeError, match="isolation"):
        ck._get_embedder()


def test_parent_never_loads_reranker_when_isolated(isolated, monkeypatch):
    # Reset the availability cache so the guard (not the cache) is exercised.
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", False)
    with pytest.raises(RuntimeError, match="isolation"):
        ck._get_cross_encoder()


def test_skip_mode_returns_zero_vectors_without_worker(isolated, monkeypatch):
    monkeypatch.setenv("ORB_SKIP_KNOWLEDGE_BASE", "1")

    def _boom(*a, **k):
        raise AssertionError("worker must not be touched in skip mode")

    monkeypatch.setattr(ck, "embed_texts_isolated", _boom)
    out = ck._embed_texts(["x"])
    assert len(out) == 1
    assert out[0] == b"\x00" * (ck.EMBEDDING_DIM * 4)


def test_worker_failure_degrades_rerank_to_lexical(isolated, monkeypatch):
    def _boom(query, passages):
        raise kw.KnowledgeWorkerError("worker crashed")

    monkeypatch.setattr(ck, "rerank_scores_isolated", _boom)
    candidates = [
        {"title": "Sepsis 1-Hour Bundle", "content": "lactate cultures antibiotics",
         "trust_tier": 3, "fusion_score": 0.5},
        {"title": "Unrelated", "content": "totally different topic",
         "trust_tier": 3, "fusion_score": 0.4},
    ]
    out = ck.rerank_results("sepsis antibiotics lactate", candidates)
    assert len(out) == 2
    assert all(c["rerank_method"] == "lexical" for c in out)
    # The lexically-relevant doc must outrank the unrelated one.
    assert out[0]["title"] == "Sepsis 1-Hour Bundle"


def test_worker_success_marks_cross_encoder(isolated, monkeypatch):
    monkeypatch.setattr(ck, "rerank_scores_isolated", lambda q, ps: [0.1, 0.9])
    candidates = [
        {"title": "A", "content": "a", "trust_tier": 3, "fusion_score": 0.5},
        {"title": "B", "content": "b", "trust_tier": 3, "fusion_score": 0.4},
    ]
    out = ck.rerank_results("query", candidates)
    assert [c["title"] for c in out] == ["B", "A"]
    assert all(c["rerank_method"] == "cross_encoder" for c in out)


def test_embed_failure_degrades_search_to_lexical_only(isolated, monkeypatch):
    """search_guidelines must survive a dead worker: dense retrieval is skipped
    and lexical (FTS/LIKE) results still come back."""
    def _boom(*a, **k):
        raise kw.KnowledgeWorkerError("worker unavailable")

    monkeypatch.setattr(ck, "embed_texts_isolated", _boom)
    # Must not raise. On the test DB the KB tables may be empty — the contract
    # is graceful degradation, not specific hits.
    out = ck.search_guidelines("sepsis lactate antibiotics", limit=3)
    assert isinstance(out, list)


def test_pool_restart_budget_disables_after_repeated_crashes(monkeypatch):
    """After the restart budget is exhausted the bridge fails fast instead of
    respawning a crashing 2 GB child forever."""
    monkeypatch.setattr(kw, "_pool_failures", kw._MAX_POOL_RESTARTS + 1)
    with pytest.raises(kw.KnowledgeWorkerError, match="disabled"):
        kw._submit(len, [], timeout=1)


def test_in_process_mode_keeps_legacy_paths(monkeypatch):
    """ORB_KNOWLEDGE_INPROCESS=1 (the suite default) must keep _use_isolated
    False so existing in-process tests exercise the legacy code."""
    monkeypatch.setenv("ORB_KNOWLEDGE_INPROCESS", "1")
    assert ck._use_isolated() is False
