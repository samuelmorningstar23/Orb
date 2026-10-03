"""THE WORD MUST MATCH THE PATIENT, NOT THE POINT BAND.

WHAT WAS ON SCREEN (2026-08-14, real browser, running ward). Vigil's Tier 1
driver strip — the deterministic tier a physician reviewer called "the product",
the one that lands on 100% of alerts and that we most need clinicians to trust —
printed:

    Severe hypoxia (SpO2 91%)
    Severe tachypnea (RR 25)
    Severe hypotension (SBP 85)

None of those three values is severe. The adjective had been bolted to the RCP
3-point band rather than to the patient, so every value that scored 3 was called
severe regardless of how far it actually was from normal. The same strings are
injected into Sage's prompt, so the model read the overstatement back out in
prose (docs/perf/sage-eval-3.json still contains "SBP 83.1 mmHg (3 points) -
Hypotension/Low perfusion state" alongside genuinely severe findings). A
physician spots this in the first minute of a demo.

THE TWO INVARIANTS THIS FILE HOLDS, and they pull in opposite directions:

  1. THE POINTS MUST NOT MOVE. The RCP NEWS2 table is the pinned authoritative
     value Orb computes; wording is not allowed to cost a single point. Every
     sweep below asserts the score alongside the label, so a future wording
     change that nudges a band edge fails here rather than in a ward.

  2. THE LABEL MUST MATCH CLINICAL SEVERITY. "Severe" is reserved for values a
     clinician would actually call severe. Those thresholds are written out as
     LITERALS in this file — deliberately not imported from
     `vigil_service._SEVERE_THRESHOLDS`, because a test that imports the rule it
     is checking asserts only that the module can read itself.

THE THRESHOLDS, and why each one:

  SpO2 < 88 %    (Scale 1) the floor of the accepted COPD target range and the
                 knee of the oxyhaemoglobin dissociation curve (PaO2 ~55 mmHg).
                 88-91 % is real hypoxaemia; it is not severe.
  SpO2 < 84 %    (Scale 2) severity is relative to the TARGET RANGE — the whole
                 reason Scale 2 exists. A chronic type-2 patient targeted at
                 88-92 % is not in severe hypoxia at 86 % the way a Scale-1
                 patient at 86 % is. This test file learned that the hard way:
                 an earlier draft asserted one 88 % threshold for both scales
                 and immediately declared four correctly-worded Scale-2 bands
                 wrong.
  RR   >= 30     marked respiratory distress / impending failure.
  RR   <= 8      respiratory depression — the whole RCP low band is severe.
  SBP  < 80      shock-range perfusion failure. 80-90 needs treating, and is not
                 a peri-arrest blood pressure.
  SBP  >= 220    hypertensive-crisis range — the whole RCP high band is severe.
  HR   >= 140    the rate at which diastolic filling and coronary perfusion fall
                 away. 131-139 is tachycardia.
  HR   <= 40     the whole RCP low band is severe bradycardia.
  Temp < 32.0 C  the mild/moderate hypothermia boundary. 32-35 C — which is the
                 ENTIRE RCP 3-point band — is MILD hypothermia, so that band
                 cannot carry the word either.

Anything scoring 3 without reaching one of those reads "Moderate", which sits
above the unqualified 2-point wording and below "Severe": the ladder is
Mild -> (unqualified) -> Moderate -> Severe while the points stay put.

WHY THE SWEEPS RUN THE FULL RANGE rather than the band edges. The defect was not
at an edge — it was uniform across a band, and a band-edge test would have
passed against the broken code at 91 %, 25 and 85 exactly as it did before.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.vigil_service import compute_news2


# A vitals set that scores 0 on every parameter, perturbed one parameter at a
# time so each sweep isolates a single band.
NORMAL = dict(hr=70, sbp=120, rr=16, temp=37.0, spo2=98,
              consciousness="alert", on_oxygen=False)


def _score(**overrides):
    return compute_news2(**{**NORMAL, **overrides})


def _param(res: dict, name: str) -> dict:
    """The breakdown row for one parameter.

    Read from `breakdown` rather than by grepping `drivers`: the breakdown is
    keyed by parameter, so a sweep cannot accidentally assert against another
    parameter's string, and it carries the points and the label TOGETHER — which
    is the pairing under test.
    """
    for b in res.get("breakdown") or []:
        if b["parameter"] == name:
            return b
    raise AssertionError(f"{name!r} absent from breakdown: {res.get('breakdown')}")


def _sweep(name, key, values, expected, **extra):
    """Assert (RCP points, clinical label) TOGETHER for every value in `values`.

    Together on purpose: the two invariants pull against each other, and a sweep
    that checked only one would let the other be traded away silently.

    Collects EVERY mismatch before failing. A band that is uniformly wrong —
    which is exactly what D5 was — should report as a band, not as one arbitrary
    value from inside it.
    """
    values = list(values)
    bad = []
    for v in values:
        b = _param(_score(**{key: v}, **extra), name)
        want_points, want_note = expected(v)
        if b["points"] != want_points or b["note"] != want_note:
            bad.append(f"  {key}={v}{f' {extra}' if extra else ''}: "
                       f"got {b['points']}pts {b['note']!r}"
                       f" — want {want_points}pts {want_note!r}")
    assert not bad, (
        f"{len(bad)} of {len(values)} values mis-scored or mis-labelled:\n"
        + "\n".join(bad[:25]))


# ═════════════════════════════════════════════════════════════════════════════
#  Per-parameter sweeps: the RCP points AND the clinical wording, together
# ═════════════════════════════════════════════════════════════════════════════
def _expected_rr(rr):
    if rr <= 8:  return 3, f"Severe bradypnea (RR {rr})"
    if rr <= 11: return 1, f"Low RR ({rr})"
    if rr <= 20: return 0, None
    if rr <= 24: return 2, f"Tachypnea (RR {rr})"
    if rr < 30:  return 3, f"Moderate tachypnea (RR {rr})"
    return 3, f"Severe tachypnea (RR {rr})"


def test_respiratory_rate_full_range():
    _sweep("Respiratory rate", "rr", range(1, 61), _expected_rr)


def _expected_spo2_scale1(s):
    if s >= 96: return 0, None
    if s >= 94: return 1, f"Mild hypoxia (SpO2 {s}%)"
    if s >= 92: return 2, f"Hypoxia (SpO2 {s}%)"
    if s >= 88: return 3, f"Moderate hypoxia (SpO2 {s}%)"
    return 3, f"Severe hypoxia (SpO2 {s}%)"


def test_spo2_scale_1_full_range():
    _sweep("SpO2", "spo2", range(60, 101), _expected_spo2_scale1)


def _expected_spo2_scale2_on_air(s):
    """Scale 2 (COPD / chronic type-2 failure), breathing air.

    The severity word is taken from the saturation measured against THIS
    patient's target range (88-92 %), which is the whole reason Scale 2 exists:
    86 % in a chronic type-2 patient is not the same clinical event as 86 % in a
    Scale-1 patient, and the label may not pretend otherwise in either
    direction. So 84-87 % reads "Hypoxia"/"Low SpO2" here while the identical
    saturation reads "Severe hypoxia" on Scale 1.
    """
    if s <= 83: return 3, f"Severe hypoxia (SpO2 {s}%, Scale 2)"
    if s <= 85: return 2, f"Hypoxia (SpO2 {s}%, Scale 2)"
    if s <= 87: return 1, f"Low SpO2 ({s}%, Scale 2)"
    return 0, None


def test_spo2_scale_2_on_air_full_range():
    _sweep("SpO2 (Scale 2)", "spo2", range(60, 101), _expected_spo2_scale2_on_air,
           on_scale_2=True)


def _expected_spo2_scale2_on_oxygen(s):
    if s <= 83: return 3, f"Severe hypoxia (SpO2 {s}%, Scale 2)"
    if s <= 85: return 2, f"Hypoxia (SpO2 {s}%, Scale 2)"
    if s <= 87: return 1, f"Low SpO2 ({s}%, Scale 2)"
    if s <= 92: return 0, None
    if s <= 94: return 1, f"Over-target SpO2 on O2 ({s}%, Scale 2)"
    if s <= 96: return 2, f"Over-target SpO2 on O2 ({s}%, Scale 2)"
    return 3, f"Over-oxygenation on O2 ({s}%, Scale 2)"


def test_spo2_scale_2_on_oxygen_full_range():
    """Over-oxygenation scores 3 and must NOT be called severe hypoxia — or
    severe anything. It is a real Scale-2 hazard with its own honest wording."""
    _sweep("SpO2 (Scale 2)", "spo2", range(60, 101), _expected_spo2_scale2_on_oxygen,
           on_scale_2=True, on_oxygen=True)


def _expected_sbp(sbp):
    if sbp < 80:   return 3, f"Severe hypotension (SBP {sbp})"
    if sbp <= 90:  return 3, f"Moderate hypotension (SBP {sbp})"
    if sbp <= 100: return 2, f"Hypotension (SBP {sbp})"
    if sbp <= 110: return 1, f"Low BP (SBP {sbp})"
    if sbp <= 219: return 0, None
    return 3, f"Severe hypertension (SBP {sbp})"


def test_systolic_bp_full_range():
    _sweep("Systolic BP", "sbp", range(40, 261), _expected_sbp)


def _expected_hr(hr):
    if hr <= 40:  return 3, f"Severe bradycardia (HR {hr})"
    if hr <= 50:  return 1, f"Bradycardia (HR {hr})"
    if hr <= 90:  return 0, None
    if hr <= 110: return 1, f"Mild tachycardia (HR {hr})"
    if hr <= 130: return 2, f"Tachycardia (HR {hr})"
    if hr < 140:  return 3, f"Moderate tachycardia (HR {hr})"
    return 3, f"Severe tachycardia (HR {hr})"


def test_heart_rate_full_range():
    _sweep("Heart rate", "hr", range(20, 221), _expected_hr)


def _expected_temp(t):
    if t < 32.0:  return 3, f"Severe hypothermia ({t:.1f}°C)"
    if t <= 35.0: return 3, f"Hypothermia ({t:.1f}°C)"
    if t <= 36.0: return 1, f"Low temp ({t:.1f}°C)"
    if t <= 38.0: return 0, None
    if t <= 39.0: return 1, f"Fever ({t:.1f}°C)"
    return 2, f"High fever ({t:.1f}°C)"


def test_temperature_full_range():
    """32-35 C is the ENTIRE RCP 3-point band and it is MILD hypothermia, so the
    band cannot carry "severe" — but 30 C genuinely can, and understating that
    is the same class of error pointing the other way."""
    _sweep("Temperature", "temp", [round(i / 10, 1) for i in range(280, 431)],
           _expected_temp)


# ═════════════════════════════════════════════════════════════════════════════
#  THE INVARIANT, stated once and checked across every parameter at once
# ═════════════════════════════════════════════════════════════════════════════
#: "Severe" is true of THIS PRINTED VALUE. Literals, not an import — see the
#: module docstring.
IS_CLINICALLY_SEVERE = {
    "Respiratory rate": lambda v: v <= 8 or v >= 30,
    "SpO2":             lambda v: v < 88,
    "SpO2 (Scale 2)":   lambda v: v < 84,
    "Systolic BP":      lambda v: v < 80 or v >= 220,
    "Heart rate":       lambda v: v <= 40 or v >= 140,
    "Temperature":      lambda v: v < 32.0,
}

#: Every parameter, every value it can carry, and how to feed it in.
EVERY_OBSERVATION = [
    ("Respiratory rate", "rr", range(1, 61), {}),
    ("SpO2", "spo2", range(60, 101), {}),
    ("SpO2 (Scale 2)", "spo2", range(60, 101), {"on_scale_2": True}),
    ("SpO2 (Scale 2)", "spo2", range(60, 101), {"on_scale_2": True, "on_oxygen": True}),
    ("Systolic BP", "sbp", range(40, 261), {}),
    ("Heart rate", "hr", range(20, 221), {}),
    ("Temperature", "temp", [round(i / 10, 1) for i in range(280, 431)], {}),
]


def _printed_value(note: str):
    """The number the label PRINTS, parsed back out of the label.

    Checked against the printed digits rather than the raw float on purpose: the
    monitor lane and the simulator both produce values like 29.6 and 79.6, and a
    label that reads "Moderate tachypnea (RR 30)" would be contradicting the
    number standing next to it even if the raw value justified the word.
    """
    inside = note.split("(", 1)
    if len(inside) < 2:
        return None
    # The lookbehind is load-bearing: without it "Hypoxia (SpO2 92%)" parses as
    # 2 — the digit inside "SpO2" — and every saturation silently reads as
    # severe, which would make this whole file assert nothing.
    m = re.search(r"(?<![A-Za-z0-9])-?\d+(?:\.\d+)?", inside[1])
    return float(m.group()) if m else None


def _all_labels():
    for name, key, values, extra in EVERY_OBSERVATION:
        for v in values:
            b = _param(_score(**{key: v}, **extra), name)
            if b["note"]:
                yield name, v, b["points"], b["note"]


def test_every_scoring_label_carries_the_value_it_is_describing():
    """A precondition for the two tests below, and worth having on its own.

    "Tachycardia" without the number is an opinion; "Tachycardia (HR 118)" is
    attribution, and attribution is the entire claim Tier 1 makes. It is also
    what lets the checks below re-derive the severity word from what the
    clinician actually reads — without it they would silently pass on anything.
    """
    naked = [f"  {name} {value}: {note!r}"
             for name, value, _pts, note in _all_labels()
             if _printed_value(note) is None]
    assert not naked, (
        "scoring labels with no value in them:\n" + "\n".join(naked[:25]))


def test_no_label_calls_a_value_severe_unless_it_is():
    """THE D5 REGRESSION, across every value every parameter can take."""
    overstated = [
        f"  {name} {value}: {note!r}"
        for name, value, _pts, note in _all_labels()
        if "severe" in note.lower()
        and not IS_CLINICALLY_SEVERE[name](_printed_value(note))
    ]
    assert not overstated, (
        "labels call a non-severe value severe — this is what a physician sees "
        "in the first minute, and Sage repeats it back in prose:\n"
        + "\n".join(overstated[:25]))


def test_every_genuinely_severe_value_is_labelled_severe():
    """The other direction. Understating is a quieter failure than overstating
    and would be the easy way to "fix" this file: strip the word everywhere."""
    understated = [
        f"  {name} {value} ({pts}pts): {note!r}"
        for name, value, pts, note in _all_labels()
        if IS_CLINICALLY_SEVERE[name](_printed_value(note))
        and "severe" not in note.lower()
    ]
    assert not understated, (
        "genuinely severe values are not labelled severe:\n" + "\n".join(understated[:25]))


def test_the_label_never_contradicts_the_number_printed_beside_it():
    """Raw values are floats. If the word is chosen off the raw value and the
    digits off the rounded one, a single reading can print "Moderate tachypnea
    (RR 30)" — the label arguing with its own number on the same line."""
    contradictions = []
    for raw, key, name, expect_severe in [
        (29.6, "rr", "Respiratory rate", True),     # prints RR 30
        (29.4, "rr", "Respiratory rate", False),    # prints RR 29
        (79.6, "sbp", "Systolic BP", False),        # prints SBP 80
        (79.4, "sbp", "Systolic BP", True),         # prints SBP 79
        (87.6, "spo2", "SpO2", False),              # prints SpO2 88%
        (87.4, "spo2", "SpO2", True),               # prints SpO2 87%
        (139.6, "hr", "Heart rate", True),          # prints HR 140
        (139.4, "hr", "Heart rate", False),         # prints HR 139
    ]:
        b = _param(_score(**{key: raw}), name)
        note = b["note"] or ""
        printed = _printed_value(note)
        if ("severe" in note.lower()) != expect_severe:
            contradictions.append(f"  raw {key}={raw} printed {printed}: {note!r}")
        # Every one of these, raw or as charted, sits in a 3-point band. Since
        # 2026-10-02 the points come off the CHARTED value (rounded half-up to
        # the chart's precision), the same number the label prints; the band
        # edges themselves are pinned in test_news2_charted_value.py.
        assert b["points"] == 3, (
            f"raw {key}={raw} (charted {b['value']}) no longer scores 3: {b}")
    assert not contradictions, "\n".join(contradictions)


