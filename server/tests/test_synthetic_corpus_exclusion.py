"""Invented local policy is never retrievable at a real site (F039).

The seed corpus carries synthetic "Orb General Hospital" SOPs and a fictional
antibiogram (source_origin 'demo_local', is_synthetic), tier 1 and 2: the tiers
the prompt tells the model outrank external guidance. At a real hospital Sage
could cite "Ceftriaxone is NO LONGER recommended ... at this institution" as
that hospital's own policy. Both retrieval lanes now drop them unless the
appliance runs the dev profile: the live lane (search_guidelines) and the warm
lane (a prefetched pool, which may predate the rule).

Embeddings are zero vectors and the query is lexical-only, so this runs in
seconds and does not depend on which embedder is installed.
"""
import pytest

import config
from database import db_session
from services import clinical_knowledge as ck
from services import sage_service
from tests.knowledge_fixtures import lexical_knowledge_models

SEPSIS_ANTIBIOTICS = ("empiric antibiotic sepsis ESBL piperacillin tazobactam "
                      "meropenem ceftriaxone institution")
SYNTHETIC_IDS = {d["document_id"] for d in ck.CLINICAL_DOCUMENTS
                 if d.get("is_synthetic") or d.get("source_origin") == "demo_local"}

_SHARED_TABLES = ("clinical_knowledge_vec", "clinical_knowledge_fts", "clinical_knowledge",
                  "clinical_knowledge_meta", "corpus_documents")


def _drop_store():
    with db_session() as db:
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


@pytest.fixture(scope="module")
def corpus():
    # Explicit seeding works even when the launcher disables automatic startup
    # seeding; queries still exercise genuine FTS with dense retrieval unavailable.
    with lexical_knowledge_models() as seed_builtin:
        _drop_store()
        seed_builtin()
        yield
        _drop_store()


@pytest.fixture
def profile(monkeypatch):
    def _set(name):
        monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", name)
    return _set


def test_the_seed_corpus_really_has_synthetic_documents():
    """Premise: without these the tests below would pass vacuously."""
    assert {"SOP-INF-001", "SOP-CODE-001", "FORM-ID-001"} <= SYNTHETIC_IDS


def test_live_lane_drops_synthetic_documents_on_a_real_site(corpus, profile):
    profile("prod")
    results = ck.search_guidelines(SEPSIS_ANTIBIOTICS, limit=10, min_relevance=0.0)
    assert results, "premise: the query must retrieve something"
    assert not ({r["document_id"] for r in results} & SYNTHETIC_IDS)
    assert not any(r["is_synthetic"] for r in results)


def test_live_lane_keeps_them_on_a_dev_appliance(corpus, profile):
    profile("dev")
    ids = {r["document_id"] for r in
           ck.search_guidelines(SEPSIS_ANTIBIOTICS, limit=10, min_relevance=0.0)}
    assert "FORM-ID-001" in ids, "the dev profile is the explicit opt-in"


def _pool():
    by_id = {d["document_id"]: d for d in ck.CLINICAL_DOCUMENTS}
    out = []
    for doc_id in ("FORM-ID-001", "EXT-SEPSIS-001"):
        d = dict(by_id[doc_id])
        d["fusion_score"] = 0.03
        out.append(d)
    return out


def test_warm_lane_drops_a_synthetic_document_from_a_cached_pool(profile, monkeypatch):
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", True)
    profile("dev")   # premise: the floor alone would keep it
    kept = sage_service._rerank_warm_pool(SEPSIS_ANTIBIOTICS, _pool(), limit=5) or []
    assert "FORM-ID-001" in {r["document_id"] for r in kept}
    profile("prod")
    results = sage_service._rerank_warm_pool(SEPSIS_ANTIBIOTICS, _pool(), limit=5) or []
    assert "FORM-ID-001" not in {r["document_id"] for r in results}


def test_warm_lane_of_only_synthetic_documents_falls_back_to_live(corpus, profile):
    profile("prod")
    only_synthetic = [d for d in _pool() if d["document_id"] == "FORM-ID-001"]
    assert sage_service._rerank_warm_pool(SEPSIS_ANTIBIOTICS, only_synthetic, limit=5) is None
    _text, sources, _conf = sage_service._get_guideline_context(
        SEPSIS_ANTIBIOTICS, warm_candidates=only_synthetic)
    assert not ({s["document_id"] for s in sources} & SYNTHETIC_IDS)
    assert "NO LONGER recommended" not in _text


def test_drop_synthetic_reads_the_profile_at_call_time(profile):
    docs = [{"document_id": "A", "is_synthetic": True},
            {"document_id": "B", "source_origin": "demo_local"},
            {"document_id": "C", "is_synthetic": False, "source_origin": "external"}]
    profile("prod")
    assert [d["document_id"] for d in ck.drop_synthetic(docs)] == ["C"]
    profile("dev")
    assert len(ck.drop_synthetic(docs)) == 3
