"""Section worklists and manual result entry with flags (lis-10).

Pinned here:
  * The worklist: tests on received samples only, STAT first then the oldest
    receipt; a cancelled test leaves it and is listed with who cancelled it
    and when; each read audited as phi_access.
  * Precision: a value with more decimal places than the test reports is
    refused, never rounded; fewer is the same number, printed at the test's
    precision.
  * Flags only against an approved range (rulings LIS-R2 and LIS-R7): a value
    equal to a limit is not flagged; no range means no flag and says so; a
    patient with no date of birth gets only a range covering every age.
  * A comparator result ('<0.01') keeps its bound as text with value_milli
    NULL and gets no H or L.
  * A coded value outside the test's list is refused.
  * Edits before validation keep the value before and after, with audit; a
    validated result is not edited here.
  * An edit says what its screen showed; if someone else changed the result
    since, it is refused 409 'result_changed' with the current row (audited
    as a read), so a stale screen never undoes another person's correction.
  * One live result per test: a second entry is refused, and the partial
    unique index refuses a second live row even written by hand.
  * Every write is one transaction with its audit row; only result staff
    enter results; entry is served while the module drains.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from db_backend import DBIntegrityError
from services import lab_module
from services import lab_master_service as lm
from services import lab_result_service as lrs
from services import orders_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import to_utc_iso, utc_iso_now

client = TestClient(main.app)

DOCTOR_ID = "D001"
WARD = "General Medicine"
SUFFIX = uuid.uuid4().hex[:5].upper()


def _code(name):
    return f"W5D{SUFFIX}-{name}"


# Tests of this file's own, so no range it approves can touch another file's.
TESTS = {
    "K": {"name": "Bench potassium", "short_name": "Bench K", "decimal_places": 1,
          "unit": "mmol/L", "result_type": "numeric"},
    "CR": {"name": "Bench creatinine", "short_name": "Bench Cr", "decimal_places": 2,
           "unit": "mg/dL", "result_type": "numeric"},
    "NA": {"name": "Bench sodium", "short_name": "Bench Na", "decimal_places": 0,
           "unit": "mmol/L", "result_type": "numeric"},
    "HBS": {"name": "Bench hepatitis B surface antigen", "short_name": "Bench HBsAg",
            "result_type": "coded", "coded_values_json": ["Reactive", "Non-reactive"]},
    "NOTE": {"name": "Bench smear comment", "short_name": "Bench smear",
             "result_type": "text"},
}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pathologist():
    uid = f"RES{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, 'pathologist', NULL, ?)",
                   (uid, f"Pathologist {uid}", hash_password("password")))
    return {"id": uid, "role": "pathologist"}


@pytest.fixture(scope="module", autouse=True)
def _catalogue(pathologist):
    """The starter catalogue plus this file's own tests, and at the end every
    laboratory row this file wrote removed, so no open work makes the module
    'draining' and no range it approved stays behind."""
    terminology_service.seed_terminology()
    lm.load_starter_catalogue()
    for key, spec in TESTS.items():
        lm.create_test({"code_system": "LOCAL", "code": _code(key), "kind": "analyte",
                        "section_id": "biochemistry", "specimen_type_code": "serum", **spec},
                       actor=pathologist)
    created: list[str] = []
    yield created
    test_ids = [lm.test_id_for("LOCAL", _code(k)) for k in TESTS]
    tmarks = ", ".join("?" for _ in test_ids)
    with db_session() as db:
        if created:
            marks = ", ".join("?" for _ in created)
            ids = tuple(created)
            db.execute(f"DELETE FROM lab_exceptions WHERE order_id IN "
                       f"(SELECT id FROM orders WHERE patient_id IN ({marks}))", ids)
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
        db.execute(f"DELETE FROM lab_tests WHERE id IN ({tmarks})", tuple(test_ids))
        db.execute(f"DELETE FROM terminology WHERE code_system = 'LOCAL' AND code IN ({tmarks})",
                   tuple(_code(k) for k in TESTS))
    lab_module.refresh_drain_state()


@pytest.fixture
def lab_on(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    with db_session() as db:
        event_id = db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, ?)",
            (utc_iso_now(), "test cutover")).lastrowid
    yield
    with db_session() as db:
        db.execute("DELETE FROM lab_module_events WHERE id = ?", (event_id,))


def _patient(created, *, dob="1980-05-01", sex="female"):
    pid = f"PRES{uuid.uuid4().hex[:8].upper()}"
    name = f"Bench {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-RES-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed, date_of_birth, sex) "
                   "VALUES (?, ?, ?, 'admitted', 'GEN-1', ?, ?, ?)",
                   (pid, name, mrn, f"B{uuid.uuid4().hex[:3]}", dob, sex))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"ARES{uuid.uuid4().hex[:8].upper()}", pid, WARD))
    created.append(pid)
    return {"id": pid, "name": name, "mrn": mrn}


@pytest.fixture
def patient(_catalogue):
    return _patient(_catalogue)


def _user(role, dept=None):
    uid = f"RES{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role.title()} {uid}", role, dept, hash_password("password")))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def tech():
    return _user("lab_technician")


@pytest.fixture(scope="module")
def phleb():
    return _user("phlebotomist")


def _received(p, phleb_hdr, tech_hdr, key="K", *, priority="routine"):
    """An order for one of this file's tests, taken in, labelled, collected
    and received. Returns (order, specimen_id, item_id)."""
    order = orders_service.create_order(
        patient_id=p["id"], order_type="lab", description=TESTS[key]["name"],
        code=_code(key), code_system="LOCAL", priority=priority, ordered_by=DOCTOR_ID,
        status="active")
    with db_session() as db:
        item = db.execute("SELECT id, specimen_id FROM lab_order_items WHERE order_id = ?",
                          (order["id"],)).fetchone()
    assert item is not None, "the laboratory should have taken the order in"
    identity = {"uhid": p["mrn"], "name": p["name"]}
    resp = client.post("/api/lab/collection/labels",
                       json={"specimen_ids": [item["specimen_id"]], "identity": identity},
                       headers=phleb_hdr)
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/lab/specimens/{item['specimen_id']}/collect",
                       json={"identity": identity}, headers=phleb_hdr)
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/lab/specimens/{item['specimen_id']}/receive", json={},
                       headers=tech_hdr)
    assert resp.status_code == 200, resp.text
    return order, item["specimen_id"], item["id"]


def _enter(item_id, value, hdr, comment=None):
    body = {"order_item_id": item_id, "value": value}
    if comment is not None:
        body["comment"] = comment
    return client.post("/api/lab/results", json=body, headers=hdr)


def _seen(result):
    """What a bench screen showing `result` sends as `expected`."""
    return {"value_text": result["value_text"], "comment": result.get("comment"),
            "abnormal_flag": result.get("abnormal_flag")}


def _edit(result, value, hdr, *, seen=None, **extra):
    """PATCH a result from a screen that showed `seen` (default: `result`)."""
    body = {"value": value, "expected": _seen(seen if seen is not None else result), **extra}
    return client.patch(f"/api/lab/results/{result['id']}", json=body, headers=hdr)


def _approve(pathologist, key, low=None, high=None, **band):
    data = {"method_note": "Bench analyser, manufacturer insert", **band}
    if low is not None:
        data["low"] = low
    if high is not None:
        data["high"] = high
    return lm.approve_reference_range(lm.test_id_for("LOCAL", _code(key)), data,
                                      actor=pathologist)


def _clear_ranges(key):
    with db_session() as db:
        db.execute("DELETE FROM lab_reference_ranges WHERE test_id = ?",
                   (lm.test_id_for("LOCAL", _code(key)),))


@pytest.fixture
def potassium_range(pathologist):
    _clear_ranges("K")
    row = _approve(pathologist, "K", low="3.5", high="5.1")
    yield row
    _clear_ranges("K")


def _worklist(hdr, section="biochemistry"):
    resp = client.get(f"/api/lab/worklists/{section}", headers=hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── 1. Pure rules ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("typed, places, expected", [
    ("4.5", 1, (None, 4500, "4.5")),
    ("4.50", 1, (None, 4500, "4.5")),
    ("4", 1, (None, 4000, "4.0")),
    ("1.2", 2, (None, 1200, "1.20")),
    ("<0.01", 2, ("<", None, "<0.01")),
    ("> 600", 0, (">", None, ">600")),
    ("≤0.5", 1, ("<=", None, "<=0.5")),
    ("-2", 0, (None, -2000, "-2")),
])
def test_a_typed_value_is_read_at_the_reporting_precision(typed, places, expected):
    parsed = lrs.parse_numeric(typed, places)
    assert (parsed["comparator"], parsed["value_milli"], parsed["value_text"]) == expected


@pytest.mark.parametrize("typed, places, code", [
    ("4.56", 1, "precision"),
    ("<0.001", 2, "precision"),
    ("5.1", 0, "precision"),
    ("", 1, "value_required"),
    ("four", 1, "not_a_number"),
    ("1e3", 0, "not_a_number"),
    ("1,000", 0, "not_a_number"),
])
def test_a_value_the_test_cannot_take_is_refused(typed, places, code):
    with pytest.raises(lrs.LabResultError) as refused:
        lrs.parse_numeric(typed, places)
    assert refused.value.code == code and refused.value.status == 422


@pytest.mark.parametrize("value, flag", [
    (5100, ""), (3500, ""), (5200, "H"), (3400, "L"), (4200, ""),
])
def test_a_value_on_a_limit_is_within_the_range(value, flag):
    assert lrs.range_flag(value, low_milli=3500, high_milli=5100, decimal_places=1) == flag


def test_flags_are_decided_at_the_reporting_precision():
    # 5.14 is printed as 5.1 by a one-place test, so it is not above 5.1.
    assert lrs.range_flag(5140, low_milli=None, high_milli=5100, decimal_places=1) == ""
    assert lrs.range_flag(5150, low_milli=None, high_milli=5100, decimal_places=1) == "H"
    assert lrs.at_precision(5150, 1) == 5200 and lrs.at_precision(-5150, 1) == -5200


def test_a_value_with_no_number_is_never_flagged():
    assert lrs.range_flag(None, low_milli=3500, high_milli=5100, decimal_places=1) == ""


# ── 2. The worklist ──────────────────────────────────────────────────────────

def test_the_worklist_puts_stat_first_then_the_oldest_receipt(lab_on, _catalogue, tech, phleb):
    first = _received(_patient(_catalogue), phleb[1], tech[1], "K")
    second = _received(_patient(_catalogue), phleb[1], tech[1], "NA")
    stat = _received(_patient(_catalogue), phleb[1], tech[1], "CR", priority="stat")
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    with db_session() as db:
        for offset, (_, specimen_id, _) in enumerate((first, second, stat)):
            db.execute("UPDATE lab_specimens SET received_at = ? WHERE id = ?",
                       (to_utc_iso(base + timedelta(minutes=offset)), specimen_id))
        # The later receipt sorts after the earlier one even when received first.
        db.execute("UPDATE lab_specimens SET received_at = ? WHERE id = ?",
                   (to_utc_iso(base - timedelta(minutes=5)), second[1]))
    items = [i["item_id"] for i in _worklist(tech[1])["items"]]
    assert items.index(stat[2]) < items.index(second[2]) < items.index(first[2])


def test_the_worklist_holds_only_received_samples_and_is_audited(lab_on, _catalogue, patient,
                                                                tech, phleb):
    # Another patient, so the planned tube is not the one received below.
    order = orders_service.create_order(
        patient_id=_patient(_catalogue)["id"], order_type="lab", description="planned only",
        code=_code("NA"), code_system="LOCAL", priority="routine", ordered_by=DOCTOR_ID,
        status="active")
    _, _, received_item = _received(patient, phleb[1], tech[1], "K")
    bench = _worklist(tech[1])
    with db_session() as db:
        planned_item = db.execute("SELECT id FROM lab_order_items WHERE order_id = ?",
                                  (order["id"],)).fetchone()[0]
        audit = db.execute(
            "SELECT event_category FROM audit_logs WHERE action_type = 'lab_worklist_viewed' "
            "AND actor_user_id = ? ORDER BY id DESC LIMIT 1", (tech[0],)).fetchone()
    on_bench = [i["item_id"] for i in bench["items"]]
    assert received_item in on_bench and planned_item not in on_bench
    assert audit is not None and audit["event_category"] == "phi_access"


def test_the_section_counts_carry_no_patient_data(lab_on, patient, tech, phleb):
    _received(patient, phleb[1], tech[1], "K", priority="stat")
    resp = client.get("/api/lab/worklists", headers=tech[1])
    assert resp.status_code == 200, resp.text
    sections = {s["id"]: s for s in resp.json()["sections"]}
    assert sections["biochemistry"]["waiting"] >= 1
    assert sections["biochemistry"]["stat_waiting"] >= 1
    assert patient["name"] not in resp.text and patient["id"] not in resp.text


def test_an_unknown_section_is_not_found(lab_on, tech):
    assert client.get("/api/lab/worklists/no-such-bench", headers=tech[1]).status_code == 404


def test_a_cancelled_test_leaves_the_worklist_with_who_and_when(lab_on, patient, tech, phleb):
    order, _, item_id = _received(patient, phleb[1], tech[1], "K")
    entered = _enter(item_id, "4.2", tech[1])
    assert entered.status_code == 200, entered.text
    nurse_id, nurse = _user("nurse", WARD)
    cancel = client.post(f"/api/lab/orders/{order['id']}/cancel",
                         json={"reason": "Patient going to theatre"}, headers=nurse)
    assert cancel.status_code == 200, cancel.text

    bench = _worklist(tech[1])
    assert item_id not in [i["item_id"] for i in bench["items"]]
    gone = next(c for c in bench["cancelled"] if c["item_id"] == item_id)
    assert gone["cancelled_by_name"] == f"Nurse {nurse_id}"
    assert gone["cancelled_at"] and gone["reason"] == "Patient going to theatre"
    with db_session() as db:
        status = db.execute("SELECT status FROM lab_results WHERE id = ?",
                            (entered.json()["result"]["id"],)).fetchone()[0]
    assert status == "withdrawn"
    refused = _enter(item_id, "4.3", tech[1])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "item_cancelled"
    assert refused.json()["detail"]["cancelled_by_name"] == f"Nurse {nurse_id}"


def test_only_result_staff_read_worklists_and_enter_results(lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    for role, dept in (("nurse", WARD), ("doctor", WARD), ("phlebotomist", None),
                       ("admin", None)):
        hdr = _user(role, dept)[1]
        assert client.get("/api/lab/worklists/biochemistry", headers=hdr).status_code == 403, role
        assert _enter(item_id, "4.2", hdr).status_code == 403, role


# ── 3. Entry and flags ───────────────────────────────────────────────────────

def test_too_many_decimals_are_refused_and_fewer_are_printed_at_precision(
        lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "CR")
    refused = _enter(item_id, "1.234", tech[1])
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "precision"
    ok = _enter(item_id, "1.2", tech[1])
    assert ok.status_code == 200, ok.text
    result = ok.json()["result"]
    assert (result["value_text"], result["value_milli"], result["decimal_places"]) == (
        "1.20", 1200, 2)


def test_a_value_equal_to_the_high_limit_is_not_flagged(lab_on, _catalogue, tech, phleb,
                                                        potassium_range):
    cases = {"5.1": ("", "Within range"), "5.2": ("H", "High"), "3.4": ("L", "Low"),
             "3.5": ("", "Within range")}
    for typed, (flag, words) in cases.items():
        _, _, item_id = _received(_patient(_catalogue), phleb[1], tech[1], "K")
        resp = _enter(item_id, typed, tech[1])
        assert resp.status_code == 200, resp.text
        result = resp.json()["result"]
        assert (result["abnormal_flag"], result["flag_text"]) == (flag, words), typed
        assert result["reference_range_id"] == potassium_range["id"]
        assert result["reference_range_text"] == "3.5 to 5.1 mmol/L"
        assert result["is_critical"] == 0 and result["critical_limit_id"] is None


def test_no_range_means_no_flag_and_says_so(lab_on, patient, tech, phleb):
    _clear_ranges("NA")
    _, _, item_id = _received(patient, phleb[1], tech[1], "NA")
    result = _enter(item_id, "180", tech[1]).json()["result"]
    assert result["abnormal_flag"] == ""
    assert result["reference_range_id"] is None
    assert result["reference_range_text"] == "Reference range not configured"
    assert result["flag_text"] == "No flag: reference range not configured"


def test_a_patient_with_no_date_of_birth_gets_no_age_banded_range(lab_on, _catalogue, tech,
                                                                  phleb, pathologist):
    _clear_ranges("NA")
    _approve(pathologist, "NA", low="135", high="145", age_min_days=6570)
    try:
        no_dob = _patient(_catalogue, dob=None)
        _, _, item_id = _received(no_dob, phleb[1], tech[1], "NA")
        result = _enter(item_id, "150", tech[1]).json()["result"]
        assert result["abnormal_flag"] == ""
        assert result["reference_range_text"] == (
            "Reference range not configured: date of birth not recorded")

        adult = _patient(_catalogue, dob="1970-01-01")
        _, _, adult_item = _received(adult, phleb[1], tech[1], "NA")
        assert _enter(adult_item, "150", tech[1]).json()["result"]["abnormal_flag"] == "H"
    finally:
        _clear_ranges("NA")


def test_a_range_covering_every_age_applies_without_a_date_of_birth(
        lab_on, _catalogue, tech, phleb, potassium_range):
    p = _patient(_catalogue, dob=None, sex=None)
    _, _, item_id = _received(p, phleb[1], tech[1], "K")
    assert _enter(item_id, "6.0", tech[1]).json()["result"]["abnormal_flag"] == "H"


def test_a_range_approved_after_collection_flags_the_value(lab_on, patient, tech, phleb,
                                                           pathologist):
    _clear_ranges("K")
    try:
        _, _, item_id = _received(patient, phleb[1], tech[1], "K")
        bench = _worklist(tech[1])
        row = next(i for i in bench["items"] if i["item_id"] == item_id)
        assert row["range"] == {"id": None, "text": "Reference range not configured",
                                "configured": False}
        _approve(pathologist, "K", low="3.5", high="5.1")
        result = _enter(item_id, "5.9", tech[1]).json()["result"]
        assert result["abnormal_flag"] == "H"
    finally:
        _clear_ranges("K")


def test_a_comparator_value_is_kept_as_a_bound_with_no_flag(lab_on, _catalogue, tech, phleb,
                                                            pathologist):
    _clear_ranges("CR")
    _approve(pathologist, "CR", low="0.60", high="1.20")
    try:
        _, _, item_id = _received(_patient(_catalogue), phleb[1], tech[1], "CR")
        resp = _enter(item_id, "<0.01", tech[1])
        assert resp.status_code == 200, resp.text
        result = resp.json()["result"]
        assert (result["comparator"], result["value_milli"], result["value_text"]) == (
            "<", None, "<0.01")
        assert result["abnormal_flag"] == ""
        assert result["flag_text"] == "No flag: the value is a bound"
        _, _, high_item = _received(_patient(_catalogue), phleb[1], tech[1], "CR")
        bound = _enter(high_item, ">20.00", tech[1]).json()["result"]
        assert bound["abnormal_flag"] == "" and bound["value_text"] == ">20.00"
    finally:
        _clear_ranges("CR")


def test_a_coded_value_outside_the_list_is_refused(lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "HBS")
    bench = _worklist(tech[1])
    row = next(i for i in bench["items"] if i["item_id"] == item_id)
    assert row["test"]["coded_values"] == ["Reactive", "Non-reactive"]
    refused = _enter(item_id, "Weakly reactive", tech[1])
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "not_in_list"
    assert refused.json()["detail"]["allowed"] == ["Reactive", "Non-reactive"]
    ok = _enter(item_id, "  non-REACTIVE ", tech[1])
    assert ok.status_code == 200, ok.text
    result = ok.json()["result"]
    assert (result["value_text"], result["value_milli"], result["abnormal_flag"]) == (
        "Non-reactive", None, "")


def test_a_text_result_is_kept_as_typed(lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "NOTE")
    result = _enter(item_id, "Target cells seen", tech[1]).json()["result"]
    assert result["value_text"] == "Target cells seen" and result["flag_text"] == "No flag"


def test_entry_writes_the_result_its_event_and_audit_together(lab_on, patient, tech, phleb):
    order, specimen_id, item_id = _received(patient, phleb[1], tech[1], "K")
    resp = _enter(item_id, "4.4", tech[1], comment="Repeat confirmed")
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert (result["status"], result["version"], result["source"], result["entry_method"],
            result["entered_by"], result["specimen_id"], result["comment"]) == (
        "entered", 1, "lis_manual", "manual", tech[0], specimen_id, "Repeat confirmed")
    with db_session() as db:
        item = db.execute("SELECT status FROM lab_order_items WHERE id = ?",
                          (item_id,)).fetchone()[0]
        events = [dict(r) for r in db.execute(
            "SELECT * FROM lab_result_events WHERE result_id = ?", (result["id"],)).fetchall()]
        audit = db.execute("SELECT * FROM audit_logs WHERE action_type = 'lab_result_entered' "
                           "AND target_id = ?", (str(result["id"]),)).fetchone()
    assert item == "resulted"
    assert [e["event_type"] for e in events] == ["entered"]
    assert json.loads(events[0]["after_json"])["value_text"] == "4.4"
    assert audit is not None and audit["event_category"] == "clinical_access"
    assert audit["patient_id"] == patient["id"]


def test_an_entry_whose_audit_fails_writes_nothing(lab_on, patient, tech, phleb, monkeypatch):
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")

    def failing(**kwargs):
        raise AuditWriteError("injected")

    monkeypatch.setattr(lrs, "log_audit_event", failing)
    resp = _enter(item_id, "4.4", tech[1])
    assert resp.status_code == 503
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM lab_results WHERE order_item_id = ?",
                          (item_id,)).fetchone()[0] == 0
        assert db.execute("SELECT status FROM lab_order_items WHERE id = ?",
                          (item_id,)).fetchone()[0] == "received"


def test_a_sample_not_yet_received_takes_no_result(lab_on, patient, tech, phleb):
    order = orders_service.create_order(
        patient_id=patient["id"], order_type="lab", description="not received",
        code=_code("K"), code_system="LOCAL", priority="routine", ordered_by=DOCTOR_ID,
        status="active")
    with db_session() as db:
        item_id = db.execute("SELECT id FROM lab_order_items WHERE order_id = ?",
                             (order["id"],)).fetchone()[0]
    resp = _enter(item_id, "4.2", tech[1])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "not_received"


# ── 4. One live result, and edits ────────────────────────────────────────────

def test_a_second_entry_is_refused(lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    assert _enter(item_id, "4.2", tech[1]).status_code == 200
    again = _enter(item_id, "4.3", tech[1])
    assert again.status_code == 409 and again.json()["detail"]["error"] == "result_exists"
    with db_session() as db:
        live = db.execute("SELECT COUNT(*) FROM lab_results WHERE order_item_id = ? "
                          "AND status NOT IN ('superseded', 'withdrawn')", (item_id,)).fetchone()[0]
    assert live == 1


def test_the_partial_unique_index_refuses_a_second_live_row(lab_on, patient, tech, phleb):
    order, specimen_id, item_id = _received(patient, phleb[1], tech[1], "K")
    first = _enter(item_id, "4.2", tech[1]).json()["result"]
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute(
                "INSERT INTO lab_results (order_item_id, order_id, specimen_id, patient_id, "
                "test_id, entered_at, status) VALUES (?, ?, ?, ?, ?, ?, 'entered')",
                (item_id, order["id"], specimen_id, patient["id"], first["test_id"],
                 utc_iso_now()))
    # A withdrawn row is not live, so it never blocks the one live result.
    with db_session() as db:
        db.execute(
            "INSERT INTO lab_results (order_item_id, order_id, specimen_id, patient_id, "
            "test_id, entered_at, status) VALUES (?, ?, ?, ?, ?, ?, 'withdrawn')",
            (item_id, order["id"], specimen_id, patient["id"], first["test_id"],
             utc_iso_now()))


def test_an_edit_keeps_the_value_before_and_after_with_audit(lab_on, _catalogue, tech, phleb,
                                                             potassium_range):
    p = _patient(_catalogue)
    _, _, item_id = _received(p, phleb[1], tech[1], "K")
    first = _enter(item_id, "4.9", tech[1]).json()["result"]
    resp = _edit(first, "5.3", tech[1], reason="Typed the wrong tube")
    assert resp.status_code == 200, resp.text
    edited = resp.json()["result"]
    assert (edited["value_text"], edited["abnormal_flag"], edited["version"]) == ("5.3", "H", 1)
    with db_session() as db:
        event = dict(db.execute("SELECT * FROM lab_result_events WHERE result_id = ? "
                                "AND event_type = 'edited'", (first["id"],)).fetchone())
        audit = dict(db.execute("SELECT * FROM audit_logs WHERE action_type = 'lab_result_edited' "
                                "AND target_id = ?", (str(first["id"]),)).fetchone())
    before, after = json.loads(event["before_json"]), json.loads(event["after_json"])
    assert (before["value_text"], before["abnormal_flag"]) == ("4.9", "")
    assert (after["value_text"], after["abnormal_flag"], after["reason"]) == (
        "5.3", "H", "Typed the wrong tube")
    meta = json.loads(audit["metadata_json"])
    assert meta["before"]["value_text"] == "4.9" and meta["after"]["value_text"] == "5.3"
    assert audit["event_category"] == "clinical_access"

    same = _edit(edited, "5.30", tech[1])
    assert same.status_code == 200 and same.json()["unchanged"] is True
    with db_session() as db:
        edits = db.execute("SELECT COUNT(*) FROM lab_result_events WHERE result_id = ? "
                           "AND event_type = 'edited'", (first["id"],)).fetchone()[0]
    assert edits == 1


def test_an_edit_is_held_to_the_same_rules(lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    first = _enter(item_id, "4.9", tech[1]).json()["result"]
    refused = _edit(first, "4.91", tech[1])
    assert refused.status_code == 422 and refused.json()["detail"]["error"] == "precision"


def test_a_validated_result_is_not_edited_here(lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    first = _enter(item_id, "4.9", tech[1]).json()["result"]
    with db_session() as db:
        db.execute("UPDATE lab_results SET status = 'tech_validated' WHERE id = ?",
                   (first["id"],))
        db.execute("UPDATE lab_order_items SET status = 'tech_validated' WHERE id = ?",
                   (item_id,))
    resp = _edit(first, "5.0", tech[1])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "already_validated"


# ── 5. Two people on one bench: a stale screen never overwrites ─────────────

def _edit_count(result_id):
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM lab_result_events WHERE result_id = ? "
                          "AND event_type = 'edited'", (result_id,)).fetchone()[0]


def _stored(result_id):
    with db_session() as db:
        return dict(db.execute("SELECT value_text, comment, abnormal_flag FROM lab_results "
                               "WHERE id = ?", (result_id,)).fetchone())


def test_a_stale_screen_cannot_undo_another_persons_correction(lab_on, patient, tech, phleb):
    """Tech A enters K 4.5. Tech B corrects it to 5.5. A's screen still shows
    4.5: adding only a comment, retyping 4.6, or even typing 5.5 is refused,
    B's correction stands, and A is told what the result now reads and who
    saved it."""
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    a_screen = _enter(item_id, "4.5", tech[1]).json()["result"]
    b_id, b_hdr = _user("lab_technician")
    corrected = _edit(a_screen, "5.5", b_hdr, reason="transcription")
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["result"]["value_text"] == "5.5"

    for typed, extra in (("4.5", {"comment": "lipaemic"}), ("4.6", {}), ("5.5", {})):
        resp = _edit(a_screen, typed, tech[1], **extra)
        assert resp.status_code == 409, (typed, resp.text)
        detail = resp.json()["detail"]
        assert detail["error"] == "result_changed", typed
        assert detail["changed_fields"] == ["value_text"]
        assert detail["changed_by_name"] == f"Lab_Technician {b_id}"
        assert detail["current"]["value_text"] == "5.5"
        assert detail["current"]["id"] == a_screen["id"]
        assert "it now reads 5.5" in detail["message"]
        assert "Nothing was saved" in detail["message"]
    assert _stored(a_screen["id"]) == {"value_text": "5.5", "comment": None,
                                       "abnormal_flag": ""}
    assert _edit_count(a_screen["id"]) == 1

    # Once A's screen is drawn again from the server, A's comment saves.
    current = _edit(a_screen, "4.5", tech[1]).json()["detail"]["current"]
    ok = _edit(current, "5.5", tech[1], comment="lipaemic")
    assert ok.status_code == 200, ok.text
    assert ok.json()["unchanged"] is False
    assert _stored(a_screen["id"]) == {"value_text": "5.5", "comment": "lipaemic",
                                       "abnormal_flag": ""}


def test_the_conflict_body_is_audited_as_a_read(lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    a_screen = _enter(item_id, "4.5", tech[1]).json()["result"]
    assert _edit(a_screen, "5.5", _user("lab_technician")[1]).status_code == 200
    assert _edit(a_screen, "4.6", tech[1]).status_code == 409
    with db_session() as db:
        audit = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'lab_result_conflict_viewed' "
            "AND target_id = ? AND actor_user_id = ?",
            (str(a_screen["id"]), tech[0])).fetchone()
        edits_by_a = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'lab_result_edited' "
            "AND target_id = ? AND actor_user_id = ?",
            (str(a_screen["id"]), tech[0])).fetchone()[0]
    assert audit is not None
    assert audit["event_category"] == "phi_access"
    assert audit["patient_id"] == patient["id"]
    assert json.loads(audit["metadata_json"])["changed_fields"] == ["value_text"]
    assert edits_by_a == 0


def test_a_conflict_whose_audit_fails_discloses_nothing(lab_on, patient, tech, phleb,
                                                        monkeypatch):
    from routes.lab import results as results_routes

    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    a_screen = _enter(item_id, "4.5", tech[1]).json()["result"]
    assert _edit(a_screen, "5.5", _user("lab_technician")[1]).status_code == 200

    def failing(**kwargs):
        raise AuditWriteError("injected")

    monkeypatch.setattr(results_routes, "log_phi_read", failing)
    resp = _edit(a_screen, "4.6", tech[1])
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "audit_unavailable"
    assert "5.5" not in resp.text
    assert _stored(a_screen["id"])["value_text"] == "5.5"


def test_a_changed_comment_or_flag_is_a_changed_result(lab_on, _catalogue, tech, phleb,
                                                       potassium_range):
    _, _, item_id = _received(_patient(_catalogue), phleb[1], tech[1], "K")
    a_screen = _enter(item_id, "4.5", tech[1]).json()["result"]
    b_hdr = _user("lab_technician")[1]
    commented = _edit(a_screen, "4.5", b_hdr, comment="Haemolysed, repeat sent")
    assert commented.status_code == 200, commented.text
    resp = _edit(a_screen, "4.7", tech[1])
    assert resp.status_code == 409
    assert resp.json()["detail"]["changed_fields"] == ["comment"]

    # A screen that showed the result as High while it is within range is
    # out of date too, though the value and comment match.
    shown_high = {**commented.json()["result"], "abnormal_flag": "H"}
    resp = _edit(commented.json()["result"], "4.7", tech[1], seen=shown_high)
    assert resp.status_code == 409
    assert resp.json()["detail"]["changed_fields"] == ["abnormal_flag"]
    assert _stored(a_screen["id"])["value_text"] == "4.5"


def test_a_blank_comment_and_no_comment_are_the_same_on_screen(lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    first = _enter(item_id, "4.5", tech[1]).json()["result"]
    assert first["comment"] is None
    seen = {**first, "comment": "  "}
    resp = _edit(first, "4.6", tech[1], seen=seen)
    assert resp.status_code == 200, resp.text


def test_an_edit_that_cannot_say_what_it_saw_is_refused(lab_on, patient, tech, phleb):
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    first = _enter(item_id, "4.5", tech[1]).json()["result"]
    no_expected = client.patch(f"/api/lab/results/{first['id']}", json={"value": "4.6"},
                               headers=tech[1])
    assert no_expected.status_code == 422
    partial = client.patch(f"/api/lab/results/{first['id']}",
                           json={"value": "4.6", "expected": {"value_text": "4.5"}},
                           headers=tech[1])
    assert partial.status_code == 422
    # The service holds the same line for any caller that skips the route.
    user = {"id": tech[0], "role": "lab_technician"}
    for expected in (None, {}, {"value_text": "4.5", "comment": None}):
        with pytest.raises(lrs.LabResultError) as refused:
            lrs.edit_result(first["id"], value="4.6", expected=expected, actor=user)
        assert refused.value.code == "result_changed" and refused.value.status == 409
    assert _stored(first["id"])["value_text"] == "4.5"
    assert _edit_count(first["id"]) == 0


def test_entry_is_served_while_the_module_drains(lab_on, patient, tech, phleb, monkeypatch):
    _, _, item_id = _received(patient, phleb[1], tech[1], "K")
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    assert lab_module.module_state() == "draining"
    assert client.get("/api/lab/worklists/biochemistry", headers=tech[1]).status_code == 200
    assert _enter(item_id, "4.2", tech[1]).status_code == 200


def test_results_are_404_with_the_module_off(tech, monkeypatch):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    assert lab_module.module_state() == "off"
    resp = client.get("/api/lab/worklists", headers=tech[1])
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "module_disabled"
