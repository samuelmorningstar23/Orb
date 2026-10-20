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

ONE FACILITY AT A TIME (multisite-9b). Stores, indents, purchase orders and
goods receipts carry the facility they belong to (migration 75, stamped by
services/procurement_service). With more than one facility of a group on the
appliance a stores desk works for its own facility (ruling MS-R9 (a)): every
list keeps to the caller's facilities' stores and documents, a store or
document of another facility answers 404 exactly like one that does not
exist, and a new store, indent or order that would land at another facility
is refused (403 facility_scope). The vendor and item masters stay group-wide:
one legal entity buys for every facility. With one facility on record the
caller's facility scope is None and none of this runs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from typing import Any

import config
from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import (
    PROCUREMENT_APPROVE,
    PROCUREMENT_READ,
    PROCUREMENT_WRITE,
    ROLE_PERMISSIONS,
    enforce_patient_scope,
    facility_scope,
    require_permission,
)
from pydantic import BaseModel, Field
from services.audit_service import log_audit_event
from services.money import format_inr_or_unset

from services import facility_stamp
from services import procurement_service as svc

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
    return HTTPException(status_code=status, detail={"error": exc.code, "message": str(exc)})


# ── Facilities (multisite-9b; see the module docstring) ─────────────────────

#: Fetched for a facility-limited reader before the limit is applied, so a
#: list is cut to its limit after the facility filter, never before it.
_SCOPED_FETCH = 500


def _at(row: Any, facilities: frozenset[int] | None) -> bool:
    """Is this store or document at one of `facilities`? Always with no limit."""
    if facilities is None:
        return True
    if not isinstance(row, dict):
        return False
    fid = row.get("facility_id")
    return fid is not None and int(fid) in facilities


def _store_codes(facilities: frozenset[int]) -> set[str]:
    """The codes of the stores at `facilities`."""
    return {
        str(s["code"]).upper() for s in svc.list_stores(active_only=False) if _at(s, facilities)
    }


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "not_found", "message": f"No {what}"})


def _require_store(code: str | None, facilities: frozenset[int] | None) -> None:
    """A store named by a facility-limited caller must be one of theirs: 404
    otherwise, the answer for a store that does not exist."""
    if facilities is None or not code:
        return
    if not _at(svc.get_store(code.upper()), facilities):
        raise _not_found(f"store {code.upper()}")


def _require_doc(load: Callable[[], Any], facilities: frozenset[int] | None, what: str) -> None:
    """For a facility-limited caller, 404 when the document does not exist or
    is at another facility, before anything is changed. Nothing is read with
    no facility limit: the service answers a missing document as before."""
    if facilities is None:
        return
    doc = load()
    if not doc or not _at(doc, facilities):
        raise _not_found(what)


def _keep_docs(rows: Iterable[dict], facilities: frozenset[int] | None, limit: int) -> list:
    if facilities is None:
        return list(rows)
    return [r for r in rows if _at(r, facilities)][:limit]


def _keep_store_rows(rows: Iterable[dict], codes: set[str]) -> list:
    """Rows of the caller's stores; a row that names no store (an item nothing
    has ever moved) carries no facility's stock and is kept."""
    return [r for r in rows if not r.get("store_code") or str(r["store_code"]).upper() in codes]


def _refuse_elsewhere(
    facilities: frozenset[int] | None,
    *,
    department: str | None = None,
    ward: str | None = None,
    account: str | None = None,
) -> None:
    """403 facility_scope when a department, ward or in-charge account named
    for a new store or document is at another facility than the caller's."""
    if facilities is None:
        return
    with db_session() as db:
        named = (
            (
                "department",
                facility_stamp.department_facility(db, department) if department else None,
            ),
            ("ward", facility_stamp.ward_facility(db, ward=ward) if ward else None),
            ("person in charge", facility_stamp.account_facility(db, account) if account else None),
        )
    for label, fid in named:
        if fid is not None and fid not in facilities:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "facility_scope",
                    "message": f"That {label} is at another facility, so nothing was saved.",
                },
            )


def _audit(
    request: Request,
    user: dict,
    action: str,
    *,
    summary: str,
    target_type: str,
    target_id: str | int | None = None,
    metadata: dict | None = None,
) -> None:
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
    gstin: str | None = None
    pan: str | None = None
    msme_number: str | None = None
    state_code: str | None = None
    address: str | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    payment_terms_days: int | None = None
    bank_name: str | None = None
    #: LAST FOUR DIGITS ONLY. Orb has no vendor-payment rail and holds PHI;
    #: a full account number would make it a target for no operational gain.
    account_last4: str | None = None
    ifsc: str | None = None
    rating: int | None = None
    is_active: bool = True
    notes: str | None = None
    #: Set by the Add form: an existing code is refused (409), not overwritten.
    create_only: bool = False


class BlacklistIn(BaseModel):
    blacklisted: bool = True
    reason: str | None = None


class StoreIn(BaseModel):
    code: str
    name: str
    store_type: str = "sub_store"
    department: str | None = None
    ward: str | None = None
    incharge_user_id: str | None = None
    is_active: bool = True
    create_only: bool = False


class ItemIn(BaseModel):
    code: str
    name: str
    category: str = "consumable"
    uom: str = "each"
    pack_size: int = 1
    hsn_sac: str | None = None
    gst_treatment: str = "taxable"
    gst_rate_bp: int = 0
    is_chargeable: bool = False
    service_item_id: int | None = None
    #: WHOLE UNITS, not milli — mirrors the column, which has no `_milli` suffix.
    reorder_level_qty: int | None = None
    reorder_qty: int | None = None
    lead_time_days: int | None = None
    default_store_id: int | None = None
    is_active: bool = True
    notes: str | None = None
    #: Present ONLY so a client that sets it gets a refusal that names Helix.
    is_pharmacy: bool = False
    create_only: bool = False


class IndentLineIn(BaseModel):
    item_code: str
    quantity_milli: int
    description: str | None = None
    uom: str | None = None
    remarks: str | None = None


