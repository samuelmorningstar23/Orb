"""Procurement & non-pharmacy stores.

WHAT THIS FILE IS ACTUALLY DEFENDING, in order of how much it would cost to get
wrong:

  1. STOCK ARITHMETIC. On hand must equal opening + receipts − issues, and the
     total must equal the sum of its parts, because a stores system whose
     balance disagrees with its own movements is worse than no stores system at
     all: people trust the number on the screen and stop counting the shelf.
     `test_total_equals_sum_of_parts` asserts exactly that identity, and the
     prove-can-fail note below records that it was watched to go red.

  2. THE STATE MACHINE. Goods cannot be received against an order nobody
     approved; an indent cannot be approved twice; a rejected indent is final.
     Both the LEGAL and the ILLEGAL moves are asserted, because a transition
     table that only ever gets tested on its happy path is a comment.

  3. PARTIAL RECEIPT. The single most common real event in a hospital store —
     the vendor sends 60 of the 100 boxes you ordered — and the one most likely
     to be modelled as all-or-nothing. Outstanding quantity, PO status and the
     refusal to over-receive are all pinned.

  4. THE REORDER BOUNDARY. `on_hand <= reorder_level` fires AT the level, not
     near it. Tested at exactly level−1, level and level+1, because an off-by-one
     here is silent until a store runs dry a cycle early.

  5. THE HELIX BOUNDARY. A drug cannot enter this module's item master. Asserted
     at the service AND at the database, because the CHECK constraint is the part
     that survives somebody writing raw SQL.

  6. MONEY HONESTY. An unpriced item reads "Not configured", never ₹0.00.

  7. THE PERMISSION TIERS (2026-08-18 split). `procurement.read` = the ledgers,
     `procurement.write` = the day's indents and receipts, `procurement.approve`
     = money and vendor standing. A pharmacist (read+write) raises but cannot
     approve, issue, blacklist or write off; a clinician cannot see the stores
     at all; admin holds all three. Asserted over HTTP because the tiers live
     in route dependencies, not in the service.

PROVEN ABLE TO FAIL. Every assertion below was watched go RED with the defect
reintroduced, then restored (2026-08-12). The six defects and what caught them:

  1. `stock_on_hand_milli` summing only positive movements (issues ignored)
     → test_on_hand_equals_opening_plus_receipts_minus_issues,
       test_total_equals_sum_of_parts,
       test_the_running_balance_column_agrees_with_the_sum
  2. `list_stock_on_hand` summing ABS(quantity) instead of the signed value
     → test_total_equals_sum_of_parts (`assert 34000 == 26000`)
  3. reorder trigger `on_hand < level` instead of `<=`
     → test_low_stock_fires_at_the_level_not_near_it[10-True] ONLY — the
       level−1 and level+1 cases stayed green, which is what makes the
       parametrised boundary worth having
  4. `_reorder_level_milli` returning the raw column (unit/milli scale mix-up)
     → four reorder tests
  5. GRN overwriting `received_quantity_milli` instead of accumulating, plus an
     over-receipt guard blind to prior receipts
     → test_the_second_delivery_closes_the_order,
       test_over_receipt_is_refused_at_the_boundary
  6. expiry view `HAVING SUM(...) >= 0` (consumed lots kept)
     → test_expiry_view_excludes_a_fully_consumed_lot
  7. `CONSUMPTION_MOVEMENTS` widened to include transfer_out/write_off
     → test_transfers_and_write_offs_are_not_consumption
  8. `list_stock_on_hand` filtering a store through the movements LEFT JOIN,
     which silently DROPS an item that has run to nil in that store — this was a
     real defect found while writing these tests, not a synthetic one
     → test_an_item_that_has_run_to_nil_in_ONE_store_is_low_in_that_store
"""
import re
from db_backend import DBIntegrityError
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

# Imported as a MODULE, not `from config import PROCUREMENT_ENABLED`: the router
# reads `config.PROCUREMENT_ENABLED` at call time, so the module-flag test has to
# patch the same module object rather than a copied constant.
import config
from auth import create_token
from database import db_session
from main import app
from services import procurement_service as svc
from services.money import QUANTITY_SCALE, rupees

client = TestClient(app)

# Distinctive so this module's fixtures cannot collide with another agent's in
# the shared session DB.
TAG = "PRC"


# ═════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═════════════════════════════════════════════════════════════════════════════

def _uid() -> str:
    return uuid.uuid4().hex[:8].upper()


def _mk_user(role: str = "admin") -> str:
    uid = f"{TAG}U{_uid()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES (?,?,?,'x')",
            (uid, f"Procurement {role}", role),
        )
    return uid


