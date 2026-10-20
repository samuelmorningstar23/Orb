"""Orb — the billing MASTERS: what the hospital sells, to whom, at what price.

This is the read/write layer over the reference data every other admin module
depends on: `service_items`, `tariff_plans`, `tariff_rates`, `package_rates`,
`payers`, `patient_payer_policies` and `document_number_series`. It exists so
that five module agents share ONE answer to three questions:

    "What does this cost?"      → resolve_rate()
    "Who is paying?"            → resolve_patient_payer()
    "What number does this
     document get?"             → next_document_number()

NUMBERING IS PER FACILITY (multisite-7a, migration 73). A series row belongs to
one facility (document_number_series.facility_id, unique with the series code),
and every allocator and lookup here takes `facility_id`, defaulting to 1, so a
site with one facility numbers exactly as before. A second facility gets its own
rows through save_document_series, whose prefix rule keeps two facilities'
numbers from ever colliding (see _prefix_problems).

THE RULE THIS MODULE IS BUILT AROUND: A PRICE NOBODY SET IS NOT ₹0.
`resolve_rate` never returns a bare integer. It returns a `RateResolution` whose
`configured` flag is False when no tariff row and no default rate exist, and
whose `rate_paise` is then `None`. A caller that wants a number must decide what
to do about a missing one; it cannot accidentally bill zero. Every screen that
shows a resolved rate must render `format_inr_or_unset`, which prints "Not
configured".

CONVENTIONS THIS FILE FOLLOWS (from the rest of server/services)
  * Every function is PURE SYNC and opens its own `db_session()`. Async callers
    go through `database.run_db` — blocking DB calls inside `async def` are
    banned and tests/test_event_loop_discipline.py enforces it.
  * All money arithmetic goes through services/money.py. There is no rounding,
    no percentage and no formatting in this file.
  * No foreign keys exist on these tables (see migration 20): a bad id yields an
    empty result, never a failed clinical write.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypedDict

from database import db_session

from services.money import (
    GST_EXEMPT,
    GST_TREATMENTS,
    assert_paise,
    format_inr_or_unset,
)

logger = logging.getLogger("orb.billing_masters")

#: The `service_items.category` vocabulary, mirroring the CHECK constraint in the
#: table definition (see database.py / migration 20).
#:
#: WHY IT IS DECLARED HERE. `upsert_service_item` validated `gst_treatment`
#: against `GST_TREATMENTS` and then handed `category` straight to SQLite. A
#: category outside the CHECK set therefore escaped as a raw IntegrityError and
#: POST /api/billing/service-items answered **500 Internal Server Error** — on
#: the very route `capture_clinical_charges` tells the clerk to use ("create a
#: service item with code '58410-2' to price this"). The natural word for a
#: pathology item is "lab"; the schema's word is "investigation", and nothing
#: told anyone. Validating here turns that into the same 422 + "expected one of
#: …" every other master-data mistake gets, so the clerk can correct it.
SERVICE_CATEGORIES = frozenset(
    {
        "consultation",
        "procedure",
        "investigation",
        "imaging",
        "bed",
        "nursing",
        "ot",
        "pharmacy",
        "consumable",
        "implant",
        "ambulance",
        "package",
        "other",
    }
)

__all__ = [
    "RateResolution",
    "PayerResolution",
    "MasterDataError",
    "SERVICE_CATEGORIES",
    "list_service_items",
    "get_service_item",
    "upsert_service_item",
    "list_tariff_plans",
    "default_tariff_plan",
    "set_tariff_rate",
    "resolve_rate",
    "resolve_rate_on",
    "resolve_package_rate",
    "list_payers",
    "get_payer",
    "upsert_payer",
    "resolve_patient_payer",
    "resolve_patient_payer_on",
    "record_patient_payer_on",
    "register_unpriced_item_on",
    "financial_year_for",
    "next_document_number",
    "SeriesNotConfigured",
    "SeriesNotSetUp",
    "SeriesEditError",
    "MAIN_FACILITY_ID",
    "GROUP_WIDE_SERIES",
    "ISSUED_NUMBER_COLUMNS",
    "series_label",
    "series_is_configured",
    "preview_series_number",
    "list_document_series",
    "save_document_series",
    "allocate_series_number",
    "verhoeff_check_digit",
    "verhoeff_valid",
    "ensure_default_masters",
]


class _RateContext(TypedDict):
    service_code: str
    tariff_plan_id: int | None
    tariff_plan_code: str | None
    ward_class: str | None
    gst_treatment: str
    gst_rate_bp: int
    service_item_id: int | None
    description: str | None
    hsn_sac: str | None
    uom: str | None


class MasterDataError(ValueError):
    """Bad reference data supplied by a caller (unknown treatment, blank code)."""


class SeriesNotConfigured(MasterDataError):
    """allocate_series_number(require_configured=True) on a series a site has
    not configured yet. A number that is printed on a card for life must never
    come from a placeholder series (the UHID, opd-4)."""

    def __init__(self, series_code: str):
        self.series_code = series_code
        super().__init__(f"document series {series_code!r} is not configured on this site")


#: Facility 1, MAIN (migration 70): every series a site had before migration
#: 73 lives here, and every allocator defaults to it.
MAIN_FACILITY_ID = 1


class SeriesNotSetUp(MasterDataError):
    """A facility other than 1 has no row for this series yet (multisite-7a).

    Never answered by borrowing facility 1's series: a number from another
    facility's series would print that facility's prefix on this facility's
    document, which is exactly what per-facility numbering exists to stop.
    The administrator sets the series up in Admin, Facilities."""

    def __init__(self, series_code: str, facility_id: int):
        self.series_code = series_code
        self.facility_id = int(facility_id)
        super().__init__(
            f"document series {series_code!r} is not set up for facility {int(facility_id)}"
        )


class SeriesEditError(MasterDataError):
    """A refused change to a document series, with per-field reasons for the
    Facilities screen: `errors` is [{field, code, message}]."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_series",
        status: int = 400,
        errors: list | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.errors = list(errors or [])

    def body(self) -> dict:
        return {"error": self.code, "message": str(self), "errors": self.errors}


# ── Resolution results ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RateResolution:
    """What one service costs under one tariff — and WHERE that came from.

    `configured` False means nobody has priced this item. `rate_paise` is then
    None, and the UI must say "Not configured" rather than "₹0.00".
    """

    service_code: str
    rate_paise: int | None
    configured: bool
    source: str  # 'tariff_rate' | 'service_default' | 'unconfigured' | 'unknown_item'
    tariff_plan_id: int | None
    tariff_plan_code: str | None
    ward_class: str | None
    gst_treatment: str
    gst_rate_bp: int
    service_item_id: int | None
    description: str | None
    hsn_sac: str | None = None
    uom: str | None = None

    @property
    def display(self) -> str:
        """Always safe to put on a screen: an amount or the words."""
        return format_inr_or_unset(self.rate_paise)

    def as_dict(self) -> dict:
        return {
            "service_code": self.service_code,
            "rate_paise": self.rate_paise,
            "rate_display": self.display,
            "rate_is_configured": self.configured,
            "source": self.source,
            "tariff_plan_id": self.tariff_plan_id,
            "tariff_plan_code": self.tariff_plan_code,
            "ward_class": self.ward_class,
            "gst_treatment": self.gst_treatment,
            "gst_rate_bp": self.gst_rate_bp,
            "service_item_id": self.service_item_id,
            "description": self.description,
            "hsn_sac": self.hsn_sac,
            "uom": self.uom,
        }


