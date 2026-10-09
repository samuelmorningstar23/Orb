"""The OPD consulting queue and its actions (opd-8, W4-F, 4 October 2026).

What this file defends:

  1. FLAG OFF, NOTHING CHANGED. The new queue routes and /api/opd/my-queue are
     404 feature_disabled; the administrator still calls a patient in through
     the old front-desk rule; no nudge is pushed.
  2. THE STATE MACHINE. call (alias of start) counts the call and ends a skip;
     skip sends the patient back to waiting, marked; recall puts a skipped
     patient back in the line at their token; assign lets a clinic doctor take
     an unassigned patient (a new token only when theirs clashes); priority
     needs a reason unless normal; the queue is in seeing order (priority, then
     token). A repeat writes its own audit row marked replay.
  3. LWBS. A cancel with reason code left_without_being_seen is stored with
     its code and raises no charge.
  4. SCOPE AND PERMISSION. Another clinic's visit is 404; in OPD mode the
     administrator and the receptionist are 403 on call, start and complete.
  5. THE NUDGE. 'opd:queue_changed' carries the department and nothing else,
     to the clinic's room and the all-clinics room, only in OPD mode.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import opd_queue_service as oq

client = TestClient(app)

DEPT_A = "Queue Test Cardiology"
DEPT_B = "Queue Test Orthopaedics"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department, name=None):
    uid = f"QTU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"Queue {role} {uid[-4:]}", role, department),
        )
    return uid


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _outpatient():
    pid = f"QTP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, diagnosis) "
            "VALUES (?, ?, ?, 52, 'M', 'registered', 'Queue fixture')",
            (pid, f"Queue Patient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}"))
    return pid


@pytest.fixture(scope="module")
def staff():
    return {
        "doc_a": _mk_user("doctor", DEPT_A, "Dr Asha Queue"),
        "doc_a2": _mk_user("doctor", DEPT_A, "Dr Arun Queue"),
        "doc_b": _mk_user("doctor", DEPT_B, "Dr Bela Queue"),
        "nurse_a": _mk_user("nurse", DEPT_A),
        "recep_a": _mk_user("receptionist", DEPT_A),
        "admin": _mk_user("admin", None),
    }


@pytest.fixture
def opd_on(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    assert config.feature_enabled("opd")
    yield


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    assert not config.feature_enabled("opd")
    yield


@pytest.fixture
def nudges(monkeypatch):
    """Capture every nudge instead of scheduling it on the app's loop."""
    from ws import realtime

    seen: list = []
    monkeypatch.setattr(realtime, "schedule_on_loop",
                        lambda make_coro, what: seen.append((make_coro, what)) or True)
    return seen


def _stated_identity(pid):
    """What the patient states at the desk (opd-6a, ruling OPD-S1): with the
    registration feature on, every visit registered through the front desk
    carries it. Read from the fixture row: name, age and UHID."""
    from services import patient_matching as pm
    with db_session() as db:
        r = db.execute(
            "SELECT p.name, p.mrn, p.date_of_birth, p.age, p.created_at, d.age_recorded_on "
            "FROM patients p LEFT JOIN patient_demographics d ON d.patient_id = p.id "
            "WHERE p.id = ?", (pid,)).fetchone()
    if r is None:
        return {"name": "Not On File", "age_years": 40, "uhid": "NOT-ON-FILE"}
    age, _ = pm.current_age(date_of_birth=r["date_of_birth"], stated_age=r["age"],
                            age_recorded_on=r["age_recorded_on"], created_at=r["created_at"])
    return {"name": r["name"], "age_years": age, "uhid": r["mrn"]}


def _register(staff_uid, pid, *, doctor=None, role="doctor"):
    body = {"patient_id": pid, "identity_check": _stated_identity(pid)}
    if doctor:
        body["doctor_id"] = doctor
    resp = client.post("/api/frontdesk/opd/visits", json=body, headers=_auth(staff_uid, role))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _post(path, uid, role="doctor", body=None):
    return client.post(path, json=body, headers=_auth(uid, role))


def _audit_rows(action_type, target_id):
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action_type, target_id)).fetchall()
    return [dict(r) for r in rows]


def _meta(row):
    return json.loads(row["metadata_json"] or "{}")


