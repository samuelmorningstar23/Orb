"""Orb Hospital OS — Ingestion Pipeline Tests

Tests for:
  - HL7 ORU parsing (multi-OBR, multi-OBX, malformed handling)
  - JSON webhook parsing
  - CSV replay parsing
  - Code-to-vital mapping
  - Deduplication fingerprinting
  - Source tagging
  - Unit normalization
  - THE VITALS WRITE ITSELF: carry-forward of the two NEWS2 parameters no feed
    can carry (ACVPU consciousness, supplemental oxygen). See
    TestIngestedRowCarriesSupplementalOxygen.
"""
import sys
import os
import unittest
from pathlib import Path

# Add server root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from database import db_session
from services.ingestion_service import _persist_vitals_row
from services.vigil_service import compute_news2
from services.ingestion_adapters import (
    HL7Adapter,
    JSONAdapter,
    CSVAdapter,
    CanonicalEvent,
    ObservationType,
    ValueType,
    ParseError,
    compute_fingerprint,
    map_to_vital_key,
    normalize_temp_to_celsius,
    select_adapter,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "scripts" / "fixtures"


class TestVitalKeyMapping(unittest.TestCase):
    """Test LOINC code and name mapping to internal vital keys."""

    def test_loinc_heart_rate(self):
        self.assertEqual(map_to_vital_key("8867-4", None), "HR")

    def test_loinc_spo2(self):
        self.assertEqual(map_to_vital_key("2708-6", None), "SpO2")

    def test_name_heart_rate(self):
        self.assertEqual(map_to_vital_key(None, "Heart Rate"), "HR")

    def test_name_case_insensitive(self):
        self.assertEqual(map_to_vital_key(None, "heart rate"), "HR")
        self.assertEqual(map_to_vital_key(None, "TEMPERATURE"), "Temp")

    def test_unknown_code(self):
        self.assertIsNone(map_to_vital_key("99999-9", "Glucose"))

    def test_code_priority_over_name(self):
        self.assertEqual(map_to_vital_key("8867-4", "Unknown Field"), "HR")


class TestTempNormalization(unittest.TestCase):
    """Test Fahrenheit-to-Celsius conversion."""

    def test_fahrenheit_conversion(self):
        result = normalize_temp_to_celsius(98.6, "°F")
        self.assertAlmostEqual(result, 37.0, places=1)

    def test_celsius_passthrough(self):
        result = normalize_temp_to_celsius(37.0, "Cel")
        self.assertEqual(result, 37.0)

    def test_none_unit(self):
        result = normalize_temp_to_celsius(37.0, None)
        self.assertEqual(result, 37.0)


class TestFingerprinting(unittest.TestCase):
    """Test dedup fingerprinting."""

    def test_same_payload_same_hash(self):
        a = compute_fingerprint("test payload")
        b = compute_fingerprint("test payload")
        self.assertEqual(a, b)

    def test_different_payload_different_hash(self):
        a = compute_fingerprint("payload A")
        b = compute_fingerprint("payload B")
        self.assertNotEqual(a, b)


class TestAdapterSelection(unittest.TestCase):
    """Test auto-detection of adapter by payload content."""

    def test_hl7_detected(self):
        adapter = select_adapter("MSH|^~\\&|SENDER|")
        self.assertIsInstance(adapter, HL7Adapter)

    def test_json_detected(self):
        adapter = select_adapter('{"patient_id": "PAT001", "observations": []}')
        self.assertIsInstance(adapter, JSONAdapter)

    def test_csv_detected(self):
        adapter = select_adapter("patient_id,name,code,value,unit\nPAT001,HR,8867-4,80,bpm\n")
        self.assertIsInstance(adapter, CSVAdapter)

    def test_unknown_payload(self):
        adapter = select_adapter("this is not any known format")
        self.assertIsNone(adapter)


class TestHL7Adapter(unittest.TestCase):
    """Test HL7 ORU message parsing."""

    def setUp(self):
        self.adapter = HL7Adapter()
        self.sample_path = FIXTURES_DIR / "sample.hl7"

    def test_can_handle_hl7(self):
        self.assertTrue(self.adapter.can_handle("MSH|^~\\&|"))
        self.assertFalse(self.adapter.can_handle('{"json": true}'))

    def test_parse_sample_file(self):
        """Parse the full sample HL7 file and validate event counts."""
        self.assertTrue(
            self.sample_path.exists(),
            f"Required fixture missing: {self.sample_path} — the HL7 ingestion "
            "path (partner-hospital onboarding) must be exercised, not skipped.",
        )

        raw = self.sample_path.read_text()
        events = self.adapter.parse(raw)

        # Should have 11 OBX segments: 6 vitals + 5 labs
        self.assertEqual(len(events), 11)

    def test_vitals_mapped_correctly(self):
        """Verify vital signs are mapped to correct internal keys."""
        self.assertTrue(
            self.sample_path.exists(),
            f"Required fixture missing: {self.sample_path} — the HL7 ingestion "
            "path (partner-hospital onboarding) must be exercised, not skipped.",
        )

        raw = self.sample_path.read_text()
        events = self.adapter.parse(raw)

        vitals = [e for e in events if e.observation_type == ObservationType.VITAL]
        labs = [e for e in events if e.observation_type == ObservationType.LAB]

        self.assertEqual(len(vitals), 6)
        self.assertEqual(len(labs), 5)

        vital_keys = {e.mapped_vital_key for e in vitals}
        self.assertEqual(vital_keys, {"HR", "SBP", "DBP", "SpO2", "Temp", "RR"})

    def test_patient_id_extracted(self):
        self.assertTrue(
            self.sample_path.exists(),
            f"Required fixture missing: {self.sample_path} — the HL7 ingestion "
            "path (partner-hospital onboarding) must be exercised, not skipped.",
        )

        raw = self.sample_path.read_text()
        events = self.adapter.parse(raw)

        for evt in events:
            self.assertEqual(evt.patient_identifier, "PAT001")

    def test_obr_groups_preserved(self):
        """Ensure OBR grouping is maintained for lab panels."""
        self.assertTrue(
            self.sample_path.exists(),
            f"Required fixture missing: {self.sample_path} — the HL7 ingestion "
            "path (partner-hospital onboarding) must be exercised, not skipped.",
        )

        raw = self.sample_path.read_text()
        events = self.adapter.parse(raw)

        group_ids = {e.obr_group_id for e in events}
        self.assertEqual(len(group_ids), 2, "Should have 2 distinct OBR groups")

    def test_numeric_values_parsed(self):
        self.assertTrue(
            self.sample_path.exists(),
            f"Required fixture missing: {self.sample_path} — the HL7 ingestion "
            "path (partner-hospital onboarding) must be exercised, not skipped.",
        )

        raw = self.sample_path.read_text()
        events = self.adapter.parse(raw)

        hr_event = next(e for e in events if e.mapped_vital_key == "HR")
        self.assertEqual(hr_event.value_numeric, 112.0)
        self.assertEqual(hr_event.value_type, ValueType.NUMERIC)

    def test_missing_pid_raises(self):
        """Missing PID segment should raise ParseError."""
        raw = "MSH|^~\\&|SENDER||\nOBX|1|NM|8867-4^HR^LN||80|bpm"
        with self.assertRaises(ParseError):
            self.adapter.parse(raw)

    def test_malformed_obx_skipped(self):
        """Malformed OBX segments should be skipped, not crash the parser."""
        raw = (
            "MSH|^~\\&|SENDER||\n"
            "PID|1||PAT001\n"
            "OBR|1|ORD001|LAB001|85354-9^Vital Signs^LN\n"
            "OBX|1|NM|8867-4^Heart Rate^LN||80|bpm\n"
            "OBX|MALFORMED\n"
            "OBX|3|NM|9279-1^Respiratory Rate^LN||16|/min\n"
        )
        events = self.adapter.parse(raw)
        # Should parse at least the valid OBX segments
        self.assertGreaterEqual(len(events), 2)

    def test_obx_without_obr(self):
        """OBX segments without a preceding OBR should still parse."""
        raw = (
            "MSH|^~\\&|SENDER||\n"
            "PID|1||PAT001\n"
            "OBX|1|NM|8867-4^Heart Rate^LN||88|bpm\n"
        )
        events = self.adapter.parse(raw)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].value_numeric, 88.0)


