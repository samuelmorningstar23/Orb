"""Pharmacy masters: endpoints under /api/pharmacy-store (grn-5).

The package router in routes/pharmacy_store/__init__.py adds the
/api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this router has
neither, so every endpoint here inherits both (404 module_disabled while the
module is off, after a 401 for a caller with no session).

Tiers, built once at import as procurement_routes does:
  read     pharmacy_store.read                 lists and single records
  write    pharmacy_store.read + .write        drug fields, classification,
                                               par levels, vendors, licences
  approve  pharmacy_store.read + .approve      stores, deciding a downgrade

APPROVE DOES NOT IMPLY WRITE: the person who approves a classification
downgrade is never the one who asked for it, and the service checks that by
person as well as by tier. The rules themselves (identity fields locked,
two-person downgrade, no downgrade while NDPS vault stock exists, legacy-lot
adoption, no deactivating a store that holds stock) live in
services/pharmacy_master_service.py. Every handler is a plain def.

Errors: a field that may not change, or a value outside its set, is 422; an
unknown id is 404; the same person deciding their own request is 403; a
pending, stale or stock-blocked change is 409.
"""
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from permissions import (
    PHARMACY_STORE_APPROVE,
    PHARMACY_STORE_READ,
    PHARMACY_STORE_WRITE,
    require_permission,
)
from services import pharmacy_master_service as masters

router = APIRouter()

_PS_READ = require_permission(PHARMACY_STORE_READ)
_PS_WRITE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_WRITE)
_PS_APPROVE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_APPROVE)

_STATUS = {
    "not_found": 404,
    "same_person": 403,
    "not_requester": 403,
    "request_pending": 409,
    "request_stale": 409,
    "vault_stock": 409,
    "decided": 409,
    "already_exists": 409,
    "dispensing_store_exists": 409,
    "store_kind_fixed": 409,
    "store_holds_stock": 409,
}


def _fail(exc: masters.MasterError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.code, 422),
                         detail={"error": exc.code, "message": str(exc)})


def _ctx(request: Request) -> dict:
    return {"request_path": request.url.path, "http_method": request.method,
            "ip_address": request.client.host if request.client else None}


# ── Drugs ─────────────────────────────────────────────────────────────────────

@router.get("/drugs")
def list_drugs(search: Optional[str] = Query(None, max_length=120),
               include_inactive: bool = True,
               user: dict = Depends(_PS_READ)):
    """The drug master with its commercial and regulatory fields. A drug with
    no schedule or NDPS class reads 'Not classified', never 'none'."""
    return {"drugs": masters.list_drugs(search=search, include_inactive=include_inactive),
            "options": masters.options()}


@router.get("/drugs/{drug_id}")
def get_drug(drug_id: str, user: dict = Depends(_PS_READ)):
    drug = masters.get_drug(drug_id)
    if drug is None:
        raise HTTPException(status_code=404, detail={"error": "not_found",
                                                     "message": f"no drug {drug_id}"})
    return {"drug": drug,
            "requests": masters.list_change_requests(decision=None, drug_id=drug_id)}


@router.patch("/drugs/{drug_id}")
def update_drug(drug_id: str, request: Request, payload: dict = Body(...),
                user: dict = Depends(_PS_WRITE)):
    """Change a drug's pack size, units, HSN code, GST rate, manufacturer,
    storage condition, reorder defaults or active flag. Name, generic name,
    form and strength are refused (422)."""
    try:
        return {"drug": masters.update_drug_fields(drug_id, payload, actor=user,
                                                   audit_ctx=_ctx(request))}
    except masters.MasterError as exc:
        raise _fail(exc)


class ClassificationIn(BaseModel):
    drug_schedule: Optional[str] = None
    ndps_category: Optional[str] = None
    is_essential_narcotic: Optional[bool] = None
    reason: Optional[str] = None


@router.post("/drugs/{drug_id}/classification")
def classify_drug(drug_id: str, body: ClassificationIn, request: Request,
                  user: dict = Depends(_PS_WRITE)):
    """Classify a drug. A change that adds control applies now; one that
    removes control waits for a different approver (GRN-R20) and is refused
    while NDPS vault stock exists."""
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items()
               if k != "reason" and v is not None}
    try:
        return masters.classify_drug(drug_id, changes, actor=user, reason=body.reason,
                                     audit_ctx=_ctx(request))
    except masters.MasterError as exc:
        raise _fail(exc)


@router.get("/classification-requests")
def list_classification_requests(decision: Optional[str] = Query("pending"),
                                 drug_id: Optional[str] = None,
                                 user: dict = Depends(_PS_READ)):
    try:
        return {"requests": masters.list_change_requests(decision=decision or None,
                                                         drug_id=drug_id)}
    except masters.MasterError as exc:
        raise _fail(exc)


class DecisionIn(BaseModel):
    decision: str
    reason: Optional[str] = None


@router.post("/classification-requests/{request_id}/decision")
def decide_classification_request(request_id: int, body: DecisionIn, request: Request,
                                  user: dict = Depends(_PS_APPROVE)):
    """Approve or refuse a pending downgrade. Never by the person who asked."""
    if body.decision not in ("approved", "refused"):
        raise HTTPException(status_code=422, detail={
            "error": "bad_value", "message": "the decision is approved or refused"})
    try:
        return {"request": masters.decide_change_request(
            request_id, approve=body.decision == "approved", actor=user, reason=body.reason,
            audit_ctx=_ctx(request))}
    except masters.MasterError as exc:
        raise _fail(exc)


