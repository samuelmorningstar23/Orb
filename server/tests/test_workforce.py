"""Workforce — duty roster, attendance, overtime and the payroll export.

WHAT THIS FILE IS DEFENDING, in order of what it would cost to get wrong:

  1. THE SUMMING IDENTITY. present + absent + leave + unrecorded == working
     days, and every bucket together == the days the person was employed in the
     period. Every figure a payroll bureau receives is derived from this
     partition, so a day that lands in two buckets (or in none) is a day
     somebody is paid twice for or not at all — and NOTHING ON ANY SCREEN WOULD
     LOOK WRONG. This is the prove-can-fail target; see the note at the bottom.

  2. THE UNRECORDED BUCKET. A day nobody wrote down is neither present nor
     absent. Folding it either way is silent and is the exact failure class this
     project keeps paying for, so it is asserted from three directions: the
     summary counts it, the CSV carries it, and `attendance_complete` says no.

  3. THE OVERTIME BOUNDARY. `> threshold`, not `>=`. Tested at threshold−1,
     threshold and threshold+1 because an off-by-one here mis-pays one minute
     per employee per day forever and looks like nothing.

  4. THE EXPORT'S COLUMN CONTRACT. The exact tuple, in order, with the six
     statutory columns EMPTY on every row and `statutory_computed_by` naming the
     payroll provider. A bureau's importer is a column-order-sensitive
     spreadsheet macro; a silent reorder is a corrupted payroll run.

  5. ROSTER CONFLICT DETECTION, including across midnight. A night shift is the
     case a same-date comparison misses, and a night shift is exactly when a
     hospital cannot afford to think a nurse is on duty who is not.

  6. NOTHING HERE TOUCHES A CLINICAL PATH, and the module switch actually
     switches the module off.

PROVEN ABLE TO FAIL (2026-08-12). Twelve defects were reintroduced into
services/workforce_service.py one at a time, the suite watched go RED, and the
file restored. What each one caught:

  a. `_classify_period` folding the unrecorded bucket into `absent` — the single
     most tempting simplification in this module
     → 6 failures incl. test_unrecorded_is_not_absent,
       test_export_marks_an_incomplete_row
  b. `working_days` computed as `present + absent + leave` (unrecorded dropped)
     → 6 failures incl. test_identity_holds_with_gaps
  c. daily overtime threshold set one minute low
     → test_overtime_boundary[480-0] and [481-1] and [600-120]
  d. overtime counting the threshold minute itself
     (`span - threshold + 1 if span >= threshold`)
     → the same three boundary cases
  e. overtime returning the WHOLE span once past the threshold
     → test_overtime_boundary[481-1] and [600-120]; the [480-0] case correctly
       stayed green, which is what a boundary suite is supposed to do
  f. the `max(0, …)` guard removed, so a short day accrues NEGATIVE overtime
     → test_overtime_boundary[479-0], via the schema CHECK constraint — i.e. the
       database refused it, which is the layer that was meant to
  g. `_absolute_window` ignoring the roster date (comparing clock times)
     → test_night_shift_conflicts_across_midnight,
       test_back_to_back_shifts_warn_but_do_not_refuse
  h. `STATUTORY_COLUMNS` filled with "0.00" instead of ""
     → test_statutory_columns_are_empty_on_every_row
  i. the `continue` removed from the recorded-day branch, so a day counted BOTH
     as present and as a weekly off
     → 5 failures; the identity block reported buckets_sum 60 against 30 days
  j. paid/unpaid leave collapsed into one bucket
     → test_the_export_carries_the_countable_facts
  k. two export columns swapped
     → 6 failures across the service, the CSV, the run signature and the API
  l. the employment-start guard removed (phantom absences before joining)
     → test_a_mid_month_joiner_has_no_phantom_absences
  m. the late-mark grace ignored, so arriving on the grace minute is late
     → test_the_grace_period_boundary[10-0]
  n. the grace SUBTRACTED from the reported lateness (a 40-minute arrival
     reported as 30) — the grace decides whether, never by how much
     → 4 failures across the late tests
  o. the grace edge made `>=` instead of `>`
     → test_the_grace_period_boundary[10-0]
  p. the scheduled start computed in UTC instead of the hospital's zone — the
     defect that would mark every nurse in an IST hospital 5h30 late, daily
     → 4 failures
  q. the shift-length clamp removed, so a night recorded against MORNING
     produces a 14-hour late mark
     → test_a_punch_that_cannot_belong_to_the_shift_is_not_a_late_mark

RECORDED HONESTLY, because it is the more useful half: the FIRST attempt at (c)
mutated `span > threshold` to `span >= threshold` and the suite stayed green —
correctly, because with `overtime = span - threshold` the two expressions give
the identical answer at the boundary. That is a no-op mutation, not a missed
defect. The four real off-by-ones above were then run in its place. A green run
against a mutation is only evidence once you have checked the mutation changes
behaviour at all.
"""
import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

# Imported as a MODULE, not `from config import WORKFORCE_ENABLED`: the router
# reads `config.WORKFORCE_ENABLED` at call time, so the module-flag test has to
# patch the same module object rather than a copied constant.
import config
from auth import create_token
from database import db_session
from db_backend import DBIntegrityError
from main import app
from services import workforce_service as svc

client = TestClient(app)

# Distinctive so this module's fixtures cannot collide with another agent's in
# the shared session DB.
TAG = "WFX"

# A fixed month in the past. `record_attendance` refuses a future date, and a
# month anchored on "now" would change length (and weekday alignment) between
# runs — a test that silently means something different in March is not a test.
YEAR, MONTH = 2026, 6          # June 2026: 30 days, the 1st is a Monday
PERIOD_START = date(YEAR, MONTH, 1)
PERIOD_END = date(YEAR, MONTH, 30)


