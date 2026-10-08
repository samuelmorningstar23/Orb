"""Pharmacy goods receipt (grn-8): /api/pharmacy-store/goods-receipts, behind
ORB_PHARMACY_STORE.

Pinned (docs/his_design/grn.json grn-8 tests; rulings GRN-RS6 (b), GRN-R12,
GRN-R14, GRN-R16, GRN-R10, GRN-R18):
  * with the module off every receipt route answers 404 module_disabled;
  * batch, MRP and an expiry typed twice are mandatory; a mismatched second
    expiry is refused and nothing is saved;
  * over-receipt is refused, at draft and again at post (two drafts that
    together exceed the order);
  * expired stock is refused; short-dated stock posts only with the approve
    tier, someone other than the receiver, and a reason; both judged on the
    hospital's date, not the server's;
  * a replayed Idempotency-Key drafts once, and posting twice moves stock once;
  * post puts the lines on the shelf in the receipt's store, status
    available, through the one stock writer; the same batch merges into one
    lot; free units carry no cost and are spread into the unit cost;
    reconcile() is clean; the order's status is derived; the PHGRN number is
    stamped; no inventory_items or stock_movements row appears;
  * a failure at any step rolls back lots, movements, order status and audit
    together;
  * a batch already on file with another expiry is refused;
  * an NDPS drug outside the vault is refused, and a narcotic received into
    the vault is dispensable through Helix and counted by pre-flight;
  * a draft can be cancelled with a reason; a posted receipt cannot.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token, hash_password
from database import db_session
from main import app
from services import audit_service, helix_service
from services import pharmacy_purchasing_service as purchasing
from services import pharmacy_stock_service as stock
from services import procurement_service as procurement
from services.helix_db import init_helix_db
from services.money import QUANTITY_SCALE

client = TestClient(app)
BASE = "/api/pharmacy-store"


def _today() -> date:
    return date.fromisoformat(stock.site_today())


def _in(days: int) -> str:
    return (_today() + timedelta(days=days)).isoformat()


LICENCE_TO = (date.today() + timedelta(days=900)).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture(autouse=True)
def module_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)


def _user(role, department=None):
    uid = f"UGR{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, department, hash_password("secretpass")))
    return {"id": uid, "role": role,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


@pytest.fixture
def pharmacist():
    return _user("pharmacist")


@pytest.fixture
def admin():
    return _user("admin")


def _drug(**fields):
    med = f"MEDGR{_uid()}"
    cols = {"id": med, "name": f"Receipttest {med[-6:]} 10 mg", "generic_name": f"grnmol{med[-6:]}",
            "form": "tablet", "strength": "10 mg", "drug_schedule": "H",
            "ndps_category": "none", "pack_size_units": 10, "gst_rate_bp": 1200,
            "purchase_uom": "strip", "hsn_code": "3004"} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    return med


def _vendor(ndps=None):
    code = f"VGR{_uid()}"
    procurement.upsert_vendor(code=code, name=f"Receipt supplier {code}", state_code="27")
    with db_session() as db:
        db.execute("UPDATE vendors SET drug_licence_numbers = 'DL-20B-77', "
                   "drug_licence_valid_to = ?, ndps_licence_number = ? WHERE code = ?",
                   (LICENCE_TO, ndps, code))
    return code


@pytest.fixture
def stores_made():
    made: list[int] = []
    yield made
    with db_session() as db:
        for store_id in made:
            db.execute("UPDATE stores SET is_active = 0 WHERE id = ?", (store_id,))


def _store(made, kind="sub_store") -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(db, code=f"GR{_uid()}", name=f"Receipt store {kind}",
                                             domain="pharmacy", pharmacy_kind=kind)
    made.append(row["id"])
    return row


def _issued_po(pharmacist, admin, made, *, drug=None, packs=10, rate=4500, store=None,
               vendor=None, discount_bp=0):
    """A pharmacy order sent to the vendor, raised by the pharmacist and
    approved and issued by the administrator."""
    drug = drug or _drug()
    store = store or _store(made)
    vendor = vendor or _vendor()
    doc = purchasing.create_purchase_order(
        vendor_code=vendor, store_code=store["code"], actor=pharmacist,
        lines=[{"helix_medication_id": drug, "packs": packs, "unit_rate_paise": rate,
                "discount_bp": discount_bp}])["purchase_order"]
    purchasing.submit_purchase_order(doc["id"], actor=pharmacist)
    purchasing.approve_purchase_order(doc["id"], actor=admin)
    purchasing.issue_purchase_order(doc["id"], actor=admin)
    return doc["id"], drug, store, vendor


def _line(po_line_no=1, packs=6, expiry=None, **extra):
    expiry = expiry or _in(400)
    return {"po_line_no": po_line_no, "packs": packs, "batch_no": "BX101",
            "expiry_date": expiry, "expiry_date_confirm": expiry, "mrp_pack_paise": 6000} | extra


def _draft(user, po_id, lines, *, key=None, **extra):
    body = {"purchase_order_id": po_id, "lines": lines, "vendor_invoice_number": "INV-1",
            "vendor_invoice_date": _today().isoformat()} | extra
    headers = dict(user["hdr"]) | ({"Idempotency-Key": key} if key else {})
    return client.post(f"{BASE}/goods-receipts", headers=headers, json=body)


def _drafted(user, po_id, lines, **extra) -> dict:
    resp = _draft(user, po_id, lines, **extra)
    assert resp.status_code == 200, resp.text
    return resp.json()["goods_receipt"]


def _post(user, grn_id, **body):
    return client.post(f"{BASE}/goods-receipts/{grn_id}/post", headers=user["hdr"], json=body)


def _lots_for(grn_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT l.* FROM helix_inventory_lots l WHERE l.id IN (SELECT lot_id FROM "
            "helix_stock_movements WHERE reference_type = 'pharmacy_grn' AND reference_id = ?)",
            (str(grn_id),)).fetchall()]


def _movements_for(grn_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM helix_stock_movements WHERE reference_type = 'pharmacy_grn' "
            "AND reference_id = ? ORDER BY seq", (str(grn_id),)).fetchall()]


def _po(po_id):
    with db_session() as db:
        doc = procurement._get_purchase_order_core(db, po_id, domain="pharmacy")
    return doc


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(target_id))).fetchall()]


def _counts():
    with db_session() as db:
        return (db.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0])


# ═════════════════════════════════════════════════════════════════════════════
#  The switch and the tiers
# ═════════════════════════════════════════════════════════════════════════════

def test_every_receipt_route_is_404_with_the_module_off(monkeypatch, pharmacist):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    for method, path in (("get", "/goods-receipts"), ("get", "/goods-receipts/receivable"),
                         ("get", "/goods-receipts/1"), ("post", "/goods-receipts"),
                         ("post", "/goods-receipts/1/post"),
                         ("post", "/goods-receipts/1/cancel")):
        resp = getattr(client, method)(f"{BASE}{path}", headers=pharmacist["hdr"],
                                       **({} if method == "get" else {"json": {}}))
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"


def test_the_tiers_hold(pharmacist, admin, stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made)
    doctor = _user("doctor", "Cardiology")
    assert client.get(f"{BASE}/goods-receipts", headers=doctor["hdr"]).status_code == 403
    assert _draft(admin, po_id, [_line()]).status_code == 403, "approve does not receive"
    doc = _drafted(pharmacist, po_id, [_line()])
    resp = _post(admin, doc["id"])
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "write_needed"
    assert _post(doctor, doc["id"]).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
#  What a receipt line needs
# ═════════════════════════════════════════════════════════════════════════════

def test_a_mismatched_second_expiry_is_refused_and_nothing_saved(pharmacist, admin, stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made)
    resp = _draft(pharmacist, po_id, [_line(expiry_date_confirm=_in(401))])
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "expiry_mismatch"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM goods_receipts WHERE purchase_order_id = ?",
                          (po_id,)).fetchone()[0] == 0
    for missing, code in (("batch_no", "missing_field"), ("mrp_pack_paise", "missing_field"),
                          ("expiry_date_confirm", "missing_field")):
        resp = _draft(pharmacist, po_id, [_line() | {missing: None}])
        assert resp.status_code == 422 and resp.json()["detail"]["error"] == code, missing
    assert _draft(pharmacist, po_id, [_line()], vendor_invoice_number="").status_code == 422


def test_over_receipt_is_refused_at_draft_and_at_post(pharmacist, admin, stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made, packs=10)
    resp = _draft(pharmacist, po_id, [_line(packs=11)])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "over_receipt"
    resp = _draft(pharmacist, po_id, [_line(packs=6), _line(packs=5)])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "over_receipt"
    first = _drafted(pharmacist, po_id, [_line(packs=6)])
    second = _drafted(pharmacist, po_id, [_line(packs=6)])
    assert _post(pharmacist, first["id"]).status_code == 200
    resp = _post(pharmacist, second["id"])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "over_receipt"
    assert _po(po_id)["lines"][0]["received_quantity_milli"] == 6 * QUANTITY_SCALE
    assert _movements_for(second["id"]) == []


def test_expired_and_short_dated_stock_on_the_hospital_date(monkeypatch, pharmacist, admin,
                                                           stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made, packs=20)
    resp = _draft(pharmacist, po_id, [_line(expiry=_in(-1))])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "expired_on_receipt"

    # Short-dated: drafted with a warning, posted only by the approve tier
    # with a reason.
    short = _drafted(pharmacist, po_id, [_line(packs=4, batch_no="SHORT1", expiry=_in(30))])
    assert short["needs_approval"] is True
    assert "short_shelf_life" in [w["code"] for w in short["warnings"]]
    resp = _post(pharmacist, short["id"])
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "approve_needed"
    resp = _post(admin, short["id"])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "missing_reason"
    resp = _post(admin, short["id"], short_expiry_reason="Only stock the vendor has; used fast")
    assert resp.status_code == 200, resp.text
    meta = json.loads(_audits("pharmacy_grn_posted", short["id"])[0]["metadata_json"])
    assert meta["short_expiry_accepted"]["reason"].startswith("Only stock")

    # The hospital's date decides, not the server's: a batch good today on the
    # server clock is expired once the site's date has moved on.
    expiring, tomorrow = _in(0), _in(1)
    monkeypatch.setattr(stock, "site_today", lambda: tomorrow)
    resp = _draft(pharmacist, po_id, [_line(packs=2, batch_no="EDGE1", expiry=expiring)])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "expired_on_receipt"


def test_the_receiver_never_accepts_their_own_short_expiry(monkeypatch, pharmacist, admin,
                                                           stores_made):
    import permissions

    po_id, *_ = _issued_po(pharmacist, admin, stores_made)
    short = _drafted(pharmacist, po_id, [_line(packs=2, batch_no="SHORT2", expiry=_in(20))])
    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "pharmacist",
                        permissions.ROLE_PERMISSIONS["pharmacist"]
                        | {permissions.PHARMACY_STORE_APPROVE})
    resp = _post(pharmacist, short["id"], short_expiry_reason="Needed today")
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "same_person"
    assert _lots_for(short["id"]) == []


# ═════════════════════════════════════════════════════════════════════════════
#  Post
# ═════════════════════════════════════════════════════════════════════════════

def test_post_puts_the_receipt_on_the_shelf_through_the_one_writer(pharmacist, admin,
                                                                   stores_made):
    po_id, drug, store, _vendor_code = _issued_po(pharmacist, admin, stores_made, packs=10,
                                                  rate=4500)
    before = _counts()
    resp = _draft(pharmacist, po_id, [_line(packs=6, free_packs=1, mfg_date=_in(-60))],
                  vendor_invoice_total_paise=30240, key=f"grn-{_uid()}")
    assert resp.status_code == 200, resp.text
    doc = resp.json()["goods_receipt"]
    assert doc["status"] == "draft" and _lots_for(doc["id"]) == []
    line = doc["lines"][0]
    assert (line["packs"], line["free_packs"], line["units"]) == (6, 1, 70)
    assert line["total_paise"] == 30240  # 6 x 4500 + 12% GST

    resp = _post(pharmacist, doc["id"])
    assert resp.status_code == 200, resp.text
    posted = resp.json()["goods_receipt"]
    assert posted["status"] == "posted" and posted["grn_number"].startswith("PHGRN-")
    lots = _lots_for(doc["id"])
    assert len(lots) == 1
    lot = lots[0]
    assert (lot["medication_id"], lot["store_id"], lot["status"]) == (drug, store["id"],
                                                                      "available")
    assert lot["quantity"] == 70 and lot["lot_number"] == "BX101"
    # Free units cost nothing and are spread: 30240 paise over 70 units.
    assert lot["unit_cost_paise"] == 432
    assert lot["mrp_pack_paise"] == 6000 and lot["units_per_pack"] == 10
    moves = _movements_for(doc["id"])
    assert [(m["movement_type"], m["quantity_change"]) for m in moves] == [("receipt", 70)]
    assert moves[0]["unit_cost_paise"] == 432 and moves[0]["actor_id"] == pharmacist["id"]
    with db_session() as db:
        assert stock.reconcile(db, lot_ids=[lot["id"]])["clean"] is True
        ref = db.execute("SELECT helix_lot_ref FROM goods_receipt_lines WHERE goods_receipt_id = ?",
                         (doc["id"],)).fetchone()[0]
    assert ref == lot["id"]
    po = _po(po_id)
    assert po["status"] == "partially_received"
    assert po["lines"][0]["received_quantity_milli"] == 6 * QUANTITY_SCALE
    assert _counts() == before, "no inventory_items or stock_movements row for a drug"
    for action in ("pharmacy_grn_created", "pharmacy_grn_posted"):
        rows = _audits(action, doc["id"])
        assert len(rows) == 1 and rows[0]["event_category"] == "security", action


def test_a_replayed_key_drafts_once_and_a_second_post_moves_nothing(pharmacist, admin,
                                                                   stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made)
    key = f"grn-{_uid()}"
    first = _draft(pharmacist, po_id, [_line()], key=key)
    again = _draft(pharmacist, po_id, [_line()], key=key)
    assert first.status_code == again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["goods_receipt"]["id"] == first.json()["goods_receipt"]["id"]
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM goods_receipts WHERE idempotency_key = ?",
                          (key,)).fetchone()[0] == 1
    grn_id = first.json()["goods_receipt"]["id"]
    assert _post(pharmacist, grn_id).status_code == 200
    replay = _post(pharmacist, grn_id)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert len(_movements_for(grn_id)) == 1
    assert len(_audits("pharmacy_grn_posted", grn_id)) == 1


def test_the_same_batch_merges_into_one_lot_at_its_weighted_cost(pharmacist, admin,
                                                                 stores_made):
    drug = _drug()
    store = _store(stores_made)
    po_a, *_ = _issued_po(pharmacist, admin, stores_made, drug=drug, store=store, rate=4500)
    po_b, *_ = _issued_po(pharmacist, admin, stores_made, drug=drug, store=store, rate=5600)
    a = _drafted(pharmacist, po_a, [_line(packs=2, batch_no="MERGE1")])
    b = _drafted(pharmacist, po_b, [_line(packs=2, batch_no="merge 1")])
    assert _post(pharmacist, a["id"]).status_code == 200
    assert _post(pharmacist, b["id"]).status_code == 200
    lots_a, lots_b = _lots_for(a["id"]), _lots_for(b["id"])
    assert len(lots_a) == 1 and lots_a[0]["id"] == lots_b[0]["id"]
    lot = lots_b[0]
    assert lot["quantity"] == 40 and lot["lot_number"] == "MERGE1"
    # (20 x 504 + 20 x 627) / 40, half up.
    assert lot["unit_cost_paise"] == 566
    with db_session() as db:
        assert stock.reconcile(db, lot_ids=[lot["id"]])["clean"] is True


def test_a_batch_on_file_with_another_expiry_is_refused(pharmacist, admin, stores_made):
    drug = _drug()
    po_a, *_ = _issued_po(pharmacist, admin, stores_made, drug=drug)
    a = _drafted(pharmacist, po_a, [_line(packs=2, batch_no="EXP1", expiry=_in(500))])
    assert _post(pharmacist, a["id"]).status_code == 200
    po_b, *_ = _issued_po(pharmacist, admin, stores_made, drug=drug)
    resp = _draft(pharmacist, po_b, [_line(packs=2, batch_no="exp1", expiry=_in(530))])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "batch_expiry_mismatch"
    resp = _draft(pharmacist, po_b, [_line(packs=1, batch_no="TWIN", expiry=_in(500)),
                                     _line(packs=1, batch_no="TWIN", expiry=_in(501))])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "batch_expiry_mismatch"


def test_a_failure_at_any_step_rolls_everything_back(monkeypatch, pharmacist, admin,
                                                     stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made)
    doc = _drafted(pharmacist, po_id, [_line(packs=3)])
    before = _po(po_id)

    def boom(conn, movement):
        raise RuntimeError("register write failed")

    stock.register_movement_hook("test_grn_boom", boom)
    try:
        with pytest.raises(RuntimeError):
            purchasing.post_goods_receipt(doc["id"], actor=pharmacist, may_write=True,
                                          may_approve=False)
    finally:
        stock.register_movement_hook("test_grn_boom", None)

    real = audit_service.log_audit_event

    def no_post_audit(*args, **kwargs):
        if kwargs.get("action_type") == "pharmacy_grn_posted":
            raise audit_service.AuditWriteError("audit store down")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", no_post_audit)
    resp = _post(pharmacist, doc["id"])
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"

    after = _po(po_id)
    assert (after["status"], after["lines"][0]["received_quantity_milli"]) == (
        before["status"], before["lines"][0]["received_quantity_milli"])
    with db_session() as db:
        assert db.execute("SELECT status FROM goods_receipts WHERE id = ?",
                          (doc["id"],)).fetchone()[0] == "draft"
    assert _lots_for(doc["id"]) == [] and _movements_for(doc["id"]) == []
    assert _audits("pharmacy_grn_posted", doc["id"]) == []


def test_a_drug_retired_or_declassified_after_ordering_is_never_received(pharmacist, admin,
                                                                          stores_made):
    """grn-6 meets grn-8: an order sent for a drug that is then retired, or
    loses its classification (GRN-R18), is refused at draft and again at
    post, so no lot of it is ever opened."""
    po_id, drug, *_ = _issued_po(pharmacist, admin, stores_made, packs=10)
    doc = _drafted(pharmacist, po_id, [_line(packs=2, batch_no="RET1")])
    with db_session() as db:
        db.execute("UPDATE helix_medication_master SET identity_status = 'retired' WHERE id = ?",
                   (drug,))
    resp = _post(pharmacist, doc["id"])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "drug_not_confirmed"
    resp = _draft(pharmacist, po_id, [_line(packs=2, batch_no="RET2")])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "drug_not_confirmed"
    assert _lots_for(doc["id"]) == []

    po_b, drug_b, *_ = _issued_po(pharmacist, admin, stores_made, packs=4)
    with db_session() as db:
        db.execute("UPDATE helix_medication_master SET ndps_category = NULL WHERE id = ?",
                   (drug_b,))
    resp = _draft(pharmacist, po_b, [_line(packs=1, batch_no="UNC1")])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "drug_unclassified"


def _nothing_moved(grn_id, po_id):
    """The stale draft stayed a draft and nothing reached the shelf or the order."""
    assert _lots_for(grn_id) == [] and _movements_for(grn_id) == []
    assert _audits("pharmacy_grn_posted", grn_id) == []
    with db_session() as db:
        assert db.execute("SELECT status FROM goods_receipts WHERE id = ?",
                          (grn_id,)).fetchone()[0] == "draft"
        assert db.execute("SELECT COALESCE(SUM(received_quantity_milli), 0) FROM "
                          "purchase_order_lines WHERE purchase_order_id = ?",
                          (po_id,)).fetchone()[0] == 0


def test_a_draft_against_an_order_cancelled_since_posts_nothing(pharmacist, admin, stores_made):
    """The order is checked again at post, not only at draft: an order the
    administrator cancels after the delivery was drafted takes nothing onto
    the shelf, for the write tier or the approve tier, and the draft can
    then only be cancelled."""
    po_id, *_ = _issued_po(pharmacist, admin, stores_made, packs=10)
    doc = _drafted(pharmacist, po_id, [_line(packs=2, batch_no="STALE1")])
    resp = client.post(f"{BASE}/purchase-orders/{po_id}/cancel", headers=admin["hdr"],
                       json={"reason": "Vendor cannot supply"})
    assert resp.status_code == 200, resp.text
    assert _po(po_id)["status"] == "cancelled"

    for user in (pharmacist, admin):
        resp = _post(user, doc["id"])
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "po_not_receivable"
        assert detail["order_status"] == "cancelled"
        assert "cancelled" in detail["message"] and "_" not in detail["message"]
    _nothing_moved(doc["id"], po_id)
    after = _po(po_id)
    assert after["status"] == "cancelled"
    assert [line["status"] for line in after["lines"]] == ["cancelled"]

    resp = _draft(pharmacist, po_id, [_line(packs=2, batch_no="STALE2")])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "po_not_receivable"
    resp = client.post(f"{BASE}/goods-receipts/{doc['id']}/cancel", headers=pharmacist["hdr"],
                       json={"reason": "Order cancelled"})
    assert resp.status_code == 200 and resp.json()["goods_receipt"]["status"] == "cancelled"


def test_a_draft_against_an_order_closed_since_posts_nothing(pharmacist, admin, stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made, packs=10)
    doc = _drafted(pharmacist, po_id, [_line(packs=3, batch_no="CLOSED1")])
    with db_session(immediate=True) as db:
        procurement._move_po_core(db, po_id, "closed", actor=admin["id"], domain="pharmacy")
    resp = _post(pharmacist, doc["id"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "po_not_receivable"
    assert resp.json()["detail"]["order_status"] == "closed"
    _nothing_moved(doc["id"], po_id)
    assert _po(po_id)["status"] == "closed"


def test_a_draft_against_an_order_line_closed_since_posts_nothing(pharmacist, admin, stores_made):
    """The order stays with the vendor, but the receipt's own order line was
    cancelled or closed short after the draft: the post is refused whole,
    the draft against it is refused too, and the line still open receives."""
    store = _store(stores_made)
    vendor = _vendor()
    first, second = _drug(), _drug()
    po = purchasing.create_purchase_order(
        vendor_code=vendor, store_code=store["code"], actor=pharmacist,
        lines=[{"helix_medication_id": first, "packs": 5, "unit_rate_paise": 4500},
               {"helix_medication_id": second, "packs": 5, "unit_rate_paise": 4500}]
    )["purchase_order"]
    purchasing.submit_purchase_order(po["id"], actor=pharmacist)
    purchasing.approve_purchase_order(po["id"], actor=admin)
    purchasing.issue_purchase_order(po["id"], actor=admin)
    po_id = po["id"]

    for closed in ("short_closed", "cancelled"):
        doc = _drafted(pharmacist, po_id, [_line(po_line_no=1, packs=1, batch_no="OPEN1"),
                                           _line(po_line_no=2, packs=1, batch_no="SHUT1")])
        with db_session() as db:
            db.execute("UPDATE purchase_order_lines SET status = ? WHERE purchase_order_id = ? "
                       "AND line_no = 2", (closed, po_id))
        resp = _post(pharmacist, doc["id"])
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert (detail["error"], detail["po_line_no"]) == ("line_closed", 2)
        assert "_" not in detail["message"]
        _nothing_moved(doc["id"], po_id)
        assert _po(po_id)["status"] == "issued"
        resp = _draft(pharmacist, po_id, [_line(po_line_no=2, packs=1, batch_no="SHUT2")])
        assert resp.status_code == 409 and resp.json()["detail"]["error"] == "line_closed"
        assert client.post(f"{BASE}/goods-receipts/{doc['id']}/cancel",
                           headers=pharmacist["hdr"],
                           json={"reason": "Line closed"}).status_code == 200
        with db_session() as db:
            db.execute("UPDATE purchase_order_lines SET status = 'open' WHERE "
                       "purchase_order_id = ? AND line_no = 2", (po_id,))

    with db_session() as db:
        db.execute("UPDATE purchase_order_lines SET status = 'short_closed' WHERE "
                   "purchase_order_id = ? AND line_no = 2", (po_id,))
    ok = _drafted(pharmacist, po_id, [_line(po_line_no=1, packs=2, batch_no="OPEN2")])
    assert _post(pharmacist, ok["id"]).status_code == 200
    assert _po(po_id)["lines"][0]["received_quantity_milli"] == 2 * QUANTITY_SCALE
    assert _po(po_id)["lines"][1]["received_quantity_milli"] == 0


def test_warnings_for_a_rate_above_mrp_and_an_invoice_mismatch(pharmacist, admin, stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made, rate=7000)
    doc = _drafted(pharmacist, po_id, [_line(packs=1, mrp_pack_paise=6500)],
                   vendor_invoice_total_paise=1000)
    codes = [w["code"] for w in doc["warnings"]]
    assert "rate_above_mrp" in codes and "invoice_total_mismatch" in codes


def test_a_draft_is_cancelled_with_a_reason_and_a_posted_receipt_never(pharmacist, admin,
                                                                      stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made)
    doc = _drafted(pharmacist, po_id, [_line(packs=1)])
    resp = client.post(f"{BASE}/goods-receipts/{doc['id']}/cancel", headers=pharmacist["hdr"],
                       json={})
    assert resp.status_code == 422
    resp = client.post(f"{BASE}/goods-receipts/{doc['id']}/cancel", headers=pharmacist["hdr"],
                       json={"reason": "Wrong delivery"})
    assert resp.status_code == 200 and resp.json()["goods_receipt"]["status"] == "cancelled"
    assert len(_audits("pharmacy_grn_cancelled", doc["id"])) == 1
    posted = _drafted(pharmacist, po_id, [_line(packs=1, batch_no="KEEP1")])
    assert _post(pharmacist, posted["id"]).status_code == 200
    resp = client.post(f"{BASE}/goods-receipts/{posted['id']}/cancel", headers=pharmacist["hdr"],
                       json={"reason": "Too late"})
    assert resp.status_code == 409


def test_an_order_discount_reaches_the_landed_cost(pharmacist, admin, stores_made):
    po_id, *_ = _issued_po(pharmacist, admin, stores_made, rate=10000, discount_bp=1000)
    doc = _drafted(pharmacist, po_id, [_line(packs=1, mrp_pack_paise=12000)])
    line = doc["lines"][0]
    assert line["unit_rate_paise"] == 9000  # 10% off the order's rate
    assert line["total_paise"] == 10080 and line["unit_cost_paise"] == 1008


def test_the_receivable_list_shows_what_is_still_to_come(pharmacist, admin, stores_made):
    po_id, drug, *_ = _issued_po(pharmacist, admin, stores_made, packs=8)
    doc = _drafted(pharmacist, po_id, [_line(packs=3)])
    _post(pharmacist, doc["id"])
    resp = client.get(f"{BASE}/goods-receipts/receivable", headers=pharmacist["hdr"])
    assert resp.status_code == 200
    order = next(o for o in resp.json()["orders"] if o["id"] == po_id)
    assert order["lines"][0]["outstanding_packs"] == 5
    assert order["lines"][0]["helix_medication_id"] == drug
    assert resp.json()["min_shelf_life_days"] == config.PHARMACY_MIN_SHELF_LIFE_DAYS
    listed = client.get(f"{BASE}/goods-receipts", headers=pharmacist["hdr"],
                        params={"purchase_order_id": po_id}).json()["goods_receipts"]
    assert [g["id"] for g in listed] == [doc["id"]]


# ═════════════════════════════════════════════════════════════════════════════
#  Narcotics
# ═════════════════════════════════════════════════════════════════════════════

def test_a_narcotic_only_into_the_vault_and_then_dispensable(pharmacist, admin, stores_made):
    drug = _drug(ndps_category="narcotic", drug_schedule="X", name=f"Narcotest {_uid()} 10 mg")
    vault = _store(stores_made, kind="ndps_vault")
    other = _store(stores_made, kind="sub_store")
    po_id, *_ = _issued_po(pharmacist, admin, stores_made, drug=drug, store=vault,
                           vendor=_vendor(ndps="NDPS-LIC-9"))
    resp = _draft(pharmacist, po_id, [_line(packs=2, batch_no="NARC1")],
                  store_code=other["code"])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "ndps_needs_vault"
    doc = _drafted(pharmacist, po_id, [_line(packs=2, batch_no="NARC1")])
    assert _post(pharmacist, doc["id"]).status_code == 200
    lot = _lots_for(doc["id"])[0]
    assert lot["store_id"] == vault["id"] and lot["quantity"] == 20

    check = helix_service.pre_flight_inventory_check(drug)
    assert check["in_stock"] is True and check["total_quantity"] == 20

    pid = f"PATGR{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Receipt Case {_uid()}", f"MRN-{pid}"))
    with db_session() as db:
        name = db.execute("SELECT name FROM helix_medication_master WHERE id = ?",
                          (drug,)).fetchone()[0]
    order = helix_service.create_order(pid, name, "10 mg", "IV", "once", pharmacist["id"])
    for target in ("reviewing", "verified", "scheduled", "dispensed"):
        helix_service.transition_order_status(order["id"], target, pharmacist["id"])
    with db_session() as db:
        assert db.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                          (lot["id"],)).fetchone()[0] == 19
        assert stock.reconcile(db, lot_ids=[lot["id"]])["clean"] is True
