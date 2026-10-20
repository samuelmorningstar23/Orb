"""Orb Chart Timeline — one chronological view of everything that happened.

Three read/near-read endpoints over services.patient_timeline_service:

    GET  /api/chart/patients/{patient_id}/timeline   the rail itself
    POST /api/chart/patients/{patient_id}/reviewed   move my watermark forward
    GET  /api/chart/unseen                           per-patient "N new" for the board

SCOPING. The two per-patient routes take the id off the PATH and hang
``require_patient_access`` on it, which is the single guard that (a) refuses a
patient outside the caller's department with a 404 rather than a 403 — a 403
would confirm the patient exists — and (b) writes the fail-closed ``phi_access``
audit row. Nothing here re-derives scope by hand; that duplication is what put
holes in four read surfaces on 2026-08-05.

``/unseen`` cannot use that dependency (it names no patient), so it derives its
scope from ``department_scope(user)`` inside the query, the same way
/api/search/patients does, and audits the read explicitly.

WHY THESE ARE SYNC ``def``. They do blocking SQLite work and no awaiting. FastAPI
runs a sync route in its threadpool, so the query and the audit write stay off
the asyncio event loop — the discipline that ended the 2026-07-30 loop stalls.
Marking them ``async def`` would silently put both back on the loop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import (
    department_scope,
    log_phi_read,
    require_patient_access,
    require_permission,
)
from pydantic import BaseModel
from services.patient_timeline_service import (
    build_chart_timeline,
    mark_reviewed,
    unseen_counts_for_scope,
)

router = APIRouter(prefix="/api/chart", tags=["chart-timeline"])


class ReviewedRequest(BaseModel):
    """Optional explicit watermark.

    Omitted (the normal case) means "now". A client sends one when it wants the
    mark pinned to the newest event it actually RENDERED, so that an event
    landing between the render and the click is not silently marked as read.
    """

    as_of: str | None = None


_PATIENT_TIMELINE_LIMIT_DEFAULT = Query(150, ge=1, le=500)
_PATIENT_TIMELINE_USER_DEFAULT = Depends(
    require_patient_access("patient.read_all", audit_action="patient_timeline_viewed")
)


@router.get("/patients/{patient_id}/timeline")
def patient_timeline(
    patient_id: str,
    limit: int = _PATIENT_TIMELINE_LIMIT_DEFAULT,
    user: dict = _PATIENT_TIMELINE_USER_DEFAULT,
):
    """Everything on this chart, newest first, with this reader's watermark.

    ``new_count`` is computed by the database across ALL rows, not across the
    (possibly truncated) list returned here — "6 new" above a capped rail has to
    still be true.
    """
    return build_chart_timeline(patient_id, user_id=user["id"], limit=limit)


_MARK_CHART_REVIEWED_USER_DEFAULT = Depends(
    require_patient_access("patient.read_all", audit_action="patient_timeline_reviewed")
)


@router.post("/patients/{patient_id}/reviewed")
def mark_chart_reviewed(
    patient_id: str,
    req: ReviewedRequest | None = None,
    user: dict = _MARK_CHART_REVIEWED_USER_DEFAULT,
):
    """Record that this clinician has read this chart up to now.

    Deliberately a **personal bookmark, not a clinical attestation**: it is not
    a sign-off, it does not assert the chart was acted on, and nothing safety-
    critical reads it. It never moves backwards, so a replayed offline request
    cannot resurrect a badge over events the clinician has already dealt with.
    """
    try:
        result = mark_reviewed(user["id"], patient_id, (req.as_of if req else None))
    except ValueError as e:
        # An unparseable as_of is the client's bug, not a server fault, and it
        # must not be swallowed into "now" — that would mark events read that
        # were never rendered.
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"patient_id": patient_id, "new_count": 0, **result}


_UNSEEN_COUNTS_USER_DEFAULT = Depends(require_permission("patient.read_all"))


@router.get("/unseen")
# Plain def on purpose (no awaits) — see the module docstring.
def unseen_counts(
    request: Request,
    user: dict = _UNSEEN_COUNTS_USER_DEFAULT,
):
    """{patient_id: N} for admitted patients this clinician has reviewed before.

    Patients never reviewed by this account are ABSENT, not zero — a badge on
    every bed on a clinician's first login teaches them to ignore the badge.

    One query for the whole board rather than one per patient: the ward board
    draws 30 rows and this must not become 30 round trips against an encrypted
    SQLite file. It is also deliberately NOT wired to the vitals poll — one
    audited read per board load, not one per tick, because a per-poll encrypted
    write is the exact shape behind the 2026-07-30 loop stalls.
    """
    dept = department_scope(user)
    counts = unseen_counts_for_scope(user["id"], dept)

    # Audited only when something was actually disclosed. An empty result names
    # no patient and therefore discloses nothing worth a row.
    if counts:
        log_phi_read(
            request=request,
            user=user,
            action_type="chart_unseen_counts",
            summary_message=(
                f"Unseen-since-review counts for {len(counts)} patient(s) in "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={"patient_ids": sorted(counts.keys()), "result_count": len(counts)},
        )

    return {"counts": counts, "department": dept}
