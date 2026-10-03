"""Payments, deposits, refunds, discount approvals and the day-end cash count.

WHAT THESE TESTS ARE FOR. This module is where money the hospital has physically
received gets written down. Every assertion below is an INVARIANT, not an
example, and each block names the failure it holds shut:

  1. Part-payments are exact to the paise across many splits — the classic
     rounding leak that leaves a bill "₹0.01 outstanding" forever.
  2. A refund can never exceed what was taken.
  3. An over-payment becomes an ADVANCE, never a negative balance.
  4. A receipt is IMMUTABLE — an edit fails, a reversal succeeds, and both halves
     stay on the ledger.
  5. Day-end totals equal the sum of their parts. This repo has been burned by a
     headline figure that disagreed with its own breakdown, so it is asserted
     from the raw rows rather than from the same query that produced it.

PROVE-CAN-FAIL. Receipt immutability (block 4) was verified by making
`amend_payment` actually perform the UPDATE it refuses; `test_editing_a_receipt_
is_refused` and `test_the_receipt_row_is_untouched_after_a_refused_edit` both go
red, and the day-end reconciliation test goes red with them.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from auth import create_token
from database import db_session
from services import payments_service as ps
from services.hospital_time import hospital_today
from services.money import format_inr, rupees, split_shares
from routes.payments_routes import router as payments_router


# ── fixtures ─────────────────────────────────────────────────────────────────

def _pid() -> str:
    """A patient id unique to one test.

    TEST-ISOLATION LAW (tests/test_nurse_mode.py states it at length): these
    tables are shared across the whole session database, so a fixed id would let
    one test's receipts land in another test's day-end total. Every test gets its
    own patient; the day-end assertions scope themselves by counter code for the
    same reason.

    The patient is REGISTERED (a patients row), because the counter now refuses
    money for an id that names nobody (TestUnknownPatientIsRefused). It is
    registered as discharged so it never joins a ward census in another module;
    the money paths do not read the status. `_admit` is the one that admits.
    """
    pid = f"PAYT{uuid.uuid4().hex[:10].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'discharged')",
            (pid, f"Payments Patient {pid[-4:]}", f"MRN-{pid}"),
        )
    return pid


def _make_invoice(patient_id: str, payable_paise: int, *, status: str = "issued") -> int:
    """An issued invoice for `payable_paise`.

    Written directly rather than through the billing module: this suite owns the
    payment side of the boundary and must not go red because a sibling module's
    signature moved.
    """
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO invoices (patient_id, status, subtotal_paise, taxable_value_paise, "
            "total_paise, payable_paise, balance_paise) VALUES (?,?,?,?,?,?,?)",
            (patient_id, status, payable_paise, payable_paise, payable_paise,
             payable_paise, payable_paise),
        )
        return int(cur.lastrowid)


def _invoice(invoice_id: int) -> dict:
    with db_session() as db:
        row = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    return {k: row[k] for k in row.keys()}


def _counter() -> str:
    return f"CTR{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(payments_router)
    return TestClient(app)


def _mk_user(role="admin", department=None):
    """A test account. DEPARTMENT DEFAULTS TO NULL, and that is the point.

    permissions.department_scope treats a user row with no department as
    UNSCOPED — the codebase's existing convention for a hospital-wide governance
    account. A cashier or finance clerk is hospital-wide by nature (money is
    taken at one counter for every ward), so that is the shape a payments account
    has. Pass a department to build the OPPOSITE case: a ward-scoped account,
    which `require_patient_access` must hold to its own ward.
    """
    uid = f"{role[:3].upper()}{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Test {role}", role, department),
        )
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_cashier():
    """A `cashier` account — a plain _mk_user since migration 33 widened the
    users.role CHECK to admit the role. (Before that, this helper had to
    suspend CHECK enforcement for one INSERT: `INSERT OR IGNORE` swallowed the
    violation silently and the minted token 401'd against a row that never
    landed. roles.py now keeps ROLE_PERMISSIONS and every rendering of the
    CHECK from diverging again.)"""
    return _mk_user("cashier")


def _admit(patient_id: str, department: str) -> None:
    """A real patient + admission, so the ward-scope derivation has something to
    derive from (a patient's department lives on their ADMISSION, not on
    patients.ward — see permissions.PATIENT_DEPARTMENT_SQL)."""
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, bed, ward, age, sex, "
            "diagnosis, status) VALUES (?, 'Payments Test', ?, 'B-1', 'PW', 40, 'F', "
            "'Observation', 'admitted')",
            (patient_id, f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        # _pid() already registered the row, so the INSERT above is ignored;
        # give it the admitted shape this fixture has always promised.
        db.execute(
            "UPDATE patients SET name = 'Payments Test', bed = 'B-1', ward = 'PW', "
            "status = 'admitted' WHERE id = ?",
            (patient_id,),
        )
        # admissions.admitted_by is a real FK to users(id), and admissions.id is
        # a TEXT primary key with no default — both have to be supplied.
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('PAYTESTADM', 'Payments Fixture', 'admin', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, status, admitted_by) "
            "VALUES (?, ?, ?, 'active', 'PAYTESTADM')",
            (f"ADM-{uuid.uuid4().hex[:10]}", patient_id, department),
        )


# ══ 1. Part payments are exact to the paise ══════════════════════════════════
#
# THE FAILURE: an invoice is settled in six instalments and ends up a paise
# short, so it never reaches 'paid' and shows an outstanding balance nobody can
# clear. It is invisible in any one payment and only appears in the total.

class TestPartPaymentArithmetic:
    @pytest.mark.parametrize("splits", [
        [1, 1],                       # halves of an odd amount
        [1, 1, 1],                    # thirds — the classic ⅓ leak
        [1, 2, 3],
        [7, 11, 13],                  # coprime weights, ugly remainders
        [1, 1, 1, 1, 1, 1, 1],        # seven ways
        [999, 1, 1],                  # one huge share, two crumbs
    ])
    def test_the_parts_settle_the_invoice_exactly(self, splits):
        patient = _pid()
        # An amount with an awkward paise tail, so every split has a remainder.
        payable = rupees("13457.33")
        invoice = _make_invoice(patient, payable)
        parts = split_shares(payable, splits)
        assert sum(parts) == payable, "the fixture itself must not lose a paise"

        allocated_total = 0
        for part in parts:
            result = ps.record_payment(
                patient_id=patient, amount_paise=part, method="cash",
                invoice_id=invoice, received_by="clerk-a",
            )
            allocated_total += result["allocated_paise"]
            # No instalment may over-pay: every one of them lands entirely on
            # the invoice while there is still something owing.
            assert result["advance_paise"] == 0

        inv = _invoice(invoice)
        assert allocated_total == payable
        assert inv["amount_paid_paise"] == payable
        assert inv["balance_paise"] == 0
        assert inv["status"] == "paid"

    def test_the_invoice_paid_total_always_equals_the_sum_of_its_allocations(self):
        """THE INVARIANT: `amount_paid_paise` is DERIVED, never accumulated. A
        running counter is what drifts; a re-derivation cannot."""
        patient = _pid()
        invoice = _make_invoice(patient, rupees("2500"))
        for amount in ("1", "0.01", "999.99", "1500"):
            ps.record_payment(patient_id=patient, amount_paise=rupees(amount),
                              method="cash", invoice_id=invoice, received_by="clerk-a")
        with db_session() as db:
            allocated = int(db.execute(
                "SELECT COALESCE(SUM(amount_paise),0) AS s FROM payment_allocations "
                "WHERE invoice_id = ? AND reversed_at IS NULL", (invoice,),
            ).fetchone()["s"])
        assert _invoice(invoice)["amount_paid_paise"] == allocated

    def test_a_single_paise_payment_is_recorded_and_allocated(self):
        patient = _pid()
        invoice = _make_invoice(patient, rupees("10"))
        result = ps.record_payment(patient_id=patient, amount_paise=1, method="cash",
                                   invoice_id=invoice, received_by="clerk-a")
        assert result["amount_paise"] == 1
        assert result["allocated_paise"] == 1
        assert _invoice(invoice)["balance_paise"] == rupees("10") - 1

    def test_a_zero_receipt_is_refused(self):
        """A receipt for nothing still prints a document that looks like a
        transaction."""
        with pytest.raises(ps.PaymentError):
            ps.record_payment(patient_id=_pid(), amount_paise=0, method="cash",
                              received_by="clerk-a")

    def test_a_float_amount_never_reaches_a_money_column(self):
        with pytest.raises(ps.PaymentError):
            ps.record_payment(patient_id=_pid(), amount_paise=100.0, method="cash",
                              received_by="clerk-a")

    def test_a_traceable_method_needs_its_reference(self):
        """A UPI payment with no UTR cannot be matched to the bank statement, and
        an unmatchable payment is indistinguishable from one that never came."""
        with pytest.raises(ps.PaymentError, match="reference"):
            ps.record_payment(patient_id=_pid(), amount_paise=rupees("100"),
                              method="upi", received_by="clerk-a")

    def test_a_replayed_receipt_takes_the_money_once(self):
        """A double-tapped counter button, or the offline queue replaying."""
        patient = _pid()
        invoice = _make_invoice(patient, rupees("500"))
        key = f"idem-{uuid.uuid4().hex}"
        first = ps.record_payment(patient_id=patient, amount_paise=rupees("500"),
                                  method="cash", invoice_id=invoice,
                                  received_by="clerk-a", idempotency_key=key)
        second = ps.record_payment(patient_id=patient, amount_paise=rupees("500"),
                                   method="cash", invoice_id=invoice,
                                   received_by="clerk-a", idempotency_key=key)
        assert second["id"] == first["id"]
        assert second["replayed"] is True
        assert _invoice(invoice)["amount_paid_paise"] == rupees("500")


# ══ 1b. Money is applied only to the paying patient's own invoice ════════════
#
# THE FAILURE: invoice ids are small sequential integers typed at a counter. A
# payment taken for patient A against a mistyped id was allocated to patient
# B's invoice: B's bill cleared with A's money, A's bill stayed open, and the
# receipt number was spent either way.

class TestCrossPatientAllocationIsRefused:
    def _spy_numbers(self, monkeypatch):
        calls = []
        real = ps.next_document_number

        def spy(*a, **kw):
            calls.append(a)
            return real(*a, **kw)
        monkeypatch.setattr(ps, "next_document_number", spy)
        return calls

    def test_a_payment_against_another_patients_invoice_is_refused(self, monkeypatch):
        payer, owner = _pid(), _pid()
        invoice = _make_invoice(owner, rupees("3000"))
        numbers = self._spy_numbers(monkeypatch)
        with pytest.raises(ps.PaymentError, match="belongs to another patient. Nothing was taken"):
            ps.record_payment(patient_id=payer, amount_paise=rupees("3000"),
                              method="cash", invoice_id=invoice, received_by="clerk-a")
        assert numbers == [], "a refused payment must not consume a receipt number"
        assert _invoice(invoice)["amount_paid_paise"] == 0
        with db_session() as db:
            taken = db.execute("SELECT COUNT(*) c FROM payments WHERE patient_id = ?",
                               (payer,)).fetchone()["c"]
        assert taken == 0
        assert ps.deposit_balance(payer)["balance_paise"] == 0

    def test_the_owners_payment_still_allocates(self, monkeypatch):
        owner = _pid()
        invoice = _make_invoice(owner, rupees("3000"))
        numbers = self._spy_numbers(monkeypatch)
        result = ps.record_payment(patient_id=owner, amount_paise=rupees("3000"),
                                   method="cash", invoice_id=invoice, received_by="clerk-a")
        assert result["allocated_paise"] == rupees("3000")
        assert len(numbers) == 1

    def test_one_patients_advance_cannot_settle_anothers_invoice(self, monkeypatch):
        payer, owner = _pid(), _pid()
        ps.record_payment(patient_id=payer, amount_paise=rupees("4000"),
                          method="cash", purpose="deposit", received_by="clerk-a")
        invoice = _make_invoice(owner, rupees("1500"))
        numbers = self._spy_numbers(monkeypatch)
        with pytest.raises(ps.PaymentError, match="belongs to another patient"):
            ps.apply_deposit_to_invoice(patient_id=payer, invoice_id=invoice,
                                        amount_paise=rupees("1500"), applied_by="clerk-a")
        assert numbers == []
        assert ps.deposit_balance(payer)["balance_paise"] == rupees("4000")
        assert _invoice(invoice)["amount_paid_paise"] == 0

    def test_a_refund_cannot_be_raised_against_another_patients_invoice(self):
        payer, owner = _pid(), _pid()
        invoice = _make_invoice(owner, rupees("1000"))
        ps.record_payment(patient_id=owner, amount_paise=rupees("1000"),
                          method="cash", invoice_id=invoice, received_by="clerk-a")
        with pytest.raises(ps.PaymentError, match="belongs to another patient"):
            ps.request_refund(patient_id=payer, amount_paise=rupees("1000"),
                              reason="mistyped", requested_by="clerk-a",
                              invoice_id=invoice)


# ══ 2. An over-payment is an advance, never a negative balance ═══════════════
#
# THE FAILURE: a patient hands over ₹5,000 against a ₹3,000 bill and the invoice
# stores a -₹2,000 "balance". Every report that sums balances is now wrong, and
# the ₹2,000 the hospital is actually holding is recorded nowhere a clerk can
# find it when the patient asks for it back.

class TestOverPaymentBecomesAnAdvance:
    def test_the_excess_lands_on_the_deposit_ledger(self):
        patient = _pid()
        invoice = _make_invoice(patient, rupees("3000"))
        result = ps.record_payment(patient_id=patient, amount_paise=rupees("5000"),
                                   method="cash", invoice_id=invoice, received_by="clerk-a")
        assert result["allocated_paise"] == rupees("3000")
        assert result["advance_paise"] == rupees("2000")
        assert result["over_payment"] is True
        assert ps.deposit_balance(patient)["balance_paise"] == rupees("2000")

    def test_the_invoice_balance_never_goes_negative(self):
        patient = _pid()
        invoice = _make_invoice(patient, rupees("3000"))
        ps.record_payment(patient_id=patient, amount_paise=rupees("9999"),
                          method="cash", invoice_id=invoice, received_by="clerk-a")
        inv = _invoice(invoice)
        assert inv["balance_paise"] == 0
        assert inv["amount_paid_paise"] == rupees("3000")
        assert inv["status"] == "paid"

    def test_paying_a_settled_invoice_is_entirely_an_advance(self):
        patient = _pid()
        invoice = _make_invoice(patient, rupees("1000"))
        ps.record_payment(patient_id=patient, amount_paise=rupees("1000"),
                          method="cash", invoice_id=invoice, received_by="clerk-a")
        again = ps.record_payment(patient_id=patient, amount_paise=rupees("250"),
                                  method="cash", invoice_id=invoice, received_by="clerk-a")
        assert again["allocated_paise"] == 0
        assert again["advance_paise"] == rupees("250")
        assert _invoice(invoice)["amount_paid_paise"] == rupees("1000")

    def test_an_advance_can_be_applied_to_an_invoice_later(self):
        patient = _pid()
        ps.record_payment(patient_id=patient, amount_paise=rupees("4000"),
                          method="cash", purpose="deposit", received_by="clerk-a")
        invoice = _make_invoice(patient, rupees("1500"))
        applied = ps.apply_deposit_to_invoice(
            patient_id=patient, invoice_id=invoice, amount_paise=rupees("1500"),
            applied_by="clerk-a",
        )
        assert applied["method"] == "adjustment", \
            "an applied advance is not new cash and must not be counted at the drawer"
        assert ps.deposit_balance(patient)["balance_paise"] == rupees("2500")
        assert _invoice(invoice)["status"] == "paid"

    def test_applying_more_advance_than_is_held_is_refused(self):
        patient = _pid()
        ps.record_payment(patient_id=patient, amount_paise=rupees("100"),
                          method="cash", purpose="deposit", received_by="clerk-a")
        invoice = _make_invoice(patient, rupees("5000"))
        with pytest.raises(ps.PaymentError, match="advance balance"):
            ps.apply_deposit_to_invoice(patient_id=patient, invoice_id=invoice,
                                        amount_paise=rupees("500"), applied_by="clerk-a")

    def test_applying_more_advance_than_the_invoice_owes_is_refused(self):
        patient = _pid()
        ps.record_payment(patient_id=patient, amount_paise=rupees("9000"),
                          method="cash", purpose="deposit", received_by="clerk-a")
        invoice = _make_invoice(patient, rupees("500"))
        with pytest.raises(ps.PaymentError, match="over-pay"):
            ps.apply_deposit_to_invoice(patient_id=patient, invoice_id=invoice,
                                        amount_paise=rupees("900"), applied_by="clerk-a")

    @pytest.mark.parametrize("dues,total", [
        ([rupees("100"), rupees("200"), rupees("300")], rupees("333.33")),
        ([rupees("1"), rupees("1"), rupees("1")], rupees("2.00")),
        ([rupees("7.77"), rupees("11.11")], rupees("5.55")),
        ([rupees("10"), rupees("0")], rupees("7.50")),
    ])
    def test_a_planned_split_never_loses_a_paise(self, dues, total):
        shares = ps.plan_deposit_split(total, dues)
        assert sum(shares) == min(total, sum(dues))
        for share, due in zip(shares, dues):
            assert 0 <= share <= due


# ══ 3. A refund can never exceed what was taken ══════════════════════════════
#
# THE FAILURE: two clerks each raise a full refund against the same ₹2,000
# receipt; each one passes a naive "amount <= payment.amount" check, and ₹4,000
# leaves the hospital against ₹2,000 that came in.

class TestRefundCeiling:
    def _paid(self, amount="2000"):
        patient = _pid()
        receipt = ps.record_payment(patient_id=patient, amount_paise=rupees(amount),
                                    method="cash", purpose="deposit", received_by="clerk-a")
        return patient, receipt

    def test_a_refund_larger_than_the_receipt_is_refused(self):
        patient, receipt = self._paid("2000")
        with pytest.raises(ps.PaymentError, match="refundable"):
            ps.request_refund(patient_id=patient, amount_paise=rupees("2000.01"),
                              reason="patient asked", requested_by="clerk-a",
                              payment_id=receipt["id"])

    def test_a_second_refund_cannot_reclaim_the_same_money(self):
        patient, receipt = self._paid("2000")
        ps.request_refund(patient_id=patient, amount_paise=rupees("2000"),
                          reason="cancelled procedure", requested_by="clerk-a",
                          payment_id=receipt["id"])
        with pytest.raises(ps.PaymentError, match="refundable"):
            ps.request_refund(patient_id=patient, amount_paise=rupees("2000"),
                              reason="cancelled procedure", requested_by="clerk-b",
                              payment_id=receipt["id"])

    def test_a_pending_refund_already_counts_against_the_ceiling(self):
        """`requested` and `approved` reserve the money, not just `paid`. Without
        this, two requests raised in the same minute both pass."""
        patient, receipt = self._paid("1000")
        ps.request_refund(patient_id=patient, amount_paise=rupees("600"),
                          reason="partial", requested_by="clerk-a",
                          payment_id=receipt["id"])
        limits = ps.refundable_paise(patient, payment_id=receipt["id"])
        assert limits["refundable_paise"] == rupees("400")
        with pytest.raises(ps.PaymentError):
            ps.request_refund(patient_id=patient, amount_paise=rupees("401"),
                              reason="too much", requested_by="clerk-a",
                              payment_id=receipt["id"])

    def test_a_refund_of_exactly_what_was_paid_is_allowed(self):
        patient, receipt = self._paid("1234.56")
        refund = ps.request_refund(patient_id=patient, amount_paise=rupees("1234.56"),
                                   reason="duplicate payment", requested_by="clerk-a",
                                   payment_id=receipt["id"])
        assert refund["amount_paise"] == rupees("1234.56")
        assert refund["refund_number"]

    def test_a_refund_always_names_what_it_refunds(self):
        """Never a bare negative entry: every refund carries a reason, a number,
        and a link to a receipt, an invoice, or the patient's advance balance."""
        patient, receipt = self._paid("500")
        refund = ps.request_refund(patient_id=patient, amount_paise=rupees("500"),
                                   reason="wrong counter", requested_by="clerk-a",
                                   payment_id=receipt["id"])
        assert refund["payment_id"] == receipt["id"]
        assert refund["reason"] == "wrong counter"
        with pytest.raises(ps.PaymentError, match="reason"):
            ps.request_refund(patient_id=patient, amount_paise=1, reason="  ",
                              requested_by="clerk-a", payment_id=receipt["id"])

    def test_the_full_lifecycle_pays_out_a_money_out_receipt(self):
        patient, receipt = self._paid("3000")
        refund = ps.request_refund(patient_id=patient, amount_paise=rupees("1200"),
                                   reason="procedure cancelled", requested_by="clerk-a",
                                   payment_id=receipt["id"])
        decided = ps.decide_refund(refund["id"], approve=True, decided_by="manager-1")
        assert decided["status"] == "approved"
        paid = ps.pay_refund(refund["id"], method="cash", paid_by="clerk-a")
        assert paid["status"] == "paid"
        assert paid["payment"]["direction"] == "out"
        assert paid["payment"]["amount_paise"] == rupees("1200")
        # THE LEDGER, not a signed amount on the same row: the direction carries
        # the sign, and the patient's net is what changed.
        snapshot = ps.patient_financial_snapshot(patient)
        assert snapshot["paid_paise"] == rupees("3000")
        assert snapshot["refunded_paise"] == rupees("1200")
        assert snapshot["net_paise"] == rupees("1800")

    def test_a_refund_cannot_be_approved_by_the_person_who_asked(self):
        patient, receipt = self._paid("800")
        refund = ps.request_refund(patient_id=patient, amount_paise=rupees("800"),
                                   reason="overcharged", requested_by="clerk-a",
                                   payment_id=receipt["id"])
        with pytest.raises(ps.PaymentError, match="second person"):
            ps.decide_refund(refund["id"], approve=True, decided_by="Clerk-A")

    def test_an_unapproved_refund_cannot_be_paid(self):
        patient, receipt = self._paid("800")
        refund = ps.request_refund(patient_id=patient, amount_paise=rupees("800"),
                                   reason="overcharged", requested_by="clerk-a",
                                   payment_id=receipt["id"])
        with pytest.raises(ps.PaymentError, match="approved"):
            ps.pay_refund(refund["id"], method="cash", paid_by="clerk-a")

    def test_a_deposit_refund_is_capped_by_the_advance_actually_held(self):
        """Money already spent on a bill is not sitting there to hand back."""
        patient = _pid()
        ps.record_payment(patient_id=patient, amount_paise=rupees("5000"),
                          method="cash", purpose="deposit", received_by="clerk-a")
        invoice = _make_invoice(patient, rupees("4000"))
        ps.apply_deposit_to_invoice(patient_id=patient, invoice_id=invoice,
                                    amount_paise=rupees("4000"), applied_by="clerk-a")
        assert ps.deposit_balance(patient)["balance_paise"] == rupees("1000")
        with pytest.raises(ps.PaymentError, match="refundable|held"):
            ps.request_refund(patient_id=patient, amount_paise=rupees("1500"),
                              reason="discharge", requested_by="clerk-a")
        ok = ps.request_refund(patient_id=patient, amount_paise=rupees("1000"),
                               reason="discharge", requested_by="clerk-a")
        assert ok["amount_paise"] == rupees("1000")


