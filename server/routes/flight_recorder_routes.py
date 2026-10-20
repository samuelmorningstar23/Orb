"""Orb Hospital OS — Flight Recorder Routes (A8 v1)

Admin-only replay/forensics API over the tamper-evident audit log and the event
bus. Same auth pattern as audit_routes: require_permission("audit.read"), which
is granted to the admin role only (see permissions.ROLE_PERMISSIONS).
"""

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query
from permissions import require_permission
from services.actor_names import attach_actor_names
from services.flight_recorder_service import (
    get_event_detail,
    get_timeline,
    verify_chain,
)

router = APIRouter(prefix="/api/flight-recorder", tags=["flight-recorder"])


_TIMELINE_PATIENT_ID_DEFAULT = Query(None)
_TIMELINE_SINCE_DEFAULT = Query(None, description="ISO-8601 lower bound (inclusive)")
_TIMELINE_UNTIL_DEFAULT = Query(None, description="ISO-8601 upper bound (inclusive)")
_TIMELINE_LIMIT_DEFAULT = Query(500, ge=1, le=5000)
_TIMELINE_USER_DEFAULT = Depends(require_permission("audit.read"))


@router.get("/timeline")
def timeline(
    patient_id: str | None = _TIMELINE_PATIENT_ID_DEFAULT,
    since: str | None = _TIMELINE_SINCE_DEFAULT,
    until: str | None = _TIMELINE_UNTIL_DEFAULT,
    limit: int = _TIMELINE_LIMIT_DEFAULT,
    user: dict = _TIMELINE_USER_DEFAULT,
):
    """Unified, time-ordered replay stream (audit + events + order transitions)."""
    events = get_timeline(patient_id=patient_id, since=since, until=until, limit=limit)
    # Names read now, never stored in the chain. An event bus row's actor is a
    # source node, not a user, so it resolves to None.
    with db_session() as db:
        attach_actor_names(db, events, id_key="actor")
    return {"events": events, "count": len(events)}


_VERIFY_PATIENT_ID_DEFAULT = Query(None)
_VERIFY_USER_DEFAULT = Depends(require_permission("audit.read"))


@router.get("/verify")
def verify(
    patient_id: str | None = _VERIFY_PATIENT_ID_DEFAULT,
    user: dict = _VERIFY_USER_DEFAULT,
):
    """Verify the audit hash chain; returns {ok, broken_at, count}."""
    return verify_chain(patient_id=patient_id)


_EVENT_DETAIL_USER_DEFAULT = Depends(require_permission("audit.read"))


@router.get("/event/{source}/{event_id}")
def event_detail(
    source: str,
    event_id: str,
    user: dict = _EVENT_DETAIL_USER_DEFAULT,
):
    """Full raw detail for a single timeline entry."""
    if source not in ("audit", "event", "order"):
        raise HTTPException(status_code=400, detail="source must be one of: audit, event, order")
    detail = get_event_detail(event_id, source)
    if detail is None:
        raise HTTPException(status_code=404, detail="event not found")
    event = detail.get("event")
    if isinstance(event, dict):
        id_key = "actor_user_id" if source == "audit" else "actor_id"
        if id_key in event:
            with db_session() as db:
                attach_actor_names(db, [event], id_key=id_key)
    return detail
