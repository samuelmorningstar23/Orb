"""Slice 7 — unified patient story aggregation + type filtering."""
import uuid

import pytest

from database import db_session
from services.patient_story_service import get_patient_story, STORY_TYPES

# Every patient this module creates carries this prefix so the teardown below
# can find them all without a hand-kept list.
_PID_PREFIX = "PS-"


@pytest.fixture(autouse=True, scope="module")
def _discharge_this_modules_patients():
    """TEST-ISOLATION LAW: `patients.status` DEFAULTS to 'admitted', so every
    patient this module creates stays on the shared session DB's ward for the
    rest of the run. That is not cosmetic. Two things downstream break:

      * the FHIR self-export walks `patients WHERE status='admitted'` and sleeps
        250 ms BETWEEN patients, so each leftover taxes every later export;
      * Sage's patient-name matcher is fuzzy at 0.70 BY DESIGN (for ASR
        mis-transcriptions), so a leaked admitted patient can answer a name it
        was never meant to. That is exactly what happened on 2026-08-08: a
        fixture patient named "Lab Test" scored **0.857 against the word
        "latest"**, and `test_sage_sticky_anchor.py::test_naming_a_patient_who_
        exists_nowhere_suppresses_the_anchor` flipped from "NO MATCHING PATIENT"
        to an out-of-department denial — a cross-module failure with no
        relationship whatsoever to the code under test.

    Hence both halves of the fix: fixture names are now checked against the
    matcher (see the names below), AND nothing this module admits stays admitted.

    Discharge, do not DELETE — audit rows written during these tests legitimately
    reference these ids. Close the ADMISSIONS row too: the census surfaces filter
    `patients.status` while the department-scoped surfaces join `admissions`, so
    closing only the patient leaves the residue half-visible, which looks fixed
    and is not.
    """
    yield
    with db_session() as db:
        db.execute(
            "UPDATE patients SET status = 'discharged' WHERE id LIKE ?",
            (f"{_PID_PREFIX}%",),
        )
        db.execute(
            "UPDATE admissions SET status = 'discharged' "
            "WHERE patient_id LIKE ? AND status = 'active'",
            (f"{_PID_PREFIX}%",),
        )


def _mk_patient():
    pid = f"PS-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, admission_date) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, "Story Test", f"MRN-{uuid.uuid4().hex[:6]}", "ICU", "ICU-1",
             "Sepsis", "2026-07-01T08:00:00Z"),
        )
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source, started_at) "
            "VALUES (?,?,?,?,?, 'active', 'clinician', ?)",
            (pid, "Ceftriaxone", "2g", "IV", "OD", "2026-07-01T09:00:00Z"),
        )
        db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, created_at) "
            "VALUES (?, 'note', 'Progress', ?, ?)",
            (pid, "Patient improving on antibiotics.", "2026-07-02T10:00:00Z"),
        )
    return pid


def test_story_merges_multiple_sources_newest_first():
    pid = _mk_patient()
    story = get_patient_story(pid)
    types = {e["type"] for e in story}
    assert "admission" in types
    assert "medication" in types
    assert "note" in types
    # Newest first: the note (Jul 2) precedes the medication (Jul 1) precedes admission.
    ts = [e["timestamp"] for e in story if e["timestamp"]]
    assert ts == sorted(ts, reverse=True)


def test_story_type_filter():
    pid = _mk_patient()
    only_meds = get_patient_story(pid, ["medication"])
    assert only_meds, "expected at least one medication event"
    assert all(e["type"] == "medication" for e in only_meds)
    # An unknown type filters to nothing (not everything).
    assert get_patient_story(pid, ["nonexistent-type"]) == []


def test_story_event_shape():
    pid = _mk_patient()
    for e in get_patient_story(pid):
        assert set(e.keys()) == {"timestamp", "type", "title", "detail", "actor"}
        assert e["type"] in STORY_TYPES


def test_story_empty_for_unknown_patient():
    assert get_patient_story("no-such-patient") == []