class TestJSONAdapter(unittest.TestCase):
    """Test JSON webhook parsing."""

    def setUp(self):
        self.adapter = JSONAdapter()
        self.sample_path = FIXTURES_DIR / "sample.json"

    def test_can_handle_json(self):
        self.assertTrue(self.adapter.can_handle('{"key": "value"}'))
        self.assertFalse(self.adapter.can_handle("MSH|^~\\&|"))

    def test_parse_sample_file(self):
        self.assertTrue(
            self.sample_path.exists(),
            f"Required fixture missing: {self.sample_path} — the JSON/FHIR "
            "ingestion path (partner-hospital onboarding) must be exercised, "
            "not skipped.",
        )

        raw = self.sample_path.read_text()
        events = self.adapter.parse(raw)

        # 6 vitals + 1 lab (Hemoglobin)
        self.assertEqual(len(events), 7)

    def test_vitals_vs_labs_classification(self):
        self.assertTrue(
            self.sample_path.exists(),
            f"Required fixture missing: {self.sample_path} — the JSON/FHIR "
            "ingestion path (partner-hospital onboarding) must be exercised, "
            "not skipped.",
        )

        raw = self.sample_path.read_text()
        events = self.adapter.parse(raw)

        vitals = [e for e in events if e.observation_type == ObservationType.VITAL]
        labs = [e for e in events if e.observation_type == ObservationType.LAB]

        self.assertEqual(len(vitals), 6)
        self.assertEqual(len(labs), 1)
        self.assertEqual(labs[0].observation_name, "Hemoglobin")

    def test_missing_patient_id(self):
        raw = '{"observations": [{"name": "HR", "value": 80}]}'
        with self.assertRaises(ParseError):
            self.adapter.parse(raw)

    def test_empty_observations(self):
        raw = '{"patient_id": "PAT001", "observations": []}'
        with self.assertRaises(ParseError):
            self.adapter.parse(raw)

    def test_invalid_json(self):
        with self.assertRaises(ParseError):
            self.adapter.parse("not valid json {{{")

    def test_numeric_value_parsing(self):
        raw = '{"patient_id": "PAT001", "observations": [{"name": "Heart Rate", "value": 92, "unit": "bpm"}]}'
        events = self.adapter.parse(raw)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].value_numeric, 92.0)
        self.assertEqual(events[0].value_type, ValueType.NUMERIC)

    def test_string_numeric_value(self):
        raw = '{"patient_id": "PAT001", "observations": [{"name": "Heart Rate", "value": "88", "unit": "bpm"}]}'
        events = self.adapter.parse(raw)
        self.assertEqual(events[0].value_numeric, 88.0)


