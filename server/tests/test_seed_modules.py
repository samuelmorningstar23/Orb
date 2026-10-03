"""scripts/seed_modules.py — the dev seed for the eight modules that ship empty.

WHAT THESE TESTS PROTECT, and why each one is not a formality:

  1. IT REFUSES ON A NON-DEV PROFILE, INCLUDING AN UNSET ONE. This script writes
     vendors, payers, claims, diet orders and an ABHA identity. On an
     installation holding real patients those rows would be indistinguishable
     from the hospital's own records within a day and there is no sweep that
     finds them again. config.py defaults an unset ORB_DEPLOYMENT_PROFILE to
     'prod', so the refusal has to hold for "prod" AND for "nobody said" — the
     second is the one a hurried operator actually hits.

  2. IT IS IDEMPOTENT. A demo box gets reseeded repeatedly. Several of the
     services underneath are deliberately NOT idempotent — a diet order
     SUPERSEDES rather than updates, a claim is always a new row, a purchase
     order is always a new document — so idempotency here is a property of this
     script's own guards, and a property that only a second run can prove.

  3. EVERY MODULE ACTUALLY HAS ROWS AFTERWARDS. The point of the seed is that
     the deeper flows (partial goods receipt, calibration overdue, roster
     conflict, kitchen indent, consent revocation) become REACHABLE. A seed that
     runs clean and leaves a module at zero has done nothing.

EVERY CASE RUNS IN A SUBPROCESS AGAINST ITS OWN DATABASE. Not fastidiousness:
the suite shares one temp DB for the whole session, and seeding twelve
housekeeping units per ward plus a payer and an equipment register into it would
change what test_command_center, test_billing and test_housekeeping see. The
subprocess also exercises the script the way an operator runs it — profile in the
environment, guards armed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Two wards, two admitted patients each. The seed attaches to whoever is on a
# ward, so this is the minimum that makes the patient-linked modules (payer
# policy, diet orders, ABDM identity, housekeeping register) reachable.
_FIXTURE = """
import json, sys
import database
database.init_db()
from database import db_session

with db_session() as db:
    for uid, name, role, dept in (
        ("ADM001", "Rajesh Kumar", "admin", None),
        ("DOC001", "Dr. Meera Sharma", "doctor", "General Medicine"),
        ("DOC004", "Dr. Vikram Rathore", "doctor", "General Surgery"),
        ("NUR001", "Priya Reddy", "nurse", "General Medicine"),
    ):
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, department, password_hash, created_at) "
            "VALUES (?,?,?,?, 'x', CURRENT_TIMESTAMP)", (uid, name, role, dept))
    for pid, name, ward, bed, dx in (
        ("PAT-SEEDT1", "Ananya Kapoor", "GM-1", "GM-101", "Diabetic ketoacidosis"),
        ("PAT-SEEDT2", "Rajesh Iyer", "GM-1", "GM-102", "COPD exacerbation"),
        ("PAT-SEEDT3", "Vikram Singh", "CCU", "CCU-01", "Acute STEMI"),
        ("PAT-SEEDT4", "Kavitha Menon", "CCU", "CCU-02", "Post-operative, general surgery"),
    ):
        db.execute(
            "INSERT OR REPLACE INTO patients (id, name, mrn, age, sex, ward, bed, "
            "attending_doctor_id, diagnosis, admission_date, status, created_at) "
            "VALUES (?,?,?,55,'Male',?,?, 'DOC001', ?, CURRENT_TIMESTAMP, 'admitted', CURRENT_TIMESTAMP)",
            (pid, name, "MRN-" + pid, ward, bed, dx))
        db.execute(
            "INSERT OR REPLACE INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
            "attending_doctor_id, status, created_at) "
            "VALUES (?,?, 'DOC001', 'General Medicine', ?, ?, 'DOC001', 'active', CURRENT_TIMESTAMP)",
            ("ADM-" + pid, pid, ward, bed))
"""

_RUN_TWICE = _FIXTURE + """
from scripts.seed_modules import seed_modules, module_row_counts

seed_modules(verbose=False)
first = module_row_counts()
seed_modules(verbose=False)
second = module_row_counts()
print("RESULT" + json.dumps({"first": first, "second": second}))
"""

_REFUSAL = _FIXTURE + """
from scripts.seed_modules import SeedRefused, seed_modules, module_row_counts

try:
    seed_modules(verbose=False)
