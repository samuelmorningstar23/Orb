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
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

import config
from permissions import ADMIN_OVERVIEW, require_permission
from services import workforce_service as svc
from services.audit_service import log_audit_event

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


def _audit(request: Request, user: dict, action: str, *, summary: str,
           target_type: str, target_id: Optional[str] = None,
           metadata: Optional[dict] = None) -> None:
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
    department: Optional[str] = None
    designation: Optional[str] = None
    employment_type: str = "permanent"
    #: Optional link to an Orb login. Most of a hospital's payroll — housekeeping,
    #: security, kitchen, drivers — has no login and never will.
    user_id: Optional[str] = None
    date_of_joining: Optional[str] = None
    date_of_exit: Optional[str] = None
    #: 0 = Monday … 6 = Sunday. NULL means the day off ROTATES with the roster,
    #: in which case it must be recorded as attendance, never inferred.
    weekly_off_day: Optional[int] = None
    payroll_bureau_ref: Optional[str] = None
    is_active: bool = True
    notes: Optional[str] = None
    #: Set by the Add form: an existing code is refused (409), not overwritten.
    create_only: bool = False


class ShiftTypeIn(BaseModel):
    code: str
    name: str
    start_minute: int
    duration_minutes: int
    #: 'night' | 'evening' | null. Marks a shift a site's policy pays an
    #: allowance for. Orb COUNTS these; the amount is the bureau's.
    allowance_code: Optional[str] = None
    is_active: bool = True


class RosterIn(BaseModel):
    roster_date: str
    shift_code: str
    employee_code: str
    department: Optional[str] = None
    ward: Optional[str] = None
    assigned_role: Optional[str] = None
    status: str = "planned"
    notes: Optional[str] = None
    #: Short rest and same-day double shifts come back as warnings by default.
    #: Set false to make the server refuse them outright — see MIN_REST_MINUTES.
    allow_short_rest: bool = True


class AttendanceIn(BaseModel):
    employee_code: str
    attendance_date: str
    status: str
    shift_code: Optional[str] = None
    #: FULL ISO timestamps with offset, not times-of-day: a night shift's in and
    #: out fall on two calendar dates and Orb will not guess which.
    first_in_at: Optional[str] = None
    last_out_at: Optional[str] = None
    leave_type_code: Optional[str] = None
    source: str = "manual"
    remarks: Optional[str] = None


class HolidayIn(BaseModel):
    holiday_date: str
    name: str
    is_restricted: bool = False


class ExportIn(BaseModel):
    year: int
    month: int
    department: Optional[str] = None
    notes: Optional[str] = None


