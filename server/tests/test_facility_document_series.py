"""Document numbering per facility (multisite-7a, W10-A, migration 73).

  1. The migration: document_number_series gains facility_id (NOT NULL, 1) and
     the key UNIQUE(facility_id, series_code); every existing row lands at
     facility 1 unchanged, ids and the AUTOINCREMENT mark included; a second
     run changes nothing; it refuses what it would drop.
  2. The allocators: next_document_number and allocate_series_number take the
     facility (default 1). One-facility numbering is byte-identical; a second
     facility numbers from its own row and never borrows facility 1's.
  3. The callers: a receipt, a refund, a claim and a credit note of a second
     facility's patient are numbered from that facility's series (and refused
     in words while it has none); stores, kitchen and NABH documents keep
     facility 1's.
  4. The prefix rule, on set-up and on edit: no prefix may equal, or begin
     with, or be the beginning of, another facility's prefix for the same
     register (or another series writing the same number column), and none
     may begin a number already issued.
  5. The routes: GET and PUT /api/facilities/{id}/document-series, behind
     ORB_MULTI_SITE, facility.read and facility.manage on the account's own
     facility (or group-wide access), audited site_admin.

Every second facility here is removed in teardown with the cache invalidated
(the TEST RULE in docs/his_design/build/contracts.md), and facility 1's
series get their prefix, suffix, digits and configured flag back. Counters
are never moved back: a counter that went backwards would reissue numbers
other tests already hold.
"""
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
import facility_fence
import migrations  # noqa: F401  (import before the migration file, see migrations_his)
from auth import create_token, hash_password
from database import db_session
from db_backend import DBIntegrityError
from migrations_his import m0073_document_series_per_facility as M73
from services import billing_masters as BM
from services import billing_service, facility_service as FS
from services import payer_service, payments_service as ps
from services.billing_masters import SeriesEditError, SeriesNotSetUp
from services.money import rupees

SEEDED = ("INV", "RCPT", "REF", "CLM", "IND", "PO", "GRN", "KIND", "INC", "NABHPACK",
          "UHID", "PHPO", "PHGRN", "PHIND", "PHTRF", "PHVR", "PHRET", "PHCNT")


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _series_restored():
    """Facility 1's editable series fields as they were; every other
    facility's rows and any second facility removed."""
    with db_session() as db:
        before = [dict(r) for r in db.execute(
            "SELECT id, prefix, suffix, pad_width, is_configured FROM document_number_series "
            "WHERE facility_id = 1").fetchall()]
        facilities_before = {r[0] for r in db.execute("SELECT id FROM facilities")}
    FS.invalidate_cache()
    yield
    with db_session() as db:
        for row in before:
            db.execute("UPDATE document_number_series SET prefix = ?, suffix = ?, pad_width = ?, "
                       "is_configured = ? WHERE id = ?",
                       (row["prefix"], row["suffix"], row["pad_width"], row["is_configured"],
                        row["id"]))
        db.execute("DELETE FROM document_number_series WHERE facility_id != 1")
        for (fid,) in db.execute("SELECT id FROM facilities").fetchall():
            if fid not in facilities_before:
                db.execute("DELETE FROM facilities WHERE id = ?", (fid,))
                db.execute("DELETE FROM departments WHERE facility_id = ?", (fid,))
    FS.invalidate_cache()


@pytest.fixture()
def second_facility():
    code = f"DS{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        cur = db.execute("INSERT INTO facilities (uuid, code, name) VALUES (?, ?, 'Orvanne Annexe')",
                         (str(uuid.uuid4()), code))
        fid = int(cur.lastrowid)
    FS.invalidate_cache()
    yield {"id": fid, "code": code}
    with db_session() as db:
        db.execute("DELETE FROM document_number_series WHERE facility_id = ?", (fid,))
        db.execute("DELETE FROM facilities WHERE id = ?", (fid,))
    FS.invalidate_cache()


@pytest.fixture()
def switch_on(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)


def _fy() -> str:
    return BM.financial_year_for()