class TestCSVAdapter(unittest.TestCase):
    """Test CSV replay parsing."""

    def setUp(self):
        self.adapter = CSVAdapter()
        self.sample_path = FIXTURES_DIR / "sample.csv"

    def test_can_handle_csv(self):
        sample = "patient_id,name,code,value,unit\nPAT001,HR,8867-4,80,bpm\n"
        self.assertTrue(self.adapter.can_handle(sample))
        self.assertFalse(self.adapter.can_handle("MSH|^~\\&|"))

    def test_parse_sample_file(self):
        self.assertTrue(
            self.sample_path.exists(),
            f"Required fixture missing: {self.sample_path} — the CSV replay "
            "ingestion path (partner-hospital onboarding) must be exercised, "
            "not skipped.",
        )

        raw = self.sample_path.read_text()
        events = self.adapter.parse(raw)

        # 14 rows in the CSV
        self.assertEqual(len(events), 14)

    def test_source_type(self):
        self.assertTrue(
            self.sample_path.exists(),
            f"Required fixture missing: {self.sample_path} — the CSV replay "
            "ingestion path (partner-hospital onboarding) must be exercised, "
            "not skipped.",
        )

        raw = self.sample_path.read_text()
        events = self.adapter.parse(raw)

        for evt in events:
            self.assertEqual(evt.source_type, "csv_replay")

    def test_vital_mapping_in_csv(self):
        raw = "patient_id,name,code,value,unit\nPAT001,Heart Rate,8867-4,75,bpm\n"
        events = self.adapter.parse(raw)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].mapped_vital_key, "HR")
        self.assertEqual(events[0].observation_type, ObservationType.VITAL)


