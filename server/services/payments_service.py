"""Orb — the money that actually changes hands: receipts, deposits, refunds,
discount approvals and the day-end cash count.

WHAT THIS MODULE IS. `billing` decides what a patient OWES. This module records
what was PAID, what was held on account, what was given back, who authorised a
reduction, and whether the drawer balanced at the end of the shift. It is the
half of the money system a clerk touches all day, and the half an auditor reads
first.

THE FOUR RULES IT IS BUILT AROUND
─────────────────────────────────
1. A RECEIPT IS IMMUTABLE. Once a `payments` row is written it is never edited.
   A mistake is corrected the way this codebase corrects an audit entry or a
   signed note: with a NEW, opposing, linked entry — `reverse_payment()` — that
   leaves both halves visible. `amend_payment()` exists only to REFUSE, loudly,
   so that a caller reaching for an edit gets an exception instead of finding
   no function and writing their own UPDATE.

   The one thing a reversal does write on the original row is its LIFECYCLE
   (`status='reversed'`, who, when, why). That is the supersession marker, not
   the financial content: amount, method, reference, patient, invoice, purpose,
   direction and receipt number are never touched by anything in this file after
   the INSERT.

2. AN OVER-PAYMENT IS AN ADVANCE, NEVER A NEGATIVE BALANCE. A receipt is
   allocated to an invoice up to that invoice's remaining due and NOT ONE PAISE
   FURTHER; the excess lands in the `patient_deposits` ledger as money the
   hospital is holding for the patient. An invoice's paid total can therefore
   never exceed its payable total — which is what stops a "balance" column going
   negative and being read as a credit nobody can explain. The over-payment path
   is explicit and is reported back to the caller (`advance_paise`) so the
   counter can tell the patient what happened.

3. MONEY IS INTEGER PAISE AND ALL OF IT GOES THROUGH services/money.py. No
   arithmetic in this file rounds; there is no percentage here except through
   `apply_basis_points`. Splits use `split_shares`, whose parts sum EXACTLY to
   the whole.

4. A VARIANCE IS RECORDED, NEVER ABSORBED. `close_counter_session` stores what
   the system expected, what the cashier counted, and the difference — even when
   the difference is zero, and especially when it is not. A session that does not
   balance closes as 'disputed', not as 'closed'. Silently reconciling a ₹200
   short is how a till loss becomes invisible.

   ITS COROLLARY: A VARIANCE THAT CANNOT BE ATTRIBUTED IS NOT PRINTED. Cash taken
   with no counter session open belongs to no drawer, so no shift count can
   explain it. It is recorded (refusing it after the notes are on the counter
   only pushes the money off-system), marked on the receipt
   (`unattributed_cash`), and reported on its own at day-end — where it holds the
   whole-day sheet OPEN rather than letting a variance that is missing a term
   read as final. See `record_payment` and `day_end_summary`.

*** PILOT SAFETY ***
Nothing in this module is on a clinical path. It writes only to migration-20
tables plus `audit_logs`. There is no code here that can refuse, delay or gate a
medication order, an administration, an observation, an alert or a discharge.
The shadow pilot does not bill through Orb at all; this module runs beside the
incumbent system, not in front of a patient.

CONVENTIONS (shared with services/billing_masters.py)
  * Every public function is PURE SYNC and opens its own `db_session`. Async
    callers go through `database.run_db` — blocking DB calls inside `async def`
    are banned and tests/test_event_loop_discipline.py enforces it.
  * A write takes ONE `db_session(immediate=True)` and never nests another
    session inside it (database.py forbids it and would deadlock unpooled), so
    every helper here takes the open connection as its first argument.
  * Document numbers are allocated BEFORE the write transaction opens, because
    `next_document_number` needs its own immediate transaction. If the write then
    fails, a number is skipped. A GAP in a receipt series is normal, explainable
    and auditable; a DUPLICATE receipt number is neither.
  * No foreign keys exist on these tables (migration 20), so a bad id yields an
    empty result and an explicit error, never a failed clinical write.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional, Sequence

from database import db_session
from services import facility_identity
from services.billing_masters import (
    MAIN_FACILITY_ID,
    SeriesNotSetUp,
    next_document_number,
    series_is_configured,
)
from services.hospital_time import hospital_stamp, hospital_today, local_day_bounds_utc
from services.money import (
    MoneyError,
    apply_basis_points,
    assert_non_negative_paise,
    assert_paise,
    format_inr,
    format_inr_or_unset,
    split_shares,
)

logger = logging.getLogger("orb.payments")

__all__ = [
    "PaymentError",
    "ImmutablePaymentError",
    "ReconciliationError",
    "PAYMENT_METHODS",
    "REFERENCE_REQUIRED_METHODS",
    "CASH_METHODS",
    "UNATTRIBUTED_CASH_NOTICE",
    "is_unattributed_cash",
    "RECEIPT_PURPOSES",
    "IMMUTABLE_PAYMENT_FIELDS",
    "record_payment",
    "get_payment",
    "receipt_print_view",
    "list_payments",
    "amend_payment",
    "reverse_payment",
    "deposit_balance",
    "list_deposit_entries",
    "apply_deposit_to_invoice",
    "plan_deposit_split",
    "refundable_paise",
    "request_refund",
    "decide_refund",
    "pay_refund",
    "get_refund",
    "list_refunds",
    "request_discount",
    "decide_discount",
    "list_discounts",
    "open_counter_session",
    "get_counter_session",
    "list_counter_sessions",
    "counter_session_expected",
    "close_counter_session",
    "reconcile_counter_session",
    "day_end_summary",
    "patient_financial_snapshot",
]


# ── Errors ───────────────────────────────────────────────────────────────────

class PaymentError(ValueError):
    """A money operation the rules refuse: a zero receipt, a refund larger than
    what was paid, a discount approved by the person who asked for it."""


class ImmutablePaymentError(PaymentError):
    """An attempt to change an issued receipt. Always raised, never conditional."""


class ReconciliationError(PaymentError):
    """A day-end total does not equal the sum of its own parts.

    This is a BUG in this module, surfaced instead of rendered. A reconciliation
    screen whose headline disagrees with its own breakdown is worse than no
    screen: the clerk trusts the big number.
    """


# ── Vocabulary (mirrors the CHECK constraints in migration 20) ───────────────

#: `payments.method`.
PAYMENT_METHODS: frozenset[str] = frozenset({
    "cash", "card", "upi", "netbanking", "cheque", "dd", "neft", "rtgs",
    "wallet", "insurance", "scheme", "adjustment",
})

#: Methods that MUST carry a reference number. A card slip, a UPI UTR, a cheque
#: number — without one the payment cannot be matched to the bank statement at
#: month end, and an unmatchable payment is indistinguishable from one that never
#: arrived. Cash has nothing to reference; 'adjustment' is an internal transfer
#: between Orb's own ledgers and references the document that caused it.
REFERENCE_REQUIRED_METHODS: frozenset[str] = frozenset({
    "card", "upi", "netbanking", "cheque", "dd", "neft", "rtgs", "wallet",
})

#: What lands in the cash drawer and therefore has to be counted at shift end.
CASH_METHODS: frozenset[str] = frozenset({"cash"})

#: The sentence a screen prints beside a cash receipt that belongs to no drawer.
UNATTRIBUTED_CASH_NOTICE = (
    "Cash taken with no counter session open. This money is in the day's takings "
    "but in nobody's drawer, so no shift count can explain it. Open a counter "
    "session before taking cash."
)

#: Purposes a MONEY-IN receipt may carry. 'refund' and 'write_off' are produced
#: by their own paths and are not accepted from a caller here.
RECEIPT_PURPOSES: frozenset[str] = frozenset({
    "advance", "deposit", "invoice_payment", "settlement",
})

#: Refund methods (migration 20 excludes insurance/scheme — a TPA settlement is
#: not refunded over a counter).
REFUND_METHODS: frozenset[str] = frozenset({
    "cash", "card", "upi", "netbanking", "cheque", "dd", "neft", "rtgs", "adjustment",
})

#: Statuses whose money is REAL — counted, refundable, reconciled.
LIVE_PAYMENT_STATUSES: tuple[str, ...] = ("received", "cleared")

#: Statuses a DRAWER or DAY-END sum counts: the live ones plus 'reversed'. A
#: reversal writes a live 'out' row and moves the original to 'reversed', so
#: summing live rows only counted the 'out' leg and took the money off twice
#: (a ₹500 till read ₹0 after ₹500 was taken and reversed). Both legs count
#: here, so a reversal nets to zero and shows as the two movements it was. An
#: earlier day's sheet does not move when one of its receipts is reversed later
#: (the 'out' leg is dated the day it is written), and a closed shift keeps the
#: count and variance stored when it closed.
LEDGER_PAYMENT_STATUSES: tuple[str, ...] = (*LIVE_PAYMENT_STATUSES, "reversed")

#: The opposite choice for a PATIENT's paid and refunded figures: neither leg.
#: The reversed receipt already leaves the live set; this drops the row that
#: reversed it, because a correction is not money refunded and counting it
#: read as a refund nobody was given.
_NOT_A_REVERSAL = "reversal_of_payment_id IS NULL"

#: Invoice statuses a receipt may be allocated against. A cancelled or void
#: invoice is not a debt; money taken "against" one is an advance.
UNALLOCATABLE_INVOICE_STATUSES: frozenset[str] = frozenset({"cancelled", "void"})

#: The columns `amend_payment` exists to protect. Everything a receipt asserts
#: about the transaction lives here; only the lifecycle columns (status,
#: cancelled_*) are writable, and only by `reverse_payment`.
IMMUTABLE_PAYMENT_FIELDS: frozenset[str] = frozenset({
    "receipt_number", "patient_id", "admission_id", "invoice_id", "payer_id",
    "direction", "amount_paise", "method", "reference_no", "bank_name",
    "instrument_date", "purpose", "counter_session_id", "received_by",
    "received_at", "idempotency_key", "notes",
    # Who issued it, fixed when the number was assigned (migration 72, whose
    # trigger refuses any later change).
    *facility_identity.SNAPSHOT_COLUMNS,
})

_MAX_TEXT = 2000
_MAX_REF = 120


# ── Small helpers ────────────────────────────────────────────────────────────

def _row(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _text(value: Optional[str], name: str, *, required: bool = False,
          max_len: int = _MAX_TEXT) -> Optional[str]:
    if value is None:
        if required:
            raise PaymentError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise PaymentError(f"{name} must be text")
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise PaymentError(f"{name} is required")
        return None
    if len(cleaned) > max_len:
        raise PaymentError(f"{name} must be {max_len} characters or fewer")
    return cleaned


def _positive_paise(value: Any, name: str) -> int:
    """Money that must be a real amount. Zero is rejected on purpose: a receipt
    for nothing, a refund of nothing and a deposit of nothing are all data-entry
    accidents, and each one prints a document that looks like a transaction."""
    try:
        amount = assert_non_negative_paise(value, name)
    except MoneyError as exc:
        raise PaymentError(str(exc)) from exc
    if amount == 0:
        raise PaymentError(f"{name} must be more than zero")
    return amount


def _utc_now_str() -> str:
    """The textual shape SQLite's CURRENT_TIMESTAMP writes, so rows inserted by
    this module sort and compare identically to rows inserted by a DEFAULT."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso_date(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    text = _text(value, name, max_len=32)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise PaymentError(f"{name} must be YYYY-MM-DD") from exc


def _receipt_series_is_configured(db, facility_id: int = MAIN_FACILITY_ID) -> bool:
    """Whether the receipt series of a facility (multisite-7a; facility 1 by
    default) is the site's real one."""
    return series_is_configured(db, "RCPT", facility_id=facility_id)


#: What each series this module draws from numbers, for a refusal's words.
_SERIES_WORDS = {"RCPT": "receipt", "REF": "refund"}


def _next_number(series_code: str, *, patient_id: Optional[str],
                 admission_id: Optional[str] = None) -> dict:
    """The next number of the series of the facility the document belongs to
    (multisite-7a): the facility its supplier snapshot is taken for, which is
    facility 1 while only one is on record. The answer adds `facility_id`.

    A facility other than 1 that has no series of its own yet is refused in
    words before anything is written, never numbered from facility 1's
    series."""
    with db_session() as db:
        facility_id = facility_identity.document_facility_id(
            db, patient_id=patient_id, admission_id=admission_id)
    try:
        doc = next_document_number(series_code, facility_id=facility_id)
    except SeriesNotSetUp as exc:
        raise PaymentError(
            f"This facility has no {_SERIES_WORDS.get(series_code, 'document')} number "
            "series yet, so nothing was recorded. An administrator sets one up in Admin, "
            "Facilities."
        ) from exc
    doc["facility_id"] = facility_id
    return doc


# ── Views (what a screen and an API response receive) ────────────────────────

def is_unattributed_cash(row: Any) -> bool:
    """Cash that belongs to no drawer: a CASH method with no counter session.

    DERIVED, never stored. The two columns it reads are immutable on a receipt
    (IMMUTABLE_PAYMENT_FIELDS holds both `method` and `counter_session_id`), so a
    stored duplicate could only ever drift out of agreement with them.
    """
    try:
        method = row["method"]
        session_id = row["counter_session_id"]
    except (KeyError, IndexError, TypeError):  # pragma: no cover - defensive
        return False
    return method in CASH_METHODS and session_id is None


