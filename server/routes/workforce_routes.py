"""Orb — Workforce API: duty roster, attendance, overtime, payroll EXPORT.

════════════════════════════════════════════════════════════════════════════════
 FOUR THINGS THIS ROUTER IS CAREFUL ABOUT
════════════════════════════════════════════════════════════════════════════════

1. IT IS NOT A CLINICAL PATH. Nothing here reads or writes medications, vitals,
   alerts, orders, admissions or shadow evidence, and nothing here may ever
   block one. A ward that is short-staffed on the roster is a fact on a screen;
   it never stops a nurse from giving a dose.

2. IT DOES NOT COMPUTE PAY. PF, ESI, Professional Tax and TDS are the payroll
   provider's, and the export ships those columns EMPTY with every row saying
   so. The reasoning is in services/workforce_service.py's module docstring and
   is repeated on the panel; it is a liability boundary, not a backlog item.

3. IT IS BEHIND A MODULE SWITCH. `config.WORKFORCE_ENABLED` (env
   `ORB_WORKFORCE`, DEFAULT ON). A site that already runs an HRMS sets it to 0
   and every route below answers 404 with a body that says WHY, so a stale
   client gets an explanation instead of a mystery. The frontend hides the tab
   from the same flag via /api/health.

4. IT CARRIES NO PHI, AND NO ROUTE TAKES A patient_id. Employment data is
   personal data under the DPDP Act but it is not patient data, so there is no
   `enforce_patient_scope` call anywhere below — because there is nothing with a
   patient scope to enforce. If a future route here ever grows a `patient_id`
   query parameter it MUST call it (tests/test_phi_read_scope_gaps.py enforces).

════════════════════════════════════════════════════════════════════════════════
 PERMISSION DEBT, STATED PLAINLY
════════════════════════════════════════════════════════════════════════════════
Orb has no `workforce.*` permission and no HR / ward-in-charge role;
`permissions.py` is outside this module's scope tonight. So every route below —
read AND write — requires `admin.overview`, which only an administrator holds.

Two real consequences, both wrong for a live deployment and both recorded as
follow-ups rather than papered over:

  * A CHARGE NURSE CANNOT MARK HER OWN WARD PRESENT. In a real hospital the
    person who knows who turned up is the ward in-charge, not an administrator.
    Until a `workforce.attendance` permission exists, attendance is entered by
    the HR/administration account — which is how most Indian hospitals already
    work from a paper muster, so the module is usable, but it is not where this
    should land.
  * "WHO IS ON TONIGHT" IS NOT VISIBLE TO THE WARD. The roster read would be
    genuinely useful on a clinical screen and is deliberately NOT exposed there,
    because a tab that 403s is worse than no tab (src/shellConfig.tsx §1.2.2).

All handlers are plain `def`, so FastAPI runs them in a threadpool: everything
below opens SQLite, and blocking the event loop is the failure mode this repo
has shipped three production freezes from.

════════════════════════════════════════════════════════════════════════════════
 ONE FACILITY AT A TIME (multisite-9b)
════════════════════════════════════════════════════════════════════════════════
With more than one facility of a group on the appliance an administrator bound
to one facility works with that facility's staff. An employee belongs to the
facility of their department in the departments master, else of the Orb login
they are linked to; one neither places belongs to no facility and is shown
only to a group-wide account. So, for a facility-bound account:

  * the employee and attendance lists keep to their facility's staff, and an
    employee, roster entry or attendance day of another facility's staff
    answers 404 like one that does not exist;
  * a department named on a read must be one of their facility's (404
    otherwise), and the views built over a whole department (the roster, its
    conflicts, the monthly muster and the payroll export) need one named
    (422 department_required) rather than adding up another facility's staff;
  * the shift types and holidays are one list for the whole group, so only a
    group-wide account changes them (403 group_setting).

With one facility on record the caller's facility scope is None and nothing
here runs.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import config
from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from permissions import ADMIN_OVERVIEW, facility_scope, require_permission
from pydantic import BaseModel
from services.audit_service import log_audit_event

from services import facility_stamp
from services import workforce_service as svc

logger = logging.getLogger("orb.workforce_routes")

router = APIRouter(prefix="/api/workforce", tags=["Workforce & Payroll Export"])


# ── Module switch ────────────────────────────────────────────────────────────


def require_module() -> None:
    """404 when the site has switched the workforce module off.

    404 rather than 403: the module is not disallowed for this user, it is not
    present at this deployment. The body says so, because a client built before
    the flag flipped deserves a sentence rather than a blank screen."""
    if not config.WORKFORCE_ENABLED:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "module_disabled",
                "module": "workforce",
                "message": (
                    "The workforce module (duty roster, attendance, payroll "
                    "export) is switched off for this deployment "
                    "(ORB_WORKFORCE=0). This site keeps its roster and "
                    "attendance somewhere else."
                ),
            },
        )


def _fail(exc: svc.WorkforceError) -> HTTPException:
    """Map a service refusal to HTTP, carrying the machine-readable `code` so a
    client can branch on it without matching English."""
    status = {
        "not_found": 404,
        "unknown_shift": 404,
        "unknown_leave_type": 404,
        "roster_conflict": 409,
        "already_exists": 409,
        "inactive_employee": 409,
        "before_joining": 409,
        "after_exit": 409,
        "future_date": 422,
        "leave_type_required": 422,
        "leave_type_unexpected": 422,
        "times_required": 422,
        "times_on_non_present": 422,
        "span_too_long": 422,
        "bad_span": 422,
    }.get(exc.code, 400)
    detail: dict = {"error": exc.code, "message": str(exc)}
    if isinstance(exc, svc.RosterConflict):
        detail["conflicts"] = exc.conflicts
    return HTTPException(status_code=status, detail=detail)


# ── Facilities (multisite-9b; see the module docstring) ─────────────────────


def _employee_facilities() -> dict[str, int | None]:
    """employee code -> the facility they belong to (None when nothing
    places them). Read only for a facility-limited caller."""
    out: dict[str, int | None] = {}
    with db_session() as db:
        rows = db.execute(
            "SELECT employee_code, department, user_id FROM staff_employment"
        ).fetchall()
        for code, department, user_id in rows:
            fid = facility_stamp.department_facility(db, department) if department else None
            if fid is None and user_id:
                fid = facility_stamp.account_facility(db, user_id)
            out[str(code).upper()] = fid
    return out


def _employee_ok(code: Any, facilities: frozenset[int], placed: dict[str, int | None]) -> bool:
    fid = placed.get(str(code or "").strip().upper())
    return fid is not None and fid in facilities


def _require_employee(facilities: frozenset[int] | None, code: str | None) -> None:
    """404 for an employee of another facility (or one no facility places)."""
    if facilities is None or not code:
        return
    if not _employee_ok(code, facilities, _employee_facilities()):
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"No employee {code.strip().upper()}"},
        )


def _require_department(facilities: frozenset[int] | None, department: str | None) -> None:
    """A department a facility-limited caller names must be one of their
    facility's: 404 otherwise, as for a department that does not exist."""
    if facilities is None or not department:
        return
    with db_session() as db:
        fid = facility_stamp.department_facility(db, department)
    if fid is None or fid not in facilities:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"No department {department.strip()}"},
        )


