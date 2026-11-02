"""Clinic sessions and availability (opd-15, W2-E, 3 October 2026).

What this file defends:

  1. THE SWITCH. Every /api/schedule route answers 404 while ORB_REGISTRATION is
     off (after a 401 for no session), so a site that never turns the
     registration desk on has no clinic timetable anywhere.
  2. WHO MAY CHANGE IT. Only schedule.manage (the administrator) writes. The
     desk (registration.read or patient.read_all) reads, a pharmacist or a
     cashier reads nothing. Every change is audited as configuration, in the
     transaction that makes it, with a string target id.
  3. SLOTS ARE COMPUTED IN HOSPITAL-LOCAL TIME and never stored. A weekly
     session yields its slots on its weekday; at 00:30 IST on a Monday "today"
     is Monday, not the UTC Sunday.
  4. AN EXCEPTION REMOVES THEM, and withdrawing it brings them back.
  5. ONE DOCTOR, ONE PLACE. A session overlapping another active session of the
     same doctor is refused, on create and on edit.
  6. MIGRATION 57 is additive, idempotent and runs through the runner on a
     database that has never seen it.
"""
from __future__ import annotations

import json
import sqlite3
from db_backend import DBIntegrityError
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import clinic_schedule_service as schedule
from services import hospital_time

client = TestClient(app)

IST = ZoneInfo("Asia/Kolkata")
CLINIC = "Schedule Test Cardiology"
OTHER = "Schedule Test Oncology"

# Monday 5 October 2026 and the Sunday before it.
MONDAY = date(2026, 10, 5)
SUNDAY = date(2026, 10, 4)
assert MONDAY.weekday() == 0 and SUNDAY.weekday() == 6


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department=None, *, name=None, active=1):
    uid = f"CS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, ?)",
            (uid, name or f"Schedule {role} {uid[-4:]}", role, department, active),
        )
    return uid


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _hdr(role, department=None):
    return _auth(_mk_user(role, department), role)


@pytest.fixture
def ist(monkeypatch):
    monkeypatch.setattr(hospital_time, "hospital_timezone", lambda: IST)
    yield


@pytest.fixture
def desk_on(monkeypatch, ist):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    yield


@pytest.fixture
def admin():
    return _hdr("admin")


@pytest.fixture
def doctor():
    return _mk_user("doctor", CLINIC, name=f"Dr Schedule {uuid.uuid4().hex[:4]}")


def _session(admin, doctor_id, **over):
    body = {"doctor_id": doctor_id, "weekday": 0, "start_time": "09:00",
            "end_time": "10:00", "slot_minutes": 15, "room": "Room 4"}
    body.update(over)
    return client.post("/api/schedule/sessions", json=body, headers=admin)


def _availability(headers, day=None, **params):
    if day is not None:
        params["date"] = day.isoformat()
    return client.get("/api/schedule/availability", params=params, headers=headers)


def _sessions_for(body, doctor_id):
    return [s for d in body["days"] for s in d["sessions"] if s["doctor_id"] == doctor_id]


def _audit_rows(target_id):
    with db_session() as db:
        rows = db.execute(
            "SELECT event_category, action_type, actor_role, target_id, metadata_json "
            "FROM audit_logs WHERE target_id = ? ORDER BY id", (target_id,)).fetchall()
    return [dict(r) for r in rows]


# ── 1. The switch ─────────────────────────────────────────────────────────────

