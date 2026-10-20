"""Medication orders, pharmacist verification: endpoints under /api/medorders.

Empty until medorders-5a (W6-D). The package router adds the /api/medorders
prefix and the ORB_MED_ORDER_STORE gate; this router adds the
ORB_MED_ORDER_LIFECYCLE gate, because one pharmacist verification of the order
is a lifecycle step (in record mode the Verify Queue keeps working through
routes/pharmacy_routes.py, and a linked row is mirrored into the store there).

Planned endpoints (docs/his_design/medorders.json, sections 5 and 8):
  GET  /queue/verification   orders needing a pharmacist, department-scoped
  POST /{id}/verify          medication.verify; the gate screens the order's
                             drug, dose and route with the pharmacist's role,
                             excluding the order's own ward row; a product is
                             bound only when it is the same ingredient (any
                             other product is a substitution, refused until
                             ruling R002)
  POST /{id}/reject          medication.verify, with a reason
"""

from fastapi import APIRouter, Depends

from routes.medorders import require_med_order_lifecycle

router = APIRouter(dependencies=[Depends(require_med_order_lifecycle)])
