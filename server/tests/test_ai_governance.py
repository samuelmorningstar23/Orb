"""AI governance tests: the PHI classifier and the per-inference audit records
on the tamper-evident hash chain.

The contract under test (docs/SAGE_AMBIENT_INTELLIGENCE.md):
  1. Any patient-scoped / census / discharge-board / forecast query classifies
     as PHI; only general medical-knowledge questions are PHI-free.
  2. Every inference writes an audit row that participates in the hash chain,
     and the chain stays verifiable afterwards.
"""
import json

import pytest

from database import db_session
from services.ai_governance import (
    QueryClassification,
    classify_query_sensitivity,
    record_inference,
)


# ─── PHI classifier ───

@pytest.fixture()
def census_patient():
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO patients (id, mrn, name, age, sex, bed, ward, diagnosis, status)
               VALUES ('PAT-GOV-1', 'MRN-GOV-1', 'Kavya Ramachandran', 54, 'F', 'B-12', 'ICU', 'Sepsis', 'admitted')"""
        )
    yield
    with db_session() as db:
        db.execute("DELETE FROM patients WHERE id = 'PAT-GOV-1'")


def test_patient_scoped_by_attached_id_is_phi():
    c = classify_query_sensitivity("what are the latest labs?", patient_id="P001")
    assert c.contains_phi is True


def test_view_context_patient_is_phi():
    c = classify_query_sensitivity(
        "summarize what I'm looking at",
        view_context={"tab": "vigil", "patient_id": "P002"},
    )
    assert c.contains_phi is True


def test_census_queries_are_phi():
    # The broad-scope context builder returns one row PER PATIENT (name, bed,
    # dx, vitals, risk) — bed+ward+dx+status per patient is PHI even when the
    # question sounds statistical.
    for q in [
        "show me all patients in the hospital",
        "hospital census please",
        "who is on the discharge board today",
        "which patients are deteriorating",
        "give me the shift handoff summary",
    ]:
        c = classify_query_sensitivity(q)
        assert c.contains_phi is True, q


def test_pronoun_reference_is_phi():
    c = classify_query_sensitivity("what should I do about his potassium?")
    assert c.contains_phi is True


def test_patient_name_match_is_phi(census_patient):
    c = classify_query_sensitivity("how is kavya doing this morning")
    assert c.contains_phi is True


def test_bed_reference_is_phi():
    c = classify_query_sensitivity("what's going on with bed 12")
    assert c.contains_phi is True


def test_general_medical_knowledge_is_phi_free():
    for q in [
        "what are the side effects of metoprolol",
        "explain the mechanism of action of vancomycin",
        "difference between STEMI and NSTEMI treatment",
    ]:
        c = classify_query_sensitivity(q)
        assert c.contains_phi is False, q
        assert c.query_class == "general_knowledge"


# ─── Per-inference audit records on the hash chain ───

PHI_FREE = QueryClassification(contains_phi=False, query_class="general_knowledge")
PHI = QueryClassification(contains_phi=True, query_class="patient_scoped")


def _last_audit_rows(n=5):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()]


def test_record_inference_writes_hash_chained_row():
    corr = record_inference(
        provider="ollama", model="local-model",
        purpose="sage_chat", outcome="served",
        classification=PHI, user_id="D001", patient_id="P001",
        timing_ms={"ttft": 120, "total": 900},
    )
    row = _last_audit_rows(1)[0]
    assert row["event_category"] == "ai_inference"
    assert row["action_type"] == "model_inference"
    assert row["outcome"] == "success"
    assert row["correlation_id"] == corr
    assert row["row_hash"]  # participates in the tamper-evidence chain
    meta = json.loads(row["metadata_json"])
    assert meta["provider"] == "ollama"
    assert meta["model"] == "local-model"
    assert meta["lane"] == "local"
    assert meta["phi"] is True


def test_audit_chain_stays_verifiable_after_inference_records():
    from database import verify_audit_chain
    record_inference(
        provider="ollama", model="local-model",
        purpose="sage_chat", outcome="served", classification=PHI_FREE,
    )
    result = verify_audit_chain()
    assert result["ok"] is True
    assert result["broken_row_ids"] == []
