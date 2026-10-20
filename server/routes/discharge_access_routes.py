"""Orb Discharge Access Routes — Public Patient Verification"""

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from services.discharge_access_service import (
    get_verified_discharge_summary,
    verify_discharge_access,
)

router = APIRouter(prefix="/api/discharge-access", tags=["discharge_access"])


class VerifyRequest(BaseModel):
    token: str
    dob: str


@router.post("/verify")
def verify_access(req: VerifyRequest, request: Request):
    """Verify patient DOB and return a short-lived access grant."""
    ip_address = request.client.host if request.client else "unknown"
    result = verify_discharge_access(req.token, req.dob, ip_address)

    if result["status"] == "error":
        # We always return 401 for verification failures to prevent info leakage
        raise HTTPException(status_code=401, detail=result)

    return result


_GET_SUMMARY_AUTHORIZATION_DEFAULT = Header(None)


@router.get("/{token}/summary")
def get_summary(
    token: str, request: Request, authorization: str | None = _GET_SUMMARY_AUTHORIZATION_DEFAULT
):
    """Retrieve the summary using the access grant."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"status": "error", "reason": "missing_grant"})

    access_grant = authorization.split(" ")[1]
    ip_address = request.client.host if request.client else "unknown"

    result = get_verified_discharge_summary(token, access_grant, ip_address)

    if result["status"] == "error":
        raise HTTPException(
            status_code=403 if result["reason"] in ["revoked", "invalid_grant"] else 401,
            detail=result,
        )

    return result
