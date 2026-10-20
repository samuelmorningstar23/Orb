"""Orb — Clinical Records Routes: structured allergies, problem list, and the
order-time medication safety pre-check (allergy + interaction screening)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from permissions import require_patient_access
from services.audit_service import log_audit_event
from services.clinical_records_service import (
    add_allergy,
    add_problem,
    deactivate_allergy,
    list_allergies,
    list_problems,
    resolve_problem,
)
from services.drug_chart import require_drug_chart
from services.drug_safety_service import check_medication_safety

router = APIRouter(prefix="/api/patients", tags=["clinical-records"])


# ── Allergies ────────────────────────────────────────────────────────────────
_GET_ALLERGIES_USER_DEFAULT = Depends(require_patient_access("patient.read_all"))


@router.get("/{patient_id}/allergies")
def get_allergies(
    patient_id: str,
    include_inactive: bool = False,
    # Scope-checked, NOT read-audited: the allergy list is a
    # safety interlock read on the eMAR path and must not acquire
    # a fail-closed audit write as a new way to fail.
    user: dict = _GET_ALLERGIES_USER_DEFAULT,
):
    return {"allergies": list_allergies(patient_id, include_inactive)}


_POST_ALLERGY_USER_DEFAULT = Depends(require_patient_access("patient.write"))


@router.post("/{patient_id}/allergies")
def post_allergy(patient_id: str, data: dict, user: dict = _POST_ALLERGY_USER_DEFAULT):
    if not data.get("substance"):
        raise HTTPException(status_code=422, detail="substance is required")
    record = add_allergy(
        patient_id,
        data["substance"],
        reaction=data.get("reaction"),
        severity=data.get("severity", "unknown"),
        recorded_by=user["id"],
        source=data.get("source", "clinician"),
    )
    log_audit_event(
        correlation_id=f"allergy-add-{record['id']}",
        event_category="clinical_record",
        action_type="allergy_recorded",
        outcome="success",
        summary_message=f"Allergy '{record['substance']}' recorded for {patient_id}",
        actor_user_id=user["id"],
        patient_id=patient_id,
        target_type="patient_allergy",
        target_id=str(record["id"]),
    )
    return record


_DELETE_ALLERGY_USER_DEFAULT = Depends(require_patient_access("patient.write"))


@router.delete("/{patient_id}/allergies/{allergy_id}")
def delete_allergy(
    patient_id: str,
    allergy_id: int,
    # Ward-scoped and bound to the patient in the path: this
    # disarms the allergy interlock, so a caller must not reach
    # another ward's patient by naming an allergy id.
    user: dict = _DELETE_ALLERGY_USER_DEFAULT,
):
    if not deactivate_allergy(allergy_id, patient_id):
        raise HTTPException(status_code=404, detail="Allergy not found")
    log_audit_event(
        correlation_id=f"allergy-deact-{allergy_id}",
        event_category="clinical_record",
        action_type="allergy_deactivated",
        outcome="success",
        summary_message=f"Allergy {allergy_id} deactivated for {patient_id}",
        actor_user_id=user["id"],
        patient_id=patient_id,
        target_type="patient_allergy",
        target_id=str(allergy_id),
    )
    return {"ok": True}


# ── Problem list ─────────────────────────────────────────────────────────────
_GET_PROBLEMS_USER_DEFAULT = Depends(
    require_patient_access("patient.read_all", audit_action="problem_list_viewed")
)


@router.get("/{patient_id}/problems")
def get_problems(
    patient_id: str,
    include_resolved: bool = False,
    user: dict = _GET_PROBLEMS_USER_DEFAULT,
):
    return {"problems": list_problems(patient_id, include_resolved)}


_POST_PROBLEM_USER_DEFAULT = Depends(require_patient_access("patient.write"))


@router.post("/{patient_id}/problems")
def post_problem(patient_id: str, data: dict, user: dict = _POST_PROBLEM_USER_DEFAULT):
    if not data.get("problem"):
        raise HTTPException(status_code=422, detail="problem is required")
    record = add_problem(
        patient_id,
        data["problem"],
        icd10_code=data.get("icd10_code"),
        snomed_code=data.get("snomed_code"),
        onset_date=data.get("onset_date"),
        recorded_by=user["id"],
        source=data.get("source", "clinician"),
    )
    log_audit_event(
        correlation_id=f"problem-add-{record['id']}",
        event_category="clinical_record",
        action_type="problem_recorded",
        outcome="success",
        summary_message=f"Problem '{record['problem']}' recorded for {patient_id}",
        actor_user_id=user["id"],
        patient_id=patient_id,
        target_type="problem_list",
        target_id=str(record["id"]),
    )
    return record


_PATCH_RESOLVE_PROBLEM_USER_DEFAULT = Depends(require_patient_access("patient.write"))


@router.patch("/{patient_id}/problems/{problem_id}/resolve")
def patch_resolve_problem(
    patient_id: str, problem_id: int, user: dict = _PATCH_RESOLVE_PROBLEM_USER_DEFAULT
):
    if not resolve_problem(problem_id, patient_id):
        raise HTTPException(status_code=404, detail="Problem not found")
    log_audit_event(
        correlation_id=f"problem-resolve-{problem_id}",
        event_category="clinical_record",
        action_type="problem_resolved",
        outcome="success",
        summary_message=f"Problem {problem_id} resolved for {patient_id}",
        actor_user_id=user["id"],
        patient_id=patient_id,
        target_type="problem_list",
        target_id=str(problem_id),
    )
    return {"ok": True}


# ── Order-time medication safety pre-check ──────────────────────────────────
# The pre-prescribe screen reads the patient's active drugs and names them in
# its alerts. On an Orb Ward Record computer the drug chart stays on paper, so
# it answers 404 "The drug chart is not part of Orb Ward Record" before the
# patient is read (services/drug_chart).
_MEDICATION_SAFETY_DRUG_DEFAULT = Query(..., min_length=1)
_MEDICATION_SAFETY_USER_DEFAULT = Depends(require_patient_access("medication.write"))


@router.get("/{patient_id}/medication-safety", dependencies=[Depends(require_drug_chart)])
def medication_safety(
    patient_id: str,
    drug: str = _MEDICATION_SAFETY_DRUG_DEFAULT,
    # Scope-checked, not read-audited: same reasoning as the
    # allergy list — this is the pre-prescribe safety screen.
    user: dict = _MEDICATION_SAFETY_USER_DEFAULT,
):
    """Screen a candidate medication against the patient's allergies and active
    meds BEFORE prescribing. Returns allergy + interaction alerts and severity."""
    return check_medication_safety(patient_id, drug)