class IndentIn(BaseModel):
    store_code: str | None = None
    department: str | None = None
    ward: str | None = None
    priority: str = "routine"
    required_by: str | None = None
    remarks: str | None = None
    lines: list[IndentLineIn] = Field(default_factory=list)
    submit: bool = False


class IndentDecisionIn(BaseModel):
    #: line_no -> approved_quantity_milli. A line not named is approved in full.
    approvals: dict[int, int] = Field(default_factory=dict)
    reject: bool = False
    reason: str | None = None


class POLineIn(BaseModel):
    item_code: str
    quantity_milli: int
    unit_rate_paise: int
    description: str | None = None
    uom: str | None = None
    discount_paise: int = 0
    discount_bp: int = 0
    gst_treatment: str | None = None
    gst_rate_bp: int | None = None
    indent_line_id: int | None = None
    remarks: str | None = None


class PurchaseOrderIn(BaseModel):
    vendor_code: str
    store_code: str | None = None
    indent_id: int | None = None
    order_date: str | None = None
    expected_date: str | None = None
    place_of_supply: str | None = None
    #: EXPLICIT, never inferred — Orb has no configured home state to compare a
    #: vendor's state code against, and guessing puts the wrong GST heads on a
    #: real document.
    is_interstate: bool = False
    other_charges_paise: int = 0
    terms: str | None = None
    remarks: str | None = None
    lines: list[POLineIn] = Field(default_factory=list)


class ReasonIn(BaseModel):
    reason: str | None = None


class GRNLineIn(BaseModel):
    po_line_no: int
    accepted_quantity_milli: int = 0
    rejected_quantity_milli: int = 0
    batch_no: str | None = None
    expiry_date: str | None = None
    rejection_reason: str | None = None
    unit_rate_paise: int | None = None
    uom: str | None = None
    description: str | None = None
    remarks: str | None = None


class GoodsReceiptIn(BaseModel):
    purchase_order_id: int
    store_code: str | None = None
    vendor_invoice_number: str | None = None
    vendor_invoice_date: str | None = None
    vendor_invoice_total_paise: int | None = None
    qc_status: str = "passed"
    remarks: str | None = None
    #: Retry-safe. A repeated POST with the same key returns the receipt that
    #: already exists instead of posting the same delivery into stock twice.
    idempotency_key: str | None = None
    post: bool = True
    lines: list[GRNLineIn] = Field(default_factory=list)


class StockMovementIn(BaseModel):
    item_code: str
    store_code: str
    movement_type: str
    quantity_milli: int
    batch_no: str | None = None
    expiry_date: str | None = None
    unit_rate_paise: int | None = None
    department: str | None = None
    #: A COSTING tag only. It records that a consumable left the store for this
    #: patient so it can be charged; it writes nothing clinical.
    patient_id: str | None = None
    remarks: str | None = None
    idempotency_key: str | None = None


# ═════════════════════════════════════════════════════════════════════════════
#  Overview
# ═════════════════════════════════════════════════════════════════════════════


_OVERVIEW_STORE_CODE_DEFAULT = Query(None)
_OVERVIEW_MODULE_DEFAULT = Depends(require_module)
_OVERVIEW_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/overview", response_model=None)
def overview(
    store_code: str | None = _OVERVIEW_STORE_CODE_DEFAULT,
    _module: None = _OVERVIEW_MODULE_DEFAULT,
    user: dict = _OVERVIEW_USER_DEFAULT,
) -> dict:
    """The stores dashboard: low stock, expiring batches, open orders. For a
    facility-limited reader, the caller's facilities' stores and documents."""
    facilities = facility_scope(user)
    _require_store(store_code, facilities)
    out = svc.procurement_overview(store_code=store_code)
    if facilities is None:
        return out
    codes = _store_codes(facilities)
    low = _keep_store_rows(out["low_stock"], codes)
    expiring = _keep_store_rows(out["expiring"], codes)
    open_pos = _keep_docs(out["open_purchase_orders"], facilities, _SCOPED_FETCH)
    pending = [
        i for i in svc.list_indents(status="submitted", limit=_SCOPED_FETCH) if _at(i, facilities)
    ]
    drafts = [
        p
        for p in svc.list_purchase_orders(limit=_SCOPED_FETCH)
        if _at(p, facilities) and p.get("status") in ("draft", "pending_approval")
    ]
    outstanding = sum(int(p.get("total_paise") or 0) for p in open_pos)
    out.update(
        {
            "low_stock": low,
            "low_stock_count": len(low),
            "expiring": expiring,
            "expiring_count": len(expiring),
            "expired_count": sum(1 for e in expiring if e.get("is_expired")),
            "open_purchase_orders": open_pos,
            "open_purchase_order_count": len(open_pos),
            "open_purchase_order_value_paise": outstanding,
            "open_purchase_order_value_display": format_inr_or_unset(outstanding),
            "pending_indent_count": len(pending),
            "draft_purchase_order_count": len(drafts),
        }
    )
    return out


