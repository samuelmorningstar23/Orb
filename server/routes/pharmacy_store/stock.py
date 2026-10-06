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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from permissions import PHARMACY_STORE_READ, require_permission
from services import pharmacy_stock_reports as reports

router = APIRouter()

_PS_READ = require_permission(PHARMACY_STORE_READ)


@router.get("/stock")
def stock_by_store(store_id: Optional[int] = Query(None, ge=1),
                   drug_id: Optional[str] = Query(None, max_length=80),
                   search: Optional[str] = Query(None, max_length=120),
                   user: dict = Depends(_PS_READ)):
    """Stock per store and drug. A lot not yet in a store reads 'Not yet in
    a store'."""
    return reports.stock_by_store(store_id=store_id, drug_id=drug_id or None,
                                  search=search or None)


@router.get("/stock/overview")
def stock_overview(user: dict = Depends(_PS_READ)):
    return reports.overview()


@router.get("/stock/lots")
def stock_lots(drug_id: Optional[str] = Query(None, max_length=80),
               store_id: Optional[int] = Query(None, ge=1),
               include_empty: bool = False,
               user: dict = Depends(_PS_READ)):
    """Lots, earliest expiry first. 'Rate not configured' when a lot's cost
    was never recorded, never Rs 0."""
    return reports.list_lots(drug_id=drug_id or None, store_id=store_id,
                             include_empty=include_empty)


@router.get("/ledger")
def ledger(drug_id: Optional[str] = Query(None, max_length=80),
           store_id: Optional[int] = Query(None, ge=1),
           lot_id: Optional[str] = Query(None, max_length=80),
           before_seq: Optional[int] = Query(None, ge=1),
           limit: int = Query(50, ge=1, le=reports.MAX_LEDGER_PAGE),
           user: dict = Depends(_PS_READ)):
    """The drug ledger, newest first by seq. Pass next_before_seq back as
    before_seq to read the next older page; new movements never shift it."""
    return reports.ledger_page(drug_id=drug_id or None, store_id=store_id,
                               lot_id=lot_id or None, before_seq=before_seq, limit=limit)


@router.get("/reorder")
def reorder(store_id: Optional[int] = Query(None, ge=1),
            include_unset: bool = True,
            needs_only: bool = False,
            user: dict = Depends(_PS_READ)):
    """Against each store's par level, or the drug's own reorder level on
    the dispensing shelf. A level nobody set reads 'No reorder level set',
    never 0."""
    return reports.reorder_report(store_id=store_id, include_unset=include_unset,
                                  needs_only=needs_only)


@router.get("/near-expiry")
def near_expiry(days: Optional[int] = Query(None, ge=1, le=reports.MAX_NEAR_EXPIRY_DAYS),
                store_id: Optional[int] = Query(None, ge=1),
                user: dict = Depends(_PS_READ)):
    """Available lots holding units that expire within the window (the site
    setting unless days is given), soonest first, and those already past
    expiry still on a shelf. Empty lots are left out."""
    try:
        return reports.near_expiry(days=days, store_id=store_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "bad_value", "message": str(exc)})


@router.get("/reconcile")
def reconcile(user: dict = Depends(_PS_READ)):
    """Read-only: clean is true only when every lot equals its ledger."""
    return reports.reconcile_report()
