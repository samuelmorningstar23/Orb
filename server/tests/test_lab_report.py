"""Laboratory reports (lis-14).

Pinned here:
  * The range on the report is the one snapshot when the result was entered,
    not the range the pathologist approves later.
  * Released values only (ruling LIS-R4): a test entered or validated but not
    authorised is 'Pending' with no value anywhere in the answer, and the
    report says it is interim.
  * Versions: an amended result prints its current value with the earlier
    released version, who replaced it, when and why; a withdrawn result is
    said to be withdrawn and its value is never repeated.
  * The Simulated honesty label for a simulated source.
  * The signatory's registration number, or 'Registration number not on
    file'; the age at collection from the date of birth, never patients.age.
  * No em dash in anything the report says.
  * Scope: the laboratory reads any report; a ward clinician reads only her
    own department's (an out-of-ward clinician gets 404, the same answer as an
    unknown sample, and the attempt is recorded). Roles with neither lab.read
    nor order.read get 403.
  * Every read and every print writes a phi_access row; a print whose row
    cannot be written shows nothing (503).
  * Reading and printing write nothing to the chart: the lab trend card's
    point count is unchanged.
  * With the laboratory off every report route is 404 module_disabled; while
    it drains they are served.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from routes.lab import reports as report_routes
from services import lab_master_service as lm
from services import lab_module
from services import lab_observations as lobs
from services import lab_report_service as lrep
from services import orders_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import utc_iso_now
from services.hospital_time import hospital_today

client = TestClient(main.app)

DOCTOR_ID = "D001"
WARD = "General Medicine"
OTHER_WARD = "Cardiology"
SUFFIX = uuid.uuid4().hex[:5].upper()
EM_DASH = chr(0x2014)


def _code(name):
    return f"W7I{SUFFIX}-{name}"


TESTS = {
    "K": {"name": "Report potassium", "short_name": "Rep K", "decimal_places": 1,
          "unit": "mmol/L", "result_type": "numeric"},
    "NA": {"name": "Report sodium", "short_name": "Rep Na", "decimal_places": 0,
           "unit": "mmol/L", "result_type": "numeric"},
    "TROP": {"name": "Report troponin", "short_name": "Rep Trop", "decimal_places": 2,
             "unit": "ng/mL", "result_type": "numeric"},
}
PANEL = "PNL"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _user(role, dept=None, registration=None):
    uid = f"REP{uuid.uuid4().hex[:6].upper()}"
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
                    "name": "Report mini panel", "short_name": "Rep panel",
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


def _patient(created, *, dob="1980-05-01", sex="female", dept=WARD, age=None):
    pid = f"PREP{uuid.uuid4().hex[:8].upper()}"
    name = f"Report {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-REP-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed, date_of_birth, sex, age) "
                   "VALUES (?, ?, ?, 'admitted', 'GEN-1', ?, ?, ?, ?)",
                   (pid, name, mrn, f"B{uuid.uuid4().hex[:3]}", dob, sex, age))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"AREP{uuid.uuid4().hex[:8].upper()}", pid, dept))
    created.append(pid)
    return {"id": pid, "name": name, "mrn": mrn}


@pytest.fixture
def patient(_catalogue):
    return _patient(_catalogue)


@pytest.fixture(scope="module")
def tech():
    return _user("lab_technician")


@pytest.fixture(scope="module")
def phleb():
    return _user("phlebotomist")


@pytest.fixture(scope="module")
def signer():
    return _user("pathologist", registration="MCI-12345")


@pytest.fixture(scope="module")
def unregistered_signer():
    return _user("pathologist")


@pytest.fixture(scope="module")
def ward_doctor():
    return _user("doctor", dept=WARD)


@pytest.fixture(scope="module")
def other_doctor():
    return _user("doctor", dept=OTHER_WARD)


@pytest.fixture(scope="module")
def other_nurse():
    return _user("nurse", dept=OTHER_WARD)


def _received(p, phleb_hdr, tech_hdr, keys=("K",), *, panel=False):
    """Orders taken in on ONE tube, labelled, collected and received.
    Returns (specimen_id, {key: item_id})."""
    if panel:
        orders = [orders_service.create_order(
            patient_id=p["id"], order_type="lab", description="Report mini panel",
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


def _enter(item_id, value, hdr, comment=None):
    body = {"order_item_id": item_id, "value": value}
    if comment:
        body["comment"] = comment
    resp = client.post("/api/lab/results", json=body, headers=hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _validated(item_id, value, tech_hdr, comment=None):
    result = _enter(item_id, value, tech_hdr, comment)
    resp = client.post("/api/lab/results/tech-validate",
                       json={"results": [{"result_id": result["id"], "expected": _seen(result)}]},
                       headers=tech_hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()["results"][0]


def _release(specimen_id, results, signer_hdr):
    resp = client.post("/api/lab/results/authorise",
                       json={"specimen_id": specimen_id,
                             "results": [{"result_id": r["id"], "expected": _seen(r)}
                                         for r in results]}, headers=signer_hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _reported(p, tech, phleb, signer, value="4.4", key="K", comment=None):
    specimen_id, items = _received(p, phleb[1], tech[1], (key,))
    result = _validated(items[key], value, tech[1], comment)
    _release(specimen_id, [result], signer[1])
    return specimen_id, result


def _report(specimen_id, hdr):
    resp = client.get(f"/api/lab/reports/{specimen_id}", headers=hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()["report"]


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, target_id)).fetchall()]


def _bad_check_digit(accession: str) -> str:
    return accession[:-1] + str((int(accession[-1]) + 1) % 10)


def _shows_value(text: str, value: str) -> bool:
    """True when `value` appears in `text` as a value of its own, not inside a
    longer token such as a timestamp's seconds (':56.912345' holds '6.9'), an
    accession number or a hex id."""
    return re.search(rf"(?<![\w.:-]){re.escape(value)}(?!\w)", text) is not None


def test_shows_value_ignores_timestamps_and_ids():
    assert _shows_value('{"value_text": "6.9", "unit": "mmol/L"}', "6.9")
    assert _shows_value("was 6.9 mmol/L.", "6.9")
    assert not _shows_value('"2026-10-09T14:06:56.912345+00:00"', "6.9")
    assert not _shows_value('"id": "PREPA131BCDE", "accession": "261009131027"', "131")


# ── 1. What the report says ─────────────────────────────────────────────────

def test_the_range_on_the_report_is_the_snapshot_not_todays(lab_on, patient, tech, phleb,
                                                            signer, master):
    test_id = lm.test_id_for("LOCAL", _code("K"))
    lm.approve_reference_range(test_id, {"low": "3.5", "high": "5.1",
                                         "method_note": "Site range, analyser A"}, actor=master)
    specimen_id, _ = _reported(patient, tech, phleb, signer, value="5.6")
    # The pathologist approves a new range for the same band afterwards.
    lm.approve_reference_range(test_id, {"low": "3.0", "high": "6.0",
                                         "method_note": "New analyser"}, actor=master)
    report = _report(specimen_id, tech[1])
    item = report["items"][0]
    assert item["state"] == "reported"
    assert item["result"]["value_text"] == "5.6"
    assert item["result"]["unit"] == "mmol/L"
    assert item["result"]["reference_range_text"] == "3.5 to 5.1 mmol/L"
    assert item["result"]["flag_text"] == "High"
    assert report["status"] == "final" and report["status_text"] == "Final report"


def test_no_range_configured_prints_no_flag(lab_on, patient, tech, phleb, signer):
    specimen_id, _ = _reported(patient, tech, phleb, signer, value="139", key="NA")
    item = _report(specimen_id, tech[1])["items"][0]
    assert item["result"]["flag_text"] == ""
    assert item["result"]["reference_range_text"].startswith("Reference range not configured")


def test_a_bound_keeps_its_comparator(lab_on, patient, tech, phleb, signer):
    specimen_id, _ = _reported(patient, tech, phleb, signer, value="<0.01", key="TROP")
    result = _report(specimen_id, tech[1])["items"][0]["result"]
    assert result["value_text"] == "<0.01" and result["comparator"] == "<"


def test_pending_analytes_are_pending_with_no_value(lab_on, patient, tech, phleb, signer):
    specimen_id, items = _received(patient, phleb[1], tech[1], panel=True)
    released = _validated(items["K"], "4.2", tech[1])
    _release(specimen_id, [released], signer[1])
    # Sodium is entered and validated on the bench but not authorised.
    _validated(items["NA"], "131", tech[1])
    resp = client.get(f"/api/lab/reports/{specimen_id}", headers=tech[1])
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    by_name = {i["test"]["name"]: i for i in report["items"]}
    assert by_name["Report potassium"]["state"] == "reported"
    sodium = by_name["Report sodium"]
    assert sodium["state"] == "pending" and sodium["state_text"] == "Pending"
    assert sodium["result"] is None and sodium["history"] == []
    assert not _shows_value(resp.text, "131"), "an unreleased value must never reach a report"
    assert report["status"] == "interim"
    assert report["status_text"] == "Interim report: 1 test not yet reported"
    assert by_name["Report potassium"]["panel_name"] == "Report mini panel"


def test_a_sample_with_nothing_released_has_no_report(lab_on, patient, tech, phleb):
    specimen_id, items = _received(patient, phleb[1], tech[1])
    _validated(items["K"], "4.4", tech[1])
    resp = client.get(f"/api/lab/reports/{specimen_id}", headers=tech[1])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "not_reported"
    assert "4.4" not in resp.text


def _amend_in_db(result_id, new_value, reason, actor_id):
    """An amendment as lis-13 stores one (migration 55's columns): the old
    version superseded and pointing at the new, the new released at version
    n+1 with the reason."""
    now = utc_iso_now()
    with db_session() as db:
        old = dict(db.execute("SELECT * FROM lab_results WHERE id = ?", (result_id,)).fetchone())
        db.execute("UPDATE lab_results SET status = 'superseded' WHERE id = ?", (result_id,))
        cur = db.execute(
            """INSERT INTO lab_results
               (order_item_id, order_id, specimen_id, patient_id, test_id, version, value_milli,
                comparator, value_text, unit, decimal_places, comment, reference_range_id,
                reference_range_text, abnormal_flag, entered_by, entered_at, tech_validated_by,
                tech_validated_at, released_by, released_at, status, amend_reason, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, '', ?, ?, ?, ?, ?, ?,
                       'released', ?, ?)""",
            (old["order_item_id"], old["order_id"], old["specimen_id"], old["patient_id"],
             old["test_id"], old["version"] + 1, int(float(new_value) * 1000), new_value,
             old["unit"], old["decimal_places"], old["reference_range_id"],
             old["reference_range_text"], actor_id, now, actor_id, now, actor_id, now, reason,
             old["source"]))
        new_id = cur.lastrowid
        db.execute("UPDATE lab_results SET superseded_by_result_id = ? WHERE id = ?",
                   (new_id, result_id))
        db.execute("INSERT INTO lab_result_events (result_id, patient_id, event_type, actor, "
                   "occurred_at, after_json) VALUES (?, ?, 'superseded', ?, ?, ?)",
                   (result_id, old["patient_id"], actor_id, now,
                    json.dumps({"superseded_by": new_id, "reason": reason})))
    return new_id


def test_an_amended_result_prints_its_history(lab_on, patient, tech, phleb, signer):
    specimen_id, result = _reported(patient, tech, phleb, signer, value="4.4")
    corrector, _ = _user("pathologist", registration="MCI-777")
    new_id = _amend_in_db(result["id"], "5.4", "Wrong dilution factor applied", corrector)
    report = _report(specimen_id, tech[1])
    item = report["items"][0]
    assert item["state"] == "reported" and item["amended"] is True
    assert item["result"]["id"] == new_id and item["result"]["version"] == 2
    assert item["result"]["value_text"] == "5.4"
    assert item["result"]["amend_reason"] == "Wrong dilution factor applied"
    [earlier] = item["history"]
    assert earlier["version"] == 1 and earlier["state"] == "replaced"
    assert earlier["value_text"] == "4.4"
    assert earlier["reported_by"]["name"].startswith("Pathologist")
    assert earlier["ended_by"]["name"] == f"Pathologist {corrector}"
    assert earlier["reason"] == "Wrong dilution factor applied"
    assert report["status"] == "amended" and report["status_text"] == "Amended report"
    printed = client.get(f"/api/lab/reports/{specimen_id}/print", headers=tech[1])
    assert printed.status_code == 200
    assert printed.json()["report"]["items"][0]["history"][0]["value_text"] == "4.4"


def test_a_withdrawn_result_never_repeats_its_value(lab_on, patient, tech, phleb, signer):
    specimen_id, result = _reported(patient, tech, phleb, signer, value="6.9")
    now = utc_iso_now()
    with db_session() as db:
        db.execute("UPDATE lab_results SET status = 'withdrawn', amend_reason = ? WHERE id = ?",
                   ("Entered on the wrong patient", result["id"]))
        db.execute("INSERT INTO lab_result_events (result_id, patient_id, event_type, actor, "
                   "occurred_at, after_json) VALUES (?, ?, 'withdrawn', ?, ?, ?)",
                   (result["id"], patient["id"], signer[0], now,
                    json.dumps({"status": "withdrawn", "reason": "Entered on the wrong patient"})))
    resp = client.get(f"/api/lab/reports/{specimen_id}", headers=tech[1])
    assert resp.status_code == 200, resp.text
    item = resp.json()["report"]["items"][0]
    assert item["state"] == "withdrawn" and item["result"] is None
    [entry] = item["history"]
    assert entry["state"] == "withdrawn" and entry["value_text"] is None
    assert entry["reason"] == "Entered on the wrong patient"
    assert entry["ended_by"]["name"] == f"Pathologist {signer[0]}"
    assert not _shows_value(resp.text, "6.9")
    assert resp.json()["report"]["status"] == "amended"


def test_a_simulated_result_carries_the_honesty_label(lab_on, patient, tech, phleb, signer):
    specimen_id, result = _reported(patient, tech, phleb, signer)
    with db_session() as db:
        db.execute("UPDATE lab_results SET source = 'simulated' WHERE id = ?", (result["id"],))
    report = _report(specimen_id, tech[1])
    assert report["simulated"] is True
    assert report["items"][0]["result"]["simulated"] is True
    assert "simulator" in report["simulated_text"]
    real_id, _ = _reported(_patient([]), tech, phleb, signer)
    assert _report(real_id, tech[1])["simulated"] is False


def test_the_signatory_and_the_registration_number(lab_on, patient, tech, phleb, signer,
                                                   unregistered_signer):
    specimen_id, _ = _reported(patient, tech, phleb, signer)
    report = _report(specimen_id, tech[1])
    assert report["signatories"] == [{"name": f"Pathologist {signer[0]}",
                                      "registration_number": "MCI-12345",
                                      "registration_text": "MCI-12345"}]
    assert report["technologists"][0]["name"] == f"Lab_Technician {tech[0]}"
    other = _patient([])
    other_id, _ = _reported(other, tech, phleb, unregistered_signer)
    person = _report(other_id, tech[1])["signatories"][0]
    assert person["registration_number"] is None
    assert person["registration_text"] == "Registration number not on file"


def test_the_age_is_at_collection_and_never_the_static_column(lab_on, _catalogue, tech,
                                                              phleb, signer):
    with_dob = _patient(_catalogue, dob="1980-05-01", sex="F")
    specimen_id, _ = _reported(with_dob, tech, phleb, signer)
    patient_block = _report(specimen_id, tech[1])["patient"]
    assert patient_block["age_text"].endswith("years") and patient_block["sex_text"] == "Female"
    assert "date_of_birth" not in patient_block
    no_dob = _patient(_catalogue, dob=None, sex=None, age=99)
    specimen_id, _ = _reported(no_dob, tech, phleb, signer)
    patient_block = _report(specimen_id, tech[1])["patient"]
    assert patient_block["age_text"] == "Not recorded"
    assert patient_block["sex_text"] == "Not recorded"


def test_age_words():
    at = "2026-10-09T04:00:00+00:00"
    assert lrep.age_text("1980-05-01", at) == "46 years"
    assert lrep.age_text("2025-09-09", at) == "13 months"
    assert lrep.age_text("2026-09-09", at) == "1 month"
    assert lrep.age_text("2026-10-08", at) == "1 day"
    assert lrep.age_text("2027-01-01", at) is None
    assert lrep.age_text("", at) is None


def test_no_em_dash_in_anything_the_report_says(lab_on, patient, tech, phleb, signer):
    specimen_id, items = _received(patient, phleb[1], tech[1], panel=True)
    _release(specimen_id, [_validated(items["K"], "4.2", tech[1], "Haemolysed slightly")],
             signer[1])
    for path in (f"/api/lab/reports/{specimen_id}", f"/api/lab/reports/{specimen_id}/print"):
        text = client.get(path, headers=tech[1]).text
        assert EM_DASH not in text and "\\u2014" not in text
    for line in (*lrep.REPORT_NOTES, lrep.SIMULATED_TEXT, *lrep.STATE_WORDS.values(),
                 *lrep.STATUS_WORDS.values(), lrep.REGISTRATION_MISSING_TEXT):
        assert EM_DASH not in line


# ── 2. Who may read it ──────────────────────────────────────────────────────

def test_the_ward_reads_its_own_patients_report(lab_on, patient, tech, phleb, signer,
                                                ward_doctor):
    specimen_id, _ = _reported(patient, tech, phleb, signer)
    assert client.get(f"/api/lab/reports/{specimen_id}",
                      headers=ward_doctor[1]).status_code == 200
    listed = client.get(f"/api/lab/reports/patient/{patient['id']}", headers=ward_doctor[1])
    assert listed.status_code == 200, listed.text
    assert [r["accession"] for r in listed.json()["reports"]] == [specimen_id]


def test_an_out_of_ward_clinician_gets_404(lab_on, patient, tech, phleb, signer,
                                           other_doctor, other_nurse):
    specimen_id, _ = _reported(patient, tech, phleb, signer)
    for hdr in (other_doctor[1], other_nurse[1]):
        for path in (f"/api/lab/reports/{specimen_id}",
                     f"/api/lab/reports/{specimen_id}/print",
                     f"/api/lab/reports/patient/{patient['id']}"):
            resp = client.get(path, headers=hdr)
            assert resp.status_code == 404, (path, resp.text)
            assert patient["name"] not in resp.text
    # The same answer as a sample that does not exist.
    unknown = client.get(f"/api/lab/reports/{_unused_accession(specimen_id)}",
                         headers=other_doctor[1])
    assert unknown.status_code == 404
    denied = _audits("patient_scope_denied", specimen_id)
    assert denied and denied[-1]["actor_user_id"] in (other_doctor[0], other_nurse[0])
    assert not _audits("lab_report_viewed", specimen_id)


def _unused_accession(accession: str) -> str:
    """A well-formed accession with a valid check digit that names no sample."""
    from services import lab_accession
    body_day = accession[:6]
    for seq in range(99999, 90000, -1):
        body = f"{body_day}{seq:05d}"
        text = body + str(lab_accession.damm_check_digit(body))
        with db_session() as db:
            if db.execute("SELECT 1 FROM lab_specimens WHERE id = ?", (text,)).fetchone() is None:
                return text
    raise AssertionError("no free accession")


def test_roles_without_lab_or_order_read_are_refused(lab_on, patient, tech, phleb, signer):
    specimen_id, _ = _reported(patient, tech, phleb, signer)
    for role in ("pharmacist", "receptionist", "cashier"):
        _, hdr = _user(role)
        assert client.get(f"/api/lab/reports/{specimen_id}", headers=hdr).status_code == 403
        assert client.get(f"/api/lab/reports/patient/{patient['id']}",
                          headers=hdr).status_code == 403
    # The phlebotomist holds lab.read (ruling LIS-R12: hospital-wide).
    assert client.get(f"/api/lab/reports/{specimen_id}", headers=phleb[1]).status_code == 200
    # The laboratory list is for lab.read only: a ward doctor does not get it.
    _, doctor_hdr = _user("doctor", dept=WARD)
    assert client.get("/api/lab/reports", headers=doctor_hdr).status_code == 403


def test_a_misread_accession_is_never_looked_up(lab_on, patient, tech, phleb, signer):
    specimen_id, _ = _reported(patient, tech, phleb, signer)
    resp = client.get(f"/api/lab/reports/{_bad_check_digit(specimen_id)}", headers=tech[1])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "bad_accession"


# ── 3. The audit trail ──────────────────────────────────────────────────────

def test_viewing_and_printing_each_write_a_phi_access_row(lab_on, patient, tech, phleb,
                                                          signer):
    specimen_id, _ = _reported(patient, tech, phleb, signer)
    _report(specimen_id, tech[1])
    printed = client.get(f"/api/lab/reports/{specimen_id}/print", headers=tech[1])
    assert printed.status_code == 200, printed.text
    body = printed.json()["report"]
    assert body["printed_at"] and body["printed_by"]["name"] == f"Lab_Technician {tech[0]}"
    viewed = _audits("lab_report_viewed", specimen_id)
    prints = _audits("lab_report_printed", specimen_id)
    assert len(viewed) == 1 and len(prints) == 1
    for row in viewed + prints:
        assert row["event_category"] == "phi_access"
        assert row["patient_id"] == patient["id"]
        assert row["actor_user_id"] == tech[0]
    assert json.loads(prints[0]["metadata_json"])["printed_at"] == body["printed_at"]


def test_a_print_that_cannot_be_audited_shows_nothing(lab_on, patient, tech, phleb, signer,
                                                      monkeypatch):
    specimen_id, _ = _reported(patient, tech, phleb, signer, value="4.7")

    def refuse(**_kw):
        raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr(report_routes, "log_phi_read", refuse)
    for path in (f"/api/lab/reports/{specimen_id}/print", f"/api/lab/reports/{specimen_id}"):
        resp = client.get(path, headers=tech[1])
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"]["error"] == "audit_unavailable"
        assert "4.7" not in resp.text and patient["name"] not in resp.text


def test_the_laboratory_list_by_day_and_by_uhid(lab_on, patient, tech, phleb, signer):
    specimen_id, _ = _reported(patient, tech, phleb, signer)
    today = hospital_today().isoformat()
    listed = client.get("/api/lab/reports", params={"day": today}, headers=tech[1])
    assert listed.status_code == 200, listed.text
    line = next(r for r in listed.json()["reports"] if r["accession"] == specimen_id)
    assert line["patient"]["mrn"] == patient["mrn"] and line["status"] == "final"
    assert line["tests"] == ["Rep K"]
    by_uhid = client.get("/api/lab/reports", params={"uhid": patient["mrn"].lower()},
                         headers=tech[1])
    assert [r["accession"] for r in by_uhid.json()["reports"]] == [specimen_id]
    with db_session() as db:
        row = db.execute("SELECT event_category, metadata_json FROM audit_logs "
                         "WHERE action_type = 'lab_reports_listed' AND actor_user_id = ? "
                         "ORDER BY id DESC LIMIT 1", (tech[0],)).fetchone()
    assert row["event_category"] == "phi_access"
    assert patient["id"] in json.loads(row["metadata_json"])["patient_ids"]
    bad = client.get("/api/lab/reports", params={"day": "9 Oct"}, headers=tech[1])
    assert bad.status_code == 422


def test_reading_and_printing_change_nothing_on_the_chart(lab_on, patient, tech, phleb,
                                                          signer):
    specimen_id, _ = _reported(patient, tech, phleb, signer)

    def points():
        with db_session() as db:
            rows = lobs.chart_lab_rows(db, patient["id"], per_analyte=50, max_rows=500)
            count = db.execute("SELECT COUNT(*) FROM observations WHERE patient_id = ?",
                               (patient["id"],)).fetchone()[0]
        return len(rows), count

    before = points()
    assert before[0] == 1
    _report(specimen_id, tech[1])
    client.get(f"/api/lab/reports/{specimen_id}/print", headers=tech[1])
    client.get(f"/api/lab/reports/patient/{patient['id']}", headers=tech[1])
    assert points() == before


# ── 4. The switch ───────────────────────────────────────────────────────────

def test_every_report_route_is_404_with_the_module_off(tech, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    for path in ("/api/lab/reports", "/api/lab/reports/261009000127",
                 "/api/lab/reports/261009000127/print", "/api/lab/reports/patient/P1"):
        resp = client.get(path, headers=tech[1])
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"


def test_reports_are_served_while_the_module_drains(lab_on, patient, tech, phleb, signer,
                                                    monkeypatch):
    specimen_id, _ = _reported(patient, tech, phleb, signer)
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    assert client.get(f"/api/lab/reports/{specimen_id}", headers=tech[1]).status_code == 200


def test_a_list_is_capped_newest_collection_first(lab_on, patient, tech, phleb, signer,
                                                  monkeypatch):
    older, _ = _reported(patient, tech, phleb, signer)
    newer, _ = _reported(patient, tech, phleb, signer)
    with db_session() as db:
        db.execute("UPDATE lab_specimens SET collected_at = ? WHERE id = ?",
                   ("2026-01-01T00:00:00.000000+00:00", older))
    monkeypatch.setattr(lrep, "REPORT_LIST_CAP", 1)
    listed = client.get(f"/api/lab/reports/patient/{patient['id']}", headers=tech[1])
    assert [r["accession"] for r in listed.json()["reports"]] == [newer]
    monkeypatch.setattr(lrep, "REPORT_LIST_CAP", 200)
    listed = client.get(f"/api/lab/reports/patient/{patient['id']}", headers=tech[1])
    assert [r["accession"] for r in listed.json()["reports"]] == [newer, older]


# ── 5. Against the laboratory's own amend and withdraw (lis-13, W7-H) ───────
# The tests above store amendments the way migration 55 defines them. These two
# run the laboratory's real writers when they are present, so the report and
# the writer cannot drift apart unseen.

def _route_present(path: str) -> bool:
    return any(getattr(r, "path", "") == path for r in main.app.routes)


needs_lis13 = pytest.mark.skipif(
    not (_route_present("/api/lab/results/{result_id}/amend")
         and _route_present("/api/lab/results/{result_id}/withdraw")),
    reason="lis-13 amend and withdraw routes are not in this build")


@needs_lis13
def test_a_real_amendment_prints_its_history(lab_on, patient, tech, phleb, signer,
                                             monkeypatch):
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="4.4")
    resp = client.post(f"/api/lab/results/{result['id']}/amend",
                       json={"value": "5.4", "reason": "Transcription error on the bench sheet",
                             "expected": _seen(result)}, headers=signer[1])
    assert resp.status_code == 200, resp.text
    report = _report(specimen_id, tech[1])
    item = report["items"][0]
    assert item["state"] == "reported" and item["result"]["version"] == 2
    assert item["result"]["value_text"] == "5.4"
    [earlier] = item["history"]
    assert earlier["state"] == "replaced" and earlier["value_text"] == "4.4"
    assert earlier["reason"] == "Transcription error on the bench sheet"
    assert earlier["ended_by"]["name"] == f"Pathologist {signer[0]}"
    assert report["status"] == "amended"


@needs_lis13
def test_a_real_withdrawal_says_withdrawn_not_cancelled(lab_on, patient, tech, phleb, signer):
    specimen_id, result = _reported(patient, tech, phleb, signer, value="6.9")
    resp = client.post(f"/api/lab/results/{result['id']}/withdraw",
                       json={"reason": "Result belonged to another patient",
                             "expected": _seen(result)}, headers=signer[1])
    assert resp.status_code == 200, resp.text
    resp = client.get(f"/api/lab/reports/{specimen_id}", headers=tech[1])
    assert resp.status_code == 200, resp.text
    item = resp.json()["report"]["items"][0]
    assert item["state"] == "withdrawn" and item["result"] is None
    assert item["history"][0]["reason"] == "Result belonged to another patient"
    assert not _shows_value(resp.text, "6.9")


def _amend(result, value, reason, signer):
    """One-step amendment through the laboratory's own route (the caller turns
    the two-person setting off). Returns the new released version."""
    resp = client.post(f"/api/lab/results/{result['id']}/amend",
                       json={"value": value, "reason": reason, "expected": _seen(result)},
                       headers=signer[1])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["amended"] is True, body
    return body["result"]


def _withdraw(result, reason, signer, *, wrong_patient=False):
    resp = client.post(f"/api/lab/results/{result['id']}/withdraw",
                       json={"reason": reason, "wrong_patient": wrong_patient,
                             "expected": _seen(result)}, headers=signer[1])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _assert_withheld(version):
    assert version["value_withheld"] is True
    assert version["value_text"] is None and version["unit"] is None
    assert version["flag_text"] == "" and version["reference_range_text"] is None


@needs_lis13
def test_an_amended_then_withdrawn_result_prints_no_earlier_value(lab_on, patient, tech, phleb,
                                                                  signer, monkeypatch):
    """Released 6.9, amended to 7.3 ('Recalculated'), then withdrawn as
    another patient's. The whole chain may be another patient's sample: no
    version prints its value, and version 1 never gives the withdrawal as the
    reason it was amended."""
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="6.9")
    amended = _amend(result, "7.3", "Recalculated", signer)
    assert amended["version"] == 2
    _withdraw(amended, "Belonged to another patient", signer, wrong_patient=True)
    for path in (f"/api/lab/reports/{specimen_id}", f"/api/lab/reports/{specimen_id}/print"):
        resp = client.get(path, headers=tech[1])
        assert resp.status_code == 200, (path, resp.text)
        report = resp.json()["report"]
        [item] = report["items"]
        assert item["state"] == "withdrawn" and item["result"] is None
        first, second = item["history"]
        assert (first["version"], first["state"]) == (1, "replaced")
        _assert_withheld(first)
        assert first["reason"] is None, "version 1 must not give the withdrawal as its reason"
        assert first["reported_by"]["name"] == f"Pathologist {signer[0]}"
        assert first["reported_at"]
        assert first["ended_by"]["name"] == f"Pathologist {signer[0]}"
        assert first["ended_at"]
        assert (second["version"], second["state"]) == (2, "withdrawn")
        _assert_withheld(second)
        assert second["reason"] == "Belonged to another patient"
        assert second["ended_by"]["name"] == f"Pathologist {signer[0]}"
        for value in ("6.9", "7.3"):
            assert not _shows_value(resp.text, value), (path, value)
        assert "Recalculated" not in resp.text
        assert report["status"] == "amended"
    listed = client.get(f"/api/lab/reports/patient/{patient['id']}", headers=tech[1])
    assert listed.status_code == 200, listed.text
    assert [r["accession"] for r in listed.json()["reports"]] == [specimen_id]
    for value in ("6.9", "7.3"):
        assert not _shows_value(listed.text, value)


@needs_lis13
def test_a_twice_amended_then_withdrawn_result_prints_no_earlier_value(lab_on, patient, tech,
                                                                      phleb, signer,
                                                                      monkeypatch):
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="5.8")
    second = _amend(result, "6.2", "Dilution factor corrected", signer)
    third = _amend(second, "6.6", "Repeated on a fresh aliquot", signer)
    _withdraw(third, "Sample from another patient", signer, wrong_patient=True)
    resp = client.get(f"/api/lab/reports/{specimen_id}", headers=tech[1])
    assert resp.status_code == 200, resp.text
    [item] = resp.json()["report"]["items"]
    assert item["state"] == "withdrawn" and item["result"] is None
    history = item["history"]
    assert [v["version"] for v in history] == [1, 2, 3]
    assert [v["state"] for v in history] == ["replaced", "replaced", "withdrawn"]
    for version in history:
        _assert_withheld(version)
        assert version["ended_by"]["name"] == f"Pathologist {signer[0]}"
    assert [v["reason"] for v in history] == [None, None, "Sample from another patient"]
    for value in ("5.8", "6.2", "6.6"):
        assert not _shows_value(resp.text, value), value
    for reason in ("Dilution factor corrected", "Repeated on a fresh aliquot"):
        assert reason not in resp.text


@needs_lis13
def test_each_amendment_prints_its_own_reason(lab_on, patient, tech, phleb, signer,
                                              monkeypatch):
    """With nothing withdrawn every earlier version keeps its value, and each
    says why IT was replaced, not why its predecessor was."""
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)
    specimen_id, result = _reported(patient, tech, phleb, signer, value="5.8")
    second = _amend(result, "6.2", "Dilution factor corrected", signer)
    _amend(second, "6.6", "Repeated on a fresh aliquot", signer)
    [item] = _report(specimen_id, tech[1])["items"]
    assert item["state"] == "reported" and item["result"]["value_text"] == "6.6"
    assert item["result"]["amend_reason"] == "Repeated on a fresh aliquot"
    first, middle = item["history"]
    assert (first["value_text"], first["reason"]) == ("5.8", "Dilution factor corrected")
    assert (middle["value_text"], middle["reason"]) == ("6.2", "Repeated on a fresh aliquot")
    assert first["value_withheld"] is False and middle["value_withheld"] is False


# ── 6. Where the reason for a replaced version comes from ────────────────────

def _event_row(eid, result_id, event_type, reason, actor="U1", at="2026-10-09T06:00:00+00:00"):
    return {"id": eid, "result_id": result_id, "event_type": event_type, "actor": actor,
            "occurred_at": at, "after_json": json.dumps({"reason": reason})}


_V1 = {"id": 1, "version": 1, "status": "superseded", "value_text": "6.9", "unit": "mmol/L",
       "abnormal_flag": "", "reference_range_text": "3.5 to 5.1 mmol/L",
       "released_at": "2026-10-09T05:00:00+00:00", "released_by": "U1",
       "amend_reason": None, "superseded_by_result_id": 2}


def test_the_reason_is_read_from_the_superseded_event_first():
    # Version 2 was withdrawn: withdraw_result overwrote its amend_reason with
    # the withdrawal reason, so that column must never explain version 1.
    successor = {"id": 2, "version": 2, "status": "withdrawn",
                 "released_at": "2026-10-09T06:00:00+00:00", "released_by": "U2",
                 "amend_reason": "Belonged to another patient"}
    events = [_event_row(10, 1, "superseded", "Recalculated"),
              _event_row(11, 2, "amended", "Recalculated from the amended event"),
              _event_row(12, 2, "withdrawn", "Belonged to another patient")]
    entry = lrep._version_entry(_V1, successor=successor, events=events, users={},
                                result_type="numeric")
    assert entry["reason"] == "Recalculated"
    no_own = lrep._version_entry(_V1, successor=successor, events=events[1:], users={},
                                 result_type="numeric")
    assert no_own["reason"] == "Recalculated from the amended event"
    neither = lrep._version_entry(_V1, successor=successor, events=events[2:], users={},
                                  result_type="numeric")
    assert neither["reason"] is None
    live = {**successor, "status": "released", "amend_reason": "Recalculated by the column"}
    column = lrep._version_entry(_V1, successor=live, events=[], users={},
                                 result_type="numeric")
    assert column["reason"] == "Recalculated by the column"


def test_a_withheld_version_prints_only_who_and_when():
    successor = {"id": 2, "version": 2, "status": "withdrawn",
                 "released_at": "2026-10-09T06:00:00+00:00", "released_by": "U2",
                 "amend_reason": "Belonged to another patient"}
    users = {"U1": {"name": "Dr One"}, "U2": {"name": "Dr Two"}}
    entry = lrep._version_entry(_V1, successor=successor,
                                events=[_event_row(10, 1, "superseded", "Recalculated from 6.9")],
                                users=users, result_type="numeric", withhold=True)
    assert entry == {
        "version": 1, "state": "replaced", "value_withheld": True, "value_text": None,
        "unit": None, "flag_text": "", "reference_range_text": None,
        "reported_at": "2026-10-09T05:00:00+00:00", "reported_by": {"name": "Dr One"},
        "ended_at": "2026-10-09T06:00:00+00:00", "ended_by": {"name": "Dr Two"},
        "reason": None,
    }
