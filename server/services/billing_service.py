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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Sequence

import config
from database import db_session
from services import billing_masters
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

    def __init__(self, message: str, *, code: str = "billing_error", detail: Optional[dict] = None):
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
    "paid": frozenset({"partially_paid", "issued"}),   # a reversed payment walks back
    "cancelled": frozenset(),                           # terminal. Use a credit note.
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
    """"1 captured charge", "3 captured charges": a real plural, never "(s)"."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _table_exists(db, name: str) -> bool:
    """Migration-20 tables always exist; Helix's do not on every deployment.

    A missing optional table must degrade to "no chargeable events found", never
    to an exception that takes out a billing screen.
    """
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _clean(text: Optional[str]) -> str:
    return (text or "").strip()


def _require_reason(reason: Optional[str], *, what: str) -> str:
    """A cancellation with no reason is an erasure with a UI on it."""
    cleaned = _clean(reason)
    if len(cleaned) < 3:
        raise BillingError(
            f"{what} requires a typed reason (at least 3 characters), an issued "
            "financial document is never removed silently",
            code="reason_required",
        )
    return cleaned[:500]


def _iso_day(value: Optional[str | date | datetime]) -> str:
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


def _local_day_of_stored(value: Any) -> Optional[str]:
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(hospital_timezone()).date().isoformat()


def _patient_exists(db, patient_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM patients WHERE id = ?", (patient_id,)
    ).fetchone() is not None


def _audit(
    db,
    *,
    action_type: str,
    summary: str,
    patient_id: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    actor: Optional[str] = None,
    metadata: Optional[dict] = None,
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
    effective_from: Optional[str] = None,
    effective_to: Optional[str] = None,
    notes: Optional[str] = None,
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
                code, _clean(name), plan_type, effective_from, effective_to,
                1 if is_default else 0, 1 if is_active else 0, notes,
            ),
        )
        if is_default:
            db.execute(
                "UPDATE tariff_plans SET is_default = 0 WHERE code <> ?", (code,)
            )
        row = db.execute("SELECT * FROM tariff_plans WHERE code = ?", (code,)).fetchone()
    return _row(row)


def list_tariff_rates(
    *, tariff_plan_id: Optional[int] = None, service_code: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """The priced rows of a plan, newest effective date first, with display strings."""
    sql = ("SELECT r.*, s.name AS service_name, s.category AS service_category "
           "FROM tariff_rates r LEFT JOIN service_items s ON s.code = r.service_code "
           "WHERE r.is_active = 1")
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


def list_package_rates(*, tariff_plan_id: Optional[int] = None, limit: int = 200) -> list[dict]:
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
    ward_class: Optional[str] = None,
    los_days_included: Optional[int] = None,
    inclusions: Optional[str] = None,
    exclusions: Optional[str] = None,
    external_scheme_code: Optional[str] = None,
    effective_from: Optional[str] = None,
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
                int(tariff_plan_id), code, _clean(name), ward_class, rate,
                los_days_included, inclusions, exclusions, external_scheme_code, eff,
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


def _resolve_encounter_context(
    db, *, patient_id: str, admission_id: Optional[str]
) -> dict:
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
    description: Optional[str] = None,
    quantity_milli: int = QUANTITY_SCALE,
    unit_rate_paise: Optional[int] = None,
    discount_paise: int = 0,
    discount_bp: int = 0,
    admission_id: Optional[str] = None,
    encounter_type: Optional[str] = None,
    ward_class: Optional[str] = None,
    tariff_plan_id: Optional[int] = None,
    gst_treatment: Optional[str] = None,
    gst_rate_bp: Optional[int] = None,
    service_date: Optional[str] = None,
    source: str = "manual",
    source_ref: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    notes: Optional[str] = None,
    charged_by: Optional[str] = None,
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
            raise BillingError(
                f"unknown patient {patient_id!r}", code="unknown_patient"
            )
        context = _resolve_encounter_context(
            db, patient_id=patient_id, admission_id=admission_id
        )
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
            raise BillingError(
                f"unknown GST treatment {treatment!r}", code="invalid_input"
            )
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
                  idempotency_key, notes)
               VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                patient_id, admission_id,
                encounter_type or context["encounter_type"],
                resolution.service_item_id, service_code, text, qty, rate,
                plan_id, ward_class,
                line.gross_paise, line.discount_paise, line.gst_treatment,
                line.gst_rate_bp, line.taxable_value_paise, line.cgst_paise,
                line.sgst_paise, line.igst_paise, line.total_paise,
                _CHARGE_BILLABLE, source, source_ref, day, charged_by,
                idempotency_key, notes,
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
        row = db.execute(
            "SELECT * FROM encounter_charges WHERE id = ?", (charge_id,)
        ).fetchone()

    out = _decorate_charge(_row(row))
    out["replayed"] = False
    out["rate_source"] = resolution.source
    return out


def list_charges(
    *,
    patient_id: Optional[str] = None,
    admission_id: Optional[str] = None,
    status: Optional[str] = None,
    invoice_id: Optional[int] = None,
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


def cancel_charge(charge_id: int, *, reason: str, actor: Optional[str] = None) -> dict:
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
               AND order_type IN ({','.join('?' * len(_BILLABLE_ORDER_TYPES))})
               AND status IN ({','.join('?' * len(_COMPLETED_ORDER_STATUSES))})
             ORDER BY created_at""",
        (patient_id, *_BILLABLE_ORDER_TYPES, *_COMPLETED_ORDER_STATUSES),
    ).fetchall():
        candidates.append({
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
        })

    if _table_exists(db, "helix_patient_med_orders"):
        has_master = _table_exists(db, "helix_medication_master")
        sql = (
            "SELECT o.id AS id, o.medication_id AS medication_id, o.status AS status, "
            "o.created_at AS created_at"
            + (", m.name AS med_name" if has_master else ", NULL AS med_name")
            + " FROM helix_patient_med_orders o"
            + (" LEFT JOIN helix_medication_master m ON m.id = o.medication_id"
               if has_master else "")
            + f" WHERE o.patient_id = ? AND o.status IN "
              f"({','.join('?' * len(_DISPENSED_MED_STATUSES))}) "
              "ORDER BY o.created_at"
        )
        for row in db.execute(sql, (patient_id, *_DISPENSED_MED_STATUSES)).fetchall():
            med_id = _clean(row["medication_id"])
            candidates.append({
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
            })

    return candidates


