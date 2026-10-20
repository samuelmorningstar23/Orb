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

import config
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


_CLINIC_DOCTORS_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/doctors")
def clinic_doctors(department: str | None = None, user: dict = _CLINIC_DOCTORS_USER_DEFAULT):
    """Active doctors who may hold a clinic (opd.consult), with their
    department. Staff names only."""
    return {"doctors": schedule.list_clinic_doctors(department)}


_LIST_SESSIONS_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/sessions")
def list_sessions(
    doctor_id: str | None = None,
    department: str | None = None,
    include_retired: bool = False,
    user: dict = _LIST_SESSIONS_USER_DEFAULT,
):
    return {
        "sessions": schedule.list_sessions(
            doctor_id=doctor_id, department=department, include_retired=include_retired
        )
    }


_CREATE_SESSION_USER_DEFAULT = Depends(_MANAGE)


@router.post("/sessions")
def create_session(data: SessionIn, request: Request, user: dict = _CREATE_SESSION_USER_DEFAULT):
    """Add a weekly clinic. 409 when it overlaps another clinic of the same
    doctor; 422 when a field is wrong."""
    try:
        return schedule.create_session(
            data.model_dump(), actor=user, request_meta=_request_meta(request)
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_READ_SESSION_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/sessions/{session_id}")
def read_session(session_id: str, user: dict = _READ_SESSION_USER_DEFAULT):
    found = schedule.get_session(session_id)
    if found is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "No such clinic"}
        )
    return found


_UPDATE_SESSION_USER_DEFAULT = Depends(_MANAGE)


@router.patch("/sessions/{session_id}")
def update_session(
    session_id: str, data: SessionPatch, request: Request, user: dict = _UPDATE_SESSION_USER_DEFAULT
):
    """Change a clinic's day, hours, slots, room, dates or notes. The doctor and
    department never change here: retire the clinic and add a new one."""
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


@router.post("/sessions/{session_id}/retire")
def retire_session(
    session_id: str, data: ReasonIn, request: Request, user: dict = _RETIRE_SESSION_USER_DEFAULT
):
    try:
        return schedule.retire_session(
            session_id, reason=data.reason, actor=user, request_meta=_request_meta(request)
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_LIST_EXCEPTIONS_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/exceptions")
def list_exceptions(
    date_from: str | None = None,
    date_to: str | None = None,
    doctor_id: str | None = None,
    include_withdrawn: bool = False,
    user: dict = _LIST_EXCEPTIONS_USER_DEFAULT,
):
    try:
        return {
            "exceptions": schedule.list_exceptions(
                date_from=date_from,
                date_to=date_to,
                doctor_id=doctor_id,
                include_withdrawn=include_withdrawn,
            )
        }
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_ADD_EXCEPTION_USER_DEFAULT = Depends(_MANAGE)


@router.post("/exceptions")
def add_exception(data: ExceptionIn, request: Request, user: dict = _ADD_EXCEPTION_USER_DEFAULT):
    """A day a clinic does not run: one session, or every session the doctor
    holds that day. A reason is required."""
    try:
        return schedule.add_exception(
            data.model_dump(), actor=user, request_meta=_request_meta(request)
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_WITHDRAW_EXCEPTION_USER_DEFAULT = Depends(_MANAGE)


@router.post("/exceptions/{exception_id}/withdraw")
def withdraw_exception(
    exception_id: str,
    data: ReasonIn,
    request: Request,
    user: dict = _WITHDRAW_EXCEPTION_USER_DEFAULT,
):
    try:
        return schedule.withdraw_exception(
            exception_id, reason=data.reason, actor=user, request_meta=_request_meta(request)
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err


_AVAILABILITY_USER_DEFAULT = Depends(_DESK_READ)


@router.get("/availability")
def availability(
    date: str | None = None,
    days: int = 1,
    doctor_id: str | None = None,
    department: str | None = None,
    user: dict = _AVAILABILITY_USER_DEFAULT,
):
    """The slots each running clinic offers, in hospital-local time. `date` is
    a hospital-local day (default: the hospital's today)."""
    try:
        return schedule.availability(
            day=date, days=days, doctor_id=doctor_id, department=department
        )
    except ClinicScheduleError as err:
        raise _refusal(err) from err
