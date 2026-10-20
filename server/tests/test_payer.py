"""Orb — the insurance / TPA desk (services/payer_service.py + routes/payer_routes.py).

WHAT THESE TESTS ARE GUARDING, in the order the money is lost:

  1. THE ARITHMETIC. `claimed = disallowed + co-pay + TDS + received + short`,
     exactly, to the paise, on every input — including the ones where the co-pay
     percentage rounds. A short settlement the desk cannot reconcile is a
     hospital arguing with a payer about a number neither of them can derive.
  2. THE LIFECYCLE. A DENIED CLAIM CANNOT BECOME SETTLED. Not by API, not by
     status change, not by recording a receipt. It goes through an appeal, which
     is a fact somebody recorded and can point at.
  3. THE AGING CLOCK. Measured from when the CURRENT status began, never from
     the claim's creation — the difference between chasing a payer who has had
     the file for four hours and one who has had it for three weeks.
  4. THE HONESTY. Orb transmits nothing to any payer, and the words that say so
     are asserted in the service, in the route payloads and in the panel source.
     Deleting the sentence turns this file red.
"""
from tests.source_contract import contract_source
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth import hash_password  # noqa: E402
from database import db_session, get_db  # noqa: E402
from main import app  # noqa: E402
from services import payer_service as ps  # noqa: E402
from services.money import rupees  # noqa: E402

client = TestClient(app)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _ensure_admin(user_id: str = "PAYER-ADM") -> str:
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            # Seeded WITHOUT a department, deliberately. permissions.department_scope
            # returns None for such an account, which is this repo's documented
            # convention for a hospital-wide governance/back-office user — and a
            # TPA desk IS hospital-wide: it chases claims for every ward. Given a
            # department instead, the desk would be ward-scoped to "Administration",
            # a ward that has no patients, and could open nobody's policy.
            # The ward-scoped case is asserted separately, below.
            (user_id, "TPA Desk", "admin", None, hash_password("password")),
        )
        db.commit()
    finally:
        db.close()
    return user_id


def _login(user_id: str) -> dict:
    resp = client.post(
        "/api/auth/login", json={"employee_id": user_id, "password": "password"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


_PATIENT_SEQ = {"n": 0}


def _ensure_patient(name: str = "Claims Test Patient") -> str:
    _PATIENT_SEQ["n"] += 1
    pid = f"PAY-PT-{_PATIENT_SEQ['n']:04d}"
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, ward, bed, status) "
            "VALUES (?, ?, ?, ?, ?, 'admitted')",
            (pid, name, f"MRN-{pid}", "W1", "1"),
        )
        db.commit()
    finally:
        db.close()
    return pid


@pytest.fixture()
def tpa_payer():
    return ps.upsert_payer(
        code="TESTTPA",
        name="Test TPA Services Pvt Ltd",
        payer_type="tpa",
        claim_submission_mode="portal",
        preauth_required=True,
    )


