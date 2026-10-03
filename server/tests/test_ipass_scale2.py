"""The I-PASS handover must say which SpO2 scale scored the patient.

WHY THIS FILE EXISTS (found 2026-10-02, Scale 2 lane sweep). NEWS2 SpO2 Scale 2
is the table for COPD / chronic type-2 respiratory failure (target 88-92%),
chosen per patient by `patients.on_scale_2`. The scorer already returns
`spo2_scale` and the Scale 2 driver wording, but the handover dropped both
halves of the fact:

  * the I line printed "NEWS2 0 (stable band)" for a COPD patient sitting at
    SpO2 90 on air, with nothing to tell the oncoming nurse that 90 IS the
    target for this patient, and the structured section carried no scale;
  * every SpO2 / oxygen driver produced the Scale 1 contingency "If SpO2 falls
    below 92% ... call the covering doctor", which tells the nurse to escalate
    an on-target reading, and said nothing at all about over-oxygenation, the
    harm Scale 2 exists to catch.

Nothing in Orb can set `on_scale_2` yet (gap register X003). These tests set it
directly so the handover is honest on the day the prescribe route ships. Scale 1
output is pinned byte for byte: existing sheets must not change.

2026-10-03: the Scale 1 SpO2 and heart-rate lines were reworded by ruling to
the RCP chart's own notation ("91% or below (NEWS2 3 points)", "131 or above"),
the same thresholds as before; see test_ipass_news2_contingency.py. Contingencies
are now read from the NEWS2 breakdown, so the tests below score real vitals
instead of hand-writing driver strings. The Scale 2 lines are unchanged.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import db_session
from services.ipass_service import _build_illness_severity, _contingencies, build_ipass
from services.vigil_service import compute_news2

#: The Scale 1 line, character for character as ruled on 2026-10-03 (until then
#: "falls below 92%", the same red band).
SCALE1_SPO2_LINE = ("If SpO2 is 91% or below (NEWS2 3 points) or oxygen requirement "
                    "rises, titrate O2 to target and call the covering doctor.")
SCALE2_SPO2_LINE = ("If SpO2 falls below 88% or oxygen requirement rises, titrate O2 "
                    "to this patient's 88-92% target (SpO2 Scale 2) and call the "
                    "covering doctor.")
SCALE2_OVER_TARGET_LINE = ("If SpO2 stays above this patient's 88-92% target on "
                           "oxygen (SpO2 Scale 2), reduce O2 to target and call "
                           "the covering doctor.")
HR_LINE = ("If heart rate is 131 or above (NEWS2 3 points) or a new arrhythmia "
           "appears, obtain a 12-lead ECG and review.")

_SNAKE_CASE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")


def _risk(*, level="elevated", news2=5, drivers=(), scale=None) -> dict:
    risk = {"score": 0.3, "level": level, "raw_news2": news2,
            "drivers": list(drivers), "scoring_model": "NEWS2"}
    if scale is not None:
        risk["spo2_scale"] = scale
    return risk


# ═════════════════════════════════════════════════════════════════════════════
#  I. ILLNESS SEVERITY
# ═════════════════════════════════════════════════════════════════════════════

def test_scale_1_illness_severity_text_is_unchanged():
    isv = _build_illness_severity(_risk(
        drivers=["Hypoxia (SpO2 92%)", "On supplemental oxygen"], scale=1))
    assert isv["text"] == ("Watcher: NEWS2 5 (elevated band). Key drivers: "
                           "Hypoxia (SpO2 92%), On supplemental oxygen.")
    assert isv["spo2_scale"] == 1


def test_a_risk_dict_without_a_scale_reads_as_scale_1():
    """`get_patient_risk` returns a scale-less dict when nothing is charted, and
    test fixtures build them by hand. Absent is Scale 1, not an error."""
    isv = _build_illness_severity(_risk(level="stable", news2=0,
                                        drivers=["No vitals recorded"]))
    assert isv["text"] == "Stable: NEWS2 0 (stable band). Key drivers: No vitals recorded."
    assert isv["spo2_scale"] is None
    assert "Scale 2" not in isv["text"]


def test_scale_2_illness_severity_names_the_scale_and_its_target():
    isv = _build_illness_severity(_risk(
        drivers=["Over-oxygenation on O2 (97%, Scale 2)", "On supplemental oxygen"],
        scale=2))
    assert isv["spo2_scale"] == 2
    assert isv["text"] == (
        "Watcher: NEWS2 5 (elevated band, SpO2 Scale 2, target 88-92%). Key drivers: "
        "Over-oxygenation on O2 (97%, Scale 2), On supplemental oxygen.")


def test_scale_2_on_target_says_why_zero_is_zero():
    """SpO2 90 on air scores 0 on Scale 2. Without the scale on the line, a
    receiving nurse reads 'NEWS2 0' beside a saturation of 90 and distrusts it."""
    isv = _build_illness_severity(_risk(
        level="stable", news2=0, drivers=["All vitals within normal range"], scale=2))
    assert isv["text"] == (
        "Stable: NEWS2 0 (stable band, SpO2 Scale 2, target 88-92%). Key drivers: "
        "All vitals within normal range.")


# ═════════════════════════════════════════════════════════════════════════════
#  S. CONTINGENCIES
# ═════════════════════════════════════════════════════════════════════════════

def _news2(spo2, on_oxygen=False, *, scale_2, hr=80) -> dict:
    """The real scorer, normal on everything but SpO2 (and HR when given)."""
    return compute_news2(hr, 125, 16, 37.0, spo2, "alert", on_oxygen, on_scale_2=scale_2)


@pytest.mark.parametrize("drop_scale", [False, True])
def test_scale_1_spo2_contingency_is_unchanged(drop_scale):
    """A risk dict with no `spo2_scale` reads as Scale 1."""
    risk = _news2(90, scale_2=False)
    if drop_scale:
        risk.pop("spo2_scale")
    assert _contingencies(risk) == [SCALE1_SPO2_LINE]


@pytest.mark.parametrize("spo2,on_oxygen", [
    (83, False),   # 3 pts, below target
    (85, False),   # 2 pts
    (87, False),   # 1 pt
    (90, True),    # on target; 2 pts for the oxygen alone
])
def test_scale_2_spo2_contingency_escalates_below_88_not_92(spo2, on_oxygen):
    out = _contingencies(_news2(spo2, on_oxygen, scale_2=True))
    assert out == [SCALE2_SPO2_LINE]
    assert not any("92% or" in c for c in out), out


@pytest.mark.parametrize("spo2", [94, 96, 97])  # over target 1, 2, 3 pts
def test_scale_2_over_target_on_oxygen_says_reduce_o2_first(spo2):
    out = _contingencies(_news2(spo2, True, scale_2=True))
    assert out == [SCALE2_OVER_TARGET_LINE, SCALE2_SPO2_LINE]


def test_scale_2_over_target_is_found_when_the_drivers_list_cut_it():
    """Drivers are the worst three. A 1-point over-target SpO2 (94 on O2) ranks
    below the 2-point "On supplemental oxygen" and any other 2-point parameter,
    so with three of those it is not in `drivers` at all. The reduce-O2 line
    must still be handed over: it is read from the SpO2 breakdown row too."""
    risk = compute_news2(115, 125, 22, 39.5, 94, "alert", True, on_scale_2=True)
    assert not any("over-target" in d.lower() for d in risk["drivers"]), risk["drivers"]
    out = _contingencies(risk)
    assert out[:2] == [SCALE2_OVER_TARGET_LINE, SCALE2_SPO2_LINE], out


@pytest.mark.parametrize("spo2,on_oxygen", [(83, False), (90, True), (94, True), (97, True)])
def test_every_scale_2_contingency_is_an_if_then_line(spo2, on_oxygen):
    """The handover's contingencies are 'if X then Y' (the docstring, and
    test_ipass.py's Scale 1 invariant). The Scale 2 lines keep that shape."""
    risk = compute_news2(80, 125, 16, 37.0, spo2, "alert", on_oxygen, on_scale_2=True)
    out = _contingencies(risk)
    assert out and all(line.lower().startswith("if ") for line in out), out


