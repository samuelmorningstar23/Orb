"""Clinic sessions and availability: the /api/schedule routes.

Created empty in wave 1 of the hospital-system build (3 October 2026) and
registered once in main.py. opd-15 (W2-E) adds the clinic session master and
availability here.

BEHIND THE 'registration' FEATURE (ORB_REGISTRATION, default off): clinic
timings belong to the registration desk. The gate is a router-level
dependency, so every endpoint added here inherits it and answers 404 while the
feature is off (after a 401 for a caller with no session).

Rules for every endpoint added here (design: docs/his_design/opd.json):
  * writes need schedule.manage and audit as configuration; availability reads
    need registration.read or patient.read_all;
  * slots are computed in hospital-local time (services.hospital_time), never
    stored; overlapping sessions for one doctor are refused;
  * plain `def` handlers.
"""

from collections.abc import Iterable

import config
from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Request
from permissions import (
    REGISTRATION_READ,
    SCHEDULE_MANAGE,
    require_permission,
    require_registration_desk,
)
from pydantic import BaseModel, Field
from services.clinic_schedule_service import ClinicScheduleError

from routes.opt_in_gates import require_registration_feature
from services import clinic_schedule_service as schedule
from services import facility_lanes

router = APIRouter(
    prefix="/api/schedule",
    tags=["Clinic schedule"],
    dependencies=[Depends(require_registration_feature)],
)


_SCHEDULE_META_USER_DEFAULT = Depends(require_permission(REGISTRATION_READ))


@router.get("/meta")
def schedule_meta(user: dict = _SCHEDULE_META_USER_DEFAULT):
    """Whether clinic timings are offered at this site. No patient data."""
    return {"feature": "registration", "enabled": config.feature_enabled("registration")}


# ── opd-15 (W2-E): the clinic session master and availability ────────────────
#
# Reads (sessions, exceptions, availability, the doctors who may hold a clinic)
# are configuration with no patient data, and are open to the registration desk
# (permissions.require_registration_desk('read'): patient.read_all or
# registration.read). They are not scoped to the caller's department: a desk
# routes patients to every clinic, and a timetable discloses nobody.
#
# Writes need schedule.manage (the administrator today) and are audited as
# 'configuration' inside the transaction that makes them
# (services/clinic_schedule_service). Every handler is a plain def.

_DESK_READ = require_registration_desk("read")
_MANAGE = require_permission(SCHEDULE_MANAGE)


class SessionIn(BaseModel):
    doctor_id: str = Field(..., min_length=1, max_length=64)
    department: str | None = Field(default=None, max_length=200)
    weekday: int
    start_time: str = Field(..., max_length=8)
    end_time: str = Field(..., max_length=8)
    slot_minutes: int
    capacity_per_slot: int = 1
    room: str | None = Field(default=None, max_length=200)
    valid_from: str | None = Field(default=None, max_length=10)
    valid_to: str | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=2000)


class SessionPatch(BaseModel):
    row_version: int
    weekday: int | None = None
    start_time: str | None = Field(default=None, max_length=8)
    end_time: str | None = Field(default=None, max_length=8)
    slot_minutes: int | None = None
    capacity_per_slot: int | None = None
    room: str | None = Field(default=None, max_length=200)
    valid_from: str | None = Field(default=None, max_length=10)
    valid_to: str | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=2000)


class ReasonIn(BaseModel):
    reason: str = Field(..., max_length=2000)


class ExceptionIn(BaseModel):
    exception_date: str = Field(..., max_length=10)
    reason: str = Field(..., max_length=2000)
    doctor_id: str | None = Field(default=None, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)


def _refusal(err: ClinicScheduleError) -> HTTPException:
    return HTTPException(status_code=err.status, detail={"error": err.code, "message": str(err)})


def _request_meta(request: Request) -> dict:
    return {
        "path": request.url.path,
        "method": request.method,
        "ip": request.client.host if request.client else None,
    }


# ── Facility scope (multisite-8h1, W12-F) ────────────────────────────────────
#
# A clinic belongs to the facility of its department (ruling MS-R4 (a)). With
# more than one facility on record, a timetable read lists only the clinics of
# the caller's facilities, and a write that names a clinic, a session or an
# exception of another facility answers 404 exactly like one that does not
# exist. A timetable names no patient, so nothing here is read-audited. With
# one facility on record nothing more is read.

