"""Orb: /api/facilities, the facility identity routes (multi-site epic).

Registered in main.py by multisite-1 (W7-J, 9 October 2026) so the later
increments add routes here without touching main.py again.

  GET   /meta                       facility.read   the switch board (W7-J)
  GET   ""                          facility.read   every facility, with its identity
  GET   /{id}                       facility.read   one facility
  PATCH /{id}                       facility.manage edit identity (own facility)
  GET   /create-check               facility.manage why a facility cannot be added
  POST  ""                          facility.manage add a facility
  POST  /{id}/deactivate            facility.manage close a facility

multisite-2 (W9-E, 10 October 2026) added everything but /meta. Facility 1's
identity (GET and PATCH) works whatever ORB_MULTI_SITE says: W162 says the HFR
half needs no ruling, and invoices printed "Not configured" until it could be
entered. Editing any other facility, the create check, create and deactivate
answer 404 feature_disabled while the switch is off; with it on, create and
deactivate go through facility_identity.can_create_facility, which refuses
while the facility fence holds its sentinel '*', so no second facility can be
added through the product until the scope sweep is finished. A refusal
answers 409 with the reasons, for the Facilities screen to list.

Every identity edit is audited (category site_admin, action facility_updated,
metadata.changes = the field-by-field diff); a numbered invoice or receipt
keeps the supplier details it was issued with (migration 72).

/meta and /create-check are declared before "/{facility_id}" so neither is
ever read as a facility id (FastAPI 0.115 does not honour a path converter).
The whole /api/facilities prefix is open through the facility fence
(facility_fence.OPEN_SURFACES): facility identity carries no patient data and
is what an operator needs to see while the fence holds. Every route here must
stay free of patient data for that reason.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from permissions import FACILITY_MANAGE, FACILITY_READ, require_permission
from routes.opt_in_gates import feature_gate

router = APIRouter(prefix="/api/facilities", tags=["facilities"])

_MULTI_SITE = feature_gate("multi_site")


@router.get("/meta")
def facilities_meta(user: dict = Depends(require_permission(FACILITY_READ))):
    """The facility switch board for the Facilities screen. Plain def: it may
    read the facilities table, which must never happen on the event loop."""
    import config
    from services import facility_service

    multi = facility_service.multi_facility_data()
    rows = facility_service.list_facilities()
    main = next((r for r in rows if r.get("id") == facility_service.MAIN_FACILITY_ID), None)
    return {
        "module": "facilities",
        "multi_site": bool(config.feature_enabled("multi_site")),
        "multi_facility_data": multi,
        "facility_count": len(rows),
        "main_facility": {
            "id": facility_service.MAIN_FACILITY_ID,
            "code": (main or {}).get("code") or facility_service.MAIN_FACILITY_CODE,
            "name": facility_service.display_name(facility=main),
            "active": bool((main or {}).get("is_active", 0)),
        },
    }


class FacilityIdentityBody(BaseModel):
    """The identity fields. Each is optional; on PATCH only the fields sent
    change, and a blank or null value clears one. `code` is accepted on
    create only (a PATCH naming it is refused with code_fixed)."""
    model_config = ConfigDict(extra="forbid")

    code: Optional[str] = None
    name: Optional[str] = None
    short_name: Optional[str] = None
    legal_name: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    state: Optional[str] = None
    state_code: Optional[str] = None
    pincode: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    gstin: Optional[str] = None
    pan: Optional[str] = None
    hfr_id: Optional[str] = None
    abdm_hip_id: Optional[str] = None
    nabh_accreditation_no: Optional[str] = None
    nabh_valid_until: Optional[str] = None
    clinical_establishment_reg_no: Optional[str] = None
    timezone: Optional[str] = None


class DeactivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = None


def _refused(exc) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.body())


def _switch_off() -> HTTPException:
    from routes.opt_in_gates import _feature_off
    return _feature_off("multi_site")


@router.get("")
def list_facilities(user: dict = Depends(require_permission(FACILITY_READ))):
    """Every facility, active or not, with its identity and whether this
    account may edit it. No patient data."""
    import config
    from services import facility_identity, facility_service

    return {
        "facilities": facility_identity.list_views(user),
        "multi_site": bool(config.feature_enabled("multi_site")),
        "multi_facility_data": facility_service.multi_facility_data(),
    }


@router.get("/create-check")
def create_check(_gate: None = Depends(_MULTI_SITE),
                 user: dict = Depends(require_permission(FACILITY_MANAGE))):
    """Why a facility cannot be added now: {allowed, reasons [{code, message}]}."""
    from services import facility_identity

    reasons = facility_identity.can_create_facility(user)
    return {"allowed": not reasons, "reasons": reasons}


@router.get("/{facility_id}")
def read_facility(facility_id: int, user: dict = Depends(require_permission(FACILITY_READ))):
    from services import facility_identity

    try:
        return facility_identity.get_view(facility_id, user)
    except facility_identity.FacilityIdentityError as exc:
        raise _refused(exc) from exc


@router.patch("/{facility_id}")
def update_facility(
    facility_id: int,
    body: FacilityIdentityBody = Body(...),
    user: dict = Depends(require_permission(FACILITY_MANAGE)),
):
    """Edit a facility's identity. Facility 1 whatever ORB_MULTI_SITE says;
    any other facility only with the switch on. The account's own facility
    only, unless it holds group-wide access."""
    import config
    from services import facility_identity, facility_service

    if facility_id != facility_service.MAIN_FACILITY_ID and not config.feature_enabled("multi_site"):
        raise _switch_off()
    try:
        return facility_identity.update_facility(
            facility_id, body.model_dump(exclude_unset=True), user=user)
    except facility_identity.FacilityIdentityError as exc:
        raise _refused(exc) from exc


@router.post("")
def create_facility(
    body: FacilityIdentityBody = Body(...),
    _gate: None = Depends(_MULTI_SITE),
    user: dict = Depends(require_permission(FACILITY_MANAGE)),
):
    """Add a facility: 404 with the switch off, 409 with the reasons while the
    create gate refuses (always, while the facility fence holds '*')."""
    from services import facility_identity

    try:
        return facility_identity.create_facility(body.model_dump(exclude_unset=True), user=user)
    except facility_identity.FacilityIdentityError as exc:
        raise _refused(exc) from exc


@router.post("/{facility_id}/deactivate")
def deactivate_facility(
    facility_id: int,
    body: Optional[DeactivateBody] = Body(default=None),
    _gate: None = Depends(_MULTI_SITE),
    user: dict = Depends(require_permission(FACILITY_MANAGE)),
):
    """Close a facility: 404 with the switch off; facility 1 never; otherwise
    the create gate, a reason, and no open work there."""
    from services import facility_identity

    try:
        return facility_identity.deactivate_facility(
            facility_id, user=user, reason=body.reason if body else None)
    except facility_identity.FacilityIdentityError as exc:
        raise _refused(exc) from exc