def test_scale_2_keeps_the_other_contingencies_after_the_spo2_lines():
    out = _contingencies(_news2(95, True, scale_2=True, hr=122))
    assert out == [SCALE2_OVER_TARGET_LINE, SCALE2_SPO2_LINE, HR_LINE]


def test_scale_2_without_an_spo2_driver_adds_no_spo2_line():
    """Same trigger as Scale 1: the scale changes WHAT is said, not WHEN. SpO2
    90 on air is on target and scores 0, so only the heart rate is handed over."""
    out = _contingencies(_news2(90, False, scale_2=True, hr=122))
    assert out == [HR_LINE]


@pytest.mark.parametrize("spo2,on_oxygen,over_target", [
    (90, True, False),     # on target, 2 points for the oxygen alone
    (94, True, True),      # over target, 1 point
    (95, True, True),      # over target, 2 points
    (97, True, True),      # over-oxygenation, 3 points
    (86, False, False),    # below target on air
])
def test_contingencies_match_the_real_scorer_wording(spo2, on_oxygen, over_target):
    """Literal driver strings above would keep passing if the scorer reworded a
    driver. This runs the real Scale 2 table so the match cannot drift from it."""
    risk = compute_news2(80, 125, 16, 37.0, spo2, "alert", on_oxygen, on_scale_2=True)
    assert risk["spo2_scale"] == 2
    out = _contingencies(risk)
    assert SCALE2_SPO2_LINE in out, (risk["drivers"], out)
    assert (SCALE2_OVER_TARGET_LINE in out) is over_target, (risk["drivers"], out)
    assert SCALE1_SPO2_LINE not in out


