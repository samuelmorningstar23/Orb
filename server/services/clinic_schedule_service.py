"""Clinic sessions, their exceptions and the slots they offer (opd-15, W2-E).

A CLINIC SESSION is a doctor's weekly outpatient clinic: "Dr Iyer, Cardiology,
Mondays 09:00 to 13:00, 15-minute slots, Room 4, from 1 November". An EXCEPTION
is a day it does not run (leave, a holiday, a cancelled clinic). SLOTS are
never stored: availability() computes them from those two tables in
hospital-local time every time it is asked, so no slot table can drift from
the session it came from. Migration 57 (migrations_his/m0057_clinic_sessions)
creates both tables.

THE RULES THIS MODULE HOLDS
  * Hospital-local time throughout. A session's weekday, start and end are wall
    clock times at the hospital; the day boundary is the hospital's midnight
    (services.hospital_time), never UTC's. In IST the UTC date is yesterday's
    until 05:30, so a UTC day key would offer Sunday's clinic at 00:30 on
    Monday.
  * One doctor, one place at a time. A session that overlaps another active
    session of the same doctor (same weekday, overlapping hours, overlapping
    validity dates, in ANY department) is refused, so a desk can never book
    one doctor into two rooms at once.
  * The doctor is an active account that may run a clinic (opd.consult, from
    permissions.ROLE_PERMISSIONS, the one authority) and the session's
    department is that doctor's own, spelled as the users row spells it: the
    same rule the front desk applies when it books a visit with a doctor.
  * A deactivated doctor's sessions offer no slots.
  * Configuration, not patient data: nothing here reads or writes a patient.
    Every change is audited (category 'configuration') inside the transaction
    that makes it, with the values before and after.
  * Nothing is deleted. A session is retired, an exception is withdrawn, and
    both keep who and why.

Every function opens its own session; writes take BEGIN IMMEDIATE so the
overlap check and the insert see the same rows.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from database import db_session
from permissions import OPD_CONSULT, ROLE_PERMISSIONS
from services import hospital_time
from services.audit_service import log_audit_event
from services.clinical_time import format_utc, utc_iso_now

#: The widest availability window one request may ask for.
MAX_AVAILABILITY_DAYS = 14
MIN_SLOT_MINUTES = 5
MAX_SLOT_MINUTES = 240
MAX_CAPACITY_PER_SLOT = 50

_MAX_ROOM_LEN = 80
_MAX_NOTE_LEN = 500
_MAX_REASON_LEN = 500
_MAX_DEPARTMENT_LEN = 200

_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: The audit category for every change here. Configuration, no PHI.
AUDIT_CATEGORY = "configuration"


class ClinicScheduleError(ValueError):
    """A refusal with the HTTP status it maps to (422, 404 or 409) and a
    machine-readable code. The message is plain words for the desk."""

    def __init__(self, message: str, *, status: int = 422, code: str = "invalid"):
        super().__init__(message)
        self.status = status
        self.code = code


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_hhmm(value: object, field: str = "time") -> str:
    """A 24-hour 'HH:MM' wall-clock time, normalised ('9:05' becomes '09:05')."""
    text = str(value or "").strip()
    if re.match(r"^\d:\d\d$", text):
        text = "0" + text
    if not _HHMM.match(text):
        raise ClinicScheduleError(f"{field} must be a time like 09:30", code="bad_time")
    return text


def parse_day(value: object, field: str = "date") -> date:
    """A 'YYYY-MM-DD' calendar day (a hospital-local day, never an instant)."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not _DAY.match(text):
        raise ClinicScheduleError(f"{field} must be a date like 2026-10-05", code="bad_date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ClinicScheduleError(f"{field} is not a real calendar date", code="bad_date")


def _optional_day(value: object, field: str) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    return parse_day(value, field).isoformat()


def _clean(value: object, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12].upper()}"


# ── Who may hold a clinic ────────────────────────────────────────────────────

def consulting_roles() -> tuple[str, ...]:
    """Roles that may run a clinic: those holding opd.consult in the one table."""
    return tuple(sorted(r for r, perms in ROLE_PERMISSIONS.items() if OPD_CONSULT in perms))