ROUTES = [
    ("get", "/api/schedule/doctors", None),
    ("get", "/api/schedule/sessions", None),
    ("post", "/api/schedule/sessions", {"doctor_id": "X", "weekday": 0, "start_time": "09:00",
                                        "end_time": "10:00", "slot_minutes": 15}),
    ("get", "/api/schedule/sessions/CSNOPE", None),
    ("patch", "/api/schedule/sessions/CSNOPE", {"row_version": 1}),
    ("post", "/api/schedule/sessions/CSNOPE/retire", {"reason": "x"}),
    ("get", "/api/schedule/exceptions", None),
    ("post", "/api/schedule/exceptions", {"exception_date": "2026-10-05", "reason": "x"}),
    ("post", "/api/schedule/exceptions/CXNOPE/withdraw", {"reason": "x"}),
    ("get", "/api/schedule/availability", None),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_route_is_404_with_the_switch_off(method, path, body, admin):
    kwargs = {"headers": admin}
    if body is not None:
        kwargs["json"] = body
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 404, (path, r.text)
    assert r.json()["detail"]["error"] == "feature_disabled"


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_route_is_401_without_a_session(method, path, body, monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    kwargs = {} if body is None else {"json": body}
    assert getattr(client, method)(path, **kwargs).status_code == 401


def test_the_switch_off_leaves_no_slot_reader_reachable(admin, doctor, monkeypatch):
    """A session written while the switch was on is unreachable once it is off."""
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    assert _session(admin, doctor).status_code == 200
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    assert client.get("/api/schedule/sessions", headers=admin).status_code == 404


# ── 2. Who may change it ──────────────────────────────────────────────────────

@pytest.mark.parametrize("role,dept", [
    ("receptionist", None), ("doctor", CLINIC), ("nurse", CLINIC), ("pharmacist", None),
    ("cashier", None),
])
def test_only_schedule_manage_writes(desk_on, role, dept, doctor):
    r = _session(_hdr(role, dept), doctor)
    assert r.status_code == 403, (role, r.text)
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM clinic_sessions WHERE doctor_id = ?",
                          (doctor,)).fetchone()[0] == 0


@pytest.mark.parametrize("role,dept,status", [
    ("receptionist", None, 200), ("receptionist", CLINIC, 200), ("admin", None, 200),
    ("doctor", CLINIC, 200), ("nurse", CLINIC, 200),
    ("pharmacist", None, 403), ("cashier", None, 403), ("lab_technician", None, 403),
])
def test_the_desk_reads_and_nobody_else(desk_on, role, dept, status):
    headers = _hdr(role, dept)
    for path in ("/api/schedule/sessions", "/api/schedule/availability",
                 "/api/schedule/exceptions", "/api/schedule/doctors"):
        assert client.get(path, headers=headers).status_code == status, (role, path)


def test_a_refused_read_is_audited(desk_on):
    uid = _mk_user("pharmacist")
    r = client.get("/api/schedule/availability", headers=_auth(uid, "pharmacist"))
    assert r.status_code == 403
    with db_session() as db:
        row = db.execute(
            "SELECT event_category, metadata_json FROM audit_logs WHERE actor_user_id = ? "
            "AND action_type = 'permission_denied' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    assert row["event_category"] == "authorization"
    assert json.loads(row["metadata_json"])["desk_mode"] == "read"


def test_every_change_is_audited_as_configuration(desk_on, admin, doctor):
    created = _session(admin, doctor).json()
    sid = created["id"]
    assert isinstance(sid, str)
    r = client.patch(f"/api/schedule/sessions/{sid}", headers=admin,
                     json={"row_version": created["row_version"], "room": "Room 9"})
    assert r.status_code == 200, r.text
    exc = client.post("/api/schedule/exceptions", headers=admin, json={
        "session_id": sid, "exception_date": MONDAY.isoformat(), "reason": "Annual leave"}).json()
    assert client.post(f"/api/schedule/exceptions/{exc['id']}/withdraw", headers=admin,
                       json={"reason": "Leave cancelled"}).status_code == 200
    assert client.post(f"/api/schedule/sessions/{sid}/retire", headers=admin,
                       json={"reason": "Clinic moved"}).status_code == 200

    rows = _audit_rows(sid)
    config_rows = [r for r in rows if r["event_category"] == "configuration"]
    assert [r["action_type"] for r in config_rows] == [
        "clinic_session_created", "clinic_session_updated", "clinic_session_retired"]
    assert all(r["actor_role"] == "admin" and r["target_id"] == sid for r in config_rows)
    update_meta = json.loads(config_rows[1]["metadata_json"])
    assert update_meta["changes"] == {"room": {"before": "Room 4", "after": "Room 9"}}
    exc_rows = [r["action_type"] for r in _audit_rows(exc["id"])
                if r["event_category"] == "configuration"]
    assert exc_rows == ["clinic_session_exception_added", "clinic_session_exception_withdrawn"]


def test_a_refused_change_writes_nothing_and_audits_no_change(desk_on, admin, doctor):
    first = _session(admin, doctor).json()
    r = _session(admin, doctor, start_time="09:30", end_time="11:00")
    assert r.status_code == 409
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM clinic_sessions WHERE doctor_id = ?",
                       (doctor,)).fetchone()[0]
        created = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'clinic_session_created' "
            "AND metadata_json LIKE ?", (f"%{doctor}%",)).fetchone()[0]
    assert n == 1 and created == 1
    assert first["id"]


