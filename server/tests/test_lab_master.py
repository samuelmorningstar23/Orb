"""The laboratory test master (lis-3): starter catalogue, ranges, limits, API.

Pinned here:
  * the starter catalogue is valid, ships no reference range and no critical
    limit (LIS-R1, LIS-R2), lists every laboratory code the order sets use,
    and ships capillary ketones, VBG and ABG as bedside tests (LIS-R18);
  * loading it is idempotent, registers its orderable codes in terminology,
    and never overwrites a code or a test the site already has;
  * a range is never edited in value: approving the same band closes the old
    row and inserts a new one; an overlapping band is refused; a value must be
    exact at the test's reporting precision;
  * which range applies (LIS-R7, strictest option): sex is normalised ('M',
    'Male ' and 'f'), age is in days from the date of birth at collection,
    with no date of birth only an every-age range applies, and patients.age
    is never read;
  * a master write whose audit row fails commits nothing (clinical_config is
    fail-closed), and only lab.master may write;
  * the API: 404 while the module is off, reads open while it drains, writes
    refused while it drains, the catalogue open to order.read or lab.read.
"""
from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import lab_master_service as lm
from services.audit_service import AuditWriteError

SERVER = Path(__file__).resolve().parents[1]
client = TestClient(main.app)

PATHOLOGIST = {"id": "LMPATH01", "role": "pathologist", "name": "Pathologist Test"}
TECHNICIAN = {"id": "LMTECH01", "role": "lab_technician", "name": "Technician Test"}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _starter_loaded():
    """The starter catalogue, loaded as the module's startup would load it."""
    lm.load_starter_catalogue()
    yield


@pytest.fixture
def own_test():
    """A numeric analyte of this test's own, so ranges approved here never
    land on a starter test another test file reads."""
    code = f"ORB-T-{uuid.uuid4().hex[:8].upper()}"
    created = lm.create_test({
        "code_system": "LOCAL", "code": code, "name": f"Test analyte {code}",
        "short_name": "Test analyte", "section_id": "biochemistry",
        "specimen_type_code": "serum", "unit": "mmol/L", "decimal_places": 1,
    }, actor=PATHOLOGIST)
    yield created
    with db_session() as db:
        db.execute("DELETE FROM lab_reference_ranges WHERE test_id = ?", (created["id"],))
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?", (created["id"],))
        db.execute("DELETE FROM lab_panel_components WHERE panel_test_id = ? OR component_test_id = ?",
                   (created["id"], created["id"]))
        db.execute("DELETE FROM lab_tests WHERE id = ?", (created["id"],))


def _hdr(role):
    uid = f"LM{uuid.uuid4().hex[:6].upper()}"
    dept = "General Medicine" if role in ("doctor", "nurse", "ward_clerk") else None
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, dept, hash_password("password")))
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


# ── 1. The starter catalogue file ────────────────────────────────────────────

def _starter():
    return lm.read_starter_catalogue()


def test_the_starter_catalogue_is_valid():
    assert lm.validate_catalogue(_starter()) == []


def test_the_starter_ships_no_range_and_no_limit():
    data = _starter()
    text = json.dumps(data)
    for word in ('"reference_ranges"', '"critical_limits"', '"low"', '"high"',
                 '"low_milli"', '"high_milli"', '"critical_low"', '"critical_high"'):
        assert word not in text
    poisoned = dict(data, reference_ranges=[{"code": "2823-3", "low": 3.5, "high": 5.1}])
    assert any("must not ship reference_ranges" in p for p in lm.validate_catalogue(poisoned))


def test_every_order_set_lab_code_is_in_the_starter():
    sets = json.loads((SERVER / "site_config" / "order_sets.json").read_text(encoding="utf-8"))
    codes = set()

    def walk(o):
        if isinstance(o, dict):
            if o.get("order_type") == "lab" and o.get("code"):
                codes.add((o["code_system"].upper(), o["code"]))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(sets)
    assert codes, "precondition: the order sets order laboratory tests"
    catalogue = {(t["code_system"], t["code"]) for t in _starter()["tests"]}
    assert codes <= catalogue, sorted(codes - catalogue)
    with db_session() as db:
        for system, code in codes:
            assert lm.get_test(db, lm.test_id_for(system, code)) is not None, code


