"""The facility spine (multisite-1, W7-J, 9 October 2026).

Migration 70 lays the spine every later multi-site increment builds on, and
nothing about it may change behaviour on a one-facility appliance (every
install today). These tests pin:

  * ORB_MULTI_SITE: a feature flag, off by default, with no prerequisite;
  * migration 70 on the session database (fresh) and on a hand-built legacy
    database: facility 1 MAIN, the four tables, the two columns reading 1 on
    every existing row, the two admissions indexes, users untouched, and an
    idempotent re-run;
  * the departments master's seeding order, so the spelling staff are
    recorded under wins over an admissions variant;
  * facility_service: the 30 s multi-facility cache, display names, and the
    one boot refusal (facility 1 missing or inactive), which nothing else
    triggers, not a second facility and not the switch;
  * /api/health's platform object, read without opening a connection;
  * the operations status page's Facilities check: an admissions spelling
    variant is reported, and a patient admitted at two facilities is urgent;
  * facility.read and facility.manage, the admin's alone;
  * GET /api/facilities/meta.

The fence itself (503 with multi-facility data) is in test_facility_fence.py.
Run with the test env, never the shipping .env.
"""
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import migrations  # noqa: F401  (import before the migration file, see migrations_his)
import permissions as P
from auth import create_token, hash_password
from database import db_session
from main import app
from migrations_his import m0070_facilities_spine as M70
from services import facility_service as FS
from services import ops_health_service

client = TestClient(app)


def _user(role: str, department=None) -> str:
    uid = f"MS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, department, hash_password("password")))
    return uid


def _hdr(role: str, department=None) -> dict:
    uid = _user(role, department)
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(autouse=True)
def _fresh_facility_cache():
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


# ── 1. The switch ────────────────────────────────────────────────────────────

def test_multi_site_is_a_feature_flag_that_defaults_off():
    assert config.FEATURE_FLAGS["multi_site"] == "ORB_MULTI_SITE"
    assert config._FEATURE_CONSTANTS["multi_site"] == "MULTI_SITE_ENABLED"
    assert "multi_site" not in config.FEATURE_PREREQUISITES
    assert config.MULTI_SITE_ENABLED is False
    assert config.feature_enabled("multi_site") is False
    assert config.feature_flags()["multi_site"] is False


@pytest.mark.parametrize("raw,on", [("1", True), ("true", True), (" On ", True), ("yes", True),
                                    ("", False), ("0", False), ("enabled", False)])
def test_the_switch_is_read_as_an_explicit_opt_in(monkeypatch, raw, on):
    monkeypatch.setenv("ORB_MULTI_SITE", raw)
    assert config._opt_in("ORB_MULTI_SITE") is on


def test_the_switch_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    assert config.feature_enabled("multi_site") is True
    assert config.switch_enabled("multi_site") is True


# ── 2. Migration 70 on the session database (a fresh database) ──────────────

def test_migration_70_is_ledgered_and_seeds_facility_one():
    with db_session() as db:
        assert 70 in {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
        rows = [dict(r) for r in db.execute("SELECT * FROM facilities WHERE id = 1")]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(M70.TABLES) <= tables
    assert len(rows) == 1
    one = rows[0]
    assert one["code"] == "MAIN" and one["name"] is None and one["is_active"] == 1
    assert one["audit_epoch_id"] is None
    assert one["uuid"] and len(one["uuid"]) == 36


def test_every_admission_and_patient_reads_facility_one():
    pid, aid = f"MSP{uuid.uuid4().hex[:6]}", f"MSA{uuid.uuid4().hex[:6]}"
    doc = _user("doctor", "General Medicine")
    with db_session() as db:
        # Written the way every existing writer writes: no facility columns.
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, 'Spine Test', ?, 'admitted')",
                   (pid, f"MRN-{pid}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, ?, 'General Medicine', 'discharged')", (aid, pid, doc))
    try:
        with db_session() as db:
            assert db.execute("SELECT registered_facility_id FROM patients WHERE id = ?",
                              (pid,)).fetchone()[0] == 1
            assert db.execute("SELECT facility_id FROM admissions WHERE id = ?",
                              (aid,)).fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM admissions WHERE facility_id != 1").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM patients "
                              "WHERE registered_facility_id != 1").fetchone()[0] == 0
    finally:
        with db_session() as db:
            db.execute("DELETE FROM admissions WHERE id = ?", (aid,))
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))


