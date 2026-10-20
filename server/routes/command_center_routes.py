"""Orb Hospital OS — Executive Command Center API Routes

A single, house-wide operational snapshot for a CMO/COO: census + capacity,
acuity heatmap, active deteriorations, sepsis-bundle compliance, forecast and
downtime-readiness. Read-only aggregation over existing services.

Gated on `forecast.read` (clinicians + admin have it) — the same operational
read permission that guards the Forecast pillar.

Facilities (multisite-12). With more than one facility of a hospital group on
the appliance the snapshot is the caller's facility by default: every section
counts that facility's patients, beds, bundles, forecast and operating list
(command_center_service.house_snapshot(facility_ids)). A reader with no
facility limit (an administrator with an audited enterprise grant) gets the
group's figures with one tile per facility (`facility_tiles`) and may narrow to
one facility with ?facility=; a facility outside the reader's own, or one not
on record, is a 404 exactly like the audit views. The answer then also says
which facility it is about (`facility`, None for the group) and lists the
facilities the reader may choose (`facilities`). With one facility on record
nothing here changes, and ?facility=1 is the same answer.
"""

from typing import Any

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import (
    FORECAST_READ,
    department_scope,
    log_phi_read,
    require_permission,
    scope_patient_ids_sql,
)

from services import audit_facility, command_center_service, facility_service

router = APIRouter(prefix="/api/command-center", tags=["Command Center"])

_NO_SUCH_FACILITY = "There is no facility with that number among the ones you can see."


def _facility_view(
    user: dict, facility: int | None
) -> tuple[frozenset[int] | None, bool, list[dict[str, Any]]]:
    """(the facilities to count, whether more than one is on record, the
    facilities the reader may choose). ?facility= only narrows; outside the
    reader's facilities it is a 404. With one facility nothing is read beyond
    the cached facility count."""
    with db_session() as db:
        try:
            scope, multi = audit_facility.narrowed_scope(user, facility, db=db)
        except audit_facility.FacilityFilterError:
            raise HTTPException(
                status_code=404, detail={"error": "not_found", "message": _NO_SUCH_FACILITY}
            ) from None
        options = (
            audit_facility.facility_options(audit_facility.reader_scope(user, db=db), db=db)
            if multi
            else []
        )
    return scope, multi, options


_GET_HOUSE_SNAPSHOT_USER_DEFAULT = Depends(require_permission(FORECAST_READ))
_GET_HOUSE_SNAPSHOT_FACILITY_DEFAULT = Query(
    None, ge=1, description="Narrow to one facility among the reader's own"
)


@router.get("", response_model=None)
@router.get("/", response_model=None)
def get_house_snapshot(
    request: Request,
    facility: int | None = _GET_HOUSE_SNAPSHOT_FACILITY_DEFAULT,
    user: dict = _GET_HOUSE_SNAPSHOT_USER_DEFAULT,
) -> dict:
    """Return the command-center snapshot.

    Never raises for a data-layer hiccup: each section degrades to zeros with an
    `error` note so the operations board always renders.

    ONE section is identity-bearing: `deteriorations` emits patient_id, name,
    ward and bed for every critical/elevated patient in the house. `forecast.read`
    is held by NURSES, so that list was a hospital-wide named PHI disclosure on a
    dashboard (audit 2026-08-05, same class as /api/forecast/discharge-board).
    It is now filtered to the caller's department.

    The aggregate sections (census, acuity, bundles, forecast, downtime
    readiness, OR count) are deliberately left house-wide: they are counts and
    percentages that name nobody, and an operations board that silently reported
    one ward's occupancy as "the house" would mislead the executive it is built
    for. If that judgement is ever revisited, revisit it here — not by quietly
    scoping the counts in the service.

    With more than one facility on record "the house" is the caller's facility
    (see the module docstring): the aggregates and the named list count only
    its patients, and the department filter below then applies inside it.
    """
    scope, multi, options = _facility_view(user, facility)
    if not multi:
        snapshot = command_center_service.house_snapshot()
    elif scope is None:
        snapshot = command_center_service.house_snapshot(None, group_tiles=True)
    else:
        snapshot = command_center_service.house_snapshot(scope)
    if multi:
        # Which facility the figures are about (None for the group view, and
        # for an account whose department no facility lists, which sees none).
        one = next(iter(scope)) if scope is not None and len(scope) == 1 else None
        snapshot["facility"] = facility_service.facility_identity(one) if one else None
        snapshot["facility_view"] = (
            "group" if scope is None else ("facility" if scope else "no_facility")
        )
        snapshot["facilities"] = options

    dept = department_scope(user)
    if dept is not None:
        rows = snapshot.get("deteriorations") or []
        # The house-wide COUNT names nobody, like the acuity totals beside it,
        # and it is what stops an empty scoped list reading as a quiet house.
        snapshot["deteriorations_house_count"] = len(rows)
        if rows:
            sql, params = scope_patient_ids_sql(dept, facilities=scope if multi else None)
            with db_session() as db:
                in_scope = {r["id"] for r in db.execute(sql, params).fetchall()}
            snapshot["deteriorations"] = [r for r in rows if r.get("patient_id") in in_scope]
        # State the boundary in the payload rather than leaving the reader to
        # infer from a short list that the hospital is quiet.
        snapshot["deteriorations_scope"] = dept

    if snapshot.get("deteriorations"):
        metadata: dict[str, Any] = {
            "result_count": len(snapshot["deteriorations"]),
            "patient_ids": [r.get("patient_id") for r in snapshot["deteriorations"]],
        }
        if multi:
            metadata["facility_scope"] = None if scope is None else sorted(scope)
        log_phi_read(
            request=request,
            user=user,
            action_type="command_center_viewed",
            summary_message=(
                f"Command-center deterioration list viewed "
                f"({len(snapshot['deteriorations'])} patients) for "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata=metadata,
        )

    return snapshot
