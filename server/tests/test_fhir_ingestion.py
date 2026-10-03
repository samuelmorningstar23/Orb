"""Orb Hospital OS — FHIR R4 Ingestion Tests

Tests for the FHIRAdapter (interoperability / partner-hospital onboarding):
  - Synthea-style Bundle of vital Observations (LOINC mapping, Temp normalization,
    plausibility validation, patient reference stripping)
  - Single Observation resource (not wrapped in a Bundle)
  - Mixed clinical Bundle (Patient / Encounter / MedicationRequest / AllergyIntolerance)
  - Implausible vital rejection
  - Malformed / empty payload handling

These exercise the adapter directly (no HTTP), mirroring test_ingestion.py.
"""
import sys
import json
import unittest
from pathlib import Path

# Add server root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ingestion_adapters import (
    FHIRAdapter,
    CanonicalEvent,
    ObservationType,
    ValueType,
    ParseError,
    select_adapter,
)


def _observation(loinc, display, value, unit, *, patient="Patient/PAT001",
                 encounter="Encounter/ENC001"):
    """Build a minimal FHIR R4 Observation resource with a valueQuantity."""
    return {
        "resourceType": "Observation",
        "status": "final",
        "code": {
            "text": display,
            "coding": [{"system": "http://loinc.org", "code": loinc, "display": display}],
        },
        "subject": {"reference": patient},
        "encounter": {"reference": encounter},
        "effectiveDateTime": "2024-01-01T12:00:00Z",
        "valueQuantity": {"value": value, "unit": unit},
    }


def _bundle(*resources, btype="transaction"):
    return {
        "resourceType": "Bundle",
        "type": btype,
        "entry": [{"resource": r} for r in resources],
    }