def test_the_columns_and_indexes_are_in_place():
    with db_session() as db:
        pcols = {r[1]: r for r in db.execute("PRAGMA table_info(patients)")}
        acols = {r[1]: r for r in db.execute("PRAGMA table_info(admissions)")}
        indexes = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='admissions'")}
    for cols, name in ((pcols, "registered_facility_id"), (acols, "facility_id")):
        col = cols[name]
        assert col[2] == "INTEGER" and col[3] == 1 and col[4] == "1"
    assert M70.ADMISSION_INDEX_NAMES <= indexes


def test_run_pending_is_idempotent_on_the_session_database():
    with db_session() as db:
        assert migrations.run_pending_migrations(db) == []
        M70.apply(db)  # a ledger reset would re-run it: still one facility 1
        assert db.execute("SELECT COUNT(*) FROM facilities WHERE id = 1").fetchone()[0] == 1


def test_no_reference_and_no_users_column():
    with db_session() as db:
        for table in M70.TABLES:
            assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall() == [], table
        users = [r[1] for r in db.execute("PRAGMA table_info(users)")]
    assert not any("facility" in c for c in users)
    for ddl in (M70.FACILITIES_DDL, M70.DEPARTMENTS_DDL, M70.USER_FACILITIES_DDL,
                M70.ENTERPRISE_ACCESS_GRANTS_DDL):
        assert "REFERENCES" not in ddl.upper()


def test_the_schema_refuses_what_the_design_forbids():
    with db_session() as db:
        with pytest.raises(Exception):  # code charset: A-Z0-9 only
            db.execute("INSERT INTO facilities (uuid, code, name) VALUES (?, 'f-2', 'X')",
                       (str(uuid.uuid4()),))
    with db_session() as db:
        with pytest.raises(Exception):  # any facility but 1 must have a name
            db.execute("INSERT INTO facilities (uuid, code, name) VALUES (?, 'F2', NULL)",
                       (str(uuid.uuid4()),))
    with db_session() as db:
        with pytest.raises(Exception):  # the code is unique ignoring case
            db.execute("INSERT INTO facilities (uuid, code, name) VALUES (?, 'MAIN', 'X')",
                       (str(uuid.uuid4()),))
    with db_session() as db:
        with pytest.raises(Exception):  # a home row carries no department
            db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, department, "
                       "granted_at) VALUES ('U1', 1, 'home', 'ICU', '2026-10-09 00:00:00')")
    with db_session() as db:
        with pytest.raises(Exception):  # a grant needs a reason
            db.execute("INSERT INTO enterprise_access_grants (user_id, granted_at, reason) "
                       "VALUES ('U1', '2026-10-09 00:00:00', '  ')")
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM facilities").fetchone()[0] == 1


# ── 3. Migration 70 on a legacy database, and the departments master ────────

def _legacy(tmp_path, *, extra=True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / f"legacy-{uuid.uuid4().hex[:6]}.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, "
                 "department TEXT, password_hash TEXT NOT NULL, is_active INTEGER DEFAULT 1)")
    conn.execute("CREATE TABLE patients (id TEXT PRIMARY KEY, name TEXT, mrn TEXT UNIQUE, status TEXT)")
    conn.execute("CREATE TABLE admissions (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, "
                 "admitted_by TEXT NOT NULL, department TEXT, status TEXT DEFAULT 'active', "
                 "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    users = [("U1", "doctor", "Cardiology", 1), ("U2", "nurse", "cardiology", 1),
             ("U3", "receptionist", "ORTHOPAEDICS", 1), ("U4", "doctor", "Orthopaedics", 1),
             ("U5", "doctor", "Psychiatry", 0), ("U6", "admin", None, 1), ("U7", "nurse", "  ", 1)]
    for uid, role, dept, active in users:
        conn.execute("INSERT INTO users VALUES (?, ?, ?, ?, 'x', ?)", (uid, uid, role, dept, active))
    for i, dept in enumerate(["CARDIOLOGY", "Oncology", "Cardiology", None]):
        conn.execute("INSERT INTO patients VALUES (?, ?, ?, 'admitted')", (f"P{i}", f"P{i}", f"M{i}"))
        conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, department) "
                     "VALUES (?, ?, 'U1', ?)", (f"A{i}", f"P{i}", dept))
    if extra:
        conn.execute("CREATE TABLE wards (id TEXT PRIMARY KEY, department TEXT NOT NULL)")
        conn.execute("INSERT INTO wards VALUES ('W1', 'Intensive Care')")
        conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL)")
        conn.execute("INSERT INTO channels VALUES ('dept-er', 'Emergency', 'department')")
        conn.execute("INSERT INTO channels VALUES ('case-1', 'Bed 4 case room', 'case_room')")
        conn.execute("CREATE TABLE opd_visits (id TEXT PRIMARY KEY, department TEXT NOT NULL)")
        conn.execute("INSERT INTO opd_visits VALUES ('V1', 'Dermatology')")
    conn.commit()
    return conn


