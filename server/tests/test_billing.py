"""Billing — charge capture, invoice lifecycle, GST, and the discharge advisory.

WHAT THESE TESTS ARE FOR, in order of how much they matter:

  1. THE CLINICAL-SAFETY INVARIANT. With ORB_DISCHARGE_FINANCIAL_ENFORCE off —
     the default, and the pilot setting — nothing in billing can stop a
     discharge. Not an outstanding balance, not an unbillable encounter, not a
     missing tariff. `test_discharge_advisory_never_blocks_*` asserts that for
     every reachable financial state, and it is the test to reintroduce a defect
     against if you ever doubt this file.
  2. MONEY THAT IS ACTUALLY RIGHT. Totals and GST are asserted against
     HAND-COMPUTED PAISE, written out in the test so the arithmetic is visible.
     A test that recomputes the answer with the same helper the code uses proves
     only that the helper is deterministic.
  3. AN ISSUED INVOICE IS A RECORD. Legal transitions happen, illegal ones are
     refused with a reason, cancellation leaves an audit row on the hash chain,
     and nothing is ever deleted.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session, verify_audit_chain
from services import billing_masters, billing_service
from services.billing_service import BillingError, DischargeFinanciallyBlocked
from services.money import format_inr, rupees


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_patient():
    pid = f"PATBIL{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed) VALUES (?, ?, ?, 'W1', '3')",
            (pid, "Billing Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    return pid


def _make_admission(patient_id):
    aid = f"ADMBIL{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO admissions (id, patient_id, admitted_by, department, ward,
                                       bed, status, created_at)
               VALUES (?, ?, 'D001', 'General Medicine', 'W1', '3', 'active', ?)""",
            (aid, patient_id, "2026-08-10T09:00:00"),
        )
    return aid


@pytest.fixture()
def patient():
    return _make_patient()


@pytest.fixture()
def admission(patient):
    return _make_admission(patient)


@pytest.fixture()
def plan():
    """A tariff plan of this test's own, so parallel fixtures cannot collide."""
    code = f"TP{uuid.uuid4().hex[:6].upper()}"
    row = billing_service.upsert_tariff_plan(code=code, name="Test plan")
    return int(row["id"])


def _service_item(code_prefix, **kwargs):
    """A service item for one test.

    THE `TST-` PREFIX IS LOAD-BEARING. tests/test_money.py asserts that Orb
    seeds no prices anywhere — "a seeded price list would be fabricated data
    that looks real" — and excludes codes beginning `TST-` from that count.
    These fixtures write real prices into the shared session DB, so without the
    prefix this file makes that test fail depending on collection order.
    """
    code = f"TST-{code_prefix}{uuid.uuid4().hex[:6].upper()}"
    billing_masters.upsert_service_item(code=code, **kwargs)
    return code


@pytest.fixture()
def consult_code():
    """A healthcare service: GST-EXEMPT even though a rate sits on the item.

    The 18% is left there ON PURPOSE — it is the stray rate the exemption rule
    exists to neutralise.
    """
    return _service_item(
        "CONS", name="Consultation", category="consultation",
        gst_treatment="exempt", gst_rate_bp=1800,
    )


@pytest.fixture()
def pharmacy_code():
    return _service_item(
        "RXITM", name="Paracetamol 500mg", category="pharmacy",
        gst_treatment="taxable", gst_rate_bp=1200, hsn_sac="3004",
    )


# ── money: the arithmetic, against hand-computed paise ────────────────────────

def test_healthcare_service_is_never_taxed(patient, plan, consult_code):
    """₹1,500 consultation. GST-exempt, so the total IS the taxable value."""
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=consult_code, rate_paise=rupees("1500"),
    )
    charge = billing_service.add_charge(
        patient_id=patient, service_code=consult_code, tariff_plan_id=plan,
    )
    assert charge["unit_rate_paise"] == 150000        # ₹1500.00
    assert charge["gross_paise"] == 150000
    assert charge["taxable_value_paise"] == 150000
    assert charge["cgst_paise"] == 0
    assert charge["sgst_paise"] == 0
    assert charge["igst_paise"] == 0
    assert charge["total_paise"] == 150000
    assert charge["total_display"] == "₹1,500.00"


def test_pharmacy_line_splits_cgst_sgst_at_hand_computed_paise(patient, plan, pharmacy_code):
    """3 × ₹47.35 at 12% GST, intra-state.

    gross      = 3 × 4735p                      = 14205p
    taxable    = 14205p (no discount)
    CGST @ 6%  = 14205 × 600 / 10000 = 852.3    → 852p (half-up)
    SGST @ 6%  = the same, rounded separately   → 852p
    total      = 14205 + 852 + 852              = 15909p  (₹159.09)
    """
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=pharmacy_code, rate_paise=rupees("47.35"),
    )
    charge = billing_service.add_charge(
        patient_id=patient, service_code=pharmacy_code, tariff_plan_id=plan,
        quantity_milli=3000,
    )
    assert charge["gross_paise"] == 14205
    assert charge["taxable_value_paise"] == 14205
    assert charge["cgst_paise"] == 852
    assert charge["sgst_paise"] == 852
    assert charge["igst_paise"] == 0
    assert charge["total_paise"] == 15909
    assert charge["total_display"] == "₹159.09"


def test_discount_is_taken_before_tax(patient, plan, pharmacy_code):
    """₹100 line, 10% discount, 12% GST.

    gross    = 10000p; discount = 1000p; taxable = 9000p
    CGST 6%  = 540p; SGST 6% = 540p; total = 10080p
    Tax on the POST-discount value is s.15(3)(a) CGST Act — a discount on the
    face of the invoice reduces the taxable value.
    """
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=pharmacy_code, rate_paise=rupees("100"),
    )
    charge = billing_service.add_charge(
        patient_id=patient, service_code=pharmacy_code, tariff_plan_id=plan,
        discount_bp=1000,
    )
    assert charge["gross_paise"] == 10000
    assert charge["discount_paise"] == 1000
    assert charge["taxable_value_paise"] == 9000
    assert charge["cgst_paise"] == 540 and charge["sgst_paise"] == 540
    assert charge["total_paise"] == 10080


