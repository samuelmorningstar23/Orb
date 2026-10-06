"""Orb Pulse: season and conditions.

Pulse used to pull live internet feeds (weather, air quality, FDA recalls, CDC
flu). Orb is strictly offline, so since 6 October 2026 it answers from the
seasonal calendar built into Orb, for the hospital's Indian state. No request
here, or anywhere in Pulse, leaves the appliance. See services/seasonal_calendar.py.

Endpoint:
  GET /api/pulse/season   the season and conditions in the hospital's part of
                          India today, and what starts in the next 60 days

The old feed endpoints (/dashboard, /weather, /aqi, /flu, /drug-recalls,
/audit-log) are gone, not stubbed: a route that still answered would suggest a
feed still existed.
"""

from fastapi import APIRouter, Depends, HTTPException

from permissions import require_permission
from services import seasonal_calendar

router = APIRouter(prefix="/api/pulse", tags=["pulse"])

# Read the calendar at startup, so a missing or broken file is in the boot log
# rather than discovered on the first click. Never raises; never stops Orb.
seasonal_calendar.preload()


@router.get("/season")
def pulse_season(user: dict = Depends(require_permission("forecast.read"))):
    """Season and conditions for the hospital's state, from the built-in calendar."""
    try:
        return seasonal_calendar.season_report()
    except seasonal_calendar.CalendarError as e:
        raise HTTPException(status_code=503, detail=e.public_message)
