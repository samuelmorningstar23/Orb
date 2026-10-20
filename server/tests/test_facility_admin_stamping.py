"""Money, stores and assets carry the facility (multisite-7b, W11-C, migration 75).

  1. The migration: facility_id INTEGER NOT NULL DEFAULT 1 on the twenty
     administrative tables, a nullable facility_id (NULL is the group) on
     tariff plans, forecasts and holidays, and the one-active-dispensing-store
     index moved from the site to the facility. On a fresh boot (the lazy Helix
     and housekeeping paths included), on a legacy database, and run twice.
  2. One facility on record (every install today): every writer stamps 1 and
     the resolver reads nothing, so single-site behaviour is unchanged.
  3. Two facilities on record: each writer stamps the facility its own sources
     name (the admission, the counter, the bill, the store, the department,
     the ward), numbers from that facility's series and never borrows facility
     1's, and refuses in words before writing anything when its sources sit at
     different facilities or name none.

Every second facility here is removed in teardown with the cache invalidated
(the TEST RULE in docs/his_design/build/contracts.md), with its departments,
home rows, wards and series; its accounts are deactivated.
"""
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import migrations  # noqa: F401  (import before the migration file, see migrations_his)
from auth import hash_password
from database import db_session
from db_backend import DBIntegrityError
from migrations_his import m0075_facility_columns_admin as M75
from services import abdm_service as ab
from services import billing_masters as BM
from services import billing_service
from services import biomedical_service as bio
from services import dietary_service as ds
from services import facility_service as FS
from services import facility_stamp
from services import housekeeping_service as hk
from services import nabh_service as nabh
from services import payer_service
from services import payments_service as ps
from services import procurement_service as proc
from services.helix_db import HELIX_SCHEMA
from services.money import rupees

SERVER_DIR = Path(__file__).resolve().parent.parent
F2 = 9751


# ── helpers ──────────────────────────────────────────────────────────────────

def _hex(n: int = 6) -> str:
    return uuid.uuid4().hex[:n].upper()


def _facility_of(table: str, row_id) -> int:
    with db_session() as db:
        row = db.execute(f"SELECT facility_id FROM {table} WHERE id = ?", (row_id,)).fetchone()
    assert row is not None, f"no {table} row {row_id}"
    return int(row[0])


def _user(*, role: str = "admin", department=None, home=None) -> str:
    uid = f"FAS{_hex(8)}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"Ysolde Marrick {uid[-4:]}", role, department, hash_password("pw")))
        if home is not None:
            db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                       "granted_at) VALUES (?, ?, 'home', 'test', CURRENT_TIMESTAMP)",
                       (uid, home))
    FS.invalidate_cache()
    return uid


def _patient(*, facility: int = 1, department=None, ward: str = "FAS-W1") -> tuple:
    """A patient registered at `facility`; with `department`, admitted there
    too. Returns (patient_id, admission_id or None)."""
    pid = f"PFAS{_hex(8)}"
    aid = None
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, ward, bed, status, "
                   "registered_facility_id) VALUES (?, ?, ?, ?, '1', ?, ?)",
                   (pid, "Quirine Halvorsen", f"MRN-{pid}", ward,
                    "admitted" if department else "discharged", facility))
        if department:
            aid = f"AFAS{_hex(8)}"
            db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, "
                       "bed, status, facility_id) VALUES (?, ?, 'D001', ?, ?, '1', 'active', ?)",
                       (aid, pid, department, ward, facility))
    return pid, aid


def _priced_charge(pid: str, aid=None) -> dict:
    plan = int(billing_service.upsert_tariff_plan(
        code=f"TP{_hex()}", name="Stamping plan")["id"])
    code = f"TST-FAS{_hex()}"
    BM.upsert_service_item(code=code, name="Consultation", category="consultation",
                           gst_treatment="exempt", gst_rate_bp=0)
    BM.set_tariff_rate(tariff_plan_id=plan, service_code=code, rate_paise=rupees("900"))
    return billing_service.add_charge(patient_id=pid, admission_id=aid, service_code=code,
                                      tariff_plan_id=plan)