class TestSourceLabeling(unittest.TestCase):
    """Verify source labels are correctly applied."""

    def test_hl7_source_type(self):
        adapter = HL7Adapter()
        self.assertEqual(adapter.source_type, "hl7")

    def test_json_source_type(self):
        adapter = JSONAdapter()
        self.assertEqual(adapter.source_type, "json")

    def test_csv_source_type(self):
        adapter = CSVAdapter()
        self.assertEqual(adapter.source_type, "csv_replay")


# ═══════════════════════════════════════════════════════════════════════════
#  THE VITALS WRITE — supplemental oxygen must survive an ingested row
# ═══════════════════════════════════════════════════════════════════════════
#
# THE DEFECT (fixed 2026-08-14). `_persist_vitals_row` INSERTed without
# `on_oxygen`, so every ingested row took the column default of 0. The same
# function reads the patient's LATEST row to decide the oxygen state of the
# CURRENT event, so the value decayed:
#
#     nurse charts "on O2"  → on_oxygen = 1     NEWS2 includes +2
#     monitor sends HR      → on_oxygen = 0     +2 GONE
#     monitor sends HR      → on_oxygen = 0     ... and never comes back
#
# NEWS2 under-scored every oxygen-dependent patient by 2 points from the first
# ingested reading onward — and oxygen dependence is the single best marker of
# the population that deteriorates. It was invisible on the dev box because
# ORB_INGESTION_MODE=simulation routes through bot_service, which DOES write the
# column. The pilot runs this lane.
#
# These tests go at `_persist_vitals_row` rather than the HTTP surface on
# purpose: the carry-forward SELECT and the INSERT are one read-modify-write
# inside a single session, and that pairing IS the invariant.

MRN_O2 = "MRN-INGEST-O2"
PID_O2 = "PAT-INGEST-O2"


def _vital_event(key: str, name: str, value: float, observed_at: str):
    return CanonicalEvent(
        patient_identifier=MRN_O2,
        observation_name=name,
        value=str(value),
        observation_type=ObservationType.VITAL,
        value_type=ValueType.NUMERIC,
        value_numeric=value,
        observed_at=observed_at,
        mapped_vital_key=key,
        source_type="hl7",
    )