def list_clinic_doctors(department: Optional[str] = None) -> list[dict]:
    """Active accounts that may hold a clinic, with their department. Staff
    names only, no patient data."""
    roles = consulting_roles()
    marks = ",".join("?" for _ in roles)
    sql = (f"SELECT id, name, TRIM(department) AS department FROM users "
           f"WHERE role IN ({marks}) AND COALESCE(is_active, 1) = 1 "
           f"AND department IS NOT NULL AND TRIM(department) != ''")
    params: list[Any] = list(roles)
    if department and department.strip():
        sql += " AND LOWER(TRIM(department)) = LOWER(?)"
        params.append(department.strip())
    sql += " ORDER BY name, id"
    with db_session() as db:
        rows = db.execute(sql, params).fetchall()
    return [{"id": r["id"], "name": r["name"], "department": r["department"]} for r in rows]


def _resolve_doctor(db, doctor_id: object, department: object) -> dict:
    """The doctor's row, refused unless they may hold a clinic here. Returns
    {'id', 'name', 'department'} with the department as users spells it."""
    did = str(doctor_id or "").strip()
    if not did:
        raise ClinicScheduleError("Choose the doctor who holds this clinic", code="doctor_required")
    row = db.execute(
        "SELECT id, name, role, TRIM(COALESCE(department, '')) AS department, "
        "COALESCE(is_active, 1) AS active FROM users WHERE id = ?",
        (did,),
    ).fetchone()
    if row is None or row["role"] not in consulting_roles() or not row["active"]:
        raise ClinicScheduleError(
            "That account is not an active doctor who can hold a clinic", code="not_a_doctor")
    if not row["department"]:
        raise ClinicScheduleError(
            f"{row['name']} has no department on record, so no clinic can be set for them",
            code="doctor_without_department")
    wanted = _clean(department, _MAX_DEPARTMENT_LEN)
    if wanted and wanted.casefold() != row["department"].casefold():
        raise ClinicScheduleError(
            f"{row['name']} works in {row['department']}, so their clinic is held there, "
            f"not in {wanted}",
            code="department_mismatch")
    return {"id": row["id"], "name": row["name"], "department": row["department"]}


# ── Serialising ──────────────────────────────────────────────────────────────

_SESSION_COLUMNS = (
    "id", "doctor_id", "department", "weekday", "start_time", "end_time", "slot_minutes",
    "capacity_per_slot", "room", "valid_from", "valid_to", "is_active", "notes",
    "row_version", "created_by", "created_at", "updated_by", "updated_at",
    "retired_by", "retired_at", "retired_reason",
)


def _session_dict(row) -> dict:
    out = {c: row[c] for c in _SESSION_COLUMNS}
    out["is_active"] = bool(out["is_active"])
    out["weekday_name"] = WEEKDAY_NAMES[int(row["weekday"])]
    keys = row.keys() if hasattr(row, "keys") else []
    out["doctor_name"] = row["doctor_name"] if "doctor_name" in keys else None
    out["doctor_active"] = bool(row["doctor_active"]) if "doctor_active" in keys else None
    out["slot_count"] = (_minutes(row["end_time"]) - _minutes(row["start_time"])) // int(row["slot_minutes"])
    return out


_EXCEPTION_COLUMNS = (
    "id", "doctor_id", "session_id", "exception_date", "kind", "reason", "created_by",
    "created_at", "withdrawn_by", "withdrawn_at", "withdrawn_reason",
)


def _exception_dict(row) -> dict:
    out = {c: row[c] for c in _EXCEPTION_COLUMNS}
    keys = row.keys() if hasattr(row, "keys") else []
    out["doctor_name"] = row["doctor_name"] if "doctor_name" in keys else None
    out["is_withdrawn"] = row["withdrawn_at"] is not None
    return out


def _session_row(db, session_id: str):
    return db.execute(
        "SELECT s.*, u.name AS doctor_name, COALESCE(u.is_active, 1) AS doctor_active "
        "FROM clinic_sessions s LEFT JOIN users u ON u.id = s.doctor_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()


# ── Audit ────────────────────────────────────────────────────────────────────

def _audit(db, *, action: str, actor: dict, target_type: str, target_id: str,
           summary: str, metadata: dict, request_meta: Optional[dict]) -> None:
    """One configuration row, written inside the caller's transaction."""
    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=f"{action}-{target_id}-{uuid.uuid4().hex[:8]}",
        event_category=AUDIT_CATEGORY,
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type=target_type,
        target_id=str(target_id),
        request_path=meta.get("path"),
        http_method=meta.get("method"),
        ip_address=meta.get("ip"),
        metadata=metadata,
    )


