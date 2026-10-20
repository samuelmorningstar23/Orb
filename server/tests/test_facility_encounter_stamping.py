"""Encounters and wards carry the facility (multisite-4, W11-A, migration 74).

Samuel ruled on 3 October 2026 (multi-site M1) that a hospital group runs one
appliance with a row-level facility id, and (M2) that isolation is keyed on
data: once a second facilities row exists, facility scope applies. This file
pins what multisite-4 adds:

  * migration 74: facility_id INTEGER NOT NULL DEFAULT 1 on opd_visits,
    appointments, surgeries, roster_import_runs, history_import_runs, wards and
    lab_specimens, on the session database (a fresh boot), on a hand-built
    legacy database and on a database booted before migration 74 existed;
    wards.facility_code and lab_specimens.site_id mapped onto the facility
    holding that code; idempotent; the OPD token index is not rebuilt and
    still refuses a duplicate token for one doctor;
  * permissions.FACILITY_PATIENT_SQL: an active admission first; otherwise the
    latest admission, OPD visit or appointment by julianday(); cancelled
    visits and bookings do not count; then the registered facility;
  * the writers stamp the facility while more than one facility is on record
    (OPD visits from the clinic, appointments from the clinic or the patient,
    surgeries from the patient, roster and history import runs from the
    importing account) and write facility 1 otherwise;
  * roster import refuses, reports and audits a row that would re-admit an
    MRN whose admissions are at another facility, move an open admission into
    another facility's department, admit into a department no facility lists,
    or reach outside the importing account's facilities; the count of patients
    with admissions at more than one facility stays 0;
  * history import fails the same rows into its reject ledger;
  * with one facility on record the plan, the apply and their output are
    exactly as before.

TEST RULE (contracts, W7-J): facility 9411 is removed in teardown and
facility_service.invalidate_cache() is called; every facility id this file
writes on a patient record is put back to 1. No HTTP route is called while the
second facility exists (the facility fence would answer 503). Run with the
test env, never the shipping .env.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import migrations  # noqa: F401  (migrations.py first: it collects migrations_his in order)
import permissions as P
from database import db_session
from db_backend import DBIntegrityError
from migrations_his import m0050_registration_opd_foundation as M50
from migrations_his import m0074_facility_columns_encounters as M74
from routes import appointments_routes as AR
from services import facility_service as FS
from services import frontdesk_service as fd
from services import history_import_service as hi
from services import roster_import as ri
from services import sage_handoff_service as sh
from services import surgery_escalation_service as se
from services import surgery_service as ss

SERVER_DIR = Path(__file__).resolve().parent.parent

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9411                       # an id no other module writes
F2_CODE = f"WA{TAG}"
DEPT_F1 = f"Kestrelholm Medicine {TAG}"
DEPT_F1_B = f"Kestrelholm Surgery {TAG}"
DEPT_F2 = f"Wrenfield Clinic {TAG}"
DEPT_UNLISTED = f"Ostervane Unlisted {TAG}"

ROSTER_HEADER = "MRN,Name,Ward,Bed,Department,Status"
ROSTER_MAPPING = {"mrn": "MRN", "name": "Name", "ward": "Ward", "bed": "Bed",
                  "department": "Department", "status": "Status"}


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(db, role, department, label):
    uid = f"FES{uuid.uuid4().hex[:8].upper()}"
    db.execute(
        "INSERT INTO users (id, name, role, department, password_hash, is_active) "
        "VALUES (?, ?, ?, ?, 'x', 1)", (uid, f"Quillon {label}", role, department))
    return uid


def _put_back_facility_two():
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Wrenfield Satellite"))
    FS.invalidate_cache()


@pytest.fixture(scope="module")
def world():
    users: dict = {}
    with db_session() as db:
        baseline = FS.facility_report(db)["patients_at_several_facilities"]
        for name, fid in ((DEPT_F1, 1), (DEPT_F1_B, 1), (DEPT_F2, F2)):
            db.execute("INSERT INTO departments (name, facility_id, seeded_from) "
                       "VALUES (?, ?, 'test')", (name, fid))
        users["doc_f1"] = _mk_user(db, "doctor", DEPT_F1, "F1 doctor")
        users["doc_f1_b"] = _mk_user(db, "doctor", DEPT_F1_B, "F1 surgeon")
        users["doc_f2"] = _mk_user(db, "doctor", DEPT_F2, "F2 doctor")
        users["doc_unlisted"] = _mk_user(db, "doctor", DEPT_UNLISTED, "unlisted doctor")
        users["admin_f1"] = _mk_user(db, "admin", None, "F1 admin")
        users["admin_f2"] = _mk_user(db, "admin", None, "F2 admin")
        db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                   "granted_at, reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, "
                   "'test')", (users["admin_f2"], F2))
    _put_back_facility_two()
    state = {"users": users, "patients": [], "baseline_split": baseline}
    try:
        yield state
    finally:
        uids, pids = list(users.values()), [(p,) for p in state["patients"]]
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
            db.execute("DELETE FROM departments WHERE name LIKE ?", (f"%{TAG}%",))
            db.executemany("DELETE FROM user_facilities WHERE user_id = ?", [(u,) for u in uids])
            db.executemany("DELETE FROM opd_visits WHERE patient_id = ?", pids)
            db.executemany("DELETE FROM appointments WHERE patient_id = ?", pids)
            db.executemany("DELETE FROM surgery_team_assignments WHERE surgery_id IN "
                           "(SELECT id FROM surgeries WHERE patient_id = ?)", pids)
            db.executemany("UPDATE surgery_prep_requests SET surgery_id = NULL "
                           "WHERE patient_id = ?", pids)
            db.executemany("DELETE FROM surgeries WHERE patient_id = ?", pids)
            db.executemany("UPDATE admissions SET facility_id = 1, status = 'discharged' "
                           "WHERE patient_id = ?", pids)
            db.executemany("UPDATE patients SET registered_facility_id = 1, "
                           "status = 'discharged' WHERE id = ?", pids)
            db.execute("UPDATE roster_import_runs SET facility_id = 1 WHERE facility_id = ?", (F2,))
            db.execute("UPDATE history_import_runs SET facility_id = 1 WHERE facility_id = ?", (F2,))
            db.executemany("UPDATE users SET is_active = 0 WHERE id = ?", [(u,) for u in uids])
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh_cache():
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


@pytest.fixture()
def one_facility(world):
    """Facility two taken off record for one test, then put back."""
    with db_session() as db:
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        yield
    finally:
        _put_back_facility_two()


def _patient(world, name, *, status="discharged", registered=1, ward=None, bed=None,
             diagnosis=None) -> str:
    pid = f"FEP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, registered_facility_id, ward, "
                   "bed, diagnosis) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (pid, name, f"MRN-{pid}", status, registered, ward, bed, diagnosis))
    world["patients"].append(pid)
    return pid


def _admission(world, pid, department, facility, *, status="discharged",
               created_at="2026-10-01 10:00:00", ward=None) -> str:
    aid = f"FEA{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, "
                   "status, created_at, facility_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (aid, pid, world["users"]["doc_f1"], department, ward, status, created_at,
                    facility))
    return aid


def _visit(pid, department, facility, registered_at, *, status="completed") -> str:
    vid = f"OPD-FE{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
                   "status, registered_at, facility_id) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                   (vid, pid, department, registered_at[:10], status, registered_at, facility))
    return vid


def _appointment(pid, facility, created_at, *, status="booked") -> str:
    apid = str(uuid.uuid4())
    with db_session() as db:
        db.execute("INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time, "
                   "status, created_at, facility_id) VALUES (?, ?, '2026-11-02', '10:00', ?, ?, ?)",
                   (apid, pid, status, created_at, facility))
    return apid


def _facility_of(pid) -> int | None:
    with db_session() as db:
        row = db.execute(f"SELECT {P.FACILITY_PATIENT_SQL} FROM patients p WHERE p.id = ?",
                         (pid,)).fetchone()
    return None if row is None else row[0]


def _column(db, table, name):
    return {r[1]: tuple(r) for r in db.execute(f"PRAGMA table_info({table})")}.get(name)


def _max_audit_id() -> int:
    with db_session() as db:
        return int(db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0])


def _refusal_rows(since: int) -> list[dict]:
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'cross_facility_move_refused' "
            "AND id > ? ORDER BY id", (since,)).fetchall()]
    for r in rows:
        r["meta"] = json.loads(r.get("metadata_json") or "{}")
    return rows


def _split_patients(pids) -> int:
    if not pids:
        return 0
    marks = ",".join("?" for _ in pids)
    with db_session() as db:
        return int(db.execute(
            f"SELECT COUNT(*) FROM (SELECT patient_id FROM admissions WHERE patient_id IN "
            f"({marks}) GROUP BY patient_id HAVING COUNT(DISTINCT facility_id) > 1)",
            tuple(pids)).fetchone()[0])


def _roster(*rows) -> bytes:
    return ("\n".join([ROSTER_HEADER, *rows]) + "\n").encode()


def _row(plan, mrn):
    return next(r for r in plan["rows"] if r["mrn"] == mrn)


# ══════════════════════════════════════════════════════════════════════════
#  1. Migration 74
# ══════════════════════════════════════════════════════════════════════════

def test_migration_74_is_ledgered_and_every_column_reads_facility_one():
    with db_session() as db:
        assert 74 in {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
        for table, column in M74.FACILITY_COLUMNS:
            col = _column(db, table, column)
            assert col is not None, f"{table}.{column} is missing after a fresh boot"
            assert col[2] == "INTEGER" and col[3] == 1 and col[4] == "1", (table, col)
            refs = [tuple(r) for r in db.execute(f"PRAGMA foreign_key_list({table})")]
            assert not any(r[3] == "facility_id" for r in refs), table
        # Every ward points at the facility its label names (all 'MAIN' today).
        assert db.execute(
            "SELECT COUNT(*) FROM wards w WHERE w.facility_id != COALESCE("
            "(SELECT f.id FROM facilities f WHERE f.code = TRIM(w.facility_code) COLLATE NOCASE), "
            "1)").fetchone()[0] == 0
        users = [r[1] for r in db.execute("PRAGMA table_info(users)")]
    assert not any("facility" in c for c in users)
    assert set(M74.TABLES) >= {"opd_visits", "appointments", "surgeries", "roster_import_runs",
                               "history_import_runs", "wards", "lab_specimens"}
    assert set(M74.ENCOUNTER_TABLES) == {"opd_visits", "appointments"}


def _legacy(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / f"legacy-{uuid.uuid4().hex[:6]}.db"))
    conn.execute("CREATE TABLE facilities (id INTEGER PRIMARY KEY, uuid TEXT NOT NULL, "
                 "code TEXT NOT NULL UNIQUE COLLATE NOCASE)")
    conn.execute("INSERT INTO facilities VALUES (1, 'u1', 'MAIN'), (2, 'u2', 'SATA')")
    conn.execute("CREATE TABLE wards (id TEXT PRIMARY KEY, facility_code TEXT NOT NULL "
                 "DEFAULT 'MAIN', code TEXT NOT NULL, department TEXT NOT NULL)")
    conn.executemany("INSERT INTO wards VALUES (?, ?, ?, 'Ward medicine')", [
        ("W1", "MAIN", "GM1"), ("W2", " sata ", "S1"), ("W3", "NOWHERE", "N1")])
    conn.execute("CREATE TABLE lab_specimens (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, "
                 "site_id TEXT)")
    conn.executemany("INSERT INTO lab_specimens VALUES (?, 'P1', ?)",
                     [("S1", None), ("S2", "SATA"), ("S3", "  ")])
    conn.execute("CREATE TABLE opd_visits (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL)")
    conn.execute("INSERT INTO opd_visits VALUES ('V1', 'P1')")
    conn.execute("CREATE TABLE appointments (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL)")
    conn.execute("INSERT INTO appointments VALUES ('A1', 'P1')")
    conn.commit()
    return conn


def _in_runner_txn(conn, fn):
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    out = fn(conn)
    conn.execute("COMMIT")
    return out


def test_migration_74_on_a_legacy_database_maps_the_labels_and_runs_twice(tmp_path):
    conn = _legacy(tmp_path)
    before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]
              for t in ("wards", "lab_specimens", "opd_visits", "appointments")}
    _in_runner_txn(conn, M74.apply)
    wards = {r[0]: r[1] for r in conn.execute("SELECT id, facility_id FROM wards")}
    specimens = {r[0]: r[1] for r in conn.execute("SELECT id, facility_id FROM lab_specimens")}
    assert wards == {"W1": 1, "W2": 2, "W3": 1}           # an unknown label stays at 1
    assert specimens == {"S1": 1, "S2": 2, "S3": 1}
    assert [r[0] for r in conn.execute("SELECT facility_id FROM opd_visits")] == [1]
    assert [r[0] for r in conn.execute("SELECT facility_id FROM appointments")] == [1]
    for table, rows in before.items():
        width = len(rows[0])
        assert [tuple(r)[:width] for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")] \
            == rows, f"migration 74 changed an existing {table} value"
    schema = sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master"))
    assert _in_runner_txn(conn, M74.add_facility_columns) == []
    assert _in_runner_txn(conn, M74.map_labels) == {"wards": 0, "lab_specimens": 0}
    assert sorted(tuple(r) for r in conn.execute(
        "SELECT type, name, sql FROM sqlite_master")) == schema
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_a_database_without_the_module_tables_migrates(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    conn.execute("CREATE TABLE opd_visits (id TEXT PRIMARY KEY)")
    _in_runner_txn(conn, M74.apply)
    assert [r[1] for r in conn.execute("PRAGMA table_info(opd_visits)")] == ["id", "facility_id"]
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} \
        == {"opd_visits"}
    assert M74.ensure_opd_visit_facility_on(conn) is False
    conn.close()


def test_the_front_desk_lazy_path_builds_the_runners_opd_visits(tmp_path):
    lazy = sqlite3.connect(str(tmp_path / "lazy.db"))
    M50.ensure_opd_visits_on(lazy)
    assert fd.ensure_opd_visit_facility_on(lazy) is True
    with db_session() as db:
        runner = [tuple(r) for r in db.execute("PRAGMA table_info(opd_visits)")]
    assert [tuple(r) for r in lazy.execute("PRAGMA table_info(opd_visits)")] == runner
    lazy.close()


_BOOT = """
import json, sys
import migrations
if sys.argv[1] == "before":
    migrations.MIGRATIONS[:] = [m for m in migrations.MIGRATIONS if m.version != 74]
