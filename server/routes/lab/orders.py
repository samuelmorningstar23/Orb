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
Built in lis-7 (W4-D): POST /orders/{order_id}/cancel, the one way to cancel
an order the laboratory manages (the Orders screen's own cancel answers 409
for it, orders_service.OrderManagedByLab). It needs order.write (the ward,
inside its own department) or lab.receive (the laboratory, hospital-wide when
the account has no department), a reason, and is served while the module
drains (finishing work includes cancelling it). Tests not yet released are
cancelled and their unreleased results withdrawn; released results stay; the
order becomes cancelled (never billed) or resulted. Audited as clinical_access
'lab_order_cancelled' in the same transaction.
"""

import uuid

from auth import get_current_user
from database import db_session, run_db
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import (
    LAB_READ,
    LAB_RECEIVE,
    ORDER_WRITE,
    department_scope,
    facility_scope,
    has_permission,
    log_phi_read,
    require_permission,
)
from pydantic import BaseModel, ConfigDict, Field
from services.audit_service import AuditWriteError, log_audit_event

from routes.opt_in_gates import require_lab_module
from services import lab_order_service

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])

_KIND_TEXT = {
    "unmapped_code": "Code not in the laboratory catalogue",
    "intake_failed": "Not taken into the laboratory yet",
    "order_state_conflict": "Order changed outside the laboratory",
    "rejected_no_decision": "Rejected specimen waiting for a decision",
}


_LIST_LAB_EXCEPTIONS_LIMIT_DEFAULT = Query(200, ge=1, le=500)
_LIST_LAB_EXCEPTIONS_USER_DEFAULT = Depends(require_permission(LAB_READ))


@router.get("/exceptions", response_model=None)
def list_lab_exceptions(
    request: Request,
    limit: int = _LIST_LAB_EXCEPTIONS_LIMIT_DEFAULT,
    user: dict = _LIST_LAB_EXCEPTIONS_USER_DEFAULT,
) -> dict:
    """Open laboratory exceptions, oldest first, with the order and patient
    each concerns. A department account sees only its own department's
    patients (department_scope, never a request parameter), and every
    account only its own facilities' rows (facility_scope, multisite-8h2).
    The audit row is written from the rows after that filter: it records the
    patients actually returned (their count, and the first 200 ids, which
    keeps the row under the audit metadata size cap)."""
    dept = department_scope(user)
    facilities = facility_scope(user)
    with db_session() as db:
        rows = lab_order_service.list_open_exceptions(
            db, dept=dept, limit=limit, facilities=facilities
        )
    for row in rows:
        row["kind_label"] = _KIND_TEXT.get((row.get("kind") or ""), "Exception")
    patient_ids = sorted({r["patient_id"] for r in rows if r.get("patient_id")})
    log_phi_read(
        request=request,
        user=user,
        action_type="lab_exceptions_viewed",
        summary_message=f"Laboratory exceptions list viewed ({len(rows)} open)",
        target_type="lab_exceptions",
        target_id="open",
        metadata={
            "count": len(rows),
            "patient_count": len(patient_ids),
            "patient_ids": patient_ids[:200],
        },
    )
    return {"exceptions": rows}


# ── lis-7: the laboratory-mediated cancel ────────────────────────────────────


def _require_any_permission(*permissions: str):
    """Like require_permission, but ANY one of `permissions` will do (the
    ward cancels with order.write, the laboratory with lab.receive). A denial
    is audited exactly as require_permission audits one."""

    _CHECKER_USER_DEFAULT = Depends(get_current_user)

    async def _checker(request: Request, user: dict = _CHECKER_USER_DEFAULT) -> dict:
        role = user.get("role", "")
        if any(has_permission(role, p) for p in permissions):
            return user
        await run_db(
            log_audit_event,
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="permission_denied",
            outcome="denied",
            summary_message=f"Missing any of: {', '.join(permissions)}",
            actor_user_id=user.get("id"),
            actor_role=role,
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={"required_any": list(permissions), "user_role": role},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "permission_denied",
                "message": "Insufficient permissions",
                "required_any": list(permissions),
            },
        )

    _checker.__name__ = "require_any_permission"
    return _checker


def request_meta(request: Request) -> dict:
    """What the audit row records about the request."""
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def lab_refusal(err: lab_order_service.LabOrderError) -> HTTPException:
    return HTTPException(
        status_code=err.status, detail={"error": err.code, "message": err.message, **err.extra}
    )


def audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": (
                "This could not be recorded because the audit trail is not available. "
                "Nothing was changed. Try again in a moment."
            ),
        },
    )


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., max_length=500)


_CANCEL_LAB_ORDER_USER_DEFAULT = Depends(_require_any_permission(ORDER_WRITE, LAB_RECEIVE))


@router.post("/orders/{order_id}/cancel", response_model=None)
def cancel_lab_order(
    order_id: str,
    body: CancelBody,
    request: Request,
    user: dict = _CANCEL_LAB_ORDER_USER_DEFAULT,
) -> dict:
    """Cancel an order the laboratory manages. The order must be inside the
    caller's department and facilities (department_scope and facility_scope,
    never a request parameter); out of scope and unknown are the same 404."""
    try:
        return lab_order_service.cancel_order(
            order_id,
            actor=user,
            reason=body.reason,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None
