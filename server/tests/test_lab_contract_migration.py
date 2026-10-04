"""Migrations 55 (lab_contract) and 56 (observations_lab_links), lis-3 and lis-5b.

Pinned here:
  * both files are collected by version and name, and the runner holds them;
  * every lab_ table exists after init_db, with no FOREIGN KEY and no REAL
    column anywhere (values are thousandths in INTEGER columns);
  * each migration is idempotent on a fresh database and on a legacy one,
    leaves existing rows alone, and skips an index whose table lacks a column
    rather than failing boot;
  * the constraints the later increments rely on: one live result per order
    item, one open critical notification per result or observation, a lane
    needs its key, and the event tables refuse an UPDATE but allow a DELETE;
  * observations gains order_id, specimen_id, lab_result_id and superseded_by,
    every existing row reads NULL in all four and stays live, and a row whose
    superseded_by is set leaves every reader;
  * the restore verifier's manifest and the retention map name the new tables.
"""
from __future__ import annotations

import sqlite3

import pytest

import migrations
import migrations_his
from database import db_session
from migrations_his import m0055_lab_contract as m55
from migrations_his import m0056_observations_lab_links as m56

LAB_TABLES = set(m55.TABLE_NAMES)
OBS_LINK_COLUMNS = {"order_id", "specimen_id", "lab_result_id", "superseded_by"}


def _tables(db) -> set:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(db, table) -> dict:
    return {r[1]: (r[2] or "").upper() for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


# ── 1. Discovery and the runner ───────────────────────────────────────────────

def test_both_migrations_are_collected_with_their_names():
    found = {m.version: m.name for m in migrations_his.collect()}
    assert found[55] == "lab_contract"
    assert found[56] == "observations_lab_links"
    by_version = {m.version: m.name for m in migrations.MIGRATIONS}
    assert by_version[55] == "lab_contract"
    assert by_version[56] == "observations_lab_links"


def test_the_session_database_has_both_in_its_ledger():
    with db_session() as db:
        applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}
    assert {55, 56} <= applied


# ── 2. Introspection on the migrated database ─────────────────────────────────

def test_every_lab_table_exists_after_init_db():
    with db_session() as db:
        assert LAB_TABLES <= _tables(db)
    assert len(LAB_TABLES) == 17


def test_no_lab_table_has_a_foreign_key_or_a_real_column():
    with db_session() as db:
        for table in sorted(LAB_TABLES):
            assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall() == [], table
            for name, decl in _columns(db, table).items():
                assert not any(word in decl for word in ("REAL", "FLOA", "DOUB")), (table, name, decl)


def test_no_lab_column_defaults_to_current_timestamp():
    """Instants are written in the one canonical spelling by the writer."""
    with db_session() as db:
        for table in sorted(LAB_TABLES):
            ddl = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
            assert "CURRENT_TIMESTAMP" not in ddl.upper(), table


def test_observations_gained_the_four_link_columns():
    with db_session() as db:
        cols = _columns(db, "observations")
        assert OBS_LINK_COLUMNS <= set(cols)
        assert cols["lab_result_id"] == "INTEGER" and cols["superseded_by"] == "INTEGER"
        assert cols["order_id"] == "TEXT" and cols["specimen_id"] == "TEXT"
        indexes = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='observations'")}
        assert {"idx_observations_order", "idx_observations_lab_result",
                "idx_observations_patient_code_time"} <= indexes


def test_the_retention_map_names_columns_the_new_tables_really_have():
    from services.consent_service import _RETENTION_SOURCES
    with db_session() as db:
        for rows in _RETENTION_SOURCES.values():
            for table, pcol, tcol in rows:
                if table in LAB_TABLES:
                    cols = _columns(db, table)
                    assert tcol in cols, (table, tcol)
                    assert pcol is None or pcol in cols, (table, pcol)
    registered = {t for rows in _RETENTION_SOURCES.values() for t, _, _ in rows}
    assert {"lab_specimens", "lab_specimen_events", "lab_order_items", "lab_results",
            "lab_result_events", "lab_critical_notifications", "lab_critical_events"} <= registered


def test_the_restore_manifest_lists_every_lab_table():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "verify_restore.py"
    spec = importlib.util.spec_from_file_location("_vr_for_lab_test", path)
    vr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vr)
    assert set(vr.MODULE_TABLES["laboratory"]) == LAB_TABLES