_META_MODULE_DEFAULT = Depends(require_module)
_META_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/meta")
def meta(
    _module=_META_MODULE_DEFAULT,
    user=_META_USER_DEFAULT,
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


_LIST_VENDORS_ACTIVE_ONLY_DEFAULT = Query(True)
_LIST_VENDORS_INCLUDE_BLACKLISTED_DEFAULT = Query(False)
_LIST_VENDORS_SEARCH_DEFAULT = Query(None)
_LIST_VENDORS_MODULE_DEFAULT = Depends(require_module)
_LIST_VENDORS_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/vendors")
def list_vendors(
    active_only: bool = _LIST_VENDORS_ACTIVE_ONLY_DEFAULT,
    include_blacklisted: bool = _LIST_VENDORS_INCLUDE_BLACKLISTED_DEFAULT,
    search: str | None = _LIST_VENDORS_SEARCH_DEFAULT,
    _module=_LIST_VENDORS_MODULE_DEFAULT,
    user=_LIST_VENDORS_USER_DEFAULT,
):
    return {
        "vendors": svc.list_vendors(
            active_only=active_only, include_blacklisted=include_blacklisted, search=search
        )
    }


_UPSERT_VENDOR_MODULE_DEFAULT = Depends(require_module)
_UPSERT_VENDOR_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/vendors")
def upsert_vendor(
    payload: VendorIn,
    request: Request,
    _module=_UPSERT_VENDOR_MODULE_DEFAULT,
    user=_UPSERT_VENDOR_USER_DEFAULT,
):
    try:
        vendor = svc.upsert_vendor(**payload.model_dump(), created_by=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_vendor_upserted",
        summary=f"Vendor {vendor['code']} ({vendor['name']}) created/updated",
        target_type="vendor",
        target_id=vendor["code"],
    )
    return vendor


_BLACKLIST_VENDOR_MODULE_DEFAULT = Depends(require_module)
_BLACKLIST_VENDOR_USER_DEFAULT = Depends(_PROC_APPROVE)


@router.post("/vendors/{code}/blacklist")
def blacklist_vendor(
    code: str,
    payload: BlacklistIn,
    request: Request,
    _module=_BLACKLIST_VENDOR_MODULE_DEFAULT,
    # Blacklisting is a commercial sanction with contractual consequences.
    user=_BLACKLIST_VENDOR_USER_DEFAULT,
):
    try:
        vendor = svc.set_vendor_blacklist(
            code.upper(),
            blacklisted=payload.blacklisted,
            reason=payload.reason,
            actor=user.get("id"),
        )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_vendor_blacklist_changed",
        summary=f"Vendor {code.upper()} blacklist set to {payload.blacklisted}",
        target_type="vendor",
        target_id=code.upper(),
        metadata={"blacklisted": payload.blacklisted, "reason": payload.reason},
    )
    return vendor


# ═════════════════════════════════════════════════════════════════════════════
#  Stores & items
# ═════════════════════════════════════════════════════════════════════════════


_LIST_STORES_ACTIVE_ONLY_DEFAULT = Query(True)
_LIST_STORES_MODULE_DEFAULT = Depends(require_module)
_LIST_STORES_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/stores", response_model=None)
def list_stores(
    active_only: bool = _LIST_STORES_ACTIVE_ONLY_DEFAULT,
    _module: None = _LIST_STORES_MODULE_DEFAULT,
    user: dict = _LIST_STORES_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    stores = svc.list_stores(active_only=active_only)
    return {"stores": [s for s in stores if _at(s, facilities)]}


_UPSERT_STORE_MODULE_DEFAULT = Depends(require_module)
_UPSERT_STORE_USER_DEFAULT = Depends(_PROC_APPROVE)


@router.post("/stores", response_model=None)
def upsert_store(
    payload: StoreIn,
    request: Request,
    _module: None = _UPSERT_STORE_MODULE_DEFAULT,
    # The store master is the control structure of hospital stock — where it
    # lives and who is in charge of it. Admin-only before the split; it stays on
    # the approval tier so a write grant (raising the day's indents) cannot
    # reshape the structure those indents move stock through.
    user: dict = _UPSERT_STORE_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    if facilities is not None:
        existing = svc.get_store(payload.code.upper())
        if existing is not None and not _at(existing, facilities):
            raise _not_found(f"store {payload.code.upper()}")
        _refuse_elsewhere(
            facilities,
            department=payload.department,
            ward=payload.ward,
            account=payload.incharge_user_id,
        )
    try:
        store = svc.upsert_store(**payload.model_dump())
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_store_upserted",
        summary=f"Store {store['code']} ({store['name']}) created/updated",
        target_type="store",
        target_id=store["code"],
    )
    return store


_LIST_ITEMS_CATEGORY_DEFAULT = Query(None)
_LIST_ITEMS_ACTIVE_ONLY_DEFAULT = Query(True)
_LIST_ITEMS_SEARCH_DEFAULT = Query(None)
_LIST_ITEMS_MODULE_DEFAULT = Depends(require_module)
_LIST_ITEMS_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/items")
def list_items(
    category: str | None = _LIST_ITEMS_CATEGORY_DEFAULT,
    active_only: bool = _LIST_ITEMS_ACTIVE_ONLY_DEFAULT,
    search: str | None = _LIST_ITEMS_SEARCH_DEFAULT,
    _module=_LIST_ITEMS_MODULE_DEFAULT,
    user=_LIST_ITEMS_USER_DEFAULT,
):
    return {
        "items": svc.list_inventory_items(category=category, active_only=active_only, search=search)
    }


_GET_ITEM_MODULE_DEFAULT = Depends(require_module)
_GET_ITEM_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/items/{code}")
def get_item(
    code: str,
    _module=_GET_ITEM_MODULE_DEFAULT,
    user=_GET_ITEM_USER_DEFAULT,
):
    item = svc.get_inventory_item(code.upper())
    if not item:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_item",
                "message": f"No non-pharmacy item with code {code.upper()!r}. If it is "
                "a medication, it is held in Helix (Pharmacy).",
            },
        )
    return item


_UPSERT_ITEM_MODULE_DEFAULT = Depends(require_module)
_UPSERT_ITEM_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/items")
def upsert_item(
    payload: ItemIn,
    request: Request,
    _module=_UPSERT_ITEM_MODULE_DEFAULT,
    user=_UPSERT_ITEM_USER_DEFAULT,
):
    try:
        item = svc.upsert_inventory_item(**payload.model_dump())
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_item_upserted",
        summary=f"Inventory item {item['code']} ({item['name']}) created/updated",
        target_type="inventory_item",
        target_id=item["code"],
    )
    return item


# ═════════════════════════════════════════════════════════════════════════════
#  Indents
# ═════════════════════════════════════════════════════════════════════════════


