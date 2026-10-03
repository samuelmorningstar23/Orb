"""Orb — vitals sources: HL7 v2 parsing, MLLP framing, provenance, and hostile devices.

WORKSTREAM 2 (monitor integration) is a promise to the hospital, and the thing
that makes it survivable is that a bedside monitor is assumed to misbehave. Bad
input from a device is NORMAL: this file's centre of gravity is therefore the
malformed, hostile and out-of-order cases, not the happy path.

What is asserted here, and why each one is a clinical-safety property:

  * encoding characters / repeats / escapes / timezone offsets are parsed from
    the message rather than assumed — a mis-read offset back-dates a reading and
    reorders it against every other feed;
  * MLLP framing is bounded and resynchronising — a truncated or garbage stream
    must not eat the message behind it, and must not grow memory without limit;
  * a device that floods is NACKed (`AE`) rather than silently dropped, so the
    reading is retried instead of lost;
  * a reading that is older than one we already hold is stored but does not
    drive the live risk score or an alert;
  * every reading carries a provenance label that says whether a machine
    measured it or a human typed it;
  * NOTHING a device can send reaches the ingestion loop as an exception.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ingestion_adapters import HL7Adapter, ObservationType, ParseError
from services.ingestion_service import _SOURCE_LABELS
from services.vitals_source import (
    HL7MonitorSource,
    IngestionSink,
    LABEL_ADMISSION_VITALS,
    LABEL_MANUAL_ENTRY,
    LABEL_MONITOR_HL7,
    LABEL_SIMULATED,
    ManualEntrySource,
    RecordingSink,
    SimulatorSource,
    SourceKind,
    SourceStats,
    VitalsReading,
    VitalsSourceRegistry,
    classify_source_label,
    configure_from_env,
    is_machine_measured,
)
from services.vitals_source import hl7v2, mllp
from services.vitals_source.base import ReadingHint
from services.vitals_source.bench_simulator import BedState, BenchMonitor, build_adt, build_oru

FIXTURES = Path(__file__).resolve().parent.parent / "scripts" / "fixtures"


# ══════════════════════════════════════════════════════════════════════════════
#  HL7 v2 parser — the bits `split("|")` gets wrong
# ══════════════════════════════════════════════════════════════════════════════

SIMPLE_ORU = (
    "MSH|^~\\&|ICU_MONITOR|HOSPITAL_A|ORB|ORB_OS|20260801143025||ORU^R01|MSG42|P|2.5\r"
    "PID|1||PAT001^^^HOSPITAL_A^MR~LOCAL9^^^IX^PI||Patel^Raj\r"
    "OBR|1|ORD1|LAB1|85354-9^Vital Signs^LN|||20260801143000\r"
    "OBX|1|NM|8867-4^Heart Rate^LN||112|bpm|60-100|H|||F|||20260801143000\r"
)


class TestHl7Parser:
    def test_msh_field_numbering_is_hl7s_not_the_list_index(self):
        """MSH-1 is the separator and MSH-2 the encoding chars, so every later
        MSH field sits one physical position earlier than in any other segment.
        Off-by-one here silently reads the wrong field on our only message type."""
        msg = hl7v2.parse(SIMPLE_ORU)
        assert msg.msh.field(1) == "|"
        assert msg.msh.field(2) == "^~\\&"
        assert msg.sending_application == "ICU_MONITOR"
        assert msg.sending_facility == "HOSPITAL_A"
        assert msg.message_type == "ORU"
        assert msg.trigger_event == "R01"
        assert msg.control_id == "MSG42"
        assert msg.version_id == "2.5"

    def test_non_msh_field_numbering(self):
        msg = hl7v2.parse(SIMPLE_ORU)
        pid = msg.first("PID")
        assert pid.field(1) == "1"
        assert pid.field(3, component=1) == "PAT001"

    def test_repetition_separator_is_honoured(self):
        """PID-3 is an identifier LIST. A monitor sends the MRN AND its own local
        id; naive splitting would return `PAT001^^^HOSPITAL_A^MR~LOCAL9...`."""
        pid = hl7v2.parse(SIMPLE_ORU).first("PID")
        reps = pid.repeats(3)
        assert len(reps) == 2
        assert pid.field(3, component=1) == "PAT001"
        assert pid.field(3, repeat=1, component=1) == "LOCAL9"
        assert pid.field(3, repeat=1, component=5) == "PI"

    def test_subcomponents(self):
        raw = SIMPLE_ORU.replace("Patel^Raj", "Patel&Senior^Raj")
        pid = hl7v2.parse(raw).first("PID")
        assert pid.field(5, component=1, subcomponent=1) == "Patel"
        assert pid.field(5, component=1, subcomponent=2) == "Senior"

    def test_escape_sequences_decoded(self):
        raw = SIMPLE_ORU.replace("Heart Rate", "Heart Rate \\S\\ ECG \\T\\ lead \\F\\ II")
        obx = hl7v2.parse(raw).all("OBX")[0]
        assert obx.field(3, component=2) == "Heart Rate ^ ECG & lead | II"

    def test_hex_and_break_escapes(self):
        assert hl7v2.unescape("a\\X0A\\b", hl7v2.EncodingCharacters()) == "a\nb"
        assert hl7v2.unescape("a\\.br\\b", hl7v2.EncodingCharacters()) == "a\nb"

    def test_unknown_escape_is_preserved_not_dropped(self):
        """Losing characters from a clinical field is worse than showing an
        escape sequence a human can recognise."""
        assert hl7v2.unescape("dose \\Zx\\ 5mg", hl7v2.EncodingCharacters()) == "dose \\Zx\\ 5mg"

    def test_non_default_encoding_characters(self):
        raw = (
            "MSH!#~\\&!OEM!SITE!ORB!ORB_OS!20260801143025!!ORU#R01!X1!P!2.3\r"
            "PID!1!!PAT7#extra\r"
            "OBX!1!NM!8867-4#Heart Rate#LN!!99!bpm\r"
        )
        msg = hl7v2.parse(raw)
        assert msg.encoding.field == "!"
        assert msg.encoding.component == "#"
        assert msg.message_type == "ORU"
        assert msg.first("PID").field(3, component=1) == "PAT7"
        assert msg.all("OBX")[0].field(3, component=2) == "Heart Rate"

    def test_timezone_offset_is_applied_not_stripped(self):
        """`20260801143000+0530` is 09:00 UTC. Discarding the offset back-dates
        the reading by 5.5 h and reorders it against every other feed.

        The offset-LESS cases below now say `default_tz=timezone.utc` out loud.
        They used to rely on the parser silently assuming UTC, which was the
        patient-safety defect: an offset-less DTM is legal HL7 and means the
        device's own local wall clock, so in a UTC+05:30 hospital that
        assumption stamped every reading 5.5 hours in the future. The asserted
        VALUES are unchanged — only the assumption is now stated instead of
        inherited. `TestOffsetLessTimestampsUseTheApplianceZone` asserts what
        happens when nobody states it."""
        assert hl7v2.parse_timestamp("20260801143000+0530") == "2026-08-01T09:00:00+00:00"
        assert hl7v2.parse_timestamp("20260801090000-0500") == "2026-08-01T14:00:00+00:00"
        assert hl7v2.parse_timestamp(
            "20260801143000.250", default_tz=timezone.utc) == "2026-08-01T14:30:00+00:00"
        assert hl7v2.parse_timestamp(
            "20260801", default_tz=timezone.utc) == "2026-08-01T00:00:00+00:00"
        assert hl7v2.parse_timestamp("NOT-A-TIME") is None
        assert hl7v2.parse_timestamp("") is None

    def test_segments_split_on_cr_not_only_newline(self):
        """The wire uses \\r. A parser that needs \\n works on fixture files and
        fails on every real device."""
        assert len(hl7v2.parse(SIMPLE_ORU).segments) == 4

    def test_junk_lines_skipped_and_z_segments_kept(self):
        raw = SIMPLE_ORU + "-- vendor debug output --\rZQA|1|proprietary\r"
        msg = hl7v2.parse(raw)
        assert msg.first("ZQA") is not None
        assert msg.skipped_lines and "vendor debug" in msg.skipped_lines[0]

    @pytest.mark.parametrize("payload", [
        "", "   ", "not hl7 at all", "MS", "MSH", "\x00\x01\x02",
        "PID|1||PAT001", "{\"json\": true}",
    ])
    def test_garbage_raises_only_the_declared_error(self, payload):
        with pytest.raises(hl7v2.Hl7ParseError):
            hl7v2.parse(payload)

    def test_short_segments_do_not_raise(self):
        msg = hl7v2.parse("MSH|^~\\&|A\rOBX|1\r")
        assert msg.all("OBX")[0].field(14) == ""
        assert msg.all("OBX")[0].field(3, component=9) == ""


class TestOffsetLessTimestampsUseTheApplianceZone:
    """BLOCKING 2. An HL7 DTM with no UTC offset is LEGAL, and it is what most
    bedside monitors emit — they print their own local wall-clock time.

    Reading one as UTC is only correct in a hospital at Greenwich. THIS HOSPITAL
    IS IN INDIA (UTC+05:30), so the old behaviour stamped every offset-less
    reading 5.5 hours in the FUTURE: it reordered against every other feed, and
    combined with any wall-clock liveness rule it muted the patient outright.

    IST is used throughout because it is the pilot's real zone AND because it
    has no DST — which makes it the easy case. The parser resolves the offset
    per-date via `zoneinfo`, so a site that DOES observe DST is handled too.
    """

    IST = "Asia/Kolkata"

    def test_offset_less_dtm_is_read_as_local_wall_clock_not_utc(self):
        got = hl7v2.parse_timestamp("20260802003406", default_tz=self.IST)
        # 00:34:06 IST on 2 Aug == 19:04:06 UTC on 1 Aug.
        assert got == "2026-08-01T19:04:06+00:00"
        # The pre-fix answer, kept here so the regression is unmistakable.
        assert got != "2026-08-02T00:34:06+00:00"

    def test_ist_has_no_dst_so_the_offset_is_five_thirty_all_year(self):
        for date, expected in (
            ("20260115120000", "2026-01-15T06:30:00+00:00"),   # winter
            ("20260715120000", "2026-07-15T06:30:00+00:00"),   # monsoon
            ("20261101120000", "2026-11-01T06:30:00+00:00"),   # post-monsoon
        ):
            assert hl7v2.parse_timestamp(date, default_tz=self.IST) == expected

    def test_an_explicit_offset_always_wins_over_the_assumed_zone(self):
        """A device that states its offset is telling us the truth; the assumed
        zone is only a fallback for devices that stay silent."""
        assert hl7v2.parse_timestamp(
            "20260801143000+0000", default_tz=self.IST) == "2026-08-01T14:30:00+00:00"
        assert hl7v2.parse_timestamp(
            "20260801143000+0530", default_tz=self.IST) == "2026-08-01T09:00:00+00:00"
        assert hl7v2.parse_timestamp(
            "20260801143000-0500", default_tz=self.IST) == "2026-08-01T19:30:00+00:00"

    def test_detail_reports_whether_the_zone_was_stated_or_assumed(self):
        """Anything reasoning about clock skew has to distinguish 'your clock is
        wrong' from 'you never said which zone you were in'."""
        stated = hl7v2.parse_timestamp_detail("20260801143000+0530", default_tz=self.IST)
        assumed = hl7v2.parse_timestamp_detail("20260801143000", default_tz=self.IST)
        assert stated.had_offset is True
        assert assumed.had_offset is False
        assert assumed.assumed_timezone == self.IST
        assert assumed.precision == "second"

    def test_partial_precision_dtms_still_land_in_the_right_zone(self):
        assert hl7v2.parse_timestamp("202608020034", default_tz=self.IST) \
            == "2026-08-01T19:04:00+00:00"
        assert hl7v2.parse_timestamp("2026080200", default_tz=self.IST) \
            == "2026-08-01T18:30:00+00:00"
        assert hl7v2.parse_timestamp("20260802", default_tz=self.IST) \
            == "2026-08-01T18:30:00+00:00"

    def test_the_default_zone_is_configurable_and_defaults_to_the_appliance(self, monkeypatch):
        """The appliance's own zone is the right default because it sits on the
        same ward as the monitors. It must be overridable for the case where a
        gateway is configured differently."""
        monkeypatch.setenv(hl7v2.TZ_ENV_VAR, self.IST)
        hl7v2.set_default_timezone(None)          # re-read the environment
        try:
            assert hl7v2.parse_timestamp("20260802003406") == "2026-08-01T19:04:06+00:00"
            assert str(getattr(hl7v2.appliance_timezone(), "key", "")) == self.IST
        finally:
            hl7v2.set_default_timezone(None)

    def test_a_garbage_timezone_degrades_to_the_appliance_zone_and_never_raises(self):
        """A typo in a config file must not stop a ward's monitor feed parsing
        timestamps."""
        assert hl7v2.resolve_timezone("Mars/Olympus_Mons") is None
        assert hl7v2.resolve_timezone("") is None
        assert hl7v2.parse_timestamp("20260802003406", default_tz="Mars/Olympus_Mons") \
            is not None

    def test_fixed_offset_and_utc_spellings_are_accepted(self):
        assert hl7v2.parse_timestamp("20260802003406", default_tz="+05:30") \
            == "2026-08-01T19:04:06+00:00"
        assert hl7v2.parse_timestamp("20260802003406", default_tz="+0530") \
            == "2026-08-01T19:04:06+00:00"
        assert hl7v2.parse_timestamp("20260802003406", default_tz="UTC") \
            == "2026-08-02T00:34:06+00:00"

    def test_the_http_hl7_feed_gets_the_same_answer_as_the_socket_feed(self):
        """One HL7 message must mean the same thing over HTTP and over MLLP —
        two feeds disagreeing about when a reading happened is the same defect
        wearing a different hat."""
        from services.ingestion_adapters import parse_hl7_timestamp
        assert parse_hl7_timestamp("20260802003406", default_timezone=self.IST) \
            == hl7v2.parse_timestamp("20260802003406", default_tz=self.IST)

    def test_adapter_reads_an_offset_less_monitor_message_in_the_device_zone(self):
        raw = (
            "MSH|^~\\&|ICU_MONITOR|HOSPITAL_A|ORB|ORB_OS|20260802003406||ORU^R01|TZ1|P|2.5\r"
            "PID|1||PAT001^^^HOSPITAL_A^MR||Patel^Raj\r"
            "OBR|1|O1|O1|85354-9^Vital Signs^LN|||20260802003406\r"
            "OBX|1|NM|8867-4^Heart Rate^LN||112|bpm|60-100|H|||F|||20260802003406\r"
        )
        events = HL7Adapter(default_timezone=self.IST).parse(raw)
        assert events[0].observed_at == "2026-08-01T19:04:06+00:00"

    def test_our_own_acks_state_their_offset_rather_than_leaving_it_to_be_guessed(self):
        """We spend this module arguing an offset-less DTM is a guess the
        receiver should not have to make. Emitting one ourselves would be the
        same defect pointed outward."""
        ack = hl7v2.parse(hl7v2.build_ack(hl7v2.parse(SIMPLE_ORU), hl7v2.ACK_ACCEPT))
        assert ack.message_datetime.endswith("+0000")


