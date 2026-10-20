"""Orb — money primitives. THE single source of truth for money arithmetic.

Every rupee Orb stores, adds, taxes or prints goes through this module. Billing,
payments, payer/claims, procurement and the front desk all import from here;
none of them re-implements a rounding rule, a GST split or an Indian number
format. That is the whole point — the failure mode this module exists to prevent
is five modules that each round "about the same way" and a bill that is two
paise off the sum of its own lines.

THE FOUR RULES
──────────────

1. MONEY IS AN ``int`` OF PAISE. Never a float, never a string, never a Decimal
   at rest. ``0.1 + 0.2`` is not ``0.3`` in binary floating point, and a hospital
   bill that is off by a hundredth of a rupee is a bill the accounts department
   will not sign. ``assert_paise()`` is the gate: it REJECTS ``float`` outright
   (including ``2.0``, which is indistinguishable from a rounding artefact once
   it is in the column) and rejects ``bool``, which is an ``int`` subclass and
   would otherwise store True as one paise.

   Parsing happens ONCE, at the edge, via ``to_paise()``, which takes a string or
   a Decimal and returns an int. Formatting happens ONCE, at the other edge, via
   ``format_inr()``. In between there is only integer arithmetic.

2. ONE ROUNDING RULE: HALF-UP, AWAY FROM ZERO, AT THE PAISE BOUNDARY.
   ``_div_round_half_up(numerator, denominator)`` is the only division in this
   file, and every rounded value in Orb comes out of it. Half-up (not banker's
   rounding) because that is what Indian invoicing, every hospital's existing
   accounting package and every clerk checking the arithmetic by hand expects.
   Away from zero (not toward negative infinity) so a refund of -₹1.005 rounds
   to -₹1.01 — the mirror of the charge that created it. A refund that is not the
   exact mirror of its charge is a reconciliation ticket.

3. ROUNDING HAPPENS EARLY AND ONCE PER LINE, NEVER AGAIN.
   A line is rounded when it is computed (extension, discount, tax). An invoice
   is the INTEGER SUM of already-rounded lines — sum-of-rounded, not
   round-of-sum. This is the order Indian tax invoices are checked in: a
   line-item total that does not reproduce from its own printed inputs is what an
   auditor flags, and it costs at most a paise per line at the invoice foot,
   which the (optional, explicit, separately recorded) round-off line absorbs.

4. NOT CONFIGURED IS NOT ZERO. A price nobody has set is ``None``, and
   ``format_inr_or_unset`` renders it as words, never as "₹0.00". A zero total
   and an unpriced item look identical on a screen and mean opposite things.

WHAT THIS MODULE DOES NOT DO
────────────────────────────
No database, no I/O, no clock, no config. Pure functions over integers, so the
tests can hammer it at the paise boundary and so it can be imported from
anywhere without a cycle.

GST, ENCODED HONESTLY
─────────────────────
Healthcare services supplied by a clinical establishment are largely EXEMPT from
GST in India (Notification 12/2017-CT(R), entry 74). Pharmacy sales, consumables,
implants, non-clinical services and everything bought from a vendor are NOT.
So the treatment is a per-item property (``GstTreatment``) and ``compute_gst``
returns zeros for an exempt item WHATEVER rate is attached to it. The mistake
this prevents is a system that taxes everything at a default rate and produces a
consultation bill with 18% GST on it.

Intra-state supply → CGST + SGST, each at HALF the rate, each rounded on its own
(that is what the law says the two components are, and it is why they can differ
by a paise from a single 18% computation). Inter-state supply → IGST at the full
rate. ``place_of_supply`` is the caller's decision; this module only takes the
resulting boolean.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

__all__ = [
    "PAISE_PER_RUPEE",
    "QUANTITY_SCALE",
    "BASIS_POINTS",
    "GstTreatment",
    "GST_EXEMPT",
    "GST_TAXABLE",
    "GST_NIL_RATED",
    "GST_ZERO_RATED",
    "GST_TREATMENTS",
    "TAXABLE_TREATMENTS",
    "MoneyError",
    "assert_paise",
    "assert_non_negative_paise",
    "to_paise",
    "from_paise",
    "rupees",
    "format_inr",
    "format_inr_or_unset",
    "format_indian_compact",
    "parse_inr",
    "GstBreakup",
    "compute_gst",
    "LineTotals",
    "compute_line",
    "InvoiceTotals",
    "compute_invoice",
    "round_to_rupee",
    "split_shares",
    "percent_of",
    "apply_basis_points",
]

# ── Units ────────────────────────────────────────────────────────────────────

PAISE_PER_RUPEE = 100

#: Quantities are integers of THOUSANDTHS of a unit ("milli-units"), for the same
#: reason money is integer paise: 0.5 vials, 1.5 hours of OT time and 2.25 litres
#: of oxygen are all real, and none of them may arrive as a float. 1 unit = 1000.
QUANTITY_SCALE = 1000

#: Rates and percentages are integer BASIS POINTS. 5% = 500, 12% = 1200,
#: 18% = 1800, 2.5% = 250. There is no such thing as a fractional percent field
#: in Orb, so there is no such thing as a float rate.
BASIS_POINTS = 10_000


class MoneyError(ValueError):
    """A value that cannot be money (a float, a bad string, a bad treatment)."""


# ── GST treatment ────────────────────────────────────────────────────────────

GstTreatment = str

#: Healthcare services by a clinical establishment — consultations, IP care,
#: diagnostics, procedures, room charges. Attracts no GST, and no rate attached
#: to the item can make it attract any.
GST_EXEMPT: GstTreatment = "exempt"
#: Pharmacy, consumables, implants, non-clinical supplies, vendor purchases.
GST_TAXABLE: GstTreatment = "taxable"
#: Nil-rated: in the schedule, at 0%. Distinct from exempt for the GSTR returns,
#: identical in arithmetic.
GST_NIL_RATED: GstTreatment = "nil_rated"
#: Zero-rated: exports / SEZ supplies. Same arithmetic, different return box.
GST_ZERO_RATED: GstTreatment = "zero_rated"

GST_TREATMENTS: frozenset[str] = frozenset({GST_EXEMPT, GST_TAXABLE, GST_NIL_RATED, GST_ZERO_RATED})
#: The only treatment that can ever produce a non-zero tax figure.
TAXABLE_TREATMENTS: frozenset[str] = frozenset({GST_TAXABLE})


# ── The one rounding rule ────────────────────────────────────────────────────


def _div_round_half_up(numerator: int, denominator: int) -> int:
    """Integer division rounding halves AWAY FROM ZERO. The only division here.

    2.5 → 3, -2.5 → -3, 1.4 → 1, -1.4 → -1. Implemented as ``(2n + d) // 2d`` on
    absolute values so there is no float anywhere in the path and no dependence
    on Python's floor-division sign behaviour (``-5 // 2 == -3``, which would
    round -2.5 to -3 by luck on this input and to the wrong side on others).
    """
    if denominator == 0:
        raise MoneyError("division by zero in money arithmetic")
    negative = (numerator < 0) != (denominator < 0)
    n, d = abs(numerator), abs(denominator)
    magnitude = (2 * n + d) // (2 * d)
    return -magnitude if negative else magnitude


def round_to_rupee(paise: int) -> int:
    """Nearest whole rupee, in paise. Same half-up rule (₹1.005 → ₹1.01 is the
    paise boundary; ₹1.50 → ₹2 is this one)."""
    return _div_round_half_up(assert_paise(paise), PAISE_PER_RUPEE) * PAISE_PER_RUPEE


def apply_basis_points(paise: int, basis_points: int) -> int:
    """``paise * basis_points / 10000``, rounded by rule 2. Used for tax, for
    percentage discounts and for co-pay shares — one implementation, so a 7.5%
    co-pay and a 7.5% discount can never disagree by a paise."""
    return _div_round_half_up(
        assert_paise(paise) * _as_int(basis_points, "basis_points"), BASIS_POINTS
    )


def percent_of(paise: int, percent: int | Decimal | str) -> int:
    """Convenience wrapper for a human-written percent ("7.5", Decimal("7.5")).
    Converts to basis points EXACTLY (no float) and defers to
    ``apply_basis_points``."""
    bp = _to_basis_points(percent)
    return apply_basis_points(paise, bp)


def _to_basis_points(percent: int | Decimal | str) -> int:
    if isinstance(percent, bool):
        raise MoneyError("percent must be a number, not a bool")
    if isinstance(percent, float):
        raise MoneyError(
            "percent must not be a float. Pass an int, a Decimal or a string "
            f"(got {percent!r}). Floats are how 7.5% becomes 7.499999999999999%."
        )
    if isinstance(percent, int):
        return percent * 100
    try:
        dec = Decimal(str(percent).strip())
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise MoneyError(f"cannot read {percent!r} as a percent") from exc
    scaled = dec * 100
    if scaled != scaled.to_integral_value():
        raise MoneyError(
            f"percent {percent!r} is finer than one basis point (0.01%); Orb "
            "stores rates as integer basis points"
        )
    return int(scaled)


# ── Type gates ───────────────────────────────────────────────────────────────


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise MoneyError(f"{name} must be an int, not a bool")
    if isinstance(value, float):
        raise MoneyError(f"{name} must be an int, not a float (got {value!r})")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise MoneyError(f"{name} must be a whole number (got {value!r})")
        return int(value)
    raise MoneyError(f"{name} must be an int (got {type(value).__name__})")


def assert_paise(value: object) -> int:
    """The gate every stored money value passes through.

    Returns the value as an ``int`` of paise, or raises. Rejects ``float``
    unconditionally — including whole-valued floats — because by the time a
    float reaches a money column the damage (a value that is 0.30000000000000004
    rupees, or that silently lost precision above 2^53) has already happened and
    is invisible. Rejects ``bool`` because ``True`` is an ``int`` and would be
    stored as one paise.
    """
    return _as_int(value, "money value (paise)")


def assert_non_negative_paise(value: object, name: str = "amount") -> int:
    """For fields where a negative is definitionally wrong (a receipt amount, a
    sum insured). Refunds and credit notes are NEGATIVE money and must NOT use
    this — they use ``assert_paise``."""
    paise = _as_int(value, name)
    if paise < 0:
        raise MoneyError(f"{name} must not be negative (got {paise} paise)")
    return paise


# ── Parsing and formatting ───────────────────────────────────────────────────

_AMOUNT_RE = re.compile(r"^(?P<sign>[-+]?)(?P<int>\d+)(?:\.(?P<frac>\d+))?$")


def to_paise(value: str | int | Decimal) -> int:
    """Parse a human/rupee amount into integer paise. THE ONLY ENTRY POINT.

    Accepts "1234.50", "₹1,23,456.78", "-45", "1,234", Decimal("12.34"), and a
    plain ``int`` of RUPEES. Rejects floats (rule 1) and anything finer than a
    paise — "10.005" is an error, not a silent round, because a price list with
    sub-paise precision is a data-entry mistake that should be fixed at the
    source rather than absorbed here.
    """
    if isinstance(value, bool):
        raise MoneyError("cannot read a bool as an amount")
    if isinstance(value, float):
        raise MoneyError(
            f"cannot read the float {value!r} as money: pass a string or a "
            "Decimal. Floats cannot represent rupees exactly."
        )
    if isinstance(value, int):
        return value * PAISE_PER_RUPEE
    if isinstance(value, Decimal):
        text = format(value, "f")
    else:
        text = str(value)

    cleaned = text.strip().replace("₹", "").replace("Rs.", "").replace("Rs", "")
    cleaned = cleaned.replace(",", "").replace(" ", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        # Accounting notation for a negative.
        cleaned = "-" + cleaned[1:-1]
    match = _AMOUNT_RE.match(cleaned)
    if not match:
        raise MoneyError(f"cannot read {value!r} as a rupee amount")
    frac = match.group("frac") or ""
    if len(frac) > 2:
        if frac[2:].strip("0"):
            raise MoneyError(
                f"{value!r} is finer than one paise; round it at the source "
                "rather than letting Orb guess"
            )
        frac = frac[:2]
    frac = (frac + "00")[:2]
    paise = int(match.group("int")) * PAISE_PER_RUPEE + int(frac)
    return -paise if match.group("sign") == "-" else paise


#: Alias — reads better at a UI/import boundary.
parse_inr = to_paise


def rupees(amount: str | int | Decimal) -> int:
    """``rupees("1234.50") -> 123450``. Sugar for tests and for seed data, so a
    literal price is never written as a bare paise integer nobody can read."""
    return to_paise(amount)


def from_paise(paise: int) -> Decimal:
    """Exact rupees as a ``Decimal``, FOR DISPLAY OR EXPORT ONLY. Never store the
    result; never do arithmetic on it and put it back."""
    p = assert_paise(paise)
    return (Decimal(p) / Decimal(PAISE_PER_RUPEE)).quantize(Decimal("0.01"))


def _group_indian(digits: str) -> str:
    """1234567 → '12,34,567'. Last three digits, then pairs (lakh/crore
    grouping) — not the western thousands grouping, which is what a naive
    ``f"{n:,}"`` produces and what makes an Indian invoice look imported."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts: list[str]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def format_inr(
    paise: int,
    *,
    symbol: bool = True,
    decimals: bool = True,
) -> str:
    """Render paise as an Indian rupee string: ``format_inr(12345678) ->
    '₹1,23,456.78'``.

    The minus sign leads the symbol ("-₹5.00"), which is how every Indian
    accounting package prints a credit and how a refund must read on a receipt.
    """
    p = assert_paise(paise)
    negative = p < 0
    p = abs(p)
    whole, frac = divmod(p, PAISE_PER_RUPEE)
    body = _group_indian(str(whole))
    if decimals:
        body = f"{body}.{frac:02d}"
    if symbol:
        body = f"₹{body}"
    return f"-{body}" if negative else body