# ══ 4. A receipt is immutable ════════════════════════════════════════════════
#
# THE FAILURE: a clerk edits yesterday's ₹5,000 receipt down to ₹500 and pockets
# the difference. Nothing in the ledger disagrees with itself afterwards, which
# is exactly why editing must be impossible rather than merely discouraged.
#
# PROVEN ABLE TO FAIL: making `amend_payment` perform the UPDATE turns both of
# the first two tests here red.

class TestReceiptImmutability:
    def _receipt(self):
        patient = _pid()
        invoice = _make_invoice(patient, rupees("5000"))
        receipt = ps.record_payment(patient_id=patient, amount_paise=rupees("5000"),
                                    method="cash", invoice_id=invoice,
                                    received_by="clerk-a")
        return patient, invoice, receipt

    def test_editing_a_receipt_is_refused(self):
        _, _, receipt = self._receipt()
        with pytest.raises(ps.ImmutablePaymentError):
            ps.amend_payment(receipt["id"], amount_paise=rupees("500"))

    def test_the_receipt_row_is_untouched_after_a_refused_edit(self):
        """The refusal is not enough on its own — the row has to still say what
        it said. This is the assertion that goes red if `amend_payment` ever
        starts writing."""
        _, _, receipt = self._receipt()
        with pytest.raises(ps.ImmutablePaymentError):
            ps.amend_payment(receipt["id"], amount_paise=1, method="upi",
                             patient_id="SOMEONE-ELSE")
        after = ps.get_payment(receipt["id"])
        assert after["amount_paise"] == rupees("5000")
        assert after["method"] == "cash"
        assert after["patient_id"] == receipt["patient_id"]
        assert after["receipt_number"] == receipt["receipt_number"]

    def test_a_reversal_succeeds_and_leaves_both_halves_visible(self):
        patient, invoice, receipt = self._receipt()
        result = ps.reverse_payment(receipt["id"], reason="wrong patient",
                                    reversed_by="manager-1")
        original, reversal = result["original"], result["reversal"]

        # The original is still there, still saying what it said.
        assert original["amount_paise"] == rupees("5000")
        assert original["method"] == "cash"
        assert original["status"] == "reversed"
        assert original["cancel_reason"] == "wrong patient"

        # The reversal is a NEW document, linked, opposing.
        assert reversal["id"] != original["id"]
        assert reversal["direction"] == "out"
        assert reversal["amount_paise"] == rupees("5000")
        assert reversal["reversal_of_payment_id"] == original["id"]
        assert reversal["receipt_number"] != original["receipt_number"]

        # And the money is back off the invoice.
        inv = _invoice(invoice)
        assert inv["amount_paid_paise"] == 0
        assert inv["balance_paise"] == rupees("5000")
        assert inv["status"] == "issued"

    def test_a_reversal_needs_a_reason_and_a_person(self):
        _, _, receipt = self._receipt()
        with pytest.raises(ps.PaymentError):
            ps.reverse_payment(receipt["id"], reason="   ", reversed_by="manager-1")
        with pytest.raises(ps.PaymentError):
            ps.reverse_payment(receipt["id"], reason="typo", reversed_by="")

    def test_a_receipt_cannot_be_reversed_twice(self):
        _, _, receipt = self._receipt()
        ps.reverse_payment(receipt["id"], reason="first", reversed_by="manager-1")
        with pytest.raises(ps.PaymentError, match="cannot be reversed again"):
            ps.reverse_payment(receipt["id"], reason="second", reversed_by="manager-1")

    def test_reversing_a_receipt_withdraws_the_advance_it_created(self):
        patient = _pid()
        invoice = _make_invoice(patient, rupees("1000"))
        receipt = ps.record_payment(patient_id=patient, amount_paise=rupees("2500"),
                                    method="cash", invoice_id=invoice,
                                    received_by="clerk-a")
        assert ps.deposit_balance(patient)["balance_paise"] == rupees("1500")
        ps.reverse_payment(receipt["id"], reason="wrong amount", reversed_by="manager-1")
        assert ps.deposit_balance(patient)["balance_paise"] == 0
        assert _invoice(invoice)["amount_paid_paise"] == 0

    def test_the_ledger_nets_to_zero_after_a_reversal(self):
        """The correction is visible as two movements, not as an absence."""
        patient, _, receipt = self._receipt()
        ps.reverse_payment(receipt["id"], reason="duplicate", reversed_by="manager-1")
        snapshot = ps.patient_financial_snapshot(patient)
        # Net is the money the hospital actually still holds: nothing. EXACTLY
        # nothing: this used to read -₹5,000 (the OUT leg counted, the IN leg
        # not), and `<= 0` let that through. A reversal is neither a payment nor
        # a refund, so the patient's figures leave out both legs.
        assert snapshot["net_paise"] == 0
        assert snapshot["paid_paise"] == 0
        assert snapshot["refunded_paise"] == 0, "a reversal is not a refund"
        rows = ps.list_payments(patient_id=patient, limit=50)
        assert len(rows) == 2, "both halves of the correction must remain on the ledger"
        assert {r["direction"] for r in rows} == {"in", "out"}

    def test_the_person_who_took_a_payment_cannot_reverse_it(self):
        """Four eyes, as on a refund decision. Voiding your own receipt is the
        till fraud receipt immutability exists to stop; the approve permission
        alone never stopped it for an admin, who holds both verbs.

        PROVEN ABLE TO FAIL: delete the received_by check in reverse_payment and
        the first reversal succeeds."""
        patient, invoice, receipt = self._receipt()        # received_by clerk-a
        with pytest.raises(ps.PaymentError, match="person who took the payment"):
            ps.reverse_payment(receipt["id"], reason="typo", reversed_by="clerk-a")
        # Same person, different spacing and case: still the same person.
        with pytest.raises(ps.PaymentError, match="person who took the payment"):
            ps.reverse_payment(receipt["id"], reason="typo", reversed_by="  CLERK-A ")
        # The refusal changed nothing: no opposing row, the receipt still live,
        # the invoice still paid.
        assert ps.get_payment(receipt["id"])["status"] == "received"
        assert len(ps.list_payments(patient_id=patient, limit=50)) == 1
        assert _invoice(invoice)["amount_paid_paise"] == rupees("5000")
        # A second person can.
        result = ps.reverse_payment(receipt["id"], reason="typo", reversed_by="manager-1")
        assert result["original"]["status"] == "reversed"

    def test_a_reversed_receipt_does_not_shrink_the_refund_ceiling(self):
        """₹1,000 and ₹500 taken, the ₹1,000 reversed: the hospital holds ₹500
        of this patient's money, so ₹500 is refundable. Counting only the
        reversal's OUT leg made it ₹0 (₹500 in, ₹1,000 out)."""
        patient = _pid()
        wrong = ps.record_payment(patient_id=patient, amount_paise=rupees("1000"),
                                  method="cash", purpose="deposit", received_by="clerk-a")
        ps.record_payment(patient_id=patient, amount_paise=rupees("500"),
                          method="cash", purpose="deposit", received_by="clerk-a")
        ps.reverse_payment(wrong["id"], reason="keyed twice", reversed_by="manager-1")
        assert ps.refundable_paise(patient)["refundable_paise"] == rupees("500")
        assert ps.patient_financial_snapshot(patient)["net_paise"] == rupees("500")


