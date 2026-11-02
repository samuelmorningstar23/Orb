"""Offline care-plans documentation. No inference, transport or clinical execution."""
from fastapi import APIRouter, Depends, Header, Query, Request
from permissions import require_permission
from routes.opt_in_gates import module_gate
from services import offline_clinical_records as records


class CreateIn(records.CreateRecord):
    data: records.CareData


class UpdateIn(records.UpdateRecord):
    data: records.CareData


require_module = module_gate("care-plans")


router = APIRouter(prefix="/api/care-plans", tags=["care-plans"], dependencies=[Depends(require_module)])


@router.get("/meta")
def get_meta(user: dict = Depends(require_permission("care_plan.read"))):
    return records.meta("care-plans", user)


@router.get("/records")
def get_records(request: Request, patient_id: str | None = Query(None, max_length=200),
                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                user: dict = Depends(require_permission("care_plan.read"))):
    return records.list_records("care-plans", user, patient_id, limit, offset, request)


@router.post("/records", status_code=201)
def post_record(body: CreateIn, request: Request,
                idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=200),
                user: dict = Depends(require_permission("care_plan.write"))):
    return records.create("care-plans", body, user, idempotency_key, request)


@router.get("/records/{record_id}")
def get_record(record_id: str, request: Request,
               user: dict = Depends(require_permission("care_plan.read"))):
    return records.detail("care-plans", record_id, user, request)


@router.put("/records/{record_id}")
def put_record(record_id: str, body: UpdateIn, request: Request,
               user: dict = Depends(require_permission("care_plan.write"))):
    return records.update("care-plans", record_id, body, user, request)


@router.post("/records/{record_id}/sign")
def sign_record(record_id: str, body: records.VersionIn, request: Request,
                user: dict = Depends(require_permission("care_plan.read"))):
    return records.sign("care-plans", record_id, body.version, user, request)


@router.post("/records/{record_id}/addenda")
def post_addendum(record_id: str, body: records.AddendumIn, request: Request,
                  user: dict = Depends(require_permission("care_plan.write"))):
    return records.addendum("care-plans", record_id, body, user, request)


@router.get("/records/{record_id}/history")
def get_history(record_id: str, request: Request, offset: int = Query(0, ge=0),
                user: dict = Depends(require_permission("care_plan.read"))):
    return records.history("care-plans", record_id, user, offset, request)