# ── Sessions ─────────────────────────────────────────────────────────────────

def _validated_session_fields(fields: dict) -> dict:
    """Validate the editable fields of a session. Returns the clean values."""
    try:
        weekday = int(fields.get("weekday"))
    except (TypeError, ValueError):
        raise ClinicScheduleError("Choose the day of the week", code="bad_weekday")
    if not 0 <= weekday <= 6:
        raise ClinicScheduleError("Choose the day of the week", code="bad_weekday")
    start = parse_hhmm(fields.get("start_time"), "Start time")
    end = parse_hhmm(fields.get("end_time"), "End time")
    if _minutes(end) <= _minutes(start):
        raise ClinicScheduleError(
            "The clinic must end after it starts, on the same day", code="bad_window")
    try:
        slot = int(fields.get("slot_minutes"))
    except (TypeError, ValueError):
        raise ClinicScheduleError("Slot length must be a number of minutes", code="bad_slot")
    if not MIN_SLOT_MINUTES <= slot <= MAX_SLOT_MINUTES:
        raise ClinicScheduleError(
            f"Slot length must be between {MIN_SLOT_MINUTES} and {MAX_SLOT_MINUTES} minutes",
            code="bad_slot")
    if _minutes(end) - _minutes(start) < slot:
        raise ClinicScheduleError(
            "The clinic is shorter than one slot", code="bad_slot")
    raw_capacity = fields.get("capacity_per_slot")
    try:
        capacity = 1 if raw_capacity is None else int(raw_capacity)
    except (TypeError, ValueError):
        raise ClinicScheduleError("Patients per slot must be a number", code="bad_capacity")
    if not 1 <= capacity <= MAX_CAPACITY_PER_SLOT:
        raise ClinicScheduleError(
            f"Patients per slot must be between 1 and {MAX_CAPACITY_PER_SLOT}",
            code="bad_capacity")
    valid_from = _optional_day(fields.get("valid_from"), "First day")
    valid_to = _optional_day(fields.get("valid_to"), "Last day")
    if valid_from and valid_to and valid_to < valid_from:
        raise ClinicScheduleError("The last day is before the first day", code="bad_dates")
    return {
        "weekday": weekday,
        "start_time": start,
        "end_time": end,
        "slot_minutes": slot,
        "capacity_per_slot": capacity,
        "room": _clean(fields.get("room"), _MAX_ROOM_LEN),
        "valid_from": valid_from,
        "valid_to": valid_to,
        "notes": _clean(fields.get("notes"), _MAX_NOTE_LEN),
    }


def _refuse_overlap(db, *, doctor_id: str, doctor_name: str, values: dict,
                    exclude_id: Optional[str] = None) -> None:
    """409 when another ACTIVE session of the same doctor overlaps this one in
    weekday, hours and validity dates, in any department."""
    clash = db.execute(
        """SELECT id, department, start_time, end_time, valid_from, valid_to
             FROM clinic_sessions
            WHERE doctor_id = ? AND weekday = ? AND is_active = 1
              AND id != ?
              AND start_time < ? AND ? < end_time
              AND (valid_to IS NULL OR ? IS NULL OR ? <= valid_to)
              AND (valid_from IS NULL OR ? IS NULL OR valid_from <= ?)
            ORDER BY start_time LIMIT 1""",
        (doctor_id, values["weekday"], exclude_id or "",
         values["end_time"], values["start_time"],
         values["valid_from"], values["valid_from"],
         values["valid_to"], values["valid_to"]),
    ).fetchone()
    if clash is not None:
        raise ClinicScheduleError(
            f"{doctor_name} already holds a clinic on {WEEKDAY_NAMES[values['weekday']]} "
            f"from {clash['start_time']} to {clash['end_time']} ({clash['department']}). "
            "One doctor cannot hold two clinics at the same time.",
            status=409, code="session_overlap")


