"""Migration 50: the registration and outpatient schema foundation (opd-1).

What this defends:
  * the migration is idempotent on a fresh database, on one where the front
    desk had already created opd_visits lazily (with rows), and run twice;
  * opd_visits exists after init_db alone, before the front desk's lazy
    fallback ever runs, and the fallback builds the same shape;
  * the identity change log is append-only in the database (UPDATE and DELETE
    refused), needs a reason, and an identity document is never stored beyond
    its last four characters;
  * the UHID series is seeded unconfigured and never resets, and every existing
    series keeps its yearly reset;
  * with registration and OPD off, the front desk's API returns visits exactly
    as before: none of the new columns reaches a screen.
"""
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import migrations  # noqa: F401  (migrations.py first: it collects migrations_his in order)
from database import db_session, get_db
from db_backend import DBIntegrityError
from migrations_his import m0050_registration_opd_foundation as m50
from services import frontdesk_service as fd

SERVER_DIR = Path(__file__).resolve().parent.parent

NEW_TABLES = (
    "patient_demographics", "patient_identity_changes", "patient_duplicate_candidates",
    "patient_match_keys", "opd_display_boards",
)

# The keys a visit carried in the API before migration 50, in order.
LEGACY_VISIT_KEYS = list(m50.OPD_VISITS_BASE_COLUMNS) + [
    "patient_name", "patient_mrn", "patient_age", "patient_sex",
    "waiting_minutes", "consultation_minutes",
]


def _schema(conn) -> list:
    return sorted(tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall())


