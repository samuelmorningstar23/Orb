"""Orb Forecast — What-If Capacity & Surge Simulator API Routes

Interactive scenario modeling on the hospital's own forecast baseline. Gated
behind ``forecast.read`` (same permission as the Forecast pillar). Auth follows
the existing Bearer/JWT pattern via ``require_permission`` (see orders_routes.py).

Endpoint:
  POST /api/capacity-sim/   — run a what-if scenario against the live baseline
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from permissions import require_permission, FORECAST_READ
from services import capacity_sim_service

router = APIRouter(prefix="/api/capacity-sim", tags=["Capacity Simulator"])


class ScenarioRequest(BaseModel):
    # Horizon
    horizon_hours: int = 24
    # Knobs
    extra_admissions: float = 0.0
    beds_delta: int = 0
    admission_rate_multiplier: float = 1.0
    los_multiplier: float = 1.0
    discharge_boost: float = 0.0
    nurse_patient_ratio: Optional[float] = None
    # Optional baseline overrides / step-label clock (never affect the math
    # except by defining the baseline itself)
    baseline_census: Optional[int] = None
    baseline_capacity: Optional[int] = None
    admission_rate_daily: Optional[float] = None
    now: Optional[str] = None


@router.post("")
@router.post("/")
def run_scenario(req: ScenarioRequest, user=Depends(require_permission(FORECAST_READ))):
    """Project occupancy, overflow, discharge need, and staffing gap for a
    what-if scenario against the current forecast baseline."""
    try:
        scenario = req.model_dump(exclude={"horizon_hours"}, exclude_none=True)
        return capacity_sim_service.simulate(scenario, horizon_hours=req.horizon_hours)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Simulation error: {str(e)}")
