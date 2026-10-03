"""Orb — Impact / ROI Routes.

Exposes the quantified ROI / outcomes model (the investor "money slide"): Orb's
recorded clinical activity converted into defensible rupee figures and prevented-harm
estimates. Every assumption is overridable via query params so a CFO can re-run
the model with their own unit economics.

Gated on ``admin.overview`` — the same admin-governance permission the rest of
the admin console uses.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from permissions import require_permission
from services.impact_service import DEFAULT_ASSUMPTIONS, compute_impact

router = APIRouter(prefix="/api/admin/impact", tags=["impact"])


@router.get("/")
def get_impact(
    user: dict = Depends(require_permission("admin.overview")),
    period_days: int | None = Query(
        None, ge=1, le=3650, description="Reporting window in days (default 30)."),
    doc_minutes_saved_per_note: float | None = Query(None, ge=0),
    clinician_hourly_cost_inr: float | None = Query(None, ge=0),
    icu_day_cost_inr: float | None = Query(None, ge=0),
    deterioration_icu_avoidance_rate: float | None = Query(None, ge=0, le=1),
    ade_cost_inr: float | None = Query(None, ge=0),
    ade_avoidance_rate_per_alert: float | None = Query(None, ge=0, le=1),
    ward_bed_day_cost_inr: float | None = Query(None, ge=0),
    los_reduction_days_per_discharge: float | None = Query(None, ge=0),
):
    """Return the ROI / outcomes model.

    Any assumption may be overridden via its query param; omitted params use the
    cited default. Degrades gracefully to zeros on an empty database.
    """
    overrides = {
        "period_days": period_days,
        "doc_minutes_saved_per_note": doc_minutes_saved_per_note,
        "clinician_hourly_cost_inr": clinician_hourly_cost_inr,
        "icu_day_cost_inr": icu_day_cost_inr,
        "deterioration_icu_avoidance_rate": deterioration_icu_avoidance_rate,
        "ade_cost_inr": ade_cost_inr,
        "ade_avoidance_rate_per_alert": ade_avoidance_rate_per_alert,
        "ward_bed_day_cost_inr": ward_bed_day_cost_inr,
        "los_reduction_days_per_discharge": los_reduction_days_per_discharge,
    }
    # Drop anything not supplied so the service falls back to its cited defaults.
    overrides = {k: v for k, v in overrides.items() if v is not None}
    return compute_impact(overrides)


@router.get("/assumptions")
def get_assumptions(user: dict = Depends(require_permission("admin.overview"))):
    """Return the catalogue of tunable assumptions (value + unit + citation).

    Lets the UI render the editable "Assumptions" panel from a single source of
    truth without hardcoding the defaults on the client.
    """
    return {
        "assumptions": {
            k: {"value": v["value"], "unit": v["unit"], "citation": v["citation"]}
            for k, v in DEFAULT_ASSUMPTIONS.items()
        }
    }
