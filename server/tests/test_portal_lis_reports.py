"""Laboratory reports in the patient portal (lis-29, ruling LIS-R11 (c)).

Pinned here:
  * Off by default. With ORB_LAB_PORTAL_REPORTS off, or on without the
    laboratory module on (draining included), every new route answers 404
    feature_disabled and My Records is exactly as before.
  * ORB_LAB_PORTAL_DELAY_HOURS: unset by default (a doctor's release only);
    0, a negative, a non-number or more than a year read as unset.
  * Only released results reach the portal, and only after a doctor released
    the report as read, or after the site's delay. A critical value never
    reaches it by the delay.
  * Flags as printed words, never colour: the patient's answer carries no raw
    flag code, no critical marker and no colour field.
  * Self-scope: another patient's sample is the same 404 as an unknown one,
    and the attempt is recorded.
  * The doctor's release: discharge.publish inside the department only;
    the results named must be the report's released results now (409
    report_changed, nothing written); a repeat writes nothing.
  * Amended and withdrawn results: no superseded value shown as current, a
    withdrawn value never shown. The delay shows a version only if it was
    still live when its delay ran out: a value amended or withdrawn inside
    the delay never reaches the patient, is never listed as an earlier
    value, and never lists the report on its own.
  * The Simulated honesty label is kept.
  * Every disclosure writes a fail-closed audit row (503 and nothing shown
    when it cannot).
  * No em dash in anything a patient reads.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from routes import bridge_routes
from services import lab_master_service as lm
from services import lab_module
from services import lab_portal_service as portal
from services import orders_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import format_utc, parse_to_utc, utc_iso_now

client = TestClient(main.app)

DOCTOR_ID = "D001"
WARD = "General Medicine"
OTHER_WARD = "Cardiology"
SUFFIX = uuid.uuid4().hex[:5].upper()
EM_DASH = chr(0x2014)


def _code(name):
    return f"W8J{SUFFIX}-{name}"


TESTS = {
    "K": {"name": "Portal potassium", "short_name": "Por K", "decimal_places": 1,
          "unit": "mmol/L", "result_type": "numeric"},
    "NA": {"name": "Portal sodium", "short_name": "Por Na", "decimal_places": 0,
           "unit": "mmol/L", "result_type": "numeric"},
}
PANEL = "PNL"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _user(role, dept=None, registration=None):
    uid = f"W8J{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, registration_number) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, f"{role.title()} {uid}", role, dept, hash_password("password"), registration))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def master():
    uid, _ = _user("pathologist")
    return {"id": uid, "role": "pathologist"}


@pytest.fixture(scope="module", autouse=True)
def _catalogue(master):
    terminology_service.seed_terminology()
    lm.load_starter_catalogue()
    for key, spec in TESTS.items():
        lm.create_test({"code_system": "LOCAL", "code": _code(key), "kind": "analyte",
                        "section_id": "biochemistry", "specimen_type_code": "serum", **spec},
                       actor=master)
    lm.create_test({"code_system": "LOCAL", "code": _code(PANEL), "kind": "panel",
                    "name": "Portal mini panel", "short_name": "Por panel",
                    "section_id": "biochemistry", "specimen_type_code": "serum"}, actor=master)
    lm.set_panel_components(lm.test_id_for("LOCAL", _code(PANEL)),
                            [lm.test_id_for("LOCAL", _code("K")),
                             lm.test_id_for("LOCAL", _code("NA"))], actor=master)
    created: list[str] = []
    yield created
    keys = list(TESTS) + [PANEL]
    test_ids = [lm.test_id_for("LOCAL", _code(k)) for k in keys]
    tmarks = ", ".join("?" for _ in test_ids)
    with db_session() as db:
        if created:
            marks = ", ".join("?" for _ in created)
            ids = tuple(created)
            db.execute(f"DELETE FROM lab_exceptions WHERE order_id IN "
                       f"(SELECT id FROM orders WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM lab_critical_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_critical_notifications WHERE patient_id IN ({marks})",
                       ids)
            db.execute(f"DELETE FROM observations WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_result_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_results WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_order_items WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimen_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimens WHERE patient_id IN ({marks})", ids)
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
            db.execute(f"UPDATE admissions SET status = 'discharged' "
                       f"WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM lab_reference_ranges WHERE test_id IN ({tmarks})",
                   tuple(test_ids))
        db.execute(f"DELETE FROM lab_panel_components WHERE panel_test_id IN ({tmarks})",
                   tuple(test_ids))
        db.execute(f"DELETE FROM lab_tests WHERE id IN ({tmarks})", tuple(test_ids))
        db.execute(f"DELETE FROM terminology WHERE code_system = 'LOCAL' AND code IN ({tmarks})",
                   tuple(_code(k) for k in keys))
    lab_module.refresh_drain_state()


@pytest.fixture
def lab_on(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", True)
    with db_session() as db:
        event_id = db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, ?)",
            (utc_iso_now(), "test cutover")).lastrowid
    yield
    with db_session() as db:
        db.execute("DELETE FROM lab_module_events WHERE id = ?", (event_id,))


@pytest.fixture
def portal_on(lab_on, monkeypatch):
    """The feature on with no delay set: a doctor's release only."""
    monkeypatch.setattr(config, "LAB_PORTAL_REPORTS_ENABLED", True)
    monkeypatch.setattr(config, "LAB_PORTAL_DELAY_HOURS", None)
    assert config.feature_enabled("lab_portal_reports") is True