def test_invoice_foots_from_its_lines_and_records_the_round_off(
    patient, plan, consult_code, pharmacy_code
):
    """Two lines; the payable is the nearest rupee and the difference is a
    recorded round-off, never smeared into a tax figure.

    consultation ₹1500 exempt            → 150000p
    pharmacy 3 × ₹47.35 @12%             →  15909p
    total                                 = 165909p (₹1659.09)
    payable (nearest rupee)               = 165900p (₹1659.00)
    round-off                             =     -9p
    """
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=consult_code, rate_paise=rupees("1500"))
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=pharmacy_code, rate_paise=rupees("47.35"))
    billing_service.add_charge(
        patient_id=patient, service_code=consult_code, tariff_plan_id=plan)
    billing_service.add_charge(
        patient_id=patient, service_code=pharmacy_code, tariff_plan_id=plan,
        quantity_milli=3000)

    invoice = billing_service.create_invoice(patient_id=patient)
    assert invoice["subtotal_paise"] == 165909 - 1704   # gross, pre-tax
    assert invoice["cgst_paise"] == 852 and invoice["sgst_paise"] == 852
    assert invoice["tax_paise"] == 1704
    assert invoice["total_paise"] == 165909
    assert invoice["rounding_adjustment_paise"] == -9
    assert invoice["payable_paise"] == 165900
    assert invoice["payable_display"] == "₹1,659.00"
    # The foot is the SUM of the printed lines, exactly.
    assert sum(int(line["total_paise"]) for line in invoice["lines"]) == invoice["total_paise"]


def test_interstate_invoice_uses_igst_not_cgst_sgst(patient, plan, pharmacy_code):
    """The same ₹100 pharmacy line supplied inter-state: one IGST levy at 12%."""
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=pharmacy_code, rate_paise=rupees("100"))
    billing_service.add_charge(
        patient_id=patient, service_code=pharmacy_code, tariff_plan_id=plan)

    invoice = billing_service.create_invoice(
        patient_id=patient, interstate=True, place_of_supply="27",
    )
    assert invoice["igst_paise"] == 1200
    assert invoice["cgst_paise"] == 0 and invoice["sgst_paise"] == 0
    assert invoice["tax_paise"] == 1200
    assert invoice["total_paise"] == 11200


def test_amount_in_words_uses_indian_grouping():
    assert billing_service.amount_in_words(12345678) == (
        "Rupees One Lakh Twenty Three Thousand Four Hundred Fifty Six and "
        "Seventy Eight Paise Only"
    )
    assert billing_service.amount_in_words(0) == "Rupees Zero Only"
    assert billing_service.amount_in_words(-50000).startswith("Minus Rupees Five Hundred")


# ── "not configured" is not zero ──────────────────────────────────────────────

def test_unpriced_item_refuses_rather_than_billing_zero(patient, plan):
    """An item nobody priced cannot be charged. No row is written."""
    code = _service_item("UNPR", name="Unpriced procedure", category="procedure")
    with pytest.raises(BillingError) as exc:
        billing_service.add_charge(
            patient_id=patient, service_code=code, tariff_plan_id=plan)
    assert exc.value.code == "rate_not_configured"
    assert billing_service.list_charges(patient_id=patient) == []


def test_explicit_zero_default_rate_is_a_real_price(patient, plan):
    """A deliberately free item (a waived consultation) IS billable at ₹0 —
    which is why an unpriced item must be `None`, not `0`."""
    code = _service_item(
        "FREE", name="Waived review", category="consultation", default_rate_paise=0)
    charge = billing_service.add_charge(
        patient_id=patient, service_code=code, tariff_plan_id=plan)
    assert charge["total_paise"] == 0
    assert charge["rate_is_configured"] == 1


def test_charge_replay_is_idempotent(patient, plan, consult_code):
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=consult_code, rate_paise=rupees("500"))
    key = f"idem-{uuid.uuid4().hex}"
    first = billing_service.add_charge(
        patient_id=patient, service_code=consult_code, tariff_plan_id=plan,
        idempotency_key=key)
    second = billing_service.add_charge(
        patient_id=patient, service_code=consult_code, tariff_plan_id=plan,
        idempotency_key=key)
    assert second["id"] == first["id"]
    assert second["replayed"] is True
    assert len(billing_service.list_charges(patient_id=patient)) == 1


def test_charge_on_unknown_patient_is_refused(plan, consult_code):
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=consult_code, rate_paise=rupees("500"))
    with pytest.raises(BillingError) as exc:
        billing_service.add_charge(
            patient_id="NO-SUCH-PATIENT", service_code=consult_code, tariff_plan_id=plan)
    assert exc.value.code == "unknown_patient"


# ── capture from the clinical record ─────────────────────────────────────────

