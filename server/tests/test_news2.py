"""Unit tests for the NEWS2 risk scorer (compute_news2).

compute_news2 (services/vigil_service.py) is the rule-based engine that produces
EVERY patient risk score and level the product currently ships on — the ML
models are not trained, so this IS the live clinical decision logic. Before this
file, no test imported or called it; every threshold boundary was unverified, so
a one-character inequality change could silently mis-triage patients.

These tests assert each parameter band edge, the single-parameter-3 escalation
rule, the on_oxygen +2 contribution, the ACVPU non-alert = 3 rule, and the
stable/elevated/critical banding, checked against the published UK NEWS2
(SpO2 Scale 1) reference table. Treat this file as a release gate.

Reference: Royal College of Physicians, National Early Warning Score (NEWS) 2.

Band table implemented by compute_news2 (points per parameter):

  Respiration:  <=8 -> 3 | 9-11 -> 1 | 12-20 -> 0 | 21-24 -> 2 | >=25 -> 3
  SpO2 (Sc.1):  >=96 -> 0 | 94-95 -> 1 | 92-93 -> 2 | <=91 -> 3
  On oxygen:    +2
  Systolic BP:  <=90 -> 3 | 91-100 -> 2 | 101-110 -> 1 | 111-219 -> 0 | >=220 -> 3
  Pulse (HR):   <=40 -> 3 | 41-50 -> 1 | 51-90 -> 0 | 91-110 -> 1 | 111-130 -> 2 | >=131 -> 3
  Temperature:  <=35.0 -> 3 | 35.1-36.0 -> 1 | 36.1-38.0 -> 0 | 38.1-39.0 -> 1 | >=39.1 -> 2
  Consciousness: Alert -> 0 | (Confusion/Voice/Pain/Unresponsive) -> 3

  Banding from raw aggregate: >=7 critical | >=5 OR any single param==3 elevated | else stable
"""
import sys
from pathlib import Path

import pytest

# Add server root to path (mirrors the other test modules).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.vigil_service import compute_news2, compute_mews


# A set of vitals that scores 0 on every parameter — the baseline we perturb
# one parameter at a time so each assertion isolates a single band.
NORMAL = dict(hr=70, sbp=120, rr=16, temp=37.0, spo2=98, consciousness="alert", on_oxygen=False)


def score_with(**overrides):
    """compute_news2 on NORMAL vitals with the given parameter(s) overridden.

    Returns the full result dict; raw_news2 is the un-normalized integer NEWS2
    aggregate, which is what the clinical band/escalation decision is taken from.
    """
    vitals = {**NORMAL, **overrides}
    return compute_news2(**vitals)


def test_all_normal_scores_zero():
    res = score_with()
    assert res["raw_news2"] == 0
    assert res["level"] == "stable"
    assert res["single_param_3"] is False
    assert res["scoring_model"] == "NEWS2"


# ── Respiration rate bands ────────────────────────────────────────────────
@pytest.mark.parametrize("rr,points", [
    (8, 3),    # <=8 bradypnea
    (5, 3),
    (9, 1), (11, 1),    # 9-11 low
    (12, 0), (16, 0), (20, 0),    # 12-20 normal
    (21, 2), (24, 2),    # 21-24 tachypnea
    (25, 3), (30, 3),    # >=25 severe
])
def test_respiration_bands(rr, points):
    assert score_with(rr=rr)["raw_news2"] == points


# ── SpO2 bands (Scale 1) ──────────────────────────────────────────────────
@pytest.mark.parametrize("spo2,points", [
    (98, 0), (96, 0),    # >=96 normal
    (95, 1), (94, 1),    # 94-95 mild
    (93, 2), (92, 2),    # 92-93 hypoxia
    (91, 3), (85, 3),    # <=91 severe
])
def test_spo2_bands(spo2, points):
    assert score_with(spo2=spo2)["raw_news2"] == points


# ── Supplemental oxygen (+2) ──────────────────────────────────────────────
def test_on_oxygen_adds_two():
    assert score_with(on_oxygen=True)["raw_news2"] == 2


def test_on_oxygen_stacks_with_other_params():
    # On O2 (+2) plus hypoxia SpO2 92 (+2) = 4, not single-param-3.
    res = score_with(on_oxygen=True, spo2=92)
    assert res["raw_news2"] == 4
    assert res["single_param_3"] is False
    assert res["level"] == "stable"  # 4 < 5 and no single param == 3