def _patient(created, *, dept=WARD):
    """An admitted patient with a Bridge login."""
    pid = f"PW8J{uuid.uuid4().hex[:8].upper()}"
    uid = f"UW8J{uuid.uuid4().hex[:8].upper()}"
    name = f"Portal {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-W8J-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, 'patient', ?)",
                   (uid, name, hash_password("password")))
        db.execute("INSERT INTO patients (id, user_id, name, mrn, status, ward, bed, "
                   "date_of_birth, sex) VALUES (?, ?, ?, ?, 'admitted', 'GEN-1', ?, "
                   "'1980-05-01', 'female')",
                   (pid, uid, name, mrn, f"B{uuid.uuid4().hex[:3]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"AW8J{uuid.uuid4().hex[:8].upper()}", pid, dept))
    created.append(pid)
    return {"id": pid, "name": name, "mrn": mrn, "user_id": uid,
            "hdr": {"Authorization": f"Bearer {create_token(uid, 'patient')}"}}


@pytest.fixture
def patient(_catalogue):
    return _patient(_catalogue)


@pytest.fixture
def other_patient(_catalogue):
    return _patient(_catalogue)


@pytest.fixture(scope="module")
def tech():
    return _user("lab_technician")


@pytest.fixture(scope="module")
def phleb():
    return _user("phlebotomist")


@pytest.fixture(scope="module")
def signer():
    return _user("pathologist", registration="MCI-W8J-1")


@pytest.fixture(scope="module")
def ward_doctor():
    return _user("doctor", dept=WARD)


@pytest.fixture(scope="module")
def other_doctor():
    return _user("doctor", dept=OTHER_WARD)


@pytest.fixture(scope="module")
def ward_nurse():
    return _user("nurse", dept=WARD)


@pytest.fixture(scope="module")
def pharmacist():
    return _user("pharmacist")


def _received(p, phleb_hdr, tech_hdr, keys=("K",), *, panel=False):
    if panel:
        orders = [orders_service.create_order(
            patient_id=p["id"], order_type="lab", description="Portal mini panel",
            code=_code(PANEL), code_system="LOCAL", priority="routine", ordered_by=DOCTOR_ID,
            status="active")]
    else:
        orders = [orders_service.create_order(
            patient_id=p["id"], order_type="lab", description=key, code=_code(key),
            code_system="LOCAL", priority="routine", ordered_by=DOCTOR_ID, status="active")
            for key in keys]
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            f"SELECT id, specimen_id, test_id FROM lab_order_items "
            f"WHERE order_id IN ({', '.join('?' for _ in orders)}) ORDER BY id",
            tuple(o["id"] for o in orders)).fetchall()]
    specimens = {r["specimen_id"] for r in rows}
    assert len(specimens) == 1, "the tests should share one tube"
    specimen_id = specimens.pop()
    identity = {"uhid": p["mrn"], "name": p["name"]}
    assert client.post("/api/lab/collection/labels",
                       json={"specimen_ids": [specimen_id], "identity": identity},
                       headers=phleb_hdr).status_code == 200
    assert client.post(f"/api/lab/specimens/{specimen_id}/collect",
                       json={"identity": identity}, headers=phleb_hdr).status_code == 200
    assert client.post(f"/api/lab/specimens/{specimen_id}/receive", json={},
                       headers=tech_hdr).status_code == 200
    items = {}
    for r in rows:
        for key in TESTS:
            if r["test_id"] == lm.test_id_for("LOCAL", _code(key)):
                items[key] = r["id"]
    return specimen_id, items


def _seen(result):
    return {"value_text": result["value_text"], "comment": result.get("comment"),
            "abnormal_flag": result.get("abnormal_flag")}


def _validated(item_id, value, tech_hdr, comment=None):
    body = {"order_item_id": item_id, "value": value}
    if comment:
        body["comment"] = comment
    resp = client.post("/api/lab/results", json=body, headers=tech_hdr)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    resp = client.post("/api/lab/results/tech-validate",
                       json={"results": [{"result_id": result["id"], "expected": _seen(result)}]},
                       headers=tech_hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()["results"][0]


def _authorise(specimen_id, results, signer_hdr):
    resp = client.post("/api/lab/results/authorise",
                       json={"specimen_id": specimen_id,
                             "results": [{"result_id": r["id"], "expected": _seen(r)}
                                         for r in results]}, headers=signer_hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _reported(p, tech, phleb, signer, value="4.4", key="K", comment=None):
    specimen_id, items = _received(p, phleb[1], tech[1], (key,))
    result = _validated(items[key], value, tech[1], comment)
    _authorise(specimen_id, [result], signer[1])
    return specimen_id, result


def _release(p, specimen_id, ids, hdr):
    return client.post(f"/api/bridge/patients/{p['id']}/lab-reports/{specimen_id}/release",
                       json={"seen_result_ids": ids}, headers=hdr)


def _released_ok(p, specimen_id, ids, doctor):
    resp = _release(p, specimen_id, ids, doctor[1])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _list(p):
    resp = client.get("/api/bridge/lab-reports", headers=p["hdr"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _detail(p, specimen_id):
    return client.get(f"/api/bridge/lab-reports/{specimen_id}", headers=p["hdr"])


def _events(specimen_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_specimen_events WHERE specimen_id = ? AND event_type = ? "
            "ORDER BY seq", (specimen_id, portal.PORTAL_RELEASE_EVENT)).fetchall()]


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, target_id)).fetchall()]


