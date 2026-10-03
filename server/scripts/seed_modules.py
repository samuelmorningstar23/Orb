"""Orb DEV SEED — the eight hospital modules that otherwise ship empty.

WHY THIS EXISTS (2026-08-14, pilot dry run). An end-to-end verification drove all
eight sold workflows against a live server and found that eight of the eleven
hospital modules — payer, procurement, housekeeping, workforce, dietary,
biomedical, ABDM and the billing price list — held ZERO rows. That is worse than
"untested": with no vendor there is no purchase order, with no purchase order
there is no partial goods receipt, and the deeper flows those modules exist for
were UNREACHABLE rather than merely unexercised. Verification also found ZERO
housekeeping units registered in any ward, which silently broke the
discharge → turnaround join until one was created by hand.

WHAT THIS IS, EXACTLY
─────────────────────
  * SEED DATA, AND IT SAYS SO. Every master or module row this writes carries
    `SEED_MARKER` in a notes/remarks column, every code is prefixed `SEED-`, and
    every organisation name ends in "(seed)". Nothing here should ever be
    mistaken on a screen for something a hospital typed. The one exception is
    clinical free text: a diet order's notes and NPO reason carry no marker,
    because the order drawer pre-fills them into the clinician's next order and
    the marker then reads as something that clinician wrote.
  * IDEMPOTENT. Running it twice produces the same row counts as running it once.
    Where the underlying service is deliberately NOT idempotent — a claim, a
    diet order (which supersedes), a purchase order, a roster line — this module
    looks for its own marker first and skips. It never uses INSERT OR REPLACE to
    paper over a duplicate.
  * DEV ONLY, FAIL CLOSED. `seed_modules()` refuses unless the deployment profile
    is explicitly 'dev'. config.py defaults an unset ORB_DEPLOYMENT_PROFILE to
    'prod', so the refusal is also the default: you have to declare a dev box to
    get demo data onto it.
  * ADDITIVE. No schema changes. Everything goes through the module services
    (`upsert_vendor`, `register_unit`, `order_diet`, …) so the seed obeys every
    refusal those services encode — an NPO without an end time is rejected here
    exactly as it would be at a bedside.

PRICES ARE ILLUSTRATIVE AND ARE NOT A TARIFF
────────────────────────────────────────────
`billing_masters.ensure_default_masters()` seeds NO prices, on purpose: a price
list nobody at the hospital typed is a fabricated number that looks like a real
one. That rule stands for production. This is the DEV seed, and a demo where
every charge reads "Not configured" cannot show charge capture at all — so it
writes obviously-round, obviously-illustrative figures (₹500, ₹1,500, ₹8,000)
and labels each one as seed data in the item's own notes. Every rupee goes
through services/money.py as an integer of paise; there is not a float in this
file.

Usage (from server/):
    set -a; source ../.env; set +a
    ORB_DEPLOYMENT_PROFILE=dev .venv/bin/python scripts/seed_modules.py
    ORB_DEPLOYMENT_PROFILE=dev .venv/bin/python scripts/seed_modules.py --counts

It is also called as the last step of scripts/seed_clinical_data.py, so
`reset_db_and_seed.py` picks it up with no extra command.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db_session  # noqa: E402
from services.money import format_inr, rupees  # noqa: E402

#: Stamped into a notes/remarks column on every master or module row this file
#: writes. Screens show it, so it is plain words with no file path, and
#: `module_row_counts()` is not the only way to find these rows again: a human
#: can grep the database for one string.
SEED_MARKER = "Sample data"

#: Earlier spellings of the marker. The idempotency guards still recognise them,
#: so a box seeded before the rename is not seeded a second time.
_LEGACY_SEED_MARKERS: tuple[str, ...] = ("[SEED] demo data (scripts/seed_modules.py)",)
_ALL_SEED_MARKERS: tuple[str, ...] = (SEED_MARKER, *_LEGACY_SEED_MARKERS)


def _is_seed_text(text: str | None) -> bool:
    """True when a notes/remarks/description value was written by this seed."""
    return any((text or "").startswith(marker) for marker in _ALL_SEED_MARKERS)

#: Every generated code starts with this, so a real vendor/payer/asset can never
#: collide with a seeded one and an operator can see at a glance which is which.
CODE_PREFIX = "SEED-"

#: Tariff rows are dated facts. A FIXED historical date (rather than "today")
#: keeps the seed idempotent across days and puts a price in force before any
#: seeded admission, so bed days resolve instead of landing in `unpriced_days`.
RATE_EFFECTIVE_FROM = "2020-01-01"

#: Who the seeded rows are attributed to. DOC001/ADM001/NUR001 are seeded by
#: scripts/seed_clinical_data.py; if they are absent the services still accept
#: the string (these are attribution columns, not foreign keys).
SEED_ACTOR = "ADM001"


class SeedRefused(RuntimeError):
    """Raised when the seed is asked to run somewhere it must not."""


def _refuse_on_prod_profile() -> None:
    """The one hard gate. Read from the module at CALL time, never bound at import.

    `from config import DEPLOYMENT_PROFILE` would bind a copy before a test (or a
    caller) could change it, and a guard that cannot be re-read is a guard that
    silently tests the wrong thing.
    """
    import config

    if getattr(config, "DEPLOYMENT_PROFILE", "prod") != "dev":
        raise SeedRefused(
            "REFUSING: this is demo data and the deployment profile is not 'dev' "
            f"(it is {getattr(config, 'DEPLOYMENT_PROFILE', 'prod')!r}). Seeded "
            "vendors, payers, claims and diet orders on an installation holding "
            "real patients would be indistinguishable from the hospital's own "
            "records inside a day. Set ORB_DEPLOYMENT_PROFILE=dev if this really "
            "is a development box."
        )


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════

def _today() -> date:
    return date.today()


def _utc_iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _admitted_patients() -> list[dict]:
    """Live admitted patients, ward-ordered. The seed attaches to whoever is
    actually on a ward rather than inventing a patient of its own — a policy or a
    diet order against a patient nobody can open is a row that demos as a 404."""
    with db_session() as db:
        rows = db.execute(
            "SELECT id, name, ward, bed, age, sex FROM patients "
            "WHERE status = 'admitted' ORDER BY ward, bed"
        ).fetchall()
    return [dict(r) for r in rows]


def _current_admission_id(patient_id: str) -> str | None:
    with db_session() as db:
        row = db.execute(
            "SELECT id FROM admissions WHERE patient_id = ? AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
    return row["id"] if row else None


def _split_bed_label(label: str) -> tuple[str, int, int] | None:
    """'GM-101' → ('GM-', 101, 3); 'CCU-01' → ('CCU-', 1, 2).

    Returned so extra beds continue the ward's OWN numbering instead of inventing
    a second convention beside it. A ward whose labels Orb cannot parse gets the
    fallback in `_ward_bed_labels`, which is honest about being a fallback.
    """
    text = (label or "").strip()
    i = len(text)
    while i > 0 and text[i - 1].isdigit():
        i -= 1
    if i == len(text) or i == 0:
        return None
    return text[:i], int(text[i:]), len(text) - i


# ═════════════════════════════════════════════════════════════════════════════
#  1 · Billing masters — the price list charge capture needs
# ═════════════════════════════════════════════════════════════════════════════

#: (code, name, category, department, rupees, uom)
#: Healthcare SERVICES supplied by a clinical establishment are GST-exempt in
#: India (Notification 12/2017-CT(R) entry 74), which is `upsert_service_item`'s
#: default — so no GST is attached to any of these, deliberately.
_SERVICE_ITEMS: tuple[tuple[str, str, str, str | None, str, str], ...] = (
    ("SEED-CONS-FU",   "Consultation: follow-up (seed)",        "consultation",  None,               "500",  "each"),
    ("SEED-BED-DAY",   "Bed charge per day (seed)",              "bed",           None,               "1500", "day"),
    ("SEED-NUR-INJ",   "Nursing: injection administration (seed)", "nursing",    None,               "150",  "each"),
    ("SEED-PROC-DRESS", "Dressing: minor (seed)",               "procedure",     None,               "350",  "each"),
    ("SEED-INV-CBC",   "Complete blood count (seed)",            "investigation", "Laboratory",       "300",  "each"),
    ("SEED-INV-RFT",   "Renal function test (seed)",             "investigation", "Laboratory",       "600",  "each"),
    ("SEED-IMG-CXR",   "Chest X-ray, PA view (seed)",            "imaging",       "Radiology",        "450",  "each"),
    ("SEED-OT-MINOR",  "Operation theatre: minor procedure (seed)", "ot",        "General Surgery",  "8000", "each"),
)

#: The price-list codes this seed owns. reset_db_and_seed keeps these (and the
#: front desk's own OPD code) and deletes any other service item, so a price
#: typed by an ad hoc test does not survive every reseed.
SEEDED_SERVICE_CODES: frozenset[str] = frozenset(row[0] for row in _SERVICE_ITEMS)

#: Bed-day rates that differ by where the bed is. `post_bed_day_charges` passes
#: the admission's WARD as `ward_class`, so these are keyed on real ward names —
#: an intensive-care bed is not a general-ward bed at the same money.
_BED_DAY_WARD_RATES: tuple[tuple[str, str], ...] = (
    ("CCU", "4500"),
    ("RCU", "4500"),
)


def _seed_billing_masters(log) -> dict:
    from services import billing_masters

    billing_masters.ensure_default_masters()
    plan = billing_masters.default_tariff_plan()
    plan_id = int(plan["id"]) if plan else None

    made = 0
    for code, name, category, department, amount, uom in _SERVICE_ITEMS:
        billing_masters.upsert_service_item(
            code=code,
            name=name,
            category=category,
            department=department,
            uom=uom,
            default_rate_paise=rupees(amount),
            notes=f"{SEED_MARKER}: illustrative price, NOT this hospital's tariff",
            created_by=SEED_ACTOR,
        )
        if plan_id is not None:
            billing_masters.set_tariff_rate(
                tariff_plan_id=plan_id,
                service_code=code,
                rate_paise=rupees(amount),
                effective_from=RATE_EFFECTIVE_FROM,
                created_by=SEED_ACTOR,
            )
        made += 1
        log(f"  + {code}: {format_inr(rupees(amount))} (illustrative)")

    if plan_id is not None:
        for ward, amount in _BED_DAY_WARD_RATES:
            billing_masters.set_tariff_rate(
                tariff_plan_id=plan_id,
                service_code="SEED-BED-DAY",
                rate_paise=rupees(amount),
                ward_class=ward,
                effective_from=RATE_EFFECTIVE_FROM,
                created_by=SEED_ACTOR,
            )
            log(f"  + SEED-BED-DAY @ {ward}: {format_inr(rupees(amount))} (illustrative)")

    return {"service_items": made}


# ═════════════════════════════════════════════════════════════════════════════
#  2 · Payer — one TPA, one state scheme, a policy, and a claim mid-lifecycle
# ═════════════════════════════════════════════════════════════════════════════

def _seed_payer(log, patients: list[dict]) -> dict:
    from services import payer_service
    from services.payer_service import ClaimError, PayerError

    payer_service.upsert_payer(
        code=f"{CODE_PREFIX}TPA1",
        name="Sanjeevani TPA Services (seed)",
        payer_type="tpa",
        state_code="29",
        contact_name="Claims desk (seed)",
        contact_phone="080-4000-0000",
        claim_submission_mode="portal",
        credit_days=45,
        preauth_required=True,
        notes=SEED_MARKER,
    )
    payer_service.upsert_payer(
        code=f"{CODE_PREFIX}SCHEME",
        name="State Health Assurance Scheme (seed)",
        payer_type="state_scheme",
        state_code="29",
        claim_submission_mode="portal",
        credit_days=60,
        preauth_required=True,
        notes=SEED_MARKER,
    )
    log("  + payers: SEED-TPA1, SEED-SCHEME")

    if not patients:
        log("  ~ no admitted patient, policy and claim skipped")
        return {"payers": 2, "policies": 0, "claims": 0}

    patient = patients[0]
    admission_id = _current_admission_id(patient["id"])

    # capture_policy always INSERTs (a new card is a new row, by design), so the
    # idempotency guard lives here rather than in the service.
    with db_session() as db:
        existing = db.execute(
            "SELECT p.id FROM patient_payer_policies p JOIN payers y ON y.id = p.payer_id "
            "WHERE p.patient_id = ? AND y.code = ?",
            (patient["id"], f"{CODE_PREFIX}TPA1"),
        ).fetchone()

    if existing:
        policy_id = int(existing["id"])
        policies_made = 0
        log(f"  ~ policy exists for {patient['name']} (#{policy_id})")
    else:
        policy = payer_service.capture_policy(
            patient_id=patient["id"],
            payer_code=f"{CODE_PREFIX}TPA1",
            admission_id=admission_id,
            policy_number="SEED/POL/0001",
            member_id="SEED-MEM-0001",
            subscriber_name=patient["name"],
            relationship_to_subscriber="self",
            valid_from=(_today() - timedelta(days=120)).isoformat(),
            valid_to=(_today() + timedelta(days=245)).isoformat(),
            sum_insured_paise=rupees("500000"),
            copay_percent_bp=1000,  # 10%
            is_primary=True,
            created_by=SEED_ACTOR,
        )
        policy_id = int(policy["id"])
        policies_made = 1
        log(f"  + policy #{policy_id} for {patient['name']} (₹5,00,000 cover, 10% co-pay)")

    # A claim MID-LIFECYCLE: pre-auth raised, pre-auth approved, claim submitted,
    # payer query outstanding. That is the state the work queue exists to show —
    # a draft claim and a settled claim both demo as "nothing to do".
    with db_session() as db:
        claim_row = db.execute(
            "SELECT id, status FROM claims WHERE patient_id = ? AND notes IN (%s)"
            % ",".join("?" for _ in _ALL_SEED_MARKERS),
            (patient["id"], *_ALL_SEED_MARKERS),
        ).fetchone()

    if claim_row:
        log(f"  ~ claim #{claim_row['id']} exists ({claim_row['status']})")
        return {"payers": 2, "policies": policies_made, "claims": 0}

    # THE CLAIM IS FOR A BILL THAT EXISTS. It used to claim a fixed ₹48,000
    # against a patient with no charge, no invoice and no receipt, so Billing
    # said ₹0 while the payer desk chased ₹48,000. The stay's charges and an
    # interim invoice are posted first, through the billing service, and the
    # claim is for the payer's share of that invoice.
    bill = _seed_interim_bill(
        log, patient, admission_id, _CLAIM_BILL_LINES, key=f"seed-tpa-{patient['id']}",
    )
    if bill is None:
        log("  ~ no interim bill, claim skipped: a claim with no bill behind it is the defect")
        return {"payers": 2, "policies": policies_made, "claims": 0}
    claimed = int(bill["payer_responsibility_paise"] or 0)

    try:
        claim = payer_service.create_claim(
            patient_id=patient["id"],
            claim_type="cashless",
            payer_code=f"{CODE_PREFIX}TPA1",
            policy_id=policy_id,
            admission_id=admission_id,
            invoice_id=int(bill["id"]),
            claimed_paise=claimed,
            notes=SEED_MARKER,
            created_by=SEED_ACTOR,
        )
        claim_id = int(claim["id"])
        now = datetime.now(timezone.utc)
        # The pre-auth was asked for the whole expected stay, so it is larger
        # than the interim bill, and the bill sits inside what was approved.
        payer_service.record_preauth_request(
            claim_id=claim_id,
            requested_paise=claimed + rupees("10000"),
            reference="SEED/PA/0001",
            actor_id=SEED_ACTOR,
            occurred_at=_utc_iso(now - timedelta(days=3)),
        )
        payer_service.record_preauth_decision(
            claim_id=claim_id,
            decision="partially_approved",
            approved_paise=claimed + rupees("5000"),
            reference="SEED/PA/0001",
            actor_id=SEED_ACTOR,
            occurred_at=_utc_iso(now - timedelta(days=2)),
        )
        payer_service.record_submission(
            claim_id=claim_id,
            claimed_paise=claimed,
            external_ref="SEED/CLM/0001",
            submitted_at=_utc_iso(now - timedelta(days=1)),
            actor_id=SEED_ACTOR,
        )
        payer_service.record_query(
            claim_id=claim_id,
            query_text="Interim bill received. Send the investigation reports before it is processed (seed).",
            actor_id=SEED_ACTOR,
            occurred_at=_utc_iso(now - timedelta(hours=6)),
        )
        log(f"  + claim #{claim_id} for {format_inr(claimed)}: pre-auth approved in part, "
            "submitted, payer query open")
        return {"payers": 2, "policies": policies_made, "claims": 1}
    except (ClaimError, PayerError) as exc:
        log(f"  x claim not seeded: {exc}")
        return {"payers": 2, "policies": policies_made, "claims": 0}


#: What a stay has run up so far, as (service code, quantity). Bed days are
#: charged at the patient's own ward rate, so a CCU bed costs a CCU bed.
_CLAIM_BILL_LINES: tuple[tuple[str, int], ...] = (
    ("SEED-BED-DAY", 3), ("SEED-CONS-FU", 4), ("SEED-INV-CBC", 3),
    ("SEED-INV-RFT", 2), ("SEED-IMG-CXR", 1), ("SEED-NUR-INJ", 12),
)
_SELF_PAY_BILL_LINES: tuple[tuple[str, int], ...] = (
    ("SEED-BED-DAY", 2), ("SEED-CONS-FU", 2), ("SEED-INV-CBC", 1), ("SEED-NUR-INJ", 4),
)
_SCHEME_BILL_LINES: tuple[tuple[str, int], ...] = (
    ("SEED-BED-DAY", 2), ("SEED-CONS-FU", 2), ("SEED-INV-RFT", 1), ("SEED-IMG-CXR", 1),
)


def _seed_interim_bill(log, patient: dict, admission_id: str | None,
                       lines: tuple[tuple[str, int], ...], *, key: str) -> dict | None:
    """Post a stay's charges and raise an issued interim invoice over them.

    Every write carries an idempotency key derived from `key`, so a second run
    replays the same charges and the same invoice instead of billing twice.
    Returns the invoice, or None when there is no admission to bill.
    """
    from services import billing_service
    from services.billing_service import BillingError
    from services.money import QUANTITY_SCALE

    if not admission_id:
        log(f"  ~ {patient['name']} has no active admission, no bill")
        return None
    try:
        for code, quantity in lines:
            billing_service.add_charge(
                patient_id=patient["id"],
                service_code=code,
                quantity_milli=quantity * QUANTITY_SCALE,
                admission_id=admission_id,
                ward_class=patient.get("ward") if code == "SEED-BED-DAY" else None,
                idempotency_key=f"{key}-charge-{code}",
                notes=SEED_MARKER,
                charged_by=SEED_ACTOR,
            )
        invoice = billing_service.create_invoice(
            patient_id=patient["id"],
            admission_id=admission_id,
            invoice_type="interim",
            issue=True,
            notes=SEED_MARKER,
            created_by=SEED_ACTOR,
            idempotency_key=f"{key}-interim",
        )
    except BillingError as exc:
        log(f"  x interim bill for {patient['name']} not seeded: {exc}")
        return None
    log(f"  {'~' if invoice.get('replayed') else '+'} interim invoice #{invoice['id']} for "
        f"{patient['name']}: {format_inr(int(invoice['payable_paise'] or 0))}")
    return invoice


# ═════════════════════════════════════════════════════════════════════════════
#  2b · Money desks: a deposit, a discount awaiting a decision, a denial
# ═════════════════════════════════════════════════════════════════════════════

def _seed_money_desk(log, patients: list[dict]) -> dict:
    """The rest of what the cashier and the payer desk exist to show.

    The claim patient pays a deposit at admission; a self-pay patient's interim
    bill carries a discount request waiting for a manager; a state-scheme claim
    against its own interim bill has been denied, with the payer's reason, so
    the denial log is not empty. All through the services, all idempotent.
    """
    from services import payer_service
    from services import payments_service as pay
    from services.payer_service import ClaimError, PayerError
    from services.payments_service import PaymentError

    out = {"deposit_receipts": 0, "discount_requests": 0, "denied_claims": 0}
    if not patients:
        log("  ~ no admitted patient, money desk skipped")
        return out

    # 1. A deposit from the claim patient, held on the ledger.
    payer_patient = patients[0]
    try:
        receipt = pay.record_payment(
            patient_id=payer_patient["id"],
            amount_paise=rupees("5000"),
            method="upi",
            purpose="deposit",
            admission_id=_current_admission_id(payer_patient["id"]),
            reference_no="SEED-UTR-0001",
            received_by=SEED_ACTOR,
            notes=SEED_MARKER,
            idempotency_key=f"seed-deposit-{payer_patient['id']}",
        )
        out["deposit_receipts"] = 0 if receipt.get("replayed") else 1
        log(f"  {'~' if receipt.get('replayed') else '+'} deposit of "
            f"{format_inr(rupees('5000'))} from {payer_patient['name']}")
    except PaymentError as exc:
        log(f"  x deposit not seeded: {exc}")

    # 2. A self-pay interim bill with a discount request nobody has decided yet.
    if len(patients) > 1:
        self_pay = patients[1]
        admission_id = _current_admission_id(self_pay["id"])
        bill = _seed_interim_bill(
            log, self_pay, admission_id, _SELF_PAY_BILL_LINES, key=f"seed-selfpay-{self_pay['id']}",
        )
        if bill is not None and not pay.list_discounts(invoice_id=int(bill["id"])):
            try:
                pay.request_discount(
                    patient_id=self_pay["id"],
                    invoice_id=int(bill["id"]),
                    admission_id=admission_id,
                    reason=f"{SEED_MARKER}: family hardship, referred by the ward social worker",
                    reason_code="HARDSHIP",
                    requested_by=SEED_ACTOR,
                    requested_paise=rupees("1000"),
                )
                out["discount_requests"] = 1
                log(f"  + discount of {format_inr(rupees('1000'))} requested for {self_pay['name']}")
            except PaymentError as exc:
                log(f"  x discount request not seeded: {exc}")

    # 3. A state-scheme claim against its own bill, denied with a reason.
    if len(patients) > 2:
        scheme_patient = patients[2]
        out["denied_claims"] = _seed_denied_claim(log, scheme_patient, payer_service,
                                                  (ClaimError, PayerError))
    return out


def _seed_denied_claim(log, patient: dict, payer_service, errors: tuple) -> int:
    admission_id = _current_admission_id(patient["id"])
    scheme = f"{CODE_PREFIX}SCHEME"
    with db_session() as db:
        policy = db.execute(
            "SELECT p.id FROM patient_payer_policies p JOIN payers y ON y.id = p.payer_id "
            "WHERE p.patient_id = ? AND y.code = ?",
            (patient["id"], scheme),
        ).fetchone()
    try:
        if policy is None:
            policy = payer_service.capture_policy(
                patient_id=patient["id"],
                payer_code=scheme,
                admission_id=admission_id,
                policy_number="SEED/SCH/0001",
                member_id="SEED-SCH-MEM-0001",
                subscriber_name=patient["name"],
                relationship_to_subscriber="self",
                valid_from=(_today() - timedelta(days=20)).isoformat(),
                valid_to=(_today() + timedelta(days=345)).isoformat(),
                sum_insured_paise=rupees("500000"),
                is_primary=True,
                created_by=SEED_ACTOR,
            )
        bill = _seed_interim_bill(
            log, patient, admission_id, _SCHEME_BILL_LINES, key=f"seed-scheme-{patient['id']}",
        )
        if bill is None:
            return 0
        claim = payer_service.create_claim(
            patient_id=patient["id"],
            claim_type="cashless",
            payer_code=scheme,
            policy_id=int(policy["id"]),
            admission_id=admission_id,
            invoice_id=int(bill["id"]),
            claimed_paise=int(bill["payer_responsibility_paise"] or 0),
            notes=SEED_MARKER,
            created_by=SEED_ACTOR,
            idempotency_key=f"seed-claim-denied-{patient['id']}",
        )
        if claim.get("replayed"):
            log(f"  ~ denied claim #{claim['id']} exists ({claim['status']})")
            return 0
        now = datetime.now(timezone.utc)
        payer_service.record_preauth_request(
            claim_id=int(claim["id"]),
            requested_paise=int(bill["payer_responsibility_paise"] or 0),
            reference="SEED/PA/0002",
            actor_id=SEED_ACTOR,
            occurred_at=_utc_iso(now - timedelta(days=2)),
        )
        payer_service.record_preauth_decision(
            claim_id=int(claim["id"]),
            decision="denied",
            reference="SEED/PA/0002",
            denial_code="WAITING_PERIOD",
            denial_reason="Enrolled 20 days ago; the scheme's 30-day initial waiting period applies (seed).",
            actor_id=SEED_ACTOR,
            occurred_at=_utc_iso(now - timedelta(days=1)),
        )
        log(f"  + claim #{claim['id']} for {patient['name']}: pre-auth denied (waiting period)")
        return 1
    except errors as exc:
        log(f"  x denied claim not seeded: {exc}")
        return 0


# ═════════════════════════════════════════════════════════════════════════════
#  3 · Procurement — vendors, stores, items with reorder points, a PARTIAL GRN
# ═════════════════════════════════════════════════════════════════════════════

_VENDORS: tuple[tuple[str, str, str, str, int], ...] = (
    (f"{CODE_PREFIX}VEN1", "Deccan Surgicals & Disposables (seed)", "supplier", "29AAACS0000A1Z5", 30),
    (f"{CODE_PREFIX}VEN2", "Kaveri Diagnostics Supplies (seed)", "distributor", "29AAACK1111B1Z2", 45),
    (f"{CODE_PREFIX}VEN3", "Nandi Biomedical Services (seed)", "amc", "29AAACN2222C1Z9", 60),
)

#: (code, name, category, uom, reorder_level, reorder_qty, lead_days, rupees)
_ITEMS: tuple[tuple[str, str, str, str, int, int, int, str], ...] = (
    (f"{CODE_PREFIX}GLOVE-M", "Examination gloves, medium (box of 100) (seed)", "ppe", "box", 40, 120, 7, "320"),
    (f"{CODE_PREFIX}SYR-5ML", "Disposable syringe 5 ml (seed)", "consumable", "each", 500, 2000, 5, "6"),
    (f"{CODE_PREFIX}IV-CANN18", "IV cannula 18G (seed)", "consumable", "each", 200, 600, 7, "28"),
    (f"{CODE_PREFIX}GAUZE", "Sterile gauze pad 10x10 cm (seed)", "consumable", "each", 300, 1000, 5, "9"),
    (f"{CODE_PREFIX}MASK-N95", "N95 respirator (seed)", "ppe", "each", 150, 500, 10, "45"),
    (f"{CODE_PREFIX}REAG-CBC", "Haematology analyser reagent pack (seed)", "reagent", "pack", 6, 20, 14, "4200"),
    (f"{CODE_PREFIX}LINEN-BS", "Bedsheet, cotton (seed)", "linen", "each", 60, 200, 21, "260"),
    (f"{CODE_PREFIX}HK-PHENYL", "Floor disinfectant concentrate 5 L (seed)", "housekeeping", "can", 20, 60, 7, "540"),
)


def _seed_procurement(log) -> dict:
    from services import procurement_service as proc
    from services.procurement_service import ProcurementError

    for code, name, vtype, gstin, terms in _VENDORS:
        proc.upsert_vendor(
            code=code, name=name, vendor_type=vtype, gstin=gstin,
            state_code="29", payment_terms_days=terms,
            contact_name="Sales desk (seed)", contact_phone="080-4000-0001",
            notes=SEED_MARKER, created_by=SEED_ACTOR,
        )
    log(f"  + vendors: {len(_VENDORS)}")

    proc.upsert_store(code=f"{CODE_PREFIX}STORE-GEN", name="General sub-store (seed)",
                      store_type="sub_store")
    proc.upsert_store(code=f"{CODE_PREFIX}STORE-HK", name="Housekeeping store (seed)",
                      store_type="housekeeping")
    store = proc.get_store(f"{CODE_PREFIX}STORE-GEN")
    store_id = int(store["id"]) if store else None

    for code, name, category, uom, level, qty, lead, _rate in _ITEMS:
        proc.upsert_inventory_item(
            code=code, name=name, category=category, uom=uom,
            reorder_level_qty=level, reorder_qty=qty, lead_time_days=lead,
            gst_treatment="taxable", gst_rate_bp=1200,
            default_store_id=store_id,
            notes=f"{SEED_MARKER}: reorder point is illustrative",
        )
    log(f"  + inventory items with reorder points: {len(_ITEMS)}")

    # One purchase order, ISSUED, with a PARTIAL goods receipt against it — the
    # flow verification called unreachable. Guarded on its own remark so a second
    # run neither raises a second PO nor re-receives the same goods.
    existing = [p for p in proc.list_purchase_orders(limit=200)
                if _is_seed_text(p.get("remarks"))]
    if existing:
        log(f"  ~ purchase order #{existing[0]['id']} exists ({existing[0]['status']})")
        return {"vendors": len(_VENDORS), "items": len(_ITEMS), "purchase_orders": 0}

    try:
        po = proc.create_purchase_order(
            vendor_code=f"{CODE_PREFIX}VEN1",
            store_code=f"{CODE_PREFIX}STORE-GEN",
            lines=[
                {"item_code": f"{CODE_PREFIX}GLOVE-M",
                 "quantity_milli": proc.units_to_milli(100),
                 "unit_rate_paise": rupees("320")},
                {"item_code": f"{CODE_PREFIX}SYR-5ML",
                 "quantity_milli": proc.units_to_milli(2000),
                 "unit_rate_paise": rupees("6")},
                {"item_code": f"{CODE_PREFIX}IV-CANN18",
                 "quantity_milli": proc.units_to_milli(600),
                 "unit_rate_paise": rupees("28")},
            ],
            order_date=(_today() - timedelta(days=6)).isoformat(),
            expected_date=(_today() + timedelta(days=1)).isoformat(),
            place_of_supply="29",
            remarks=f"{SEED_MARKER}: illustrative rates, not a negotiated price",
            created_by=SEED_ACTOR,
        )
        po_id = int(po["id"])
        proc.submit_purchase_order(po_id, actor=SEED_ACTOR)
        proc.approve_purchase_order(po_id, actor="DOC001")
        proc.issue_purchase_order(po_id, actor=SEED_ACTOR)

        # PARTIAL: the gloves arrive in full, the syringes short, the cannulae not
        # at all. The PO therefore lands in 'partially_received' with a real
        # outstanding quantity behind it.
        full = proc.get_purchase_order(po_id) or {}
        by_item = {ln.get("item_code"): ln for ln in (full.get("lines") or [])}
        grn_lines = []
        glove = by_item.get(f"{CODE_PREFIX}GLOVE-M")
        syringe = by_item.get(f"{CODE_PREFIX}SYR-5ML")
        if glove:
            grn_lines.append({"po_line_no": glove.get("line_no"),
                              "item_code": f"{CODE_PREFIX}GLOVE-M",
                              "accepted_quantity_milli": proc.units_to_milli(100)})
        if syringe:
            grn_lines.append({"po_line_no": syringe.get("line_no"),
                              "item_code": f"{CODE_PREFIX}SYR-5ML",
                              "accepted_quantity_milli": proc.units_to_milli(1200)})
        if grn_lines:
            proc.create_goods_receipt(
                purchase_order_id=po_id,
                lines=grn_lines,
                store_code=f"{CODE_PREFIX}STORE-GEN",
                vendor_invoice_number="SEED/INV/0001",
                vendor_invoice_date=(_today() - timedelta(days=1)).isoformat(),
                received_by=SEED_ACTOR,
                remarks=f"{SEED_MARKER}: partial receipt; cannulae not despatched",
                idempotency_key=f"seed-grn-{po_id}-1",
            )
        after = proc.get_purchase_order(po_id) or {}
        log(f"  + purchase order #{po_id} → {after.get('status')} with a partial receipt")
        return {"vendors": len(_VENDORS), "items": len(_ITEMS), "purchase_orders": 1}
    except ProcurementError as exc:
        log(f"  x purchase order not seeded: {exc}")
        return {"vendors": len(_VENDORS), "items": len(_ITEMS), "purchase_orders": 0}


# ═════════════════════════════════════════════════════════════════════════════
#  4 · Housekeeping — a bed register for EVERY real ward
# ═════════════════════════════════════════════════════════════════════════════

def _ward_bed_labels(ward: str, occupied: list[str], target: int) -> list[str]:
    """The ward's bed list: the beds patients are actually in, extended to the
    site's bed count using the ward's own numbering.

    `target` is forecast_service.DEFAULT_WARD_BEDS — the SAME site setting the
    ward board and the capacity forecast divide by. Registering some other number
    here would put housekeeping's coverage report and the capacity screen into
    disagreement about how many beds the ward has, which is precisely the class
    of bug this dry run is fixing on the ward board.
    """
    labels = list(dict.fromkeys(occupied))
    parsed = [p for p in (_split_bed_label(b) for b in labels) if p]
    if parsed:
        prefix, _, pad = parsed[0]
        highest = max(n for _, n, _ in parsed)
    else:
        prefix, highest, pad = f"{ward}-", 0, 2
    next_n = highest + 1
    while len(labels) < target:
        labels.append(f"{prefix}{next_n:0{pad}d}")
        next_n += 1
    return labels


def _seed_housekeeping(log, patients: list[dict]) -> dict:
    from services import housekeeping_service as hk
    from services.forecast_service import DEFAULT_WARD_BEDS

    by_ward: dict[str, list[str]] = {}
    for p in patients:
        ward = (p.get("ward") or "").strip()
        if not ward:
            continue
        by_ward.setdefault(ward, [])
        if p.get("bed"):
            by_ward[ward].append(str(p["bed"]))

    if not by_ward:
        log("  ~ no admitted patient names a ward, nothing to register")
        return {"housekeeping_units": 0, "housekeeping_tasks": 0}

    occupied_beds = {(p.get("ward"), str(p.get("bed"))) for p in patients if p.get("bed")}
    registered = 0
    for ward, beds in sorted(by_ward.items()):
        for label in _ward_bed_labels(ward, beds, DEFAULT_WARD_BEDS):
            is_occupied = (ward, label) in occupied_beds
            hk.register_unit(
                unit_code=label,
                ward=ward,
                bed_label=label,
                unit_type="bed",
                # A bed with a patient in it is 'occupied'; an empty one is
                # 'vacant_dirty', which is register_unit's own default and its
                # documented refusal to claim a clean nobody performed.
                initial_state="occupied" if is_occupied else "vacant_dirty",
                notes=SEED_MARKER,
                actor_user_id=SEED_ACTOR,
                actor_role="admin",
            )
            registered += 1
    log(f"  + housekeeping units registered: {registered} across {len(by_ward)} ward(s)")

    # One open cleaning request, so the queue and the turnaround clock have
    # something in them. Raised on a vacant_dirty unit only — never on a bed with
    # a patient in it, which would vacate that bed in the same transaction.
    tasks = 0
    open_tasks = [t for t in hk.list_tasks(status="open")
                  if _is_seed_text(t.get("description"))]
    if open_tasks:
        log(f"  ~ cleaning request already open ({open_tasks[0]['id']})")
    else:
        dirty = [u for u in hk.list_units() if u["state"] == "vacant_dirty"]
        if dirty:
            task = hk.raise_cleaning_request(
                unit_id=dirty[0]["id"],
                request_type="routine",
                priority="routine",
                clean_type="standard",
                description=f"{SEED_MARKER}: routine clean before the next admission",
                actor_user_id="NUR001",
                actor_role="nurse",
            )
            tasks = 1
            log(f"  + cleaning request open on {dirty[0]['unit_code']} ({task['id']})")

    return {"housekeeping_units": registered, "housekeeping_tasks": tasks}


# ═════════════════════════════════════════════════════════════════════════════
#  5 · Workforce — shift types, employees, a published roster
# ═════════════════════════════════════════════════════════════════════════════

#: The roster uses the shifts migration 24 installs. This script used to add its
#: own M/E/N set with different hours beside them, so the roster screen offered
#: two catalogues for one hospital.
_SHIFT_CODES: tuple[str, ...] = ("MORNING", "EVENING", "NIGHT")

#: The set earlier runs of this script installed, matched on code AND name so a
#: site's own "M" is never touched. Retired (deactivated, never deleted: a
#: roster row may still name one, and the roster can still place it).
_LEGACY_SEED_SHIFTS: dict[str, str] = {
    "M": "Morning 07:00–15:00",
    "E": "Evening 15:00–23:00",
    "N": "Night 23:00–07:00",
}

#: (employee_code, name, department, designation, employment_type, user_id)
_EMPLOYEES: tuple[tuple[str, str, str, str, str, str | None], ...] = (
    (f"{CODE_PREFIX}EMP01", "Priya Reddy", "General Medicine", "Staff Nurse", "permanent", "NUR001"),
    (f"{CODE_PREFIX}EMP02", "Fatima Sheikh", "General Medicine", "Staff Nurse", "permanent", None),
    (f"{CODE_PREFIX}EMP03", "Joseph Mathew", "General Medicine", "Staff Nurse", "contract", None),
    (f"{CODE_PREFIX}EMP04", "Sunita Yadav", "Cardiology", "Senior Staff Nurse", "permanent", None),
    (f"{CODE_PREFIX}EMP05", "Ravi Shankar", "Cardiology", "Staff Nurse", "permanent", None),
    (f"{CODE_PREFIX}EMP06", "Meenakshi Iyer", "General Medicine", "Ward Assistant", "outsourced", None),
    (f"{CODE_PREFIX}EMP07", "Abdul Rahman", "Housekeeping", "Housekeeping Attendant", "outsourced", None),
    (f"{CODE_PREFIX}EMP08", "Lata Kulkarni", "Dietetics", "Dietitian", "permanent", None),
)

#: Three days of cover. EMP01 works the night of day 0 (ends 07:00) and the
#: evening of day 1 (starts 14:00): seven hours' rest, under MIN_REST_MINUTES,
#: so the roster conflict report has a real advisory to show instead of an
#: empty screen.
_ROSTER_PLAN: tuple[tuple[int, str, str, str | None], ...] = (
    (0, "MORNING", f"{CODE_PREFIX}EMP02", "GM-1"),
    (0, "EVENING", f"{CODE_PREFIX}EMP03", "GM-1"),
    (0, "NIGHT", f"{CODE_PREFIX}EMP01", "GM-1"),
    (1, "MORNING", f"{CODE_PREFIX}EMP02", "GM-1"),
    (1, "EVENING", f"{CODE_PREFIX}EMP01", "GM-1"),
    (1, "NIGHT", f"{CODE_PREFIX}EMP03", "GM-1"),
    (0, "MORNING", f"{CODE_PREFIX}EMP04", "CCU"),
    (0, "EVENING", f"{CODE_PREFIX}EMP05", "CCU"),
    (1, "MORNING", f"{CODE_PREFIX}EMP05", "CCU"),
    (1, "EVENING", f"{CODE_PREFIX}EMP04", "CCU"),
    (2, "MORNING", f"{CODE_PREFIX}EMP02", "GM-1"),
    (2, "EVENING", f"{CODE_PREFIX}EMP03", "GM-1"),
    (2, "NIGHT", f"{CODE_PREFIX}EMP01", "GM-1"),
)


def _seed_workforce(log) -> dict:
    from services import workforce_service as wf
    from services.workforce_service import RosterConflict, WorkforceError

    with db_session() as db:
        for code, name in _LEGACY_SEED_SHIFTS.items():
            db.execute(
                "UPDATE duty_shift_types SET is_active = 0, updated_at = CURRENT_TIMESTAMP "
                "WHERE code = ? AND name = ? AND is_active = 1",
                (code, name),
            )
        present = {r["code"] for r in db.execute(
            "SELECT code FROM duty_shift_types WHERE is_active = 1").fetchall()}
    missing = [code for code in _SHIFT_CODES if code not in present]
    if missing:
        log(f"  x shift types missing (migration 24 not applied?): {', '.join(missing)}")
    for code, name, dept, designation, etype, user_id in _EMPLOYEES:
        wf.upsert_employee(
            employee_code=code, full_name=name, department=dept,
            designation=designation, employment_type=etype, user_id=user_id,
            date_of_joining=(_today() - timedelta(days=400)).isoformat(),
            notes=SEED_MARKER, created_by=SEED_ACTOR,
        )
    log(f"  + shift types in use: {len(_SHIFT_CODES) - len(missing)}; employees: {len(_EMPLOYEES)}")

    made = 0
    base = _today()
    for offset, shift, employee, ward in _ROSTER_PLAN:
        day = (base + timedelta(days=offset)).isoformat()
        with db_session() as db:
            already = db.execute(
                "SELECT id FROM duty_roster WHERE roster_date = ? AND shift_code = ? "
                "AND employee_code = ? AND status <> 'cancelled'",
                (day, shift, employee),
            ).fetchone()
        if already:
            continue
        try:
            wf.assign_roster(
                roster_date=day, shift_code=shift, employee_code=employee,
                ward=ward, status="published", notes=SEED_MARKER,
                created_by=SEED_ACTOR, allow_short_rest=True,
            )
            made += 1
        except (RosterConflict, WorkforceError) as exc:
            log(f"  x roster {day} {shift} {employee}: {exc}")
    log(f"  + roster lines: {made} new")
    return {"shift_types": len(_SHIFT_CODES) - len(missing), "employees": len(_EMPLOYEES),
            "roster_lines": made}


# ═════════════════════════════════════════════════════════════════════════════
#  6 · Dietary — a diet for every admitted patient, including one NPO
# ═════════════════════════════════════════════════════════════════════════════

#: Diagnosis keyword → the diet a dietitian would plausibly write. Keyword-driven
#: rather than positional so the seed stays coherent when the patient list moves.
_DIET_BY_KEYWORD: tuple[tuple[str, str], ...] = (
    ("ketoacidosis", "diabetic"),
    ("diabet", "diabetic"),
    ("renal", "renal"),
    ("aki", "renal"),
    ("ckd", "renal"),
    ("heart failure", "cardiac"),
    ("stemi", "cardiac"),
    ("angina", "cardiac"),
    ("cabg", "cardiac"),
    ("stroke", "soft"),
    ("copd", "normal"),
)


def _diet_for(diagnosis: str) -> str:
    text = (diagnosis or "").lower()
    for keyword, diet in _DIET_BY_KEYWORD:
        if keyword in text:
            return diet
    return "normal"


def _seed_dietary(log, patients: list[dict]) -> dict:
    from services import dietary_service as diet
    from services.dietary_service import DietOrderError, MealPlanError, KitchenIndentError

    if not patients:
        log("  ~ no admitted patient, no diet orders")
        return {"diet_orders": 0, "meal_plans": 0, "kitchen_indents": 0}

    with db_session() as db:
        diagnoses = {
            r["id"]: (r["diagnosis"] or "")
            for r in db.execute(
                "SELECT id, diagnosis FROM patients WHERE status = 'admitted'"
            ).fetchall()
        }

    # Exactly ONE patient is nil by mouth, and it is a surgical patient, with a
    # reason and an end time — order_diet refuses an NPO without both.
    npo_patient = next(
        (p for p in patients
         if "surg" in (diagnoses.get(p["id"], "")).lower()
         or (p.get("ward") or "").startswith("SW")),
        patients[-1],
    )

    # NO MARKER ON THE ORDER ITSELF. A diet order's notes and NPO reason are
    # clinical free text, and the order drawer pre-fills both when a clinician
    # reviews or changes the diet, so a marker here was copied into the
    # clinician's own next order. The meal plan and indent below keep it: no
    # screen pre-fills those into anything.
    made = 0
    npo_written = False
    for p in patients:
        if diet.get_active_order(p["id"]) is not None:
            continue
        try:
            if p["id"] == npo_patient["id"]:
                diet.order_diet(
                    patient_id=p["id"], diet_type="npo", ordered_by="DOC004",
                    npo_reason="Nil by mouth for theatre in the morning",
                    npo_until=_utc_iso(datetime.now(timezone.utc) + timedelta(hours=14)),
                )
                npo_written = True
            else:
                diet.order_diet(
                    patient_id=p["id"], diet_type=_diet_for(diagnoses.get(p["id"], "")),
                    ordered_by="DOC001", texture="regular",
                )
            made += 1
        except DietOrderError as exc:
            log(f"  x diet for {p['name']}: {exc}")
    log(f"  + diet orders: {made} new" + (" (one NPO)" if npo_written else ""))

    # A locked meal plan and the kitchen indent raised off it — the flow that had
    # no data to run against.
    plans = indents = 0
    ward = next((p.get("ward") for p in patients if p.get("ward")), None)
    if ward:
        today_iso = _today().isoformat()
        existing = [pl for pl in diet.list_meal_plans(ward=ward, service_date=today_iso)
                    if pl.get("meal_service") == "lunch"]
        if existing:
            log(f"  ~ meal plan for {ward} lunch already exists (#{existing[0]['id']})")
        else:
            try:
                plan = diet.build_meal_plan(ward=ward, meal_service="lunch",
                                            generated_by="DOC001",
                                            service_date=today_iso, notes=SEED_MARKER)
                diet.lock_meal_plan(int(plan["id"]), locked_by="DOC001")
                diet.raise_kitchen_indent(meal_plan_id=int(plan["id"]),
                                          raised_by="DOC001", notes=SEED_MARKER)
                plans, indents = 1, 1
                log(f"  + meal plan #{plan['id']} locked for {ward} lunch, indent raised")
            except (MealPlanError, KitchenIndentError) as exc:
                log(f"  x meal plan/indent: {exc}")

    return {"diet_orders": made, "meal_plans": plans, "kitchen_indents": indents}


# ═════════════════════════════════════════════════════════════════════════════
#  7 · Biomedical — an equipment register with ONE calibration overdue
# ═════════════════════════════════════════════════════════════════════════════

#: (tag, name, category, ward, criticality, produces_readings, cost_rupees)
_ASSETS: tuple[tuple[str, str, str, str, str, bool, str], ...] = (
    (f"{CODE_PREFIX}VENT-01", "ICU ventilator (seed)", "ventilator", "CCU", "life_support", True, "1200000"),
    (f"{CODE_PREFIX}MON-01", "Bedside patient monitor (seed)", "patient_monitor", "CCU", "critical", True, "185000"),
    (f"{CODE_PREFIX}MON-02", "Bedside patient monitor (seed)", "patient_monitor", "GM-1", "critical", True, "185000"),
    (f"{CODE_PREFIX}PUMP-01", "Volumetric infusion pump (seed)", "infusion_pump", "CCU", "critical", False, "48000"),
    (f"{CODE_PREFIX}PUMP-02", "Syringe pump (seed)", "syringe_pump", "RCU", "critical", False, "36000"),
    (f"{CODE_PREFIX}DEFIB-01", "Biphasic defibrillator (seed)", "defibrillator", "CCU", "life_support", False, "420000"),
    (f"{CODE_PREFIX}ECG-01", "12-lead ECG machine (seed)", "ecg", "GM-1", "routine", True, "95000"),
    (f"{CODE_PREFIX}O2C-01", "Oxygen concentrator (seed)", "oxygen_concentrator", "RCU", "critical", False, "62000"),
)


def _seed_biomedical(log) -> dict:
    from services import biomedical_service as bio
    from services.biomedical_service import BiomedicalError
    from services import procurement_service as proc

    vendor = proc.get_vendor(f"{CODE_PREFIX}VEN3")
    vendor_id = int(vendor["id"]) if vendor else None

    for tag, name, category, ward, criticality, readings, cost in _ASSETS:
        bio.upsert_asset(
            asset_tag=tag, name=name, category=category, ward=ward,
            manufacturer="Nandi Medical Systems (seed)", model="SEED-100",
            serial_number=f"SN-{tag}", criticality=criticality,
            produces_clinical_readings=readings,
            purchase_date=(_today() - timedelta(days=900)).isoformat(),
            commissioned_date=(_today() - timedelta(days=870)).isoformat(),
            purchase_cost_paise=rupees(cost),
            warranty_expiry_date=(_today() + timedelta(days=95)).isoformat(),
            vendor_id=vendor_id,
            notes=f"{SEED_MARKER}: illustrative capital cost",
            actor=SEED_ACTOR,
        )
    log(f"  + biomedical assets: {len(_ASSETS)}")

    made = 0
    overdue_tag = f"{CODE_PREFIX}MON-01"
    for tag, *_rest in _ASSETS:
        try:
            # ONE asset is deliberately OVERDUE: its calibration was last done
            # 400 days ago on a 365-day interval, so the due register has a real
            # breach in it. An equipment module whose compliance screen is
            # entirely green teaches nobody to read it.
            last_done_days = 400 if tag == overdue_tag else 60
            bio.upsert_schedule(
                asset_tag=tag, schedule_type="calibration", interval_days=365,
                last_done_date=(_today() - timedelta(days=last_done_days)).isoformat(),
                grace_days=15, responsible="Biomedical engineering (seed)",
                vendor_id=vendor_id,
                certificate_reference=f"SEED/CAL/{tag}",
                actor=SEED_ACTOR,
            )
            bio.upsert_schedule(
                asset_tag=tag, schedule_type="preventive_maintenance",
                interval_days=180,
                last_done_date=(_today() - timedelta(days=90)).isoformat(),
                grace_days=7, responsible="Nandi Biomedical Services (seed)",
                vendor_id=vendor_id, actor=SEED_ACTOR,
            )
            made += 2
        except BiomedicalError as exc:
            log(f"  x schedule for {tag}: {exc}")
    log(f"  + schedules: {made} (calibration OVERDUE on {overdue_tag})")
    return {"biomedical_assets": len(_ASSETS), "biomedical_schedules": made}


# ═════════════════════════════════════════════════════════════════════════════
#  8 · ABDM — one ABHA identity, verified, with a granted consent
# ═════════════════════════════════════════════════════════════════════════════

#: Fourteen digits. Not an issued ABHA number and not derived from one — ABHA
#: numbers have no public check digit (see abdm_service.validate_abha_number), so
#: nothing offline can tell a real one from this. It is here to give the screen a
#: correctly-SHAPED value to render.
_SEED_ABHA_DIGITS = "91000000000017"
_SEED_ABHA_ADDRESS = "seed.demo.patient@abdm"
_SEED_CONSENT_REQUEST_ID = "SEED-CONSENT-0001"


def _seed_abdm(log, patients: list[dict]) -> dict:
    from services import abdm_service as abdm
    from services.abdm_service import AbdmError

    if not patients:
        log("  ~ no admitted patient, no ABHA identity")
        return {"abha_identities": 0, "abdm_consents": 0, "abdm_care_contexts": 0}

    patient = patients[0]

    with db_session() as db:
        row = db.execute(
            "SELECT id, verification_status FROM abha_identities "
            "WHERE abha_number = ? AND status = 'active'",
            (_SEED_ABHA_DIGITS,),
        ).fetchone()

    identities = 0
    if row:
        identity_id = int(row["id"])
        log(f"  ~ ABHA identity #{identity_id} exists ({row['verification_status']})")
    else:
        identity = abdm.record_abha(
            patient_id=patient["id"],
            abha_number=_SEED_ABHA_DIGITS,
            abha_address=_SEED_ABHA_ADDRESS,
            source="abha_card",
            recorded_by=SEED_ACTOR,
            notes=SEED_MARKER,
        )
        identity_id = int(identity["id"])
        identities = 1
        abdm.record_verification(
            identity_id=identity_id,
            method="abha_card_sighted",
            outcome="verified",
            performed_by="Front desk (seed)",
            performed_where="in_person",
            reference="SEED/VERIF/0001",
            notes=SEED_MARKER,
            recorded_by=SEED_ACTOR,
        )
        log(f"  + ABHA identity #{identity_id} recorded and verified for {patient['name']}")

    # A care context: this admission linked to that ABHA. Requires a verified
    # identity — the module's one compliance refusal, honoured rather than
    # bypassed.
    contexts = 0
    admission_id = _current_admission_id(patient["id"])
    reference = f"SEED-CC-{patient['id']}"
    with db_session() as db:
        have_cc = db.execute(
            "SELECT id FROM abdm_care_contexts WHERE care_context_reference = ?",
            (reference,),
        ).fetchone()
    if have_cc:
        log(f"  ~ care context {reference} exists")
    else:
        try:
            abdm.link_care_context(
                identity_id=identity_id,
                care_context_reference=reference,
                hi_type="DischargeSummary",
                display=f"Admission {admission_id or '—'} (seed)",
                encounter_type="admission",
                encounter_ref=admission_id,
                encounter_date=_today().isoformat(),
                linked_by="Front desk (seed)",
                link_method="abdm_portal",
                link_reference="SEED/LINK/0001",
                notes=SEED_MARKER,
                recorded_by=SEED_ACTOR,
            )
            contexts = 1
            log(f"  + care context {reference} linked")
        except AbdmError as exc:
            log(f"  x care context: {exc}")

    # A consent, requested and then GRANTED — so revocation (the flow
    # verification could not reach) has something to revoke.
    consents = 0
    with db_session() as db:
        have_consent = db.execute(
            "SELECT id, status FROM abdm_consent_artefacts WHERE request_id = ?",
            (_SEED_CONSENT_REQUEST_ID,),
        ).fetchone()
    if have_consent:
        log(f"  ~ consent #{have_consent['id']} exists ({have_consent['status']})")
    else:
        try:
            consent = abdm.record_consent_request(
                patient_id=patient["id"],
                requester_name="Sanjeevani Clinic, Bengaluru (seed)",
                purpose_code="CAREMGT",
                hi_types=["OPConsultation", "DiagnosticReport"],
                date_range_from=(_today() - timedelta(days=365)).isoformat(),
                date_range_to=_today().isoformat(),
                expires_at=_utc_iso(datetime.now(timezone.utc) + timedelta(days=30)),
                request_id=_SEED_CONSENT_REQUEST_ID,
                identity_id=identity_id,
                recorded_by=SEED_ACTOR,
                notes=SEED_MARKER,
            )
            abdm.record_consent_grant(
                consent_id=int(consent["id"]),
                artefact_id="SEED-ARTEFACT-0001",
                actor_id=SEED_ACTOR,
            )
            consents = 1
            log(f"  + consent #{consent['id']} requested and granted")
        except AbdmError as exc:
            log(f"  x consent: {exc}")

    return {"abha_identities": identities, "abdm_consents": consents,
            "abdm_care_contexts": contexts}


# ═════════════════════════════════════════════════════════════════════════════
#  Row counts — what "this module is no longer empty" actually means
# ═════════════════════════════════════════════════════════════════════════════

#: module → the tables whose emptiness made that module a shell. Used by the
#: script's --counts output and asserted by tests/test_seed_modules.py.
MODULE_TABLES: dict[str, tuple[str, ...]] = {
    "billing": ("service_items", "tariff_rates"),
    "payer": ("payers", "patient_payer_policies", "claims"),
    "money": ("encounter_charges", "invoices", "payments", "discount_approvals"),
    "procurement": ("vendors", "inventory_items", "purchase_orders", "goods_receipts"),
    "housekeeping": ("housekeeping_units", "housekeeping_tasks"),
    "workforce": ("duty_shift_types", "staff_employment", "duty_roster"),
    "dietary": ("diet_orders", "meal_plans", "kitchen_indents"),
    "biomedical": ("biomedical_assets", "biomedical_schedules"),
    "abdm": ("abha_identities", "abdm_consent_artefacts", "abdm_care_contexts"),
}


def module_row_counts() -> dict[str, dict[str, int]]:
    """Rows per table, grouped by module. A missing table counts as 0 rather than
    raising: this is a report, and a report that dies on an unmigrated table
    tells you less than one that says zero."""
    out: dict[str, dict[str, int]] = {}
    with db_session() as db:
        present = {
            r["name"] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for module, tables in MODULE_TABLES.items():
            counts: dict[str, int] = {}
            for table in tables:
                if table not in present:
                    counts[table] = 0
                    continue
                counts[table] = int(
                    db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                )
            out[module] = counts
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════════════════════

def seed_modules(*, verbose: bool = True) -> dict:
    """Seed every empty module. Idempotent. Refuses on a non-dev profile."""
    _refuse_on_prod_profile()

    def log(message: str) -> None:
        if verbose:
            print(message)

    patients = _admitted_patients()
    stats: dict = {}

    log("\n═══ Billing masters (illustrative prices) ═══")
    stats.update(_seed_billing_masters(log))
    log("\n═══ Payer · TPA, policy, claim mid-lifecycle ═══")
    stats.update(_seed_payer(log, patients))
    log("\n═══ Money desks · deposit, discount request, denied claim ═══")
    stats.update(_seed_money_desk(log, patients))
    log("\n═══ Procurement · vendors, items, PO with partial receipt ═══")
    stats.update(_seed_procurement(log))
    log("\n═══ Housekeeping · bed register for every ward ═══")
    stats.update(_seed_housekeeping(log, patients))
    log("\n═══ Workforce · shifts, staff, published roster ═══")
    stats.update(_seed_workforce(log))
    log("\n═══ Dietary · diet orders incl. one NPO, meal plan, indent ═══")
    stats.update(_seed_dietary(log, patients))
    log("\n═══ Biomedical · asset register, one calibration overdue ═══")
    stats.update(_seed_biomedical(log))
    log("\n═══ ABDM · ABHA identity, care context, granted consent ═══")
    stats.update(_seed_abdm(log, patients))

    return stats


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    counts_only = "--counts" in argv

    if not counts_only:
        try:
            seed_modules()
        except SeedRefused as exc:
            print(exc)
            return 2

    print(f"\n{'=' * 56}")
    for module, counts in module_row_counts().items():
        rendered = ", ".join(f"{t}={n}" for t, n in counts.items())
        flag = " " if all(n > 0 for n in counts.values()) else "!"
        print(f" {flag} {module:<14} {rendered}")
    print(f"{'=' * 56}")
    print(f" seeded rows are tagged {SEED_MARKER!r} (diet orders excepted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
