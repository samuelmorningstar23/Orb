"""The hospital-system build's opt-in platform (his-1, wave 1, 3 October 2026).

One reader for every opt-in switch, default OFF; three opt-in modules published
inside /api/health "modules" with their state words; eleven feature flags under
"features", each off when a prerequisite is off; the settings each later
increment reads, with the strictest default on every held safety ruling; the
retention and erasure registries pre-registered for tables later waves create;
three new fail-closed audit categories.

With every switch off nothing old changes: the default-on modules read exactly
as before, and the only additions to /api/health are new keys whose values are
all off.
"""
import pytest
from fastapi.testclient import TestClient

import config
from database import db_session


# ── 1. The one reader ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (None, False), ("", False), ("   ", False), ("0", False), ("false", False),
    ("no", False), ("off", False), ("enabled", False), ("2", False),
    ("1", True), ("true", True), ("TRUE", True), (" yes ", True), ("on", True),
])
def test_an_opt_in_switch_is_on_only_for_the_four_words(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("ORB_LAB", raising=False)
    else:
        monkeypatch.setenv("ORB_LAB", raw)
    assert config._opt_in("ORB_LAB") is expected


def test_the_four_design_names_are_one_helper():
    assert config._opt_in_enabled is config._opt_in
    assert config._module_opt_in is config._opt_in
    assert config._feature_enabled is config._opt_in


# ── 2. Opt-in modules ─────────────────────────────────────────────────────────

def test_opt_in_modules_are_the_three_ruled_ids_and_default_off():
    assert config.OPT_IN_MODULES == {
        "adt": "ORB_ADT", "pharmacy-store": "ORB_PHARMACY_STORE", "lab": "ORB_LAB"}
    for m, env in config.OPT_IN_MODULES.items():
        assert config.HOSPITAL_MODULE_ENV[m] == env
        assert config.module_state(m) == "off"
        assert config.enabled_modules()[m] is False
    assert config.ADT_ENABLED is False
    assert config.PHARMACY_STORE_ENABLED is False
    assert config.LAB_ENABLED is False


def test_patching_the_constant_turns_a_module_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    assert config.module_state("adt") == "on"
    assert config.enabled_modules()["adt"] is True
    assert config.module_states()["adt"] == "on"


def test_a_drain_check_makes_a_switched_off_module_draining(monkeypatch):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {})
    config.register_module_drain_check("lab", lambda: True)
    assert config.module_state("lab") == "draining"
    assert config.enabled_modules()["lab"] is True, "draining keeps the tab for lab staff"
    config.register_module_drain_check("lab", lambda: False)
    assert config.module_state("lab") == "off"
    # The switch alone decides 'on'.
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    config.register_module_drain_check("lab", lambda: False)
    assert config.module_state("lab") == "on"
    config.register_module_drain_check("lab", None)
    assert "lab" not in config._MODULE_DRAIN_CHECKS


def test_a_failing_drain_check_reads_as_draining_not_off(monkeypatch):
    """Open clinical work must not vanish because the check broke (LIS-R21)."""
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {})

    def boom():
        raise RuntimeError("cache not ready")

    config.register_module_drain_check("lab", boom)
    assert config.module_state("lab") == "draining"


def test_only_an_opt_in_module_can_register_a_drain_check():
    with pytest.raises(ValueError):
        config.register_module_drain_check("billing", lambda: True)


def test_default_on_modules_still_read_blank_as_on(monkeypatch):
    """The 2026-08-11 convention is untouched for every module that had it."""
    monkeypatch.setenv("ORB_BILLING", "")
    assert config._module_enabled("ORB_BILLING") is True


# ── 3. Feature flags ──────────────────────────────────────────────────────────

FEATURE_IDS = {
    "registration", "opd", "adt_strict_locations", "pharmacy_ward_stock",
    "ndps_register", "lab_analyser_listener", "result_quarantine",
    "result_corrections", "oru_order_link", "lab_critical_interfaced",
    "lab_portal_reports",
}