def _department_required(facilities: frozenset[int] | None, department: str | None) -> None:
    """A whole-department view for a facility-limited caller needs a
    department of their facility named (it would otherwise add up another
    facility's staff)."""
    if facilities is None:
        return
    if not department:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "department_required",
                "message": "Choose one of your facility's departments.",
            },
        )
    _require_department(facilities, department)


def _group_setting(facilities: frozenset[int] | None, what: str) -> None:
    """403 for a facility-limited caller changing a list the whole group shares."""
    if facilities is None:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "error": "group_setting",
            "message": (
                f"The {what} are one list for every facility of the group, so only an "
                "account with group-wide access changes them."
            ),
        },
    )


def _audit(
    request: Request,
    user: dict,
    action: str,
    *,
    summary: str,
    target_type: str,
    target_id: str | int | None = None,
    metadata: dict | None = None,
) -> None:
    """Every workforce WRITE leaves a trail.

    Category is `security` rather than a clinical one: an attendance correction
    is an administrative act on an employee's record, and mixing it into the
    clinical categories would dilute the trail a clinical investigation reads.
    `patient_id` is never passed — there is no patient anywhere in this module,
    and an audit row is not the place to invent one."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome="success",
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata=metadata or {},
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Request models
# ═════════════════════════════════════════════════════════════════════════════


class EmployeeIn(BaseModel):
    employee_code: str
    full_name: str
    department: str | None = None
    designation: str | None = None
    employment_type: str = "permanent"
    #: Optional link to an Orb login. Most of a hospital's payroll — housekeeping,
    #: security, kitchen, drivers — has no login and never will.
    user_id: str | None = None
    date_of_joining: str | None = None
    date_of_exit: str | None = None
    #: 0 = Monday … 6 = Sunday. NULL means the day off ROTATES with the roster,
    #: in which case it must be recorded as attendance, never inferred.
    weekly_off_day: int | None = None
    payroll_bureau_ref: str | None = None
    is_active: bool = True
    notes: str | None = None
    #: Set by the Add form: an existing code is refused (409), not overwritten.
    create_only: bool = False


class ShiftTypeIn(BaseModel):
    code: str
    name: str
    start_minute: int
    duration_minutes: int
    #: 'night' | 'evening' | null. Marks a shift a site's policy pays an
    #: allowance for. Orb COUNTS these; the amount is the bureau's.
    allowance_code: str | None = None
    is_active: bool = True


class RosterIn(BaseModel):
    roster_date: str
    shift_code: str
    employee_code: str
    department: str | None = None
    ward: str | None = None
    assigned_role: str | None = None
    status: str = "planned"
    notes: str | None = None
    #: Short rest and same-day double shifts come back as warnings by default.
    #: Set false to make the server refuse them outright — see MIN_REST_MINUTES.
    allow_short_rest: bool = True


class AttendanceIn(BaseModel):
    employee_code: str
    attendance_date: str
    status: str
    shift_code: str | None = None
    #: FULL ISO timestamps with offset, not times-of-day: a night shift's in and
    #: out fall on two calendar dates and Orb will not guess which.
    first_in_at: str | None = None
    last_out_at: str | None = None
    leave_type_code: str | None = None
    source: str = "manual"
    remarks: str | None = None


class HolidayIn(BaseModel):
    holiday_date: str
    name: str
    is_restricted: bool = False


class ExportIn(BaseModel):
    year: int
    month: int
    department: str | None = None
    notes: str | None = None


# ═════════════════════════════════════════════════════════════════════════════
#  Meta
# ═════════════════════════════════════════════════════════════════════════════


_META_MODULE_DEFAULT = Depends(require_module)
_META_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/meta")
def meta(
    _module=_META_MODULE_DEFAULT,
    user=_META_USER_DEFAULT,
):
    """The vocabulary a client needs to render forms without hard-coding it,
    plus the policy numbers and the statutory boundary in words — so the panel
    shows the site's own overtime threshold rather than a guess at it."""
    return {
        "shift_types": svc.list_shift_types(include_inactive=True),
        "leave_types": svc.list_leave_types(include_inactive=True),
        "day_statuses": list(svc.DAY_STATUSES),
        "employment_types": list(svc.EMPLOYMENT_TYPES),
        "attendance_sources": list(svc.ATTENDANCE_SOURCES),
        "policy": svc.policy(),
        "min_rest_minutes": svc.MIN_REST_MINUTES,
        "payroll_export_columns": list(svc.PAYROLL_EXPORT_COLUMNS),
        "statutory_columns": list(svc.STATUTORY_COLUMNS),
        "statutory_computed_by": svc.STATUTORY_COMPUTED_BY,
        "boundary": {
            "orb_owns": "duty roster, attendance, overtime hours, shift counts",
            "provider_owns": "salary, PF, ESI, Professional Tax, TDS, payslips",
            "message": svc.policy()["statutory_boundary"],
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Employees
# ═════════════════════════════════════════════════════════════════════════════


_LIST_EMPLOYEES_DEPARTMENT_DEFAULT = Query(None)
_LIST_EMPLOYEES_INCLUDE_INACTIVE_DEFAULT = Query(False)
_LIST_EMPLOYEES_SEARCH_DEFAULT = Query(None)
_LIST_EMPLOYEES_MODULE_DEFAULT = Depends(require_module)
_LIST_EMPLOYEES_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/employees", response_model=None)
def list_employees(
    department: str | None = _LIST_EMPLOYEES_DEPARTMENT_DEFAULT,
    include_inactive: bool = _LIST_EMPLOYEES_INCLUDE_INACTIVE_DEFAULT,
    search: str | None = _LIST_EMPLOYEES_SEARCH_DEFAULT,
    _module: None = _LIST_EMPLOYEES_MODULE_DEFAULT,
    user: dict = _LIST_EMPLOYEES_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_department(facilities, department)
    rows = svc.list_employees(
        department=department, include_inactive=include_inactive, search=search
    )
    if facilities is not None:
        placed = _employee_facilities()
        rows = [r for r in rows if _employee_ok(r.get("employee_code"), facilities, placed)]
    return {"employees": rows}


_GET_EMPLOYEE_MODULE_DEFAULT = Depends(require_module)
_GET_EMPLOYEE_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/employees/{employee_code}", response_model=None)
def get_employee(
    employee_code: str,
    _module: None = _GET_EMPLOYEE_MODULE_DEFAULT,
    user: dict = _GET_EMPLOYEE_USER_DEFAULT,
) -> dict:
    _require_employee(facility_scope(user), employee_code)
    try:
        return svc.get_employee(employee_code)
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc


_UPSERT_EMPLOYEE_MODULE_DEFAULT = Depends(require_module)
_UPSERT_EMPLOYEE_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/employees", response_model=None)
def upsert_employee(
    payload: EmployeeIn,
    request: Request,
    _module: None = _UPSERT_EMPLOYEE_MODULE_DEFAULT,
    user: dict = _UPSERT_EMPLOYEE_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    if facilities is not None:
        placed = _employee_facilities()
        code = payload.employee_code.strip().upper()
        if code in placed and not _employee_ok(code, facilities, placed):
            raise HTTPException(
                status_code=404, detail={"error": "not_found", "message": f"No employee {code}"}
            )
        _require_department(facilities, payload.department)
        if not payload.department:
            # Placed by the linked login: it must be one of this facility's.
            with db_session() as db:
                fid = (
                    facility_stamp.account_facility(db, payload.user_id)
                    if payload.user_id
                    else None
                )
            if fid is None or fid not in facilities:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "department_required",
                        "message": "Give the employee one of your facility's departments.",
                    },
                )
    try:
        employee = svc.upsert_employee(**payload.model_dump(), created_by=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "workforce_employee_upsert",
        summary=f"Employment record saved for {employee['employee_code']}",
        target_type="staff_employment",
        target_id=employee["employee_code"],
    )
    return employee


# ═════════════════════════════════════════════════════════════════════════════
#  Shift types & holidays
# ═════════════════════════════════════════════════════════════════════════════


_LIST_SHIFTS_INCLUDE_INACTIVE_DEFAULT = Query(False)
_LIST_SHIFTS_MODULE_DEFAULT = Depends(require_module)
_LIST_SHIFTS_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/shifts")
def list_shifts(
    include_inactive: bool = _LIST_SHIFTS_INCLUDE_INACTIVE_DEFAULT,
    _module=_LIST_SHIFTS_MODULE_DEFAULT,
    user=_LIST_SHIFTS_USER_DEFAULT,
):
    return {"shift_types": svc.list_shift_types(include_inactive=include_inactive)}


_UPSERT_SHIFT_MODULE_DEFAULT = Depends(require_module)
_UPSERT_SHIFT_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/shifts", response_model=None)
def upsert_shift(
    payload: ShiftTypeIn,
    request: Request,
    _module: None = _UPSERT_SHIFT_MODULE_DEFAULT,
    user: dict = _UPSERT_SHIFT_USER_DEFAULT,
) -> dict:
    _group_setting(facility_scope(user), "shift types")
    try:
        shift = svc.upsert_shift_type(**payload.model_dump())
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    # Editing a shift's hours can create conflicts in a roster that was already
    # written against the old hours. GET /roster/conflicts is what finds them,
    # and this audit line is how somebody knows to look.
    _audit(
        request,
        user,
        "workforce_shift_upsert",
        summary=f"Shift {shift['code']} set to {shift['start_time']}-{shift['end_time']}",
        target_type="duty_shift_type",
        target_id=shift["code"],
    )
    return shift


_LIST_HOLIDAYS_DATE_FROM_DEFAULT = Query(None)
_LIST_HOLIDAYS_DATE_TO_DEFAULT = Query(None)
_LIST_HOLIDAYS_MODULE_DEFAULT = Depends(require_module)
_LIST_HOLIDAYS_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/holidays")
def list_holidays(
    date_from: str | None = _LIST_HOLIDAYS_DATE_FROM_DEFAULT,
    date_to: str | None = _LIST_HOLIDAYS_DATE_TO_DEFAULT,
    _module=_LIST_HOLIDAYS_MODULE_DEFAULT,
    user=_LIST_HOLIDAYS_USER_DEFAULT,
):
    try:
        return {"holidays": svc.list_holidays(date_from=date_from, date_to=date_to)}
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc


_UPSERT_HOLIDAY_MODULE_DEFAULT = Depends(require_module)
_UPSERT_HOLIDAY_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/holidays", response_model=None)
def upsert_holiday(
    payload: HolidayIn,
    request: Request,
    _module: None = _UPSERT_HOLIDAY_MODULE_DEFAULT,
    user: dict = _UPSERT_HOLIDAY_USER_DEFAULT,
) -> dict:
    _group_setting(facility_scope(user), "holidays")
    try:
        holiday = svc.upsert_holiday(**payload.model_dump(), created_by=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "workforce_holiday_upsert",
        summary=f"Holiday {holiday['holiday_date']}: {holiday['name']}",
        target_type="hospital_holiday",
        target_id=holiday["holiday_date"],
    )
    return holiday


_DELETE_HOLIDAY_MODULE_DEFAULT = Depends(require_module)
_DELETE_HOLIDAY_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.delete("/holidays/{holiday_date}", response_model=None)
def delete_holiday(
    holiday_date: str,
    request: Request,
    _module: None = _DELETE_HOLIDAY_MODULE_DEFAULT,
    user: dict = _DELETE_HOLIDAY_USER_DEFAULT,
) -> dict:
    _group_setting(facility_scope(user), "holidays")
    try:
        removed = svc.delete_holiday(holiday_date)
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    if not removed:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"No holiday on {holiday_date}"},
        )
    _audit(
        request,
        user,
        "workforce_holiday_delete",
        summary=f"Holiday removed: {holiday_date}",
        target_type="hospital_holiday",
        target_id=holiday_date,
    )
    return {"deleted": True, "holiday_date": holiday_date}