def _row(series_code: str, facility_id: int = 1) -> dict:
    with db_session() as db:
        row = db.execute("SELECT * FROM document_number_series WHERE series_code = ? "
                         "AND facility_id = ?", (series_code, facility_id)).fetchone()
    return dict(row) if row is not None else None


def _add_series(facility_id: int, series_code: str, prefix: str, *, next_number: int = 1,
                financial_year=None, document_type: str = "invoice"):
    with db_session() as db:
        db.execute(
            "INSERT INTO document_number_series (facility_id, series_code, document_type, prefix, "
            "next_number, financial_year) VALUES (?, ?, ?, ?, ?, ?)",
            (facility_id, series_code, document_type, prefix, next_number, financial_year))


def _patient_at(facility_id: int) -> str:
    pid = f"PDS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, registered_facility_id) "
                   "VALUES (?, ?, ?, 'admitted', ?)",
                   (pid, "Quillon Varrette", f"MRN-{pid}", facility_id))
    return pid


def _issued_invoice(pid: str) -> dict:
    plan = int(billing_service.upsert_tariff_plan(
        code=f"TP{uuid.uuid4().hex[:6].upper()}", name="Series plan")["id"])
    code = f"TST-DS{uuid.uuid4().hex[:6].upper()}"
    BM.upsert_service_item(code=code, name="Consultation", category="consultation")
    BM.set_tariff_rate(tariff_plan_id=plan, service_code=code, rate_paise=rupees("700"))
    billing_service.add_charge(patient_id=pid, service_code=code, tariff_plan_id=plan)
    return billing_service.create_invoice(patient_id=pid, issue=True)


# ═════════════════════════════════════════════════════════════════════════════
#  1. The migration
# ═════════════════════════════════════════════════════════════════════════════

def test_migration_73_is_ledgered_and_every_series_sits_at_facility_one():
    with db_session() as db:
        assert db.execute("SELECT 1 FROM schema_migrations WHERE version = 73").fetchone()
        cols = [r[1] for r in db.execute("PRAGMA table_info(document_number_series)")]
        assert cols == list(M73.CANONICAL_COLUMNS)
        assert M73._unique_keys(db) == [("facility_id", "series_code")]
        assert M73.is_new_shape(db)
        codes = {r[0] for r in db.execute(
            "SELECT series_code FROM document_number_series WHERE facility_id = 1")}
        others = db.execute(
            "SELECT COUNT(*) FROM document_number_series WHERE facility_id != 1").fetchone()[0]
    assert set(SEEDED) <= codes
    assert others == 0
    uhid = _row("UHID")
    assert (uhid["resets_yearly"], uhid["check_digit"]) == (0, "verhoeff")