@pytest.fixture
def oxygen_patient():
    """A patient a nurse has charted as oxygen-dependent, with nothing else."""
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, status) VALUES (?, ?, ?, ?)",
            (PID_O2, "Ingest O2 Test", MRN_O2, "admitted"),
        )
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (PID_O2,))
        # The nurse's observation set: hypoxic ON supplemental oxygen, alert.
        db.execute(
            """INSERT INTO vitals
                   (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                    spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                    source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (PID_O2, "2026-08-14T08:00:00+00:00", 88.0, 118.0, 74.0, 93.0, 37.0,
             18.0, "alert", 1, "nurse_observation"),
        )
    yield PID_O2
    with db_session() as db:
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (PID_O2,))


def _latest_row(patient_id: str):
    with db_session() as db:
        return db.execute(
            "SELECT * FROM vitals WHERE patient_id = ? ORDER BY id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()


class TestIngestedRowCarriesSupplementalOxygen:
    """The oxygen point must survive contact with a monitor feed."""

    def test_ingested_row_persists_the_carried_oxygen_flag(self, oxygen_patient):
        """The write itself: the new row must STORE on_oxygen, not default it to 0.

        Storing is what makes the flag survive; a value only carried in memory
        for one score is gone by the next reading.
        """
        _persist_vitals_row(
            MRN_O2,
            [_vital_event("HR", "Heart Rate", 96.0, "2026-08-14T08:15:00+00:00")],
            event_id=None,
            source_label="ingested_hl7",
        )

        row = _latest_row(oxygen_patient)
        assert row["source"] == "ingested_hl7", "fixture row was picked up, not the ingested one"
        assert row["on_oxygen"] == 1, (
            "the ingested row dropped supplemental oxygen — the patient is still "
            "on O2, and NEWS2 just lost 2 points"
        )

    def test_the_oxygen_point_is_still_in_the_score_after_ingestion(self, oxygen_patient):
        """The clinical consequence, asserted in NEWS2 points rather than columns.

        RR 18 (0) + SpO2 93 (2) + O2 (+2) + SBP 118 (0) + HR 96 (1) + T 37.0 (0)
        + alert (0) = 5 → 'elevated'. Without the oxygen point it is 3 → 'stable',
        and nobody is called.

        Scored on the SECOND ingested row, deliberately. The first one still
        reads the nurse's charted row and looks fine even with the defect
        present — that is exactly why this went unnoticed. The lie starts one
        reading later, and then never stops.
        """
        _persist_vitals_row(
            MRN_O2,
            [_vital_event("HR", "Heart Rate", 96.0, "2026-08-14T08:15:00+00:00")],
            event_id=None,
            source_label="ingested_hl7",
        )
        result = _persist_vitals_row(
            MRN_O2,
            [_vital_event("HR", "Heart Rate", 96.0, "2026-08-14T08:30:00+00:00")],
            event_id=None,
            source_label="ingested_hl7",
        )
        patient_id, vitals_map, _observed, consciousness, on_oxygen, on_scale_2 = result
        assert on_oxygen is True, "carry-forward lost the oxygen state"

        scored = compute_news2(
            vitals_map["HR"], vitals_map["SBP"], vitals_map["RR"],
            vitals_map["Temp"], vitals_map["SpO2"], consciousness, on_oxygen,
            on_scale_2=on_scale_2,
        )
        assert scored["raw_news2"] == 5
        assert scored["level"] == "elevated"
        assert any("oxygen" in d.lower() for d in scored["drivers"]), (
            "the supplemental-oxygen driver vanished from the explanation"
        )

    def test_oxygen_survives_a_whole_run_of_ingested_rows(self, oxygen_patient):
        """The decay case. One ingested row hid the bug badly; a RUN of them is
        how a real monitor behaves, and it is where the value used to reach 0 and
        stay there."""
        # 08:60 is not a time: it used to be stored at receipt (today) and
        # still inherit the August oxygen flag. With carry-forward age-limited
        # (2026-10-03) a 50-day-old assessment is rightly not carried, so the
        # run is now four real quarter-hours.
        for hhmm in ("08:15", "08:30", "08:45", "09:00"):
            _persist_vitals_row(
                MRN_O2,
                [_vital_event("HR", "Heart Rate", 96.0,
                              f"2026-08-14T{hhmm}:00+00:00")],
                event_id=None,
                source_label="ingested_hl7",
            )

        with db_session() as db:
            flags = [r["on_oxygen"] for r in db.execute(
                "SELECT on_oxygen FROM vitals WHERE patient_id = ? AND source = ? ORDER BY id",
                (PID_O2, "ingested_hl7"),
            ).fetchall()]
        assert flags == [1, 1, 1, 1], (
            f"supplemental oxygen decayed across a run of ingested rows: {flags}"
        )

    def test_a_charted_room_air_state_is_carried_as_a_recorded_false(self, oxygen_patient):
        """Carry-forward must not be one-way. A patient weaned off oxygen has a
        charted `on_oxygen = 0`, and that is a RECORDED FALSE — the next ingested
        row must inherit it and must NOT re-add the +2."""
        with db_session() as db:
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, spo2,
                                       consciousness, on_oxygen, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (PID_O2, "2026-08-14T09:00:00+00:00", 84.0, 97.0, "alert", 0,
                 "nurse_observation"),
            )

        _, _, _, _, on_oxygen, _ = _persist_vitals_row(
            MRN_O2,
            [_vital_event("HR", "Heart Rate", 84.0, "2026-08-14T09:15:00+00:00")],
            event_id=None,
            source_label="ingested_hl7",
        )
        assert on_oxygen is False
        assert _latest_row(PID_O2)["on_oxygen"] == 0


class TestIngestedRowIsHonestAboutWhatWasNeverAssessed:
    """A machine cannot assess ACVPU or read an oxygen mask. Say so."""

    def test_a_patient_with_no_history_reports_both_human_parameters_missing(self):
        pid, mrn = "PAT-INGEST-NEW", "MRN-INGEST-NEW"
        with db_session() as db:
            db.execute(
                "INSERT OR IGNORE INTO patients (id, name, mrn, status) VALUES (?, ?, ?, ?)",
                (pid, "Ingest Fresh Test", mrn, "admitted"),
            )
            db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        try:
            evt = CanonicalEvent(
                patient_identifier=mrn, observation_name="Heart Rate", value="96",
                observation_type=ObservationType.VITAL, value_type=ValueType.NUMERIC,
                value_numeric=96.0, observed_at="2026-08-14T08:15:00+00:00",
                mapped_vital_key="HR", source_type="hl7",
            )
            _, vitals_map, _, consciousness, on_oxygen, on_scale_2 = _persist_vitals_row(
                mrn, [evt], event_id=None, source_label="ingested_hl7",
            )
            # Never assessed is None, NOT a manufactured 'alert' / 'on air'.
            assert consciousness is None
            assert on_oxygen is None

            scored = compute_news2(
                vitals_map["HR"], vitals_map["SBP"], vitals_map["RR"],
                vitals_map["Temp"], vitals_map["SpO2"], consciousness, on_oxygen,
                on_scale_2=on_scale_2,
            )
            assert scored["complete"] is False
            assert "Consciousness (ACVPU)" in scored["missing_parameters"]
            assert "Supplemental O2" in scored["missing_parameters"]

            # And the stored row keeps the absence rather than inventing a
            # documented-normal finding for the NEXT row to carry forward.
            row = _latest_row(pid)
            assert row["consciousness"] is None
            assert row["on_oxygen"] is None
        finally:
            with db_session() as db:
                db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))


class TestIngestedRowUsesThePatientsSpO2Scale:
    """COPD patients are scored on the RCP Scale 2 table, in this lane too."""

    def test_a_scale_2_patient_is_not_scored_on_scale_1(self):
        pid, mrn = "PAT-INGEST-COPD", "MRN-INGEST-COPD"
        with db_session() as db:
            db.execute(
                "INSERT OR IGNORE INTO patients (id, name, mrn, status) VALUES (?, ?, ?, ?)",
                (pid, "Ingest COPD Test", mrn, "admitted"),
            )
            db.execute("UPDATE patients SET on_scale_2 = 1 WHERE id = ?", (pid,))
            db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        try:
            evt = CanonicalEvent(
                patient_identifier=mrn, observation_name="SpO2", value="90",
                observation_type=ObservationType.VITAL, value_type=ValueType.NUMERIC,
                value_numeric=90.0, observed_at="2026-08-14T08:15:00+00:00",
                mapped_vital_key="SpO2", source_type="hl7",
            )
            _, vitals_map, _, consciousness, on_oxygen, on_scale_2 = _persist_vitals_row(
                mrn, [evt], event_id=None, source_label="ingested_hl7",
            )
            assert on_scale_2 is True, (
                "the ingestion lane ignored patients.on_scale_2 — a COPD patient "
                "at their normal 90% collects a false 3-point escalation on every "
                "ingested reading"
            )
            scored = compute_news2(
                vitals_map["HR"], vitals_map["SBP"], vitals_map["RR"],
                vitals_map["Temp"], vitals_map["SpO2"], consciousness, on_oxygen,
                on_scale_2=on_scale_2,
            )
            assert scored["spo2_scale"] == 2
            assert scored["single_param_3"] is False
        finally:
            with db_session() as db:
                db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
