"""Tests for the guideline corpus loader (services.corpus_loader).

These exercise the loader end-to-end against the isolated session test DB set up by
tests/conftest.py (ORB_DB_PATH -> throwaway temp file). They assert that:
  * loading the sample corpus creates chunks in the shared clinical_knowledge store,
  * corpus_stats() reports correct counts (documents / chunks / embedded vs lexical),
  * re-loading is idempotent (no duplicate-chunk explosion),
  * a loaded document is retrievable via clinical_knowledge.search_guidelines
    (falling back to the guaranteed FTS lexical path when the embedder is absent),
  * .txt files and JSON-sidecar metadata are handled.

The real BGE-M3 embedder is not installed in the dev venv, so the loader runs in
"lexical" mode here; the embedded-mode assertions are guarded on embedder availability.
"""
import os

import pytest

from database import db_session
from services import corpus_loader
from services.clinical_knowledge import search_guidelines

# Sample corpus lives at server/corpus/ (sibling of tests/).
CORPUS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "corpus")

# document_ids declared by the sample files (front-matter / sidecar).
SAMPLE_DOC_IDS = {
    "REF-SEPSIS-NICE-001",
    "REF-AKI-KDIGO-001",
    "REF-CAP-CURB65-001",
    "SOP-HH-001",
}


@pytest.fixture(scope="module")
def loaded():
    """Load the sample corpus once for the module and return the load result."""
    return corpus_loader.load_directory(CORPUS_DIR, default_trust_tier=3)


def _chunks_for(document_id):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) FROM clinical_knowledge WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]


def test_sample_corpus_directory_exists():
    assert os.path.isdir(CORPUS_DIR), f"expected sample corpus at {CORPUS_DIR}"


def test_load_creates_chunks(loaded):
    assert loaded["files_found"] >= 4
    assert loaded["files_loaded"] == loaded["files_found"]
    assert loaded["chunks_loaded"] >= loaded["files_loaded"]
    # Every sample document produced at least one chunk in the shared store.
    for doc_id in SAMPLE_DOC_IDS:
        assert _chunks_for(doc_id) >= 1, f"no chunks for {doc_id}"


def test_multi_section_document_is_chunked(loaded):
    # The sepsis reference has several ~900-char sections and must split into >1 chunk.
    assert _chunks_for("REF-SEPSIS-NICE-001") >= 2


def test_sidecar_metadata_applied(loaded):
    # hand-hygiene-protocol.md carries no front-matter; its metadata comes from the
    # JSON sidecar (document_id SOP-HH-001, trust_tier 1).
    with db_session() as db:
        row = db.execute(
            "SELECT title, trust_tier, source_type, source_origin, version "
            "FROM clinical_knowledge WHERE document_id = 'SOP-HH-001' LIMIT 1"
        ).fetchone()
    assert row is not None
    title, trust_tier, source_type, source_origin, version = row
    assert trust_tier == 1
    assert source_type == "hospital_sop"
    assert source_origin == "demo_local"
    assert version == "v3.0"
    assert "Hand Hygiene" in title


def test_corpus_stats_counts(loaded):
    stats = corpus_loader.corpus_stats()
    # Only the loader has written to clinical_knowledge in this test DB.
    assert stats["documents"] == len(SAMPLE_DOC_IDS)
    assert stats["loader_documents"] == len(SAMPLE_DOC_IDS)
    assert stats["chunks"] == loaded["chunks_loaded"]
    assert stats["embedded_chunks"] + stats["lexical_only_chunks"] == stats["chunks"]

    if corpus_loader._real_embedder_available():
        assert stats["mode"] == "embedded"
        assert stats["embedded_chunks"] == stats["chunks"]
    else:
        # No BGE-M3 -> lexical-only: chunks indexed in FTS, no vectors stored.
        assert stats["mode"] == "lexical"
        assert stats["embedded_chunks"] == 0
        assert stats["lexical_only_chunks"] == stats["chunks"]


def test_reload_is_idempotent(loaded):
    before = corpus_loader.corpus_stats()
    per_doc_before = {d: _chunks_for(d) for d in SAMPLE_DOC_IDS}

    # Re-load the exact same corpus (same source + version).
    corpus_loader.load_directory(CORPUS_DIR, default_trust_tier=3)

    after = corpus_loader.corpus_stats()
    per_doc_after = {d: _chunks_for(d) for d in SAMPLE_DOC_IDS}

    assert after["documents"] == before["documents"], "document count changed on reload"
    assert after["chunks"] == before["chunks"], "chunk count changed on reload (duplication!)"
    assert per_doc_after == per_doc_before, "per-document chunk counts changed on reload"


def test_loaded_doc_is_retrievable(loaded):
    """A loaded guideline is retrievable through the same path Sage uses.

    Prefer clinical_knowledge.search_guidelines (the public retrieval API). Guard
    the embedder-dependent case: if the API returns nothing because embeddings are
    unavailable, assert the guaranteed FTS lexical path instead.
    """
    query = "community acquired pneumonia CURB-65 severity assessment"
    target = "REF-CAP-CURB65-001"

    results = search_guidelines(query, limit=8)
    found = any(r.get("document_id") == target for r in results)

    if not found:
        # Fall back to the guaranteed lexical path (must exist regardless of embedder).
        assert not corpus_loader._real_embedder_available(), (
            "search_guidelines failed to retrieve the doc even with a real embedder"
        )
        with db_session() as db:
            rows = db.execute(
                "SELECT ck.document_id "
                "FROM clinical_knowledge_fts f "
                "JOIN clinical_knowledge ck ON ck.id = f.rowid "
                "WHERE clinical_knowledge_fts MATCH ?",
                ("pneumonia OR CURB OR severity",),
            ).fetchall()
        found = any(r[0] == target for r in rows)

    assert found, "loaded CAP guideline was not retrievable"


def test_txt_and_default_tier(tmp_path):
    """.txt files are supported, and files without metadata inherit the default tier."""
    p = tmp_path / "oxygen-targets.txt"
    p.write_text(
        "Target oxygen saturations for most acutely ill adults are 94 to 98 percent. "
        "For patients at risk of hypercapnic respiratory failure, such as COPD, aim "
        "for 88 to 92 percent pending blood gas results.\n",
        encoding="utf-8",
    )
    result = corpus_loader.load_directory(str(tmp_path), default_trust_tier=2)
    assert result["files_found"] == 1
    assert result["files_loaded"] == 1
    assert result["chunks_loaded"] >= 1

    # document_id defaults to a slug of the filename; tier defaults to the argument.
    with db_session() as db:
        row = db.execute(
            "SELECT trust_tier, source_type FROM clinical_knowledge "
            "WHERE document_id = 'OXYGEN-TARGETS' LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == 2
    assert row[1] == "national_guideline"

    # Clean up so the shared-store counts in other tests stay predictable.
    with db_session() as db:
        corpus_loader._delete_document(db, "OXYGEN-TARGETS", fts=True, vec=True)