# ═════════════════════════════════════════════════════════════════════════════
#  Meta
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/meta")
def meta(
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
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

@router.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
    search: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    return {"employees": svc.list_employees(department=department,
                                            include_inactive=include_inactive,
                                            search=search)}


@router.get("/employees/{employee_code}")
def get_employee(
    employee_code: str,
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    try:
        return svc.get_employee(employee_code)
    except svc.WorkforceError as exc:
        raise _fail(exc)


@router.post("/employees")
def upsert_employee(
    payload: EmployeeIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    try:
        employee = svc.upsert_employee(**payload.model_dump(), created_by=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc)
    _audit(request, user, "workforce_employee_upsert",
           summary=f"Employment record saved for {employee['employee_code']}",
           target_type="staff_employment", target_id=employee["employee_code"])
    return employee


# ═════════════════════════════════════════════════════════════════════════════
#  Shift types & holidays
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/shifts")
def list_shifts(
    include_inactive: bool = Query(False),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    return {"shift_types": svc.list_shift_types(include_inactive=include_inactive)}


@router.post("/shifts")
def upsert_shift(
    payload: ShiftTypeIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    try:
        shift = svc.upsert_shift_type(**payload.model_dump())
    except svc.WorkforceError as exc:
        raise _fail(exc)
    # Editing a shift's hours can create conflicts in a roster that was already
    # written against the old hours. GET /roster/conflicts is what finds them,
    # and this audit line is how somebody knows to look.
    _audit(request, user, "workforce_shift_upsert",
           summary=f"Shift {shift['code']} set to {shift['start_time']}-{shift['end_time']}",
           target_type="duty_shift_type", target_id=shift["code"])
    return shift


@router.get("/holidays")
def list_holidays(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    try:
        return {"holidays": svc.list_holidays(date_from=date_from, date_to=date_to)}
    except svc.WorkforceError as exc:
        raise _fail(exc)


@router.post("/holidays")
def upsert_holiday(
    payload: HolidayIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    try:
        holiday = svc.upsert_holiday(**payload.model_dump(), created_by=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc)
    _audit(request, user, "workforce_holiday_upsert",
           summary=f"Holiday {holiday['holiday_date']}: {holiday['name']}",
           target_type="hospital_holiday", target_id=holiday["holiday_date"])
    return holiday


@router.delete("/holidays/{holiday_date}")
def delete_holiday(
    holiday_date: str,
    request: Request,
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    try:
        removed = svc.delete_holiday(holiday_date)
    except svc.WorkforceError as exc:
        raise _fail(exc)
    if not removed:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"No holiday on {holiday_date}"})
    _audit(request, user, "workforce_holiday_delete",
           summary=f"Holiday removed: {holiday_date}",
           target_type="hospital_holiday", target_id=holiday_date)
    return {"deleted": True, "holiday_date": holiday_date}


# ═════════════════════════════════════════════════════════════════════════════
#  Duty roster
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/roster")
def get_roster(
    date_from: str = Query(...),
    date_to: str = Query(...),
    department: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Who is on, by date and shift, with what actually happened beside it."""
    try:
        return svc.get_roster(date_from=date_from, date_to=date_to,
                              department=department)
    except svc.WorkforceError as exc:
        raise _fail(exc)


@router.get("/roster/conflicts")
def roster_conflicts(
    date_from: str = Query(...),
    date_to: str = Query(...),
    department: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Overlapping assignments already on the roster.

    `assign_roster` refuses to create one, so a hit here means the roster was
    written before this module existed, arrived by import, or a shift's hours
    were edited underneath existing assignments — the realistic case."""
    try:
        conflicts = svc.find_conflicts(date_from=date_from, date_to=date_to,
                                       department=department)
    except svc.WorkforceError as exc:
        raise _fail(exc)
    return {"conflicts": conflicts, "conflict_count": len(conflicts)}


@router.post("/roster")
def assign_roster(
    payload: RosterIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    try:
        entry = svc.assign_roster(**payload.model_dump(), created_by=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc)
    _audit(request, user, "workforce_roster_assign",
           summary=(f"{entry['employee_code']} rostered on {entry['shift_code']} "
                    f"for {entry['roster_date']}"),
           target_type="duty_roster", target_id=str(entry["id"]),
           metadata={"warnings": [w["kind"] for w in entry.get("warnings", [])]})
    return entry


@router.post("/roster/{roster_id}/cancel")
def cancel_roster(
    roster_id: int,
    request: Request,
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    try:
        entry = svc.cancel_roster_entry(roster_id, actor=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc)
    _audit(request, user, "workforce_roster_cancel",
           summary=(f"Roster entry {roster_id} cancelled "
                    f"({entry['employee_code']} / {entry['shift_code']})"),
           target_type="duty_roster", target_id=str(roster_id))
    return entry


# ═════════════════════════════════════════════════════════════════════════════
#  Attendance
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/attendance")
def list_attendance(
    date_from: str = Query(...),
    date_to: str = Query(...),
    department: Optional[str] = Query(None),
    employee_code: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    try:
        rows = svc.list_attendance(date_from=date_from, date_to=date_to,
                                   department=department, employee_code=employee_code)
    except svc.WorkforceError as exc:
        raise _fail(exc)
    return {"attendance": rows, "count": len(rows)}


@router.post("/attendance")
def record_attendance(
    payload: AttendanceIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Record or CORRECT one employee's one day.

    A correction replaces the day rather than adding a second row — two rows for
    one day would break the summing identity the monthly summary rests on. The
    previous values are not lost: the audit trail carries the write, and the
    schema's unique index is what makes the replacement the only possibility."""
    try:
        row = svc.record_attendance(**payload.model_dump(), recorded_by=user.get("id"))
    except svc.WorkforceError as exc:
        raise _fail(exc)
    _audit(request, user, "workforce_attendance_record",
           summary=(f"{row['employee_code']} marked {row['status']} on "
                    f"{row['attendance_date']}"),
           target_type="attendance_day", target_id=str(row["id"]),
           metadata={"status": row["status"], "worked_minutes": row["worked_minutes"],
                     "overtime_minutes": row["overtime_minutes"],
                     "source": row["source"]})
    return row


@router.get("/attendance/summary")
def attendance_summary(
    year: int = Query(...),
    month: int = Query(...),
    employee_code: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    """The monthly muster. One employee when `employee_code` is given, otherwise
    everybody (optionally in one department).

    Every row carries its own `identity` block — the bucket sum against the days
    in the period — so the screen can show that the arithmetic closes rather
    than asking anyone to assume it."""
    try:
        if employee_code:
            return {"employees": [svc.monthly_summary(
                employee_code=employee_code, year=year, month=month)]}
        return svc.period_summaries(year=year, month=month, department=department)
    except svc.WorkforceError as exc:
        raise _fail(exc)


# ═════════════════════════════════════════════════════════════════════════════
#  Payroll export
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/payroll-export")
def preview_payroll_export(
    year: int = Query(...),
    month: int = Query(...),
    department: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Preview the file WITHOUT recording a run.

    Deliberately side-effect free: `payroll_export_runs` is the record of what
    was actually sent to the bureau, and a GET that somebody refreshed three
    times would put three sends in it. Generating the real file is a POST."""
    try:
        return svc.build_payroll_export(year=year, month=month, department=department)
    except svc.WorkforceError as exc:
        raise _fail(exc)


@router.post("/payroll-export")
def generate_payroll_export(
    payload: ExportIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Generate the CSV and RECORD the run (checksum, header order, row count).

    Returns the text rather than a file stream so the caller gets the checksum
    and the warnings in the same response it saves — a browser that downloaded
    a stream would have the file and no way to prove which run it was."""
    try:
        export = svc.build_payroll_export(year=payload.year, month=payload.month,
                                          department=payload.department)
        csv_text = svc.render_payroll_csv(export)
        run = svc.record_export_run(export, csv_text,
                                    generated_by=user.get("id"), notes=payload.notes)
    except svc.WorkforceError as exc:
        raise _fail(exc)
    _audit(request, user, "workforce_payroll_export",
           summary=(f"Payroll export {export['file_name']} generated "
                    f"({export['employee_count']} employees, "
                    f"{export['incomplete_employee_count']} with unrecorded days)"),
           target_type="payroll_export_run", target_id=str(run["id"]),
           metadata={"sha256": run["sha256"], "period_start": export["period_start"],
                     "period_end": export["period_end"]})
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


@router.get("/payroll-export/runs")
def payroll_export_runs(
    limit: int = Query(25, ge=1, le=200),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    """What has been sent, and the checksum of each file. A payroll dispute
    three months later is answered from here."""
    return {"runs": svc.list_export_runs(limit=limit)}


@router.get("/payroll-export.csv", response_class=PlainTextResponse)
def download_payroll_export(
    year: int = Query(...),
    month: int = Query(...),
    department: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(require_permission(ADMIN_OVERVIEW)),
):
    """The file, as a download, for an operator with curl and no browser.

    Like the JSON preview this does NOT record a run — the same reason, and the
    same consequence: use POST /payroll-export for the copy that goes to the
    bureau, so the checksum of what was sent exists."""
    try:
        export = svc.build_payroll_export(year=year, month=month, department=department)
        csv_text = svc.render_payroll_csv(export)
    except svc.WorkforceError as exc:
        raise _fail(exc)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{export["file_name"]}"'},
    )
