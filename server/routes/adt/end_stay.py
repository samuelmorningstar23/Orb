"""End of stay and reopen (ADT): endpoints under /api/adt.

The package router in routes/adt/__init__.py adds the /api/adt prefix and the
ORB_ADT gate; this router has neither, so every endpoint added here inherits
both (no session is 401, and every route is 404 while the module is off).

Built in adt-8a (W3-B):
  POST /admissions/{admission_id}/end       discharge.generate plus scope, typed MRN read-back (confirm_mrn)

Built in adt-8b (W4-B):
  POST /admissions/{admission_id}/reopen    discharge.generate plus scope through the ENDED
                                            admission's own department, reason and confirm_mrn;
                                            only an end with no discharge summary

No ADT permission ends a stay. Death, LAMA and DAMA stay doctor-only (ruling
ADT-S3, strictest option): discharge.generate is held by the doctor alone, so
a nurse, a ward clerk or an administrator gets 403 with the denial audited. A
patient outside the doctor's department is 404, never 403, with the attempt
audited, exactly as permissions.require_patient_access answers. Open
escalation chains are listed in the response, never closed (R009, held).

Every handler is a plain def (FastAPI threadpools it); the work runs in one
immediate session in services/adt/end_stay.end_stay.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from permissions import DISCHARGE_GENERATE, department_scope, patient_in_scope, require_permission
from database import db_session
from services.adt import end_stay as adt_end
from services.adt.core import AdtError, http_detail
from services.audit_service import AuditWriteError, log_audit_event

router = APIRouter()


class EndStayIn(BaseModel):
    #: routine, referred_out, transferred_out, dama, lama, absconded or death.
    end_type: str
    #: The patient's MRN, typed back from the wristband. Refused on mismatch.
    confirm_mrn: str
    #: When the stay ended; now when omitted. Up to ORB_ADT_BACKTIME_HOURS back.
    occurred_at: Optional[str] = None
    #: Required for LAMA, DAMA and absconded.
    reason: Optional[str] = None
    notes: Optional[str] = None


def _meta(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _scoped_admission(admission_id: str, user: dict, request: Request) -> dict:
    """The admission, or 404 when it does not exist or its patient is outside
    the caller's department. The out-of-scope attempt is audited before the
    404, as require_patient_access does for a patient id on the path."""
    found = adt_end.patient_for_admission(admission_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Admission not found")
    dept = department_scope(user)
    if dept is None:
        return found
    with db_session() as db:
        in_scope = patient_in_scope(db, found["patient_id"], dept)
    if not in_scope:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="patient_scope_denied",
            outcome="denied",
            summary_message=(f"Patient {found['patient_id']} is outside the caller's department "
                             f"({dept})"),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=found["patient_id"],
            target_type="admission",
            target_id=str(admission_id),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={"department": dept, "scope_source": "authenticated_user"},
        )
        raise HTTPException(status_code=404, detail="Admission not found")
    return found


@router.post("/admissions/{admission_id}/end")
def end_admission(
    admission_id: str,
    body: EndStayIn,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_permission(DISCHARGE_GENERATE)),
):
    """Record how a stay ended. The MRN must be typed back (409
    read_back_mismatch writes nothing). Writes the end type, the discharge
    movement, the NABH disposition (never doubled) and a clinical_access
    audit in one transaction; on a death deactivates the portal login and
    issues no discharge link. Lists open escalation chains and the bundles it
    closed."""
    _scoped_admission(admission_id, user, request)
    try:
        return adt_end.end_stay(
            admission_id,
            end_type=(body.end_type or "").strip().lower(),
            actor=user,
            confirm_mrn=body.confirm_mrn,
            occurred_at=body.occurred_at,
            reason=body.reason,
            notes=body.notes,
            idempotency_key=idempotency_key,
            request_meta=_meta(request),
        )
    except AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=http_detail(err))


class ReopenIn(BaseModel):
    #: Why the end was recorded in error. Required; it is part of the record.
    reason: str
    #: The patient's MRN, typed back from the wristband. Refused on mismatch.
    confirm_mrn: str


def _scoped_ended_admission(admission_id: str, user: dict, request: Request) -> dict:
    """The admission, or 404 when it does not exist or its OWN department is
    not the caller's. Scope follows the ended admission (design adt-8b), not
    whichever stay is the patient's latest, so a doctor can reopen only a
    stay their department treated. The out-of-scope attempt is audited
    before the 404."""
    found = adt_end.patient_for_admission(admission_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Admission not found")
    dept = department_scope(user)
    if dept is None or (found.get("department") or "") == dept:
        return found
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="patient_scope_denied",
        outcome="denied",
        summary_message=(f"Admission {admission_id} belongs to {found.get('department')}, "
                         f"outside the caller's department ({dept})"),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=found["patient_id"],
        target_type="admission",
        target_id=str(admission_id),
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"department": dept, "scope_source": "ended_admission"},
    )
    raise HTTPException(status_code=404, detail="Admission not found")


@router.post("/admissions/{admission_id}/reopen")
def reopen_admission(
    admission_id: str,
    body: ReopenIn,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_permission(DISCHARGE_GENERATE)),
):
    """Reopen a stay whose end was recorded in error. The MRN must be typed
    back (409 read_back_mismatch writes nothing) and a reason given. Only an
    end with no discharge summary is reopened (409 summary_on_file
    otherwise). Restores the stay, takes its bed back when free (else the
    patient awaits a bed), writes a correction movement pointing at the
    discharge movement, voids the NABH disposition, restores a portal login
    the end deactivated, lists the bundles the end cancelled without
    restoring them, and audits clinical_access 'stay_reopened', in one
    transaction."""
    _scoped_ended_admission(admission_id, user, request)
    try:
        return adt_end.reopen_stay(
            admission_id,
            actor=user,
            confirm_mrn=body.confirm_mrn,
            reason=body.reason,
            idempotency_key=idempotency_key,
            request_meta=_meta(request),
        )
    except AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=http_detail(err))
    except AuditWriteError:
        raise HTTPException(status_code=503, detail={
            "error": "audit_unavailable",
            "message": "The record of this correction could not be written, so nothing was "
                       "changed. Try again in a moment.",
        })
