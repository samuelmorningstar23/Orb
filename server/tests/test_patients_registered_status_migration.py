"""Migration 52: patients.status admits 'registered' (opd-2), an FK-parent rebuild.

patients is the parent of about 35 tables, so this rebuild has the users
rebuild's shape (database._atomic_table_rebuild, which commits the runner's
transaction and owns its own). What this defends:
  * on a database in today's shape, with FK children holding rows: every row
    and value kept, the column order frozen, the two migration 15 indexes
    recreated, no child row orphaned (PRAGMA foreign_key_check), the runner's
    transaction handed back, and a second run a byte-identical no-op;
  * the guards: an unknown column, index or trigger aborts before anything
    changes, and a rebuild that would orphan a child row rolls back whole;
  * database.SCHEMA, a fresh database and the migration agree on one shape;
  * a 'registered' row (a person on the index who was never admitted) is on no
    board and in no admitted-patient count, and the shadow scorecard and the
    NABH inpatient-days are identical with and without one.
"""
import importlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import migrations
from auth import create_token
from database import SCHEMA, db_session
from migrations_his import m0052_patients_status_registered as m52

OLD_STATUSES = ("admitted", "discharged", "transferred")

# The patients table exactly as every appliance has it before migration 52:
# the base SCHEMA CREATE, then init_db's on_scale_2 and migration 15's import
# columns appended by ALTER, then migration 15's two indexes.
OLD_PATIENTS_DDL = """CREATE TABLE patients (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    name TEXT NOT NULL,
    mrn TEXT UNIQUE NOT NULL,
    bed TEXT,
    ward TEXT,
    admission_date TIMESTAMP,
    diagnosis TEXT,
    status TEXT DEFAULT 'admitted' CHECK (status IN ('admitted', 'discharged', 'transferred')),
    age INTEGER,
    date_of_birth DATE,
    sex TEXT,
    phone TEXT,
    emergency_contact_name TEXT,
    emergency_contact_phone TEXT,
    address TEXT,
    attending_doctor_id TEXT REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)"""
OLD_PATIENTS_ALTERS = (
    "ALTER TABLE patients ADD COLUMN on_scale_2 INTEGER DEFAULT 0",
    "ALTER TABLE patients ADD COLUMN import_run_id TEXT",
    "ALTER TABLE patients ADD COLUMN import_source_system TEXT",
    "ALTER TABLE patients ADD COLUMN import_external_id TEXT",
    'CREATE UNIQUE INDEX idx_patients_import_external ON "patients"(import_source_system, '
    "import_external_id) WHERE import_external_id IS NOT NULL",
    'CREATE INDEX idx_patients_import_run ON "patients"(import_run_id)',
)

_DRIVERS = ["sqlite3"]
try:
    import sqlcipher3.dbapi2  # noqa: F401  (presence probe only)

    _DRIVERS.append("sqlcipher3.dbapi2")
except ImportError:
    pass


def _legacy_db(drv, path):
    """users, the pre-52 patients table, and FK children of the kinds that
    matter (admissions, vitals, medications, an audit-like trail), with rows,
    foreign_keys ON like every Orb connection."""
    conn = drv.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute(OLD_PATIENTS_DDL)
    for statement in OLD_PATIENTS_ALTERS:
        conn.execute(statement)
    conn.execute("CREATE TABLE admissions (id TEXT PRIMARY KEY, "
                 "patient_id TEXT NOT NULL REFERENCES patients(id), status TEXT)")
    conn.execute("CREATE TABLE vitals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "patient_id TEXT NOT NULL REFERENCES patients(id), heart_rate REAL)")
    conn.execute("CREATE TABLE medications (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "patient_id TEXT NOT NULL REFERENCES patients(id), name TEXT)")
    conn.execute("CREATE TABLE audit_like (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "patient_id TEXT REFERENCES patients(id), action TEXT)")
    conn.execute("INSERT INTO users (id, name) VALUES ('D1', 'Dr A')")
    conn.execute(
        "INSERT INTO patients (id, user_id, name, mrn, ward, bed, admission_date, status, age, "
        "date_of_birth, sex, phone, attending_doctor_id, on_scale_2) VALUES "
        "('P1', NULL, 'Asha', 'MRN-1', 'CCU', 'C-1', '2026-09-30 04:00:00', 'admitted', 61, "
        "'1965-02-01', 'F', '9876543210', 'D1', 1)")
    conn.execute(
        "INSERT INTO patients (id, name, mrn, status, import_run_id, import_source_system, "
        "import_external_id) VALUES ('P2', 'Bala', 'MRN-2', 'discharged', 'RUN1', 'OLDHIS', 'X9')")
    conn.execute("INSERT INTO patients (id, name, mrn, status) VALUES ('P3', 'Chitra', 'MRN-3', NULL)")
    conn.execute("INSERT INTO admissions VALUES ('ADM1', 'P1', 'active')")
    conn.execute("INSERT INTO admissions VALUES ('ADM2', 'P2', 'discharged')")
    conn.executemany("INSERT INTO vitals (patient_id, heart_rate) VALUES (?, ?)",
                     [("P1", 88.0), ("P1", 91.0), ("P2", 70.0)])
    conn.execute("INSERT INTO medications (patient_id, name) VALUES ('P1', 'ceftriaxone')")
    conn.execute("INSERT INTO audit_like (patient_id, action) VALUES ('P1', 'viewed')")
    conn.commit()
    return conn


