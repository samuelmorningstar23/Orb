"""Grounded summaries receive authorized rows, never inferred chart prose.

These use the real chart assembler and isolated migrated test database. The
model and GPU are not involved: provenance, state qualifiers and complete
source text must already be correct before a model can select any fact.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from database import db_session

from services import sage_service, vigil_service

DEPARTMENT = "Summary Evidence Medicine"
RECORDED_AT = "2026-10-20T10:00:00+00:00"


@pytest.fixture
def pid():
    patient_id = "SUMMARY-EVIDENCE-" + uuid.uuid4().hex
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, bed, ward, diagnosis, status, created_at) "
            "VALUES (?, 'Synthetic Evidence Patient', ?, 64, 'F', 'SE-1', 'SE', "
            "'Suspected pneumonia; not confirmed', 'admitted', ?)",
            (patient_id, "MRN-" + patient_id, RECORDED_AT),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, admitted_by, department, ward, bed, status, created_at) "
            "VALUES (?, 'D001', ?, 'SE', 'SE-1', 'active', ?)",
            (patient_id, DEPARTMENT, RECORDED_AT),
        )
    yield patient_id
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (patient_id,))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (patient_id,))


def _context(pid, **kwargs):
    with db_session() as db:
        return sage_service._assemble_patient_context(db, pid, DEPARTMENT, **kwargs)


def _facts(context, domain):
    return [fact for fact in context["grounding_facts"] if fact["domain"] == domain]


def _observation(pid, timestamp=RECORDED_AT, *, source="clinician", voided_at=None):
    with db_session() as db:
        cursor = db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp, "
            "spo2, temperature, respiratory_rate, consciousness, on_oxygen, source, voided_at) "
            "VALUES (?, ?, 96, 118, 74, 94, 38.1, 22, NULL, NULL, ?, ?)",
            (pid, timestamp, source, voided_at),
        )
        return cursor.lastrowid


def test_empty_chart_has_identity_and_documented_diagnosis_without_absence_claims(pid):
    context = _context(pid)
    assert {fact["domain"] for fact in context["grounding_facts"]} == {"identity", "diagnosis"}
    assert _facts(context, "diagnosis")[0]["text"] == "Recorded diagnosis: Suspected pneumonia; not confirmed"
    assert _facts(context, "diagnosis")[0]["source"] == {
        "table": "patients", "id": pid, "patient_id": pid,
        "field": "diagnosis", "recorded_at": RECORDED_AT,
    }
    assert context["grounding_scope"]["medication_changes"] == "Medication change history has not been retrieved"
    assert context["grounding_scope"]["unresolved_tasks"] == "Outstanding-task ledger has not been retrieved"
    assert not any("no imaging" in fact["text"].lower() for fact in context["grounding_facts"])


def test_out_of_department_and_facility_requests_receive_no_ledger(pid):
    with db_session() as db:
        assert sage_service._assemble_patient_context(db, pid, "Other Medicine") is None
    assert _context(pid, facilities=frozenset()) is None


def test_observation_values_keep_provenance_and_do_not_assess_null_fields(pid):
    observation_id = _observation(pid)
    fact = _facts(_context(pid), "observations")[0]
    assert "heart_rate: 96.0" in fact["text"]
    assert "temperature: 38.1" in fact["text"]
    assert "consciousness: unknown (not recorded)" in fact["text"]
    assert "on_oxygen: unknown (not recorded)" in fact["text"]
    assert "stable" not in fact["text"].lower()
    assert fact["source"]["id"] == observation_id
    assert fact["source"]["recorded_at"] == RECORDED_AT


def test_simulated_observations_remain_explicitly_simulated(pid):
    _observation(pid, source="simulated")
    fact = _facts(_context(pid), "observations")[0]
    assert fact["text"].startswith("[SIMULATED OBSERVATION, not a real measurement]")
    assert "source" in fact["source"]["field"]


def test_carried_forward_and_illegible_observation_caveats_are_preserved(pid):
    observation_id = _observation(pid)
    carried = '{"heart_rate":"2026-10-19T09:00:00+00:00"}'
    with db_session() as db:
        db.execute(
            "UPDATE vitals SET carried_from = ?, illegible = 'consciousness', entered_at = ? WHERE id = ?",
            (carried, RECORDED_AT, observation_id),
        )
    fact = _facts(_context(pid), "observations")[0]
    assert "carried_from: " + carried in fact["text"]
    assert "illegible: consciousness" in fact["text"]
    assert "consciousness: unknown (not recorded)" in fact["text"]
    assert "row timestamps do not replace carried-forward measurement times" in _context(pid)["grounding_scope"]["observations"]


def test_voided_and_previous_stay_observations_cannot_be_selected(pid, monkeypatch):
    earlier = _observation(pid)
    current_time = "2026-10-22T10:00:00+00:00"
    current = _observation(pid, current_time)
    voided = _observation(pid, "2026-10-22T11:00:00+00:00", voided_at=RECORDED_AT)
    monkeypatch.setattr(
        vigil_service, "current_stay_start_on",
        lambda db, patient_id: datetime(2026, 10, 21, tzinfo=timezone.utc),
    )
    facts = _facts(_context(pid), "observations")
    assert [fact["source"]["id"] for fact in facts] == [current]
    assert earlier != current != voided


def test_observation_source_scope_is_bounded_to_the_actual_six_rows(pid):
    base = datetime(2026, 10, 20, tzinfo=timezone.utc)
    ids = [_observation(pid, (base + timedelta(minutes=i)).isoformat()) for i in range(8)]
    facts = _facts(_context(pid), "observations")
    assert [fact["source"]["id"] for fact in facts] == list(reversed(ids[-6:]))
    assert "six" in _context(pid)["grounding_scope"]["observations"]


def test_held_and_unverified_medication_rows_are_not_asserted_as_administered(pid):
    with db_session() as db:
        med_id = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "source, started_at, held_at, hold_reason) "
            "VALUES (?, 'Synthetic Drug', '5 mg', 'Oral', 'daily', 'active', 'llm_extracted', ?, ?, 'Nil by mouth')",
            (pid, RECORDED_AT, RECORDED_AT),
        ).lastrowid
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status) "
            "VALUES (?, 'Discontinued Drug', '1 mg', 'Oral', 'daily', 'discontinued')", (pid,),
        )
    context = _context(pid)
    facts = _facts(context, "current_medications")
    assert len(facts) == 1
    assert facts[0]["source"]["id"] == med_id
    assert "on hold since " + RECORDED_AT in facts[0]["text"]
    assert "recorded hold reason: Nil by mouth" in facts[0]["text"]
    assert "clinician verification is not recorded" in facts[0]["text"]
    assert "administered" not in facts[0]["text"]
    assert not _facts(context, "medication_changes")
    assert not _facts(context, "unresolved_tasks")


def test_long_lab_source_keeps_final_qualification_and_simulated_label(pid):
    content = "Results pending. " * 30 + "Do not interpret the preliminary value: specimen was contaminated."
    with db_session() as db:
        lab_id = db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id, source, created_at) "
            "VALUES (?, 'lab_result', 'Synthetic BMP', ?, 'D001', 'simulated', ?)",
            (pid, content, RECORDED_AT),
        ).lastrowid
    fact = _facts(_context(pid), "labs")[0]
    assert fact["source"]["id"] == lab_id
    assert fact["text"].startswith(sage_service.SIMULATED_RESULT_PREFIX)
    assert fact["text"].endswith(content)


def test_signed_note_keeps_long_negation_and_override_without_importing_draft(pid):
    content = "Assessment recorded. " * 40 + "Pulmonary embolism has NOT been confirmed."
    with db_session() as db:
        note_id = db.execute(
            "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, structured_note, "
            "status, signed_at, created_at, unverified_ack_by, unverified_ack_at) "
            "VALUES (?, 'D001', 'Raw dictation is not the signed text', ?, 'signed', ?, ?, 'D001', ?)",
            (pid, content, RECORDED_AT, RECORDED_AT, RECORDED_AT),
        ).lastrowid
        db.execute(
            "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, status, created_at) "
            "VALUES (?, 'D001', 'UNSIGNED DRAFT: confirmed embolism', 'draft', '2026-10-23')",
            (pid,),
        )
    fact = _facts(_context(pid), "notes")[0]
    assert fact["source"]["id"] == note_id
    assert fact["text"].endswith(content)
    assert "signed with unverified acknowledgement by D001" in fact["text"]
    assert "UNSIGNED" not in fact["text"]
    assert "Raw dictation" not in fact["text"]
    assert not _facts(_context(pid), "unresolved_tasks")


def test_only_full_reviewed_imaging_text_is_evidence_and_no_exam_absence_is_inferred(pid):
    reviewed_text = "Image reviewed. " * 30 + "No pneumothorax; scan does not exclude pulmonary embolism."
    with db_session() as db:
        image_id = db.execute(
            "INSERT INTO lens_analyses (patient_id, author_id, image_path, image_type, analysis, "
            "reviewed_text, reviewed_at, reviewed_by, source) "
            "VALUES (?, 'D001', '', 'ct', 'UNVALIDATED DRAFT: pulmonary embolism', ?, ?, 'D001', 'simulated')",
            (pid, reviewed_text, RECORDED_AT),
        ).lastrowid
        db.execute(
            "INSERT INTO lens_analyses (patient_id, author_id, image_path, image_type, analysis, created_at) "
            "VALUES (?, 'D001', '', 'ct', 'NEW UNVALIDATED DRAFT: pulmonary embolism', '2026-10-23')",
            (pid,),
        )
    fact = _facts(_context(pid), "imaging")[0]
    assert fact["source"]["id"] == image_id
    assert fact["source"]["recorded_at"] == RECORDED_AT
    assert fact["text"].startswith(sage_service.SIMULATED_RESULT_PREFIX)
    assert fact["text"].endswith(reviewed_text)
    assert "UNVALIDATED" not in fact["text"]


def test_problem_sources_and_patient_chart_mutations_are_preserved_without_inference(pid):
    with db_session() as db:
        problem_id = db.execute(
            "INSERT INTO problem_list (patient_id, problem, source, recorded_at) "
            "VALUES (?, 'Possible asthma, not diagnosed', 'llm_extracted', ?)", (pid, RECORDED_AT),
        ).lastrowid
    before = _context(pid)
    problem = _facts(before, "problems")[0]
    assert problem["source"]["id"] == problem_id
    assert "llm_extracted" in problem["text"]
    assert problem["text"].endswith("Possible asthma, not diagnosed")
    with db_session() as db:
        db.execute("UPDATE patients SET diagnosis = 'Updated documented diagnosis' WHERE id = ?", (pid,))
    after = _context(pid)
    assert "not confirmed" in _facts(before, "diagnosis")[0]["text"]
    assert "Updated documented diagnosis" in _facts(after, "diagnosis")[0]["text"]


def test_problem_list_entry_does_not_fill_an_unknown_recorded_diagnosis(pid):
    with db_session() as db:
        db.execute("UPDATE patients SET diagnosis = NULL WHERE id = ?", (pid,))
        db.execute(
            "INSERT INTO problem_list (patient_id, problem, source) "
            "VALUES (?, 'Possible asthma, not diagnosed', 'llm_extracted')", (pid,),
        )
    context = _context(pid)
    assert not _facts(context, "diagnosis")
    assert _facts(context, "problems")[0]["text"].endswith("Possible asthma, not diagnosed")