def _auth(user_id: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


@pytest.fixture(scope="module")
def admin_headers() -> dict:
    return _auth(_mk_user("admin"), "admin")


@pytest.fixture(scope="module")
def pharmacist_headers() -> dict:
    return _auth(_mk_user("pharmacist"), "pharmacist")


@pytest.fixture(scope="module")
def nurse_headers() -> dict:
    return _auth(_mk_user("nurse"), "nurse")


@pytest.fixture(scope="module")
def doctor_headers() -> dict:
    return _auth(_mk_user("doctor"), "doctor")


@pytest.fixture
def store() -> dict:
    return svc.upsert_store(
        code=f"{TAG}S{_uid()}", name="Procurement test store",
        store_type="sub_store", department="Surgery",
    )


@pytest.fixture
def vendor() -> dict:
    return svc.upsert_vendor(
        code=f"{TAG}V{_uid()}", name="Test Surgical Supplies",
        state_code="27", gstin="27AAAAA0000A1Z5",
    )


def _mk_item(**kwargs) -> dict:
    defaults = dict(
        code=f"{TAG}I{_uid()}", name="Sterile glove size 7",
        category="consumable", uom="box", gst_treatment="taxable",
        gst_rate_bp=1200,
    )
    defaults.update(kwargs)
    return svc.upsert_inventory_item(**defaults)


@pytest.fixture
def item() -> dict:
    return _mk_item()


def _full_chain(vendor, store, item, *, qty_milli: int, rate_paise: int):
    """indent → approved → PO → issued. Returns (indent, po)."""
    indent = svc.create_indent(
        store_code=store["code"], department="Surgery",
        lines=[{"item_code": item["code"], "quantity_milli": qty_milli}],
        submit=True, requested_by="tester",
    )
    indent = svc.decide_indent(indent["id"], actor="approver")
    po = svc.create_purchase_order(
        vendor_code=vendor["code"], store_code=store["code"], indent_id=indent["id"],
        lines=[{"item_code": item["code"], "quantity_milli": qty_milli,
                "unit_rate_paise": rate_paise}],
        created_by="buyer",
    )
    po = svc.submit_purchase_order(po["id"])
    po = svc.approve_purchase_order(po["id"], actor="approver")
    po = svc.issue_purchase_order(po["id"], actor="approver")
    return indent, po


# ═════════════════════════════════════════════════════════════════════════════
#  1. THE HELIX BOUNDARY — drugs are not stocked here
# ═════════════════════════════════════════════════════════════════════════════

class TestPharmacyBoundary:
    """Two stock systems that disagree is worse than one incomplete one. The
    boundary is structural, not a convention."""

    def test_service_refuses_a_pharmacy_item_and_names_helix(self):
        with pytest.raises(svc.PharmacyItemRefused) as exc:
            _mk_item(is_pharmacy=True)
        message = str(exc.value).lower()
        assert "helix" in message, "the refusal must tell the user where drugs live"
        assert "pharmacy" in message

    def test_the_database_itself_refuses_a_pharmacy_row(self):
        """The CHECK constraint is the guarantee that survives raw SQL. If this
        ever passes, the service-level guard is the ONLY thing standing between
        Orb and two ledgers for the same controlled substance."""
        with pytest.raises(DBIntegrityError):
            with db_session() as db:
                db.execute(
                    "INSERT INTO inventory_items (code, name, category, is_pharmacy) "
                    "VALUES (?,?,?,1)", (f"{TAG}DRUG{_uid()}", "Morphine 10mg", "other"),
                )

    def test_medication_is_not_a_valid_category(self):
        with pytest.raises(svc.ProcurementError) as exc:
            _mk_item(category="medication")
        assert "helix" in str(exc.value).lower()

    def test_no_sql_in_this_module_touches_a_helix_table(self):
        """STRUCTURAL, and the strongest form of this assertion available.

        A row-count check only proves Helix was untouched by the ONE path a test
        happened to drive. This reads every SQL string in the procurement service
        and asserts that not one of them names a `helix_` table — so a future
        function that decides to "just read the drug lots for a combined stock
        view" fails here, at the moment it is written, rather than after two
        ledgers have been disagreeing in production for a month.
        """
        import ast
        import pathlib

        source = pathlib.Path(svc.__file__).read_text()
        tree = ast.parse(source)
        sql_keyword = re.compile(
            r"\b(select|insert\s+into|update|delete\s+from|join)\b", re.I)
        offenders = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and sql_keyword.search(node.value) and "helix_" in node.value.lower()
        ]
        assert offenders == [], (
            "procurement SQL must never read or write a Helix table — drug stock "
            f"belongs to pharmacy. Offending statements: {offenders}"
        )

    def test_a_receipt_changes_no_helix_table_that_exists(
            self, vendor, store, item):
        """The dynamic half: whatever Helix tables this session has, a stores
        receipt leaves every one of them exactly as it found it."""
        def _helix_counts() -> dict:
            with db_session() as db:
                names = [r["name"] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'helix\\_%' ESCAPE '\\'").fetchall()]
                return {n: db.execute(f"SELECT COUNT(*) AS c FROM {n}").fetchone()["c"]
                        for n in names}

        before = _helix_counts()
        _, po = _full_chain(vendor, store, item,
                            qty_milli=10 * QUANTITY_SCALE, rate_paise=rupees("50"))
        svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 10 * QUANTITY_SCALE}],
        )
        assert _helix_counts() == before, \
            "a stores receipt must never write into Helix"
        # …and the stock it DID move is in this module's ledger.
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 10 * QUANTITY_SCALE


# ═════════════════════════════════════════════════════════════════════════════
#  2. THE STATE MACHINE — legal and illegal transitions
# ═════════════════════════════════════════════════════════════════════════════

class TestIndentStateMachine:

    def test_draft_to_submitted_to_approved_is_legal(self, store, item):
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": 5 * QUANTITY_SCALE}],
        )
        assert indent["status"] == "draft"
        assert indent["indent_number"] is None, \
            "a draft must not burn a number out of the IND- series"

        indent = svc.submit_indent(indent["id"], actor="ward")
        assert indent["status"] == "submitted"
        assert indent["indent_number"], "submitting allocates the document number"

        indent = svc.decide_indent(indent["id"], actor="approver")
        assert indent["status"] == "approved"

    def test_a_draft_cannot_be_approved_without_being_submitted(self, store, item):
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE}],
        )
        with pytest.raises(svc.IllegalTransition) as exc:
            svc.decide_indent(indent["id"], actor="approver")
        assert "draft" in str(exc.value)

    def test_a_rejected_indent_is_final(self, store, item):
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE}],
            submit=True,
        )
        indent = svc.decide_indent(indent["id"], reject=True, reason="Not budgeted")
        assert indent["status"] == "rejected"
        assert svc.INDENT_TRANSITIONS["rejected"] == frozenset()
        with pytest.raises(svc.IllegalTransition):
            svc.decide_indent(indent["id"], actor="approver")
        with pytest.raises(svc.IllegalTransition):
            svc.cancel_indent(indent["id"], reason="changed my mind")

    def test_a_rejection_needs_a_reason(self, store, item):
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE}],
            submit=True,
        )
        with pytest.raises(svc.ProcurementError) as exc:
            svc.decide_indent(indent["id"], reject=True)
        assert exc.value.code == "missing_field"

    def test_partial_approval_records_the_cut_as_a_number(self, store, item):
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": 100 * QUANTITY_SCALE}],
            submit=True,
        )
        indent = svc.decide_indent(indent["id"],
                                   approvals={1: 40 * QUANTITY_SCALE},
                                   actor="approver")
        assert indent["status"] == "partially_approved"
        line = indent["lines"][0]
        assert line["quantity_milli"] == 100 * QUANTITY_SCALE
        assert line["approved_quantity_milli"] == 40 * QUANTITY_SCALE
        assert line["effective_quantity_milli"] == 40 * QUANTITY_SCALE, \
            "a PO must be raised off the APPROVED quantity, not the requested one"

    def test_approving_more_than_was_asked_for_is_refused(self, store, item):
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": 10 * QUANTITY_SCALE}],
            submit=True,
        )
        with pytest.raises(svc.ProcurementError) as exc:
            svc.decide_indent(indent["id"], approvals={1: 11 * QUANTITY_SCALE})
        assert exc.value.code == "over_approval"

    def test_zeroing_every_line_rejects_the_indent(self, store, item):
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": 10 * QUANTITY_SCALE}],
            submit=True,
        )
        indent = svc.decide_indent(indent["id"], approvals={1: 0})
        assert indent["status"] == "rejected"

    def test_before_a_decision_nothing_is_approved(self, store, item):
        """`effective_quantity_milli` is None on an undecided indent — NOT the
        requested quantity. Defaulting it to the request would let a purchase
        order be raised off an indent nobody signed."""
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": 7 * QUANTITY_SCALE}],
            submit=True,
        )
        assert indent["lines"][0]["effective_quantity_milli"] is None