# ══ 5. Discounts are a person's decision ═════════════════════════════════════
#
# THE FAILURE: ₹40,000 comes off a bill and the system can say only that "a
# discount was applied". A reduction with no name on it is indistinguishable from
# theft, and it is the one number an auditor will always ask about.

class TestDiscountApprovals:
    def test_an_approval_records_who_and_why(self):
        patient = _pid()
        request = ps.request_discount(
            patient_id=patient, reason="hardship — sanctioned by trust fund",
            requested_by="clerk-a", requested_paise=rupees("2500"),
            reason_code="HARDSHIP",
        )
        assert request["status"] == "requested"
        assert request["approved_paise"] is None
        assert request["approved_display"] == "Not decided"

        decided = ps.decide_discount(request["id"], approve=True, decided_by="manager-1",
                                     decision_note="approved at counter")
        assert decided["status"] == "approved"
        assert decided["decided_by"] == "manager-1"
        assert decided["approved_paise"] == rupees("2500")
        assert decided["reason"] == "hardship — sanctioned by trust fund"

    def test_the_approver_cannot_be_the_requester(self):
        request = ps.request_discount(patient_id=_pid(), reason="hardship",
                                      requested_by="clerk-a",
                                      requested_paise=rupees("100"))
        with pytest.raises(ps.PaymentError, match="second name"):
            ps.decide_discount(request["id"], approve=True, decided_by="clerk-a")

    def test_an_anonymous_approval_is_refused(self):
        request = ps.request_discount(patient_id=_pid(), reason="hardship",
                                      requested_by="clerk-a",
                                      requested_paise=rupees("100"))
        with pytest.raises(ps.PaymentError, match="decided_by"):
            ps.decide_discount(request["id"], approve=True, decided_by="  ")

    def test_a_reason_is_required_to_ask(self):
        with pytest.raises(ps.PaymentError, match="reason"):
            ps.request_discount(patient_id=_pid(), reason="", requested_by="clerk-a",
                                requested_paise=rupees("100"))

    def test_a_percent_discount_is_computed_once_in_basis_points(self):
        """7.5% of ₹13,457.33 must be the same number here as anywhere else in
        Orb — one implementation (`money.apply_basis_points`), no floats."""
        base = rupees("13457.33")
        request = ps.request_discount(
            patient_id=_pid(), reason="corporate tie-up", requested_by="clerk-a",
            discount_type="percent", percent_bp=750, base_paise=base,
        )
        from services.money import apply_basis_points
        assert request["requested_paise"] == apply_basis_points(base, 750)
        assert request["percent_bp"] == 750

    def test_a_percent_of_an_unknown_base_is_refused(self):
        with pytest.raises(ps.PaymentError, match="base"):
            ps.request_discount(patient_id=_pid(), reason="corporate",
                                requested_by="clerk-a", discount_type="percent",
                                percent_bp=1000)

    def test_a_policy_limit_is_recorded_and_flagged_not_enforced(self):
        """A senior person may legitimately go above the limit; the record has to
        show that they did, which is a different thing from stopping them."""
        request = ps.request_discount(
            patient_id=_pid(), reason="board approval", requested_by="clerk-a",
            requested_paise=rupees("50000"), policy_limit_paise=rupees("10000"),
        )
        assert request["exceeded_limit"] is True
        decided = ps.decide_discount(request["id"], approve=True, decided_by="director-1")
        assert decided["status"] == "approved"
        assert decided["exceeded_limit"] is True
        assert decided["approved_paise"] == rupees("50000")

    def test_approving_more_than_was_asked_for_is_refused(self):
        request = ps.request_discount(patient_id=_pid(), reason="hardship",
                                      requested_by="clerk-a",
                                      requested_paise=rupees("1000"))
        with pytest.raises(ps.PaymentError, match="raise a new request"):
            ps.decide_discount(request["id"], approve=True, decided_by="manager-1",
                               approved_paise=rupees("1001"))

    def test_a_partial_approval_records_the_smaller_figure(self):
        request = ps.request_discount(patient_id=_pid(), reason="hardship",
                                      requested_by="clerk-a",
                                      requested_paise=rupees("1000"))
        decided = ps.decide_discount(request["id"], approve=True, decided_by="manager-1",
                                     approved_paise=rupees("400"))
        assert decided["approved_paise"] == rupees("400")
        assert decided["requested_paise"] == rupees("1000")


