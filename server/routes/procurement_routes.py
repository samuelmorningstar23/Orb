"""Orb — Procurement & non-pharmacy stores API.

Vendors, the non-pharmacy item master, indent → purchase order → goods receipt,
stock on hand, reorder points, consumption and expiry.

════════════════════════════════════════════════════════════════════════════════
 THREE THINGS THIS ROUTER IS CAREFUL ABOUT
════════════════════════════════════════════════════════════════════════════════

1. IT IS NOT A CLINICAL PATH. Nothing here writes to medications, vitals,
   alerts, orders, admissions or the shadow-mode evidence capture, and nothing
   here may ever block one. A store that has run out shows a warning on a
   screen; it does not stop a nurse.

2. IT IS BEHIND A MODULE SWITCH. `config.PROCUREMENT_ENABLED` (env
   `ORB_PROCUREMENT`, DEFAULT ON per the 2026-08-11 ruling). A site that runs
   its own purchasing system sets it to 0; every route below then answers 404
   with a body that says WHY, so a stale client gets an explanation instead of a
   mystery. The frontend hides the tab from the same flag via /api/health.

3. IT SEPARATES RAISING FROM APPROVING. The module runs on its own three-tier
   permission family (`permissions.py`, split 2026-08-18):

     `procurement.read`    — the ledgers: catalogue, vendors, stores, indents,
                             POs, GRNs, stock on hand and every report. Seeing
                             the shelf commits nothing.
     `procurement.write`   — the day's routine writes: raising, submitting and
                             withdrawing indents, drafting and submitting POs,
                             recording a goods receipt, moving stock.
     `procurement.approve` — money and vendor standing: deciding an indent,
                             approving / issuing / cancelling a PO,
                             blacklisting a vendor, writing off stock, and the
                             store master itself.

   The person who asks for the money is not the person who signs for it — that
   is the oldest control in purchasing. A pharmacist holds read+write, so they
   keep their long-standing ability to raise an indent but cannot approve one;
   and a site can now hand a storekeeper exactly read+write (or a purchase
   officer all three) without minting another admin account. Before the split
   these routes borrowed `helix.read` / `helix.write` and `admin.overview`;
   the raise-vs-approve boundary drawn then is unchanged, it just has its own
   names now.

All handlers are plain `def`, so FastAPI runs them in a threadpool: everything
below opens SQLite, and blocking the event loop is the failure mode this repo
has shipped three production freezes from.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import config
from permissions import (
    enforce_patient_scope,
    PROCUREMENT_APPROVE,
    PROCUREMENT_READ,
    PROCUREMENT_WRITE,
    ROLE_PERMISSIONS,
    require_permission,
)
from services import procurement_service as svc
from services.audit_service import log_audit_event

logger = logging.getLogger("orb.procurement_routes")

router = APIRouter(prefix="/api/procurement", tags=["Procurement & Stores"])


# ── Module switch ────────────────────────────────────────────────────────────

def require_module() -> None:
    """404 when the site has switched procurement off.

    404 rather than 403: the module is not disallowed for this user, it is not
    present at this deployment. The body says so, because a client that was
    built before the flag flipped deserves a sentence rather than a blank."""
    if not config.PROCUREMENT_ENABLED:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "module_disabled",
                "module": "procurement",
                "message": (
                    "The procurement & stores module is switched off for this "
                    "deployment (ORB_PROCUREMENT=0). This site runs its own "
                    "purchasing system."
                ),
            },
        )


# ── Who can reach the stores ─────────────────────────────────────────────────
# One dependency per tier, built once at import (the payer-desk pattern).
#
# WRITE and APPROVE both require READ as well, and that is not belt-and-braces:
# every mutating handler answers with the document it touched — an indent, a PO
# with its money columns, a GRN — so a write-only grant would be a read grant
# with an extra step. APPROVE deliberately does NOT imply WRITE: an approver
# who cannot quietly edit the document they are signing is the point of having
# the tier at all.
_PROC_READ = require_permission(PROCUREMENT_READ)
_PROC_WRITE = require_permission(PROCUREMENT_READ, PROCUREMENT_WRITE)
_PROC_APPROVE = require_permission(PROCUREMENT_READ, PROCUREMENT_APPROVE)


def _fail(exc: svc.ProcurementError) -> HTTPException:
    """Map a service error to HTTP. The `code` is carried through so a client can
    branch on it without matching English."""
    status = {
        "not_found": 404,
        "unknown_item": 404,
        "illegal_transition": 409,
        "over_receipt": 409,
        "insufficient_stock": 409,
        "already_received": 409,
        "po_not_receivable": 409,
        "indent_not_approved": 409,
        "vendor_blacklisted": 409,
        "vendor_inactive": 409,
        "line_closed": 409,
        "already_exists": 409,
        "pharmacy_item_refused": 422,
    }.get(exc.code, 400)
    return HTTPException(status_code=status,
                         detail={"error": exc.code, "message": str(exc)})


def _audit(request: Request, user: dict, action: str, *, summary: str,
           target_type: str, target_id: Optional[str] = None,
           metadata: Optional[dict] = None) -> None:
    """Every procurement WRITE leaves a trail.

    Category is `security` (fail-closed, awaited) rather than `clinical_access`
    or `phi_access` — a purchase order is an administrative act on hospital
    money, and mixing it into the clinical categories would dilute the trail a
    clinical investigation reads. `patient_id` is deliberately never passed:
    the only patient link in this module is a costing tag on a stock issue, and
    an audit row is not the place to widen it."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome="success",
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata=metadata or {},
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Request models
# ═════════════════════════════════════════════════════════════════════════════

