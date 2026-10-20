"""Orb — patient billing: charge capture, invoices, and the discharge advisory.

This is the module that turns things that happened to a patient into a bill, and
it is built on top of two files it never duplicates:

  * ``services/money.py``          — every paise of arithmetic, one rounding rule.
  * ``services/billing_masters.py`` — what an item costs, who is paying, what
                                      number a document gets.

Nothing here invents a rate, a rounding rule or a tax split. If a number in this
file was not added, subtracted or compared as an integer of paise, it is a bug.

WHAT THIS MODULE PROMISES
─────────────────────────

1. A CHARGE IS NEVER ₹0 BY ACCIDENT. Every posting path resolves a rate through
   ``billing_masters.resolve_rate`` and REFUSES when the resolution says
   ``configured=False``. An unpriced item raises ``BillingError`` with code
   ``rate_not_configured``; the automatic capture path reports it as a SKIPPED
   event with a reason. Neither ever writes a zero-rupee line and calls it a
   charge. "Not configured" reaches the screen as words.

   THE SAME REFUSAL APPLIES ONE STEP LATER. ``create_invoice`` will not raise a
   bill over a charge that carries ``rate_is_configured = 0`` — the row the
   frontdesk writes when a consultation happened but nobody has priced it
   (``charge_not_priced``). Invoicing it would print a numbered GST line reading
   ₹0.00 AND move the charge out of ``pending``, which is what makes the
   "this bill is incomplete" advisory disappear.

2. AN ISSUED INVOICE IS A FINANCIAL RECORD. It is never deleted and its number is
   never reused. The only ways out of ``issued`` are payment (derived, not
   asserted), cancellation WITH A TYPED REASON while nothing has been paid
   against it, or — once money has moved — a CREDIT NOTE, which is a second
   document that reverses the first rather than an erasure of it. Every one of
   those transitions writes an audit row on the tamper-evident chain.

3. GST IS AN ITEM PROPERTY, NOT A DEFAULT. Healthcare services supplied by a
   clinical establishment are exempt in India; pharmacy, consumables and
   implants are not. The treatment travels from the service master onto the
   charge and onto the invoice line, and ``money.compute_gst`` returns zeros for
   an exempt line whatever rate is sitting on the item.

4. MONEY NEVER BLOCKS CARE. ``discharge_financial_advisory`` computes what the
   money side knows at discharge and records it. It WARNS. It does not stop a
   discharge unless ``ORB_DISCHARGE_FINANCIAL_ENFORCE`` is explicitly on — a flag
   that defaults OFF and must stay off for the pilot. See
   ``assert_discharge_permitted`` for the whole argument; the short version is
   that a patient who is medically ready to leave must never be held on a ward by
   a software rule about an unpaid bill.

CONVENTIONS (shared with the rest of server/services)
  * Every public function is PURE SYNC and opens its own ``db_session``. Async
    callers go through ``database.run_db``; blocking DB calls inside ``async def``
    are banned and tests/test_event_loop_discipline.py enforces it. The routes in
    routes/billing_routes.py are plain ``def``, which FastAPI threadpools.
  * No foreign keys exist on the migration-20 tables, deliberately (a money row
    must never be able to make a clinical write or a clinical schema change
    fail). Referential integrity is this file's job: it checks that a patient
    exists before charging them, and a dangling id degrades to "unknown", never
    to an exception on a clinical path.
  * ``config.DISCHARGE_FINANCIAL_ENFORCE`` is read through the MODULE
    (``config.X``), never imported by value, so a deployment or a test that
    changes it changes what this code does.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import config
from database import db_session

from services import billing_masters, facility_identity
from services.audit_service import log_audit_event
from services.hospital_time import hospital_now, hospital_timezone, hospital_today
from services.money import (
    GST_EXEMPT,
    GST_TREATMENTS,
    QUANTITY_SCALE,
    LineTotals,
    MoneyError,
    apply_basis_points,
    assert_paise,
    compute_invoice,
    compute_line,
    format_inr,
    format_inr_or_unset,
)

logger = logging.getLogger("orb.billing")

__all__ = [
    "BillingError",
    "DischargeFinanciallyBlocked",
    # masters this module owns (billing_masters owns the rest)
    "upsert_tariff_plan",
    "list_tariff_rates",
    "list_package_rates",
    "upsert_package_rate",
    # charge capture
    "add_charge",
    "list_charges",
    "list_op_patients",
    "cancel_charge",
    "capture_clinical_charges",
    "post_bed_day_charges",
    # invoices
    "create_invoice",
    "issue_invoice",
    "cancel_invoice",
    "create_credit_note",
    "get_invoice",
    "list_invoices",
    "refresh_invoice_payment_state",
    "invoice_print_view",
    # money views
    "patient_financial_summary",
    "discharge_financial_advisory",
    "assert_discharge_permitted",
    "amount_in_words",
]


class BillingError(ValueError):
    """A billing operation the caller may not perform.

    Carries a machine-readable ``code`` so the HTTP layer can map it (422 for a
    bad request, 409 for an illegal state transition) and so the UI can react to
    ``rate_not_configured`` — the one error a clerk fixes rather than reports.
    """

    def __init__(self, message: str, *, code: str = "billing_error", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


class DischargeFinanciallyBlocked(RuntimeError):
    """Raised ONLY when ORB_DISCHARGE_FINANCIAL_ENFORCE is explicitly enabled.

    With the flag at its default (off) this exception is unreachable — see
    ``assert_discharge_permitted``. It exists so a POST-pilot site that has moved
    its billing into Orb can turn on a soft stop deliberately, rather than
    somebody adding an un-flagged ``if`` to the discharge path later.
    """

    def __init__(self, advisory: dict):
        super().__init__(
            "Discharge financial clearance is ENFORCED at this deployment and this "
            f"admission has an outstanding balance of {advisory.get('outstanding_display')}."
        )
        self.advisory = advisory


# ── Status vocabularies ──────────────────────────────────────────────────────

#: Charge lifecycle used by THIS module. The schema also allows 'written_off'
#: and 'packaged'; neither is produced here (write-off belongs to the payments
#: module, package allocation to the payer module) and both are left alone if
#: another module sets them — this file only ever moves pending↔billed and
#: pending→cancelled.
_CHARGE_BILLABLE = "pending"
_CHARGE_BILLED = "billed"
_CHARGE_CANCELLED = "cancelled"

#: Invoice transitions this module performs. `paid` / `partially_paid` are
#: DERIVED from what has been allocated against the invoice, never asserted by a
#: caller — an invoice that says "paid" because somebody clicked "paid" is how a
#: hospital loses money it was actually owed.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"issued", "cancelled"}),
    "issued": frozenset({"partially_paid", "paid", "cancelled"}),
    "partially_paid": frozenset({"paid", "issued"}),
    "paid": frozenset({"partially_paid", "issued"}),  # a reversed payment walks back
    "cancelled": frozenset(),  # terminal. Use a credit note.
    "void": frozenset(),
    "written_off": frozenset(),
}

#: Order types Orb already records that a hospital bills for. `medication` is
#: deliberately ABSENT: a medication order is not a dispense, and billing the
#: order as well as the pharmacy dispense charges the patient twice for one drug.
_BILLABLE_ORDER_TYPES = ("lab", "imaging", "procedure")

#: Order statuses that mean the work was actually done. A cancelled or still
#: active order is not a charge.
_COMPLETED_ORDER_STATUSES = ("completed", "resulted")

#: Helix pharmacy states that mean the drug physically left the pharmacy.
_DISPENSED_MED_STATUSES = ("dispensed", "delivered")


# ── Small helpers ────────────────────────────────────────────────────────────


def _row(row) -> dict:
    return {k: row[k] for k in row.keys()}


def _count(n: int, singular: str, plural: str | None = None) -> str:
    """ "1 captured charge", "3 captured charges": a real plural, never "(s)"."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _table_exists(db, name: str) -> bool:
    """Migration-20 tables always exist; Helix's do not on every deployment.

    A missing optional table must degrade to "no chargeable events found", never
    to an exception that takes out a billing screen.
    """
    return (
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def _clean(text: str | None) -> str:
    return (text or "").strip()


def _require_reason(reason: str | None, *, what: str) -> str:
    """A cancellation with no reason is an erasure with a UI on it."""
    cleaned = _clean(reason)
    if len(cleaned) < 3:
        raise BillingError(
            f"{what} requires a typed reason (at least 3 characters), an issued "
            "financial document is never removed silently",
            code="reason_required",
        )
    return cleaned[:500]


def _iso_day(value: str | date | datetime | None) -> str:
    """The hospital-local day a charge belongs to.

    The OPD lane passes the consultation's full UTC timestamp. Slicing its first
    ten characters dated every visit between 00:00 and 05:30 IST to the previous
    day, on the bill and in the tariff lookup. A bare date passes through.
    """
    if value is None:
        return hospital_today().isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(hospital_timezone()).date().isoformat()
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _local_day_of_stored(value) or hospital_today().isoformat()


def _now_iso() -> str:
    return hospital_now().isoformat(timespec="seconds")


def _local_day_of_stored(value: Any) -> str | None:
    """The HOSPITAL-LOCAL calendar day of a timestamp Orb stored (which is UTC).

    ``str(created_at)[:10]`` is wrong and it is wrong in the direction that costs
    money. Orb persists every timestamp in UTC (services/hospital_time.py), and a
    hospital in UTC+05:30 does most of an evening's work after 18:30 UTC — so
    slicing the UTC date off a stored timestamp puts every evening event on
    YESTERDAY. That would price a charge against a tariff that was not yet in
    force, date a line wrongly on a GST invoice, and split a bed-day count.
    Caught by the capture tests on the first run: a lab order completed at 22:56
    local resolved to "no price", because the price started "tomorrow".

    A value that is already a bare date (10 characters) is returned as-is: it was
    never an instant, so converting it would shift it by a day for free.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= 10:
        return text[:10]
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return text[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(hospital_timezone()).date().isoformat()


def _patient_exists(db, patient_id: str) -> bool:
    return db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is not None


def _audit(
    db,
    *,
    action_type: str,
    summary: str,
    patient_id: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    actor: str | None = None,
    metadata: dict | None = None,
    outcome: str = "success",
) -> None:
    """One audit call for the whole module, written in the CALLER'S transaction.

    Category ``billing``: not in audit_service's fail-closed set, because a
    failed audit write must not roll back a bill a clerk is standing at a counter
    raising. Passing ``db=`` means the audit row commits or rolls back WITH the
    financial row it describes, so a cancelled invoice can never exist without
    its cancellation record.
    """
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="billing",
        action_type=action_type,
        outcome=outcome,
        db=db,
        summary_message=summary,
        actor_user_id=actor,
        patient_id=patient_id,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        metadata=metadata,
    )


# ── Masters this module owns (the rest live in billing_masters) ──────────────


def create_service_item(**fields) -> dict:
    """The price list's "New item": refuses a code already on the list.

    billing_masters.upsert_service_item is keyed on the code, so "New item" on an
    existing code overwrote it, and a blank price un-priced a code the desk had
    been billing. A create now names what holds the code and changes nothing;
    changing an existing item stays with the editing paths.
    """
    code = (fields.get("code") or "").strip()
    existing = billing_masters.get_service_item(code) if code else None
    if existing:
        raise BillingError(
            f"{code} already exists as {existing['name']}. Nothing was changed. Use "
            "a different code, or set this item's rate from the price list.",
            code="already_exists",
        )
    return billing_masters.upsert_service_item(**fields)


def upsert_tariff_plan(
    *,
    code: str,
    name: str,
    plan_type: str = "self_pay",
    is_default: bool = False,
    is_active: bool = True,
    effective_from: str | None = None,
    effective_to: str | None = None,
    notes: str | None = None,
) -> dict:
    """Create or edit a price list.

    Exactly one plan may be the default (the plan a self-pay patient is priced
    on), so setting one clears the others in the same transaction — two defaults
    would make ``resolve_rate`` depend on insertion order, which is how the same
    service quietly costs two different amounts.
    """
    code = _clean(code)
    if not code:
        raise BillingError("tariff plan code must not be blank", code="invalid_input")
    if not _clean(name):
        raise BillingError("tariff plan name must not be blank", code="invalid_input")
    with db_session() as db:
        db.execute(
            """INSERT INTO tariff_plans
                 (code, name, plan_type, effective_from, effective_to, is_default,
                  is_active, notes, updated_at)
               VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, plan_type=excluded.plan_type,
                 effective_from=excluded.effective_from,
                 effective_to=excluded.effective_to,
                 is_default=excluded.is_default, is_active=excluded.is_active,
                 notes=excluded.notes, updated_at=CURRENT_TIMESTAMP""",
            (
                code,
                _clean(name),
                plan_type,
                effective_from,
                effective_to,
                1 if is_default else 0,
                1 if is_active else 0,
                notes,
            ),
        )
        if is_default:
            db.execute("UPDATE tariff_plans SET is_default = 0 WHERE code <> ?", (code,))
        row = db.execute("SELECT * FROM tariff_plans WHERE code = ?", (code,)).fetchone()
    return _row(row)


def list_tariff_rates(
    *,
    tariff_plan_id: int | None = None,
    service_code: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """The priced rows of a plan, newest effective date first, with display strings."""
    sql = (
        "SELECT r.*, s.name AS service_name, s.category AS service_category "
        "FROM tariff_rates r LEFT JOIN service_items s ON s.code = r.service_code "
        "WHERE r.is_active = 1"
    )
    params: list[Any] = []
    if tariff_plan_id is not None:
        sql += " AND r.tariff_plan_id = ?"
        params.append(int(tariff_plan_id))
    if service_code:
        sql += " AND r.service_code = ?"
        params.append(service_code)
    sql += " ORDER BY r.service_code, r.effective_from DESC, r.id DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]
    for r in rows:
        r["rate_display"] = format_inr(int(r["rate_paise"]))
    return rows


def list_package_rates(*, tariff_plan_id: int | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM package_rates WHERE is_active = 1"
    params: list[Any] = []
    if tariff_plan_id is not None:
        sql += " AND tariff_plan_id = ?"
        params.append(int(tariff_plan_id))
    sql += " ORDER BY package_code, effective_from DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]
    for r in rows:
        r["package_rate_display"] = format_inr(int(r["package_rate_paise"]))
    return rows


def upsert_package_rate(
    *,
    tariff_plan_id: int,
    package_code: str,
    name: str,
    package_rate_paise: int,
    ward_class: str | None = None,
    los_days_included: int | None = None,
    inclusions: str | None = None,
    exclusions: str | None = None,
    external_scheme_code: str | None = None,
    effective_from: str | None = None,
) -> dict:
    """Price a bundled package (a surgery package, a PM-JAY benefit package).

    ``package_rate_paise`` is REQUIRED and passes ``assert_paise`` — there is no
    "unpriced package", because a package with no price is simply not a package
    the hospital offers. That is the opposite rule to a service item, and it is
    deliberate: an unpriced service still gets billed at its own rate, whereas an
    unpriced package would silently bundle a whole admission into nothing.
    """
    code = _clean(package_code)
    if not code:
        raise BillingError("package code must not be blank", code="invalid_input")
    rate = assert_paise(package_rate_paise)
    if rate < 0:
        raise BillingError("a package rate must not be negative", code="invalid_input")
    eff = effective_from or hospital_today().isoformat()
    with db_session() as db:
        db.execute(
            """INSERT INTO package_rates
                 (tariff_plan_id, package_code, name, ward_class, package_rate_paise,
                  los_days_included, inclusions_json, exclusions_json,
                  external_scheme_code, effective_from, is_active)
               VALUES (?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(tariff_plan_id, package_code, IFNULL(ward_class,''), effective_from)
               DO UPDATE SET name=excluded.name,
                             package_rate_paise=excluded.package_rate_paise,
                             los_days_included=excluded.los_days_included,
                             inclusions_json=excluded.inclusions_json,
                             exclusions_json=excluded.exclusions_json,
                             external_scheme_code=excluded.external_scheme_code,
                             is_active=1""",
            (
                int(tariff_plan_id),
                code,
                _clean(name),
                ward_class,
                rate,
                los_days_included,
                inclusions,
                exclusions,
                external_scheme_code,
                eff,
            ),
        )
        row = db.execute(
            "SELECT * FROM package_rates WHERE tariff_plan_id = ? AND package_code = ? "
            "AND IFNULL(ward_class,'') = IFNULL(?,'') AND effective_from = ?",
            (int(tariff_plan_id), code, ward_class, eff),
        ).fetchone()
    out = _row(row)
    out["package_rate_display"] = format_inr(int(out["package_rate_paise"]))
    return out


# ── Charge capture ───────────────────────────────────────────────────────────


def _charge_line(row, *, interstate: bool = False) -> LineTotals:
    """Recompute one stored charge through money.compute_line.

    The charge row stores the INPUTS (rate, quantity, discount, GST treatment)
    that were in force when the charge was captured, and the intra-state totals
    computed from them. Invoicing recomputes from those same stored inputs — not
    from a fresh master lookup — so a bill raised today from a charge captured
    last week reproduces last week's price, while the arithmetic still comes from
    exactly one implementation. ``interstate`` is the only thing that can differ,
    because place of supply is a property of the INVOICE, not of the charge.
    """
    return compute_line(
        unit_rate_paise=int(row["unit_rate_paise"] or 0),
        quantity_milli=int(row["quantity_milli"] or QUANTITY_SCALE),
        # Stored signed (negative on a negative line); compute_line takes a
        # magnitude and re-applies the sign itself.
        discount_paise=abs(int(row["discount_paise"] or 0)),
        gst_rate_bp=int(row["gst_rate_bp"] or 0),
        gst_treatment=row["gst_treatment"] or GST_EXEMPT,
        interstate=interstate,
    )


def _decorate_charge(row: dict) -> dict:
    """Add the display strings a screen needs. Money is never formatted twice."""
    row["gross_display"] = format_inr(int(row["gross_paise"] or 0))
    row["total_display"] = format_inr(int(row["total_paise"] or 0))
    row["unit_rate_display"] = (
        format_inr(int(row["unit_rate_paise"]))
        if int(row.get("rate_is_configured") or 0) == 1
        else format_inr_or_unset(None)
    )
    row["tax_paise"] = (
        int(row["cgst_paise"] or 0) + int(row["sgst_paise"] or 0) + int(row["igst_paise"] or 0)
    )
    row["tax_display"] = format_inr(row["tax_paise"])
    row["quantity"] = int(row["quantity_milli"] or 0) / QUANTITY_SCALE
    return row


def _resolve_encounter_context(db, *, patient_id: str, admission_id: str | None) -> dict:
    """Payer, tariff plan and encounter type for a charge or an invoice."""
    payer = billing_masters.resolve_patient_payer(patient_id, admission_id=admission_id)
    admission = None
    if admission_id:
        admission = db.execute(
            "SELECT id, patient_id, ward, bed, department, status, created_at "
            "FROM admissions WHERE id = ?",
            (admission_id,),
        ).fetchone()
        if admission is not None and admission["patient_id"] != patient_id:
            raise BillingError(
                "that admission belongs to a different patient",
                code="admission_patient_mismatch",
            )
    return {
        "payer": payer,
        "admission": _row(admission) if admission is not None else None,
        "encounter_type": "ip" if admission_id else "op",
    }


def add_charge(
    *,
    patient_id: str,
    service_code: str,
    description: str | None = None,
    quantity_milli: int = QUANTITY_SCALE,
    unit_rate_paise: int | None = None,
    discount_paise: int = 0,
    discount_bp: int = 0,
    admission_id: str | None = None,
    encounter_type: str | None = None,
    ward_class: str | None = None,
    tariff_plan_id: int | None = None,
    gst_treatment: str | None = None,
    gst_rate_bp: int | None = None,
    service_date: str | None = None,
    source: str = "manual",
    source_ref: str | None = None,
    idempotency_key: str | None = None,
    notes: str | None = None,
    charged_by: str | None = None,
) -> dict:
    """Post ONE chargeable line against a patient (and optionally an admission).

    THE RATE IS RESOLVED, NOT ASSUMED. With no explicit ``unit_rate_paise`` the
    rate comes from ``billing_masters.resolve_rate`` under the patient's tariff
    plan, and an unconfigured item RAISES ``rate_not_configured`` rather than
    posting a ₹0 line. An explicit rate is allowed (an ad-hoc item, a negotiated
    one-off) and is recorded as given.

    Idempotent when ``idempotency_key`` is supplied: a replay returns the
    existing charge with ``replayed=True`` instead of billing the patient twice.
    That matters because the automatic capture path replays whenever a clinical
    event is replayed (offline queue, retry).
    """
    patient_id = _clean(patient_id)
    service_code = _clean(service_code)
    if not patient_id:
        raise BillingError("patient_id is required", code="invalid_input")
    if not service_code:
        raise BillingError("service_code is required", code="invalid_input")
    qty = int(quantity_milli)
    if qty <= 0:
        raise BillingError("quantity must be greater than zero", code="invalid_input")
    day = _iso_day(service_date)

    with db_session() as db:
        if idempotency_key:
            existing = db.execute(
                "SELECT * FROM encounter_charges WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                out = _decorate_charge(_row(existing))
                out["replayed"] = True
                return out

        if not _patient_exists(db, patient_id):
            raise BillingError(f"unknown patient {patient_id!r}", code="unknown_patient")
        context = _resolve_encounter_context(db, patient_id=patient_id, admission_id=admission_id)
        plan_id = tariff_plan_id if tariff_plan_id is not None else context["payer"].tariff_plan_id

        resolution = billing_masters.resolve_rate(
            service_code, tariff_plan_id=plan_id, ward_class=ward_class, on_date=day
        )
        if unit_rate_paise is None:
            if not resolution.configured:
                raise BillingError(
                    f"{service_code} has no price on tariff plan "
                    f"{resolution.tariff_plan_code or '(none)'}, price it before "
                    "billing it. Orb will not bill an unpriced item at ₹0.",
                    code="rate_not_configured",
                    detail=resolution.as_dict(),
                )
            rate = int(resolution.rate_paise or 0)
        else:
            rate = assert_paise(unit_rate_paise)

        treatment = gst_treatment or resolution.gst_treatment or GST_EXEMPT
        if treatment not in GST_TREATMENTS:
            raise BillingError(f"unknown GST treatment {treatment!r}", code="invalid_input")
        rate_bp = int(resolution.gst_rate_bp or 0) if gst_rate_bp is None else int(gst_rate_bp)

        try:
            line = compute_line(
                unit_rate_paise=rate,
                quantity_milli=qty,
                discount_paise=assert_paise(discount_paise),
                discount_bp=int(discount_bp),
                gst_rate_bp=rate_bp,
                gst_treatment=treatment,
                # Charges are computed intra-state: the patient is standing in
                # the hospital, which is the place of supply for all but an
                # exceptional case. Place of supply is settled again, per
                # INVOICE, when the document is raised.
                interstate=False,
            )
        except MoneyError as exc:
            raise BillingError(str(exc), code="invalid_amount") from exc

        text = _clean(description) or _clean(resolution.description) or service_code
        # The facility the charge was incurred at (multisite-7b, migration 75):
        # the admission's, else the patient's latest encounter; facility 1,
        # with no statement run, while only one facility is on record.
        facility_id = facility_identity.document_facility_id(
            db, patient_id=patient_id, admission_id=admission_id
        )
        # `rate_is_configured` is written as 1 unconditionally, and that is
        # correct BECAUSE of the refusal above: every path into this INSERT has
        # either resolved a configured rate or been handed one explicitly. There
        # is no way to reach here with an unpriced item, so there is no
        # zero-rupee row for a screen to mistake for a free service.
        cur = db.execute(
            """INSERT INTO encounter_charges
                 (patient_id, admission_id, encounter_type, service_item_id, service_code,
                  description, quantity_milli, unit_rate_paise, rate_is_configured,
                  tariff_plan_id, ward_class, gross_paise, discount_paise, gst_treatment,
                  gst_rate_bp, taxable_value_paise, cgst_paise, sgst_paise, igst_paise,
                  total_paise, status, source, source_ref, service_date, charged_by,
                  idempotency_key, notes, facility_id)
               VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                patient_id,
                admission_id,
                encounter_type or context["encounter_type"],
                resolution.service_item_id,
                service_code,
                text,
                qty,
                rate,
                plan_id,
                ward_class,
                line.gross_paise,
                line.discount_paise,
                line.gst_treatment,
                line.gst_rate_bp,
                line.taxable_value_paise,
                line.cgst_paise,
                line.sgst_paise,
                line.igst_paise,
                line.total_paise,
                _CHARGE_BILLABLE,
                source,
                source_ref,
                day,
                charged_by,
                idempotency_key,
                notes,
                facility_id,
            ),
        )
        charge_id = cur.lastrowid
        _audit(
            db,
            action_type="billing_charge_posted",
            summary=(
                f"Charge {charge_id} posted: {text} × {qty / QUANTITY_SCALE:g} = "
                f"{format_inr(line.total_paise)} ({source})"
            ),
            patient_id=patient_id,
            target_type="encounter_charge",
            target_id=charge_id,
            actor=charged_by,
            metadata={
                "service_code": service_code,
                "source": source,
                "source_ref": source_ref,
                "admission_id": admission_id,
                "total_paise": line.total_paise,
                "gst_treatment": line.gst_treatment,
                "rate_source": resolution.source,
                "rate_overridden": unit_rate_paise is not None,
            },
        )
        row = db.execute("SELECT * FROM encounter_charges WHERE id = ?", (charge_id,)).fetchone()

    out = _decorate_charge(_row(row))
    out["replayed"] = False
    out["rate_source"] = resolution.source
    return out


