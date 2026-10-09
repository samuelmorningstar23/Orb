"""Orb: /api/facilities, the facility identity routes (multi-site epic).

Registered in main.py by multisite-1 (W7-J, 9 October 2026) so the later
increments add routes here without touching main.py again. This skeleton
serves one read, GET /api/facilities/meta: whether ORB_MULTI_SITE is on,
whether more than one facility is on record, how many there are, and facility
1's code and display name. Configuration only, no patient data.

What lands here later, from the design (docs/his_design/multisite.json):
  * multisite-2 (W9-E): GET "" and GET "/{id}" (facility.read) and PATCH
    "/{id}" (facility.manage, own facility), working for facility 1 with the
    switch off; POST "" and POST "/{id}/deactivate", 404 with the switch off
    and then the create gate (which refuses while facility_fence holds '*').
  * multisite-3 and 7a: departments and document series per facility.
Declare /meta before any "/{id}" route, or give the id an int converter, so
"meta" is never read as a facility id.

The whole /api/facilities prefix is open through the facility fence
(facility_fence.OPEN_SURFACES): facility identity carries no patient data and
is what an operator needs to see while the fence holds. Every route here must
stay free of patient data for that reason.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from permissions import FACILITY_READ, require_permission

router = APIRouter(prefix="/api/facilities", tags=["facilities"])


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
