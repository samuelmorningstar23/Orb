"""Orb Hospital OS — Audit Routes"""

from typing import Any, NamedTuple

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query
from permissions import require_permission
from services.actor_names import attach_actor_names

from services import audit_facility

router = APIRouter(prefix="/api/audit", tags=["audit"])

# Facilities (multisite-11): with more than one facility on record a reader
# sees only the rows of their facilities under the epoch rule
# (services/audit_facility.py): a row naming a facility is that facility's, a
# row naming none from before the first expansion is facility 1's, and a later
# row naming none is shown only to a group-wide administrator. Each row then
# carries `facility` and the answer lists the `facilities` the reader may
# filter by; ?facility= only ever narrows, and one outside the reader's
# facilities is a 404. With one facility nothing here changes.

_NO_SUCH_FACILITY = "There is no facility with that number among the ones you can read."


class _FacilityView(NamedTuple):
    """How one audit read is limited by facility."""

    multi: bool
    epoch: int
    clause: str
    params: tuple[int, ...]


def _facility_view(db: Any, user: dict, facility: int | None) -> _FacilityView:
    """The facility condition for this reader (and ?facility=, which only
    narrows). With one facility on record: no condition and nothing read
    beyond the cached facility count."""
    try:
        scope, multi = audit_facility.narrowed_scope(user, facility, db=db)
    except audit_facility.FacilityFilterError:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": _NO_SUCH_FACILITY}
        ) from None
    epoch = audit_facility.audit_epoch(db) if multi else 0
    clause, params = audit_facility.visible_sql(scope, epoch)
    return _FacilityView(multi, epoch, clause, params)


_GET_AUDIT_LOGS_PATIENT_ID_DEFAULT = Query(None)
_GET_AUDIT_LOGS_CORRELATION_ID_DEFAULT = Query(None)
_GET_AUDIT_LOGS_ACTION_TYPE_DEFAULT = Query(None)
_GET_AUDIT_LOGS_EVENT_CATEGORY_DEFAULT = Query(None)
_GET_AUDIT_LOGS_OUTCOME_DEFAULT = Query(None)
_GET_AUDIT_LOGS_START_DATE_DEFAULT = Query(None)
_GET_AUDIT_LOGS_END_DATE_DEFAULT = Query(None)
_GET_AUDIT_LOGS_LIMIT_DEFAULT = Query(50, ge=1, le=1000)
_GET_AUDIT_LOGS_OFFSET_DEFAULT = Query(0, ge=0)
_GET_AUDIT_LOGS_FACILITY_DEFAULT = Query(None, ge=1)
_GET_AUDIT_LOGS_USER_DEFAULT = Depends(require_permission("audit.read"))