# ── Systolic BP bands ─────────────────────────────────────────────────────
@pytest.mark.parametrize("sbp,points", [
    (90, 3), (80, 3),    # <=90 severe hypotension
    (91, 2), (100, 2),    # 91-100 hypotension
    (101, 1), (110, 1),    # 101-110 low
    (111, 0), (120, 0), (219, 0),    # 111-219 normal
    (220, 3), (230, 3),    # >=220 severe hypertension
])
def test_systolic_bp_bands(sbp, points):
    assert score_with(sbp=sbp)["raw_news2"] == points


# ── Pulse (HR) bands ──────────────────────────────────────────────────────
@pytest.mark.parametrize("hr,points", [
    (40, 3), (30, 3),    # <=40 severe bradycardia
    (41, 1), (50, 1),    # 41-50 bradycardia
    (51, 0), (70, 0), (90, 0),    # 51-90 normal
    (91, 1), (110, 1),    # 91-110 mild tachycardia
    (111, 2), (130, 2),    # 111-130 tachycardia
    (131, 3), (160, 3),    # >=131 severe tachycardia
])
def test_pulse_bands(hr, points):
    assert score_with(hr=hr)["raw_news2"] == points


# ── Temperature bands ─────────────────────────────────────────────────────
@pytest.mark.parametrize("temp,points", [
    (35.0, 3), (34.0, 3),    # <=35.0 hypothermia
    (35.1, 1), (36.0, 1),    # 35.1-36.0 low
    (36.1, 0), (37.0, 0), (38.0, 0),    # 36.1-38.0 normal
    (38.1, 1), (39.0, 1),    # 38.1-39.0 fever
    (39.1, 2), (40.0, 2),    # >=39.1 high fever
])
def test_temperature_bands(temp, points):
    assert score_with(temp=temp)["raw_news2"] == points


# ── Consciousness (ACVPU) ─────────────────────────────────────────────────
@pytest.mark.parametrize("consciousness", ["alert", "Alert", "A", "a", ""])
def test_consciousness_alert_scores_zero(consciousness):
    assert score_with(consciousness=consciousness)["raw_news2"] == 0


@pytest.mark.parametrize("consciousness", ["confusion", "voice", "pain", "unresponsive", "V", "P", "U"])
def test_consciousness_non_alert_scores_three(consciousness):
    res = score_with(consciousness=consciousness)
    assert res["raw_news2"] == 3
    assert res["single_param_3"] is True


# ── Single-parameter-3 escalation rule ────────────────────────────────────
def test_single_param_3_escalates_to_elevated():
    # One parameter scoring 3 (HR 35 -> severe bradycardia) with total < 5 must
    # still escalate the patient to at least 'elevated' per the NEWS2 rule.
    res = score_with(hr=35)
    assert res["raw_news2"] == 3
    assert res["single_param_3"] is True
    assert res["level"] == "elevated"


def test_score_three_without_single_param_3_stays_stable():
    # Aggregate of 3 made up of non-3 contributions (RR 9 -> 1, SpO2 94 -> 1,
    # HR 95 -> 1) is total 3 with NO single parameter == 3, so it must remain
    # 'stable' (proves the escalation keys off the per-parameter 3, not the total).
    res = score_with(rr=9, spo2=94, hr=95)
    assert res["raw_news2"] == 3
    assert res["single_param_3"] is False
    assert res["level"] == "stable"


# ── Aggregate banding: stable / elevated / critical ───────────────────────
def test_band_stable_below_five():
    # RR 21 (2) + SpO2 94 (1) + HR 95 (1) = 4, no single param 3 -> stable.
    res = score_with(rr=21, spo2=94, hr=95)
    assert res["raw_news2"] == 4
    assert res["level"] == "stable"


def test_band_elevated_at_five():
    # RR 21 (2) + SpO2 92 (2) + HR 95 (1) = 5 -> elevated.
    res = score_with(rr=21, spo2=92, hr=95)
    assert res["raw_news2"] == 5
    assert res["level"] == "elevated"


def test_band_critical_at_seven():
    # RR 25 (3) + SpO2 92 (2) + HR 111 (2) = 7 -> critical.
    res = score_with(rr=25, spo2=92, hr=111)
    assert res["raw_news2"] == 7
    assert res["level"] == "critical"


def test_band_critical_high_aggregate():
    # Multiple severe parameters: RR 30 (3) + SpO2 85 (3) + O2 (2) + SBP 80 (3)
    # + HR 160 (3) + temp 34 (3) + unresponsive (3) = 20.
    res = score_with(rr=30, spo2=85, on_oxygen=True, sbp=80, hr=160,
                     temp=34.0, consciousness="unresponsive")
    assert res["raw_news2"] == 20
    assert res["level"] == "critical"