@dataclass(frozen=True)
class PayerResolution:
    """Who is paying for this encounter, and under which price list."""

    payer_id: int | None
    payer_code: str | None
    payer_name: str | None
    payer_type: str
    policy_id: int | None
    policy_number: str | None
    tariff_plan_id: int | None
    tariff_plan_code: str | None
    copay_percent_bp: int
    preauth_required: bool
    sum_insured_paise: int | None
    sum_insured_configured: bool
    source: str  # 'policy' | 'default_self_pay' | 'none'

    def as_dict(self) -> dict:
        return {
            "payer_id": self.payer_id,
            "payer_code": self.payer_code,
            "payer_name": self.payer_name,
            "payer_type": self.payer_type,
            "policy_id": self.policy_id,
            "policy_number": self.policy_number,
            "tariff_plan_id": self.tariff_plan_id,
            "tariff_plan_code": self.tariff_plan_code,
            "copay_percent_bp": self.copay_percent_bp,
            "preauth_required": self.preauth_required,
            "sum_insured_paise": self.sum_insured_paise,
            "sum_insured_display": format_inr_or_unset(self.sum_insured_paise, unset="Not known"),
            "sum_insured_is_configured": self.sum_insured_configured,
            "source": self.source,
        }


# ── Service items ────────────────────────────────────────────────────────────


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def list_service_items(
    *,
    category: str | None = None,
    active_only: bool = True,
    search: str | None = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT * FROM service_items WHERE 1=1"
    params: list[Any] = []
    if active_only:
        sql += " AND is_active = 1"
    if category:
        sql += " AND category = ?"
        params.append(category)
    if search:
        sql += " AND (name LIKE ? OR code LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    sql += " ORDER BY category, name LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        return [_row_to_dict(r) for r in db.execute(sql, params).fetchall()]


def get_service_item(code: str) -> dict | None:
    with db_session() as db:
        row = db.execute("SELECT * FROM service_items WHERE code = ?", (code,)).fetchone()
    return _row_to_dict(row) if row else None


def upsert_service_item(
    *,
    code: str,
    name: str,
    category: str = "other",
    department: str | None = None,
    uom: str = "each",
    gst_treatment: str = GST_EXEMPT,
    gst_rate_bp: int = 0,
    hsn_sac: str | None = None,
    default_rate_paise: int | None = None,
    is_active: bool = True,
    notes: str | None = None,
    created_by: str | None = None,
) -> dict:
    """Create or update one chargeable item.

    `default_rate_paise=None` means "no price" and sets `rate_is_configured=0` —
    it does NOT write 0 and call it a price. Passing an explicit 0 DOES set
    `rate_is_configured=1`: a genuinely free item is a real thing (a waived
    consultation, a scheme-covered service) and must be distinguishable from an
    unpriced one.
    """
    code = (code or "").strip()
    if not code:
        raise MasterDataError("service item code must not be blank")
    if gst_treatment not in GST_TREATMENTS:
        raise MasterDataError(
            f"unknown GST treatment {gst_treatment!r}; expected one of {sorted(GST_TREATMENTS)}"
        )
    # Checked HERE rather than left to the table's CHECK constraint: an
    # IntegrityError from inside db_session is not a BillingError or a
    # MasterDataError, so `_handle` in routes/billing_routes.py cannot translate
    # it and the caller gets a bare 500 with no idea which field was wrong.
    if category not in SERVICE_CATEGORIES:
        raise MasterDataError(
            f"unknown service category {category!r}; expected one of {sorted(SERVICE_CATEGORIES)}"
        )
    configured = default_rate_paise is not None
    rate = assert_paise(default_rate_paise) if configured else 0
    if configured and rate < 0:
        raise MasterDataError("a service item's default rate must not be negative")

    with db_session() as db:
        db.execute(
            """INSERT INTO service_items
                 (code, name, category, department, uom, gst_treatment, gst_rate_bp,
                  hsn_sac, default_rate_paise, rate_is_configured, is_active, notes,
                  created_by, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, category=excluded.category,
                 department=excluded.department, uom=excluded.uom,
                 gst_treatment=excluded.gst_treatment, gst_rate_bp=excluded.gst_rate_bp,
                 hsn_sac=excluded.hsn_sac,
                 default_rate_paise=excluded.default_rate_paise,
                 rate_is_configured=excluded.rate_is_configured,
                 is_active=excluded.is_active, notes=excluded.notes,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                code,
                name,
                category,
                department,
                uom,
                gst_treatment,
                int(gst_rate_bp),
                hsn_sac,
                rate,
                1 if configured else 0,
                1 if is_active else 0,
                notes,
                created_by,
            ),
        )
        row = db.execute("SELECT * FROM service_items WHERE code = ?", (code,)).fetchone()
    return _row_to_dict(row)


# ── Tariffs ──────────────────────────────────────────────────────────────────


def list_tariff_plans(*, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM tariff_plans"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY is_default DESC, name"
    with db_session() as db:
        return [_row_to_dict(r) for r in db.execute(sql).fetchall()]


def default_tariff_plan() -> dict | None:
    """The plan a self-pay patient is priced on. Migration 20 seeds 'SELFPAY'."""
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM tariff_plans WHERE is_active = 1 "
            "ORDER BY is_default DESC, id ASC LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def set_tariff_rate(
    *,
    tariff_plan_id: int,
    service_code: str,
    rate_paise: int,
    ward_class: str | None = None,
    effective_from: str | None = None,
    created_by: str | None = None,
) -> dict:
    """Price one service under one plan (optionally per bed class).

    A rate row is a fact with a date, so re-pricing writes a NEW row with a new
    `effective_from` rather than overwriting the old one — a bill raised last
    month must still reproduce from the rate that was in force then. Two writes
    on the SAME effective date collide on the unique index and update in place,
    which is the "I typed it wrong five minutes ago" case.
    """
    rate = assert_paise(rate_paise)
    eff = effective_from or date.today().isoformat()
    with db_session() as db:
        item = db.execute("SELECT id FROM service_items WHERE code = ?", (service_code,)).fetchone()
        db.execute(
            """INSERT INTO tariff_rates
                 (tariff_plan_id, service_item_id, service_code, ward_class,
                  rate_paise, effective_from, is_active, created_by)
               VALUES (?,?,?,?,?,?,1,?)
               ON CONFLICT(tariff_plan_id, service_code, IFNULL(ward_class,''), effective_from)
               DO UPDATE SET rate_paise=excluded.rate_paise, is_active=1,
                             service_item_id=excluded.service_item_id""",
            (
                int(tariff_plan_id),
                item["id"] if item else None,
                service_code,
                ward_class,
                rate,
                eff,
                created_by,
            ),
        )
        row = db.execute(
            "SELECT * FROM tariff_rates WHERE tariff_plan_id = ? AND service_code = ? "
            "AND IFNULL(ward_class,'') = IFNULL(?,'') AND effective_from = ?",
            (int(tariff_plan_id), service_code, ward_class, eff),
        ).fetchone()
    return _row_to_dict(row)


def resolve_rate(
    service_code: str,
    *,
    tariff_plan_id: int | None = None,
    ward_class: str | None = None,
    on_date: str | None = None,
) -> RateResolution:
    """What does this service cost, under this plan, on this date?

    PRECEDENCE, most specific first:
      1. a tariff_rates row for (plan, code, exact ward_class) in force on the date
      2. a tariff_rates row for (plan, code, ward_class NULL) in force on the date
      3. the service item's own default_rate_paise, IF rate_is_configured
      4. nothing — `configured=False`, `rate_paise=None`, source 'unconfigured'

    "In force on the date" = effective_from <= date AND (effective_to IS NULL OR
    effective_to >= date), newest effective_from wins. Dates are ISO strings and
    compare lexicographically, which is why the column default is '1970-01-01'
    rather than NULL.

    An unknown service code returns source 'unknown_item' — also unconfigured.
    That distinction matters at a billing counter: "we don't sell that" and "we
    haven't priced that" are different conversations.
    """
    with db_session() as db:
        return resolve_rate_on(
            db,
            service_code,
            tariff_plan_id=tariff_plan_id,
            ward_class=ward_class,
            on_date=on_date,
        )


def resolve_rate_on(
    db,
    service_code: str,
    *,
    tariff_plan_id: int | None = None,
    ward_class: str | None = None,
    on_date: str | None = None,
) -> RateResolution:
    """resolve_rate on the caller's connection (opd-13): the outpatient charge
    writers price a line inside the transaction that raises it, so the rate
    and the row are read and written under one lock. Same precedence, same
    answer, no commit."""
    day = on_date or date.today().isoformat()
    item = db.execute("SELECT * FROM service_items WHERE code = ?", (service_code,)).fetchone()
    plan_row = None
    if tariff_plan_id is not None:
        plan_row = db.execute(
            "SELECT * FROM tariff_plans WHERE id = ?", (int(tariff_plan_id),)
        ).fetchone()
    if plan_row is None:
        plan_row = db.execute(
            "SELECT * FROM tariff_plans WHERE is_active = 1 "
            "ORDER BY is_default DESC, id ASC LIMIT 1"
        ).fetchone()

    rate_row = None
    if plan_row is not None:
        rate_row = db.execute(
            """SELECT * FROM tariff_rates
                WHERE tariff_plan_id = ? AND service_code = ? AND is_active = 1
                  AND effective_from <= ?
                  AND (effective_to IS NULL OR effective_to >= ?)
                  AND (ward_class = ? OR ward_class IS NULL)
                ORDER BY (ward_class IS NULL) ASC, effective_from DESC, id DESC
                LIMIT 1""",
            (int(plan_row["id"]), service_code, day, day, ward_class),
        ).fetchone()

    plan_id = int(plan_row["id"]) if plan_row is not None else None
    plan_code = plan_row["code"] if plan_row is not None else None
    treatment = item["gst_treatment"] if item is not None else GST_EXEMPT
    rate_bp = int(item["gst_rate_bp"]) if item is not None else 0
    common: _RateContext = {
        "service_code": service_code,
        "tariff_plan_id": plan_id,
        "tariff_plan_code": plan_code,
        "ward_class": ward_class,
        "gst_treatment": treatment,
        "gst_rate_bp": rate_bp,
        "service_item_id": int(item["id"]) if item is not None else None,
        "description": item["name"] if item is not None else None,
        "hsn_sac": item["hsn_sac"] if item is not None else None,
        "uom": item["uom"] if item is not None else None,
    }

    if rate_row is not None:
        return RateResolution(
            rate_paise=int(rate_row["rate_paise"]),
            configured=True,
            source="tariff_rate",
            **common,
        )
    if item is None:
        return RateResolution(rate_paise=None, configured=False, source="unknown_item", **common)
    if int(item["rate_is_configured"] or 0) == 1:
        return RateResolution(
            rate_paise=int(item["default_rate_paise"]),
            configured=True,
            source="service_default",
            **common,
        )
    return RateResolution(rate_paise=None, configured=False, source="unconfigured", **common)


def resolve_package_rate(
    package_code: str,
    *,
    tariff_plan_id: int | None = None,
    ward_class: str | None = None,
    on_date: str | None = None,
) -> dict | None:
    """The bundled price for a package (surgery package, PM-JAY HBP), or None.

    None means "this plan does not have that package", which is a real answer a
    claims clerk needs — not a zero.
    """
    day = on_date or date.today().isoformat()
    with db_session() as db:
        if tariff_plan_id is None:
            plan = db.execute(
                "SELECT id FROM tariff_plans WHERE is_active = 1 "
                "ORDER BY is_default DESC, id ASC LIMIT 1"
            ).fetchone()
            tariff_plan_id = int(plan["id"]) if plan else None
        if tariff_plan_id is None:
            return None
        row = db.execute(
            """SELECT * FROM package_rates
                WHERE tariff_plan_id = ? AND package_code = ? AND is_active = 1
                  AND effective_from <= ?
                  AND (effective_to IS NULL OR effective_to >= ?)
                  AND (ward_class = ? OR ward_class IS NULL)
                ORDER BY (ward_class IS NULL) ASC, effective_from DESC, id DESC
                LIMIT 1""",
            (int(tariff_plan_id), package_code, day, day, ward_class),
        ).fetchone()
    return _row_to_dict(row) if row else None


# ── Payers ───────────────────────────────────────────────────────────────────


def list_payers(*, active_only: bool = True, payer_type: str | None = None) -> list[dict]:
    sql = "SELECT * FROM payers WHERE 1=1"
    params: list[Any] = []
    if active_only:
        sql += " AND is_active = 1"
    if payer_type:
        sql += " AND payer_type = ?"
        params.append(payer_type)
    sql += " ORDER BY payer_type, name"
    with db_session() as db:
        return [_row_to_dict(r) for r in db.execute(sql, params).fetchall()]


def get_payer(code: str) -> dict | None:
    with db_session() as db:
        row = db.execute("SELECT * FROM payers WHERE code = ?", (code,)).fetchone()
    return _row_to_dict(row) if row else None


def upsert_payer(
    *,
    code: str,
    name: str,
    payer_type: str = "self_pay",
    tariff_plan_id: int | None = None,
    gstin: str | None = None,
    state_code: str | None = None,
    contact_name: str | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    claim_submission_mode: str | None = None,
    credit_days: int | None = None,
    preauth_required: bool = False,
    is_active: bool = True,
    notes: str | None = None,
) -> dict:
    code = (code or "").strip()
    if not code:
        raise MasterDataError("payer code must not be blank")
    with db_session() as db:
        db.execute(
            """INSERT INTO payers
                 (code, name, payer_type, tariff_plan_id, gstin, state_code,
                  contact_name, contact_phone, contact_email, claim_submission_mode,
                  credit_days, preauth_required, is_active, notes, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, payer_type=excluded.payer_type,
                 tariff_plan_id=excluded.tariff_plan_id, gstin=excluded.gstin,
                 state_code=excluded.state_code, contact_name=excluded.contact_name,
                 contact_phone=excluded.contact_phone, contact_email=excluded.contact_email,
                 claim_submission_mode=excluded.claim_submission_mode,
                 credit_days=excluded.credit_days,
                 preauth_required=excluded.preauth_required,
                 is_active=excluded.is_active, notes=excluded.notes,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                code,
                name,
                payer_type,
                tariff_plan_id,
                gstin,
                state_code,
                contact_name,
                contact_phone,
                contact_email,
                claim_submission_mode,
                credit_days,
                1 if preauth_required else 0,
                1 if is_active else 0,
                notes,
            ),
        )
        row = db.execute("SELECT * FROM payers WHERE code = ?", (code,)).fetchone()
    return _row_to_dict(row)


def resolve_patient_payer(
    patient_id: str,
    *,
    admission_id: str | None = None,
) -> PayerResolution:
    """Who pays for this encounter, and on which tariff.

    Precedence: an active policy for THIS admission, then an active policy for
    the patient with no admission scope, then self-pay. Self-pay is the fallback
    because it is the honest one — an unresolved payer is a patient paying cash
    at the counter, not free care.
    """
    with db_session() as db:
        return resolve_patient_payer_on(db, patient_id, admission_id=admission_id)


def resolve_patient_payer_on(
    db,
    patient_id: str,
    *,
    admission_id: str | None = None,
) -> PayerResolution:
    """resolve_patient_payer on the caller's connection (opd-13), so a payer
    recorded earlier in the same transaction (the registration desk's payer
    category) is the one an outpatient line is priced under. No commit."""
    row = None
    if admission_id:
        row = db.execute(
            """SELECT p.*, y.code AS payer_code, y.name AS payer_name,
                      y.payer_type AS payer_type, y.tariff_plan_id AS payer_plan_id
                 FROM patient_payer_policies p
                 LEFT JOIN payers y ON y.id = p.payer_id
                WHERE p.patient_id = ? AND p.admission_id = ? AND p.status = 'active'
                ORDER BY p.is_primary DESC, p.id DESC LIMIT 1""",
            (patient_id, admission_id),
        ).fetchone()
    if row is None:
        row = db.execute(
            """SELECT p.*, y.code AS payer_code, y.name AS payer_name,
                      y.payer_type AS payer_type, y.tariff_plan_id AS payer_plan_id
                 FROM patient_payer_policies p
                 LEFT JOIN payers y ON y.id = p.payer_id
                WHERE p.patient_id = ? AND p.admission_id IS NULL AND p.status = 'active'
                ORDER BY p.is_primary DESC, p.id DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()

    default_plan = db.execute(
        "SELECT id, code FROM tariff_plans WHERE is_active = 1 "
        "ORDER BY is_default DESC, id ASC LIMIT 1"
    ).fetchone()
    self_payer = db.execute(
        "SELECT * FROM payers WHERE payer_type = 'self_pay' AND is_active = 1 "
        "ORDER BY id ASC LIMIT 1"
    ).fetchone()

    plan_id = None
    plan_code = None
    if row is not None and row["payer_plan_id"] is not None:
        plan_id = int(row["payer_plan_id"])
        plan = db.execute("SELECT code FROM tariff_plans WHERE id = ?", (plan_id,)).fetchone()
        plan_code = plan["code"] if plan else None
    elif default_plan is not None:
        plan_id = int(default_plan["id"])
        plan_code = default_plan["code"]

    if row is not None:
        return PayerResolution(
            payer_id=int(row["payer_id"]),
            payer_code=row["payer_code"],
            payer_name=row["payer_name"],
            payer_type=row["payer_type"] or "self_pay",
            policy_id=int(row["id"]),
            policy_number=row["policy_number"],
            tariff_plan_id=plan_id,
            tariff_plan_code=plan_code,
            copay_percent_bp=int(row["copay_percent_bp"] or 0),
            preauth_required=bool(row["preauth_required"]),
            sum_insured_paise=(
                int(row["sum_insured_paise"])
                if row["sum_insured_paise"] is not None
                and int(row["sum_insured_is_configured"] or 0) == 1
                else None
            ),
            sum_insured_configured=int(row["sum_insured_is_configured"] or 0) == 1,
            source="policy",
        )

    return PayerResolution(
        payer_id=int(self_payer["id"]) if self_payer else None,
        payer_code=self_payer["code"] if self_payer else None,
        payer_name=self_payer["name"] if self_payer else None,
        payer_type="self_pay",
        policy_id=None,
        policy_number=None,
        tariff_plan_id=plan_id,
        tariff_plan_code=plan_code,
        copay_percent_bp=0,
        preauth_required=False,
        sum_insured_paise=None,
        sum_insured_configured=False,
        source="default_self_pay" if self_payer else "none",
    )


#: The longest policy or member number the registration desk may type.
MAX_POLICY_TEXT = 60


def record_patient_payer_on(
    db,
    *,
    patient_id: str,
    payer_code: str,
    policy_number: str | None = None,
    member_id: str | None = None,
    created_by: str | None = None,
) -> dict:
    """Record who pays for a patient's outpatient care (opd-13): one active
    patient_payer_policies row with NO admission, so resolve_patient_payer
    prices every outpatient line on that payer's tariff.

    The payer must be an active row of the payer master; an unknown or retired
    code is refused (MasterDataError), never created here. The row is recorded
    unverified (verified_by NULL): the desk wrote down what the patient said,
    and the insurance desk verifies cover. Any earlier admission-free policy of
    the patient's stays as it is; the newest primary one wins, which is the
    resolver's existing rule. On the caller's connection, no commit.
    """
    code = (payer_code or "").strip()
    if not code:
        raise MasterDataError("payer code must not be blank")
    payer = db.execute(
        "SELECT id, code, name, payer_type, tariff_plan_id, is_active FROM payers WHERE code = ?",
        (code,),
    ).fetchone()
    if payer is None or not int(payer["is_active"] or 0):
        raise MasterDataError(f"{code!r} is not an active payer on this site")

    def _text(value: str | None, what: str) -> str | None:
        text = (value or "").strip() or None
        if text is not None and len(text) > MAX_POLICY_TEXT:
            raise MasterDataError(f"the {what} is longer than {MAX_POLICY_TEXT} characters")
        return text

    cursor = db.execute(
        """INSERT INTO patient_payer_policies
               (patient_id, admission_id, payer_id, policy_number, member_id,
                is_primary, status, created_by)
           VALUES (?, NULL, ?, ?, ?, 1, 'active', ?)""",
        (
            patient_id,
            int(payer["id"]),
            _text(policy_number, "policy number"),
            _text(member_id, "member id"),
            created_by,
        ),
    )
    return {
        "policy_id": int(cursor.lastrowid),
        "payer_id": int(payer["id"]),
        "payer_code": payer["code"],
        "payer_name": payer["name"],
        "payer_type": payer["payer_type"],
        "tariff_plan_id": payer["tariff_plan_id"],
        "verified": False,
    }


def register_unpriced_item_on(
    db,
    *,
    code: str,
    name: str,
    category: str = "consultation",
    notes: str | None = None,
) -> bool:
    """Put a service code on the price list UNPRICED (rate_is_configured 0),
    so the billing counter sees it and prices it; never over a row a site has
    touched (INSERT OR IGNORE on the unique code). True when this call added
    it. The front desk registers OPD-CONSULT the same way
    (frontdesk_service.ensure_frontdesk_schema). On the caller's connection."""
    if category not in SERVICE_CATEGORIES:
        raise MasterDataError(f"unknown service category {category!r}")
    cursor = db.execute(
        """INSERT OR IGNORE INTO service_items
               (code, name, category, uom, gst_treatment, gst_rate_bp,
                default_rate_paise, rate_is_configured, is_active, notes)
           VALUES (?, ?, ?, 'each', 'exempt', 0, 0, 0, 1, ?)""",
        (code, name, category, notes),
    )
    return bool(cursor.rowcount)


# ── Document numbering ───────────────────────────────────────────────────────


def financial_year_for(when: date | datetime | str | None = None) -> str:
    """India's financial year label for a date: 1 April – 31 March. April 2026
    through March 2027 is '2026-27'. Invoice series reset on it."""
    if when is None:
        d = date.today()
    elif isinstance(when, str):
        d = date.fromisoformat(when[:10])
    elif isinstance(when, datetime):
        d = when.date()
    else:
        d = when
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _series_row(db, series_code: str, facility_id: int):
    """The series row for one facility, or None. Every lookup by code goes
    through here (migration 73 made the code unique per facility, not alone)."""
    return db.execute(
        "SELECT * FROM document_number_series WHERE series_code = ? AND facility_id = ?",
        (series_code, int(facility_id)),
    ).fetchone()


def _missing_series(series_code: str, facility_id: int, message: str) -> MasterDataError:
    """Facility 1's refusal is the one this module always gave; any other
    facility is told its series is not set up yet."""
    if int(facility_id) == MAIN_FACILITY_ID:
        return MasterDataError(message)
    return SeriesNotSetUp(series_code, facility_id)


def series_is_configured(db, series_code: str, *, facility_id: int = MAIN_FACILITY_ID) -> bool:
    """Whether a facility's series is the site's real one (is_configured),
    False when the facility has no such series. On the caller's connection."""
    row = _series_row(db, series_code, facility_id)
    return bool(row is not None and int(row["is_configured"] or 0))


def next_document_number(
    series_code: str, *, when: date | str | None = None, facility_id: int = MAIN_FACILITY_ID
) -> dict:
    """Allocate the next number in a series. ONE implementation, for everybody.

    Returns `{"number": "INV-2026-27-000001", "sequence": 1, "is_configured": 0}`.

    THE COUNTER IS INCREMENTED INSIDE AN IMMEDIATE TRANSACTION. Two counters
    issuing receipts at the same second must not both read 41 and both write 42:
    `db_session(immediate=True)` takes the write lock before the SELECT, which is
    the documented way to make a read-then-write guard actually hold in this
    codebase (database.py's note on db_session).

    THE SERIES RESETS ON THE FINANCIAL YEAR, and the year is part of the number,
    because a GST invoice series is per financial year.

    `is_configured` rides along: a site that has not set its real prefix gets
    numbers, but every screen that prints one must show that the series is a
    placeholder. A placeholder invoice number that looks official is worse than
    no number at all.

    `facility_id` picks the facility's own row (migration 73); it defaults to 1,
    whose rows are every series a site had before, so one-facility numbering is
    unchanged. A facility other than 1 with no row for the series raises
    SeriesNotSetUp: it never borrows facility 1's counter.
    """
    fy = financial_year_for(when)
    with db_session(immediate=True) as db:
        row = _series_row(db, series_code, facility_id)
        if row is None:
            raise _missing_series(
                series_code,
                facility_id,
                f"unknown document series {series_code!r}; migration 20 seeds "
                "INV, RCPT, REF, CLM, IND, PO and GRN",
            )
        # A new financial year restarts the sequence at 1.
        if (row["financial_year"] or "") != fy:
            sequence = 1
            db.execute(
                "UPDATE document_number_series SET financial_year = ?, next_number = 2, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (fy, int(row["id"])),
            )
        else:
            sequence = int(row["next_number"])
            db.execute(
                "UPDATE document_number_series SET next_number = next_number + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(row["id"]),),
            )
        prefix = row["prefix"] or ""
        suffix = row["suffix"] or ""
        width = int(row["pad_width"] or 6)
        configured = int(row["is_configured"] or 0)

    return {
        "series_code": series_code,
        "number": f"{prefix}{fy}-{str(sequence).zfill(width)}{suffix}",
        "sequence": sequence,
        "financial_year": fy,
        "is_configured": configured,
    }


# ── Check digits ─────────────────────────────────────────────────────────────
# Verhoeff's dihedral-group scheme catches every single-digit error and every
# transposition of adjacent digits, which a plain mod-10 sum does not. It is the
# check digit a UHID series carries (migration 50 seeds check_digit='verhoeff'):
# a hospital number is read aloud and typed at counters for decades.

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_VERHOEFF_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def _digits_only(text: str) -> str:
    text = str(text or "")
    if not text or not text.isdigit() or not text.isascii():
        raise ValueError("a Verhoeff check digit is computed over ASCII digits only")
    return text


def verhoeff_check_digit(digits: str) -> str:
    """The Verhoeff check digit to append to `digits` (a string of 0-9)."""
    c = 0
    for i, ch in enumerate(reversed(_digits_only(digits))):
        c = _VERHOEFF_D[c][_VERHOEFF_P[(i + 1) % 8][int(ch)]]
    return str(_VERHOEFF_INV[c])


def verhoeff_valid(number: str) -> bool:
    """True when the last digit of `number` is its Verhoeff check digit."""
    try:
        digits = _digits_only(number)
    except ValueError:
        return False
    if len(digits) < 2:
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


_CHECK_DIGIT_SCHEMES = {"verhoeff": verhoeff_check_digit}


def allocate_series_number(
    db,
    series_code: str,
    *,
    when: date | str | None = None,
    require_configured: bool = False,
    facility_id: int = MAIN_FACILITY_ID,
) -> dict:
    """Allocate the next number in a series on the CALLER'S connection.

    The connection must already hold a write transaction (the caller opened
    db_session(immediate=True)), so the counter moves in the same transaction
    as the document that carries the number: a rolled-back registration hands
    its UHID back, and two desks registering at the same second serialise on
    the write lock instead of both reading 41. This function never commits and
    never opens a session (next_document_number, which does, is unchanged).

    A series with resets_yearly = 1 behaves exactly like next_document_number:
    the financial year restarts the sequence and is part of the number. A
    series with resets_yearly = 0 (the UHID, migration 50) never resets and
    never embeds a year: the number is prefix + zero-padded sequence (+ check
    digit) + suffix, and a sequence wider than the pad simply grows.

    `require_configured` refuses a series the site has not configured with
    SeriesNotConfigured, before anything moves. A number that is printed on a
    card for life must not come from a placeholder series.

    Returns {series_code, number, sequence, financial_year (None when the
    series never resets), is_configured, check_digit}.

    `facility_id` (default 1) picks the facility's own row, as in
    next_document_number (migration 73).
    """
    if not getattr(db, "in_transaction", False):
        raise RuntimeError(
            "allocate_series_number needs the caller's open write transaction "
            "(db_session(immediate=True)); it never opens or commits one itself"
        )
    row = _series_row(db, series_code, facility_id)
    if row is None:
        raise _missing_series(series_code, facility_id, f"unknown document series {series_code!r}")
    keys = set(row.keys())
    configured = int(row["is_configured"] or 0)
    if require_configured and not configured:
        raise SeriesNotConfigured(series_code)
    resets_yearly = (
        int(row["resets_yearly"])
        if "resets_yearly" in keys and row["resets_yearly"] is not None
        else 1
    )
    scheme = (row["check_digit"] or None) if "check_digit" in keys else None
    if scheme is not None and scheme not in _CHECK_DIGIT_SCHEMES:
        # Fail closed: a number issued without the check digit the series
        # promises would validate nowhere.
        raise MasterDataError(f"series {series_code!r} names an unknown check digit {scheme!r}")
    prefix = row["prefix"] or ""
    suffix = row["suffix"] or ""
    width = int(row["pad_width"] or 6)

    if resets_yearly:
        fy = financial_year_for(when)
        if (row["financial_year"] or "") != fy:
            sequence = 1
            db.execute(
                "UPDATE document_number_series SET financial_year = ?, next_number = 2, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (fy, int(row["id"])),
            )
        else:
            sequence = int(row["next_number"])
            db.execute(
                "UPDATE document_number_series SET next_number = next_number + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(row["id"]),),
            )
        body = f"{fy}-{str(sequence).zfill(width)}"
    else:
        fy = None
        sequence = int(row["next_number"])
        db.execute(
            "UPDATE document_number_series SET next_number = next_number + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(row["id"]),),
        )
        body = str(sequence).zfill(width)
    if scheme is not None:
        body += _CHECK_DIGIT_SCHEMES[scheme](str(sequence).zfill(width))
    return {
        "series_code": series_code,
        "number": f"{prefix}{body}{suffix}",
        "sequence": sequence,
        "financial_year": fy,
        "is_configured": configured,
        "check_digit": scheme,
    }


# ── Series per facility: what each series numbers, and the editor ────────────
# multisite-7a. The editor is reached through /api/facilities/{id}/
# document-series (routes/facility_routes.py), behind ORB_MULTI_SITE.

#: Series that are one for the whole group, never one per facility. The UHID is
#: a patient's hospital number for life and one person has one group MRN (ruling
#: MS-R11 (a)); a second facility with its own UHID series would split one
#: person into two records and their allergy history with them.
GROUP_WIDE_SERIES = frozenset({"UHID"})

#: Where each seeded series' numbers are written, as (table, column). The
#: prefix rule reads these for numbers already issued, and treats series that
#: write the same column as one register: stores and pharmacy purchase orders
#: share purchase_orders.po_number and its unique index, so their prefixes must
#: keep apart as surely as two facilities' must. A series a site added itself
#: is not listed; only its own rows at other facilities are compared.
ISSUED_NUMBER_COLUMNS: dict = {
    "INV": (("invoices", "invoice_number"),),
    "RCPT": (("payments", "receipt_number"),),
    "REF": (("refunds", "refund_number"),),
    "CLM": (("claims", "claim_number"),),
    "IND": (("indents", "indent_number"),),
    "PHIND": (("indents", "indent_number"),),
    "PO": (("purchase_orders", "po_number"),),
    "PHPO": (("purchase_orders", "po_number"),),
    "GRN": (("goods_receipts", "grn_number"),),
    "PHGRN": (("goods_receipts", "grn_number"),),
    "PHTRF": (("pharmacy_transfers", "transfer_number"),),
    "PHVR": (("pharmacy_vendor_returns", "return_number"),),
    "PHRET": (("pharmacy_ward_returns", "return_number"),),
    "PHCNT": (("pharmacy_stock_counts", "count_number"),),
    "KIND": (("kitchen_indents", "indent_number"),),
    "INC": (("nabh_incidents", "reference"),),
    "NABHPACK": (("nabh_evidence_exports", "reference"),),
    "UHID": (("patients", "mrn"),),
}

#: What each seeded series numbers, in the words the Facilities screen shows.
SERIES_LABELS: dict = {
    "INV": "Invoices and credit notes",
    "RCPT": "Receipts",
    "REF": "Refunds",
    "CLM": "Insurance claims",
    "IND": "Store indents",
    "PO": "Purchase orders",
    "GRN": "Goods receipts",
    "KIND": "Kitchen indents",
    "INC": "Incident reports",
    "NABHPACK": "NABH evidence packs",
    "UHID": "Hospital numbers (UHID)",
    "PHPO": "Pharmacy purchase orders",
    "PHGRN": "Pharmacy goods receipts",
    "PHIND": "Pharmacy indents",
    "PHTRF": "Pharmacy transfers",
    "PHVR": "Pharmacy returns to vendor",
    "PHRET": "Ward returns of medicines",
    "PHCNT": "Pharmacy stock counts",
}

#: The fields the editor may change. The code, document type, counter,
#: financial year, reset rule and check digit are never edited: moving a
#: counter back reissues numbers, and the other four are what the software
#: relies on the series to be.
EDITABLE_SERIES_FIELDS = ("prefix", "suffix", "pad_width", "is_configured")

_PREFIX_RE = re.compile(r"^[A-Za-z0-9/._-]{0,16}$")
_SUFFIX_RE = re.compile(r"^[A-Za-z0-9/._-]{0,8}$")
PAD_WIDTH_MIN, PAD_WIDTH_MAX = 1, 12


def series_label(series_code: str, document_type: str | None = None) -> str:
    """The words for what a series numbers."""
    label = SERIES_LABELS.get(series_code)
    if label:
        return label
    words = (document_type or series_code or "").replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else str(series_code)


def preview_series_number(row, *, when: date | str | None = None) -> str:
    """The number a series would hand out next, as it would print. Allocates
    nothing; follows allocate_series_number's rules (a yearly series restarts
    at 1 in a new financial year; a check digit is appended)."""
    keys = set(row.keys())
    width = int(row["pad_width"] or 6)
    resets = (
        int(row["resets_yearly"])
        if "resets_yearly" in keys and row["resets_yearly"] is not None
        else 1
    )
    scheme = (row["check_digit"] or None) if "check_digit" in keys else None
    if resets:
        fy = financial_year_for(when)
        sequence = 1 if (row["financial_year"] or "") != fy else int(row["next_number"])
        body = f"{fy}-{str(sequence).zfill(width)}"
    else:
        sequence = int(row["next_number"])
        body = str(sequence).zfill(width)
    if scheme in _CHECK_DIGIT_SCHEMES:
        body += _CHECK_DIGIT_SCHEMES[scheme](str(sequence).zfill(width))
    return f"{row['prefix'] or ''}{body}{row['suffix'] or ''}"


def _series_view(row) -> dict:
    out = _row_to_dict(row)
    out["facility_id"] = int(out.get("facility_id") or MAIN_FACILITY_ID)
    out["label"] = series_label(out["series_code"], out.get("document_type"))
    out["is_configured"] = bool(int(out.get("is_configured") or 0))
    out["resets_yearly"] = (
        bool(int(out["resets_yearly"])) if out.get("resets_yearly") is not None else True
    )
    out["group_wide"] = out["series_code"] in GROUP_WIDE_SERIES
    out["next_number_preview"] = preview_series_number(row)
    return out


def _table_has(db, table: str, column: str) -> bool:
    cols = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")').fetchall()}
    return column in cols


def _register_codes(series_code: str) -> set:
    """Every series code that writes one of the same number columns as this
    one, this one included."""
    mine = set(ISSUED_NUMBER_COLUMNS.get(series_code, ()))
    codes = {series_code}
    for code, columns in ISSUED_NUMBER_COLUMNS.items():
        if mine & set(columns):
            codes.add(code)
    return codes


def _prefix_problems(
    db, *, facility_id: int, series_code: str, prefix: str, exclude_id: int | None
) -> list:
    """Why `prefix` may not number this series at this facility, as field errors.

    THE RULE. Two series writing one register (the same series at another
    facility, or another series that writes the same number column) may not
    have prefixes where one equals or begins with the other: only then could
    their numbers ever be the same string, and the register's unique index
    would refuse the second one at the moment a document is saved. And no
    number already issued may begin with the prefix, whoever issued it: the
    series would then be able to hand out a number already on record. So a
    prefix is checked when it is set or changed, never on other edits."""
    errors = []
    codes = sorted(_register_codes(series_code))
    marks = ",".join("?" for _ in codes)
    has_facilities = (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facilities'"
        ).fetchone()
        is not None
    )
    facility_code = "f.code" if has_facilities else "NULL"
    join = "LEFT JOIN facilities f ON f.id = d.facility_id" if has_facilities else ""
    others = db.execute(
        f"SELECT d.id, d.facility_id, d.series_code, d.document_type, d.prefix, "
        f"{facility_code} AS facility_code FROM document_number_series d {join} "
        f"WHERE d.series_code IN ({marks}) AND d.id != ? ORDER BY d.facility_id, d.series_code",
        (*codes, int(exclude_id) if exclude_id is not None else -1),
    ).fetchall()
    for other in others:
        theirs = other["prefix"] or ""
        if not (prefix == theirs or prefix.startswith(theirs) or theirs.startswith(prefix)):
            continue
        where = other["facility_code"] or f"facility {int(other['facility_id'])}"
        what = series_label(other["series_code"], other["document_type"]).lower()
        shown = theirs if theirs else "no prefix at all"
        if other["series_code"] == series_code:
            message = (
                f"Facility {where} numbers its {what} with {shown}. Two facilities' "
                "prefixes may not be the same, and neither may begin with the other, "
                "or their numbers could collide."
            )
        else:
            message = (
                f"{series_label(other['series_code'], other['document_type'])} at "
                f"{where} are numbered with {shown} in the same register. The two "
                "prefixes may not be the same, and neither may begin with the other."
            )
        errors.append({"field": "prefix", "code": "prefix_clash", "message": message})
        break
    for table, column in ISSUED_NUMBER_COLUMNS.get(series_code, ()):
        if not _table_has(db, table, column):
            continue
        hit = db.execute(
            f'SELECT "{column}" AS n FROM "{table}" WHERE "{column}" IS NOT NULL '
            f'AND substr("{column}", 1, ?) = ? LIMIT 1',
            (len(prefix), prefix),
        ).fetchone()
        if hit is not None:
            errors.append(
                {
                    "field": "prefix",
                    "code": "prefix_matches_issued",
                    "message": (
                        f"A number already issued, {hit['n']}, begins with "
                        f"{prefix if prefix else 'no prefix at all'}. Choose a prefix "
                        "that no issued number begins with, so no number can be "
                        "handed out twice."
                    ),
                }
            )
            break
    return errors


def _facility_on_record(db, facility_id: int) -> dict | None:
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facilities'"
        ).fetchone()
        is None
    ):
        return (
            {"id": MAIN_FACILITY_ID, "code": "MAIN", "is_active": 1}
            if int(facility_id) == MAIN_FACILITY_ID
            else None
        )
    row = db.execute(
        "SELECT id, code, is_active FROM facilities WHERE id = ?", (int(facility_id),)
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def list_document_series(facility_id: int) -> dict:
    """A facility's series for the Facilities screen: {facility_id, series,
    can_add}. `can_add` lists the series facility 1 has that this facility
    does not (never a group-wide one); it is empty for facility 1, which holds
    every series the software seeds."""
    fid = int(facility_id)
    with db_session() as db:
        if _facility_on_record(db, fid) is None:
            raise SeriesEditError("No facility has that number.", code="not_found", status=404)
        rows = db.execute(
            "SELECT * FROM document_number_series WHERE facility_id = ? ORDER BY series_code",
            (fid,),
        ).fetchall()
        series = [_series_view(r) for r in rows]
        can_add = []
        if fid != MAIN_FACILITY_ID:
            have = {r["series_code"] for r in rows}
            for main in db.execute(
                "SELECT series_code, document_type FROM document_number_series "
                "WHERE facility_id = ? ORDER BY series_code",
                (MAIN_FACILITY_ID,),
            ).fetchall():
                code = main["series_code"]
                if code in have or code in GROUP_WIDE_SERIES:
                    continue
                can_add.append(
                    {
                        "series_code": code,
                        "document_type": main["document_type"],
                        "label": series_label(code, main["document_type"]),
                    }
                )
    return {"facility_id": fid, "series": series, "can_add": can_add}


def _validated_series_fields(changes: Any) -> tuple[dict, list]:
    if not isinstance(changes, dict):
        raise SeriesEditError("Send the fields to change as an object.")
    out: dict = {}
    errors: list = []
    for field, value in changes.items():
        if field not in EDITABLE_SERIES_FIELDS:
            errors.append(
                {
                    "field": field,
                    "code": "not_editable",
                    "message": "This part of a number series cannot be changed here.",
                }
            )
            continue
        if field == "prefix":
            if not isinstance(value, str) or not _PREFIX_RE.match(value):
                errors.append(
                    {
                        "field": field,
                        "code": "bad_prefix",
                        "message": "Use up to 16 letters, digits or the characters "
                        "/ . _ -, with no spaces.",
                    }
                )
                continue
        elif field == "suffix":
            if not isinstance(value, str) or not _SUFFIX_RE.match(value):
                errors.append(
                    {
                        "field": field,
                        "code": "bad_suffix",
                        "message": "Use up to 8 letters, digits or the characters "
                        "/ . _ -, with no spaces.",
                    }
                )
                continue
        elif field == "pad_width":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not PAD_WIDTH_MIN <= value <= PAD_WIDTH_MAX
            ):
                errors.append(
                    {
                        "field": field,
                        "code": "bad_pad_width",
                        "message": f"Give a whole number of digits from "
                        f"{PAD_WIDTH_MIN} to {PAD_WIDTH_MAX}.",
                    }
                )
                continue
        elif field == "is_configured":
            if not isinstance(value, bool):
                errors.append({"field": field, "code": "bad_value", "message": "Say yes or no."})
                continue
            value = 1 if value else 0
        out[field] = value
    return out, errors