def _series(fid: int, code: str, document_type: str, prefix: str) -> None:
    with db_session() as db:
        db.execute("INSERT INTO document_number_series (facility_id, series_code, "
                   "document_type, prefix, next_number) VALUES (?, ?, ?, ?, 1)",
                   (fid, code, document_type, prefix))


def _next(code: str, fid: int = 1):
    with db_session() as db:
        row = db.execute("SELECT next_number, financial_year FROM document_number_series "
                         "WHERE series_code = ? AND facility_id = ?", (code, fid)).fetchone()
    return None if row is None else tuple(row)


def _ward(department: str) -> str:
    """A ward in Bed setup for `department`; returns its code."""
    code = f"FW{_hex(5)}"
    with db_session() as db:
        db.execute("INSERT INTO wards (id, code, name, department, created_at) "
                   "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                   (f"WFAS{_hex(8)}", code, f"Ward {code}", department))
    return code


def _vendor() -> dict:
    return proc.upsert_vendor(code=f"FASV{_hex()}", name="Thornquist Surgical Supplies",
                              state_code="27", gstin="27AAAAA0000A1Z5")


def _item() -> dict:
    return proc.upsert_inventory_item(code=f"FASI{_hex()}", name="Sterile glove size 7",
                                      category="consumable", uom="box",
                                      gst_treatment="taxable", gst_rate_bp=1200)


def _issued_po(vendor: dict, store: dict, item: dict) -> dict:
    po = proc.create_purchase_order(
        vendor_code=vendor["code"], store_code=store["code"],
        lines=[{"item_code": item["code"], "quantity_milli": 5000, "unit_rate_paise": 1000}],
        created_by="buyer")
    proc.submit_purchase_order(po["id"])
    proc.approve_purchase_order(po["id"], actor="approver")
    return proc.issue_purchase_order(po["id"], actor="approver")


def _verified_identity(pid: str) -> dict:
    number = "7" + str(uuid.uuid4().int)[:13]
    identity = ab.record_abha(patient_id=pid, abha_number=number, recorded_by="FAS-ADM")
    return ab.record_verification(identity_id=identity["id"], method="aadhaar_otp",
                                  outcome="verified", performed_by="FAS-ADM",
                                  performed_where="abdm_portal", reference="TXN-FAS")


def _locked_plan_on(ward_code: str, department: str, facility: int) -> dict:
    pid, _ = _patient(facility=facility, department=department, ward=ward_code)
    ds.order_diet(patient_id=pid, diet_type="diabetic", ordered_by="D001")
    plan = ds.build_meal_plan(ward=ward_code, meal_service="lunch", generated_by="D001")
    return ds.lock_meal_plan(plan["id"], locked_by="D001")


@pytest.fixture()
def second():
    """Facility 9751 with one department of its own, a department-less
    account placed there and one at facility 1, and facility 1 a department
    of its own. Removed in teardown."""
    code = f"AS{_hex(4)}"
    dept2, dept1 = f"Vellmorn Annexe {_hex(4)}", f"Caldrey Main {_hex(4)}"
    with db_session() as db:
        db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), code, "Vellmorn Annexe"))
        db.execute("INSERT INTO departments (facility_id, name, created_by) VALUES (?, ?, 'test')",
                   (F2, dept2))
        db.execute("INSERT INTO departments (facility_id, name, created_by) VALUES (1, ?, 'test')",
                   (dept1,))
    FS.invalidate_cache()
    out = {"id": F2, "code": code, "dept2": dept2, "dept1": dept1}
    out["cashier2"] = _user(home=F2)
    out["cashier1"] = _user()
    yield out
    with db_session() as db:
        # Rows this test placed at the second facility go back to facility 1,
        # so no later test (or the operations page) meets a record naming a
        # facility that is no longer on record.
        db.execute("UPDATE admissions SET facility_id = 1 WHERE facility_id = ?", (F2,))
        db.execute("UPDATE patients SET registered_facility_id = 1 "
                   "WHERE registered_facility_id = ?", (F2,))
        for table, column, _declaration in M75.FACILITY_COLUMNS:
            db.execute(f"UPDATE {table} SET {column} = 1 WHERE {column} = ?", (F2,))
        db.execute("DELETE FROM document_number_series WHERE facility_id = ?", (F2,))
        db.execute("DELETE FROM wards WHERE department IN (?, ?)", (dept2, dept1))
        db.execute("DELETE FROM departments WHERE name IN (?, ?)", (dept2, dept1))
        db.execute("DELETE FROM user_facilities WHERE facility_id = ?", (F2,))
        db.execute("UPDATE users SET is_active = 0 WHERE id IN (?, ?)",
                   (out["cashier1"], out["cashier2"]))
        db.execute("UPDATE cash_counter_sessions SET status = 'closed' WHERE status = 'open' "
                   "AND counter_code LIKE 'FAS%'")
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()


