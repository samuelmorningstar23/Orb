"""Specimens: endpoints under /api/lab.

Empty until lis-8, lis-9 (W4-D, W5-D). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /collection, /collection/labels       lab.collect (ward-scoped for department accounts)
  POST /specimens/{specimen_id}/collect      lab.collect, two-identifier check; add require_lab_module() (new work)
  GET  /specimens/scan/{code}, /specimens/{specimen_id}
  POST /specimens/{specimen_id}/receive, /reject, /recollect   lab.receive

Back-dating a collection is refused until ruling LIS-R6 is confirmed (config.LAB_COLLECTION_BACKDATE_MINUTES is 0).
"""
from fastapi import APIRouter, Depends

from routes.opt_in_gates import require_lab_module

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])
