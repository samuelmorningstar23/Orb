"""The NDPS register: endpoints under /api/pharmacy-store (grn-13, W7-G).

The package router in routes/pharmacy_store/__init__.py adds the
/api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this router has
neither, so every endpoint added here inherits both (404 module_disabled while
the module is off, after a 401 for a caller with no session).

Every endpoint needs pharmacy_store.read AND pharmacy_store.ndps:
  GET  /ndps/options                     the switch, mode, lane, stores, drugs, witnesses
  GET  /ndps/balances                    the register against the shelf, per drug and store
  GET  /ndps/register                    entries, newest first (names patients: audited)
  GET  /ndps/variances                   ?status=open|reconciled|all
  GET  /ndps/export.csv                  the register as CSV (audited before it is sent)
  POST /ndps/openings                    open the register from the shelf, witnessed
  POST /ndps/reconcile                   raise a variance for every mismatch
  POST /ndps/variances/{variance_id}/reconcile   close a variance, witnessed
  POST /ndps/variances/{variance_id}/incident    report it to the incident register

The register writes nothing until a site sets ORB_PHARMACY_NDPS_ON_FAILURE
('block' or 'record', ruling GRN-RS7, held for Samuel) as well as
ORB_PHARMACY_NDPS_REGISTER; the reads still answer (an empty register, or
entries written while it was on). Writes that need the register on answer 409
register_off. The rules live in services/ndps_service.py.

Errors: an unknown variance or store is 404; a missing NDPS permission or a
witness refusal is 403; a locked witness PIN is 429; the register off, a
variance already reconciled or reported, or nothing to open is 409; an audit
outage is 503 audit_unavailable and nothing was returned or changed; anything
the request must correct is 422.

PER FACILITY (multisite-8h3, ruling MS-R9 (a)). With more than one facility on
record, every read and action covers the caller's facilities' stores only, and
a store or variance of another facility is 404 after an authorization
'facility_document_denied' row. With one facility on record nothing here
changes.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from permissions import (
    PHARMACY_STORE_NDPS,
    PHARMACY_STORE_READ,
    facility_scope,
    require_permission,
)
from pydantic import BaseModel, Field
from services.audit_service import AuditWriteError

from services import ndps_service as ndps
from services import pharmacy_facility


def _store_gate(user: dict, store_id: int | None, request: Request) -> None:
    """404 for a named store of another facility (multisite-8h3)."""
    if store_id is not None:
        pharmacy_facility.gate(
            user, "store", store_id, request, message=f"no pharmacy store {store_id}"
        )


router = APIRouter()

_PS_NDPS = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_NDPS)

_STATUS = {
    "not_found": 404,
    "ndps_permission_needed": 403,
    "witness_same_person": 403,
    "witness_not_eligible": 403,
    "witness_no_pin": 403,
    "witness_pin_wrong": 403,
    "witness_locked": 429,
    "register_off": 409,
    "already_reconciled": 409,
    "already_reported": 409,
    "nothing_to_open": 409,
    "incident_register_off": 409,
}


def _fail(exc: ndps.NdpsError) -> HTTPException:
    detail = {"error": exc.code, "message": str(exc)}
    if getattr(exc, "detail", None):
        detail.update({k: v for k, v in exc.detail.items() if k not in detail})
    return HTTPException(status_code=_STATUS.get(exc.code, 422), detail=detail)


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": "The audit record could not be written, so nothing was shown or changed. "
            "Try again shortly.",
        },
    )


def _ctx(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
    }


# ── Reads ─────────────────────────────────────────────────────────────────────


_NDPS_OPTIONS_USER_DEFAULT = Depends(_PS_NDPS)


@router.get("/ndps/options")
def ndps_options(user: dict = _NDPS_OPTIONS_USER_DEFAULT):
    return ndps.options(user)


_NDPS_BALANCES_STORE_ID_DEFAULT = Query(None, ge=1)
_NDPS_BALANCES_DRUG_ID_DEFAULT = Query(None, max_length=80)
_NDPS_BALANCES_USER_DEFAULT = Depends(_PS_NDPS)


@router.get("/ndps/balances")
def ndps_balances(
    request: Request,
    store_id: int | None = _NDPS_BALANCES_STORE_ID_DEFAULT,
    drug_id: str | None = _NDPS_BALANCES_DRUG_ID_DEFAULT,
    user: dict = _NDPS_BALANCES_USER_DEFAULT,
) -> dict:
    """No patient data: drug, store, the register's balance and the shelf's
    (the caller's facilities' stores)."""
    _store_gate(user, store_id, request)
    return ndps.balances(
        store_id=store_id, drug_id=drug_id or None, facilities=facility_scope(user)
    )


_NDPS_REGISTER_STORE_ID_DEFAULT = Query(None, ge=1)
_NDPS_REGISTER_DRUG_ID_DEFAULT = Query(None, max_length=80)
_NDPS_REGISTER_BEFORE_ID_DEFAULT = Query(None, ge=1)
_NDPS_REGISTER_LIMIT_DEFAULT = Query(100, ge=1, le=ndps.MAX_PAGE)
_NDPS_REGISTER_USER_DEFAULT = Depends(_PS_NDPS)


@router.get("/ndps/register")
def ndps_register(
    request: Request,
    store_id: int | None = _NDPS_REGISTER_STORE_ID_DEFAULT,
    drug_id: str | None = _NDPS_REGISTER_DRUG_ID_DEFAULT,
    before_id: int | None = _NDPS_REGISTER_BEFORE_ID_DEFAULT,
    limit: int = _NDPS_REGISTER_LIMIT_DEFAULT,
    user: dict = _NDPS_REGISTER_USER_DEFAULT,
) -> dict:
    """Register entries, newest first (the caller's facilities' stores)."""
    _store_gate(user, store_id, request)
    try:
        return ndps.register_page(
            actor=user,
            store_id=store_id,
            drug_id=drug_id or None,
            before_id=before_id,
            limit=limit,
            audit_ctx=_ctx(request),
        )
    except ndps.NdpsError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_NDPS_VARIANCES_STATUS_DEFAULT = Query("open", max_length=20)
_NDPS_VARIANCES_LIMIT_DEFAULT = Query(200, ge=1, le=500)
_NDPS_VARIANCES_USER_DEFAULT = Depends(_PS_NDPS)


@router.get("/ndps/variances")
def ndps_variances(
    status: str | None = _NDPS_VARIANCES_STATUS_DEFAULT,
    limit: int = _NDPS_VARIANCES_LIMIT_DEFAULT,
    user: dict = _NDPS_VARIANCES_USER_DEFAULT,
) -> dict:
    """Variances, newest first (the caller's facilities' stores)."""
    try:
        return ndps.list_variances(status=status, limit=limit, facilities=facility_scope(user))
    except ndps.NdpsError as exc:
        raise _fail(exc) from exc


_NDPS_EXPORT_STORE_ID_DEFAULT = Query(None, ge=1)
_NDPS_EXPORT_DRUG_ID_DEFAULT = Query(None, max_length=80)
_NDPS_EXPORT_DATE_FROM_DEFAULT = Query(None, alias="from", max_length=10)
_NDPS_EXPORT_DATE_TO_DEFAULT = Query(None, alias="to", max_length=10)
_NDPS_EXPORT_USER_DEFAULT = Depends(_PS_NDPS)


@router.get("/ndps/export.csv")
def ndps_export(
    request: Request,
    store_id: int | None = _NDPS_EXPORT_STORE_ID_DEFAULT,
    drug_id: str | None = _NDPS_EXPORT_DRUG_ID_DEFAULT,
    date_from: str | None = _NDPS_EXPORT_DATE_FROM_DEFAULT,
    date_to: str | None = _NDPS_EXPORT_DATE_TO_DEFAULT,
    user: dict = _NDPS_EXPORT_USER_DEFAULT,
) -> Response:
    """The register as CSV. Audited ('security', fail-closed) before the file
    is sent: no audit row, no file. A store of another facility is 404."""
    _store_gate(user, store_id, request)
    try:
        filename, text = ndps.export_csv(
            actor=user,
            store_id=store_id,
            drug_id=drug_id or None,
            date_from=date_from,
            date_to=date_to,
            audit_ctx=_ctx(request),
        )
    except ndps.NdpsError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ── Writes ────────────────────────────────────────────────────────────────────


class WitnessIn(BaseModel):
    witness_id: str | None = Field(None, max_length=80)
    witness_pin: str | None = Field(None, max_length=64)


class OpeningIn(WitnessIn):
    reason: str | None = Field(None, max_length=ndps.MAX_TEXT)
    store_id: int | None = Field(None, ge=1)
    drug_id: str | None = Field(None, max_length=80)


_NDPS_OPEN_USER_DEFAULT = Depends(_PS_NDPS)


@router.post("/ndps/openings")
def ndps_open(body: OpeningIn, request: Request, user: dict = _NDPS_OPEN_USER_DEFAULT):
    """Open (or open again) the register from what the shelf holds, witnessed."""
    try:
        return ndps.open_register(
            actor=user,
            reason=body.reason,
            witness_id=body.witness_id,
            witness_pin=body.witness_pin,
            store_id=body.store_id,
            drug_id=body.drug_id or None,
            audit_ctx=_ctx(request),
        )
    except ndps.NdpsError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_NDPS_RECONCILE_USER_DEFAULT = Depends(_PS_NDPS)


@router.post("/ndps/reconcile")
def ndps_reconcile(request: Request, user: dict = _NDPS_RECONCILE_USER_DEFAULT):
    try:
        return ndps.raise_reconciliation_variances(actor=user, audit_ctx=_ctx(request))
    except ndps.NdpsError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class VarianceCloseIn(WitnessIn):
    note: str | None = Field(None, max_length=ndps.MAX_TEXT)


_NDPS_CLOSE_VARIANCE_USER_DEFAULT = Depends(_PS_NDPS)


@router.post("/ndps/variances/{variance_id}/reconcile")
def ndps_close_variance(
    variance_id: int,
    body: VarianceCloseIn,
    request: Request,
    user: dict = _NDPS_CLOSE_VARIANCE_USER_DEFAULT,
):
    try:
        return ndps.reconcile_variance(
            variance_id,
            actor=user,
            note=body.note,
            witness_id=body.witness_id,
            witness_pin=body.witness_pin,
            audit_ctx=_ctx(request),
        )
    except ndps.NdpsError as exc:
        raise _fail(exc) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


_NDPS_VARIANCE_INCIDENT_USER_DEFAULT = Depends(_PS_NDPS)


@router.post("/ndps/variances/{variance_id}/incident")
def ndps_variance_incident(
    variance_id: int, request: Request, user: dict = _NDPS_VARIANCE_INCIDENT_USER_DEFAULT
):
    from services.nabh_service import NabhError

    try:
        return ndps.draft_incident(variance_id, actor=user, audit_ctx=_ctx(request))
    except ndps.NdpsError as exc:
        raise _fail(exc) from exc
    except NabhError as exc:
        raise HTTPException(
            status_code=422, detail={"error": exc.code, "message": str(exc)}
        ) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None