# ═════════════════════════════════════════════════════════════════════════════
#  Duty roster
# ═════════════════════════════════════════════════════════════════════════════


_GET_ROSTER_DATE_FROM_DEFAULT = Query(...)
_GET_ROSTER_DATE_TO_DEFAULT = Query(...)
_GET_ROSTER_DEPARTMENT_DEFAULT = Query(None)
_GET_ROSTER_MODULE_DEFAULT = Depends(require_module)
_GET_ROSTER_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/roster", response_model=None)
def get_roster(
    date_from: str = _GET_ROSTER_DATE_FROM_DEFAULT,
    date_to: str = _GET_ROSTER_DATE_TO_DEFAULT,
    department: str | None = _GET_ROSTER_DEPARTMENT_DEFAULT,
    _module: None = _GET_ROSTER_MODULE_DEFAULT,
    user: dict = _GET_ROSTER_USER_DEFAULT,
) -> dict:
    """Who is on, by date and shift, with what actually happened beside it."""
    _department_required(facility_scope(user), department)
    try:
        return svc.get_roster(date_from=date_from, date_to=date_to, department=department)
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc


_ROSTER_CONFLICTS_DATE_FROM_DEFAULT = Query(...)
_ROSTER_CONFLICTS_DATE_TO_DEFAULT = Query(...)
_ROSTER_CONFLICTS_DEPARTMENT_DEFAULT = Query(None)
_ROSTER_CONFLICTS_MODULE_DEFAULT = Depends(require_module)
_ROSTER_CONFLICTS_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/roster/conflicts", response_model=None)
def roster_conflicts(
    date_from: str = _ROSTER_CONFLICTS_DATE_FROM_DEFAULT,
    date_to: str = _ROSTER_CONFLICTS_DATE_TO_DEFAULT,
    department: str | None = _ROSTER_CONFLICTS_DEPARTMENT_DEFAULT,
    _module: None = _ROSTER_CONFLICTS_MODULE_DEFAULT,
    user: dict = _ROSTER_CONFLICTS_USER_DEFAULT,
) -> dict:
    """Overlapping assignments already on the roster.

    `assign_roster` refuses to create one, so a hit here means the roster was
    written before this module existed, arrived by import, or a shift's hours
    were edited underneath existing assignments — the realistic case."""
    _department_required(facility_scope(user), department)
    try:
        conflicts = svc.find_conflicts(date_from=date_from, date_to=date_to, department=department)
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    return {"conflicts": conflicts, "conflict_count": len(conflicts)}


