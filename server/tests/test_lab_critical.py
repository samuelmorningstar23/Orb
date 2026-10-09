"""Laboratory critical results, the laboratory's side (lis-15).

Pinned here (rulings LIS-R1, LIS-R3, LIS-R4, LIS-R19 applied as build
defaults; go-live held on LIS-R20):
  * Technical validation of a value past a pathologist-approved critical
    limit raises ONE open notification for the result, marked preliminary,
    and after the commit posts it to the patient's case room as the
    technologist, shown as 'Laboratory' (role 'laboratory', type 'alert',
    is_bot 0, source 'lab'), pushed live as 'message:new'.
  * No approved limit, no critical, whatever the flag says.
  * A patient with no case room: delivery_status 'no_case_room', the
    critical stays open, its tier is due at once and the next sweep moves
    it up the ladder. A failed post is retried once per tier and never
    posted twice.
  * A callback closes it only with the name and role of the person told,
    the method and their read-back. A Relay reply never closes it.
    Discharge never closes it, and the Critical results screen says
    'Patient discharged'.
  * The ladder comes from site.toml [lab.critical]; Orb ships none, and the
    file refuses a blank or unknown key and a section with no tiers.
  * Nothing here touches the NEWS2 escalation chains or the shadow log.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import case_room_alerts, lab_module, orders_service, site_config_service
from services import lab_critical_service as lcs
from services import lab_master_service as lm
from services import terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import format_utc, parse_to_utc, utc_iso_now
from services.site_config_service import SiteConfig, SiteConfigError, load_site_config

client = TestClient(main.app)

WARD = "General Medicine"
SUFFIX = uuid.uuid4().hex[:5].upper()
CODE = f"W7HC{SUFFIX}-K"
LADDER = [
    {"tier": 1, "role": "Ward nurse in charge and the ordering doctor",
     "label": "Ward nurse in charge and the ordering doctor", "timeout_minutes": 15},
    {"tier": 2, "role": "Attending or duty doctor", "label": "Attending or duty doctor",
     "timeout_minutes": 15},
    {"tier": 3, "role": "Duty medical officer", "label": "Duty medical officer",
     "timeout_minutes": None},
]


# ── fixtures ─────────────────────────────────────────────────────────────────

def _user(role, dept=None):
    uid = f"LCR{uuid.uuid4().hex[:6].upper()}"
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
                    "name": "Critical potassium", "short_name": "Crit K", "decimal_places": 1,
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


@pytest.fixture
def limit(master):
    test_id = lm.test_id_for("LOCAL", CODE)
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?", (test_id,))
    row = lm.approve_critical_limit(test_id, {"critical_low": "2.5", "critical_high": "6.5",
                                              "agreed_with": "Dr Ward, physician lead",
                                              "method_note": "Critical test limit"},
                                    actor=master)
    yield row
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?", (test_id,))


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


@pytest.fixture
def pushes(monkeypatch):
    sent: list[tuple] = []

    def record(msg_data, channel_id, *, what=""):
        sent.append((msg_data, channel_id))
        return True

    monkeypatch.setattr(case_room_alerts, "push_message_new", record)
    return sent


@pytest.fixture
def keep_site_config(monkeypatch):
    """load_site_config replaces the module's loaded config; put it back."""
    monkeypatch.setattr(site_config_service, "_loaded", site_config_service._loaded)


def _patient(created, *, case_room=True):
    pid = f"PLCR{uuid.uuid4().hex[:8].upper()}"
    name = f"Critical {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-LCR-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed, date_of_birth, sex) "
                   "VALUES (?, ?, ?, 'admitted', 'GM-1', ?, '1975-03-02', 'male')",
                   (pid, name, mrn, f"B{uuid.uuid4().hex[:3]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"ALCR{uuid.uuid4().hex[:8].upper()}", pid, WARD))
        if case_room:
            db.execute("INSERT INTO channels (id, name, type, patient_id) "
                       "VALUES (?, ?, 'case_room', ?)", (f"lcr-{pid.lower()}", f"Case {name}", pid))
    created.append(pid)
    return {"id": pid, "name": name, "mrn": mrn,
            "room": f"lcr-{pid.lower()}" if case_room else None}


@pytest.fixture(scope="module")
def tech():
    return _user("lab_technician")


@pytest.fixture(scope="module")
def phleb():
    return _user("phlebotomist")


@pytest.fixture(scope="module")
def signer():
    return _user("pathologist")


def _received(p, phleb_hdr, tech_hdr):
    order = orders_service.create_order(
        patient_id=p["id"], order_type="lab", description="K", code=CODE, code_system="LOCAL",
        priority="stat", ordered_by="D001", status="active")
    with db_session() as db:
        item = dict(db.execute("SELECT id, specimen_id FROM lab_order_items WHERE order_id = ?",
                               (order["id"],)).fetchone())
    identity = {"uhid": p["mrn"], "name": p["name"]}
    assert client.post("/api/lab/collection/labels",
                       json={"specimen_ids": [item["specimen_id"]], "identity": identity},
                       headers=phleb_hdr).status_code == 200
    assert client.post(f"/api/lab/specimens/{item['specimen_id']}/collect",
                       json={"identity": identity}, headers=phleb_hdr).status_code == 200
    assert client.post(f"/api/lab/specimens/{item['specimen_id']}/receive", json={},
                       headers=tech_hdr).status_code == 200
    return order, item["specimen_id"], item["id"]


def _seen(result):
    return {"value_text": result["value_text"], "comment": result.get("comment"),
            "abnormal_flag": result.get("abnormal_flag")}


def _enter(item_id, value, hdr):
    resp = client.post("/api/lab/results", json={"order_item_id": item_id, "value": value},
                       headers=hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _validate(result, hdr):
    return client.post("/api/lab/results/tech-validate",
                       json={"results": [{"result_id": result["id"],
                                          "expected": _seen(result)}]}, headers=hdr)


def _critical_validated(created, tech, phleb, *, case_room=True, value="7.0"):
    p = _patient(created, case_room=case_room)
    order, specimen_id, item_id = _received(p, phleb[1], tech[1])
    entered = _enter(item_id, value, tech[1])
    resp = _validate(entered, tech[1])
    assert resp.status_code == 200, resp.text
    return p, order, specimen_id, entered, resp.json()


def _notifications(patient_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_critical_notifications WHERE patient_id = ? ORDER BY raised_at",
            (patient_id,)).fetchall()]


def _events(notification_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_critical_events WHERE notification_id = ? ORDER BY id",
            (notification_id,)).fetchall()]


def _room_messages(room):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY id", (room,)).fetchall()]


def _later(minutes):
    return format_utc(parse_to_utc(utc_iso_now()) + timedelta(minutes=minutes))