# ── 3. Idempotent, fresh and legacy ───────────────────────────────────────────

_LEGACY_OBSERVATIONS = """CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT,
    source TEXT NOT NULL DEFAULT 'unknown',
    observation_code TEXT,
    observation_name TEXT NOT NULL,
    value_text TEXT,
    value_numeric REAL,
    unit TEXT,
    status TEXT DEFAULT 'final',
    observed_at TIMESTAMP,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)"""


@pytest.fixture
def scratch(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_both_apply_twice_on_a_legacy_database_and_keep_its_rows(scratch):
    scratch.execute(_LEGACY_OBSERVATIONS)
    scratch.execute("INSERT INTO observations (patient_id, observation_code, observation_name, "
                    "value_numeric, unit, observed_at) VALUES "
                    "('P1', '2823-3', 'Potassium', 4.1, 'mmol/L', '2026-10-01 08:00:00')")
    before = dict(scratch.execute("SELECT * FROM observations").fetchone())
    for _ in range(2):
        m55.apply(scratch)
        m56.apply(scratch)
    after = dict(scratch.execute("SELECT * FROM observations").fetchone())
    assert {k: after[k] for k in before} == before
    assert all(after[c] is None for c in OBS_LINK_COLUMNS)
    assert LAB_TABLES <= _tables(scratch)
    for table in LAB_TABLES:
        assert scratch.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert scratch.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_an_existing_odd_lab_table_is_left_alone_and_its_index_skipped(scratch):
    scratch.execute(_LEGACY_OBSERVATIONS)
    scratch.execute("CREATE TABLE lab_tests (id TEXT PRIMARY KEY, name TEXT)")
    m55.apply(scratch)
    assert set(_columns(scratch, "lab_tests")) == {"id", "name"}
    indexes = {r[0] for r in scratch.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='lab_tests'")}
    assert "idx_lab_tests_section" not in indexes


def test_migration_56_without_an_observations_table_changes_nothing(scratch):
    m56.apply(scratch)
    assert "observations" not in _tables(scratch)


# ── 4. The constraints later increments rely on ───────────────────────────────

NOW = "2026-10-04T06:00:00.000000+00:00"


@pytest.fixture
def lab_db(scratch):
    scratch.execute(_LEGACY_OBSERVATIONS)
    m55.apply(scratch)
    m56.apply(scratch)
    return scratch


def _result(db, item, status="entered"):
    db.execute("INSERT INTO lab_results (order_item_id, order_id, patient_id, test_id, "
               "entered_at, status) VALUES (?, 'O1', 'P1', 'LOINC:2823-3', ?, ?)",
               (item, NOW, status))


def test_one_live_result_per_order_item(lab_db):
    _result(lab_db, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _result(lab_db, 1)
    lab_db.execute("UPDATE lab_results SET status = 'superseded' WHERE order_item_id = 1")
    _result(lab_db, 1)  # a correction after the original was superseded
    _result(lab_db, 2, status="withdrawn")
    _result(lab_db, 2)


def test_one_open_critical_per_result_and_per_observation(lab_db):
    def raise_(nid, lane, result_id=None, obs_id=None, status="open"):
        lab_db.execute(
            "INSERT INTO lab_critical_notifications (id, source_lane, result_id, observation_id, "
            "patient_id, test_name, value_display, raised_at, status) "
            "VALUES (?, ?, ?, ?, 'P1', 'Potassium', '7.0 mmol/L', ?, ?)",
            (nid, lane, result_id, obs_id, NOW, status))
    raise_("C1", "lis", result_id=10)
    with pytest.raises(sqlite3.IntegrityError):
        raise_("C2", "lis", result_id=10)
    raise_("C3", "lis", result_id=10, status="acknowledged")
    raise_("C4", "interfaced", obs_id=99)
    with pytest.raises(sqlite3.IntegrityError):
        raise_("C5", "interfaced", obs_id=99)
    with pytest.raises(sqlite3.IntegrityError):
        raise_("C6", "lis", obs_id=5)          # an LIS notification needs its result
    with pytest.raises(sqlite3.IntegrityError):
        raise_("C7", "interfaced", result_id=5)  # an interfaced one needs its observation


@pytest.mark.parametrize("table,insert", [
    ("lab_module_events", "INSERT INTO lab_module_events (event, occurred_at) VALUES ('enabled', ?)"),
    ("lab_specimen_events", "INSERT INTO lab_specimen_events (specimen_id, patient_id, seq, "
                            "event_type, occurred_at) VALUES ('S1', 'P1', 1, 'planned', ?)"),
    ("lab_result_events", "INSERT INTO lab_result_events (result_id, patient_id, event_type, "
                          "occurred_at) VALUES (1, 'P1', 'entered', ?)"),
    ("lab_critical_events", "INSERT INTO lab_critical_events (notification_id, patient_id, "
                            "event_type, occurred_at) VALUES ('C1', 'P1', 'raised', ?)"),
])
def test_event_tables_refuse_an_update_and_allow_a_delete(lab_db, table, insert):
    lab_db.execute(insert, (NOW,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        lab_db.execute(f"UPDATE {table} SET occurred_at = occurred_at")
    lab_db.execute(f"DELETE FROM {table}")
    assert lab_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_checks_refuse_the_values_no_writer_may_store(lab_db):
    bad = [
        "INSERT INTO lab_tests (id, code, code_system, name, short_name, created_at, updated_at) "
        "VALUES ('X:1', '1', 'SNOMED', 'n', 's', 'x', 'x')",
        "INSERT INTO lab_tests (id, code, code_system, name, short_name, decimal_places, "
        "created_at, updated_at) VALUES ('X:2', '2', 'LOCAL', 'n', 's', 4, 'x', 'x')",
        "INSERT INTO lab_tests (id, code, code_system, name, short_name, created_at, updated_at) "
        "VALUES ('X:3', '3', 'LOCAL', 'n', 'seventeen chars x', 'x', 'x')",
        "INSERT INTO lab_tests (id, code, code_system, name, short_name, performed_at, "
        "created_at, updated_at) VALUES ('X:4', '4', 'LOCAL', 'n', 's', 'theatre', 'x', 'x')",
        "INSERT INTO lab_reference_ranges (test_id, display_text, effective_from, approved_by, "
        "approved_at, method_note) VALUES ('T', 'd', 'x', 'u', 'x', 'n')",          # no bound
        "INSERT INTO lab_reference_ranges (test_id, low_milli, high_milli, display_text, "
        "effective_from, approved_by, approved_at, method_note) "
        "VALUES ('T', 5000, 4000, 'd', 'x', 'u', 'x', 'n')",                        # low above high
        "INSERT INTO lab_critical_limits (test_id, critical_high_milli, display_text, "
        "effective_from, approved_by, approved_at, method_note) "
        "VALUES ('T', 6500, 'd', 'x', 'u', 'x', 'n')",                              # no agreed_with
        "INSERT INTO lab_results (order_item_id, order_id, patient_id, test_id, entered_at, "
        "abnormal_flag) VALUES (1, 'O', 'P', 'T', 'x', 'CRIT')",
        "INSERT INTO lab_accession_counters (hospital_day, last_seq) VALUES ('261004', 100000)",
    ]
    for sql in bad:
        with pytest.raises(sqlite3.IntegrityError):
            lab_db.execute(sql)


# ── 5. superseded_by leaves every reader ──────────────────────────────────────

def test_a_superseded_observation_leaves_every_reader():
    from services import lab_observations as lo
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO patients (id, name, mrn, status) "
                   "VALUES ('PLABSUP1', 'Superseded Reader', 'MRN-LABSUP-1', 'discharged')")
        old = db.execute(
            "INSERT INTO observations (patient_id, source, observation_code, observation_name, "
            "value_numeric, unit, observed_at) VALUES ('PLABSUP1', 'lis_manual', '2823-3', "
            "'Potassium', 7.0, 'mmol/L', '2026-10-03T09:00:00.000000+00:00')").lastrowid
        new = db.execute(
            "INSERT INTO observations (patient_id, source, observation_code, observation_name, "
            "value_numeric, unit, observed_at, lab_result_id) VALUES ('PLABSUP1', 'lis_manual', "
            "'2823-3', 'Potassium', 4.0, 'mmol/L', '2026-10-03T09:00:00.000000+00:00', 2)"
        ).lastrowid
        db.execute("UPDATE observations SET superseded_by = ? WHERE id = ?", (new, old))
    try:
        with db_session() as db:
            chart = lo.chart_lab_rows(db, "PLABSUP1", per_analyte=50, max_rows=100)
            assert [r["id"] for r in chart] == [new]
            assert chart[0]["lab_result_id"] == 2
            assert [r["value_numeric"] for r in lo.latest_per_analyte(db, "PLABSUP1", limit=10)] == [4.0]
            assert [r["id"] for r in lo.recent_observations(db, "PLABSUP1", limit=10)] == [new]
            assert lo.previous_result(db, "PLABSUP1", "2823-3", "2026-10-04T00:00:00Z")["id"] == new
            assert "superseded_by IS NULL" in lo.live_observations_sql("ob")
            assert "?" not in lo.live_observations_sql("ob")
    finally:
        with db_session() as db:
            db.execute("DELETE FROM observations WHERE patient_id = 'PLABSUP1'")
            db.execute("DELETE FROM patients WHERE id = 'PLABSUP1'")


def test_an_interfaced_row_reads_lab_result_id_none_on_the_chart():
    from services import lab_observations as lo
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO patients (id, name, mrn, status) "
                   "VALUES ('PLABSUP2', 'Interfaced Reader', 'MRN-LABSUP-2', 'discharged')")
        db.execute("INSERT INTO observations (patient_id, source, observation_name, value_numeric, "
                   "unit, observed_at) VALUES ('PLABSUP2', 'ingested_hl7', 'Sodium', 139, "
                   "'mmol/L', '2026-10-03 08:00:00')")
    try:
        with db_session() as db:
            rows = lo.chart_lab_rows(db, "PLABSUP2", per_analyte=5, max_rows=10)
            assert len(rows) == 1 and rows[0]["lab_result_id"] is None
            assert set(rows[0].keys()) == {
                "id", "observation_name", "value_numeric", "unit", "reference_range",
                "abnormal_flag", "status", "source", "observed_at", "lab_result_id"}
    finally:
        with db_session() as db:
            db.execute("DELETE FROM observations WHERE patient_id = 'PLABSUP2'")
            db.execute("DELETE FROM patients WHERE id = 'PLABSUP2'")


def test_the_chart_route_carries_lab_result_id_and_drops_a_superseded_row():
    """lis-5 (part b): the chart's lab card learns which rows the laboratory
    module released, through the one reader the route already uses."""
    import uuid as _uuid
    from fastapi.testclient import TestClient
    from auth import create_token
    from main import app

    doctor = f"LCM{_uuid.uuid4().hex[:8].upper()}"
    pid = f"LCMP{_uuid.uuid4().hex[:8].upper()}"
    dept = "Lab Link Medicine"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Dr Link', 'doctor', 'x', ?)", (doctor, dept))
        db.execute("INSERT INTO patients (id, name, mrn, bed, ward, sex, status, admission_date) "
                   "VALUES (?, 'Link Patient', ?, 'LL-1', 'LL', 'F', 'admitted', "
                   "'2026-10-01 06:00:00')", (pid, f"MRN-{pid[-8:]}"))
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, 'LL', 'LL-1', 'active', ?)",
                   (f"ADM-{pid}", pid, dept, doctor))
        rows = {}
        for key, value, observed, lab_result_id in (
                ("interfaced", 4.4, "2026-10-02 06:00:00", None),
                ("old_lis", 7.0, "2026-10-03T06:00:00.000000+00:00", 11),
                ("new_lis", 6.9, "2026-10-03T06:00:00.000000+00:00", 12)):
            rows[key] = db.execute(
                "INSERT INTO observations (patient_id, source, observation_name, value_numeric, "
                "unit, reference_range, abnormal_flag, status, observed_at, lab_result_id) "
                "VALUES (?, 'lis_manual', 'Potassium', ?, 'mmol/L', '3.5-5.1', 'HH', 'final', ?, ?)",
                (pid, value, observed, lab_result_id)).lastrowid
        db.execute("UPDATE observations SET superseded_by = ? WHERE id = ?",
                   (rows["new_lis"], rows["old_lis"]))
    try:
        resp = TestClient(app).get(f"/api/vigil/patients/{pid}",
                                   headers={"Authorization": f"Bearer {create_token(doctor, 'doctor')}"})
        assert resp.status_code == 200, resp.text
        labs = resp.json()["lab_observations"]
        assert [(o["value_numeric"], o["lab_result_id"]) for o in labs] == [(4.4, None), (6.9, 12)]
    finally:
        with db_session() as db:
            db.execute("DELETE FROM observations WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
