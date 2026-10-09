"""Returns to vendor (grn-22): /api/pharmacy-store/vendor-returns, behind
ORB_PHARMACY_STORE.

Pinned (docs/his_design/grn.json, grn-22 tests, and the strict defaults the
service lists for Samuel):
  * with the module off every route answers 404 module_disabled; the read
    tier lists, the write tier drafts, the approve tier posts;
  * only lots held off the shelf, expired or near expiry go back; an in-date
    lot is refused;
  * a draft moves nothing; posting reduces the lot by a vendor_return
    movement and numbers the document from PHVR; a held lot left empty is
    closed as returned to the vendor;
  * a return never exceeds what the lot holds, at draft and again at post;
  * an idempotent replay drafts once and posts once; the same key for another
    return is refused;
  * the drafter never posts; a lot from another vendor is refused;
  * the credit note is recorded once; only a draft is cancelled;
  * a narcotic return needs the NDPS permission to draft, the vendor's NDPS
    licence, and a third person's PIN at posting; with the register on it is
    entered in the register;
  * every write commits with its 'security' audit row or not at all;
  * reconcile() is clean over every lot a return touched.
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
from services import audit_service, ndps_service
from services import pharmacy_purchasing_service as purchasing
from services import pharmacy_stock_service as stock
from services import pin_security
from services import procurement_service as procurement
from services.audit_service import AuditWriteError
from services.helix_db import init_helix_db

client = TestClient(app)
BASE = "/api/pharmacy-store"
PIN = "6284"


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


def _days(n: int) -> str:
    return (date.fromisoformat(stock.site_today()) + timedelta(days=n)).isoformat()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture(autouse=True)
def module_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
    # The NDPS register's lane follows the deployment profile; the register rows
    # these tests write must be 'non_production' (deletable) under any suite profile.
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev")


def _user(role, *, pin=PIN):
    uid = f"USRV{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, pin_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, hash_password("secretpass"),
                    pin_security.hash_pin(pin) if pin else None))
    return {"id": uid, "role": role,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


@pytest.fixture
def ph():
    return _user("pharmacist")


@pytest.fixture
def ph_b():
    return _user("pharmacist")


@pytest.fixture
def admin():
    return _user("admin")


class _Made:
    def __init__(self):
        self.stores: list[int] = []
        self.drugs: list[str] = []
        self.vendors: list[int] = []


@pytest.fixture
def made():
    m = _Made()
    yield m
    with db_session() as db:
        for store_id in m.stores:
            db.execute("DELETE FROM pharmacy_vendor_return_lines WHERE pharmacy_vendor_return_id "
                       "IN (SELECT id FROM pharmacy_vendor_returns WHERE store_id = ?)",
                       (store_id,))
            db.execute("DELETE FROM pharmacy_vendor_returns WHERE store_id = ?", (store_id,))
        for drug in m.drugs:
            db.execute("DELETE FROM ndps_register WHERE helix_medication_id = ? "
                       "AND lane = 'non_production'", (drug,))
            db.execute("DELETE FROM ndps_variances WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_stock_movements WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_lot_events WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_inventory_lots WHERE medication_id = ?", (drug,))
        for store_id in m.stores:
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))
        for vendor_id in m.vendors:
            db.execute("DELETE FROM vendors WHERE id = ?", (vendor_id,))


def _drug(m, **fields) -> str:
    med = f"MEDVR{_uid()}"
    cols = {"id": med, "name": f"Returntest {med[-6:]} 5 mg", "generic_name": "returnol",
            "form": "tablet", "strength": "5 mg", "drug_schedule": "H",
            "ndps_category": "none", "pack_size_units": 10} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    m.drugs.append(med)
    return med


def _store(m, kind="sub_store") -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(db, code=f"VR{_uid()}", name=f"Return {kind} {_uid()}",
                                             domain="pharmacy", pharmacy_kind=kind)
    m.stores.append(row["id"])
    return dict(row)


def _vendor(m, *, ndps=False) -> dict:
    code = f"VRV{_uid()}"
    with db_session() as db:
        vid = db.execute(
            "INSERT INTO vendors (code, name, is_active, drug_licence_numbers, "
            "drug_licence_valid_to, ndps_licence_number) VALUES (?, ?, 1, 'DL-20B-1', ?, ?)",
            (code, f"Return vendor {code}", _days(400), "NDPS-77" if ndps else None)).lastrowid
    m.vendors.append(vid)
    return {"id": vid, "code": code}


def _lot(drug, store_id, units, expiry, *, vendor=None, status="available", cost=120):
    with db_session(immediate=True) as db:
        lot = stock.open_or_merge_lot(db, medication_id=drug, lot_number=f"VB{_uid()}",
                                      expiration_date=expiry, units=units, actor_id="tester",
                                      store_id=store_id, unit_cost_paise=cost,
                                      vendor_id=vendor["id"] if vendor else None,
                                      movement_type="receipt")["lot"]
        if status != "available":
            stock.set_lot_status(db, lot["id"], status, actor_id="tester",
                                 reason="Supplier recall notice")
    return lot


def _qty(lot_id) -> int:
    with db_session() as db:
        return int(db.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                              (lot_id,)).fetchone()[0])


def _status(lot_id) -> str:
    with db_session() as db:
        return db.execute("SELECT status FROM helix_inventory_lots WHERE id = ?",
                          (lot_id,)).fetchone()[0]


def _create(user, store, vendor, lines, **extra):
    body = {"store_code": store["code"], "vendor_code": vendor["code"],
            "reason": "Supplier recall", "lines": [{"lot_id": lot, "quantity_units": units}
                                                   for lot, units in lines]} | extra
    return client.post(f"{BASE}/vendor-returns", headers=user["hdr"], json=body)


def _drafted(user, store, vendor, lines, **extra) -> dict:
    resp = _create(user, store, vendor, lines, **extra)
    assert resp.status_code == 200, resp.text
    return resp.json()["vendor_return"]


def _post(user, rid, **witness):
    return client.post(f"{BASE}/vendor-returns/{rid}/post", headers=user["hdr"], json=witness)


def _movements(rid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM helix_stock_movements WHERE reference_type = ? AND reference_id = ? "
            "ORDER BY seq", (purchasing.VR_REFERENCE, str(rid))).fetchall()]


def _audits(action, rid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(rid))).fetchall()]


def _reconcile(lot_ids):
    with db_session() as db:
        return stock.reconcile(db, lot_ids=list(lot_ids))


def _err(resp):
    return resp.json()["detail"]["error"]


# ═════════════════════════════════════════════════════════════════════════════
#  The switch and the tiers
# ═════════════════════════════════════════════════════════════════════════════

def test_every_vendor_return_route_is_404_with_the_module_off(monkeypatch, ph):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    for method, path in (("get", "/vendor-returns"), ("get", "/vendor-returns/options"),
                         ("get", "/vendor-returns/stock?store_code=X"),
                         ("get", "/vendor-returns/1"), ("post", "/vendor-returns"),
                         ("post", "/vendor-returns/1/post"), ("post", "/vendor-returns/1/credit"),
                         ("post", "/vendor-returns/1/cancel")):
        resp = getattr(client, method)(f"{BASE}{path}", headers=ph["hdr"],
                                       **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 404, (path, resp.text)
        assert _err(resp) == "module_disabled"


def test_the_tiers(ph, admin, made):
    nurse = _user("nurse")
    assert client.get(f"{BASE}/vendor-returns", headers=nurse["hdr"]).status_code == 403
    assert client.get(f"{BASE}/vendor-returns", headers=admin["hdr"]).status_code == 200
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    lot = _lot(drug, store["id"], 10, _days(20), vendor=vendor)
    assert _create(admin, store, vendor, [(lot["id"], 2)]).status_code == 403, \
        "the administrator approves; the pharmacist drafts"
    doc = _drafted(ph, store, vendor, [(lot["id"], 2)])
    resp = _post(ph, doc["id"])
    assert resp.status_code == 403, "posting needs the approve tier"
    assert _qty(lot["id"]) == 10


# ═════════════════════════════════════════════════════════════════════════════
#  Draft, post, number, close
# ═════════════════════════════════════════════════════════════════════════════

def test_a_return_reduces_the_lot_and_numbers_the_document(ph, admin, made):
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    held = _lot(drug, store["id"], 6, _days(200), vendor=vendor, status="quarantined")
    near = _lot(drug, store["id"], 10, _days(30), vendor=vendor, cost=300)
    doc = _drafted(ph, store, vendor, [(held["id"], 6), (near["id"], 4)])
    assert doc["status"] == "draft" and doc["return_number"] is None
    assert doc["total_units"] == 10 and doc["total_paise"] == 6 * 120 + 4 * 300
    assert [line["reason"] for line in doc["lines"]] == ["Held off the shelf", "Near expiry"]
    assert _movements(doc["id"]) == [], "a draft moves nothing"
    assert (_qty(held["id"]), _qty(near["id"])) == (6, 10)
    assert _audits("pharmacy_vendor_return_created", doc["id"])

    resp = _post(admin, doc["id"])
    assert resp.status_code == 200, resp.text
    sent = resp.json()["vendor_return"]
    assert sent["status"] == "posted" and sent["posted_by"] == admin["id"]
    assert sent["return_number"] and sent["return_number"].startswith("PHVR")
    assert (_qty(held["id"]), _qty(near["id"])) == (0, 6)
    assert _status(held["id"]) == "returned_to_vendor", "an emptied held lot is closed"
    assert _status(near["id"]) == "available", "a part return leaves the lot on the shelf"
    moves = _movements(doc["id"])
    assert [(m["movement_type"], m["quantity_change"], m["lot_id"]) for m in moves] == [
        ("vendor_return", -6, held["id"]), ("vendor_return", -4, near["id"])]
    assert all(m["actor_id"] == admin["id"] for m in moves)
    meta = json.loads(_audits("pharmacy_vendor_return_posted", doc["id"])[-1]["metadata_json"])
    assert [line["lot_closed"] for line in meta["lines"]] == [True, False]
    assert _reconcile([held["id"], near["id"]])["clean"]


def test_an_in_date_lot_does_not_go_back(ph, made):
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    lot = _lot(drug, store["id"], 10, _days(300), vendor=vendor)
    resp = _create(ph, store, vendor, [(lot["id"], 1)])
    assert resp.status_code == 422 and _err(resp) == "lot_not_returnable"
    assert resp.json()["detail"]["why"] == "in_date"
    stock_now = client.get(f"{BASE}/vendor-returns/stock", headers=ph["hdr"],
                           params={"store_code": store["code"]}).json()
    assert lot["id"] not in {r["lot_id"] for r in stock_now["lots"]}


def test_the_returnable_stock_lists_held_expired_and_near_expiry_lots(ph, made):
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    held = _lot(drug, store["id"], 3, _days(400), vendor=vendor, status="recalled")
    near = _lot(drug, store["id"], 3, _days(10), vendor=vendor)
    _lot(drug, store["id"], 3, _days(200), vendor=vendor)
    body = client.get(f"{BASE}/vendor-returns/stock", headers=ph["hdr"],
                      params={"store_code": store["code"]}).json()
    rows = {r["lot_id"]: r for r in body["lots"]}
    assert set(rows) == {held["id"], near["id"]}
    assert rows[held["id"]]["why_label"] == "Held off the shelf"
    assert rows[near["id"]]["why"] == "near_expiry" and rows[near["id"]]["days_left"] == 10
    assert body["near_expiry_days"] == config.PHARMACY_NEAR_EXPIRY_DAYS


def test_a_return_cannot_exceed_the_lot(ph, admin, made):
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    lot = _lot(drug, store["id"], 5, _days(15), vendor=vendor)
    resp = _create(ph, store, vendor, [(lot["id"], 6)])
    assert resp.status_code == 409 and _err(resp) == "exceeds_lot"
    doc = _drafted(ph, store, vendor, [(lot["id"], 5)])
    with db_session(immediate=True) as db:      # units leave the lot after the draft
        stock.post_movement(db, lot_id=lot["id"], movement_type="transfer_out",
                            quantity_change=-2, actor_id="tester")
    resp = _post(admin, doc["id"])
    assert resp.status_code == 409 and _err(resp) == "exceeds_lot"
    assert _qty(lot["id"]) == 3 and _movements(doc["id"]) == []
    with db_session() as db:
        assert db.execute("SELECT status FROM pharmacy_vendor_returns WHERE id = ?",
                          (doc["id"],)).fetchone()[0] == "draft"


def test_an_idempotent_replay_posts_once(ph, admin, made):
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    lot = _lot(drug, store["id"], 8, _days(20), vendor=vendor)
    key = f"vr-{uuid.uuid4().hex}"
    first = _create(ph, store, vendor, [(lot["id"], 3)], idempotency_key=key)
    again = client.post(f"{BASE}/vendor-returns", headers=ph["hdr"] | {"Idempotency-Key": key},
                        json={"store_code": store["code"], "vendor_code": vendor["code"],
                              "reason": "Supplier recall",
                              "lines": [{"lot_id": lot["id"], "quantity_units": 3}]})
    assert first.status_code == again.status_code == 200
    assert again.json()["replayed"] is True
    rid = first.json()["vendor_return"]["id"]
    assert again.json()["vendor_return"]["id"] == rid
    other = _create(ph, store, vendor, [(lot["id"], 2)], idempotency_key=key)
    assert other.status_code == 409 and _err(other) == "idempotency_conflict"

    assert _post(admin, rid).status_code == 200
    twice = _post(admin, rid)
    assert twice.status_code == 200 and twice.json()["replayed"] is True
    assert len(_movements(rid)) == 1 and _qty(lot["id"]) == 5
    assert len(_audits("pharmacy_vendor_return_posted", rid)) == 1
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM pharmacy_vendor_returns WHERE "
                          "idempotency_key = ?", (key,)).fetchone()[0] == 1
    assert _reconcile([lot["id"]])["clean"]


def test_the_drafter_never_posts(ph, made):
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    lot = _lot(drug, store["id"], 4, _days(20), vendor=vendor)
    doc = _drafted(ph, store, vendor, [(lot["id"], 4)])
    with pytest.raises(purchasing.PurchasingError) as caught:
        purchasing.post_vendor_return(doc["id"], actor={"id": ph["id"], "role": "admin"})
    assert caught.value.code == "same_person"
    assert _qty(lot["id"]) == 4


def test_a_lot_goes_back_only_to_its_own_vendor(ph, made):
    store = _store(made)
    supplier, other = _vendor(made), _vendor(made)
    drug = _drug(made)
    lot = _lot(drug, store["id"], 4, _days(20), vendor=supplier)
    resp = _create(ph, store, other, [(lot["id"], 1)])
    assert resp.status_code == 422 and _err(resp) == "lot_from_another_vendor"
    legacy = _lot(drug, store["id"], 4, _days(25))
    resp = _create(ph, store, other, [(legacy["id"], 1)])
    assert resp.status_code == 200, resp.text
    assert [w["code"] for w in resp.json()["warnings"]] == ["vendor_not_recorded"]
    elsewhere = _store(made)
    resp = _create(ph, elsewhere, supplier, [(lot["id"], 1)])
    assert resp.status_code == 422 and _err(resp) == "lot_not_in_store"


def test_credit_note_and_cancel(ph, admin, made):
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    lot = _lot(drug, store["id"], 9, _days(20), vendor=vendor)
    doc = _drafted(ph, store, vendor, [(lot["id"], 2)])
    path = f"{BASE}/vendor-returns/{doc['id']}"
    resp = client.post(f"{path}/credit", headers=ph["hdr"], json={"credit_note_ref": "CN-1"})
    assert resp.status_code == 409 and _err(resp) == "illegal_transition"
    assert _post(admin, doc["id"]).status_code == 200
    resp = client.post(f"{path}/cancel", headers=ph["hdr"], json={"reason": "Changed mind"})
    assert resp.status_code == 409, "a return that was sent is never cancelled"
    resp = client.post(f"{path}/credit", headers=admin["hdr"], json={"credit_note_ref": ""})
    assert resp.status_code == 422
    resp = client.post(f"{path}/credit", headers=admin["hdr"],
                       json={"credit_note_ref": "CN-2026-118"})
    assert resp.status_code == 200, resp.text
    body = resp.json()["vendor_return"]
    assert body["status"] == "credited" and body["vendor_credit_note_ref"] == "CN-2026-118"
    same = client.post(f"{path}/credit", headers=ph["hdr"], json={"credit_note_ref": "CN-2026-118"})
    assert same.status_code == 200 and same.json()["replayed"] is True
    other = client.post(f"{path}/credit", headers=ph["hdr"], json={"credit_note_ref": "CN-9"})
    assert other.status_code == 409 and _err(other) == "already_credited"
    assert len(_audits("pharmacy_vendor_return_credited", doc["id"])) == 1

    draft = _drafted(ph, store, vendor, [(lot["id"], 1)])
    dpath = f"{BASE}/vendor-returns/{draft['id']}"
    resp = client.post(f"{dpath}/cancel", headers=ph["hdr"], json={})
    assert resp.status_code == 422 and _err(resp) == "missing_reason"
    resp = client.post(f"{dpath}/cancel", headers=ph["hdr"], json={"reason": "Vendor will collect"})
    assert resp.status_code == 200 and resp.json()["vendor_return"]["status"] == "cancelled"
    assert _post(admin, draft["id"]).status_code == 409
    assert _qty(lot["id"]) == 7


def test_a_failed_audit_write_rolls_the_post_back(monkeypatch, ph, admin, made):
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    lot = _lot(drug, store["id"], 5, _days(20), vendor=vendor, status="quarantined")
    doc = _drafted(ph, store, vendor, [(lot["id"], 5)])
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "pharmacy_vendor_return_posted":
            raise AuditWriteError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    resp = _post(admin, doc["id"])
    assert resp.status_code == 503 and _err(resp) == "audit_unavailable"
    assert _qty(lot["id"]) == 5 and _status(lot["id"]) == "quarantined"
    assert _movements(doc["id"]) == []
    with db_session() as db:
        row = db.execute("SELECT status, return_number FROM pharmacy_vendor_returns WHERE id = ?",
                         (doc["id"],)).fetchone()
    assert (row["status"], row["return_number"]) == ("draft", None)


# ═════════════════════════════════════════════════════════════════════════════
#  Narcotic and psychotropic returns
# ═════════════════════════════════════════════════════════════════════════════

def test_a_narcotic_return_needs_the_ndps_licence_permission_and_a_third_witness(
        monkeypatch, ph, ph_b, admin, made):
    vault = _store(made, "ndps_vault")
    licensed, unlicensed = _vendor(made, ndps=True), _vendor(made)
    narcotic = _drug(made, ndps_category="narcotic", drug_schedule="X")
    lot = _lot(narcotic, vault["id"], 4, _days(500), vendor=licensed, status="quarantined")
    lot2 = _lot(narcotic, vault["id"], 2, _days(500), vendor=unlicensed, status="quarantined")
    resp = _create(ph, vault, unlicensed, [(lot2["id"], 2)])
    assert resp.status_code == 422 and _err(resp) == "vendor_ndps_licence_missing"
    with pytest.raises(purchasing.PurchasingError) as caught:
        purchasing.create_vendor_return(
            vendor_code=licensed["code"], store_code=vault["code"],
            lines=[{"lot_id": lot["id"], "quantity_units": 4}], actor={"id": "X1", "role": "admin"},
            reason="Recall")
    assert caught.value.code == "ndps_permission_needed"
    doc = _drafted(ph, vault, licensed, [(lot["id"], 4)])
    assert doc["controlled"] is True and doc["witness_required"] is True

    resp = _post(admin, doc["id"])
    assert resp.status_code == 422 and _err(resp) == "witness_required"
    resp = _post(admin, doc["id"], witness_id=ph["id"], witness_pin=PIN)
    assert resp.status_code == 403 and _err(resp) == "witness_same_person", \
        "the drafter does not witness their own return"
    resp = _post(admin, doc["id"], witness_id=ph_b["id"], witness_pin="0000")
    assert resp.status_code == 403 and _err(resp) == "witness_pin_wrong"
    assert _qty(lot["id"]) == 4

    monkeypatch.setattr(config, "PHARMACY_NDPS_REGISTER_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_NDPS_ON_FAILURE", "block")
    with db_session(immediate=True) as db:      # the vault's register, opened at the shelf
        ndps_service._insert_entry(db, lane="non_production", store_id=vault["id"],
                                   drug_id=narcotic, entry_type="opening", quantity=6,
                                   balance_after=6, actor_id="tester",
                                   occurred_at="2026-10-09T00:00:00Z")
    resp = _post(admin, doc["id"], witness_id=ph_b["id"], witness_pin=PIN)
    assert resp.status_code == 200, resp.text
    assert _qty(lot["id"]) == 0 and _status(lot["id"]) == "returned_to_vendor"
    with db_session() as db:
        entry = dict(db.execute(
            "SELECT * FROM ndps_register WHERE helix_medication_id = ? ORDER BY seq DESC "
            "LIMIT 1", (narcotic,)).fetchone())
    assert (entry["entry_type"], entry["quantity_units"], entry["balance_after_units"]) == (
        "vendor_return", -4, 2)
    assert entry["witness_id"] == ph_b["id"] and entry["actor_id"] == admin["id"]
    assert (entry["counterparty_type"], entry["counterparty_ref"]) == ("vendor",
                                                                     str(licensed["id"]))
    assert entry["document_ref"] == f"pharmacy_vendor_return:{doc['id']}"
    meta = json.loads(_audits("pharmacy_vendor_return_posted", doc["id"])[-1]["metadata_json"])
    assert meta["witness_id"] == ph_b["id"] and meta["controlled"] is True


# ═════════════════════════════════════════════════════════════════════════════
#  Reads
# ═════════════════════════════════════════════════════════════════════════════

def test_reads(ph, ph_b, admin, made):
    store, vendor = _store(made), _vendor(made)
    drug = _drug(made)
    lot = _lot(drug, store["id"], 4, _days(20), vendor=vendor)
    doc = _drafted(ph, store, vendor, [(lot["id"], 1)])
    rows = client.get(f"{BASE}/vendor-returns", headers=ph_b["hdr"],
                      params={"status": "draft"}).json()["vendor_returns"]
    assert doc["id"] in {r["id"] for r in rows} and "lines" not in rows[0]
    assert client.get(f"{BASE}/vendor-returns", headers=ph_b["hdr"],
                      params={"status": "nonsense"}).status_code == 422
    one = client.get(f"{BASE}/vendor-returns/{doc['id']}", headers=admin["hdr"]).json()
    assert one["vendor_return"]["lines"][0]["lot_id"] == lot["id"]
    assert client.get(f"{BASE}/vendor-returns/999999999", headers=ph["hdr"]).status_code == 404
    opts = client.get(f"{BASE}/vendor-returns/options", headers=admin["hdr"]).json()
    assert store["code"] in {s["code"] for s in opts["stores"]}
    assert vendor["code"] in {v["code"] for v in opts["vendors"]}
    assert ph["id"] in {w["id"] for w in opts["witnesses"]}
    assert admin["id"] not in {w["id"] for w in opts["witnesses"]}
    resp = client.get(f"{BASE}/vendor-returns/stock", headers=ph["hdr"],
                      params={"store_code": "NOSUCHSTORE"})
    assert resp.status_code == 422 and _err(resp) == "store_not_found"


def test_every_vendor_return_get_answers_cleanly_for_every_role():
    paths = ("/vendor-returns", "/vendor-returns?status=posted", "/vendor-returns/options",
             "/vendor-returns/stock?store_code=NOSUCHSTORE", "/vendor-returns/1",
             "/vendor-returns/999999999")
    for role in ("admin", "doctor", "nurse", "pharmacist", "ward_clerk", "cashier",
                 "receptionist", "lab_technician"):
        who = _user(role)
        for path in paths:
            resp = client.get(f"{BASE}{path}", headers=who["hdr"])
            assert resp.status_code < 500, (role, path, resp.text)
            if role not in ("admin", "pharmacist"):
                assert resp.status_code == 403, (role, path, resp.status_code)
