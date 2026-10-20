"""Pharmacy-internal transfers: endpoints under /api/pharmacy-store (grn-11, W5-G).

The package router in routes/pharmacy_store/__init__.py adds the
/api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this router has
neither, so every endpoint added here inherits both (404 module_disabled while
the module is off, after a 401 for a caller with no session).

Endpoints (docs/his_design/grn.json, section E):
  read   pharmacy_store.read
    GET  /transfers                     newest first; ?status= one status or 'open'
    GET  /transfers/options             stores, witnesses, site settings
    GET  /transfers/stock?store_code=   what a store could send now
    GET  /transfers/{transfer_id}       one transfer with its lines
  write  pharmacy_store.read + pharmacy_store.write
    POST /transfers                     a draft with its FEFO issue plan
    POST /transfers/{transfer_id}/replan    plan a draft again
    POST /transfers/{transfer_id}/dispatch  send it: the units go in transit
    POST /transfers/{transfer_id}/receive   receive some or all lines
    POST /transfers/{transfer_id}/cancel    give up a draft, with a reason

A CONTROLLED transfer (the NDPS vault at either end, or a narcotic or
psychotropic line) also needs pharmacy_store.ndps for every write, and its
dispatch and receipt a witness confirmed by their own signing PIN. Those rules,
the FEFO plan, in transit counted nowhere, and receipt by somebody other than
the dispatcher all live in services/pharmacy_transfer_service.py, which runs
each write in one immediate transaction with its 'security' audit row.

Errors: an unknown transfer is 404; the dispatcher receiving, a missing NDPS
permission or a witness refusal is 403; a locked witness PIN is 429; a move
the status does not allow, a stale plan or short stock is 409; an audit
outage is 503 audit_unavailable and nothing was changed; anything the request
must correct is 422. No patient data is read or returned.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import (
    PHARMACY_STORE_NDPS,
    PHARMACY_STORE_READ,
    PHARMACY_STORE_WRITE,
    require_permission,
)
from pydantic import BaseModel, Field
from services.audit_service import AuditWriteError

from services import pharmacy_transfer_service as transfers

router = APIRouter()

_PS_READ = require_permission(PHARMACY_STORE_READ)
_PS_WRITE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_WRITE)

_STATUS = {
    "not_found": 404,
    "same_person": 403,
    "ndps_permission_needed": 403,
    "witness_same_person": 403,
    "witness_not_eligible": 403,
    "witness_no_pin": 403,
    "witness_pin_wrong": 403,
    "witness_locked": 429,
    "illegal_transition": 409,
    "plan_stale": 409,
    "insufficient_stock": 409,
    "line_already_received": 409,
    "lot_status_conflict": 409,
    "idempotency_conflict": 409,
    "lot_changed": 409,
    "store_inactive": 409,
}


def _fail(exc: transfers.TransferError, request: Request, user: dict) -> HTTPException:
    if exc.code == "ndps_permission_needed":
        _audit_denied(request, user)
    detail = {"error": exc.code, "message": str(exc)}
    if getattr(exc, "detail", None):
        detail.update({k: v for k, v in exc.detail.items() if k not in detail})
    return HTTPException(status_code=_STATUS.get(exc.code, 422), detail=detail)


def _audit_denied(request: Request, user: dict) -> None:
    """A controlled transfer refused for want of pharmacy_store.ndps is a
    permission denial, audited as require_permission audits one."""
    from services.audit_service import log_audit_event

    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="permission_denied",
            outcome="denied",
            summary_message=f"Missing permissions: {PHARMACY_STORE_NDPS}",
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={
                "required_permissions": [
                    PHARMACY_STORE_READ,
                    PHARMACY_STORE_WRITE,
                    PHARMACY_STORE_NDPS,
                ],
                "missing_permissions": [PHARMACY_STORE_NDPS],
                "user_role": user.get("role"),
            },
        )
    except Exception:  # authorization rows are fail-soft; the refusal stands
        pass


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": "The audit record could not be written, so nothing was changed. "
            "Try again shortly.",
        },
    )


def _ctx(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
    }


# ── Reads ─────────────────────────────────────────────────────────────────────


_LIST_TRANSFERS_STATUS_DEFAULT = Query(None, max_length=40)
_LIST_TRANSFERS_STORE_ID_DEFAULT = Query(None, ge=1)
_LIST_TRANSFERS_LIMIT_DEFAULT = Query(100, ge=1, le=500)
_LIST_TRANSFERS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/transfers")
def list_transfers(
    request: Request,
    status: str | None = _LIST_TRANSFERS_STATUS_DEFAULT,
    store_id: int | None = _LIST_TRANSFERS_STORE_ID_DEFAULT,
    limit: int = _LIST_TRANSFERS_LIMIT_DEFAULT,
    user: dict = _LIST_TRANSFERS_USER_DEFAULT,
):
    try:
        return {
            "transfers": transfers.list_transfers(status=status, store_id=store_id, limit=limit)
        }
    except transfers.TransferError as exc:
        raise _fail(exc, request, user) from exc


_TRANSFER_OPTIONS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/transfers/options")
def transfer_options(user: dict = _TRANSFER_OPTIONS_USER_DEFAULT):
    """The stores a transfer may run between, who may witness a controlled
    one (never the caller), and the site's settings for the screen."""
    return transfers.options(user)


