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

PER FACILITY (multisite-8h3, ruling MS-R9 (a)). With more than one facility on
record, the stores, par levels and legacy-lot count are the caller's
facilities' only; a store or par level of another facility is 404 after an
authorization 'facility_document_denied' row (services/pharmacy_facility.gate);
a store the caller would create at another facility is 403 facility_scope.
The drug master and vendors stay group-wide. With one facility on record the
caller's scope is None and nothing here changes.

DRUG IDENTITY (grn-6, ruling GRN-RS1 option (b)). Adding a drug, asking for an
identity correction, retiring a drug and the second pharmacist's decision are
all the WRITE tier: identity is a pharmacist's call, never the approve tier's.
The decider is never the requester (403), approving names the identity the
decider checked (409 request_stale when it moved), a duplicate name or an
unacknowledged same generic name, strength and form is 409, and a drug in use
is never rewritten (422 identity_locked). An audit outage is 503
audit_unavailable and nothing changed.
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from permissions import (
    PHARMACY_STORE_APPROVE,
    PHARMACY_STORE_READ,
    PHARMACY_STORE_WRITE,
    facility_scope,
    require_permission,
)
from pydantic import BaseModel, Field
from services.audit_service import AuditWriteError

from services import pharmacy_facility
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
    "same_identity_exists": 409,
    "dispensing_store_exists": 409,
    "store_kind_fixed": 409,
    "store_holds_stock": 409,
    "facility_scope": 403,
}


def _fail(exc: masters.MasterError) -> HTTPException:
    detail = {"error": exc.code, "message": str(exc)}
    extra = getattr(exc, "detail", None) or {}
    detail.update({k: v for k, v in extra.items() if k not in detail})
    return HTTPException(status_code=_STATUS.get(exc.code, 422), detail=detail)


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": "The audit record could not be written, so nothing was changed. "
            "Try again shortly.",
        },
    )


def _ctx(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
    }


# ── Drugs ─────────────────────────────────────────────────────────────────────


_LIST_DRUGS_SEARCH_DEFAULT = Query(None, max_length=120)
_LIST_DRUGS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/drugs")
def list_drugs(
    search: str | None = _LIST_DRUGS_SEARCH_DEFAULT,
    include_inactive: bool = True,
    user: dict = _LIST_DRUGS_USER_DEFAULT,
):
    """The drug master with its commercial and regulatory fields. A drug with
    no schedule or NDPS class reads 'Not classified', never 'none'."""
    return {
        "drugs": masters.list_drugs(search=search, include_inactive=include_inactive),
        "options": masters.options(),
    }


_GET_DRUG_USER_DEFAULT = Depends(_PS_READ)


@router.get("/drugs/{drug_id}")
def get_drug(drug_id: str, user: dict = _GET_DRUG_USER_DEFAULT):
    drug = masters.get_drug(drug_id)
    if drug is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": f"no drug {drug_id}"}
        )
    return {"drug": drug, "requests": masters.list_change_requests(decision=None, drug_id=drug_id)}


_UPDATE_DRUG_PAYLOAD_DEFAULT = Body(...)
_UPDATE_DRUG_USER_DEFAULT = Depends(_PS_WRITE)


@router.patch("/drugs/{drug_id}")
def update_drug(
    drug_id: str,
    request: Request,
    payload: dict = _UPDATE_DRUG_PAYLOAD_DEFAULT,
    user: dict = _UPDATE_DRUG_USER_DEFAULT,
):
    """Change a drug's pack size, units, HSN code, GST rate, manufacturer,
    storage condition, reorder defaults or active flag. Name, generic name,
    form and strength are refused (422)."""
    try:
        return {
            "drug": masters.update_drug_fields(
                drug_id, payload, actor=user, audit_ctx=_ctx(request)
            )
        }
    except masters.MasterError as exc:
        raise _fail(exc) from exc


class ClassificationIn(BaseModel):
    drug_schedule: str | None = None
    ndps_category: str | None = None
    is_essential_narcotic: bool | None = None
    reason: str | None = None


_CLASSIFY_DRUG_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/drugs/{drug_id}/classification")
def classify_drug(
    drug_id: str, body: ClassificationIn, request: Request, user: dict = _CLASSIFY_DRUG_USER_DEFAULT
):
    """Classify a drug. A change that adds control applies now; one that
    removes control waits for a different approver (GRN-R20) and is refused
    while NDPS vault stock exists."""
    changes = {
        k: v
        for k, v in body.model_dump(exclude_unset=True).items()
        if k != "reason" and v is not None
    }
    try:
        return masters.classify_drug(
            drug_id, changes, actor=user, reason=body.reason, audit_ctx=_ctx(request)
        )
    except masters.MasterError as exc:
        raise _fail(exc) from exc


