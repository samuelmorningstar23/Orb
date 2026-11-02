"""Authorised current-admission AI drafts; no model generation on HTTP reads."""

from typing import Any

from fastapi import APIRouter, Depends, Response
from permissions import PATIENT_READ_ALL, SAGE_QUERY, require_patient_access

from services import patient_summary_service as svc

router = APIRouter(prefix="/api/patients", tags=["patient-summary"])
_READ_USER = Depends(
    require_patient_access(
        PATIENT_READ_ALL, SAGE_QUERY, audit_action="patient_summary_viewed"
    )
)
_REFRESH_USER = Depends(
    require_patient_access(
        PATIENT_READ_ALL, SAGE_QUERY, audit_action="patient_summary_refresh_requested"
    )
)


@router.get("/{patient_id}/summary")
async def patient_summary(
    patient_id: str, response: Response, user: dict[str, Any] = _READ_USER
) -> dict[str, Any]:
    # Summaries are PHI and cannot be kept by an HTTP intermediary after the
    # live source-revision check. Durable caching is inside encrypted Orb DB.
    response.headers["Cache-Control"] = "no-store"
    answer = await svc.patient_summary_service.get(patient_id, user)
    if not svc.patient_summary_service.settings.enabled:
        answer.update(status="unavailable", reason="feature_disabled", summary=None)
    return answer


@router.post("/{patient_id}/summary/refresh", status_code=202)
async def refresh_patient_summary(
    patient_id: str, response: Response, user: dict[str, Any] = _REFRESH_USER
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    answer = await svc.patient_summary_service.request(patient_id, user)
    if not svc.patient_summary_service.settings.enabled:
        answer.update(status="unavailable", reason="feature_disabled", summary=None)
    return answer
