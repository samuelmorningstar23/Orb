"""billing_service.refresh_invoice_payment_state after a receipt is reversed.

THE FAILURE (F094, billing leg): reverse_payment writes a NEW 'out' row carrying
reversal_of_payment_id and marks the original 'reversed' with its allocations
reversed. The billing read counted that 'out' leg in its `direct` sum while the
original had already left the live set, so an invoice whose ₹5,000 receipt was
reversed read amount_paid -₹5,000 and a balance of twice the bill, and
get_invoice wrote that back to the row.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import db_session
from services import billing_service as bs
from services import payments_service as ps
from services.money import rupees


def _patient() -> str:
    # Registered: the counter refuses money for an id that names nobody.
    pid = f"BREV{uuid.uuid4().hex[:10].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'discharged')",
            (pid, "Reversal Refresh Test", f"MRN-{pid}"),
        )
    return pid


def _invoice(patient_id: str, payable_paise: int) -> int:
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO invoices (patient_id, status, subtotal_paise, taxable_value_paise, "
            "total_paise, payable_paise, balance_paise) VALUES (?,?,?,?,?,?,?)",
            (patient_id, "issued", payable_paise, payable_paise, payable_paise,
             payable_paise, payable_paise),
        )
        return int(cur.lastrowid)


def test_reversing_an_allocated_receipt_returns_the_invoice_to_unpaid():
    patient = _patient()
    invoice = _invoice(patient, rupees("5000"))
    receipt = ps.record_payment(patient_id=patient, amount_paise=rupees("5000"),
                                method="cash", invoice_id=invoice,
                                received_by="clerk-a")
    assert bs.refresh_invoice_payment_state(invoice)["amount_paid_paise"] == rupees("5000")

    ps.reverse_payment(receipt["id"], reason="wrong patient", reversed_by="manager-1")

    header = bs.refresh_invoice_payment_state(invoice)
    assert header["amount_paid_paise"] == 0
    assert header["balance_paise"] == rupees("5000")
    assert header["status"] == "issued"

    # get_invoice writes the derived figures back; the stored row must agree.
    bs.get_invoice(invoice)
    with db_session() as db:
        row = db.execute(
            "SELECT amount_paid_paise, balance_paise, status FROM invoices WHERE id = ?",
            (invoice,),
        ).fetchone()
    assert row["amount_paid_paise"] == 0
    assert row["balance_paise"] == rupees("5000")
    assert row["status"] == "issued"


def test_a_counter_refund_still_reduces_what_was_paid():
    """Only the reversal leg is excluded: a plain money-out row on the invoice
    with no allocation (a refund at the counter) still counts against paid."""
    patient = _patient()
    invoice = _invoice(patient, rupees("5000"))
    with db_session() as db:
        db.execute(
            "INSERT INTO payments (receipt_number, patient_id, invoice_id, direction, "
            "amount_paise, method, status, received_by, received_at) "
            "VALUES (?, ?, ?, 'in', ?, 'cash', 'received', 'clerk-a', '2026-10-03 06:00:00')",
            (f"T-{uuid.uuid4().hex[:8]}", patient, invoice, rupees("3000")),
        )
        db.execute(
            "INSERT INTO payments (receipt_number, patient_id, invoice_id, direction, "
            "amount_paise, method, status, received_by, received_at) "
            "VALUES (?, ?, ?, 'out', ?, 'cash', 'received', 'clerk-b', '2026-10-03 07:00:00')",
            (f"T-{uuid.uuid4().hex[:8]}", patient, invoice, rupees("1000")),
        )
    header = bs.refresh_invoice_payment_state(invoice)
    assert header["amount_paid_paise"] == rupees("2000")
    assert header["balance_paise"] == rupees("3000")
