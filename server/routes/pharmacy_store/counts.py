"""Stock take and adjustments: endpoints under /api/pharmacy-store.

Empty until grn-12 (W6-E). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  counts, plus submit and approve; adjustments   approver differs from the counter
"""
from fastapi import APIRouter

router = APIRouter()