def test_ketones_vbg_and_abg_are_bedside_tests_and_nothing_else_is():
    poct = {t["code"] for t in _starter()["tests"] if t.get("performed_at") == "ward_poct"}
    assert poct == {"ORB-LAB-BLOOD-KETONES", "ORB-LAB-VBG", "ORB-LAB-ABG"}
    with db_session() as db:
        for code in poct:
            assert lm.get_test(db, f"LOCAL:{code}")["performed_at"] == "ward_poct"


def test_short_names_fit_a_label():
    assert all(len(t["short_name"]) <= lm.SHORT_NAME_MAX for t in _starter()["tests"])


# ── 2. Loading ───────────────────────────────────────────────────────────────

def test_loading_again_inserts_nothing_and_audits_nothing():
    with db_session() as db:
        tests = db.execute("SELECT COUNT(*) FROM lab_tests").fetchone()[0]
        terms = db.execute("SELECT COUNT(*) FROM terminology").fetchone()[0]
        audits = db.execute("SELECT COUNT(*) FROM audit_logs WHERE event_category = 'clinical_config'"
                            ).fetchone()[0]
    again = lm.load_starter_catalogue()
    assert again["loaded"] is False and again["terminology_added"] == 0
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM lab_tests").fetchone()[0] == tests
        assert db.execute("SELECT COUNT(*) FROM terminology").fetchone()[0] == terms
        assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE event_category = 'clinical_config'"
                          ).fetchone()[0] == audits


def test_every_starter_code_validates_for_orders_once_loaded():
    from services.terminology_service import validate_code
    for t in _starter()["tests"]:
        assert validate_code(t["code_system"], t["code"]), t["code"]


def test_the_starter_never_puts_back_what_the_pathologist_changed(own_test):
    with db_session() as db:
        db.execute("UPDATE lab_tests SET short_name = 'K renamed' WHERE id = 'LOINC:2823-3'")
    try:
        lm.load_starter_catalogue()
        with db_session() as db:
            assert lm.get_test(db, "LOINC:2823-3")["short_name"] == "K renamed"
    finally:
        with db_session() as db:
            db.execute("UPDATE lab_tests SET short_name = 'Potassium' WHERE id = 'LOINC:2823-3'")


def test_register_codes_adds_only_missing_codes_and_keeps_existing_displays():
    from services.terminology_service import register_codes
    code = f"ORB-REG-{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        before = db.execute("SELECT display FROM terminology WHERE code_system='LOINC' AND code='2823-3'"
                            ).fetchone()[0]
        added = register_codes(db, [("loinc", "2823-3", "Something else"), ("LOCAL", code, "New one")])
        assert added == 1
        assert db.execute("SELECT display FROM terminology WHERE code_system='LOINC' AND code='2823-3'"
                          ).fetchone()[0] == before
        db.execute("DELETE FROM terminology WHERE code_system = 'LOCAL' AND code = ?", (code,))
    with pytest.raises(ValueError):
        with db_session() as db:
            register_codes(db, [("LOCAL", "", "blank code")])


def test_catalogue_search_gives_the_specimen_and_the_panel_components():
    with db_session() as db:
        found = lm.search_catalogue(db, "cbc")
        cbc = next(t for t in found if t["id"] == "LOINC:58410-2")
        assert cbc["kind"] == "panel" and cbc["container"] == "EDTA tube"
        assert [c["test_id"] for c in cbc["components"]][:2] == ["LOINC:718-7", "LOINC:4544-3"]
        assert lm.search_catalogue(db, "2823-3")[0]["id"] == "LOINC:2823-3"
        assert len(lm.search_catalogue(db, "", limit=5)) == 5


# ── 3. Which range applies ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("M", "male"), ("Male ", "male"), ("m", "male"), ("MALE", "male"),
    ("f", "female"), (" Female", "female"), ("F", "female"),
    ("", None), (None, None), ("other", None), ("unknown", None), ("x", None),
])
def test_sex_is_normalised(raw, expected):
    assert lm.normalise_sex(raw) == expected


def test_age_in_days_comes_from_the_date_of_birth_at_collection():
    assert lm.age_days_at("2026-10-01", "2026-10-04T06:00:00Z") == 3
    assert lm.age_days_at("2026-10-01T00:00:00", "2026-10-04T06:00:00Z") == 3
    assert lm.age_days_at(None, "2026-10-04T06:00:00Z") is None
    assert lm.age_days_at("not a date", "2026-10-04T06:00:00Z") is None
    assert lm.age_days_at("2027-01-01", "2026-10-04T06:00:00Z") is None  # born after collection


