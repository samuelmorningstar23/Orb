"""Laboratory reports: endpoints under /api/lab.

Empty until lis-14 (W7-I). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /reports/{accession}                  lab.read
  GET  /reports/patient/{patient_id}         lab.read, or require_patient_access(order.read)
"""
from fastapi import APIRouter, Depends

from routes.opt_in_gates import require_lab_module

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])
