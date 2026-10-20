"""Pharmacy purchasing and goods receipt: endpoints under /api/pharmacy-store.

Purchase orders (grn-7, W4-C) and goods receipts (grn-8, W5-C). The package
router in routes/pharmacy_store/__init__.py adds the /api/pharmacy-store
prefix and the ORB_PHARMACY_STORE gate; this router has neither, so every
endpoint added here inherits both.

Endpoints (docs/his_design/grn.json, section E):
  purchase-orders, plus submit, approve, issue, cancel, reorder-suggestions
  goods-receipts, plus receivable, post and cancel  (expiry typed twice,
                                                     ruling GRN-RS6 (b), held)

Goods receipts: reading is the read tier; drafting and cancelling a draft are
the write tier; posting is read plus (write or approve), and the service then
needs the write tier, or for a short-dated batch the approve tier from someone
other than the receiver, with a reason (GRN-R16).

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

PER FACILITY (multisite-8h3, ruling MS-R9 (a)). With more than one facility on
record, an order, receipt or store of another facility is 404 (the same answer
as one that does not exist) after an authorization 'facility_document_denied'
row (services/pharmacy_facility.gate), before anything is read or written,
and the lists, options, receivable orders and suggestions are the caller's
facilities' only. With one facility on record nothing here changes.
"""

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from permissions import (
    PHARMACY_STORE_APPROVE,
    PHARMACY_STORE_READ,
    PHARMACY_STORE_WRITE,
    facility_scope,
    has_permission,
    require_permission,
)
from pydantic import BaseModel, Field
from services.audit_service import AuditWriteError

from services import pharmacy_facility
from services import pharmacy_purchasing_service as purchasing

router = APIRouter()

_PS_READ = require_permission(PHARMACY_STORE_READ)
_PS_WRITE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_WRITE)
_PS_APPROVE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_APPROVE)

_STATUS = {
    "not_found": 404,
    "same_person": 403,
    "approve_needed": 403,
    "write_needed": 403,
    "illegal_transition": 409,
    "already_received": 409,
    # Goods receipts (grn-8): a conflict with what is already on file.
    "po_not_receivable": 409,
    "line_closed": 409,
    "batch_expiry_mismatch": 409,
    "batch_mrp_mismatch": 409,
    "lot_status_conflict": 409,
    "idempotency_conflict": 409,
    "already_exists": 409,
}


def _fail(exc: purchasing.PurchasingError) -> HTTPException:
    detail = {"error": exc.code, "message": str(exc)}
    if getattr(exc, "detail", None):
        detail.update({k: v for k, v in exc.detail.items() if k not in detail})
    return HTTPException(status_code=_STATUS.get(exc.code, 422), detail=detail)


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


def _order_gate(user: dict, po_id: Any, request: Request) -> None:
    """404 for an order of another facility (multisite-8h3)."""
    pharmacy_facility.gate(
        user, "purchase_order", po_id, request, message=f"No purchase order {po_id}"
    )


def _receipt_gate(user: dict, grn_id: Any, request: Request) -> None:
    """404 for a goods receipt of another facility (multisite-8h3)."""
    pharmacy_facility.gate(
        user, "goods_receipt", grn_id, request, message=f"No goods receipt {grn_id}"
    )


def _store_gate(user: dict, store_code: Any, request: Request) -> None:
    """404 for a store of another facility named in a body (multisite-8h3)."""
    pharmacy_facility.gate(
        user, "store_code", store_code, request, message=f"no pharmacy store {store_code!r}"
    )


_CANCEL_TIER_USER_DEFAULT = Depends(_PS_READ)


