"""Analysers: endpoints under /api/lab.

Empty until lis-23, lis-24, lis-25 (W5-F, W6-G). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  /analysers CRUD and credential rotation    lab.interface
  /analysers/{instrument_id}/map             lab.master
  POST /analysers/{instrument_id}/messages   instrument key, no staff session

The instrument intake route authenticates by X-Instrument-Id and X-Instrument-Key, not a session: put it on a second router in this file gated by require_lab_module(require_session=False) (strict, not draining), include that router from routes/lab/__init__.py, and list the route in tests/test_unauthenticated_surface.py PUBLIC_ROUTES with its reason.
"""
from fastapi import APIRouter, Depends

from routes.opt_in_gates import require_lab_module

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])