def list_sessions(*, doctor_id: Optional[str] = None, department: Optional[str] = None,
                  include_retired: bool = False) -> list[dict]:
    sql = ("SELECT s.*, u.name AS doctor_name, COALESCE(u.is_active, 1) AS doctor_active "
           "FROM clinic_sessions s LEFT JOIN users u ON u.id = s.doctor_id WHERE 1 = 1")
    params: list[Any] = []
    if not include_retired:
        sql += " AND s.is_active = 1"
    if doctor_id and doctor_id.strip():
        sql += " AND s.doctor_id = ?"
        params.append(doctor_id.strip())
    if department and department.strip():
        sql += " AND LOWER(s.department) = LOWER(?)"
        params.append(department.strip())
    sql += " ORDER BY s.department, s.weekday, s.start_time, u.name, s.id"
    with db_session() as db:
        rows = db.execute(sql, params).fetchall()
    return [_session_dict(r) for r in rows]


def get_session(session_id: str) -> Optional[dict]:
    with db_session() as db:
        row = _session_row(db, session_id)
    return _session_dict(row) if row else None


def create_session(fields: dict, *, actor: dict, request_meta: Optional[dict] = None) -> dict:
    """Add a weekly clinic. Refuses an overlap with the doctor's other clinics."""
    values = _validated_session_fields(fields)
    now = utc_iso_now()
    session_id = _new_id("CS")
    with db_session(immediate=True) as db:
        doctor = _resolve_doctor(db, fields.get("doctor_id"), fields.get("department"))
        _refuse_overlap(db, doctor_id=doctor["id"], doctor_name=doctor["name"], values=values)
        db.execute(
            """INSERT INTO clinic_sessions (id, doctor_id, department, weekday, start_time,
                   end_time, slot_minutes, capacity_per_slot, room, valid_from, valid_to,
                   is_active, notes, row_version, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?)""",
            (session_id, doctor["id"], doctor["department"], values["weekday"],
             values["start_time"], values["end_time"], values["slot_minutes"],
             values["capacity_per_slot"], values["room"], values["valid_from"],
             values["valid_to"], values["notes"], (actor or {}).get("id"), now),
        )
        _audit(db, action="clinic_session_created", actor=actor, target_type="clinic_session",
               target_id=session_id,
               summary=(f"Clinic added for {doctor['name']} ({doctor['department']}), "
                        f"{WEEKDAY_NAMES[values['weekday']]} {values['start_time']} to "
                        f"{values['end_time']}"),
               metadata={"after": {**values, "doctor_id": doctor["id"],
                                   "department": doctor["department"]}},
               request_meta=request_meta)
        row = _session_row(db, session_id)
    return _session_dict(row)


_EDITABLE = ("weekday", "start_time", "end_time", "slot_minutes", "capacity_per_slot", "room",
             "valid_from", "valid_to", "notes")


def update_session(session_id: str, changes: dict, *, expected_row_version: object,
                   actor: dict, request_meta: Optional[dict] = None) -> dict:
    """Change an active session's day, hours, slots, room, dates or notes. The
    doctor and department never change (retire and add a new one instead), and
    the edit must name the row_version it was made against."""
    try:
        expected = int(expected_row_version)
    except (TypeError, ValueError):
        raise ClinicScheduleError("row_version is required", code="row_version_required")
    with db_session(immediate=True) as db:
        row = _session_row(db, session_id)
        if row is None:
            raise ClinicScheduleError("No such clinic", status=404, code="not_found")
        if not row["is_active"]:
            raise ClinicScheduleError("This clinic has been retired", status=409, code="retired")
        if int(row["row_version"]) != expected:
            raise ClinicScheduleError(
                "Someone changed this clinic since you opened it. Reload and try again.",
                status=409, code="stale")
        before = {k: row[k] for k in _EDITABLE}
        merged = dict(before)
        for key in _EDITABLE:
            if key in changes:
                merged[key] = changes[key]
        values = _validated_session_fields(merged)
        if values == before:
            return _session_dict(row)
        _refuse_overlap(db, doctor_id=row["doctor_id"],
                        doctor_name=row["doctor_name"] or row["doctor_id"],
                        values=values, exclude_id=session_id)
        db.execute(
            """UPDATE clinic_sessions
                  SET weekday = ?, start_time = ?, end_time = ?, slot_minutes = ?,
                      capacity_per_slot = ?, room = ?, valid_from = ?, valid_to = ?,
                      notes = ?, row_version = row_version + 1, updated_by = ?,
                      updated_at = ?
                WHERE id = ? AND row_version = ?""",
            (values["weekday"], values["start_time"], values["end_time"],
             values["slot_minutes"], values["capacity_per_slot"], values["room"],
             values["valid_from"], values["valid_to"], values["notes"],
             (actor or {}).get("id"), utc_iso_now(), session_id, expected),
        )
        changed = {k: {"before": before[k], "after": values[k]}
                   for k in _EDITABLE if before[k] != values[k]}
        _audit(db, action="clinic_session_updated", actor=actor, target_type="clinic_session",
               target_id=session_id,
               summary=f"Clinic {session_id} changed: {', '.join(sorted(changed))}",
               metadata={"changes": changed, "row_version": expected + 1},
               request_meta=request_meta)
        row = _session_row(db, session_id)
    return _session_dict(row)


