"""Returns to vendor: endpoints under /api/pharmacy-store.

Empty until grn-22 (W7-G). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  vendor-returns                            pharmacy_store.write, approve tier to post
"""
from fastapi import APIRouter

router = APIRouter()
