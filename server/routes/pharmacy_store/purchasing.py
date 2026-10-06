"""Pharmacy purchasing and goods receipt: endpoints under /api/pharmacy-store.

Purchase orders (grn-7, W4-C) are here; goods receipts arrive with grn-8
(W5-C). The package router in routes/pharmacy_store/__init__.py adds the
/api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this router has
neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  purchase-orders, plus submit, approve, issue, cancel, reorder-suggestions
  goods-receipts, plus post and cancel     expiry typed twice (ruling GRN-RS6, held)

Tiers, built once at import as procurement_routes does:
  read     pharmacy_store.read                 lists, one order, form options,
                                               reorder suggestions
  write    pharmacy_store.read + .write        raise a draft, submit it
  approve  pharmacy_store.read + .approve      approve, issue to the vendor
  cancel   pharmacy_store.read + (.write or .approve); the service then needs
           the approve tier once the order is approved or issued

The rules (licensed vendor, classified drug with a pack size and GST rate,
NDPS drugs into the vault, approver differs from creator) live in
services/pharmacy_purchasing_service.py, which runs every action in one
immediate transaction with its 'security' audit row. Document numbers are
stamped after commit, as procurement does. Works with ORB_PROCUREMENT off.

Errors: an unknown order id is 404; the creator approving, or a cancel past
approval without the approve tier, is 403; a move the order's status does not
allow is 409; anything the request must correct is 422; an audit outage is
503 audit_unavailable, and nothing was changed.
"""
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from permissions import (
    PHARMACY_STORE_APPROVE,
    PHARMACY_STORE_READ,
    PHARMACY_STORE_WRITE,
    has_permission,
    require_permission,
)
from services import pharmacy_purchasing_service as purchasing
from services.audit_service import AuditWriteError

router = APIRouter()

_PS_READ = require_permission(PHARMACY_STORE_READ)
_PS_WRITE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_WRITE)
_PS_APPROVE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_APPROVE)

_STATUS = {
    "not_found": 404,
    "same_person": 403,
    "approve_needed": 403,
    "illegal_transition": 409,
    "already_received": 409,
}


def _fail(exc: purchasing.PurchasingError) -> HTTPException:
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


def _cancel_tier(request: Request, user: dict = Depends(_PS_READ)) -> dict:
    """Read plus write or approve. Which of the two the order needs is the
    service's call, by its status. A denial is audited as require_permission
    audits one."""
    role = user.get("role", "")
    if has_permission(role, PHARMACY_STORE_WRITE) or has_permission(role, PHARMACY_STORE_APPROVE):
        return user
    import uuid

    from services.audit_service import log_audit_event

    log_audit_event(
        correlation_id=str(uuid.uuid4()), event_category="authorization",
        action_type="permission_denied", outcome="denied",
        summary_message=f"Missing permissions: {PHARMACY_STORE_WRITE} or {PHARMACY_STORE_APPROVE}",
        actor_user_id=user.get("id"), actor_role=role, request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"required_permissions": [PHARMACY_STORE_READ, PHARMACY_STORE_WRITE,
                                           PHARMACY_STORE_APPROVE],
                  "missing_permissions": [PHARMACY_STORE_WRITE, PHARMACY_STORE_APPROVE],
                  "user_role": role})
    raise HTTPException(status_code=403, detail={
        "error": "permission_denied", "message": "Insufficient permissions",
        "required": [PHARMACY_STORE_READ, f"{PHARMACY_STORE_WRITE} or {PHARMACY_STORE_APPROVE}"],
        "missing": [PHARMACY_STORE_WRITE, PHARMACY_STORE_APPROVE]})


# ── Reads ─────────────────────────────────────────────────────────────────────

@router.get("/purchase-orders")
def list_purchase_orders(status: Optional[str] = Query(None, max_length=40),
                         limit: int = Query(100, ge=1, le=500),
                         user: dict = Depends(_PS_READ)):
    try:
        return {"purchase_orders": purchasing.list_purchase_orders(status=status, limit=limit)}
    except purchasing.PurchasingError as exc:
        raise _fail(exc)


@router.get("/purchase-orders/options")
def purchase_order_options(user: dict = Depends(_PS_READ)):
    """Vendors with their drug licence state, the stores an order may go to,
    and every active drug with whether it can be ordered, and why not."""
    return purchasing.options()


@router.get("/purchase-orders/reorder-suggestions")
def reorder_suggestions(store_id: Optional[int] = Query(None, ge=1),
                        user: dict = Depends(_PS_READ)):
    """Drugs at or below their reorder level, with the suggested packs, the
    last rate paid and whether each can be ordered."""
    return {"suggestions": purchasing.reorder_suggestions(store_id=store_id)}


