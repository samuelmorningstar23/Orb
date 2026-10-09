"""Medication orders, pharmacy supply: endpoints under /api/medorders
(medorders-5b, W7-E).

The package router adds the /api/medorders prefix and the ORB_MED_ORDER_STORE
gate; this router adds the ORB_MED_ORDER_LIFECYCLE gate, so the endpoints below
answer 404 feature_disabled while either switch is off.

Ruling MO-R12 (a): the prescriber picks the supply source on each order. Orb
makes no supply row on its own for a chart or a CPOE order; asking pharmacy is
this explicit request.

  POST /orders/{id}/request-supply
      medication.prescribe or medication.verify (design section 8). An order
      outside the caller's department is an audited 404. Idempotent: one open
      supply row per order, so a second request answers the same row
      (replayed true). The row always starts 'pending' through the unchanged
      helix_service.insert_order, with the order's exact formulary product.
      Refusals use core.http_detail: order_not_live, order_held,
      not_formulary_bound, supply_source_conflict,
      dose_route_and_frequency_required, formulary_product_unavailable.

  POST /orders/{id}/supply-source   {supply_source, expected_version?}
      medication.prescribe only (the prescriber picks: pharmacy, ward_stock or
      patient_own). An order outside the caller's department is an audited
      404. Choosing the source the order already has is a replay. Refusals:
      order_not_live, supply_open (a pharmacy supply is still open and the
      choice moves away from pharmacy), stale_version.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from permissions import (
    MEDICATION_PRESCRIBE,
    MEDICATION_VERIFY,
    require_any_permission,
    require_permission,
)
from routes.medorders import require_med_order_lifecycle
from routes.medorders.core import (
    ORDER_NOT_FOUND,
    _emit,
    _med_order_patient_in_scope_or_404,
    _refusal,
    _request_meta,
)
from services.medorders import core as store
from services.medorders import queries
from services.medorders import supply as supply_service

router = APIRouter(dependencies=[Depends(require_med_order_lifecycle)])


def _supply_view(row: dict) -> dict:
    """The supply row as the order screen needs it: ids, state and what was
    asked for. The pharmacy's own notes stay on the pharmacy screens."""
    keys = ("id", "patient_id", "medication_id", "dosage", "route", "frequency", "status",
            "created_at", "med_order_id")
    return {k: row.get(k) for k in keys}


@router.post("/orders/{order_id}/request-supply")
def request_supply(order_id: str, request: Request,
                   user: dict = Depends(require_any_permission(MEDICATION_PRESCRIBE,
                                                               MEDICATION_VERIFY))):
    """Ask pharmacy to supply this order. Answers {order, supply, replayed}."""
    _med_order_patient_in_scope_or_404(order_id, user, request)
    try:
        result = supply_service.request_supply(
            order_id, actor_id=user["id"], actor_role=user.get("role"),
            request_meta=_request_meta(request))
    except store.MedOrderNotFound:
        raise HTTPException(status_code=404, detail=ORDER_NOT_FOUND)
    except store.MedOrderError as e:
        raise _refusal(e, user, request)
    if not result["replayed"]:
        _emit(result["order"])
    return {
        "order": queries.serialize_order(result["order"]),
        "supply": _supply_view(result["supply"]),
        "replayed": result["replayed"],
    }


class SupplySourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supply_source: Literal["pharmacy", "ward_stock", "patient_own"]
    expected_version: Optional[int] = None


@router.post("/orders/{order_id}/supply-source")
def set_supply_source(order_id: str, req: SupplySourceRequest, request: Request,
                      user: dict = Depends(require_permission(MEDICATION_PRESCRIBE))):
    """The prescriber says where this order's medicine comes from (ruling
    MO-R12 (a)). Answers {order, replayed}."""
    _med_order_patient_in_scope_or_404(order_id, user, request)
    try:
        result = supply_service.set_supply_source(
            order_id, req.supply_source, actor_id=user["id"], actor_role=user.get("role"),
            expected_version=req.expected_version, request_meta=_request_meta(request))
    except store.MedOrderNotFound:
        raise HTTPException(status_code=404, detail=ORDER_NOT_FOUND)
    except store.MedOrderError as e:
        raise _refusal(e, user, request)
    if not result["replayed"]:
        _emit(result["order"])
    return {"order": queries.serialize_order(result["order"]), "replayed": result["replayed"]}
