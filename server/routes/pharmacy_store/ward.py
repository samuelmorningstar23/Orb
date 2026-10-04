"""Ward stock, ward transfers and ward indents: endpoints under /api/pharmacy-store.

Empty until grn-21, grn-23 (W8-F, W9-F). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  ward/stock, ward/transfers/awaiting, ward/transfers/{transfer_id}/receive, ward/indents   ward_* permissions with department_scope

Also behind ORB_PHARMACY_WARD_STOCK (ruling GRN-RS5, held, so the strict
default is "off"): this router carries feature_gate('pharmacy_ward_stock') as a
router-level dependency, so every endpoint added here runs the pharmacy-store
module gate first, then this feature gate, then its own permission. Do not add
the feature gate again per route, and do not move a ward endpoint to another
sub-router to dodge it. tests/test_his_route_skeletons.py pins both the gate
and its order. Another ward's id is 404.
"""
from fastapi import APIRouter, Depends

from routes.opt_in_gates import feature_gate

require_pharmacy_ward_stock_feature = feature_gate("pharmacy_ward_stock")

router = APIRouter(dependencies=[Depends(require_pharmacy_ward_stock_feature)])
