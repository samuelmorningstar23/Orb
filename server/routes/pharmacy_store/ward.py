"""Ward stock, ward transfers and ward indents: endpoints under /api/pharmacy-store (grn-21, W8-F).

The package router in routes/pharmacy_store/__init__.py adds the
/api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this router has
neither, so every endpoint added here inherits both.

Also behind ORB_PHARMACY_WARD_STOCK (ruling GRN-RS5, held, so the strict
default is "off"): this router carries feature_gate('pharmacy_ward_stock') as a
router-level dependency, so every endpoint added here runs the pharmacy-store
module gate first, then this feature gate, then its own permission. Do not add
the feature gate again per route, and do not move a ward endpoint to another
sub-router to dodge it. tests/test_his_route_skeletons.py pins both the gate
and its order. Another ward's id is 404.

Endpoints (docs/his_design/grn.json, section E; services/pharmacy_transfer_service):
  read   ward_stock.read (the ward, its own department) or pharmacy_store.read
         (the pharmacy desk, every ward store)
    GET  /ward/stock                       each ward store against its stock list
    GET  /ward/transfers/awaiting          transfers on their way to a ward store
    GET  /ward/indents                     issue indents; ?status= one status or 'open'
    GET  /ward/indents/{indent_id}         one indent with its lines and transfers
  raise  ward_indent.raise (own department) or pharmacy_store.write (on the
         ward's behalf)
    POST /ward/indents                     ask the dispensing pharmacy for stock
    POST /ward/indents/{indent_id}/cancel  withdraw it, with a reason
  decide pharmacy_store.write or pharmacy_store.approve, never the raiser
    POST /ward/indents/{indent_id}/decision
  issue  pharmacy_store.read (plan) and pharmacy_store.write (issue)
    GET  /ward/indents/{indent_id}/issue-plan
    POST /ward/indents/{indent_id}/issue   a transfer draft from the dispensing
                                           pharmacy; sent with POST
                                           /transfers/{id}/dispatch
  receive ward_stock.receive, the store's own department, never the dispatcher
    POST /ward/transfers/{transfer_id}/receive
         a transfer that became controlled on its way (a drug classified
         narcotic or psychotropic after it was sent) also needs witness_id and
         witness_pin: a pharmacy_store.ndps holder who did not send it

helix.receive (the ward's receipt of a patient's dispensed order) reaches none
of these. No patient data is read or returned.

Errors: an unknown, another ward's or another desk's id is 404; the dispatcher
receiving, the raiser deciding, a missing grant or a refused witness is 403 (a
locked witness PIN is 429); a move the status
does not allow, short stock, an open transfer or a drug already asked for is
409; an audit outage is 503 audit_unavailable and nothing was changed;
anything the request must correct is 422 (including a drug not on the stock
list, a barred high-alert drug or concentrated electrolyte, and NDPS stock).
"""
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from permissions import (
    PHARMACY_STORE_APPROVE,
    PHARMACY_STORE_READ,
    PHARMACY_STORE_WRITE,
    WARD_INDENT_RAISE,
    WARD_STOCK_READ,
    WARD_STOCK_RECEIVE,
    require_any_permission,
    require_permission,
)
from routes.opt_in_gates import feature_gate
from services import pharmacy_transfer_service as transfers
from services.audit_service import AuditWriteError

require_pharmacy_ward_stock_feature = feature_gate("pharmacy_ward_stock")

router = APIRouter(dependencies=[Depends(require_pharmacy_ward_stock_feature)])

_WARD_READ = require_any_permission(WARD_STOCK_READ, PHARMACY_STORE_READ)
_WARD_RAISE = require_any_permission(WARD_INDENT_RAISE, PHARMACY_STORE_WRITE)
_DECIDE = require_any_permission(PHARMACY_STORE_WRITE, PHARMACY_STORE_APPROVE)
_PS_READ = require_permission(PHARMACY_STORE_READ)
_PS_WRITE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_WRITE)
_RECEIVE = require_permission(WARD_STOCK_RECEIVE)

_STATUS = {
    "not_found": 404,
    "ward_stock_off": 404,
    "same_person": 403,
    "permission_denied": 403,
    "ndps_permission_needed": 403,
    "witness_same_person": 403,
    "witness_sent_it": 403,
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
    "store_inactive": 409,
    "transfer_open": 409,
    "already_requested": 409,
    "no_dispensing_store": 409,
}


def _fail(exc: transfers.TransferError) -> HTTPException:
    detail = {"error": exc.code, "message": str(exc)}
    if getattr(exc, "detail", None):
        detail.update({k: v for k, v in exc.detail.items() if k not in detail})
    return HTTPException(status_code=_STATUS.get(exc.code, 422), detail=detail)


def _audit_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={
        "error": "audit_unavailable",
        "message": "The audit record could not be written, so nothing was changed. "
                   "Try again shortly."})


def _ctx(request: Request) -> dict:
    return {"request_path": request.url.path, "http_method": request.method,
            "ip_address": request.client.host if request.client else None}


# ── Reads ─────────────────────────────────────────────────────────────────────

@router.get("/ward/stock")
def ward_stock(store_code: Optional[str] = Query(None, max_length=40),
               user: dict = Depends(_WARD_READ)):
    """Each ward store the caller sees, every drug on its list and every drug
    it holds: on hand, held, expired, on its way, asked for, par and top-up."""
    try:
        return transfers.ward_stock(actor=user, store_code=store_code)
    except transfers.TransferError as exc:
        raise _fail(exc)