def _payment_view(row: dict) -> dict:
    """One receipt, ready to render. `signed_paise` is the ledger view (money out
    is negative); `amount_paise` stays positive because the DIRECTION carries the
    sign in this schema — a signed amount plus a direction is two sources of
    truth about one thing.

    `unattributed_cash` travels with EVERY rendering of the receipt, not only the
    response to the call that took it, because that is the one fact a day-end
    reconciliation needs and cannot recover later: which cash never entered a
    drawer."""
    amount = int(row["amount_paise"])
    signed = amount if row["direction"] == "in" else -amount
    orphan = is_unattributed_cash(row)
    return {
        "id": int(row["id"]),
        "receipt_number": row["receipt_number"],
        "patient_id": row["patient_id"],
        "admission_id": row["admission_id"],
        "invoice_id": row["invoice_id"],
        "payer_id": row["payer_id"],
        "direction": row["direction"],
        "amount_paise": amount,
        "amount_display": format_inr(amount),
        "signed_paise": signed,
        "method": row["method"],
        "reference_no": row["reference_no"],
        "bank_name": row["bank_name"],
        "instrument_date": row["instrument_date"],
        "purpose": row["purpose"],
        "status": row["status"],
        "counter_session_id": row["counter_session_id"],
        "refund_id": row["refund_id"],
        "reversal_of_payment_id": row["reversal_of_payment_id"],
        "received_by": row["received_by"],
        "received_at": row["received_at"],
        "cancelled_by": row["cancelled_by"],
        "cancelled_at": row["cancelled_at"],
        "cancel_reason": row["cancel_reason"],
        "notes": row["notes"],
        "is_reversed": row["status"] == "reversed",
        "unattributed_cash": orphan,
        "unattributed_cash_notice": UNATTRIBUTED_CASH_NOTICE if orphan else None,
        # An issued receipt is a document, not a draft. Say so on the screen so
        # nobody looks for an edit button.
        "is_immutable": True,
    }


def _refund_view(row: dict) -> dict:
    amount = int(row["amount_paise"])
    return {
        "id": int(row["id"]),
        "refund_number": row["refund_number"],
        "patient_id": row["patient_id"],
        "admission_id": row["admission_id"],
        "invoice_id": row["invoice_id"],
        "payment_id": row["payment_id"],
        "amount_paise": amount,
        "amount_display": format_inr(amount),
        "reason_code": row["reason_code"],
        "reason": row["reason"],
        "status": row["status"],
        "method": row["method"],
        "reference_no": row["reference_no"],
        "requested_by": row["requested_by"],
        "requested_at": row["requested_at"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
        "decision_note": row["decision_note"],
        "paid_by": row["paid_by"],
        "paid_at": row["paid_at"],
    }


def _discount_view(row: dict) -> dict:
    approved = row["approved_paise"]
    limit = row["policy_limit_paise"]
    return {
        "id": int(row["id"]),
        "patient_id": row["patient_id"],
        "admission_id": row["admission_id"],
        "invoice_id": row["invoice_id"],
        "encounter_charge_id": row["encounter_charge_id"],
        "scope": row["scope"],
        "discount_type": row["discount_type"],
        "percent_bp": int(row["percent_bp"] or 0),
        "requested_paise": int(row["requested_paise"] or 0),
        "requested_display": format_inr(int(row["requested_paise"] or 0)),
        "approved_paise": None if approved is None else int(approved),
        # None is NOT zero: an undecided request has no approved amount, and
        # "₹0.00" there would read as "rejected to nil".
        "approved_display": format_inr_or_unset(
            None if approved is None else int(approved), unset="Not decided"
        ),
        "policy_limit_paise": None if limit is None else int(limit),
        "policy_limit_display": format_inr_or_unset(
            None if limit is None else int(limit), unset="No limit configured"
        ),
        "exceeded_limit": bool(row["exceeded_limit"]),
        "reason_code": row["reason_code"],
        "reason": row["reason"],
        "status": row["status"],
        "requested_by": row["requested_by"],
        "requested_at": row["requested_at"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
        "decision_note": row["decision_note"],
    }


def _session_view(row: dict) -> dict:
    declared = row["declared_cash_paise"]
    system = row["system_cash_paise"]
    variance = row["variance_paise"]
    return {
        "id": int(row["id"]),
        "counter_code": row["counter_code"],
        "shift_label": row["shift_label"],
        "opened_by": row["opened_by"],
        "opened_at": row["opened_at"],
        "opening_float_paise": int(row["opening_float_paise"] or 0),
        "opening_float_display": format_inr(int(row["opening_float_paise"] or 0)),
        "closed_by": row["closed_by"],
        "closed_at": row["closed_at"],
        # An OPEN session has not been counted. None here, and "Not counted" on
        # the screen — never ₹0.00, which would read as an empty till.
        "declared_cash_paise": None if declared is None else int(declared),
        "declared_cash_display": format_inr_or_unset(
            None if declared is None else int(declared), unset="Not counted"
        ),
        "system_cash_paise": None if system is None else int(system),
        "system_cash_display": format_inr_or_unset(
            None if system is None else int(system), unset="Not counted"
        ),
        "variance_paise": None if variance is None else int(variance),
        "variance_display": format_inr_or_unset(
            None if variance is None else int(variance), unset="Not counted"
        ),
        "status": row["status"],
        "reconciled_by": row["reconciled_by"],
        "reconciled_at": row["reconciled_at"],
        "notes": row["notes"],
    }


# ── Audit ────────────────────────────────────────────────────────────────────

def _audit(db, *, action: str, outcome: str, actor: Optional[str],
           patient_id: Optional[str] = None, target_type: Optional[str] = None,
           target_id: Optional[str] = None, summary: str = "",
           metadata: Optional[dict] = None) -> None:
    """Attribute a money movement to a person, on the tamper-evident chain.

    Category 'financial' is deliberately NOT one of the fail-closed categories
    ({security, clinical_access, phi_access, authentication, ai_inference}): those
    refuse the action when the audit write fails. Refusing to record a payment the
    patient has already handed over would leave the hospital holding untracked cash,
    which is a worse outcome than a missing audit row — and the row is written
    inside the SAME transaction as the payment, so the two commit together or
    not at all in every ordinary failure.
    """
    from services.audit_service import log_audit_event

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="financial",
        action_type=action,
        outcome=outcome,
        db=db,
        summary_message=summary or action,
        actor_user_id=actor,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        patient_id=patient_id,
        metadata=metadata,
    )


# ── Invoice coupling (read-mostly; this module owns only the PAID columns) ───

def _load_invoice(db, invoice_id: int) -> dict:
    row = db.execute("SELECT * FROM invoices WHERE id = ?", (int(invoice_id),)).fetchone()
    if row is None:
        raise PaymentError(f"invoice {invoice_id} does not exist")
    return _row(row)


def _load_patient_invoice(db, invoice_id: int, patient_id: str, *,
                          then: str = "Nothing was taken.") -> dict:
    """The invoice, refused unless it is THIS patient's.

    Invoice ids are small sequential integers typed at a counter, so one
    mistyped digit names another patient's bill. Taking money for patient A and
    allocating it to patient B's invoice clears B's debt with A's money and
    leaves A's bill open; the refund path already refused the receipt-side
    version of this.
    """
    invoice = _load_invoice(db, invoice_id)
    if invoice["patient_id"] != patient_id:
        label = invoice.get("invoice_number") or invoice_id
        raise PaymentError(f"Invoice {label} belongs to another patient. {then}")
    return invoice


def invoice_for_payment(patient_id: str, invoice_id: int) -> dict:
    """Whose invoice this is and what it still owes, for the counter screen to
    show BEFORE Take payment is enabled. Refuses another patient's invoice with
    the same sentence record_payment uses, so the screen and the ledger agree.
    """
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    with db_session() as db:
        invoice = _load_patient_invoice(db, int(invoice_id), patient_id,
                                        then="Check the invoice number.")
        due = _invoice_due_paise(db, invoice)
        person = db.execute("SELECT name FROM patients WHERE id = ?", (patient_id,)).fetchone()
    payable = int(invoice["payable_paise"] or 0)
    return {
        "invoice_id": int(invoice["id"]),
        "invoice_number": invoice.get("invoice_number"),
        "patient_id": patient_id,
        "patient_name": person["name"] if person else None,
        "status": invoice["status"],
        "allocatable": invoice["status"] not in UNALLOCATABLE_INVOICE_STATUSES,
        "payable_paise": payable,
        "payable_display": format_inr(payable),
        "due_paise": due,
        "due_display": format_inr(due),
    }


# ── Who the money is for, and who handled it ─────────────────────────────────

#: Invoice statuses that still carry a debt the counter can take money against.
#: A draft is not yet a bill (it has no number); paid, written-off, cancelled
#: and void owe nothing.
OPEN_INVOICE_STATUSES: tuple[str, ...] = ("issued", "partially_paid")

#: Shortest text the counter's patient search runs on. One character matches
#: half the register and identifies nobody.
PATIENT_SEARCH_MIN_CHARS = 2


def _require_registered_patient(db, patient_id: str, *,
                                then: str = "Nothing was taken.") -> dict:
    """The patient row, or a refusal a clerk can act on.

    Money tables carry no foreign keys (migration 20, rule 1), so until this
    check a receipt, refund or discount could be issued against any text typed
    into the Patient ID box, and that money then belonged to nobody the
    hospital could find. Checked before a document number is spent.
    """
    row = db.execute(
        "SELECT id, name, mrn FROM patients WHERE id = ?", (patient_id,)
    ).fetchone()
    if row is None:
        raise PaymentError(
            f"No patient with ID {patient_id} is registered. Check the ID, or "
            f"search by name or MRN. {then}"
        )
    return _row(row)


def _chunks(ids: Sequence[str], size: int = 400):
    """Keep an IN (...) list under SQLite's bound-variable ceiling."""
    for start in range(0, len(ids), size):
        yield ids[start:start + size]


def attach_people(rows: list[dict], *, dept: Optional[str] = None,
                  staff_keys: Sequence[str] = ()) -> list[dict]:
    """Put names on a list a person will read: the patient's name and MRN, and
    the name of each member of staff named in `staff_keys`.

    A cashier cannot match "PAT-50A2D09E" to the person at the counter, and
    "ADM001" is not a signature. The ids stay on every row; names go beside
    them as `patient_name`, `patient_mrn` and `<key>_name`, each None when no
    such patient or user exists.

    `dept` is the caller's ward scope (permissions.department_scope). A
    ward-scoped account gets names only for its own ward's patients, so a list
    that already crossed wards discloses nothing it did not before.
    """
    patient_ids = sorted({str(r["patient_id"]) for r in rows if r.get("patient_id")})
    staff_ids = sorted({str(r[k]) for r in rows for k in staff_keys if r.get(k)})
    patients: dict[str, dict] = {}
    staff: dict[str, str] = {}
    with db_session() as db:
        for chunk in _chunks(patient_ids):
            sql = (f"SELECT p.id, p.name, p.mrn FROM patients p "
                   f"WHERE p.id IN ({','.join('?' * len(chunk))})")
            args: list = list(chunk)
            if dept is not None:
                from permissions import PATIENT_DEPARTMENT_SQL
                sql += f" AND {PATIENT_DEPARTMENT_SQL} = ?"
                args.append(dept)
            for r in db.execute(sql, args).fetchall():
                patients[r["id"]] = {"name": r["name"], "mrn": r["mrn"]}
        for chunk in _chunks(staff_ids):
            for r in db.execute(
                f"SELECT id, name FROM users WHERE id IN ({','.join('?' * len(chunk))})",
                list(chunk),
            ).fetchall():
                staff[str(r["id"])] = r["name"]
    for row in rows:
        if "patient_id" in row:
            person = patients.get(str(row.get("patient_id"))) or {}
            row["patient_name"] = person.get("name")
            row["patient_mrn"] = person.get("mrn")
        for key in staff_keys:
            if key in row:
                row[f"{key}_name"] = staff.get(str(row[key])) if row[key] else None
    return rows


def search_patients(query: Optional[str], *, dept: Optional[str] = None,
                    limit: int = 20) -> list[dict]:
    """Patients the counter can take money for, by name, MRN, bed or exact ID.

    Who they are and where they are, nothing clinical: no diagnosis, no risk.
    Unlike the ward palette (/api/search/patients) discharged patients are
    included, because the final bill is settled at discharge and a refund is
    often paid weeks after it. Admitted patients sort first. `dept` scopes the
    QUERY, never a post-filter, so a LIMIT cannot hide an in-scope match.
    """
    text = (query or "").strip()
    if len(text) < PATIENT_SEARCH_MIN_CHARS:
        return []
    # A literal % or _ typed by the clerk must not become a wildcard.
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    sql = (
        "SELECT p.id, p.name, p.mrn, p.bed, p.ward, p.status FROM patients p "
        "WHERE (p.id = ? OR p.name LIKE ? ESCAPE '\\' OR p.mrn LIKE ? ESCAPE '\\' "
        "OR p.bed LIKE ? ESCAPE '\\')"
    )
    args: list = [text, like, like, like]
    if dept is not None:
        from permissions import PATIENT_DEPARTMENT_SQL
        sql += f" AND {PATIENT_DEPARTMENT_SQL} = ?"
        args.append(dept)
    sql += (
        " ORDER BY CASE WHEN p.id = ? THEN 0 WHEN p.mrn LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END,"
        " (p.status = 'admitted') DESC, p.name COLLATE NOCASE, p.id LIMIT ?"
    )
    args.extend([text, like, max(1, min(int(limit), 50))])
    with db_session() as db:
        return [
            {"id": r["id"], "name": r["name"], "mrn": r["mrn"], "bed": r["bed"],
             "ward": r["ward"], "status": r["status"]}
            for r in db.execute(sql, args).fetchall()
        ]


def open_invoices_for_payment(patient_id: str) -> list[dict]:
    """This patient's bills that still owe money, oldest first, so the counter
    offers a choice instead of an invoice number typed by hand (one wrong digit
    named another patient's bill)."""
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    marks = ",".join("?" * len(OPEN_INVOICE_STATUSES))
    out: list[dict] = []
    with db_session() as db:
        rows = [_row(r) for r in db.execute(
            f"SELECT * FROM invoices WHERE patient_id = ? AND status IN ({marks}) "
            "AND invoice_type != 'credit_note' "
            "ORDER BY COALESCE(issued_at, created_at), id",
            (patient_id, *OPEN_INVOICE_STATUSES),
        ).fetchall()]
        for inv in rows:
            due = _invoice_due_paise(db, inv)
            if due <= 0:
                continue
            payable = int(inv["payable_paise"] or 0)
            out.append({
                "invoice_id": int(inv["id"]),
                "invoice_number": inv["invoice_number"],
                "invoice_type": inv["invoice_type"],
                "status": inv["status"],
                "issued_at": inv["issued_at"],
                "payable_paise": payable,
                "payable_display": format_inr(payable),
                "due_paise": due,
                "due_display": format_inr(due),
            })
    return out


def _allocated_paise(db, invoice_id: int) -> int:
    """What is currently allocated to this invoice, reversals excluded. This —
    not a running counter — is the authority: recomputing from the allocation
    rows makes every update idempotent and makes a lost update impossible."""
    row = db.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS s FROM payment_allocations "
        "WHERE invoice_id = ? AND reversed_at IS NULL",
        (int(invoice_id),),
    ).fetchone()
    return int(row["s"])


