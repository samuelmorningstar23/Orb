"""Laboratory orders: endpoints under /api/lab.

Empty until lis-6, lis-7 (W3-D, W4-D). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  POST /orders/{order_id}/cancel             order.write with patient scope, or lab.receive; reason required; served while draining
  GET  /exceptions                           lab.read

Order intake runs from orders_service on activation, not from a route here.
"""
from fastapi import APIRouter, Depends

from routes.opt_in_gates import require_lab_module

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])
