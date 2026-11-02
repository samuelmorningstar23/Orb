"""Offline perioperative documentation. No inference, transport or clinical execution."""
from fastapi import APIRouter, Depends, Header, Query, Request
from permissions import require_permission
from routes.opt_in_gates import module_gate
from services import offline_clinical_records as records


class CreateIn(records.CreateRecord):
    data: records.PerioperativeData


class UpdateIn(records.UpdateRecord):
    data: records.PerioperativeData


require_module = module_gate("perioperative")


router = APIRouter(prefix="/api/perioperative", tags=["perioperative"], dependencies=[Depends(require_module)])


@router.get("/meta")
def get_meta(user: dict = Depends(require_permission("perioperative_record.read"))):
    return records.meta("perioperative", user)


@router.get("/cases")
def get_cases(request: Request, q: str = Query("", max_length=200), offset: int = Query(0, ge=0),
              user: dict = Depends(require_permission("perioperative_record.read"))):
    return records.list_perioperative_cases(user, request, q, offset)


@router.get("/records")
def get_records(request: Request, patient_id: str | None = Query(None, max_length=200),
                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                user: dict = Depends(require_permission("perioperative_record.read"))):
    return records.list_records("perioperative", user, patient_id, limit, offset, request)


@router.post("/records", status_code=201)
def post_record(body: CreateIn, request: Request,
                idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=200),
                user: dict = Depends(require_permission("perioperative_record.write"))):
    return records.create("perioperative", body, user, idempotency_key, request)


@router.get("/records/{record_id}")
def get_record(record_id: str, request: Request,
               user: dict = Depends(require_permission("perioperative_record.read"))):
    return records.detail("perioperative", record_id, user, request)


@router.put("/records/{record_id}")
def put_record(record_id: str, body: UpdateIn, request: Request,
               user: dict = Depends(require_permission("perioperative_record.write"))):
    return records.update("perioperative", record_id, body, user, request)


@router.post("/records/{record_id}/sign")
def sign_record(record_id: str, body: records.VersionIn, request: Request,
                user: dict = Depends(require_permission("perioperative_record.read"))):
    return records.sign("perioperative", record_id, body.version, user, request)


@router.post("/records/{record_id}/addenda")
def post_addendum(record_id: str, body: records.AddendumIn, request: Request,
                  user: dict = Depends(require_permission("perioperative_record.write"))):
    return records.addendum("perioperative", record_id, body, user, request)


@router.get("/records/{record_id}/history")
def get_history(record_id: str, request: Request, offset: int = Query(0, ge=0),
                user: dict = Depends(require_permission("perioperative_record.read"))):
    return records.history("perioperative", record_id, user, offset, request)
