"""Orb — WORKFORCE: duty roster, staff attendance, overtime, payroll EXPORT.

════════════════════════════════════════════════════════════════════════════════
 THE BOUNDARY. READ THIS BEFORE ADDING A FUNCTION TO THIS FILE.
════════════════════════════════════════════════════════════════════════════════

    ORB OWNS THE ROSTER AND THE ATTENDANCE.
    THE PAYROLL PROVIDER OWNS THE MONEY.

There is no salary in this module. No PF, no ESI, no Professional Tax, no TDS,
no gross, no net, no rate of any kind. Not "not yet" — not ever, and the schema
(migration `workforce_attendance`) has no column to put one in.

WHY THE LINE IS EXACTLY HERE, and why it is the defensible split rather than a
shortcut:

  * The statutory half is a LIABILITY, not a feature. Provident Fund, ESI,
    Professional Tax and TDS are computed from rules that change with every
    Union and State budget, differ by state, and carry interest and penalty when
    they are wrong. A hospital already pays a payroll bureau or a chartered
    accountant to track them and to be accountable for them. Orb computing them
    slightly wrong does not produce a bug report; it produces a demand notice
    addressed to the hospital.
  * The roster half is a thing ONLY ORB CAN KNOW. Who was actually on the ward
    at 03:00, who swapped, who stayed four hours past handover because the
    relief did not arrive — that lives in the system the ward uses, and today it
    lives on a paper sheet in the nursing office and is retyped once a month
    from memory. That retyping is where a hospital's attendance data actually
    dies, and it is the part worth building.

So this module converts what the ward knows into what the bureau needs, and
stops. `build_payroll_export` emits the statutory columns EMPTY, every row
carrying `statutory_computed_by=payroll_provider`, so the file is
self-describing on the bureau's desk and nobody has to remember this decision.

════════════════════════════════════════════════════════════════════════════════
 THE ONE INVARIANT EVERYTHING ELSE RESTS ON
════════════════════════════════════════════════════════════════════════════════

Every day in a period lands in EXACTLY ONE bucket, and the buckets sum:

    present + absent + leave + unrecorded + weekly_off + holiday == days_in_period
    present + absent + leave + unrecorded                       == working_days

`unrecorded` is the bucket that makes this module honest. A day with no
attendance row is NOT absent and NOT present — it is a day nobody wrote down.
Folding it into "absent" docks a nurse's pay for the ward in-charge's
paperwork; folding it into "present" pays for a shift nobody worked. Both are
silent, and both are the kind of defect this project has shipped before. So the
third bucket exists, it is carried all the way into the CSV as
`days_unrecorded`, and `attendance_complete` says `no` on that row.

════════════════════════════════════════════════════════════════════════════════
 CONVENTIONS
════════════════════════════════════════════════════════════════════════════════

  * PURE SYNC. Every public function opens its own `db_session()`. Async callers
    go through `database.run_db`; the route module uses plain `def` handlers so
    FastAPI threadpools them. Blocking DB work inside `async def` is banned and
    tests/test_event_loop_discipline.py enforces it.
  * DURATIONS ARE INTEGER MINUTES. There is no float anywhere in this file. The
    decimal hours in the export are rendered from integer minutes by integer
    arithmetic, and the exact minutes ship alongside them so nothing is lost to
    rounding.
  * TIMES ARE FULL UTC INSTANTS. A night shift's in and out fall on two
    different calendar dates; a duration computed from two times-of-day has to
    guess which, and that guess is a night nurse's pay. `record_attendance`
    takes instants and refuses a span longer than
    `config.WORKFORCE_MAX_SPAN_MINUTES`.
  * A DATE IS THE HOSPITAL'S DATE. Day keys come from `services.hospital_time`,
    never from `datetime.utcnow().date()` — in IST that is yesterday until
    05:30, which is the middle of the night shift.
  * NO FOREIGN KEYS on these tables. Referential integrity is this module's job;
    a dangling employee code yields "unknown", never a failed write.
  * NOTHING HERE IS A CLINICAL PATH. No function in this module reads or writes
    medications, vitals, alerts, orders, admissions or shadow evidence, and no
    function here may block one. A roster gap is a warning on a screen. A ward
    that is short-staffed is a fact to display, never a reason to refuse a nurse
    an action.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import config
from database import db_session

# sqlcipher3 raises its OWN exception hierarchy: sqlcipher3.dbapi2.IntegrityError
# is NOT a subclass of sqlite3.IntegrityError, so `except sqlite3.IntegrityError`
# is DEAD CODE whenever ORB_DB_ENCRYPTION_KEY is set — i.e. on every shipping
# appliance. Guarded by tests/test_db_exception_aliases.py.
from db_backend import DBIntegrityError

from services.hospital_time import hospital_timezone, hospital_today

logger = logging.getLogger("orb.workforce")

__all__ = [
    "WorkforceError",
    "RosterConflict",
    # policy
    "MINUTES_PER_DAY",
    "DAY_STATUSES",
    "PAYROLL_EXPORT_COLUMNS",
    "STATUTORY_COLUMNS",
    "STATUTORY_COMPUTED_BY",
    "policy",
    # masters
    "list_shift_types",
    "upsert_shift_type",
    "list_leave_types",
    "list_employees",
    "get_employee",
    "upsert_employee",
    "list_holidays",
    "upsert_holiday",
    "delete_holiday",
    # roster
    "assign_roster",
    "cancel_roster_entry",
    "get_roster",
    "find_conflicts",
    # attendance
    "record_attendance",
    "list_attendance",
    "monthly_summary",
    "period_summaries",
    # export
    "build_payroll_export",
    "render_payroll_csv",
    "record_export_run",
    "list_export_runs",
    "period_bounds",
]


MINUTES_PER_DAY = 1440

#: The five ways a calendar day can be RECORDED. A sixth state — `unrecorded` —
#: exists only as the absence of a row and is never stored; see the module
#: docstring for why that distinction is the point of this module.
DAY_STATUSES = ("present", "absent", "leave", "weekly_off", "holiday")

ROSTER_STATUSES = ("planned", "published", "cancelled")
ATTENDANCE_SOURCES = ("manual", "biometric_import", "roster_default")
EMPLOYMENT_TYPES = ("permanent", "probation", "contract", "consultant", "trainee", "outsourced")
ALLOWANCE_CODES = ("night", "evening")

#: The value written into every row's `statutory_computed_by` column. It is a
#: constant rather than a config value on purpose: it is not a setting, it is a
#: statement about who is accountable, and a site must not be able to change it
#: to "orb" from an environment variable.
STATUTORY_COMPUTED_BY = "payroll_provider"

#: The statutory deduction columns. Present in the file so the bureau's importer
#: has a slot to fill, ALWAYS EMPTY on the way out of Orb.
STATUTORY_COLUMNS = (
    "pf_employee_inr",
    "pf_employer_inr",
    "esi_employee_inr",
    "esi_employer_inr",
    "professional_tax_inr",
    "tds_inr",
)

#: THE FILE CONTRACT. A payroll bureau's importer is a column-order-sensitive
#: spreadsheet macro more often than it is software, so this order is part of
#: what Orb promises; `payroll_export_runs.column_signature` records the exact
#: header that was sent with each file, and tests/test_workforce.py pins this
#: tuple literally. Changing it is a deliberate act with a hospital on the other
#: end of it, not a refactor.
PAYROLL_EXPORT_COLUMNS = (
    # Who
    "employee_code",
    "employee_name",
    "department",
    "designation",
    "employment_type",
    "payroll_bureau_ref",
    # Which period
    "period_start",
    "period_end",
    "days_in_period",
    "days_working",
    # Days, in mutually exclusive buckets that sum to days_in_period
    "days_present",
    "days_absent",
    "days_leave_paid",
    "days_leave_unpaid",
    "days_leave_total",
    "days_weekly_off",
    "days_holiday",
    "days_unrecorded",
    # The bureau's loss-of-pay working (a COUNT of days, not a decision)
    "days_absent_or_unpaid_leave",
    "attendance_complete",
    # Time. Exact minutes AND decimal hours: the minutes are the record, the
    # hours are what a payroll sheet wants, and shipping only the rounded form
    # would lose a minute a month per employee that nobody could reconstruct.
    "worked_minutes",
    "worked_hours",
    "overtime_minutes",
    "overtime_hours",
    "late_marks",
    "late_minutes",
    # Allowance-bearing shift COUNTS. Orb counts the shifts; the site's policy
    # and the bureau decide what each is worth.
    "night_shift_count",
    "evening_shift_count",
    "holiday_shift_count",
    "weekly_off_worked_count",
    # Statutory — always empty, see STATUTORY_COLUMNS.
    *STATUTORY_COLUMNS,
    "statutory_computed_by",
)


class WorkforceError(Exception):
    """A refusal this module can explain. `code` is machine-readable so a client
    can branch on it without matching English."""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


class RosterConflict(WorkforceError):
    """The same person cannot be in two places at once. Carries the conflicting
    assignments so the UI can name them rather than saying "conflict"."""

    def __init__(self, message: str, conflicts: list[dict]):
        super().__init__(message, code="roster_conflict")
        self.conflicts = conflicts


# ═════════════════════════════════════════════════════════════════════════════
#  Policy
# ═════════════════════════════════════════════════════════════════════════════


def policy() -> dict[str, Any]:
    """The numbers this module judges a day against, and where they came from.

    Read from `config` at CALL time, not bound at import, so a test (or an ops
    change) that patches the constant is honoured by the next request rather
    than by the next restart."""
    return {
        "overtime_daily_minutes": config.WORKFORCE_OT_DAILY_MINUTES,
        "late_grace_minutes": config.WORKFORCE_LATE_GRACE_MINUTES,
        "max_span_minutes": config.WORKFORCE_MAX_SPAN_MINUTES,
        "overtime_basis": (
            "Minutes worked beyond the daily threshold, per day. A site sets the "
            "threshold (ORB_WORKFORCE_OT_DAILY_MINUTES); Orb counts the minutes "
            "and never prices them."
        ),
        "statutory_boundary": (
            "Orb records the roster and attendance. PF, ESI, Professional Tax "
            "and TDS are computed by the hospital's payroll provider, not by "
            "Orb: those rules change with every budget and differ by state, and "
            "getting them wrong is a statutory liability rather than a bug."
        ),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════


def _row(r) -> dict:
    return dict(r) if r is not None else {}


def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def _as_date(value: Any, field: str = "date") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    raise WorkforceError(
        f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}", code="bad_date"
    )


def _as_instant(value: Any, field: str) -> datetime:
    """Parse a timestamp into an AWARE UTC datetime.

    A naive value is read as UTC and NOT as hospital-local: everything Orb
    persists is UTC, so a naive string arriving from a client that forgot its
    offset is far more likely to be a UTC timestamp than a local one. The route
    layer sends offsets; this is the last line of defence, not the contract."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise WorkforceError(
                f"{field} must be an ISO timestamp, got {value!r}", code="bad_timestamp"
            ) from None
    else:
        raise WorkforceError(
            f"{field} must be an ISO timestamp, got {value!r}", code="bad_timestamp"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require(value: str | None, field: str) -> str:
    text = _clean(value)
    if not text:
        raise WorkforceError(f"{field} is required", code="missing_field")
    return text


def minutes_to_hours(minutes: int) -> str:
    """Integer minutes → a 2-decimal hours string, by INTEGER arithmetic.

    `450 → "7.50"`. No float touches a payroll figure: 7.5 is exact in binary
    but 8.1 is not, and a column of values that each end in .09999 is a column
    somebody has to explain to an accountant. Rounds half-up on the hundredth."""
    if minutes < 0:
        raise WorkforceError("minutes cannot be negative", code="bad_minutes")
    hundredths = (minutes * 100 + 30) // 60
    return f"{hundredths // 100}.{hundredths % 100:02d}"


def period_bounds(year: int, month: int) -> tuple[date, date]:
    """The first and last calendar day of a month. Whole months only — a payroll
    period that is not a month is not a thing an Indian bureau asks for, and
    supporting an arbitrary window would invite a half-month export that quietly
    disagrees with the one the bureau ran."""
    if not (1 <= int(month) <= 12):
        raise WorkforceError(f"month must be 1-12, got {month}", code="bad_period")
    if not (2000 <= int(year) <= 2100):
        raise WorkforceError(f"year out of range: {year}", code="bad_period")
    year, month = int(year), int(month)
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _daterange(start: date, end: date) -> Iterable[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


# ═════════════════════════════════════════════════════════════════════════════
#  Shift types
# ═════════════════════════════════════════════════════════════════════════════


def _shift_view(row: dict) -> dict:
    start = int(row["start_minute"])
    duration = int(row["duration_minutes"])
    end = start + duration
    return {
        "code": row["code"],
        "name": row["name"],
        "start_minute": start,
        "duration_minutes": duration,
        "start_time": f"{start // 60:02d}:{start % 60:02d}",
        "end_time": f"{(end // 60) % 24:02d}:{end % 60:02d}",
        # Derived, never stored: a stored flag and a stored duration can
        # disagree, and then a night shift stops conflicting with anything.
        "crosses_midnight": end > MINUTES_PER_DAY,
        "allowance_code": row["allowance_code"],
        "is_active": bool(row["is_active"]),
    }


def list_shift_types(*, include_inactive: bool = False) -> list[dict]:
    with db_session() as db:
        sql = "SELECT * FROM duty_shift_types"
        if not include_inactive:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY start_minute, code"
        return [_shift_view(dict(r)) for r in db.execute(sql).fetchall()]


def _shift_map(db) -> dict[str, dict]:
    return {
        r["code"]: _shift_view(dict(r))
        for r in db.execute("SELECT * FROM duty_shift_types").fetchall()
    }


def upsert_shift_type(
    *,
    code: str,
    name: str,
    start_minute: int,
    duration_minutes: int,
    allowance_code: str | None = None,
    is_active: bool = True,
) -> dict:
    code = _require(code, "code").upper()
    name = _require(name, "name")
    start_minute, duration_minutes = int(start_minute), int(duration_minutes)
    if not 0 <= start_minute < MINUTES_PER_DAY:
        raise WorkforceError("start_minute must be within a day", code="bad_shift")
    if not 0 < duration_minutes <= MINUTES_PER_DAY:
        raise WorkforceError("duration_minutes must be 1..1440", code="bad_shift")
    allowance_code = _clean(allowance_code)
    if allowance_code and allowance_code not in ALLOWANCE_CODES:
        raise WorkforceError(
            f"allowance_code must be one of {', '.join(ALLOWANCE_CODES)}", code="bad_shift"
        )
    with db_session() as db:
        db.execute(
            """INSERT INTO duty_shift_types
                 (code, name, start_minute, duration_minutes, allowance_code, is_active)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, start_minute=excluded.start_minute,
                 duration_minutes=excluded.duration_minutes,
                 allowance_code=excluded.allowance_code,
                 is_active=excluded.is_active,
                 updated_at=CURRENT_TIMESTAMP""",
            (code, name, start_minute, duration_minutes, allowance_code, 1 if is_active else 0),
        )
        row = db.execute("SELECT * FROM duty_shift_types WHERE code = ?", (code,)).fetchone()
    return _shift_view(dict(row))


def list_leave_types(*, include_inactive: bool = False) -> list[dict]:
    with db_session() as db:
        sql = "SELECT code, name, is_paid, sort_order, is_active FROM leave_types"
        if not include_inactive:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY sort_order, code"
        return [
            {
                "code": r["code"],
                "name": r["name"],
                "is_paid": bool(r["is_paid"]),
                "is_active": bool(r["is_active"]),
            }
            for r in db.execute(sql).fetchall()
        ]


# ═════════════════════════════════════════════════════════════════════════════
#  Employees
# ═════════════════════════════════════════════════════════════════════════════


def _employee_view(row: dict) -> dict:
    return {
        "employee_code": row["employee_code"],
        "user_id": row["user_id"],
        "full_name": row["full_name"],
        "department": row["department"],
        "designation": row["designation"],
        "employment_type": row["employment_type"],
        "date_of_joining": row["date_of_joining"],
        "date_of_exit": row["date_of_exit"],
        "weekly_off_day": row["weekly_off_day"],
        # NULL weekly_off_day means the day off ROTATES with the roster. Said in
        # words as well as in the null, because a blank cell in a table reads as
        # "missing data" and this one is a fact about the job.
        "weekly_off_label": (
            "Rotational"
            if row["weekly_off_day"] is None
            else ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[
                int(row["weekly_off_day"])
            ]
        ),
        "payroll_bureau_ref": row["payroll_bureau_ref"],
        "is_active": bool(row["is_active"]),
        "notes": row["notes"],
    }


def list_employees(
    *, department: str | None = None, include_inactive: bool = False, search: str | None = None
) -> list[dict]:
    clauses, params = [], []
    if not include_inactive:
        clauses.append("is_active = 1")
    if _clean(department):
        clauses.append("department = ?")
        params.append(_clean(department) or "")
    if _clean(search):
        clauses.append("(full_name LIKE ? OR employee_code LIKE ?)")
        like = f"%{_clean(search)}%"
        params.extend([like, like])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db_session() as db:
        rows = db.execute(
            f"SELECT * FROM staff_employment{where} ORDER BY department, full_name",
            params,
        ).fetchall()
    return [_employee_view(dict(r)) for r in rows]


def get_employee(employee_code: str) -> dict:
    code = _require(employee_code, "employee_code")
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM staff_employment WHERE employee_code = ?", (code,)
        ).fetchone()
    if row is None:
        raise WorkforceError(f"No employee with code {code}", code="not_found")
    return _employee_view(dict(row))


def upsert_employee(
    *,
    employee_code: str,
    full_name: str,
    department: str | None = None,
    designation: str | None = None,
    employment_type: str = "permanent",
    user_id: str | None = None,
    date_of_joining: str | None = None,
    date_of_exit: str | None = None,
    weekly_off_day: int | None = None,
    payroll_bureau_ref: str | None = None,
    is_active: bool = True,
    notes: str | None = None,
    created_by: str | None = None,
    create_only: bool = False,
) -> dict:
    """Create or update one employee, keyed on `employee_code`.

    `create_only` is the Add form: a code already on the register is refused
    (`already_exists`, naming the person) instead of overwriting their record.
    On an update a blank optional field keeps what is on file, except the exit
    date and the weekly day off, where a blank MEANS something (still employed;
    the day off rotates) and so is written as sent.
    """
    code = _require(employee_code, "employee_code").upper()
    name = _require(full_name, "full_name")
    if employment_type not in EMPLOYMENT_TYPES:
        raise WorkforceError(
            f"employment_type must be one of {', '.join(EMPLOYMENT_TYPES)}",
            code="bad_employment_type",
        )
    doj = _as_date(date_of_joining, "date_of_joining").isoformat() if date_of_joining else None
    doe = _as_date(date_of_exit, "date_of_exit").isoformat() if date_of_exit else None
    if doj and doe and doe < doj:
        raise WorkforceError("date_of_exit is before date_of_joining", code="bad_dates")
    if weekly_off_day is not None:
        weekly_off_day = int(weekly_off_day)
        if not 0 <= weekly_off_day <= 6:
            raise WorkforceError(
                "weekly_off_day must be 0 (Mon) .. 6 (Sun), or null for a rotational day off",
                code="bad_weekly_off",
            )
    with db_session() as db:
        if create_only:
            existing = db.execute(
                "SELECT full_name, department FROM staff_employment WHERE employee_code = ?",
                (code,),
            ).fetchone()
            if existing:
                dept = f" ({existing['department']})" if existing["department"] else ""
                raise WorkforceError(
                    f"{code} already exists as {existing['full_name']}{dept}. Nothing "
                    "was changed. Use a different code, or edit that person's record.",
                    code="already_exists",
                )
        db.execute(
            """INSERT INTO staff_employment
                 (employee_code, user_id, full_name, department, designation,
                  employment_type, date_of_joining, date_of_exit, weekly_off_day,
                  payroll_bureau_ref, is_active, notes, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(employee_code) DO UPDATE SET
                 user_id=COALESCE(excluded.user_id, user_id), full_name=excluded.full_name,
                 department=COALESCE(excluded.department, department),
                 designation=COALESCE(excluded.designation, designation),
                 employment_type=excluded.employment_type,
                 date_of_joining=COALESCE(excluded.date_of_joining, date_of_joining),
                 date_of_exit=excluded.date_of_exit,
                 weekly_off_day=excluded.weekly_off_day,
                 payroll_bureau_ref=COALESCE(excluded.payroll_bureau_ref, payroll_bureau_ref),
                 is_active=excluded.is_active, notes=COALESCE(excluded.notes, notes),
                 updated_at=CURRENT_TIMESTAMP""",
            (
                code,
                _clean(user_id),
                name,
                _clean(department),
                _clean(designation),
                employment_type,
                doj,
                doe,
                weekly_off_day,
                _clean(payroll_bureau_ref),
                1 if is_active else 0,
                _clean(notes),
                _clean(created_by),
            ),
        )
        row = db.execute(
            "SELECT * FROM staff_employment WHERE employee_code = ?", (code,)
        ).fetchone()
    return _employee_view(dict(row))


# ═════════════════════════════════════════════════════════════════════════════
#  Holidays
# ═════════════════════════════════════════════════════════════════════════════


def list_holidays(*, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    clauses, params = [], []
    if date_from:
        clauses.append("holiday_date >= ?")
        params.append(_as_date(date_from, "date_from").isoformat())
    if date_to:
        clauses.append("holiday_date <= ?")
        params.append(_as_date(date_to, "date_to").isoformat())
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db_session() as db:
        rows = db.execute(
            f"SELECT holiday_date, name, is_restricted FROM hospital_holidays{where} "
            "ORDER BY holiday_date",
            params,
        ).fetchall()
    return [
        {
            "holiday_date": r["holiday_date"],
            "name": r["name"],
            "is_restricted": bool(r["is_restricted"]),
        }
        for r in rows
    ]


def upsert_holiday(
    *, holiday_date: str, name: str, is_restricted: bool = False, created_by: str | None = None
) -> dict:
    day = _as_date(holiday_date, "holiday_date")
    label = _require(name, "name")
    with db_session() as db:
        db.execute(
            """INSERT INTO hospital_holidays (holiday_date, name, is_restricted, created_by)
               VALUES (?,?,?,?)
               ON CONFLICT(holiday_date) DO UPDATE SET
                 name=excluded.name, is_restricted=excluded.is_restricted""",
            (day.isoformat(), label, 1 if is_restricted else 0, _clean(created_by)),
        )
    return {"holiday_date": day.isoformat(), "name": label, "is_restricted": bool(is_restricted)}


def delete_holiday(holiday_date: str) -> bool:
    day = _as_date(holiday_date, "holiday_date")
    with db_session() as db:
        cur = db.execute("DELETE FROM hospital_holidays WHERE holiday_date = ?", (day.isoformat(),))
        return bool(cur.rowcount)


def _holiday_set(db, start: date, end: date) -> set[str]:
    return {
        r["holiday_date"]
        for r in db.execute(
            "SELECT holiday_date FROM hospital_holidays "
            "WHERE holiday_date >= ? AND holiday_date <= ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Duty roster
# ═════════════════════════════════════════════════════════════════════════════
#
#  CONFLICT DETECTION, and why it works in absolute minutes rather than in
#  clock times.
#
#  A shift is an interval [start, start + duration) measured in minutes from
#  midnight of its ROSTER DATE. NIGHT (21:00, 600 min) on the 3rd is therefore
#  [1260, 1860) — it runs past 1440 into the 4th, and that is the whole reason
#  the schema stores a duration instead of an end-of-day clock string. To
#  compare two assignments on different dates, both intervals are shifted onto a
#  common origin by (date − epoch) × 1440. Two assignments conflict iff the
#  intervals OVERLAP.
#
#  What that catches that a same-date comparison does not: a NIGHT on the 3rd
#  (21:00→07:00) against a shift starting 02:00 on the 4th. What it deliberately
#  does NOT call a conflict: back-to-back shifts that merely touch (a NIGHT
#  ending 07:00 and a MORNING starting 07:00) — that is legal, and it is
#  reported separately as a REST warning, because refusing it outright would
#  make the roster unusable in a ward that is genuinely short a nurse.

_EPOCH = date(2000, 1, 1)


def _absolute_window(day: date, shift: dict) -> tuple[int, int]:
    base = (day - _EPOCH).days * MINUTES_PER_DAY
    start = base + shift["start_minute"]
    return start, start + shift["duration_minutes"]


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _rest_minutes(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Gap between two non-overlapping windows, in minutes."""
    return b[0] - a[1] if b[0] >= a[1] else a[0] - b[1]


#: Below this many minutes between the end of one shift and the start of the
#: next, the roster WARNS. It does not refuse: a ward that is one nurse short at
#: 22:00 needs a roster it can still write, and a refusal there would be
#: overridden by writing the shift on paper — which is worse, because then Orb
#: does not know about it. Advisory, and visible.
MIN_REST_MINUTES = 8 * 60


def _conflict_candidates(
    db, employee_code: str, day: date, exclude_id: int | None = None
) -> list[dict]:
    """Assignments that could possibly touch `day` — the day before, the day
    itself and the day after. A shift cannot be longer than 24h (schema CHECK),
    so nothing outside that window can overlap."""
    sql = (
        "SELECT * FROM duty_roster WHERE employee_code = ? AND status <> 'cancelled' "
        "AND roster_date >= ? AND roster_date <= ?"
    )
    params: list[Any] = [
        employee_code,
        (day - timedelta(days=1)).isoformat(),
        (day + timedelta(days=1)).isoformat(),
    ]
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_id)
    return _rows(db.execute(sql, params).fetchall())


def _describe_conflicts(
    db, employee_code: str, day: date, shift_code: str, exclude_id: int | None = None
) -> tuple[list[dict], list[dict]]:
    """(blocking, advisory) for putting `employee_code` on `shift_code` on `day`."""
    shifts = _shift_map(db)
    if shift_code not in shifts:
        raise WorkforceError(f"Unknown shift {shift_code}", code="unknown_shift")
    window = _absolute_window(day, shifts[shift_code])
    blocking: list[dict] = []
    advisory: list[dict] = []
    for other in _conflict_candidates(db, employee_code, day, exclude_id):
        other_shift = shifts.get(other["shift_code"])
        if other_shift is None:
            # A roster row naming a shift that has since been deleted. It cannot
            # be placed on a timeline, so it cannot be proven safe either —
            # surfaced as advisory rather than silently ignored.
            advisory.append(
                {
                    "kind": "unknown_shift",
                    "roster_id": other["id"],
                    "roster_date": other["roster_date"],
                    "shift_code": other["shift_code"],
                    "department": other["department"],
                    "message": (
                        f"Already assigned to shift {other['shift_code']} on "
                        f"{other['roster_date']}, which is no longer a defined "
                        "shift. Orb cannot check it for a clash."
                    ),
                }
            )
            continue
        other_window = _absolute_window(_as_date(other["roster_date"]), other_shift)
        entry = {
            "roster_id": other["id"],
            "roster_date": other["roster_date"],
            "shift_code": other["shift_code"],
            "shift_name": other_shift["name"],
            "department": other["department"],
            "ward": other["ward"],
        }
        if _overlaps(window, other_window):
            blocking.append(
                {
                    **entry,
                    "kind": "overlap",
                    "message": (
                        f"Already on {other_shift['name']} on {other['roster_date']}"
                        + (f" in {other['department']}" if other["department"] else "")
                        + ", the two shifts overlap."
                    ),
                }
            )
            continue
        rest = _rest_minutes(window, other_window)
        if rest < MIN_REST_MINUTES:
            advisory.append(
                {
                    **entry,
                    "kind": "short_rest",
                    "rest_minutes": rest,
                    "message": (
                        f"Only {rest // 60}h {rest % 60:02d}m between this "
                        f"shift and {other_shift['name']} on "
                        f"{other['roster_date']}."
                    ),
                }
            )
        elif _as_date(other["roster_date"]) == day:
            advisory.append(
                {
                    **entry,
                    "kind": "double_shift",
                    "message": (f"Also on {other_shift['name']} the same day."),
                }
            )
    return blocking, advisory


def assign_roster(
    *,
    roster_date: str,
    shift_code: str,
    employee_code: str,
    department: str | None = None,
    ward: str | None = None,
    assigned_role: str | None = None,
    status: str = "planned",
    notes: str | None = None,
    created_by: str | None = None,
    allow_short_rest: bool = True,
) -> dict:
    """Put one person on one shift on one day.

    REFUSES on an overlapping assignment (`RosterConflict`) — nobody is in two
    places at once, and a roster that says otherwise is a roster the ward stops
    reading. Short rest and double shifts come back as `warnings` on the created
    row instead: see MIN_REST_MINUTES."""
    day = _as_date(roster_date, "roster_date")
    code = _require(shift_code, "shift_code").upper()
    employee = _require(employee_code, "employee_code").upper()
    if status not in ROSTER_STATUSES or status == "cancelled":
        raise WorkforceError("status must be 'planned' or 'published'", code="bad_status")
    with db_session() as db:
        emp = db.execute(
            "SELECT * FROM staff_employment WHERE employee_code = ?", (employee,)
        ).fetchone()
        if emp is None:
            raise WorkforceError(f"No employee with code {employee}", code="not_found")
        if not emp["is_active"]:
            raise WorkforceError(
                f"{emp['full_name']} is not an active employee", code="inactive_employee"
            )
        blocking, advisory = _describe_conflicts(db, employee, day, code)
        if blocking:
            raise RosterConflict(
                f"{emp['full_name']} is already rostered on an overlapping shift.", blocking
            )
        if advisory and not allow_short_rest:
            raise RosterConflict(
                f"{emp['full_name']} would have less than "
                f"{MIN_REST_MINUTES // 60}h between shifts.",
                advisory,
            )
        try:
            cur = db.execute(
                """INSERT INTO duty_roster
                     (roster_date, shift_code, employee_code, department, ward,
                      assigned_role, status, notes, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    day.isoformat(),
                    code,
                    employee,
                    _clean(department) or emp["department"],
                    _clean(ward),
                    _clean(assigned_role) or emp["designation"],
                    status,
                    _clean(notes),
                    _clean(created_by),
                ),
            )
        except DBIntegrityError:
            # The partial unique index. Reachable when two ward clerks assign the
            # same slot at the same moment — the overlap check above ran before
            # the other transaction committed.
            raise RosterConflict(
                f"{emp['full_name']} is already on {code} on {day.isoformat()}.",
                [
                    {
                        "kind": "duplicate",
                        "roster_date": day.isoformat(),
                        "shift_code": code,
                        "message": "This exact assignment already exists.",
                    }
                ],
            ) from None
        row = db.execute("SELECT * FROM duty_roster WHERE id = ?", (cur.lastrowid,)).fetchone()
    result = _row(row)
    result["warnings"] = advisory
    return result


def cancel_roster_entry(roster_id: int, *, actor: str | None = None) -> dict:
    with db_session() as db:
        row = db.execute("SELECT * FROM duty_roster WHERE id = ?", (roster_id,)).fetchone()
        if row is None:
            raise WorkforceError(f"No roster entry {roster_id}", code="not_found")
        db.execute(
            "UPDATE duty_roster SET status='cancelled', updated_at=CURRENT_TIMESTAMP, "
            "notes = COALESCE(notes,'') || ? WHERE id = ?",
            (f"\ncancelled by {_clean(actor) or 'unknown'}", roster_id),
        )
        row = db.execute("SELECT * FROM duty_roster WHERE id = ?", (roster_id,)).fetchone()
    return _row(row)


def find_conflicts(*, date_from: str, date_to: str, department: str | None = None) -> list[dict]:
    """Every overlapping pair already ON the roster in a window.

    `assign_roster` refuses to create one, but a roster can still hold conflicts
    that predate this module, that arrived by import, or that appeared when
    somebody edited a shift's hours underneath existing assignments — which is
    the realistic case and the reason this is a QUERY and not just a guard."""
    start, end = _as_date(date_from, "date_from"), _as_date(date_to, "date_to")
    if end < start:
        raise WorkforceError("date_to is before date_from", code="bad_range")
    with db_session() as db:
        shifts = _shift_map(db)
        params: list[Any] = [
            (start - timedelta(days=1)).isoformat(),
            (end + timedelta(days=1)).isoformat(),
        ]
        sql = (
            "SELECT r.*, e.full_name FROM duty_roster r "
            "LEFT JOIN staff_employment e ON e.employee_code = r.employee_code "
            "WHERE r.status <> 'cancelled' AND r.roster_date >= ? AND r.roster_date <= ?"
        )
        if _clean(department):
            sql += " AND r.department = ?"
            params.append(_clean(department) or "")
        rows = _rows(db.execute(sql + " ORDER BY r.employee_code, r.roster_date", params))

    by_employee: dict[str, list[dict]] = {}
    for row in rows:
        by_employee.setdefault(row["employee_code"], []).append(row)

    out: list[dict] = []
    for employee_code, entries in by_employee.items():
        placed = []
        for entry in entries:
            shift = shifts.get(entry["shift_code"])
            if shift is None:
                continue
            placed.append((entry, _absolute_window(_as_date(entry["roster_date"]), shift)))
        for i in range(len(placed)):
            for j in range(i + 1, len(placed)):
                (a, wa), (b, wb) = placed[i], placed[j]
                if not _overlaps(wa, wb):
                    continue
                # Only report a pair that actually touches the requested window;
                # the query deliberately reaches a day either side to catch a
                # night shift, and those neighbours are context, not findings.
                if not any(start <= _as_date(x["roster_date"]) <= end for x in (a, b)):
                    continue
                out.append(
                    {
                        "employee_code": employee_code,
                        "employee_name": a.get("full_name") or "Unknown employee",
                        "kind": "overlap",
                        "first": {
                            "roster_id": a["id"],
                            "roster_date": a["roster_date"],
                            "shift_code": a["shift_code"],
                            "department": a["department"],
                        },
                        "second": {
                            "roster_id": b["id"],
                            "roster_date": b["roster_date"],
                            "shift_code": b["shift_code"],
                            "department": b["department"],
                        },
                        "message": (
                            f"{a.get('full_name') or employee_code} is on "
                            f"{a['shift_code']} ({a['roster_date']}) and "
                            f"{b['shift_code']} ({b['roster_date']}), these overlap."
                        ),
                    }
                )
    out.sort(key=lambda c: (c["first"]["roster_date"], c["employee_code"]))
    return out


def get_roster(*, date_from: str, date_to: str, department: str | None = None) -> dict:
    """The board: for each date and shift, who is planned and what happened.

    PLANNED vs ACTUAL is the whole point. `planned` is the roster row; `actual`
    is the attendance row for the same person and date, or None. A person with
    no attendance row shows as `unrecorded` — not as present, and not as absent.

    A line AFTER the hospital's today shows as `scheduled` and is not counted
    as a gap: record_attendance refuses a future date, so flagging it as "not
    recorded" was a warning nobody could clear (248 of them on 1 October).
    """
    start, end = _as_date(date_from, "date_from"), _as_date(date_to, "date_to")
    if end < start:
        raise WorkforceError("date_to is before date_from", code="bad_range")
    if (end - start).days > 62:
        raise WorkforceError("roster window is limited to 62 days", code="bad_range")

    with db_session() as db:
        shifts = _shift_map(db)
        params: list[Any] = [start.isoformat(), end.isoformat()]
        sql = (
            "SELECT r.*, e.full_name, e.designation FROM duty_roster r "
            "LEFT JOIN staff_employment e ON e.employee_code = r.employee_code "
            "WHERE r.status <> 'cancelled' AND r.roster_date >= ? AND r.roster_date <= ?"
        )
        if _clean(department):
            sql += " AND r.department = ?"
            params.append(_clean(department) or "")
        roster_rows = _rows(
            db.execute(sql + " ORDER BY r.roster_date, r.shift_code, e.full_name", params)
        )

        attendance = {
            (r["employee_code"], r["attendance_date"]): dict(r)
            for r in db.execute(
                "SELECT * FROM attendance_days WHERE attendance_date >= ? AND attendance_date <= ?",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        }
        holidays = _holiday_set(db, start, end)

    today_key = hospital_today().isoformat()
    days: dict[str, dict] = {}
    for day in _daterange(start, end):
        key = day.isoformat()
        days[key] = {
            "date": key,
            "is_holiday": key in holidays,
            "weekday": day.weekday(),
            "shifts": {},
        }

    for row in roster_rows:
        day_key = str(row["roster_date"])[:10]
        bucket = days.setdefault(
            day_key,
            {
                "date": day_key,
                "is_holiday": False,
                "weekday": _as_date(day_key).weekday(),
                "shifts": {},
            },
        )
        shift = shifts.get(row["shift_code"])
        slot = bucket["shifts"].setdefault(
            row["shift_code"],
            {
                "shift_code": row["shift_code"],
                "shift_name": shift["name"] if shift else row["shift_code"],
                "start_time": shift["start_time"] if shift else None,
                "end_time": shift["end_time"] if shift else None,
                "assignments": [],
                "planned_count": 0,
                "present_count": 0,
                "absent_count": 0,
                "leave_count": 0,
                "unrecorded_count": 0,
                "scheduled_count": 0,
            },
        )
        actual = attendance.get((row["employee_code"], day_key))
        if actual:
            actual_status = actual["status"]
        else:
            actual_status = "scheduled" if day_key > today_key else "unrecorded"
        slot["assignments"].append(
            {
                "roster_id": row["id"],
                "employee_code": row["employee_code"],
                "employee_name": row.get("full_name") or "Unknown employee",
                "designation": row.get("designation") or row.get("assigned_role"),
                "department": row["department"],
                "ward": row["ward"],
                "status": row["status"],
                "actual_status": actual_status,
                "actual_in": actual["first_in_at"] if actual else None,
                "actual_out": actual["last_out_at"] if actual else None,
                "worked_minutes": actual["worked_minutes"] if actual else 0,
                "overtime_minutes": actual["overtime_minutes"] if actual else 0,
                "late_minutes": actual["late_minutes"] if actual else 0,
            }
        )
        slot["planned_count"] += 1
        if actual_status == "present":
            slot["present_count"] += 1
        elif actual_status == "absent":
            slot["absent_count"] += 1
        elif actual_status == "leave":
            slot["leave_count"] += 1
        elif actual_status == "unrecorded":
            slot["unrecorded_count"] += 1
        elif actual_status == "scheduled":
            slot["scheduled_count"] += 1

    ordered_days = []
    for key in sorted(days):
        bucket = days[key]
        bucket["shifts"] = sorted(
            bucket["shifts"].values(),
            key=lambda s: (shifts.get(s["shift_code"], {}).get("start_minute", 0), s["shift_code"]),
        )
        ordered_days.append(bucket)

    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "department": _clean(department),
        "days": ordered_days,
        "shift_types": [
            _shift_view(dict(s)) if not isinstance(s, dict) else s for s in shifts.values()
        ],
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Attendance
# ═════════════════════════════════════════════════════════════════════════════


def _compute_span(
    first_in: datetime | None, last_out: datetime | None, shift: dict | None, attendance_date: date
) -> tuple[int, int, int]:
    """(worked_minutes, late_minutes, overtime_minutes) for one day.

    OVERTIME IS A DAILY THRESHOLD, and the boundary is `>`, not `>=`: working
    exactly the threshold is not overtime. That single character is the whole
    test at server/tests/test_workforce.py::TestOvertimeBoundary, because an
    off-by-one here pays or withholds one minute for every employee, every day,
    and nothing on any screen would look wrong."""
    if first_in is None or last_out is None:
        return 0, 0, 0
    span = int((last_out - first_in).total_seconds() // 60)
    if span <= 0:
        raise WorkforceError(
            "Punch-out is not after punch-in. Both are full timestamps, so a "
            "night shift ending the next morning is fine. This one is not.",
            code="bad_span",
        )
    if span > config.WORKFORCE_MAX_SPAN_MINUTES:
        raise WorkforceError(
            f"{span // 60}h {span % 60:02d}m is longer than this site allows for a "
            f"single attendance record ({config.WORKFORCE_MAX_SPAN_MINUTES // 60}h). "
            "That is nearly always a punch-out that was never recorded; correct "
            "the times or raise ORB_WORKFORCE_MAX_SPAN_MINUTES.",
            code="span_too_long",
        )

    late = 0
    if shift is not None:
        # Scheduled start as a UTC instant. The roster's clock times are
        # HOSPITAL-LOCAL, so the comparison has to be made in the hospital's
        # zone — comparing a local 07:00 against a UTC punch marks every nurse
        # in India 5h30 late.
        #
        # THE DATE CONVENTION, because a night shift makes it ambiguous:
        # an attendance day is stamped with the date the SHIFT STARTS. A night
        # shift running 21:00 on the 3rd to 07:00 on the 4th is recorded against
        # the 3rd. `duty_shift_types` stores start + duration for the same
        # reason (see migration `workforce_attendance`), so both halves of this
        # module agree on which day a night belongs to.
        tz = hospital_timezone()
        scheduled = datetime.combine(attendance_date, datetime.min.time(), tzinfo=tz) + timedelta(
            minutes=shift["start_minute"]
        )
        grace = timedelta(minutes=config.WORKFORCE_LATE_GRACE_MINUTES)
        behind = int((first_in - scheduled).total_seconds() // 60)
        # A LATE MARK IS ONLY MEANINGFUL WITHIN THE SHIFT ITSELF.
        #
        # If the punch-in is later than the shift's own length, this punch
        # cannot belong to this instance of this shift — the commonest cause is
        # a night worked but recorded against the wrong shift code or the wrong
        # date. Emitting the raw difference then puts a "840 minutes late" into
        # a payroll file, and a late figure in a payroll file is a disciplinary
        # document. Orb will not manufacture one out of a data-entry slip: past
        # that horizon the late mark is ZERO, and the mismatch stays visible in
        # the worked minutes and the punch times, which are facts.
        if grace < timedelta(minutes=behind) <= timedelta(minutes=shift["duration_minutes"]):
            late = behind

    threshold = config.WORKFORCE_OT_DAILY_MINUTES
    overtime = span - threshold if span > threshold else 0
    return span, late, overtime


def record_attendance(
    *,
    employee_code: str,
    attendance_date: str,
    status: str,
    shift_code: str | None = None,
    first_in_at: str | None = None,
    last_out_at: str | None = None,
    leave_type_code: str | None = None,
    source: str = "manual",
    remarks: str | None = None,
    recorded_by: str | None = None,
) -> dict:
    """Record (or correct) one employee's one day. Upsert on (employee, date).

    Correcting an existing day REPLACES it rather than adding a second row: two
    rows for one day would break the summing identity in `monthly_summary` with
    nothing raising an error, which is why the unique index exists in the
    schema as well as this upsert."""
    employee = _require(employee_code, "employee_code").upper()
    day = _as_date(attendance_date, "attendance_date")
    if status not in DAY_STATUSES:
        raise WorkforceError(f"status must be one of {', '.join(DAY_STATUSES)}", code="bad_status")
    if source not in ATTENDANCE_SOURCES:
        raise WorkforceError("unknown attendance source", code="bad_source")
    if day > hospital_today():
        raise WorkforceError("Attendance cannot be recorded for a future date.", code="future_date")

    code = clean_shift.upper() if (clean_shift := _clean(shift_code)) else None
    leave_code = clean_leave.upper() if (clean_leave := _clean(leave_type_code)) else None

    if status == "leave" and not leave_code:
        raise WorkforceError(
            "A leave day must name its leave type. The payroll provider cannot "
            "price a leave day that does not say which leave it was.",
            code="leave_type_required",
        )
    if status != "leave" and leave_code:
        raise WorkforceError(
            "leave_type_code is only valid on a leave day", code="leave_type_unexpected"
        )
    if status != "present" and (first_in_at or last_out_at):
        raise WorkforceError(
            f"A '{status}' day cannot carry punch times. Record it as present if "
            "the person worked.",
            code="times_on_non_present",
        )
    if status == "present" and not (first_in_at and last_out_at):
        raise WorkforceError(
            "A present day needs both a punch-in and a punch-out. Record what is "
            "known and correct it later rather than guessing the other half.",
            code="times_required",
        )

    first_in = _as_instant(first_in_at, "first_in_at") if first_in_at else None
    last_out = _as_instant(last_out_at, "last_out_at") if last_out_at else None

    with db_session() as db:
        emp = db.execute(
            "SELECT * FROM staff_employment WHERE employee_code = ?", (employee,)
        ).fetchone()
        if emp is None:
            raise WorkforceError(f"No employee with code {employee}", code="not_found")
        joining = _as_date(emp["date_of_joining"]) if emp["date_of_joining"] else None
        exit_day = _as_date(emp["date_of_exit"]) if emp["date_of_exit"] else None
        if joining and day < joining:
            raise WorkforceError(
                f"{emp['full_name']} joined on {joining.isoformat()}.", code="before_joining"
            )
        if exit_day and day > exit_day:
            raise WorkforceError(
                f"{emp['full_name']} left on {exit_day.isoformat()}.", code="after_exit"
            )

        shift = None
        if code:
            shift_row = db.execute(
                "SELECT * FROM duty_shift_types WHERE code = ?", (code,)
            ).fetchone()
            if shift_row is None:
                raise WorkforceError(f"Unknown shift {code}", code="unknown_shift")
            shift = _shift_view(dict(shift_row))
        if leave_code:
            leave_row = db.execute(
                "SELECT code FROM leave_types WHERE code = ?", (leave_code,)
            ).fetchone()
            if leave_row is None:
                raise WorkforceError(f"Unknown leave type {leave_code}", code="unknown_leave_type")

        worked, late, overtime = _compute_span(first_in, last_out, shift, day)

        db.execute(
            """INSERT INTO attendance_days
                 (attendance_date, employee_code, shift_code, status, leave_type_code,
                  first_in_at, last_out_at, worked_minutes, late_minutes,
                  overtime_minutes, source, remarks, recorded_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(employee_code, attendance_date) DO UPDATE SET
                 shift_code=excluded.shift_code, status=excluded.status,
                 leave_type_code=excluded.leave_type_code,
                 first_in_at=excluded.first_in_at, last_out_at=excluded.last_out_at,
                 worked_minutes=excluded.worked_minutes,
                 late_minutes=excluded.late_minutes,
                 overtime_minutes=excluded.overtime_minutes,
                 source=excluded.source, remarks=excluded.remarks,
                 recorded_by=excluded.recorded_by, updated_at=CURRENT_TIMESTAMP""",
            (
                day.isoformat(),
                employee,
                code,
                status,
                leave_code,
                _iso(first_in),
                _iso(last_out),
                worked,
                late,
                overtime,
                source,
                _clean(remarks),
                _clean(recorded_by),
            ),
        )
        row = db.execute(
            "SELECT * FROM attendance_days WHERE employee_code = ? AND attendance_date = ?",
            (employee, day.isoformat()),
        ).fetchone()
    out = _row(row)
    out["employee_name"] = emp["full_name"]
    out["worked_hours"] = minutes_to_hours(out["worked_minutes"])
    out["overtime_hours"] = minutes_to_hours(out["overtime_minutes"])
    return out


def list_attendance(
    *, date_from: str, date_to: str, department: str | None = None, employee_code: str | None = None
) -> list[dict]:
    start, end = _as_date(date_from, "date_from"), _as_date(date_to, "date_to")
    if end < start:
        raise WorkforceError("date_to is before date_from", code="bad_range")
    clauses = ["a.attendance_date >= ?", "a.attendance_date <= ?"]
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if _clean(department):
        clauses.append("e.department = ?")
        params.append(_clean(department) or "")
    if _clean(employee_code):
        clauses.append("a.employee_code = ?")
        params.append((_clean(employee_code) or "").upper())
    with db_session() as db:
        rows = _rows(
            db.execute(
                "SELECT a.*, e.full_name, e.department, e.designation FROM attendance_days a "
                "LEFT JOIN staff_employment e ON e.employee_code = a.employee_code "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY a.attendance_date DESC, e.full_name",
                params,
            )
        )
    for row in rows:
        row["employee_name"] = row.pop("full_name", None) or "Unknown employee"
        row["worked_hours"] = minutes_to_hours(row["worked_minutes"])
        row["overtime_hours"] = minutes_to_hours(row["overtime_minutes"])
    return rows


# ═════════════════════════════════════════════════════════════════════════════
#  Monthly summary — the arithmetic everything downstream trusts
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class _Buckets:
    present: int = 0
    absent: int = 0
    leave_paid: int = 0
    leave_unpaid: int = 0
    weekly_off: int = 0
    holiday: int = 0
    unrecorded: int = 0


def _classify_period(
    employee: dict,
    start: date,
    end: date,
    attendance: dict[str, dict],
    holidays: set[str],
    paid_leave: dict[str, bool],
    today: date | None = None,
) -> dict:
    """Assign EVERY day in [start, end] ∩ employment to exactly one bucket.

    The order of the tests below is the whole design:

      1. A RECORDED DAY WINS. If somebody wrote down what happened, that is what
         happened — including a nurse who came in on her own day off, who lands
         in `present` and whose weekly-off count is correctly one lower.
      2. Then a declared holiday.
      3. Then the employee's fixed weekly off. NULL weekly_off_day (rotational)
         matches nothing here, so a rotational employee's days off must be
         RECORDED as `weekly_off` — Orb will not invent one, because inventing
         one would silently convert an unrecorded day into a day off.
      4. Otherwise: UNRECORDED. Never absent. See the module docstring.

    Days outside the employment window are not counted at all: a person who
    joined on the 14th did not have 13 absences.

    Days AFTER the hospital's today are not counted either. They have not
    happened, record_attendance refuses them, and counting them as unrecorded
    flagged a whole month of future days as gaps nobody could close. They are
    reported as `days_still_to_come`, outside the identity, and
    `attendance_complete` stays False until the period is over.
    """
    joining = _as_date(employee["date_of_joining"]) if employee["date_of_joining"] else None
    exit_day = _as_date(employee["date_of_exit"]) if employee["date_of_exit"] else None
    weekly_off = employee["weekly_off_day"]

    counts = {
        "present": 0,
        "absent": 0,
        "leave_paid": 0,
        "leave_unpaid": 0,
        "weekly_off": 0,
        "holiday": 0,
        "unrecorded": 0,
    }
    worked_minutes = overtime_minutes = late_minutes = late_marks = 0
    night = evening = holiday_shift = weekly_off_worked = 0
    leave_by_type: dict[str, int] = {}
    in_employment = 0
    still_to_come = 0
    unrecorded_dates: list[str] = []
    today = today or hospital_today()

    for day in _daterange(start, end):
        if joining and day < joining:
            continue
        if exit_day and day > exit_day:
            continue
        if day > today:
            still_to_come += 1
            continue
        in_employment += 1
        key = day.isoformat()
        record = attendance.get(key)
        is_holiday = key in holidays
        is_weekly_off = weekly_off is not None and day.weekday() == int(weekly_off)

        if record is not None:
            status = record["status"]
            if status == "present":
                counts["present"] += 1
                worked_minutes += int(record["worked_minutes"] or 0)
                overtime_minutes += int(record["overtime_minutes"] or 0)
                late = int(record["late_minutes"] or 0)
                late_minutes += late
                if late > 0:
                    late_marks += 1
                allowance = record.get("allowance_code")
                if allowance == "night":
                    night += 1
                elif allowance == "evening":
                    evening += 1
                if is_holiday:
                    holiday_shift += 1
                if is_weekly_off:
                    weekly_off_worked += 1
            elif status == "absent":
                counts["absent"] += 1
            elif status == "leave":
                code = record["leave_type_code"]
                leave_by_type[code] = leave_by_type.get(code, 0) + 1
                if paid_leave.get(code, True):
                    counts["leave_paid"] += 1
                else:
                    counts["leave_unpaid"] += 1
            elif status == "weekly_off":
                counts["weekly_off"] += 1
            elif status == "holiday":
                counts["holiday"] += 1
            continue

        if is_holiday:
            counts["holiday"] += 1
        elif is_weekly_off:
            counts["weekly_off"] += 1
        else:
            counts["unrecorded"] += 1
            unrecorded_dates.append(key)

    leave_total = counts["leave_paid"] + counts["leave_unpaid"]
    working_days = counts["present"] + counts["absent"] + leave_total + counts["unrecorded"]

    return {
        "employee_code": employee["employee_code"],
        "employee_name": employee["full_name"],
        "department": employee["department"],
        "designation": employee["designation"],
        "employment_type": employee["employment_type"],
        "payroll_bureau_ref": employee["payroll_bureau_ref"],
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        # Days IN EMPLOYMENT within the period, up to today. Not the calendar
        # length of the month: the identity below is stated against this
        # number for exactly that reason.
        "days_in_period": in_employment,
        # In employment but after today: not yet counted in any bucket.
        "days_still_to_come": still_to_come,
        "counted_through": min(end, today).isoformat() if today >= start else None,
        "days_working": working_days,
        "days_present": counts["present"],
        "days_absent": counts["absent"],
        "days_leave_paid": counts["leave_paid"],
        "days_leave_unpaid": counts["leave_unpaid"],
        "days_leave_total": leave_total,
        "days_weekly_off": counts["weekly_off"],
        "days_holiday": counts["holiday"],
        "days_unrecorded": counts["unrecorded"],
        "days_absent_or_unpaid_leave": counts["absent"] + counts["leave_unpaid"],
        "leave_by_type": leave_by_type,
        # Complete only once nothing is unrecorded AND the period is over: a
        # month with days still to come is not a month payroll can close on.
        "attendance_complete": counts["unrecorded"] == 0 and still_to_come == 0,
        # Capped so a month of gaps does not become a 900-element field on a
        # screen; the count above is always exact.
        "unrecorded_dates": unrecorded_dates[:31],
        "worked_minutes": worked_minutes,
        "worked_hours": minutes_to_hours(worked_minutes),
        "overtime_minutes": overtime_minutes,
        "overtime_hours": minutes_to_hours(overtime_minutes),
        "late_marks": late_marks,
        "late_minutes": late_minutes,
        "night_shift_count": night,
        "evening_shift_count": evening,
        "holiday_shift_count": holiday_shift,
        "weekly_off_worked_count": weekly_off_worked,
    }


def _load_period(db, start: date, end: date, employees: list[dict]) -> tuple[dict, set[str], dict]:
    codes = [e["employee_code"] for e in employees]
    attendance: dict[str, dict[str, dict]] = {c: {} for c in codes}
    if codes:
        # Chunked so a hospital with more than SQLite's 999-variable limit worth
        # of staff still gets an answer instead of an OperationalError.
        for i in range(0, len(codes), 400):
            chunk = codes[i : i + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = db.execute(
                f"SELECT a.*, s.allowance_code FROM attendance_days a "
                f"LEFT JOIN duty_shift_types s ON s.code = a.shift_code "
                f"WHERE a.attendance_date >= ? AND a.attendance_date <= ? "
                f"AND a.employee_code IN ({placeholders})",
                [start.isoformat(), end.isoformat(), *chunk],
            ).fetchall()
            for r in rows:
                row = dict(r)
                attendance.setdefault(row["employee_code"], {})[
                    str(row["attendance_date"])[:10]
                ] = row
    holidays = _holiday_set(db, start, end)
    paid_leave = {
        r["code"]: bool(r["is_paid"])
        for r in db.execute("SELECT code, is_paid FROM leave_types").fetchall()
    }
    return attendance, holidays, paid_leave


def monthly_summary(*, employee_code: str, year: int, month: int) -> dict:
    """One employee, one month, every bucket, plus the identity check itself.

    `identity_holds` is computed here rather than only asserted in a test, and
    it is returned to the caller. A summary that cannot show its own arithmetic
    is a number somebody has to take on faith, and payroll figures are exactly
    where nobody should."""
    start, end = period_bounds(year, month)
    employee = get_employee(employee_code)
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM staff_employment WHERE employee_code = ?", (employee["employee_code"],)
        ).fetchone()
        attendance, holidays, paid_leave = _load_period(db, start, end, [dict(row)])
    summary = _classify_period(
        dict(row), start, end, attendance.get(employee["employee_code"], {}), holidays, paid_leave
    )
    return _with_identity(summary)


def _with_identity(summary: dict) -> dict:
    buckets = (
        summary["days_present"]
        + summary["days_absent"]
        + summary["days_leave_total"]
        + summary["days_unrecorded"]
        + summary["days_weekly_off"]
        + summary["days_holiday"]
    )
    summary["identity"] = {
        "buckets_sum": buckets,
        "days_in_period": summary["days_in_period"],
        "holds": buckets == summary["days_in_period"]
        and (
            summary["days_present"]
            + summary["days_absent"]
            + summary["days_leave_total"]
            + summary["days_unrecorded"]
            == summary["days_working"]
        ),
        "statement": (
            "present + absent + leave + unrecorded + weekly off + holiday "
            "= days in period; present + absent + leave + unrecorded "
            "= working days"
        ),
    }
    if not summary["identity"]["holds"]:
        # Not raised: a summary that refuses to render leaves a payroll clerk
        # with nothing at all. Logged loudly and flagged in the payload so the
        # screen can say the number is not trustworthy.
        logger.error(
            "workforce summary identity FAILED for %s %s..%s: %s",
            summary["employee_code"],
            summary["period_start"],
            summary["period_end"],
            summary["identity"],
        )
    return summary


def period_summaries(
    *, year: int, month: int, department: str | None = None, include_inactive: bool = False
) -> dict:
    """Every employee's month, in one pass. The export and the UI both read this
    so the screen and the file can never disagree."""
    start, end = period_bounds(year, month)
    clauses, params = [], []
    if not include_inactive:
        # An employee who LEFT during the period still belongs in it — they were
        # paid for the days they worked. `is_active` alone would drop them, and
        # a leaver missing from their final payroll file is the single most
        # expensive error this export can make.
        clauses.append("(is_active = 1 OR date_of_exit IS NULL OR date_of_exit >= ?)")
        params.append(start.isoformat())
    if _clean(department):
        clauses.append("department = ?")
        params.append(_clean(department) or "")
    # Somebody who joins after the period ends was never in it.
    clauses.append("(date_of_joining IS NULL OR date_of_joining <= ?)")
    params.append(end.isoformat())
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    with db_session() as db:
        employees = _rows(
            db.execute(
                f"SELECT * FROM staff_employment{where} ORDER BY department, full_name",
                params,
            )
        )
        attendance, holidays, paid_leave = _load_period(db, start, end, employees)

    today = hospital_today()
    rows = [
        _with_identity(
            _classify_period(
                emp,
                start,
                end,
                attendance.get(emp["employee_code"], {}),
                holidays,
                paid_leave,
                today=today,
            )
        )
        for emp in employees
    ]
    # Nobody who was not employed for a single day of the period. Days still to
    # come count: a future month still lists the people rostered into it.
    rows = [r for r in rows if r["days_in_period"] + r["days_still_to_come"] > 0]

    incomplete = [r for r in rows if not r["attendance_complete"]]
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "year": start.year,
        "month": start.month,
        "department": _clean(department),
        # The last day counted (today, inside an open month), None when the
        # whole period is still to come, and whether the period is over.
        "counted_through": min(end, today).isoformat() if today >= start else None,
        "period_closed": today > end,
        "employees": rows,
        "employee_count": len(rows),
        "incomplete_employee_count": len(incomplete),
        "unrecorded_day_count": sum(r["days_unrecorded"] for r in rows),
        "still_to_come_day_count": sum(r["days_still_to_come"] for r in rows),
        "identity_holds": all(r["identity"]["holds"] for r in rows),
        "totals": {
            "days_present": sum(r["days_present"] for r in rows),
            "days_absent": sum(r["days_absent"] for r in rows),
            "days_leave_total": sum(r["days_leave_total"] for r in rows),
            "days_unrecorded": sum(r["days_unrecorded"] for r in rows),
            "overtime_minutes": sum(r["overtime_minutes"] for r in rows),
            "overtime_hours": minutes_to_hours(sum(r["overtime_minutes"] for r in rows)),
            "night_shift_count": sum(r["night_shift_count"] for r in rows),
            "evening_shift_count": sum(r["evening_shift_count"] for r in rows),
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Payroll export
# ═════════════════════════════════════════════════════════════════════════════


def build_payroll_export(*, year: int, month: int, department: str | None = None) -> dict:
    """The rows a payroll bureau receives, as dicts keyed by the exact columns.

    Every statutory column is the EMPTY STRING and every row says
    `statutory_computed_by=payroll_provider`. That is not decoration: the file
    lands on a desk months after this conversation, and it has to explain its
    own boundary without anyone remembering it."""
    summary = period_summaries(year=year, month=month, department=department)
    rows: list[dict[str, str]] = []
    for emp in summary["employees"]:
        row = {
            "employee_code": emp["employee_code"],
            "employee_name": emp["employee_name"],
            "department": emp["department"] or "",
            "designation": emp["designation"] or "",
            "employment_type": emp["employment_type"] or "",
            "payroll_bureau_ref": emp["payroll_bureau_ref"] or "",
            "period_start": emp["period_start"],
            "period_end": emp["period_end"],
            "days_in_period": str(emp["days_in_period"]),
            "days_working": str(emp["days_working"]),
            "days_present": str(emp["days_present"]),
            "days_absent": str(emp["days_absent"]),
            "days_leave_paid": str(emp["days_leave_paid"]),
            "days_leave_unpaid": str(emp["days_leave_unpaid"]),
            "days_leave_total": str(emp["days_leave_total"]),
            "days_weekly_off": str(emp["days_weekly_off"]),
            "days_holiday": str(emp["days_holiday"]),
            "days_unrecorded": str(emp["days_unrecorded"]),
            "days_absent_or_unpaid_leave": str(emp["days_absent_or_unpaid_leave"]),
            "attendance_complete": "yes" if emp["attendance_complete"] else "no",
            "worked_minutes": str(emp["worked_minutes"]),
            "worked_hours": emp["worked_hours"],
            "overtime_minutes": str(emp["overtime_minutes"]),
            "overtime_hours": emp["overtime_hours"],
            "late_marks": str(emp["late_marks"]),
            "late_minutes": str(emp["late_minutes"]),
            "night_shift_count": str(emp["night_shift_count"]),
            "evening_shift_count": str(emp["evening_shift_count"]),
            "holiday_shift_count": str(emp["holiday_shift_count"]),
            "weekly_off_worked_count": str(emp["weekly_off_worked_count"]),
            "statutory_computed_by": STATUTORY_COMPUTED_BY,
        }
        for column in STATUTORY_COLUMNS:
            row[column] = ""
        rows.append(row)

    # Read by the person sending the file, so real plurals and the column
    # names in quotes as they appear in the CSV, not bare like code.
    warnings: list[str] = []
    if not summary["period_closed"]:
        to_come = summary["still_to_come_day_count"]
        warnings.append(
            "This month is not over. The file counts attendance up to "
            f"{summary['counted_through'] or 'nothing yet'}, and the {to_come} "
            f"{'day' if to_come == 1 else 'days'} still to come are not in any column. "
            'Every row says "no" under "attendance_complete" until the month closes.'
        )
    if summary["unrecorded_day_count"]:
        people = sum(1 for e in summary["employees"] if e["days_unrecorded"])
        days = summary["unrecorded_day_count"]
        warnings.append(
            f"{people} {'employee has' if people == 1 else 'employees have'} {days} past "
            f"{'day' if days == 1 else 'days'} with no attendance record. Those days go "
            'in the "days_unrecorded" column and the row says "no" under '
            '"attendance_complete". Orb does not guess them: a missing day is '
            "not an absence."
        )
    if not summary["identity_holds"]:
        warnings.append(
            "The day buckets do not sum to the days in the period for at least "
            "one employee. Do not send this file. Raise it as a defect."
        )
    if not rows:
        warnings.append("No employees fall inside this period.")

    return {
        "columns": list(PAYROLL_EXPORT_COLUMNS),
        "rows": rows,
        "period_start": summary["period_start"],
        "period_end": summary["period_end"],
        "year": summary["year"],
        "month": summary["month"],
        "department": summary["department"],
        "employee_count": summary["employee_count"],
        "incomplete_employee_count": summary["incomplete_employee_count"],
        "counted_through": summary["counted_through"],
        "period_closed": summary["period_closed"],
        "still_to_come_day_count": summary["still_to_come_day_count"],
        "file_name": _export_filename(summary["year"], summary["month"], summary["department"]),
        "warnings": warnings,
        "statutory_note": (
            "PF, ESI, Professional Tax and TDS columns are intentionally empty. "
            "They are computed by the hospital's payroll provider, not by Orb, "
            "those rules change with each budget and differ by state, and every "
            'row of this file records that in its "statutory_computed_by" '
            f'column ("{STATUTORY_COMPUTED_BY}").'
        ),
    }


def _export_filename(year: int, month: int, department: str | None) -> str:
    suffix = ""
    if department:
        safe = "".join(ch if ch.isalnum() else "-" for ch in department).strip("-")
        suffix = f"-{safe.lower()}" if safe else ""
    return f"orb-payroll-{year:04d}-{month:02d}{suffix}.csv"


def render_payroll_csv(export: dict) -> str:
    """The file itself.

    `\\r\\n` line endings and QUOTE_MINIMAL, because the receiving end is a
    payroll bureau's Excel macro far more often than it is a parser, and RFC
    4180 is what those tools expect. Missing keys are written as empty, never
    skipped: a short row would silently shift every subsequent column."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=list(PAYROLL_EXPORT_COLUMNS),
        lineterminator="\r\n",
        extrasaction="ignore",
        restval="",
    )
    writer.writeheader()
    for row in export["rows"]:
        writer.writerow(row)
    return buf.getvalue()


def record_export_run(
    export: dict, csv_text: str, *, generated_by: str | None = None, notes: str | None = None
) -> dict:
    """Persist WHAT LEFT THE BUILDING: the header order, the row count and the
    SHA-256 of the exact bytes. Three months later a bureau asks why an
    employee's overtime is 6 hours; the answer is this checksum against the file
    they still have, not a regeneration from data that has since been fixed."""
    digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    header = ",".join(PAYROLL_EXPORT_COLUMNS)
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO payroll_export_runs
                 (period_start, period_end, department, file_name, row_count,
                  employee_count, incomplete_employee_count, column_signature,
                  sha256, generated_by, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                export["period_start"],
                export["period_end"],
                export["department"],
                export["file_name"],
                len(export["rows"]),
                export["employee_count"],
                export["incomplete_employee_count"],
                header,
                digest,
                _clean(generated_by),
                _clean(notes),
            ),
        )
        row = db.execute(
            "SELECT * FROM payroll_export_runs WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row(row)


def list_export_runs(*, limit: int = 25) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    with db_session() as db:
        return _rows(
            db.execute(
                "SELECT * FROM payroll_export_runs ORDER BY generated_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        )
