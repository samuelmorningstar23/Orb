"""Critical results: endpoints under /api/lab.

Empty until lis-15, lis-16, lis-22 (W7-H, W8-G). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_critical_surface: open while the laboratory is
on or draining, and while interfaced criticals are on without ORB_LAB.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /criticals                            lab.read for the laboratory view; lab.critical_ack, ward-scoped, for the ward card
  POST /criticals/{notification_id}/callback   lab.critical_log
  POST /criticals/{notification_id}/acknowledge   lab.critical_ack, patient scope, read-back

Go-live is blocked on ruling LIS-R20 (does this sit inside held R023?). A Relay post never closes a callback (LIS-R3).
"""
from fastapi import APIRouter, Depends

from routes.opt_in_gates import require_lab_critical_surface

router = APIRouter(dependencies=[Depends(require_lab_critical_surface)])