except SeedRefused as exc:
    print("RESULT" + json.dumps({"refused": True, "message": str(exc),
                                 "counts": module_row_counts()}))
else:
    print("RESULT" + json.dumps({"refused": False, "counts": module_row_counts()}))
"""


def _run(script: str, db_path, profile: str | None) -> dict:
    """Run `script` in its own interpreter against its own database."""
    env = {k: v for k, v in os.environ.items() if k != "ORB_DEPLOYMENT_PROFILE"}
    env["ORB_DB_PATH"] = str(db_path)
    env["PYTHONPATH"] = SERVER_DIR
    if profile is not None:
        env["ORB_DEPLOYMENT_PROFILE"] = profile
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        env=env, cwd=SERVER_DIR, timeout=300,
    )
    marker = "RESULT"
    for line in proc.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise AssertionError(
        f"seed subprocess produced no result\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


@pytest.fixture(scope="module")
def twice(tmp_path_factory) -> dict:
    """One dev-profile box, seeded twice. Shared by the assertions below so the
    (slow) migration + double seed runs once."""
    db = tmp_path_factory.mktemp("seed_modules") / "orb.db"
    return _run(_RUN_TWICE, db, "dev")


# ── 1 · The profile gate ─────────────────────────────────────────────────────

@pytest.mark.parametrize("profile", ["prod", None], ids=["prod", "unset"])
def test_refuses_unless_the_profile_is_explicitly_dev(profile, tmp_path):
    """A prod profile — declared, or merely not declared — gets nothing.

    The 'unset' case is the important one. config.py resolves a missing
    ORB_DEPLOYMENT_PROFILE to 'prod' precisely so that forgetting to declare a
    box cannot unlock dev behaviour, and this seed has to inherit that default
    rather than treating "no answer" as permission.
    """
    result = _run(_REFUSAL, tmp_path / "orb.db", profile)

    assert result["refused"] is True, (
        f"seed_modules ran under profile={profile!r} — demo vendors, payers and "
        "claims must never be written to a box that has not declared itself dev"
    )
    assert "dev" in result["message"]

    # And it refused BEFORE writing anything, not halfway through. Checked on
    # tables that ONLY this seed populates — service_items, payers, stores and
    # the document series are created by migration 20 and by the front desk at
    # boot, so a non-zero count there proves nothing either way.
    assert result["counts"]["procurement"]["vendors"] == 0
    assert result["counts"]["biomedical"]["biomedical_assets"] == 0
    assert result["counts"]["housekeeping"]["housekeeping_units"] == 0
    assert result["counts"]["dietary"]["diet_orders"] == 0
    assert result["counts"]["abdm"]["abha_identities"] == 0


# ── 2 · Idempotency ──────────────────────────────────────────────────────────

def test_seeding_twice_changes_nothing(twice):
    """Run it, run it again, same rows. Not "roughly the same" — identical.

    This is where a seed rots first: a helper that INSERTs unconditionally looks
    fine on a clean box and quietly triples the ward's diet orders on the third
    demo. Compared table by table so the failure message names the module that
    grew rather than just saying the totals differ.
    """
    first, second = twice["first"], twice["second"]
    assert set(first) == set(second)
    for module in first:
        assert first[module] == second[module], (
            f"{module} is not idempotent: run 1 {first[module]}, run 2 {second[module]}"
        )


# ── 3 · The modules are actually populated ───────────────────────────────────

def test_every_module_reports_rows_after_seeding(twice):
    """The whole point. Eight modules held ZERO rows, so their deeper flows were
    unreachable rather than untested; each table below is one that was empty."""
    empty = {
        f"{module}.{table}"
        for module, counts in twice["first"].items()
        for table, n in counts.items()
        if n == 0
    }
    assert not empty, f"still empty after seeding: {sorted(empty)}"


def test_the_flows_verification_could_not_reach_now_have_data(twice):
    """Named, because "non-zero" is not the same as "the interesting case exists".

    A purchase order with a full receipt does not exercise partial receipt; an
    equipment register where everything is in date never renders the overdue
    state. These are the specific rows the dry run went looking for and did not
    find.
    """
    counts = twice["first"]
    assert counts["procurement"]["goods_receipts"] >= 1, "no partial goods receipt"
    assert counts["dietary"]["kitchen_indents"] >= 1, "no kitchen indent"
    assert counts["abdm"]["abdm_consent_artefacts"] >= 1, "no consent to revoke"
    assert counts["payer"]["claims"] >= 1, "no claim in flight"
    assert counts["workforce"]["duty_roster"] >= 1, "no roster to find conflicts in"


# ── 4 · The specific shapes the demo needs ───────────────────────────────────

_SHAPES = _FIXTURE + """
from datetime import date
import json
from scripts.seed_modules import seed_modules, SEED_MARKER
from database import db_session