_LIST_CLASSIFICATION_REQUESTS_DECISION_DEFAULT = Query("pending")
_LIST_CLASSIFICATION_REQUESTS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/classification-requests")
def list_classification_requests(
    decision: str | None = _LIST_CLASSIFICATION_REQUESTS_DECISION_DEFAULT,
    drug_id: str | None = None,
    user: dict = _LIST_CLASSIFICATION_REQUESTS_USER_DEFAULT,
):
    """Classification changes only: identity requests are on /identity-requests,
    where a second pharmacist decides them."""
    try:
        return {
            "requests": masters.list_change_requests(
                decision=decision or None,
                drug_id=drug_id,
                kinds=masters.CLASSIFICATION_REQUEST_KINDS,
            )
        }
    except masters.MasterError as exc:
        raise _fail(exc) from exc


# ── Drug identity (grn-6) ─────────────────────────────────────────────────────


class IdentityIn(BaseModel):
    name: str | None = Field(None, max_length=400)
    generic_name: str | None = Field(None, max_length=400)
    form: str | None = Field(None, max_length=200)
    strength: str | None = Field(None, max_length=200)


def _identity(body: IdentityIn) -> dict:
    return {
        "name": body.name,
        "generic_name": body.generic_name,
        "form": body.form,
        "strength": body.strength,
    }


_PREVIEW_DRUG_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/drugs/preview")
def preview_drug(body: IdentityIn, user: dict = _PREVIEW_DRUG_USER_DEFAULT):
    """What the allergy and interaction screens will make of this identity,
    and any drug with the same name or the same generic name, strength and
    form. Saves nothing."""
    try:
        return masters.preview_drug(_identity(body))
    except masters.MasterError as exc:
        raise _fail(exc) from exc


class NewDrugIn(IdentityIn):
    another_brand: bool = False
    note: str | None = Field(None, max_length=1000)


_CREATE_DRUG_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/drugs")
def create_drug(body: NewDrugIn, request: Request, user: dict = _CREATE_DRUG_USER_DEFAULT):
    """Add a drug as a draft. It cannot be ordered until a different
    pharmacist confirms it."""
    try:
        return masters.create_drug(
            _identity(body),
            actor=user,
            acknowledge_same_identity=body.another_brand,
            note=body.note,
            audit_ctx=_ctx(request),
        )
    except masters.MasterError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class CorrectionIn(IdentityIn):
    reason: str | None = Field(None, max_length=1000)
    another_brand: bool = False


_REQUEST_IDENTITY_CORRECTION_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/drugs/{drug_id}/identity-correction")
def request_identity_correction(
    drug_id: str,
    body: CorrectionIn,
    request: Request,
    user: dict = _REQUEST_IDENTITY_CORRECTION_USER_DEFAULT,
):
    """Ask a second pharmacist to correct a confirmed drug that nothing names
    yet. A drug with orders, stock or purchase lines is retired and replaced
    instead (422 identity_locked)."""
    try:
        return masters.request_identity_correction(
            drug_id,
            _identity(body),
            actor=user,
            reason=body.reason,
            acknowledge_same_identity=body.another_brand,
            audit_ctx=_ctx(request),
        )
    except masters.MasterError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class RetireIn(BaseModel):
    reason: str | None = Field(None, max_length=1000)
    replacement_drug_id: str | None = Field(None, max_length=80)


_RETIRE_DRUG_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/drugs/{drug_id}/retire")
def retire_drug(
    drug_id: str, body: RetireIn, request: Request, user: dict = _RETIRE_DRUG_USER_DEFAULT
):
    """Take a confirmed drug out of ordering now, with a reason. Its orders,
    receipts and ledger keep naming it."""
    try:
        return masters.retire_drug(
            drug_id,
            actor=user,
            reason=body.reason,
            replacement_drug_id=body.replacement_drug_id,
            audit_ctx=_ctx(request),
        )
    except masters.MasterError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_LIST_IDENTITY_REQUESTS_DECISION_DEFAULT = Query("pending")
_LIST_IDENTITY_REQUESTS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/identity-requests")
def list_identity_requests(
    decision: str | None = _LIST_IDENTITY_REQUESTS_DECISION_DEFAULT,
    user: dict = _LIST_IDENTITY_REQUESTS_USER_DEFAULT,
):
    """New drugs and identity corrections waiting for a second pharmacist,
    each with what it screens as."""
    try:
        return {"requests": masters.list_identity_requests(decision=decision or None)}
    except masters.MasterError as exc:
        raise _fail(exc) from exc


class IdentityDecisionIn(BaseModel):
    decision: str
    reason: str | None = Field(None, max_length=1000)
    expected: IdentityIn | None = None