class TestPurchaseOrderStateMachine:

    def test_the_full_legal_path(self, vendor, store, item):
        _, po = _full_chain(vendor, store, item,
                            qty_milli=20 * QUANTITY_SCALE, rate_paise=rupees("125"))
        assert po["status"] == "issued"
        assert po["po_number"], "a PO gets its number when it is issued to a vendor"
        assert po["can_receive"] is True

    def test_a_draft_po_cannot_be_received_against(self, vendor, store, item):
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("10")}],
        )
        assert po["status"] == "draft"
        assert po["can_receive"] is False
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_goods_receipt(
                purchase_order_id=po["id"],
                lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE}],
            )
        assert exc.value.code == "po_not_receivable"

    def test_a_draft_cannot_jump_straight_to_issued(self, vendor, store, item):
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("10")}],
        )
        with pytest.raises(svc.IllegalTransition):
            svc.issue_purchase_order(po["id"])

    def test_a_partially_received_po_cannot_be_cancelled(self, vendor, store, item):
        """Once a delivery has landed the order is no longer cancellable — it is
        short-closed, so the receipt that did happen stays on the record. The
        transition table is the first of two guards."""
        _, po = _full_chain(vendor, store, item,
                            qty_milli=10 * QUANTITY_SCALE, rate_paise=rupees("40"))
        svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 4 * QUANTITY_SCALE}],
        )
        assert svc.get_purchase_order(po["id"])["status"] == "partially_received"
        assert "cancelled" not in svc.PO_TRANSITIONS["partially_received"]
        with pytest.raises(svc.IllegalTransition):
            svc.cancel_purchase_order(po["id"], reason="vendor went quiet")
        assert svc.get_purchase_order(po["id"])["status"] == "partially_received"

    def test_the_quantity_guard_catches_a_po_whose_status_lags_its_lines(
            self, vendor, store, item):
        """THE SECOND GUARD, and the reason it is not redundant.

        Normally a receipt drives the PO off `issued`, so the transition table
        alone would do. This test forces the state the table cannot see — line
        quantities received while the header still says `issued` — which is what
        a future code path that updates quantities without refreshing status
        would produce. The cancel must still be refused, because the goods are
        physically here whatever the header says.
        """
        _, po = _full_chain(vendor, store, item,
                            qty_milli=10 * QUANTITY_SCALE, rate_paise=rupees("40"))
        with db_session() as db:
            db.execute(
                "UPDATE purchase_order_lines SET received_quantity_milli = ? "
                "WHERE purchase_order_id = ?", (4 * QUANTITY_SCALE, po["id"]))
        assert svc.get_purchase_order(po["id"])["status"] == "issued"
        with pytest.raises(svc.ProcurementError) as exc:
            svc.cancel_purchase_order(po["id"], reason="vendor went quiet")
        assert exc.value.code == "already_received"

    def test_a_blacklisted_vendor_cannot_take_a_new_order(self, vendor, store, item):
        svc.set_vendor_blacklist(vendor["code"], blacklisted=True,
                                 reason="repeated short supply")
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_purchase_order(
                vendor_code=vendor["code"], store_code=store["code"],
                lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                        "unit_rate_paise": rupees("10")}],
            )
        assert exc.value.code == "vendor_blacklisted"

    def test_a_po_cannot_be_raised_against_an_unapproved_indent(
            self, vendor, store, item):
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE}],
            submit=True,
        )
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_purchase_order(
                vendor_code=vendor["code"], indent_id=indent["id"],
                lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                        "unit_rate_paise": rupees("10")}],
            )
        assert exc.value.code == "indent_not_approved"


class TestGoodsReceiptStateMachine:

    def test_a_posted_receipt_cannot_be_cancelled(self, vendor, store, item):
        """The stock has already moved. Allowing a cancel here would delete a
        receipt while the goods stay on the shelf."""
        _, po = _full_chain(vendor, store, item,
                            qty_milli=5 * QUANTITY_SCALE, rate_paise=rupees("30"))
        grn = svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 5 * QUANTITY_SCALE}],
        )
        assert grn["status"] == "posted"
        assert "cancelled" not in svc.GRN_TRANSITIONS["posted"]
        with pytest.raises(svc.IllegalTransition):
            svc.cancel_goods_receipt(grn["id"], reason="mistake")

    def test_a_draft_receipt_can_be_cancelled_and_moves_no_stock(
            self, vendor, store, item):
        _, po = _full_chain(vendor, store, item,
                            qty_milli=5 * QUANTITY_SCALE, rate_paise=rupees("30"))
        grn = svc.create_goods_receipt(
            purchase_order_id=po["id"], post=False,
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 5 * QUANTITY_SCALE}],
        )
        assert grn["status"] == "draft"
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 0, \
            "a draft receipt must not put stock on a shelf"
        grn = svc.cancel_goods_receipt(grn["id"], reason="wrong PO")
        assert grn["status"] == "cancelled"
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 0

    def test_posting_twice_is_refused(self, vendor, store, item):
        _, po = _full_chain(vendor, store, item,
                            qty_milli=5 * QUANTITY_SCALE, rate_paise=rupees("30"))
        grn = svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 5 * QUANTITY_SCALE}],
        )
        with pytest.raises(svc.IllegalTransition):
            svc.post_goods_receipt(grn["id"])

    def test_idempotency_key_replays_instead_of_double_posting(
            self, vendor, store, item):
        """The classic double-tap on a slow connection. A double-posted GRN
        inflates a store by exactly one delivery and nobody finds it until a
        physical count months later."""
        _, po = _full_chain(vendor, store, item,
                            qty_milli=10 * QUANTITY_SCALE, rate_paise=rupees("20"))
        key = f"grn-{_uid()}"
        first = svc.create_goods_receipt(
            purchase_order_id=po["id"], idempotency_key=key,
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 6 * QUANTITY_SCALE}],
        )
        second = svc.create_goods_receipt(
            purchase_order_id=po["id"], idempotency_key=key,
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 6 * QUANTITY_SCALE}],
        )
        assert second["id"] == first["id"]
        assert second.get("idempotent_replay") is True
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 6 * QUANTITY_SCALE


# ═════════════════════════════════════════════════════════════════════════════
#  3. PARTIAL RECEIPT — the normal case, and the one most often modelled wrong
# ═════════════════════════════════════════════════════════════════════════════

