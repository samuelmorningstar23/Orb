"""Orb Hospital OS — FHIR R4 Export / Write-Back Tests

Exercises services/fhir_export_service.py against a seeded test patient:
  - Patient resource shape (resourceType / id / name / identifier / gender)
  - Observations emit valid LOINC-coded vitals with valueQuantity
  - Provenance & safety: an llm_extracted MedicationRequest is a *draft* carrying
    the ai-draft tag, while a clinician med is *active* with no ai-draft tag
  - AllergyIntolerance / Condition serialisation + provenance
  - The collection Bundle wraps every resource type with fullUrls + provenance
  - Interoperability round-trip: an exported Observation re-ingests cleanly
    through the ingestion FHIRAdapter to the same internal vital key/value

Data is seeded directly into the isolated test DB (conftest sets ORB_DB_PATH and
initialises the schema/migrations once per session).
"""
import json
import sys
from pathlib import Path

import pytest

# Add server root to path (mirrors sibling test modules).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import get_db
from services import fhir_export_service as export
from services.ingestion_adapters import FHIRAdapter, ObservationType

PATIENT_ID = "FHIREXP001"
MRN = "MRN-FHIREXP-001"


@pytest.fixture(scope="module")
def seeded_patient():
    """Seed one patient with a clinician med, an llm_extracted med, an allergy,
    a problem and a vitals row. Returns the patient id."""
    db = get_db()
    try:
        # Clean any prior run's rows for idempotency.
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (PATIENT_ID,))
        db.execute("DELETE FROM medications WHERE patient_id = ?", (PATIENT_ID,))
        db.execute("DELETE FROM patient_allergies WHERE patient_id = ?", (PATIENT_ID,))
        db.execute("DELETE FROM problem_list WHERE patient_id = ?", (PATIENT_ID,))
        db.execute("DELETE FROM patients WHERE id = ?", (PATIENT_ID,))

        db.execute(
            "INSERT INTO patients (id, name, mrn, sex, date_of_birth, status) "
            "VALUES (?, ?, ?, ?, ?, 'admitted')",
            (PATIENT_ID, "Jane Doe", MRN, "F", "1985-06-15"),
        )
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
            "diastolic_bp, spo2, temperature, respiratory_rate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (PATIENT_ID, "2026-07-03T10:00:00Z", 88, 120, 80, 97, 37.0, 16),
        )
        # Clinician-entered medication → active, no ai-draft.
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
            "status, source) VALUES (?, 'Amoxicillin', '500mg', 'Oral', 'TID', 'active', 'clinician')",
            (PATIENT_ID,),
        )
        # LLM-extracted medication → MUST export as a draft with the ai-draft tag.
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
            "status, source) VALUES (?, 'Lisinopril', '10mg', 'Oral', 'once daily', 'active', 'llm_extracted')",
            (PATIENT_ID,),
        )
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
            "status, source) VALUES (?, 'penicillin', 'anaphylaxis', 'severe', 'active', 'llm_extracted')",
            (PATIENT_ID,),
        )
        db.execute(
            "INSERT INTO problem_list (patient_id, problem, icd10_code, status, source) "
            "VALUES (?, 'Type 2 diabetes mellitus', 'E11.9', 'active', 'clinician')",
            (PATIENT_ID,),
        )
        db.commit()
    finally:
        db.close()
    return PATIENT_ID


def _has_ai_draft_tag(resource: dict) -> bool:
    tags = resource.get("meta", {}).get("tag", [])
    return any(t.get("code") == export.AI_DRAFT_CODE
               and t.get("system") == export.ORB_PROVENANCE_SYSTEM for t in tags)


def _has_orb_source_tag(resource: dict) -> bool:
    tags = resource.get("meta", {}).get("tag", [])
    return any(t.get("code") == export.ORB_SOURCE_CODE
               and t.get("system") == export.ORB_SOURCE_SYSTEM for t in tags)


# ── Patient ──────────────────────────────────────────────────────────────────

def test_patient_resource_shape(seeded_patient):
    resource = export.patient_to_fhir(seeded_patient)
    assert resource is not None
    assert resource["resourceType"] == "Patient"
    assert resource["id"] == seeded_patient
    assert resource["name"][0]["text"] == "Jane Doe"
    assert resource["gender"] == "female"
    assert resource["birthDate"] == "1985-06-15"
    assert resource["identifier"][0]["value"] == MRN
    assert _has_orb_source_tag(resource)


def test_patient_missing_returns_none():
    assert export.patient_to_fhir("NO_SUCH_PATIENT") is None


# ── Observations ─────────────────────────────────────────────────────────────