def _counter(user_id: str) -> dict:
    return ps.open_counter_session(counter_code=f"FAS{_hex(5)}", opened_by=user_id)


# ═════════════════════════════════════════════════════════════════════════════
#  1. The migration
# ═════════════════════════════════════════════════════════════════════════════

def test_migration_75_is_ledgered_and_every_column_is_in_place():
    with db_session() as db:
        assert db.execute("SELECT 1 FROM schema_migrations WHERE version = 75").fetchone()
        for table, column, declaration in M75.FACILITY_COLUMNS:
            info = {r[1]: r for r in db.execute(f"PRAGMA table_info({table})")}
            assert column in info, table
            notnull, default = info[column][3], info[column][4]
            if table in M75.GROUP_TABLES:
                assert (notnull, default) == (0, None), table
            else:
                assert (notnull, default) == (1, "1"), table
                assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE facility_id IS NULL"
                                  ).fetchone()[0] == 0
    assert set(M75.HELIX_TABLES) <= set(M75.FACILITY_TABLES)
    assert len(M75.FACILITY_TABLES) == 20 and len(M75.GROUP_TABLES) == 3


def test_one_active_dispensing_store_is_now_one_per_facility():
    with db_session() as db:
        cols = [r[2] for r in db.execute(f"PRAGMA index_info({M75.DISPENSING_INDEX})")]
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name = ?",
                         (M75.DISPENSING_INDEX,)).fetchone()[0]
    assert cols == ["facility_id", "pharmacy_kind"]
    assert M75.DISPENSING_PREDICATE in sql


def test_the_migration_on_a_legacy_database_adds_only_what_is_missing_and_runs_twice(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, invoice_number TEXT)")
    conn.execute("INSERT INTO invoices (invoice_number) VALUES ('INV-1')")
    conn.execute("CREATE TABLE stores (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT)")
    conn.execute("INSERT INTO stores (code, name) VALUES ('MAIN', 'Main store')")
    conn.execute("CREATE TABLE tariff_plans (id INTEGER PRIMARY KEY, code TEXT)")
    conn.execute("INSERT INTO tariff_plans (code) VALUES ('GEN')")
    conn.commit()
    M75.apply(conn)
    conn.commit()
    schema = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
    M75.apply(conn)
    conn.commit()
    assert sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()) == schema
    assert conn.execute("SELECT facility_id FROM invoices").fetchall() == [(1,)]
    assert conn.execute("SELECT facility_id FROM stores").fetchall() == [(1,)]
    assert conn.execute("SELECT facility_id FROM tariff_plans").fetchall() == [(None,)]
    # stores lacks pharmacy_kind and is_active, so the index is left alone.
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = ?",
                        (M75.DISPENSING_INDEX,)).fetchone()[0] == 0
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_index_moves_on_a_migration_54_database_and_refuses_as_before(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "m54.db"))
    conn.execute("CREATE TABLE stores (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, "
                 "is_active INTEGER NOT NULL DEFAULT 1, pharmacy_kind TEXT)")
    conn.execute("CREATE UNIQUE INDEX ux_stores_one_active_dispensing ON stores (pharmacy_kind) "
                 "WHERE pharmacy_kind = 'dispensing' AND is_active = 1")
    conn.execute("INSERT INTO stores (code, name, pharmacy_kind) VALUES ('RX', 'Rx', 'dispensing')")
    conn.commit()
    M75.apply(conn)
    conn.commit()
    assert M75.one_dispensing_store_per_facility(conn) is False
    with pytest.raises(DBIntegrityError, match="pharmacy_kind"):
        conn.execute("INSERT INTO stores (code, name, pharmacy_kind) VALUES ('RX2', 'Rx two', "
                     "'dispensing')")
    conn.execute("INSERT INTO stores (code, name, pharmacy_kind, facility_id) VALUES "
                 "('RX3', 'Rx three', 'dispensing', 2)")
    assert conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 2


