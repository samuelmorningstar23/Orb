"""Orb Hospital OS — Flight Recorder Routes (A8 v1)

Admin-only replay/forensics API over the tamper-evident audit log and the event
bus. Same auth pattern as audit_routes: require_permission("audit.read"), which
is granted to the admin role only (see permissions.ROLE_PERMISSIONS).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from permissions import require_permission
from services.flight_recorder_service import (
    get_event_detail,
    get_timeline,
    verify_chain,
)

router = APIRouter(prefix="/api/flight-recorder", tags=["flight-recorder"])


@router.get("/timeline")
def timeline(
    patient_id: Optional[str] = Query(None),
    since: Optional[str] = Query(None, description="ISO-8601 lower bound (inclusive)"),
    until: Optional[str] = Query(None, description="ISO-8601 upper bound (inclusive)"),
    limit: int = Query(500, ge=1, le=5000),
    user: dict = Depends(require_permission("audit.read")),
):
    """Unified, time-ordered replay stream (audit + events + order transitions)."""
    events = get_timeline(patient_id=patient_id, since=since, until=until, limit=limit)
    return {"events": events, "count": len(events)}


@router.get("/verify")
def verify(
    patient_id: Optional[str] = Query(None),
    user: dict = Depends(require_permission("audit.read")),
):
    """Verify the audit hash chain; returns {ok, broken_at, count}."""
    return verify_chain(patient_id=patient_id)


@router.get("/event/{source}/{event_id}")
def event_detail(
    source: str,
    event_id: str,
    user: dict = Depends(require_permission("audit.read")),
):
    """Full raw detail for a single timeline entry."""
    if source not in ("audit", "event", "order"):
        raise HTTPException(status_code=400, detail="source must be one of: audit, event, order")
    detail = get_event_detail(event_id, source)
    if detail is None:
        raise HTTPException(status_code=404, detail="event not found")
    return detail