def list_charges(
    *,
    patient_id: str | None = None,
    admission_id: str | None = None,
    status: str | None = None,
    invoice_id: int | None = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT * FROM encounter_charges WHERE 1=1"
    params: list[Any] = []
    if patient_id:
        sql += " AND patient_id = ?"
        params.append(patient_id)
    if admission_id:
        sql += " AND admission_id = ?"
        params.append(admission_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if invoice_id is not None:
        sql += " AND invoice_id = ?"
        params.append(int(invoice_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]
    return [_decorate_charge(r) for r in rows]


#: The most outpatients one counter list names (opd-14). A day with more says
#: so (`truncated`) rather than quietly dropping the rest.
OP_PATIENTS_LIMIT = 200


def list_op_patients(
    *,
    day: str | None = None,
    scope_department: str | None = None,
    limit: int = OP_PATIENTS_LIMIT,
    facilities: frozenset[int] | None = None,
) -> dict:
    """Outpatients with something still to bill on one hospital-local day (opd-14).

    `facilities` (multisite-9a, document-keyed) is the caller's
    permissions.facility_scope: a set keeps the lines, and the visits shown
    beside them, whose own facility is one of them. None (one facility on
    record) changes nothing.

    A patient is listed when they have at least one PENDING outpatient line
    (encounter_type 'op', no admission) whose service day, read in the
    hospital's time zone, is `day` (today when None). Each entry carries the
    patient's name and UHID, that day's outpatient visits (clinic, doctor,
    token, status), how many lines are pending and unpriced, the priced total,
    and the ids of exactly those lines: the counter's "Bill this visit" bills
    those ids as an outpatient invoice, never an inpatient line.

    `scope_department` is permissions.department_scope(user), never a request
    value. A scoped reader sees only patients inside its department by the
    house rule (permissions.scope_patient_ids_sql), which is the same rule the
    per-patient billing routes apply, so nothing is listed that the desk could
    not then open.

    Read-only. The route audits the read (phi_access) when anyone is listed.
    """
    from permissions import scope_patient_ids_sql

    if day is None or not str(day).strip():
        when = hospital_today()
    else:
        try:
            when = date.fromisoformat(str(day).strip())
        except ValueError as exc:
            raise BillingError("date must be YYYY-MM-DD", code="invalid_input") from exc
    target = when.isoformat()
    # service_date holds either a bare local day or a UTC instant; a local day
    # is never more than a day from the UTC date it was stored under, so this
    # window catches every candidate and the exact day is decided below.
    lo = (when - timedelta(days=1)).isoformat()
    hi = (when + timedelta(days=2)).isoformat()
    cap = max(1, min(int(limit), OP_PATIENTS_LIMIT))

    from services.facility_documents import facility_clause

    scope_clause, scope_params = "", ()
    if scope_department is not None:
        scope_sql, scope_params = scope_patient_ids_sql(scope_department)
        scope_clause = f" AND ec.patient_id IN ({scope_sql})"
    facility_sql, facility_params = facility_clause("ec.facility_id", facilities)
    scope_clause += facility_sql
    scope_params = (*scope_params, *facility_params)
    visit_facility_sql, visit_facility_params = facility_clause("facility_id", facilities)
    with db_session() as db:
        rows = db.execute(
            f"""SELECT ec.id, ec.patient_id, ec.total_paise, ec.rate_is_configured,
                       ec.service_date, p.name AS patient_name, p.mrn AS patient_mrn
                  FROM encounter_charges ec
                  JOIN patients p ON p.id = ec.patient_id
                 WHERE ec.status = ? AND ec.encounter_type = 'op'
                   AND ec.admission_id IS NULL
                   AND ec.service_date >= ? AND ec.service_date < ?{scope_clause}
                 ORDER BY ec.id""",
            (_CHARGE_BILLABLE, lo, hi, *scope_params),
        ).fetchall()

        by_patient: dict[str, dict] = {}
        for r in rows:
            if _local_day_of_stored(r["service_date"]) != target:
                continue
            entry = by_patient.setdefault(
                r["patient_id"],
                {
                    "id": r["patient_id"],
                    "name": r["patient_name"],
                    "mrn": r["patient_mrn"],
                    "charge_ids": [],
                    "pending_charge_count": 0,
                    "unpriced_charge_count": 0,
                    "priced_total_paise": 0,
                },
            )
            entry["charge_ids"].append(int(r["id"]))
            entry["pending_charge_count"] += 1
            if int(r["rate_is_configured"] or 0) == 1:
                entry["priced_total_paise"] += int(r["total_paise"] or 0)
            else:
                entry["unpriced_charge_count"] += 1

        ordered = sorted(by_patient.values(), key=lambda e: ((e["name"] or "").casefold(), e["id"]))
        truncated = len(ordered) > cap
        listed = ordered[:cap]

        has_visits = _table_exists(db, "opd_visits")
        for entry in listed:
            entry["inpatient_now"] = (
                db.execute(
                    "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
                    (entry["id"],),
                ).fetchone()
                is not None
            )
            entry["visits"] = [
                {
                    "department": v["department"],
                    "doctor_name": v["doctor_name"],
                    "token_number": v["token_number"],
                    "status": v["status"],
                }
                for v in (
                    db.execute(
                        "SELECT department, doctor_name, token_number, status FROM opd_visits "
                        f"WHERE patient_id = ? AND visit_date = ?{visit_facility_sql} "
                        "ORDER BY registered_at, id",
                        (entry["id"], target, *visit_facility_params),
                    ).fetchall()
                    if has_visits
                    else []
                )
            ]
            entry["priced_total_display"] = format_inr(entry["priced_total_paise"])

    return {
        "date": target,
        "patients": listed,
        "patient_count": len(listed),
        "truncated": truncated,
    }


def cancel_charge(charge_id: int, *, reason: str, actor: str | None = None) -> dict:
    """Cancel an unbilled charge. A charge already on an invoice cannot be cancelled.

    The row is KEPT with a reason and an actor — a charge that vanishes is a
    charge nobody can explain at a reconciliation. Once the charge is on an
    invoice the invoice is the financial record, so the correction is a credit
    note or an invoice cancellation, not a quiet edit underneath it.
    """
    cleaned = _require_reason(reason, what="Cancelling a charge")
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM encounter_charges WHERE id = ?", (int(charge_id),)
        ).fetchone()
        if row is None:
            raise BillingError(f"no charge {charge_id}", code="not_found")
        if row["status"] == _CHARGE_CANCELLED:
            return _decorate_charge(_row(row))
        if row["status"] != _CHARGE_BILLABLE:
            raise BillingError(
                f"charge {charge_id} is {row['status']} and is on invoice "
                f"{row['invoice_id']}; cancel or credit the invoice instead",
                code="charge_not_cancellable",
                detail={"status": row["status"], "invoice_id": row["invoice_id"]},
            )
        db.execute(
            "UPDATE encounter_charges SET status = ?, cancelled_by = ?, "
            "cancelled_at = CURRENT_TIMESTAMP, cancel_reason = ? WHERE id = ? AND status = ?",
            (_CHARGE_CANCELLED, actor, cleaned, int(charge_id), _CHARGE_BILLABLE),
        )
        _audit(
            db,
            action_type="billing_charge_cancelled",
            summary=f"Charge {charge_id} ({row['description']}) cancelled: {cleaned}",
            patient_id=row["patient_id"],
            target_type="encounter_charge",
            target_id=charge_id,
            actor=actor,
            metadata={"reason": cleaned, "total_paise": int(row["total_paise"] or 0)},
        )
        fresh = db.execute(
            "SELECT * FROM encounter_charges WHERE id = ?", (int(charge_id),)
        ).fetchone()
    return _decorate_charge(_row(fresh))


# ── Pharmacy at batch MRP (grn-16, ruling GRN-R12, ORB_PHARMACY_STORE) ───────


def _pharmacy_store_on() -> bool:
    """The pharmacy store module is 'on' (not draining, not off)."""
    try:
        return config.module_state("pharmacy-store") == "on"
    except Exception:  # noqa: BLE001 - an unreadable switch reads as off
        return False


def _unit_mrp(lot: dict | None) -> int | None:
    """A batch's MRP for one dispense unit: the pack MRP over the units in a
    pack, by money.py's one rounding rule. None where the batch has no MRP or
    pack size recorded (nothing is guessed). The same figure the ward-return
    credit uses (pharmacy_store_service.credit_candidate)."""
    from services.money import _div_round_half_up

    mrp = (lot or {}).get("mrp_pack_paise")
    per_pack = (lot or {}).get("units_per_pack")
    if isinstance(mrp, bool) or isinstance(per_pack, bool):
        return None
    if not isinstance(mrp, int) or not isinstance(per_pack, int) or per_pack <= 0 or mrp < 0:
        return None
    return _div_round_half_up(mrp, per_pack)


def _drug_name(db, medication_id: str | None, cache: dict) -> str | None:
    med = _clean(medication_id)
    if not med:
        return None
    if med not in cache:
        row = (
            db.execute("SELECT name FROM helix_medication_master WHERE id = ?", (med,)).fetchone()
            if _table_exists(db, "helix_medication_master")
            else None
        )
        cache[med] = row["name"] if row is not None else None
    return cache[med]


def _dispensed_lots(db, order_id: str) -> list[dict]:
    """Every batch an order's dispense movements took, first taken first, with
    the drug that left the shelf (the substitute when one was set) and the
    units. One batch per order today (a dispense is never split)."""
    if not (
        _table_exists(db, "helix_stock_movements") and _table_exists(db, "helix_inventory_lots")
    ):
        return []
    out = []
    for r in db.execute(
        "SELECT s.lot_id AS lot_id, s.medication_id AS medication_id, "
        "-SUM(s.quantity_change) AS units FROM helix_stock_movements s "
        "WHERE s.order_id = ? AND s.movement_type = 'dispense' "
        "GROUP BY s.lot_id, s.medication_id ORDER BY MIN(s.seq), s.lot_id",
        (order_id,),
    ).fetchall():
        lot = db.execute(
            "SELECT id, lot_number, mrp_pack_paise, units_per_pack FROM helix_inventory_lots "
            "WHERE id = ?",
            (r["lot_id"],),
        ).fetchone()
        out.append(
            {
                "lot_id": r["lot_id"],
                "medication_id": r["medication_id"],
                "units": int(r["units"] or 0),
                "lot": _row(lot) if lot is not None else None,
            }
        )
    return out


def _dispense_key(order_id: str, lot_id: str | None, first_lot_id: str | None) -> str:
    """The charge key of one batch of an order's dispense: helix:<order> for
    the first (the key every pharmacy charge has carried), and the batch added
    for any further one, so a split dispense could never reuse a key."""
    if first_lot_id is None or lot_id == first_lot_id:
        return f"helix:{order_id}"
    return f"helix:{order_id}:{lot_id}"


def _dispense_candidates(db, *, patient_id: str) -> list[dict]:
    """The pharmacy source with the pharmacy store on (grn-16).

    Each dispensed order is priced from its dispense MOVEMENT: the drug that
    left the shelf (the substitute when one was dispensed, never the
    prescribed drug) and its batch, at the batch MRP per unit times the units
    dispensed. A batch with no MRP falls back to the tariff for that drug,
    units times the tariff rate (GRN-R12). An order with no dispense movement
    is priced on the tariff for the drug Helix dispensed
    (COALESCE(substitute, prescribed)).

    A ward return (grn-15) is its OWN credit candidate, keyed
    helix-return:<return id>, never a reuse of the order's key: it credits the
    returned units at the unit rate the order was charged (credit_of names
    that charge's key)."""
    out: list[dict] = []
    names: dict = {}
    order_cols = {
        r[1] for r in db.execute("PRAGMA table_info(helix_patient_med_orders)").fetchall()
    }
    substitute = (
        "o.substitute_medication_id" if "substitute_medication_id" in order_cols else "NULL"
    )
    first_lots: dict = {}
    for row in db.execute(
        f"SELECT o.id AS id, o.medication_id AS medication_id, {substitute} AS substitute_id, "
        "o.status AS status, o.created_at AS created_at FROM helix_patient_med_orders o "
        f"WHERE o.patient_id = ? AND o.status IN "
        f"({','.join('?' * len(_DISPENSED_MED_STATUSES))}) ORDER BY o.created_at",
        (patient_id, *_DISPENSED_MED_STATUSES),
    ).fetchall():
        day = _local_day_of_stored(row["created_at"])
        lots = [lot for lot in _dispensed_lots(db, row["id"]) if lot["units"] > 0]
        first_lots[row["id"]] = lots[0]["lot_id"] if lots else None
        if not lots:
            med_id = _clean(row["substitute_id"]) or _clean(row["medication_id"])
            name = _drug_name(db, med_id, names)
            out.append(
                {
                    "source": "pharmacy",
                    "source_ref": row["id"],
                    "idempotency_key": f"helix:{row['id']}",
                    "candidate_codes": [c for c in [med_id, f"RX-{med_id}" if med_id else ""] if c],
                    "description": name or med_id or "Pharmacy dispense",
                    "service_date": day,
                    "kind": "pharmacy",
                    "label": f"pharmacy: {name or med_id}",
                    "price_basis": "tariff",
                }
            )
            continue
        for lot in lots:
            med_id = _clean(lot["medication_id"])
            name = _drug_name(db, med_id, names)
            unit_mrp = _unit_mrp(lot["lot"])
            out.append(
                {
                    "source": "pharmacy",
                    "source_ref": row["id"],
                    "idempotency_key": _dispense_key(
                        row["id"], lot["lot_id"], first_lots[row["id"]]
                    ),
                    "candidate_codes": [c for c in [med_id, f"RX-{med_id}" if med_id else ""] if c],
                    "description": name or med_id or "Pharmacy dispense",
                    "service_date": day,
                    "kind": "pharmacy",
                    "label": f"pharmacy: {name or med_id}",
                    "units": lot["units"],
                    "quantity_milli": lot["units"] * QUANTITY_SCALE,
                    "medication_id": med_id,
                    "prescribed_medication_id": _clean(row["medication_id"]),
                    "lot_id": lot["lot_id"],
                    "batch": (lot["lot"] or {}).get("lot_number"),
                    "unit_mrp_paise": unit_mrp,
                    "price_basis": "batch_mrp" if unit_mrp is not None else "tariff",
                }
            )

    if _table_exists(db, "pharmacy_ward_returns"):
        for r in db.execute(
            "SELECT id, helix_order_id, helix_medication_id, lot_id, quantity_units, "
            "occurred_at, created_at FROM pharmacy_ward_returns WHERE patient_id = ? "
            "ORDER BY id",
            (patient_id,),
        ).fetchall():
            order_id = r["helix_order_id"]
            if order_id not in first_lots:
                lots = [lot for lot in _dispensed_lots(db, order_id) if lot["units"] > 0]
                first_lots[order_id] = lots[0]["lot_id"] if lots else None
            units = int(r["quantity_units"] or 0)
            if units <= 0:
                continue
            name = (
                _drug_name(db, r["helix_medication_id"], names)
                or _clean(r["helix_medication_id"])
                or "Pharmacy return"
            )
            out.append(
                {
                    "source": "pharmacy",
                    "source_ref": str(r["id"]),
                    "idempotency_key": f"helix-return:{r['id']}",
                    "candidate_codes": [],
                    "description": f"Returned: {name}, {_count(units, 'unit')}",
                    "service_date": _local_day_of_stored(r["occurred_at"] or r["created_at"]),
                    "kind": "pharmacy_return",
                    "label": f"pharmacy return: {name}",
                    "units": units,
                    "quantity_milli": units * QUANTITY_SCALE,
                    "credit_of": _dispense_key(order_id, r["lot_id"], first_lots.get(order_id)),
                }
            )
    return out


def _tax_inclusive_unit_rate(unit_mrp: int, units: int, rate_bp: int) -> int:
    """The largest taxable value per unit whose line, GST added, does not pass
    units times the unit MRP. MRP is the tax-inclusive ceiling a medicine may
    be sold at, so a taxable line at MRP carries its tax inside the MRP, never
    on top of it."""
    qty = units * QUANTITY_SCALE
    ceiling = unit_mrp * units

    def total(rate: int) -> int:
        return compute_line(
            unit_rate_paise=rate, quantity_milli=qty, gst_rate_bp=rate_bp, gst_treatment="taxable"
        ).total_paise

    rate = (unit_mrp * 10000) // (10000 + rate_bp)
    while total(rate + 1) <= ceiling:
        rate += 1
    while rate > 0 and total(rate) > ceiling:
        rate -= 1
    return rate


def _mrp_line(cand: dict, *, plan_id: int | None, ward_class: str | None) -> dict:
    """How a batch-MRP candidate is charged: the service item (if the price
    list has the drug) supplies the code and the GST treatment, the batch the
    price. Without a service item the line is recorded under RX-<drug> and is
    exempt, the module's default treatment for an item the list does not know."""
    resolution = None
    for code in cand["candidate_codes"]:
        attempt = billing_masters.resolve_rate(
            code, tariff_plan_id=plan_id, ward_class=ward_class, on_date=cand["service_date"]
        )
        if attempt.source != "unknown_item":
            resolution = attempt
            break
    med_id = cand.get("medication_id") or ""
    service_code = resolution.service_code if resolution is not None else f"RX-{med_id}"
    treatment = (resolution.gst_treatment if resolution is not None else GST_EXEMPT) or GST_EXEMPT
    rate_bp = int(resolution.gst_rate_bp or 0) if resolution is not None else 0
    unit_mrp = int(cand["unit_mrp_paise"])
    units = int(cand["units"])
    inclusive = treatment == "taxable" and rate_bp > 0
    rate = _tax_inclusive_unit_rate(unit_mrp, units, rate_bp) if inclusive else unit_mrp
    line = compute_line(
        unit_rate_paise=rate,
        quantity_milli=units * QUANTITY_SCALE,
        gst_rate_bp=rate_bp,
        gst_treatment=treatment,
    )
    note = (
        f"Priced at batch MRP: {format_inr(unit_mrp)} a unit"
        + (f", batch {cand['batch']}" if cand.get("batch") else "")
        + (", GST included in the MRP" if inclusive else "")
    )
    return {
        "service_code": service_code,
        "unit_rate_paise": rate,
        "quantity_milli": units * QUANTITY_SCALE,
        "gst_treatment": treatment,
        "gst_rate_bp": rate_bp,
        "total_paise": line.total_paise,
        "notes": note,
        "rate_display": format_inr(unit_mrp),
    }


def _clinical_candidates(db, *, patient_id: str) -> list[dict]:
    """Chargeable things Orb ALREADY KNOWS HAPPENED. Read-only, always.

    Two sources, both of which are records of work that was actually done:

      * ``orders`` of type lab / imaging / procedure that reached
        ``completed``/``resulted``. A draft, active or cancelled order is not a
        charge, and a MEDICATION order is deliberately excluded — the drug is
        billed when the pharmacy dispenses it, and billing the order too would
        charge the patient twice for one dose.
      * ``helix_patient_med_orders`` that reached ``dispensed``/``delivered`` —
        the drug physically left the pharmacy.

        UNTIL 2026-09-07 NONE OF THESE WERE EVER BILLED. The filter here was
        ``IFNULL(o.is_mock_event, 0) = 0``, and ``helix_service`` set
        ``is_mock_event = 1`` on EVERY transition into ``dispensed`` and
        ``delivered`` — unconditionally, a leftover from a demonstration script.
        So the two writers agreed perfectly and the pharmacy revenue line was
        structurally zero, in silence, on every site. The column is gone
        (migration 35) and so is the filter: a dispense is a dispense.

    This function READS clinical tables and writes nothing to them. No clinical
    path learns that billing exists.
    """
    candidates: list[dict] = []

    for row in db.execute(
        f"""SELECT id, order_type, code, code_system, description, status,
                   result_at, created_at
              FROM orders
             WHERE patient_id = ?
               AND order_type IN ({",".join("?" * len(_BILLABLE_ORDER_TYPES))})
               AND status IN ({",".join("?" * len(_COMPLETED_ORDER_STATUSES))})
             ORDER BY created_at""",
        (patient_id, *_BILLABLE_ORDER_TYPES, *_COMPLETED_ORDER_STATUSES),
    ).fetchall():
        candidates.append(
            {
                "source": "order",
                "source_ref": row["id"],
                "idempotency_key": f"order:{row['id']}",
                # The order's own code (LOINC/local) is the only honest join to a
                # price list. A site prices a lab by creating a service item whose
                # code is the code its order catalogue already uses.
                "candidate_codes": [c for c in [_clean(row["code"])] if c],
                "description": row["description"],
                "service_date": _local_day_of_stored(row["result_at"] or row["created_at"]),
                "kind": row["order_type"],
                "label": f"{row['order_type']}: {row['description']}",
            }
        )

    # With the pharmacy store on (grn-16) the pharmacy source is read from the
    # dispense movements instead: the drug that left the shelf, at its batch
    # MRP, and ward returns as credits. Off, the lines below are unchanged.
    if _pharmacy_store_on() and _table_exists(db, "helix_patient_med_orders"):
        candidates.extend(_dispense_candidates(db, patient_id=patient_id))
        return candidates

    if _table_exists(db, "helix_patient_med_orders"):
        has_master = _table_exists(db, "helix_medication_master")
        sql = (
            "SELECT o.id AS id, o.medication_id AS medication_id, o.status AS status, "
            "o.created_at AS created_at"
            + (", m.name AS med_name" if has_master else ", NULL AS med_name")
            + " FROM helix_patient_med_orders o"
            + (
                " LEFT JOIN helix_medication_master m ON m.id = o.medication_id"
                if has_master
                else ""
            )
            + f" WHERE o.patient_id = ? AND o.status IN "
            f"({','.join('?' * len(_DISPENSED_MED_STATUSES))}) "
            "ORDER BY o.created_at"
        )
        for row in db.execute(sql, (patient_id, *_DISPENSED_MED_STATUSES)).fetchall():
            med_id = _clean(row["medication_id"])
            candidates.append(
                {
                    "source": "pharmacy",
                    "source_ref": row["id"],
                    "idempotency_key": f"helix:{row['id']}",
                    # Two spellings so a site can key its pharmacy price list either
                    # on the Helix medication id or on an RX- prefixed code.
                    "candidate_codes": [c for c in [med_id, f"RX-{med_id}" if med_id else ""] if c],
                    "description": row["med_name"] or med_id or "Pharmacy dispense",
                    "service_date": _local_day_of_stored(row["created_at"]),
                    "kind": "pharmacy",
                    "label": f"pharmacy: {row['med_name'] or med_id}",
                }
            )

    return candidates


def capture_clinical_charges(
    *,
    patient_id: str,
    admission_id: str | None = None,
    ward_class: str | None = None,
    actor: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Pull the chargeable clinical events Orb already recorded onto the bill.

    Returns ``{"posted": [...], "skipped": [...], ...}``. An event Orb cannot
    price is SKIPPED WITH A REASON — never posted at ₹0 — because a zero-rupee
    line on a bill reads as "free" and is indistinguishable from an item nobody
    priced. The reasons are the clerk's worklist:

      ``already_charged``      — captured on an earlier run (idempotent).
      ``no_service_code``      — the clinical record carries no code to price on.
      ``no_service_item``      — no service item exists for that code.
      ``rate_not_configured``  — the item exists but has no price on this tariff.
      ``order_not_charged``    a ward return (pharmacy store on) whose
                                 dispense is not on the bill: nothing to credit.

    With the pharmacy store on (grn-16) a dispense with a batch MRP is charged
    at that MRP per unit times the units dispensed, whatever the tariff says,
    and a ward return posts its own negative line; see _dispense_candidates.

    ``dry_run=True`` computes the same answer and writes nothing; that is how the
    discharge advisory can say "there are events this bill does not yet include"
    without posting charges as a side effect of asking a question.
    """
    patient_id = _clean(patient_id)
    posted: list[dict] = []
    skipped: list[dict] = []

    with db_session() as db:
        if not _patient_exists(db, patient_id):
            raise BillingError(f"unknown patient {patient_id!r}", code="unknown_patient")
        context = _resolve_encounter_context(db, patient_id=patient_id, admission_id=admission_id)
        plan_id = context["payer"].tariff_plan_id
        candidates = _clinical_candidates(db, patient_id=patient_id)
        existing_keys = {
            r["idempotency_key"]
            for r in db.execute(
                "SELECT idempotency_key FROM encounter_charges "
                "WHERE patient_id = ? AND idempotency_key IS NOT NULL",
                (patient_id,),
            ).fetchall()
        }

    # The unit rate each pharmacy line was (or, in a dry run, would be)
    # charged at, by key, so a return in the same run credits what was billed.
    charged_at: dict = {}

    for cand in candidates:
        if cand["idempotency_key"] in existing_keys:
            skipped.append({**_candidate_view(cand), "reason": "already_charged"})
            continue
        if cand.get("credit_of") is not None:
            basis = charged_at.get(cand["credit_of"]) or _charged_line(
                patient_id, cand["credit_of"]
            )
            if basis is None:
                # Nothing on the bill to credit: the dispense was never
                # charged, or its charge was cancelled.
                skipped.append({**_candidate_view(cand), "reason": "order_not_charged"})
                continue
            credit = compute_line(
                unit_rate_paise=-int(basis["unit_rate_paise"]),
                quantity_milli=int(cand["quantity_milli"]),
                gst_rate_bp=int(basis["gst_rate_bp"] or 0),
                gst_treatment=basis["gst_treatment"] or GST_EXEMPT,
            )
            if dry_run:
                posted.append(
                    {
                        **_candidate_view(cand),
                        "service_code": basis["service_code"],
                        "rate_paise": -int(basis["unit_rate_paise"]),
                        "total_paise": credit.total_paise,
                        "would_post": True,
                    }
                )
                continue
            charge = add_charge(
                patient_id=patient_id,
                admission_id=admission_id,
                service_code=basis["service_code"],
                description=cand["description"],
                quantity_milli=int(cand["quantity_milli"]),
                unit_rate_paise=-int(basis["unit_rate_paise"]),
                gst_treatment=basis["gst_treatment"] or GST_EXEMPT,
                gst_rate_bp=int(basis["gst_rate_bp"] or 0),
                ward_class=ward_class,
                tariff_plan_id=plan_id,
                service_date=cand["service_date"],
                source=cand["source"],
                source_ref=str(cand["source_ref"]),
                idempotency_key=cand["idempotency_key"],
                notes=f"Credit for medicine returned from the ward (charge {cand['credit_of']})",
                charged_by=actor,
            )
            posted.append(
                {
                    **_candidate_view(cand),
                    "charge_id": charge["id"],
                    "service_code": charge["service_code"],
                    "total_paise": charge["total_paise"],
                    "total_display": charge["total_display"],
                }
            )
            continue
        if cand.get("unit_mrp_paise") is not None:
            line = _mrp_line(cand, plan_id=plan_id, ward_class=ward_class)
            charged_at[cand["idempotency_key"]] = line
            if dry_run:
                posted.append(
                    {
                        **_candidate_view(cand),
                        "service_code": line["service_code"],
                        "rate_paise": line["unit_rate_paise"],
                        "rate_display": line["rate_display"],
                        "total_paise": line["total_paise"],
                        "would_post": True,
                    }
                )
                continue
            charge = add_charge(
                patient_id=patient_id,
                admission_id=admission_id,
                service_code=line["service_code"],
                description=cand["description"],
                quantity_milli=line["quantity_milli"],
                unit_rate_paise=line["unit_rate_paise"],
                gst_treatment=line["gst_treatment"],
                gst_rate_bp=line["gst_rate_bp"],
                ward_class=ward_class,
                tariff_plan_id=plan_id,
                service_date=cand["service_date"],
                source=cand["source"],
                source_ref=str(cand["source_ref"]),
                idempotency_key=cand["idempotency_key"],
                notes=line["notes"],
                charged_by=actor,
            )
            posted.append(
                {
                    **_candidate_view(cand),
                    "charge_id": charge["id"],
                    "service_code": charge["service_code"],
                    "total_paise": charge["total_paise"],
                    "total_display": charge["total_display"],
                }
            )
            continue
        if not cand["candidate_codes"]:
            skipped.append({**_candidate_view(cand), "reason": "no_service_code"})
            continue

        resolution = None
        for code in cand["candidate_codes"]:
            attempt = billing_masters.resolve_rate(
                code,
                tariff_plan_id=plan_id,
                ward_class=ward_class,
                on_date=cand["service_date"],
            )
            if attempt.source != "unknown_item":
                resolution = attempt
                break
        if resolution is None:
            skipped.append(
                {
                    **_candidate_view(cand),
                    "reason": "no_service_item",
                    "hint": (
                        "create a service item with code "
                        f"{cand['candidate_codes'][0]!r} to price this"
                    ),
                }
            )
            continue
        if not resolution.configured:
            skipped.append(
                {
                    **_candidate_view(cand),
                    "reason": "rate_not_configured",
                    "service_code": resolution.service_code,
                    "rate_display": resolution.display,
                }
            )
            continue
        charged_at[cand["idempotency_key"]] = {
            "service_code": resolution.service_code,
            "unit_rate_paise": resolution.rate_paise,
            "gst_rate_bp": resolution.gst_rate_bp,
            "gst_treatment": resolution.gst_treatment,
        }

        if dry_run:
            posted.append(
                {
                    **_candidate_view(cand),
                    "service_code": resolution.service_code,
                    "rate_paise": resolution.rate_paise,
                    "rate_display": resolution.display,
                    # More than one unit (a grn-14 dispense): the line, not the rate.
                    **(
                        {
                            "total_paise": compute_line(
                                unit_rate_paise=int(resolution.rate_paise or 0),
                                quantity_milli=int(cand["quantity_milli"]),
                                gst_rate_bp=int(resolution.gst_rate_bp or 0),
                                gst_treatment=resolution.gst_treatment or GST_EXEMPT,
                            ).total_paise
                        }
                        if cand.get("quantity_milli", QUANTITY_SCALE) != QUANTITY_SCALE
                        else {}
                    ),
                    "would_post": True,
                }
            )
            continue

        charge = add_charge(
            patient_id=patient_id,
            admission_id=admission_id,
            service_code=resolution.service_code,
            description=cand["description"],
            # Units dispensed for a pharmacy line with the store on (grn-16);
            # one unit for every other candidate, as always.
            quantity_milli=cand.get("quantity_milli", QUANTITY_SCALE),
            ward_class=ward_class,
            tariff_plan_id=plan_id,
            service_date=cand["service_date"],
            source=cand["source"],
            source_ref=str(cand["source_ref"]),
            idempotency_key=cand["idempotency_key"],
            charged_by=actor,
        )
        posted.append(
            {
                **_candidate_view(cand),
                **{
                    "charge_id": charge["id"],
                    "service_code": charge["service_code"],
                    "total_paise": charge["total_paise"],
                    "total_display": charge["total_display"],
                },
            }
        )

    posted_total = sum(int(p.get("total_paise") or p.get("rate_paise") or 0) for p in posted)
    unpriceable = [s for s in skipped if s["reason"] != "already_charged"]
    return {
        "patient_id": patient_id,
        "admission_id": admission_id,
        "dry_run": dry_run,
        "posted": posted,
        "skipped": skipped,
        "posted_count": len(posted),
        "posted_total_paise": posted_total,
        "posted_total_display": format_inr(posted_total),
        # The honest headline: how many things happened to this patient that the
        # bill cannot yet include. It is what turns a discharge advisory from
        # "₹0 outstanding" into "unknown".
        "unpriceable_count": len(unpriceable),
    }


def _candidate_view(cand: dict) -> dict:
    return {
        "source": cand["source"],
        "source_ref": str(cand["source_ref"]),
        "description": cand["description"],
        "kind": cand["kind"],
        "service_date": cand["service_date"],
    }


def _charged_line(patient_id: str, key: str) -> dict | None:
    """The live (not cancelled) charge a return credit reverses, by its key,
    or None. A return is credited at the unit rate the patient was charged."""
    with db_session() as db:
        row = db.execute(
            "SELECT service_code, unit_rate_paise, gst_treatment, gst_rate_bp "
            "FROM encounter_charges WHERE patient_id = ? AND idempotency_key = ? "
            "AND status != ? AND rate_is_configured = 1",
            (patient_id, key, _CHARGE_CANCELLED),
        ).fetchone()
    return _row(row) if row is not None else None


def _discharge_stamp(db, admission: dict) -> str | None:
    """When a discharged admission ended, as stored (UTC), or None.

    ``admissions.discharged_at`` first: an imported encounter carries it, and
    since migration 58 (adt-8a) so does every end of stay the beds and
    admissions module records with ORB_ADT on, including ends with no
    discharge summary (a death, a LAMA, an absconded closure). An Orb-native
    discharge with ORB_ADT off leaves it NULL and timestamps its discharge
    summary instead. The summary is the patient's first one written at or
    after this admission began, so an earlier stay's summary is never taken
    for this one. services/stay_times.stay_end is the shared reader of the
    same rule (it also bounds the summary by the next admission's start), and
    its docstring corrects the migrations._m15 note, which predates ADT."""
    if admission.get("discharged_at"):
        return admission["discharged_at"]
    row = db.execute(
        """SELECT discharged_at FROM discharge_summaries
            WHERE patient_id = ? AND discharged_at IS NOT NULL
              AND julianday(discharged_at) >= julianday(?)
            ORDER BY julianday(discharged_at) ASC, id ASC LIMIT 1""",
        (admission["patient_id"], admission.get("created_at") or "1970-01-01"),
    ).fetchone()
    return row["discharged_at"] if row else None


#: The movement types that change where a patient lies, and so the ward a bed
#: day is priced at. The ledger also records an admission, a bed assigned on
#: arrival, leave and its end, absconding and return, a discharge, and the link
#: of an existing admission to a bed (migration 51's vocabulary); none of those
#: moves the patient between wards, so none of them may reprice a day.
_REPRICING_MOVEMENT_TYPES = ("transfer", "bed_change", "bed_swap", "correction")


def _admission_moves(db, admission_id: str) -> list[dict]:
    """The admission's ward moves, oldest first. The ledger table is created
    by migration 51 (and lazily by services/frontdesk_service), so a database
    without it has no transfers to read, which is the same answer as an empty
    ledger."""
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'patient_movements'"
        ).fetchone()
        is None
    ):
        return []
    placeholders = ", ".join("?" for _ in _REPRICING_MOVEMENT_TYPES)
    rows = db.execute(
        f"""SELECT from_ward, to_ward, moved_at FROM patient_movements
            WHERE admission_id = ? AND movement_type IN ({placeholders})
            ORDER BY julianday(moved_at) ASC, rowid ASC""",
        (admission_id, *_REPRICING_MOVEMENT_TYPES),
    ).fetchall()
    return [_row(r) for r in rows]


def _ward_by_day(moves: list[dict], current_ward: str | None, start_d: date, end_d: date) -> dict:
    """The ward each bed day is priced at: the ward the patient was in at the
    END of that hospital-local day (a midnight census), so a transfer day is
    billed at the ward the patient moved to. Before the first transfer the
    patient was in that transfer's from-ward. With no transfers every day is
    the admission's ward, as before."""
    if not moves:
        return {}
    ward = moves[0].get("from_ward") or current_ward
    dated = [(_local_day_of_stored(m.get("moved_at")), m.get("to_ward")) for m in moves]
    out = {}
    day = start_d
    idx = 0
    while day <= end_d:
        while (
            idx < len(dated)
            and (dated_day := dated[idx][0]) is not None
            and dated_day <= day.isoformat()
        ):
            ward = dated[idx][1] or ward
            idx += 1
        out[day] = ward
        day += timedelta(days=1)
    return out


# ── Bed days by bed class (adt-15, ORB_ADT) ──────────────────────────────────

#: Movements that place a stay in a bed, the list the ADT census reads
#: (services/adt_census_service._LOCATING_TYPES). A movement a correction
#: points at (corrects_movement_id) never happened.
_LOCATING_MOVEMENT_TYPES = (
    "admission",
    "bed_assigned",
    "bed_change",
    "transfer",
    "bed_swap",
    "location_linked",
    "correction",
)

#: Presence movements (adt-10): between a leave_start and the next of these the
#: patient is on leave. Absconding is not leave; its days are charged as days
#: on the ward (ADT-P6 speaks of leave only).
_PRESENCE_MOVEMENT_TYPES = ("leave_start", "leave_end", "absconded", "absconded_return")

#: Why a day could not be priced by its bed class, in words for the desk.
BED_DAY_UNPRICED_WORDS = {
    "no_bed": "Not in a bed from bed setup at midnight",
    "no_bed_class": "The bed has no bed class in bed setup",
    "no_service_code": "The bed class has no bed-day charge code in bed setup",
    "no_service_item": "The bed-day charge code is not on the price list",
    "rate_not_configured": "No price on this tariff for that day",
}


def _adt_module_on() -> bool:
    """The beds and admissions module is 'on' (not draining, not off)."""
    try:
        return config.module_state("adt") == "on"
    except Exception:  # noqa: BLE001 - an unreadable switch reads as off
        return False


def _parse_stored(value: Any) -> datetime | None:
    """A stored time as an aware UTC datetime (clinical_time reads every
    legacy spelling), or None."""
    from services.clinical_time import parse_to_utc

    if value is None or value == "":
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


def _midnight_after(day: date) -> datetime:
    """The midnight that ends a hospital-local day, in UTC: the census instant."""
    tz = hospital_timezone()
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(UTC)


def _stay_ledger(db, admission: dict) -> tuple[list[dict], list[dict]]:
    """(placing moves, presence moves) of one stay, in the order they happened
    (moved_at, ties by the order they were written), with every movement a
    correction points at left out. Empty without the migration 51 ledger."""
    if not _table_exists(db, "patient_movements"):
        return [], []
    cols = {r[1] for r in db.execute("PRAGMA table_info(patient_movements)").fetchall()}
    if not {"to_bed_id", "from_bed_id", "corrects_movement_id"} <= cols:
        return [], []
    rows = [
        _row(r)
        for r in db.execute(
            "SELECT rowid AS row_order, id, movement_type, moved_at, from_bed_id, to_bed_id, "
            "corrects_movement_id FROM patient_movements WHERE admission_id = ?",
            (admission["id"],),
        ).fetchall()
    ]
    voided = {r["corrects_movement_id"] for r in rows if r.get("corrects_movement_id")}
    floor = datetime.min.replace(tzinfo=UTC)
    for r in rows:
        r["at"] = _parse_stored(r.get("moved_at")) or floor
    live = sorted(
        (r for r in rows if r["id"] not in voided), key=lambda r: (r["at"], r.get("row_order") or 0)
    )
    return (
        [r for r in live if r["movement_type"] in _LOCATING_MOVEMENT_TYPES],
        [r for r in live if r["movement_type"] in _PRESENCE_MOVEMENT_TYPES],
    )


def _bed_at(
    placing: list[dict], admission: dict, instant: datetime, *, inclusive: bool
) -> str | None:
    """The bed (housekeeping_units.id) the stay lay in at `instant`.

    Before the first placing move: nowhere when that move is the admission
    itself, the bed a link records when it is a link (a link places a patient
    who was already lying there), else the move's from-bed. With no moves at
    all, the admission's own bed."""
    if not placing:
        return admission.get("bed_id")
    first = placing[0]
    if first["movement_type"] == "admission":
        bed = None
    elif first["movement_type"] == "location_linked":
        bed = first.get("to_bed_id")
    else:
        bed = first.get("from_bed_id")
    for move in placing:
        if move["at"] < instant or (inclusive and move["at"] <= instant):
            bed = move.get("to_bed_id")
        else:
            break
    return bed


def _on_leave_at(presence: list[dict], instant: datetime, *, inclusive: bool) -> bool:
    state = None
    for move in presence:
        if move["at"] < instant or (inclusive and move["at"] <= instant):
            state = move["movement_type"]
        else:
            break
    return state == "leave_start"


def _bed_master(db, bed_ids: set) -> tuple[dict, dict]:
    """(beds by id, bed classes by code) for the beds a stay lay in."""
    beds: dict = {}
    classes: dict = {}
    ids = sorted(b for b in bed_ids if b)
    if ids and _table_exists(db, "housekeeping_units"):
        marks = ",".join("?" for _ in ids)
        for r in db.execute(
            f"SELECT id, bed_label, ward, bed_class FROM housekeeping_units WHERE id IN ({marks})",
            tuple(ids),
        ).fetchall():
            beds[r["id"]] = _row(r)
    if _table_exists(db, "bed_classes"):
        for r in db.execute("SELECT code, name, bed_day_service_code FROM bed_classes").fetchall():
            classes[r["code"]] = _row(r)
    return beds, classes


def _class_of(bed: dict | None, classes: dict) -> dict | None:
    code = _clean((bed or {}).get("bed_class"))
    if not code:
        return None
    found = classes.get(code)
    if found is None:
        folded = {k.strip().casefold(): v for k, v in classes.items()}
        found = folded.get(code.casefold())
    return found


def post_bed_day_charges(
    *,
    admission_id: str,
    service_code: str | None = None,
    through: str | None = None,
    ward_class: str | None = None,
    actor: str | None = None,
    patient_id: str | None = None,
) -> dict:
    """Post one bed-day charge per calendar day of an admission, idempotently.

    Bed days are the largest single line on most Indian IPD bills and Orb records
    no per-day event for them, so they are DERIVED here from the admission date.
    One charge per calendar day, keyed ``bedday:<admission>:<date>``, so running
    this every morning (or twice in one morning) cannot double-charge a day.

    WHAT THIS DOES NOT DECIDE: whether the DISCHARGE day is chargeable. Hospitals
    differ (checkout time, part-day policy), Orb has no field for the policy, and
    guessing would put a day of room rent on a bill nobody authorised. The
    default ``through`` is the discharge date for a discharged admission and
    today for a live one; a site that does not charge the discharge day passes
    the day before. Recorded as a followup, not silently assumed.

    THE DISCHARGE DATE comes from :func:`_discharge_stamp`. It used to be
    today's date for every admission, because ``admissions.discharged_at`` is
    NULL for an Orb-native discharge (its time is on discharge_summaries), so a
    run a week after discharge billed seven more days.

    EACH DAY IS PRICED AT ITS OWN WARD (:func:`_ward_by_day`), from the
    transfer ledger. The admission row holds only the CURRENT ward, so a CCU
    stay that ended on a general ward was billed entirely at general rates.
    An explicit ``ward_class`` still prices every day at that class.

    THE STAY STARTS AT ITS ARRIVAL (adt-8c, services/stay_times): admitted_at,
    which beds and admissions (ORB_ADT) records and may back-time, else
    created_at. Without ORB_ADT admitted_at is never written, so the first day
    is the one it always was.

    BY BED CLASS (adt-15, ORB_ADT on, no ``service_code``). Each day is priced
    by the bed the patient lay in at the midnight census (the midnight that
    ends the day; the departure for the last day of an ended stay, now for
    today): the ledger's placing moves give the bed (to_bed_id), the bed its
    class, and the class (bed_classes) the charge code (bed_day_service_code),
    priced on the tariff row for that class (ward_class = the class code). A
    day that cannot be priced that way is not charged and is named with the
    reason in ``unpriced_detail`` (and its date in ``unpriced_days``): no bed,
    a bed with no class, a class with no code, a code not on the price list,
    no price that day. The days already posted are never posted again (one
    key per day, whichever way it was priced). With ORB_ADT off a service code
    is still required, as before.

    LEAVE DAYS (ruling ADT-P6, ORB_ADT on): a day the patient was on leave at
    the midnight census is charged when ORB_ADT_LEAVE_RENT_CHARGED is on (the
    default: the bed is held) and is not charged when it is off; either way
    the day is listed in ``leave_days``. Absconded days are charged as days on
    the ward.

    ``patient_id``, when given (the route passes the path's patient), must be
    the admission's patient: bed days are never posted on another patient's
    stay through a patient the caller may reach.
    """
    from services import stay_times

    service_code = _clean(service_code)
    adt_on = _adt_module_on()
    by_class = not service_code
    if by_class and not adt_on:
        raise BillingError("a bed-day service code is required", code="invalid_input")
    if by_class and _clean(ward_class):
        raise BillingError(
            "a ward class prices bed days only with a bed-day service code; without "
            "one each day is priced by the class of its bed",
            code="invalid_input",
        )
    with db_session() as db:
        adm = db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone()
        if adm is None:
            raise BillingError(f"no admission {admission_id!r}", code="not_found")
        admission = _row(adm)
        if patient_id is not None and admission.get("patient_id") != _clean(patient_id):
            raise BillingError(
                "that admission belongs to a different patient",
                code="admission_patient_mismatch",
            )
        ended = (admission.get("status") or "active") != "active"
        discharge_stamp = (
            _discharge_stamp(db, admission)
            if admission.get("status") == "discharged" or (by_class and ended)
            else None
        )
        moves = _admission_moves(db, admission_id)
        placing, presence = _stay_ledger(db, admission) if adt_on else ([], [])
        beds: dict[str, dict]
        classes: dict[int, dict]
        beds, classes = ({}, {})
        existing_keys: set = set()
        if by_class:
            beds, classes = _bed_master(
                db,
                {admission.get("bed_id")}
                | {m.get("to_bed_id") for m in placing}
                | {m.get("from_bed_id") for m in placing},
            )
            existing_keys = {
                r["idempotency_key"]
                for r in db.execute(
                    "SELECT idempotency_key FROM encounter_charges WHERE admission_id = ? "
                    "AND idempotency_key IS NOT NULL",
                    (admission_id,),
                ).fetchall()
            }

    # The admission timestamp is stored UTC; the bed day it starts is a
    # hospital-local calendar day. See _local_day_of_stored.
    stay_start = stay_times.stay_start_stored(admission)
    start = _local_day_of_stored(stay_start) or hospital_today().isoformat()
    if by_class:
        last = _local_day_of_stored(discharge_stamp) if ended else hospital_today().isoformat()
        if through:
            end: str | None
            end = _iso_day(through)
            if last and end > last:
                raise BillingError(
                    f"bed days can be posted only up to {last}, the "
                    + ("day this stay ended" if ended else "current day"),
                    code="through_after_stay_end",
                    detail={"last_day": last},
                )
        elif last:
            end = last
        else:
            raise BillingError(
                "this admission has ended but its end time is not recorded; give the "
                "last chargeable day",
                code="invalid_input",
            )
    elif through:
        end = _iso_day(through)
    elif admission.get("status") == "discharged":
        end = _local_day_of_stored(discharge_stamp)
        if not end:
            # Never "today" for a discharged stay: that bills every day since.
            raise BillingError(
                "this admission is discharged but its discharge time is not "
                "recorded; give the last chargeable day",
                code="invalid_input",
            )
    else:
        end = hospital_today().isoformat()
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as exc:
        raise BillingError(
            f"unreadable date on this admission: {exc}", code="invalid_input"
        ) from exc
    if end_d < start_d:
        raise BillingError("the through-date is before the admission date", code="invalid_input")

    wards = _ward_by_day(moves, admission.get("ward"), start_d, end_d)
    posted: list[dict]
    posted, replayed, unpriced_days = [], [], []
    unpriced_detail: list[dict] = []
    leave_days: list[str] = []
    leave_rent = bool(getattr(config, "ADT_LEAVE_RENT_CHARGED", True))
    # The census instant of the stay's last day: its departure once it has
    # ended, now while it is live (today's midnight has not come yet).
    cap = (_parse_stored(discharge_stamp) if ended else datetime.now(UTC)) if adt_on else None

    def _unpriced(day_iso: str, reason: str, **extra) -> None:
        unpriced_days.append(day_iso)
        unpriced_detail.append(
            {
                "day": day_iso,
                "reason": reason,
                "message": BED_DAY_UNPRICED_WORDS.get(reason, reason),
                **extra,
            }
        )

    day = start_d
    while day <= end_d:
        iso = day.isoformat()
        key = f"bedday:{admission_id}:{iso}"
        instant, inclusive = (_midnight_after(day), False) if adt_on else (None, False)
        if cap is not None and instant is not None and instant > cap:
            instant, inclusive = cap, True
        if by_class and key in existing_keys:
            replayed.append({"idempotency_key": key, "service_date": iso})
            day += timedelta(days=1)
            continue
        if instant is not None and _on_leave_at(presence, instant, inclusive=inclusive):
            leave_days.append(iso)
            if not leave_rent:
                day += timedelta(days=1)
                continue
        code_for_day, class_for_day, words = service_code, ward_class, f"Bed charge: {iso}"
        if by_class:
            assert instant is not None
            bed_id = _bed_at(placing, admission, instant, inclusive=inclusive)
            bed = beds.get(bed_id) if bed_id else None
            if bed is None:
                _unpriced(iso, "no_bed")
                day += timedelta(days=1)
                continue
            bed_class = _class_of(bed, classes)
            label = bed.get("bed_label")
            if bed_class is None:
                _unpriced(iso, "no_bed_class", bed=label, bed_class=bed.get("bed_class"))
                day += timedelta(days=1)
                continue
            code_for_day = _clean(bed_class.get("bed_day_service_code"))
            class_for_day = bed_class["code"]
            if not code_for_day:
                _unpriced(iso, "no_service_code", bed=label, bed_class=class_for_day)
                day += timedelta(days=1)
                continue
            words = f"Bed charge ({bed_class.get('name') or class_for_day}): {iso}"
        try:
            charge = add_charge(
                patient_id=admission["patient_id"],
                admission_id=admission_id,
                service_code=code_for_day,
                ward_class=(
                    class_for_day
                    if by_class
                    else ward_class or wards.get(day) or admission.get("ward")
                ),
                service_date=iso,
                source="bed_day",
                source_ref=f"{admission_id}:{iso}",
                idempotency_key=key,
                charged_by=actor,
                description=words,
            )
            (replayed if charge.get("replayed") else posted).append(charge)
        except BillingError as exc:
            if exc.code != "rate_not_configured":
                raise
            # A DAY-BY-DAY answer, not an all-or-nothing failure. Rates are
            # dated: a hospital that loads its price list on Tuesday has no rate
            # in force for a patient admitted on Sunday, and the useful answer is
            # "Tuesday onward is billed, Sunday and Monday are not" — with those
            # two days NAMED, so somebody prices them rather than discovering
            # the gap when the final bill is short.
            unknown = (exc.detail or {}).get("source") == "unknown_item"
            if by_class:
                _unpriced(
                    iso,
                    "no_service_item" if unknown else "rate_not_configured",
                    bed=label,
                    bed_class=class_for_day,
                    service_code=code_for_day,
                )
            else:
                _unpriced(iso, "rate_not_configured", service_code=code_for_day)
        day += timedelta(days=1)

    total = sum(int(c["total_paise"]) for c in posted)
    return {
        "admission_id": admission_id,
        "service_code": service_code or None,
        "pricing": "bed_class" if by_class else "service_code",
        "stay_start": stay_start,
        "from": start_d.isoformat(),
        "through": end_d.isoformat(),
        "posted": posted,
        "already_present": len(replayed),
        "posted_count": len(posted),
        "posted_total_paise": total,
        "posted_total_display": format_inr(total),
        "unpriced_days": unpriced_days,
        "unpriced_day_count": len(unpriced_days),
        "unpriced_detail": unpriced_detail,
        "leave_days": leave_days,
        "leave_rent_charged": leave_rent if adt_on else None,
    }


# ── Invoices ─────────────────────────────────────────────────────────────────


def _decorate_invoice(row: dict) -> dict:
    for field in (
        "subtotal_paise",
        "discount_paise",
        "taxable_value_paise",
        "cgst_paise",
        "sgst_paise",
        "igst_paise",
        "tax_paise",
        "total_paise",
        "rounding_adjustment_paise",
        "payable_paise",
        "amount_paid_paise",
        "balance_paise",
        "patient_responsibility_paise",
        "payer_responsibility_paise",
    ):
        if field in row:
            row[field.replace("_paise", "_display")] = format_inr(int(row[field] or 0))
    row["is_credit_note"] = row.get("invoice_type") == "credit_note"
    # A draft holds no number, and that is the point: a draft must not consume a
    # GST invoice number. The UI shows "Draft" rather than a blank.
    row["number_display"] = row.get("invoice_number") or "Draft, not yet numbered"
    return row


def create_invoice(
    *,
    patient_id: str,
    admission_id: str | None = None,
    invoice_type: str = "final",
    charge_ids: Sequence[int] | None = None,
    facilities: frozenset[int] | None = None,
    encounter_type: str | None = None,
    place_of_supply: str | None = None,
    interstate: bool = False,
    round_off: bool | None = None,
    issue: bool = False,
    notes: str | None = None,
    created_by: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Raise an invoice over pending charges. Draft by default.

    ``invoice_type``:
      ``interim`` — the running IPD bill a family asks for mid-stay. It is a real
                    invoice over the charges captured SO FAR; later charges get
                    their own document.
      ``final``   — the discharge bill: everything still pending.
      ``advance`` — a pro-forma/advance demand.

    ``charge_ids=None`` takes every pending charge for the encounter (the
    admission when given, otherwise the patient's outpatient charges). Claiming
    is atomic: the charges are moved to ``billed`` in the same transaction that
    writes the invoice, guarded by ``WHERE status='pending'``, so two clerks
    invoicing the same admission at the same instant cannot both bill the same
    line — the loser's transaction rolls back whole.

    ``facilities`` (multisite-9a) is the caller's permissions.facility_scope:
    a set limits that sweep to the lines stamped at one of those facilities,
    so another facility's pending lines stay pending for its own counter and
    never land on this facility's bill. Named charges are checked by the route
    first. None (one facility on record) changes nothing.

    AN UNPRICED CHARGE STOPS THE BILL, whether it was named explicitly or swept
    in: ``charge_not_priced``, with the offending charges in ``detail``. See the
    block guarding it below for why refusing beats issuing an incomplete
    document.

    ROUNDING: the payable is rounded to the rupee for a self-pay patient (the
    customary Indian invoice round-off, recorded explicitly as
    ``rounding_adjustment_paise``) and NOT rounded for a payer document, which is
    settled to the paise. Pass ``round_off`` to override.
    """
    patient_id = _clean(patient_id)
    if invoice_type not in ("interim", "final", "advance"):
        raise BillingError(
            f"invoice_type {invoice_type!r} is not raised here (credit notes go "
            "through create_credit_note)",
            code="invalid_input",
        )
    # AN OUTPATIENT BILL TAKES OUTPATIENT LINES ONLY (opd-14). A caller that
    # names the encounter 'op' (the counter's "Bill this visit", the OPD
    # counter form) gets exactly that patient's outpatient lines with no
    # admission: an inpatient line is never swept or named onto it. Callers
    # that do not name the encounter are unchanged.
    op_bill = _clean(encounter_type).lower() == "op"
    if op_bill and _clean(admission_id):
        raise BillingError(
            "an outpatient bill carries no admission; bill the stay from its admission instead",
            code="op_bill_with_admission",
        )

    with db_session() as db:
        if idempotency_key:
            existing = db.execute(
                "SELECT * FROM invoices WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                out = _decorate_invoice(_row(existing))
                out["replayed"] = True
                return out

        if not _patient_exists(db, patient_id):
            raise BillingError(f"unknown patient {patient_id!r}", code="unknown_patient")
        context = _resolve_encounter_context(db, patient_id=patient_id, admission_id=admission_id)
        payer = context["payer"]

        if charge_ids:
            placeholders = ",".join("?" * len(charge_ids))
            rows = db.execute(
                f"SELECT * FROM encounter_charges WHERE id IN ({placeholders}) ORDER BY id",
                tuple(int(c) for c in charge_ids),
            ).fetchall()
            found = {int(r["id"]) for r in rows}
            missing = [int(c) for c in charge_ids if int(c) not in found]
            if missing:
                raise BillingError(
                    f"no such charge(s): {missing}",
                    code="not_found",
                    detail={"charge_ids": missing},
                )
            wrong_patient = [int(r["id"]) for r in rows if r["patient_id"] != patient_id]
            if wrong_patient:
                raise BillingError(
                    f"charge(s) {wrong_patient} belong to another patient",
                    code="charge_patient_mismatch",
                )
            not_pending = [
                {"id": int(r["id"]), "status": r["status"]}
                for r in rows
                if r["status"] != _CHARGE_BILLABLE
            ]
            if not_pending:
                raise BillingError(
                    "some charges are not pending (already billed or cancelled)",
                    code="charge_not_billable",
                    detail={"charges": not_pending},
                )
            if op_bill:
                not_outpatient = [
                    int(r["id"])
                    for r in rows
                    if r["admission_id"] or (r["encounter_type"] or "") != "op"
                ]
                if not_outpatient:
                    raise BillingError(
                        "an outpatient bill takes outpatient lines only; "
                        f"charge(s) {not_outpatient} belong to a stay or another encounter",
                        code="charge_encounter_mismatch",
                        detail={"charge_ids": not_outpatient},
                    )
        else:
            from services.facility_documents import facility_clause

            # Only the caller's facilities' lines (multisite-9a); '' with no limit.
            facility_sql, facility_params = facility_clause("facility_id", facilities)
            if admission_id:
                rows = db.execute(
                    "SELECT * FROM encounter_charges WHERE admission_id = ? AND status = ?"
                    f"{facility_sql} ORDER BY id",
                    (admission_id, _CHARGE_BILLABLE, *facility_params),
                ).fetchall()
            elif op_bill:
                rows = db.execute(
                    "SELECT * FROM encounter_charges WHERE patient_id = ? AND status = ? "
                    f"AND admission_id IS NULL AND encounter_type = 'op'{facility_sql} "
                    "ORDER BY id",
                    (patient_id, _CHARGE_BILLABLE, *facility_params),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM encounter_charges WHERE patient_id = ? AND status = ? "
                    f"AND admission_id IS NULL{facility_sql} ORDER BY id",
                    (patient_id, _CHARGE_BILLABLE, *facility_params),
                ).fetchall()

        if not rows:
            raise BillingError(
                "there are no pending charges to invoice for this encounter",
                code="no_charges",
            )

        # AN UNPRICED CHARGE MAY NOT BECOME A ₹0.00 LINE ON A TAX INVOICE.
        #
        # `rate_is_configured = 0` is the frontdesk's deliberate record that a
        # chargeable event happened and NOBODY HAS SAID WHAT IT COSTS
        # (frontdesk_service._raise_consultation_charge: "UNPRICED IS NOT FREE").
        # Its zero is a placeholder, not a price. Sweeping it onto an invoice
        # would do two irreversible things at once: print a numbered GST line
        # reading ₹0.00 — a document that asserts the item was free, which is the
        # fabrication this whole module exists to prevent — and move the charge to
        # `billed`, which drops it out of `unpriced_charge_count` and silently
        # flips the discharge advisory from "NOT KNOWN — this bill is incomplete"
        # to "No outstanding balance recorded".
        #
        # WHY REFUSE RATHER THAN CARRY THE INCOMPLETENESS ONTO THE DOCUMENT. A
        # tax invoice has no way to say "amount unknown": every line must carry a
        # taxable value, so an issued document cannot honestly hold this fact at
        # all. Refusal, by contrast, costs nothing and is undone in seconds — the
        # charge stays `pending`, the advisory keeps saying the bill is
        # incomplete, and the clerk either prices the item (billing_masters) or
        # cancels the charge with a reason. It is also the answer `add_charge`
        # already gives to the same fact, so "Not configured" is refused
        # everywhere in this module rather than in one place and priced at zero in
        # the next.
        #
        # This cannot block care: it is a billing-counter refusal, and
        # `assert_discharge_permitted` never consults it.
        unpriced = [r for r in rows if int(r["rate_is_configured"] or 0) == 0]
        if unpriced:
            raise BillingError(
                f"{len(unpriced)} charge(s) on this encounter have no configured "
                "price, price them (or cancel them with a reason) before raising "
                "the invoice. Orb will not put an unpriced item on a bill at ₹0.00.",
                code="charge_not_priced",
                detail={
                    "charges": [
                        {
                            "id": int(r["id"]),
                            "service_code": r["service_code"],
                            "description": r["description"],
                        }
                        for r in unpriced
                    ]
                },
            )

        lines = [_charge_line(r, interstate=interstate) for r in rows]
        totals = compute_invoice(
            lines,
            round_off_to_rupee=(
                (payer.payer_type == "self_pay") if round_off is None else bool(round_off)
            ),
        )

        # Who owes what. A co-pay percentage is applied through the SAME basis
        # point helper as everything else, so a 7.5% co-pay and a 7.5% discount
        # can never disagree by a paise. Self-pay: all of it is the patient's.
        if payer.payer_type == "self_pay" or payer.payer_id is None:
            patient_share = totals.payable_paise
            payer_share = 0
        else:
            patient_share = apply_basis_points(totals.payable_paise, payer.copay_percent_bp)
            payer_share = totals.payable_paise - patient_share

        # The facility that bills (multisite-7b, migration 75): the admission's,
        # else the patient's latest encounter, the facility its supplier block
        # and number are taken for. Facility 1, with no statement run, while
        # only one facility is on record.
        facility_id = facility_identity.document_facility_id(
            db, patient_id=patient_id, admission_id=admission_id
        )
        cur = db.execute(
            """INSERT INTO invoices
                 (patient_id, admission_id, encounter_type, payer_id, tariff_plan_id,
                  invoice_type, status, place_of_supply, is_interstate,
                  subtotal_paise, discount_paise, taxable_value_paise, cgst_paise,
                  sgst_paise, igst_paise, tax_paise, total_paise,
                  rounding_adjustment_paise, payable_paise, amount_paid_paise,
                  balance_paise, patient_responsibility_paise, payer_responsibility_paise,
                  period_from, period_to, idempotency_key, notes, created_by, facility_id)
               VALUES (?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)""",
            (
                patient_id,
                admission_id,
                encounter_type or context["encounter_type"],
                payer.payer_id,
                payer.tariff_plan_id,
                invoice_type,
                place_of_supply,
                1 if interstate else 0,
                totals.subtotal_paise,
                totals.discount_paise,
                totals.taxable_value_paise,
                totals.cgst_paise,
                totals.sgst_paise,
                totals.igst_paise,
                totals.tax_paise,
                totals.total_paise,
                totals.rounding_adjustment_paise,
                totals.payable_paise,
                totals.payable_paise,  # balance = payable until money arrives
                patient_share,
                payer_share,
                rows[0]["service_date"],
                rows[-1]["service_date"],
                idempotency_key,
                notes,
                created_by,
                facility_id,
            ),
        )
        invoice_id = int(cur.lastrowid)

        for line_no, (charge, line) in enumerate(zip(rows, lines, strict=False), start=1):
            db.execute(
                """INSERT INTO invoice_lines
                     (invoice_id, line_no, encounter_charge_id, service_item_id,
                      service_code, description, hsn_sac, quantity_milli, uom,
                      unit_rate_paise, gross_paise, discount_paise, gst_treatment,
                      gst_rate_bp, taxable_value_paise, cgst_paise, sgst_paise,
                      igst_paise, tax_paise, total_paise)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    invoice_id,
                    line_no,
                    int(charge["id"]),
                    charge["service_item_id"],
                    charge["service_code"],
                    charge["description"],
                    _hsn_for(db, charge["service_code"]),
                    int(charge["quantity_milli"] or QUANTITY_SCALE),
                    _uom_for(db, charge["service_code"]),
                    int(charge["unit_rate_paise"] or 0),
                    line.gross_paise,
                    line.discount_paise,
                    line.gst_treatment,
                    line.gst_rate_bp,
                    line.taxable_value_paise,
                    line.cgst_paise,
                    line.sgst_paise,
                    line.igst_paise,
                    line.tax_paise,
                    line.total_paise,
                ),
            )

        claimed = db.execute(
            "UPDATE encounter_charges SET status = ?, invoice_id = ? "
            f"WHERE id IN ({','.join('?' * len(rows))}) AND status = ?",
            (_CHARGE_BILLED, invoice_id, *[int(r["id"]) for r in rows], _CHARGE_BILLABLE),
        ).rowcount
        if claimed != len(rows):
            # Somebody else billed one of these lines between our read and our
            # write. Raising rolls the whole session back — no invoice, no
            # half-claimed charges — and the caller retries against fresh state.
            raise BillingError(
                "these charges were billed by someone else while this invoice was "
                "being prepared; reload and try again",
                code="charges_raced",
                detail={"expected": len(rows), "claimed": claimed},
            )

        _audit(
            db,
            action_type="billing_invoice_created",
            summary=(
                f"{invoice_type} invoice {invoice_id} created over {len(rows)} charge(s), "
                f"payable {format_inr(totals.payable_paise)}"
            ),
            patient_id=patient_id,
            target_type="invoice",
            target_id=invoice_id,
            actor=created_by,
            metadata={
                "invoice_type": invoice_type,
                "admission_id": admission_id,
                "charge_ids": [int(r["id"]) for r in rows],
                "payable_paise": totals.payable_paise,
                "tax_paise": totals.tax_paise,
                "is_interstate": interstate,
            },
        )

    if issue:
        return issue_invoice(invoice_id, actor=created_by)
    return get_invoice(invoice_id)


def _hsn_for(db, service_code: str) -> str | None:
    row = db.execute("SELECT hsn_sac FROM service_items WHERE code = ?", (service_code,)).fetchone()
    return row["hsn_sac"] if row else None


def _uom_for(db, service_code: str) -> str | None:
    row = db.execute("SELECT uom FROM service_items WHERE code = ?", (service_code,)).fetchone()
    return row["uom"] if row else None


def issue_invoice(invoice_id: int, *, actor: str | None = None) -> dict:
    """Draft → issued. Allocates the GST invoice number.

    ORDER MATTERS HERE. The status is checked BEFORE a number is drawn, because
    a number drawn and then not used is a GAP in a GST series that a hospital has
    to explain to an auditor. The final UPDATE is still guarded
    (``WHERE status='draft'``) against a racing second issue; if that guard bites
    after a number was drawn, the gap is logged loudly rather than hidden.

    The number itself comes from ``billing_masters.next_document_number``, which
    resets per financial year and reports whether the site has configured its
    real series. Until it has, every screen that prints the number must say so.
    """
    with db_session() as db:
        row = db.execute("SELECT * FROM invoices WHERE id = ?", (int(invoice_id),)).fetchone()
        if row is None:
            raise BillingError(f"no invoice {invoice_id}", code="not_found")
        if row["status"] == "issued" and row["invoice_number"]:
            return get_invoice(invoice_id)
        _assert_transition(row["status"], "issued", invoice_id)
        patient_id = row["patient_id"]
        admission_id = row["admission_id"]

    allocation = billing_masters.next_document_number("INV")

    with db_session() as db:
        # The supplier block is fixed in the SAME statement that assigns the
        # number (multisite-2, migration 72): a reprint after the hospital's
        # details change still shows what the invoice was issued with.
        # It names the invoice's own facility (multisite-7b), the one it was
        # raised at, whichever facility the patient has been seen at since.
        snapshot = facility_identity.supplier_snapshot(
            db, patient_id=patient_id, admission_id=admission_id, facility_id=row["facility_id"]
        )
        updated = db.execute(
            "UPDATE invoices SET status = 'issued', invoice_number = ?, "
            "issued_at = ?, issued_by = ?, updated_at = CURRENT_TIMESTAMP, "
            + ", ".join(f"{c} = ?" for c in facility_identity.SNAPSHOT_COLUMNS)
            + " WHERE id = ? AND status = 'draft'",
            (allocation["number"], _now_iso(), actor, *snapshot, int(invoice_id)),
        ).rowcount
        if updated != 1:
            logger.error(
                "Invoice %s was not in draft when its number was applied; document "
                "number %s is now a GAP in the INV series and must be accounted for.",
                invoice_id,
                allocation["number"],
            )
            raise BillingError(
                "this invoice was issued or cancelled by someone else; reload it",
                code="invoice_raced",
                detail={"burned_number": allocation["number"]},
            )
        _audit(
            db,
            action_type="billing_invoice_issued",
            summary=f"Invoice {invoice_id} issued as {allocation['number']}",
            patient_id=patient_id,
            target_type="invoice",
            target_id=invoice_id,
            actor=actor,
            metadata={
                "invoice_number": allocation["number"],
                "financial_year": allocation["financial_year"],
                # A site that has not set its real prefix is issuing PLACEHOLDER
                # numbers. Recording that on the audit row means a later
                # reconciliation can tell which documents were provisional.
                "series_is_configured": bool(allocation["is_configured"]),
            },
        )
    return get_invoice(invoice_id)


def _assert_transition(current: str, target: str, invoice_id: int) -> None:
    allowed = _LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise BillingError(
            f"invoice {invoice_id} is {current}; it cannot become {target}"
            + (
                ", an invoice with payments against it is reversed with a credit "
                "note, never cancelled"
                if current in ("paid", "partially_paid") and target == "cancelled"
                else ""
            ),
            code="illegal_transition",
            detail={"from": current, "to": target, "allowed": sorted(allowed)},
        )


def cancel_invoice(invoice_id: int, *, reason: str, actor: str | None = None) -> dict:
    """Cancel a draft or an unpaid issued invoice, with a typed reason.

    NOTHING IS DELETED. The invoice row, its lines and its number all stay; the
    status becomes ``cancelled`` and the reason, the actor and the time are
    recorded on the row AND on the audit chain. The number is never reused — a
    reused GST invoice number is two different documents claiming to be the same
    one.

    Charges go back to ``pending`` so they can be billed again on a corrected
    invoice, which is the whole reason a clerk cancels one.

    ONCE MONEY HAS MOVED, CANCELLATION IS REFUSED. An invoice with a payment
    allocated against it is reversed with a credit note (``create_credit_note``),
    because cancelling it would leave a receipt pointing at a document that
    claims never to have existed.
    """
    cleaned = _require_reason(reason, what="Cancelling an invoice")
    state = refresh_invoice_payment_state(invoice_id)
    if state["amount_paid_paise"] != 0:
        raise BillingError(
            f"invoice {invoice_id} has {format_inr(state['amount_paid_paise'])} paid "
            "against it. Reverse it with a credit note rather than cancelling it",
            code="invoice_has_payments",
            detail={"amount_paid_paise": state["amount_paid_paise"]},
        )

    with db_session() as db:
        row = db.execute("SELECT * FROM invoices WHERE id = ?", (int(invoice_id),)).fetchone()
        if row is None:
            raise BillingError(f"no invoice {invoice_id}", code="not_found")
        if row["status"] == "cancelled":
            return get_invoice(invoice_id)
        _assert_transition(row["status"], "cancelled", invoice_id)

        updated = db.execute(
            "UPDATE invoices SET status = 'cancelled', cancelled_at = ?, "
            "cancelled_by = ?, cancel_reason = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status IN ('draft','issued')",
            (_now_iso(), actor, cleaned, int(invoice_id)),
        ).rowcount
        if updated != 1:
            raise BillingError(
                "this invoice changed while it was being cancelled; reload it",
                code="invoice_raced",
            )
        released = db.execute(
            "UPDATE encounter_charges SET status = ?, invoice_id = NULL "
            "WHERE invoice_id = ? AND status = ?",
            (_CHARGE_BILLABLE, int(invoice_id), _CHARGE_BILLED),
        ).rowcount
        _audit(
            db,
            action_type="billing_invoice_cancelled",
            summary=(
                f"Invoice {invoice_id} ({row['invoice_number'] or 'draft'}) cancelled: {cleaned}"
            ),
            patient_id=row["patient_id"],
            target_type="invoice",
            target_id=invoice_id,
            actor=actor,
            metadata={
                "reason": cleaned,
                "previous_status": row["status"],
                "invoice_number": row["invoice_number"],
                "payable_paise": int(row["payable_paise"] or 0),
                "charges_released": released,
            },
        )
    return get_invoice(invoice_id)


def _existing_credit_note(db, invoice_id: int):
    """The credit note already raised against this invoice, if there is one.

    ``supersedes_invoice_id`` is the link ``get_invoice`` already reads to list an
    invoice's credit notes, so this asks the same question the screen asks.
    """
    return db.execute(
        "SELECT id, invoice_number, payable_paise, issued_at FROM invoices "
        "WHERE supersedes_invoice_id = ? AND invoice_type = 'credit_note' "
        "ORDER BY id LIMIT 1",
        (int(invoice_id),),
    ).fetchone()


def create_credit_note(invoice_id: int, *, reason: str, actor: str | None = None) -> dict:
    """Reverse an issued invoice with a SECOND document, not an erasure.

    The credit note is a full invoice row of type ``credit_note`` whose lines are
    the original's with the sign flipped, carrying its own number from the same
    series and pointing at what it reverses (``supersedes_invoice_id``). The
    original invoice is left exactly as it was: it happened, a receipt may exist
    against it, and the patient's outstanding is the SUM of both documents —
    which is how the credit shows up as money no longer owed.

    The charges are NOT released back to pending. They were billed, and the
    billing was reversed; re-billing them is a new decision a clerk makes by
    posting fresh charges, not a side effect of a credit note.

    ONE INVOICE, ONE CREDIT NOTE. A second call is REFUSED (``already_credited``,
    with the existing note's id and number in ``detail``). This note reverses the
    invoice IN FULL, so a second one would credit the patient the whole bill
    twice and burn a second GST credit-note number for one document — a number a
    hospital then has to explain to an auditor. The check runs BEFORE
    ``next_document_number`` for exactly that reason (the ordering
    ``issue_invoice`` documents at length), and is repeated inside the write so a
    double-tapped button cannot slip between the two.
    """
    cleaned = _require_reason(reason, what="Raising a credit note")

    with db_session() as db:
        row = db.execute("SELECT * FROM invoices WHERE id = ?", (int(invoice_id),)).fetchone()
        if row is None:
            raise BillingError(f"no invoice {invoice_id}", code="not_found")
        if row["status"] in ("draft", "cancelled"):
            raise BillingError(
                f"invoice {invoice_id} is {row['status']}; a credit note reverses an "
                "ISSUED document. Cancel a draft instead",
                code="illegal_transition",
            )
        if row["invoice_type"] == "credit_note":
            raise BillingError("a credit note cannot itself be credited", code="illegal_transition")
        existing = _existing_credit_note(db, invoice_id)
        if existing is not None:
            raise BillingError(
                f"invoice {invoice_id} has already been credited in full by credit "
                f"note {existing['invoice_number'] or existing['id']} for "
                f"{format_inr(int(existing['payable_paise'] or 0))}: reload the "
                "invoice; the credit is already on it",
                code="already_credited",
                detail={
                    "credit_note_id": int(existing["id"]),
                    "credit_note_number": existing["invoice_number"],
                    "payable_paise": int(existing["payable_paise"] or 0),
                },
            )
        source = _row(row)
        lines = [
            _row(r)
            for r in db.execute(
                "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
                (int(invoice_id),),
            ).fetchall()
        ]
        # The facility whose INV series numbers the note (multisite-7a): the
        # invoice's own (multisite-7b), so the note is issued by the supplier
        # that issued the bill; its supplier snapshot and its own facility_id
        # below are that facility's too. Facility 1 while only one is on record.
        series_facility = (
            int(source["facility_id"])
            if source.get("facility_id") is not None
            else facility_identity.document_facility_id(
                db, patient_id=source["patient_id"], admission_id=source["admission_id"]
            )
        )

    if not lines:
        raise BillingError(f"invoice {invoice_id} has no lines to credit", code="no_charges")

    reversed_lines = [
        compute_line(
            unit_rate_paise=-int(ln["unit_rate_paise"] or 0),
            quantity_milli=int(ln["quantity_milli"] or QUANTITY_SCALE),
            discount_paise=abs(int(ln["discount_paise"] or 0)),
            gst_rate_bp=int(ln["gst_rate_bp"] or 0),
            gst_treatment=ln["gst_treatment"] or GST_EXEMPT,
            interstate=bool(source["is_interstate"]),
        )
        for ln in lines
    ]
    # A credit note settles to the paise against the document it reverses, so it
    # is NOT rounded to the rupee: rounding it independently would leave the pair
    # off by up to a rupee and the patient owing (or owed) an unexplained amount.
    totals = compute_invoice(reversed_lines, round_off_to_rupee=False)
    # It also reverses the SOURCE's round-off. Leaving that out credited
    # ₹5,750.50 against a ₹5,751.00 bill and left 50 paise owing on a fully
    # reversed invoice. The credit's payable is minus the source's payable,
    # exactly; the round-off line carries whatever closes the pair.
    source_payable = int(source.get("payable_paise") or 0)
    cn_payable = -source_payable
    cn_rounding = cn_payable - totals.total_paise
    if cn_rounding != -int(source.get("rounding_adjustment_paise") or 0):
        logger.warning(
            "Credit note for invoice %s: reversed lines total %s against a payable "
            "of %s; the %s paise difference is carried as round-off.",
            invoice_id,
            totals.total_paise,
            source_payable,
            cn_rounding,
        )
    try:
        allocation = billing_masters.next_document_number("INV", facility_id=series_facility)
    except billing_masters.SeriesNotSetUp as exc:
        raise BillingError(
            "This facility has no invoice number series yet, so no credit note can be "
            "numbered. An administrator sets one up in Admin, Facilities.",
            code="series_not_set_up",
            detail={"series_code": exc.series_code, "facility_id": exc.facility_id},
        ) from exc

    with db_session() as db:
        # Re-checked with the number already drawn, because the gap between the
        # check above and this write is where a double-tapped button lands. If it
        # bites, the number is a GAP in the INV series — logged loudly, exactly as
        # issue_invoice does, rather than reused on the next credit note.
        raced = _existing_credit_note(db, invoice_id)
        if raced is not None:
            logger.error(
                "Invoice %s was credited by note %s while credit note number %s was "
                "being drawn; that number is now a GAP in the INV series and must be "
                "accounted for.",
                invoice_id,
                int(raced["id"]),
                allocation["number"],
            )
            raise BillingError(
                f"invoice {invoice_id} was credited by someone else while this "
                "credit note was being raised; reload it",
                code="already_credited",
                detail={
                    "credit_note_id": int(raced["id"]),
                    "credit_note_number": raced["invoice_number"],
                    "burned_number": allocation["number"],
                },
            )
        # A credit note is numbered here, so its supplier block is fixed here
        # too (multisite-2, migration 72), from the identity on record now.
        snapshot = facility_identity.supplier_snapshot(
            db,
            patient_id=source["patient_id"],
            admission_id=source["admission_id"],
            facility_id=series_facility,
        )
        cur = db.execute(
            """INSERT INTO invoices
                 (invoice_number, patient_id, admission_id, encounter_type, payer_id,
                  tariff_plan_id, invoice_type, status, place_of_supply, is_interstate,
                  subtotal_paise, discount_paise, taxable_value_paise, cgst_paise,
                  sgst_paise, igst_paise, tax_paise, total_paise,
                  rounding_adjustment_paise, payable_paise, amount_paid_paise,
                  balance_paise, supersedes_invoice_id, issued_at, issued_by,
                  notes, created_by, facility_id, """
            + facility_identity.SNAPSHOT_COLUMNS_SQL
            + """)
               VALUES (?,?,?,?,?,?,'credit_note','issued',?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,"""
            + facility_identity.SNAPSHOT_PLACEHOLDERS
            + ")",
            (
                allocation["number"],
                source["patient_id"],
                source["admission_id"],
                source["encounter_type"],
                source["payer_id"],
                source["tariff_plan_id"],
                source["place_of_supply"],
                source["is_interstate"],
                totals.subtotal_paise,
                totals.discount_paise,
                totals.taxable_value_paise,
                totals.cgst_paise,
                totals.sgst_paise,
                totals.igst_paise,
                totals.tax_paise,
                totals.total_paise,
                cn_rounding,
                cn_payable,
                cn_payable,
                int(invoice_id),
                _now_iso(),
                actor,
                f"Credit note against invoice {source['invoice_number'] or invoice_id}: {cleaned}",
                actor,
                series_facility,
                *snapshot,
            ),
        )
        note_id = int(cur.lastrowid)
        for line_no, (src, line) in enumerate(zip(lines, reversed_lines, strict=False), start=1):
            db.execute(
                """INSERT INTO invoice_lines
                     (invoice_id, line_no, encounter_charge_id, service_item_id,
                      service_code, description, hsn_sac, quantity_milli, uom,
                      unit_rate_paise, gross_paise, discount_paise, gst_treatment,
                      gst_rate_bp, taxable_value_paise, cgst_paise, sgst_paise,
                      igst_paise, tax_paise, total_paise)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    note_id,
                    line_no,
                    src["encounter_charge_id"],
                    src["service_item_id"],
                    src["service_code"],
                    f"Credit: {src['description']}",
                    src["hsn_sac"],
                    int(src["quantity_milli"] or QUANTITY_SCALE),
                    src["uom"],
                    -int(src["unit_rate_paise"] or 0),
                    line.gross_paise,
                    line.discount_paise,
                    line.gst_treatment,
                    line.gst_rate_bp,
                    line.taxable_value_paise,
                    line.cgst_paise,
                    line.sgst_paise,
                    line.igst_paise,
                    line.tax_paise,
                    line.total_paise,
                ),
            )
        _audit(
            db,
            action_type="billing_credit_note_raised",
            summary=(
                f"Credit note {allocation['number']} raised against invoice "
                f"{source['invoice_number'] or invoice_id} for "
                f"{format_inr(cn_payable)}: {cleaned}"
            ),
            patient_id=source["patient_id"],
            target_type="invoice",
            target_id=note_id,
            actor=actor,
            metadata={
                "reason": cleaned,
                "supersedes_invoice_id": int(invoice_id),
                "credit_note_number": allocation["number"],
                "payable_paise": cn_payable,
            },
        )
    return get_invoice(note_id)


def refresh_invoice_payment_state(invoice_id: int) -> dict:
    """Recompute what has been paid against an invoice, from the payment records.

    PAID IS DERIVED, NEVER ASSERTED. Two shapes are counted, because the payments
    module may legitimately write either:

      1. ``payment_allocations`` rows that are not reversed — the canonical form,
         and the only one that can split one payment across several invoices.
      2. a ``payments`` row attached straight to this invoice with NO allocation
         rows — the one-payment-one-invoice shape at a cash counter.

    Counting both, and excluding (2) whenever (1) exists for that payment, is
    what stops a receipt being counted twice AND stops it being counted zero
    times. Cancelled/bounced/reversed payments count for nothing; a ``direction
    = 'out'`` payment (a refund at the counter) reduces what was paid.

    Called on every read of an invoice, so the screen agrees with the ledger even
    if the payments module never calls back into billing.
    """
    with db_session() as db:
        row = db.execute("SELECT * FROM invoices WHERE id = ?", (int(invoice_id),)).fetchone()
        if row is None:
            raise BillingError(f"no invoice {invoice_id}", code="not_found")

        allocated = db.execute(
            """SELECT COALESCE(SUM(a.amount_paise), 0) AS total
                 FROM payment_allocations a
                 JOIN payments p ON p.id = a.payment_id
                WHERE a.invoice_id = ? AND a.reversed_at IS NULL
                  AND p.status IN ('received','cleared')""",
            (int(invoice_id),),
        ).fetchone()["total"]

        # A reversal's 'out' leg is left out: the receipt it reverses already
        # leaves the live set (status 'reversed', allocations reversed), so
        # counting the leg as well took the money off twice (-₹5,000 paid).
        direct = db.execute(
            """SELECT COALESCE(SUM(CASE WHEN p.direction = 'out'
                                        THEN -p.amount_paise ELSE p.amount_paise END), 0) AS total
                 FROM payments p
                WHERE p.invoice_id = ? AND p.status IN ('received','cleared')
                  AND p.reversal_of_payment_id IS NULL
                  AND NOT EXISTS (SELECT 1 FROM payment_allocations a
                                   WHERE a.payment_id = p.id AND a.reversed_at IS NULL)""",
            (int(invoice_id),),
        ).fetchone()["total"]

        paid = int(allocated or 0) + int(direct or 0)
        payable = int(row["payable_paise"] or 0)
        balance = payable - paid

        status = row["status"]
        # Payment only moves an invoice that is live. A cancelled or draft
        # invoice is not made "paid" by money arriving against it — that is a
        # misallocation for a human to fix, not a state change to infer.
        if status in ("issued", "partially_paid", "paid") and payable > 0:
            if paid <= 0:
                status = "issued"
            elif paid >= payable:
                status = "paid"
            else:
                status = "partially_paid"

        if (
            paid != int(row["amount_paid_paise"] or 0)
            or balance != int(row["balance_paise"] or 0)
            or status != row["status"]
        ):
            db.execute(
                "UPDATE invoices SET amount_paid_paise = ?, balance_paise = ?, "
                "status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (paid, balance, status, int(invoice_id)),
            )
        fresh = db.execute("SELECT * FROM invoices WHERE id = ?", (int(invoice_id),)).fetchone()
    return _decorate_invoice(_row(fresh))


def get_invoice(invoice_id: int) -> dict:
    """One invoice, its lines, its patient and what has been paid against it."""
    header = refresh_invoice_payment_state(invoice_id)
    with db_session() as db:
        lines = [
            _row(r)
            for r in db.execute(
                "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
                (int(invoice_id),),
            ).fetchall()
        ]
        patient = db.execute(
            "SELECT id, name, mrn, age, sex, ward, bed FROM patients WHERE id = ?",
            (header["patient_id"],),
        ).fetchone()
        payer = None
        if header.get("payer_id"):
            payer = db.execute(
                "SELECT id, code, name, payer_type, gstin FROM payers WHERE id = ?",
                (int(header["payer_id"]),),
            ).fetchone()
        payments = [
            _row(r)
            for r in db.execute(
                """SELECT p.id, p.receipt_number, p.amount_paise, p.direction, p.method,
                      p.status, p.received_at
                 FROM payments p
                WHERE p.invoice_id = ?
                   OR p.id IN (SELECT payment_id FROM payment_allocations
                                WHERE invoice_id = ? AND reversed_at IS NULL)
                ORDER BY p.id""",
                (int(invoice_id), int(invoice_id)),
            ).fetchall()
        ]
        credit_notes = [
            _row(r)
            for r in db.execute(
                "SELECT id, invoice_number, payable_paise, issued_at FROM invoices "
                "WHERE supersedes_invoice_id = ? ORDER BY id",
                (int(invoice_id),),
            ).fetchall()
        ]

    for line in lines:
        for field in (
            "gross_paise",
            "discount_paise",
            "taxable_value_paise",
            "cgst_paise",
            "sgst_paise",
            "igst_paise",
            "tax_paise",
            "total_paise",
            "unit_rate_paise",
        ):
            line[field.replace("_paise", "_display")] = format_inr(int(line[field] or 0))
        line["quantity"] = int(line["quantity_milli"] or 0) / QUANTITY_SCALE
    for payment in payments:
        payment["amount_display"] = format_inr(int(payment["amount_paise"] or 0))
    for note in credit_notes:
        note["payable_display"] = format_inr(int(note["payable_paise"] or 0))

    header["lines"] = lines
    header["patient"] = _row(patient) if patient is not None else None
    header["payer"] = _row(payer) if payer is not None else None
    header["payments"] = payments
    # The hospital's name heads the bill on screen as well as in print. None
    # when the site has not configured one: the screen says so, never invents.
    # A numbered bill names the supplier it was issued by (its snapshot, or
    # the site name for one issued before migration 72); a draft names the
    # facility's identity as it stands (multisite-2).
    supplier = facility_identity.supplier_block(header, number_field="invoice_number")
    header["supplier_name"] = supplier["name"] if supplier["name_is_configured"] else None
    header["credit_notes"] = credit_notes
    header["allowed_transitions"] = sorted(_LEGAL_TRANSITIONS.get(header["status"], frozenset()))
    return header


def list_invoices(
    *,
    patient_id: str | None = None,
    admission_id: str | None = None,
    status: str | None = None,
    invoice_type: str | None = None,
    limit: int = 200,
    facilities: frozenset[int] | None = None,
) -> list[dict]:
    """Invoices, newest first. `facilities` (multisite-9a, document-keyed) is
    the caller's permissions.facility_scope: a set keeps the invoices whose
    own facility is one of them, in the statement, so the LIMIT never hides
    one; None (one facility on record) changes nothing."""
    from services.facility_documents import facility_clause

    sql = (
        "SELECT i.*, p.name AS patient_name, p.mrn AS patient_mrn "
        "FROM invoices i LEFT JOIN patients p ON p.id = i.patient_id WHERE 1=1"
    )
    params: list[Any] = []
    if patient_id:
        sql += " AND i.patient_id = ?"
        params.append(patient_id)
    if admission_id:
        sql += " AND i.admission_id = ?"
        params.append(admission_id)
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if invoice_type:
        sql += " AND i.invoice_type = ?"
        params.append(invoice_type)
    facility_sql, facility_params = facility_clause("i.facility_id", facilities)
    sql += facility_sql
    params.extend(facility_params)
    sql += " ORDER BY i.id DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]
    return [_decorate_invoice(r) for r in rows]


# ── The printable document ───────────────────────────────────────────────────

_ONES = (
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
)
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _words_below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")).strip()


def _words_below_thousand(n: int) -> str:
    if n < 100:
        return _words_below_hundred(n)
    head = f"{_ONES[n // 100]} Hundred"
    rest = n % 100
    return f"{head} {_words_below_hundred(rest)}".strip() if rest else head


def amount_in_words(paise: int) -> str:
    """'₹1,23,456.78' → 'Rupees One Lakh Twenty Three Thousand Four Hundred Fifty
    Six and Seventy Eight Paise Only'.

    Indian grouping (crore / lakh / thousand), which is what an Indian invoice
    prints and what a cashier checks the figures against. Integer arithmetic
    throughout — this is a rendering of the stored paise, never a re-derivation.
    """
    p = assert_paise(paise)
    negative = p < 0
    p = abs(p)
    rupees_part, paise_part = divmod(p, 100)

    if rupees_part == 0:
        words = "Zero"
    else:
        chunks: list[str] = []
        crore, rest = divmod(rupees_part, 10_000_000)
        lakh, rest = divmod(rest, 100_000)
        thousand, hundreds = divmod(rest, 1000)
        if crore:
            chunks.append(f"{_words_below_thousand(crore)} Crore")
        if lakh:
            chunks.append(f"{_words_below_hundred(lakh)} Lakh")
        if thousand:
            chunks.append(f"{_words_below_hundred(thousand)} Thousand")
        if hundreds:
            chunks.append(_words_below_thousand(hundreds))
        words = " ".join(chunks)

    out = f"Rupees {words}"
    if paise_part:
        out += f" and {_words_below_hundred(paise_part)} Paise"
    out += " Only"
    return ("Minus " + out) if negative else out


def invoice_print_view(invoice_id: int) -> dict:
    """Everything a printed / exported invoice needs, and nothing it must invent.

    THE SUPPLIER BLOCK IS HONEST ABOUT WHAT IT DOES NOT HAVE. A GST tax invoice
    must carry the supplier's legal name, address and GSTIN. Whatever of these
    is not configured prints as NOT CONFIGURED, and the notice says the document
    is a bill, not a GST tax invoice. A printed document that shows a blank
    where a GSTIN belongs is a document somebody notices; one that shows a
    plausible placeholder is one they file.

    AN ISSUED INVOICE NEVER CHANGES (multisite-2). The block comes from
    facility_identity.supplier_block: a numbered invoice prints the snapshot
    taken when its number was assigned (migration 72), one numbered before
    that migration prints the old block (site name, address and GSTIN not
    configured) exactly as it always did, and only a draft prints the
    facility's identity as it stands now.
    """
    invoice = get_invoice(invoice_id)
    supplier = facility_identity.supplier_block(invoice, number_field="invoice_number")

    # The INV series of the facility the bill names as its supplier
    # (multisite-7a); facility 1 for a bill numbered before migration 72.
    with db_session() as db:
        series_configured = billing_masters.series_is_configured(
            db,
            "INV",
            facility_id=int(supplier.get("facility_id") or billing_masters.MAIN_FACILITY_ID),
        )

    tax_lines = [
        left_item for left_item in invoice["lines"] if int(left_item["tax_paise"] or 0) != 0
    ]
    exempt_lines = [
        left_item for left_item in invoice["lines"] if left_item["gst_treatment"] != "taxable"
    ]

    return {
        "invoice": invoice,
        # name, name_is_configured, address, gstin, state_code, facility_id,
        # source ('snapshot', 'legacy' or 'live'), snapshot_at, and notice:
        # said in words on the document itself, not buried in a tooltip, while
        # the GSTIN or address is missing (a tax invoice without a GSTIN is not
        # a tax invoice); None once both are on the document.
        "supplier": supplier,
        "number_series_configured": series_configured,
        "number_notice": (
            None
            if series_configured
            else "Document numbers come from a placeholder series. Set the hospital's "
            "real invoice series before treating these numbers as final."
        ),
        "amount_in_words": amount_in_words(int(invoice["payable_paise"] or 0)),
        "has_tax": bool(tax_lines),
        "exempt_line_count": len(exempt_lines),
        "gst_note": (
            "Healthcare services supplied by a clinical establishment are exempt "
            "from GST (Notification 12/2017-CT(R), entry 74). Tax shown, where "
            "any, is on pharmacy, consumables and other taxable supplies."
        ),
        "printed_at": _now_iso(),
    }


# ── Patient money view + the discharge advisory ──────────────────────────────


def _refresh_patient_invoices(patient_id: str) -> None:
    """Re-derive payment state for every live invoice of one patient.

    WHY THIS EXISTS. `invoices.amount_paid_paise` is a CACHE of what the payment
    records say, and a cache nobody refreshes is a lie with a timestamp. The
    payments module writes a receipt and an allocation; if billing only
    recomputed on `get_invoice`, a patient who had just paid in full would still
    read as "₹5,000 outstanding" on the discharge advisory — which is precisely
    the false warning that gets an advisory ignored, and then gets it enforced.
    Caught by test_a_clear_patient_reads_as_clear on the first run.
    """
    with db_session() as db:
        ids = [
            int(r["id"])
            for r in db.execute(
                "SELECT id FROM invoices WHERE patient_id = ? AND status IN "
                "('issued','partially_paid','paid')",
                (patient_id,),
            ).fetchall()
        ]
    for invoice_id in ids:
        try:
            refresh_invoice_payment_state(invoice_id)
        except BillingError:  # pragma: no cover - the row was deleted mid-loop
            continue


def patient_financial_summary(patient_id: str, *, admission_id: str | None = None) -> dict:
    """What this patient owes, what is not yet billed, and what is unknowable.

    ``outstanding_paise`` is the SUM of the balances of live invoices — issued
    and partially paid — INCLUDING credit notes, whose balances are negative and
    therefore reduce it. Cancelled and draft invoices are not money owed.

    ``unbilled_*`` is charge capture that has not reached an invoice yet: real
    money the patient will owe, deliberately reported SEPARATELY from the
    outstanding rather than added into it, because one is a demand that exists
    and the other is a demand that does not exist yet.

    ``unpriced_charge_count`` is the number of those pending charges that carry
    ``rate_is_configured = 0`` — captured, real, and worth an amount nobody has
    entered (frontdesk_service._raise_consultation_charge: "UNPRICED IS NOT
    FREE"). Such a charge contributes 0 to ``unbilled_total_paise`` and to
    ``net_due_paise``, so while this count is non-zero those figures are floors,
    not facts, and no caller may present them as the whole position.
    """
    patient_id = _clean(patient_id)
    _refresh_patient_invoices(patient_id)
    with db_session() as db:
        live = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(balance_paise), 0) AS bal "
            "FROM invoices WHERE patient_id = ? AND status IN ('issued','partially_paid')"
            + (" AND (admission_id = ? OR admission_id IS NULL)" if admission_id else ""),
            ((patient_id, admission_id) if admission_id else (patient_id,)),
        ).fetchone()
        drafts = db.execute(
            "SELECT COUNT(*) AS n FROM invoices WHERE patient_id = ? AND status = 'draft'",
            (patient_id,),
        ).fetchone()["n"]
        # A stay's view still counts the patient's charges with NO admission
        # (the OPD consultation that led to the stay, desk charges posted before
        # charges carried the stay), as invoices, deposits and claims do here.
        # Counting the stay's charges alone told the discharge advisory those
        # were not owed.
        unbilled = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total_paise), 0) AS total, "
            "COALESCE(SUM(CASE WHEN rate_is_configured = 0 THEN 1 ELSE 0 END), 0) "
            "AS unpriced "
            "FROM encounter_charges WHERE patient_id = ? AND status = ?"
            + (" AND (admission_id = ? OR admission_id IS NULL)" if admission_id else ""),
            (
                (patient_id, _CHARGE_BILLABLE, admission_id)
                if admission_id
                else (patient_id, _CHARGE_BILLABLE)
            ),
        ).fetchone()
        deposit = db.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS bal FROM patient_deposits "
            "WHERE patient_id = ?"
            + (" AND (admission_id = ? OR admission_id IS NULL)" if admission_id else ""),
            ((patient_id, admission_id) if admission_id else (patient_id,)),
        ).fetchone()["bal"]
        open_claims = 0
        if _table_exists(db, "claims"):
            open_claims = db.execute(
                "SELECT COUNT(*) AS n FROM claims WHERE patient_id = ? AND status NOT IN "
                "('settled','closed','cancelled','denied')"
                + (" AND (admission_id = ? OR admission_id IS NULL)" if admission_id else ""),
                ((patient_id, admission_id) if admission_id else (patient_id,)),
            ).fetchone()["n"]

    outstanding = int(live["bal"] or 0)
    unbilled_total = int(unbilled["total"] or 0)
    deposit_balance = int(deposit or 0)
    return {
        "patient_id": patient_id,
        "admission_id": admission_id,
        "live_invoice_count": int(live["n"] or 0),
        "draft_invoice_count": int(drafts or 0),
        "outstanding_paise": outstanding,
        "outstanding_display": format_inr(outstanding),
        "unbilled_charge_count": int(unbilled["n"] or 0),
        "unbilled_total_paise": unbilled_total,
        "unbilled_total_display": format_inr(unbilled_total),
        "unpriced_charge_count": int(unbilled["unpriced"] or 0),
        "deposit_balance_paise": deposit_balance,
        "deposit_balance_display": format_inr(deposit_balance),
        "open_claim_count": int(open_claims or 0),
        # What the patient would owe if every pending charge were billed and the
        # advance on hand were applied. One number, all three inputs visible
        # beside it, so nobody has to guess how it was reached.
        "net_due_paise": outstanding + unbilled_total - deposit_balance,
        "net_due_display": format_inr(outstanding + unbilled_total - deposit_balance),
    }


def discharge_financial_advisory(
    *,
    patient_id: str,
    admission_id: str | None = None,
    checked_by: str | None = None,
    record: bool = True,
) -> dict:
    """What the money side knows at discharge. ADVISORY. It cannot stop anything.

    This function NEVER raises for a financial reason and never returns a
    refusal. It returns a picture and records it in ``discharge_financial_checks``
    so that, months later, a site can answer "what did the system say when this
    patient left?" — which is the only thing a shadow-mode pilot is entitled to
    ask of a financial rule.

    THE THREE ANSWERS, and why the third one exists:

      ``clear``       — nothing is owed that Orb can see.
      ``outstanding`` — a positive net due; the amount is in the payload.
      ``unknown``     — Orb CANNOT compute the position: there are clinical
                        events it could not price (no service item, no tariff),
                        captured charges that carry no configured price and so
                        sit in the totals as a 0 that is not a price, or no
                        tariff plan at all. Reporting ₹0 here would tell a
                        clerk the bill is settled when in fact the bill was never
                        computable. "Not configured" is not zero, and neither is
                        "not knowable".

    ``blocks_discharge`` is included so a caller does not have to re-derive the
    rule, and with the enforcement flag off (the default, and the pilot setting)
    it is ALWAYS False — see ``assert_discharge_permitted``.
    """
    summary = patient_financial_summary(patient_id, admission_id=admission_id)

    unpriceable = 0
    capture_error: str | None = None
    try:
        dry = capture_clinical_charges(
            patient_id=patient_id, admission_id=admission_id, dry_run=True
        )
        unpriceable = int(dry["unpriceable_count"])
        uncaptured = int(dry["posted_count"])
    except Exception as exc:  # noqa: BLE001 — see below
        # BROAD ON PURPOSE. This runs on a discharge screen. An unknown patient,
        # a half-configured master, a Helix table that does not exist on this
        # deployment — none of them may take out the discharge path or turn a
        # warning into a failure. Whatever went wrong, the honest answer is
        # 'unknown', which is exactly what it is.
        capture_error = str(exc)
        unpriceable, uncaptured = 0, 0
        logger.warning(
            "Discharge advisory could not price this encounter for %s",
            patient_id,
            exc_info=True,
        )

    with db_session() as db:
        has_plan = (
            db.execute("SELECT 1 FROM tariff_plans WHERE is_active = 1 LIMIT 1").fetchone()
            is not None
        )

    net_due = summary["net_due_paise"]
    # A captured-but-unpriced charge forces 'unknown' for the same reason an
    # unpriceable event does: it sits in every total as a 0 that is not a
    # price, so the net due is missing a term nobody has entered. The two are
    # different facts — ``unpriceable_event_count`` is clinical events the
    # capture pass could NOT turn into charges; ``unpriced_charge_count`` is
    # charges that DID land (the frontdesk writes them deliberately — "UNPRICED
    # IS NOT FREE") and are waiting for the billing counter to price them.
    if capture_error or unpriceable > 0 or not has_plan or summary["unpriced_charge_count"] > 0:
        status = "unknown"
    elif net_due > 0:
        status = "outstanding"
    else:
        status = "clear"

    enforced = bool(getattr(config, "DISCHARGE_FINANCIAL_ENFORCE", False))
    # ENFORCEMENT NEVER APPLIES TO 'unknown'. Even a site that has deliberately
    # turned on financial clearance must not hold a patient on a number the
    # system does not have.
    blocks = enforced and status == "outstanding"

    reasons: list[str] = []
    if capture_error:
        reasons.append(f"the charge capture could not run: {capture_error}")
    if not has_plan:
        reasons.append("no active tariff plan is configured, so nothing can be priced")
    if unpriceable:
        reasons.append(
            f"{_count(unpriceable, 'clinical event')} could not be priced and "
            f"{'is' if unpriceable == 1 else 'are'} missing from this bill"
        )
    # Unpriced pending charges contribute 0 to ``unbilled_total_paise``, so the
    # ₹-figure below only ever describes the PRICED ones. Say each thing about
    # the charges it is true of: a "worth ₹0.00" line that is really "worth
    # nobody-has-said" reads as "this costs nothing" at a discharge counter.
    priced_unbilled = summary["unbilled_charge_count"] - summary["unpriced_charge_count"]
    if priced_unbilled:
        reasons.append(
            f"{_count(priced_unbilled, 'captured charge')} worth "
            f"{summary['unbilled_total_display']} "
            f"{'is' if priced_unbilled == 1 else 'are'} not on an invoice yet"
        )
    if summary["unpriced_charge_count"]:
        n_unpriced = summary["unpriced_charge_count"]
        reasons.append(
            f"{_count(n_unpriced, 'captured charge')} "
            f"{'has' if n_unpriced == 1 else 'have'} no configured price. "
            f"{'Its' if n_unpriced == 1 else 'Their'} amount is unknown and is in "
            "none of these figures"
        )
    if summary["outstanding_paise"] > 0:
        reasons.append(f"{summary['outstanding_display']} is outstanding on issued invoices")
    # Money the hospital owes back (a credit note on a paid bill, an advance
    # larger than the bill). "No outstanding balance" said nothing about it,
    # and the patient left without the refund being raised.
    if status == "clear" and net_due < 0:
        reasons.append(
            f"{format_inr(-net_due)} is due back to the patient: raise the refund before they leave"
        )
    if summary["open_claim_count"]:
        n_claims = summary["open_claim_count"]
        reasons.append(
            f"{_count(n_claims, 'insurance claim')} {'is' if n_claims == 1 else 'are'} still open"
        )

    advisory = {
        **summary,
        "advisory_status": status,
        "unpriceable_event_count": unpriceable,
        "uncaptured_event_count": uncaptured,
        "reasons": reasons,
        "enforced": enforced,
        "blocks_discharge": blocks,
        "headline": _advisory_headline(status, summary, blocks),
        "message": (
            "Advisory only: this does not prevent the discharge."
            if not blocks
            else "Financial clearance is ENFORCED at this deployment; an override is required."
        ),
        "checked_at": _now_iso(),
        "checked_by": checked_by,
    }

    if record:
        _record_discharge_check(advisory, checked_by=checked_by)
    return advisory


def _advisory_headline(status: str, summary: dict, blocks: bool) -> str:
    if status == "unknown":
        return "Financial position: NOT KNOWN (this bill is incomplete)"
    if status == "outstanding":
        return f"Outstanding: {format_inr(summary['net_due_paise'])}"
    if summary["net_due_paise"] < 0:
        return f"Refund due to the patient: {format_inr(-summary['net_due_paise'])}"
    return "No outstanding balance recorded"


def _record_discharge_check(advisory: dict, *, checked_by: str | None) -> None:
    """Write the advisory into discharge_financial_checks. Never raises upward.

    ``discharge_proceeded`` is written as 1 and ``blocked_discharge`` as 0 unless
    enforcement is on AND the check blocked, because with the flag off the
    discharge is not this module's decision to record any other way. A failure to
    write this row is logged and swallowed: the record of a warning must never
    become the reason a discharge fails.
    """
    try:
        with db_session() as db:
            db.execute(
                """INSERT INTO discharge_financial_checks
                     (patient_id, admission_id, checked_at, checked_by,
                      outstanding_paise, deposit_balance_paise, unbilled_charge_count,
                      open_claim_count, advisory_status, enforced, blocked_discharge,
                      discharge_proceeded, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    advisory["patient_id"],
                    advisory["admission_id"],
                    _now_iso(),
                    checked_by,
                    # 'unknown' stores NULL rather than 0: the column must not
                    # claim a figure the check could not compute.
                    None if advisory["advisory_status"] == "unknown" else advisory["net_due_paise"],
                    advisory["deposit_balance_paise"],
                    advisory["unbilled_charge_count"],
                    advisory["open_claim_count"],
                    advisory["advisory_status"],
                    1 if advisory["enforced"] else 0,
                    1 if advisory["blocks_discharge"] else 0,
                    0 if advisory["blocks_discharge"] else 1,
                    "; ".join(advisory["reasons"])[:500] or None,
                ),
            )
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "Could not record the discharge financial check for %s; the advisory "
            "still stands and the discharge is unaffected.",
            advisory.get("patient_id"),
            exc_info=True,
        )


def assert_discharge_permitted(
    *,
    patient_id: str,
    admission_id: str | None = None,
    checked_by: str | None = None,
    override_reason: str | None = None,
) -> dict:
    """The hook a discharge path may call. WITH THE FLAG OFF IT NEVER REFUSES.

    ─────────────────────────────────────────────────────────────────────────
    WHY THIS IS ADVISORY BY DEFAULT, AND WHY THE DEFAULT MUST NOT CHANGE
    ─────────────────────────────────────────────────────────────────────────
    A patient's discharge must never wait on an unpaid bill.

      1. CLINICAL. A discharge decision is a clinical decision. A patient who is
         medically ready to leave and is held on a ward by a software rule is
         occupying a bed somebody sicker needs and is accumulating the risks of
         being in a hospital. In India, holding a patient over a bill is also
         unlawful.
      2. PILOT SCOPE. The shadow pilot does NOT bill through Orb — the incumbent
         system still runs the money — so Orb's idea of an outstanding balance is
         definitionally incomplete. Enforcing on an incomplete figure would block
         discharges over bills that were already paid somewhere Orb cannot see.

    So: ``config.DISCHARGE_FINANCIAL_ENFORCE`` defaults OFF, this function
    returns the advisory and nothing else, and the caller displays a warning. The
    flag exists at all so a POST-pilot site that has moved its billing into Orb
    can turn on a soft stop DELIBERATELY, with an override path, rather than
    somebody adding un-flagged enforcement later.

    Even with the flag ON, an ``unknown`` position never blocks, and a typed
    ``override_reason`` always clears the stop — recorded against the actor.
    """
    advisory = discharge_financial_advisory(
        patient_id=patient_id,
        admission_id=admission_id,
        checked_by=checked_by,
    )
    if not advisory["blocks_discharge"]:
        return advisory
    if _clean(override_reason):
        advisory["overridden"] = True
        advisory["override_reason"] = _clean(override_reason)[:500]
        advisory["blocks_discharge"] = False
        try:
            with db_session() as db:
                db.execute(
                    "UPDATE discharge_financial_checks SET overridden_by = ?, "
                    "override_reason = ?, blocked_discharge = 0, discharge_proceeded = 1 "
                    "WHERE id = (SELECT MAX(id) FROM discharge_financial_checks "
                    "WHERE patient_id = ?)",
                    (checked_by, advisory["override_reason"], patient_id),
                )
        except Exception:  # pragma: no cover - defensive
            logger.warning("Could not record a discharge financial override", exc_info=True)
        return advisory
    raise DischargeFinanciallyBlocked(advisory)
