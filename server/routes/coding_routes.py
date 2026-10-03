"""Orb Hospital OS — Revenue Integrity / Coding & CDI API Routes

Clinician-scoped access to the AI medical-coding & CDI assistant. Reads only —
suggests ICD-10/SNOMED codes, flags documentation gaps, and returns an
ILLUSTRATIVE reimbursement-capture estimate for a patient.

Auth follows the existing Bearer/JWT + RBAC pattern. The endpoint is gated on
the SCRIBE_READ_ALL ("scribe.read_all") permission so it stays scoped to
clinicians who can already read the clinical documentation it derives codes
from (doctors, nurses, admins) — CDI is a documentation-derived function.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from permissions import require_patient_access, require_permission, SCRIBE_READ_ALL
from services import coding_service
from services.audit_service import log_audit_event

router = APIRouter(prefix="/api/coding", tags=["Revenue Integrity / Coding"])


@router.get("/patient/{patient_id}")
def suggest_patient_codes(
    patient_id: str,
    request: Request,
    use_llm: bool = Query(False, description="Route through the optional LLM refinement hook (no-op unless a local LLM is wired up)."),
    # No audit_action: this route already writes its own phi_access row below.
    user=Depends(require_patient_access(SCRIBE_READ_ALL)),
):
    """Suggest codes, flag CDI gaps and estimate reimbursement for a patient.

    Returns {patient_id, patient_found, suggested_codes, cdi_flags, reimbursement}.

    An unknown patient id is a 404. It used to return the same all-zero report a
    fully-documented patient returns, so a typo'd or stale id read as "no capture
    gaps found" — a coder could close a review that never actually ran.
    """
    result = coding_service.suggest_codes(patient_id, use_llm=use_llm)
    if not result.get("patient_found"):
        raise HTTPException(status_code=404, detail="Patient not found")

    # This endpoint reads diagnosis, problem list, medications and the whole note
    # corpus for a named patient and returns a financial view of it. That is a
    # sensitive PHI read and needs its own trail — the generic audit middleware
    # only covers POST/PUT/PATCH/DELETE. 'phi_access' is fail-closed: if the audit
    # row cannot be persisted, log_audit_event raises and no report is returned.
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="phi_access",
        action_type="coding_cdi_report_viewed",
        outcome="success",
        summary_message=f"Revenue-integrity/CDI report generated for patient {patient_id}",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type="patient",
        target_id=patient_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={
            "suggested_code_count": len(result.get("suggested_codes", [])),
            "cdi_flag_count": len(result.get("cdi_flags", [])),
            "use_llm": use_llm,
        },
    )
    return result
