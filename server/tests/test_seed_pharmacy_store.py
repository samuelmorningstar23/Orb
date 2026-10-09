"""The pharmacy store's dev seed (grn-18, W8-F).

Each scenario runs in its own interpreter against its own database, as
tests/test_seed_modules.py does, because the seed is a script that reads the
deployment profile and the module switch at call time.

Pinned:
  * with the module off (or not a dev box) the seed writes nothing, and
    reset_and_seed does not even call it, so a reseed without the module is
    what it was;
  * after the seed, reconcile() is clean over every lot, every lot is in the
    dispensing store, the goods receipt's lots exist and the order is received,
    one batch is near expiry, and no NDPS stock was seeded;
  * a second run adds nothing; a reseed (the real wipe, then seed_helix, then
    this seed) leaves no orphan pharmacy rows and one order;
  * with ward stock on, a ward store and its stock list are seeded, and no
    stock is sent to it;
  * reset_and_seed calls it after seed_helix and before seed_pharmacy_queue.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FIXTURE = """
import json
import database
database.init_db()
from database import db_session
from services.helix_db import init_helix_db
init_helix_db()

with db_session() as db:
    for uid, name, role, dept in (
        ("ADM001", "Rajesh Kumar", "admin", None),
        ("PHA001", "Deepa Joshi", "pharmacist", None),
        ("NUR001", "Priya Reddy", "nurse", "General Medicine"),
        ("DOC001", "Dr. Meera Sharma", "doctor", "General Medicine"),
    ):
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, department, password_hash, created_at) "
            "VALUES (?,?,?,?, 'x', CURRENT_TIMESTAMP)", (uid, name, role, dept))

from scripts.seed_helix import seed_helix
from scripts.seed_pharmacy_store import seed_pharmacy_store

def state():
    from services import pharmacy_stock_service as stock
    with db_session() as db:
        rec = stock.reconcile(db)
        lots = [dict(r) for r in db.execute(
            "SELECT l.id, l.medication_id, l.store_id, l.status, l.quantity, l.expiration_date, "
            "s.pharmacy_kind FROM helix_inventory_lots l LEFT JOIN stores s ON s.id = l.store_id"
        ).fetchall()]
        grn_lots = [r[0] for r in db.execute(
            "SELECT helix_lot_ref FROM goods_receipt_lines").fetchall()]
        missing = [ref for ref in grn_lots if ref and db.execute(
            "SELECT 1 FROM helix_inventory_lots WHERE id = ?", (ref,)).fetchone() is None]
        orphan_lines = db.execute(
            "SELECT COUNT(*) FROM goods_receipt_lines WHERE goods_receipt_id NOT IN "
            "(SELECT id FROM goods_receipts)").fetchone()[0]
        orphan_po_lines = db.execute(
            "SELECT COUNT(*) FROM purchase_order_lines WHERE purchase_order_id NOT IN "
            "(SELECT id FROM purchase_orders)").fetchone()[0]
        moves_without_lot = db.execute(
            "SELECT COUNT(*) FROM helix_stock_movements WHERE lot_id NOT IN "
            "(SELECT id FROM helix_inventory_lots)").fetchone()[0]
        pos = [dict(r) for r in db.execute(
            "SELECT id, status, domain FROM purchase_orders WHERE domain = 'pharmacy'").fetchall()]
        grns = [dict(r) for r in db.execute(
            "SELECT id, status FROM goods_receipts WHERE domain = 'pharmacy'").fetchall()]
        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            "pharmacy_transfers", "pharmacy_par_levels", "vendors")}
        stores = [dict(r) for r in db.execute(
            "SELECT code, pharmacy_kind, department FROM stores "
            "WHERE pharmacy_kind IS NOT NULL").fetchall()]
        ndps = db.execute(
            "SELECT COUNT(*) FROM helix_inventory_lots l JOIN helix_medication_master m "
            "ON m.id = l.medication_id WHERE m.ndps_category IN ('narcotic', 'psychotropic') "
            "AND l.quantity > 0").fetchone()[0]
    return {"clean": rec["clean"], "lots": lots, "grn_lots": grn_lots, "missing": missing,
            "orphan_lines": orphan_lines, "orphan_po_lines": orphan_po_lines,
            "moves_without_lot": moves_without_lot, "pos": pos, "grns": grns,
            "counts": counts, "stores": stores, "ndps": ndps}
