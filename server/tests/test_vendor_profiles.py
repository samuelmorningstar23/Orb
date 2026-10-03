"""Orb — bedside-monitor VENDOR PROFILES (Philips IntelliVue / GE CARESCAPE).

The hospital's monitors are Philips or GE and nobody has been to site yet. These
tests hold the properties that make that survivable — and, just as important,
they hold the HONESTY properties, because the danger with a vendor mapping
written without a device in the room is not that it is wrong (it will be, in
places) but that it silently looks right.

Asserted here:

  * a vendor table NEVER overrides a mapping Orb already understood (LOINC
    wins), so a wrong guess cannot corrupt a reading that was already correct;
  * an identifier nobody maps is COUNTED and NAMED, because that list is the
    entire deliverable of a bench run before hardware arrives;
  * an observation the vendor sends that Orb deliberately does not score (mean
    arterial pressure) is neither scored NOR reported as unknown;
  * unit translation happens BEFORE the plausibility bounds — `DEGF` is the case
    that otherwise deletes a temperature silently;
  * one message carrying two sources for the same vital resolves deterministically
    (ECG heart rate beats oximeter pulse rate), not by parse order;
  * detection never claims a vendor from a weak string match;
  * no profile can claim to be verified against a device, because none is.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ingestion_adapters import ObservationType
from services.vitals_source import hl7v2, vendor_profiles as vp
from services.vitals_source.base import VITAL_KEYS
from services.vitals_source.bench_simulator import BedState, build_oru
from services.vitals_source.profile_adapter import VendorProfileAdapter


def _message(vendor, **kwargs) -> str:
    bed = BedState(patient_id="P001")
    bed.step(random.Random(11))
    return build_oru(bed, vendor=vendor, **kwargs), bed


def _parse(vendor, raw, **kwargs):
    notes: list[tuple[str, str]] = []
    adapter = VendorProfileAdapter(
        vp.profile_for(vendor) if isinstance(vendor, str) else vendor,
        observer=lambda kind, detail: notes.append((kind, detail)),
        **kwargs,
    )
    events = adapter.parse(raw, source_name="bench")
    mapped = {e.mapped_vital_key: e for e in events if e.mapped_vital_key}
    return events, mapped, notes


# ════════════════════════════════════════════════════════════════════════════
#  1. The tables themselves
# ════════════════════════════════════════════════════════════════════════════
def test_vital_keys_agree_with_the_contract():
    """`vendor_profiles` restates VITAL_KEYS as a literal (it must not import Orb
    code, so the bench can run on a laptop). The two must not drift."""
    assert vp.VITAL_KEYS == VITAL_KEYS


def test_no_identifier_means_two_different_vitals_across_vendors():
    """The auto/unnamed fallback consults both tables. If one token meant HR in
    one and SpO2 in the other, the fallback would map by coin toss."""
    assert vp.collision_report() == []


def test_every_mapping_targets_a_real_vital_or_is_explicitly_unscored():
    for profile in vp.all_profiles():
        for mapping in profile.mappings:
            assert mapping.vital_key is None or mapping.vital_key in VITAL_KEYS


def test_no_profile_claims_verification():
    """There is no code path that sets this True, and there must not be one until
    a captured message or a vendor spec exists."""
    for profile in vp.all_profiles():
        assert profile.describe()["verified_against_device"] is False
        assert profile.handshake.verified is False
    for level in vp.Confidence:
        assert level.is_verified is False


def test_both_vendor_profiles_cover_every_news2_vital():
    for profile in (vp.PHILIPS_INTELLIVUE, vp.GE_CARESCAPE):
        covered = {m.vital_key for m in profile.mappings if m.vital_key}
        assert covered == set(VITAL_KEYS), f"{profile.key} is missing {set(VITAL_KEYS) - covered}"


def test_site_questions_ask_for_a_captured_message():
    """The one artefact that settles the whole table has to be on the list."""
    for profile in (vp.PHILIPS_INTELLIVUE, vp.GE_CARESCAPE):
        assert any("captured" in q.lower() for q in profile.site_questions)


def test_unknown_profile_name_falls_back_to_generic_not_an_exception():
    """A typo in `.env` must cost LOINC-only mapping, never the ward's feed."""
    assert vp.profile_for("phillips").key == "generic"     # note the misspelling
    assert vp.is_known_profile("phillips") is False
    assert vp.profile_for("philips").key == "philips_intellivue"
    assert vp.profile_for("ge").key == "ge_carescape"
    assert vp.profile_for(None).key == "generic"