def test_observations_loinc_and_valuequantity(seeded_patient):
    obs = export.observations_to_fhir(seeded_patient)
    # One Observation per non-null vital in the single seeded row.
    assert len(obs) == 6

    loinc_codes = set()
    for o in obs:
        assert o["resourceType"] == "Observation"
        assert o["status"] == "final"
        assert o["subject"]["reference"] == f"Patient/{seeded_patient}"
        coding = o["code"]["coding"][0]
        assert coding["system"] == "http://loinc.org"
        loinc_codes.add(coding["code"])
        vq = o["valueQuantity"]
        assert vq["value"] is not None
        assert vq["system"] == "http://unitsofmeasure.org"
        assert "effectiveDateTime" in o
        assert _has_orb_source_tag(o)

    assert loinc_codes == {"8867-4", "8480-6", "8462-4", "9279-1", "8310-5", "2708-6"}


def test_observations_since_filter_excludes_older(seeded_patient):
    # A future `since` cutoff filters out the seeded (older) row.
    assert export.observations_to_fhir(seeded_patient, since="2099-01-01T00:00:00Z") == []


# ── MedicationRequest provenance / safety ────────────────────────────────────

def test_medication_provenance_flags(seeded_patient):
    meds = export.medications_to_fhir(seeded_patient)
    by_name = {m["medicationCodeableConcept"]["text"]: m for m in meds}
    assert set(by_name) == {"Amoxicillin", "Lisinopril"}

    clinician = by_name["Amoxicillin"]
    assert clinician["status"] == "active"
    assert clinician["intent"] == "order"
    assert not _has_ai_draft_tag(clinician)

    ai = by_name["Lisinopril"]
    # An unverified AI-extracted med must NEVER be an active order.
    assert ai["status"] == "draft"
    assert ai["intent"] == "proposal"
    assert _has_ai_draft_tag(ai)

    for m in meds:
        assert m["resourceType"] == "MedicationRequest"
        assert m["subject"]["reference"] == f"Patient/{seeded_patient}"
        assert m["dosageInstruction"][0]["text"]
        assert _has_orb_source_tag(m)


# ── AllergyIntolerance ───────────────────────────────────────────────────────

def test_allergies_resource(seeded_patient):
    allergies = export.allergies_to_fhir(seeded_patient)
    assert len(allergies) == 1
    a = allergies[0]
    assert a["resourceType"] == "AllergyIntolerance"
    assert a["code"]["text"] == "penicillin"
    assert a["criticality"] == "high"          # severe → high
    assert a["clinicalStatus"]["coding"][0]["code"] == "active"
    assert a["patient"]["reference"] == f"Patient/{seeded_patient}"
    # Sourced from llm_extracted → carries ai-draft provenance tag.
    assert _has_ai_draft_tag(a)


# ── Condition ────────────────────────────────────────────────────────────────

def test_conditions_resource(seeded_patient):
    conditions = export.conditions_to_fhir(seeded_patient)
    assert len(conditions) == 1
    c = conditions[0]
    assert c["resourceType"] == "Condition"
    assert c["code"]["text"] == "Type 2 diabetes mellitus"
    assert c["code"]["coding"][0]["code"] == "E11.9"
    assert c["clinicalStatus"]["coding"][0]["code"] == "active"
    # Clinician-entered → no ai-draft tag.
    assert not _has_ai_draft_tag(c)
    assert _has_orb_source_tag(c)


# ── Bundle ($everything) ─────────────────────────────────────────────────────

def test_patient_bundle_structure(seeded_patient):
    bundle = export.patient_bundle(seeded_patient)
    assert bundle is not None
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"
    assert _has_orb_source_tag(bundle)

    types = set()
    for entry in bundle["entry"]:
        assert entry["fullUrl"].startswith("https://orb.health/fhir/")
        res = entry["resource"]
        types.add(res["resourceType"])
        # Every wrapped resource carries Orb source provenance.
        assert _has_orb_source_tag(res)

    assert {"Patient", "Observation", "MedicationRequest",
            "AllergyIntolerance", "Condition"}.issubset(types)


def test_patient_bundle_missing_returns_none():
    assert export.patient_bundle("NO_SUCH_PATIENT") is None


# ── Interoperability round-trip ──────────────────────────────────────────────

def test_observation_roundtrips_through_ingestion_adapter(seeded_patient):
    """An exported Observation must re-ingest through the FHIRAdapter to the
    same internal vital key + numeric value — proving export/ingest are
    interoperable and use consistent LOINC/reference conventions."""
    obs = export.observations_to_fhir(seeded_patient)
    hr = next(o for o in obs if o["code"]["coding"][0]["code"] == "8867-4")

    events = FHIRAdapter().parse(json.dumps(hr))
    assert len(events) == 1
    evt = events[0]
    assert evt.patient_identifier == seeded_patient   # "Patient/{id}" reference stripped
    assert evt.mapped_vital_key == "HR"
    assert evt.value_numeric == 88.0
    assert evt.observation_type == ObservationType.VITAL
    assert evt.observation_code_system == "LOINC"
