"""Results: endpoints under /api/lab.

Empty until lis-10, lis-11, lis-12, lis-13 (W5-D, W6-F, W7-H). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /worklists/{section_id}               lab.result
  POST /results, PATCH /results/{result_id}, POST /results/{result_id}/delta-ack, POST /results/tech-validate   lab.result
  POST /results/authorise                    lab.authorise, per specimen
  POST /results/{result_id}/amend, /withdraw   lab.authorise, reason required

Two different people enter and authorise while config.LAB_REQUIRE_DISTINCT_AUTHORISER is on (ruling LIS-R5, held). No autoverification (LIS-R8).
"""
from fastapi import APIRouter, Depends

from routes.opt_in_gates import require_lab_module

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])
