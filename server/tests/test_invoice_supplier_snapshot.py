"""The supplier block a numbered bill or receipt was issued with (multisite-2,
W9-E, migration 72).

A GST invoice never changes after issue. Before this change the supplier block
was built at print time, so once facility identity became editable a reprint
of last month's invoice would have shown this month's GSTIN and address. These
tests pin the four halves of the fix:

  1. The migration: six columns on invoices and payments, and two triggers
     that refuse any later change to them on a numbered document (and refuse
     filling one in on a document numbered before the migration).
  2. Invoices: the snapshot is written in the statement that assigns the
     number (issue, credit note); a numbered invoice reprints byte for byte
     after the identity changes; one numbered before migration 72 prints the
     old 'Not configured' block; a draft prints the identity as it stands.
  3. Receipts: every receipt carries the snapshot taken when it was written,
     and the receipt print reads it (GET /api/payments/receipts/{id}/print),
     writing one phi_access audit row per print.
  4. The discharge print and the patient's discharge page name the facility
     through facility_identity.print_site_name.

Every test puts facility 1's identity back as it found it, because
test_billing's print test expects an unconfigured site.
"""
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import migrations  # noqa: F401  (import before the migration file, see migrations_his)
from auth import create_token
from database import db_session
from db_backend import DBIntegrityError
from migrations_his import m0072_invoice_supplier_snapshot as M72
from services import billing_masters, billing_service, facility_identity as FI
from services import facility_service as FS
from services import payments_service as ps
from services.money import rupees

GSTIN_MH = "27AAPFU0939F1ZV"
GSTIN_MH_2 = "27AAPFU0939F2ZU"
SITE_NAME = "Snapshot General"

ADMIN = {"id": "D001", "role": "admin", "department": None}


@pytest.fixture(autouse=True)
def _facility_one_restored(monkeypatch):
    """Facility 1 as the suite has it (nothing configured), restored after,
    with a known site settings name."""
    from services import site_config_service as scs
    cfg = scs.SiteConfig(present=True, site_name=SITE_NAME)
    monkeypatch.setattr(scs, "current_site_config", lambda: cfg)
    with db_session() as db:
        before = {k: v for k, v in zip(
            [r[1] for r in db.execute("PRAGMA table_info(facilities)")],
            db.execute("SELECT * FROM facilities WHERE id = 1").fetchone())}
    FS.invalidate_cache()
    yield
    with db_session() as db:
        cols = [c for c in before if c != "id"]
        db.execute(f"UPDATE facilities SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = 1",
                   tuple(before[c] for c in cols))
    FS.invalidate_cache()


def _configure(**fields):
    return FI.update_facility(1, fields, user=ADMIN)


def _patient() -> str:
    pid = f"PATSNP{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'discharged')",
                   (pid, "Snapshot Patient", f"MRN-{pid}"))
    return pid


@pytest.fixture()
def billed_patient():
    """A patient with one priced, unbilled consultation charge."""
    pid = _patient()
    plan = int(billing_service.upsert_tariff_plan(
        code=f"TP{uuid.uuid4().hex[:6].upper()}", name="Snapshot plan")["id"])
    code = f"TST-SNP{uuid.uuid4().hex[:6].upper()}"
    billing_masters.upsert_service_item(code=code, name="Consultation", category="consultation",
                                        gst_treatment="exempt", gst_rate_bp=0)
    billing_masters.set_tariff_rate(tariff_plan_id=plan, service_code=code,
                                    rate_paise=rupees("1000"))
    billing_service.add_charge(patient_id=pid, service_code=code, tariff_plan_id=plan)
    return pid


def _printed(invoice_id: int) -> str:
    view = billing_service.invoice_print_view(invoice_id)
    view.pop("printed_at")
    return json.dumps(view, sort_keys=True, default=str)


def _row(table: str, row_id: int) -> dict:
    with db_session() as db:
        row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return {k: row[k] for k in row.keys()}


# ── 1. The migration ─────────────────────────────────────────────────────────

def test_the_writers_bind_the_migrations_columns_in_order():
    assert FI.SNAPSHOT_COLUMNS == M72.SNAPSHOT_COLUMNS


def test_migration_72_is_ledgered_with_its_columns_and_triggers():
    with db_session() as db:
        assert db.execute("SELECT 1 FROM schema_migrations WHERE version = 72").fetchone()
        for table in M72.TABLES_WITH_SNAPSHOT:
            cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            assert set(M72.SNAPSHOT_COLUMNS) <= cols, table
        triggers = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert M72.TRIGGER_NAMES <= triggers