def _legacy_series_db(path, *, with_m50_columns: bool = True):
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE document_number_series (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_code TEXT NOT NULL UNIQUE,
        document_type TEXT NOT NULL,
        prefix TEXT NOT NULL DEFAULT '',
        suffix TEXT NOT NULL DEFAULT '',
        financial_year TEXT,
        pad_width INTEGER NOT NULL DEFAULT 6,
        next_number INTEGER NOT NULL DEFAULT 1,
        is_configured INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    if with_m50_columns:
        conn.execute("ALTER TABLE document_number_series ADD COLUMN resets_yearly "
                     "INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE document_number_series ADD COLUMN check_digit TEXT")
    for code, kind, prefix, nxt, configured in (
            ("INV", "invoice", "HSP/INV/", 42, 1), ("RCPT", "receipt", "RCPT-", 7, 0),
            ("GONE", "temporary", "G-", 1, 0), ("PO", "purchase_order", "PO-", 3, 0)):
        conn.execute("INSERT INTO document_number_series (series_code, document_type, prefix, "
                     "next_number, is_configured, financial_year) VALUES (?, ?, ?, ?, ?, '2026-27')",
                     (code, kind, prefix, nxt, configured))
    if with_m50_columns:
        conn.execute("INSERT INTO document_number_series (series_code, document_type, prefix, "
                     "next_number, is_configured, resets_yearly, check_digit) VALUES "
                     "('UHID', 'uhid', '', 17, 1, 0, 'verhoeff')")
    conn.execute("DELETE FROM document_number_series WHERE series_code = 'GONE'")
    conn.commit()
    return conn


def _apply(conn):
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        M73.apply(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def test_the_rebuild_keeps_every_row_its_id_and_the_sequence_mark_and_runs_once(tmp_path):
    conn = _legacy_series_db(tmp_path / "legacy.db")
    before = conn.execute("SELECT * FROM document_number_series ORDER BY id").fetchall()
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'document_number_series'"
                       ).fetchone()[0]
    _apply(conn)
    after = conn.execute("SELECT * FROM document_number_series ORDER BY id").fetchall()
    assert [r[:-1] for r in after] == before
    assert {r[-1] for r in after} == {1}
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'document_number_series'"
                        ).fetchone()[0] == seq
    assert M73.is_new_shape(conn)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (M73.ASIDE,)).fetchone()
    # A later series gets a new id, never the deleted one's.
    conn.execute("INSERT INTO document_number_series (series_code, document_type) "
                 "VALUES ('NEW', 'x')")
    assert conn.execute("SELECT id FROM document_number_series WHERE series_code = 'NEW'"
                        ).fetchone()[0] == seq + 1
    conn.execute("DELETE FROM document_number_series WHERE series_code = 'NEW'")
    schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    rows = conn.execute("SELECT * FROM document_number_series ORDER BY id").fetchall()
    _apply(conn)
    assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    assert conn.execute("SELECT * FROM document_number_series ORDER BY id").fetchall() == rows
    conn.close()