# ══ 6. Day-end: the total IS the sum of its parts ════════════════════════════
#
# THE FAILURE this repo has already been burned by: a headline figure computed by
# one query and a breakdown computed by another, disagreeing. The clerk trusts
# the headline. Here the breakdown and the total are asserted against the RAW
# ROWS, so neither can hide inside the other.

class TestDayEndReconciliation:
    def _session(self, counter=None, float_paise=None):
        counter = counter or _counter()
        return ps.open_counter_session(
            counter_code=counter, opened_by="clerk-a", shift_label="morning",
            opening_float_paise=rupees("2000") if float_paise is None else float_paise,
        ), counter

    def test_expected_cash_is_float_plus_cash_in_minus_cash_out(self):
        session, _ = self._session()
        patient = _pid()
        ps.record_payment(patient_id=patient, amount_paise=rupees("1500"), method="cash",
                          purpose="deposit", received_by="clerk-a",
                          counter_session_id=session["id"])
        ps.record_payment(patient_id=patient, amount_paise=rupees("700"), method="upi",
                          reference_no="UTR-1", purpose="deposit", received_by="clerk-a",
                          counter_session_id=session["id"])
        report = ps.counter_session_expected(session["id"])
        assert report["cash_in_paise"] == rupees("1500")
        assert report["expected_cash_paise"] == rupees("2000") + rupees("1500")
        # UPI is in the takings but NOT in the drawer.
        assert report["totals"]["in_paise"] == rupees("2200")

    def test_the_totals_equal_the_sum_of_the_method_rows(self):
        """assert total == sum(parts) — for the session and for the day."""
        session, counter = self._session()
        patient = _pid()
        for amount, method, ref in (
            ("1234.56", "cash", None), ("99.99", "cash", None),
            ("2500", "upi", "UTR-2"), ("450.25", "card", "AUTH-9"),
            ("1", "netbanking", "NEFT-3"),
        ):
            ps.record_payment(patient_id=patient, amount_paise=rupees(amount),
                              method=method, reference_no=ref, purpose="deposit",
                              received_by="clerk-a", counter_session_id=session["id"])
        report = ps.counter_session_expected(session["id"])
        parts = report["by_method"]
        assert report["totals"]["in_paise"] == sum(p["in_paise"] for p in parts)
        assert report["totals"]["out_paise"] == sum(p["out_paise"] for p in parts)
        assert report["totals"]["net_paise"] == sum(p["net_paise"] for p in parts)
        assert report["totals"]["in_count"] == sum(p["in_count"] for p in parts)

        day = ps.day_end_summary(counter_code=counter)
        day_parts = day["by_method"]
        assert day["totals"]["in_paise"] == sum(p["in_paise"] for p in day_parts)
        assert day["totals"]["out_paise"] == sum(p["out_paise"] for p in day_parts)
        assert day["totals"]["net_paise"] == \
            day["totals"]["in_paise"] - day["totals"]["out_paise"]

    def test_the_breakdown_matches_the_raw_ledger_rows(self):
        """The independent check: read the payment rows directly and re-add
        them. A grouping bug cannot survive both this and the service's own
        internal reconciliation guard."""
        session, counter = self._session()
        patient = _pid()
        for amount, method, ref in (
            ("13.37", "cash", None), ("0.01", "cash", None),
            ("777.77", "upi", "UTR-7"),
        ):
            ps.record_payment(patient_id=patient, amount_paise=rupees(amount),
                              method=method, reference_no=ref, purpose="deposit",
                              received_by="clerk-a", counter_session_id=session["id"])
        with db_session() as db:
            rows = db.execute(
                "SELECT method, direction, amount_paise FROM payments "
                "WHERE counter_session_id = ? AND status IN ('received','cleared')",
                (session["id"],),
            ).fetchall()
        raw_in = sum(int(r["amount_paise"]) for r in rows if r["direction"] == "in")
        report = ps.counter_session_expected(session["id"])
        assert report["totals"]["in_paise"] == raw_in
        raw_cash = sum(int(r["amount_paise"]) for r in rows
                       if r["direction"] == "in" and r["method"] == "cash")
        assert report["cash_in_paise"] == raw_cash

    def test_a_variance_is_recorded_not_absorbed(self):
        session, _ = self._session(float_paise=rupees("1000"))
        patient = _pid()
        ps.record_payment(patient_id=patient, amount_paise=rupees("3000"), method="cash",
                          purpose="deposit", received_by="clerk-a",
                          counter_session_id=session["id"])
        closed = ps.close_counter_session(session["id"],
                                          declared_cash_paise=rupees("3800"),
                                          closed_by="clerk-a")
        assert closed["system_cash_paise"] == rupees("4000")
        assert closed["declared_cash_paise"] == rupees("3800")
        assert closed["variance_paise"] == -rupees("200")
        assert closed["balanced"] is False
        # A short till is an EXCEPTION somebody has to clear, not a closed shift.
        assert closed["status"] == "disputed"

    def test_a_balanced_shift_closes_clean(self):
        session, _ = self._session(float_paise=rupees("500"))
        ps.record_payment(patient_id=_pid(), amount_paise=rupees("250"), method="cash",
                          purpose="deposit", received_by="clerk-a",
                          counter_session_id=session["id"])
        closed = ps.close_counter_session(session["id"],
                                          declared_cash_paise=rupees("750"),
                                          closed_by="clerk-a")
        assert closed["variance_paise"] == 0
        assert closed["status"] == "closed"
        assert closed["balanced"] is True

    def test_an_uncounted_drawer_reads_as_uncounted_and_never_as_zero(self):
        session, _ = self._session()
        view = ps.get_counter_session(session["id"])
        assert view["declared_cash_paise"] is None
        assert view["declared_cash_display"] == "Not counted"
        assert view["variance_display"] == "Not counted"

    def test_a_day_with_an_open_session_is_not_final(self):
        session, counter = self._session()
        day = ps.day_end_summary(counter_code=counter)
        assert day["is_final"] is False
        assert day["counted_cash_paise"] is None
        assert day["counted_cash_display"] == "Not counted"
        ps.close_counter_session(session["id"], declared_cash_paise=rupees("2000"),
                                 closed_by="clerk-a")
        day = ps.day_end_summary(counter_code=counter)
        assert day["is_final"] is True
        assert day["counted_cash_paise"] == rupees("2000")

    def test_two_open_sessions_on_one_counter_are_refused(self):
        _, counter = self._session()
        with pytest.raises(ps.PaymentError, match="already has an open session"):
            ps.open_counter_session(counter_code=counter, opened_by="clerk-b")

    def test_a_closed_session_cannot_take_more_money(self):
        session, _ = self._session()
        ps.close_counter_session(session["id"], declared_cash_paise=rupees("2000"),
                                 closed_by="clerk-a")
        with pytest.raises(ps.PaymentError, match="cannot take money"):
            ps.record_payment(patient_id=_pid(), amount_paise=rupees("10"),
                              method="cash", purpose="deposit", received_by="clerk-a",
                              counter_session_id=session["id"])

    def test_reconciling_keeps_the_variance_and_needs_a_second_person(self):
        session, _ = self._session(float_paise=0)
        ps.record_payment(patient_id=_pid(), amount_paise=rupees("100"), method="cash",
                          purpose="deposit", received_by="clerk-a",
                          counter_session_id=session["id"])
        ps.close_counter_session(session["id"], declared_cash_paise=rupees("90"),
                                 closed_by="clerk-a")
        with pytest.raises(ps.PaymentError, match="second pair of eyes"):
            ps.reconcile_counter_session(session["id"], reconciled_by="clerk-a")
        signed = ps.reconcile_counter_session(session["id"], reconciled_by="manager-1")
        assert signed["status"] == "reconciled"
        assert signed["variance_paise"] == -rupees("10"), \
            "reconciling ACCEPTS a variance; it must never erase it"

    # ── cash that entered no drawer ──────────────────────────────────────────
    #
    # THE FAILURE: a cash receipt taken with no counter session open lands in the
    # day's takings and in NOBODY'S drawer. `counter_session_expected` scopes
    # itself to `counter_session_id`, so this money is in no shift's expected
    # figure — and the day-end variance (counted − expected, across the drawers)
    # is then missing a term while still being printed as if it were the day's
    # variance. The design: take the money (a refusal after the notes are on the
    # counter only pushes it off-system), MARK it, and hold the whole-day sheet
    # open until it is explained.

    def test_cash_taken_with_no_counter_session_is_recorded_and_marked(self):
        receipt = ps.record_payment(patient_id=_pid(), amount_paise=rupees("1234.56"),
                                    method="cash", purpose="deposit",
                                    received_by="clerk-a")
        assert receipt["amount_paise"] == 123456        # taken, not refused
        assert receipt["unattributed_cash"] is True
        assert "no counter session" in receipt["unattributed_cash_notice"]
        # The mark travels with every rendering of the receipt, not just this
        # response — it is the one fact a day-end cannot recover later.
        assert ps.get_payment(receipt["id"])["unattributed_cash"] is True

    def test_cash_taken_into_an_open_drawer_is_not_marked(self):
        session, _ = self._session()
        receipt = ps.record_payment(patient_id=_pid(), amount_paise=rupees("1234.56"),
                                    method="cash", purpose="deposit",
                                    received_by="clerk-a",
                                    counter_session_id=session["id"])
        assert receipt["unattributed_cash"] is False
        assert receipt["unattributed_cash_notice"] is None

    def test_a_non_cash_receipt_without_a_session_is_not_unattributed(self):
        """UPI never touches a drawer, so a UPI receipt with no session is not a
        cash-attribution problem — it is matched against the bank statement by
        its UTR. Only CASH_METHODS can be unattributed."""
        receipt = ps.record_payment(patient_id=_pid(), amount_paise=rupees("500"),
                                    method="upi", reference_no="UTR-NOSESSION",
                                    purpose="deposit", received_by="clerk-a")
        assert receipt["unattributed_cash"] is False

    def test_unattributed_cash_never_enters_a_drawers_expected_figure(self):
        """float ₹1,000 + ₹300 into the drawer, ₹700 outside it.

            expected = 100000p + 30000p = 130000p — the ₹700 is NOT in it.
        """
        session, _ = self._session(float_paise=rupees("1000"))
        patient = _pid()
        ps.record_payment(patient_id=patient, amount_paise=rupees("300"), method="cash",
                          purpose="deposit", received_by="clerk-a",
                          counter_session_id=session["id"])
        ps.record_payment(patient_id=patient, amount_paise=rupees("700"), method="cash",
                          purpose="deposit", received_by="clerk-a")
        report = ps.counter_session_expected(session["id"])
        assert report["cash_in_paise"] == 30000
        assert report["expected_cash_paise"] == 130000

    def test_the_whole_day_sheet_will_not_call_itself_final_with_cash_in_no_drawer(self):
        """The day-wide sheet is asserted as a DELTA, because the session DB is
        shared and other tests' receipts land in the same hospital-wide day (the
        test-isolation law at the top of this file).

        PROVEN ABLE TO FAIL: drop `unattributed_blocks` from `uncountable` in
        payments_service.day_end_summary and `is_final` comes back True with a
        variance printed over cash that is in nobody's drawer.
        """
        before = ps.day_end_summary()
        ps.record_payment(patient_id=_pid(), amount_paise=rupees("999.99"),
                          method="cash", purpose="deposit", received_by="clerk-a")
        after = ps.day_end_summary()

        assert after["unattributed_cash_in_paise"] - \
            before["unattributed_cash_in_paise"] == 99999
        assert after["unattributed_cash_count"] - before["unattributed_cash_count"] == 1
        assert after["unattributed_cash_notice"]

        assert after["is_final"] is False
        assert after["counted_cash_paise"] is None
        assert after["variance_paise"] is None
        assert any("no counter session" in b for b in after["finality_blockers"]), \
            "the sheet must say IN WORDS why it cannot be finalised"

    def test_a_counters_own_day_is_not_held_open_by_cash_from_no_counter(self):
        """A counter-scoped sheet is about ONE drawer, and drawer-less cash
        belongs to none — another till's exception is not this till's to clear.

            float ₹500 + ₹250 into the drawer = 75000p expected, counted 75000p
            → variance 0, day final, while ₹800 sits unattributed elsewhere.
        """
        session, counter = self._session(float_paise=rupees("500"))
        ps.record_payment(patient_id=_pid(), amount_paise=rupees("250"), method="cash",
                          purpose="deposit", received_by="clerk-a",
                          counter_session_id=session["id"])
        ps.record_payment(patient_id=_pid(), amount_paise=rupees("800"), method="cash",
                          purpose="deposit", received_by="clerk-a")
        ps.close_counter_session(session["id"], declared_cash_paise=rupees("750"),
                                 closed_by="clerk-a")

        day = ps.day_end_summary(counter_code=counter)
        assert day["cash_in_paise"] == 25000        # the ₹800 is not this counter's
        assert day["counted_cash_paise"] == 75000
        assert day["variance_paise"] == 0
        assert day["is_final"] is True
        assert day["finality_blockers"] == []
        # …but the hospital's own figure still carries it, so it is not hidden.
        assert day["unattributed_cash_count"] >= 1

    def test_a_refund_paid_at_the_counter_leaves_the_drawer(self):
        session, _ = self._session(float_paise=0)
        patient = _pid()
        receipt = ps.record_payment(patient_id=patient, amount_paise=rupees("1000"),
                                    method="cash", purpose="deposit",
                                    received_by="clerk-a",
                                    counter_session_id=session["id"])
        refund = ps.request_refund(patient_id=patient, amount_paise=rupees("400"),
                                   reason="cancelled", requested_by="clerk-a",
                                   payment_id=receipt["id"])
        ps.decide_refund(refund["id"], approve=True, decided_by="manager-1")
        ps.pay_refund(refund["id"], method="cash", paid_by="clerk-a",
                      counter_session_id=session["id"])
        report = ps.counter_session_expected(session["id"])
        assert report["cash_in_paise"] == rupees("1000")
        assert report["cash_out_paise"] == rupees("400")
        assert report["expected_cash_paise"] == rupees("600")

    # ── reversals in the drawer and on the sheet ─────────────────────────────
    #
    # THE FAILURE: a reversal moves the original to 'reversed' and writes a live
    # 'out' row. Summing live rows only counted the 'out' leg, so ₹500 taken and
    # reversed left the drawer expecting ₹500 LESS than its float. The honest
    # count was the till that came up "short".

    def test_a_reversal_nets_to_zero_in_the_drawer(self):
        """float ₹1,000 + ₹500 taken − ₹500 reversed = ₹1,000 expected."""
        session, _ = self._session(float_paise=rupees("1000"))
        receipt = ps.record_payment(patient_id=_pid(), amount_paise=rupees("500"),
                                    method="cash", purpose="deposit",
                                    received_by="clerk-a",
                                    counter_session_id=session["id"])
        ps.reverse_payment(receipt["id"], reason="keyed twice", reversed_by="manager-1")
        report = ps.counter_session_expected(session["id"])
        # Both legs, visible as the two movements they were.
        assert report["cash_in_paise"] == rupees("500")
        assert report["cash_out_paise"] == rupees("500")
        assert report["expected_cash_paise"] == rupees("1000")
        closed = ps.close_counter_session(session["id"],
                                          declared_cash_paise=rupees("1000"),
                                          closed_by="clerk-a")
        assert closed["balanced"] is True, "the honest count must not read as short"

    def test_a_reversal_nets_to_zero_on_the_day_sheet(self):
        session, counter = self._session(float_paise=0)
        receipt = ps.record_payment(patient_id=_pid(), amount_paise=rupees("750"),
                                    method="cash", purpose="deposit",
                                    received_by="clerk-a",
                                    counter_session_id=session["id"])
        ps.reverse_payment(receipt["id"], reason="wrong patient", reversed_by="manager-1")
        day = ps.day_end_summary(counter_code=counter)
        assert day["cash_in_paise"] == rupees("750")
        assert day["cash_out_paise"] == rupees("750")
        assert day["cash_net_paise"] == 0
        assert day["totals"]["in_count"] == 1 and day["totals"]["out_count"] == 1

    # ── drawers the day touched but did not open ─────────────────────────────
    #
    # THE FAILURE: the sheet chose its drawers by `opened_at` inside the day, so
    # a till opened yesterday and never closed was not on today's sheet at all:
    # no open session, so "final", and "Counted ₹0.00" over the cash it took.

    @staticmethod
    def _days_ago(days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def _backdate_session(self, session_id: int, days: int) -> None:
        with db_session() as db:
            db.execute("UPDATE cash_counter_sessions SET opened_at = ? WHERE id = ?",
                       (self._days_ago(days), session_id))

    def test_a_till_opened_on_an_earlier_day_holds_todays_sheet_open(self):
        """PROVEN ABLE TO FAIL: select the sheet's drawers by `opened_at` alone
        and this sheet comes back final with ₹0.00 counted."""
        session, counter = self._session(float_paise=rupees("200"))
        self._backdate_session(session["id"], 2)
        ps.record_payment(patient_id=_pid(), amount_paise=rupees("300"), method="cash",
                          purpose="deposit", received_by="clerk-a",
                          counter_session_id=session["id"])
        day = ps.day_end_summary(counter_code=counter)
        assert [s["id"] for s in day["sessions"]] == [session["id"]]
        assert day["open_session_count"] == 1
        assert day["is_final"] is False
        assert day["counted_cash_paise"] is None
        assert day["counted_cash_display"] == "Not counted"
        assert any(f"Counter {counter} has been open since" in b and "clerk-a" in b
                   for b in day["finality_blockers"]), day["finality_blockers"]

    def test_a_till_open_with_no_receipts_today_still_holds_the_sheet(self):
        """Open since yesterday, nothing taken today: still an uncounted drawer."""
        session, counter = self._session()
        self._backdate_session(session["id"], 1)
        day = ps.day_end_summary(counter_code=counter)
        assert day["is_final"] is False
        assert day["open_session_count"] == 1

    def test_a_shift_that_ran_past_midnight_is_counted_on_today_sheet(self):
        session, counter = self._session(float_paise=rupees("100"))
        self._backdate_session(session["id"], 1)
        ps.record_payment(patient_id=_pid(), amount_paise=rupees("400"), method="cash",
                          purpose="deposit", received_by="clerk-a",
                          counter_session_id=session["id"])
        ps.close_counter_session(session["id"], declared_cash_paise=rupees("500"),
                                 closed_by="clerk-a")
        day = ps.day_end_summary(counter_code=counter)
        assert [s["id"] for s in day["sessions"]] == [session["id"]]
        assert day["counted_cash_paise"] == rupees("500")
        assert day["variance_paise"] == 0
        assert day["is_final"] is True

    def test_reversing_an_old_receipt_does_not_pull_its_closed_shift_onto_today(self):
        """The reversal row carries the ORIGINAL receipt's session. Today's sheet
        shows the reversal as today's money out, but the old shift's count stays
        on its own day."""
        session, counter = self._session(float_paise=0)
        receipt = ps.record_payment(patient_id=_pid(), amount_paise=rupees("250"),
                                    method="cash", purpose="deposit",
                                    received_by="clerk-a",
                                    counter_session_id=session["id"])
        ps.close_counter_session(session["id"], declared_cash_paise=rupees("250"),
                                 closed_by="clerk-a")
        with db_session() as db:
            db.execute("UPDATE cash_counter_sessions SET opened_at = ?, closed_at = ? "
                       "WHERE id = ?", (self._days_ago(3), self._days_ago(3), session["id"]))
            db.execute("UPDATE payments SET received_at = ? WHERE id = ?",
                       (self._days_ago(3), receipt["id"]))
        ps.reverse_payment(receipt["id"], reason="bounced note", reversed_by="manager-1")
        day = ps.day_end_summary(counter_code=counter)
        assert day["cash_out_paise"] == rupees("250")
        assert day["sessions"] == []
        assert day["counted_cash_paise"] == 0

    def test_an_earlier_days_sheet_ignores_a_till_opened_after_it(self):
        _, counter = self._session()
        yesterday = (hospital_today() - timedelta(days=1)).isoformat()
        day = ps.day_end_summary(day=yesterday, counter_code=counter)
        assert day["sessions"] == []
        assert day["is_final"] is True


# ══ 7. Money is never rendered as a lie ══════════════════════════════════════

class TestHonestNumbers:
    def test_amounts_are_formatted_in_indian_grouping(self):
        patient = _pid()
        receipt = ps.record_payment(patient_id=patient, amount_paise=rupees("1234567.89"),
                                    method="cash", purpose="deposit", received_by="clerk-a")
        assert receipt["amount_display"] == "₹12,34,567.89"
        assert format_inr(receipt["amount_paise"]) == receipt["amount_display"]

    def test_an_unconfigured_receipt_series_is_reported_as_unconfigured(self):
        """Migration 20 seeds the series with is_configured=0. A placeholder
        number that looks official is worse than no number at all, so every
        response that carries one carries the flag too."""
        receipt = ps.record_payment(patient_id=_pid(), amount_paise=rupees("10"),
                                    method="cash", purpose="deposit", received_by="clerk-a")
        assert "receipt_series_is_configured" in receipt
        assert isinstance(receipt["receipt_series_is_configured"], bool)

    def test_every_stored_money_column_is_an_integer(self):
        """No float can have slipped into a row this module wrote."""
        patient = _pid()
        invoice = _make_invoice(patient, rupees("100"))
        ps.record_payment(patient_id=patient, amount_paise=rupees("150"), method="cash",
                          invoice_id=invoice, received_by="clerk-a")
        with db_session() as db:
            for table, column in (
                ("payments", "amount_paise"),
                ("payment_allocations", "amount_paise"),
                ("patient_deposits", "amount_paise"),
            ):
                for row in db.execute(f"SELECT {column} AS v FROM {table}").fetchall():
                    assert isinstance(row["v"], int), f"{table}.{column} holds a non-integer"


# ══ 8. Audit — a money movement always has a name on it ══════════════════════

class TestAudit:
    def test_a_receipt_writes_an_attributable_audit_row(self):
        patient = _pid()
        receipt = ps.record_payment(patient_id=patient, amount_paise=rupees("321"),
                                    method="cash", purpose="deposit",
                                    received_by="CLERK-XYZ")
        with db_session() as db:
            row = db.execute(
                "SELECT * FROM audit_logs WHERE action_type = 'payment_recorded' "
                "AND target_id = ? ORDER BY id DESC LIMIT 1", (str(receipt["id"]),),
            ).fetchone()
        assert row is not None
        assert row["actor_user_id"] == "CLERK-XYZ"
        assert row["patient_id"] == patient
        assert row["event_category"] == "financial"

    def test_a_discount_decision_is_audited_with_both_names(self):
        request = ps.request_discount(patient_id=_pid(), reason="hardship",
                                      requested_by="clerk-a",
                                      requested_paise=rupees("100"))
        ps.decide_discount(request["id"], approve=True, decided_by="manager-9")
        with db_session() as db:
            row = db.execute(
                "SELECT * FROM audit_logs WHERE action_type = 'discount_decided' "
                "AND target_id = ? ORDER BY id DESC LIMIT 1", (str(request["id"]),),
            ).fetchone()
        assert row is not None
        assert row["actor_user_id"] == "manager-9"
        assert "clerk-a" in (row["metadata_json"] or "")

    def test_the_audit_chain_still_verifies_after_a_run_of_money_operations(self):
        from database import verify_audit_chain
        patient = _pid()
        receipt = ps.record_payment(patient_id=patient, amount_paise=rupees("500"),
                                    method="cash", purpose="deposit", received_by="clerk-a")
        ps.reverse_payment(receipt["id"], reason="test", reversed_by="manager-1")
        result = verify_audit_chain()
        assert result.get("valid", result.get("is_valid")) is not False


# ══ 9. The HTTP surface ══════════════════════════════════════════════════════

class TestRoutes:
    def test_a_receipt_can_be_taken_and_read_back(self, client):
        _, hdr = _mk_user("admin")
        patient = _pid()
        invoice = _make_invoice(patient, rupees("2000"))
        resp = client.post("/api/payments/receipts", headers=hdr, json={
            "patient_id": patient, "amount_paise": rupees("2000"),
            "method": "cash", "invoice_id": invoice,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["allocated_paise"] == rupees("2000")
        read = client.get(f"/api/payments/receipts/{body['id']}", headers=hdr)
        assert read.status_code == 200
        assert read.json()["amount_paise"] == rupees("2000")

    def test_the_invoice_check_shows_owner_and_balance_and_refuses_a_stranger(self, client):
        _, hdr = _mk_user("admin")
        owner, other = _pid(), _pid()
        _admit(owner, "PW")
        _admit(other, "PW")
        invoice = _make_invoice(owner, rupees("2000"))
        ok = client.get(f"/api/payments/patients/{owner}/invoices/{invoice}", headers=hdr)
        assert ok.status_code == 200, ok.text
        assert ok.json()["patient_id"] == owner
        assert ok.json()["patient_name"] == "Payments Test"
        assert ok.json()["due_paise"] == rupees("2000")
        assert ok.json()["allocatable"] is True
        wrong = client.get(f"/api/payments/patients/{other}/invoices/{invoice}", headers=hdr)
        assert wrong.status_code == 422
        message = wrong.json()["detail"]["message"]
        assert "belongs to another patient" in message
        assert owner not in message, "the refusal must not name the invoice's owner"

    def test_a_cross_patient_receipt_over_http_is_a_422(self, client):
        _, hdr = _mk_user("admin")
        owner, payer = _pid(), _pid()
        invoice = _make_invoice(owner, rupees("2000"))
        resp = client.post("/api/payments/receipts", headers=hdr, json={
            "patient_id": payer, "amount_paise": rupees("2000"),
            "method": "cash", "invoice_id": invoice,
        })
        assert resp.status_code == 422
        assert "Nothing was taken" in resp.json()["detail"]["message"]

    def test_editing_a_receipt_over_http_is_a_409_that_names_the_remedy(self, client):
        _, hdr = _mk_user("admin")
        patient = _pid()
        created = client.post("/api/payments/receipts", headers=hdr, json={
            "patient_id": patient, "amount_paise": rupees("100"),
            "method": "cash", "purpose": "deposit",
        }).json()
        resp = client.patch(f"/api/payments/receipts/{created['id']}", headers=hdr,
                            json={"amount_paise": 1})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "receipt_immutable"
        assert "reverse" in detail["remedy"]
        # And nothing moved.
        after = client.get(f"/api/payments/receipts/{created['id']}", headers=hdr).json()
        assert after["amount_paise"] == rupees("100")

    def test_a_decimal_amount_on_the_wire_is_refused(self, client):
        """JSON has one number type and it is a double. `100.5` in an
        `amount_paise` field is a rupee figure in the wrong box."""
        _, hdr = _mk_user("admin")
        resp = client.post("/api/payments/receipts", headers=hdr, json={
            "patient_id": _pid(), "amount_paise": 100.5, "method": "cash",
            "purpose": "deposit",
        })
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "bad_money_value"

    def test_a_refund_over_the_ceiling_is_a_422_with_the_reason(self, client):
        _, hdr = _mk_user("admin")
        patient = _pid()
        created = client.post("/api/payments/receipts", headers=hdr, json={
            "patient_id": patient, "amount_paise": rupees("100"),
            "method": "cash", "purpose": "deposit",
        }).json()
        resp = client.post("/api/payments/refunds", headers=hdr, json={
            "patient_id": patient, "amount_paise": rupees("500"),
            "reason": "too much", "payment_id": created["id"],
        })
        assert resp.status_code == 422
        assert "refundable" in resp.json()["detail"]["message"]

    def test_the_approver_comes_from_the_session_not_the_body(self, client):
        """A body-supplied approver name is a signature anybody can forge."""
        uid, hdr = _mk_user("admin")
        patient = _pid()
        created = client.post("/api/payments/discounts", headers=hdr, json={
            "patient_id": patient, "reason": "hardship", "requested_paise": rupees("100"),
        }).json()
        assert created["requested_by"] == uid
        resp = client.post(f"/api/payments/discounts/{created['id']}/decision",
                           headers=hdr, json={"approve": True, "decided_by": "someone-else"})
        # The session user IS the requester, so four-eyes refuses — proving the
        # body's `decided_by` was never read.
        assert resp.status_code == 422
        assert "second name" in resp.json()["detail"]["message"]

    def test_a_clinician_cannot_reach_a_money_route(self, client):
        for role in ("doctor", "nurse", "pharmacist", "patient"):
            _, hdr = _mk_user(role)
            resp = client.get("/api/payments/day-end", headers=hdr)
            assert resp.status_code == 403, f"{role} reached the day-end sheet"

    def test_an_unauthenticated_caller_is_refused(self, client):
        assert client.get("/api/payments/day-end").status_code in (401, 403)

    def test_a_ward_scoped_account_cannot_read_another_wards_money(self, client):
        """THE 2026-08-05 SHAPE: a patient id off the path with no ward check.
        A money route leaks the same thing a chart route does — that this person
        is admitted, and what they have paid.

        404 rather than 403, because a 403 confirms the patient exists.
        """
        patient = _pid()
        _admit(patient, "Payments Ward A")
        ps.record_payment(patient_id=patient, amount_paise=rupees("100"), method="cash",
                          purpose="deposit", received_by="clerk-a")

        _, other_ward = _mk_user("admin", department="Payments Ward B")
        for path in (
            f"/api/payments/deposits/{patient}",
            f"/api/payments/patients/{patient}/summary",
        ):
            assert client.get(path, headers=other_ward).status_code == 404, path

        # The hospital-wide finance account (no department on its user row) reads
        # across wards — that is what makes a single cash counter possible.
        _, finance = _mk_user("admin")
        for path in (
            f"/api/payments/deposits/{patient}",
            f"/api/payments/patients/{patient}/summary",
        ):
            assert client.get(path, headers=finance).status_code == 200, path

        # TEST-ISOLATION LAW (see tests/test_nurse_mode.py): this is the only
        # test in the module that admits anybody, and an admitted patient left
        # behind is visible to every census and name-resolution surface for the
        # rest of the session. Retire them on both status columns — leaving
        # either one 'active' is the worst outcome, because it looks cleaned up.
        with db_session() as db:
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (patient,))
            db.execute(
                "UPDATE admissions SET status = 'discharged' WHERE patient_id = ?",
                (patient,),
            )

    def test_the_module_switch_hides_every_route(self, client, monkeypatch):
        """ORB_PAYMENTS=0 must make these routes answer as if they do not
        exist — a tab whose routes 404 is the documented failure mode, and it
        must be a clean 404 rather than a 500."""
        _, hdr = _mk_user("admin")
        monkeypatch.setattr(config, "PAYMENTS_ENABLED", False)
        for method, path in (
            ("get", "/api/payments/day-end"),
            ("get", "/api/payments/receipts"),
            ("post", "/api/payments/receipts"),
        ):
            resp = getattr(client, method)(path, headers=hdr, **(
                {"json": {}} if method == "post" else {}
            ))
            assert resp.status_code == 404, f"{method} {path} was reachable"
            assert resp.json()["detail"]["error"] == "module_disabled"

    def test_the_day_end_sheet_adds_up_over_http(self, client):
        _, hdr = _mk_user("admin")
        counter = _counter()
        session = client.post("/api/payments/counters/open", headers=hdr, json={
            "counter_code": counter, "opening_float_paise": rupees("100"),
        }).json()
        patient = _pid()
        for amount, method, ref in (("500", "cash", None), ("321.45", "upi", "UTR-X")):
            client.post("/api/payments/receipts", headers=hdr, json={
                "patient_id": patient, "amount_paise": rupees(amount), "method": method,
                "reference_no": ref, "purpose": "deposit",
                "counter_session_id": session["id"],
            })
        day = client.get(f"/api/payments/day-end?counter_code={counter}", headers=hdr).json()
        parts = day["by_method"]
        assert day["totals"]["in_paise"] == sum(p["in_paise"] for p in parts)
        assert day["totals"]["in_paise"] == rupees("821.45")
        assert day["is_final"] is False


# ══ 10. The permission split ═════════════════════════════════════════════════
#
# 2026-08-18: payments.read / payments.record / payments.approve /
# payments.close_counter replaced the borrowed admin.overview (the routes
# module's docstring carries the design). What this block holds shut:
#
#   * admin still reaches every tier — the split must not lock out the one role
#     that runs the desk today;
#   * clinicians reach NOTHING, read or write, exactly as before;
#   * a cashier can work a counter end to end (read, record, close) and is
#     refused the approve verb WITH A FULLY VALID REQUEST — the 403 must be the
#     permission (it names payments.approve), never a validation accident or
#     the service's own four-eyes rule wearing a different status code.

class TestPermissionSplit:
    def _receipt_for(self, client, hdr, patient=None, session_id=None):
        patient = patient or _pid()
        body = {"patient_id": patient, "amount_paise": rupees("1000"),
                "method": "cash", "purpose": "deposit"}
        if session_id is not None:
            body["counter_session_id"] = session_id
        resp = client.post("/api/payments/receipts", headers=hdr, json=body)
        assert resp.status_code == 200, resp.text
        return patient, resp.json()

    def test_admin_still_reaches_every_tier(self, client):
        """admin holds all four permissions, so the split changes nothing for
        the accounts that run the desk today."""
        _, admin_a = _mk_user("admin")
        _, admin_b = _mk_user("admin")
        counter = _counter()

        # RECORD: open a drawer, take money.
        opened = client.post("/api/payments/counters/open", headers=admin_a,
                             json={"counter_code": counter, "opening_float_paise": 0})
        assert opened.status_code == 200, opened.text
        session_id = opened.json()["id"]
        patient, receipt = self._receipt_for(client, admin_a, session_id=session_id)

        # READ: every summary surface.
        for path in ("/api/payments/config", "/api/payments/receipts",
                     "/api/payments/counters", "/api/payments/day-end",
                     f"/api/payments/counters/{session_id}/expected"):
            assert client.get(path, headers=admin_a).status_code == 200, path

        # APPROVE: a SECOND admin decides — the service's four-eyes rule
        # (approver ≠ requester) survives the split; the permission is in
        # addition to it, not instead of it.
        refund = client.post("/api/payments/refunds", headers=admin_a, json={
            "patient_id": patient, "amount_paise": rupees("100"),
            "reason": "split smoke test", "payment_id": receipt["id"],
        })
        assert refund.status_code == 200, refund.text
        decided = client.post(f"/api/payments/refunds/{refund.json()['id']}/decision",
                              headers=admin_b, json={"approve": True})
        assert decided.status_code == 200, decided.text

        # CLOSE_COUNTER: the end-of-shift count.
        closed = client.post(f"/api/payments/counters/{session_id}/close",
                             headers=admin_a,
                             json={"declared_cash_paise": rupees("1000")})
        assert closed.status_code == 200, closed.text

    def test_an_admin_cannot_reverse_their_own_receipt(self, client):
        """An admin holds record AND approve, so the permission tier never
        stopped one voiding their own receipt. The service does: the reverser
        is the session user, and it may not be the one who took the money."""
        _, admin_a = _mk_user("admin")
        _, admin_b = _mk_user("admin")
        _, receipt = self._receipt_for(client, admin_a)
        own = client.post(f"/api/payments/receipts/{receipt['id']}/reverse",
                          headers=admin_a, json={"reason": "keyed twice"})
        assert own.status_code == 422, own.text
        assert "person who took the payment" in own.json()["detail"]["message"]
        other = client.post(f"/api/payments/receipts/{receipt['id']}/reverse",
                            headers=admin_b, json={"reason": "keyed twice"})
        assert other.status_code == 200, other.text
        assert other.json()["original"]["status"] == "reversed"

    def test_clinicians_are_refused_on_reads_and_writes(self, client):
        """Doctor and nurse hold no payments.*. The read gate refuses the GETs,
        and — because every write requires read as well — refuses every write
        even with a fully valid body."""
        for role in ("doctor", "nurse"):
            _, hdr = _mk_user(role)
            for path in ("/api/payments/receipts", "/api/payments/day-end",
                         "/api/payments/counters"):
                resp = client.get(path, headers=hdr)
                assert resp.status_code == 403, f"{role} read {path}: {resp.status_code}"
            resp = client.post("/api/payments/receipts", headers=hdr, json={
                "patient_id": _pid(), "amount_paise": rupees("100"),
                "method": "cash", "purpose": "deposit",
            })
            assert resp.status_code == 403, f"{role} write: {resp.text}"
            assert "payments.read" in resp.json()["detail"]["missing"]

    def test_a_cashier_can_read_record_and_close(self, client):
        """The role the split exists for: a counter account that is not an
        admin account. Read the desk, open a drawer, take money, count out."""
        _, csh = _mk_cashier()
        counter = _counter()

        opened = client.post("/api/payments/counters/open", headers=csh,
                             json={"counter_code": counter, "opening_float_paise": 0})
        assert opened.status_code == 200, opened.text
        session_id = opened.json()["id"]

        self._receipt_for(client, csh, session_id=session_id)   # RECORD

        assert client.get("/api/payments/config", headers=csh).status_code == 200
        assert client.get("/api/payments/receipts", headers=csh).status_code == 200
        assert client.get(f"/api/payments/counters/{session_id}/expected",
                          headers=csh).status_code == 200        # READ

        closed = client.post(f"/api/payments/counters/{session_id}/close",
                             headers=csh,
                             json={"declared_cash_paise": rupees("1000")})
        assert closed.status_code == 200, closed.text            # CLOSE_COUNTER
        assert closed.json()["balanced"] is True

    def test_a_cashier_is_refused_the_approve_verb_with_a_valid_body(self, client):
        """The refusal must be the PERMISSION. The body is valid, the refund
        exists, and the deciding cashier is NOT the requester — so the
        service's four-eyes rule would have allowed it (it answers 422, not
        403, when it refuses). A 403 naming exactly payments.approve proves
        the permission gate said no, and that read — which the cashier holds —
        was not the problem."""
        _, csh_a = _mk_cashier()
        _, csh_b = _mk_cashier()
        patient, receipt = self._receipt_for(client, csh_a)
        refund = client.post("/api/payments/refunds", headers=csh_a, json={
            "patient_id": patient, "amount_paise": rupees("500"),
            "reason": "procedure cancelled", "payment_id": receipt["id"],
        })
        assert refund.status_code == 200, refund.text   # raising a request is RECORD work

        resp = client.post(f"/api/payments/refunds/{refund.json()['id']}/decision",
                           headers=csh_b,
                           json={"approve": True, "decision_note": "looks right"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["missing"] == ["payments.approve"]

        # The other two approve-tier surfaces refuse the same cashier the same
        # way: reversing a receipt (a void of recorded money; the service's own
        # taker-may-not-reverse rule would also refuse csh_a, but as a 422, so
        # the 403 naming the permission proves the gate said no first) …
        reverse = client.post(f"/api/payments/receipts/{receipt['id']}/reverse",
                              headers=csh_a, json={"reason": "wrong patient"})
        assert reverse.status_code == 403
        assert reverse.json()["detail"]["missing"] == ["payments.approve"]

        # … and deciding a discount.
        disc = client.post("/api/payments/discounts", headers=csh_a, json={
            "patient_id": patient, "reason": "hardship",
            "requested_paise": rupees("100"),
        })
        assert disc.status_code == 200, disc.text       # requesting is RECORD work
        decide = client.post(f"/api/payments/discounts/{disc.json()['id']}/decision",
                             headers=csh_b, json={"approve": True})
        assert decide.status_code == 403
        assert decide.json()["detail"]["missing"] == ["payments.approve"]

    def test_the_split_carries_a_refund_across_both_roles(self, client):
        """The designed lifecycle, end to end: cashier takes the money and
        raises the request (record), cashier is refused the decision (no
        approve), admin approves (approve), cashier pays out (record — the
        service will only pay an APPROVED refund, so the authorisation has
        already happened by the time the cash crosses the counter)."""
        _, csh = _mk_cashier()
        _, admin = _mk_user("admin")
        patient, receipt = self._receipt_for(client, csh)
        refund = client.post("/api/payments/refunds", headers=csh, json={
            "patient_id": patient, "amount_paise": rupees("1000"),
            "reason": "discharge settlement", "payment_id": receipt["id"],
        }).json()

        assert client.post(f"/api/payments/refunds/{refund['id']}/decision",
                           headers=csh,
                           json={"approve": True}).status_code == 403

        approved = client.post(f"/api/payments/refunds/{refund['id']}/decision",
                               headers=admin, json={"approve": True})
        assert approved.status_code == 200, approved.text

        paid = client.post(f"/api/payments/refunds/{refund['id']}/pay",
                           headers=csh, json={"method": "cash"})
        assert paid.status_code == 200, paid.text
        assert paid.json()["status"] == "paid"

    def test_the_edit_refusal_still_teaches_the_recording_client(self, client):
        """PATCH /receipts/{id} is RECORD-tier ON PURPOSE (see the route): it
        can never mutate — it exists to answer 409 with the immutability rule —
        and its audience is the counter screen that tries an edit. Tiered at
        approve, the cashier's client would get a blank 403 instead of the
        lesson pointing at /reverse."""
        _, csh = _mk_cashier()
        _, receipt = self._receipt_for(client, csh)
        resp = client.patch(f"/api/payments/receipts/{receipt['id']}",
                            headers=csh, json={"amount_paise": 1})
        assert resp.status_code == 409
        assert "reverse" in resp.json()["detail"]["remedy"]


# ══ 11. Who the money is for (F093) ═════════════════════════════════════════
#
# The counter took any text as a Patient ID and an invoice's internal integer,
# and the service checked only that the id was not blank: a receipt or an
# advance could be issued to a patient who does not exist, and that money then
# belonged to nobody the hospital could find. Every money list printed bare
# ids (PAT-50A2D09E, ADM001) a clerk cannot read.

def _rcpt_next() -> int:
    with db_session() as db:
        row = db.execute(
            "SELECT next_number FROM document_number_series WHERE series_code = 'RCPT'"
        ).fetchone()
    return int(row["next_number"]) if row else 0


def _named_patient(name: str, *, status: str = "discharged", bed: str = "C-7") -> str:
    pid = f"PAYN{uuid.uuid4().hex[:10].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, status) VALUES (?,?,?,?,?,?)",
            (pid, name, f"MRN-{pid}", bed, "PW", status),
        )
    return pid


def _phi_audit_count(action_type: str) -> int:
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE event_category = 'phi_access' "
            "AND action_type = ?", (action_type,),
        ).fetchone()["n"]


class TestUnknownPatientIsRefused:
    def test_a_receipt_for_an_id_that_names_nobody_is_refused(self):
        before = _rcpt_next()
        with pytest.raises(ps.PaymentError) as exc:
            ps.record_payment(patient_id="PAT-NOBODY01", amount_paise=rupees("500"),
                              method="cash", purpose="deposit", received_by="clerk-a")
        message = str(exc.value)
        assert "No patient with ID PAT-NOBODY01 is registered" in message
        assert "Nothing was taken" in message
        # Refused before a receipt number was spent, so the series has no gap.
        assert _rcpt_next() == before
        with db_session() as db:
            assert db.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE patient_id = 'PAT-NOBODY01'"
            ).fetchone()["n"] == 0
            assert db.execute(
                "SELECT COUNT(*) AS n FROM patient_deposits WHERE patient_id = 'PAT-NOBODY01'"
            ).fetchone()["n"] == 0

    def test_a_refund_for_an_id_that_names_nobody_says_so(self):
        """It used to be refused only by the cap, as "at most ₹0.00 is
        refundable", which names the wrong problem."""
        with pytest.raises(ps.PaymentError, match="No patient with ID PAT-NOBODY02") as exc:
            ps.request_refund(patient_id="PAT-NOBODY02", amount_paise=rupees("10"),
                              reason="probe", requested_by="clerk-a")
        assert "No refund was raised" in str(exc.value)
        assert "refundable" not in str(exc.value)

    def test_a_discount_for_an_id_that_names_nobody_is_refused(self):
        with pytest.raises(ps.PaymentError, match="No patient with ID PAT-NOBODY03"):
            ps.request_discount(patient_id="PAT-NOBODY03", reason="hardship",
                                requested_by="clerk-a", requested_paise=rupees("10"),
                                scope="patient")

    def test_over_http_it_is_a_422_the_counter_can_print(self, client):
        _, hdr = _mk_cashier()
        resp = client.post("/api/payments/receipts", headers=hdr, json={
            "patient_id": "PAT-NOBODY04", "amount_paise": rupees("500"),
            "method": "cash", "purpose": "deposit",
        })
        assert resp.status_code == 422
        assert "No patient with ID PAT-NOBODY04 is registered" in resp.json()["detail"]["message"]

    def test_a_replayed_receipt_still_returns_the_original(self):
        """The patient check sits after the replay check, so a double tap
        answers with the receipt already issued, not with a new question."""
        patient = _pid()
        key = f"idem-{uuid.uuid4().hex}"
        first = ps.record_payment(patient_id=patient, amount_paise=rupees("50"),
                                  method="cash", purpose="deposit",
                                  received_by="clerk-a", idempotency_key=key)
        again = ps.record_payment(patient_id=patient, amount_paise=rupees("50"),
                                  method="cash", purpose="deposit",
                                  received_by="clerk-a", idempotency_key=key)
        assert again["replayed"] is True and again["id"] == first["id"]


class TestCounterPatientSearch:
    def test_finds_a_patient_by_name_mrn_bed_or_id_with_no_clinical_fields(self, client):
        _, hdr = _mk_cashier()
        token = uuid.uuid4().hex[:6].upper()
        pid = _named_patient(f"Qorvane {token}", bed=f"Q-{token}")
        for q in (f"qorvane {token.lower()}", f"MRN-{pid}", f"Q-{token}", pid):
            resp = client.get("/api/payments/patients", headers=hdr, params={"q": q})
            assert resp.status_code == 200, resp.text
            hits = [p for p in resp.json()["patients"] if p["id"] == pid]
            assert hits, f"search for {q!r} missed the patient"
            assert hits[0]["name"] == f"Qorvane {token}"
            assert hits[0]["mrn"] == f"MRN-{pid}"
            assert set(hits[0]) == {"id", "name", "mrn", "bed", "ward", "status"}

    def test_includes_discharged_patients_and_sorts_admitted_first(self, client):
        _, hdr = _mk_cashier()
        token = uuid.uuid4().hex[:6].upper()
        gone = _named_patient(f"Brellik {token} Gone", status="discharged")
        here = _named_patient(f"Brellik {token} Here", status="admitted")
        try:
            rows = client.get("/api/payments/patients", headers=hdr,
                              params={"q": f"Brellik {token}"}).json()["patients"]
            assert [r["id"] for r in rows] == [here, gone]
        finally:
            with db_session() as db:
                db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (here,))

    def test_a_one_character_query_returns_nothing(self, client):
        _, hdr = _mk_cashier()
        resp = client.get("/api/payments/patients", headers=hdr, params={"q": "a"})
        assert resp.status_code == 200
        assert resp.json()["patients"] == []

    def test_a_like_wildcard_typed_by_the_clerk_is_literal(self, client):
        _, hdr = _mk_cashier()
        resp = client.get("/api/payments/patients", headers=hdr, params={"q": "%%"})
        assert resp.json()["patients"] == []

    def test_a_ward_scoped_account_finds_only_its_own_ward(self, client):
        token = uuid.uuid4().hex[:6].upper()
        mine, theirs = _pid(), _pid()
        _admit(mine, f"Search Ward A {token}")
        _admit(theirs, f"Search Ward B {token}")
        try:
            _, ward_a = _mk_user("admin", department=f"Search Ward A {token}")
            ids = {p["id"] for p in client.get(
                "/api/payments/patients", headers=ward_a,
                params={"q": "Payments Test"}).json()["patients"]}
            assert theirs not in ids
            assert mine in ids
        finally:
            with db_session() as db:
                for pid in (mine, theirs):
                    db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
                    db.execute("UPDATE admissions SET status = 'discharged' "
                               "WHERE patient_id = ?", (pid,))

    def test_a_search_that_names_anyone_is_audited(self, client):
        _, hdr = _mk_cashier()
        token = uuid.uuid4().hex[:6].upper()
        _named_patient(f"Audrix {token}")
        before = _phi_audit_count("payments_patient_search")
        client.get("/api/payments/patients", headers=hdr, params={"q": f"Audrix {token}"})
        assert _phi_audit_count("payments_patient_search") == before + 1
        # A probe that matches nobody discloses nothing and writes nothing.
        client.get("/api/payments/patients", headers=hdr,
                   params={"q": f"Nomatch {uuid.uuid4().hex}"})
        assert _phi_audit_count("payments_patient_search") == before + 1

    def test_a_clinician_cannot_use_it(self, client):
        _, hdr = _mk_user("nurse")
        assert client.get("/api/payments/patients", headers=hdr,
                          params={"q": "Payments"}).status_code == 403


