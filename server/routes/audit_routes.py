"""Orb Hospital OS — Audit Routes"""
from fastapi import APIRouter, Depends, Query
from typing import Optional

from database import db_session
from permissions import require_permission

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/logs")
def get_audit_logs(
    patient_id: Optional[str] = Query(None),
    correlation_id: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    event_category: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_permission("audit.read")),
):
    """Retrieve audit logs. Requires audit.read permission."""

    query = "SELECT * FROM audit_logs WHERE 1=1"
    params = []

    if patient_id:
        query += " AND patient_id = ?"
        params.append(patient_id)
    if correlation_id:
        query += " AND correlation_id = ?"
        params.append(correlation_id)
    if action_type:
        query += " AND action_type = ?"
        params.append(action_type)
    if event_category:
        # Accepts a COMMA-SEPARATED list. The Security tab is not one category:
        # sign-ins are written as 'authentication', permission denials as
        # 'authorization', and lockouts/posture changes as 'security'. It used
        # to ask for the single value 'auth', which matches none of the three —
        # so the tab rendered "No security events" while the Overview tab on the
        # same screen counted 39 authentication + 3 authorization events and the
        # Audit Logs tab listed them by timestamp.
        cats = [c.strip() for c in event_category.split(",") if c.strip()]
        if len(cats) == 1:
            query += " AND event_category = ?"
            params.append(cats[0])
        elif cats:
            query += " AND event_category IN (%s)" % ",".join("?" * len(cats))
            params.extend(cats)
    if outcome:
        # Comma-separated too: the Security tab defaults to "failure,denied" so
        # the handful of refusals are not buried under every successful sign-in.
        outs = [o.strip() for o in outcome.split(",") if o.strip()]
        if outs:
            query += " AND outcome IN (%s)" % ",".join("?" * len(outs))
            params.extend(outs)
    if start_date:
        query += " AND timestamp >= ?"
        params.append(start_date)
    if end_date:
        query += " AND timestamp <= ?"
        params.append(end_date)

    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with db_session() as db:
        rows = db.execute(query, params).fetchall()
        
        # Get total count for pagination
        count_query = query.replace("SELECT *", "SELECT COUNT(id) as total", 1).split(" ORDER BY")[0]
        count_params = params[:-2]
        total = db.execute(count_query, count_params).fetchone()["total"]

    return {
        "logs": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# Fixed by the audit_logs CHECK constraint (database.py), so it is not scanned for.
_OUTCOMES = ("success", "failure", "partial_success", "denied")


@router.get("/facets")
def get_audit_facets(
    event_category: Optional[str] = Query(None),
    user: dict = Depends(require_permission("audit.read")),
):
    """The categories and actions the trail actually holds, for the filter lists.

    The audit screen used to hard-code them and they drifted: it offered 'auth',
    which matched no row, while sign-ins were written as 'authentication', and
    any action added since was not offered at all. `event_category`
    (comma-separated, like /logs) narrows the actions to those categories.
    The category list and the full action list are read from indexes on
    audit_logs; a narrowed list reads only the chosen categories' rows.
    """
    cats = [c.strip() for c in (event_category or "").split(",") if c.strip()]
    with db_session() as db:
        categories = [r["v"] for r in db.execute(
            "SELECT DISTINCT event_category AS v FROM audit_logs ORDER BY v").fetchall()]
        if cats:
            actions = [r["v"] for r in db.execute(
                "SELECT DISTINCT action_type AS v FROM audit_logs "
                "WHERE event_category IN (%s) ORDER BY v" % ",".join("?" * len(cats)),
                cats).fetchall()]
        else:
            actions = [r["v"] for r in db.execute(
                "SELECT DISTINCT action_type AS v FROM audit_logs ORDER BY v").fetchall()]
    return {"categories": categories, "actions": actions, "outcomes": list(_OUTCOMES)}