def test_a_table_from_before_migration_50_gains_its_columns_with_their_defaults(tmp_path):
    conn = _legacy_series_db(tmp_path / "pre50.db", with_m50_columns=False)
    _apply(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(document_number_series)")]
    assert cols == list(M73.CANONICAL_COLUMNS)
    assert conn.execute("SELECT resets_yearly, check_digit, facility_id, next_number FROM "
                        "document_number_series WHERE series_code = 'INV'").fetchone() == (
        1, None, 1, 42)
    conn.close()


def test_the_new_key_lets_a_second_facility_hold_a_code_once(tmp_path):
    conn = _legacy_series_db(tmp_path / "key.db")
    _apply(conn)
    conn.execute("INSERT OR IGNORE INTO document_number_series (series_code, document_type, "
                 "prefix) VALUES ('INV', 'invoice', 'INV-')")
    assert conn.execute("SELECT COUNT(*) FROM document_number_series WHERE series_code = 'INV'"
                        ).fetchone()[0] == 1
    conn.execute("INSERT INTO document_number_series (facility_id, series_code, document_type, "
                 "prefix) VALUES (2, 'INV', 'invoice', 'F2/')")
    with pytest.raises(DBIntegrityError):
        conn.execute("INSERT INTO document_number_series (facility_id, series_code, "
                     "document_type, prefix) VALUES (2, 'INV', 'invoice', 'F2B/')")
    conn.close()


@pytest.mark.parametrize("stranger", ["column", "index", "child", "aside"])
def test_the_rebuild_refuses_what_it_would_drop_and_changes_nothing(tmp_path, stranger):
    conn = _legacy_series_db(tmp_path / f"{stranger}.db")
    if stranger == "column":
        conn.execute("ALTER TABLE document_number_series ADD COLUMN site_note TEXT")
    elif stranger == "index":
        conn.execute("CREATE INDEX idx_series_prefix ON document_number_series(prefix)")
    elif stranger == "child":
        conn.execute("CREATE TABLE series_notes (id INTEGER PRIMARY KEY, series_id INTEGER "
                     "REFERENCES document_number_series(id))")
    else:
        conn.execute("CREATE TABLE document_number_series_aside (id INTEGER)")
    conn.commit()
    schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    rows = conn.execute("SELECT * FROM document_number_series ORDER BY id").fetchall()
    with pytest.raises(RuntimeError):
        _apply(conn)
    assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    assert conn.execute("SELECT * FROM document_number_series ORDER BY id").fetchall() == rows
    conn.close()


def test_ensure_default_masters_reinsert_lands_at_facility_one():
    _add_series(9737, "INV", "F9737/")
    before = _row("GRN")
    with db_session() as db:
        db.execute("DELETE FROM document_number_series WHERE series_code = 'GRN' AND facility_id = 1")
    try:
        BM.ensure_default_masters()
        again = _row("GRN")
        assert again is not None and again["facility_id"] == 1
        assert again["prefix"] == "GRN-" and again["is_configured"] == 0
        with db_session() as db:
            assert db.execute("SELECT COUNT(*) FROM document_number_series WHERE series_code = "
                              "'INV' AND facility_id = 1").fetchone()[0] == 1
    finally:
        with db_session() as db:
            db.execute("UPDATE document_number_series SET prefix = ?, suffix = ?, pad_width = ?, "
                       "next_number = MAX(next_number, ?), financial_year = ?, is_configured = ? "
                       "WHERE series_code = 'GRN' AND facility_id = 1",
                       (before["prefix"], before["suffix"], before["pad_width"],
                        before["next_number"], before["financial_year"], before["is_configured"]))


# ═════════════════════════════════════════════════════════════════════════════
#  2. The allocators
# ═════════════════════════════════════════════════════════════════════════════

def test_one_facility_numbering_is_unchanged():
    first = BM.next_document_number("INV")
    row = _row("INV")
    second = BM.next_document_number("INV", facility_id=1)
    assert second["sequence"] == first["sequence"] + 1
    assert second["number"] == (f"{row['prefix']}{_fy()}-"
                                f"{str(second['sequence']).zfill(int(row['pad_width']))}"
                                f"{row['suffix']}")
    assert set(second) == {"series_code", "number", "sequence", "financial_year", "is_configured"}


def test_a_second_facilitys_number_comes_from_its_own_series():
    _add_series(9731, "INV", "F9731/INV-", next_number=5, financial_year=_fy())
    main_before = _row("INV")["next_number"]
    out = BM.next_document_number("INV", facility_id=9731)
    assert out["number"] == f"F9731/INV-{_fy()}-000005"
    assert _row("INV", 9731)["next_number"] == 6
    assert _row("INV")["next_number"] == main_before


def test_a_facility_without_the_series_is_refused_never_given_facility_ones():
    before = _row("RCPT")["next_number"]
    with pytest.raises(SeriesNotSetUp) as err:
        BM.next_document_number("RCPT", facility_id=9732)
    assert (err.value.series_code, err.value.facility_id) == ("RCPT", 9732)
    assert _row("RCPT")["next_number"] == before
    with db_session(immediate=True) as db:
        with pytest.raises(SeriesNotSetUp):
            BM.allocate_series_number(db, "RCPT", facility_id=9732)
    # Facility 1's refusal for an unknown code is the one it always was.
    with pytest.raises(BM.MasterDataError) as unknown:
        BM.next_document_number("NO-SUCH-SERIES")
    assert not isinstance(unknown.value, SeriesNotSetUp)
    assert "migration 20 seeds" in str(unknown.value)


def test_allocate_series_number_takes_the_facility():
    _add_series(9733, "PO", "F9733/PO-", next_number=3, financial_year=_fy(),
                document_type="purchase_order")
    with db_session(immediate=True) as db:
        out = BM.allocate_series_number(db, "PO", facility_id=9733)
    assert out["number"] == f"F9733/PO-{_fy()}-000003"


# ═════════════════════════════════════════════════════════════════════════════
#  3. The callers
# ═════════════════════════════════════════════════════════════════════════════

def test_a_second_facilitys_receipt_refund_and_credit_note_use_its_series(second_facility):
    fid = second_facility["id"]
    pid = _patient_at(fid)
    with pytest.raises(ps.PaymentError, match="no receipt number series yet"):
        ps.record_payment(patient_id=pid, amount_paise=rupees("100"), method="cash",
                          purpose="advance", received_by="clerk-ds")
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM payments WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0
    _add_series(fid, "RCPT", f"{second_facility['code']}/R-", financial_year=_fy(),
                document_type="receipt")
    receipt = ps.record_payment(patient_id=pid, amount_paise=rupees("100"), method="cash",
                                purpose="advance", received_by="clerk-ds")
    assert receipt["receipt_number"] == f"{second_facility['code']}/R-{_fy()}-000001"
    _add_series(fid, "REF", f"{second_facility['code']}/F-", financial_year=_fy(),
                document_type="refund")
    refund = ps.request_refund(patient_id=pid, amount_paise=rupees("50"), reason="Overpaid",
                               requested_by="clerk-ds")
    assert refund["refund_number"].startswith(f"{second_facility['code']}/F-")

    invoice = _issued_invoice(pid)
    with pytest.raises(billing_service.BillingError) as err:
        billing_service.create_credit_note(invoice["id"], reason="Billed in error")
    assert err.value.code == "series_not_set_up"
    _add_series(fid, "INV", f"{second_facility['code']}/I-", financial_year=_fy())
    note = billing_service.create_credit_note(invoice["id"], reason="Billed in error")
    assert note["invoice_number"] == f"{second_facility['code']}/I-{_fy()}-000001"


def test_a_second_facilitys_claim_uses_its_claim_series(second_facility):
    fid = second_facility["id"]
    pid = _patient_at(fid)
    code = f"TPA{uuid.uuid4().hex[:5].upper()}"
    payer_service.upsert_payer(code=code, name="Orvanne Health TPA", payer_type="tpa")
    with pytest.raises(payer_service.ClaimError, match="no claim number series yet"):
        payer_service.create_claim(patient_id=pid, payer_code=code, created_by="DS-ADM")
    _add_series(fid, "CLM", f"{second_facility['code']}/C-", financial_year=_fy(),
                document_type="claim")
    claim = payer_service.create_claim(patient_id=pid, payer_code=code, created_by="DS-ADM")
    assert claim["claim_number"] == f"{second_facility['code']}/C-{_fy()}-000001"


def test_facility_ones_patients_and_unattributed_documents_keep_facility_ones_series(
        second_facility):
    """With two facilities on record, a facility 1 patient's receipt still
    numbers from facility 1, and so do documents with no facility yet."""
    pid = _patient_at(1)
    before = _row("RCPT")["next_number"]
    receipt = ps.record_payment(patient_id=pid, amount_paise=rupees("100"), method="cash",
                                purpose="advance", received_by="clerk-ds")
    assert receipt["receipt_number"].startswith(_row("RCPT")["prefix"])
    assert _row("RCPT")["next_number"] == before + 1
    import inspect
    from services import dietary_service, nabh_service, procurement_service
    for module in (procurement_service, dietary_service, nabh_service):
        assert "facility_id=MAIN_FACILITY_ID" in inspect.getsource(module)


def test_the_print_reads_the_configured_flag_of_the_suppliers_facility(second_facility):
    fid = second_facility["id"]
    pid = _patient_at(fid)
    invoice = _issued_invoice(pid)
    _add_series(fid, "INV", f"{second_facility['code']}/I-", financial_year=_fy())
    with db_session() as db:
        db.execute("UPDATE document_number_series SET is_configured = 1 WHERE facility_id = ? "
                   "AND series_code = 'INV'", (fid,))
        db.execute("UPDATE document_number_series SET is_configured = 0 WHERE facility_id = 1 "
                   "AND series_code = 'INV'")
    view = billing_service.invoice_print_view(invoice["id"])
    assert view["supplier"]["facility_id"] == fid
    assert view["number_series_configured"] is True


def test_the_invoice_and_receipt_number_indexes_still_hold():
    with db_session() as db:
        unique = {r[0]: r[1] for r in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name IN "
            "('idx_invoices_number', 'idx_payments_receipt')")}
    assert set(unique) == {"idx_invoices_number", "idx_payments_receipt"}
    assert all("UNIQUE" in (sql or "").upper() for sql in unique.values())


