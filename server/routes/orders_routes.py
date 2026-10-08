"""Orb Hospital OS — Orders / CPOE-lite API Routes

Guarded provider order entry with terminology validation. Auth follows the
existing Bearer/JWT pattern via Depends(get_current_user); the authenticated
user's id is used as the actor on creates and transitions.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from typing import Optional

from auth import get_current_user
from database import db_session
from permissions import (department_scope, enforce_patient_scope, log_phi_read,
                         patient_in_scope, require_patient_access,
                         require_permission, ORDER_READ, ORDER_WRITE)
from services import order_sets_service, orders_service, terminology_service
from services.medication_safety_gate import enforce_medication_safety
from services.order_sets_service import OrderSetBlocked, OrderSetError
from services.drug_chart import DRUG_CHART_OFF, drug_chart_enabled, require_drug_chart

router = APIRouter(prefix="/api/orders", tags=["Orders / CPOE"])


def _refuse_medication_order(order: Optional[dict]) -> None:
    """404 "The drug chart is not part of Orb Ward Record" for a medication
    order on an Orb Ward Record computer, whose drug chart stays on paper
    (services/drug_chart). Every other order is left as it is."""
    if order and order.get("order_type") == "medication" and not drug_chart_enabled():
        raise HTTPException(status_code=404, detail=DRUG_CHART_OFF)


# ══════════════════════════════════════════════════════════════════════════
#  REQUEST MODELS
# ══════════════════════════════════════════════════════════════════════════

class CreateOrderRequest(BaseModel):
    patient_id: str
    order_type: str
    description: str
    code: Optional[str] = None
    code_system: Optional[str] = None
    priority: str = "routine"
    source: str = "clinician"
    detail: Optional[dict] = None
    safety_ack: Optional[dict] = None


class TransitionRequest(BaseModel):
    to_status: str
    detail: Optional[str] = None


class ResultRequest(BaseModel):
    result_text: str


class OrderSetItemSelection(BaseModel):
    """One clinician edit to one item of a set, before it becomes an order.

    Everything is optional: an item with no selection is ordered exactly as the
    protocol file states it. `include: false` drops the item. The edited values
    — not the file's — are what the safety gates screen.
    """
    index: int
    include: bool = True
    description: Optional[str] = None
    priority: Optional[str] = None
    dose: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    safety_ack: Optional[dict] = None


class OrderSetApplyRequest(BaseModel):
    patient_id: str
    items: Optional[list[OrderSetItemSelection]] = None


# ══════════════════════════════════════════════════════════════════════════
#  ORDER SETS / PROTOCOLS
#  (must precede /{order_id} so "sets" is not read as an order id)
# ══════════════════════════════════════════════════════════════════════════
# A set is a CONVENIENCE, never a bypass: every order it produces goes through
# the allergy interlock, the dose guard, the discharged-patient check and
# terminology validation, and the whole set is refused if any one item is
# stopped. The enforcement lives in services/order_sets_service.py; these
# handlers only carry the HTTP contract, the ward scope and the read audit.

def _selections(req: OrderSetApplyRequest) -> Optional[list[dict]]:
    return [s.model_dump() for s in req.items] if req.items else None


def _order_set_status(e: OrderSetError) -> int:
    """404 for "there is no such thing", 400 for "your request is wrong".

    The patient case is 404 for the reason the whole scoping law is: a 403 or a
    400 that distinguishes "this patient is not yours" from "no such patient"
    confirms the patient exists.
    """
    message = str(e)
    return 404 if ("not found" in message or "Unknown order set" in message) else 400


@router.get("/sets", dependencies=[Depends(require_drug_chart)])
def list_order_sets(user=Depends(require_permission(ORDER_READ))):
    """Every configured set. No patient, no PHI — the protocol list itself."""
    config = order_sets_service.get_config()
    return {
        "sets": order_sets_service.list_sets(),
        "config": {
            "path": config["path"],
            "config_version": config["config_version"],
            "fingerprint": config["fingerprint"],
            "editable_by": "an engineer, in the site configuration file, not through this API",
        },
    }


@router.get("/sets/{set_id}", dependencies=[Depends(require_drug_chart)])
def get_order_set(set_id: str, user=Depends(require_permission(ORDER_READ))):
    """One set in full, with the codes this deployment cannot resolve flagged."""
    try:
        s = order_sets_service.get_set(set_id)
        return {"set": s, "unknown_codes": order_sets_service.unknown_codes(set_id)}
    except OrderSetError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/sets/{set_id}/preview", dependencies=[Depends(require_drug_chart)])
def preview_order_set(
    set_id: str,
    req: OrderSetApplyRequest,
    request: Request,
    user=Depends(require_permission(ORDER_WRITE)),
):
    """What this set would order for this patient, and what would stop it.

    Writes no orders. Runs the identical gates apply runs, so preview and apply
    can never disagree about whether an item is safe.
    """
    # SCOPING LAW: the patient is named in the BODY, so the path-shaped
    # dependency cannot see it — enforce_patient_scope is the same derivation,
    # 404 not 403, and the PHI read is audited below.
    enforce_patient_scope(user, req.patient_id)
    try:
        result = order_sets_service.preview_set(
            req.patient_id, set_id, actor_id=user["id"], selections=_selections(req))
    except OrderSetError as e:
        raise HTTPException(status_code=_order_set_status(e), detail=str(e))
    log_phi_read(
        request=request, user=user, action_type="order_set_previewed",
        summary_message=(f"Previewed order set '{set_id}' for {req.patient_id}: "
                         f"{len(result['items'])} item(s), "
                         f"{len(result['blocked'])} blocked"),
        patient_id=req.patient_id, target_type="order_set", target_id=set_id,
        metadata={"set_id": set_id, "blocked": [b["description"] for b in result["blocked"]]},
    )
    return result


@router.post("/sets/{set_id}/apply", dependencies=[Depends(require_drug_chart)])
def apply_order_set(
    set_id: str,
    req: OrderSetApplyRequest,
    user=Depends(require_permission(ORDER_WRITE)),
):
    """Create the real, individual, DRAFT orders for a set. ALL OR NOTHING.

    428 — a severe allergy/interaction alert on at least one drug; re-submit
          with that item's `safety_ack` to override with a reason.
    422 — something harder: the dose guard stopped an item, or a screen could
          not run. Not acknowledgeable here; fix the protocol file or order the
          drug individually where the per-dose override machinery lives.
    """
    enforce_patient_scope(user, req.patient_id)
    try:
        result = order_sets_service.apply_set(
            req.patient_id, set_id,
            actor_id=user["id"], actor_role=user.get("role"),
            selections=_selections(req),
        )
    except OrderSetBlocked as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={
                "error": "order_set_blocked",
                "message": (
                    "This order set was NOT applied, no orders were created. A "
                    "safety check stopped at least one item, and a set is applied "
                    "whole or not at all."
                ),
                "blocked": e.blocked,
                "items": e.verdicts,
            },
        )
    except OrderSetError as e:
        raise HTTPException(status_code=_order_set_status(e), detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    for order in result["orders"]:
        _shadow_clinical_action(req.patient_id, order["order_type"], order, user)
    return {"status": "success", **result}


@router.get("/sets/applications/{patient_id}", dependencies=[Depends(require_drug_chart)])
def list_order_set_applications(
    patient_id: str,
    user=Depends(
        require_patient_access(ORDER_READ, audit_action="order_set_applications_viewed")
    ),
):
    """Protocols applied to this patient, newest first."""
    return {"applications": order_sets_service.list_applications(patient_id)}


# ══════════════════════════════════════════════════════════════════════════
#  TERMINOLOGY (must precede /{order_id} so the path isn't shadowed)
# ══════════════════════════════════════════════════════════════════════════

@router.get("/terminology/validate")
def validate_terminology(
    code_system: str = Query(...),
    code: str = Query(...),
    user=Depends(require_permission(ORDER_READ)),
):
    """Validate a code against the local terminology subset (for the create form)."""
    valid = terminology_service.validate_code(code_system, code)
    return {
        "valid": valid,
        "code_system": code_system.upper(),
        "code": code,
        "display": terminology_service.get_display(code_system, code) if valid else None,
    }


# ══════════════════════════════════════════════════════════════════════════
#  CRUD + STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════

def _structured_dose(detail: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    """(dose, route) from ``detail.medication``, the shape the order-set lane
    writes. (None, None) when the order carries no structured dose, which
    leaves the gate exactly as it was: allergy and interaction screen only."""
    medication = (detail or {}).get("medication") if isinstance(detail, dict) else None
    if not isinstance(medication, dict):
        return None, None

    def _text(value) -> Optional[str]:
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip()
        return text or None

    return _text(medication.get("dose")), _text(medication.get("route"))


@router.post("")
@router.post("/")
def create_order(req: CreateOrderRequest, user=Depends(require_permission(ORDER_WRITE))):
    """Create a new order (validates codes against terminology).

    Medication orders additionally pass the drug-safety gate (allergy +
    interaction screen) after terminology validation and before persisting:
    severe alerts 428 until re-submitted with an acknowledged safety_ack, and
    the screen/override result is returned as "safety". Non-medication orders
    are unaffected.
    """
    # SCOPING LAW — the patient is named in the BODY (2026-08-14).
    # This route held ORDER_WRITE and nothing else, so the permission answered
    # "may this account write orders?" and NOBODY answered "against THIS
    # patient?". A Cardiology nurse creating a nursing order on an Oncology
    # patient was PROBED on 2026-08-07: HTTP 200, row persisted, and from that
    # moment the order appears on the other ward's chart as a real instruction.
    # Of the eight bare caller-named mutations this is the CLINICAL one — the
    # others move money, images or audio; this one tells a nurse to give a drug.
    #
    # The two sibling routes overhead (preview/apply an order set) have carried
    # exactly this line since they were written; create_order — the route both
    # of them exist to be a convenience wrapper around — did not. Same
    # derivation, same 404-never-403 (a 403 confirms the patient exists), and
    # this is a `def` handler so the blocking check is already on FastAPI's
    # threadpool.
    # On an Orb Ward Record computer a medication order is refused before the
    # patient is read: the drug chart stays on paper (services/drug_chart).
    _refuse_medication_order({"order_type": req.order_type})
    enforce_patient_scope(user, req.patient_id)

    safety: Optional[dict] = None
    # The dose guard needs a dose. A CPOE description is free text, so the
    # guard only ran when a structured dose came with the order; without this
    # an 8 g paracetamol in detail.medication was screened for allergy and
    # nothing else, while the chart lane refused it. actor_role decides, in the
    # gate, who may clear a dose block (ruling R011), exactly as on the chart.
    dose, route = _structured_dose(req.detail)

    def _medication_safety_gate():
        nonlocal safety
        safety = enforce_medication_safety(
            req.patient_id,
            req.description,
            actor_id=user["id"],
            entry_point="orders.cpoe",
            safety_ack=req.safety_ack,
            dose=dose,
            route=route,
            actor_role=user.get("role"),
        )

    try:
        order = orders_service.create_order(
            patient_id=req.patient_id,
            order_type=req.order_type,
            description=req.description,
            code=req.code,
            code_system=req.code_system,
            priority=req.priority,
            ordered_by=user["id"],
            source=req.source,
            detail=req.detail,
            pre_persist_gate=_medication_safety_gate if req.order_type == "medication" else None,
        )
        _shadow_clinical_action(req.patient_id, req.order_type, order, user)
        response = {"status": "success", "order": order}
        if safety is not None:
            response["safety"] = safety
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  SHADOW INSTRUMENT — `clinical.action`
# ══════════════════════════════════════════════════════════════════════════════
# The clinician did the real thing, through CPOE, on their own. This is the third
# member of the scorecard's CLINICIAN_ACTION_KINDS and the only one that does NOT
# require the clinician to have used a Sage card: it is how "Sage proposed X and
# the doctor independently ordered X" becomes a matched pair, and equally how
# "Sage proposed X and the doctor ordered something else" becomes a disagreement.
# Without it, `agreement` could only ever measure clinicians who clicked Orb's
# own buttons — a population selected for agreeing with Orb.
#
# NOTE the deliberate asymmetry with the Sage lane: this DOES NOT open an
# episode. A routine order on a patient nothing is happening to is not evidence
# of anything, and minting an episode for it would fill the episode index with
# noise and hand the false-alert denominator a stream of alert-free episodes. If
# no episode is open within the correlation window, the order is simply not
# recorded here.
def _shadow_clinical_action(patient_id: str, order_type: str, order: dict, user: dict) -> None:
    """SYNC — `create_order` is a `def` handler, so this body is already on
    FastAPI's threadpool, never the event loop.

    Takes the patient/order-type as plain values rather than a CreateOrderRequest
    so an order-set item records identically to a hand-typed one: an order set is
    a clinician doing the real thing through CPOE, and excluding it here would
    quietly re-select the measured population for people who type orders one at
    a time.
    """
    try:
        from database import db_session
        from services import shadow_service
        with db_session() as db:
            episode_id = shadow_service.current_episode(patient_id, db=db)
            if not episode_id:
                return
            last_alert = db.execute(
                "SELECT occurred_at FROM shadow_events_current WHERE episode_id = ? "
                "AND kind = 'alert.fired' ORDER BY id DESC LIMIT 1",
                (episode_id,),
            ).fetchone()
            now = shadow_service.utc_now()
            minutes_after = None
            if last_alert is not None:
                from datetime import datetime
                try:
                    minutes_after = round(
                        (datetime.fromisoformat(now)
                         - datetime.fromisoformat(last_alert["occurred_at"])).total_seconds() / 60.0,
                        1)
                except (TypeError, ValueError):
                    minutes_after = None
            shadow_service.emit_many([shadow_service.build(
                "clinical.action", episode_id, "manual",
                occurred_at=now, patient_id=patient_id,
                actor_user_id=user.get("id"), actor_role=user.get("role"),
                subject_type="orders", subject_id=(order or {}).get("id"),
                payload={
                    # Medication orders are classed as `add_medication` so they
                    # compare directly against the class Sage proposes for a drug
                    # (`action.proposed.action_class`); anything else keeps its
                    # own order type rather than being flattened into one bucket.
                    "action_class": ("add_medication" if order_type == "medication"
                                     else f"order_{order_type}"),
                    "source_table": "orders",
                    "minutes_after_alert": minutes_after,
                },
            )], db=db)
    except Exception:
        # Rule 1: an order must never fail because the observational log did.
        pass


@router.get("/patient/{patient_id}")
def list_patient_orders(
    patient_id: str,
    status: Optional[str] = Query(None),
    user=Depends(
        require_patient_access(ORDER_READ, audit_action="patient_orders_viewed")
    ),
):
    """List a patient's orders, optionally filtered by status.

    Each order also says whether the laboratory manages it (lab_managed,
    lis-7) and, when it does, where each of its tests is (lab). A laboratory
    order the laboratory could not take in carries lab_intake instead, so the
    person who ordered it sees why. Read from the laboratory's tables whatever
    ORB_LAB says now: an order the laboratory took in stays its for life."""
    from services import lab_order_service

    orders = orders_service.list_orders(patient_id, status)
    if not drug_chart_enabled():
        # The medication orders are the drug chart, which an Orb Ward Record
        # computer keeps on paper; every other order is listed.
        orders = [o for o in orders if o.get("order_type") != "medication"]
    with db_session() as db:
        lab_order_service.annotate_orders(db, orders)
    return {"orders": orders}


@router.get("/{order_id}")
def get_order(order_id: str, request: Request, user=Depends(require_permission(ORDER_READ))):
    """One order. Ward-scoped and audited like every other chart read.

    The surrogate id hid the patient from the path guard, so any ORDER_READ
    holder could read any ward's order (and its result) by id, and the read
    left no trace.
    """
    order = _order_in_scope_or_404(order_id, user)
    log_phi_read(
        request=request, user=user, action_type="order_viewed",
        summary_message=f"Order {order_id} viewed for {order.get('patient_id')}",
        patient_id=order.get("patient_id"), target_type="order", target_id=order_id,
    )
    return {"order": order}


@router.get("/{order_id}/events")
def get_order_events(order_id: str, request: Request,
                     user=Depends(require_permission(ORDER_READ))):
    order = _order_in_scope_or_404(order_id, user)
    events = orders_service.get_order_events(order_id)
    log_phi_read(
        request=request, user=user, action_type="order_events_viewed",
        summary_message=f"Order {order_id} history viewed for {order.get('patient_id')}",
        patient_id=order.get("patient_id"), target_type="order", target_id=order_id,
        metadata={"event_count": len(events)},
    )
    return {"events": events}


def _order_in_scope_or_404(order_id: str, user: dict) -> dict:
    """The order, or 404 when it does not exist OR sits outside the caller's ward.

    The two answers are identical on purpose: a 403 for out-of-ward would
    confirm the order exists. Same "Order not found" as _scope_by_order below.
    """
    order = orders_service.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    _refuse_medication_order(order)
    dept = department_scope(user)
    if dept is not None:
        with db_session() as db:
            if not patient_in_scope(db, order.get("patient_id"), dept):
                raise HTTPException(status_code=404, detail="Order not found")
    return order


def _scope_by_order(order_id: str, user: dict) -> None:
    """Ward-scope a mutation that names its patient through a SURROGATE id.

    `/{order_id}/transition` and `/{order_id}/result` never mention a patient,
    so neither the path-shaped guard nor the caller-named-patient ratchet can
    see them — but an order BELONGS to a patient, and cancelling or resulting
    another ward's order is the same boundary crossing as creating one there.
    permissions.enforce_patient_scope's docstring already names the surrogate-id
    spelling as part of this defect class.

    Deliberately silent when the order does not exist: an unknown id keeps the
    service's existing 400, so this check adds a scope decision without also
    rewriting the not-found contract. Out of scope answers 404 ORDER not found —
    not "patient not found" — because to this caller the order is what does not
    exist, and naming the patient would confirm one is there.
    """
    existing = orders_service.get_order(order_id)
    if not existing:
        return
    _refuse_medication_order(existing)
    dept = department_scope(user)
    if dept is None:
        return
    with db_session() as db:
        if not patient_in_scope(db, existing.get("patient_id"), dept):
            raise HTTPException(status_code=404, detail="Order not found")


def _lab_managed_conflict(err: Exception) -> HTTPException:
    """The 409 for a manual move or free-text result on an order the
    laboratory manages (lis-7). The code lets a screen branch without reading
    the sentence."""
    return HTTPException(status_code=409, detail={
        "error": "order_managed_by_lab",
        "message": str(err) or orders_service.LAB_MANAGED_MESSAGE,
    })


@router.post("/{order_id}/transition")
def transition_order(order_id: str, req: TransitionRequest, user=Depends(require_permission(ORDER_WRITE))):
    """Guarded, idempotent state transition for an order."""
    _scope_by_order(order_id, user)
    try:
        order = orders_service.transition_order(
            order_id=order_id,
            to_status=req.to_status,
            actor_id=user["id"],
            detail=req.detail,
        )
        return {"status": "success", "order": order}
    except orders_service.OrderManagedByLab as e:
        # lis-7: the laboratory owns this order's lifecycle whatever ORB_LAB
        # says now. 409: the request is well-formed, the order refuses it.
        raise _lab_managed_conflict(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{order_id}/result")
def add_result(order_id: str, req: ResultRequest, user=Depends(require_permission(ORDER_WRITE))):
    """Attach a result and move a lab/imaging order to 'resulted'."""
    _scope_by_order(order_id, user)
    try:
        order = orders_service.add_result(
            order_id=order_id,
            result_text=req.result_text,
            actor_id=user["id"],
        )
        return {"status": "success", "order": order}
    except orders_service.OrderManagedByLab as e:
        raise _lab_managed_conflict(e)
    except orders_service.OrderAlreadyResulted as e:
        # 409, not 400: the request is well-formed, the ORDER is in a state that
        # refuses it. A stale tab showing '+ Result' lands here.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
