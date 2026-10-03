"""Patient registration: the /api/registration routes.

Created empty in wave 1 of the hospital-system build (3 October 2026) and
registered once in main.py. opd-4, opd-5 and opd-6 (W4-A) add their endpoints
here.

BEHIND THE 'registration' FEATURE (ORB_REGISTRATION, default off). The gate is
a router-level dependency, so every endpoint added here inherits it and answers
404 while the feature is off (after a 401 for a caller with no session).

Rules for every endpoint added here (design: docs/his_design/opd.json):
  * the desk dependency permissions.require_registration_desk(mode) (W2-E):
    read is patient.read_all or registration.read, write is frontdesk.write,
    patient.write or registration.write;
  * only the minimal-field search is hospital-wide (UHID, name, sex, age, phone
    last 4, last visit date, inpatient now): at least 3 characters, at most 20
    results, a phi_access row naming the matched ids;
  * an identity check answers only match or no match and never returns a
    stored value (ruling OPD-S1, held), rate-limited and audited;
  * a probable duplicate follows config.DUPLICATE_MODE ('block' until ruling
    OPD-S2 is confirmed); an exact UHID or active ABHA match is always refused;
  * no allergy field at the desk (ruling OPD-S5, held);
  * every write audits as patient_identity (fail-closed) in the transaction.

Planned endpoints: GET /config; GET /search; POST /match; POST /patients;
GET and PATCH /patients/{patient_id}; GET /patients/{patient_id}/changes and
/card; GET /duplicates; POST /duplicates/{candidate_id}/resolve.
"""
from fastapi import APIRouter, Depends

import config
from permissions import REGISTRATION_READ, require_permission
from routes.opt_in_gates import require_registration_feature

router = APIRouter(
    prefix="/api/registration",
    tags=["Registration"],
    dependencies=[Depends(require_registration_feature)],
)


@router.get("/meta")
def registration_meta(user: dict = Depends(require_permission(REGISTRATION_READ))):
    """What this site's registration settings are. No patient data."""
    return {
        "feature": "registration",
        "enabled": config.feature_enabled("registration"),
        "settings": {
            "uhid_source": config.UHID_SOURCE,
            "uhid_pattern_set": config.UHID_PATTERN is not None,
            "duplicate_mode": config.DUPLICATE_MODE,
            "queue_poll_seconds": config.OPD_QUEUE_POLL_SECONDS,
        },
    }
