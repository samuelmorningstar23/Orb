"""Laboratory catalogue and master: endpoints under /api/lab.

Empty until lis-3, lis-4 (W2-D, W3-D). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /catalogue?q=                         order.read or lab.read
  /master/tests, /panels, /ranges, /critical-limits, /specimen-types, /rejection-reasons   GET lab.read, writes lab.master

No reference range and no critical limit ships active (rulings LIS-R1, LIS-R2, held); the pathologist approves each. Writes audit as clinical_config.
"""
from fastapi import APIRouter, Depends

from routes.opt_in_gates import require_lab_module

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])
