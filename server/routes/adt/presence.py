"""Leave and absconded (ADT): endpoints under /api/adt.

The package router in routes/adt/__init__.py adds the /api/adt prefix and the
ORB_ADT gate; this router has neither, so every endpoint added here inherits
both (no session is 401, and every route is 404 while the module is off).

Built in adt-10 (W7-D):
  POST /admissions/{admission_id}/leave        the patient goes on leave
  POST /admissions/{admission_id}/leave-end    back on the ward from leave
  POST /admissions/{admission_id}/absconded    gone without leave, or not back from leave
  POST /admissions/{admission_id}/returned     an absconded patient is back

Each needs adt.presence or patient.write, the patient inside the caller's
department (404 outside it, the attempt audited) and an Idempotency-Key
header. Ruling ADT-S7 is held: the strictest option is built, so only the
administrator (adt.presence) and the doctors (patient.write) record presence;
nurses and ward clerks hold neither and are refused (403) until Samuel
confirms. The bed stays held. The write, its movement and its fail-closed
patient_movement audit commit together (503 audit_unavailable rolls it back).

Display only (adt-10): no obs timer, medication list, escalation, sepsis
auto-start or simulator reads presence until adt-11 (ruling ADT-S4).

Every handler is a plain def (FastAPI threadpools it); the work runs in one
immediate session in services/adt/presence.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from permissions import ADT_PRESENCE, PATIENT_WRITE, require_any_permission
from routes.adt.end_stay import _meta, _scoped_admission
from services.adt import core as adt_core
from services.adt import presence as adt_presence
from services.audit_service import AuditWriteError

router = APIRouter()


class PresenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Why: required for a leave (who allowed it and where the patient goes)
    #: and for absconding (how and when the patient was found missing, and who
    #: was told). Optional for a return.
    reason: Optional[str] = None
    #: When it happened; now when omitted. Up to ORB_ADT_BACKTIME_HOURS back,
    #: never before the admission or the patient's last leave or return.
    occurred_at: Optional[str] = None
    notes: Optional[str] = None


class LeaveIn(PresenceIn):
    #: When the patient is expected back (optional; recorded on the movement).
    expected_return_at: Optional[str] = None


def _audit_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={
        "error": "audit_unavailable",
        "message": "The record of this could not be written, so nothing was changed. "
                   "Try again in a moment.",
    })


def _run(fn, admission_id: str, body: PresenceIn, request: Request, user: dict,
         idempotency_key: Optional[str], **extra):
    _scoped_admission(admission_id, user, request)
    try:
        return fn(admission_id, actor=user, reason=body.reason, occurred_at=body.occurred_at,
                  notes=body.notes, idempotency_key=idempotency_key,
                  request_meta=_meta(request), **extra)
    except adt_core.AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=adt_core.http_detail(err))
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/admissions/{admission_id}/leave")
def start_leave(
    admission_id: str,
    body: LeaveIn,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_any_permission(ADT_PRESENCE, PATIENT_WRITE)),
):
    """The patient goes on leave; the bed stays theirs. A reason is required.
    409 already_on_leave while a leave is open, patient_absconded when the
    patient is recorded absconded."""
    return _run(adt_presence.start_leave, admission_id, body, request, user, idempotency_key,
                expected_return_at=body.expected_return_at)


@router.post("/admissions/{admission_id}/leave-end")
def end_leave(
    admission_id: str,
    body: PresenceIn,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_any_permission(ADT_PRESENCE, PATIENT_WRITE)),
):
    """The patient is back on the ward from leave. 409 not_on_leave when no
    leave is open."""
    return _run(adt_presence.end_leave, admission_id, body, request, user, idempotency_key)


@router.post("/admissions/{admission_id}/absconded")
def mark_absconded(
    admission_id: str,
    body: PresenceIn,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_any_permission(ADT_PRESENCE, PATIENT_WRITE)),
):
    """The patient has gone without leave, or did not come back from leave.
    What is known is required as the reason. The stay is not ended here: an
    absconded stay is closed through POST /admissions/{id}/end (ADT-S3)."""
    return _run(adt_presence.mark_absconded, admission_id, body, request, user, idempotency_key)


@router.post("/admissions/{admission_id}/returned")
def mark_returned(
    admission_id: str,
    body: PresenceIn,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_any_permission(ADT_PRESENCE, PATIENT_WRITE)),
):
    """An absconded patient is back on the ward. 409 not_absconded otherwise."""
    return _run(adt_presence.mark_returned, admission_id, body, request, user, idempotency_key)