def _site_departments(monkeypatch, names):
    from services import site_config_service as scs
    cfg = scs.SiteConfig(present=True, site_name="Spine General",
                         departments=[{"name": n, "wards": []} for n in names])
    monkeypatch.setattr(scs, "current_site_config", lambda: cfg)


def test_seeding_order_users_then_site_file_then_admissions(tmp_path, monkeypatch):
    _site_departments(monkeypatch, ["Radiology", "oncology"])
    conn = _legacy(tmp_path)
    users_before = [tuple(r) for r in conn.execute("PRAGMA table_info(users)")]
    M70.apply(conn)
    conn.commit()
    rows = {r["name"]: r["seeded_from"] for r in conn.execute("SELECT * FROM departments")}
    assert rows == {
        "Cardiology": "users",          # doctor 'Cardiology' sorts before nurse 'cardiology'
        "Orthopaedics": "users",        # covering roles before the receptionist's capitals
        "Radiology": "site_config",
        "oncology": "site_config",      # the site file before the admissions' 'Oncology'
        "Intensive Care": "wards",
        "Emergency": "relay_channel",   # department channels only, not the case room
        "Dermatology": "opd_visits",
    }
    # An inactive account's department is not seeded from users, and a blank
    # department is no department.
    assert "Psychiatry" not in rows
    assert all(n == n.strip() and n for n in rows)
    # Every row belongs to facility 1, and users is untouched.
    assert {r[0] for r in conn.execute("SELECT DISTINCT facility_id FROM departments")} == {1}
    assert [tuple(r) for r in conn.execute("PRAGMA table_info(users)")] == users_before


def test_the_migration_is_additive_and_idempotent_on_a_legacy_database(tmp_path, monkeypatch):
    _site_departments(monkeypatch, [])
    conn = _legacy(tmp_path)
    admissions_before = [tuple(r) for r in conn.execute("SELECT * FROM admissions ORDER BY id")]
    M70.apply(conn)
    conn.commit()
    first = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]
             for t in M70.TABLES}
    schema = sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master"))
    M70.apply(conn)
    conn.commit()
    assert {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]
            for t in M70.TABLES} == first
    assert sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master")) == schema
    after = [tuple(r)[:len(admissions_before[0])]
             for r in conn.execute("SELECT * FROM admissions ORDER BY id")]
    assert after == admissions_before
    assert {r[0] for r in conn.execute("SELECT facility_id FROM admissions")} == {1}
    assert {r[0] for r in conn.execute("SELECT registered_facility_id FROM patients")} == {1}
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_a_database_without_the_optional_tables_migrates(tmp_path, monkeypatch):
    _site_departments(monkeypatch, [])
    conn = _legacy(tmp_path, extra=False)
    M70.apply(conn)
    conn.commit()
    names = {r[0] for r in conn.execute("SELECT name FROM departments")}
    assert names == {"Cardiology", "Orthopaedics", "Oncology"}


def test_an_admissions_spelling_variant_is_reported_and_never_rewritten(tmp_path, monkeypatch):
    _site_departments(monkeypatch, [])
    conn = _legacy(tmp_path)
    M70.apply(conn)
    conn.commit()
    report = FS.facility_report(conn)
    variants = {(v["where"], v["spelling"]): (v["master_spelling"], v["rows"])
                for v in report["department_variants"]}
    assert variants[("admissions", "CARDIOLOGY")] == ("Cardiology", 1)
    assert variants[("staff accounts", "cardiology")] == ("Cardiology", 1)
    assert ("admissions", "Cardiology") not in variants
    # The admission keeps its spelling: reported, never rewritten.
    assert conn.execute("SELECT department FROM admissions WHERE id = 'A0'").fetchone()[0] == "CARDIOLOGY"
    assert report["unknown_facility_ids"] == {}
    assert report["patients_at_several_facilities"] == 0
    assert report["facility_one_problem"] is None


def test_the_report_counts_unknown_facility_ids_and_split_patients(tmp_path, monkeypatch):
    _site_departments(monkeypatch, [])
    conn = _legacy(tmp_path)
    M70.apply(conn)
    conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, facility_id) "
                 "VALUES ('A9', 'P0', 'U1', 'Cardiology', 7)")
    conn.commit()
    report = FS.facility_report(conn)
    assert report["unknown_facility_ids"] == {"admissions": 1}
    assert report["patients_at_several_facilities"] == 1


