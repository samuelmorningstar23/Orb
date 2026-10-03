"""Orb Hospital OS — FHIR R4 Export / Write-Back API Routes

Read-only endpoints that serialise Orb's clinical data OUT as FHIR R4 resources
so they can be written into a hospital's EHR. Exported resources are clearly
flagged as originating from Orb, and AI-assisted content carries an ai-draft
provenance tag (see services/fhir_export_service.py).

    GET /api/fhir/Patient/{patient_id}                     → Patient resource
    GET /api/fhir/patient/{patient_id}/$everything         → collection Bundle
    GET /api/fhir/patient/{patient_id}/MedicationRequest   → searchset Bundle

All endpoints require the ``patient.read_all`` permission and every export is
audited (category ``phi_access``) because PHI is leaving the building.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from permissions import (require_patient_access, require_permission,
                         has_permission, department_scope, patient_in_scope)
from database import db_session
from services.audit_service import log_audit_event
from services import fhir_export_service as export

router = APIRouter(prefix="/api/fhir", tags=["fhir-export"])


def _authorize_patient_access(user: dict, patient_id: str) -> None:
    """Allow the export if the caller holds patient.read_all (clinician/admin)
    OR is the patient themselves exporting their OWN record (patient portal
    "Download my record" — GDPR data-portability). Otherwise 403."""
    if has_permission(user.get("role"), "patient.read_all"):
        # Holding patient.read_all is not the same as covering this ward. A
        # departmented clinician exporting a whole chart out of the building is
        # the LAST place to skip the scope check.
        dept = department_scope(user)
        if dept is not None:
            with db_session() as db:
                if not patient_in_scope(db, patient_id, dept):
                    # 404, not 403 — a 403 would confirm the patient exists.
                    raise HTTPException(status_code=404,
                                        detail=f"Patient {patient_id} not found")
        return
    with db_session() as db:
        row = db.execute(
            "SELECT id FROM patients WHERE id = ? AND user_id = ?",
            (patient_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="You may only export your own record")


def _audit_export(request: Request, user: dict, patient_id: str,
                  target_type: str, resource_count: int) -> None:
    """Record a PHI-access audit event for a FHIR export.

    Uses the fail-closed ``phi_access`` category: if the audit write cannot be
    persisted, log_audit_event raises and the export does not return unaudited.
    """
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="phi_access",
        action_type="fhir_export",
        outcome="success",
        summary_message=f"FHIR R4 export of {target_type} for patient {patient_id}",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type=target_type,
        target_id=patient_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"resource_count": resource_count, "format": "fhir-r4"},
    )


@router.get("/Patient/{patient_id}")
def export_patient(
    patient_id: str,
    request: Request,
    # No audit_action: _audit_export below already writes the phi_access row.
    user: dict = Depends(require_patient_access("patient.read_all")),
):
    """Export a single patient as a FHIR R4 Patient resource."""
    resource = export.patient_to_fhir(patient_id)
    if resource is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    _audit_export(request, user, patient_id, "Patient", 1)
    return resource


@router.get("/patient/{patient_id}/$everything")
def export_everything(
    patient_id: str,
    request: Request,
    since: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """Export a full FHIR R4 collection Bundle for a patient (Patient +
    Observations + MedicationRequests + AllergyIntolerances + Conditions).

    Clinicians/admins (patient.read_all) can export anyone; a patient can export
    only their OWN record (data-portability from the Bridge portal)."""
    _authorize_patient_access(user, patient_id)
    if since:
        # observations_to_fhir compares it in the stored spelling and refuses
        # anything that is not a timestamp; say so as a 400, not a 500.
        from services.clinical_time import to_utc_iso
        try:
            to_utc_iso(since)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"since is not a timestamp: {since!r}")
    bundle = export.patient_bundle(patient_id, since=since)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    _audit_export(request, user, patient_id, "Bundle", len(bundle.get("entry", [])))
    return bundle


@router.get("/patient/{patient_id}/MedicationRequest")
def export_medications(
    patient_id: str,
    request: Request,
    user: dict = Depends(require_patient_access("patient.read_all")),
):
    """Export a patient's medications as a FHIR R4 searchset Bundle of
    MedicationRequest resources (AI-suggested meds flagged as drafts)."""
    resources = export.medications_to_fhir(patient_id)
    _audit_export(request, user, patient_id, "MedicationRequest", len(resources))
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "meta": export._orb_meta(),
        "total": len(resources),
        "entry": [{
            "fullUrl": f"https://orb.health/fhir/MedicationRequest/{r.get('id', '')}",
            "resource": r,
        } for r in resources],
    }
