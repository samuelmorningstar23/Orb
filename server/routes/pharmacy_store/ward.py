"""Ward stock, ward transfers and ward indents: endpoints under /api/pharmacy-store.

Empty until grn-21, grn-23 (W8-F, W9-F). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  ward/stock, ward/transfers/awaiting, ward/transfers/{transfer_id}/receive, ward/indents   ward_* permissions with department_scope

Also behind ORB_PHARMACY_WARD_STOCK (ruling GRN-RS5, held): add feature_gate('pharmacy_ward_stock') from routes.opt_in_gates to these routes. Another ward's id is 404.
"""
from fastapi import APIRouter

router = APIRouter()