seed_modules(verbose=False)

from services import biomedical_service as bio
from services import dietary_service as diet
from services import procurement_service as proc
from services import housekeeping_service as hk
from services.forecast_service import DEFAULT_WARD_BEDS

register = bio.due_register()
npo = []
with db_session() as db:
    npo = [dict(r) for r in db.execute(
        "SELECT patient_id, npo_reason, npo_until FROM diet_orders "
        "WHERE diet_type = 'npo' AND status = 'active'").fetchall()]
    unmarked_vendors = db.execute(
        "SELECT COUNT(*) AS n FROM vendors WHERE COALESCE(notes,'') NOT LIKE ?",
        ("%" + SEED_MARKER + "%",)).fetchone()["n"]
    diet_text = [r["notes"] or "" for r in db.execute(
        "SELECT notes FROM diet_orders").fetchall()] + [
        r["npo_reason"] or "" for r in db.execute(
            "SELECT npo_reason FROM diet_orders").fetchall()]
    po = db.execute(
        "SELECT status FROM purchase_orders ORDER BY id DESC LIMIT 1").fetchone()
    outstanding = db.execute(
        "SELECT COUNT(*) AS n FROM purchase_order_lines "
        "WHERE received_quantity_milli < quantity_milli").fetchone()["n"]
    claims = [dict(r) for r in db.execute(
        "SELECT c.status, c.claimed_paise, c.invoice_id, i.status AS invoice_status, "
        "i.invoice_type, i.payer_responsibility_paise, "
        "(SELECT COALESCE(SUM(total_paise), 0) FROM encounter_charges ch "
        " WHERE ch.invoice_id = i.id) AS charged_paise, i.payable_paise "
        "FROM claims c LEFT JOIN invoices i ON i.id = c.invoice_id").fetchall()]
    deposits = [dict(r) for r in db.execute(
        "SELECT patient_id, purpose, amount_paise FROM payments "
        "WHERE purpose = 'deposit'").fetchall()]
    discounts = [dict(r) for r in db.execute(
        "SELECT status, invoice_id, requested_paise FROM discount_approvals").fetchall()]

from services import payer_service
denials = payer_service.denial_log()

units_by_ward = {}
for u in hk.list_units():
    units_by_ward[u["ward"]] = units_by_ward.get(u["ward"], 0) + 1

