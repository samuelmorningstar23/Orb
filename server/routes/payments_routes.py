"""Orb — Payments, deposits, refunds, discount approvals, day-end cash.

The HTTP surface over services/payments_service.py. Everything here is a money
operation; nothing here is on a clinical path.

THREE THINGS THIS FILE IS RESPONSIBLE FOR
─────────────────────────────────────────
1. THE MODULE SWITCH. Every route sits behind `config.PAYMENTS_ENABLED`
   (ORB_PAYMENTS, default ON). A site that keeps its cash counter in another
   system sets ORB_PAYMENTS=0 and these routes answer 404 — the same answer a
   route that does not exist gives, because to that deployment it does not.
   The flag is read PER REQUEST from the module, not captured at import, so a
   test (or an ops reload) can flip it.

2. THE EVENT LOOP. Every handler is `async def` and every database call goes
   through `database.run_db`. A blocking SQLite call inside `async def` stalls
   the loop for every clinician on the appliance, and
   tests/test_event_loop_discipline.py enforces the rule.

3. TURNING A RULE INTO A STATUS CODE. `PaymentError` is a refusal the operator
   can act on (422); `ImmutablePaymentError` is a CONFLICT with a document that
   already exists (409), which is the honest code for "this receipt has already
   been issued and cannot be changed".

THE EDIT ENDPOINT EXISTS IN ORDER TO REFUSE. `PATCH /receipts/{id}` is
implemented and always returns 409. A missing endpoint teaches a client author
nothing; one that answers with the rule and points at `POST /receipts/{id}/
reverse` teaches them the model in one round trip.

AUTHORISATION — FOUR PERMISSIONS, TIERED BY WHAT THE HANDLER DOES. Every route
wears one of payments.read / payments.record / payments.approve /
payments.close_counter (the dependency block below maps the tiers; writes
require read as well). Until 2026-08-18 every route borrowed `admin.overview`,
which conflated governance with cash handling and could not express the
segregation of duties this module already enforced IN CODE — decide_refund and
decide_discount refuse an approver who is the requester, reconciliation demands
a second pair of eyes (services/payments_service.py). Those in-code rules stay
exactly as they were; the permission table now AGREES with them, and adds the
tier the code alone could not express: RECORD (taking money, raising
refund/discount requests) and APPROVE (authorising recorded money to leave or
change) are separate verbs, and the `cashier` role (permissions.py) holds
read/record/close_counter and deliberately NOT approve. `admin` holds all four,
so nothing about who can reach these routes today changes — what changed is
what a counter account has to be handed.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

import config
from database import run_db
from permissions import (PAYMENTS_APPROVE, PAYMENTS_CLOSE_COUNTER,
                         PAYMENTS_READ, PAYMENTS_RECORD, department_scope,
                         enforce_patient_scope, log_phi_read,
                         require_patient_access, require_permission)
from services import payments_service as payments
from services.money import MoneyError

logger = logging.getLogger("orb.routes.payments")

router = APIRouter(prefix="/api/payments", tags=["payments"])

# ── Who can reach the counter ────────────────────────────────────────────────
# Four permissions, tiered by what each handler DOES (2026-08-18; the payer
# desk's `_DESK_READ`/`_DESK_WRITE` split in payer_routes.py is the pattern).
#
#   read           every GET: lists, receipts, counter status, the day-end sheet
#   record         money moving at the counter: taking a receipt, applying a
#                  deposit, RAISING a refund/discount request, paying out an
#                  already-approved refund, opening a drawer
#   approve        recorded money leaving or changing: refund and discount
#                  decisions, reversing a receipt
#   close_counter  the end-of-shift count: closing and reconciling a session
#
# WRITES REQUIRE READ AS WELL, and that is not belt-and-braces. Every write
# handler answers with the document it touched — a receipt with its patient and
# amounts, a refund file, a counter session — so a write-only grant would be a
# read grant with an extra step. Requiring both means `payments.read` is the
# only way to see this desk, whichever verb was used to ask.
#
# The permission is IN ADDITION to the segregation the service already enforces
# in code (approver ≠ requester on refund/discount decisions, the person who
# took a payment may not reverse it (since 2026-10-03), a second pair of
# eyes on reconciliation — services/payments_service.py). Those refusals stay;
# what the split adds is the tier code alone could not express: a `cashier`
# (read/record/close_counter) cannot hold the approve verb at all.
_COUNTER_READ = require_permission(PAYMENTS_READ)
_COUNTER_RECORD = require_permission(PAYMENTS_READ, PAYMENTS_RECORD)
_COUNTER_APPROVE = require_permission(PAYMENTS_READ, PAYMENTS_APPROVE)
_COUNTER_CLOSE = require_permission(PAYMENTS_READ, PAYMENTS_CLOSE_COUNTER)


async def require_module(request: Request) -> None:
    """404 when the site has switched this module off.

    Read from `config` at call time — `from config import PAYMENTS_ENABLED` would
    freeze the value at import and make the switch untestable and un-reloadable.
    """
    if not config.PAYMENTS_ENABLED:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "module_disabled",
                "module": "payments",
                "message": "The payments module is not enabled for this deployment "
                           "(ORB_PAYMENTS=0).",
            },
        )


def _actor(user: dict) -> str:
    """Who is doing this, as one attributable string. Never taken from the body:
    a request cannot name someone else as the person who took the money."""
    return str(user.get("id") or user.get("employee_id") or user.get("username") or "unknown")


def _guard(fn, *args, **kwargs):
    """Run a service call, translating its refusals into HTTP.

    Returns a thunk so the whole thing can go through `run_db` in one hop —
    including the exception translation, which must happen where the error is
    raised rather than after it has crossed a thread boundary as something else.
    """
    def _call():
        return fn(*args, **kwargs)
    return _call


async def _run(fn, *args, **kwargs):
    try:
        return await run_db(_guard(fn, *args, **kwargs))
    except payments.ImmutablePaymentError as exc:
        # 409, not 422: the request is well formed and the rule is about a
        # document that already exists.
        raise HTTPException(status_code=409, detail={
            "error": "receipt_immutable", "message": str(exc),
            "remedy": "POST /api/payments/receipts/{id}/reverse",
        }) from exc
    except payments.ReconciliationError as exc:
        # A total that does not equal its own parts is OUR bug, and it must not
        # be rendered as if it were a figure. 500 and a loud log.
        logger.error("payments reconciliation failure: %s", exc)
        raise HTTPException(status_code=500, detail={
            "error": "reconciliation_failed", "message": str(exc),
        }) from exc
    except payments.PaymentError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "payment_refused", "message": str(exc),
        }) from exc
    except MoneyError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "bad_money_value", "message": str(exc),
        }) from exc


async def _with_names(rows: list[dict], user: dict, *staff_keys: str) -> list[dict]:
    """Names beside the ids on a list a person reads (payments.attach_people).
    Patient names are held to the caller's ward scope, the one derivation
    every list uses, so a ward-scoped account sees no new names."""
    return await run_db(
        payments.attach_people, rows, dept=department_scope(user), staff_keys=staff_keys,
    )


class PatientMoneyBody(BaseModel):
    """A money body that NAMES A PATIENT. Typed for exactly one reason.

    The caller-named-patient drift guard (tests/test_caller_named_patient_scope.py)
    finds the patient by INSPECTING THE SIGNATURE — a parameter called
    ``patient_id`` or a Pydantic model with a ``patient_id`` field. Every
    mutation in this file spelled its body ``dict``, so all four of the ones
    that name a patient were invisible to it: the guard could never have
    reported them, and it ran green while `POST /refunds` moved money against
    any patient id the caller typed. `GET /refunds`, `GET /refunds/limits`,
    `GET /receipts` and `GET /discounts` — the READ siblings, right beside them
    — have each carried `enforce_patient_scope` since 2026-08-12.

    EXTRAS ARE DELIBERATELY UNVALIDATED (``extra="allow"``). Only `patient_id`
    is declared; every other field passes through untouched to the parsers
    below. That is not laziness — `_paise` exists to refuse `100.5` with a 422
    that explains that ₹12.34 is 1234, and declaring `amount_paise: int` here
    would hand that judgement to pydantic, which in lax mode accepts `100.0`
    and turns the explicit refusal into a generic list-shaped error the money
    tests assert against.
    """

    model_config = ConfigDict(extra="allow")

    patient_id: Optional[str] = None

    def raw(self) -> dict:
        """The body as the dict the `_paise` / `_int_or_none` parsers expect,
        with every extra field exactly as it arrived on the wire."""
        return self.model_dump()


def _int_or_none(body: dict, key: str) -> Optional[int]:
    value = body.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise HTTPException(status_code=422, detail={
            "error": "bad_money_value",
            "message": f"{key} must be a whole number of paise, not {type(value).__name__}",
        })
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail={
            "error": "bad_money_value", "message": f"{key} must be an integer",
        })


def _paise(body: dict, key: str) -> int:
    """Pull an amount out of a JSON body WITHOUT ever letting a float in.

    JSON has one number type and it is a double. `12.34` on the wire is a rupee
    amount someone typed into the wrong field, and accepting it silently is how a
    float reaches a money column. The client sends paise, as an integer, or it
    gets a 422 that says so.
    """
    value = body.get(key)
    if isinstance(value, bool):
        raise HTTPException(status_code=422, detail={
            "error": "bad_money_value", "message": f"{key} must be an integer of paise",
        })
    if isinstance(value, float):
        raise HTTPException(status_code=422, detail={
            "error": "bad_money_value",
            "message": (
                f"{key} must be an INTEGER number of paise, not a decimal "
                f"({value!r}). ₹12.34 is 1234."
            ),
        })
    if value is None:
        raise HTTPException(status_code=422, detail={
            "error": "bad_money_value", "message": f"{key} is required (integer paise)",
        })
    if isinstance(value, str):
        raise HTTPException(status_code=422, detail={
            "error": "bad_money_value",
            "message": f"{key} must be an integer of paise, not text, parse it client-side",
        })
    return int(value)


# ── Module status ────────────────────────────────────────────────────────────

@router.get("/config", dependencies=[Depends(require_module)])
async def payments_config(user: dict = Depends(_COUNTER_READ)):
    """What this module can and cannot do here, for a screen to render honestly."""
    def _read():
        from database import db_session
        with db_session() as db:
            row = db.execute(
                "SELECT prefix, next_number, financial_year, is_configured "
                "FROM document_number_series WHERE series_code = 'RCPT'"
            ).fetchone()
        return {
            "receipt_series": {
                "prefix": row["prefix"] if row else None,
                "financial_year": row["financial_year"] if row else None,
                "next_number": int(row["next_number"]) if row else None,
                "is_configured": bool(row and int(row["is_configured"] or 0)),
            } if row else None,
        }

    series = await run_db(_read)
    return {
        "module": "payments",
        "enabled": True,
        "methods": sorted(payments.PAYMENT_METHODS),
        "reference_required_methods": sorted(payments.REFERENCE_REQUIRED_METHODS),
        "receipt_purposes": sorted(payments.RECEIPT_PURPOSES),
        "refund_methods": sorted(payments.REFUND_METHODS),
        # Stated on every screen: receipts are documents, not editable rows.
        "receipts_are_immutable": True,
        **series,
    }


# ── Receipts ─────────────────────────────────────────────────────────────────

@router.post("/receipts", dependencies=[Depends(require_module)])
async def create_receipt(
    body: PatientMoneyBody = Body(...),
    user: dict = Depends(_COUNTER_RECORD),
):
    """Take money. Over-payment becomes an advance and says so in the response."""
    # PHI SCOPE (2026-08-14). Same rule the GET beside it has carried since
    # 2026-08-12, on the mutation that was never given it. A receipt names its
    # patient and states what they owe; writing one against another ward's
    # patient is both a disclosure and a real financial record on a chart the
    # caller may not see. 404, never 403 — a 403 confirms the patient exists.
    # Blocking, so it goes through run_db from this async def.
    await run_db(enforce_patient_scope, user, body.patient_id)
    data = body.raw()
    return await _run(
        payments.record_payment,
        patient_id=body.patient_id,
        amount_paise=_paise(data, "amount_paise"),
        method=data.get("method"),
        purpose=data.get("purpose") or "invoice_payment",
        invoice_id=_int_or_none(data, "invoice_id"),
        admission_id=data.get("admission_id"),
        payer_id=_int_or_none(data, "payer_id"),
        reference_no=data.get("reference_no"),
        bank_name=data.get("bank_name"),
        instrument_date=data.get("instrument_date"),
        counter_session_id=_int_or_none(data, "counter_session_id"),
        received_by=_actor(user),
        notes=data.get("notes"),
        idempotency_key=data.get("idempotency_key"),
    )


@router.get("/receipts", dependencies=[Depends(require_module)])
async def list_receipts(
    patient_id: Optional[str] = Query(default=None),
    invoice_id: Optional[int] = Query(default=None),
    counter_session_id: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None),
    direction: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(_COUNTER_READ),
):
    # PHI SCOPE (2026-08-12). patient_id arrives on the QUERY STRING, so
    # require_patient_access — which resolves it from the PATH — cannot guard this
    # route, and the path-param drift test greps straight past it. A financial
    # record names its patient, so an unscoped filter is the same disclosure as a
    # chart read. enforce_patient_scope 404s (never 403 — a 403 confirms the
    # patient exists); it is blocking, so it goes through run_db from async def.
    await run_db(enforce_patient_scope, user, patient_id)
    receipts = await _run(
        payments.list_payments,
        patient_id=patient_id,
        invoice_id=invoice_id,
        counter_session_id=counter_session_id,
        status=status,
        direction=direction,
        limit=limit,
    )
    return {"receipts": await _with_names(receipts, user, "received_by", "cancelled_by")}


@router.get("/receipts/{payment_id}", dependencies=[Depends(require_module)])
async def read_receipt(
    payment_id: int,
    user: dict = Depends(_COUNTER_READ),
):
    receipt = await _run(payments.get_payment, payment_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


@router.get("/receipts/{payment_id}/print", dependencies=[Depends(require_module)])
async def print_receipt(
    payment_id: int,
    request: Request,
    user: dict = Depends(_COUNTER_READ),
):
    """The printable receipt (multisite-2): the receipt, the supplier block it
    was issued with (never re-read from today's hospital details), the
    placeholder-series notice and the amount in words. A receipt names its
    patient, so the caller's patient scope applies: 404, never 403.

    A print is a disclosure (its notes carry a refund's reason), so it is
    audited like billing_routes.print_invoice: one phi_access row, which is
    fail-closed, so the receipt is not returned if the row cannot be written."""
    view = await _run(payments.receipt_print_view, payment_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    patient_id = view["receipt"]["patient_id"]
    await run_db(enforce_patient_scope, user, patient_id)
    await run_db(
        log_phi_read,
        request=request,
        user=user,
        action_type="payments_receipt_printed",
        summary_message=f"Receipt {payment_id} rendered for print",
        patient_id=patient_id,
        target_type="payment",
        target_id=str(payment_id),
    )
    return view


@router.patch("/receipts/{payment_id}", dependencies=[Depends(require_module)])
async def refuse_receipt_edit(
    payment_id: int,
    body: dict = Body(default_factory=dict),
    # RECORD, not APPROVE, deliberately. This endpoint can never mutate — it
    # exists to answer 409 with the immutability rule — and its audience is the
    # recording client whose screen tries an edit. Tier it at approve and that
    # client gets a blank 403 instead of the one-round-trip lesson pointing at
    # /reverse; nothing is protected in exchange, because there is nothing here
    # to protect.
    user: dict = Depends(_COUNTER_RECORD),
):
    """ALWAYS 409. See the module docstring: this endpoint exists to state the
    rule, not to be a stub somebody later "implements"."""
    return await _run(payments.amend_payment, payment_id, **(body or {}))


@router.post("/receipts/{payment_id}/reverse", dependencies=[Depends(require_module)])
async def reverse_receipt(
    payment_id: int,
    body: dict = Body(...),
    # APPROVE: a reversal voids recorded money. The permission alone did not
    # stop an admin (who holds record AND approve) voiding their own receipt,
    # the exact edit-the-ledger failure receipts are immutable to prevent, so
    # the service also refuses a reversal by the person who took the payment
    # (422), as it refuses a refund decided by its requester. The reverser is
    # the session user (_actor), never a name from the body.
    user: dict = Depends(_COUNTER_APPROVE),
):
    return await _run(
        payments.reverse_payment,
        payment_id,
        reason=body.get("reason"),
        reversed_by=_actor(user),
    )


# ── Deposits and advances ────────────────────────────────────────────────────

@router.get("/deposits/{patient_id}", dependencies=[Depends(require_module)])
async def read_deposits(
    patient_id: str,
    admission_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    user: dict = Depends(
        require_patient_access(PAYMENTS_READ, audit_action="payments_deposits_read")
    ),
):
    """A patient's advance balance and its ledger.

    WEARS THE WARD GUARD, and it has to. This route takes a patient id off the
    PATH: without `require_patient_access` a ward-scoped account could walk ids
    and learn who is admitted and what they have paid — the exact shape of the
    2026-08-05 leak (tests/test_patient_scope_routes.py enumerates the live app
    to catch a route added without it).

    A hospital-wide finance account is seeded with NO department, which is this
    codebase's existing convention for an unscoped governance account; it
    therefore reads across wards, and every read of this route lands on the
    fail-closed phi_access audit trail with its name on it.
    """
    balance = await _run(payments.deposit_balance, patient_id, admission_id=admission_id)
    entries = await _run(
        payments.list_deposit_entries, patient_id, admission_id=admission_id, limit=limit
    )
    return {**balance, "entries": entries}


@router.post("/deposits/apply", dependencies=[Depends(require_module)])
async def apply_deposit(
    body: PatientMoneyBody = Body(...),
    user: dict = Depends(_COUNTER_RECORD),
):
    # PHI SCOPE (2026-08-14). `GET /deposits/{patient_id}` right above wears
    # require_patient_access; this one spends the same patient's advance
    # balance and wore nothing. 404, never 403.
    await run_db(enforce_patient_scope, user, body.patient_id)
    data = body.raw()
    return await _run(
        payments.apply_deposit_to_invoice,
        patient_id=body.patient_id,
        invoice_id=_int_or_none(data, "invoice_id"),
        amount_paise=_paise(data, "amount_paise"),
        applied_by=_actor(user),
        admission_id=data.get("admission_id"),
        notes=data.get("notes"),
    )


# ── Refunds ──────────────────────────────────────────────────────────────────

@router.get("/refunds/limits", dependencies=[Depends(require_module)])
async def refund_limits(
    patient_id: str = Query(...),
    payment_id: Optional[int] = Query(default=None),
    invoice_id: Optional[int] = Query(default=None),
    user: dict = Depends(_COUNTER_READ),
):
    """What is refundable, before anyone types an amount."""
    # PHI SCOPE (2026-08-12). patient_id arrives on the QUERY STRING, so
    # require_patient_access — which resolves it from the PATH — cannot guard this
    # route, and the path-param drift test greps straight past it. A financial
    # record names its patient, so an unscoped filter is the same disclosure as a
    # chart read. enforce_patient_scope 404s (never 403 — a 403 confirms the
    # patient exists); it is blocking, so it goes through run_db from async def.
    await run_db(enforce_patient_scope, user, patient_id)
    return await _run(
        payments.refundable_paise, patient_id, payment_id=payment_id, invoice_id=invoice_id
    )


@router.post("/refunds", dependencies=[Depends(require_module)])
async def create_refund(
    body: PatientMoneyBody = Body(...),
    user: dict = Depends(_COUNTER_RECORD),
):
    # PHI SCOPE (2026-08-14). THE SHARPEST OF THE FOUR, because its own two
    # siblings already do it: `GET /refunds/limits?patient_id=` and
    # `GET /refunds?patient_id=` both call enforce_patient_scope, so a caller
    # could be refused permission to ASK what was refundable for a patient and
    # still be allowed to REQUEST the refund. The read was scoped and the write
    # beside it was not, purely because the write's `dict` body hid it from
    # the guard that enumerates this surface. 404, never 403.
    await run_db(enforce_patient_scope, user, body.patient_id)
    data = body.raw()
    return await _run(
        payments.request_refund,
        patient_id=body.patient_id,
        amount_paise=_paise(data, "amount_paise"),
        reason=data.get("reason"),
        requested_by=_actor(user),
        payment_id=_int_or_none(data, "payment_id"),
        invoice_id=_int_or_none(data, "invoice_id"),
        admission_id=data.get("admission_id"),
        reason_code=data.get("reason_code"),
        idempotency_key=data.get("idempotency_key"),
    )


@router.get("/refunds", dependencies=[Depends(require_module)])
async def list_refunds(
    patient_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(_COUNTER_READ),
):
    # PHI SCOPE (2026-08-12). patient_id arrives on the QUERY STRING, so
    # require_patient_access — which resolves it from the PATH — cannot guard this
    # route, and the path-param drift test greps straight past it. A financial
    # record names its patient, so an unscoped filter is the same disclosure as a
    # chart read. enforce_patient_scope 404s (never 403 — a 403 confirms the
    # patient exists); it is blocking, so it goes through run_db from async def.
    await run_db(enforce_patient_scope, user, patient_id)
    refunds = await _run(
        payments.list_refunds, patient_id=patient_id, status=status, limit=limit
    )
    return {"refunds": await _with_names(
        refunds, user, "requested_by", "decided_by", "paid_by",
    )}


@router.post("/refunds/{refund_id}/decision", dependencies=[Depends(require_module)])
async def decide_refund(
    refund_id: int,
    body: dict = Body(...),
    # APPROVE — and the permission is IN ADDITION to the service's four-eyes
    # rule (an approver who is the requester is refused with 422), never
    # instead of it.
    user: dict = Depends(_COUNTER_APPROVE),
):
    """Approve or reject. The service refuses if the approver is the requester."""
    approve = body.get("approve")
    if not isinstance(approve, bool):
        raise HTTPException(status_code=422, detail={
            "error": "bad_request", "message": "approve must be true or false",
        })
    return await _run(
        payments.decide_refund,
        refund_id,
        approve=approve,
        decided_by=_actor(user),
        decision_note=body.get("decision_note"),
    )


@router.post("/refunds/{refund_id}/pay", dependencies=[Depends(require_module)])
async def pay_refund(
    refund_id: int,
    body: dict = Body(...),
    # RECORD, not APPROVE: the authorisation already happened — the service
    # refuses to pay a refund that is not `approved` — and handing the cash
    # across the counter is the cashier's half of the segregated pair. The
    # designed lifecycle is request (record) → decide (approve, second person)
    # → pay out (record).
    user: dict = Depends(_COUNTER_RECORD),
):
    return await _run(
        payments.pay_refund,
        refund_id,
        method=body.get("method"),
        paid_by=_actor(user),
        reference_no=body.get("reference_no"),
        counter_session_id=_int_or_none(body, "counter_session_id"),
    )


# ── Discount approvals ───────────────────────────────────────────────────────

@router.post("/discounts", dependencies=[Depends(require_module)])
async def create_discount(
    body: PatientMoneyBody = Body(...),
    user: dict = Depends(_COUNTER_RECORD),
):
    # PHI SCOPE (2026-08-14). `GET /discounts?patient_id=` is scoped; the
    # request that creates one was not. 404, never 403.
    await run_db(enforce_patient_scope, user, body.patient_id)
    data = body.raw()
    requested = data.get("requested_paise")
    return await _run(
        payments.request_discount,
        patient_id=body.patient_id,
        reason=data.get("reason"),
        requested_by=_actor(user),
        invoice_id=_int_or_none(data, "invoice_id"),
        encounter_charge_id=_int_or_none(data, "encounter_charge_id"),
        scope=data.get("scope") or "invoice",
        discount_type=data.get("discount_type") or "amount",
        requested_paise=None if requested is None else _paise(data, "requested_paise"),
        percent_bp=_int_or_none(data, "percent_bp") or 0,
        base_paise=None if data.get("base_paise") is None else _paise(data, "base_paise"),
        policy_limit_paise=(
            None if data.get("policy_limit_paise") is None
            else _paise(data, "policy_limit_paise")
        ),
        reason_code=data.get("reason_code"),
        admission_id=data.get("admission_id"),
    )


@router.get("/discounts", dependencies=[Depends(require_module)])
async def list_discounts(
    status: Optional[str] = Query(default=None),
    invoice_id: Optional[int] = Query(default=None),
    patient_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(_COUNTER_READ),
):
    # PHI SCOPE (2026-08-12). patient_id arrives on the QUERY STRING, so
    # require_patient_access — which resolves it from the PATH — cannot guard this
    # route, and the path-param drift test greps straight past it. A financial
    # record names its patient, so an unscoped filter is the same disclosure as a
    # chart read. enforce_patient_scope 404s (never 403 — a 403 confirms the
    # patient exists); it is blocking, so it goes through run_db from async def.
    await run_db(enforce_patient_scope, user, patient_id)
    discounts = await _run(
        payments.list_discounts, status=status, invoice_id=invoice_id,
        patient_id=patient_id, limit=limit,
    )
    return {"discounts": await _with_names(discounts, user, "requested_by", "decided_by")}


@router.post("/discounts/{discount_id}/decision", dependencies=[Depends(require_module)])
async def decide_discount(
    discount_id: int,
    body: dict = Body(...),
    # APPROVE — in addition to the service's own requester-may-not-decide rule.
    user: dict = Depends(_COUNTER_APPROVE),
):
    """The approver's identity comes from the SESSION, never the body. A discount
    is a person's decision; a body-supplied approver name is a signature anybody
    can forge."""
    approve = body.get("approve")
    if not isinstance(approve, bool):
        raise HTTPException(status_code=422, detail={
            "error": "bad_request", "message": "approve must be true or false",
        })
    approved = body.get("approved_paise")
    return await _run(
        payments.decide_discount,
        discount_id,
        approve=approve,
        decided_by=_actor(user),
        approved_paise=None if approved is None else _paise(body, "approved_paise"),
        decision_note=body.get("decision_note"),
    )


# ── Cash counters and the day-end count ──────────────────────────────────────

@router.post("/counters/open", dependencies=[Depends(require_module)])
async def open_counter(
    body: dict = Body(...),
    # RECORD, not CLOSE_COUNTER: opening a drawer with its float is the START
    # of taking money — a cashier must be able to open their own shift.
    # CLOSE_COUNTER is only the end-of-shift count and its sign-off.
    user: dict = Depends(_COUNTER_RECORD),
):
    return await _run(
        payments.open_counter_session,
        counter_code=body.get("counter_code"),
        opened_by=_actor(user),
        shift_label=body.get("shift_label"),
        opening_float_paise=(
            0 if body.get("opening_float_paise") is None
            else _paise(body, "opening_float_paise")
        ),
    )


@router.get("/counters", dependencies=[Depends(require_module)])
async def list_counters(
    counter_code: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(_COUNTER_READ),
):
    sessions = await _run(
        payments.list_counter_sessions, counter_code=counter_code, status=status, limit=limit
    )
    return {"sessions": await _with_names(
        sessions, user, "opened_by", "closed_by", "reconciled_by",
    )}


@router.get("/counters/{session_id}/expected", dependencies=[Depends(require_module)])
async def counter_expected(
    session_id: int,
    user: dict = Depends(_COUNTER_READ),
):
    """What the drawer should hold, and the breakdown it is the sum of."""
    return await _run(payments.counter_session_expected, session_id)


@router.post("/counters/{session_id}/close", dependencies=[Depends(require_module)])
async def close_counter(
    session_id: int,
    body: dict = Body(...),
    user: dict = Depends(_COUNTER_CLOSE),
):
    return await _run(
        payments.close_counter_session,
        session_id,
        declared_cash_paise=_paise(body, "declared_cash_paise"),
        closed_by=_actor(user),
        notes=body.get("notes"),
    )


@router.post("/counters/{session_id}/reconcile", dependencies=[Depends(require_module)])
async def reconcile_counter(
    session_id: int,
    body: dict = Body(default_factory=dict),
    # CLOSE_COUNTER — in addition to the service's second-pair-of-eyes rule
    # (the reconciler may not be the person who closed the till).
    user: dict = Depends(_COUNTER_CLOSE),
):
    return await _run(
        payments.reconcile_counter_session,
        session_id,
        reconciled_by=_actor(user),
        notes=(body or {}).get("notes"),
    )


@router.get("/day-end", dependencies=[Depends(require_module)])
async def day_end(
    day: Optional[str] = Query(default=None, description="YYYY-MM-DD, hospital-local"),
    counter_code: Optional[str] = Query(default=None),
    user: dict = Depends(_COUNTER_READ),
):
    return await _run(payments.day_end_summary, day=day, counter_code=counter_code)


@router.get("/patients", dependencies=[Depends(require_module)])
async def search_counter_patients(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=50),
    user: dict = Depends(_COUNTER_READ),
):
    """Find the patient at the counter by name, MRN, bed or ID.

    The counter used to take a typed Patient ID, and receipts were issued to
    whatever was typed. A cashier holds no chart permission, so the ward list
    (/api/vigil/patients) is closed to them; this returns who and where only
    (name, MRN, bed, ward, status), from the caller's own ward when they have
    one, and is audited whenever it names anyone, as /api/search/patients is.
    """
    dept = department_scope(user)
    results = await _run(payments.search_patients, q, dept=dept, limit=limit)
    if results:
        await run_db(
            log_phi_read,
            request=request,
            user=user,
            action_type="payments_patient_search",
            summary_message=(
                f"Counter patient search returned {len(results)} match(es) in "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={"result_count": len(results),
                      "patient_ids": [r["id"] for r in results]},
        )
    return {"patients": results}


@router.get("/patients/{patient_id}/invoices", dependencies=[Depends(require_module)])
async def open_invoices(
    patient_id: str,
    user: dict = Depends(
        require_patient_access(PAYMENTS_READ, audit_action="payments_open_invoices")
    ),
):
    """This patient's bills that still owe money, for the counter to offer as
    a choice. Ward-guarded on the path, like the invoice check below."""
    return {"invoices": await _run(payments.open_invoices_for_payment, patient_id)}


@router.get("/patients/{patient_id}/invoices/{invoice_id}", dependencies=[Depends(require_module)])
async def invoice_for_payment(
    patient_id: str,
    invoice_id: int,
    user: dict = Depends(
        require_patient_access(PAYMENTS_READ, audit_action="payments_invoice_check")
    ),
):
    """The invoice a payment is about to be applied to, checked against the
    patient first. The counter shows its owner and balance before Take payment
    is enabled; another patient's invoice is a 422 that names no one."""
    return await _run(payments.invoice_for_payment, patient_id, invoice_id)


@router.get("/patients/{patient_id}/summary", dependencies=[Depends(require_module)])
async def patient_summary(
    patient_id: str,
    admission_id: Optional[str] = Query(default=None),
    user: dict = Depends(
        require_patient_access(PAYMENTS_READ, audit_action="payments_patient_summary")
    ),
):
    """ADVISORY ONLY — a financial position, never a gate on care.

    Ward-guarded for the same reason as `/deposits/{patient_id}`: a patient id in
    the path plus no scope check is how a cross-ward probe becomes a feature.
    """
    return await _run(
        payments.patient_financial_snapshot, patient_id, admission_id=admission_id
    )