def _cancel_tier(request: Request, user: dict = _CANCEL_TIER_USER_DEFAULT) -> dict:
    """Read plus write or approve. Which of the two the order needs is the
    service's call, by its status. A denial is audited as require_permission
    audits one."""
    role = user.get("role", "")
    if has_permission(role, PHARMACY_STORE_WRITE) or has_permission(role, PHARMACY_STORE_APPROVE):
        return user
    import uuid

    from services.audit_service import log_audit_event

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="permission_denied",
        outcome="denied",
        summary_message=f"Missing permissions: {PHARMACY_STORE_WRITE} or {PHARMACY_STORE_APPROVE}",
        actor_user_id=user.get("id"),
        actor_role=role,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={
            "required_permissions": [
                PHARMACY_STORE_READ,
                PHARMACY_STORE_WRITE,
                PHARMACY_STORE_APPROVE,
            ],
            "missing_permissions": [PHARMACY_STORE_WRITE, PHARMACY_STORE_APPROVE],
            "user_role": role,
        },
    )
    raise HTTPException(
        status_code=403,
        detail={
            "error": "permission_denied",
            "message": "Insufficient permissions",
            "required": [
                PHARMACY_STORE_READ,
                f"{PHARMACY_STORE_WRITE} or {PHARMACY_STORE_APPROVE}",
            ],
            "missing": [PHARMACY_STORE_WRITE, PHARMACY_STORE_APPROVE],
        },
    )


# ── Reads ─────────────────────────────────────────────────────────────────────


_LIST_PURCHASE_ORDERS_STATUS_DEFAULT = Query(None, max_length=40)
_LIST_PURCHASE_ORDERS_LIMIT_DEFAULT = Query(100, ge=1, le=500)
_LIST_PURCHASE_ORDERS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/purchase-orders")
def list_purchase_orders(
    status: str | None = _LIST_PURCHASE_ORDERS_STATUS_DEFAULT,
    limit: int = _LIST_PURCHASE_ORDERS_LIMIT_DEFAULT,
    user: dict = _LIST_PURCHASE_ORDERS_USER_DEFAULT,
) -> dict:
    """Pharmacy orders, newest first (the caller's facilities only)."""
    try:
        return {
            "purchase_orders": purchasing.list_purchase_orders(
                status=status, limit=limit, facilities=facility_scope(user)
            )
        }
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc


_PURCHASE_ORDER_OPTIONS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/purchase-orders/options")
def purchase_order_options(user: dict = _PURCHASE_ORDER_OPTIONS_USER_DEFAULT) -> dict:
    """Vendors with their drug licence state, the stores an order may go to
    (the caller's facilities' only), and every active drug with whether it
    can be ordered, and why not."""
    return purchasing.options(facilities=facility_scope(user))


_REORDER_SUGGESTIONS_STORE_ID_DEFAULT = Query(None, ge=1)
_REORDER_SUGGESTIONS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/purchase-orders/reorder-suggestions")
def reorder_suggestions(
    request: Request,
    store_id: int | None = _REORDER_SUGGESTIONS_STORE_ID_DEFAULT,
    user: dict = _REORDER_SUGGESTIONS_USER_DEFAULT,
) -> dict:
    """Drugs at or below their reorder level, with the suggested packs, the
    last rate paid and whether each can be ordered (the caller's facilities'
    stores; a named store of another facility is 404)."""
    if store_id is not None:
        pharmacy_facility.gate(
            user, "store", store_id, request, message=f"no pharmacy store {store_id}"
        )
    return {
        "suggestions": purchasing.reorder_suggestions(
            store_id=store_id, facilities=facility_scope(user)
        )
    }


_GET_PURCHASE_ORDER_USER_DEFAULT = Depends(_PS_READ)


@router.get("/purchase-orders/{po_id}")
def get_purchase_order(
    po_id: int, request: Request, user: dict = _GET_PURCHASE_ORDER_USER_DEFAULT
) -> dict:
    """One order; an order of another facility is 404."""
    _order_gate(user, po_id, request)
    doc = purchasing.get_purchase_order(po_id)
    if doc is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": f"No purchase order {po_id}"}
        )
    return {"purchase_order": doc}


# ── Writes ────────────────────────────────────────────────────────────────────


class POLineIn(BaseModel):
    helix_medication_id: str = Field(..., min_length=1, max_length=80)
    packs: Any = None
    unit_rate_paise: Any = None
    discount_bp: Any = 0
    remarks: str | None = Field(None, max_length=500)


