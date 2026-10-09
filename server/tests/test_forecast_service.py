"""Tests for the Forecast service's fail-closed behaviour.

The forecast LSTM ships UNTRAINED (random init). The safety-critical contract is
that an untrained network must NEVER surface numbers as if they were neural
predictions — predict_patient_los() must route to the transparent, clearly
lower-confidence heuristic and tag the source accordingly. These tests lock that
contract in so a future change that "wires up" the model can't silently start
emitting random-init predictions to clinicians.

See services/forecast_service.py:predict_patient_los (the FAIL CLOSED guard) and
models/forecast_lstm.py (self.trained defaults to False until a validated
checkpoint loads).
"""
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import db_session
from models.forecast_lstm import load_model
from services import forecast_service, hospital_time
from services.forecast_service import (
    _heuristic_prediction,
    predict_hospital_flow,
    predict_patient_los,
)
from services.vitals_source import hl7v2


def test_model_ships_untrained():
    """Precondition for the fail-closed guard: no validated checkpoint exists in
    the test environment, so the model must report itself untrained."""
    model = load_model()
    assert model.trained is False


def test_untrained_model_routes_to_labelled_heuristic():
    """With an untrained model, predictions must be tagged as heuristic and
    explicitly flagged model_trained=False so the UI never attributes them to a
    trained neural network."""
    result = predict_patient_los("PAT_DOES_NOT_EXIST")
    assert result["prediction_source"] == "heuristic"
    assert result["model_trained"] is False


def test_heuristic_shape_for_unknown_patient():
    """The heuristic fallback returns a complete, low-confidence prediction shape
    even when the patient row is missing (no crash, no None fields)."""
    result = _heuristic_prediction("PAT_DOES_NOT_EXIST")
    for key in (
        "predicted_los_days",
        "discharge_probability",
        "acuity_trajectory",
        "confidence",
        "estimated_discharge_date",
    ):
        assert key in result
    # Heuristic confidence is intentionally low (<= 0.5) — it is rule-based, not learned.
    assert 0.0 < result["confidence"] <= 0.5
    assert result["predicted_los_days"] > 0


# ══════════════════════════════════════════════════════════════════════════════
#  DAY 0 — the measured entry, and the hospital-local day it is stamped with
#
#  Two defects live here:
#
#    1. The series started at TOMORROW, so the panel's first chart point was a
#       prediction sitting next to a Current Census card that said something
#       else. Day 0 must be MEASURED — today's census and today's actual
#       admissions/discharges — and flagged so no client mistakes it for output
#       of the model.
#
#    2. Every date key came from `datetime.now(timezone.utc)`. In a UTC+05:30
#       hospital the UTC date is YESTERDAY's until 05:30 local, so between
#       midnight and half five — night shift — day 0 was stamped with a day key
#       the browser (which computes its own from the LOCAL date, see
#       src/lib/datetime.ts) can never match, and "today's" counts were taken
#       from a UTC window that is not the ward's day at all.
#
#  Every test below is pinned to 20:00 UTC = 01:30 IST, i.e. inside that window,
#  because outside it the two conventions agree and a UTC implementation passes.
# ══════════════════════════════════════════════════════════════════════════════

IST = "Asia/Kolkata"
# 2027-03-14T20:00Z is 2027-03-15T01:30+05:30. UTC date and hospital date are
# DIFFERENT DAYS. The year is deliberately far from any real test-run clock so
# that rows other test modules write with `now` cannot land inside this window.
FROZEN_UTC = datetime(2027, 3, 14, 20, 0, tzinfo=timezone.utc)
UTC_DAY = "2027-03-14"
HOSPITAL_DAY = "2027-03-15"


@pytest.fixture()
def ist_after_midnight(monkeypatch):
    """Pin the appliance to IST and the clock to 01:30 local (= yesterday, UTC)."""
    hl7v2.set_default_timezone(IST)
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: FROZEN_UTC)
    forecast_service._hospital_flow_cache.clear()
    yield
    forecast_service._hospital_flow_cache.clear()
    hl7v2.set_default_timezone(None)  # back to the environment/OS zone


