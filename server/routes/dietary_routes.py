"""Orb — Diet & Kitchen routes.

Diet orders per admitted patient, the ward view a nurse works from, ward-wise
meal plans, the kitchen count and the kitchen indent.

FIVE THINGS THIS FILE IS CAREFUL ABOUT
──────────────────────────────────────

1. IT TOUCHES NO CLINICAL WRITE PATH. Every write here lands in `diet_orders`,
   `meal_plans`, `meal_plan_lines`, `kitchen_indents` or `kitchen_indent_lines`.
   It READS `patients`, `admissions`, `users` and `patient_allergies`. No route
   in this file can refuse, delay or gate an admission, an order, a medication
   administration, a vitals entry or a discharge.

2. SYNC HANDLERS. Every handler is a plain `def`, so FastAPI runs it in the
   threadpool and the blocking SQLite work in dietary_service never touches the
   event loop. (tests/test_event_loop_discipline.py enforces the rule for
   `async def`; the honest way to satisfy it for a DB-heavy module is not to be
   async at all.)

3. THE MODULE FLAG IS A 404, NOT A 403. A site that runs its own kitchen system
   sets ORB_DIETARY=0 and does not have this desk; a 403 would say "you are not
   allowed", which is a different and wrong statement.

4. WHO MAY DO WHAT IS SPLIT THREE WAYS, and the split is the safety property:

     * WRITING A DIET ORDER needs `order.write` — doctors and nurses only. A
       kitchen or catering clerk (an `admin` account in Orb's role model) must
       never be able to change what a patient is allowed to eat. This is the same
       permission the clinical order routes use, because a diet IS an order.
     * READING THE WARD VIEW needs `order.read` (doctor, nurse, admin) and is
       ward-scoped and PHI-audited, because it carries names, beds and live
       allergies.
     * RUNNING THE KITCHEN — building a plan, locking it, raising and issuing an
       indent — needs `order.write` OR `admin.overview`, because both the ward
       and the catering desk legitimately do it, and they hold different
       accounts. That is an ANY-OF check, which `require_permission` (an ALL-OF
       factory) cannot express, so `_require_any` below implements it and audits
       its denials the same way.

5. PHI READS ARE SCOPED AND AUDITED. `GET /orders` takes `patient_id` on the
   QUERY STRING, so it calls `enforce_patient_scope` — the path-param guard
   cannot see that spelling and six routes were caught leaking through it. The
   ward view is filtered by `department_scope(user)`. Both write a fail-closed
   `phi_access` audit row: a read that cannot be audited is a read that does not
   happen. The KITCHEN board is the deliberate exception — it carries counts and
   ward labels and not one patient name, so there is no PHI to audit.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import config
from auth import get_current_user
from permissions import (
    ADMIN_OVERVIEW,
    ORDER_READ,
    ORDER_WRITE,
    department_scope,
    enforce_patient_scope,
    has_permission,
    require_permission,
)
from services.audit_service import log_audit_event
from services.hospital_time import hospital_now
from services.dietary_service import (
    ALLERGY_SOURCE_TABLE,
    ALLERGY_UNAVAILABLE_MESSAGE,
    DIET_TYPE_LABELS,
    DIET_TYPES,
    KitchenIndentError,
    MEAL_SERVICE_LABELS,
    MEAL_SERVICES,
    NPO,
    NPO_EXPIRY_NOTICE,
    NPO_LABEL,
    NPO_LONG_WINDOW_HOURS,
    NPO_MAX_WINDOW_HOURS,
    TEXTURE_LABELS,
    TEXTURES,
    DietaryError,
    DietOrderError,
    MealPlanError,
    build_meal_plan,
    cancel_kitchen_indent,
    dietary_summary,
    discontinue_diet,
    get_active_order,
    get_kitchen_indent,
    get_meal_plan,
    get_order,
    issue_kitchen_indent,
    kitchen_board,
    list_kitchen_indents,
    list_meal_plans,
    list_wards,
    lock_meal_plan,
    order_diet,
    patient_diet_history,
    raise_kitchen_indent,
    stale_orders,
    ward_view,
)

logger = logging.getLogger("orb.dietary.routes")

router = APIRouter(prefix="/api/dietary", tags=["dietary"])


# ── The module switch ────────────────────────────────────────────────────────

def require_module() -> None:
    """404 the whole desk when a deployment has hidden this module.

    Read from `config.DIETARY_ENABLED` at REQUEST time, not import time, so a
    test (or an ops toggle that reloads config) that patches the constant
    actually changes behaviour rather than being shadowed by a value captured
    when the module was first imported.
    """
    if not getattr(config, "DIETARY_ENABLED", True):
        raise HTTPException(
            status_code=404,
            detail={
                "error": "module_disabled",
                "message": (
                    "The diet & kitchen module is not enabled for this deployment."
                ),
            },
        )


_READ = require_permission(ORDER_READ)
_ORDER = require_permission(ORDER_WRITE)


def _require_any(*permissions: str):
    """Dependency: the caller must hold AT LEAST ONE of these permissions.

    `require_permission` is an ALL-OF factory and cannot express this. The
    kitchen desk genuinely has two kinds of user — the ward staff who hold
    `order.write` and the catering/administration account that holds
    `admin.overview` — and demanding both would lock one of them out of work they
    do every day, while inventing a new permission would mean editing
    permissions.py, which this module does not own.

    Denials are audited exactly as `require_permission` audits them, so an
    any-of check is not a quieter check. Plain `def`, so FastAPI threadpools the
    blocking audit write.
    """
    def _checker(request: Request, user: dict = Depends(get_current_user)) -> dict:
        role = user.get("role", "")
        if any(has_permission(role, perm) for perm in permissions):
            return user
        log_audit_event(
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
            metadata={"required_any_of": list(permissions)},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": (
                    "Running the kitchen desk needs either clinical order rights "
                    "or an administration account."
                ),
                "required_any_of": list(permissions),
            },
        )
    return _checker


_KITCHEN = _require_any(ORDER_WRITE, ADMIN_OVERVIEW)


def _audit(
    user: dict,
    action: str,
    *,
    summary: str,
    outcome: str = "success",
    patient_id: Optional[str] = None,
    target_id: Optional[str] = None,
    target_type: str = "diet_order",
    metadata: Optional[dict] = None,
    category: str = "clinical_support",
) -> None:
    """One audit line per action.

    `phi_access` (FAIL-CLOSED — the read is refused if it cannot be audited) for
    anything that returns a named patient's diet, bed or allergies. A diet order
    names the patient, their ward, their bed and what they are allergic to; that
    is PHI by any reading, and the audit middleware fires on writes only, so a
    read that leaves no trace is a read nobody can investigate.
    """
    log_audit_event(
        correlation_id=f"dietary-{action}-{target_id or patient_id or 'na'}",
        event_category=category,
        action_type=f"dietary_{action}",
        outcome=outcome,
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        metadata=metadata,
    )


def _strip_basis(plan: dict) -> dict:
    """Remove the plan's basis order ids before it goes over the wire.

    `meal_plans.basis_order_ids_json` records WHICH diet orders a count was built
    from. It is a forensic aid — the answer to "what was this count built from"
    after a discharged patient dropped out of it — and it stays in the database
    for that. It does not need to leave the server: an order id is a handle onto
    a patient, and the catering account that reads this payload has no use for
    one. Smallest disclosure that does the job.
    """
    plan.pop("basis_order_ids", None)
    plan.pop("basis_order_ids_json", None)
    return plan


def _handle(fn, *args, **kwargs):
    """Run a service call, turning its refusals into the right HTTP status.

    409 for a state conflict the caller's screen is simply stale about (a locked
    plan, a second active order, an indent already raised) — the panel shows the
    message verbatim, and every one of those messages says WHY rather than
    "conflict". 404 for something that is not there. 400 for input that cannot be
    true (an NPO in the past, an unknown diet type).
    """
    try:
        return fn(*args, **kwargs)
    except (MealPlanError, KitchenIndentError, DietOrderError) as exc:
        message = str(exc)
        if "not found" in message or message.startswith("unknown patient"):
            raise HTTPException(
                status_code=404, detail={"error": "not_found", "message": message}
            )
        if any(token in message for token in (
            "is locked", "already has", "already ", "reload and try again",
            "lock it first", "cannot be", "nothing to discontinue",
        )):
            raise HTTPException(
                status_code=409, detail={"error": "conflict", "message": message}
            )
        raise HTTPException(
            status_code=400, detail={"error": "invalid", "message": message}
        )
    except DietaryError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid", "message": str(exc)}
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Request bodies
# ══════════════════════════════════════════════════════════════════════════════

class DietOrderIn(BaseModel):
    patient_id: str
    diet_type: str
    texture: str = "regular"
    restriction_notes: Optional[str] = None
    notes: Optional[str] = None
    npo_reason: Optional[str] = None
    npo_until: Optional[str] = None


class DiscontinueIn(BaseModel):
    reason: Optional[str] = None


class MealPlanIn(BaseModel):
    ward: str
    meal_service: str
    service_date: Optional[str] = None
    notes: Optional[str] = None


class IndentIn(BaseModel):
    meal_plan_id: int = Field(..., ge=1)
    notes: Optional[str] = None


class CancelIn(BaseModel):
    reason: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
#  Reference / meta
# ══════════════════════════════════════════════════════════════════════════════

def _meal_service_at(hour: int) -> str:
    """The service a ward is counting for at this hospital-local hour.

    Breakfast until 10:00, lunch until 15:00, dinner after that. A desk that
    always opened on Lunch had a night nurse building tomorrow's count under the
    wrong service at 02:00.
    """
    if hour < 10:
        return "breakfast"
    if hour < 15:
        return "lunch"
    return "dinner"


def _capabilities(user: dict) -> dict:
    """What this caller may do here, from the SAME checks the write routes use.

    The panel hides a button the server would refuse, instead of letting a
    catering account fill in a diet order and meet a 403. Advisory only: every
    route still enforces its own permission.
    """
    role = user.get("role", "")
    write_orders = has_permission(role, ORDER_WRITE)
    return {
        "write_orders": write_orders,
        "run_kitchen": write_orders or has_permission(role, ADMIN_OVERVIEW),
    }


@router.get("/meta")
def meta(user: dict = Depends(_READ), _m: None = Depends(require_module)) -> dict:
    """Everything the panel needs to render its forms without hard-coding a
    vocabulary that could drift from the server's CHECK constraints, plus the
    hospital's own day and current meal service (the browser's clock is not the
    hospital's) and what this caller may do."""
    now = hospital_now()
    return {
        "service_date": now.date().isoformat(),
        "current_meal_service": _meal_service_at(now.hour),
        "capabilities": _capabilities(user),
        "diet_types": list(DIET_TYPES),
        "diet_type_labels": DIET_TYPE_LABELS,
        "textures": list(TEXTURES),
        "texture_labels": TEXTURE_LABELS,
        "meal_services": list(MEAL_SERVICES),
        "meal_service_labels": MEAL_SERVICE_LABELS,
        "npo_diet_type": NPO,
        "npo_label": NPO_LABEL,
        "npo_expiry_notice": NPO_EXPIRY_NOTICE,
        "npo_max_window_hours": NPO_MAX_WINDOW_HOURS,
        "npo_long_window_hours": NPO_LONG_WINDOW_HOURS,
        "allergy_source": ALLERGY_SOURCE_TABLE,
        "allergy_unavailable_message": ALLERGY_UNAVAILABLE_MESSAGE,
        "allergies_are_read_not_copied": True,
    }