def _invoice_due_paise(db, invoice: dict) -> int:
    """Remaining due. Never negative — see rule 2: an over-payment is an advance,
    so 'due' bottoms out at zero and the excess goes to the deposit ledger."""
    payable = int(invoice["payable_paise"] or 0)
    return max(payable - _allocated_paise(db, int(invoice["id"])), 0)


def _recompute_invoice(db, invoice_id: int) -> dict:
    """Re-derive `amount_paid_paise` / `balance_paise` / `status` from the
    allocations. Called after every allocation and every reversal.

    STATUS IS ONLY TOUCHED IN THE PAID DIMENSION. draft/cancelled/void/
    written_off are the billing module's states and are left exactly as found —
    this module must never promote a draft to 'issued' or resurrect a void.
    """
    invoice = _load_invoice(db, invoice_id)
    paid = _allocated_paise(db, invoice_id)
    payable = int(invoice["payable_paise"] or 0)
    balance = payable - paid
    status = invoice["status"]
    if status in ("issued", "partially_paid", "paid"):
        if payable > 0 and paid >= payable:
            status = "paid"
        elif paid > 0:
            status = "partially_paid"
        else:
            status = "issued"
    db.execute(
        "UPDATE invoices SET amount_paid_paise = ?, balance_paise = ?, status = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (paid, balance, status, int(invoice_id)),
    )
    invoice["amount_paid_paise"] = paid
    invoice["balance_paise"] = balance
    invoice["status"] = status
    return invoice


# ── Deposits ─────────────────────────────────────────────────────────────────

def _deposit_balance(db, patient_id: str, admission_id: Optional[str] = None) -> int:
    """SUM of the signed ledger. `patient_deposits.amount_paise` is the one
    signed money column in the spine precisely so this is a single SUM."""
    sql = "SELECT COALESCE(SUM(amount_paise), 0) AS s FROM patient_deposits WHERE patient_id = ?"
    args: list = [patient_id]
    if admission_id:
        sql += " AND admission_id = ?"
        args.append(admission_id)
    return int(db.execute(sql, tuple(args)).fetchone()["s"])


def _insert_deposit(db, *, patient_id: str, movement: str, amount_paise: int,
                    admission_id: Optional[str] = None, payment_id: Optional[int] = None,
                    invoice_id: Optional[int] = None, created_by: Optional[str] = None,
                    notes: Optional[str] = None) -> int:
    cur = db.execute(
        "INSERT INTO patient_deposits (patient_id, admission_id, movement, amount_paise, "
        "payment_id, invoice_id, created_by, created_at, notes) VALUES (?,?,?,?,?,?,?,?,?)",
        (patient_id, admission_id, movement, assert_paise(amount_paise), payment_id,
         invoice_id, created_by, _utc_now_str(), notes),
    )
    return int(cur.lastrowid)