def _completed_order(patient_id, *, code, description="Serum lactate", order_type="lab"):
    oid = f"ORD{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO orders (id, patient_id, order_type, code, code_system,
                                   description, status, ordered_by)
               VALUES (?, ?, ?, ?, 'LOINC', ?, 'completed', 'D001')""",
            (oid, patient_id, order_type, code, description),
        )
    return oid


def test_capture_bills_completed_orders_and_names_what_it_cannot_price(
    patient, admission, plan, consult_code
):
    """A priced order is posted; an unpriceable one is reported, never zeroed."""
    lab_code = _service_item("LAB", name="Serum lactate", category="investigation")
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=lab_code, rate_paise=rupees("350"))
    # Make this patient's plan the default so capture resolves to it.
    billing_service.upsert_tariff_plan(
        code=f"D{uuid.uuid4().hex[:5].upper()}", name="unused")
    with db_session() as db:
        db.execute("UPDATE tariff_plans SET is_default = 0")
        db.execute("UPDATE tariff_plans SET is_default = 1 WHERE id = ?", (plan,))

    priced = _completed_order(patient, code=lab_code)
    unknown = _completed_order(patient, code="NO-SUCH-CODE", description="Mystery test")
    uncoded = _completed_order(patient, code=None, description="Uncoded test")

    result = billing_service.capture_clinical_charges(
        patient_id=patient, admission_id=admission)

    assert result["posted_count"] == 1
    assert result["posted"][0]["source_ref"] == priced
    assert result["posted"][0]["total_paise"] == 35000
    reasons = {s["source_ref"]: s["reason"] for s in result["skipped"]}
    assert reasons[unknown] == "no_service_item"
    assert reasons[uncoded] == "no_service_code"
    assert result["unpriceable_count"] == 2

    # Idempotent: a second sweep bills nothing again.
    again = billing_service.capture_clinical_charges(
        patient_id=patient, admission_id=admission)
    assert again["posted_count"] == 0
    assert {s["reason"] for s in again["skipped"] if s["source_ref"] == priced} == {
        "already_charged"}


def test_capture_dry_run_writes_nothing(patient, admission, plan):
    lab_code = _service_item("LAB", name="Serum lactate", category="investigation")
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=lab_code, rate_paise=rupees("350"))
    with db_session() as db:
        db.execute("UPDATE tariff_plans SET is_default = 0")
        db.execute("UPDATE tariff_plans SET is_default = 1 WHERE id = ?", (plan,))
    _completed_order(patient, code=lab_code)

    dry = billing_service.capture_clinical_charges(
        patient_id=patient, admission_id=admission, dry_run=True)
    assert dry["posted_count"] == 1
    assert dry["posted"][0]["would_post"] is True
    assert billing_service.list_charges(patient_id=patient) == []


def test_capture_ignores_orders_that_are_not_finished(patient, admission, plan):
    lab_code = _service_item("LAB", name="Serum lactate", category="investigation")
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=lab_code, rate_paise=rupees("350"))
    with db_session() as db:
        db.execute("UPDATE tariff_plans SET is_default = 0")
        db.execute("UPDATE tariff_plans SET is_default = 1 WHERE id = ?", (plan,))
        db.execute(
            """INSERT INTO orders (id, patient_id, order_type, code, description,
                                   status, ordered_by)
               VALUES (?, ?, 'lab', ?, 'Cancelled test', 'cancelled', 'D001')""",
            (f"ORD{uuid.uuid4().hex[:8].upper()}", patient, lab_code),
        )
        db.execute(
            """INSERT INTO orders (id, patient_id, order_type, code, description,
                                   status, ordered_by)
               VALUES (?, ?, 'medication', ?, 'A drug order', 'completed', 'D001')""",
            (f"ORD{uuid.uuid4().hex[:8].upper()}", patient, lab_code),
        )
    result = billing_service.capture_clinical_charges(patient_id=patient)
    # Neither a cancelled order nor a MEDICATION order is a charge: the drug is
    # billed when the pharmacy dispenses it.
    assert result["posted_count"] == 0
    assert result["skipped"] == []


def test_bed_days_are_one_charge_per_day_and_idempotent(patient, admission, plan):
    bed_code = _service_item("BED", name="General ward bed", category="bed")
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=bed_code, rate_paise=rupees("2500"),
        effective_from="2026-01-01")
    with db_session() as db:
        db.execute("UPDATE tariff_plans SET is_default = 0")
        db.execute("UPDATE tariff_plans SET is_default = 1 WHERE id = ?", (plan,))

    result = billing_service.post_bed_day_charges(
        admission_id=admission, service_code=bed_code, through="2026-08-12")
    assert result["posted_count"] == 3          # 10th, 11th, 12th August
    assert result["posted_total_paise"] == 750000
    assert result["unpriced_days"] == []

    again = billing_service.post_bed_day_charges(
        admission_id=admission, service_code=bed_code, through="2026-08-12")
    assert again["posted_count"] == 0
    assert again["already_present"] == 3


def test_bed_days_name_the_days_a_dated_tariff_cannot_price(patient, admission, plan):
    """A price list loaded on the 12th does not price the 10th and 11th.

    The honest answer is per-day: bill what is priced, NAME what is not. An
    all-or-nothing failure would leave a clerk with no bill and no idea why; a
    silent skip would leave a bill that is quietly two days short.
    """
    bed_code = _service_item("BED", name="General ward bed", category="bed")
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=bed_code, rate_paise=rupees("2500"),
        effective_from="2026-08-12")
    with db_session() as db:
        db.execute("UPDATE tariff_plans SET is_default = 0")
        db.execute("UPDATE tariff_plans SET is_default = 1 WHERE id = ?", (plan,))

    result = billing_service.post_bed_day_charges(
        admission_id=admission, service_code=bed_code, through="2026-08-12")
    assert result["posted_count"] == 1
    assert result["unpriced_days"] == ["2026-08-10", "2026-08-11"]


# ── invoice lifecycle ────────────────────────────────────────────────────────

def _one_charge(patient, plan, code, amount="1000"):
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=code, rate_paise=rupees(amount))
    return billing_service.add_charge(
        patient_id=patient, service_code=code, tariff_plan_id=plan)


def test_draft_holds_no_number_and_issue_allocates_one(patient, plan, consult_code):
    _one_charge(patient, plan, consult_code)
    draft = billing_service.create_invoice(patient_id=patient)
    assert draft["status"] == "draft"
    assert draft["invoice_number"] is None
    assert draft["number_display"].startswith("Draft")

    issued = billing_service.issue_invoice(draft["id"], actor="D001")
    assert issued["status"] == "issued"
    assert issued["invoice_number"]
    assert issued["issued_by"] == "D001"


def test_creating_an_invoice_claims_its_charges(patient, plan, consult_code):
    charge = _one_charge(patient, plan, consult_code)
    invoice = billing_service.create_invoice(patient_id=patient)
    charges = billing_service.list_charges(patient_id=patient)
    assert charges[0]["status"] == "billed"
    assert charges[0]["invoice_id"] == invoice["id"]
    assert invoice["lines"][0]["encounter_charge_id"] == charge["id"]

    # A second invoice over the same encounter has nothing left to bill.
    with pytest.raises(BillingError) as exc:
        billing_service.create_invoice(patient_id=patient)
    assert exc.value.code == "no_charges"


def test_illegal_transitions_are_refused(patient, plan, consult_code):
    _one_charge(patient, plan, consult_code)
    invoice = billing_service.create_invoice(patient_id=patient, issue=True)

    # Issued → issued (a second issue) returns the invoice rather than a second
    # number; issued → draft is not a transition at all.
    assert billing_service.issue_invoice(invoice["id"])["invoice_number"] == \
        invoice["invoice_number"]

    billing_service.cancel_invoice(invoice["id"], reason="Wrong patient selected")
    with pytest.raises(BillingError) as exc:
        billing_service.issue_invoice(invoice["id"])
    assert exc.value.code == "illegal_transition"


def test_cancellation_needs_a_reason_and_leaves_an_audit_trail(patient, plan, consult_code):
    _one_charge(patient, plan, consult_code)
    invoice = billing_service.create_invoice(patient_id=patient, issue=True)

    with pytest.raises(BillingError) as exc:
        billing_service.cancel_invoice(invoice["id"], reason="  ")
    assert exc.value.code == "reason_required"

    cancelled = billing_service.cancel_invoice(
        invoice["id"], reason="Charged to the wrong admission", actor="D001")
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_reason"] == "Charged to the wrong admission"
    # NOT DELETED: the number and the lines survive the cancellation.
    assert cancelled["invoice_number"] == invoice["invoice_number"]
    assert len(cancelled["lines"]) == 1
    # …and the charge is billable again.
    assert billing_service.list_charges(patient_id=patient)[0]["status"] == "pending"

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'billing_invoice_cancelled' "
            "AND target_id = ? ORDER BY id DESC LIMIT 1",
            (str(invoice["id"]),),
        ).fetchone()
    assert row is not None
    assert "Charged to the wrong admission" in row["summary_message"]
    assert row["actor_user_id"] == "D001"
    assert row["patient_id"] == patient
    assert verify_audit_chain()["ok"] is True


def _allocate_payment(patient_id, invoice_id, paise):
    """Record money against an invoice the way the payments module will."""
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO payments (patient_id, invoice_id, direction, amount_paise,
                                     method, purpose, status, received_by)
               VALUES (?, ?, 'in', ?, 'cash', 'invoice_payment', 'received', 'D001')""",
            (patient_id, int(invoice_id), int(paise)),
        )
        db.execute(
            "INSERT INTO payment_allocations (payment_id, invoice_id, amount_paise, "
            "allocated_by) VALUES (?, ?, ?, 'D001')",
            (cur.lastrowid, int(invoice_id), int(paise)),
        )