def _charges(pid):
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM encounter_charges WHERE patient_id = ?",
                          (pid,)).fetchone()[0]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Flag off
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_the_queue_routes_do_not_exist(staff, opd_off, nudges):
    pid = _outpatient()
    visit = _register(staff["doc_a"], pid, doctor=staff["doc_a"])
    hdr = _auth(staff["doc_a"], "doctor")
    for action in ("call", "skip", "recall", "assign"):
        resp = client.post(f"/api/frontdesk/opd/visits/{visit['id']}/{action}", headers=hdr)
        assert resp.status_code == 404, (action, resp.text)
        assert resp.json()["detail"]["error"] == "feature_disabled"
    resp = client.patch(f"/api/frontdesk/opd/visits/{visit['id']}",
                        json={"room": "4"}, headers=hdr)
    assert resp.status_code == 404
    assert client.get("/api/opd/my-queue", headers=hdr).status_code == 404

    # The administrator still calls a patient in, exactly as before.
    resp = _post(f"/api/frontdesk/opd/visits/{visit['id']}/start", staff["admin"], "admin")
    assert resp.status_code == 200, resp.text
    assert "call_count" not in resp.json() and "priority" not in resp.json()
    queue = client.get("/api/frontdesk/opd/queue", headers=hdr).json()
    assert "poll_seconds" not in queue and "opd_mode" not in queue
    assert nudges == []


# ═════════════════════════════════════════════════════════════════════════════
# 2. The state machine
# ═════════════════════════════════════════════════════════════════════════════