def test_the_lazy_schema_owners_carry_the_column(tmp_path):
    ref = sqlite3.connect(str(tmp_path / "helix.db"))
    ref.executescript(HELIX_SCHEMA)
    for table in M75.HELIX_TABLES:
        info = {r[1]: r for r in ref.execute(f"PRAGMA table_info({table})")}
        assert (info["facility_id"][2], info["facility_id"][3], info["facility_id"][4]) \
            == ("INTEGER", 1, "1"), table
    assert M75.add_facility_columns(ref) == []

    hk_db = sqlite3.connect(str(tmp_path / "hk.db"))
    for statement in migrations.HOUSEKEEPING_TABLES:
        hk_db.execute(statement)
    from migrations_his.m0053_adt_bed_master import add_housekeeping_unit_columns
    add_housekeeping_unit_columns(hk_db)
    assert M75.add_facility_columns(hk_db, tables=("housekeeping_units",)) \
        == ["housekeeping_units.facility_id"]
    import inspect
    assert "add_facility_columns(db, tables=(\"housekeeping_units\",))" \
        in inspect.getsource(hk.ensure_housekeeping_schema)


def test_a_fresh_boot_has_every_column_including_the_lazy_tables(tmp_path):
    db_path = tmp_path / "fresh.db"
    body = (
        "import json\n"
        "from database import init_db, get_db\n"
        "from services.helix_db import init_helix_db\n"
        "from services.housekeeping_service import ensure_housekeeping_schema\n"
        "from migrations_his.m0075_facility_columns_admin import FACILITY_COLUMNS\n"
        "init_db(); init_helix_db(); ensure_housekeeping_schema(force=True)\n"
        "conn = get_db()\n"
        "out = {t: [r[1] for r in conn.execute('PRAGMA table_info(%s)' % t)] "
        "for t, c, d in FACILITY_COLUMNS}\n"
        "out['_ledger'] = [r[0] for r in conn.execute('SELECT version FROM schema_migrations')]\n"
        "print('ORB_TEST_JSON:' + json.dumps(out))\n"
    )
    proc_ = subprocess.run(
        [sys.executable, "-c", body], cwd=str(SERVER_DIR), capture_output=True, text=True,
        timeout=300,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
             "ORB_DEPLOYMENT_PROFILE": "dev", "ORB_DB_PATH": str(db_path)})
    assert proc_.returncode == 0, proc_.stderr[-4000:]
    line = [ln for ln in proc_.stdout.splitlines() if ln.startswith("ORB_TEST_JSON:")][-1]
    got = json.loads(line[len("ORB_TEST_JSON:"):])
    assert 75 in got.pop("_ledger")
    missing = [t for t, cols in got.items() if "facility_id" not in cols]
    assert missing == []
    assert "Traceback" not in proc_.stdout + proc_.stderr


# ═════════════════════════════════════════════════════════════════════════════
#  2. One facility on record
# ═════════════════════════════════════════════════════════════════════════════

def test_with_one_facility_the_resolver_reads_nothing():
    calls = []

    def boom():
        calls.append(1)
        raise AssertionError("a source was read with one facility on record")

    FS.invalidate_cache()
    with db_session() as db:
        assert not facility_stamp.multi_facility(db)
        assert facility_stamp.resolve(db, (("the thing", boom),), what="thing",
                                      fallback=boom) == 1
    assert calls == []
    assert "facility_id" in ps.IMMUTABLE_PAYMENT_FIELDS