# ── 3. Slots, in hospital-local time ─────────────────────────────────────────

def test_a_weekly_session_generates_its_slots_on_its_weekday(desk_on, admin, doctor):
    assert _session(admin, doctor).status_code == 200
    body = _availability(admin, MONDAY, doctor_id=doctor).json()
    [sess] = _sessions_for(body, doctor)
    assert sess["cancelled"] is False and sess["room"] == "Room 4"
    assert [s["start"] for s in sess["slots"]] == ["09:00", "09:15", "09:30", "09:45"]
    assert sess["slots"][-1]["end"] == "10:00"
    # 09:00 in IST is 03:30 UTC; the instant is stated in UTC.
    first = datetime.fromisoformat(sess["slots"][0]["starts_at"])
    assert first == datetime(2026, 10, 5, 3, 30, tzinfo=timezone.utc)
    assert body["timezone"] == "Asia/Kolkata"
    # Not on Tuesday.
    tuesday = _availability(admin, MONDAY + timedelta(days=1), doctor_id=doctor).json()
    assert _sessions_for(tuesday, doctor) == []


def test_a_week_of_availability_lists_each_day_once(desk_on, admin, doctor):
    _session(admin, doctor)
    _session(admin, doctor, weekday=3, start_time="14:00", end_time="15:00", slot_minutes=30)
    body = _availability(admin, MONDAY, days=7, doctor_id=doctor).json()
    assert [d["date"] for d in body["days"]] == [
        (MONDAY + timedelta(days=i)).isoformat() for i in range(7)]
    per_day = {d["weekday_name"]: [s["start_time"] for s in d["sessions"]] for d in body["days"]}
    assert per_day["Monday"] == ["09:00"] and per_day["Thursday"] == ["14:00"]
    assert per_day["Tuesday"] == []


def test_today_is_the_hospitals_day_not_the_utc_one(desk_on, admin, doctor, monkeypatch):
    """00:30 IST on Monday is still Sunday in UTC. "Today" must be Monday."""
    _session(admin, doctor)
    monkeypatch.setattr(hospital_time, "_utc_now",
                        lambda: datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc))
    body = _availability(admin, doctor_id=doctor).json()
    assert body["from"] == MONDAY.isoformat() == body["hospital_today"]
    [sess] = _sessions_for(body, doctor)
    assert len(sess["slots"]) == 4
    assert not any(s["past"] for s in sess["slots"])


def test_slots_already_started_are_marked_past(desk_on, admin, doctor, monkeypatch):
    _session(admin, doctor)
    # 09:20 IST on Monday.
    monkeypatch.setattr(hospital_time, "_utc_now",
                        lambda: datetime(2026, 10, 5, 3, 50, tzinfo=timezone.utc))
    [sess] = _sessions_for(_availability(admin, doctor_id=doctor).json(), doctor)
    assert [s["past"] for s in sess["slots"]] == [True, True, False, False]


def test_validity_dates_bound_the_weeks_a_session_runs(desk_on, admin, doctor):
    _session(admin, doctor, valid_from=(MONDAY + timedelta(days=7)).isoformat())
    assert _sessions_for(_availability(admin, MONDAY, doctor_id=doctor).json(), doctor) == []
    later = _availability(admin, MONDAY + timedelta(days=7), doctor_id=doctor).json()
    assert len(_sessions_for(later, doctor)) == 1


def test_a_retired_session_and_a_departed_doctor_offer_nothing(desk_on, admin, doctor):
    created = _session(admin, doctor).json()
    client.post(f"/api/schedule/sessions/{created['id']}/retire", headers=admin,
                json={"reason": "Clinic closed"})
    assert _sessions_for(_availability(admin, MONDAY, doctor_id=doctor).json(), doctor) == []

    gone = _mk_user("doctor", CLINIC, name="Dr Departed")
    _session(admin, gone)
    with db_session() as db:
        db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (gone,))
    assert _sessions_for(_availability(admin, MONDAY, doctor_id=gone).json(), gone) == []