def _all_rows(conn):
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
            for t in ("users", "patients", "admissions", "vitals", "medications", "audit_like")}


def _patients_ddl(conn):
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='patients'").fetchone()[0]


def _patients_indexes(conn):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE tbl_name='patients' AND type='index' "
        "AND sql IS NOT NULL"))


def _apply_in_runner_txn(conn):
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    m52.apply(conn)
    assert conn.in_transaction, "the rebuild must hand the runner an open transaction back"
    conn.execute("COMMIT")


# ── The rebuild on a database in today's shape ──────────────────────────────

@pytest.mark.parametrize("driver", _DRIVERS)
def test_the_rebuild_keeps_every_row_child_and_index(tmp_path, driver):
    drv = importlib.import_module(driver)
    conn = _legacy_db(drv, tmp_path / "legacy.db")
    before = _all_rows(conn)
    indexes_before = _patients_indexes(conn)
    assert m52.stored_statuses(conn) == OLD_STATUSES

    _apply_in_runner_txn(conn)

    assert set(m52.stored_statuses(conn)) == set(m52.FROZEN_STATUSES)
    assert [r[1] for r in conn.execute("PRAGMA table_info(patients)")] == list(
        m52.CANONICAL_COLUMNS)
    assert _all_rows(conn) == before, "a row or a value changed"
    assert _patients_indexes(conn) == indexes_before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "FK enforcement not restored"

    # The widened CHECK takes a registered row and still refuses the unknown.
    conn.execute("INSERT INTO patients (id, name, mrn, status) VALUES "
                 "('P4', 'Deepa', 'MRN-4', 'registered')")
    with pytest.raises(drv.IntegrityError):
        conn.execute("INSERT INTO patients (id, name, mrn, status) VALUES "
                     "('P5', 'Esha', 'MRN-5', 'deceased')")
    # Children still point at the rebuilt parent, under enforced FKs.
    with pytest.raises(drv.IntegrityError):
        conn.execute("INSERT INTO vitals (patient_id, heart_rate) VALUES ('NOPE', 60)")
    conn.execute("INSERT INTO vitals (patient_id, heart_rate) VALUES ('P4', 60)")
    # The unique import index came back and still holds.
    with pytest.raises(drv.IntegrityError):
        conn.execute("INSERT INTO patients (id, name, mrn, import_source_system, "
                     "import_external_id) VALUES ('P6', 'F', 'MRN-6', 'OLDHIS', 'X9')")

    # Re-entry is a byte-identical no-op.
    ddl = _patients_ddl(conn)
    rows = _all_rows(conn)
    _apply_in_runner_txn(conn)
    assert _patients_ddl(conn) == ddl
    assert _all_rows(conn) == rows
    conn.close()


def test_a_child_row_that_was_already_orphaned_does_not_block_the_rebuild(tmp_path):
    """Pre-existing orphans are reported by foreign_key_check before and after
    alike; the rebuild refuses only NEW ones."""
    conn = _legacy_db(sqlite3, tmp_path / "orphan.db")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO vitals (patient_id, heart_rate) VALUES ('GHOST', 50)")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert len(orphans) == 1
    _apply_in_runner_txn(conn)
    assert set(m52.stored_statuses(conn)) == set(m52.FROZEN_STATUSES)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == orphans
    conn.close()


def test_a_rebuild_that_would_orphan_a_child_rolls_back_whole(tmp_path, monkeypatch):
    conn = _legacy_db(sqlite3, tmp_path / "rollback.db")
    before = _all_rows(conn)
    ddl = _patients_ddl(conn)
    real = m52._patient_fk_violations
    calls = {"n": 0}

    def fake(c):
        calls["n"] += 1
        found = real(c)
        if calls["n"] == 2:  # the check inside the rebuild's own transaction
            found = found | {("vitals", 1, 0)}
        return found

    monkeypatch.setattr(m52, "_patient_fk_violations", fake)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="missing patients"):
        m52.apply(conn)
    assert calls["n"] == 2
    if conn.in_transaction:
        conn.execute("ROLLBACK")
    assert _patients_ddl(conn) == ddl, "patients changed although the rebuild failed"
    assert _all_rows(conn) == before
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
        "users", "patients", "admissions", "vitals", "medications", "audit_like",
        "sqlite_sequence"}
    conn.close()