def test_with_one_facility_money_is_stamped_facility_one():
    pid, aid = _patient(department="Caldrey Medicine")
    charge = _priced_charge(pid, aid)
    invoice = billing_service.create_invoice(patient_id=pid, admission_id=aid, issue=True)
    note = billing_service.create_credit_note(invoice["id"], reason="Billed in error")
    session = _counter("D001")
    receipt = ps.record_payment(patient_id=pid, amount_paise=rupees("500"), method="cash",
                                purpose="advance", received_by="clerk-fas",
                                counter_session_id=session["id"])
    refund = ps.request_refund(patient_id=pid, amount_paise=rupees("100"), reason="Overpaid",
                               requested_by="clerk-fas", payment_id=receipt["id"])
    with db_session() as db:
        deposits = {r[0] for r in db.execute(
            "SELECT facility_id FROM patient_deposits WHERE patient_id = ?", (pid,))}
    assert _facility_of("encounter_charges", charge["id"]) == 1
    assert _facility_of("invoices", invoice["id"]) == 1
    assert _facility_of("invoices", note["id"]) == 1
    assert _facility_of("cash_counter_sessions", session["id"]) == 1
    assert _facility_of("payments", receipt["id"]) == 1
    assert _facility_of("refunds", refund["id"]) == 1
    assert deposits == {1}
    ps.close_counter_session(session["id"], declared_cash_paise=rupees("500"),
                             closed_by="clerk-fas")


def test_with_one_facility_stores_and_assets_are_stamped_facility_one():
    store = proc.upsert_store(code=f"FASS{_hex()}", name="Caldrey sub store",
                              department="Surgery")
    vendor, item = _vendor(), _item()
    po = _issued_po(vendor, store, item)
    grn = proc.create_goods_receipt(
        purchase_order_id=po["id"], received_by="storekeeper",
        lines=[{"po_line_no": 1, "accepted_quantity_milli": 5000, "batch_no": "B1",
                "expiry_date": "2099-01-01"}])
    unit = hk.register_unit(unit_code=f"FAS-HK-{_hex()}", ward="FAS-W1", bed_label="3")
    asset = bio.upsert_asset(asset_tag=f"FAS{_hex()}", name="Infusion pump",
                             department="Surgery", actor="D001")
    incident = nabh.report_incident(incident_type="patient_fall", severity="minor",
                                    title="Slip near bed", description="Patient slipped.",
                                    reported_by="D001", reporter_role="admin")
    assert {_facility_of("stores", store["id"]), _facility_of("purchase_orders", po["id"]),
            _facility_of("goods_receipts", grn["id"]),
            _facility_of("housekeeping_units", unit["id"]),
            _facility_of("biomedical_assets", asset["id"]),
            _facility_of("nabh_incidents", incident["id"])} == {1}
    assert po["po_number"] and grn["grn_number"]


# ═════════════════════════════════════════════════════════════════════════════
#  3. Two facilities on record
# ═════════════════════════════════════════════════════════════════════════════

def test_an_f2_admissions_charges_and_bill_are_stamped_f2(second):
    pid, aid = _patient(facility=F2, department=second["dept2"])
    charge = _priced_charge(pid, aid)
    draft = billing_service.create_invoice(patient_id=pid, admission_id=aid)
    assert _facility_of("encounter_charges", charge["id"]) == F2
    assert _facility_of("invoices", draft["id"]) == F2
    assert billing_service.invoice_print_view(draft["id"])["supplier"]["facility_id"] == F2
    issued = billing_service.issue_invoice(draft["id"], actor="D001")
    with db_session() as db:
        snap = db.execute("SELECT supplier_facility_id FROM invoices WHERE id = ?",
                          (issued["id"],)).fetchone()[0]
    assert snap == F2


def test_a_credit_note_is_issued_by_the_facility_that_issued_the_bill(second):
    """Document-keyed: the note follows the bill, not where the patient was
    seen since."""
    pid, _ = _patient(facility=F2)
    _priced_charge(pid)
    invoice = billing_service.create_invoice(patient_id=pid, issue=True)
    assert _facility_of("invoices", invoice["id"]) == F2
    with db_session() as db:
        db.execute("UPDATE patients SET registered_facility_id = 1 WHERE id = ?", (pid,))
    _series(F2, "INV", "invoice", f"{second['code']}/I-")
    before_f1 = _next("INV")
    note = billing_service.create_credit_note(invoice["id"], reason="Billed in error")
    assert note["invoice_number"].startswith(f"{second['code']}/I-")
    assert _facility_of("invoices", note["id"]) == F2
    with db_session() as db:
        assert db.execute("SELECT supplier_facility_id FROM invoices WHERE id = ?",
                          (note["id"],)).fetchone()[0] == F2
    assert _next("INV") == before_f1