# ── 1. Raising at technical validation (LIS-R4 (a)) ─────────────────────────

def test_validation_raises_one_critical_and_posts_it_as_the_laboratory(
        lab_on, _catalogue, limit, tech, phleb, no_ladder, pushes):
    p, _, _, entered, body = _critical_validated(_catalogue, tech, phleb)
    (note,) = _notifications(p["id"])
    assert (note["status"], note["source_lane"], note["result_id"], note["preliminary"]) == (
        "open", "lis", entered["id"], 1)
    assert note["value_display"] == "7.0 mmol/L" and note["test_name"] == "Critical potassium"
    assert note["raised_by"] == tech[0] and note["raised_in_department"] == WARD
    assert body["criticals"] == [{"notification_id": note["id"], "posted": True, "reason": None,
                                  "kind": "raised", "message_id": note["relay_message_id"]}]
    (msg,) = _room_messages(p["room"])
    assert msg["id"] == note["relay_message_id"]
    assert (msg["sender_id"], msg["sender_name"], msg["sender_role"]) == (
        tech[0], "Laboratory", "laboratory")
    assert (msg["message_type"], msg["is_bot"], msg["source"]) == ("alert", 0, "lab")
    assert msg["idempotency_key"] == f"lab-critical:{note['id']}:t1"
    for words in (p["name"], p["mrn"], "Critical potassium 7.0 mmol/L",
                  "Critical below 2.5 or above 6.5 mmol/L", "Preliminary, not yet authorised"):
        assert words in msg["content"], words
    assert "\u2014" not in msg["content"]
    assert "NEWS2" not in msg["content"] and "score" not in msg["content"].lower()
    assert [e["event_type"] for e in _events(note["id"])] == ["raised", "posted"]
    # Pushed live to the room with the body a reload shows.
    assert len(pushes) == 1
    assert pushes[0][1] == p["room"] and pushes[0][0]["id"] == msg["id"]
    assert pushes[0][0]["is_bot"] is False and pushes[0][0]["source"] == "lab"
    with db_session() as db:
        audit = db.execute("SELECT event_category FROM audit_logs WHERE action_type = "
                           "'lab_critical_raised' AND target_id = ?", (note["id"],)).fetchall()
    assert [a[0] for a in audit] == ["clinical_access"]


def test_no_ladder_means_due_at_once(lab_on, _catalogue, limit, tech, phleb, no_ladder, pushes):
    p, *_ = _critical_validated(_catalogue, tech, phleb)
    (note,) = _notifications(p["id"])
    assert note["tier_due_at"] == note["raised_at"]
    assert json.loads(note["policy_json"])["configured"] is False


def test_one_open_notification_per_result(lab_on, _catalogue, limit, tech, phleb, signer,
                                          no_ladder, pushes):
    p, _, specimen_id, entered, body = _critical_validated(_catalogue, tech, phleb)
    validated = body["results"][0]
    released = client.post("/api/lab/results/authorise",
                           json={"specimen_id": specimen_id,
                                 "results": [{"result_id": validated["id"],
                                              "expected": _seen(validated)}]},
                           headers=signer[1])
    assert released.status_code == 200, released.text
    assert released.json()["criticals"] == []
    assert len(_notifications(p["id"])) == 1
    with db_session() as db, pytest.raises(Exception):
        db.execute(
            "INSERT INTO lab_critical_notifications (id, source_lane, result_id, patient_id, "
            "test_name, value_display, raised_at, status) VALUES (?, 'lis', ?, ?, 'K', '7.0', ?, "
            "'open')", (f"LC-dup-{uuid.uuid4().hex[:6]}", entered["id"], p["id"], utc_iso_now()))


def test_a_release_raises_one_that_validation_did_not(lab_on, _catalogue, limit, tech, phleb,
                                                      signer, no_ladder, pushes):
    """A result validated before the laboratory raised criticals is raised
    at release, authorised (not preliminary)."""
    p, _, specimen_id, entered, body = _critical_validated(_catalogue, tech, phleb)
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_events WHERE patient_id = ?", (p["id"],))
        db.execute("DELETE FROM lab_critical_notifications WHERE patient_id = ?", (p["id"],))
    validated = body["results"][0]
    released = client.post("/api/lab/results/authorise",
                           json={"specimen_id": specimen_id,
                                 "results": [{"result_id": validated["id"],
                                              "expected": _seen(validated)}]},
                           headers=signer[1])
    assert released.status_code == 200, released.text
    (note,) = _notifications(p["id"])
    assert note["preliminary"] == 0 and released.json()["criticals"][0]["posted"] is True
    msg = _room_messages(p["room"])[-1]
    assert "Authorised by" in msg["content"] and "Preliminary" not in msg["content"]


def test_no_critical_without_an_approved_limit(lab_on, _catalogue, tech, phleb, no_ladder,
                                               pushes):
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?",
                   (lm.test_id_for("LOCAL", CODE),))
    p, _, _, entered, body = _critical_validated(_catalogue, tech, phleb, value="9.9")
    assert entered["is_critical"] == 0 and body["criticals"] == []
    assert _notifications(p["id"]) == []
    # A row that claims to be critical without naming an approved limit, or
    # naming one that is not there, raises nothing either.
    now = utc_iso_now()
    with db_session() as db:
        for limit_id in (None, 987654321):
            fake = {**entered, "is_critical": 1, "critical_limit_id": limit_id}
            assert lcs.raise_for_result_in_txn(db, fake, actor={"id": tech[0],
                                                                "role": "lab_technician"},
                                               preliminary=True, now=now,
                                               trigger="tech_validated") is None
    assert _notifications(p["id"]) == []


def test_an_audit_failure_raises_and_validates_nothing(lab_on, _catalogue, limit, tech, phleb,
                                                       no_ladder, pushes, monkeypatch):
    p = _patient(_catalogue)
    _, _, item_id = _received(p, phleb[1], tech[1])
    entered = _enter(item_id, "7.1", tech[1])
    real = lcs.log_audit_event

    def failing(**kwargs):
        if kwargs.get("action_type") == "lab_critical_raised":
            raise AuditWriteError("audit store down")
        return real(**kwargs)

    monkeypatch.setattr(lcs, "log_audit_event", failing)
    resp = _validate(entered, tech[1])
    assert resp.status_code == 503, resp.text
    assert _notifications(p["id"]) == []
    with db_session() as db:
        assert db.execute("SELECT status FROM lab_results WHERE id = ?",
                          (entered["id"],)).fetchone()[0] == "entered"
    assert _room_messages(p["room"]) == []


# ── 2. Delivery that fails ───────────────────────────────────────────────────