@router.get("/ward/transfers/awaiting")
def ward_transfers_awaiting(user: dict = Depends(_WARD_READ)):
    """Transfers on their way to a ward store the caller sees, with their lines."""
    return {"transfers": transfers.ward_transfers_awaiting(actor=user)}


@router.get("/ward/indents")
def list_ward_indents(status: Optional[str] = Query(None, max_length=40),
                      store_code: Optional[str] = Query(None, max_length=40),
                      limit: int = Query(100, ge=1, le=500),
                      user: dict = Depends(_WARD_READ)):
    try:
        return {"indents": transfers.list_ward_indents(actor=user, status=status,
                                                       store_code=store_code, limit=limit)}
    except transfers.TransferError as exc:
        raise _fail(exc)


@router.get("/ward/indents/{indent_id}")
def get_ward_indent(indent_id: int, user: dict = Depends(_WARD_READ)):
    try:
        return {"indent": transfers.get_ward_indent(indent_id, actor=user)}
    except transfers.TransferError as exc:
        raise _fail(exc)


@router.get("/ward/indents/{indent_id}/issue-plan")
def ward_issue_plan(indent_id: int, user: dict = Depends(_PS_READ)):
    """What issuing this indent would take from the dispensing pharmacy, FEFO."""
    try:
        return transfers.ward_issue_plan(indent_id, actor=user)
    except transfers.TransferError as exc:
        raise _fail(exc)


# ── Writes ────────────────────────────────────────────────────────────────────

class IndentLineIn(BaseModel):
    helix_medication_id: str = Field(..., min_length=1, max_length=80)
    quantity_units: Any = None


class IndentIn(BaseModel):
    store_code: str = Field(..., min_length=1, max_length=40)
    lines: List[IndentLineIn] = Field(..., min_length=1, max_length=transfers.MAX_DRUGS)
    priority: str = Field("routine", max_length=20)
    remarks: Optional[str] = Field(None, max_length=transfers.MAX_TEXT)


@router.post("/ward/indents")
def raise_ward_indent(body: IndentIn, request: Request, user: dict = Depends(_WARD_RAISE)):
    """Ask the dispensing pharmacy for drugs on a ward store's list."""
    try:
        return transfers.raise_ward_indent(
            store_code=body.store_code, lines=[line.model_dump() for line in body.lines],
            actor=user, priority=body.priority, remarks=body.remarks, audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


class ApprovalIn(BaseModel):
    line_no: Any = None
    units: Any = None


class DecisionIn(BaseModel):
    approvals: Optional[List[ApprovalIn]] = Field(None, max_length=transfers.MAX_DRUGS)
    reject: bool = False
    reason: Optional[str] = Field(None, max_length=transfers.MAX_TEXT)


@router.post("/ward/indents/{indent_id}/decision")
def decide_ward_indent(indent_id: int, body: DecisionIn, request: Request,
                       user: dict = Depends(_DECIDE)):
    """Approve (in whole or in part) or refuse a ward indent."""
    try:
        return transfers.decide_ward_indent(
            indent_id, actor=user,
            approvals=[a.model_dump() for a in body.approvals] if body.approvals else None,
            reject=body.reject, reason=body.reason, audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


class CancelIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=transfers.MAX_TEXT)


@router.post("/ward/indents/{indent_id}/cancel")
def cancel_ward_indent(indent_id: int, body: CancelIn, request: Request,
                       user: dict = Depends(_WARD_RAISE)):
    try:
        return transfers.cancel_ward_indent(indent_id, actor=user, reason=body.reason,
                                            audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


class IssueLineIn(BaseModel):
    line_no: Any = None
    units: Any = None


class IssueIn(BaseModel):
    lines: Optional[List[IssueLineIn]] = Field(None, max_length=transfers.MAX_DRUGS)
    remarks: Optional[str] = Field(None, max_length=transfers.MAX_TEXT)
    idempotency_key: Optional[str] = Field(None, max_length=120)


@router.post("/ward/indents/{indent_id}/issue")
def issue_ward_indent(indent_id: int, body: IssueIn, request: Request,
                      user: dict = Depends(_PS_WRITE)):
    """A transfer draft from the dispensing pharmacy with its FEFO plan.
    Nothing moves until it is sent with POST /transfers/{id}/dispatch."""
    try:
        return transfers.issue_ward_indent(
            indent_id, actor=user,
            lines=[line.model_dump() for line in body.lines] if body.lines else None,
            remarks=body.remarks, idempotency_key=body.idempotency_key,
            audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


class WardReceiptLineIn(BaseModel):
    line_id: Any = None
    received_units: Any = None
    discrepancy_reason: Optional[str] = Field(None, max_length=transfers.MAX_TEXT)


class WardReceiptIn(BaseModel):
    lines: List[WardReceiptLineIn] = Field(..., min_length=1, max_length=transfers.MAX_LINES)
    witness_id: Optional[str] = Field(None, max_length=80)
    witness_pin: Optional[str] = Field(None, max_length=64)


@router.post("/ward/transfers/{transfer_id}/receive")
def receive_ward_transfer(transfer_id: int, body: WardReceiptIn, request: Request,
                          user: dict = Depends(_RECEIVE)):
    """Receive a transfer on the ward. Never by the person who sent it. A
    transfer that became controlled on its way is received with a witness."""
    try:
        return transfers.receive_ward_transfer(
            transfer_id, lines=[line.model_dump() for line in body.lines], actor=user,
            witness_id=body.witness_id, witness_pin=body.witness_pin, audit_ctx=_ctx(request))
    except transfers.TransferError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()