class TestOpenInvoicesForTheCounter:
    def test_lists_only_bills_that_still_owe_money(self, client):
        _, hdr = _mk_cashier()
        patient = _pid()
        owing = _make_invoice(patient, rupees("3000"))
        part = _make_invoice(patient, rupees("1000"))
        ps.record_payment(patient_id=patient, amount_paise=rupees("400"), method="cash",
                          invoice_id=part, received_by="clerk-a")
        paid = _make_invoice(patient, rupees("200"))
        ps.record_payment(patient_id=patient, amount_paise=rupees("200"), method="cash",
                          invoice_id=paid, received_by="clerk-a")
        _make_invoice(patient, rupees("900"), status="cancelled")
        _make_invoice(patient, rupees("900"), status="draft")
        _make_invoice(_pid(), rupees("700"))   # someone else's bill

        resp = client.get(f"/api/payments/patients/{patient}/invoices", headers=hdr)
        assert resp.status_code == 200, resp.text
        rows = {r["invoice_id"]: r for r in resp.json()["invoices"]}
        assert set(rows) == {owing, part}
        assert rows[owing]["due_paise"] == rupees("3000")
        assert rows[part]["due_paise"] == rupees("600")
        assert rows[part]["due_display"] == format_inr(rupees("600"))

    def test_a_ward_scoped_account_cannot_list_another_wards_bills(self, client):
        token = uuid.uuid4().hex[:6].upper()
        patient = _pid()
        _admit(patient, f"Invoice Ward A {token}")
        _make_invoice(patient, rupees("100"))
        try:
            _, other = _mk_user("admin", department=f"Invoice Ward B {token}")
            assert client.get(f"/api/payments/patients/{patient}/invoices",
                              headers=other).status_code == 404
        finally:
            with db_session() as db:
                db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (patient,))
                db.execute("UPDATE admissions SET status = 'discharged' "
                           "WHERE patient_id = ?", (patient,))


