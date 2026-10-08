"""The laboratory's analyser registry (lis-23): migration 64, keys, code map.

Pinned here:
  * migration 64 is collected, idempotent on a fresh database and on one from
    before it, has no FOREIGN KEY, no REAL and no CURRENT_TIMESTAMP, and the
    database itself refuses a switched-on analyser with no key, two live codes
    for one test on one analyser and two codes differing only in capitals;
    the restore manifest lists both tables;
  * the key is shown once and never readable again: only its digest is
    stored, and no read, list or audit row carries the key or the digest;
  * a new key invalidates the old one at once, and replacing one needs a
    reason; an unknown id, no key and a wrong key get one answer; a right key
    on a switched-off analyser is refused separately;
  * strict defaults: a new analyser starts switched off, switching on needs a
    key and a reason, only HL7 over HTTP or MLLP is accepted, an internet
    address is refused, a code maps only to an active laboratory analyte,
    one live code per test, the factor is exact in thousandths;
  * a map change by a technician (or the administrator) is 403; registering
    by a pathologist is 403; every change is audited as clinical_config, and
    a write whose audit fails commits nothing;
  * the API: 404 while the module is off, reads open and writes refused while
    it drains, unknown fields refused, the key answer is never cached.
"""
from __future__ import annotations

import hmac
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import main
import migrations
import migrations_his
from auth import create_token, hash_password
from database import db_session
from db_backend import DBIntegrityError
from migrations_his import m0055_lab_contract as m55
from migrations_his import m0064_lab_analysers as m64
from services import lab_instrument_service as lis
from services import lab_master_service as lm
from services.audit_service import AuditWriteError

client = TestClient(main.app)

ADMIN = {"id": "LIADMIN1", "role": "admin"}
PATHOLOGIST = {"id": "LIPATH01", "role": "pathologist"}
TECHNICIAN = {"id": "LITECH01", "role": "lab_technician"}

HAEMOGLOBIN = "LOINC:718-7"
POTASSIUM = "LOINC:2823-3"
CBC_PANEL = "LOINC:58410-2"
KETONES = "LOCAL:ORB-LAB-BLOOD-KETONES"
BLOOD_CULTURE = "LOCAL:ORB-LAB-BLOOD-CULTURE"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _starter_loaded():
    """The starter catalogue (sections and tests), as the module's startup
    loads it. Idempotent."""
    lm.load_starter_catalogue()
    yield


def _new_id() -> str:
    return f"T-{uuid.uuid4().hex[:10].upper()}"


@pytest.fixture
def analyser():
    """A registered analyser of this test's own, removed afterwards."""
    ident = _new_id()
    created = lis.create_instrument({"id": ident, "name": f"Haematology analyser {ident}",
                                     "section_id": "haematology", "make": "Make",
                                     "model": "Model 1", "host": "192.168.10.21"}, actor=ADMIN)
    yield created
    with db_session() as db:
        db.execute("DELETE FROM lab_instrument_test_map WHERE instrument_id = ?", (ident,))
        db.execute("DELETE FROM lab_instruments WHERE id = ?", (ident,))


def _hdr(role):
    uid = f"LI{uuid.uuid4().hex[:6].upper()}"
    dept = "General Medicine" if role in ("doctor", "nurse", "ward_clerk") else None
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, dept, hash_password("password")))
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _stored_digest(ident):
    with db_session() as db:
        return db.execute("SELECT credential_hash FROM lab_instruments WHERE id = ?",
                          (ident,)).fetchone()[0]


def _authenticate(ident, key):
    with db_session() as db:
        return lis.authenticate(db, ident, key)


def _audit_rows(ident):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT event_category, action_type, actor_user_id, summary_message, metadata_json "
            "FROM audit_logs WHERE target_type = 'lab_instrument' AND target_id = ? ORDER BY id",
            (ident,)).fetchall()]


# ── 1. Migration 64 ──────────────────────────────────────────────────────────

def test_migration_64_is_collected_and_in_the_session_ledger():
    assert {m.version: m.name for m in migrations_his.collect()}[64] == "lab_analysers"
    assert {m.version: m.name for m in migrations.MIGRATIONS}[64] == "lab_analysers"
    with db_session() as db:
        assert db.execute("SELECT 1 FROM schema_migrations WHERE version = 64").fetchone()