_ASSIGN_ROSTER_MODULE_DEFAULT = Depends(require_module)
_ASSIGN_ROSTER_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/roster", response_model=None)
def assign_roster(
    payload: RosterIn,
    request: Request,
    _module: None = _ASSIGN_ROSTER_MODULE_DEFAULT,
    user: dict = _ASSIGN_ROSTER_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_employee(facilities, payload.employee_code)
    _require_department(facilities, payload.department)
    if facilities is not None and payload.ward:
        with db_session() as db:
            ward_at = facility_stamp.ward_facility(db, ward=payload.ward)
        if ward_at is not None and ward_at not in facilities:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"No ward {payload.ward.strip()}"},
            )
    try:
        entry = svc.assign_roster(**payload.model_dump(), created_by=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "workforce_roster_assign",
        summary=(
            f"{entry['employee_code']} rostered on {entry['shift_code']} for {entry['roster_date']}"
        ),
        target_type="duty_roster",
        target_id=str(entry["id"]),
        metadata={"warnings": [w["kind"] for w in entry.get("warnings", [])]},
    )
    return entry


_CANCEL_ROSTER_MODULE_DEFAULT = Depends(require_module)
_CANCEL_ROSTER_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/roster/{roster_id}/cancel", response_model=None)
def cancel_roster(
    roster_id: int,
    request: Request,
    _module: None = _CANCEL_ROSTER_MODULE_DEFAULT,
    user: dict = _CANCEL_ROSTER_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    if facilities is not None:
        with db_session() as db:
            row = db.execute(
                "SELECT employee_code FROM duty_roster WHERE id = ?", (int(roster_id),)
            ).fetchone()
        if row is None or not _employee_ok(row[0], facilities, _employee_facilities()):
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"No roster entry {roster_id}"},
            )
    try:
        entry = svc.cancel_roster_entry(roster_id, actor=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "workforce_roster_cancel",
        summary=(
            f"Roster entry {roster_id} cancelled ({entry['employee_code']} / {entry['shift_code']})"
        ),
        target_type="duty_roster",
        target_id=str(roster_id),
    )
    return entry


