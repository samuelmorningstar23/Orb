"""Orb Hospital OS — Orders / CPOE-lite API Routes

Guarded provider order entry with terminology validation. Auth follows the
existing Bearer/JWT pattern via Depends(get_current_user); the authenticated
user's id is used as the actor on creates and transitions.
"""

import uuid

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import (
    MEDICATION_PRESCRIBE,
    ORDER_READ,
    ORDER_WRITE,
    ROLE_PERMISSIONS,
    enforce_patient_scope,
    log_phi_read,
    require_patient_access,
    require_permission,
)
from pydantic import BaseModel
from services.audit_service import log_audit_event
from services.drug_chart import DRUG_CHART_OFF, drug_chart_enabled, require_drug_chart
from services.medication_safety_gate import enforce_medication_safety
from services.medorders import core as medorder_store
from services.medorders import cpoe
from services.order_sets_service import OrderSetBlocked, OrderSetError

from services import order_sets_service, orders_service, terminology_service

router = APIRouter(prefix="/api/orders", tags=["Orders / CPOE"])


def _refuse_medication_order(order: dict | None) -> None:
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
    code: str | None = None
    code_system: str | None = None
    priority: str = "routine"
    source: str = "clinician"
    detail: dict | None = None
    safety_ack: dict | None = None


class TransitionRequest(BaseModel):
    to_status: str
    detail: str | None = None


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
    description: str | None = None
    priority: str | None = None
    dose: str | None = None
    route: str | None = None
    frequency: str | None = None
    safety_ack: dict | None = None


class OrderSetApplyRequest(BaseModel):
    patient_id: str
    items: list[OrderSetItemSelection] | None = None


# ══════════════════════════════════════════════════════════════════════════
#  ORDER SETS / PROTOCOLS
#  (must precede /{order_id} so "sets" is not read as an order id)
# ══════════════════════════════════════════════════════════════════════════
# A set is a CONVENIENCE, never a bypass: every order it produces goes through
# the allergy interlock, the dose guard, the discharged-patient check and
# terminology validation, and the whole set is refused if any one item is
# stopped. The enforcement lives in services/order_sets_service.py; these
# handlers only carry the HTTP contract, the ward scope and the read audit.


def _selections(req: OrderSetApplyRequest) -> list[dict] | None:
    return [s.model_dump() for s in req.items] if req.items else None


def _order_set_status(e: OrderSetError) -> int:
    """404 for "there is no such thing", 400 for "your request is wrong".

    The patient case is 404 for the reason the whole scoping law is: a 403 or a
    400 that distinguishes "this patient is not yours" from "no such patient"
    confirms the patient exists.
    """
    message = str(e)
    return 404 if ("not found" in message or "Unknown order set" in message) else 400


_LIST_ORDER_SETS_USER_DEFAULT = Depends(require_permission(ORDER_READ))


@router.get("/sets", dependencies=[Depends(require_drug_chart)])
def list_order_sets(user=_LIST_ORDER_SETS_USER_DEFAULT):
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


_GET_ORDER_SET_USER_DEFAULT = Depends(require_permission(ORDER_READ))


@router.get("/sets/{set_id}", dependencies=[Depends(require_drug_chart)])
def get_order_set(set_id: str, user=_GET_ORDER_SET_USER_DEFAULT):
    """One set in full, with the codes this deployment cannot resolve flagged."""
    try:
        s = order_sets_service.get_set(set_id)
        return {"set": s, "unknown_codes": order_sets_service.unknown_codes(set_id)}
    except OrderSetError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


_PREVIEW_ORDER_SET_USER_DEFAULT = Depends(require_permission(ORDER_WRITE))


@router.post("/sets/{set_id}/preview", dependencies=[Depends(require_drug_chart)])
def preview_order_set(
    set_id: str,
    req: OrderSetApplyRequest,
    request: Request,
    user=_PREVIEW_ORDER_SET_USER_DEFAULT,
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
            req.patient_id, set_id, actor_id=user["id"], selections=_selections(req)
        )
    except OrderSetError as e:
        raise HTTPException(status_code=_order_set_status(e), detail=str(e)) from e
    log_phi_read(
        request=request,
        user=user,
        action_type="order_set_previewed",
        summary_message=(
            f"Previewed order set '{set_id}' for {req.patient_id}: "
            f"{len(result['items'])} item(s), "
            f"{len(result['blocked'])} blocked"
        ),
        patient_id=req.patient_id,
        target_type="order_set",
        target_id=set_id,
        metadata={"set_id": set_id, "blocked": [b["description"] for b in result["blocked"]]},
    )
    return result


