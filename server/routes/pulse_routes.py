"""Orb Pulse — API Routes

Environmental intelligence endpoints.
Data flows IN only — zero patient data egress.

Endpoints:
  GET /api/pulse/dashboard       — Full environmental dashboard
  GET /api/pulse/weather         — Weather + 7-day forecast
  GET /api/pulse/aqi             — Air Quality Index
  GET /api/pulse/flu             — CDC flu surveillance
  GET /api/pulse/drug-recalls    — FDA drug recall alerts
  GET /api/pulse/audit-log       — Ingestion audit trail
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from auth import get_current_user
from permissions import require_permission
from services.pulse_service import (
    get_pulse_dashboard,
    fetch_weather,
    fetch_aqi,
    fetch_cdc_flu_data,
    fetch_fda_recalls,
    get_ingestion_audit_log,
)

router = APIRouter(prefix="/api/pulse", tags=["pulse"])


@router.get("/dashboard")
async def pulse_dashboard(
    lat: float = Query(default=19.0760, description="Latitude"),
    lon: float = Query(default=72.8777, description="Longitude"),
    user: dict = Depends(require_permission("forecast.read")),
):
    """Full environmental intelligence dashboard."""
    try:
        return await get_pulse_dashboard(lat, lon)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pulse error: {str(e)}")


@router.get("/weather")
async def weather(
    lat: float = Query(default=19.0760),
    lon: float = Query(default=72.8777),
    user: dict = Depends(require_permission("forecast.read")),
):
    """Current weather + 7-day forecast with clinical risk flags."""
    try:
        return await fetch_weather(lat, lon)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Weather error: {str(e)}")


@router.get("/aqi")
async def air_quality(
    lat: float = Query(default=19.0760),
    lon: float = Query(default=72.8777),
    user: dict = Depends(require_permission("forecast.read")),
):
    """Air Quality Index with respiratory risk flags."""
    try:
        return await fetch_aqi(lat, lon)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AQI error: {str(e)}")


@router.get("/flu")
async def flu_surveillance(
    user: dict = Depends(require_permission("forecast.read")),
):
    """CDC influenza-like illness surveillance data."""
    try:
        return await fetch_cdc_flu_data()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"CDC error: {str(e)}")


@router.get("/drug-recalls")
async def drug_recalls(
    limit: int = Query(default=10, le=50),
    user: dict = Depends(require_permission("forecast.read")),
):
    """Recent FDA drug recall/enforcement actions."""
    try:
        return await fetch_fda_recalls(limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"FDA error: {str(e)}")


@router.get("/audit-log")
def audit_log(
    limit: int = Query(default=50, le=200),
    user: dict = Depends(require_permission("audit.read")),
):
    """Audit trail of all external data ingestion events."""
    return get_ingestion_audit_log(limit)