class TestAckBuilder:
    def test_ack_echoes_control_id_and_code(self):
        msg = hl7v2.parse(SIMPLE_ORU)
        ack = hl7v2.parse(hl7v2.build_ack(msg, hl7v2.ACK_ACCEPT))
        assert ack.message_type == "ACK"
        msa = ack.first("MSA")
        assert msa.field(1) == "AA"
        assert msa.field(2) == "MSG42"

    def test_ack_is_buildable_for_an_unparseable_message(self):
        """A sender with no reply blocks on its own ACK timeout and stops sending
        vitals entirely — so even garbage gets an answer."""
        ack = hl7v2.parse(hl7v2.build_ack(None, hl7v2.ACK_REJECT, "unparseable"))
        assert ack.first("MSA").field(1) == "AR"

    def test_error_text_cannot_inject_segments_or_fields(self):
        """The NACK text is built from device input; unsanitised it could append
        fields or whole segments to our own reply."""
        ack_text = hl7v2.build_ack(None, hl7v2.ACK_ERROR, "bad\rMSA|AA|forged|x")
        parsed = hl7v2.parse(ack_text)
        assert len(parsed.all("MSA")) == 1
        assert parsed.first("MSA").field(1) == "AE"


# ══════════════════════════════════════════════════════════════════════════════
#  MLLP framing — TCP gives bytes, not messages
# ══════════════════════════════════════════════════════════════════════════════

class TestMllpFramer:
    def test_single_message(self):
        f = mllp.MllpFramer()
        assert f.feed(mllp.frame(b"hello")) == [b"hello"]

    def test_message_split_across_many_chunks(self):
        f = mllp.MllpFramer()
        data = mllp.frame(b"MSH|one|two|three")
        out = []
        for i in range(len(data)):
            out.extend(f.feed(data[i:i + 1]))
        assert out == [b"MSH|one|two|three"]

    def test_multiple_messages_in_one_chunk(self):
        f = mllp.MllpFramer()
        blob = mllp.frame(b"one") + mllp.frame(b"two") + mllp.frame(b"three")
        assert f.feed(blob) == [b"one", b"two", b"three"]

    def test_garbage_before_start_block_discarded_and_counted(self):
        f = mllp.MllpFramer()
        assert f.feed(b"\x00 line noise \xff" + mllp.frame(b"good")) == [b"good"]
        assert f.stats.bytes_discarded_before_start > 0
        assert f.stats.resyncs == 1

    def test_truncated_message_then_a_good_one_resyncs(self):
        """Device rebooted mid-send. The partial must be abandoned WITHOUT
        eating the message behind it."""
        f = mllp.MllpFramer()
        out = f.feed(b"\x0bMSH|half a mess" + mllp.frame(b"complete"))
        assert out == [b"complete"]

    def test_oversize_block_is_abandoned_and_stream_recovers(self):
        f = mllp.MllpFramer(max_message_bytes=512)
        f.feed(b"\x0b" + b"A" * 2000)
        assert f.stats.oversize_messages == 1
        assert f.pending_bytes == 0
        assert f.feed(mllp.frame(b"after")) == [b"after"]

    def test_buffer_cannot_grow_without_limit(self):
        f = mllp.MllpFramer(max_message_bytes=1024)
        for _ in range(50):
            f.feed(b"\x0b" + b"B" * 900)
        assert f.pending_bytes <= 1024 + 900

    def test_end_block_without_trailing_cr(self):
        f = mllp.MllpFramer()
        assert f.feed(b"\x0bpayload\x1c") == [b"payload"]

    def test_never_raises_on_arbitrary_bytes(self):
        f = mllp.MllpFramer()
        blob = bytes(range(256)) * 8
        f.feed(blob)          # must not raise
        f.feed(b"")
        f.reset()


# ══════════════════════════════════════════════════════════════════════════════
#  The shared HL7 adapter, driven by real-device-shaped messages
# ══════════════════════════════════════════════════════════════════════════════

