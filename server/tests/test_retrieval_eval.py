"""Tests for the Sage retrieval evaluation harness (services.retrieval_eval).

Runs the shipped clinical question bank end-to-end against the seeded knowledge
base plus the sample loader corpus, and asserts the metrics dict is well-formed
with a non-zero hit-rate on the exact-match questions.

The BGE-M3 dense embedder is absent in the dev venv, so retrieval runs on the
FTS/BM25 lexical path plus the lexical reranker. Embedder-dependent assertions are
guarded to that path; the lexical floor (exact-match hit-rate > 0) must hold
regardless.
"""
import os

import pytest

from database import db_session
from services import clinical_knowledge as ck
from services import corpus_loader
from services import retrieval_eval
from tests.knowledge_fixtures import lexical_knowledge_models

CORPUS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "corpus")

_SHARED_TABLES = (
    "clinical_knowledge_vec",
    "clinical_knowledge_fts",
    "clinical_knowledge",
    "clinical_knowledge_meta",
    "corpus_documents",
)


def _drop_store():
    with db_session() as db:
        # Loading sqlite-vec is required to DROP the vec0 virtual table (and its
        # shadow tables); without it the drop errors and stale rows persist,
        # breaking a subsequent rebuild with UNIQUE-constraint failures.
        try:
            import sqlite_vec
            db.enable_load_extension(True)
            sqlite_vec.load(db)
        except Exception:
            pass
        for t in _SHARED_TABLES:
            try:
                db.execute(f"DROP TABLE IF EXISTS {t}")
            except Exception:
                pass
        db.commit()


@pytest.fixture(scope="module")
def seeded_corpus():
    """Seed the built-in corpus and load the sample loader corpus, then tear down
    the shared store so sibling test modules start clean."""
    with lexical_knowledge_models() as seed_builtin:
        _drop_store()
        seed_builtin()
        corpus_loader.load_directory(CORPUS_DIR, default_trust_tier=3)
        yield
        _drop_store()


def test_questions_bank_loads():
    questions = retrieval_eval.load_questions()
    assert len(questions) >= 15
    for q in questions:
        assert q["query"]
        assert q["expected_source"] or q["expected_keywords"]


def test_eval_runs_and_returns_metrics(seeded_corpus):
    metrics = retrieval_eval.run_eval(k=5)

    # Metric keys present and well-typed.
    assert metrics["num_questions"] >= 15
    assert "mrr" in metrics
    assert "hit_rate" in metrics
    for kk in (1, 3, 5):
        assert f"recall@{kk}" in metrics
        assert f"hit_rate@{kk}" in metrics
        assert 0.0 <= metrics[f"recall@{kk}"] <= 1.0
        assert 0.0 <= metrics[f"hit_rate@{kk}"] <= 1.0
    assert 0.0 <= metrics["mrr"] <= 1.0

    # Monotonicity: recall/hit-rate cannot decrease as k grows.
    assert metrics["recall@1"] <= metrics["recall@3"] <= metrics["recall@5"]
    assert metrics["hit_rate@1"] <= metrics["hit_rate@3"] <= metrics["hit_rate@5"]

    # The lexical floor: exact-match questions must be retrievable even without the
    # dense embedder.
    assert metrics["exact_match_hit_rate"] > 0.0
    assert metrics["hit_rate"] > 0.0

    if ck._get_cross_encoder() is None and not corpus_loader._real_embedder_available():
        # Lexical-only path: the harness should still surface a solid majority of
        # exact-match targets via FTS + reranking.
        assert metrics["exact_match_hit_rate"] >= 0.5


def test_eval_per_question_shape(seeded_corpus):
    metrics = retrieval_eval.run_eval(k=5)
    assert len(metrics["per_question"]) == metrics["num_questions"]
    sample = metrics["per_question"][0]
    for key in ("id", "query", "expected_source", "retrieved", "first_relevant_rank", "hit"):
        assert key in sample


def test_eval_exact_match_questions_hit(seeded_corpus):
    """Spot-check that clearly-unique seed docs are retrieved for their questions."""
    questions = retrieval_eval.load_questions()
    by_id = {q["id"]: q for q in questions}
    for qid in ("Q-DKA", "Q-STEMI", "Q-CURB65", "Q-AKI-KDIGO"):
        q = by_id[qid]
        res = retrieval_eval.evaluate_question(q, ck.search_guidelines, k=5)
        assert res["first_relevant_rank"] is not None, f"{qid} not retrieved: {res['retrieved']}"


def test_eval_accepts_custom_search_fn(seeded_corpus):
    calls = []

    def fake_search(query, limit=5, rerank=True, approved_only=False):
        calls.append(query)
        return [{"document_id": "EXT-METAB-001", "title": "t", "content": "c"}]

    q = [{"id": "q1", "query": "dka", "expected_source": "EXT-METAB-001"}]
    metrics = retrieval_eval.run_eval(questions=[retrieval_eval._normalize_question(q[0], 0)],
                                      k=5, search_fn=fake_search)
    assert metrics["num_questions"] == 1
    assert metrics["hit_rate"] == 1.0
    assert calls == ["dka"]
