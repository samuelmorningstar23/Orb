"""Orb — the billing MASTERS: what the hospital sells, to whom, at what price.

This is the read/write layer over the reference data every other admin module
depends on: `service_items`, `tariff_plans`, `tariff_rates`, `package_rates`,
`payers`, `patient_payer_policies` and `document_number_series`. It exists so
that five module agents share ONE answer to three questions:

    "What does this cost?"      → resolve_rate()
    "Who is paying?"            → resolve_patient_payer()
    "What number does this
     document get?"             → next_document_number()

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
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

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
SERVICE_CATEGORIES = frozenset({
    "consultation", "procedure", "investigation", "imaging", "bed", "nursing",
    "ot", "pharmacy", "consumable", "implant", "ambulance", "package", "other",
})

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
    "resolve_package_rate",
    "list_payers",
    "get_payer",
    "upsert_payer",
    "resolve_patient_payer",
    "financial_year_for",
    "next_document_number",
    "ensure_default_masters",
]


class MasterDataError(ValueError):
    """Bad reference data supplied by a caller (unknown treatment, blank code)."""


# ── Resolution results ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class RateResolution:
    """What one service costs under one tariff — and WHERE that came from.

    `configured` False means nobody has priced this item. `rate_paise` is then
    None, and the UI must say "Not configured" rather than "₹0.00".
    """
    service_code: str
    rate_paise: Optional[int]
    configured: bool
    source: str                     # 'tariff_rate' | 'service_default' | 'unconfigured' | 'unknown_item'
    tariff_plan_id: Optional[int]
    tariff_plan_code: Optional[str]
    ward_class: Optional[str]
    gst_treatment: str
    gst_rate_bp: int
    service_item_id: Optional[int]
    description: Optional[str]
    hsn_sac: Optional[str] = None
    uom: Optional[str] = None

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
    payer_id: Optional[int]
    payer_code: Optional[str]
    payer_name: Optional[str]
    payer_type: str
    policy_id: Optional[int]
    policy_number: Optional[str]
    tariff_plan_id: Optional[int]
    tariff_plan_code: Optional[str]
    copay_percent_bp: int
    preauth_required: bool
    sum_insured_paise: Optional[int]
    sum_insured_configured: bool
    source: str                     # 'policy' | 'default_self_pay' | 'none'

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
            "sum_insured_display": format_inr_or_unset(
                self.sum_insured_paise, unset="Not known"
            ),
            "sum_insured_is_configured": self.sum_insured_configured,
            "source": self.source,
        }


# ── Service items ────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def list_service_items(
    *,
    category: Optional[str] = None,
    active_only: bool = True,
    search: Optional[str] = None,
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


def get_service_item(code: str) -> Optional[dict]:
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM service_items WHERE code = ?", (code,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def upsert_service_item(
    *,
    code: str,
    name: str,
    category: str = "other",
    department: Optional[str] = None,
    uom: str = "each",
    gst_treatment: str = GST_EXEMPT,
    gst_rate_bp: int = 0,
    hsn_sac: Optional[str] = None,
    default_rate_paise: Optional[int] = None,
    is_active: bool = True,
    notes: Optional[str] = None,
    created_by: Optional[str] = None,
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
                code, name, category, department, uom, gst_treatment, int(gst_rate_bp),
                hsn_sac, rate, 1 if configured else 0, 1 if is_active else 0, notes,
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


def default_tariff_plan() -> Optional[dict]:
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
    ward_class: Optional[str] = None,
    effective_from: Optional[str] = None,
    created_by: Optional[str] = None,
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
        item = db.execute(
            "SELECT id FROM service_items WHERE code = ?", (service_code,)
        ).fetchone()
        db.execute(
            """INSERT INTO tariff_rates
                 (tariff_plan_id, service_item_id, service_code, ward_class,
                  rate_paise, effective_from, is_active, created_by)
               VALUES (?,?,?,?,?,?,1,?)
               ON CONFLICT(tariff_plan_id, service_code, IFNULL(ward_class,''), effective_from)
               DO UPDATE SET rate_paise=excluded.rate_paise, is_active=1,
                             service_item_id=excluded.service_item_id""",
            (
                int(tariff_plan_id), item["id"] if item else None, service_code,
                ward_class, rate, eff, created_by,
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
    tariff_plan_id: Optional[int] = None,
    ward_class: Optional[str] = None,
    on_date: Optional[str] = None,
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
    day = on_date or date.today().isoformat()
    with db_session() as db:
        item = db.execute(
            "SELECT * FROM service_items WHERE code = ?", (service_code,)
        ).fetchone()
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
    common = {
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
            rate_paise=int(rate_row["rate_paise"]), configured=True,
            source="tariff_rate", **common,
        )
    if item is None:
        return RateResolution(
            rate_paise=None, configured=False, source="unknown_item", **common
        )
    if int(item["rate_is_configured"] or 0) == 1:
        return RateResolution(
            rate_paise=int(item["default_rate_paise"]), configured=True,
            source="service_default", **common,
        )
    return RateResolution(
        rate_paise=None, configured=False, source="unconfigured", **common
    )


def resolve_package_rate(
    package_code: str,
    *,
    tariff_plan_id: Optional[int] = None,
    ward_class: Optional[str] = None,
    on_date: Optional[str] = None,
) -> Optional[dict]:
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

def list_payers(*, active_only: bool = True, payer_type: Optional[str] = None) -> list[dict]:
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


def get_payer(code: str) -> Optional[dict]:
    with db_session() as db:
        row = db.execute("SELECT * FROM payers WHERE code = ?", (code,)).fetchone()
    return _row_to_dict(row) if row else None


def upsert_payer(
    *,
    code: str,
    name: str,
    payer_type: str = "self_pay",
    tariff_plan_id: Optional[int] = None,
    gstin: Optional[str] = None,
    state_code: Optional[str] = None,
    contact_name: Optional[str] = None,
    contact_phone: Optional[str] = None,
    contact_email: Optional[str] = None,
    claim_submission_mode: Optional[str] = None,
    credit_days: Optional[int] = None,
    preauth_required: bool = False,
    is_active: bool = True,
    notes: Optional[str] = None,
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
                code, name, payer_type, tariff_plan_id, gstin, state_code,
                contact_name, contact_phone, contact_email, claim_submission_mode,
                credit_days, 1 if preauth_required else 0, 1 if is_active else 0, notes,
            ),
        )
        row = db.execute("SELECT * FROM payers WHERE code = ?", (code,)).fetchone()
    return _row_to_dict(row)


def resolve_patient_payer(
    patient_id: str,
    *,
    admission_id: Optional[str] = None,
) -> PayerResolution:
    """Who pays for this encounter, and on which tariff.

    Precedence: an active policy for THIS admission, then an active policy for
    the patient with no admission scope, then self-pay. Self-pay is the fallback
    because it is the honest one — an unresolved payer is a patient paying cash
    at the counter, not free care.
    """
    with db_session() as db:
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
            plan = db.execute(
                "SELECT code FROM tariff_plans WHERE id = ?", (plan_id,)
            ).fetchone()
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


# ── Document numbering ───────────────────────────────────────────────────────

def financial_year_for(when: Optional[date | datetime | str] = None) -> str:
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


def next_document_number(series_code: str, *, when: Optional[date | str] = None) -> dict:
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
    """
    fy = financial_year_for(when)
    with db_session(immediate=True) as db:
        row = db.execute(
            "SELECT * FROM document_number_series WHERE series_code = ?", (series_code,)
        ).fetchone()
        if row is None:
            raise MasterDataError(
                f"unknown document series {series_code!r}; migration 20 seeds "
                "INV, RCPT, REF, CLM, IND, PO and GRN"
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