class TestPartialReceipt:

    def test_partial_receipt_leaves_the_correct_outstanding_quantity(
            self, vendor, store, item):
        """Order 100, receive 60: 40 outstanding, PO partially_received, and the
        shelf holds exactly 60."""
        ordered = 100 * QUANTITY_SCALE
        _, po = _full_chain(vendor, store, item, qty_milli=ordered,
                            rate_paise=rupees("15.50"))

        svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 60 * QUANTITY_SCALE}],
        )

        po = svc.get_purchase_order(po["id"])
        assert po["status"] == "partially_received"
        line = po["lines"][0]
        assert line["received_quantity_milli"] == 60 * QUANTITY_SCALE
        assert line["outstanding_quantity_milli"] == 40 * QUANTITY_SCALE
        assert line["status"] == "partially_received"
        assert po["is_fully_received"] is False
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 60 * QUANTITY_SCALE

    def test_the_second_delivery_closes_the_order(self, vendor, store, item):
        ordered = 100 * QUANTITY_SCALE
        _, po = _full_chain(vendor, store, item, qty_milli=ordered,
                            rate_paise=rupees("15.50"))
        svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 60 * QUANTITY_SCALE}])
        svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 40 * QUANTITY_SCALE}])

        po = svc.get_purchase_order(po["id"])
        assert po["status"] == "received"
        assert po["lines"][0]["outstanding_quantity_milli"] == 0
        assert po["is_fully_received"] is True
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == ordered

    def test_over_receipt_is_refused_at_the_boundary(self, vendor, store, item):
        """Outstanding 40: receiving 40 is fine, 41 is not."""
        ordered = 100 * QUANTITY_SCALE
        _, po = _full_chain(vendor, store, item, qty_milli=ordered,
                            rate_paise=rupees("15.50"))
        svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 60 * QUANTITY_SCALE}])

        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_goods_receipt(
                purchase_order_id=po["id"],
                lines=[{"po_line_no": 1,
                        "accepted_quantity_milli": 40 * QUANTITY_SCALE + 1}])
        assert exc.value.code == "over_receipt"
        # …and nothing leaked into stock from the refused attempt.
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 60 * QUANTITY_SCALE

        ok = svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 40 * QUANTITY_SCALE}])
        assert ok["status"] == "posted"

    def test_rejected_quantity_consumes_the_order_but_not_the_shelf(
            self, vendor, store, item):
        """Rejected goods sit in the receiving bay awaiting return. They count
        against the order (the vendor sent them) and they are NOT stock."""
        ordered = 10 * QUANTITY_SCALE
        _, po = _full_chain(vendor, store, item, qty_milli=ordered,
                            rate_paise=rupees("100"))
        svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1,
                    "accepted_quantity_milli": 7 * QUANTITY_SCALE,
                    "rejected_quantity_milli": 3 * QUANTITY_SCALE,
                    "rejection_reason": "packaging torn"}],
        )
        po = svc.get_purchase_order(po["id"])
        assert po["lines"][0]["received_quantity_milli"] == 7 * QUANTITY_SCALE
        assert po["lines"][0]["outstanding_quantity_milli"] == 3 * QUANTITY_SCALE
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 7 * QUANTITY_SCALE

    def test_a_rejection_without_a_reason_is_refused(self, vendor, store, item):
        _, po = _full_chain(vendor, store, item, qty_milli=10 * QUANTITY_SCALE,
                            rate_paise=rupees("100"))
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_goods_receipt(
                purchase_order_id=po["id"],
                lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE,
                        "rejected_quantity_milli": QUANTITY_SCALE}])
        assert exc.value.code == "missing_field"


# ═════════════════════════════════════════════════════════════════════════════
#  4. STOCK ARITHMETIC — the identity everything else rests on
# ═════════════════════════════════════════════════════════════════════════════

class TestStockArithmetic:

    def test_on_hand_equals_opening_plus_receipts_minus_issues(
            self, vendor, store, item):
        opening = 25 * QUANTITY_SCALE
        received = 60 * QUANTITY_SCALE
        issued_a = 12 * QUANTITY_SCALE
        issued_b = 3 * QUANTITY_SCALE

        svc.record_stock_movement(
            item_code=item["code"], store_code=store["code"],
            movement_type="opening", quantity_milli=opening,
            unit_rate_paise=rupees("40"), remarks="stock take",
        )
        _, po = _full_chain(vendor, store, item, qty_milli=received,
                            rate_paise=rupees("40"))
        svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": received}])
        svc.issue_stock(item_code=item["code"], store_code=store["code"],
                        quantity_milli=issued_a, department="Surgery")
        svc.issue_stock(item_code=item["code"], store_code=store["code"],
                        quantity_milli=issued_b, department="Surgery")

        expected = opening + received - issued_a - issued_b
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == expected

    def test_total_equals_sum_of_parts(self, vendor, store, item):
        """THE INVARIANT. The reported balance is the sum of the movements that
        produced it — asserted as `total == sum(parts)` so that any future
        caching, denormalised balance column or per-batch bucket that drifts
        from the ledger fails here rather than on a shelf.

        Also asserted across two stores: the hospital-wide figure must be the
        sum of the per-store figures, not an independently computed number."""
        other_store = svc.upsert_store(code=f"{TAG}S{_uid()}", name="Second store",
                                       store_type="ward", ward="W1")
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=30 * QUANTITY_SCALE)
        svc.record_stock_movement(item_code=item["code"],
                                  store_code=other_store["code"],
                                  movement_type="opening",
                                  quantity_milli=11 * QUANTITY_SCALE)
        svc.issue_stock(item_code=item["code"], store_code=store["code"],
                        quantity_milli=4 * QUANTITY_SCALE, department="Surgery")

        parts = [m["quantity_milli"] for m in svc.stock_ledger(item["code"],
                                                               store_code=store["code"])]
        total = svc.stock_on_hand_milli(item["code"], store["code"])
        assert total == sum(parts), "on hand must be the sum of its own movements"

        hospital_wide = svc.stock_on_hand_milli(item["code"])
        per_store = (svc.stock_on_hand_milli(item["code"], store["code"])
                     + svc.stock_on_hand_milli(item["code"], other_store["code"]))
        assert hospital_wide == per_store, \
            "the total across stores must equal the sum of the per-store figures"

        # And the aggregated view agrees with the same identity.
        rows = svc.list_stock_on_hand(store_code=store["code"])
        row = next(r for r in rows if r["item_code"] == item["code"])
        assert row["on_hand_milli"] == total

    def test_the_running_balance_column_agrees_with_the_sum(self, store, item):
        """`balance_after_milli` is a convenience copy for reading a ledger. It
        must never be able to say something different from SUM(quantity_milli),
        because a ledger that disagrees with its own last line is unreadable."""
        for qty in (10, 5, 2):
            svc.record_stock_movement(
                item_code=item["code"], store_code=store["code"],
                movement_type="opening" if qty == 10 else "receipt",
                quantity_milli=qty * QUANTITY_SCALE)
        svc.issue_stock(item_code=item["code"], store_code=store["code"],
                        quantity_milli=6 * QUANTITY_SCALE, department="Surgery")
        movements = svc.stock_ledger(item["code"], store_code=store["code"])
        latest = movements[0]
        assert latest["balance_after_milli"] == svc.stock_on_hand_milli(
            item["code"], store["code"])

    def test_issuing_more_than_is_on_hand_is_refused(self, store, item):
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=5 * QUANTITY_SCALE)
        with pytest.raises(svc.ProcurementError) as exc:
            svc.issue_stock(item_code=item["code"], store_code=store["code"],
                            quantity_milli=6 * QUANTITY_SCALE, department="Surgery")
        assert exc.value.code == "insufficient_stock"
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 5 * QUANTITY_SCALE

    def test_two_stores_do_not_bleed_into_each_other(self, store, item):
        other = svc.upsert_store(code=f"{TAG}S{_uid()}", name="Third store",
                                 store_type="ot")
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=8 * QUANTITY_SCALE)
        assert svc.stock_on_hand_milli(item["code"], other["code"]) == 0
        with pytest.raises(svc.ProcurementError):
            svc.issue_stock(item_code=item["code"], store_code=other["code"],
                            quantity_milli=QUANTITY_SCALE)

    def test_a_float_quantity_is_refused(self, store, item):
        with pytest.raises(svc.ProcurementError) as exc:
            svc.record_stock_movement(item_code=item["code"],
                                      store_code=store["code"],
                                      movement_type="opening", quantity_milli=1.5)
        assert exc.value.code == "bad_quantity"


