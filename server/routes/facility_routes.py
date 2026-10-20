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
  GET   /{id}/document-series       facility.read   a facility's number series
  PUT   /{id}/document-series/{code} facility.manage set up or edit one series

The two document-series routes (multisite-7a, W10-A) answer 404
feature_disabled while ORB_MULTI_SITE is off, as the design's series editor
needs the switch. A series carries no patient data, so they sit in this open
prefix too.

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

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from permissions import FACILITY_MANAGE, FACILITY_READ, require_permission
from pydantic import BaseModel, ConfigDict

from routes.opt_in_gates import feature_gate

router = APIRouter(prefix="/api/facilities", tags=["facilities"])

_MULTI_SITE = feature_gate("multi_site")


_FACILITIES_META_USER_DEFAULT = Depends(require_permission(FACILITY_READ))


@router.get("/meta")
def facilities_meta(user: dict = _FACILITIES_META_USER_DEFAULT):
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

    code: str | None = None
    name: str | None = None
    short_name: str | None = None
    legal_name: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    district: str | None = None
    state: str | None = None
    state_code: str | None = None
    pincode: str | None = None
    phone: str | None = None
    email: str | None = None
    gstin: str | None = None
    pan: str | None = None
    hfr_id: str | None = None
    abdm_hip_id: str | None = None
    nabh_accreditation_no: str | None = None
    nabh_valid_until: str | None = None
    clinical_establishment_reg_no: str | None = None
    timezone: str | None = None


class DeactivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


def _refused(exc) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.body())


def _switch_off() -> HTTPException:
    from routes.opt_in_gates import _feature_off

    return _feature_off("multi_site")


_LIST_FACILITIES_USER_DEFAULT = Depends(require_permission(FACILITY_READ))


@router.get("")
def list_facilities(user: dict = _LIST_FACILITIES_USER_DEFAULT):
    """Every facility, active or not, with its identity and whether this
    account may edit it. No patient data."""
    import config

    from services import facility_identity, facility_service

    return {
        "facilities": facility_identity.list_views(user),
        "multi_site": bool(config.feature_enabled("multi_site")),
        "multi_facility_data": facility_service.multi_facility_data(),
    }


_CREATE_CHECK_GATE_DEFAULT = Depends(_MULTI_SITE)
_CREATE_CHECK_USER_DEFAULT = Depends(require_permission(FACILITY_MANAGE))


@router.get("/create-check")
def create_check(_gate: None = _CREATE_CHECK_GATE_DEFAULT, user: dict = _CREATE_CHECK_USER_DEFAULT):
    """Why a facility cannot be added now: {allowed, reasons [{code, message}]}."""
    from services import facility_identity

    reasons = facility_identity.can_create_facility(user)
    return {"allowed": not reasons, "reasons": reasons}


_READ_FACILITY_USER_DEFAULT = Depends(require_permission(FACILITY_READ))


@router.get("/{facility_id}")
def read_facility(facility_id: int, user: dict = _READ_FACILITY_USER_DEFAULT):
    from services import facility_identity

    try:
        return facility_identity.get_view(facility_id, user)
    except facility_identity.FacilityIdentityError as exc:
        raise _refused(exc) from exc


_UPDATE_FACILITY_BODY_DEFAULT = Body(...)
_UPDATE_FACILITY_USER_DEFAULT = Depends(require_permission(FACILITY_MANAGE))


@router.patch("/{facility_id}")
def update_facility(
    facility_id: int,
    body: FacilityIdentityBody = _UPDATE_FACILITY_BODY_DEFAULT,
    user: dict = _UPDATE_FACILITY_USER_DEFAULT,
):
    """Edit a facility's identity. Facility 1 whatever ORB_MULTI_SITE says;
    any other facility only with the switch on. The account's own facility
    only, unless it holds group-wide access."""
    import config

    from services import facility_identity, facility_service

    if facility_id != facility_service.MAIN_FACILITY_ID and not config.feature_enabled(
        "multi_site"
    ):
        raise _switch_off()
    try:
        return facility_identity.update_facility(
            facility_id, body.model_dump(exclude_unset=True), user=user
        )
    except facility_identity.FacilityIdentityError as exc:
        raise _refused(exc) from exc


_CREATE_FACILITY_BODY_DEFAULT = Body(...)
_CREATE_FACILITY_GATE_DEFAULT = Depends(_MULTI_SITE)
_CREATE_FACILITY_USER_DEFAULT = Depends(require_permission(FACILITY_MANAGE))