# ════════════════════════════════════════════════════════════════════════════
#  2. Detection
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("vendor,expected", [
    ("philips", "philips_intellivue"),
    ("ge", "ge_carescape"),
])
def test_detection_names_the_vendor_from_the_bench_message(vendor, expected):
    raw, _ = _message(vendor)
    detection = vp.detect_from_message(hl7v2.parse(raw))
    assert detection.profile.key == expected
    assert detection.named_vendor is True


def test_detection_says_it_is_guessing_when_nothing_matches():
    raw, _ = _message("generic")
    detection = vp.detect_from_message(hl7v2.parse(raw))
    assert detection.named_vendor is False
    assert detection.profile.key == "unnamed_vendor"


def test_short_hints_match_whole_tokens_only():
    """`GE` must not be found inside SURGERY. A false vendor claim is worse than
    no claim: it silently applies the wrong identifier table."""
    assert vp.detect("SURGERY_GATEWAY", "IMAGING_DEPT").named_vendor is False
    assert vp.detect("GE_UNITY_ID", "CARESCAPE^B650").profile.key == "ge_carescape"
    assert vp.detect("PhilipsIntelliBridge", "").profile.key == "philips_intellivue"


def test_coding_system_alone_is_not_a_vendor_claim():
    """MDIL says 'MDC nomenclature', which is Philips' habit, not Philips' alone."""
    detection = vp.detect("MONITOR", "WARD_A", coding_systems=["MDIL"])
    assert detection.profile.key == "philips_intellivue"
    assert detection.named_vendor is False


# ════════════════════════════════════════════════════════════════════════════
#  3. Mapping — the reason the layer exists
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("vendor", ["philips", "ge"])
def test_a_vendor_message_maps_every_vital(vendor):
    raw, bed = _message(vendor)
    _, mapped, _ = _parse(vendor, raw)
    assert set(mapped) == set(VITAL_KEYS)
    expected = dict(zip(("HR", "SBP", "DBP", "SpO2", "Temp", "RR"), bed.values()))
    for key, value in expected.items():
        assert mapped[key].value_numeric == pytest.approx(float(value))
        assert mapped[key].observation_type is ObservationType.VITAL


@pytest.mark.parametrize("vendor", ["philips", "ge"])
def test_the_same_message_without_the_profile_loses_vitals(vendor):
    """The control. If this passed, the profile layer would be doing nothing."""
    raw, _ = _message(vendor)
    _, mapped, _ = _parse("generic", raw)
    assert set(mapped) != set(VITAL_KEYS)


def test_loinc_is_never_overridden_by_a_vendor_guess():
    """A gateway configured for LOINC behaves identically with a profile on."""
    raw, bed = _message("generic")           # LOINC-coded
    _, mapped, notes = _parse("philips", raw)
    assert set(mapped) == set(VITAL_KEYS)
    # Nothing was mapped BY the profile: the shared table had already done it.
    assert [n for n in notes if n[0] == "mapped"] == []


def test_an_unmapped_identifier_is_counted_and_named():
    """This list is what a bench run exists to produce."""
    raw, _ = _message("philips", unknown_obx=True)
    _, _, notes = _parse("philips", raw)
    unmapped = [detail for kind, detail in notes if kind == "unmapped"]
    assert len(unmapped) == 1
    assert "MDC_PRESS_BLD_ART_ABP_MEAN" in unmapped[0]