def retire_session(session_id: str, *, reason: object, actor: dict,
                   request_meta: Optional[dict] = None) -> dict:
    """Stop a clinic from offering slots. The row and its history stay."""
    why = _clean(reason, _MAX_REASON_LEN)
    if not why:
        raise ClinicScheduleError("Say why this clinic is being retired", code="reason_required")
    with db_session(immediate=True) as db:
        row = _session_row(db, session_id)
        if row is None:
            raise ClinicScheduleError("No such clinic", status=404, code="not_found")
        if not row["is_active"]:
            raise ClinicScheduleError("This clinic is already retired", status=409, code="retired")
        db.execute(
            "UPDATE clinic_sessions SET is_active = 0, retired_by = ?, retired_at = ?, "
            "retired_reason = ?, row_version = row_version + 1 WHERE id = ?",
            ((actor or {}).get("id"), utc_iso_now(), why, session_id),
        )
        _audit(db, action="clinic_session_retired", actor=actor, target_type="clinic_session",
               target_id=session_id,
               summary=f"Clinic {session_id} retired: {why}",
               metadata={"reason": why, "doctor_id": row["doctor_id"],
                         "department": row["department"]},
               request_meta=request_meta)
        row = _session_row(db, session_id)
    return _session_dict(row)


# ── Exceptions ───────────────────────────────────────────────────────────────

def list_exceptions(*, date_from: object = None, date_to: object = None,
                    doctor_id: Optional[str] = None, include_withdrawn: bool = False) -> list[dict]:
    start = parse_day(date_from, "From") if date_from else hospital_time.hospital_today()
    end = parse_day(date_to, "To") if date_to else start + timedelta(days=60)
    if end < start:
        raise ClinicScheduleError("The end date is before the start date", code="bad_dates")
    if (end - start).days > 366:
        raise ClinicScheduleError("Ask for at most a year of exceptions at once", code="bad_dates")
    sql = ("SELECT e.*, u.name AS doctor_name FROM clinic_session_exceptions e "
           "LEFT JOIN users u ON u.id = e.doctor_id "
           "WHERE e.exception_date BETWEEN ? AND ?")
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if not include_withdrawn:
        sql += " AND e.withdrawn_at IS NULL"
    if doctor_id and doctor_id.strip():
        sql += " AND e.doctor_id = ?"
        params.append(doctor_id.strip())
    sql += " ORDER BY e.exception_date, u.name, e.id"
    with db_session() as db:
        rows = db.execute(sql, params).fetchall()
    return [_exception_dict(r) for r in rows]


def _runs_on(row, day: date) -> bool:
    key = day.isoformat()
    return (int(row["weekday"]) == day.weekday()
            and (row["valid_from"] is None or row["valid_from"] <= key)
            and (row["valid_to"] is None or key <= row["valid_to"]))


