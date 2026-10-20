"""Returns to vendor: endpoints under /api/pharmacy-store (grn-22, W7-G).

The package router in routes/pharmacy_store/__init__.py adds the
/api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this router has
neither, so every endpoint added here inherits both (404 module_disabled while
the module is off, after a 401 for a caller with no session).

Endpoints (docs/his_design/grn.json, section E):
  read     pharmacy_store.read
    GET  /vendor-returns                   newest first; ?status=
    GET  /vendor-returns/options           stores, vendors, witnesses, window
    GET  /vendor-returns/stock?store_code= lots that may go back now
    GET  /vendor-returns/{return_id}       one return with its lines
  write    pharmacy_store.read + pharmacy_store.write
    POST /vendor-returns                   a draft (Idempotency-Key header or body)
    POST /vendor-returns/{return_id}/cancel    give up a draft, with a reason
  approve  pharmacy_store.read + pharmacy_store.approve
    POST /vendor-returns/{return_id}/post      send it: the units leave the shelf
  write or approve
    POST /vendor-returns/{return_id}/credit    the vendor's credit note number

The rules (held or near-expiry lots only, never more than a lot holds, the
lot's own vendor, the poster differs from the drafter, a narcotic or
psychotropic return needs the NDPS permission, the vendor's NDPS licence and a
third person as witness by PIN) live in services/pharmacy_purchasing_service.py,
which runs every action in one immediate transaction with its 'security' audit
row. The PHVR number is stamped after the post commits. No patient data is
read or returned.

Errors: an unknown return is 404; the drafter posting, a missing NDPS
permission or a witness refusal is 403; a locked witness PIN is 429; a move the
status does not allow, more units than the lot holds or a reused request key
is 409; an audit outage is 503 audit_unavailable and nothing was changed;
anything the request must correct is 422.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from permissions import (
    PHARMACY_STORE_APPROVE,
    PHARMACY_STORE_NDPS,
    PHARMACY_STORE_READ,
    PHARMACY_STORE_WRITE,
    has_permission,
    require_permission,
)
from pydantic import BaseModel, Field
from services.audit_service import AuditWriteError

from services import pharmacy_purchasing_service as purchasing

router = APIRouter()

_PS_READ = require_permission(PHARMACY_STORE_READ)
_PS_WRITE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_WRITE)
_PS_APPROVE = require_permission(PHARMACY_STORE_READ, PHARMACY_STORE_APPROVE)

_STATUS = {
    "not_found": 404,
    "same_person": 403,
    "ndps_permission_needed": 403,
    "witness_same_person": 403,
    "witness_not_eligible": 403,
    "witness_no_pin": 403,
    "witness_pin_wrong": 403,
    "witness_locked": 429,
    "illegal_transition": 409,
    "idempotency_conflict": 409,
    "already_credited": 409,
    "exceeds_lot": 409,
    "insufficient_stock": 409,
    "lot_changed": 409,
    "lot_status_conflict": 409,
    "store_inactive": 409,
}


def _audit_denied(request: Request, user: dict, missing: list[str]) -> None:
    """A refusal for want of a permission, audited as require_permission
    audits one. Authorization rows are fail-soft; the refusal stands."""
    from services.audit_service import log_audit_event

    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="permission_denied",
            outcome="denied",
            summary_message=f"Missing permissions: {' or '.join(missing)}",
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={"missing_permissions": missing, "user_role": user.get("role")},
        )
    except Exception:
        pass


def _fail(exc: purchasing.PurchasingError, request: Request, user: dict) -> HTTPException:
    if exc.code == "ndps_permission_needed":
        _audit_denied(request, user, [PHARMACY_STORE_NDPS])
    detail = {"error": exc.code, "message": str(exc)}
    if getattr(exc, "detail", None):
        detail.update({k: v for k, v in exc.detail.items() if k not in detail})
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


_WRITE_OR_APPROVE_USER_DEFAULT = Depends(_PS_READ)


def _write_or_approve(request: Request, user: dict = _WRITE_OR_APPROVE_USER_DEFAULT) -> dict:
    """Read plus write or approve, for recording a credit note."""
    role = user.get("role", "")
    if has_permission(role, PHARMACY_STORE_WRITE) or has_permission(role, PHARMACY_STORE_APPROVE):
        return user
    _audit_denied(request, user, [PHARMACY_STORE_WRITE, PHARMACY_STORE_APPROVE])
    raise HTTPException(
        status_code=403,
        detail={
            "error": "permission_denied",
            "message": "Insufficient permissions",
            "required": [
                PHARMACY_STORE_READ,
                f"{PHARMACY_STORE_WRITE} or {PHARMACY_STORE_APPROVE}",
            ],
        },
    )


# ── Reads ─────────────────────────────────────────────────────────────────────


_LIST_VENDOR_RETURNS_STATUS_DEFAULT = Query(None, max_length=40)
_LIST_VENDOR_RETURNS_LIMIT_DEFAULT = Query(100, ge=1, le=500)
_LIST_VENDOR_RETURNS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/vendor-returns")
def list_vendor_returns(
    request: Request,
    status: str | None = _LIST_VENDOR_RETURNS_STATUS_DEFAULT,
    limit: int = _LIST_VENDOR_RETURNS_LIMIT_DEFAULT,
    user: dict = _LIST_VENDOR_RETURNS_USER_DEFAULT,
):
    try:
        return {
            "vendor_returns": purchasing.list_vendor_returns(status=status or None, limit=limit)
        }
    except purchasing.PurchasingError as exc:
        raise _fail(exc, request, user) from exc


_VENDOR_RETURN_OPTIONS_USER_DEFAULT = Depends(_PS_READ)


@router.get("/vendor-returns/options")
def vendor_return_options(user: dict = _VENDOR_RETURN_OPTIONS_USER_DEFAULT):
    """Stores stock goes back from, vendors, who may witness a controlled
    return (never the caller) and the near-expiry window."""
    return purchasing.vendor_return_options(user)


_VENDOR_RETURN_STOCK_STORE_CODE_DEFAULT = Query(..., min_length=1, max_length=40)
_VENDOR_RETURN_STOCK_USER_DEFAULT = Depends(_PS_READ)


@router.get("/vendor-returns/stock")
def vendor_return_stock(
    request: Request,
    store_code: str = _VENDOR_RETURN_STOCK_STORE_CODE_DEFAULT,
    user: dict = _VENDOR_RETURN_STOCK_USER_DEFAULT,
):
    """The lots of one store that may go back to a vendor now: held off the
    shelf, expired or near expiry."""
    try:
        return purchasing.returnable_stock(store_code)
    except purchasing.PurchasingError as exc:
        raise _fail(exc, request, user) from exc


_GET_VENDOR_RETURN_USER_DEFAULT = Depends(_PS_READ)


@router.get("/vendor-returns/{return_id}")
def get_vendor_return(return_id: int, user: dict = _GET_VENDOR_RETURN_USER_DEFAULT):
    doc = purchasing.get_vendor_return(return_id)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"No return to vendor {return_id}"},
        )
    return {"vendor_return": doc}


# ── Writes ────────────────────────────────────────────────────────────────────


class VendorReturnLineIn(BaseModel):
    lot_id: str = Field(..., min_length=1, max_length=80)
    quantity_units: Any = None
    reason: str | None = Field(None, max_length=purchasing.VR_TEXT_LIMIT)


class VendorReturnIn(BaseModel):
    vendor_code: str = Field(..., min_length=1, max_length=40)
    store_code: str = Field(..., min_length=1, max_length=40)
    lines: list[VendorReturnLineIn] = Field(..., min_length=1, max_length=purchasing.VR_MAX_LINES)
    reason: str | None = Field(None, max_length=purchasing.VR_TEXT_LIMIT)
    remarks: str | None = Field(None, max_length=purchasing.VR_TEXT_LIMIT)
    idempotency_key: str | None = Field(None, max_length=120)


_CREATE_VENDOR_RETURN_IDEMPOTENCY_KEY_DEFAULT = Header(None, max_length=120)
_CREATE_VENDOR_RETURN_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/vendor-returns")
def create_vendor_return(
    body: VendorReturnIn,
    request: Request,
    idempotency_key: str | None = _CREATE_VENDOR_RETURN_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _CREATE_VENDOR_RETURN_USER_DEFAULT,
):
    """Draft a return. Nothing leaves the shelf until it is posted."""
    try:
        return purchasing.create_vendor_return(
            vendor_code=body.vendor_code,
            store_code=body.store_code,
            lines=[line.model_dump() for line in body.lines],
            actor=user,
            reason=body.reason,
            remarks=body.remarks,
            idempotency_key=idempotency_key or body.idempotency_key,
            audit_ctx=_ctx(request),
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class WitnessIn(BaseModel):
    witness_id: str | None = Field(None, max_length=80)
    witness_pin: str | None = Field(None, max_length=64)


_POST_VENDOR_RETURN_USER_DEFAULT = Depends(_PS_APPROVE)


@router.post("/vendor-returns/{return_id}/post")
def post_vendor_return(
    return_id: int, body: WitnessIn, request: Request, user: dict = _POST_VENDOR_RETURN_USER_DEFAULT
):
    """Send it back. A return already sent posts nothing again."""
    try:
        return purchasing.post_vendor_return(
            return_id,
            actor=user,
            witness_id=body.witness_id,
            witness_pin=body.witness_pin,
            audit_ctx=_ctx(request),
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class CreditIn(BaseModel):
    credit_note_ref: str | None = Field(None, max_length=purchasing.VR_TEXT_LIMIT)


_CREDIT_VENDOR_RETURN_USER_DEFAULT = Depends(_write_or_approve)


@router.post("/vendor-returns/{return_id}/credit")
def credit_vendor_return(
    return_id: int,
    body: CreditIn,
    request: Request,
    user: dict = _CREDIT_VENDOR_RETURN_USER_DEFAULT,
):
    try:
        return purchasing.record_vendor_credit(
            return_id, credit_note_ref=body.credit_note_ref, actor=user, audit_ctx=_ctx(request)
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=purchasing.VR_TEXT_LIMIT)


_CANCEL_VENDOR_RETURN_USER_DEFAULT = Depends(_PS_WRITE)


@router.post("/vendor-returns/{return_id}/cancel")
def cancel_vendor_return(
    return_id: int,
    body: CancelIn,
    request: Request,
    user: dict = _CANCEL_VENDOR_RETURN_USER_DEFAULT,
):
    try:
        return purchasing.cancel_vendor_return(
            return_id, reason=body.reason, actor=user, audit_ctx=_ctx(request)
        )
    except purchasing.PurchasingError as exc:
        raise _fail(exc, request, user) from exc
    except AuditWriteError:
        raise _audit_unavailable() from None
