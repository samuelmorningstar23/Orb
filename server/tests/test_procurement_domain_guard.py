"""Procurement domain guards (grn-20 part b, docs/his_design/grn.json).

Migration 54 lets the pharmacy store reuse the procurement document tables
through a `domain` column, and partitions the store master by
`stores.pharmacy_kind`. These tests pin that the two desks cannot reach each
other's documents or stores, whatever the caller's tier:

  * every stores endpoint that takes an id answers 404 for a pharmacy-domain
    id, and leaves the document exactly as it was;
  * lists, reports and the overview show stores documents and stores only;
  * a stores receipt cannot be raised against a pharmacy order (a receipt's
    domain comes from its order);
  * no stock moves into, out of or through a pharmacy store on the stores
    ledger, and the stores desk cannot rename, deactivate or take over a
    pharmacy store;
  * the pharmacy branch of a receipt post writes no stock_movements row and
    touches no inventory_items row;
  * each desk numbers from its own series;
  * a site-timezone "today" passed as an aware datetime is the hospital's date.

The pharmacy documents here are made through the cores with domain='pharmacy'
and a stand-in drug resolver, which is how the pharmacy purchasing service in a
later wave will call them. Nothing here needs ORB_PHARMACY_STORE: the guards are
data-driven and hold whatever the switches say.
"""
from __future__ import annotations

import inspect
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import procurement_service as svc
from services.money import QUANTITY_SCALE, rupees

client = TestClient(app)
TAG = "PDG"
BASE = "/api/procurement"


def _uid() -> str:
    return uuid.uuid4().hex[:8].upper()


class _Rollback(Exception):
    """Raised inside a db_session to throw away everything the test wrote."""


# ═════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═════════════════════════════════════════════════════════════════════════════

def _resolve_drug(db, item_code: str):
    """A stand-in for the pharmacy catalogue the purchasing service will pass:
    any code starting DRUG is a drug with a configured GST rate and pack."""
    if not item_code.startswith("DRUG"):
        return None
    return {"code": item_code, "name": f"Drug {item_code}", "uom": "strip",
            "hsn_sac": "3004", "gst_treatment": "taxable", "gst_rate_bp": 1200,
            "helix_medication_id": f"HMX-{item_code}", "units_per_pack": 10}


@pytest.fixture(scope="module")
def admin_headers() -> dict:
    uid = f"{TAG}U{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?,?,?,'x')",
                   (uid, "Domain guard admin", "admin"))
    return {"Authorization": f"Bearer {create_token(uid, 'admin')}"}


@pytest.fixture
def vendor() -> dict:
    return svc.upsert_vendor(code=f"{TAG}V{_uid()}", name="Domain guard supplier",
                             state_code="27")


@pytest.fixture
def stores_store() -> dict:
    return svc.upsert_store(code=f"{TAG}S{_uid()}", name="Domain guard stores store",
                            store_type="sub_store", department="Surgery")


def _make_pharmacy_store(kind: str = "sub_store", **kw) -> dict:
    with db_session(immediate=True) as db:
        return svc._upsert_store_core(
            db, code=kw.pop("code", f"{TAG}P{_uid()}"), name=kw.pop("name", "Pharmacy sub-store"),
            domain="pharmacy", pharmacy_kind=kind, **kw)


@pytest.fixture
def pharmacy_store() -> dict:
    return _make_pharmacy_store()


@pytest.fixture
def item() -> dict:
    return svc.upsert_inventory_item(code=f"{TAG}I{_uid()}", name=f"Domain guard gauze {_uid()}",
                                     category="consumable", uom="box",
                                     gst_treatment="taxable", gst_rate_bp=1200)


def _pharmacy_po(vendor: dict, store: dict, *, issue: bool = True, units: int = 10) -> int:
    with db_session(immediate=True) as db:
        po_id = svc._create_purchase_order_core(
            db, vendor_code=vendor["code"], store_code=store["code"], domain="pharmacy",
            resolve_line=_resolve_drug, today=date.today(), created_by="buyer",
            lines=[{"item_code": f"DRUG{_uid()}", "quantity_milli": units * QUANTITY_SCALE,
                    "unit_rate_paise": rupees("12")}])
        if issue:
            svc._submit_purchase_order_core(db, po_id, domain="pharmacy")
            svc._approve_purchase_order_core(db, po_id, actor="approver", domain="pharmacy")
            svc._issue_purchase_order_core(db, po_id, actor="approver", domain="pharmacy")
    return po_id


def _pharmacy_grn(po_id: int, **line) -> int:
    expiry = (date.today() + timedelta(days=400)).isoformat()
    body = {"po_line_no": 1, "accepted_quantity_milli": 4 * QUANTITY_SCALE,
            "batch_no": "PB-1", "expiry_date": expiry, **line}
    with db_session(immediate=True) as db:
        return svc._create_goods_receipt_core(
            db, purchase_order_id=po_id, lines=[body], today=date.today(), domain="pharmacy")


