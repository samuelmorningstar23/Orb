"""Laboratory reports: endpoints under /api/lab.

The package router in routes/lab/__init__.py adds the /api/lab prefix. This
router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining (a report of released
work is finished work, ruling LIS-R21). Nothing here starts new work.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /reports/{accession}                  lab.read
  GET  /reports/patient/{patient_id}         lab.read, or require_patient_access(order.read)

Built in lis-14 (W7-I). Every handler is a plain def; every answer that names
a patient is audited through log_phi_read (phi_access, fail-closed: when the
row cannot be written the answer is 503 and nothing is shown):

  GET /reports                     lab.read. The laboratory's list: the samples
                                   with a result released on one hospital-local
                                   day (?day=YYYY-MM-DD, default today), or every
                                   report of one patient by exact UHID (?uhid=).
                                   Hospital-wide for an account with no
                                   department; department-scoped otherwise.
                                   Audited 'lab_reports_listed'.
  GET /reports/patient/{patient_id}
                                   lab.read or order.read, with the patient
                                   scope of require_patient_access (an
                                   out-of-ward clinician gets 404). Audited
                                   'lab_patient_reports_listed' by the guard.
  GET /reports/{accession}         lab.read or order.read. One sample's report;
                                   a ward account reads only its own
                                   department's patients (404 otherwise, the
                                   same answer as an unknown sample). Audited
                                   'lab_report_viewed'.
  GET /reports/{accession}/print   The same report as it is printed, with the
                                   time and the person printing. Audited
                                   'lab_report_printed'.

The report shows released values only (ruling LIS-R4): a test that is not
released is 'Pending' with no value, whatever the bench has entered.
"""

import uuid
from datetime import date

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import (
    LAB_READ,
    ORDER_READ,
    department_scope,
    log_phi_read,
    require_any_permission,
    require_patient_access,
    require_permission,
)
from services.audit_service import AuditWriteError, log_audit_event
from services.clinical_time import utc_iso_now
from services.hospital_time import hospital_today

from routes.opt_in_gates import require_lab_module
from services import lab_report_service

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])


def _refusal(err: lab_report_service.LabReportError) -> HTTPException:
    return HTTPException(status_code=err.status, detail={"error": err.code, "message": err.message})


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": (
                "This report could not be shown because the audit trail is not "
                "available. Try again in a moment."
            ),
        },
    )