def test_payment_state_is_derived_not_asserted(patient, plan, consult_code):
    _one_charge(patient, plan, consult_code, amount="1000")
    invoice = billing_service.create_invoice(patient_id=patient, issue=True)
    assert invoice["balance_paise"] == 100000

    _allocate_payment(patient, invoice["id"], 40000)
    part = billing_service.get_invoice(invoice["id"])
    assert part["status"] == "partially_paid"
    assert part["amount_paid_paise"] == 40000
    assert part["balance_paise"] == 60000

    _allocate_payment(patient, invoice["id"], 60000)
    full = billing_service.get_invoice(invoice["id"])
    assert full["status"] == "paid"
    assert full["balance_paise"] == 0


def test_a_paid_invoice_is_credited_not_cancelled(patient, plan, consult_code):
    _one_charge(patient, plan, consult_code, amount="1000")
    invoice = billing_service.create_invoice(patient_id=patient, issue=True)
    _allocate_payment(patient, invoice["id"], 100000)

    with pytest.raises(BillingError) as exc:
        billing_service.cancel_invoice(invoice["id"], reason="Billed in error")
    assert exc.value.code == "invoice_has_payments"

    note = billing_service.create_credit_note(
        invoice["id"], reason="Billed in error", actor="D001")
    assert note["invoice_type"] == "credit_note"
    assert note["status"] == "issued"
    assert note["invoice_number"]
    assert note["supersedes_invoice_id"] == invoice["id"]
    assert note["payable_paise"] == -100000
    assert note["lines"][0]["total_paise"] == -100000

    # The original invoice still exists, still paid — it happened.
    original = billing_service.get_invoice(invoice["id"])
    assert original["status"] == "paid"
    # …and the patient now owes nothing: the credit note's negative balance
    # cancels the invoice out at the account level.
    summary = billing_service.patient_financial_summary(patient)
    assert summary["outstanding_paise"] == -100000 + 0


def test_a_credit_note_on_a_rounded_invoice_nets_to_zero(patient, plan, consult_code):
    """The source invoice rounds ₹1,000.50 up to ₹1,001.00. Its credit note used
    to reverse the lines (−₹1,000.50) and not the 50 paise round-off, so a fully
    reversed bill left the patient owing 50 paise (U314)."""
    _one_charge(patient, plan, consult_code, amount="1000.50")
    invoice = billing_service.create_invoice(patient_id=patient, issue=True)
    assert invoice["rounding_adjustment_paise"] == 50
    assert invoice["payable_paise"] == 100100

    note = billing_service.create_credit_note(
        invoice["id"], reason="Billed in error", actor="D001")
    assert note["payable_paise"] == -100100
    assert note["rounding_adjustment_paise"] == -50
    assert note["total_paise"] + note["rounding_adjustment_paise"] == note["payable_paise"]
    assert invoice["payable_paise"] + note["payable_paise"] == 0

    summary = billing_service.patient_financial_summary(patient)
    assert summary["outstanding_paise"] == 0


def test_money_owed_back_is_called_a_refund_not_a_clear_balance(patient, plan, consult_code):
    """After a credit note on a paid bill the hospital owes the patient. The
    advisory said "No outstanding balance recorded" (U315)."""
    _one_charge(patient, plan, consult_code, amount="1000")
    invoice = billing_service.create_invoice(patient_id=patient, issue=True)
    _allocate_payment(patient, invoice["id"], 100000)
    billing_service.create_credit_note(invoice["id"], reason="Billed in error", actor="D001")

    advisory = billing_service.discharge_financial_advisory(patient_id=patient, record=False)
    assert advisory["advisory_status"] == "clear"
    assert advisory["blocks_discharge"] is False
    assert advisory["headline"] == f"Refund due to the patient: {format_inr(100000)}"
    assert any("due back to the patient" in r for r in advisory["reasons"])


def _inv_series_next_number() -> int:
    """The next number the INV series will hand out.

    Read rather than allocated: calling next_document_number to "check" would
    itself consume one, which is the very thing these assertions are about.
    """
    with db_session() as db:
        row = db.execute(
            "SELECT next_number FROM document_number_series WHERE series_code = 'INV'"
        ).fetchone()
    return int(row["next_number"])


