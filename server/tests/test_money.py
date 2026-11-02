"""The hospital-administration spine: money maths, masters, schema, flags.

WHAT THESE TESTS ARE FOR. Every other admin module imports services/money.py, so
a defect here is a defect in five modules at once and it shows up as a bill that
is a paise off — the kind of thing nobody notices until an accounts department
refuses to sign a month's revenue. So the assertions here are INVARIANTS, not
examples: "the parts sum to the whole", "no float can be stored", "an exempt item
is never taxed", "an unpriced item never reads as free".

Each block names the failure it is holding shut.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import migrations
from database import db_session
from services import billing_masters as masters
from services.money import (
    BASIS_POINTS,
    GST_EXEMPT,
    GST_NIL_RATED,
    GST_TAXABLE,
    GST_ZERO_RATED,
    InvoiceTotals,
    LineTotals,
    MoneyError,
    QUANTITY_SCALE,
    apply_basis_points,
    assert_paise,
    compute_gst,
    compute_invoice,
    compute_line,
    format_indian_compact,
    format_inr,
    format_inr_or_unset,
    from_paise,
    percent_of,
    round_to_rupee,
    rupees,
    split_shares,
    to_paise,
)


# ══ 1. No float ever enters a stored value ═══════════════════════════════════
#
# THE FAILURE: somebody writes `amount = price * 1.18` and the column now holds
# 118.00000000000001 rupees. Nothing raises; the bill is wrong by a fraction of
# a paise per line and the totals stop reconciling. The gate has to reject floats
# at the type level, including whole-valued ones — by the time a float has a
# clean value you cannot tell whether it started clean.

class TestNoFloatsEver:
    @pytest.mark.parametrize("bad", [1.0, 0.1, -2.5, 1e9, float("nan"), float("inf")])
    def test_assert_paise_rejects_every_float(self, bad):
        with pytest.raises(MoneyError):
            assert_paise(bad)

    def test_assert_paise_rejects_bool(self):
        """True is an int in Python. Stored as money it is one paise."""
        with pytest.raises(MoneyError):
            assert_paise(True)
        with pytest.raises(MoneyError):
            assert_paise(False)

    def test_to_paise_rejects_a_float_amount(self):
        with pytest.raises(MoneyError):
            to_paise(1234.50)

    def test_every_computed_field_is_an_int(self):
        line = compute_line(
            unit_rate_paise=to_paise("1234.57"), quantity_milli=3500,
            discount_bp=750, gst_rate_bp=1800, gst_treatment=GST_TAXABLE,
        )
        for name, value in line.as_dict().items():
            if name.endswith("_paise") or name.endswith("_bp"):
                assert isinstance(value, int) and not isinstance(value, bool), name
        totals = compute_invoice([line])
        for name, value in totals.as_dict().items():
            assert isinstance(value, int) and not isinstance(value, bool), name

    def test_percent_must_not_be_a_float(self):
        from decimal import Decimal
        with pytest.raises(MoneyError):
            percent_of(10000, 7.5)
        # …but the exact forms all work and all agree with basis points.
        assert percent_of(100000, "7.5") == apply_basis_points(100000, 750)
        assert percent_of(100000, Decimal("7.5")) == apply_basis_points(100000, 750)
        assert percent_of(100000, 7) == apply_basis_points(100000, 700)

    def test_a_percent_finer_than_a_basis_point_is_refused(self):
        """Orb stores rates as integer basis points, so 0.005% has nowhere to
        live. Refuse it rather than round it into a rate somebody will read back
        as exact."""
        with pytest.raises(MoneyError):
            percent_of(100000, "0.005")

    def test_non_negative_gate_allows_zero_and_refuses_a_negative(self):
        """For fields where a negative is definitionally wrong — a receipt
        amount, a sum insured. Refunds are NEGATIVE money and use assert_paise."""
        from services.money import assert_non_negative_paise
        assert assert_non_negative_paise(0) == 0
        assert assert_non_negative_paise(500, "receipt amount") == 500
        with pytest.raises(MoneyError):
            assert_non_negative_paise(-1, "receipt amount")


# ══ 2. Parsing and Indian formatting ═════════════════════════════════════════
#
# THE FAILURE: a bill that prints ₹1,234,567.00 — western grouping on an Indian
# invoice — or a parser that quietly turns "10.005" into ₹10.01 and loses the
# fact that the price list has sub-paise junk in it.

class TestParseAndFormat:
    @pytest.mark.parametrize("text,expected", [
        ("0", 0),
        ("1", 100),
        ("1.5", 150),
        ("1.50", 150),
        ("1234.56", 123456),
        ("₹1,23,456.78", 12345678),
        ("Rs. 1,234", 123400),
        ("-45.05", -4505),
        ("(45.05)", -4505),
        ("  12.3  ", 1230),
    ])
    def test_to_paise(self, text, expected):
        assert to_paise(text) == expected

    def test_int_input_is_rupees_not_paise(self):
        """`to_paise(5)` is ₹5, because a human writing a bare number means
        rupees. Paise integers are never re-parsed — they are already paise."""
        assert to_paise(5) == 500

    def test_sub_paise_is_an_error_not_a_silent_round(self):
        with pytest.raises(MoneyError):
            to_paise("10.005")
        # Trailing zeros beyond two places are harmless, not an error.
        assert to_paise("10.5000") == 1050

    @pytest.mark.parametrize("text", ["", "abc", "1.2.3", "₹", "5%", "1e5", "--5"])
    def test_garbage_is_rejected(self, text):
        with pytest.raises(MoneyError):
            to_paise(text)

    @pytest.mark.parametrize("paise,expected", [
        (0, "₹0.00"),
        (5, "₹0.05"),
        (100, "₹1.00"),
        (123456, "₹1,234.56"),
        (12345678, "₹1,23,456.78"),                 # lakh grouping, not thousands
        (1234567890, "₹1,23,45,678.90"),            # crore grouping
        (12345678901234, "₹1,23,45,67,89,012.34"),
        (-4505, "-₹45.05"),
    ])
    def test_format_inr_uses_indian_grouping(self, paise, expected):
        assert format_inr(paise) == expected

    def test_format_round_trips_through_parse(self):
        for paise in (0, 1, 99, 100, 123456789, -55555, 10_00_00_00_000):
            assert to_paise(format_inr(paise)) == paise

    def test_not_configured_is_words_never_zero(self):
        """THE FAILURE this pins: an unpriced item rendering as ₹0.00, which a
        clerk reads as 'free' and bills as free."""
        assert format_inr_or_unset(None) == "Not configured"
        assert format_inr_or_unset(None, unset="Not known") == "Not known"
        assert format_inr_or_unset(0) == "₹0.00"
        assert format_inr_or_unset(None) != format_inr_or_unset(0)

    @pytest.mark.parametrize("paise,expected", [
        (99_99_00, "₹9,999"),
        (1_00_000_00, "₹1.00 L"),
        (12_34_567_00, "₹12.35 L"),
        (1_00_00_000_00, "₹1.00 Cr"),
        (12_34_56_789_00, "₹12.35 Cr"),
        (-1_00_00_000_00, "-₹1.00 Cr"),
    ])
    def test_lakh_crore_compact(self, paise, expected):
        assert format_indian_compact(paise) == expected

    def test_from_paise_is_exact(self):
        assert str(from_paise(123456)) == "1234.56"
        assert str(from_paise(-5)) == "-0.05"


# ══ 3. Rounding at the paise boundary ════════════════════════════════════════
#
# THE FAILURE: two different rounding rules in two modules. A refund computed
# with banker's rounding and a charge computed half-up differ by a paise, and the
# patient's balance never returns to zero.

class TestRounding:
    @pytest.mark.parametrize("paise,bp,expected", [
        (100, 1800, 18),        # ₹1.00 @ 18% = 18p exactly
        (1, 5000, 1),           # 0.5p → 1p   (half UP, not to-even)
        (3, 5000, 2),           # 1.5p → 2p   (to-even would say 2 as well)
        (5, 5000, 3),           # 2.5p → 3p   (to-even would say 2 — this is the point)
        (-5, 5000, -3),         # away from zero, the mirror of the charge
        (-1, 5000, -1),
        (100, 0, 0),
    ])
    def test_half_up_away_from_zero(self, paise, bp, expected):
        assert apply_basis_points(paise, bp) == expected

    def test_refund_is_the_exact_mirror_of_its_charge(self):
        """The invariant that makes a patient balance reach zero: negating the
        input negates every output, with no off-by-one at a .5 boundary."""
        for rate in (1, 3, 5, 7, 33, 12345, 99999):
            charge = compute_line(
                unit_rate_paise=rate, quantity_milli=1500,
                gst_rate_bp=500, gst_treatment=GST_TAXABLE,
            )
            refund = compute_line(
                unit_rate_paise=-rate, quantity_milli=1500,
                gst_rate_bp=500, gst_treatment=GST_TAXABLE,
            )
            assert refund.total_paise == -charge.total_paise
            assert refund.taxable_value_paise == -charge.taxable_value_paise
            assert refund.tax_paise == -charge.tax_paise

    @pytest.mark.parametrize("paise,expected", [
        (0, 0), (49, 0), (50, 100), (51, 100), (149, 100), (150, 200),
        (-50, -100), (-49, 0), (-150, -200),
    ])
    def test_round_to_rupee(self, paise, expected):
        assert round_to_rupee(paise) == expected


# ══ 4. GST: exempt vs taxable, and the CGST/SGST vs IGST split ═══════════════
#
# THE FAILURE this whole block exists to stop: a consultation bill with 18% GST
# on it. Healthcare services by a clinical establishment are exempt in India;
# pharmacy and consumables are not. A system that taxes everything at a default
# rate produces a bill the hospital cannot legally issue.

class TestGst:
    @pytest.mark.parametrize("treatment", [GST_EXEMPT, GST_NIL_RATED, GST_ZERO_RATED])
    def test_non_taxable_treatments_are_never_taxed_whatever_the_rate(self, treatment):
        """Even with 18% sitting on the item master — the stray-rate case."""
        gst = compute_gst(1_000_00, rate_bp=1800, treatment=treatment)
        assert (gst.cgst_paise, gst.sgst_paise, gst.igst_paise, gst.tax_paise) == (0, 0, 0, 0)
        assert gst.taxable_value_paise == 1_000_00

    def test_a_consultation_is_exempt_and_a_syringe_is_not(self):
        consultation = compute_line(
            unit_rate_paise=rupees("500"), gst_rate_bp=1800, gst_treatment=GST_EXEMPT
        )
        syringe = compute_line(
            unit_rate_paise=rupees("500"), gst_rate_bp=1200, gst_treatment=GST_TAXABLE
        )
        assert consultation.total_paise == rupees("500")
        assert consultation.tax_paise == 0
        assert syringe.tax_paise == 6000          # 12% of ₹500 = ₹60
        assert syringe.total_paise == rupees("560")

    def test_intrastate_is_cgst_plus_sgst_and_they_are_equal(self):
        gst = compute_gst(1_000_00, rate_bp=1800, treatment=GST_TAXABLE, interstate=False)
        assert gst.cgst_paise == gst.sgst_paise == 9000     # 9% + 9%
        assert gst.igst_paise == 0
        assert gst.tax_paise == gst.cgst_paise + gst.sgst_paise

    def test_interstate_is_igst_only(self):
        gst = compute_gst(1_000_00, rate_bp=1800, treatment=GST_TAXABLE, interstate=True)
        assert gst.igst_paise == 18000
        assert gst.cgst_paise == gst.sgst_paise == 0
        assert gst.tax_paise == 18000

    def test_the_one_paise_difference_between_the_two_splits_is_deliberate(self):
        """₹1.00 at 5%: intra-state is 2.5p + 2.5p, each rounded half-up to 3p,
        so 6p. Inter-state is one 5p levy. The difference is real — CGST and SGST
        are separate levies, each rounded on its own. Pinned so nobody 'fixes' it
        into a single computation cut in half."""
        intra = compute_gst(100, rate_bp=500, treatment=GST_TAXABLE, interstate=False)
        inter = compute_gst(100, rate_bp=500, treatment=GST_TAXABLE, interstate=True)
        assert intra.tax_paise == 6
        assert inter.tax_paise == 5

    def test_unknown_treatment_is_rejected_not_defaulted(self):
        with pytest.raises(MoneyError):
            compute_gst(100, rate_bp=1800, treatment="maybe_taxable")

    def test_negative_rate_is_rejected(self):
        with pytest.raises(MoneyError):
            compute_gst(100, rate_bp=-1, treatment=GST_TAXABLE)


# ══ 5. Line and invoice arithmetic, and the rounding ORDER ═══════════════════

class TestLinesAndInvoices:
    def test_quantity_is_thousandths_so_half_units_need_no_float(self):
        half = compute_line(unit_rate_paise=rupees("100"), quantity_milli=500)
        assert half.gross_paise == rupees("50")
        two_and_a_quarter = compute_line(
            unit_rate_paise=rupees("100"), quantity_milli=2250
        )
        assert two_and_a_quarter.gross_paise == rupees("225")

    def test_tax_is_computed_on_the_post_discount_value(self):
        """s.15(3)(a) CGST: a discount shown on the face of the invoice reduces
        the taxable value. Taxing the pre-discount value overcharges the patient
        and the government."""
        line = compute_line(
            unit_rate_paise=rupees("1000"), discount_bp=1000,   # 10%
            gst_rate_bp=1800, gst_treatment=GST_TAXABLE,
        )
        assert line.discount_paise == rupees("100")
        assert line.taxable_value_paise == rupees("900")
        assert line.tax_paise == rupees("162")                  # 18% of 900
        assert line.total_paise == rupees("1062")

    def test_discount_cannot_invert_a_line(self):
        line = compute_line(unit_rate_paise=rupees("100"), discount_paise=rupees("500"))
        assert line.discount_paise == rupees("100")
        assert line.total_paise == 0

    def test_a_negative_discount_is_rejected_it_would_be_a_hidden_surcharge(self):
        with pytest.raises(MoneyError):
            compute_line(unit_rate_paise=rupees("100"), discount_paise=-500)

    def test_invoice_is_the_sum_of_rounded_lines_not_a_rounded_sum(self):
        """THE FAILURE: the foot re-derives tax from the subtotal, gets a
        different number from the printed lines, and a clerk adding the column by
        hand disagrees with the system.

        Three lines whose individual taxes each round up. Re-deriving 18% from
        the ₹3.03 subtotal would give 55p; the printed lines add to 57p."""
        lines = [
            compute_line(unit_rate_paise=101, gst_rate_bp=1800, gst_treatment=GST_TAXABLE)
            for _ in range(3)
        ]
        assert [line.tax_paise for line in lines] == [18, 18, 18]
        totals = compute_invoice(lines, round_off_to_rupee=False)
        assert totals.tax_paise == 54
        assert totals.taxable_value_paise == 303
        assert totals.total_paise == 357
        # Not the same as taxing the subtotal in one go, and that is correct.
        assert apply_basis_points(303, 1800) == 55

    def test_every_invoice_field_is_the_exact_sum_of_its_lines(self):
        lines = [
            compute_line(unit_rate_paise=rupees("123.45"), quantity_milli=1500,
                         gst_rate_bp=1800, gst_treatment=GST_TAXABLE),
            compute_line(unit_rate_paise=rupees("99.99"), discount_bp=500,
                         gst_rate_bp=500, gst_treatment=GST_TAXABLE),
            compute_line(unit_rate_paise=rupees("2500"), gst_treatment=GST_EXEMPT),
        ]
        totals = compute_invoice(lines, round_off_to_rupee=False)
        assert totals.total_paise == sum(line.total_paise for line in lines)
        assert totals.tax_paise == sum(line.tax_paise for line in lines)
        assert totals.taxable_value_paise == sum(line.taxable_value_paise for line in lines)
        assert totals.taxable_value_paise + totals.tax_paise == totals.total_paise

    def test_round_off_is_explicit_and_reconciles(self):
        """The 'Round Off' line an Indian invoice prints. The adjustment is
        RECORDED, not smeared into a tax figure — otherwise the tax on the
        invoice stops matching the tax on the lines and the GST return breaks."""
        lines = [compute_line(unit_rate_paise=12345, gst_rate_bp=1800,
                              gst_treatment=GST_TAXABLE)]
        totals = compute_invoice(lines, round_off_to_rupee=True)
        assert totals.total_paise == 14567          # ₹145.67
        assert totals.payable_paise == 14600        # ₹146.00
        assert totals.rounding_adjustment_paise == 33
        assert totals.total_paise + totals.rounding_adjustment_paise == totals.payable_paise

    def test_an_empty_invoice_is_zero_not_an_error(self):
        totals = compute_invoice([])
        assert isinstance(totals, InvoiceTotals)
        assert totals.total_paise == 0 and totals.payable_paise == 0

    def test_credit_note_totals_are_negative_and_round_the_right_way(self):
        line = compute_line(unit_rate_paise=-12345, gst_rate_bp=1800,
                            gst_treatment=GST_TAXABLE)
        totals = compute_invoice([line], round_off_to_rupee=True)
        assert totals.total_paise == -14567
        assert totals.payable_paise == -14600
        assert totals.rounding_adjustment_paise == -33


# ══ 6. Large values — lakh and crore, and the 2^53 line ══════════════════════
#
# THE FAILURE: an annual revenue figure computed in a JS number, or a Python
# float, silently losing precision above 2^53 paise. Python ints do not, and this
# pins that the whole pipeline stays integral at hospital-annual scale.

class TestLargeValues:
    def test_a_crore_bill_is_exact_end_to_end(self):
        one_crore = to_paise("1,00,00,000")
        assert one_crore == 1_00_00_000 * 100          # ₹1 crore, in paise
        line = compute_line(unit_rate_paise=one_crore, gst_rate_bp=1800,
                            gst_treatment=GST_TAXABLE)
        assert line.tax_paise == 18_00_000 * 100       # ₹18 lakh
        assert line.total_paise == 1_18_00_000 * 100   # ₹1.18 crore
        assert format_inr(line.total_paise) == "₹1,18,00,000.00"
        assert format_indian_compact(line.total_paise) == "₹1.18 Cr"

    def test_a_thousand_crore_of_lines_still_reconciles(self):
        lines = [
            compute_line(unit_rate_paise=to_paise("99,99,999.99"),
                         gst_rate_bp=1200, gst_treatment=GST_TAXABLE)
            for _ in range(1000)
        ]
        totals = compute_invoice(lines, round_off_to_rupee=False)
        assert totals.total_paise == sum(line.total_paise for line in lines)
        assert totals.total_paise == 1000 * (999999999 + 119999999 + 1)


# ══ 7. Splitting money without losing a paise ════════════════════════════════
#
# THE FAILURE: payer 80% / patient 20% of ₹100.01 computed independently gives
# ₹80.01 + ₹20.00 = ₹100.01 by luck, or ₹80.01 + ₹20.01 = ₹100.02 by rounding.
# The stray paise becomes a balance nobody can clear.

class TestSplitShares:
    @pytest.mark.parametrize("total,weights", [
        (10001, [80, 20]), (100, [1, 1, 1]), (1, [1, 1]), (7, [1, 2, 4]),
        (999999, [1, 1, 1, 1, 1, 1, 1]), (0, [1, 1]),
    ])
    def test_shares_always_sum_to_the_total(self, total, weights):
        assert sum(split_shares(total, weights)) == total

    def test_negative_totals_split_too(self):
        assert sum(split_shares(-10001, [80, 20])) == -10001
        assert split_shares(-100, [1, 1]) == [-50, -50]

    def test_largest_remainder_is_deterministic(self):
        assert split_shares(100, [1, 1, 1]) == [34, 33, 33]
        assert split_shares(100, [1, 1, 1]) == split_shares(100, [1, 1, 1])

    def test_zero_weights_are_rejected(self):
        with pytest.raises(MoneyError):
            split_shares(100, [0, 0])


# ══ 8. Migration 20 — the shared schema ══════════════════════════════════════

SPINE_TABLES = [
    "service_items", "tariff_plans", "tariff_rates", "package_rates",
    "payers", "patient_payer_policies", "encounter_charges",
    "invoices", "invoice_lines", "document_number_series",
    "payments", "payment_allocations", "patient_deposits", "refunds",
    "discount_approvals", "claims", "claim_events",
    "vendors", "stores", "inventory_items", "indents", "indent_lines",
    "purchase_orders", "purchase_order_lines", "goods_receipts",
    "goods_receipt_lines", "stock_movements",
    "cash_counter_sessions", "discharge_financial_checks",
]


class TestMigration20:
    def test_it_is_registered_and_applied(self):
        assert any(m.version == 20 and m.name == "hospital_admin_spine"
                   for m in migrations.MIGRATIONS)
        with db_session() as db:
            applied = {r[0] for r in db.execute(
                "SELECT version FROM schema_migrations").fetchall()}
        assert 20 in applied

    @pytest.mark.parametrize("table", SPINE_TABLES)
    def test_every_spine_table_exists(self, table):
        with db_session() as db:
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        assert row is not None, f"migration 20 did not create {table}"

    @pytest.mark.parametrize("table", SPINE_TABLES)
    def test_no_foreign_key_anywhere_in_the_spine(self, table):
        """THE INVARIANT: a hospital's money must never be able to make a
        clinical write fail. `PRAGMA foreign_keys=ON` is set on every Orb
        connection, so ONE `REFERENCES patients(id)` here would make a patient
        row undeletable and could fail a clinical table rebuild."""
        with db_session() as db:
            fks = db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        assert list(fks) == [], f"{table} declares a foreign key; the spine must have none"

    @pytest.mark.parametrize("table", SPINE_TABLES)
    def test_no_float_column_anywhere_in_the_spine(self, table):
        """Money is INTEGER paise. A REAL/FLOAT/DOUBLE column is the defect,
        already committed, waiting to be used."""
        with db_session() as db:
            cols = db.execute(f"PRAGMA table_info({table})").fetchall()
        bad = [c[1] for c in cols
               if str(c[2]).upper().split("(")[0] in ("REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL")]
        assert bad == [], f"{table} has non-integer numeric column(s): {bad}"

    @pytest.mark.parametrize("table", SPINE_TABLES)
    def test_money_columns_are_integer_and_named_so(self, table):
        with db_session() as db:
            cols = db.execute(f"PRAGMA table_info({table})").fetchall()
        for col in cols:
            if col[1].endswith("_paise") or col[1].endswith("_milli") or col[1].endswith("_bp"):
                assert str(col[2]).upper() == "INTEGER", f"{table}.{col[1]} is {col[2]}"

    def test_it_is_idempotent(self):
        """Re-running against a live schema is a no-op, not an error — the
        ledger prevents re-application but idempotency is what keeps a manual
        repair run safe."""
        with db_session() as db:
            migrations._m20_hospital_admin_spine(db)
            migrations._m20_hospital_admin_spine(db)
            plans = db.execute(
                "SELECT COUNT(*) FROM tariff_plans WHERE code='SELFPAY'").fetchone()[0]
        assert plans == 1

    def test_charge_capture_is_idempotent_by_key(self):
        """An auto-posted charge replayed by the offline queue must not bill the
        same dose twice."""
        with db_session() as db:
            db.execute(
                "INSERT INTO encounter_charges (patient_id, service_code, description, "
                "idempotency_key) VALUES ('PZZZ','X','x','dup-key-1')"
            )
        with pytest.raises(Exception):
            with db_session() as db:
                db.execute(
                    "INSERT INTO encounter_charges (patient_id, service_code, description, "
                    "idempotency_key) VALUES ('PZZZ','X','x','dup-key-1')"
                )
        with db_session() as db:
            # A NULL key is not a duplicate of another NULL key (partial index).
            db.execute("INSERT INTO encounter_charges (patient_id, service_code, description) "
                       "VALUES ('PZZZ','X','x')")
            db.execute("INSERT INTO encounter_charges (patient_id, service_code, description) "
                       "VALUES ('PZZZ','X','x')")
            db.execute("DELETE FROM encounter_charges WHERE patient_id='PZZZ'")

    def test_procurement_cannot_hold_pharmacy_stock(self):
        """Drug stock belongs to Helix. Two stock ledgers for the same physical
        drug is how a hospital ends up with two answers about a controlled
        substance. The CHECK makes it impossible, not merely discouraged."""
        with pytest.raises(Exception):
            with db_session() as db:
                db.execute(
                    "INSERT INTO inventory_items (code, name, is_pharmacy) "
                    "VALUES ('ZZ-DRUG','Some drug',1)"
                )

    def test_a_dangling_patient_id_is_accepted(self):
        """The consequence of having no foreign keys, asserted deliberately: a
        charge against an unknown patient inserts (and reports as unknown later)
        rather than raising inside whatever clinical flow posted it."""
        with db_session() as db:
            db.execute(
                "INSERT INTO encounter_charges (patient_id, service_code, description) "
                "VALUES ('NO-SUCH-PATIENT','X','x')"
            )
            db.execute("DELETE FROM encounter_charges WHERE patient_id='NO-SUCH-PATIENT'")

    def test_the_discharge_check_defaults_to_advisory(self):
        with db_session() as db:
            db.execute(
                "INSERT INTO discharge_financial_checks (patient_id) VALUES ('PZZZ')"
            )
            row = db.execute(
                "SELECT enforced, blocked_discharge, discharge_proceeded, advisory_status "
                "FROM discharge_financial_checks WHERE patient_id='PZZZ'"
            ).fetchone()
            db.execute("DELETE FROM discharge_financial_checks WHERE patient_id='PZZZ'")
        assert row["enforced"] == 0
        assert row["blocked_discharge"] == 0
        assert row["discharge_proceeded"] == 1
        assert row["advisory_status"] == "unknown"


# ══ 9. Billing masters ═══════════════════════════════════════════════════════

@pytest.fixture()
def clean_masters():
    """Each test owns its own codes; clean up so ordering never matters."""
    yield
    with db_session() as db:
        db.execute("DELETE FROM tariff_rates WHERE service_code LIKE 'TST-%'")
        db.execute("DELETE FROM service_items WHERE code LIKE 'TST-%'")
        db.execute("DELETE FROM payers WHERE code LIKE 'TST-%'")
        db.execute("DELETE FROM patient_payer_policies WHERE patient_id LIKE 'TSTP%'")
        db.execute("DELETE FROM package_rates WHERE package_code LIKE 'TST-%'")


class TestBillingMasters:
    def test_an_unpriced_item_resolves_as_unconfigured_never_zero(self, clean_masters):
        """THE FAILURE this pins: `resolve_rate` returning 0 for an item nobody
        has priced, and a counter billing ₹0 for a CT scan."""
        masters.upsert_service_item(code="TST-CT", name="CT brain", category="imaging")
        res = masters.resolve_rate("TST-CT")
        assert res.configured is False
        assert res.rate_paise is None
        assert res.source == "unconfigured"
        assert res.display == "Not configured"

    def test_an_explicit_zero_is_a_real_price(self, clean_masters):
        """A waived consultation is a genuine ₹0 and must be distinguishable
        from an unpriced one."""
        masters.upsert_service_item(code="TST-FREE", name="Waived review",
                                    category="consultation", default_rate_paise=0)
        res = masters.resolve_rate("TST-FREE")
        assert res.configured is True
        assert res.rate_paise == 0
        assert res.display == "₹0.00"

    def test_unknown_code_is_distinguishable_from_unpriced(self, clean_masters):
        res = masters.resolve_rate("TST-DOES-NOT-EXIST")
        assert res.configured is False
        assert res.source == "unknown_item"

    def test_tariff_rate_beats_the_item_default(self, clean_masters):
        masters.upsert_service_item(code="TST-CONS", name="OP consultation",
                                    category="consultation",
                                    default_rate_paise=rupees("500"))
        plan = masters.default_tariff_plan()
        masters.set_tariff_rate(tariff_plan_id=plan["id"], service_code="TST-CONS",
                                rate_paise=rupees("650"), effective_from="2020-01-01")
        res = masters.resolve_rate("TST-CONS")
        assert res.rate_paise == rupees("650")
        assert res.source == "tariff_rate"

    def test_ward_class_specific_rate_wins_over_the_general_one(self, clean_masters):
        masters.upsert_service_item(code="TST-BED", name="Bed day", category="bed")
        plan = masters.default_tariff_plan()
        masters.set_tariff_rate(tariff_plan_id=plan["id"], service_code="TST-BED",
                                rate_paise=rupees("1500"), effective_from="2020-01-01")
        masters.set_tariff_rate(tariff_plan_id=plan["id"], service_code="TST-BED",
                                ward_class="icu", rate_paise=rupees("6000"),
                                effective_from="2020-01-01")
        assert masters.resolve_rate("TST-BED").rate_paise == rupees("1500")
        assert masters.resolve_rate("TST-BED", ward_class="icu").rate_paise == rupees("6000")
        assert masters.resolve_rate("TST-BED", ward_class="general").rate_paise == rupees("1500")

    def test_a_rate_is_resolved_as_of_a_date_not_as_of_today(self, clean_masters):
        """A bill raised last month must still reproduce from the rate that was
        in force then."""
        masters.upsert_service_item(code="TST-XR", name="X-ray", category="imaging")
        plan = masters.default_tariff_plan()
        masters.set_tariff_rate(tariff_plan_id=plan["id"], service_code="TST-XR",
                                rate_paise=rupees("300"), effective_from="2020-01-01")
        masters.set_tariff_rate(tariff_plan_id=plan["id"], service_code="TST-XR",
                                rate_paise=rupees("450"), effective_from="2026-06-01")
        assert masters.resolve_rate("TST-XR", on_date="2026-05-31").rate_paise == rupees("300")
        assert masters.resolve_rate("TST-XR", on_date="2026-06-01").rate_paise == rupees("450")

    def test_a_clinical_service_carries_its_exemption_through_to_the_line(self, clean_masters):
        masters.upsert_service_item(code="TST-PROC", name="Minor procedure",
                                    category="procedure", gst_treatment=GST_EXEMPT,
                                    gst_rate_bp=1800, default_rate_paise=rupees("2000"))
        masters.upsert_service_item(code="TST-IMPL", name="Implant",
                                    category="implant", gst_treatment=GST_TAXABLE,
                                    gst_rate_bp=1200, default_rate_paise=rupees("2000"))
        proc = masters.resolve_rate("TST-PROC")
        impl = masters.resolve_rate("TST-IMPL")
        proc_line = compute_line(unit_rate_paise=proc.rate_paise,
                                 gst_rate_bp=proc.gst_rate_bp,
                                 gst_treatment=proc.gst_treatment)
        impl_line = compute_line(unit_rate_paise=impl.rate_paise,
                                 gst_rate_bp=impl.gst_rate_bp,
                                 gst_treatment=impl.gst_treatment)
        assert proc_line.tax_paise == 0
        assert impl_line.tax_paise == rupees("240")

    def test_an_unknown_gst_treatment_is_refused_at_the_master(self, clean_masters):
        with pytest.raises(masters.MasterDataError):
            masters.upsert_service_item(code="TST-BAD", name="Bad", gst_treatment="sometimes")

    def test_self_pay_is_the_fallback_payer(self, clean_masters):
        res = masters.resolve_patient_payer("TSTP-NOBODY")
        assert res.payer_type == "self_pay"
        assert res.source == "default_self_pay"
        assert res.sum_insured_configured is False
        assert res.as_dict()["sum_insured_display"] == "Not known"

    def test_an_active_policy_selects_the_payers_tariff(self, clean_masters):
        with db_session() as db:
            db.execute(
                "INSERT INTO tariff_plans (code, name, plan_type) "
                "VALUES ('TST-TPA','Test TPA schedule','tpa') "
                "ON CONFLICT(code) DO NOTHING"
            )
            plan_id = db.execute(
                "SELECT id FROM tariff_plans WHERE code='TST-TPA'").fetchone()["id"]
        payer = masters.upsert_payer(code="TST-PAYER", name="Test TPA",
                                     payer_type="tpa", tariff_plan_id=plan_id)
        with db_session() as db:
            db.execute(
                "INSERT INTO patient_payer_policies (patient_id, payer_id, policy_number, "
                "copay_percent_bp, status) VALUES ('TSTP-1', ?, 'POL-1', 1000, 'active')",
                (payer["id"],),
            )
        res = masters.resolve_patient_payer("TSTP-1")
        assert res.payer_code == "TST-PAYER"
        assert res.tariff_plan_code == "TST-TPA"
        assert res.copay_percent_bp == 1000
        assert res.source == "policy"
        with db_session() as db:
            db.execute("DELETE FROM tariff_plans WHERE code='TST-TPA'")

    def test_an_unknown_sum_insured_reads_as_unknown_not_zero(self, clean_masters):
        payer = masters.upsert_payer(code="TST-INS", name="Test insurer", payer_type="insurer")
        with db_session() as db:
            db.execute(
                "INSERT INTO patient_payer_policies (patient_id, payer_id, status) "
                "VALUES ('TSTP-2', ?, 'active')", (payer["id"],),
            )
        res = masters.resolve_patient_payer("TSTP-2")
        assert res.sum_insured_paise is None
        assert res.as_dict()["sum_insured_display"] == "Not known"

    def test_document_numbers_are_unique_sequential_and_flagged_unconfigured(self):
        first = masters.next_document_number("INV")
        second = masters.next_document_number("INV")
        assert second["sequence"] == first["sequence"] + 1
        assert first["number"] != second["number"]
        assert first["financial_year"] in first["number"]
        # Nobody has set a real series yet, and the UI must be able to say so.
        assert first["is_configured"] == 0

    def test_an_unknown_series_is_an_error_not_a_silent_number(self):
        with pytest.raises(masters.MasterDataError):
            masters.next_document_number("NOPE")

    @pytest.mark.parametrize("day,expected", [
        ("2026-04-01", "2026-27"), ("2026-03-31", "2025-26"),
        ("2026-12-31", "2026-27"), ("2027-01-01", "2026-27"),
    ])
    def test_financial_year_runs_april_to_march(self, day, expected):
        assert masters.financial_year_for(day) == expected

    def test_ensure_default_masters_is_idempotent(self):
        before = masters.ensure_default_masters()
        after = masters.ensure_default_masters()
        assert before["tariff_plans"] == after["tariff_plans"]
        assert before["document_series"] == after["document_series"]

    def test_no_price_is_ever_seeded(self):
        """Money is not a clinical number, but it is still a number people trust.
        A seeded price list would be fabricated data that looks real.

        Asserted as a DELTA across the seed call, not as a count over the table.
        The first spelling counted every priced row in the database and excluded
        only a 'TST-' prefix, so it passed alone and failed the moment a sibling
        suite (billing, procurement, front desk) priced anything — the suite
        order decided the verdict, which makes it a test of nothing. What must
        hold is that SEEDING introduces no price, whatever else the database
        already contains.
        """
        def _counts():
            with db_session() as db:
                return (
                    db.execute("SELECT COUNT(*) FROM service_items "
                               "WHERE rate_is_configured = 1").fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM tariff_rates").fetchone()[0],
                )

        before = _counts()
        masters.ensure_default_masters()
        masters.ensure_default_masters()   # idempotent: a re-run must not price either
        assert _counts() == before, (
            "ensure_default_masters() introduced a price — a seeded rate is "
            "fabricated money that looks real on an invoice"
        )


# ══ 10. Module flags ═════════════════════════════════════════════════════════

class TestModuleFlags:
    def test_every_non_opt_in_module_defaults_on_and_every_opt_in_module_off(self):
        """Founder ruling 2026-08-11: build it all NOW and ship it VISIBLE.
        The three hospital-system modules added on 3 October and the thirteen
        offline workflows added on 12 October 2026 are OPT-IN and default OFF
        (config.OPT_IN_MODULES).

        The assertion is a SUPERSET, not an exact set. It used to name the five
        money modules literally, which meant every later module — biomedical,
        housekeeping, dietary, NABH, workforce — broke this test by existing,
        and the person adding one had to edit an assertion in the money lane to
        say nothing new. What this test is actually for is the RULING: whatever
        default-on modules exist, they are all ON unless a site switched one
        off, and every opt-in module is OFF unless a site switched it on. The
        five money modules stay pinned by name because THIS file owns them; the
        exact membership of the whole board belongs to config, and is asserted
        against the frontend's vocabulary by
        test_module_ids_match_the_frontend_vocabulary below."""
        modules = config.enabled_modules()
        assert {"billing", "payments", "payer", "procurement",
                "frontdesk"} <= set(modules)
        opt_in = set(config.OPT_IN_MODULES)
        assert opt_in == {
            "adt", "pharmacy-store", "lab", "feedback", "mortuary", "cssd",
            "reporting", "finance-tools", "radiology", "care-plans", "emergency",
            "perioperative", "dialysis", "record-governance", "radiation-safety",
            "infection-surveillance",
        }
        assert opt_in <= set(modules)
        assert all(on for m, on in modules.items() if m not in opt_in), modules
        assert not any(modules[m] for m in opt_in), modules

    @pytest.mark.parametrize("raw,expected", [
        (None, True), ("", True), ("   ", True), ("1", True), ("true", True),
        ("on", True), ("yes", True),
        ("0", False), ("false", False), ("FALSE", False), ("off", False), ("no", False),
    ])
    def test_a_blank_switch_means_the_documented_default_not_off(self, monkeypatch, raw, expected):
        """The failure this mirrors: a live switch written with a BLANK value,
        every reader treating blank as falsy, and no log line saying so."""
        if raw is None:
            monkeypatch.delenv("ORB_BILLING", raising=False)
        else:
            monkeypatch.setenv("ORB_BILLING", raw)
        assert config._module_enabled("ORB_BILLING") is expected

    def test_module_ids_match_the_frontend_vocabulary(self):
        assert set(config.HOSPITAL_MODULE_ENV) == set(config.enabled_modules())
        assert config.HOSPITAL_MODULE_ENV["billing"] == "ORB_BILLING"

    def test_discharge_financial_enforcement_defaults_off(self):
        """A patient's discharge must never wait on an unpaid bill."""
        assert config.DISCHARGE_FINANCIAL_ENFORCE is False

    def test_health_endpoint_publishes_the_module_set(self):
        from fastapi.testclient import TestClient
        import main
        with TestClient(main.app) as client:
            body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["modules"] == config.enabled_modules()