@router.post("/classification-requests/{request_id}/withdraw")
def withdraw_classification_request(request_id: int, request: Request,
                                    user: dict = Depends(_PS_WRITE)):
    try:
        return {"request": masters.withdraw_change_request(request_id, actor=user,
                                                           audit_ctx=_ctx(request))}
    except masters.MasterError as exc:
        raise _fail(exc)


# ── Stores ────────────────────────────────────────────────────────────────────

@router.get("/stores")
def list_stores(include_inactive: bool = True, user: dict = Depends(_PS_READ)):
    return {"stores": masters.list_stores(include_inactive=include_inactive),
            "legacy_lots": masters.legacy_lot_count()}


class StoreIn(BaseModel):
    code: str
    name: str
    pharmacy_kind: str
    department: Optional[str] = None
    ward: Optional[str] = None
    incharge_user_id: Optional[str] = None


@router.post("/stores")
def create_store(body: StoreIn, request: Request, user: dict = Depends(_PS_APPROVE)):
    """Create a pharmacy store. The dispensing store adopts every legacy lot
    in the same transaction."""
    try:
        return masters.create_store(
            code=body.code, name=body.name, pharmacy_kind=body.pharmacy_kind,
            department=body.department, ward=body.ward,
            incharge_user_id=body.incharge_user_id, actor=user, audit_ctx=_ctx(request))
    except masters.MasterError as exc:
        raise _fail(exc)


class StoreUpdateIn(BaseModel):
    name: Optional[str] = None
    department: Optional[str] = None
    ward: Optional[str] = None
    incharge_user_id: Optional[str] = None
    is_active: Optional[bool] = None


@router.put("/stores/{code}")
def update_store(code: str, body: StoreUpdateIn, request: Request,
                 user: dict = Depends(_PS_APPROVE)):
    try:
        return masters.update_store(
            code, name=body.name, department=body.department, ward=body.ward,
            incharge_user_id=body.incharge_user_id, is_active=body.is_active, actor=user,
            audit_ctx=_ctx(request))
    except masters.MasterError as exc:
        raise _fail(exc)


# ── Par levels ────────────────────────────────────────────────────────────────

@router.get("/par-levels")
def list_par_levels(store_id: Optional[int] = None, user: dict = Depends(_PS_READ)):
    return {"par_levels": masters.list_par_levels(store_id=store_id)}


class ParLevelIn(BaseModel):
    store_id: int
    helix_medication_id: str
    par_units: Any
    reorder_level_units: Any = None
    max_units: Any = None
    is_active: Any = True


@router.put("/par-levels")
def upsert_par_level(body: ParLevelIn, request: Request, user: dict = Depends(_PS_WRITE)):
    try:
        return {"par_level": masters.upsert_par_level(
            store_id=body.store_id, helix_medication_id=body.helix_medication_id,
            par_units=body.par_units, reorder_level_units=body.reorder_level_units,
            max_units=body.max_units, is_active=body.is_active, actor=user,
            audit_ctx=_ctx(request))}
    except masters.MasterError as exc:
        raise _fail(exc)


# ── Vendors ───────────────────────────────────────────────────────────────────

@router.get("/vendors")
def list_vendors(search: Optional[str] = Query(None, max_length=120),
                 include_inactive: bool = True, user: dict = Depends(_PS_READ)):
    """Vendors with their drug licence status ('none', 'expired', 'expiring',
    'valid'), judged on the hospital's date. No bank details."""
    return {"vendors": masters.list_vendors(search=search, include_inactive=include_inactive)}


@router.post("/vendors")
def create_vendor(request: Request, payload: dict = Body(...),
                  user: dict = Depends(_PS_WRITE)):
    """Add a vendor through the shared vendor core, so the pharmacy store works
    with Procurement switched off. A code already in use is refused (409)."""
    try:
        return {"vendor": masters.create_vendor(payload, actor=user, audit_ctx=_ctx(request))}
    except masters.MasterError as exc:
        raise _fail(exc)


@router.put("/vendors/{code}")
def update_vendor(code: str, request: Request, payload: dict = Body(...),
                  user: dict = Depends(_PS_WRITE)):
    try:
        return {"vendor": masters.update_vendor(code, payload, actor=user,
                                                audit_ctx=_ctx(request))}
    except masters.MasterError as exc:
        raise _fail(exc)


class LicenceIn(BaseModel):
    drug_licence_numbers: Optional[str] = None
    drug_licence_valid_to: Optional[str] = None
    ndps_licence_number: Optional[str] = None


@router.put("/vendors/{code}/drug-licence")
def set_drug_licence(code: str, body: LicenceIn, request: Request,
                     user: dict = Depends(_PS_WRITE)):
    try:
        return {"vendor": masters.set_drug_licence(
            code, drug_licence_numbers=body.drug_licence_numbers,
            drug_licence_valid_to=body.drug_licence_valid_to,
            ndps_licence_number=body.ndps_licence_number, actor=user,
            audit_ctx=_ctx(request))}
    except masters.MasterError as exc:
        raise _fail(exc)