@pytest.mark.parametrize("pooled", [False, True])
def test_opd_counter_later_price_failure_rolls_back_all_lines(patient, consult_code, monkeypatch, pooled):
    import db_backend
    monkeypatch.setattr(db_backend, "POOL_ENABLED", pooled)
    unpriced = _service_item("NOPRICE", name="Unpriced outpatient item", category="consultation")
    before = _inv_series_next_number()
    with pytest.raises(BillingError) as refused:
        billing_service.create_opd_invoice_atomic(
            patient_id=patient, created_by="D001", lines=[
                {"service_code": consult_code, "unit_rate_paise": 150000},
                {"service_code": unpriced},
            ],
        )
    assert refused.value.code == "rate_not_configured"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM encounter_charges WHERE patient_id=?", (patient,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM invoices WHERE patient_id=?", (patient,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE patient_id=?", (patient,)).fetchone()[0] == 0
    assert _inv_series_next_number() == before


@pytest.mark.parametrize("pooled", [False, True])
def test_opd_counter_issue_audit_exception_restores_number_and_charges(patient, consult_code, monkeypatch, pooled):
    import db_backend
    monkeypatch.setattr(db_backend, "POOL_ENABLED", pooled)
    original_audit = billing_service._audit
    def fail_issue(db, **kwargs):
        if kwargs["action_type"] == "billing_invoice_issued":
            raise RuntimeError("Audit unavailable")
        return original_audit(db, **kwargs)
    monkeypatch.setattr(billing_service, "_audit", fail_issue)
    before = _inv_series_next_number()
    with pytest.raises(RuntimeError, match="Audit unavailable"):
        billing_service.create_opd_invoice_atomic(
            patient_id=patient, created_by="D001", lines=[{"service_code": consult_code, "unit_rate_paise": 150000}],
        )
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM encounter_charges WHERE patient_id=?", (patient,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM invoices WHERE patient_id=?", (patient,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE patient_id=?", (patient,)).fetchone()[0] == 0
    assert _inv_series_next_number() == before


@pytest.mark.parametrize("pooled", [False, True])
def test_opd_counter_commits_one_issued_invoice_and_exact_lines(patient, consult_code, monkeypatch, pooled):
    import db_backend
    monkeypatch.setattr(db_backend, "POOL_ENABLED", pooled)
    result = billing_service.create_opd_invoice_atomic(
        patient_id=patient, created_by="D001", lines=[
            {"service_code": consult_code, "unit_rate_paise": 150000},
            {"service_code": consult_code, "unit_rate_paise": 50000},
        ],
    )
    assert result["invoice"]["status"] == "issued"
    assert result["invoice"]["invoice_number"]
    assert result["invoice"]["payable_paise"] == 200000
    with db_session() as db:
        rows = db.execute("SELECT id,status,invoice_id FROM encounter_charges WHERE patient_id=? ORDER BY id", (patient,)).fetchall()
        assert [row["id"] for row in rows] == result["charge_ids"]
        assert all(row["status"] == "billed" and row["invoice_id"] == result["invoice"]["id"] for row in rows)


@pytest.mark.parametrize("pooled", [False, True])
def test_explicit_connection_uses_staged_payer_and_tariff(patient, plan, consult_code, monkeypatch, pooled):
    """Caller-owned work sees its own policy/rate writes in either DB mode."""
    import db_backend
    monkeypatch.setattr(db_backend, "POOL_ENABLED", pooled)
    payer = billing_masters.upsert_payer(
        code="TST-PAYER-" + uuid.uuid4().hex, name="Synthetic staged payer",
        payer_type="insurer", tariff_plan_id=plan,
    )
    billing_masters.set_tariff_rate(tariff_plan_id=plan, service_code=consult_code, rate_paise=150000)
    with db_session(immediate=True) as db:
        billing_masters.record_patient_payer_on(
            db, patient_id=patient, payer_code=payer["code"], created_by="D001",
        )
        db.execute("UPDATE tariff_rates SET rate_paise=175001 WHERE tariff_plan_id=? AND service_code=?",
                   (plan, consult_code))
        charge = billing_service.add_charge(patient_id=patient, service_code=consult_code,
                                            charged_by="D001", _db=db)
        assert charge["tariff_plan_id"] == plan
        assert charge["unit_rate_paise"] == 175001
        prior_name = db.execute("SELECT legal_name FROM facilities WHERE id=1").fetchone()[0]
        db.execute("UPDATE facilities SET legal_name='Synthetic transaction-local supplier' WHERE id=1")
        invoice = billing_service.create_invoice(patient_id=patient, charge_ids=[charge["id"]],
                                                  encounter_type="op", created_by="D001", _db=db)
        assert invoice["supplier_name"] == "Synthetic transaction-local supplier"
        # Restore the shared synthetic fixture before issue; the draft read
        # above must still see the caller's current identity, without caches.
        db.execute("UPDATE facilities SET legal_name=? WHERE id=1", (prior_name,))
        invoice = billing_service.issue_invoice(invoice["id"], actor="D001", _db=db)
        assert invoice["payer_id"] == payer["id"]
        assert invoice["payer"]["id"] == payer["id"]
        assert invoice["payable_paise"] == invoice["payer_responsibility_paise"] == 175001
        assert invoice["patient_responsibility_paise"] == 0


def test_a_second_credit_note_against_one_invoice_is_refused(patient, plan, consult_code):
    """ONE INVOICE, ONE CREDIT NOTE.

    `create_credit_note` reverses the invoice IN FULL, so running it twice
    credits the patient the whole bill twice AND burns a second GST credit-note
    number for one document — a number the hospital then has to account for to an
    auditor. Both halves are asserted: the refusal, and the untouched series.

    PROVEN ABLE TO FAIL: neuter either `_existing_credit_note` guard in
    billing_service.create_credit_note and this test goes red — with the
    pre-check gone the INV series advances for the refused note (assert 4 == 3);
    with both gone a second ₹-1,000 credit note is created and no error is
    raised at all.
    """
    _one_charge(patient, plan, consult_code, amount="1000")
    invoice = billing_service.create_invoice(patient_id=patient, issue=True)
    _allocate_payment(patient, invoice["id"], 100000)

    first = billing_service.create_credit_note(
        invoice["id"], reason="Billed in error", actor="D001")
    assert first["payable_paise"] == -100000

    series_before = _inv_series_next_number()
    with pytest.raises(BillingError) as exc:
        billing_service.create_credit_note(
            invoice["id"], reason="Billed in error (double-tap)", actor="D001")
    assert exc.value.code == "already_credited"
    # The refusal points at the document that already exists…
    assert exc.value.detail["credit_note_id"] == first["id"]
    assert exc.value.detail["credit_note_number"] == first["invoice_number"]
    # …and NO number was drawn for the refused one.
    assert _inv_series_next_number() == series_before

    # Exactly one credit note, and the patient's position is credited ONCE:
    # invoice +₹1,000 (paid) and credit note −₹1,000 → −₹1,000 outstanding, not
    # −₹2,000.
    assert len(billing_service.get_invoice(invoice["id"])["credit_notes"]) == 1
    assert billing_service.patient_financial_summary(patient)["outstanding_paise"] == -100000


def test_print_view_says_what_it_does_not_have(patient, plan, consult_code):
    _one_charge(patient, plan, consult_code, amount="1000")
    invoice = billing_service.create_invoice(patient_id=patient, issue=True)
    view = billing_service.invoice_print_view(invoice["id"])
    assert view["supplier"]["gstin"] == "Not configured"
    assert "not a GST tax invoice" in view["supplier"]["notice"]
    # Migration 20 seeds the INV series with is_configured = 0.
    assert view["number_series_configured"] is False
    assert view["number_notice"]
    assert view["amount_in_words"] == "Rupees One Thousand Only"


# ── the discharge advisory: the clinical-safety invariant ────────────────────

def _owing_patient(patient, plan, consult_code, amount="5000"):
    _one_charge(patient, plan, consult_code, amount=amount)
    return billing_service.create_invoice(patient_id=patient, issue=True)


@pytest.fixture(autouse=True)
def _enforcement_off(monkeypatch):
    """Every test starts from the PILOT setting: enforcement off.

    Set explicitly rather than relied upon, so a test that turns it on cannot
    leak into the next one and so the default is asserted rather than assumed.
    """
    monkeypatch.setattr(config, "DISCHARGE_FINANCIAL_ENFORCE", False, raising=False)


def test_the_shipped_default_is_advisory():
    """The flag's default is the invariant. If this fails, nothing else matters."""
    import importlib
    fresh = importlib.import_module("config")
    assert fresh.DISCHARGE_FINANCIAL_ENFORCE is False


def test_discharge_advisory_warns_with_the_amount(patient, admission, plan, consult_code):
    _owing_patient(patient, plan, consult_code, amount="5000")
    advisory = billing_service.discharge_financial_advisory(
        patient_id=patient, admission_id=admission, checked_by="D001")

    assert advisory["advisory_status"] == "outstanding"
    assert advisory["outstanding_paise"] == 500000
    assert advisory["outstanding_display"] == "₹5,000.00"
    assert format_inr(500000) in advisory["headline"]
    assert any("outstanding" in r for r in advisory["reasons"])
    assert advisory["message"].startswith("Advisory only")


def _stage_financial_state(scenario, plan, consult_code):
    """Put a fresh patient into one of the three states an advisory can report."""
    pid = _make_patient()
    aid = _make_admission(pid)
    if scenario == "outstanding":
        # An issued, unpaid invoice PLUS a captured-but-unbilled charge: money
        # owed on a document, and money owed that has not reached one.
        _owing_patient(pid, plan, consult_code, amount="5000")
        other = _service_item("XRAY", name="Chest X-ray", category="imaging")
        billing_masters.set_tariff_rate(
            tariff_plan_id=plan, service_code=other, rate_paise=rupees("800"))
        billing_service.add_charge(
            patient_id=pid, admission_id=aid, service_code=other, tariff_plan_id=plan)
    elif scenario == "unknown":
        # A completed order Orb has no price for at all: the bill CANNOT be
        # computed, which is neither "clear" nor a fabricated zero.
        _completed_order(pid, code="UNPRICEABLE-CODE", description="Mystery test")
    elif scenario == "clear":
        _one_charge(pid, plan, consult_code, amount="1000")
        invoice = billing_service.create_invoice(patient_id=pid, issue=True)
        _allocate_payment(pid, invoice["id"], 100000)
    return pid, aid


@pytest.mark.parametrize("scenario", ["outstanding", "unknown", "clear"])
def test_discharge_advisory_never_blocks_with_the_flag_off(
    scenario, plan, consult_code
):
    """THE CLINICAL-SAFETY INVARIANT — asserted across EVERY reachable state.

    Owing money, owing money that is not even billed yet, an encounter Orb cannot
    price at all, and a patient who has paid in full: in none of them may billing
    stop a discharge while ORB_DISCHARGE_FINANCIAL_ENFORCE is off.

    PROVEN ABLE TO FAIL: drop the `enforced and` from `blocks` in
    billing_service.discharge_financial_advisory and the `outstanding` case goes
    red on both the advisory and the assert hook.
    """
    pid, aid = _stage_financial_state(scenario, plan, consult_code)

    advisory = billing_service.discharge_financial_advisory(
        patient_id=pid, admission_id=aid, checked_by="D001")

    assert advisory["enforced"] is False
    assert advisory["blocks_discharge"] is False
    assert advisory["advisory_status"] == scenario
    assert advisory["message"].startswith("Advisory only")

    # The hook a discharge path would call RETURNS; it does not raise.
    result = billing_service.assert_discharge_permitted(
        patient_id=pid, admission_id=aid, checked_by="D001")
    assert result["blocks_discharge"] is False

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM discharge_financial_checks WHERE patient_id = ? "
            "ORDER BY id DESC LIMIT 1", (pid,),
        ).fetchone()
    assert row["advisory_status"] == scenario
    assert row["enforced"] == 0
    assert row["blocked_discharge"] == 0
    assert row["discharge_proceeded"] == 1
    if scenario == "unknown":
        # 'unknown' stores NULL, never 0 — the column must not claim a figure
        # the check could not compute.
        assert row["outstanding_paise"] is None
    if scenario == "outstanding":
        assert advisory["unbilled_charge_count"] == 1
        assert advisory["outstanding_paise"] == 500000


def test_a_clear_patient_reads_as_clear(patient, admission, plan, consult_code):
    _one_charge(patient, plan, consult_code, amount="1000")
    invoice = billing_service.create_invoice(patient_id=patient, issue=True)
    _allocate_payment(patient, invoice["id"], 100000)
    advisory = billing_service.discharge_financial_advisory(
        patient_id=patient, admission_id=admission)
    assert advisory["advisory_status"] == "clear"
    assert advisory["blocks_discharge"] is False


def _unpriced_pending_charge(patient_id, admission_id=None,
                             description="General Medicine consultation"):
    """The row frontdesk_service._raise_consultation_charge writes when nobody
    has priced the service: captured, `pending`, `rate_is_configured = 0`, and
    a 0 in every money column that is a placeholder, not a price ("UNPRICED IS
    NOT FREE"). Inserted directly because billing_service.add_charge refuses to
    create one — the refusal is the module's own rule, and the frontdesk path
    is the one that legitimately bypasses it."""
    with db_session() as db:
        db.execute(
            """INSERT INTO encounter_charges
                 (patient_id, admission_id, encounter_type, service_code,
                  description, quantity_milli, unit_rate_paise, rate_is_configured,
                  gross_paise, taxable_value_paise, total_paise, status, source,
                  source_ref, service_date, charged_by)
               VALUES (?, ?, 'op', 'TST-UNPRICED-OPD', ?, 1000, 0, 0,
                       0, 0, 0, 'pending', 'manual', 'VIS-TEST',
                       '2026-08-12', 'FD001')""",
            (patient_id, admission_id, description),
        )


def test_an_unpriced_captured_charge_is_not_reported_as_free(plan):
    """THE ₹0.00 LIE. An unpriced captured charge legitimately carries
    total_paise = 0, so a summary that only sums totals reports it as "worth
    ₹0.00" and the advisory concludes "No outstanding balance recorded" — a
    clerk reads "this costs nothing" where the truth is "nobody has priced
    this". The honest advisory answer is the third one: `unknown`.

    PROVEN ABLE TO FAIL: drop `unpriced_charge_count` from the status
    condition in discharge_financial_advisory and the headline assert goes red.
    """
    pid = _make_patient()
    aid = _make_admission(pid)
    _unpriced_pending_charge(pid, aid)

    summary = billing_service.patient_financial_summary(pid, admission_id=aid)
    assert summary["unbilled_charge_count"] == 1
    assert summary["unpriced_charge_count"] == 1
    # The paise fields still sum what is known — they are floors, and the
    # count is the flag that says so.
    assert summary["unbilled_total_paise"] == 0

    advisory = billing_service.discharge_financial_advisory(
        patient_id=pid, admission_id=aid, checked_by="D001")
    assert advisory["advisory_status"] == "unknown"
    assert advisory["headline"] != "No outstanding balance recorded"
    assert "NOT KNOWN" in advisory["headline"]
    assert any("no configured price" in r for r in advisory["reasons"])
    # No reason may price the unpriced charge at ₹0.00.
    assert not any("₹0.00" in r for r in advisory["reasons"])
    # And the clinical-safety invariant holds: 'unknown' never blocks.
    assert advisory["blocks_discharge"] is False


def test_an_unpriced_charge_cannot_become_a_zero_rupee_invoice_line(plan):
    """THE ₹0.00 LIE, ONE STEP LATER — and the worse half of it.

    `create_invoice` used to sweep EVERY pending charge with no
    `rate_is_configured` filter, so the frontdesk's "nobody has priced this" row
    became a numbered GST line reading ₹0.00 on an issued document — a bill that
    asserts the consultation was free. It also moved the charge to `billed`,
    which drops it out of `unpriced_charge_count` and silently turns the
    discharge advisory from "NOT KNOWN — this bill is incomplete" into a clean
    "No outstanding balance recorded". The warning the whole ₹0-aggregate fix
    exists to produce disappeared at the exact moment the bill was raised.

    PROVEN ABLE TO FAIL: delete the `unpriced` guard in
    billing_service.create_invoice (the block raising `charge_not_priced`) and
    every assertion below goes red — the invoice is created, its single line
    reads ₹0.00, and the advisory reads 'clear'.
    """
    pid = _make_patient()
    aid = _make_admission(pid)
    _unpriced_pending_charge(pid, aid)

    with pytest.raises(BillingError) as exc:
        billing_service.create_invoice(patient_id=pid, admission_id=aid)
    assert exc.value.code == "charge_not_priced"
    # The refusal NAMES the charge, so the clerk knows what to price.
    assert exc.value.detail["charges"][0]["service_code"] == "TST-UNPRICED-OPD"
    assert "₹0.00" in str(exc.value)          # …as the thing it refuses to print

    # Nothing was written: no invoice, and the charge is still waiting to be
    # priced rather than sitting on a document as free.
    assert billing_service.list_invoices(patient_id=pid) == []
    charges = billing_service.list_charges(patient_id=pid)
    assert len(charges) == 1
    assert charges[0]["status"] == "pending"
    assert charges[0]["invoice_id"] is None

    # And the advisory still says the bill cannot be computed.
    advisory = billing_service.discharge_financial_advisory(
        patient_id=pid, admission_id=aid, checked_by="D001")
    assert advisory["advisory_status"] == "unknown"
    assert "NOT KNOWN" in advisory["headline"]
    assert advisory["unpriced_charge_count"] == 1
    assert advisory["blocks_discharge"] is False   # never, whatever billing says


def test_an_explicitly_named_unpriced_charge_is_refused_too(plan):
    """The sweep is not the only way in: naming the charge id must not be a way
    around the rule."""
    pid = _make_patient()
    _unpriced_pending_charge(pid)
    charge_id = billing_service.list_charges(patient_id=pid)[0]["id"]

    with pytest.raises(BillingError) as exc:
        billing_service.create_invoice(patient_id=pid, charge_ids=[charge_id])
    assert exc.value.code == "charge_not_priced"
    assert exc.value.detail["charges"][0]["id"] == charge_id


def test_one_unpriced_charge_stops_the_bill_without_consuming_the_priced_ones(plan):
    """A mixed encounter: ₹800 X-ray (priced) + one unpriced consultation.

    The refusal must roll back WHOLE — the priced charge stays `pending`, not
    half-claimed on an invoice that was never written. Once the unpriced charge
    is dealt with (here: cancelled with a reason, the clerk's other option
    besides pricing it), the bill raises with hand-computed paise:

        X-ray ₹800.00, GST-exempt        =  80000p
        payable, self-pay, rounded       =  80000p
    """
    pid = _make_patient()
    aid = _make_admission(pid)
    xray = _service_item("XRAY", name="Chest X-ray", category="imaging")
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=xray, rate_paise=rupees("800"))
    priced = billing_service.add_charge(
        patient_id=pid, admission_id=aid, service_code=xray, tariff_plan_id=plan)
    _unpriced_pending_charge(pid, aid)

    with pytest.raises(BillingError) as exc:
        billing_service.create_invoice(patient_id=pid, admission_id=aid)
    assert exc.value.code == "charge_not_priced"
    assert [c["service_code"] for c in exc.value.detail["charges"]] == ["TST-UNPRICED-OPD"]
    assert billing_service.list_charges(patient_id=pid, status="billed") == []

    unpriced_id = next(
        c["id"] for c in billing_service.list_charges(patient_id=pid)
        if c["service_code"] == "TST-UNPRICED-OPD"
    )
    billing_service.cancel_charge(
        unpriced_id, reason="No price on the list; billed outside Orb", actor="D001")

    invoice = billing_service.create_invoice(patient_id=pid, admission_id=aid, issue=True)
    assert invoice["payable_paise"] == 80000
    assert invoice["payable_display"] == "₹800.00"
    assert len(invoice["lines"]) == 1
    assert invoice["lines"][0]["encounter_charge_id"] == priced["id"]
    # No line on the issued document reads ₹0.00.
    assert all(int(line["total_paise"]) != 0 for line in invoice["lines"])


