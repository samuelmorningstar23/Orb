"""Ward returns: endpoints under /api/pharmacy-store (grn-15, W9-D).

The package router in routes/pharmacy_store/__init__.py adds the
/api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this router has
neither, so every endpoint added here inherits both (404 module_disabled while
the module is off, after a 401 for a caller with no session).

Endpoints (docs/his_design/grn.json, section E; the rules live in
services/pharmacy_store_service.py, ward returns section):
  record   ward_return.record (the nurse, her own department, ruling GRN-R8 (a))
           or pharmacy_store.write (the pharmacy counter, every ward)
    GET  /ward-returns/options             returns quarantine stores, whether
                                           restock is on, witnesses
    GET  /ward-returns/returnable          delivered orders that still hold
                                           units a return could name
    GET  /ward-returns                     returns, newest first; ?status=
    GET  /ward-returns/{return_id}         one return
    POST /ward-returns                     take units back into returns
                                           quarantine (Idempotency-Key header
                                           or body field)
  inspect  pharmacy_store.read + pharmacy_store.write
    POST /ward-returns/{return_id}/inspect write it off, or restock it into the
                                           batch it came from (ruling GRN-RS3,
                                           held: restock is refused on every
                                           site until Samuel rules)

pharmacy_store.read alone (the administrator) reaches none of these: every
answer names a patient, and reading a patient's medicines is not part of that
grant.

Every list read writes a phi_access row with the patient ids returned
(fail-closed: the read is refused when the row cannot be written). Every write
runs in one immediate transaction with its clinical_access audit row.

Errors: an unknown return or order, or another ward's, is 404 (another ward's
order writes an authorization row first); a missing NDPS permission, a refused
witness, or the person who took a return in writing it off or restocking it is
403; a locked witness PIN is 429; an order not yet received, more units than can come back,
no returns store, a return already inspected, restock switched off on this
site or a reused request key is 409; an audit outage is 503 audit_unavailable
and nothing was changed; anything the request must correct is 422.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from permissions import (
    PHARMACY_STORE_NDPS,
    PHARMACY_STORE_READ,
    PHARMACY_STORE_WRITE,
    WARD_RETURN_RECORD,
    log_phi_read,
    require_any_permission,
    require_permission,
)
from pydantic import BaseModel, ConfigDict, Field
from services.audit_service import AuditWriteError

from services import pharmacy_store_service as returns

router = APIRouter()

_RECORD = require_any_permission(WARD_RETURN_RECORD, PHARMACY_STORE_WRITE)
_INSPECT = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_WRITE)


def _ctx(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
    }


def _audit_denied(request: Request, user: dict, missing: list[str]) -> None:
    """A refusal for want of a permission, recorded as require_permission
    records one. Authorization rows are fail-soft; the refusal stands."""
    from services.audit_service import log_audit_event

    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="permission_denied",
            outcome="denied",
            summary_message=f"Missing permissions: {' or '.join(missing)}",
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={"missing_permissions": missing, "user_role": user.get("role")},
        )
    except Exception:  # noqa: BLE001
        pass


def _fail(exc: returns.WardReturnError, request: Request, user: dict) -> HTTPException:
    if exc.code == "ndps_permission_needed":
        _audit_denied(request, user, [PHARMACY_STORE_NDPS])
    detail = {"error": exc.code, "message": str(exc)}
    if getattr(exc, "detail", None):
        detail.update({k: v for k, v in exc.detail.items() if k not in detail})
    return HTTPException(
        status_code=returns.WARD_RETURN_STATUS_CODES.get(exc.code, 422), detail=detail
    )


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": "The audit record could not be written, so nothing was changed. "
            "Try again shortly.",
        },
    )


def _read_audit(
    request: Request,
    user: dict,
    action: str,
    summary: str,
    rows: list,
    *,
    target_id: str | None = None,
) -> None:
    """The phi_access row for a read that names patients. Written only when
    the answer names somebody; fail-closed (503, nothing returned)."""
    patient_ids = sorted({str(r.get("patient_id")) for r in rows if r.get("patient_id")})
    if not patient_ids:
        return
    log_phi_read(
        request=request,
        user=user,
        action_type=action,
        summary_message=summary,
        patient_id=patient_ids[0] if len(patient_ids) == 1 else None,
        target_type=returns.WARD_RETURN_REFERENCE,
        target_id=str(target_id) if target_id is not None else None,
        metadata={"patient_ids": patient_ids[:200], "count": len(rows)},
    )


# ── Reads ─────────────────────────────────────────────────────────────────────


_WARD_RETURN_OPTIONS_USER_DEFAULT = Depends(_RECORD)


@router.get("/ward-returns/options")
def ward_return_options(user: dict = _WARD_RETURN_OPTIONS_USER_DEFAULT):
    """Returns quarantine stores, whether restock is on, witnesses. No patient data."""
    return returns.options(actor=user)


_RETURNABLE_ORDERS_HELIX_ORDER_ID_DEFAULT = Query(None, max_length=80)
_RETURNABLE_ORDERS_LIMIT_DEFAULT = Query(100, ge=1, le=returns.MAX_LIST)
_RETURNABLE_ORDERS_USER_DEFAULT = Depends(_RECORD)


@router.get("/ward-returns/returnable")
def returnable_orders(
    request: Request,
    helix_order_id: str | None = _RETURNABLE_ORDERS_HELIX_ORDER_ID_DEFAULT,
    limit: int = _RETURNABLE_ORDERS_LIMIT_DEFAULT,
    user: dict = _RETURNABLE_ORDERS_USER_DEFAULT,
):
    """Delivered orders in the caller's scope with units that can still come back."""
    try:
        rows = returns.returnable_orders(actor=user, helix_order_id=helix_order_id, limit=limit)
        _read_audit(
            request,
            user,
            "pharmacy_returnable_orders_viewed",
            f"Orders that can be returned viewed ({len(rows)})",
            rows,
        )
    except returns.WardReturnError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None
    return {"orders": rows, "window_days": returns.RETURNABLE_WINDOW_DAYS}


