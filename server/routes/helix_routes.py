"""Orb Hospital OS — Helix API Routes"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from permissions import enforce_patient_scope, require_permission
from services import helix_service
from services.drug_safety_service import check_medication_safety
from services.medication_safety_gate import enforce_medication_safety

router = APIRouter(prefix="/api/helix", tags=["Helix Pharmacy"])

# ═══════════════════════════════════════════════════════════════════
#  READ ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@router.get("/catalog")
def get_catalog(user=Depends(require_permission("helix.read"))):
    """Get the medication master catalog."""
    return {"catalog": helix_service.get_medication_catalog()}

@router.get("/inventory")
def get_inventory(user=Depends(require_permission("helix.read"))):
    """Get all inventory lots."""
    return {"inventory": helix_service.get_inventory()}

@router.get("/orders/pending")
def get_pending_orders(user=Depends(require_permission("helix.read"))):
    """Get pending medication orders for pharmacy verification."""
    return {"orders": helix_service.get_pending_orders()}

@router.get("/orders")
def get_all_orders(user=Depends(require_permission("helix.read"))):
    """Get all medication orders across all statuses."""
    return {"orders": helix_service.get_all_orders()}

@router.get("/stock-movements")
def get_stock_movements(user=Depends(require_permission("helix.read"))):
    """Get stock movement audit log."""
    return {"movements": helix_service.get_stock_movements()}

@router.get("/orders/{order_id}/transitions")
def get_order_transitions(order_id: str, user=Depends(require_permission("helix.read"))):
    """Get transition history for a specific order."""
    return {"transitions": helix_service.get_order_transitions(order_id)}


# ═══════════════════════════════════════════════════════════════════
#  WRITE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

class OrderApproveRequest(BaseModel):
    patient_id: Optional[str] = None
    patient_name: Optional[str] = None
    medication: str
    dose: str = ""
    route: str = ""
    frequency: str = ""
    safety_ack: Optional[dict] = None


# What an order cannot go to pharmacy without. A blank medication matters as
# much as a blank dose: create_order looks the drug up with LIKE '%name%', which
# an empty name turns into "the first drug in the formulary".
_ORDER_FIELDS = ("medication", "dose", "route", "frequency")


def _and_join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _require_order_fields(req: OrderApproveRequest) -> None:
    """422 unless the medication, dose, route and frequency are all filled in.

    The Sage card used to send '' for whatever the model left out, and the
    order reached the pharmacy with no dose to check or dispense against.
    Checked before the patient lookup and the safety gate, so a refused body
    writes nothing and audits no screen for an order that never happens.
    """
    missing = [field for field in _ORDER_FIELDS if not (getattr(req, field) or "").strip()]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "order_incomplete",
                "message": f"Enter the {_and_join(missing)} before sending this order to pharmacy.",
                "missing": missing,
            },
        )


def _resolve_approve_patient(req: OrderApproveRequest) -> str:
    """Resolve the target patient for an approve request.

    An explicit patient_id wins (404 if unknown). A bare name must match
    EXACTLY ONE patient by case-insensitive equality — zero or several matches
    fail 422 with the candidate count. Never a LIKE scan, never an 'UNKNOWN'
    fallback: a medication order must not be chartable to the wrong patient.
    """
    from database import db_session

    if req.patient_id:
        with db_session() as conn:
            row = conn.execute("SELECT id FROM patients WHERE id = ?", (req.patient_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Patient {req.patient_id} not found")
        return row["id"]

    name = (req.patient_name or "").strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "patient_unresolved",
                "message": "patient_id or patient_name is required to approve a medication order.",
                "candidates": 0,
            },
        )

    with db_session() as conn:
        rows = conn.execute(
            "SELECT id FROM patients WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchall()
    if len(rows) != 1:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "patient_unresolved",
                "message": (
                    f"Name '{name}' matched {len(rows)} patients: supply an explicit "
                    f"patient_id to approve this order."
                ),
                "candidates": len(rows),
            },
        )
    return rows[0]["id"]


@router.post("/orders/approve")
def approve_helix_order(req: OrderApproveRequest, user=Depends(require_permission("helix.write"))):
    """Approve a drafted medication request from Sage and create a Helix order."""
    _require_order_fields(req)
    patient_id = _resolve_approve_patient(req)

    # SCOPING LAW — the patient is named in the BODY (2026-08-14).
    # This route creates a real medication order against whatever patient the
    # body named, and the permission above only ever answered "may this account
    # approve pharmacy orders?".
    #
    # WHY IT IS SAFE TO APPLY HERE, given that pharmacy is a hospital-wide
    # function: a central pharmacy account is seeded with NO department, and
    # permissions.department_scope treats a department-less user row as
    # unscoped — the same convention the finance accounts in
    # routes/payments_routes.py rely on. So for the pharmacist this appliance
    # actually runs, this check is a no-op; it becomes a real boundary only for
    # an account an administrator has deliberately tied to one ward, which is
    # precisely when it should bite. 404, never 403.
    #
    # Placed after resolution so the name→id path is covered too, not just an
    # explicit patient_id.
    enforce_patient_scope(user, patient_id)

    # Drug-safety gate: severe alerts 428 until acknowledged; overrides audited.
    # The dose guard too: a pharmacist holds no medication.prescribe, so a dose
    # it blocks goes back to the prescriber rather than being typed through.
    safety = enforce_medication_safety(
        patient_id,
        req.medication,
        actor_id=user["id"],
        entry_point="helix.approve",
        safety_ack=req.safety_ack,
        dose=req.dose,
        route=req.route,
        actor_role=user.get("role"),
    )

    try:
        order = helix_service.create_order(
            patient_id=patient_id,
            medication_name=req.medication,
            dose=req.dose.strip(),
            route=req.route.strip(),
            frequency=req.frequency.strip(),
            prescribed_by=user["id"]
        )
        return {"status": "success", "order": order, "safety": safety}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class TransitionRequest(BaseModel):
    target_status: str
    idempotency_key: Optional[str] = None
    pharmacist_notes: str = ""
    substitute_medication_id: Optional[str] = None
    scheduled_at: Optional[str] = None
    safety_ack: Optional[dict] = None


@router.get("/orders/{order_id}/safety")
def get_order_safety(order_id: str, substitute_medication_id: Optional[str] = None,
                     user=Depends(require_permission("helix.read"))):
    """The allergy and interaction screen for the drug this order would dispense.

    Read-only: it writes no audit row, because the gate audits the screen at the
    step that commits the drug. `substitute_medication_id` previews a substitute
    the pharmacist is considering, so the alert is on screen before they propose
    it rather than arriving as a refusal afterwards.
    """
    try:
        target = helix_service.order_screen_target(order_id, substitute_medication_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not target:
        raise HTTPException(status_code=404, detail="Order not found")
    enforce_patient_scope(user, target["patient_id"])

    try:
        safety = check_medication_safety(target["patient_id"], target["screen_name"])
    except Exception:
        # Never a clean screen by default: a failed screen says so.
        safety = {
            "drug": target["screen_name"],
            "allergy_alerts": [],
            "interaction_alerts": [],
            "max_severity": "unknown",
            "blocking": True,
            "safe": False,
            "interaction_screened": False,
            "screen_failed": True,
            "coverage_note": "Drug-safety screening is unavailable: do not assume this order was checked.",
        }
    return {
        "order_id": order_id,
        "medication_id": target["medication_id"],
        "drug_name": target["drug_name"],
        "is_substitute": target["is_substitute"],
        "safety": safety,
    }


@router.post("/orders/{order_id}/transition")
def transition_order(order_id: str, req: TransitionRequest, user=Depends(require_permission("helix.write"))):
    """
    Guarded, idempotent state transition for a Helix medication order.

    Legal transitions:
      pending -> reviewing
      reviewing -> verified | rejected | substitute_suggested
      substitute_suggested -> reviewing
      verified -> scheduled | reviewing | rejected
      scheduled -> dispensed (decrements inventory atomically) | reviewing | rejected

    `rejected` needs `pharmacist_notes` (422 without), and `verified` needs a
    dose on the order (422 without). A step the order's current status does not
    allow answers 409 with the order as it is now, so a screen opened before
    someone else moved it can reload instead of failing with a code.

    `substitute_suggested`, `verified` and `dispensed` each pass the drug-safety
    gate on the effective drug (the substitute when one is set): severe alerts
    answer 428 until re-sent with `safety_ack`, and an override is audited.

    `dispensed -> delivered` is NOT here. Receipt is the ward's attestation, so
    it has its own route (`POST /orders/{id}/receive`) and its own permission
    (`helix.receive`), which this route's `helix.write` holders do not have. The
    service refuses the target too, so naming it in the body reaches nothing.
    """
    # Scoped like /receive: a no-op for the department-less central pharmacy
    # account, a real boundary for an account tied to one ward.
    patient_id = helix_service.order_patient_id(order_id)
    if patient_id:
        enforce_patient_scope(user, patient_id)

    safety = None
    try:
        target = helix_service.transition_screen_target(
            order_id,
            req.target_status,
            substitute_medication_id=req.substitute_medication_id,
            idempotency_key=req.idempotency_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if target:
        safety = enforce_medication_safety(
            target["patient_id"],
            target["screen_name"],
            actor_id=user["id"],
            entry_point=f"helix.{req.target_status}",
            safety_ack=req.safety_ack,
        )

    try:
        result = helix_service.transition_order_status(
            order_id=order_id,
            target_status=req.target_status,
            actor_id=user["id"],
            idempotency_key=req.idempotency_key,
            pharmacist_notes=req.pharmacist_notes,
            substitute_medication_id=req.substitute_medication_id,
            scheduled_at=req.scheduled_at,
        )
        if safety is not None:
            result["safety"] = safety
        return result
    except helix_service.StaleOrderState as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "order_state_changed",
                "message": str(e),
                "current_status": e.current_status,
                "order": helix_service.get_order(order_id),
            },
        )
    except helix_service.TransitionNeedsInput as e:
        raise HTTPException(status_code=422, detail={"error": "order_incomplete", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ═══════════════════════════════════════════════════════════════════
#  WARD RECEIPT — the other end of the dispense
# ═══════════════════════════════════════════════════════════════════

class ReceiveRequest(BaseModel):
    idempotency_key: Optional[str] = None


@router.get("/orders/awaiting-receipt")
def orders_awaiting_receipt(user=Depends(require_permission("helix.receive"))):
    """Drugs in transit to this caller's ward, waiting to be signed for.

    Ward-scoped from the AUTHENTICATED user row, never from the request — the
    same rule as every other patient-list surface (`permissions.department_scope`
    and its note on why /api/nurse/board once leaked 30 patients across six
    departments).

    Gated on `helix.receive` alone. A nurse holds no other pharmacy permission,
    and this returns no inventory, no formulary and no order that is not already
    on her ward.
    """
    from permissions import department_scope
    return {"orders": helix_service.orders_awaiting_receipt(department_scope(user))}


@router.post("/orders/{order_id}/receive")
def receive_order(order_id: str, req: ReceiveRequest,
                  user=Depends(require_permission("helix.receive"))):
    """Record that a dispensed drug physically arrived on the ward.

    The last hop of the custody chain, and the only route that can write
    `delivered`. Two independent controls stand behind it and both are
    deliberate:

      * `helix.receive` is granted to the nurse and NOT to the pharmacist, so
        the desk that dispatched cannot close its own loop.
      * `helix_service.receive_order` additionally refuses when the receiving
        account IS the dispatching account, which is what holds the day an
        administrator grants one login both permissions.
    """
    # SCOPING LAW — the patient is named by the ORDER, not by the request, so it
    # is resolved first and checked BEFORE anything is written (the same shape as
    # /orders/approve). A nurse can only see orders the ward-scoped listing above
    # returned; this is what stops a hand-crafted order id from another ward.
    # 404, never 403 — a 403 would confirm the order exists.
    patient_id = helix_service.order_patient_id(order_id)
    if not patient_id:
        raise HTTPException(status_code=404, detail="Order not found")
    enforce_patient_scope(user, patient_id)

    try:
        return helix_service.receive_order(
            order_id=order_id,
            actor_id=user["id"],
            idempotency_key=req.idempotency_key,
        )
    except helix_service.SelfReceiptRefused as e:
        # 403, not 400: this is a policy refusal naming a control, and it should
        # read as one wherever access decisions are reviewed.
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
