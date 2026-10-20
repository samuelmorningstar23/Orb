"""Pharmacy store: the /api/pharmacy-store route package.

Created empty in wave 1 of the hospital-system build (3 October 2026) and
registered once in main.py, so each later pharmacy-store increment adds
endpoints to its own sub-module here instead of editing main.py.

THE WHOLE PACKAGE IS BEHIND ORB_PHARMACY_STORE (default off). The gate is a
router-level dependency on this package router, so every endpoint a
sub-module adds inherits it and answers 404 while the module is off (after a
401 for a caller with no session). tests/test_his_route_skeletons.py checks
every /api/pharmacy-store route carries it.

Rules for every endpoint added here (design: docs/his_design/grn.json):
  * a plain `def` handler;
  * one tier dependency built at import, as procurement_routes does:
    pharmacy_store.read; read plus write; read plus approve; read plus ndps.
    The ward endpoints use the ward_* permissions with department_scope;
  * every write audits through audit_service with db=conn inside the same
    immediate transaction as its stock movements; Helix stays the only owner of
    drug stock (services/pharmacy_stock_service, grn-4);
  * a wrong-domain or another ward's id is 404, a separation-of-duties refusal
    is 403, a conflict is 409.

Sub-modules, one per concern, each exporting `router` (no prefix):
  masters         drugs, classification requests, stores, par levels, vendors
  purchasing      purchase orders and goods receipts
  stock           stock, ledger, near expiry, reorder, reconcile
  lots            quarantine, recall, write-off, recall trace
  transfers       pharmacy-internal transfers
  counts          stock take and adjustments
  ndps            the NDPS register, balances, variances, export
  ward            ward stock, ward transfers, ward indents (ORB_PHARMACY_WARD_STOCK)
  returns         ward returns and their inspection
  vendor_returns  returns to vendor
"""

import config
from fastapi import APIRouter, Depends
from permissions import PHARMACY_STORE_READ, require_permission

from routes.opt_in_gates import require_pharmacy_store_module
from routes.pharmacy_store import (
    counts,
    lots,
    masters,
    ndps,
    purchasing,
    returns,
    stock,
    transfers,
    vendor_returns,
    ward,
)

router = APIRouter(
    prefix="/api/pharmacy-store",
    tags=["Pharmacy store"],
    dependencies=[Depends(require_pharmacy_store_module)],
)


_PHARMACY_STORE_META_USER_DEFAULT = Depends(require_permission(PHARMACY_STORE_READ))


@router.get("/meta")
def pharmacy_store_meta(user: dict = _PHARMACY_STORE_META_USER_DEFAULT):
    """What this site's pharmacy store is set to. No patient data and no stock."""
    return {
        "module": "pharmacy-store",
        "state": config.module_state("pharmacy-store"),
        "features": {
            "pharmacy_ward_stock": config.feature_enabled("pharmacy_ward_stock"),
            "ndps_register": config.feature_enabled("ndps_register"),
        },
        "settings": {
            "near_expiry_days": config.PHARMACY_NEAR_EXPIRY_DAYS,
            "min_shelf_life_days": config.PHARMACY_MIN_SHELF_LIFE_DAYS,
            "licence_warn_days": config.PHARMACY_LICENCE_WARN_DAYS,
            "max_dispense_units": config.PHARMACY_MAX_DISPENSE_UNITS,
        },
    }


for _sub in (
    masters,
    purchasing,
    stock,
    lots,
    transfers,
    counts,
    ndps,
    ward,
    returns,
    vendor_returns,
):
    router.include_router(_sub.router)
