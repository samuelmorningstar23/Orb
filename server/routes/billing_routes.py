"""Orb — billing HTTP surface: masters, charge capture, invoices, advisory.

WHO CAN REACH THIS. The desk runs on its own permission family now — the
``admin.overview`` borrow recorded here as a followup is undone (2026-08-18,
same shape as the payer desk's split). Three tiers, because the desk holds two
different pens:

  * ``billing.read`` — every read: invoices, charges, patient summaries, GST
    figures, and the tariff/price list itself. Seeing a price is part of
    working the counter.
  * ``billing.read`` + ``billing.write`` — recording what happened to ONE
    patient: capturing a charge, sweeping clinical events onto the bill,
    posting bed-days, raising/issuing/cancelling/crediting an invoice.
  * ``billing.read`` + ``billing.tariff`` — mutating the TARIFF MASTER: the
    service-item catalogue, tariff plans, rates and package rates. A separate
    authority from charge capture ON PURPOSE: a clerk capturing a charge
    records one patient's bill; whoever edits the price list changes what
    every FUTURE invoice in the hospital says. One desk, two pens, and the
    second one is not implied by the first.

WRITES REQUIRE READ AS WELL (house rule, routes/payer_routes.py precedent):
every mutation here answers with the charge/invoice/master row it touched, so a
write-only grant would be a read grant with an extra step.

Nothing changes TODAY about who can reach the desk: `admin` holds all three
permissions, and `src/shellConfig.tsx` shows the Billing tab to `admin` only.
What changes is what a future billing-clerk role has to be handed — the desk,
not the audit log. The one exception stays: the discharge advisory is gated on
``discharge.read``, because the person who needs the warning is the clinician
doing the discharge.

MODULE FLAG. Every route refuses with 404 when ``config.BILLING_ENABLED`` is off
(``ORB_BILLING=0``), so a site that hides the module does not merely lose a tab —
the endpoints stop answering. 404 rather than 403: a disabled module should look
absent, not forbidden. The flag is read PER REQUEST through the config module so
a deployment or a test that changes it changes what the server does.

WHY THESE ARE ALL PLAIN ``def``. FastAPI runs a sync route in its threadpool, so
the blocking SQLite work in services/billing_service.py stays off the event loop
without a run_db() hop. tests/test_event_loop_discipline.py enforces the rule
that makes this the correct shape rather than a stylistic one.

THE ONE THING THIS FILE MAY NEVER DO: return a refusal that stops clinical work.
``GET /discharge-advisory`` answers 200 with a warning even when a patient owes
money, and even when enforcement is enabled it reports ``blocks_discharge`` as
DATA rather than as an HTTP error, because it is the discharge path's decision,
not billing's.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import config
from permissions import (
    BILLING_READ,
    BILLING_TARIFF,
    BILLING_WRITE,
    department_scope,
    enforce_patient_scope,
    log_phi_read,
    require_patient_access,
    require_permission,
)
from routes.opt_in_gates import require_opd_feature
from services import billing_masters, billing_service
from services.audit_service import AuditWriteError
from services.billing_masters import MasterDataError
from services.billing_service import BillingError
from services.money import GST_EXEMPT, GST_TREATMENTS, QUANTITY_SCALE

router = APIRouter(prefix="/api/billing", tags=["Billing"])

#: Errors a clerk can act on themselves are 422; a state conflict (someone else
#: issued the invoice, the charge is already billed) is 409, so a UI can tell
#: "fix your input" from "reload and look again".
_CONFLICT_CODES = {
    "illegal_transition", "invoice_raced", "charges_raced", "charge_not_billable",
    "charge_not_cancellable", "invoice_has_payments", "already_exists",
}


def _billing_enabled() -> None:
    """404 when this deployment does not run the billing module."""
    if not getattr(config, "BILLING_ENABLED", True):
        raise HTTPException(
            status_code=404,
            detail={
                "error": "module_disabled",
                "module": "billing",
                "message": "The billing module is not enabled for this deployment "
                           "(ORB_BILLING=0).",
            },
        )


# ── Who can reach the desk ───────────────────────────────────────────────────
# Module-level, same as routes/payer_routes.py, so a route's gate is one grep
# away. WRITES REQUIRE READ (see the module docstring), and TARIFF requires
# READ for the same reason — every master mutation echoes the row it wrote.
_DESK_READ = require_permission(BILLING_READ)
_DESK_WRITE = require_permission(BILLING_READ, BILLING_WRITE)
_DESK_TARIFF = require_permission(BILLING_READ, BILLING_TARIFF)


def _read(user: dict = Depends(_DESK_READ)) -> dict:
    """billing.read + the module flag, in one dependency."""
    _billing_enabled()
    return user


def _write(user: dict = Depends(_DESK_WRITE)) -> dict:
    """Charge capture / invoice lifecycle: billing.read + billing.write."""
    _billing_enabled()
    return user


def _tariff(user: dict = Depends(_DESK_TARIFF)) -> dict:
    """Tariff-master mutations: billing.read + billing.tariff — the second pen."""
    _billing_enabled()
    return user


def _handle(fn, *args, **kwargs):
    """Run a service call and translate its errors into HTTP without losing them."""
    try:
        return fn(*args, **kwargs)
    except BillingError as exc:
        raise HTTPException(
            status_code=409 if exc.code in _CONFLICT_CODES else 422,
            detail={"error": exc.code, "message": str(exc), "detail": exc.detail},
        )
    except MasterDataError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "master_data", "message": str(exc)}
        )
    except ValueError as exc:   # money.MoneyError and friends
        raise HTTPException(
            status_code=422, detail={"error": "invalid_amount", "message": str(exc)}
        )


# ── Request models ───────────────────────────────────────────────────────────

class ServiceItemIn(BaseModel):
    code: str
    name: str
    category: str = "other"
    department: Optional[str] = None
    uom: str = "each"
    gst_treatment: str = GST_EXEMPT
    gst_rate_bp: int = 0
    hsn_sac: Optional[str] = None
    #: None means "no price", which is NOT ₹0. An explicit 0 means a genuinely
    #: free item and is stored as a configured price.
    default_rate_paise: Optional[int] = None
    is_active: bool = True
    notes: Optional[str] = None
    #: Set by the price list's New item: a code already on the list is refused
    #: (409) rather than overwritten.
    create_only: bool = False


class TariffPlanIn(BaseModel):
    code: str
    name: str
    plan_type: str = "self_pay"
    is_default: bool = False
    is_active: bool = True
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    notes: Optional[str] = None


class TariffRateIn(BaseModel):
    tariff_plan_id: int
    service_code: str
    rate_paise: int
    ward_class: Optional[str] = None
    effective_from: Optional[str] = None


class PackageRateIn(BaseModel):
    tariff_plan_id: int
    package_code: str
    name: str
    package_rate_paise: int
    ward_class: Optional[str] = None
    los_days_included: Optional[int] = None
    inclusions: Optional[str] = None
    exclusions: Optional[str] = None
    external_scheme_code: Optional[str] = None
    effective_from: Optional[str] = None


class ChargeIn(BaseModel):
    service_code: str
    description: Optional[str] = None
    quantity_milli: int = QUANTITY_SCALE
    #: Omit to price from the tariff. Supplying it is an explicit override and is
    #: recorded as one on the audit row.
    unit_rate_paise: Optional[int] = None
    discount_paise: int = 0
    discount_bp: int = 0
    admission_id: Optional[str] = None
    encounter_type: Optional[str] = None
    ward_class: Optional[str] = None
    tariff_plan_id: Optional[int] = None
    gst_treatment: Optional[str] = None
    gst_rate_bp: Optional[int] = None
    service_date: Optional[str] = None
    idempotency_key: Optional[str] = None
    notes: Optional[str] = None


class CaptureIn(BaseModel):
    admission_id: Optional[str] = None
    ward_class: Optional[str] = None
    dry_run: bool = False


class BedDaysIn(BaseModel):
    admission_id: str
    service_code: str
    through: Optional[str] = None
    ward_class: Optional[str] = None


class InvoiceIn(BaseModel):
    admission_id: Optional[str] = None
    invoice_type: str = "final"
    charge_ids: Optional[list[int]] = None
    encounter_type: Optional[str] = None
    place_of_supply: Optional[str] = None
    interstate: bool = False
    round_off: Optional[bool] = None
    issue: bool = False
    notes: Optional[str] = None
    idempotency_key: Optional[str] = None


class OpdInvoiceIn(BaseModel):
    """An OPD bill in one call: capture the lines, raise the invoice, issue it."""
    lines: list[ChargeIn] = Field(min_length=1)
    place_of_supply: Optional[str] = None
    interstate: bool = False
    notes: Optional[str] = None
    issue: bool = True


class ReasonIn(BaseModel):
    reason: str


# ── Config / masters ─────────────────────────────────────────────────────────

@router.get("/config")
def billing_config(user: dict = Depends(_read)):
    """What this deployment has configured — including what it has NOT."""
    plan = billing_masters.default_tariff_plan()
    items = billing_masters.list_service_items(limit=1000)
    priced = sum(1 for i in items if int(i.get("rate_is_configured") or 0) == 1)
    return {
        "module": "billing",
        "enabled": True,
        "discharge_financial_enforce": bool(
            getattr(config, "DISCHARGE_FINANCIAL_ENFORCE", False)
        ),
        "default_tariff_plan": plan,
        "service_item_count": len(items),
        "priced_service_item_count": priced,
        "unpriced_service_item_count": len(items) - priced,
        "financial_year": billing_masters.financial_year_for(),
    }


@router.get("/service-items")
def list_service_items(
    category: Optional[str] = None,
    search: Optional[str] = None,
    active_only: bool = True,
    limit: int = Query(500, ge=1, le=2000),
    user: dict = Depends(_read),
):
    return {
        "items": billing_masters.list_service_items(
            category=category, search=search, active_only=active_only, limit=limit
        )
    }


@router.post("/service-items")
def upsert_service_item(payload: ServiceItemIn, user: dict = Depends(_tariff)):
    if payload.gst_treatment not in GST_TREATMENTS:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_input",
                    "message": f"unknown GST treatment {payload.gst_treatment!r}"},
        )
    item = _handle(
        billing_service.create_service_item if payload.create_only
        else billing_masters.upsert_service_item,
        code=payload.code, name=payload.name, category=payload.category,
        department=payload.department, uom=payload.uom,
        gst_treatment=payload.gst_treatment, gst_rate_bp=payload.gst_rate_bp,
        hsn_sac=payload.hsn_sac, default_rate_paise=payload.default_rate_paise,
        is_active=payload.is_active, notes=payload.notes, created_by=user.get("id"),
    )
    return {"status": "success", "item": item}


@router.get("/tariff-plans")
def list_tariff_plans(active_only: bool = True, user: dict = Depends(_read)):
    return {"plans": billing_masters.list_tariff_plans(active_only=active_only)}


@router.post("/tariff-plans")
def upsert_tariff_plan(payload: TariffPlanIn, user: dict = Depends(_tariff)):
    plan = _handle(
        billing_service.upsert_tariff_plan,
        code=payload.code, name=payload.name, plan_type=payload.plan_type,
        is_default=payload.is_default, is_active=payload.is_active,
        effective_from=payload.effective_from, effective_to=payload.effective_to,
        notes=payload.notes,
    )
    return {"status": "success", "plan": plan}


@router.get("/tariff-rates")
def list_tariff_rates(
    tariff_plan_id: Optional[int] = None,
    service_code: Optional[str] = None,
    limit: int = Query(500, ge=1, le=2000),
    user: dict = Depends(_read),
):
    return {
        "rates": billing_service.list_tariff_rates(
            tariff_plan_id=tariff_plan_id, service_code=service_code, limit=limit
        )
    }


@router.post("/tariff-rates")
def set_tariff_rate(payload: TariffRateIn, user: dict = Depends(_tariff)):
    rate = _handle(
        billing_masters.set_tariff_rate,
        tariff_plan_id=payload.tariff_plan_id, service_code=payload.service_code,
        rate_paise=payload.rate_paise, ward_class=payload.ward_class,
        effective_from=payload.effective_from, created_by=user.get("id"),
    )
    return {"status": "success", "rate": rate}


@router.get("/packages")
def list_packages(tariff_plan_id: Optional[int] = None, user: dict = Depends(_read)):
    return {"packages": billing_service.list_package_rates(tariff_plan_id=tariff_plan_id)}


@router.post("/packages")
def upsert_package(payload: PackageRateIn, user: dict = Depends(_tariff)):
    package = _handle(
        billing_service.upsert_package_rate,
        tariff_plan_id=payload.tariff_plan_id, package_code=payload.package_code,
        name=payload.name, package_rate_paise=payload.package_rate_paise,
        ward_class=payload.ward_class, los_days_included=payload.los_days_included,
        inclusions=payload.inclusions, exclusions=payload.exclusions,
        external_scheme_code=payload.external_scheme_code,
        effective_from=payload.effective_from,
    )
    return {"status": "success", "package": package}


@router.get("/rate/{service_code}")
def resolve_rate(
    service_code: str,
    tariff_plan_id: Optional[int] = None,
    ward_class: Optional[str] = None,
    on_date: Optional[str] = None,
    user: dict = Depends(_read),
):
    """What one service costs — with ``rate_is_configured`` so the UI can say
    "Not configured" instead of drawing a zero."""
    resolution = billing_masters.resolve_rate(
        service_code, tariff_plan_id=tariff_plan_id, ward_class=ward_class,
        on_date=on_date,
    )
    return resolution.as_dict()


# ── Outpatients at the counter (opd-14, the 'opd' feature) ──────────────────

@router.get("/op-patients")
def list_op_patients(
    request: Request,
    date: Optional[str] = Query(None, max_length=10),
    _opd: None = Depends(require_opd_feature),
    user: dict = Depends(_read),
):
    """Outpatients with pending outpatient lines on one hospital-local day.

    The Charges picker merges these with the ward and discharged patients, so
    a walk-in who never had an admission can be billed at the counter. 404
    feature_disabled while the 'opd' feature is off (the gate runs first), so
    with it off the desk is exactly what it was.

    The department is the caller's own (permissions.department_scope), never
    a parameter. Names and UHIDs are PHI, so a non-empty answer writes one
    fail-closed phi_access row naming every listed patient; if it cannot be
    written nothing is returned (503).
    """
    dept = department_scope(user)
    result = _handle(billing_service.list_op_patients, day=date, scope_department=dept)
    if result["patients"]:
        try:
            log_phi_read(
                request=request, user=user, action_type="billing_op_patients_listed",
                summary_message=(
                    f"Outpatients to bill on {result['date']} listed "
                    f"({result['patient_count']} patients)"
                ),
                target_type="patient_list",
                target_id=f"op-patients-{result['date']}",
                metadata={
                    "visit_date": result["date"],
                    "patient_ids": [p["id"] for p in result["patients"]],
                    "result_count": result["patient_count"],
                    "truncated": result["truncated"],
                },
            )
        except AuditWriteError:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "audit_unavailable",
                    "message": ("The list could not be shown because the audit trail "
                                "is not available. Try again in a moment."),
                },
            )
    return result


# ── Charge capture ───────────────────────────────────────────────────────────

@router.get("/patients/{patient_id}/charges")
def list_patient_charges(
    patient_id: str,
    admission_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(500, ge=1, le=2000),
    # require_patient_access = the permission + "…and this patient is on your
    # ward" + a fail-closed phi_access audit row, in one dependency. It is the
    # house guard for every /{patient_id} route, and
    # tests/test_patient_scope_routes.py enumerates the live app to prove no new
    # one is added without it.
    user: dict = Depends(require_patient_access(
        BILLING_READ, audit_action="billing_charges_read")),
):
    _billing_enabled()
    charges = billing_service.list_charges(
        patient_id=patient_id, admission_id=admission_id, status=status, limit=limit
    )
    return {
        "patient_id": patient_id,
        "charges": charges,
        "summary": billing_service.patient_financial_summary(
            patient_id, admission_id=admission_id
        ),
    }


@router.post("/patients/{patient_id}/charges")
def post_charge(
    patient_id: str,
    payload: ChargeIn,
    # Same house guard as the reads: permission + ward scope off the PATH. No
    # audit_action — a mutation is already covered by the audit middleware and
    # by this module's own billing_charge_posted row.
    user: dict = Depends(require_patient_access(BILLING_READ, BILLING_WRITE)),
):
    _billing_enabled()
    charge = _handle(
        billing_service.add_charge,
        patient_id=patient_id, service_code=payload.service_code,
        description=payload.description, quantity_milli=payload.quantity_milli,
        unit_rate_paise=payload.unit_rate_paise, discount_paise=payload.discount_paise,
        discount_bp=payload.discount_bp, admission_id=payload.admission_id,
        encounter_type=payload.encounter_type, ward_class=payload.ward_class,
        tariff_plan_id=payload.tariff_plan_id, gst_treatment=payload.gst_treatment,
        gst_rate_bp=payload.gst_rate_bp, service_date=payload.service_date,
        idempotency_key=payload.idempotency_key, notes=payload.notes,
        charged_by=user.get("id"),
    )
    return {"status": "success", "charge": charge}


@router.post("/patients/{patient_id}/charges/capture")
def capture_charges(
    patient_id: str,
    payload: CaptureIn,
    user: dict = Depends(require_patient_access(BILLING_READ, BILLING_WRITE)),
):
    """Pull the chargeable clinical events Orb already recorded onto the bill.

    READ-ONLY against every clinical table. Events Orb cannot price come back in
    ``skipped`` with a reason — they are never posted at ₹0.
    """
    _billing_enabled()
    result = _handle(
        billing_service.capture_clinical_charges,
        patient_id=patient_id, admission_id=payload.admission_id,
        ward_class=payload.ward_class, dry_run=payload.dry_run, actor=user.get("id"),
    )
    return {"status": "success", **result}


@router.post("/patients/{patient_id}/bed-days")
def post_bed_days(
    patient_id: str,
    payload: BedDaysIn,
    user: dict = Depends(require_patient_access(BILLING_READ, BILLING_WRITE)),
):
    _billing_enabled()
    result = _handle(
        billing_service.post_bed_day_charges,
        admission_id=payload.admission_id, service_code=payload.service_code,
        through=payload.through, ward_class=payload.ward_class, actor=user.get("id"),
    )
    return {"status": "success", **result}


@router.post("/charges/{charge_id}/cancel")
def cancel_charge(charge_id: int, payload: ReasonIn, user: dict = Depends(_write)):
    charge = _handle(
        billing_service.cancel_charge, charge_id, reason=payload.reason,
        actor=user.get("id"),
    )
    return {"status": "success", "charge": charge}


# ── Invoices ─────────────────────────────────────────────────────────────────

@router.get("/invoices")
def list_invoices(
    patient_id: Optional[str] = None,
    admission_id: Optional[str] = None,
    status: Optional[str] = None,
    invoice_type: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(_read),
):
    if patient_id:
        enforce_patient_scope(user, patient_id)
    return {
        "invoices": billing_service.list_invoices(
            patient_id=patient_id, admission_id=admission_id, status=status,
            invoice_type=invoice_type, limit=limit,
        )
    }


@router.post("/patients/{patient_id}/invoices")
def create_invoice(
    patient_id: str,
    payload: InvoiceIn,
    user: dict = Depends(require_patient_access(BILLING_READ, BILLING_WRITE)),
):
    """Raise an IPD interim/final bill (or an advance demand) over pending charges."""
    _billing_enabled()
    invoice = _handle(
        billing_service.create_invoice,
        patient_id=patient_id, admission_id=payload.admission_id,
        invoice_type=payload.invoice_type, charge_ids=payload.charge_ids,
        encounter_type=payload.encounter_type, place_of_supply=payload.place_of_supply,
        interstate=payload.interstate, round_off=payload.round_off,
        issue=payload.issue, notes=payload.notes,
        idempotency_key=payload.idempotency_key, created_by=user.get("id"),
    )
    return {"status": "success", "invoice": invoice}


@router.post("/patients/{patient_id}/opd-invoice")
def create_opd_invoice(
    patient_id: str,
    payload: OpdInvoiceIn,
    user: dict = Depends(require_patient_access(BILLING_READ, BILLING_WRITE)),
):
    """The outpatient counter: type the lines, get a bill, in one round trip.

    The charges are posted first (each one priced or refused on its own), then a
    single invoice is raised over exactly those charge ids — never over "whatever
    is pending", which would sweep an unrelated line onto an OPD bill.
    """
    _billing_enabled()
    charge_ids: list[int] = []
    for line in payload.lines:
        charge = _handle(
            billing_service.add_charge,
            patient_id=patient_id, service_code=line.service_code,
            description=line.description, quantity_milli=line.quantity_milli,
            unit_rate_paise=line.unit_rate_paise, discount_paise=line.discount_paise,
            discount_bp=line.discount_bp, encounter_type="op",
            tariff_plan_id=line.tariff_plan_id, gst_treatment=line.gst_treatment,
            gst_rate_bp=line.gst_rate_bp, service_date=line.service_date,
            idempotency_key=line.idempotency_key, notes=line.notes,
            charged_by=user.get("id"),
        )
        charge_ids.append(int(charge["id"]))

    invoice = _handle(
        billing_service.create_invoice,
        patient_id=patient_id, invoice_type="final", charge_ids=charge_ids,
        encounter_type="op", place_of_supply=payload.place_of_supply,
        interstate=payload.interstate, issue=payload.issue, notes=payload.notes,
        created_by=user.get("id"),
    )
    return {"status": "success", "invoice": invoice, "charge_ids": charge_ids}


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, request: Request, user: dict = Depends(_read)):
    invoice = _handle(billing_service.get_invoice, invoice_id)
    enforce_patient_scope(user, invoice.get("patient_id"))
    log_phi_read(
        request=request, user=user, action_type="billing_invoice_read",
        summary_message=f"Invoice {invoice_id} read",
        patient_id=invoice.get("patient_id"), target_type="invoice",
        target_id=str(invoice_id),
    )
    return {"invoice": invoice}


@router.get("/invoices/{invoice_id}/print")
def print_invoice(invoice_id: int, request: Request, user: dict = Depends(_read)):
    """The printable/exportable document, with its own honesty notices attached."""
    view = _handle(billing_service.invoice_print_view, invoice_id)
    enforce_patient_scope(user, view["invoice"].get("patient_id"))
    log_phi_read(
        request=request, user=user, action_type="billing_invoice_printed",
        summary_message=f"Invoice {invoice_id} rendered for print/export",
        patient_id=view["invoice"].get("patient_id"), target_type="invoice",
        target_id=str(invoice_id),
    )
    return view


@router.post("/invoices/{invoice_id}/issue")
def issue_invoice(invoice_id: int, user: dict = Depends(_write)):
    invoice = _handle(billing_service.issue_invoice, invoice_id, actor=user.get("id"))
    return {"status": "success", "invoice": invoice}


@router.post("/invoices/{invoice_id}/cancel")
def cancel_invoice(invoice_id: int, payload: ReasonIn, user: dict = Depends(_write)):
    """Cancel with a typed reason. Nothing is deleted; the number is never reused."""
    invoice = _handle(
        billing_service.cancel_invoice, invoice_id, reason=payload.reason,
        actor=user.get("id"),
    )
    return {"status": "success", "invoice": invoice}


@router.post("/invoices/{invoice_id}/credit-note")
def credit_note(invoice_id: int, payload: ReasonIn, user: dict = Depends(_write)):
    """Reverse a paid/part-paid invoice with a second document."""
    invoice = _handle(
        billing_service.create_credit_note, invoice_id, reason=payload.reason,
        actor=user.get("id"),
    )
    return {"status": "success", "invoice": invoice}


# ── Patient money view + the discharge advisory ──────────────────────────────

@router.get("/patients/{patient_id}/summary")
def patient_summary(
    patient_id: str,
    admission_id: Optional[str] = None,
    user: dict = Depends(require_patient_access(
        BILLING_READ, audit_action="billing_summary_read")),
):
    _billing_enabled()
    return billing_service.patient_financial_summary(patient_id, admission_id=admission_id)


@router.get("/patients/{patient_id}/discharge-advisory")
def discharge_advisory(
    patient_id: str,
    admission_id: Optional[str] = None,
    record: bool = True,
    user: dict = Depends(require_patient_access(
        "discharge.read", audit_action="billing_discharge_advisory_read")),
):
    """The discharge warning. ALWAYS 200. NEVER a refusal.

    This endpoint is the one a discharge screen calls, so it is gated on
    ``discharge.read`` (doctor, nurse, admin) rather than on the desk's own
    ``billing.read`` — a warning the person doing the discharge cannot see is
    not a warning.

    IT RETURNS 200 EVEN WHEN THE PATIENT OWES MONEY, and even when a site has
    enabled ``ORB_DISCHARGE_FINANCIAL_ENFORCE``. ``blocks_discharge`` in the body
    is DATA: with the flag off it is always False, and with the flag on it is the
    site's own soft-stop for the discharge path to honour, override and record.
    Billing does not get to answer 4xx to a clinical question.
    """
    _billing_enabled()
    advisory = billing_service.discharge_financial_advisory(
        patient_id=patient_id, admission_id=admission_id,
        checked_by=user.get("id"), record=record,
    )
    return advisory
