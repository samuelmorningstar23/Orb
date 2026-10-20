"""Pharmacy stock and the pharmacy store per facility (multisite-14 and
multisite-8h3, W13-C, wave 13).

Ruling MS-R9 (a): pharmacy stock and the pharmacy store are per facility (a
drug licence is per premises and stock must be where the patient is), so
F109's "hospital-wide" pharmacy means facility-wide.

Pinned here, on a database this module owns (isolated_module_database), with
a second facility and the fence opened (FACILITY_BLIND_SURFACES = ()) so each
lane answers for itself:

  * a lot takes its store's facility and a movement its lot's;
  * an F2 dispense never draws an F1 lot, even one that expires sooner; with
    no F2 stock it is refused, never drawn from F1; a pharmacist naming an F1
    batch for an F2 patient gets lot_not_found; the batch picker lists the
    patient's facility only; an F1 dispense still takes the F1 lot;
  * the Helix inventory and stock movements are the caller's facility's, and
    the inventory names that facility for the HelixPanel label;
  * every pharmacy store list and report keeps the caller's facilities, and a
    drug's own reorder level is judged against each facility's own shelf;
  * a store, order, receipt, return, transfer or NDPS variance of another
    facility is 404 after an authorization 'facility_document_denied' row,
    before anything is read or written; a store created for another
    facility's department is 403 facility_scope with nothing written;
  * a transfer never crosses facilities (409 cross_facility_transfer, even for
    a group-wide account), and a ward indent is issued from the ward store's
    own facility's dispensing store;
  * legacy stock is adopted only by its own facility's dispensing store, and
    a lot whose stamp and store disagree is at no facility (never dispensed,
    reported by reconcile);
  * with one facility on record every clause is empty and every pick, list
    and gate is exactly as before;
  * on the call graph /api/pharmacy-store reaches no blind SQL owner except,
    at most, the NDPS incident lane (NABH ward list, multisite-12, which
    W13-A sweeps in this wave).

Test patients carry invented names (never a word a clinician types). Run with
the test env, never the shipping .env.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import config
import facility_fence as F
import main
from auth import create_token
from database import db_session
from services import facility_documents as FD
from services import facility_service as FS
from services import helix_service
from services import ndps_service
from services import pharmacy_facility as PF
from services import pharmacy_stock_reports as reports
from services import pharmacy_stock_service as stock
from services import pharmacy_transfer_service as transfers
from services import procurement_service as procurement
from services.clinical_time import utc_iso_now

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9871  # an id no other module writes
F2_CODE = f"PS{TAG[:4]}"
F2_NAME = "Quellmarsh Satellite"
F1_DEPT = f"Dravenholt Medicine {TAG}"
F2_DEPT = f"Quellmarsh Medicine {TAG}"
BASE = "/api/pharmacy-store"

client = TestClient(main.app, raise_server_exceptions=False)


def _hex(n: int = 6) -> str:
    return uuid.uuid4().hex[:n].upper()


def _days(n: int) -> str:
    return (date.fromisoformat(stock.site_today()) + timedelta(days=n)).isoformat()


def _user(db, role: str, *, department=None, home=None) -> str:
    uid = f"PS{role[:3].upper()}{_hex(6)}"
    db.execute(
        "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, 'x')",
        (uid, f"Ysolde Varrick {uid[-4:]}", role, department),
    )
    if home is not None:
        db.execute(
            "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, "
            "reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, 'pharmacy stock sweep')",
            (uid, home),
        )
    return uid


def _patient(db, name: str, *, facility: int, department: str) -> str:
    pid = f"PSP{_hex(8)}"
    db.execute(
        "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, status, "
        "registered_facility_id) VALUES (?, ?, ?, 61, 'M', ?, '4', 'admitted', ?)",
        (pid, name, f"MRN-{pid}", f"Ward {TAG}", facility),
    )
    db.execute(
        "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status, "
        "facility_id, created_at) VALUES (?, ?, 'system', ?, ?, '4', 'active', ?, ?)",
        (f"PSA{_hex(8)}", pid, department, f"Ward {TAG}", facility, "2026-09-01T08:00:00+00:00"),
    )
    return pid


def _drug(db, label: str, **fields) -> str:
    med = f"PSM{_hex(8)}"
    cols = {
        "id": med,
        "name": f"{label} {med[-4:]} 10 mg",
        "generic_name": label.lower(),
        "form": "tablet",
        "strength": "10 mg",
        "drug_schedule": "H",
        "ndps_category": "none",
        "pack_size_units": 10,
    } | fields
    db.execute(
        f"INSERT INTO helix_medication_master ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        tuple(cols.values()),
    )
    return med


def _store(db, kind: str, *, incharge=None, department=None) -> dict:
    return dict(
        procurement._upsert_store_core(
            db,
            code=f"PS{_hex(6)}",
            name=f"Orvanne {kind} {_hex(3)}",
            domain="pharmacy",
            pharmacy_kind=kind,
            incharge_user_id=incharge,
            department=department,
        )
    )


def _lot(store_id, drug, units, expiry_days, *, batch=None) -> dict:
    with db_session(immediate=True) as db:
        out = stock.open_or_merge_lot(
            db,
            medication_id=drug,
            lot_number=batch or f"B{_hex(5)}",
            expiration_date=_days(expiry_days),
            units=units,
            actor_id="tester",
            store_id=store_id,
            units_per_pack=10,
        )
    return out["lot"]


def _order(patient_id, drug, prescriber, *, status="scheduled") -> str:
    oid = f"PSO-{_hex(8)}"
    with db_session() as db:
        db.execute(
            "INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, status, "
            "prescribed_by, dosage, frequency, route) VALUES (?, ?, ?, ?, ?, '10 mg', 'OD', "
            "'oral')",
            (oid, patient_id, drug, status, prescriber),
        )
    return oid


def _qty(lot_id) -> int:
    with db_session() as db:
        return int(
            db.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?", (lot_id,))
            .fetchone()[0]
        )


def _headers(uid: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _denials(target_type: str, target_id) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT event_category, outcome, actor_user_id, metadata_json FROM audit_logs "
            "WHERE action_type = ? AND target_type = ? AND target_id = ? ORDER BY id",
            (FD.DENIAL_ACTION, target_type, str(target_id)),
        ).fetchall()
    return [{**dict(r), "metadata": json.loads(r["metadata_json"] or "{}")} for r in rows]


def _err(resp) -> str:
    return resp.json()["detail"]["error"]


# ── the world ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def world(isolated_module_database):
    from services.helix_db import init_helix_db

    init_helix_db()
    w: dict = {}
    with db_session() as db:
        db.execute(
            "INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
            (F2, str(uuid.uuid4()), F2_CODE, F2_NAME),
        )
        for name, fid in ((F1_DEPT, 1), (F2_DEPT, F2)):
            db.execute(
                "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
                (name, fid),
            )
        w["doc1"] = _user(db, "doctor", department=F1_DEPT)
        w["doc2"] = _user(db, "doctor", department=F2_DEPT)
        w["pharm1"] = _user(db, "pharmacist")
        w["pharm2"] = _user(db, "pharmacist", home=F2)
        w["admin1"] = _user(db, "admin")
        w["admin2"] = _user(db, "admin", home=F2)
        w["admin_g"] = _user(db, "admin")
        db.execute(
            "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
            "VALUES (?, 'system', CURRENT_TIMESTAMP, 'group pharmacy audit in the sweep test')",
            (w["admin_g"],),
        )
        w["nurse1"] = _user(db, "nurse", department=F1_DEPT)
        w["nurse2"] = _user(db, "nurse", department=F2_DEPT)
        w["p1"] = _patient(db, "Tobrin Halvessey", facility=1, department=F1_DEPT)
        w["p2"] = _patient(db, "Ismay Corrowdine", facility=F2, department=F2_DEPT)
        w["drug"] = _drug(db, "Velmorastine")
        w["only_f1"] = _drug(db, "Strevalide")
        w["reorder"] = _drug(
            db, "Pelluvanine", reorder_level_units=15, reorder_qty_units=20
        )
        w["ward_drug"] = _drug(db, "Corrasolate")
    FS.invalidate_cache()
    assert FS.multi_facility_data() is True
    with db_session(immediate=True) as db:
        existing = db.execute(
            "SELECT * FROM stores WHERE pharmacy_kind = 'dispensing' AND is_active = 1 "
            "AND facility_id = 1"
        ).fetchone()
        w["disp1"] = (
            dict(existing)
            if existing is not None
            else _store(db, "dispensing", incharge=w["pharm1"])
        )
        w["disp2"] = _store(db, "dispensing", incharge=w["pharm2"])
        w["sub1"] = _store(db, "sub_store", incharge=w["pharm1"])
        w["sub2"] = _store(db, "sub_store", incharge=w["pharm2"])
    assert w["disp2"]["facility_id"] == F2 and w["sub2"]["facility_id"] == F2
    assert w["sub1"]["facility_id"] == 1
    # The F1 lot of the dispensed drug expires SOONER than the F2 lot, so an
    # earliest-expiry pick across facilities would take it.
    w["lot1"] = _lot(w["disp1"]["id"], w["drug"], 10, 60)
    w["lot2"] = _lot(w["disp2"]["id"], w["drug"], 10, 300)
    w["lot_only_f1"] = _lot(w["disp1"]["id"], w["only_f1"], 10, 200)
    w["reorder1"] = _lot(w["disp1"]["id"], w["reorder"], 10, 200)
    w["reorder2"] = _lot(w["disp2"]["id"], w["reorder"], 10, 200)
    w["h"] = {
        k: _headers(w[k], role)
        for k, role in (
            ("pharm1", "pharmacist"),
            ("pharm2", "pharmacist"),
            ("admin1", "admin"),
            ("admin2", "admin"),
            ("admin_g", "admin"),
            ("nurse1", "nurse"),
            ("nurse2", "nurse"),
        )
    }
    try:
        yield w
    finally:
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def switches(monkeypatch):
    """The fence as it will stand once every screen is swept, and the
    pharmacy store module on."""
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


def _one_facility(monkeypatch) -> None:
    """Every install today: one facility on record (the cached count)."""
    monkeypatch.setattr(FS, "multi_facility_data", lambda *, db=None: False)


# ═════════════════════════════════════════════════════════════════════════════
#  1. Lots and movements carry the facility
# ═════════════════════════════════════════════════════════════════════════════


def test_a_new_lot_takes_its_store_facility_and_its_movements_the_lot_facility(world):
    with db_session() as db:
        lot = db.execute(
            "SELECT facility_id FROM helix_inventory_lots WHERE id = ?", (world["lot2"]["id"],)
        ).fetchone()
        moves = db.execute(
            "SELECT DISTINCT facility_id FROM helix_stock_movements WHERE lot_id = ?",
            (world["lot2"]["id"],),
        ).fetchall()
        one = db.execute(
            "SELECT facility_id FROM helix_inventory_lots WHERE id = ?", (world["lot1"]["id"],)
        ).fetchone()
    assert lot["facility_id"] == F2
    assert [r[0] for r in moves] == [F2]
    assert one["facility_id"] == 1
    with db_session() as db:
        assert PF.lot_facility(db, world["lot2"]["id"]) == F2
        assert PF.lot_facility(db, world["lot1"]["id"]) == 1


def test_with_one_facility_a_new_lot_keeps_the_default_and_nothing_extra_is_written(
    world, monkeypatch
):
    _one_facility(monkeypatch)
    drug = None
    with db_session() as db:
        drug = _drug(db, "Thessarine")
    lot = _lot(world["disp2"]["id"], drug, 3, 200)
    with db_session() as db:
        row = db.execute(
            "SELECT facility_id FROM helix_inventory_lots WHERE id = ?", (lot["id"],)
        ).fetchone()
        mv = db.execute(
            "SELECT facility_id FROM helix_stock_movements WHERE lot_id = ?", (lot["id"],)
        ).fetchone()
    # One facility on record: nothing stamps, the column default stands.
    assert row["facility_id"] == 1 and mv["facility_id"] == 1


# ═════════════════════════════════════════════════════════════════════════════
#  2. Dispensing draws only the admission facility's lots
# ═════════════════════════════════════════════════════════════════════════════


def test_an_f2_dispense_never_draws_an_f1_lot_even_one_expiring_sooner(world):
    oid = _order(world["p2"], world["drug"], world["doc2"])
    before1, before2 = _qty(world["lot1"]["id"]), _qty(world["lot2"]["id"])
    out = helix_service.transition_order_status(oid, "dispensed", world["pharm2"])
    assert out["ok"] and out["order"]["status"] == "dispensed"
    assert _qty(world["lot1"]["id"]) == before1
    assert _qty(world["lot2"]["id"]) == before2 - 1
    with db_session() as db:
        mv = db.execute(
            "SELECT lot_id, facility_id FROM helix_stock_movements WHERE order_id = ?", (oid,)
        ).fetchone()
    assert mv["lot_id"] == world["lot2"]["id"] and mv["facility_id"] == F2


def test_an_f1_dispense_still_takes_the_f1_lot(world):
    oid = _order(world["p1"], world["drug"], world["doc1"])
    before1, before2 = _qty(world["lot1"]["id"]), _qty(world["lot2"]["id"])
    helix_service.transition_order_status(oid, "dispensed", world["pharm1"])
    assert _qty(world["lot1"]["id"]) == before1 - 1
    assert _qty(world["lot2"]["id"]) == before2


def test_with_no_f2_stock_the_dispense_is_refused_not_drawn_from_f1(world):
    oid = _order(world["p2"], world["only_f1"], world["doc2"])
    before = _qty(world["lot_only_f1"]["id"])
    with pytest.raises(ValueError) as exc:
        helix_service.transition_order_status(oid, "dispensed", world["pharm2"])
    assert "No stock of" in str(exc.value) and F2_NAME in str(exc.value)
    assert _qty(world["lot_only_f1"]["id"]) == before
    with db_session() as db:
        status = db.execute(
            "SELECT status FROM helix_patient_med_orders WHERE id = ?", (oid,)
        ).fetchone()[0]
        moves = db.execute(
            "SELECT COUNT(*) FROM helix_stock_movements WHERE order_id = ?", (oid,)
        ).fetchone()[0]
    assert status == "scheduled" and moves == 0


def test_the_dispense_route_refuses_an_f2_dispense_with_only_f1_stock(world):
    oid = _order(world["p2"], world["only_f1"], world["doc2"])
    resp = client.post(
        f"/api/helix/orders/{oid}/transition",
        headers=world["h"]["pharm2"],
        json={"target_status": "dispensed"},
    )
    assert resp.status_code == 400, resp.text
    assert F2_NAME in resp.json()["detail"]
    assert _qty(world["lot_only_f1"]["id"]) == 10


def test_a_pharmacist_naming_an_f1_batch_for_an_f2_patient_is_refused(world):
    oid = _order(world["p2"], world["drug"], world["doc2"])
    before = _qty(world["lot1"]["id"])
    with pytest.raises(helix_service.DispenseRefused) as exc:
        helix_service.transition_order_status(
            oid, "dispensed", world["pharm2"], lot_id=world["lot1"]["id"]
        )
    assert exc.value.code == "lot_not_found" and exc.value.status == 422
    assert _qty(world["lot1"]["id"]) == before


def test_the_batch_picker_lists_only_the_patient_facility_batches(world):
    oid = _order(world["p2"], world["drug"], world["doc2"])
    options = helix_service.dispense_options(oid)
    assert options["enabled"] is True
    assert [lot["id"] for lot in options["lots"]] == [world["lot2"]["id"]]
    oid1 = _order(world["p1"], world["drug"], world["doc1"])
    assert [lot["id"] for lot in helix_service.dispense_options(oid1)["lots"]] == [
        world["lot1"]["id"]
    ]


def test_pre_flight_counts_the_patient_facility_shelf(world):
    assert helix_service.pre_flight_inventory_check(world["only_f1"])["in_stock"] is True
    named = helix_service.pre_flight_inventory_check(world["only_f1"], patient_id=world["p2"])
    assert named["in_stock"] is False and named["total_quantity"] == 0


def test_with_one_facility_the_dispense_is_earliest_expiry_across_the_shelf(world, monkeypatch):
    _one_facility(monkeypatch)
    oid = _order(world["p2"], world["drug"], world["doc2"])
    before1 = _qty(world["lot1"]["id"])
    helix_service.transition_order_status(oid, "dispensed", world["pharm2"])
    # The pick is exactly as before: the earliest expiry on any shelf.
    assert _qty(world["lot1"]["id"]) == before1 - 1


def test_the_dispensable_predicate_is_unchanged_without_a_facility():
    assert stock.shelf_lot_sql("l") == stock.shelf_lot_sql("l", facility_id=None)
    assert stock.dispensable_lot_sql("l").count("?") == 1
    assert stock.dispensable_lot_sql("l", facility_id=F2).count("?") == 1
    assert f"facility_id = {F2}" in stock.dispensable_lot_sql("l", facility_id=F2)
    assert "facility_id" not in stock.shelf_lot_sql("l")


# ═════════════════════════════════════════════════════════════════════════════
#  3. Helix inventory, movements and the facility label
# ═════════════════════════════════════════════════════════════════════════════


def test_the_helix_inventory_is_the_caller_facility_stock_and_names_it(world):
    resp = client.get("/api/helix/inventory", headers=world["h"]["pharm2"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {lot["id"] for lot in body["inventory"]}
    assert world["lot2"]["id"] in ids and world["lot1"]["id"] not in ids
    assert all(lot["facility_id"] == F2 for lot in body["inventory"])
    assert body["facility"] == {"id": F2, "code": F2_CODE, "name": F2_NAME}
    one = client.get("/api/helix/inventory", headers=world["h"]["pharm1"]).json()
    assert world["lot1"]["id"] in {lot["id"] for lot in one["inventory"]}
    assert world["lot2"]["id"] not in {lot["id"] for lot in one["inventory"]}
    assert one["facility"]["id"] == 1


def test_a_group_wide_account_sees_every_facility_stock_with_no_single_label(world):
    body = client.get("/api/helix/inventory", headers=world["h"]["admin_g"]).json()
    ids = {lot["id"] for lot in body["inventory"]}
    assert {world["lot1"]["id"], world["lot2"]["id"]} <= ids
    assert "facility" not in body


def test_the_stock_movements_are_the_caller_facility_movements(world):
    rows = client.get("/api/helix/stock-movements", headers=world["h"]["pharm2"]).json()[
        "movements"
    ]
    lots = {r["lot_id"] for r in rows}
    assert world["lot2"]["id"] in lots and world["lot1"]["id"] not in lots


def test_with_one_facility_the_inventory_answer_is_unchanged(world, monkeypatch):
    _one_facility(monkeypatch)
    body = client.get("/api/helix/inventory", headers=world["h"]["pharm2"]).json()
    assert "facility" not in body
    ids = {lot["id"] for lot in body["inventory"]}
    assert {world["lot1"]["id"], world["lot2"]["id"]} <= ids


# ═════════════════════════════════════════════════════════════════════════════
#  4. Masters, stock reports and reorder per facility
# ═════════════════════════════════════════════════════════════════════════════


def test_the_store_list_keeps_the_caller_facilities(world):
    stores = client.get(f"{BASE}/stores", headers=world["h"]["pharm2"]).json()["stores"]
    codes = {s["code"] for s in stores}
    assert world["disp2"]["code"] in codes and world["sub2"]["code"] in codes
    assert world["disp1"]["code"] not in codes and world["sub1"]["code"] not in codes


def test_editing_another_facility_store_is_404_with_the_audit_row(world):
    resp = client.put(
        f"{BASE}/stores/{world['sub1']['code']}",
        headers=world["h"]["admin2"],
        json={"name": "Renamed by the satellite"},
    )
    assert resp.status_code == 404 and _err(resp) == "not_found"
    rows = _denials("pharmacy_store", world["sub1"]["code"])
    assert rows and rows[-1]["actor_user_id"] == world["admin2"]
    meta = rows[-1]["metadata"]
    assert meta["lane"] == PF.LANE and meta["scope_cause"] == "facility"
    assert meta["document_facility_id"] == 1 and meta["facility_scope"] == [F2]
    with db_session() as db:
        name = db.execute(
            "SELECT name FROM stores WHERE id = ?", (world["sub1"]["id"],)
        ).fetchone()[0]
    assert name != "Renamed by the satellite"


def test_a_store_for_another_facility_department_is_refused_with_nothing_written(world):
    code = f"PSX{_hex(4)}"
    resp = client.post(
        f"{BASE}/stores",
        headers=world["h"]["admin2"],
        json={"code": code, "name": "Dravenholt sub-store", "pharmacy_kind": "sub_store",
              "department": F1_DEPT},
    )
    assert resp.status_code == 403 and _err(resp) == "facility_scope", resp.text
    with db_session() as db:
        assert db.execute("SELECT 1 FROM stores WHERE code = ?", (code,)).fetchone() is None


def test_a_par_level_in_another_facility_store_is_404(world):
    resp = client.put(
        f"{BASE}/par-levels",
        headers=world["h"]["pharm2"],
        json={"store_id": world["sub1"]["id"], "helix_medication_id": world["drug"],
              "par_units": 5},
    )
    assert resp.status_code == 404 and _err(resp) == "not_found"
    with db_session() as db:
        assert db.execute(
            "SELECT 1 FROM pharmacy_par_levels WHERE store_id = ?", (world["sub1"]["id"],)
        ).fetchone() is None


def test_the_stock_reports_keep_the_caller_facilities(world):
    rows = client.get(f"{BASE}/stock", headers=world["h"]["pharm2"]).json()["rows"]
    assert rows and all(r["store_id"] in (world["disp2"]["id"], world["sub2"]["id"]) for r in rows)
    lots = client.get(f"{BASE}/stock/lots", headers=world["h"]["pharm2"]).json()["lots"]
    assert world["lot1"]["id"] not in {lot["id"] for lot in lots}
    ledger = client.get(f"{BASE}/ledger", headers=world["h"]["pharm2"]).json()["movements"]
    assert world["lot1"]["id"] not in {m["lot_id"] for m in ledger}
    assert world["lot2"]["id"] in {m["lot_id"] for m in ledger}
    recon = client.get(f"{BASE}/reconcile", headers=world["h"]["pharm2"]).json()
    with db_session() as db:
        # The lots stamped at F2 or held in F2's stores (a lot whose two
        # disagree is still reported to the facility that holds it).
        f2_lots = db.execute(
            "SELECT COUNT(*) FROM helix_inventory_lots WHERE facility_id = ? OR store_id IN "
            "(SELECT id FROM stores WHERE facility_id = ?)",
            (F2, F2),
        ).fetchone()[0]
        f1_only = db.execute(
            "SELECT COUNT(*) FROM helix_inventory_lots WHERE facility_id = 1 AND (store_id IS "
            "NULL OR store_id IN (SELECT id FROM stores WHERE facility_id = 1))"
        ).fetchone()[0]
    assert recon["lots_checked"] == f2_lots and f1_only > 0
    resp = client.get(
        f"{BASE}/stock?store_id={world['disp1']['id']}", headers=world["h"]["pharm2"]
    )
    assert resp.status_code == 404


def test_a_drug_reorder_level_is_judged_against_each_facility_shelf(world):
    rows = client.get(f"{BASE}/reorder", headers=world["h"]["pharm2"]).json()["rows"]
    mine = [r for r in rows if r["drug_id"] == world["reorder"] and r["basis"] == "drug_default"]
    # 10 on the F2 shelf is below the level of 15, whatever F1 holds.
    assert len(mine) == 1 and mine[0]["store_id"] == world["disp2"]["id"]
    assert mine[0]["stock_units"] == 10 and mine[0]["needs_reorder"] is True
    group = reports.reorder_report(facilities=None)["rows"]
    per_store = {
        r["store_id"]: r["stock_units"]
        for r in group
        if r["drug_id"] == world["reorder"] and r["basis"] == "drug_default"
    }
    assert per_store == {world["disp1"]["id"]: 10, world["disp2"]["id"]: 10}


def test_with_one_facility_the_reorder_report_judges_one_shelf(world, monkeypatch):
    _one_facility(monkeypatch)
    rows = reports.reorder_report(facilities=None)["rows"]
    mine = [r for r in rows if r["drug_id"] == world["reorder"] and r["basis"] == "drug_default"]
    assert len(mine) == 1 and mine[0]["stock_units"] == 20 and mine[0]["needs_reorder"] is False


# ═════════════════════════════════════════════════════════════════════════════
#  5. Documents of another facility are 404 with the audit row
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def documents(world):
    d: dict = {}
    with db_session() as db:
        vendor = db.execute(
            "INSERT INTO vendors (code, name) VALUES (?, ?)", (f"PSV{TAG}", "Ardmoor Drug House")
        ).lastrowid
        d["po1"] = int(
            db.execute(
                "INSERT INTO purchase_orders (vendor_id, store_id, status, domain, facility_id) "
                "VALUES (?, ?, 'draft', 'pharmacy', 1)",
                (vendor, world["disp1"]["id"]),
            ).lastrowid
        )
        d["po2"] = int(
            db.execute(
                "INSERT INTO purchase_orders (vendor_id, store_id, status, domain, facility_id) "
                "VALUES (?, ?, 'draft', 'pharmacy', ?)",
                (vendor, world["disp2"]["id"], F2),
            ).lastrowid
        )
        d["grn1"] = int(
            db.execute(
                "INSERT INTO goods_receipts (purchase_order_id, vendor_id, store_id, domain, "
                "facility_id) VALUES (?, ?, ?, 'pharmacy', 1)",
                (d["po1"], vendor, world["disp1"]["id"]),
            ).lastrowid
        )
        d["vr1"] = int(
            db.execute(
                "INSERT INTO pharmacy_vendor_returns (vendor_id, store_id) VALUES (?, ?)",
                (vendor, world["disp1"]["id"]),
            ).lastrowid
        )
        d["var1"] = int(
            db.execute(
                "INSERT INTO ndps_variances (lane, store_id, helix_medication_id, variance_kind, "
                "raised_by, raised_at) VALUES (?, ?, ?, 'count_mismatch', 'system', ?)",
                (ndps_service.current_lane(), world["disp1"]["id"], world["drug"], utc_iso_now()),
            ).lastrowid
        )
        d["tr1"] = int(
            db.execute(
                "INSERT INTO pharmacy_transfers (from_store_id, to_store_id, status, created_by) "
                "VALUES (?, ?, 'draft', 'system')",
                (world["disp1"]["id"], world["sub1"]["id"]),
            ).lastrowid
        )
    return d


def test_an_f1_purchase_order_is_404_to_f2_and_left_out_of_its_list(world, documents):
    resp = client.get(
        f"{BASE}/purchase-orders/{documents['po1']}", headers=world["h"]["pharm2"]
    )
    assert resp.status_code == 404 and _err(resp) == "not_found"
    assert _denials("pharmacy_purchase_order", documents["po1"])
    listed = client.get(f"{BASE}/purchase-orders", headers=world["h"]["pharm2"]).json()
    ids = {p["id"] for p in listed["purchase_orders"]}
    assert documents["po2"] in ids and documents["po1"] not in ids
    for step in ("submit", "approve", "issue"):
        hdr = world["h"]["pharm2"] if step == "submit" else world["h"]["admin2"]
        resp = client.post(f"{BASE}/purchase-orders/{documents['po1']}/{step}", headers=hdr)
        assert resp.status_code == 404, (step, resp.text)
    with db_session() as db:
        status = db.execute(
            "SELECT status FROM purchase_orders WHERE id = ?", (documents["po1"],)
        ).fetchone()[0]
    assert status == "draft"


def test_an_f1_goods_receipt_is_404_to_f2(world, documents):
    resp = client.get(f"{BASE}/goods-receipts/{documents['grn1']}", headers=world["h"]["pharm2"])
    assert resp.status_code == 404
    resp = client.post(
        f"{BASE}/goods-receipts/{documents['grn1']}/post", headers=world["h"]["pharm2"], json={}
    )
    assert resp.status_code == 404
    listed = client.get(f"{BASE}/goods-receipts", headers=world["h"]["pharm2"]).json()
    assert documents["grn1"] not in {g["id"] for g in listed["goods_receipts"]}
    assert _denials("pharmacy_goods_receipt", documents["grn1"])


def test_a_receipt_against_an_f1_order_is_404_to_f2(world, documents):
    resp = client.post(
        f"{BASE}/goods-receipts",
        headers=world["h"]["pharm2"],
        json={"purchase_order_id": documents["po1"], "lines": [{"po_line_no": 1, "packs": 1}]},
    )
    assert resp.status_code == 404 and _err(resp) == "not_found"


def test_an_f1_return_to_vendor_is_404_to_f2(world, documents):
    resp = client.get(f"{BASE}/vendor-returns/{documents['vr1']}", headers=world["h"]["pharm2"])
    assert resp.status_code == 404
    listed = client.get(f"{BASE}/vendor-returns", headers=world["h"]["pharm2"]).json()
    assert documents["vr1"] not in {v["id"] for v in listed["vendor_returns"]}
    resp = client.get(
        f"{BASE}/vendor-returns/stock?store_code={world['disp1']['code']}",
        headers=world["h"]["pharm2"],
    )
    assert resp.status_code == 404
    assert _denials("pharmacy_vendor_return", documents["vr1"])


def test_an_f1_ndps_variance_is_404_to_f2_and_left_out_of_its_list(world, documents):
    listed = client.get(
        f"{BASE}/ndps/variances?status=all", headers=world["h"]["pharm2"]
    ).json()
    assert documents["var1"] not in {v["id"] for v in listed["variances"]}
    resp = client.post(
        f"{BASE}/ndps/variances/{documents['var1']}/reconcile",
        headers=world["h"]["pharm2"],
        json={"note": "counted again at the satellite", "witness_id": "x", "witness_pin": "1"},
    )
    assert resp.status_code == 404 and _err(resp) == "not_found", resp.text
    assert _denials("ndps_variance", documents["var1"])
    with db_session() as db:
        status = db.execute(
            "SELECT status FROM ndps_variances WHERE id = ?", (documents["var1"],)
        ).fetchone()[0]
    assert status == "open"
    resp = client.get(
        f"{BASE}/ndps/balances?store_id={world['disp1']['id']}", headers=world["h"]["pharm2"]
    )
    assert resp.status_code == 404


def test_an_f1_transfer_is_404_to_f2(world, documents):
    resp = client.get(f"{BASE}/transfers/{documents['tr1']}", headers=world["h"]["pharm2"])
    assert resp.status_code == 404
    resp = client.post(
        f"{BASE}/transfers/{documents['tr1']}/cancel",
        headers=world["h"]["pharm2"],
        json={"reason": "not ours"},
    )
    assert resp.status_code == 404
    listed = client.get(f"{BASE}/transfers", headers=world["h"]["pharm2"]).json()["transfers"]
    assert documents["tr1"] not in {t["id"] for t in listed}
    assert _denials("pharmacy_transfer", documents["tr1"])
    with db_session() as db:
        status = db.execute(
            "SELECT status FROM pharmacy_transfers WHERE id = ?", (documents["tr1"],)
        ).fetchone()[0]
    assert status == "draft"


# ═════════════════════════════════════════════════════════════════════════════
#  6. Transfers never cross facilities; ward issues come from the own facility
# ═════════════════════════════════════════════════════════════════════════════


def test_a_transfer_naming_another_facility_store_is_404(world):
    resp = client.post(
        f"{BASE}/transfers",
        headers=world["h"]["pharm1"],
        json={"from_store_code": world["disp1"]["code"], "to_store_code": world["sub2"]["code"],
              "lines": [{"helix_medication_id": world["drug"], "quantity_units": 1}]},
    )
    assert resp.status_code == 404 and _err(resp) == "not_found"
    assert _denials("pharmacy_store", world["sub2"]["code"])


def test_a_transfer_between_facilities_is_refused_even_for_a_group_wide_account(world):
    actor = {"id": world["admin_g"], "role": "admin"}
    with pytest.raises(transfers.TransferError) as exc:
        transfers.create_transfer(
            from_store_code=world["disp1"]["code"],
            to_store_code=world["sub2"]["code"],
            lines=[{"helix_medication_id": world["drug"], "quantity_units": 1}],
            actor=actor,
        )
    assert exc.value.code == "cross_facility_transfer"
    with db_session() as db:
        assert db.execute(
            "SELECT 1 FROM pharmacy_transfers WHERE from_store_id = ? AND to_store_id = ?",
            (world["disp1"]["id"], world["sub2"]["id"]),
        ).fetchone() is None


def test_a_transfer_inside_one_facility_still_drafts(world):
    resp = client.post(
        f"{BASE}/transfers",
        headers=world["h"]["pharm2"],
        json={"from_store_code": world["disp2"]["code"], "to_store_code": world["sub2"]["code"],
              "lines": [{"helix_medication_id": world["drug"], "quantity_units": 1}]},
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()["transfer"]
    assert doc["from_store_id"] == world["disp2"]["id"]
    options = client.get(f"{BASE}/transfers/options", headers=world["h"]["pharm2"]).json()
    codes = {s["code"] for s in options["stores"]}
    assert world["disp2"]["code"] in codes and world["disp1"]["code"] not in codes
    client.post(
        f"{BASE}/transfers/{doc['id']}/cancel", headers=world["h"]["pharm2"],
        json={"reason": "drafted only to show the facility rule"},
    )


@pytest.fixture
def ward_on(monkeypatch):
    monkeypatch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", True)


@pytest.fixture(scope="module")
def wards(world):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "PHARMACY_STORE_ENABLED", True)
        patch.setattr(config, "PHARMACY_WARD_STOCK_ENABLED", True)
        with db_session(immediate=True) as db:
            out = {
                "ward1": _store(db, "ward_imprest", department=F1_DEPT),
                "ward2": _store(db, "ward_imprest", department=F2_DEPT),
            }
        for key in ("ward1", "ward2"):
            db.execute(
                "INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units, "
                "max_units, is_active) VALUES (?, ?, 20, 30, 1)",
                (out[key]["id"], world["ward_drug"]),
            )
    assert out["ward1"]["facility_id"] == 1 and out["ward2"]["facility_id"] == F2
    return out


def test_each_desk_and_ward_sees_only_its_facility_ward_stores(world, wards, ward_on):
    for who, mine, other in (
        ("nurse2", "ward2", "ward1"),
        ("pharm2", "ward2", "ward1"),
        ("pharm1", "ward1", "ward2"),
    ):
        body = client.get(f"{BASE}/ward/stock", headers=world["h"][who]).json()
        codes = {s["store"]["code"] if "store" in s else s.get("code") for s in body["stores"]}
        assert wards[mine]["code"] in codes, (who, codes)
        assert wards[other]["code"] not in codes, (who, codes)


def test_a_ward_indent_is_issued_from_its_own_facility_dispensing_store(world, wards, ward_on):
    resp = client.post(
        f"{BASE}/ward/indents",
        headers=world["h"]["nurse2"],
        json={"store_code": wards["ward2"]["code"],
              "lines": [{"helix_medication_id": world["ward_drug"], "quantity_units": 5}]},
    )
    assert resp.status_code == 200, resp.text
    indent = resp.json()["indent"]
    assert indent["issuing_store_code"] == world["disp2"]["code"]
    with db_session() as db:
        fid = db.execute(
            "SELECT facility_id FROM indents WHERE id = ?", (indent["id"],)
        ).fetchone()[0]
    assert fid == F2
    resp = client.post(
        f"{BASE}/ward/indents",
        headers=world["h"]["nurse2"],
        json={"store_code": wards["ward1"]["code"],
              "lines": [{"helix_medication_id": world["ward_drug"], "quantity_units": 5}]},
    )
    assert resp.status_code == 404
    assert _denials("pharmacy_store", wards["ward1"]["code"])


# ═════════════════════════════════════════════════════════════════════════════
#  7. Legacy stock, lots at no facility, ward consumption
# ═════════════════════════════════════════════════════════════════════════════


def test_legacy_stock_is_adopted_only_by_its_own_facility_dispensing_store(world):
    with db_session() as db:
        drug = _drug(db, "Halveronide")
    with db_session(immediate=True) as db:
        legacy = stock.open_or_merge_lot(
            db, medication_id=drug, lot_number=f"L{_hex(4)}", expiration_date=_days(200),
            units=4, actor_id="tester",
        )["lot"]
    with db_session(immediate=True) as db:
        at_f2 = stock.adopt_legacy_lots(db, world["disp2"]["id"], actor_id="tester")
    assert legacy["id"] not in at_f2["adopted"]
    with db_session(immediate=True) as db:
        at_f1 = stock.adopt_legacy_lots(db, world["disp1"]["id"], actor_id="tester")
    assert legacy["id"] in at_f1["adopted"]


def test_a_lot_whose_stamp_and_store_disagree_is_at_no_facility(world):
    with db_session() as db:
        drug = _drug(db, "Ormessadine")
    lot = _lot(world["disp2"]["id"], drug, 5, 200)
    with db_session() as db:
        db.execute("UPDATE helix_inventory_lots SET facility_id = 1 WHERE id = ?", (lot["id"],))
        assert PF.lot_facility(db, lot["id"]) is None
    oid = _order(world["p2"], drug, world["doc2"])
    with pytest.raises(ValueError):
        helix_service.transition_order_status(oid, "dispensed", world["pharm2"])
    assert _qty(lot["id"]) == 5
    with db_session() as db:
        report = stock.reconcile(db, lot_ids=[lot["id"]])
    assert report["clean"] is False
    assert {m["kind"] for m in report["facility_mismatches"]} >= {"lot"}


def test_the_ward_stock_store_for_a_dose_is_at_the_patient_facility(world, wards):
    with db_session() as db:
        found, _ = stock._ward_store_for(db, F2_DEPT, None, world["ward_drug"], facility_id=F2)
        assert found is not None and found["id"] == wards["ward2"]["id"]
        none, reason = stock._ward_store_for(db, F2_DEPT, None, world["ward_drug"], facility_id=1)
        assert none is None and reason == "no_ward_store"
        assert PF.patient_facility(db, world["p2"]) == F2


# ═════════════════════════════════════════════════════════════════════════════
#  8. With one facility on record every helper is a no-op
# ═════════════════════════════════════════════════════════════════════════════


def test_with_one_facility_every_clause_is_empty_and_no_gate_reads(world, monkeypatch):
    _one_facility(monkeypatch)
    assert PF.scope({"id": world["pharm2"], "role": "pharmacist"}) is None
    assert PF.lot_sql("l", None) == "" and PF.column_sql("s.facility_id", None) == ""
    assert PF.store_ids_sql("store_id", None) == ""
    assert PF.scope_view(None) is None

    def no_session(*args, **kwargs):
        raise AssertionError("the gate read the database with one facility on record")

    monkeypatch.setattr(PF, "db_session", no_session)
    assert PF.document_outside(
        {"id": world["pharm2"], "role": "pharmacist"}, "purchase_order", 1
    ) is False
    PF.gate({"id": world["pharm2"], "role": "pharmacist"}, "store", 1, message="never")


def test_an_empty_scope_sees_nothing():
    assert PF.lot_sql("l", frozenset()) == PF.NEVER
    assert PF.column_sql("s.facility_id", frozenset()) == PF.NEVER
    assert PF.in_scope(F2, frozenset()) is False and PF.in_scope(None, None) is True


# ═════════════════════════════════════════════════════════════════════════════
#  9. The registry: the store lanes are swept
# ═════════════════════════════════════════════════════════════════════════════


def test_the_pharmacy_store_routes_reach_no_blind_sql_owner_but_the_incident_lane():
    from tests import facility_sql_sites as R

    g = R.graph()
    found = R.scan()
    rows = R.registry()
    problems = []
    checked = 0
    for r in R.app_routes(main.app, g):
        if r.node is None or R.surface_for(r.path, ("/api/pharmacy-store",)) is None:
            continue
        checked += 1
        for key in sorted(g.reach(r.node) & set(found)):
            _, row = R.row_for(rows, *key)
            if row is None or row.kind == R.BLIND:
                problems.append((r.path, key))
    assert checked > 80
    # The NDPS incident lane reaches the NABH ward list, a multisite-12 row
    # that W13-A sweeps in this same wave: before that sweep it is the one
    # blind reach, after it there is none. Nothing else may be blind.
    incident = (
        "/api/pharmacy-store/ndps/variances/{variance_id}/incident",
        ("services/nabh_service.py", "hospital_wards._work"),
    )
    assert set(problems) <= {incident}, problems
    pharmacy ={k for k, area in R.row_areas().items() if area == "pharmacy"}
    assert pharmacy and all(rows[k].kind in R.SAFE_KINDS for k in pharmacy)