def _minimal_pre_50_db(path):
    """The tables migration 50 alters, in their pre-50 shapes, with a row in each."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE admissions (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, "
                 "status TEXT DEFAULT 'active')")
    conn.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, "
                 "description TEXT NOT NULL)")
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
    conn.execute("INSERT INTO admissions (id, patient_id) VALUES ('ADM1', 'P1')")
    conn.execute("INSERT INTO orders (id, patient_id, description) VALUES ('O1', 'P1', 'CBC')")
    conn.execute("INSERT INTO document_number_series (series_code, document_type, prefix, "
                 "next_number, is_configured) VALUES ('INV', 'invoice', 'INV-', 42, 1)")
    conn.commit()
    return conn


def _apply_in_runner_txn(conn):
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    m50.apply(conn)
    conn.execute("COMMIT")


# ── The migration on its own ────────────────────────────────────────────────

def test_migration_50_on_a_pre_50_database_and_run_twice(tmp_path):
    conn = _minimal_pre_50_db(tmp_path / "pre50.db")
    _apply_in_runner_txn(conn)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"opd_visits", *NEW_TABLES} <= tables
    assert "patient_movements" not in tables, "migration 51 owns the movement ledger"
    cols = [r[1] for r in conn.execute("PRAGMA table_info(opd_visits)")]
    assert cols == list(m50.OPD_VISITS_BASE_COLUMNS) + [n for n, _ in m50.OPD_VISITS_ADDED_COLUMNS]
    assert "opd_visit_id" in {r[1] for r in conn.execute("PRAGMA table_info(admissions)")}
    assert "opd_visit_id" in {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='idx_orders_opd_visit'"
                        ).fetchone()
    # Existing rows untouched; the existing series keeps its yearly reset.
    assert conn.execute("SELECT id, patient_id, opd_visit_id FROM admissions").fetchall() == [
        ("ADM1", "P1", None)]
    assert conn.execute(
        "SELECT next_number, is_configured, resets_yearly, check_digit FROM "
        "document_number_series WHERE series_code='INV'").fetchone() == (42, 1, 1, None)
    assert conn.execute(
        "SELECT document_type, prefix, next_number, is_configured, resets_yearly, check_digit "
        "FROM document_number_series WHERE series_code='UHID'").fetchone() == (
        "uhid", "", 1, 0, 0, "verhoeff")

    snapshot = _schema(conn)
    rows = conn.execute("SELECT * FROM document_number_series ORDER BY id").fetchall()
    _apply_in_runner_txn(conn)
    assert _schema(conn) == snapshot
    assert conn.execute("SELECT * FROM document_number_series ORDER BY id").fetchall() == rows
    conn.close()


def test_migration_50_keeps_a_lazily_created_opd_visits_table_and_its_rows(tmp_path):
    conn = _minimal_pre_50_db(tmp_path / "lazy.db")
    conn.execute(m50.OPD_VISITS_DDL)
    for statement in m50.OPD_VISITS_INDEXES:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
        "registered_at, status) VALUES ('OPD-1', 'P1', 'Cardiology', '2026-10-01', 4, "
        "'2026-10-01T04:00:00+00:00', 'completed')")
    conn.commit()
    _apply_in_runner_txn(conn)
    row = conn.execute(
        "SELECT id, token_number, status, priority, arrival_source, call_count, "
        "scope_self_registered, disposition, cancel_reason_code, identity_check_json "
        "FROM opd_visits").fetchone()
    assert row == ("OPD-1", 4, "completed", "normal", "walk_in", 0, 0, None, None, None)
    conn.close()


def test_the_identity_change_log_is_append_only_and_needs_a_reason(tmp_path):
    conn = _minimal_pre_50_db(tmp_path / "idlog.db")
    _apply_in_runner_txn(conn)
    conn.execute(
        "INSERT INTO patient_identity_changes (patient_id, field, old_value, new_value, "
        "reason, changed_by, changed_at) VALUES ('P1', 'name', 'Ramesh', 'Rakesh', "
        "'Spelling corrected from Aadhaar card', 'R1', '2026-10-04T05:00:00+00:00')")
    with pytest.raises(DBIntegrityError, match="append-only"):
        conn.execute("UPDATE patient_identity_changes SET new_value = 'Mahesh'")
    with pytest.raises(DBIntegrityError, match="append-only"):
        conn.execute("DELETE FROM patient_identity_changes")
    with pytest.raises(DBIntegrityError):
        conn.execute(
            "INSERT INTO patient_identity_changes (patient_id, field, reason, changed_by, "
            "changed_at) VALUES ('P1', 'phone', '   ', 'R1', '2026-10-04T05:00:00+00:00')")
    assert conn.execute("SELECT new_value FROM patient_identity_changes").fetchall() == [("Rakesh",)]
    conn.close()


def test_an_identity_document_is_stored_as_its_last_four_only(tmp_path):
    conn = _minimal_pre_50_db(tmp_path / "doc.db")
    _apply_in_runner_txn(conn)
    conn.execute("INSERT INTO patient_demographics (patient_id, id_document_type, "
                 "id_document_last4) VALUES ('P1', 'aadhaar', '1234')")
    with pytest.raises(DBIntegrityError):
        conn.execute("INSERT INTO patient_demographics (patient_id, id_document_type, "
                     "id_document_last4) VALUES ('P2', 'aadhaar', '123456789012')")
    with pytest.raises(DBIntegrityError):
        conn.execute("INSERT INTO patient_demographics (patient_id, registration_source) "
                     "VALUES ('P3', 'walk_by')")
    conn.close()


def test_a_duplicate_pair_is_one_row_in_one_order(tmp_path):
    conn = _minimal_pre_50_db(tmp_path / "dup.db")
    _apply_in_runner_txn(conn)
    conn.execute("INSERT INTO patient_duplicate_candidates (patient_id, candidate_patient_id, "
                 "score, source, detected_at) VALUES ('PA', 'PB', 80, 'registration', 'now')")
    with pytest.raises(DBIntegrityError):  # same pair again
        conn.execute("INSERT INTO patient_duplicate_candidates (patient_id, "
                     "candidate_patient_id, score, source, detected_at) "
                     "VALUES ('PA', 'PB', 75, 'sweep', 'now')")
    with pytest.raises(DBIntegrityError):  # the reverse order is refused
        conn.execute("INSERT INTO patient_duplicate_candidates (patient_id, "
                     "candidate_patient_id, score, source, detected_at) "
                     "VALUES ('PB', 'PA', 80, 'registration', 'now')")
    conn.close()


# ── The session database (init_db ran migration 50) ─────────────────────────

def test_the_session_database_ran_migration_50():
    with db_session() as db:
        assert 50 in {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"opd_visits", *NEW_TABLES} <= tables
        visit_cols = {r[1] for r in db.execute("PRAGMA table_info(opd_visits)")}
        assert {n for n, _ in m50.OPD_VISITS_ADDED_COLUMNS} <= visit_cols
        assert {"cancel_reason_code", "identity_check_json", "scope_self_registered"} <= visit_cols
        uhid = db.execute(
            "SELECT is_configured, resets_yearly, check_digit FROM document_number_series "
            "WHERE series_code = 'UHID'").fetchone()
        assert tuple(uhid) == (0, 0, "verhoeff")
        others = db.execute(
            "SELECT COUNT(*) FROM document_number_series WHERE series_code <> 'UHID' "
            "AND (resets_yearly <> 1 OR check_digit IS NOT NULL)").fetchone()[0]
        assert others == 0, "an existing series lost its yearly reset"


def test_the_session_database_refuses_an_identity_change_edit():
    """The same triggers on the real schema; the inserted row is rolled back."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO patient_identity_changes (patient_id, field, reason, changed_by, "
            "changed_at) VALUES ('PIC-ROLLBACK', 'name', 'test', 'R1', 'now')")
        with pytest.raises(Exception, match="append-only"):
            conn.execute("UPDATE patient_identity_changes SET reason = 'x' "
                         "WHERE patient_id = 'PIC-ROLLBACK'")
        with pytest.raises(Exception, match="append-only"):
            conn.execute("DELETE FROM patient_identity_changes WHERE patient_id = 'PIC-ROLLBACK'")
    finally:
        conn.rollback()
        conn.close()
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patient_identity_changes "
                          "WHERE patient_id = 'PIC-ROLLBACK'").fetchone()[0] == 0