def test_the_migration_is_idempotent_and_skips_absent_tables(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, invoice_number TEXT)")
    conn.execute("INSERT INTO invoices (invoice_number) VALUES ('INV-1')")
    M72.apply(conn)
    conn.commit()
    schema = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
    M72.apply(conn)
    conn.commit()
    assert sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()) == schema
    assert conn.execute("SELECT supplier_snapshot_at FROM invoices").fetchone()[0] is None
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")} \
        == {"trg_invoices_supplier_snapshot_fixed"}
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ── 2. Invoices ──────────────────────────────────────────────────────────────

def test_issuing_writes_the_snapshot_with_the_number(billed_patient):
    _configure(legal_name="Snapshot Hospitals Pvt Ltd", address_line1="1 MG Road",
               city="Pune", state="Maharashtra", pincode="411001", gstin=GSTIN_MH)
    invoice = billing_service.create_invoice(patient_id=billed_patient)
    assert _row("invoices", invoice["id"])["supplier_snapshot_at"] is None
    issued = billing_service.issue_invoice(invoice["id"], actor="D001")
    row = _row("invoices", issued["id"])
    assert row["invoice_number"] and row["supplier_snapshot_at"]
    assert row["supplier_facility_id"] == 1
    assert row["supplier_legal_name"] == "Snapshot Hospitals Pvt Ltd"
    assert row["supplier_address"] == "1 MG Road, Pune, Maharashtra 411001"
    assert row["supplier_gstin"] == GSTIN_MH
    assert row["supplier_state_code"] == "27"
    supplier = billing_service.invoice_print_view(issued["id"])["supplier"]
    assert supplier["source"] == "snapshot"
    assert supplier["gstin"] == GSTIN_MH and supplier["notice"] is None
    assert issued["supplier_name"] == "Snapshot Hospitals Pvt Ltd"


def test_an_issued_invoice_reprints_byte_for_byte_after_an_identity_change(billed_patient):
    _configure(legal_name="First Legal Name", address_line1="1 MG Road", gstin=GSTIN_MH)
    issued = billing_service.create_invoice(patient_id=billed_patient, issue=True)
    before = _printed(issued["id"])
    _configure(legal_name="Second Legal Name", address_line1="9 New Road", gstin=GSTIN_MH_2,
               name="Renamed Hospital")
    assert _printed(issued["id"]) == before
    assert "Second Legal Name" not in before and "First Legal Name" in before
    assert billing_service.get_invoice(issued["id"])["supplier_name"] == "First Legal Name"


def test_an_unconfigured_site_snapshots_the_site_name_and_prints_not_configured(
        billed_patient, monkeypatch):
    issued = billing_service.create_invoice(patient_id=billed_patient, issue=True)
    view = billing_service.invoice_print_view(issued["id"])
    supplier = view["supplier"]
    assert supplier["source"] == "snapshot"
    assert supplier["name"] == SITE_NAME and supplier["name_is_configured"] is True
    assert supplier["address"] == "Not configured" and supplier["gstin"] == "Not configured"
    assert "not a GST tax invoice" in supplier["notice"]
    # Renaming the site in its settings file later does not rename the bill.
    from services import site_config_service as scs
    renamed = scs.SiteConfig(present=True, site_name="Renamed Elsewhere")
    monkeypatch.setattr(scs, "current_site_config", lambda: renamed)
    assert billing_service.invoice_print_view(issued["id"])["supplier"]["name"] == SITE_NAME


def test_an_invoice_numbered_before_migration_72_prints_the_legacy_block():
    pid = _patient()
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO invoices (invoice_number, patient_id, status, payable_paise, "
            "balance_paise) VALUES (?, ?, 'issued', 100, 100)",
            (f"INV-LEGACY-{uuid.uuid4().hex[:6]}", pid))
        invoice_id = int(cur.lastrowid)
    _configure(legal_name="Configured Later", address_line1="1 MG Road", gstin=GSTIN_MH)
    supplier = billing_service.invoice_print_view(invoice_id)["supplier"]
    assert supplier["source"] == "legacy"
    assert supplier["name"] == SITE_NAME
    assert supplier["address"] == "Not configured" and supplier["gstin"] == "Not configured"
    assert supplier["notice"] == FI.LEGACY_NOTICE
    # ...and the trigger refuses filling the snapshot in afterwards.
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("UPDATE invoices SET supplier_gstin = ?, supplier_snapshot_at = 'x' "
                       "WHERE id = ?", (GSTIN_MH, invoice_id))
    assert _row("invoices", invoice_id)["supplier_snapshot_at"] is None