def _shows_value(text: str, value: str) -> bool:
    return re.search(rf"(?<![\w.:-]){re.escape(value)}(?!\w)", text) is not None


def _released_at(result_id):
    with db_session() as db:
        return db.execute("SELECT released_at FROM lab_results WHERE id = ?",
                          (result_id,)).fetchone()[0]


def _pin_now(monkeypatch, at):
    monkeypatch.setattr(portal, "_now", lambda: at)


def json_text(body) -> str:
    return json.dumps(body)


# ── 1. Off by default ────────────────────────────────────────────────────────

def test_the_feature_and_the_delay_are_off_by_default():
    assert config.feature_enabled("lab_portal_reports") is False
    assert config.feature_flags()["lab_portal_reports"] is False
    assert config.LAB_PORTAL_DELAY_HOURS is None
    assert config.FEATURE_PREREQUISITES["lab_portal_reports"] == (("module", "lab"),)


def test_every_route_is_404_while_the_feature_is_off(lab_on, patient, tech, phleb, signer,
                                                     ward_doctor):
    specimen_id, result = _reported(patient, tech, phleb, signer)
    calls = [
        client.get("/api/bridge/lab-reports", headers=patient["hdr"]),
        client.get(f"/api/bridge/lab-reports/{specimen_id}", headers=patient["hdr"]),
        client.get(f"/api/bridge/patients/{patient['id']}/lab-reports", headers=ward_doctor[1]),
        _release(patient, specimen_id, [result["id"]], ward_doctor[1]),
    ]
    for resp in calls:
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "feature_disabled"
        assert resp.json()["detail"]["feature"] == "lab_portal_reports"
    assert _events(specimen_id) == []
    # My Records is unchanged: no new key, no laboratory report in it.
    reports = client.get("/api/bridge/reports", headers=patient["hdr"])
    assert reports.status_code == 200, reports.text
    assert set(reports.json()) == {"patient_id", "medications", "drug_chart",
                                   "clinical_documents", "labs", "discharge_summary",
                                   "imaging_reports"}
    assert not _shows_value(reports.text, "4.4")


def test_the_feature_needs_the_laboratory_on_not_draining(monkeypatch):
    monkeypatch.setattr(config, "LAB_PORTAL_REPORTS_ENABLED", True)
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {})
    assert config.feature_enabled("lab_portal_reports") is False
    config.register_module_drain_check("lab", lambda: True)
    assert config.module_state("lab") == "draining"
    assert config.feature_enabled("lab_portal_reports") is False
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    assert config.feature_enabled("lab_portal_reports") is True


