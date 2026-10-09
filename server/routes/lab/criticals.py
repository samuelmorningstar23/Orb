"""Critical results: endpoints under /api/lab.

The package router in routes/lab/__init__.py adds the /api/lab prefix. This
router carries require_lab_critical_surface: open while the laboratory is on
or draining, and while interfaced criticals are on without ORB_LAB (ruling
LIS-R22). Switching the laboratory off must never make an open critical
unreachable (LIS-R21). Handlers are plain def.

Go-live is blocked on ruling LIS-R20 (does this sit inside held R023?). A
Relay post, or any reply to it, never closes a critical (LIS-R3).

Built in lis-15 (W7-H), the laboratory's side
(services/lab_critical_service.py):
  GET  /criticals                     lab.read. The Critical results screen:
                                      every open critical, most overdue
                                      first, with its tier, due time,
                                      delivery to the case room ('Not
                                      delivered: no case room. Phone the
                                      ward.'), what has happened to the
                                      result since, and 'Patient discharged'
                                      for a patient who has gone home; then
                                      those closed in the last 24 hours.
                                      Hospital-wide for the laboratory roles,
                                      department-scoped for an account with a
                                      department. Audited as phi_access
                                      'lab_critical_list_viewed' with the
                                      patients returned.
  POST /criticals/{notification_id}/callback
                                      lab.critical_log. Close a critical with
                                      the laboratory's callback: the name and
                                      role of the person told, phone or in
                                      person, and their read-back (required).
  POST /criticals/{notification_id}/attempt
                                      lab.critical_log. Log a call that
                                      reached nobody; the critical stays
                                      open and its ladder keeps running.
Each write is one immediate transaction with a fail-closed clinical_access
audit row. Each names the value the screen showed (value_told, required): a
value changed since the screen loaded is refused 409 'value_changed' with
the value now told, and nothing is written, so a call about a value that has
since changed is never logged as a read-back of the new one.

Built in lis-16 (W8-G), the ward's side:
  GET  /criticals/ward                lab.critical_ack. The ward card on the
                                      nurse and doctor homes: the open
                                      criticals of the patients on the
                                      caller's ward (department_scope, the
                                      patient's active admission first, else
                                      the latest, so a transfer moves the
                                      card and a discharged patient's stays
                                      on the last ward without a bed). The
                                      laboratory's value, its own flag and
                                      the limit its pathologist agreed; no
                                      score and no severity of Orb's own.
                                      Audited as phi_access
                                      'lab_critical_ward_viewed' whenever a
                                      patient is returned.
  POST /criticals/{notification_id}/acknowledge
                                      lab.critical_ack. The ward's read-back
                                      acknowledgement (ruling LIS-R3 closure
                                      (ii)): the name of the person told, how
                                      (read on the ward screen, or a phone
                                      call from the laboratory) and a
                                      read-back (required). Closes the
                                      critical and stops its ladder. A
                                      patient outside the caller's ward is
                                      404. Names the value the card showed
                                      (value_told), checked like a callback's.

Interfaced criticals (lis-22, ORB_LAB_CRITICAL_INTERFACED) are raised by
the ingestion pipeline and use these same routes.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from database import db_session
from permissions import (LAB_CRITICAL_ACK, LAB_CRITICAL_LOG, LAB_READ, department_scope,
                         has_permission, log_phi_read, require_permission)
from routes.lab.orders import audit_unavailable, request_meta
from routes.opt_in_gates import require_lab_critical_surface
from services import lab_critical_service
from services.audit_service import AuditWriteError

router = APIRouter(dependencies=[Depends(require_lab_critical_surface)])


class CallbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient_name: str = Field(..., max_length=lab_critical_service.NAME_MAX)
    recipient_role: str = Field(..., max_length=40)
    method: Literal["phone", "in_person"]
    # Must be true: a callback without a read-back closes nothing.
    read_back: bool
    note: Optional[str] = Field(None, max_length=lab_critical_service.NOTE_MAX)
    # The value the screen showed as told to the ward, which is the value read
    # back. Required: the stale-screen guard, like `expected` on a result.
    value_told: str = Field(..., max_length=lab_critical_service.VALUE_TOLD_MAX)


class AttemptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["phone", "in_person"]
    note: str = Field(..., max_length=lab_critical_service.NOTE_MAX)
    # The value the screen showed: the call was about this value.
    value_told: str = Field(..., max_length=lab_critical_service.VALUE_TOLD_MAX)


class AcknowledgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The person who was told: typically the person acknowledging.
    recipient_name: str = Field(..., max_length=lab_critical_service.NAME_MAX)
    recipient_role: Optional[str] = Field(None, max_length=40)
    # Read on the ward screen, or a phone call from the laboratory.
    method: Literal["in_app", "phone"] = "in_app"
    # Who phoned from the laboratory, when it was a call.
    caller_name: Optional[str] = Field(None, max_length=lab_critical_service.NAME_MAX)
    # Must be true: an acknowledgement without a read-back closes nothing.
    read_back: bool
    note: Optional[str] = Field(None, max_length=lab_critical_service.NOTE_MAX)
    # The value the card showed, which is the value read back.
    value_told: str = Field(..., max_length=lab_critical_service.VALUE_TOLD_MAX)


def _refusal(err: lab_critical_service.LabCriticalError) -> HTTPException:
    return HTTPException(status_code=err.status,
                         detail={"error": err.code, "message": err.message, **err.extra})


def _write(call):
    try:
        return call()
    except lab_critical_service.LabCriticalError as err:
        raise _refusal(err)
    except AuditWriteError:
        raise audit_unavailable()


@router.get("/criticals")
def list_criticals(request: Request, user: dict = Depends(require_permission(LAB_READ))):
    """The laboratory's Critical results screen. The audit row records the
    patients actually returned."""
    dept = department_scope(user)
    with db_session() as db:
        body = lab_critical_service.list_for_lab(db, dept=dept)
    shown = body["open"] + body["recent"]
    patient_ids = sorted({n["patient"]["id"] for n in shown})
    log_phi_read(
        request=request, user=user,
        action_type="lab_critical_list_viewed",
        summary_message=(f"Laboratory critical results viewed ({len(body['open'])} open, "
                         f"{len(body['recent'])} recently closed)"),
        target_type="lab_critical_list",
        target_id="criticals",
        metadata={"open": len(body["open"]), "recent": len(body["recent"]),
                  "patient_count": len(patient_ids), "patient_ids": patient_ids[:200]},
    )
    return {**body, "can_log": has_permission(user.get("role", ""), LAB_CRITICAL_LOG),
            "scope": {"department": dept, "hospital_wide": dept is None}}


@router.post("/criticals/{notification_id}/callback")
def log_callback(notification_id: str, body: CallbackBody, request: Request,
                 user: dict = Depends(require_permission(LAB_CRITICAL_LOG))):
    return _write(lambda: lab_critical_service.log_callback(
        notification_id, actor=user, recipient_name=body.recipient_name,
        recipient_role=body.recipient_role, method=body.method, read_back=body.read_back,
        note=body.note, value_told=body.value_told, dept=department_scope(user),
        request_meta=request_meta(request)))


@router.post("/criticals/{notification_id}/attempt")
def log_attempt(notification_id: str, body: AttemptBody, request: Request,
                user: dict = Depends(require_permission(LAB_CRITICAL_LOG))):
    return _write(lambda: lab_critical_service.log_attempt(
        notification_id, actor=user, method=body.method, note=body.note,
        value_told=body.value_told, dept=department_scope(user),
        request_meta=request_meta(request)))


@router.get("/criticals/ward")
def ward_criticals(request: Request, user: dict = Depends(require_permission(LAB_CRITICAL_ACK))):
    """The ward card: open criticals of the patients on the caller's ward,
    scoped from the signed-in account, never from the request. The audit row
    records the patients returned; an empty card discloses nothing and
    writes none."""
    dept = department_scope(user)
    with db_session() as db:
        body = lab_critical_service.list_for_ward(db, dept=dept)
    patient_ids = sorted({n["patient"]["id"] for n in body["open"]})
    if patient_ids:
        try:
            log_phi_read(
                request=request, user=user,
                action_type="lab_critical_ward_viewed",
                summary_message=(f"Ward critical laboratory results viewed "
                                 f"({len(body['open'])} open)"),
                target_type="lab_critical_list",
                target_id="ward",
                metadata={"open": len(body["open"]), "patient_count": len(patient_ids),
                          "patient_ids": patient_ids[:200]},
            )
        except AuditWriteError:
            # Fail-closed: no audit row, no patient data.
            raise audit_unavailable()
    return {**body, "scope": {"department": dept, "hospital_wide": dept is None}}


@router.post("/criticals/{notification_id}/acknowledge")
def acknowledge(notification_id: str, body: AcknowledgeBody, request: Request,
                user: dict = Depends(require_permission(LAB_CRITICAL_ACK))):
    return _write(lambda: lab_critical_service.acknowledge(
        notification_id, actor=user, recipient_name=body.recipient_name,
        recipient_role=body.recipient_role, method=body.method, caller_name=body.caller_name,
        read_back=body.read_back, note=body.note, value_told=body.value_told,
        dept=department_scope(user), request_meta=request_meta(request)))