def test_priority_needs_a_reason_and_orders_the_queue(staff, opd_on, nudges):
    doc = staff["doc_a"]
    first = _register(doc, _outpatient(), doctor=doc)
    second = _register(doc, _outpatient(), doctor=doc)
    third = _register(doc, _outpatient(), doctor=doc)
    hdr = _auth(doc, "doctor")

    no_reason = client.patch(f"/api/frontdesk/opd/visits/{third['id']}",
                             json={"priority": "emergency"}, headers=hdr)
    assert no_reason.status_code == 422, no_reason.text
    unknown = client.patch(f"/api/frontdesk/opd/visits/{third['id']}",
                           json={"priority": "vip", "priority_reason": "Board member"},
                           headers=hdr)
    assert unknown.status_code == 422
    resp = client.patch(
        f"/api/frontdesk/opd/visits/{third['id']}",
        json={"priority": "emergency", "priority_reason": "Chest pain at the counter",
              "room": "Room 2"},
        headers=_auth(staff["recep_a"], "receptionist"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["priority"] == "emergency" and body["priority_label"] == "Emergency"
    assert body["priority_reason"] == "Chest pain at the counter"
    assert body["room"] == "Room 2"
    senior = client.patch(f"/api/frontdesk/opd/visits/{second['id']}",
                          json={"priority": "senior_citizen", "priority_reason": "Aged 81"},
                          headers=hdr)
    assert senior.status_code == 200

    mine = client.get("/api/opd/my-queue", headers=hdr).json()
    order = [v["id"] for v in mine["waiting"]]
    assert order.index(third["id"]) < order.index(second["id"]) < order.index(first["id"])
    assert {p["code"] for p in mine["priorities"]} >= {"normal", "emergency"}
    for v in mine["waiting"]:
        assert "vitals" not in v and "news2" not in json.dumps(v).lower()

    # The desk queue is in the same seeing order, with its OPD-mode keys.
    queue = client.get("/api/frontdesk/opd/queue", headers=hdr).json()
    assert queue["opd_mode"] is True and queue["poll_seconds"] == config.OPD_QUEUE_POLL_SECONDS
    assert any(r["code"] == "left_without_being_seen" for r in queue["cancel_reasons"])
    mine_q = next(q for q in queue["queues"] if q["doctor_id"] == doc)
    ids = [v["id"] for v in mine_q["visits"]]
    assert ids.index(third["id"]) < ids.index(second["id"]) < ids.index(first["id"])

    # Back to normal clears the reason; a repeat changes nothing and says so.
    normal = client.patch(f"/api/frontdesk/opd/visits/{second['id']}",
                          json={"priority": "normal"}, headers=hdr)
    assert normal.json()["priority_reason"] is None
    again = client.patch(f"/api/frontdesk/opd/visits/{second['id']}",
                         json={"priority": "normal"}, headers=hdr)
    assert again.status_code == 200
    rows = _audit_rows("opd_visit_queue_updated", second["id"])
    assert [_meta(r)["replay"] for r in rows] == [False, False, True]
    assert all(_meta(r)["written"] == "in_transaction" for r in rows)


def test_call_skip_recall_and_call_again(staff, opd_on, nudges):
    doc = staff["doc_a"]
    pid = _outpatient()
    visit = _register(doc, pid, doctor=doc)
    vid = visit["id"]

    called = _post(f"/api/frontdesk/opd/visits/{vid}/call", doc)
    assert called.status_code == 200, called.text
    assert called.json()["status"] == "in_consultation" and called.json()["call_count"] == 1
    mine = client.get("/api/opd/my-queue", headers=_auth(doc, "doctor")).json()
    assert vid in [v["id"] for v in mine["current"]]
    assert vid not in [v["id"] for v in mine["waiting"] + mine["skipped"]]

    skipped = _post(f"/api/frontdesk/opd/visits/{vid}/skip", doc)
    assert skipped.status_code == 200, skipped.text
    body = skipped.json()
    assert body["status"] == "waiting" and body["skipped"] is True and body["called_at"] is None
    mine = client.get("/api/opd/my-queue", headers=_auth(doc, "doctor")).json()
    assert vid in [v["id"] for v in mine["skipped"]]
    assert vid not in [v["id"] for v in mine["waiting"]]
    assert _post(f"/api/frontdesk/opd/visits/{vid}/skip", doc).status_code == 200

    recalled = _post(f"/api/frontdesk/opd/visits/{vid}/recall", doc)
    assert recalled.status_code == 200 and recalled.json()["skipped"] is False
    assert _post(f"/api/frontdesk/opd/visits/{vid}/recall", doc).status_code == 200

    # Calling a skipped patient in directly ends the skip and counts the call.
    _post(f"/api/frontdesk/opd/visits/{vid}/skip", doc)
    again = _post(f"/api/frontdesk/opd/visits/{vid}/call", doc)
    assert again.json()["status"] == "in_consultation"
    assert again.json()["skipped"] is False and again.json()["call_count"] == 2

    skips = _audit_rows("opd_visit_skipped", vid)
    assert [_meta(r)["replay"] for r in skips] == [False, True, False]
    recalls = _audit_rows("opd_visit_recalled", vid)
    assert [_meta(r)["replay"] for r in recalls] == [False, True]
    assert len(_audit_rows("opd_consultation_started", vid)) == 2

    # A finished visit refuses every queue action.
    done = _post(f"/api/frontdesk/opd/visits/{vid}/complete", doc, body={})
    assert done.status_code == 200, done.text
    for action in ("skip", "recall"):
        resp = _post(f"/api/frontdesk/opd/visits/{vid}/{action}", doc)
        assert resp.status_code == 409, (action, resp.text)
    assert client.patch(f"/api/frontdesk/opd/visits/{vid}", json={"room": "9"},
                        headers=_auth(doc, "doctor")).status_code == 409


def test_a_doctor_takes_an_unassigned_patient(staff, opd_on, nudges):
    doc, doc2 = staff["doc_a"], staff["doc_a2"]
    own = _register(doc2, _outpatient(), doctor=doc2)
    unassigned = _register(staff["recep_a"], _outpatient(), role="receptionist")
    assert unassigned["doctor_id"] is None
    mine = client.get("/api/opd/my-queue", headers=_auth(doc2, "doctor")).json()
    assert unassigned["id"] in [v["id"] for v in mine["unassigned"]]

    taken = _post(f"/api/frontdesk/opd/visits/{unassigned['id']}/assign", doc2)
    assert taken.status_code == 200, taken.text
    assert taken.json()["doctor_id"] == doc2
    assert taken.json()["doctor_name"] == "Dr Arun Queue"
    with db_session() as db:
        holders = db.execute(
            "SELECT COUNT(*) FROM opd_visits WHERE doctor_id = ? AND visit_date = ? "
            "AND token_number = ? AND status != 'cancelled'",
            (doc2, own["visit_date"], taken.json()["token_number"])).fetchone()[0]
    assert holders == 1, "two of doctor 2's patients hold one token"
    rows = _audit_rows("opd_visit_assigned", unassigned["id"])
    assert _meta(rows[-1])["doctor_id"] == doc2
    repeat = _post(f"/api/frontdesk/opd/visits/{unassigned['id']}/assign", doc2)
    assert repeat.status_code == 200
    assert _meta(_audit_rows("opd_visit_assigned", unassigned["id"])[-1])["replay"] is True
    # Already another doctor's patient.
    resp = _post(f"/api/frontdesk/opd/visits/{unassigned['id']}/assign", doc)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "already_assigned"


def test_assign_gives_a_new_token_when_the_doctors_series_holds_it(staff, opd_on, nudges):
    doc = _mk_user("doctor", DEPT_A, "Dr Token Clash")
    mine = _register(doc, _outpatient(), doctor=doc)
    loose = _register(staff["recep_a"], _outpatient(), role="receptionist")
    with db_session() as db:
        # Put the unassigned visit on the doctor's own token number.
        db.execute("UPDATE opd_visits SET token_number = ? WHERE id = ?",
                   (mine["token_number"], loose["id"]))
    taken = _post(f"/api/frontdesk/opd/visits/{loose['id']}/assign", doc)
    assert taken.status_code == 200, taken.text
    assert taken.json()["token_number"] != mine["token_number"]
    meta = _meta(_audit_rows("opd_visit_assigned", loose["id"])[-1])
    assert meta["token_changed"] is True
    assert meta["previous_token_number"] == mine["token_number"]
    assert taken.json()["token_changed"] is True
    assert taken.json()["previous_token_number"] == mine["token_number"]


def test_a_take_that_reissues_the_token_says_so_everywhere(staff, opd_on, nudges):
    """The common case, not an edge: the unassigned line and every doctor's
    line each number from 1, so a doctor holding their own token 1 who takes
    the desk's token 1 gets token 2 for that patient. The slip in the
    patient's hand says 1, and 1 in that doctor's line is someone else. The
    take's answer, every later answer, the doctor's queue and the desk's
    queue all say the slip's number is no longer this patient's."""
    dept = f"Queue Reissue {uuid.uuid4().hex[:6]}"
    doc = _mk_user("doctor", dept, "Dr Rhea Reissue")
    desk = _mk_user("receptionist", dept)
    own = _register(doc, _outpatient(), doctor=doc)
    loose = _register(desk, _outpatient(), role="receptionist")
    assert own["token_number"] == 1 and loose["token_number"] == 1
    assert loose["doctor_id"] is None
    assert loose["previous_token_number"] is None

    taken = _post(f"/api/frontdesk/opd/visits/{loose['id']}/assign", doc)
    assert taken.status_code == 200, taken.text
    body = taken.json()
    assert body["token_number"] == 2
    assert body["token_changed"] is True
    assert body["previous_token_number"] == 1
    meta = _meta(_audit_rows("opd_visit_assigned", loose["id"])[-1])
    assert (meta["token_changed"], meta["previous_token_number"], meta["token_number"]) == (True, 1, 2)

    # A repeat changes nothing, and still says what the slip shows.
    again = _post(f"/api/frontdesk/opd/visits/{loose['id']}/assign", doc).json()
    assert (again["token_number"], again["token_changed"], again["previous_token_number"]) == (2, False, 1)
    assert _meta(_audit_rows("opd_visit_assigned", loose["id"])[-1])["replay"] is True

    # The doctor's queue: the taken patient carries the old token, their own does not.
    mine = client.get("/api/opd/my-queue", headers=_auth(doc, "doctor")).json()
    waiting = {v["id"]: v for v in mine["waiting"]}
    assert (waiting[loose["id"]]["token_number"], waiting[loose["id"]]["previous_token_number"]) == (2, 1)
    assert (waiting[own["id"]]["token_number"], waiting[own["id"]]["previous_token_number"]) == (1, None)

    # The desk's queue says the same.
    desk_q = client.get("/api/frontdesk/opd/queue", headers=_auth(desk, "receptionist"))
    assert desk_q.status_code == 200, desk_q.text
    seen = {v["id"]: v for q in desk_q.json()["queues"] for v in q["visits"]}
    assert (seen[loose["id"]]["token_number"], seen[loose["id"]]["previous_token_number"]) == (2, 1)
    assert seen[own["id"]]["previous_token_number"] is None

    # Every later answer about the visit carries it too.
    called = _post(f"/api/frontdesk/opd/visits/{loose['id']}/call", doc)
    assert called.status_code == 200, called.text
    assert (called.json()["token_number"], called.json()["previous_token_number"]) == (2, 1)


def test_a_take_that_keeps_the_token_says_nothing_changed(staff, opd_on, nudges):
    dept = f"Queue Keep {uuid.uuid4().hex[:6]}"
    doc = _mk_user("doctor", dept, "Dr Kiran Keep")
    desk = _mk_user("receptionist", dept)
    loose = _register(desk, _outpatient(), role="receptionist")
    taken = _post(f"/api/frontdesk/opd/visits/{loose['id']}/assign", doc)
    assert taken.status_code == 200, taken.text
    body = taken.json()
    assert (body["token_number"], body["token_changed"], body["previous_token_number"]) == (
        loose["token_number"], False, None)
    mine = client.get("/api/opd/my-queue", headers=_auth(doc, "doctor")).json()
    assert [v["previous_token_number"] for v in mine["waiting"]] == [None]


def test_reissued_tokens_reads_only_a_token_change(staff, opd_on):
    """A replay row, a row that says nothing changed, a non-number token and
    another action's row never read as a changed token. Rows go in through
    the audit service, so the hash chain stays whole."""
    from services.audit_service import log_audit_event

    vid = f"OPD-RT{uuid.uuid4().hex[:6].upper()}"
    other = f"OPD-RT{uuid.uuid4().hex[:6].upper()}"
    rows = (
        (vid, oq.ASSIGN_AUDIT_ACTION, {"replay": True}),
        (other, oq.ASSIGN_AUDIT_ACTION, {"replay": False, "token_changed": False}),
        (other, oq.ASSIGN_AUDIT_ACTION, {"token_changed": True, "previous_token_number": "3"}),
        (other, "opd_visit_skipped", {"token_changed": True, "previous_token_number": 5}),
        (vid, oq.ASSIGN_AUDIT_ACTION,
         {"replay": False, "token_changed": True, "previous_token_number": 4, "token_number": 9}),
    )
    with db_session() as db:
        for target, action, meta in rows:
            log_audit_event(
                correlation_id=f"{oq.ASSIGN_AUDIT_PREFIX}-{target}",
                event_category="clinical_record", action_type=action, outcome="success",
                db=db, target_type="opd_visit", target_id=target, metadata=meta)
        assert oq.reissued_tokens(db, [vid, other, None, vid]) == {vid: 4}
        assert oq.reissued_tokens(db, []) == {}


def _lines(department, visit_date):
    """{doctor id, or None for the unassigned line: [token, ...]} for one
    clinic's day, cancelled visits included."""
    with db_session() as db:
        rows = db.execute(
            "SELECT doctor_id, token_number FROM opd_visits "
            "WHERE department = ? AND visit_date = ? ORDER BY token_number, id",
            (department, visit_date)).fetchall()
    lines: dict = {}
    for r in rows:
        lines.setdefault(r["doctor_id"], []).append(r["token_number"])
    return lines


def test_a_kept_take_never_hands_its_number_to_the_next_walk_in(staff, opd_on, nudges):
    """The desk's unassigned line gives tokens 1 and 2 and a doctor takes 2,
    keeping it. That patient still holds a slip saying 2, so the desk's next
    unassigned walk-in must not be given 2 as well: two people in one clinic
    answering to one number is a wrong-patient path (the doctor calls 2 and
    opens one record while the other patient walks in)."""
    dept = f"Queue Spent {uuid.uuid4().hex[:6]}"
    doc = _mk_user("doctor", dept, "Dr Sona Spent")
    desk = _mk_user("receptionist", dept)
    first = _register(desk, _outpatient(), role="receptionist")
    second = _register(desk, _outpatient(), role="receptionist")
    assert (first["token_number"], second["token_number"]) == (1, 2)

    taken = _post(f"/api/frontdesk/opd/visits/{second['id']}/assign", doc)
    assert taken.status_code == 200, taken.text
    assert (taken.json()["token_number"], taken.json()["token_changed"]) == (2, False)

    walk_in = _register(desk, _outpatient(), role="receptionist")
    assert walk_in["doctor_id"] is None
    assert walk_in["token_number"] == 3, "the next walk-in was given the taken patient's number"
    assert _lines(dept, second["visit_date"]) == {None: [1, 3], doc: [2]}
    meta = _meta(_audit_rows("opd_visit_assigned", second["id"])[-1])
    assert meta["unassigned_token_number"] == 2


def test_a_reissued_take_keeps_the_slip_number_spent_too(staff, opd_on, nudges):
    """The same hole when the take issues a new token: the slip says 2, the
    unassigned line no longer holds a 2, and the next walk-in must still not
    be given one."""
    dept = f"Queue Spent {uuid.uuid4().hex[:6]}"
    doc = _mk_user("doctor", dept, "Dr Tara Twice")
    desk = _mk_user("receptionist", dept)
    _register(doc, _outpatient(), doctor=doc)
    _register(doc, _outpatient(), doctor=doc)
    _register(desk, _outpatient(), role="receptionist")
    loose = _register(desk, _outpatient(), role="receptionist")
    assert loose["token_number"] == 2

    taken = _post(f"/api/frontdesk/opd/visits/{loose['id']}/assign", doc)
    assert taken.status_code == 200, taken.text
    body = taken.json()
    assert (body["token_number"], body["token_changed"], body["previous_token_number"]) == (3, True, 2)

    walk_in = _register(desk, _outpatient(), role="receptionist")
    assert walk_in["token_number"] == 3, "the next walk-in was given the taken patient's number"
    assert _lines(dept, loose["visit_date"]) == {None: [1, 3], doc: [1, 2, 3]}
    assert _meta(_audit_rows("opd_visit_assigned", loose["id"])[-1])["unassigned_token_number"] == 2


def test_a_take_never_lands_on_a_number_the_doctors_line_cancelled(staff, opd_on, nudges):
    """The doctor's token 1 left without being seen. A take of the desk's
    token 1 must not hand that doctor's line a second 1 the same morning:
    _next_token counts cancelled visits for exactly this reason, and the
    take's clash check now does the same."""
    dept = f"Queue Gone {uuid.uuid4().hex[:6]}"
    doc = _mk_user("doctor", dept, "Dr Gita Gone")
    desk = _mk_user("receptionist", dept)
    gone = _register(doc, _outpatient(), doctor=doc)
    cancelled = client.post(
        f"/api/frontdesk/opd/visits/{gone['id']}/cancel",
        json={"reason": "Left without being seen", "reason_code": "left_without_being_seen"},
        headers=_auth(desk, "receptionist"))
    assert cancelled.status_code == 200, cancelled.text
    loose = _register(desk, _outpatient(), role="receptionist")
    assert (gone["token_number"], loose["token_number"]) == (1, 1)

    taken = _post(f"/api/frontdesk/opd/visits/{loose['id']}/assign", doc)
    assert taken.status_code == 200, taken.text
    body = taken.json()
    assert (body["token_number"], body["token_changed"], body["previous_token_number"]) == (2, True, 1)

    booked = _register(desk, _outpatient(), doctor=doc, role="receptionist")
    assert booked["token_number"] == 3
    walk_in = _register(desk, _outpatient(), role="receptionist")
    assert walk_in["token_number"] == 2, "the taken patient's slip number was issued again"
    assert _lines(dept, loose["visit_date"]) == {None: [2], doc: [1, 2, 3]}


def test_a_take_with_no_audit_row_is_refused_and_changes_nothing(staff, opd_on):
    """The unassigned line reads the numbers takes moved out of it back from
    the take's audit row, so a take that writes none would reopen the reuse
    above. It is refused and rolls back."""
    from services.audit_service import AuditWriteError

    dept = f"Queue Unrecorded {uuid.uuid4().hex[:6]}"
    doc = _mk_user("doctor", dept, "Dr Uma Unrecorded")
    desk = _mk_user("receptionist", dept)
    loose = _register(desk, _outpatient(), role="receptionist")
    with pytest.raises(AuditWriteError):
        oq.assign_visit(loose["id"], doctor_id=doc)
    with pytest.raises(AuditWriteError):
        oq.assign_visit(loose["id"], doctor_id=doc, on_done=lambda db, visit, facts: None)
    with db_session() as db:
        row = db.execute("SELECT doctor_id, doctor_name, token_number FROM opd_visits "
                         "WHERE id = ?", (loose["id"],)).fetchone()
    assert tuple(row) == (None, None, 1)
    assert _audit_rows("opd_visit_assigned", loose["id"]) == []


def test_taken_unassigned_top_reads_each_takes_slip_number(staff, opd_on):
    """What the unassigned line counts as spent, per kind of take row: the
    recorded unassigned_token_number; previous_token_number on a reissue
    recorded before that field existed; a kept take's own token. A replay
    row, another action's row, a visit booked straight to a doctor and an
    unassigned visit spend nothing. Rows go in through the audit service, so
    the hash chain stays whole."""
    from services.audit_service import log_audit_event
    from services.frontdesk_service import _next_token

    day = "2031-01-15"
    tag = uuid.uuid4().hex[:6]
    depts = {k: f"Queue Top {k} {tag}" for k in ("recorded", "older", "kept", "none")}
    doc = staff["doc_a"]
    visits = (
        # (department key, doctor, token now, [(action, metadata), ...])
        ("recorded", doc, 9, [
            (oq.ASSIGN_AUDIT_ACTION, {"replay": False, "token_changed": True,
                                      "previous_token_number": 4, "token_number": 9,
                                      "unassigned_token_number": 4}),
            (oq.ASSIGN_AUDIT_ACTION, {"replay": True})]),
        ("older", doc, 9, [
            (oq.ASSIGN_AUDIT_ACTION, {"replay": False, "token_changed": True,
                                      "previous_token_number": 4, "token_number": 9})]),
        ("kept", doc, 6, [
            (oq.ASSIGN_AUDIT_ACTION, {"replay": False, "token_changed": False})]),
        ("none", doc, 6, [(oq.ASSIGN_AUDIT_ACTION, {"replay": True})]),
        ("none", doc, 7, [("opd_visit_skipped", {"replay": False})]),
        ("none", doc, 8, []),
        ("none", None, 1, []),
    )
    with db_session() as db:
        for key, doctor, token, rows in visits:
            vid = f"OPD-TT{uuid.uuid4().hex[:6].upper()}"
            db.execute(
                "INSERT INTO opd_visits (id, patient_id, department, doctor_id, visit_date, "
                "token_number, registered_at) VALUES (?, 'QTP-TOP', ?, ?, ?, ?, ?)",
                (vid, depts[key], doctor, day, token, f"{day}T03:00:00+00:00"))
            for action, meta in rows:
                log_audit_event(
                    correlation_id=f"{oq.ASSIGN_AUDIT_PREFIX}-{vid}",
                    event_category="clinical_record", action_type=action, outcome="success",
                    db=db, target_type="opd_visit", target_id=vid, metadata=meta)
        tops = {k: oq.taken_unassigned_top(db, visit_date=day, department=d)
                for k, d in depts.items()}
        assert tops == {"recorded": 4, "older": 4, "kept": 6, "none": 0}
        assert oq.taken_unassigned_top(db, visit_date="2031-01-16",
                                       department=depts["recorded"]) == 0
        # The unassigned line steps over a spent number; a doctor's line is untouched.
        assert _next_token(db, visit_date=day, department=depts["older"], doctor_id=None) == 5
        assert _next_token(db, visit_date=day, department=depts["older"], doctor_id="") == 5
        assert _next_token(db, visit_date=day, department=depts["older"], doctor_id=doc) == 10
        assert _next_token(db, visit_date=day, department=depts["none"], doctor_id=None) == 2


def test_flag_off_unassigned_numbering_is_unchanged(staff, opd_off):
    """No take can happen with the feature off, so the unassigned line numbers
    exactly as before: 1, 2, 3, a cancelled number never reissued."""
    dept = f"Queue Off {uuid.uuid4().hex[:6]}"
    doc = _mk_user("doctor", dept, "Dr Omar Off")
    tokens = []
    for n in range(3):
        visit = _register(doc, _outpatient())
        assert visit["doctor_id"] is None
        tokens.append(visit["token_number"])
        if n == 1:
            resp = client.post(f"/api/frontdesk/opd/visits/{visit['id']}/cancel",
                               json={"reason": "Registered in error"},
                               headers=_auth(doc, "doctor"))
            assert resp.status_code == 200, resp.text
    assert tokens == [1, 2, 3]


def test_flag_off_desk_queue_carries_no_token_change_field(staff, opd_off):
    doc = staff["doc_a"]
    _register(doc, _outpatient(), doctor=doc)
    resp = client.get("/api/frontdesk/opd/queue", headers=_auth(doc, "doctor"))
    assert resp.status_code == 200, resp.text
    visits = [v for q in resp.json()["queues"] for v in q["visits"]]
    assert visits
    for v in visits:
        assert "previous_token_number" not in v and "token_changed" not in v


# ═════════════════════════════════════════════════════════════════════════════
# 3. Left without being seen
# ═════════════════════════════════════════════════════════════════════════════

def test_lwbs_is_a_coded_cancel_with_no_charge(staff, opd_on, nudges):
    pid = _outpatient()
    visit = _register(staff["recep_a"], pid, role="receptionist")
    hdr = _auth(staff["recep_a"], "receptionist")
    bad = client.post(f"/api/frontdesk/opd/visits/{visit['id']}/cancel",
                      json={"reason": "Gone", "reason_code": "bored"}, headers=hdr)
    assert bad.status_code == 422
    resp = client.post(f"/api/frontdesk/opd/visits/{visit['id']}/cancel",
                       json={"reason": "Left without being seen",
                             "reason_code": "left_without_being_seen"}, headers=hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["cancel_reason_code"] == "left_without_being_seen"
    assert body["cancel_reason_label"] == "Left without being seen"
    assert _charges(pid) == 0
    with db_session() as db:
        stored = db.execute("SELECT cancel_reason_code, consultation_charge_id FROM opd_visits "
                            "WHERE id = ?", (visit["id"],)).fetchone()
    assert tuple(stored) == ("left_without_being_seen", None)
    rows = _audit_rows("opd_visit_cancelled", visit["id"])
    assert _meta(rows[-1])["reason_code"] == "left_without_being_seen"


# ═════════════════════════════════════════════════════════════════════════════
# 4. Scope and permission
# ═════════════════════════════════════════════════════════════════════════════

def test_another_clinics_visit_is_404(staff, opd_on, nudges):
    visit = _register(staff["doc_a"], _outpatient(), doctor=staff["doc_a"])
    other = staff["doc_b"]
    for action in ("call", "skip", "recall", "assign", "start", "complete"):
        resp = _post(f"/api/frontdesk/opd/visits/{visit['id']}/{action}", other, body={})
        assert resp.status_code == 404, (action, resp.text)
    resp = client.patch(f"/api/frontdesk/opd/visits/{visit['id']}",
                        json={"room": "1"}, headers=_auth(other, "doctor"))
    assert resp.status_code == 404
    mine = client.get("/api/opd/my-queue", headers=_auth(other, "doctor")).json()
    assert visit["id"] not in json.dumps(mine)


@pytest.mark.parametrize("who", ["admin", "recep_a"])
def test_admin_and_receptionist_cannot_run_a_consultation(staff, opd_on, nudges, who):
    role = "admin" if who == "admin" else "receptionist"
    visit = _register(staff["doc_a"], _outpatient(), doctor=staff["doc_a"])
    for action in ("call", "start", "complete", "skip", "recall", "assign"):
        resp = _post(f"/api/frontdesk/opd/visits/{visit['id']}/{action}", staff[who], role,
                     body={})
        assert resp.status_code == 403, (action, resp.text)
    with db_session() as db:
        status = db.execute("SELECT status FROM opd_visits WHERE id = ?",
                            (visit["id"],)).fetchone()[0]
    assert status == "waiting"
    assert client.get("/api/opd/my-queue", headers=_auth(staff[who], role)).status_code == 403


def test_a_nurse_cannot_call_a_patient_in(staff, opd_on, nudges):
    visit = _register(staff["doc_a"], _outpatient(), doctor=staff["doc_a"])
    assert _post(f"/api/frontdesk/opd/visits/{visit['id']}/call",
                 staff["nurse_a"], "nurse").status_code == 403


def test_the_doctor_queue_read_is_audited_when_it_shows_anyone(staff, opd_on, nudges):
    lonely = _mk_user("doctor", f"Queue Empty Clinic {uuid.uuid4().hex[:4]}")
    with db_session() as db:
        before = db.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = "
                            "'opd_my_queue_viewed' AND actor_user_id = ?", (lonely,)).fetchone()[0]
    empty = client.get("/api/opd/my-queue", headers=_auth(lonely, "doctor"))
    assert empty.status_code == 200 and empty.json()["total"] == 0
    doc = staff["doc_a"]
    _register(doc, _outpatient(), doctor=doc)
    client.get("/api/opd/my-queue", headers=_auth(doc, "doctor"))
    with db_session() as db:
        after_lonely = db.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = "
                                  "'opd_my_queue_viewed' AND actor_user_id = ?",
                                  (lonely,)).fetchone()[0]
        row = db.execute("SELECT * FROM audit_logs WHERE action_type = 'opd_my_queue_viewed' "
                         "AND actor_user_id = ? ORDER BY id DESC LIMIT 1", (doc,)).fetchone()
    assert after_lonely == before
    assert row is not None and row["event_category"] == "phi_access"
    assert _meta(row)["visit_ids"]


# ═════════════════════════════════════════════════════════════════════════════
# 5. The content-free nudge
# ═════════════════════════════════════════════════════════════════════════════

def test_the_nudge_carries_only_the_department(staff, opd_on, nudges, monkeypatch):
    from ws import realtime

    doc = staff["doc_a"]
    visit = _register(doc, _outpatient(), doctor=doc)
    _post(f"/api/frontdesk/opd/visits/{visit['id']}/call", doc)
    assert len(nudges) == 2, "register and call each nudge once"

    emitted: list = []

    async def fake_emit(event, payload, room=None, **kw):
        emitted.append((event, payload, room))

    monkeypatch.setattr(realtime.sio, "emit", fake_emit)
    for make_coro, what in nudges:
        asyncio.run(make_coro())
        assert DEPT_A in what
    assert emitted
    for event, payload, room in emitted:
        assert event == oq.QUEUE_NUDGE_EVENT == "opd:queue_changed"
        assert set(payload) == {"department"} and payload["department"] == DEPT_A
        # opd-9b: one emit to the clinic's rooms, the doctors' vitals rooms and
        # the desk's opd rooms, so a socket in two of them hears it once.
        assert room == [f"dept:{DEPT_A}", "dept:__all__", f"opd:{DEPT_A}", "opd:__all__"]
    assert oq.queue_nudge_payload("X") == {"department": "X"}


def test_no_nudge_when_the_feature_is_off(opd_off, nudges):
    assert oq.nudge_queue_changed(DEPT_A) is False
    assert nudges == []


def test_the_skip_and_recall_services_need_an_open_visit(staff, opd_on, nudges):
    from services.frontdesk_service import FrontDeskError

    with pytest.raises(FrontDeskError):
        oq.skip_visit("OPD-NOSUCH1")
    visit = _register(staff["doc_a"], _outpatient(), doctor=staff["doc_a"])
    _post(f"/api/frontdesk/opd/visits/{visit['id']}/cancel", staff["doc_a"],
          body={"reason": "Registered in error", "reason_code": "registered_in_error"})
    with pytest.raises(oq.QueueConflict):
        oq.skip_visit(visit["id"])
    with pytest.raises(oq.QueueConflict):
        oq.recall_visit(visit["id"])


def test_priority_ranks_emergency_first_and_normal_last():
    assert oq.priority_rank("emergency") < oq.priority_rank("pregnant") < oq.priority_rank("normal")
    assert oq.priority_rank(None) == oq.priority_rank("normal")
    now = datetime.now(timezone.utc).isoformat()
    visits = [
        {"id": "a", "status": "waiting", "priority": "normal", "token_number": 1},
        {"id": "b", "status": "waiting", "priority": "senior_citizen", "token_number": 5},
        {"id": "c", "status": "waiting", "priority": "emergency", "token_number": 9},
        {"id": "d", "status": "in_consultation", "priority": "normal", "token_number": 7},
        {"id": "e", "status": "waiting", "priority": "emergency", "token_number": 2,
         "skipped_at": now},
    ]
    assert [v["id"] for v in sorted(visits, key=oq.queue_sort_key)] == ["d", "c", "b", "a", "e"]