_NO_SUCH_CLINIC = {"error": "not_found", "message": "No such clinic"}


def _clinic_outside(department: object, facilities: frozenset[int] | None) -> bool:
    """Is a clinic of `department` outside `facilities`? Never with no
    facility limit; always for a department no facility lists."""
    if facilities is None:
        return False
    from services import facility_service

    name = str(department or "").strip()
    return facility_lanes.outside(
        facility_service.department_facility(name) if name else None, facilities
    )


def _doctor_departments(doctor_ids: Iterable[object]) -> dict[str, str | None]:
    """{doctor id: the department their account holds} for a clinic named by
    its doctor only."""
    ids = sorted({str(d) for d in doctor_ids if d})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with db_session() as db:
        rows = db.execute(
            f"SELECT id, TRIM(COALESCE(department, '')) FROM users WHERE id IN ({marks})", ids
        ).fetchall()
    return {str(r[0]): (r[1] or None) for r in rows}


def _exception_department(exception_id: str) -> tuple[bool, str | None]:
    """(the exception exists, the department of its clinic): its session's,
    else its doctor's."""
    with db_session() as db:
        row = db.execute(
            "SELECT e.doctor_id, s.department FROM clinic_session_exceptions e "
            "LEFT JOIN clinic_sessions s ON s.id = e.session_id WHERE e.id = ?",
            (exception_id,),
        ).fetchone()
    if row is None:
        return False, None
    if row[1]:
        return True, row[1]
    return True, _doctor_departments([row[0]]).get(str(row[0]))


def _require_clinic_in_scope(department: object, facilities: frozenset[int] | None) -> None:
    if _clinic_outside(department, facilities):
        raise HTTPException(status_code=404, detail=dict(_NO_SUCH_CLINIC))


_CLINIC_DOCTORS_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/doctors", response_model=None)
def clinic_doctors(
    department: str | None = None, user: dict = _CLINIC_DOCTORS_USER_DEFAULT
) -> dict:
    """Active doctors who may hold a clinic (opd.consult), with their
    department. Staff names only."""
    facilities = facility_lanes.scope_of(user)
    doctors = schedule.list_clinic_doctors(department)
    if facilities is not None:
        doctors = [d for d in doctors if not _clinic_outside(d.get("department"), facilities)]
    return {"doctors": doctors}


_LIST_SESSIONS_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/sessions", response_model=None)
def list_sessions(
    doctor_id: str | None = None,
    department: str | None = None,
    include_retired: bool = False,
    user: dict = _LIST_SESSIONS_USER_DEFAULT,
) -> dict:
    facilities = facility_lanes.scope_of(user)
    sessions = schedule.list_sessions(
        doctor_id=doctor_id, department=department, include_retired=include_retired
    )
    if facilities is not None:
        sessions = [s for s in sessions if not _clinic_outside(s.get("department"), facilities)]
    return {"sessions": sessions}


_CREATE_SESSION_USER_DEFAULT = Depends(_MANAGE)


@router.post("/sessions", response_model=None)
def create_session(
    data: SessionIn, request: Request, user: dict = _CREATE_SESSION_USER_DEFAULT
) -> dict:
    """Add a weekly clinic. 409 when it overlaps another clinic of the same
    doctor; 422 when a field is wrong."""
    facilities = facility_lanes.scope_of(user)
    if facilities is not None:
        clinic = data.department or _doctor_departments([data.doctor_id]).get(data.doctor_id)
        _require_clinic_in_scope(clinic, facilities)
    try:
        return schedule.create_session(
            data.model_dump(), actor=user, request_meta=_request_meta(request)
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_READ_SESSION_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/sessions/{session_id}", response_model=None)
def read_session(session_id: str, user: dict = _READ_SESSION_USER_DEFAULT) -> dict:
    found = schedule.get_session(session_id)
    if found is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "No such clinic"}
        )
    _require_clinic_in_scope(found.get("department"), facility_lanes.scope_of(user))
    return found


def _require_session_in_scope(session_id: str, user: dict) -> None:
    """404 for a session of a clinic outside the caller's facilities; an
    unknown session is left to the service's own 404."""
    facilities = facility_lanes.scope_of(user)
    if facilities is None:
        return
    found = schedule.get_session(session_id)
    if found is not None:
        _require_clinic_in_scope(found.get("department"), facilities)