class VendorIn(BaseModel):
    code: str
    name: str
    vendor_type: str = "supplier"
    gstin: Optional[str] = None
    pan: Optional[str] = None
    msme_number: Optional[str] = None
    state_code: Optional[str] = None
    address: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    payment_terms_days: Optional[int] = None
    bank_name: Optional[str] = None
    #: LAST FOUR DIGITS ONLY. Orb has no vendor-payment rail and holds PHI;
    #: a full account number would make it a target for no operational gain.
    account_last4: Optional[str] = None
    ifsc: Optional[str] = None
    rating: Optional[int] = None
    is_active: bool = True
    notes: Optional[str] = None
    #: Set by the Add form: an existing code is refused (409), not overwritten.
    create_only: bool = False


class BlacklistIn(BaseModel):
    blacklisted: bool = True
    reason: Optional[str] = None


class StoreIn(BaseModel):
    code: str
    name: str
    store_type: str = "sub_store"
    department: Optional[str] = None
    ward: Optional[str] = None
    incharge_user_id: Optional[str] = None
    is_active: bool = True
    create_only: bool = False


class ItemIn(BaseModel):
    code: str
    name: str
    category: str = "consumable"
    uom: str = "each"
    pack_size: int = 1
    hsn_sac: Optional[str] = None
    gst_treatment: str = "taxable"
    gst_rate_bp: int = 0
    is_chargeable: bool = False
    service_item_id: Optional[int] = None
    #: WHOLE UNITS, not milli — mirrors the column, which has no `_milli` suffix.
    reorder_level_qty: Optional[int] = None
    reorder_qty: Optional[int] = None
    lead_time_days: Optional[int] = None
    default_store_id: Optional[int] = None
    is_active: bool = True
    notes: Optional[str] = None
    #: Present ONLY so a client that sets it gets a refusal that names Helix.
    is_pharmacy: bool = False
    create_only: bool = False


class IndentLineIn(BaseModel):
    item_code: str
    quantity_milli: int
    description: Optional[str] = None
    uom: Optional[str] = None
    remarks: Optional[str] = None


class IndentIn(BaseModel):
    store_code: Optional[str] = None
    department: Optional[str] = None
    ward: Optional[str] = None
    priority: str = "routine"
    required_by: Optional[str] = None
    remarks: Optional[str] = None
    lines: list[IndentLineIn] = Field(default_factory=list)
    submit: bool = False


class IndentDecisionIn(BaseModel):
    #: line_no -> approved_quantity_milli. A line not named is approved in full.
    approvals: dict[int, int] = Field(default_factory=dict)
    reject: bool = False
    reason: Optional[str] = None