@router.get("/summary")
def summary(user: dict = Depends(_READ), _m: None = Depends(require_module)) -> dict:
    """Overview tiles. Aggregate counts within the caller's ward scope, no
    patient identifiers, so no PHI audit row.

    The scope travels with the counts. The tiles are ward-scoped while the
    kitchen tab beside them is hospital-wide, and nothing on screen said which
    was which; `scope` lets the panel caption the tiles ("Your wards: GM-1") and
    explain a department with nobody admitted instead of showing bare zeros.
    """
    dept = department_scope(user)
    body = dietary_summary(department=dept)
    body["scope"] = {
        "department": dept,
        "hospital_wide": dept is None,
        "wards": [w["ward"] for w in list_wards(department=dept)],
    }
    return body


@router.get("/wards")
def wards(user: dict = Depends(_READ), _m: None = Depends(require_module)) -> dict:
    return {"wards": list_wards(department=department_scope(user))}


# ══════════════════════════════════════════════════════════════════════════════
#  The ward view
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/ward")
def ward_diets(
    ward: Optional[str] = Query(None),
    user: dict = Depends(_READ),
    _m: None = Depends(require_module),
) -> dict:
    """The nurse's screen: every diet order on this ward, NPO called out
    separately, and the admitted patients nobody has written an order for.

    WARD SCOPE. `department_scope(user)` is derived from the AUTHENTICATED user
    and is never taken from the request — a Cardiology nurse naming
    `?ward=Oncology` gets Oncology beds only if Oncology is their department.
    This is the same derivation `permissions.patient_in_scope` uses, applied to
    the same latest-admission row.
    """
    dept = department_scope(user)
    view = ward_view(ward=ward, department=dept)
    # FAIL-CLOSED: names, beds and allergies are going over the wire.
    _audit(
        user, "ward_view_read",
        category="phi_access",
        summary=f"Diet ward view read for {ward or 'all wards'} ({view['order_count']} orders)",
        target_type="ward",
        target_id=ward,
        metadata={
            "ward": ward,
            "department": dept,
            "order_count": view["order_count"],
            "npo_count": view["npo_count"],
        },
    )
    return view