def _claim_with_policy(
    *,
    claimed_paise: int,
    copay_bp: int = 0,
    claim_type: str = "cashless",
    payer_code: str = "TESTTPA",
) -> dict:
    """A patient with cover and a claim already carrying an amount."""
    patient_id = _ensure_patient()
    ps.capture_policy(
        patient_id=patient_id,
        payer_code=payer_code,
        policy_number="POL-00001",
        copay_percent_bp=copay_bp,
        sum_insured_paise=rupees("500000"),
        created_by="PAYER-ADM",
    )
    return ps.create_claim(
        patient_id=patient_id,
        claim_type=claim_type,
        claimed_paise=claimed_paise,
        created_by="PAYER-ADM",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  1. THE ARITHMETIC — short settlement, to the paise
# ══════════════════════════════════════════════════════════════════════════════

def test_short_settlement_exact_paise():
    """The canonical short settlement: claimed ₹1,00,000, approved ₹85,000,
    received ₹80,000. Every derived figure is asserted as an exact integer —
    no tolerance, because there is no such thing as a bill that is nearly right."""
    s = ps.compute_settlement(
        claimed_paise=rupees("100000"),
        approved_paise=rupees("85000"),
        settled_paise=rupees("80000"),
    )
    assert s.claimed_paise == 10_000_000
    assert s.approved_paise == 8_500_000
    assert s.disallowed_paise == 1_500_000           # ₹15,000 the payer refused
    assert s.expected_receipt_paise == 8_500_000     # no co-pay, no TDS
    assert s.settled_paise == 8_000_000
    assert s.short_settled_paise == 500_000          # ₹5,000 approved and never paid
    assert s.is_short_settled is True
    assert s.is_over_settled is False
    assert s.patient_liability_paise == 1_500_000


def test_reconciliation_identity_holds_exactly():
    """claimed = disallowed + co-pay + TDS + received + short-settled.

    THE invariant. Asserted across amounts and co-pay percentages chosen so the
    co-pay rounds in both directions — the case where an implementation that
    recomputes the payer's share from the percentage (instead of subtracting the
    already-rounded co-pay) drifts by a paise.
    """
    cases = [
        # (claimed, approved, copay_bp, tds, settled)
        (rupees("100000"), rupees("85000"), 0, 0, rupees("80000")),
        (rupees("100000"), rupees("85000"), 1000, 0, rupees("70000")),
        # 3333 bp of ₹1,234.57 → a genuinely fractional co-pay.
        (123457, 123457, 3333, 0, 40000),
        (123457, 100000, 3333, 1717, 50000),
        (1, 1, 5000, 0, 0),                 # one paise, half of it — rounds up
        (3, 3, 3333, 0, 1),                 # 0.9999 paise — rounds up to 1
        (rupees("250000"), rupees("175500"), 2000, 17550, rupees("120000")),
        (rupees("99999.99"), rupees("99999.99"), 1, 0, 0),
    ]
    for claimed, approved, copay_bp, tds, settled in cases:
        s = ps.compute_settlement(
            claimed_paise=claimed,
            approved_paise=approved,
            copay_percent_bp=copay_bp,
            tds_paise=tds,
            settled_paise=settled,
        )
        total = (
            s.disallowed_paise
            + s.copay_paise
            + s.tds_paise
            + (s.settled_paise or 0)
            + (s.short_settled_paise or 0)
        )
        assert total == claimed, (
            f"reconciliation broke for claimed={claimed} approved={approved} "
            f"copay_bp={copay_bp} tds={tds} settled={settled}: "
            f"disallowed={s.disallowed_paise} copay={s.copay_paise} tds={s.tds_paise} "
            f"settled={s.settled_paise} short={s.short_settled_paise} sum={total}"
        )
        # And the intermediate identity, which is what makes the first one hold.
        assert s.expected_receipt_paise == approved - s.copay_paise - tds


def test_copay_is_charged_on_the_admissible_amount_not_the_billed_amount():
    """A 10% co-pay on a ₹1,00,000 claim of which the payer allowed ₹80,000 is
    ₹8,000 — not ₹10,000. Charging co-pay on the disallowed portion too bills
    the patient twice for the same rupee."""
    s = ps.compute_settlement(
        claimed_paise=rupees("100000"),
        approved_paise=rupees("80000"),
        copay_percent_bp=1000,
    )
    assert s.copay_paise == rupees("8000")
    assert s.patient_liability_paise == rupees("20000") + rupees("8000")
    assert s.expected_receipt_paise == rupees("72000")


def test_tds_is_never_billed_to_the_patient():
    """TDS is the payer's tax on its own payment. It reduces what the hospital
    receives; it does not increase what the patient owes."""
    with_tds = ps.compute_settlement(
        claimed_paise=rupees("100000"), approved_paise=rupees("100000"), tds_paise=rupees("2000")
    )
    without = ps.compute_settlement(
        claimed_paise=rupees("100000"), approved_paise=rupees("100000"), tds_paise=0
    )
    assert with_tds.patient_liability_paise == without.patient_liability_paise == 0
    assert with_tds.expected_receipt_paise == rupees("98000")


def test_undecided_claim_reports_unknown_not_zero():
    """A claim awaiting a decision and a claim approved for nothing look
    identical as zeros and mean opposite things."""
    s = ps.compute_settlement(claimed_paise=rupees("50000"))
    assert s.approved_paise is None
    assert s.disallowed_paise is None
    assert s.expected_receipt_paise is None
    assert s.patient_liability_paise is None
    assert s.is_decided is False
    d = s.as_dict()
    assert d["approved_display"] == "Awaiting decision"
    assert d["disallowed_display"] == "Not yet known"
    assert "0.00" not in d["approved_display"]


def test_over_settlement_is_flagged_not_silently_absorbed():
    s = ps.compute_settlement(
        claimed_paise=rupees("10000"),
        approved_paise=rupees("10000"),
        settled_paise=rupees("11000"),
    )
    assert s.short_settled_paise == -rupees("1000")
    assert s.is_over_settled is True
    assert s.is_short_settled is False


def test_reimbursement_is_not_a_hospital_receivable():
    """Under reimbursement the PATIENT pays the hospital and recovers from the
    insurer. Counting it as a hospital receivable inflates the receivable report
    with money that is not coming to the hospital at all."""
    s = ps.compute_settlement(
        claimed_paise=rupees("60000"),
        approved_paise=rupees("50000"),
        claim_type="reimbursement",
    )
    assert s.hospital_receivable_paise == 0
    assert s.patient_liability_paise == rupees("60000")


def test_impossible_figures_are_refused():
    with pytest.raises(ps.ClaimError, match="exceeds claimed"):
        ps.compute_settlement(claimed_paise=rupees("100"), approved_paise=rupees("101"))
    with pytest.raises(ps.ClaimError, match="exceeds the payer"):
        ps.compute_settlement(
            claimed_paise=rupees("100"), approved_paise=rupees("100"), tds_paise=rupees("101")
        )
    with pytest.raises(Exception):
        # Floats are rejected at the money boundary — 2.0 is indistinguishable
        # from a rounding artefact once it is in the column.
        ps.compute_settlement(claimed_paise=100.0)


# ══════════════════════════════════════════════════════════════════════════════
#  2. THE LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

def test_happy_path_cashless_lifecycle(tpa_payer):
    claim = _claim_with_policy(claimed_paise=rupees("100000"), copay_bp=1000)
    cid = claim["id"]
    assert claim["status"] == "draft"

    claim = ps.record_preauth_request(claim_id=cid, requested_paise=rupees("90000"))
    assert claim["status"] == "preauth_requested"
    assert claim["preauth_status"] == "requested"

    claim = ps.record_preauth_decision(
        claim_id=cid, decision="partially_approved",
        approved_paise=rupees("60000"), reference="PA-991",
    )
    assert claim["status"] == "preauth_approved"
    assert claim["preauth_status"] == "partially_approved"
    assert claim["preauth_approved_paise"] == rupees("60000")

    claim = ps.record_submission(claim_id=cid, external_ref="TPA-CLM-77")
    assert claim["status"] == "submitted"
    assert claim["submitted_at"]

    claim = ps.record_query(claim_id=cid, query_text="Discharge summary illegible")
    assert claim["status"] == "queried"
    claim = ps.record_query(claim_id=cid, query_text="Re-scanned and uploaded", answered=True)
    assert claim["status"] == "submitted"

    claim = ps.record_decision(
        claim_id=cid, decision="partially_approved", approved_paise=rupees("70000")
    )
    assert claim["status"] == "partially_approved"
    assert claim["disallowed_paise"] == rupees("30000")
    # 10% co-pay on the ₹70,000 admitted + the ₹30,000 the payer refused.
    assert claim["patient_liability_paise"] == rupees("37000")

    claim = ps.record_settlement(claim_id=cid, settled_paise=rupees("60000"))
    assert claim["status"] == "settled"
    assert claim["settlement"]["short_settled_paise"] == rupees("3000")
    assert claim["settlement"]["is_short_settled"] is True

    claim = ps.close_claim(claim_id=cid)
    assert claim["status"] == "closed"


def test_denied_claim_cannot_silently_become_settled(tpa_payer):
    """THE transition that must not exist.

    If money arrives against a denied claim, either the denial was overturned —
    which is an appeal somebody recorded — or the receipt belongs elsewhere.
    Both need a person. Allowing the shortcut lets a desk clear its denial list
    by marking things settled, and the denial list is the only place a hospital
    finds out a payer is systematically refusing a procedure.
    """
    claim = _claim_with_policy(claimed_paise=rupees("40000"))
    cid = claim["id"]
    ps.record_submission(claim_id=cid, external_ref="X-1")
    ps.record_decision(
        claim_id=cid, decision="denied",
        denial_code="EXCLUSION", denial_reason="Cosmetic procedure excluded",
    )
    assert ps.get_claim(cid)["status"] == "denied"

    with pytest.raises(ps.IllegalTransition) as exc:
        ps.record_settlement(claim_id=cid, settled_paise=rupees("40000"))
    assert "denied" in str(exc.value) and "settled" in str(exc.value)

    # Still denied. Nothing was written.
    after = ps.get_claim(cid)
    assert after["status"] == "denied"
    assert after["settled_paise"] is None

    # 'settled' is not even offered as a next step.
    assert "settled" not in after["legal_next_statuses"]

    # The legal route: appeal → re-decision → settlement.
    ps.record_appeal(claim_id=cid, grounds="Procedure was reconstructive, not cosmetic")
    ps.record_decision(claim_id=cid, decision="approved", approved_paise=rupees("40000"))
    settled = ps.record_settlement(claim_id=cid, settled_paise=rupees("40000"))
    assert settled["status"] == "settled"


def test_the_transition_table_itself_forbids_denied_to_settled():
    """Asserted on the table as well as through the API, so a refactor that
    stops routing settlements through _transition cannot lose the rule."""
    assert "settled" not in ps.LEGAL_TRANSITIONS["denied"]
    assert "appealed" in ps.LEGAL_TRANSITIONS["denied"]


@pytest.mark.parametrize("frm,to", [
    ("draft", "approved"),          # never submitted, never decided
    ("draft", "settled"),           # money against a claim nobody sent
    ("submitted", "preauth_approved"),
    ("settled", "denied"),          # a settled claim cannot be un-paid
    ("cancelled", "submitted"),     # terminal
    ("closed", "appealed"),         # terminal
    ("preauth_denied", "submitted"),  # a denied pre-auth is not permission
])
def test_illegal_transitions_are_refused(frm, to):
    assert to not in ps.LEGAL_TRANSITIONS[frm], f"{frm} -> {to} must not be legal"


def test_terminal_statuses_go_nowhere():
    for status in ps.TERMINAL_STATUSES:
        assert ps.LEGAL_TRANSITIONS[status] == frozenset()


def test_denial_must_carry_a_reason(tpa_payer):
    """An unexplained denial cannot be appealed, so it cannot be recorded."""
    claim = _claim_with_policy(claimed_paise=rupees("10000"))
    ps.record_submission(claim_id=claim["id"], external_ref="X-2")
    with pytest.raises(ps.ClaimError, match="reason"):
        ps.record_decision(claim_id=claim["id"], decision="denied")


def test_full_approval_cannot_be_recorded_as_partial_and_vice_versa(tpa_payer):
    claim = _claim_with_policy(claimed_paise=rupees("10000"))
    ps.record_submission(claim_id=claim["id"], external_ref="X-3")
    with pytest.raises(ps.ClaimError, match="partially_approved"):
        # Approved for less than claimed, but labelled a full approval.
        ps.record_decision(
            claim_id=claim["id"], decision="approved", approved_paise=rupees("9000")
        )
    with pytest.raises(ps.ClaimError, match="less than the amount"):
        ps.record_decision(
            claim_id=claim["id"], decision="partially_approved",
            approved_paise=rupees("10000"),
        )


def test_cannot_record_a_submission_for_zero(tpa_payer):
    """A ₹0 submission sits in the queue forever, reconciled against nothing."""
    patient_id = _ensure_patient()
    ps.capture_policy(patient_id=patient_id, payer_code="TESTTPA", policy_number="P-0")
    claim = ps.create_claim(patient_id=patient_id, created_by="PAYER-ADM")
    assert claim["claimed_paise"] == 0
    with pytest.raises(ps.ClaimError, match="₹0"):
        ps.record_submission(claim_id=claim["id"])


# ── seeding a claim from an invoice: the payer's SHARE, never the whole bill ──

def _invoice(
    patient_id: str, *, payable_paise: int, payer_share_paise: int
) -> int:
    """An issued invoice with the two shares billing_service.create_invoice
    computes: the co-pay percentage applied to the payable, and the remainder.

    Written directly, like the payments suite's own fixture, so this file asserts
    the PAYER side of the boundary without going red when a sibling module's
    signature moves. The two columns are the whole contract: whatever split
    billing wrote, the claim must seed from `payer_responsibility_paise`.
    """
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO invoices (patient_id, status, invoice_type, subtotal_paise, "
            "taxable_value_paise, total_paise, payable_paise, balance_paise, "
            "patient_responsibility_paise, payer_responsibility_paise) "
            "VALUES (?, 'issued', 'final', ?, ?, ?, ?, ?, ?, ?)",
            (patient_id, payable_paise, payable_paise, payable_paise, payable_paise,
             payable_paise, payable_paise - payer_share_paise, payer_share_paise),
        )
        db.commit()
        return int(cur.lastrowid)
    finally:
        db.close()


def test_a_hundred_percent_copay_invoice_claims_nothing_from_the_payer(tpa_payer):
    """THE ZERO THAT WAS NOT A ZERO.

    A 100%-co-pay policy leaves the payer owing nothing: billing writes
    `payer_responsibility_paise = 0` and `patient_responsibility_paise` = the
    whole bill. The seeding used an `or` chain, so that legitimate 0 was falsy
    and fell through to `payable_paise` — and the desk opened a claim for the
    ENTIRE ₹1,00,000 against a payer who owed none of it. That figure goes on a
    submission, comes back denied, and is argued about for weeks.

    Hand-computed: payable 10_000_000p, payer share 0p → claimed 0p, exactly.

    PROVEN ABLE TO FAIL: restore the `or` chain in payer_service.create_claim
    (`int(inv["payer_responsibility_paise"] or 0) or int(inv["payable_paise"] or 0)
    or None`) and this reads 10_000_000.
    """
    patient_id = _ensure_patient()
    ps.capture_policy(
        patient_id=patient_id, payer_code="TESTTPA", policy_number="POL-COPAY-100",
        copay_percent_bp=10_000, created_by="PAYER-ADM",
    )
    invoice_id = _invoice(patient_id, payable_paise=10_000_000, payer_share_paise=0)

    claim = ps.create_claim(
        patient_id=patient_id, invoice_id=invoice_id, created_by="PAYER-ADM")

    assert claim["claimed_paise"] == 0
    assert claim["claimed_paise"] != 10_000_000, \
        "a 100%-co-pay claim must not bill the whole invoice to the payer"
    # The claim FILE is legitimate (the desk may still need to record a denial or
    # an appeal); what it claims is zero, and the timeline says why in words.
    events = ps.get_claim(claim["id"])["events"]
    created = next(e for e in events if e["event_type"] == "created")
    assert created["amount_paise"] == 0
    assert "Nothing is claimable" in (created["remark"] or "")


def test_a_part_copay_invoice_claims_exactly_the_payers_share(tpa_payer):
    """The other side of the same guard: a 20% co-pay on ₹1,00,000.

        payable                 = 10_000_000p
        patient (20% co-pay)    =  2_000_000p
        payer share             =  8_000_000p  ← what is claimed
    """
    patient_id = _ensure_patient()
    ps.capture_policy(
        patient_id=patient_id, payer_code="TESTTPA", policy_number="POL-COPAY-20",
        copay_percent_bp=2_000, created_by="PAYER-ADM",
    )
    invoice_id = _invoice(
        patient_id, payable_paise=10_000_000, payer_share_paise=8_000_000)

    claim = ps.create_claim(
        patient_id=patient_id, invoice_id=invoice_id, created_by="PAYER-ADM")
    assert claim["claimed_paise"] == 8_000_000


def test_an_explicit_claimed_amount_still_wins_over_the_invoice(tpa_payer):
    """Seeding is a convenience, not a rule: a desk that types a figure gets it."""
    patient_id = _ensure_patient()
    ps.capture_policy(
        patient_id=patient_id, payer_code="TESTTPA", policy_number="POL-EXPLICIT",
        copay_percent_bp=10_000, created_by="PAYER-ADM",
    )
    invoice_id = _invoice(patient_id, payable_paise=10_000_000, payer_share_paise=0)
    claim = ps.create_claim(
        patient_id=patient_id, invoice_id=invoice_id,
        claimed_paise=rupees("25000"), created_by="PAYER-ADM",
    )
    assert claim["claimed_paise"] == 2_500_000


def test_settlement_before_a_decision_is_refused(tpa_payer):
    """Money cannot be reconciled against an approval that does not exist.

    The lifecycle catches it first — 'submitted' has no path to 'settled' — and
    record_settlement carries a second, independent check on `approved_paise`
    for the case where a claim reaches an approved status with no amount on it
    (an import, a repair script). Both are asserted, because the belt is only a
    belt if the braces are tested separately.
    """
    claim = _claim_with_policy(claimed_paise=rupees("10000"))
    cid = claim["id"]
    ps.record_submission(claim_id=cid, external_ref="X-4")
    with pytest.raises(ps.IllegalTransition, match="cannot become 'settled'"):
        ps.record_settlement(claim_id=cid, settled_paise=rupees("10000"))

    # Now force the pathological row the second guard exists for: an 'approved'
    # claim carrying no approved amount, which no normal path can produce.
    ps.record_decision(claim_id=cid, decision="approved", approved_paise=rupees("10000"))
    with db_session() as db:
        db.execute("UPDATE claims SET approved_paise = NULL WHERE id = ?", (cid,))
    with pytest.raises(ps.ClaimError, match="before the payer"):
        ps.record_settlement(claim_id=cid, settled_paise=rupees("10000"))


def test_claimed_amount_frozen_after_submission(tpa_payer):
    claim = _claim_with_policy(claimed_paise=rupees("10000"))
    ps.set_claimed_amount(claim_id=claim["id"], claimed_paise=rupees("12000"))
    assert ps.get_claim(claim["id"])["claimed_paise"] == rupees("12000")
    ps.record_submission(claim_id=claim["id"], external_ref="X-5")
    with pytest.raises(ps.ClaimError, match="cannot be changed"):
        ps.set_claimed_amount(claim_id=claim["id"], claimed_paise=rupees("13000"))


def test_preauth_approval_cannot_exceed_the_request(tpa_payer):
    claim = _claim_with_policy(claimed_paise=rupees("100000"))
    ps.record_preauth_request(claim_id=claim["id"], requested_paise=rupees("50000"))
    with pytest.raises(ps.ClaimError, match="exceeds the amount requested"):
        ps.record_preauth_decision(
            claim_id=claim["id"], decision="approved", approved_paise=rupees("60000")
        )


def test_no_claim_against_a_self_pay_patient():
    """A patient with no cover has nobody to claim from. Creating the claim
    anyway leaves it in the work queue forever, waiting for a submission that
    cannot happen."""
    patient_id = _ensure_patient()
    with pytest.raises(ps.ClaimError, match="no payer policy"):
        ps.create_claim(patient_id=patient_id, created_by="PAYER-ADM")


# ══════════════════════════════════════════════════════════════════════════════
#  3. POLICY CAPTURE
# ══════════════════════════════════════════════════════════════════════════════

def test_unknown_sum_insured_is_not_zero_cover(tpa_payer):
    """The card shows a policy number, not a balance. A hospital that reads ₹0
    of cover as a fact refuses a cashless admission it should have accepted."""
    patient_id = _ensure_patient()
    policy = ps.capture_policy(
        patient_id=patient_id, payer_code="TESTTPA", policy_number="POL-NOSUM"
    )
    assert policy["sum_insured_paise"] is None
    assert policy["sum_insured_is_configured"] is False
    assert policy["sum_insured_display"] == "Not known"
    assert "0.00" not in policy["sum_insured_display"]


def test_capturing_a_primary_policy_demotes_the_previous_one(tpa_payer):
    patient_id = _ensure_patient()
    first = ps.capture_policy(
        patient_id=patient_id, payer_code="TESTTPA", policy_number="OLD-CARD"
    )
    second = ps.capture_policy(
        patient_id=patient_id, payer_code="TESTTPA", policy_number="NEW-CARD"
    )
    rows = {p["id"]: p for p in ps.list_policies(patient_id=patient_id)}
    assert rows[first["id"]]["is_primary"] is False
    assert rows[second["id"]]["is_primary"] is True


def test_policy_for_an_unknown_patient_is_refused(tpa_payer):
    """No foreign keys exist on these tables by design, so this guard IS the
    referential integrity. A policy keyed to a mistyped patient id is money the
    desk will never find."""
    with pytest.raises(ps.PayerError, match="No patient with ID NO-SUCH-PATIENT is registered"):
        ps.capture_policy(patient_id="NO-SUCH-PATIENT", payer_code="TESTTPA")


def test_the_cover_list_names_the_patient(tpa_payer):
    """The cover list printed bare ids the desk cannot read; it now carries the
    name and MRN, as every claim already did."""
    patient_id = _ensure_patient("Cover List Patient")
    ps.capture_policy(patient_id=patient_id, payer_code="TESTTPA", policy_number="POL-NAME")
    row = ps.list_policies(patient_id=patient_id)[0]
    assert row["patient_name"] == "Cover List Patient"
    assert row["patient_mrn"] == f"MRN-{patient_id}"


def _clm_next() -> int:
    db = get_db()
    try:
        row = db.execute(
            "SELECT next_number FROM document_number_series WHERE series_code = 'CLM'"
        ).fetchone()
    finally:
        db.close()
    return int(row["next_number"]) if row else 0


def test_a_claim_for_an_unknown_patient_says_so_before_asking_for_a_policy(tpa_payer):
    """The patient is checked first. An unknown id used to fall through to the
    policy lookup and come back "has no payer policy on file", sending the desk
    to capture cover for a patient who does not exist."""
    before = _clm_next()
    with pytest.raises(ps.ClaimError) as exc:
        ps.create_claim(patient_id="PAT-NOBODY99", created_by="PAYER-ADM")
    message = str(exc.value)
    assert "No patient with ID PAT-NOBODY99 is registered" in message
    assert "policy" not in message
    assert _clm_next() == before, "a refused claim must not spend a CLM number"


def test_the_no_policy_refusal_names_the_patient_not_the_id():
    patient_id = _ensure_patient("Selfpay Named Patient")
    with pytest.raises(ps.ClaimError) as exc:
        ps.create_claim(patient_id=patient_id, created_by="PAYER-ADM")
    assert "Selfpay Named Patient" in str(exc.value)
    assert f"MRN-{patient_id}" in str(exc.value)


def test_self_pay_is_excluded_from_the_claim_payer_list():
    codes = {p["code"] for p in ps.list_payers(include_self_pay=False)}
    assert "SELF" not in codes
    assert "SELF" in {p["code"] for p in ps.list_payers(include_self_pay=True)}


# ══════════════════════════════════════════════════════════════════════════════
#  4. AGING — from the RIGHT timestamps
# ══════════════════════════════════════════════════════════════════════════════

def _backdate_event(claim_id: int, event_type: str, when: str) -> None:
    with db_session() as db:
        db.execute(
            "UPDATE claim_events SET occurred_at = ? WHERE claim_id = ? AND event_type = ?",
            (when, claim_id, event_type),
        )


def _backdate_column(claim_id: int, column: str, when: str) -> None:
    assert column in {"created_at", "submitted_at", "decided_at", "settled_at"}
    with db_session() as db:
        db.execute(f"UPDATE claims SET {column} = ? WHERE id = ?", (when, claim_id))


def test_aging_measures_the_current_status_not_the_claim_age(tpa_payer):
    """THE aging bug this guards against: a claim drafted 40 days ago and
    submitted an hour ago must NOT read as 40 days pending with the payer. The
    desk would spend its morning chasing a payer who has had the file since
    breakfast."""
    claim = _claim_with_policy(claimed_paise=rupees("20000"))
    cid = claim["id"]
    _backdate_column(cid, "created_at", "2026-07-01 09:00:00")
    _backdate_event(cid, "created", "2026-07-01 09:00:00")

    # Still a draft: it genuinely IS 40+ days old, and says so.
    drafted = ps.get_claim(cid)
    assert drafted["aging_anchor_source"] == "created_at"
    assert drafted["age_days"] is not None and drafted["age_days"] > 30

    ps.record_submission(claim_id=cid, external_ref="AGE-1")
    submitted = ps.get_claim(cid)
    assert submitted["age_days"] == 0, (
        "a freshly submitted claim must age from its submission, not its creation"
    )
    assert submitted["age_bucket"] == "0-7"
    assert submitted["aging_anchor_source"] in ("event:submitted", "submitted_at")
    assert submitted["aging_anchor_label"] == "Submitted to payer"


def test_a_query_restarts_the_clock_on_the_hospital(tpa_payer):
    """While a query is open the delay is the HOSPITAL's. An aging report that
    keeps counting from the original submission blames the payer for the two
    weeks the hospital took to find a discharge summary."""
    claim = _claim_with_policy(claimed_paise=rupees("20000"))
    cid = claim["id"]
    ps.record_submission(claim_id=cid, external_ref="AGE-2")
    _backdate_column(cid, "submitted_at", "2026-06-01 09:00:00")
    _backdate_event(cid, "submitted", "2026-06-01 09:00:00")

    aged = ps.get_claim(cid)
    assert aged["age_days"] > 30

    ps.record_query(claim_id=cid, query_text="Send the implant sticker")
    queried = ps.get_claim(cid)
    assert queried["status"] == "queried"
    assert queried["age_days"] == 0
    assert queried["aging_anchor_source"] == "event:query_raised"
    assert queried["aging_anchor_label"] == "Query raised"


def test_aging_buckets_and_stale_flag(tpa_payer):
    claim = _claim_with_policy(claimed_paise=rupees("20000"))
    cid = claim["id"]
    ps.record_submission(claim_id=cid, external_ref="AGE-3")
    _backdate_column(cid, "submitted_at", "2026-07-01 09:00:00")
    _backdate_event(cid, "submitted", "2026-07-01 09:00:00")

    row = ps.get_claim(cid)
    # 2026-07-01 → the suite's "today" is well past 31 days later.
    assert row["age_bucket"] in ("31-60", "60+")
    assert row["is_stale"] is True

    queue = ps.work_queue()
    ours = next(c for c in queue["claims"] if c["id"] == cid)
    assert ours["is_stale"] is True
    assert queue["stale_count"] >= 1
    assert {b["bucket"] for b in queue["buckets"]} >= {"0-7", "8-14", "15-30", "31-60", "60+"}


def test_work_queue_is_ordered_oldest_first_and_excludes_finished_claims(tpa_payer):
    old = _claim_with_policy(claimed_paise=rupees("11111"))
    ps.record_submission(claim_id=old["id"], external_ref="OLD")
    _backdate_column(old["id"], "submitted_at", "2026-01-01 09:00:00")
    _backdate_event(old["id"], "submitted", "2026-01-01 09:00:00")

    new = _claim_with_policy(claimed_paise=rupees("22222"))
    ps.record_submission(claim_id=new["id"], external_ref="NEW")

    queue = ps.work_queue()
    ids = [c["id"] for c in queue["claims"]]
    assert ids.index(old["id"]) < ids.index(new["id"])

    # A settled claim leaves the queue: it is no longer stuck.
    ps.record_decision(claim_id=new["id"], decision="approved", approved_paise=rupees("22222"))
    ps.record_settlement(claim_id=new["id"], settled_paise=rupees("22222"))
    assert new["id"] not in [c["id"] for c in ps.work_queue()["claims"]]


def test_unparseable_timestamp_is_unknown_not_zero_days():
    """A six-month-old claim whose anchor cannot be parsed must not land at the
    top of the freshest bucket."""
    assert ps._parse_ts("not a date") is None
    assert ps._age_days(None) is None
    assert ps._bucket_for(None) == "unknown"


# ══════════════════════════════════════════════════════════════════════════════
#  5. THE DENIAL LOG
# ══════════════════════════════════════════════════════════════════════════════

def test_denial_log_records_reason_codes_and_money_at_stake(tpa_payer):
    claim = _claim_with_policy(claimed_paise=rupees("30000"))
    ps.record_submission(claim_id=claim["id"], external_ref="DEN-1")
    ps.record_decision(
        claim_id=claim["id"], decision="denied",
        denial_code="DOCUMENT_DEFICIENCY",
        denial_reason="Investigation reports not attached",
    )
    log = ps.denial_log()
    ours = next(d for d in log["denials"] if d["claim_id"] == claim["id"])
    assert ours["denial_code"] == "DOCUMENT_DEFICIENCY"
    assert ours["denial_reason"] == "Investigation reports not attached"
    assert ours["amount_at_stake_paise"] == rupees("30000")
    assert any(e["denial_code"] == "DOCUMENT_DEFICIENCY" for e in log["by_code"])


def test_denial_reason_codes_are_declared_as_orbs_own_not_a_standard():
    """There is no national denial-code standard in India. Claiming these are
    the payer's official codes would be a lie about somebody else's document."""
    log = ps.denial_log()
    assert "not a payer" in log["codes_are_local"].lower()
    codes = dict(ps.DENIAL_REASON_CODES)
    assert "DOCUMENT_DEFICIENCY" in codes and "OTHER" in codes


# ══════════════════════════════════════════════════════════════════════════════
#  6. THE HONESTY RULE — Orb transmits nothing
# ══════════════════════════════════════════════════════════════════════════════

def test_no_transmission_wording_says_the_three_things_that_matter():
    notice = ps.NO_TRANSMISSION_NOTICE.lower()
    assert "does not connect" in notice
    assert "tpa" in notice and "insurer" in notice
    assert "not transmitted" in notice or "nothing on this screen is transmitted" in notice
    assert "elsewhere" in notice or "portal" in notice
    short = ps.NO_TRANSMISSION_SHORT.lower()
    assert "not sent" in short and "payer" in short


def test_no_function_in_this_module_claims_to_send():
    """Naming is the guard that survives a rewrite. If a function here is ever
    called `send_claim` or `submit_to_payer`, either it actually transmits — in
    which case this whole file's premise changed — or it is lying in the one
    place a developer looks first."""
    forbidden = ("send_", "transmit_", "submit_to_", "upload_to_payer", "post_to_payer")
    for name in ps.__all__:
        assert not any(name.startswith(f) for f in forbidden), (
            f"{name} implies Orb transmits something. It does not."
        )


def test_every_route_payload_carries_the_notice(tpa_payer):
    headers = _login(_ensure_admin())
    claim = _claim_with_policy(claimed_paise=rupees("15000"))
    paths = [
        "/api/payer/meta",
        "/api/payer/summary",
        "/api/payer/payers",
        "/api/payer/claims",
        f"/api/payer/claims/{claim['id']}",
        "/api/payer/work-queue",
        "/api/payer/denials",
        "/api/payer/policies",
    ]
    for path in paths:
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200, f"{path}: {resp.text}"
        body = resp.json()
        assert body["notice"] == ps.NO_TRANSMISSION_NOTICE, path
        assert body["transmits_to_payer"] is False, path


def test_recording_a_submission_says_it_did_not_transmit(tpa_payer):
    headers = _login(_ensure_admin())
    claim = _claim_with_policy(claimed_paise=rupees("15000"))
    resp = client.post(
        f"/api/payer/claims/{claim['id']}/submission",
        json={"external_ref": "PORTAL-123"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transmits_to_payer"] is False
    assert body["claim"]["status"] == "submitted"
    assert body["claim"]["transmission_notice"] == ps.NO_TRANSMISSION_SHORT


def test_the_panel_renders_the_notice():
    """The words have to reach a human, not merely a JSON payload. Asserted
    against the panel source because that is the artefact a clerk actually
    reads."""
    panel = os.path.join(
        os.path.dirname(__file__), "..", "..", "src", "pages", "panels", "PayerPanel.tsx"
    )
    with open(panel, encoding="utf-8") as fh:
        source = fh.read()
    assert "does not connect to any TPA" in source
    assert "not sent to the payer" in source.lower()
    # And no verb anywhere in the panel that claims a transmission.
    for lie in ("Send to payer", "Submit to TPA", "Transmit claim"):
        assert lie not in source, f"the panel offers to {lie!r}, which Orb cannot do"


# ══════════════════════════════════════════════════════════════════════════════
#  7. ROUTES — RBAC, module flag, status codes
# ══════════════════════════════════════════════════════════════════════════════

def test_routes_require_authentication():
    assert client.get("/api/payer/claims").status_code in (401, 403)
    assert client.post("/api/payer/claims", json={"patient_id": "X"}).status_code in (401, 403)


def test_clinical_roles_do_not_reach_the_tpa_desk():
    """A nurse does not chase a TPA denial, and the rail does not offer her the
    tab. The server refuses it too — a UI-only restriction is not one."""
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            ("PAYER-NURSE", "Ward Nurse", "nurse", "W1", hash_password("password")),
        )
        db.commit()
    finally:
        db.close()
    headers = _login("PAYER-NURSE")
    assert client.get("/api/payer/claims", headers=headers).status_code == 403
    assert client.get("/api/payer/work-queue", headers=headers).status_code == 403
    # And the refusal names the desk's OWN permission, not the governance one it
    # used to borrow — if this ever reads `admin.overview` again, the split has
    # been reverted somewhere.
    body = client.get("/api/payer/claims", headers=headers).json()
    assert body["detail"]["required"] == ["payer.read"]


# ── The read / write split ───────────────────────────────────────────────────
# Until 2026-08-12 every route here was gated on `admin.overview` — the
# permission that also opens the audit log and the governance dashboard — so a
# TPA clerk recording a settlement had to be handed the hospital's own
# tamper-evidence record to do their job. `payer.read` / `payer.write` exist to
# undo that, and these three tests are what makes the claim true rather than
# merely written down.

def test_a_holder_of_payer_read_cannot_write():
    """Reading the aging report is a finance manager's Monday. Recording a
    settlement moves money in the hospital's books. One permission cannot mean
    both, or the split is decoration.

    The same account, the same token — one permission removed — and every write
    stops. Including `POST /claims/{id}/note`, which is the cheapest-looking
    write on the desk and still mutates a claim file.
    """
    import permissions as perms

    headers = _login(_ensure_admin())
    patient_id = _ensure_patient("Read Only Desk Patient")
    claim = _claim_with_policy(claimed_paise=rupees("18000"))

    read_only = set(perms.ROLE_PERMISSIONS["admin"]) - {perms.PAYER_WRITE}
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(perms.ROLE_PERMISSIONS, "admin", read_only)

        # Reading the desk is untouched.
        assert client.get("/api/payer/claims", headers=headers).status_code == 200
        assert client.get(
            f"/api/payer/claims/{claim['id']}", headers=headers
        ).status_code == 200
        assert client.get("/api/payer/work-queue", headers=headers).status_code == 200

        # Every write is refused — the master, the policy capture, the lifecycle
        # and the note.
        writes = [
            ("/api/payer/payers", {"code": "RO-TPA", "name": "Read Only TPA"}),
            ("/api/payer/policies",
             {"patient_id": patient_id, "payer_code": "TESTTPA", "policy_number": "RO-1"}),
            (f"/api/payer/claims/{claim['id']}/submission", {"external_ref": "RO-2"}),
            (f"/api/payer/claims/{claim['id']}/settlement",
             {"settled_paise": rupees("18000")}),
            (f"/api/payer/claims/{claim['id']}/note", {"note": "read-only desk"}),
        ]
        for path, body in writes:
            resp = client.post(path, json=body, headers=headers)
            assert resp.status_code == 403, f"{path} was writable on payer.read alone"
            assert resp.json()["detail"]["missing"] == ["payer.write"], path

    # Nothing leaked past the refusals: the claim is where it was.
    assert ps.get_claim(claim["id"])["status"] == "draft"


def test_the_desk_works_without_the_governance_grant(tpa_payer):
    """THE point of the change.

    An account holding nothing but `payer.read` + `payer.write` can run the
    entire insurance desk — and cannot read the audit log or open the governance
    dashboard. Before the split those came in the same grant, so the desk whose
    work the audit trail records could read the trail.
    """
    import permissions as perms

    headers = _login(_ensure_admin())
    patient_id = _ensure_patient("No Governance Patient")

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(
            perms.ROLE_PERMISSIONS, "admin", {perms.PAYER_READ, perms.PAYER_WRITE}
        )

        # The desk: reachable on payer.* alone, read and write.
        assert client.get("/api/payer/claims", headers=headers).status_code == 200
        assert client.get("/api/payer/denials", headers=headers).status_code == 200
        resp = client.post(
            "/api/payer/policies",
            json={"patient_id": patient_id, "payer_code": "TESTTPA",
                  "policy_number": "NO-GOV-1"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        # The governance surfaces the old grant dragged along with it: gone.
        assert client.get("/api/audit/logs", headers=headers).status_code == 403
        assert client.get("/api/admin/overview", headers=headers).status_code == 403


def test_every_write_route_is_gated_on_payer_write():
    """Drift guard, asserted on the source rather than on one sampled route.

    A POST added later that copies its `Depends(...)` from the GET above it
    would be a write a read-only desk account could make, and no functional test
    that nobody wrote would catch it. So the rule is checked structurally: every
    `@router.get` takes `_DESK_READ`, everything else takes `_DESK_WRITE`.
    """
    import ast
    import re

    from routes import payer_routes as pr

    source = contract_source(pr)
    handlers = [
        node for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
                for d in node.decorator_list)
    ]
    blocks = [
        next(d.func.attr for d in node.decorator_list
             if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
             and isinstance(d.func.value, ast.Name) and d.func.value.id == "router")
        + "(\n" + (ast.get_source_segment(source, node) or "")
        for node in handlers
    ]
    assert len(blocks) >= 20, "the router shrank — is this test still reading it?"

    seen_get = seen_write = 0
    for block in blocks:
        verb = block.split("(", 1)[0].strip()
        handler = re.search(r"\ndef (\w+)", block)
        name = handler.group(1) if handler else "<unknown>"
        if verb == "get":
            assert "Depends(_DESK_READ)" in block, f"GET {name} is not on _DESK_READ"
            assert "_DESK_WRITE" not in block, f"GET {name} over-requires payer.write"
            seen_get += 1
        else:
            assert "Depends(_DESK_WRITE)" in block, (
                f"{verb.upper()} {name} is not on _DESK_WRITE — a read-only desk "
                f"account could make this write"
            )
            seen_write += 1
    assert seen_get and seen_write

    # And the two dependencies ask for exactly what they claim to. Read from the
    # dependency's own closure, so this cannot pass by reading a comment.
    import permissions as perms

    def _required(dep) -> set:
        cells = dict(zip(dep.__code__.co_freevars,
                         (c.cell_contents for c in dep.__closure__)))
        return set(cells["permissions"])

    assert _required(pr._DESK_READ) == {perms.PAYER_READ}
    # Writes demand READ as well: every write handler answers with the claim it
    # touched, so a write-only grant would be a read grant with an extra step.
    assert _required(pr._DESK_WRITE) == {perms.PAYER_READ, perms.PAYER_WRITE}
    # Neither one is the governance permission any more.
    assert perms.ADMIN_OVERVIEW not in _required(pr._DESK_READ) | _required(pr._DESK_WRITE)


def test_module_flag_off_serves_404_not_403(monkeypatch, tpa_payer):
    """A site that turned the desk off does not HAVE an insurance desk. 403
    would say 'you are not allowed', which is a different, wrong statement."""
    headers = _login(_ensure_admin())
    import config as cfg
    monkeypatch.setattr(cfg, "PAYER_ENABLED", False)
    resp = client.get("/api/payer/claims", headers=headers)
    assert resp.status_code == 404
    assert "not enabled" in resp.json()["detail"]


def test_illegal_transition_over_http_is_409_with_the_reason(tpa_payer):
    headers = _login(_ensure_admin())
    claim = _claim_with_policy(claimed_paise=rupees("25000"))
    cid = claim["id"]
    client.post(f"/api/payer/claims/{cid}/submission", json={"external_ref": "H-1"},
                headers=headers)
    client.post(
        f"/api/payer/claims/{cid}/decision",
        json={"decision": "denied", "denial_code": "EXCLUSION", "denial_reason": "Excluded"},
        headers=headers,
    )
    resp = client.post(
        f"/api/payer/claims/{cid}/settlement", json={"settled_paise": rupees("25000")},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert "denied" in resp.json()["detail"] and "settled" in resp.json()["detail"]


def test_full_desk_flow_over_http(tpa_payer):
    """One pass through the API the panel actually calls."""
    headers = _login(_ensure_admin())
    patient_id = _ensure_patient("HTTP Flow Patient")

    resp = client.post(
        "/api/payer/policies",
        json={
            "patient_id": patient_id,
            "payer_code": "TESTTPA",
            "policy_number": "HTTP-POL-1",
            "copay_percent_bp": 1000,
            "sum_insured_paise": rupees("300000"),
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["policy"]["sum_insured_display"].startswith("₹")

    resp = client.post(
        "/api/payer/claims",
        json={"patient_id": patient_id, "claim_type": "cashless",
              "claimed_paise": rupees("100000")},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    cid = resp.json()["claim"]["id"]

    assert client.post(
        f"/api/payer/claims/{cid}/preauth",
        json={"requested_paise": rupees("90000"), "reference": "PA-1"}, headers=headers,
    ).status_code == 200
    assert client.post(
        f"/api/payer/claims/{cid}/preauth/decision",
        json={"decision": "approved", "approved_paise": rupees("90000")}, headers=headers,
    ).status_code == 200
    assert client.post(
        f"/api/payer/claims/{cid}/submission", json={"external_ref": "TPA-9"}, headers=headers,
    ).status_code == 200
    assert client.post(
        f"/api/payer/claims/{cid}/decision",
        json={"decision": "partially_approved", "approved_paise": rupees("80000"),
              "tds_paise": rupees("800")},
        headers=headers,
    ).status_code == 200

    resp = client.post(
        f"/api/payer/claims/{cid}/settlement",
        json={"settled_paise": rupees("65000")}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    settlement = resp.json()["claim"]["settlement"]
    # claimed 100000, approved 80000 → disallowed 20000; co-pay 10% of 80000 =
    # 8000; TDS 800 → expected 71200; received 65000 → short 6200.
    assert settlement["disallowed_paise"] == rupees("20000")
    assert settlement["copay_paise"] == rupees("8000")
    assert settlement["expected_receipt_paise"] == rupees("71200")
    assert settlement["short_settled_paise"] == rupees("6200")
    assert settlement["is_short_settled"] is True
    assert settlement["patient_liability_paise"] == rupees("28000")


def test_money_is_never_a_float_anywhere_in_a_payload(tpa_payer):
    """A float in a money field is the defect the whole money layer exists to
    prevent, and JSON hides it well — 8000.0 renders as '8000' in most viewers."""
    headers = _login(_ensure_admin())
    claim = _claim_with_policy(claimed_paise=rupees("12345.67"))

    def assert_no_float(node, path="root"):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, float) and (k.endswith("_paise") or k.endswith("_bp")):
                    raise AssertionError(f"{path}.{k} is a float: {v!r}")
                assert_no_float(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                assert_no_float(v, f"{path}[{i}]")

    for path in ("/api/payer/work-queue", f"/api/payer/claims/{claim['id']}",
                 "/api/payer/summary", "/api/payer/denials"):
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200, path
        assert_no_float(resp.json(), path)


def test_indian_money_formatting_reaches_the_payload(tpa_payer):
    """₹1,00,000 — lakh grouping, not ₹100,000."""
    claim = _claim_with_policy(claimed_paise=rupees("100000"))
    assert ps.get_claim(claim["id"])["claimed_display"] == "₹1,00,000.00"


def test_a_ward_scoped_user_cannot_open_another_wards_policy(tpa_payer):
    """The other half of the seeded-without-a-department decision above.

    A desk account is hospital-wide ON PURPOSE. An account that DOES carry a
    department must still be held to it — otherwise "hospital-wide" would be a
    property of the module rather than of the user row, and the financial
    surfaces would become the way around ward scoping. 404 not 403: a 403 would
    confirm the patient exists.
    """
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            ("PAYER-WARD-ADM", "Ward Desk", "admin", "Cardiology",
             hash_password("password")),
        )
        db.commit()
    finally:
        db.close()

    patient_id = _ensure_patient("Other Ward Patient")   # seeded into ward W1
    headers = _login("PAYER-WARD-ADM")

    resp = client.post(
        "/api/payer/policies",
        json={"patient_id": patient_id, "payer_code": "TESTTPA",
              "policy_number": "X-WARD-1"},
        headers=headers,
    )
    assert resp.status_code == 404, (
        "a Cardiology account wrote an insurance policy against a W1 patient")

    assert client.get(
        f"/api/payer/policies?patient_id={patient_id}", headers=headers
    ).status_code == 404


def test_tds_reads_not_yet_known_before_the_payer_decides(tpa_payer):
    """No TDS is recorded until the decision. The ledger showed "TDS ₹0.00" on an
    undecided claim, which reads as "the payer withheld nothing" (U313/F148)."""
    claim = _claim_with_policy(claimed_paise=rupees("100000"))
    s = claim["settlement"]
    assert s["tds_paise"] is None
    assert s["tds_display"] == "Not yet known"


def test_copay_out_of_range_is_refused_in_percent_words(tpa_payer):
    """The cover form showed the service's message as written, which named a
    field in basis points ("copay_percent_bp must be between 0 and 10000")."""
    with pytest.raises(ps.PayerError) as exc:
        ps.capture_policy(
            patient_id=_ensure_patient(), payer_code="TESTTPA", policy_number="POL-BAD",
            copay_percent_bp=15_000, created_by="PAYER-ADM",
        )
    assert str(exc.value) == "Co-pay must be a number from 0 to 100."
    assert "bp" not in str(exc.value)