class TestNamesOnMoneyLists:
    def test_receipts_carry_the_patient_name_mrn_and_who_took_them(self, client):
        uid, hdr = _mk_cashier()
        patient = _pid()
        client.post("/api/payments/receipts", headers=hdr, json={
            "patient_id": patient, "amount_paise": rupees("100"),
            "method": "cash", "purpose": "deposit",
        })
        rows = client.get("/api/payments/receipts", headers=hdr,
                          params={"patient_id": patient}).json()["receipts"]
        assert rows and rows[0]["patient_id"] == patient
        assert rows[0]["patient_name"] == f"Payments Patient {patient[-4:]}"
        assert rows[0]["patient_mrn"] == f"MRN-{patient}"
        assert rows[0]["received_by"] == uid
        assert rows[0]["received_by_name"] == "Test cashier"
        assert rows[0]["received_at"], "the Received column needs the time"

    def test_refunds_discounts_and_counters_name_their_people(self, client):
        uid, hdr = _mk_cashier()
        patient = _pid()
        receipt = client.post("/api/payments/receipts", headers=hdr, json={
            "patient_id": patient, "amount_paise": rupees("100"),
            "method": "cash", "purpose": "deposit",
        }).json()
        client.post("/api/payments/refunds", headers=hdr, json={
            "patient_id": patient, "amount_paise": rupees("10"),
            "reason": "overpaid", "payment_id": receipt["id"],
        })
        client.post("/api/payments/discounts", headers=hdr, json={
            "patient_id": patient, "reason": "hardship", "requested_paise": rupees("10"),
            "scope": "patient",
        })
        refund = client.get("/api/payments/refunds", headers=hdr,
                            params={"patient_id": patient}).json()["refunds"][0]
        assert refund["patient_name"] == f"Payments Patient {patient[-4:]}"
        assert refund["requested_by_name"] == "Test cashier"
        assert refund["decided_by_name"] is None
        discount = client.get("/api/payments/discounts", headers=hdr,
                              params={"patient_id": patient}).json()["discounts"][0]
        assert discount["patient_mrn"] == f"MRN-{patient}"
        assert discount["requested_by_name"] == "Test cashier"

        counter = _counter()
        client.post("/api/payments/counters/open", headers=hdr,
                    json={"counter_code": counter})
        session = client.get("/api/payments/counters", headers=hdr,
                             params={"counter_code": counter}).json()["sessions"][0]
        assert session["opened_by"] == uid
        assert session["opened_by_name"] == "Test cashier"

    def test_a_ward_scoped_account_is_given_no_other_wards_names(self, client):
        """The receipts list already crossed wards for an account with no
        patient filter; names must not make that wider than it was."""
        token = uuid.uuid4().hex[:6].upper()
        theirs = _pid()
        _admit(theirs, f"Names Ward B {token}")
        try:
            ps.record_payment(patient_id=theirs, amount_paise=rupees("100"), method="cash",
                              purpose="deposit", received_by="clerk-a")
            _, ward_a = _mk_user("admin", department=f"Names Ward A {token}")
            rows = [r for r in client.get("/api/payments/receipts", headers=ward_a,
                                          params={"limit": 500}).json()["receipts"]
                    if r["patient_id"] == theirs]
            assert rows, "the cross-ward receipt is in the list (pre-existing)"
            assert rows[0]["patient_name"] is None
            assert rows[0]["patient_mrn"] is None
        finally:
            with db_session() as db:
                db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (theirs,))
                db.execute("UPDATE admissions SET status = 'discharged' "
                           "WHERE patient_id = ?", (theirs,))


