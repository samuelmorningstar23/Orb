"""Source-backed summary verification, not keyword matching or clinical validation."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from capacity_answer_quality import (
    _grounding_module,
    apply_workflow_checks,
    patient_summary_checks,
)


def example(*, positive=False):
    module = _grounding_module()
    source = [
        {"domain": "identity", "text": "Recorded patient identity: name: XqzAA VyxA; age: 30; sex: M",
         "source": {"table": "patients", "id": "CAP-SYNTH-000", "field": "name,age,sex"}},
        {"domain": "diagnosis", "text": "Recorded diagnosis: Synthetic evaluation record",
         "source": {"table": "patients", "id": "CAP-SYNTH-000", "field": "diagnosis"}},
    ]
    if positive:
        source.extend([
            {"domain": "observations", "text": "heart_rate: 83; systolic_bp: 121; spo2: 96",
             "source": {"table": "vitals", "id": 1, "field": "record", "recorded_at": "2026-10-23T12:00:00Z"}},
            {"domain": "current_medications", "text": "Synthetic Test Tablet 5 mg Oral OD; ON HOLD pending review. Administration is not confirmed.",
             "source": {"table": "medications", "id": 2, "field": "record"}},
            {"domain": "notes", "text": "No chest pain reported. Imaging discussed; scan completion is not documented.",
             "source": {"table": "scribe_notes", "id": 3, "field": "structured_note"}},
            {"domain": "labs", "text": "[SIMULATED RESULT, not a real lab or image] Sodium 129 mmol/L.",
             "source": {"table": "clinical_documents", "id": 4, "field": "content"}},
        ])
    bundle = module.compile_sage_context({"patient_id": "CAP-SYNTH-000", "grounding_facts": source})
    ids = tuple(f.fact_id for f in bundle.facts)
    return module.render_sage(bundle, ids), {"grounding_bundle": module.bundle_to_dict(bundle)}, module.grounding_metadata(bundle, ids)


@pytest.mark.parametrize("positive", [False, True])
def test_only_exact_independently_expected_source_rendering_passes(positive):
    answer, expected, proof = example(positive=positive)
    result = patient_summary_checks(answer, expected, proof, patient_id="CAP-SYNTH-000")
    assert result["passed"] and all(result["checks"].values())
    assert proof["clinical_validation"] is False


def test_model_can_reorder_facts_but_cannot_rewrite_them():
    module = _grounding_module()
    _, expected, _ = example(positive=True)
    bundle = module.bundle_from_dict(expected["grounding_bundle"])
    ids = tuple(f.fact_id for f in reversed(bundle.facts))
    answer = module.render_sage(bundle, ids)
    assert patient_summary_checks(answer, expected, module.grounding_metadata(bundle, ids))["passed"]


def test_only_exact_independently_captured_patient_label_is_allowed():
    answer, expected, proof = example()
    expected["answer_label"] = "About XqzAA VyxA, bed 1:"
    assert patient_summary_checks(expected["answer_label"] + "\n\n" + answer, expected, proof)["passed"]
    assert not patient_summary_checks("About XqzAB VyxB, bed 2:\n\n" + answer, expected, proof)["passed"]
    assert not patient_summary_checks("The patient is stable.\n\n" + answer, expected, proof)["passed"]


@pytest.mark.parametrize("claim", [
    "No active medications are listed, so there is no change history to report.",
    "No recent Scribe notes indicate any ongoing tasks have been formally recorded.",
    "No recent imaging has been performed.",
    "The patient is clinically stable.",
    "No outstanding tasks remain.",
    "No changes to medications are needed.",
    "Creatinine is normal.",
    "XqzAB VyxB has the same findings.",
    "ACT|Start metoprolol now.",
    "There is no evidence of deterioration.",
])
def test_even_plausible_extra_prose_with_valid_proof_is_rejected(claim):
    answer, expected, proof = example()
    result = patient_summary_checks(answer + "\n" + claim, expected, proof)
    assert not result["passed"] and not result["checks"]["exact_source_rendering"]


@pytest.mark.parametrize("answer", [
    "Patient XqzAA VyxA: Synthetic evaluation record. Observations: no recent vitals and no recent labs. "
    "No signed Scribe notes or imaging documented. Medication changes: No active medications are listed, "
    "so there is no change history to report. Unresolved tasks: no recent Scribe notes indicate any ongoing tasks have been formally recorded.",
    "Patient XqzAA VyxA: Synthetic evaluation record. Observations: no recent vitals and no recent labs. "
    "No signed Scribe notes exist. Medication changes: No active medications are listed, so any change history is unknown. "
    "Unresolved tasks: No recent imaging has been performed. There is no information about outstanding tasks.",
    "XqzAA VyxA: Synthetic evaluation record. No recent vital observations are documented. "
    "No active medications or medication change history are documented. No recent laboratory results are available. "
    "Outstanding tasks are not documented; this does not establish that no tasks remain.",
])
def test_old_lexical_false_passes_cannot_pass_a_forged_good_proof(answer):
    _, expected, proof = example()
    assert not patient_summary_checks(answer, expected, proof)["passed"]


@pytest.mark.parametrize("old,new", [
    ("heart_rate: 83", "heart_rate: 38"),
    ("ON HOLD", "administered"),
    ("No chest pain reported.", "Chest pain reported."),
    ("129 mmol/L", "139 mmol/L"),
    ("[SIMULATED RESULT, not a real lab or image]", ""),
    ("whether imaging was performed is unknown", "imaging was not performed"),
    ("medication-change history is unknown", "medication-change history is normal"),
])
def test_positive_record_values_negations_states_and_unknowns_cannot_change(old, new):
    answer, expected, proof = example(positive=True)
    # The renderer escapes Markdown source quotes. Locate its exact rendering.
    escaped_old = _grounding_module()._quote(old).removeprefix("> ")
    assert escaped_old in answer
    result = patient_summary_checks(answer.replace(escaped_old, new), expected, proof)
    assert not result["passed"]


@pytest.mark.parametrize("change", [
    lambda p: p.update(source_fingerprint="0" * 64),
    lambda p: p.update(contract="future-unverified-contract"),
    lambda p: p.update(extractive_only=False),
    lambda p: p.update(extractive_only=1),
    lambda p: p.update(clinical_validation=True),
    lambda p: p.update(clinical_validation=0),
    lambda p: p.update(extra="unapproved field"),
    lambda p: p["selected_fact_ids"].pop(),
    lambda p: p["selected_fact_ids"].append(p["selected_fact_ids"][0]),
    lambda p: p["selected_fact_ids"].__setitem__(0, "FOREIGN"),
    lambda p: p["source_references"][0].update(id="OTHER-PATIENT"),
    lambda p: p["source_references"].clear(),
])
def test_malformed_stale_foreign_or_self_qualified_proof_fails_closed(change):
    answer, expected, proof = example()
    change(proof)
    assert not patient_summary_checks(answer, expected, proof)["passed"]


def test_model_metadata_is_not_a_substitute_for_independent_expected_evidence():
    answer, _, proof = example()
    result = patient_summary_checks(answer, {"name": "XqzAA VyxA", "diagnosis": "Synthetic evaluation record"}, proof)
    assert not result["passed"] and not result["checks"]["trusted_source_evidence_present"]


@pytest.mark.parametrize("proof_count", [0, 2, True, "1"])
def test_one_unambiguous_grounding_proof_is_required(proof_count):
    answer, expected, proof = example()
    assert not patient_summary_checks(answer, expected, proof, proof_count=proof_count)["passed"]


def test_expected_source_edits_invalidate_a_previously_good_answer_and_proof():
    answer, expected, proof = example()
    changed = copy.deepcopy(expected)
    changed["grounding_bundle"]["facts"][1]["text"] = "A different documented diagnosis"
    result = patient_summary_checks(answer, changed, proof)
    assert not result["passed"] and not result["checks"]["source_fingerprint_matches"]


def test_exact_answer_for_wrong_authorized_patient_cannot_pass():
    answer, expected, proof = example()
    assert not patient_summary_checks(answer, expected, proof, patient_id="CAP-SYNTH-099")["passed"]


def row_and_plan():
    answer, expected, proof = example()
    row = {"request_id": "a", "lane": "patient", "success": True, "error": None,
           "full_s": 2.0, "synthetic_answer": answer, "summary_grounding": proof,
           "summary_grounding_proof_count": 1, "completed_within_target": True,
           "first_readable_within_target": True}
    return row, [{"request_id": "a", "patient_id": "CAP-SYNTH-000"}], {"CAP-SYNTH-000": expected}


def test_semantic_failure_preserves_transport_timing_and_changes_success():
    row, plan, expectations = row_and_plan()
    row["synthetic_answer"] += " The patient is stable."
    result = apply_workflow_checks([row], plan, expectations)
    assert row["transport_success"] is True
    assert row["success"] is False and row["error"] == "summary_quality_failed"
    assert row["full_s"] == 2.0 and not row["completed_within_target"]
    assert result["task_checks_passed"] == 0


@pytest.mark.parametrize("error", ["timeout", "stream_error", "capacity_rejected", "missing_terminal_event"])
def test_exact_answer_and_proof_cannot_pass_after_transport_failure(error):
    row, plan, expectations = row_and_plan()
    row.update(success=False, error=error)
    result = apply_workflow_checks([row], plan, expectations)
    assert result["transport_completed"] == result["task_checks_passed"] == 0
    assert row["error"] == error


def test_valid_grounding_still_never_claims_clinical_qualification():
    row, plan, expectations = row_and_plan()
    result = apply_workflow_checks([row], plan, expectations)
    assert result["all_patient_requests_passed"] and result["task_checks_passed"] == 1
    assert result["clinical_accuracy_qualified"] is False


def test_missing_fixture_source_proof_is_refused():
    row, plan, _ = row_and_plan()
    with pytest.raises(ValueError, match="expectations"):
        apply_workflow_checks([row], plan, {})


def test_synthetic_capture_observes_content_and_separate_grounding_metadata(monkeypatch):
    import loadtest_capacity as capacity
    from qualify_ollama_serving import install_synthetic_answer_capture

    monkeypatch.setattr(capacity, "StreamObservation", capacity.StreamObservation)
    install_synthetic_answer_capture()
    observation = capacity.StreamObservation()
    answer, _, proof = example()
    observation.accept("message", json.dumps({"content": answer}), .1)
    observation.accept("message", json.dumps({"metadata": {"summary_grounding": proof}}), .2)
    observation.accept("message", '{"done":true}', .3)
    report = observation.report()
    assert report["synthetic_answer"] == answer and report["summary_grounding"] == proof
    assert report["summary_grounding_proof_count"] == 1


def test_ambiguous_multiple_grounding_proofs_are_not_silently_overwritten(monkeypatch):
    import loadtest_capacity as capacity
    from qualify_ollama_serving import install_synthetic_answer_capture

    monkeypatch.setattr(capacity, "StreamObservation", capacity.StreamObservation)
    install_synthetic_answer_capture()
    observation = capacity.StreamObservation()
    _, _, proof = example()
    for _ in range(2):
        observation.accept("message", json.dumps({"metadata": {"summary_grounding": proof}}), .1)
    assert observation.report()["summary_grounding"] is None
    assert observation.report()["summary_grounding_proof_count"] == 2
