"""Bed master (ADT): endpoints under /api/adt.

The package router in routes/adt/__init__.py adds the /api/adt prefix and the
ORB_ADT gate; this router has neither, so every endpoint added here inherits
both (a caller with no session gets 401, and every route 404s while the
module is off).

Built in adt-1 (W2-B):
  GET   /master                              adt.read    the whole master, no patient data
  POST  /master/wards                        bed.master  set up a ward
  PATCH /master/wards/{ward_id}              bed.master  change a ward
  POST  /master/rooms                        bed.master  set up a room on a ward
  PATCH /master/rooms/{room_id}              bed.master  change a room
  POST  /master/beds                         bed.master  register beds (a list or a range)
  PATCH /master/beds/{bed_id}                bed.master  change a bed
  POST  /master/bed-classes                  bed.master  set up a bed class
  PATCH /master/bed-classes/{code}           bed.master  change a bed class
  POST  /master/import-site-config           bed.master  wards from the site configuration

Built in adt-3 (W3-B):
  POST  /master/beds/{bed_id}/retire              bed.master  retire a bed nobody occupies
  POST  /master/wards/{ward_id}/register-complete bed.master  the ward's register is complete
  GET   /master/reconciliation                    bed.master  active admissions against the master
  POST  /master/reconciliation/link               bed.master  link matched admissions to their beds

Every handler is a plain def (FastAPI threadpools it). Master edits are
audited as category 'facilities' inside the write's own transaction, with a
bed_master_events row beside each; a ward code, room code or bed label is
immutable once anything refers to it. The reconciliation names admission and
patient ids (no name, no MRN): reading it is a fail-closed phi_access audit,
and each link is a fail-closed patient_movement audit written by the single
location writer (services.adt.core.locate_admission_on). A permission refusal
is a 403 with the denial audited by permissions.require_permission.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import ADT_READ, BED_MASTER, require_permission
from pydantic import BaseModel
from services.adt.core import AdtError, http_detail

from services import bed_master_service as bm

router = APIRouter()


# ── Request bodies ───────────────────────────────────────────────────────────
# None of these carries a patient id, and none may ever grow one.


class WardIn(BaseModel):
    code: str
    department: str
    name: str | None = None
    ward_type: str = "general"
    default_bed_class: str | None = None
    cleaning_tracked: bool = True
    reason: str | None = None


class WardPatch(BaseModel):
    code: str | None = None
    name: str | None = None
    department: str | None = None
    ward_type: str | None = None
    default_bed_class: str | None = None
    cleaning_tracked: bool | None = None
    is_active: bool | None = None
    reason: str | None = None


class RoomIn(BaseModel):
    ward_id: str
    code: str
    name: str | None = None
    room_type: str = "bay"
    is_isolation: bool | None = None
    reason: str | None = None


class RoomPatch(BaseModel):
    code: str | None = None
    name: str | None = None
    room_type: str | None = None
    is_isolation: bool | None = None
    is_active: bool | None = None
    reason: str | None = None


class BedRange(BaseModel):
    prefix: str = ""
    start: int
    end: int
    pad: int = 0


class BedsIn(BaseModel):
    ward_id: str
    labels: list[str] | None = None
    range: BedRange | None = None
    room_id: str | None = None
    bed_class: str | None = None
    is_isolation_room: bool | None = None
    notes: str | None = None
    #: Link beds already typed into the housekeeping register on this ward with
    #: the same label, keeping their cleaning state, instead of refusing.
    adopt_existing: bool = False
    reason: str | None = None


class BedPatch(BaseModel):
    bed_label: str | None = None
    room_id: str | None = None
    bed_class: str | None = None
    is_isolation_room: bool | None = None
    notes: str | None = None
    reason: str | None = None


class BedClassIn(BaseModel):
    code: str
    name: str
    bed_day_service_code: str | None = None
    is_critical_care: bool = False
    counts_in_census: bool = True
    reason: str | None = None


class BedClassPatch(BaseModel):
    code: str | None = None
    name: str | None = None
    bed_day_service_code: str | None = None
    is_critical_care: bool | None = None
    counts_in_census: bool | None = None
    is_active: bool | None = None
    reason: str | None = None


class ReasonIn(BaseModel):
    reason: str | None = None


class LinkIn(BaseModel):
    #: Only admissions on this ward.
    ward_id: str | None = None
    #: Only these admissions; every matched admission in scope when omitted.
    admission_ids: list[str] | None = None
    #: The bed manager has read that host-ward nurses will not open a
    #: boarder's chart (ruling ADT-S6 is not applied).
    boarder_acknowledged: bool = False
    #: Typed reason for linking a patient already lying in a bed housekeeping
    #: records out of service.
    out_of_service_reason: str | None = None
    reason: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _meta(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _refused(err: bm.BedMasterError) -> HTTPException:
    detail = {"error": err.code, "message": str(err)}
    detail.update({k: v for k, v in err.detail.items() if k not in detail})
    return HTTPException(status_code=err.http_status, detail=detail)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except bm.BedMasterError as err:
        raise _refused(err) from err
    except AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=http_detail(err)) from err


# ── Read ─────────────────────────────────────────────────────────────────────


_GET_MASTER_WARD_ID_DEFAULT = Query(None)
_GET_MASTER_USER_DEFAULT = Depends(require_permission(ADT_READ))


@router.get("/master", response_model=None)
def get_master(
    ward_id: str | None = _GET_MASTER_WARD_ID_DEFAULT,
    _user: dict = _GET_MASTER_USER_DEFAULT,
) -> dict:
    """Wards, rooms, beds and bed classes. Hospital structure only: no
    patient appears here, so there is no read audit. With more than one
    facility on record, the caller's facility's structure only
    (multisite-8h1)."""
    return bm.list_master(ward_id=ward_id, actor=_user)


# ── Wards ────────────────────────────────────────────────────────────────────


_POST_WARD_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.post("/master/wards", status_code=201)
def post_ward(body: WardIn, request: Request, user: dict = _POST_WARD_USER_DEFAULT):
    return _run(bm.save_ward, body.model_dump(), actor=user, request_meta=_meta(request))


_PATCH_WARD_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.patch("/master/wards/{ward_id}")
def patch_ward(
    ward_id: str,
    body: WardPatch,
    request: Request,
    user: dict = _PATCH_WARD_USER_DEFAULT,
):
    return _run(
        bm.save_ward,
        body.model_dump(exclude_unset=True),
        actor=user,
        ward_id=ward_id,
        request_meta=_meta(request),
    )


# ── Rooms ────────────────────────────────────────────────────────────────────


_POST_ROOM_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.post("/master/rooms", status_code=201)
def post_room(body: RoomIn, request: Request, user: dict = _POST_ROOM_USER_DEFAULT):
    return _run(bm.save_room, body.model_dump(), actor=user, request_meta=_meta(request))


_PATCH_ROOM_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.patch("/master/rooms/{room_id}")
def patch_room(
    room_id: str,
    body: RoomPatch,
    request: Request,
    user: dict = _PATCH_ROOM_USER_DEFAULT,
):
    return _run(
        bm.save_room,
        body.model_dump(exclude_unset=True),
        actor=user,
        room_id=room_id,
        request_meta=_meta(request),
    )


# ── Beds ─────────────────────────────────────────────────────────────────────


_POST_BEDS_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.post("/master/beds", status_code=201)
def post_beds(body: BedsIn, request: Request, user: dict = _POST_BEDS_USER_DEFAULT):
    data = body.model_dump()
    if body.range is None:
        data.pop("range", None)
    return _run(bm.register_beds, data, actor=user, request_meta=_meta(request))


_PATCH_BED_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.patch("/master/beds/{bed_id}")
def patch_bed(
    bed_id: str,
    body: BedPatch,
    request: Request,
    user: dict = _PATCH_BED_USER_DEFAULT,
):
    return _run(
        bm.update_bed,
        bed_id,
        body.model_dump(exclude_unset=True),
        actor=user,
        request_meta=_meta(request),
    )


# ── Bed classes ──────────────────────────────────────────────────────────────


_POST_BED_CLASS_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.post("/master/bed-classes", status_code=201)
def post_bed_class(body: BedClassIn, request: Request, user: dict = _POST_BED_CLASS_USER_DEFAULT):
    return _run(bm.save_bed_class, body.model_dump(), actor=user, request_meta=_meta(request))


_PATCH_BED_CLASS_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.patch("/master/bed-classes/{code}")
def patch_bed_class(
    code: str,
    body: BedClassPatch,
    request: Request,
    user: dict = _PATCH_BED_CLASS_USER_DEFAULT,
):
    return _run(
        bm.save_bed_class,
        body.model_dump(exclude_unset=True),
        actor=user,
        code=code,
        request_meta=_meta(request),
    )


# ── Site configuration ───────────────────────────────────────────────────────


_POST_IMPORT_SITE_CONFIG_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.post("/master/import-site-config")
def post_import_site_config(request: Request, user: dict = _POST_IMPORT_SITE_CONFIG_USER_DEFAULT):
    return _run(bm.import_site_config_wards, actor=user, request_meta=_meta(request))


# ── Retire, reconciliation and register complete (adt-3) ─────────────────────


_POST_RETIRE_BED_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.post("/master/beds/{bed_id}/retire")
def post_retire_bed(
    bed_id: str,
    request: Request,
    body: ReasonIn | None = None,
    user: dict = _POST_RETIRE_BED_USER_DEFAULT,
):
    """Retire a bed. Refused while a patient is in it or it is reserved."""
    return _run(
        bm.retire_bed,
        bed_id,
        (body.model_dump() if body else {}),
        actor=user,
        request_meta=_meta(request),
    )


_POST_REGISTER_COMPLETE_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.post("/master/wards/{ward_id}/register-complete")
def post_register_complete(
    ward_id: str,
    request: Request,
    body: ReasonIn | None = None,
    user: dict = _POST_REGISTER_COMPLETE_USER_DEFAULT,
):
    """Mark the ward's bed register complete (stamps ledger_complete_from).
    Refused while an admission on the ward is unlinked or a bed is doubled."""
    return _run(
        bm.mark_register_complete,
        ward_id,
        (body.model_dump() if body else {}),
        actor=user,
        request_meta=_meta(request),
    )


_GET_RECONCILIATION_WARD_ID_DEFAULT = Query(None)
_GET_RECONCILIATION_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.get("/master/reconciliation")
def get_reconciliation(
    request: Request,
    ward_id: str | None = _GET_RECONCILIATION_WARD_ID_DEFAULT,
    user: dict = _GET_RECONCILIATION_USER_DEFAULT,
):
    """Active admissions sorted against the master. Audited as phi_access."""
    return _run(bm.reconciliation_report, actor=user, ward_id=ward_id, request_meta=_meta(request))


_POST_RECONCILIATION_LINK_USER_DEFAULT = Depends(require_permission(BED_MASTER))


@router.post("/master/reconciliation/link")
def post_reconciliation_link(
    body: LinkIn, request: Request, user: dict = _POST_RECONCILIATION_LINK_USER_DEFAULT
):
    """Link matched admissions to their master beds. Never a doubled bed."""
    return _run(bm.link_admissions, body.model_dump(), actor=user, request_meta=_meta(request))