@router.get("/stale")
def stale(user: dict = Depends(_READ), _m: None = Depends(require_module)) -> dict:
    """Active diet orders whose patient is no longer admitted here.

    The VISIBLE half of "a discharged patient leaves the kitchen count". The
    count drops them automatically; this is where a dietitian sees that it
    happened and closes the order, rather than the row quietly vanishing.
    """
    dept = department_scope(user)
    rows = stale_orders(department=dept)
    _audit(
        user, "stale_orders_read",
        category="phi_access",
        summary=f"Lapsed diet orders read ({len(rows)})",
        target_type="ward",
        metadata={"department": dept, "count": len(rows)},
    )
    return {"orders": rows, "count": len(rows)}


# ══════════════════════════════════════════════════════════════════════════════
#  Diet orders
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/orders")
def orders(
    patient_id: str = Query(...),
    history: bool = Query(False),
    user: dict = Depends(_READ),
    _m: None = Depends(require_module),
) -> dict:
    """One patient's current diet order, and optionally their history.

    PHI SCOPE. `patient_id` arrives on the QUERY STRING, so
    `require_patient_access` — which resolves it from the PATH — cannot guard
    this route and the path-param drift guard greps straight past it. A diet
    order names the patient's ward, bed and live allergies, so an unscoped read
    is the same disclosure as a chart read. 404, never 403: a 403 confirms the
    patient exists. Plain `def` route, so FastAPI threadpools this blocking
    check.
    """
    enforce_patient_scope(user, patient_id)
    current = get_active_order(patient_id)
    payload: dict = {"order": current}
    if history:
        payload["history"] = patient_diet_history(patient_id)
    _audit(
        user, "order_read",
        category="phi_access",
        summary=f"Diet order read for {patient_id}",
        patient_id=patient_id,
        target_id=str(current["id"]) if current else None,
        metadata={"has_active_order": current is not None, "history": history},
    )
    return payload