def test_a_draft_shows_the_identity_as_it_stands(billed_patient):
    draft = billing_service.create_invoice(patient_id=billed_patient)
    first = billing_service.invoice_print_view(draft["id"])["supplier"]
    assert first["source"] == "live" and first["gstin"] == "Not configured"
    assert first["name"] == SITE_NAME
    _configure(legal_name="Draft Time Legal", address_line1="2 Station Road", gstin=GSTIN_MH)
    second = billing_service.invoice_print_view(draft["id"])["supplier"]
    assert second["source"] == "live"
    assert second["name"] == "Draft Time Legal" and second["gstin"] == GSTIN_MH
    assert second["notice"] is None
    assert billing_service.get_invoice(draft["id"])["supplier_name"] == "Draft Time Legal"


def test_a_numbered_invoice_refuses_any_supplier_change(billed_patient):
    issued = billing_service.create_invoice(patient_id=billed_patient, issue=True)
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("UPDATE invoices SET supplier_legal_name = 'Edited' WHERE id = ?",
                       (issued["id"],))
    # Payment state still moves: only the supplier columns are fixed.
    with db_session() as db:
        db.execute("UPDATE invoices SET notes = 'still writable' WHERE id = ?", (issued["id"],))
    assert _row("invoices", issued["id"])["notes"] == "still writable"


def test_a_credit_note_takes_its_snapshot_when_it_is_numbered(billed_patient):
    _configure(legal_name="At Invoice Time", address_line1="1 MG Road", gstin=GSTIN_MH)
    issued = billing_service.create_invoice(patient_id=billed_patient, issue=True)
    _configure(legal_name="At Credit Time")
    note = billing_service.create_credit_note(issued["id"], reason="Billed in error",
                                              actor="D001")
    row = _row("invoices", note["id"])
    assert row["supplier_snapshot_at"] and row["supplier_legal_name"] == "At Credit Time"
    assert _row("invoices", issued["id"])["supplier_legal_name"] == "At Invoice Time"


# ── 3. Receipts ──────────────────────────────────────────────────────────────

def _receipt(pid: str) -> dict:
    return ps.record_payment(patient_id=pid, amount_paise=50000, method="cash",
                             purpose="advance", received_by="D001")


def test_every_receipt_carries_the_snapshot_and_reprints_unchanged():
    pid = _patient()
    _configure(legal_name="Receipt Legal", address_line1="1 MG Road", gstin=GSTIN_MH)
    receipt = _receipt(pid)
    row = _row("payments", receipt["id"])
    assert row["supplier_snapshot_at"] and row["supplier_gstin"] == GSTIN_MH
    first = ps.receipt_print_view(receipt["id"])
    assert first["supplier"]["source"] == "snapshot"
    assert first["supplier"]["name"] == "Receipt Legal"
    assert first["amount_in_words"] == "Rupees Five Hundred Only"
    _configure(legal_name="Changed After", gstin=GSTIN_MH_2)
    second = ps.receipt_print_view(receipt["id"])
    first.pop("printed_at"), second.pop("printed_at")
    assert json.dumps(second, sort_keys=True, default=str) == json.dumps(first, sort_keys=True,
                                                                         default=str)
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("UPDATE payments SET supplier_gstin = ? WHERE id = ?",
                       (GSTIN_MH_2, receipt["id"]))


def test_a_reversal_receipt_carries_its_own_snapshot():
    pid = _patient()
    receipt = _receipt(pid)
    _configure(legal_name="At Reversal", address_line1="1 MG Road", gstin=GSTIN_MH)
    out = ps.reverse_payment(receipt["id"], reason="Wrong patient", reversed_by="ADM2")
    reversal = _row("payments", out["reversal"]["id"])
    assert reversal["supplier_snapshot_at"] and reversal["supplier_legal_name"] == "At Reversal"
    original = _row("payments", receipt["id"])
    assert original["status"] == "reversed" and original["supplier_legal_name"] == SITE_NAME