def test_both_tables_have_no_foreign_key_no_real_and_no_current_timestamp():
    with db_session() as db:
        for table in m64.TABLE_NAMES:
            assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall() == [], table
            for r in db.execute(f"PRAGMA table_info({table})").fetchall():
                assert not any(w in (r[2] or "").upper() for w in ("REAL", "FLOA", "DOUB")), (table, r[1])
            ddl = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
            assert "CURRENT_TIMESTAMP" not in ddl.upper(), table


def _scratch(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    return conn


def test_migration_64_runs_twice_on_a_fresh_database_and_on_one_from_before_it(tmp_path):
    fresh = _scratch(tmp_path / "fresh.db")
    m64.apply(fresh)
    m64.apply(fresh)
    assert {"lab_instruments", "lab_instrument_test_map"} <= {
        r[0] for r in fresh.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    older = _scratch(tmp_path / "older.db")
    m55.apply(older)
    older.execute("INSERT INTO lab_sections (id, name) VALUES ('haematology', 'Haematology')")
    m64.apply(older)
    older.execute(
        "INSERT INTO lab_instruments (id, name, created_at, created_by, updated_at, updated_by) "
        "VALUES ('XN-1', 'XN', 't', 'u', 't', 'u')")
    m64.apply(older)
    assert older.execute("SELECT COUNT(*) FROM lab_instruments").fetchone()[0] == 1
    assert older.execute("SELECT COUNT(*) FROM lab_sections").fetchone()[0] == 1
    indexes = {r[0] for r in older.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'lab_instrument_test_map'")}
    assert {"ux_lab_instrument_test_map_live_test", "ux_lab_instrument_test_map_code_nocase",
            "idx_lab_instrument_test_map_test"} <= indexes


def test_the_database_refuses_what_no_writer_may_store(tmp_path):
    db = _scratch(tmp_path / "checks.db")
    m64.apply(db)
    instrument = ("INSERT INTO lab_instruments (id, name, enabled, credential_hash, "
                  "credential_rotated_at, credential_rotated_by, created_at, created_by, "
                  "updated_at, updated_by) VALUES (?, 'x', ?, ?, ?, ?, 't', 'u', 't', 'u')")
    with pytest.raises(DBIntegrityError):
        db.execute(instrument, ("A1", 1, None, None, None))      # on with no key
    with pytest.raises(DBIntegrityError):
        db.execute(instrument, ("A1", 0, "sha256:ab", None, None))   # a key with no issuer
    db.execute(instrument, ("A1", 1, "sha256:ab", "t", "u"))
    mapping = ("INSERT INTO lab_instrument_test_map (instrument_id, instrument_code, test_id, "
               "active, note, approved_by, approved_at, retired_by, retired_at, retire_reason) "
               "VALUES ('A1', ?, ?, ?, 'manual', 'P', 't', ?, ?, ?)")
    db.execute(mapping, ("HB", HAEMOGLOBIN, 1, None, None, None))
    with pytest.raises(DBIntegrityError):
        db.execute(mapping, ("hb", POTASSIUM, 1, None, None, None))        # a case twin
    with pytest.raises(DBIntegrityError):
        db.execute(mapping, ("HGB", HAEMOGLOBIN, 1, None, None, None))     # a second live code
    db.execute(mapping, ("HGB", HAEMOGLOBIN, 0, "P", "t", "old code"))     # a retired one is fine
    with pytest.raises(DBIntegrityError):
        db.execute(mapping, ("K", POTASSIUM, 0, "P", "t", " "))           # retired with no reason


def test_the_restore_manifest_lists_both_tables():
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "verify_restore.py"
    spec = importlib.util.spec_from_file_location("_vr_for_analyser_test", path)
    vr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vr)
    assert set(vr.MODULE_TABLES["lab analysers"]) == set(m64.TABLE_NAMES)


# ── 2. The key ───────────────────────────────────────────────────────────────

def test_a_new_analyser_starts_switched_off_with_no_key(analyser):
    assert analyser["enabled"] is False
    assert analyser["key"] == {"issued": False, "issued_at": None, "issued_by": None,
                               "issued_by_name": None}
    assert analyser["map"] == [] and analyser["mapped_codes"] == 0
    assert analyser["section_name"] == "Haematology"
    assert _stored_digest(analyser["id"]) is None


def test_the_key_is_shown_once_and_never_readable_again(analyser, monkeypatch):
    ident = analyser["id"]
    issued = lis.issue_key(ident, actor=ADMIN)
    key = issued["key"]
    assert len(key) >= 40 and issued["replaced"] is False
    digest = _stored_digest(ident)
    assert digest == lis.hash_key(key) and digest.startswith("sha256:") and key not in digest
    assert issued["instrument"]["key"]["issued"] is True
    assert "credential_hash" not in json.dumps(issued["instrument"])

    monkeypatch.setattr(config, "LAB_ENABLED", True)
    h = _hdr("lab_technician")
    for path in ("/api/lab/analysers", f"/api/lab/analysers/{ident}"):
        resp = client.get(path, headers=h)
        assert resp.status_code == 200, resp.text
        assert key not in resp.text and digest not in resp.text
        assert digest.split(":", 1)[1] not in resp.text
        assert "credential_hash" not in resp.text
    for row in _audit_rows(ident):
        text = json.dumps(row)
        assert key not in text and digest.split(":", 1)[1] not in text


def test_a_new_key_invalidates_the_old_one_at_once_and_needs_a_reason(analyser):
    ident = analyser["id"]
    first = lis.issue_key(ident, actor=ADMIN)["key"]
    lis.set_instrument_enabled(ident, True, "Commissioned by the biomedical team", actor=ADMIN)
    assert _authenticate(ident, first)["id"] == ident
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.issue_key(ident, actor=ADMIN)
    assert err.value.code == "reason_required"
    second = lis.issue_key(ident, actor=ADMIN, reason="Service engineer visit")
    assert second["replaced"] is True and second["key"] != first
    with pytest.raises(lis.InstrumentAuthError) as refused:
        _authenticate(ident, first)
    assert refused.value.code == "unauthenticated"
    assert _authenticate(ident, second["key"])["id"] == ident
    actions = [r["action_type"] for r in _audit_rows(ident)]
    assert "lab_instrument_credential_issued" in actions
    assert "lab_instrument_credential_rotated" in actions


def test_unknown_id_no_key_and_wrong_key_get_one_answer(analyser, monkeypatch):
    ident = analyser["id"]
    compared = []
    real = hmac.compare_digest
    monkeypatch.setattr(lis.hmac, "compare_digest", lambda a, b: compared.append(1) or real(a, b))
    for who, key in ((ident, "anything"), ("NO-SUCH-ANALYSER", "anything"), ("bad id!", "x"),
                     (ident, None), (ident, "")):
        with pytest.raises(lis.InstrumentAuthError) as err:
            _authenticate(who, key)
        assert err.value.code == "unauthenticated", who
    assert len(compared) == 5, "every refusal runs the same constant-time comparison"
    key = lis.issue_key(ident, actor=ADMIN)["key"]
    with pytest.raises(lis.InstrumentAuthError) as err:
        _authenticate(ident, key + "x")
    assert err.value.code == "unauthenticated"
    with pytest.raises(lis.InstrumentAuthError) as err:
        _authenticate(ident, key)
    assert err.value.code == "instrument_disabled", "the right key on a switched-off analyser"
    lis.set_instrument_enabled(ident, True, "Commissioned", actor=ADMIN)
    assert _authenticate(ident.lower(), key)["id"] == ident, "the header id is normalised"


def test_switching_on_needs_a_key_and_a_reason(analyser):
    ident = analyser["id"]
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.set_instrument_enabled(ident, True, "Ready", actor=ADMIN)
    assert err.value.code == "key_required"
    lis.issue_key(ident, actor=ADMIN)
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.set_instrument_enabled(ident, True, "  ", actor=ADMIN)
    assert err.value.code == "missing_field"
    on = lis.set_instrument_enabled(ident, True, "Commissioned", actor=ADMIN)
    assert on["enabled"] is True and on["status_reason"] == "Commissioned"
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.set_instrument_enabled(ident, True, "Again", actor=ADMIN)
    assert err.value.code == "no_change"
    off = lis.set_instrument_enabled(ident, False, "Sent for repair", actor=ADMIN)
    assert off["enabled"] is False
    actions = [r["action_type"] for r in _audit_rows(ident)]
    assert actions.count("lab_instrument_enabled") == 1 and actions.count("lab_instrument_disabled") == 1


# ── 3. What an analyser may be ───────────────────────────────────────────────

def test_ids_are_capitals_and_checked():
    ident = _new_id()
    try:
        made = lis.create_instrument({"id": ident.lower(), "name": "Chemistry"}, actor=ADMIN)
        assert made["id"] == ident
        with pytest.raises(lis.LabInstrumentError) as err:
            lis.create_instrument({"id": ident, "name": "Again"}, actor=ADMIN)
        assert err.value.status == 409
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_instruments WHERE id = ?", (ident,))
    for bad in ("x", "has space", "slash/id", "", None, "A" * 33):
        with pytest.raises(lis.LabInstrumentError) as err:
            lis.create_instrument({"id": bad, "name": "Bad"}, actor=ADMIN)
        assert err.value.code == "invalid_id", bad


def test_only_hl7_over_http_or_mllp_is_accepted(analyser):
    for field, value in (("protocol", "astm"), ("transport", "tcp_client"),
                         ("transport", "tcp_listen")):
        with pytest.raises(lis.LabInstrumentError) as err:
            lis.update_instrument(analyser["id"], {field: value}, actor=ADMIN)
        assert err.value.code == "not_supported", (field, value)
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.update_instrument(analyser["id"], {"protocol": "json"}, actor=ADMIN)
    assert err.value.code == "invalid_value"
    changed = lis.update_instrument(analyser["id"], {"transport": "mllp_listen"}, actor=ADMIN)
    assert changed["transport"] == "mllp_listen" and changed["version"] == analyser["version"] + 1


def test_an_internet_address_is_refused_and_a_hospital_one_kept(analyser):
    for host in ("8.8.8.8", "2001:4860:4860::8888"):
        with pytest.raises(lis.LabInstrumentError) as err:
            lis.update_instrument(analyser["id"], {"host": host}, actor=ADMIN)
        assert err.value.code == "host_not_local", host
    for host in ("http://10.0.0.5", "10.0.0.5/results", "bad host"):
        with pytest.raises(lis.LabInstrumentError) as err:
            lis.update_instrument(analyser["id"], {"host": host}, actor=ADMIN)
        assert err.value.code == "invalid_value", host
    assert lis.update_instrument(analyser["id"], {"host": "10.20.0.7"}, actor=ADMIN)["host"] == "10.20.0.7"
    assert lis.update_instrument(analyser["id"], {"host": "LAB-XN1.local"},
                                 actor=ADMIN)["host"] == "lab-xn1.local"
    with pytest.raises(lis.LabInstrumentError):
        lis.update_instrument(analyser["id"], {"port": 70000}, actor=ADMIN)


def test_the_time_zone_is_a_zone_name(analyser):
    for tz in ("+05:30", "Mars/Olympus", "local"):
        with pytest.raises(lis.LabInstrumentError) as err:
            lis.update_instrument(analyser["id"], {"assumed_timezone": tz}, actor=ADMIN)
        assert err.value.code == "invalid_value", tz
    assert lis.update_instrument(analyser["id"], {"assumed_timezone": "Asia/Kolkata"},
                                 actor=ADMIN)["assumed_timezone"] == "Asia/Kolkata"
    assert lis.update_instrument(analyser["id"], {"assumed_timezone": ""},
                                 actor=ADMIN)["assumed_timezone"] is None


def test_a_stale_edit_is_refused(analyser):
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.update_instrument(analyser["id"], {"name": "Renamed"}, actor=ADMIN,
                              expected_version=analyser["version"] + 5)
    assert err.value.code == "stale_version" and err.value.status == 409
    same = lis.update_instrument(analyser["id"], {"name": analyser["name"]}, actor=ADMIN)
    assert same["version"] == analyser["version"], "no change writes nothing"


def test_unknown_section_and_biomedical_entry_are_refused_and_calibration_shows(analyser):
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.update_instrument(analyser["id"], {"section_id": "astrology"}, actor=ADMIN)
    assert err.value.code == "unknown_reference"
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.update_instrument(analyser["id"], {"biomedical_asset_id": 987654321}, actor=ADMIN)
    assert err.value.code == "unknown_reference"
    with db_session() as db:
        asset_id = db.execute(
            "INSERT INTO biomedical_assets (asset_tag, name, category) VALUES (?, ?, 'lab_analyser')",
            (f"BME-{uuid.uuid4().hex[:6]}", "Haematology analyser")).lastrowid
        db.execute("INSERT INTO biomedical_schedules (asset_id, schedule_type, interval_days, "
                   "last_done_date, next_due_date, grace_days) VALUES (?, 'calibration', 180, "
                   "'2025-01-01', '2025-07-01', 7)", (asset_id,))
    try:
        linked = lis.update_instrument(analyser["id"], {"biomedical_asset_id": asset_id}, actor=ADMIN)
        bio = linked["biomedical"]
        assert bio["found"] is True and bio["name"] == "Haematology analyser"
        assert bio["calibration"]["next_due_date"] == "2025-07-01"
        assert bio["calibration"]["overdue"] is True
    finally:
        with db_session() as db:
            db.execute("UPDATE lab_instruments SET biomedical_asset_id = NULL WHERE id = ?",
                       (analyser["id"],))
            db.execute("DELETE FROM biomedical_schedules WHERE asset_id = ?", (asset_id,))
            db.execute("DELETE FROM biomedical_assets WHERE id = ?", (asset_id,))


# ── 4. The code map ──────────────────────────────────────────────────────────

def _map(ident, code, test_id, **extra):
    body = {"instrument_code": code, "test_id": test_id, "note": "Checked against the host manual"}
    body.update(extra)
    return lis.save_mapping(ident, body, actor=PATHOLOGIST)


def test_a_map_change_by_a_technician_or_the_administrator_is_refused(analyser):
    for actor in (TECHNICIAN, ADMIN, {"id": "D1", "role": "doctor"}, {}):
        with pytest.raises(lis.LabInstrumentError) as err:
            lis.save_mapping(analyser["id"], {"instrument_code": "HB", "test_id": HAEMOGLOBIN,
                                              "note": "x"}, actor=actor)
        assert err.value.status == 403
    _map(analyser["id"], "HB", HAEMOGLOBIN)
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.retire_mapping(analyser["id"], "HB", "Wrong", actor=TECHNICIAN)
    assert err.value.status == 403


def test_registry_writes_are_the_administrators_alone(analyser):
    for actor in (PATHOLOGIST, TECHNICIAN, {"id": "N1", "role": "nurse"}):
        with pytest.raises(lis.LabInstrumentError) as err:
            lis.create_instrument({"id": _new_id(), "name": "x"}, actor=actor)
        assert err.value.status == 403
        with pytest.raises(lis.LabInstrumentError) as err:
            lis.issue_key(analyser["id"], actor=actor)
        assert err.value.status == 403
    assert _stored_digest(analyser["id"]) is None


def test_a_code_maps_only_to_an_active_laboratory_analyte(analyser):
    ident = analyser["id"]
    for test_id, code in ((CBC_PANEL, "not_an_analyte"), (KETONES, "test_is_bedside"),
                          ("LOCAL:NO-SUCH-TEST", "unknown_reference")):
        with pytest.raises(lis.LabInstrumentError) as err:
            _map(ident, "X1", test_id)
        assert err.value.code == code, test_id
    own_code = f"ORB-LI-{uuid.uuid4().hex[:6].upper()}"
    own = lm.create_test({"code_system": "LOCAL", "code": own_code, "name": f"Switched off {own_code}",
                          "short_name": "Off test", "section_id": "biochemistry",
                          "specimen_type_code": "serum", "unit": "mmol/L"}, actor=PATHOLOGIST)
    try:
        lm.update_test(own["id"], {"active": False}, actor=PATHOLOGIST)
        with pytest.raises(lis.LabInstrumentError) as err:
            _map(ident, "X1", own["id"])
        assert err.value.code == "test_inactive"
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_tests WHERE id = ?", (own["id"],))
            db.execute("DELETE FROM terminology WHERE code_system = 'LOCAL' AND code = ?", (own_code,))
    with db_session() as db:
        assert lis.list_mappings(db, ident) == []


def test_one_live_code_per_test_and_no_codes_that_differ_only_in_capitals(analyser):
    ident = analyser["id"]
    _map(ident, "HB", HAEMOGLOBIN, instrument_unit="g/dL")
    with pytest.raises(lis.LabInstrumentError) as err:
        _map(ident, "HGB", HAEMOGLOBIN)
    assert err.value.code == "test_already_mapped" and err.value.status == 409
    with pytest.raises(lis.LabInstrumentError) as err:
        _map(ident, "hb", POTASSIUM)
    assert err.value.code == "code_case_twin"
    for bad in ("H|B", "H^B", "", "   "):
        with pytest.raises(lis.LabInstrumentError):
            _map(ident, bad, POTASSIUM)
    lis.retire_mapping(ident, "HB", "Analyser firmware renamed the code", actor=PATHOLOGIST)
    moved = _map(ident, "HGB", HAEMOGLOBIN)
    assert moved["active"] is True and moved["test_name"] == "Haemoglobin"
    with db_session() as db:
        assert lis.mapping_for(db, ident, "HGB")["test_id"] == HAEMOGLOBIN
        assert lis.mapping_for(db, ident, "HB") is None, "a retired code finds nothing"
        assert lis.mapping_for(db, ident, "hgb") is None, "a code is looked up exactly as sent"
        assert lis.mapping_for(db, ident.lower(), "HGB")["test_id"] == HAEMOGLOBIN


def test_the_factor_is_exact_in_thousandths_and_a_text_test_takes_none(analyser):
    ident = analyser["id"]
    with pytest.raises(lis.LabInstrumentError) as err:
        _map(ident, "GLU", "LOINC:2345-7", factor="0.0555")
    assert err.value.code == "precision"
    for bad in ("0", "-1", "abc", "1e400"):
        with pytest.raises(lis.LabInstrumentError):
            _map(ident, "GLU", "LOINC:2345-7", factor=bad)
    tenth = _map(ident, "GLU", "LOINC:2345-7", factor="0.1", instrument_unit="mg/L")
    assert tenth["factor_milli"] == 100 and tenth["factor"] == "0.1"
    assert _map(ident, "K", POTASSIUM)["factor"] == "1"
    with pytest.raises(lis.LabInstrumentError) as err:
        _map(ident, "BC", BLOOD_CULTURE, factor="2")
    assert err.value.code == "factor_not_numeric"
    with pytest.raises(lis.LabInstrumentError):
        _map(ident, "BC", BLOOD_CULTURE, instrument_unit="mg")
    assert _map(ident, "BC", BLOOD_CULTURE)["factor_milli"] == 1000


def test_a_mapping_needs_a_note_retiring_needs_a_reason_and_nothing_is_deleted(analyser):
    ident = analyser["id"]
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.save_mapping(ident, {"instrument_code": "K", "test_id": POTASSIUM, "note": " "},
                         actor=PATHOLOGIST)
    assert err.value.code == "missing_field"
    _map(ident, "K", POTASSIUM)
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.retire_mapping(ident, "K", "", actor=PATHOLOGIST)
    assert err.value.code == "missing_field"
    retired = lis.retire_mapping(ident, "K", "Moved to the chemistry analyser", actor=PATHOLOGIST)
    assert retired["active"] is False and retired["retire_reason"] == "Moved to the chemistry analyser"
    with pytest.raises(lis.LabInstrumentError) as err:
        lis.retire_mapping(ident, "K", "Again", actor=PATHOLOGIST)
    assert err.value.code == "no_change"
    back = _map(ident, "K", POTASSIUM)
    assert back["active"] is True and back["retire_reason"] is None and back["version"] == 3
    with db_session() as db:
        assert len(lis.list_mappings(db, ident)) == 1


def test_every_map_change_is_audited_and_a_repeat_writes_nothing(analyser):
    ident = analyser["id"]
    _map(ident, "K", POTASSIUM)
    _map(ident, "K", POTASSIUM)                                     # identical: no write
    _map(ident, "K", POTASSIUM, instrument_unit="mmol/L")          # a change
    lis.retire_mapping(ident, "K", "Wrong analyser", actor=PATHOLOGIST)
    rows = [r for r in _audit_rows(ident) if r["action_type"] == "lab_analyser_map_changed"]
    assert [json.loads(r["metadata_json"])["change"] for r in rows] == ["added", "changed", "retired"]
    assert all(r["event_category"] == "clinical_config" and r["actor_user_id"] == PATHOLOGIST["id"]
               for r in rows)
    first = json.loads(rows[0]["metadata_json"])
    assert first["before"] is None and first["after"]["test_id"] == POTASSIUM
    assert json.loads(rows[1]["metadata_json"])["after"]["instrument_unit"] == "mmol/L"


# ── 5. An audit that fails commits nothing ───────────────────────────────────

def test_a_write_whose_audit_fails_commits_nothing(analyser, monkeypatch):
    ident = analyser["id"]
    key = lis.issue_key(ident, actor=ADMIN)["key"]
    digest = _stored_digest(ident)

    def refuse(*a, **k):
        raise AuditWriteError("audit store unavailable")
    monkeypatch.setattr(lis, "log_audit_event", refuse)
    new_id = _new_id()
    with pytest.raises(AuditWriteError):
        lis.create_instrument({"id": new_id, "name": "Never stored"}, actor=ADMIN)
    with pytest.raises(AuditWriteError):
        lis.issue_key(ident, actor=ADMIN, reason="Rotation that must not happen")
    with pytest.raises(AuditWriteError):
        lis.set_instrument_enabled(ident, True, "Not audited", actor=ADMIN)
    with pytest.raises(AuditWriteError):
        lis.update_instrument(ident, {"name": "Renamed"}, actor=ADMIN)
    with pytest.raises(AuditWriteError):
        _map(ident, "HB", HAEMOGLOBIN)
    with db_session() as db:
        assert db.execute("SELECT 1 FROM lab_instruments WHERE id = ?", (new_id,)).fetchone() is None
        now = lis.get_instrument(db, ident)
        assert now["enabled"] is False and now["name"] == analyser["name"] and now["map"] == []
    assert _stored_digest(ident) == digest, "the old key still stands"
    monkeypatch.undo()
    lis.set_instrument_enabled(ident, True, "Commissioned", actor=ADMIN)
    assert _authenticate(ident, key)["id"] == ident


def test_the_api_says_audit_unavailable_and_changes_nothing(analyser, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    monkeypatch.setattr(lis, "log_audit_event",
                        lambda *a, **k: (_ for _ in ()).throw(AuditWriteError("down")))
    resp = client.post(f"/api/lab/analysers/{analyser['id']}/key", json={},
                       headers=_hdr("admin"))
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
    assert "key" not in resp.json()
    assert _stored_digest(analyser["id"]) is None


# ── 6. The API ───────────────────────────────────────────────────────────────

_ROUTES = (
    ("get", "/api/lab/analysers"), ("get", "/api/lab/analysers/XN-1"),
    ("post", "/api/lab/analysers"), ("patch", "/api/lab/analysers/XN-1"),
    ("post", "/api/lab/analysers/XN-1/status"), ("post", "/api/lab/analysers/XN-1/key"),
    ("put", "/api/lab/analysers/XN-1/map"), ("post", "/api/lab/analysers/XN-1/map/retire"),
)


def _call(method, path, headers, body=None):
    kwargs = {"headers": headers}
    if method != "get":
        kwargs["json"] = body or {}
    return getattr(client, method)(path, **kwargs)


def test_every_route_404s_while_the_module_is_off_and_401s_with_no_session():
    h = _hdr("admin")
    for method, path in _ROUTES:
        resp = _call(method, path, h)
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"
        assert _call(method, path, {}).status_code == 401, path


def test_while_draining_reads_answer_and_writes_404(monkeypatch, analyser):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    h = _hdr("admin")
    assert client.get("/api/lab/analysers", headers=h).status_code == 200
    assert client.get(f"/api/lab/analysers/{analyser['id']}", headers=h).status_code == 200
    resp = client.post(f"/api/lab/analysers/{analyser['id']}/key", json={}, headers=h)
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "module_disabled"
    assert _stored_digest(analyser["id"]) is None
    resp = client.put(f"/api/lab/analysers/{analyser['id']}/map", headers=_hdr("pathologist"),
                      json={"instrument_code": "K", "test_id": POTASSIUM, "note": "x"})
    assert resp.status_code == 404


def test_who_may_do_what_through_the_api(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    ident = _new_id()
    body = {"id": ident, "name": "Chemistry analyser", "section_id": "biochemistry",
            "transport": "mllp_listen", "host": "10.1.2.3"}
    try:
        for role in ("pathologist", "lab_technician", "doctor"):
            assert client.post("/api/lab/analysers", json=body, headers=_hdr(role)).status_code == 403
        created = client.post("/api/lab/analysers", json=body, headers=_hdr("admin"))
        assert created.status_code == 201, created.text
        assert created.json()["enabled"] is False
        for role in ("lab_technician", "pathologist", "phlebotomist", "admin"):
            listed = client.get("/api/lab/analysers", headers=_hdr(role))
            assert listed.status_code == 200, role
            assert any(a["id"] == ident for a in listed.json()["analysers"])
        options = listed.json()["options"]
        assert options["http_intake_built"] is False
        assert options["listener"] == {"enabled": False, "port": config.LAB_MLLP_PORT}
        assert [p["value"] for p in options["protocols"] if p["supported"]] == ["hl7v2"]
        for role in ("nurse", "pharmacist", "receptionist"):
            assert client.get("/api/lab/analysers", headers=_hdr(role)).status_code == 403, role

        mapping = {"instrument_code": "K", "test_id": POTASSIUM, "note": "Host manual rev 4"}
        for role in ("lab_technician", "admin"):
            resp = client.put(f"/api/lab/analysers/{ident}/map", json=mapping, headers=_hdr(role))
            assert resp.status_code == 403, role
        saved = client.put(f"/api/lab/analysers/{ident}/map", json=mapping, headers=_hdr("pathologist"))
        assert saved.status_code == 200, saved.text
        assert saved.json()["test_name"] == "Potassium"

        key = client.post(f"/api/lab/analysers/{ident}/key", json={}, headers=_hdr("admin"))
        assert key.status_code == 200, key.text
        assert key.headers["cache-control"] == "no-store"
        assert len(key.json()["key"]) >= 40
        again = client.post(f"/api/lab/analysers/{ident}/key", json={}, headers=_hdr("admin"))
        assert again.status_code == 422 and again.json()["detail"]["error"] == "reason_required"
        detail = client.get(f"/api/lab/analysers/{ident}", headers=_hdr("pathologist")).json()
        assert detail["key"]["issued"] is True and key.json()["key"] not in json.dumps(detail)
        assert [m["instrument_code"] for m in detail["map"]] == ["K"]

        on = client.post(f"/api/lab/analysers/{ident}/status", headers=_hdr("admin"),
                         json={"enabled": True, "reason": "Commissioned"})
        assert on.status_code == 200 and on.json()["enabled"] is True
        assert client.get("/api/lab/analysers/NO-SUCH-ONE", headers=_hdr("admin")).status_code == 404
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_instrument_test_map WHERE instrument_id = ?", (ident,))
            db.execute("DELETE FROM lab_instruments WHERE id = ?", (ident,))


def test_unknown_fields_are_refused_and_a_refusal_keeps_its_code(monkeypatch, analyser):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    h = _hdr("admin")
    resp = client.patch(f"/api/lab/analysers/{analyser['id']}", headers=h,
                        json={"credential_hash": "sha256:00"})
    assert resp.status_code == 422, "unknown fields are refused, never ignored"
    resp = client.patch(f"/api/lab/analysers/{analyser['id']}", headers=h, json={"host": "1.1.1.1"})
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "host_not_local"
    resp = client.post("/api/lab/analysers", headers=h,
                       json={"id": analyser["id"], "name": "Twin"})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "duplicate"