# ═════════════════════════════════════════════════════════════════════════════
#  Attendance
# ═════════════════════════════════════════════════════════════════════════════


_LIST_ATTENDANCE_DATE_FROM_DEFAULT = Query(...)
_LIST_ATTENDANCE_DATE_TO_DEFAULT = Query(...)
_LIST_ATTENDANCE_DEPARTMENT_DEFAULT = Query(None)
_LIST_ATTENDANCE_EMPLOYEE_CODE_DEFAULT = Query(None)
_LIST_ATTENDANCE_MODULE_DEFAULT = Depends(require_module)
_LIST_ATTENDANCE_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/attendance", response_model=None)
def list_attendance(
    date_from: str = _LIST_ATTENDANCE_DATE_FROM_DEFAULT,
    date_to: str = _LIST_ATTENDANCE_DATE_TO_DEFAULT,
    department: str | None = _LIST_ATTENDANCE_DEPARTMENT_DEFAULT,
    employee_code: str | None = _LIST_ATTENDANCE_EMPLOYEE_CODE_DEFAULT,
    _module: None = _LIST_ATTENDANCE_MODULE_DEFAULT,
    user: dict = _LIST_ATTENDANCE_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_department(facilities, department)
    _require_employee(facilities, employee_code)
    try:
        rows = svc.list_attendance(
            date_from=date_from, date_to=date_to, department=department, employee_code=employee_code
        )
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    if facilities is not None:
        placed = _employee_facilities()
        rows = [r for r in rows if _employee_ok(r.get("employee_code"), facilities, placed)]
    return {"attendance": rows, "count": len(rows)}


_RECORD_ATTENDANCE_MODULE_DEFAULT = Depends(require_module)
_RECORD_ATTENDANCE_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/attendance", response_model=None)
def record_attendance(
    payload: AttendanceIn,
    request: Request,
    _module: None = _RECORD_ATTENDANCE_MODULE_DEFAULT,
    user: dict = _RECORD_ATTENDANCE_USER_DEFAULT,
) -> dict:
    """Record or CORRECT one employee's one day.

    A correction replaces the day rather than adding a second row — two rows for
    one day would break the summing identity the monthly summary rests on. The
    previous values are not lost: the audit trail carries the write, and the
    schema's unique index is what makes the replacement the only possibility."""
    _require_employee(facility_scope(user), payload.employee_code)
    try:
        row = svc.record_attendance(**payload.model_dump(), recorded_by=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "workforce_attendance_record",
        summary=(f"{row['employee_code']} marked {row['status']} on {row['attendance_date']}"),
        target_type="attendance_day",
        target_id=str(row["id"]),
        metadata={
            "status": row["status"],
            "worked_minutes": row["worked_minutes"],
            "overtime_minutes": row["overtime_minutes"],
            "source": row["source"],
        },
    )
    return row


_ATTENDANCE_SUMMARY_YEAR_DEFAULT = Query(...)
_ATTENDANCE_SUMMARY_MONTH_DEFAULT = Query(...)
_ATTENDANCE_SUMMARY_EMPLOYEE_CODE_DEFAULT = Query(None)
_ATTENDANCE_SUMMARY_DEPARTMENT_DEFAULT = Query(None)
_ATTENDANCE_SUMMARY_MODULE_DEFAULT = Depends(require_module)
_ATTENDANCE_SUMMARY_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/attendance/summary", response_model=None)
def attendance_summary(
    year: int = _ATTENDANCE_SUMMARY_YEAR_DEFAULT,
    month: int = _ATTENDANCE_SUMMARY_MONTH_DEFAULT,
    employee_code: str | None = _ATTENDANCE_SUMMARY_EMPLOYEE_CODE_DEFAULT,
    department: str | None = _ATTENDANCE_SUMMARY_DEPARTMENT_DEFAULT,
    _module: None = _ATTENDANCE_SUMMARY_MODULE_DEFAULT,
    user: dict = _ATTENDANCE_SUMMARY_USER_DEFAULT,
) -> dict:
    """The monthly muster. One employee when `employee_code` is given, otherwise
    everybody (optionally in one department).

    Every row carries its own `identity` block — the bucket sum against the days
    in the period — so the screen can show that the arithmetic closes rather
    than asking anyone to assume it."""
    facilities = facility_scope(user)
    if employee_code:
        _require_employee(facilities, employee_code)
    else:
        _department_required(facilities, department)
    try:
        if employee_code:
            return {
                "employees": [
                    svc.monthly_summary(employee_code=employee_code, year=year, month=month)
                ]
            }
        return svc.period_summaries(year=year, month=month, department=department)
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc


# ═════════════════════════════════════════════════════════════════════════════
#  Payroll export
# ═════════════════════════════════════════════════════════════════════════════


_PREVIEW_PAYROLL_EXPORT_YEAR_DEFAULT = Query(...)
_PREVIEW_PAYROLL_EXPORT_MONTH_DEFAULT = Query(...)
_PREVIEW_PAYROLL_EXPORT_DEPARTMENT_DEFAULT = Query(None)
_PREVIEW_PAYROLL_EXPORT_MODULE_DEFAULT = Depends(require_module)
_PREVIEW_PAYROLL_EXPORT_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/payroll-export", response_model=None)
def preview_payroll_export(
    year: int = _PREVIEW_PAYROLL_EXPORT_YEAR_DEFAULT,
    month: int = _PREVIEW_PAYROLL_EXPORT_MONTH_DEFAULT,
    department: str | None = _PREVIEW_PAYROLL_EXPORT_DEPARTMENT_DEFAULT,
    _module: None = _PREVIEW_PAYROLL_EXPORT_MODULE_DEFAULT,
    user: dict = _PREVIEW_PAYROLL_EXPORT_USER_DEFAULT,
) -> dict:
    """Preview the file WITHOUT recording a run.

    Deliberately side-effect free: `payroll_export_runs` is the record of what
    was actually sent to the bureau, and a GET that somebody refreshed three
    times would put three sends in it. Generating the real file is a POST."""
    _department_required(facility_scope(user), department)
    try:
        return svc.build_payroll_export(year=year, month=month, department=department)
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc


_GENERATE_PAYROLL_EXPORT_MODULE_DEFAULT = Depends(require_module)
_GENERATE_PAYROLL_EXPORT_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/payroll-export", response_model=None)
def generate_payroll_export(
    payload: ExportIn,
    request: Request,
    _module: None = _GENERATE_PAYROLL_EXPORT_MODULE_DEFAULT,
    user: dict = _GENERATE_PAYROLL_EXPORT_USER_DEFAULT,
) -> dict:
    """Generate the CSV and RECORD the run (checksum, header order, row count).

    Returns the text rather than a file stream so the caller gets the checksum
    and the warnings in the same response it saves — a browser that downloaded
    a stream would have the file and no way to prove which run it was."""
    _department_required(facility_scope(user), payload.department)
    try:
        export = svc.build_payroll_export(
            year=payload.year, month=payload.month, department=payload.department
        )
        csv_text = svc.render_payroll_csv(export)
        run = svc.record_export_run(
            export, csv_text, generated_by=user.get("id"), notes=payload.notes
        )
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "workforce_payroll_export",
        summary=(
            f"Payroll export {export['file_name']} generated "
            f"({export['employee_count']} employees, "
            f"{export['incomplete_employee_count']} with unrecorded days)"
        ),
        target_type="payroll_export_run",
        target_id=str(run["id"]),
        metadata={
            "sha256": run["sha256"],
            "period_start": export["period_start"],
            "period_end": export["period_end"],
        },
    )
    return {
        "run": run,
        "file_name": export["file_name"],
        "csv": csv_text,
        "sha256": run["sha256"],
        "columns": export["columns"],
        "employee_count": export["employee_count"],
        "incomplete_employee_count": export["incomplete_employee_count"],
        "warnings": export["warnings"],
        "statutory_note": export["statutory_note"],
    }


