"""Orb — the INSURANCE / TPA DESK: payers, patient policies, pre-authorisation,
claims and the aging work queue.

In an Indian hospital this desk is where the money actually lives. A ward earns
the revenue; the TPA desk collects it, and the gap between those two facts is
weeks of paperwork, a pre-authorisation that has not come back, a claim that was
short-settled by ₹4,300 with no reason given, and a denial nobody appealed
because nobody noticed it. This module is the record of that work.

╔══════════════════════════════════════════════════════════════════════════════╗
║ ORB DOES NOT TRANSMIT ANYTHING TO ANY PAYER. READ THIS BEFORE ANYTHING ELSE. ║
╚══════════════════════════════════════════════════════════════════════════════╝

Orb has NO connection to any TPA, insurer, government scheme or clearing house.
There is no API, no portal integration, no e-mail gateway, no file drop. Every
"submitted", "pre-auth raised" and "settled" in this module is a RECORD OF
SOMETHING A HUMAN DID SOMEWHERE ELSE — on the TPA's own portal, over e-mail, or
on paper carried to a desk.

That is not a limitation to be papered over with hopeful verbs. It is the single
most dangerous thing this module could get wrong, because the failure is silent
and expensive: a desk that believes Orb sent the claim does not send the claim,
and by the time anyone checks, the payer's submission window has closed and the
hospital eats the bill. So:

  * ``NO_TRANSMISSION_NOTICE`` is returned by every write in this module and is
    rendered, undismissable, on the panel that drives it.
  * Function names say ``record_*``, never ``send_*`` or ``submit_*``.
  * ``claims.submitted_at`` means "the desk told Orb it submitted this
    elsewhere, at this time" — never "Orb transmitted it".
  * tests/test_payer.py asserts the wording is present in the service, in the
    route payloads and in the panel source. Deleting the sentence turns the
    suite red on purpose.

WHAT THIS MODULE MUST NEVER DO
──────────────────────────────
Nothing here may touch, gate or delay a clinical action. A pending pre-auth does
not stop a procedure; an exhausted policy does not stop a discharge; a denied
claim does not stop anything at all. This module reads clinical tables (patient
name, admission id) and writes only to its own: ``payers``,
``patient_payer_policies``, ``claims``, ``claim_events``. Discharge financial
clearance is advisory by default and is not implemented here.

WHERE THE PATIENT'S POLICY COMES FROM
─────────────────────────────────────
The clinical admission form captures NO payer information at all — deliberately,
because an admission is a clinical act and must not wait on an insurance card.
So the capture path lives HERE (``capture_policy``) and the desk enters cover
after the fact, which is also how it works in reality: the patient arrives, the
card is photographed at the counter, the policy is keyed in an hour later.

MONEY
─────
Integer paise, always, through services/money.py. There is exactly ONE rounding
operation in this entire module — the co-pay percentage in
``compute_settlement`` — and it is documented at the site. Everything else is
integer addition and subtraction, so the reconciliation identity

    claimed = disallowed + co-pay + TDS + received + short-settled

holds EXACTLY, to the paise, with no tolerance. tests/test_payer.py asserts it.

CONVENTIONS (same as the rest of server/services)
  * Every function is PURE SYNC and opens its own ``db_session()``. Async callers
    go through ``database.run_db``; blocking DB calls inside ``async def`` are
    banned and tests/test_event_loop_discipline.py enforces it.
  * No foreign keys on these tables (migration 20): a dangling id yields
    "unknown", never a failed clinical write.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from database import db_session
from db_backend import DBIntegrityError
from services.billing_masters import next_document_number, resolve_patient_payer
from services.money import (
    apply_basis_points,
    assert_non_negative_paise,
    format_inr,
    format_inr_or_unset,
)

logger = logging.getLogger("orb.payer")

__all__ = [
    "NO_TRANSMISSION_NOTICE",
    "NO_TRANSMISSION_SHORT",
    "PayerError",
    "ClaimError",
    "IllegalTransition",
    "PAYER_TYPES",
    "CLAIM_TYPES",
    "CLAIM_STATUSES",
    "OPEN_STATUSES",
    "TERMINAL_STATUSES",
    "LEGAL_TRANSITIONS",
    "AGING_BUCKETS",
    "DENIAL_REASON_CODES",
    "ClaimSettlement",
    "compute_settlement",
    "list_payers",
    "upsert_payer",
    "capture_policy",
    "list_policies",
    "verify_policy",
    "set_policy_status",
    "create_claim",
    "set_claimed_amount",
    "get_claim",
    "list_claims",
    "record_preauth_request",
    "record_preauth_decision",
    "record_submission",
    "record_query",
    "record_decision",
    "record_settlement",
    "record_appeal",
    "close_claim",
    "cancel_claim",
    "add_note",
    "work_queue",
    "denial_log",
    "desk_summary",
]


# ══════════════════════════════════════════════════════════════════════════════
#  The honesty notice
# ══════════════════════════════════════════════════════════════════════════════

# The exact words. Referenced by the routes, rendered by the panel, asserted by
# the tests. If a product decision ever changes them, all three move together —
# which is the point of there being one constant rather than three strings.
NO_TRANSMISSION_NOTICE = (
    "Orb does not connect to any TPA, insurer or government scheme. "
    "Nothing on this screen is transmitted to a payer. "
    "Recording a pre-authorisation, a submission or a settlement here records "
    "what the desk did elsewhere, on the payer's portal, by e-mail or on paper."
)

# The short form, for a button's helper line or a column header. Same claim,
# fewer words; it must remain unambiguous on its own because it sometimes
# appears without the long form beside it.
NO_TRANSMISSION_SHORT = "Recorded in Orb only, not sent to the payer."


# ══════════════════════════════════════════════════════════════════════════════
#  Errors
# ══════════════════════════════════════════════════════════════════════════════

class PayerError(ValueError):
    """Bad payer/policy input — blank code, unknown payer, impossible dates."""


class ClaimError(ValueError):
    """A claim operation that cannot be reconciled — amounts that do not add up,
    a decision on a claim that was never submitted, an unknown claim id."""


class IllegalTransition(ClaimError):
    """A claim lifecycle move that is not in ``LEGAL_TRANSITIONS``.

    Its own class because ONE of these matters more than the rest: a denied
    claim must never become settled without an appeal that someone recorded.
    Money arriving against a claim the payer refused means either the payment
    belongs to a different claim or the denial was reversed — and both of those
    are events a human has to look at, not a status a UI may quietly overwrite.
    """


# ══════════════════════════════════════════════════════════════════════════════
#  Vocabulary — mirrors migration 20's CHECK constraints exactly
# ══════════════════════════════════════════════════════════════════════════════

PAYER_TYPES: frozenset[str] = frozenset({
    "self_pay", "tpa", "insurer", "pmjay", "state_scheme", "cghs", "esic",
    "corporate", "charity",
})

CLAIM_TYPES: frozenset[str] = frozenset({"cashless", "reimbursement", "scheme"})

# Claim types where the PAYER pays the HOSPITAL. Under reimbursement the patient
# settles the hospital bill in full and recovers from the insurer themselves, so
# the hospital's receivable against the payer is zero however large the claim is
# — see compute_settlement. Getting this backwards would put money in a
# receivable report that is never coming.
HOSPITAL_RECEIVABLE_TYPES: frozenset[str] = frozenset({"cashless", "scheme"})

CLAIM_STATUSES: tuple[str, ...] = (
    "draft", "preauth_requested", "preauth_approved", "preauth_denied",
    "submitted", "queried", "approved", "partially_approved", "denied",
    "settled", "closed", "cancelled", "appealed",
)

# Statuses where the money is still moving and the desk still owes somebody an
# action. These are what the work queue shows and what ages.
OPEN_STATUSES: frozenset[str] = frozenset({
    "draft", "preauth_requested", "preauth_approved", "preauth_denied",
    "submitted", "queried", "approved", "partially_approved", "denied",
    "appealed",
})

TERMINAL_STATUSES: frozenset[str] = frozenset({"closed", "cancelled"})

# ── THE LIFECYCLE ────────────────────────────────────────────────────────────
#
#   draft ──► preauth_requested ──► preauth_approved ──► submitted ──► queried
#     │                    └──────► preauth_denied ──► appealed        │  ▲
#     └────────────────────────────────────────────► submitted ────────┘  │
#                                                        │                │
#                          approved / partially_approved / denied ────────┘
#                                     │            │            │
#                                  settled      settled      appealed
#                                     │            │            │
#                                   closed       closed       (re-decided)
#
# THE ONE TRANSITION THAT IS DELIBERATELY MISSING: denied ──► settled.
#
# A denied claim cannot become settled. Not by a status dropdown, not by
# recording a receipt against it, not by a bulk update. If money arrives against
# a denied claim, either the denial was overturned — which is an APPEAL, and an
# appeal is a thing a human did and can point at — or the receipt belongs to a
# different claim. Both need a person. Allowing the shortcut would let a desk
# close its denial list by marking things settled, and the denial list is the
# only place a hospital ever finds out that a payer is systematically refusing a
# particular procedure code.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"preauth_requested", "submitted", "cancelled"}),
    "preauth_requested": frozenset({"preauth_approved", "preauth_denied", "cancelled"}),
    # A pre-auth approval is permission to treat cashless; the claim itself is
    # still submitted afterwards, on discharge, with the final bill.
    "preauth_approved": frozenset({"submitted", "cancelled"}),
    # A denied pre-auth is appealed, or the case converts to cash and the claim
    # is cancelled. It is NOT submitted as if approved.
    "preauth_denied": frozenset({"appealed", "cancelled"}),
    "submitted": frozenset({
        "queried", "approved", "partially_approved", "denied", "cancelled",
    }),
    # Answering a query puts the claim back in the payer's hands: 'submitted'.
    "queried": frozenset({
        "submitted", "approved", "partially_approved", "denied", "cancelled",
    }),
    "approved": frozenset({"settled", "closed"}),
    # A partial approval may be banked as-is (settled) or contested (appealed).
    "partially_approved": frozenset({"settled", "appealed", "closed"}),
    # NO 'settled' HERE. See the note above.
    "denied": frozenset({"appealed", "closed"}),
    "appealed": frozenset({
        "submitted", "approved", "partially_approved", "denied", "closed",
    }),
    "settled": frozenset({"closed"}),
    "closed": frozenset(),
    "cancelled": frozenset(),
}

# Which claim_events row type records each arrival. Kept beside the transition
# table so a new status cannot be added without deciding how it is evidenced.
_EVENT_FOR_STATUS: dict[str, str] = {
    "preauth_requested": "preauth_requested",
    "preauth_approved": "preauth_decided",
    "preauth_denied": "preauth_decided",
    "submitted": "submitted",
    "queried": "query_raised",
    "approved": "approved",
    "partially_approved": "partially_approved",
    "denied": "denied",
    "settled": "settled",
    "appealed": "appealed",
    "closed": "closed",
    "cancelled": "cancelled",
}

# ── Aging buckets, in days ───────────────────────────────────────────────────
# Chosen against how Indian TPA desks actually talk about their pending list:
# a week is normal, a fortnight is a follow-up call, a month is an escalation,
# and past sixty days the claim is usually in trouble. The upper bound is
# exclusive; `None` means open-ended.
AGING_BUCKETS: tuple[tuple[str, int, Optional[int]], ...] = (
    ("0-7", 0, 8),
    ("8-14", 8, 15),
    ("15-30", 15, 31),
    ("31-60", 31, 61),
    ("60+", 61, None),
)

# Past this many days in an open status, the queue flags the claim as stuck. It
# is a display hint, not a rule that does anything.
STALE_AFTER_DAYS = 15

# ── Denial reason codes ──────────────────────────────────────────────────────
#
# THESE ARE ORB'S OWN LOCAL VOCABULARY. They are NOT IRDAI codes, NOT any TPA's
# official reason codes, and NOT a standard. No such national standard exists in
# India — every payer denies in its own words on its own letterhead — so the
# desk records the payer's free-text reason verbatim in `denial_reason` and
# optionally tags it with one of these for counting. A code the desk invents is
# accepted too; this list is a suggestion, not a constraint.
DENIAL_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("NON_DISCLOSURE", "Pre-existing condition not disclosed"),
    ("WAITING_PERIOD", "Within policy waiting period"),
    ("EXCLUSION", "Treatment excluded under the policy"),
    ("NOT_MEDICALLY_NECESSARY", "Payer disputes medical necessity"),
    ("PACKAGE_MISMATCH", "Billed outside the agreed package/tariff"),
    ("DOCUMENT_DEFICIENCY", "Documents missing or illegible"),
    ("LATE_INTIMATION", "Intimation/submission after the payer's window"),
    ("POLICY_LAPSED", "Policy not in force on the date of service"),
    ("SUM_INSURED_EXHAUSTED", "Sum insured already used up"),
    ("DUPLICATE", "Duplicate of a claim already processed"),
    ("OTHER", "Other: see the payer's own wording"),
)


# ══════════════════════════════════════════════════════════════════════════════
#  Settlement arithmetic — PURE, and the part that must never be wrong
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ClaimSettlement:
    """What was claimed, what the payer allowed, what actually arrived, and the
    exact size of every gap between them.

    Every field is integer paise or None. None means NOT YET KNOWN and must
    render as words, never as ₹0.00 — a claim awaiting a decision and a claim
    approved for nothing look identical as zeros and mean opposite things.
    """
    claim_type: str
    claimed_paise: int
    approved_paise: Optional[int]
    disallowed_paise: Optional[int]
    copay_percent_bp: int
    copay_paise: Optional[int]
    tds_paise: int
    expected_receipt_paise: Optional[int]
    settled_paise: Optional[int]
    short_settled_paise: Optional[int]
    patient_liability_paise: Optional[int]
    hospital_receivable_paise: Optional[int]
    is_short_settled: bool
    is_over_settled: bool
    is_decided: bool
    is_received: bool

    def as_dict(self) -> dict:
        """Amounts AND their display strings, so no screen has to decide what an
        unknown amount looks like."""
        return {
            "claim_type": self.claim_type,
            "claimed_paise": self.claimed_paise,
            "claimed_display": format_inr(self.claimed_paise),
            "approved_paise": self.approved_paise,
            "approved_display": format_inr_or_unset(
                self.approved_paise, unset="Awaiting decision"
            ),
            "disallowed_paise": self.disallowed_paise,
            "disallowed_display": format_inr_or_unset(
                self.disallowed_paise, unset="Not yet known"
            ),
            "copay_percent_bp": self.copay_percent_bp,
            "copay_paise": self.copay_paise,
            "copay_display": format_inr_or_unset(self.copay_paise, unset="Not yet known"),
            "tds_paise": self.tds_paise,
            "tds_display": format_inr(self.tds_paise),
            "expected_receipt_paise": self.expected_receipt_paise,
            "expected_receipt_display": format_inr_or_unset(
                self.expected_receipt_paise, unset="Not yet known"
            ),
            "settled_paise": self.settled_paise,
            "settled_display": format_inr_or_unset(
                self.settled_paise, unset="Nothing received"
            ),
            "short_settled_paise": self.short_settled_paise,
            "short_settled_display": format_inr_or_unset(
                self.short_settled_paise, unset="Not yet known"
            ),
            "patient_liability_paise": self.patient_liability_paise,
            "patient_liability_display": format_inr_or_unset(
                self.patient_liability_paise, unset="Not yet known"
            ),
            "hospital_receivable_paise": self.hospital_receivable_paise,
            "hospital_receivable_display": format_inr_or_unset(
                self.hospital_receivable_paise, unset="Not yet known"
            ),
            "is_short_settled": self.is_short_settled,
            "is_over_settled": self.is_over_settled,
            "is_decided": self.is_decided,
            "is_received": self.is_received,
        }


def compute_settlement(
    *,
    claimed_paise: int,
    approved_paise: Optional[int] = None,
    copay_percent_bp: int = 0,
    tds_paise: int = 0,
    settled_paise: Optional[int] = None,
    claim_type: str = "cashless",
) -> ClaimSettlement:
    """Reconcile one claim, to the paise.

    THE IDENTITY THIS FUNCTION EXISTS TO GUARANTEE — it holds EXACTLY, with no
    tolerance, for every input the function accepts::

        claimed = disallowed + co-pay + TDS + received + short-settled

    Read left to right it is the whole story of a rupee the hospital billed: the
    payer refused some of it (disallowed), the patient owes some of it (co-pay),
    the government took some of it (TDS), some of it arrived (received), and
    whatever is left is the money that was approved and never came
    (short-settled). If those five do not add back to what was claimed, a
    hospital is looking at a number that does not exist.

    THE ONLY ROUNDING IN THIS MODULE is the co-pay percentage, one line below,
    and it cannot break the identity: ``expected_receipt`` is DERIVED by
    subtracting the already-rounded co-pay, so whatever the rounding did, the
    parts still sum. That ordering is deliberate — computing both the co-pay and
    the payer's share independently from the percentage is how the two end up a
    paise apart.

    ARGUMENTS
      claimed_paise    what the hospital billed the payer. Required, ≥ 0.
      approved_paise   what the payer admitted. None until a decision is
                       recorded; then 0 ≤ approved ≤ claimed.
      copay_percent_bp the patient's share of the ADMISSIBLE (approved) amount,
                       in basis points, from the policy. 0 for schemes.
      tds_paise        tax deducted at source by the payer before paying. ≥ 0.
      settled_paise    what actually reached the hospital's bank. None until a
                       receipt is recorded.
      claim_type       'cashless' | 'scheme' → the payer pays the hospital.
                       'reimbursement'       → the PATIENT pays the hospital in
                                               full and recovers from the
                                               insurer, so the hospital's
                                               receivable against the payer is
                                               zero and the patient's liability
                                               is the entire claimed amount.

    RAISES ClaimError on figures that cannot be true: an approval larger than
    the claim, or a TDS larger than the payer's share. Both are typos, and both
    would otherwise be stored as a negative amount that quietly poisons a
    receivable report.
    """
    if claim_type not in CLAIM_TYPES:
        raise ClaimError(f"unknown claim type: {claim_type!r}")
    claimed = assert_non_negative_paise(claimed_paise, "claimed_paise")
    tds = assert_non_negative_paise(tds_paise, "tds_paise")
    # Worded in the percent the person typed, not the basis points the column
    # stores: these messages reach the screen through the routes.
    if not isinstance(copay_percent_bp, int) or isinstance(copay_percent_bp, bool):
        raise ClaimError("Co-pay must be a number from 0 to 100.")
    if copay_percent_bp < 0 or copay_percent_bp > 10_000:
        raise ClaimError("Co-pay must be a number from 0 to 100.")

    settled = (
        None if settled_paise is None
        else assert_non_negative_paise(settled_paise, "settled_paise")
    )

    # ── Reimbursement: the payer never pays the hospital ─────────────────────
    # The patient has already settled (or will settle) the hospital bill in
    # full; the claim here tracks the PATIENT's recovery so the desk can help
    # chase it. Counting it as a hospital receivable would inflate the
    # receivable report by money that is not coming to the hospital at all.
    if claim_type == "reimbursement":
        approved_r = (
            None if approved_paise is None
            else assert_non_negative_paise(approved_paise, "approved_paise")
        )
        if approved_r is not None and approved_r > claimed:
            raise ClaimError(
                f"approved ({format_inr(approved_r)}) exceeds claimed "
                f"({format_inr(claimed)}): check the figures"
            )
        return ClaimSettlement(
            claim_type=claim_type,
            claimed_paise=claimed,
            approved_paise=approved_r,
            disallowed_paise=None if approved_r is None else claimed - approved_r,
            copay_percent_bp=copay_percent_bp,
            copay_paise=None,
            tds_paise=tds,
            expected_receipt_paise=0,
            settled_paise=settled,
            short_settled_paise=None,
            patient_liability_paise=claimed,
            hospital_receivable_paise=0,
            is_short_settled=False,
            is_over_settled=False,
            is_decided=approved_r is not None,
            is_received=settled is not None and settled > 0,
        )

    # ── Cashless / scheme: the payer owes the hospital ───────────────────────
    if approved_paise is None:
        # Nothing decided yet. Every derived figure is genuinely unknown, and
        # says so rather than defaulting to zero.
        return ClaimSettlement(
            claim_type=claim_type,
            claimed_paise=claimed,
            approved_paise=None,
            disallowed_paise=None,
            copay_percent_bp=copay_percent_bp,
            copay_paise=None,
            tds_paise=tds,
            expected_receipt_paise=None,
            settled_paise=settled,
            short_settled_paise=None,
            patient_liability_paise=None,
            hospital_receivable_paise=None,
            is_short_settled=False,
            is_over_settled=False,
            is_decided=False,
            is_received=settled is not None and settled > 0,
        )

    approved = assert_non_negative_paise(approved_paise, "approved_paise")
    if approved > claimed:
        raise ClaimError(
            f"approved ({format_inr(approved)}) exceeds claimed "
            f"({format_inr(claimed)}): check the figures"
        )

    disallowed = claimed - approved

    # THE ONE ROUNDING. apply_basis_points is half-up away from zero, the single
    # rule in services/money.py. The patient's co-pay is a percentage of the
    # ADMISSIBLE amount (what the payer allowed), not of what was billed — a
    # co-pay charged on the disallowed portion too would bill the patient twice
    # for the same rupee.
    copay = apply_basis_points(approved, copay_percent_bp)

    # DERIVED BY SUBTRACTION, never recomputed from the percentage. This is what
    # keeps the identity exact regardless of which way the co-pay rounded.
    payer_share = approved - copay
    if tds > payer_share:
        raise ClaimError(
            f"TDS ({format_inr(tds)}) exceeds the payer's share "
            f"({format_inr(payer_share)}): check the figures"
        )
    expected_receipt = payer_share - tds

    short_settled = None if settled is None else expected_receipt - settled

    return ClaimSettlement(
        claim_type=claim_type,
        claimed_paise=claimed,
        approved_paise=approved,
        disallowed_paise=disallowed,
        copay_percent_bp=copay_percent_bp,
        copay_paise=copay,
        tds_paise=tds,
        expected_receipt_paise=expected_receipt,
        settled_paise=settled,
        short_settled_paise=short_settled,
        # What the patient owes the hospital: the payer's refusal plus the
        # contractual co-pay. TDS is NOT here — that is the payer's tax on its
        # own payment, and billing it to the patient would be theft.
        patient_liability_paise=disallowed + copay,
        hospital_receivable_paise=(
            expected_receipt if claim_type in HOSPITAL_RECEIVABLE_TYPES else 0
        ),
        is_short_settled=short_settled is not None and short_settled > 0,
        is_over_settled=short_settled is not None and short_settled < 0,
        is_decided=True,
        is_received=settled is not None and settled > 0,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Timestamps + aging
# ══════════════════════════════════════════════════════════════════════════════
#
# SQLite's CURRENT_TIMESTAMP writes naive UTC as 'YYYY-MM-DD HH:MM:SS', and that
# is what every DEFAULT in migration 20 produces. So every timestamp this module
# WRITES uses the same shape and the same clock; mixing local time into one
# column would make an age computation silently wrong by 5½ hours in India,
# which is exactly the size of error that hides inside a "0-7 days" bucket.

def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a stored timestamp into a naive-UTC datetime, or None.

    Tolerates the three shapes that reach these columns: SQLite's
    'YYYY-MM-DD HH:MM:SS', an ISO string with a 'T', and an ISO string carrying
    'Z' or an offset (normalised to UTC). Anything unparseable returns None and
    the caller treats the age as unknown — never as zero, which would put a
    six-month-old claim at the top of the freshest bucket.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        text = text.replace(" ", "T", 1) if " " in text[:11] else text
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _age_days(anchor: Optional[datetime], *, now: Optional[datetime] = None) -> Optional[int]:
    """Whole days elapsed since `anchor`. None when the anchor is unknown.

    Floored, not rounded: a claim submitted eight hours ago is 0 days old, not
    1. A desk counts a claim's age in completed days because that is how the
    payer's own service-level window is counted.
    """
    if anchor is None:
        return None
    reference = now or datetime.now(timezone.utc).replace(tzinfo=None)
    delta = reference - anchor
    return max(0, delta.days)


def _bucket_for(age_days: Optional[int]) -> str:
    if age_days is None:
        return "unknown"
    for label, low, high in AGING_BUCKETS:
        if age_days >= low and (high is None or age_days < high):
            return label
    return AGING_BUCKETS[-1][0]


# What each open status ages FROM. The single most common way an aging report
# lies is by measuring everything from created_at: a claim drafted on admission
# and submitted three weeks later would show as 21 days pending with the payer
# on the day it was submitted, and the desk would chase a payer who has had the
# file for four hours. Each status ages from the moment it BEGAN.
_AGING_ANCHOR_LABEL: dict[str, str] = {
    "draft": "Drafted",
    "preauth_requested": "Pre-auth raised",
    "preauth_approved": "Pre-auth decided",
    "preauth_denied": "Pre-auth denied",
    "submitted": "Submitted to payer",
    "queried": "Query raised",
    "approved": "Decision received",
    "partially_approved": "Decision received",
    "denied": "Denied",
    "appealed": "Appeal recorded",
    "settled": "Settled",
    "closed": "Closed",
    "cancelled": "Cancelled",
}

# status → the claim_events.event_type whose latest occurrence starts its clock.
# 'draft' has none (it ages from created_at, which IS when it began).
_AGING_EVENT: dict[str, str] = {
    "preauth_requested": "preauth_requested",
    "preauth_approved": "preauth_decided",
    "preauth_denied": "preauth_decided",
    "submitted": "submitted",
    "queried": "query_raised",
    "approved": "approved",
    "partially_approved": "partially_approved",
    "denied": "denied",
    "appealed": "appealed",
    "settled": "settled",
}

# Column fallbacks, used when the event row is missing (a claim imported or
# repaired outside the normal path). Ordered best-first.
_AGING_COLUMN_FALLBACK: dict[str, tuple[str, ...]] = {
    "submitted": ("submitted_at",),
    "queried": ("submitted_at",),
    "approved": ("decided_at", "submitted_at"),
    "partially_approved": ("decided_at", "submitted_at"),
    "denied": ("decided_at", "submitted_at"),
    "settled": ("settled_at", "decided_at"),
    "appealed": ("decided_at",),
}


def _aging_for(row: dict, event_times: dict[str, str], *, now: Optional[datetime] = None) -> dict:
    """Aging block for one claim, measured from when its CURRENT status began."""
    status = row.get("status") or "draft"
    anchor_raw: Optional[Any] = None
    source = "created_at"

    event_type = _AGING_EVENT.get(status)
    if event_type and event_times.get(event_type):
        anchor_raw = event_times[event_type]
        source = f"event:{event_type}"
    else:
        for column in _AGING_COLUMN_FALLBACK.get(status, ()):
            if row.get(column):
                anchor_raw = row[column]
                source = column
                break
    if anchor_raw is None:
        anchor_raw = row.get("created_at")
        source = "created_at"

    anchor = _parse_ts(anchor_raw)
    days = _age_days(anchor, now=now)
    return {
        "age_days": days,
        "age_bucket": _bucket_for(days),
        "aging_anchor": anchor.strftime("%Y-%m-%d %H:%M:%S") if anchor else None,
        "aging_anchor_source": source,
        "aging_anchor_label": _AGING_ANCHOR_LABEL.get(status, "Updated"),
        "is_stale": bool(days is not None and days >= STALE_AFTER_DAYS and status in OPEN_STATUSES),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ══════════════════════════════════════════════════════════════════════════════

def _row(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unknown_patient_message(patient_id: str) -> str:
    """What the desk reads when a typed id names nobody. One sentence for both
    forms (cover and claim), in words a clerk can act on."""
    return (f"No patient with ID {patient_id} is registered. Check the ID, or "
            "search by name or MRN.")


def _require_transition(current: str, target: str) -> None:
    allowed = LEGAL_TRANSITIONS.get(current)
    if allowed is None:
        raise ClaimError(f"unknown claim status on record: {current!r}")
    if target not in allowed:
        raise IllegalTransition(
            f"a claim that is '{current}' cannot become '{target}'. "
            f"Legal next steps: {', '.join(sorted(allowed)) or 'none, this claim is closed'}."
        )


def _write_event(
    db,
    *,
    claim_id: int,
    event_type: str,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    amount_paise: Optional[int] = None,
    denial_code: Optional[str] = None,
    denial_reason: Optional[str] = None,
    remark: Optional[str] = None,
    document_ref: Optional[str] = None,
    actor_id: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> None:
    """Append one immutable line to the claim's history.

    `occurred_at` is when the DESK did the thing (it may have been on the
    payer's portal yesterday); `recorded_at` defaults to now and is when Orb
    heard about it. Keeping both is what makes an aging report defensible when a
    payer disputes a submission date.
    """
    db.execute(
        """INSERT INTO claim_events
             (claim_id, event_type, from_status, to_status, amount_paise,
              denial_code, denial_reason, remark, document_ref, actor_id,
              occurred_at, recorded_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            claim_id, event_type, from_status, to_status, amount_paise,
            _clean(denial_code), _clean(denial_reason), _clean(remark),
            _clean(document_ref), actor_id, occurred_at or _utc_now_str(),
            _utc_now_str(),
        ),
    )


