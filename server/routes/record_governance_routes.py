"""Authenticated, opt-in local records governance."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from permissions import (RECORD_GOVERNANCE_READ, RECORD_GOVERNANCE_WRITE,
                         RECORD_GOVERNANCE_RELEASE, require_permission)
from routes.opt_in_gates import module_gate
from services import record_governance_service as svc
from services.offline_workflows import WorkflowError

router = APIRouter(prefix="/api/record-governance", tags=["Records governance"],
                   dependencies=[Depends(module_gate("record-governance"))])
Kind = Literal["hold", "file", "release"]


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    patient_id: str = Field(min_length=1, max_length=120)
    admission_id: str | None = Field(default=None, max_length=120)
    categories: list[str] = Field(default_factory=list, max_length=30)
    reason: str | None = Field(default=None, max_length=4000)
    authority_reference: str | None = Field(default=None, max_length=300)
    file_number: str | None = Field(default=None, max_length=120)
    medium: Literal["paper", "digital_reference"] | None = None
    location: str | None = Field(default=None, max_length=200)
    custodian: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=4000)
    recipient: str | None = Field(default=None, max_length=300)
    documents: list[str] | None = Field(default=None, max_length=50)


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1)
    action: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=4000)
    location: str | None = Field(default=None, max_length=200)
    custodian: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(default=None, max_length=300)


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except WorkflowError as exc:
        raise HTTPException(exc.status, {"error": exc.code, "message": str(exc)}) from exc


@router.get("/{kind}")
def records(kind: Kind, patient_id: str | None = Query(default=None, max_length=120),
            limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0),
            user: dict = Depends(require_permission(RECORD_GOVERNANCE_READ))):
    return call(svc.list_records, kind, actor=user, patient_id=patient_id, limit=limit, offset=offset)


@router.get("/{kind}/{record_id}")
def detail(kind: Kind, record_id: str, user: dict = Depends(require_permission(RECORD_GOVERNANCE_READ))):
    return call(svc.detail, kind, record_id, actor=user)


@router.post("/{kind}", status_code=201)
def create(kind: Kind, body: CreateBody, user: dict = Depends(require_permission(RECORD_GOVERNANCE_WRITE))):
    return call(svc.create, kind, body.model_dump(), actor=user)


@router.post("/{kind}/{record_id}/transition")
def transition(kind: Kind, record_id: str, body: TransitionBody,
               user: dict = Depends(require_permission(RECORD_GOVERNANCE_WRITE))):
    if kind in {"hold", "release"}:
        from permissions import has_permission
        if not has_permission(user.get("role", ""), RECORD_GOVERNANCE_RELEASE):
            raise HTTPException(403, "Records release authority is required.")
    return call(svc.transition, kind, record_id, actor=user, **body.model_dump())