from database import init_db, get_db
init_db()
c = get_db()
tables = ("opd_visits", "appointments", "surgeries", "roster_import_runs",
          "history_import_runs", "wards", "lab_specimens")
if sys.argv[1] == "before":
    c.execute("INSERT INTO users (id, name, role, department, password_hash) "
              "VALUES ('UFE1', 'Quillon Legacy', 'doctor', 'Legacy Medicine', 'x')")
    c.execute("INSERT INTO patients (id, name, mrn, status) "
              "VALUES ('PFE1', 'Odalric Thwaite', 'MRN-FE1', 'discharged')")
    c.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
              "VALUES ('AFE1', 'PFE1', 'UFE1', 'Legacy Medicine', 'discharged')")
    c.execute("INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time) "
              "VALUES ('APFE1', 'PFE1', '2026-10-01', '09:00')")
    c.execute("INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
              "registered_at) VALUES ('VFE1', 'PFE1', 'Legacy Medicine', '2026-10-01', 1, "
              "'2026-10-01T03:00:00+00:00')")
    c.execute("INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis, "
              "scheduled_date, scheduled_time) VALUES ('SFE1', 'PFE1', 'UFE1', 'Hernia repair', "
              "'Inguinal hernia', '2026-10-02', '10:00')")
    c.execute("INSERT INTO wards (id, code, department, created_at) "
              "VALUES ('WRD-FE1', 'LM1', 'Legacy Medicine', '2026-10-01T00:00:00Z')")
    c.execute("INSERT INTO roster_import_runs (id, mapping_json) VALUES ('RI-FE1', '{}')")
    c.commit()