_PAYROLL_EXPORT_RUNS_LIMIT_DEFAULT = Query(25, ge=1, le=200)
_PAYROLL_EXPORT_RUNS_MODULE_DEFAULT = Depends(require_module)
_PAYROLL_EXPORT_RUNS_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/payroll-export/runs", response_model=None)
def payroll_export_runs(
    limit: int = _PAYROLL_EXPORT_RUNS_LIMIT_DEFAULT,
    _module: None = _PAYROLL_EXPORT_RUNS_MODULE_DEFAULT,
    user: dict = _PAYROLL_EXPORT_RUNS_USER_DEFAULT,
) -> dict:
    """What has been sent, and the checksum of each file. A payroll dispute
    three months later is answered from here. A facility-limited reader sees
    the runs of their facility's departments; a run over the whole group is
    shown only to a group-wide account."""
    facilities = facility_scope(user)
    runs = svc.list_export_runs(limit=limit if facilities is None else 200)
    if facilities is not None:
        with db_session() as db:
            runs = [
                r
                for r in runs
                if r.get("department")
                and facility_stamp.department_facility(db, r["department"]) in facilities
            ][:limit]
    return {"runs": runs}


_DOWNLOAD_PAYROLL_EXPORT_YEAR_DEFAULT = Query(...)
_DOWNLOAD_PAYROLL_EXPORT_MONTH_DEFAULT = Query(...)
_DOWNLOAD_PAYROLL_EXPORT_DEPARTMENT_DEFAULT = Query(None)
_DOWNLOAD_PAYROLL_EXPORT_MODULE_DEFAULT = Depends(require_module)
_DOWNLOAD_PAYROLL_EXPORT_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/payroll-export.csv", response_class=PlainTextResponse)
def download_payroll_export(
    year: int = _DOWNLOAD_PAYROLL_EXPORT_YEAR_DEFAULT,
    month: int = _DOWNLOAD_PAYROLL_EXPORT_MONTH_DEFAULT,
    department: str | None = _DOWNLOAD_PAYROLL_EXPORT_DEPARTMENT_DEFAULT,
    _module: None = _DOWNLOAD_PAYROLL_EXPORT_MODULE_DEFAULT,
    user: dict = _DOWNLOAD_PAYROLL_EXPORT_USER_DEFAULT,
) -> PlainTextResponse:
    """The file, as a download, for an operator with curl and no browser.

    Like the JSON preview this does NOT record a run — the same reason, and the
    same consequence: use POST /payroll-export for the copy that goes to the
    bureau, so the checksum of what was sent exists."""
    _department_required(facility_scope(user), department)
    try:
        export = svc.build_payroll_export(year=year, month=month, department=department)
        csv_text = svc.render_payroll_csv(export)
    except svc.WorkforceError as exc:
        raise _fail(exc) from exc
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{export["file_name"]}"'},
    )
