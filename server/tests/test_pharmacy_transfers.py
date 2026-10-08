"""Pharmacy-internal transfers (grn-11): /api/pharmacy-store/transfers, behind
ORB_PHARMACY_STORE.

Pinned (docs/his_design/grn.json, grn-11 tests, and the strict defaults the
service docstring lists):
  * with the module off every transfers route answers 404 module_disabled;
    the read tier lists, the write tier drafts, nobody else does;
  * the issue plan is FEFO over usable lots of the sending store only;
    expired, undated and quarantined lots never move, and short stock is
    refused with nothing written;
  * a draft moves nothing; dispatch posts one transfer_out per line, and the
    units are then in transit, counted by neither store and dispensable
    nowhere; a dispatch sent twice posts once;
  * each transfer_out and transfer_in pair sums to zero; a short receipt books
    what arrived and records the shortfall with its reason (out + in +
    shortfall = 0); over-receipt and a line received twice are refused;
  * the dispatcher receiving gets 403; a draft is cancelled with a reason and
    nothing else is;
  * a sub-store lot is never dispensed until it has been transferred to the
    dispensing shelf;
  * controlled stock (GRN-R14): a narcotic moves only between the vault and
    the dispensing pharmacy, the vault takes nothing else, an unclassified
    drug does not move, and a controlled dispatch or receipt needs the NDPS
    permission and a second user's PIN (self, wrong PIN, lockout, ineligible
    and unprovisioned witnesses refused);
  * every write commits with its 'security' audit row or not at all;
  * reconcile() stays clean over every lot a transfer touched.
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
from services import pharmacy_stock_reports as reports
from services import pharmacy_stock_service as stock
from services import pharmacy_transfer_service as transfers
from services import pin_security
from services import procurement_service as procurement
from services.helix_db import init_helix_db

client = TestClient(app)
BASE = "/api/pharmacy-store"
PIN = "4826"


def _today() -> date:
    return date.fromisoformat(stock.site_today())


def _days(n: int) -> str:
    return (_today() + timedelta(days=n)).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture(autouse=True)
def module_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)


def _user(role, *, pin=PIN):
    uid = f"USRT{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, pin_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, hash_password("secretpass"),
                    pin_security.hash_pin(pin) if pin else None))
    return {"id": uid, "role": role,
            "hdr": {"Authorization": f"Bearer {create_token(uid, role)}"}}


@pytest.fixture
def ph_a():
    return _user("pharmacist")


@pytest.fixture
def ph_b():
    return _user("pharmacist")


@pytest.fixture
def ph_c():
    return _user("pharmacist")


class _Made:
    def __init__(self):
        self.stores: list[int] = []
        self.drugs: list[str] = []


@pytest.fixture
def made():
    """Everything a test creates, removed afterwards: the lots, movements and
    lot events of its drugs (wherever they sit), the transfers touching its
    stores or drugs, then the stores it made."""
    m = _Made()
    yield m
    with db_session() as db:
        for drug in m.drugs:
            db.execute("DELETE FROM pharmacy_transfer_lines WHERE helix_medication_id = ?",
                       (drug,))
            db.execute("DELETE FROM helix_stock_movements WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_lot_events WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_inventory_lots WHERE medication_id = ?", (drug,))
        for store_id in m.stores:
            db.execute("DELETE FROM pharmacy_transfer_lines WHERE pharmacy_transfer_id IN "
                       "(SELECT id FROM pharmacy_transfers WHERE to_store_id = ? "
                       "OR from_store_id = ?)", (store_id, store_id))
            db.execute("DELETE FROM pharmacy_transfers WHERE to_store_id = ? OR from_store_id = ?",
                       (store_id, store_id))
        db.execute("DELETE FROM pharmacy_transfers WHERE id NOT IN "
                   "(SELECT pharmacy_transfer_id FROM pharmacy_transfer_lines) "
                   "AND created_by LIKE 'USRT%'")
        for store_id in m.stores:
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def _drug(m: _Made, **fields) -> str:
    med = f"MEDTR{_uid()}"
    cols = {"id": med, "name": f"Transfertest {med[-6:]} 5 mg", "generic_name": "transferol",
            "form": "tablet", "strength": "5 mg", "drug_schedule": "H",
            "ndps_category": "none", "pack_size_units": 10} | fields
    with db_session() as db:
        db.execute(f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    m.drugs.append(med)
    return med


def _store(m: _Made, kind="sub_store") -> dict:
    with db_session(immediate=True) as db:
        row = procurement._upsert_store_core(db, code=f"TR{_uid()}", name=f"Transfer {kind} "
                                             f"{_uid()}", domain="pharmacy", pharmacy_kind=kind)
    m.stores.append(row["id"])
    return dict(row)


@pytest.fixture
def dispensing(made):
    """The active dispensing store: the one already there, or a new one made
    without adopting legacy lots, removed afterwards."""
    with db_session() as db:
        row = db.execute("SELECT * FROM stores WHERE pharmacy_kind = 'dispensing' "
                         "AND is_active = 1").fetchone()
    if row is not None:
        return dict(row)
    return _store(made, "dispensing")


def _lot(drug, store_id, units, expiry, *, status="available", cost=None, batch=None):
    with db_session(immediate=True) as db:
        out = stock.open_or_merge_lot(db, medication_id=drug, lot_number=batch or f"B{_uid()}",
                                      expiration_date=expiry, units=units, actor_id="tester",
                                      store_id=store_id, status=status, unit_cost_paise=cost,
                                      units_per_pack=10)
    return out["lot"]


def _qty(lot_id) -> int:
    with db_session() as db:
        return int(db.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                              (lot_id,)).fetchone()[0])


def _create(user, from_store, to_store, lines, **extra):
    body = {"from_store_code": from_store["code"], "to_store_code": to_store["code"],
            "lines": [{"helix_medication_id": d, "quantity_units": u} for d, u in lines],
            **extra}
    return client.post(f"{BASE}/transfers", headers=user["hdr"], json=body)


def _drafted(user, from_store, to_store, lines, **extra) -> dict:
    resp = _create(user, from_store, to_store, lines, **extra)
    assert resp.status_code == 200, resp.text
    return resp.json()["transfer"]


def _dispatch(user, tid, **witness):
    return client.post(f"{BASE}/transfers/{tid}/dispatch", headers=user["hdr"], json=witness)


def _receive(user, tid, lines, **witness):
    return client.post(f"{BASE}/transfers/{tid}/receive", headers=user["hdr"],
                       json={"lines": lines, **witness})


def _full(doc):
    return [{"line_id": line["id"], "received_units": line["quantity_units"]}
            for line in doc["lines"]]


def _movements(tid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM helix_stock_movements WHERE reference_type = 'pharmacy_transfer' "
            "AND reference_id = ? ORDER BY seq", (str(tid),)).fetchall()]


def _audits(action, tid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(tid))).fetchall()]


def _balance(tid):
    with db_session() as db:
        return transfers.transfer_balance(db, tid)


def _reconcile(lot_ids):
    with db_session() as db:
        return stock.reconcile(db, lot_ids=list(lot_ids))


def _touched_lots(tid):
    with db_session() as db:
        rows = db.execute("SELECT from_lot_id, to_lot_id FROM pharmacy_transfer_lines "
                          "WHERE pharmacy_transfer_id = ?", (tid,)).fetchall()
    return {x for r in rows for x in (r["from_lot_id"], r["to_lot_id"]) if x}


def _err(resp):
    return resp.json()["detail"]["error"]


# ═════════════════════════════════════════════════════════════════════════════
#  The switch and the tiers
# ═════════════════════════════════════════════════════════════════════════════

def test_every_transfers_route_is_404_with_the_module_off(monkeypatch, ph_a):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    for method, path in (("get", "/transfers"), ("get", "/transfers/options"),
                         ("get", "/transfers/stock?store_code=X"), ("get", "/transfers/1"),
                         ("post", "/transfers"), ("post", "/transfers/1/replan"),
                         ("post", "/transfers/1/dispatch"), ("post", "/transfers/1/receive"),
                         ("post", "/transfers/1/cancel")):
        resp = getattr(client, method)(f"{BASE}{path}", headers=ph_a["hdr"],
                                       **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 404, (path, resp.text)
        assert _err(resp) == "module_disabled"


def test_only_the_read_tier_lists_and_only_the_write_tier_drafts(made, dispensing):
    nurse = _user("nurse")
    assert client.get(f"{BASE}/transfers", headers=nurse["hdr"]).status_code == 403
    admin = _user("admin")
    assert client.get(f"{BASE}/transfers", headers=admin["hdr"]).status_code == 200
    sub = _store(made)
    drug = _drug(made)
    _lot(drug, sub["id"], 10, _days(200))
    resp = _create(admin, sub, dispensing, [(drug, 5)])
    assert resp.status_code == 403, "admin holds read and approve, not write"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM pharmacy_transfer_lines WHERE "
                          "helix_medication_id = ?", (drug,)).fetchone()[0] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  The FEFO issue plan
# ═════════════════════════════════════════════════════════════════════════════

def test_the_plan_is_fefo_over_usable_lots_of_the_sending_store(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    other = _store(made)
    late = _lot(drug, sub["id"], 10, _days(300))
    soon = _lot(drug, sub["id"], 3, _days(30))
    middle = _lot(drug, sub["id"], 5, _days(100))
    _lot(drug, sub["id"], 4, _days(-2))                       # expired
    _lot(drug, sub["id"], 6, _days(10), status="quarantined")
    _lot(drug, other["id"], 50, _days(5))                     # another store's lot
    doc = _drafted(ph_a, sub, dispensing, [(drug, 10)])
    assert doc["status"] == "draft"
    plan = [(line["from_lot_id"], line["quantity_units"]) for line in doc["lines"]]
    assert plan == [(soon["id"], 3), (middle["id"], 5), (late["id"], 2)]
    assert [line["expiry_date"] for line in doc["lines"]] == [_days(30), _days(100), _days(300)]
    # A draft moves nothing and is not in transit.
    assert (_qty(soon["id"]), _qty(middle["id"]), _qty(late["id"])) == (3, 5, 10)
    assert _movements(doc["id"]) == []
    with db_session() as db:
        assert reports.in_transit_units(db).get((dispensing["id"], drug)) is None
    assert _audits("pharmacy_transfer_created", doc["id"])


def test_short_stock_is_refused_and_nothing_is_written(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    _lot(drug, sub["id"], 4, _days(100))
    _lot(drug, sub["id"], 9, _days(-1))
    resp = _create(ph_a, sub, dispensing, [(drug, 5)])
    assert resp.status_code == 409 and _err(resp) == "insufficient_stock"
    short = resp.json()["detail"]["short"]
    assert short[0]["usable_units"] == 4 and short[0]["asked_units"] == 5
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM pharmacy_transfer_lines WHERE "
                          "helix_medication_id = ?", (drug,)).fetchone()[0] == 0


def test_store_and_line_shapes_are_checked(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    _lot(drug, sub["id"], 10, _days(100))
    assert _err(_create(ph_a, sub, sub, [(drug, 1)])) == "same_store"
    ward = _store(made, "returns_quarantine")
    resp = _create(ph_a, sub, ward, [(drug, 1)])
    assert resp.status_code == 422 and _err(resp) == "store_not_transferable"
    resp = _create(ph_a, sub, dispensing, [(drug, 1), (drug, 2)])
    assert _err(resp) == "duplicate_line"
    resp = _create(ph_a, sub, dispensing, [(drug, 0)])
    assert resp.status_code == 422 and _err(resp) == "bad_value"
    with db_session() as db:
        db.execute("UPDATE stores SET is_active = 0 WHERE id = ?", (sub["id"],))
    resp = _create(ph_a, sub, dispensing, [(drug, 1)])
    assert resp.status_code == 409 and _err(resp) == "store_inactive"


# ═════════════════════════════════════════════════════════════════════════════
#  Dispatch, in transit, receipt
# ═════════════════════════════════════════════════════════════════════════════

def test_in_transit_units_are_dispensable_nowhere_and_land_on_receipt(ph_a, ph_b, made,
                                                                       dispensing):
    drug = _drug(made)
    sub = _store(made)
    lot = _lot(drug, sub["id"], 12, _days(200), cost=250)
    with db_session() as db:
        assert stock.dispensable_units(db, drug) == 0
        assert stock.pick_fefo_lot(db, drug) is None, "a sub-store lot is never dispensed"
    doc = _drafted(ph_a, sub, dispensing, [(drug, 10)])
    resp = _dispatch(ph_a, doc["id"])
    assert resp.status_code == 200, resp.text
    sent = resp.json()["transfer"]
    assert sent["status"] == "dispatched" and sent["dispatched_by"] == ph_a["id"]
    assert sent["in_transit_units"] == 10
    assert _qty(lot["id"]) == 2
    outs = _movements(doc["id"])
    assert [(m["movement_type"], m["quantity_change"], m["lot_id"]) for m in outs] == [
        ("transfer_out", -10, lot["id"])]
    assert outs[0]["actor_id"] == ph_a["id"] and outs[0]["unit_cost_paise"] == 250
    with db_session() as db:
        assert stock.dispensable_units(db, drug) == 0, "in transit is dispensable nowhere"
        assert reports.in_transit_units(db)[(dispensing["id"], drug)] == 10
        rows = reports.stock_by_store(db, drug_id=drug)["rows"]
    assert sum(r["on_hand_units"] for r in rows) == 2, "counted by neither store"

    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 200, resp.text
    got = resp.json()["transfer"]
    assert got["status"] == "received" and got["received_by"] == ph_b["id"]
    assert got["in_transit_units"] == 0
    line = got["lines"][0]
    assert line["received_units"] == 10 and line["shortfall_units"] == 0
    with db_session() as db:
        landed = dict(db.execute("SELECT * FROM helix_inventory_lots WHERE id = ?",
                                 (line["to_lot_id"],)).fetchone())
        assert stock.dispensable_units(db, drug) == 10
        picked = stock.pick_fefo_lot(db, drug)
        assert reports.in_transit_units(db).get((dispensing["id"], drug)) is None
    assert landed["store_id"] == dispensing["id"] and landed["lot_number"] == lot["lot_number"]
    assert landed["expiration_date"] == lot["expiration_date"] and landed["status"] == "available"
    assert landed["unit_cost_paise"] == 250
    assert picked["id"] == landed["id"], "transferred stock is dispensed from the shelf"
    moves = _movements(doc["id"])
    assert sum(m["quantity_change"] for m in moves) == 0, "the pair sums to zero"
    assert [m["movement_type"] for m in moves] == ["transfer_out", "transfer_in"]
    assert _balance(doc["id"])["balanced"]
    assert _reconcile(_touched_lots(doc["id"]))["clean"]
    assert _audits("pharmacy_transfer_dispatched", doc["id"])
    assert _audits("pharmacy_transfer_received", doc["id"])


def test_a_dispatch_sent_twice_posts_once(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    first = _lot(drug, sub["id"], 4, _days(50))
    second = _lot(drug, sub["id"], 20, _days(90))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 9)])
    assert _dispatch(ph_a, doc["id"]).status_code == 200
    again = _dispatch(ph_a, doc["id"])
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert len(_movements(doc["id"])) == 2
    assert (_qty(first["id"]), _qty(second["id"])) == (0, 15)
    assert len(_audits("pharmacy_transfer_dispatched", doc["id"])) == 1
    with db_session() as db:
        number = db.execute("SELECT transfer_number FROM pharmacy_transfers WHERE id = ?",
                            (doc["id"],)).fetchone()[0]
    assert number and number.startswith("PHTRF"), "numbered from its own series at dispatch"


def test_the_dispatcher_receiving_gets_403(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    _lot(drug, sub["id"], 5, _days(60))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 5)])
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    resp = _receive(ph_a, doc["id"], _full(sent))
    assert resp.status_code == 403 and _err(resp) == "same_person"
    assert [m["movement_type"] for m in _movements(doc["id"])] == ["transfer_out"]
    with db_session() as db:
        assert db.execute("SELECT status FROM pharmacy_transfers WHERE id = ?",
                          (doc["id"],)).fetchone()[0] == "dispatched"


def test_partial_and_short_receipt(ph_a, ph_b, made, dispensing):
    drug_a = _drug(made)
    drug_b = _drug(made)
    sub = _store(made)
    _lot(drug_a, sub["id"], 6, _days(80))
    _lot(drug_b, sub["id"], 8, _days(80))
    doc = _drafted(ph_a, sub, dispensing, [(drug_a, 6), (drug_b, 8)])
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    line_a, line_b = sent["lines"]

    resp = _receive(ph_b, doc["id"], [{"line_id": line_a["id"], "received_units": 6}])
    part = resp.json()["transfer"]
    assert part["status"] == "partially_received" and part["in_transit_units"] == 8
    with db_session() as db:
        assert reports.in_transit_units(db)[(dispensing["id"], drug_b)] == 8
        assert reports.in_transit_units(db).get((dispensing["id"], drug_a)) is None

    resp = _receive(ph_b, doc["id"], [{"line_id": line_a["id"], "received_units": 6}])
    assert resp.status_code == 409 and _err(resp) == "line_already_received"
    resp = _receive(ph_b, doc["id"], [{"line_id": line_b["id"], "received_units": 9}])
    assert resp.status_code == 422 and _err(resp) == "over_receipt"
    resp = _receive(ph_b, doc["id"], [{"line_id": line_b["id"], "received_units": 5}])
    assert resp.status_code == 422 and _err(resp) == "discrepancy_reason_required"

    resp = _receive(ph_b, doc["id"], [{"line_id": line_b["id"], "received_units": 5,
                                       "discrepancy_reason": "Three strips crushed in the box"}])
    assert resp.status_code == 200, resp.text
    done = resp.json()["transfer"]
    assert done["status"] == "received" and done["in_transit_units"] == 0
    short = done["lines"][1]
    assert short["received_units"] == 5 and short["shortfall_units"] == 3
    assert short["discrepancy_reason"] == "Three strips crushed in the box"
    moves = _movements(doc["id"])
    assert sum(m["quantity_change"] for m in moves) == -3, "out + in + shortfall = 0"
    balance = _balance(doc["id"])
    assert balance["balanced"] and [ln["shortfall_units"] for ln in balance["lines"]] == [0, 3]
    audit = _audits("pharmacy_transfer_received", doc["id"])[-1]
    meta = json.loads(audit["metadata_json"])
    assert meta["shortfall_units"] == 3 and meta["lines"][0]["reason"]
    assert _reconcile(_touched_lots(doc["id"]))["clean"]


def test_nothing_arrived_is_a_receipt_of_zero_with_a_reason(ph_a, ph_b, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    _lot(drug, sub["id"], 4, _days(80))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 4)])
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    resp = _receive(ph_b, doc["id"], [{"line_id": sent["lines"][0]["id"], "received_units": 0,
                                       "discrepancy_reason": "Box never reached the pharmacy"}])
    assert resp.status_code == 200, resp.text
    line = resp.json()["transfer"]["lines"][0]
    assert line["to_lot_id"] is None and line["shortfall_units"] == 4
    assert [m["movement_type"] for m in _movements(doc["id"])] == ["transfer_out"]
    assert _balance(doc["id"])["balanced"]


def test_cancel_only_a_draft_and_only_with_a_reason(ph_a, ph_b, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    lot = _lot(drug, sub["id"], 10, _days(80))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 4)])
    resp = client.post(f"{BASE}/transfers/{doc['id']}/cancel", headers=ph_b["hdr"], json={})
    assert resp.status_code == 422 and _err(resp) == "missing_field"
    resp = client.post(f"{BASE}/transfers/{doc['id']}/cancel", headers=ph_b["hdr"],
                       json={"reason": "Raised against the wrong store"})
    assert resp.status_code == 200 and resp.json()["transfer"]["status"] == "cancelled"
    assert _qty(lot["id"]) == 10 and _movements(doc["id"]) == []
    assert _dispatch(ph_a, doc["id"]).status_code == 409
    second = _drafted(ph_a, sub, dispensing, [(drug, 4)])
    _dispatch(ph_a, second["id"])
    resp = client.post(f"{BASE}/transfers/{second['id']}/cancel", headers=ph_a["hdr"],
                       json={"reason": "Too late"})
    assert resp.status_code == 409 and _err(resp) == "illegal_transition"


def test_a_stale_plan_is_refused_whole_and_can_be_planned_again(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    first = _lot(drug, sub["id"], 5, _days(40))
    second = _lot(drug, sub["id"], 10, _days(90))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 8)])
    with db_session(immediate=True) as db:
        stock.post_movement(db, lot_id=second["id"], movement_type="write_off",
                            quantity_change=-8, actor_id="tester")
    resp = _dispatch(ph_a, doc["id"])
    assert resp.status_code == 409 and _err(resp) == "plan_stale"
    assert _qty(first["id"]) == 5, "the first line's movement was rolled back"
    assert _movements(doc["id"]) == []
    third = _lot(drug, sub["id"], 10, _days(120))
    resp = client.post(f"{BASE}/transfers/{doc['id']}/replan", headers=ph_a["hdr"])
    assert resp.status_code == 200, resp.text
    plan = [(line["from_lot_id"], line["quantity_units"])
            for line in resp.json()["transfer"]["lines"]]
    assert plan == [(first["id"], 5), (second["id"], 2), (third["id"], 1)]
    assert _dispatch(ph_a, doc["id"]).status_code == 200
    assert _audits("pharmacy_transfer_replanned", doc["id"])


def test_a_quarantined_or_expired_plan_lot_is_stale(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    lot = _lot(drug, sub["id"], 5, _days(40))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 5)])
    with db_session(immediate=True) as db:
        stock.set_lot_status(db, lot["id"], "quarantined", actor_id="tester",
                             reason="Damaged packaging")
    resp = _dispatch(ph_a, doc["id"])
    assert resp.status_code == 409 and _err(resp) == "plan_stale"
    assert "quarantined" in resp.json()["detail"]["message"]


def test_create_is_idempotent_on_its_request_key(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    _lot(drug, sub["id"], 10, _days(80))
    key = f"key-{_uid()}"
    one = _drafted(ph_a, sub, dispensing, [(drug, 4)], idempotency_key=key)
    resp = _create(ph_a, sub, dispensing, [(drug, 4)], idempotency_key=key)
    assert resp.status_code == 200 and resp.json()["replayed"] is True
    assert resp.json()["transfer"]["id"] == one["id"]
    resp = _create(ph_a, sub, dispensing, [(drug, 5)], idempotency_key=key)
    assert resp.status_code == 409 and _err(resp) == "idempotency_conflict"


def test_a_failed_audit_write_rolls_the_dispatch_back(monkeypatch, ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    lot = _lot(drug, sub["id"], 6, _days(80))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 6)])
    real = audit_service.log_audit_event

    def flaky(*args, **kwargs):
        if kwargs.get("action_type") == "pharmacy_transfer_dispatched":
            raise audit_service.AuditWriteError("injected audit outage")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", flaky)
    resp = _dispatch(ph_a, doc["id"])
    assert resp.status_code == 503 and _err(resp) == "audit_unavailable"
    assert _qty(lot["id"]) == 6 and _movements(doc["id"]) == []
    with db_session() as db:
        row = db.execute("SELECT status, dispatched_by FROM pharmacy_transfers WHERE id = ?",
                         (doc["id"],)).fetchone()
    assert (row["status"], row["dispatched_by"]) == ("draft", None)


def test_a_drug_off_the_formulary_never_lands_on_the_dispensing_shelf(ph_a, ph_b, made,
                                                                      dispensing):
    retired = _drug(made, identity_status="retired")
    inactive = _drug(made, is_active=0)
    sub = _store(made)
    _lot(retired, sub["id"], 5, _days(80))
    _lot(inactive, sub["id"], 5, _days(80))
    assert _err(_create(ph_a, sub, dispensing, [(retired, 5)])) == "drug_not_confirmed"
    assert _err(_create(ph_a, sub, dispensing, [(inactive, 5)])) == "drug_inactive"
    # Moving it OFF the shelf, or between sub-stores, is allowed.
    other = _store(made)
    assert _create(ph_a, sub, other, [(retired, 5)]).status_code == 200

    # Made inactive while on its way: the receipt onto the shelf is refused and
    # the units stay in transit.
    drug = _drug(made)
    _lot(drug, sub["id"], 5, _days(80))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 5)])
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    with db_session() as db:
        db.execute("UPDATE helix_medication_master SET is_active = 0 WHERE id = ?", (drug,))
    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 422 and _err(resp) == "drug_inactive"
    with db_session() as db:
        assert reports.in_transit_units(db)[(dispensing["id"], drug)] == 5
        assert stock.dispensable_units(db, drug) == 0


# ═════════════════════════════════════════════════════════════════════════════
#  Controlled stock (GRN-R14) and the witness
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def vault(made):
    return _store(made, "ndps_vault")


def test_controlled_routes(ph_a, made, dispensing, vault):
    narcotic = _drug(made, ndps_category="narcotic", drug_schedule="X")
    plain = _drug(made)
    unclassified = _drug(made, ndps_category=None)
    sub = _store(made)
    _lot(narcotic, vault["id"], 10, _days(100))
    _lot(plain, sub["id"], 10, _days(100))
    _lot(unclassified, sub["id"], 10, _days(100))
    assert _err(_create(ph_a, vault, sub, [(narcotic, 2)])) == "ndps_route_refused"
    assert _err(_create(ph_a, sub, vault, [(plain, 2)])) == "vault_is_for_ndps"
    assert _err(_create(ph_a, sub, dispensing, [(unclassified, 2)])) == "drug_unclassified"
    resp = _create(ph_a, vault, dispensing, [(narcotic, 2)])
    assert resp.status_code == 200, resp.text
    doc = resp.json()["transfer"]
    assert doc["controlled"] is True and doc["witness_required"] is True


def test_a_controlled_transfer_needs_the_ndps_permission(made, dispensing, vault):
    narcotic = _drug(made, ndps_category="narcotic", drug_schedule="X")
    _lot(narcotic, vault["id"], 10, _days(100))
    with db_session() as db:
        from_store = db.execute("SELECT * FROM stores WHERE id = ?", (vault["id"],)).fetchone()
    actor = {"id": f"USRT{_uid()}", "role": "admin"}
    with pytest.raises(transfers.TransferError) as caught:
        transfers.create_transfer(from_store_code=from_store["code"],
                                  to_store_code=dispensing["code"],
                                  lines=[{"helix_medication_id": narcotic, "quantity_units": 1}],
                                  actor=actor)
    assert caught.value.code == "ndps_permission_needed"


def test_a_vault_transfer_is_witnessed_on_both_legs(ph_a, ph_b, ph_c, made, dispensing, vault):
    narcotic = _drug(made, ndps_category="narcotic", drug_schedule="X")
    lot = _lot(narcotic, vault["id"], 10, _days(100))
    doc = _drafted(ph_a, vault, dispensing, [(narcotic, 4)])

    resp = _dispatch(ph_a, doc["id"])
    assert resp.status_code == 422 and _err(resp) == "witness_required"
    resp = _dispatch(ph_a, doc["id"], witness_id=ph_a["id"], witness_pin=PIN)
    assert resp.status_code == 403 and _err(resp) == "witness_same_person"
    resp = _dispatch(ph_a, doc["id"], witness_id=ph_b["id"], witness_pin="0000")
    assert resp.status_code == 403 and _err(resp) == "witness_pin_wrong"
    assert _audits("pharmacy_transfer_witness_refused", doc["id"])
    assert _qty(lot["id"]) == 10 and _movements(doc["id"]) == []
    nurse = _user("nurse")
    resp = _dispatch(ph_a, doc["id"], witness_id=nurse["id"], witness_pin=PIN)
    assert resp.status_code == 403 and _err(resp) == "witness_not_eligible"
    no_pin = _user("pharmacist", pin=None)
    resp = _dispatch(ph_a, doc["id"], witness_id=no_pin["id"], witness_pin=PIN)
    assert resp.status_code == 403 and _err(resp) == "witness_no_pin"

    resp = _dispatch(ph_a, doc["id"], witness_id=ph_c["id"], witness_pin=PIN)
    assert resp.status_code == 200, resp.text
    meta = json.loads(_audits("pharmacy_transfer_dispatched", doc["id"])[-1]["metadata_json"])
    assert meta["witness_id"] == ph_c["id"] and meta["controlled"] is True
    sent = resp.json()["transfer"]

    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 422 and _err(resp) == "witness_required"
    resp = _receive(ph_b, doc["id"], _full(sent), witness_id=ph_a["id"], witness_pin=PIN)
    assert resp.status_code == 200, resp.text
    meta = json.loads(_audits("pharmacy_transfer_received", doc["id"])[-1]["metadata_json"])
    assert meta["witness_id"] == ph_a["id"]
    assert _balance(doc["id"])["balanced"]
    assert _reconcile(_touched_lots(doc["id"]))["clean"]
    with db_session() as db:
        # Vault stock of a narcotic and the dispensing cabinet are both on the
        # shelf; nothing else changed.
        assert stock.dispensable_units(db, narcotic) == 10


def test_a_receipt_that_cannot_happen_never_asks_for_the_witness_pin(ph_a, ph_b, ph_c, made,
                                                                     dispensing, vault):
    """A draft (or the dispatcher) is refused before the witness's PIN is
    checked, so a wrong PIN there is neither counted nor audited."""
    narcotic = _drug(made, ndps_category="narcotic", drug_schedule="X")
    _lot(narcotic, vault["id"], 10, _days(100))
    doc = _drafted(ph_a, vault, dispensing, [(narcotic, 2)])
    resp = _receive(ph_b, doc["id"], _full(doc), witness_id=ph_c["id"], witness_pin="0000")
    assert resp.status_code == 409 and _err(resp) == "illegal_transition"
    assert not pin_security.PIN_ATTEMPTS.get(ph_c["id"])
    assert _audits("pharmacy_transfer_witness_refused", doc["id"]) == []
    sent = _dispatch(ph_a, doc["id"], witness_id=ph_c["id"], witness_pin=PIN).json()["transfer"]
    resp = _receive(ph_a, doc["id"], _full(sent), witness_id=ph_c["id"], witness_pin="0000")
    assert resp.status_code == 403 and _err(resp) == "same_person"
    assert not pin_security.PIN_ATTEMPTS.get(ph_c["id"])
    assert [m["movement_type"] for m in _movements(doc["id"])] == ["transfer_out"]


def test_a_locked_witness_pin_is_429(ph_a, ph_b, made, dispensing, vault):
    narcotic = _drug(made, ndps_category="psychotropic", drug_schedule="H1")
    _lot(narcotic, vault["id"], 10, _days(100))
    doc = _drafted(ph_a, vault, dispensing, [(narcotic, 1)])
    for _ in range(pin_security.MAX_PIN_ATTEMPTS):
        _dispatch(ph_a, doc["id"], witness_id=ph_b["id"], witness_pin="9999")
    resp = _dispatch(ph_a, doc["id"], witness_id=ph_b["id"], witness_pin=PIN)
    assert resp.status_code == 429 and _err(resp) == "witness_locked"
    assert _movements(doc["id"]) == []


def test_the_leg_in_progress_names_its_witness(monkeypatch, ph_a, ph_c, made, dispensing,
                                               vault):
    narcotic = _drug(made, ndps_category="narcotic", drug_schedule="X")
    _lot(narcotic, vault["id"], 10, _days(100))
    doc = _drafted(ph_a, vault, dispensing, [(narcotic, 2)])
    seen = []
    stock.register_movement_hook("test_transfer_leg",
                                 lambda conn, mv: seen.append(transfers.current_transfer_leg()))
    try:
        resp = _dispatch(ph_a, doc["id"], witness_id=ph_c["id"], witness_pin=PIN)
    finally:
        stock.register_movement_hook("test_transfer_leg", None)
    assert resp.status_code == 200, resp.text
    assert seen and seen[0]["leg"] == "dispatch" and seen[0]["witness_id"] == ph_c["id"]
    assert seen[0]["transfer_id"] == doc["id"] and seen[0]["controlled"] is True
    assert transfers.current_transfer_leg() is None


# ═════════════════════════════════════════════════════════════════════════════
#  Reads
# ═════════════════════════════════════════════════════════════════════════════

def test_reads(ph_a, ph_b, made, dispensing, vault):
    drug = _drug(made)
    sub = _store(made)
    _lot(drug, sub["id"], 7, _days(60))
    _lot(drug, sub["id"], 2, _days(-3))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 3)])
    _dispatch(ph_a, doc["id"])

    body = client.get(f"{BASE}/transfers", headers=ph_b["hdr"],
                      params={"status": "open", "store_id": sub["id"]}).json()
    assert [t["id"] for t in body["transfers"]] == [doc["id"]]
    assert client.get(f"{BASE}/transfers", headers=ph_b["hdr"],
                      params={"status": "nonsense"}).status_code == 422
    assert client.get(f"{BASE}/transfers/999999999", headers=ph_b["hdr"]).status_code == 404

    stock_now = client.get(f"{BASE}/transfers/stock", headers=ph_b["hdr"],
                           params={"store_code": sub["code"]}).json()
    row = [d for d in stock_now["drugs"] if d["drug_id"] == drug]
    assert row and row[0]["usable_units"] == 4, "expired units are not offered"

    opts = client.get(f"{BASE}/transfers/options", headers=ph_b["hdr"]).json()
    kinds = {s["pharmacy_kind"] for s in opts["stores"]}
    assert kinds <= {"dispensing", "sub_store", "ndps_vault"}
    assert vault["code"] in {s["code"] for s in opts["stores"]}
    ids = {w["id"] for w in opts["witnesses"]}
    assert ph_a["id"] in ids and ph_b["id"] not in ids, "never the caller"
    no_pin = _user("pharmacist", pin=None)
    opts = client.get(f"{BASE}/transfers/options", headers=ph_b["hdr"]).json()
    assert no_pin["id"] not in {w["id"] for w in opts["witnesses"]}, \
        "nobody is offered as a witness who has no signing PIN to confirm with"
    admin = _user("admin")
    assert client.get(f"{BASE}/transfers/options", headers=admin["hdr"]).json()[
        "witnesses"] == []


def test_a_drug_made_narcotic_after_the_draft_is_refused_at_dispatch(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    lot = _lot(drug, sub["id"], 6, _days(80))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 6)])
    with db_session() as db:
        db.execute("UPDATE helix_medication_master SET ndps_category = 'narcotic' WHERE id = ?",
                   (drug,))
    resp = _dispatch(ph_a, doc["id"])
    assert resp.status_code == 422 and _err(resp) == "ndps_route_refused"
    assert _qty(lot["id"]) == 6 and _movements(doc["id"]) == []


def test_the_witness_can_be_tied_to_the_register_switch(monkeypatch, ph_a, ph_c, made,
                                                        dispensing, vault):
    """NDPS_WITNESS_ALWAYS off: a controlled transfer needs its witness only
    while the NDPS register is on."""
    narcotic = _drug(made, ndps_category="narcotic", drug_schedule="X")
    _lot(narcotic, vault["id"], 10, _days(100))
    monkeypatch.setattr(transfers, "NDPS_WITNESS_ALWAYS", False)
    monkeypatch.setattr(config, "feature_enabled", lambda fid: fid == "ndps_register")
    doc = _drafted(ph_a, vault, dispensing, [(narcotic, 1)])
    assert _err(_dispatch(ph_a, doc["id"])) == "witness_required"
    monkeypatch.setattr(config, "feature_enabled", lambda fid: False)
    resp = _dispatch(ph_a, doc["id"])
    assert resp.status_code == 200, resp.text


def test_receipt_into_a_closed_store_or_onto_a_held_batch_is_409(ph_a, ph_b, made):
    drug = _drug(made)
    sub = _store(made)
    other = _store(made)
    lot = _lot(drug, sub["id"], 6, _days(80), batch=f"HELD{_uid()}")
    doc = _drafted(ph_a, sub, other, [(drug, 6)])
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    with db_session() as db:
        db.execute("UPDATE stores SET is_active = 0 WHERE id = ?", (other["id"],))
    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 409 and _err(resp) == "store_inactive"
    with db_session() as db:
        db.execute("UPDATE stores SET is_active = 1 WHERE id = ?", (other["id"],))
    # The same batch is already held in the receiving store as quarantined
    # stock: never merged into it.
    _lot(drug, other["id"], 1, lot["expiration_date"], status="quarantined",
         batch=lot["lot_number"])
    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 409 and _err(resp) == "lot_status_conflict"
    with db_session() as db:
        assert reports.in_transit_units(db)[(other["id"], drug)] == 6


# ═════════════════════════════════════════════════════════════════════════════
#  Stock held while on its way (recall, NSQ, quarantine): never lands available
# ═════════════════════════════════════════════════════════════════════════════

def _hold(lot_id, status, reason="Manufacturer recall notice for this batch"):
    with db_session(immediate=True) as db:
        stock.set_lot_status(db, lot_id, status, actor_id="tester", reason=reason)


def _lot_row(lot_id) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM helix_inventory_lots WHERE id = ?",
                               (lot_id,)).fetchone())


def _assert_held_landing(doc_id, line, drug, store_id, lands, units):
    landed = _lot_row(line["to_lot_id"])
    assert landed["store_id"] == store_id and landed["status"] == lands
    assert int(landed["quantity"]) == units
    assert line["to_lot_status"] == lands and line["received_units"] == units
    with db_session() as db:
        assert stock.dispensable_units(db, drug) == 0, "held stock is dispensable nowhere"
        assert stock.pick_fefo_lot(db, drug) is None
        assert reports.in_transit_units(db).get((store_id, drug)) is None, \
            "counted in the receiving store, where a batch recall can find it"
    assert _balance(doc_id)["balanced"]
    assert _reconcile(_touched_lots(doc_id))["clean"]
    meta = json.loads(_audits("pharmacy_transfer_received", doc_id)[-1]["metadata_json"])
    assert meta["lines"][0]["landed_status"] == lands and meta["lines"][0]["held_reason"]
    assert meta["held_units"] == units


@pytest.mark.parametrize("status, lands", [("recalled", "recalled"), ("nsq", "nsq"),
                                           ("quarantined", "quarantined")])
def test_a_lot_held_while_on_its_way_never_lands_available(status, lands, ph_a, ph_b, made,
                                                           dispensing):
    """The reviewer's case: 6 units on their way to the dispensing shelf, the
    lot they left is recalled (or NSQ, or quarantined), then the receipt."""
    drug = _drug(made)
    sub = _store(made)
    lot = _lot(drug, sub["id"], 8, _days(200))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 6)])
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    _hold(lot["id"], status)
    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 200, resp.text
    got = resp.json()["transfer"]
    assert got["status"] == "received"
    line = got["lines"][0]
    _assert_held_landing(doc["id"], line, drug, dispensing["id"], lands, 6)
    assert _lot_row(line["to_lot_id"])["lot_number"] == lot["lot_number"]


def test_a_lot_closed_while_on_its_way_lands_quarantined(ph_a, ph_b, made, dispensing):
    """Written off (or returned) after the transfer emptied it: nothing says
    why, so the units land quarantined for a pharmacist to decide."""
    drug = _drug(made)
    sub = _store(made)
    lot = _lot(drug, sub["id"], 5, _days(200))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 5)])
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    _hold(lot["id"], "written_off", reason="Closed once the transfer emptied it")
    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 200, resp.text
    _assert_held_landing(doc["id"], resp.json()["transfer"]["lines"][0], drug,
                         dispensing["id"], "quarantined", 5)


@pytest.mark.parametrize("status", ["recalled", "nsq"])
def test_a_batch_held_in_another_store_while_on_its_way_lands_held(status, ph_a, ph_b, made,
                                                                   dispensing):
    """A recall or an NSQ finding is about the batch: a lot of it held in any
    other store (batch spelt with other case and spaces, another expiry on
    file) holds the units on their way too, though their own lot is still
    available."""
    drug = _drug(made)
    sub = _store(made)
    other = _store(made)
    batch = f"RC{_uid()}"
    lot = _lot(drug, sub["id"], 8, _days(200), batch=batch)
    doc = _drafted(ph_a, sub, dispensing, [(drug, 6)])
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    twin = _lot(drug, other["id"], 2, _days(210), batch=f"{batch[:2]} {batch[2:]}".lower())
    _hold(twin["id"], status)
    assert _lot_row(lot["id"])["status"] == "available"
    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 200, resp.text
    _assert_held_landing(doc["id"], resp.json()["transfer"]["lines"][0], drug,
                         dispensing["id"], status, 6)
    # What is left of the batch in the sub-store is not offered or planned.
    stock_now = client.get(f"{BASE}/transfers/stock", headers=ph_b["hdr"],
                           params={"store_code": sub["code"]}).json()
    assert [d for d in stock_now["drugs"] if d["drug_id"] == drug] == []
    resp = _create(ph_a, sub, dispensing, [(drug, 1)])
    assert resp.status_code == 409 and _err(resp) == "insufficient_stock"


def test_a_batch_recalled_after_the_draft_is_stale_and_planned_around(ph_a, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    other = _store(made)
    batch = f"RC{_uid()}"
    lot = _lot(drug, sub["id"], 6, _days(100), batch=batch)
    good = _lot(drug, sub["id"], 10, _days(300))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 4)])
    assert [line["from_lot_id"] for line in doc["lines"]] == [lot["id"]]
    twin = _lot(drug, other["id"], 1, lot["expiration_date"], batch=batch)
    _hold(twin["id"], "recalled")
    resp = _dispatch(ph_a, doc["id"])
    assert resp.status_code == 409 and _err(resp) == "plan_stale"
    assert "recalled" in resp.json()["detail"]["message"]
    assert _qty(lot["id"]) == 6 and _movements(doc["id"]) == []
    resp = client.post(f"{BASE}/transfers/{doc['id']}/replan", headers=ph_a["hdr"])
    assert resp.status_code == 200, resp.text
    assert [(line["from_lot_id"], line["quantity_units"])
            for line in resp.json()["transfer"]["lines"]] == [(good["id"], 4)]
    stock_now = client.get(f"{BASE}/transfers/stock", headers=ph_a["hdr"],
                           params={"store_code": sub["code"]}).json()
    row = [d for d in stock_now["drugs"] if d["drug_id"] == drug]
    assert row and row[0]["usable_units"] == 10 and row[0]["lot_count"] == 1
    assert row[0]["earliest_expiry"] == _days(300)
    assert _dispatch(ph_a, doc["id"]).status_code == 200


def test_a_held_landing_onto_the_batch_held_available_there_is_409(ph_a, ph_b, made):
    """The receiving store still holds the batch as available stock: held
    units are never merged into it, so the receipt is refused and the units
    stay on their way until that lot is held as well."""
    drug = _drug(made)
    sub = _store(made)
    other = _store(made)
    batch = f"RC{_uid()}"
    lot = _lot(drug, sub["id"], 6, _days(80), batch=batch)
    doc = _drafted(ph_a, sub, other, [(drug, 6)])
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    there = _lot(drug, other["id"], 3, lot["expiration_date"], batch=batch)
    _hold(lot["id"], "recalled")
    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 409 and _err(resp) == "lot_status_conflict"
    assert "stay on their way" in resp.json()["detail"]["message"]
    assert _qty(there["id"]) == 3
    assert [m["movement_type"] for m in _movements(doc["id"])] == ["transfer_out"]
    with db_session() as db:
        assert reports.in_transit_units(db)[(other["id"], drug)] == 6
    # Once the receiving store's lot is recalled too, the units land with it.
    _hold(there["id"], "recalled")
    resp = _receive(ph_b, doc["id"], _full(sent))
    assert resp.status_code == 200, resp.text
    line = resp.json()["transfer"]["lines"][0]
    assert line["to_lot_id"] == there["id"] and line["to_lot_status"] == "recalled"
    assert _qty(there["id"]) == 9
    assert _reconcile(_touched_lots(doc["id"]))["clean"]


def test_an_available_landing_says_so_on_the_line(ph_a, ph_b, made, dispensing):
    drug = _drug(made)
    sub = _store(made)
    _lot(drug, sub["id"], 3, _days(90))
    doc = _drafted(ph_a, sub, dispensing, [(drug, 3)])
    assert doc["lines"][0]["to_lot_status"] is None
    sent = _dispatch(ph_a, doc["id"]).json()["transfer"]
    line = _receive(ph_b, doc["id"], _full(sent)).json()["transfer"]["lines"][0]
    assert line["to_lot_status"] == "available"
    meta = json.loads(_audits("pharmacy_transfer_received", doc["id"])[-1]["metadata_json"])
    assert meta["lines"][0]["landed_status"] == "available"
    assert meta["lines"][0]["held_reason"] is None and meta["held_units"] == 0


def test_every_transfers_get_answers_cleanly_for_every_role():
    """With the module on, every GET route here answers each role without a
    server error (the Ward Record boot probe runs with the module off)."""
    paths = ("/transfers", "/transfers?status=open", "/transfers/options",
             "/transfers/stock?store_code=NOSUCHSTORE", "/transfers/1", "/transfers/999999999")
    for role in ("admin", "doctor", "nurse", "pharmacist", "ward_clerk", "cashier",
                 "receptionist", "lab_technician"):
        who = _user(role)
        for path in paths:
            resp = client.get(f"{BASE}{path}", headers=who["hdr"])
            assert resp.status_code < 500, (role, path, resp.text)
            if role not in ("admin", "pharmacist"):
                assert resp.status_code == 403, (role, path, resp.status_code)
