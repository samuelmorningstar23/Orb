"""Phase 1 clinical-safety: structured allergies/problems + order-time drug
safety screening (allergy + interaction), and the agentic auto-order block.
"""
import uuid

import pytest

from database import db_session
from services.clinical_records_service import (
    add_allergy, list_allergies, deactivate_allergy,
    add_problem, list_problems, resolve_problem,
)
from services.drug_safety_service import (
    classify_drug, check_medication_safety, check_allergies, check_interactions,
)


@pytest.fixture()
def patient():
    """TEST-ISOLATION LAW: patients.status DEFAULTS to 'admitted', so every test
    in this module used to leave one more admitted patient in the shared session
    DB — ~11 by the end of the module. That is not a cosmetic leak. The FHIR
    self-export in services/downtime_service.py iterates
    `patients WHERE status='admitted'` and sleeps _PATIENT_SLEEP_S (250 ms)
    BETWEEN PATIENTS, so each leftover adds ~0.275 s to every export call in
    tests/test_downtime_fhir_export.py and to everything downstream of it.
    Discharge on teardown; do not DELETE, because the allergy, problem and
    medication rows written during the test legitimately reference this id."""
    pid = f"PATSAFE{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Safety Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _add_active_med(pid, drug):
    with db_session() as db:
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status) "
            "VALUES (?,?,?,?,?, 'active')",
            (pid, drug, "1", "Oral", "OD"),
        )


# ── classification ───────────────────────────────────────────────────────────
def test_classify_drug():
    assert "penicillins" in classify_drug("Amoxicillin 500mg")
    assert "cephalosporins" in classify_drug("cefuroxime")
    assert "nsaids" in classify_drug("Ibuprofen")
    assert classify_drug("some-random-herbal") == []


# ── allergy checking ─────────────────────────────────────────────────────────
def test_direct_allergy_blocks(patient):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    result = check_medication_safety(patient, "Amoxicillin 500mg")
    assert result["blocking"] is True
    assert result["max_severity"] == "severe"
    assert any(a["match"] == "direct" for a in result["allergy_alerts"])


def test_class_allergy_matches_same_class(patient):
    # Allergic to penicillin → flucloxacillin (same class) alerts.
    add_allergy(patient, "penicillin", severity="severe")
    result = check_medication_safety(patient, "flucloxacillin")
    assert result["allergy_alerts"]
    assert result["blocking"] is True


def test_penicillin_cephalosporin_cross_reactivity(patient):
    add_allergy(patient, "penicillin", severity="moderate")
    result = check_medication_safety(patient, "cefuroxime")
    assert any(a["match"] == "cross_reactivity" for a in result["allergy_alerts"])
    # cross-reactivity is moderate, not blocking
    assert result["max_severity"] == "moderate"
    assert result["blocking"] is False


def test_unrelated_drug_is_safe(patient):
    add_allergy(patient, "penicillin", severity="severe")
    result = check_medication_safety(patient, "paracetamol")
    assert result["safe"] is True
    assert result["blocking"] is False


def test_cross_reactivity_is_bidirectional(patient):
    # Cephalosporin-allergic patient prescribed a penicillin must alert (the
    # reverse direction, previously missed).
    add_allergy(patient, "cefalexin", severity="moderate")
    result = check_medication_safety(patient, "amoxicillin")
    assert any(a["match"] == "cross_reactivity" for a in result["allergy_alerts"])


def test_combination_product_matches_ingredient_allergy(patient):
    # A patient allergic to codeine, prescribed co-codamol (codeine+paracetamol),
    # must alert even though the product name lacks the string "codeine".
    add_allergy(patient, "codeine", severity="severe")
    result = check_medication_safety(patient, "co-codamol")
    assert result["allergy_alerts"]
    assert result["blocking"] is True


# ── interaction checking ─────────────────────────────────────────────────────
def test_warfarin_nsaid_interaction(patient):
    _add_active_med(patient, "warfarin")
    result = check_medication_safety(patient, "ibuprofen")
    assert any(i["type"] == "interaction" and i["severity"] == "severe"
               for i in result["interaction_alerts"])
    assert result["max_severity"] == "severe"


# ── clinical records CRUD ────────────────────────────────────────────────────
def test_allergy_upsert_and_deactivate(patient):
    a1 = add_allergy(patient, "Penicillin", severity="mild")
    a2 = add_allergy(patient, "penicillin", severity="severe")  # same substance, upsert
    assert a1["id"] == a2["id"]
    assert a2["severity"] == "severe"
    assert len(list_allergies(patient)) == 1
    assert deactivate_allergy(a2["id"], patient) is True
    assert list_allergies(patient) == []
    assert len(list_allergies(patient, include_inactive=True)) == 1


def test_problem_list_lifecycle(patient):
    p = add_problem(patient, "Community-acquired pneumonia", icd10_code="J18.9")
    assert len(list_problems(patient)) == 1
    assert resolve_problem(p["id"], patient) is True
    assert list_problems(patient) == []
    assert len(list_problems(patient, include_resolved=True)) == 1


# ── agentic auto-order safety block ──────────────────────────────────────────
def test_agentic_dispatch_blocked_on_severe_allergy(patient):
    from services.sage_handoff_service import HandoffBlocked, _dispatch_to_helix
    add_allergy(patient, "amoxicillin", severity="severe")
    with pytest.raises(HandoffBlocked) as blocked:
        _dispatch_to_helix(
            {"patient_id": patient, "target": "amoxicillin", "rationale": "start abx"},
            actor_id="D001",
        )
    assert blocked.value.safety["blocking"] is True
    assert blocked.value.safety["screen_failed"] is False
    # nothing charted
    with db_session() as db:
        n = db.execute(
            "SELECT count(*) FROM medications WHERE patient_id = ? AND drug_name = 'amoxicillin'",
            (patient,),
        ).fetchone()[0]
    assert n == 0


def test_agentic_dispatch_charts_with_llm_provenance(patient):
    from services.sage_handoff_service import _dispatch_to_helix
    _dispatch_to_helix(
        {"patient_id": patient, "target": "paracetamol", "rationale": "give iv paracetamol"},
        actor_id="D001",
    )
    with db_session() as db:
        row = db.execute(
            "SELECT dosage, route, source FROM medications WHERE patient_id = ? AND drug_name = 'paracetamol'",
            (patient,),
        ).fetchone()
    assert row is not None
    assert row["source"] == "llm_extracted"
    assert row["dosage"] == "REVIEW REQUIRED"   # no fabricated dose
    assert row["route"] == "IV"                 # route heuristic still applied