@router.post("/orders")
def create_order(
    body: DietOrderIn,
    user: dict = Depends(_ORDER),
    _m: None = Depends(require_module),
) -> dict:
    """Write (or change) a patient's diet order.

    `order.write` — a diet IS a clinical order and only a clinician writes one.
    A catering account holding `admin.overview` can run the kitchen desk and
    cannot reach this route.
    """
    enforce_patient_scope(user, body.patient_id)
    row = _handle(order_diet, ordered_by=user.get("id") or "", **body.model_dump())
    _audit(
        user, "order_written",
        category="phi_access",
        summary=(
            f"Diet order for {body.patient_id}: {row['diet_label']}"
            + (f" until {row['npo_until_iso']}" if row["is_npo"] else "")
        ),
        patient_id=body.patient_id,
        target_id=str(row["id"]),
        metadata={
            "diet_type": row["diet_type"],
            "texture": row["texture"],
            "is_npo": row["is_npo"],
            "npo_until": row["npo_until_iso"],
            "supersedes_order_id": row["supersedes_order_id"],
        },
    )
    return {"order": row}


@router.post("/orders/{order_id}/discontinue")
def stop_order(
    order_id: int,
    body: DiscontinueIn,
    user: dict = Depends(_ORDER),
    _m: None = Depends(require_module),
) -> dict:
    # Scope FIRST, on the order's own patient. A ward check that runs after the
    # write has already happened is not a ward check — it is a log entry.
    existing = _handle(get_order, order_id)
    enforce_patient_scope(user, existing.get("patient_id"))
    row = _handle(
        discontinue_diet,
        order_id,
        discontinued_by=user.get("id") or "",
        reason=body.reason,
    )
    _audit(
        user, "order_discontinued",
        category="phi_access",
        summary=f"Diet order #{order_id} discontinued for {row['patient_id']}",
        patient_id=row.get("patient_id"),
        target_id=str(order_id),
        metadata={"reason": body.reason},
    )
    return {"order": row}