# ═════════════════════════════════════════════════════════════════════════════
#  5. REORDER POINT — fires AT the boundary, not near it
# ═════════════════════════════════════════════════════════════════════════════

class TestReorderBoundary:

    @pytest.mark.parametrize("on_hand_units,expected_low", [
        (9, True),    # below the level
        (10, True),   # AT the level — a reorder level IS the trigger point
        (11, False),  # one above: not low
    ])
    def test_low_stock_fires_at_the_level_not_near_it(
            self, store, on_hand_units, expected_low):
        item = _mk_item(reorder_level_qty=10, reorder_qty=50)
        svc.record_stock_movement(
            item_code=item["code"], store_code=store["code"],
            movement_type="opening",
            quantity_milli=on_hand_units * QUANTITY_SCALE)

        rows = svc.low_stock(store_code=store["code"])
        codes = {r["item_code"] for r in rows}
        assert (item["code"] in codes) is expected_low, (
            f"{on_hand_units} units against a reorder level of 10 should "
            f"{'' if expected_low else 'NOT '}be low"
        )

    def test_the_reorder_level_column_is_read_as_whole_units(self, store):
        """`reorder_level_qty` has no `_milli` suffix, so it is WHOLE UNITS.
        Comparing on-hand milli against it raw would read a level of 10 boxes as
        0.01 of a box, and the store would never trip its own alert."""
        item = _mk_item(reorder_level_qty=10)
        assert svc.get_inventory_item(item["code"])["reorder_level_milli"] == \
            10 * QUANTITY_SCALE

        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=5 * QUANTITY_SCALE)
        rows = svc.low_stock(store_code=store["code"])
        assert any(r["item_code"] == item["code"] for r in rows), (
            "5 units against a level of 10 units must be low; if this fails the "
            "level is being compared in the wrong scale"
        )

    def test_an_item_with_no_reorder_level_is_never_low(self, store):
        """Unconfigured is not zero, and it is not 'fine' either — it is simply
        not a reorder decision this system can make."""
        item = _mk_item(reorder_level_qty=None)
        rows = svc.low_stock(store_code=store["code"])
        assert item["code"] not in {r["item_code"] for r in rows}

    def test_an_item_that_has_run_to_nil_in_ONE_store_is_low_in_that_store(self, store):
        """THE ROW A NAIVE JOIN DROPS.

        Central holds 40; theatre holds none and never has. Filtered to theatre,
        the item must appear AT ZERO and LOW — "you have none of this" is the
        most actionable line a stores view can print, and it is precisely the
        line that disappears when the query joins through the movements table
        and there are no movements to join to. Before this was fixed, the
        theatre view simply omitted the item, which reads as "not our problem".
        """
        theatre = svc.upsert_store(code=f"{TAG}S{_uid()}", name="Theatre store",
                                   store_type="ot")
        item = _mk_item(reorder_level_qty=10)
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=40 * QUANTITY_SCALE)

        rows = svc.list_stock_on_hand(store_code=theatre["code"])
        entry = next((r for r in rows if r["item_code"] == item["code"]), None)
        assert entry is not None, "an item at nil in this store must still be listed"
        assert entry["store_code"] == theatre["code"]
        assert entry["on_hand_milli"] == 0
        assert entry["is_low"] is True

        # …and the store it IS in is unaffected.
        central = next(r for r in svc.list_stock_on_hand(store_code=store["code"])
                       if r["item_code"] == item["code"])
        assert central["on_hand_milli"] == 40 * QUANTITY_SCALE
        assert central["is_low"] is False

    def test_include_zero_false_suppresses_items_a_store_does_not_carry(self, store):
        """The other side of the same coin: a large catalogue would otherwise
        print every item a store has never held. The caller can turn it off."""
        theatre = svc.upsert_store(code=f"{TAG}S{_uid()}", name="Theatre store 2",
                                   store_type="ot")
        item = _mk_item(reorder_level_qty=10)
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=40 * QUANTITY_SCALE)
        rows = svc.list_stock_on_hand(store_code=theatre["code"], include_zero=False)
        assert item["code"] not in {r["item_code"] for r in rows}

    def test_a_never_stocked_item_at_its_level_is_low(self, store):
        """Zero on hand against a level of 5 is the most urgent case there is,
        and it is the one a naive JOIN drops because the item has no movements."""
        item = _mk_item(reorder_level_qty=5)
        rows = svc.low_stock()
        entry = next((r for r in rows if r["item_code"] == item["code"]), None)
        assert entry is not None, "an item with no stock at all must show as low"
        assert entry["on_hand_milli"] == 0
        assert entry["shortfall_milli"] == 5 * QUANTITY_SCALE


# ═════════════════════════════════════════════════════════════════════════════
#  6. EXPIRY — consumed lots drop out
# ═════════════════════════════════════════════════════════════════════════════