def deposit_balance(patient_id: str, *, admission_id: Optional[str] = None) -> dict:
    """What the hospital is holding for this patient, right now."""
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    with db_session() as db:
        balance = _deposit_balance(db, patient_id, admission_id)
        count = int(db.execute(
            "SELECT COUNT(*) AS c FROM patient_deposits WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()["c"])
    return {
        "patient_id": patient_id,
        "admission_id": admission_id,
        "balance_paise": balance,
        "balance_display": format_inr(balance),
        "entry_count": count,
    }


def list_deposit_entries(patient_id: str, *, admission_id: Optional[str] = None,
                         limit: int = 200) -> list[dict]:
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    sql = "SELECT * FROM patient_deposits WHERE patient_id = ?"
    args: list = [patient_id]
    if admission_id:
        sql += " AND admission_id = ?"
        args.append(admission_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, tuple(args)).fetchall()]
    out = []
    for r in rows:
        amount = int(r["amount_paise"])
        out.append({
            "id": int(r["id"]),
            "movement": r["movement"],
            "amount_paise": amount,
            "amount_display": format_inr(amount),
            "payment_id": r["payment_id"],
            "invoice_id": r["invoice_id"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "notes": r["notes"],
        })
    return out


def plan_deposit_split(total_paise: int, dues_paise: Sequence[int]) -> list[int]:
    """Split an advance across several open invoices so the parts sum EXACTLY to
    the total (`money.split_shares`, largest-remainder), then cap each share at
    that invoice's own due and re-offer the freed paise to the others.

    Pure arithmetic, no database — a UI can preview a split before anybody
    commits it, and the preview is the same maths the commit uses.
    """
    total = _positive_paise(total_paise, "total_paise")
    dues = [assert_non_negative_paise(d, "due_paise") for d in dues_paise]
    if not dues:
        raise PaymentError("no invoices to split across")
    capacity = sum(dues)
    if capacity == 0:
        return [0] * len(dues)
    distributable = min(total, capacity)

    shares = split_shares(distributable, dues)
    # Cap-and-redistribute. `split_shares` is proportional to the dues, so an
    # over-cap can only happen through the largest-remainder tie-breaking; the
    # loop is bounded by the number of invoices.
    for _ in range(len(dues)):
        excess = 0
        for i, due in enumerate(dues):
            if shares[i] > due:
                excess += shares[i] - due
                shares[i] = due
        if excess == 0:
            break
        room = [max(due - shares[i], 0) for i, due in enumerate(dues)]
        if sum(room) == 0:
            break
        extra = split_shares(excess, room)
        for i in range(len(dues)):
            shares[i] += extra[i]
    # THE INVARIANT: the parts are the whole. A split that loses a paise leaves
    # an unexplained balance nobody can clear.
    if sum(shares) != distributable:  # pragma: no cover - defensive
        raise ReconciliationError(
            f"deposit split lost money: parts sum to {sum(shares)}, total is {distributable}"
        )
    return shares


# ── Receipts ─────────────────────────────────────────────────────────────────

def _find_by_idempotency(db, key: str) -> Optional[dict]:
    row = db.execute(
        "SELECT * FROM payments WHERE idempotency_key = ?", (key,)
    ).fetchone()
    return _row(row) if row else None


def _require_open_session(db, session_id: int) -> dict:
    row = db.execute(
        "SELECT * FROM cash_counter_sessions WHERE id = ?", (int(session_id),)
    ).fetchone()
    if row is None:
        raise PaymentError(f"counter session {session_id} does not exist")
    session = _row(row)
    if session["status"] != "open":
        raise PaymentError(
            f"counter session {session_id} is {session['status']}; a closed session "
            "cannot take money. Open the next shift's session first"
        )
    return session


def _allocatable_invoice(db, invoice_id: int, patient_id: str) -> dict:
    """The invoice a receipt is taken against: this patient's, and a debt."""
    invoice = _load_patient_invoice(db, int(invoice_id), patient_id)
    if invoice["status"] in UNALLOCATABLE_INVOICE_STATUSES:
        raise PaymentError(
            f"invoice {invoice_id} is {invoice['status']}; it is not a debt. "
            "Take the money as an advance instead."
        )
    return invoice


def record_payment(
    *,
    patient_id: str,
    amount_paise: int,
    method: str,
    purpose: str = "invoice_payment",
    invoice_id: Optional[int] = None,
    admission_id: Optional[str] = None,
    payer_id: Optional[int] = None,
    reference_no: Optional[str] = None,
    bank_name: Optional[str] = None,
    instrument_date: Optional[str] = None,
    counter_session_id: Optional[int] = None,
    received_by: Optional[str] = None,
    notes: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Take money and issue a receipt. THE entry point for money coming in.

    Allocation, and the over-payment rule (rule 2):
      * with an `invoice_id`, the receipt is allocated to that invoice up to its
        remaining due and no further;
      * anything left over becomes an ADVANCE on the patient's deposit ledger and
        is reported as `advance_paise`;
      * with no `invoice_id` (an advance or an admission deposit) the whole
        amount goes to the ledger.

    So `invoices.amount_paid_paise` can never exceed `invoices.payable_paise`,
    and no balance column can go negative through this path.

    IDEMPOTENT ON `idempotency_key`. A double-tapped counter button, or an
    offline queue replaying, must not take the money twice. The key is checked
    inside the write transaction and backed by a UNIQUE index, so two concurrent
    replays cannot both insert.
    """
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    amount = _positive_paise(amount_paise, "amount_paise")
    method = _text(method, "method", required=True, max_len=32)
    if method not in PAYMENT_METHODS:
        raise PaymentError(f"unknown payment method {method!r}")
    purpose = _text(purpose, "purpose", required=True, max_len=32)
    if purpose not in RECEIPT_PURPOSES:
        raise PaymentError(
            f"{purpose!r} is not a receipt purpose; refunds go through "
            "request_refund/pay_refund and write-offs through billing"
        )
    reference_no = _text(reference_no, "reference_no", max_len=_MAX_REF)
    if method in REFERENCE_REQUIRED_METHODS and not reference_no:
        raise PaymentError(
            f"a {method} payment needs a reference number, without one it cannot "
            "be matched to the bank statement at month end"
        )
    # CASH WITH NO DRAWER IS RECORDED, AND RECORDED AS SUCH.
    #
    # Cash physically taken with no counter session open lands in the day's
    # takings but in nobody's till: `counter_session_expected` scopes itself to
    # `counter_session_id`, so this money is in no shift's expected figure, and
    # the day-end variance therefore cannot be attributed to anyone.
    #
    # WHY NOT REFUSE IT. The refusal would arrive AFTER the patient has handed
    # over the notes — the one moment where "no" does not undo anything. A clerk
    # holding ₹5,000 that Orb will not accept writes it in a book, or takes it as
    # "card", and unrecorded cash is strictly worse than unattributed cash: the
    # first is invisible, the second is a number with a name on it that a
    # supervisor can chase. (The counter-session requirement is still the right
    # END STATE; it belongs at the till-open step in the UI, not at the moment
    # money is already on the counter.)
    #
    # So: take it, mark it, and make the day-end refuse to call itself final
    # while any of it exists — see `day_end_summary`. `unattributed_cash` is
    # derived from the receipt's own immutable columns (`is_unattributed_cash`),
    # never stored, so it cannot drift.
    orphan_cash = method in CASH_METHODS and counter_session_id is None

    bank_name = _text(bank_name, "bank_name", max_len=200)
    instrument_date = _iso_date(instrument_date, "instrument_date")
    notes = _text(notes, "notes")
    received_by = _text(received_by, "received_by", max_len=120)
    admission_id = _text(admission_id, "admission_id", max_len=64)
    idempotency_key = _text(idempotency_key, "idempotency_key", max_len=200)

    # Replay check BEFORE burning a receipt number (see the module docstring on
    # why numbers are allocated outside the transaction). Every refusal this
    # receipt can meet is checked here too: the patient, the invoice (its
    # patient and that it is still a debt) and the counter session. A closed
    # session or a cancelled invoice used to be refused only after the number
    # was drawn, leaving a gap in the receipt series for every refusal. They are
    # checked again under the write lock below, for a state that changes between.
    with db_session() as db:
        if idempotency_key:
            existing = _find_by_idempotency(db, idempotency_key)
            if existing:
                view = _payment_view(existing)
                view["replayed"] = True
                return view
        _require_registered_patient(db, patient_id)
        if invoice_id is not None:
            _allocatable_invoice(db, int(invoice_id), patient_id)
        if counter_session_id is not None:
            _require_open_session(db, counter_session_id)

    doc = _next_number("RCPT", patient_id=patient_id, admission_id=admission_id)
    now = _utc_now_str()

    with db_session(immediate=True) as db:
        if idempotency_key:
            existing = _find_by_idempotency(db, idempotency_key)
            if existing:
                view = _payment_view(existing)
                view["replayed"] = True
                return view

        if counter_session_id is not None:
            _require_open_session(db, counter_session_id)

        allocate_to: Optional[dict] = None
        allocated = 0
        if invoice_id is not None:
            allocate_to = _allocatable_invoice(db, int(invoice_id), patient_id)
            allocated = min(amount, _invoice_due_paise(db, allocate_to))

        advance = amount - allocated

        cur = db.execute(
            "INSERT INTO payments (receipt_number, patient_id, admission_id, invoice_id, "
            "payer_id, direction, amount_paise, method, reference_no, bank_name, "
            "instrument_date, purpose, status, counter_session_id, received_by, "
            "received_at, idempotency_key, notes, "
            + facility_identity.SNAPSHOT_COLUMNS_SQL + ") "
            "VALUES (?,?,?,?,?,'in',?,?,?,?,?,?,'received',?,?,?,?,?,"
            + facility_identity.SNAPSHOT_PLACEHOLDERS + ")",
            (doc["number"], patient_id, admission_id, invoice_id, payer_id, amount,
             method, reference_no, bank_name, instrument_date, purpose,
             counter_session_id, received_by, now, idempotency_key, notes,
             *facility_identity.supplier_snapshot(
                 db, patient_id=patient_id, admission_id=admission_id)),
        )
        payment_id = int(cur.lastrowid)

        if allocated > 0 and allocate_to is not None:
            db.execute(
                "INSERT INTO payment_allocations (payment_id, invoice_id, amount_paise, "
                "allocated_by, allocated_at) VALUES (?,?,?,?,?)",
                (payment_id, int(allocate_to["id"]), allocated, received_by, now),
            )
            _recompute_invoice(db, int(allocate_to["id"]))

        if advance > 0:
            _insert_deposit(
                db,
                patient_id=patient_id,
                admission_id=admission_id,
                movement="deposit",
                amount_paise=advance,
                payment_id=payment_id,
                invoice_id=invoice_id,
                created_by=received_by,
                notes=(
                    f"over-payment on receipt {doc['number']} held as advance"
                    if allocated > 0 else None
                ),
            )

        balance = _deposit_balance(db, patient_id)
        row = _row(db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone())
        series_configured = _receipt_series_is_configured(db, doc["facility_id"])

        _audit(
            db,
            action="payment_recorded",
            # 'partial_success' for drawer-less cash, the same outcome
            # close_counter_session gives a shift that did not balance: the money
            # was taken, and something about it still has to be explained.
            outcome="partial_success" if orphan_cash else "success",
            actor=received_by,
            patient_id=patient_id,
            target_type="payment",
            target_id=payment_id,
            summary=(
                f"Receipt {doc['number']}: {format_inr(amount)} by {method}"
                + (". NO COUNTER SESSION: this cash is in no drawer"
                   if orphan_cash else "")
            ),
            metadata={
                "receipt_number": doc["number"],
                "amount_paise": amount,
                "method": method,
                "purpose": purpose,
                "invoice_id": invoice_id,
                "allocated_paise": allocated,
                "advance_paise": advance,
                "counter_session_id": counter_session_id,
                "unattributed_cash": orphan_cash,
            },
        )

    if orphan_cash:
        logger.warning(
            "Receipt %s took %s in cash with no counter session open; it is in the "
            "day's takings and in no drawer, and the day-end cannot be finalised "
            "until it is explained.",
            doc["number"], format_inr(amount),
        )

    view = _payment_view(row)
    view.update({
        "replayed": False,
        "allocated_paise": allocated,
        "allocated_display": format_inr(allocated),
        "advance_paise": advance,
        "advance_display": format_inr(advance),
        "deposit_balance_paise": balance,
        "deposit_balance_display": format_inr(balance),
        # A site that has not set its real receipt series is printing a
        # PLACEHOLDER number. Say so, everywhere it is shown.
        "receipt_series_is_configured": series_configured,
        "over_payment": advance > 0 and allocated > 0,
    })
    return view


def get_payment(payment_id: int) -> Optional[dict]:
    with db_session() as db:
        row = db.execute("SELECT * FROM payments WHERE id = ?", (int(payment_id),)).fetchone()
        if row is None:
            return None
        view = _payment_view(_row(row))
        allocations = [
            {
                "id": int(a["id"]),
                "invoice_id": int(a["invoice_id"]),
                "amount_paise": int(a["amount_paise"]),
                "amount_display": format_inr(int(a["amount_paise"])),
                "reversed_at": a["reversed_at"],
            }
            for a in db.execute(
                "SELECT * FROM payment_allocations WHERE payment_id = ? ORDER BY id",
                (int(payment_id),),
            ).fetchall()
        ]
        view["allocations"] = allocations
        # The series of the facility the receipt was issued by (its snapshot).
        issued_by = row["supplier_facility_id"] if "supplier_facility_id" in row.keys() else None
        view["receipt_series_is_configured"] = _receipt_series_is_configured(
            db, int(issued_by or MAIN_FACILITY_ID))
    return view


def receipt_print_view(payment_id: int) -> Optional[dict]:
    """Everything a printed receipt needs, and nothing it must invent
    (multisite-2). None when no receipt has that id.

    The supplier block is the one the receipt was issued with: every receipt
    is numbered when it is written, so it carries the snapshot taken then
    (migration 72), and one written before that migration prints the old block
    (site name, address and GSTIN not configured) exactly as before. Changing
    the hospital's details afterwards never changes a receipt already handed
    over. A placeholder receipt series is said in words, as on the invoice."""
    from services.billing_service import amount_in_words

    receipt = get_payment(payment_id)
    if receipt is None:
        return None
    with db_session() as db:
        row = _row(db.execute("SELECT * FROM payments WHERE id = ?",
                              (int(payment_id),)).fetchone())
    supplier = facility_identity.supplier_block(row, number_field="receipt_number")
    configured = bool(receipt.get("receipt_series_is_configured"))
    return {
        "receipt": receipt,
        "supplier": supplier,
        "number_series_configured": configured,
        "number_notice": (
            None if configured else
            "Receipt numbers come from a placeholder series. Set the hospital's real "
            "receipt series before treating these numbers as final."
        ),
        "amount_in_words": amount_in_words(int(receipt["amount_paise"])),
        "printed_at": _utc_now_str(),
    }


def list_payments(
    *,
    patient_id: Optional[str] = None,
    invoice_id: Optional[int] = None,
    counter_session_id: Optional[int] = None,
    status: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    clauses: list[str] = []
    args: list = []
    if patient_id:
        clauses.append("patient_id = ?")
        args.append(patient_id)
    if invoice_id is not None:
        clauses.append("invoice_id = ?")
        args.append(int(invoice_id))
    if counter_session_id is not None:
        clauses.append("counter_session_id = ?")
        args.append(int(counter_session_id))
    if status:
        clauses.append("status = ?")
        args.append(status)
    if direction:
        clauses.append("direction = ?")
        args.append(direction)
    sql = "SELECT * FROM payments"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with db_session() as db:
        rows = db.execute(sql, tuple(args)).fetchall()
        return [_payment_view(_row(r)) for r in rows]


def amend_payment(payment_id: int, **changes: Any) -> dict:
    """ALWAYS RAISES. This function exists so the rule has a name.

    A receipt is a document the hospital has already handed to a patient. Editing
    it changes what the hospital says it received AFTER saying it — which is the
    single most useful thing to be able to do if you are stealing from a till,
    and the single hardest thing to detect afterwards. The audit chain treats a
    correction the same way (a new row, never an UPDATE), and so does a signed
    clinical note (a superseding version, never an edit).

    The correction path is `reverse_payment()`: it writes an opposing entry,
    links it to the original, and leaves both on the ledger.
    """
    fields = sorted(changes)
    raise ImmutablePaymentError(
        f"receipt {payment_id} cannot be edited"
        + (f" (attempted: {', '.join(fields)})" if fields else "")
        + ". A receipt is immutable once issued. Reverse it with "
          "reverse_payment() and record a new one."
    )


def reverse_payment(payment_id: int, *, reason: str, reversed_by: str) -> dict:
    """Undo a receipt the honest way: a NEW, opposing, linked entry.

    What this writes:
      * a new `payments` row, direction 'out', same amount and method, carrying
        `reversal_of_payment_id`;
      * on the ORIGINAL row, the lifecycle columns only — status 'reversed', who,
        when and why. Not one financial column is touched.
      * `reversed_at` on the original's allocations, then a recompute of every
        invoice they touched;
      * an opposing deposit-ledger entry for any advance the receipt created.

    Both halves stay visible forever. That is the point: a reversal that erased
    the original would be an edit wearing a different name.

    FOUR EYES, as on a refund decision: the person who took the payment may not
    reverse it. Voiding your own receipt is the till fraud that receipt
    immutability exists to stop, and the approve permission alone did not stop
    it for an account that holds both verbs (every admin does).
    """
    reason = _text(reason, "reason", required=True)
    reversed_by = _text(reversed_by, "reversed_by", required=True, max_len=120)

    with db_session() as db:
        row = db.execute("SELECT * FROM payments WHERE id = ?", (int(payment_id),)).fetchone()
        if row is None:
            raise PaymentError(f"payment {payment_id} does not exist")
        original = _row(row)
    if original["direction"] != "in":
        raise PaymentError(
            "only a money-IN receipt can be reversed; money out is corrected by "
            "recording the money coming back in"
        )
    if original["status"] not in LIVE_PAYMENT_STATUSES:
        raise PaymentError(
            f"payment {payment_id} is {original['status']} and cannot be reversed again"
        )
    # Checked before a receipt number is spent. `received_by` is immutable on a
    # receipt, so the write transaction below has nothing to re-check.
    if (original["received_by"] or "").strip().lower() == reversed_by.strip().lower():
        raise PaymentError(
            "a receipt cannot be reversed by the person who took the payment. "
            "A second person must sign for a reversal"
        )

    doc = _next_number("RCPT", patient_id=original["patient_id"],
                       admission_id=original["admission_id"])
    now = _utc_now_str()

    with db_session(immediate=True) as db:
        # Re-read under the write lock: two clerks reversing the same receipt at
        # the same second must not both succeed.
        current = _row(db.execute(
            "SELECT * FROM payments WHERE id = ?", (int(payment_id),)
        ).fetchone())
        if current["status"] not in LIVE_PAYMENT_STATUSES:
            raise PaymentError(
                f"payment {payment_id} is {current['status']} and cannot be reversed again"
            )

        amount = int(current["amount_paise"])
        cur = db.execute(
            "INSERT INTO payments (receipt_number, patient_id, admission_id, invoice_id, "
            "payer_id, direction, amount_paise, method, reference_no, bank_name, "
            "instrument_date, purpose, status, counter_session_id, "
            "reversal_of_payment_id, received_by, received_at, notes, "
            + facility_identity.SNAPSHOT_COLUMNS_SQL + ") "
            "VALUES (?,?,?,?,?,'out',?,?,?,?,?,?,'received',?,?,?,?,?,"
            + facility_identity.SNAPSHOT_PLACEHOLDERS + ")",
            (doc["number"], current["patient_id"], current["admission_id"],
             current["invoice_id"], current["payer_id"], amount, current["method"],
             current["reference_no"], current["bank_name"], current["instrument_date"],
             current["purpose"], current["counter_session_id"], int(payment_id),
             reversed_by, now, f"Reversal of receipt {current['receipt_number']}: {reason}",
             *facility_identity.supplier_snapshot(
                 db, patient_id=current["patient_id"], admission_id=current["admission_id"])),
        )
        reversal_id = int(cur.lastrowid)

        # LIFECYCLE ONLY. Nothing here changes what the original receipt says
        # about the money — only that it has been superseded, by whom and why.
        db.execute(
            "UPDATE payments SET status = 'reversed', cancelled_by = ?, cancelled_at = ?, "
            "cancel_reason = ? WHERE id = ?",
            (reversed_by, now, reason, int(payment_id)),
        )

        touched_invoices = [
            int(a["invoice_id"])
            for a in db.execute(
                "SELECT invoice_id FROM payment_allocations "
                "WHERE payment_id = ? AND reversed_at IS NULL",
                (int(payment_id),),
            ).fetchall()
        ]
        db.execute(
            "UPDATE payment_allocations SET reversed_at = ?, reversed_by = ? "
            "WHERE payment_id = ? AND reversed_at IS NULL",
            (now, reversed_by, int(payment_id)),
        )
        for inv_id in touched_invoices:
            _recompute_invoice(db, inv_id)

        advance_row = db.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS s FROM patient_deposits "
            "WHERE payment_id = ? AND movement = 'deposit'",
            (int(payment_id),),
        ).fetchone()
        advance_created = int(advance_row["s"])
        if advance_created:
            _insert_deposit(
                db,
                patient_id=current["patient_id"],
                admission_id=current["admission_id"],
                movement="refund",
                amount_paise=-advance_created,
                payment_id=reversal_id,
                created_by=reversed_by,
                notes=f"reversal of receipt {current['receipt_number']}",
            )

        reversal = _row(db.execute(
            "SELECT * FROM payments WHERE id = ?", (reversal_id,)
        ).fetchone())
        original_after = _row(db.execute(
            "SELECT * FROM payments WHERE id = ?", (int(payment_id),)
        ).fetchone())

        _audit(
            db,
            action="payment_reversed",
            outcome="success",
            actor=reversed_by,
            patient_id=current["patient_id"],
            target_type="payment",
            target_id=payment_id,
            summary=f"Reversed receipt {current['receipt_number']} ({format_inr(amount)}): {reason}",
            metadata={
                "reversal_payment_id": reversal_id,
                "reversal_receipt_number": doc["number"],
                "amount_paise": amount,
                "reason": reason,
                "invoices_recomputed": touched_invoices,
                "advance_withdrawn_paise": advance_created,
            },
        )

    return {
        "original": _payment_view(original_after),
        "reversal": _payment_view(reversal),
        "invoices_recomputed": touched_invoices,
        "advance_withdrawn_paise": advance_created,
    }


def apply_deposit_to_invoice(
    *,
    patient_id: str,
    invoice_id: int,
    amount_paise: int,
    applied_by: str,
    admission_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Move money the hospital is already holding onto an invoice.

    This is NOT new money at the counter, so it is recorded with method
    'adjustment' — the cash drawer at shift end must not count it as cash taken.
    It still becomes an allocation, so `invoices.amount_paid_paise` stays exactly
    equal to the sum of its allocations, with no second way to be paid.
    """
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    applied_by = _text(applied_by, "applied_by", required=True, max_len=120)
    amount = _positive_paise(amount_paise, "amount_paise")
    notes = _text(notes, "notes")

    def check(db) -> dict:
        invoice = _load_patient_invoice(db, int(invoice_id), patient_id)
        if invoice["status"] in UNALLOCATABLE_INVOICE_STATUSES:
            raise PaymentError(f"invoice {invoice_id} is {invoice['status']}")
        balance = _deposit_balance(db, patient_id, admission_id)
        if amount > balance:
            raise PaymentError(
                f"advance balance is {format_inr(balance)}; cannot apply "
                f"{format_inr(amount)}"
            )
        due = _invoice_due_paise(db, invoice)
        if amount > due:
            raise PaymentError(
                f"invoice {invoice_id} has {format_inr(due)} outstanding; applying "
                f"{format_inr(amount)} would over-pay it. Apply the balance and "
                "leave the rest on account."
            )
        return invoice

    # Every check before a receipt number is spent (one patient's advance must
    # never settle another patient's bill, and a refusal must leave no gap in
    # the series), then again under the write lock for a balance that moved.
    with db_session() as db:
        check(db)

    doc = _next_number("RCPT", patient_id=patient_id, admission_id=admission_id)
    now = _utc_now_str()

    with db_session(immediate=True) as db:
        invoice = check(db)

        cur = db.execute(
            "INSERT INTO payments (receipt_number, patient_id, admission_id, invoice_id, "
            "direction, amount_paise, method, purpose, status, received_by, received_at, notes, "
            + facility_identity.SNAPSHOT_COLUMNS_SQL + ") "
            "VALUES (?,?,?,?,'in',?,'adjustment','settlement','received',?,?,?,"
            + facility_identity.SNAPSHOT_PLACEHOLDERS + ")",
            (doc["number"], patient_id, admission_id, int(invoice_id), amount,
             applied_by, now, notes or "advance applied to invoice",
             *facility_identity.supplier_snapshot(
                 db, patient_id=patient_id, admission_id=admission_id)),
        )
        payment_id = int(cur.lastrowid)
        db.execute(
            "INSERT INTO payment_allocations (payment_id, invoice_id, amount_paise, "
            "allocated_by, allocated_at, notes) VALUES (?,?,?,?,?,?)",
            (payment_id, int(invoice_id), amount, applied_by, now, "from patient advance"),
        )
        _insert_deposit(
            db,
            patient_id=patient_id,
            admission_id=admission_id,
            movement="apply",
            amount_paise=-amount,
            payment_id=payment_id,
            invoice_id=int(invoice_id),
            created_by=applied_by,
            notes=notes,
        )
        updated = _recompute_invoice(db, int(invoice_id))
        remaining = _deposit_balance(db, patient_id, admission_id)
        row = _row(db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone())

        _audit(
            db,
            action="deposit_applied",
            outcome="success",
            actor=applied_by,
            patient_id=patient_id,
            target_type="invoice",
            target_id=invoice_id,
            summary=f"Applied {format_inr(amount)} advance to invoice {invoice_id}",
            metadata={"amount_paise": amount, "payment_id": payment_id,
                      "deposit_balance_paise": remaining},
        )

    view = _payment_view(row)
    view.update({
        "allocated_paise": amount,
        "advance_paise": 0,
        "deposit_balance_paise": remaining,
        "deposit_balance_display": format_inr(remaining),
        "invoice_status": updated["status"],
        "invoice_balance_paise": updated["balance_paise"],
    })
    return view


# ── Refunds ──────────────────────────────────────────────────────────────────

def _open_refund_total(db, *, patient_id: Optional[str] = None,
                       payment_id: Optional[int] = None,
                       invoice_id: Optional[int] = None,
                       include_paid: bool = True) -> int:
    """Refunds already claiming the same money. `requested` and `approved` are
    counted as well as `paid` — otherwise two clerks each raise a full refund
    against one payment and both pass the cap."""
    statuses = ["requested", "approved"] + (["paid"] if include_paid else [])
    placeholders = ",".join("?" * len(statuses))
    clauses = [f"status IN ({placeholders})"]
    args: list = list(statuses)
    if payment_id is not None:
        clauses.append("payment_id = ?")
        args.append(int(payment_id))
    elif invoice_id is not None:
        clauses.append("invoice_id = ?")
        args.append(int(invoice_id))
    elif patient_id is not None:
        clauses.append("patient_id = ?")
        args.append(patient_id)
    row = db.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS s FROM refunds WHERE "
        + " AND ".join(clauses),
        tuple(args),
    ).fetchone()
    return int(row["s"])


def _refundable(db, *, patient_id: str, payment_id: Optional[int] = None,
                invoice_id: Optional[int] = None) -> dict:
    """The cap. THE RULE: you cannot give back more than you took.

    Three scopes, narrowest first:
      * against ONE receipt — that receipt's amount, less refunds already
        claiming it;
      * against ONE invoice — what is allocated to it and not reversed, less
        refunds already claiming it;
      * against the PATIENT — everything received less everything already paid
        back, less refunds in flight. A 'paid' refund already has its own
        money-out payment row, so counting it again here would halve the cap.
    """
    if payment_id is not None:
        row = db.execute("SELECT * FROM payments WHERE id = ?", (int(payment_id),)).fetchone()
        if row is None:
            raise PaymentError(f"payment {payment_id} does not exist")
        payment = _row(row)
        if payment["patient_id"] != patient_id:
            raise PaymentError(
                f"payment {payment_id} belongs to a different patient"
            )
        if payment["direction"] != "in" or payment["status"] not in LIVE_PAYMENT_STATUSES:
            paid = 0
        else:
            paid = int(payment["amount_paise"])
        claimed = _open_refund_total(db, payment_id=int(payment_id))
        return {
            "scope": "payment",
            "paid_paise": paid,
            "already_refunded_paise": claimed,
            "refundable_paise": max(paid - claimed, 0),
        }

    if invoice_id is not None:
        # The receipt scope above refuses another patient's payment; the
        # invoice scope must refuse another patient's bill the same way.
        _load_patient_invoice(db, int(invoice_id), patient_id)
        paid = _allocated_paise(db, int(invoice_id))
        claimed = _open_refund_total(db, invoice_id=int(invoice_id))
        return {
            "scope": "invoice",
            "paid_paise": paid,
            "already_refunded_paise": claimed,
            "refundable_paise": max(paid - claimed, 0),
        }

    # Neither leg of a reversal (see _NOT_A_REVERSAL): counting only the 'out'
    # leg took a reversed receipt off twice and capped the patient's other,
    # real receipts below what they paid.
    placeholders = ",".join("?" * len(LIVE_PAYMENT_STATUSES))
    row = db.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN direction = 'in' THEN amount_paise ELSE 0 END), 0) AS took, "
        "COALESCE(SUM(CASE WHEN direction = 'out' THEN amount_paise ELSE 0 END), 0) AS gave "
        f"FROM payments WHERE patient_id = ? AND status IN ({placeholders}) "
        f"AND {_NOT_A_REVERSAL}",
        (patient_id, *LIVE_PAYMENT_STATUSES),
    ).fetchone()
    net = int(row["took"]) - int(row["gave"])
    claimed = _open_refund_total(db, patient_id=patient_id, include_paid=False)
    return {
        "scope": "patient",
        "paid_paise": net,
        "already_refunded_paise": claimed,
        "refundable_paise": max(net - claimed, 0),
    }


