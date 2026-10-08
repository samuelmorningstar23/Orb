"""Specimen receipt, rejection and recollection (lis-9).

Pinned here:
  * Every accession number is checked by its check digit before anything is
    looked up: a misread digit is 422 bad_accession on scan and on receive.
  * Receive: only a collected sample (a planned one is 409 not_collected),
    never twice; one transaction with its audit row; a sample past its
    stability time is refused unless the technologist says why, and the
    screen is told to suggest 'delayed'.
  * Reject: a reason from the list is required ('other' also needs a note);
    the tests become 'rejected', unreleased results are withdrawn, the order
    stays in progress with a 'rejected_no_decision' entry; a reported result
    refuses the rejection; the case-room notice goes after the commit and a
    failed notice never undoes the rejection. A discharged patient's case
    room is hidden in Relay, so it is never reported as the ward told.
  * The decision: recollect plans a NEW tube linked by recollect_of with the
    tests moved onto it (and is new work, so it stops while draining); cancel
    cancels only the rejected tests. A rejected sample stays on the
    laboratory's list and the ward's Samples view until one of the two.
  * The NABH incident prompt appears only for an incident_prompt reason and
    only with ORB_NABH on.
  * Time in transit runs from the collection event, not collected_at.
  * Lists and scans are audited as phi_access; nurses and phlebotomists
    cannot receive.
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
from services import lab_accession, lab_module, lab_order_service as los
from services import lab_master_service as lm
from services import lab_result_service as lrs
from services import orders_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import to_utc_iso, utc_iso_now

client = TestClient(main.app)

DOCTOR_ID = "D001"
WARD = "General Medicine"
POTASSIUM = "2823-3"
CREATININE = "2160-0"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _catalogue():
    """The starter catalogue, and at the end every laboratory row and case-room
    message this file wrote removed, so no open work makes the module
    'draining' for later files."""
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
            db.execute(f"DELETE FROM lab_result_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_results WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_order_items WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimen_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimens WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM messages WHERE channel_id IN "
                       f"(SELECT id FROM channels WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM channels WHERE patient_id IN ({marks})", ids)
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


def _patient(created, dept=WARD, *, case_room=False):
    pid = f"PREC{uuid.uuid4().hex[:8].upper()}"
    name = f"Receipt {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-REC-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'admitted', ?, ?)",
                   (pid, name, mrn, "GEN-1", f"B{uuid.uuid4().hex[:3]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"AREC{uuid.uuid4().hex[:8].upper()}", pid, dept))
        if case_room:
            db.execute("INSERT INTO channels (id, name, type, patient_id) "
                       "VALUES (?, ?, 'case_room', ?)",
                       (f"case-{pid.lower()}", f"Case room {name}", pid))
    created.append(pid)
    return {"id": pid, "name": name, "mrn": mrn}


@pytest.fixture
def patient(_catalogue):
    return _patient(_catalogue)


def _user(role, dept=None):
    uid = f"REC{uuid.uuid4().hex[:6].upper()}"
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


def _order(patient_id, code=POTASSIUM, *, priority="routine"):
    return orders_service.create_order(
        patient_id=patient_id, order_type="lab", description=f"LOINC {code}",
        code=code, code_system="LOINC", priority=priority, ordered_by=DOCTOR_ID,
        status="active")


def _specimen_of(order_id):
    with db_session() as db:
        row = db.execute("SELECT specimen_id FROM lab_order_items WHERE order_id = ? "
                         "ORDER BY id LIMIT 1", (order_id,)).fetchone()
    assert row is not None, "the laboratory should have taken the order in"
    return row[0]


def _identity(p):
    return {"uhid": p["mrn"], "name": p["name"]}


def _collected(p, phleb_hdr, code=POTASSIUM, *, priority="routine"):
    """An order taken in, its label printed and the sample collected."""
    order = _order(p["id"], code, priority=priority)
    specimen_id = _specimen_of(order["id"])
    resp = client.post("/api/lab/collection/labels",
                       json={"specimen_ids": [specimen_id], "identity": _identity(p)},
                       headers=phleb_hdr)
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/lab/specimens/{specimen_id}/collect",
                       json={"identity": _identity(p)}, headers=phleb_hdr)
    assert resp.status_code == 200, resp.text
    return order, specimen_id


def _received(p, phleb_hdr, tech_hdr, code=POTASSIUM, **kw):
    order, specimen_id = _collected(p, phleb_hdr, code, **kw)
    resp = client.post(f"/api/lab/specimens/{specimen_id}/receive", json={}, headers=tech_hdr)
    assert resp.status_code == 200, resp.text
    return order, specimen_id


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


def _open_exceptions(order_id, kind):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_exceptions WHERE order_id = ? AND kind = ? AND resolved_at IS NULL",
            (order_id, kind)).fetchall()]


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ?",
            (action, target_id)).fetchall()]


def _bad_check_digit(accession: str) -> str:
    last = int(accession[-1])
    return accession[:-1] + str((last + 1) % 10)


def _reject(specimen_id, hdr, reason_code="haemolysed", note=None):
    body = {"reason_code": reason_code}
    if note is not None:
        body["note"] = note
    return client.post(f"/api/lab/specimens/{specimen_id}/reject", json=body, headers=hdr)


# ── 1. The check digit ───────────────────────────────────────────────────────

def test_a_bad_check_digit_is_refused_before_anything_is_looked_up(lab_on, patient, tech, phleb):
    _, specimen_id = _collected(patient, phleb[1])
    wrong = _bad_check_digit(specimen_id)
    assert not lab_accession.is_valid_accession(wrong)

    scan = client.get(f"/api/lab/specimens/scan/{wrong}", headers=tech[1])
    assert scan.status_code == 422
    assert scan.json()["detail"]["error"] == "bad_accession"
    receive = client.post(f"/api/lab/specimens/{wrong}/receive", json={}, headers=tech[1])
    assert receive.status_code == 422
    assert receive.json()["detail"]["error"] == "bad_accession"
    assert _specimen(specimen_id)["status"] == "collected"


def test_a_typed_number_with_spaces_scans(lab_on, patient, tech, phleb):
    _, specimen_id = _collected(patient, phleb[1])
    spaced = f"{specimen_id[:6]} {specimen_id[6:11]} {specimen_id[11]}"
    resp = client.get(f"/api/lab/specimens/scan/{spaced}", headers=tech[1])
    assert resp.status_code == 200, resp.text
    detail = resp.json()["specimen"]
    assert detail["id"] == specimen_id
    assert detail["actions"]["receive"] is True
    assert detail["patient"]["name"] == patient["name"]
    assert _audits("lab_specimen_viewed", specimen_id)


def test_an_unknown_but_well_formed_number_is_not_found(lab_on, tech):
    unknown = lab_accession.format_accession(datetime(2020, 1, 1).date(), 99998)
    resp = client.get(f"/api/lab/specimens/scan/{unknown}", headers=tech[1])
    assert resp.status_code == 404


# ── 2. Receive ───────────────────────────────────────────────────────────────

def test_an_uncollected_sample_cannot_be_received(lab_on, patient, tech):
    order = _order(patient["id"])
    specimen_id = _specimen_of(order["id"])
    resp = client.post(f"/api/lab/specimens/{specimen_id}/receive", json={}, headers=tech[1])
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_collected"
    assert _specimen(specimen_id)["status"] == "planned"


def test_receive_moves_the_sample_and_its_tests_and_is_audited(lab_on, patient, tech, phleb):
    order, specimen_id = _collected(patient, phleb[1])
    resp = client.post(f"/api/lab/specimens/{specimen_id}/receive", json={}, headers=tech[1])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items_received"] == 1
    assert body["specimen"]["status"] == "received"
    specimen = _specimen(specimen_id)
    assert specimen["status"] == "received" and specimen["received_by"] == tech[0]
    assert [i["status"] for i in _items(order["id"])] == ["received"]
    assert _events(specimen_id)[-1]["event_type"] == "received"
    assert _audits("lab_specimen_received", specimen_id)

    again = client.post(f"/api/lab/specimens/{specimen_id}/receive", json={}, headers=tech[1])
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "already_received"


def test_only_receiving_staff_receive(lab_on, patient, phleb):
    _, specimen_id = _collected(patient, phleb[1])
    for role in ("nurse", "phlebotomist", "doctor"):
        hdr = _user(role, WARD if role in ("nurse", "doctor") else None)[1]
        resp = client.post(f"/api/lab/specimens/{specimen_id}/receive", json={}, headers=hdr)
        assert resp.status_code == 403, role
    assert _specimen(specimen_id)["status"] == "collected"


def test_a_receipt_whose_audit_fails_changes_nothing(lab_on, patient, tech, phleb, monkeypatch):
    _, specimen_id = _collected(patient, phleb[1])

    def failing(**kwargs):
        if kwargs.get("action_type") == "lab_specimen_received":
            raise AuditWriteError("injected")
        return original(**kwargs)

    original = los.log_audit_event
    monkeypatch.setattr(los, "log_audit_event", failing)
    resp = client.post(f"/api/lab/specimens/{specimen_id}/receive", json={}, headers=tech[1])
    assert resp.status_code == 503
    assert _specimen(specimen_id)["status"] == "collected"


def test_a_sample_past_its_stability_needs_a_reason_and_delayed_is_suggested(
        lab_on, patient, tech, phleb):
    _, specimen_id = _collected(patient, phleb[1])
    three_hours_ago = to_utc_iso(datetime.now(timezone.utc) - timedelta(hours=3))
    with db_session() as db:
        db.execute("UPDATE lab_specimens SET collected_at = ? WHERE id = ?",
                   (three_hours_ago, specimen_id))
        db.execute("UPDATE lab_specimen_types SET stability_minutes = 60 WHERE code = 'serum'")
    try:
        scan = client.get(f"/api/lab/specimens/scan/{specimen_id}", headers=tech[1]).json()
        stability = scan["specimen"]["stability"]
        assert stability["exceeded"] is True
        assert stability["suggested_rejection"] == "delayed"

        refused = client.post(f"/api/lab/specimens/{specimen_id}/receive", json={},
                              headers=tech[1])
        assert refused.status_code == 409
        assert refused.json()["detail"]["error"] == "past_stability"
        no_reason = client.post(f"/api/lab/specimens/{specimen_id}/receive",
                                json={"accept_past_stability": True}, headers=tech[1])
        assert no_reason.status_code == 409
        assert _specimen(specimen_id)["status"] == "collected"

        ok = client.post(f"/api/lab/specimens/{specimen_id}/receive",
                         json={"accept_past_stability": True,
                               "receipt_note": "Pathologist asked to run it"},
                         headers=tech[1])
        assert ok.status_code == 200, ok.text
        assert _specimen(specimen_id)["receipt_note"] == "Pathologist asked to run it"
    finally:
        with db_session() as db:
            db.execute("UPDATE lab_specimen_types SET stability_minutes = NULL "
                       "WHERE code = 'serum'")


def test_time_in_transit_runs_from_the_collection_event(lab_on, patient, tech, phleb):
    _, specimen_id = _collected(patient, phleb[1])
    # The sample was drawn three hours ago, but the collection was recorded now.
    with db_session() as db:
        db.execute("UPDATE lab_specimens SET collected_at = ? WHERE id = ?",
                   (to_utc_iso(datetime.now(timezone.utc) - timedelta(hours=3)), specimen_id))
    lists = client.get("/api/lab/receiving", headers=tech[1])
    assert lists.status_code == 200, lists.text
    entry = next(s for s in lists.json()["in_transit"] if s["id"] == specimen_id)
    assert entry["transit_minutes"] is not None and entry["transit_minutes"] < 5
    assert entry["stability"]["age_minutes"] >= 179
    with db_session() as db:
        audit = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'lab_receiving_list_viewed' "
            "AND actor_user_id = ? ORDER BY id DESC LIMIT 1", (tech[0],)).fetchone()
    assert audit is not None and audit["event_category"] == "phi_access"


def test_the_in_transit_list_puts_stat_first(lab_on, _catalogue, tech, phleb):
    first = _patient(_catalogue)
    second = _patient(_catalogue)
    _, routine = _collected(first, phleb[1])
    _, stat = _collected(second, phleb[1], CREATININE, priority="stat")
    ids = [s["id"] for s in client.get("/api/lab/receiving", headers=tech[1]).json()["in_transit"]]
    assert ids.index(stat) < ids.index(routine)


# ── 3. Reject ────────────────────────────────────────────────────────────────

def test_reject_needs_a_reason_from_the_list(lab_on, patient, tech, phleb):
    _, specimen_id = _collected(patient, phleb[1])
    none = client.post(f"/api/lab/specimens/{specimen_id}/reject", json={}, headers=tech[1])
    assert none.status_code == 422 and none.json()["detail"]["error"] == "reason_required"
    unknown = _reject(specimen_id, tech[1], "spilt_coffee")
    assert unknown.status_code == 422 and unknown.json()["detail"]["error"] == "unknown_reason"
    other = _reject(specimen_id, tech[1], "other")
    assert other.status_code == 422 and other.json()["detail"]["error"] == "note_required"
    assert _specimen(specimen_id)["status"] == "collected"


def test_reject_marks_the_tests_rejected_and_withdraws_unreleased_results(
        lab_on, patient, tech, phleb):
    order, specimen_id = _received(patient, phleb[1], tech[1])
    item_id = _items(order["id"])[0]["id"]
    entered = lrs.enter_result(item_id, value="4.2", actor={"id": tech[0],
                                                            "role": "lab_technician"})
    result_id = entered["result"]["id"]

    resp = _reject(specimen_id, tech[1], "haemolysed", note="Pink serum")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rejected_items"] == 1 and body["withdrawn_results"] == 1
    specimen = _specimen(specimen_id)
    assert (specimen["status"], specimen["rejection_code"], specimen["rejection_note"],
            specimen["rejected_by"]) == ("rejected", "haemolysed", "Pink serum", tech[0])
    assert [i["status"] for i in _items(order["id"])] == ["rejected"]
    with db_session() as db:
        result = db.execute("SELECT status, amend_reason FROM lab_results WHERE id = ?",
                            (result_id,)).fetchone()
    assert result["status"] == "withdrawn" and "Haemolysed" in result["amend_reason"]
    assert _order_row(order["id"])["status"] == "in_progress"
    assert len(_open_exceptions(order["id"], "rejected_no_decision")) == 1
    assert _audits("lab_specimen_rejected", specimen_id)
    assert body["incident_prompt"] is None


def test_a_sample_with_a_reported_result_cannot_be_rejected(lab_on, patient, tech, phleb):
    order, specimen_id = _received(patient, phleb[1], tech[1])
    with db_session() as db:
        db.execute("UPDATE lab_order_items SET status = 'released' WHERE order_id = ?",
                   (order["id"],))
    resp = _reject(specimen_id, tech[1])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "results_reported"
    assert _specimen(specimen_id)["status"] == "received"


def test_a_rejection_tells_the_ward_in_the_case_room(lab_on, _catalogue, tech, phleb):
    p = _patient(_catalogue, case_room=True)
    _, specimen_id = _collected(p, phleb[1])
    resp = _reject(specimen_id, tech[1], "clotted")
    assert resp.status_code == 200, resp.text
    notice = resp.json()["ward_notice"]
    assert notice["posted"] is True
    with db_session() as db:
        msg = dict(db.execute("SELECT * FROM messages WHERE id = ?",
                              (notice["message_id"],)).fetchone())
    assert msg["sender_name"] == "Laboratory" and msg["sender_id"] == tech[0]
    assert msg["message_type"] == "text" and msg["source"] == "lab"
    assert p["name"] in msg["content"] and "Clotted" in msg["content"]
    assert "\u2014" not in msg["content"]  # no em dash
    assert _events(specimen_id)[-1]["event_type"] == "ward_notified"


def test_a_patient_with_no_case_room_is_said_and_recorded(lab_on, patient, tech, phleb):
    _, specimen_id = _collected(patient, phleb[1])
    resp = _reject(specimen_id, tech[1])
    assert resp.status_code == 200
    assert resp.json()["ward_notice"] == {"posted": False, "reason": "no_case_room"}
    assert _events(specimen_id)[-1]["event_type"] == "ward_notice_failed"


def test_a_discharged_patient_is_not_reported_as_told(lab_on, _catalogue, tech, phleb):
    """Relay hides a discharged patient's case room from every channel list,
    so a post there reaches no clinician: nothing is posted, the specimen
    records the failure, and the answer tells the laboratory to phone."""
    p = _patient(_catalogue, case_room=True)
    order, specimen_id = _collected(p, phleb[1])
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (p["id"],))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?",
                   (p["id"],))
    resp = _reject(specimen_id, tech[1], "clotted")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ward_notice"] == {"posted": False, "reason": "case_room_closed"}
    assert _specimen(specimen_id)["status"] == "rejected"
    assert [i["status"] for i in _items(order["id"])] == ["rejected"]
    last = _events(specimen_id)[-1]
    assert last["event_type"] == "ward_notice_failed"
    assert json.loads(last["metadata_json"]) == {"reason": "case_room_closed"}
    assert "Phone the ward" in last["detail"]
    with db_session() as db:
        posted = db.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id IN "
            "(SELECT id FROM channels WHERE patient_id = ?)", (p["id"],)).fetchone()[0]
    assert posted == 0


def test_a_failed_notice_does_not_undo_the_rejection(lab_on, _catalogue, tech, phleb,
                                                       monkeypatch):
    p = _patient(_catalogue, case_room=True)
    order, specimen_id = _collected(p, phleb[1])

    def broken(*args, **kwargs):
        raise RuntimeError("relay is down")

    monkeypatch.setattr(los, "_insert_ward_notice", broken)
    resp = _reject(specimen_id, tech[1])
    assert resp.status_code == 200, resp.text
    assert resp.json()["ward_notice"]["posted"] is False
    assert resp.json()["ward_notice"]["reason"] == "failed"
    assert _specimen(specimen_id)["status"] == "rejected"
    assert [i["status"] for i in _items(order["id"])] == ["rejected"]
    assert _events(specimen_id)[-1]["event_type"] == "ward_notice_failed"


def test_a_rejection_whose_audit_fails_changes_nothing(lab_on, patient, tech, phleb,
                                                       monkeypatch):
    order, specimen_id = _collected(patient, phleb[1])

    def failing(**kwargs):
        if kwargs.get("action_type") == "lab_specimen_rejected":
            raise AuditWriteError("injected")
        return original(**kwargs)

    original = los.log_audit_event
    monkeypatch.setattr(los, "log_audit_event", failing)
    resp = _reject(specimen_id, tech[1])
    assert resp.status_code == 503
    assert _specimen(specimen_id)["status"] == "collected"
    assert [i["status"] for i in _items(order["id"])] == ["collected"]
    assert _open_exceptions(order["id"], "rejected_no_decision") == []


# ── 4. The NABH incident prompt ──────────────────────────────────────────────

def test_a_mislabelled_rejection_prompts_an_incident_with_nabh_on(lab_on, patient, tech, phleb,
                                                                   monkeypatch):
    monkeypatch.setattr(config, "NABH_ENABLED", True)
    _, specimen_id = _collected(patient, phleb[1])
    prompt = _reject(specimen_id, tech[1], "mislabelled").json()["incident_prompt"]
    assert prompt["incident_type"] == "specimen_error"
    assert prompt["reason_code"] == "mislabelled" and prompt["message"]
    # The register takes no report from a laboratory account, so the prompt
    # says who files it instead of sending the technologist to a refusal.
    from routes.nabh_routes import REPORTER_ROLES
    from services.nabh_service import INCIDENT_TYPES
    assert "specimen_error" in INCIDENT_TYPES
    assert not {"lab_technician", "pathologist"} & set(REPORTER_ROLES)
    assert "ward in charge" in prompt["message"]


def test_no_incident_prompt_with_nabh_off(lab_on, patient, tech, phleb, monkeypatch):
    monkeypatch.setattr(config, "NABH_ENABLED", False)
    _, specimen_id = _collected(patient, phleb[1])
    assert _reject(specimen_id, tech[1], "unlabelled").json()["incident_prompt"] is None


def test_no_incident_prompt_for_a_reason_that_is_not_an_identification_event(
        lab_on, patient, tech, phleb, monkeypatch):
    monkeypatch.setattr(config, "NABH_ENABLED", True)
    _, specimen_id = _collected(patient, phleb[1])
    assert _reject(specimen_id, tech[1], "insufficient").json()["incident_prompt"] is None


# ── 5. The decision: recollect or cancel ─────────────────────────────────────

def test_a_rejected_sample_is_listed_until_a_decision(lab_on, _catalogue, tech, phleb):
    p = _patient(_catalogue)
    _, specimen_id = _collected(p, phleb[1])
    _reject(specimen_id, tech[1], "clotted")

    rejected = client.get("/api/lab/receiving", headers=tech[1]).json()["rejected"]
    entry = next(s for s in rejected if s["id"] == specimen_id)
    assert entry["rejection_display"] == "Clotted"
    nurse = _user("nurse", WARD)[1]
    ward = client.get("/api/lab/collection", headers=nurse).json()["specimens"]
    row = next(s for s in ward if s["id"] == specimen_id)
    assert row["recollection_needed"] is True and row["rejection_display"] == "Clotted"


def test_recollect_plans_a_new_tube_and_moves_the_tests(lab_on, _catalogue, tech, phleb):
    p = _patient(_catalogue)
    order, specimen_id = _collected(p, phleb[1])
    _reject(specimen_id, tech[1], "clotted")

    resp = client.post(f"/api/lab/specimens/{specimen_id}/recollect",
                       json={"note": "Ward told"}, headers=tech[1])
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["recollected_as"]
    assert new_id != specimen_id and lab_accession.is_valid_accession(new_id)
    new = _specimen(new_id)
    assert (new["status"], new["recollect_of"], new["label_print_count"]) == (
        "planned", specimen_id, 0)
    assert [(i["specimen_id"], i["status"]) for i in _items(order["id"])] == [
        (new_id, "awaiting_collection")]
    assert _specimen(specimen_id)["status"] == "rejected"
    assert _events(specimen_id)[-1]["event_type"] == "recollect_requested"
    assert _open_exceptions(order["id"], "rejected_no_decision") == []
    assert _order_row(order["id"])["status"] == "in_progress"
    assert _audits("lab_recollect_requested", specimen_id)

    rejected = client.get("/api/lab/receiving", headers=tech[1]).json()["rejected"]
    assert specimen_id not in [s["id"] for s in rejected]
    ward = client.get("/api/lab/collection", headers=_user("nurse", WARD)[1]).json()["specimens"]
    ids = {s["id"]: s for s in ward}
    assert specimen_id not in ids
    assert ids[new_id]["recollect_of"] == specimen_id

    twice = client.post(f"/api/lab/specimens/{specimen_id}/recollect", json={},
                        headers=tech[1])
    assert twice.status_code == 409
    assert twice.json()["detail"]["error"] == "nothing_to_recollect"


def test_the_recollected_tube_is_collected_like_any_other(lab_on, _catalogue, tech, phleb):
    p = _patient(_catalogue)
    _, specimen_id = _collected(p, phleb[1])
    _reject(specimen_id, tech[1], "clotted")
    new_id = client.post(f"/api/lab/specimens/{specimen_id}/recollect", json={},
                         headers=tech[1]).json()["recollected_as"]
    printed = client.post("/api/lab/collection/labels",
                          json={"specimen_ids": [new_id], "identity": _identity(p)},
                          headers=phleb[1])
    assert printed.status_code == 200, printed.text
    collected = client.post(f"/api/lab/specimens/{new_id}/collect",
                            json={"identity": _identity(p)}, headers=phleb[1])
    assert collected.status_code == 200, collected.text
    received = client.post(f"/api/lab/specimens/{new_id}/receive", json={}, headers=tech[1])
    assert received.status_code == 200, received.text


def test_cancelling_the_rejected_tests_is_the_other_decision(lab_on, _catalogue, tech, phleb):
    p = _patient(_catalogue)
    order, specimen_id = _collected(p, phleb[1])
    _reject(specimen_id, tech[1], "insufficient")

    no_reason = client.post(f"/api/lab/specimens/{specimen_id}/cancel-rejected", json={},
                            headers=tech[1])
    assert no_reason.status_code == 422
    resp = client.post(f"/api/lab/specimens/{specimen_id}/cancel-rejected",
                       json={"reason": "Doctor no longer needs it"}, headers=tech[1])
    assert resp.status_code == 200, resp.text
    items = _items(order["id"])
    assert [(i["status"], i["cancelled_by"]) for i in items] == [("cancelled", tech[0])]
    assert _order_row(order["id"])["status"] == "cancelled"
    assert _open_exceptions(order["id"], "rejected_no_decision") == []
    rejected = client.get("/api/lab/receiving", headers=tech[1]).json()["rejected"]
    assert specimen_id not in [s["id"] for s in rejected]
    assert _audits("lab_rejected_tests_cancelled", specimen_id)


def test_recollect_stops_while_draining_and_cancel_does_not(lab_on, _catalogue, tech, phleb,
                                                            monkeypatch):
    p = _patient(_catalogue)
    _, specimen_id = _collected(p, phleb[1])
    _reject(specimen_id, tech[1], "clotted")
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    assert lab_module.module_state() == "draining"
    blocked = client.post(f"/api/lab/specimens/{specimen_id}/recollect", json={},
                          headers=tech[1])
    assert blocked.status_code == 404
    assert blocked.json()["detail"]["error"] == "module_disabled"
    assert client.get("/api/lab/receiving", headers=tech[1]).status_code == 200
    cancelled = client.post(f"/api/lab/specimens/{specimen_id}/cancel-rejected",
                            json={"reason": "Laboratory closing"}, headers=tech[1])
    assert cancelled.status_code == 200, cancelled.text


def test_receipt_routes_are_404_with_the_module_off(patient, tech, monkeypatch):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    assert lab_module.module_state() == "off"
    resp = client.get("/api/lab/receiving", headers=tech[1])
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "module_disabled"


def test_the_notice_text_has_plain_words():
    text = los.rejection_notice_text({
        "patient_name": "Asha Menon", "specimen": "Serum", "tests": ["Potassium"],
        "reason": "Haemolysed: pink", "collected_at": "2026-10-06T04:00:00.000000+00:00"})
    assert text.startswith("The laboratory rejected the Serum sample for Asha Menon collected ")
    assert "(Potassium)" in text and "Reason: Haemolysed: pink." in text
    assert "\u2014" not in text  # no em dash