def test_an_unknown_column_aborts_before_anything_changes(tmp_path):
    conn = _legacy_db(sqlite3, tmp_path / "column.db")
    conn.execute("ALTER TABLE patients ADD COLUMN blood_group TEXT")
    conn.commit()
    ddl = _patients_ddl(conn)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="blood_group"):
        m52.apply(conn)
    conn.execute("ROLLBACK")
    assert _patients_ddl(conn) == ddl
    conn.close()


@pytest.mark.parametrize("ddl", [
    "CREATE INDEX idx_patients_site_ward ON patients(ward)",
    "CREATE TRIGGER trg_site_patients AFTER INSERT ON patients BEGIN SELECT 1; END",
])
def test_an_unknown_index_or_trigger_aborts_before_anything_changes(tmp_path, ddl):
    conn = _legacy_db(sqlite3, tmp_path / "assoc.db")
    conn.execute(ddl)
    conn.commit()
    before = _patients_ddl(conn)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="associated objects"):
        m52.apply(conn)
    conn.execute("ROLLBACK")
    assert _patients_ddl(conn) == before
    conn.close()


def _pre_50_tables(conn):
    """The tables migrations 50 and 51 alter or rebuild, in pre-50 shapes."""
    conn.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL "
                 "REFERENCES patients(id), description TEXT NOT NULL)")
    conn.execute("""CREATE TABLE document_number_series (
        id INTEGER PRIMARY KEY AUTOINCREMENT, series_code TEXT NOT NULL UNIQUE,
        document_type TEXT NOT NULL, prefix TEXT NOT NULL DEFAULT '',
        suffix TEXT NOT NULL DEFAULT '', financial_year TEXT,
        pad_width INTEGER NOT NULL DEFAULT 6, next_number INTEGER NOT NULL DEFAULT 1,
        is_configured INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMP)""")
    conn.execute("INSERT INTO orders VALUES ('O1', 'P1', 'CBC')")
    conn.commit()


def test_the_runner_applies_50_51_and_52_in_one_call_with_no_transaction_open(
        tmp_path, monkeypatch):
    """The ops-script path: no transaction at entry. Migration 52 commits the
    runner's work for 50 and 51, re-opens a transaction, and all three land
    in the ledger; a second call applies nothing."""
    conn = _legacy_db(sqlite3, tmp_path / "runner.db")
    _pre_50_tables(conn)
    before = _all_rows(conn)
    keep = [m for m in migrations.MIGRATIONS if m.version in (50, 51, 52)]
    assert [m.version for m in keep] == [50, 51, 52]
    monkeypatch.setattr(migrations, "MIGRATIONS", keep)
    assert conn.in_transaction is False
    assert migrations.run_pending_migrations(conn) == [50, 51, 52]
    assert {r[0] for r in conn.execute("SELECT version FROM schema_migrations")} == {50, 51, 52}
    assert set(m52.stored_statuses(conn)) == set(m52.FROZEN_STATUSES)
    after = _all_rows(conn)
    # Migration 50 appended admissions.opd_visit_id (NULL on every old row).
    assert [row[:-1] for row in after.pop("admissions")] == before.pop("admissions")
    assert after == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrations.run_pending_migrations(conn) == []
    conn.close()


# ── One shape: database.SCHEMA, a fresh database and the migration ──────────

def test_schema_and_migration_freeze_the_same_status_list():
    import re
    match = re.search(r"CREATE TABLE IF NOT EXISTS patients \((.*?)\n\);", SCHEMA, re.S)
    assert match, "patients DDL not found in database.SCHEMA"
    check = re.search(r"CHECK \(status IN \(([^)]*)\)\)", match.group(1))
    assert tuple(re.findall(r"'([^']*)'", check.group(1))) == m52.FROZEN_STATUSES


def test_the_session_database_has_the_canonical_patients_shape(tmp_path):
    """The session database (fresh: SCHEMA plus init_db's ALTERs) and a legacy
    database rebuilt by migration 52 have the same columns, types, defaults and
    keys, so a rebuilt appliance and a new one agree column for column."""
    with db_session() as db:
        fresh = [tuple(r) for r in db.execute("PRAGMA table_info(patients)")]
        assert 52 in {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
        assert set(m52.stored_statuses(db)) == set(m52.FROZEN_STATUSES)
        fresh_indexes = sorted(r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name='patients' AND type='index' "
            "AND name NOT LIKE 'sqlite_autoindex%'"))
    # Migration 70 (the facility spine) appends registered_facility_id after
    # this frozen list, on a fresh database and a rebuilt one by the same
    # ALTER, so the two still agree column for column.
    from migrations_his import m0070_facilities_spine as m70
    later = [column for table, column, _ in m70.FACILITY_COLUMNS if table == "patients"]
    assert [r[1] for r in fresh] == list(m52.CANONICAL_COLUMNS) + later
    conn = _legacy_db(sqlite3, tmp_path / "shape.db")
    _apply_in_runner_txn(conn)
    m70.add_facility_columns(conn)
    rebuilt = [tuple(r) for r in conn.execute("PRAGMA table_info(patients)")]
    assert rebuilt == fresh
    assert sorted(m52.KNOWN_INDEXES) == fresh_indexes
    conn.close()


