"""Ward returns: endpoints under /api/pharmacy-store.

Empty until grn-15 (W9-D). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  ward-returns, plus inspect                pharmacy_store.write; nurses (ward_return.record) per GRN-R8

Restock only of sealed, in-date, non-cold-chain, non-NDPS packs after inspection (ruling GRN-RS3, held).
"""
from fastapi import APIRouter

router = APIRouter()
