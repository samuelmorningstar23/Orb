"""Technical validation, authorisation and release into the chart (lis-11).

Pinned here:
  * Who: a technician (lab.result) validates and never authorises (403); a
    signatory (lab.authorise) authorises one sample at a time. While two
    different people are required (ruling LIS-R5, the shipped default) a
    signatory who entered, changed or validated a result cannot authorise it;
    with the site setting off one signatory may do every step, and the
    release records that one person did.
  * What release writes, in one transaction: one observations row per
    analyte with observed_at = the collection instant in canonical UTC,
    value_text at the reporting precision ('4.50'), value_numeric NULL for a
    bound, source 'lis_manual', the order, the accession and the
    lab_results id; the result and its test to released; the audit row. An
    audit row that cannot be written releases nothing.
  * The completion rule (LIS-R16 (a)): partial panels release analyte by
    analyte, and the order is resulted only once every test is reported or
    cancelled; a billing candidate appears only then.
  * Release never depends on the order's state: an order completed by hand
    before the guard still receives the value, and the laboratory gets an
    'order_state_conflict' exception.
  * The chart shows released values only.
  * Critical flags (LIS-R1): HH and LL only from an approved critical limit;
    a value on the limit is not critical; a bound wholly beyond it is.
  * A signatory can send a result back to the bench with a reason.
  * The sample moves to in process at entry and completed once every test
    is reported or cancelled.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import billing_service, lab_module
from services import lab_master_service as lm
from services import lab_observations as lobs
from services import lab_order_service as los
from services import lab_result_service as lrs
from services import orders_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import to_utc_iso, utc_iso_now

client = TestClient(main.app)

DOCTOR_ID = "D001"
WARD = "General Medicine"
SUFFIX = uuid.uuid4().hex[:5].upper()


def _code(name):
    return f"W6F{SUFFIX}-{name}"


TESTS = {
    "ALB": {"name": "Release albumin", "short_name": "Rel Alb", "decimal_places": 2,
            "unit": "g/dL", "result_type": "numeric"},
    "K": {"name": "Release potassium", "short_name": "Rel K", "decimal_places": 1,
          "unit": "mmol/L", "result_type": "numeric"},
    "TROP": {"name": "Release troponin", "short_name": "Rel Trop", "decimal_places": 2,
             "unit": "ng/mL", "result_type": "numeric"},
    "HBS": {"name": "Release hepatitis B surface antigen", "short_name": "Rel HBsAg",
            "result_type": "coded", "coded_values_json": ["Reactive", "Non-reactive"]},
}
PANEL = "PNL"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _user(role, dept=None):
    uid = f"REL{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role.title()} {uid}", role, dept, hash_password("password")))
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
                    "name": "Release mini panel", "short_name": "Rel panel",
                    "section_id": "biochemistry", "specimen_type_code": "serum"}, actor=master)
    lm.set_panel_components(lm.test_id_for("LOCAL", _code(PANEL)),
                            [lm.test_id_for("LOCAL", _code("ALB")),
                             lm.test_id_for("LOCAL", _code("K"))], actor=master)
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
        db.execute(f"DELETE FROM lab_critical_limits WHERE test_id IN ({tmarks})",
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


def _patient(created, *, dob="1980-05-01", sex="female"):
    pid = f"PREL{uuid.uuid4().hex[:8].upper()}"
    name = f"Release {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-REL-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed, date_of_birth, sex) "
                   "VALUES (?, ?, ?, 'admitted', 'GEN-1', ?, ?, ?)",
                   (pid, name, mrn, f"B{uuid.uuid4().hex[:3]}", dob, sex))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"AREL{uuid.uuid4().hex[:8].upper()}", pid, WARD))
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
    return _user("pathologist")


@pytest.fixture(scope="module")
def second_signer():
    return _user("pathologist")


def _received(p, phleb_hdr, tech_hdr, keys=("K",), *, priority="routine"):
    """Orders for these tests (one order each), taken in on ONE tube,
    labelled, collected and received. Returns (orders, specimen_id,
    {key: item_id})."""
    orders = []
    for key in keys:
        orders.append(orders_service.create_order(
            patient_id=p["id"], order_type="lab", description=key, code=_code(key),
            code_system="LOCAL", priority=priority, ordered_by=DOCTOR_ID, status="active"))
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            f"SELECT i.id, i.specimen_id, i.test_id FROM lab_order_items i "
            f"WHERE i.order_id IN ({', '.join('?' for _ in orders)}) ORDER BY i.id",
            tuple(o["id"] for o in orders)).fetchall()]
    specimens = {r["specimen_id"] for r in rows}
    assert len(specimens) == 1, "the tests should share one tube"
    specimen_id = specimens.pop()
    identity = {"uhid": p["mrn"], "name": p["name"]}
    resp = client.post("/api/lab/collection/labels",
                       json={"specimen_ids": [specimen_id], "identity": identity},
                       headers=phleb_hdr)
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/lab/specimens/{specimen_id}/collect",
                       json={"identity": identity}, headers=phleb_hdr)
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/lab/specimens/{specimen_id}/receive", json={}, headers=tech_hdr)
    assert resp.status_code == 200, resp.text
    by_test = {r["test_id"]: r["id"] for r in rows}
    items = {}
    for key in keys:
        items[key] = by_test[lm.test_id_for("LOCAL", _code(key))]
    return orders, specimen_id, items


def _received_panel(p, phleb_hdr, tech_hdr):
    order = orders_service.create_order(
        patient_id=p["id"], order_type="lab", description="Release mini panel",
        code=_code(PANEL), code_system="LOCAL", priority="routine", ordered_by=DOCTOR_ID,
        status="active")
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, specimen_id, test_id FROM lab_order_items WHERE order_id = ? "
            "ORDER BY id", (order["id"],)).fetchall()]
    specimen_id = rows[0]["specimen_id"]
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
        key = "ALB" if r["test_id"].endswith("-ALB") else "K"
        items[key] = r["id"]
    return order, specimen_id, items


def _enter(item_id, value, hdr):
    resp = client.post("/api/lab/results", json={"order_item_id": item_id, "value": value},
                       headers=hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _seen(result):
    return {"value_text": result["value_text"], "comment": result.get("comment"),
            "abnormal_flag": result.get("abnormal_flag")}


def _validate(results, hdr):
    return client.post("/api/lab/results/tech-validate",
                       json={"results": [{"result_id": r["id"], "expected": _seen(r)}
                                         for r in results]}, headers=hdr)


def _authorise(specimen_id, results, hdr):
    return client.post("/api/lab/results/authorise",
                       json={"specimen_id": specimen_id,
                             "results": [{"result_id": r["id"], "expected": _seen(r)}
                                         for r in results]}, headers=hdr)


def _validated(item_id, value, tech_hdr):
    result = _enter(item_id, value, tech_hdr)
    resp = _validate([result], tech_hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()["results"][0]


def _stored(table, where, params):
    with db_session() as db:
        return [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {where}",
                                            params).fetchall()]


def _observations(result_id):
    return _stored("observations", "lab_result_id = ?", (result_id,))


# ── 1. Who may do what ──────────────────────────────────────────────────────

def test_a_technician_never_authorises(lab_on, patient, tech, phleb):
    _, specimen_id, items = _received(patient, phleb[1], tech[1])
    result = _validated(items["K"], "4.4", tech[1])
    resp = _authorise(specimen_id, [result], tech[1])
    assert resp.status_code == 403
    phleb_resp = _authorise(specimen_id, [result], phleb[1])
    assert phleb_resp.status_code == 403
    assert _observations(result["id"]) == []
    with pytest.raises(lrs.LabResultError) as refused:
        lrs.authorise_and_release(specimen_id, [{"result_id": result["id"],
                                                 "expected": _seen(result)}],
                                  actor={"id": tech[0], "role": "lab_technician"})
    assert refused.value.status == 403 and refused.value.code == "permission_denied"


def test_a_phlebotomist_cannot_validate(lab_on, patient, tech, phleb):
    _, _, items = _received(patient, phleb[1], tech[1])
    result = _enter(items["K"], "4.4", tech[1])
    assert _validate([result], phleb[1]).status_code == 403


def test_the_person_who_entered_cannot_authorise_while_two_people_are_required(
        lab_on, patient, tech, phleb, signer, second_signer):
    _, specimen_id, items = _received(patient, phleb[1], tech[1])
    # The signatory enters the value herself; a technician validates it.
    entered = _enter(items["K"], "4.6", signer[1])
    validated = _validate([entered], tech[1]).json()["results"][0]
    refused = _authorise(specimen_id, [validated], signer[1])
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "second_person_required"
    assert _observations(validated["id"]) == []
    assert _stored("lab_results", "id = ?", (validated["id"],))[0]["status"] == "tech_validated"
    released = _authorise(specimen_id, [validated], second_signer[1])
    assert released.status_code == 200, released.text
    assert released.json()["one_person"] == 0


def test_the_person_who_validated_cannot_authorise_either(lab_on, patient, tech, phleb,
                                                          signer, second_signer):
    _, specimen_id, items = _received(patient, phleb[1], tech[1])
    entered = _enter(items["K"], "4.6", tech[1])
    validated = _validate([entered], signer[1]).json()["results"][0]
    refused = _authorise(specimen_id, [validated], signer[1])
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "second_person_required"
    assert _authorise(specimen_id, [validated], second_signer[1]).status_code == 200


def test_a_person_who_changed_the_value_cannot_authorise_it(lab_on, patient, tech, phleb,
                                                            signer):
    _, specimen_id, items = _received(patient, phleb[1], tech[1])
    entered = _enter(items["K"], "4.6", tech[1])
    edited = client.patch(f"/api/lab/results/{entered['id']}",
                          json={"value": "4.7", "expected": _seen(entered)},
                          headers=signer[1])
    assert edited.status_code == 200, edited.text
    validated = _validate([edited.json()["result"]], tech[1]).json()["results"][0]
    refused = _authorise(specimen_id, [validated], signer[1])
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "second_person_required"


def test_one_signatory_may_do_every_step_when_the_site_allows_it(lab_on, _catalogue, patient,
                                                                  tech, phleb, signer,
                                                                  monkeypatch):
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)
    _, specimen_id, items = _received(patient, phleb[1], tech[1])
    entered = _enter(items["K"], "4.6", signer[1])
    validated = _validate([entered], signer[1]).json()["results"][0]
    resp = _authorise(specimen_id, [validated], signer[1])
    assert resp.status_code == 200, resp.text
    assert resp.json()["one_person"] == 1
    events = _stored("lab_result_events", "result_id = ? AND event_type = 'released'",
                     (validated["id"],))
    assert json.loads(events[0]["after_json"])["one_person"] is True
    audit = _stored("audit_logs", "action_type = 'lab_result_released' AND target_id = ?",
                    (str(validated["id"]),))[0]
    assert json.loads(audit["metadata_json"])["one_person"] is True
    # A technician still never authorises, whatever the setting.
    _, other_specimen, other = _received(_patient(_catalogue), phleb[1], tech[1])
    tech_result = _validated(other["K"], "4.1", tech[1])
    assert _authorise(other_specimen, [tech_result], tech[1]).status_code == 403


def test_the_setting_is_strict_unless_switched_off_explicitly(monkeypatch):
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", True)
    assert lrs.distinct_authoriser_required() is True
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", None)
    assert lrs.distinct_authoriser_required() is True
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", False)
    assert lrs.distinct_authoriser_required() is False


# ── 2. What a release writes ────────────────────────────────────────────────

def test_release_writes_one_observation_per_analyte(lab_on, patient, tech, phleb, signer):
    orders, specimen_id, items = _received(patient, phleb[1], tech[1], ("ALB", "K"))
    alb = _validated(items["ALB"], "4.5", tech[1])
    potassium = _validated(items["K"], "3.9", tech[1])
    resp = _authorise(specimen_id, [alb, potassium], signer[1])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["released"] == 2 and len(body["observation_ids"]) == 2
    with db_session() as db:
        collected = db.execute("SELECT collected_at FROM lab_specimens WHERE id = ?",
                               (specimen_id,)).fetchone()[0]
    obs = _observations(alb["id"])
    assert len(obs) == 1
    row = obs[0]
    assert row["observed_at"] == to_utc_iso(collected)
    assert row["value_text"] == "4.50" and row["value_numeric"] == 4.5
    assert (row["source"], row["status"], row["unit"]) == ("lis_manual", "final", "g/dL")
    assert (row["lab_result_id"], row["specimen_id"], row["obr_group_id"]) == (
        alb["id"], specimen_id, specimen_id)
    assert row["order_id"] == orders[0]["id"]
    assert row["observation_code"] == _code("ALB") and row["observation_code_system"] == "LOCAL"
    assert row["reference_range"] == "Reference range not configured"
    assert row["patient_id"] == patient["id"]
    stored = _stored("lab_results", "id = ?", (alb["id"],))[0]
    assert (stored["status"], stored["released_by"], stored["observation_id"]) == (
        "released", signer[0], row["id"])
    assert _stored("lab_order_items", "id = ?", (items["ALB"],))[0]["status"] == "released"
    audit = _stored("audit_logs", "action_type = 'lab_result_released' AND target_id = ?",
                    (str(alb["id"]),))
    assert len(audit) == 1 and audit[0]["event_category"] == "clinical_access"
    assert audit[0]["patient_id"] == patient["id"]
    assert len(_observations(potassium["id"])) == 1


def test_a_bound_is_released_with_no_number(lab_on, patient, tech, phleb, signer):
    _, specimen_id, items = _received(patient, phleb[1], tech[1], ("TROP",))
    bound = _validated(items["TROP"], "<0.01", tech[1])
    assert _authorise(specimen_id, [bound], signer[1]).status_code == 200
    row = _observations(bound["id"])[0]
    assert row["value_numeric"] is None and row["value_text"] == "<0.01"


def test_a_coded_result_is_released_as_text(lab_on, patient, tech, phleb, signer):
    _, specimen_id, items = _received(patient, phleb[1], tech[1], ("HBS",))
    coded = _validated(items["HBS"], "non-reactive", tech[1])
    assert _authorise(specimen_id, [coded], signer[1]).status_code == 200
    row = _observations(coded["id"])[0]
    assert row["value_numeric"] is None and row["value_text"] == "Non-reactive"


def test_an_audit_failure_releases_nothing(lab_on, patient, tech, phleb, signer, monkeypatch):
    _, specimen_id, items = _received(patient, phleb[1], tech[1], ("ALB", "K"))
    alb = _validated(items["ALB"], "4.5", tech[1])
    potassium = _validated(items["K"], "3.9", tech[1])
    real = lrs.log_audit_event
    calls = {"n": 0}

    def failing_second(**kwargs):
        # The first result's audit row succeeds; the second fails, so the
        # whole authorisation must roll back.
        if kwargs.get("action_type") == "lab_result_released":
            calls["n"] += 1
            if calls["n"] == 2:
                raise AuditWriteError("injected")
        return real(**kwargs)

    monkeypatch.setattr(lrs, "log_audit_event", failing_second)
    resp = _authorise(specimen_id, [alb, potassium], signer[1])
    assert resp.status_code == 503
    for result, item in ((alb, items["ALB"]), (potassium, items["K"])):
        assert _observations(result["id"]) == []
        assert _stored("lab_results", "id = ?", (result["id"],))[0]["status"] == "tech_validated"
        assert _stored("lab_order_items", "id = ?", (item,))[0]["status"] == "tech_validated"
    assert _stored("lab_specimens", "id = ?", (specimen_id,))[0]["status"] == "in_process"


def test_a_validation_whose_audit_fails_validates_nothing(lab_on, patient, tech, phleb,
                                                          monkeypatch):
    _, _, items = _received(patient, phleb[1], tech[1])
    result = _enter(items["K"], "4.4", tech[1])

    def failing(**kwargs):
        raise AuditWriteError("injected")

    monkeypatch.setattr(lrs, "log_audit_event", failing)
    assert _validate([result], tech[1]).status_code == 503
    assert _stored("lab_results", "id = ?", (result["id"],))[0]["status"] == "entered"


def test_one_bad_result_in_a_batch_releases_none(lab_on, patient, tech, phleb, signer):
    _, specimen_id, items = _received(patient, phleb[1], tech[1], ("ALB", "K"))
    alb = _validated(items["ALB"], "4.5", tech[1])
    potassium = _enter(items["K"], "3.9", tech[1])  # entered, not validated
    resp = _authorise(specimen_id, [alb, potassium], signer[1])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "not_validated"
    assert _observations(alb["id"]) == []


def test_a_result_from_another_sample_is_refused(lab_on, _catalogue, tech, phleb, signer):
    _, first_specimen, first = _received(_patient(_catalogue), phleb[1], tech[1])
    _, second_specimen, second = _received(_patient(_catalogue), phleb[1], tech[1])
    one = _validated(first["K"], "4.1", tech[1])
    two = _validated(second["K"], "4.2", tech[1])
    resp = _authorise(first_specimen, [one, two], signer[1])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "not_on_this_sample"
    assert _observations(one["id"]) == []
    misread = first_specimen[:-1] + str((int(first_specimen[-1]) + 1) % 10)
    bad = client.post("/api/lab/results/authorise",
                      json={"specimen_id": misread,
                            "results": [{"result_id": one["id"], "expected": _seen(one)}]},
                      headers=signer[1])
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "bad_accession"


def test_a_stale_screen_cannot_validate_or_release(lab_on, patient, tech, phleb, signer):
    _, specimen_id, items = _received(patient, phleb[1], tech[1])
    entered = _enter(items["K"], "4.4", tech[1])
    stale = {**entered, "value_text": "4.3"}
    refused = _validate([stale], tech[1])
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["error"] == "result_changed" and "current" not in detail
    validated = _validate([entered], tech[1]).json()["results"][0]
    stale_release = _authorise(specimen_id, [{**validated, "comment": "different"}], signer[1])
    assert stale_release.status_code == 409
    assert stale_release.json()["detail"]["error"] == "result_changed"
    assert _observations(validated["id"]) == []


def test_validation_moves_the_result_off_the_worklist(lab_on, patient, tech, phleb):
    _, _, items = _received(patient, phleb[1], tech[1])
    result = _enter(items["K"], "4.4", tech[1])
    resp = _validate([result], tech[1])
    assert resp.status_code == 200, resp.text
    validated = resp.json()["results"][0]
    assert (validated["status"], validated["tech_validated_by"]) == ("tech_validated", tech[0])
    assert _stored("lab_order_items", "id = ?", (items["K"],))[0]["status"] == "tech_validated"
    audit = _stored("audit_logs", "action_type = 'lab_result_tech_validated' AND target_id = ?",
                    (str(result["id"]),))
    assert len(audit) == 1 and audit[0]["event_category"] == "clinical_access"
    bench = client.get("/api/lab/worklists/biochemistry", headers=tech[1]).json()
    assert items["K"] not in [i["item_id"] for i in bench["items"]]
    again = _validate([validated], tech[1])
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_validated"
    edit = client.patch(f"/api/lab/results/{result['id']}",
                        json={"value": "4.5", "expected": _seen(validated)}, headers=tech[1])
    assert edit.status_code == 409 and edit.json()["detail"]["error"] == "already_validated"


# ── 3. The completion rule, billing and the order's state ───────────────────

def test_a_panel_releases_analyte_by_analyte_and_bills_only_when_complete(
        lab_on, patient, tech, phleb, signer):
    order, specimen_id, items = _received_panel(patient, phleb[1], tech[1])
    alb = _validated(items["ALB"], "4.2", tech[1])
    potassium = _validated(items["K"], "4.0", tech[1])

    def billed():
        with db_session() as db:
            return [c for c in billing_service._clinical_candidates(db, patient_id=patient["id"])
                    if c["source_ref"] == order["id"]]

    assert _authorise(specimen_id, [alb], signer[1]).status_code == 200
    with db_session() as db:
        status = db.execute("SELECT status FROM orders WHERE id = ?", (order["id"],)).fetchone()[0]
    assert status == "in_progress"
    assert billed() == []
    assert _stored("lab_specimens", "id = ?", (specimen_id,))[0]["status"] == "in_process"
    # The released albumin is on the chart; the validated potassium is not.
    with db_session() as db:
        chart = lobs.chart_lab_rows(db, patient["id"], per_analyte=10, max_rows=50)
    assert [r["lab_result_id"] for r in chart] == [alb["id"]]

    resp = _authorise(specimen_id, [potassium], signer[1])
    assert resp.status_code == 200, resp.text
    assert resp.json()["orders"] == [{"id": order["id"], "status": "resulted", "conflict": False}]
    with db_session() as db:
        row = dict(db.execute("SELECT status, result_text FROM orders WHERE id = ?",
                              (order["id"],)).fetchone())
        events = [dict(r) for r in db.execute(
            "SELECT new_status FROM order_events WHERE order_id = ? ORDER BY id",
            (order["id"],)).fetchall()]
    assert row == {"status": "resulted", "result_text": los.LAB_RESULT_TEXT}
    assert events[-1]["new_status"] == "resulted"
    assert len(billed()) == 1
    assert _stored("lab_specimens", "id = ?", (specimen_id,))[0]["status"] == "completed"
    assert resp.json()["specimen"]["status"] == "completed"


def test_a_cancelled_test_and_a_released_one_complete_the_order(lab_on, patient, tech, phleb,
                                                                signer):
    orders, specimen_id, items = _received(patient, phleb[1], tech[1], ("ALB", "K"))
    alb = _validated(items["ALB"], "4.2", tech[1])
    cancel = client.post(f"/api/lab/orders/{orders[1]['id']}/cancel",
                         json={"reason": "Duplicate request"}, headers=tech[1])
    assert cancel.status_code == 200, cancel.text
    assert _authorise(specimen_id, [alb], signer[1]).status_code == 200
    assert _stored("orders", "id = ?", (orders[0]["id"],))[0]["status"] == "resulted"
    assert _stored("orders", "id = ?", (orders[1]["id"],))[0]["status"] == "cancelled"
    assert _stored("lab_specimens", "id = ?", (specimen_id,))[0]["status"] == "completed"


def test_release_onto_a_manually_completed_order_lands_and_records_an_exception(
        lab_on, patient, tech, phleb, signer):
    orders, specimen_id, items = _received(patient, phleb[1], tech[1])
    validated = _validated(items["K"], "4.4", tech[1])
    with db_session() as db:
        # A legacy completion made by hand before the laboratory guard existed.
        db.execute("UPDATE orders SET status = 'completed' WHERE id = ?", (orders[0]["id"],))
    resp = _authorise(specimen_id, [validated], signer[1])
    assert resp.status_code == 200, resp.text
    assert resp.json()["orders"][0]["conflict"] is True
    assert len(_observations(validated["id"])) == 1
    assert _stored("orders", "id = ?", (orders[0]["id"],))[0]["status"] == "completed"
    open_exceptions = _stored(
        "lab_exceptions", "order_id = ? AND kind = 'order_state_conflict' AND resolved_at IS NULL",
        (orders[0]["id"],))
    assert len(open_exceptions) == 1


def test_the_chart_shows_released_values_only(lab_on, patient, tech, phleb, signer):
    _, specimen_id, items = _received(patient, phleb[1], tech[1])
    entered = _enter(items["K"], "4.4", tech[1])

    def chart():
        with db_session() as db:
            return lobs.chart_lab_rows(db, patient["id"], per_analyte=10, max_rows=50)

    assert chart() == []
    validated = _validate([entered], tech[1]).json()["results"][0]
    assert chart() == []
    assert _authorise(specimen_id, [validated], signer[1]).status_code == 200
    rows = chart()
    assert len(rows) == 1 and rows[0]["lab_result_id"] == entered["id"]
    assert rows[0]["value_numeric"] == 4.4


# ── 4. The sample's progress ────────────────────────────────────────────────

def test_the_sample_moves_to_in_process_at_entry(lab_on, patient, tech, phleb):
    _, specimen_id, items = _received(patient, phleb[1], tech[1])
    assert _stored("lab_specimens", "id = ?", (specimen_id,))[0]["status"] == "received"
    _enter(items["K"], "4.4", tech[1])
    assert _stored("lab_specimens", "id = ?", (specimen_id,))[0]["status"] == "in_process"
    events = [e["event_type"] for e in _stored("lab_specimen_events", "specimen_id = ? "
                                               "ORDER BY seq", (specimen_id,))]
    assert events[-1] == "in_process"


@pytest.mark.parametrize("statuses, target", [
    (["released"], "completed"),
    (["released", "cancelled"], "completed"),
    (["cancelled", "cancelled"], None),
    (["released", "tech_validated"], "in_process"),
    (["resulted", "received"], "in_process"),
    (["received"], None),
    (["released", "rejected"], "in_process"),
])
def test_the_sample_progress_rule(statuses, target):
    assert los.specimen_progress_target(statuses) == target


# ── 5. Sending a result back ────────────────────────────────────────────────

def test_a_signatory_sends_a_result_back_to_the_bench(lab_on, patient, tech, phleb, signer):
    _, _, items = _received(patient, phleb[1], tech[1])
    validated = _validated(items["K"], "4.4", tech[1])
    no_reason = client.post(f"/api/lab/results/{validated['id']}/return",
                            json={"reason": "  ", "expected": _seen(validated)},
                            headers=signer[1])
    assert no_reason.status_code == 422
    assert client.post(f"/api/lab/results/{validated['id']}/return",
                       json={"reason": "Haemolysed?", "expected": _seen(validated)},
                       headers=tech[1]).status_code == 403
    resp = client.post(f"/api/lab/results/{validated['id']}/return",
                       json={"reason": "Check the sample for haemolysis",
                             "expected": _seen(validated)}, headers=signer[1])
    assert resp.status_code == 200, resp.text
    back = resp.json()["result"]
    assert (back["status"], back["tech_validated_by"]) == ("entered", None)
    assert _stored("lab_order_items", "id = ?", (items["K"],))[0]["status"] == "resulted"
    bench = client.get("/api/lab/worklists/biochemistry", headers=tech[1]).json()
    row = next(i for i in bench["items"] if i["item_id"] == items["K"])
    assert row["result"]["returned"]["reason"] == "Check the sample for haemolysis"
    audit = _stored("audit_logs", "action_type = 'lab_result_returned' AND target_id = ?",
                    (str(validated["id"]),))
    assert len(audit) == 1 and audit[0]["event_category"] == "clinical_access"
    # A technologist who validated before the return still took part.
    again = _validate([back], tech[1]).json()["results"][0]
    queues = client.get("/api/lab/validation", headers=signer[1]).json()
    entry = next(r for g in queues["authorisation"] for r in g["results"]
                 if r["result_id"] == again["id"])
    assert entry["you_took_part"] is False and entry["may_authorise"] is True


# ── 6. The Validate screen's queues ─────────────────────────────────────────

def test_the_queues_list_each_step_and_are_audited(lab_on, patient, tech, phleb, signer):
    _, specimen_id, items = _received(patient, phleb[1], tech[1], ("ALB", "K"))
    alb = _enter(items["ALB"], "4.2", tech[1])
    potassium = _validated(items["K"], "4.0", tech[1])
    resp = client.get("/api/lab/validation", headers=tech[1])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    technical = [r["result_id"] for g in body["technical"] for r in g["results"]]
    waiting = {r["result_id"]: r for g in body["authorisation"] for r in g["results"]}
    assert alb["id"] in technical and potassium["id"] in waiting
    assert body["can_authorise"] is False and body["require_distinct_authoriser"] is True
    assert waiting[potassium["id"]]["you_took_part"] is True
    assert waiting[potassium["id"]]["may_authorise"] is False
    group = next(g for g in body["authorisation"] if g["specimen_id"] == specimen_id)
    assert group["patient"]["id"] == patient["id"]
    as_signer = client.get("/api/lab/validation", headers=signer[1]).json()
    entry = next(r for g in as_signer["authorisation"] for r in g["results"]
                 if r["result_id"] == potassium["id"])
    assert as_signer["can_authorise"] is True and entry["may_authorise"] is True
    audit = _stored("audit_logs", "action_type = 'lab_validation_viewed' AND actor_user_id = ? "
                    "ORDER BY id DESC", (tech[0],))
    assert audit and audit[0]["event_category"] == "phi_access"
    assert patient["id"] in json.loads(audit[0]["metadata_json"])["patient_ids"]
    assert client.get("/api/lab/validation", headers=phleb[1]).status_code == 403


# ── 7. Critical flags (ruling LIS-R1) ───────────────────────────────────────

@pytest.fixture
def potassium_limit(master):
    test_id = lm.test_id_for("LOCAL", _code("K"))
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?", (test_id,))
    row = lm.approve_critical_limit(test_id, {"critical_low": "2.5", "critical_high": "6.5",
                                              "agreed_with": "Dr Ward, physician lead",
                                              "method_note": "Release test limit"},
                                    actor=master)
    yield row
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?", (test_id,))


@pytest.mark.parametrize("value, low, high, comparator, bound, flag", [
    (7000, 2500, 6500, None, None, "HH"),
    (6500, 2500, 6500, None, None, ""),
    (2400, 2500, 6500, None, None, "LL"),
    (2500, 2500, 6500, None, None, ""),
    (None, 2500, 6500, ">", 6500, "HH"),
    (None, 2500, 6500, ">=", 6500, ""),
    (None, 2500, 6500, ">=", 6600, "HH"),
    (None, 2500, 6500, ">", 6000, ""),
    (None, 2500, 6500, "<", 2500, "LL"),
    (None, 2500, 6500, "<=", 2500, ""),
    (None, 2500, None, ">", 9000, ""),
])
def test_the_critical_rule(value, low, high, comparator, bound, flag):
    assert lrs.critical_flag(value, low_milli=low, high_milli=high, decimal_places=1,
                             comparator=comparator, bound_milli=bound) == flag


def test_a_value_past_an_approved_critical_limit_is_critical(lab_on, _catalogue, tech, phleb,
                                                             signer, potassium_limit):
    _, specimen_id, items = _received(_patient(_catalogue), phleb[1], tech[1])
    high = _enter(items["K"], "7.0", tech[1])
    assert (high["abnormal_flag"], high["is_critical"], high["critical_limit_id"]) == (
        "HH", 1, potassium_limit["id"])
    assert high["flag_text"] == "Critically high"
    assert high["critical_limit_text"] == "Critical below 2.5 or above 6.5 mmol/L"
    _, _, on_limit_items = _received(_patient(_catalogue), phleb[1], tech[1])
    on_limit = _enter(on_limit_items["K"], "6.5", tech[1])
    assert (on_limit["abnormal_flag"], on_limit["is_critical"]) == ("", 0)
    validated = _validate([high], tech[1]).json()["results"][0]
    assert _authorise(specimen_id, [validated], signer[1]).status_code == 200
    assert _observations(high["id"])[0]["abnormal_flag"] == "HH"


def test_no_critical_limit_means_no_critical_flag(lab_on, patient, tech, phleb):
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?",
                   (lm.test_id_for("LOCAL", _code("K")),))
    _, _, items = _received(patient, phleb[1], tech[1])
    high = _enter(items["K"], "9.9", tech[1])
    assert (high["abnormal_flag"], high["is_critical"], high["critical_limit_id"]) == ("", 0, None)


# ── 8. Module state ─────────────────────────────────────────────────────────

def test_validation_and_release_are_served_while_the_module_drains(lab_on, patient, tech, phleb,
                                                                   signer, monkeypatch):
    _, specimen_id, items = _received(patient, phleb[1], tech[1])
    entered = _enter(items["K"], "4.4", tech[1])
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    assert client.get("/api/lab/validation", headers=tech[1]).status_code == 200
    validated = _validate([entered], tech[1])
    assert validated.status_code == 200, validated.text
    assert _authorise(specimen_id, validated.json()["results"], signer[1]).status_code == 200


def test_validation_routes_are_404_with_the_module_off(tech, signer, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    for method, path, hdr in (
            ("get", "/api/lab/validation", tech[1]),
            ("post", "/api/lab/results/tech-validate", tech[1]),
            ("post", "/api/lab/results/authorise", signer[1]),
            ("post", "/api/lab/results/1/delta-ack", tech[1]),
            ("post", "/api/lab/results/1/return", signer[1])):
        resp = getattr(client, method)(path, headers=hdr, **({} if method == "get" else
                                                            {"json": {}}))
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"