def refundable_paise(patient_id: str, *, payment_id: Optional[int] = None,
                     invoice_id: Optional[int] = None) -> dict:
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    with db_session() as db:
        result = _refundable(db, patient_id=patient_id, payment_id=payment_id,
                             invoice_id=invoice_id)
        result["deposit_balance_paise"] = _deposit_balance(db, patient_id)
    result["patient_id"] = patient_id
    result["refundable_display"] = format_inr(result["refundable_paise"])
    result["deposit_balance_display"] = format_inr(result["deposit_balance_paise"])
    return result


def _check_refund_ceiling(db, *, patient_id: str, amount: int,
                          payment_id: Optional[int], invoice_id: Optional[int],
                          admission_id: Optional[str]) -> tuple[dict, int]:
    """Refuse a refund above what can be handed back; return (cap, ceiling)."""
    cap = _refundable(db, patient_id=patient_id, payment_id=payment_id,
                      invoice_id=invoice_id)
    deposit = _deposit_balance(db, patient_id, admission_id)
    is_deposit_refund = payment_id is None and invoice_id is None
    # A deposit refund is capped by the ADVANCE HELD, which is the narrower
    # and more honest number: money already spent on an invoice is not
    # sitting there to be handed back.
    ceiling = min(cap["refundable_paise"], deposit) if is_deposit_refund \
        else cap["refundable_paise"]
    if amount > ceiling:
        raise PaymentError(
            f"cannot refund {format_inr(amount)}, at most {format_inr(ceiling)} "
            f"is refundable ({cap['scope']} scope; "
            f"{format_inr(cap['paid_paise'])} received, "
            f"{format_inr(cap['already_refunded_paise'])} already claimed"
            + (f", {format_inr(deposit)} held on account" if is_deposit_refund else "")
            + ")"
        )
    return cap, ceiling


