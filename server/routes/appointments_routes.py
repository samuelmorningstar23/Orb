"""Orb — Appointment Booking Routes.

Real appointment persistence for the patient portal ("Schedule Follow-Up") and
clinician-facing views. Backed by the ``appointments`` table (see database.py).

Auth model (mirrors bridge_routes + clinical_records_routes):
  - POST   ""            book: patients book for THEMSELVES (own patient row),
                          stored as a request (origin='patient'), never as agreed;
                          clinicians (patient.write) pass an explicit patient_id.
  - GET    "/mine"       patient portal: own upcoming appointments.
  - GET    "" ?patient_id  clinician list (patient.read_all).
  - DELETE "/{id}"       cancel (status -> 'cancelled'); booking patient or clinician.

Booking is a clinical mutation: it honours an optional Idempotency-Key header
(a double-submitted booking would create a duplicate slot) and is audit-logged.

Every date check reads the HOSPITAL's day and clock (services.hospital_time),
never the server's: at 00:30 IST the UTC date is still yesterday, so a booking
for "today" was refused as past and the patient's "upcoming" list started a
day late (opd-16). Cancel and complete are scoped to the caller's department
like every other staff route here: the appointment's patient must be in the
caller's ward, or a patient with no admission (no active one, in OPD mode)
whose appointment names the caller's clinic (the forecast list's own rule).

OPD MODE (the 'opd' feature, ORB_OPD, off by default; opd-16, W7-A):
  - POST   ""            the staff branch accepts patient.write OR
                          registration.write (the receptionist), scopes the
                          body-named patient through enforce_patient_scope(...,
                          opd_arm=True), and a departmented desk books only into
                          its own clinic. With doctor_id the booking takes a
                          doctor's slot from the clinic sessions (migration 57):
                          a slot that takes N patients holds positions 1 to N,
                          a full slot is 409 slot_full, and the database refuses
                          a second booking of one position (migration 68).
                          An Idempotency-Key replay answers only the booking
                          the request asks for (the same patient, doctor, day,
                          time and clinic); a key that names another booking
                          is 409 idempotency_key_reused, and nothing about that
                          booking is returned.
  - GET    "/day"        the front desk's appointments for one hospital day,
                          with each clinic session's slots and how many are
                          booked; a booking whose session has ended is shown as
                          a no-show (computed on read, never written by a read).
  - POST   "/{id}/check-in"   the patient has arrived: the identity check, then
                          an OPD visit with a token, the appointment linked to it
                          in the visit's own transaction. Idempotent.
  - POST   "/{id}/no-show"    marked by the desk once the slot has started.
  - POST   "/{id}/reschedule" a NEW row pointing at the old one; the old row is
                          kept, cancelled with the reason (a no-show stays one).
Patients' own requests (POST /api/bridge/appointments) are unchanged.

This table is also where a PATIENT'S follow-up request lands (bridge_routes
POST /api/bridge/appointments writes through ``persist_appointment`` with
origin='patient'), because it is the only appointment store any staff surface
reads — services/forecast_service.get_appointments merges its booked rows into
the Appointments panel. See the ORIGIN_* block below.
"""
from __future__ import annotations

import sqlite3
from db_backend import DBIntegrityError
import uuid
from datetime import datetime, date, time as dt_time, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

import config
from auth import get_current_user
from permissions import (
    OPD_CONSULT,
    PATIENT_WRITE,
    REGISTRATION_WRITE,
    department_scope,
    enforce_patient_scope,
    has_permission,
    log_phi_read,
    patient_in_scope,
    require_patient_self,
    require_permission,
    require_registration_desk,
)
from database import db_session
from routes.opt_in_gates import require_opd_feature
from services import hospital_time
from services.audit_service import AuditWriteError, log_audit_event
from services.appointment_types import APPOINTMENT_TYPES, parse_type, type_key, type_label
from services.clinical_time import utc_iso_now

router = APIRouter(prefix="/api/appointments", tags=["appointments"])

# Booking horizon. Without an upper bound a typo'd year ("2206-03-04") books a
# slot 180 years out that never appears in any clinician view and never expires.
MAX_BOOKING_HORIZON_DAYS = 730

# Free-text caps — the columns are unbounded TEXT, so an unchecked body could
# park megabytes of PHI-shaped text in the appointments table.
_MAX_TYPE_LEN = 120
_MAX_REASON_LEN = 2000

# appointments.status CHECK constraint (database.py SCHEMA, widened by
# migration 68 with 'checked_in' and 'no_show').
_TERMINAL_STATUSES = {"cancelled", "completed"}

#: Statuses that hold a doctor's slot: everything but a cancellation (the
#: partial unique index idx_appointments_doctor_slot uses the same rule).
_LIVE_STATUSES = ("booked", "checked_in", "completed", "no_show")

#: The visit type an OPD visit opened by check-in carries, by appointment type
#: (services.appointment_types). It matters only to a charge rule written for
#: one visit type (opd-13); anything else is a new visit.
_VISIT_TYPE_FOR = {
    "follow_up": "follow_up",
    "medication_review": "review",
    "post_discharge_check": "review",
}

_MAX_CANCEL_REASON_LEN = 500

# ── Where a patient's follow-up REQUEST lands ────────────────────────────────
# A patient asking for a follow-up used to write to `patient_appointment_requests`,
# a table no staff route, service or screen has ever read (pilot dry run,
# 2026-08-14) — the API answered "the care team will confirm a time" and nobody
# was ever told. There is exactly one appointment surface staff actually look at:
# the Appointments panel, fed by services/forecast_service.get_appointments,
# which reads THIS table for rows with status='booked'. So the request is written
# here, through the same door — with `origin='patient'` (migration 28) so both
# sides can tell an ask from an agreed slot, and a type that says out loud that
# the time still needs confirming.
ORIGIN_STAFF = "staff"
ORIGIN_PATIENT = "patient"
PATIENT_REQUEST_TYPE = "Patient request: confirm time"

# A patient may ask for a DATE without naming a time; `scheduled_time` is NOT
# NULL and a schedule row with no time sorts and prints as a blank. Rather than
# invent a plausible-looking slot silently, the request is listed at the first
# clinic slot and BOTH sides are told that is what happened: the staff row's
# reason says the patient chose no time, and the patient's confirmation says the
# same. Neither side is shown a time anyone agreed to.
FIRST_CLINIC_SLOT = "09:00"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row_get(row, key, default=None):
    """Read a column that may not exist on this row.

    `origin` / `department` arrive with migration 28; a row built by an older
    fixture (or read back before the migration runs) simply has no such key, and
    sqlite3.Row raises IndexError rather than returning None for a miss.
    """
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value

def _resolve_own_patient_id(user_id: str) -> str:
    """Resolve the caller's OWN patient record id from their login id.

    Mirrors bridge_routes._resolve_patient_id — a patient can only ever act on
    their own patient_id; the row is looked up by users.id, never trusted from
    the request body.
    """
    with db_session() as db:
        row = db.execute(
            "SELECT id FROM patients WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No patient record found for your account")
    return row["id"]


def _serialize(row: sqlite3.Row) -> dict:
    """Serialize an appointments row to the API shape."""
    return {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "booked_by_user_id": row["booked_by_user_id"],
        "scheduled_date": row["scheduled_date"],
        "scheduled_time": row["scheduled_time"],
        "appointment_type": row["appointment_type"],
        # One of services.appointment_types.APPOINTMENT_TYPES. An old free-text
        # type reads as "other" here, and its own text stays in the label.
        "appointment_type_key": type_key(row["appointment_type"]),
        "appointment_type_label": type_label(row["appointment_type"]),
        "reason": row["reason"],
        "status": row["status"],
        "created_at": row["created_at"],
        # Who put this row on the list. 'patient' means the patient asked for it
        # and no one has agreed the slot with them — the clinician list and the
        # patient's own view both key their wording off this.
        "origin": _row_get(row, "origin", ORIGIN_STAFF),
        "department": _row_get(row, "department"),
        # OPD mode (opd-16): the doctor, the slot and what happened to the
        # booking. With the feature off the shape is exactly the old one.
        **(_slot_fields(row) if _opd_mode() else {}),
    }


def _opd_mode() -> bool:
    """Is the 'opd' feature on right now? Read at call time."""
    return config.feature_enabled("opd")


