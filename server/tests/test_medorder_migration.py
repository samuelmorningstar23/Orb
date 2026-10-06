"""Migration 61, med_order_store (medorders-1, W4-E).

Pinned here:
  * the file is collected by version and name and the session database ran it;
  * med_orders and med_order_events exist with no FOREIGN KEY and no
    CURRENT_TIMESTAMP default, and their CHECKs hold the state machine's
    invariants;
  * the ledger refuses an UPDATE and allows a DELETE (no DELETE-abort trigger,
    ruling MO-R13 (a));
  * medications gains med_order_id (one ward row per order), held_at and
    hold_reason; helix_patient_med_orders gains med_order_id; every existing
    row reads NULL;
  * the migration is idempotent on the session database, on an empty one and
    on a legacy one holding chart and pharmacy rows, which it leaves untouched;
  * init_helix_db after the runner leaves the column and the index; init_helix_db
    alone on a legacy pharmacy table without the column still runs its whole
    script; HELIX_SCHEMA carries the column but never the index;
  * the restore verifier's manifest names both tables.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from db_backend import DBIntegrityError

import migrations
import migrations_his
from database import db_session
from migrations_his import m0061_med_order_store as m61

SERVER_DIR = Path(__file__).resolve().parent.parent
NEW_TABLES = set(m61.TABLE_NAMES)


def _tables(db) -> set:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db, table) -> dict:
    return {r[1]: (r[2] or "").upper() for r in db.execute(f"PRAGMA table_info({table})")}


def _indexes(db, table) -> set:
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?", (table,))}


def _schema(db) -> list:
    return sorted(tuple(r) for r in db.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL"))


# ── 1. Discovery and the runner ───────────────────────────────────────────────

def test_the_migration_is_collected_with_its_name():
    assert {m.version: m.name for m in migrations_his.collect()}[61] == "med_order_store"
    assert {m.version: m.name for m in migrations.MIGRATIONS}[61] == "med_order_store"


def test_the_session_database_ran_it():
    with db_session() as db:
        applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
    assert 61 in applied


# ── 2. Introspection on the migrated database ─────────────────────────────────

def test_both_tables_exist_without_foreign_keys_or_server_clock_defaults():
    with db_session() as db:
        assert NEW_TABLES <= _tables(db)
        for table in NEW_TABLES:
            assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall() == [], table
            ddl = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
            assert "CURRENT_TIMESTAMP" not in ddl.upper(), table
            assert "REFERENCES" not in ddl.upper(), table


def test_the_added_columns_are_there_and_null_on_every_existing_row():
    with db_session() as db:
        meds = _columns(db, "medications")
        assert {"med_order_id": "TEXT", "held_at": "TEXT", "hold_reason": "TEXT"}.items() \
            <= meds.items()
        assert _columns(db, "helix_patient_med_orders")["med_order_id"] == "TEXT"
        assert "ux_medications_med_order" in _indexes(db, "medications")
        assert "idx_helix_orders_med_order" in _indexes(db, "helix_patient_med_orders")
        assert {"ux_med_orders_legacy_medication", "ux_med_orders_idempotency",
                "idx_med_orders_patient_status", "idx_med_orders_cpoe"} <= _indexes(db, "med_orders")


def test_the_ledger_refuses_an_update_and_allows_a_delete():
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO med_order_events (med_order_id, patient_id, kind, created_at) "
            "VALUES ('m61-probe', 'P-m61', 'created', '2026-10-04T00:00:00.000000Z')")
        event_id = cur.lastrowid
    with pytest.raises(Exception) as exc:
        with db_session() as db:
            db.execute("UPDATE med_order_events SET reason = 'rewritten' WHERE id = ?",
                       (event_id,))
    assert "append-only" in str(exc.value)
    with db_session() as db:
        triggers = {r[0]: r[1] for r in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='med_order_events'")}
        assert set(triggers) == {m61.NO_UPDATE_TRIGGER}
        db.execute("DELETE FROM med_order_events WHERE id = ?", (event_id,))
        assert db.execute("SELECT COUNT(*) FROM med_order_events WHERE id = ?",
                          (event_id,)).fetchone()[0] == 0


def _order_row(**overrides):
    row = dict(id="m61-check", patient_id="P-m61", status="prescribed", source="chart",
               drug_name="Paracetamol", entered_by="D001", created_at="t", updated_at="t")
    row.update(overrides)
    return row


@pytest.mark.parametrize("bad", [
    {"status": "held"},                                   # held needs held_from
    {"held_from": "prescribed"},                          # held_from only while held
    {"status": "held", "held_from": "draft"},             # a hold returns to a live state
    {"status": "stopped", "projection_status": "active"},  # an ended order has no ward status
    {"status": "verified"},                               # a verified order names its verifier
    {"status": "paused"},
    {"priority": "asap"},
    {"supply_source": "shop"},
    {"encounter_type": "er"},
    {"needs_verification": 2},
    {"is_prn": 5},
])
def test_the_checks_hold_the_state_machine_invariants(bad):
    row = _order_row(**bad)
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute(f"INSERT INTO med_orders ({', '.join(row)}) VALUES "
                       f"({', '.join('?' for _ in row)})", tuple(row.values()))


def test_a_ward_row_belongs_to_at_most_one_order():
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES ('P-m61-uniq', 'Unique', "
                   "'MRN-m61-uniq')")
        for _ in range(2):
            db.execute("INSERT INTO medications (patient_id, drug_name, dosage, frequency) "
                       "VALUES ('P-m61-uniq', 'Aspirin', '75 mg', 'OD')")
        ids = [r[0] for r in db.execute(
            "SELECT id FROM medications WHERE patient_id = 'P-m61-uniq'")]
        db.execute("UPDATE medications SET med_order_id = 'same-order' WHERE id = ?", (ids[0],))
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("UPDATE medications SET med_order_id = 'same-order' WHERE id = ?",
                       (ids[1],))
    with db_session() as db:
        db.execute("DELETE FROM medications WHERE patient_id = 'P-m61-uniq'")
        db.execute("DELETE FROM patients WHERE id = 'P-m61-uniq'")


# ── 3. Idempotent: the session database, an empty one, a legacy one ───────────

def test_a_second_run_on_the_session_database_changes_nothing():
    with db_session() as db:
        before = _schema(db)
        m61.apply(db)
        assert _schema(db) == before


def _scratch(tmp_path, name):
    conn = sqlite3.connect(str(tmp_path / name))
    conn.row_factory = sqlite3.Row
    return conn


def test_an_empty_database_gets_both_tables_twice_over(tmp_path):
    conn = _scratch(tmp_path, "empty.db")
    for _ in range(2):
        conn.execute("BEGIN")
        m61.apply(conn)
        conn.commit()
    assert NEW_TABLES <= _tables(conn)
    assert "medications" not in _tables(conn) and "helix_patient_med_orders" not in _tables(conn)
    conn.close()


_LEGACY = (
    """CREATE TABLE medications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, drug_name TEXT NOT NULL,
        dosage TEXT NOT NULL, route TEXT NOT NULL DEFAULT 'Oral', frequency TEXT NOT NULL,
        status TEXT DEFAULT 'active' CHECK (status IN ('active', 'discontinued', 'prn')),
        prescribed_by TEXT, notes TEXT, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        source TEXT DEFAULT 'clinician', verified_by TEXT, verified_at TIMESTAMP)""",
    """CREATE TABLE helix_patient_med_orders (
        id TEXT PRIMARY KEY, patient_id TEXT, medication_id TEXT,
        status TEXT DEFAULT 'pending', prescribed_by TEXT, dosage TEXT, frequency TEXT,
        route TEXT, substitute_medication_id TEXT, created_at TIMESTAMP)""",
    "INSERT INTO medications (patient_id, drug_name, dosage, frequency, status, source) "
    "VALUES ('P1', 'Enoxaparin', '40 mg', 'OD', 'active', 'clinician')",
    "INSERT INTO medications (patient_id, drug_name, dosage, frequency, status, source, "
    "verified_by) VALUES ('P1', 'Amoxicillin', '500 mg', 'TDS', 'discontinued', "
    "'llm_extracted', 'PH1')",
    "INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, status) "
    "VALUES ('H1', 'P1', 'MED-AMOX', 'pending')",
)


def test_a_legacy_database_keeps_every_row_and_runs_twice(tmp_path):
    conn = _scratch(tmp_path, "legacy.db")
    for sql in _LEGACY:
        conn.execute(sql)
    conn.commit()
    meds_before = [tuple(r) for r in conn.execute("SELECT * FROM medications ORDER BY id")]
    helix_before = [tuple(r) for r in conn.execute("SELECT * FROM helix_patient_med_orders")]
    conn.execute("BEGIN")
    m61.apply(conn)
    conn.commit()
    first = _schema(conn)
    conn.execute("BEGIN")
    m61.apply(conn)
    conn.commit()
    assert _schema(conn) == first

    width = len(meds_before[0])
    meds_after = [tuple(r) for r in conn.execute("SELECT * FROM medications ORDER BY id")]
    assert [r[:width] for r in meds_after] == meds_before
    assert all(r[width:] == (None, None, None) for r in meds_after)
    helix_after = [tuple(r) for r in conn.execute("SELECT * FROM helix_patient_med_orders")]
    assert [r[:-1] for r in helix_after] == helix_before and helix_after[0][-1] is None
    assert "idx_helix_orders_med_order" in _indexes(conn, "helix_patient_med_orders")
    conn.close()


def test_the_runner_applies_it_inside_one_transaction(tmp_path):
    """Through migrations.run_pending_migrations on a database whose ledger
    already holds every other version: only 61 runs, and it is ledgered."""
    conn = _scratch(tmp_path, "runner.db")
    for sql in _LEGACY[:2]:
        conn.execute(sql)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, "
                 "applied_at TEXT)")
    others = [m for m in migrations.MIGRATIONS if m.version != 61]
    conn.executemany("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, 'x')",
                     [(m.version, m.name) for m in others])
    conn.commit()
    applied = migrations.run_pending_migrations(conn)
    assert applied == [61]
    assert NEW_TABLES <= _tables(conn)
    assert migrations.run_pending_migrations(conn) == []
    conn.close()


# ── 4. init_helix_db and HELIX_SCHEMA ─────────────────────────────────────────

def test_helix_schema_has_the_column_but_never_the_index():
    from services.helix_db import HELIX_SCHEMA
    table = HELIX_SCHEMA.split("CREATE TABLE IF NOT EXISTS helix_patient_med_orders", 1)[1]
    table = table.split(");", 1)[0]
    assert "med_order_id TEXT" in table
    assert "idx_helix_orders_med_order" not in HELIX_SCHEMA
    assert "med_order_id)" not in HELIX_SCHEMA.replace(" ", "")


def test_init_helix_db_after_the_runner_keeps_the_column_and_the_index():
    from services.helix_db import init_helix_db
    init_helix_db()
    with db_session() as db:
        assert "med_order_id" in _columns(db, "helix_patient_med_orders")
        assert "idx_helix_orders_med_order" in _indexes(db, "helix_patient_med_orders")


def test_init_helix_db_alone_on_a_legacy_table_without_the_column_runs_its_whole_script(tmp_path):
    """22 test files call init_helix_db. On a pharmacy table that predates the
    column, an unguarded index in the HELIX_SCHEMA script would abort every
    statement after it; the guarded step must simply skip the index."""
    db_path = tmp_path / "legacy_helix.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript("""
        CREATE TABLE helix_patient_med_orders (
            id TEXT PRIMARY KEY, patient_id TEXT, medication_id TEXT,
            status TEXT DEFAULT 'pending', prescribed_by TEXT, dosage TEXT, frequency TEXT,
            route TEXT, substitute_medication_id TEXT, pharmacist_notes TEXT,
            reviewed_by TEXT, reviewed_at TIMESTAMP, scheduled_at TIMESTAMP,
            last_transition_id TEXT, received_by TEXT, received_at TIMESTAMP,
            created_at TIMESTAMP);
        INSERT INTO helix_patient_med_orders (id, patient_id, status) VALUES ('H1', 'P1', 'pending');
    """)
    raw.commit()
    raw.close()
    body = """