def test_the_front_desk_fallback_builds_the_migrations_shape(tmp_path):
    """ensure_frontdesk_schema shares migration 50's DDL: on an empty database
    the lazy path and the runner produce the same opd_visits."""
    lazy = sqlite3.connect(str(tmp_path / "lazy_only.db"))
    m50.ensure_opd_visits_on(lazy)
    lazy.commit()
    with db_session() as db:
        runner = [tuple(r) for r in db.execute("PRAGMA table_info(opd_visits)")]
        runner_idx = sorted(r[0] for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='opd_visits' AND type='index' "
            "AND sql IS NOT NULL"))
    assert [tuple(r) for r in lazy.execute("PRAGMA table_info(opd_visits)")] == runner
    assert sorted(r[0] for r in lazy.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='opd_visits' AND type='index' "
        "AND sql IS NOT NULL")) == runner_idx
    assert fd.ensure_opd_visits_on is m50.ensure_opd_visits_on
    lazy.close()


def test_retention_review_lists_the_new_phi_tables():
    from services.consent_service import retention_review
    review = retention_review()
    listed = {t for c in review["categories"] for t in c["by_table"]}
    assert {"opd_visits", "patient_demographics", "patient_identity_changes",
            "patient_match_keys"} <= listed
    assert review["deleted_anything"] is False


def test_init_db_alone_creates_opd_visits_before_the_front_desk_runs(tmp_path):
    """A brand new database: init_db() and nothing else. opd_visits must exist
    in its full shape, with the front desk's lazy path never having run."""
    db_path = tmp_path / "fresh.db"
    body = """
import json
from database import init_db, get_db
from services import frontdesk_service as fd
init_db()
conn = get_db()
print("ORB_TEST_JSON:" + json.dumps({
    "lazy_ran": fd._schema_ready,
    "opd_cols": [r[1] for r in conn.execute("PRAGMA table_info(opd_visits)")],
    "tables": sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")),
    "applied": sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations")),
}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", body], cwd=str(SERVER_DIR), capture_output=True, text=True,
        timeout=300,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
             "ORB_DEPLOYMENT_PROFILE": "dev", "ORB_DB_PATH": str(db_path)})
    assert proc.returncode == 0, proc.stderr
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("ORB_TEST_JSON:")][-1]
    got = json.loads(line[len("ORB_TEST_JSON:"):])
    assert got["lazy_ran"] is False
    assert got["opd_cols"] == list(m50.OPD_VISITS_BASE_COLUMNS) + [
        n for n, _ in m50.OPD_VISITS_ADDED_COLUMNS]
    assert {"opd_visits", "patient_movements", *NEW_TABLES} <= set(got["tables"])
    assert {50, 51, 52} <= set(got["applied"])


# ── With registration and OPD off, the front desk's API is unchanged ────────

@pytest.fixture(scope="module")
def clinic():
    dept = f"W2A Clinic {uuid.uuid4().hex[:6]}"
    doctor = f"W2AD{uuid.uuid4().hex[:6].upper()}"
    pid = f"W2AP{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, 'Dr Opd Foundation', 'doctor', 'x', ?, 1)", (doctor, dept))
        db.execute("INSERT INTO patients (id, name, mrn, age, sex, status) "
                   "VALUES (?, 'Opd Foundation Patient', ?, 50, 'F', 'discharged')",
                   (pid, f"MRN-{pid}"))
    return {"dept": dept, "doctor": doctor, "pid": pid}


def test_a_registered_visit_carries_the_old_keys_only(clinic):
    visit = fd.register_opd_visit(patient_id=clinic["pid"], department=clinic["dept"],
                                  doctor_id=clinic["doctor"], registered_by=clinic["doctor"])
    assert list(visit) == LEGACY_VISIT_KEYS
    started = fd.start_opd_consultation(visit["id"], called_by=clinic["doctor"])
    assert list(started) == LEGACY_VISIT_KEYS
    assert list(fd.get_opd_visit(visit["id"])) == LEGACY_VISIT_KEYS
    cancelled = fd.cancel_opd_visit(
        fd.register_opd_visit(patient_id=clinic["pid"], department=clinic["dept"])["id"],
        reason="Left before being called")
    assert list(cancelled) == LEGACY_VISIT_KEYS


def test_the_queue_carries_the_old_keys_only(clinic):
    fd.register_opd_visit(patient_id=clinic["pid"], department=clinic["dept"],
                          doctor_id=clinic["doctor"])
    queue = fd.list_opd_queue(department=clinic["dept"])
    visits = [v for q in queue["queues"] for v in q["visits"]]
    assert visits
    for v in visits:
        assert list(v) == LEGACY_VISIT_KEYS
