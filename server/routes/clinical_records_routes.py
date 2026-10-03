"""Orb — Clinical Records Routes: structured allergies, problem list, and the
order-time medication safety pre-check (allergy + interaction screening)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from permissions import require_patient_access
from services.clinical_records_service import (
    add_allergy, list_allergies, deactivate_allergy,
    add_problem, list_problems, resolve_problem,
)
from services.drug_safety_service import check_medication_safety
from services.audit_service import log_audit_event

router = APIRouter(prefix="/api/patients", tags=["clinical-records"])


# ── Allergies ────────────────────────────────────────────────────────────────
@router.get("/{patient_id}/allergies")
def get_allergies(patient_id: str, include_inactive: bool = False,
                  # Scope-checked, NOT read-audited: the allergy list is a
                  # safety interlock read on the eMAR path and must not acquire
                  # a fail-closed audit write as a new way to fail.
                  user: dict = Depends(require_patient_access("patient.read_all"))):
    return {"allergies": list_allergies(patient_id, include_inactive)}


@router.post("/{patient_id}/allergies")
def post_allergy(patient_id: str, data: dict,
                 user: dict = Depends(require_patient_access("patient.write"))):
    if not data.get("substance"):
        raise HTTPException(status_code=422, detail="substance is required")
    record = add_allergy(
        patient_id, data["substance"],
        reaction=data.get("reaction"),
        severity=data.get("severity", "unknown"),
        recorded_by=user["id"],
        source=data.get("source", "clinician"),
    )
    log_audit_event(
        correlation_id=f"allergy-add-{record['id']}", event_category="clinical_record",
        action_type="allergy_recorded", outcome="success",
        summary_message=f"Allergy '{record['substance']}' recorded for {patient_id}",
        actor_user_id=user["id"], patient_id=patient_id,
        target_type="patient_allergy", target_id=str(record["id"]),
    )
    return record


@router.delete("/{patient_id}/allergies/{allergy_id}")
def delete_allergy(patient_id: str, allergy_id: int,
                   # Ward-scoped and bound to the patient in the path: this
                   # disarms the allergy interlock, so a caller must not reach
                   # another ward's patient by naming an allergy id.
                   user: dict = Depends(require_patient_access("patient.write"))):
    if not deactivate_allergy(allergy_id, patient_id):
        raise HTTPException(status_code=404, detail="Allergy not found")
    log_audit_event(
        correlation_id=f"allergy-deact-{allergy_id}", event_category="clinical_record",
        action_type="allergy_deactivated", outcome="success",
        summary_message=f"Allergy {allergy_id} deactivated for {patient_id}",
        actor_user_id=user["id"], patient_id=patient_id,
        target_type="patient_allergy", target_id=str(allergy_id),
    )
    return {"ok": True}


# ── Problem list ─────────────────────────────────────────────────────────────
@router.get("/{patient_id}/problems")
def get_problems(patient_id: str, include_resolved: bool = False,
                 user: dict = Depends(
                     require_patient_access("patient.read_all",
                                            audit_action="problem_list_viewed")
                 )):
    return {"problems": list_problems(patient_id, include_resolved)}


@router.post("/{patient_id}/problems")
def post_problem(patient_id: str, data: dict,
                 user: dict = Depends(require_patient_access("patient.write"))):
    if not data.get("problem"):
        raise HTTPException(status_code=422, detail="problem is required")
    record = add_problem(
        patient_id, data["problem"],
        icd10_code=data.get("icd10_code"), snomed_code=data.get("snomed_code"),
        onset_date=data.get("onset_date"), recorded_by=user["id"],
        source=data.get("source", "clinician"),
    )
    log_audit_event(
        correlation_id=f"problem-add-{record['id']}", event_category="clinical_record",
        action_type="problem_recorded", outcome="success",
        summary_message=f"Problem '{record['problem']}' recorded for {patient_id}",
        actor_user_id=user["id"], patient_id=patient_id,
        target_type="problem_list", target_id=str(record["id"]),
    )
    return record


@router.patch("/{patient_id}/problems/{problem_id}/resolve")
def patch_resolve_problem(patient_id: str, problem_id: int,
                          user: dict = Depends(require_patient_access("patient.write"))):
    if not resolve_problem(problem_id, patient_id):
        raise HTTPException(status_code=404, detail="Problem not found")
    log_audit_event(
        correlation_id=f"problem-resolve-{problem_id}", event_category="clinical_record",
        action_type="problem_resolved", outcome="success",
        summary_message=f"Problem {problem_id} resolved for {patient_id}",
        actor_user_id=user["id"], patient_id=patient_id,
        target_type="problem_list", target_id=str(problem_id),
    )
    return {"ok": True}


# ── Order-time medication safety pre-check ──────────────────────────────────
@router.get("/{patient_id}/medication-safety")
def medication_safety(patient_id: str, drug: str = Query(..., min_length=1),
                      # Scope-checked, not read-audited: same reasoning as the
                      # allergy list — this is the pre-prescribe safety screen.
                      user: dict = Depends(require_patient_access("medication.write"))):
    """Screen a candidate medication against the patient's allergies and active
    meds BEFORE prescribing. Returns allergy + interaction alerts and severity."""
    return check_medication_safety(patient_id, drug)