def _uid() -> str:
    return uuid.uuid4().hex[:8].upper()


def _mk_user(role: str = "admin") -> str:
    uid = f"{TAG}U{_uid()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES (?,?,?,'x')",
            (uid, f"Workforce {role}", role),
        )
    return uid


def _auth(user_id: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


@pytest.fixture(scope="module")
def admin_headers() -> dict:
    return _auth(_mk_user("admin"), "admin")


@pytest.fixture(scope="module")
def nurse_headers() -> dict:
    return _auth(_mk_user("nurse"), "nurse")


def _mk_employee(**kwargs) -> dict:
    defaults = dict(
        employee_code=f"{TAG}E{_uid()}",
        full_name="Sister Anita Rao",
        department=f"{TAG}-Ward-A",
        designation="Staff Nurse",
        date_of_joining="2020-01-01",
        weekly_off_day=6,  # Sunday
    )
    defaults.update(kwargs)
    return svc.upsert_employee(**defaults)


@pytest.fixture
def employee() -> dict:
    return _mk_employee()


def _utc(day: date, hour: int, minute: int = 0, plus_days: int = 0) -> str:
    """A UTC instant for `day` at a given hour. The service takes instants, so
    the tests build instants — no test here depends on the appliance timezone
    except the ones that are explicitly about lateness."""
    return datetime(day.year, day.month, day.day, hour, minute,
                    tzinfo=timezone.utc).isoformat() if plus_days == 0 else (
        datetime(day.year, day.month, day.day, hour, minute,
                 tzinfo=timezone.utc) + timedelta(days=plus_days)).isoformat()


def _present(employee_code: str, day: date, *, minutes: int, shift: str | None = None,
             start_hour: int = 3) -> dict:
    """Mark a present day of exactly `minutes` worked."""
    start = datetime(day.year, day.month, day.day, start_hour, 0, tzinfo=timezone.utc)
    return svc.record_attendance(
        employee_code=employee_code, attendance_date=day.isoformat(),
        status="present", shift_code=shift,
        first_in_at=start.isoformat(),
        last_out_at=(start + timedelta(minutes=minutes)).isoformat(),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  1. THE SUMMING IDENTITY — the arithmetic every payroll figure rests on
# ═════════════════════════════════════════════════════════════════════════════

class TestSummaryArithmetic:
    """present + absent + leave == working days, and every bucket sums to the
    days employed. This is the file's centre of gravity."""

    def test_summary_arithmetic_closes(self, employee):
        """A fully recorded month: every one of the 30 days is accounted for
        exactly once, and present + absent + leave == working days with no
        unrecorded remainder."""
        code = employee["employee_code"]
        for day in (PERIOD_START + timedelta(days=i) for i in range(30)):
            if day.weekday() == 6:                       # her fixed weekly off
                svc.record_attendance(employee_code=code,
                                      attendance_date=day.isoformat(),
                                      status="weekly_off")
            elif day.day in (10, 11):
                svc.record_attendance(employee_code=code,
                                      attendance_date=day.isoformat(),
                                      status="leave", leave_type_code="CL")
            elif day.day == 12:
                svc.record_attendance(employee_code=code,
                                      attendance_date=day.isoformat(),
                                      status="absent")
            else:
                _present(code, day, minutes=480)

        s = svc.monthly_summary(employee_code=code, year=YEAR, month=MONTH)

        assert s["days_unrecorded"] == 0, "the month was fully recorded"
        # THE STATED INVARIANT.
        assert s["days_present"] + s["days_absent"] + s["days_leave_total"] \
            == s["days_working"]
        # And the whole partition.
        assert (s["days_present"] + s["days_absent"] + s["days_leave_total"]
                + s["days_unrecorded"] + s["days_weekly_off"] + s["days_holiday"]) \
            == s["days_in_period"] == 30
        assert s["identity"]["holds"] is True

        # The individual counts, so a shift of days between two buckets that
        # happens to preserve the sum is still caught.
        assert s["days_weekly_off"] == 4      # 7, 14, 21, 28 June 2026
        assert s["days_leave_total"] == 2
        assert s["days_leave_paid"] == 2      # CL is a paid type
        assert s["days_leave_unpaid"] == 0
        assert s["days_absent"] == 1
        assert s["days_present"] == 23
        assert s["days_working"] == 26

    def test_identity_holds_with_gaps(self, employee):
        """The same identity, with three days nobody wrote down. `working_days`
        must still equal present + absent + leave + UNRECORDED — this is the
        assertion that goes red the moment somebody 'simplifies' the unrecorded
        bucket away."""
        code = employee["employee_code"]
        for day in (PERIOD_START + timedelta(days=i) for i in range(30)):
            if day.weekday() == 6 or day.day in (5, 6, 7):
                continue                       # 5 and 6 unrecorded; 7 is a Sunday
            _present(code, day, minutes=480)

        s = svc.monthly_summary(employee_code=code, year=YEAR, month=MONTH)

        assert s["days_unrecorded"] == 2, "5 and 6 June were never recorded"
        assert s["days_present"] + s["days_absent"] + s["days_leave_total"] \
            + s["days_unrecorded"] == s["days_working"]
        assert (s["days_present"] + s["days_absent"] + s["days_leave_total"]
                + s["days_unrecorded"] + s["days_weekly_off"] + s["days_holiday"]) \
            == s["days_in_period"]
        assert s["identity"]["holds"] is True
        assert s["attendance_complete"] is False

    def test_unrecorded_is_not_absent(self, employee):
        """An empty month is 26 unrecorded days and ZERO absences. A hospital
        that has not started using the module must not appear to have a ward
        that never came to work."""
        s = svc.monthly_summary(employee_code=employee["employee_code"],
                                year=YEAR, month=MONTH)
        assert s["days_absent"] == 0
        assert s["days_present"] == 0
        assert s["days_unrecorded"] == 26          # 30 − 4 Sundays
        assert s["days_weekly_off"] == 4
        assert s["attendance_complete"] is False
        assert s["identity"]["holds"] is True

    def test_a_nurse_who_works_her_day_off_is_counted_once(self, employee):
        """Sunday is her weekly off. She came in anyway. That day is PRESENT,
        it counts toward working days, her weekly-off count drops by one, and
        `weekly_off_worked_count` tells the bureau it happened. The one thing it
        must not be is counted in two buckets."""
        code = employee["employee_code"]
        sunday = date(YEAR, MONTH, 7)
        assert sunday.weekday() == 6
        _present(code, sunday, minutes=480)

        s = svc.monthly_summary(employee_code=code, year=YEAR, month=MONTH)
        assert s["days_present"] == 1
        assert s["days_weekly_off"] == 3, "the Sunday she worked is not also a day off"
        assert s["weekly_off_worked_count"] == 1
        assert (s["days_present"] + s["days_absent"] + s["days_leave_total"]
                + s["days_unrecorded"] + s["days_weekly_off"] + s["days_holiday"]) \
            == s["days_in_period"] == 30
        assert s["identity"]["holds"] is True

    def test_a_mid_month_joiner_has_no_phantom_absences(self):
        """Somebody who joined on the 15th did not miss the first fourteen days.
        Days outside the employment window are not in any bucket at all."""
        employee = _mk_employee(date_of_joining=f"{YEAR}-{MONTH:02d}-15")
        s = svc.monthly_summary(employee_code=employee["employee_code"],
                                year=YEAR, month=MONTH)
        assert s["days_in_period"] == 16          # 15..30 June
        assert s["days_absent"] == 0
        assert (s["days_present"] + s["days_absent"] + s["days_leave_total"]
                + s["days_unrecorded"] + s["days_weekly_off"] + s["days_holiday"]) == 16

    def test_a_leaver_is_still_in_their_final_payroll_file(self):
        """An employee who left mid-period is inactive but MUST appear — they
        were paid for the days they worked, and a leaver missing from their last
        payroll file is the most expensive error this export can make."""
        employee = _mk_employee(date_of_exit=f"{YEAR}-{MONTH:02d}-12", is_active=False)
        summary = svc.period_summaries(year=YEAR, month=MONTH,
                                       department=employee["department"])
        codes = [e["employee_code"] for e in summary["employees"]]
        assert employee["employee_code"] in codes
        row = next(e for e in summary["employees"]
                   if e["employee_code"] == employee["employee_code"])
        assert row["days_in_period"] == 12

    def test_a_declared_holiday_leaves_the_working_days(self, employee):
        code = employee["employee_code"]
        svc.upsert_holiday(holiday_date=f"{YEAR}-{MONTH:02d}-16",
                           name=f"{TAG} Founders Day")
        try:
            s = svc.monthly_summary(employee_code=code, year=YEAR, month=MONTH)
            assert s["days_holiday"] == 1
            assert s["days_working"] == 25       # 30 − 4 Sundays − 1 holiday
            assert s["identity"]["holds"] is True
        finally:
            svc.delete_holiday(f"{YEAR}-{MONTH:02d}-16")

    def test_a_rotational_employee_gets_no_invented_day_off(self):
        """`weekly_off_day = NULL` means the day off rotates. Orb must NOT pick
        one: inventing a day off silently converts an unrecorded day into an
        excused one, which is the same class of defect as calling it absent."""
        employee = _mk_employee(weekly_off_day=None)
        s = svc.monthly_summary(employee_code=employee["employee_code"],
                                year=YEAR, month=MONTH)
        assert s["days_weekly_off"] == 0
        assert s["days_unrecorded"] == 30
        assert employee["weekly_off_label"] == "Rotational"


# ═════════════════════════════════════════════════════════════════════════════
#  2. THE OVERTIME BOUNDARY
# ═════════════════════════════════════════════════════════════════════════════

class TestOvertimeBoundary:
    """`worked > threshold` accrues overtime; `worked == threshold` does not."""

    @pytest.mark.parametrize("worked_minutes,expected_ot", [
        (479, 0),    # one minute short of the threshold
        (480, 0),    # EXACTLY the threshold — not overtime
        (481, 1),    # one minute past
        (600, 120),
    ])
    def test_overtime_boundary(self, employee, worked_minutes, expected_ot):
        assert config.WORKFORCE_OT_DAILY_MINUTES == 480, (
            "this boundary test is written against the shipped 8h default")
        row = _present(employee["employee_code"], date(YEAR, MONTH, 3),
                       minutes=worked_minutes)
        assert row["worked_minutes"] == worked_minutes
        assert row["overtime_minutes"] == expected_ot

    def test_overtime_accumulates_across_the_month(self, employee):
        code = employee["employee_code"]
        _present(code, date(YEAR, MONTH, 2), minutes=540)   # +60
        _present(code, date(YEAR, MONTH, 3), minutes=510)   # +30
        _present(code, date(YEAR, MONTH, 4), minutes=480)   # +0
        s = svc.monthly_summary(employee_code=code, year=YEAR, month=MONTH)
        assert s["overtime_minutes"] == 90
        assert s["overtime_hours"] == "1.50"

    def test_a_non_present_day_cannot_carry_worked_minutes(self, employee):
        """Enforced by the DATABASE, not only by the service. A leave day with
        480 worked minutes would inflate a month's hours and pass every
        service-level check, so the CHECK constraint is the guarantee that
        survives somebody writing raw SQL."""
        with pytest.raises(DBIntegrityError):
            with db_session() as db:
                db.execute(
                    "INSERT INTO attendance_days (attendance_date, employee_code, "
                    "status, leave_type_code, worked_minutes) VALUES (?,?,?,?,?)",
                    (f"{YEAR}-{MONTH:02d}-09", employee["employee_code"],
                     "leave", "CL", 480),
                )

    def test_a_leave_day_must_name_its_leave_type(self, employee):
        with pytest.raises(svc.WorkforceError) as exc:
            svc.record_attendance(employee_code=employee["employee_code"],
                                  attendance_date=f"{YEAR}-{MONTH:02d}-09",
                                  status="leave")
        assert exc.value.code == "leave_type_required"
        # And at the database, for the same reason as above.
        with pytest.raises(DBIntegrityError):
            with db_session() as db:
                db.execute(
                    "INSERT INTO attendance_days (attendance_date, employee_code, status) "
                    "VALUES (?,?,?)",
                    (f"{YEAR}-{MONTH:02d}-08", employee["employee_code"], "leave"),
                )

    def test_an_impossible_span_is_refused_rather_than_paid(self, employee):
        """A punch-out that was never recorded shows up as a 30-hour day. Paying
        22 hours of overtime for it is worse than refusing the entry."""
        day = date(YEAR, MONTH, 4)
        with pytest.raises(svc.WorkforceError) as exc:
            svc.record_attendance(
                employee_code=employee["employee_code"],
                attendance_date=day.isoformat(), status="present",
                first_in_at=_utc(day, 6),
                last_out_at=_utc(day, 12, plus_days=1),   # 30 hours later
            )
        assert exc.value.code == "span_too_long"

    def test_a_late_mark_is_real_minutes_late(self, employee):
        """MORNING starts 07:00 hospital-local. Arriving 40 minutes past that,
        with a 10-minute grace, is a 40-minute late mark."""
        from services.hospital_time import hospital_timezone
        day = date(YEAR, MONTH, 23)
        tz = hospital_timezone()
        start = datetime(day.year, day.month, day.day, 7, 40, tzinfo=tz)
        row = svc.record_attendance(
            employee_code=employee["employee_code"], attendance_date=day.isoformat(),
            status="present", shift_code="MORNING",
            first_in_at=start.isoformat(),
            last_out_at=(start + timedelta(minutes=420)).isoformat(),
        )
        assert row["late_minutes"] == 40

    @pytest.mark.parametrize("minutes_after_start,expected_late", [
        (0, 0),     # exactly on time
        (10, 0),    # EXACTLY the grace — not late
        (11, 11),   # one minute past the grace
        (40, 40),
    ])
    def test_the_grace_period_boundary(self, employee, minutes_after_start, expected_late):
        """The grace is a courtesy, and its edge is `>`, not `>=`. Arriving on
        the grace minute itself is not a late mark; one minute past it reports
        the FULL lateness from the scheduled start, not the excess over the
        grace — the grace decides WHETHER it is late, never by how much."""
        from services.hospital_time import hospital_timezone
        assert config.WORKFORCE_LATE_GRACE_MINUTES == 10, (
            "this boundary test is written against the shipped 10-minute grace")
        day = date(YEAR, MONTH, 28)
        tz = hospital_timezone()
        start = (datetime(day.year, day.month, day.day, 7, 0, tzinfo=tz)
                 + timedelta(minutes=minutes_after_start))
        row = svc.record_attendance(
            employee_code=employee["employee_code"], attendance_date=day.isoformat(),
            status="present", shift_code="MORNING",
            first_in_at=start.isoformat(),
            last_out_at=(start + timedelta(minutes=420)).isoformat(),
        )
        assert row["late_minutes"] == expected_late

    def test_late_is_measured_in_the_hospitals_own_timezone(self, employee):
        """The roster's clock times are HOSPITAL-LOCAL; the punches are UTC
        instants. Comparing a local 07:00 against a UTC punch marks every nurse
        in an India-time hospital 5h30 late, every single day.

        A nurse arriving exactly at the hospital's 07:00 is not late — asserted
        through the same resolver the service uses, so the test states the
        invariant rather than the appliance's current offset."""
        from services.hospital_time import hospital_timezone
        day = date(YEAR, MONTH, 29)
        tz = hospital_timezone()
        on_time = datetime(day.year, day.month, day.day, 7, 0, tzinfo=tz)
        row = svc.record_attendance(
            employee_code=employee["employee_code"], attendance_date=day.isoformat(),
            status="present", shift_code="MORNING",
            first_in_at=on_time.isoformat(),
            last_out_at=(on_time + timedelta(minutes=420)).isoformat(),
        )
        assert row["late_minutes"] == 0
        # And the stored instant really is UTC, whatever zone it arrived in.
        assert row["first_in_at"].endswith("+00:00")

    def test_a_punch_that_cannot_belong_to_the_shift_is_not_a_late_mark(self, employee):
        """A night worked but recorded against MORNING. The punch-in is 14 hours
        after that shift's 07:00 start, which is longer than the shift itself —
        so it is not this shift's instance at all.

        Orb must NOT write "840 minutes late" into a payroll file. A late figure
        there is a disciplinary document, and this one would have been
        manufactured out of a data-entry slip. The hours worked still record what
        happened; only the invented late mark is withheld."""
        from services.hospital_time import hospital_timezone
        day = date(YEAR, MONTH, 26)
        tz = hospital_timezone()
        start = datetime(day.year, day.month, day.day, 21, 0, tzinfo=tz)
        row = svc.record_attendance(
            employee_code=employee["employee_code"], attendance_date=day.isoformat(),
            status="present", shift_code="MORNING",   # the wrong shift
            first_in_at=start.isoformat(),
            last_out_at=(start + timedelta(minutes=600)).isoformat(),
        )
        assert row["late_minutes"] == 0, "a 14-hour 'late' mark is a fabrication"
        assert row["worked_minutes"] == 600, "what they actually worked is still recorded"

    def test_the_last_minute_of_the_shift_still_counts_as_late(self, employee):
        """The clamp is at the shift's own length, inclusive — arriving with one
        minute of a 7-hour shift left is still (very) late, not unrecordable."""
        from services.hospital_time import hospital_timezone
        day = date(YEAR, MONTH, 27)
        tz = hospital_timezone()
        start = datetime(day.year, day.month, day.day, 7, 0, tzinfo=tz) + timedelta(minutes=420)
        row = svc.record_attendance(
            employee_code=employee["employee_code"], attendance_date=day.isoformat(),
            status="present", shift_code="MORNING",
            first_in_at=start.isoformat(),
            last_out_at=(start + timedelta(minutes=60)).isoformat(),
        )
        assert row["late_minutes"] == 420

    def test_minutes_to_hours_is_exact(self):
        """Rendered by integer arithmetic — no float ever touches a payroll
        figure. Half-up on the hundredth."""
        assert svc.minutes_to_hours(0) == "0.00"
        assert svc.minutes_to_hours(30) == "0.50"
        assert svc.minutes_to_hours(90) == "1.50"
        assert svc.minutes_to_hours(486) == "8.10"
        assert svc.minutes_to_hours(1) == "0.02"      # 0.01666… → 0.02
        assert svc.minutes_to_hours(485) == "8.08"    # 8.0833…  → 8.08


# ═════════════════════════════════════════════════════════════════════════════
#  3. ROSTER CONFLICT DETECTION
# ═════════════════════════════════════════════════════════════════════════════

class TestRosterConflicts:
    """Nobody is in two places at once — including across midnight."""

    def test_same_person_two_overlapping_shifts_is_refused(self, employee):
        code = employee["employee_code"]
        day = f"{YEAR}-{MONTH:02d}-02"
        svc.assign_roster(roster_date=day, shift_code="MORNING", employee_code=code)
        with pytest.raises(svc.RosterConflict) as exc:
            # GENERAL is 09:00-17:00; MORNING is 07:00-14:00. They overlap.
            svc.assign_roster(roster_date=day, shift_code="GENERAL", employee_code=code)
        assert exc.value.code == "roster_conflict"
        assert exc.value.conflicts
        assert exc.value.conflicts[0]["kind"] == "overlap"
        assert exc.value.conflicts[0]["shift_code"] == "MORNING"

    def test_the_same_slot_twice_is_refused(self, employee):
        code = employee["employee_code"]
        day = f"{YEAR}-{MONTH:02d}-03"
        svc.assign_roster(roster_date=day, shift_code="NIGHT", employee_code=code)
        with pytest.raises(svc.RosterConflict):
            svc.assign_roster(roster_date=day, shift_code="NIGHT", employee_code=code)

    def test_night_shift_conflicts_across_midnight(self, employee):
        """NIGHT on the 3rd runs 21:00 → 07:00 on the 4th. An 02:00 shift on the
        4th overlaps it, and a same-DATE comparison would miss that completely —
        which is why the schema stores start + duration and the detector works
        in absolute minutes."""
        code = employee["employee_code"]
        svc.upsert_shift_type(code=f"{TAG}EARLY", name="Early relief (02:00-06:00)",
                              start_minute=2 * 60, duration_minutes=4 * 60)
        svc.assign_roster(roster_date=f"{YEAR}-{MONTH:02d}-03", shift_code="NIGHT",
                          employee_code=code)
        with pytest.raises(svc.RosterConflict) as exc:
            svc.assign_roster(roster_date=f"{YEAR}-{MONTH:02d}-04",
                              shift_code=f"{TAG}EARLY", employee_code=code)
        conflict = exc.value.conflicts[0]
        assert conflict["kind"] == "overlap"
        assert conflict["shift_code"] == "NIGHT"
        assert conflict["roster_date"] == f"{YEAR}-{MONTH:02d}-03", (
            "the clash is with the PREVIOUS day's night shift")

    def test_two_different_people_on_the_same_shift_is_not_a_conflict(self):
        """The obvious inverse. A shift has more than one nurse on it, and a
        conflict detector that fires on that is a detector nobody can use."""
        a, b = _mk_employee(), _mk_employee()
        day = f"{YEAR}-{MONTH:02d}-05"
        svc.assign_roster(roster_date=day, shift_code="NIGHT",
                          employee_code=a["employee_code"])
        entry = svc.assign_roster(roster_date=day, shift_code="NIGHT",
                                  employee_code=b["employee_code"])
        assert entry["id"]

    def test_conflicts_are_detected_across_departments(self):
        """The same nurse rostered in two DIFFERENT wards at the same hour is
        the conflict a per-department roster screen cannot see. Detection is by
        person, globally, on purpose."""
        employee = _mk_employee(department=f"{TAG}-ICU")
        day = f"{YEAR}-{MONTH:02d}-06"
        svc.assign_roster(roster_date=day, shift_code="MORNING",
                          employee_code=employee["employee_code"],
                          department=f"{TAG}-ICU")
        with pytest.raises(svc.RosterConflict):
            svc.assign_roster(roster_date=day, shift_code="GENERAL",
                              employee_code=employee["employee_code"],
                              department=f"{TAG}-Ward-B")

    def test_back_to_back_shifts_warn_but_do_not_refuse(self, employee):
        """NIGHT ends 07:00; MORNING starts 07:00 the same morning. They do not
        overlap, so the roster accepts it — and warns about the rest gap,
        because a ward that is genuinely one nurse short needs a roster it can
        still write. A refusal here just moves the shift onto paper, where Orb
        cannot see it."""
        code = employee["employee_code"]
        svc.assign_roster(roster_date=f"{YEAR}-{MONTH:02d}-08", shift_code="NIGHT",
                          employee_code=code)
        entry = svc.assign_roster(roster_date=f"{YEAR}-{MONTH:02d}-09",
                                  shift_code="MORNING", employee_code=code)
        assert entry["id"]
        kinds = {w["kind"] for w in entry["warnings"]}
        assert "short_rest" in kinds
        assert entry["warnings"][0]["rest_minutes"] == 0

    def test_short_rest_can_be_refused_when_the_caller_asks(self, employee):
        code = employee["employee_code"]
        svc.assign_roster(roster_date=f"{YEAR}-{MONTH:02d}-11", shift_code="NIGHT",
                          employee_code=code)
        with pytest.raises(svc.RosterConflict):
            svc.assign_roster(roster_date=f"{YEAR}-{MONTH:02d}-12",
                              shift_code="MORNING", employee_code=code,
                              allow_short_rest=False)

    def test_cancelling_frees_the_slot(self, employee):
        code = employee["employee_code"]
        day = f"{YEAR}-{MONTH:02d}-13"
        entry = svc.assign_roster(roster_date=day, shift_code="EVENING",
                                  employee_code=code)
        svc.cancel_roster_entry(entry["id"], actor="tester")
        again = svc.assign_roster(roster_date=day, shift_code="EVENING",
                                  employee_code=code)
        assert again["id"] != entry["id"]

    def test_find_conflicts_reports_a_roster_broken_by_a_shift_edit(self):
        """The realistic way a conflict gets INTO a roster: the assignments were
        legal, then somebody changed a shift's hours underneath them. Nothing
        re-validates on that edit, so the query has to exist."""
        employee = _mk_employee()
        code = employee["employee_code"]
        late = f"{TAG}LATE{_uid()[:3]}"
        svc.upsert_shift_type(code=late, name="Late (18:00-20:00)",
                              start_minute=18 * 60, duration_minutes=120)
        day = f"{YEAR}-{MONTH:02d}-17"
        svc.assign_roster(roster_date=day, shift_code="MORNING", employee_code=code)
        svc.assign_roster(roster_date=day, shift_code=late, employee_code=code)
        assert not [c for c in svc.find_conflicts(date_from=day, date_to=day)
                    if c["employee_code"] == code], "07-14 and 18-20 do not overlap"

        # Now widen the late shift back over the morning one.
        svc.upsert_shift_type(code=late, name="Late (rescheduled 08:00-20:00)",
                              start_minute=8 * 60, duration_minutes=12 * 60)
        conflicts = [c for c in svc.find_conflicts(date_from=day, date_to=day)
                     if c["employee_code"] == code]
        assert conflicts, "the roster now holds an overlap and must say so"
        assert conflicts[0]["kind"] == "overlap"

    def test_planned_vs_actual_shows_an_unrecorded_assignment_as_unrecorded(self):
        employee = _mk_employee(department=f"{TAG}-RosterView{_uid()[:3]}")
        code = employee["employee_code"]
        day = date(YEAR, MONTH, 18)
        svc.assign_roster(roster_date=day.isoformat(), shift_code="MORNING",
                          employee_code=code, department=employee["department"])
        board = svc.get_roster(date_from=day.isoformat(), date_to=day.isoformat(),
                               department=employee["department"])
        slot = board["days"][0]["shifts"][0]
        assert slot["planned_count"] == 1
        assert slot["unrecorded_count"] == 1
        assert slot["present_count"] == 0
        assert slot["assignments"][0]["actual_status"] == "unrecorded"

        _present(code, day, minutes=480, shift="MORNING")
        board = svc.get_roster(date_from=day.isoformat(), date_to=day.isoformat(),
                               department=employee["department"])
        slot = board["days"][0]["shifts"][0]
        assert slot["present_count"] == 1
        assert slot["unrecorded_count"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  4. THE PAYROLL EXPORT — the file contract
# ═════════════════════════════════════════════════════════════════════════════

#: The columns, EXACTLY, in order. Written out literally rather than imported
#: from the service so that a change to the service's tuple fails HERE, where
#: the consequence (a bureau's importer breaking) is written down — a test that
#: asserts a constant against itself asserts nothing.
EXPECTED_COLUMNS = (
    "employee_code", "employee_name", "department", "designation",
    "employment_type", "payroll_bureau_ref",
    "period_start", "period_end", "days_in_period", "days_working",
    "days_present", "days_absent", "days_leave_paid", "days_leave_unpaid",
    "days_leave_total", "days_weekly_off", "days_holiday", "days_unrecorded",
    "days_absent_or_unpaid_leave", "attendance_complete",
    "worked_minutes", "worked_hours", "overtime_minutes", "overtime_hours",
    "late_marks", "late_minutes",
    "night_shift_count", "evening_shift_count", "holiday_shift_count",
    "weekly_off_worked_count",
    "pf_employee_inr", "pf_employer_inr", "esi_employee_inr", "esi_employer_inr",
    "professional_tax_inr", "tds_inr",
    "statutory_computed_by",
)


class TestPayrollExport:

    def test_the_columns_are_exactly_this_in_exactly_this_order(self):
        assert tuple(svc.PAYROLL_EXPORT_COLUMNS) == EXPECTED_COLUMNS

    def test_the_csv_header_matches_the_columns(self):
        export = svc.build_payroll_export(year=YEAR, month=MONTH,
                                          department=f"{TAG}-nobody-here")
        text = svc.render_payroll_csv(export)
        header = next(csv.reader(io.StringIO(text)))
        assert tuple(header) == EXPECTED_COLUMNS

    def test_statutory_columns_are_empty_on_every_row(self):
        """PF, ESI, PT and TDS ship EMPTY, and every row says who computes them.
        This is the module's whole reason for existing in this shape."""
        department = f"{TAG}-Statutory{_uid()[:3]}"
        for _ in range(3):
            employee = _mk_employee(department=department)
            _present(employee["employee_code"], date(YEAR, MONTH, 2), minutes=500)

        export = svc.build_payroll_export(year=YEAR, month=MONTH, department=department)
        assert len(export["rows"]) == 3
        rows = list(csv.DictReader(io.StringIO(svc.render_payroll_csv(export))))
        assert len(rows) == 3
        for row in rows:
            for column in ("pf_employee_inr", "pf_employer_inr", "esi_employee_inr",
                           "esi_employer_inr", "professional_tax_inr", "tds_inr"):
                assert row[column] == "", f"{column} must leave Orb empty"
            assert row["statutory_computed_by"] == "payroll_provider"
        assert "payroll provider" in export["statutory_note"].lower()

    def test_the_export_carries_the_countable_facts(self):
        department = f"{TAG}-Facts{_uid()[:3]}"
        employee = _mk_employee(department=department,
                                payroll_bureau_ref="BUREAU-9911")
        code = employee["employee_code"]
        _present(code, date(YEAR, MONTH, 2), minutes=540, shift="NIGHT")   # +60 OT
        _present(code, date(YEAR, MONTH, 3), minutes=480, shift="EVENING")
        svc.record_attendance(employee_code=code,
                              attendance_date=f"{YEAR}-{MONTH:02d}-04",
                              status="leave", leave_type_code="LWP")

        export = svc.build_payroll_export(year=YEAR, month=MONTH, department=department)
        row = next(r for r in export["rows"] if r["employee_code"] == code)

        assert row["payroll_bureau_ref"] == "BUREAU-9911"
        assert row["days_present"] == "2"
        assert row["days_leave_unpaid"] == "1"
        assert row["days_leave_paid"] == "0"
        assert row["days_absent_or_unpaid_leave"] == "1"
        assert row["overtime_minutes"] == "60"
        assert row["overtime_hours"] == "1.00"
        assert row["worked_minutes"] == "1020"
        assert row["worked_hours"] == "17.00"
        assert row["night_shift_count"] == "1"
        assert row["evening_shift_count"] == "1"

    def test_export_marks_an_incomplete_row(self):
        """A month with gaps ships with the gap counted, the row flagged, and a
        warning on the response. It is NOT blocked — a bureau that is told
        exactly which days are missing can act; a hospital that cannot run
        payroll at all will go back to the paper muster."""
        department = f"{TAG}-Incomplete{_uid()[:3]}"
        employee = _mk_employee(department=department)
        _present(employee["employee_code"], date(YEAR, MONTH, 2), minutes=480)

        export = svc.build_payroll_export(year=YEAR, month=MONTH, department=department)
        row = export["rows"][0]
        assert row["attendance_complete"] == "no"
        assert int(row["days_unrecorded"]) == 25       # 26 working days − 1 recorded
        assert export["incomplete_employee_count"] == 1
        assert any("unrecorded" in w for w in export["warnings"])

        complete_row_days = (int(row["days_present"]) + int(row["days_absent"])
                             + int(row["days_leave_total"]) + int(row["days_unrecorded"]))
        assert complete_row_days == int(row["days_working"])

    def test_a_run_records_the_checksum_of_what_was_sent(self):
        department = f"{TAG}-Run{_uid()[:3]}"
        _mk_employee(department=department)
        export = svc.build_payroll_export(year=YEAR, month=MONTH, department=department)
        text = svc.render_payroll_csv(export)
        run = svc.record_export_run(export, text, generated_by="tester")

        import hashlib
        assert run["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert run["column_signature"] == ",".join(EXPECTED_COLUMNS)
        assert run["row_count"] == len(export["rows"])
        assert run["file_name"].endswith(".csv")

    def test_the_csv_uses_rfc4180_line_endings(self):
        """The receiving end is a payroll bureau's Excel macro more often than
        it is a parser."""
        export = svc.build_payroll_export(year=YEAR, month=MONTH,
                                          department=f"{TAG}-nobody")
        assert svc.render_payroll_csv(export).endswith("\r\n")

    def test_a_comma_in_a_name_does_not_shift_the_columns(self):
        department = f"{TAG}-Comma{_uid()[:3]}"
        _mk_employee(department=department, full_name="Rao, Anita (Sr.)",
                     designation='Nurse "In-charge"')
        export = svc.build_payroll_export(year=YEAR, month=MONTH, department=department)
        rows = list(csv.DictReader(io.StringIO(svc.render_payroll_csv(export))))
        assert rows[0]["employee_name"] == "Rao, Anita (Sr.)"
        assert rows[0]["designation"] == 'Nurse "In-charge"'
        assert rows[0]["statutory_computed_by"] == "payroll_provider"


# ═════════════════════════════════════════════════════════════════════════════
#  5. THE API SURFACE
# ═════════════════════════════════════════════════════════════════════════════

class TestApi:

    def test_meta_states_the_boundary_in_words(self, admin_headers):
        resp = client.get("/api/workforce/meta", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["statutory_computed_by"] == "payroll_provider"
        assert tuple(body["payroll_export_columns"]) == EXPECTED_COLUMNS
        message = body["boundary"]["message"].lower()
        assert "payroll provider" in message
        assert "pf" in message and "esi" in message
        assert body["policy"]["overtime_daily_minutes"] == 480

    def test_a_conflict_comes_back_as_409_with_the_clash_named(self, admin_headers):
        employee = _mk_employee()
        day = f"{YEAR}-{MONTH:02d}-20"
        first = client.post("/api/workforce/roster", headers=admin_headers, json={
            "roster_date": day, "shift_code": "MORNING",
            "employee_code": employee["employee_code"]})
        assert first.status_code == 200, first.text
        clash = client.post("/api/workforce/roster", headers=admin_headers, json={
            "roster_date": day, "shift_code": "GENERAL",
            "employee_code": employee["employee_code"]})
        assert clash.status_code == 409
        detail = clash.json()["detail"]
        assert detail["error"] == "roster_conflict"
        assert detail["conflicts"][0]["shift_code"] == "MORNING"

    def test_attendance_round_trips_through_the_api(self, admin_headers):
        employee = _mk_employee()
        day = date(YEAR, MONTH, 22)
        resp = client.post("/api/workforce/attendance", headers=admin_headers, json={
            "employee_code": employee["employee_code"],
            "attendance_date": day.isoformat(),
            "status": "present", "shift_code": "NIGHT",
            "first_in_at": _utc(day, 15, 30),
            "last_out_at": _utc(day, 1, 30, plus_days=1),
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["worked_minutes"] == 600
        assert resp.json()["overtime_minutes"] == 120

        summary = client.get("/api/workforce/attendance/summary", headers=admin_headers,
                             params={"year": YEAR, "month": MONTH,
                                     "employee_code": employee["employee_code"]})
        assert summary.status_code == 200
        row = summary.json()["employees"][0]
        assert row["days_present"] == 1
        assert row["identity"]["holds"] is True

    def test_the_csv_download_is_a_csv(self, admin_headers):
        department = f"{TAG}-Download{_uid()[:3]}"
        _mk_employee(department=department)
        resp = client.get("/api/workforce/payroll-export.csv", headers=admin_headers,
                          params={"year": YEAR, "month": MONTH,
                                  "department": department})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]
        assert resp.text.splitlines()[0] == ",".join(EXPECTED_COLUMNS)

    def test_generating_the_file_records_a_run_and_previewing_does_not(self, admin_headers):
        department = f"{TAG}-RunApi{_uid()[:3]}"
        _mk_employee(department=department)
        before = len(client.get("/api/workforce/payroll-export/runs",
                                headers=admin_headers, params={"limit": 200}
                                ).json()["runs"])

        preview = client.get("/api/workforce/payroll-export", headers=admin_headers,
                             params={"year": YEAR, "month": MONTH,
                                     "department": department})
        assert preview.status_code == 200
        mid = len(client.get("/api/workforce/payroll-export/runs", headers=admin_headers,
                             params={"limit": 200}).json()["runs"])
        assert mid == before, "a preview is not a send"

        generated = client.post("/api/workforce/payroll-export", headers=admin_headers,
                                json={"year": YEAR, "month": MONTH,
                                      "department": department})
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body["csv"].splitlines()[0] == ",".join(EXPECTED_COLUMNS)
        assert body["sha256"] and len(body["sha256"]) == 64
        after = len(client.get("/api/workforce/payroll-export/runs", headers=admin_headers,
                               params={"limit": 200}).json()["runs"])
        assert after == before + 1

    def test_a_clinical_role_cannot_reach_the_module(self, nurse_headers):
        """PERMISSION DEBT, PINNED. Every route needs `admin.overview`, so a
        nurse gets 403 — including the roster read, which she arguably should
        have. This test exists so that when a `workforce.*` permission lands,
        the person adding it sees exactly which doors they are opening."""
        for method, path, kwargs in [
            ("get", "/api/workforce/meta", {}),
            ("get", "/api/workforce/roster",
             {"params": {"date_from": f"{YEAR}-{MONTH:02d}-01",
                         "date_to": f"{YEAR}-{MONTH:02d}-02"}}),
            ("post", "/api/workforce/attendance",
             {"json": {"employee_code": "X", "attendance_date": "2026-06-01",
                       "status": "absent"}}),
            ("get", "/api/workforce/payroll-export",
             {"params": {"year": YEAR, "month": MONTH}}),
        ]:
            resp = getattr(client, method)(path, headers=nurse_headers, **kwargs)
            assert resp.status_code == 403, f"{method.upper()} {path} -> {resp.status_code}"

    def test_an_unauthenticated_caller_gets_nothing(self):
        resp = client.get("/api/workforce/meta")
        assert resp.status_code in (401, 403)

    def test_a_switched_off_module_404s_with_a_reason(self, admin_headers, monkeypatch):
        """A site that runs its own HRMS. The tab disappears from the shell via
        /api/health; a stale client that asks anyway gets a sentence."""
        monkeypatch.setattr(config, "WORKFORCE_ENABLED", False)
        resp = client.get("/api/workforce/meta", headers=admin_headers)
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["error"] == "module_disabled"
        assert detail["module"] == "workforce"
        assert "ORB_WORKFORCE=0" in detail["message"]

    def test_the_module_is_on_the_health_switchboard(self):
        assert "workforce" in config.enabled_modules()
        assert config.HOSPITAL_MODULE_ENV["workforce"] == "ORB_WORKFORCE"
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert "workforce" in resp.json()["modules"]


# ═════════════════════════════════════════════════════════════════════════════
#  6. THE CLINICAL FIREBREAK
# ═════════════════════════════════════════════════════════════════════════════

class TestNotAClinicalPath:
    """This module must not be able to touch, or block, patient care."""

    def test_the_service_never_writes_a_clinical_table(self):
        source = (__import__("pathlib").Path(svc.__file__)).read_text()
        for table in ("medications", "medication_administrations", "vitals",
                      "alerts", "orders", "admissions", "shadow_events",
                      "audit_logs", "patients"):
            for verb in ("INSERT INTO", "UPDATE", "DELETE FROM"):
                assert f"{verb} {table}" not in source, (
                    f"workforce_service must never {verb} {table}")

    def test_no_route_takes_a_patient_id(self):
        """If one ever does it must call enforce_patient_scope
        (tests/test_phi_read_scope_gaps.py). Today there is no patient anywhere
        in this module, and this test is what keeps that true."""
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/workforce"):
                continue
            assert "patient_id" not in path
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                assert "patient_id" not in getattr(endpoint, "__annotations__", {})