def _audit_series(
    db,
    *,
    action: str,
    actor: dict | None,
    facility: dict,
    series_code: str,
    summary: str,
    changes: dict,
) -> None:
    """site_admin, in the writing transaction (fail-soft category, as every
    facility identity edit is)."""
    from services.audit_service import log_audit_event

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="site_admin",
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type="document_series",
        target_id=f"{int(facility['id'])}:{series_code}",
        metadata={
            "facility_id": int(facility["id"]),
            "facility_code": facility.get("code"),
            "series_code": series_code,
            "changes": changes,
        },
    )


def save_document_series(
    facility_id: int, series_code: str, changes: dict, *, actor: dict | None = None
) -> dict:
    """Set up or edit one facility's series. Returns {series, created, changed}.

    EDIT (the facility has the series): only EDITABLE_SERIES_FIELDS; a changed
    prefix goes through _prefix_problems; nothing else is compared, so editing
    the digits of a series whose prefix predates the rule is not refused.

    SET UP (a facility other than 1 that lacks it): only a series facility 1
    has (its document type, reset rule and check digit are copied), never a
    group-wide one (GROUP_WIDE_SERIES), only for an active facility, and a
    prefix must be given and pass the rule. The counter starts at 1.

    Who may do this is the route's decision (facility.manage, own facility,
    ORB_MULTI_SITE); every change writes a site_admin row with the field diff.
    """
    fid = int(facility_id)
    code = (series_code or "").strip()
    fields, errors = _validated_series_fields(changes)
    if errors:
        raise SeriesEditError(
            "Some details need correcting before they can be saved.", errors=errors
        )
    with db_session(immediate=True) as db:
        facility = _facility_on_record(db, fid)
        if facility is None:
            raise SeriesEditError("No facility has that number.", code="not_found", status=404)
        row = _series_row(db, code, fid)
        if row is None:
            if fid == MAIN_FACILITY_ID:
                raise SeriesEditError(
                    "Facility 1 has no series with that code.", code="series_not_found", status=404
                )
            if code in GROUP_WIDE_SERIES:
                raise SeriesEditError(
                    f"{series_label(code)} are one series for the whole group, kept at "
                    "facility 1, so a patient keeps one number at every facility.",
                    code="group_wide_series",
                    status=409,
                )
            template = _series_row(db, code, MAIN_FACILITY_ID)
            if template is None:
                raise SeriesEditError(
                    "No series with that code exists to set up.",
                    code="series_not_found",
                    status=404,
                )
            if not int(facility.get("is_active") or 0):
                raise SeriesEditError(
                    "This facility is closed; its numbering cannot be set up.",
                    code="facility_inactive",
                    status=409,
                )
            if "prefix" not in fields:
                raise SeriesEditError(
                    "Give the prefix this facility's numbers start with.",
                    errors=[
                        {
                            "field": "prefix",
                            "code": "prefix_required",
                            "message": "A facility's own series needs its own prefix.",
                        }
                    ],
                )
            problems = _prefix_problems(
                db, facility_id=fid, series_code=code, prefix=fields["prefix"], exclude_id=None
            )
            if problems:
                raise SeriesEditError("That prefix could repeat a number.", errors=problems)
            t = _row_to_dict(template)
            values = {
                "prefix": fields["prefix"],
                "suffix": fields.get("suffix", ""),
                "pad_width": fields.get("pad_width", int(t.get("pad_width") or 6)),
                "is_configured": fields.get("is_configured", 0),
            }
            db.execute(
                "INSERT INTO document_number_series (facility_id, series_code, document_type, "
                "prefix, suffix, pad_width, next_number, is_configured, resets_yearly, "
                "check_digit) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    fid,
                    code,
                    t["document_type"],
                    values["prefix"],
                    values["suffix"],
                    values["pad_width"],
                    values["is_configured"],
                    int(t["resets_yearly"]) if t.get("resets_yearly") is not None else 1,
                    t.get("check_digit"),
                ),
            )
            fresh = _series_row(db, code, fid)
            _audit_series(
                db,
                action="document_series_created",
                actor=actor,
                facility=facility,
                series_code=code,
                summary=f"Numbering for {series_label(code).lower()} set up at "
                f"{facility.get('code') or fid}",
                changes={k: {"from": None, "to": v} for k, v in values.items()},
            )
            return {"series": _series_view(fresh), "created": True, "changed": sorted(values)}

        current = _row_to_dict(row)
        diff = {
            k: {"from": current.get(k), "to": v} for k, v in fields.items() if current.get(k) != v
        }
        if not diff:
            return {"series": _series_view(row), "created": False, "changed": []}
        if "prefix" in diff:
            problems = _prefix_problems(
                db,
                facility_id=fid,
                series_code=code,
                prefix=fields["prefix"],
                exclude_id=int(current["id"]),
            )
            if problems:
                raise SeriesEditError("That prefix could repeat a number.", errors=problems)
        sets = ", ".join(f"{k} = ?" for k in diff)
        db.execute(
            f"UPDATE document_number_series SET {sets}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (*(fields[k] for k in diff), int(current["id"])),
        )
        fresh = _series_row(db, code, fid)
        _audit_series(
            db,
            action="document_series_updated",
            actor=actor,
            facility=facility,
            series_code=code,
            summary=f"Numbering for {series_label(code, current.get('document_type')).lower()} "
            f"changed at {facility.get('code') or fid}",
            changes=diff,
        )
        return {"series": _series_view(fresh), "created": False, "changed": sorted(diff)}


