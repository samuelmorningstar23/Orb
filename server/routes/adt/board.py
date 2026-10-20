"""Bed board (ADT): endpoints under /api/adt.

The package router in routes/adt/__init__.py adds the /api/adt prefix and the
ORB_ADT gate; this router has neither, so every endpoint added here inherits
both (a caller with no session gets 401, and every route 404s while the module
is off).

Built in adt-4a (W4-B):
  GET  /board?ward_id=            adt.read  every bed on the caller's wards with its derived status
  GET  /awaiting-bed?ward_id=     adt.read  two lists: awaiting a bed, and bed text not linked
  GET  /beds/available?ward_id=   adt.read  beds a patient could be placed in now (no patient data)

Bed status is derived in the precedence the design states (occupied and
reserved above out of service, a bed vacated after its last clean needs
cleaning, 'Vacant, cleaning not tracked' where housekeeping does not track
the ward); services/bed_master_service.py holds the rules. A tile names its
occupant only inside the caller's department scope; any other occupied tile
says 'Occupied' or 'Boarder' with no patient field. A read that names anybody
writes one fail-closed phi_access audit with the patient ids; when that audit
cannot be written the read is refused (503), never served unaudited. A ward
the caller may not see is 404, never 403.

Every handler is a plain def (FastAPI threadpools it).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import ADT_READ, require_permission
from services.audit_service import AuditWriteError

from services import bed_master_service as bm

router = APIRouter()


def _meta(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _run(fn, *args, **kwargs):
    """Call a board read; its refusals become HTTP answers in words."""
    try:
        return fn(*args, **kwargs)
    except bm.BedMasterError as err:
        raise HTTPException(
            status_code=err.http_status,
            detail={"error": err.code, "message": str(err), **err.detail},
        ) from err
    except AuditWriteError:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "audit_unavailable",
                "message": "The record of who viewed these patients could not be written, so the "
                "list is not shown. Try again in a moment.",
            },
        ) from None


_GET_BOARD_WARD_ID_DEFAULT = Query(None)
_GET_BOARD_USER_DEFAULT = Depends(require_permission(ADT_READ))


@router.get("/board")
def get_board(
    request: Request,
    ward_id: str | None = _GET_BOARD_WARD_ID_DEFAULT,
    user: dict = _GET_BOARD_USER_DEFAULT,
):
    """The bed board for the caller's wards, or one of them."""
    return _run(bm.bed_board, actor=user, ward_id=ward_id or None, request_meta=_meta(request))


_GET_AWAITING_BED_WARD_ID_DEFAULT = Query(None)
_GET_AWAITING_BED_USER_DEFAULT = Depends(require_permission(ADT_READ))


@router.get("/awaiting-bed")
def get_awaiting_bed(
    request: Request,
    ward_id: str | None = _GET_AWAITING_BED_WARD_ID_DEFAULT,
    user: dict = _GET_AWAITING_BED_USER_DEFAULT,
):
    """Admissions in the caller's scope awaiting a bed, and those whose typed
    bed is not linked to bed setup, as two separate lists."""
    return _run(bm.awaiting_bed, actor=user, ward_id=ward_id or None, request_meta=_meta(request))


_GET_AVAILABLE_BEDS_WARD_ID_DEFAULT = Query(None)
_GET_AVAILABLE_BEDS_USER_DEFAULT = Depends(require_permission(ADT_READ))


@router.get("/beds/available")
def get_available_beds(
    ward_id: str | None = _GET_AVAILABLE_BEDS_WARD_ID_DEFAULT,
    user: dict = _GET_AVAILABLE_BEDS_USER_DEFAULT,
):
    """Beds free now, and beds waiting for or under a clean, for a picker."""
    return _run(bm.available_beds, actor=user, ward_id=ward_id or None)