def test_a_receipt_takes_the_counters_facility_with_its_series_and_deposit(second):
    pid, _ = _patient(facility=1)
    session = _counter(second["cashier2"])
    assert _facility_of("cash_counter_sessions", session["id"]) == F2
    _series(F2, "RCPT", "receipt", f"{second['code']}/R-")
    receipt = ps.record_payment(patient_id=pid, amount_paise=rupees("700"), method="cash",
                                purpose="advance", received_by=second["cashier2"],
                                counter_session_id=session["id"])
    assert receipt["receipt_number"].startswith(f"{second['code']}/R-")
    assert _facility_of("payments", receipt["id"]) == F2
    with db_session() as db:
        snap = db.execute("SELECT supplier_facility_id FROM payments WHERE id = ?",
                          (receipt["id"],)).fetchone()[0]
        deposits = [r[0] for r in db.execute(
            "SELECT facility_id FROM patient_deposits WHERE payment_id = ?", (receipt["id"],))]
    assert snap == F2 and deposits == [F2]


def test_a_counter_never_takes_money_for_another_facilitys_bill(second):
    pid, _ = _patient(facility=F2)
    _priced_charge(pid)
    invoice = billing_service.create_invoice(patient_id=pid, issue=True)
    session = _counter(second["cashier1"])
    assert _facility_of("cash_counter_sessions", session["id"]) == 1
    before = (_next("RCPT"), _next("RCPT", F2))
    with pytest.raises(ps.PaymentError, match="one facility"):
        ps.record_payment(patient_id=pid, amount_paise=rupees("100"), method="cash",
                          invoice_id=invoice["id"], received_by=second["cashier1"],
                          counter_session_id=session["id"])
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM payments WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0
    assert (_next("RCPT"), _next("RCPT", F2)) == before


def test_a_refund_and_a_reversal_stay_with_the_receipts_facility(second):
    pid, _ = _patient(facility=1)
    session = _counter(second["cashier2"])
    _series(F2, "RCPT", "receipt", f"{second['code']}/R-")
    _series(F2, "REF", "refund", f"{second['code']}/F-")
    receipt = ps.record_payment(patient_id=pid, amount_paise=rupees("400"), method="cash",
                                purpose="advance", received_by=second["cashier2"],
                                counter_session_id=session["id"])
    refund = ps.request_refund(patient_id=pid, amount_paise=rupees("50"), reason="Overpaid",
                               requested_by=second["cashier2"], payment_id=receipt["id"])
    assert refund["refund_number"].startswith(f"{second['code']}/F-")
    assert _facility_of("refunds", refund["id"]) == F2
    out = ps.reverse_payment(receipt["id"], reason="Wrong patient", reversed_by="supervisor-fas")
    assert out["reversal"]["receipt_number"].startswith(f"{second['code']}/R-")
    assert _facility_of("payments", out["reversal"]["id"]) == F2


def test_a_claim_takes_its_bills_facility(second):
    pid, aid = _patient(facility=F2, department=second["dept2"])
    _priced_charge(pid, aid)
    invoice = billing_service.create_invoice(patient_id=pid, admission_id=aid, issue=True)
    tpa = f"TPA{_hex(5)}"
    payer_service.upsert_payer(code=tpa, name="Orlenne Health TPA", payer_type="tpa")
    _series(F2, "CLM", "claim", f"{second['code']}/C-")
    claim = payer_service.create_claim(patient_id=pid, payer_code=tpa, admission_id=aid,
                                       invoice_id=invoice["id"], created_by="FAS-ADM")
    assert claim["claim_number"].startswith(f"{second['code']}/C-")
    assert _facility_of("claims", claim["id"]) == F2


