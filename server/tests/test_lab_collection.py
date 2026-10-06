"""Specimen labels and collection (lis-8).

Pinned here:
  * The collection list: a ward account sees its own department only; the
    phlebotomist (no department) sees every ward; STAT first; each read is
    audited as phi_access with the patients returned.
  * Labels (ruling LIS-R6, strictest option): one patient at a time, only
    after the typed UHID and name both match the record; a refusal prints
    nothing and is audited; each print is counted on the specimen (which
    closes it to later merges) and audited as phi_access in the same
    transaction.
  * Collect: refused without the identity payload, with a wrong identifier,
    before the label is printed, or outside the caller's department; with
    back-dating refused by default and never beyond the site limit or the
    two-hour ceiling; and specimen, items, order, order_events and audit in
    one transaction, so a failed audit row leaves nothing collected.
  * New work stops while the module drains; the list stays readable.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import lab_module, lab_order_service as los
from services import lab_master_service as lm
from services import orders_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import utc_iso_now

client = TestClient(main.app)

DOCTOR_ID = "D001"
WARD = "General Medicine"
OTHER_WARD = "Cardiology"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _catalogue():
    terminology_service.seed_terminology()
    lm.load_starter_catalogue()
    created: list[str] = []
    yield created
    if created:
        marks = ", ".join("?" for _ in created)
        ids = tuple(created)
        with db_session() as db:
            db.execute(f"DELETE FROM lab_exceptions WHERE order_id IN "
                       f"(SELECT id FROM orders WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM lab_order_items WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimen_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimens WHERE patient_id IN ({marks})", ids)
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
            db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                       ids)
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


def _patient(created, dept=WARD, name=None):
    pid = f"PLOC{uuid.uuid4().hex[:8].upper()}"
    name = name or f"Collect {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-LOC-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'admitted', ?, ?)",
                   (pid, name, mrn, f"{dept[:3].upper()}-1", f"B{uuid.uuid4().hex[:3]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"ALOC{uuid.uuid4().hex[:8].upper()}", pid, dept))
    created.append(pid)
    return {"id": pid, "name": name, "mrn": mrn}


@pytest.fixture
def patient(_catalogue):
    return _patient(_catalogue)


def _user(role, dept=WARD):
    uid = f"LOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role.title()} {uid}", role, dept, hash_password("password")))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


def _hdr(role, dept=WARD):
    return _user(role, dept)[1]


def _order(patient_id, code="2823-3", system="LOINC", *, priority="routine"):
    order = orders_service.create_order(
        patient_id=patient_id, order_type="lab", description=f"{system} {code}",
        code=code, code_system=system, priority=priority, ordered_by=DOCTOR_ID,
        status="active")
    return order


def _specimen_of(order_id):
    with db_session() as db:
        row = db.execute("SELECT specimen_id FROM lab_order_items WHERE order_id = ? "
                         "ORDER BY id LIMIT 1", (order_id,)).fetchone()
    assert row is not None, "the laboratory should have taken the order in"
    return row[0]


def _specimen(specimen_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM lab_specimens WHERE id = ?",
                               (specimen_id,)).fetchone())


def _items(order_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()]


def _order_row(order_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def _events(specimen_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_specimen_events WHERE specimen_id = ? ORDER BY seq",
            (specimen_id,)).fetchall()]


def _audits(action, *, target_like=None, patient_id=None):
    sql = "SELECT * FROM audit_logs WHERE action_type = ?"
    args: list = [action]
    if target_like:
        sql += " AND target_id LIKE ?"
        args.append(f"%{target_like}%")
    if patient_id:
        sql += " AND patient_id = ?"
        args.append(patient_id)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def _ok_identity(p):
    return {"uhid": p["mrn"], "name": p["name"]}


def _print(specimen_ids, identity, hdr):
    return client.post("/api/lab/collection/labels",
                       json={"specimen_ids": specimen_ids, "identity": identity}, headers=hdr)


def _collect(specimen_id, body, hdr):
    return client.post(f"/api/lab/specimens/{specimen_id}/collect", json=body, headers=hdr)


def _printed(p, hdr, code="2823-3"):
    order = _order(p["id"], code)
    specimen_id = _specimen_of(order["id"])
    resp = _print([specimen_id], _ok_identity(p), hdr)
    assert resp.status_code == 200, resp.text
    return order, specimen_id


# ── 1. The collection list ───────────────────────────────────────────────────

def test_a_ward_nurse_sees_her_department_and_the_phlebotomist_every_ward(lab_on, _catalogue):
    mine = _patient(_catalogue, WARD)
    theirs = _patient(_catalogue, OTHER_WARD)
    routine = _specimen_of(_order(mine["id"])["id"])
    other = _specimen_of(_order(theirs["id"])["id"])
    stat = _specimen_of(_order(mine["id"], "2160-0", priority="stat")["id"])

    nurse_id, nurse = _user("nurse", WARD)
    resp = client.get("/api/lab/collection", headers=nurse)
    assert resp.status_code == 200, resp.text
    ids = [s["id"] for s in resp.json()["specimens"]]
    assert routine in ids and stat in ids and other not in ids
    assert resp.json()["scope"] == {"department": WARD, "hospital_wide": False}
    assert resp.json()["backdate_minutes"] == 0

    phleb = client.get("/api/lab/collection", headers=_hdr("phlebotomist", None))
    assert phleb.status_code == 200, phleb.text
    rows = phleb.json()["specimens"]
    listed = [s["id"] for s in rows]
    assert {routine, other, stat} <= set(listed)
    # STAT first, ahead of every routine sample.
    assert listed.index(stat) < listed.index(routine)
    entry = next(s for s in rows if s["id"] == routine)
    assert entry["patient"]["name"] == mine["name"] and entry["patient"]["mrn"] == mine["mrn"]
    assert entry["patient"]["department"] == WARD
    assert entry["container"] and entry["tests"][0]["short_name"] == "Potassium"
    assert entry["label_print_count"] == 0 and entry["recollection_needed"] is False

    audit = [a for a in _audits("lab_collection_list_viewed") if a["actor_user_id"] == nurse_id]
    assert len(audit) == 1 and audit[0]["event_category"] == "phi_access"


def test_the_list_needs_lab_collect(lab_on):
    assert client.get("/api/lab/collection", headers=_hdr("admin", None)).status_code == 403


def test_the_list_stays_readable_while_the_module_drains(monkeypatch):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    assert lab_module.module_state() == "draining"
    resp = client.get("/api/lab/collection", headers=_hdr("phlebotomist", None))
    assert resp.status_code == 200, resp.text


def test_a_rejected_sample_shows_as_recollection_needed(lab_on, patient):
    order = _order(patient["id"])
    specimen_id = _specimen_of(order["id"])
    with db_session() as db:
        db.execute("UPDATE lab_specimens SET status = 'rejected', rejection_code = 'haemolysed' "
                   "WHERE id = ?", (specimen_id,))
        db.execute("UPDATE lab_order_items SET status = 'rejected' WHERE order_id = ?",
                   (order["id"],))
    rows = client.get("/api/lab/collection", headers=_hdr("nurse")).json()["specimens"]
    entry = next(s for s in rows if s["id"] == specimen_id)
    assert entry["recollection_needed"] is True and entry["rejection_code"] == "haemolysed"


# ── 2. Labels ────────────────────────────────────────────────────────────────

def test_labels_need_the_two_identifiers(lab_on, patient):
    specimen_id = _specimen_of(_order(patient["id"])["id"])
    hdr = _hdr("nurse")
    missing = _print([specimen_id], None, hdr)
    assert missing.status_code == 422
    assert missing.json()["detail"]["error"] == "identity_required"

    wrong = _print([specimen_id], {"uhid": "MRN-SOMEONE-ELSE", "name": patient["name"]}, hdr)
    assert wrong.status_code == 409
    detail = wrong.json()["detail"]
    assert detail["error"] == "identity_not_confirmed"
    assert detail["checks"] == {"uhid": "no_match", "name": "match"}
    assert patient["mrn"] not in wrong.text          # the record's UHID is never echoed
    name_only = _print([specimen_id], {"name": patient["name"]}, hdr)
    assert name_only.json()["detail"]["checks"]["uhid"] == "not_given"

    assert _specimen(specimen_id)["label_print_count"] == 0
    refused = _audits("lab_collection_identity_refused", target_like=specimen_id)
    assert len(refused) == 2 and {r["outcome"] for r in refused} == {"failure"}
    assert {r["event_category"] for r in refused} == {"clinical_access"}


def test_a_matching_identity_prints_and_counts_the_label(lab_on, patient):
    order = _order(patient["id"], "58410-2")                    # CBC: one EDTA tube
    specimen_id = _specimen_of(order["id"])
    # Spaces, case and word order do not matter; the characters do.
    identity = {"uhid": f"  {patient['mrn'].lower()} ",
                "name": " ".join(reversed(patient["name"].upper().split()))}
    resp = _print([specimen_id], identity, _hdr("nurse"))
    assert resp.status_code == 200, resp.text
    label = resp.json()["labels"][0]
    assert label["specimen_id"] == specimen_id
    assert label["patient"]["name"] == patient["name"]
    assert label["patient"]["mrn"] == patient["mrn"]
    assert label["container"] and label["print_number"] == 1
    assert set(label["tests"]) == {"Haemoglobin", "Haematocrit", "RBC", "WBC", "Platelets"}
    specimen = _specimen(specimen_id)
    assert specimen["label_print_count"] == 1 and specimen["label_printed_at"]
    assert [e["event_type"] for e in _events(specimen_id)][-1] == "label_printed"
    audit = _audits("lab_labels_printed", target_like=specimen_id)
    assert len(audit) == 1 and audit[0]["event_category"] == "phi_access"
    assert audit[0]["patient_id"] == patient["id"]

    again = _print([specimen_id], _ok_identity(patient), _hdr("nurse"))
    assert again.json()["labels"][0]["print_number"] == 2
    assert _specimen(specimen_id)["label_printed_at"] == specimen["label_printed_at"]


def test_a_printed_tube_is_closed_to_later_orders(lab_on, patient):
    _order_one, specimen_id = _printed(patient, _hdr("nurse"))
    later = _order(patient["id"], "2951-2")                     # sodium, same serum tube type
    assert _specimen_of(later["id"]) != specimen_id


def test_labels_print_one_patient_at_a_time(lab_on, _catalogue):
    a = _patient(_catalogue)
    b = _patient(_catalogue)
    sa = _specimen_of(_order(a["id"])["id"])
    sb = _specimen_of(_order(b["id"])["id"])
    resp = _print([sa, sb], _ok_identity(a), _hdr("phlebotomist", None))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "one_patient_at_a_time"
    assert _specimen(sa)["label_print_count"] == 0


def test_labels_for_another_departments_patient_are_not_found(lab_on, patient):
    specimen_id = _specimen_of(_order(patient["id"])["id"])
    resp = _print([specimen_id], _ok_identity(patient), _hdr("nurse", OTHER_WARD))
    assert resp.status_code == 404
    unknown = _print(["999999999999"], _ok_identity(patient), _hdr("nurse", OTHER_WARD))
    assert resp.json()["detail"] == unknown.json()["detail"]


def test_a_label_print_whose_audit_fails_counts_nothing(lab_on, patient, monkeypatch):
    specimen_id = _specimen_of(_order(patient["id"])["id"])
    real = los.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "lab_labels_printed":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(los, "log_audit_event", failing)
    resp = _print([specimen_id], _ok_identity(patient), _hdr("nurse"))
    assert resp.status_code == 503
    assert _specimen(specimen_id)["label_print_count"] == 0


# ── 3. Collect ───────────────────────────────────────────────────────────────

def test_collect_without_the_identity_payload_is_refused(lab_on, patient):
    hdr = _hdr("nurse")
    _order_one, specimen_id = _printed(patient, hdr)
    resp = _collect(specimen_id, {}, hdr)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "identity_required"
    blank = _collect(specimen_id, {"identity": {"uhid": " ", "name": ""}}, hdr)
    assert blank.status_code == 422
    assert _specimen(specimen_id)["status"] == "planned"


def test_collect_with_a_wrong_identifier_is_refused_and_audited(lab_on, patient):
    hdr = _hdr("nurse")
    order, specimen_id = _printed(patient, hdr)
    resp = _collect(specimen_id, {"identity": {"uhid": patient["mrn"], "name": "Someone Else"}},
                    hdr)
    assert resp.status_code == 409
    assert resp.json()["detail"]["checks"] == {"uhid": "match", "name": "no_match"}
    assert _specimen(specimen_id)["status"] == "planned"
    assert _order_row(order["id"])["status"] == "active"
    assert _audits("lab_collection_identity_refused", target_like=specimen_id)


def test_collect_needs_the_label_printed_first(lab_on, patient):
    specimen_id = _specimen_of(_order(patient["id"])["id"])
    resp = _collect(specimen_id, {"identity": _ok_identity(patient)}, _hdr("nurse"))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "labels_not_printed"


def test_collect_moves_the_specimen_its_tests_and_the_order_together(lab_on, patient):
    uid, hdr = _user("nurse")
    order, specimen_id = _printed(patient, hdr)
    resp = _collect(specimen_id, {"identity": _ok_identity(patient),
                                  "collection_location": "Bed side"}, hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items_collected"] == 1
    assert body["orders"] == [{"id": order["id"], "status": "in_progress", "conflict": False}]
    assert "identity_check_json" not in body["specimen"]
    specimen = _specimen(specimen_id)
    assert specimen["status"] == "collected" and specimen["collected_by"] == uid
    assert specimen["collection_location"] == "Bed side"
    assert specimen["collection_backdate_reason"] is None
    assert '"uhid": "match"' in specimen["identity_check_json"]
    assert {i["status"] for i in _items(order["id"])} == {"collected"}
    assert _order_row(order["id"])["status"] == "in_progress"
    last = orders_service.get_order_events(order["id"])[-1]
    assert (last["prior_status"], last["new_status"], last["actor_id"]) == (
        "active", "in_progress", uid)
    assert [e["event_type"] for e in _events(specimen_id)][-1] == "collected"
    audit = _audits("lab_specimen_collected", target_like=specimen_id)
    assert len(audit) == 1 and audit[0]["event_category"] == "clinical_access"
    # A second tap finds it already collected.
    again = _collect(specimen_id, {"identity": _ok_identity(patient)}, hdr)
    assert again.status_code == 409
    assert again.json()["detail"]["specimen_status"] == "collected"


def test_a_phlebotomist_collects_on_any_ward_and_a_nurse_only_on_hers(lab_on, patient):
    _order_one, specimen_id = _printed(patient, _hdr("phlebotomist", None))
    elsewhere = _collect(specimen_id, {"identity": _ok_identity(patient)},
                         _hdr("nurse", OTHER_WARD))
    assert elsewhere.status_code == 404
    resp = _collect(specimen_id, {"identity": _ok_identity(patient)},
                    _hdr("phlebotomist", None))
    assert resp.status_code == 200, resp.text


def test_an_injected_audit_failure_rolls_back_specimen_and_order_together(lab_on, patient,
                                                                         monkeypatch):
    hdr = _hdr("nurse")
    order, specimen_id = _printed(patient, hdr)
    events_before = len(orders_service.get_order_events(order["id"]))
    real = los.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "lab_specimen_collected":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(los, "log_audit_event", failing)
    resp = _collect(specimen_id, {"identity": _ok_identity(patient)}, hdr)
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "audit_unavailable"
    assert _specimen(specimen_id)["status"] == "planned"
    assert {i["status"] for i in _items(order["id"])} == {"awaiting_collection"}
    assert _order_row(order["id"])["status"] == "active"
    assert len(orders_service.get_order_events(order["id"])) == events_before
    assert [e["event_type"] for e in _events(specimen_id)][-1] == "label_printed"


# ── 4. Back-dating (ruling LIS-R6: refused until confirmed) ──────────────────

def _minutes_ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def test_the_default_refuses_any_back_dating(lab_on, patient):
    assert config.LAB_COLLECTION_BACKDATE_MINUTES == 0
    hdr = _hdr("nurse")
    _order_one, specimen_id = _printed(patient, hdr)
    resp = _collect(specimen_id, {"identity": _ok_identity(patient),
                                  "collected_at": _minutes_ago(5),
                                  "backdate_reason": "Drawn before the round"}, hdr)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "backdating_refused"
    assert resp.json()["detail"]["limit_minutes"] == 0
    assert _specimen(specimen_id)["status"] == "planned"


def test_back_dating_within_a_site_limit_needs_a_reason(lab_on, patient, monkeypatch):
    monkeypatch.setattr(config, "LAB_COLLECTION_BACKDATE_MINUTES", 60)
    hdr = _hdr("nurse")
    order, specimen_id = _printed(patient, hdr)
    # The order was placed moments ago, so a time 30 minutes back is before it.
    with db_session() as db:
        db.execute("UPDATE order_events SET created_at = ? WHERE order_id = ? "
                   "AND new_status = 'active'", (_minutes_ago(90), order["id"]))
    no_reason = _collect(specimen_id, {"identity": _ok_identity(patient),
                                       "collected_at": _minutes_ago(30)}, hdr)
    assert no_reason.status_code == 422
    assert no_reason.json()["detail"]["error"] == "backdate_reason_required"
    too_far = _collect(specimen_id, {"identity": _ok_identity(patient),
                                     "collected_at": _minutes_ago(75),
                                     "backdate_reason": "late entry"}, hdr)
    assert too_far.status_code == 422
    assert too_far.json()["detail"]["error"] == "backdating_refused"
    no_zone = _collect(specimen_id, {"identity": _ok_identity(patient),
                                     "collected_at": "2026-10-04T08:00:00",
                                     "backdate_reason": "late entry"}, hdr)
    assert no_zone.json()["detail"]["error"] == "bad_collected_at"
    future = _collect(specimen_id, {"identity": _ok_identity(patient),
                                    "collected_at": _minutes_ago(-10),
                                    "backdate_reason": "x"}, hdr)
    assert future.json()["detail"]["error"] == "collected_in_future"
    ok = _collect(specimen_id, {"identity": _ok_identity(patient),
                                "collected_at": _minutes_ago(30),
                                "backdate_reason": "Drawn before the network came back"}, hdr)
    assert ok.status_code == 200, ok.text
    specimen = _specimen(specimen_id)
    assert specimen["collection_backdate_reason"] == "Drawn before the network came back"
    assert specimen["collected_at"] < utc_iso_now()


def test_a_collection_time_before_the_order_is_refused(lab_on, patient, monkeypatch):
    monkeypatch.setattr(config, "LAB_COLLECTION_BACKDATE_MINUTES", 60)
    hdr = _hdr("nurse")
    _order_one, specimen_id = _printed(patient, hdr)
    resp = _collect(specimen_id, {"identity": _ok_identity(patient),
                                  "collected_at": _minutes_ago(20),
                                  "backdate_reason": "late"}, hdr)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "before_order"


def test_no_site_setting_goes_past_the_two_hour_ceiling(lab_on, monkeypatch):
    monkeypatch.setattr(config, "LAB_COLLECTION_BACKDATE_MINUTES", 600)
    assert los.backdate_limit_minutes() == los.BACKDATE_CEILING_MINUTES == 120
    monkeypatch.setattr(config, "LAB_COLLECTION_BACKDATE_MINUTES", 0)
    assert los.backdate_limit_minutes() == 0


# ── 5. Draining and the identity rule ────────────────────────────────────────

def test_labels_and_collection_stop_while_the_module_drains(lab_on, patient, monkeypatch):
    hdr = _hdr("nurse")
    _order_one, specimen_id = _printed(patient, hdr)
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    assert lab_module.module_state() == "draining"
    printed = _print([specimen_id], _ok_identity(patient), hdr)
    collected = _collect(specimen_id, {"identity": _ok_identity(patient)}, hdr)
    for resp in (printed, collected):
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "module_disabled"
    assert _specimen(specimen_id)["status"] == "planned"


@pytest.mark.parametrize("typed,expected", [
    ({"uhid": "mrn 001", "name": "asha  devi"}, {"uhid": "match", "name": "match"}),
    ({"uhid": "MRN001", "name": "Devi, Asha"}, {"uhid": "match", "name": "match"}),
    ({"uhid": "MRN002", "name": "Asha Devi"}, {"uhid": "no_match", "name": "match"}),
    ({"uhid": "MRN001", "name": "Asha"}, {"uhid": "match", "name": "no_match"}),
    ({"uhid": "", "name": None}, {"uhid": "not_given", "name": "not_given"}),
])
def test_the_identity_rule(typed, expected):
    record = {"name": "Asha Devi", "mrn": "MRN001"}
    checks = los.collection_identity_checks(record, typed)
    assert checks == expected
    assert los.identity_confirmed(checks) is (expected == {"uhid": "match", "name": "match"})


def test_a_record_with_no_uhid_can_never_be_confirmed():
    checks = los.collection_identity_checks({"name": "Asha Devi", "mrn": None},
                                            {"uhid": "MRN001", "name": "Asha Devi"})
    assert checks["uhid"] == "not_on_record" and not los.identity_confirmed(checks)