def test_no_case_room_is_undelivered_stays_open_and_climbs_on_the_next_sweep(
        lab_on, _catalogue, limit, tech, phleb, ladder, pushes):
    p, _, _, _, body = _critical_validated(_catalogue, tech, phleb, case_room=False)
    (note,) = _notifications(p["id"])
    assert body["criticals"][0]["posted"] is False
    assert body["criticals"][0]["reason"] == "no_case_room"
    assert (note["status"], note["delivery_status"], note["current_tier"]) == (
        "open", "no_case_room", 1)
    # The tier is due at once, not 15 minutes from now.
    assert parse_to_utc(note["tier_due_at"]) <= parse_to_utc(utc_iso_now())
    failed = [e for e in _events(note["id"]) if e["event_type"] == "delivery_failed"]
    assert json.loads(failed[0]["metadata_json"])["reason"] == "no_case_room"
    assert "Phone the ward" in failed[0]["detail"]
    out = lcs.sweep()
    assert out["escalated"] >= 1
    (note,) = _notifications(p["id"])
    assert note["current_tier"] == 2 and note["status"] == "open"
    assert note["delivery_status"] == "no_case_room"
    kinds = [(e["event_type"], e["tier"]) for e in _events(note["id"])]
    assert ("escalated", 2) in kinds and ("delivery_failed", 2) in kinds
    with db_session() as db:
        audit = db.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = "
                           "'lab_critical_escalated' AND target_id = ?",
                           (note["id"],)).fetchone()[0]
    assert audit == 1