def test_stores_documents_take_the_stores_facility_and_its_series(second):
    store = proc.upsert_store(code=f"FASS{_hex()}", name="Vellmorn sub store",
                              department=second["dept2"])
    assert _facility_of("stores", store["id"]) == F2
    for code, kind in (("IND", "indent"), ("PO", "purchase_order"), ("GRN", "goods_receipt")):
        _series(F2, code, kind, f"{second['code']}/{code}-")
    vendor, item = _vendor(), _item()
    indent = proc.create_indent(store_code=store["code"], department=second["dept2"],
                                lines=[{"item_code": item["code"], "quantity_milli": 5000}],
                                submit=True, requested_by=second["cashier2"])
    po = _issued_po(vendor, store, item)
    grn = proc.create_goods_receipt(
        purchase_order_id=po["id"], received_by="storekeeper",
        lines=[{"po_line_no": 1, "accepted_quantity_milli": 5000, "batch_no": "B2",
                "expiry_date": "2099-01-01"}])
    assert (_facility_of("indents", indent["id"]), _facility_of("purchase_orders", po["id"]),
            _facility_of("goods_receipts", grn["id"])) == (F2, F2, F2)
    assert indent["indent_number"].startswith(f"{second['code']}/IND-")
    assert po["po_number"].startswith(f"{second['code']}/PO-")
    assert grn["grn_number"].startswith(f"{second['code']}/GRN-")


def test_a_facility_without_its_series_leaves_the_order_unnumbered(second):
    store = proc.upsert_store(code=f"FASS{_hex()}", name="Vellmorn sub store",
                              department=second["dept2"])
    before = _next("PO")
    po = _issued_po(_vendor(), store, _item())
    assert po["po_number"] is None
    assert _next("PO") == before


def test_goods_for_one_facility_never_land_in_anothers_store(second):
    store2 = proc.upsert_store(code=f"FASS{_hex()}", name="Vellmorn sub store",
                               department=second["dept2"])
    store1 = proc.upsert_store(code=f"FASS{_hex()}", name="Caldrey sub store",
                               department=second["dept1"])
    po = _issued_po(_vendor(), store2, _item())
    with pytest.raises(proc.ProcurementError) as err:
        proc.create_goods_receipt(
            purchase_order_id=po["id"], store_code=store1["code"], received_by="storekeeper",
            lines=[{"po_line_no": 1, "accepted_quantity_milli": 1000, "batch_no": "B3",
                    "expiry_date": "2099-01-01"}])
    assert err.value.code == facility_stamp.MISMATCH
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM goods_receipts WHERE purchase_order_id = ?",
                          (po["id"],)).fetchone()[0] == 0


def test_a_store_nobody_can_place_is_refused_and_a_store_keeps_its_facility(second):
    with pytest.raises(proc.ProcurementError) as err:
        proc.upsert_store(code=f"FASS{_hex()}", name="Unplaced store")
    assert err.value.code == facility_stamp.UNKNOWN
    store = proc.upsert_store(code=f"FASS{_hex()}", name="Vellmorn sub store",
                              department=second["dept2"])
    with pytest.raises(proc.ProcurementError) as moved:
        proc.upsert_store(code=store["code"], name="Vellmorn sub store",
                          department=second["dept1"])
    assert moved.value.code == facility_stamp.MISMATCH
    assert _facility_of("stores", store["id"]) == F2


def test_a_housekeeping_unit_takes_its_wards_facility(second):
    ward2, ward1 = _ward(second["dept2"]), _ward(second["dept1"])
    code = f"FAS-HK-{_hex()}"
    unit = hk.register_unit(unit_code=code, ward=ward2, bed_label="4")
    assert _facility_of("housekeeping_units", unit["id"]) == F2
    with pytest.raises(hk.HousekeepingError, match="one facility"):
        hk.register_unit(unit_code=code, ward=ward1, bed_label="4")
    assert _facility_of("housekeeping_units", unit["id"]) == F2