# ── Normalized 0-1 index ──────────────────────────────────────────────────
def test_normalized_index_does_not_flatten_high_scores():
    # The normalized 'score' must NOT compress the sickest patients to the same
    # value: a raw-7 critical and a raw-14 patient must yield distinct indices
    # (regression guard for the old min(1.0, score/10) flattening bug).
    low = score_with(rr=25, spo2=92, hr=111)            # raw 7
    high = score_with(rr=30, spo2=85, sbp=80, hr=160)   # raw 12
    assert low["raw_news2"] < high["raw_news2"]
    assert low["score"] < high["score"]
    assert low["score"] <= 1.0 and high["score"] <= 1.0


def test_normalized_index_capped_at_one():
    res = score_with(rr=30, spo2=85, on_oxygen=True, sbp=80, hr=160,
                     temp=34.0, consciousness="unresponsive")
    assert res["score"] == 1.0  # raw 20 / 20.0 max aggregate


# ── None handling (missing vitals must not crash) ─────────────────────────
def test_missing_parameters_are_skipped():
    res = compute_news2(hr=None, sbp=None, rr=None, temp=None, spo2=None)
    assert res["raw_news2"] == 0
    assert res["level"] == "stable"


# ── compute_mews deprecated alias parity ──────────────────────────────────
def test_compute_mews_alias_matches_news2_for_shared_params():
    # The deprecated alias drops on_oxygen and defaults consciousness='alert';
    # for inputs where neither matters it must equal compute_news2.
    args = dict(hr=95, sbp=100, rr=21, temp=38.5, spo2=94)
    mews = compute_mews(**args)
    news2 = compute_news2(**args, consciousness="alert", on_oxygen=False)
    assert mews["raw_news2"] == news2["raw_news2"]
    assert mews["level"] == news2["level"]


# ── COPD / NEWS2 Scale 2 (target SpO2 88-92%) ─────────────────────────────
# Scale 2 is for COPD / chronic type-2 respiratory failure; selected per-patient
# via patients.on_scale_2. Reference: RCP NEWS2 Scale 2 SpO2 table.
def test_copd_scale2_normal_spo2_does_not_escalate():
    # A COPD patient at their normal 90% SpO2 must score 0 on Scale 2
    # (Scale 1 would wrongly give 2 → alarm fatigue).
    res = compute_news2(**{**NORMAL, "spo2": 90}, on_scale_2=True)
    assert res["raw_news2"] == 0
    assert res["spo2_scale"] == 2
    # Contrast: the same reading on Scale 1 scores 3 (severe hypoxia, <92%) and
    # triggers the single-parameter-3 escalation — a false alarm for a COPD patient.
    scale1 = compute_news2(**{**NORMAL, "spo2": 90})
    assert scale1["raw_news2"] == 3
    assert scale1["single_param_3"] is True


@pytest.mark.parametrize("spo2,points", [
    (83, 3), (84, 2), (85, 2), (86, 1), (87, 1), (88, 0), (92, 0), (95, 0),
])
def test_scale2_bands_on_air(spo2, points):
    # On air, >=93 scores 0; the low bands mirror the RCP Scale 2 table.
    assert compute_news2(**{**NORMAL, "spo2": spo2}, on_scale_2=True)["raw_news2"] == points


@pytest.mark.parametrize("spo2,spo2_points", [
    (93, 1), (94, 1), (95, 2), (96, 2), (97, 3), (99, 3),
])
def test_scale2_over_oxygenation_on_o2(spo2, spo2_points):
    # On oxygen, high SpO2 is scored (over-oxygenation is dangerous in COPD).
    # Total = SpO2 sub-score + 2 for the supplemental-oxygen parameter.
    res = compute_news2(**{**NORMAL, "spo2": spo2, "on_oxygen": True}, on_scale_2=True)
    assert res["raw_news2"] == spo2_points + 2


# ── COVERAGE HONESTY over ALL SEVEN parameters ────────────────────────────
# THE DEFECT (fixed 2026-08-14): `complete` / `missing_parameters` tracked the
# five NUMERIC vitals only. ACVPU consciousness and supplemental oxygen — the
# two biggest single escalators on the RCP table (+3 and +2) and the two a
# machine can NEVER supply, because both are human assessments — were absent
# from the coverage set. So an observation set with no ACVPU assessment scored
# Alert (0 points) and came back `complete: true`: a 5-of-7 NEWS2 presented as a
# whole one. In the ingestion lane the pilot will actually run, where a bedside
# monitor sends numbers and nothing else, that was the NORMAL case.
#
# The contract: `None` means NOT ASSESSED. It scores the same 0 points an
# unmeasured heart rate does — nothing about the arithmetic changes — but it is
# reported as missing. An explicit value, INCLUDING 'alert' or False, is a
# positive statement that a human looked.

