"""Outpatient consultations: the /api/opd routes.

Created empty in wave 1 of the hospital-system build (3 October 2026) and
registered once in main.py. Later OPD increments (opd-7, opd-10, opd-10a,
opd-11, opd-12, opd-19) add their endpoints here.

BEHIND THE 'opd' FEATURE (ORB_OPD, which also needs ORB_REGISTRATION and the
front desk module; default off). The gate is a router-level dependency, so
every endpoint added here inherits it and answers 404 while the feature is off
(after a 401 for a caller with no session).

Rules for every endpoint added here (design: docs/his_design/opd.json):
  * opd.consult on every route; an OPD prescription needs opd.consult AND
    medication.prescribe;
  * patient-addressed reads use require_patient_access(..., opd_arm=True) or
    enforce_patient_scope(..., opd_arm=True) once the OPD scope arm exists
    (W3-A), and only on the whitelist that test_opd_scope_drift pins;
    visit-addressed routes resolve the visit and refuse writes when the patient
    now has an active admission;
  * OPD never computes or shows NEWS2, a band or AI advice, and never writes
    vitals, risk_scores or medications (test_opd_no_scoring);
  * signing a consultation and writing an OPD prescription need an allergy
    review (ruling OPD-S7, held);
  * plain `def` handlers; patient_id in the path or a typed body; target_id
    always a string in audit rows.

Built: GET /patients/{patient_id}/record (opd-7, W3-A).

Planned endpoints: GET /my-queue; GET /patients/{patient_id}/consultations;
GET and PUT /visits/{visit_id}/consultation, POST .../sign and .../addenda;
POST /visits/{visit_id}/allergy-review, /complete, /orders, /prescriptions;
GET /terminology/search.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

import config
from permissions import OPD_CONSULT, department_scope, require_patient_access, require_permission
from routes.opt_in_gates import require_opd_feature
from services.opd_service import get_opd_record

router = APIRouter(
    prefix="/api/opd",
    tags=["Outpatients"],
    dependencies=[Depends(require_opd_feature)],
)


@router.get("/meta")
def opd_meta(user: dict = Depends(require_permission(OPD_CONSULT))):
    """What this site's outpatient settings are. No patient data."""
    return {
        "feature": "opd",
        "enabled": config.feature_enabled("opd"),
        "settings": {
            "scope_days": config.OPD_SCOPE_DAYS,
            "charge_at": config.OPD_CHARGE_AT,
        },
    }


@router.get("/patients/{patient_id}/record")
def opd_record(
    patient_id: str,
    request: Request,
    # The OPD scope arm (opd-7). The ordinary scope, OR (the 'opd' feature on,
    # no active admission anywhere, and a non-cancelled visit in the caller's
    # clinic within ORB_OPD_SCOPE_DAYS). On the whitelist in
    # tests/test_opd_scope_drift.py. Every read writes a fail-closed phi_access
    # row that names the arm (admission, opd_visit or hospital_wide).
    user: dict = Depends(
        require_patient_access(OPD_CONSULT, opd_arm=True, audit_action="opd_record_viewed")
    ),
):
    """The outpatient record view: identity, allergies, problems, OPD-linked
    results and signed OPD consultations of every clinic (ruling OPD-N2,
    extent ii). Never the inpatient chart, vitals, a score or AI advice."""
    record = get_opd_record(
        patient_id,
        viewer_department=department_scope(user),
        access=getattr(request.state, "orb_patient_scope", None),
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return record
