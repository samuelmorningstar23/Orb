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

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from permissions import ADT_READ, BED_MASTER, require_permission
from services import bed_master_service as bm
from services.adt.core import AdtError, http_detail

router = APIRouter()


# ── Request bodies ───────────────────────────────────────────────────────────
# None of these carries a patient id, and none may ever grow one.

class WardIn(BaseModel):
    code: str
    department: str
    name: Optional[str] = None
    ward_type: str = "general"
    default_bed_class: Optional[str] = None
    cleaning_tracked: bool = True
    reason: Optional[str] = None


class WardPatch(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    department: Optional[str] = None
    ward_type: Optional[str] = None
    default_bed_class: Optional[str] = None
    cleaning_tracked: Optional[bool] = None
    is_active: Optional[bool] = None
    reason: Optional[str] = None


class RoomIn(BaseModel):
    ward_id: str
    code: str
    name: Optional[str] = None
    room_type: str = "bay"
    is_isolation: Optional[bool] = None
    reason: Optional[str] = None


class RoomPatch(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    room_type: Optional[str] = None
    is_isolation: Optional[bool] = None
    is_active: Optional[bool] = None
    reason: Optional[str] = None


class BedRange(BaseModel):
    prefix: str = ""
    start: int
    end: int
    pad: int = 0


class BedsIn(BaseModel):
    ward_id: str
    labels: Optional[list[str]] = None
    range: Optional[BedRange] = None
    room_id: Optional[str] = None
    bed_class: Optional[str] = None
    is_isolation_room: Optional[bool] = None
    notes: Optional[str] = None
    #: Link beds already typed into the housekeeping register on this ward with
    #: the same label, keeping their cleaning state, instead of refusing.
    adopt_existing: bool = False
    reason: Optional[str] = None


class BedPatch(BaseModel):
    bed_label: Optional[str] = None
    room_id: Optional[str] = None
    bed_class: Optional[str] = None
    is_isolation_room: Optional[bool] = None
    notes: Optional[str] = None
    reason: Optional[str] = None


class BedClassIn(BaseModel):
    code: str
    name: str
    bed_day_service_code: Optional[str] = None
    is_critical_care: bool = False
    counts_in_census: bool = True
    reason: Optional[str] = None


class BedClassPatch(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    bed_day_service_code: Optional[str] = None
    is_critical_care: Optional[bool] = None
    counts_in_census: Optional[bool] = None
    is_active: Optional[bool] = None
    reason: Optional[str] = None


class ReasonIn(BaseModel):
    reason: Optional[str] = None


class LinkIn(BaseModel):
    #: Only admissions on this ward.
    ward_id: Optional[str] = None
    #: Only these admissions; every matched admission in scope when omitted.
    admission_ids: Optional[list[str]] = None
    #: The bed manager has read that host-ward nurses will not open a
    #: boarder's chart (ruling ADT-S6 is not applied).
    boarder_acknowledged: bool = False
    #: Typed reason for linking a patient already lying in a bed housekeeping
    #: records out of service.
    out_of_service_reason: Optional[str] = None
    reason: Optional[str] = None


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
        raise _refused(err)
    except AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=http_detail(err))


# ── Read ─────────────────────────────────────────────────────────────────────

@router.get("/master")
def get_master(
    ward_id: Optional[str] = Query(None),
    _user: dict = Depends(require_permission(ADT_READ)),
):
    """Wards, rooms, beds and bed classes. Hospital structure only: no
    patient appears here, so there is no read audit."""
    return bm.list_master(ward_id=ward_id)


# ── Wards ────────────────────────────────────────────────────────────────────

@router.post("/master/wards", status_code=201)
def post_ward(body: WardIn, request: Request,
              user: dict = Depends(require_permission(BED_MASTER))):
    return _run(bm.save_ward, body.model_dump(), actor=user, request_meta=_meta(request))


@router.patch("/master/wards/{ward_id}")
def patch_ward(ward_id: str, body: WardPatch, request: Request,
               user: dict = Depends(require_permission(BED_MASTER))):
    return _run(bm.save_ward, body.model_dump(exclude_unset=True), actor=user,
                ward_id=ward_id, request_meta=_meta(request))


# ── Rooms ────────────────────────────────────────────────────────────────────

@router.post("/master/rooms", status_code=201)
def post_room(body: RoomIn, request: Request,
              user: dict = Depends(require_permission(BED_MASTER))):
    return _run(bm.save_room, body.model_dump(), actor=user, request_meta=_meta(request))


@router.patch("/master/rooms/{room_id}")
def patch_room(room_id: str, body: RoomPatch, request: Request,
               user: dict = Depends(require_permission(BED_MASTER))):
    return _run(bm.save_room, body.model_dump(exclude_unset=True), actor=user,
                room_id=room_id, request_meta=_meta(request))


# ── Beds ─────────────────────────────────────────────────────────────────────

@router.post("/master/beds", status_code=201)
def post_beds(body: BedsIn, request: Request,
              user: dict = Depends(require_permission(BED_MASTER))):
    data = body.model_dump()
    if body.range is None:
        data.pop("range", None)
    return _run(bm.register_beds, data, actor=user, request_meta=_meta(request))


@router.patch("/master/beds/{bed_id}")
def patch_bed(bed_id: str, body: BedPatch, request: Request,
              user: dict = Depends(require_permission(BED_MASTER))):
    return _run(bm.update_bed, bed_id, body.model_dump(exclude_unset=True), actor=user,
                request_meta=_meta(request))


# ── Bed classes ──────────────────────────────────────────────────────────────

@router.post("/master/bed-classes", status_code=201)
def post_bed_class(body: BedClassIn, request: Request,
                   user: dict = Depends(require_permission(BED_MASTER))):
    return _run(bm.save_bed_class, body.model_dump(), actor=user, request_meta=_meta(request))


@router.patch("/master/bed-classes/{code}")
def patch_bed_class(code: str, body: BedClassPatch, request: Request,
                    user: dict = Depends(require_permission(BED_MASTER))):
    return _run(bm.save_bed_class, body.model_dump(exclude_unset=True), actor=user,
                code=code, request_meta=_meta(request))


# ── Site configuration ───────────────────────────────────────────────────────

@router.post("/master/import-site-config")
def post_import_site_config(request: Request,
                            user: dict = Depends(require_permission(BED_MASTER))):
    return _run(bm.import_site_config_wards, actor=user, request_meta=_meta(request))


# ── Retire, reconciliation and register complete (adt-3) ─────────────────────

@router.post("/master/beds/{bed_id}/retire")
def post_retire_bed(bed_id: str, request: Request, body: Optional[ReasonIn] = None,
                    user: dict = Depends(require_permission(BED_MASTER))):
    """Retire a bed. Refused while a patient is in it or it is reserved."""
    return _run(bm.retire_bed, bed_id, (body.model_dump() if body else {}), actor=user,
                request_meta=_meta(request))


@router.post("/master/wards/{ward_id}/register-complete")
def post_register_complete(ward_id: str, request: Request, body: Optional[ReasonIn] = None,
                           user: dict = Depends(require_permission(BED_MASTER))):
    """Mark the ward's bed register complete (stamps ledger_complete_from).
    Refused while an admission on the ward is unlinked or a bed is doubled."""
    return _run(bm.mark_register_complete, ward_id, (body.model_dump() if body else {}),
                actor=user, request_meta=_meta(request))


@router.get("/master/reconciliation")
def get_reconciliation(request: Request, ward_id: Optional[str] = Query(None),
                       user: dict = Depends(require_permission(BED_MASTER))):
    """Active admissions sorted against the master. Audited as phi_access."""
    return _run(bm.reconciliation_report, actor=user, ward_id=ward_id,
                request_meta=_meta(request))


@router.post("/master/reconciliation/link")
def post_reconciliation_link(body: LinkIn, request: Request,
                             user: dict = Depends(require_permission(BED_MASTER))):
    """Link matched admissions to their master beds. Never a doubled bed."""
    return _run(bm.link_admissions, body.model_dump(), actor=user, request_meta=_meta(request))