@router.post("")
def create_facility(
    request: Request,
    body: FacilityIdentityBody = _CREATE_FACILITY_BODY_DEFAULT,
    _gate: None = _CREATE_FACILITY_GATE_DEFAULT,
    user: dict = _CREATE_FACILITY_USER_DEFAULT,
) -> dict:
    """Add a facility: 404 with the switch off, 409 with the reasons while the
    create gate refuses (always, while the facility fence holds '*'). The
    facility_created row carries the request's path, method, client address
    and user agent."""
    from services import facility_documents, facility_identity

    try:
        return facility_identity.create_facility(
            body.model_dump(exclude_unset=True),
            user=user,
            request_meta=facility_documents.request_meta(request),
        )
    except facility_identity.FacilityIdentityError as exc:
        raise _refused(exc) from exc


_DEACTIVATE_FACILITY_BODY_DEFAULT = Body(default=None)
_DEACTIVATE_FACILITY_GATE_DEFAULT = Depends(_MULTI_SITE)
_DEACTIVATE_FACILITY_USER_DEFAULT = Depends(require_permission(FACILITY_MANAGE))


@router.post("/{facility_id}/deactivate")
def deactivate_facility(
    facility_id: int,
    body: DeactivateBody | None = _DEACTIVATE_FACILITY_BODY_DEFAULT,
    _gate: None = _DEACTIVATE_FACILITY_GATE_DEFAULT,
    user: dict = _DEACTIVATE_FACILITY_USER_DEFAULT,
):
    """Close a facility: 404 with the switch off; facility 1 never; otherwise
    the create gate, a reason, and no open work there."""
    from services import facility_identity

    try:
        return facility_identity.deactivate_facility(
            facility_id, user=user, reason=body.reason if body else None
        )
    except facility_identity.FacilityIdentityError as exc:
        raise _refused(exc) from exc


class DocumentSeriesBody(BaseModel):
    """The editable parts of one number series. On an edit only the fields sent
    change; setting up a facility's own series needs a prefix."""

    model_config = ConfigDict(extra="forbid")

    prefix: str | None = None
    suffix: str | None = None
    pad_width: int | None = None
    is_configured: bool | None = None


_LIST_DOCUMENT_SERIES_GATE_DEFAULT = Depends(_MULTI_SITE)
_LIST_DOCUMENT_SERIES_USER_DEFAULT = Depends(require_permission(FACILITY_READ))


@router.get("/{facility_id}/document-series")
def list_document_series(
    facility_id: int,
    _gate: None = _LIST_DOCUMENT_SERIES_GATE_DEFAULT,
    user: dict = _LIST_DOCUMENT_SERIES_USER_DEFAULT,
):
    """A facility's number series and what it may still set up, with whether
    this account may edit them. No patient data."""
    from services import billing_masters, facility_identity

    try:
        out = billing_masters.list_document_series(facility_id)
    except billing_masters.SeriesEditError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.body()) from exc
    out["can_edit"] = facility_identity.can_edit(user, facility_id)
    return out


_SAVE_DOCUMENT_SERIES_BODY_DEFAULT = Body(...)
_SAVE_DOCUMENT_SERIES_GATE_DEFAULT = Depends(_MULTI_SITE)
_SAVE_DOCUMENT_SERIES_USER_DEFAULT = Depends(require_permission(FACILITY_MANAGE))


@router.put("/{facility_id}/document-series/{series_code}")
def save_document_series(
    facility_id: int,
    series_code: str,
    body: DocumentSeriesBody = _SAVE_DOCUMENT_SERIES_BODY_DEFAULT,
    _gate: None = _SAVE_DOCUMENT_SERIES_GATE_DEFAULT,
    user: dict = _SAVE_DOCUMENT_SERIES_USER_DEFAULT,
):
    """Set up or edit one series of one facility (multisite-7a). The account's
    own facility only, unless it holds group-wide access. A prefix that could
    repeat a number is refused with the reason (400 invalid_series)."""
    from services import billing_masters, facility_identity

    if not facility_identity.can_edit(user, facility_id):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_your_facility",
                "message": "Only an administrator of this facility can change its numbering.",
            },
        )
    try:
        return billing_masters.save_document_series(
            facility_id, series_code, body.model_dump(exclude_unset=True), actor=user
        )
    except billing_masters.SeriesEditError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.body()) from exc