import json
from services.helix_db import init_helix_db
from database import get_db
init_helix_db()
conn = get_db()
print("ORB_TEST_JSON:" + json.dumps({
    "tables": sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")),
    "indexes": sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")),
    "columns": [r[1] for r in conn.execute("PRAGMA table_info(helix_patient_med_orders)")],
    "rows": conn.execute("SELECT COUNT(*) FROM helix_patient_med_orders").fetchone()[0],
}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", body], cwd=str(SERVER_DIR),
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
             "ORB_DEPLOYMENT_PROFILE": "dev", "ORB_DB_PATH": str(db_path),
             "ORB_JWT_SECRET": "suite-only-secret-0123456789abcdef0123456789"},
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("ORB_TEST_JSON:")][-1]
    got = json.loads(line[len("ORB_TEST_JSON:"):])
    # Every table and index of the script after helix_patient_med_orders was made.
    assert {"helix_pharmacy_verifications", "helix_stock_movements", "helix_transitions",
            "helix_medication_master", "helix_inventory_lots"} <= set(got["tables"])
    assert {"idx_helix_transitions_order", "idx_helix_transitions_idem"} <= set(got["indexes"])
    assert "med_order_id" not in got["columns"]
    assert "idx_helix_orders_med_order" not in got["indexes"]
    assert got["rows"] == 1
    assert "Traceback" not in proc.stderr


# ── 5. The restore manifest ───────────────────────────────────────────────────

def test_the_restore_manifest_lists_both_tables():
    import importlib.util
    path = SERVER_DIR.parent / "scripts" / "lib" / "verify_restore.py"
    spec = importlib.util.spec_from_file_location("_vr_for_medorders", path)
    vr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vr)
    assert set(vr.MODULE_TABLES["medication orders"]) == NEW_TABLES