items = proc.list_inventory_items()
print("RESULT" + json.dumps({
    "overdue_calibrations": register["overdue_calibration_count"],
    "npo_orders": npo,
    "diet_text": diet_text,
    "seed_marker": SEED_MARKER,
    "unmarked_vendors": unmarked_vendors,
    "po_status": po["status"] if po else None,
    "outstanding_po_lines": outstanding,
    "units_by_ward": units_by_ward,
    "ward_beds_setting": DEFAULT_WARD_BEDS,
    "items_with_reorder_point": len([i for i in items if i.get("reorder_level_milli")]),
    "claims": claims,
    "deposits": deposits,
    "discounts": discounts,
    "denials": denials,
}))
"""


@pytest.fixture(scope="module")
def shapes(tmp_path_factory) -> dict:
    db = tmp_path_factory.mktemp("seed_shapes") / "orb.db"
    return _run(_SHAPES, db, "dev")


def test_one_calibration_is_overdue(shapes):
    """An equipment register whose compliance screen is entirely green teaches
    nobody to read it, and cannot demonstrate the state the module exists for."""
    assert shapes["overdue_calibrations"] >= 1


def test_exactly_one_patient_is_nil_by_mouth_and_it_is_reviewable(shapes):
    """NPO is the diet order with teeth: it must carry a reason and an end time
    (dietary_service refuses otherwise), and it must be ONE patient — an NPO on
    the whole ward is obviously fiction and demonstrates nothing."""
    npo = shapes["npo_orders"]
    assert len(npo) == 1, f"expected exactly one NPO order, got {len(npo)}"
    assert npo[0]["npo_reason"], "an NPO nobody can review is an NPO nobody lifts"
    assert npo[0]["npo_until"], "nil by mouth with no end time"


def test_the_purchase_order_is_only_partially_received(shapes):
    """Partial receipt is the flow verification called unreachable. A fully
    received PO would leave it unreachable with more rows in the table."""
    assert shapes["po_status"] == "partially_received"
    assert shapes["outstanding_po_lines"] >= 1


def test_every_ward_has_a_full_bed_register(shapes):
    """ZERO housekeeping units in any ward is what silently broke the
    discharge → turnaround join. The register is sized from the SAME site
    setting the ward board and the capacity forecast divide by, so housekeeping's
    coverage report and the capacity screen cannot disagree about how many beds a
    ward has."""
    beds = shapes["ward_beds_setting"]
    assert shapes["units_by_ward"], "no housekeeping units registered in any ward"
    for ward, n in shapes["units_by_ward"].items():
        assert n == beds, f"{ward} has {n} units registered, site setting says {beds}"


def test_every_seeded_row_says_it_is_seed_data(shapes):
    """Nothing this script writes may be mistakable on a screen for something the
    hospital typed."""
    assert shapes["unmarked_vendors"] == 0


def test_the_seed_marker_is_plain_words(shapes):
    """Screens render the marker, so it names no file and says nothing about a
    demo: it is an honest label for sample data and nothing more."""
    marker = shapes["seed_marker"]
    assert marker == "Sample data"
    assert "demo" not in marker.lower() and "/" not in marker and ".py" not in marker


def test_diet_order_text_never_carries_the_seed_marker(shapes):
    """The order drawer pre-fills a diet order's notes and NPO reason into the
    clinician's next order, so a marker on the seeded order became text in an
    order the clinician wrote. Clinical free text carries no seed wording."""
    assert shapes["diet_text"], "no diet orders were seeded"
    for text in shapes["diet_text"]:
        assert "seed" not in text.lower(), text
        assert shapes["seed_marker"].lower() not in text.lower(), text
        assert "demo" not in text.lower(), text


def test_items_carry_reorder_points(shapes):
    """An inventory master with no reorder level cannot produce a reorder
    report — the module's whole purpose."""
    assert shapes["items_with_reorder_point"] >= 5


# ── 5 · A reseed leaves every module agreeing with itself ────────────────────
#
# reset_db_and_seed wipes every table with a patient_id, and stock_movements has
# one (nullable). The purchase orders and goods receipts its GRN rows posted from
# have none, so after every reseed the PO read "partially received" while the
# store held none of it, and the housekeeping register kept beds from earlier
# seeds (CCU 16 of 15) and weeks-old state. Runs the REAL wipe between two seeds.

_RESEED = _FIXTURE + """
from scripts.seed_modules import seed_modules
from scripts.reset_db_and_seed import wipe_patient_tables
from database import _build_connection

seed_modules(verbose=False)

# A bed left over from an earlier seed, and weeks in its state.
with db_session() as db:
    db.execute(
        "INSERT INTO housekeeping_units (id, unit_code, ward, bed_label, unit_type, state, "
        "state_since, is_active, notes, created_at, updated_at) "
        "VALUES ('HK-STALE', 'CCU-99', 'CCU', 'CCU-99', 'bed', 'vacant_dirty', "
        "'2026-09-01T00:00:00+00:00', 1, 'stale', '2026-09-01', '2026-09-01')")

conn = _build_connection()
wipe_patient_tables(conn)
conn.close()
""" + _FIXTURE.replace("import database\ndatabase.init_db()\n", "") + """
from scripts.seed_modules import seed_modules as reseed
reseed(verbose=False)

from services import housekeeping_service as hk
from services.forecast_service import DEFAULT_WARD_BEDS
with db_session() as db:
    pos = [dict(r) for r in db.execute(
        "SELECT id, status FROM purchase_orders").fetchall()]
    received = {r["item_code"]: r["q"] for r in db.execute(
        "SELECT item_code, SUM(received_quantity_milli) AS q FROM purchase_order_lines "
        "GROUP BY item_code").fetchall()}
    accepted = {r["item_code"]: r["q"] for r in db.execute(
        "SELECT item_code, SUM(accepted_quantity_milli) AS q FROM goods_receipt_lines "
        "GROUP BY item_code").fetchall()}
    stocked = {r["item_code"]: r["q"] for r in db.execute(
        "SELECT item_code, SUM(quantity_milli) AS q FROM stock_movements "
        "WHERE reference_type = 'grn' GROUP BY item_code").fetchall()}

units_by_ward = {}
for u in hk.list_units():
    units_by_ward[u["ward"]] = units_by_ward.get(u["ward"], 0) + 1
print("RESULT" + json.dumps({
    "purchase_orders": pos,
    "received": received,
    "accepted": accepted,
    "stocked": stocked,
    "units_by_ward": units_by_ward,
    "stale_unit_codes": [u["unit_code"] for u in hk.list_units() if u["unit_code"] == "CCU-99"],
    "ward_beds_setting": DEFAULT_WARD_BEDS,
}))
"""