class PurchaseOrderIn(BaseModel):
    vendor_code: str = Field(..., min_length=1, max_length=40)
    store_code: str = Field(..., min_length=1, max_length=40)
    lines: list[POLineIn] = Field(..., min_length=1, max_length=purchasing.MAX_LINES)
    expected_date: str | None = Field(None, max_length=10)
    is_interstate: bool = False
    place_of_supply: str | None = Field(None, max_length=40)
    other_charges_paise: Any = 0
    terms: str | None = Field(None, max_length=2000)
    remarks: str | None = Field(None, max_length=2000)


_CREATE_PURCHASE_ORDER_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/purchase-orders")
def create_purchase_order(
    body: PurchaseOrderIn, request: Request, user: dict = _CREATE_PURCHASE_ORDER_USER_DEFAULT
) -> dict:
    """Raise a draft. Refused for an unlicensed vendor, an unclassified drug,
    a drug with no pack size or GST rate, or an NDPS drug outside the vault;
    a store of another facility is 404."""
    _store_gate(user, body.store_code, request)
    try:
        return purchasing.create_purchase_order(
            vendor_code=body.vendor_code,
            store_code=body.store_code,
            lines=[line.model_dump() for line in body.lines],
            actor=user,
            expected_date=body.expected_date,
            is_interstate=body.is_interstate,
            place_of_supply=body.place_of_supply,
            other_charges_paise=body.other_charges_paise,
            terms=body.terms,
            remarks=body.remarks,
            audit_ctx=_ctx(request),
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class SuggestionItemIn(BaseModel):
    helix_medication_id: str = Field(..., min_length=1, max_length=80)
    unit_rate_paise: Any = None
    packs: Any = None
    discount_bp: Any = 0
    remarks: str | None = Field(None, max_length=500)


class FromSuggestionsIn(BaseModel):
    vendor_code: str = Field(..., min_length=1, max_length=40)
    store_code: str = Field(..., min_length=1, max_length=40)
    items: list[SuggestionItemIn] = Field(..., min_length=1, max_length=purchasing.MAX_LINES)
    expected_date: str | None = Field(None, max_length=10)
    is_interstate: bool = False
    remarks: str | None = Field(None, max_length=2000)


_CREATE_FROM_SUGGESTIONS_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/purchase-orders/from-suggestions")
def create_from_suggestions(
    body: FromSuggestionsIn, request: Request, user: dict = _CREATE_FROM_SUGGESTIONS_USER_DEFAULT
) -> dict:
    """Turn reorder suggestions into a draft. The quantity is the server's
    current suggestion unless packs are given; the rate is always typed. A
    store of another facility is 404."""
    _store_gate(user, body.store_code, request)
    try:
        return purchasing.create_from_suggestions(
            vendor_code=body.vendor_code,
            store_code=body.store_code,
            items=[item.model_dump() for item in body.items],
            actor=user,
            is_interstate=body.is_interstate,
            expected_date=body.expected_date,
            remarks=body.remarks,
            audit_ctx=_ctx(request),
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_SUBMIT_PURCHASE_ORDER_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/purchase-orders/{po_id}/submit")
def submit_purchase_order(
    po_id: int, request: Request, user: dict = _SUBMIT_PURCHASE_ORDER_USER_DEFAULT
) -> dict:
    """Send a draft for approval; an order of another facility is 404."""
    _order_gate(user, po_id, request)
    try:
        return purchasing.submit_purchase_order(po_id, actor=user, audit_ctx=_ctx(request))
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_APPROVE_PURCHASE_ORDER_USER_DEFAULT = Depends(_PS_APPROVE)


@router.post("/purchase-orders/{po_id}/approve")
def approve_purchase_order(
    po_id: int, request: Request, user: dict = _APPROVE_PURCHASE_ORDER_USER_DEFAULT
) -> dict:
    """Never by the person who raised the order (403 same_person). An order
    of another facility is 404."""
    _order_gate(user, po_id, request)
    try:
        return purchasing.approve_purchase_order(po_id, actor=user, audit_ctx=_ctx(request))
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_ISSUE_PURCHASE_ORDER_USER_DEFAULT = Depends(_PS_APPROVE)


@router.post("/purchase-orders/{po_id}/issue")
def issue_purchase_order(
    po_id: int, request: Request, user: dict = _ISSUE_PURCHASE_ORDER_USER_DEFAULT
) -> dict:
    """Send the order to the vendor. Its PHPO number is drawn after commit.
    An order of another facility is 404."""
    _order_gate(user, po_id, request)
    try:
        return purchasing.issue_purchase_order(po_id, actor=user, audit_ctx=_ctx(request))
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=1000)


_CANCEL_PURCHASE_ORDER_USER_DEFAULT = Depends(_cancel_tier)


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_purchase_order(
    po_id: int, body: CancelIn, request: Request, user: dict = _CANCEL_PURCHASE_ORDER_USER_DEFAULT
) -> dict:
    """Cancel an order, with a reason; an order of another facility is 404."""
    _order_gate(user, po_id, request)
    try:
        return purchasing.cancel_purchase_order(
            po_id,
            reason=body.reason,
            actor=user,
            may_approve=has_permission(user.get("role", ""), PHARMACY_STORE_APPROVE),
            audit_ctx=_ctx(request),
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


# ── Goods receipts (grn-8) ────────────────────────────────────────────────────


_LIST_GOODS_RECEIPTS_STATUS_DEFAULT = Query(None, max_length=40)
_LIST_GOODS_RECEIPTS_PURCHASE_ORDER_ID_DEFAULT = Query(None, ge=1)
_LIST_GOODS_RECEIPTS_LIMIT_DEFAULT = Query(100, ge=1, le=500)
_LIST_GOODS_RECEIPTS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/goods-receipts")
def list_goods_receipts(
    status: str | None = _LIST_GOODS_RECEIPTS_STATUS_DEFAULT,
    purchase_order_id: int | None = _LIST_GOODS_RECEIPTS_PURCHASE_ORDER_ID_DEFAULT,
    limit: int = _LIST_GOODS_RECEIPTS_LIMIT_DEFAULT,
    user: dict = _LIST_GOODS_RECEIPTS_USER_DEFAULT,
) -> dict:
    """Goods receipts, newest first (the caller's facilities only)."""
    try:
        return {
            "goods_receipts": purchasing.list_goods_receipts(
                status=status,
                purchase_order_id=purchase_order_id,
                limit=limit,
                facilities=facility_scope(user),
            )
        }
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc


_RECEIVABLE_ORDERS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/goods-receipts/receivable")
def receivable_orders(user: dict = _RECEIVABLE_ORDERS_USER_DEFAULT) -> dict:
    """Orders sent to a vendor with packs still to come, line by line, and
    the hospital's minimum shelf life at receipt (the caller's facilities'
    orders only)."""
    return purchasing.receivable_orders(facilities=facility_scope(user))


_GET_GOODS_RECEIPT_USER_DEFAULT = Depends(_PS_READ)


@router.get("/goods-receipts/{grn_id}")
def get_goods_receipt(
    grn_id: int, request: Request, user: dict = _GET_GOODS_RECEIPT_USER_DEFAULT
) -> dict:
    """One goods receipt; a receipt of another facility is 404."""
    _receipt_gate(user, grn_id, request)
    doc = purchasing.get_goods_receipt(grn_id)
    if doc is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": f"No goods receipt {grn_id}"}
        )
    return {"goods_receipt": doc}


class GRNLineIn(BaseModel):
    po_line_no: Any = None
    packs: Any = 0
    free_packs: Any = 0
    rejected_packs: Any = 0
    rejection_reason: str | None = Field(None, max_length=500)
    batch_no: str | None = Field(None, max_length=80)
    expiry_date: str | None = Field(None, max_length=10)
    expiry_date_confirm: str | None = Field(None, max_length=10)
    mrp_pack_paise: Any = None
    mfg_date: str | None = Field(None, max_length=10)
    unit_rate_paise: Any = None
    remarks: str | None = Field(None, max_length=500)


class GoodsReceiptIn(BaseModel):
    purchase_order_id: Any = None
    lines: list[GRNLineIn] = Field(..., min_length=1, max_length=purchasing.MAX_LINES)
    vendor_invoice_number: str | None = Field(None, max_length=80)
    vendor_invoice_date: str | None = Field(None, max_length=10)
    vendor_invoice_total_paise: Any = None
    store_code: str | None = Field(None, max_length=40)
    remarks: str | None = Field(None, max_length=2000)
    idempotency_key: str | None = Field(None, max_length=200)


_CREATE_GOODS_RECEIPT_IDEMPOTENCY_KEY_DEFAULT = Header(
    None, alias="Idempotency-Key", max_length=200
)
_CREATE_GOODS_RECEIPT_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/goods-receipts")
def create_goods_receipt(
    body: GoodsReceiptIn,
    request: Request,
    idempotency_key: str | None = _CREATE_GOODS_RECEIPT_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _CREATE_GOODS_RECEIPT_USER_DEFAULT,
) -> dict:
    """Record a delivery as a draft. Batch, MRP and an expiry typed twice on
    every line received; nothing moves into stock until it is posted. The
    Idempotency-Key header (or body field) makes a retry return the same
    draft. An order or store of another facility is 404; procurement's core
    refuses a store of another facility than the order's."""
    if body.purchase_order_id is not None:
        _order_gate(user, body.purchase_order_id, request)
    if body.store_code:
        _store_gate(user, body.store_code, request)
    try:
        return purchasing.create_goods_receipt(
            purchase_order_id=body.purchase_order_id,
            lines=[line.model_dump() for line in body.lines],
            actor=user,
            vendor_invoice_number=body.vendor_invoice_number,
            vendor_invoice_date=body.vendor_invoice_date,
            vendor_invoice_total_paise=body.vendor_invoice_total_paise,
            store_code=body.store_code,
            remarks=body.remarks,
            idempotency_key=idempotency_key or body.idempotency_key,
            audit_ctx=_ctx(request),
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class PostReceiptIn(BaseModel):
    short_expiry_reason: str | None = Field(None, max_length=1000)


_POST_GOODS_RECEIPT_USER_DEFAULT = Depends(_cancel_tier)


@router.post("/goods-receipts/{grn_id}/post")
def post_goods_receipt(
    grn_id: int,
    body: PostReceiptIn,
    request: Request,
    user: dict = _POST_GOODS_RECEIPT_USER_DEFAULT,
) -> dict:
    """Put the receipt on the shelf in one transaction. A short-dated batch
    needs the approve tier, someone other than the receiver, and a reason.
    Posting twice answers the posted receipt and moves nothing again. A
    receipt of another facility is 404; its lots take its store's facility."""
    _receipt_gate(user, grn_id, request)
    role = user.get("role", "")
    try:
        return purchasing.post_goods_receipt(
            grn_id,
            actor=user,
            may_write=has_permission(role, PHARMACY_STORE_WRITE),
            may_approve=has_permission(role, PHARMACY_STORE_APPROVE),
            short_expiry_reason=body.short_expiry_reason,
            audit_ctx=_ctx(request),
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_CANCEL_GOODS_RECEIPT_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/goods-receipts/{grn_id}/cancel")
def cancel_goods_receipt(
    grn_id: int, body: CancelIn, request: Request, user: dict = _CANCEL_GOODS_RECEIPT_USER_DEFAULT
) -> dict:
    """Cancel a draft, with a reason. A posted receipt is never cancelled. A
    receipt of another facility is 404."""
    _receipt_gate(user, grn_id, request)
    try:
        return purchasing.cancel_goods_receipt(
            grn_id, reason=body.reason, actor=user, audit_ctx=_ctx(request)
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None
