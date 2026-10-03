"""Revenue Integrity / coding & CDI service tests.

Covers deterministic code suggestion (validated against the local terminology
subset), CDI gap flagging, the illustrative reimbursement estimate, and safe
handling of an empty patient. The LLM is not required — the deterministic path
is what runs.
"""
import uuid

import pytest

from database import db_session
from services import coding_service, terminology_service


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module", autouse=True)
def _seed_terminology():
    """Ensure the curated terminology starter set is present (idempotent).

    conftest initializes the schema but does not seed terminology, so we seed it
    here — coding_service only emits codes that exist in `terminology`.
    """
    terminology_service.seed_terminology()


_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: patients.status DEFAULTS to 'admitted', so every
    patient `_new_patient` seeds used to stay admitted in the shared session DB
    — ~6 by the end of the module. That is not a cosmetic leak. The FHIR
    self-export in services/downtime_service.py iterates
    `patients WHERE status='admitted'` and sleeps _PATIENT_SLEEP_S (250 ms)
    BETWEEN PATIENTS, so each leftover adds ~0.275 s to every export call in
    tests/test_downtime_fhir_export.py and to everything downstream of it.

    The seeding happens in a plain helper rather than a fixture, so the helper
    registers each id here and this autouse teardown discharges the batch. Do
    not DELETE — the profile, medication and coding rows written during the test
    legitimately reference these ids."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _SEEDED],
        )
    _SEEDED.clear()


def _new_patient(diagnosis: str | None = None) -> str:
    pid = f"PATCODE{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, diagnosis) VALUES (?, ?, ?, ?)",
            (pid, "Coding Test", f"MRN-{uuid.uuid4().hex[:6]}", diagnosis),
        )
    _SEEDED.append(pid)
    return pid


@pytest.fixture()
def sepsis_patient() -> str:
    """A patient with a Sepsis diagnosis, a documented comorbidity, and a med."""
    pid = _new_patient(diagnosis="Sepsis secondary to pneumonia")
    with db_session() as db:
        db.execute(
            "INSERT INTO patient_profiles (patient_id, comorbidities) VALUES (?, ?)",
            (pid, "Type 2 diabetes mellitus, hypertension"),
        )
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, "Metformin", "500mg", "Oral", "BD"),
        )
    return pid


# ── suggestions ────────────────────────────────────────────────────────────────
def test_suggests_valid_terminology_code(sepsis_patient):
    result = coding_service.suggest_codes(sepsis_patient)

    assert result["patient_id"] == sepsis_patient
    assert result["suggested_codes"], "expected at least one suggested code"

    codes = {(c["system"], c["code"]) for c in result["suggested_codes"]}
    # Sepsis from the diagnosis text → A41.9 (present in the seed terminology).
    assert ("ICD10", "A41.9") in codes

    # Every emitted code must actually exist in the terminology table.
    for c in result["suggested_codes"]:
        assert terminology_service.validate_code(c["system"], c["code"]), \
            f"emitted non-existent code {c['system']} {c['code']}"
        assert c["display"]
        assert 0.0 <= c["confidence"] <= 1.0
        assert c["evidence"]


def test_medication_infers_diabetes_code(sepsis_patient):
    result = coding_service.suggest_codes(sepsis_patient)
    codes = {(c["system"], c["code"]) for c in result["suggested_codes"]}
    # Metformin + "diabetes" comorbidity → E11.9.
    assert ("ICD10", "E11.9") in codes


# ── CDI flags ──────────────────────────────────────────────────────────────────
def test_cdi_flag_is_produced(sepsis_patient):
    result = coding_service.suggest_codes(sepsis_patient)
    flags = result["cdi_flags"]
    assert flags, "expected at least one CDI flag"

    for f in flags:
        assert f["type"]
        assert f["message"]
        assert f["rationale"]
        assert f["est_impact_inr"] >= 0

    types = {f["type"] for f in flags}
    # Sepsis, unspecified organism → a specificity flag; metformin → uncoded-from-med.
    assert "nonspecific_code" in types
    assert "uncoded_condition_from_medication" in types


# ── reimbursement ───────────────────────────────────────────────────────────────
def test_reimbursement_fields_present_and_non_negative(sepsis_patient):
    reimb = coding_service.suggest_codes(sepsis_patient)["reimbursement"]
    for key in ("captured_inr", "potential_inr", "uplift_inr"):
        assert key in reimb
        assert reimb[key] >= 0
    # Nothing is coded yet but conditions are suggested → real uplift opportunity.
    assert reimb["potential_inr"] >= reimb["captured_inr"]
    assert reimb["uplift_inr"] == round(reimb["potential_inr"] - reimb["captured_inr"], 2)
    assert "assumptions" in reimb
    assert reimb["assumptions"]["base_rate_inr"] > 0
    assert reimb["assumptions"]["citations"]


def test_capture_total_equals_the_flags_listed_under_it(sepsis_patient):
    """The headline used to leave out the sharper-code flags (+₹2,000 each), so
    it read short of the sum of the cards printed beneath it (U309)."""
    result = coding_service.suggest_codes(sepsis_patient)
    flags, reimb = result["cdi_flags"], result["reimbursement"]
    nonspecific = [f for f in flags if f["type"] == "nonspecific_code"]
    assert nonspecific, "fixture should carry an unspecified code"
    assert reimb["specificity_uplift_inr"] == round(sum(f["est_impact_inr"] for f in nonspecific), 2)
    assert reimb["uplift_inr"] == round(sum(f["est_impact_inr"] for f in flags), 2)


# ── empty / unknown patient ────────────────────────────────────────────────────
def test_empty_patient_no_crash():
    pid = _new_patient(diagnosis=None)  # exists but no clinical data
    result = coding_service.suggest_codes(pid)
    assert result["patient_id"] == pid
    assert result["suggested_codes"] == []
    assert result["cdi_flags"] == []
    assert result["reimbursement"]["captured_inr"] == 0
    assert result["reimbursement"]["potential_inr"] == 0
    assert result["reimbursement"]["uplift_inr"] == 0


def test_unknown_patient_no_crash():
    result = coding_service.suggest_codes("NO_SUCH_PATIENT")
    assert result["suggested_codes"] == []
    assert result["cdi_flags"] == []
    assert result["reimbursement"]["potential_inr"] == 0


def test_use_llm_hook_is_noop_without_llm(sepsis_patient):
    """use_llm=True must behave identically (deterministic) when no LLM is wired."""
    base = coding_service.suggest_codes(sepsis_patient, use_llm=False)
    llm = coding_service.suggest_codes(sepsis_patient, use_llm=True)
    assert base["suggested_codes"] == llm["suggested_codes"]
    assert base["cdi_flags"] == llm["cdi_flags"]
