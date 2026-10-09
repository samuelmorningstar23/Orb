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
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from permissions import PHARMACY_STORE_NDPS, PHARMACY_STORE_READ, require_permission
from services import ndps_service as ndps
from services.audit_service import AuditWriteError

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
    return HTTPException(status_code=503, detail={
        "error": "audit_unavailable",
        "message": "The audit record could not be written, so nothing was shown or changed. "
                   "Try again shortly."})


def _ctx(request: Request) -> dict:
    return {"request_path": request.url.path, "http_method": request.method,
            "ip_address": request.client.host if request.client else None}


# ── Reads ─────────────────────────────────────────────────────────────────────

@router.get("/ndps/options")
def ndps_options(user: dict = Depends(_PS_NDPS)):
    return ndps.options(user)


@router.get("/ndps/balances")
def ndps_balances(store_id: Optional[int] = Query(None, ge=1),
                  drug_id: Optional[str] = Query(None, max_length=80),
                  user: dict = Depends(_PS_NDPS)):
    """No patient data: drug, store, the register's balance and the shelf's."""
    return ndps.balances(store_id=store_id, drug_id=drug_id or None)


@router.get("/ndps/register")
def ndps_register(request: Request,
                  store_id: Optional[int] = Query(None, ge=1),
                  drug_id: Optional[str] = Query(None, max_length=80),
                  before_id: Optional[int] = Query(None, ge=1),
                  limit: int = Query(100, ge=1, le=ndps.MAX_PAGE),
                  user: dict = Depends(_PS_NDPS)):
    try:
        return ndps.register_page(actor=user, store_id=store_id, drug_id=drug_id or None,
                                  before_id=before_id, limit=limit, audit_ctx=_ctx(request))
    except ndps.NdpsError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


@router.get("/ndps/variances")
def ndps_variances(status: Optional[str] = Query("open", max_length=20),
                   limit: int = Query(200, ge=1, le=500),
                   user: dict = Depends(_PS_NDPS)):
    try:
        return ndps.list_variances(status=status, limit=limit)
    except ndps.NdpsError as exc:
        raise _fail(exc)


@router.get("/ndps/export.csv")
def ndps_export(request: Request,
                store_id: Optional[int] = Query(None, ge=1),
                drug_id: Optional[str] = Query(None, max_length=80),
                date_from: Optional[str] = Query(None, alias="from", max_length=10),
                date_to: Optional[str] = Query(None, alias="to", max_length=10),
                user: dict = Depends(_PS_NDPS)):
    """The register as CSV. Audited ('security', fail-closed) before the file
    is sent: no audit row, no file."""
    try:
        filename, text = ndps.export_csv(actor=user, store_id=store_id,
                                         drug_id=drug_id or None, date_from=date_from,
                                         date_to=date_to, audit_ctx=_ctx(request))
    except ndps.NdpsError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()
    return Response(content=text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})


# ── Writes ────────────────────────────────────────────────────────────────────

class WitnessIn(BaseModel):
    witness_id: Optional[str] = Field(None, max_length=80)
    witness_pin: Optional[str] = Field(None, max_length=64)


class OpeningIn(WitnessIn):
    reason: Optional[str] = Field(None, max_length=ndps.MAX_TEXT)
    store_id: Optional[int] = Field(None, ge=1)
    drug_id: Optional[str] = Field(None, max_length=80)


@router.post("/ndps/openings")
def ndps_open(body: OpeningIn, request: Request, user: dict = Depends(_PS_NDPS)):
    """Open (or open again) the register from what the shelf holds, witnessed."""
    try:
        return ndps.open_register(actor=user, reason=body.reason, witness_id=body.witness_id,
                                  witness_pin=body.witness_pin, store_id=body.store_id,
                                  drug_id=body.drug_id or None, audit_ctx=_ctx(request))
    except ndps.NdpsError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/ndps/reconcile")
def ndps_reconcile(request: Request, user: dict = Depends(_PS_NDPS)):
    try:
        return ndps.raise_reconciliation_variances(actor=user, audit_ctx=_ctx(request))
    except ndps.NdpsError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


class VarianceCloseIn(WitnessIn):
    note: Optional[str] = Field(None, max_length=ndps.MAX_TEXT)


@router.post("/ndps/variances/{variance_id}/reconcile")
def ndps_close_variance(variance_id: int, body: VarianceCloseIn, request: Request,
                        user: dict = Depends(_PS_NDPS)):
    try:
        return ndps.reconcile_variance(variance_id, actor=user, note=body.note,
                                       witness_id=body.witness_id,
                                       witness_pin=body.witness_pin, audit_ctx=_ctx(request))
    except ndps.NdpsError as exc:
        raise _fail(exc)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/ndps/variances/{variance_id}/incident")
def ndps_variance_incident(variance_id: int, request: Request,
                           user: dict = Depends(_PS_NDPS)):
    from services.nabh_service import NabhError

    try:
        return ndps.draft_incident(variance_id, actor=user, audit_ctx=_ctx(request))
    except ndps.NdpsError as exc:
        raise _fail(exc)
    except NabhError as exc:
        raise HTTPException(status_code=422, detail={"error": exc.code, "message": str(exc)})
    except AuditWriteError:
        raise _audit_unavailable()
