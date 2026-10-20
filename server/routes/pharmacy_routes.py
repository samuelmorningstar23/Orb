"""Orb Hospital OS — Pharmacist Verification API Routes.

The closed loop for AI/agentic medications. A pharmacist (helix.read/helix.write)
reviews meds charted with provenance ``llm_extracted``/``agentic`` and either
verifies (activates) or rejects (discontinues) them, correcting the dose as
needed. Every verify/reject is audited under the ``clinical_record`` category.

FACILITY SCOPE (multisite-9a, W12-E; ruling MS-R9 (a): the verify queue is per
facility). With more than one facility on record the queue lists only the
medicines of patients whose latest encounter is at the caller's facilities,
and verify or reject of a medicine outside them is a 404 that writes an
authorization 'facility_document_denied' row before anything is screened or
changed. With one facility on record the caller's facility scope is None and
every route here runs exactly as before.
"""

from __future__ import annotations

import uuid

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Request
from permissions import (
    enforce_patient_scope,
    facility_denial,
    facility_scope,
    log_phi_read,
    require_permission,
)
from pydantic import BaseModel
from services.audit_service import log_audit_event
from services.drug_chart import require_drug_chart
from services.drug_safety_service import formulary_interaction_coverage
from services.facility_documents import audit_document_denial, request_meta

from services import pharmacy_verification_service as pharmacy

# The verification queue is drug chart rows. On an Orb Ward Record computer
# every route answers 404 "The drug chart is not part of Orb Ward Record"
# before any row is read (services/drug_chart).
router = APIRouter(
    prefix="/api/pharmacy",
    tags=["Pharmacy Verification"],
    dependencies=[Depends(require_drug_chart)],
)


class VerifyRequest(BaseModel):
    dosage: str | None = None
    route: str | None = None
    frequency: str | None = None
    # Required (non-empty) when the re-run safety screen is severe or failed —
    # the pharmacist's acknowledgement reason for overriding the block.
    ack_reason: str | None = None


class RejectRequest(BaseModel):
    reason: str


def _facility_gate(user: dict, med_id: int, request: Request) -> None:
    """404, after an authorization 'facility_document_denied' row, when the
    medicine's patient is outside the caller's facility scope (multisite-9a).
    Checked before the service screens or changes anything.

    With one facility on record facility_scope answers None and nothing is
    read. A medicine no row holds passes here and the service refuses it in
    its own words, exactly as before."""
    facilities = facility_scope(user)
    if facilities is None:
        return
    patient_id = pharmacy.medication_patient_id(med_id)
    if patient_id is None:
        return
    with db_session() as db:
        denied, patient_facility = facility_denial(db, patient_id, facilities)
    if not denied:
        return
    audit_document_denial(
        user=user,
        lane="pharmacy_verify_queue",
        target_type="medication",
        target_id=med_id,
        document_facility_id=patient_facility,
        facilities=facilities,
        patient_id=patient_id,
        meta=request_meta(request),
    )
    raise HTTPException(status_code=404, detail=pharmacy.NOT_ON_CHART_MESSAGE)


_GET_PENDING_USER_DEFAULT = Depends(require_permission("helix.read"))


@router.get("/pending")
def get_pending(user: dict = _GET_PENDING_USER_DEFAULT) -> dict:
    """List all AI/agentic meds awaiting pharmacist verification: the caller's
    facilities only, once more than one facility is on record."""
    return {"pending": pharmacy.list_pending_verification(facilities=facility_scope(user))}


_GET_INTERACTION_COVERAGE_USER_DEFAULT = Depends(require_permission("helix.read"))


@router.get("/interaction-coverage")
def get_interaction_coverage(user=_GET_INTERACTION_COVERAGE_USER_DEFAULT):
    """Which formulary items the interaction knowledge base can screen.

    An item that is not covered is screened for allergies only, so a clean
    interaction result for it means nothing; this is the list to review.
    """
    return formulary_interaction_coverage()


_GET_SOURCE_NOTE_USER_DEFAULT = Depends(require_permission("helix.read"))


@router.get("/{med_id}/source-note")
def get_source_note(med_id: int, request: Request, user=_GET_SOURCE_NOTE_USER_DEFAULT):
    """The signed note a queue medicine was read from: its words, who signed
    it and when, and the sentence that names the drug.

    A pharmacist holds no scribe permission, so the Verify Queue reads the one
    note behind the row it is checking here, and nothing else. 404 when the
    medicine was not charted from a note. Scoped like the Helix routes (a
    no-op for the department-less central pharmacy account), and the read is
    on the audit chain before the text is returned.
    """
    note = pharmacy.source_note(med_id)
    if note is None:
        raise HTTPException(
            status_code=404, detail="This medicine was not charted from a signed note."
        )
    enforce_patient_scope(user, note["patient_id"])
    log_phi_read(
        request=request,
        user=user,
        action_type="scribe_note_viewed",
        summary_message=f"Signed note {note['note_id']} viewed from the Verify Queue (medication {med_id})",
        patient_id=note["patient_id"],
        target_type="scribe_note",
        target_id=str(note["note_id"]),
    )
    return note


_VERIFY_USER_DEFAULT = Depends(require_permission("helix.write"))


@router.post("/{med_id}/verify")
def verify(
    med_id: int,
    req: VerifyRequest,
    request: Request,
    user: dict = _VERIFY_USER_DEFAULT,
) -> dict:
    """Verify (and optionally dose-correct) an AI/agentic med → active order.

    A blank or placeholder dose, route or frequency returns 422. A severe (or
    failed) re-run safety screen returns 428 Precondition Required; the
    pharmacist re-submits with a non-empty ``ack_reason`` to override (the
    override is audited on the tamper-evident chain by the service).
    """
    _facility_gate(user, med_id, request)
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
        raise HTTPException(status_code=422, detail=str(e)) from e
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
            ) from e
        raise HTTPException(status_code=400, detail=str(e)) from e

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


_REJECT_USER_DEFAULT = Depends(require_permission("helix.write"))


@router.post("/{med_id}/reject")
def reject(
    med_id: int,
    req: RejectRequest,
    request: Request,
    user: dict = _REJECT_USER_DEFAULT,
) -> dict:
    """Reject an AI/agentic med → discontinued, with the reason recorded."""
    _facility_gate(user, med_id, request)
    try:
        result = pharmacy.reject_medication(
            med_id=med_id,
            pharmacist_id=user["id"],
            reason=req.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

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
