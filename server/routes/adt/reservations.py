"""Bed reservations (ADT): endpoints under /api/adt.

The package router in routes/adt/__init__.py adds the /api/adt prefix and the
ORB_ADT gate; this router has neither, so every endpoint added here inherits
both (no session is 401, and every route is 404 while the module is off).

Built in adt-12 (W7-D):
  GET  /reservations?ward_id=                   adt.read: beds held now on the
                                                caller's wards, and held anywhere
                                                for the caller's own patients
  POST /reservations                            adt.bed or patient.write: hold a
                                                free bed (Idempotency-Key required)
  POST /reservations/{reservation_id}/release   adt.bed or patient.write: let it
                                                go, with a reason

Expiry from config.ADT_RESERVATION_MINUTES (120) and
ADT_RESERVATION_ELECTIVE_MINUTES (1440, elective and post-operative), ruling
ADT-P9. A bed held for a patient is placeable only by that patient's
admission (services/adt/core.locate_admission_on fulfils it); anyone else is
refused with words that never say who it is held for.

SCOPE. A bed carries no patient data, so any active ward's bed may be held
for a patient the caller names; a bed held for nobody named only on a ward of
the caller's own department (404 otherwise). The department is
department_scope(user), from the authenticated user, never from the body.
Every patient a reservation names must be inside it (404 outside it, the
attempt audited), on the way in and on release: on the way in the handler
checks the admission's patient and a body patient_id each on its own, and the
service checks the patient the row will name again inside its own
transaction. A reservation naming nobody is released by whoever made it, by an
account with no department, or by the department that owns the ward.

Every handler is a plain def (FastAPI threadpools it); the work runs in one
immediate session in services/adt/reservations.py, with its fail-closed
patient_movement audit (503 audit_unavailable rolls it back).
"""

from __future__ import annotations

import uuid

from database import db_session
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from permissions import (
    ADT_BED,
    ADT_READ,
    PATIENT_WRITE,
    department_scope,
    patient_in_scope,
    require_any_permission,
    require_permission,
)
from pydantic import BaseModel, ConfigDict
from services.adt import core as adt_core
from services.adt import reservations as adt_res
from services.audit_service import AuditWriteError, log_audit_event

from routes.adt.end_stay import _meta, _scoped_admission

router = APIRouter()


class ReserveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The master bed to hold.
    bed_id: str
    #: admission, transfer, post_op, elective or other.
    purpose: str
    #: Required for a transfer and for a patient coming back from surgery.
    admission_id: str | None = None
    #: The patient the bed is held for, when there is no admission yet.
    patient_id: str | None = None
    note: str | None = None
    #: A shorter hold than the purpose's default; never a longer one.
    expires_in_minutes: int | None = None


class ReleaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Why the bed is no longer held. Required; it is part of the record.
    reason: str


def _audit_unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": f"The record of this {what} could not be written, so nothing was changed. "
            "Try again in a moment.",
        },
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": "not_found", "message": adt_res.OUT_OF_SCOPE_WORDS}
    )