def request_refund(
    *,
    patient_id: str,
    amount_paise: int,
    reason: str,
    requested_by: str,
    payment_id: Optional[int] = None,
    invoice_id: Optional[int] = None,
    admission_id: Optional[str] = None,
    reason_code: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Raise a refund. Never a bare negative entry — a refund always names what
    it refunds (a receipt, an invoice, or the patient's advance balance) and
    always carries a reason.

    Capped at the moment it is raised AND again at the moment it is paid, because
    money can move in between.
    """
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    amount = _positive_paise(amount_paise, "amount_paise")
    reason = _text(reason, "reason", required=True)
    requested_by = _text(requested_by, "requested_by", required=True, max_len=120)
    reason_code = _text(reason_code, "reason_code", max_len=64)
    admission_id = _text(admission_id, "admission_id", max_len=64)
    idempotency_key = _text(idempotency_key, "idempotency_key", max_len=200)

    with db_session() as db:
        if idempotency_key:
            row = db.execute(
                "SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row is not None:
                view = _refund_view(_row(row))
                view["replayed"] = True
                return view
        # Before the number is spent. Without it an unknown id was refused
        # only by the cap, as "at most ₹0.00 is refundable", which names the
        # wrong problem.
        _require_registered_patient(db, patient_id, then="No refund was raised.")
        # The cap too, so an over-sized request draws no refund number; it is
        # checked again under the write lock below.
        _check_refund_ceiling(db, patient_id=patient_id, amount=amount,
                              payment_id=payment_id, invoice_id=invoice_id,
                              admission_id=admission_id)

    doc = _next_number("REF", patient_id=patient_id, admission_id=admission_id)
    now = _utc_now_str()

    with db_session(immediate=True) as db:
        if idempotency_key:
            row = db.execute(
                "SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row is not None:
                view = _refund_view(_row(row))
                view["replayed"] = True
                return view

        cap, ceiling = _check_refund_ceiling(
            db, patient_id=patient_id, amount=amount, payment_id=payment_id,
            invoice_id=invoice_id, admission_id=admission_id)

        cur = db.execute(
            "INSERT INTO refunds (refund_number, patient_id, admission_id, invoice_id, "
            "payment_id, amount_paise, reason_code, reason, status, requested_by, "
            "requested_at, idempotency_key) VALUES (?,?,?,?,?,?,?,?,'requested',?,?,?)",
            (doc["number"], patient_id, admission_id, invoice_id, payment_id, amount,
             reason_code, reason, requested_by, now, idempotency_key),
        )
        refund_id = int(cur.lastrowid)
        row = _row(db.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone())

        _audit(
            db,
            action="refund_requested",
            outcome="success",
            actor=requested_by,
            patient_id=patient_id,
            target_type="refund",
            target_id=refund_id,
            summary=f"Refund {doc['number']} requested: {format_inr(amount)}: {reason}",
            metadata={"amount_paise": amount, "payment_id": payment_id,
                      "invoice_id": invoice_id, "cap_paise": ceiling,
                      "scope": cap["scope"]},
        )

    view = _refund_view(row)
    view.update({
        "replayed": False,
        "refundable_ceiling_paise": ceiling,
        "scope": cap["scope"],
    })
    return view


def decide_refund(refund_id: int, *, approve: bool, decided_by: str,
                  decision_note: Optional[str] = None) -> dict:
    """Approve or reject. FOUR EYES: the approver may not be the requester.

    Money leaving a hospital on one person's say-so is the oldest fraud there is.
    A single-clerk site will need a documented override; that is a deliberate
    site decision and a flag, not a default (see the followups).
    """
    decided_by = _text(decided_by, "decided_by", required=True, max_len=120)
    decision_note = _text(decision_note, "decision_note")
    now = _utc_now_str()

    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM refunds WHERE id = ?", (int(refund_id),)).fetchone()
        if row is None:
            raise PaymentError(f"refund {refund_id} does not exist")
        refund = _row(row)
        if refund["status"] != "requested":
            raise PaymentError(
                f"refund {refund_id} is {refund['status']}; only a requested refund "
                "can be decided"
            )
        if (refund["requested_by"] or "").strip().lower() == decided_by.strip().lower():
            raise PaymentError(
                "a refund cannot be approved by the person who requested it. "
                "A second person must sign for money leaving the hospital"
            )
        status = "approved" if approve else "rejected"
        db.execute(
            "UPDATE refunds SET status = ?, decided_by = ?, decided_at = ?, "
            "decision_note = ? WHERE id = ?",
            (status, decided_by, now, decision_note, int(refund_id)),
        )
        updated = _row(db.execute(
            "SELECT * FROM refunds WHERE id = ?", (int(refund_id),)
        ).fetchone())
        _audit(
            db,
            action="refund_decided",
            outcome="success",
            actor=decided_by,
            patient_id=refund["patient_id"],
            target_type="refund",
            target_id=refund_id,
            summary=f"Refund {refund['refund_number']} {status} by {decided_by}",
            metadata={"status": status, "requested_by": refund["requested_by"],
                      "amount_paise": int(refund["amount_paise"]),
                      "decision_note": decision_note},
        )
    return _refund_view(updated)


def pay_refund(refund_id: int, *, method: str, paid_by: str,
               reference_no: Optional[str] = None,
               counter_session_id: Optional[int] = None) -> dict:
    """Hand the money back and write the money-OUT receipt.

    The cap is re-checked here against everything EXCEPT this refund — approval
    may have been days ago, and the patient may have been refunded elsewhere
    since.
    """
    method = _text(method, "method", required=True, max_len=32)
    if method not in REFUND_METHODS:
        raise PaymentError(f"{method!r} is not a refund method")
    paid_by = _text(paid_by, "paid_by", required=True, max_len=120)
    reference_no = _text(reference_no, "reference_no", max_len=_MAX_REF)
    if method in REFERENCE_REQUIRED_METHODS and not reference_no:
        raise PaymentError(f"a {method} refund needs a reference number")

    with db_session() as db:
        row = db.execute("SELECT * FROM refunds WHERE id = ?", (int(refund_id),)).fetchone()
        if row is None:
            raise PaymentError(f"refund {refund_id} does not exist")
        refund = _row(row)
        # Before the receipt number is drawn, as record_payment does: a closed
        # counter session, or an advance already spent, must not leave a gap
        # in the series. Both are checked again under the write lock below.
        if refund["status"] == "approved":
            if counter_session_id is not None:
                _require_open_session(db, counter_session_id)
            if refund["payment_id"] is None and refund["invoice_id"] is None:
                held = _deposit_balance(db, refund["patient_id"], refund["admission_id"])
                if int(refund["amount_paise"]) > held:
                    raise PaymentError(
                        f"advance balance is now {format_inr(held)}; cannot refund "
                        f"{format_inr(int(refund['amount_paise']))}"
                    )
    if refund["status"] != "approved":
        raise PaymentError(
            f"refund {refund_id} is {refund['status']}; only an approved refund can be paid"
        )

    doc = _next_number("RCPT", patient_id=refund["patient_id"],
                       admission_id=refund["admission_id"])
    now = _utc_now_str()
    amount = int(refund["amount_paise"])

    with db_session(immediate=True) as db:
        current = _row(db.execute(
            "SELECT * FROM refunds WHERE id = ?", (int(refund_id),)
        ).fetchone())
        if current["status"] != "approved":
            raise PaymentError(f"refund {refund_id} is {current['status']} and cannot be paid")
        if counter_session_id is not None:
            _require_open_session(db, counter_session_id)

        # Re-check the cap, excluding THIS refund from the "already claimed"
        # figure (it is the one being settled).
        cap = _refundable(db, patient_id=current["patient_id"],
                          payment_id=current["payment_id"],
                          invoice_id=current["invoice_id"])
        headroom = cap["refundable_paise"] + amount  # this refund is inside `already_refunded`
        if amount > headroom:  # pragma: no cover - defensive; the request already capped
            raise PaymentError("refund now exceeds what was paid; re-check before paying")

        is_deposit_refund = current["payment_id"] is None and current["invoice_id"] is None
        if is_deposit_refund:
            held = _deposit_balance(db, current["patient_id"], current["admission_id"])
            if amount > held:
                raise PaymentError(
                    f"advance balance is now {format_inr(held)}; cannot refund "
                    f"{format_inr(amount)}"
                )

        cur = db.execute(
            "INSERT INTO payments (receipt_number, patient_id, admission_id, invoice_id, "
            "direction, amount_paise, method, reference_no, purpose, status, "
            "counter_session_id, refund_id, received_by, received_at, notes, "
            + facility_identity.SNAPSHOT_COLUMNS_SQL + ") "
            "VALUES (?,?,?,?,'out',?,?,?,'refund','received',?,?,?,?,?,"
            + facility_identity.SNAPSHOT_PLACEHOLDERS + ")",
            (doc["number"], current["patient_id"], current["admission_id"],
             current["invoice_id"], amount, method, reference_no, counter_session_id,
             int(refund_id), paid_by, now,
             f"Refund {current['refund_number']}: {current['reason']}",
             *facility_identity.supplier_snapshot(
                 db, patient_id=current["patient_id"], admission_id=current["admission_id"])),
        )
        payment_id = int(cur.lastrowid)

        if is_deposit_refund:
            _insert_deposit(
                db,
                patient_id=current["patient_id"],
                admission_id=current["admission_id"],
                movement="refund",
                amount_paise=-amount,
                payment_id=payment_id,
                created_by=paid_by,
                notes=f"refund {current['refund_number']}",
            )

        db.execute(
            "UPDATE refunds SET status = 'paid', method = ?, reference_no = ?, "
            "paid_by = ?, paid_at = ? WHERE id = ?",
            (method, reference_no, paid_by, now, int(refund_id)),
        )
        updated = _row(db.execute(
            "SELECT * FROM refunds WHERE id = ?", (int(refund_id),)
        ).fetchone())
        payment_row = _row(db.execute(
            "SELECT * FROM payments WHERE id = ?", (payment_id,)
        ).fetchone())

        # Cash handed back with no session open is the same hole as cash taken
        # with none — money out of a drawer nobody opened. Marked identically so
        # one derivation (`is_unattributed_cash`) covers both directions and the
        # day-end nets them.
        orphan_cash = method in CASH_METHODS and counter_session_id is None
        _audit(
            db,
            action="refund_paid",
            outcome="partial_success" if orphan_cash else "success",
            actor=paid_by,
            patient_id=current["patient_id"],
            target_type="refund",
            target_id=refund_id,
            summary=(
                f"Refund {current['refund_number']} paid: {format_inr(amount)} by {method}"
                + (". NO COUNTER SESSION: this cash left no drawer"
                   if orphan_cash else "")
            ),
            metadata={"amount_paise": amount, "method": method,
                      "payment_id": payment_id, "receipt_number": doc["number"],
                      "unattributed_cash": orphan_cash},
        )

    view = _refund_view(updated)
    view["payment"] = _payment_view(payment_row)
    return view


def get_refund(refund_id: int) -> Optional[dict]:
    with db_session() as db:
        row = db.execute("SELECT * FROM refunds WHERE id = ?", (int(refund_id),)).fetchone()
    return _refund_view(_row(row)) if row else None


def list_refunds(*, patient_id: Optional[str] = None, status: Optional[str] = None,
                 limit: int = 100) -> list[dict]:
    clauses, args = [], []
    if patient_id:
        clauses.append("patient_id = ?")
        args.append(patient_id)
    if status:
        clauses.append("status = ?")
        args.append(status)
    sql = "SELECT * FROM refunds"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with db_session() as db:
        return [_refund_view(_row(r)) for r in db.execute(sql, tuple(args)).fetchall()]


# ── Discount approvals ───────────────────────────────────────────────────────

def request_discount(
    *,
    patient_id: str,
    reason: str,
    requested_by: str,
    invoice_id: Optional[int] = None,
    encounter_charge_id: Optional[int] = None,
    scope: str = "invoice",
    discount_type: str = "amount",
    requested_paise: Optional[int] = None,
    percent_bp: int = 0,
    base_paise: Optional[int] = None,
    policy_limit_paise: Optional[int] = None,
    reason_code: Optional[str] = None,
    admission_id: Optional[str] = None,
) -> dict:
    """Ask for a reduction. A discount is a PERSON'S DECISION, so this records
    who asked, why, and (on decision) who agreed.

    A percentage request is converted to paise HERE, once, through
    `money.apply_basis_points`, against a stated base — so the amount on the
    approval is the amount the invoice will move by, and a 7.5% discount and a
    7.5% co-pay can never disagree by a paise.

    APPLYING the approved amount to the invoice is the billing module's job. This
    module records the AUTHORITY; billing does the arithmetic on the bill.
    """
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    reason = _text(reason, "reason", required=True)
    requested_by = _text(requested_by, "requested_by", required=True, max_len=120)
    reason_code = _text(reason_code, "reason_code", max_len=64)
    admission_id = _text(admission_id, "admission_id", max_len=64)
    if scope not in ("invoice", "charge", "patient"):
        raise PaymentError(f"unknown discount scope {scope!r}")
    if discount_type not in ("amount", "percent"):
        raise PaymentError(f"unknown discount type {discount_type!r}")

    with db_session(immediate=True) as db:
        _require_registered_patient(db, patient_id, then="No discount was requested.")
        base = None
        if base_paise is not None:
            base = assert_non_negative_paise(base_paise, "base_paise")
        elif invoice_id is not None:
            base = int(_load_patient_invoice(db, int(invoice_id), patient_id)["payable_paise"] or 0)

        if discount_type == "percent":
            bp = int(percent_bp)
            if bp <= 0:
                raise PaymentError("a percent discount needs percent_bp above zero")
            if not base:
                raise PaymentError(
                    "a percent discount needs a base amount. Pass base_paise, or an "
                    "invoice_id whose payable is set. There is no percentage of an "
                    "unknown number."
                )
            amount = apply_basis_points(base, bp)
        else:
            bp = 0
            if requested_paise is None:
                raise PaymentError("requested_paise is required for an amount discount")
            amount = _positive_paise(requested_paise, "requested_paise")

        limit = None
        if policy_limit_paise is not None:
            limit = assert_non_negative_paise(policy_limit_paise, "policy_limit_paise")
        exceeded = 1 if (limit is not None and amount > limit) else 0

        now = _utc_now_str()
        cur = db.execute(
            "INSERT INTO discount_approvals (patient_id, admission_id, invoice_id, "
            "encounter_charge_id, scope, discount_type, percent_bp, requested_paise, "
            "policy_limit_paise, exceeded_limit, reason_code, reason, status, "
            "requested_by, requested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'requested',?,?)",
            (patient_id, admission_id, invoice_id, encounter_charge_id, scope,
             discount_type, bp, amount, limit, exceeded, reason_code, reason,
             requested_by, now),
        )
        discount_id = int(cur.lastrowid)
        row = _row(db.execute(
            "SELECT * FROM discount_approvals WHERE id = ?", (discount_id,)
        ).fetchone())
        _audit(
            db,
            action="discount_requested",
            outcome="success",
            actor=requested_by,
            patient_id=patient_id,
            target_type="discount_approval",
            target_id=discount_id,
            summary=f"Discount of {format_inr(amount)} requested: {reason}",
            metadata={"requested_paise": amount, "percent_bp": bp,
                      "invoice_id": invoice_id, "exceeded_limit": bool(exceeded),
                      "policy_limit_paise": limit},
        )
    return _discount_view(row)


def decide_discount(discount_id: int, *, approve: bool, decided_by: str,
                    approved_paise: Optional[int] = None,
                    decision_note: Optional[str] = None) -> dict:
    """Approve (possibly for less) or reject.

    THE APPROVER IS THE POINT OF THIS RECORD. Two rules, both hard:
      * `decided_by` is required — an anonymous discount is an unexplained hole
        in the day's takings;
      * the approver may NOT be the requester. A clerk who can both ask for and
        grant a reduction has a cash register with no lock on it.

    A policy limit is recorded and flagged when exceeded, never silently
    enforced: a senior person may legitimately go above it, and the record has to
    show that they did.
    """
    decided_by = _text(decided_by, "decided_by", required=True, max_len=120)
    decision_note = _text(decision_note, "decision_note")
    now = _utc_now_str()

    with db_session(immediate=True) as db:
        row = db.execute(
            "SELECT * FROM discount_approvals WHERE id = ?", (int(discount_id),)
        ).fetchone()
        if row is None:
            raise PaymentError(f"discount request {discount_id} does not exist")
        discount = _row(row)
        if discount["status"] != "requested":
            raise PaymentError(
                f"discount {discount_id} is {discount['status']}; only a requested "
                "discount can be decided"
            )
        if (discount["requested_by"] or "").strip().lower() == decided_by.strip().lower():
            raise PaymentError(
                "a discount cannot be approved by the person who requested it. "
                "A discount is a decision and it needs a second name on it"
            )

        requested = int(discount["requested_paise"] or 0)
        if approve:
            granted = requested if approved_paise is None else _positive_paise(
                approved_paise, "approved_paise"
            )
            if granted > requested:
                raise PaymentError(
                    f"cannot approve {format_inr(granted)} against a request for "
                    f"{format_inr(requested)}: raise a new request for the larger amount"
                )
            status = "approved"
        else:
            granted = 0
            status = "rejected"

        limit = discount["policy_limit_paise"]
        exceeded = 1 if (limit is not None and granted > int(limit)) else 0

        db.execute(
            "UPDATE discount_approvals SET status = ?, approved_paise = ?, "
            "exceeded_limit = ?, decided_by = ?, decided_at = ?, decision_note = ? "
            "WHERE id = ?",
            (status, granted, exceeded, decided_by, now, decision_note, int(discount_id)),
        )
        updated = _row(db.execute(
            "SELECT * FROM discount_approvals WHERE id = ?", (int(discount_id),)
        ).fetchone())
        _audit(
            db,
            action="discount_decided",
            outcome="success",
            actor=decided_by,
            patient_id=discount["patient_id"],
            target_type="discount_approval",
            target_id=discount_id,
            summary=(
                f"Discount {status} by {decided_by}: {format_inr(granted)} of "
                f"{format_inr(requested)} requested"
            ),
            metadata={"status": status, "approved_paise": granted,
                      "requested_paise": requested,
                      "requested_by": discount["requested_by"],
                      "exceeded_limit": bool(exceeded),
                      "decision_note": decision_note},
        )
    return _discount_view(updated)


def list_discounts(*, status: Optional[str] = None, invoice_id: Optional[int] = None,
                   patient_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    clauses, args = [], []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if invoice_id is not None:
        clauses.append("invoice_id = ?")
        args.append(int(invoice_id))
    if patient_id:
        clauses.append("patient_id = ?")
        args.append(patient_id)
    sql = "SELECT * FROM discount_approvals"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with db_session() as db:
        return [_discount_view(_row(r)) for r in db.execute(sql, tuple(args)).fetchall()]


# ── Cash counter sessions and the day-end count ──────────────────────────────

def open_counter_session(*, counter_code: str, opened_by: str,
                         shift_label: Optional[str] = None,
                         opening_float_paise: int = 0) -> dict:
    """Open a till. ONE OPEN SESSION PER COUNTER, enforced: two open sessions on
    the same drawer means the same cash is counted twice, or not at all."""
    counter_code = _text(counter_code, "counter_code", required=True, max_len=64)
    opened_by = _text(opened_by, "opened_by", required=True, max_len=120)
    shift_label = _text(shift_label, "shift_label", max_len=64)
    try:
        opening = assert_non_negative_paise(opening_float_paise, "opening_float_paise")
    except MoneyError as exc:
        raise PaymentError(str(exc)) from exc

    with db_session(immediate=True) as db:
        open_row = db.execute(
            "SELECT id FROM cash_counter_sessions WHERE counter_code = ? AND status = 'open'",
            (counter_code,),
        ).fetchone()
        if open_row is not None:
            raise PaymentError(
                f"counter {counter_code} already has an open session "
                f"(#{int(open_row['id'])}); close it before opening another"
            )
        cur = db.execute(
            "INSERT INTO cash_counter_sessions (counter_code, shift_label, opened_by, "
            "opened_at, opening_float_paise, status) VALUES (?,?,?,?,?,'open')",
            (counter_code, shift_label, opened_by, _utc_now_str(), opening),
        )
        session_id = int(cur.lastrowid)
        row = _row(db.execute(
            "SELECT * FROM cash_counter_sessions WHERE id = ?", (session_id,)
        ).fetchone())
        _audit(
            db,
            action="counter_session_opened",
            outcome="success",
            actor=opened_by,
            target_type="cash_counter_session",
            target_id=session_id,
            summary=f"Counter {counter_code} opened with float {format_inr(opening)}",
            metadata={"counter_code": counter_code, "opening_float_paise": opening,
                      "shift_label": shift_label},
        )
    return _session_view(row)


def _method_breakdown(db, where_sql: str, args: Sequence) -> tuple[list[dict], dict]:
    """Per-method money in/out, plus totals derived by SUMMING THOSE PARTS.

    The totals are built from the same rows the breakdown is built from, and then
    checked against an INDEPENDENT aggregate by the caller. This repo has been
    burned by a headline that did not equal its own breakdown; the check is the
    point, not the query.
    """
    rows = db.execute(
        "SELECT method, direction, COUNT(*) AS n, COALESCE(SUM(amount_paise), 0) AS s "
        f"FROM payments WHERE {where_sql} GROUP BY method, direction ORDER BY method",
        tuple(args),
    ).fetchall()

    by_method: dict[str, dict] = {}
    for r in rows:
        entry = by_method.setdefault(r["method"], {
            "method": r["method"],
            "in_paise": 0, "out_paise": 0, "net_paise": 0,
            "in_count": 0, "out_count": 0,
        })
        if r["direction"] == "in":
            entry["in_paise"] += int(r["s"])
            entry["in_count"] += int(r["n"])
        else:
            entry["out_paise"] += int(r["s"])
            entry["out_count"] += int(r["n"])
        entry["net_paise"] = entry["in_paise"] - entry["out_paise"]

    parts = sorted(by_method.values(), key=lambda e: e["method"])
    for entry in parts:
        entry["in_display"] = format_inr(entry["in_paise"])
        entry["out_display"] = format_inr(entry["out_paise"])
        entry["net_display"] = format_inr(entry["net_paise"])

    totals = {
        "in_paise": sum(e["in_paise"] for e in parts),
        "out_paise": sum(e["out_paise"] for e in parts),
        "in_count": sum(e["in_count"] for e in parts),
        "out_count": sum(e["out_count"] for e in parts),
    }
    totals["net_paise"] = totals["in_paise"] - totals["out_paise"]
    return parts, totals


def _assert_totals_reconcile(db, where_sql: str, args: Sequence, totals: dict,
                             context: str) -> None:
    """THE INVARIANT: total == sum(parts). Checked against a query the breakdown
    did not produce, so a bug in the grouping cannot hide inside its own sum."""
    row = db.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN direction = 'in' THEN amount_paise ELSE 0 END), 0) AS took, "
        "COALESCE(SUM(CASE WHEN direction = 'out' THEN amount_paise ELSE 0 END), 0) AS gave, "
        "COUNT(*) AS n "
        f"FROM payments WHERE {where_sql}",
        tuple(args),
    ).fetchone()
    took, gave = int(row["took"]), int(row["gave"])
    if took != totals["in_paise"] or gave != totals["out_paise"]:
        raise ReconciliationError(
            f"{context}: the breakdown does not equal the total, "
            f"parts say in={totals['in_paise']} out={totals['out_paise']}, "
            f"the ledger says in={took} out={gave}"
        )
    if int(row["n"]) != totals["in_count"] + totals["out_count"]:
        raise ReconciliationError(
            f"{context}: receipt counts disagree, parts say "
            f"{totals['in_count'] + totals['out_count']}, the ledger says {int(row['n'])}"
        )


def counter_session_expected(session_id: int) -> dict:
    """What the drawer SHOULD hold, and where every rupee of it came from.

    expected cash = opening float + cash in − cash out.

    A reversed receipt and the money-out row that reversed it are BOTH counted
    (LEDGER_PAYMENT_STATUSES), so a reversal shows up in the breakdown as the
    two movements it actually was and nets to zero. Counting only the live 'out'
    leg once made a ₹500 receipt, reversed, take ₹500 more out of the drawer.
    """
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM cash_counter_sessions WHERE id = ?", (int(session_id),)
        ).fetchone()
        if row is None:
            raise PaymentError(f"counter session {session_id} does not exist")
        session = _row(row)

        placeholders = ",".join("?" * len(LEDGER_PAYMENT_STATUSES))
        where = f"counter_session_id = ? AND status IN ({placeholders})"
        args = (int(session_id), *LEDGER_PAYMENT_STATUSES)
        parts, totals = _method_breakdown(db, where, args)
        _assert_totals_reconcile(db, where, args, totals, f"counter session {session_id}")

        cash = next((p for p in parts if p["method"] == "cash"), None)
        cash_in = cash["in_paise"] if cash else 0
        cash_out = cash["out_paise"] if cash else 0
        opening = int(session["opening_float_paise"] or 0)
        expected = opening + cash_in - cash_out

    return {
        "session": _session_view(session),
        "opening_float_paise": opening,
        "opening_float_display": format_inr(opening),
        "cash_in_paise": cash_in,
        "cash_in_display": format_inr(cash_in),
        "cash_out_paise": cash_out,
        "cash_out_display": format_inr(cash_out),
        "expected_cash_paise": expected,
        "expected_cash_display": format_inr(expected),
        "by_method": parts,
        "totals": {
            **totals,
            "in_display": format_inr(totals["in_paise"]),
            "out_display": format_inr(totals["out_paise"]),
            "net_display": format_inr(totals["net_paise"]),
        },
    }


def close_counter_session(session_id: int, *, declared_cash_paise: int,
                          closed_by: str, notes: Optional[str] = None) -> dict:
    """Count the drawer and close the shift.

    THE VARIANCE IS STORED, NEVER ABSORBED (rule 4). A session that does not
    balance closes as 'disputed', not 'closed', so the exception is a state
    somebody has to clear rather than a number nobody reads. `system_cash_paise`
    is snapshotted onto the row at close so the closing figure cannot drift later
    if a payment is reversed.
    """
    closed_by = _text(closed_by, "closed_by", required=True, max_len=120)
    notes = _text(notes, "notes")
    try:
        declared = assert_non_negative_paise(declared_cash_paise, "declared_cash_paise")
    except MoneyError as exc:
        raise PaymentError(str(exc)) from exc

    expected_report = counter_session_expected(int(session_id))
    if expected_report["session"]["status"] != "open":
        raise PaymentError(
            f"counter session {session_id} is "
            f"{expected_report['session']['status']}; it is already closed"
        )
    expected = expected_report["expected_cash_paise"]
    variance = declared - expected
    now = _utc_now_str()

    with db_session(immediate=True) as db:
        current = _row(db.execute(
            "SELECT * FROM cash_counter_sessions WHERE id = ?", (int(session_id),)
        ).fetchone())
        if current["status"] != "open":
            raise PaymentError(f"counter session {session_id} is already {current['status']}")
        status = "closed" if variance == 0 else "disputed"
        db.execute(
            "UPDATE cash_counter_sessions SET closed_by = ?, closed_at = ?, "
            "declared_cash_paise = ?, system_cash_paise = ?, variance_paise = ?, "
            "status = ?, notes = ? WHERE id = ?",
            (closed_by, now, declared, expected, variance, status, notes, int(session_id)),
        )
        row = _row(db.execute(
            "SELECT * FROM cash_counter_sessions WHERE id = ?", (int(session_id),)
        ).fetchone())
        _audit(
            db,
            action="counter_session_closed",
            outcome="success" if variance == 0 else "partial_success",
            actor=closed_by,
            target_type="cash_counter_session",
            target_id=session_id,
            summary=(
                f"Counter {current['counter_code']} closed, expected "
                f"{format_inr(expected)}, counted {format_inr(declared)}, variance "
                f"{format_inr(variance)}"
            ),
            metadata={"expected_cash_paise": expected, "declared_cash_paise": declared,
                      "variance_paise": variance, "status": status,
                      "counter_code": current["counter_code"]},
        )

    result = _session_view(row)
    result.update({
        "expected_cash_paise": expected,
        "expected_cash_display": format_inr(expected),
        "by_method": expected_report["by_method"],
        "totals": expected_report["totals"],
        "balanced": variance == 0,
    })
    return result


def reconcile_counter_session(session_id: int, *, reconciled_by: str,
                              notes: Optional[str] = None) -> dict:
    """Sign off a closed (or disputed) session. The variance stays on the row —
    reconciling is somebody ACCEPTING the difference, not erasing it."""
    reconciled_by = _text(reconciled_by, "reconciled_by", required=True, max_len=120)
    notes = _text(notes, "notes")
    now = _utc_now_str()
    with db_session(immediate=True) as db:
        row = db.execute(
            "SELECT * FROM cash_counter_sessions WHERE id = ?", (int(session_id),)
        ).fetchone()
        if row is None:
            raise PaymentError(f"counter session {session_id} does not exist")
        session = _row(row)
        if session["status"] == "open":
            raise PaymentError("close the session before reconciling it")
        if session["status"] == "reconciled":
            raise PaymentError(f"counter session {session_id} is already reconciled")
        if (session["closed_by"] or "").strip().lower() == reconciled_by.strip().lower():
            raise PaymentError(
                "a shift cannot be signed off by the same person who counted it. "
                "A variance needs a second pair of eyes"
            )
        db.execute(
            "UPDATE cash_counter_sessions SET status = 'reconciled', reconciled_by = ?, "
            "reconciled_at = ?, notes = COALESCE(?, notes) WHERE id = ?",
            (reconciled_by, now, notes, int(session_id)),
        )
        updated = _row(db.execute(
            "SELECT * FROM cash_counter_sessions WHERE id = ?", (int(session_id),)
        ).fetchone())
        _audit(
            db,
            action="counter_session_reconciled",
            outcome="success",
            actor=reconciled_by,
            target_type="cash_counter_session",
            target_id=session_id,
            summary=(
                f"Counter {session['counter_code']} reconciled by {reconciled_by}; "
                f"variance {format_inr(int(session['variance_paise'] or 0))} accepted"
            ),
            metadata={"variance_paise": session["variance_paise"],
                      "closed_by": session["closed_by"]},
        )
    return _session_view(updated)


def get_counter_session(session_id: int) -> Optional[dict]:
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM cash_counter_sessions WHERE id = ?", (int(session_id),)
        ).fetchone()
    return _session_view(_row(row)) if row else None


def list_counter_sessions(*, counter_code: Optional[str] = None,
                          status: Optional[str] = None, limit: int = 50) -> list[dict]:
    clauses, args = [], []
    if counter_code:
        clauses.append("counter_code = ?")
        args.append(counter_code)
    if status:
        clauses.append("status = ?")
        args.append(status)
    sql = "SELECT * FROM cash_counter_sessions"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with db_session() as db:
        return [_session_view(_row(r)) for r in db.execute(sql, tuple(args)).fetchall()]


def day_end_summary(*, day: Optional[date | str] = None,
                    counter_code: Optional[str] = None) -> dict:
    """The day's takings, by method, with the totals proved against the ledger.

    THE DAY IS A HOSPITAL-LOCAL DAY, converted to a UTC WINDOW. Stored timestamps
    are UTC (SQLite's CURRENT_TIMESTAMP), and in IST a local day starts at
    18:30 UTC the previous date — so `date(received_at) = '2026-08-12'` would
    silently put the first five and a half hours of a shift on the wrong day's
    cash sheet, every single day. `local_day_bounds_utc` is the repo's answer to
    exactly this and is used here for that reason.

    UNATTRIBUTED CASH — cash taken with no counter session (`record_payment`
    explains why it is recorded rather than refused). It is in this day's
    takings and in NO drawer's expected figure, so:

      * it is reported on its own (`unattributed_cash_*`), never folded into a
        session's numbers, and
      * while any exists, THE WHOLE-DAY SHEET REFUSES TO CALL ITSELF FINAL and
        reports `counted_cash_paise` / `variance_paise` as unset. A day-end
        variance is "counted minus expected across the drawers"; with money in
        no drawer that subtraction is missing a term, and printing it anyway is
        the arithmetic lie this module exists to prevent. `finality_blockers`
        says which sentence applies, in words.

    A COUNTER-SCOPED report is about ONE drawer, and drawer-less cash belongs to
    none — the counter filter already excludes it from that report's takings. So
    the figure is shown there (a supervisor should see the hospital has
    unattributed cash today) but does NOT gate that counter's finality: another
    counter's missing till is not this till's exception to clear.

    THE DRAWERS ON THE SHEET are every drawer the day touched, not only the ones
    opened inside it: opened in the window, still open and opened before the
    window ended, or holding a receipt taken in the window. Selecting by
    `opened_at` alone left a till opened yesterday and never closed off today's
    sheet, which then read final with "Counted ₹0.00" over cash that drawer had
    taken. Any open drawer holds the sheet open, and one opened before the day
    began is named in `finality_blockers`. A shift that runs past midnight
    appears on both days' sheets, because both days' money went into it.

    A reversal counts as both of its legs (LEDGER_PAYMENT_STATUSES), on the day
    each leg was written, so an earlier day's sheet does not change when one of
    its receipts is reversed later.
    """
    if day is None:
        local_day = hospital_today()
    elif isinstance(day, str):
        try:
            local_day = date.fromisoformat(day[:10])
        except ValueError as exc:
            raise PaymentError("day must be YYYY-MM-DD") from exc
    else:
        local_day = day
    start, end = local_day_bounds_utc(local_day)
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    end_s = end.strftime("%Y-%m-%d %H:%M:%S")

    placeholders = ",".join("?" * len(LEDGER_PAYMENT_STATUSES))
    where = (
        f"datetime(received_at) >= ? AND datetime(received_at) < ? "
        f"AND status IN ({placeholders})"
    )
    args: list = [start_s, end_s, *LEDGER_PAYMENT_STATUSES]
    if counter_code:
        where += (
            " AND counter_session_id IN (SELECT id FROM cash_counter_sessions "
            "WHERE counter_code = ?)"
        )
        args.append(counter_code)

    with db_session() as db:
        parts, totals = _method_breakdown(db, where, args)
        _assert_totals_reconcile(db, where, args, totals, f"day-end {local_day.isoformat()}")

        # Every drawer the day touched (see the docstring). Reversal rows carry
        # the ORIGINAL receipt's session, so they are left out of the "holding a
        # receipt" test: reversing an old receipt today must not pull a shift
        # closed weeks ago, and its count, onto today's sheet.
        session_rows = [_row(r) for r in db.execute(
            "SELECT *, datetime(opened_at) < ? AS opened_before_day "
            "FROM cash_counter_sessions WHERE ("
            "(datetime(opened_at) >= ? AND datetime(opened_at) < ?) "
            "OR (status = 'open' AND datetime(opened_at) < ?) "
            "OR id IN (SELECT counter_session_id FROM payments "
            "WHERE counter_session_id IS NOT NULL "
            "AND datetime(received_at) >= ? AND datetime(received_at) < ? "
            f"AND {_NOT_A_REVERSAL})) "
            + ("AND counter_code = ? " if counter_code else "")
            + "ORDER BY id DESC",
            tuple([start_s, start_s, end_s, end_s, start_s, end_s]
                  + ([counter_code] if counter_code else [])),
        ).fetchall()]
        sessions = [_session_view(r) for r in session_rows]
        opened_earlier = [
            r for r in session_rows if r["status"] == "open" and r["opened_before_day"]
        ]

        # Cash the day took that no drawer expected. Never counter-filtered: it
        # has no counter, and the filter above would silently drop it.
        cash_placeholders = ",".join("?" * len(CASH_METHODS))
        orphan = db.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN direction = 'in' THEN amount_paise ELSE 0 END), 0) AS took, "
            "COALESCE(SUM(CASE WHEN direction = 'out' THEN amount_paise ELSE 0 END), 0) AS gave, "
            "COUNT(*) AS n FROM payments "
            "WHERE datetime(received_at) >= ? AND datetime(received_at) < ? "
            f"AND status IN ({placeholders}) AND counter_session_id IS NULL "
            f"AND method IN ({cash_placeholders})",
            (start_s, end_s, *LEDGER_PAYMENT_STATUSES, *sorted(CASH_METHODS)),
        ).fetchone()

        refunds_open = int(db.execute(
            "SELECT COUNT(*) AS c FROM refunds WHERE status IN ('requested','approved')"
        ).fetchone()["c"])
        discounts_open = int(db.execute(
            "SELECT COUNT(*) AS c FROM discount_approvals WHERE status = 'requested'"
        ).fetchone()["c"])
        series_configured = _receipt_series_is_configured(db)

    cash = next((p for p in parts if p["method"] == "cash"), None)
    counted = sum(
        int(s["declared_cash_paise"]) for s in sessions
        if s["declared_cash_paise"] is not None
    )
    variance_total = sum(
        int(s["variance_paise"]) for s in sessions if s["variance_paise"] is not None
    )
    open_sessions = [s for s in sessions if s["status"] == "open"]

    unattributed_in = int(orphan["took"] or 0)
    unattributed_out = int(orphan["gave"] or 0)
    unattributed_count = int(orphan["n"] or 0)
    # Whole-day sheet only — see the docstring on why a counter-scoped report is
    # not held open by cash that belongs to no counter.
    unattributed_blocks = counter_code is None and unattributed_count > 0
    uncountable = bool(open_sessions) or unattributed_blocks

    finality_blockers: list[str] = []
    if open_sessions:
        finality_blockers.append(
            f"{len(open_sessions)} counter session(s) are still open and uncounted"
        )
    # Named, because nobody at today's counter remembers opening it.
    for stale in opened_earlier:
        finality_blockers.append(
            f"Counter {stale['counter_code']} has been open since "
            f"{hospital_stamp(stale['opened_at']) or 'an earlier day'} "
            f"(opened by {stale['opened_by'] or 'unknown'}), before this day began. "
            "Count it and close it"
        )
    if unattributed_blocks:
        finality_blockers.append(
            f"{unattributed_count} cash receipt(s) totalling "
            f"{format_inr(unattributed_in - unattributed_out)} were taken with no "
            "counter session. That money is in this day's takings and in no "
            "drawer, so the day's variance cannot be attributed"
        )
    # "Not counted" is true of an open drawer; unattributed cash is a different
    # fact and gets its own word, because the fix is a different action.
    unset = "Not counted" if open_sessions or not unattributed_blocks \
        else "Not attributable"

    return {
        "day": local_day.isoformat(),
        "window_utc": {"from": start_s, "to": end_s},
        "counter_code": counter_code,
        "by_method": parts,
        "totals": {
            **totals,
            "in_display": format_inr(totals["in_paise"]),
            "out_display": format_inr(totals["out_paise"]),
            "net_display": format_inr(totals["net_paise"]),
        },
        "cash_in_paise": cash["in_paise"] if cash else 0,
        "cash_out_paise": cash["out_paise"] if cash else 0,
        "cash_net_paise": cash["net_paise"] if cash else 0,
        "cash_net_display": format_inr(cash["net_paise"] if cash else 0),
        "sessions": sessions,
        # A day with an open session — or with cash that entered no drawer — is
        # NOT counted. Rendering the counted figure as if it were final would be
        # a fabricated day-end.
        "counted_cash_paise": None if uncountable else counted,
        "counted_cash_display": format_inr_or_unset(
            None if uncountable else counted, unset=unset
        ),
        "variance_paise": None if uncountable else variance_total,
        "variance_display": format_inr_or_unset(
            None if uncountable else variance_total, unset=unset
        ),
        "unattributed_cash_in_paise": unattributed_in,
        "unattributed_cash_out_paise": unattributed_out,
        "unattributed_cash_net_paise": unattributed_in - unattributed_out,
        "unattributed_cash_display": format_inr(unattributed_in - unattributed_out),
        "unattributed_cash_count": unattributed_count,
        "unattributed_cash_notice": (
            UNATTRIBUTED_CASH_NOTICE if unattributed_count else None
        ),
        "open_session_count": len(open_sessions),
        "finality_blockers": finality_blockers,
        "is_final": not uncountable,
        "open_refund_count": refunds_open,
        "open_discount_count": discounts_open,
        "receipt_series_is_configured": series_configured,
    }


def patient_financial_snapshot(patient_id: str, *, admission_id: Optional[str] = None) -> dict:
    """What this patient has paid, what is held on account, what is in flight.

    ADVISORY ONLY. Nothing here may gate a clinical action; a discharge that
    consults this must warn at most (config.DISCHARGE_FINANCIAL_ENFORCE defaults
    OFF and must stay OFF for the pilot).
    """
    patient_id = _text(patient_id, "patient_id", required=True, max_len=64)
    placeholders = ",".join("?" * len(LIVE_PAYMENT_STATUSES))
    with db_session() as db:
        # Neither leg of a reversal (see _NOT_A_REVERSAL): a reversed receipt
        # was neither paid nor refunded, and its 'out' row alone read as a refund.
        row = db.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN direction='in' THEN amount_paise ELSE 0 END),0) AS took, "
            "COALESCE(SUM(CASE WHEN direction='out' THEN amount_paise ELSE 0 END),0) AS gave, "
            "COUNT(*) AS n "
            f"FROM payments WHERE patient_id = ? AND status IN ({placeholders}) "
            f"AND {_NOT_A_REVERSAL}",
            (patient_id, *LIVE_PAYMENT_STATUSES),
        ).fetchone()
        deposit = _deposit_balance(db, patient_id, admission_id)
        open_refunds = int(db.execute(
            "SELECT COUNT(*) AS c FROM refunds WHERE patient_id = ? "
            "AND status IN ('requested','approved')", (patient_id,),
        ).fetchone()["c"])
        open_discounts = int(db.execute(
            "SELECT COUNT(*) AS c FROM discount_approvals WHERE patient_id = ? "
            "AND status = 'requested'", (patient_id,),
        ).fetchone()["c"])
    paid = int(row["took"])
    refunded = int(row["gave"])
    return {
        "patient_id": patient_id,
        "admission_id": admission_id,
        "receipt_count": int(row["n"]),
        "paid_paise": paid,
        "paid_display": format_inr(paid),
        "refunded_paise": refunded,
        "refunded_display": format_inr(refunded),
        "net_paise": paid - refunded,
        "net_display": format_inr(paid - refunded),
        "deposit_balance_paise": deposit,
        "deposit_balance_display": format_inr(deposit),
        "open_refund_count": open_refunds,
        "open_discount_count": open_discounts,
        "advisory_only": True,
    }