class POLineIn(BaseModel):
    item_code: str
    quantity_milli: int
    unit_rate_paise: int
    description: Optional[str] = None
    uom: Optional[str] = None
    discount_paise: int = 0
    discount_bp: int = 0
    gst_treatment: Optional[str] = None
    gst_rate_bp: Optional[int] = None
    indent_line_id: Optional[int] = None
    remarks: Optional[str] = None


class PurchaseOrderIn(BaseModel):
    vendor_code: str
    store_code: Optional[str] = None
    indent_id: Optional[int] = None
    order_date: Optional[str] = None
    expected_date: Optional[str] = None
    place_of_supply: Optional[str] = None
    #: EXPLICIT, never inferred — Orb has no configured home state to compare a
    #: vendor's state code against, and guessing puts the wrong GST heads on a
    #: real document.
    is_interstate: bool = False
    other_charges_paise: int = 0
    terms: Optional[str] = None
    remarks: Optional[str] = None
    lines: list[POLineIn] = Field(default_factory=list)


class ReasonIn(BaseModel):
    reason: Optional[str] = None


class GRNLineIn(BaseModel):
    po_line_no: int
    accepted_quantity_milli: int = 0
    rejected_quantity_milli: int = 0
    batch_no: Optional[str] = None
    expiry_date: Optional[str] = None
    rejection_reason: Optional[str] = None
    unit_rate_paise: Optional[int] = None
    uom: Optional[str] = None
    description: Optional[str] = None
    remarks: Optional[str] = None


class GoodsReceiptIn(BaseModel):
    purchase_order_id: int
    store_code: Optional[str] = None
    vendor_invoice_number: Optional[str] = None
    vendor_invoice_date: Optional[str] = None
    vendor_invoice_total_paise: Optional[int] = None
    qc_status: str = "passed"
    remarks: Optional[str] = None
    #: Retry-safe. A repeated POST with the same key returns the receipt that
    #: already exists instead of posting the same delivery into stock twice.
    idempotency_key: Optional[str] = None
    post: bool = True
    lines: list[GRNLineIn] = Field(default_factory=list)


class StockMovementIn(BaseModel):
    item_code: str
    store_code: str
    movement_type: str
    quantity_milli: int
    batch_no: Optional[str] = None
    expiry_date: Optional[str] = None
    unit_rate_paise: Optional[int] = None
    department: Optional[str] = None
    #: A COSTING tag only. It records that a consumable left the store for this
    #: patient so it can be charged; it writes nothing clinical.
    patient_id: Optional[str] = None
    remarks: Optional[str] = None
    idempotency_key: Optional[str] = None