# ── Bootstrap ────────────────────────────────────────────────────────────────


def ensure_default_masters() -> dict:
    """Idempotently re-assert the STRUCTURAL defaults migration 20 seeds.

    Structural only — a self-pay tariff plan, a self-pay payer, a central store
    and the document series. NO PRICES. Every rupee in this system has to have
    been typed by somebody at the hospital; a seeded price list would be a
    fabricated number that looks like a real one, which is exactly the failure
    this project's rules forbid.

    Exists separately from the migration so an admin screen can repair a
    deployment whose masters were deleted, without a re-migration.
    """
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO tariff_plans (code, name, plan_type, is_default, is_active) "
            "VALUES ('SELFPAY', 'Self-pay (cash)', 'self_pay', 1, 1)"
        )
        db.execute(
            "INSERT OR IGNORE INTO payers (code, name, payer_type, is_active) "
            "VALUES ('SELF', 'Self-pay / cash patient', 'self_pay', 1)"
        )
        db.execute(
            "INSERT OR IGNORE INTO stores (code, name, store_type, is_active) "
            "VALUES ('CENTRAL', 'Central store', 'central', 1)"
        )
        for series_code, doc_type, prefix in (
            ("INV", "invoice", "INV-"),
            ("RCPT", "receipt", "RCPT-"),
            ("REF", "refund", "REF-"),
            ("CLM", "claim", "CLM-"),
            ("IND", "indent", "IND-"),
            ("PO", "purchase_order", "PO-"),
            ("GRN", "goods_receipt", "GRN-"),
        ):
            # No facility named: the row lands at facility 1 through the column
            # default, and the (facility_id, series_code) key (migration 73)
            # makes the insert a no-op where facility 1 already has it.
            db.execute(
                "INSERT OR IGNORE INTO document_number_series "
                "(series_code, document_type, prefix, next_number, is_configured) "
                "VALUES (?,?,?,1,0)",
                (series_code, doc_type, prefix),
            )
        plans = db.execute("SELECT COUNT(*) FROM tariff_plans").fetchone()[0]
        payers = db.execute("SELECT COUNT(*) FROM payers").fetchone()[0]
        series = db.execute("SELECT COUNT(*) FROM document_number_series").fetchone()[0]
    return {"tariff_plans": plans, "payers": payers, "document_series": series}