def add_exception(fields: dict, *, actor: dict, request_meta: Optional[dict] = None) -> dict:
    """Cancel one session on one day, or (no session named) every session the
    doctor holds that day. A reason is required."""
    day = parse_day(fields.get("exception_date"), "Date")
    reason = _clean(fields.get("reason"), _MAX_REASON_LEN)
    if not reason:
        raise ClinicScheduleError("Say why the clinic is not running", code="reason_required")
    session_id = _clean(fields.get("session_id"), 64)
    exception_id = _new_id("CX")
    with db_session(immediate=True) as db:
        if session_id:
            srow = _session_row(db, session_id)
            if srow is None:
                raise ClinicScheduleError("No such clinic", status=404, code="not_found")
            named = _clean(fields.get("doctor_id"), 64)
            if named and named != srow["doctor_id"]:
                raise ClinicScheduleError(
                    "That clinic belongs to a different doctor", code="doctor_mismatch")
            if not srow["is_active"] or not _runs_on(srow, day):
                raise ClinicScheduleError(
                    f"That clinic does not run on {WEEKDAY_NAMES[day.weekday()]} "
                    f"{day.isoformat()}", code="not_running")
            doctor_id = srow["doctor_id"]
            doctor_name = srow["doctor_name"] or doctor_id
        else:
            did = _clean(fields.get("doctor_id"), 64)
            if not did:
                raise ClinicScheduleError(
                    "Choose the doctor or the clinic that is not running", code="doctor_required")
            drow = db.execute("SELECT id, name FROM users WHERE id = ?", (did,)).fetchone()
            if drow is None:
                raise ClinicScheduleError("No such doctor", status=404, code="not_found")
            doctor_id, doctor_name = drow["id"], drow["name"]
        clash = db.execute(
            """SELECT id FROM clinic_session_exceptions
                WHERE doctor_id = ? AND exception_date = ? AND withdrawn_at IS NULL
                  AND (session_id IS NULL OR (? IS NOT NULL AND session_id = ?))
                LIMIT 1""",
            (doctor_id, day.isoformat(), session_id, session_id),
        ).fetchone()
        if clash is not None:
            raise ClinicScheduleError(
                f"{doctor_name}'s clinic on {day.isoformat()} is already marked as not running",
                status=409, code="already_cancelled")
        db.execute(
            """INSERT INTO clinic_session_exceptions (id, doctor_id, session_id,
                   exception_date, kind, reason, created_by, created_at)
               VALUES (?, ?, ?, ?, 'cancelled', ?, ?, ?)""",
            (exception_id, doctor_id, session_id, day.isoformat(), reason,
             (actor or {}).get("id"), utc_iso_now()),
        )
        _audit(db, action="clinic_session_exception_added", actor=actor,
               target_type="clinic_session_exception", target_id=exception_id,
               summary=(f"Clinic not running on {day.isoformat()} for {doctor_name}"
                        f"{' (one session)' if session_id else ' (whole day)'}: {reason}"),
               metadata={"doctor_id": doctor_id, "session_id": session_id,
                         "exception_date": day.isoformat(), "reason": reason},
               request_meta=request_meta)
        row = db.execute(
            "SELECT e.*, u.name AS doctor_name FROM clinic_session_exceptions e "
            "LEFT JOIN users u ON u.id = e.doctor_id WHERE e.id = ?", (exception_id,)).fetchone()
    return _exception_dict(row)


def withdraw_exception(exception_id: str, *, reason: object, actor: dict,
                       request_meta: Optional[dict] = None) -> dict:
    """The clinic runs after all: the exception is withdrawn, not deleted."""
    why = _clean(reason, _MAX_REASON_LEN)
    if not why:
        raise ClinicScheduleError("Say why the clinic is running after all", code="reason_required")
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM clinic_session_exceptions WHERE id = ?",
                         (exception_id,)).fetchone()
        if row is None:
            raise ClinicScheduleError("No such exception", status=404, code="not_found")
        if row["withdrawn_at"] is not None:
            raise ClinicScheduleError("This exception was already withdrawn", status=409,
                                      code="withdrawn")
        db.execute(
            "UPDATE clinic_session_exceptions SET withdrawn_by = ?, withdrawn_at = ?, "
            "withdrawn_reason = ? WHERE id = ? AND withdrawn_at IS NULL",
            ((actor or {}).get("id"), utc_iso_now(), why, exception_id),
        )
        _audit(db, action="clinic_session_exception_withdrawn", actor=actor,
               target_type="clinic_session_exception", target_id=exception_id,
               summary=f"Clinic exception {exception_id} withdrawn: {why}",
               metadata={"reason": why, "doctor_id": row["doctor_id"],
                         "session_id": row["session_id"],
                         "exception_date": row["exception_date"]},
               request_meta=request_meta)
        row = db.execute(
            "SELECT e.*, u.name AS doctor_name FROM clinic_session_exceptions e "
            "LEFT JOIN users u ON u.id = e.doctor_id WHERE e.id = ?", (exception_id,)).fetchone()
    return _exception_dict(row)


# ── Availability ─────────────────────────────────────────────────────────────

