"""Clinic sessions and availability: the /api/schedule routes.

Created empty in wave 1 of the hospital-system build (3 October 2026) and
registered once in main.py. opd-15 (W2-E) adds the clinic session master and
availability here.

BEHIND THE 'registration' FEATURE (ORB_REGISTRATION, default off): clinic
timings belong to the registration desk. The gate is a router-level
dependency, so every endpoint added here inherits it and answers 404 while the
feature is off (after a 401 for a caller with no session).

Rules for every endpoint added here (design: docs/his_design/opd.json):
  * writes need schedule.manage and audit as configuration; availability reads
    need registration.read or patient.read_all;
  * slots are computed in hospital-local time (services.hospital_time), never
    stored; overlapping sessions for one doctor are refused;
  * plain `def` handlers.
"""
from fastapi import APIRouter, Depends

import config
from permissions import REGISTRATION_READ, require_permission
from routes.opt_in_gates import require_registration_feature

router = APIRouter(
    prefix="/api/schedule",
    tags=["Clinic schedule"],
    dependencies=[Depends(require_registration_feature)],
)


@router.get("/meta")
def schedule_meta(user: dict = Depends(require_permission(REGISTRATION_READ))):
    """Whether clinic timings are offered at this site. No patient data."""
    return {"feature": "registration", "enabled": config.feature_enabled("registration")}
