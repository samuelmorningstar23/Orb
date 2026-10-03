"""Pharmacy purchasing and goods receipt: endpoints under /api/pharmacy-store.

Empty until grn-7, grn-8 (W4-C, W5-C). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  purchase-orders, plus submit, approve, issue, cancel, reorder-suggestions
  goods-receipts, plus post and cancel     expiry typed twice (ruling GRN-RS6, held)

Document numbers are stamped after commit, as procurement does.
"""
from fastapi import APIRouter

router = APIRouter()