def test_a_monitor_only_set_is_not_complete():
    """Five perfect numbers and no human assessment is not a complete NEWS2."""
    res = compute_news2(hr=70, sbp=120, rr=16, temp=37.0, spo2=98)
    assert res["raw_news2"] == 0
    assert res["complete"] is False
    assert set(res["missing_parameters"]) == {"Consciousness (ACVPU)", "Supplemental O2"}
    assert res["coverage_note"] and "INCOMPLETE" in res["coverage_note"]


def test_missing_acvpu_alone_is_reported():
    res = compute_news2(hr=70, sbp=120, rr=16, temp=37.0, spo2=98, on_oxygen=False)
    assert res["complete"] is False
    assert res["missing_parameters"] == ["Consciousness (ACVPU)"]
    assert "Consciousness (ACVPU)" in res["coverage_note"]


def test_missing_supplemental_oxygen_alone_is_reported():
    res = compute_news2(hr=70, sbp=120, rr=16, temp=37.0, spo2=98, consciousness="alert")
    assert res["complete"] is False
    assert res["missing_parameters"] == ["Supplemental O2"]
    assert "Supplemental O2" in res["coverage_note"]


def test_an_explicit_normal_assessment_counts_as_measured():
    """'alert' and False are FINDINGS, not absences — a nurse looked and charted
    them. Only these seven-parameter sets may claim to be complete."""
    res = compute_news2(hr=70, sbp=120, rr=16, temp=37.0, spo2=98,
                        consciousness="alert", on_oxygen=False)
    assert res["complete"] is True
    assert res["missing_parameters"] == []
    assert res["coverage_note"] is None
    assert res["drivers"] == ["All vitals within normal range"]


def test_a_blank_acvpu_string_is_an_absence_not_an_assessment():
    """An empty ACVPU field is a form nobody filled in, not a finding of Alert."""
    res = compute_news2(hr=70, sbp=120, rr=16, temp=37.0, spo2=98,
                        consciousness="   ", on_oxygen=False)
    assert res["raw_news2"] == 0          # unchanged arithmetic
    assert res["complete"] is False
    assert res["missing_parameters"] == ["Consciousness (ACVPU)"]


def test_the_coverage_denominator_is_seven():
    """The partial-set driver line has to count against all seven parameters —
    reporting '3/5' on a set that is really 3-of-7 is the same lie one level
    down."""
    res = compute_news2(hr=70, sbp=120, rr=16, temp=None, spo2=None)
    assert res["drivers"] == ["Within range on what was measured (3/7 parameters)"]


def test_an_unassessed_parameter_scores_zero_exactly_as_before():
    """Coverage honesty must not have moved a single point. `None` and an
    explicit normal finding produce the SAME aggregate — the flag is the only
    thing that differs."""
    unassessed = compute_news2(hr=95, sbp=100, rr=21, temp=38.5, spo2=94)
    documented = compute_news2(hr=95, sbp=100, rr=21, temp=38.5, spo2=94,
                               consciousness="alert", on_oxygen=False)
    assert unassessed["raw_news2"] == documented["raw_news2"]
    assert unassessed["level"] == documented["level"]
    assert unassessed["score"] == documented["score"]
    assert unassessed["complete"] is False and documented["complete"] is True


# ═════════════════════════════════════════════════════════════════════════════
#  THE RCP TABLE MUST NOT MOVE — full-range sweep, every parameter
# ═════════════════════════════════════════════════════════════════════════════
# Added 2026-08-15 alongside the driver-label fix (D5). The labels attached to
# each band were rewritten so that "severe" describes the patient rather than
# the point band ("Severe hypoxia (SpO2 91%)" was on screen on a real ward).
# That edit touched every 3-point branch in `compute_news2`, which is exactly the
# code path a mis-typed inequality hides in.
#
# The scoring is the PINNED AUTHORITATIVE VALUE: Orb computes it, no model may
# recompute it, and no wording change is allowed to cost a single point. The
# tests above check the band EDGES; these walk the full plausible range of each
# parameter against an independent transcription of the RCP table, because the
# label defect was uniform ACROSS a band, not at its edge — and the fix for it
# lives inside the same branches.
#
# Reference: Royal College of Physicians, NEWS2 (2017), observation chart.

