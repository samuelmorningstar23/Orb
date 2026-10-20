"""Medication orders, hold, resume and stop: endpoints under /api/medorders.

Empty until medorders-6a (W6-D). The package router adds the /api/medorders
prefix and the ORB_MED_ORDER_STORE gate; this router adds the
ORB_MED_ORDER_LIFECYCLE gate.

Planned endpoints (docs/his_design/medorders.json, sections 2 and 8; ruling
MO-R2 (a): only the prescriber holds, resumes or stops a live order):
  POST /{id}/hold     medication.prescribe, reason required
  POST /{id}/resume   medication.prescribe, reason required; re-screened,
                      excluding the order's own ward row
  POST /{id}/stop     medication.prescribe, reason required
A nurse still records a single dose as not given (held) at the bedside; that
is always allowed (services.medorders.core.order_allows_dose).
"""

from fastapi import APIRouter, Depends

from routes.medorders import require_med_order_lifecycle

router = APIRouter(dependencies=[Depends(require_med_order_lifecycle)])