out = {
    "applied": sorted(r[0] for r in c.execute("SELECT version FROM schema_migrations")),
    "cols": {t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")] for t in tables},
    "counts": {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("patients", "admissions", "appointments", "opd_visits", "surgeries",
                         "wards", "roster_import_runs", "users")},
    "fk": [list(r) for r in c.execute("PRAGMA foreign_key_check").fetchall()],
    "ok": c.execute("PRAGMA integrity_check").fetchone()[0],
}
if sys.argv[1] == "after":
    out["stamped"] = {t: c.execute(f"SELECT DISTINCT facility_id FROM {t}").fetchall() and
                      sorted(r[0] for r in c.execute(f"SELECT DISTINCT facility_id FROM {t}"))
                      for t in ("appointments", "opd_visits", "surgeries", "wards",
                                "roster_import_runs")}
print("ORB_TEST_JSON:" + json.dumps(out))
"""


def _boot(db_path, phase) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _BOOT, phase], cwd=str(SERVER_DIR), capture_output=True,
        text=True, timeout=300,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
             "ORB_DEPLOYMENT_PROFILE": "dev", "ORB_DB_PATH": str(db_path)})
    assert proc.returncode == 0, proc.stderr[-4000:]
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("ORB_TEST_JSON:")][-1]
    return json.loads(line[len("ORB_TEST_JSON:"):])


def test_a_database_booted_before_migration_74_gains_it_at_the_next_boot(tmp_path):
    db_path = tmp_path / "pre74.db"
    before = _boot(db_path, "before")
    assert 74 not in before["applied"]
    assert all("facility_id" not in cols for cols in before["cols"].values())
    after = _boot(db_path, "after")
    assert 74 in after["applied"]
    for table, cols in after["cols"].items():
        assert cols[:-1] == before["cols"][table] and cols[-1] == "facility_id", table
    assert after["counts"] == before["counts"]
    assert after["stamped"] == {t: [1] for t in after["stamped"]}
    assert after["fk"] == [] and after["ok"] == "ok"


def test_the_token_index_is_not_rebuilt_and_still_refuses_a_duplicate_token(world):
    with db_session() as db:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_opd_token_unique'"
                         ).fetchone()[0]
    assert "facility_id" not in sql
    assert sql.replace("IF NOT EXISTS ", "") in [
        s.replace("IF NOT EXISTS ", "") for s in M50.OPD_VISITS_INDEXES]
    pid = _patient(world, "Ilka Vantongeren")
    doctor = world["users"]["doc_f2"]
    with db_session() as db:
        db.execute("INSERT INTO opd_visits (id, patient_id, department, doctor_id, visit_date, "
                   "token_number, registered_at, facility_id) VALUES (?, ?, ?, ?, '2026-10-20', "
                   "7, '2026-10-20T04:00:00+00:00', ?)",
                   (f"OPD-FT{TAG}1", pid, DEPT_F2, doctor, F2))
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("INSERT INTO opd_visits (id, patient_id, department, doctor_id, "
                       "visit_date, token_number, registered_at, facility_id) VALUES (?, ?, ?, ?, "
                       "'2026-10-20', 7, '2026-10-20T04:05:00+00:00', ?)",
                       (f"OPD-FT{TAG}2", pid, DEPT_F2, doctor, F2))


# ══════════════════════════════════════════════════════════════════════════
#  2. FACILITY_PATIENT_SQL: the facility of the latest encounter
# ══════════════════════════════════════════════════════════════════════════

def test_an_opd_visit_at_another_facility_moves_a_discharged_patient(world):
    pid = _patient(world, "Corvin Ashgrave")
    _admission(world, pid, DEPT_F1, 1, created_at="2026-10-01 10:00:00")
    assert _facility_of(pid) == 1
    _visit(pid, DEPT_F2, F2, "2026-10-05T04:00:00+00:00")
    assert _facility_of(pid) == F2
    with db_session() as db:
        assert P.patient_in_scope(db, pid, None, facilities=frozenset({F2})) is True
        assert P.patient_in_scope(db, pid, None, facilities=frozenset({1})) is False
        # The F1 ward that discharged them no longer holds the latest encounter.
        assert P.patient_in_scope(db, pid, DEPT_F1, facilities=frozenset({1})) is False
        assert P.facility_denial(db, pid, frozenset({1})) == (True, F2)
        sql, params = P.scope_patient_ids_sql(None, facilities=frozenset({F2}))
        ids = {r[0] for r in db.execute(sql, params).fetchall()}
    assert pid in ids


def test_an_active_admission_always_wins_over_a_later_visit_or_booking(world):
    pid = _patient(world, "Teodor Brannagh", status="admitted")
    _admission(world, pid, DEPT_F1, 1, status="active", created_at="2026-10-01 10:00:00")
    _visit(pid, DEPT_F2, F2, "2026-10-06T04:00:00+00:00")
    _appointment(pid, F2, "2026-10-07 04:00:00")
    assert _facility_of(pid) == 1
    with db_session() as db:
        assert P.patient_in_scope(db, pid, DEPT_F1, facilities=frozenset({1})) is True


def test_cancelled_visits_and_bookings_are_not_encounters(world):
    pid = _patient(world, "Mireille Okonkwo-Hale")
    _admission(world, pid, DEPT_F1, 1, created_at="2026-10-01 10:00:00")
    _visit(pid, DEPT_F2, F2, "2026-10-05T04:00:00+00:00", status="cancelled")
    _appointment(pid, F2, "2026-10-06 04:00:00", status="cancelled")
    assert _facility_of(pid) == 1
    _appointment(pid, F2, "2026-10-07 04:00:00", status="booked")
    assert _facility_of(pid) == F2


def test_instants_are_compared_by_julianday_not_by_their_spelling(world):
    """A visit stamped 09:00+05:30 (03:30 UTC) is EARLIER than an admission
    stamped '04:00:00' UTC on the same day, though its text sorts later."""
    pid = _patient(world, "Sabine Quillfeather")
    _visit(pid, DEPT_F2, F2, "2026-10-05T09:00:00+05:30")
    _admission(world, pid, DEPT_F1, 1, created_at="2026-10-05 04:00:00")
    assert _facility_of(pid) == 1


def test_a_tie_goes_to_the_admission_and_no_encounter_reads_the_registered_facility(world):
    pid = _patient(world, "Hollis Varnakov")
    _admission(world, pid, DEPT_F1, 1, created_at="2026-10-05 04:00:00")
    _visit(pid, DEPT_F2, F2, "2026-10-05T04:00:00+00:00")
    assert _facility_of(pid) == 1
    bare = _patient(world, "Ottoline Brisk", status="registered", registered=F2)
    assert _facility_of(bare) == F2


# ══════════════════════════════════════════════════════════════════════════
#  3. Writers stamp the facility
# ══════════════════════════════════════════════════════════════════════════

def test_an_opd_visit_is_stamped_with_its_clinics_facility(world):
    pid = _patient(world, "Wendeline Ashcombe", status="registered")
    visit = fd.register_opd_visit(patient_id=pid, department=DEPT_F2.lower(),
                                  registered_by=world["users"]["admin_f2"])
    assert "facility_id" not in visit, "the visit's API shape must not change"
    with db_session() as db:
        stored = db.execute("SELECT facility_id, department FROM opd_visits WHERE id = ?",
                            (visit["id"],)).fetchone()
    assert tuple(stored) == (F2, DEPT_F2)
    other = fd.register_opd_visit(patient_id=pid, department=DEPT_F1,
                                  registered_by=world["users"]["admin_f1"])
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM opd_visits WHERE id = ?",
                          (other["id"],)).fetchone()[0] == 1


def test_a_clinic_no_facility_lists_is_refused_with_words(world):
    with db_session() as db:
        with pytest.raises(fd.FrontDeskError) as err:
            fd.opd_visit_facility_on(db, DEPT_UNLISTED)
        assert "departments list" in str(err.value)
        assert fd.department_facility_on(db, f"  {DEPT_F2.upper()} ") == F2
        assert fd.department_facility_on(db, DEPT_UNLISTED) is None


def test_with_one_facility_a_visit_reads_facility_one_and_its_shape_is_unchanged(
        world, one_facility):
    pid = _patient(world, "Radegund Oake", status="registered")
    visit = fd.register_opd_visit(patient_id=pid, department=DEPT_F1)
    assert "facility_id" not in visit
    with db_session() as db:
        assert fd.opd_visit_facility_on(db, DEPT_UNLISTED) == 1
        assert db.execute("SELECT facility_id FROM opd_visits WHERE id = ?",
                          (visit["id"],)).fetchone()[0] == 1


def test_a_booking_takes_its_clinics_facility_or_the_patients(world):
    f1_patient = _patient(world, "Leocadia Firth")
    _admission(world, f1_patient, DEPT_F1, 1)
    f2_patient = _patient(world, "Barnaby Szolnok", status="registered", registered=F2)
    booker = world["users"]["admin_f1"]
    asked, _ = AR.persist_appointment(
        patient_id=f2_patient, booked_by_user_id=booker, scheduled_date="2026-11-03",
        scheduled_time="09:30", appointment_type="Follow-up", reason=None, origin="patient")
    plain, _ = AR.persist_appointment(
        patient_id=f1_patient, booked_by_user_id=booker, scheduled_date="2026-11-03",
        scheduled_time="10:30", appointment_type="Follow-up", reason=None)
    clinic, _ = AR.persist_appointment(
        patient_id=f1_patient, booked_by_user_id=booker, scheduled_date="2026-11-04",
        scheduled_time="10:30", appointment_type="Follow-up", reason=None,
        department=DEPT_F2)
    assert "facility_id" not in clinic, "the booking's API shape must not change"
    with db_session() as db:
        got = {r[0]: r[1] for r in db.execute(
            "SELECT id, facility_id FROM appointments WHERE id IN (?, ?, ?)",
            (asked["id"], plain["id"], clinic["id"]))}
        assert AR._appointment_facility_on(db, patient_id=f1_patient,
                                           department=DEPT_UNLISTED, fallback=F2) == F2
    assert got == {asked["id"]: F2, plain["id"]: 1, clinic["id"]: F2}
    # The booking at the F2 clinic is now the patient's latest encounter.
    assert _facility_of(f1_patient) == F2


def test_a_doctors_slot_booking_takes_the_sessions_clinic(world):
    pid = _patient(world, "Ferdinanda Glasby", status="registered")
    offer = {"session": {"department": DEPT_F2, "session_id": None}, "slot": {"capacity": 1}}
    with db_session() as db:
        row, created = AR._book_slot_on(
            db, patient_id=pid, booked_by=world["users"]["admin_f2"],
            doctor_id=world["users"]["doc_f2"], scheduled_date="2026-11-05",
            scheduled_time="11:00", appointment_type="Follow-up", reason=None, offer=offer,
            idem=None)
        assert created and row["facility_id"] == F2


def test_with_one_facility_a_booking_reads_facility_one(world, one_facility):
    pid = _patient(world, "Gaspard Hethering", status="registered", registered=F2)
    booked, created = AR.persist_appointment(
        patient_id=pid, booked_by_user_id=world["users"]["admin_f1"],
        scheduled_date="2026-11-06", scheduled_time="09:00", appointment_type="Follow-up",
        reason=None, department=DEPT_F2)
    assert created
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM appointments WHERE id = ?",
                          (booked["id"],)).fetchone()[0] == 1


def test_an_emergency_case_is_booked_at_the_patients_facility(world):
    pid = _patient(world, "Isidore Penhallow", status="admitted", diagnosis="Perforated viscus")
    _admission(world, pid, DEPT_F2, F2, status="active", created_at="2026-10-10 06:00:00")
    with db_session() as db:
        assert ss.surgery_facility_on(db, pid) == F2
    prep = se.create_surgery_prep_request(pid, None, world["users"]["doc_f2"], "Dr Quillon",
                                          "Laparotomy", "Perforation", "emergency", "")
    made = se.confirm_surgery_prep(prep["id"], world["users"]["doc_f2"], "Dr Quillon",
                                   prep["idempotency_key"])
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM surgeries WHERE id = ?",
                          (made["surgery_id"],)).fetchone()[0] == F2


def test_a_relay_transfer_case_is_booked_at_the_patients_facility(world):
    pid = _patient(world, "Aurelio Thistlewood", status="admitted", diagnosis="Ischaemic bowel")
    _admission(world, pid, DEPT_F2, F2, status="active", created_at="2026-10-11 06:00:00")
    with db_session() as db:
        surgery_id, new = sh._dispatch_to_transfer(
            {"patient_id": pid, "target": DEPT_F2}, world["users"]["doc_f2"], db)
    assert new is True
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM surgeries WHERE id = ?",
                          (surgery_id,)).fetchone()[0] == F2


def test_with_one_facility_an_emergency_case_reads_facility_one(world, one_facility):
    pid = _patient(world, "Clementine Ravelstoke", status="admitted", diagnosis="Appendicitis")
    _admission(world, pid, DEPT_F2, F2, status="active", created_at="2026-10-12 06:00:00")
    with db_session() as db:
        assert ss.surgery_facility_on(db, pid) == 1
    prep = se.create_surgery_prep_request(pid, None, world["users"]["doc_f1"], "Dr Quillon",
                                          "Appendicectomy", "Appendicitis", "emergency", "")
    made = se.confirm_surgery_prep(prep["id"], world["users"]["doc_f1"], "Dr Quillon",
                                   prep["idempotency_key"])
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM surgeries WHERE id = ?",
                          (made["surgery_id"],)).fetchone()[0] == 1


# ══════════════════════════════════════════════════════════════════════════
#  4. Roster import: one facility per patient
# ══════════════════════════════════════════════════════════════════════════

def _mrn(tag) -> str:
    return f"FE{tag}{uuid.uuid4().hex[:6].upper()}"


def _apply(world, content, actor):
    result = ri.apply_import(content, ROSTER_MAPPING, actor_user_id=actor,
                             source_filename="facility.csv")
    world["patients"].extend(result["created_patient_ids"])
    return result


def test_a_roster_import_at_f2_creates_f2_admissions(world):
    mrn = _mrn("NEW")
    content = _roster(f"{mrn},Ysolde Marrowby,WF-1,B2,{DEPT_F2},admitted")
    planned = ri.plan(content, ROSTER_MAPPING, actor_user_id=world["users"]["admin_f2"])
    assert planned["facility_refusals"] == [] and planned["facility_id"] == F2
    assert _row(planned, mrn)["admission"]["facility_id"] == F2
    result = _apply(world, content, world["users"]["admin_f2"])
    assert result["admissions"]["created"] == 1
    assert result["admissions"]["facility_refused"] == 0
    with db_session() as db:
        patient = db.execute("SELECT id, registered_facility_id FROM patients WHERE mrn = ?",
                             (mrn,)).fetchone()
        adm = db.execute("SELECT facility_id, department FROM admissions WHERE patient_id = ?",
                         (patient["id"],)).fetchone()
        run = db.execute("SELECT facility_id FROM roster_import_runs WHERE id = ?",
                         (result["run_id"],)).fetchone()
    assert patient["registered_facility_id"] == F2
    assert tuple(adm) == (F2, DEPT_F2)
    assert run["facility_id"] == F2
    listed = next(r for r in ri.list_runs(500) if r["id"] == result["run_id"])
    assert listed["facility_id"] == F2


def test_a_roster_row_re_admitting_an_f1_mrn_at_f2_is_reported_not_created(world):
    pid = _patient(world, "Ottokar Fenwright")
    _admission(world, pid, DEPT_F1, 1)
    with db_session() as db:
        mrn = db.execute("SELECT mrn FROM patients WHERE id = ?", (pid,)).fetchone()[0]
    content = _roster(f"{mrn},Ottokar Fenwright,WF-2,B1,{DEPT_F2},admitted")
    admin = world["users"]["admin_f2"]
    planned = ri.plan(content, ROSTER_MAPPING, actor_user_id=admin)
    row = _row(planned, mrn)
    assert row["action"] == "skip" and row["reason"] == "patient_at_another_facility"
    assert row["admission"]["action"] == "rejected"
    assert row["patient_id"] is None and row["changes"] == {}, "another facility's patient leaked"
    assert planned["counts"]["facility_refusals"] == 1
    assert [r["code"] for r in planned["facility_refusals"]] == ["patient_at_another_facility"]
    since = _max_audit_id()
    result = _apply(world, content, admin)
    assert result["admissions"]["created"] == 0
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM admissions WHERE patient_id = ? AND "
                          "facility_id = ?", (pid, F2)).fetchone()[0] == 0
        assert db.execute("SELECT ward FROM patients WHERE id = ?", (pid,)).fetchone()[0] is None
    rows = _refusal_rows(since)
    assert len(rows) == 1
    audit = rows[0]
    assert audit["event_category"] == "authorization" and audit["outcome"] == "denied"
    assert audit["patient_id"] == pid and audit["actor_user_id"] == admin
    assert audit["meta"]["lane"] == "roster_import"
    assert audit["meta"]["cause"] == "patient_at_another_facility"
    assert audit["meta"]["to_facility_id"] == F2 and audit["meta"]["from_facility_ids"] == [1]
    assert _split_patients(world["patients"]) == 0


def test_moving_an_open_f2_admission_into_an_f1_department_is_refused(world):
    pid = _patient(world, "Philippa Grosvenor-Ash", status="admitted", ward="WF-3")
    _admission(world, pid, DEPT_F2, F2, status="active", ward="WF-3",
               created_at="2026-10-12 08:00:00")
    with db_session() as db:
        mrn = db.execute("SELECT mrn FROM patients WHERE id = ?", (pid,)).fetchone()[0]
    content = _roster(f"{mrn},Philippa Grosvenor-Ash,WF-3,B4,{DEPT_F1},admitted")
    since = _max_audit_id()
    for actor in (world["users"]["admin_f2"], "system"):
        planned = ri.plan(content, ROSTER_MAPPING, actor_user_id=actor)
        assert _row(planned, mrn)["reason"] == "patient_at_another_facility"
    result = _apply(world, content, "system")
    assert result["admissions"]["updated"] == 0
    with db_session() as db:
        adm = db.execute("SELECT department, facility_id FROM admissions WHERE patient_id = ?",
                         (pid,)).fetchone()
        bed = db.execute("SELECT bed FROM patients WHERE id = ?", (pid,)).fetchone()[0]
    assert tuple(adm) == (DEPT_F2, F2)
    assert bed is None, "a refused row must write nothing, not even the bed"
    assert [r["meta"]["cause"] for r in _refusal_rows(since)] == ["patient_at_another_facility"]
    assert _split_patients(world["patients"]) == 0


def test_an_f2_account_cannot_admit_into_or_change_another_facilitys_records(world):
    admin = world["users"]["admin_f2"]
    new_mrn = _mrn("OUT")
    f1_patient = _patient(world, "Leopold Danforth")
    _admission(world, f1_patient, DEPT_F1, 1)
    with db_session() as db:
        f1_mrn = db.execute("SELECT mrn FROM patients WHERE id = ?",
                            (f1_patient,)).fetchone()[0]
    content = ("MRN,Name,Ward,Department\n"
               f"{new_mrn},Cosima Varrell,KH-1,{DEPT_F1}\n"
               f"{f1_mrn},Leopold Danforth-Renamed,,\n").encode()
    mapping = {"mrn": "MRN", "name": "Name", "ward": "Ward", "department": "Department"}
    planned = ri.plan(content, mapping, actor_user_id=admin)
    assert _row(planned, new_mrn)["reason"] == "actor_outside_facility"
    changed = _row(planned, f1_mrn)
    assert changed["reason"] == "actor_outside_facility"
    assert changed["changes"] == {} and changed["patient_id"] is None
    result = ri.apply_import(content, mapping, actor_user_id=admin)
    assert result["created_patient_ids"] == [] and result["updated_patient_ids"] == []
    with db_session() as db:
        assert db.execute("SELECT name FROM patients WHERE id = ?",
                          (f1_patient,)).fetchone()[0] == "Leopold Danforth"


def test_a_department_no_facility_lists_or_no_department_at_all_is_refused(world):
    listed_nowhere, blank = _mrn("UNL"), _mrn("BLANK")
    content = ("MRN,Name,Department\n"
               f"{listed_nowhere},Eudora Pemberly,{DEPT_UNLISTED}\n"
               f"{blank},Tobiah Kestrell,\n").encode()
    planned = ri.plan(content, {"mrn": "MRN", "name": "Name", "department": "Department"},
                      actor_user_id="system")
    assert _row(planned, listed_nowhere)["reason"] == "department_not_listed"
    words = [i["message"] for i in _row(planned, blank)["issues"]
             if i["code"] == "department_not_listed"]
    assert words and "names no department" in words[0]
    assert planned["counts"]["facility_refusals"] == 2


def test_a_plan_with_more_than_one_facility_needs_the_importing_account(world):
    with pytest.raises(ValueError, match="account running the import"):
        ri.plan(_roster(f"{_mrn('NOACT')},Hester Wendall,WF-1,B1,{DEPT_F2},admitted"),
                ROSTER_MAPPING)


def test_absent_from_roster_lists_only_the_accounts_facilities(world):
    ward = f"KH{TAG}"
    f1_patient = _patient(world, "Seraphine Holloway", status="admitted", ward=ward)
    _admission(world, f1_patient, DEPT_F1, 1, status="active", ward=ward,
               created_at="2026-10-13 08:00:00")
    content = _roster(f"{_mrn('ABS')},Rowan Ellery,{ward},B9,{DEPT_F2},admitted")
    as_f2 = ri.plan(content, ROSTER_MAPPING, actor_user_id=world["users"]["admin_f2"])
    group = ri.plan(content, ROSTER_MAPPING, actor_user_id="system")
    assert f1_patient not in {p["id"] for p in as_f2["absent_from_roster"]["patients"]}
    assert f1_patient in {p["id"] for p in group["absent_from_roster"]["patients"]}


def test_a_re_admission_another_facility_overtook_is_refused_inside_the_write(world,
                                                                             monkeypatch):
    pid = _patient(world, "Wilhelmina Strand", status="discharged", registered=F2)
    with db_session() as db:
        mrn = db.execute("SELECT mrn FROM patients WHERE id = ?", (pid,)).fetchone()[0]
    content = _roster(f"{mrn},Wilhelmina Strand,WF-5,B3,{DEPT_F2},admitted")
    admin = world["users"]["admin_f2"]
    stale = ri.plan(content, ROSTER_MAPPING, actor_user_id=admin)
    assert _row(stale, mrn)["admission"]["action"] == "create"
    # Between the plan and the write, facility 1 admits the patient.
    _admission(world, pid, DEPT_F1, 1, created_at="2026-10-14 08:00:00")
    monkeypatch.setattr(ri, "plan", lambda *a, **k: copy.deepcopy(stale))
    since = _max_audit_id()
    result = ri.apply_import(content, ROSTER_MAPPING, actor_user_id=admin)
    assert result["admissions"]["created"] == 0
    assert _row(result, mrn)["reason"] == "patient_at_another_facility"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM admissions WHERE patient_id = ? AND "
                          "facility_id = ?", (pid, F2)).fetchone()[0] == 0
    rows = _refusal_rows(since)
    assert [r["meta"]["cause"] for r in rows] == ["patient_at_another_facility"]
    assert rows[0]["patient_id"] == pid
    assert _split_patients(world["patients"]) == 0


def test_with_one_facility_roster_import_output_is_unchanged(world, one_facility):
    mrn = _mrn("ONE")
    content = _roster(f"{mrn},Benedikta Lorrimer,KH-9,B1,{DEPT_F1},admitted")
    planned = ri.plan(content, ROSTER_MAPPING)          # no account needed
    assert "facility_refusals" not in planned and "facility_id" not in planned
    assert "facility_refusals" not in planned["counts"]
    assert "facility_id" not in _row(planned, mrn)["admission"]
    result = _apply(world, content, world["users"]["admin_f1"])
    assert "facility_refused" not in result["admissions"]
    with db_session() as db:
        pid = db.execute("SELECT id FROM patients WHERE mrn = ?", (mrn,)).fetchone()[0]
        assert db.execute("SELECT facility_id FROM admissions WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 1
        assert db.execute("SELECT registered_facility_id FROM patients WHERE id = ?",
                          (pid,)).fetchone()[0] == 1
        assert db.execute("SELECT facility_id FROM roster_import_runs WHERE id = ?",
                          (result["run_id"],)).fetchone()[0] == 1
    listed = next(r for r in ri.list_runs(500) if r["id"] == result["run_id"])
    assert "facility_id" not in listed


def test_the_operations_count_of_split_patients_stays_where_it_was(world):
    with db_session() as db:
        assert FS.facility_report(db)["patients_at_several_facilities"] \
            == world["baseline_split"]
    assert _split_patients(world["patients"]) == 0


# ══════════════════════════════════════════════════════════════════════════
#  5. History import
# ══════════════════════════════════════════════════════════════════════════

PATIENT_HEADER = "PatientID,MRN,Name"
PATIENT_MAPPING = {"external_id": "PatientID", "mrn": "MRN", "name": "Name"}
ADMISSION_MAPPING = {"external_id": "EncID", "mrn": "MRN", "department": "Dept",
                     "admitted_at": "AdmitDate"}


def _system() -> str:
    return f"Ledgerline HIS {uuid.uuid4().hex[:6].upper()}"


def _admissions_csv(*rows) -> bytes:
    return ("\n".join(["EncID,MRN,Dept,AdmitDate", *rows]) + "\n").encode()


def test_a_history_run_and_its_patients_are_at_the_importing_accounts_facility(world):
    system, mrn = _system(), _mrn("HIP")
    data = (f"{PATIENT_HEADER}\nEXT-{mrn},{mrn},Anselm Wickersham\n").encode()
    result = hi.apply_import(data, "patients", PATIENT_MAPPING, source_system=system,
                             actor_user_id=world["users"]["admin_f2"])
    assert result["summary"]["created"] == 1
    with db_session() as db:
        patient = db.execute("SELECT id, registered_facility_id FROM patients WHERE mrn = ?",
                             (mrn,)).fetchone()
        run = db.execute("SELECT facility_id FROM history_import_runs WHERE id = ?",
                         (result["run_id"],)).fetchone()
    world["patients"].append(patient["id"])
    assert patient["registered_facility_id"] == F2 and run["facility_id"] == F2
    listed = next(r for r in hi.list_runs(500) if r["id"] == result["run_id"])
    assert listed["facility_id"] == F2


def test_history_rows_for_another_facilitys_patient_fail_into_the_ledger(world):
    pid = _patient(world, "Gwendolyn Marchbanks")
    _admission(world, pid, DEPT_F1, 1)
    with db_session() as db:
        mrn = db.execute("SELECT mrn FROM patients WHERE id = ?", (pid,)).fetchone()[0]
    data = _admissions_csv(f"ENC-{mrn},{mrn},{DEPT_F1},2024-03-01")
    planned = hi.plan(data, "admissions", ADMISSION_MAPPING, source_system=_system(),
                      actor_user_id=world["users"]["admin_f2"])
    assert planned["counts"]["failed"] == 1
    failure = planned["failures"][0]
    assert failure["reason"] == "facility_scope" and failure["patient_id"] is None


def test_a_past_admission_never_splits_a_patient_across_facilities(world):
    pid = _patient(world, "Thaddeus Quarrington")
    _admission(world, pid, DEPT_F2, F2)
    with db_session() as db:
        mrn = db.execute("SELECT mrn FROM patients WHERE id = ?", (pid,)).fetchone()[0]
    system = _system()
    # 'system' is group-wide; its run is at facility 1. A past admission with
    # no listed department would be filed at facility 1: refused.
    data = _admissions_csv(f"ENC1-{mrn},{mrn},Old General Ward,2023-01-05",
                           f"ENC2-{mrn},{mrn},{DEPT_F2},2023-06-05")
    result = hi.apply_import(data, "admissions", ADMISSION_MAPPING, source_system=system,
                             actor_user_id="system")
    assert result["summary"]["created"] == 1 and result["summary"]["failed"] == 1
    assert {f["reason"] for f in result["failures"]} == {"cross_facility_move_refused"}
    with db_session() as db:
        stamped = {r[0] for r in db.execute(
            "SELECT facility_id FROM admissions WHERE patient_id = ?", (pid,))}
        rejects = [r[0] for r in db.execute(
            "SELECT code FROM history_import_rejects WHERE run_id = ?", (result["run_id"],))]
    assert stamped == {F2}
    assert rejects == ["cross_facility_move_refused"]
    assert _split_patients([pid]) == 0


def test_a_history_dry_run_with_more_than_one_facility_needs_the_account(world):
    data = (f"{PATIENT_HEADER}\nEXT-X,{_mrn('HIX')},Perpetua Lindqvist\n").encode()
    with pytest.raises(ValueError, match="account running the import"):
        hi.plan(data, "patients", PATIENT_MAPPING, source_system=_system())


def test_with_one_facility_a_history_run_is_unchanged(world, one_facility):
    system, mrn = _system(), _mrn("HI1")
    data = (f"{PATIENT_HEADER}\nEXT-{mrn},{mrn},Evander Coldridge\n").encode()
    hi.plan(data, "patients", PATIENT_MAPPING, source_system=system)   # no account needed
    result = hi.apply_import(data, "patients", PATIENT_MAPPING, source_system=system,
                             actor_user_id=world["users"]["admin_f2"])
    with db_session() as db:
        patient = db.execute("SELECT id, registered_facility_id FROM patients WHERE mrn = ?",
                             (mrn,)).fetchone()
        run = db.execute("SELECT facility_id FROM history_import_runs WHERE id = ?",
                         (result["run_id"],)).fetchone()
    world["patients"].append(patient["id"])
    assert patient["registered_facility_id"] == 1 and run["facility_id"] == 1
    listed = next(r for r in hi.list_runs(500) if r["id"] == result["run_id"])
    assert "facility_id" not in listed


# ── the backend quality gate: every function this change touches is typed ──
def _changed_functions():
    """Every function multisite-4 added or changed in a production module. The
    backend quality gate (tools/backend-quality) wants each one annotated in
    full: every parameter but self, and the return."""
    from routes import admin_routes, roster_import_routes

    return [
        AR._appointment_facility_on,
        AR.persist_appointment,
        AR._book_slot_on,
        AR.reschedule_appointment,
        admin_routes.history_import_dry_run,
        roster_import_routes.dry_run,
        fd.ensure_frontdesk_schema,
        fd.opd_visit_facility_on,
        fd.department_facility_on,
        fd.register_opd_visit,
        hi._Resolver.__init__,
        hi._Resolver.prefetch,
        hi.plan,
        hi._facility_context,
        hi._fail_for_facility,
        hi._check_facility,
        hi._write_row,
        hi._write_patient,
        hi._open_run,
        hi.list_runs,
        ri.plan,
        ri._facility_context,
        ri._refuse_for_facility,
        ri._check_facilities,
        ri._audit_facility_refusals,
        ri._overtaken_by_another_facility,
        ri._absent_from_roster,
        ri.apply_import,
        ri.list_runs,
        sh._dispatch_to_transfer,
        se.confirm_surgery_prep,
        ss.surgery_facility_on,
        M74.ensure_opd_visit_facility_on,
        M74.add_facility_columns,
        M74.map_labels,
        M74.apply,
    ]


def test_every_function_multisite_4_touches_is_annotated_in_full():
    import inspect

    gaps = []
    for fn in _changed_functions():
        sig = inspect.signature(fn)
        for name, param in sig.parameters.items():
            if name in ("self", "cls"):
                continue
            if param.annotation is inspect.Parameter.empty:
                gaps.append(f"{fn.__qualname__}({name})")
        if sig.return_annotation is inspect.Signature.empty:
            gaps.append(f"{fn.__qualname__} -> ?")
    assert gaps == []


def test_the_annotated_routes_answer_the_same_shape():
    """A route's return annotation becomes its response model in FastAPI, which
    then checks and re-serialises every answer. The three routes multisite-4
    annotated return a plain mapping, so the model must be a plain dict that
    lets every key and value through unchanged."""
    from typing import Any, get_args, get_origin

    from routes import admin_routes, roster_import_routes

    wanted = {
        AR.reschedule_appointment: dict[str, Any],
        admin_routes.history_import_dry_run: dict,
        roster_import_routes.dry_run: dict,
    }
    routers = (AR.router, admin_routes.router, roster_import_routes.router)
    found = {}
    for router in routers:
        for route in router.routes:
            if getattr(route, "endpoint", None) in wanted:
                found[route.endpoint] = route
    assert set(found) == set(wanted)
    sample = {
        "appointment": {"id": "A-1", "facility_id": None, "slot_position": 2},
        "previous": {"id": "A-0", "status": "cancelled"},
        "replayed": False,
        "counts": {"create": 3, "update": 0},
        "rows": [{"row": 2, "issues": []}],
    }
    for endpoint, model in wanted.items():
        route = found[endpoint]
        assert route.response_model == model
        assert get_origin(route.response_model) in (None, dict)
        assert get_args(route.response_model) in ((), (str, Any))
        assert route.response_field is not None
        assert route.response_field.serialize(sample, mode="json") == sample