def test_the_unbilled_reason_prices_only_what_is_priced(plan):
    """One priced pending charge (₹800) beside one unpriced. The ₹-figure must
    describe only the priced charge — "2 charge(s) worth ₹800.00" would fold a
    fabricated 0 into the total — and the unpriced one gets its own sentence."""
    pid = _make_patient()
    aid = _make_admission(pid)
    xray = _service_item("XRAY", name="Chest X-ray", category="imaging")
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=xray, rate_paise=rupees("800"))
    billing_service.add_charge(
        patient_id=pid, admission_id=aid, service_code=xray, tariff_plan_id=plan)
    _unpriced_pending_charge(pid, aid)

    advisory = billing_service.discharge_financial_advisory(
        patient_id=pid, admission_id=aid, checked_by="D001")
    assert advisory["advisory_status"] == "unknown"
    assert advisory["unbilled_charge_count"] == 2
    assert advisory["unpriced_charge_count"] == 1
    assert ("1 captured charge worth ₹800.00 is not on an invoice yet"
            in advisory["reasons"])
    assert any("1 captured charge has no configured price" in r
               for r in advisory["reasons"])


def test_a_genuinely_free_charge_still_reads_as_clear(plan, consult_code):
    """`rate_is_configured = 1` with a rate of 0 is a REAL price — a waived camp
    consultation (billing_masters: "a genuinely free item is a real thing").
    It must not trip the unpriced machinery: free is a fact, unpriced is the
    absence of one, and this test is what keeps the two distinguishable."""
    pid = _make_patient()
    aid = _make_admission(pid)
    billing_service.add_charge(
        patient_id=pid, admission_id=aid, service_code=consult_code,
        tariff_plan_id=plan, unit_rate_paise=0)

    summary = billing_service.patient_financial_summary(pid, admission_id=aid)
    assert summary["unbilled_charge_count"] == 1
    assert summary["unpriced_charge_count"] == 0

    advisory = billing_service.discharge_financial_advisory(
        patient_id=pid, admission_id=aid)
    assert advisory["advisory_status"] == "clear"
    assert advisory["headline"] == "No outstanding balance recorded"
    # "worth ₹0.00" is TRUE of this charge: somebody decided it is free.
    assert ("1 captured charge worth ₹0.00 is not on an invoice yet"
            in advisory["reasons"])


