"""Dispense quantity and batch choice at dispense (grn-14, W8-E; ruling GRN-R13:
the pharmacist enters the quantity, default 1, bounded by a site maximum).

Pinned:
  * module off (ORB_PHARMACY_STORE): dispense_quantity_units and lot_id are
    ignored, whatever they hold. One unit, earliest expiry first, the audit
    metadata and the transition payload exactly as before;
  * module on: the quantity is a whole number from 1 to
    ORB_PHARMACY_MAX_DISPENSE_UNITS (422 otherwise); a batch of another drug,
    or of the drug prescribed when a substitute is set, is 422 lot_wrong_drug;
    an expired, undated, quarantined, ward-stock or wrong-store batch is 422
    lot_not_dispensable; a batch holding too few units is 409, and with no
    batch named a quantity no single batch holds is 409 too: never split;
  * every refusal writes nothing (no movement, no transition, status kept);
  * the drug-safety gate still screens the effective drug at dispense;
  * the audit row carries the quantity, the batch and who chose it, in the
    dispense's own transaction (an unwritable audit rolls the dispense back);
  * GET /api/helix/orders/{id}/dispense-options lists the effective drug's
    dispensable batches only with the module on, and is department-scoped.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import audit_service, helix_service
from services import pharmacy_stock_service as stock
from services.helix_db import init_helix_db

client = TestClient(app)
SOON = (date.today() + timedelta(days=60)).isoformat()
LATER = (date.today() + timedelta(days=300)).isoformat()
PAST = (date.today() - timedelta(days=2)).isoformat()
DEPT = "W8E Dispense Ward"
OTHER_DEPT = "W8E Other Dispense Ward"


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture()
def module_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_MAX_DISPENSE_UNITS", 6)


# ── People, drugs, lots and orders ────────────────────────────────────────────

def _user(role="pharmacist", department=None):
    uid = f"UW8E{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"{role} {uid}", role, department))
    return uid


def _hdr(uid, role="pharmacist"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _drug(generic="dosimol", ndps=None):
    med = f"MEDW8E{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength, "
                   "ndps_category) VALUES (?, ?, ?, 'tablet', '10 mg', ?)",
                   (med, f"Dosimol {med[-6:]} 10 mg", generic, ndps))
    return med


def _lot(med, units, *, expiry=LATER, store_id=None, status="available", number=None):
    with db_session(immediate=True) as db:
        return stock.open_or_merge_lot(
            db, medication_id=med, lot_number=number or f"BW{uuid.uuid4().hex[:6].upper()}",
            expiration_date=expiry, units=units, actor_id="tester", store_id=store_id,
            status=status)["lot"]["id"]


def _store(kind):
    with db_session() as db:
        return db.execute(
            "INSERT INTO stores (code, name, store_type, is_active, pharmacy_kind) "
            "VALUES (?, ?, 'ward', 1, ?)",
            (f"W8E{uuid.uuid4().hex[:6].upper()}", f"W8E {kind} store", kind)).lastrowid


def _scheduled_order(med, prescriber, *, substitute=None, department=DEPT):
    pid = f"PATW8E{uuid.uuid4().hex[:6].upper()}"
    oid = f"ORDW8E{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Dispense Quantity {pid[-4:]}", f"MRN-{pid}"))
        db.execute("INSERT INTO admissions (patient_id, department, status, admitted_by) "
                   "VALUES (?, ?, 'active', ?)", (pid, department, prescriber))
        db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, dosage, "
                   "route, frequency, status, prescribed_by, substitute_medication_id) "
                   "VALUES (?, ?, ?, '10 mg', 'PO', 'OD', 'scheduled', ?, ?)",
                   (oid, pid, med, prescriber, substitute))
    return oid, pid


def _q(sql, *params):
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _movements(oid):
    return _q("SELECT * FROM helix_stock_movements WHERE order_id = ?", oid)


def _qty(lot):
    return _q("SELECT quantity FROM helix_inventory_lots WHERE id = ?", lot)[0]["quantity"]


def _status(oid):
    return _q("SELECT status FROM helix_patient_med_orders WHERE id = ?", oid)[0]["status"]


def _audit(oid):
    rows = _q("SELECT * FROM audit_logs WHERE action_type = 'helix_dispense_recorded' "
              "AND target_id = ?", oid)
    assert len(rows) == 1, rows
    return rows[0], json.loads(rows[0]["metadata_json"])


def _dispense(oid, actor, **body):
    return client.post(f"/api/helix/orders/{oid}/transition",
                       json={"target_status": "dispensed", **body}, headers=_hdr(actor))


def _assert_nothing_written(oid, *lots_and_units):
    assert _movements(oid) == []
    assert _status(oid) == "scheduled"
    assert _q("SELECT 1 FROM helix_transitions WHERE order_id = ?", oid) == []
    for lot, units in lots_and_units:
        assert _qty(lot) == units


# ── Module off: the new fields are ignored ────────────────────────────────────

def test_module_off_ignores_the_quantity_and_the_batch():
    assert config.module_state("pharmacy-store") == "off"
    pharmacist = _user()
    med = _drug()
    first = _lot(med, 5, expiry=SOON)
    second = _lot(med, 5, expiry=LATER)
    other_drug_lot = _lot(_drug(), 5)
    oid, _ = _scheduled_order(med, pharmacist)

    resp = _dispense(oid, pharmacist, dispense_quantity_units=4, lot_id=other_drug_lot,
                     idempotency_key=f"off-{oid}")
    assert resp.status_code == 200, resp.text
    move = _movements(oid)[0]
    assert (move["lot_id"], move["quantity_change"]) == (first, -1)
    assert (_qty(first), _qty(second), _qty(other_drug_lot)) == (4, 5, 5)
    _, meta = _audit(oid)
    assert set(meta) == {"order_id", "transition_id", "movement_id", "lot_id", "medication_id",
                         "prescribed_medication_id", "substituted", "quantity_units",
                         "store_id"}
    assert meta["quantity_units"] == 1
    payload = json.loads(resp.json()["transition"]["payload"])
    assert set(payload) == {"pharmacist_notes", "substitute_medication_id", "scheduled_at",
                            "stock_movement_id"}


@pytest.mark.parametrize("junk", ["three", -4, 0, True, 2.5, {"units": 2}])
def test_module_off_never_reads_a_malformed_quantity(junk):
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 3)
    oid, _ = _scheduled_order(med, pharmacist)
    resp = _dispense(oid, pharmacist, dispense_quantity_units=junk, lot_id=["not", "an", "id"])
    assert resp.status_code == 200, resp.text
    assert _qty(lot) == 2


def test_module_off_dispense_options_list_nothing():
    pharmacist = _user()
    med = _drug()
    _lot(med, 3)
    oid, _ = _scheduled_order(med, pharmacist)
    resp = client.get(f"/api/helix/orders/{oid}/dispense-options", headers=_hdr(pharmacist))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False and body["max_units"] == 1 and body["lots"] == []
    assert body["medication_id"] == med


# ── Module on: the quantity ───────────────────────────────────────────────────

def test_a_bounded_quantity_leaves_one_batch_earliest_expiry_first(module_on):
    pharmacist = _user()
    med = _drug()
    small_soon = _lot(med, 2, expiry=SOON)
    big_later = _lot(med, 10, expiry=LATER)
    oid, pid = _scheduled_order(med, pharmacist)

    resp = _dispense(oid, pharmacist, dispense_quantity_units=4)
    assert resp.status_code == 200, resp.text
    # The earliest batch holds only 2, so the earliest batch holding all 4.
    moves = _movements(oid)
    assert len(moves) == 1
    assert (moves[0]["lot_id"], moves[0]["quantity_change"]) == (big_later, -4)
    assert (_qty(small_soon), _qty(big_later)) == (2, 6)
    row, meta = _audit(oid)
    assert row["patient_id"] == pid and row["event_category"] == "clinical_access"
    assert meta["quantity_units"] == 4 and meta["requested_units"] == 4
    assert meta["lot_id"] == big_later and meta["lot_choice"] == "earliest_expiry"
    assert meta["max_dispense_units"] == 6
    lot_number = _q("SELECT lot_number FROM helix_inventory_lots WHERE id = ?",
                    big_later)[0]["lot_number"]
    assert meta["lot_number"] == lot_number
    assert "4 units of" in row["summary_message"] and lot_number in row["summary_message"]
    payload = json.loads(resp.json()["transition"]["payload"])
    assert payload["dispense_quantity_units"] == 4 and payload["lot_id"] is None
    with db_session() as db:
        assert stock.reconcile(db, lot_ids=[small_soon, big_later])["clean"] is True


def test_the_default_is_one_unit_with_the_module_on(module_on):
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 3)
    oid, _ = _scheduled_order(med, pharmacist)
    resp = _dispense(oid, pharmacist)
    assert resp.status_code == 200, resp.text
    assert _movements(oid)[0]["quantity_change"] == -1 and _qty(lot) == 2


@pytest.mark.parametrize("units,code", [
    (7, "quantity_out_of_range"), (0, "quantity_out_of_range"), (-2, "quantity_out_of_range"),
    ("3", "bad_quantity"), (2.5, "bad_quantity"), (True, "bad_quantity"),
])
def test_the_quantity_is_bounded_and_whole(module_on, units, code):
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 20)
    oid, _ = _scheduled_order(med, pharmacist)
    resp = _dispense(oid, pharmacist, dispense_quantity_units=units)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code
    assert "_" not in resp.json()["detail"]["message"]
    _assert_nothing_written(oid, (lot, 20))


def test_the_site_maximum_is_one_unless_the_site_raises_it(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
    assert config.PHARMACY_MAX_DISPENSE_UNITS == 1
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 5)
    oid, _ = _scheduled_order(med, pharmacist)
    resp = _dispense(oid, pharmacist, dispense_quantity_units=2)
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "quantity_out_of_range"
    assert "at most 1 unit" in resp.json()["detail"]["message"]
    _assert_nothing_written(oid, (lot, 5))


def test_a_quantity_no_single_batch_holds_is_refused_never_split(module_on):
    pharmacist = _user()
    med = _drug()
    a, b = _lot(med, 2, expiry=SOON), _lot(med, 2, expiry=LATER)
    oid, _ = _scheduled_order(med, pharmacist)
    resp = _dispense(oid, pharmacist, dispense_quantity_units=3)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "insufficient_lot_stock"
    assert "never split" in resp.json()["detail"]["message"]
    _assert_nothing_written(oid, (a, 2), (b, 2))

    short = _drug()
    only = _lot(short, 2)
    oid2, _ = _scheduled_order(short, pharmacist)
    resp = _dispense(oid2, pharmacist, dispense_quantity_units=5)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "insufficient_lot_stock"
    assert "Only 2 units" in resp.json()["detail"]["message"]
    _assert_nothing_written(oid2, (only, 2))


# ── Module on: the batch ──────────────────────────────────────────────────────

def test_the_pharmacists_batch_is_taken_even_when_another_expires_first(module_on):
    pharmacist = _user()
    med = _drug()
    soon = _lot(med, 5, expiry=SOON)
    chosen = _lot(med, 5, expiry=LATER)
    oid, _ = _scheduled_order(med, pharmacist)
    resp = _dispense(oid, pharmacist, dispense_quantity_units=2, lot_id=chosen)
    assert resp.status_code == 200, resp.text
    assert (_qty(soon), _qty(chosen)) == (5, 3)
    _, meta = _audit(oid)
    assert meta["lot_id"] == chosen and meta["lot_choice"] == "pharmacist"
    assert meta["quantity_units"] == 2


def test_a_batch_of_another_drug_is_refused(module_on):
    pharmacist = _user()
    med, other = _drug(), _drug()
    mine = _lot(med, 5)
    theirs = _lot(other, 5)
    oid, _ = _scheduled_order(med, pharmacist)
    resp = _dispense(oid, pharmacist, lot_id=theirs)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "lot_wrong_drug"
    _assert_nothing_written(oid, (mine, 5), (theirs, 5))


def test_with_a_substitute_only_the_substitutes_batch_may_leave(module_on):
    pharmacist = _user()
    prescribed, substitute = _drug(), _drug()
    prescribed_lot = _lot(prescribed, 5)
    substitute_lot = _lot(substitute, 5)
    oid, _ = _scheduled_order(prescribed, pharmacist, substitute=substitute)

    resp = _dispense(oid, pharmacist, lot_id=prescribed_lot)
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "lot_wrong_drug"
    _assert_nothing_written(oid, (prescribed_lot, 5), (substitute_lot, 5))

    resp = _dispense(oid, pharmacist, lot_id=substitute_lot, dispense_quantity_units=2)
    assert resp.status_code == 200, resp.text
    assert (_qty(prescribed_lot), _qty(substitute_lot)) == (5, 3)
    _, meta = _audit(oid)
    assert meta["medication_id"] == substitute and meta["substituted"] is True
    assert meta["lot_id"] == substitute_lot


def test_an_unknown_batch_is_refused(module_on):
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 5)
    oid, _ = _scheduled_order(med, pharmacist)
    for bad in ("NO-SUCH-LOT", "   "):
        resp = _dispense(oid, pharmacist, lot_id=bad)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "lot_not_found"
    _assert_nothing_written(oid, (lot, 5))


def test_an_expired_or_undated_batch_is_refused(module_on):
    pharmacist = _user()
    med = _drug()
    good = _lot(med, 5)
    expired = _lot(med, 5, expiry=PAST)
    undated = _lot(med, 5, expiry=None)
    oid, _ = _scheduled_order(med, pharmacist)
    for lot, words in ((expired, "expired on"), (undated, "no expiry date")):
        resp = _dispense(oid, pharmacist, lot_id=lot)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "lot_not_dispensable"
        assert words in resp.json()["detail"]["message"]
    _assert_nothing_written(oid, (good, 5), (expired, 5), (undated, 5))


def test_a_quarantined_batch_is_refused(module_on):
    pharmacist = _user()
    med = _drug()
    held = _lot(med, 5)
    with db_session(immediate=True) as db:
        stock.set_lot_status(db, held, "quarantined", actor_id=pharmacist, reason="test")
    oid, _ = _scheduled_order(med, pharmacist)
    resp = _dispense(oid, pharmacist, lot_id=held)
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "lot_not_dispensable"
    assert "quarantined" in resp.json()["detail"]["message"]
    _assert_nothing_written(oid, (held, 5))


def test_a_ward_stock_batch_and_a_vault_batch_of_an_ordinary_drug_are_refused(module_on):
    pharmacist = _user()
    med = _drug(ndps="none")
    ward = _store("ward_imprest")
    vault = _store("ndps_vault")
    ward_lot = _lot(med, 5, store_id=ward)
    vault_lot = _lot(med, 5, store_id=vault)
    oid, _ = _scheduled_order(med, pharmacist)
    for lot, words in ((ward_lot, "ward stock"), (vault_lot, "NDPS vault")):
        resp = _dispense(oid, pharmacist, lot_id=lot)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "lot_not_dispensable"
        assert words in resp.json()["detail"]["message"]
    _assert_nothing_written(oid, (ward_lot, 5), (vault_lot, 5))


def test_a_batch_holding_too_few_units_is_409_and_never_split(module_on):
    pharmacist = _user()
    med = _drug()
    small = _lot(med, 2, expiry=SOON)
    big = _lot(med, 10)
    oid, _ = _scheduled_order(med, pharmacist)
    resp = _dispense(oid, pharmacist, lot_id=small, dispense_quantity_units=3)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "insufficient_lot_stock"
    assert "holds 2 units" in resp.json()["detail"]["message"]
    _assert_nothing_written(oid, (small, 2), (big, 10))


def test_a_replay_after_a_shaped_dispense_writes_nothing_more(module_on):
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 6)
    oid, _ = _scheduled_order(med, pharmacist)
    key = f"shaped-{oid}"
    first = _dispense(oid, pharmacist, lot_id=lot, dispense_quantity_units=3, idempotency_key=key)
    assert first.status_code == 200
    again = _dispense(oid, pharmacist, lot_id=lot, dispense_quantity_units=3, idempotency_key=key)
    assert again.status_code == 200 and again.json()["already_applied"] is True
    assert len(_movements(oid)) == 1 and _qty(lot) == 3


# ── The gate and the audit ────────────────────────────────────────────────────

def test_the_safety_gate_still_screens_the_effective_drug(module_on):
    pharmacist = _user()
    prescribed = _drug(generic="dosimol")
    substitute = _drug(generic="amoxicillin")
    substitute_lot = _lot(substitute, 5)
    oid, pid = _scheduled_order(prescribed, pharmacist, substitute=substitute)
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
                   "status) VALUES (?, 'Penicillin', 'anaphylaxis', 'severe', 'active')", (pid,))
    resp = _dispense(oid, pharmacist, lot_id=substitute_lot, dispense_quantity_units=2)
    assert resp.status_code == 428, resp.text
    _assert_nothing_written(oid, (substitute_lot, 5))


def test_an_unwritable_audit_rolls_a_shaped_dispense_back(module_on, monkeypatch):
    pharmacist = _user()
    med = _drug()
    lot = _lot(med, 6)
    oid, _ = _scheduled_order(med, pharmacist)
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "helix_dispense_recorded":
            raise audit_service.AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    with pytest.raises(audit_service.AuditWriteError):
        helix_service.transition_order_status(oid, "dispensed", pharmacist,
                                              dispense_quantity_units=3, lot_id=lot)
    _assert_nothing_written(oid, (lot, 6))


# ── The batch picker ──────────────────────────────────────────────────────────

def test_dispense_options_list_the_effective_drugs_dispensable_batches(module_on):
    pharmacist = _user()
    prescribed, substitute = _drug(), _drug()
    _lot(prescribed, 5)
    later = _lot(substitute, 4, expiry=LATER)
    soon = _lot(substitute, 3, expiry=SOON)
    _lot(substitute, 3, expiry=PAST)
    held = _lot(substitute, 3)
    with db_session(immediate=True) as db:
        stock.set_lot_status(db, held, "quarantined", actor_id=pharmacist, reason="test")
    _lot(substitute, 3, store_id=_store("ward_imprest"))
    oid, _ = _scheduled_order(prescribed, pharmacist, substitute=substitute)

    resp = client.get(f"/api/helix/orders/{oid}/dispense-options", headers=_hdr(pharmacist))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True and body["max_units"] == 6
    assert body["medication_id"] == substitute
    assert [lot["id"] for lot in body["lots"]] == [soon, later]
    assert [lot["quantity"] for lot in body["lots"]] == [3, 4]
    assert set(body["lots"][0]) == {"id", "lot_number", "expiration_date", "quantity",
                                    "store_name"}


def test_dispense_options_are_scoped_and_unknown_orders_404(module_on):
    med = _drug()
    _lot(med, 3)
    scoped = _user(department=OTHER_DEPT)
    oid, _ = _scheduled_order(med, scoped, department=DEPT)
    resp = client.get(f"/api/helix/orders/{oid}/dispense-options", headers=_hdr(scoped))
    assert resp.status_code == 404
    resp = client.get("/api/helix/orders/NO-SUCH-ORDER/dispense-options",
                      headers=_hdr(_user()))
    assert resp.status_code == 404
    nurse = _user("nurse", DEPT)
    resp = client.get(f"/api/helix/orders/{oid}/dispense-options", headers=_hdr(nurse, "nurse"))
    assert resp.status_code == 403