# ═════════════════════════════════════════════════════════════════════════════
#  4. The prefix rule
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("prefix", ["INV-", "INV", "INV-F2/", "I"])
def test_a_second_facility_prefix_equal_to_or_overlapping_facility_ones_is_refused(
        second_facility, prefix):
    with pytest.raises(SeriesEditError) as err:
        BM.save_document_series(second_facility["id"], "INV", {"prefix": prefix})
    assert {e["code"] for e in err.value.errors} >= {"prefix_clash"}
    assert _row("INV", second_facility["id"]) is None


def test_a_prefix_that_begins_an_issued_number_is_refused_on_set_up_and_on_edit(second_facility):
    with db_session() as db:
        db.execute("UPDATE document_number_series SET prefix = 'OLDQX/' WHERE facility_id = 1 "
                   "AND series_code = 'INV'")
    old = _issued_invoice(_patient_at(1))
    assert old["invoice_number"].startswith("OLDQX/")
    with db_session() as db:
        db.execute("UPDATE document_number_series SET prefix = 'INV-' WHERE facility_id = 1 "
                   "AND series_code = 'INV'")
    with pytest.raises(SeriesEditError) as err:
        BM.save_document_series(second_facility["id"], "INV", {"prefix": "OLDQX/"})
    assert [e["code"] for e in err.value.errors] == ["prefix_matches_issued"]
    assert old["invoice_number"] in err.value.errors[0]["message"]
    # On an edit too: facility 1 going back to a prefix its issued bills begin with.
    with pytest.raises(SeriesEditError) as again:
        BM.save_document_series(1, "INV", {"prefix": "OLDQX/"})
    assert [e["code"] for e in again.value.errors] == ["prefix_matches_issued"]
    assert _row("INV")["prefix"] == "INV-"