def test_enforcement_blocks_only_when_explicitly_enabled(
    patient, admission, plan, consult_code, monkeypatch
):
    _owing_patient(patient, plan, consult_code, amount="5000")

    # Off (the default): permitted.
    assert billing_service.assert_discharge_permitted(
        patient_id=patient, admission_id=admission)["blocks_discharge"] is False

    # On: and only now does it refuse.
    monkeypatch.setattr(config, "DISCHARGE_FINANCIAL_ENFORCE", True, raising=False)
    with pytest.raises(DischargeFinanciallyBlocked) as exc:
        billing_service.assert_discharge_permitted(
            patient_id=patient, admission_id=admission, checked_by="D001")
    assert exc.value.advisory["advisory_status"] == "outstanding"

    # A typed override always clears it, and is recorded.
    overridden = billing_service.assert_discharge_permitted(
        patient_id=patient, admission_id=admission, checked_by="D001",
        override_reason="Family paying at the counter now")
    assert overridden["blocks_discharge"] is False
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM discharge_financial_checks WHERE patient_id = ? "
            "ORDER BY id DESC LIMIT 1", (patient,),
        ).fetchone()
    assert row["override_reason"] == "Family paying at the counter now"
    assert row["discharge_proceeded"] == 1


def test_enforcement_never_blocks_on_an_unknown_position(
    patient, admission, plan, monkeypatch
):
    """Even an enforcing site may not hold a patient on a number it does not have."""
    monkeypatch.setattr(config, "DISCHARGE_FINANCIAL_ENFORCE", True, raising=False)
    _completed_order(patient, code="UNPRICEABLE-CODE", description="Mystery test")

    advisory = billing_service.assert_discharge_permitted(
        patient_id=patient, admission_id=admission)
    assert advisory["advisory_status"] == "unknown"
    assert advisory["enforced"] is True
    assert advisory["blocks_discharge"] is False