# ══════════════════════════════════════════════════════════════════════════════
#  Meal plans
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/meal-plans")
def meal_plans(
    service_date: Optional[str] = Query(None),
    ward: Optional[str] = Query(None),
    meal_service: Optional[str] = Query(None),
    user: dict = Depends(_READ),
    _m: None = Depends(require_module),
) -> dict:
    """Plans are COUNTS — ward label, diet type, integer. No patient identifiers
    cross this boundary, so no PHI audit row. (`basis_order_ids` is stripped
    below: it is an audit aid for the ward, not something a catering screen
    needs, and an order id is a handle onto a patient.)"""
    plans = _handle(
        list_meal_plans, service_date=service_date, ward=ward, meal_service=meal_service
    )
    return {"plans": [_strip_basis(p) for p in plans]}


@router.get("/meal-plans/{plan_id}")
def meal_plan(
    plan_id: int,
    user: dict = Depends(_READ),
    _m: None = Depends(require_module),
) -> dict:
    return {"plan": _strip_basis(_handle(get_meal_plan, plan_id))}


@router.post("/meal-plans")
def make_meal_plan(
    body: MealPlanIn,
    user: dict = Depends(_KITCHEN),
    _m: None = Depends(require_module),
) -> dict:
    """Roll this ward's live diet orders into a count for one meal service.

    Discharged patients are not in the count, and there is no sweep to run: the
    service re-derives who is still admitted on every build.
    """
    plan = _strip_basis(
        _handle(build_meal_plan, generated_by=user.get("id") or "", **body.model_dump())
    )
    _audit(
        user, "meal_plan_built",
        summary=(
            f"{plan['meal_service_label']} plan for {plan['ward']} on "
            f"{plan['service_date']}: {plan['total_meals']} meals, "
            f"{plan['npo_count']} nil by mouth"
        ),
        target_type="meal_plan",
        target_id=str(plan["id"]),
        metadata={
            "ward": plan["ward"],
            "meal_service": plan["meal_service"],
            "total_meals": plan["total_meals"],
            "npo_count": plan["npo_count"],
            "order_count": plan["order_count"],
        },
    )
    return {"plan": plan}