def _slots_for(day: date, row, tz, now_local: datetime) -> list[dict]:
    start = datetime.combine(day, time.fromisoformat(row["start_time"]), tzinfo=tz)
    end = datetime.combine(day, time.fromisoformat(row["end_time"]), tzinfo=tz)
    step = timedelta(minutes=int(row["slot_minutes"]))
    slots = []
    cursor = start
    while cursor + step <= end:
        slot_end = cursor + step
        slots.append({
            "start": cursor.strftime("%H:%M"),
            "end": slot_end.strftime("%H:%M"),
            "starts_at": format_utc(cursor),
            "ends_at": format_utc(slot_end),
            "past": cursor < now_local,
            "capacity": int(row["capacity_per_slot"]),
        })
        cursor = slot_end
    return slots


def availability(*, day: object = None, days: object = 1, doctor_id: Optional[str] = None,
                 department: Optional[str] = None) -> dict:
    """The slots each running clinic offers, day by day, in hospital-local time.

    `day` is a hospital-local calendar day (default: the hospital's today, which
    in IST is still yesterday's UTC date until 05:30). A session runs on a day
    when its weekday matches, the day is inside its validity dates, the session
    is active and its doctor's account is active. An exception for that doctor
    and day (the session, or the whole day) removes every slot and says why.

    Booked counts are not here: appointments do not carry a doctor or a slot
    until opd-16, so these are the slots a clinic offers, not the free ones.
    """
    start = parse_day(day, "Date") if day else hospital_time.hospital_today()
    try:
        span = int(days)
    except (TypeError, ValueError):
        raise ClinicScheduleError("days must be a number", code="bad_days")
    if not 1 <= span <= MAX_AVAILABILITY_DAYS:
        raise ClinicScheduleError(
            f"Ask for between 1 and {MAX_AVAILABILITY_DAYS} days", code="bad_days")
    end = start + timedelta(days=span - 1)
    tz = hospital_time.hospital_timezone()
    now_local = hospital_time.hospital_now()

    sql = ("SELECT s.*, u.name AS doctor_name, COALESCE(u.is_active, 1) AS doctor_active "
           "FROM clinic_sessions s JOIN users u ON u.id = s.doctor_id "
           "WHERE s.is_active = 1 AND COALESCE(u.is_active, 1) = 1")
    params: list[Any] = []
    if doctor_id and doctor_id.strip():
        sql += " AND s.doctor_id = ?"
        params.append(doctor_id.strip())
    if department and department.strip():
        sql += " AND LOWER(s.department) = LOWER(?)"
        params.append(department.strip())
    sql += " ORDER BY s.start_time, u.name, s.id"
    with db_session() as db:
        sessions = db.execute(sql, params).fetchall()
        exceptions = db.execute(
            "SELECT * FROM clinic_session_exceptions WHERE withdrawn_at IS NULL "
            "AND exception_date BETWEEN ? AND ? ORDER BY created_at, id",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    by_day: dict[tuple[str, str], list] = {}
    for exc in exceptions:
        by_day.setdefault((exc["exception_date"], exc["doctor_id"]), []).append(exc)

    out_days = []
    for offset in range(span):
        current = start + timedelta(days=offset)
        key = current.isoformat()
        day_sessions = []
        for row in sessions:
            if not _runs_on(row, current):
                continue
            matching = [e for e in by_day.get((key, row["doctor_id"]), [])
                        if e["session_id"] is None or e["session_id"] == row["id"]]
            cancelled = matching[0] if matching else None
            day_sessions.append({
                "session_id": row["id"],
                "doctor_id": row["doctor_id"],
                "doctor_name": row["doctor_name"],
                "department": row["department"],
                "room": row["room"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "slot_minutes": int(row["slot_minutes"]),
                "capacity_per_slot": int(row["capacity_per_slot"]),
                "cancelled": cancelled is not None,
                "cancel_reason": cancelled["reason"] if cancelled is not None else None,
                "exception_id": cancelled["id"] if cancelled is not None else None,
                "slots": [] if cancelled is not None else _slots_for(current, row, tz, now_local),
            })
        out_days.append({
            "date": key,
            "weekday": current.weekday(),
            "weekday_name": WEEKDAY_NAMES[current.weekday()],
            "sessions": day_sessions,
        })
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "hospital_today": hospital_time.hospital_today().isoformat(),
        "timezone": getattr(tz, "key", None) or str(tz),
        "days": out_days,
    }
