"""Laboratory orders: endpoints under /api/lab.

Empty until lis-6, lis-7 (W3-D, W4-D). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  POST /orders/{order_id}/cancel             order.write with patient scope, or lab.receive; reason required; served while draining
  GET  /exceptions                           lab.read

Order intake runs from orders_service on activation, not from a route here.

Built in lis-6 (W3-D): GET /exceptions, the laboratory's open exceptions list
(an order whose code maps to nothing the laboratory collects, an intake that
failed and waits for the automatic retry). It is a laboratory-facing list:
hospital-wide for an account with no department on record (how the lab roles
are seeded, ruling LIS-R12), and ward-scoped for an account that has one: a
department account sees only rows whose patient is in its department, by the
same predicate as patient_in_scope, and a row with no derivable department is
hidden from it (fail-closed). Each read is audited as phi_access through
log_phi_read (fail-closed), because a row names the patient, and the audit
records the patients actually returned. Served while the module drains, so the
laboratory can still see what is left.
The cancel route is lis-7 (W4-D).
"""
from fastapi import APIRouter, Depends, Query, Request

from database import db_session
from permissions import LAB_READ, department_scope, log_phi_read, require_permission
from routes.opt_in_gates import require_lab_module
from services import lab_order_service

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])

_KIND_TEXT = {
    "unmapped_code": "Code not in the laboratory catalogue",
    "intake_failed": "Not taken into the laboratory yet",
    "order_state_conflict": "Order changed outside the laboratory",
    "rejected_no_decision": "Rejected specimen waiting for a decision",
}


@router.get("/exceptions")
def list_lab_exceptions(request: Request, limit: int = Query(200, ge=1, le=500),
                        user: dict = Depends(require_permission(LAB_READ))):
    """Open laboratory exceptions, oldest first, with the order and patient
    each concerns. A department account sees only its own department's
    patients (department_scope, never a request parameter). The audit row is
    written from the rows after that filter: it records the patients actually
    returned (their count, and the first 200 ids, which keeps the row under
    the audit metadata size cap)."""
    dept = department_scope(user)
    with db_session() as db:
        rows = lab_order_service.list_open_exceptions(db, dept=dept, limit=limit)
    for row in rows:
        row["kind_label"] = _KIND_TEXT.get(row.get("kind"), "Exception")
    patient_ids = sorted({r["patient_id"] for r in rows if r.get("patient_id")})
    log_phi_read(
        request=request, user=user,
        action_type="lab_exceptions_viewed",
        summary_message=f"Laboratory exceptions list viewed ({len(rows)} open)",
        target_type="lab_exceptions",
        target_id="open",
        metadata={"count": len(rows), "patient_count": len(patient_ids),
                  "patient_ids": patient_ids[:200]},
    )
    return {"exceptions": rows}