_DECIDE_IDENTITY_REQUEST_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/identity-requests/{request_id}/decision")
def decide_identity_request(
    request_id: int,
    body: IdentityDecisionIn,
    request: Request,
    user: dict = _DECIDE_IDENTITY_REQUEST_USER_DEFAULT,
):
    """A second pharmacist confirms or refuses. Never the requester (403).
    Confirming names the identity that was checked."""
    if body.decision not in ("approved", "refused"):
        raise HTTPException(
            status_code=422,
            detail={"error": "bad_value", "message": "the decision is approved or refused"},
        )
    try:
        return masters.decide_identity_request(
            request_id,
            approve=body.decision == "approved",
            actor=user,
            reason=body.reason,
            expected=_identity(body.expected) if body.expected is not None else None,
            audit_ctx=_ctx(request),
        )
    except masters.MasterError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_WITHDRAW_IDENTITY_REQUEST_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/identity-requests/{request_id}/withdraw")
def withdraw_identity_request(
    request_id: int, request: Request, user: dict = _WITHDRAW_IDENTITY_REQUEST_USER_DEFAULT
):
    """The requester takes their own request back. A withdrawn new drug is
    retired, never ordered."""
    try:
        return masters.withdraw_identity_request(request_id, actor=user, audit_ctx=_ctx(request))
    except masters.MasterError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class DecisionIn(BaseModel):
    decision: str
    reason: str | None = None


_DECIDE_CLASSIFICATION_REQUEST_USER_DEFAULT = Depends(_PS_APPROVE)


@router.post("/classification-requests/{request_id}/decision")
def decide_classification_request(
    request_id: int,
    body: DecisionIn,
    request: Request,
    user: dict = _DECIDE_CLASSIFICATION_REQUEST_USER_DEFAULT,
):
    """Approve or refuse a pending downgrade. Never by the person who asked."""
    if body.decision not in ("approved", "refused"):
        raise HTTPException(
            status_code=422,
            detail={"error": "bad_value", "message": "the decision is approved or refused"},
        )
    try:
        return {
            "request": masters.decide_change_request(
                request_id,
                approve=body.decision == "approved",
                actor=user,
                reason=body.reason,
                audit_ctx=_ctx(request),
            )
        }
    except masters.MasterError as exc:
        raise _fail(exc) from exc


_WITHDRAW_CLASSIFICATION_REQUEST_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/classification-requests/{request_id}/withdraw")
def withdraw_classification_request(
    request_id: int, request: Request, user: dict = _WITHDRAW_CLASSIFICATION_REQUEST_USER_DEFAULT
):
    try:
        return {
            "request": masters.withdraw_change_request(
                request_id, actor=user, audit_ctx=_ctx(request)
            )
        }
    except masters.MasterError as exc:
        raise _fail(exc) from exc


# ── Stores ────────────────────────────────────────────────────────────────────


_LIST_STORES_USER_DEFAULT = Depends(_PS_READ)


@router.get("/stores")
def list_stores(include_inactive: bool = True, user: dict = _LIST_STORES_USER_DEFAULT) -> dict:
    """The pharmacy stores (the caller's facilities' only, once more than one
    facility is on record) and the legacy lots still waiting for a store."""
    facilities = facility_scope(user)
    return {
        "stores": masters.list_stores(include_inactive=include_inactive, facilities=facilities),
        "legacy_lots": masters.legacy_lot_count(facilities=facilities),
    }


class StoreIn(BaseModel):
    code: str
    name: str
    pharmacy_kind: str
    department: str | None = None
    ward: str | None = None
    incharge_user_id: str | None = None


_CREATE_STORE_USER_DEFAULT = Depends(_PS_APPROVE)


@router.post("/stores")
def create_store(body: StoreIn, request: Request, user: dict = _CREATE_STORE_USER_DEFAULT) -> dict:
    """Create a pharmacy store. The dispensing store adopts every legacy lot
    (of its own facility) in the same transaction. A store the caller would
    create at another facility is 403 facility_scope."""
    try:
        return masters.create_store(
            code=body.code,
            name=body.name,
            pharmacy_kind=body.pharmacy_kind,
            department=body.department,
            ward=body.ward,
            incharge_user_id=body.incharge_user_id,
            actor=user,
            audit_ctx=_ctx(request),
            facilities=facility_scope(user),
        )
    except masters.MasterError as exc:
        raise _fail(exc) from exc


class StoreUpdateIn(BaseModel):
    name: str | None = None
    department: str | None = None
    ward: str | None = None
    incharge_user_id: str | None = None
    is_active: bool | None = None


_UPDATE_STORE_USER_DEFAULT = Depends(_PS_APPROVE)


@router.put("/stores/{code}")
def update_store(
    code: str, body: StoreUpdateIn, request: Request, user: dict = _UPDATE_STORE_USER_DEFAULT
) -> dict:
    """Edit a pharmacy store; a store of another facility is 404."""
    pharmacy_facility.gate(user, "store_code", code, request, message=f"no pharmacy store {code}")
    try:
        return masters.update_store(
            code,
            name=body.name,
            department=body.department,
            ward=body.ward,
            incharge_user_id=body.incharge_user_id,
            is_active=body.is_active,
            actor=user,
            audit_ctx=_ctx(request),
        )
    except masters.MasterError as exc:
        raise _fail(exc) from exc