def test_a_requested_switch_without_the_laboratory_stays_off_at_boot():
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "ORB_DEPLOYMENT_PROFILE": "dev", "ORB_JWT_SECRET": "probe-only-secret-0123456789abcdef",
           "ORB_LAB_PORTAL_REPORTS": "1"}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import json, config; print('FLAGS:' + json.dumps(config.feature_flags()))"],
        cwd=str(Path(__file__).resolve().parents[1]), env=env,
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    assert '"lab_portal_reports": false' in out
    assert "Feature lab_portal_reports (ORB_LAB_PORTAL_REPORTS) is set but stays OFF" in out


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("  ", None), ("24", 24), (" 48 ", 48), ("1", 1),
    ("8760", 8760), ("0", None), ("-3", None), ("1.5", None), ("soon", None),
    ("8761", None),
])
def test_the_delay_setting_reads_only_whole_hours_from_one_to_a_year(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("ORB_LAB_PORTAL_DELAY_HOURS", raising=False)
    else:
        monkeypatch.setenv("ORB_LAB_PORTAL_DELAY_HOURS", raw)
    assert config._portal_delay_hours() == expected


def test_the_service_reads_a_bad_delay_as_none(monkeypatch):
    for bad in (0, -1, True, "24", 9000, 1.5):
        monkeypatch.setattr(config, "LAB_PORTAL_DELAY_HOURS", bad)
        assert portal.delay_hours() is None, bad
    monkeypatch.setattr(config, "LAB_PORTAL_DELAY_HOURS", 12)
    assert portal.delay_hours() == 12


# ── 2. A doctor's release (no delay set) ─────────────────────────────────────

def test_with_no_delay_nothing_reaches_the_portal_without_a_doctor(portal_on, patient, tech,
                                                                   phleb, signer, monkeypatch):
    specimen_id, result = _reported(patient, tech, phleb, signer, value="5.6")
    # A year later, still nothing: there is no delay to pass.
    _pin_now(monkeypatch, parse_to_utc(_released_at(result["id"])) + timedelta(days=365))
    body = _list(patient)
    assert body["reports"] == []
    assert body["note"] == ("Laboratory reports appear here after your doctor has released "
                            "them to you.")
    resp = _detail(patient, specimen_id)
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "not_found"
    assert not _shows_value(resp.text, "5.6")


def test_a_doctors_release_shows_the_report_as_printed_words(portal_on, patient, tech, phleb,
                                                             signer, ward_doctor, master):
    test_id = lm.test_id_for("LOCAL", _code("K"))
    lm.approve_reference_range(test_id, {"low": "3.5", "high": "5.1",
                                         "method_note": "Site range W8J"}, actor=master)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="5.6",
                                    comment="Repeat sample advised")
    state = client.get(f"/api/bridge/patients/{patient['id']}/lab-reports",
                       headers=ward_doctor[1])
    assert state.status_code == 200, state.text
    [entry] = state.json()["reports"]
    assert entry["portal"]["state"] == "needs_release"
    assert entry["portal"]["needs_doctor_release"] is True
    assert entry["portal"]["live_result_ids"] == [result["id"]]

    answer = _released_ok(patient, specimen_id, [result["id"]], ward_doctor)
    assert answer["released_result_ids"] == [result["id"]] and answer["already"] is False
    assert answer["portal"]["state"] == "shown"
    [event] = _events(specimen_id)
    assert event["actor_user_id"] == ward_doctor[0]
    [audit] = _audits("lab_report_portal_released", specimen_id)
    assert audit["event_category"] == "clinical_access"
    assert audit["patient_id"] == patient["id"]

    body = _list(patient)
    [line] = body["reports"]
    assert line["accession"] == specimen_id
    assert line["tests"] == ["Portal potassium"]
    assert line["shown_count"] == 1 and line["complete"] is True
    assert not _shows_value(json_text(body), "5.6"), "the list carries no values"

    resp = _detail(patient, specimen_id)
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    [test] = report["tests"]
    assert test["state"] == "shown"
    assert test["value_text"] == "5.6" and test["unit"] == "mmol/L"
    assert test["flag_text"] == "High"
    assert test["reference_range_text"] == "3.5 to 5.1 mmol/L"
    assert test["comment"] == "Repeat sample advised"
    assert report["signatories"] == [{"name": f"Pathologist {signer[0]}",
                                      "registration_text": "MCI-W8J-1"}]
    assert report["status_text"] == "Final report"
    # No colour and no code: words only, nothing a screen could paint.
    for word in ("abnormal_flag", "is_critical", "colour", "color", "severity", "band",
                 "critical_limit"):
        assert word not in resp.text, word
    # Nothing about the patient's identity, place or the staff who handled the tube.
    for key in ("patient", "mrn", "ward", "bed", "collected_by", "received_by", "date_of_birth"):
        assert f'"{key}"' not in resp.text, key
    assert EM_DASH not in resp.text and EM_DASH not in json_text(body)
    assert _audits("portal_lab_reports_listed", patient["id"])
    [viewed] = _audits("portal_lab_report_viewed", specimen_id)
    assert viewed["event_category"] == "phi_access"
    assert viewed["actor_user_id"] == patient["user_id"]


def test_a_repeat_release_writes_nothing(portal_on, patient, tech, phleb, signer, ward_doctor):
    specimen_id, result = _reported(patient, tech, phleb, signer)
    _released_ok(patient, specimen_id, [result["id"]], ward_doctor)
    again = _released_ok(patient, specimen_id, [result["id"]], ward_doctor)
    assert again["already"] is True and again["released_result_ids"] == []
    assert len(_events(specimen_id)) == 1
    assert len(_audits("lab_report_portal_released", specimen_id)) == 1


def test_a_release_naming_other_results_is_refused_and_writes_nothing(
        portal_on, patient, tech, phleb, signer, ward_doctor):
    specimen_id, result = _reported(patient, tech, phleb, signer, value="4.9")
    for ids in ([result["id"] + 100000], [result["id"], result["id"] + 1]):
        resp = _release(patient, specimen_id, ids, ward_doctor[1])
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "report_changed"
        assert not _shows_value(resp.text, "4.9")
    for body in ({"seen_result_ids": []}, {"seen_result_ids": [True]},
                 {"seen_result_ids": ["1"]}, {"seen_result_ids": [result["id"]], "x": 1}, {}):
        resp = client.post(
            f"/api/bridge/patients/{patient['id']}/lab-reports/{specimen_id}/release",
            json=body, headers=ward_doctor[1])
        assert resp.status_code == 422, (body, resp.text)
    assert _events(specimen_id) == []
    assert _list(patient)["reports"] == []


def test_a_release_before_anything_is_authorised_is_refused(portal_on, patient, tech, phleb,
                                                            ward_doctor):
    specimen_id, items = _received(patient, phleb[1], tech[1])
    validated = _validated(items["K"], "6.1", tech[1])
    resp = _release(patient, specimen_id, [validated["id"]], ward_doctor[1])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "not_reported"
    assert _events(specimen_id) == []
    assert not _shows_value(resp.text, "6.1")


