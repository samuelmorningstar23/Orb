"""Default-off local surveillance with explicit source and interpretation contracts."""
from typing import Literal
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import Field
from permissions import INFECTION_SURVEILLANCE_READ as READ, INFECTION_SURVEILLANCE_WRITE as WRITE, INFECTION_SURVEILLANCE_MANAGE as MANAGE, require_permission
from routes.opt_in_gates import module_gate
from routes.local_operations_http import StrictBody, call
from services import infection_surveillance_service as svc

router = APIRouter(prefix='/api/infection-surveillance', tags=['Local infection surveillance'], dependencies=[Depends(module_gate('infection-surveillance'))])


class DefinitionIn(StrictBody):
    code: str = Field(max_length=64)
    name: str = Field(max_length=200)
    case_definition: str = Field(max_length=10000)
    counting_policy: str = Field(max_length=4000)
    denominator_unit: Literal['patient_days', 'device_days']
    device_label: str | None = Field(None, max_length=200)
    evidence_reference: str = Field(max_length=500)


class WindowIn(StrictBody):
    definition_id: str = Field(max_length=100)
    label: str = Field(max_length=200)
    population_description: str = Field(max_length=4000)
    start_date: str = Field(max_length=10)
    end_date: str = Field(max_length=10)
    source_reference: str = Field(max_length=500)
    rate_multiplier: int = Field(ge=1, le=1000000)
    minimum_cell: int = Field(ge=1, le=100000)
    isolate_policy: Literal['all_isolates', 'first_per_patient']


class CaseIn(StrictBody):
    window_id: str = Field(max_length=100)
    patient_id: str = Field(max_length=100)
    episode_reference: str = Field(max_length=200)
    event_date: str = Field(max_length=10)
    evidence_reference: str = Field(max_length=500)
    statement: str = Field(max_length=10000)


class ExposureIn(StrictBody):
    window_id: str = Field(max_length=100)
    patient_id: str = Field(max_length=100)
    observed_date: str = Field(max_length=10)
    device_reference: str | None = Field(None, max_length=200)
    day_count: Literal[1] = 1
    evidence_reference: str = Field(max_length=500)


class SusceptibilityIn(StrictBody):
    window_id: str = Field(max_length=100)
    patient_id: str = Field(max_length=100)
    isolate_reference: str = Field(max_length=200)
    specimen_date: str = Field(max_length=10)
    organism_code: str = Field(max_length=100)
    specimen_code: str = Field(max_length=100)
    antibiotic_code: str = Field(max_length=100)
    result: Literal['S', 'I', 'R']
    standard_reference: str = Field(max_length=300)
    method: str = Field(max_length=300)
    evidence_reference: str = Field(max_length=500)
    validator_reference: str = Field(max_length=500)


class IsolationIn(StrictBody):
    patient_id: str = Field(max_length=100)
    case_id: str | None = Field(None, max_length=100)
    precautions: str = Field(max_length=4000)
    started_at: str = Field(max_length=100)
    start_reason: str = Field(max_length=4000)
    authority_reference: str = Field(max_length=500)


class RevisionReason(StrictBody):
    version: int = Field(ge=1)
    reason: str = Field(max_length=4000)


class ClassificationIn(RevisionReason):
    status: Literal['included', 'excluded', 'under_review']
    reviewer_reference: str = Field(max_length=500)


class IsolationEndIn(RevisionReason):
    ended_at: str = Field(max_length=100)
    authority_reference: str = Field(max_length=500)


class WindowActionIn(RevisionReason):
    action: Literal['finalize', 'reopen']


@router.get('')
def register(request: Request, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000), user: dict = Depends(require_permission(READ))):
    return call(svc.register, actor=user, limit=limit, offset=offset, request=request)


@router.get('/records/{kind}/{ident}')
def detail(kind: Literal['definition', 'window', 'case', 'exposure', 'susceptibility', 'isolation'], ident: str, request: Request,
           limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000), user: dict = Depends(require_permission(READ))):
    return call(svc.detail, kind, ident, actor=user, limit=limit, offset=offset, request=request)


@router.post('/records/definition')
def definition(body: DefinitionIn, request: Request, key: str = Header(..., alias='Idempotency-Key'), user: dict = Depends(require_permission(MANAGE))):
    return call(svc.create, 'definition', body.model_dump(), actor=user, key=key, request=request)


@router.post('/records/window')
def window(body: WindowIn, request: Request, key: str = Header(..., alias='Idempotency-Key'), user: dict = Depends(require_permission(MANAGE))):
    return call(svc.create, 'window', body.model_dump(), actor=user, key=key, request=request)


@router.post('/records/case')
def case(body: CaseIn, request: Request, key: str = Header(..., alias='Idempotency-Key'), user: dict = Depends(require_permission(WRITE))):
    return call(svc.create, 'case', body.model_dump(), actor=user, key=key, request=request)


@router.post('/records/exposure')
def exposure(body: ExposureIn, request: Request, key: str = Header(..., alias='Idempotency-Key'), user: dict = Depends(require_permission(WRITE))):
    return call(svc.create, 'exposure', body.model_dump(), actor=user, key=key, request=request)


@router.post('/records/susceptibility')
def susceptibility(body: SusceptibilityIn, request: Request, key: str = Header(..., alias='Idempotency-Key'), user: dict = Depends(require_permission(WRITE))):
    return call(svc.create, 'susceptibility', body.model_dump(), actor=user, key=key, request=request)


@router.post('/records/isolation')
def isolation(body: IsolationIn, request: Request, key: str = Header(..., alias='Idempotency-Key'), user: dict = Depends(require_permission(WRITE))):
    return call(svc.create, 'isolation', body.model_dump(), actor=user, key=key, request=request)


@router.post('/cases/{ident}/classification')
def classify(ident: str, body: ClassificationIn, request: Request, user: dict = Depends(require_permission(MANAGE))):
    return call(svc.classify, ident, body.model_dump(), actor=user, request=request)


@router.post('/records/{kind}/{ident}/void')
def void(kind: Literal['exposure', 'susceptibility'], ident: str, body: RevisionReason, request: Request, user: dict = Depends(require_permission(WRITE))):
    return call(svc.void, kind, ident, body.model_dump(), actor=user, request=request)


@router.post('/isolations/{ident}/end')
def end_isolation(ident: str, body: IsolationEndIn, request: Request, user: dict = Depends(require_permission(WRITE))):
    return call(svc.end_isolation, ident, body.model_dump(), actor=user, request=request)


@router.post('/windows/{ident}/actions')
def window_action(ident: str, body: WindowActionIn, request: Request, user: dict = Depends(require_permission(MANAGE))):
    return call(svc.window_action, ident, body.model_dump(), actor=user, request=request)


@router.get('/windows/{ident}/metrics')
def metrics(ident: str, request: Request, group_by: str = Query('organism,antibiotic', max_length=100), user: dict = Depends(require_permission(READ))):
    return call(svc.metrics, ident, actor=user, group_by=group_by, request=request)