# The only `claims` columns a lifecycle transition may write. `status` and
# `updated_at` are always written and are not listed.
_UPDATABLE_CLAIM_COLUMNS: frozenset[str] = frozenset({
    "claimed_paise", "approved_paise", "disallowed_paise", "tds_paise",
    "settled_paise", "patient_liability_paise", "preauth_requested_paise",
    "preauth_approved_paise", "preauth_status", "preauth_number",
    "submitted_at", "submitted_by", "decided_at", "settled_at", "external_ref",
    "invoice_id", "notes",
})


def _load_claim(db, claim_id: int):
    row = db.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise ClaimError(f"claim {claim_id} not found")
    return row


def _policy_copay_bp(db, policy_id: Optional[int]) -> int:
    if not policy_id:
        return 0
    row = db.execute(
        "SELECT copay_percent_bp FROM patient_payer_policies WHERE id = ?", (policy_id,)
    ).fetchone()
    return int(row["copay_percent_bp"] or 0) if row else 0


# ══════════════════════════════════════════════════════════════════════════════
#  Payer master
# ══════════════════════════════════════════════════════════════════════════════

def list_payers(
    *,
    active_only: bool = True,
    payer_type: Optional[str] = None,
    include_self_pay: bool = True,
) -> list[dict]:
    """The payer directory, with the tariff plan each payer is priced under.

    `include_self_pay=False` for any screen that is choosing a payer to CLAIM
    from: self-pay is a payer in the billing sense (somebody is paying) but
    there is nobody to send a claim to, and offering it in a claim form is how a
    desk creates a claim that can never be submitted.
    """
    sql = """SELECT p.*, t.code AS tariff_plan_code, t.name AS tariff_plan_name
               FROM payers p
               LEFT JOIN tariff_plans t ON t.id = p.tariff_plan_id
              WHERE 1=1"""
    params: list[Any] = []
    if active_only:
        sql += " AND p.is_active = 1"
    if payer_type:
        sql += " AND p.payer_type = ?"
        params.append(payer_type)
    if not include_self_pay:
        sql += " AND p.payer_type != 'self_pay'"
    sql += " ORDER BY p.payer_type, p.name"
    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]
    for row in rows:
        row["claimable"] = row.get("payer_type") != "self_pay"
        row["preauth_required"] = bool(row.get("preauth_required"))
        row["is_active"] = bool(row.get("is_active"))
        # credit_days is genuinely optional; 0 would read as "pays same day".
        row["credit_days_display"] = (
            f"{row['credit_days']} days" if row.get("credit_days") else "Not configured"
        )
    return rows


