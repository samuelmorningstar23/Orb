"""Pharmacy purchase orders (grn-7): /api/pharmacy-store/purchase-orders,
behind ORB_PHARMACY_STORE.

Pinned (docs/his_design/grn.json, grn-7 tests, rulings GRN-R10, R11, R14, R18):
  * with the module off every purchasing route answers 404 module_disabled;
  * an unclassified drug (schedule or NDPS class missing), an inactive or
    unconfirmed drug, a drug with no pack size or no GST rate is refused;
  * a vendor with no drug licence, or an expired one, is refused; one ending
    within the warning window is allowed with a warning; an NDPS line needs
    the vendor's NDPS licence; an NDPS drug goes into the vault and the vault
    takes nothing else;
  * self-approval is 403 and changes nothing;
  * the audit row exists if and only if the order commits: a failed audit
    write rolls the order (or the approval) back with 503;
  * line money matches money.compute_line, the pack size is snapshotted, and
    the number comes from the PHPO series;
  * a pharmacy order is invisible and unreachable on /api/procurement;
  * everything works with ORB_PROCUREMENT patched off;
  * the licence is checked again at issue;
  * cancelling: the write tier only before approval, a reason always;
  * reorder suggestions become a draft at the suggested packs, with a typed
    rate, and are not suggested again once on order.
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
from services import audit_service
from services import pharmacy_purchasing_service as purchasing
from services import pharmacy_stock_service as stock
from services import procurement_service as procurement
from services.helix_db import init_helix_db
from services.money import QUANTITY_SCALE, compute_line

client = TestClient(app)
BASE = "/api/pharmacy-store"
TODAY = date.today()
LATER = (TODAY + timedelta(days=400)).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture(autouse=True)
def module_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)


def _user(role, department=None):
    uid = f"UPO{_uid()}"
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
    med = f"MEDPO{_uid()}"
    cols = {"id": med, "name": f"Ordertest {med[-6:]} 10 mg", "generic_name": "ordermol",
            "form": "tablet", "strength": "10 mg", "drug_schedule": "H",
            "ndps_category": "none", "pack_size_units": 10, "gst_rate_bp": 1200,
            "purchase_uom": "strip", "hsn_code": "3004"} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    return med


def _vendor(*, valid_to=LATER, numbers="DL-20B-1234", ndps=None, **extra):
    code = f"VPO{_uid()}"
    procurement.upsert_vendor(code=code, name=f"Pharma supplier {code}", state_code="27", **extra)
    with db_session() as db:
        db.execute("UPDATE vendors SET drug_licence_numbers = ?, drug_licence_valid_to = ?, "
                   "ndps_licence_number = ? WHERE code = ?", (numbers, valid_to, ndps, code))
    return code


@pytest.fixture
def stores_made():
    made: list[int] = []
    yield made
    with db_session() as db:
        for store_id in made:
            for (lot_id,) in db.execute("SELECT id FROM helix_inventory_lots WHERE store_id = ?",
                                        (store_id,)).fetchall():
                db.execute("DELETE FROM helix_stock_movements WHERE lot_id = ?", (lot_id,))
                db.execute("DELETE FROM pharmacy_lot_events WHERE lot_id = ?", (lot_id,))
                db.execute("DELETE FROM helix_inventory_lots WHERE id = ?", (lot_id,))
            db.execute("DELETE FROM pharmacy_par_levels WHERE store_id = ?", (store_id,))
            db.execute("UPDATE stores SET is_active = 0 WHERE id = ?", (store_id,))


def _store(made, kind="sub_store") -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(db, code=f"PO{_uid()}", name=f"PO store {kind}",
                                             domain="pharmacy", pharmacy_kind=kind)
    made.append(row["id"])
    return row


def _line(drug, packs=3, rate=4500, **extra):
    return {"helix_medication_id": drug, "packs": packs, "unit_rate_paise": rate} | extra


def _create(user, vendor, store, lines, **extra):
    return client.post(f"{BASE}/purchase-orders", headers=user["hdr"],
                       json={"vendor_code": vendor, "store_code": store["code"], "lines": lines}
                       | extra)


def _audits(action, po_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(po_id))).fetchall()]


def _po_row(po_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone())


def _drafted(pharmacist, made, **kw):
    drug = kw.pop("drug", None) or _drug()
    vendor = kw.pop("vendor", None) or _vendor()
    store = kw.pop("store", None) or _store(made)
    resp = _create(pharmacist, vendor, store, [_line(drug)])
    assert resp.status_code == 200, resp.text
    return resp.json()["purchase_order"], drug, vendor, store


# ═════════════════════════════════════════════════════════════════════════════
#  The switch and the tiers
# ═════════════════════════════════════════════════════════════════════════════

def test_every_purchasing_route_is_404_with_the_module_off(monkeypatch, pharmacist):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    for method, path in (("get", "/purchase-orders"), ("get", "/purchase-orders/options"),
                         ("get", "/purchase-orders/reorder-suggestions"),
                         ("get", "/purchase-orders/1"), ("post", "/purchase-orders"),
                         ("post", "/purchase-orders/from-suggestions"),
                         ("post", "/purchase-orders/1/submit"),
                         ("post", "/purchase-orders/1/approve"),
                         ("post", "/purchase-orders/1/issue"),
                         ("post", "/purchase-orders/1/cancel")):
        resp = getattr(client, method)(f"{BASE}{path}", headers=pharmacist["hdr"],
                                       **({} if method == "get" else {"json": {}}))
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"


def test_the_tiers_hold(pharmacist, admin, stores_made):
    doctor = _user("doctor", "Cardiology")
    assert client.get(f"{BASE}/purchase-orders", headers=doctor["hdr"]).status_code == 403
    store = _store(stores_made)
    # The approver tier does not raise orders, and the write tier does not approve.
    assert _create(admin, _vendor(), store, [_line(_drug())]).status_code == 403
    doc, *_ = _drafted(pharmacist, stores_made, store=store)
    client.post(f"{BASE}/purchase-orders/{doc['id']}/submit", headers=pharmacist["hdr"])
    for action in ("approve", "issue"):
        resp = client.post(f"{BASE}/purchase-orders/{doc['id']}/{action}",
                           headers=pharmacist["hdr"])
        assert resp.status_code == 403, (action, resp.text)
    assert client.post(f"{BASE}/purchase-orders/{doc['id']}/cancel", headers=doctor["hdr"],
                       json={"reason": "x"}).status_code == 403


def test_reading_alone_never_cancels(monkeypatch, pharmacist, stores_made):
    """A role that may read the store but neither raise nor approve orders
    (none today) is refused a cancel, and the refusal is audited."""
    import permissions

    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "cashier",
                        permissions.ROLE_PERMISSIONS["cashier"] | {"pharmacy_store.read"})
    reader = _user("cashier")
    doc, *_ = _drafted(pharmacist, stores_made)
    resp = client.post(f"{BASE}/purchase-orders/{doc['id']}/cancel", headers=reader["hdr"],
                       json={"reason": "x"})
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "permission_denied"
    assert _po_row(doc["id"])["status"] == "draft"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = "
                          "'permission_denied' AND actor_user_id = ?",
                          (reader["id"],)).fetchone()[0] == 1


# ═════════════════════════════════════════════════════════════════════════════
#  What may be ordered (GRN-R18 and the drug master)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("fields, code", [
    ({"drug_schedule": None, "ndps_category": None}, "drug_unclassified"),
    ({"drug_schedule": "H", "ndps_category": None}, "drug_unclassified"),
    ({"drug_schedule": None, "ndps_category": "none"}, "drug_unclassified"),
    ({"pack_size_units": None}, "pack_size_not_configured"),
    ({"gst_rate_bp": None}, "gst_not_configured"),
    ({"is_active": 0}, "drug_inactive"),
    ({"identity_status": "draft"}, "drug_not_confirmed"),
])
def test_a_drug_that_is_not_orderable_is_refused(pharmacist, stores_made, fields, code):
    drug = _drug(**fields)
    resp = _create(pharmacist, _vendor(), _store(stores_made), [_line(drug)])
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM purchase_order_lines WHERE "
                          "helix_medication_id = ?", (drug,)).fetchone()[0] == 0


def test_an_unknown_drug_or_a_repeated_line_is_refused(pharmacist, stores_made):
    store, vendor, drug = _store(stores_made), _vendor(), _drug()
    resp = _create(pharmacist, vendor, store, [_line("MED-NOPE-" + _uid())])
    assert resp.json()["detail"]["error"] == "drug_not_found"
    resp = _create(pharmacist, vendor, store, [_line(drug), _line(drug, packs=1)])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "duplicate_line"


@pytest.mark.parametrize("line_extra, code", [
    ({"packs": 0}, "bad_value"), ({"packs": 1.5}, "bad_value"), ({"packs": True}, "bad_value"),
    ({"unit_rate_paise": -1}, "bad_value"), ({"unit_rate_paise": None}, "bad_value"),
])
def test_quantities_and_rates_are_whole_and_never_invented(pharmacist, stores_made, line_extra,
                                                           code):
    resp = _create(pharmacist, _vendor(), _store(stores_made), [_line(_drug()) | line_extra])
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code


# ═════════════════════════════════════════════════════════════════════════════
#  The vendor's licence (GRN-R10) and where NDPS drugs go (GRN-R14)
# ═════════════════════════════════════════════════════════════════════════════

def test_a_vendor_without_a_valid_drug_licence_is_refused(pharmacist, stores_made):
    store, drug = _store(stores_made), _drug()
    yesterday = (TODAY - timedelta(days=2)).isoformat()
    for vendor, code in ((_vendor(numbers=None), "vendor_licence_missing"),
                         (_vendor(valid_to=None), "vendor_licence_missing"),
                         (_vendor(valid_to=yesterday), "vendor_licence_expired")):
        resp = _create(pharmacist, vendor, store, [_line(drug)])
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == code


def test_a_licence_ending_soon_is_allowed_with_a_warning(pharmacist, stores_made):
    soon = (TODAY + timedelta(days=10)).isoformat()
    resp = _create(pharmacist, _vendor(valid_to=soon), _store(stores_made), [_line(_drug())])
    assert resp.status_code == 200, resp.text
    assert [w["code"] for w in resp.json()["warnings"]] == ["vendor_licence_expiring"]
    assert resp.json()["purchase_order"]["vendor_licence"]["status"] == "expiring"


def test_ndps_drugs_go_into_the_vault_from_an_ndps_licensed_vendor(pharmacist, stores_made):
    morphine = _drug(ndps_category="narcotic", drug_schedule="X")
    paracetamol = _drug()
    vault, sub = _store(stores_made, "ndps_vault"), _store(stores_made)
    resp = _create(pharmacist, _vendor(ndps="NDPS-77"), sub, [_line(morphine)])
    assert resp.json()["detail"]["error"] == "ndps_needs_vault"
    resp = _create(pharmacist, _vendor(ndps="NDPS-77"), vault, [_line(paracetamol)])
    assert resp.json()["detail"]["error"] == "vault_is_for_ndps"
    resp = _create(pharmacist, _vendor(), vault, [_line(morphine)])
    assert resp.json()["detail"]["error"] == "vendor_ndps_licence_missing"
    resp = _create(pharmacist, _vendor(ndps="NDPS-77"), vault, [_line(morphine)])
    assert resp.status_code == 200, resp.text


def test_a_returns_or_closed_store_takes_no_order(pharmacist, stores_made):
    drug, vendor = _drug(), _vendor()
    returns = _store(stores_made, "returns_quarantine")
    assert _create(pharmacist, vendor, returns, [_line(drug)]).json()["detail"]["error"] \
        == "store_not_orderable"
    closed = _store(stores_made)
    with db_session() as db:
        db.execute("UPDATE stores SET is_active = 0 WHERE id = ?", (closed["id"],))
    assert _create(pharmacist, vendor, closed, [_line(drug)]).json()["detail"]["error"] \
        == "store_inactive"


# ═════════════════════════════════════════════════════════════════════════════
#  The whole life of an order
# ═════════════════════════════════════════════════════════════════════════════

def test_draft_submit_approve_issue_with_audit_money_and_a_phpo_number(pharmacist, admin,
                                                                        stores_made):
    drug = _drug(gst_rate_bp=1200, pack_size_units=15)
    resp = _create(pharmacist, _vendor(), _store(stores_made),
                   [_line(drug, packs=7, rate=12345, discount_bp=500)])
    assert resp.status_code == 200, resp.text
    doc = resp.json()["purchase_order"]
    assert doc["status"] == "draft" and doc["domain"] == "pharmacy"
    assert doc["created_by"] == pharmacist["id"]
    line = doc["lines"][0]
    want = compute_line(unit_rate_paise=12345, quantity_milli=7 * QUANTITY_SCALE,
                        discount_bp=500, gst_rate_bp=1200, gst_treatment="taxable",
                        interstate=False)
    for key in ("gross_paise", "discount_paise", "taxable_value_paise", "cgst_paise",
                "sgst_paise", "igst_paise", "tax_paise", "total_paise"):
        assert line[key] == getattr(want, key), key
    assert line["helix_medication_id"] == drug
    assert line["units_per_pack"] == 15 and line["quantity_packs"] == 7
    assert line["quantity_units"] == 105
    po_id = doc["id"]
    created = _audits("pharmacy_po_created", po_id)
    assert len(created) == 1 and created[0]["event_category"] == "security"
    assert created[0]["target_id"] == str(po_id)

    # A later change to the master never rewrites the line's pack size.
    with db_session() as db:
        db.execute("UPDATE helix_medication_master SET pack_size_units = 20 WHERE id = ?", (drug,))
    assert client.post(f"{BASE}/purchase-orders/{po_id}/submit",
                       headers=pharmacist["hdr"]).status_code == 200
    resp = client.post(f"{BASE}/purchase-orders/{po_id}/approve", headers=admin["hdr"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["purchase_order"]["approved_by"] == admin["id"]
    resp = client.post(f"{BASE}/purchase-orders/{po_id}/issue", headers=admin["hdr"])
    assert resp.status_code == 200, resp.text
    issued = resp.json()["purchase_order"]
    assert issued["status"] == "issued"
    assert issued["po_number"].startswith("PHPO-")
    assert issued["lines"][0]["units_per_pack"] == 15
    assert isinstance(issued["number_series_configured"], bool)
    for action in ("pharmacy_po_submitted", "pharmacy_po_approved", "pharmacy_po_issued"):
        rows = _audits(action, po_id)
        assert len(rows) == 1 and rows[0]["event_category"] == "security", action

    listed = client.get(f"{BASE}/purchase-orders", headers=pharmacist["hdr"]).json()
    assert any(p["id"] == po_id for p in listed["purchase_orders"])
    got = client.get(f"{BASE}/purchase-orders/{po_id}", headers=pharmacist["hdr"]).json()
    assert got["purchase_order"]["po_number"] == issued["po_number"]


def test_the_creator_never_approves_their_own_order(admin, stores_made):
    """A person holding both tiers still cannot approve what they raised.
    Nobody holds both today, so the order is raised through the service."""
    store, drug, vendor = _store(stores_made), _drug(), _vendor()
    made = purchasing.create_purchase_order(vendor_code=vendor, store_code=store["code"],
                                            lines=[_line(drug)], actor=admin)
    po_id = made["purchase_order"]["id"]
    purchasing.submit_purchase_order(po_id, actor=admin)
    resp = client.post(f"{BASE}/purchase-orders/{po_id}/approve", headers=admin["hdr"])
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "same_person"
    assert _po_row(po_id)["status"] == "pending_approval"
    assert _audits("pharmacy_po_approved", po_id) == []


def test_an_unknown_order_is_404(pharmacist, admin):
    assert client.get(f"{BASE}/purchase-orders/987654321", headers=pharmacist["hdr"]).status_code \
        == 404
    resp = client.post(f"{BASE}/purchase-orders/987654321/approve", headers=admin["hdr"])
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "not_found"


def test_a_licence_that_lapsed_since_approval_stops_the_issue(pharmacist, admin, stores_made):
    doc, drug, vendor, store = _drafted(pharmacist, stores_made)
    po_id = doc["id"]
    client.post(f"{BASE}/purchase-orders/{po_id}/submit", headers=pharmacist["hdr"])
    assert client.post(f"{BASE}/purchase-orders/{po_id}/approve",
                       headers=admin["hdr"]).status_code == 200
    with db_session() as db:
        db.execute("UPDATE vendors SET drug_licence_valid_to = ? WHERE code = ?",
                   ((TODAY - timedelta(days=1)).isoformat(), vendor))
    resp = client.post(f"{BASE}/purchase-orders/{po_id}/issue", headers=admin["hdr"])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "vendor_licence_expired"
    row = _po_row(po_id)
    assert row["status"] == "approved" and row["po_number"] is None


def test_a_drug_unclassified_since_the_draft_stops_the_submit(pharmacist, stores_made):
    doc, drug, *_ = _drafted(pharmacist, stores_made)
    with db_session() as db:
        db.execute("UPDATE helix_medication_master SET ndps_category = NULL WHERE id = ?", (drug,))
    resp = client.post(f"{BASE}/purchase-orders/{doc['id']}/submit", headers=pharmacist["hdr"])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "drug_unclassified"
    assert _po_row(doc["id"])["status"] == "draft"


def test_an_illegal_move_is_409(pharmacist, admin, stores_made):
    doc, _drug_id, vendor, _store_row = _drafted(pharmacist, stores_made)
    resp = client.post(f"{BASE}/purchase-orders/{doc['id']}/issue", headers=admin["hdr"])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "illegal_transition"
    # A cancelled order is answered as one, before any licence check.
    client.post(f"{BASE}/purchase-orders/{doc['id']}/cancel", headers=pharmacist["hdr"],
                json={"reason": "Not needed"})
    with db_session() as db:
        db.execute("UPDATE vendors SET drug_licence_valid_to = ? WHERE code = ?",
                   ((TODAY - timedelta(days=1)).isoformat(), vendor))
    resp = client.post(f"{BASE}/purchase-orders/{doc['id']}/submit", headers=pharmacist["hdr"])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "illegal_transition"


# ═════════════════════════════════════════════════════════════════════════════
#  The audit row exists if and only if the change commits
# ═════════════════════════════════════════════════════════════════════════════

def _failing_audit(monkeypatch, action):
    real = audit_service.log_audit_event

    def flaky(*args, **kwargs):
        if kwargs.get("action_type") == action:
            raise audit_service.AuditWriteError("injected audit outage")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", flaky)


def test_a_failed_audit_write_rolls_the_new_order_back(monkeypatch, pharmacist, stores_made):
    _failing_audit(monkeypatch, "pharmacy_po_created")
    drug = _drug()
    resp = _create(pharmacist, _vendor(), _store(stores_made), [_line(drug)])
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "audit_unavailable"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM purchase_orders WHERE created_by = ?",
                          (pharmacist["id"],)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM purchase_order_lines WHERE "
                          "helix_medication_id = ?", (drug,)).fetchone()[0] == 0


def test_a_failed_audit_write_rolls_the_approval_back(monkeypatch, pharmacist, admin,
                                                      stores_made):
    doc, *_ = _drafted(pharmacist, stores_made)
    client.post(f"{BASE}/purchase-orders/{doc['id']}/submit", headers=pharmacist["hdr"])
    _failing_audit(monkeypatch, "pharmacy_po_approved")
    resp = client.post(f"{BASE}/purchase-orders/{doc['id']}/approve", headers=admin["hdr"])
    assert resp.status_code == 503
    row = _po_row(doc["id"])
    assert row["status"] == "pending_approval" and row["approved_by"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  The two desks stay apart, and Procurement can be off
# ═════════════════════════════════════════════════════════════════════════════

def test_a_pharmacy_order_is_invisible_and_unreachable_on_procurement(pharmacist, admin,
                                                                       stores_made):
    doc, *_ = _drafted(pharmacist, stores_made)
    po_id = doc["id"]
    client.post(f"{BASE}/purchase-orders/{po_id}/submit", headers=pharmacist["hdr"])
    listed = client.get("/api/procurement/purchase-orders", headers=admin["hdr"],
                        params={"limit": 500}).json()["purchase_orders"]
    assert all(p["id"] != po_id for p in listed)
    assert client.get(f"/api/procurement/purchase-orders/{po_id}",
                      headers=admin["hdr"]).status_code == 404
    for action in ("approve", "issue"):
        assert client.post(f"/api/procurement/purchase-orders/{po_id}/{action}",
                           headers=admin["hdr"]).status_code == 404, action
    assert client.post(f"/api/procurement/purchase-orders/{po_id}/cancel", headers=admin["hdr"],
                       json={"reason": "no"}).status_code == 404
    assert _po_row(po_id)["status"] == "pending_approval"
    assert _audits("pharmacy_po_approved", po_id) == []


def test_it_works_with_procurement_switched_off(monkeypatch, pharmacist, admin, stores_made):
    monkeypatch.setattr(config, "PROCUREMENT_ENABLED", False)
    assert client.get("/api/procurement/purchase-orders",
                      headers=admin["hdr"]).status_code == 404
    doc, *_ = _drafted(pharmacist, stores_made)
    po_id = doc["id"]
    for user, action in ((pharmacist, "submit"), (admin, "approve"), (admin, "issue")):
        resp = client.post(f"{BASE}/purchase-orders/{po_id}/{action}", headers=user["hdr"])
        assert resp.status_code == 200, (action, resp.text)
    assert _po_row(po_id)["status"] == "issued"
    opts = client.get(f"{BASE}/purchase-orders/options", headers=pharmacist["hdr"])
    assert opts.status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
#  Cancelling
# ═════════════════════════════════════════════════════════════════════════════

def test_cancelling_needs_a_reason_and_the_approve_tier_after_approval(pharmacist, admin,
                                                                       stores_made):
    doc, *_ = _drafted(pharmacist, stores_made)
    url = f"{BASE}/purchase-orders/{doc['id']}/cancel"
    resp = client.post(url, headers=pharmacist["hdr"], json={"reason": "  "})
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "missing_field"
    resp = client.post(url, headers=pharmacist["hdr"], json={"reason": "Typed the wrong drug"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["purchase_order"]["status"] == "cancelled"
    rows = _audits("pharmacy_po_cancelled", doc["id"])
    assert len(rows) == 1
    assert json.loads(rows[0]["metadata_json"])["reason"] == "Typed the wrong drug"

    doc, *_ = _drafted(pharmacist, stores_made)
    po_id = doc["id"]
    client.post(f"{BASE}/purchase-orders/{po_id}/submit", headers=pharmacist["hdr"])
    client.post(f"{BASE}/purchase-orders/{po_id}/approve", headers=admin["hdr"])
    resp = client.post(f"{BASE}/purchase-orders/{po_id}/cancel", headers=pharmacist["hdr"],
                       json={"reason": "Not needed"})
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "approve_needed"
    assert _po_row(po_id)["status"] == "approved"
    resp = client.post(f"{BASE}/purchase-orders/{po_id}/cancel", headers=admin["hdr"],
                       json={"reason": "Vendor cannot supply"})
    assert resp.status_code == 200 and resp.json()["purchase_order"]["status"] == "cancelled"


# ═════════════════════════════════════════════════════════════════════════════
#  Reorder suggestions to a draft order
# ═════════════════════════════════════════════════════════════════════════════

def test_reorder_suggestions_become_a_draft_and_are_not_suggested_twice(pharmacist, admin,
                                                                        stores_made):
    store = _store(stores_made)
    drug = _drug(pack_size_units=10)
    with db_session(immediate=True) as db:
        stock.open_or_merge_lot(db, medication_id=drug, lot_number=f"B{_uid()}",
                                expiration_date=LATER, units=5, actor_id="tester",
                                store_id=store["id"])
        db.execute("INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units) "
                   "VALUES (?, ?, 20)", (store["id"], drug))
    resp = client.get(f"{BASE}/purchase-orders/reorder-suggestions", headers=pharmacist["hdr"],
                      params={"store_id": store["id"]})
    assert resp.status_code == 200, resp.text
    mine = [s for s in resp.json()["suggestions"] if s["drug_id"] == drug]
    assert len(mine) == 1
    assert mine[0]["suggested_units"] == 15 and mine[0]["suggested_packs"] == 2
    assert mine[0]["orderable"] is True and mine[0]["last_rate_paise"] is None

    vendor = _vendor()
    body = {"vendor_code": vendor, "store_code": store["code"],
            "items": [{"helix_medication_id": drug}]}
    resp = client.post(f"{BASE}/purchase-orders/from-suggestions", headers=pharmacist["hdr"],
                       json=body)
    assert resp.status_code == 422, "a rate is never invented"
    body["items"][0]["unit_rate_paise"] = 2500
    resp = client.post(f"{BASE}/purchase-orders/from-suggestions", headers=pharmacist["hdr"],
                       json=body)
    assert resp.status_code == 200, resp.text
    doc = resp.json()["purchase_order"]
    assert doc["status"] == "draft"
    assert doc["lines"][0]["quantity_packs"] == 2 and doc["lines"][0]["unit_rate_paise"] == 2500
    created = _audits("pharmacy_po_created", doc["id"])
    assert json.loads(created[0]["metadata_json"])["source"] == "reorder_suggestions"

    # On order now: 5 on the shelf plus 20 ordered is above par.
    again = client.get(f"{BASE}/purchase-orders/reorder-suggestions", headers=pharmacist["hdr"],
                       params={"store_id": store["id"]}).json()["suggestions"]
    assert all(s["drug_id"] != drug for s in again)
    resp = client.post(f"{BASE}/purchase-orders/from-suggestions", headers=pharmacist["hdr"],
                       json=body)
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "no_suggestion"

    # Cancelled, it is suggested again; a cancelled order's rate is not offered
    # as the last rate paid.
    client.post(f"{BASE}/purchase-orders/{doc['id']}/cancel", headers=pharmacist["hdr"],
                json={"reason": "Redo"})
    back = client.get(f"{BASE}/purchase-orders/reorder-suggestions", headers=pharmacist["hdr"],
                      params={"store_id": store["id"]}).json()["suggestions"]
    assert [s["suggested_packs"] for s in back if s["drug_id"] == drug] == [2]
    assert [s["last_rate_paise"] for s in back if s["drug_id"] == drug] == [None]


def test_a_suggestion_for_an_unorderable_drug_says_why(pharmacist, stores_made):
    store = _store(stores_made)
    drug = _drug(drug_schedule=None)
    with db_session() as db:
        db.execute("INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units) "
                   "VALUES (?, ?, 20)", (store["id"], drug))
    rows = client.get(f"{BASE}/purchase-orders/reorder-suggestions", headers=pharmacist["hdr"],
                      params={"store_id": store["id"]}).json()["suggestions"]
    mine = next(s for s in rows if s["drug_id"] == drug)
    assert mine["orderable"] is False
    assert [p["code"] for p in mine["problems"]] == ["drug_unclassified"]


def test_the_form_options_say_what_can_be_ordered(pharmacist, stores_made):
    good, bad = _drug(), _drug(pack_size_units=None)
    expired = _vendor(valid_to=(TODAY - timedelta(days=3)).isoformat())
    _store(stores_made, "returns_quarantine")
    body = client.get(f"{BASE}/purchase-orders/options", headers=pharmacist["hdr"]).json()
    drugs = {d["id"]: d for d in body["drugs"]}
    assert drugs[good]["orderable"] is True
    assert drugs[bad]["orderable"] is False
    assert [p["code"] for p in drugs[bad]["problems"]] == ["pack_size_not_configured"]
    vendor = next(v for v in body["vendors"] if v["code"] == expired)
    assert vendor["licence_status"] == "expired" and vendor["orderable"] is False
    assert all(s["pharmacy_kind"] in purchasing.ORDERABLE_STORE_KINDS for s in body["stores"])