@router.get("/purchase-orders/{po_id}")
def get_purchase_order(po_id: int, user: dict = Depends(_PS_READ)):
    doc = purchasing.get_purchase_order(po_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"No purchase order {po_id}"})
    return {"purchase_order": doc}


# ── Writes ────────────────────────────────────────────────────────────────────

class POLineIn(BaseModel):
    helix_medication_id: str = Field(..., min_length=1, max_length=80)
    packs: Any = None
    unit_rate_paise: Any = None
    discount_bp: Any = 0
    remarks: Optional[str] = Field(None, max_length=500)


class PurchaseOrderIn(BaseModel):
    vendor_code: str = Field(..., min_length=1, max_length=40)
    store_code: str = Field(..., min_length=1, max_length=40)
    lines: List[POLineIn] = Field(..., min_length=1, max_length=purchasing.MAX_LINES)
    expected_date: Optional[str] = Field(None, max_length=10)
    is_interstate: bool = False
    place_of_supply: Optional[str] = Field(None, max_length=40)
    other_charges_paise: Any = 0
    terms: Optional[str] = Field(None, max_length=2000)
    remarks: Optional[str] = Field(None, max_length=2000)


@router.post("/purchase-orders")
def create_purchase_order(body: PurchaseOrderIn, request: Request,
                          user: dict = Depends(_PS_WRITE)):
    """Raise a draft. Refused for an unlicensed vendor, an unclassified drug,
    a drug with no pack size or GST rate, or an NDPS drug outside the vault."""
    try:
        return purchasing.create_purchase_order(
            vendor_code=body.vendor_code, store_code=body.store_code,
            lines=[line.model_dump() for line in body.lines], actor=user,
            expected_date=body.expected_date, is_interstate=body.is_interstate,
            place_of_supply=body.place_of_supply, other_charges_paise=body.other_charges_paise,
            terms=body.terms, remarks=body.remarks, audit_ctx=_ctx(request))
    except purchasing.PurchasingError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


class SuggestionItemIn(BaseModel):
    helix_medication_id: str = Field(..., min_length=1, max_length=80)
    unit_rate_paise: Any = None
    packs: Any = None
    discount_bp: Any = 0
    remarks: Optional[str] = Field(None, max_length=500)


class FromSuggestionsIn(BaseModel):
    vendor_code: str = Field(..., min_length=1, max_length=40)
    store_code: str = Field(..., min_length=1, max_length=40)
    items: List[SuggestionItemIn] = Field(..., min_length=1, max_length=purchasing.MAX_LINES)
    expected_date: Optional[str] = Field(None, max_length=10)
    is_interstate: bool = False
    remarks: Optional[str] = Field(None, max_length=2000)


@router.post("/purchase-orders/from-suggestions")
def create_from_suggestions(body: FromSuggestionsIn, request: Request,
                            user: dict = Depends(_PS_WRITE)):
    """Turn reorder suggestions into a draft. The quantity is the server's
    current suggestion unless packs are given; the rate is always typed."""
    try:
        return purchasing.create_from_suggestions(
            vendor_code=body.vendor_code, store_code=body.store_code,
            items=[item.model_dump() for item in body.items], actor=user,
            is_interstate=body.is_interstate, expected_date=body.expected_date,
            remarks=body.remarks, audit_ctx=_ctx(request))
    except purchasing.PurchasingError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/purchase-orders/{po_id}/submit")
def submit_purchase_order(po_id: int, request: Request, user: dict = Depends(_PS_WRITE)):
    try:
        return purchasing.submit_purchase_order(po_id, actor=user, audit_ctx=_ctx(request))
    except purchasing.PurchasingError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/purchase-orders/{po_id}/approve")
def approve_purchase_order(po_id: int, request: Request, user: dict = Depends(_PS_APPROVE)):
    """Never by the person who raised the order (403 same_person)."""
    try:
        return purchasing.approve_purchase_order(po_id, actor=user, audit_ctx=_ctx(request))
    except purchasing.PurchasingError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/purchase-orders/{po_id}/issue")
def issue_purchase_order(po_id: int, request: Request, user: dict = Depends(_PS_APPROVE)):
    """Send the order to the vendor. Its PHPO number is drawn after commit."""
    try:
        return purchasing.issue_purchase_order(po_id, actor=user, audit_ctx=_ctx(request))
    except purchasing.PurchasingError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


class CancelIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=1000)


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_purchase_order(po_id: int, body: CancelIn, request: Request,
                          user: dict = Depends(_cancel_tier)):
    try:
        return purchasing.cancel_purchase_order(
            po_id, reason=body.reason, actor=user,
            may_approve=has_permission(user.get("role", ""), PHARMACY_STORE_APPROVE),
            audit_ctx=_ctx(request))
    except purchasing.PurchasingError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()