def test_feature_flag_ids_are_the_contract_and_all_default_off():
    # A superset: the medication-order and multi-site epics add their own
    # flags in their first waves without editing this test.
    assert FEATURE_IDS <= set(config.FEATURE_FLAGS)
    assert set(config._FEATURE_CONSTANTS) == set(config.FEATURE_FLAGS)
    flags = config.feature_flags()
    assert set(flags) == set(config.FEATURE_FLAGS)
    assert not any(flags.values()), flags
    # Module ids and feature ids never collide (switch_enabled reads both).
    assert not FEATURE_IDS & set(config.enabled_modules())


def test_unknown_feature_and_switch_ids_are_off():
    assert config.feature_enabled("no_such_feature") is False
    assert config.switch_enabled("no_such_switch") is False


def test_opd_needs_registration_and_the_front_desk(monkeypatch):
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    assert config.feature_enabled("opd") is False
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    assert config.feature_enabled("opd") is True
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", False)
    assert config.feature_enabled("opd") is False


@pytest.mark.parametrize("flag,const,module_const", [
    ("adt_strict_locations", "ADT_STRICT_LOCATIONS_ENABLED", "ADT_ENABLED"),
    ("pharmacy_ward_stock", "PHARMACY_WARD_STOCK_ENABLED", "PHARMACY_STORE_ENABLED"),
    ("lab_analyser_listener", "LAB_ANALYSER_LISTENER_ENABLED", "LAB_ENABLED"),
])
def test_a_module_feature_is_off_while_its_module_is_off(monkeypatch, flag, const, module_const):
    monkeypatch.setattr(config, const, True)
    assert config.feature_enabled(flag) is False
    monkeypatch.setattr(config, module_const, True)
    assert config.feature_enabled(flag) is True


def test_the_analyser_listener_stays_off_while_the_lab_drains(monkeypatch):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {})
    monkeypatch.setattr(config, "LAB_ANALYSER_LISTENER_ENABLED", True)
    config.register_module_drain_check("lab", lambda: True)
    assert config.module_state("lab") == "draining"
    assert config.feature_enabled("lab_analyser_listener") is False


def test_the_ndps_register_refuses_without_a_failure_mode(monkeypatch):
    """GRN-RS7 is held: the register cannot be on until a mode is chosen."""
    monkeypatch.setattr(config, "PHARMACY_STORE_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_NDPS_REGISTER_ENABLED", True)
    monkeypatch.setattr(config, "PHARMACY_NDPS_ON_FAILURE", None)
    assert config.feature_enabled("ndps_register") is False
    for mode in ("block", "record"):
        monkeypatch.setattr(config, "PHARMACY_NDPS_ON_FAILURE", mode)
        assert config.feature_enabled("ndps_register") is True
    monkeypatch.setattr(config, "PHARMACY_NDPS_ON_FAILURE", "ignore")
    assert config.feature_enabled("ndps_register") is False


def test_the_lab_features_without_a_module_prerequisite_stand_alone(monkeypatch):
    for flag, const in (("result_quarantine", "RESULT_QUARANTINE_ENABLED"),
                        ("lab_critical_interfaced", "LAB_CRITICAL_INTERFACED_ENABLED")):
        monkeypatch.setattr(config, const, True)
        assert config.feature_enabled(flag) is True, flag


