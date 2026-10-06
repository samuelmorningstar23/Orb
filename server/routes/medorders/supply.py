"""Medication orders, pharmacy supply: endpoints under /api/medorders.

Empty until medorders-5b (W7-E). The package router adds the /api/medorders
prefix and the ORB_MED_ORDER_STORE gate; this router adds the
ORB_MED_ORDER_LIFECYCLE gate.

Planned endpoint (docs/his_design/medorders.json, sections 5 and 8; ruling
MO-R12 (a): the prescriber picks pharmacy, ward stock or the patient's own
supply on each order):
  POST /{id}/request-supply   medication.prescribe or medication.verify; one
                              open supply row per order (idempotent), always
                              starting 'pending' through the unchanged
                              helix_service.insert_order
"""
from fastapi import APIRouter, Depends

from routes.medorders import require_med_order_lifecycle

router = APIRouter(dependencies=[Depends(require_med_order_lifecycle)])
