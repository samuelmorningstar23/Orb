"""Procurement connection-taking cores (grn-20 part a, 3 October 2026).

Every procurement write is now a public wrapper (unchanged signature, same
session, same numbering, same answer) around `_<name>_core(db, ...)`, which runs
on a connection the CALLER owns. The pharmacy purchasing service in a later
wave builds on these cores so that a document and its audit row commit or roll
back together. This file pins the contract that service will rely on:

  1. EVERY WRITE HAS A CORE, and the core takes the connection first.
  2. A CORE NEVER OPENS A SESSION, never stamps a number and never re-reads
     through a public getter (each of those takes a session of its own, which
     would either commit early or deadlock inside an immediate transaction).
  3. A CORE NEVER READS THE SERVER CLOCK FOR A CALENDAR DATE. The expiry
     comparison and the default order date take `today` from the caller.
  4. ONE TRANSACTION: a whole chain run through cores inside one immediate
     session commits together, and a failure after the cores (standing in for
     a failed audit write) leaves no document, no stock and no number behind.
  5. THE WRAPPERS ARE UNCHANGED. test_procurement.py passes untouched; the
     few wrapper behaviours the split could have moved are pinned here too.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from datetime import date, datetime, timedelta

import pytest

from database import db_session
from services import procurement_service as svc
from services.money import QUANTITY_SCALE, rupees

TAG = "PRCC"

#: Every public write and the core it must delegate to.
WRITE_CORES = {
    "upsert_vendor": "_upsert_vendor_core",
    "set_vendor_blacklist": "_set_vendor_blacklist_core",
    "upsert_store": "_upsert_store_core",
    "upsert_inventory_item": "_upsert_inventory_item_core",
    "create_indent": "_create_indent_core",
    "submit_indent": "_submit_indent_core",
    "decide_indent": "_decide_indent_core",
    "cancel_indent": "_cancel_indent_core",
    "create_purchase_order": "_create_purchase_order_core",
    "submit_purchase_order": "_submit_purchase_order_core",
    "approve_purchase_order": "_approve_purchase_order_core",
    "issue_purchase_order": "_issue_purchase_order_core",
    "cancel_purchase_order": "_cancel_purchase_order_core",
    "create_goods_receipt": "_create_goods_receipt_core",
    "post_goods_receipt": "_post_goods_receipt_core",
    "cancel_goods_receipt": "_cancel_goods_receipt_core",
    "record_stock_movement": "_record_stock_movement_core",
    "issue_stock": "_issue_stock_core",
}

#: Connection-taking readers, so a caller can answer from inside its transaction.
READ_CORES = ("_get_indent_core", "_get_purchase_order_core", "_get_goods_receipt_core")

#: Calls a core must never make: each opens its own session or draws a number.
FORBIDDEN_IN_CORES = {
    "db_session", "_stamp_document_number", "next_document_number",
    "get_indent", "get_purchase_order", "get_goods_receipt", "get_vendor",
    "get_store", "get_inventory_item", "list_purchase_orders",
    # Public writes: a core calls the core, never the wrapper.
    *WRITE_CORES.keys(),
}


def _uid() -> str:
    return uuid.uuid4().hex[:8].upper()


def _all_cores() -> list[str]:
    return sorted(set(WRITE_CORES.values()) | set(READ_CORES) | {"_move_po_core"})


def _function_node(name: str) -> ast.FunctionDef:
    source = textwrap.dedent(inspect.getsource(getattr(svc, name)))
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if isinstance(call.func, ast.Name):
            names.add(call.func.id)
        elif isinstance(call.func, ast.Attribute):
            names.add(call.func.attr)
    return names


# ─── Fixtures: a store, a vendor, an item, an approved indent ────────────────

@pytest.fixture
def store() -> dict:
    return svc.upsert_store(code=f"{TAG}S{_uid()}", name="Cores test store",
                            store_type="sub_store", department="Surgery")


@pytest.fixture
def vendor() -> dict:
    return svc.upsert_vendor(code=f"{TAG}V{_uid()}", name="Cores test supplier",
                             state_code="27")


def _item(category: str = "consumable") -> dict:
    return svc.upsert_inventory_item(
        code=f"{TAG}I{_uid()}", name=f"Cores test {category} {_uid()}",
        category=category, uom="box", gst_treatment="taxable", gst_rate_bp=1200,
    )


@pytest.fixture
def item() -> dict:
    return _item()


@pytest.fixture
def reagent() -> dict:
    return _item("reagent")


def _issued_po(vendor: dict, store: dict, item: dict, qty_units: int = 10) -> dict:
    po = svc.create_purchase_order(
        vendor_code=vendor["code"], store_code=store["code"],
        lines=[{"item_code": item["code"], "quantity_milli": qty_units * QUANTITY_SCALE,
                "unit_rate_paise": rupees("40")}],
        created_by="buyer",
    )
    svc.submit_purchase_order(po["id"])
    svc.approve_purchase_order(po["id"], actor="approver")
    return svc.issue_purchase_order(po["id"], actor="approver")


def _count(sql: str, params: tuple) -> int:
    with db_session() as db:
        return int(db.execute(sql, params).fetchone()[0])


# ═════════════════════════════════════════════════════════════════════════════
#  1 and 2. Structure
# ═════════════════════════════════════════════════════════════════════════════

class TestEveryWriteHasACore:

    @pytest.mark.parametrize("public,core", sorted(WRITE_CORES.items()))
    def test_the_core_exists_and_takes_the_connection_first(self, public, core):
        assert hasattr(svc, public), f"{public} is still a public write"
        assert hasattr(svc, core), f"{public} has no connection-taking core {core}"
        params = list(inspect.signature(getattr(svc, core)).parameters)
        assert params[0] == "db", f"{core} must take the caller's connection first"

    @pytest.mark.parametrize("public,core", sorted(WRITE_CORES.items()))
    def test_the_wrapper_calls_its_core(self, public, core):
        """The wrapper delegates (directly, or through _move_po for the four PO
        transitions), so a rule added to a core reaches the stores route too."""
        called = _called_names(_function_node(public))
        if public in {"submit_purchase_order", "approve_purchase_order",
                      "issue_purchase_order", "cancel_purchase_order"}:
            assert "_move_po" in called
            assert "_move_po_core" in _called_names(_function_node("_move_po"))
            assert "_move_po_core" in _called_names(_function_node(core))
        else:
            assert core in called, f"{public} does not call {core}"

    @pytest.mark.parametrize("core", _all_cores())
    def test_a_core_never_opens_a_session_stamps_or_rereads(self, core):
        offenders = _called_names(_function_node(core)) & FORBIDDEN_IN_CORES
        assert offenders == set(), (
            f"{core} calls {sorted(offenders)}; a core runs on the caller's "
            "connection and must not open a session, draw a number or call a "
            "public function"
        )

    @pytest.mark.parametrize("core", _all_cores())
    def test_a_core_never_reads_the_server_clock_for_a_date(self, core):
        node = _function_node(core)
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "today"):
                pytest.fail(f"{core} reads the server clock for a calendar date; "
                            "take `today` from the caller instead")

    def test_the_cores_that_compare_dates_require_today(self):
        for core in ("_create_goods_receipt_core", "_create_purchase_order_core"):
            param = inspect.signature(getattr(svc, core)).parameters["today"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY
            assert param.default is inspect.Parameter.empty, \
                f"{core} must not default `today`: the caller picks the clock"


# ═════════════════════════════════════════════════════════════════════════════
#  3. `today` is the caller's
# ═════════════════════════════════════════════════════════════════════════════

class TestTodayIsAParameter:

    def test_calendar_today_accepts_a_date_a_datetime_and_an_iso_string(self):
        assert svc._calendar_today(date(2031, 5, 6)) == date(2031, 5, 6)
        assert svc._calendar_today(datetime(2031, 5, 6, 23, 59)) == date(2031, 5, 6)
        assert svc._calendar_today("2031-05-06") == date(2031, 5, 6)

    @pytest.mark.parametrize("bad", [None, 20310506, 3.5])
    def test_calendar_today_refuses_anything_else(self, bad):
        with pytest.raises(TypeError):
            svc._calendar_today(bad)

    def test_a_malformed_string_fails_loudly(self):
        with pytest.raises(ValueError):
            svc._calendar_today("06/05/2031")

    def test_expiry_is_judged_against_the_callers_today_not_the_server_clock(
            self, vendor, store, reagent):
        po = _issued_po(vendor, store, reagent)
        expiry = (date.today() + timedelta(days=30)).isoformat()
        line = {"po_line_no": 1, "accepted_quantity_milli": 1 * QUANTITY_SCALE,
                "batch_no": "B-LATE", "expiry_date": expiry}

        # In date by the server clock, but the caller's today is a year on.
        with pytest.raises(svc.ProcurementError) as exc:
            with db_session(immediate=True) as db:
                svc._create_goods_receipt_core(
                    db, purchase_order_id=po["id"], lines=[line],
                    today=date.today() + timedelta(days=365))
        assert exc.value.code == "expired_on_receipt"

        # The same line against the caller's real today is received.
        with db_session(immediate=True) as db:
            grn_id = svc._create_goods_receipt_core(
                db, purchase_order_id=po["id"], lines=[line], today=date.today())
        assert svc.get_goods_receipt(grn_id)["status"] == "draft"

    def test_a_past_today_accepts_what_the_server_clock_would_refuse(
            self, vendor, store, reagent):
        """Proof the core does not consult date.today() behind the caller."""
        po = _issued_po(vendor, store, reagent)
        long_gone = (date.today() - timedelta(days=400)).isoformat()
        with db_session(immediate=True) as db:
            grn_id = svc._create_goods_receipt_core(
                db, purchase_order_id=po["id"],
                lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE,
                        "batch_no": "B-OLD", "expiry_date": long_gone}],
                today=(date.today() - timedelta(days=500)).isoformat())
        assert svc.get_goods_receipt(grn_id)["lines"][0]["expiry_date"] == long_gone

    def test_the_stores_wrapper_still_refuses_expired_stock_on_its_own_clock(
            self, vendor, store, reagent):
        po = _issued_po(vendor, store, reagent)
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_goods_receipt(
                purchase_order_id=po["id"],
                lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE,
                        "batch_no": "B-X",
                        "expiry_date": (date.today() - timedelta(days=1)).isoformat()}])
        assert exc.value.code == "expired_on_receipt"

    def test_the_po_order_date_defaults_to_the_callers_today(self, vendor, store, item):
        with db_session(immediate=True) as db:
            po_id = svc._create_purchase_order_core(
                db, vendor_code=vendor["code"], store_code=store["code"],
                lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                        "unit_rate_paise": rupees("5")}],
                today=date(2031, 1, 2))
        assert svc.get_purchase_order(po_id)["order_date"] == "2031-01-02"

    def test_the_stores_wrapper_still_dates_a_po_today(self, vendor, store, item):
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("5")}])
        assert po["order_date"] == date.today().isoformat()

    def test_a_core_called_without_today_is_a_programming_error(self, vendor, store, item):
        with pytest.raises(TypeError):
            with db_session(immediate=True) as db:
                svc._create_purchase_order_core(  # type: ignore[call-arg]
                    db, vendor_code=vendor["code"],
                    lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                            "unit_rate_paise": rupees("5")}])


# ═════════════════════════════════════════════════════════════════════════════
#  4. One transaction
# ═════════════════════════════════════════════════════════════════════════════

class _AuditOutage(RuntimeError):
    """Stands in for a fail-closed audit write that fails after the cores ran."""


class TestOneTransaction:

    def _chain_in_one_transaction(self, db, vendor, store, item, remark: str) -> dict:
        indent_id = svc._create_indent_core(
            db, store_code=store["code"], department="Surgery", submit=True,
            lines=[{"item_code": item["code"], "quantity_milli": 4 * QUANTITY_SCALE}],
            remarks=remark)
        svc._decide_indent_core(db, indent_id, actor="approver")
        po_id = svc._create_purchase_order_core(
            db, vendor_code=vendor["code"], store_code=store["code"],
            indent_id=indent_id, remarks=remark, today=date.today(),
            lines=[{"item_code": item["code"], "quantity_milli": 4 * QUANTITY_SCALE,
                    "unit_rate_paise": rupees("25")}])
        svc._submit_purchase_order_core(db, po_id)
        svc._approve_purchase_order_core(db, po_id, actor="approver")
        svc._issue_purchase_order_core(db, po_id, actor="approver")
        grn_id = svc._create_goods_receipt_core(
            db, purchase_order_id=po_id, remarks=remark, today=date.today(),
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 4 * QUANTITY_SCALE}])
        posted = svc._post_goods_receipt_core(db, grn_id, actor="storekeeper")
        return {"indent_id": indent_id, "po_id": po_id, "grn_id": grn_id,
                "posted": posted,
                "po_in_txn": svc._get_purchase_order_core(db, po_id),
                "grn_in_txn": svc._get_goods_receipt_core(db, grn_id)}

    def test_a_whole_chain_commits_in_one_immediate_transaction(self, vendor, store, item):
        with db_session(immediate=True) as db:
            out = self._chain_in_one_transaction(db, vendor, store, item, f"{TAG} commit")

        # Readers on the caller's connection saw the uncommitted state.
        assert out["po_in_txn"]["status"] == "received"
        assert out["grn_in_txn"]["status"] == "posted"
        assert [ln["line_no"] for ln in out["posted"]] == [1]

        assert svc.get_indent(out["indent_id"])["status"] == "converted"
        assert svc.get_purchase_order(out["po_id"])["status"] == "received"
        assert svc.get_goods_receipt(out["grn_id"])["status"] == "posted"
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 4 * QUANTITY_SCALE

    def test_cores_draw_no_number_the_caller_stamps_after_commit(self, vendor, store, item):
        with db_session(immediate=True) as db:
            out = self._chain_in_one_transaction(db, vendor, store, item, f"{TAG} numbers")
        assert svc.get_indent(out["indent_id"])["indent_number"] is None
        assert svc.get_purchase_order(out["po_id"])["po_number"] is None
        assert svc.get_goods_receipt(out["grn_id"])["grn_number"] is None

        assert svc._stamp_document_number("goods_receipt", out["grn_id"])
        assert svc.get_goods_receipt(out["grn_id"])["grn_number"]

    def test_a_failure_after_the_cores_rolls_every_write_back(self, vendor, store, item):
        remark = f"{TAG} rollback {_uid()}"
        with pytest.raises(_AuditOutage):
            with db_session(immediate=True) as db:
                self._chain_in_one_transaction(db, vendor, store, item, remark)
                raise _AuditOutage("audit write failed")

        assert _count("SELECT COUNT(*) FROM indents WHERE remarks = ?", (remark,)) == 0
        assert _count("SELECT COUNT(*) FROM purchase_orders WHERE remarks = ?", (remark,)) == 0
        assert _count("SELECT COUNT(*) FROM goods_receipts WHERE remarks = ?", (remark,)) == 0
        assert _count("SELECT COUNT(*) FROM stock_movements WHERE item_code = ?",
                      (item["code"],)) == 0
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 0

    def test_a_rolled_back_post_leaves_the_po_receivable(self, vendor, store, item):
        po = _issued_po(vendor, store, item)
        draft = svc.create_goods_receipt(
            purchase_order_id=po["id"], post=False,
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 3 * QUANTITY_SCALE}])
        with pytest.raises(_AuditOutage):
            with db_session(immediate=True) as db:
                svc._post_goods_receipt_core(db, draft["id"], actor="storekeeper")
                raise _AuditOutage("audit write failed")
        assert svc.get_goods_receipt(draft["id"])["status"] == "draft"
        after = svc.get_purchase_order(po["id"])
        assert after["status"] == "issued"
        assert after["outstanding_quantity_milli"] == 10 * QUANTITY_SCALE
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 0

    def test_stock_movement_and_issue_cores_share_the_callers_transaction(
            self, store, item):
        with db_session(immediate=True) as db:
            svc._record_stock_movement_core(
                db, item_code=item["code"], store_code=store["code"],
                movement_type="opening", quantity_milli=5 * QUANTITY_SCALE)
            issued = svc._issue_stock_core(
                db, item_code=item["code"], store_code=store["code"],
                quantity_milli=2 * QUANTITY_SCALE, department="Theatre")
            # The issue saw the opening balance written a statement earlier.
            assert issued["balance_after_milli"] == 3 * QUANTITY_SCALE
            assert issued["remarks"] == "Theatre"
        assert svc.stock_on_hand_milli(item["code"], store["code"]) == 3 * QUANTITY_SCALE

    def test_master_cores_roll_back_with_the_callers_transaction(self):
        code = f"{TAG}V{_uid()}"
        store_code = f"{TAG}S{_uid()}"
        with pytest.raises(_AuditOutage):
            with db_session(immediate=True) as db:
                row = svc._upsert_vendor_core(db, code=code, name="Rolled back vendor")
                assert row["code"] == code
                svc._upsert_store_core(db, code=store_code, name="Rolled back store")
                raise _AuditOutage("audit write failed")
        assert svc.get_vendor(code) is None
        assert svc.get_store(store_code) is None


# ═════════════════════════════════════════════════════════════════════════════
#  5. Wrapper behaviour the split could have moved
# ═════════════════════════════════════════════════════════════════════════════

class TestWrappersUnchanged:

    def test_a_malformed_retry_is_refused_before_the_idempotent_replay(
            self, vendor, store, item):
        po = _issued_po(vendor, store, item)
        key = f"{TAG}-replay-{_uid()}"
        first = svc.create_goods_receipt(
            purchase_order_id=po["id"], idempotency_key=key,
            lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE}])
        replay = svc.create_goods_receipt(
            purchase_order_id=po["id"], idempotency_key=key,
            lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE}])
        assert replay["id"] == first["id"] and replay["idempotent_replay"] is True

        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_goods_receipt(purchase_order_id=po["id"], idempotency_key=key,
                                     lines=[])
        assert exc.value.code == "empty_document"
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_goods_receipt(purchase_order_id=po["id"], idempotency_key=key,
                                     qc_status="lost",
                                     lines=[{"po_line_no": 1,
                                             "accepted_quantity_milli": QUANTITY_SCALE}])
        assert exc.value.code == "bad_qc_status"

    def test_the_idempotency_lookup_answers_the_id_or_none(self, vendor, store, item):
        po = _issued_po(vendor, store, item)
        key = f"{TAG}-key-{_uid()}"
        with db_session() as db:
            assert svc._goods_receipt_id_for_key(db, key) is None
        grn = svc.create_goods_receipt(
            purchase_order_id=po["id"], idempotency_key=key,
            lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE}])
        with db_session() as db:
            assert svc._goods_receipt_id_for_key(db, key) == grn["id"]

    def test_a_rejection_answers_with_the_rejected_document(self, store, item):
        indent = svc.create_indent(
            store_code=store["code"], submit=True,
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE}])
        out = svc.decide_indent(indent["id"], reject=True, reason="Not stocked here",
                                actor="approver")
        assert out["status"] == "rejected"
        assert out["rejected_reason"] == "Not stocked here"
        assert all(ln["approved_quantity_milli"] == 0 for ln in out["lines"])

    def test_the_wrappers_still_number_what_they_always_numbered(self, vendor, store, item):
        indent = svc.create_indent(
            store_code=store["code"], submit=True,
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE}])
        assert indent["indent_number"]
        po = _issued_po(vendor, store, item)
        assert po["po_number"]
        grn = svc.create_goods_receipt(
            purchase_order_id=po["id"],
            lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE}])
        assert grn["status"] == "posted" and grn["grn_number"]

    def test_a_drug_is_still_refused_at_the_item_core(self):
        with pytest.raises(svc.PharmacyItemRefused):
            with db_session() as db:
                svc._upsert_inventory_item_core(
                    db, code=f"{TAG}D{_uid()}", name="Anything", is_pharmacy=True)