def test_nothing_about_slots_is_stored(desk_on, admin, doctor):
    with db_session() as db:
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%slot%'")}
    # Mortuary physical storage slots are a distinct, persisted register. This
    # invariant is about computed clinic appointment slots, never all uses of
    # the word "slot" across independent hospital domains.
    assert names - {"mortuary_slots"} == set()


# ── 4. Exceptions ─────────────────────────────────────────────────────────────

def test_a_whole_day_exception_removes_every_slot_and_says_why(desk_on, admin, doctor):
    _session(admin, doctor)
    _session(admin, doctor, start_time="14:00", end_time="15:00")
    r = client.post("/api/schedule/exceptions", headers=admin, json={
        "doctor_id": doctor, "exception_date": MONDAY.isoformat(), "reason": "On leave"})
    assert r.status_code == 200, r.text
    sessions = _sessions_for(_availability(admin, MONDAY, doctor_id=doctor).json(), doctor)
    assert len(sessions) == 2
    assert all(s["cancelled"] and s["slots"] == [] and s["cancel_reason"] == "On leave"
               for s in sessions)
    # The next Monday is untouched.
    nxt = _sessions_for(_availability(admin, MONDAY + timedelta(days=7),
                                      doctor_id=doctor).json(), doctor)
    assert all(not s["cancelled"] and s["slots"] for s in nxt)


def test_a_session_exception_removes_only_that_session(desk_on, admin, doctor):
    morning = _session(admin, doctor).json()
    _session(admin, doctor, start_time="14:00", end_time="15:00")
    r = client.post("/api/schedule/exceptions", headers=admin, json={
        "session_id": morning["id"], "exception_date": MONDAY.isoformat(),
        "reason": "Theatre list"})
    assert r.status_code == 200, r.text
    by_start = {s["start_time"]: s for s in _sessions_for(
        _availability(admin, MONDAY, doctor_id=doctor).json(), doctor)}
    assert by_start["09:00"]["slots"] == [] and by_start["09:00"]["cancelled"]
    assert len(by_start["14:00"]["slots"]) == 4


def test_withdrawing_an_exception_brings_the_slots_back(desk_on, admin, doctor):
    _session(admin, doctor)
    exc = client.post("/api/schedule/exceptions", headers=admin, json={
        "doctor_id": doctor, "exception_date": MONDAY.isoformat(), "reason": "On leave"}).json()
    r = client.post(f"/api/schedule/exceptions/{exc['id']}/withdraw", headers=admin,
                    json={"reason": "Back early"})
    assert r.status_code == 200 and r.json()["is_withdrawn"] is True
    [sess] = _sessions_for(_availability(admin, MONDAY, doctor_id=doctor).json(), doctor)
    assert not sess["cancelled"] and len(sess["slots"]) == 4
    # Kept, not deleted, and listed when asked for.
    listed = client.get("/api/schedule/exceptions", headers=admin, params={
        "date_from": MONDAY.isoformat(), "doctor_id": doctor, "include_withdrawn": True}).json()
    assert [e["id"] for e in listed["exceptions"]] == [exc["id"]]
    again = client.post(f"/api/schedule/exceptions/{exc['id']}/withdraw", headers=admin,
                        json={"reason": "twice"})
    assert again.status_code == 409