# ─────────────────────────────────────────────────────────────────────────────
# Regression, 2026-08-08 (Wave 7). The story used to finish with
#
#     events.sort(key=lambda e: e["timestamp"] or "", reverse=True)
#
# — a LEXICAL sort over a feed whose rows arrive in two timestamp dialects.
# ``orders.created_at`` and ``medications.started_at`` take SQLite's
# CURRENT_TIMESTAMP default ("2026-07-05 09:00:00", space separator) while
# risk_scores and application writes use ISO ("2026-07-03T09:00:00Z"). ' ' (0x20)
# sorts before 'T' (0x54), so EVERY bare-dialect row sorted below EVERY ISO row
# no matter when it actually happened.
#
# On a real chart that files every order and every drug start underneath every
# alert and note. This is the one surface a clinician reads to establish
# sequence — "was the antibiotic given before or after the lactate?" — so a
# silent reordering is a clinical defect, not a cosmetic one.
# ─────────────────────────────────────────────────────────────────────────────

def test_story_orders_mixed_timestamp_dialects_by_real_instant():
    pid = f"PS-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, admission_date) "
            "VALUES (?,?,?,'ICU','ICU-9','Sepsis','2026-07-01T08:00:00Z')",
            (pid, "Orbfixture Dialectcase", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        # ISO dialect, EARLIER instant (09:00).
        db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, created_at) "
            "VALUES (?, 'note', 'Earlier', 'PS-EARLIER-NOTE', '2026-07-03T09:00:00Z')",
            (pid,),
        )
        # Bare SQLite dialect, LATER instant (12:00) on the SAME DAY — which is
        # where the separator actually decides the comparison.
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
            "status, source, started_at) "
            "VALUES (?, 'PS-LATER-DRUG', '1g', 'IV', 'OD', 'active', 'clinician', "
            "'2026-07-03 12:00:00')",
            (pid,),
        )

    assert "2026-07-03 12:00:00" < "2026-07-03T09:00:00Z", (
        "premise: lexically the LATER bare-dialect row sorts BELOW the earlier "
        "ISO one, so a reverse string sort files it last"
    )

    story = get_patient_story(pid)
    positions = {}
    for i, e in enumerate(story):
        if "PS-EARLIER-NOTE" in (e["detail"] or ""):
            positions["note"] = i
        if "PS-LATER-DRUG" in (e["title"] or ""):
            positions["med"] = i
    assert {"note", "med"} <= positions.keys(), f"both events must appear: {story}"
    # Newest first, so the LATER medication must come BEFORE the earlier note.
    assert positions["med"] < positions["note"]


def test_story_carries_the_streams_that_were_missing(monkeypatch):
    """Labs, care bundles, problems, allergies and order transitions are part of
    "everything that happened" and were absent from the narrative entirely."""
    pid = f"PS-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, admission_date) "
            "VALUES (?,?,?,'ICU','ICU-8','Sepsis','2026-07-01T08:00:00Z')",
            (pid, "Orbfixture Streamscase", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT INTO observations (patient_id, observation_name, value_numeric, unit, "
            "abnormal_flag, observed_at) VALUES (?, 'Serum lactate', 4.2, 'mmol/L', 'HH', "
            "'2026-07-02T10:00:00Z')",
            (pid,),
        )
        db.execute(
            "INSERT INTO care_bundles (patient_id, bundle_type, trigger_reason, triggered_at) "
            "VALUES (?, 'sepsis6', 'NEWS2 >= 7', '2026-07-02T10:05:00Z')",
            (pid,),
        )
        db.execute(
            "INSERT INTO problem_list (patient_id, problem, icd10_code, recorded_at) "
            "VALUES (?, 'Septic shock', 'R65.21', '2026-07-02T11:00:00Z')",
            (pid,),
        )
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, recorded_at) "
            "VALUES (?, 'ceftriaxone', 'anaphylaxis', 'severe', '2026-07-02T12:00:00Z')",
            (pid,),
        )

    types = {e["type"] for e in get_patient_story(pid)}
    assert {"observation", "bundle", "problem", "allergy"} <= types
    assert types <= set(STORY_TYPES), "every emitted type must be in the filter vocabulary"


def test_a_critically_abnormal_lab_is_not_rendered_as_routine():
    pid = f"PS-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, admission_date) "
            "VALUES (?,?,?,'ICU','ICU-7','Sepsis','2026-07-01T08:00:00Z')",
            (pid, "Orbfixture Labscase", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT INTO observations (patient_id, observation_name, value_numeric, unit, "
            "abnormal_flag, observed_at) VALUES (?, 'Potassium', 7.1, 'mmol/L', 'HH', "
            "'2026-07-02T10:00:00Z')",
            (pid,),
        )
    obs = [e for e in get_patient_story(pid) if e["type"] == "observation"]
    assert obs and "7.1" in obs[0]["detail"] and "HH" in obs[0]["detail"]
