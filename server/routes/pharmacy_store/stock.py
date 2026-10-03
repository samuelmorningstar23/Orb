"""Pharmacy stock views: endpoints under /api/pharmacy-store.

Empty until grn-9 (W4-C). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  stock, ledger (paged by seq), near-expiry, reorder, reconcile   pharmacy_store.read

Near-expiry window from config.PHARMACY_NEAR_EXPIRY_DAYS (GRN-R16).
"""
from fastapi import APIRouter

router = APIRouter()