def test_an_exception_is_refused_for_a_day_the_session_does_not_run(desk_on, admin, doctor):
    morning = _session(admin, doctor).json()
    r = client.post("/api/schedule/exceptions", headers=admin, json={
        "session_id": morning["id"], "exception_date": SUNDAY.isoformat(), "reason": "x"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "not_running"


def test_a_duplicate_exception_is_refused_but_a_whole_day_after_one_session_is_not(
        desk_on, admin, doctor):
    morning = _session(admin, doctor).json()
    _session(admin, doctor, start_time="14:00", end_time="15:00")
    one = {"session_id": morning["id"], "exception_date": MONDAY.isoformat(), "reason": "List"}
    assert client.post("/api/schedule/exceptions", headers=admin, json=one).status_code == 200
    assert client.post("/api/schedule/exceptions", headers=admin, json=one).status_code == 409
    whole = {"doctor_id": doctor, "exception_date": MONDAY.isoformat(), "reason": "Leave"}
    assert client.post("/api/schedule/exceptions", headers=admin, json=whole).status_code == 200
    assert client.post("/api/schedule/exceptions", headers=admin, json=whole).status_code == 409
    sessions = _sessions_for(_availability(admin, MONDAY, doctor_id=doctor).json(), doctor)
    assert all(s["slots"] == [] for s in sessions)


def test_an_exception_needs_a_reason(desk_on, admin, doctor):
    r = client.post("/api/schedule/exceptions", headers=admin, json={
        "doctor_id": doctor, "exception_date": MONDAY.isoformat(), "reason": "   "})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "reason_required"


# ── 5. One doctor, one place ──────────────────────────────────────────────────

def test_an_overlapping_session_is_refused(desk_on, admin, doctor):
    assert _session(admin, doctor).status_code == 200
    r = _session(admin, doctor, start_time="09:45", end_time="11:00")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "session_overlap"
    assert "cannot hold two clinics" in r.json()["detail"]["message"]


@pytest.mark.parametrize("over", [
    {"start_time": "10:00", "end_time": "11:00"},          # back to back
    {"weekday": 1},                                         # another day
    {"valid_from": "2026-12-01"},                           # later term, see below
])
def test_sessions_that_do_not_overlap_are_accepted(desk_on, admin, doctor, over):
    first = {"valid_to": "2026-11-30"} if "valid_from" in over else {}
    assert _session(admin, doctor, **first).status_code == 200
    r = _session(admin, doctor, **over)
    assert r.status_code == 200, r.text


def test_two_doctors_may_share_the_same_hours(desk_on, admin, doctor):
    other = _mk_user("doctor", CLINIC)
    assert _session(admin, doctor).status_code == 200
    assert _session(admin, other).status_code == 200


def test_an_edit_into_an_overlap_is_refused_and_a_stale_edit_too(desk_on, admin, doctor):
    _session(admin, doctor)
    afternoon = _session(admin, doctor, start_time="14:00", end_time="15:00").json()
    r = client.patch(f"/api/schedule/sessions/{afternoon['id']}", headers=admin,
                     json={"row_version": afternoon["row_version"], "start_time": "09:30"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "session_overlap"
    ok = client.patch(f"/api/schedule/sessions/{afternoon['id']}", headers=admin,
                      json={"row_version": afternoon["row_version"], "end_time": "16:00"})
    assert ok.status_code == 200 and ok.json()["row_version"] == afternoon["row_version"] + 1
    stale = client.patch(f"/api/schedule/sessions/{afternoon['id']}", headers=admin,
                         json={"row_version": afternoon["row_version"], "room": "Room 1"})
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "stale"


def test_a_retired_session_no_longer_blocks_its_hours(desk_on, admin, doctor):
    first = _session(admin, doctor).json()
    client.post(f"/api/schedule/sessions/{first['id']}/retire", headers=admin,
                json={"reason": "Moved"})
    assert _session(admin, doctor).status_code == 200


# ── Validation ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("over,code", [
    ({"end_time": "09:00"}, "bad_window"),
    ({"end_time": "08:00"}, "bad_window"),
    ({"start_time": "25:00"}, "bad_time"),
    ({"slot_minutes": 2}, "bad_slot"),
    ({"slot_minutes": 90}, "bad_slot"),
    ({"weekday": 7}, "bad_weekday"),
    ({"capacity_per_slot": 0}, "bad_capacity"),
    ({"valid_from": "2026-11-01", "valid_to": "2026-10-01"}, "bad_dates"),
    ({"valid_from": "2026-02-30"}, "bad_date"),
    ({"department": OTHER}, "department_mismatch"),
])
def test_a_bad_session_is_refused_in_words(desk_on, admin, doctor, over, code):
    r = _session(admin, doctor, **over)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == code
    assert detail["message"] and "_" not in detail["message"]


@pytest.mark.parametrize("role", ["nurse", "receptionist", "admin"])
def test_only_a_doctor_may_hold_a_clinic(desk_on, admin, role):
    other = _mk_user(role, CLINIC)
    r = _session(admin, other)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "not_a_doctor"


def test_the_department_is_the_doctors_own_spelling(desk_on, admin, doctor):
    r = _session(admin, doctor, department=CLINIC.upper())
    assert r.status_code == 200 and r.json()["department"] == CLINIC


def test_the_doctors_list_holds_doctors_only(desk_on, admin, doctor):
    nurse = _mk_user("nurse", CLINIC)
    ids = {d["id"] for d in client.get("/api/schedule/doctors", headers=admin,
                                       params={"department": CLINIC}).json()["doctors"]}
    assert doctor in ids and nurse not in ids


def test_availability_is_bounded(desk_on, admin):
    assert _availability(admin, MONDAY, days=15).status_code == 422
    assert _availability(admin, MONDAY, days=0).status_code == 422
    assert client.get("/api/schedule/availability", params={"date": "05/10/2026"},
                      headers=admin).status_code == 422


def test_availability_carries_no_patient_data(desk_on, admin, doctor):
    _session(admin, doctor)
    text = json.dumps(_availability(admin, MONDAY, doctor_id=doctor).json())
    for word in ("patient", "mrn", "uhid"):
        assert word not in text.lower()


# ── 6. Migration 57 ───────────────────────────────────────────────────────────

def _m57():
    from migrations_his.m0057_clinic_sessions import MIGRATION
    return MIGRATION


def _schema(conn):
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name").fetchall()


def test_migration_57_is_named_and_numbered_as_the_plan_says():
    mig = _m57()
    assert (mig.version, mig.name) == (57, "clinic_sessions")


def test_migration_57_is_idempotent_on_a_fresh_database(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    _m57().apply(conn)
    conn.commit()
    first = _schema(conn)
    _m57().apply(conn)
    conn.commit()
    assert _schema(conn) == first
    names = {r[1] for r in first}
    assert {"clinic_sessions", "clinic_session_exceptions",
            "idx_clinic_sessions_doctor", "idx_clinic_sessions_department",
            "idx_clinic_session_exceptions_day"} <= names
    conn.close()


def test_migration_57_runs_through_the_runner_on_an_older_database(tmp_path, monkeypatch):
    """A database whose ledger stops before 57 and holds an unrelated table:
    the runner applies 57, ledgers it, and touches nothing else."""
    import migrations

    conn = sqlite3.connect(str(tmp_path / "older.db"))
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
                 "name TEXT NOT NULL, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO schema_migrations (version, name) VALUES (49, 'x')")
    conn.execute("CREATE TABLE opd_visits (id TEXT PRIMARY KEY, patient_id TEXT)")
    conn.execute("INSERT INTO opd_visits VALUES ('V1', 'P1')")
    conn.commit()
    before = {r[1]: r for r in _schema(conn)}
    monkeypatch.setattr(migrations, "MIGRATIONS", [_m57()])
    assert migrations.run_pending_migrations(conn) == [57]
    after = {r[1]: r for r in _schema(conn)}
    for name, row in before.items():
        assert after[name] == row, name
    assert conn.execute("SELECT * FROM opd_visits").fetchall() == [("V1", "P1")]
    assert conn.execute("SELECT version FROM schema_migrations WHERE version = 57").fetchone()
    assert migrations.run_pending_migrations(conn) == []
    conn.close()


def test_the_database_refuses_a_malformed_session_row(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "checks.db"))
    _m57().apply(conn)
    bad = [
        ("CS1", "D", "X", 7, "09:00", "10:00", 15),   # weekday out of range
        ("CS2", "D", "X", 0, "10:00", "09:00", 15),   # ends before it starts
        ("CS3", "D", "X", 0, "9am", "10:00", 15),     # not HH:MM
        ("CS4", "D", "X", 0, "09:00", "10:00", 1),    # slot too short
    ]
    for row in bad:
        with pytest.raises(DBIntegrityError):
            conn.execute(
                "INSERT INTO clinic_sessions (id, doctor_id, department, weekday, start_time, "
                "end_time, slot_minutes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'now')", row)
    conn.close()


def test_the_session_database_has_both_tables():
    with db_session() as db:
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('clinic_sessions', 'clinic_session_exceptions')")}
    assert names == {"clinic_sessions", "clinic_session_exceptions"}


def test_the_restore_manifest_checks_both_tables():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "verify_restore.py"
    spec = importlib.util.spec_from_file_location("_w2e_verify_restore", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {"clinic_sessions", "clinic_session_exceptions"} <= set(module.ALL_MIGRATED_TABLES)
