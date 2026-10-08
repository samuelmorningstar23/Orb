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
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from permissions import (
    PHARMACY_STORE_NDPS,
    PHARMACY_STORE_READ,
    PHARMACY_STORE_WRITE,
    require_permission,
)
from services import pharmacy_transfer_service as transfers
from services.audit_service import AuditWriteError

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
            correlation_id=str(uuid.uuid4()), event_category="authorization",
            action_type="permission_denied", outcome="denied",
            summary_message=f"Missing permissions: {PHARMACY_STORE_NDPS}",
            actor_user_id=user.get("id"), actor_role=user.get("role"),
            request_path=request.url.path, http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={"required_permissions": [PHARMACY_STORE_READ, PHARMACY_STORE_WRITE,
                                               PHARMACY_STORE_NDPS],
                      "missing_permissions": [PHARMACY_STORE_NDPS],
                      "user_role": user.get("role")})
    except Exception:  # authorization rows are fail-soft; the refusal stands
        pass


def _audit_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={
        "error": "audit_unavailable",
        "message": "The audit record could not be written, so nothing was changed. "
                   "Try again shortly."})


def _ctx(request: Request) -> dict:
    return {"request_path": request.url.path, "http_method": request.method,
            "ip_address": request.client.host if request.client else None}


# ── Reads ─────────────────────────────────────────────────────────────────────

@router.get("/transfers")
def list_transfers(request: Request,
                   status: Optional[str] = Query(None, max_length=40),
                   store_id: Optional[int] = Query(None, ge=1),
                   limit: int = Query(100, ge=1, le=500),
                   user: dict = Depends(_PS_READ)):
    try:
        return {"transfers": transfers.list_transfers(status=status, store_id=store_id,
                                                      limit=limit)}
    except transfers.TransferError as exc:
        raise _fail(exc, request, user)


@router.get("/transfers/options")
def transfer_options(user: dict = Depends(_PS_READ)):
    """The stores a transfer may run between, who may witness a controlled
    one (never the caller), and the site's settings for the screen."""
    return transfers.options(user)


@router.get("/transfers/stock")
def transfer_store_stock(request: Request,
                         store_code: str = Query(..., min_length=1, max_length=40),
                         user: dict = Depends(_PS_READ)):
    """Each drug a store holds usable units of now, for the transfer form."""
    try:
        return transfers.store_stock(store_code)
    except transfers.TransferError as exc:
        raise _fail(exc, request, user)


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int, user: dict = Depends(_PS_READ)):
    doc = transfers.get_transfer(transfer_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"No transfer {transfer_id}"})
    return {"transfer": doc}


# ── Writes ────────────────────────────────────────────────────────────────────

class TransferLineIn(BaseModel):
    helix_medication_id: str = Field(..., min_length=1, max_length=80)
    quantity_units: Any = None


class TransferIn(BaseModel):
    from_store_code: str = Field(..., min_length=1, max_length=40)
    to_store_code: str = Field(..., min_length=1, max_length=40)
    lines: List[TransferLineIn] = Field(..., min_length=1, max_length=transfers.MAX_DRUGS)
    remarks: Optional[str] = Field(None, max_length=transfers.MAX_TEXT)
    idempotency_key: Optional[str] = Field(None, max_length=120)


@router.post("/transfers")
def create_transfer(body: TransferIn, request: Request, user: dict = Depends(_PS_WRITE)):
    """Raise a draft. The plan takes the sending store's usable lots, earliest
    expiry first. Nothing moves until dispatch."""
    try:
        return transfers.create_transfer(
            from_store_code=body.from_store_code, to_store_code=body.to_store_code,
            lines=[line.model_dump() for line in body.lines], actor=user,
            remarks=body.remarks, idempotency_key=body.idempotency_key,
            audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc, request, user)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/transfers/{transfer_id}/replan")
def replan_transfer(transfer_id: int, request: Request, user: dict = Depends(_PS_WRITE)):
    try:
        return transfers.replan_transfer(transfer_id, actor=user, audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc, request, user)
    except AuditWriteError:
        raise _audit_unavailable()


class WitnessIn(BaseModel):
    witness_id: Optional[str] = Field(None, max_length=80)
    witness_pin: Optional[str] = Field(None, max_length=64)


@router.post("/transfers/{transfer_id}/dispatch")
def dispatch_transfer(transfer_id: int, body: WitnessIn, request: Request,
                      user: dict = Depends(_PS_WRITE)):
    """Send it. A dispatch already made posts nothing again."""
    try:
        return transfers.dispatch_transfer(
            transfer_id, actor=user, witness_id=body.witness_id,
            witness_pin=body.witness_pin, audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc, request, user)
    except AuditWriteError:
        raise _audit_unavailable()


class ReceiptLineIn(BaseModel):
    line_id: Any = None
    received_units: Any = None
    discrepancy_reason: Optional[str] = Field(None, max_length=transfers.MAX_TEXT)


class ReceiptIn(WitnessIn):
    lines: List[ReceiptLineIn] = Field(..., min_length=1, max_length=transfers.MAX_LINES)


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id: int, body: ReceiptIn, request: Request,
                     user: dict = Depends(_PS_WRITE)):
    """Receive some or all lines. Never by the person who dispatched it."""
    try:
        return transfers.receive_transfer(
            transfer_id, lines=[line.model_dump() for line in body.lines], actor=user,
            witness_id=body.witness_id, witness_pin=body.witness_pin, audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc, request, user)
    except AuditWriteError:
        raise _audit_unavailable()


class CancelIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=transfers.MAX_TEXT)


@router.post("/transfers/{transfer_id}/cancel")
def cancel_transfer(transfer_id: int, body: CancelIn, request: Request,
                    user: dict = Depends(_PS_WRITE)):
    try:
        return transfers.cancel_transfer(transfer_id, reason=body.reason, actor=user,
                                         audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc, request, user)
    except AuditWriteError:
        raise _audit_unavailable()