class TestExpiry:

    def _receive_batch(self, vendor, store, item, *, qty_milli, batch, expiry):
        _, po = _full_chain(vendor, store, item, qty_milli=qty_milli,
                            rate_paise=rupees("200"))
        return svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": qty_milli,
                    "batch_no": batch, "expiry_date": expiry}],
        )

    def test_expiry_view_excludes_a_fully_consumed_lot(self, vendor, store):
        """A lot that has been used up nets to zero and DROPS OUT. An expiry list
        that keeps showing lots somebody consumed months ago is a list
        storekeepers learn to ignore — and then they ignore the one that
        matters."""
        item = _mk_item(category="reagent", name="HbA1c reagent")
        soon = (date.today() + timedelta(days=20)).isoformat()
        keep = (date.today() + timedelta(days=45)).isoformat()

        self._receive_batch(vendor, store, item, qty_milli=4 * QUANTITY_SCALE,
                            batch="LOT-USED", expiry=soon)
        self._receive_batch(vendor, store, item, qty_milli=6 * QUANTITY_SCALE,
                            batch="LOT-KEPT", expiry=keep)

        before = {r["batch_no"] for r in svc.expiry_report(store_code=store["code"])}
        assert {"LOT-USED", "LOT-KEPT"} <= before

        svc.record_stock_movement(
            item_code=item["code"], store_code=store["code"],
            movement_type="consumption", quantity_milli=4 * QUANTITY_SCALE,
            batch_no="LOT-USED", expiry_date=soon)

        after = svc.expiry_report(store_code=store["code"])
        batches = {r["batch_no"] for r in after}
        assert "LOT-USED" not in batches, "a consumed lot must leave the expiry view"
        assert "LOT-KEPT" in batches
        kept = next(r for r in after if r["batch_no"] == "LOT-KEPT")
        assert kept["balance_milli"] == 6 * QUANTITY_SCALE

    def test_a_partly_consumed_lot_shows_its_remaining_balance(self, vendor, store):
        item = _mk_item(category="reagent")
        expiry = (date.today() + timedelta(days=30)).isoformat()
        self._receive_batch(vendor, store, item, qty_milli=10 * QUANTITY_SCALE,
                            batch="LOT-PART", expiry=expiry)
        svc.record_stock_movement(
            item_code=item["code"], store_code=store["code"],
            movement_type="consumption", quantity_milli=4 * QUANTITY_SCALE,
            batch_no="LOT-PART", expiry_date=expiry)
        row = next(r for r in svc.expiry_report(store_code=store["code"])
                   if r["batch_no"] == "LOT-PART")
        assert row["balance_milli"] == 6 * QUANTITY_SCALE
        assert row["days_to_expiry"] == 30
        assert row["is_expired"] is False

    def test_an_expiry_tracked_item_must_arrive_with_batch_and_expiry(
            self, vendor, store):
        item = _mk_item(category="reagent")
        _, po = _full_chain(vendor, store, item, qty_milli=QUANTITY_SCALE,
                            rate_paise=rupees("500"))
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_goods_receipt(
                purchase_order_id=po["id"],
                lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE}])
        assert exc.value.code == "expiry_required"

    def test_already_expired_stock_cannot_be_received(self, vendor, store):
        item = _mk_item(category="reagent")
        _, po = _full_chain(vendor, store, item, qty_milli=QUANTITY_SCALE,
                            rate_paise=rupees("500"))
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_goods_receipt(
                purchase_order_id=po["id"],
                lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE,
                        "batch_no": "OLD",
                        "expiry_date": (date.today() - timedelta(days=1)).isoformat()}])
        assert exc.value.code == "expired_on_receipt"

    def test_a_non_expiring_item_needs_no_batch(self, vendor, store):
        """Linen does not go off. Demanding an expiry date for it would train
        storekeepers to type a fake one, which poisons the dates that matter."""
        item = _mk_item(category="linen", name="Bedsheet")
        assert not svc.item_tracks_expiry(item)
        _, po = _full_chain(vendor, store, item, qty_milli=20 * QUANTITY_SCALE,
                            rate_paise=rupees("250"))
        grn = svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 20 * QUANTITY_SCALE}])
        assert grn["status"] == "posted"
        assert not any(r["item_code"] == item["code"]
                       for r in svc.expiry_report(store_code=store["code"]))


# ═════════════════════════════════════════════════════════════════════════════
#  7. CONSUMPTION BY DEPARTMENT
# ═════════════════════════════════════════════════════════════════════════════

class TestConsumptionReport:

    def test_consumption_is_grouped_by_the_store_department(self, store, item):
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=100 * QUANTITY_SCALE,
                                  unit_rate_paise=rupees("20"))
        svc.issue_stock(item_code=item["code"], store_code=store["code"],
                        quantity_milli=30 * QUANTITY_SCALE, department="Surgery")

        report = svc.consumption_report(store_code=store["code"])
        line = next(r for r in report["lines"] if r["item_code"] == item["code"])
        assert line["department"] == "Surgery"
        assert line["quantity_milli"] == 30 * QUANTITY_SCALE

    def test_transfers_and_write_offs_are_not_consumption(self, store, item):
        """A transfer between two hospital stores is not consumption — the
        hospital still has the goods. A write-off is a loss, not a use. Lumping
        them together is how a department gets billed for a flood."""
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=100 * QUANTITY_SCALE,
                                  unit_rate_paise=rupees("20"))
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="transfer_out",
                                  quantity_milli=10 * QUANTITY_SCALE)
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="write_off",
                                  quantity_milli=5 * QUANTITY_SCALE,
                                  remarks="water damage")
        svc.issue_stock(item_code=item["code"], store_code=store["code"],
                        quantity_milli=7 * QUANTITY_SCALE, department="Surgery")

        report = svc.consumption_report(store_code=store["code"])
        line = next(r for r in report["lines"] if r["item_code"] == item["code"])
        assert line["quantity_milli"] == 7 * QUANTITY_SCALE, \
            "only issue/consumption count; transfer_out and write_off must not"
        # …but they did leave the store.
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 78 * QUANTITY_SCALE

    def test_an_unpriced_issue_leaves_the_total_flagged_incomplete(self, store):
        """MONEY HONESTY. An item nobody priced contributes no value, and the
        report says the total is incomplete rather than presenting a partial
        figure as if it covered everything."""
        item = _mk_item()   # never received, so no purchase rate
        assert svc.get_inventory_item(item["code"])["rate_is_configured"] is False
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=10 * QUANTITY_SCALE)
        svc.issue_stock(item_code=item["code"], store_code=store["code"],
                        quantity_milli=2 * QUANTITY_SCALE, department="Surgery")

        report = svc.consumption_report(store_code=store["code"])
        line = next(r for r in report["lines"] if r["item_code"] == item["code"])
        assert line["unvalued_movement_count"] == 1
        assert line["value_display"] == "Partly unpriced"
        assert report["value_is_complete"] is False
        assert report["total_value_display"] == "Partly unpriced"


# ═════════════════════════════════════════════════════════════════════════════
#  8. MONEY
# ═════════════════════════════════════════════════════════════════════════════

class TestMoney:

    def test_an_unpriced_item_reads_not_configured_never_zero(self):
        item = _mk_item()
        assert item["last_purchase_rate_paise"] is None
        assert item["last_purchase_rate_display"] == "Not configured"

    def test_a_posted_receipt_sets_the_last_purchase_rate(self, vendor, store, item):
        _, po = _full_chain(vendor, store, item, qty_milli=4 * QUANTITY_SCALE,
                            rate_paise=rupees("137.50"))
        svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 4 * QUANTITY_SCALE}])
        refreshed = svc.get_inventory_item(item["code"])
        assert refreshed["rate_is_configured"] is True
        assert refreshed["last_purchase_rate_paise"] == 13750
        assert refreshed["last_purchase_rate_display"] == "₹137.50"

    def test_po_totals_are_the_sum_of_the_lines_plus_a_visible_round_off(
            self, vendor, store, item):
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=store["code"],
            lines=[
                {"item_code": item["code"], "quantity_milli": 3 * QUANTITY_SCALE,
                 "unit_rate_paise": rupees("33.33")},
                {"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                 "unit_rate_paise": rupees("10.10")},
            ],
        )
        line_total = sum(line["total_paise"] for line in po["lines"])
        assert po["total_paise"] == line_total + po["rounding_adjustment_paise"]
        assert po["total_paise"] % 100 == 0, \
            "the payable is a whole rupee and the difference is the Round Off line"
        assert (po["cgst_paise"] + po["sgst_paise"]
                == sum(l["cgst_paise"] + l["sgst_paise"] for l in po["lines"]))

    def test_intra_state_uses_cgst_sgst_and_inter_state_uses_igst(
            self, vendor, store, item):
        intra = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=store["code"], is_interstate=False,
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("1000")}])
        inter = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=store["code"], is_interstate=True,
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("1000")}])
        assert intra["igst_paise"] == 0 and intra["cgst_paise"] > 0
        assert inter["cgst_paise"] == 0 and inter["igst_paise"] > 0
        assert intra["tax_paise"] == inter["tax_paise"] == rupees("120")

    def test_an_exempt_item_attracts_no_tax_whatever_rate_is_attached(
            self, vendor, store):
        item = _mk_item(gst_treatment="exempt", gst_rate_bp=1800)
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("500")}])
        assert po["tax_paise"] == 0

    def test_a_full_bank_account_number_is_refused(self):
        with pytest.raises(svc.ProcurementError) as exc:
            svc.upsert_vendor(code=f"{TAG}V{_uid()}", name="Leaky Vendor",
                              account_last4="123456789012")
        assert exc.value.code == "bad_account_last4"


