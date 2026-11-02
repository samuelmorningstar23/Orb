"""Local CSSD processing and custody; no device or clinical control."""
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import Field
from permissions import CSSD_READ, CSSD_WRITE, CSSD_MANAGE, CSSD_RELEASE, has_permission, require_permission
from routes.opt_in_gates import module_gate
from routes.local_operations_http import StrictBody, call
from services import cssd_service as svc

router = APIRouter(prefix="/api/cssd", tags=["CSSD"], dependencies=[Depends(module_gate("cssd"))])


class ItemIn(StrictBody):
    name: str = Field(max_length=200)
    quantity: int = Field(ge=1, le=10000)


class SetIn(StrictBody):
    code: str = Field(max_length=64)
    name: str = Field(max_length=200)
    items: list[ItemIn] = Field(min_length=1, max_length=100)


class PackIn(StrictBody):
    set_id: str
    tag: str = Field(max_length=100)


class PackActionIn(StrictBody):
    action: Literal["clean", "pack", "reprocess", "retire", "issue", "return"]
    version: int = Field(ge=1)
    note: str = Field(max_length=4000)
    destination: str | None = None
    reference: str | None = None
    surgery_id: str | None = None


class CycleIn(StrictBody):
    reference: str = Field(max_length=300)
    sterilizer_reference: str = Field(max_length=300)
    sop_reference: str = Field(max_length=300)
    method: str = Field(max_length=300)
    pack_ids: list[str] = Field(min_length=1, max_length=100)


class CycleActionIn(StrictBody):
    action: Literal["complete", "record_checks", "release", "recall"]
    version: int = Field(ge=1)
    note: str = Field(max_length=4000)
    physical_result: Literal["pass", "fail", "pending"] | None = None
    chemical_result: Literal["pass", "fail", "pending"] | None = None
    biological_result: Literal["pass", "fail", "pending", "not_required"] | None = None
    biological_basis: str | None = None
    measurements: str | None = None
    expires_at: str | None = None
    release_reference: str | None = None


@router.get("")
def register(request: Request, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000),
             user: dict = Depends(require_permission(CSSD_READ))):
    return call(svc.register, actor=user, limit=limit, offset=offset, request=request)


@router.post("/sets")
def create_set(body: SetIn, request: Request, user: dict = Depends(require_permission(CSSD_MANAGE))):
    return call(svc.create_set, body.model_dump(), actor=user, request=request)


@router.post("/packs")
def create_pack(body: PackIn, request: Request, user: dict = Depends(require_permission(CSSD_WRITE))):
    return call(svc.create_pack, body.model_dump(), actor=user, request=request)


@router.get("/packs/{ident}")
def pack(ident: str, request: Request, user: dict = Depends(require_permission(CSSD_READ))):
    return call(svc.pack_detail, ident, actor=user, request=request)


@router.post("/packs/{ident}/actions")
def pack_action(ident: str, body: PackActionIn, request: Request, user: dict = Depends(require_permission(CSSD_WRITE))):
    return call(svc.pack_action, ident, body.model_dump(), actor=user, request=request)


@router.post("/cycles")
def start_cycle(body: CycleIn, request: Request, key: str = Header(..., alias="Idempotency-Key"),
                user: dict = Depends(require_permission(CSSD_WRITE))):
    return call(svc.start_cycle, body.model_dump(), actor=user, key=key, request=request)


@router.get("/cycles/{ident}")
def cycle(ident: str, request: Request, user: dict = Depends(require_permission(CSSD_READ))):
    return call(svc.cycle_detail, ident, actor=user, request=request)


@router.post("/cycles/{ident}/actions")
def cycle_action(ident: str, body: CycleActionIn, request: Request, user: dict = Depends(require_permission(CSSD_WRITE))):
    if body.action in ("release", "recall") and not has_permission(user["role"], CSSD_RELEASE):
        raise HTTPException(403, detail={"error": "permission_denied", "message": "CSSD release authority is required."})
    return call(svc.cycle_action, ident, body.model_dump(), actor=user, request=request)
