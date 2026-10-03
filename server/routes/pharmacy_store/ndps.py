"""The NDPS register: endpoints under /api/pharmacy-store.

Empty until grn-13 (W7-G). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  ndps/register, ndps/balances, ndps/variances, ndps/export.csv   pharmacy_store.ndps

The register hook is off until a site sets ORB_PHARMACY_NDPS_ON_FAILURE ('block' or 'record', ruling GRN-RS7, held) as well as ORB_PHARMACY_NDPS_REGISTER. Witness by a second user's PIN.
"""
from fastapi import APIRouter

router = APIRouter()
