"""Orb Hospital OS — Pharmacist Verification API Routes.

The closed loop for AI/agentic medications. A pharmacist (helix.read/helix.write)
reviews meds charted with provenance ``llm_extracted``/``agentic`` and either
verifies (activates) or rejects (discontinues) them, correcting the dose as
needed. Every verify/reject is audited under the ``clinical_record`` category.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from permissions import require_permission
from services import pharmacy_verification_service as pharmacy
from services.audit_service import log_audit_event
from services.drug_safety_service import formulary_interaction_coverage

router = APIRouter(prefix="/api/pharmacy", tags=["Pharmacy Verification"])


class VerifyRequest(BaseModel):
    dosage: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    # Required (non-empty) when the re-run safety screen is severe or failed —
    # the pharmacist's acknowledgement reason for overriding the block.
    ack_reason: Optional[str] = None


class RejectRequest(BaseModel):
    reason: str


@router.get("/pending")
def get_pending(user=Depends(require_permission("helix.read"))):
    """List all AI/agentic meds awaiting pharmacist verification."""
    return {"pending": pharmacy.list_pending_verification()}


@router.get("/interaction-coverage")
def get_interaction_coverage(user=Depends(require_permission("helix.read"))):
    """Which formulary items the interaction knowledge base can screen.

    An item that is not covered is screened for allergies only, so a clean
    interaction result for it means nothing; this is the list to review.
    """
    return formulary_interaction_coverage()


@router.post("/{med_id}/verify")
def verify(med_id: int, req: VerifyRequest, request: Request,
           user=Depends(require_permission("helix.write"))):
    """Verify (and optionally dose-correct) an AI/agentic med → active order.

    A blank or placeholder dose, route or frequency returns 422. A severe (or
    failed) re-run safety screen returns 428 Precondition Required; the
    pharmacist re-submits with a non-empty ``ack_reason`` to override (the
    override is audited on the tamper-evident chain by the service).
    """
    try:
        result = pharmacy.verify_medication(
            med_id=med_id,
            pharmacist_id=user["id"],
            dosage=req.dosage,
            route=req.route,
            frequency=req.frequency,
            ack_reason=req.ack_reason,
        )
    except pharmacy.IncompleteOrderError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        if str(e).startswith("severe_ack_required:"):
            raise HTTPException(
                status_code=428,
                detail={
                    "error": "severe_ack_required",
                    # The prefix is the service's marker for this branch; the
                    # queue prints `message` as it is, so it goes.
                    "message": str(e).split(":", 1)[1].strip(),
                    "safety": getattr(e, "safety", None),
                },
            )
        raise HTTPException(status_code=400, detail=str(e))

    med = result["medication"]
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_record",
        action_type="medication_verify",
        outcome="success",
        summary_message=f"Pharmacist verified medication {med_id} ({med.get('drug_name')})",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type="medication",
        target_id=str(med_id),
        patient_id=med.get("patient_id"),
        request_path=request.url.path,
        http_method=request.method,
        metadata={
            "dose": med.get("dose"),
            "route": med.get("route"),
            "frequency": med.get("frequency"),
            "max_severity": result["safety"].get("max_severity"),
            "safety_alert_count": len(result["safety_alerts"]),
            "interaction_screened": result["safety"].get("interaction_screened"),
            "screen_failed": bool(result["safety"].get("screen_failed")),
            "acknowledged": bool(req.ack_reason),
        },
    )
    return result


@router.post("/{med_id}/reject")
def reject(med_id: int, req: RejectRequest, request: Request,
           user=Depends(require_permission("helix.write"))):
    """Reject an AI/agentic med → discontinued, with the reason recorded."""
    try:
        result = pharmacy.reject_medication(
            med_id=med_id,
            pharmacist_id=user["id"],
            reason=req.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    med = result["medication"]
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_record",
        action_type="medication_reject",
        outcome="success",
        summary_message=f"Pharmacist rejected medication {med_id} ({med.get('drug_name')}): {result['reason']}",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type="medication",
        target_id=str(med_id),
        patient_id=med.get("patient_id"),
        request_path=request.url.path,
        http_method=request.method,
        metadata={
            "reason": result["reason"],
            "max_severity": result["safety"].get("max_severity"),
            "safety_alert_count": len(result["safety_alerts"]),
        },
    )
    return result
