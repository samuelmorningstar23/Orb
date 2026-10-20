"""Pharmacy charges at batch MRP (grn-16, W10-A, ruling GRN-R12, ORB_PHARMACY_STORE).

With the pharmacy store on, the billing capture prices the pharmacy source from
the dispense MOVEMENT, not the order:

  * the drug charged is the one that left the shelf (the substitute when one
    was dispensed) and the description names it;
  * the line is the units dispensed times the batch MRP per unit (the pack MRP
    over the pack size, rounded once by money.py), whatever the tariff says;
  * MRP is the tax-inclusive ceiling: a taxable drug's GST sits inside it;
  * a batch with no MRP falls back to the tariff, units times its rate, and
    with no tariff either the line is skipped with a reason, never posted at 0;
  * a ward return is its own credit candidate keyed helix-return:<id>, never a
    reuse of helix:<order>, credited at the rate the order was charged, and
    only when the order's charge is on the bill;
  * a replay posts nothing twice.

With the store off the candidates are exactly what they were.

Every drug id starts TST- so its service item is exempt from
tests/test_money.py's no-seeded-prices rule.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

import config
from database import db_session
from services import billing_masters, billing_service
from services import pharmacy_stock_service as stock
from services import procurement_service as procurement
from services.helix_db import init_helix_db
from services.money import rupees


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


@pytest.fixture()
def store_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)


class _Made:
    def __init__(self):
        self.drugs: list[str] = []
        self.patients: list[str] = []
        self.stores: list[int] = []


@pytest.fixture()
def made():
    m = _Made()
    yield m
    with db_session() as db:
        for pid in m.patients:
            db.execute("DELETE FROM pharmacy_ward_returns WHERE patient_id = ?", (pid,))
        for drug in m.drugs:
            db.execute("DELETE FROM helix_stock_movements WHERE medication_id = ?", (drug,))
            db.execute("DELETE FROM pharmacy_lot_events WHERE helix_medication_id = ?", (drug,))
            db.execute("DELETE FROM helix_inventory_lots WHERE medication_id = ?", (drug,))
        for pid in m.patients:
            db.execute("DELETE FROM helix_patient_med_orders WHERE patient_id = ?", (pid,))
        for store_id in m.stores:
            db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def _drug(m, name: str) -> str:
    med = f"TST-MEDBM{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength, "
                   "drug_schedule, ndps_category, pack_size_units, storage_condition) "
                   "VALUES (?, ?, ?, 'tablet', '500 mg', 'H', 'none', 10, 'room_temperature')",
                   (med, name, name.split()[0].lower()))
    m.drugs.append(med)
    return med


def _dispensing_store(m) -> int:
    with db_session() as db:
        row = db.execute("SELECT id FROM stores WHERE pharmacy_kind = 'dispensing' "
                         "AND is_active = 1 ORDER BY id LIMIT 1").fetchone()
    if row is not None:
        return int(row["id"])
    with db_session(immediate=True) as db:
        made = procurement._upsert_store_core(db, code=f"BM{_uid()}", name=f"Dispensary {_uid()}",
                                              domain="pharmacy", pharmacy_kind="dispensing")
    m.stores.append(int(made["id"]))
    return int(made["id"])


def _lot(m, drug, units, *, mrp=1000, per_pack=10) -> dict:
    expiry = (date.fromisoformat(stock.site_today()) + timedelta(days=300)).isoformat()
    store_id = _dispensing_store(m)
    with db_session(immediate=True) as db:
        return dict(stock.open_or_merge_lot(
            db, medication_id=drug, lot_number=f"BM{_uid()}", expiration_date=expiry,
            units=units, actor_id="tester", store_id=store_id, unit_cost_paise=50,
            mrp_pack_paise=mrp, units_per_pack=per_pack, movement_type="receipt")["lot"])


def _patient(m) -> tuple[str, str]:
    pid = f"PATBM{_uid()}"
    aid = f"ADMBM{_uid()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('BMDOC', 'Dr Ilse Varga', 'doctor', 'x')")
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Corvina {pid[-4:]}", f"MRN-{pid}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'BMDOC', 'General Medicine', 'active')", (aid, pid))
    m.patients.append(pid)
    return pid, aid


def _order(m, pid, prescribed, lot, units, *, substitute=None, status="dispensed") -> str:
    oid = f"ORDBM{uuid.uuid4().hex[:8].upper()}"
    with db_session(immediate=True) as db:
        db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, "
                   "substitute_medication_id, dosage, route, frequency, status) "
                   "VALUES (?, ?, ?, ?, '500 mg', 'PO', 'TDS', ?)",
                   (oid, pid, prescribed, substitute, status))
        if lot is not None:
            stock.post_movement(db, lot_id=lot["id"], movement_type="dispense",
                                quantity_change=-units, actor_id="tester", order_id=oid,
                                reference_type="helix_order", reference_id=oid,
                                idempotency_key=f"helix_order:{oid}:dispense")
    return oid


def _return(pid, oid, drug, lot, units) -> int:
    with db_session() as db:
        cur = db.execute("INSERT INTO pharmacy_ward_returns (patient_id, helix_order_id, "
                         "helix_medication_id, lot_id, quantity_units, received_by, occurred_at) "
                         "VALUES (?, ?, ?, ?, ?, 'tester', CURRENT_TIMESTAMP)",
                         (pid, oid, drug, lot["id"], units))
        return int(cur.lastrowid)


def _charges(pid) -> list[dict]:
    return billing_service.list_charges(patient_id=pid)


def _capture(pid, aid, **kw):
    return billing_service.capture_clinical_charges(patient_id=pid, admission_id=aid, **kw)


# ═════════════════════════════════════════════════════════════════════════════

def test_a_substituted_order_bills_the_substitute_at_its_batch_mrp(made, store_on):
    prescribed = _drug(made, "Ordamycin 500 mg")
    substitute = _drug(made, "Velquinol 500 mg")
    lot = _lot(made, substitute, 30, mrp=1000, per_pack=10)          # Rs 1.00 a unit
    pid, aid = _patient(made)
    oid = _order(made, pid, prescribed, lot, 3, substitute=substitute)
    out = _capture(pid, aid)
    assert out["posted_count"] == 1
    [charge] = _charges(pid)
    assert charge["idempotency_key"] == f"helix:{oid}"
    assert charge["description"] == "Velquinol 500 mg"
    assert charge["service_code"] == f"RX-{substitute}"
    assert charge["unit_rate_paise"] == 100 and charge["quantity_milli"] == 3000
    assert charge["total_paise"] == 300
    assert "batch MRP" in charge["notes"] and lot["lot_number"] in charge["notes"]


def test_the_line_is_units_times_the_mrp_per_unit(made, store_on):
    drug = _drug(made, "Triprolan 250 mg")
    lot = _lot(made, drug, 9, mrp=1000, per_pack=3)                  # 333.33 paise a unit
    pid, aid = _patient(made)
    _order(made, pid, drug, lot, 2)
    _capture(pid, aid)
    [charge] = _charges(pid)
    assert charge["unit_rate_paise"] == 333                           # rounded once, half up
    assert charge["total_paise"] == 666                               # 2 x 333, not 666.67


def test_a_taxable_drug_carries_its_gst_inside_the_mrp(made, store_on):
    drug = _drug(made, "Halvorex 40 mg")
    billing_masters.upsert_service_item(code=drug, name="Halvorex 40 mg", category="pharmacy",
                                        gst_treatment="taxable", gst_rate_bp=1200)
    lot = _lot(made, drug, 10, mrp=10000, per_pack=10)                # Rs 10 a unit, MRP
    pid, aid = _patient(made)
    _order(made, pid, drug, lot, 3)
    _capture(pid, aid)
    [charge] = _charges(pid)
    assert charge["service_code"] == drug and charge["gst_rate_bp"] == 1200
    assert charge["gst_treatment"] == "taxable"
    assert charge["total_paise"] <= 3000                              # never above 3 x MRP
    assert charge["total_paise"] >= 2995
    assert "GST included in the MRP" in charge["notes"]


def test_a_return_is_its_own_credit_with_its_own_key(made, store_on):
    drug = _drug(made, "Pelurane 10 mg")
    lot = _lot(made, drug, 20, mrp=2500, per_pack=10)                 # Rs 2.50 a unit
    pid, aid = _patient(made)
    oid = _order(made, pid, drug, lot, 4, status="delivered")
    _capture(pid, aid)
    rid = _return(pid, oid, drug, lot, 1)
    out = _capture(pid, aid)
    reasons = {s["source_ref"]: s["reason"] for s in out["skipped"]}
    assert reasons[oid] == "already_charged"                          # the order: not again
    [credit] = [c for c in _charges(pid) if c["idempotency_key"] == f"helix-return:{rid}"]
    assert credit["total_paise"] == -250 and credit["quantity_milli"] == 1000
    assert credit["description"] == "Returned: Pelurane 10 mg, 1 unit"
    assert credit["source"] == "pharmacy" and credit["source_ref"] == str(rid)
    keys = [c["idempotency_key"] for c in _charges(pid)]
    assert keys.count(f"helix:{oid}") == 1
    again = _capture(pid, aid)
    assert again["posted_count"] == 0
    assert len(_charges(pid)) == 2


def test_a_return_whose_dispense_is_not_on_the_bill_credits_nothing(made, store_on):
    drug = _drug(made, "Quorastin 20 mg")
    lot = _lot(made, drug, 10, mrp=None, per_pack=10)                 # no MRP, no tariff
    pid, aid = _patient(made)
    oid = _order(made, pid, drug, lot, 2, status="delivered")
    rid = _return(pid, oid, drug, lot, 1)
    out = _capture(pid, aid)
    reasons = {s["source_ref"]: s["reason"] for s in out["skipped"]}
    assert reasons[oid] == "no_service_item"
    assert reasons[str(rid)] == "order_not_charged"
    assert _charges(pid) == []


def test_no_mrp_falls_back_to_the_tariff_and_no_price_at_all_is_skipped_never_zero(
        made, store_on):
    drug = _drug(made, "Ambrelix 5 mg")
    billing_masters.upsert_service_item(code=drug, name="Ambrelix 5 mg", category="pharmacy")
    lot = _lot(made, drug, 10, mrp=None, per_pack=10)
    pid, aid = _patient(made)
    oid = _order(made, pid, drug, lot, 2)
    out = _capture(pid, aid)
    assert {s["source_ref"]: s["reason"] for s in out["skipped"]}[oid] == "rate_not_configured"
    assert _charges(pid) == []
    billing_masters.upsert_service_item(code=drug, name="Ambrelix 5 mg", category="pharmacy",
                                        default_rate_paise=rupees("4"))
    dry = _capture(pid, aid, dry_run=True)
    assert dry["posted"][0]["total_paise"] == 800 and _charges(pid) == []
    _capture(pid, aid)
    [charge] = _charges(pid)
    assert (charge["unit_rate_paise"], charge["quantity_milli"], charge["total_paise"]) == (
        400, 2000, 800)


def test_a_replay_does_not_double_charge(made, store_on):
    drug = _drug(made, "Sorvadine 100 mg")
    lot = _lot(made, drug, 10, mrp=500, per_pack=5)
    pid, aid = _patient(made)
    _order(made, pid, drug, lot, 1)
    first = _capture(pid, aid)
    second = _capture(pid, aid)
    assert first["posted_count"] == 1 and second["posted_count"] == 0
    assert [s["reason"] for s in second["skipped"]] == ["already_charged"]
    assert len(_charges(pid)) == 1


def test_a_dry_run_writes_nothing(made, store_on):
    drug = _drug(made, "Melantrix 2 mg")
    lot = _lot(made, drug, 10, mrp=1200, per_pack=12)
    pid, aid = _patient(made)
    _order(made, pid, drug, lot, 5)
    dry = _capture(pid, aid, dry_run=True)
    assert dry["posted"][0]["would_post"] is True
    assert dry["posted"][0]["total_paise"] == 500
    assert _charges(pid) == []


def test_with_the_store_off_the_candidates_are_unchanged(made, monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", False)
    prescribed = _drug(made, "Ordamycin 500 mg")
    substitute = _drug(made, "Velquinol 500 mg")
    lot = _lot(made, substitute, 10)
    pid, _aid = _patient(made)
    oid = _order(made, pid, prescribed, lot, 3, substitute=substitute)
    _return(pid, oid, substitute, lot, 1)
    with db_session() as db:
        created = db.execute("SELECT created_at FROM helix_patient_med_orders WHERE id = ?",
                             (oid,)).fetchone()[0]
        candidates = billing_service._clinical_candidates(db, patient_id=pid)
    assert candidates == [{
        "source": "pharmacy",
        "source_ref": oid,
        "idempotency_key": f"helix:{oid}",
        "candidate_codes": [prescribed, f"RX-{prescribed}"],
        "description": "Ordamycin 500 mg",
        "service_date": billing_service._local_day_of_stored(created),
        "kind": "pharmacy",
        "label": "pharmacy: Ordamycin 500 mg",
    }]
