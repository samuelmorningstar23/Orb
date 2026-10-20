"""Orb Hospital OS — Executive Command Center API Routes

A single, house-wide operational snapshot for a CMO/COO: census + capacity,
acuity heatmap, active deteriorations, sepsis-bundle compliance, forecast and
downtime-readiness. Read-only aggregation over existing services.

Gated on `forecast.read` (clinicians + admin have it) — the same operational
read permission that guards the Forecast pillar.
"""

from database import db_session
from fastapi import APIRouter, Depends, Request
from permissions import (
    FORECAST_READ,
    department_scope,
    log_phi_read,
    require_permission,
    scope_patient_ids_sql,
)

from services import command_center_service

router = APIRouter(prefix="/api/command-center", tags=["Command Center"])


_GET_HOUSE_SNAPSHOT_USER_DEFAULT = Depends(require_permission(FORECAST_READ))


@router.get("")
@router.get("/")
def get_house_snapshot(
    request: Request,
    user=_GET_HOUSE_SNAPSHOT_USER_DEFAULT,
):
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
    """
    snapshot = command_center_service.house_snapshot()

    dept = department_scope(user)
    if dept is not None:
        rows = snapshot.get("deteriorations") or []
        # The house-wide COUNT names nobody, like the acuity totals beside it,
        # and it is what stops an empty scoped list reading as a quiet house.
        snapshot["deteriorations_house_count"] = len(rows)
        if rows:
            sql, params = scope_patient_ids_sql(dept)
            with db_session() as db:
                in_scope = {r["id"] for r in db.execute(sql, params).fetchall()}
            snapshot["deteriorations"] = [r for r in rows if r.get("patient_id") in in_scope]
        # State the boundary in the payload rather than leaving the reader to
        # infer from a short list that the hospital is quiet.
        snapshot["deteriorations_scope"] = dept

    if snapshot.get("deteriorations"):
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
            metadata={
                "result_count": len(snapshot["deteriorations"]),
                "patient_ids": [r.get("patient_id") for r in snapshot["deteriorations"]],
            },
        )

    return snapshot