class TestFHIRVitalBundle(unittest.TestCase):
    """Synthea-style Bundle of vital Observations."""

    def setUp(self):
        self.adapter = FHIRAdapter()
        self.bundle = _bundle(
            _observation("8867-4", "Heart rate", 88, "beats/minute"),
            _observation("2708-6", "Oxygen saturation", 97, "%"),
            _observation("8310-5", "Body temperature", 98.6, "°F"),
        )

    def test_can_handle(self):
        self.assertTrue(self.adapter.can_handle(json.dumps(self.bundle)))
        self.assertFalse(self.adapter.can_handle("MSH|^~\\&|"))
        # A plain (non-FHIR) JSON webhook must NOT be claimed by the FHIR adapter.
        self.assertFalse(self.adapter.can_handle('{"patient_id": "PAT001", "observations": []}'))

    def test_auto_detect_routes_to_fhir(self):
        adapter = select_adapter(json.dumps(self.bundle))
        self.assertIsInstance(adapter, FHIRAdapter)

    def test_event_count(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        self.assertEqual(len(events), 3)

    def test_patient_reference_stripped(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        for evt in events:
            self.assertEqual(evt.patient_identifier, "PAT001")
            self.assertEqual(evt.encounter_identifier, "ENC001")

    def test_vital_keys_mapped(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        keys = {e.mapped_vital_key for e in events}
        self.assertEqual(keys, {"HR", "SpO2", "Temp"})
        for evt in events:
            self.assertEqual(evt.observation_type, ObservationType.VITAL)
            self.assertEqual(evt.observation_code_system, "LOINC")
            self.assertEqual(evt.source_type, "fhir")

    def test_hr_numeric_plausible(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        hr = next(e for e in events if e.mapped_vital_key == "HR")
        self.assertEqual(hr.value_numeric, 88.0)
        self.assertEqual(hr.value_type, ValueType.NUMERIC)
        self.assertEqual(hr.observation_code, "8867-4")

    def test_temp_fahrenheit_normalized(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        temp = next(e for e in events if e.mapped_vital_key == "Temp")
        # 98.6°F → 37.0°C, and within plausible bounds so numeric is retained.
        self.assertAlmostEqual(temp.value_numeric, 37.0, places=1)
        self.assertEqual(temp.unit, "°C")

    def test_observed_at_from_effective(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        for evt in events:
            self.assertEqual(evt.observed_at, "2024-01-01T12:00:00Z")


class TestFHIRSingleObservation(unittest.TestCase):
    """A single Observation resource (not wrapped in a Bundle)."""

    def setUp(self):
        self.adapter = FHIRAdapter()

    def test_single_observation(self):
        obs = _observation("8480-6", "Systolic blood pressure", 118, "mmHg")
        events = self.adapter.parse(json.dumps(obs))
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt.patient_identifier, "PAT001")
        self.assertEqual(evt.mapped_vital_key, "SBP")
        self.assertEqual(evt.value_numeric, 118.0)
        self.assertEqual(evt.observation_type, ObservationType.VITAL)

    def test_single_lab_observation(self):
        # Non-vital LOINC → classified as LAB, no vital key.
        obs = {
            "resourceType": "Observation",
            "status": "final",
            "code": {
                "text": "Hemoglobin",
                "coding": [{"system": "http://loinc.org", "code": "718-7",
                            "display": "Hemoglobin"}],
            },
            "subject": {"reference": "Patient/PAT002"},
            "valueQuantity": {"value": 13.5, "unit": "g/dL"},
            "referenceRange": [{"low": {"value": 12}, "high": {"value": 16}}],
            "interpretation": [{"coding": [{"code": "N"}]}],
        }
        events = self.adapter.parse(json.dumps(obs))
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt.observation_type, ObservationType.LAB)
        self.assertIsNone(evt.mapped_vital_key)
        self.assertEqual(evt.observation_name, "Hemoglobin")
        self.assertEqual(evt.value_numeric, 13.5)
        self.assertEqual(evt.reference_range, "12-16")
        self.assertEqual(evt.abnormal_flag, "N")

    def test_value_string_observation(self):
        obs = {
            "resourceType": "Observation",
            "status": "final",
            "code": {"text": "Blood group"},
            "subject": {"reference": "Patient/PAT003"},
            "valueString": "O positive",
        }
        events = self.adapter.parse(json.dumps(obs))
        self.assertEqual(events[0].value, "O positive")
        self.assertEqual(events[0].value_type, ValueType.TEXT)

    def test_subject_identifier_fallback(self):
        obs = {
            "resourceType": "Observation",
            "status": "final",
            "code": {"text": "Heart rate",
                     "coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
            "subject": {"identifier": {"value": "MRN-99"}},
            "valueQuantity": {"value": 72, "unit": "bpm"},
        }
        events = self.adapter.parse(json.dumps(obs))
        self.assertEqual(events[0].patient_identifier, "MRN-99")


class TestFHIRMixedBundle(unittest.TestCase):
    """Bundle mixing Patient + Encounter + MedicationRequest + AllergyIntolerance."""

    def setUp(self):
        self.adapter = FHIRAdapter()
        self.bundle = _bundle(
            {
                "resourceType": "Patient",
                "id": "PAT001",
                "name": [{"family": "Doe", "given": ["Jane"]}],
                "active": True,
            },
            {
                "resourceType": "Encounter",
                "id": "ENC001",
                "status": "in-progress",
                "subject": {"reference": "Patient/PAT001"},
            },
            {
                "resourceType": "MedicationRequest",
                "status": "active",
                "medicationCodeableConcept": {
                    "text": "Amoxicillin 500mg",
                    "coding": [{"display": "Amoxicillin"}],
                },
                "subject": {"reference": "Patient/PAT001"},
            },
            {
                "resourceType": "AllergyIntolerance",
                "criticality": "high",
                "code": {
                    "text": "Penicillin",
                    "coding": [{"display": "Penicillin G"}],
                },
                "patient": {"reference": "Patient/PAT001"},
            },
        )

    def test_event_types(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        by_type = {e.event_type: e for e in events}
        self.assertEqual(set(by_type), {"patient", "encounter",
                                        "medication_request", "allergy"})

    def test_patient_resource(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        pat = next(e for e in events if e.event_type == "patient")
        self.assertEqual(pat.patient_identifier, "PAT001")
        self.assertEqual(pat.observation_name, "Patient demographics")
        self.assertEqual(pat.value, "Jane Doe")

    def test_encounter_resource(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        enc = next(e for e in events if e.event_type == "encounter")
        self.assertEqual(enc.patient_identifier, "PAT001")
        self.assertEqual(enc.encounter_identifier, "ENC001")

    def test_medication_request_resource(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        med = next(e for e in events if e.event_type == "medication_request")
        self.assertEqual(med.patient_identifier, "PAT001")
        self.assertEqual(med.observation_name, "MedicationRequest")
        self.assertEqual(med.value, "Amoxicillin 500mg")

    def test_allergy_resource(self):
        events = self.adapter.parse(json.dumps(self.bundle))
        allergy = next(e for e in events if e.event_type == "allergy")
        self.assertEqual(allergy.patient_identifier, "PAT001")
        self.assertEqual(allergy.observation_name, "AllergyIntolerance")
        self.assertEqual(allergy.value, "Penicillin")
        self.assertEqual(allergy.abnormal_flag, "high")

    def test_unknown_resource_skipped(self):
        bundle = _bundle(
            _observation("8867-4", "Heart rate", 80, "bpm"),
            {"resourceType": "DeviceMetric", "id": "dev1"},
            {"resourceType": "Provenance", "id": "prov1"},
        )
        events = self.adapter.parse(json.dumps(bundle))
        # Only the Observation is emitted; unknown types skipped gracefully.
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].mapped_vital_key, "HR")


class TestFHIRPlausibility(unittest.TestCase):
    """Implausible vital readings must have value_numeric rejected."""

    def setUp(self):
        self.adapter = FHIRAdapter()

    def test_implausible_spo2_rejected(self):
        obs = _observation("2708-6", "Oxygen saturation", 900, "%")
        events = self.adapter.parse(json.dumps(obs))
        evt = events[0]
        self.assertEqual(evt.mapped_vital_key, "SpO2")
        # 900% is physiologically impossible → numeric dropped, event still emitted.
        self.assertIsNone(evt.value_numeric)

    def test_implausible_hr_rejected(self):
        obs = _observation("8867-4", "Heart rate", -5, "bpm")
        events = self.adapter.parse(json.dumps(obs))
        self.assertIsNone(events[0].value_numeric)


class TestFHIRErrorHandling(unittest.TestCase):
    """Malformed and empty payloads must raise ParseError (never crash)."""

    def setUp(self):
        self.adapter = FHIRAdapter()

    def test_malformed_json_raises(self):
        with self.assertRaises(ParseError):
            self.adapter.parse("not valid json {{{")

    def test_empty_bundle_raises(self):
        empty = {"resourceType": "Bundle", "type": "collection", "entry": []}
        with self.assertRaises(ParseError):
            self.adapter.parse(json.dumps(empty))

    def test_bundle_with_only_unknown_types_raises(self):
        bundle = _bundle(
            {"resourceType": "Device", "id": "d1"},
            {"resourceType": "Organization", "id": "o1"},
        )
        with self.assertRaises(ParseError):
            self.adapter.parse(json.dumps(bundle))

    def test_missing_resource_type_raises(self):
        with self.assertRaises(ParseError):
            self.adapter.parse('{"foo": "bar"}')


class TestFHIRAdapterMetadata(unittest.TestCase):
    """Adapter identity / registration."""

    def test_adapter_name(self):
        self.assertEqual(FHIRAdapter().adapter_name, "fhir_r4")

    def test_source_type(self):
        self.assertEqual(FHIRAdapter().source_type, "fhir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
