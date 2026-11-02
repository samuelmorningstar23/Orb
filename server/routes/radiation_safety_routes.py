"""Local equipment evidence with explicit authority and dates."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from permissions import require_permission
from routes.opt_in_gates import module_gate
from services import radiation_safety_service as svc
from services.offline_workflows import WorkflowError

router = APIRouter(prefix="/api/radiation-safety", tags=["Radiation safety evidence"],
                   dependencies=[Depends(module_gate("radiation-safety"))])


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    asset_id: int = Field(ge=1)
    record_kind: Literal["licence", "officer"]
    reference: str = Field(min_length=1, max_length=300)
    authority: str = Field(min_length=1, max_length=300)
    valid_from: str = Field(min_length=10, max_length=10)
    valid_to: str = Field(min_length=10, max_length=10)
    officer_name: str | None = Field(default=None, max_length=200)
    qualification_reference: str | None = Field(default=None, max_length=300)
    scope_description: str = Field(min_length=1, max_length=4000)


class Withdrawal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4000)


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except WorkflowError as exc:
        raise HTTPException(exc.status, {"error": exc.code, "message": str(exc)}) from exc


@router.get("")
def listing(asset_id: int | None = Query(default=None, ge=1), offset: int = Query(default=0, ge=0),
            limit: int = Query(default=50, ge=1, le=100),
            asset_query: str = Query(default="", max_length=200),
            asset_offset: int = Query(default=0, ge=0), asset_limit: int = Query(default=50, ge=1, le=100), user: dict = Depends(require_permission("radiation_safety.read"))):
    return call(svc.listing, actor=user, asset_id=asset_id, offset=offset, limit=limit, asset_query=asset_query, asset_offset=asset_offset, asset_limit=asset_limit)


@router.post("", status_code=201)
def create(body: Evidence, user: dict = Depends(require_permission("radiation_safety.write"))):
    return call(svc.create, actor=user, values=body.model_dump())


@router.get("/{record_id}")
def history(record_id: str, user: dict = Depends(require_permission("radiation_safety.read"))):
    return call(svc.history, record_id, actor=user)


@router.post("/{record_id}/withdraw")
def withdraw(record_id: str, body: Withdrawal, user: dict = Depends(require_permission("radiation_safety.write"))):
    return call(svc.withdraw, record_id, actor=user, **body.model_dump())