_LIST_WARD_RETURNS_STATUS_DEFAULT = Query(None, max_length=40)
_LIST_WARD_RETURNS_LIMIT_DEFAULT = Query(100, ge=1, le=returns.MAX_LIST)
_LIST_WARD_RETURNS_USER_DEFAULT = Depends(_RECORD)


@router.get("/ward-returns")
def list_ward_returns(
    request: Request,
    status: str | None = _LIST_WARD_RETURNS_STATUS_DEFAULT,
    limit: int = _LIST_WARD_RETURNS_LIMIT_DEFAULT,
    user: dict = _LIST_WARD_RETURNS_USER_DEFAULT,
):
    try:
        rows = returns.list_ward_returns(actor=user, status=status or None, limit=limit)
        _read_audit(
            request,
            user,
            "pharmacy_ward_returns_viewed",
            f"Ward returns viewed ({len(rows)})",
            rows,
        )
    except returns.WardReturnError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None
    return {"ward_returns": rows}


_GET_WARD_RETURN_USER_DEFAULT = Depends(_RECORD)


@router.get("/ward-returns/{return_id}")
def get_ward_return(return_id: int, request: Request, user: dict = _GET_WARD_RETURN_USER_DEFAULT):
    try:
        row = returns.get_ward_return(return_id, actor=user, audit_ctx=_ctx(request))
        _read_audit(
            request,
            user,
            "pharmacy_ward_return_viewed",
            f"Ward return {return_id} viewed",
            [row],
            target_id=str(return_id),
        )
    except returns.WardReturnError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None
    return {"ward_return": row}


# ── Writes ────────────────────────────────────────────────────────────────────


class WardReturnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    helix_order_id: str = Field(..., min_length=1, max_length=80)
    quantity_units: Any = None
    #: True, false or left unanswered (null): what the person taking it in saw.
    seal_intact: bool | None = None
    #: The batch, when the order was dispensed from more than one.
    lot_id: str | None = Field(None, max_length=80)
    #: The returns quarantine store, when the site has more than one.
    store_code: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=returns.MAX_TEXT)
    idempotency_key: str | None = Field(None, max_length=120)
    #: A narcotic or psychotropic return: the second pharmacist and their PIN.
    witness_id: str | None = Field(None, max_length=80)
    witness_pin: str | None = Field(None, max_length=40)


_RECORD_WARD_RETURN_USER_DEFAULT = Depends(_RECORD)
_RECORD_WARD_RETURN_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")


@router.post("/ward-returns")
def record_ward_return(
    body: WardReturnIn,
    request: Request,
    user: dict = _RECORD_WARD_RETURN_USER_DEFAULT,
    idempotency_key: str | None = _RECORD_WARD_RETURN_IDEMPOTENCY_KEY_DEFAULT,
):
    """Take units of a delivered order back into returns quarantine. They are
    never dispensable there; a pharmacist inspects them."""
    try:
        return returns.record_ward_return(
            body.helix_order_id,
            actor=user,
            quantity_units=body.quantity_units,
            seal_intact=body.seal_intact,
            lot_id=body.lot_id,
            store_code=body.store_code,
            note=body.note,
            idempotency_key=idempotency_key or body.idempotency_key,
            witness_id=body.witness_id,
            witness_pin=body.witness_pin,
            audit_ctx=_ctx(request),
        )
    except returns.WardReturnError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class InspectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(..., max_length=20)
    reason: str | None = Field(None, max_length=returns.MAX_TEXT)
    #: The inspecting pharmacist's own finding; restock needs true.
    seal_intact: bool | None = None
    witness_id: str | None = Field(None, max_length=80)
    witness_pin: str | None = Field(None, max_length=40)


_INSPECT_WARD_RETURN_USER_DEFAULT = Depends(_INSPECT)


@router.post("/ward-returns/{return_id}/inspect")
def inspect_ward_return(
    return_id: int,
    body: InspectIn,
    request: Request,
    user: dict = _INSPECT_WARD_RETURN_USER_DEFAULT,
):
    """Write a return off, or restock it into its original batch (refused on
    every site until ruling GRN-RS3 is made)."""
    try:
        return returns.inspect_ward_return(
            return_id,
            actor=user,
            decision=body.decision,
            reason=body.reason,
            seal_intact=body.seal_intact,
            witness_id=body.witness_id,
            witness_pin=body.witness_pin,
            audit_ctx=_ctx(request),
        )
    except returns.WardReturnError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None
