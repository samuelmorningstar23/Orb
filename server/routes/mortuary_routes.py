"""Local mortuary storage and custody routes, separate from clinical death records."""
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import Field
from permissions import MORTUARY_READ, MORTUARY_WRITE, MORTUARY_MANAGE, MORTUARY_RELEASE, has_permission, require_permission
from routes.opt_in_gates import module_gate
from routes.local_operations_http import StrictBody, call
from services import mortuary_service as svc

router = APIRouter(prefix="/api/mortuary", tags=["Mortuary"], dependencies=[Depends(module_gate("mortuary"))])


class SlotIn(StrictBody):
    code: str = Field(max_length=64)
    name: str = Field(max_length=200)


class IntakeIn(StrictBody):
    body_tag: str = Field(max_length=100)
    identity_label: str = Field(max_length=300)
    authority_reference: str = Field(max_length=500)
    slot_id: str = Field(max_length=100)
    patient_id: str | None = Field(None, max_length=100)
    received_at: str | None = None


class ActionIn(StrictBody):
    action: Literal["move", "hold", "clear_hold", "release"]
    version: int = Field(ge=1)
    note: str = Field(max_length=4000)
    slot_id: str | None = None
    authority_reference: str | None = None
    release_authority_reference: str | None = None
    recipient_name: str | None = None
    recipient_relationship: str | None = None
    identity_check_reference: str | None = None
    witness_id: str | None = None


@router.get("")
def register(request: Request, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000),
             user: dict = Depends(require_permission(MORTUARY_READ))):
    return call(svc.register, actor=user, limit=limit, offset=offset, request=request)


@router.post("/slots")
def slot(body: SlotIn, request: Request, user: dict = Depends(require_permission(MORTUARY_MANAGE))):
    return call(svc.add_slot, body.model_dump(), actor=user, request=request)


@router.post("")
def intake(body: IntakeIn, request: Request, key: str = Header(..., alias="Idempotency-Key"),
           user: dict = Depends(require_permission(MORTUARY_WRITE))):
    return call(svc.intake, body.model_dump(), actor=user, key=key, request=request)


@router.get("/{ident}")
def detail(ident: str, request: Request, user: dict = Depends(require_permission(MORTUARY_READ))):
    return call(svc.detail, ident, actor=user, request=request)


@router.post("/{ident}/actions")
def action(ident: str, body: ActionIn, request: Request,
           user: dict = Depends(require_permission(MORTUARY_WRITE))):
    if body.action in ("release", "clear_hold") and not has_permission(user["role"], MORTUARY_RELEASE):
        raise HTTPException(403, detail={"error": "permission_denied", "message": "Mortuary release authority is required."})
    return call(svc.act, ident, body.model_dump(), actor=user, request=request)