def _rcp_respiration(rr):
    if rr <= 8: return 3
    if rr <= 11: return 1
    if rr <= 20: return 0
    if rr <= 24: return 2
    return 3


def _rcp_spo2_scale_1(spo2):
    if spo2 >= 96: return 0
    if spo2 >= 94: return 1
    if spo2 >= 92: return 2
    return 3


def _rcp_spo2_scale_2(spo2, on_oxygen):
    if spo2 <= 83: return 3
    if spo2 <= 85: return 2
    if spo2 <= 87: return 1
    if spo2 <= 92: return 0
    if not on_oxygen: return 0      # >=93 breathing air is fine on Scale 2
    if spo2 <= 94: return 1
    if spo2 <= 96: return 2
    return 3


def _rcp_systolic(sbp):
    if sbp <= 90: return 3
    if sbp <= 100: return 2
    if sbp <= 110: return 1
    if sbp <= 219: return 0
    return 3


def _rcp_pulse(hr):
    if hr <= 40: return 3
    if hr <= 50: return 1
    if hr <= 90: return 0
    if hr <= 110: return 1
    if hr <= 130: return 2
    return 3


def _rcp_temperature(temp):
    if temp <= 35.0: return 3
    if temp <= 36.0: return 1
    if temp <= 38.0: return 0
    if temp <= 39.0: return 1
    return 2


def _sweep_points(key, values, reference, **extra):
    """Every value, scored in isolation against the RCP reference. Reports ALL
    divergences: a shifted inequality moves a whole band, and one arbitrary
    value from it is a much worse bug report than the band."""
    wrong = []
    for v in values:
        got = score_with(**{key: v}, **extra)["raw_news2"]
        want = reference(v)
        if got != want:
            wrong.append(f"  {key}={v}{'' if not extra else f' {extra}'}: "
                         f"scored {got}, RCP table says {want}")
    assert not wrong, (
        f"the NEWS2 scoring table moved on {len(wrong)} value(s) — this is the "
        f"pinned clinical number every band, alert and escalation is taken "
        f"from:\n" + "\n".join(wrong[:25]))


def test_respiration_full_range_matches_rcp():
    _sweep_points("rr", range(1, 61), _rcp_respiration)


def test_spo2_scale_1_full_range_matches_rcp():
    _sweep_points("spo2", range(60, 101), _rcp_spo2_scale_1)


def test_spo2_scale_2_on_air_full_range_matches_rcp():
    _sweep_points("spo2", range(60, 101),
                  lambda s: _rcp_spo2_scale_2(s, False), on_scale_2=True)


def test_spo2_scale_2_on_oxygen_full_range_matches_rcp():
    """+2 for the oxygen itself, on top of the saturation band."""
    _sweep_points("spo2", range(60, 101),
                  lambda s: _rcp_spo2_scale_2(s, True) + 2,
                  on_scale_2=True, on_oxygen=True)


def test_systolic_bp_full_range_matches_rcp():
    _sweep_points("sbp", range(40, 261), _rcp_systolic)


def test_pulse_full_range_matches_rcp():
    _sweep_points("hr", range(20, 221), _rcp_pulse)


def test_temperature_full_range_matches_rcp():
    _sweep_points("temp", [round(i / 10, 1) for i in range(280, 431)],
                  _rcp_temperature)


def test_the_single_parameter_3_rule_survives_the_full_range():
    """`single_param_3` is what turns a NEWS2 of 3 into an 'elevated' patient and
    fires an alert the aggregate alone would never raise. It is set inside the
    same branches the labels were rewritten in."""
    wrong = []
    for key, values, reference in [
        ("rr", range(1, 61), _rcp_respiration),
        ("spo2", range(60, 101), _rcp_spo2_scale_1),
        ("sbp", range(40, 261), _rcp_systolic),
        ("hr", range(20, 221), _rcp_pulse),
        ("temp", [round(i / 10, 1) for i in range(280, 431)], _rcp_temperature),
    ]:
        for v in values:
            res = score_with(**{key: v})
            want = reference(v) == 3
            if res["single_param_3"] is not want:
                wrong.append(f"  {key}={v}: single_param_3={res['single_param_3']}, "
                             f"expected {want}")
            if want and res["level"] == "stable":
                wrong.append(f"  {key}={v}: scores 3 yet reads 'stable'")
    assert not wrong, "\n".join(wrong[:25])