@router.get("/logs")
def get_audit_logs(
    patient_id: str | None = _GET_AUDIT_LOGS_PATIENT_ID_DEFAULT,
    correlation_id: str | None = _GET_AUDIT_LOGS_CORRELATION_ID_DEFAULT,
    action_type: str | None = _GET_AUDIT_LOGS_ACTION_TYPE_DEFAULT,
    event_category: str | None = _GET_AUDIT_LOGS_EVENT_CATEGORY_DEFAULT,
    outcome: str | None = _GET_AUDIT_LOGS_OUTCOME_DEFAULT,
    start_date: str | None = _GET_AUDIT_LOGS_START_DATE_DEFAULT,
    end_date: str | None = _GET_AUDIT_LOGS_END_DATE_DEFAULT,
    limit: int = _GET_AUDIT_LOGS_LIMIT_DEFAULT,
    offset: int = _GET_AUDIT_LOGS_OFFSET_DEFAULT,
    facility: int | None = _GET_AUDIT_LOGS_FACILITY_DEFAULT,
    user: dict = _GET_AUDIT_LOGS_USER_DEFAULT,
) -> dict:
    """Retrieve audit logs. Requires audit.read permission."""

    query = "SELECT * FROM audit_logs WHERE 1=1"
    params: list[str | int] = []

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
            query += " AND event_category IN ({})".format(",".join("?" * len(cats)))
            params.extend(cats)
    if outcome:
        # Comma-separated too: the Security tab defaults to "failure,denied" so
        # the handful of refusals are not buried under every successful sign-in.
        outs = [o.strip() for o in outcome.split(",") if o.strip()]
        if outs:
            query += " AND outcome IN ({})".format(",".join("?" * len(outs)))
            params.extend(outs)
    if start_date:
        query += " AND timestamp >= ?"
        params.append(start_date)
    if end_date:
        query += " AND timestamp <= ?"
        params.append(end_date)

    with db_session() as db:
        view = _facility_view(db, user, facility)
        if view.clause:
            query += " AND " + view.clause
            params.extend(view.params)
        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = db.execute(query, params).fetchall()

        # Get total count for pagination
        count_query = query.replace("SELECT *", "SELECT COUNT(id) as total", 1).split(" ORDER BY")[
            0
        ]
        count_params = params[:-2]
        total = db.execute(count_query, count_params).fetchone()["total"]
        # The person, by name, read now: the chained row keeps only the id.
        logs = attach_actor_names(db, [dict(r) for r in rows])
        facilities: list[dict] = []
        if view.multi:
            audit_facility.attach_facilities(db, logs, view.epoch)
            facilities = audit_facility.facility_options(
                audit_facility.reader_scope(user, db=db), db=db
            )

    body: dict[str, Any] = {
        "logs": logs,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
    if view.multi:
        body["facilities"] = facilities
    return body


# Fixed by the audit_logs CHECK constraint (database.py), so it is not scanned for.
_OUTCOMES = ("success", "failure", "partial_success", "denied")


_GET_AUDIT_FACETS_EVENT_CATEGORY_DEFAULT = Query(None)
_GET_AUDIT_FACETS_USER_DEFAULT = Depends(require_permission("audit.read"))


@router.get("/facets")
def get_audit_facets(
    event_category: str | None = _GET_AUDIT_FACETS_EVENT_CATEGORY_DEFAULT,
    user: dict = _GET_AUDIT_FACETS_USER_DEFAULT,
) -> dict:
    """The categories and actions the trail actually holds, for the filter lists.

    The audit screen used to hard-code them and they drifted: it offered 'auth',
    which matched no row, while sign-ins were written as 'authentication', and
    any action added since was not offered at all. `event_category`
    (comma-separated, like /logs) narrows the actions to those categories.
    The category list and the full action list are read from indexes on
    audit_logs; a narrowed list reads only the chosen categories' rows. With
    more than one facility on record both lists come from the rows the reader
    may see.
    """
    cats = [c.strip() for c in (event_category or "").split(",") if c.strip()]
    with db_session() as db:
        view = _facility_view(db, user, None)
        where = f" WHERE {view.clause}" if view.clause else ""
        also = f" AND {view.clause}" if view.clause else ""
        categories = [
            r["v"]
            for r in db.execute(
                f"SELECT DISTINCT event_category AS v FROM audit_logs{where} ORDER BY v",
                view.params,
            ).fetchall()
        ]
        if cats:
            actions = [
                r["v"]
                for r in db.execute(
                    "SELECT DISTINCT action_type AS v FROM audit_logs "
                    "WHERE event_category IN ({}){} ORDER BY v".format(
                        ",".join("?" * len(cats)), also
                    ),
                    [*cats, *view.params],
                ).fetchall()
            ]
        else:
            actions = [
                r["v"]
                for r in db.execute(
                    f"SELECT DISTINCT action_type AS v FROM audit_logs{where} ORDER BY v",
                    view.params,
                ).fetchall()
            ]
    return {"categories": categories, "actions": actions, "outcomes": list(_OUTCOMES)}