# ═════════════════════════════════════════════════════════════════════════════
#  The three strings that were actually on screen
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("overrides,parameter,expected", [
    (dict(spo2=91), "SpO2", "Moderate hypoxia (SpO2 91%)"),
    (dict(rr=25), "Respiratory rate", "Moderate tachypnea (RR 25)"),
    (dict(sbp=85), "Systolic BP", "Moderate hypotension (SBP 85)"),
])
def test_the_three_labels_seen_on_the_ward(overrides, parameter, expected):
    res = _score(**overrides)
    b = _param(res, parameter)
    assert b["note"] == expected
    assert b["points"] == 3, "the RCP escalation this value earns must not move"
    assert res["single_param_3"] is True, (
        "wording changed the single-parameter-3 escalation rule")
    assert res["level"] == "elevated"
    assert expected in res["drivers"], (
        "the driver strip and the breakdown disagree — they are the same string")


def test_a_genuinely_severe_patient_still_reads_severe():
    """The peri-arrest set from the 2026-08-14 dry run: RR 32, SpO2 86, on O2,
    Temp 39.4, SBP 78, HR 138, ACVPU voice — NEWS2 19. Three of those ARE severe
    and must still say so; HR 138 must not."""
    res = compute_news2(hr=138, sbp=78, rr=32, temp=39.4, spo2=86,
                        consciousness="voice", on_oxygen=True)
    assert res["raw_news2"] == 19, "the pinned NEWS2 for this set moved"
    assert res["level"] == "critical"
    # Read off the breakdown, not `drivers` — the driver strip is capped at
    # three (vigil_service.py:494) and this patient has seven scoring
    # parameters, so four of these labels never reach that list.
    notes = {b["parameter"]: b["note"] for b in res["breakdown"]}
    assert notes["Respiratory rate"] == "Severe tachypnea (RR 32)"
    assert notes["SpO2"] == "Severe hypoxia (SpO2 86%)"
    assert notes["Systolic BP"] == "Severe hypotension (SBP 78)"
    assert notes["Heart rate"] == "Moderate tachycardia (HR 138)", (
        "HR 138 is tachycardia, not severe tachycardia")