def test_a_machine_takes_its_departments_facility_and_keeps_it(second):
    tag = f"FAS{_hex()}"
    asset = bio.upsert_asset(asset_tag=tag, name="Defibrillator",
                             department=second["dept2"], actor=second["cashier2"])
    assert _facility_of("biomedical_assets", asset["id"]) == F2
    with pytest.raises(bio.BiomedicalError) as err:
        bio.upsert_asset(asset_tag=tag, name="Defibrillator", department=second["dept1"])
    assert err.value.code == facility_stamp.MISMATCH
    placed_by_account = bio.upsert_asset(asset_tag=f"FAS{_hex()}", name="Suction pump",
                                         actor=second["cashier2"])
    assert _facility_of("biomedical_assets", placed_by_account["id"]) == F2


def test_an_incident_is_filed_at_the_patients_facility_and_never_refused(second):
    pid, _ = _patient(facility=F2, department=second["dept2"])
    f1_before = _next("INC")
    first = nabh.report_incident(incident_type="patient_fall", severity="minor", title="Slip",
                                 description="Slipped near the bed.", patient_id=pid,
                                 reported_by=second["cashier1"], reporter_role="admin")
    assert _facility_of("nabh_incidents", first["id"]) == F2
    assert _next("INC") != f1_before          # no F2 series yet: facility 1's numbered it
    _series(F2, "INC", "incident", f"{second['code']}/INC-")
    later = nabh.report_incident(incident_type="patient_fall", severity="minor", title="Slip",
                                 description="Slipped again.", patient_id=pid,
                                 reported_by=second["cashier1"], reporter_role="admin")
    assert later["reference"].startswith(f"{second['code']}/INC-")
    anonymous = nabh.report_incident(incident_type="other", severity="minor", title="Noise",
                                     description="Alarm left sounding.", is_anonymous=True,
                                     reported_by=second["cashier2"], reporter_role="admin")
    # Nothing about who reported it places an anonymous report.
    assert _facility_of("nabh_incidents", anonymous["id"]) == 1
    by_reporter = nabh.report_incident(incident_type="other", severity="minor",
                                       title="Noise", description="Alarm left sounding.",
                                       reported_by=second["cashier2"], reporter_role="admin")
    assert _facility_of("nabh_incidents", by_reporter["id"]) == F2


def test_an_evidence_pack_is_refused_while_two_facilities_are_on_record(second):
    with db_session() as db:
        before = db.execute("SELECT COUNT(*) FROM nabh_evidence_exports").fetchone()[0]
    with pytest.raises(nabh.NabhError) as err:
        nabh.record_evidence_export(generated_by="FAS-ADM")
    assert err.value.code == "pack_spans_facilities"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM nabh_evidence_exports").fetchone()[0] == before


def test_a_care_context_takes_its_admissions_facility(second):
    pid, aid = _patient(facility=F2, department=second["dept2"])
    identity = _verified_identity(pid)
    cc = ab.link_care_context(identity_id=identity["id"], care_context_reference=aid,
                              encounter_ref=aid, hi_type="DischargeSummary",
                              linked_by="FAS-MRD")
    assert _facility_of("abdm_care_contexts", cc["id"]) == F2


def test_a_kitchen_indent_takes_its_wards_facility_and_its_series(second):
    ward2 = _ward(second["dept2"])
    plan = _locked_plan_on(ward2, second["dept2"], F2)
    indent = ds.raise_kitchen_indent(meal_plan_id=plan["id"], raised_by="D001")
    assert _facility_of("kitchen_indents", indent["id"]) == F2
    f1_before = _next("KIND")
    with pytest.raises(ds.KitchenIndentError, match="no kitchen indent number series"):
        ds.issue_kitchen_indent(indent["id"], issued_by="D001")
    assert _next("KIND") == f1_before
    _series(F2, "KIND", "kitchen_indent", f"{second['code']}/K-")
    issued = ds.issue_kitchen_indent(indent["id"], issued_by="D001")
    assert issued["indent_number"].startswith(f"{second['code']}/K-")


def test_a_counter_the_master_cannot_place_is_not_opened(second):
    stray = _user(role="nurse", department=f"Unlisted Ward {_hex(4)}")
    with pytest.raises(ps.PaymentError, match="cannot tell which facility"):
        _counter(stray)