_APPLY_ORDER_SET_USER_DEFAULT = Depends(require_permission(ORDER_WRITE))


@router.post("/sets/{set_id}/apply", dependencies=[Depends(require_drug_chart)])
def apply_order_set(
    set_id: str,
    req: OrderSetApplyRequest,
    user=_APPLY_ORDER_SET_USER_DEFAULT,
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
            req.patient_id,
            set_id,
            actor_id=user["id"],
            actor_role=user.get("role"),
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
        ) from e
    except OrderSetError as e:
        raise HTTPException(status_code=_order_set_status(e), detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    for order in result["orders"]:
        _shadow_clinical_action(req.patient_id, order["order_type"], order, user)
    return {"status": "success", **result}


_LIST_ORDER_SET_APPLICATIONS_USER_DEFAULT = Depends(
    require_patient_access(ORDER_READ, audit_action="order_set_applications_viewed")
)


@router.get("/sets/applications/{patient_id}", dependencies=[Depends(require_drug_chart)])
def list_order_set_applications(
    patient_id: str,
    user=_LIST_ORDER_SET_APPLICATIONS_USER_DEFAULT,
):
    """Protocols applied to this patient, newest first."""
    return {"applications": order_sets_service.list_applications(patient_id)}


# ══════════════════════════════════════════════════════════════════════════
#  TERMINOLOGY (must precede /{order_id} so the path isn't shadowed)
# ══════════════════════════════════════════════════════════════════════════


_VALIDATE_TERMINOLOGY_CODE_SYSTEM_DEFAULT = Query(...)
_VALIDATE_TERMINOLOGY_CODE_DEFAULT = Query(...)
_VALIDATE_TERMINOLOGY_USER_DEFAULT = Depends(require_permission(ORDER_READ))


@router.get("/terminology/validate")
def validate_terminology(
    code_system: str = _VALIDATE_TERMINOLOGY_CODE_SYSTEM_DEFAULT,
    code: str = _VALIDATE_TERMINOLOGY_CODE_DEFAULT,
    user=_VALIDATE_TERMINOLOGY_USER_DEFAULT,
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


def _structured_dose(detail: dict | None) -> tuple[str | None, str | None]:
    """(dose, route) from ``detail.medication``, the shape the order-set lane
    writes. (None, None) when the order carries no structured dose, which
    leaves the gate exactly as it was: allergy and interaction screen only."""
    medication = (detail or {}).get("medication") if isinstance(detail, dict) else None
    if not isinstance(medication, dict):
        return None, None

    def _text(value) -> str | None:
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip()
        return text or None

    return _text(medication.get("dose")), _text(medication.get("route"))


_CREATE_ORDER_USER_DEFAULT = Depends(require_permission(ORDER_WRITE))


@router.post("")
@router.post("/")
def create_order(req: CreateOrderRequest, request: Request, user=_CREATE_ORDER_USER_DEFAULT):
    """Create a new order (validates codes against terminology).

    Medication orders additionally pass the drug-safety gate (allergy +
    interaction screen) after terminology validation and before persisting:
    severe alerts 428 until re-submitted with an acknowledged safety_ack, and
    the screen/override result is returned as "safety". Non-medication orders
    are unaffected.

    With ORB_MED_ORDER_LIFECYCLE on, a medication order is also a draft
    medication order (medorders-7): see _create_managed_medication_order.
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
    if req.order_type == "medication" and cpoe.lifecycle_on():
        return _create_managed_medication_order(req, request, user)

    safety: dict | None = None
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
        raise HTTPException(status_code=400, detail=str(e)) from e


# ══════════════════════════════════════════════════════════════════════════════
#  MEDICATION ORDERS THE ORDER STORE MANAGES (medorders-7, lifecycle on)
# ══════════════════════════════════════════════════════════════════════════════


def _request_meta(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _needs(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"error": code, "message": message})


def _store_refusal(
    err: "medorder_store.MedOrderError", user: dict, request: Request, patient_id: str | None
) -> HTTPException:
    """A store refusal as HTTP (its own status and body). A 403 (not a
    prescriber) is audited as an authorization denial first, as the store's
    own routes do."""
    if isinstance(err, medorder_store.NotPermitted):
        order = err.order or {}
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="med_order_step_refused",
            outcome="denied",
            summary_message=f"Medication order step refused: {err.code}",
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=order["patient_id"] or patient_id,
            target_type="med_order",
            target_id=str(order["id"]) if order.get("id") else None,
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={
                "code": err.code,
                "user_role": user.get("role"),
                "entry_point": "orders.transition",
            },
        )
    return HTTPException(status_code=err.http_status, detail=medorder_store.http_detail(err))


def _push_med_order(med_order: dict | None) -> None:
    """After commit: the same 'medorders:update' the store's own routes push
    (ids, status and version only, never a drug name). Fire and forget."""
    if not med_order:
        return
    try:
        from ws.realtime import schedule_patient_vitals

        schedule_patient_vitals(
            "medorders:update",
            {
                "patient_id": med_order["patient_id"],
                "med_order_id": med_order.get("id"),
                "status": med_order.get("status"),
                "version": med_order.get("version"),
            },
        )
    except Exception:  # noqa: BLE001  (the write has committed; never fail it)
        pass


def _create_managed_medication_order(req: CreateOrderRequest, request: Request, user: dict) -> dict:
    """A CPOE medicine with the lifecycle on: the orders row AND a linked
    draft medication order, in one transaction. Nothing reaches the ward
    until a prescriber signs it (POST /api/medorders/orders/{id}/sign), which
    screens it again with the signer's role.

    The medicine is its fields, in detail.medication: drug_name, dose, route
    and frequency are required (422 drug_name_required,
    dose_route_and_frequency_required), so every draft can be signed and the
    dose guard runs now; is_prn and prn_indication are optional. The
    description must name the drug (422 description_does_not_name_the_drug):
    the list shows one and the ward receives the other. A code is optional
    (ruling MO-R11 (a): free text is allowed) and validated when given.

    The gate runs here as it always has for a CPOE medicine (allergy,
    interaction and dose guard, 428 to acknowledge), on the drug the ward will
    receive. No shadow clinical.action: the signature records it."""
    med = cpoe.structured_medication(req.detail)
    if not med["drug_name"]:
        raise _needs("drug_name_required", "Enter the medicine's name.")
    if not (med["dose"] and med["route"] and med["frequency"]):
        raise _needs(
            "dose_route_and_frequency_required",
            "Enter the dose, the route and how often it is given.",
        )
    if not cpoe.description_names_drug(req.description, med["drug_name"]):
        raise _needs(
            "description_does_not_name_the_drug",
            "The order's description must name the medicine being ordered.",
        )

    safety: dict | None = None

    def _medication_safety_gate():
        nonlocal safety
        safety = enforce_medication_safety(
            req.patient_id,
            med["drug_name"],
            actor_id=user["id"],
            entry_point=cpoe.ENTRY_CPOE_CREATE,
            safety_ack=req.safety_ack,
            dose=med["dose"],
            route=med["route"],
            actor_role=user.get("role"),
        )

    linked: dict = {}

    def _write_linked_draft(conn, order: dict) -> None:
        linked["med_order"] = cpoe.create_linked_draft_in_txn(
            conn,
            order,
            source="cpoe",
            drug_name=med["drug_name"],
            dose=med["dose"],
            route=med["route"],
            frequency=med["frequency"],
            is_prn=med["is_prn"],
            prn_indication=med["prn_indication"],
            actor_id=user["id"],
            actor_role=user.get("role"),
            entry_point=cpoe.ENTRY_CPOE_CREATE,
            lane=cpoe.LANE_CPOE,
            request_meta=_request_meta(request),
        ).order

    detail = {**(req.detail or {}), "medication": med}
    try:
        order = orders_service.create_order(
            patient_id=req.patient_id,
            order_type="medication",
            description=req.description,
            code=req.code,
            code_system=req.code_system,
            priority=req.priority,
            ordered_by=user["id"],
            source=req.source,
            detail=detail,
            status="draft",
            pre_persist_gate=_medication_safety_gate,
            in_transaction=_write_linked_draft,
            allow_uncoded_medication=True,
        )
    except medorder_store.MedOrderError as e:
        raise _store_refusal(e, user, request, req.patient_id) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    with db_session() as db:
        cpoe.annotate_orders(db, [order])
    _push_med_order(linked.get("med_order"))
    response = {"status": "success", "order": order}
    if safety is not None:
        response["safety"] = safety
    return response


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

    A medication order the order store manages (medorders-7, lifecycle on) is
    NOT recorded here: it reaches nobody until a prescriber signs it, so the
    store's sign step records it then (services/medorders/cpoe.after_signed),
    once, with the signer as the actor. The emitter itself is
    orders_service.record_clinical_action, shared by both, so the two record
    identically.
    """
    if (order or {}).get("managed_by_medication_order"):
        return
    orders_service.record_clinical_action(
        patient_id,
        order_type,
        (order or {}).get("id"),
        actor_id=user.get("id"),
        actor_role=user.get("role"),
    )


_LIST_PATIENT_ORDERS_STATUS_DEFAULT = Query(None)
_LIST_PATIENT_ORDERS_USER_DEFAULT = Depends(
    require_patient_access(ORDER_READ, audit_action="patient_orders_viewed")
)


@router.get("/patient/{patient_id}")
def list_patient_orders(
    patient_id: str,
    status: str | None = _LIST_PATIENT_ORDERS_STATUS_DEFAULT,
    user=_LIST_PATIENT_ORDERS_USER_DEFAULT,
):
    """List a patient's orders, optionally filtered by status.

    Each order also says whether the laboratory manages it (lab_managed,
    lis-7) and, when it does, where each of its tests is (lab). A laboratory
    order the laboratory could not take in carries lab_intake instead, so the
    person who ordered it sees why. Read from the laboratory's tables whatever
    ORB_LAB says now: an order the laboratory took in stays its for life.

    Each medication order also says whether the order store manages it
    (managed_by_medication_order, medorders-7) and, when it does, the
    medication order's state (medication_order), read from the link whatever
    the switches say now."""
    from services import lab_order_service

    orders = orders_service.list_orders(patient_id, status)
    if not drug_chart_enabled():
        # The medication orders are the drug chart, which an Orb Ward Record
        # computer keeps on paper; every other order is listed.
        orders = [o for o in orders if o.get("order_type") != "medication"]
    with db_session() as db:
        lab_order_service.annotate_orders(db, orders)
        cpoe.annotate_orders(db, orders)
    return {"orders": orders}


_GET_ORDER_USER_DEFAULT = Depends(require_permission(ORDER_READ))


@router.get("/{order_id}")
def get_order(order_id: str, request: Request, user=_GET_ORDER_USER_DEFAULT):
    """One order. Ward-scoped and audited like every other chart read.

    The surrogate id hid the patient from the path guard, so any ORDER_READ
    holder could read any ward's order (and its result) by id, and the read
    left no trace.
    """
    order = _order_in_scope_or_404(order_id, user)
    log_phi_read(
        request=request,
        user=user,
        action_type="order_viewed",
        summary_message=f"Order {order_id} viewed for {order.get('patient_id')}",
        patient_id=order["patient_id"],
        target_type="order",
        target_id=order_id,
    )
    with db_session() as db:
        cpoe.annotate_orders(db, [order])
    return {"order": order}


_GET_ORDER_EVENTS_USER_DEFAULT = Depends(require_permission(ORDER_READ))


@router.get("/{order_id}/events")
def get_order_events(order_id: str, request: Request, user=_GET_ORDER_EVENTS_USER_DEFAULT):
    order = _order_in_scope_or_404(order_id, user)
    events = orders_service.get_order_events(order_id)
    log_phi_read(
        request=request,
        user=user,
        action_type="order_events_viewed",
        summary_message=f"Order {order_id} history viewed for {order.get('patient_id')}",
        patient_id=order["patient_id"],
        target_type="order",
        target_id=order_id,
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
    _refuse_order_out_of_scope(user, order["patient_id"])
    return order


def _refuse_order_out_of_scope(user: dict, patient_id: str) -> None:
    """404 "Order not found" unless the order's patient is in the caller's scope.

    permissions.enforce_patient_scope answers the department half exactly as
    the inline check here did (no row for a department-only refusal) and, with
    more than one facility on record, checks the patient's facility first and
    writes the patient_scope_denied row a probe across a facility boundary
    always gets (multisite-8). Re-worded to this lane's own 404 so the refusal
    never says that a patient is there.
    """
    try:
        enforce_patient_scope(user, patient_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="Order not found") from None


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
    _refuse_order_out_of_scope(user, existing["patient_id"])


def _lab_managed_conflict(err: Exception) -> HTTPException:
    """The 409 for a manual move or free-text result on an order the
    laboratory manages (lis-7). The code lets a screen branch without reading
    the sentence."""
    return HTTPException(
        status_code=409,
        detail={
            "error": "order_managed_by_lab",
            "message": str(err) or orders_service.LAB_MANAGED_MESSAGE,
        },
    )


def _managed_by_medication_order_conflict(med_order: dict) -> HTTPException:
    """The 409 for a manual move of an order the medication-order store
    manages (medorders-7), naming the medication order so a screen can sign
    or refresh it."""
    return HTTPException(
        status_code=409,
        detail={
            "error": cpoe.MANAGED_CODE,
            "message": orders_service.MEDICATION_MANAGED_MESSAGE,
            "med_order_id": med_order.get("id"),
            "med_order_status": med_order.get("status"),
        },
    )


def _cancel_through_medication_order(
    order_id: str, req: TransitionRequest, request: Request, user: dict
) -> dict:
    """Cancel on a linked order, handed to the store's own cancel
    (medorders-7): an unsigned draft, with a reason (422 reason_required), by
    anyone in scope who may write orders (this route already needs
    order.write); a signed one is a prescriber's step (ruling MO-R2 (a)), 403
    prescriber_required to anyone else (audited) and 409 cancel_not_offered to
    a prescriber until that step is built. The store moves this order's
    status in the same transaction."""
    held = ROLE_PERMISSIONS.get(user.get("role", ""), set())
    try:
        result = cpoe.cancel_linked(
            order_id,
            actor_id=user["id"],
            actor_role=user.get("role"),
            reason=req.detail,
            may_cancel_any_draft=ORDER_WRITE in held,
            may_prescribe=MEDICATION_PRESCRIBE in held,
            request_meta=_request_meta(request),
        )
    except medorder_store.MedOrderError as e:
        raise _store_refusal(e, user, request, None) from e
    if not result["replayed"]:
        _push_med_order(result["med_order"])
    order = result["order"]
    with db_session() as db:
        cpoe.annotate_orders(db, [order])
    return {"status": "success", "order": order}


_TRANSITION_ORDER_USER_DEFAULT = Depends(require_permission(ORDER_WRITE))


@router.post("/{order_id}/transition")
def transition_order(
    order_id: str,
    req: TransitionRequest,
    request: Request,
    user=_TRANSITION_ORDER_USER_DEFAULT,
):
    """Guarded, idempotent state transition for an order.

    An order the medication-order store manages (medorders-7, keyed on the
    link whatever the switches say now) is moved by the store only: Cancel is
    handed to the store's own cancel, and every other target, a replay
    included, is 409 managed_by_medication_order."""
    _scope_by_order(order_id, user)
    if req.to_status in orders_service.LEGAL_TRANSITIONS:
        linked = cpoe.linked_order(order_id)
        if linked is not None:
            if req.to_status == "cancelled":
                return _cancel_through_medication_order(order_id, req, request, user)
            raise _managed_by_medication_order_conflict(linked)
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
        raise _lab_managed_conflict(e) from e
    except orders_service.OrderManagedByMedicationOrder as e:
        # Linked in the instant since the check above (the service checks
        # the link inside its own transaction).
        raise _managed_by_medication_order_conflict({"id": e.med_order_id}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


_ADD_RESULT_USER_DEFAULT = Depends(require_permission(ORDER_WRITE))


@router.post("/{order_id}/result")
def add_result(order_id: str, req: ResultRequest, user=_ADD_RESULT_USER_DEFAULT):
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
        raise _lab_managed_conflict(e) from e
    except orders_service.OrderAlreadyResulted as e:
        # 409, not 400: the request is well-formed, the ORDER is in a state that
        # refuses it. A stale tab showing '+ Result' lands here.
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