def test_a_failed_post_is_retried_once_per_tier_and_never_doubled(
        lab_on, _catalogue, limit, tech, phleb, no_ladder, pushes, monkeypatch):
    real = case_room_alerts.persist_case_room_alert
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("relay write failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(case_room_alerts, "persist_case_room_alert", flaky)
    p, _, _, _, body = _critical_validated(_catalogue, tech, phleb)
    (note,) = _notifications(p["id"])
    assert body["criticals"][0] == {"notification_id": note["id"], "posted": False,
                                    "reason": "failed", "kind": "raised", "message_id": None}
    assert note["delivery_status"] == "failed" and note["status"] == "open"
    assert _room_messages(p["room"]) == []
    # No ladder: the one retry of this tier is the next sweep's.
    assert lcs.sweep()["retried"] >= 1
    (note,) = _notifications(p["id"])
    assert note["delivery_status"] == "posted"
    assert len(_room_messages(p["room"])) == 1
    # Delivered: nothing more to try, however often the sweep runs.
    lcs.sweep()
    lcs.sweep()
    assert len(_room_messages(p["room"])) == 1
    # A retry after a crash between the post and its bookkeeping finds the
    # tier's own post by its key and never posts again.
    with db_session() as db:
        db.execute("UPDATE lab_critical_notifications SET delivery_status = 'failed' "
                   "WHERE id = ?", (note["id"],))
        db.execute("DELETE FROM lab_critical_events WHERE notification_id = ? "
                   "AND event_type = 'delivery_failed'", (note["id"],))
    with db_session(immediate=True) as db:
        again = lcs.deliver_in_txn(db, note["id"], now=utc_iso_now())
    assert again["posted"] is True and again["reason"] == "already_posted"
    assert len(_room_messages(p["room"])) == 1


def test_an_undelivered_last_tier_gets_one_retry_and_no_more(
        lab_on, _catalogue, limit, tech, phleb, no_ladder, pushes):
    p, *_ = _critical_validated(_catalogue, tech, phleb, case_room=False)
    (note,) = _notifications(p["id"])
    lcs.sweep()
    lcs.sweep()
    lcs.sweep()
    attempts = [e for e in _events(note["id"]) if e["event_type"] == "delivery_failed"]
    assert len(attempts) == lcs.ATTEMPTS_PER_TIER
    (note,) = _notifications(p["id"])
    assert note["status"] == "open", "Orb never closes a critical by itself"
    # Due since it was raised: a failed post on a tier already due keeps the
    # time it fell due, so it never looks less overdue on the screen.
    assert note["tier_due_at"] == note["raised_at"]


# ── 3. The ladder ────────────────────────────────────────────────────────────

def test_the_sweep_climbs_the_ladder_and_posts_each_tier_once(
        lab_on, _catalogue, limit, tech, phleb, ladder, pushes):
    p, *_ = _critical_validated(_catalogue, tech, phleb)
    (note,) = _notifications(p["id"])
    due = parse_to_utc(note["tier_due_at"]) - parse_to_utc(note["raised_at"])
    assert timedelta(minutes=14) < due <= timedelta(minutes=15)
    assert lcs.sweep()["escalated"] == 0, "not due yet"
    out = lcs.sweep(now=_later(16))
    assert out["escalated"] >= 1 and out["pushes"]
    (note,) = _notifications(p["id"])
    assert note["current_tier"] == 2 and note["delivery_status"] == "posted"
    messages = _room_messages(p["room"])
    assert len(messages) == 2
    assert messages[1]["idempotency_key"] == f"lab-critical:{note['id']}:t2"
    assert "The laboratory is now calling: Attending or duty doctor" in messages[1]["content"]
    reached = _later(40)
    lcs.sweep(now=reached)
    (note,) = _notifications(p["id"])
    # The last tier never hands on: it is due from the moment it is reached.
    assert note["current_tier"] == 3 and note["tier_due_at"] == reached
    lcs.sweep(now=_later(600))
    assert len(_room_messages(p["room"])) == 3, "a posted last tier is never posted again"
    (note,) = _notifications(p["id"])
    assert note["status"] == "open" and note["tier_due_at"] == reached


def test_a_critical_at_the_top_of_its_ladder_is_listed_first_and_due(
        lab_on, _catalogue, limit, tech, phleb, pushes, monkeypatch):
    """Round 3 review: a critical nobody confirmed that climbed to the last
    tier is due from the moment it got there, so the screen (the most
    overdue first) lists it before a fresh tier-1 critical not due for 15
    minutes, and counts it as due. The same holds for a row written before
    the last tier kept that time (tier_due_at NULL)."""
    two = [dict(LADDER[0]), {**LADDER[2], "tier": 2}]
    monkeypatch.setattr(site_config_service, "_loaded",
                        SiteConfig(present=True, lab_critical={"tiers": two}))
    old_p, *_ = _critical_validated(_catalogue, tech, phleb)
    (old,) = _notifications(old_p["id"])
    with db_session() as db:
        db.execute("UPDATE lab_critical_notifications SET tier_due_at = ? WHERE id = ?",
                   (_later(-1), old["id"]))
    before = utc_iso_now()
    assert lcs.sweep()["escalated"] >= 1
    (old,) = _notifications(old_p["id"])
    assert old["current_tier"] == 2 and old["tier_due_at"] is not None
    assert parse_to_utc(before) <= parse_to_utc(old["tier_due_at"]) <= parse_to_utc(utc_iso_now())
    new_p, *_ = _critical_validated(_catalogue, tech, phleb)
    (new,) = _notifications(new_p["id"])
    assert new["current_tier"] == 1

    def listed():
        body = client.get("/api/lab/criticals", headers=tech[1]).json()
        return [(n["id"], n["tier"]["current"], n["tier"]["due_now"], n["tier"]["due_at"])
                for n in body["open"] if n["id"] in (old["id"], new["id"])]

    (first, second) = listed()
    assert (first[0], first[1], first[2]) == (old["id"], 2, True)
    assert first[3] == old["tier_due_at"]
    assert (second[0], second[1], second[2]) == (new["id"], 1, False)
    # A row from before the fix, with no time on its last tier, is due from
    # when the ladder reached it and still listed first.
    with db_session() as db:
        db.execute("UPDATE lab_critical_notifications SET tier_due_at = NULL WHERE id = ?",
                   (old["id"],))
        reached = db.execute("SELECT occurred_at FROM lab_critical_events WHERE "
                             "notification_id = ? AND event_type = 'escalated' AND tier = 2 "
                             "ORDER BY id DESC LIMIT 1", (old["id"],)).fetchone()[0]
    (first, second) = listed()
    assert first == (old["id"], 2, True, reached)
    assert second[0] == new["id"]


def test_a_notification_keeps_the_ladder_it_was_raised_under(
        lab_on, _catalogue, limit, tech, phleb, ladder, pushes, monkeypatch):
    p, *_ = _critical_validated(_catalogue, tech, phleb)
    monkeypatch.setattr(site_config_service, "_loaded", SiteConfig())
    lcs.sweep(now=_later(16))
    (note,) = _notifications(p["id"])
    assert note["current_tier"] == 2


def test_the_ladder_is_read_from_site_toml(tmp_path, keep_site_config):
    path = tmp_path / "site.toml"
    path.write_text(
        '[site]\nname = "Lab Hospital"\n\n[lab.critical]\n\n[[lab.critical.tiers]]\n'
        'role = "Ward nurse in charge"\ntimeout_minutes = 15\n\n[[lab.critical.tiers]]\n'
        'role = "Duty doctor"\nlabel = "Duty doctor on call"\ntimeout_minutes = 30\n')
    cfg = load_site_config(path, apply_env=False)
    assert cfg.lab_critical == {"tiers": [
        {"tier": 1, "role": "Ward nurse in charge", "label": "Ward nurse in charge",
         "timeout_minutes": 15},
        {"tier": 2, "role": "Duty doctor", "label": "Duty doctor on call",
         "timeout_minutes": None}]}
    assert any("last critical-result tier" in note for note in cfg.errors)
    assert site_config_service.site_config_snapshot()["lab_critical_tiers"][1]["tier"] == 2


@pytest.mark.parametrize("body, words", [
    ('[lab.critical]\n', "lists no tiers"),
    ('[lab.critical]\n[[lab.critical.tiers]]\nrole = ""\n', "empty"),
    ('[lab.critical]\n[[lab.critical.tiers]]\nrole = "Nurse"\ntimeout = 15\n',
     "not a setting Orb knows"),
    ('[lab.critical]\nescalate = true\n[[lab.critical.tiers]]\nrole = "Nurse"\n',
     "not a setting Orb knows"),
    ('[lab]\ncritcal = 1\n', "not a setting Orb knows"),
    ('[lab.critical]\n[[lab.critical.tiers]]\nrole = "Nurse"\n[[lab.critical.tiers]]\n'
     'role = "Doctor"\n', "has no timeout_minutes"),
    ('[lab.critical]\n[[lab.critical.tiers]]\nrole = "Nurse"\ntimeout_minutes = 0\n'
     '[[lab.critical.tiers]]\nrole = "Doctor"\n', "outside the range"),
])
def test_the_ladder_refuses_a_blank_unknown_or_empty_setting(tmp_path, body, words,
                                                              keep_site_config):
    path = tmp_path / "site.toml"
    path.write_text('[site]\nname = "Lab Hospital"\n\n' + body)
    with pytest.raises(SiteConfigError) as refused:
        load_site_config(path, apply_env=False)
    assert words in str(refused.value)


def test_orb_ships_no_ladder(keep_site_config):
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "site_config" / "site.example.toml"
    cfg = load_site_config(example, apply_env=False)
    assert cfg.lab_critical == {}
    assert lcs.ladder() == []


# ── 4. The callback log (LIS-R3) ─────────────────────────────────────────────

def _told_now(nid):
    """The value the ward was last told for a critical, as the screen shows it
    (value_told), which a callback or an attempt names."""
    with db_session() as db:
        row = db.execute("SELECT * FROM lab_critical_notifications WHERE id = ?",
                         (nid,)).fetchone()
        return lcs._told(dict(row), lcs._changes(db, nid)) if row is not None else "7.0 mmol/L"


def _callback(nid, hdr, **over):
    body = {"recipient_name": "Sister Mary Thomas", "recipient_role": "nurse_in_charge",
            "method": "phone", "read_back": True, "note": "Read back 7.0",
            "value_told": _told_now(nid)}
    body.update(over)
    return client.post(f"/api/lab/criticals/{nid}/callback", json=body, headers=hdr)


def test_a_callback_needs_recipient_role_method_and_read_back(
        lab_on, _catalogue, limit, tech, phleb, no_ladder, pushes):
    p, *_ = _critical_validated(_catalogue, tech, phleb)
    (note,) = _notifications(p["id"])
    assert _callback(note["id"], tech[1], recipient_name=" ").json()["detail"]["error"] == \
        "recipient_required"
    assert _callback(note["id"], tech[1], recipient_role="porter").json()["detail"]["error"] == \
        "recipient_role_required"
    assert _callback(note["id"], tech[1], method="in_app").status_code == 422
    refused = _callback(note["id"], tech[1], read_back=False)
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "read_back_required"
    assert _notifications(p["id"])[0]["status"] == "open"
    ok = _callback(note["id"], tech[1])
    assert ok.status_code == 200, ok.text
    view = ok.json()["notification"]
    assert view["status"] == "acknowledged"
    assert view["acknowledged"]["recipient_name"] == "Sister Mary Thomas"
    assert view["acknowledged"]["recipient_role_text"] == "Nurse in charge"
    assert view["acknowledged"]["read_back"] is True
    stored = _notifications(p["id"])[0]
    assert (stored["status"], stored["recipient_role"], stored["method"], stored["read_back"],
            stored["acknowledged_by"]) == ("acknowledged", "nurse_in_charge", "phone", 1, tech[0])
    assert [e["event_type"] for e in _events(note["id"])][-2:] == ["callback_logged",
                                                                   "acknowledged"]
    again = _callback(note["id"], tech[1])
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_closed"
    with db_session() as db:
        audit = db.execute("SELECT event_category, metadata_json FROM audit_logs WHERE "
                           "action_type = 'lab_critical_callback_logged' AND target_id = ?",
                           (note["id"],)).fetchall()
    assert len(audit) == 1 and audit[0][0] == "clinical_access"
    assert json.loads(audit[0][1])["closes"] is True


def test_only_laboratory_staff_log_a_callback(lab_on, _catalogue, limit, tech, phleb,
                                              no_ladder, pushes):
    p, *_ = _critical_validated(_catalogue, tech, phleb)
    (note,) = _notifications(p["id"])
    nurse = _user("nurse", WARD)
    admin = _user("admin")
    for hdr in (phleb[1], nurse[1], admin[1]):
        assert _callback(note["id"], hdr).status_code == 403
    assert _notifications(p["id"])[0]["status"] == "open"


def test_a_call_that_reached_nobody_is_logged_and_keeps_it_open(
        lab_on, _catalogue, limit, tech, phleb, no_ladder, pushes):
    p, *_ = _critical_validated(_catalogue, tech, phleb)
    (note,) = _notifications(p["id"])
    resp = client.post(f"/api/lab/criticals/{note['id']}/attempt",
                       json={"method": "phone", "note": "Ward phone rang out twice",
                             "value_told": "7.0 mmol/L"},
                       headers=tech[1])
    assert resp.status_code == 200, resp.text
    view = resp.json()["notification"]
    assert view["status"] == "open"
    assert view["callbacks"][0]["reached"] is False
    assert view["callbacks"][0]["note"] == "Ward phone rang out twice"


def test_a_relay_reply_never_closes_it(lab_on, _catalogue, limit, tech, phleb, no_ladder,
                                       pushes):
    from services import relay_service

    p, *_ = _critical_validated(_catalogue, tech, phleb)
    doctor = _user("doctor", WARD)
    relay_service.send_message(p["room"], doctor[0], "Dr Reply", "doctor",
                               "Seen. Repeat K sent, read back 7.0.")
    lcs.sweep()
    assert _notifications(p["id"])[0]["status"] == "open"


# ── 5. Discharge (LIS-R19) and the Critical results screen ──────────────────

def test_discharge_leaves_it_open_and_the_screen_says_patient_discharged(
        lab_on, _catalogue, limit, tech, phleb, ladder, pushes, monkeypatch):
    import services.discharge_service as ds

    p, *_ = _critical_validated(_catalogue, tech, phleb)
    doctor = _user("doctor", WARD)

    async def _draft(_pid):
        return "Discharged home."

    monkeypatch.setattr(ds, "generate_discharge_summary", _draft)
    resp = client.post(f"/api/vigil/patients/{p['id']}/discharge", headers=doctor[1])
    assert resp.status_code == 200, resp.text
    (note,) = _notifications(p["id"])
    assert note["status"] == "open", "discharge must never close a critical"
    listed = client.get("/api/lab/criticals", headers=tech[1])
    assert listed.status_code == 200, listed.text
    (view,) = [n for n in listed.json()["open"] if n["id"] == note["id"]]
    assert view["patient"]["discharged"] is True
    assert view["patient"]["bed"] is None and view["patient"]["ward"] is None
    # The next tier's post for a discharged patient is not made into a closed
    # room: it is recorded as not delivered and the tier is due at once.
    posted_before = len(_room_messages(p["room"]))
    lcs.sweep(now=_later(16))
    (note,) = _notifications(p["id"])
    assert note["current_tier"] == 2 and note["status"] == "open"
    assert note["delivery_status"] == "no_case_room"
    last = [e for e in _events(note["id"]) if e["event_type"] == "delivery_failed"][-1]
    assert json.loads(last["metadata_json"])["reason"] == "case_room_closed"
    assert "discharging consultant" in last["detail"]
    assert len(_room_messages(p["room"])) == posted_before
    listed = client.get("/api/lab/criticals", headers=tech[1]).json()
    (view,) = [n for n in listed["open"] if n["id"] == note["id"]]
    assert "discharged" in view["delivery"]["text"]


def test_the_screen_lists_open_criticals_and_is_audited(lab_on, _catalogue, limit, tech, phleb,
                                                        ladder, pushes):
    p, *_ = _critical_validated(_catalogue, tech, phleb)
    (note,) = _notifications(p["id"])
    resp = client.get("/api/lab/criticals", headers=tech[1])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (view,) = [n for n in body["open"] if n["id"] == note["id"]]
    assert view["preliminary"] is True and view["value_display"] == "7.0 mmol/L"
    assert view["critical_limit_text"] == "Critical below 2.5 or above 6.5 mmol/L"
    assert view["tier"]["current"] == 1 and view["tier"]["count"] == 3
    assert view["tier"]["label"] == "Ward nurse in charge and the ordering doctor"
    assert view["delivery"]["status"] == "posted"
    assert view["result_now"]["status"] == "tech_validated"
    assert body["ladder"]["configured"] is True
    with db_session() as db:
        audit = db.execute("SELECT event_category, metadata_json FROM audit_logs WHERE "
                           "action_type = 'lab_critical_list_viewed' AND actor_user_id = ? "
                           "ORDER BY id DESC LIMIT 1", (tech[0],)).fetchone()
    assert audit[0] == "phi_access"
    assert p["id"] in json.loads(audit[1])["patient_ids"]
    nurse = _user("nurse", WARD)
    assert client.get("/api/lab/criticals", headers=nurse[1]).status_code == 403
    _callback(note["id"], tech[1])
    recent = client.get("/api/lab/criticals", headers=tech[1]).json()["recent"]
    assert any(n["id"] == note["id"] for n in recent)


def test_the_screen_is_reachable_while_draining_and_404_when_off(tech, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    off = client.get("/api/lab/criticals", headers=tech[1])
    assert off.status_code == 404 and off.json()["detail"]["error"] == "module_disabled"
    assert client.post("/api/lab/criticals/LC-x/callback", headers=tech[1],
                       json={}).status_code == 404
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    assert client.get("/api/lab/criticals", headers=tech[1]).status_code == 200
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", True)
    assert client.get("/api/lab/criticals", headers=tech[1]).status_code == 200


def test_an_open_critical_keeps_a_switched_off_laboratory_draining(
        lab_on, _catalogue, limit, tech, phleb, no_ladder, pushes):
    _critical_validated(_catalogue, tech, phleb)
    with db_session() as db:
        assert lab_module.open_work_counts(db)["critical_results"] >= 1


# ── 6. Nothing else moves ────────────────────────────────────────────────────

def test_news2_chains_and_the_shadow_log_are_untouched(lab_on, _catalogue, limit, tech, phleb,
                                                       ladder, pushes):
    p, *_ = _critical_validated(_catalogue, tech, phleb, case_room=False)
    lcs.sweep()
    with db_session() as db:
        chains = db.execute("SELECT COUNT(*) FROM clinical_escalations WHERE patient_id = ?",
                            (p["id"],)).fetchone()[0]
        shadow = db.execute("SELECT COUNT(*) FROM shadow_events WHERE patient_id = ?",
                            (p["id"],)).fetchone()[0]
        vitals = db.execute("SELECT COUNT(*) FROM vitals WHERE patient_id = ?",
                            (p["id"],)).fetchone()[0]
        risks = db.execute("SELECT COUNT(*) FROM risk_scores WHERE patient_id = ?",
                           (p["id"],)).fetchone()[0]
    assert (chains, shadow, vitals, risks) == (0, 0, 0, 0)


def test_the_sweep_does_nothing_while_every_critical_surface_is_off(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    assert lcs.surface_open() is False
    assert lcs.sweep()["ran"] is False


@pytest.mark.parametrize("tiers, tier, minutes", [
    ([], 1, 0), (LADDER, 1, 15), (LADDER, 2, 15), (LADDER, 3, 0), (LADDER, 4, None),
    # A timeout on the last tier is ignored, as the site file says when it loads one.
    ([dict(LADDER[0]), {**LADDER[2], "timeout_minutes": 30}], 2, 0)])
def test_when_a_tier_is_due(tiers, tier, minutes):
    """The last tier never hands on, so it is due from the moment it is
    reached (round 3 review); a tier the ladder does not have is never due."""
    now = utc_iso_now()
    due = lcs.due_at(now, tiers, tier)
    if minutes is None:
        assert due is None
    else:
        assert parse_to_utc(due) - parse_to_utc(now) == timedelta(minutes=minutes)


# ── 7. A critical sent back to the bench and changed (round 1 review) ───────
#
# The value the ward was told is the value a critical stands for. When a
# signatory sends the result back to the bench and its value is changed and
# validated again, the open critical says so in words, the ward is told the
# preliminary value it was given has changed (a new post, never the stale
# value again), the sweep's next tier posts the value as it now stands, and a
# critical already confirmed with the ward is reopened when its value no
# longer holds, so the laboratory phones the correction and logs it.

def _return(result, hdr, reason="Haemolysed sample, rerun on the same tube"):
    return client.post(f"/api/lab/results/{result['id']}/return",
                       json={"reason": reason, "expected": _seen(result)}, headers=hdr)


def _edit(result, value, hdr):
    resp = client.patch(f"/api/lab/results/{result['id']}",
                        json={"value": value, "reason": "Rerun after a haemolysed sample",
                              "expected": _seen(result)}, headers=hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _rerun(validated, value, tech, signer):
    """Send a validated result back, change it to `value` and validate it
    again. Returns (the result as validated again, the validate answer)."""
    back = _return(validated, signer[1])
    assert back.status_code == 200, back.text
    edited = _edit(back.json()["result"], value, tech[1])
    again = _validate(edited, tech[1])
    assert again.status_code == 200, again.text
    body = again.json()
    return body["results"][0], body


def _release(specimen_id, result, hdr):
    resp = client.post("/api/lab/results/authorise",
                       json={"specimen_id": specimen_id,
                             "results": [{"result_id": result["id"], "expected": _seen(result)}]},
                       headers=hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _listed(tech_hdr, nid):
    body = client.get("/api/lab/criticals", headers=tech_hdr).json()
    found = [n for n in body["open"] + body["recent"] if n["id"] == nid]
    assert len(found) == 1, nid
    return found[0]


def test_a_sent_back_critical_changed_to_normal_tells_the_ward_and_stays_open(
        lab_on, _catalogue, limit, tech, phleb, signer, ladder, pushes):
    p, _, specimen_id, _, body = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (note,) = _notifications(p["id"])
    validated, again = _rerun(body["results"][0], "4.5", tech, signer)
    with db_session() as db:
        stored = dict(db.execute("SELECT value_text, status, is_critical FROM lab_results "
                                 "WHERE id = ?", (validated["id"],)).fetchone())
    assert stored == {"value_text": "4.5", "status": "tech_validated", "is_critical": 0}
    # The ward is told, at once, that the preliminary value it was given has
    # changed: a new post under its own key, never a second copy of the old one.
    assert again["criticals"] == [{"notification_id": note["id"], "posted": True,
                                   "reason": None, "kind": "corrected",
                                   "message_id": again["criticals"][0]["message_id"]}]
    first, second = _room_messages(p["room"])
    assert second["idempotency_key"] == f"lab-critical:{note['id']}:t1:r1"
    for words in ("Correction to a critical laboratory result", p["name"],
                  "Critical potassium 7.0 mmol/L as critical", "now 4.5 mmol/L",
                  "which is not critical", "Preliminary, not yet authorised",
                  "The laboratory will phone to confirm"):
        assert words in second["content"], words
    assert "\u2014" not in second["content"]
    # The critical stays open, standing for the value the ward was told.
    (note,) = _notifications(p["id"])
    assert (note["status"], note["value_display"], note["current_tier"]) == (
        "open", "7.0 mmol/L", 1)
    changed = [json.loads(e["metadata_json"]) for e in _events(note["id"])
               if e["event_type"] == "escalated"]
    assert [(c["reason"], c["from_value"], c["to_value"], c["still_critical"]) for c in changed] \
        == [("value_changed", "7.0 mmol/L", "4.5 mmol/L", False)]
    with db_session() as db:
        audit = db.execute("SELECT event_category FROM audit_logs WHERE action_type = "
                           "'lab_critical_value_changed' AND target_id = ?",
                           (note["id"],)).fetchall()
    assert [a[0] for a in audit] == ["clinical_access"]
    # The screen says the change in words and the value as it now stands.
    view = _listed(tech[1], note["id"])
    assert view["value_now"] == "4.5 mmol/L" and view["result_now"]["changed"] is True
    assert view["result_now"]["status"] == "tech_validated"
    for words in ("Preliminary, not yet authorised", "now 4.5 mmol/L", "not critical",
                  "7.0 mmol/L", "Phone the ward"):
        assert words in view["result_now"]["text"], words
    assert view["changes"][0]["to_value"] == "4.5 mmol/L"
    # The next tier posts the value as it stands, never the stale 7.0 as critical.
    lcs.sweep(now=_later(16))
    third = _room_messages(p["room"])[-1]
    assert third["idempotency_key"] == f"lab-critical:{note['id']}:t2:r1"
    assert "Correction to a critical laboratory result" in third["content"]
    assert "now 4.5 mmol/L" in third["content"]
    assert "Critical laboratory result for" not in third["content"]
    # Released: the screen says authorised beside the new value, and nothing
    # more is raised or posted for a value already told.
    posts = len(_room_messages(p["room"]))
    released = _release(specimen_id, validated, signer[1])
    assert released["criticals"] == []
    assert len(_room_messages(p["room"])) == posts
    view = _listed(tech[1], note["id"])
    assert view["result_now"]["status"] == "released"
    assert "Authorised and on the chart" in view["result_now"]["text"]
    assert "now 4.5 mmol/L" in view["result_now"]["text"]
    assert len(_notifications(p["id"])) == 1


def test_a_sent_back_critical_changed_to_another_critical_value_is_posted_again(
        lab_on, _catalogue, limit, tech, phleb, signer, no_ladder, pushes):
    p, _, specimen_id, _, body = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (note,) = _notifications(p["id"])
    validated, again = _rerun(body["results"][0], "6.9", tech, signer)
    assert validated["is_critical"] == 1
    (answer,) = again["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        note["id"], True, "changed")
    second = _room_messages(p["room"])[-1]
    assert second["idempotency_key"] == f"lab-critical:{note['id']}:t1:r1"
    for words in ("Changed result", "reported 7.0 mmol/L", "now 6.9 mmol/L",
                  "Critical potassium 6.9 mmol/L", "Preliminary, not yet authorised"):
        assert words in second["content"], words
    view = _listed(tech[1], note["id"])
    assert view["value_now"] == "6.9 mmol/L" and view["result_now"]["changed"] is True
    assert "was 7.0 mmol/L, now 6.9 mmol/L" in view["result_now"]["text"]
    # The release backstop raises nothing more for the value already told.
    released = _release(specimen_id, validated, signer[1])
    assert released["criticals"] == [] and len(_notifications(p["id"])) == 1
    # The callback confirms the value as it now stands.
    ok = _callback(note["id"], tech[1], note="Read back 6.9")
    assert ok.status_code == 200, ok.text
    with db_session() as db:
        audit = db.execute("SELECT summary_message, metadata_json FROM audit_logs WHERE "
                           "action_type = 'lab_critical_callback_logged' AND target_id = ?",
                           (note["id"],)).fetchone()
    assert "6.9 mmol/L" in audit[0]
    assert json.loads(audit[1])["value_told"] == "6.9 mmol/L"


def test_a_confirmed_critical_changed_to_normal_is_reopened_and_the_ward_told(
        lab_on, _catalogue, limit, tech, phleb, signer, ladder, pushes):
    p, _, _, _, body = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (note,) = _notifications(p["id"])
    assert _callback(note["id"], tech[1]).status_code == 200
    lcs.sweep(now=_later(16))
    _, again = _rerun(body["results"][0], "4.5", tech, signer)
    (answer,) = again["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        note["id"], True, "corrected")
    (note,) = _notifications(p["id"])
    assert (note["status"], note["current_tier"], note["acknowledged_at"], note["read_back"]) == (
        "open", 1, None, None)
    due = parse_to_utc(note["tier_due_at"]) - parse_to_utc(utc_iso_now())
    assert timedelta(minutes=14) < due <= timedelta(minutes=15)
    last = _room_messages(p["room"])[-1]
    assert last["idempotency_key"] == f"lab-critical:{note['id']}:t1:r1"
    assert "Correction to a critical laboratory result" in last["content"]
    assert "now 4.5 mmol/L" in last["content"]
    changed = [json.loads(e["metadata_json"]) for e in _events(note["id"])
               if e["event_type"] == "escalated"]
    assert changed[-1]["reopened"] is True and changed[-1]["still_critical"] is False
    view = _listed(tech[1], note["id"])
    assert view["status"] == "open" and view["acknowledged"] is None
    assert view["callbacks"][0]["reached"] is True, "the earlier callback stays in the log"
    assert "Phone the ward" in view["result_now"]["text"]
    # Closed again only by a new callback with a read-back.
    assert _callback(note["id"], tech[1], note="Told the change, read back 4.5").status_code == 200
    assert _notifications(p["id"])[0]["status"] == "acknowledged"


def test_a_confirmed_critical_changed_to_another_critical_value_raises_one_naming_the_earlier(
        lab_on, _catalogue, limit, tech, phleb, signer, no_ladder, pushes):
    p, _, _, _, body = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (first,) = _notifications(p["id"])
    assert _callback(first["id"], tech[1]).status_code == 200
    _, again = _rerun(body["results"][0], "6.8", tech, signer)
    notes = _notifications(p["id"])
    assert [(n["status"], n["value_display"]) for n in notes] == [
        ("acknowledged", "7.0 mmol/L"), ("open", "6.8 mmol/L")]
    (answer,) = again["criticals"]
    assert (answer["notification_id"], answer["kind"]) == (notes[1]["id"], "changed")
    last = _room_messages(p["room"])[-1]
    assert "reported 7.0 mmol/L" in last["content"] and "now 6.8 mmol/L" in last["content"]


def test_a_value_changed_back_is_told_again_under_a_new_key(
        lab_on, _catalogue, limit, tech, phleb, signer, no_ladder, pushes):
    p, _, _, _, body = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (note,) = _notifications(p["id"])
    validated, _ = _rerun(body["results"][0], "4.5", tech, signer)
    _, again = _rerun(validated, "7.0", tech, signer)
    (answer,) = again["criticals"]
    assert (answer["notification_id"], answer["posted"], answer["kind"]) == (
        note["id"], True, "changed")
    keys = [m["idempotency_key"] for m in _room_messages(p["room"])]
    assert keys == [f"lab-critical:{note['id']}:t1", f"lab-critical:{note['id']}:t1:r1",
                    f"lab-critical:{note['id']}:t1:r2"]
    last = _room_messages(p["room"])[-1]
    assert "reported 4.5 mmol/L" in last["content"]
    assert "Critical potassium 7.0 mmol/L" in last["content"]
    assert len(_notifications(p["id"])) == 1


def test_a_failed_post_of_a_change_still_gets_its_one_retry(
        lab_on, _catalogue, limit, tech, phleb, signer, no_ladder, pushes, monkeypatch):
    """With no ladder the first tier is also the last: the change's post has
    its own attempts, so the one retry is not used up by the earlier post."""
    p, _, _, _, body = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (note,) = _notifications(p["id"])
    real = case_room_alerts.persist_case_room_alert
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("relay write failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(case_room_alerts, "persist_case_room_alert", flaky)
    _, again = _rerun(body["results"][0], "4.5", tech, signer)
    assert again["criticals"][0]["posted"] is False
    assert len(_room_messages(p["room"])) == 1
    assert lcs.sweep()["retried"] >= 1
    last = _room_messages(p["room"])[-1]
    assert last["idempotency_key"] == f"lab-critical:{note['id']}:t1:r1"
    assert "now 4.5 mmol/L" in last["content"]
    lcs.sweep()
    assert len(_room_messages(p["room"])) == 2


def test_a_tier_posted_while_the_result_is_back_on_the_bench_says_it_may_change(
        lab_on, _catalogue, limit, tech, phleb, signer, ladder, pushes):
    p, _, _, _, body = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (note,) = _notifications(p["id"])
    assert _return(body["results"][0], signer[1]).status_code == 200
    lcs.sweep(now=_later(16))
    last = _room_messages(p["room"])[-1]
    assert last["idempotency_key"] == f"lab-critical:{note['id']}:t2"
    assert "checking this result again" in last["content"]
    assert "Preliminary, not yet authorised" not in last["content"]
    view = _listed(tech[1], note["id"])
    assert view["result_now"]["status"] == "entered"
    assert "Sent back to the bench" in view["result_now"]["text"]


def test_a_change_committed_but_never_posted_is_posted_by_the_next_sweep(
        lab_on, _catalogue, limit, tech, phleb, no_ladder, pushes):
    """The step's transaction recorded the change and the process stopped
    before its post: the sweep owes the post and makes it once."""
    p, _, _, entered, _ = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (note,) = _notifications(p["id"])
    changed = {**entered, "value_text": "6.9", "is_critical": 1,
               "critical_limit_id": limit["id"]}
    with db_session(immediate=True) as db:
        assert lcs.raise_for_result_in_txn(
            db, changed, actor={"id": tech[0], "role": "lab_technician"}, preliminary=True,
            now=utc_iso_now(), trigger="tech_validated") == note["id"]
    assert _notifications(p["id"])[0]["delivery_status"] == "pending"
    assert lcs.sweep()["delivered"] >= 1
    last = _room_messages(p["room"])[-1]
    assert last["idempotency_key"] == f"lab-critical:{note['id']}:t1:r1"
    assert "now 6.9 mmol/L" in last["content"]
    lcs.sweep()
    assert len(_room_messages(p["room"])) == 2


# ── 6. A post after authorisation, and a callback from an out-of-date screen ──

def test_a_tier_posted_after_release_says_authorised_never_preliminary(
        lab_on, _catalogue, limit, tech, phleb, signer, ladder, pushes):
    """Release does not change the value already told, so nothing new is
    posted then; the next tier's post is written from the result as it now
    stands: authorised, by whom and when, never 'Preliminary'."""
    p, _, specimen_id, _, body = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (note,) = _notifications(p["id"])
    first = _room_messages(p["room"])[-1]
    assert "Preliminary, not yet authorised" in first["content"]
    released = _release(specimen_id, body["results"][0], signer[1])
    assert released["criticals"] == []
    lcs.sweep(now=_later(16))
    last = _room_messages(p["room"])[-1]
    assert last["idempotency_key"] == f"lab-critical:{note['id']}:t2"
    assert f"Authorised by Pathologist {signer[0]} at " in last["content"]
    assert "Critical potassium 7.0 mmol/L" in last["content"]
    for stale in ("Preliminary", "not yet authorised", "Validated by"):
        assert stale not in last["content"], stale
    view = _listed(tech[1], note["id"])
    assert view["result_now"]["status"] == "released"
    assert view["result_now"]["text"] == "Authorised and on the chart"


def test_a_tier_posted_before_release_still_says_preliminary(
        lab_on, _catalogue, limit, tech, phleb, ladder, pushes):
    p, *_ = _critical_validated(_catalogue, tech, phleb, value="7.0")
    lcs.sweep(now=_later(16))
    last = _room_messages(p["room"])[-1]
    assert f"Preliminary, not yet authorised. Validated by Lab_Technician {tech[0]}" \
        in last["content"]
    assert "Authorised" not in last["content"]


def test_a_callback_from_a_screen_that_showed_an_earlier_value_closes_nothing(
        lab_on, _catalogue, limit, tech, phleb, signer, no_ladder, pushes):
    """The screen loaded with 7.0; the result was then sent back, changed to
    4.5 and validated again, so the ward was sent a correction. A callback or
    an attempt naming 7.0 is refused 'value_changed' with nothing written,
    and only a callback naming the value now told closes it."""
    p, _, _, _, body = _critical_validated(_catalogue, tech, phleb, value="7.0")
    (note,) = _notifications(p["id"])
    shown = _listed(tech[1], note["id"])["value_told"]
    assert shown == "7.0 mmol/L"
    _rerun(body["results"][0], "4.5", tech, signer)
    events_before = len(_events(note["id"]))
    refused = _callback(note["id"], tech[1], value_told=shown, note="Read back 7.0")
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "value_changed"
    assert detail["value_told"] == "4.5 mmol/L" and detail["value_shown"] == "7.0 mmol/L"
    assert "4.5 mmol/L" in detail["message"] and "\u2014" not in detail["message"]
    attempt = client.post(f"/api/lab/criticals/{note['id']}/attempt",
                          json={"method": "phone", "note": "Ward phone rang out",
                                "value_told": shown}, headers=tech[1])
    assert attempt.status_code == 409 and attempt.json()["detail"]["error"] == "value_changed"
    (stored,) = _notifications(p["id"])
    assert (stored["status"], stored["acknowledged_at"], stored["read_back"]) == (
        "open", None, None)
    assert len(_events(note["id"])) == events_before
    with db_session() as db:
        logged = db.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = "
                            "'lab_critical_callback_logged' AND target_id = ?",
                            (note["id"],)).fetchone()[0]
    assert logged == 0
    # Naming no value is refused before anything is read.
    unnamed = {"recipient_name": "Sister Mary Thomas", "recipient_role": "nurse_in_charge",
               "method": "phone", "read_back": True}
    assert client.post(f"/api/lab/criticals/{note['id']}/callback", json=unnamed,
                       headers=tech[1]).status_code == 422
    assert client.post(f"/api/lab/criticals/{note['id']}/attempt",
                       json={"method": "phone", "note": "Ward phone rang out"},
                       headers=tech[1]).status_code == 422
    assert _callback(note["id"], tech[1], value_told="  ").json()["detail"]["error"] == \
        "value_told_required"
    assert _notifications(p["id"])[0]["status"] == "open"
    # The reloaded screen names the value now told, and the call closes it.
    ok = _callback(note["id"], tech[1], value_told="4.5 mmol/L",
                   note="Told the change, read back 4.5")
    assert ok.status_code == 200, ok.text
    with db_session() as db:
        audit = db.execute("SELECT summary_message, metadata_json FROM audit_logs WHERE "
                           "action_type = 'lab_critical_callback_logged' AND target_id = ?",
                           (note["id"],)).fetchall()
    assert len(audit) == 1 and "4.5 mmol/L" in audit[0][0]
    assert json.loads(audit[0][1])["value_told"] == "4.5 mmol/L"