def _slot_fields(row) -> dict:
    """Migration 68's columns, as the API returns them in OPD mode."""
    return {
        "doctor_id": _row_get(row, "doctor_id"),
        "session_id": _row_get(row, "session_id"),
        "slot_position": _row_get(row, "slot_position"),
        "checked_in_at": _row_get(row, "checked_in_at"),
        "opd_visit_id": _row_get(row, "opd_visit_id"),
        "no_show_at": _row_get(row, "no_show_at"),
        "cancel_reason": _row_get(row, "cancel_reason"),
        "cancelled_at": _row_get(row, "cancelled_at"),
        "completed_at": _row_get(row, "completed_at"),
        "rescheduled_from_id": _row_get(row, "rescheduled_from_id"),
    }


def _validate_slot(scheduled_date: str, scheduled_time: str) -> None:
    """Validate YYYY-MM-DD / HH:MM formats and reject bookings in the past.

    "Past" is the HOSPITAL's day and clock (services.hospital_time), never the
    server's date.today()/datetime.now(): at 00:30 IST the UTC date is still
    yesterday, and the server's local zone is whatever the appliance was set
    up with."""
    try:
        parsed_date = datetime.strptime(scheduled_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="scheduled_date must be YYYY-MM-DD")
    try:
        parsed_time = datetime.strptime(scheduled_time, "%H:%M").time()
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="scheduled_time must be HH:MM (24h)")
    now = hospital_time.hospital_now()
    today = now.date()
    if parsed_date < today:
        raise HTTPException(status_code=422, detail="Cannot book an appointment in the past")
    # Only the DATE was checked, so a slot earlier TODAY booked cleanly — the
    # patient portal would happily accept "today at 08:00" at 4pm and then show
    # it under "upcoming" until midnight.
    if parsed_date == today and parsed_time < now.time().replace(tzinfo=None):
        raise HTTPException(
            status_code=422,
            detail="Cannot book an appointment earlier today. That time has passed",
        )
    if parsed_date > today + timedelta(days=MAX_BOOKING_HORIZON_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot book more than {MAX_BOOKING_HORIZON_DAYS} days ahead",
        )


def _validate_date_only(scheduled_date: str) -> date:
    """Date half of _validate_slot, for a request that names no time. The
    hospital's day, as above."""
    try:
        parsed_date = datetime.strptime(scheduled_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="scheduled_date must be YYYY-MM-DD")
    today = hospital_time.hospital_today()
    if parsed_date < today:
        raise HTTPException(status_code=422, detail="Cannot book an appointment in the past")
    if parsed_date > today + timedelta(days=MAX_BOOKING_HORIZON_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot book more than {MAX_BOOKING_HORIZON_DAYS} days ahead",
        )
    return parsed_date


def default_slot_for(parsed_date: date) -> str | None:
    """The slot used when a patient names a date but no time.

    A future date gets the first clinic slot. TODAY gets the next half hour on
    the clock instead — the first slot has usually already passed, and a row
    listed in the past is a row the day list has already scrolled past. Returns
    None when nothing is left today (asked for after 23:30), which the caller
    turns into a plain refusal rather than a slot nobody will keep.

    Derived from `now`, so it is never in the past by construction — callers
    must NOT then push it back through _validate_slot's earlier-today check,
    which would be a clock race for a value we just computed.

    `now` and today are the hospital's (services.hospital_time), not the
    server's.
    """
    now = hospital_time.hospital_now()
    today = now.date()
    if parsed_date > today:
        return FIRST_CLINIC_SLOT
    if parsed_date < today:
        return None
    if now.strftime("%H:%M") < FIRST_CLINIC_SLOT:
        return FIRST_CLINIC_SLOT
    hour, minute = (now.hour, 30) if now.minute < 30 else (now.hour + 1, 0)
    if hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def persist_appointment(
    *,
    patient_id: str,
    booked_by_user_id: str,
    scheduled_date: str,
    scheduled_time: str,
    appointment_type: str,
    reason: str | None,
    department: str | None = None,
    origin: str = ORIGIN_STAFF,
    idem: str | None = None,
    bind_replay: bool = False,
) -> tuple[dict, bool]:
    """Write one appointment row and return (serialized, created).

    THE one writer for this table, shared by the clinician/patient booking route
    below and by the patient's follow-up REQUEST in bridge_routes — so a request
    cannot drift into a second table that no staff surface reads (which is
    exactly how `patient_appointment_requests` became a dead letter box).

    `created` is False when an existing row satisfied the caller: an
    Idempotency-Key replay, or the same patient/date/time already live.

    `bind_replay` (OPD mode's booking with no doctor, opd-16): a replay answers
    only the booking this request asks for (_replay_is_this_booking); a key
    that names any other booking raises _ReplayConflict before that row is
    read into an answer. Without it a replay returns the row as it always did.

    BLOCKING. Opens its own session, so async callers must go through run_db().
    """

    def _replayed(row) -> tuple[dict, bool]:
        if bind_replay and not _replay_is_this_booking(
                row, patient_id=patient_id, scheduled_date=scheduled_date,
                scheduled_time=scheduled_time, doctor_id=None,
                department=department, origin=origin):
            raise _ReplayConflict(row["id"])
        return _serialize(row), False

    with db_session() as db:
        # Confirm the target patient exists (FK is enforced, but a 404 is clearer
        # than a raw IntegrityError for clinician-supplied ids).
        patient = db.execute(
            "SELECT id FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        # Idempotency: a replay with the same key returns the original booking
        # instead of creating a duplicate slot.
        if idem:
            existing = db.execute(
                "SELECT * FROM appointments WHERE idempotency_key = ?", (idem,)
            ).fetchone()
            if existing:
                return _replayed(existing)

        # Duplicate-slot guard. The Idempotency-Key only dedupes a REPLAY of one
        # request; a patient who taps "Schedule Follow-Up" twice (or two staff
        # booking the same slot) sends two distinct requests and used to create
        # two live appointments for the same patient at the same minute. The
        # existing booking is returned rather than 409'd — the caller's intent is
        # already satisfied, and a 409 would send the portal into an error state
        # for something that is not an error.
        duplicate = db.execute(
            """SELECT * FROM appointments
               WHERE patient_id = ? AND scheduled_date = ? AND scheduled_time = ?
                 AND status = 'booked'""",
            (patient_id, scheduled_date, scheduled_time),
        ).fetchone()
        if duplicate:
            return _serialize(duplicate), False

        appt_id = str(uuid.uuid4())
        try:
            db.execute(
                """INSERT INTO appointments
                       (id, patient_id, booked_by_user_id, scheduled_date, scheduled_time,
                        appointment_type, reason, status, idempotency_key, origin, department)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'booked', ?, ?, ?)""",
                (appt_id, patient_id, booked_by_user_id, scheduled_date, scheduled_time,
                 appointment_type, reason, idem, origin, department),
            )
        except DBIntegrityError:
            # Concurrent replay won the unique-idempotency-key race — return theirs.
            if idem:
                dup = db.execute(
                    "SELECT * FROM appointments WHERE idempotency_key = ?", (idem,)
                ).fetchone()
                if dup:
                    return _replayed(dup)
            raise
        row = db.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
        return _serialize(row), True


# ── Scope: which appointments a staff caller may see and act on ─────────────

def _has_admission(db, patient_id: str, *, active_only: bool) -> bool:
    sql = "SELECT 1 FROM admissions WHERE patient_id = ?"
    if active_only:
        sql += " AND status = 'active'"
    return db.execute(sql + " LIMIT 1", (patient_id,)).fetchone() is not None


def _appointment_visible(db, row, dept: str | None) -> bool:
    """May a caller scoped to `dept` (permissions.department_scope) see and act
    on this appointment? The rule the Appointments list reads by
    (services.forecast_service.get_appointments):
      * a caller with no department sees every appointment;
      * the patient is in the caller's ward (permissions.patient_in_scope);
      * or the appointment names the caller's clinic and no ward owns the
        patient: no admission at all, or, in OPD mode, no ACTIVE admission
        (frontdesk_service.opd_registration_allowed draws the same line).
    Cancel and complete went by none of this before opd-16: a ward doctor
    could cancel another ward's patient's appointment by its id."""
    if dept is None:
        return True
    patient_id = row["patient_id"]
    if patient_in_scope(db, patient_id, dept):
        return True
    clinic = (_row_get(row, "department") or "").strip().casefold()
    if not clinic or clinic != dept.strip().casefold():
        return False
    return not _has_admission(db, patient_id, active_only=_opd_mode())


def _appointment_or_404(db, appointment_id: str, dept: str | None):
    """The row, or 404 for no such appointment AND for one outside the
    caller's scope (404, never 403: an id confirms a booking exists)."""
    row = db.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    if row is None or not _appointment_visible(db, row, dept):
        raise HTTPException(status_code=404, detail="Appointment not found")
    return row


def _may_book_for_patients(role: str) -> bool:
    """patient.write (the clinician), or in OPD mode registration.write (the
    receptionist, ruling OPD-N3)."""
    if has_permission(role, PATIENT_WRITE):
        return True
    return _opd_mode() and has_permission(role, REGISTRATION_WRITE)


def _require_booking_staff(request: Request, user: dict) -> None:
    """403, audited, unless the caller may book for patients (OPD mode)."""
    role = user.get("role", "")
    if _may_book_for_patients(role):
        return
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="permission_denied",
        outcome="denied",
        summary_message="Managing appointments requires patient.write or registration.write",
        actor_user_id=user.get("id"),
        actor_role=role,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={"required_permissions": [PATIENT_WRITE, REGISTRATION_WRITE]},
    )
    raise HTTPException(
        status_code=403,
        detail={"error": "permission_denied",
                "message": "Not permitted to manage appointments"},
    )