def test_a_receipt_written_before_migration_72_prints_the_legacy_block():
    pid = _patient()
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO payments (receipt_number, patient_id, amount_paise, direction, "
            "method, purpose, status) VALUES (?, ?, 100, 'in', 'cash', 'advance', 'received')",
            (f"RCPT-LEGACY-{uuid.uuid4().hex[:6]}", pid))
        payment_id = int(cur.lastrowid)
    supplier = ps.receipt_print_view(payment_id)["supplier"]
    assert supplier["source"] == "legacy" and supplier["gstin"] == "Not configured"


def test_the_receipt_print_route():
    from routes.payments_routes import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    uid = f"CSH{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, 'cashier', 'x')",
                   (uid, "Snapshot cashier"))
    hdr = {"Authorization": f"Bearer {create_token(uid, 'cashier')}"}
    receipt = _receipt(_patient())
    resp = client.get(f"/api/payments/receipts/{receipt['id']}/print", headers=hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["receipt"]["id"] == receipt["id"] and body["supplier"]["source"] == "snapshot"
    assert client.get("/api/payments/receipts/999999999/print", headers=hdr).status_code == 404
    assert client.get(f"/api/payments/receipts/{receipt['id']}/print").status_code == 401


def _print_audit_rows(payment_id: int) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'payments_receipt_printed' "
            "AND target_id = ? ORDER BY id", (str(payment_id),)).fetchall()]


def test_a_receipt_print_writes_one_phi_access_row():
    """A print is a disclosure, like billing_routes.print_invoice: each print
    writes one fail-closed phi_access row naming the patient and the receipt;
    a refused print (unknown receipt, no login) writes none."""
    from routes.payments_routes import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    uid = f"CSH{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, 'cashier', 'x')",
                   (uid, "Audit cashier"))
    hdr = {"Authorization": f"Bearer {create_token(uid, 'cashier')}"}
    pid = _patient()
    receipt = _receipt(pid)
    assert _print_audit_rows(receipt["id"]) == []

    assert client.get(f"/api/payments/receipts/{receipt['id']}/print",
                      headers=hdr).status_code == 200
    rows = _print_audit_rows(receipt["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["event_category"] == "phi_access"
    assert row["outcome"] == "success"
    assert row["patient_id"] == pid
    assert row["target_type"] == "payment"
    assert row["actor_user_id"] == uid

    assert client.get(f"/api/payments/receipts/{receipt['id']}/print").status_code == 401
    assert client.get("/api/payments/receipts/999999999/print", headers=hdr).status_code == 404
    assert len(_print_audit_rows(receipt["id"])) == 1
    assert _print_audit_rows(999999999) == []

    assert client.get(f"/api/payments/receipts/{receipt['id']}/print",
                      headers=hdr).status_code == 200
    assert len(_print_audit_rows(receipt["id"])) == 2


# ── 4. Discharge print and the patient's discharge page ──────────────────────

def test_the_print_name_follows_facility_identity():
    pid = _patient()
    assert FI.print_site_name(pid) == SITE_NAME
    _configure(name="Identity Name Hospital")
    assert FI.print_site_name(pid) == "Identity Name Hospital"
    _configure(name=None)
    assert FI.print_site_name(pid) == SITE_NAME


def test_an_unnamed_site_prints_no_name(monkeypatch):
    from services import site_config_service as scs
    monkeypatch.setattr(scs, "current_site_config", lambda: scs.SiteConfig(present=False))
    assert FI.print_site_name(_patient()) is None


def test_the_chart_route_carries_the_identity_name():
    from main import app
    client = TestClient(app)
    pid = _patient()
    _configure(name="Chart Print Hospital")
    hdr = {"Authorization": f"Bearer {create_token('D001', 'doctor')}"}
    with db_session() as db:
        dept = db.execute("SELECT department FROM users WHERE id = 'D001'").fetchone()[0]
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')", (f"ADM-{pid}", pid, dept))
    try:
        resp = client.get(f"/api/vigil/patients/{pid}", headers=hdr)
        assert resp.status_code == 200, resp.text
        assert resp.json()["site_name"] == "Chart Print Hospital"
    finally:
        with db_session() as db:
            db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))


def test_the_discharge_page_reads_its_name_through_facility_identity():
    import inspect
    from services import discharge_access_service
    src = inspect.getsource(discharge_access_service)
    assert "facility_identity.print_site_name(patient_id, db=db)" in src
    assert "current_site_config().site_name" not in src