def _boot_config(env: dict) -> str:
    """Import config in a clean interpreter with `env` and return its log."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "ORB_DEPLOYMENT_PROFILE": "dev", "ORB_JWT_SECRET": "probe-only-secret-0123456789abcdef"}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import json, config; print('FLAGS:' + json.dumps(config.feature_flags()))"],
        cwd=str(Path(__file__).resolve().parents[1]), env={**base, **env},
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout + proc.stderr


def test_boot_says_why_a_requested_feature_stays_off():
    out = _boot_config({"ORB_OPD": "1", "ORB_PHARMACY_NDPS_REGISTER": "1"})
    assert "Feature opd (ORB_OPD) is set but stays OFF: ORB_REGISTRATION is off" in out
    assert "NDPS register stays OFF" in out and "GRN-RS7" in out
    assert '"opd": false' in out and '"ndps_register": false' in out


def test_boot_reads_the_env_into_effective_flags():
    out = _boot_config({"ORB_REGISTRATION": "yes", "ORB_OPD": "on", "ORB_LAB": "1",
                        "ORB_LAB_ANALYSER_LISTENER": "true"})
    assert '"registration": true' in out and '"opd": true' in out
    assert '"lab_analyser_listener": true' in out
    assert "stays OFF" not in out


# ── 4. Settings and the strict safety defaults ────────────────────────────────

def test_settings_defaults():
    assert config.UHID_SOURCE == "hospital"
    assert config.UHID_PATTERN is None
    assert config.OPD_SCOPE_DAYS == 7
    assert config.OPD_CHARGE_AT == "completion"
    assert config.OPD_QUEUE_POLL_SECONDS == 30
    assert config.ADT_BACKTIME_HOURS == 72
    assert config.ADT_RESERVATION_MINUTES == 120
    assert config.ADT_RESERVATION_ELECTIVE_MINUTES == 1440
    assert config.ADT_ABSCONDED_CLOSE_HOURS == 24
    assert config.ADT_LEAVE_PROMPT_HOURS == 48
    assert config.ADT_LEAVE_RENT_CHARGED is True
    assert config.PHARMACY_NEAR_EXPIRY_DAYS == 90
    assert config.PHARMACY_MIN_SHELF_LIFE_DAYS == 90
    assert config.PHARMACY_LICENCE_WARN_DAYS == 30
    assert config.PHARMACY_MAX_DISPENSE_UNITS == 1
    assert config.LAB_MLLP_PORT == 2576
    assert config.LAB_CRITICAL_SWEEP_S == 60


def test_held_safety_rulings_default_to_the_strictest_option():
    # OPD-S2: probable duplicates are refused until Samuel confirms 'warn'.
    assert config.DUPLICATE_MODE == "block"
    # GRN-RS7: no NDPS failure mode chosen, so the register cannot be on.
    assert config.PHARMACY_NDPS_ON_FAILURE is None
    # LIS-R5: two different people enter and authorise.
    assert config.LAB_REQUIRE_DISTINCT_AUTHORISER is True
    # LIS-R6: no back-dating of a collection time.
    assert config.LAB_COLLECTION_BACKDATE_MINUTES == 0


@pytest.mark.parametrize("raw,expected", [("", 7), ("14", 14), ("-3", 0), ("seven", 7)])
def test_an_integer_setting_never_turns_a_typo_into_zero(monkeypatch, raw, expected):
    monkeypatch.setenv("ORB_OPD_SCOPE_DAYS", raw)
    assert config._int_setting("ORB_OPD_SCOPE_DAYS", 7, minimum=0) == expected


@pytest.mark.parametrize("raw,expected", [("", "block"), ("warn", "warn"), ("WARN", "warn"),
                                          ("allow", "block")])
def test_a_choice_setting_falls_back_to_its_strict_default(monkeypatch, raw, expected):
    monkeypatch.setenv("ORB_DUPLICATE_MODE", raw)
    assert config._choice_setting("ORB_DUPLICATE_MODE", ("warn", "block"), "block") == expected


# ── 5. /api/health ────────────────────────────────────────────────────────────

def test_health_publishes_modules_states_and_features_all_off():
    import main
    with TestClient(main.app) as client:
        body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["modules"] == config.enabled_modules()
    assert body["module_states"] == {"adt": "off", "pharmacy-store": "off", "lab": "off"}
    assert FEATURE_IDS <= set(body["features"])
    assert not any(body["features"].values()), body["features"]
    # Every default-on module is still on, exactly as before this build.
    for m, on in body["modules"].items():
        assert on is (m not in config.OPT_IN_MODULES), m


def test_health_follows_the_switch_board(monkeypatch):
    import main
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    with TestClient(main.app) as client:
        body = client.get("/api/health").json()
    assert body["modules"]["lab"] is True
    assert body["module_states"]["lab"] == "on"
    assert body["features"]["registration"] is True


def test_health_never_opens_a_database_connection(monkeypatch):
    """A drain check that touched the DB would block the event loop here."""
    import database
    import main

    def refuse(*a, **k):
        raise AssertionError("/api/health opened a database connection")

    with TestClient(main.app) as client:
        monkeypatch.setattr(database, "db_session", refuse)
        monkeypatch.setattr(database, "get_db", refuse)
        assert client.get("/api/health").status_code == 200


# ── 6. Retention and erasure pre-registration ─────────────────────────────────

PLANNED_RETENTION = {
    ("opd_visits", "patient_id", "registered_at"),
    ("patient_demographics", "patient_id", "registered_at"),
    ("patient_identity_changes", "patient_id", "changed_at"),
    ("patient_match_keys", "patient_id", "computed_at"),
    ("opd_consultations", "patient_id", "created_at"),
    ("opd_consultation_addenda", "patient_id", "created_at"),
    ("patient_allergy_assessments", "patient_id", "assessed_at"),
    ("lab_specimens", "patient_id", "created_at"),
    ("lab_specimen_events", "patient_id", "occurred_at"),
    ("lab_order_items", "patient_id", "created_at"),
    ("lab_results", "patient_id", "entered_at"),
    ("lab_result_events", "patient_id", "occurred_at"),
    ("lab_critical_notifications", "patient_id", "raised_at"),
    ("lab_critical_events", "patient_id", "occurred_at"),
    ("result_quarantine", None, "created_at"),
}


def test_every_planned_phi_table_is_registered_for_retention():
    from services.consent_service import _RETENTION_SOURCES
    clinical = set(_RETENTION_SOURCES["clinical_record"])
    assert PLANNED_RETENTION <= clinical
    assert ("pharmacy_ward_returns", "patient_id", "created_at") in set(
        _RETENTION_SOURCES["prescription_and_dispensing"])
    every = {t for rows in _RETENTION_SOURCES.values() for t, _, _ in rows}
    assert "ndps_register" not in every, "live register rows are kept by law, never reviewed"


def test_every_registered_table_that_exists_has_the_named_columns():
    """The contract for later waves: create the table with these columns, or
    change the registration in the same increment."""
    from services.consent_service import _RETENTION_SOURCES
    with db_session() as db:
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for rows in _RETENTION_SOURCES.values():
            for table, pcol, tcol in rows:
                if table not in tables:
                    continue
                cols = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")').fetchall()}
                assert tcol in cols, f"{table} has no {tcol}"
                if pcol:
                    assert pcol in cols, f"{table} has no {pcol}"


def test_a_registered_table_that_does_not_exist_yet_is_skipped_quietly(caplog):
    from services import consent_service as cs
    with db_session() as db:
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    absent = {t for t, _, _ in PLANNED_RETENTION} - tables
    assert absent, "precondition: some planned tables do not exist yet"
    review = cs.retention_review()
    listed = {t for c in review["categories"] for t in c["by_table"]}
    assert not (absent & listed)
    assert not any("retention probe failed" in r.getMessage() for r in caplog.records)
    assert review["deleted_anything"] is False


def test_live_ndps_register_rows_are_retained_after_erasure():
    from services import consent_service as cs
    assert "ndps_register" in cs.ERASURE_RETAINED_TABLES
    assert cs.ERASURE_RETAINED_ROW_FILTERS["ndps_register"] == "lane = 'live'"
    # The count honours the filter: build the register shape on a scratch
    # connection (the real table arrives with migration 54).
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE ndps_register (id INTEGER PRIMARY KEY, patient_id TEXT, lane TEXT)")
    db.executemany("INSERT INTO ndps_register (patient_id, lane) VALUES (?, ?)",
                   [("P1", "live"), ("P1", "live"), ("P1", "non_production"), ("P2", "live")])
    kept = {r["table"]: r for r in cs._retained_after_erasure(db, "P1")}
    assert kept["ndps_register"]["rows"] == 2
    assert "NDPS" in kept["ndps_register"]["reason"]
    db.close()


# ── 7. Fail-closed audit categories ───────────────────────────────────────────

def test_the_three_his_audit_categories_are_fail_closed():
    from services.audit_service import _FAIL_CLOSED_CATEGORIES
    assert {"patient_identity", "patient_movement", "clinical_config"} <= _FAIL_CLOSED_CATEGORIES
