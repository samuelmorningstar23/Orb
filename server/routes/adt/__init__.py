"""Beds and admissions (ADT): the /api/adt route package.

Created empty in wave 1 of the hospital-system build (3 October 2026) and
registered once in main.py, so each later ADT increment adds endpoints to its
own sub-module here instead of editing main.py or one shared route file.

THE WHOLE PACKAGE IS BEHIND ORB_ADT (default off). The gate is a router-level
dependency on this package router, so every endpoint any sub-module adds
inherits it and answers 404 while the module is off (after a 401 for a caller
with no session). tests/test_his_route_skeletons.py checks every /api/adt route
carries it.

Rules for every endpoint added here (design: docs/his_design/adt.json):
  * a plain `def` handler (FastAPI threadpools it; no sync SQLite on the loop);
  * permissions from permissions.py: adt.read, adt.bed, adt.transfer,
    adt.presence, bed.master, each move route also accepting patient.write;
  * scope through department_scope / patient_in_scope, and a scope failure is
    404, never 403;
  * writes run in db_session(immediate=True) with the audit written inside the
    same transaction (category patient_movement, fail-closed) and an
    Idempotency-Key header.

Sub-modules, one per concern, each exporting `router` (no prefix):
  master        bed master: wards, rooms, beds, bed classes, reconciliation
  board         bed board, awaiting bed and unlinked lists, available beds
  moves         assign, change bed, transfer, swap, correct a movement
  presence      leave, leave end, absconded, returned
  end_stay      end of stay and reopen
  reservations  bed reservations
  census        midnight census and its CSV
"""

import config
from fastapi import APIRouter, Depends
from permissions import ADT_READ, require_permission

from routes.adt import board, census, end_stay, master, moves, presence, reservations
from routes.opt_in_gates import require_adt_module

router = APIRouter(
    prefix="/api/adt",
    tags=["Beds and admissions"],
    dependencies=[Depends(require_adt_module)],
)


_ADT_META_USER_DEFAULT = Depends(require_permission(ADT_READ))


@router.get("/meta")
def adt_meta(user: dict = _ADT_META_USER_DEFAULT):
    """What this site's beds and admissions module is set to. No patient data."""
    return {
        "module": "adt",
        "state": config.module_state("adt"),
        "features": {"adt_strict_locations": config.feature_enabled("adt_strict_locations")},
        "settings": {
            "backtime_hours": config.ADT_BACKTIME_HOURS,
            "reservation_minutes": config.ADT_RESERVATION_MINUTES,
            "reservation_elective_minutes": config.ADT_RESERVATION_ELECTIVE_MINUTES,
            "absconded_close_hours": config.ADT_ABSCONDED_CLOSE_HOURS,
            "leave_prompt_hours": config.ADT_LEAVE_PROMPT_HOURS,
            "leave_rent_charged": config.ADT_LEAVE_RENT_CHARGED,
        },
    }


for _sub in (master, board, moves, presence, end_stay, reservations, census):
    router.include_router(_sub.router)