def _scope_denied(request: Request, user: dict, patient_id: str, accession: str) -> None:
    """Record a cross-ward attempt before the 404, as require_patient_access
    does, so a probe for another ward's sample is visible in the trail."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="patient_scope_denied",
        outcome="denied",
        summary_message=(
            f"Laboratory report {accession} is for a patient outside the "
            f"caller's department ({department_scope(user)})"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type="lab_report",
        target_id=accession,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"department": department_scope(user), "scope_source": "authenticated_user"},
    )


def _one_report(accession: str, request: Request, user: dict) -> dict:
    dept = department_scope(user)
    try:
        with db_session() as db:
            return lab_report_service.report_for(db, accession, dept=dept)
    except lab_report_service.LabReportError as err:
        out_of_scope = err.extra.get("out_of_scope_patient_id")
        if out_of_scope:
            _scope_denied(request, user, out_of_scope, str(accession))
        raise _refusal(err) from err


def _audit_read(
    request: Request,
    user: dict,
    report: dict,
    *,
    action: str,
    summary: str,
    extra: dict | None = None,
) -> None:
    try:
        log_phi_read(
            request=request,
            user=user,
            action_type=action,
            summary_message=summary,
            patient_id=report["patient"]["id"],
            target_type="lab_report",
            target_id=report["accession"],
            metadata={
                "status": report["status"],
                "amended": report["amended"],
                "result_ids": [i["result"]["id"] for i in report["items"] if i.get("result")],
                **(extra or {}),
            },
        )
    except AuditWriteError:
        raise _audit_unavailable() from None


def _parse_day(text: str | None) -> date:
    if not text:
        return hospital_today()
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise HTTPException(
            status_code=422, detail={"error": "bad_day", "message": "Give the day as YYYY-MM-DD."}
        ) from None


_LIST_REPORTS_DAY_DEFAULT = Query(None, max_length=10)
_LIST_REPORTS_UHID_DEFAULT = Query(None, max_length=64)
_LIST_REPORTS_USER_DEFAULT = Depends(require_permission(LAB_READ))


@router.get("/reports")
def list_reports(
    request: Request,
    day: str | None = _LIST_REPORTS_DAY_DEFAULT,
    uhid: str | None = _LIST_REPORTS_UHID_DEFAULT,
    user: dict = _LIST_REPORTS_USER_DEFAULT,
):
    """The laboratory's report list: one hospital-local day, or one patient by
    exact UHID. The audit row records the patients actually returned."""
    dept = department_scope(user)
    wanted = _parse_day(day) if not uhid else None
    with db_session() as db:
        if uhid:
            patient = lab_report_service.patient_by_uhid(db, uhid, dept=dept)
            reports = lab_report_service.reports_for_patient(db, patient["id"]) if patient else []
        else:
            assert wanted is not None
            reports = lab_report_service.reports_for_day(db, wanted, dept=dept)
    patient_ids = sorted({r["patient"]["id"] for r in reports})
    if reports:
        try:
            log_phi_read(
                request=request,
                user=user,
                action_type="lab_reports_listed",
                summary_message=f"Laboratory reports listed ({len(reports)})",
                target_type="lab_reports",
                target_id=(
                    f"uhid:{uhid.strip()}"
                    if uhid
                    else wanted.isoformat()
                    if wanted is not None
                    else ""
                ),
                metadata={
                    "count": len(reports),
                    "patient_count": len(patient_ids),
                    "patient_ids": patient_ids[:200],
                    "by": "uhid" if uhid else "day",
                },
            )
        except AuditWriteError:
            raise _audit_unavailable() from None
    return {
        "reports": reports,
        "day": wanted.isoformat() if wanted else None,
        "uhid": uhid.strip() if uhid else None,
        "cap": lab_report_service.REPORT_LIST_CAP,
        "scope": {"department": dept, "hospital_wide": dept is None},
    }


_LIST_PATIENT_REPORTS_USER_DEFAULT = Depends(
    require_patient_access(
        LAB_READ, ORDER_READ, any_of=True, audit_action="lab_patient_reports_listed"
    )
)


@router.get("/reports/patient/{patient_id}")
def list_patient_reports(
    patient_id: str,
    user: dict = _LIST_PATIENT_REPORTS_USER_DEFAULT,
):
    """Every laboratory report of one patient, newest collection first. The
    guard checks the ward (404 outside it) and writes the read audit row."""
    with db_session() as db:
        exists = db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Patient not found")
        reports = lab_report_service.reports_for_patient(db, patient_id)
    return {"patient_id": patient_id, "reports": reports, "cap": lab_report_service.REPORT_LIST_CAP}


_GET_REPORT_USER_DEFAULT = Depends(require_any_permission(LAB_READ, ORDER_READ))


@router.get("/reports/{accession}")
def get_report(
    accession: str,
    request: Request,
    user: dict = _GET_REPORT_USER_DEFAULT,
):
    report = _one_report(accession, request, user)
    _audit_read(
        request,
        user,
        report,
        action="lab_report_viewed",
        summary=f"Laboratory report {report['accession']} viewed",
    )
    return {"report": report}


_PRINT_REPORT_USER_DEFAULT = Depends(require_any_permission(LAB_READ, ORDER_READ))


@router.get("/reports/{accession}/print")
def print_report(
    accession: str,
    request: Request,
    user: dict = _PRINT_REPORT_USER_DEFAULT,
):
    """The report as it is printed: the same content, with when and by whom.
    Audited as a print, fail-closed, before the body leaves."""
    report = _one_report(accession, request, user)
    printed_at = utc_iso_now()
    _audit_read(
        request,
        user,
        report,
        action="lab_report_printed",
        summary=f"Laboratory report {report['accession']} printed",
        extra={"printed_at": printed_at},
    )
    with db_session() as db:
        site_name = _site_name()
        printer = db.execute("SELECT name FROM users WHERE id = ?", (user.get("id"),)).fetchone()
    return {
        "report": {
            **report,
            "printed_at": printed_at,
            "printed_by": {
                "name": (printer["name"] if printer else None)
                or user.get("name")
                or "Name not on file"
            },
            "hospital": {"name": site_name},
        }
    }


def _site_name() -> str | None:
    """The hospital's name from the site settings, never invented."""
    try:
        from services.site_config_service import current_site_config

        return current_site_config().site_name or None
    except Exception:  # the settings file is optional
        return None