# ── 4. facility_service ──────────────────────────────────────────────────────

def test_one_facility_is_not_multi_facility_data():
    assert FS.multi_facility_data() is False
    assert FS.cached_multi_facility_data() is False


def test_the_answer_is_cached_for_thirty_seconds(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(FS, "_clock", lambda: now[0])
    calls = []
    real = FS.facility_count
    monkeypatch.setattr(FS, "facility_count", lambda db: calls.append(1) or real(db))
    FS.invalidate_cache()
    assert FS.multi_facility_data() is False
    now[0] += 29
    assert FS.multi_facility_data() is False
    assert len(calls) == 1
    now[0] += 2
    FS.multi_facility_data()
    assert len(calls) == 2
    FS.invalidate_cache()
    FS.multi_facility_data()
    assert len(calls) == 3


def test_a_failed_read_keeps_the_last_answer(monkeypatch):
    def boom(db):
        raise RuntimeError("locked")
    FS.invalidate_cache()
    monkeypatch.setattr(FS, "facility_count", boom)
    assert FS.multi_facility_data() is False  # never known: False, not 503s everywhere


def test_display_name_falls_back_to_the_site_file_then_this_hospital(monkeypatch):
    _site_departments(monkeypatch, [])
    assert FS.display_name() == "Spine General"
    from services import site_config_service as scs
    monkeypatch.setattr(scs, "current_site_config", lambda: scs.SiteConfig())
    assert FS.display_name() == "This hospital"
    assert FS.display_name(facility={"id": 1, "name": "  Sunrise Hospital "}) == "Sunrise Hospital"
    assert FS.display_name(2, facility={"id": 2, "code": "SAT", "name": None}) == "SAT"


def test_the_boot_refusal_is_only_for_facility_one(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "boot.db"))
    assert "missing" in FS.facility_one_problem(conn)
    conn.execute(M70.FACILITIES_DDL)
    assert "Facility 1 (MAIN) is missing" in FS.facility_one_problem(conn)
    M70.seed_facility_one(conn)
    assert FS.facility_one_problem(conn) is None
    # A second facility, active or not, is never a reason to refuse a boot.
    conn.execute("INSERT INTO facilities (uuid, code, name, is_active) VALUES (?, 'SAT', 'Satellite', 0)",
                 (str(uuid.uuid4()),))
    assert FS.facility_one_problem(conn) is None
    FS.assert_facility_one(conn)
    conn.execute("UPDATE facilities SET is_active = 0 WHERE id = 1")
    assert "inactive" in FS.facility_one_problem(conn)
    with pytest.raises(FS.FacilitySpineError, match="will not start"):
        FS.assert_facility_one(conn)


def test_the_app_refuses_to_start_without_facility_one(monkeypatch):
    import main
    monkeypatch.setattr(FS, "facility_one_problem", lambda db: "Facility 1 (MAIN) is missing.")
    with pytest.raises(FS.FacilitySpineError):
        with TestClient(main.app):
            pass


def test_the_app_starts_with_two_facilities_and_the_switch_off(second_facility):
    import main
    assert config.feature_enabled("multi_site") is False
    with TestClient(main.app) as c:
        assert c.get("/api/health").json()["platform"]["multi_facility_data"] is True


@pytest.fixture()
def second_facility():
    """A second facilities row inserted by SQL, removed after (rows are never
    deleted in the product; the session database must go back to one)."""
    with db_session() as db:
        cur = db.execute("INSERT INTO facilities (uuid, code, name) VALUES (?, 'SPINE2', 'Spine Two')",
                         (str(uuid.uuid4()),))
        fid = cur.lastrowid
    FS.invalidate_cache()
    try:
        yield fid
    finally:
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (fid,))
        FS.invalidate_cache()


# ── 5. /api/health ───────────────────────────────────────────────────────────

def test_health_publishes_the_platform_object_and_the_same_modules():
    body = client.get("/api/health").json()
    assert body["platform"] == {"multi_site": False, "multi_facility_data": False}
    assert body["modules"] == config.enabled_modules()
    assert body["features"]["multi_site"] is False