def test_a_release_after_a_new_result_needs_the_new_screen(portal_on, patient, tech, phleb,
                                                           signer, ward_doctor):
    """The doctor read the report with potassium only; sodium was released
    since. The old screen's ids are refused; the new screen's are taken."""
    specimen_id, items = _received(patient, phleb[1], tech[1], panel=True)
    k = _validated(items["K"], "4.2", tech[1])
    _authorise(specimen_id, [k], signer[1])
    na = _validated(items["NA"], "139", tech[1])
    _authorise(specimen_id, [na], signer[1])
    resp = _release(patient, specimen_id, [k["id"]], ward_doctor[1])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "report_changed"
    _released_ok(patient, specimen_id, sorted([k["id"], na["id"]]), ward_doctor)
    report = _detail(patient, specimen_id).json()["report"]
    assert {t["name"]: t["value_text"] for t in report["tests"]} == {
        "Portal potassium": "4.2", "Portal sodium": "139"}


def test_only_a_doctor_in_the_patients_department_releases(portal_on, patient, tech, phleb,
                                                           signer, other_doctor, ward_nurse,
                                                           pharmacist):
    specimen_id, result = _reported(patient, tech, phleb, signer)
    assert _release(patient, specimen_id, [result["id"]], ward_nurse[1]).status_code == 403
    assert _release(patient, specimen_id, [result["id"]], pharmacist[1]).status_code == 403
    assert _release(patient, specimen_id, [result["id"]], tech[1]).status_code == 403
    resp = _release(patient, specimen_id, [result["id"]], other_doctor[1])
    assert resp.status_code == 404, resp.text
    assert _events(specimen_id) == []
    # The ward's read of the portal state is department-scoped too.
    state = client.get(f"/api/bridge/patients/{patient['id']}/lab-reports",
                       headers=other_doctor[1])
    assert state.status_code == 404
    assert client.get(f"/api/bridge/patients/{patient['id']}/lab-reports",
                      headers=pharmacist[1]).status_code == 403


def test_a_release_names_the_sample_of_this_patient_only(portal_on, patient, other_patient,
                                                         tech, phleb, signer, ward_doctor):
    specimen_id, result = _reported(other_patient, tech, phleb, signer)
    resp = _release(patient, specimen_id, [result["id"]], ward_doctor[1])
    assert resp.status_code == 404, resp.text
    assert _events(specimen_id) == []


# ── 3. The site's delay ──────────────────────────────────────────────────────

def test_the_delay_shows_a_report_only_once_it_has_passed(portal_on, patient, tech, phleb,
                                                          signer, monkeypatch, ward_doctor):
    monkeypatch.setattr(config, "LAB_PORTAL_DELAY_HOURS", 24)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="4.7")
    released = parse_to_utc(_released_at(result["id"]))
    _pin_now(monkeypatch, released + timedelta(hours=23, minutes=59))
    body = _list(patient)
    assert body["reports"] == []
    assert "24 hours after the laboratory reports them" in body["note"]
    assert _detail(patient, specimen_id).status_code == 404
    state = client.get(f"/api/bridge/patients/{patient['id']}/lab-reports",
                       headers=ward_doctor[1]).json()["reports"][0]["portal"]
    assert state["state"] == "waiting" and state["needs_doctor_release"] is False
    assert parse_to_utc(state["shown_by_delay_from"]) == released + timedelta(hours=24)

    _pin_now(monkeypatch, released + timedelta(hours=24))
    [line] = _list(patient)["reports"]
    assert line["accession"] == specimen_id
    report = _detail(patient, specimen_id).json()["report"]
    assert report["tests"][0]["value_text"] == "4.7"
    assert _events(specimen_id) == [], "the delay writes nothing"


def test_a_critical_value_never_reaches_the_portal_by_the_delay(portal_on, patient, tech, phleb,
                                                                signer, ward_doctor,
                                                                monkeypatch):
    monkeypatch.setattr(config, "LAB_PORTAL_DELAY_HOURS", 1)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="7.2")
    with db_session() as db:
        db.execute("UPDATE lab_results SET is_critical = 1, abnormal_flag = 'HH' WHERE id = ?",
                   (result["id"],))
    released = parse_to_utc(_released_at(result["id"]))
    _pin_now(monkeypatch, released + timedelta(days=30))
    assert _list(patient)["reports"] == []
    assert _detail(patient, specimen_id).status_code == 404
    state = client.get(f"/api/bridge/patients/{patient['id']}/lab-reports",
                       headers=ward_doctor[1]).json()["reports"][0]["portal"]
    assert state["state"] == "needs_release" and state["needs_doctor_release"] is True
    assert state["shown_by_delay_from"] is None
    _released_ok(patient, specimen_id, [result["id"]], ward_doctor)
    [audit] = _audits("lab_report_portal_released", specimen_id)
    assert f'"critical_result_ids": [{result["id"]}]' in audit["metadata_json"]
    test = _detail(patient, specimen_id).json()["report"]["tests"][0]
    assert test["value_text"] == "7.2"
    assert test["flag_text"] == "Critically high"