@pytest.fixture(scope="module")
def reseeded(tmp_path_factory) -> dict:
    db = tmp_path_factory.mktemp("seed_reseed") / "orb.db"
    return _run(_RESEED, db, "dev")


def test_after_a_reseed_the_purchase_order_and_the_store_agree(reseeded):
    assert len(reseeded["purchase_orders"]) == 1, reseeded["purchase_orders"]
    assert reseeded["purchase_orders"][0]["status"] == "partially_received"
    received = {k: v for k, v in reseeded["received"].items() if v}
    assert received, "the reseeded PO has no receipt at all"
    assert received == reseeded["accepted"], "PO lines and goods receipt lines disagree"
    assert received == reseeded["stocked"], (
        "the PO says goods were received but the store's stock ledger does not hold them"
    )


def test_after_a_reseed_the_bed_register_is_rebuilt_not_appended(reseeded):
    beds = reseeded["ward_beds_setting"]
    assert reseeded["stale_unit_codes"] == [], "a bed from an earlier seed survived the reseed"
    for ward, n in reseeded["units_by_ward"].items():
        assert n == beds, f"{ward} has {n} units registered after a reseed, site setting says {beds}"


# ── 6 · A reseed cuts the masters back to the seed manifest ──────────────────
#
# The wipe reaches patient data only, so rows an ad hoc test or a manual
# verification run added to a master table survived every reseed: load-test
# doctors bookable at the front desk, a workflow-test portal login, hand-typed
# price items (a second CBC among them) and a no-op roster import run. The
# website fixtures were recorded off that database and copied all of it.

_SWEEP = _FIXTURE + """
from scripts.seed_modules import seed_modules
from scripts.reset_db_and_seed import (
    seeded_service_codes, seeded_user_ids, sweep_unseeded_masters, wipe_patient_tables,
)
from database import _build_connection

seed_modules(verbose=False)

with db_session() as db:
    for uid, role in (("U-A-TEST01", "doctor"), ("U-B-TEST01", "doctor"),
                      ("PWFTEST01", "patient"), ("dr_rathore", "doctor")):
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, 'General Medicine', 'x')", (uid, "Debris " + uid, role))
    for code in ("XR-CHEST", "CONS-OP"):
        db.execute(
            "INSERT INTO service_items (code, name, category, default_rate_paise, "
            "rate_is_configured) VALUES (?, ?, 'consultation', 80000, 1)", (code, code))
    item = db.execute("SELECT id FROM service_items WHERE code = 'CONS-OP'").fetchone()["id"]
    plan = db.execute("SELECT id FROM tariff_plans ORDER BY id LIMIT 1").fetchone()["id"]
    db.execute(
        "INSERT INTO tariff_rates (tariff_plan_id, service_item_id, service_code, rate_paise) "
        "VALUES (?, ?, 'CONS-OP', 80000)", (plan, item))
    db.execute(
        "INSERT INTO roster_import_runs (id, actor_user_id, source_filename, mapping_json) "
        "VALUES ('RI-TEST', 'ADM001', 'noop-verify.csv', '{}')")

conn = _build_connection()
wipe_patient_tables(conn)
removed = sweep_unseeded_masters(conn)
conn.close()

with db_session() as db:
    users = sorted(r["id"] for r in db.execute("SELECT id FROM users").fetchall())
    codes = sorted(r["code"] for r in db.execute("SELECT code FROM service_items").fetchall())
    rate_codes = sorted({r["service_code"] for r in db.execute(
        "SELECT service_code FROM tariff_rates").fetchall()})
    runs = db.execute("SELECT COUNT(*) AS n FROM roster_import_runs").fetchone()["n"]

print("RESULT" + json.dumps({
    "removed": removed,
    "users": users,
    "codes": codes,
    "rate_codes": rate_codes,
    "runs": runs,
    "keep_users": sorted(seeded_user_ids()),
    "keep_codes": sorted(seeded_service_codes()),
    "seed_staff": [[u[0], u[1]] for u in __import__(
        "scripts.seed_clinical_data", fromlist=["USERS"]).USERS if u[2] != "patient"],
}))
"""