_LIST_INDENTS_STATUS_DEFAULT = Query(None)
_LIST_INDENTS_STORE_CODE_DEFAULT = Query(None)
_LIST_INDENTS_DEPARTMENT_DEFAULT = Query(None)
_LIST_INDENTS_LIMIT_DEFAULT = Query(100, ge=1, le=500)
_LIST_INDENTS_MODULE_DEFAULT = Depends(require_module)
_LIST_INDENTS_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/indents", response_model=None)
def list_indents(
    status: str | None = _LIST_INDENTS_STATUS_DEFAULT,
    store_code: str | None = _LIST_INDENTS_STORE_CODE_DEFAULT,
    department: str | None = _LIST_INDENTS_DEPARTMENT_DEFAULT,
    limit: int = _LIST_INDENTS_LIMIT_DEFAULT,
    _module: None = _LIST_INDENTS_MODULE_DEFAULT,
    user: dict = _LIST_INDENTS_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_store(store_code, facilities)
    rows = svc.list_indents(
        status=status,
        store_code=store_code,
        department=department,
        limit=limit if facilities is None else _SCOPED_FETCH,
    )
    return {"indents": _keep_docs(rows, facilities, limit)}


_GET_INDENT_MODULE_DEFAULT = Depends(require_module)
_GET_INDENT_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/indents/{indent_id}", response_model=None)
def get_indent(
    indent_id: int,
    _module: None = _GET_INDENT_MODULE_DEFAULT,
    user: dict = _GET_INDENT_USER_DEFAULT,
) -> dict:
    doc = svc.get_indent(indent_id)
    if not doc or not _at(doc, facility_scope(user)):
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": f"No indent {indent_id}"}
        )
    return doc


_CREATE_INDENT_MODULE_DEFAULT = Depends(require_module)
_CREATE_INDENT_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/indents", response_model=None)
def create_indent(
    payload: IndentIn,
    request: Request,
    _module: None = _CREATE_INDENT_MODULE_DEFAULT,
    user: dict = _CREATE_INDENT_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_store(payload.store_code, facilities)
    _refuse_elsewhere(facilities, department=payload.department, ward=payload.ward)
    try:
        doc = svc.create_indent(
            store_code=payload.store_code,
            department=payload.department,
            ward=payload.ward,
            priority=payload.priority,
            required_by=payload.required_by,
            lines=[line.model_dump() for line in payload.lines],
            requested_by=user.get("id"),
            remarks=payload.remarks,
            submit=payload.submit,
        )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_indent_created",
        summary=f"Indent {doc.get('indent_number') or doc['id']} raised "
        f"({len(doc.get('lines', []))} lines, status {doc['status']})",
        target_type="indent",
        target_id=doc["id"],
        metadata={"status": doc["status"], "priority": doc["priority"]},
    )
    return doc


_SUBMIT_INDENT_MODULE_DEFAULT = Depends(require_module)
_SUBMIT_INDENT_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/indents/{indent_id}/submit", response_model=None)
def submit_indent(
    indent_id: int,
    request: Request,
    _module: None = _SUBMIT_INDENT_MODULE_DEFAULT,
    user: dict = _SUBMIT_INDENT_USER_DEFAULT,
) -> dict:
    _require_doc(lambda: svc.get_indent(indent_id), facility_scope(user), f"indent {indent_id}")
    try:
        doc = svc.submit_indent(indent_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_indent_submitted",
        summary=f"Indent {doc.get('indent_number') or indent_id} submitted",
        target_type="indent",
        target_id=indent_id,
    )
    return doc


_DECIDE_INDENT_MODULE_DEFAULT = Depends(require_module)
_DECIDE_INDENT_USER_DEFAULT = Depends(_PROC_APPROVE)


@router.post("/indents/{indent_id}/decide", response_model=None)
def decide_indent(
    indent_id: int,
    payload: IndentDecisionIn,
    request: Request,
    _module: None = _DECIDE_INDENT_MODULE_DEFAULT,
    # Approving an indent commits the hospital to spend. Deliberately a stricter
    # permission than raising one.
    user: dict = _DECIDE_INDENT_USER_DEFAULT,
) -> dict:
    _require_doc(lambda: svc.get_indent(indent_id), facility_scope(user), f"indent {indent_id}")
    try:
        doc = svc.decide_indent(
            indent_id,
            approvals=payload.approvals,
            reject=payload.reject,
            reason=payload.reason,
            actor=user.get("id"),
        )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_indent_decided",
        summary=f"Indent {doc.get('indent_number') or indent_id} → {doc['status']}",
        target_type="indent",
        target_id=indent_id,
        metadata={"status": doc["status"], "rejected": payload.reject},
    )
    return doc


_CANCEL_INDENT_MODULE_DEFAULT = Depends(require_module)
_CANCEL_INDENT_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/indents/{indent_id}/cancel", response_model=None)
def cancel_indent(
    indent_id: int,
    payload: ReasonIn,
    request: Request,
    _module: None = _CANCEL_INDENT_MODULE_DEFAULT,
    user: dict = _CANCEL_INDENT_USER_DEFAULT,
) -> dict:
    _require_doc(lambda: svc.get_indent(indent_id), facility_scope(user), f"indent {indent_id}")
    try:
        doc = svc.cancel_indent(indent_id, reason=payload.reason, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_indent_cancelled",
        summary=f"Indent {doc.get('indent_number') or indent_id} cancelled",
        target_type="indent",
        target_id=indent_id,
        metadata={"reason": payload.reason},
    )
    return doc


# ═════════════════════════════════════════════════════════════════════════════
#  Purchase orders
# ═════════════════════════════════════════════════════════════════════════════


