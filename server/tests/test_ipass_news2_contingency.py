"""I-PASS contingency triggers are the RCP NEWS2 chart's own 3-point thresholds.

WHY THIS FILE EXISTS (found 2026-10-03, ruled by Samuel the same day). On
2026-10-02 the Vigil command bar started colouring each vital from the server's
NEWS2 breakdown points. The handover's "if X then Y" lines still carried
hand-written cutoffs and picked their line by keyword-matching the driver text,
so the sheet disagreed with the bar and, in four cases, with itself:

  SBP 90  (3 pts, red on the bar)  -> "If systolic BP < 90 mmHg ..."
  SBP 225 (3 pts, hypertension)    -> "If systolic BP < 90 mmHg, give a fluid bolus ..."
  HR 38   (3 pts, bradycardia)     -> "If heart rate exceeds 130 ..."
  RR 8    (3 pts, bradypnoea)      -> "If respiratory rate rises above 24 ..."
  Temp 35.0 (3 pts)                -> no temperature line at all
  Temp 35.8 (1 pt, low)            -> "If febrile ... start the Sepsis Six bundle"

The keyword match lost the direction ("(hr" matched bradycardia, "sbp" matched
hypertension), and the drivers list is cut to three (the CfC overlay can take a
slot), so a vital that scored could lose its line altogether.

THE RULING: one line per vital that scored, chosen from the NEWS2 breakdown row
and the side of normal its charted value sits on, and the trigger printed as
the chart prints it: "is 90 mmHg or below (NEWS2 3 points)". The four
directions that had no line of their own (SBP high, HR low, RR low, Temp low)
give the RCP red-score response, "inform the covering doctor for urgent
review", and invent no clinical action. SpO2 Scale 2 lines are unchanged (ruled
2026-10-02, pinned in test_ipass_scale2.py).

The sweep below derives every threshold from `compute_news2` itself, so a
number on the sheet that stops being where the 3 points start goes red here.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from services.ipass_service import _contingencies
from services.vigil_service import compute_news2

# The approved wording, character for character.
SBP_LOW = ("If systolic BP is 90 mmHg or below (NEWS2 3 points), give a fluid bolus "
           "per protocol and escalate to the registrar.")
SBP_HIGH = ("If systolic BP is 220 mmHg or above (NEWS2 3 points), inform the covering "
            "doctor for urgent review.")
HR_LOW = ("If heart rate is 40 or below (NEWS2 3 points), inform the covering doctor "
          "for urgent review.")
HR_HIGH = ("If heart rate is 131 or above (NEWS2 3 points) or a new arrhythmia appears, "
           "obtain a 12-lead ECG and review.")
RR_LOW = ("If respiratory rate is 8 or below (NEWS2 3 points), inform the covering "
          "doctor for urgent review.")
RR_HIGH = ("If respiratory rate is 25 or above (NEWS2 3 points) or work of breathing "
           "worsens, reassess the airway and escalate.")
SPO2_S1 = ("If SpO2 is 91% or below (NEWS2 3 points) or oxygen requirement rises, "
           "titrate O2 to target and call the covering doctor.")
TEMP_LOW = ("If temperature is 35.0°C or below (NEWS2 3 points), inform the covering "
            "doctor for urgent review.")
FEVER = ("If febrile with a rising NEWS2, start the Sepsis Six bundle and send blood "
         "cultures + lactate.")
ACVPU = ("If new confusion or a drop in ACVPU, check glucose, do a neuro assessment "
         "and escalate urgently.")

#: A full, normal, assessed set: every parameter measured and scoring 0.
NORMAL = dict(hr=80, sbp=125, rr=16, temp=37.0, spo2=97)


def _risk(**over) -> dict:
    v = {**NORMAL, **over}
    return compute_news2(v["hr"], v["sbp"], v["rr"], v["temp"], v["spo2"],
                         over.get("consciousness", "alert"), over.get("on_oxygen", False))


# ═════════════════════════════════════════════════════════════════════════════
#  THE APPROVED LINES, ONE ABNORMAL VITAL AT A TIME
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("over,line", [
    (dict(sbp=90), SBP_LOW),      # 3 pts: the bar shows it red, the sheet said "< 90"
    (dict(sbp=95), SBP_LOW),      # 2 pts
    (dict(sbp=105), SBP_LOW),     # 1 pt
    (dict(sbp=225), SBP_HIGH),    # 3 pts, the HIGH side
    (dict(hr=38), HR_LOW),        # 3 pts
    (dict(hr=45), HR_LOW),        # 1 pt
    (dict(hr=95), HR_HIGH),       # 1 pt
    (dict(hr=125), HR_HIGH),      # 2 pts
    (dict(hr=135), HR_HIGH),      # 3 pts
    (dict(rr=8), RR_LOW),         # 3 pts
    (dict(rr=10), RR_LOW),        # 1 pt: today this vital got no line at all
    (dict(rr=22), RR_HIGH),       # 2 pts
    (dict(rr=26), RR_HIGH),       # 3 pts
    (dict(spo2=91), SPO2_S1),     # 3 pts
    (dict(spo2=93), SPO2_S1),     # 2 pts
    (dict(spo2=95), SPO2_S1),     # 1 pt
    (dict(temp=35.0), TEMP_LOW),  # 3 pts: today no temperature line at all
    (dict(temp=35.8), TEMP_LOW),  # 1 pt: today "If febrile ..."
    (dict(temp=38.5), FEVER),     # 1 pt
    (dict(temp=39.5), FEVER),     # 2 pts (NEWS2 has no 3-point fever band)
    (dict(consciousness="confused"), ACVPU),
])
def test_one_scoring_vital_gets_exactly_the_line_for_its_side(over, line):
    assert _contingencies(_risk(**over)) == [line]


def test_supplemental_oxygen_alone_hands_over_the_spo2_line():
    """SpO2 97 on oxygen: 0 points for the saturation, 2 for the oxygen. The
    oxygen requirement is what the line is about, as it was before."""
    assert _contingencies(_risk(on_oxygen=True)) == [SPO2_S1]


def test_hypertension_never_gets_the_fluid_bolus_line():
    """SBP 225 matched the keyword "sbp" and was told to have a fluid bolus."""
    out = _contingencies(_risk(sbp=225))
    assert not any("fluid bolus" in c for c in out), out


@pytest.mark.parametrize("over,wrong", [
    # The clause only the high-side line carries, so the old wording
    # ("exceeds 130", "rises above 24") is caught as well as the new.
    (dict(hr=38), "arrhythmia"),
    (dict(rr=8), "work of breathing"),
    (dict(temp=35.8), "febrile"),
    (dict(temp=35.0), "febrile"),
])
def test_a_low_vital_is_never_handed_the_high_side_line(over, wrong):
    out = _contingencies(_risk(**over))
    assert not any(wrong in c for c in out), out


# ═════════════════════════════════════════════════════════════════════════════
#  PARITY: every printed number is where compute_news2 starts scoring 3
# ═════════════════════════════════════════════════════════════════════════════

#: breakdown parameter -> (compute_news2 keyword, chart precision, sweep range,
#: the words every line for this vital starts with).
_VITALS = {
    "Respiratory rate": ("rr", 0, (0, 60), ("If respiratory rate",)),
    "SpO2": ("spo2", 0, (50, 100), ("If SpO2",)),
    "Systolic BP": ("sbp", 0, (40, 300), ("If systolic BP",)),
    "Heart rate": ("hr", 0, (20, 220), ("If heart rate",)),
    "Temperature": ("temp", 1, (30, 43), ("If temperature", "If febrile")),
}
_TRIGGER = re.compile(
    r"is (?P<num>\d+(?:\.\d)?)(?: mmHg|%|°C)? or (?P<side>below|above) \(NEWS2 3 points\)")


def _sweep(parameter):
    key, dp, (lo, hi), _ = _VITALS[parameter]
    step = Decimal(1).scaleb(-dp)
    v = Decimal(lo)
    while v <= hi:
        value = int(v) if dp == 0 else float(v)
        risk = _risk(**{key: value})
        row = next(b for b in risk["breakdown"] if b["parameter"] == parameter)
        yield value, row["points"], risk
        v += step


def _edges(parameter):
    """(lowest 0-point value, highest 0-point value, 3-point edge below or None,
    3-point edge above or None), all read off the real scorer."""
    pts = [(v, p) for v, p, _ in _sweep(parameter)]
    zero = [v for v, p in pts if p == 0]
    z_lo, z_hi = min(zero), max(zero)
    below = [v for v, p in pts if p == 3 and v < z_lo]
    above = [v for v, p in pts if p == 3 and v > z_hi]
    return z_lo, z_hi, (max(below) if below else None), (min(above) if above else None)


@pytest.mark.parametrize("parameter", list(_VITALS))
def test_every_scoring_value_gets_one_line_whose_number_is_the_chart_threshold(parameter):
    _, dp, _, prefixes = _VITALS[parameter]
    z_lo, z_hi, edge_below, edge_above = _edges(parameter)
    for value, points, risk in _sweep(parameter):
        mine = [c for c in _contingencies(risk) if c.startswith(prefixes)]
        if points == 0:
            assert mine == [], (parameter, value, mine)
            continue
        assert len(mine) == 1, (parameter, value, mine)
        side = "below" if value < z_lo else "above"
        edge = edge_below if side == "below" else edge_above
        m = _TRIGGER.search(mine[0])
        if edge is None:
            # No 3-point band on this side (fever tops out at 2): the line must
            # not claim one.
            assert m is None, (parameter, value, mine[0])
            continue
        assert m, (parameter, value, mine[0])
        assert m["side"] == side, (parameter, value, mine[0])
        assert m["num"] == f"{edge:.{dp}f}", (parameter, value, edge, mine[0])


def test_the_sweep_covers_every_red_threshold_on_the_chart():
    """The eight 3-point bands NEWS2 Scale 1 has for the numeric vitals. If the
    scorer grows or loses one, this list and the lines must be revisited."""
    found = {}
    for parameter in _VITALS:
        _, _, below, above = _edges(parameter)
        found[parameter] = (below, above)
    assert found == {
        "Respiratory rate": (8, 25),
        "SpO2": (91, None),
        "Systolic BP": (90, 220),
        "Heart rate": (40, 131),
        "Temperature": (35.0, None),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  SELECTION: the breakdown, not the top-three drivers
# ═════════════════════════════════════════════════════════════════════════════

def test_a_vital_cut_from_the_top_three_drivers_still_gets_its_line():
    """RR 26, SpO2 91 and SBP 88 take the three driver slots; the 1-point HR 45
    is not in `drivers`, but it scored, so it is handed over."""
    risk = _risk(rr=26, spo2=91, sbp=88, hr=45)
    assert not any("HR" in d for d in risk["drivers"]), risk["drivers"]
    out = _contingencies(risk)
    assert HR_LOW in out, out
    assert {SPO2_S1, SBP_LOW, RR_HIGH} <= set(out), out


def test_the_cfc_overlay_taking_a_driver_slot_does_not_drop_a_line():
    """`_apply_cfc_overlay` prepends a CfC driver and cuts back to three."""
    risk = _risk(sbp=88, hr=135, temp=35.0)
    risk["drivers"] = (["CfC: high near-term deterioration risk"] + risk["drivers"])[:3]
    out = _contingencies(risk)
    assert {SBP_LOW, HR_HIGH, TEMP_LOW} <= set(out), out


def test_lines_follow_the_handover_order():
    """Oxygenation, circulation, heart rate, temperature, consciousness,
    breathing rate: the order the sheet has always used."""
    risk = _risk(rr=26, spo2=91, sbp=88, hr=135, temp=35.0, consciousness="confused")
    assert _contingencies(risk) == [SPO2_S1, SBP_LOW, HR_HIGH, TEMP_LOW, ACVPU, RR_HIGH]


def test_no_vitals_recorded_keeps_the_level_line():
    """`get_patient_risk` returns a stub with no breakdown when nothing is
    charted. That patient still gets a contingency, and it names no number."""
    out = _contingencies({"score": 0, "level": "stable",
                          "drivers": ["No vitals recorded"], "scoring_model": "NEWS2"})
    assert out == ["If new abnormal vitals develop, repeat a full set of observations "
                   "and reassess."]


def test_every_line_is_if_then_and_has_no_em_dash():
    risk = _risk(rr=26, spo2=91, sbp=225, hr=38, temp=35.0, consciousness="confused")
    out = _contingencies(risk)
    assert out and all(c.startswith("If ") for c in out), out
    assert not any("\u2014" in c for c in out), out  # no em dash