def _row(sex="any", lo=0, hi=None, text="r"):
    return {"sex": sex, "age_min_days": lo, "age_max_days": hi, "display_text": text}


def test_selection_by_sex_and_age_band():
    rows = [_row("male", 0, 6570, "boy"), _row("male", 6570, None, "man"),
            _row("female", 0, None, "woman")]
    assert lm.select_applicable(rows, sex="male", age_days=20000).row["display_text"] == "man"
    assert lm.select_applicable(rows, sex="male", age_days=100).row["display_text"] == "boy"
    assert lm.select_applicable(rows, sex="female", age_days=100).row["display_text"] == "woman"
    # The band's upper age is exclusive.
    assert lm.select_applicable(rows, sex="male", age_days=6570).row["display_text"] == "man"


def test_no_date_of_birth_uses_only_an_every_age_range():
    banded = [_row("any", 0, 6570), _row("any", 6570, None)]
    choice = lm.select_applicable(banded, sex="male", age_days=None)
    assert choice.row is None and choice.reason == "date_of_birth_not_recorded"
    assert choice.display_text == "Reference range not configured: date of birth not recorded"
    every_age = [_row("any", 0, None, "all ages")]
    assert lm.select_applicable(every_age, sex=None, age_days=None).row["display_text"] == "all ages"


def test_unknown_sex_matches_only_a_range_for_any_sex():
    rows = [_row("male", 0, None), _row("female", 0, None)]
    choice = lm.select_applicable(rows, sex=None, age_days=20000)
    assert choice.row is None and choice.reason == "sex_not_recorded"


def test_nothing_configured_says_so_and_never_reads_as_zero():
    choice = lm.select_applicable([], sex="male", age_days=20000)
    assert choice.row is None and choice.display_text == "Reference range not configured"
    limit = lm.select_applicable([], sex="male", age_days=20000, limits=True)
    assert limit.display_text == "Critical limit not configured"


def test_two_matching_rows_are_refused_as_ambiguous_not_guessed():
    choice = lm.select_applicable([_row("any"), _row("male")], sex="male", age_days=100)
    assert choice.row is None and choice.reason == "ambiguous"


def test_a_static_age_never_stands_in_for_a_missing_date_of_birth(own_test):
    lm.approve_reference_range(own_test["id"], {"sex": "any", "age_min_days": 6570,
                                                "low": "3.5", "high": "5.1",
                                                "method_note": "Adult range, analyser A"},
                               actor=PATHOLOGIST)
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO patients (id, name, mrn, status, age, sex, date_of_birth) "
                   "VALUES ('PLMAGE1', 'Static Age', 'MRN-LMAGE-1', 'discharged', 40, 'M', NULL)")
    try:
        with db_session() as db:
            choice = lm.applicable_range_for_patient(db, own_test["id"], "PLMAGE1")
            assert choice.row is None and choice.reason == "date_of_birth_not_recorded"
            db.execute("UPDATE patients SET date_of_birth = '1986-01-01' WHERE id = 'PLMAGE1'")
            assert lm.applicable_range_for_patient(db, own_test["id"], "PLMAGE1").row is not None
    finally:
        with db_session() as db:
            db.execute("DELETE FROM patients WHERE id = 'PLMAGE1'")


def test_patients_age_is_never_read_by_the_master_service():
    tree = ast.parse((SERVER / "services" / "lab_master_service.py").read_text(encoding="utf-8"))
    sql = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "FROM patients" in n.value]
    assert sql, "precondition: the service reads patients somewhere"
    for text in sql:
        selected = text.split("FROM patients")[0].upper()
        assert "AGE" not in selected.replace("DATE_OF_BIRTH", ""), text


# ── 4. Approving ranges and limits ───────────────────────────────────────────

def test_approving_the_same_band_closes_the_old_row(own_test):
    first = lm.approve_reference_range(own_test["id"], {"low": "3.5", "high": "5.1",
                                                        "method_note": "Initial"}, actor=PATHOLOGIST)
    assert first["display_text"] == "3.5 to 5.1 mmol/L" and first["effective_to"] is None
    second = lm.approve_reference_range(own_test["id"], {"low": "3.6", "high": "5.0",
                                                         "method_note": "New analyser"},
                                        actor=PATHOLOGIST)
    with db_session() as db:
        old = dict(db.execute("SELECT * FROM lab_reference_ranges WHERE id = ?",
                              (first["id"],)).fetchone())
        current = lm.list_reference_ranges(db, test_id=own_test["id"])
        history = lm.list_reference_ranges(db, test_id=own_test["id"], include_retired=True)
    assert old["effective_to"] is not None
    assert (old["low_milli"], old["high_milli"]) == (3500, 5100), "never edited in value"
    assert [r["id"] for r in current] == [second["id"]]
    assert {r["id"] for r in history} == {first["id"], second["id"]}