def _conflict(code: str, message: str, status: int = 409, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message, **extra})


def _audit_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={
        "error": "audit_unavailable",
        "message": ("The audit trail could not be written, so nothing was saved or shown. "
                    "Try again in a moment."),
    })


def _same_clinic(a: str | None, b: str | None) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


# ── OPD mode: a replay answers only its own booking (opd-16) ────────────────

class _ReplayConflict(HTTPException):
    """409 idempotency_key_reused: this request's Idempotency-Key already names
    a different booking (another patient, doctor, day, time or clinic).

    Raised before that booking is read into any answer, so nothing about it
    reaches the caller. `appointment_id` is for the refusal's audit row only,
    never for the response: the front desk's OpdReplayConflict and
    _reschedule_replay below keep the same rule."""

    def __init__(self, appointment_id: str):
        super().__init__(status_code=409, detail={
            "error": "idempotency_key_reused",
            "message": ("This request was already used for a different booking. "
                        "Nothing was saved. Close this and book again."),
        })
        self.appointment_id = str(appointment_id)


def _replay_is_this_booking(row, *, patient_id: str, scheduled_date: str,
                            scheduled_time: str, doctor_id: str | None,
                            department: str | None, origin: str = ORIGIN_STAFF,
                            rescheduled_from_id: str | None = None) -> bool:
    """Is the booking an Idempotency-Key names the one this request asks for?
    The same patient, doctor (or none), day, time, clinic, origin and
    predecessor.

    The key is global (one unique index over the table) and the scope check
    runs on the patient the BODY names, not on the row the key names. Without
    this, a key reused for another patient answered that patient's booking,
    with their name and hospital number, to a desk that is refused them by
    name (probed in review, 9 October 2026)."""
    return (
        row["patient_id"] == patient_id
        and _row_get(row, "doctor_id") == doctor_id
        and row["scheduled_date"] == scheduled_date
        and row["scheduled_time"] == scheduled_time
        and _same_clinic(_row_get(row, "department"), department)
        and _row_get(row, "origin", ORIGIN_STAFF) == origin
        and _row_get(row, "rescheduled_from_id") == rescheduled_from_id
    )