def test_health_follows_the_switch(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    body = client.get("/api/health").json()
    assert body["platform"]["multi_site"] is True
    assert body["features"]["multi_site"] is True


def test_health_reads_the_platform_object_without_a_connection(monkeypatch):
    import database
    FS.prime_cache()

    def refuse(*a, **k):
        raise AssertionError("/api/health opened a database connection")

    monkeypatch.setattr(database, "db_session", refuse)
    monkeypatch.setattr(database, "get_db", refuse)
    FS.invalidate_cache()  # even a stale cache is not refreshed by health
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["platform"]["multi_facility_data"] is False


# ── 6. The operations status page ───────────────────────────────────────────

def _facilities_check() -> dict:
    payload = ops_health_service.build_status()
    return next(c for c in payload["checks"] if c["key"] == "facilities")


def test_the_status_page_reports_one_facility_in_words():
    check = _facilities_check()
    assert check["status"] == "ok", check
    assert check["summary"].startswith("One facility is on record (")
    assert "code MAIN" in check["summary"]
    assert check["detail"]["facility_fence"]["holding"] is False
    assert check["detail"]["facility_fence"]["every_patient_screen"] is True


def test_the_status_page_reports_an_admissions_variant():
    pid, aid = f"MSV{uuid.uuid4().hex[:6]}", f"MSVA{uuid.uuid4().hex[:6]}"
    doc = _user("doctor", "Spine Variant Ward")
    with db_session() as db:
        db.execute("INSERT INTO departments (name, seeded_from) VALUES ('Spine Variant Ward', 'test')")
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, 'Variant Test', ?, 'admitted')",
                   (pid, f"MRN-{pid}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, ?, 'spine variant ward', 'discharged')", (aid, pid, doc))
    try:
        check = _facilities_check()
        assert check["status"] == "attention"
        assert "'spine variant ward' on 1 admissions (listed as 'Spine Variant Ward')" in check["summary"]
        assert check["what_to_do"]
        assert "Variant Test" not in str(check) and pid not in str(check)
    finally:
        with db_session() as db:
            db.execute("DELETE FROM admissions WHERE id = ?", (aid,))
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))
            db.execute("DELETE FROM departments WHERE name = 'Spine Variant Ward'")
            db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (doc,))


def test_a_patient_admitted_at_two_facilities_is_urgent(second_facility):
    pid = f"MST{uuid.uuid4().hex[:6]}"
    doc = _user("doctor", "General Medicine")
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, 'Split Test', ?, 'discharged')",
                   (pid, f"MRN-{pid}"))
        for i, fid in enumerate((1, second_facility)):
            db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                       "facility_id) VALUES (?, ?, ?, 'General Medicine', 'discharged', ?)",
                       (f"{pid}-A{i}", pid, doc, fid))
    try:
        check = _facilities_check()
        assert check["status"] == "urgent"
        assert check["summary"].startswith("1 patient record(s) have admissions at more than one facility")
        assert check["detail"]["facility_fence"]["holding"] is True
        assert any("2 facilities are on record" in n for n in check["notes"])
        assert "Split Test" not in str(check) and pid not in str(check)
    finally:
        with db_session() as db:
            db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))


# ── 7. Permissions and the facilities route ─────────────────────────────────

def test_facility_permissions_are_the_admins_alone():
    assert P.FACILITY_READ == "facility.read" and P.FACILITY_MANAGE == "facility.manage"
    holders = {role for role, perms in P.ROLE_PERMISSIONS.items()
               if P.FACILITY_READ in perms or P.FACILITY_MANAGE in perms}
    assert holders == {"admin"}
    assert {P.FACILITY_READ, P.FACILITY_MANAGE} <= P.ROLE_PERMISSIONS["admin"]


def test_facilities_meta_for_the_admin_only():
    assert client.get("/api/facilities/meta").status_code == 401
    assert client.get("/api/facilities/meta", headers=_hdr("nurse", "General Medicine")).status_code == 403
    resp = client.get("/api/facilities/meta", headers=_hdr("admin"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["multi_site"] is False and body["multi_facility_data"] is False
    assert body["facility_count"] == 1
    assert body["main_facility"]["id"] == 1 and body["main_facility"]["code"] == "MAIN"
    assert body["main_facility"]["active"] is True
    assert isinstance(body["main_facility"]["name"], str) and body["main_facility"]["name"]


def test_the_restore_manifest_carries_the_spine():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "verify_restore.py"
    spec = importlib.util.spec_from_file_location("verify_restore_spine", path)
    vr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vr)
    assert set(M70.TABLES) <= set(vr.MODULE_TABLES["facilities"])
    assert vr.SEEDED_REFERENCE["facilities"] == 1