def test_an_overlapping_band_is_refused(own_test):
    lm.approve_reference_range(own_test["id"], {"sex": "any", "low": "3.5", "high": "5.1",
                                                "method_note": "All"}, actor=PATHOLOGIST)
    with pytest.raises(lm.LabMasterError) as err:
        lm.approve_reference_range(own_test["id"], {"sex": "male", "age_min_days": 6570,
                                                    "low": "3.6", "high": "5.2",
                                                    "method_note": "Men"}, actor=PATHOLOGIST)
    assert err.value.code == "overlap" and err.value.status == 409


def test_a_value_must_be_exact_at_the_reporting_precision(own_test):
    with pytest.raises(lm.LabMasterError) as err:
        lm.approve_reference_range(own_test["id"], {"low": "3.55", "high": "5.1",
                                                    "method_note": "x"}, actor=PATHOLOGIST)
    assert err.value.code == "precision"
    ok = lm.approve_reference_range(own_test["id"], {"low": "3.50", "high": 5.1,
                                                     "method_note": "x"}, actor=PATHOLOGIST)
    assert (ok["low_milli"], ok["high_milli"]) == (3500, 5100)


def test_one_sided_ranges_and_limits_read_in_words(own_test):
    r = lm.approve_reference_range(own_test["id"], {"high": "5.1", "method_note": "x"},
                                   actor=PATHOLOGIST)
    assert r["display_text"] == "5.1 mmol/L or below" and r["low_milli"] is None
    limit = lm.approve_critical_limit(own_test["id"], {"critical_low": "2.5", "critical_high": "6.5",
                                                       "agreed_with": "Dr A, Dr B (medicine)",
                                                       "method_note": "Agreed at the lab meeting"},
                                      actor=PATHOLOGIST)
    assert limit["display_text"] == "Critical below 2.5 or above 6.5 mmol/L"
    assert limit["agreed_with"] == "Dr A, Dr B (medicine)"


def test_a_limit_names_who_it_was_agreed_with_and_an_approval_needs_a_note(own_test):
    with pytest.raises(lm.LabMasterError) as err:
        lm.approve_critical_limit(own_test["id"], {"critical_high": "6.5", "method_note": "x"},
                                  actor=PATHOLOGIST)
    assert err.value.code == "missing_field"
    with pytest.raises(lm.LabMasterError):
        lm.approve_reference_range(own_test["id"], {"low": "1", "high": "2", "method_note": "  "},
                                   actor=PATHOLOGIST)


def test_only_a_numeric_analyte_takes_a_range():
    with pytest.raises(lm.LabMasterError) as err:
        lm.approve_reference_range("LOINC:58410-2", {"low": "1", "high": "2", "method_note": "x"},
                                   actor=PATHOLOGIST)
    assert err.value.code == "not_numeric"


def test_retiring_a_range_leaves_the_test_not_configured(own_test):
    r = lm.approve_reference_range(own_test["id"], {"low": "3.5", "high": "5.1",
                                                    "method_note": "x"}, actor=PATHOLOGIST)
    lm.retire_reference_range(r["id"], "Method withdrawn", actor=PATHOLOGIST)
    with db_session() as db:
        choice = lm.applicable_range(db, own_test["id"], sex="m", date_of_birth="1980-01-01")
    assert choice.row is None and choice.reason == "not_configured"
    with pytest.raises(lm.LabMasterError) as err:
        lm.retire_reference_range(r["id"], "again", actor=PATHOLOGIST)
    assert err.value.status == 409


def test_a_unit_change_waits_until_the_ranges_are_retired(own_test):
    r = lm.approve_reference_range(own_test["id"], {"low": "3.5", "high": "5.1",
                                                    "method_note": "x"}, actor=PATHOLOGIST)
    with pytest.raises(lm.LabMasterError) as err:
        lm.update_test(own_test["id"], {"unit": "mg/dL"}, actor=PATHOLOGIST)
    assert err.value.code == "retire_ranges_first"
    lm.retire_reference_range(r["id"], "Changing unit", actor=PATHOLOGIST)
    changed = lm.update_test(own_test["id"], {"unit": "mg/dL"}, actor=PATHOLOGIST)
    assert changed["unit"] == "mg/dL" and changed["version"] == own_test["version"] + 1