def _denied(
    user: dict,
    request: Request,
    *,
    patient_id: str | None,
    target_type: str,
    target_id: str,
    dept: str | None,
) -> None:
    """Audit an out-of-scope attempt before the 404, as _scoped_admission does."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="patient_scope_denied",
        outcome="denied",
        summary_message=(f"{target_type} {target_id} is outside the caller's department ({dept})"),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type=target_type,
        target_id=str(target_id),
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"department": dept, "scope_source": "authenticated_user"},
    )


def _scoped_patient(patient_id: str, user: dict, request: Request, *, dept: str | None) -> None:
    """404 when the patient does not exist or is outside `dept`, the caller's
    department as the handler derived it from the authenticated user
    (department_scope), the out-of-scope attempt audited first."""
    with db_session() as db:
        exists = db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone()
        in_scope = exists is not None and (dept is None or patient_in_scope(db, patient_id, dept))
    if exists is None:
        raise _not_found()
    if not in_scope:
        _denied(
            user,
            request,
            patient_id=patient_id,
            target_type="patient",
            target_id=patient_id,
            dept=dept,
        )
        raise _not_found()


_LIST_RESERVATIONS_USER_DEFAULT = Depends(require_permission(ADT_READ))


@router.get("/reservations")
def list_reservations(
    request: Request,
    ward_id: str | None = None,
    user: dict = _LIST_RESERVATIONS_USER_DEFAULT,
):
    """Beds held now. The patient a bed is held for is named only inside the
    caller's department, and a read that names anybody is audited
    (phi_access, fail-closed: 503 when that row cannot be written)."""
    try:
        return adt_res.list_reservations(actor=user, ward_id=ward_id, request_meta=_meta(request))
    except adt_core.AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=adt_core.http_detail(err)) from err
    except AuditWriteError:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "audit_unavailable",
                "message": "This read could not be recorded, so nothing is shown. Try again in a moment.",
            },
        ) from None


_RESERVE_BED_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_RESERVE_BED_USER_DEFAULT = Depends(require_any_permission(ADT_BED, PATIENT_WRITE))


@router.post("/reservations", status_code=201)
def reserve_bed(
    body: ReserveIn,
    request: Request,
    idempotency_key: str | None = _RESERVE_BED_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _RESERVE_BED_USER_DEFAULT,
):
    """Hold a free master bed for a patient on the way. 409 bed_reserved when
    it is held already, bed_occupied when a patient lies in it,
    patient_already_reserved when a bed is held for this patient already;
    422 admission_required for a transfer or post-operative hold with no
    admission named.

    Scope comes from the authenticated user, never from the body. Every
    patient the body names is checked on its own: the admission's patient,
    and a patient_id given beside an admission or alone. Outside the caller's
    department the answer is 404 with the attempt audited. The service checks
    the patient the row will name once more inside its own transaction, and
    that refusal is audited and answered the same way."""
    dept = department_scope(user)
    if body.admission_id:
        _scoped_admission(body.admission_id, user, request)
    if body.patient_id:
        _scoped_patient(body.patient_id, user, request, dept=dept)
    try:
        return adt_res.reserve_bed(
            body.model_dump(),
            actor=user,
            idempotency_key=idempotency_key,
            request_meta=_meta(request),
        )
    except adt_res.ReservationOutOfScope as err:
        # The write rolled back; the attempt is recorded on its own connection.
        _denied(
            user,
            request,
            patient_id=err.patient_id,
            target_type="patient",
            target_id=err.patient_id,
            dept=dept,
        )
        raise _not_found() from err
    except adt_core.AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=adt_core.http_detail(err)) from err
    except AuditWriteError:
        raise _audit_unavailable("reservation") from None


_RELEASE_RESERVATION_USER_DEFAULT = Depends(require_any_permission(ADT_BED, PATIENT_WRITE))


@router.post("/reservations/{reservation_id}/release")
def release_reservation(
    reservation_id: str,
    body: ReleaseIn,
    request: Request,
    user: dict = _RELEASE_RESERVATION_USER_DEFAULT,
):
    """Let a held bed go, with the reason. 409 reservation_not_active once it
    is fulfilled, released or expired."""
    facts = adt_res.reservation_facts(reservation_id)
    if facts is None:
        raise _not_found()
    dept = department_scope(user)
    if facts.get("patient_id"):
        if dept is not None:
            with db_session() as db:
                in_scope = patient_in_scope(db, facts["patient_id"], dept)
            if not in_scope:
                _denied(
                    user,
                    request,
                    patient_id=facts["patient_id"],
                    target_type="bed_reservation",
                    target_id=reservation_id,
                    dept=dept,
                )
                raise _not_found()
    elif not (
        dept is None
        or facts.get("reserved_by") == user.get("id")
        or (facts.get("ward_department") or "").casefold() == dept.casefold()
    ):
        raise _not_found()
    try:
        return adt_res.release_reservation(
            reservation_id, actor=user, reason=body.reason, request_meta=_meta(request)
        )
    except adt_core.AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=adt_core.http_detail(err)) from err
    except AuditWriteError:
        raise _audit_unavailable("release") from None
