"""Tests for the Sage second-stage reranker (services.clinical_knowledge).

Covers:
  * the lexical/heuristic reranker reorders a known candidate set sensibly,
  * it never crashes when the cross-encoder / BGE embedder is absent (dev venv),
  * empty / degenerate inputs are handled,
  * search_guidelines still returns results end-to-end with reranking on,
  * the approval_only flag filters draft loader docs while keeping seed docs.

The cross-encoder is not installed in the dev venv, so the lexical path is what
runs here; cross-encoder-dependent assertions are avoided.
"""
import os

import pytest

from database import db_session
from services import clinical_knowledge as ck
from services.clinical_knowledge import rerank_results, search_guidelines


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
def seeded_kb():
    """Build the seeded knowledge base once, then drop it so other test modules
    (e.g. test_corpus_loader) see a clean shared store."""
    _drop_store()
    ck.init_knowledge_base()
    yield
    _drop_store()


# ─────────────────────────────────────────────────────────────────────────────
# Unit: the lexical reranker
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _force_lexical_path(monkeypatch):
    """Pin the reranker to the lexical path for these unit tests.

    On a full install (FlagEmbedding + the ~2 GB BGE reranker weights cached)
    `_get_cross_encoder()` returns a real cross-encoder and the ordering
    assertions below — written for the deterministic lexical scorer — would
    exercise model behaviour instead. monkeypatch restores the cached state
    afterwards, so other modules still see whatever the environment provides.
    """
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", True)


def test_cross_encoder_skipped_on_fast_boot(monkeypatch):
    # ORB_SKIP_KNOWLEDGE_BASE must gate the heavy cross-encoder load off — the
    # documented fast-boot/constrained-hardware path — regardless of whether
    # the model happens to be installed on this machine.
    monkeypatch.setenv("ORB_SKIP_KNOWLEDGE_BASE", "1")
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", False)
    assert ck._get_cross_encoder() is None


def test_rerank_empty_is_safe():
    assert rerank_results("anything", []) == []


def test_rerank_prefers_high_overlap_candidate():
    candidates = [
        {"document_id": "A", "title": "Headache", "trust_tier": 3, "fusion_score": 0.03,
         "content": "aspirin dosing for tension headache in adults"},
        {"document_id": "B", "title": "DKA protocol", "trust_tier": 3, "fusion_score": 0.02,
         "content": "diabetic ketoacidosis insulin infusion potassium replacement bicarbonate criteria"},
        {"document_id": "C", "title": "Weather", "trust_tier": 3, "fusion_score": 0.01,
         "content": "an unrelated passage about seasonal weather patterns"},
    ]
    ranked = rerank_results("diabetic ketoacidosis insulin potassium management", candidates)
    assert ranked[0]["document_id"] == "B"
    assert all("rerank_score" in c for c in ranked)
    assert ranked[0]["rerank_method"] == "lexical"


def test_rerank_trust_tier_breaks_ties():
    # Two candidates with identical lexical content; the higher-trust (tier 1) wins.
    candidates = [
        {"document_id": "T3", "title": "Sepsis", "trust_tier": 3, "fusion_score": 0.02,
         "content": "sepsis broad spectrum antibiotics lactate crystalloid vasopressors"},
        {"document_id": "T1", "title": "Sepsis", "trust_tier": 1, "fusion_score": 0.02,
         "content": "sepsis broad spectrum antibiotics lactate crystalloid vasopressors"},
    ]
    ranked = rerank_results("sepsis antibiotics lactate crystalloid vasopressors", candidates)
    assert ranked[0]["document_id"] == "T1"


def test_rerank_top_k_truncates():
    candidates = [
        {"document_id": f"D{i}", "title": "t", "trust_tier": 3, "fusion_score": 0.01,
         "content": f"passage number {i} mentioning pneumonia" if i == 2 else "unrelated filler text"}
        for i in range(5)
    ]
    ranked = rerank_results("pneumonia", candidates, top_k=2)
    assert len(ranked) == 2
    assert ranked[0]["document_id"] == "D2"


def test_rerank_never_raises_on_missing_fields():
    # Candidates lacking content/title/trust_tier must not crash the reranker.
    candidates = [{"document_id": "X"}, {"document_id": "Y", "content": "sepsis"}]
    ranked = rerank_results("sepsis", candidates)
    assert {c["document_id"] for c in ranked} == {"X", "Y"}


# ─────────────────────────────────────────────────────────────────────────────
# Integration: search still works with reranking on
# ─────────────────────────────────────────────────────────────────────────────
def test_search_returns_results_with_rerank(seeded_kb):
    results = search_guidelines("sepsis qSOFA lactate screening", limit=5)
    assert len(results) > 0
    # Reranking annotates every returned result.
    assert all("rerank_score" in r for r in results)
    assert all("document_id" in r for r in results)


def test_search_finds_expected_seed_doc(seeded_kb):
    results = search_guidelines("DKA insulin infusion potassium replacement bicarbonate", limit=5)
    ids = [r["document_id"] for r in results]
    assert "EXT-METAB-001" in ids


def test_search_rerank_disabled_still_returns(seeded_kb):
    results = search_guidelines("STEMI door to balloon primary PCI", limit=5, rerank=False)
    assert len(results) > 0


def test_approved_only_filters_draft_loader_docs(seeded_kb):
    """A draft loader doc is excluded under approved_only; seed docs are retained."""
    from services import corpus_loader

    with db_session() as db:
        corpus_loader._ensure_bookkeeping(db)
        # Register a fake loader doc as DRAFT, pointing at an existing seed chunk's
        # document_id space is avoided — use a brand-new id and a real chunk row.
        db.execute(
            "INSERT INTO clinical_knowledge (document_id, chunk_index, category, title, content, "
            "source, source_type, source_origin, trust_tier) "
            "VALUES ('DRAFT-DOC-001', 0, 'Sepsis', 'Draft Sepsis Note', "
            "'draft sepsis qSOFA lactate screening content for testing', 'unit-test', "
            "'reference', 'external', 3)",
        )
        rid = db.execute("SELECT id FROM clinical_knowledge WHERE document_id='DRAFT-DOC-001'").fetchone()[0]
        # Index it in FTS so it is retrievable lexically.
        db.execute(
            "INSERT INTO clinical_knowledge_fts (rowid, title, content, source, category, specialty) "
            "VALUES (?, 'Draft Sepsis Note', 'draft sepsis qSOFA lactate screening content for testing', "
            "'unit-test', 'Sepsis', NULL)",
            (rid,),
        )
        db.execute(
            "INSERT OR REPLACE INTO corpus_documents "
            "(document_id, title, trust_tier, chunk_count, approval_status) "
            "VALUES ('DRAFT-DOC-001', 'Draft Sepsis Note', 3, 1, 'draft')",
        )
        db.commit()

    q = "sepsis qSOFA lactate screening"
    all_ids = [r["document_id"] for r in search_guidelines(q, limit=15, approved_only=False)]
    approved_ids = [r["document_id"] for r in search_guidelines(q, limit=15, approved_only=True)]

    assert "DRAFT-DOC-001" in all_ids, "draft doc should be retrievable when not filtering"
    assert "DRAFT-DOC-001" not in approved_ids, "draft doc must be excluded under approved_only"

    # Cleanup the injected rows so the store stays clean for teardown.
    with db_session() as db:
        db.execute("DELETE FROM clinical_knowledge WHERE document_id='DRAFT-DOC-001'")
        db.execute("DELETE FROM corpus_documents WHERE document_id='DRAFT-DOC-001'")
        db.commit()
