"""Authenticated local feedback register. No external transport."""
from typing import Literal
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import Field
from permissions import FEEDBACK_READ, FEEDBACK_WRITE, FEEDBACK_MANAGE, require_permission
from routes.opt_in_gates import module_gate
from routes.local_operations_http import StrictBody, call
from services import feedback_service as svc

router = APIRouter(prefix="/api/feedback", tags=["Feedback"], dependencies=[Depends(module_gate("feedback"))])


class FeedbackIn(StrictBody):
    kind: Literal["feedback", "complaint"]
    channel: Literal["in_person", "telephone", "written", "other"]
    subject: str = Field(max_length=200)
    statement: str = Field(max_length=10000)
    patient_id: str | None = Field(None, max_length=100)
    reporter_name: str | None = Field(None, max_length=200)
    contact_reference: str | None = Field(None, max_length=300)
    rating: int | None = Field(None, ge=1, le=5)


class ActionIn(StrictBody):
    action: Literal["note", "assign", "investigate", "resolve", "close", "reopen", "void"]
    version: int = Field(ge=1)
    note: str = Field(max_length=10000)
    owner_id: str | None = Field(None, max_length=100)


@router.get("")
def register(request: Request, status: str | None = None, limit: int = Query(100, ge=1, le=200),
             offset: int = Query(0, ge=0, le=100000), user: dict = Depends(require_permission(FEEDBACK_READ))):
    return call(svc.list_cases, actor=user, status=status, limit=limit, offset=offset, request=request)


@router.post("")
def record(body: FeedbackIn, request: Request, key: str = Header(..., alias="Idempotency-Key"),
           user: dict = Depends(require_permission(FEEDBACK_WRITE))):
    return call(svc.register, body.model_dump(), actor=user, key=key, request=request)


@router.get("/{ident}")
def detail(ident: str, request: Request, user: dict = Depends(require_permission(FEEDBACK_READ))):
    return call(svc.detail, ident, actor=user, request=request)


@router.post("/{ident}/actions")
def action(ident: str, body: ActionIn, request: Request,
           user: dict = Depends(require_permission(FEEDBACK_MANAGE))):
    return call(svc.act, ident, body.model_dump(), actor=user, request=request)