def test_an_edit_that_overlaps_another_facilitys_prefix_is_refused(second_facility):
    fid = second_facility["id"]
    made = BM.save_document_series(fid, "INV", {"prefix": "ZQ2/"})
    assert made["created"] is True
    with pytest.raises(SeriesEditError) as err:
        BM.save_document_series(1, "INV", {"prefix": "ZQ"})
    assert {e["code"] for e in err.value.errors} == {"prefix_clash"}
    with pytest.raises(SeriesEditError):
        BM.save_document_series(fid, "INV", {"prefix": "INV-ZQ/"})
    assert _row("INV", fid)["prefix"] == "ZQ2/"


def test_series_writing_one_register_keep_their_prefixes_apart():
    """Stores and pharmacy purchase orders share purchase_orders.po_number."""
    with pytest.raises(SeriesEditError) as err:
        BM.save_document_series(1, "PHPO", {"prefix": "PO-PH/"})
    assert {e["code"] for e in err.value.errors} == {"prefix_clash"}
    assert "Purchase orders" in err.value.errors[0]["message"]


def test_an_edit_that_leaves_the_prefix_alone_is_never_refused_and_is_audited():
    with db_session() as db:
        since = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0])
    out = BM.save_document_series(1, "KIND", {"pad_width": 7, "is_configured": True},
                                  actor={"id": "DS-ADM", "role": "admin"})
    assert out["changed"] == ["is_configured", "pad_width"]
    assert out["series"]["is_configured"] is True and out["series"]["pad_width"] == 7
    sequence = out["series"]["next_number_preview"].rsplit("-", 1)[-1]
    assert len(sequence) == 7 and sequence.isdigit()
    with db_session() as db:
        rows = db.execute("SELECT * FROM audit_logs WHERE id > ? AND action_type = "
                          "'document_series_updated'", (since,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["event_category"] == "site_admin" and rows[0]["target_id"] == "1:KIND"
    assert BM.save_document_series(1, "KIND", {"pad_width": 7})["changed"] == []


def test_what_cannot_be_set_up_or_changed(second_facility):
    fid = second_facility["id"]
    with pytest.raises(SeriesEditError) as group:
        BM.save_document_series(fid, "UHID", {"prefix": "X9/"})
    assert group.value.code == "group_wide_series" and group.value.status == 409
    with pytest.raises(SeriesEditError) as missing:
        BM.save_document_series(fid, "RCPT", {"pad_width": 6})
    assert {e["code"] for e in missing.value.errors} == {"prefix_required"}
    with pytest.raises(SeriesEditError) as fields:
        BM.save_document_series(1, "INV", {"next_number": 1, "prefix": "bad prefix"})
    assert {e["code"] for e in fields.value.errors} == {"not_editable", "bad_prefix"}
    with pytest.raises(SeriesEditError) as nowhere:
        BM.save_document_series(9739, "INV", {"prefix": "Q/"})
    assert nowhere.value.status == 404


# ═════════════════════════════════════════════════════════════════════════════
#  5. The routes
# ═════════════════════════════════════════════════════════════════════════════

def _user(role: str, department=None) -> tuple:
    uid = f"DS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role} {uid}", role, department, hash_password("password")))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture()