class TestRefusedMoneyDrawsNoNumber:
    """Every refusal is checked before next_document_number. A cancelled
    invoice, a closed counter session or an over-sized refund used to be
    refused only after the number was drawn, so each left a gap in a series an
    auditor reads as missing money (U329/F148)."""

    def _spy(self, monkeypatch):
        calls = []
        real = ps.next_document_number

        def spy(*a, **kw):
            calls.append(a)
            return real(*a, **kw)
        monkeypatch.setattr(ps, "next_document_number", spy)
        return calls

    def test_a_payment_against_a_cancelled_invoice(self, monkeypatch):
        patient = _pid()
        invoice = _make_invoice(patient, rupees("500"), status="cancelled")
        numbers = self._spy(monkeypatch)
        with pytest.raises(ps.PaymentError, match="not a debt"):
            ps.record_payment(patient_id=patient, amount_paise=rupees("500"), method="cash",
                              invoice_id=invoice, received_by="clerk-a")
        assert numbers == []

    def test_a_payment_into_a_closed_session(self, monkeypatch):
        session = ps.open_counter_session(counter_code=_counter(), opened_by="clerk-a",
                                          shift_label="morning", opening_float_paise=0)
        ps.close_counter_session(session["id"], declared_cash_paise=0, closed_by="clerk-a")
        numbers = self._spy(monkeypatch)
        with pytest.raises(ps.PaymentError, match="cannot take money"):
            ps.record_payment(patient_id=_pid(), amount_paise=rupees("100"), method="cash",
                              purpose="deposit", received_by="clerk-a",
                              counter_session_id=session["id"])
        assert numbers == []

    def test_an_oversized_refund_request(self, monkeypatch):
        patient = _pid()
        ps.record_payment(patient_id=patient, amount_paise=rupees("100"), method="upi",
                          reference_no="UTR-RF", purpose="deposit", received_by="clerk-a")
        numbers = self._spy(monkeypatch)
        with pytest.raises(ps.PaymentError, match="cannot refund"):
            ps.request_refund(patient_id=patient, amount_paise=rupees("500"),
                              reason="probe", requested_by="clerk-a")
        assert numbers == []

    def test_applying_more_advance_than_is_held(self, monkeypatch):
        patient = _pid()
        invoice = _make_invoice(patient, rupees("500"))
        numbers = self._spy(monkeypatch)
        with pytest.raises(ps.PaymentError, match="advance balance"):
            ps.apply_deposit_to_invoice(patient_id=patient, invoice_id=invoice,
                                        amount_paise=rupees("200"), applied_by="clerk-a")
        assert numbers == []