# ═════════════════════════════════════════════════════════════════════════════
#  Overview
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/overview")
def overview(
    store_code: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    """The stores dashboard: low stock, expiring batches, open orders."""
    return svc.procurement_overview(store_code=store_code)


@router.get("/meta")
def meta(
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    """The vocabulary a client needs to render forms without hard-coding it, plus
    the state machines, so the UI offers exactly the transitions the server will
    accept rather than its own guess at them."""
    return {
        "item_categories": list(svc.ITEM_CATEGORIES),
        "expiry_tracked_categories": sorted(svc.EXPIRY_TRACKED_CATEGORIES),
        "vendor_types": list(svc.VENDOR_TYPES),
        "store_types": list(svc.STORE_TYPES),
        "movement_types": {
            "inward": list(svc.INWARD_MOVEMENTS),
            "outward": list(svc.OUTWARD_MOVEMENTS),
            "consumption": list(svc.CONSUMPTION_MOVEMENTS),
        },
        "transitions": {
            "indent": {k: sorted(v) for k, v in svc.INDENT_TRANSITIONS.items()},
            "purchase_order": {k: sorted(v) for k, v in svc.PO_TRANSITIONS.items()},
            "goods_receipt": {k: sorted(v) for k, v in svc.GRN_TRANSITIONS.items()},
        },
        "receivable_po_statuses": sorted(svc.RECEIVABLE_PO_STATUSES),
        "quantity_scale": svc.QUANTITY_SCALE,
        "pharmacy_boundary": {
            "owner": "helix",
            "message": (
                "Medications are stocked in Helix (Pharmacy), not here. This "
                "module holds non-pharmacy stores only: consumables, implants, "
                "reagents, linen, PPE, instruments, housekeeping and stationery."
            ),
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Vendors
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/vendors")
def list_vendors(
    active_only: bool = Query(True),
    include_blacklisted: bool = Query(False),
    search: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    return {"vendors": svc.list_vendors(active_only=active_only,
                                        include_blacklisted=include_blacklisted,
                                        search=search)}


@router.post("/vendors")
def upsert_vendor(
    payload: VendorIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    try:
        vendor = svc.upsert_vendor(**payload.model_dump(), created_by=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_vendor_upserted",
           summary=f"Vendor {vendor['code']} ({vendor['name']}) created/updated",
           target_type="vendor", target_id=vendor["code"])
    return vendor


@router.post("/vendors/{code}/blacklist")
def blacklist_vendor(
    code: str,
    payload: BlacklistIn,
    request: Request,
    _module=Depends(require_module),
    # Blacklisting is a commercial sanction with contractual consequences.
    user=Depends(_PROC_APPROVE),
):
    try:
        vendor = svc.set_vendor_blacklist(code.upper(), blacklisted=payload.blacklisted,
                                          reason=payload.reason, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_vendor_blacklist_changed",
           summary=f"Vendor {code.upper()} blacklist set to {payload.blacklisted}",
           target_type="vendor", target_id=code.upper(),
           metadata={"blacklisted": payload.blacklisted, "reason": payload.reason})
    return vendor


# ═════════════════════════════════════════════════════════════════════════════
#  Stores & items
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/stores")
def list_stores(
    active_only: bool = Query(True),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    return {"stores": svc.list_stores(active_only=active_only)}


@router.post("/stores")
def upsert_store(
    payload: StoreIn,
    request: Request,
    _module=Depends(require_module),
    # The store master is the control structure of hospital stock — where it
    # lives and who is in charge of it. Admin-only before the split; it stays on
    # the approval tier so a write grant (raising the day's indents) cannot
    # reshape the structure those indents move stock through.
    user=Depends(_PROC_APPROVE),
):
    try:
        store = svc.upsert_store(**payload.model_dump())
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_store_upserted",
           summary=f"Store {store['code']} ({store['name']}) created/updated",
           target_type="store", target_id=store["code"])
    return store


@router.get("/items")
def list_items(
    category: Optional[str] = Query(None),
    active_only: bool = Query(True),
    search: Optional[str] = Query(None),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    return {"items": svc.list_inventory_items(category=category,
                                              active_only=active_only, search=search)}


@router.get("/items/{code}")
def get_item(
    code: str,
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    item = svc.get_inventory_item(code.upper())
    if not item:
        raise HTTPException(status_code=404, detail={
            "error": "unknown_item",
            "message": f"No non-pharmacy item with code {code.upper()!r}. If it is "
                       "a medication, it is held in Helix (Pharmacy).",
        })
    return item


@router.post("/items")
def upsert_item(
    payload: ItemIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    try:
        item = svc.upsert_inventory_item(**payload.model_dump())
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_item_upserted",
           summary=f"Inventory item {item['code']} ({item['name']}) created/updated",
           target_type="inventory_item", target_id=item["code"])
    return item


# ═════════════════════════════════════════════════════════════════════════════
#  Indents
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/indents")
def list_indents(
    status: Optional[str] = Query(None),
    store_code: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    return {"indents": svc.list_indents(status=status, store_code=store_code,
                                        department=department, limit=limit)}


@router.get("/indents/{indent_id}")
def get_indent(
    indent_id: int,
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    doc = svc.get_indent(indent_id)
    if not doc:
        raise HTTPException(status_code=404, detail={"error": "not_found",
                                                     "message": f"No indent {indent_id}"})
    return doc


@router.post("/indents")
def create_indent(
    payload: IndentIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    try:
        doc = svc.create_indent(
            store_code=payload.store_code, department=payload.department,
            ward=payload.ward, priority=payload.priority,
            required_by=payload.required_by,
            lines=[line.model_dump() for line in payload.lines],
            requested_by=user.get("id"), remarks=payload.remarks,
            submit=payload.submit,
        )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_indent_created",
           summary=f"Indent {doc.get('indent_number') or doc['id']} raised "
                   f"({len(doc.get('lines', []))} lines, status {doc['status']})",
           target_type="indent", target_id=doc["id"],
           metadata={"status": doc["status"], "priority": doc["priority"]})
    return doc


@router.post("/indents/{indent_id}/submit")
def submit_indent(
    indent_id: int,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    try:
        doc = svc.submit_indent(indent_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_indent_submitted",
           summary=f"Indent {doc.get('indent_number') or indent_id} submitted",
           target_type="indent", target_id=indent_id)
    return doc


@router.post("/indents/{indent_id}/decide")
def decide_indent(
    indent_id: int,
    payload: IndentDecisionIn,
    request: Request,
    _module=Depends(require_module),
    # Approving an indent commits the hospital to spend. Deliberately a stricter
    # permission than raising one.
    user=Depends(_PROC_APPROVE),
):
    try:
        doc = svc.decide_indent(indent_id, approvals=payload.approvals,
                                reject=payload.reject, reason=payload.reason,
                                actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_indent_decided",
           summary=f"Indent {doc.get('indent_number') or indent_id} → {doc['status']}",
           target_type="indent", target_id=indent_id,
           metadata={"status": doc["status"], "rejected": payload.reject})
    return doc


@router.post("/indents/{indent_id}/cancel")
def cancel_indent(
    indent_id: int,
    payload: ReasonIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    try:
        doc = svc.cancel_indent(indent_id, reason=payload.reason, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_indent_cancelled",
           summary=f"Indent {doc.get('indent_number') or indent_id} cancelled",
           target_type="indent", target_id=indent_id,
           metadata={"reason": payload.reason})
    return doc


# ═════════════════════════════════════════════════════════════════════════════
#  Purchase orders
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/purchase-orders")
def list_purchase_orders(
    status: Optional[str] = Query(None),
    vendor_code: Optional[str] = Query(None),
    open_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    return {"purchase_orders": svc.list_purchase_orders(
        status=status, vendor_code=vendor_code, open_only=open_only, limit=limit)}


@router.get("/purchase-orders/{po_id}")
def get_purchase_order(
    po_id: int,
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    doc = svc.get_purchase_order(po_id)
    if not doc:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"No purchase order {po_id}"})
    return doc


@router.post("/purchase-orders")
def create_purchase_order(
    payload: PurchaseOrderIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    try:
        doc = svc.create_purchase_order(
            vendor_code=payload.vendor_code,
            lines=[line.model_dump() for line in payload.lines],
            store_code=payload.store_code, indent_id=payload.indent_id,
            order_date=payload.order_date, expected_date=payload.expected_date,
            place_of_supply=payload.place_of_supply,
            is_interstate=payload.is_interstate,
            other_charges_paise=payload.other_charges_paise,
            terms=payload.terms, remarks=payload.remarks,
            created_by=user.get("id"),
        )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_po_created",
           summary=f"Purchase order drafted for {doc.get('vendor_name')} "
                   f"({len(doc.get('lines', []))} lines)",
           target_type="purchase_order", target_id=doc["id"],
           metadata={"total_paise": doc.get("total_paise"),
                     "vendor_code": doc.get("vendor_code"),
                     "is_interstate": doc.get("is_interstate")})
    return doc


@router.post("/purchase-orders/{po_id}/submit")
def submit_purchase_order(
    po_id: int,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    try:
        doc = svc.submit_purchase_order(po_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_po_submitted",
           summary=f"Purchase order {po_id} submitted for approval",
           target_type="purchase_order", target_id=po_id)
    return doc


@router.post("/purchase-orders/{po_id}/approve")
def approve_purchase_order(
    po_id: int,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_APPROVE),
):
    try:
        doc = svc.approve_purchase_order(po_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_po_approved",
           summary=f"Purchase order {po_id} approved "
                   f"({doc.get('total_paise')} paise to {doc.get('vendor_name')})",
           target_type="purchase_order", target_id=po_id,
           metadata={"total_paise": doc.get("total_paise")})
    return doc


@router.post("/purchase-orders/{po_id}/issue")
def issue_purchase_order(
    po_id: int,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_APPROVE),
):
    try:
        doc = svc.issue_purchase_order(po_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_po_issued",
           summary=f"Purchase order {doc.get('po_number') or po_id} issued to "
                   f"{doc.get('vendor_name')}",
           target_type="purchase_order", target_id=po_id,
           metadata={"po_number": doc.get("po_number")})
    return doc


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_purchase_order(
    po_id: int,
    payload: ReasonIn,
    request: Request,
    _module=Depends(require_module),
    # Withdrawing a commitment made to a vendor is the same kind of act as
    # making one, so it sits on the same tier as approve/issue.
    user=Depends(_PROC_APPROVE),
):
    try:
        doc = svc.cancel_purchase_order(po_id, reason=payload.reason or "",
                                        actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_po_cancelled",
           summary=f"Purchase order {doc.get('po_number') or po_id} cancelled",
           target_type="purchase_order", target_id=po_id,
           metadata={"reason": payload.reason})
    return doc


# ═════════════════════════════════════════════════════════════════════════════
#  Goods receipt
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/goods-receipts")
def list_goods_receipts(
    purchase_order_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    return {"goods_receipts": svc.list_goods_receipts(
        purchase_order_id=purchase_order_id, status=status, limit=limit)}


@router.get("/goods-receipts/{grn_id}")
def get_goods_receipt(
    grn_id: int,
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    doc = svc.get_goods_receipt(grn_id)
    if not doc:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"No goods receipt {grn_id}"})
    return doc


@router.post("/goods-receipts")
def create_goods_receipt(
    payload: GoodsReceiptIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    """Receive a delivery. Partial receipt is the normal case; over-receipt is
    refused with 409."""
    try:
        doc = svc.create_goods_receipt(
            purchase_order_id=payload.purchase_order_id,
            lines=[line.model_dump() for line in payload.lines],
            store_code=payload.store_code,
            vendor_invoice_number=payload.vendor_invoice_number,
            vendor_invoice_date=payload.vendor_invoice_date,
            vendor_invoice_total_paise=payload.vendor_invoice_total_paise,
            qc_status=payload.qc_status, received_by=user.get("id"),
            remarks=payload.remarks, idempotency_key=payload.idempotency_key,
            post=payload.post,
        )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    if not doc.get("idempotent_replay"):
        _audit(request, user, "procurement_grn_created",
               summary=f"Goods receipt {doc.get('grn_number') or doc['id']} "
                       f"({doc['status']}) against PO {payload.purchase_order_id}",
               target_type="goods_receipt", target_id=doc["id"],
               metadata={"status": doc["status"],
                         "purchase_order_id": payload.purchase_order_id,
                         "line_count": len(doc.get("lines", []))})
    return doc


@router.post("/goods-receipts/{grn_id}/post")
def post_goods_receipt(
    grn_id: int,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    try:
        doc = svc.post_goods_receipt(grn_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_grn_posted",
           summary=f"Goods receipt {doc.get('grn_number') or grn_id} posted into "
                   f"store {doc.get('store_code')}",
           target_type="goods_receipt", target_id=grn_id)
    return doc


@router.post("/goods-receipts/{grn_id}/cancel")
def cancel_goods_receipt(
    grn_id: int,
    payload: ReasonIn,
    request: Request,
    _module=Depends(require_module),
    # Only a DRAFT is cancellable (the state machine refuses a posted one), and
    # a draft has moved no stock — but erasing a recorded delivery document was
    # deliberately kept above the clerk who recorded it before the split, and
    # stays there: the hands that record a receipt cannot also disappear it.
    user=Depends(_PROC_APPROVE),
):
    try:
        doc = svc.cancel_goods_receipt(grn_id, reason=payload.reason or "",
                                       actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_grn_cancelled",
           summary=f"Draft goods receipt {grn_id} cancelled",
           target_type="goods_receipt", target_id=grn_id,
           metadata={"reason": payload.reason})
    return doc


# ═════════════════════════════════════════════════════════════════════════════
#  Stock
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/stock")
def stock(
    store_code: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    low_only: bool = Query(False),
    include_zero: bool = Query(True),
    limit: int = Query(500, ge=1, le=1000),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    return {"stock": svc.list_stock_on_hand(store_code=store_code, category=category,
                                            low_only=low_only,
                                            include_zero=include_zero, limit=limit)}


@router.get("/stock/{item_code}/ledger")
def stock_ledger(
    item_code: str,
    store_code: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    return {
        "item_code": item_code.upper(),
        "on_hand": svc.stock_on_hand(item_code, store_code),
        "movements": svc.stock_ledger(item_code, store_code=store_code, limit=limit),
    }


@router.post("/stock/movements")
def record_movement(
    payload: StockMovementIn,
    request: Request,
    _module=Depends(require_module),
    user=Depends(_PROC_WRITE),
):
    """Issue, return, transfer, adjust or write off stock.

    A WRITE-OFF needs the stricter permission: it is the one movement that makes
    stock disappear without anybody receiving it, so a store cannot quietly
    balance its own books."""
    # PHI SCOPE (2026-08-12). A stock movement may be charged to a patient, and
    # that patient is named in the request BODY where no path guard can see it.
    # Consuming stock against another ward's patient both discloses them and
    # writes a durable row. Optional field, so only checked when supplied.
    if getattr(payload, "patient_id", None):
        enforce_patient_scope(user, payload.patient_id)

    if payload.movement_type in ("write_off", "adjustment", "expiry"):
        role = user.get("role", "")
        if PROCUREMENT_APPROVE not in ROLE_PERMISSIONS.get(role, set()):
            raise HTTPException(status_code=403, detail={
                "error": "permission_denied",
                "message": (
                    f"A {payload.movement_type} removes stock without a receipt "
                    "and needs procurement approval authority. Ask for a stock "
                    "adjustment to be approved rather than issuing it out."
                ),
                "required": [PROCUREMENT_APPROVE],
            })
    try:
        if payload.movement_type == "issue":
            row = svc.issue_stock(
                item_code=payload.item_code, store_code=payload.store_code,
                quantity_milli=payload.quantity_milli, department=payload.department,
                patient_id=payload.patient_id, batch_no=payload.batch_no,
                moved_by=user.get("id"), idempotency_key=payload.idempotency_key,
                remarks=payload.remarks,
            )
        else:
            row = svc.record_stock_movement(
                item_code=payload.item_code, store_code=payload.store_code,
                movement_type=payload.movement_type,
                quantity_milli=payload.quantity_milli, batch_no=payload.batch_no,
                expiry_date=payload.expiry_date,
                unit_rate_paise=payload.unit_rate_paise,
                reference_type="manual", patient_id=payload.patient_id,
                moved_by=user.get("id"), idempotency_key=payload.idempotency_key,
                remarks=payload.remarks,
                allow_negative=payload.movement_type == "adjustment",
            )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "procurement_stock_movement",
           summary=f"{payload.movement_type} of {payload.item_code} in "
                   f"{payload.store_code}",
           target_type="inventory_item", target_id=payload.item_code.upper(),
           metadata={"movement_type": payload.movement_type,
                     "quantity_milli": payload.quantity_milli,
                     "store_code": payload.store_code.upper(),
                     "balance_after_milli": row.get("balance_after_milli")})
    return row


# ═════════════════════════════════════════════════════════════════════════════
#  Reports
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/reports/low-stock")
def low_stock(
    store_code: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    rows = svc.low_stock(store_code=store_code, limit=limit)
    return {"low_stock": rows, "count": len(rows)}


@router.get("/reports/consumption")
def consumption(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    store_code: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    try:
        return svc.consumption_report(date_from=date_from, date_to=date_to,
                                      department=department, store_code=store_code,
                                      limit=limit)
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc


@router.get("/reports/expiry")
def expiry(
    within_days: int = Query(90, ge=0, le=3650),
    store_code: Optional[str] = Query(None),
    include_expired: bool = Query(True),
    limit: int = Query(200, ge=1, le=1000),
    _module=Depends(require_module),
    user=Depends(_PROC_READ),
):
    rows = svc.expiry_report(within_days=within_days, store_code=store_code,
                             include_expired=include_expired, limit=limit)
    return {
        "expiring": rows,
        "count": len(rows),
        "expired_count": sum(1 for r in rows if r["is_expired"]),
        "within_days": within_days,
    }
