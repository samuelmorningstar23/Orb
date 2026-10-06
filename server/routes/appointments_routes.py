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
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from auth import get_current_user
from permissions import (
    PATIENT_WRITE,
    enforce_patient_scope,
    has_permission,
    log_phi_read,
    require_patient_self,
    require_permission,
)
from database import db_session
from services.audit_service import log_audit_event
from services.appointment_types import APPOINTMENT_TYPES, parse_type, type_key, type_label

router = APIRouter(prefix="/api/appointments", tags=["appointments"])

# Booking horizon. Without an upper bound a typo'd year ("2206-03-04") books a
# slot 180 years out that never appears in any clinician view and never expires.
MAX_BOOKING_HORIZON_DAYS = 730

# Free-text caps — the columns are unbounded TEXT, so an unchecked body could
# park megabytes of PHI-shaped text in the appointments table.
_MAX_TYPE_LEN = 120
_MAX_REASON_LEN = 2000

# appointments.status CHECK constraint (database.py SCHEMA).
_TERMINAL_STATUSES = {"cancelled", "completed"}

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
    }


def _validate_slot(scheduled_date: str, scheduled_time: str) -> None:
    """Validate YYYY-MM-DD / HH:MM formats and reject bookings in the past."""
    try:
        parsed_date = datetime.strptime(scheduled_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="scheduled_date must be YYYY-MM-DD")
    try:
        parsed_time = datetime.strptime(scheduled_time, "%H:%M").time()
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="scheduled_time must be HH:MM (24h)")
    if parsed_date < date.today():
        raise HTTPException(status_code=422, detail="Cannot book an appointment in the past")
    # Only the DATE was checked, so a slot earlier TODAY booked cleanly — the
    # patient portal would happily accept "today at 08:00" at 4pm and then show
    # it under "upcoming" until midnight.
    if parsed_date == date.today() and parsed_time < datetime.now().time():
        raise HTTPException(
            status_code=422,
            detail="Cannot book an appointment earlier today. That time has passed",
        )
    if parsed_date > date.today() + timedelta(days=MAX_BOOKING_HORIZON_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot book more than {MAX_BOOKING_HORIZON_DAYS} days ahead",
        )


def _validate_date_only(scheduled_date: str) -> date:
    """Date half of _validate_slot, for a request that names no time."""
    try:
        parsed_date = datetime.strptime(scheduled_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="scheduled_date must be YYYY-MM-DD")
    if parsed_date < date.today():
        raise HTTPException(status_code=422, detail="Cannot book an appointment in the past")
    if parsed_date > date.today() + timedelta(days=MAX_BOOKING_HORIZON_DAYS):
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
    """
    if parsed_date > date.today():
        return FIRST_CLINIC_SLOT
    if parsed_date < date.today():
        return None
    now = datetime.now()
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
) -> tuple[dict, bool]:
    """Write one appointment row and return (serialized, created).

    THE one writer for this table, shared by the clinician/patient booking route
    below and by the patient's follow-up REQUEST in bridge_routes — so a request
    cannot drift into a second table that no staff surface reads (which is
    exactly how `patient_appointment_requests` became a dead letter box).

    `created` is False when an existing row satisfied the caller: an
    Idempotency-Key replay, or the same patient/date/time already live.

    BLOCKING. Opens its own session, so async callers must go through run_db().
    """
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
                return _serialize(existing), False

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
                    return _serialize(dup), False
            raise
        row = db.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
        return _serialize(row), True


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
    """
    role = user.get("role", "")

    if role == "patient":
        patient_id = _resolve_own_patient_id(user["id"])
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


# ── Patient portal: own upcoming ─────────────────────────────────────────────

@router.get("/mine")
def my_appointments(user: dict = Depends(require_patient_self())):
    """List the logged-in patient's own upcoming (booked, not-past) appointments."""
    patient_id = _resolve_own_patient_id(user["id"])
    today = date.today().isoformat()
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
    user: dict = Depends(get_current_user),
):
    """Cancel an appointment (soft: status -> 'cancelled', row is kept).

    Permitted for the booking patient (their own appointment) or any clinician
    with ``patient.write``.
    """
    role = user.get("role", "")

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Appointment not found")

        if role == "patient":
            own_patient_id = _resolve_own_patient_id(user["id"])
            if row["patient_id"] != own_patient_id:
                raise HTTPException(
                    status_code=403,
                    detail={"error": "permission_denied", "message": "Not your appointment"},
                )
        elif not has_permission(role, PATIENT_WRITE):
            raise HTTPException(
                status_code=403,
                detail={"error": "permission_denied", "message": "Not permitted to cancel appointments"},
            )

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

        db.execute(
            "UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment_id,)
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
    """
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Appointment not found")
        if row["status"] == "completed":
            return _serialize(row)
        if row["status"] == "cancelled":
            raise HTTPException(
                status_code=409,
                detail="This appointment was cancelled and cannot be marked completed.",
            )
        db.execute(
            "UPDATE appointments SET status = 'completed' WHERE id = ?", (appointment_id,)
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