def test_recognised_but_unscored_observations_are_not_reported_as_unknown():
    """Mean arterial pressure is a real reading Orb does not score. If it landed
    in the unknown list, the list would fill with noise and stop being read."""
    raw, _ = _message("ge")
    events, mapped, notes = _parse("ge", raw)
    assert not any("NBPM" in detail for _, detail in notes if _ == "unmapped")
    assert "NBPM" not in {e.observation_code for e in events if e.mapped_vital_key}
    # …and it is still STORED, just not scored.
    assert any(e.observation_code == "NBPM" for e in events)


def test_no_identifier_is_matched_as_a_substring():
    """`MDC_PRESS_BLD_NONINV_MEAN` must never satisfy a lookup for `..._SYS`."""
    profile = vp.PHILIPS_INTELLIVUE
    assert profile.lookup("150019", "MDC_PRESS_BLD_NONINV_MEAN").mapping.vital_key is None
    assert profile.lookup(None, "MDC_PRESS") is None


def test_fahrenheit_is_translated_before_the_plausibility_bounds():
    """THE silent-loss case. A GE gateway writing `DEGF` sends 98.6; Orb's own
    normaliser does not recognise that spelling, so 98.6 was labelled °C and then
    REJECTED as implausible — the temperature vanished and the patient's last
    value carried forward. Unit translation has to happen first."""
    raw, _ = _message("ge")
    raw = raw.replace("|DEGC|", "|DEGF|")
    raw = raw.replace("TEMP^TEMPERATURE^GEMS||36", "TEMP^TEMPERATURE^GEMS||98")
    _, mapped, _ = _parse("ge", raw)
    assert mapped["Temp"].value_numeric == pytest.approx(37.0, abs=0.3)
    assert mapped["Temp"].unit == "°C"


def test_philips_numeric_dimension_codes_are_translated():
    raw, _ = _message("philips")
    _, mapped, _ = _parse("philips", raw)
    assert mapped["SpO2"].unit == "%"
    assert mapped["HR"].unit == "bpm"


def test_ecg_heart_rate_beats_oximeter_pulse_rate_deterministically():
    """A Philips panel routinely carries both. Whichever wins must be a decision,
    not parse order — a poor pleth trace would otherwise change NEWS2."""
    raw, bed = _message("philips")
    ts = "20260801120000"
    pulse = f"OBX|9|NM|149530^MDC_PULS_OXIM_PULS_RATE^MDIL||55|264864^MDC_DIM_BEAT_PER_MIN^MDIL||N|||F|||{ts}\r"
    _, mapped, notes = _parse("philips", raw + pulse)
    assert mapped["HR"].value_numeric == pytest.approx(float(bed.values()[0]))
    assert any(kind == "demoted" for kind, _ in notes)
    # The demoted reading is still recorded, simply not scored.
    assert mapped["HR"].observation_name == "MDC_ECG_HEART_RATE"


def test_pulse_rate_is_used_when_there_is_no_ecg_rate():
    ts = "20260801120000"
    raw = "\r".join([
        f"MSH|^~\\&|PhilipsIntelliBridge|IntelliVue^MX450|ORB|ORB_OS|{ts}||ORU^R01|C1|P|2.5",
        "PID|1||P001^^^H^MR||Doe^Test||19700101|U",
        f"OBX|1|NM|149530^MDC_PULS_OXIM_PULS_RATE^MDIL||71|264864^MDC_DIM_BEAT_PER_MIN^MDIL||N|||F|||{ts}",
    ]) + "\r"
    _, mapped, _ = _parse("philips", raw)
    assert mapped["HR"].value_numeric == pytest.approx(71.0)