# ── Par levels ────────────────────────────────────────────────────────────────


_LIST_PAR_LEVELS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/par-levels")
def list_par_levels(
    request: Request, store_id: int | None = None, user: dict = _LIST_PAR_LEVELS_USER_DEFAULT
) -> dict:
    """Par levels of the caller's facilities' stores; a named store of
    another facility is 404."""
    if store_id is not None:
        pharmacy_facility.gate(
            user, "store", store_id, request, message=f"no pharmacy store {store_id}"
        )
    return {
        "par_levels": masters.list_par_levels(store_id=store_id, facilities=facility_scope(user))
    }


class ParLevelIn(BaseModel):
    store_id: int
    helix_medication_id: str
    par_units: Any
    reorder_level_units: Any = None
    max_units: Any = None
    is_active: Any = True


_UPSERT_PAR_LEVEL_USER_DEFAULT = Depends(_PS_WRITE)


@router.put("/par-levels")
def upsert_par_level(
    body: ParLevelIn, request: Request, user: dict = _UPSERT_PAR_LEVEL_USER_DEFAULT
) -> dict:
    """Set one drug's levels in one store; a store of another facility is 404."""
    pharmacy_facility.gate(
        user, "store", body.store_id, request, message=f"no pharmacy store {body.store_id}"
    )
    try:
        return {
            "par_level": masters.upsert_par_level(
                store_id=body.store_id,
                helix_medication_id=body.helix_medication_id,
                par_units=body.par_units,
                reorder_level_units=body.reorder_level_units,
                max_units=body.max_units,
                is_active=body.is_active,
                actor=user,
                audit_ctx=_ctx(request),
            )
        }
    except masters.MasterError as exc:
        raise _fail(exc) from exc


# ── Vendors ───────────────────────────────────────────────────────────────────


_LIST_VENDORS_SEARCH_DEFAULT = Query(None, max_length=120)
_LIST_VENDORS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/vendors")
def list_vendors(
    search: str | None = _LIST_VENDORS_SEARCH_DEFAULT,
    include_inactive: bool = True,
    user: dict = _LIST_VENDORS_USER_DEFAULT,
):
    """Vendors with their drug licence status ('none', 'expired', 'expiring',
    'valid'), judged on the hospital's date. No bank details."""
    return {"vendors": masters.list_vendors(search=search, include_inactive=include_inactive)}


_CREATE_VENDOR_PAYLOAD_DEFAULT = Body(...)
_CREATE_VENDOR_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/vendors")
def create_vendor(
    request: Request,
    payload: dict = _CREATE_VENDOR_PAYLOAD_DEFAULT,
    user: dict = _CREATE_VENDOR_USER_DEFAULT,
):
    """Add a vendor through the shared vendor core, so the pharmacy store works
    with Procurement switched off. A code already in use is refused (409)."""
    try:
        return {"vendor": masters.create_vendor(payload, actor=user, audit_ctx=_ctx(request))}
    except masters.MasterError as exc:
        raise _fail(exc) from exc


_UPDATE_VENDOR_PAYLOAD_DEFAULT = Body(...)
_UPDATE_VENDOR_USER_DEFAULT = Depends(_PS_WRITE)


@router.put("/vendors/{code}")
def update_vendor(
    code: str,
    request: Request,
    payload: dict = _UPDATE_VENDOR_PAYLOAD_DEFAULT,
    user: dict = _UPDATE_VENDOR_USER_DEFAULT,
):
    try:
        return {"vendor": masters.update_vendor(code, payload, actor=user, audit_ctx=_ctx(request))}
    except masters.MasterError as exc:
        raise _fail(exc) from exc


class LicenceIn(BaseModel):
    drug_licence_numbers: str | None = None
    drug_licence_valid_to: str | None = None
    ndps_licence_number: str | None = None


_SET_DRUG_LICENCE_USER_DEFAULT = Depends(_PS_WRITE)


@router.put("/vendors/{code}/drug-licence")
def set_drug_licence(
    code: str, body: LicenceIn, request: Request, user: dict = _SET_DRUG_LICENCE_USER_DEFAULT
):
    try:
        return {
            "vendor": masters.set_drug_licence(
                code,
                drug_licence_numbers=body.drug_licence_numbers,
                drug_licence_valid_to=body.drug_licence_valid_to,
                ndps_licence_number=body.ndps_licence_number,
                actor=user,
                audit_ctx=_ctx(request),
            )
        }
    except masters.MasterError as exc:
        raise _fail(exc) from exc