def test_a_master_write_whose_audit_fails_commits_nothing(own_test, monkeypatch):
    def refuse(*a, **k):
        raise AuditWriteError("audit store unavailable")
    monkeypatch.setattr(lm, "log_audit_event", refuse)
    with pytest.raises(AuditWriteError):
        lm.approve_reference_range(own_test["id"], {"low": "3.5", "high": "5.1",
                                                    "method_note": "x"}, actor=PATHOLOGIST)
    with pytest.raises(AuditWriteError):
        lm.update_test(own_test["id"], {"name": "Renamed"}, actor=PATHOLOGIST)
    with db_session() as db:
        assert lm.list_reference_ranges(db, test_id=own_test["id"], include_retired=True) == []
        assert lm.get_test(db, own_test["id"])["name"] == own_test["name"]


def test_every_master_write_is_audited_as_clinical_config(own_test):
    r = lm.approve_reference_range(own_test["id"], {"low": "3.5", "high": "5.1",
                                                    "method_note": "x"}, actor=PATHOLOGIST)
    with db_session() as db:
        row = db.execute(
            "SELECT event_category, actor_user_id, target_id FROM audit_logs "
            "WHERE action_type = 'lab_range_approved' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["event_category"] == "clinical_config"
    assert row["actor_user_id"] == PATHOLOGIST["id"] and row["target_id"] == str(r["id"])


def test_only_lab_master_may_write():
    for actor in (TECHNICIAN, {"id": "A1", "role": "admin"}, {"id": "D1", "role": "doctor"}, {}):
        with pytest.raises(lm.LabMasterError) as err:
            lm.approve_reference_range("LOINC:2823-3", {"low": "3.5", "high": "5.1",
                                                        "method_note": "x"}, actor=actor)
        assert err.value.status == 403


def test_a_panel_takes_analytes_only(own_test):
    with pytest.raises(lm.LabMasterError):
        lm.set_panel_components("LOINC:58410-2", ["LOINC:51990-0"], actor=PATHOLOGIST)
    with pytest.raises(lm.LabMasterError):
        lm.set_panel_components("LOINC:58410-2", ["LOCAL:NO-SUCH-TEST"], actor=PATHOLOGIST)
    with pytest.raises(lm.LabMasterError):
        lm.set_panel_components(own_test["id"], ["LOINC:718-7"], actor=PATHOLOGIST)


def test_a_new_test_is_registered_for_orders_and_a_duplicate_refused(own_test):
    from services.terminology_service import validate_code
    assert validate_code("LOCAL", own_test["code"])
    with pytest.raises(lm.LabMasterError) as err:
        lm.create_test({"code_system": "LOCAL", "code": own_test["code"], "name": "Again",
                        "short_name": "Again", "section_id": "biochemistry",
                        "specimen_type_code": "serum"}, actor=PATHOLOGIST)
    assert err.value.status == 409
    with db_session() as db:
        db.execute("DELETE FROM terminology WHERE code_system = 'LOCAL' AND code = ?",
                   (own_test["code"],))


def test_the_gaps_count_tests_with_no_approved_range_or_limit(own_test):
    with db_session() as db:
        before = lm.configuration_gaps(db)
    lm.approve_reference_range(own_test["id"], {"low": "3.5", "high": "5.1",
                                                "method_note": "x"}, actor=PATHOLOGIST)
    with db_session() as db:
        after = lm.configuration_gaps(db)
    assert after["without_reference_range"] == before["without_reference_range"] - 1
    assert after["without_critical_limit"] == before["without_critical_limit"]
    assert after["numeric_tests"] == before["numeric_tests"]


# ── 5. The API ───────────────────────────────────────────────────────────────

def test_every_master_route_404s_while_the_module_is_off():
    h = _hdr("pathologist")
    for method, path in (("get", "/api/lab/catalogue"), ("get", "/api/lab/master/tests"),
                         ("get", "/api/lab/master/ranges"), ("post", "/api/lab/master/ranges")):
        resp = getattr(client, method)(path, headers=h, **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"


def test_the_catalogue_serves_the_order_screen_and_the_lab(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    for role in ("doctor", "nurse", "pathologist", "phlebotomist"):
        resp = client.get("/api/lab/catalogue?q=potassium", headers=_hdr(role))
        assert resp.status_code == 200, (role, resp.text)
        assert any(t["id"] == "LOINC:2823-3" for t in resp.json()["tests"])
    for role in ("pharmacist", "cashier", "receptionist"):
        assert client.get("/api/lab/catalogue", headers=_hdr(role)).status_code == 403, role


def test_only_a_pathologist_approves_a_range_through_the_api(monkeypatch, own_test):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    body = {"test_id": own_test["id"], "low": "3.5", "high": "5.1", "method_note": "Analyser A"}
    for role in ("admin", "lab_technician", "doctor"):
        assert client.post("/api/lab/master/ranges", json=body, headers=_hdr(role)).status_code == 403
    resp = client.post("/api/lab/master/ranges", json=body, headers=_hdr("pathologist"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["display_text"] == "3.5 to 5.1 mmol/L"
    listed = client.get(f"/api/lab/master/ranges?test_id={own_test['id']}",
                        headers=_hdr("lab_technician"))
    assert listed.status_code == 200 and len(listed.json()["ranges"]) == 1


def test_a_refusal_comes_back_with_its_code(monkeypatch, own_test):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    resp = client.post("/api/lab/master/ranges", headers=_hdr("pathologist"),
                       json={"test_id": own_test["id"], "low": "3.55", "high": "5.1",
                             "method_note": "x"})
    assert resp.status_code == 400 and resp.json()["detail"]["error"] == "precision"
    resp = client.post("/api/lab/master/ranges", headers=_hdr("pathologist"),
                       json={"test_id": own_test["id"], "low": "3.5", "method_note": "x",
                             "surprise": True})
    assert resp.status_code == 422, "unknown fields are refused, never ignored"


def test_while_draining_reads_answer_and_writes_404(monkeypatch, own_test):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    h = _hdr("pathologist")
    assert client.get("/api/lab/master/tests", headers=h).status_code == 200
    resp = client.post("/api/lab/master/ranges", headers=h,
                       json={"test_id": own_test["id"], "low": "3.5", "high": "5.1",
                             "method_note": "x"})
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "module_disabled"
    resp = client.patch(f"/api/lab/master/tests/{own_test['id']}", headers=h, json={"name": "x"})
    assert resp.status_code == 404


def test_the_test_list_carries_the_loud_gap_counts(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    body = client.get("/api/lab/master/tests", headers=_hdr("lab_technician")).json()
    assert body["gaps"]["numeric_tests"] >= 1
    assert body["gaps"]["without_reference_range"] <= body["gaps"]["numeric_tests"]
    assert any(t["id"] == "LOINC:2823-3" for t in body["tests"])


def test_a_pathologist_adds_and_changes_a_test_through_the_api(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    code = f"ORB-API-{uuid.uuid4().hex[:6].upper()}"
    body = {"code_system": "LOCAL", "code": code, "name": "Serum magnesium (site)",
            "short_name": "Magnesium", "section_id": "biochemistry",
            "specimen_type_code": "serum", "unit": "mg/dL", "decimal_places": 1}
    assert client.post("/api/lab/master/tests", json=body,
                       headers=_hdr("lab_technician")).status_code == 403
    created = client.post("/api/lab/master/tests", json=body, headers=_hdr("pathologist"))
    try:
        assert created.status_code == 201, created.text
        tid = created.json()["id"]
        assert tid == f"LOCAL:{code}" and created.json()["version"] == 1
        changed = client.patch(f"/api/lab/master/tests/{tid}", json={"name": "Magnesium"},
                               headers=_hdr("pathologist"))
        assert changed.status_code == 200, changed.text
        assert changed.json()["name"] == "Magnesium" and changed.json()["version"] == 2
        refused = client.patch(f"/api/lab/master/tests/{tid}", json={"code": "OTHER"},
                               headers=_hdr("pathologist"))
        assert refused.status_code == 422, "the code is the identity and never changes"
        fetched = client.get(f"/api/lab/master/tests/{tid}", headers=_hdr("phlebotomist"))
        assert fetched.status_code == 200 and fetched.json()["reference_ranges"] == []
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_tests WHERE code = ?", (code,))
            db.execute("DELETE FROM terminology WHERE code_system = 'LOCAL' AND code = ?", (code,))