def test_implausible_vendor_values_are_still_rejected():
    """A vendor profile is not a licence to skip the plausibility gate."""
    ts = "20260801120000"
    raw = "\r".join([
        f"MSH|^~\\&|GE_UNITY_ID|CARESCAPE^B650|ORB|ORB_OS|{ts}||ORU^R01|C2|P|2.4",
        "PID|1||P001^^^H^MR||Doe^Test||19700101|U",
        f"OBX|1|NM|NBPS^NBP SYSTOLIC^GEMS||9991|mmHg||N|||F|||{ts}",
        f"OBX|2|NM|SPO2^OXYGEN SATURATION^GEMS||NaN|%||N|||F|||{ts}",
    ]) + "\r"
    _, mapped, _ = _parse("ge", raw)
    assert mapped["SBP"].value_numeric is None      # 9991 mmHg is not a blood pressure
    assert mapped["SpO2"].value_numeric is None     # a disconnected probe is not 100%


def test_strict_mode_refuses_unstandardised_mappings_and_says_so():
    """On the bench, strict mode turns the GE convention table off so the run
    produces the list of identifiers to take to the vendor."""
    raw, _ = _message("ge")
    _, mapped, notes = _parse("ge", raw, strict=True)
    assert [k for k, _ in notes].count("skipped") >= 3
    # HR/SpO2/Temp survive because Orb's OWN name table matched them — that is
    # the shared LOINC/common-name map, not the vendor profile.
    assert "SBP" not in mapped


def test_adapter_name_records_which_table_read_the_message():
    assert VendorProfileAdapter(vp.GENERIC).adapter_name == "hl7_oru"
    assert VendorProfileAdapter(vp.PHILIPS_INTELLIVUE).adapter_name == "hl7_oru:philips_intellivue"


def test_an_observer_that_raises_cannot_break_ingestion():
    def boom(kind, detail):
        raise RuntimeError("observability must never be load-bearing")

    raw, _ = _message("philips", unknown_obx=True)
    adapter = VendorProfileAdapter(vp.PHILIPS_INTELLIVUE, observer=boom)
    events = adapter.parse(raw)
    assert {e.mapped_vital_key for e in events if e.mapped_vital_key} == set(VITAL_KEYS)


# ════════════════════════════════════════════════════════════════════════════
#  4. The bench simulator speaks both
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("vendor,marker", [
    ("philips", "MDC_ECG_HEART_RATE"),
    ("ge", "NBPS^NBP SYSTOLIC"),
    ("generic", "8867-4"),
])
def test_bench_emits_each_vendors_wire_format(vendor, marker):
    raw, _ = _message(vendor)
    assert marker in raw
    assert raw.startswith("MSH|^~\\&|")
    assert "\r" in raw and "\n" not in raw       # segment terminator is CR


def test_bench_vendor_messages_still_parse_as_ordinary_hl7():
    """The vendor difference must live in the CONTENT, never in the grammar. If a
    vendor message needed a different parser, the profile layer would be in the
    wrong place."""
    for vendor in ("philips", "ge", "generic"):
        raw, _ = _message(vendor)
        message = hl7v2.parse(raw)
        assert message.message_type == "ORU" and message.trigger_event == "R01"
        assert message.first("PID").field(3, component=1) == "P001"


def test_vendor_table_prints_without_a_device_or_a_database(capsys):
    """The sheet handed to the biomedical engineer before bench day. It must run
    on a laptop with no Orb environment at all, and it must say out loud that
    nothing in it is confirmed."""
    from services.vitals_source.bench_simulator import main
    assert main(["--vendor", "philips", "--vendor-table"]) == 0
    out = capsys.readouterr().out
    assert "NOT VERIFIED" in out
    assert "MDC_ECG_HEART_RATE" in out
    assert "ASK THE VENDOR" in out


def test_bench_rejects_an_unknown_vendor_rather_than_silently_sending_loinc():
    from services.vitals_source.bench_simulator import main
    assert main(["--vendor", "siemens", "--print"]) == 2


