"""Turnaround times: endpoints under /api/lab.

The package router in routes/lab/__init__.py adds the /api/lab prefix. This
router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. Nothing here starts
new work.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /tat                                  lab.read

Built in lis-18 (W7-I), both lab.read, plain def:
  GET /tat?from=YYYY-MM-DD&to=YYYY-MM-DD
        The turnaround dashboard for whole hospital-local days (default the
        last seven, today included; at most 92): median, 90th percentile and
        percent within target by section and priority and by day, time to
        critical acknowledgement, the live overdue list and the open
        exceptions count (services/lab_tat_service). The overdue list names
        patients, so the read is audited as phi_access 'lab_tat_viewed'
        (fail-closed: 503 and nothing shown when the row cannot be written).
  GET /tat/export?from=&to=
        The period as CSV, one row per test reported, with no patient name or
        UHID (the accession links a row to the record). Audited as phi_access
        'lab_tat_exported', fail-closed, before the file leaves.
A department account reads only its own department's patients; the
laboratory roles have no department and read hospital-wide (ruling LIS-R12).
"""

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import LAB_READ, department_scope, log_phi_read, require_permission
from services.audit_service import AuditWriteError

from routes.opt_in_gates import require_lab_module
from services import lab_tat_service

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": (
                "The turnaround times could not be shown because the audit trail is "
                "not available. Try again in a moment."
            ),
        },
    )


def _period(first: str | None, last: str | None):
    try:
        return lab_tat_service.resolve_period(first, last)
    except lab_tat_service.LabTatError as err:
        raise HTTPException(
            status_code=err.status, detail={"error": err.code, "message": err.message}
        ) from err


_GET_TAT_FIRST_DEFAULT = Query(None, alias="from", max_length=10)
_GET_TAT_LAST_DEFAULT = Query(None, alias="to", max_length=10)
_GET_TAT_USER_DEFAULT = Depends(require_permission(LAB_READ))


@router.get("/tat")
def get_tat(
    request: Request,
    first: str | None = _GET_TAT_FIRST_DEFAULT,
    last: str | None = _GET_TAT_LAST_DEFAULT,
    user: dict = _GET_TAT_USER_DEFAULT,
):
    first_day, last_day = _period(first, last)
    dept = department_scope(user)
    with db_session() as db:
        board = lab_tat_service.dashboard(db, first=first_day, last=last_day, dept=dept)
    overdue = board["overdue"]["items"]
    patient_ids = sorted({o["patient"]["id"] for o in overdue if o["patient"]["id"]})
    try:
        log_phi_read(
            request=request,
            user=user,
            action_type="lab_tat_viewed",
            summary_message=(
                f"Laboratory turnaround times viewed "
                f"({first_day.isoformat()} to {last_day.isoformat()}, "
                f"{len(overdue)} overdue)"
            ),
            target_type="lab_tat",
            target_id=f"{first_day.isoformat()}..{last_day.isoformat()}",
            metadata={
                "reported": board["overall"]["count"],
                "overdue": len(overdue),
                "patient_count": len(patient_ids),
                "patient_ids": patient_ids[:200],
            },
        )
    except AuditWriteError:
        raise _audit_unavailable() from None
    return {**board, "scope": {"department": dept, "hospital_wide": dept is None}}


_EXPORT_TAT_FIRST_DEFAULT = Query(None, alias="from", max_length=10)
_EXPORT_TAT_LAST_DEFAULT = Query(None, alias="to", max_length=10)
_EXPORT_TAT_USER_DEFAULT = Depends(require_permission(LAB_READ))


@router.get("/tat/export")
def export_tat(
    request: Request,
    first: str | None = _EXPORT_TAT_FIRST_DEFAULT,
    last: str | None = _EXPORT_TAT_LAST_DEFAULT,
    user: dict = _EXPORT_TAT_USER_DEFAULT,
):
    first_day, last_day = _period(first, last)
    dept = department_scope(user)
    with db_session() as db:
        export = lab_tat_service.export_csv(db, first=first_day, last=last_day, dept=dept)
    try:
        log_phi_read(
            request=request,
            user=user,
            action_type="lab_tat_exported",
            summary_message=(
                f"Laboratory turnaround times exported "
                f"({first_day.isoformat()} to {last_day.isoformat()}, "
                f"{export['rows']} tests)"
            ),
            target_type="lab_tat",
            target_id=f"{first_day.isoformat()}..{last_day.isoformat()}",
            metadata={
                "rows": export["rows"],
                "patient_count": len(export["patient_ids"]),
                "patient_ids": export["patient_ids"][:200],
                "specimen_count": len(export["specimen_ids"]),
            },
        )
    except AuditWriteError:
        raise _audit_unavailable() from None
    return {
        "filename": export["filename"],
        "content_type": export["content_type"],
        "rows": export["rows"],
        "csv": export["csv"],
    }