# ── A registered row is on no board and in no admitted count ────────────────

@pytest.fixture()
def registered_patient():
    pid = f"REG{uuid.uuid4().hex[:8].upper()}"
    yield pid
    with db_session() as db:
        db.execute("DELETE FROM patients WHERE id = ?", (pid,))


def _insert_registered(pid):
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, age, sex, status) "
                   "VALUES (?, 'Registered Only', ?, 34, 'M', 'registered')", (pid, f"UHID-{pid}"))


def _metric(text, name):
    for line in text.splitlines():
        if line.startswith(name + " "):
            return float(line.split()[-1])
    raise AssertionError(f"{name} not in metrics")


def test_a_registered_row_is_in_no_admitted_count(registered_patient):
    from services.metrics_service import render_metrics
    before = render_metrics()
    _insert_registered(registered_patient)
    after = render_metrics()
    assert _metric(after, "orb_active_admissions") == _metric(before, "orb_active_admissions")
    assert _metric(after, "orb_patients_total") == _metric(before, "orb_patients_total") + 1
    with db_session() as db:
        admitted = {r[0] for r in db.execute("SELECT id FROM patients WHERE status = 'admitted'")}
    assert registered_patient not in admitted


def test_a_registered_row_is_on_no_board(registered_patient):
    from main import app
    _insert_registered(registered_patient)
    uid = f"W2AH{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:  # an unscoped doctor sees every admitted patient
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, 'Dr Hospitalwide', 'doctor', 'x', NULL, 1)", (uid,))
    client = TestClient(app)
    resp = client.get("/api/vigil/patients?limit=500",
                      headers={"Authorization": f"Bearer {create_token(uid, 'doctor')}"})
    assert resp.status_code == 200, resp.text
    assert registered_patient not in {p["id"] for p in resp.json()}


def _strip_volatile(card: dict) -> dict:
    card = dict(card)
    card.pop("generated_at", None)
    return card


def test_the_shadow_scorecard_is_identical_with_and_without_a_registered_row(
        registered_patient):
    from services import shadow_scorecard_service as sc
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=14)
    window = [{"ward": sc._UNSPECIFIED_WARD, "start": start, "end": now}]
    with db_session() as db:
        wards_before, diag_before = sc._census_wards(db, start, now)
        beds_before = sc._occupied_bed_days(db, window, None)
    card_before = _strip_volatile(sc.scorecard(days=14, now=now))

    _insert_registered(registered_patient)

    with db_session() as db:
        wards_after, diag_after = sc._census_wards(db, start, now)
        beds_after = sc._occupied_bed_days(db, window, None)
    card_after = _strip_volatile(sc.scorecard(days=14, now=now))
    assert diag_after == diag_before
    assert wards_after == wards_before
    assert beds_after == beds_before
    assert card_after == card_before


def test_the_census_reader_would_count_the_row_without_the_exclusion(registered_patient):
    """The control: the same row with an ordinary status DOES move the
    diagnostics, so the identity above is the exclusion at work."""
    from services import shadow_scorecard_service as sc
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=14)
    with db_session() as db:
        _, before = sc._census_wards(db, start, now)
        db.execute("INSERT INTO patients (id, name, mrn, status) "
                   "VALUES (?, 'Control', ?, 'discharged')",
                   (registered_patient, f"UHID-{registered_patient}"))
        _, after = sc._census_wards(db, start, now)
    assert after["patients_scanned"] == before["patients_scanned"] + 1
    assert after["undated_admissions"] == before["undated_admissions"] + 1


def test_nabh_inpatient_days_are_identical_with_and_without_a_registered_row(
        registered_patient):
    from services import nabh_service
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    with db_session() as db:
        before = nabh_service._inpatient_days(db, start, end, None)
    _insert_registered(registered_patient)
    with db_session() as db:
        # A registered row that somehow carried an admission date must still
        # add no inpatient-day.
        db.execute("UPDATE patients SET admission_date = ?, ward = 'GEN' WHERE id = ?",
                   ((end - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), registered_patient))
    with db_session() as db:
        after = nabh_service._inpatient_days(db, start, end, None)
    assert after == before