# ════════════════════════════════════════════════════════════════════════════
#  5. Over a real socket — the Week-6 bench, in miniature
# ════════════════════════════════════════════════════════════════════════════
class _ProfileSink:
    """A sink that actually RUNS the adapter the source handed it.

    `RecordingSink` deliberately ignores the adapter (it records wire payloads),
    which would let the source pass the wrong table down and every test still
    pass. This one parses with whatever it is given, so the assertion is about
    the source's wiring rather than about the adapter in isolation.
    """

    def __init__(self):
        self.mapped: list[set] = []
        self.adapters: list[str] = []

    async def submit_payload(self, raw_payload, *, source, correlation_id=None,
                             external_message_id=None, adapter=None, hints=()):
        from services.ingestion_service import IngestionResult
        events = adapter.parse(raw_payload, source_name=source.source_id)
        self.adapters.append(adapter.adapter_name)
        self.mapped.append({e.mapped_vital_key for e in events if e.mapped_vital_key})
        return IngestionResult(correlation_id=correlation_id, parse_status="parsed",
                               received=1, inserted_vitals=len(self.mapped[-1]))

    async def submit_readings(self, readings, *, source, correlation_id=None):
        raise AssertionError("not used")


@pytest.mark.parametrize("vendor,expected_adapter", [
    ("philips", "hl7_oru:philips_intellivue"),
    ("ge", "hl7_oru:ge_carescape"),
])
async def test_auto_detection_picks_the_table_over_a_real_socket(vendor, expected_adapter):
    """Both makes on one bench day, no hardware: the point of the exercise."""
    import asyncio

    from services.vitals_source import HL7MonitorSource
    from services.vitals_source.bench_simulator import BenchMonitor

    sink = _ProfileSink()
    source = HL7MonitorSource(sink, host="127.0.0.1", port=0, profile="auto")
    await source.start()
    try:
        monitor = BenchMonitor("127.0.0.1", source.bound_port, patients=["P001"],
                               vendor=vendor, faults=["unknown_obx"], seed=5)
        await monitor.run_once()
        await asyncio.wait_for(source._queue.join(), timeout=5)
    finally:
        await source.stop()

    assert sink.adapters == [expected_adapter]
    assert sink.mapped == [set(VITAL_KEYS)]

    status = source.status()["profile"]
    assert status["mode"] == "auto"
    assert status["verified_against_device"] is False
    assert any(k.endswith(expected_adapter.split(":")[1]) for k in status["detected"])
    # The bench sent one identifier nobody has mapped; the operator must be able
    # to read it off the admin surface and send it to the vendor.
    assert status["unmapped_distinct"] == 1
    assert "action" in status


async def test_a_configured_profile_is_not_second_guessed_by_detection():
    """An operator who has been to site and named the vendor outranks a string
    match on MSH-3."""
    import asyncio

    from services.vitals_source import HL7MonitorSource
    from services.vitals_source.bench_simulator import BenchMonitor

    sink = _ProfileSink()
    source = HL7MonitorSource(sink, host="127.0.0.1", port=0, profile="ge_carescape")
    await source.start()
    try:
        monitor = BenchMonitor("127.0.0.1", source.bound_port, patients=["P001"],
                               vendor="philips", seed=5)
        await monitor.run_once()
        await asyncio.wait_for(source._queue.join(), timeout=5)
    finally:
        await source.stop()

    assert sink.adapters == ["hl7_oru:ge_carescape"]
    assert source.status()["profile"]["configured"] == "ge_carescape"
    # And the mismatch is VISIBLE rather than silent: Philips identifiers do not
    # appear in the GE table, so they land in the unknown list.
    assert source.status()["profile"]["unmapped_distinct"] > 0


async def test_an_unknown_profile_name_does_not_stop_the_feed():
    import asyncio

    from services.vitals_source import HL7MonitorSource
    from services.vitals_source.bench_simulator import BenchMonitor

    sink = _ProfileSink()
    source = HL7MonitorSource(sink, host="127.0.0.1", port=0, profile="phillips")
    await source.start()
    try:
        monitor = BenchMonitor("127.0.0.1", source.bound_port, patients=["P001"],
                               vendor="generic", seed=5)
        await monitor.run_once()
        await asyncio.wait_for(source._queue.join(), timeout=5)
    finally:
        await source.stop()

    assert sink.mapped == [set(VITAL_KEYS)]        # LOINC still maps everything
    assert source.status()["profile"]["configured"] == "generic"