# ── HTTP surface ─────────────────────────────────────────────────────────────

app = FastAPI()
from routes.billing_routes import router as billing_router  # noqa: E402

app.include_router(billing_router)
client = TestClient(app)


def _auth(role="admin", uid="ADMBILL"):
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,'x')",
            (uid, f"{role} biller", role),
        )
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def test_route_requires_permission(patient):
    resp = client.get(f"/api/billing/patients/{patient}/charges",
                      headers=_auth(role="nurse", uid="NURBILL"))
    assert resp.status_code == 403


def test_discharge_advisory_route_answers_200_while_money_is_owed(
    patient, admission, plan, consult_code
):
    """The route may warn. It may not refuse."""
    _owing_patient(patient, plan, consult_code, amount="5000")
    resp = client.get(
        f"/api/billing/patients/{patient}/discharge-advisory",
        params={"admission_id": admission},
        headers=_auth(role="doctor", uid="DOCBILL"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocks_discharge"] is False
    assert body["outstanding_display"] == "₹5,000.00"


def test_routes_disappear_when_the_module_is_switched_off(patient, monkeypatch):
    """ORB_BILLING=0 removes the module: 404, not 403 — absent, not forbidden."""
    monkeypatch.setattr(config, "BILLING_ENABLED", False, raising=False)
    resp = client.get("/api/billing/config", headers=_auth())
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "module_disabled"


def test_unpriced_item_surfaces_as_422_with_a_code(patient, plan):
    code = _service_item("UNPR", name="Unpriced procedure", category="procedure")
    resp = client.post(
        f"/api/billing/patients/{patient}/charges",
        json={"service_code": code, "tariff_plan_id": plan},
        headers=_auth(),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "rate_not_configured"


def test_opd_invoice_in_one_call(patient, plan, consult_code):
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan, service_code=consult_code, rate_paise=rupees("600"))
    resp = client.post(
        f"/api/billing/patients/{patient}/opd-invoice",
        json={"lines": [{"service_code": consult_code, "tariff_plan_id": plan}]},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    invoice = resp.json()["invoice"]
    assert invoice["status"] == "issued"
    assert invoice["encounter_type"] == "op"
    assert invoice["payable_paise"] == 60000


def test_charge_day_is_the_hospital_local_day_of_a_utc_timestamp(monkeypatch):
    """The OPD lane hands record_charge the consultation's full UTC timestamp.
    Slicing its first ten characters put a visit at 01:30 IST on the previous
    day, on the bill and in the tariff lookup."""
    from datetime import timedelta, timezone as tz

    ist = tz(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(billing_service, "hospital_timezone", lambda: ist)
    assert billing_service._iso_day("2026-08-12T20:00:00+00:00") == "2026-08-13"
    assert billing_service._iso_day("2026-08-12 20:00:00") == "2026-08-13"
    # A bare day was never an instant and is not shifted.
    assert billing_service._iso_day("2026-08-12") == "2026-08-12"