_TRANSFER_STORE_STOCK_STORE_CODE_DEFAULT = Query(..., min_length=1, max_length=40)
_TRANSFER_STORE_STOCK_USER_DEFAULT = Depends(_PS_READ)


@router.get("/transfers/stock")
def transfer_store_stock(
    request: Request,
    store_code: str = _TRANSFER_STORE_STOCK_STORE_CODE_DEFAULT,
    user: dict = _TRANSFER_STORE_STOCK_USER_DEFAULT,
):
    """Each drug a store holds usable units of now, for the transfer form."""
    try:
        return transfers.store_stock(store_code)
    except transfers.TransferError as exc:
        raise _fail(exc, request, user) from exc


_GET_TRANSFER_USER_DEFAULT = Depends(_PS_READ)


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int, user: dict = _GET_TRANSFER_USER_DEFAULT):
    doc = transfers.get_transfer(transfer_id)
    if doc is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": f"No transfer {transfer_id}"}
        )
    return {"transfer": doc}


# ── Writes ────────────────────────────────────────────────────────────────────


class TransferLineIn(BaseModel):
    helix_medication_id: str = Field(..., min_length=1, max_length=80)
    quantity_units: Any = None


class TransferIn(BaseModel):
    from_store_code: str = Field(..., min_length=1, max_length=40)
    to_store_code: str = Field(..., min_length=1, max_length=40)
    lines: list[TransferLineIn] = Field(..., min_length=1, max_length=transfers.MAX_DRUGS)
    remarks: str | None = Field(None, max_length=transfers.MAX_TEXT)
    idempotency_key: str | None = Field(None, max_length=120)


_CREATE_TRANSFER_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/transfers")
def create_transfer(body: TransferIn, request: Request, user: dict = _CREATE_TRANSFER_USER_DEFAULT):
    """Raise a draft. The plan takes the sending store's usable lots, earliest
    expiry first. Nothing moves until dispatch."""
    try:
        return transfers.create_transfer(
            from_store_code=body.from_store_code,
            to_store_code=body.to_store_code,
            lines=[line.model_dump() for line in body.lines],
            actor=user,
            remarks=body.remarks,
            idempotency_key=body.idempotency_key,
            audit_ctx=_ctx(request),
        )
    except transfers.TransferError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_REPLAN_TRANSFER_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/transfers/{transfer_id}/replan")
def replan_transfer(transfer_id: int, request: Request, user: dict = _REPLAN_TRANSFER_USER_DEFAULT):
    try:
        return transfers.replan_transfer(transfer_id, actor=user, audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class WitnessIn(BaseModel):
    witness_id: str | None = Field(None, max_length=80)
    witness_pin: str | None = Field(None, max_length=64)


_DISPATCH_TRANSFER_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/transfers/{transfer_id}/dispatch")
def dispatch_transfer(
    transfer_id: int,
    body: WitnessIn,
    request: Request,
    user: dict = _DISPATCH_TRANSFER_USER_DEFAULT,
):
    """Send it. A dispatch already made posts nothing again."""
    try:
        return transfers.dispatch_transfer(
            transfer_id,
            actor=user,
            witness_id=body.witness_id,
            witness_pin=body.witness_pin,
            audit_ctx=_ctx(request),
        )
    except transfers.TransferError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class ReceiptLineIn(BaseModel):
    line_id: Any = None
    received_units: Any = None
    discrepancy_reason: str | None = Field(None, max_length=transfers.MAX_TEXT)


class ReceiptIn(WitnessIn):
    lines: list[ReceiptLineIn] = Field(..., min_length=1, max_length=transfers.MAX_LINES)


_RECEIVE_TRANSFER_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(
    transfer_id: int, body: ReceiptIn, request: Request, user: dict = _RECEIVE_TRANSFER_USER_DEFAULT
):
    """Receive some or all lines. Never by the person who dispatched it."""
    try:
        return transfers.receive_transfer(
            transfer_id,
            lines=[line.model_dump() for line in body.lines],
            actor=user,
            witness_id=body.witness_id,
            witness_pin=body.witness_pin,
            audit_ctx=_ctx(request),
        )
    except transfers.TransferError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=transfers.MAX_TEXT)


_CANCEL_TRANSFER_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/transfers/{transfer_id}/cancel")
def cancel_transfer(
    transfer_id: int, body: CancelIn, request: Request, user: dict = _CANCEL_TRANSFER_USER_DEFAULT
):
    try:
        return transfers.cancel_transfer(
            transfer_id, reason=body.reason, actor=user, audit_ctx=_ctx(request)
        )
    except transfers.TransferError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None