@router.post("/meal-plans/{plan_id}/lock")
def lock_plan(
    plan_id: int,
    user: dict = Depends(_KITCHEN),
    _m: None = Depends(require_module),
) -> dict:
    plan = _strip_basis(_handle(lock_meal_plan, plan_id, locked_by=user.get("id") or ""))
    _audit(
        user, "meal_plan_locked",
        summary=f"Meal plan #{plan_id} locked at {plan['total_meals']} meals",
        target_type="meal_plan",
        target_id=str(plan_id),
        metadata={"total_meals": plan["total_meals"]},
    )
    return {"plan": plan}


# ══════════════════════════════════════════════════════════════════════════════
#  Kitchen indents + the kitchen's own board
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/kitchen")
def kitchen(
    service_date: Optional[str] = Query(None),
    meal_service: Optional[str] = Query(None),
    user: dict = Depends(_READ),
    _m: None = Depends(require_module),
) -> dict:
    """The kitchen's screen. COUNTS AND WARD LABELS ONLY — not one patient name,
    MRN, bed or allergy. The kitchen does not need to know who is diabetic in
    order to cook fourteen diabetic trays, so there is no PHI here and therefore
    no PHI audit row."""
    return _handle(kitchen_board, service_date=service_date, meal_service=meal_service)


@router.get("/indents")
def indents(
    service_date: Optional[str] = Query(None),
    meal_service: Optional[str] = Query(None),
    ward: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    user: dict = Depends(_READ),
    _m: None = Depends(require_module),
) -> dict:
    return {
        "indents": _handle(
            list_kitchen_indents,
            service_date=service_date,
            meal_service=meal_service,
            ward=ward,
            status=status,
        )
    }


@router.get("/indents/{indent_id}")
def indent(
    indent_id: int,
    user: dict = Depends(_READ),
    _m: None = Depends(require_module),
) -> dict:
    return {"indent": _handle(get_kitchen_indent, indent_id)}


@router.post("/indents")
def create_indent(
    body: IndentIn,
    user: dict = Depends(_KITCHEN),
    _m: None = Depends(require_module),
) -> dict:
    row = _handle(
        raise_kitchen_indent,
        meal_plan_id=body.meal_plan_id,
        raised_by=user.get("id") or "",
        notes=body.notes,
    )
    _audit(
        user, "indent_raised",
        summary=(
            f"Kitchen indent raised for {row['ward']} "
            f"{row['meal_service_label'].lower()}: {row['total_meals']} meals"
        ),
        target_type="kitchen_indent",
        target_id=str(row["id"]),
        metadata={"meal_plan_id": body.meal_plan_id, "total_meals": row["total_meals"]},
    )
    return {"indent": row}


@router.post("/indents/{indent_id}/issue")
def issue_indent(
    indent_id: int,
    user: dict = Depends(_KITCHEN),
    _m: None = Depends(require_module),
) -> dict:
    row = _handle(issue_kitchen_indent, indent_id, issued_by=user.get("id") or "")
    _audit(
        user, "indent_issued",
        summary=f"Kitchen indent {row.get('indent_number') or indent_id} issued to the kitchen",
        target_type="kitchen_indent",
        target_id=str(indent_id),
        metadata={
            "indent_number": row.get("indent_number"),
            "total_meals": row["total_meals"],
        },
    )
    return {"indent": row}


@router.post("/indents/{indent_id}/cancel")
def cancel_indent(
    indent_id: int,
    body: CancelIn,
    user: dict = Depends(_KITCHEN),
    _m: None = Depends(require_module),
) -> dict:
    row = _handle(
        cancel_kitchen_indent,
        indent_id,
        cancelled_by=user.get("id") or "",
        reason=body.reason,
    )
    _audit(
        user, "indent_cancelled",
        summary=f"Kitchen indent {row.get('indent_number') or indent_id} cancelled",
        target_type="kitchen_indent",
        target_id=str(indent_id),
        metadata={"reason": body.reason},
    )
    return {"indent": row}