def _pharmacy_indent(store: dict, *, submit: bool = False) -> int:
    with db_session(immediate=True) as db:
        return svc._create_indent_core(
            db, store_code=store["code"], domain="pharmacy", resolve_line=_resolve_drug,
            submit=submit, lines=[{"item_code": f"DRUG{_uid()}", "quantity_milli": QUANTITY_SCALE}])


def _row(sql: str, params: tuple):
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def _count(sql: str, params: tuple = ()) -> int:
    with db_session() as db:
        return int(db.execute(sql, params).fetchone()[0])


# ═════════════════════════════════════════════════════════════════════════════
#  Every stores endpoint that takes an id: a pharmacy id is a 404
# ═════════════════════════════════════════════════════════════════════════════

class TestStoresRoutesCannotReachPharmacyDocuments:

    @pytest.mark.parametrize("method,path,body", [
        ("get", "/indents/{id}", None),
        ("post", "/indents/{id}/submit", None),
        ("post", "/indents/{id}/decide", {"reject": True, "reason": "Not ours"}),
        ("post", "/indents/{id}/cancel", {"reason": "Not ours"}),
    ])
    def test_indent_endpoints(self, admin_headers, pharmacy_store, method, path, body):
        indent_id = _pharmacy_indent(pharmacy_store, submit=path.endswith("decide"))
        before = _row("SELECT status, indent_number FROM indents WHERE id = ?", (indent_id,))
        resp = getattr(client, method)(f"{BASE}{path.format(id=indent_id)}",
                                       headers=admin_headers, **({"json": body} if body else {}))
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "not_found"
        assert _row("SELECT status, indent_number FROM indents WHERE id = ?", (indent_id,)) == before

    @pytest.mark.parametrize("method,path,body,status", [
        ("get", "/purchase-orders/{id}", None, "issued"),
        ("post", "/purchase-orders/{id}/submit", None, "draft"),
        ("post", "/purchase-orders/{id}/approve", None, "pending_approval"),
        ("post", "/purchase-orders/{id}/issue", None, "approved"),
        ("post", "/purchase-orders/{id}/cancel", {"reason": "Not ours"}, "draft"),
    ])
    def test_purchase_order_endpoints(self, admin_headers, vendor, pharmacy_store,
                                      method, path, body, status):
        """Each transition is tried from the status where it would be legal, so
        the 404 can only come from the domain guard."""
        po_id = _pharmacy_po(vendor, pharmacy_store, issue=False)
        steps = {"draft": [], "pending_approval": ["pending_approval"],
                 "approved": ["pending_approval", "approved"],
                 "issued": ["pending_approval", "approved", "issued"]}[status]
        with db_session(immediate=True) as db:
            for target in steps:
                svc._move_po_core(db, po_id, target, actor="approver", domain="pharmacy")
        resp = getattr(client, method)(f"{BASE}{path.format(id=po_id)}",
                                       headers=admin_headers, **({"json": body} if body else {}))
        assert resp.status_code == 404, resp.text
        assert _row("SELECT status, po_number FROM purchase_orders WHERE id = ?",
                    (po_id,)) == {"status": status, "po_number": None}

    def test_a_stores_receipt_against_a_pharmacy_order_is_refused(
            self, admin_headers, vendor, pharmacy_store):
        po_id = _pharmacy_po(vendor, pharmacy_store)
        receipts = _count("SELECT COUNT(*) FROM goods_receipts")
        movements = _count("SELECT COUNT(*) FROM stock_movements")
        resp = client.post(f"{BASE}/goods-receipts", headers=admin_headers, json={
            "purchase_order_id": po_id,
            "lines": [{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE}]})
        assert resp.status_code == 404, resp.text
        assert _count("SELECT COUNT(*) FROM goods_receipts") == receipts
        assert _count("SELECT COUNT(*) FROM stock_movements") == movements
        assert _row("SELECT status FROM purchase_orders WHERE id = ?", (po_id,))["status"] == "issued"

    @pytest.mark.parametrize("method,path,body", [
        ("get", "/goods-receipts/{id}", None),
        ("post", "/goods-receipts/{id}/post", None),
        ("post", "/goods-receipts/{id}/cancel", {"reason": "Not ours"}),
    ])
    def test_goods_receipt_endpoints(self, admin_headers, vendor, pharmacy_store,
                                     method, path, body):
        grn_id = _pharmacy_grn(_pharmacy_po(vendor, pharmacy_store))
        resp = getattr(client, method)(f"{BASE}{path.format(id=grn_id)}",
                                       headers=admin_headers, **({"json": body} if body else {}))
        assert resp.status_code == 404, resp.text
        assert _row("SELECT status, grn_number FROM goods_receipts WHERE id = ?",
                    (grn_id,)) == {"status": "draft", "grn_number": None}

    def test_the_lists_and_the_overview_show_stores_documents_only(
            self, admin_headers, vendor, pharmacy_store):
        overview_before = client.get(f"{BASE}/overview", headers=admin_headers).json()
        indent_id = _pharmacy_indent(pharmacy_store, submit=True)
        draft_po = _pharmacy_po(vendor, pharmacy_store, issue=False)
        grn_id = _pharmacy_grn(_pharmacy_po(vendor, pharmacy_store))
        ids = lambda path, key: {d["id"] for d in client.get(  # noqa: E731
            f"{BASE}{path}", headers=admin_headers, params={"limit": 500}).json()[key]}
        assert indent_id not in ids("/indents", "indents")
        assert draft_po not in ids("/purchase-orders", "purchase_orders")
        assert grn_id not in ids("/goods-receipts", "goods_receipts")
        overview = client.get(f"{BASE}/overview", headers=admin_headers).json()
        assert overview["pending_indent_count"] == overview_before["pending_indent_count"]
        assert overview["draft_purchase_order_count"] == overview_before["draft_purchase_order_count"]
        assert all(p["domain"] == "stores" for p in overview["open_purchase_orders"])
        # The pharmacy desk sees them through the same functions.
        assert indent_id in {d["id"] for d in svc.list_indents(domain="pharmacy", limit=500)}
        assert draft_po in {d["id"] for d in svc.list_purchase_orders(domain="pharmacy", limit=500)}
        assert grn_id in {d["id"] for d in svc.list_goods_receipts(domain="pharmacy", limit=500)}