_UPDATE_SESSION_USER_DEFAULT = Depends(_MANAGE)


@router.patch("/sessions/{session_id}", response_model=None)
def update_session(
    session_id: str, data: SessionPatch, request: Request, user: dict = _UPDATE_SESSION_USER_DEFAULT
) -> dict:
    """Change a clinic's day, hours, slots, room, dates or notes. The doctor and
    department never change here: retire the clinic and add a new one."""
    _require_session_in_scope(session_id, user)
    changes = data.model_dump(exclude_unset=True)
    expected = changes.pop("row_version", None)
    try:
        return schedule.update_session(
            session_id,
            changes,
            expected_row_version=expected,
            actor=user,
            request_meta=_request_meta(request),
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_RETIRE_SESSION_USER_DEFAULT = Depends(_MANAGE)


@router.post("/sessions/{session_id}/retire", response_model=None)
def retire_session(
    session_id: str, data: ReasonIn, request: Request, user: dict = _RETIRE_SESSION_USER_DEFAULT
) -> dict:
    _require_session_in_scope(session_id, user)
    try:
        return schedule.retire_session(
            session_id, reason=data.reason, actor=user, request_meta=_request_meta(request)
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_LIST_EXCEPTIONS_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/exceptions", response_model=None)
def list_exceptions(
    date_from: str | None = None,
    date_to: str | None = None,
    doctor_id: str | None = None,
    include_withdrawn: bool = False,
    user: dict = _LIST_EXCEPTIONS_USER_DEFAULT,
) -> dict:
    try:
        exceptions = schedule.list_exceptions(
            date_from=date_from,
            date_to=date_to,
            doctor_id=doctor_id,
            include_withdrawn=include_withdrawn,
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err
    facilities = facility_lanes.scope_of(user)
    if facilities is not None:
        clinics = _doctor_departments(e.get("doctor_id") for e in exceptions)
        exceptions = [
            e
            for e in exceptions
            if not _clinic_outside(clinics.get(str(e.get("doctor_id"))), facilities)
        ]
    return {"exceptions": exceptions}


_ADD_EXCEPTION_USER_DEFAULT = Depends(_MANAGE)


@router.post("/exceptions", response_model=None)
def add_exception(
    data: ExceptionIn, request: Request, user: dict = _ADD_EXCEPTION_USER_DEFAULT
) -> dict:
    """A day a clinic does not run: one session, or every session the doctor
    holds that day. A reason is required."""
    facilities = facility_lanes.scope_of(user)
    if facilities is not None:
        if data.session_id:
            _require_session_in_scope(data.session_id, user)
        if data.doctor_id:
            clinic = _doctor_departments([data.doctor_id]).get(data.doctor_id)
            _require_clinic_in_scope(clinic, facilities)
    try:
        return schedule.add_exception(
            data.model_dump(), actor=user, request_meta=_request_meta(request)
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_WITHDRAW_EXCEPTION_USER_DEFAULT = Depends(_MANAGE)


@router.post("/exceptions/{exception_id}/withdraw", response_model=None)
def withdraw_exception(
    exception_id: str,
    data: ReasonIn,
    request: Request,
    user: dict = _WITHDRAW_EXCEPTION_USER_DEFAULT,
) -> dict:
    facilities = facility_lanes.scope_of(user)
    if facilities is not None:
        exists, clinic = _exception_department(exception_id)
        if exists:
            _require_clinic_in_scope(clinic, facilities)
    try:
        return schedule.withdraw_exception(
            exception_id, reason=data.reason, actor=user, request_meta=_request_meta(request)
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_AVAILABILITY_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/availability", response_model=None)
def availability(
    date: str | None = None,
    days: int = 1,
    doctor_id: str | None = None,
    department: str | None = None,
    user: dict = _AVAILABILITY_USER_DEFAULT,
) -> dict:
    """The slots each running clinic offers, in hospital-local time. `date` is
    a hospital-local day (default: the hospital's today)."""
    try:
        found = schedule.availability(
            day=date, days=days, doctor_id=doctor_id, department=department
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err
    facilities = facility_lanes.scope_of(user)
    if facilities is not None:
        for day in found.get("days") or []:
            day["sessions"] = [
                s
                for s in day.get("sessions") or []
                if not _clinic_outside(s.get("department"), facilities)
            ]
    return found