# ── 4. Partial reports, amendments, withdrawals ──────────────────────────────

def test_a_test_not_yet_released_shows_no_value(portal_on, patient, tech, phleb, signer,
                                                ward_doctor):
    specimen_id, items = _received(patient, phleb[1], tech[1], panel=True)
    k = _validated(items["K"], "4.3", tech[1])
    _authorise(specimen_id, [k], signer[1])
    _validated(items["NA"], "131", tech[1])      # validated, not authorised
    _released_ok(patient, specimen_id, [k["id"]], ward_doctor)
    resp = _detail(patient, specimen_id)
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    tests = {t["name"]: t for t in report["tests"]}
    assert tests["Portal potassium"]["value_text"] == "4.3"
    sodium = tests["Portal sodium"]
    assert sodium["state"] == "waiting" and sodium["state_text"] == "Not available here yet"
    assert sodium["value_text"] is None
    assert not _shows_value(resp.text, "131")
    assert report["complete"] is False
    assert report["status_text"] == "Some results on this report are not available here yet."


def test_a_result_released_after_the_doctors_release_waits_for_another(
        portal_on, patient, tech, phleb, signer, ward_doctor):
    specimen_id, items = _received(patient, phleb[1], tech[1], panel=True)
    k = _validated(items["K"], "4.3", tech[1])
    _authorise(specimen_id, [k], signer[1])
    _released_ok(patient, specimen_id, [k["id"]], ward_doctor)
    na = _validated(items["NA"], "128", tech[1])
    _authorise(specimen_id, [na], signer[1])
    resp = _detail(patient, specimen_id)
    tests = {t["name"]: t for t in resp.json()["report"]["tests"]}
    assert tests["Portal sodium"]["state"] == "waiting"
    assert not _shows_value(resp.text, "128")


def test_an_amended_result_shows_no_value_until_the_new_one_is_released(
        portal_on, patient, tech, phleb, signer, ward_doctor, monkeypatch):
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="6.9")
    _released_ok(patient, specimen_id, [result["id"]], ward_doctor)
    resp = client.post(f"/api/lab/results/{result['id']}/amend",
                       json={"value": "4.1", "reason": "Haemolysed, re-run on a fresh sample",
                             "expected": _seen(result)}, headers=signer[1])
    assert resp.status_code == 200, resp.text
    amended = resp.json()["result"]
    detail = _detail(patient, specimen_id)
    assert detail.status_code == 200, "a report once shown stays listed"
    [test] = detail.json()["report"]["tests"]
    assert test["state"] == "corrected_waiting"
    assert test["value_text"] is None
    assert not _shows_value(detail.text, "6.9") and not _shows_value(detail.text, "4.1")
    assert "Haemolysed" not in detail.text
    _released_ok(patient, specimen_id, [amended["id"]], ward_doctor)
    detail = _detail(patient, specimen_id)
    [test] = detail.json()["report"]["tests"]
    assert test["state"] == "shown" and test["value_text"] == "4.1"
    assert test["corrected"] is True and test["corrected_text"] == "Corrected by the laboratory"
    assert [e["value_text"] for e in test["earlier"]] == ["6.9"]
    assert test["earlier"][0]["replaced_at"]
    assert "Haemolysed" not in detail.text, "an amendment reason never reaches the patient"


def test_a_withdrawn_result_never_shows_a_value(portal_on, patient, tech, phleb, signer,
                                               ward_doctor):
    specimen_id, result = _reported(patient, tech, phleb, signer, value="6.9")
    _released_ok(patient, specimen_id, [result["id"]], ward_doctor)
    resp = client.post(f"/api/lab/results/{result['id']}/withdraw",
                       json={"reason": "Belonged to another patient", "wrong_patient": True,
                             "expected": _seen(result)}, headers=signer[1])
    assert resp.status_code == 200, resp.text
    detail = _detail(patient, specimen_id)
    assert detail.status_code == 200, detail.text
    [test] = detail.json()["report"]["tests"]
    assert test["state"] == "withdrawn" and test["value_text"] is None
    assert test["state_text"].startswith("Withdrawn by the laboratory")
    assert test["earlier"] == []
    assert not _shows_value(detail.text, "6.9")
    assert "another patient" not in detail.text


# ── 4b. The delay and a version that stopped being live ──────────────────────
# The delay shows a version only if it was still the live one when its delay
# ran out. The laboratory's own clock writes the amendment and withdrawal
# times, so each test moves the first release back in the database to set how
# long the version was live.

def _set_released_at(result_id, at):
    with db_session() as db:
        db.execute("UPDATE lab_results SET released_at = ? WHERE id = ?",
                   (format_utc(at), result_id))


def _amend(result, signer, value="4.1"):
    resp = client.post(f"/api/lab/results/{result['id']}/amend",
                       json={"value": value, "reason": "Mistyped, corrected from the analyser",
                             "expected": _seen(result)}, headers=signer[1])
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _withdraw(result, signer):
    resp = client.post(f"/api/lab/results/{result['id']}/withdraw",
                       json={"reason": "Belonged to another patient", "wrong_patient": True,
                             "expected": _seen(result)}, headers=signer[1])
    assert resp.status_code == 200, resp.text