class TestHL7AdapterOnMonitorMessages:
    def test_monitor_fixture_parses_with_offsets_units_and_escapes(self):
        raw = (FIXTURES / "monitor_oru.hl7").read_text()
        events = HL7Adapter().parse(raw)

        assert len(events) == 7  # 6 vitals + 1 unmapped (MAP)
        vitals = {e.mapped_vital_key: e for e in events if e.mapped_vital_key}
        assert set(vitals) == {"HR", "SBP", "DBP", "SpO2", "Temp", "RR"}

        # PID-3 carried two repetitions; the first identifier wins.
        assert all(e.patient_identifier == "PAT001" for e in events)
        # PV1-19 encounter number survives the trailing-empty-field run.
        assert events[0].encounter_identifier == "ENC-99117"
        # 101.5 °F normalised to Celsius before it can reach NEWS2.
        assert vitals["Temp"].value_numeric == pytest.approx(38.6, abs=0.05)
        assert vitals["Temp"].unit == "°C"
        # +0530 applied, not discarded.
        assert vitals["HR"].observed_at == "2026-08-01T09:00:00+00:00"
        # `\S\` in the OBR panel name decoded rather than left as an escape.
        assert "^" in events[0].panel_name

    def test_hostile_fixture_yields_only_trustworthy_values_and_never_raises(self):
        raw = (FIXTURES / "monitor_hostile.hl7").read_text()
        events = HL7Adapter().parse(raw)

        by_key = {}
        for e in events:
            if e.mapped_vital_key:
                by_key.setdefault(e.mapped_vital_key, []).append(e)

        # "NaN" is not a number the pipeline may act on.
        assert all(e.value_numeric is None for e in by_key["HR"] if e.value == "NaN")
        # SpO2 = 900 % and HR = -17 bpm are impossible; they must not reach NEWS2.
        assert all(e.value_numeric is None for e in by_key["SpO2"])
        assert all(e.value_numeric is None for e in by_key["HR"])
        # The one good reading in the message still lands.
        assert by_key["Temp"][0].value_numeric == pytest.approx(38.6)

    def test_legacy_sample_fixture_is_unchanged_by_the_new_parser(self):
        """Behaviour-preservation guard for the pre-existing HTTP HL7 feed."""
        events = HL7Adapter().parse((FIXTURES / "sample.hl7").read_text())
        assert len(events) == 11
        assert len([e for e in events if e.observation_type == ObservationType.VITAL]) == 6
        assert len({e.obr_group_id for e in events}) == 2

    def test_bench_simulator_output_is_parseable_by_the_adapter(self):
        """The bench simulator is the DEVICE side. If Orb cannot parse what it
        emits, the Week-6 bench test proves nothing."""
        bed = BedState(patient_id="PAT001")
        bed.step(__import__("random").Random(7))
        events = HL7Adapter().parse(build_oru(bed, use_escapes=True))
        assert len({e.mapped_vital_key for e in events if e.mapped_vital_key}) == 6

    def test_message_with_no_patient_identifier_is_rejected(self):
        with pytest.raises(ParseError):
            HL7Adapter().parse("MSH|^~\\&|X\rOBX|1|NM|8867-4^HR^LN||80|bpm\r")


