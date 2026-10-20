"""Pharmacy stock views: endpoints under /api/pharmacy-store (grn-9, W4-C).

The package router in routes/pharmacy_store/__init__.py adds the
/api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this router has
neither, so every endpoint added here inherits both.

Endpoints (docs/his_design/grn.json, section E), all pharmacy_store.read and
all read-only:
  stock            stock per store and drug: dispensable, usable, in transit,
                   quarantined, expired
  stock/overview   the numbers the Stock screen leads with
  stock/lots       lots, earliest expiry first, with status and value
  ledger           the drug ledger, newest first, paged by seq
  reorder          what needs ordering, against store par or the drug level
  near-expiry      lots expiring within ORB_PHARMACY_NEAR_EXPIRY_DAYS (GRN-R16),
                   valued at cost and at MRP
  reconcile        does every lot's quantity equal the sum of its ledger?

The reports live in services/pharmacy_stock_reports.py and use the stock
service's own predicates, so a screen never counts a lot the dispense would
refuse. No patient data: a ledger row tied to a patient's order shows neither
the order id nor its remarks. Every handler is a plain def.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from permissions import PHARMACY_STORE_READ, require_permission

from services import pharmacy_stock_reports as reports

router = APIRouter()

_PS_READ = require_permission(PHARMACY_STORE_READ)


_STOCK_BY_STORE_STORE_ID_DEFAULT = Query(None, ge=1)
_STOCK_BY_STORE_DRUG_ID_DEFAULT = Query(None, max_length=80)
_STOCK_BY_STORE_SEARCH_DEFAULT = Query(None, max_length=120)
_STOCK_BY_STORE_USER_DEFAULT = Depends(_PS_READ)


@router.get("/stock")
def stock_by_store(
    store_id: int | None = _STOCK_BY_STORE_STORE_ID_DEFAULT,
    drug_id: str | None = _STOCK_BY_STORE_DRUG_ID_DEFAULT,
    search: str | None = _STOCK_BY_STORE_SEARCH_DEFAULT,
    user: dict = _STOCK_BY_STORE_USER_DEFAULT,
):
    """Stock per store and drug. A lot not yet in a store reads 'Not yet in
    a store'."""
    return reports.stock_by_store(store_id=store_id, drug_id=drug_id or None, search=search or None)


_STOCK_OVERVIEW_USER_DEFAULT = Depends(_PS_READ)


@router.get("/stock/overview")
def stock_overview(user: dict = _STOCK_OVERVIEW_USER_DEFAULT):
    return reports.overview()


_STOCK_LOTS_DRUG_ID_DEFAULT = Query(None, max_length=80)
_STOCK_LOTS_STORE_ID_DEFAULT = Query(None, ge=1)
_STOCK_LOTS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/stock/lots")
def stock_lots(
    drug_id: str | None = _STOCK_LOTS_DRUG_ID_DEFAULT,
    store_id: int | None = _STOCK_LOTS_STORE_ID_DEFAULT,
    include_empty: bool = False,
    user: dict = _STOCK_LOTS_USER_DEFAULT,
):
    """Lots, earliest expiry first. 'Rate not configured' when a lot's cost
    was never recorded, never Rs 0."""
    return reports.list_lots(
        drug_id=drug_id or None, store_id=store_id, include_empty=include_empty
    )


_LEDGER_DRUG_ID_DEFAULT = Query(None, max_length=80)
_LEDGER_STORE_ID_DEFAULT = Query(None, ge=1)
_LEDGER_LOT_ID_DEFAULT = Query(None, max_length=80)
_LEDGER_BEFORE_SEQ_DEFAULT = Query(None, ge=1)
_LEDGER_LIMIT_DEFAULT = Query(50, ge=1, le=reports.MAX_LEDGER_PAGE)
_LEDGER_USER_DEFAULT = Depends(_PS_READ)


@router.get("/ledger")
def ledger(
    drug_id: str | None = _LEDGER_DRUG_ID_DEFAULT,
    store_id: int | None = _LEDGER_STORE_ID_DEFAULT,
    lot_id: str | None = _LEDGER_LOT_ID_DEFAULT,
    before_seq: int | None = _LEDGER_BEFORE_SEQ_DEFAULT,
    limit: int = _LEDGER_LIMIT_DEFAULT,
    user: dict = _LEDGER_USER_DEFAULT,
):
    """The drug ledger, newest first by seq. Pass next_before_seq back as
    before_seq to read the next older page; new movements never shift it."""
    return reports.ledger_page(
        drug_id=drug_id or None,
        store_id=store_id,
        lot_id=lot_id or None,
        before_seq=before_seq,
        limit=limit,
    )


_REORDER_STORE_ID_DEFAULT = Query(None, ge=1)
_REORDER_USER_DEFAULT = Depends(_PS_READ)


@router.get("/reorder")
def reorder(
    store_id: int | None = _REORDER_STORE_ID_DEFAULT,
    include_unset: bool = True,
    needs_only: bool = False,
    user: dict = _REORDER_USER_DEFAULT,
):
    """Against each store's par level, or the drug's own reorder level on
    the dispensing shelf. A level nobody set reads 'No reorder level set',
    never 0."""
    return reports.reorder_report(
        store_id=store_id, include_unset=include_unset, needs_only=needs_only
    )


_NEAR_EXPIRY_DAYS_DEFAULT = Query(None, ge=1, le=reports.MAX_NEAR_EXPIRY_DAYS)
_NEAR_EXPIRY_STORE_ID_DEFAULT = Query(None, ge=1)
_NEAR_EXPIRY_USER_DEFAULT = Depends(_PS_READ)


@router.get("/near-expiry")
def near_expiry(
    days: int | None = _NEAR_EXPIRY_DAYS_DEFAULT,
    store_id: int | None = _NEAR_EXPIRY_STORE_ID_DEFAULT,
    user: dict = _NEAR_EXPIRY_USER_DEFAULT,
):
    """Available lots holding units that expire within the window (the site
    setting unless days is given), soonest first, and those already past
    expiry still on a shelf. Empty lots are left out."""
    try:
        return reports.near_expiry(days=days, store_id=store_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "bad_value", "message": str(exc)}
        ) from exc


_RECONCILE_USER_DEFAULT = Depends(_PS_READ)


@router.get("/reconcile")
def reconcile(user: dict = _RECONCILE_USER_DEFAULT):
    """Read-only: clean is true only when every lot equals its ledger."""
    return reports.reconcile_report()