def test_a_value_amended_inside_the_delay_never_reaches_the_patient(
        portal_on, patient, tech, phleb, signer, ward_doctor, monkeypatch):
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)
    monkeypatch.setattr(config, "LAB_PORTAL_DELAY_HOURS", 24)
    specimen_id, first = _reported(patient, tech, phleb, signer, value="6.9")
    amended = _amend(first, signer)
    replaced = parse_to_utc(_released_at(amended["id"]))
    start = replaced - timedelta(hours=2)          # 6.9 was live for two hours
    _set_released_at(first["id"], start)

    # The first value's delay has run out, but it was replaced 22 hours before
    # that: nothing is listed and the report is not there.
    _pin_now(monkeypatch, start + timedelta(hours=24, minutes=1))
    assert _list(patient)["reports"] == []
    assert _detail(patient, specimen_id).status_code == 404
    state = client.get(f"/api/bridge/patients/{patient['id']}/lab-reports",
                       headers=ward_doctor[1]).json()["reports"][0]["portal"]
    assert state["state"] == "waiting" and state["shown_result_ids"] == []
    assert parse_to_utc(state["shown_by_delay_from"]) == replaced + timedelta(hours=24)

    # The corrected value's own delay runs out: it shows, and the value the
    # patient was never shown is not listed as an earlier one.
    _pin_now(monkeypatch, start + timedelta(hours=26, minutes=1))
    [line] = _list(patient)["reports"]
    assert line["accession"] == specimen_id and line["shown_count"] == 1
    detail = _detail(patient, specimen_id)
    assert detail.status_code == 200, detail.text
    [test] = detail.json()["report"]["tests"]
    assert test["state"] == "shown" and test["value_text"] == "4.1"
    assert test["earlier"] == []
    assert not _shows_value(detail.text, "6.9")
    assert "Mistyped" not in detail.text


def test_a_value_amended_after_its_delay_is_listed_as_earlier(
        portal_on, patient, tech, phleb, signer, monkeypatch):
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)
    monkeypatch.setattr(config, "LAB_PORTAL_DELAY_HOURS", 24)
    specimen_id, first = _reported(patient, tech, phleb, signer, value="6.9")
    amended = _amend(first, signer)
    replaced = parse_to_utc(_released_at(amended["id"]))
    _set_released_at(first["id"], replaced - timedelta(hours=30))   # shown 6 hours

    _pin_now(monkeypatch, replaced + timedelta(hours=1))
    detail = _detail(patient, specimen_id)
    assert detail.status_code == 200, "a report the patient was shown stays listed"
    [test] = detail.json()["report"]["tests"]
    assert test["state"] == "corrected_waiting" and test["value_text"] is None
    assert not _shows_value(detail.text, "6.9") and not _shows_value(detail.text, "4.1")

    _pin_now(monkeypatch, replaced + timedelta(hours=24, minutes=1))
    [test] = _detail(patient, specimen_id).json()["report"]["tests"]
    assert test["state"] == "shown" and test["value_text"] == "4.1"
    assert [e["value_text"] for e in test["earlier"]] == ["6.9"]


def test_a_value_withdrawn_inside_the_delay_never_lists_the_report(
        portal_on, patient, tech, phleb, signer, monkeypatch):
    monkeypatch.setattr(config, "LAB_PORTAL_DELAY_HOURS", 24)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="6.9")
    _withdraw(result, signer)
    withdrawn = parse_to_utc(utc_iso_now())
    start = withdrawn - timedelta(hours=1)         # live for about an hour
    _set_released_at(result["id"], start)
    for later in (timedelta(hours=25), timedelta(days=30)):
        _pin_now(monkeypatch, start + later)
        assert _list(patient)["reports"] == []
        detail = _detail(patient, specimen_id)
        assert detail.status_code == 404
        assert not _shows_value(detail.text, "6.9")
        assert "another patient" not in detail.text


def test_a_value_withdrawn_after_its_delay_shows_the_withdrawal(
        portal_on, patient, tech, phleb, signer, monkeypatch):
    monkeypatch.setattr(config, "LAB_PORTAL_DELAY_HOURS", 24)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="6.9")
    _withdraw(result, signer)
    withdrawn = parse_to_utc(utc_iso_now())
    _set_released_at(result["id"], withdrawn - timedelta(hours=30))
    _pin_now(monkeypatch, withdrawn + timedelta(hours=1))
    [line] = _list(patient)["reports"]
    assert line["accession"] == specimen_id and line["shown_count"] == 0
    detail = _detail(patient, specimen_id)
    assert detail.status_code == 200, detail.text
    [test] = detail.json()["report"]["tests"]
    assert test["state"] == "withdrawn" and test["value_text"] is None
    assert test["earlier"] == []
    assert not _shows_value(detail.text, "6.9")
    assert "another patient" not in detail.text