"""

_SEED_TWICE_AND_RESEED = _FIXTURE + """
seed_helix()
first_result = seed_pharmacy_store(verbose=False)
first = state()
again = seed_pharmacy_store(verbose=False)
second = state()

from scripts.reset_db_and_seed import wipe_patient_tables
from database import _build_connection
conn = _build_connection()
wipe_patient_tables(conn)
conn.close()
seed_helix()
seed_pharmacy_store(verbose=False)
third = state()
print("RESULT" + json.dumps({"first_result": first_result, "first": first,
                             "again": again, "second": second, "third": third}, default=str))
"""

_OFF = _FIXTURE + """
seed_helix()
result = seed_pharmacy_store(verbose=False)
print("RESULT" + json.dumps({"result": result, "state": state()}, default=str))
"""


def _run(script: str, db_path, env_extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("ORB_DEPLOYMENT_PROFILE", "ORB_PHARMACY_STORE",
                        "ORB_PHARMACY_WARD_STOCK", "ORB_DB_PATH")}
    env["ORB_DB_PATH"] = str(db_path)
    env["PYTHONPATH"] = SERVER_DIR
    env.update(env_extra)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=env, cwd=SERVER_DIR, timeout=600)
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT"):
            return json.loads(line[len("RESULT"):])
    raise AssertionError(f"no result\nSTDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-6000:]}")


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> dict:
    db = tmp_path_factory.mktemp("seed_pharmacy_store") / "orb.db"
    return _run(_SEED_TWICE_AND_RESEED, db,
                {"ORB_DEPLOYMENT_PROFILE": "dev", "ORB_PHARMACY_STORE": "true"})


def test_the_seed_runs_and_reconcile_is_clean(seeded):
    first = seeded["first"]
    assert "skipped" not in seeded["first_result"], seeded["first_result"]
    assert first["clean"], "reconcile() is not clean after the seed"
    assert first["moves_without_lot"] == 0


def test_every_lot_is_in_the_dispensing_store(seeded):
    lots = seeded["first"]["lots"]
    assert lots
    assert {lot["pharmacy_kind"] for lot in lots} == {"dispensing"}, lots


def test_one_order_is_received_on_one_posted_receipt_and_its_lots_exist(seeded):
    first = seeded["first"]
    assert [p["status"] for p in first["pos"]] == ["received"]
    assert [g["status"] for g in first["grns"]] == ["posted"]
    assert first["grn_lots"] and all(first["grn_lots"]) and first["missing"] == []


def test_one_seeded_batch_is_near_expiry(seeded):
    from datetime import date, timedelta
    lots = seeded["first"]["lots"]
    grn = set(seeded["first"]["grn_lots"])
    soon = (date.today() + timedelta(days=90)).isoformat()
    near = [lot for lot in lots if lot["id"] in grn and str(lot["expiration_date"])[:10] <= soon]
    assert len(near) == 1, lots


def test_no_ndps_stock_is_seeded(seeded):
    assert seeded["first"]["ndps"] == 0


def test_a_second_run_adds_nothing(seeded):
    first, second = seeded["first"], seeded["second"]
    assert second["pos"] == first["pos"] and second["grns"] == first["grns"]
    assert second["counts"] == first["counts"]
    assert len(second["stores"]) == len(first["stores"])
    assert seeded["again"]["purchase_order_id"] == seeded["first_result"]["purchase_order_id"]


def test_a_reseed_leaves_no_orphan_pharmacy_rows(seeded):
    third = seeded["third"]
    assert third["clean"]
    assert third["missing"] == [] and third["orphan_lines"] == 0
    assert third["orphan_po_lines"] == 0 and third["moves_without_lot"] == 0
    assert [p["status"] for p in third["pos"]] == ["received"], "one order, not two"
    assert {lot["pharmacy_kind"] for lot in third["lots"]} == {"dispensing"}
    # The masters are kept: one dispensing store and one seeded vendor.
    assert [s["code"] for s in third["stores"] if s["pharmacy_kind"] == "dispensing"] == [
        "SEED-PHDISP"]
    assert third["counts"]["pharmacy_transfers"] == 0


def test_no_ward_stock_store_without_ward_stock(seeded):
    assert not [s for s in seeded["first"]["stores"] if s["pharmacy_kind"] == "ward_imprest"]
    assert seeded["first"]["counts"]["pharmacy_par_levels"] == 0


@pytest.mark.parametrize("env", [
    {"ORB_DEPLOYMENT_PROFILE": "dev"},
    {"ORB_DEPLOYMENT_PROFILE": "prod", "ORB_PHARMACY_STORE": "true",
     "ORB_JWT_SECRET": "suite-only-secret-0123456789abcdef0123456789"},
], ids=["module-off", "not-dev"])
def test_the_seed_writes_nothing_unless_dev_with_the_module_on(tmp_path, env):
    out = _run(_OFF, tmp_path / "orb.db", env)
    assert "skipped" in out["result"]
    state = out["state"]
    assert state["stores"] == [] and state["pos"] == [] and state["grns"] == []
    assert {lot["store_id"] for lot in state["lots"]} == {None}, "lots stay as seed_helix left them"


def test_with_ward_stock_on_a_ward_store_and_its_list_are_seeded(tmp_path):
    out = _run(_OFF, tmp_path / "orb.db", {"ORB_DEPLOYMENT_PROFILE": "dev",
                                           "ORB_PHARMACY_STORE": "true",
                                           "ORB_PHARMACY_WARD_STOCK": "true"})
    state = out["state"]
    ward = [s for s in state["stores"] if s["pharmacy_kind"] == "ward_imprest"]
    assert ward == [{"code": "SEED-PHWARD1", "pharmacy_kind": "ward_imprest",
                     "department": "General Medicine"}]
    assert state["counts"]["pharmacy_par_levels"] == 2
    assert state["counts"]["pharmacy_transfers"] == 0, "no stock is sent to the ward"
    assert {lot["pharmacy_kind"] for lot in state["lots"]} == {"dispensing"}


def test_reset_and_seed_calls_it_between_seed_helix_and_the_pharmacy_queue():
    source = Path(SERVER_DIR, "scripts", "reset_db_and_seed.py").read_text()
    fn = next(node for node in ast.walk(ast.parse(source))
              if isinstance(node, ast.FunctionDef) and node.name == "reset_and_seed")
    calls = [n.func.id for n in sorted(
        (n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)),
        key=lambda n: (n.lineno, n.col_offset))]
    order = [c for c in calls if c in ("seed_helix", "seed_pharmacy_store", "should_seed",
                                        "seed_pharmacy_queue")]
    assert order.index("seed_helix") < order.index("seed_pharmacy_store") \
        < order.index("seed_pharmacy_queue")
    # Gated: the call sits under `if should_seed():`.
    gated = [n for n in ast.walk(fn) if isinstance(n, ast.If)
             and isinstance(n.test, ast.Call) and getattr(n.test.func, "id", "") == "should_seed"]
    assert gated and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") ==
                         "seed_pharmacy_store" for g in gated for c in ast.walk(g))


# The operator docs this increment hand-edited, and the code it wrote, carry
# no em dash (Samuel's standing rule, 3 September 2026).
_NO_EM_DASH = (
    "docs/MODULE_MANUAL.md",
    "docs/manual_sections/08_helix_lens_pulse_bridge.md",
    "docs/manual_sections/16_hospital_admin_modules.md",
    "server/scripts/seed_pharmacy_store.py",
    "server/routes/pharmacy_store/ward.py",
    "src/api/pharmacyStore/ward.ts",
    "src/pages/panels/pharmacyStore/WardStockView.tsx",
    "src/pages/panels/nurse/WardStockSection.tsx",
)


@pytest.mark.parametrize("relative", _NO_EM_DASH)
def test_the_changed_docs_and_new_files_carry_no_em_dash(relative):
    path = Path(SERVER_DIR).parent / relative
    assert "—" not in path.read_text(encoding="utf-8"), relative