def test_a_reseed_keeps_only_the_masters_the_seed_writes(tmp_path):
    result = _run(_SWEEP, tmp_path / "orb.db", "dev")

    stray_users = set(result["users"]) - set(result["keep_users"])
    assert not stray_users, f"logins outside the seed manifest survived: {sorted(stray_users)}"
    for uid in ("ADM001", "DOC001", "NUR001"):
        assert uid in result["users"], f"the sweep removed a seeded login: {uid}"

    stray_codes = set(result["codes"]) - set(result["keep_codes"])
    assert not stray_codes, f"price items outside the seed manifest survived: {sorted(stray_codes)}"
    assert set(result["rate_codes"]) <= set(result["keep_codes"]), result["rate_codes"]
    assert "SEED-INV-CBC" in result["codes"], "the sweep removed the seed's own price item"

    assert result["runs"] == 0, "a roster import run survived the reseed"

    # The duplicate surgeon login is gone from the manifest, so the sweep takes
    # it too: the front desk no longer offers "Dr. Vikram Rathore" twice.
    assert "dr_rathore" not in result["users"]
    names = [name for _, name in result["seed_staff"]]
    assert len(names) == len(set(names)), f"two seeded logins share a name: {sorted(names)}"


_SWEEP_PROD = _FIXTURE + """
from scripts.reset_db_and_seed import sweep_unseeded_masters
from database import _build_connection

with db_session() as db:
    db.execute(
        "INSERT INTO users (id, name, role, department, password_hash) "
        "VALUES ('U-A-TEST01', 'Debris', 'doctor', 'General Medicine', 'x')")

conn = _build_connection()
removed = sweep_unseeded_masters(conn)
conn.close()
with db_session() as db:
    kept = db.execute("SELECT COUNT(*) AS n FROM users WHERE id = 'U-A-TEST01'").fetchone()["n"]
print("RESULT" + json.dumps({"removed": removed, "kept": kept}))
"""


@pytest.mark.parametrize("profile", ["prod", None], ids=["prod", "unset"])
def test_the_master_sweep_refuses_unless_the_profile_is_dev(profile, tmp_path):
    """The sweep deletes staff logins. On a box that has not declared itself dev
    it must delete nothing, exactly as the module seed refuses there."""
    result = _run(_SWEEP_PROD, tmp_path / "orb.db", profile)
    assert result["removed"] == {}
    assert result["kept"] == 1


# ── 7 · The money desks agree with each other ────────────────────────────────
#
# The seed used to open a ₹48,000 claim against a patient with no charge, no
# invoice and no receipt, so Billing showed ₹0 while the payer desk chased the
# claim, and the cashier, discount and denial screens had nothing at all.

def test_every_seeded_claim_is_for_an_issued_bill_with_charges_behind_it(shapes):
    claims = shapes["claims"]
    assert len(claims) >= 2, claims
    for c in claims:
        assert c["invoice_id"], f"a claim with no bill behind it: {c}"
        assert c["invoice_status"] == "issued" and c["invoice_type"] == "interim", c
        assert c["charged_paise"] == c["payable_paise"] > 0, c
        assert c["claimed_paise"] == c["payer_responsibility_paise"], (
            f"the claim is not for the payer's share of its bill: {c}"
        )


def test_the_cashier_has_a_deposit_and_a_discount_awaiting_a_decision(shapes):
    assert shapes["deposits"], "no deposit receipt was seeded"
    assert all(d["amount_paise"] > 0 for d in shapes["deposits"])
    pending = [d for d in shapes["discounts"] if d["status"] == "requested"]
    assert pending and pending[0]["invoice_id"], shapes["discounts"]


def test_the_denial_log_has_a_denial_with_the_payers_reason(shapes):
    rows = shapes["denials"].get("rows") or shapes["denials"].get("denials") or []
    assert rows, f"the denial log is empty: {shapes['denials']}"
    assert any(r.get("denial_reason") for r in rows), rows

