"""Lot status, recall and write-off: endpoints under /api/pharmacy-store.

Empty until grn-10 (W6-E). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  lots, lots/{lot_id}/quarantine, recall, write-off, recall-trace

Releasing a quarantined lot needs a different pharmacist with a reason; recalled and NSQ are terminal (ruling GRN-RS2, held). The recall trace is a phi_access read.
"""

from fastapi import APIRouter

router = APIRouter()
