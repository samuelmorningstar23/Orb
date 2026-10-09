"""The ward critical card and the ward's read-back acknowledgement (lis-16).

Rulings applied as build defaults (go-live held on LIS-R20, an R023
question, and LIS-R3): the card is the laboratory's own communication. It
shows the laboratory's value, its own flag and the limit its pathologist
agreed, the patient's name and UHID (no bed once discharged), and whether
the value is preliminary, never a score or a severity Orb worked out. Pinned
here:
  * the card lists only the open criticals of patients on the caller's ward
    (PATIENT_DEPARTMENT_SQL: a transfer moves the card, a discharged patient
    stays on the last ward without a bed);
  * an acknowledgement needs the name of the person told and read_back true;
    a patient on another ward is 404; a stale screen is 409 and writes
    nothing;
  * an acknowledgement is audited (fail-closed), closes the critical and
    stops its ladder, and the laboratory sees it was closed on the ward;
  * the card answers 404 while the laboratory module is off and the
    interfaced switch is off; laboratory staff and administrators do not get
    the ward card.
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
from services import case_room_alerts, lab_module, lab_tat_service, orders_service
from services import lab_critical_service as lcs
from services import lab_master_service as lm
from services import site_config_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import format_utc, parse_to_utc, utc_iso_now
from services.site_config_service import SiteConfig

client = TestClient(main.app)

WARD = "General Medicine"
OTHER_WARD = "Cardiology"
SUFFIX = uuid.uuid4().hex[:5].upper()
CODE = f"W8GA{SUFFIX}-K"
LADDER = [
    {"tier": 1, "role": "Ward nurse in charge", "label": "Ward nurse in charge",
     "timeout_minutes": 15},
    {"tier": 2, "role": "Duty doctor", "label": "Duty doctor", "timeout_minutes": None},
]


# ── fixtures ─────────────────────────────────────────────────────────────────

def _user(role, dept=None):
    uid = f"LCA{uuid.uuid4().hex[:6].upper()}"
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
    lm.create_test({"code_system": "LOCAL", "code": CODE, "kind": "analyte",
                    "name": "Ward potassium", "short_name": "Ward K", "decimal_places": 1,
                    "unit": "mmol/L", "result_type": "numeric", "section_id": "biochemistry",
                    "specimen_type_code": "serum"}, actor=master)
    created: list[str] = []
    yield created
    test_id = lm.test_id_for("LOCAL", CODE)
    with db_session() as db:
        if created:
            marks = ", ".join("?" for _ in created)
            ids = tuple(created)
            db.execute(f"DELETE FROM lab_critical_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_critical_notifications WHERE patient_id IN ({marks})",
                       ids)
            db.execute(f"DELETE FROM lab_exceptions WHERE order_id IN "
                       f"(SELECT id FROM orders WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM observations WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_result_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_results WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_order_items WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimen_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimens WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM messages WHERE channel_id IN "
                       f"(SELECT id FROM channels WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM channels WHERE patient_id IN ({marks})", ids)
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
            db.execute(f"UPDATE admissions SET status = 'discharged' "
                       f"WHERE patient_id IN ({marks})", ids)
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?", (test_id,))
        db.execute("DELETE FROM lab_tests WHERE id = ?", (test_id,))
        db.execute("DELETE FROM terminology WHERE code_system = 'LOCAL' AND code = ?", (CODE,))
    lab_module.refresh_drain_state()


@pytest.fixture(scope="module", autouse=True)
def _limit(master, _catalogue):
    test_id = lm.test_id_for("LOCAL", CODE)
    row = lm.approve_critical_limit(test_id, {"critical_low": "2.5", "critical_high": "6.5",
                                              "agreed_with": "Dr Ward, physician lead",
                                              "method_note": "Ward card test limit"},
                                    actor=master)
    yield row


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
def no_ladder(monkeypatch):
    monkeypatch.setattr(site_config_service, "_loaded", SiteConfig())


@pytest.fixture
def ladder(monkeypatch):
    monkeypatch.setattr(site_config_service, "_loaded",
                        SiteConfig(present=True, lab_critical={"tiers": [dict(t) for t in LADDER]}))


@pytest.fixture(autouse=True)
def pushes(monkeypatch):
    sent: list[tuple] = []

    def record(msg_data, channel_id, *, what=""):
        sent.append((msg_data, channel_id))
        return True

    monkeypatch.setattr(case_room_alerts, "push_message_new", record)
    return sent


@pytest.fixture(scope="module")
def tech():
    return _user("lab_technician")


@pytest.fixture(scope="module")
def phleb():
    return _user("phlebotomist")


@pytest.fixture(scope="module")
def nurse():
    return _user("nurse", WARD)


@pytest.fixture(scope="module")
def doctor():
    return _user("doctor", WARD)


@pytest.fixture(scope="module")
def other_nurse():
    return _user("nurse", OTHER_WARD)


def _patient(created, *, dept=WARD, case_room=True):
    pid = f"PLCA{uuid.uuid4().hex[:8].upper()}"
    name = f"Ward card {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-LCA-{uuid.uuid4().hex[:8].upper()}"
    bed = f"B{uuid.uuid4().hex[:3]}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed, date_of_birth, sex) "
                   "VALUES (?, ?, ?, 'admitted', 'GM-1', ?, '1975-03-02', 'male')",
                   (pid, name, mrn, bed))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"ALCA{uuid.uuid4().hex[:8].upper()}", pid, dept))
        if case_room:
            db.execute("INSERT INTO channels (id, name, type, patient_id) "
                       "VALUES (?, ?, 'case_room', ?)", (f"lca-{pid.lower()}", f"Case {name}", pid))
    created.append(pid)
    return {"id": pid, "name": name, "mrn": mrn, "bed": bed,
            "room": f"lca-{pid.lower()}" if case_room else None}


def _critical(created, tech, phleb, *, dept=WARD, value="7.0"):
    """A patient on `dept` with a critical raised by technical validation."""
    p = _patient(created, dept=dept)
    order = orders_service.create_order(
        patient_id=p["id"], order_type="lab", description="K", code=CODE, code_system="LOCAL",
        priority="stat", ordered_by="D001", status="active")
    with db_session() as db:
        item = dict(db.execute("SELECT id, specimen_id FROM lab_order_items WHERE order_id = ?",
                               (order["id"],)).fetchone())
    identity = {"uhid": p["mrn"], "name": p["name"]}
    assert client.post("/api/lab/collection/labels",
                       json={"specimen_ids": [item["specimen_id"]], "identity": identity},
                       headers=phleb[1]).status_code == 200
    assert client.post(f"/api/lab/specimens/{item['specimen_id']}/collect",
                       json={"identity": identity}, headers=phleb[1]).status_code == 200
    assert client.post(f"/api/lab/specimens/{item['specimen_id']}/receive", json={},
                       headers=tech[1]).status_code == 200
    entered = client.post("/api/lab/results", json={"order_item_id": item["id"], "value": value},
                          headers=tech[1])
    assert entered.status_code == 200, entered.text
    result = entered.json()["result"]
    validated = client.post(
        "/api/lab/results/tech-validate",
        json={"results": [{"result_id": result["id"],
                           "expected": {"value_text": result["value_text"],
                                        "comment": result.get("comment"),
                                        "abnormal_flag": result.get("abnormal_flag")}}]},
        headers=tech[1])
    assert validated.status_code == 200, validated.text
    (note,) = _notes(p["id"])
    return p, note


def _notes(patient_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_critical_notifications WHERE patient_id = ? ORDER BY raised_at",
            (patient_id,)).fetchall()]


def _events(notification_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_critical_events WHERE notification_id = ? ORDER BY id",
            (notification_id,)).fetchall()]


def _card(headers) -> dict:
    resp = client.get("/api/lab/criticals/ward", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _card_item(headers, patient_id):
    items = [n for n in _card(headers)["open"] if n["patient"]["id"] == patient_id]
    return items[0] if items else None


def _ack(headers, item, **overrides):
    body = {"recipient_name": "Sister Mary Rao", "read_back": True,
            "value_told": item["value_told"], **overrides}
    return client.post(f"/api/lab/criticals/{item['id']}/acknowledge", json=body,
                       headers=headers)


def _later(minutes):
    return format_utc(parse_to_utc(utc_iso_now()) + timedelta(minutes=minutes))


# ── 1. The card ──────────────────────────────────────────────────────────────

def test_the_card_shows_the_laboratorys_value_flag_and_limit_for_its_own_ward_only(
        lab_on, _catalogue, tech, phleb, nurse, other_nurse, no_ladder):
    mine, _ = _critical(_catalogue, tech, phleb)
    theirs, _ = _critical(_catalogue, tech, phleb, dept=OTHER_WARD, value="2.1")
    item = _card_item(nurse[1], mine["id"])
    assert item is not None
    assert _card_item(nurse[1], theirs["id"]) is None
    assert _card_item(other_nurse[1], mine["id"]) is None
    other = _card_item(other_nurse[1], theirs["id"])
    assert other["lab_flag"] == {"code": "LL", "text": "Critical low"}
    assert item["patient"] == {"id": mine["id"], "name": mine["name"], "mrn": mine["mrn"],
                               "bed": mine["bed"], "discharged": False, "discharged_at": None}
    assert (item["test_name"], item["value_told"], item["value_display"]) == (
        "Ward potassium", "7.0 mmol/L", "7.0 mmol/L")
    assert item["lab_flag"] == {"code": "HH", "text": "Critical high"}
    assert item["critical_limit_text"] == "Critical below 2.5 or above 6.5 mmol/L"
    assert item["preliminary"] is True
    assert item["state_text"] == "Preliminary, not yet authorised"
    assert item["sender"] == "Laboratory" and item["source_lane"] == "lis"
    assert item["change_text"] is None and item["withdrawn"] is False
    # The laboratory's communication only: no tier clock, no score, no severity.
    text = json.dumps(item).lower()
    for word in ("score", "news2", "severity", "tier", "due_at"):
        assert word not in text, word
    body = _card(nurse[1])
    assert body["scope"] == {"department": WARD, "hospital_wide": False}
    assert body["methods"] == lcs.WARD_ACK_METHODS


def test_a_transferred_patients_critical_moves_to_the_new_ward(
        lab_on, _catalogue, tech, phleb, nurse, other_nurse, no_ladder):
    p, _ = _critical(_catalogue, tech, phleb)
    assert _card_item(nurse[1], p["id"]) is not None
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'transferred' WHERE patient_id = ?", (p["id"],))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                   "created_at) VALUES (?, ?, 'D001', ?, 'active', ?)",
                   (f"ALCA{uuid.uuid4().hex[:8].upper()}", p["id"], OTHER_WARD,
                    (datetime.now(timezone.utc) + timedelta(seconds=5)).strftime(
                        "%Y-%m-%d %H:%M:%S")))
    assert _card_item(nurse[1], p["id"]) is None
    assert _card_item(other_nurse[1], p["id"]) is not None
    (note,) = _notes(p["id"])
    assert note["status"] == "open", "a transfer never closes a critical"


def test_a_discharged_patient_stays_on_the_last_ward_without_a_bed(
        lab_on, _catalogue, tech, phleb, nurse, no_ladder):
    p, _ = _critical(_catalogue, tech, phleb)
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (p["id"],))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (p["id"],))
    item = _card_item(nurse[1], p["id"])
    assert item is not None
    assert item["patient"]["bed"] is None and item["patient"]["discharged"] is True
    assert item["patient"]["mrn"] == p["mrn"]


# ── 2. Acknowledging ─────────────────────────────────────────────────────────

def test_an_acknowledgement_without_a_read_back_is_refused(
        lab_on, _catalogue, tech, phleb, nurse, no_ladder):
    p, note = _critical(_catalogue, tech, phleb)
    item = _card_item(nurse[1], p["id"])
    refused = _ack(nurse[1], item, read_back=False)
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "read_back_required"
    body = {"recipient_name": "Sister Mary Rao", "value_told": item["value_told"]}
    missing = client.post(f"/api/lab/criticals/{item['id']}/acknowledge", json=body,
                          headers=nurse[1])
    assert missing.status_code == 422
    assert _notes(p["id"])[0]["status"] == "open"


@pytest.mark.parametrize("name", ["", " ", "R"])
def test_an_acknowledgement_names_the_person_told(lab_on, _catalogue, tech, phleb, nurse,
                                                  no_ladder, name):
    p, _ = _critical(_catalogue, tech, phleb)
    item = _card_item(nurse[1], p["id"])
    refused = _ack(nurse[1], item, recipient_name=name)
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "recipient_required"
    assert _notes(p["id"])[0]["status"] == "open"


def test_an_unknown_role_or_method_is_refused(lab_on, _catalogue, tech, phleb, nurse, no_ladder):
    p, _ = _critical(_catalogue, tech, phleb)
    item = _card_item(nurse[1], p["id"])
    assert _ack(nurse[1], item, recipient_role="porter").status_code == 422
    assert _ack(nurse[1], item, method="in_person").status_code == 422
    assert _notes(p["id"])[0]["status"] == "open"


def test_a_patient_on_another_ward_is_404(lab_on, _catalogue, tech, phleb, nurse, other_nurse,
                                          no_ladder):
    p, note = _critical(_catalogue, tech, phleb)
    item = _card_item(nurse[1], p["id"])
    refused = _ack(other_nurse[1], item)
    assert refused.status_code == 404
    unknown = client.post("/api/lab/criticals/LC-not-a-real-one/acknowledge",
                          json={"recipient_name": "Sister Rao", "read_back": True,
                                "value_told": "7.0 mmol/L"}, headers=nurse[1])
    assert unknown.status_code == 404
    assert _notes(p["id"])[0]["status"] == "open"


def test_a_stale_card_is_refused_and_writes_nothing(lab_on, _catalogue, tech, phleb, nurse,
                                                    no_ladder):
    p, _ = _critical(_catalogue, tech, phleb)
    item = _card_item(nurse[1], p["id"])
    refused = _ack(nurse[1], item, value_told="6.9 mmol/L")
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["error"] == "value_changed" and detail["value_told"] == "7.0 mmol/L"
    assert "Nothing was recorded" in detail["message"]
    assert _notes(p["id"])[0]["status"] == "open"
    assert [e["event_type"] for e in _events(item["id"])] == ["raised", "posted"]


def test_an_acknowledgement_is_audited_and_stops_the_ladder(
        lab_on, _catalogue, tech, phleb, nurse, ladder, pushes):
    p, note = _critical(_catalogue, tech, phleb)
    item = _card_item(nurse[1], p["id"])
    done = _ack(nurse[1], item)
    assert done.status_code == 200, done.text
    view = done.json()["notification"]
    assert view["status"] == "acknowledged"
    assert view["acknowledged"]["closure"] == "ward_read_back"
    assert view["acknowledged"]["method_text"] == "Read on the ward screen"
    (row,) = _notes(p["id"])
    assert (row["status"], row["method"], row["read_back"], row["recipient_name"],
            row["recipient_role"], row["acknowledged_by"]) == (
        "acknowledged", "in_app", 1, "Sister Mary Rao", "nurse", nurse[0])
    ack = [e for e in _events(row["id"]) if e["event_type"] == "acknowledged"]
    assert len(ack) == 1
    meta = json.loads(ack[0]["metadata_json"])
    assert meta["closure"] == "ward_read_back" and meta["value_told"] == "7.0 mmol/L"
    assert meta["read_back"] is True
    with db_session() as db:
        audit = db.execute("SELECT event_category, actor_user_id, patient_id FROM audit_logs "
                           "WHERE action_type = 'lab_critical_acknowledged' AND target_id = ?",
                           (row["id"],)).fetchall()
    assert [tuple(a) for a in audit] == [("clinical_access", nurse[0], p["id"])]
    # The tier clock stops: a sweep long after the tier fell due moves nothing.
    posts = len(pushes)
    lcs.sweep(now=_later(90))
    (row,) = _notes(p["id"])
    assert row["current_tier"] == 1 and row["status"] == "acknowledged"
    assert [e["event_type"] for e in _events(row["id"])].count("escalated") == 0
    assert len(pushes) == posts
    # Gone from the card; a second acknowledgement is refused.
    assert _card_item(nurse[1], p["id"]) is None
    again = _ack(nurse[1], item)
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_closed"
    # The laboratory sees how it was closed, and the turnaround report counts
    # it as a ward read-back.
    with db_session() as db:
        listed = lcs.list_for_lab(db, dept=None)
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        times = lab_tat_service.critical_times(db, start, start + timedelta(hours=2))
    lab_view = next(n for n in listed["recent"] if n["id"] == row["id"])
    assert lab_view["acknowledged"]["closure"] == "ward_read_back"
    assert lab_view["acknowledged"]["recipient_name"] == "Sister Mary Rao"
    (timed,) = [t for t in times if t["notification_id"] == row["id"]]
    assert timed["closed_by"] == "ward_read_back" and timed["minutes"] is not None


def test_a_doctor_acknowledges_a_phone_call_from_the_laboratory(
        lab_on, _catalogue, tech, phleb, doctor, no_ladder):
    p, _ = _critical(_catalogue, tech, phleb)
    item = _card_item(doctor[1], p["id"])
    done = _ack(doctor[1], item, recipient_name="Dr Iyer", method="phone",
                caller_name="Lab technologist Anil")
    assert done.status_code == 200, done.text
    (row,) = _notes(p["id"])
    assert (row["method"], row["recipient_role"]) == ("phone", "doctor")
    meta = json.loads([e for e in _events(row["id"])
                       if e["event_type"] == "acknowledged"][0]["metadata_json"])
    assert meta["caller_name"] == "Lab technologist Anil"
    assert done.json()["notification"]["acknowledged"]["method_text"] == (
        "Phone call from the laboratory")


def test_an_audit_failure_acknowledges_nothing(lab_on, _catalogue, tech, phleb, nurse, no_ladder,
                                               monkeypatch):
    p, _ = _critical(_catalogue, tech, phleb)
    item = _card_item(nurse[1], p["id"])
    real = lcs.log_audit_event

    def failing(**kwargs):
        if kwargs.get("action_type") == "lab_critical_acknowledged":
            raise AuditWriteError("audit store down")
        return real(**kwargs)

    monkeypatch.setattr(lcs, "log_audit_event", failing)
    refused = _ack(nurse[1], item)
    assert refused.status_code == 503
    (row,) = _notes(p["id"])
    assert row["status"] == "open" and row["acknowledged_at"] is None
    assert "acknowledged" not in [e["event_type"] for e in _events(row["id"])]


def test_a_withdrawn_value_is_read_back_as_withdrawn(lab_on, _catalogue, tech, phleb, nurse,
                                                     no_ladder):
    """A critical whose result the laboratory withdrew says so on the card,
    with no flag and no limit, and the read-back names the withdrawal."""
    p, note = _critical(_catalogue, tech, phleb)
    now = utc_iso_now()
    with db_session(immediate=True) as db:
        result = dict(db.execute("SELECT * FROM lab_results WHERE id = ?",
                                 (note["result_id"],)).fetchone())
        db.execute("UPDATE lab_results SET status = 'withdrawn' WHERE id = ?", (result["id"],))
        ids = lcs.record_withdrawal_in_txn(db, result, actor={"id": tech[0],
                                                               "role": "lab_technician"}, now=now)
    assert ids == [note["id"]]
    item = _card_item(nurse[1], p["id"])
    assert item["withdrawn"] is True and item["value_told"] == lcs.WITHDRAWN_TOLD
    assert item["lab_flag"] is None and item["critical_limit_text"] is None
    assert "Do not act on it" in item["change_text"]
    assert _ack(nurse[1], item).status_code == 200


# ── 3. Who gets the card, and when ───────────────────────────────────────────

def test_laboratory_staff_and_administrators_do_not_get_the_ward_card(lab_on, tech, no_ladder):
    assert client.get("/api/lab/criticals/ward", headers=tech[1]).status_code == 403
    _, admin = _user("admin")
    assert client.get("/api/lab/criticals/ward", headers=admin).status_code == 403
    refused = client.post("/api/lab/criticals/LC-x/acknowledge", headers=tech[1],
                          json={"recipient_name": "Anil", "read_back": True,
                                "value_told": "7.0 mmol/L"})
    assert refused.status_code == 403


def test_the_card_is_absent_with_the_laboratory_off_and_the_interfaced_switch_off(
        monkeypatch, nurse):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {})
    for resp in (client.get("/api/lab/criticals/ward", headers=nurse[1]),
                 client.post("/api/lab/criticals/LC-x/acknowledge", headers=nurse[1],
                             json={"recipient_name": "Sister Rao", "read_back": True,
                                   "value_told": "7.0 mmol/L"})):
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "module_disabled"
    assert client.get("/api/lab/criticals/ward").status_code == 401


def test_the_card_is_served_with_only_the_interfaced_switch_on(monkeypatch, nurse):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", True)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {})
    assert client.get("/api/lab/criticals/ward", headers=nurse[1]).status_code == 200


def test_the_ward_read_is_audited_only_when_it_shows_a_patient(
        lab_on, _catalogue, tech, phleb, no_ladder):
    uid, empty_ward = _user("nurse", f"Empty ward {uuid.uuid4().hex[:4]}")
    with db_session() as db:
        before = db.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = "
                            "'lab_critical_ward_viewed' AND actor_user_id = ?", (uid,)).fetchone()[0]
    assert _card(empty_ward)["open"] == []
    with db_session() as db:
        after = db.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = "
                           "'lab_critical_ward_viewed' AND actor_user_id = ?", (uid,)).fetchone()[0]
    assert after == before
    nurse_id, ward_nurse = _user("nurse", WARD)
    p, _ = _critical(_catalogue, tech, phleb)
    assert _card_item(ward_nurse, p["id"]) is not None
    with db_session() as db:
        rows = db.execute("SELECT event_category, metadata_json FROM audit_logs WHERE action_type = "
                          "'lab_critical_ward_viewed' AND actor_user_id = ?",
                          (nurse_id,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == "phi_access"
    assert p["id"] in json.loads(rows[0][1])["patient_ids"]