_LIST_PURCHASE_ORDERS_STATUS_DEFAULT = Query(None)
_LIST_PURCHASE_ORDERS_VENDOR_CODE_DEFAULT = Query(None)
_LIST_PURCHASE_ORDERS_OPEN_ONLY_DEFAULT = Query(False)
_LIST_PURCHASE_ORDERS_LIMIT_DEFAULT = Query(100, ge=1, le=500)
_LIST_PURCHASE_ORDERS_MODULE_DEFAULT = Depends(require_module)
_LIST_PURCHASE_ORDERS_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/purchase-orders", response_model=None)
def list_purchase_orders(
    status: str | None = _LIST_PURCHASE_ORDERS_STATUS_DEFAULT,
    vendor_code: str | None = _LIST_PURCHASE_ORDERS_VENDOR_CODE_DEFAULT,
    open_only: bool = _LIST_PURCHASE_ORDERS_OPEN_ONLY_DEFAULT,
    limit: int = _LIST_PURCHASE_ORDERS_LIMIT_DEFAULT,
    _module: None = _LIST_PURCHASE_ORDERS_MODULE_DEFAULT,
    user: dict = _LIST_PURCHASE_ORDERS_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    rows = svc.list_purchase_orders(
        status=status,
        vendor_code=vendor_code,
        open_only=open_only,
        limit=limit if facilities is None else _SCOPED_FETCH,
    )
    return {"purchase_orders": _keep_docs(rows, facilities, limit)}


_GET_PURCHASE_ORDER_MODULE_DEFAULT = Depends(require_module)
_GET_PURCHASE_ORDER_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/purchase-orders/{po_id}", response_model=None)
def get_purchase_order(
    po_id: int,
    _module: None = _GET_PURCHASE_ORDER_MODULE_DEFAULT,
    user: dict = _GET_PURCHASE_ORDER_USER_DEFAULT,
) -> dict:
    doc = svc.get_purchase_order(po_id)
    if not doc or not _at(doc, facility_scope(user)):
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": f"No purchase order {po_id}"}
        )
    return doc


_CREATE_PURCHASE_ORDER_MODULE_DEFAULT = Depends(require_module)
_CREATE_PURCHASE_ORDER_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/purchase-orders", response_model=None)
def create_purchase_order(
    payload: PurchaseOrderIn,
    request: Request,
    _module: None = _CREATE_PURCHASE_ORDER_MODULE_DEFAULT,
    user: dict = _CREATE_PURCHASE_ORDER_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_store(payload.store_code, facilities)
    if payload.indent_id is not None:
        indent_id = payload.indent_id
        _require_doc(lambda: svc.get_indent(indent_id), facilities, f"indent {indent_id}")
    try:
        doc = svc.create_purchase_order(
            vendor_code=payload.vendor_code,
            lines=[line.model_dump() for line in payload.lines],
            store_code=payload.store_code,
            indent_id=payload.indent_id,
            order_date=payload.order_date,
            expected_date=payload.expected_date,
            place_of_supply=payload.place_of_supply,
            is_interstate=payload.is_interstate,
            other_charges_paise=payload.other_charges_paise,
            terms=payload.terms,
            remarks=payload.remarks,
            created_by=user.get("id"),
        )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_po_created",
        summary=f"Purchase order drafted for {doc.get('vendor_name')} "
        f"({len(doc.get('lines', []))} lines)",
        target_type="purchase_order",
        target_id=doc["id"],
        metadata={
            "total_paise": doc.get("total_paise"),
            "vendor_code": doc.get("vendor_code"),
            "is_interstate": doc.get("is_interstate"),
        },
    )
    return doc


_SUBMIT_PURCHASE_ORDER_MODULE_DEFAULT = Depends(require_module)
_SUBMIT_PURCHASE_ORDER_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/purchase-orders/{po_id}/submit", response_model=None)
def submit_purchase_order(
    po_id: int,
    request: Request,
    _module: None = _SUBMIT_PURCHASE_ORDER_MODULE_DEFAULT,
    user: dict = _SUBMIT_PURCHASE_ORDER_USER_DEFAULT,
) -> dict:
    _require_doc(
        lambda: svc.get_purchase_order(po_id), facility_scope(user), f"purchase order {po_id}"
    )
    try:
        doc = svc.submit_purchase_order(po_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_po_submitted",
        summary=f"Purchase order {po_id} submitted for approval",
        target_type="purchase_order",
        target_id=po_id,
    )
    return doc


_APPROVE_PURCHASE_ORDER_MODULE_DEFAULT = Depends(require_module)
_APPROVE_PURCHASE_ORDER_USER_DEFAULT = Depends(_PROC_APPROVE)


@router.post("/purchase-orders/{po_id}/approve", response_model=None)
def approve_purchase_order(
    po_id: int,
    request: Request,
    _module: None = _APPROVE_PURCHASE_ORDER_MODULE_DEFAULT,
    user: dict = _APPROVE_PURCHASE_ORDER_USER_DEFAULT,
) -> dict:
    _require_doc(
        lambda: svc.get_purchase_order(po_id), facility_scope(user), f"purchase order {po_id}"
    )
    try:
        doc = svc.approve_purchase_order(po_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_po_approved",
        summary=f"Purchase order {po_id} approved "
        f"({doc.get('total_paise')} paise to {doc.get('vendor_name')})",
        target_type="purchase_order",
        target_id=po_id,
        metadata={"total_paise": doc.get("total_paise")},
    )
    return doc


_ISSUE_PURCHASE_ORDER_MODULE_DEFAULT = Depends(require_module)
_ISSUE_PURCHASE_ORDER_USER_DEFAULT = Depends(_PROC_APPROVE)


@router.post("/purchase-orders/{po_id}/issue", response_model=None)
def issue_purchase_order(
    po_id: int,
    request: Request,
    _module: None = _ISSUE_PURCHASE_ORDER_MODULE_DEFAULT,
    user: dict = _ISSUE_PURCHASE_ORDER_USER_DEFAULT,
) -> dict:
    _require_doc(
        lambda: svc.get_purchase_order(po_id), facility_scope(user), f"purchase order {po_id}"
    )
    try:
        doc = svc.issue_purchase_order(po_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_po_issued",
        summary=f"Purchase order {doc.get('po_number') or po_id} issued to "
        f"{doc.get('vendor_name')}",
        target_type="purchase_order",
        target_id=po_id,
        metadata={"po_number": doc.get("po_number")},
    )
    return doc


_CANCEL_PURCHASE_ORDER_MODULE_DEFAULT = Depends(require_module)
_CANCEL_PURCHASE_ORDER_USER_DEFAULT = Depends(_PROC_APPROVE)