def _mk_patient(status: str = "admitted") -> str:
    pid = f"PFC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, status) "
            "VALUES (?, 'Forecast Fixture', ?, 'W1', 'W1-9', 'Pneumonia', ?)",
            (pid, f"MRN-{uuid.uuid4().hex[:8]}", status),
        )
    return pid


def _mk_admission(patient_id: str, created_at_utc: str) -> str:
    aid = f"ADMFC-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, ward, status, created_at) "
            "VALUES (?, ?, 'D001', 'W1', 'active', ?)",
            (aid, patient_id, created_at_utc),
        )
    return aid


def _mk_discharge_summary(patient_id: str, discharged_at_utc: str) -> None:
    with db_session() as db:
        db.execute(
            "INSERT INTO discharge_summaries (patient_id, author_id, summary, discharged_at) "
            "VALUES (?, 'D001', 'Fixture', ?)",
            (patient_id, discharged_at_utc),
        )


@pytest.fixture()
def cleanup_rows():
    """Drop everything a test in this section wrote, whatever it wrote."""
    made: list[str] = []
    yield made
    with db_session() as db:
        for pid in made:
            db.execute("DELETE FROM discharge_summaries WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
            # An admitted fixture patient gets a LoS prediction cached against it
            # by the forecast run itself — that row is a FK to patients.
            db.execute("DELETE FROM forecast_predictions WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))


def test_series_leads_with_a_measured_day_zero(ist_after_midnight):
    """Was: daily_forecasts[0] was TOMORROW's prediction. The Forecast panel had
    to synthesize its own day-0 anchor to stop the chart contradicting the
    census card next to it."""
    result = predict_hospital_flow(days_ahead=3, include_current_day=True)
    days = result["daily_forecasts"]

    assert len(days) == 4, "day 0 is extra, not a replacement for a forecast day"
    day0 = days[0]
    assert day0["is_current"] is True
    # Measured, and it says so under names that cannot be read as predictions.
    assert day0["measured_occupancy"] == result["current_occupancy"]
    assert day0["predicted_occupancy"] == result["current_occupancy"]
    assert "actual_admissions" in day0 and "actual_discharges" in day0
    # Everything after it is a prediction and must not carry the flag.
    assert all(not d.get("is_current") for d in days[1:])


def test_day_zero_census_is_the_real_census(ist_after_midnight, cleanup_rows):
    """Day 0's census is counted, not modelled: admitting one more patient moves
    it by exactly one."""
    before = predict_hospital_flow(days_ahead=2, include_current_day=True)

    pid = _mk_patient(status="admitted")
    cleanup_rows.append(pid)
    forecast_service._hospital_flow_cache.clear()

    after = predict_hospital_flow(days_ahead=2, include_current_day=True)
    assert after["daily_forecasts"][0]["measured_occupancy"] == \
        before["daily_forecasts"][0]["measured_occupancy"] + 1
    assert after["daily_forecasts"][0]["measured_occupancy"] == after["current_occupancy"]


def test_day_keys_are_the_hospital_local_day_not_the_utc_day(ist_after_midnight):
    """THE TRAP. At 01:30 IST the UTC date is still yesterday. A day-0 row keyed
    2027-03-14 is a row the ward — and the browser's localDateKey() — calls
    yesterday, so the client's anchor guard would never match it and the panel
    would show two 'today's."""
    days = predict_hospital_flow(days_ahead=3, include_current_day=True)["daily_forecasts"]

    assert days[0]["date"] == HOSPITAL_DAY
    assert days[0]["date"] != UTC_DAY
    # The predicted days step from the hospital's today, not from UTC's.
    assert [d["date"] for d in days[1:]] == ["2027-03-16", "2027-03-17", "2027-03-18"]
    assert UTC_DAY not in [d["date"] for d in days]
    # The human label agrees with the key (2027-03-15 was a Monday), and is
    # written day first ("Mon 15 Mar"), not the US "Mon Mar 15".
    assert days[0]["day_label"] == "Mon 15 Mar"
    assert days[1]["day_label"] == "Tue 16 Mar"


def test_today_counts_come_from_the_local_day_window(ist_after_midnight, cleanup_rows):
    """The other half of the trap: 'what happened today' is a UTC WINDOW around
    a local day, not a UTC date.

    Two admissions and two discharges, one each side of IST midnight — both on
    the SAME UTC date (2027-03-14). A UTC-date implementation counts 2; the
    hospital's day contains exactly 1.
    """
    inside = _mk_patient(status="discharged")   # 00:30 IST on the 15th
    before_midnight = _mk_patient(status="discharged")  # 22:30 IST on the 14th
    cleanup_rows.extend([inside, before_midnight])

    # Written in the two shapes the app actually produces: admission_service
    # writes `datetime.now(timezone.utc).isoformat()` (offset AND microseconds),
    # discharge_service leans on SQLite CURRENT_TIMESTAMP (space, naive UTC). A
    # plain string comparison against either would be a lie in a different way.
    _mk_admission(inside, "2027-03-14T19:00:00.123456+00:00")
    _mk_admission(before_midnight, "2027-03-14T17:00:00.123456+00:00")
    _mk_discharge_summary(inside, "2027-03-14 19:15:00")
    _mk_discharge_summary(before_midnight, "2027-03-14 17:15:00")
    forecast_service._hospital_flow_cache.clear()

    day0 = predict_hospital_flow(days_ahead=2, include_current_day=True)["daily_forecasts"][0]
    assert day0["actual_admissions"] == 1
    assert day0["actual_discharges"] == 1
    # The series keys carry the same measured numbers, so a client that walks
    # daily_forecasts plots a continuous curve rather than a hole.
    assert day0["predicted_admissions"] == 1
    assert day0["predicted_discharges"] == 1


def test_summary_is_computed_over_the_predicted_days_only(ist_after_midnight):
    """A measured day 0 must not move the peak, the averages or the totals, and
    `beds_available_in_48h` must keep meaning the second PREDICTED day."""
    with_day0 = predict_hospital_flow(days_ahead=5, include_current_day=True)
    forecast_service._hospital_flow_cache.clear()
    without = predict_hospital_flow(days_ahead=5, include_current_day=False)

    assert with_day0["summary"] == without["summary"]
    assert with_day0["daily_forecasts"][1:] == without["daily_forecasts"]


def test_default_series_still_starts_at_tomorrow(ist_after_midnight):
    """command_center_service reads daily_forecasts[0] POSITIONALLY as 'the next
    24 hours'. Day 0 is opt-in precisely so a measured census cannot land in
    that slot and be relabelled a projection."""
    days = predict_hospital_flow(days_ahead=1)["daily_forecasts"]
    assert len(days) == 1
    assert days[0]["date"] == "2027-03-16"
    assert not days[0].get("is_current")


def test_cache_is_keyed_by_window_shape_and_local_day(ist_after_midnight, monkeypatch):
    """Was: one unkeyed cache slot for every caller. The Command Center's
    days_ahead=1 result was served to the panel's days_ahead=7 request for the
    next hour (and an include_current_day result to callers that never asked for
    one); and a series computed at 23:50 local kept naming YESTERDAY as day 0
    until 00:50."""
    one_day = predict_hospital_flow(days_ahead=1)
    seven_day = predict_hospital_flow(days_ahead=7)
    assert len(one_day["daily_forecasts"]) == 1
    assert len(seven_day["daily_forecasts"]) == 7

    assert not predict_hospital_flow(days_ahead=7)["daily_forecasts"][0].get("is_current")
    assert predict_hospital_flow(days_ahead=7, include_current_day=True)[
        "daily_forecasts"][0]["is_current"] is True

    # Roll the clock past local midnight: the cached day 0 is now yesterday's
    # census with yesterday's date on it, and must not be served.
    monkeypatch.setattr(hospital_time, "_utc_now",
                        lambda: FROZEN_UTC.replace(day=15, hour=20))
    rolled = predict_hospital_flow(days_ahead=7, include_current_day=True)
    assert rolled["daily_forecasts"][0]["date"] == "2027-03-16"


def test_every_forecast_row_reconciles_with_the_one_before_it():
    """FOUND ON SCREEN 2026-08-12, not by a test: 3 of 7 census rows did not add
    up, and the occupancy percentage never equalled census/capacity.

    The loop carried a float and rounded admissions and occupancy INDEPENDENTLY,
    so the three numbers printed on a row were each individually defensible and
    collectively wrong. A bed manager reading a row that does not add up stops
    believing the board — and the same figures resurface on Command Center.

    The identity may legitimately break ONLY where the capacity clamp bites, so
    that is the single exemption.
    """
    from services.forecast_service import predict_hospital_flow, TOTAL_CAPACITY

    result = predict_hospital_flow(days_ahead=7)
    rows = result["daily_forecasts"]
    assert rows, "no forecast rows produced"

    prev = None
    for i, row in enumerate(rows):
        occ, adm, dis = (row["predicted_occupancy"], row["predicted_admissions"],
                         row["predicted_discharges"])
        for name, value in (("occupancy", occ), ("admissions", adm), ("discharges", dis)):
            assert isinstance(value, int), f"row {i} {name} is {type(value).__name__}, not int"

        # The percentage must describe the census actually shown beside it.
        assert row["occupancy_pct"] == round(occ / TOTAL_CAPACITY * 100, 1), (
            f"row {i}: {occ}/{TOTAL_CAPACITY} is not {row['occupancy_pct']}%")

        if prev is not None:
            expected = prev - dis + adm
            clamped = expected < 0 or expected > TOTAL_CAPACITY + 5
            if not clamped:
                assert occ == expected, (
                    f"row {i} does not reconcile: {prev} - {dis} + {adm} = "
                    f"{expected}, but the row shows {occ}")
        prev = occ


# ══════════════════════════════════════════════════════════════════════════════
#  Season is context, never a multiplier (ruled 6 October 2026)
# ══════════════════════════════════════════════════════════════════════════════
#  Admissions used to be scaled by Pulse's live internet data: +20% for a
#  thunderstorm, +35% for a flu surge, hand-picked. Orb is strictly offline and
#  those feeds are gone. Forecast now carries today's seasonal entries as a
#  plain list beside the numbers, and admissions are base rate x day of week.
# ══════════════════════════════════════════════════════════════════════════════

def _season_calendar(tmp_path, monkeypatch, state):
    """A fixture calendar in which 15 March 2027 (FROZEN_UTC's hospital day) is
    inside an in-peak season and an environmental entry, and a state to read it."""
    from services import seasonal_calendar, site_config_service
    from services.india_states import STATES
    from services.site_config_service import SiteConfig

    src = [{"name": "Fixture", "url": "https://example.org", "accessed": "2026-10-01"}]

    def entry(eid, kind, title, start, end, peak=None):
        e = {"id": eid, "kind": kind, "title": title, "zones": ["all_india"],
             "start": start, "end": end, "summary": "Fixture.",
             "typically_rises": ["Fever"], "sources": src}
        if peak:
            e["peak"] = {"start": peak[0], "end": peak[1]}
        return e

    doc = {
        "version": 1, "status": "draft", "reviewed_by": None, "reviewed_on": None,
        "compiled_on": "2026-10-06",
        "zones": [{"id": "all_india", "name": "All India", "states": list(STATES)}],
        "entries": [
            entry("hot-season", "environment", "Hot season", "03-01", "05-31"),
            entry("spring", "season", "Spring", "02-15", "04-15", peak=("03-10", "03-20")),
            entry("monsoon", "season", "Monsoon", "06-01", "09-30"),
        ],
    }
    path = tmp_path / "seasonal_calendar.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv(seasonal_calendar.CALENDAR_PATH_ENV, str(path))
    seasonal_calendar.reload_calendar()
    monkeypatch.setattr(site_config_service, "_loaded",
                        SiteConfig(present=state is not None, site_state=state))


def _no_env_keys(result: dict) -> None:
    for row in result["daily_forecasts"]:
        leftovers = [k for k in row if k.startswith("env_")]
        assert not leftovers, f"{row['date']} still carries {leftovers}"


def test_forecast_carries_the_season_and_no_environmental_fields(
        ist_after_midnight, tmp_path, monkeypatch):
    _season_calendar(tmp_path, monkeypatch, "MH")
    result = predict_hospital_flow(days_ahead=3, include_current_day=True)
    _no_env_keys(result)
    assert result["season"] == [
        {"id": "spring", "title": "Spring", "kind": "season", "in_peak": True},
        {"id": "hot-season", "title": "Hot season", "kind": "environment", "in_peak": False},
    ]


def test_forecast_season_is_empty_without_a_state_or_a_calendar(
        ist_after_midnight, tmp_path, monkeypatch):
    from services import seasonal_calendar

    _season_calendar(tmp_path, monkeypatch, None)
    assert predict_hospital_flow(days_ahead=2)["season"] == []

    forecast_service._hospital_flow_cache.clear()
    _season_calendar(tmp_path, monkeypatch, "MH")
    monkeypatch.setenv(seasonal_calendar.CALENDAR_PATH_ENV, str(tmp_path / "absent.json"))
    seasonal_calendar.reload_calendar()
    result = predict_hospital_flow(days_ahead=2)
    assert result["season"] == []
    _no_env_keys(result)


def test_the_season_does_not_move_a_single_number(ist_after_midnight, tmp_path, monkeypatch):
    """An in-peak season and none at all must give identical forecasts."""
    _season_calendar(tmp_path, monkeypatch, None)
    without = predict_hospital_flow(days_ahead=7, include_current_day=True)
    forecast_service._hospital_flow_cache.clear()
    _season_calendar(tmp_path, monkeypatch, "MH")
    with_season = predict_hospital_flow(days_ahead=7, include_current_day=True)
    assert with_season["season"], "the fixture should put this day in season"
    assert with_season["daily_forecasts"] == without["daily_forecasts"]
    assert with_season["summary"] == without["summary"]


def test_admissions_are_base_rate_times_day_of_week_only(ist_after_midnight):
    """No weather, air-quality or flu factor is left in the arithmetic."""
    from datetime import date as _date

    assert not hasattr(forecast_service, "_get_environmental_multipliers")
    with db_session() as db:
        history = db.execute(
            """SELECT admission_date, COUNT(*) as cnt
               FROM admission_history
               WHERE admission_date >= date('now', '-30 days')
               GROUP BY admission_date"""
        ).fetchall()
    rate = sum(h["cnt"] for h in history) / len(history) if history else 4.5
    for row in predict_hospital_flow(days_ahead=7)["daily_forecasts"]:
        dow = _date.fromisoformat(row["date"]).weekday()
        expected = int(round(round(rate * forecast_service.DOW_MULTIPLIERS[dow], 1)))
        assert row["predicted_admissions"] == expected, row


def test_beds_and_admissions_off_adds_no_capacity_source(ist_after_midnight, monkeypatch):
    """adt-14 pin: with Beds and admissions off the forecast answers carry no
    capacity source and every bed count is the site setting, as before."""
    import config

    monkeypatch.setattr(config, "ADT_ENABLED", False)
    assert forecast_service.capacity_source_summary() is None
    assert "capacity_source" not in predict_hospital_flow(days_ahead=2)
    capacity = forecast_service.get_bed_capacity_forecast()
    assert "capacity_source" not in capacity
    assert all("capacity_source" not in row for row in capacity["current"].values())