# ═════════════════════════════════════════════════════════════════════════════
#  END TO END: the builder the route calls, on a charted Scale 2 patient
# ═════════════════════════════════════════════════════════════════════════════

#: Every patient this module admits, discharged on teardown (conftest's sweep
#: explains why a leftover admitted patient is not inert).
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_created():
    yield
    if not _CREATED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _CREATED])
    _CREATED.clear()


def _admit(*, scale_2: bool, spo2: float, on_oxygen: bool) -> str:
    """Admitted patient with ONE vitals row, normal on everything but SpO2."""
    pid = f"IPS2{uuid.uuid4().hex[:8].upper()}"
    _CREATED.append(pid)
    ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, age, sex, status, on_scale_2) "
            "VALUES (?, ?, ?, 'S2-4', 'S2-WARD', 'COPD exacerbation', 68, 'M', 'admitted', ?)",
            (pid, f"Scale Handover {pid}", f"MRN-{pid}", 1 if scale_2 else 0),
        )
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
               diastolic_bp, spo2, temperature, respiratory_rate, consciousness, on_oxygen)
               VALUES (?, ?, 80, 125, 75, ?, 37.0, 16, 'alert', ?)""",
            (pid, ts, spo2, 1 if on_oxygen else 0),
        )
    return pid


def test_scale_2_reaches_the_handover_for_a_charted_patient_on_air():
    """SpO2 90 on air: on target for this patient, NEWS2 0. Scale 1 would have
    scored it 3 and handed over 'call the doctor below 92%'."""
    draft = build_ipass(_admit(scale_2=True, spo2=90, on_oxygen=False))
    isv = draft["sections"]["illness_severity"]
    assert isv["spo2_scale"] == 2
    assert isv["news2"] == 0
    assert "(stable band, SpO2 Scale 2, target 88-92%)" in isv["text"]
    contingencies = draft["sections"]["situation_awareness"]["contingencies"]
    assert not any("below 92%" in c for c in contingencies), contingencies
    # The printed sheet and the stored summary carry the same line.
    assert "SpO2 Scale 2, target 88-92%" in draft["plain_text"]
    assert "SpO2 Scale 2, target 88-92%" in draft["source_summary"]
    assert not _SNAKE_CASE.findall(draft["plain_text"])


def test_scale_2_on_oxygen_hands_over_the_88_percent_threshold():
    """SpO2 90 on O2: on target, 2 points for the oxygen. The SpO2 contingency
    fires on the oxygen driver and must name 88%, not 92%."""
    draft = build_ipass(_admit(scale_2=True, spo2=90, on_oxygen=True))
    assert draft["sections"]["illness_severity"]["spo2_scale"] == 2
    contingencies = draft["sections"]["situation_awareness"]["contingencies"]
    assert SCALE2_SPO2_LINE in contingencies, contingencies
    assert SCALE1_SPO2_LINE not in contingencies
    assert SCALE2_OVER_TARGET_LINE not in contingencies
    assert f"- {SCALE2_SPO2_LINE}" in draft["plain_text"]


def test_scale_2_over_target_on_oxygen_hands_over_reduce_o2():
    draft = build_ipass(_admit(scale_2=True, spo2=95, on_oxygen=True))
    contingencies = draft["sections"]["situation_awareness"]["contingencies"]
    assert contingencies[:2] == [SCALE2_OVER_TARGET_LINE, SCALE2_SPO2_LINE], contingencies
    assert SCALE1_SPO2_LINE not in contingencies


def test_scale_1_patient_with_the_same_reading_is_unchanged():
    """The control: same vitals, Scale 1. The scale is per patient."""
    draft = build_ipass(_admit(scale_2=False, spo2=90, on_oxygen=False))
    isv = draft["sections"]["illness_severity"]
    assert isv["spo2_scale"] == 1
    assert "Scale 2" not in isv["text"]
    # SpO2 90 on Scale 1 scores 3 on its own; the I line names it (2026-10-03).
    assert isv["text"].startswith(
        "Watcher: NEWS2 3 (elevated band) · a single parameter scores 3. Key drivers: ")
    assert draft["sections"]["situation_awareness"]["contingencies"][0] == SCALE1_SPO2_LINE
    assert "Scale 2" not in draft["plain_text"]