@router.post("/purchase-orders/{po_id}/cancel", response_model=None)
def cancel_purchase_order(
    po_id: int,
    payload: ReasonIn,
    request: Request,
    _module: None = _CANCEL_PURCHASE_ORDER_MODULE_DEFAULT,
    # Withdrawing a commitment made to a vendor is the same kind of act as
    # making one, so it sits on the same tier as approve/issue.
    user: dict = _CANCEL_PURCHASE_ORDER_USER_DEFAULT,
) -> dict:
    _require_doc(
        lambda: svc.get_purchase_order(po_id), facility_scope(user), f"purchase order {po_id}"
    )
    try:
        doc = svc.cancel_purchase_order(po_id, reason=payload.reason or "", actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_po_cancelled",
        summary=f"Purchase order {doc.get('po_number') or po_id} cancelled",
        target_type="purchase_order",
        target_id=po_id,
        metadata={"reason": payload.reason},
    )
    return doc


# ═════════════════════════════════════════════════════════════════════════════
#  Goods receipt
# ═════════════════════════════════════════════════════════════════════════════


_LIST_GOODS_RECEIPTS_PURCHASE_ORDER_ID_DEFAULT = Query(None)
_LIST_GOODS_RECEIPTS_STATUS_DEFAULT = Query(None)
_LIST_GOODS_RECEIPTS_LIMIT_DEFAULT = Query(100, ge=1, le=500)
_LIST_GOODS_RECEIPTS_MODULE_DEFAULT = Depends(require_module)
_LIST_GOODS_RECEIPTS_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/goods-receipts", response_model=None)
def list_goods_receipts(
    purchase_order_id: int | None = _LIST_GOODS_RECEIPTS_PURCHASE_ORDER_ID_DEFAULT,
    status: str | None = _LIST_GOODS_RECEIPTS_STATUS_DEFAULT,
    limit: int = _LIST_GOODS_RECEIPTS_LIMIT_DEFAULT,
    _module: None = _LIST_GOODS_RECEIPTS_MODULE_DEFAULT,
    user: dict = _LIST_GOODS_RECEIPTS_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    rows = svc.list_goods_receipts(
        purchase_order_id=purchase_order_id,
        status=status,
        limit=limit if facilities is None else _SCOPED_FETCH,
    )
    return {"goods_receipts": _keep_docs(rows, facilities, limit)}


_GET_GOODS_RECEIPT_MODULE_DEFAULT = Depends(require_module)
_GET_GOODS_RECEIPT_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/goods-receipts/{grn_id}", response_model=None)
def get_goods_receipt(
    grn_id: int,
    _module: None = _GET_GOODS_RECEIPT_MODULE_DEFAULT,
    user: dict = _GET_GOODS_RECEIPT_USER_DEFAULT,
) -> dict:
    doc = svc.get_goods_receipt(grn_id)
    if not doc or not _at(doc, facility_scope(user)):
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": f"No goods receipt {grn_id}"}
        )
    return doc


_CREATE_GOODS_RECEIPT_MODULE_DEFAULT = Depends(require_module)
_CREATE_GOODS_RECEIPT_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/goods-receipts", response_model=None)
def create_goods_receipt(
    payload: GoodsReceiptIn,
    request: Request,
    _module: None = _CREATE_GOODS_RECEIPT_MODULE_DEFAULT,
    user: dict = _CREATE_GOODS_RECEIPT_USER_DEFAULT,
) -> dict:
    """Receive a delivery. Partial receipt is the normal case; over-receipt is
    refused with 409."""
    facilities = facility_scope(user)
    po_id = payload.purchase_order_id
    _require_doc(lambda: svc.get_purchase_order(po_id), facilities, f"purchase order {po_id}")
    _require_store(payload.store_code, facilities)
    try:
        doc = svc.create_goods_receipt(
            purchase_order_id=payload.purchase_order_id,
            lines=[line.model_dump() for line in payload.lines],
            store_code=payload.store_code,
            vendor_invoice_number=payload.vendor_invoice_number,
            vendor_invoice_date=payload.vendor_invoice_date,
            vendor_invoice_total_paise=payload.vendor_invoice_total_paise,
            qc_status=payload.qc_status,
            received_by=user.get("id"),
            remarks=payload.remarks,
            idempotency_key=payload.idempotency_key,
            post=payload.post,
        )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    if not doc.get("idempotent_replay"):
        _audit(
            request,
            user,
            "procurement_grn_created",
            summary=f"Goods receipt {doc.get('grn_number') or doc['id']} "
            f"({doc['status']}) against PO {payload.purchase_order_id}",
            target_type="goods_receipt",
            target_id=doc["id"],
            metadata={
                "status": doc["status"],
                "purchase_order_id": payload.purchase_order_id,
                "line_count": len(doc.get("lines", [])),
            },
        )
    return doc


_POST_GOODS_RECEIPT_MODULE_DEFAULT = Depends(require_module)
_POST_GOODS_RECEIPT_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/goods-receipts/{grn_id}/post", response_model=None)
def post_goods_receipt(
    grn_id: int,
    request: Request,
    _module: None = _POST_GOODS_RECEIPT_MODULE_DEFAULT,
    user: dict = _POST_GOODS_RECEIPT_USER_DEFAULT,
) -> dict:
    _require_doc(
        lambda: svc.get_goods_receipt(grn_id), facility_scope(user), f"goods receipt {grn_id}"
    )
    try:
        doc = svc.post_goods_receipt(grn_id, actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_grn_posted",
        summary=f"Goods receipt {doc.get('grn_number') or grn_id} posted into "
        f"store {doc.get('store_code')}",
        target_type="goods_receipt",
        target_id=grn_id,
    )
    return doc


_CANCEL_GOODS_RECEIPT_MODULE_DEFAULT = Depends(require_module)
_CANCEL_GOODS_RECEIPT_USER_DEFAULT = Depends(_PROC_APPROVE)