# ═════════════════════════════════════════════════════════════════════════════
#  9. HTTP surface — auth, module flag, error mapping
# ═════════════════════════════════════════════════════════════════════════════

class TestRoutes:

    def test_unauthenticated_is_refused(self):
        assert client.get("/api/procurement/overview").status_code in (401, 403)

    def test_a_nurse_cannot_read_the_stores_module(self, nurse_headers):
        """Not a permissions statement about nurses so much as proof the gate is
        actually wired: a role without procurement.read gets 403, not data."""
        assert client.get("/api/procurement/overview",
                          headers=nurse_headers).status_code == 403

    def test_admin_can_read_the_overview(self, admin_headers):
        response = client.get("/api/procurement/overview", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert "low_stock" in body and "expiring" in body
        assert "helix" in body["pharmacy_boundary_note"].lower()

    def test_meta_publishes_the_state_machines(self, admin_headers):
        body = client.get("/api/procurement/meta", headers=admin_headers).json()
        assert body["transitions"]["indent"]["rejected"] == []
        assert "posted" in body["transitions"]["goods_receipt"]["draft"]
        assert body["quantity_scale"] == QUANTITY_SCALE

    def test_a_pharmacist_cannot_approve_a_purchase_order(
            self, pharmacist_headers, vendor, store, item):
        """SEPARATION OF DUTIES: raising (`procurement.write`) and approving
        (`procurement.approve`) are different permissions, and the pharmacist
        holds only the first."""
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("10")}])
        svc.submit_purchase_order(po["id"])
        response = client.post(f"/api/procurement/purchase-orders/{po['id']}/approve",
                               headers=pharmacist_headers)
        assert response.status_code == 403

    def test_over_receipt_returns_409_with_a_machine_readable_code(
            self, admin_headers, vendor, store, item):
        _, po = _full_chain(vendor, store, item, qty_milli=2 * QUANTITY_SCALE,
                            rate_paise=rupees("10"))
        response = client.post(
            "/api/procurement/goods-receipts",
            headers=admin_headers,
            json={"purchase_order_id": po["id"],
                  "lines": [{"po_line_no": 1,
                             "accepted_quantity_milli": 3 * QUANTITY_SCALE}]},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "over_receipt"

    def test_posting_a_pharmacy_item_is_refused_with_422(self, admin_headers):
        response = client.post(
            "/api/procurement/items", headers=admin_headers,
            json={"code": f"{TAG}X{_uid()}", "name": "Morphine", "is_pharmacy": True},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "pharmacy_item_refused"

    def test_a_write_off_needs_approval_authority(self, pharmacist_headers, store, item):
        """The pharmacist clears the write-tier dependency (they hold
        procurement.write), so this pins the IN-BODY escalation: a movement that
        makes stock disappear without a receipt demands procurement.approve."""
        svc.record_stock_movement(item_code=item["code"], store_code=store["code"],
                                  movement_type="opening",
                                  quantity_milli=5 * QUANTITY_SCALE)
        response = client.post(
            "/api/procurement/stock/movements", headers=pharmacist_headers,
            json={"item_code": item["code"], "store_code": store["code"],
                  "movement_type": "write_off", "quantity_milli": QUANTITY_SCALE},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["required"] == ["procurement.approve"]
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 5 * QUANTITY_SCALE

    def test_the_module_flag_404s_every_route(self, admin_headers, monkeypatch):
        """A site that runs its own purchasing system sets ORB_PROCUREMENT=0. The
        routes must then answer 404 with an EXPLANATION, so a stale client that
        still has the tab gets a sentence rather than a blank screen."""
        monkeypatch.setattr(config, "PROCUREMENT_ENABLED", False)
        response = client.get("/api/procurement/overview", headers=admin_headers)
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert detail["error"] == "module_disabled"
        assert "ORB_PROCUREMENT" in detail["message"]

    def test_the_end_to_end_chain_over_http(self, admin_headers):
        """One pass through the whole thing the way a storekeeper would drive it,
        so the request/response shapes are proved, not just the service."""
        vendor_code = f"{TAG}V{_uid()}"
        store_code = f"{TAG}S{_uid()}"
        item_code = f"{TAG}I{_uid()}"

        assert client.post("/api/procurement/vendors", headers=admin_headers, json={
            "code": vendor_code, "name": "HTTP Vendor", "state_code": "27",
        }).status_code == 200
        assert client.post("/api/procurement/stores", headers=admin_headers, json={
            "code": store_code, "name": "HTTP Store", "store_type": "central",
            "department": "Central",
        }).status_code == 200
        assert client.post("/api/procurement/items", headers=admin_headers, json={
            "code": item_code, "name": "Suture 3-0", "category": "consumable",
            "uom": "box", "gst_rate_bp": 1200, "reorder_level_qty": 5,
        }).status_code == 200

        indent = client.post("/api/procurement/indents", headers=admin_headers, json={
            "store_code": store_code, "department": "Central", "submit": True,
            "lines": [{"item_code": item_code, "quantity_milli": 10 * QUANTITY_SCALE}],
        }).json()
        assert indent["status"] == "submitted"

        decided = client.post(f"/api/procurement/indents/{indent['id']}/decide",
                              headers=admin_headers, json={"approvals": {}}).json()
        assert decided["status"] == "approved"

        po = client.post("/api/procurement/purchase-orders", headers=admin_headers,
                         json={"vendor_code": vendor_code, "store_code": store_code,
                               "indent_id": indent["id"],
                               "lines": [{"item_code": item_code,
                                          "quantity_milli": 10 * QUANTITY_SCALE,
                                          "unit_rate_paise": 25000}]}).json()
        po_id = po["id"]
        client.post(f"/api/procurement/purchase-orders/{po_id}/submit",
                    headers=admin_headers)
        client.post(f"/api/procurement/purchase-orders/{po_id}/approve",
                    headers=admin_headers)
        issued = client.post(f"/api/procurement/purchase-orders/{po_id}/issue",
                             headers=admin_headers).json()
        assert issued["status"] == "issued" and issued["po_number"]

        grn = client.post("/api/procurement/goods-receipts", headers=admin_headers,
                          json={"purchase_order_id": po_id,
                                "lines": [{"po_line_no": 1,
                                           "accepted_quantity_milli": 6 * QUANTITY_SCALE}]}
                          ).json()
        assert grn["status"] == "posted"

        after = client.get(f"/api/procurement/purchase-orders/{po_id}",
                           headers=admin_headers).json()
        assert after["status"] == "partially_received"
        assert after["lines"][0]["outstanding_quantity_milli"] == 4 * QUANTITY_SCALE

        stock = client.get("/api/procurement/stock", headers=admin_headers,
                           params={"store_code": store_code}).json()["stock"]
        entry = next(s for s in stock if s["item_code"] == item_code)
        assert entry["on_hand_milli"] == 6 * QUANTITY_SCALE
        assert entry["is_low"] is False    # 6 units against a level of 5

        client.post("/api/procurement/stock/movements", headers=admin_headers,
                    json={"item_code": item_code, "store_code": store_code,
                          "movement_type": "issue",
                          "quantity_milli": 2 * QUANTITY_SCALE,
                          "department": "Theatre"})
        low = client.get("/api/procurement/reports/low-stock", headers=admin_headers,
                         params={"store_code": store_code}).json()
        assert any(r["item_code"] == item_code for r in low["low_stock"]), \
            "4 units against a reorder level of 5 must now be low"


# ═════════════════════════════════════════════════════════════════════════════
#  10. THE PERMISSION TIERS — read / write / approve, asserted from outside
# ═════════════════════════════════════════════════════════════════════════════

class TestPermissionTiers:
    """The 2026-08-18 three-way split, proved over HTTP.

    `procurement.read` sees the ledgers; `procurement.write` raises the day's
    indents and receipts; `procurement.approve` commits money and vendor
    standing. The grants under test: pharmacist holds read+write (their
    long-standing indent-raising reach, previously borrowed from helix.write)
    but NOT approve; doctor and nurse hold none of the three; admin holds all
    three. The write-off half of the approve tier is pinned separately in
    TestRoutes.test_a_write_off_needs_approval_authority, because that gate is
    an in-body escalation rather than a route dependency."""

    READ_SURFACES = [
        "/api/procurement/overview",
        "/api/procurement/items",
        "/api/procurement/indents",
        "/api/procurement/purchase-orders",
        "/api/procurement/stock",
        "/api/procurement/reports/low-stock",
    ]

    # ── Pharmacist: read and write, never approve ────────────────────────────

    def test_a_pharmacist_can_read_the_catalogue(self, pharmacist_headers):
        assert client.get("/api/procurement/items",
                          headers=pharmacist_headers).status_code == 200

    def test_a_pharmacist_can_raise_an_indent(self, pharmacist_headers, store, item):
        """The reach the split deliberately PRESERVES: raising an indent was the
        pharmacist's before (via helix.write) and stays theirs (via
        procurement.write)."""
        response = client.post(
            "/api/procurement/indents", headers=pharmacist_headers,
            json={"store_code": store["code"],
                  "lines": [{"item_code": item["code"],
                             "quantity_milli": QUANTITY_SCALE}]},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "draft"

    def test_a_pharmacist_cannot_decide_an_indent(
            self, pharmacist_headers, store, item):
        indent = svc.create_indent(
            store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE}],
            submit=True,
        )
        response = client.post(f"/api/procurement/indents/{indent['id']}/decide",
                               headers=pharmacist_headers, json={"approvals": {}})
        assert response.status_code == 403
        assert svc.get_indent(indent["id"])["status"] == "submitted", \
            "the refused decision must not have moved the document"

    def test_a_pharmacist_cannot_issue_a_purchase_order(
            self, pharmacist_headers, vendor, store, item):
        """Even a fully APPROVED order — the transition is legal, the actor is
        not. Issuing is the moment the commitment leaves the building."""
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("10")}])
        svc.submit_purchase_order(po["id"])
        svc.approve_purchase_order(po["id"], actor="approver")
        response = client.post(
            f"/api/procurement/purchase-orders/{po['id']}/issue",
            headers=pharmacist_headers)
        assert response.status_code == 403
        assert svc.get_purchase_order(po["id"])["status"] == "approved"

    def test_a_pharmacist_cannot_blacklist_a_vendor(
            self, pharmacist_headers, vendor):
        response = client.post(
            f"/api/procurement/vendors/{vendor['code']}/blacklist",
            headers=pharmacist_headers,
            json={"blacklisted": True, "reason": "short supply"})
        assert response.status_code == 403

    # ── Clinicians: no business in the stores at all ─────────────────────────

    @pytest.mark.parametrize("path", READ_SURFACES)
    def test_a_doctor_cannot_read_the_stores(self, doctor_headers, path):
        assert client.get(path, headers=doctor_headers).status_code == 403

    @pytest.mark.parametrize("path", READ_SURFACES)
    def test_a_nurse_cannot_read_the_stores(self, nurse_headers, path):
        assert client.get(path, headers=nurse_headers).status_code == 403

    def test_clinicians_cannot_raise_an_indent(
            self, doctor_headers, nurse_headers, store, item):
        payload = {"store_code": store["code"],
                   "lines": [{"item_code": item["code"],
                              "quantity_milli": QUANTITY_SCALE}]}
        for headers in (doctor_headers, nurse_headers):
            assert client.post("/api/procurement/indents", headers=headers,
                               json=payload).status_code == 403

    def test_clinicians_cannot_touch_the_vendor_master(
            self, doctor_headers, nurse_headers):
        for headers in (doctor_headers, nurse_headers):
            assert client.post(
                "/api/procurement/vendors", headers=headers,
                json={"code": f"{TAG}V{_uid()}", "name": "Should Not Exist"},
            ).status_code == 403

    # ── Admin: all three tiers ───────────────────────────────────────────────

    def test_an_admin_passes_all_three_tiers(self, admin_headers, store, item):
        # READ — the ledgers.
        assert client.get("/api/procurement/vendors",
                          headers=admin_headers).status_code == 200
        # WRITE — raise and submit an indent…
        raised = client.post(
            "/api/procurement/indents", headers=admin_headers,
            json={"store_code": store["code"], "submit": True,
                  "lines": [{"item_code": item["code"],
                             "quantity_milli": QUANTITY_SCALE}]})
        assert raised.status_code == 200
        # APPROVE — …and sign it.
        decided = client.post(
            f"/api/procurement/indents/{raised.json()['id']}/decide",
            headers=admin_headers, json={"approvals": {}})
        assert decided.status_code == 200
        assert decided.json()["status"] == "approved"
