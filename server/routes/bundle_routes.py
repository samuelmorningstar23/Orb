"""Orb — Care Bundle (Sepsis Six / deterioration) routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from permissions import (
    department_scope,
    enforce_patient_scope,
    require_patient_access,
    require_permission,
)
from pydantic import BaseModel, Field
from services.audit_service import log_audit_event
from services.sepsis_bundle_service import (
    BundleNotActive,
    cancel_bundle,
    complete_element,
    get_active_bundles,
    get_bundle,
    get_bundles_for_patient,
    start_bundle,
)

router = APIRouter(prefix="/api/bundles", tags=["care-bundles"])

# Starting, completing and cancelling a bundle are clinical acts on a patient,
# so they need a clinical WRITE. They were gated on `alert.read`, which the
# admin account (and with it billing and TPA) holds, and complete/cancel took a
# bare bundle id with no ward check at all. `vitals.write` is the bedside write
# the nurse and doctor hold and the administrator does not.
_BUNDLE_WRITE = "vitals.write"


def _bundle_in_scope(bundle_id: int, user: dict) -> dict:
    """The bundle, if it exists and its patient is on the caller's ward.
    404 for both, never 403: a 403 would confirm the bundle exists."""
    bundle = get_bundle(bundle_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="Care bundle not found")
    enforce_patient_scope(user, bundle["patient_id"])
    return bundle


_ACTIVE_BUNDLES_USER_DEFAULT = Depends(require_permission("vitals.read"))


@router.get("/active")
def active_bundles(user: dict = _ACTIVE_BUNDLES_USER_DEFAULT):
    # Ward-scoped from the authenticated user row, like every patient list.
    return {"bundles": get_active_bundles(department_scope(user))}


_PATIENT_BUNDLES_USER_DEFAULT = Depends(
    require_patient_access("vitals.read", audit_action="care_bundles_viewed")
)


@router.get("/patient/{patient_id}")
def patient_bundles(
    patient_id: str,
    active_only: bool = False,
    user: dict = _PATIENT_BUNDLES_USER_DEFAULT,
):
    return {"bundles": get_bundles_for_patient(patient_id, active_only)}


_START_USER_DEFAULT = Depends(require_patient_access(_BUNDLE_WRITE))


@router.post("/patient/{patient_id}/start")
def start(
    patient_id: str,
    data: dict | None = None,
    user: dict = _START_USER_DEFAULT,
):
    data = data or {}
    bundle = start_bundle(
        patient_id,
        bundle_type=data.get("bundle_type", "sepsis6"),
        triggered_by=user["id"],
        trigger_reason=data.get("reason", "Manual start"),
        source="clinician",
    )
    assert bundle is not None
    log_audit_event(
        correlation_id=f"bundle-start-{bundle['id']}",
        event_category="care_bundle",
        action_type="bundle_started",
        outcome="success",
        summary_message=f"{bundle['bundle_type']} bundle started for {patient_id}",
        actor_user_id=user["id"],
        patient_id=patient_id,
        target_type="care_bundle",
        target_id=str(bundle["id"]),
    )
    return bundle


_COMPLETE_USER_DEFAULT = Depends(require_permission(_BUNDLE_WRITE))


@router.post("/{bundle_id}/complete/{element}")
def complete(bundle_id: int, element: str, user: dict = _COMPLETE_USER_DEFAULT):
    _bundle_in_scope(bundle_id, user)
    try:
        bundle = complete_element(bundle_id, element, actor_id=user["id"])
    except BundleNotActive as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log_audit_event(
        correlation_id=f"bundle-elem-{bundle_id}",
        event_category="care_bundle",
        action_type="bundle_element_completed",
        outcome="success",
        summary_message=f"Bundle {bundle_id} element '{element}' completed",
        actor_user_id=user["id"],
        patient_id=bundle.get("patient_id"),
        target_type="care_bundle",
        target_id=str(bundle_id),
        metadata={"element": element},
    )
    return bundle


class CancelRequest(BaseModel):
    # Mandatory. Stopping a sepsis protocol mid-hour is a clinical decision, and
    # the audit row is the only record of why (the table has no column for it).
    reason: str = Field(min_length=1, max_length=500)


_CANCEL_USER_DEFAULT = Depends(require_permission(_BUNDLE_WRITE))


@router.post("/{bundle_id}/cancel")
def cancel(bundle_id: int, req: CancelRequest, user: dict = _CANCEL_USER_DEFAULT):
    reason = req.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A reason is required to cancel a care bundle.")
    bundle = _bundle_in_scope(bundle_id, user)
    if not cancel_bundle(bundle_id, actor_id=user["id"], reason=reason):
        raise HTTPException(status_code=409, detail="This care bundle is no longer active.")
    log_audit_event(
        correlation_id=f"bundle-cancel-{bundle_id}",
        event_category="care_bundle",
        action_type="bundle_cancelled",
        outcome="success",
        summary_message=f"Bundle {bundle_id} cancelled: {reason}",
        actor_user_id=user["id"],
        actor_role=user.get("role"),
        patient_id=bundle["patient_id"],
        target_type="care_bundle",
        target_id=str(bundle_id),
        metadata={"reason": reason, "bundle_type": bundle.get("bundle_type")},
    )
    return {"ok": True}
