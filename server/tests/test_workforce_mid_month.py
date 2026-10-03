"""A month in progress: future days are scheduled, not "Not recorded".

On 1 October the muster flagged 248 future days as unrecorded, a warning nobody
could clear because record_attendance refuses a future date. The muster and the
export now count only up to the hospital's today, report the rest as days still
to come, and keep attendance_complete False until the month is over.

The hospital's "today" is pinned inside June 2026 (a past month, so attendance
can be recorded for the days before it).
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from services import workforce_service as svc

YEAR, MONTH = 2026, 6             # June 2026: 30 days, the 1st is a Monday
TODAY = date(2026, 6, 10)         # a Wednesday


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    monkeypatch.setattr(svc, "hospital_today", lambda: TODAY)


def _employee():
    return svc.upsert_employee(
        employee_code=f"WMM{uuid.uuid4().hex[:8].upper()}",
        full_name="Sister Kavya Nair",
        department=f"WMM-Ward-{uuid.uuid4().hex[:4]}",
        designation="Staff Nurse",
        date_of_joining="2020-01-01",
        weekly_off_day=6,  # Sunday
    )


def _present(code, day):
    start = datetime(day.year, day.month, day.day, 3, 0, tzinfo=timezone.utc)
    svc.record_attendance(employee_code=code, attendance_date=day.isoformat(),
                          status="present", first_in_at=start.isoformat(),
                          last_out_at=(start + timedelta(minutes=480)).isoformat())


def test_mid_month_summary_counts_only_to_today():
    emp = _employee()
    code = emp["employee_code"]
    for d in (1, 2, 3):
        _present(code, date(YEAR, MONTH, d))

    s = svc.monthly_summary(employee_code=code, year=YEAR, month=MONTH)
    assert s["days_in_period"] == 10, "1 to 10 June, not the whole month"
    assert s["days_still_to_come"] == 20
    assert s["counted_through"] == "2026-06-10"
    assert s["days_present"] == 3
    assert s["days_weekly_off"] == 1                     # Sunday 7 June
    assert s["days_unrecorded"] == 6                     # 4, 5, 6, 8, 9, 10 June
    assert all(d <= "2026-06-10" for d in s["unrecorded_dates"])
    assert s["identity"]["holds"], s["identity"]
    assert s["attendance_complete"] is False


def test_fully_recorded_so_far_is_still_not_complete_before_month_end():
    emp = _employee()
    code = emp["employee_code"]
    for d in (1, 2, 3, 4, 5, 6, 8, 9, 10):
        _present(code, date(YEAR, MONTH, d))
    s = svc.monthly_summary(employee_code=code, year=YEAR, month=MONTH)
    assert s["days_unrecorded"] == 0
    assert s["attendance_complete"] is False, "the month has not closed"


def test_muster_and_export_report_days_still_to_come():
    emp = _employee()
    code, dept = emp["employee_code"], emp["department"]
    _present(code, date(YEAR, MONTH, 1))

    muster = svc.period_summaries(year=YEAR, month=MONTH, department=dept)
    assert muster["period_closed"] is False
    assert muster["counted_through"] == "2026-06-10"
    assert muster["still_to_come_day_count"] == 20
    # 2, 3, 4, 5, 6, 8, 9, 10 June: past days nobody wrote down. Not 28.
    assert muster["unrecorded_day_count"] == 8

    export = svc.build_payroll_export(year=YEAR, month=MONTH, department=dept)
    row = export["rows"][0]
    assert row["attendance_complete"] == "no"
    assert int(row["days_unrecorded"]) == 8
    assert int(row["days_in_period"]) == 10
    assert any("not over" in w and "20 days still to come" in w for w in export["warnings"])
    # The file contract is unchanged: no column was added for the days to come.
    assert set(row) == set(svc.PAYROLL_EXPORT_COLUMNS)


def test_a_month_entirely_in_the_future_still_lists_its_people():
    emp = _employee()
    muster = svc.period_summaries(year=2026, month=7, department=emp["department"])
    assert muster["counted_through"] is None
    assert [e["employee_code"] for e in muster["employees"]] == [emp["employee_code"]]
    row = muster["employees"][0]
    assert row["days_unrecorded"] == 0
    assert row["days_still_to_come"] == 31


def test_roster_board_shows_future_lines_as_scheduled():
    emp = _employee()
    code, dept = emp["employee_code"], emp["department"]
    past, future = date(YEAR, MONTH, 9), date(YEAR, MONTH, 15)
    for day in (past, future):
        svc.assign_roster(roster_date=day.isoformat(), shift_code="MORNING",
                          employee_code=code, department=dept)
    board = svc.get_roster(date_from=past.isoformat(), date_to=future.isoformat(),
                           department=dept)
    by_date = {d["date"]: d for d in board["days"]}
    past_slot = by_date[past.isoformat()]["shifts"][0]
    future_slot = by_date[future.isoformat()]["shifts"][0]
    assert past_slot["assignments"][0]["actual_status"] == "unrecorded"
    assert past_slot["unrecorded_count"] == 1
    assert future_slot["assignments"][0]["actual_status"] == "scheduled"
    assert future_slot["unrecorded_count"] == 0
    assert future_slot["scheduled_count"] == 1