def capture_clinical_charges(
    *,
    patient_id: str,
    admission_id: Optional[str] = None,
    ward_class: Optional[str] = None,
    actor: Optional[str] = None,
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
        context = _resolve_encounter_context(
            db, patient_id=patient_id, admission_id=admission_id
        )
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

    for cand in candidates:
        if cand["idempotency_key"] in existing_keys:
            skipped.append({**_candidate_view(cand), "reason": "already_charged"})
            continue
        if not cand["candidate_codes"]:
            skipped.append({**_candidate_view(cand), "reason": "no_service_code"})
            continue

        resolution = None
        for code in cand["candidate_codes"]:
            attempt = billing_masters.resolve_rate(
                code, tariff_plan_id=plan_id, ward_class=ward_class,
                on_date=cand["service_date"],
            )
            if attempt.source != "unknown_item":
                resolution = attempt
                break
        if resolution is None:
            skipped.append({
                **_candidate_view(cand),
                "reason": "no_service_item",
                "hint": (
                    "create a service item with code "
                    f"{cand['candidate_codes'][0]!r} to price this"
                ),
            })
            continue
        if not resolution.configured:
            skipped.append({
                **_candidate_view(cand),
                "reason": "rate_not_configured",
                "service_code": resolution.service_code,
                "rate_display": resolution.display,
            })
            continue

        if dry_run:
            posted.append({
                **_candidate_view(cand),
                "service_code": resolution.service_code,
                "rate_paise": resolution.rate_paise,
                "rate_display": resolution.display,
                "would_post": True,
            })
            continue

        charge = add_charge(
            patient_id=patient_id,
            admission_id=admission_id,
            service_code=resolution.service_code,
            description=cand["description"],
            ward_class=ward_class,
            tariff_plan_id=plan_id,
            service_date=cand["service_date"],
            source=cand["source"],
            source_ref=str(cand["source_ref"]),
            idempotency_key=cand["idempotency_key"],
            charged_by=actor,
        )
        posted.append({**_candidate_view(cand), **{
            "charge_id": charge["id"],
            "service_code": charge["service_code"],
            "total_paise": charge["total_paise"],
            "total_display": charge["total_display"],
        }})

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


def _discharge_stamp(db, admission: dict) -> Optional[str]:
    """When a discharged admission ended, as stored (UTC), or None.

    An imported encounter carries ``admissions.discharged_at``; an Orb-native
    discharge leaves that NULL and timestamps its discharge summary instead
    (migrations._m15 explains the split). The summary is the patient's first
    one written at or after this admission began, so an earlier stay's
    summary is never taken for this one."""
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


def _admission_moves(db, admission_id: str) -> list[dict]:
    """The admission's ward transfers, oldest first. The ledger table is
    created by services/frontdesk_service, so a database that never ran it has
    no transfers to read, which is the same answer as an empty ledger."""
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'patient_movements'"
    ).fetchone() is None:
        return []
    rows = db.execute(
        """SELECT from_ward, to_ward, moved_at FROM patient_movements
            WHERE admission_id = ? ORDER BY julianday(moved_at) ASC, rowid ASC""",
        (admission_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def _ward_by_day(moves: list[dict], current_ward: Optional[str],
                 start_d: date, end_d: date) -> dict:
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
        while idx < len(dated) and dated[idx][0] and dated[idx][0] <= day.isoformat():
            ward = dated[idx][1] or ward
            idx += 1
        out[day] = ward
        day += timedelta(days=1)
    return out


def post_bed_day_charges(
    *,
    admission_id: str,
    service_code: str,
    through: Optional[str] = None,
    ward_class: Optional[str] = None,
    actor: Optional[str] = None,
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
    """
    service_code = _clean(service_code)
    if not service_code:
        raise BillingError("a bed-day service code is required", code="invalid_input")
    with db_session() as db:
        adm = db.execute(
            "SELECT * FROM admissions WHERE id = ?", (admission_id,)
        ).fetchone()
        if adm is None:
            raise BillingError(f"no admission {admission_id!r}", code="not_found")
        admission = _row(adm)
        discharge_stamp = (_discharge_stamp(db, admission)
                           if admission.get("status") == "discharged" else None)
        moves = _admission_moves(db, admission_id)

    # The admission timestamp is stored UTC; the bed day it starts is a
    # hospital-local calendar day. See _local_day_of_stored.
    start = _local_day_of_stored(admission.get("created_at")) or hospital_today().isoformat()
    if through:
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
        raise BillingError(f"unreadable date on this admission: {exc}", code="invalid_input")
    if end_d < start_d:
        raise BillingError(
            "the through-date is before the admission date", code="invalid_input"
        )

    wards = _ward_by_day(moves, admission.get("ward"), start_d, end_d)
    posted, replayed, unpriced_days = [], [], []
    day = start_d
    while day <= end_d:
        try:
            charge = add_charge(
                patient_id=admission["patient_id"],
                admission_id=admission_id,
                service_code=service_code,
                ward_class=ward_class or wards.get(day) or admission.get("ward"),
                service_date=day.isoformat(),
                source="bed_day",
                source_ref=f"{admission_id}:{day.isoformat()}",
                idempotency_key=f"bedday:{admission_id}:{day.isoformat()}",
                charged_by=actor,
                description=f"Bed charge: {day.isoformat()}",
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
            unpriced_days.append(day.isoformat())
        day += timedelta(days=1)

    total = sum(int(c["total_paise"]) for c in posted)
    return {
        "admission_id": admission_id,
        "service_code": service_code,
        "from": start_d.isoformat(),
        "through": end_d.isoformat(),
        "posted": posted,
        "already_present": len(replayed),
        "posted_count": len(posted),
        "posted_total_paise": total,
        "posted_total_display": format_inr(total),
        "unpriced_days": unpriced_days,
        "unpriced_day_count": len(unpriced_days),
    }


# ── Invoices ─────────────────────────────────────────────────────────────────

def _decorate_invoice(row: dict) -> dict:
    for field in (
        "subtotal_paise", "discount_paise", "taxable_value_paise", "cgst_paise",
        "sgst_paise", "igst_paise", "tax_paise", "total_paise",
        "rounding_adjustment_paise", "payable_paise", "amount_paid_paise",
        "balance_paise", "patient_responsibility_paise", "payer_responsibility_paise",
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
    admission_id: Optional[str] = None,
    invoice_type: str = "final",
    charge_ids: Optional[Sequence[int]] = None,
    encounter_type: Optional[str] = None,
    place_of_supply: Optional[str] = None,
    interstate: bool = False,
    round_off: Optional[bool] = None,
    issue: bool = False,
    notes: Optional[str] = None,
    created_by: Optional[str] = None,
    idempotency_key: Optional[str] = None,
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
        context = _resolve_encounter_context(
            db, patient_id=patient_id, admission_id=admission_id
        )
        payer = context["payer"]

        if charge_ids:
            placeholders = ",".join("?" * len(charge_ids))
            rows = db.execute(
                f"SELECT * FROM encounter_charges WHERE id IN ({placeholders}) "
                "ORDER BY id",
                tuple(int(c) for c in charge_ids),
            ).fetchall()
            found = {int(r["id"]) for r in rows}
            missing = [int(c) for c in charge_ids if int(c) not in found]
            if missing:
                raise BillingError(
                    f"no such charge(s): {missing}", code="not_found",
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
                for r in rows if r["status"] != _CHARGE_BILLABLE
            ]
            if not_pending:
                raise BillingError(
                    "some charges are not pending (already billed or cancelled)",
                    code="charge_not_billable", detail={"charges": not_pending},
                )
        else:
            if admission_id:
                rows = db.execute(
                    "SELECT * FROM encounter_charges WHERE admission_id = ? AND status = ? "
                    "ORDER BY id",
                    (admission_id, _CHARGE_BILLABLE),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM encounter_charges WHERE patient_id = ? AND status = ? "
                    "AND admission_id IS NULL ORDER BY id",
                    (patient_id, _CHARGE_BILLABLE),
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

        cur = db.execute(
            """INSERT INTO invoices
                 (patient_id, admission_id, encounter_type, payer_id, tariff_plan_id,
                  invoice_type, status, place_of_supply, is_interstate,
                  subtotal_paise, discount_paise, taxable_value_paise, cgst_paise,
                  sgst_paise, igst_paise, tax_paise, total_paise,
                  rounding_adjustment_paise, payable_paise, amount_paid_paise,
                  balance_paise, patient_responsibility_paise, payer_responsibility_paise,
                  period_from, period_to, idempotency_key, notes, created_by)
               VALUES (?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)""",
            (
                patient_id, admission_id,
                encounter_type or context["encounter_type"],
                payer.payer_id, payer.tariff_plan_id, invoice_type,
                place_of_supply, 1 if interstate else 0,
                totals.subtotal_paise, totals.discount_paise, totals.taxable_value_paise,
                totals.cgst_paise, totals.sgst_paise, totals.igst_paise,
                totals.tax_paise, totals.total_paise,
                totals.rounding_adjustment_paise, totals.payable_paise,
                totals.payable_paise,          # balance = payable until money arrives
                patient_share, payer_share,
                rows[0]["service_date"], rows[-1]["service_date"],
                idempotency_key, notes, created_by,
            ),
        )
        invoice_id = int(cur.lastrowid)

        for line_no, (charge, line) in enumerate(zip(rows, lines), start=1):
            db.execute(
                """INSERT INTO invoice_lines
                     (invoice_id, line_no, encounter_charge_id, service_item_id,
                      service_code, description, hsn_sac, quantity_milli, uom,
                      unit_rate_paise, gross_paise, discount_paise, gst_treatment,
                      gst_rate_bp, taxable_value_paise, cgst_paise, sgst_paise,
                      igst_paise, tax_paise, total_paise)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    invoice_id, line_no, int(charge["id"]), charge["service_item_id"],
                    charge["service_code"], charge["description"],
                    _hsn_for(db, charge["service_code"]),
                    int(charge["quantity_milli"] or QUANTITY_SCALE),
                    _uom_for(db, charge["service_code"]),
                    int(charge["unit_rate_paise"] or 0),
                    line.gross_paise, line.discount_paise, line.gst_treatment,
                    line.gst_rate_bp, line.taxable_value_paise, line.cgst_paise,
                    line.sgst_paise, line.igst_paise, line.tax_paise, line.total_paise,
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


def _hsn_for(db, service_code: str) -> Optional[str]:
    row = db.execute(
        "SELECT hsn_sac FROM service_items WHERE code = ?", (service_code,)
    ).fetchone()
    return row["hsn_sac"] if row else None


def _uom_for(db, service_code: str) -> Optional[str]:
    row = db.execute(
        "SELECT uom FROM service_items WHERE code = ?", (service_code,)
    ).fetchone()
    return row["uom"] if row else None


def issue_invoice(invoice_id: int, *, actor: Optional[str] = None) -> dict:
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

    allocation = billing_masters.next_document_number("INV")

    with db_session() as db:
        updated = db.execute(
            "UPDATE invoices SET status = 'issued', invoice_number = ?, "
            "issued_at = ?, issued_by = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'draft'",
            (allocation["number"], _now_iso(), actor, int(invoice_id)),
        ).rowcount
        if updated != 1:
            logger.error(
                "Invoice %s was not in draft when its number was applied; document "
                "number %s is now a GAP in the INV series and must be accounted for.",
                invoice_id, allocation["number"],
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
            + (", an invoice with payments against it is reversed with a credit "
               "note, never cancelled" if current in ("paid", "partially_paid")
               and target == "cancelled" else ""),
            code="illegal_transition",
            detail={"from": current, "to": target, "allowed": sorted(allowed)},
        )


def cancel_invoice(invoice_id: int, *, reason: str, actor: Optional[str] = None) -> dict:
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
                f"Invoice {invoice_id} ({row['invoice_number'] or 'draft'}) cancelled: "
                f"{cleaned}"
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


def create_credit_note(
    invoice_id: int, *, reason: str, actor: Optional[str] = None
) -> dict:
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
            raise BillingError(
                "a credit note cannot itself be credited", code="illegal_transition"
            )
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
        lines = [_row(r) for r in db.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
            (int(invoice_id),),
        ).fetchall()]

    if not lines:
        raise BillingError(
            f"invoice {invoice_id} has no lines to credit", code="no_charges"
        )

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
            invoice_id, totals.total_paise, source_payable, cn_rounding,
        )
    allocation = billing_masters.next_document_number("INV")

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
                invoice_id, int(raced["id"]), allocation["number"],
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
        cur = db.execute(
            """INSERT INTO invoices
                 (invoice_number, patient_id, admission_id, encounter_type, payer_id,
                  tariff_plan_id, invoice_type, status, place_of_supply, is_interstate,
                  subtotal_paise, discount_paise, taxable_value_paise, cgst_paise,
                  sgst_paise, igst_paise, tax_paise, total_paise,
                  rounding_adjustment_paise, payable_paise, amount_paid_paise,
                  balance_paise, supersedes_invoice_id, issued_at, issued_by,
                  notes, created_by)
               VALUES (?,?,?,?,?,?,'credit_note','issued',?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)""",
            (
                allocation["number"], source["patient_id"], source["admission_id"],
                source["encounter_type"], source["payer_id"], source["tariff_plan_id"],
                source["place_of_supply"], source["is_interstate"],
                totals.subtotal_paise, totals.discount_paise, totals.taxable_value_paise,
                totals.cgst_paise, totals.sgst_paise, totals.igst_paise,
                totals.tax_paise, totals.total_paise,
                cn_rounding, cn_payable,
                cn_payable, int(invoice_id), _now_iso(), actor,
                f"Credit note against invoice {source['invoice_number'] or invoice_id}: {cleaned}",
                actor,
            ),
        )
        note_id = int(cur.lastrowid)
        for line_no, (src, line) in enumerate(zip(lines, reversed_lines), start=1):
            db.execute(
                """INSERT INTO invoice_lines
                     (invoice_id, line_no, encounter_charge_id, service_item_id,
                      service_code, description, hsn_sac, quantity_milli, uom,
                      unit_rate_paise, gross_paise, discount_paise, gst_treatment,
                      gst_rate_bp, taxable_value_paise, cgst_paise, sgst_paise,
                      igst_paise, tax_paise, total_paise)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    note_id, line_no, src["encounter_charge_id"], src["service_item_id"],
                    src["service_code"], f"Credit: {src['description']}", src["hsn_sac"],
                    int(src["quantity_milli"] or QUANTITY_SCALE), src["uom"],
                    -int(src["unit_rate_paise"] or 0),
                    line.gross_paise, line.discount_paise, line.gst_treatment,
                    line.gst_rate_bp, line.taxable_value_paise, line.cgst_paise,
                    line.sgst_paise, line.igst_paise, line.tax_paise, line.total_paise,
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

        if (paid != int(row["amount_paid_paise"] or 0)
                or balance != int(row["balance_paise"] or 0)
                or status != row["status"]):
            db.execute(
                "UPDATE invoices SET amount_paid_paise = ?, balance_paise = ?, "
                "status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (paid, balance, status, int(invoice_id)),
            )
        fresh = db.execute("SELECT * FROM invoices WHERE id = ?", (int(invoice_id),)).fetchone()
    return _decorate_invoice(_row(fresh))


def _site_name() -> Optional[str]:
    """The configured hospital name, or None. Site config is optional and a
    missing one must not break a bill."""
    try:
        from services.site_config_service import current_site_config
        return current_site_config().site_name or None
    except Exception:  # pragma: no cover - defensive
        logger.debug("site config unavailable for an invoice", exc_info=True)
        return None


def get_invoice(invoice_id: int) -> dict:
    """One invoice, its lines, its patient and what has been paid against it."""
    header = refresh_invoice_payment_state(invoice_id)
    with db_session() as db:
        lines = [_row(r) for r in db.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
            (int(invoice_id),),
        ).fetchall()]
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
        payments = [_row(r) for r in db.execute(
            """SELECT p.id, p.receipt_number, p.amount_paise, p.direction, p.method,
                      p.status, p.received_at
                 FROM payments p
                WHERE p.invoice_id = ?
                   OR p.id IN (SELECT payment_id FROM payment_allocations
                                WHERE invoice_id = ? AND reversed_at IS NULL)
                ORDER BY p.id""",
            (int(invoice_id), int(invoice_id)),
        ).fetchall()]
        credit_notes = [_row(r) for r in db.execute(
            "SELECT id, invoice_number, payable_paise, issued_at FROM invoices "
            "WHERE supersedes_invoice_id = ? ORDER BY id",
            (int(invoice_id),),
        ).fetchall()]

    for line in lines:
        for field in ("gross_paise", "discount_paise", "taxable_value_paise",
                      "cgst_paise", "sgst_paise", "igst_paise", "tax_paise",
                      "total_paise", "unit_rate_paise"):
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
    header["supplier_name"] = _site_name()
    header["credit_notes"] = credit_notes
    header["allowed_transitions"] = sorted(_LEGAL_TRANSITIONS.get(header["status"], frozenset()))
    return header


def list_invoices(
    *,
    patient_id: Optional[str] = None,
    admission_id: Optional[str] = None,
    status: Optional[str] = None,
    invoice_type: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    sql = ("SELECT i.*, p.name AS patient_name, p.mrn AS patient_mrn "
           "FROM invoices i LEFT JOIN patients p ON p.id = i.patient_id WHERE 1=1")
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
    sql += " ORDER BY i.id DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]
    return [_decorate_invoice(r) for r in rows]


# ── The printable document ───────────────────────────────────────────────────

_ONES = (
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
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
    must carry the supplier's legal name, address and GSTIN. Orb's site config
    holds a hospital NAME and nothing else, so this view returns the name it has
    and marks address/GSTIN as NOT CONFIGURED. A printed document that shows a
    blank where a GSTIN belongs is a document somebody notices; one that shows a
    plausible placeholder is one they file.
    """
    invoice = get_invoice(invoice_id)

    site_name = None
    try:  # site config is optional; a missing one must not break a bill
        from services.site_config_service import current_site_config
        site_name = current_site_config().site_name
    except Exception:  # pragma: no cover - defensive
        logger.debug("site config unavailable for invoice print view", exc_info=True)

    with db_session() as db:
        series = db.execute(
            "SELECT is_configured FROM document_number_series WHERE series_code = 'INV'"
        ).fetchone()
    series_configured = bool(series["is_configured"]) if series is not None else False

    tax_lines = [l for l in invoice["lines"] if int(l["tax_paise"] or 0) != 0]
    exempt_lines = [l for l in invoice["lines"] if l["gst_treatment"] != "taxable"]

    return {
        "invoice": invoice,
        "supplier": {
            "name": site_name or "Not configured",
            "name_is_configured": bool(site_name),
            "address": "Not configured",
            "gstin": "Not configured",
            # Said in words on the document itself, not buried in a tooltip: a
            # tax invoice without a GSTIN is not a tax invoice.
            "notice": (
                "This hospital's GSTIN and registered address are not configured in "
                "Orb, so this document is a BILL, not a GST tax invoice."
            ),
        },
        "number_series_configured": series_configured,
        "number_notice": (
            None if series_configured else
            "Document numbers come from a placeholder series. Set the hospital's "
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
            int(r["id"]) for r in db.execute(
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


def patient_financial_summary(
    patient_id: str, *, admission_id: Optional[str] = None
) -> dict:
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
            ((patient_id, _CHARGE_BILLABLE, admission_id) if admission_id
             else (patient_id, _CHARGE_BILLABLE)),
        ).fetchone()
        deposit = db.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS bal FROM patient_deposits "
            "WHERE patient_id = ?" + (" AND (admission_id = ? OR admission_id IS NULL)"
                                      if admission_id else ""),
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
    admission_id: Optional[str] = None,
    checked_by: Optional[str] = None,
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
    capture_error: Optional[str] = None
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
            "Discharge advisory could not price this encounter for %s", patient_id,
            exc_info=True,
        )

    with db_session() as db:
        has_plan = db.execute(
            "SELECT 1 FROM tariff_plans WHERE is_active = 1 LIMIT 1"
        ).fetchone() is not None

    net_due = summary["net_due_paise"]
    # A captured-but-unpriced charge forces 'unknown' for the same reason an
    # unpriceable event does: it sits in every total as a 0 that is not a
    # price, so the net due is missing a term nobody has entered. The two are
    # different facts — ``unpriceable_event_count`` is clinical events the
    # capture pass could NOT turn into charges; ``unpriced_charge_count`` is
    # charges that DID land (the frontdesk writes them deliberately — "UNPRICED
    # IS NOT FREE") and are waiting for the billing counter to price them.
    if (capture_error or unpriceable > 0 or not has_plan
            or summary["unpriced_charge_count"] > 0):
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
    priced_unbilled = (
        summary["unbilled_charge_count"] - summary["unpriced_charge_count"]
    )
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
            f"{format_inr(-net_due)} is due back to the patient: raise the refund "
            "before they leave"
        )
    if summary["open_claim_count"]:
        n_claims = summary["open_claim_count"]
        reasons.append(
            f"{_count(n_claims, 'insurance claim')} {'is' if n_claims == 1 else 'are'} still open")

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
            if not blocks else
            "Financial clearance is ENFORCED at this deployment; an override is required."
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


def _record_discharge_check(advisory: dict, *, checked_by: Optional[str]) -> None:
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
                    advisory["patient_id"], advisory["admission_id"], _now_iso(),
                    checked_by,
                    # 'unknown' stores NULL rather than 0: the column must not
                    # claim a figure the check could not compute.
                    None if advisory["advisory_status"] == "unknown"
                    else advisory["net_due_paise"],
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
            advisory.get("patient_id"), exc_info=True,
        )


def assert_discharge_permitted(
    *,
    patient_id: str,
    admission_id: Optional[str] = None,
    checked_by: Optional[str] = None,
    override_reason: Optional[str] = None,
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
        patient_id=patient_id, admission_id=admission_id, checked_by=checked_by,
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