def _audit_replay_refused(request: Request, user: dict, *, patient_id: str,
                          err: _ReplayConflict, requested: dict) -> None:
    """A booking refused because its Idempotency-Key names another booking,
    written after the rollback (the refusal saved nothing). The footing of the
    front desk's opd_visit_replay_refused ('authorization', denied), so a key
    replayed for another patient is visible in the trail although the caller
    learns nothing about that booking."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="appointment_replay_refused",
        outcome="denied",
        summary_message=(
            f"Appointment booking for {patient_id} refused: its idempotency key already "
            f"names appointment {err.appointment_id}, which is a different booking"),
        actor_user_id=user.get("id"),
        actor_role=user.get("role", ""),
        patient_id=patient_id,
        target_type="appointment",
        target_id=err.appointment_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={"reason": "idempotency_key_names_another_booking", **requested},
    )


# ── OPD mode: a doctor's slot (opd-16) ──────────────────────────────────────

def _slot_offer(doctor_id: str, scheduled_date: str, scheduled_time: str) -> dict:
    """The clinic session and slot this doctor offers at this hospital-local
    day and time (services.clinic_schedule_service.availability, computed in
    hospital time, never stored). Refusals: 409 clinic_not_running (an
    exception cancels it that day), 409 no_such_slot, 422 slot_passed."""
    from services import clinic_schedule_service as schedule

    try:
        offer = schedule.availability(day=scheduled_date, days=1, doctor_id=doctor_id)
    except schedule.ClinicScheduleError as err:
        raise _conflict(err.code, str(err), status=err.status)
    for day in offer["days"]:
        for session in day["sessions"]:
            if session["cancelled"] and session["start_time"] <= scheduled_time < session["end_time"]:
                raise _conflict(
                    "clinic_not_running",
                    "This clinic does not run that day"
                    + (f": {session['cancel_reason']}" if session.get("cancel_reason") else "."))
            for slot in session["slots"]:
                if slot["start"] != scheduled_time:
                    continue
                if slot["past"]:
                    raise _conflict("slot_passed", "That slot has already started.", status=422)
                return {"session": session, "slot": slot}
    raise _conflict(
        "no_such_slot",
        "That doctor has no clinic slot at that time. Choose a slot from the clinic's timings.")


def _book_slot_on(db, *, patient_id: str, booked_by: str, doctor_id: str,
                  scheduled_date: str, scheduled_time: str, appointment_type: str,
                  reason: str | None, offer: dict, idem: str | None,
                  rescheduled_from_id: str | None = None):
    """Write one booking into a doctor's slot on the caller's (immediate)
    transaction. Returns (row, created).

    A slot that takes N patients holds positions 1 to N; the lowest free one is
    taken, and a full slot is 409 slot_full. The database refuses a second
    live booking of one position (idx_appointments_doctor_slot, migration 68),
    so two desks booking the last place at once get one booking and one 409.
    The same patient already booked at that time with this doctor is the
    booking returned (a double tap); with another doctor it is 409
    patient_already_booked. An Idempotency-Key replay answers only this
    request's booking: a key that names any other booking is
    _ReplayConflict (409 idempotency_key_reused), before that row is read."""
    session = offer["session"]
    capacity = int(offer["slot"]["capacity"] or 1)
    if idem:
        existing = db.execute(
            "SELECT * FROM appointments WHERE idempotency_key = ?", (idem,)).fetchone()
        if existing is not None:
            if not _replay_is_this_booking(
                    existing, patient_id=patient_id, scheduled_date=scheduled_date,
                    scheduled_time=scheduled_time, doctor_id=doctor_id,
                    department=session["department"],
                    rescheduled_from_id=rescheduled_from_id):
                raise _ReplayConflict(existing["id"])
            return existing, False
    duplicate = db.execute(
        """SELECT * FROM appointments
            WHERE patient_id = ? AND scheduled_date = ? AND scheduled_time = ?
              AND status IN ('booked', 'checked_in')""",
        (patient_id, scheduled_date, scheduled_time),
    ).fetchone()
    if duplicate is not None:
        if _row_get(duplicate, "doctor_id") == doctor_id:
            return duplicate, False
        raise _conflict("patient_already_booked",
                        "This patient already has an appointment at that time.")
    used = {
        int(r["slot_position"]) for r in db.execute(
            "SELECT slot_position FROM appointments WHERE doctor_id = ? AND scheduled_date = ? "
            "AND scheduled_time = ? AND status != 'cancelled' AND slot_position IS NOT NULL",
            (doctor_id, scheduled_date, scheduled_time),
        ).fetchall()
    }
    position = next((n for n in range(1, capacity + 1) if n not in used), None)
    if position is None:
        raise _conflict("slot_full", "That slot is fully booked. Choose another.",
                        capacity=capacity)
    appt_id = str(uuid.uuid4())
    try:
        db.execute(
            """INSERT INTO appointments
                   (id, patient_id, booked_by_user_id, scheduled_date, scheduled_time,
                    appointment_type, reason, status, idempotency_key, origin, department,
                    doctor_id, session_id, slot_position, rescheduled_from_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'booked', ?, ?, ?, ?, ?, ?, ?)""",
            (appt_id, patient_id, booked_by, scheduled_date, scheduled_time,
             appointment_type, reason, idem, ORIGIN_STAFF, session["department"],
             doctor_id, session["session_id"], position, rescheduled_from_id),
        )
    except DBIntegrityError:
        raise _conflict("slot_full", "That slot was just booked. Choose another.",
                        capacity=capacity)
    return db.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone(), True


def _audit_appointment(db, request: Request, user: dict, *, action: str, row,
                       summary: str, metadata: dict | None = None) -> None:
    """One clinical_record row for an appointment change, in its transaction."""
    log_audit_event(
        correlation_id=f"appointment-{action.removeprefix('appointment_')}-{row['id']}",
        event_category="clinical_record",
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role", ""),
        patient_id=row["patient_id"],
        target_type="appointment",
        target_id=str(row["id"]),
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={**(metadata or {}), "written": "in_transaction"},
    )


def _staff_view(db, rows) -> list[dict]:
    """Appointments as the desk reads them (OPD mode): the API shape plus the
    patient's name and hospital number, the doctor's name and the token of the
    visit check-in opened."""
    out = []
    for row in rows:
        data = _serialize(row)
        patient = db.execute(
            "SELECT name, mrn, age, sex FROM patients WHERE id = ?", (row["patient_id"],)
        ).fetchone()
        data["patient_name"] = patient["name"] if patient else None
        data["patient_uhid"] = patient["mrn"] if patient else None
        data["patient_age"] = patient["age"] if patient else None
        data["patient_sex"] = patient["sex"] if patient else None
        data["inpatient_now"] = _has_admission(db, row["patient_id"], active_only=True)
        doctor_id = _row_get(row, "doctor_id")
        doctor = db.execute("SELECT name FROM users WHERE id = ?", (doctor_id,)).fetchone() \
            if doctor_id else None
        data["doctor_name"] = doctor["name"] if doctor else None
        visit_id = _row_get(row, "opd_visit_id")
        visit = db.execute(
            "SELECT token_number, status FROM opd_visits WHERE id = ?", (visit_id,)
        ).fetchone() if visit_id else None
        data["token_number"] = visit["token_number"] if visit else None
        data["visit_status"] = visit["status"] if visit else None
        out.append(data)
    return out


def _session_end(db, row, cache: dict) -> datetime | None:
    """When the clinic session this booking sits in ends, hospital-local and
    aware; None when the booking has no session (or it is gone)."""
    session_id = _row_get(row, "session_id")
    if not session_id:
        return None
    if session_id not in cache:
        found = db.execute("SELECT end_time FROM clinic_sessions WHERE id = ?",
                           (session_id,)).fetchone()
        cache[session_id] = found["end_time"] if found else None
    end_time = cache[session_id]
    if not end_time:
        return None
    try:
        day = date.fromisoformat(str(row["scheduled_date"]))
        return datetime.combine(day, dt_time.fromisoformat(end_time),
                                tzinfo=hospital_time.hospital_timezone())
    except ValueError:
        return None


def _effective_status(db, row, now: datetime, cache: dict) -> tuple[str, bool]:
    """(status as the desk should read it, computed). A booking nobody checked
    in is a no-show once its clinic session has ended (a booking with no
    session: once its day has ended). Computed on read and never written:
    a read does not change the record, and the desk marks a no-show itself."""
    status = row["status"]
    if status != "booked":
        return status, False
    end = _session_end(db, row, cache)
    if end is not None:
        return ("no_show", True) if now >= end else (status, False)
    try:
        day = date.fromisoformat(str(row["scheduled_date"]))
    except ValueError:
        return status, False
    return ("no_show", True) if day < now.date() else (status, False)


def _slot_started(row, now: datetime) -> bool:
    try:
        start = datetime.combine(date.fromisoformat(str(row["scheduled_date"])),
                                 dt_time.fromisoformat(str(row["scheduled_time"])),
                                 tzinfo=hospital_time.hospital_timezone())
    except ValueError:
        return False
    return now >= start


# ── Book ─────────────────────────────────────────────────────────────────────

class BookAppointmentRequest(BaseModel):
    """The booking body, TYPED — and typing it is a security decision here.

    This route used to take ``data: dict``. That is not a style preference: the
    caller-named-patient drift guard (tests/test_caller_named_patient_scope.py)
    finds a patient by INSPECTING THE SIGNATURE — a parameter named
    ``patient_id``, or a Pydantic body model with a ``patient_id`` field — so a
    route that spells its body ``dict`` is INVISIBLE to it. Its own docstring
    names this endpoint as the worked example of that blind spot, and the
    cross-ward booking below was probed live on 2026-08-07 while the guard ran
    green. An untyped body is therefore a route that has opted out of review.

    Every field is Optional so the handler keeps emitting its OWN 422s ("
    scheduled_date must be YYYY-MM-DD", "patient_id is required") rather than
    FastAPI's list-shaped validation error — the existing clients and tests read
    `detail` as a string.
    """
    patient_id: str | None = None
    scheduled_date: str | None = None
    scheduled_time: str | None = None
    appointment_type: str | None = None
    reason: str | None = None
    # OPD mode only (opd-16), ignored with the feature off exactly as an
    # unknown field always was: the doctor whose clinic slot this takes, and
    # the clinic of a booking with no doctor.
    doctor_id: str | None = None
    department: str | None = None


@router.post("")
def book_appointment(
    data: BookAppointmentRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict = Depends(get_current_user),
):
    """Book an appointment.

    Patients book for themselves (patient_id resolved from their login, any
    patient_id in the body is ignored). Clinicians with ``patient.write`` book
    on behalf of a patient named in the body — WITHIN THEIR OWN WARD.

    OPD mode (opd-16): the staff branch also admits registration.write (the
    receptionist), scopes the patient through the outpatient arm, keeps a
    departmented desk inside its own clinic, and with doctor_id takes a slot
    from that doctor's clinic sessions (see _book_slot_on).
    """
    role = user.get("role", "")

    if role == "patient":
        patient_id = _resolve_own_patient_id(user["id"])
    elif _opd_mode() and _may_book_for_patients(role):
        patient_id = str(data.patient_id or "").strip()
        if not patient_id:
            raise HTTPException(status_code=422, detail="patient_id is required")
        # THE OUTPATIENT ARM (opd-16), on the whitelist in
        # tests/test_opd_scope_drift.py: the ordinary ward scope, OR no active
        # admission anywhere and a visit in the caller's clinic within
        # ORB_OPD_SCOPE_DAYS. 404 otherwise, never 403. Only the booking; the
        # chart stays admission-only.
        enforce_patient_scope(user, patient_id, opd_arm=True)
        return _book_as_staff_opd(data, request, idempotency_key, user, patient_id)
    elif has_permission(role, PATIENT_WRITE):
        patient_id = str(data.patient_id or "").strip()
        if not patient_id:
            raise HTTPException(status_code=422, detail="patient_id is required")
        # SCOPING LAW — the patient is named in the BODY (2026-08-14).
        # PROBED CROSS-WARD on 2026-08-07: a Cardiology doctor booked an
        # appointment for an Oncology patient, HTTP 200, row persisted. The
        # LIST route thirty lines below has carried enforce_patient_scope since
        # the 2026-08-05 read sweep; the WRITE route beside it never got it,
        # because that sweep was looking for reads.
        #
        # Applied only on this branch, and that is deliberate: on the `patient`
        # branch the id was derived from the session, so re-checking it against
        # a department would risk 404ing a patient out of their own booking if
        # their user row ever carried a department. Same 404-never-403 rule as
        # everywhere else — a 403 would confirm the patient exists.
        enforce_patient_scope(user, patient_id)
    else:
        raise HTTPException(
            status_code=403,
            detail={"error": "permission_denied", "message": "Not permitted to book appointments"},
        )

    scheduled_date = str(data.scheduled_date or "").strip()
    scheduled_time = str(data.scheduled_time or "").strip()
    _validate_slot(scheduled_date, scheduled_time)

    appointment_type = str(data.appointment_type or "Follow-up").strip() or "Follow-up"
    if len(appointment_type) > _MAX_TYPE_LEN:
        raise HTTPException(status_code=422,
                            detail=f"appointment_type exceeds {_MAX_TYPE_LEN} characters")
    # A clinician books one of the fixed types (services/appointment_types.py).
    # Free text made the list impossible to count by type. The stored value is
    # the label, the spelling the column has always defaulted to. A patient's
    # own wording is not refused: their branch below files it in the reason.
    if role != "patient":
        key = parse_type(data.appointment_type)
        if key is None:
            raise HTTPException(
                status_code=422,
                detail="appointment_type must be one of: " + ", ".join(APPOINTMENT_TYPES),
            )
        appointment_type = APPOINTMENT_TYPES[key]
    reason = data.reason
    if reason is not None:
        reason = str(reason).strip() or None
    if reason and len(reason) > _MAX_REASON_LEN:
        raise HTTPException(status_code=422,
                            detail=f"reason exceeds {_MAX_REASON_LEN} characters")

    idem = idempotency_key.strip() if idempotency_key else None

    # A patient cannot agree a slot with themselves, so whatever a patient posts
    # here is a REQUEST, stored the way POST /api/bridge/appointments stores one.
    # It was saved as origin='staff', so the staff list and the patient's own
    # view both showed an ask as a booking the care team had made.
    origin = ORIGIN_STAFF
    if role == "patient":
        origin = ORIGIN_PATIENT
        asked = ["Patient-requested follow-up."]
        if data.appointment_type and appointment_type != "Follow-up":
            asked.append(f"Asked for: {appointment_type}.")
        if reason:
            asked.append(reason)
        appointment_type = PATIENT_REQUEST_TYPE
        reason = " ".join(asked)[:_MAX_REASON_LEN]

    appointment, created = persist_appointment(
        patient_id=patient_id,
        booked_by_user_id=user["id"],
        scheduled_date=scheduled_date,
        scheduled_time=scheduled_time,
        appointment_type=appointment_type,
        reason=reason,
        origin=origin,
        idem=idem,
    )
    # An idempotency replay or a duplicate slot resolved to an EXISTING row —
    # nothing was written, so nothing is audited (a second "appointment_booked"
    # entry for one booking would overstate the record).
    if not created:
        return appointment

    log_audit_event(
        correlation_id=f"appointment-book-{appointment['id']}",
        event_category="clinical_record",
        action_type="appointment_booked",
        outcome="success",
        summary_message=f"Appointment booked for {patient_id} on {scheduled_date} {scheduled_time}",
        actor_user_id=user["id"],
        actor_role=role,
        patient_id=patient_id,
        target_type="appointment",
        target_id=appointment["id"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={"appointment_type": appointment_type, "origin": origin},
    )

    return appointment


def _staff_type_and_reason(data: BookAppointmentRequest) -> tuple[str, str | None]:
    """The staff booking's type (one of the fixed four) and reason, with the
    same 422s the route has always given."""
    raw_type = str(data.appointment_type or "Follow-up").strip() or "Follow-up"
    if len(raw_type) > _MAX_TYPE_LEN:
        raise HTTPException(status_code=422,
                            detail=f"appointment_type exceeds {_MAX_TYPE_LEN} characters")
    key = parse_type(data.appointment_type)
    if key is None:
        raise HTTPException(
            status_code=422,
            detail="appointment_type must be one of: " + ", ".join(APPOINTMENT_TYPES),
        )
    reason = data.reason
    if reason is not None:
        reason = str(reason).strip() or None
    if reason and len(reason) > _MAX_REASON_LEN:
        raise HTTPException(status_code=422,
                            detail=f"reason exceeds {_MAX_REASON_LEN} characters")
    return APPOINTMENT_TYPES[key], reason


def _book_as_staff_opd(data: BookAppointmentRequest, request: Request,
                       idempotency_key: str | None, user: dict, patient_id: str) -> dict:
    """The staff booking in OPD mode, after the endpoint scoped the patient.

    The clinic: with doctor_id it is the doctor's clinic session's department;
    without, the body's department or the caller's own. A caller with a
    department books only into it (403 department_scope, the front desk's own
    refusal). With doctor_id the time must be one of that doctor's slots.

    A replay (the same Idempotency-Key) answers only the booking this request
    asks for, and a key that names another booking is 409
    idempotency_key_reused with an 'appointment_replay_refused' row. A slot
    booking answered from an existing row (a replay or a double tap) carries
    the patient's name and hospital number again, so it writes its own
    'appointment_booking_repeated' row in the transaction."""
    dept = department_scope(user)
    scheduled_date = str(data.scheduled_date or "").strip()
    scheduled_time = str(data.scheduled_time or "").strip()
    _validate_slot(scheduled_date, scheduled_time)
    appointment_type, reason = _staff_type_and_reason(data)
    idem = idempotency_key.strip() if idempotency_key else None
    doctor_id = str(data.doctor_id or "").strip() or None

    if doctor_id is None:
        department = str(data.department or "").strip() or dept
        if dept is not None and not _same_clinic(department, dept):
            raise _conflict("department_scope", f"You can only book appointments in {dept}",
                            status=403)
        try:
            appointment, created = persist_appointment(
                patient_id=patient_id, booked_by_user_id=user["id"],
                scheduled_date=scheduled_date, scheduled_time=scheduled_time,
                appointment_type=appointment_type, reason=reason,
                department=department, origin=ORIGIN_STAFF, idem=idem, bind_replay=True,
            )
        except _ReplayConflict as err:
            _audit_replay_refused(request, user, patient_id=patient_id, err=err, requested={
                "requested_doctor_id": None, "requested_date": scheduled_date,
                "requested_time": scheduled_time, "requested_department": department})
            raise
        if created:
            log_audit_event(
                correlation_id=f"appointment-book-{appointment['id']}",
                event_category="clinical_record",
                action_type="appointment_booked",
                outcome="success",
                summary_message=(f"Appointment booked for {patient_id} on "
                                 f"{scheduled_date} {scheduled_time}"),
                actor_user_id=user["id"],
                actor_role=user.get("role", ""),
                patient_id=patient_id,
                target_type="appointment",
                target_id=appointment["id"],
                request_path=request.url.path,
                http_method=request.method,
                ip_address=request.client.host if request.client else None,
                metadata={"appointment_type": appointment_type, "origin": ORIGIN_STAFF,
                          "department": department, "opd_mode": True},
            )
        return appointment

    offer = _slot_offer(doctor_id, scheduled_date, scheduled_time)
    session = offer["session"]
    if dept is not None and not _same_clinic(session["department"], dept):
        raise _conflict("department_scope",
                        f"You can only book appointments in {dept}", status=403)
    try:
        with db_session(immediate=True) as db:
            if db.execute("SELECT 1 FROM patients WHERE id = ?",
                          (patient_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="Patient not found")
            row, created = _book_slot_on(
                db, patient_id=patient_id, booked_by=user["id"], doctor_id=doctor_id,
                scheduled_date=scheduled_date, scheduled_time=scheduled_time,
                appointment_type=appointment_type, reason=reason, offer=offer, idem=idem)
            if created:
                _audit_appointment(
                    db, request, user, action="appointment_booked", row=row,
                    summary=(f"Appointment booked for {patient_id} with {doctor_id} on "
                             f"{scheduled_date} {scheduled_time}"),
                    metadata={"appointment_type": appointment_type, "origin": ORIGIN_STAFF,
                              "doctor_id": doctor_id, "session_id": session["session_id"],
                              "slot_position": row["slot_position"],
                              "department": session["department"]})
            else:
                # Nothing new was saved, but the answer names the patient
                # again: its own action, so a count of bookings stays true.
                matched_by = ("idempotency_key"
                              if idem and _row_get(row, "idempotency_key") == idem
                              else "same_patient_and_slot")
                _audit_appointment(
                    db, request, user, action="appointment_booking_repeated", row=row,
                    summary=(f"Appointment {row['id']} booking repeated for {patient_id} "
                             f"with {doctor_id} on {scheduled_date} {scheduled_time}; "
                             "nothing new was saved"),
                    metadata={"replay": True, "matched_by": matched_by,
                              "doctor_id": doctor_id,
                              "session_id": _row_get(row, "session_id"),
                              "slot_position": _row_get(row, "slot_position"),
                              "department": _row_get(row, "department")})
            return _staff_view(db, [row])[0]
    except _ReplayConflict as err:
        _audit_replay_refused(request, user, patient_id=patient_id, err=err, requested={
            "requested_doctor_id": doctor_id, "requested_date": scheduled_date,
            "requested_time": scheduled_time,
            "requested_department": session["department"]})
        raise


# ── OPD mode: the front desk's day (opd-16) ──────────────────────────────────

@router.get("/day", dependencies=[Depends(require_opd_feature)])
def appointments_day(
    request: Request,
    date_: str | None = Query(default=None, alias="date", max_length=10),
    doctor_id: str | None = Query(default=None, max_length=64),
    user: dict = Depends(require_registration_desk("read")),
):
    """One hospital day of appointments for the caller's clinic, with each
    clinic session's slots and how many are taken.

    The department is the caller's own (permissions.department_scope), never a
    parameter; a caller with no department sees every clinic. Each row is in
    scope by the list rule (_appointment_visible). A booking still open after
    its session ended reads effective_status 'no_show' (computed, not
    written). Audited as a PHI read with the appointment ids when anything is
    listed."""
    from services import clinic_schedule_service as schedule

    dept = department_scope(user)
    now = hospital_time.hospital_now()
    try:
        day = date.fromisoformat(date_) if date_ else now.date()
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    doctor = (doctor_id or "").strip() or None
    sql = "SELECT * FROM appointments WHERE scheduled_date = ?"
    params: list = [day.isoformat()]
    if doctor:
        sql += " AND doctor_id = ?"
        params.append(doctor)
    sql += " ORDER BY scheduled_time, COALESCE(doctor_id, ''), slot_position, created_at, id"
    cache: dict = {}
    with db_session() as db:
        rows = [r for r in db.execute(sql, params).fetchall()
                if _appointment_visible(db, r, dept)]
        listed = _staff_view(db, rows)
        for data, row in zip(listed, rows):
            data["effective_status"], data["no_show_computed"] = _effective_status(
                db, row, now, cache)
        booked = {
            (r["doctor_id"], r["scheduled_time"]): r["n"] for r in db.execute(
                "SELECT doctor_id, scheduled_time, COUNT(*) AS n FROM appointments "
                "WHERE scheduled_date = ? AND doctor_id IS NOT NULL AND status != 'cancelled' "
                "GROUP BY doctor_id, scheduled_time", (day.isoformat(),)).fetchall()
        }
    try:
        offer = schedule.availability(day=day, days=1, doctor_id=doctor, department=dept)
    except schedule.ClinicScheduleError as err:
        raise _conflict(err.code, str(err), status=err.status)
    sessions = []
    for offered in offer["days"]:
        for session in offered["sessions"]:
            slots = []
            for slot in session["slots"]:
                taken = int(booked.get((session["doctor_id"], slot["start"]), 0))
                slots.append({**slot, "booked": taken,
                              "free": max(0, int(slot["capacity"]) - taken)})
            sessions.append({**session, "slots": slots})

    if listed:
        try:
            log_phi_read(
                request=request, user=user, action_type="appointments_day_viewed",
                summary_message=(f"Appointments for {day.isoformat()} viewed ({len(listed)}) "
                                 f"for {dept or '(all clinics, no department on record)'}"),
                target_type="appointment_day", target_id=day.isoformat(),
                metadata={"result_count": len(listed),
                          "appointment_ids": [a["id"] for a in listed],
                          "matched_patient_ids": sorted({a["patient_id"] for a in listed})},
            )
        except AuditWriteError:
            raise _audit_unavailable()
    return {
        "date": day.isoformat(),
        "hospital_today": now.date().isoformat(),
        "department": dept,
        "appointments": listed,
        "sessions": sessions,
        "types": [{"key": k, "label": v} for k, v in APPOINTMENT_TYPES.items()],
    }


# ── Patient portal: own upcoming ─────────────────────────────────────────────

@router.get("/mine")
def my_appointments(user: dict = Depends(require_patient_self())):
    """List the logged-in patient's own upcoming (booked, not-past) appointments.
    "Not past" is the hospital's day (services.hospital_time), not the server's."""
    patient_id = _resolve_own_patient_id(user["id"])
    today = hospital_time.hospital_today().isoformat()
    with db_session() as db:
        rows = db.execute(
            """SELECT * FROM appointments
               WHERE patient_id = ? AND status = 'booked' AND scheduled_date >= ?
               ORDER BY scheduled_date ASC, scheduled_time ASC""",
            (patient_id, today),
        ).fetchall()
    return {"appointments": [_serialize(r) for r in rows]}


# ── Clinician: list by patient ───────────────────────────────────────────────

@router.get("")
def list_appointments(
    patient_id: str,
    request: Request,
    user: dict = Depends(require_permission("patient.read_all")),
):
    """List all appointments for a patient (clinician view).

    WARD SCOPING + READ AUDIT. `patient_id` is named by the CALLER on the query
    string, so this is a per-patient PHI read wearing a list route's clothes:
    holding patient.read_all let a nurse on one ward pull another ward's
    appointment schedule, free-text `reason` and all. It survived the first pass
    at the 2026-08-05 leak only because the id is not in the path, which is
    where the drift guard was looking.
    """
    enforce_patient_scope(user, patient_id)

    with db_session() as db:
        rows = db.execute(
            """SELECT * FROM appointments
               WHERE patient_id = ?
               ORDER BY scheduled_date ASC, scheduled_time ASC""",
            (patient_id,),
        ).fetchall()

    if rows:
        log_phi_read(
            request=request,
            user=user,
            action_type="appointments_viewed",
            summary_message=f"Appointment list viewed ({len(rows)}) for {patient_id}",
            patient_id=patient_id,
            metadata={"result_count": len(rows)},
        )

    return {"appointments": [_serialize(r) for r in rows]}


# ── Cancel ───────────────────────────────────────────────────────────────────

@router.delete("/{appointment_id}")
def cancel_appointment(
    appointment_id: str,
    request: Request,
    reason: str | None = Query(default=None, max_length=_MAX_CANCEL_REASON_LEN),
    user: dict = Depends(get_current_user),
):
    """Cancel an appointment (soft: status -> 'cancelled', row is kept).

    Permitted for the booking patient (their own appointment) or any clinician
    with ``patient.write`` (in OPD mode also registration.write) WITHIN THEIR
    OWN SCOPE: another ward's appointment is 404, the same answer as an id that
    does not exist (opd-16; it was cancellable by id from any ward). A
    checked-in appointment is cancelled with its visit at the front desk, and
    a no-show stays one. `reason` is kept on the row.
    """
    role = user.get("role", "")
    own_patient_id = _resolve_own_patient_id(user["id"]) if role == "patient" else None
    dept = department_scope(user)
    note = (reason or "").strip()[:_MAX_CANCEL_REASON_LEN] or None

    with db_session(immediate=True) as db:
        row = db.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Appointment not found")

        if role == "patient":
            if row["patient_id"] != own_patient_id:
                raise HTTPException(
                    status_code=403,
                    detail={"error": "permission_denied", "message": "Not your appointment"},
                )
        elif not _may_book_for_patients(role):
            raise HTTPException(
                status_code=403,
                detail={"error": "permission_denied", "message": "Not permitted to cancel appointments"},
            )
        elif not _appointment_visible(db, row, dept):
            raise HTTPException(status_code=404, detail="Appointment not found")

        if row["status"] == "cancelled":
            return _serialize(row)
        # A completed appointment is a clinical fact that already happened —
        # cancelling it rewrote history (the row silently flipped to 'cancelled'
        # and the visit disappeared from the record).
        if row["status"] == "completed":
            raise HTTPException(
                status_code=409,
                detail="This appointment is already marked completed and cannot be cancelled.",
            )
        if row["status"] == "checked_in":
            raise HTTPException(
                status_code=409,
                detail=("This patient has checked in. Cancel the outpatient visit at the "
                        "front desk instead, and the appointment is closed with it."),
            )
        if row["status"] == "no_show":
            raise HTTPException(
                status_code=409,
                detail="This appointment is marked as a no-show and cannot be cancelled.",
            )

        db.execute(
            "UPDATE appointments SET status = 'cancelled', cancelled_at = ?, cancelled_by = ?, "
            "cancel_reason = COALESCE(?, cancel_reason) WHERE id = ?",
            (utc_iso_now(), user["id"], note, appointment_id),
        )
        updated = db.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()

    log_audit_event(
        correlation_id=f"appointment-cancel-{appointment_id}",
        event_category="clinical_record",
        action_type="appointment_cancelled",
        outcome="success",
        summary_message=f"Appointment {appointment_id} cancelled for {row['patient_id']}",
        actor_user_id=user["id"],
        actor_role=role,
        patient_id=row["patient_id"],
        target_type="appointment",
        target_id=appointment_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={"reason_given": note is not None},
    )

    return _serialize(updated)


# ── Complete ─────────────────────────────────────────────────────────────────

@router.post("/{appointment_id}/complete")
def complete_appointment(
    appointment_id: str,
    request: Request,
    user: dict = Depends(require_permission(PATIENT_WRITE)),
):
    """Mark an appointment as attended (status -> 'completed'). Clinician-only.

    The schema has always allowed 'completed' but nothing could ever set it, so
    every past appointment stayed 'booked' forever: the clinician list could not
    distinguish "attended" from "no-show, still open", and a patient's portal
    view only ever hid past slots by date. Idempotent — a repeat call returns the
    row unchanged.

    WARD SCOPE (opd-16): another ward's appointment is 404, the same answer as
    an id that does not exist; it could be marked attended by id from any
    ward. A no-show cannot be marked attended (book again or reschedule).
    """
    dept = department_scope(user)
    with db_session(immediate=True) as db:
        row = db.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        if not row or not _appointment_visible(db, row, dept):
            raise HTTPException(status_code=404, detail="Appointment not found")
        if row["status"] == "completed":
            return _serialize(row)
        if row["status"] == "cancelled":
            raise HTTPException(
                status_code=409,
                detail="This appointment was cancelled and cannot be marked completed.",
            )
        if row["status"] == "no_show":
            raise HTTPException(
                status_code=409,
                detail="This appointment is marked as a no-show and cannot be marked completed.",
            )
        db.execute(
            "UPDATE appointments SET status = 'completed', completed_at = ?, completed_by = ? "
            "WHERE id = ?",
            (utc_iso_now(), user["id"], appointment_id),
        )
        updated = db.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()

    log_audit_event(
        correlation_id=f"appointment-complete-{appointment_id}",
        event_category="clinical_record",
        action_type="appointment_completed",
        outcome="success",
        summary_message=f"Appointment {appointment_id} marked completed for {row['patient_id']}",
        actor_user_id=user["id"],
        actor_role=user.get("role", ""),
        patient_id=row["patient_id"],
        target_type="appointment",
        target_id=appointment_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
    )

    return _serialize(updated)


# ═════════════════════════════════════════════════════════════════════════════
#  OPD mode: check-in, no-show and reschedule (opd-16)
# ═════════════════════════════════════════════════════════════════════════════

_NOT_OPEN = {
    "cancelled": "This appointment was cancelled.",
    "completed": "This appointment is already completed.",
    "no_show": "This appointment is marked as a no-show. Reschedule it, or register a walk-in visit.",
    "checked_in": "This patient has already checked in.",
}


def _not_open(status: str) -> HTTPException:
    return _conflict("appointment_not_open",
                     _NOT_OPEN.get(status, "This appointment cannot be changed now."),
                     appointment_status=status)


class StatedIdentityIn(BaseModel):
    """What the patient STATES at check-in (ruling OPD-S1), compared on the
    server exactly as at the front desk; never echoed back. The same fields
    as routes/frontdesk_routes.IdentityStatedIn."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, max_length=200)
    date_of_birth: str | None = Field(None, max_length=40)
    age_years: int | None = None
    phone_last4: str | None = Field(None, max_length=10)
    uhid: str | None = Field(None, max_length=40)


class CheckInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Required while the 'registration' feature is on, which it always is in
    # OPD mode: no visit is attached to a record without it.
    identity_check: StatedIdentityIn | None = None


@router.post("/{appointment_id}/check-in", dependencies=[Depends(require_opd_feature)])
def check_in_appointment(
    appointment_id: str,
    request: Request,
    data: CheckInRequest | None = None,
    user: dict = Depends(require_registration_desk("write")),
):
    """The patient has arrived: open their OPD visit with a token.

    Exactly the front desk's visit registration (POST /api/frontdesk/opd/
    visits): the desk's write rule, the caller's clinic only, the
    registration rule for this patient (frontdesk_service.
    opd_registration_allowed), the stated-identifier check (OPD-S1), and the
    visit's audit rows and outpatient-record window written in the visit's
    own transaction. The appointment moves to 'checked_in' and names the
    visit in that same transaction, or nothing is saved. Only on the
    appointment's own day. A repeat answers the same visit (the visit's
    idempotency key is the appointment's)."""
    from routes import frontdesk_routes as desk
    from services.frontdesk_service import (
        FrontDeskError,
        OpdReplayConflict,
        opd_registration_allowed,
        register_opd_visit,
    )
    from services.opd_queue_service import nudge_queue_changed

    body = data or CheckInRequest()
    dept = department_scope(user)
    with db_session() as db:
        row = _appointment_or_404(db, appointment_id, dept)
    status = row["status"]
    if status not in ("booked", "checked_in"):
        raise _not_open(status)
    if status == "booked" and row["scheduled_date"] != hospital_time.hospital_today().isoformat():
        raise _conflict(
            "not_today",
            f"This appointment is for {row['scheduled_date']}. Check the patient in on the "
            "day of their appointment, or reschedule it.")
    department = (_row_get(row, "department") or dept or "").strip()
    if not department:
        raise _conflict("department_required",
                        "This appointment names no clinic. Reschedule it into a clinic first.",
                        status=422)
    if dept is not None and not _same_clinic(department, dept):
        raise _conflict("department_scope", f"You can only check patients in to {dept}",
                        status=403)
    patient_id = row["patient_id"]
    with db_session() as db:
        if not opd_registration_allowed(db, patient_id, dept):
            raise HTTPException(status_code=404, detail="Patient not found")

    identity = None
    if config.feature_enabled("registration"):
        identity = desk._confirm_identity(request, user, patient_id=patient_id, dept=dept,
                                          stated=body.identity_check)

    self_registered = has_permission(user.get("role", ""), OPD_CONSULT)
    registration_audit = desk._registration_audit(
        request, user, patient_id=patient_id, dept=dept,
        self_registered=self_registered, identity=identity)
    replay_audit = desk._registration_replay_audit(request, user)

    def _on_registered(db, visit: dict) -> None:
        registration_audit(db, visit)
        moved = db.execute(
            "UPDATE appointments SET status = 'checked_in', checked_in_at = ?, "
            "checked_in_by = ?, opd_visit_id = ? WHERE id = ? AND status = 'booked'",
            (utc_iso_now(), user["id"], visit["id"], appointment_id),
        )
        if moved.rowcount != 1:
            raise _conflict("appointment_changed",
                            "This appointment changed while the patient was being checked in. "
                            "Nothing was saved.")
        fresh = db.execute("SELECT * FROM appointments WHERE id = ?",
                           (appointment_id,)).fetchone()
        _audit_appointment(
            db, request, user, action="appointment_checked_in", row=fresh,
            summary=(f"Appointment {appointment_id} checked in: OPD visit {visit['id']}, "
                     f"token {visit['token_number']}"),
            metadata={"opd_visit_id": visit["id"], "token_number": visit["token_number"],
                      "department": visit["department"], "doctor_id": visit.get("doctor_id"),
                      **desk._identity_facts(identity)})

    def _on_replay(db, visit: dict, facts: dict) -> None:
        replay_audit(db, visit, facts)
        current = db.execute("SELECT opd_visit_id FROM appointments WHERE id = ?",
                             (appointment_id,)).fetchone()
        if current is None or current["opd_visit_id"] != visit["id"]:
            raise _conflict("appointment_changed",
                            "This appointment no longer names that visit. Nothing was saved.")

    try:
        visit = register_opd_visit(
            patient_id=patient_id,
            department=department,
            doctor_id=_row_get(row, "doctor_id"),
            visit_type=_VISIT_TYPE_FOR.get(type_key(row["appointment_type"]), "new"),
            appointment_id=appointment_id,
            registered_by=user.get("id"),
            idempotency_key=f"appointment-checkin:{appointment_id}",
            self_registered=self_registered,
            on_registered=_on_registered,
            on_replay=_on_replay,
            identity_check=identity,
        )
    except OpdReplayConflict as err:
        desk._audit_replay_refused(request, user, patient_id=patient_id,
                                   department=department, err=err)
        raise desk._replay_conflict()
    except FrontDeskError as err:
        raise HTTPException(status_code=422, detail=str(err))
    except AuditWriteError:
        raise _audit_unavailable()
    nudge_queue_changed(visit.get("department"))
    with db_session() as db:
        fresh = db.execute("SELECT * FROM appointments WHERE id = ?",
                           (appointment_id,)).fetchone()
        appointment = _staff_view(db, [fresh])[0]
    return {
        "appointment": appointment,
        "visit": {**visit, **desk._identity_facts(identity)},
        "replayed": status == "checked_in",
    }


@router.post("/{appointment_id}/no-show", dependencies=[Depends(require_opd_feature)])
def mark_no_show(
    appointment_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """The patient did not come: marked by the desk once the appointment time
    has started (409 not_yet_due before it). Only an open booking; a repeat
    answers the row unchanged. The caller's scope, as every route here."""
    _require_booking_staff(request, user)
    dept = department_scope(user)
    now = hospital_time.hospital_now()
    with db_session(immediate=True) as db:
        row = _appointment_or_404(db, appointment_id, dept)
        if row["status"] == "no_show":
            return _staff_view(db, [row])[0]
        if row["status"] != "booked":
            raise _not_open(row["status"])
        if not _slot_started(row, now):
            raise _conflict("not_yet_due",
                            "The appointment time has not come yet, so it cannot be a no-show.")
        db.execute(
            "UPDATE appointments SET status = 'no_show', no_show_at = ?, no_show_by = ? "
            "WHERE id = ? AND status = 'booked'",
            (utc_iso_now(), user["id"], appointment_id),
        )
        fresh = db.execute("SELECT * FROM appointments WHERE id = ?",
                           (appointment_id,)).fetchone()
        _audit_appointment(
            db, request, user, action="appointment_no_show", row=fresh,
            summary=f"Appointment {appointment_id} marked as a no-show",
            metadata={"scheduled_date": fresh["scheduled_date"],
                      "scheduled_time": fresh["scheduled_time"],
                      "doctor_id": _row_get(fresh, "doctor_id")})
        return _staff_view(db, [fresh])[0]


def _reschedule_replay(db, idem: str | None, appointment_id: str, old) -> dict | None:
    """The earlier answer when this Idempotency-Key already moved THIS
    appointment; 409 idempotency_key_reused when it names another booking;
    None when the key is new (or there is none)."""
    if not idem:
        return None
    replayed = db.execute("SELECT * FROM appointments WHERE idempotency_key = ?",
                          (idem,)).fetchone()
    if replayed is None:
        return None
    if _row_get(replayed, "rescheduled_from_id") != appointment_id:
        raise _conflict("idempotency_key_reused",
                        "This request key was already used for another booking.")
    return {"appointment": _staff_view(db, [replayed])[0],
            "previous": _staff_view(db, [old])[0], "replayed": True}


class RescheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduled_date: str = Field(..., max_length=10)
    scheduled_time: str = Field(..., max_length=5)
    # Another doctor's slot; omitted, the same doctor (or, for a booking with
    # no doctor, the same clinic with no doctor).
    doctor_id: str | None = Field(None, max_length=64)
    reason: str | None = Field(None, max_length=_MAX_CANCEL_REASON_LEN)


@router.post("/{appointment_id}/reschedule", dependencies=[Depends(require_opd_feature)])
def reschedule_appointment(
    appointment_id: str,
    data: RescheduleRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict = Depends(get_current_user),
):
    """Move a booking to another day or slot as a NEW row that points at the
    old one (rescheduled_from_id). The old row is kept: an open booking is
    cancelled with the reason, a no-show stays a no-show. One live successor
    per booking (409 already_rescheduled). The same slot rules as a booking,
    the caller's scope and clinic, all in one transaction."""
    _require_booking_staff(request, user)
    dept = department_scope(user)
    scheduled_date = data.scheduled_date.strip()
    scheduled_time = data.scheduled_time.strip()
    note = (data.reason or "").strip()[:_MAX_CANCEL_REASON_LEN] or None
    idem = idempotency_key.strip() if idempotency_key else None

    with db_session() as db:
        old = _appointment_or_404(db, appointment_id, dept)
        # A retry of a move that already happened answers that move, before
        # anything about the old row's state (now cancelled) is checked.
        replay = _reschedule_replay(db, idem, appointment_id, old)
    if replay is not None:
        return replay
    _validate_slot(scheduled_date, scheduled_time)
    if old["status"] not in ("booked", "no_show"):
        raise _not_open(old["status"])
    doctor_id = (data.doctor_id or "").strip() or _row_get(old, "doctor_id")
    if (doctor_id == _row_get(old, "doctor_id") and scheduled_date == old["scheduled_date"]
            and scheduled_time == old["scheduled_time"]):
        raise _conflict("same_slot", "That is the time the appointment already has.",
                        status=422)
    offer = _slot_offer(doctor_id, scheduled_date, scheduled_time) if doctor_id else None
    department = offer["session"]["department"] if offer else (
        _row_get(old, "department") or dept)
    if dept is not None and not _same_clinic(department, dept):
        raise _conflict("department_scope", f"You can only book appointments in {dept}",
                        status=403)

    with db_session(immediate=True) as db:
        old = _appointment_or_404(db, appointment_id, dept)
        replay = _reschedule_replay(db, idem, appointment_id, old)
        if replay is not None:
            return replay
        if old["status"] not in ("booked", "no_show"):
            raise _not_open(old["status"])
        successor = db.execute(
            "SELECT id FROM appointments WHERE rescheduled_from_id = ? AND status != 'cancelled'",
            (appointment_id,)).fetchone()
        if successor is not None:
            raise _conflict("already_rescheduled",
                            "This appointment was already moved to another time.",
                            appointment_id=successor["id"])
        # The old booking gives its slot back first, so a move to the same
        # time with another doctor is not refused as a clash with itself. If
        # the new booking is refused, the transaction rolls this back too.
        if old["status"] == "booked":
            moved_to = f"Rescheduled to {scheduled_date} {scheduled_time}"
            db.execute(
                "UPDATE appointments SET status = 'cancelled', cancelled_at = ?, "
                "cancelled_by = ?, cancel_reason = ? WHERE id = ? AND status = 'booked'",
                (utc_iso_now(), user["id"],
                 (f"{moved_to}: {note}" if note else moved_to)[:_MAX_CANCEL_REASON_LEN],
                 appointment_id),
            )
        if offer is not None:
            new_row, _created = _book_slot_on(
                db, patient_id=old["patient_id"], booked_by=user["id"], doctor_id=doctor_id,
                scheduled_date=scheduled_date, scheduled_time=scheduled_time,
                appointment_type=old["appointment_type"], reason=old["reason"], offer=offer,
                idem=idem, rescheduled_from_id=appointment_id)
            if _row_get(new_row, "rescheduled_from_id") != appointment_id:
                raise _conflict("patient_already_booked",
                                "This patient already has an appointment at that time.")
        else:
            clash = db.execute(
                "SELECT 1 FROM appointments WHERE patient_id = ? AND scheduled_date = ? "
                "AND scheduled_time = ? AND status IN ('booked', 'checked_in')",
                (old["patient_id"], scheduled_date, scheduled_time)).fetchone()
            if clash is not None:
                raise _conflict("patient_already_booked",
                                "This patient already has an appointment at that time.")
            new_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO appointments
                       (id, patient_id, booked_by_user_id, scheduled_date, scheduled_time,
                        appointment_type, reason, status, idempotency_key, origin, department,
                        rescheduled_from_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'booked', ?, ?, ?, ?)""",
                (new_id, old["patient_id"], user["id"], scheduled_date, scheduled_time,
                 old["appointment_type"], old["reason"], idem, ORIGIN_STAFF, department,
                 appointment_id),
            )
            new_row = db.execute("SELECT * FROM appointments WHERE id = ?", (new_id,)).fetchone()
        previous = db.execute("SELECT * FROM appointments WHERE id = ?",
                              (appointment_id,)).fetchone()
        _audit_appointment(
            db, request, user, action="appointment_rescheduled", row=new_row,
            summary=(f"Appointment {appointment_id} rescheduled to {scheduled_date} "
                     f"{scheduled_time} as {new_row['id']}"),
            metadata={"rescheduled_from_id": appointment_id,
                      "previous_status": old["status"],
                      "from": f"{old['scheduled_date']} {old['scheduled_time']}",
                      "to": f"{scheduled_date} {scheduled_time}",
                      "doctor_id": doctor_id, "reason_given": note is not None})
        return {"appointment": _staff_view(db, [new_row])[0],
                "previous": _staff_view(db, [previous])[0], "replayed": False}