def format_inr_or_unset(paise: int | None, *, unset: str = "Not configured") -> str:
    """``None`` is not zero (rule 4). A tariff nobody has entered must read as
    "Not configured" on the screen and in every export, because "₹0.00" is a
    price — a free item — and somebody will bill it."""
    if paise is None:
        return unset
    return format_inr(paise)


def format_indian_compact(paise: int, *, symbol: bool = True) -> str:
    """Dashboard scale: ``'₹1.23 Cr'``, ``'₹4.50 L'``, ``'₹9,999'``.

    Two decimals of the unit, half-up, computed on integers. Only for headline
    figures — never for a bill line, never for anything a clerk reconciles.
    """
    p = assert_paise(paise)
    negative = p < 0
    p = abs(p)
    crore = 100 * 100_000 * PAISE_PER_RUPEE  # ₹1,00,00,000
    lakh = 100_000 * PAISE_PER_RUPEE  # ₹1,00,000
    if p >= crore:
        hundredths = _div_round_half_up(p * 100, crore)
        body = f"{hundredths // 100}.{hundredths % 100:02d} Cr"
    elif p >= lakh:
        hundredths = _div_round_half_up(p * 100, lakh)
        body = f"{hundredths // 100}.{hundredths % 100:02d} L"
    else:
        body = _group_indian(str(p // PAISE_PER_RUPEE))
    if symbol:
        body = f"₹{body}"
    return f"-{body}" if negative else body


# ── GST ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GstBreakup:
    """What tax a taxable value attracts, split the way the invoice prints it."""

    taxable_value_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    tax_paise: int
    rate_bp: int
    treatment: GstTreatment
    interstate: bool

    def as_dict(self) -> dict:
        return {
            "taxable_value_paise": self.taxable_value_paise,
            "cgst_paise": self.cgst_paise,
            "sgst_paise": self.sgst_paise,
            "igst_paise": self.igst_paise,
            "tax_paise": self.tax_paise,
            "gst_rate_bp": self.rate_bp,
            "gst_treatment": self.treatment,
            "is_interstate": 1 if self.interstate else 0,
        }


def compute_gst(
    taxable_value_paise: int,
    *,
    rate_bp: int = 0,
    treatment: GstTreatment = GST_EXEMPT,
    interstate: bool = False,
) -> GstBreakup:
    """GST on one taxable value.

    EXEMPT / NIL-RATED / ZERO-RATED → all zeros, whatever ``rate_bp`` says. That
    is deliberate and it is the India-specific rule this whole module exists to
    encode: a consultation, a bed day and a procedure are healthcare services
    supplied by a clinical establishment and carry no GST, so a stray 18% left on
    the service master must not be able to tax them. Pharmacy, consumables and
    implants are ``GST_TAXABLE`` and do.

    INTRA-STATE → CGST at half the rate + SGST at half the rate, EACH rounded
    independently. The two halves are separate levies, not one number cut in two.
    ₹1.00 at 5% is 2.5% + 2.5% = 2.5p + 2.5p, each rounded half-up to 3p, so the
    intra-state tax is 6p; the SAME value supplied inter-state is 5p of IGST.
    That one-paise difference is real, it follows from the two levies being
    computed separately, and the tests pin it so nobody "fixes" it later.
    """
    value = assert_paise(taxable_value_paise)
    rate = _as_int(rate_bp, "gst_rate_bp")
    if rate < 0:
        raise MoneyError(f"gst_rate_bp must not be negative (got {rate})")
    if treatment not in GST_TREATMENTS:
        raise MoneyError(
            f"unknown GST treatment {treatment!r}; expected one of {sorted(GST_TREATMENTS)}"
        )
    if treatment not in TAXABLE_TREATMENTS or rate == 0:
        return GstBreakup(value, 0, 0, 0, 0, rate, treatment, interstate)

    if interstate:
        igst = apply_basis_points(value, rate)
        return GstBreakup(value, 0, 0, igst, igst, rate, treatment, True)

    half = apply_basis_points(value, rate // 2) if rate % 2 == 0 else None
    if half is None:
        # An odd basis-point rate (e.g. 0.05%) cannot be halved in basis points.
        # Halve the VALUE side of the fraction instead, still with no float:
        # value * rate / (2 * 10000), rounded once per component.
        half = _div_round_half_up(value * rate, 2 * BASIS_POINTS)
    return GstBreakup(value, half, half, 0, half * 2, rate, treatment, False)


# ── Lines ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LineTotals:
    """One priced line — a charge, an invoice line, a PO line. Every field is
    already rounded; nothing downstream rounds again."""

    gross_paise: int
    discount_paise: int
    taxable_value_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    tax_paise: int
    total_paise: int
    gst_rate_bp: int
    gst_treatment: GstTreatment

    def as_dict(self) -> dict:
        return {
            "gross_paise": self.gross_paise,
            "discount_paise": self.discount_paise,
            "taxable_value_paise": self.taxable_value_paise,
            "cgst_paise": self.cgst_paise,
            "sgst_paise": self.sgst_paise,
            "igst_paise": self.igst_paise,
            "tax_paise": self.tax_paise,
            "total_paise": self.total_paise,
            "gst_rate_bp": self.gst_rate_bp,
            "gst_treatment": self.gst_treatment,
        }


def compute_line(
    *,
    unit_rate_paise: int,
    quantity_milli: int = QUANTITY_SCALE,
    discount_paise: int = 0,
    discount_bp: int = 0,
    gst_rate_bp: int = 0,
    gst_treatment: GstTreatment = GST_EXEMPT,
    interstate: bool = False,
) -> LineTotals:
    """The line arithmetic, in a FIXED order that every module shares.

    THE ORDER (this is the contract; do not reorder it anywhere else):

      1. gross    = round(unit_rate * quantity_milli / 1000)
      2. discount = round(gross * discount_bp / 10000) + explicit discount_paise,
                    capped at |gross| so a line can never invert its own sign
      3. taxable  = gross - discount        ← tax is on the POST-discount value,
                    which is the GST valuation rule for a discount shown on the
                    face of the invoice (s.15(3)(a) CGST Act)
      4. tax      = compute_gst(taxable, ...)   ← rounded inside, per component
      5. total    = taxable + tax               ← pure integer addition

    Rounding occurs at steps 1, 2 and 4 and NOWHERE ELSE. A negative
    ``unit_rate_paise`` (a credit note line, a refund) flows through unchanged —
    every step is sign-symmetric.
    """
    rate = assert_paise(unit_rate_paise)
    qty = _as_int(quantity_milli, "quantity_milli")
    explicit_discount = assert_paise(discount_paise)
    disc_bp = _as_int(discount_bp, "discount_bp")
    if disc_bp < 0:
        raise MoneyError(f"discount_bp must not be negative (got {disc_bp})")
    if explicit_discount < 0:
        raise MoneyError(
            f"discount_paise must not be negative (got {explicit_discount}); a "
            "negative discount is a surcharge and must be its own line"
        )

    gross = _div_round_half_up(rate * qty, QUANTITY_SCALE)
    discount = apply_basis_points(abs(gross), disc_bp) + explicit_discount
    if discount > abs(gross):
        # A discount larger than the line is a data error, not a negative bill.
        # Cap rather than raise: the caller (an approval workflow) has already
        # decided the amount, and a hard failure here would block a bill over a
        # rounding edge. The cap is visible in the returned discount_paise.
        discount = abs(gross)
    signed_discount = -discount if gross < 0 else discount
    taxable = gross - signed_discount

    gst = compute_gst(taxable, rate_bp=gst_rate_bp, treatment=gst_treatment, interstate=interstate)
    return LineTotals(
        gross_paise=gross,
        discount_paise=signed_discount,
        taxable_value_paise=taxable,
        cgst_paise=gst.cgst_paise,
        sgst_paise=gst.sgst_paise,
        igst_paise=gst.igst_paise,
        tax_paise=gst.tax_paise,
        total_paise=taxable + gst.tax_paise,
        gst_rate_bp=gst.rate_bp,
        gst_treatment=gst.treatment,
    )


# ── Invoices ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InvoiceTotals:
    subtotal_paise: int  # sum of line gross, before discount
    discount_paise: int
    taxable_value_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    tax_paise: int
    total_paise: int  # taxable + tax, exact sum of the lines
    rounding_adjustment_paise: int
    payable_paise: int  # total + rounding adjustment

    def as_dict(self) -> dict:
        return {
            "subtotal_paise": self.subtotal_paise,
            "discount_paise": self.discount_paise,
            "taxable_value_paise": self.taxable_value_paise,
            "cgst_paise": self.cgst_paise,
            "sgst_paise": self.sgst_paise,
            "igst_paise": self.igst_paise,
            "tax_paise": self.tax_paise,
            "total_paise": self.total_paise,
            "rounding_adjustment_paise": self.rounding_adjustment_paise,
            "payable_paise": self.payable_paise,
        }


def compute_invoice(
    lines: Sequence[LineTotals] | Iterable[LineTotals],
    *,
    round_off_to_rupee: bool = True,
) -> InvoiceTotals:
    """Foot an invoice from already-computed lines.

    SUM OF ROUNDED, NOT ROUNDED SUM (rule 3). Each line was rounded when it was
    computed and is printed that way; the foot is the integer sum of exactly
    those printed numbers, so a clerk adding the column by hand gets the same
    answer the system did. Re-deriving the tax from the invoice subtotal would
    be a second, different rounding and would disagree with the lines by up to a
    paise each.

    ``round_off_to_rupee`` is the customary Indian invoice round-off: the payable
    is the nearest rupee and the difference is recorded EXPLICITLY as
    ``rounding_adjustment_paise`` (the "Round Off" line every Indian invoice
    prints) rather than being smeared into a tax figure. Turn it off for
    payer/claim documents, which are settled to the paise.
    """
    subtotal = discount = taxable = cgst = sgst = igst = tax = total = 0
    for line in lines:
        subtotal += line.gross_paise
        discount += line.discount_paise
        taxable += line.taxable_value_paise
        cgst += line.cgst_paise
        sgst += line.sgst_paise
        igst += line.igst_paise
        tax += line.tax_paise
        total += line.total_paise

    if round_off_to_rupee:
        payable = round_to_rupee(total)
        adjustment = payable - total
    else:
        payable = total
        adjustment = 0

    return InvoiceTotals(
        subtotal_paise=subtotal,
        discount_paise=discount,
        taxable_value_paise=taxable,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=igst,
        tax_paise=tax,
        total_paise=total,
        rounding_adjustment_paise=adjustment,
        payable_paise=payable,
    )


# ── Allocation ───────────────────────────────────────────────────────────────


def split_shares(total_paise: int, weights: Sequence[int]) -> list[int]:
    """Split a total across parties so the parts sum EXACTLY to the total.

    Largest-remainder: each share is floored, then the leftover paise are handed
    out one at a time to the largest remainders. This is how a payer/patient
    split, a co-pay, a package allocated across its component lines and a
    deposit applied to several invoices must all be done — the naive
    ``round(total * w / sum(w))`` per party loses or invents paise, and the paise
    it loses turn into an unexplained balance nobody can clear.

    Weights must be non-negative and not all zero. Sign of the total is
    preserved (a refund splits like a charge).
    """
    total = assert_paise(total_paise)
    ws = [_as_int(w, "weight") for w in weights]
    if any(w < 0 for w in ws):
        raise MoneyError("split weights must not be negative")
    weight_sum = sum(ws)
    if weight_sum == 0:
        raise MoneyError("split weights must not all be zero")

    negative = total < 0
    magnitude = abs(total)
    base = [(magnitude * w) // weight_sum for w in ws]
    remainders = [(magnitude * w) % weight_sum for w in ws]
    leftover = magnitude - sum(base)
    # Hand the leftover paise to the largest remainders; ties go to the earlier
    # party, so the result is deterministic and reproducible in an audit.
    order = sorted(range(len(ws)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        base[i] += 1
    return [-b for b in base] if negative else base