# "The caller did not send this field", as distinct from sending None.
_UNSET: Any = object()

# Payer columns an update may leave as they are, with the value a NEW payer gets
# when the caller does not send one.
_PAYER_DEFAULTS: dict[str, Any] = {
    "payer_type": "tpa", "tariff_plan_id": None, "gstin": None, "state_code": None,
    "address": None, "contact_name": None, "contact_phone": None,
    "contact_email": None, "claim_submission_mode": None, "portal_url": None,
    "credit_days": None, "preauth_required": False, "is_active": True, "notes": None,
}


def upsert_payer(
    *,
    code: str,
    name: str,
    payer_type: Any = _UNSET,
    tariff_plan_id: Any = _UNSET,
    gstin: Any = _UNSET,
    state_code: Any = _UNSET,
    address: Any = _UNSET,
    contact_name: Any = _UNSET,
    contact_phone: Any = _UNSET,
    contact_email: Any = _UNSET,
    claim_submission_mode: Any = _UNSET,
    portal_url: Any = _UNSET,
    credit_days: Any = _UNSET,
    preauth_required: Any = _UNSET,
    is_active: Any = _UNSET,
    notes: Any = _UNSET,
    clear_tariff_plan: bool = False,
) -> dict:
    """Create or update one payer, keyed on `code`.

    PARTIAL ON UPDATE. A field the caller does not send keeps its saved value.
    The Insurance desk's form sends code, name, type, mode and pre-auth only;
    when every other column was overwritten with its default, saving a payer's
    name wiped its tariff link (set through the billing masters), and every
    patient of that payer was silently repriced at the default tariff. Clearing
    an existing tariff link therefore also needs `clear_tariff_plan=True`: a
    `tariff_plan_id` of None on its own is refused, never taken as "remove".

    `claim_submission_mode` ('portal' | 'email' | 'paper' | 'api') records HOW
    THE DESK SUBMITS — outside Orb, in every case, including 'api', which means
    the payer has an API that a human uses through the payer's own tooling. Orb
    does not call it.
    """
    code = (_clean(code) or "").upper()
    if not code:
        raise PayerError("payer code must not be blank")
    name = _clean(name) or ""
    if not name:
        raise PayerError("payer name must not be blank")

    with db_session() as db:
        found = db.execute("SELECT * FROM payers WHERE code = ?", (code,)).fetchone()
        existing = _row(found) if found else None
        sent = {
            "payer_type": payer_type, "gstin": gstin, "state_code": state_code,
            "address": address, "contact_name": contact_name,
            "contact_phone": contact_phone, "contact_email": contact_email,
            "claim_submission_mode": claim_submission_mode, "portal_url": portal_url,
            "credit_days": credit_days, "preauth_required": preauth_required,
            "is_active": is_active, "notes": notes,
        }
        v = {
            col: (value if value is not _UNSET
                  else existing[col] if existing else _PAYER_DEFAULTS[col])
            for col, value in sent.items()
        }

        saved_plan = existing["tariff_plan_id"] if existing else None
        if clear_tariff_plan:
            plan = None
        elif tariff_plan_id is _UNSET:
            plan = saved_plan
        elif tariff_plan_id is None and saved_plan is not None:
            raise PayerError(
                f"{code} is linked to a tariff plan. Saving it without one would "
                "price every patient of this payer at the default tariff. To remove "
                "the link, ask for it to be cleared explicitly."
            )
        else:
            plan = tariff_plan_id

        if v["payer_type"] not in PAYER_TYPES:
            raise PayerError(
                f"unknown payer type {v['payer_type']!r}; expected one of {sorted(PAYER_TYPES)}"
            )
        mode = _clean(v["claim_submission_mode"])
        if mode and mode not in ("portal", "email", "paper", "api"):
            raise PayerError(f"unknown claim submission mode {mode!r}")
        credit = v["credit_days"]
        if credit is not None:
            if not isinstance(credit, int) or isinstance(credit, bool) or credit < 0:
                raise PayerError("credit_days must be a non-negative whole number of days")

        db.execute(
            """INSERT INTO payers
                 (code, name, payer_type, tariff_plan_id, gstin, state_code, address,
                  contact_name, contact_phone, contact_email, claim_submission_mode,
                  portal_url, credit_days, preauth_required, is_active, notes, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, payer_type=excluded.payer_type,
                 tariff_plan_id=excluded.tariff_plan_id, gstin=excluded.gstin,
                 state_code=excluded.state_code, address=excluded.address,
                 contact_name=excluded.contact_name, contact_phone=excluded.contact_phone,
                 contact_email=excluded.contact_email,
                 claim_submission_mode=excluded.claim_submission_mode,
                 portal_url=excluded.portal_url, credit_days=excluded.credit_days,
                 preauth_required=excluded.preauth_required,
                 is_active=excluded.is_active, notes=excluded.notes,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                code, name, v["payer_type"], plan, _clean(v["gstin"]), _clean(v["state_code"]),
                _clean(v["address"]), _clean(v["contact_name"]), _clean(v["contact_phone"]),
                _clean(v["contact_email"]), mode, _clean(v["portal_url"]), credit,
                1 if v["preauth_required"] else 0, 1 if v["is_active"] else 0,
                _clean(v["notes"]),
            ),
        )
        row = _row(db.execute("SELECT * FROM payers WHERE code = ?", (code,)).fetchone())
    row["claimable"] = row["payer_type"] != "self_pay"
    row["preauth_required"] = bool(row["preauth_required"])
    row["is_active"] = bool(row["is_active"])
    return row


# ══════════════════════════════════════════════════════════════════════════════
#  Patient policies — the capture path the admission form deliberately lacks
# ══════════════════════════════════════════════════════════════════════════════

def capture_policy(
    *,
    patient_id: str,
    payer_code: str,
    admission_id: Optional[str] = None,
    policy_number: Optional[str] = None,
    member_id: Optional[str] = None,
    scheme_beneficiary_id: Optional[str] = None,
    subscriber_name: Optional[str] = None,
    relationship_to_subscriber: Optional[str] = None,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    sum_insured_paise: Optional[int] = None,
    copay_percent_bp: int = 0,
    preauth_required: Optional[bool] = None,
    is_primary: bool = True,
    created_by: Optional[str] = None,
) -> dict:
    """Record a patient's cover. This is Orb's ONLY payer-capture path.

    A NOTE ON THE SUM INSURED. It is very often unknown at the counter — the
    card shows a policy number, not a balance. So `sum_insured_paise=None`
    stores NULL with `sum_insured_is_configured=0`, and every screen renders
    that as "Not known". A hospital that reads ₹0 of cover as a fact refuses a
    cashless admission it should have accepted.
    """
    patient_id = _clean(patient_id) or ""
    if not patient_id:
        raise PayerError("patient id must not be blank")
    payer_code = (_clean(payer_code) or "").upper()
    if not payer_code:
        raise PayerError("payer code must not be blank")
    # Worded in the percent the person typed, not the basis points the column
    # stores: this message reaches the cover form as it is.
    if not isinstance(copay_percent_bp, int) or isinstance(copay_percent_bp, bool):
        raise PayerError("Co-pay must be a number from 0 to 100.")
    if copay_percent_bp < 0 or copay_percent_bp > 10_000:
        raise PayerError("Co-pay must be a number from 0 to 100.")
    if sum_insured_paise is not None:
        assert_non_negative_paise(sum_insured_paise, "sum_insured_paise")
    if valid_from and valid_to and str(valid_to) < str(valid_from):
        raise PayerError("policy valid_to is before valid_from")

    with db_session() as db:
        patient = db.execute(
            "SELECT id FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if patient is None:
            # No FK exists (migration 20 rule 1), so this is the guard. A policy
            # keyed to a mistyped patient id is money the desk will never find.
            raise PayerError(_unknown_patient_message(patient_id))
        payer = db.execute(
            "SELECT * FROM payers WHERE code = ?", (payer_code,)
        ).fetchone()
        if payer is None:
            raise PayerError(f"unknown payer {payer_code!r}")

        if is_primary:
            # One primary policy per (patient, admission scope). Demoting rather
            # than refusing: the desk correcting a wrong card should not have to
            # delete the old row first.
            if admission_id:
                db.execute(
                    "UPDATE patient_payer_policies SET is_primary = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE patient_id = ? AND admission_id = ?",
                    (patient_id, admission_id),
                )
            else:
                db.execute(
                    "UPDATE patient_payer_policies SET is_primary = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE patient_id = ? AND admission_id IS NULL",
                    (patient_id,),
                )

        cursor = db.execute(
            """INSERT INTO patient_payer_policies
                 (patient_id, admission_id, payer_id, policy_number, member_id,
                  scheme_beneficiary_id, subscriber_name, relationship_to_subscriber,
                  valid_from, valid_to, sum_insured_paise, sum_insured_is_configured,
                  copay_percent_bp, preauth_required, is_primary, status, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)""",
            (
                patient_id, _clean(admission_id), int(payer["id"]), _clean(policy_number),
                _clean(member_id), _clean(scheme_beneficiary_id), _clean(subscriber_name),
                _clean(relationship_to_subscriber), _clean(valid_from), _clean(valid_to),
                sum_insured_paise,
                1 if sum_insured_paise is not None else 0,
                copay_percent_bp,
                1 if (payer["preauth_required"] if preauth_required is None else preauth_required) else 0,
                1 if is_primary else 0,
                created_by,
            ),
        )
        policy_id = int(cursor.lastrowid)

    logger.info("payer policy captured patient=%s payer=%s policy=%s",
                patient_id, payer_code, policy_id)
    return list_policies(patient_id=patient_id, policy_id=policy_id)[0]


def list_policies(
    *,
    patient_id: Optional[str] = None,
    admission_id: Optional[str] = None,
    policy_id: Optional[int] = None,
    active_only: bool = False,
) -> list[dict]:
    """Policies on file, newest first, with the payer and the patient resolved.

    The patient's name and MRN ride along, as they do on every claim
    (_CLAIM_SELECT): the cover list printed bare ids the desk cannot read."""
    sql = """SELECT pp.*, p.code AS payer_code, p.name AS payer_name,
                    p.payer_type AS payer_type, p.claim_submission_mode,
                    pt.name AS patient_name, pt.mrn AS patient_mrn
               FROM patient_payer_policies pp
               LEFT JOIN payers p ON p.id = pp.payer_id
               LEFT JOIN patients pt ON pt.id = pp.patient_id
              WHERE 1=1"""
    params: list[Any] = []
    if policy_id is not None:
        sql += " AND pp.id = ?"
        params.append(policy_id)
    if patient_id:
        sql += " AND pp.patient_id = ?"
        params.append(patient_id)
    if admission_id:
        sql += " AND pp.admission_id = ?"
        params.append(admission_id)
    if active_only:
        sql += " AND pp.status = 'active'"
    sql += " ORDER BY pp.is_primary DESC, pp.id DESC"

    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]

    for row in rows:
        configured = int(row.get("sum_insured_is_configured") or 0) == 1
        row["sum_insured_is_configured"] = configured
        # NOT-CONFIGURED IS NOT ZERO. If the flag is off the amount is withheld
        # entirely, so no caller can read a stale number as a fact.
        row["sum_insured_paise"] = row.get("sum_insured_paise") if configured else None
        row["sum_insured_display"] = format_inr_or_unset(
            row["sum_insured_paise"], unset="Not known"
        )
        row["copay_percent_bp"] = int(row.get("copay_percent_bp") or 0)
        row["copay_display"] = (
            f"{row['copay_percent_bp'] / 100:g}%" if row["copay_percent_bp"] else "None"
        )
        row["preauth_required"] = bool(row.get("preauth_required"))
        row["is_primary"] = bool(row.get("is_primary"))
        row["claimable"] = row.get("payer_type") not in (None, "self_pay")
    return rows


def verify_policy(*, policy_id: int, verified_by: str) -> dict:
    """Mark a policy as checked against the payer (by the desk, on the payer's
    portal — Orb verified nothing)."""
    with db_session() as db:
        row = db.execute(
            "SELECT id FROM patient_payer_policies WHERE id = ?", (policy_id,)
        ).fetchone()
        if row is None:
            raise PayerError(f"policy {policy_id} not found")
        db.execute(
            "UPDATE patient_payer_policies SET verified_by = ?, verified_at = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (verified_by, _utc_now_str(), policy_id),
        )
    return list_policies(policy_id=policy_id)[0]


def set_policy_status(*, policy_id: int, status: str) -> dict:
    """active | exhausted | expired | rejected | cancelled."""
    allowed = {"active", "exhausted", "expired", "rejected", "cancelled"}
    if status not in allowed:
        raise PayerError(f"unknown policy status {status!r}; expected one of {sorted(allowed)}")
    with db_session() as db:
        row = db.execute(
            "SELECT id FROM patient_payer_policies WHERE id = ?", (policy_id,)
        ).fetchone()
        if row is None:
            raise PayerError(f"policy {policy_id} not found")
        db.execute(
            "UPDATE patient_payer_policies SET status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (status, policy_id),
        )
    return list_policies(policy_id=policy_id)[0]


# ══════════════════════════════════════════════════════════════════════════════
#  Claims — creation
# ══════════════════════════════════════════════════════════════════════════════

def create_claim(
    *,
    patient_id: str,
    claim_type: str = "cashless",
    payer_code: Optional[str] = None,
    policy_id: Optional[int] = None,
    admission_id: Optional[str] = None,
    invoice_id: Optional[int] = None,
    claimed_paise: Optional[int] = None,
    package_code: Optional[str] = None,
    notes: Optional[str] = None,
    created_by: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Open a claim file. Creates nothing anywhere but Orb.

    The payer is taken from the explicit `payer_code`, else from the patient's
    policy (`billing_masters.resolve_patient_payer`). A SELF-PAY resolution is
    refused: there is no one to claim from, and a claim against 'SELF' would sit
    in the work queue forever waiting for a submission that cannot happen.

    `idempotency_key`: the same key again returns the claim it opened (with
    `replayed: True`) instead of a second claim file. A double click on "Open
    claim" used to open two files, each with its own CLM number.
    """
    patient_id = _clean(patient_id) or ""
    if not patient_id:
        raise ClaimError("patient id must not be blank")
    # The patient before the policy. An unknown id used to fall through to
    # the policy lookup and come back "has no payer policy on file", sending
    # the desk to capture cover for a patient who does not exist.
    with db_session() as db:
        patient = db.execute(
            "SELECT id, name, mrn FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
    if patient is None:
        raise ClaimError(_unknown_patient_message(patient_id))
    key = _clean(idempotency_key)
    if key:
        replay = _claim_for_key(key, patient_id)
        if replay is not None:
            return replay
    if claim_type not in CLAIM_TYPES:
        raise ClaimError(f"unknown claim type {claim_type!r}; expected one of {sorted(CLAIM_TYPES)}")
    if claimed_paise is not None:
        assert_non_negative_paise(claimed_paise, "claimed_paise")

    resolved_policy_id = policy_id
    resolved_payer_code = (_clean(payer_code) or "").upper() or None

    if resolved_payer_code is None:
        # Fall back to whatever the patient's policy says. resolve_patient_payer
        # answers 'self_pay' when there is no policy, which we then refuse —
        # deliberately, so the desk is told to capture the policy first rather
        # than being handed an unusable claim.
        resolution = resolve_patient_payer(patient_id, admission_id=admission_id)
        resolved_payer_code = resolution.payer_code
        if resolved_policy_id is None:
            resolved_policy_id = resolution.policy_id
        if resolution.payer_type == "self_pay" or not resolved_payer_code:
            mrn = str(patient["mrn"] or "")
            # Seeded MRNs already read "MRN-GM001"; label only a bare number.
            mrn = mrn if mrn.upper().startswith("MRN") else f"MRN {mrn}"
            raise ClaimError(
                f"{patient['name']} ({mrn}) has no payer policy on file. "
                "Capture the policy before raising a claim. A self-pay patient has "
                "no payer to claim from."
            )

    # Allocated OUTSIDE the session below: next_document_number takes its own
    # BEGIN IMMEDIATE, and db_session's contract says an immediate session must
    # be the outermost one (a nested frame is only a SAVEPOINT and cannot be
    # upgraded).
    #
    # From the claims series of the patient's facility (multisite-7a): the
    # admission's, else the latest encounter's; facility 1 while only one is
    # on record. A facility with no claims series of its own is refused.
    from services import facility_identity
    from services.billing_masters import SeriesNotSetUp

    with db_session() as db:
        claim_facility = facility_identity.document_facility_id(
            db, patient_id=patient_id, admission_id=admission_id)
    try:
        number = next_document_number("CLM", facility_id=claim_facility)
    except SeriesNotSetUp as exc:
        raise ClaimError(
            "This facility has no claim number series yet, so no claim was raised. An "
            "administrator sets one up in Admin, Facilities."
        ) from exc

    try:
        claim_id = _insert_claim(
            number=number, patient_id=patient_id, claim_type=claim_type,
            resolved_payer_code=resolved_payer_code, resolved_policy_id=resolved_policy_id,
            admission_id=admission_id, invoice_id=invoice_id, claimed_paise=claimed_paise,
            package_code=package_code, notes=notes, created_by=created_by, key=key,
        )
    except DBIntegrityError:
        # Two requests with the same key raced past the lookup above; the unique
        # index let one through. Answer with that one. The loser's CLM number is
        # left unused, which is a gap in the sequence, not a second claim.
        replay = _claim_for_key(key, patient_id) if key else None
        if replay is None:
            raise
        return replay

    logger.info("claim created id=%s patient=%s payer=%s type=%s",
                claim_id, patient_id, resolved_payer_code, claim_type)
    claim = get_claim(claim_id)
    claim["number_is_configured"] = bool(number.get("is_configured"))
    return claim


def _claim_for_key(key: str, patient_id: str) -> Optional[dict]:
    """The claim an earlier request with this key opened, or None."""
    with db_session() as db:
        row = db.execute(
            "SELECT id, patient_id FROM claims WHERE idempotency_key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    if row["patient_id"] != patient_id:
        raise ClaimError(
            "this request was already used to open a claim for a different patient; "
            "reload the form and try again"
        )
    claim = get_claim(int(row["id"]))
    claim["replayed"] = True
    return claim


def _insert_claim(
    *,
    number: dict,
    patient_id: str,
    claim_type: str,
    resolved_payer_code: str,
    resolved_policy_id: Optional[int],
    admission_id: Optional[str],
    invoice_id: Optional[int],
    claimed_paise: Optional[int],
    package_code: Optional[str],
    notes: Optional[str],
    created_by: Optional[str],
    key: Optional[str],
) -> int:
    """Write the claim row and its 'created' event in one transaction."""
    with db_session() as db:
        patient = db.execute(
            "SELECT id, name FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if patient is None:
            raise ClaimError(f"unknown patient {patient_id!r}")
        payer = db.execute(
            "SELECT * FROM payers WHERE code = ?", (resolved_payer_code,)
        ).fetchone()
        if payer is None:
            raise ClaimError(f"unknown payer {resolved_payer_code!r}")
        if payer["payer_type"] == "self_pay":
            raise ClaimError(
                "cannot raise a claim against a self-pay payer. There is nobody "
                "to submit it to"
            )

        claimed = claimed_paise
        seed_remark: Optional[str] = None
        if claimed is None and invoice_id:
            # Seed from the invoice if the billing module has raised one. The
            # payer's share, not the whole bill: the patient's own share was
            # never the payer's to pay.
            inv = db.execute(
                "SELECT payer_responsibility_paise, payable_paise FROM invoices WHERE id = ?",
                (invoice_id,),
            ).fetchone()
            if inv is not None:
                # ZERO IS AN ANSWER, NOT A MISSING VALUE. Tested with `is None`
                # rather than for truthiness because a 100%-co-pay policy makes
                # `payer_responsibility_paise` a legitimate 0 — the patient owes
                # the whole bill and the payer owes nothing. Under an `or` chain
                # that 0 fell through to `payable_paise` and the desk opened a
                # claim for the ENTIRE invoice against a payer who owed none of
                # it: a figure that goes on a submission, gets denied, and is
                # argued about for weeks. The fallback to `payable_paise` now
                # applies only when the split is genuinely ABSENT (a NULL — a row
                # written by something other than billing_service.create_invoice,
                # which always computes both shares).
                payer_share = inv["payer_responsibility_paise"]
                payable = inv["payable_paise"]
                if payer_share is not None:
                    claimed = int(payer_share)
                elif payable is not None:
                    claimed = int(payable)
                if claimed == 0:
                    seed_remark = (
                        f"Invoice {invoice_id} leaves the payer with a "
                        f"{format_inr(0)} share (the policy puts the whole bill on "
                        "the patient). Nothing is claimable from this payer."
                    )

        preauth_status = "not_required"
        if payer["preauth_required"] or (
            resolved_policy_id is not None and _policy_preauth_required(db, resolved_policy_id)
        ):
            preauth_status = None  # NULL: required but not yet requested.

        cursor = db.execute(
            """INSERT INTO claims
                 (claim_number, patient_id, admission_id, invoice_id, payer_id, policy_id,
                  claim_type, package_code, preauth_status, claimed_paise, status,
                  created_by, notes, idempotency_key)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'draft', ?, ?, ?)""",
            (
                number["number"], patient_id, _clean(admission_id), invoice_id,
                int(payer["id"]), resolved_policy_id, claim_type, _clean(package_code),
                preauth_status, int(claimed or 0), created_by, _clean(notes), key,
            ),
        )
        claim_id = int(cursor.lastrowid)
        _write_event(
            db,
            claim_id=claim_id,
            event_type="created",
            to_status="draft",
            amount_paise=claimed,
            remark=(
                "Claim amount not yet entered. Nothing is claimed until it is."
                if claimed is None else seed_remark
            ),
            actor_id=created_by,
        )
    return claim_id


def _policy_preauth_required(db, policy_id: int) -> bool:
    row = db.execute(
        "SELECT preauth_required FROM patient_payer_policies WHERE id = ?", (policy_id,)
    ).fetchone()
    return bool(row["preauth_required"]) if row else False


def set_claimed_amount(*, claim_id: int, claimed_paise: int, actor_id: Optional[str] = None) -> dict:
    """Set or revise what the hospital is claiming.

    Only while the claim is still the hospital's to change. Once it has been
    submitted, the figure on the payer's desk is the figure of record; revising
    it in Orb would leave the two disagreeing with no trace of which is right.
    A revision after submission goes through a query response or an appeal.
    """
    assert_non_negative_paise(claimed_paise, "claimed_paise")
    revisable = {"draft", "preauth_requested", "preauth_approved", "preauth_denied", "queried"}
    with db_session() as db:
        row = _load_claim(db, claim_id)
        if row["status"] not in revisable:
            raise ClaimError(
                f"the claimed amount cannot be changed once a claim is "
                f"'{row['status']}': record a query response or an appeal instead"
            )
        previous = int(row["claimed_paise"] or 0)
        db.execute(
            "UPDATE claims SET claimed_paise = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (claimed_paise, claim_id),
        )
        _write_event(
            db,
            claim_id=claim_id,
            event_type="note",
            amount_paise=claimed_paise,
            remark=f"Claimed amount set to {format_inr(claimed_paise)} "
                   f"(was {format_inr(previous)}).",
            actor_id=actor_id,
        )
    return get_claim(claim_id)


# ══════════════════════════════════════════════════════════════════════════════
#  Claims — reading
# ══════════════════════════════════════════════════════════════════════════════

_CLAIM_SELECT = """
    SELECT c.*, p.code AS payer_code, p.name AS payer_name, p.payer_type AS payer_type,
           p.claim_submission_mode, p.portal_url,
           pt.name AS patient_name, pt.mrn AS patient_mrn, pt.ward AS patient_ward,
           pol.policy_number AS policy_number, pol.copay_percent_bp AS policy_copay_bp,
           pol.sum_insured_paise AS policy_sum_insured_paise,
           pol.sum_insured_is_configured AS policy_sum_insured_configured
      FROM claims c
      LEFT JOIN payers p ON p.id = c.payer_id
      LEFT JOIN patients pt ON pt.id = c.patient_id
      LEFT JOIN patient_payer_policies pol ON pol.id = c.policy_id
"""


def _event_times(db, claim_id: int) -> dict[str, str]:
    """Latest occurred_at per event type — the aging anchors."""
    rows = db.execute(
        "SELECT event_type, MAX(COALESCE(occurred_at, recorded_at)) AS at "
        "FROM claim_events WHERE claim_id = ? GROUP BY event_type",
        (claim_id,),
    ).fetchall()
    return {r["event_type"]: r["at"] for r in rows if r["at"]}


def _decorate(row: dict, event_times: dict[str, str], *, now: Optional[datetime] = None) -> dict:
    """Add the derived block every screen needs: settlement, aging, honesty."""
    claim_type = row.get("claim_type") or "cashless"
    copay_bp = int(row.get("policy_copay_bp") or 0)
    settlement = compute_settlement(
        claimed_paise=int(row.get("claimed_paise") or 0),
        approved_paise=row.get("approved_paise"),
        copay_percent_bp=copay_bp,
        tds_paise=int(row.get("tds_paise") or 0),
        settled_paise=row.get("settled_paise"),
        claim_type=claim_type,
    )
    row["settlement"] = settlement.as_dict()
    # No TDS is recorded until the payer decides. The arithmetic above takes it
    # as 0, but the screen must not: "TDS ₹0.00" on an undecided claim reads as
    # "the payer withheld nothing", which nobody yet knows.
    if row.get("tds_paise") is None:
        row["settlement"]["tds_paise"] = None
        row["settlement"]["tds_display"] = "Not yet known"

    # The decision-time figures were STORED on the claim; the block above is a
    # LIVE recomputation from the policy as it stands now. They can legitimately
    # disagree — somebody edits the policy's co-pay after a decision — and when
    # they do, the stored figure is the record and the screen must be told there
    # is a discrepancy rather than quietly showing the newer number as if it had
    # always been true.
    stored_liability = row.get("patient_liability_paise")
    row["stored_patient_liability_paise"] = stored_liability
    row["settlement_matches_record"] = (
        stored_liability is None
        or settlement.patient_liability_paise is None
        or int(stored_liability) == int(settlement.patient_liability_paise)
    )

    row.update(_aging_for(row, event_times, now=now))
    row["is_open"] = row.get("status") in OPEN_STATUSES
    row["legal_next_statuses"] = sorted(LEGAL_TRANSITIONS.get(row.get("status") or "draft", ()))
    row["patient_name"] = row.get("patient_name") or "Unknown patient"
    row["claimed_display"] = format_inr(int(row.get("claimed_paise") or 0))
    # Repeated on every claim, not only on the page header, because a claim row
    # is the thing that gets screenshotted, pasted into a chat and acted on
    # alone.
    row["transmission_notice"] = NO_TRANSMISSION_SHORT
    row["hospital_receivable_applies"] = claim_type in HOSPITAL_RECEIVABLE_TYPES
    return row


def get_claim(claim_id: int, *, include_events: bool = True) -> dict:
    with db_session() as db:
        row = db.execute(_CLAIM_SELECT + " WHERE c.id = ?", (claim_id,)).fetchone()
        if row is None:
            raise ClaimError(f"claim {claim_id} not found")
        claim = _decorate(_row(row), _event_times(db, claim_id))
        if include_events:
            events = db.execute(
                "SELECT * FROM claim_events WHERE claim_id = ? ORDER BY id ASC",
                (claim_id,),
            ).fetchall()
            claim["events"] = [
                {
                    **_row(e),
                    "amount_display": format_inr_or_unset(e["amount_paise"], unset="—"),
                }
                for e in events
            ]
    return claim


def list_claims(
    *,
    status: Optional[str] = None,
    statuses: Optional[list[str]] = None,
    payer_code: Optional[str] = None,
    patient_id: Optional[str] = None,
    claim_type: Optional[str] = None,
    open_only: bool = False,
    search: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    sql = _CLAIM_SELECT + " WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND c.status = ?"
        params.append(status)
    if statuses:
        sql += f" AND c.status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    if open_only:
        open_list = sorted(OPEN_STATUSES)
        sql += f" AND c.status IN ({','.join('?' * len(open_list))})"
        params.extend(open_list)
    if payer_code:
        sql += " AND p.code = ?"
        params.append(payer_code.upper())
    if patient_id:
        sql += " AND c.patient_id = ?"
        params.append(patient_id)
    if claim_type:
        sql += " AND c.claim_type = ?"
        params.append(claim_type)
    if search:
        like = f"%{search}%"
        sql += (" AND (c.claim_number LIKE ? OR pt.name LIKE ? OR c.patient_id LIKE ?"
                " OR c.external_ref LIKE ? OR c.preauth_number LIKE ?)")
        params.extend([like, like, like, like, like])
    sql += " ORDER BY c.id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))

    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]
        # One grouped query for the whole page rather than one per claim.
        anchors: dict[int, dict[str, str]] = {}
        if rows:
            ids = [int(r["id"]) for r in rows]
            for e in db.execute(
                f"SELECT claim_id, event_type, MAX(COALESCE(occurred_at, recorded_at)) AS at "
                f"FROM claim_events WHERE claim_id IN ({','.join('?' * len(ids))}) "
                f"GROUP BY claim_id, event_type",
                ids,
            ).fetchall():
                if e["at"]:
                    anchors.setdefault(int(e["claim_id"]), {})[e["event_type"]] = e["at"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return [_decorate(r, anchors.get(int(r["id"]), {}), now=now) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  Claims — the lifecycle. Every one of these records; none of them sends.
# ══════════════════════════════════════════════════════════════════════════════

def _transition(
    claim_id: int,
    target: str,
    *,
    event_type: Optional[str] = None,
    updates: Optional[dict[str, Any]] = None,
    amount_paise: Optional[int] = None,
    denial_code: Optional[str] = None,
    denial_reason: Optional[str] = None,
    remark: Optional[str] = None,
    document_ref: Optional[str] = None,
    actor_id: Optional[str] = None,
    occurred_at: Optional[str] = None,
    guard=None,
) -> dict:
    """Move a claim, atomically, with its evidence.

    ``immediate=True``: this is a read-then-decide-then-write guard. The status
    is read, checked against LEGAL_TRANSITIONS and then written, and without the
    write lock held across all three, two clerks acting on the same claim can
    both read 'denied', both pass their own check, and one of them can land a
    transition the other's write has already made illegal. This is exactly the
    class of race documented on db_session — and here it would let a denied
    claim reach 'settled' through a legal-looking pair of moves.
    """
    with db_session(immediate=True) as db:
        row = _load_claim(db, claim_id)
        current = row["status"]
        _require_transition(current, target)
        if guard is not None:
            guard(row)

        sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = [target]
        for column, value in (updates or {}).items():
            # Column names are interpolated, so they are checked against a fixed
            # list. Every caller of _transition is inside this module and passes
            # a literal, but a whitelist is what keeps that true after the next
            # edit — an f-string over a dict key is one refactor away from being
            # a caller-supplied string.
            if column not in _UPDATABLE_CLAIM_COLUMNS:
                raise ClaimError(f"refusing to update unknown claim column {column!r}")
            sets.append(f"{column} = ?")
            params.append(value)
        params.append(claim_id)
        db.execute(f"UPDATE claims SET {', '.join(sets)} WHERE id = ?", params)

        _write_event(
            db,
            claim_id=claim_id,
            event_type=event_type or _EVENT_FOR_STATUS.get(target, "note"),
            from_status=current,
            to_status=target,
            amount_paise=amount_paise,
            denial_code=denial_code,
            denial_reason=denial_reason,
            remark=remark,
            document_ref=document_ref,
            actor_id=actor_id,
            occurred_at=occurred_at,
        )
    return get_claim(claim_id)


def record_preauth_request(
    *,
    claim_id: int,
    requested_paise: int,
    actor_id: Optional[str] = None,
    reference: Optional[str] = None,
    remark: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    """Record that the desk raised a pre-authorisation WITH THE PAYER, elsewhere.

    Orb sends nothing. `occurred_at` is when the desk actually raised it, which
    is what the aging clock uses — a pre-auth raised on the portal at 09:00 and
    keyed into Orb at 17:00 is eight hours old, not zero.
    """
    assert_non_negative_paise(requested_paise, "requested_paise")
    return _transition(
        claim_id,
        "preauth_requested",
        updates={
            "preauth_requested_paise": requested_paise,
            "preauth_status": "requested",
            "preauth_number": _clean(reference),
        },
        amount_paise=requested_paise,
        remark=remark or NO_TRANSMISSION_SHORT,
        document_ref=_clean(reference),
        actor_id=actor_id,
        occurred_at=occurred_at,
    )


def record_preauth_decision(
    *,
    claim_id: int,
    decision: str,
    approved_paise: Optional[int] = None,
    reference: Optional[str] = None,
    denial_code: Optional[str] = None,
    denial_reason: Optional[str] = None,
    actor_id: Optional[str] = None,
    remark: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    """Record the payer's pre-auth answer: approved | partially_approved | denied.

    A partial approval is the common case and the dangerous one — the payer
    approves ₹40,000 against a ₹90,000 request and the desk must tell the family
    about the gap BEFORE the procedure, not at discharge. So a partial approval
    keeps its own amount and never overwrites the requested figure.
    """
    decision = (_clean(decision) or "").lower()
    if decision not in ("approved", "partially_approved", "denied"):
        raise ClaimError(
            "pre-auth decision must be 'approved', 'partially_approved' or 'denied'"
        )
    if decision == "denied":
        if not _clean(denial_reason):
            # A denial with no reason is a dead end: nobody can appeal "no".
            raise ClaimError("a pre-auth denial must record the payer's reason")
        return _transition(
            claim_id,
            "preauth_denied",
            event_type="preauth_decided",
            updates={
                "preauth_status": "denied",
                "preauth_approved_paise": 0,
                "preauth_number": _clean(reference),
            },
            amount_paise=0,
            denial_code=_clean(denial_code) or "OTHER",
            denial_reason=denial_reason,
            remark=remark,
            document_ref=_clean(reference),
            actor_id=actor_id,
            occurred_at=occurred_at,
        )

    if approved_paise is None:
        raise ClaimError("a pre-auth approval must record the approved amount")
    assert_non_negative_paise(approved_paise, "approved_paise")

    def guard(row):
        requested = row["preauth_requested_paise"]
        if requested is not None and approved_paise > int(requested):
            raise ClaimError(
                f"pre-auth approved ({format_inr(approved_paise)}) exceeds the amount "
                f"requested ({format_inr(int(requested))}): check the figures"
            )
        if decision == "partially_approved" and requested is not None and approved_paise == int(requested):
            raise ClaimError(
                "a 'partially_approved' pre-auth must be for less than the amount "
                "requested. Record 'approved' if the payer allowed it in full"
            )

    return _transition(
        claim_id,
        "preauth_approved",
        event_type="preauth_decided",
        updates={
            "preauth_status": "approved" if decision == "approved" else "partially_approved",
            "preauth_approved_paise": approved_paise,
            "preauth_number": _clean(reference),
        },
        amount_paise=approved_paise,
        remark=remark,
        document_ref=_clean(reference),
        actor_id=actor_id,
        occurred_at=occurred_at,
        guard=guard,
    )


def record_submission(
    *,
    claim_id: int,
    claimed_paise: Optional[int] = None,
    external_ref: Optional[str] = None,
    submitted_at: Optional[str] = None,
    actor_id: Optional[str] = None,
    remark: Optional[str] = None,
) -> dict:
    """Record that the desk SUBMITTED THE CLAIM TO THE PAYER SOMEWHERE ELSE.

    Orb transmits nothing. This writes `submitted_at` — the time the desk says
    it submitted, defaulting to now — and that timestamp is what the whole
    aging report is built on, which is why it is a recorded fact and not a
    system event.
    """
    if claimed_paise is not None:
        assert_non_negative_paise(claimed_paise, "claimed_paise")

    stamp = _clean(submitted_at) or _utc_now_str()
    updates: dict[str, Any] = {
        "submitted_at": stamp,
        "submitted_by": actor_id,
        "external_ref": _clean(external_ref),
    }
    if claimed_paise is not None:
        updates["claimed_paise"] = claimed_paise

    def guard(row):
        amount = claimed_paise if claimed_paise is not None else int(row["claimed_paise"] or 0)
        if amount <= 0:
            # A claim submitted for nothing is a data-entry accident that would
            # sit in the queue as ₹0 and be reconciled against ₹0 forever.
            raise ClaimError(
                "cannot record a submission for ₹0. Enter what was claimed first"
            )

    return _transition(
        claim_id,
        "submitted",
        updates=updates,
        amount_paise=claimed_paise,
        remark=remark or NO_TRANSMISSION_SHORT,
        document_ref=_clean(external_ref),
        actor_id=actor_id,
        occurred_at=stamp,
        guard=guard,
    )


def record_query(
    *,
    claim_id: int,
    query_text: str,
    answered: bool = False,
    actor_id: Optional[str] = None,
    document_ref: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    """Record a payer query (`answered=False`) or the desk's answer to one
    (`answered=True`, which puts the claim back with the payer).

    The aging clock restarts on each: while a query is open the delay is the
    HOSPITAL's, and once answered it is the payer's again. An aging report that
    keeps counting from the original submission blames the payer for the two
    weeks the hospital took to find a discharge summary.
    """
    text = _clean(query_text)
    if not text:
        raise ClaimError("a query must record what the payer asked for")
    if answered:
        return _transition(
            claim_id,
            "submitted",
            event_type="query_answered",
            updates={"submitted_at": occurred_at or _utc_now_str()},
            remark=text,
            document_ref=_clean(document_ref),
            actor_id=actor_id,
            occurred_at=occurred_at,
        )
    return _transition(
        claim_id,
        "queried",
        event_type="query_raised",
        remark=text,
        document_ref=_clean(document_ref),
        actor_id=actor_id,
        occurred_at=occurred_at,
    )


def record_decision(
    *,
    claim_id: int,
    decision: str,
    approved_paise: Optional[int] = None,
    tds_paise: int = 0,
    denial_code: Optional[str] = None,
    denial_reason: Optional[str] = None,
    external_ref: Optional[str] = None,
    actor_id: Optional[str] = None,
    remark: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    """Record the payer's decision on the submitted claim.

    decision: 'approved' | 'partially_approved' | 'denied'.

    The disallowed amount and the patient's liability are COMPUTED here, from
    compute_settlement, and stored — so the figure the desk reads later is the
    figure that was true when the decision came in, not one that drifts when
    somebody edits the policy's co-pay next month.
    """
    decision = (_clean(decision) or "").lower()
    if decision not in ("approved", "partially_approved", "denied"):
        raise ClaimError(
            "decision must be 'approved', 'partially_approved' or 'denied'"
        )
    stamp = _clean(occurred_at) or _utc_now_str()

    if decision == "denied":
        if not _clean(denial_reason):
            raise ClaimError("a denial must record the payer's reason, an unexplained "
                             "denial cannot be appealed")
        with db_session() as db:
            row = _load_claim(db, claim_id)
            claimed = int(row["claimed_paise"] or 0)
            claim_type = row["claim_type"] or "cashless"
        return _transition(
            claim_id,
            "denied",
            updates={
                "approved_paise": 0,
                "disallowed_paise": claimed,
                # The payer refused everything, so the whole claim falls to the
                # patient unless it is appealed. Recorded, never enforced.
                "patient_liability_paise": claimed,
                "decided_at": stamp,
                "external_ref": _clean(external_ref),
            },
            amount_paise=0,
            denial_code=_clean(denial_code) or "OTHER",
            denial_reason=denial_reason,
            remark=remark,
            document_ref=_clean(external_ref),
            actor_id=actor_id,
            occurred_at=stamp,
        )

    if approved_paise is None:
        raise ClaimError("an approval must record the approved amount")
    assert_non_negative_paise(approved_paise, "approved_paise")
    assert_non_negative_paise(tds_paise, "tds_paise")

    target = "approved" if decision == "approved" else "partially_approved"

    # Read + compute + write inside ONE immediate session. The settlement is
    # derived from the row under the write lock (never from a value the caller
    # passed in), and every read it needs — including the policy's co-pay — uses
    # THIS connection: opening a second db_session inside an immediate one would
    # queue a fresh connection behind the write lock this block already holds.
    with db_session(immediate=True) as db:
        row = _load_claim(db, claim_id)
        current = row["status"]
        _require_transition(current, target)

        claimed = int(row["claimed_paise"] or 0)
        settlement = compute_settlement(
            claimed_paise=claimed,
            approved_paise=approved_paise,
            copay_percent_bp=_policy_copay_bp(db, row["policy_id"]),
            tds_paise=tds_paise,
            claim_type=row["claim_type"] or "cashless",
        )
        if target == "approved" and settlement.disallowed_paise:
            raise ClaimError(
                f"the payer allowed {format_inr(approved_paise)} of "
                f"{format_inr(claimed)}: record this as 'partially_approved', "
                "not 'approved'"
            )
        if target == "partially_approved" and not settlement.disallowed_paise:
            raise ClaimError(
                "a 'partially_approved' decision must be for less than the amount "
                "claimed. Record 'approved' if the payer allowed it in full"
            )

        db.execute(
            """UPDATE claims SET status = ?, approved_paise = ?, disallowed_paise = ?,
                   patient_liability_paise = ?, tds_paise = ?, decided_at = ?,
                   external_ref = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?""",
            (
                target, approved_paise, settlement.disallowed_paise,
                settlement.patient_liability_paise, tds_paise, stamp,
                _clean(external_ref), claim_id,
            ),
        )
        _write_event(
            db,
            claim_id=claim_id,
            event_type=_EVENT_FOR_STATUS[target],
            from_status=current,
            to_status=target,
            amount_paise=approved_paise,
            remark=remark,
            document_ref=_clean(external_ref),
            actor_id=actor_id,
            occurred_at=stamp,
        )
    return get_claim(claim_id)


def record_settlement(
    *,
    claim_id: int,
    settled_paise: int,
    tds_paise: Optional[int] = None,
    utr_reference: Optional[str] = None,
    actor_id: Optional[str] = None,
    remark: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    """Record money that ACTUALLY ARRIVED against an approved claim.

    Only reachable from 'approved' or 'partially_approved'. A denied claim
    cannot be settled — see LEGAL_TRANSITIONS. If a payer pays a denied claim,
    the denial was overturned and somebody records the appeal first; that is a
    fact about the world, and it should be in the file.

    Short settlement is computed, not typed: `short_settled = expected receipt −
    received`, where the expected receipt already has the co-pay and TDS taken
    out. A desk that types the shortfall by hand types it wrong.
    """
    assert_non_negative_paise(settled_paise, "settled_paise")
    if tds_paise is not None:
        assert_non_negative_paise(tds_paise, "tds_paise")
    stamp = _clean(occurred_at) or _utc_now_str()

    with db_session(immediate=True) as db:
        row = _load_claim(db, claim_id)
        current = row["status"]
        _require_transition(current, "settled")
        if row["approved_paise"] is None:
            raise ClaimError(
                "cannot record a settlement before the payer's decision. There is "
                "no approved amount to reconcile against"
            )
        effective_tds = int(row["tds_paise"] or 0) if tds_paise is None else tds_paise
        settlement = compute_settlement(
            claimed_paise=int(row["claimed_paise"] or 0),
            approved_paise=int(row["approved_paise"]),
            copay_percent_bp=_policy_copay_bp(db, row["policy_id"]),
            tds_paise=effective_tds,
            settled_paise=settled_paise,
            claim_type=row["claim_type"] or "cashless",
        )
        db.execute(
            """UPDATE claims SET status = 'settled', settled_paise = ?, tds_paise = ?,
                   patient_liability_paise = ?, settled_at = ?, external_ref = COALESCE(?, external_ref),
                   updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?""",
            (
                settled_paise, effective_tds, settlement.patient_liability_paise,
                stamp, _clean(utr_reference), claim_id,
            ),
        )
        note = remark
        if settlement.is_short_settled:
            note = (
                f"SHORT SETTLED by {format_inr(settlement.short_settled_paise or 0)}, "
                f"expected {format_inr(settlement.expected_receipt_paise or 0)}, "
                f"received {format_inr(settled_paise)}."
                + (f" {remark}" if remark else "")
            )
        elif settlement.is_over_settled:
            note = (
                f"OVER SETTLED by {format_inr(abs(settlement.short_settled_paise or 0))}, "
                f"expected {format_inr(settlement.expected_receipt_paise or 0)}, "
                f"received {format_inr(settled_paise)}. Check this against the payer's advice."
                + (f" {remark}" if remark else "")
            )
        _write_event(
            db,
            claim_id=claim_id,
            event_type="settled",
            from_status=current,
            to_status="settled",
            amount_paise=settled_paise,
            remark=note,
            document_ref=_clean(utr_reference),
            actor_id=actor_id,
            occurred_at=stamp,
        )
    return get_claim(claim_id)


def record_appeal(
    *,
    claim_id: int,
    grounds: str,
    actor_id: Optional[str] = None,
    document_ref: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    """Record that the hospital appealed a denial or a partial approval —
    lodged with the payer elsewhere, as always."""
    text = _clean(grounds)
    if not text:
        raise ClaimError("an appeal must record the grounds")
    return _transition(
        claim_id,
        "appealed",
        remark=text,
        document_ref=_clean(document_ref),
        actor_id=actor_id,
        occurred_at=occurred_at,
    )


def close_claim(*, claim_id: int, reason: Optional[str] = None, actor_id: Optional[str] = None) -> dict:
    """Close the file. Terminal — a closed claim can go nowhere."""
    return _transition(
        claim_id, "closed", remark=_clean(reason), actor_id=actor_id
    )


def cancel_claim(*, claim_id: int, reason: str, actor_id: Optional[str] = None) -> dict:
    """Withdraw a claim that should never have been raised. Terminal."""
    text = _clean(reason)
    if not text:
        raise ClaimError("cancelling a claim must record why")
    return _transition(claim_id, "cancelled", remark=text, actor_id=actor_id)


def add_note(*, claim_id: int, note: str, actor_id: Optional[str] = None) -> dict:
    """Append a note without moving the claim. The desk's running commentary —
    'called Ramesh at the TPA, said Tuesday' — which is most of what makes a
    follow-up call productive."""
    text = _clean(note)
    if not text:
        raise ClaimError("a note must not be blank")
    with db_session() as db:
        row = _load_claim(db, claim_id)
        _write_event(
            db,
            claim_id=claim_id,
            event_type="note",
            from_status=row["status"],
            to_status=row["status"],
            remark=text,
            actor_id=actor_id,
        )
    return get_claim(claim_id)


# ══════════════════════════════════════════════════════════════════════════════
#  The work queue — what a TPA desk actually lives in
# ══════════════════════════════════════════════════════════════════════════════

def work_queue(
    *,
    payer_code: Optional[str] = None,
    bucket: Optional[str] = None,
    limit: int = 300,
) -> dict:
    """Everything still owed to (or by) somebody, oldest first, in aging buckets.

    A TPA desk does not read a claim list; it reads THIS. The question it asks
    every morning is "what is stuck and for how long", and the answer has to be
    ordered by age rather than by claim number, bucketed the way the desk
    escalates, and split by whose move it is — because "the payer has had this
    for 40 days" and "we have not answered their query for 40 days" are the same
    number and completely different mornings.
    """
    claims = list_claims(open_only=True, payer_code=payer_code, limit=limit)

    # Oldest first, unknown ages last (they need attention but cannot be ranked).
    claims.sort(key=lambda c: (c["age_days"] is None, -(c["age_days"] or 0)))

    if bucket:
        claims = [c for c in claims if c["age_bucket"] == bucket]

    buckets: list[dict] = []
    for label, _low, _high in AGING_BUCKETS:
        in_bucket = [c for c in claims if c["age_bucket"] == label]
        buckets.append({
            "bucket": label,
            "count": len(in_bucket),
            # Value at risk = what the hospital still expects to receive on these
            # claims. Undecided claims have no expected receipt yet, so the
            # CLAIMED amount stands in and the field says which is which.
            "claimed_paise": sum(int(c.get("claimed_paise") or 0) for c in in_bucket),
            "claimed_display": format_inr(
                sum(int(c.get("claimed_paise") or 0) for c in in_bucket)
            ),
        })
    unknown = [c for c in claims if c["age_bucket"] == "unknown"]
    if unknown:
        buckets.append({
            "bucket": "unknown",
            "count": len(unknown),
            "claimed_paise": sum(int(c.get("claimed_paise") or 0) for c in unknown),
            "claimed_display": format_inr(
                sum(int(c.get("claimed_paise") or 0) for c in unknown)
            ),
        })

    # WHOSE MOVE IS IT. The single most useful cut of this list.
    with_payer = {"preauth_requested", "submitted", "appealed"}
    with_hospital = {"draft", "queried", "preauth_approved", "preauth_denied",
                     "approved", "partially_approved", "denied"}

    return {
        "notice": NO_TRANSMISSION_NOTICE,
        "generated_at": _utc_now_str(),
        "total": len(claims),
        "buckets": buckets,
        "stale_after_days": STALE_AFTER_DAYS,
        "stale_count": sum(1 for c in claims if c["is_stale"]),
        "awaiting_payer": sum(1 for c in claims if c["status"] in with_payer),
        "awaiting_hospital": sum(1 for c in claims if c["status"] in with_hospital),
        "claims": claims,
    }


def denial_log(*, limit: int = 200, payer_code: Optional[str] = None) -> dict:
    """Every denial and every disallowance, with the payer's own reason.

    Rolled up by reason code as well as listed, because a denial log's value is
    not the individual line — it is noticing that eleven claims this quarter died
    on DOCUMENT_DEFICIENCY, which is a fixable process problem inside the
    hospital and not the payer being difficult.
    """
    sql = """SELECT e.*, c.claim_number, c.patient_id, c.status AS claim_status,
                    c.claimed_paise, c.approved_paise, c.claim_type,
                    p.code AS payer_code, p.name AS payer_name,
                    pt.name AS patient_name
               FROM claim_events e
               JOIN claims c ON c.id = e.claim_id
               LEFT JOIN payers p ON p.id = c.payer_id
               LEFT JOIN patients pt ON pt.id = c.patient_id
              WHERE (e.denial_code IS NOT NULL OR e.event_type = 'denied')"""
    params: list[Any] = []
    if payer_code:
        sql += " AND p.code = ?"
        params.append(payer_code.upper())
    sql += " ORDER BY e.id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))

    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]

    labels = dict(DENIAL_REASON_CODES)
    by_code: dict[str, dict] = {}
    for row in rows:
        row["patient_name"] = row.get("patient_name") or "Unknown patient"
        claimed = int(row.get("claimed_paise") or 0)
        approved = row.get("approved_paise")
        # The money the denial actually cost, which is what makes a reason code
        # worth arguing about.
        lost = claimed if approved is None else max(0, claimed - int(approved))
        row["amount_at_stake_paise"] = lost
        row["amount_at_stake_display"] = format_inr(lost)
        code = row.get("denial_code") or "OTHER"
        row["denial_code_label"] = labels.get(code, code)
        entry = by_code.setdefault(code, {
            "denial_code": code,
            "label": labels.get(code, code),
            "count": 0,
            "amount_paise": 0,
        })
        entry["count"] += 1
        entry["amount_paise"] += lost

    summary = sorted(by_code.values(), key=lambda e: (-e["amount_paise"], e["denial_code"]))
    for entry in summary:
        entry["amount_display"] = format_inr(entry["amount_paise"])

    return {
        "notice": NO_TRANSMISSION_NOTICE,
        # Said again here because a denial log is the screen most likely to be
        # exported and read away from the app.
        "codes_are_local": (
            "Reason codes are Orb's own local tags for counting. They are not a "
            "payer's official codes; the payer's own wording is kept verbatim."
        ),
        "total": len(rows),
        "by_code": summary,
        "denials": rows,
    }


def desk_summary() -> dict:
    """The numbers a TPA desk head asks for: open claims, what is at risk, and
    what was short-paid. All computed, none stored."""
    claims = list_claims(open_only=True, limit=1000)
    settled = list_claims(status="settled", limit=1000)

    short_settled = [c for c in settled if c["settlement"]["is_short_settled"]]
    short_total = sum(
        int(c["settlement"]["short_settled_paise"] or 0) for c in short_settled
    )
    # Receivable = what the hospital still expects from payers on open claims.
    # Undecided claims contribute their CLAIMED amount, which is an estimate and
    # is labelled as one; reimbursement claims contribute nothing (the patient
    # pays the hospital, not the payer).
    receivable = 0
    estimated = 0
    for c in claims:
        if not c["hospital_receivable_applies"]:
            continue
        expected = c["settlement"]["expected_receipt_paise"]
        if expected is None:
            estimated += int(c.get("claimed_paise") or 0)
        else:
            receivable += int(expected)

    return {
        "notice": NO_TRANSMISSION_NOTICE,
        "open_claims": len(claims),
        "stale_claims": sum(1 for c in claims if c["is_stale"]),
        "denied_open": sum(1 for c in claims if c["status"] == "denied"),
        "awaiting_preauth": sum(1 for c in claims if c["status"] == "preauth_requested"),
        "confirmed_receivable_paise": receivable,
        "confirmed_receivable_display": format_inr(receivable),
        "estimated_receivable_paise": estimated,
        "estimated_receivable_display": format_inr(estimated),
        "short_settled_count": len(short_settled),
        "short_settled_paise": short_total,
        "short_settled_display": format_inr(short_total),
    }
