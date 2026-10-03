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

Planned endpoints: GET /my-queue; GET /patients/{patient_id}/record and
/consultations; GET and PUT /visits/{visit_id}/consultation, POST .../sign and
.../addenda; POST /visits/{visit_id}/allergy-review, /complete, /orders,
/prescriptions; GET /terminology/search.
"""
from fastapi import APIRouter, Depends

import config
from permissions import OPD_CONSULT, require_permission
from routes.opt_in_gates import require_opd_feature

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