@pytest.mark.parametrize("status", ["superseded", "withdrawn"])
def test_the_delay_needs_a_readable_end_for_a_version_no_longer_live(status):
    released = parse_to_utc("2026-10-01T08:00:00Z")
    version = {"id": 1, "status": status, "released_at": format_utc(released),
               "is_critical": 0, "abnormal_flag": ""}
    assert portal.delay_due_at({**version, "live_until": None}, 24) is None
    assert portal.delay_due_at({**version, "live_until": "not a time"}, 24) is None
    ended = released + timedelta(hours=24)
    assert portal.delay_due_at({**version, "live_until": ended}, 24) is None, \
        "a delay running out at the very moment it stopped being live shows nothing"
    later = released + timedelta(hours=30)
    assert portal.delay_due_at({**version, "live_until": later}, 24) == \
        released + timedelta(hours=24)
    live = {**version, "status": "released"}
    assert portal.delay_due_at(live, 24) == released + timedelta(hours=24)


# ── 5. Self-scope, labels, audit ─────────────────────────────────────────────

def test_another_patients_report_is_the_same_404_as_an_unknown_one(
        portal_on, patient, other_patient, tech, phleb, signer, ward_doctor):
    specimen_id, result = _reported(other_patient, tech, phleb, signer, value="5.9")
    _released_ok(other_patient, specimen_id, [result["id"]], ward_doctor)
    assert _detail(other_patient, specimen_id).status_code == 200
    theirs = _detail(patient, specimen_id)
    unknown = _detail(patient, "999999999999")
    assert theirs.status_code == 404 and unknown.status_code == 404
    assert theirs.json() == unknown.json()
    assert not _shows_value(theirs.text, "5.9")
    assert _list(patient)["reports"] == []
    [denied] = _audits("portal_lab_report_denied", specimen_id)
    assert denied["outcome"] == "denied"
    assert denied["patient_id"] == other_patient["id"]
    assert denied["actor_user_id"] == patient["user_id"]


def test_only_a_patient_login_reads_the_patient_routes(portal_on, patient, tech, phleb, signer,
                                                       ward_doctor):
    specimen_id, _result = _reported(patient, tech, phleb, signer)
    assert client.get("/api/bridge/lab-reports", headers=ward_doctor[1]).status_code == 403
    assert client.get(f"/api/bridge/lab-reports/{specimen_id}",
                      headers=ward_doctor[1]).status_code == 403
    assert client.get("/api/bridge/lab-reports").status_code == 401


def test_a_simulated_result_keeps_its_label(portal_on, patient, tech, phleb, signer,
                                            ward_doctor):
    specimen_id, result = _reported(patient, tech, phleb, signer)
    with db_session() as db:
        db.execute("UPDATE lab_results SET source = 'simulated' WHERE id = ?", (result["id"],))
    _released_ok(patient, specimen_id, [result["id"]], ward_doctor)
    [line] = _list(patient)["reports"]
    assert line["simulated"] is True
    report = _detail(patient, specimen_id).json()["report"]
    assert report["simulated"] is True
    assert report["simulated_text"] == "Simulated result, not from your care"
    assert report["tests"][0]["simulated"] is True


def test_nothing_is_shown_when_the_audit_row_cannot_be_written(portal_on, patient, tech, phleb,
                                                               signer, ward_doctor,
                                                               monkeypatch):
    specimen_id, result = _reported(patient, tech, phleb, signer, value="5.2")
    _released_ok(patient, specimen_id, [result["id"]], ward_doctor)

    def refuse(**kwargs):
        if kwargs.get("event_category") == "phi_access":
            raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr(bridge_routes, "log_audit_event", refuse)
    for path in ("/api/bridge/lab-reports", f"/api/bridge/lab-reports/{specimen_id}"):
        resp = client.get(path, headers=patient["hdr"])
        assert resp.status_code == 503, (path, resp.text)
        assert resp.json()["detail"]["error"] == "audit_unavailable"
        assert specimen_id not in resp.text and not _shows_value(resp.text, "5.2")


def test_a_release_whose_audit_row_fails_writes_nothing(portal_on, patient, tech, phleb, signer,
                                                        ward_doctor, monkeypatch):
    specimen_id, result = _reported(patient, tech, phleb, signer)

    def refuse(**kwargs):
        raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr(portal, "log_audit_event", refuse)
    resp = _release(patient, specimen_id, [result["id"]], ward_doctor[1])
    assert resp.status_code == 503, resp.text
    assert _events(specimen_id) == []


def test_the_patient_words_carry_no_em_dash():
    for text in (portal.SIMULATED_TEXT, portal.NOT_AVAILABLE_TEXT, portal.CORRECTED_WAITING_TEXT,
                 portal.CORRECTED_TEXT, portal.WITHDRAWN_TEXT, portal.CANCELLED_TEXT,
                 portal.SOME_HELD_TEXT, portal.portal_note(None), portal.portal_note(1),
                 portal.portal_note(48), *portal.PATIENT_NOTES,
                 *portal.STAFF_STATE_WORDS.values()):
        assert EM_DASH not in text, text
    assert portal.portal_note(1).startswith("Laboratory reports appear here 1 hour after")