# ═════════════════════════════════════════════════════════════════════════════
#  The cores refuse the other desk's documents, both ways
# ═════════════════════════════════════════════════════════════════════════════

class TestCoresRefuseTheOtherDesk:

    def test_stores_cores_refuse_pharmacy_ids_and_pharmacy_cores_refuse_stores_ids(
            self, vendor, pharmacy_store, stores_store, item):
        ph_po = _pharmacy_po(vendor, pharmacy_store, issue=False)
        ph_indent = _pharmacy_indent(pharmacy_store)
        ph_grn = _pharmacy_grn(_pharmacy_po(vendor, pharmacy_store))
        st_po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=stores_store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("3")}])["id"]
        st_indent = svc.create_indent(store_code=stores_store["code"],
                                      lines=[{"item_code": item["code"],
                                              "quantity_milli": QUANTITY_SCALE}])["id"]

        calls = [
            lambda db, d, i: svc._submit_indent_core(db, i, domain=d),
            lambda db, d, i: svc._decide_indent_core(db, i, reject=True, reason="x", domain=d),
            lambda db, d, i: svc._cancel_indent_core(db, i, domain=d),
        ]
        po_calls = [
            lambda db, d, i: svc._submit_purchase_order_core(db, i, domain=d),
            lambda db, d, i: svc._approve_purchase_order_core(db, i, domain=d),
            lambda db, d, i: svc._issue_purchase_order_core(db, i, domain=d),
            lambda db, d, i: svc._cancel_purchase_order_core(db, i, reason="x", domain=d),
        ]
        for domain, indent_id, po_id in (("stores", ph_indent, ph_po),
                                         ("pharmacy", st_indent, st_po)):
            for call, doc_id in [(c, indent_id) for c in calls] + [(c, po_id) for c in po_calls]:
                with pytest.raises(svc.ProcurementError) as exc:
                    with db_session(immediate=True) as db:
                        call(db, domain, doc_id)
                assert exc.value.code == "not_found"
        with db_session() as db:
            assert svc._get_indent_core(db, ph_indent) is None
            assert svc._get_purchase_order_core(db, ph_po) is None
            assert svc._get_goods_receipt_core(db, ph_grn) is None
            assert svc._get_indent_core(db, st_indent, domain="pharmacy") is None
            assert svc._get_purchase_order_core(db, st_po, domain="pharmacy") is None
            assert svc._get_indent_core(db, ph_indent, domain="pharmacy")["domain"] == "pharmacy"
        for core in (svc._post_goods_receipt_core,):
            with pytest.raises(svc.ProcurementError) as exc:
                with db_session(immediate=True) as db:
                    core(db, ph_grn)
            assert exc.value.code == "not_found"
        with pytest.raises(svc.ProcurementError) as exc:
            with db_session(immediate=True) as db:
                svc._cancel_goods_receipt_core(db, ph_grn, reason="x")
        assert exc.value.code == "not_found"

    def test_the_refusal_reads_exactly_like_a_missing_document(self, vendor, pharmacy_store):
        """The stores desk learns nothing from the wording: a pharmacy order and
        an order that does not exist are refused in the same words."""
        po_id = _pharmacy_po(vendor, pharmacy_store, issue=False)
        with pytest.raises(svc.ProcurementError) as exc:
            svc.submit_purchase_order(po_id)
        assert str(exc.value) == f"no purchase order {po_id}"
        assert svc.get_purchase_order(po_id) is None

    def test_an_idempotency_key_of_the_other_desk_is_refused_not_replayed(
            self, vendor, pharmacy_store):
        po_id = _pharmacy_po(vendor, pharmacy_store)
        key = f"{TAG}-key-{_uid()}"
        with db_session(immediate=True) as db:
            svc._create_goods_receipt_core(
                db, purchase_order_id=po_id, today=date.today(), domain="pharmacy",
                idempotency_key=key,
                lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE,
                        "batch_no": "K1", "expiry_date": "2031-01-01"}])
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_goods_receipt(purchase_order_id=po_id, idempotency_key=key,
                                     lines=[{"po_line_no": 1,
                                             "accepted_quantity_milli": QUANTITY_SCALE}])
        assert exc.value.code == "already_exists"
        with db_session() as db:
            assert svc._goods_receipt_id_for_key(db, key, domain="pharmacy") is not None

    def test_every_id_taking_core_takes_a_domain_that_defaults_to_stores(self):
        for name in ("_submit_indent_core", "_decide_indent_core", "_cancel_indent_core",
                     "_get_indent_core", "_move_po_core", "_submit_purchase_order_core",
                     "_approve_purchase_order_core", "_issue_purchase_order_core",
                     "_cancel_purchase_order_core", "_get_purchase_order_core",
                     "_create_goods_receipt_core", "_post_goods_receipt_core",
                     "_cancel_goods_receipt_core", "_get_goods_receipt_core",
                     "_goods_receipt_id_for_key", "_create_indent_core",
                     "_create_purchase_order_core", "_upsert_store_core"):
            param = inspect.signature(getattr(svc, name)).parameters["domain"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert param.default == "stores", name

    def test_an_unknown_domain_is_refused(self, vendor, pharmacy_store):
        with pytest.raises(svc.ProcurementError) as exc:
            svc.list_purchase_orders(domain="kitchen")
        assert exc.value.code == "unknown_domain"


# ═════════════════════════════════════════════════════════════════════════════
#  The pharmacy branch of the cores
# ═════════════════════════════════════════════════════════════════════════════

class TestPharmacyDocuments:

    def test_a_pharmacy_order_resolves_drugs_through_the_caller_and_snapshots_the_pack(
            self, vendor, pharmacy_store):
        po_id = _pharmacy_po(vendor, pharmacy_store, issue=False)
        with db_session() as db:
            po = svc._get_purchase_order_core(db, po_id, domain="pharmacy")
        assert po["domain"] == "pharmacy" and po["store_code"] == pharmacy_store["code"]
        line = po["lines"][0]
        assert line["inventory_item_id"] is None
        assert line["helix_medication_id"] == f"HMX-{line['item_code']}"
        assert line["units_per_pack"] == 10 and line["gst_rate_bp"] == 1200

    @pytest.mark.parametrize("resolver,code,expected", [
        (None, "DRUG1", "pharmacy_lines_unresolved"),
        (_resolve_drug, "GAUZE", "unknown_item"),
        (lambda db, c: {**_resolve_drug(db, c), "gst_rate_bp": None}, "DRUG1",
         "gst_not_configured"),
        (lambda db, c: {**_resolve_drug(db, c), "units_per_pack": 0}, "DRUG1", "bad_pack_size"),
    ])
    def test_a_pharmacy_order_line_is_refused_when_the_drug_is_not_right(
            self, vendor, pharmacy_store, resolver, code, expected):
        with pytest.raises(svc.ProcurementError) as exc:
            with db_session(immediate=True) as db:
                svc._create_purchase_order_core(
                    db, vendor_code=vendor["code"], store_code=pharmacy_store["code"],
                    domain="pharmacy", resolve_line=resolver, today=date.today(),
                    lines=[{"item_code": code, "quantity_milli": QUANTITY_SCALE,
                            "unit_rate_paise": rupees("1")}])
        assert exc.value.code == expected

    def test_a_pharmacy_order_needs_a_pharmacy_store_and_a_stores_order_a_stores_store(
            self, vendor, pharmacy_store, stores_store, item):
        with pytest.raises(svc.ProcurementError) as exc:
            with db_session(immediate=True) as db:
                svc._create_purchase_order_core(
                    db, vendor_code=vendor["code"], store_code=stores_store["code"],
                    domain="pharmacy", resolve_line=_resolve_drug, today=date.today(),
                    lines=[{"item_code": "DRUG1", "quantity_milli": QUANTITY_SCALE,
                            "unit_rate_paise": rupees("1")}])
        assert exc.value.code == "not_found"
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_purchase_order(
                vendor_code=vendor["code"], store_code=pharmacy_store["code"],
                lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                        "unit_rate_paise": rupees("1")}])
        assert exc.value.code == "not_found"
        with pytest.raises(svc.ProcurementError) as exc:
            svc.create_indent(store_code=pharmacy_store["code"],
                              lines=[{"item_code": item["code"], "quantity_milli": 1000}])
        assert exc.value.code == "not_found"

    def test_a_pharmacy_receipt_needs_a_batch_and_an_expiry(self, vendor, pharmacy_store):
        po_id = _pharmacy_po(vendor, pharmacy_store)
        for missing in ({"batch_no": None}, {"expiry_date": None}):
            with pytest.raises(svc.ProcurementError) as exc:
                _pharmacy_grn(po_id, **missing)
            assert exc.value.code == "expiry_required"

    def test_a_pharmacy_receipt_takes_its_domain_from_the_order_and_keeps_its_drug_facts(
            self, vendor, pharmacy_store):
        po_id = _pharmacy_po(vendor, pharmacy_store)
        grn_id = _pharmacy_grn(po_id, free_quantity_milli=2 * QUANTITY_SCALE,
                               mrp_pack_paise=rupees("150"), mfg_date="2026-01-01")
        with db_session() as db:
            grn = svc._get_goods_receipt_core(db, grn_id, domain="pharmacy")
        assert grn["domain"] == "pharmacy" and grn["store_code"] == pharmacy_store["code"]
        line = grn["lines"][0]
        assert (line["free_quantity_milli"], line["mrp_pack_paise"], line["mfg_date"]) == (
            2 * QUANTITY_SCALE, rupees("150"), "2026-01-01")
        assert line["helix_medication_id"].startswith("HMX-") and line["units_per_pack"] == 10
        assert line["helix_lot_ref"] is None  # the stock service links the lot later

    def test_a_manufacturing_date_after_the_expiry_is_refused(self, vendor, pharmacy_store):
        po_id = _pharmacy_po(vendor, pharmacy_store)
        with pytest.raises(svc.ProcurementError) as exc:
            _pharmacy_grn(po_id, mfg_date="2040-01-01")
        assert exc.value.code == "bad_date"

    def test_a_stores_receipt_refuses_the_pharmacy_only_fields(self, vendor, stores_store, item):
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=stores_store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": 5 * QUANTITY_SCALE,
                    "unit_rate_paise": rupees("4")}])
        svc.submit_purchase_order(po["id"])
        svc.approve_purchase_order(po["id"], actor="approver")
        svc.issue_purchase_order(po["id"], actor="approver")
        for extra in ({"free_quantity_milli": 1000}, {"mrp_pack_paise": 100},
                      {"mfg_date": "2026-01-01"}):
            with pytest.raises(svc.ProcurementError) as exc:
                svc.create_goods_receipt(
                    purchase_order_id=po["id"],
                    lines=[{"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE, **extra}])
            assert exc.value.code == "bad_line"

    def test_the_pharmacy_post_writes_no_stores_stock_and_still_advances_the_order(
            self, vendor, pharmacy_store):
        po_id = _pharmacy_po(vendor, pharmacy_store, units=10)
        grn_id = _pharmacy_grn(po_id)
        movements = _count("SELECT COUNT(*) FROM stock_movements")
        items = _count("SELECT COUNT(*) FROM inventory_items")
        rates = _count("SELECT COALESCE(SUM(last_purchase_rate_paise), 0) FROM inventory_items")
        with db_session(immediate=True) as db:
            posted = svc._post_goods_receipt_core(db, grn_id, actor="storekeeper",
                                                  domain="pharmacy")
        assert _count("SELECT COUNT(*) FROM stock_movements") == movements
        assert _count("SELECT COUNT(*) FROM stock_movements WHERE reference_id = ?",
                      (str(grn_id),)) == 0
        assert _count("SELECT COUNT(*) FROM inventory_items") == items
        assert _count("SELECT COALESCE(SUM(last_purchase_rate_paise), 0) "
                      "FROM inventory_items") == rates
        assert [ln["helix_medication_id"] for ln in posted][0].startswith("HMX-")
        with db_session() as db:
            assert svc._get_goods_receipt_core(db, grn_id, domain="pharmacy")["status"] == "posted"
            po = svc._get_purchase_order_core(db, po_id, domain="pharmacy")
        assert po["status"] == "partially_received"
        assert po["lines"][0]["received_quantity_milli"] == 4 * QUANTITY_SCALE

    def test_an_issue_indent_is_a_pharmacy_document_only(self, stores_store, item,
                                                         pharmacy_store):
        with pytest.raises(svc.ProcurementError) as exc:
            with db_session(immediate=True) as db:
                svc._create_indent_core(db, store_code=stores_store["code"],
                                        indent_kind="issue",
                                        lines=[{"item_code": item["code"],
                                                "quantity_milli": 1000}])
        assert exc.value.code == "bad_indent_kind"
        other = _make_pharmacy_store()
        with db_session(immediate=True) as db:
            indent_id = svc._create_indent_core(
                db, store_code=pharmacy_store["code"], domain="pharmacy",
                indent_kind="issue", issuing_store_code=other["code"],
                resolve_line=_resolve_drug,
                lines=[{"item_code": "DRUG9", "quantity_milli": 1000}])
            row = db.execute("SELECT domain, indent_kind, issuing_store_id FROM indents "
                             "WHERE id = ?", (indent_id,)).fetchone()
        assert tuple(row) == ("pharmacy", "issue", other["id"])


# ═════════════════════════════════════════════════════════════════════════════
#  The store master is partitioned
# ═════════════════════════════════════════════════════════════════════════════

class TestStoreMasterPartition:

    def test_the_stores_desk_cannot_see_a_pharmacy_store(self, admin_headers, pharmacy_store,
                                                        stores_store):
        codes = {s["code"] for s in svc.list_stores(active_only=False)}
        assert pharmacy_store["code"] not in codes and stores_store["code"] in codes
        assert pharmacy_store["code"] not in {
            s["code"] for s in client.get(f"{BASE}/stores", headers=admin_headers,
                                          params={"active_only": False}).json()["stores"]}
        assert svc.get_store(pharmacy_store["code"]) is None
        assert svc.get_store(int(pharmacy_store["id"])) is None
        pharmacy_codes = {s["code"] for s in svc.list_stores(domain="pharmacy", active_only=False)}
        assert pharmacy_store["code"] in pharmacy_codes
        assert stores_store["code"] not in pharmacy_codes
        assert svc.get_store(pharmacy_store["code"], domain="pharmacy")["pharmacy_kind"] == "sub_store"

    @pytest.mark.parametrize("change", [
        {"name": "Renamed by stores"}, {"is_active": False}, {"store_type": "central"},
    ])
    def test_the_stores_desk_cannot_rename_or_deactivate_a_pharmacy_store(
            self, admin_headers, pharmacy_store, change):
        before = _row("SELECT * FROM stores WHERE id = ?", (pharmacy_store["id"],))
        body = {"code": pharmacy_store["code"], "name": pharmacy_store["name"],
                "store_type": pharmacy_store["store_type"], **change}
        resp = client.post(f"{BASE}/stores", headers=admin_headers, json=body)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "already_exists"
        with pytest.raises(svc.ProcurementError):
            svc.upsert_store(**body)
        assert _row("SELECT * FROM stores WHERE id = ?", (pharmacy_store["id"],)) == before

    def test_the_pharmacy_desk_cannot_take_over_a_stores_store(self, stores_store):
        with pytest.raises(svc.ProcurementError) as exc:
            _make_pharmacy_store(code=stores_store["code"], name="Taken over")
        assert exc.value.code == "already_exists"
        assert svc.get_store(stores_store["code"])["name"] == stores_store["name"]

    def test_a_pharmacy_store_must_name_a_known_kind_and_a_stores_store_none(self):
        with pytest.raises(svc.ProcurementError) as exc:
            _make_pharmacy_store(kind="shop")
        assert exc.value.code == "bad_store_kind"
        with pytest.raises(svc.ProcurementError) as exc:
            _make_pharmacy_store(kind=None)
        assert exc.value.code == "bad_store_kind"
        with pytest.raises(svc.ProcurementError) as exc:
            with db_session(immediate=True) as db:
                svc._upsert_store_core(db, code=f"{TAG}X{_uid()}", name="X",
                                       pharmacy_kind="dispensing")
        assert exc.value.code == "bad_store_kind"

    def test_a_pharmacy_store_keeps_its_kind(self, pharmacy_store):
        with pytest.raises(svc.ProcurementError) as exc:
            _make_pharmacy_store(kind="dispensing", code=pharmacy_store["code"])
        assert exc.value.code == "store_kind_fixed"
        updated = _make_pharmacy_store(kind="sub_store", code=pharmacy_store["code"],
                                       name="Renamed by pharmacy")
        assert updated["name"] == "Renamed by pharmacy" and updated["pharmacy_kind"] == "sub_store"

    @pytest.mark.parametrize("kind", ["ward_imprest", "crash_cart"])
    def test_ward_stores_are_refused_while_ward_stock_is_off(self, kind, monkeypatch):
        """GRN-RS5 (held for Samuel): no drug is held on a ward until the site
        has ruled which ones may be. The strict default refuses the store."""
        assert config.feature_enabled("pharmacy_ward_stock") is False
        with pytest.raises(svc.ProcurementError) as exc:
            _make_pharmacy_store(kind=kind)
        assert exc.value.code == "ward_stock_off"
        monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
        monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", True)
        assert _make_pharmacy_store(kind=kind, department="Cardiology")["pharmacy_kind"] == kind

    def test_one_active_dispensing_store(self):
        """Run in a transaction that is rolled back, so the shared test
        database keeps no dispensing store for other tests to collide with."""
        with pytest.raises(_Rollback):
            with db_session(immediate=True) as db:
                svc._upsert_store_core(db, code=f"{TAG}D{_uid()}", name="Dispensary one",
                                       domain="pharmacy", pharmacy_kind="dispensing")
                with pytest.raises(svc.ProcurementError) as exc:
                    svc._upsert_store_core(db, code=f"{TAG}D{_uid()}", name="Dispensary two",
                                           domain="pharmacy", pharmacy_kind="dispensing")
                assert exc.value.code == "dispensing_store_exists"
                raise _Rollback()


# ═════════════════════════════════════════════════════════════════════════════
#  No stock moves through a pharmacy store on the stores ledger
# ═════════════════════════════════════════════════════════════════════════════

class TestStoresLedgerExcludesPharmacyStores:

    def test_a_movement_into_a_pharmacy_store_is_refused(self, admin_headers, pharmacy_store,
                                                         item):
        before = _count("SELECT COUNT(*) FROM stock_movements")
        resp = client.post(f"{BASE}/stock/movements", headers=admin_headers, json={
            "item_code": item["code"], "store_code": pharmacy_store["code"],
            "movement_type": "opening", "quantity_milli": 5 * QUANTITY_SCALE})
        assert resp.status_code == 404, resp.text
        for call in (
            lambda: svc.record_stock_movement(item_code=item["code"],
                                              store_code=pharmacy_store["code"],
                                              movement_type="receipt",
                                              quantity_milli=QUANTITY_SCALE),
            lambda: svc.issue_stock(item_code=item["code"], store_code=pharmacy_store["code"],
                                    quantity_milli=QUANTITY_SCALE, department="Ward 3"),
        ):
            with pytest.raises(svc.ProcurementError) as exc:
                call()
            assert exc.value.code == "not_found"
        assert _count("SELECT COUNT(*) FROM stock_movements") == before

    def test_the_stores_reports_show_no_pharmacy_store(self, pharmacy_store, stores_store, item):
        svc.record_stock_movement(item_code=item["code"], store_code=stores_store["code"],
                                  movement_type="opening", quantity_milli=3 * QUANTITY_SCALE)
        assert svc.stock_on_hand_milli(item["code"], pharmacy_store["code"]) == 0
        assert svc.list_stock_on_hand(store_code=pharmacy_store["code"]) == []
        assert svc.stock_ledger(item["code"], store_code=pharmacy_store["code"]) == []
        assert svc.stock_on_hand_milli(item["code"], stores_store["code"]) == 3 * QUANTITY_SCALE
        everywhere = {r["store_code"] for r in svc.list_stock_on_hand(limit=5000)}
        assert pharmacy_store["code"] not in everywhere


# ═════════════════════════════════════════════════════════════════════════════
#  Numbering: each desk from its own series
# ═════════════════════════════════════════════════════════════════════════════

class TestDocumentSeries:

    def test_every_pharmacy_kind_names_a_series_migration_54_seeded(self):
        pharmacy = {kind: spec for kind, spec in svc._DOC_SERIES.items()
                    if kind.startswith("pharmacy_")}
        assert {spec[2] for spec in pharmacy.values()} == {
            "PHPO", "PHGRN", "PHIND", "PHTRF", "PHVR", "PHRET", "PHCNT"}
        with db_session() as db:
            seeded = {r[0] for r in db.execute(
                "SELECT series_code FROM document_number_series WHERE series_code LIKE 'PH%'")}
            for kind, (table, column, series, _domain) in pharmacy.items():
                assert series in seeded, kind
                cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                assert column in cols, (kind, table, column)

    def _next(self, series: str) -> int:
        return _count("SELECT next_number FROM document_number_series WHERE series_code = ?",
                      (series,))

    def test_a_pharmacy_order_takes_a_pharmacy_number_and_never_a_stores_one(
            self, vendor, pharmacy_store):
        po_id = _pharmacy_po(vendor, pharmacy_store)
        po_next = self._next("PO")
        assert svc._stamp_document_number("purchase_order", po_id) is None
        assert self._next("PO") == po_next, "a stores number was drawn for a pharmacy order"
        number = svc._stamp_document_number("pharmacy_purchase_order", po_id)
        assert number and number.startswith("PHPO-")
        assert _row("SELECT po_number FROM purchase_orders WHERE id = ?",
                    (po_id,))["po_number"] == number

    def test_a_stores_order_never_takes_a_pharmacy_number(self, vendor, stores_store, item):
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=stores_store["code"],
            lines=[{"item_code": item["code"], "quantity_milli": QUANTITY_SCALE,
                    "unit_rate_paise": rupees("2")}])
        ph_next = self._next("PHPO")
        assert svc._stamp_document_number("pharmacy_purchase_order", po["id"]) is None
        assert self._next("PHPO") == ph_next


# ═════════════════════════════════════════════════════════════════════════════
#  The shared vendor core carries the drug licence
# ═════════════════════════════════════════════════════════════════════════════

class TestVendorCore:

    def test_licence_fields_are_written_and_a_stores_update_keeps_them(self):
        code = f"{TAG}L{_uid()}"
        with db_session(immediate=True) as db:
            row = svc._upsert_vendor_core(
                db, code=code, name="Licensed distributor", vendor_type="distributor",
                drug_licence_numbers="20B-123, 21B-456", drug_licence_valid_to="2027-03-31",
                ndps_licence_number="NDPS-77")
        assert (row["drug_licence_numbers"], row["drug_licence_valid_to"],
                row["ndps_licence_number"]) == ("20B-123, 21B-456", "2027-03-31", "NDPS-77")
        again = svc.upsert_vendor(code=code, name="Licensed distributor (renamed)",
                                  vendor_type="distributor")
        assert again["drug_licence_valid_to"] == "2027-03-31"
        assert again["ndps_licence_number"] == "NDPS-77"

    def test_a_bad_licence_date_is_refused(self):
        with pytest.raises(svc.ProcurementError) as exc:
            with db_session(immediate=True) as db:
                svc._upsert_vendor_core(db, code=f"{TAG}L{_uid()}", name="X",
                                        drug_licence_valid_to="31/03/2027")
        assert exc.value.code == "bad_date"


# ═════════════════════════════════════════════════════════════════════════════
#  An aware "today" is the hospital's date (wave 1 review finding)
# ═════════════════════════════════════════════════════════════════════════════

class TestSiteToday:

    @pytest.fixture
    def ist(self, monkeypatch):
        from services import hospital_time
        monkeypatch.setattr(hospital_time, "hospital_timezone",
                            lambda: ZoneInfo("Asia/Kolkata"))

    def test_an_aware_utc_instant_is_cut_to_the_hospital_date(self, ist):
        # 20:00 UTC is 01:30 the next morning in an IST hospital.
        assert svc._calendar_today(datetime(2031, 5, 6, 20, 0, tzinfo=timezone.utc)) == date(2031, 5, 7)
        # 18:00 UTC is 23:30 the same evening.
        assert svc._calendar_today(datetime(2031, 5, 6, 18, 0, tzinfo=timezone.utc)) == date(2031, 5, 6)
        # An instant already in IST keeps its own date.
        assert svc._calendar_today(
            datetime(2031, 5, 7, 1, 30, tzinfo=ZoneInfo("Asia/Kolkata"))) == date(2031, 5, 7)
        # A naive datetime is a wall-clock reading and is cut as before.
        assert svc._calendar_today(datetime(2031, 5, 6, 23, 59)) == date(2031, 5, 6)

    def test_a_lot_that_expired_yesterday_on_the_ward_is_refused_at_8pm_utc(
            self, ist, vendor, stores_store):
        reagent = svc.upsert_inventory_item(code=f"{TAG}R{_uid()}", name=f"Reagent {_uid()}",
                                            category="reagent", uom="kit")
        po = svc.create_purchase_order(
            vendor_code=vendor["code"], store_code=stores_store["code"],
            lines=[{"item_code": reagent["code"], "quantity_milli": 2 * QUANTITY_SCALE,
                    "unit_rate_paise": rupees("9")}])
        svc.submit_purchase_order(po["id"])
        svc.approve_purchase_order(po["id"], actor="approver")
        svc.issue_purchase_order(po["id"], actor="approver")
        line = {"po_line_no": 1, "accepted_quantity_milli": QUANTITY_SCALE,
                "batch_no": "R-1", "expiry_date": "2031-05-06"}
        # 20:00 UTC on 6 May: still the 6th in UTC, already the 7th on the ward.
        with pytest.raises(svc.ProcurementError) as exc:
            with db_session(immediate=True) as db:
                svc._create_goods_receipt_core(
                    db, purchase_order_id=po["id"], lines=[line],
                    today=datetime(2031, 5, 6, 20, 0, tzinfo=timezone.utc))
        assert exc.value.code == "expired_on_receipt"
        # 18:00 UTC is 23:30 on the 6th on the ward: in date, received.
        with db_session(immediate=True) as db:
            grn_id = svc._create_goods_receipt_core(
                db, purchase_order_id=po["id"], lines=[line],
                today=datetime(2031, 5, 6, 18, 0, tzinfo=timezone.utc))
        assert svc.get_goods_receipt(grn_id)["status"] == "draft"
