"""THE NUMBER A NURSE SEES MUST EARN THE POINTS SHOWN BESIDE IT.

WHAT WAS WRONG (found 2026-10-01 by the adversarial fact-check of the PhysioNet
NEWS2 replay, docs/fundraising/research_2026-10-01/R5_real_data_validation_
VERIFY.md section 8; ruled 2026-10-02). `compute_news2` banded the RAW float and
printed it rounded half-to-even, so a value between two chart numbers was
scored by whichever comparison operator its branch happened to use:

    RR 20.4   -> 2 points, "Tachypnea (RR 20)"       RCP chart: RR 20 = 0
    SBP 90.4  -> 2 points, "Hypotension (SBP 90)"    RCP chart: SBP 90 = 3
    HR 40.3   -> 1 point,  "Bradycardia (HR 40)"     RCP chart: HR 40 = 3

The upper edges over-scored. The LOWER edges under-scored and silently dropped
the single-parameter-3 escalation on a patient whose printed number earns it.
Vitals arrive as floats from the monitor lane, the simulator and the replay's
hourly medians, so none of this needed a contrived input.

THE RULE: every numeric vital is rounded HALF-UP to the RCP chart's precision
(whole numbers for RR, SpO2, SBP and HR; tenths of a degree for temperature)
BEFORE banding. That one number earns the points, picks the word, is printed in
the label and is returned as breakdown `value`.

WHAT THIS FILE HOLDS, and why each part exists:

  1. A literal table of every band edge of every parameter, fed fractional
     values on both sides of the half. Literals rather than a computed
     expectation, because the dense sweep below is rounding-direction blind:
     half-to-even satisfies "points agree with the printed number" just as well,
     and it rounds RR 8.5 to 8 (3 points) where a person reading a monitor
     writes 9 (1 point). Only a literal can tell those apart.
  2. A dense sweep of every parameter at 0.01 (temperature 0.005) asserting the
     invariant itself: the RCP table applied to the PRINTED value equals the
     points awarded, and the printed value is the breakdown `value`.
  3. The clinical stake, stated directly: the low-side edges keep their
     single-parameter-3 escalation.
  4. The surfaces. Every place that prints a vital beside a NEWS2 score must
     print the number that was scored: the shared formatter
     (services/vital_charting.py) against the scorer, the I-PASS sheet and the
     Tier 1 prompt. The three Relay alert lanes are tested end to end in
     test_wedge_loop.py.

The RCP table is transcribed here again rather than imported from
test_news2.py or the service: a test that imports the rule it checks asserts
only that the module can read itself.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.vigil_service import compute_news2


# Scores 0 on every parameter; each check perturbs one parameter only.
NORMAL = dict(hr=70, sbp=120, rr=16, temp=37.0, spo2=98,
              consciousness="alert", on_oxygen=False)

KEY_TO_PARAMETER = {"rr": "Respiratory rate", "sbp": "Systolic BP",
                    "hr": "Heart rate", "temp": "Temperature"}


def _spo2_parameter(extra):
    return "SpO2 (Scale 2)" if extra.get("on_scale_2") else "SpO2"


def _row(key, raw, extra):
    """The (result, breakdown row) for one perturbed parameter."""
    res = compute_news2(**{**NORMAL, key: raw, **extra})
    name = _spo2_parameter(extra) if key == "spo2" else KEY_TO_PARAMETER[key]
    for b in res["breakdown"]:
        if b["parameter"] == name:
            return res, b
    raise AssertionError(f"{name!r} absent from breakdown: {res['breakdown']}")


def _printed(note):
    """The number the label prints. The lookbehind keeps the 2 in "SpO2" from
    being read as the value."""
    m = re.search(r"(?<![A-Za-z0-9])-?\d+(?:\.\d+)?", note.split("(", 1)[1])
    return float(m.group())


# ── The RCP NEWS2 (2017) observation chart, by printed value ────────────────
def _rcp_rr(v):
    if v <= 8: return 3
    if v <= 11: return 1
    if v <= 20: return 0
    if v <= 24: return 2
    return 3


def _rcp_spo2_scale_1(v):
    if v >= 96: return 0
    if v >= 94: return 1
    if v >= 92: return 2
    return 3


def _rcp_spo2_scale_2(v, on_oxygen):
    if v <= 83: return 3
    if v <= 85: return 2
    if v <= 87: return 1
    if v <= 92: return 0
    if not on_oxygen: return 0
    if v <= 94: return 1
    if v <= 96: return 2
    return 3


def _rcp_sbp(v):
    if v <= 90: return 3
    if v <= 100: return 2
    if v <= 110: return 1
    if v <= 219: return 0
    return 3


def _rcp_hr(v):
    if v <= 40: return 3
    if v <= 50: return 1
    if v <= 90: return 0
    if v <= 110: return 1
    if v <= 130: return 2
    return 3


def _rcp_temp(v):
    if v <= 35.0: return 3
    if v <= 36.0: return 1
    if v <= 38.0: return 0
    if v <= 39.0: return 1
    return 2


# ═════════════════════════════════════════════════════════════════════════════
#  1. Every band edge, both sides of the half, as literals
# ═════════════════════════════════════════════════════════════════════════════
S2_AIR = {"on_scale_2": True}
S2_O2 = {"on_scale_2": True, "on_oxygen": True}

# (key, raw, extra, charted value, points). Rows marked HE are the ones
# Python's half-to-even round()/format() would print and score differently.
EDGES = [
    # Respiratory rate: 8|9, 11|12, 20|21, 24|25
    ("rr", 8.4, {}, 8, 3),
    ("rr", 8.5, {}, 9, 1),             # HE: 8, 3 points
    ("rr", 11.4, {}, 11, 1),
    ("rr", 11.5, {}, 12, 0),
    ("rr", 20.4, {}, 20, 0),           # was 2 points, "Tachypnea (RR 20)"
    ("rr", 20.5, {}, 21, 2),           # HE: 20, 0 points
    ("rr", 24.4, {}, 24, 2),
    ("rr", 24.5, {}, 25, 3),           # HE: 24, 2 points
    # SpO2 Scale 1: 91|92, 93|94, 95|96
    ("spo2", 91.4, {}, 91, 3),
    ("spo2", 91.5, {}, 92, 2),         # was 3 points, "SpO2 92%"
    ("spo2", 93.4, {}, 93, 2),
    ("spo2", 93.5, {}, 94, 1),
    ("spo2", 95.4, {}, 95, 1),
    ("spo2", 95.5, {}, 96, 0),         # was 1 point, "SpO2 96%"
    # SpO2 Scale 2 on air: 83|84, 85|86, 87|88, and 92|93 scores 0 either side
    ("spo2", 83.4, S2_AIR, 83, 3),     # was 2 points, single-param-3 lost
    ("spo2", 83.5, S2_AIR, 84, 2),
    ("spo2", 85.4, S2_AIR, 85, 2),
    ("spo2", 85.5, S2_AIR, 86, 1),
    ("spo2", 87.4, S2_AIR, 87, 1),     # was 0 points; chart 87 = 1
    ("spo2", 87.5, S2_AIR, 88, 0),
    ("spo2", 92.5, S2_AIR, 93, 0),
    # SpO2 Scale 2 on oxygen: 92|93, 94|95, 96|97 (the +2 for the oxygen is
    # its own breakdown row, so these are the saturation points alone)
    ("spo2", 92.4, S2_O2, 92, 0),      # was 1 point, "on O2 (92%)"
    ("spo2", 92.5, S2_O2, 93, 1),      # HE: 92, 0 points
    ("spo2", 94.4, S2_O2, 94, 1),
    ("spo2", 94.5, S2_O2, 95, 2),      # HE: 94, 1 point
    ("spo2", 96.4, S2_O2, 96, 2),
    ("spo2", 96.5, S2_O2, 97, 3),      # HE: 96, 2 points
    # Systolic BP: 90|91, 100|101, 110|111, 219|220
    ("sbp", 90.4, {}, 90, 3),          # was 2 points, single-param-3 lost
    ("sbp", 90.5, {}, 91, 2),          # HE: 90, 3 points
    ("sbp", 100.4, {}, 100, 2),
    ("sbp", 100.5, {}, 101, 1),        # HE: 100, 2 points
    ("sbp", 110.4, {}, 110, 1),
    ("sbp", 110.5, {}, 111, 0),        # HE: 110, 1 point
    ("sbp", 219.4, {}, 219, 0),        # was 3 points, "SBP 219"
    ("sbp", 219.5, {}, 220, 3),
    # Heart rate: 40|41, 50|51, 90|91, 110|111, 130|131
    ("hr", 40.3, {}, 40, 3),           # was 1 point, single-param-3 lost
    ("hr", 40.5, {}, 41, 1),           # HE: 40, 3 points
    ("hr", 50.4, {}, 50, 1),
    ("hr", 50.5, {}, 51, 0),           # HE: 50, 1 point
    ("hr", 90.4, {}, 90, 0),           # was 1 point, "Mild tachycardia (HR 90)"
    ("hr", 90.5, {}, 91, 1),           # HE: 90, 0 points
    ("hr", 110.4, {}, 110, 1),
    ("hr", 110.5, {}, 111, 2),         # HE: 110, 1 point
    ("hr", 130.4, {}, 130, 2),
    ("hr", 130.5, {}, 131, 3),         # HE: 130, 2 points
    # Temperature, to 0.1 C: 35.0|35.1, 36.0|36.1, 38.0|38.1, 39.0|39.1.
    # x.x5 is stored in binary just BELOW the half (38.05 is 38.0499...), so a
    # rounding done on the binary value prints 38.0. The device sent 38.05.
    ("temp", 35.04, {}, 35.0, 3),      # was 1 point, single-param-3 lost
    ("temp", 35.05, {}, 35.1, 1),
    ("temp", 36.04, {}, 36.0, 1),
    ("temp", 36.05, {}, 36.1, 0),
    ("temp", 38.04, {}, 38.0, 0),      # was 1 point, "Fever (38.0°C)"
    ("temp", 38.05, {}, 38.1, 1),      # HE and binary rounding: 38.0
    ("temp", 39.04, {}, 39.0, 1),
    ("temp", 39.05, {}, 39.1, 2),
]


@pytest.mark.parametrize("key,raw,extra,charted,points", EDGES,
                         ids=[f"{k}={r}{'-s2' if e.get('on_scale_2') else ''}"
                              f"{'-o2' if e.get('on_oxygen') else ''}"
                              for k, r, e, _c, _p in EDGES])
def test_every_band_edge_scores_and_prints_the_charted_value(
        key, raw, extra, charted, points):
    res, b = _row(key, raw, extra)
    assert b["value"] == charted, (
        f"{key}={raw} should be scored as {charted}, breakdown says {b['value']}")
    assert b["points"] == points, (
        f"{key}={raw} (charted {charted}) earns {points} on the RCP chart, "
        f"scored {b['points']}: {b}")
    if points:
        assert b["note"] is not None and _printed(b["note"]) == charted, (
            f"{key}={raw}: the label prints a different number from the one "
            f"scored: {b['note']!r}")
        assert b["note"] in res["drivers"], (
            "the driver strip and the breakdown disagree; they are one string")
    else:
        assert b["note"] is None, f"{key}={raw} scores 0 yet carries {b['note']!r}"


def test_the_edge_table_covers_every_band_edge():
    """The table is only as good as its coverage. Every boundary on the RCP
    chart must appear with a value on each side of it."""
    boundaries = {
        ("rr", ()): {8, 11, 20, 24},
        ("spo2", ()): {91, 93, 95},
        ("spo2", (("on_scale_2", True),)): {83, 85, 87},
        ("spo2", (("on_oxygen", True), ("on_scale_2", True))): {92, 94, 96},
        ("sbp", ()): {90, 100, 110, 219},
        ("hr", ()): {40, 50, 90, 110, 130},
        ("temp", ()): {35.0, 36.0, 38.0, 39.0},
    }
    step = {"temp": 0.1}
    missing = []
    for (key, extra), edges in boundaries.items():
        rows = [(r, c) for k, r, e, c, _p in EDGES
                if k == key and tuple(sorted(e.items())) == extra]
        for edge in edges:
            upper = round(edge + step.get(key, 1), 1)
            if not any(c == edge and r != c for r, c in rows):
                missing.append(f"{key} {dict(extra)}: nothing rounding DOWN to {edge}")
            if not any(c == upper for _r, c in rows):
                missing.append(f"{key} {dict(extra)}: nothing rounding UP to {upper}")
    assert not missing, "\n".join(missing)


# ═════════════════════════════════════════════════════════════════════════════
#  2. The invariant, densely: the chart applied to the printed number
# ═════════════════════════════════════════════════════════════════════════════
INTEGER_PARAMETERS = [
    ("rr", [i / 100 for i in range(0, 6001)], {}, _rcp_rr),
    ("spo2", [i / 100 for i in range(6000, 10001)], {}, _rcp_spo2_scale_1),
    ("spo2", [i / 100 for i in range(6000, 10001)], S2_AIR,
     lambda v: _rcp_spo2_scale_2(v, False)),
    ("spo2", [i / 100 for i in range(6000, 10001)], S2_O2,
     lambda v: _rcp_spo2_scale_2(v, True)),
    ("sbp", [i / 20 for i in range(800, 5201)], {}, _rcp_sbp),
    ("hr", [i / 20 for i in range(400, 4401)], {}, _rcp_hr),
]


@pytest.mark.parametrize("key,values,extra,rcp", INTEGER_PARAMETERS,
                         ids=["rr", "spo2-s1", "spo2-s2-air", "spo2-s2-o2",
                              "sbp", "hr"])
def test_no_whole_number_vital_prints_a_value_that_contradicts_its_points(
        key, values, extra, rcp):
    bad = []
    for raw in values:
        _res, b = _row(key, raw, extra)
        v = b["value"]
        if not isinstance(v, int) or abs(v - raw) > 0.5:
            bad.append(f"  {key}={raw}: value {v!r} is not the whole number charted")
        elif rcp(v) != b["points"]:
            bad.append(f"  {key}={raw}: shows {v}, RCP gives {rcp(v)}, "
                       f"scored {b['points']} {b['note']!r}")
        elif b["note"] and _printed(b["note"]) != v:
            bad.append(f"  {key}={raw}: scored {v}, label prints {b['note']!r}")
    assert not bad, (f"{len(bad)} of {len(values)} readings contradict the "
                     f"chart:\n" + "\n".join(bad[:25]))


def test_no_temperature_prints_a_value_that_contradicts_its_points():
    values = [i / 1000 for i in range(28000, 43001, 5)]
    bad = []
    for raw in values:
        _res, b = _row("temp", raw, {})
        v = b["value"]
        if v != float(f"{v:.1f}") or abs(v - raw) > 0.05 + 1e-9:
            bad.append(f"  temp={raw}: value {v!r} is not the tenth charted")
        elif _rcp_temp(v) != b["points"]:
            bad.append(f"  temp={raw}: shows {v}, RCP gives {_rcp_temp(v)}, "
                       f"scored {b['points']} {b['note']!r}")
        elif b["note"] and _printed(b["note"]) != v:
            bad.append(f"  temp={raw}: scored {v}, label prints {b['note']!r}")
    assert not bad, (f"{len(bad)} of {len(values)} readings contradict the "
                     f"chart:\n" + "\n".join(bad[:25]))


# ═════════════════════════════════════════════════════════════════════════════
#  3. The clinical stake
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("overrides", [
    dict(sbp=90.4), dict(rr=8.3), dict(hr=40.3), dict(temp=35.04),
    dict(spo2=83.4, on_scale_2=True),
], ids=["sbp-90.4", "rr-8.3", "hr-40.3", "temp-35.04", "spo2-s2-83.4"])
def test_low_side_edges_keep_the_single_parameter_escalation(overrides):
    """Each of these prints a number that scores 3 on the chart, and before
    the rule each scored 1 or 2 with no escalation: an under-score, the
    direction that matters."""
    res = compute_news2(**{**NORMAL, **overrides})
    assert res["single_param_3"] is True, res
    assert res["level"] == "elevated", res


def test_a_rate_charted_as_normal_raises_no_driver():
    """RR 20.4 is charted as 20, which is normal. Before the rule it raised
    "Tachypnea (RR 20)" and 2 points; in the replay that is alert burden
    invented at the band edge."""
    res = compute_news2(**{**NORMAL, "rr": 20.4})
    assert res["raw_news2"] == 0
    assert res["level"] == "stable"
    assert res["drivers"] == ["All vitals within normal range"]


# ═════════════════════════════════════════════════════════════════════════════
#  4. Every surface that prints a vital beside a NEWS2 score prints this number
# ═════════════════════════════════════════════════════════════════════════════
# The alert lanes are tested end to end in test_wedge_loop.py (the persisted
# Relay text). These are the shared formatter and the two document/prompt
# formatters that print a vital on the same page as the NEWS2 drivers.

@pytest.mark.parametrize("value,dp,expected", [
    (20.5, 0, "21"),        # half-to-even would print "20"
    (8.5, 0, "9"),          # half-to-even would print "8"
    (20.4, 0, "20"),
    (130.5, 0, "131"),
    (38.05, 1, "38.1"),     # binary rounding and toFixed print "38.0"
    (35.05, 1, "35.1"),
    (36.25, 1, "36.3"),     # half-to-even would print "36.2"
    (37, 1, "37.0"),
    (98, 0, "98"),
    ("20.5", 0, "21"),      # a numeric string from a row reads the same way
])
def test_the_shared_formatter_prints_the_charted_value(value, dp, expected):
    from services.vital_charting import charted_text
    assert charted_text(value, dp) == expected


@pytest.mark.parametrize("value", [None, "", float("nan"), float("inf"), "n/a", True])
def test_a_missing_vital_prints_as_missing_never_as_a_number(value):
    from services.vital_charting import charted_text, NOT_RECORDED
    assert charted_text(value) == NOT_RECORDED
    assert charted_text(value, 1, unit="°C") == NOT_RECORDED


def test_the_formatter_and_the_scorer_agree_on_every_value():
    """The formatter is what the surfaces print; compute_news2 is what scored.
    They must be one rule, not two that happen to agree on integers."""
    from services.vital_charting import charted_text
    bad = []
    for raw in [i / 100 for i in range(0, 6001)]:
        _res, b = _row("rr", raw, {})
        if charted_text(raw) != str(b["value"]):
            bad.append(f"  rr={raw}: prints {charted_text(raw)}, scored {b['value']}")
    for raw in [i / 1000 for i in range(28000, 43001, 5)]:
        _res, b = _row("temp", raw, {})
        if charted_text(raw, 1) != f"{b['value']:.1f}":
            bad.append(f"  temp={raw}: prints {charted_text(raw, 1)}, scored {b['value']}")
    assert not bad, "\n".join(bad[:25])


def test_the_ipass_sheet_prints_the_value_its_drivers_were_scored_on():
    """I-PASS prints "NEWS2 n" and the drivers, then the latest vitals two
    lines later. f"{20.5:.0f}" put "RR 20" under "Tachypnea (RR 21)"."""
    from services.ipass_service import _num
    assert _num(20.5) == "21"
    assert _num(38.05, 1) == "38.1"
    assert _num(None) == "--"


def test_the_tier1_prompt_trend_lines_print_the_charted_values():
    """Tier 1 tells the model "Respiratory rate 21/min = 2pt"; the trend lines
    in the same prompt must not say RR 20 for the same reading."""
    from services.sage_assessment_service import _format_observation
    line = _format_observation({
        "timestamp": "2026-10-02T08:00:00", "heart_rate": 130.5,
        "systolic_bp": 90.4, "respiratory_rate": 20.5, "spo2": 95.5,
        "temperature": 38.05, "consciousness": "alert", "on_oxygen": 0,
    })
    assert "HR 131 · BP 90 · RR 21 · SpO2 96% · Temp 38.1°C" in line, line


def test_the_downtime_print_pack_prints_the_charted_values():
    """The paper pack carries "NEWS2 n (level)" beside the latest vitals and is
    what a nurse re-scores from when the system is down. It printed the raw
    floats ("28.2", "140.6/70.1"), which are not values on the chart."""
    from services.downtime_service import _e, _render_vitals_news2
    html = _render_vitals_news2({
        "latest_vitals": {"heart_rate": 130.5, "systolic_bp": 140.6,
                          "diastolic_bp": 70.1, "spo2": 86.9, "respiratory_rate": 28.2,
                          "temperature": 38.05, "consciousness": "alert",
                          "on_oxygen": 1, "timestamp": "2026-10-02T08:00:00"},
        "news2": {"raw_news2": 11, "level": "critical"},
    })
    for cell in ("<td>131</td>", "<td>141/70</td>", "<td>87% on O₂</td>",
                 "<td>28</td>", "<td>38.1</td>"):
        assert cell in html, (cell, html)
    missing = _render_vitals_news2({"latest_vitals": {"heart_rate": None},
                                    "news2": {}})
    assert f"<td>{_e(None)}</td>" in missing and "None" not in missing, missing