def client(monkeypatch):
    from main import app
    monkeypatch.setattr(facility_fence, "FACILITY_BLIND_SURFACES", ())
    return TestClient(app)


def test_the_series_routes_are_off_with_the_switch_off(client):
    _, hdr = _user("admin")
    got = client.get("/api/facilities/1/document-series", headers=hdr)
    assert got.status_code == 404 and got.json()["detail"]["error"] == "feature_disabled"
    put = client.put("/api/facilities/1/document-series/INV", headers=hdr, json={"pad_width": 6})
    assert put.status_code == 404


def test_the_series_routes_with_the_switch_on(client, switch_on, second_facility):
    fid = second_facility["id"]
    uid, hdr = _user("admin")
    _, nurse = _user("nurse", "General Medicine")
    listed = client.get("/api/facilities/1/document-series", headers=hdr)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["can_edit"] is True and body["can_add"] == []
    inv = next(s for s in body["series"] if s["series_code"] == "INV")
    assert inv["label"] == "Invoices and credit notes" and inv["next_number_preview"]
    other = client.get(f"/api/facilities/{fid}/document-series", headers=hdr).json()
    assert other["series"] == [] and other["can_edit"] is False
    assert "UHID" not in {s["series_code"] for s in other["can_add"]}
    assert "INV" in {s["series_code"] for s in other["can_add"]}
    assert client.get("/api/facilities/9738/document-series", headers=hdr).status_code == 404

    assert client.put(f"/api/facilities/{fid}/document-series/INV", headers=nurse,
                      json={"prefix": "N/"}).status_code == 403
    refused = client.put(f"/api/facilities/{fid}/document-series/INV", headers=hdr,
                         json={"prefix": "VQ/"})
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "not_your_facility"

    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute("INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
                   "VALUES (?, 'test', ?, 'series route test')", (uid, now))
    FS.invalidate_cache()
    try:
        bad = client.put(f"/api/facilities/{fid}/document-series/INV", headers=hdr,
                         json={"prefix": "INV"})
        assert bad.status_code == 400
        assert bad.json()["detail"]["errors"][0]["code"] == "prefix_clash"
        extra = client.put(f"/api/facilities/{fid}/document-series/INV", headers=hdr,
                           json={"prefix": "VQ/", "next_number": 1})
        assert extra.status_code == 422
        made = client.put(f"/api/facilities/{fid}/document-series/INV", headers=hdr,
                          json={"prefix": "VQ/", "is_configured": True})
        assert made.status_code == 200, made.text
        assert made.json()["created"] is True
        assert made.json()["series"]["next_number_preview"] == f"VQ/{_fy()}-000001"
        with db_session() as db:
            row = db.execute("SELECT * FROM audit_logs WHERE action_type = "
                             "'document_series_created' AND target_id = ?",
                             (f"{fid}:INV",)).fetchone()
        assert row is not None and row["event_category"] == "site_admin"
    finally:
        with db_session() as db:
            db.execute("DELETE FROM enterprise_access_grants WHERE user_id = ?", (uid,))
        FS.invalidate_cache()