@router.post("/goods-receipts/{grn_id}/cancel", response_model=None)
def cancel_goods_receipt(
    grn_id: int,
    payload: ReasonIn,
    request: Request,
    _module: None = _CANCEL_GOODS_RECEIPT_MODULE_DEFAULT,
    # Only a DRAFT is cancellable (the state machine refuses a posted one), and
    # a draft has moved no stock — but erasing a recorded delivery document was
    # deliberately kept above the clerk who recorded it before the split, and
    # stays there: the hands that record a receipt cannot also disappear it.
    user: dict = _CANCEL_GOODS_RECEIPT_USER_DEFAULT,
) -> dict:
    _require_doc(
        lambda: svc.get_goods_receipt(grn_id), facility_scope(user), f"goods receipt {grn_id}"
    )
    try:
        doc = svc.cancel_goods_receipt(grn_id, reason=payload.reason or "", actor=user.get("id"))
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_grn_cancelled",
        summary=f"Draft goods receipt {grn_id} cancelled",
        target_type="goods_receipt",
        target_id=grn_id,
        metadata={"reason": payload.reason},
    )
    return doc


# ═════════════════════════════════════════════════════════════════════════════
#  Stock
# ═════════════════════════════════════════════════════════════════════════════


_STOCK_STORE_CODE_DEFAULT = Query(None)
_STOCK_CATEGORY_DEFAULT = Query(None)
_STOCK_LOW_ONLY_DEFAULT = Query(False)
_STOCK_INCLUDE_ZERO_DEFAULT = Query(True)
_STOCK_LIMIT_DEFAULT = Query(500, ge=1, le=1000)
_STOCK_MODULE_DEFAULT = Depends(require_module)
_STOCK_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/stock", response_model=None)
def stock(
    store_code: str | None = _STOCK_STORE_CODE_DEFAULT,
    category: str | None = _STOCK_CATEGORY_DEFAULT,
    low_only: bool = _STOCK_LOW_ONLY_DEFAULT,
    include_zero: bool = _STOCK_INCLUDE_ZERO_DEFAULT,
    limit: int = _STOCK_LIMIT_DEFAULT,
    _module: None = _STOCK_MODULE_DEFAULT,
    user: dict = _STOCK_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_store(store_code, facilities)
    rows = svc.list_stock_on_hand(
        store_code=store_code,
        category=category,
        low_only=low_only,
        include_zero=include_zero,
        limit=limit,
    )
    if facilities is not None and not store_code:
        rows = _keep_store_rows(rows, _store_codes(facilities))
    return {"stock": rows}


_STOCK_LEDGER_STORE_CODE_DEFAULT = Query(None)
_STOCK_LEDGER_LIMIT_DEFAULT = Query(200, ge=1, le=1000)
_STOCK_LEDGER_MODULE_DEFAULT = Depends(require_module)
_STOCK_LEDGER_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/stock/{item_code}/ledger", response_model=None)
def stock_ledger(
    item_code: str,
    store_code: str | None = _STOCK_LEDGER_STORE_CODE_DEFAULT,
    limit: int = _STOCK_LEDGER_LIMIT_DEFAULT,
    _module: None = _STOCK_LEDGER_MODULE_DEFAULT,
    user: dict = _STOCK_LEDGER_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_store(store_code, facilities)
    if facilities is None or store_code:
        return {
            "item_code": item_code.upper(),
            "on_hand": svc.stock_on_hand(item_code, store_code),
            "movements": svc.stock_ledger(item_code, store_code=store_code, limit=limit),
        }
    # Every store of the caller's facilities, and only those: the balance is
    # the sum of their own balances, the ledger their own movements.
    codes = _store_codes(facilities)
    on_hand = svc.stock_on_hand(item_code, None)
    held = sum(svc.stock_on_hand_milli(item_code, code) for code in sorted(codes))
    level = on_hand.get("reorder_level_milli")
    on_hand.update(
        {
            "on_hand_milli": held,
            "on_hand_display": svc.format_quantity(held, on_hand.get("uom")),
            "is_low": level is not None and held <= level,
        }
    )
    every = svc.stock_ledger(item_code, store_code=None, limit=1000)
    movements = _keep_store_rows(every, codes)[:limit]
    return {"item_code": item_code.upper(), "on_hand": on_hand, "movements": movements}


_RECORD_MOVEMENT_MODULE_DEFAULT = Depends(require_module)
_RECORD_MOVEMENT_USER_DEFAULT = Depends(_PROC_WRITE)


@router.post("/stock/movements", response_model=None)
def record_movement(
    payload: StockMovementIn,
    request: Request,
    _module: None = _RECORD_MOVEMENT_MODULE_DEFAULT,
    user: dict = _RECORD_MOVEMENT_USER_DEFAULT,
) -> dict:
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
    _require_store(payload.store_code, facility_scope(user))

    if payload.movement_type in ("write_off", "adjustment", "expiry"):
        role = user.get("role", "")
        if PROCUREMENT_APPROVE not in ROLE_PERMISSIONS.get(role, set()):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "message": (
                        f"A {payload.movement_type} removes stock without a receipt "
                        "and needs procurement approval authority. Ask for a stock "
                        "adjustment to be approved rather than issuing it out."
                    ),
                    "required": [PROCUREMENT_APPROVE],
                },
            )
    try:
        if payload.movement_type == "issue":
            row = svc.issue_stock(
                item_code=payload.item_code,
                store_code=payload.store_code,
                quantity_milli=payload.quantity_milli,
                department=payload.department,
                patient_id=payload.patient_id,
                batch_no=payload.batch_no,
                moved_by=user.get("id"),
                idempotency_key=payload.idempotency_key,
                remarks=payload.remarks,
            )
        else:
            row = svc.record_stock_movement(
                item_code=payload.item_code,
                store_code=payload.store_code,
                movement_type=payload.movement_type,
                quantity_milli=payload.quantity_milli,
                batch_no=payload.batch_no,
                expiry_date=payload.expiry_date,
                unit_rate_paise=payload.unit_rate_paise,
                reference_type="manual",
                patient_id=payload.patient_id,
                moved_by=user.get("id"),
                idempotency_key=payload.idempotency_key,
                remarks=payload.remarks,
                allow_negative=payload.movement_type == "adjustment",
            )
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "procurement_stock_movement",
        summary=f"{payload.movement_type} of {payload.item_code} in {payload.store_code}",
        target_type="inventory_item",
        target_id=payload.item_code.upper(),
        metadata={
            "movement_type": payload.movement_type,
            "quantity_milli": payload.quantity_milli,
            "store_code": payload.store_code.upper(),
            "balance_after_milli": row.get("balance_after_milli"),
        },
    )
    return row


