"""Census (ADT): endpoints under /api/adt.

The package router in routes/adt/__init__.py adds the /api/adt prefix and the
ORB_ADT gate; this router has neither, so every endpoint added here inherits
both (a caller with no session gets 401, and every route 404s while the
module is off).

Built in adt-13a (W7-C):
  GET  /census?from=&to=&ward_id=     adt.read  midnight census, admissions,
                                                transfers, ends by type,
                                                inpatient days, licensed and
                                                available beds, occupancy,
                                                ALOS and return to ICU, per
                                                ward and hospital-local day
  GET  /census.csv?from=&to=&ward_id= adt.read  the same as a spreadsheet,
                                                with the method of every figure

Days are hospital-local (services.hospital_time); any day before a ward's
ledger_complete_from reads 'insufficient data', never zero
(services/adt_census_service.py holds every rule and its words). Counts only:
no patient is named, so nothing is read-audited. A caller with a department
sees that department's wards and no hospital totals; a ward the caller may not
see is 404, never 403. Still to come: adt-13b (NABH indicators, nabh_service).

Every handler is a plain def (FastAPI threadpools it).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from permissions import ADT_READ, require_permission

from services import adt_census_service as census_service

router = APIRouter()


def _read(user: dict, from_day: str | None, to_day: str | None, ward_id: str | None) -> dict:
    try:
        return census_service.census(
            actor=user, from_day=from_day, to_day=to_day, ward_id=ward_id or None
        )
    except census_service.CensusError as err:
        raise HTTPException(
            status_code=err.http_status,
            detail={"error": err.code, "message": str(err), **err.detail},
        ) from err


_GET_CENSUS_FROM_DAY_DEFAULT = Query(None, alias="from")
_GET_CENSUS_TO_DAY_DEFAULT = Query(None, alias="to")
_GET_CENSUS_WARD_ID_DEFAULT = Query(None)
_GET_CENSUS_USER_DEFAULT = Depends(require_permission(ADT_READ))


@router.get("/census")
def get_census(
    from_day: str | None = _GET_CENSUS_FROM_DAY_DEFAULT,
    to_day: str | None = _GET_CENSUS_TO_DAY_DEFAULT,
    ward_id: str | None = _GET_CENSUS_WARD_ID_DEFAULT,
    user: dict = _GET_CENSUS_USER_DEFAULT,
):
    """The census for the caller's wards (or one of them) over a period of
    whole hospital-local days, YYYY-MM-DD inclusive; the seven days ending
    yesterday when no dates are given. Hospital totals for an account with
    no department. Every answer carries the method of each figure."""
    return _read(user, from_day, to_day, ward_id)


_GET_CENSUS_CSV_FROM_DAY_DEFAULT = Query(None, alias="from")
_GET_CENSUS_CSV_TO_DAY_DEFAULT = Query(None, alias="to")
_GET_CENSUS_CSV_WARD_ID_DEFAULT = Query(None)
_GET_CENSUS_CSV_USER_DEFAULT = Depends(require_permission(ADT_READ))


@router.get("/census.csv")
def get_census_csv(
    from_day: str | None = _GET_CENSUS_CSV_FROM_DAY_DEFAULT,
    to_day: str | None = _GET_CENSUS_CSV_TO_DAY_DEFAULT,
    ward_id: str | None = _GET_CENSUS_CSV_WARD_ID_DEFAULT,
    user: dict = _GET_CENSUS_CSV_USER_DEFAULT,
):
    """The same census as CSV: one row per ward and day, the hospital's rows,
    then the method of every figure."""
    result = _read(user, from_day, to_day, ward_id)
    name = f"census-{result['from']}-to-{result['to']}.csv"
    return Response(
        content=census_service.census_csv(result),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )
