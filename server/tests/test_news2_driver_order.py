"""NEWS2 drivers are listed WORST FIRST (ruled by Samuel 2026-10-02).

`compute_news2` returns at most three drivers. They used to be the first three
in the order the parameters are CHECKED (RR, SpO2, O2, SBP, HR, Temp, ACVPU),
so a patient whose escalation came from a new ACVPU change behind three 1-point
vitals got a driver strip that never mentioned the confusion:

    RR 10, SpO2 95, HR 105, new confusion  ->  NEWS2 6, elevated
    drivers: Low RR (10); Mild hypoxia (SpO2 95%); Mild tachycardia (HR 105)

Those drivers are what the alert card's Tier 1 strip, Sage's prompt, the
guideline-retrieval query, the handoff and the downtime sheet print. They are
now ordered by points, then Severe before Moderate (2026-10-03; a new ACVPU
change sorts with Severe), then RCP chart order (RR, SpO2, O2, SBP, HR, ACVPU,
Temp), which is the same order the Relay reason line uses
(`news2_reason_line`), so the strip and the alert line can no longer disagree
about what made the patient sick. Expected lists are literals.
"""
from __future__ import annotations

import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.vigil_service import compute_news2, news2_reason_line  # noqa: E402


def _drivers(hr=80, sbp=120, rr=16, temp=37.0, spo2=97, consciousness="alert",
             on_oxygen=False, on_scale_2=False):
    return compute_news2(hr, sbp, rr, temp, spo2, consciousness=consciousness,
                         on_oxygen=on_oxygen, on_scale_2=on_scale_2)["drivers"]


def test_new_confusion_behind_three_1_point_vitals_leads_the_drivers():
    assert _drivers(rr=10, spo2=95, hr=105, consciousness="confusion") == [
        "Altered consciousness (ACVPU)", "Low RR (10)", "Mild hypoxia (SpO2 95%)"]


def test_a_2_point_parameter_checked_later_outranks_a_1_point_one_checked_first():
    assert _drivers(rr=10, sbp=95) == ["Hypotension (SBP 95)", "Low RR (10)"]


def test_peri_arrest_names_three_of_the_3_pointers_not_the_oxygen():
    # Five parameters score 3. Check order used to spend the third slot on
    # the 2-point "On supplemental oxygen" and hide hypotension, tachycardia
    # and the ACVPU change.
    assert _drivers(hr=138, sbp=78, rr=32, temp=39.4, spo2=86,
                    consciousness="voice", on_oxygen=True) == [
        "Severe tachypnea (RR 32)", "Severe hypoxia (SpO2 86%)",
        "Severe hypotension (SBP 78)"]


def test_a_severe_3_pointer_outranks_moderate_ones_checked_before_it():
    # Septic shock. Chart order alone named three Moderate findings and
    # counted Severe tachycardia (review finding F1, ruled 2026-10-03).
    assert _drivers(hr=142, sbp=82, rr=26, temp=39.5, spo2=90) == [
        "Severe tachycardia (HR 142)", "Moderate tachypnea (RR 26)",
        "Moderate hypoxia (SpO2 90%)"]


def test_an_acvpu_change_sorts_with_severe_ahead_of_mild_hypothermia():
    assert _drivers(rr=7, temp=34.8, consciousness="confusion") == [
        "Severe bradypnea (RR 7)", "Altered consciousness (ACVPU)",
        "Hypothermia (34.8°C)"]


def test_inside_the_severe_group_the_rcp_chart_puts_consciousness_before_temperature():
    # Both Severe-ranked, so only the chart can order them. The engine checks
    # temperature BEFORE consciousness; the chart is the other way round, and
    # the chart is what a nurse reads top to bottom.
    assert _drivers(rr=7, temp=31.6, consciousness="confusion") == [
        "Severe bradypnea (RR 7)", "Altered consciousness (ACVPU)",
        "Severe hypothermia (31.6°C)"]


def test_severe_is_marked_on_the_breakdown_where_the_word_is_chosen():
    rows = {b["parameter"]: b for b in compute_news2(
        142, 82, 26, 31.6, 90, consciousness="voice", on_oxygen=True)["breakdown"]}
    assert {k: v["severe"] for k, v in rows.items()} == {
        "Heart rate": True, "Systolic BP": False, "Respiratory rate": False,
        "Temperature": True, "SpO2": False, "Consciousness (ACVPU)": True,
        "Supplemental O2": False}


def test_scale_2_drivers_sort_on_the_same_rule():
    assert _drivers(spo2=97, on_oxygen=True, on_scale_2=True, hr=105) == [
        "Over-oxygenation on O2 (97%, Scale 2)", "On supplemental oxygen",
        "Mild tachycardia (HR 105)"]


def test_the_driver_strip_and_the_relay_reason_line_name_the_same_parameters():
    """One order, two surfaces. Over a grid that crosses every band edge the
    lanes care about, the drivers are exactly the parameters the reason line
    names, in the same order."""
    grid = itertools.product(
        (40, 105, 125, 140),          # HR
        (78, 88, 95, 170),            # SBP
        (7, 10, 22, 26),              # RR
        (34.8, 37.0, 39.4),           # Temp
        (86, 93, 95),                 # SpO2
        ("alert", "confusion"),
        (False, True),                # on O2
    )
    checked = 0
    for hr, sbp, rr, temp, spo2, acvpu, o2 in grid:
        r = compute_news2(hr, sbp, rr, temp, spo2, consciousness=acvpu, on_oxygen=o2)
        named = news2_reason_line(r).split(" (+")[0].split("; ")
        assert r["drivers"] == named, (hr, sbp, rr, temp, spo2, acvpu, o2, r["drivers"], named)
        checked += 1
    assert checked == 4 * 4 * 4 * 3 * 3 * 2 * 2


def test_the_empty_cases_are_unchanged():
    assert _drivers() == ["All vitals within normal range"]
    assert compute_news2(80, None, None, None, None)["drivers"] == [
        "Within range on what was measured (1/7 parameters)"]
