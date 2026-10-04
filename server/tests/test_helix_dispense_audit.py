"""The Helix dispense after grn-4: one immediate transaction, one writer, one
audit row.

Pinned (docs/his_design/grn.json, grn-4 tests):
  * a dispense through the route answers 200 with exactly one movement, with
    pooled connections and with ORB_DB_POOL=0, and does not hang (the old
    in-body commit would have left the immediate session's COMMIT with no
    transaction);
  * two pharmacists dispensing the last unit at once: exactly one succeeds;
  * a replayed request key writes no second movement;
  * the movement's balance_after equals the lot;
  * every dispense writes clinical_access helix_dispense_recorded in the same
    transaction, with string ids; when that write fails the dispense rolls
    back: no movement, no transition, status unchanged, lot unchanged
    (ALWAYS ON: an audit outage now stops dispensing as it stops charting);
  * a quarantined or off-shelf lot is never dispensed; a narcotic's vault lot
    is.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from auth import create_token, hash_password
from database import db_session
from main import app
from services import audit_service, helix_service
from services import pharmacy_stock_service as stock
from services.helix_db import init_helix_db

client = TestClient(app)
LATER = (date.today() + timedelta(days=300)).isoformat()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


def _user(role="pharmacist"):
    uid = f"UDSP{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, hash_password("secretpass")))
    return uid


def _hdr(uid, role="pharmacist"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _drug(ndps=None):
    med = f"MEDDSP{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength, "
                   "ndps_category) VALUES (?, ?, 'dispensamol', 'tablet', '10 mg', ?)",
                   (med, f"Dispensamol {med[-6:]} 10 mg", ndps))
    return med


def _lot(med, units, store_id=None):
    with db_session(immediate=True) as db:
        return stock.open_or_merge_lot(
            db, medication_id=med, lot_number=f"BD{uuid.uuid4().hex[:6]}",
            expiration_date=LATER, units=units, actor_id="tester",
            store_id=store_id)["lot"]["id"]


def _scheduled_order(med, prescriber):
    pid = f"PATDSP{uuid.uuid4().hex[:6].upper()}"
    oid = f"ORDDSP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Dispense Case {pid[-4:]}", f"MRN-{pid}"))
        db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, dosage, "
                   "route, frequency, status, prescribed_by) "
                   "VALUES (?, ?, ?, '10 mg', 'PO', 'OD', 'scheduled', ?)",
                   (oid, pid, med, prescriber))
    return oid, pid


def _q(sql, *params):
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _movements(oid):
    return _q("SELECT * FROM helix_stock_movements WHERE order_id = ?", oid)


@pytest.fixture
def vault():
    with db_session() as db:
        store_id = db.execute(
            "INSERT INTO stores (code, name, store_type, is_active, pharmacy_kind) "
            "VALUES (?, 'Dispense test vault', 'sub_store', 1, 'ndps_vault')",
            (f"DSPV{uuid.uuid4().hex[:6].upper()}",)).lastrowid
    yield store_id
    with db_session() as db:
        for (lot_id,) in db.execute("SELECT id FROM helix_inventory_lots WHERE store_id = ?",
                                    (store_id,)).fetchall():
            db.execute("DELETE FROM helix_stock_movements WHERE lot_id = ?", (lot_id,))
            db.execute("DELETE FROM helix_inventory_lots WHERE id = ?", (lot_id,))
        db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


# ── the route, pooled and unpooled ───────────────────────────────────────────

@pytest.mark.parametrize("pooled", [True, False], ids=["ORB_DB_POOL=1", "ORB_DB_POOL=0"])
def test_a_dispense_through_the_route_writes_one_movement_and_does_not_hang(monkeypatch, pooled):
    import db_backend
    monkeypatch.setattr(db_backend, "POOL_ENABLED", pooled)
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 5)
    oid, pid = _scheduled_order(med, pharmacist)

    started = time.monotonic()
    resp = client.post(f"/api/helix/orders/{oid}/transition",
                       json={"target_status": "dispensed", "idempotency_key": f"k-{oid}"},
                       headers=_hdr(pharmacist))
    assert resp.status_code == 200, resp.text
    assert time.monotonic() - started < 10, "the dispense waited on its own write lock"
    body = resp.json()
    assert body["order"]["status"] == "dispensed" and body["already_applied"] is False

    moves = _movements(oid)
    assert len(moves) == 1
    move = moves[0]
    assert (move["lot_id"], move["quantity_change"], move["movement_type"]) == (lot, -1, "dispense")
    assert move["reason"] == "dispense" and move["actor_id"] == pharmacist
    assert move["reference_type"] == "helix_order" and move["reference_id"] == oid
    assert move["balance_after_units"] == 4
    assert _q("SELECT quantity FROM helix_inventory_lots WHERE id = ?", lot)[0]["quantity"] == 4
    assert json.loads(body["transition"]["payload"])["stock_movement_id"] == move["id"]

    # A replay of the same key writes nothing more.
    again = client.post(f"/api/helix/orders/{oid}/transition",
                        json={"target_status": "dispensed", "idempotency_key": f"k-{oid}"},
                        headers=_hdr(pharmacist))
    assert again.status_code == 200 and again.json()["already_applied"] is True
    assert len(_movements(oid)) == 1
    with db_session() as db:
        assert stock.reconcile(db, lot_ids=[lot])["clean"] is True


# ── the audit row ────────────────────────────────────────────────────────────

def test_every_dispense_is_audited_in_its_own_transaction_with_string_ids():
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 2)
    oid, pid = _scheduled_order(med, pharmacist)
    result = helix_service.transition_order_status(oid, "dispensed", pharmacist)
    rows = _q("SELECT * FROM audit_logs WHERE action_type = 'helix_dispense_recorded' "
              "AND target_id = ?", oid)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_category"] == "clinical_access" and row["outcome"] == "success"
    assert row["patient_id"] == pid and row["actor_user_id"] == pharmacist
    assert row["target_type"] == "helix_order"
    assert row["correlation_id"] == result["transition"]["id"]
    meta = json.loads(row["metadata_json"])
    move = _movements(oid)[0]
    assert meta == {
        "order_id": oid, "transition_id": result["transition"]["id"],
        "movement_id": move["id"], "lot_id": lot, "medication_id": med,
        "prescribed_medication_id": med, "substituted": False, "quantity_units": 1,
        "store_id": None,
    }
    for key in ("order_id", "transition_id", "movement_id", "lot_id", "medication_id"):
        assert isinstance(meta[key], str)
    assert "1 unit of" in row["summary_message"]


def test_no_audit_row_for_a_step_that_moves_no_stock():
    pharmacist = _user()
    med = _drug()
    _lot(med, 2)
    oid, _ = _scheduled_order(med, pharmacist)
    helix_service.transition_order_status(oid, "reviewing", pharmacist)
    assert _q("SELECT 1 FROM audit_logs WHERE action_type = 'helix_dispense_recorded' "
              "AND target_id = ?", oid) == []


def test_when_the_audit_cannot_be_written_the_dispense_rolls_back(monkeypatch):
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 3)
    oid, _ = _scheduled_order(med, pharmacist)
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "helix_dispense_recorded":
            raise audit_service.AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    with pytest.raises(audit_service.AuditWriteError):
        helix_service.transition_order_status(oid, "dispensed", pharmacist,
                                              idempotency_key=f"fail-{oid}")
    assert _movements(oid) == []
    assert _q("SELECT status FROM helix_patient_med_orders WHERE id = ?", oid)[0]["status"] \
        == "scheduled"
    assert _q("SELECT 1 FROM helix_transitions WHERE idempotency_key = ?", f"fail-{oid}") == []
    assert _q("SELECT quantity FROM helix_inventory_lots WHERE id = ?", lot)[0]["quantity"] == 3

    # With the audit back, the same key dispenses once.
    monkeypatch.setattr(audit_service, "log_audit_event", real)
    helix_service.transition_order_status(oid, "dispensed", pharmacist,
                                          idempotency_key=f"fail-{oid}")
    assert len(_movements(oid)) == 1


# ── the race for the last unit ───────────────────────────────────────────────

def test_two_pharmacists_dispensing_the_last_unit_exactly_one_succeeds():
    p1, p2 = _user(), _user()
    med = _drug()
    lot = _lot(med, 1)
    orders = [_scheduled_order(med, p1)[0], _scheduled_order(med, p2)[0]]
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def run(oid, actor):
        barrier.wait()
        try:
            outcomes[oid] = helix_service.transition_order_status(oid, "dispensed", actor)
        except Exception as exc:  # noqa: BLE001 - the loser's refusal is the point
            outcomes[oid] = exc

    threads = [threading.Thread(target=run, args=(oid, actor))
               for oid, actor in zip(orders, (p1, p2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a dispense hung"

    won = [oid for oid, out in outcomes.items() if isinstance(out, dict)]
    lost = [out for out in outcomes.values() if not isinstance(out, dict)]
    assert len(won) == 1 and len(lost) == 1, outcomes
    assert isinstance(lost[0], ValueError) and "No stock" in str(lost[0])
    assert _q("SELECT quantity FROM helix_inventory_lots WHERE id = ?", lot)[0]["quantity"] == 0
    assert len(_q("SELECT 1 FROM helix_stock_movements WHERE lot_id = ? "
                  "AND movement_type = 'dispense'", lot)) == 1


# ── what may leave the shelf ─────────────────────────────────────────────────

def test_a_quarantined_lot_is_never_dispensed():
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 5)
    with db_session(immediate=True) as db:
        stock.set_lot_status(db, lot, "quarantined", actor_id=pharmacist, reason="test")
    oid, _ = _scheduled_order(med, pharmacist)
    with pytest.raises(ValueError, match="No stock"):
        helix_service.transition_order_status(oid, "dispensed", pharmacist)
    assert _movements(oid) == []


def test_a_narcotic_is_dispensed_from_the_vault(vault):
    pharmacist = _user()
    narcotic = _drug(ndps="narcotic")
    lot = _lot(narcotic, 2, store_id=vault)
    oid, _ = _scheduled_order(narcotic, pharmacist)
    helix_service.transition_order_status(oid, "dispensed", pharmacist)
    move = _movements(oid)[0]
    assert move["lot_id"] == lot and move["store_id"] == vault

    plain = _drug(ndps="none")
    _lot(plain, 2, store_id=vault)
    other, _ = _scheduled_order(plain, pharmacist)
    with pytest.raises(ValueError, match="No stock"):
        helix_service.transition_order_status(other, "dispensed", pharmacist)