class TestBenchSimulatorRunsWithoutOrb:
    def test_cli_runs_with_no_orb_configuration_at_all(self):
        """The bench simulator is the DEVICE side. It gets run on a biomedical
        engineer's laptop next to a monitor, over a ward network, with no
        database, no key and no .env. If importing it pulled in
        ingestion_service → database → config it would die on a missing JWT
        secret, and the Week-6 bench test would need a full Orb install."""
        import subprocess

        server_root = str(Path(__file__).resolve().parent.parent)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORB_")}
        env["PATH"] = os.environ.get("PATH", "")
        env["PYTHONPATH"] = server_root
        proc = subprocess.run(
            [sys.executable, "-m", "services.vitals_source.bench_simulator",
             "--print", "--seed", "5"],
            cwd=server_root, env=env, capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("MSH|^~\\&|")
        assert "8867-4" in proc.stdout


# ══════════════════════════════════════════════════════════════════════════════
#  Provenance — can a reviewer tell a machine reading from a typed one?
# ══════════════════════════════════════════════════════════════════════════════

class TestProvenance:
    def test_the_three_peers_classify_distinctly(self):
        assert classify_source_label(LABEL_MONITOR_HL7) is SourceKind.MONITOR
        assert classify_source_label(LABEL_MANUAL_ENTRY) is SourceKind.MANUAL
        assert classify_source_label(LABEL_SIMULATED) is SourceKind.SIMULATOR

    def test_only_a_monitor_reading_counts_as_machine_measured(self):
        assert is_machine_measured(LABEL_MONITOR_HL7)
        for label in (LABEL_MANUAL_ENTRY, LABEL_SIMULATED, "clinician",
                      "llm_extracted", "ingested_hl7", None, "who_knows"):
            assert not is_machine_measured(label)

    def test_pre_existing_writers_are_all_classifiable(self):
        """These literals are written by admission_service, the Scribe extractor
        and the generic feeds. An unclassifiable one is a reading whose origin
        the scorecard cannot report."""
        assert classify_source_label("clinician") is SourceKind.MANUAL
        assert classify_source_label("llm_extracted") is SourceKind.DERIVED

    def test_nurse_typed_observations_classify_as_manual_not_unknown(self):
        """REGRESSION. nurse_shift_service stamps 'nurse_manual' on every
        nurse-charted set — the pilot's single most common vitals path — and
        until 2026-08-10 that literal was absent from _LABEL_KIND, so the
        ward's own observations classified UNKNOWN: the shadow scorecard could
        not say a human typed them."""
        from services.vitals_source import LABEL_NURSE_OBSERVATION
        assert LABEL_NURSE_OBSERVATION == "nurse_manual"
        assert classify_source_label("nurse_manual") is SourceKind.MANUAL
        assert not is_machine_measured("nurse_manual")

    def test_admission_typed_vitals_classify_as_manual_not_unknown(self):
        """The admission form's initial set is a human reading a device and
        typing it — the same kind of evidence as a nurse-charted set, and it
        must be attributable as such on the shadow scorecard."""
        assert LABEL_ADMISSION_VITALS == "clinician"
        assert classify_source_label(LABEL_ADMISSION_VITALS) is SourceKind.MANUAL
        assert not is_machine_measured(LABEL_ADMISSION_VITALS)

    def test_the_admission_writer_and_the_classifier_share_one_constant(self):
        """Same guard as the nurse path below: the label the admission seam
        stamps IS the constant the classifier maps, so the writer and
        `classify_source_label` cannot drift apart."""
        assert ManualEntrySource.admission_label == LABEL_ADMISSION_VITALS
        assert classify_source_label(ManualEntrySource.admission_label) is SourceKind.MANUAL

    def test_the_nurse_writer_and_the_classifier_share_one_constant(self):
        """OBS_SOURCE is defined AS the package's label so the writer and
        `classify_source_label` cannot drift apart a second time."""
        from services import nurse_shift_service
        from services.vitals_source import LABEL_NURSE_OBSERVATION
        assert nurse_shift_service.OBS_SOURCE == LABEL_NURSE_OBSERVATION
        assert ManualEntrySource.observation_label == LABEL_NURSE_OBSERVATION
        assert classify_source_label(nurse_shift_service.OBS_SOURCE) is SourceKind.MANUAL

    def test_every_ingestion_feed_label_is_classifiable(self):
        """Structural guard: adding an adapter to ingestion_service without
        giving its label a kind would create unattributable clinical data."""
        from services import nurse_shift_service
        from services.vitals_source.base import _LABEL_KIND
        for label in _SOURCE_LABELS.values():
            assert label in _LABEL_KIND, f"{label!r} has no SourceKind"
        # The direct writers outside ingestion_service's feed table. This is
        # the hole the 'nurse_manual' defect lived in: the guard only scanned
        # _SOURCE_LABELS, and a writer that never passed through an adapter was
        # invisible to it.
        for label in (nurse_shift_service.OBS_SOURCE, "clinician"):
            assert label in _LABEL_KIND, f"{label!r} has no SourceKind"

    def test_unknown_label_is_never_guessed(self):
        assert classify_source_label("some_new_thing") is SourceKind.UNKNOWN
        assert classify_source_label(None) is SourceKind.UNKNOWN

    def test_reading_drops_junk_values_but_keeps_absences_absent(self):
        """A vital the source did not measure must stay missing so carry-forward
        applies — writing 0 would fabricate an observation."""
        r = VitalsReading(
            patient_identifier="P1", observed_at="2026-08-01T10:00:00+00:00",
            values={"HR": 88, "SpO2": None, "Temp": "oops", "NOTAVITAL": 5},
        )
        assert r.values == {"HR": 88.0}
        payload = r.to_json_payload()
        assert [o["code"] for o in payload["observations"]] == ["8867-4"]


# ══════════════════════════════════════════════════════════════════════════════
#  Ordering — a replayed buffer must not overwrite the present
# ══════════════════════════════════════════════════════════════════════════════

def _iso(delta_minutes: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


class TestOrderingGuard:
    def test_fresh_reading_is_live(self):
        sink = IngestionSink()
        assert sink.is_backfill("mon", [ReadingHint("P1", _iso())]) is False

    def test_reading_older_than_one_already_seen_is_backfill(self):
        sink = IngestionSink()
        sink.is_backfill("mon", [ReadingHint("P1", _iso(-1))])
        assert sink.is_backfill("mon", [ReadingHint("P1", _iso(-5))]) is True

    def test_wall_clock_distance_alone_never_suppresses_alerting(self):
        """REPLACES `test_reading_older_than_the_live_window_is_backfill...`.

        That test asserted the defect: a reading whose device timestamp was
        further from now than the window was flagged `backfill`, which skips
        NEWS2, the Vigil alert bridge and the live emit. Proven over a real
        socket, a monitor whose clock was 20 minutes slow had 100 % of its
        HR 132 / SpO2 84 / RR 30 messages muted — Orb said nothing about a
        crashing patient because a CLOCK was wrong.

        The rule now: distance from our wall clock is evidence about the
        DEVICE'S CLOCK, never about whether the patient is deteriorating. It
        raises a counted fault and the reading stays live. This assertion is
        strictly stronger than the one it replaces — it pins the same input and
        additionally requires the skew to be measured and visible."""
        sink = IngestionSink(skew_tolerance_seconds=600)
        decision = sink.classify("mon", [ReadingHint("P1", _iso(-45), device_id="MON-A")])

        assert decision.backfill is False, "a wrong clock must not mute a patient"
        assert decision.skew_state == "skewed"
        assert decision.skew_seconds == pytest.approx(-45 * 60, abs=5)

        # Visible and countable, not a debug log line.
        report = sink.clock_skew_report("mon")
        assert report["state"] == "skewed"
        assert report["devices_skewed"] == 1
        assert report["devices"][0]["device_id"] == "MON-A"
        assert report["devices"][0]["faults"] == 1

    def test_high_water_is_per_source_and_per_patient(self):
        sink = IngestionSink()
        sink.is_backfill("monA", [ReadingHint("P1", _iso(-1))])
        assert sink.is_backfill("monB", [ReadingHint("P1", _iso(-3))]) is False
        assert sink.is_backfill("monA", [ReadingHint("P2", _iso(-3))]) is False

    def test_mixed_payload_is_treated_as_live(self):
        """Under-suppressing costs a redundant reading; over-suppressing hides a
        deterioration. Only one of those is survivable."""
        sink = IngestionSink()
        sink.is_backfill("mon", [ReadingHint("P1", _iso(-1))])
        assert sink.is_backfill("mon", [
            ReadingHint("P1", _iso(-9)), ReadingHint("P2", _iso()),
        ]) is False

    def test_undateable_reading_is_never_silently_muted(self):
        sink = IngestionSink()
        assert sink.is_backfill("mon", [ReadingHint("P1", None)]) is False
        assert sink.is_backfill("mon", [ReadingHint("P1", "not-a-date")]) is False

    def test_tracking_table_is_bounded(self):
        """A device that invents a patient id per message must not grow memory."""
        from services.vitals_source.sink import _MAX_TRACKED_PATIENTS
        sink = IngestionSink()
        for i in range(_MAX_TRACKED_PATIENTS + 250):
            sink.is_backfill("mon", [ReadingHint(f"P{i}", _iso())])
        assert len(sink._high_water) <= _MAX_TRACKED_PATIENTS

    def test_ordering_survives_a_constant_clock_offset(self):
        """The reason ordering is judged device-clock against device-clock: a
        constant offset cancels out entirely. A monitor 20 minutes slow still
        orders its own stream perfectly, so we keep the replay protection
        WITHOUT the wall-clock rule that muted the patient."""
        sink = IngestionSink(skew_tolerance_seconds=600)
        # Device clock is 20 min slow; these are its successive readings.
        sink.classify("mon", [ReadingHint("P1", _iso(-20), device_id="MON-A")])
        fresher = sink.classify("mon", [ReadingHint("P1", _iso(-19), device_id="MON-A")])
        replayed = sink.classify("mon", [ReadingHint("P1", _iso(-65), device_id="MON-A")])

        assert fresher.backfill is False           # its newer reading is live
        assert replayed.backfill is True           # a genuine replay still caught
        assert fresher.skew_state == "skewed"      # and the clock fault is still raised


class TestClockSkewIsAFaultNotSilence:
    """BLOCKING 3. The live window used to answer 'is this current?' by
    comparing the DEVICE'S clock to OURS and discarding anything too old.

    Proven over a real socket before this fix: a monitor 20 minutes slow sent
    six messages carrying HR 132 / SpO2 84 / RR 30 (NEWS2 ≈ 10) and 100 % were
    classified backfill — no NEWS2, no Vigil alert, no live emit. The patient
    was crashing and Orb said nothing, because a CLOCK was wrong.

    The deliberate replacement, argued in `sink.py`'s docstring: receipt time is
    the alerting clock (it is the one clock we control), device time is recorded
    for the record and for ordering, and a disagreement between them is an
    OPERATIONAL FAULT raised against the device — counted, surfaced in
    `status()`, and logged at ERROR — never a muted patient.
    """

    def test_a_slow_clock_does_not_mute_the_patient(self):
        sink = IngestionSink(skew_tolerance_seconds=600)
        for i in range(6):
            decision = sink.classify(
                "mon", [ReadingHint("P1", _iso(-20 + i * 0.1), device_id="MON-A")])
            assert decision.backfill is False
        assert sink.clock_skew_report("mon")["devices_skewed"] == 1

    def test_a_fast_clock_is_a_fault_too(self):
        """A device 20 minutes AHEAD was invisible before: the old window only
        looked backwards. Its recorded observation times are just as wrong."""
        sink = IngestionSink(skew_tolerance_seconds=600)
        decision = sink.classify("mon", [ReadingHint("P1", _iso(+20), device_id="MON-B")])
        assert decision.backfill is False
        assert decision.skew_state == "skewed"
        assert decision.skew_seconds > 0
        assert sink.clock_skew_report("mon")["devices_skewed"] == 1

    def test_a_healthy_clock_raises_nothing(self):
        sink = IngestionSink(skew_tolerance_seconds=600)
        decision = sink.classify("mon", [ReadingHint("P1", _iso(-1), device_id="MON-A")])
        assert decision.skew_state == "ok"
        assert sink.clock_skew_report("mon")["state"] == "ok"

    def test_skew_is_counted_per_device_not_per_patient(self):
        """One monitor serves many beds; the fault belongs to the clock. Keying
        it by patient would also put an identifier into a report that is
        rendered on an admin screen and shipped in the heartbeat."""
        sink = IngestionSink(skew_tolerance_seconds=600)
        for patient in ("P1", "P2", "P3"):
            sink.classify("mon", [ReadingHint(patient, _iso(-30), device_id="MON-A")])
        sink.classify("mon", [ReadingHint("P9", _iso(-1), device_id="MON-B")])

        report = sink.clock_skew_report("mon")
        assert report["devices_tracked"] == 2
        assert report["devices_skewed"] == 1
        blob = json.dumps(report)
        for patient in ("P1", "P2", "P3", "P9"):
            assert patient not in blob, "a clock report must not carry patient ids"

    def test_faults_count_transitions_so_one_bad_device_is_one_fault(self):
        """A device 20 minutes out sends a message every few seconds. A counter
        that ticks per message tells an operator how chatty the device is, not
        how many devices are broken."""
        sink = IngestionSink(skew_tolerance_seconds=600)
        for i in range(25):
            sink.classify("mon", [ReadingHint("P1", _iso(-30 + i * 0.01), device_id="MON-A")])
        device = sink.clock_skew_report("mon")["devices"][0]
        assert device["faults"] == 1
        assert device["readings_out_of_tolerance"] == 25

    def test_a_clock_that_gets_fixed_recovers(self):
        sink = IngestionSink(skew_tolerance_seconds=600)
        sink.classify("mon", [ReadingHint("P1", _iso(-30), device_id="MON-A")])
        assert sink.clock_skew_report("mon")["state"] == "skewed"
        sink.classify("mon", [ReadingHint("P1", _iso(), device_id="MON-A")])
        assert sink.clock_skew_report("mon")["state"] == "ok"

    def test_source_stats_carry_the_skew_so_it_is_countable(self):
        stats = SourceStats()
        sink = IngestionSink(skew_tolerance_seconds=600)
        sink.classify("mon", [ReadingHint("P1", _iso(-30), device_id="MON-A")],
                      stats=stats)
        assert stats.clock_skew_faults == 1
        assert stats.clock_skew_readings == 1
        assert stats.clock_skew_state == "skewed"
        assert stats.last_clock_skew_seconds == pytest.approx(-1800, abs=5)
        assert stats.max_abs_clock_skew_seconds == pytest.approx(1800, abs=5)
        json.dumps(stats.as_dict())   # must stay serialisable for the heartbeat

    def test_forgetting_a_source_keeps_the_clock_finding(self):
        """'The monitor in bay 3 has a wrong clock' is an operational finding
        that outlives a feed reconnect. Dropping it on every reconnect is how a
        fault becomes invisible."""
        sink = IngestionSink(skew_tolerance_seconds=600)
        sink.classify("mon", [ReadingHint("P1", _iso(-30), device_id="MON-A")])
        sink.forget("mon")
        assert sink.clock_skew_report("mon")["devices_skewed"] == 1

    def test_clock_table_is_bounded(self):
        from services.vitals_source.sink import _MAX_TRACKED_CLOCKS
        sink = IngestionSink()
        for i in range(_MAX_TRACKED_CLOCKS + 100):
            sink.classify("mon", [ReadingHint("P1", _iso(), device_id=f"DEV{i}")])
        assert len(sink._clocks) <= _MAX_TRACKED_CLOCKS


# ══════════════════════════════════════════════════════════════════════════════
#  The monitor source over a real socket, with a real fake monitor
# ══════════════════════════════════════════════════════════════════════════════

async def _start_source(sink, **kwargs) -> HL7MonitorSource:
    source = HL7MonitorSource(sink, host="127.0.0.1", port=0, **kwargs)
    await source.start()
    return source


async def _send_raw(port: int, data: bytes, *, expect_acks: int = 0,
                    timeout: float = 3.0) -> list[str]:
    """Open a connection, write bytes, collect MSA-1 codes, close."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    codes: list[str] = []
    try:
        writer.write(data)
        await writer.drain()
        framer = mllp.MllpFramer()
        deadline = asyncio.get_running_loop().time() + timeout
        while len(codes) < expect_acks:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            for raw in framer.feed(chunk):
                msa = hl7v2.parse(raw.decode()).first("MSA")
                codes.append(msa.field(1) if msa else "?")
    finally:
        writer.close()
    return codes


def _oru(patient="PAT001", *, control="C1", observed=None) -> str:
    bed = BedState(patient_id=patient)
    bed.sequence = 1
    return build_oru(bed, control_id=control, observed_at=observed)


class TestMonitorSourceOverSocket:
    async def test_good_message_is_accepted_acked_and_forwarded(self):
        sink = RecordingSink()
        source = await _start_source(sink)
        try:
            codes = await _send_raw(source.bound_port, mllp.frame(_oru()), expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=5)
            assert codes == ["AA"]
            assert len(sink.payloads) == 1
            assert sink.payloads[0]["provenance_label"] == LABEL_MONITOR_HL7
            assert sink.payloads[0]["external_message_id"] == "C1"
            assert sink.payloads[0]["backfill"] is False
            # The RAW device bytes are what travels on, not our re-serialisation.
            assert sink.payloads[0]["raw"].startswith("MSH|")
        finally:
            await source.stop()

    async def test_garbage_before_a_message_does_not_eat_it(self):
        sink = RecordingSink()
        source = await _start_source(sink)
        try:
            blob = b"\x00\x1f serial line noise \xff" + mllp.frame(_oru())
            codes = await _send_raw(source.bound_port, blob, expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=5)
            assert codes == ["AA"]
            assert len(sink.payloads) == 1
        finally:
            await source.stop()

    async def test_unparseable_message_is_rejected_not_retried_forever(self):
        """AR, not AE: retransmitting identical unparseable bytes fails
        identically, and telling the sender to retry turns one bad message into
        a permanent flood."""
        sink = RecordingSink()
        source = await _start_source(sink)
        try:
            codes = await _send_raw(
                source.bound_port, mllp.frame(b"this is not HL7 at all"), expect_acks=1)
            assert codes == ["AR"]
            assert sink.payloads == []
            assert source.stats.parse_failures == 1
            assert source.running
        finally:
            await source.stop()

    async def test_non_oru_message_is_rejected_and_counted_not_silently_dropped(self):
        sink = RecordingSink()
        source = await _start_source(sink)
        try:
            codes = await _send_raw(
                source.bound_port,
                mllp.frame(build_adt(BedState(patient_id="PAT001"))),
                expect_acks=1)
            assert codes == ["AR"]
            assert source.unsupported_messages == 1
            assert sink.payloads == []
        finally:
            await source.stop()

    async def test_message_without_a_patient_identifier_is_rejected(self):
        sink = RecordingSink()
        source = await _start_source(sink)
        try:
            headless = ("MSH|^~\\&|X|Y|ORB|ORB_OS|20260801143025||ORU^R01|Z1|P|2.5\r"
                        "OBX|1|NM|8867-4^Heart Rate^LN||80|bpm\r")
            codes = await _send_raw(source.bound_port, mllp.frame(headless), expect_acks=1)
            assert codes == ["AR"]
            assert sink.payloads == []
        finally:
            await source.stop()

    async def test_truncated_message_then_a_good_one(self):
        """The cable was pulled mid-send. The next message must still land."""
        sink = RecordingSink()
        source = await _start_source(sink)
        try:
            half = _oru()[: len(_oru()) // 2].encode()
            blob = b"\x0b" + half + mllp.frame(_oru(control="C2"))
            codes = await _send_raw(source.bound_port, blob, expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=5)
            assert codes == ["AA"]
            assert [p["external_message_id"] for p in sink.payloads] == ["C2"]
        finally:
            await source.stop()

    async def test_abrupt_disconnect_mid_message_does_not_kill_the_source(self):
        sink = RecordingSink()
        source = await _start_source(sink)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", source.bound_port)
            writer.write(b"\x0bMSH|^~\\&|partial")
            await writer.drain()
            writer.close()
            await asyncio.sleep(0.1)

            assert source.running
            codes = await _send_raw(source.bound_port, mllp.frame(_oru()), expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=5)
            assert codes == ["AA"]
            assert len(sink.payloads) == 1
        finally:
            await source.stop()

    async def test_duplicate_retransmission_is_forwarded_for_audited_dedup(self):
        """Dedup is authoritative downstream (fingerprint + ingestion_events), so
        a retransmit is forwarded rather than dropped here — dropping it at the
        socket would erase the audit record that it ever arrived."""
        sink = RecordingSink()
        source = await _start_source(sink)
        try:
            msg = mllp.frame(_oru())
            codes = await _send_raw(source.bound_port, msg + msg, expect_acks=2)
            await asyncio.wait_for(source._queue.join(), timeout=5)
            assert codes == ["AA", "AA"]
            assert len(sink.payloads) == 2
        finally:
            await source.stop()

    async def test_out_of_order_replay_is_stored_but_flagged_backfill(self):
        sink = RecordingSink()
        source = await _start_source(sink)
        try:
            now = datetime.now(timezone.utc)
            fresh = mllp.frame(_oru(control="NOW", observed=now))
            stale = mllp.frame(_oru(control="OLD", observed=now - timedelta(minutes=45)))
            await _send_raw(source.bound_port, fresh + stale, expect_acks=2)
            await asyncio.wait_for(source._queue.join(), timeout=5)

            flags = {p["external_message_id"]: p["backfill"] for p in sink.payloads}
            assert flags == {"NOW": False, "OLD": True}
            # Nothing was dropped — both are on their way to the vitals history.
            assert len(sink.payloads) == 2
        finally:
            await source.stop()

    async def test_flood_is_nacked_with_ae_so_the_reading_is_retried_not_lost(self):
        gate = asyncio.Event()

        class _BlockedSink(RecordingSink):
            async def submit_payload(self, *args, **kwargs):
                await gate.wait()
                return await super().submit_payload(*args, **kwargs)

        sink = _BlockedSink()
        source = await _start_source(sink, queue_size=1, workers=1)
        try:
            blob = b"".join(mllp.frame(_oru(control=f"F{i}")) for i in range(6))
            codes = await _send_raw(source.bound_port, blob, expect_acks=6)
            assert "AE" in codes, f"expected backpressure NACKs, got {codes}"
            assert source.stats.overflow_rejections >= 1
            # The connection survives the flood; only individual messages are refused.
            assert source.running
        finally:
            gate.set()
            await source.stop()

    async def test_connection_cap_refuses_extra_sockets(self):
        sink = RecordingSink()
        source = await _start_source(sink, max_connections=1)
        try:
            r1, w1 = await asyncio.open_connection("127.0.0.1", source.bound_port)
            await asyncio.sleep(0.05)
            r2, w2 = await asyncio.open_connection("127.0.0.1", source.bound_port)
            # The second connection is closed by the server without being served.
            assert await asyncio.wait_for(r2.read(1), timeout=2) == b""
            w1.close()
            w2.close()
        finally:
            await source.stop()

    async def test_a_failing_sink_never_unwinds_into_the_loop(self):
        """The workstream's hard rule: no unhandled exception from a device
        message may reach the ingestion loop."""
        class _ExplodingSink(RecordingSink):
            async def submit_payload(self, *args, **kwargs):
                raise RuntimeError("database on fire")

        source = await _start_source(_ExplodingSink())
        try:
            codes = await _send_raw(source.bound_port, mllp.frame(_oru()), expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=5)
            assert codes == ["AA"]
            assert source.stats.parse_failures == 1
            assert "database on fire" in (source.stats.last_error or "")
            assert source.running
            # And the next message is still processed.
            await _send_raw(source.bound_port, mllp.frame(_oru(control="C9")), expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=5)
        finally:
            await source.stop()

    async def test_bench_monitor_with_every_fault_mode_enabled(self):
        """The whole point of the bench simulator: run the nastiest device we can
        describe against the real socket path and require the source to survive
        it and still deliver the good messages."""
        sink = RecordingSink()
        source = await _start_source(sink, queue_size=4096)
        try:
            monitor = BenchMonitor(
                "127.0.0.1", source.bound_port,
                patients=["PAT001", "PAT002"],
                interval_seconds=0,
                faults=["garbage", "truncate", "duplicate", "out_of_order", "unsupported"],
                seed=11,
            )
            await monitor.run_forever(cycles=4)
            await asyncio.wait_for(source._queue.join(), timeout=10)

            assert source.running
            assert len(sink.payloads) >= 8            # at least the 2 beds × 4 cycles
            assert source.stats.messages_accepted >= 8
            assert monitor.stats.faults_injected      # faults really fired
            assert any(p["backfill"] for p in sink.payloads) or \
                "out_of_order" not in monitor.stats.faults_injected
        finally:
            await source.stop()

    async def test_stop_releases_the_port(self):
        """A listener left bound across a reload turns 'restart Orb' into
        'address already in use' on the appliance."""
        source = await _start_source(RecordingSink())
        port = source.bound_port
        await source.stop()
        # Binding the same port again must now succeed.
        again = HL7MonitorSource(RecordingSink(), host="127.0.0.1", port=port)
        await again.start()
        try:
            assert again.bound_port == port
        finally:
            await again.stop()

    async def test_stop_is_idempotent(self):
        source = await _start_source(RecordingSink())
        await source.stop()
        await source.stop()
        assert not source.running

    async def test_status_reports_health_without_payloads(self):
        source = await _start_source(RecordingSink())
        try:
            await _send_raw(source.bound_port, mllp.frame(b"garbage"), expect_acks=1)
            status = source.status()
            assert status["kind"] == "monitor"
            assert status["provenance_label"] == LABEL_MONITOR_HL7
            assert status["protocol"] == "hl7v2-mllp"
            assert status["queue_capacity"] >= 1
            assert status["recent_rejects"], "a reject must be visible to an operator"
            # No PHI or payload bytes leak into the status surface.
            for reject in status["recent_rejects"]:
                assert set(reject) == {"at", "peer", "code", "reason", "control_id"}
        finally:
            await source.stop()


def _critical_bed(patient="PAT001") -> BedState:
    """HR 132 / SpO2 84 / RR 30 — NEWS2 ≈ 10. A patient who is crashing."""
    bed = BedState(patient_id=patient, hr=132.0, spo2=84.0, rr=30.0,
                   sbp=88.0, dbp=52.0, temp=38.9)
    bed.sequence = 1
    return bed


class TestAMonitorWithAWrongClockStillAlerts:
    """BLOCKING 3, over a real socket — the reviewer's repro, made permanent.

    Before the fix this test's first assertion failed six times out of six: a
    monitor whose clock was 20 minutes slow had every one of its critical
    messages classified `backfill`, which skips NEWS2, the Vigil alert bridge
    and the live emit.
    """

    async def _run(self, *, clock_offset_seconds, emit_offset, device_tz=None,
                   messages=6):
        sink = RecordingSink(skew_tolerance_seconds=900)
        source = await _start_source(sink, device_timezone=device_tz)
        try:
            bed = _critical_bed()
            blob = b""
            for i in range(messages):
                blob += mllp.frame(build_oru(
                    bed,
                    observed_at=datetime.now(timezone.utc) + timedelta(seconds=i),
                    control_id=f"CRIT{i}",
                    device_timezone=device_tz,
                    emit_offset=emit_offset,
                    clock_offset_seconds=clock_offset_seconds,
                ))
            await _send_raw(source.bound_port, blob, expect_acks=messages)
            await asyncio.wait_for(source._queue.join(), timeout=10)
            return sink, source, source.status()
        finally:
            await source.stop()

    async def test_a_twenty_minute_slow_clock_no_longer_mutes_a_crashing_patient(self):
        sink, source, status = await self._run(
            clock_offset_seconds=-20 * 60, emit_offset=True)

        assert len(sink.payloads) == 6
        excluded = [p for p in sink.payloads if p["backfill"]]
        assert excluded == [], (
            "a monitor with a wrong clock must not silence a crashing patient; "
            f"{len(excluded)}/6 critical messages were excluded from alerting")
        assert source.stats.backfilled == 0

        # …and the real fault is loud instead.
        assert source.stats.clock_skew_faults == 1
        assert source.stats.clock_skew_state == "skewed"
        assert source.stats.last_clock_skew_seconds == pytest.approx(-1200, abs=60)
        assert status["clock_skew"]["state"] == "skewed"
        assert status["clock_skew"]["devices_skewed"] == 1
        assert status["clock_skew"]["devices"][0]["device_id"] == "ICU_MONITOR^HOSPITAL_A"

    async def test_a_twenty_minute_fast_clock_is_also_visible(self):
        sink, source, status = await self._run(
            clock_offset_seconds=+20 * 60, emit_offset=True)
        assert [p["backfill"] for p in sink.payloads] == [False] * 6
        assert source.stats.clock_skew_faults == 1
        assert source.stats.last_clock_skew_seconds == pytest.approx(+1200, abs=60)
        assert status["clock_skew"]["state"] == "skewed"

    async def test_an_offset_less_local_dtm_is_neither_skewed_nor_muted(self):
        """The compound case. Pre-fix, a monitor emitting offset-less IST wall
        clock was read as UTC and landed 5.5 h in the FUTURE — wrong in the
        record, wrong for ordering, and one sign flip away from total silence.
        A correct clock plus a correctly-assumed zone is simply healthy."""
        sink, source, status = await self._run(
            clock_offset_seconds=0, emit_offset=False)
        assert [p["backfill"] for p in sink.payloads] == [False] * 6
        assert status["clock_skew"]["state"] == "ok"
        assert source.stats.clock_skew_faults == 0
        assert abs(source.stats.last_clock_skew_seconds) < 60

    async def test_a_device_configured_in_another_zone_is_read_in_that_zone(self):
        """Different devices on one ward can be configured differently, and
        assuming they agree is how a timezone bug bites twice. A gateway set to
        UTC while the appliance is in IST is healthy ONLY if the source is told
        so — otherwise it looks 5.5 hours out."""
        sink, source, status = await self._run(
            clock_offset_seconds=0, emit_offset=False, device_tz="UTC")
        assert [p["backfill"] for p in sink.payloads] == [False] * 6
        assert status["clock_skew"]["state"] == "ok"
        assert source.describe()["assumed_timezone"] == "UTC"
        assert source.describe()["device_timezone_configured"] is True

    async def test_the_same_device_misdeclared_shows_up_as_a_skewed_clock(self):
        """The failure mode this makes VISIBLE rather than silent: a device that
        really is in IST, but whose source was configured as UTC, is 5.5 h out.
        Pre-fix that was either a silent 5.5 h error in the record or total
        alerting silence. Now it is a named fault, and the patient still alerts."""
        sink = RecordingSink(skew_tolerance_seconds=900)
        source = await _start_source(sink, device_timezone="UTC")
        try:
            bed = _critical_bed()
            blob = mllp.frame(build_oru(bed, control_id="MISDECLARED",
                                        device_timezone="Asia/Kolkata",
                                        emit_offset=False))
            await _send_raw(source.bound_port, blob, expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=10)

            assert sink.payloads[0]["backfill"] is False, "still alerts"
            assert source.stats.clock_skew_state == "skewed"
            assert source.stats.last_clock_skew_seconds == pytest.approx(5.5 * 3600, abs=120)
            device = source.status()["clock_skew"]["devices"][0]
            assert device["device_states_offset"] is False
        finally:
            await source.stop()

    async def test_replay_detection_still_works_under_a_wrong_clock(self):
        """We did not buy liveness by giving up ordering: a device that replays
        its buffer is still caught, because ordering compares the device's clock
        to itself and a constant offset cancels."""
        sink = RecordingSink(skew_tolerance_seconds=900)
        source = await _start_source(sink)
        try:
            bed = _critical_bed()
            now = datetime.now(timezone.utc)
            blob = mllp.frame(build_oru(bed, observed_at=now, control_id="NOW",
                                        clock_offset_seconds=-20 * 60))
            blob += mllp.frame(build_oru(bed, observed_at=now - timedelta(minutes=45),
                                         control_id="OLD",
                                         clock_offset_seconds=-20 * 60))
            await _send_raw(source.bound_port, blob, expect_acks=2)
            await asyncio.wait_for(source._queue.join(), timeout=10)

            flags = {p["external_message_id"]: p["backfill"] for p in sink.payloads}
            assert flags == {"NOW": False, "OLD": True}
        finally:
            await source.stop()

    async def test_the_skew_surface_is_phi_free_and_serialisable(self):
        sink, source, status = await self._run(
            clock_offset_seconds=-30 * 60, emit_offset=True, messages=2)
        blob = json.dumps(status["clock_skew"])
        assert "PAT001" not in blob
        assert "Doe" not in blob
        assert status["clock_skew"]["tolerance_seconds"] == 900


class TestMonitorSourceConnectMode:
    async def test_orb_can_dial_out_to_a_gateway_that_listens(self):
        """Some central stations and serial-to-IP converters expect the receiver
        to connect to THEM. Same message path, different socket setup."""
        received: list[str] = []
        ready = asyncio.Event()

        async def gateway(reader, writer):
            writer.write(mllp.frame(_oru(control="GW1")))
            await writer.drain()
            ready.set()
            data = await reader.read(65536)
            received.append(data.decode(errors="replace"))
            writer.close()

        server = await asyncio.start_server(gateway, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sink = RecordingSink()
        source = HL7MonitorSource(sink, host="127.0.0.1", port=port, mode="connect")
        await source.start()
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            # Orb is the CLIENT here, so there is no ACK round-trip to
            # synchronise on from this side — poll until the message lands.
            deadline = asyncio.get_running_loop().time() + 5
            while not sink.payloads and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            assert [p["external_message_id"] for p in sink.payloads] == ["GW1"]
            # Orb still ACKs on a dialled-out connection: same message path.
            await asyncio.sleep(0.05)
            assert received and "MSA|AA" in received[0]
        finally:
            await source.stop()
            server.close()
            await server.wait_closed()

    def test_unknown_mode_is_rejected_at_construction(self):
        with pytest.raises(ValueError):
            HL7MonitorSource(RecordingSink(), mode="carrier-pigeon")


# ══════════════════════════════════════════════════════════════════════════════
#  The other two peers, and the registry that supervises all three
# ══════════════════════════════════════════════════════════════════════════════

class TestManualEntrySource:
    async def test_typed_vitals_carry_manual_provenance(self):
        sink = RecordingSink()
        source = ManualEntrySource(sink)
        await source.start()
        result = await source.record(
            "PAT001", {"HR": 96, "SpO2": 94}, entered_by="N001")
        assert result.inserted_vitals == 1
        assert source.kind is SourceKind.MANUAL
        assert source.provenance_label == LABEL_MANUAL_ENTRY
        reading = sink.readings[0]
        assert reading.entered_by == "N001"
        assert reading.values == {"HR": 96.0, "SpO2": 94.0}

    async def test_manual_reading_renders_the_documented_webhook_shape(self):
        sink = RecordingSink()
        source = ManualEntrySource(sink)
        await source.record("PAT001", {"Temp": 38.4}, observed_at="2026-08-01T10:00:00+00:00")
        payload = sink.readings[0].to_json_payload()
        assert payload["patient_id"] == "PAT001"
        assert payload["observations"] == [{
            "code": "8310-5", "name": "Body Temperature", "value": 38.4,
            "unit": "Cel", "status": "final", "observed_at": "2026-08-01T10:00:00+00:00",
        }]

    async def test_empty_entry_is_a_no_op_not_a_fabricated_row(self):
        sink = RecordingSink()
        source = ManualEntrySource(sink)
        result = await source.record("PAT001", {})
        assert result.inserted_vitals == 0
        assert sink.readings == []

    def test_nurse_observation_wrapper_is_fail_loud(self, monkeypatch):
        """The nurse charting path's recorded design decision: an observation
        feeds an escalation decision, so a failed write is never queued or
        optimistic — it reaches the caller, and it is counted on the source's
        health surface rather than only in a log line."""
        from services import nurse_shift_service

        def _refuse(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(nurse_shift_service, "_record_observation_txn", _refuse)
        source = ManualEntrySource(None)
        with pytest.raises(RuntimeError, match="disk full"):
            source.record_observation("PAT001", "N001", {"heart_rate": 88})
        assert source.stats.messages_rejected == 1
        assert source.stats.messages_accepted == 0
        assert "disk full" in (source.stats.last_error or "")

    def test_nurse_observation_wrapper_counts_charted_and_replayed(self, monkeypatch):
        """A replay (offline queue, double-tap) is a duplicate on the health
        surface, not a second reading — miscounting here would make manual
        entry look busier than the ward actually is."""
        from services import nurse_shift_service

        results = iter([{"vitals_id": 7, "replayed": False},
                        {"vitals_id": 7, "replayed": True}])
        monkeypatch.setattr(nurse_shift_service, "_record_observation_txn",
                            lambda *a, **k: next(results))
        source = ManualEntrySource(None)
        first = source.record_observation("PAT001", "N001", {"heart_rate": 88})
        replay = source.record_observation("PAT001", "N001", {"heart_rate": 88})
        assert first["replayed"] is False and replay["replayed"] is True
        assert source.stats.messages_accepted == 2
        assert source.stats.readings_ingested == 1
        assert source.stats.duplicates == 1
        assert source.stats.last_reading_at is not None


class _FakeCursor:
    lastrowid = 4242


class _FakeTransaction:
    """Stands in for the caller's already-open `db_session` handle."""

    def __init__(self, fail: Exception | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self.fail = fail

    def execute(self, sql, params=()):
        if self.fail is not None:
            raise self.fail
        self.calls.append((sql, params))
        return _FakeCursor()


class TestAdmissionVitalsSeam:
    """`record_at_admission` — the admission form's initial vitals set.

    Third manual path, same provenance contract as the other two. It is the one
    that writes inside SOMEONE ELSE'S transaction, because an admission is
    atomic: the reading commits with the patient or not at all.
    """

    def test_admission_write_carries_the_seams_manual_provenance(self):
        db = _FakeTransaction()
        source = ManualEntrySource(None)
        row_id = source.record_at_admission(
            db, "PAT001", {"HR": 96, "SBP": 120, "SpO2": 94},
            observed_at="2026-08-11T09:00:00+00:00", on_oxygen=True)

        assert row_id == 4242
        assert len(db.calls) == 1
        sql, params = db.calls[0]
        assert "INSERT INTO vitals" in sql
        # No wire event: a typed set has no device bytes behind it, so the
        # column is left NULL rather than pointed at a fabricated event.
        assert "source_event_id" not in sql
        assert params[0] == "PAT001"
        # The same instant, in the one stored spelling every vitals writer uses
        # (services/clinical_time.py, 2026-10-03).
        assert params[1] == "2026-08-11T09:00:00.000000+00:00"
        assert params[10] == source.admission_label == LABEL_ADMISSION_VITALS
        assert classify_source_label(params[10]) is SourceKind.MANUAL
        assert params[8] is None, "no ACVPU supplied: not assessed, never 'alert'"
        assert params[9] == 1, "supplemental O2 must persist for the NEWS2 +2 term"

    def test_a_vital_the_clinician_did_not_enter_stays_null_not_zero(self):
        """Carry-forward and NEWS2 both read a missing vital as 'not measured'.
        A 0 here would be a fabricated observation — an RR of 0 scores +3.
        ACVPU and O2 follow the same rule since 2026-10-03: not supplied is not
        assessed (NULL), never a recorded 'alert' / on air; an explicit False is
        still a recorded false."""
        db = _FakeTransaction()
        ManualEntrySource(None).record_at_admission(
            db, "PAT001", {"HR": 96}, observed_at="2026-08-11T09:00:00+00:00")
        params = db.calls[0][1]
        assert params[2] == 96
        assert params[3:8] == (None, None, None, None, None)
        assert params[8:10] == (None, None), "absent ACVPU/O2 were stored as assessed"

        db = _FakeTransaction()
        ManualEntrySource(None).record_at_admission(
            db, "PAT001", {"HR": 96}, observed_at="2026-08-11T09:00:00+00:00",
            consciousness="alert", on_oxygen=False)
        assert db.calls[0][1][8:10] == ("alert", 0), "a recorded value must be kept"

    def test_a_vital_key_the_seam_does_not_know_is_refused_not_dropped(self):
        """A caller-side key typo must not silently vanish: the row would then
        disagree with the NEWS2 the caller computed from the same form, and the
        chart would show a vital the clinician entered as never taken."""
        db = _FakeTransaction()
        source = ManualEntrySource(None)
        with pytest.raises(ValueError, match="heart_rate"):
            source.record_at_admission(
                db, "PAT001", {"HR": 96, "heart_rate": 96},
                observed_at="2026-08-11T09:00:00+00:00")
        assert db.calls == [], "nothing may be written on a rejected set"
        assert source.stats.messages_rejected == 1

    def test_admission_write_is_fail_loud_and_counted(self):
        """Admission is fail-loud by design — the clinician must not be told a
        patient was admitted with vitals that were never stored — and the
        failure shows up on the source's health surface, not only in a log."""
        source = ManualEntrySource(None)
        db = _FakeTransaction(fail=RuntimeError("disk full"))
        with pytest.raises(RuntimeError, match="disk full"):
            source.record_at_admission(db, "PAT001", {"HR": 96},
                                       observed_at="2026-08-11T09:00:00+00:00")
        assert source.stats.messages_rejected == 1
        assert source.stats.messages_accepted == 0
        assert "disk full" in (source.stats.last_error or "")

    def test_admission_write_is_counted_on_the_shared_health_surface(self):
        source = ManualEntrySource(None)
        source.record_at_admission(_FakeTransaction(), "PAT001", {"HR": 96},
                                   observed_at="2026-08-11T09:00:00+00:00")
        assert source.stats.messages_received == 1
        assert source.stats.messages_accepted == 1
        assert source.stats.readings_ingested == 1
        assert source.stats.last_reading_at is not None


class TestSimulatorSource:
    async def test_tick_and_companion_run_in_the_historical_order(self):
        order: list[str] = []

        async def tick():
            order.append("vitals")

        async def companion():
            order.append("companion")

        source = SimulatorSource(tick=tick, companion_tick=companion, interval_seconds=0.01)
        await source.start()
        await asyncio.sleep(0.05)
        await source.stop()
        assert order[:4] == ["vitals", "companion", "vitals", "companion"]
        assert source.provenance_label == LABEL_SIMULATED
        assert source.kind is SourceKind.SIMULATOR

    async def test_a_failing_tick_does_not_kill_the_loop(self):
        calls = {"n": 0}

        async def tick():
            calls["n"] += 1
            raise RuntimeError("bot exploded")

        source = SimulatorSource(tick=tick, interval_seconds=0.01)
        await source.start()
        await asyncio.sleep(0.06)
        await source.stop()
        assert calls["n"] >= 2
        assert "bot exploded" in (source.stats.last_error or "")


class TestRegistry:
    async def test_manual_entry_is_always_available_and_monitor_is_opt_in(self, monkeypatch):
        monkeypatch.delenv("ORB_MONITOR_ENABLED", raising=False)
        reg = configure_from_env(target=VitalsSourceRegistry())
        assert [s.source_id for s in reg.sources] == ["manual"]

    async def test_monitor_is_registered_when_configured(self, monkeypatch):
        monkeypatch.setenv("ORB_MONITOR_ENABLED", "1")
        monkeypatch.setenv("ORB_MONITOR_PORT", "0")
        reg = configure_from_env(target=VitalsSourceRegistry())
        kinds = {s.kind for s in reg.sources}
        assert SourceKind.MONITOR in kinds and SourceKind.MANUAL in kinds
        await reg.reset()

    async def test_unsupported_protocol_is_loud_but_not_fatal(self, monkeypatch):
        monkeypatch.setenv("ORB_MONITOR_ENABLED", "1")
        monkeypatch.setenv("ORB_MONITOR_PROTOCOL", "astm-serial")
        reg = configure_from_env(target=VitalsSourceRegistry())
        assert SourceKind.MONITOR not in {s.kind for s in reg.sources}
        assert reg.get("manual") is not None

    async def test_one_source_failing_to_start_does_not_stop_the_others(self):
        class _Broken(ManualEntrySource):
            async def start(self):
                raise OSError("address already in use")

        reg = VitalsSourceRegistry()
        reg.register(_Broken(RecordingSink(), source_id="broken"))
        reg.register(ManualEntrySource(RecordingSink(), source_id="manual"))
        started = await reg.start_all()
        assert started == ["manual"]
        assert "address already in use" in (reg.get("broken").stats.last_error or "")
        await reg.reset()

    async def test_status_is_serialisable_and_phi_free(self):
        reg = VitalsSourceRegistry()
        reg.register(ManualEntrySource(RecordingSink()))
        await reg.start_all()
        status = reg.status()
        import json
        json.dumps(status)  # must not raise
        assert status["count"] == 1
        assert status["sources"][0]["kind"] == "manual"
        await reg.reset()

    def test_duplicate_source_id_is_rejected(self):
        reg = VitalsSourceRegistry()
        reg.register(ManualEntrySource(RecordingSink()))
        with pytest.raises(ValueError):
            reg.register(ManualEntrySource(RecordingSink()))

    async def test_reconfiguring_replaces_rather_than_leaving_the_ward_sourceless(
            self, monkeypatch):
        """A lifespan that runs twice in one process must not end up with the
        'already registered' error swallowed and NO sources at all."""
        monkeypatch.delenv("ORB_MONITOR_ENABLED", raising=False)
        reg = VitalsSourceRegistry()
        configure_from_env(target=reg)
        configure_from_env(target=reg)
        assert [s.source_id for s in reg.sources] == ["manual"]

    async def test_a_monitor_that_cannot_bind_leaves_no_orphan_tasks(self):
        """Binding failure ('address already in use') is the everyday appliance
        case. The drain workers are spawned before the socket, so they must be
        reaped when it fails or they run forever against a dead source."""
        blocker = await _start_source(RecordingSink())
        try:
            before = len(asyncio.all_tasks())
            clash = HL7MonitorSource(RecordingSink(), host="127.0.0.1",
                                     port=blocker.bound_port, source_id="clash")
            reg = VitalsSourceRegistry()
            reg.register(clash)
            assert await reg.start_all() == []
            await asyncio.sleep(0.05)
            assert len(asyncio.all_tasks()) <= before
            assert not clash.running
        finally:
            await blocker.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  End to end: a monitor message becomes a `monitor_hl7` vitals row
# ══════════════════════════════════════════════════════════════════════════════

class TestEndToEndPersistence:
    async def test_monitor_reading_lands_with_machine_provenance(self):
        from database import db_session

        patient_id = "P-VS-E2E"
        with db_session() as db:
            db.execute(
                "INSERT OR IGNORE INTO patients (id, name, age, sex, mrn, status, ward) "
                "VALUES (?, 'Vitals Source Test', 44, 'M', ?, 'admitted', 'ICU')",
                (patient_id, patient_id),
            )

        source = await _start_source(IngestionSink())
        try:
            bed = BedState(patient_id=patient_id)
            bed.sequence = 7
            await _send_raw(source.bound_port,
                            mllp.frame(build_oru(bed, control_id="E2E1")), expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=15)

            with db_session() as db:
                row = db.execute(
                    "SELECT source, heart_rate, spo2, source_event_id FROM vitals "
                    "WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (patient_id,)
                ).fetchone()
                assert row is not None, "monitor reading did not reach the vitals table"
                assert row["source"] == LABEL_MONITOR_HL7
                assert is_machine_measured(row["source"])
                assert row["heart_rate"] is not None

                # The evidence chain: vitals row → ingestion_events → the device's
                # own bytes and the source that produced them.
                event = db.execute(
                    "SELECT source_name, raw_payload, parse_status FROM ingestion_events "
                    "WHERE id = ?", (row["source_event_id"],)
                ).fetchone()
                assert event is not None
                assert event["source_name"] == source.source_id
                assert event["raw_payload"].startswith("MSH|")
        finally:
            await source.stop()

    async def test_out_of_order_reading_is_stored_but_does_not_become_the_live_risk(self):
        """The clinical reason the backfill flag exists: risk_scores rows are
        stamped with the CURRENT time, so a replayed 45-minute-old reading would
        otherwise become the patient's newest risk score and could mask a fresher,
        worse one."""
        from database import db_session

        patient_id = "P-VS-STALE"
        with db_session() as db:
            db.execute(
                "INSERT OR IGNORE INTO patients (id, name, age, sex, mrn, status, ward) "
                "VALUES (?, 'Stale Replay Test', 61, 'F', ?, 'admitted', 'ICU')",
                (patient_id, patient_id),
            )
            db.execute("DELETE FROM risk_scores WHERE patient_id = ?", (patient_id,))
            db.execute("DELETE FROM vitals WHERE patient_id = ?", (patient_id,))

        source = await _start_source(IngestionSink())
        try:
            now = datetime.now(timezone.utc)
            bed = BedState(patient_id=patient_id)
            bed.sequence = 1
            await _send_raw(
                source.bound_port,
                mllp.frame(build_oru(bed, control_id="FRESH", observed_at=now)),
                expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=15)

            with db_session() as db:
                risk_after_fresh = db.execute(
                    "SELECT COUNT(*) AS n FROM risk_scores WHERE patient_id = ?",
                    (patient_id,)).fetchone()["n"]

            bed.sequence = 2
            bed.hr = 41.0  # would be a very different NEWS2 if it scored
            await _send_raw(
                source.bound_port,
                mllp.frame(build_oru(bed, control_id="STALE",
                                     observed_at=now - timedelta(minutes=45))),
                expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=15)

            with db_session() as db:
                vitals_rows = db.execute(
                    "SELECT COUNT(*) AS n FROM vitals WHERE patient_id = ?",
                    (patient_id,)).fetchone()["n"]
                risk_rows = db.execute(
                    "SELECT COUNT(*) AS n FROM risk_scores WHERE patient_id = ?",
                    (patient_id,)).fetchone()["n"]

            # Nothing was dropped: both readings are in the history.
            assert vitals_rows == 2
            # But the stale one did not produce a new "current" risk score.
            assert risk_rows == risk_after_fresh
            assert source.stats.backfilled == 1
        finally:
            await source.stop()

    async def test_a_slow_clock_monitors_critical_vitals_still_reach_scoring(self):
        """THE PROOF FOR BLOCKING 3, end to end against the real database.

        A monitor 20 minutes slow sends HR 132 / SpO2 84 / RR 30 — NEWS2 ≈ 10.
        Before the fix, every one of those messages was flagged `backfill`,
        which returns from `_insert_vitals` BEFORE `compute_news2`: no risk
        score, no Vigil alert, no live tile. The patient was crashing and the
        ward was told nothing.

        This asserts the thing that actually matters clinically — a `risk_scores`
        row exists and it is a high one — not merely that a flag flipped."""
        from database import db_session

        patient_id = "P-VS-SLOWCLOCK"
        with db_session() as db:
            db.execute(
                "INSERT OR IGNORE INTO patients (id, name, age, sex, mrn, status, ward) "
                "VALUES (?, 'Slow Clock Test', 58, 'M', ?, 'admitted', 'ICU')",
                (patient_id, patient_id),
            )
            db.execute("DELETE FROM risk_scores WHERE patient_id = ?", (patient_id,))
            db.execute("DELETE FROM vitals WHERE patient_id = ?", (patient_id,))

        source = await _start_source(IngestionSink(skew_tolerance_seconds=900))
        try:
            bed = _critical_bed(patient_id)
            await _send_raw(
                source.bound_port,
                mllp.frame(build_oru(bed, control_id="SLOWCLK",
                                     clock_offset_seconds=-20 * 60,
                                     emit_offset=True)),
                expect_acks=1)
            await asyncio.wait_for(source._queue.join(), timeout=15)

            with db_session() as db:
                risk = db.execute(
                    "SELECT score, level FROM risk_scores "
                    "WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (patient_id,)
                ).fetchone()
                row = db.execute(
                    "SELECT timestamp, source_event_id FROM vitals "
                    "WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (patient_id,)
                ).fetchone()
                event = db.execute(
                    "SELECT received_at FROM ingestion_events WHERE id = ?",
                    (row["source_event_id"],)).fetchone()

            # BOTH CLOCKS ARE IN THE RECORD, which is why receipt time can drive
            # alerting without losing anything: `vitals.timestamp` is the
            # DEVICE's time (needed for ordering and for the clinical record),
            # `ingestion_events.received_at` is OURS, and the device's own bytes
            # are in `raw_payload`. A reviewer can reconstruct the skew for any
            # past reading — no extra column was needed to make it evidential.
            device_time = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
            receipt = datetime.fromisoformat(str(event["received_at"]).replace(" ", "T"))
            if receipt.tzinfo is None:
                receipt = receipt.replace(tzinfo=timezone.utc)
            if device_time.tzinfo is None:
                device_time = device_time.replace(tzinfo=timezone.utc)
            skew_minutes = (device_time - receipt).total_seconds() / 60
            assert skew_minutes == pytest.approx(-20, abs=2), (
                f"the record should preserve the device's own (wrong) clock; "
                f"measured {skew_minutes:.1f} min")

            assert risk is not None, (
                "critical vitals from a monitor with a slow clock produced NO risk "
                "score — the patient is being scored on nothing")
            assert risk["level"] == "critical", (
                f"expected a critical risk level for HR 132 / SpO2 84 / RR 30, got "
                f"{risk['level']!r} (score {risk['score']})")
            assert source.stats.backfilled == 0
            # The clock is still called out as the operational fault it is.
            assert source.stats.clock_skew_faults == 1
        finally:
            await source.stop()