# ═════════════════════════════════════════════════════════════════════════════
#  Reports
# ═════════════════════════════════════════════════════════════════════════════


_LOW_STOCK_STORE_CODE_DEFAULT = Query(None)
_LOW_STOCK_LIMIT_DEFAULT = Query(200, ge=1, le=1000)
_LOW_STOCK_MODULE_DEFAULT = Depends(require_module)
_LOW_STOCK_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/reports/low-stock", response_model=None)
def low_stock(
    store_code: str | None = _LOW_STOCK_STORE_CODE_DEFAULT,
    limit: int = _LOW_STOCK_LIMIT_DEFAULT,
    _module: None = _LOW_STOCK_MODULE_DEFAULT,
    user: dict = _LOW_STOCK_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_store(store_code, facilities)
    rows = svc.low_stock(store_code=store_code, limit=limit)
    if facilities is not None and not store_code:
        rows = _keep_store_rows(rows, _store_codes(facilities))
    return {"low_stock": rows, "count": len(rows)}


_CONSUMPTION_DATE_FROM_DEFAULT = Query(None)
_CONSUMPTION_DATE_TO_DEFAULT = Query(None)
_CONSUMPTION_DEPARTMENT_DEFAULT = Query(None)
_CONSUMPTION_STORE_CODE_DEFAULT = Query(None)
_CONSUMPTION_LIMIT_DEFAULT = Query(500, ge=1, le=2000)
_CONSUMPTION_MODULE_DEFAULT = Depends(require_module)
_CONSUMPTION_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/reports/consumption", response_model=None)
def consumption(
    date_from: str | None = _CONSUMPTION_DATE_FROM_DEFAULT,
    date_to: str | None = _CONSUMPTION_DATE_TO_DEFAULT,
    department: str | None = _CONSUMPTION_DEPARTMENT_DEFAULT,
    store_code: str | None = _CONSUMPTION_STORE_CODE_DEFAULT,
    limit: int = _CONSUMPTION_LIMIT_DEFAULT,
    _module: None = _CONSUMPTION_MODULE_DEFAULT,
    user: dict = _CONSUMPTION_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_store(store_code, facilities)
    try:
        if facilities is None or store_code:
            return svc.consumption_report(
                date_from=date_from,
                date_to=date_to,
                department=department,
                store_code=store_code,
                limit=limit,
            )
        # The caller's facilities' stores, each counted by the service and
        # added up here, so another facility's issues never enter a total.
        reports = [
            svc.consumption_report(
                date_from=date_from,
                date_to=date_to,
                department=department,
                store_code=code,
                limit=limit,
            )
            for code in sorted(_store_codes(facilities))
        ]
    except svc.ProcurementError as exc:
        raise _fail(exc) from exc
    return _merged_consumption(reports, date_from=date_from, date_to=date_to, limit=limit)


def _merged_consumption(
    reports: list[dict], *, date_from: str | None, date_to: str | None, limit: int
) -> dict:
    """Several stores' consumption reports as one, the way the service adds a
    single report up: lines by department and value, departments summed."""
    lines = sorted(
        (line for r in reports for line in r["lines"]),
        key=lambda line: (line["department"], -int(line["value_paise"])),
    )[:limit]
    departments: dict[str, dict] = {}
    for line in lines:
        dept = departments.setdefault(
            line["department"],
            {
                "department": line["department"],
                "value_paise": 0,
                "line_count": 0,
                "unvalued_movement_count": 0,
            },
        )
        dept["value_paise"] += int(line["value_paise"])
        dept["line_count"] += 1
        dept["unvalued_movement_count"] += int(line["unvalued_movement_count"])
    for dept in departments.values():
        dept["value_display"] = format_inr_or_unset(
            dept["value_paise"] if dept["unvalued_movement_count"] == 0 else None,
            unset="Partly unpriced",
        )
    total_value = sum(int(line["value_paise"]) for line in lines)
    total_unvalued = sum(int(line["unvalued_movement_count"]) for line in lines)
    first = reports[0] if reports else {}
    return {
        "date_from": first.get("date_from", date_from),
        "date_to": first.get("date_to", date_to),
        "lines": lines,
        "departments": sorted(departments.values(), key=lambda d: -d["value_paise"]),
        "total_value_paise": total_value,
        "total_value_display": format_inr_or_unset(
            total_value if total_unvalued == 0 else None, unset="Partly unpriced"
        ),
        "unvalued_movement_count": total_unvalued,
        "value_is_complete": total_unvalued == 0,
    }


_EXPIRY_WITHIN_DAYS_DEFAULT = Query(90, ge=0, le=3650)
_EXPIRY_STORE_CODE_DEFAULT = Query(None)
_EXPIRY_INCLUDE_EXPIRED_DEFAULT = Query(True)
_EXPIRY_LIMIT_DEFAULT = Query(200, ge=1, le=1000)
_EXPIRY_MODULE_DEFAULT = Depends(require_module)
_EXPIRY_USER_DEFAULT = Depends(_PROC_READ)


@router.get("/reports/expiry", response_model=None)
def expiry(
    within_days: int = _EXPIRY_WITHIN_DAYS_DEFAULT,
    store_code: str | None = _EXPIRY_STORE_CODE_DEFAULT,
    include_expired: bool = _EXPIRY_INCLUDE_EXPIRED_DEFAULT,
    limit: int = _EXPIRY_LIMIT_DEFAULT,
    _module: None = _EXPIRY_MODULE_DEFAULT,
    user: dict = _EXPIRY_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_store(store_code, facilities)
    rows = svc.expiry_report(
        within_days=within_days, store_code=store_code, include_expired=include_expired, limit=limit
    )
    if facilities is not None and not store_code:
        rows = _keep_store_rows(rows, _store_codes(facilities))
    return {
        "expiring": rows,
        "count": len(rows),
        "expired_count": sum(1 for r in rows if r["is_expired"]),
        "within_days": within_days,
    }
