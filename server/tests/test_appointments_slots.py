"""Appointments with a doctor and a slot (opd-16, W7-A, 9 October 2026).

What this file pins:

  1. MIGRATION 68: the rebuild keeps every row with its origin and department
     and both old indexes, widens the statuses, adds the doctor and slot
     columns, and refuses (changing nothing, naming the ids) on orphan rows,
     unknown columns, indexes or triggers, and live slot clashes.
  2. FLAG OFF: the new routes are switched off; a doctor_id in a booking is
     ignored as an unknown field always was; the answer's shape is unchanged.
  3. DEFECTS FIXED WHATEVER THE SWITCH: cancel and complete are scoped to the
     caller's ward (another ward's appointment is 404), and every date check is
     the hospital's day (a booking at 00:30 IST for "today" is accepted, /mine
     lists it).
  4. DOCTOR AND SLOT: a booking takes a slot of the doctor's clinic session;
     a full slot is 409; a slot of N patients holds N; a time that is not a
     slot, a passed slot and a cancelled clinic day are refused.
  5. WHO BOOKS: the receptionist books inside its own clinic and is refused
     for another ward's inpatient and for another clinic's doctor; a nurse and
     the administrator are refused.
  6. RESCHEDULE keeps history (a new row pointing at the old; the old kept);
     CHECK-IN opens the OPD visit with the appointment and a token, needs the
     identity check, is idempotent and only on the day; completing or
     cancelling the visit closes the appointment; NO-SHOW manual after the
     slot starts, or computed after the session ends on read.
  7. The day view and the forecast list carry the doctor.
  8. A REPLAY ANSWERS ONLY ITS OWN BOOKING (review round 1): an
     Idempotency-Key that names a booking for another patient, doctor, day,
     time or clinic is 409 idempotency_key_reused, says nothing about that
     booking, saves nothing and is audited as refused; the same request again
     answers its booking and audits the repeat; the booking with no doctor
     binds its replay the same way; flag off a replay is unchanged.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from db_backend import DBIntegrityError
from main import app
from services import clinic_schedule_service as schedule
from services import frontdesk_service as fd
from services import hospital_time

client = TestClient(app)

CLINIC = "Slots Test Medicine"
OTHER = "Slots Test Surgery"
STORED_DOB = "1980-02-11"
STORED_PHONE = "9876501234"

#: 12:00 IST on Monday 5 October 2026.
NOW = datetime(2026, 10, 5, 6, 30, tzinfo=timezone.utc)
MONDAY = date(2026, 10, 5)
TUESDAY = date(2026, 10, 6)

m68 = importlib.import_module("migrations_his.m0068_appointments_slots")


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department=None, name=None):
    uid = f"APS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, ?, ?, 'x', ?, 1)",
                   (uid, name or f"Slots {role} {uid[-4:]}", role, department))
    return uid


def _hdr(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def staff():
    return {
        "recep": _mk_user("receptionist"),
        "recep_clinic": _mk_user("receptionist", CLINIC),
        "doc_other": _mk_user("doctor", OTHER, "Dr Slots Other"),
        "nurse": _mk_user("nurse", CLINIC),
        "admin": _mk_user("admin"),
    }


@pytest.fixture
def doctor():
    """A fresh clinic doctor per test, so sessions never overlap."""
    return _mk_user("doctor", CLINIC, f"Dr Slots {uuid.uuid4().hex[:4]}")


@pytest.fixture
def clock(monkeypatch):
    """The hospital is in IST and it is 12:00 on Monday 5 October 2026."""
    from services.vitals_source import hl7v2
    hl7v2.set_default_timezone("Asia/Kolkata")
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: NOW)
    yield
    hl7v2.set_default_timezone(None)


@pytest.fixture
def opd_on(monkeypatch, clock):
    from ws import realtime
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(config, "OPD_SCOPE_DAYS", 7)
    monkeypatch.setattr(config, "OPD_CHARGE_AT", "completion")
    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    assert config.feature_enabled("opd")
    yield


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    assert not config.feature_enabled("opd")
    yield


def _patient(*, admitted_in=None, status="registered"):
    pid = f"APP{uuid.uuid4().hex[:8].upper()}"
    name = f"Meena Slotcheck {pid[-4:]}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, date_of_birth, phone) "
            "VALUES (?, ?, ?, 46, 'F', ?, ?, ?)",
            (pid, name, f"APS-{uuid.uuid4().hex[:8].upper()}",
             "admitted" if admitted_in else status, STORED_DOB, STORED_PHONE))
        if admitted_in:
            db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                       "VALUES ('APSADMIT', 'Slots Admitter', 'doctor', 'x')")
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                "status, created_at) VALUES (?, ?, 'APSADMIT', ?, 'Bay', '2', 'active', ?)",
                (f"APA{uuid.uuid4().hex[:8].upper()}", pid, admitted_in,
                 datetime.now(timezone.utc).isoformat()))
    return {"id": pid, "name": name}


def _stated(p, **over):
    body = {"name": p["name"], "date_of_birth": STORED_DOB, "phone_last4": STORED_PHONE[-4:]}
    body.update(over)
    return body


def _session(doctor_id, *, weekday=1, start="09:00", end="10:00", slot=15, capacity=1):
    return schedule.create_session(
        {"doctor_id": doctor_id, "weekday": weekday, "start_time": start, "end_time": end,
         "slot_minutes": slot, "capacity_per_slot": capacity, "room": "Room 2"},
        actor={"id": "APS-ADMIN", "role": "admin"})


def _book(uid, role, pid, *, day=TUESDAY, at="09:00", doctor_id=None, key=None, **extra):
    body = {"patient_id": pid, "scheduled_date": day.isoformat(), "scheduled_time": at,
            "appointment_type": "follow_up", **extra}
    if doctor_id:
        body["doctor_id"] = doctor_id
    headers = _hdr(uid, role)
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/api/appointments", json=body, headers=headers)


def _row(appointment_id):
    with db_session() as db:
        r = db.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    return dict(r) if r else None


def _insert(pid, *, day, at, status="booked", department=CLINIC, doctor_id=None,
            session_id=None, position=None):
    aid = str(uuid.uuid4())
    with db_session() as db:
        db.execute(
            "INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time, "
            "appointment_type, status, origin, department, doctor_id, session_id, slot_position) "
            "VALUES (?, ?, ?, ?, 'Follow-up', ?, 'staff', ?, ?, ?, ?)",
            (aid, pid, day.isoformat(), at, status, department, doctor_id, session_id,
             position if position is not None else (1 if doctor_id else None)))
    return aid


def _audit(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(target_id))).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Migration 68
# ═════════════════════════════════════════════════════════════════════════════

_OLD_SHAPE = (
    "CREATE TABLE appointments (id TEXT PRIMARY KEY, "
    "patient_id TEXT NOT NULL REFERENCES patients(id), "
    "booked_by_user_id TEXT REFERENCES users(id), scheduled_date TEXT NOT NULL, "
    "scheduled_time TEXT NOT NULL, appointment_type TEXT NOT NULL DEFAULT 'Follow-up', "
    "reason TEXT, status TEXT NOT NULL DEFAULT 'booked' CHECK (status IN "
    "('booked', 'cancelled', 'completed')), idempotency_key TEXT, "
    "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
)


def _old_db(*extra):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE patients (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
    conn.execute(_OLD_SHAPE)
    conn.execute("ALTER TABLE appointments ADD COLUMN origin TEXT NOT NULL DEFAULT 'staff'")
    conn.execute("ALTER TABLE appointments ADD COLUMN department TEXT")
    for statement in m68.KNOWN_INDEXES.values():
        conn.execute(statement)
    conn.execute("INSERT INTO patients VALUES ('P1')")
    conn.execute("INSERT INTO users VALUES ('U1')")
    conn.execute(
        "INSERT INTO appointments (id, patient_id, booked_by_user_id, scheduled_date, "
        "scheduled_time, reason, idempotency_key, origin, department) VALUES "
        "('A1', 'P1', 'U1', '2026-11-01', '09:00', 'Review', 'K1', 'patient', 'Cardiology')")
    conn.execute(
        "INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time, status) "
        "VALUES ('A2', 'P1', '2026-11-02', '10:00', 'cancelled')")
    for statement in extra:
        conn.execute(statement)
    return conn


def _migrate(conn):
    conn.execute("BEGIN")
    try:
        m68.apply(conn)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def test_the_rebuild_keeps_every_row_origin_department_and_both_indexes():
    conn = _old_db()
    _migrate(conn)
    assert m68.has_new_shape(conn)
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM appointments")}
    assert rows["A1"]["origin"] == "patient" and rows["A1"]["department"] == "Cardiology"
    assert rows["A1"]["idempotency_key"] == "K1" and rows["A1"]["reason"] == "Review"
    assert rows["A2"]["status"] == "cancelled" and rows["A2"]["origin"] == "staff"
    assert all(rows["A1"][c] is None for c in m68.ADDED_COLUMNS)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'appointments'")}
    assert {"idx_appointments_patient", "idx_appointments_idem",
            m68.DOCTOR_SLOT_INDEX_NAME} <= names
    # The new statuses are accepted, and one slot position takes one live booking.
    conn.execute("UPDATE appointments SET status = 'no_show' WHERE id = 'A1'")
    conn.execute("INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time, "
                 "doctor_id, slot_position) VALUES ('S1', 'P1', '2026-11-05', '09:00', 'U1', 1)")
    with pytest.raises(DBIntegrityError):
        conn.execute("INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time, "
                     "doctor_id, slot_position) VALUES ('S2', 'P1', '2026-11-05', '09:00', 'U1', 1)")
    # A doctor's booking must hold a position.
    with pytest.raises(DBIntegrityError):
        conn.execute("INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time, "
                     "doctor_id) VALUES ('S3', 'P1', '2026-11-05', '09:15', 'U1')")
    # Idempotent.
    _migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 3


def test_the_rebuild_refuses_orphans_with_a_list_and_changes_nothing():
    conn = _old_db()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time) "
                 "VALUES ('ORPHAN', 'NOBODY', '2026-11-03', '11:00')")
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(m68.AppointmentsRebuildRefused) as refused:
        _migrate(conn)
    assert refused.value.ids == ["ORPHAN"]
    assert not m68.has_new_shape(conn)
    assert conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 3


@pytest.mark.parametrize("extra", [
    "ALTER TABLE appointments ADD COLUMN surprise TEXT",
    "CREATE INDEX idx_surprise ON appointments(reason)",
    "CREATE TRIGGER trg_surprise AFTER UPDATE ON appointments BEGIN SELECT 1; END",
])
def test_the_rebuild_refuses_what_it_would_drop(extra):
    conn = _old_db(extra)
    with pytest.raises(m68.AppointmentsRebuildRefused):
        _migrate(conn)
    assert not m68.has_new_shape(conn)


def test_the_slot_index_is_not_built_over_a_clash():
    conn = _old_db()
    _migrate(conn)
    conn.execute(f"DROP INDEX {m68.DOCTOR_SLOT_INDEX_NAME}")
    for aid in ("C1", "C2"):
        conn.execute("INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time, "
                     f"doctor_id, slot_position) VALUES ('{aid}', 'P1', '2026-11-05', '09:00', "
                     "'U1', 1)")
    with pytest.raises(m68.AppointmentsRebuildRefused) as refused:
        _migrate(conn)
    assert refused.value.ids == ["C1", "C2"]


def test_the_suites_database_has_the_new_shape():
    with db_session() as db:
        assert m68.has_new_shape(db)
        names = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'appointments'")}
    assert {"idx_appointments_patient", "idx_appointments_idem",
            m68.DOCTOR_SLOT_INDEX_NAME, "idx_appointments_day"} <= names


# ═════════════════════════════════════════════════════════════════════════════
# 2. Flag off
# ═════════════════════════════════════════════════════════════════════════════

OPD_ROUTES = [
    ("get", "/api/appointments/day", None),
    ("post", "/api/appointments/APS-NOPE/check-in", {}),
    ("post", "/api/appointments/APS-NOPE/no-show", None),
    ("post", "/api/appointments/APS-NOPE/reschedule",
     {"scheduled_date": "2030-01-01", "scheduled_time": "09:00"}),
]


@pytest.mark.parametrize("method, path, body", OPD_ROUTES)
def test_flag_off_the_new_routes_are_switched_off(staff, opd_off, method, path, body):
    resp = getattr(client, method)(path, headers=_hdr(staff["recep"], "receptionist"),
                                   **({"json": body} if body is not None else {}))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "feature_disabled"


@pytest.mark.parametrize("method, path, body", OPD_ROUTES)
def test_no_session_is_401(opd_on, method, path, body):
    resp = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert resp.status_code == 401


def test_flag_off_a_doctor_in_the_body_is_ignored_and_the_shape_is_unchanged(opd_off, doctor):
    pid = _patient(admitted_in=CLINIC)["id"]
    day = (hospital_time.hospital_today() + timedelta(days=3)).isoformat()
    resp = client.post("/api/appointments", json={
        "patient_id": pid, "scheduled_date": day, "scheduled_time": "11:00",
        "appointment_type": "follow_up", "doctor_id": doctor},
        headers=_hdr(doctor, "doctor"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "doctor_id" not in body and "slot_position" not in body
    assert _row(body["id"])["doctor_id"] is None


# ═════════════════════════════════════════════════════════════════════════════
# 3. The two defects, fixed whatever the switch
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("switch", ["opd_off", "opd_on"])
def test_another_wards_appointment_cannot_be_cancelled_or_completed(request, switch, staff):
    request.getfixturevalue(switch)
    clinic_doctor = _mk_user("doctor", CLINIC)
    theirs = _patient(admitted_in=OTHER)["id"]
    mine = _patient(admitted_in=CLINIC)["id"]
    future = hospital_time.hospital_today() + timedelta(days=4)
    other_appt = _insert(theirs, day=future, at="10:00", department=None)
    my_appt = _insert(mine, day=future, at="10:30", department=None)
    headers = _hdr(clinic_doctor, "doctor")
    assert client.delete(f"/api/appointments/{other_appt}", headers=headers).status_code == 404
    assert client.post(f"/api/appointments/{other_appt}/complete",
                       headers=headers).status_code == 404
    assert _row(other_appt)["status"] == "booked"
    assert client.post(f"/api/appointments/{my_appt}/complete",
                       headers=headers).status_code == 200
    second = _insert(mine, day=future, at="11:00", department=None)
    resp = client.delete(f"/api/appointments/{second}?reason=Patient%20phoned", headers=headers)
    assert resp.status_code == 200 and _row(second)["cancel_reason"] == "Patient phoned"
    # The other ward's own doctor still can.
    assert client.delete(f"/api/appointments/{other_appt}",
                         headers=_hdr(staff["doc_other"], "doctor")).status_code == 200


@pytest.fixture
def half_past_midnight(monkeypatch):
    """00:30 IST on Monday 5 October 2026, which is 19:00 UTC on Sunday. The
    instant is in the past of any real run, so a server-clock date check
    would refuse Monday as past where the hospital's day accepts it."""
    from services.vitals_source import hl7v2
    hl7v2.set_default_timezone("Asia/Kolkata")
    monkeypatch.setattr(hospital_time, "_utc_now",
                        lambda: datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc))
    yield
    hl7v2.set_default_timezone(None)


def test_a_booking_at_half_past_midnight_ist_uses_the_hospitals_day(half_past_midnight, opd_off):
    from routes.appointments_routes import default_slot_for

    doctor_id = _mk_user("doctor", CLINIC)
    pid = _patient(admitted_in=CLINIC)["id"]
    assert hospital_time.hospital_today() == MONDAY
    ok = client.post("/api/appointments", json={
        "patient_id": pid, "scheduled_date": MONDAY.isoformat(), "scheduled_time": "10:00",
        "appointment_type": "follow_up"}, headers=_hdr(doctor_id, "doctor"))
    assert ok.status_code == 200, ok.text
    # Sunday is the UTC date at this instant, and it is the past on the ward.
    past = client.post("/api/appointments", json={
        "patient_id": pid, "scheduled_date": "2026-10-04", "scheduled_time": "23:00",
        "appointment_type": "follow_up"}, headers=_hdr(doctor_id, "doctor"))
    assert past.status_code == 422
    # A request with no time on the hospital's today gets the first clinic slot.
    assert default_slot_for(MONDAY) == "09:00"


def test_mine_lists_from_the_hospitals_day(clock, opd_off):
    from auth import hash_password
    pid = _patient(admitted_in=CLINIC)["id"]
    uid = f"APSU{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, is_active) "
                   "VALUES (?, 'Slots Portal', 'patient', ?, 1)", (uid, hash_password("x")))
        db.execute("UPDATE patients SET user_id = ? WHERE id = ?", (uid, pid))
    today = _insert(pid, day=MONDAY, at="15:00", department=None)
    yesterday = _insert(pid, day=MONDAY - timedelta(days=1), at="15:00", department=None)
    listed = {a["id"] for a in client.get("/api/appointments/mine",
                                          headers=_hdr(uid, "patient")).json()["appointments"]}
    assert today in listed and yesterday not in listed


# ═════════════════════════════════════════════════════════════════════════════
# 4. Doctor and slot
# ═════════════════════════════════════════════════════════════════════════════

def test_a_slot_booking_holds_the_doctor_and_a_full_slot_is_refused(staff, opd_on, doctor):
    session = _session(doctor)
    first = _patient()["id"]
    resp = _book(staff["recep"], "receptionist", first, doctor_id=doctor)
    assert resp.status_code == 200, resp.text
    appt = resp.json()
    assert appt["doctor_id"] == doctor and appt["session_id"] == session["id"]
    assert appt["slot_position"] == 1 and appt["department"] == CLINIC
    assert appt["doctor_name"].startswith("Dr Slots")
    [audit] = _audit("appointment_booked", appt["id"])
    assert json.loads(audit["metadata_json"])["slot_position"] == 1

    # The same patient again is the same booking, not a second one.
    again = _book(staff["recep"], "receptionist", first, doctor_id=doctor)
    assert again.status_code == 200 and again.json()["id"] == appt["id"]
    full = _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor)
    assert full.status_code == 409 and full.json()["detail"]["error"] == "slot_full"
    # The next slot is free.
    assert _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor,
                 at="09:15").status_code == 200


def test_a_slot_for_two_takes_two(staff, opd_on, doctor):
    _session(doctor, capacity=2)
    a = _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor).json()
    b = _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor).json()
    assert {a["slot_position"], b["slot_position"]} == {1, 2}
    third = _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor)
    assert third.status_code == 409
    # A cancelled booking gives its place back.
    client.delete(f"/api/appointments/{a['id']}", headers=_hdr(staff["recep"], "receptionist"))
    assert _book(staff["recep"], "receptionist", _patient()["id"],
                 doctor_id=doctor).json()["slot_position"] == a["slot_position"]


@pytest.mark.parametrize("day, at, code, status", [
    (TUESDAY, "09:07", "no_such_slot", 409),
    (TUESDAY, "11:00", "no_such_slot", 409),
    (MONDAY + timedelta(days=2), "09:00", "no_such_slot", 409),
])
def test_a_time_that_is_not_a_slot_is_refused(staff, opd_on, doctor, day, at, code, status):
    _session(doctor)
    resp = _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor,
                 day=day, at=at)
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["error"] == code


def test_a_passed_slot_and_a_cancelled_clinic_day_are_refused(staff, opd_on, doctor):
    _session(doctor, weekday=0, start="11:00", end="13:00", slot=30)
    passed = _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor,
                   day=MONDAY, at="11:30")
    assert passed.status_code == 422
    schedule.add_exception({"doctor_id": doctor, "exception_date": (MONDAY + timedelta(days=7)).isoformat(),
                            "reason": "Doctor on leave"},
                           actor={"id": "APS-ADMIN", "role": "admin"})
    off = _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor,
                day=MONDAY + timedelta(days=7), at="11:00")
    assert off.status_code == 409 and off.json()["detail"]["error"] == "clinic_not_running"


# ═════════════════════════════════════════════════════════════════════════════
# 5. Who books
# ═════════════════════════════════════════════════════════════════════════════

def test_the_clinic_receptionist_books_inside_its_clinic_only(staff, opd_on, doctor):
    _session(doctor)
    seen_here = _patient()
    fd.register_opd_visit(patient_id=seen_here["id"], department=CLINIC, registered_by="X")
    ok = _book(staff["recep_clinic"], "receptionist", seen_here["id"], doctor_id=doctor)
    assert ok.status_code == 200, ok.text

    inpatient_elsewhere = _patient(admitted_in=OTHER)["id"]
    assert _book(staff["recep_clinic"], "receptionist", inpatient_elsewhere,
                 doctor_id=doctor, at="09:15").status_code == 404
    # A patient this clinic never saw is outside a clinic desk's scope; the
    # central desk books them.
    never_seen = _patient()["id"]
    assert _book(staff["recep_clinic"], "receptionist", never_seen, doctor_id=doctor,
                 at="09:30").status_code == 404
    assert _book(staff["recep"], "receptionist", never_seen, doctor_id=doctor,
                 at="09:30").status_code == 200
    # Another clinic's doctor is not this desk's to book.
    _session(staff["doc_other"], weekday=1, start="14:00", end="15:00")
    other = _book(staff["recep_clinic"], "receptionist", seen_here["id"],
                  doctor_id=staff["doc_other"], at="14:00")
    assert other.status_code == 403 and other.json()["detail"]["error"] == "department_scope"
    no_doctor = _book(staff["recep_clinic"], "receptionist", seen_here["id"], at="16:00",
                      department=OTHER)
    assert no_doctor.status_code == 403


def test_a_nurse_and_the_administrator_do_not_book(staff, opd_on, doctor):
    _session(doctor)
    pid = _patient(admitted_in=CLINIC)["id"]
    assert _book(staff["nurse"], "nurse", pid, doctor_id=doctor).status_code == 403
    assert _book(staff["admin"], "admin", pid, doctor_id=doctor).status_code == 403


def test_the_doctor_books_their_own_follow_up(opd_on, doctor):
    _session(doctor)
    seen = _patient()
    fd.register_opd_visit(patient_id=seen["id"], department=CLINIC, doctor_id=doctor,
                          registered_by=doctor)
    resp = _book(doctor, "doctor", seen["id"], doctor_id=doctor)
    assert resp.status_code == 200, resp.text


# ═════════════════════════════════════════════════════════════════════════════
# 6. Reschedule, check-in, no-show
# ═════════════════════════════════════════════════════════════════════════════

def test_reschedule_is_a_new_row_and_the_old_one_is_kept(staff, opd_on, doctor):
    _session(doctor)
    pid = _patient()["id"]
    old = _book(staff["recep"], "receptionist", pid, doctor_id=doctor).json()
    resp = client.post(f"/api/appointments/{old['id']}/reschedule",
                       json={"scheduled_date": TUESDAY.isoformat(), "scheduled_time": "09:45",
                             "reason": "Patient asked for later"},
                       headers=_hdr(staff["recep"], "receptionist"))
    assert resp.status_code == 200, resp.text
    new, previous = resp.json()["appointment"], resp.json()["previous"]
    assert new["rescheduled_from_id"] == old["id"] and new["scheduled_time"] == "09:45"
    assert new["doctor_id"] == doctor and new["status"] == "booked"
    assert previous["status"] == "cancelled"
    assert previous["cancel_reason"].startswith("Rescheduled to 2026-10-06 09:45")
    assert _audit("appointment_rescheduled", new["id"])
    # The old slot is free again; the old row cannot be moved twice.
    assert _book(staff["recep"], "receptionist", _patient()["id"],
                 doctor_id=doctor).status_code == 200
    twice = client.post(f"/api/appointments/{old['id']}/reschedule",
                        json={"scheduled_date": TUESDAY.isoformat(), "scheduled_time": "09:30"},
                        headers=_hdr(staff["recep"], "receptionist"))
    assert twice.status_code == 409


def test_a_repeated_reschedule_answers_the_first_move(staff, opd_on, doctor):
    _session(doctor)
    old = _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor).json()
    headers = {**_hdr(staff["recep"], "receptionist"), "Idempotency-Key": f"rs-{uuid.uuid4()}"}
    body = {"scheduled_date": TUESDAY.isoformat(), "scheduled_time": "09:30"}
    first = client.post(f"/api/appointments/{old['id']}/reschedule", json=body, headers=headers)
    again = client.post(f"/api/appointments/{old['id']}/reschedule", json=body, headers=headers)
    assert first.status_code == 200 and again.status_code == 200, again.text
    assert again.json()["replayed"] is True
    assert again.json()["appointment"]["id"] == first.json()["appointment"]["id"]
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM appointments WHERE rescheduled_from_id = ?",
                       (old["id"],)).fetchone()[0]
    assert n == 1
    # The same key on another booking is refused, not answered with this one.
    other = _book(staff["recep"], "receptionist", _patient()["id"], doctor_id=doctor,
                  at="09:45").json()
    reused = client.post(f"/api/appointments/{other['id']}/reschedule",
                         json={"scheduled_date": TUESDAY.isoformat(), "scheduled_time": "09:15"},
                         headers=headers)
    assert reused.status_code == 409
    assert reused.json()["detail"]["error"] == "idempotency_key_reused"


def test_a_no_show_rescheduled_stays_a_no_show(staff, opd_on, doctor):
    session = _session(doctor, weekday=0, start="09:00", end="10:00")
    _session(doctor, weekday=1, start="11:00", end="12:00")
    pid = _patient()["id"]
    missed = _insert(pid, day=MONDAY, at="09:00", doctor_id=doctor, session_id=session["id"])
    marked = client.post(f"/api/appointments/{missed}/no-show",
                         headers=_hdr(staff["recep"], "receptionist"))
    assert marked.status_code == 200 and marked.json()["status"] == "no_show"
    assert _audit("appointment_no_show", missed)
    moved = client.post(f"/api/appointments/{missed}/reschedule",
                        json={"scheduled_date": TUESDAY.isoformat(), "scheduled_time": "11:00"},
                        headers=_hdr(staff["recep"], "receptionist"))
    assert moved.status_code == 200, moved.text
    assert moved.json()["previous"]["status"] == "no_show"
    again = client.post(f"/api/appointments/{missed}/reschedule",
                        json={"scheduled_date": TUESDAY.isoformat(), "scheduled_time": "11:15"},
                        headers=_hdr(staff["recep"], "receptionist"))
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_rescheduled"


def test_no_show_only_once_the_slot_has_started(staff, opd_on, doctor):
    pid = _patient()["id"]
    later = _insert(pid, day=MONDAY, at="13:00")
    early = client.post(f"/api/appointments/{later}/no-show",
                        headers=_hdr(staff["recep"], "receptionist"))
    assert early.status_code == 409 and early.json()["detail"]["error"] == "not_yet_due"
    assert client.post(f"/api/appointments/{later}/no-show",
                       headers=_hdr(staff["nurse"], "nurse")).status_code == 403


def test_check_in_opens_the_visit_with_a_token_and_is_idempotent(staff, opd_on, doctor):
    session = _session(doctor, weekday=0, start="09:00", end="17:00")
    p = _patient()
    aid = _insert(p["id"], day=MONDAY, at="12:30", doctor_id=doctor, session_id=session["id"])
    headers = _hdr(staff["recep"], "receptionist")
    missing = client.post(f"/api/appointments/{aid}/check-in", json={}, headers=headers)
    assert missing.status_code == 409
    assert missing.json()["detail"]["error"] == "identity_check_required"
    wrong = client.post(f"/api/appointments/{aid}/check-in",
                        json={"identity_check": _stated(p, date_of_birth="1999-01-01")},
                        headers=headers)
    assert wrong.status_code == 409 and wrong.json()["detail"]["error"] == "identity_not_confirmed"
    assert _row(aid)["status"] == "booked"

    resp = client.post(f"/api/appointments/{aid}/check-in",
                       json={"identity_check": _stated(p)}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    visit = body["visit"]
    assert visit["appointment_id"] == aid and visit["doctor_id"] == doctor
    assert visit["token_number"] >= 1 and visit["department"] == CLINIC
    assert body["appointment"]["status"] == "checked_in"
    assert body["appointment"]["opd_visit_id"] == visit["id"]
    assert body["appointment"]["token_number"] == visit["token_number"]
    assert STORED_DOB not in resp.text and STORED_PHONE not in resp.text
    assert _audit("appointment_checked_in", aid)
    assert _audit("opd_visit_registered", visit["id"])

    again = client.post(f"/api/appointments/{aid}/check-in",
                        json={"identity_check": _stated(p)}, headers=headers)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["visit"]["id"] == visit["id"]
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM opd_visits WHERE appointment_id = ?",
                       (aid,)).fetchone()[0]
    assert n == 1

    # Completing the visit marks the appointment attended.
    fd.complete_opd_visit(visit["id"], completed_by=doctor)
    assert _row(aid)["status"] == "completed"


def test_cancelling_the_visit_closes_the_checked_in_appointment(staff, opd_on, doctor):
    p = _patient()
    aid = _insert(p["id"], day=MONDAY, at="12:00")
    resp = client.post(f"/api/appointments/{aid}/check-in",
                       json={"identity_check": _stated(p)},
                       headers=_hdr(staff["recep"], "receptionist"))
    assert resp.status_code == 200, resp.text
    visit = resp.json()["visit"]
    assert client.delete(f"/api/appointments/{aid}",
                         headers=_hdr(staff["recep"], "receptionist")).status_code == 409
    fd.cancel_opd_visit(visit["id"], reason="Left before being seen",
                        reason_code="left_without_being_seen", cancelled_by=staff["recep"])
    row = _row(aid)
    assert row["status"] == "cancelled"
    assert row["cancel_reason"].startswith("The outpatient visit was cancelled")


def test_check_in_only_on_the_day_and_in_the_callers_clinic(staff, opd_on, doctor):
    p = _patient()
    tomorrow = _insert(p["id"], day=TUESDAY, at="10:00")
    early = client.post(f"/api/appointments/{tomorrow}/check-in",
                        json={"identity_check": _stated(p)},
                        headers=_hdr(staff["recep"], "receptionist"))
    assert early.status_code == 409 and early.json()["detail"]["error"] == "not_today"
    elsewhere = _insert(p["id"], day=MONDAY, at="10:00", department=OTHER)
    # A clinic desk does not see another clinic's booking at all.
    assert client.post(f"/api/appointments/{elsewhere}/check-in",
                       json={"identity_check": _stated(p)},
                       headers=_hdr(staff["recep_clinic"], "receptionist")).status_code == 404
    assert client.post(f"/api/appointments/{elsewhere}/check-in",
                       json={"identity_check": _stated(p)},
                       headers=_hdr(staff["nurse"], "nurse")).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# 7. The day view and the forecast list
# ═════════════════════════════════════════════════════════════════════════════

def test_the_day_view_lists_the_clinics_day_with_slots_and_a_computed_no_show(
        staff, opd_on, doctor):
    morning = _session(doctor, weekday=0, start="09:00", end="10:00", slot=30)
    afternoon = _session(doctor, weekday=0, start="14:00", end="15:00", slot=30)
    p = _patient()
    over = _insert(p["id"], day=MONDAY, at="09:30", doctor_id=doctor, session_id=morning["id"])
    still = _insert(_patient()["id"], day=MONDAY, at="14:00", doctor_id=doctor,
                    session_id=afternoon["id"])
    elsewhere = _insert(_patient()["id"], day=MONDAY, at="14:30", department=OTHER)
    resp = client.get("/api/appointments/day", params={"date": MONDAY.isoformat()},
                      headers=_hdr(staff["recep_clinic"], "receptionist"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_id = {a["id"]: a for a in body["appointments"]}
    assert over in by_id and still in by_id and elsewhere not in by_id
    assert by_id[over]["effective_status"] == "no_show" and by_id[over]["no_show_computed"]
    assert by_id[over]["status"] == "booked"
    assert by_id[still]["effective_status"] == "booked"
    assert by_id[still]["doctor_name"].startswith("Dr Slots")
    assert by_id[over]["patient_name"] == p["name"]
    sessions = {s["session_id"]: s for s in body["sessions"]}
    slot = {s["start"]: s for s in sessions[afternoon["id"]]["slots"]}["14:00"]
    assert slot["booked"] == 1 and slot["free"] == 0
    [audit] = [a for a in _audit("appointments_day_viewed", MONDAY.isoformat())
               if over in json.loads(a["metadata_json"])["appointment_ids"]]
    assert elsewhere not in json.loads(audit["metadata_json"])["appointment_ids"]
    # The read wrote nothing to the booking.
    assert _row(over)["status"] == "booked"


def test_the_forecast_list_carries_the_doctor(staff, opd_on, doctor):
    from services.forecast_service import get_appointments
    _session(doctor)
    pid = _patient()["id"]
    appt = _book(staff["recep"], "receptionist", pid, doctor_id=doctor).json()
    [row] = [r for r in get_appointments(days_ahead=3) if r["id"] == appt["id"]]
    assert row["doctor_id"] == doctor and row["doctor_name"].startswith("Dr Slots")


# ═════════════════════════════════════════════════════════════════════════════
# 8. A replay answers only its own booking
# ═════════════════════════════════════════════════════════════════════════════

def _key():
    return f"appointment-{uuid.uuid4()}"


def _count(sql, *params):
    with db_session() as db:
        return db.execute(sql, params).fetchone()[0]


def test_a_key_reused_for_another_patient_is_refused_and_names_nobody(staff, opd_on, doctor):
    """Probed in review: the central desk books another ward's inpatient into
    the 09:00 slot with key K, then the clinic desk books its own patient at
    09:15 with K. The answer was 200 with the inpatient's name and hospital
    number, and the clinic's patient was not booked."""
    _session(doctor)
    key = _key()
    inpatient = _patient(admitted_in=OTHER)
    first = _book(staff["recep"], "receptionist", inpatient["id"], doctor_id=doctor, key=key)
    assert first.status_code == 200, first.text
    seen_here = _patient()
    fd.register_opd_visit(patient_id=seen_here["id"], department=CLINIC, registered_by="X")

    reused = _book(staff["recep_clinic"], "receptionist", seen_here["id"], doctor_id=doctor,
                   at="09:15", key=key)
    assert reused.status_code == 409, reused.text
    assert reused.json()["detail"]["error"] == "idempotency_key_reused"
    for told in (inpatient["id"], inpatient["name"], first.json()["id"],
                 first.json()["patient_uhid"]):
        assert told not in reused.text
    assert _count("SELECT COUNT(*) FROM appointments WHERE patient_id = ?", seen_here["id"]) == 0
    [refused] = _audit("appointment_replay_refused", first.json()["id"])
    assert refused["outcome"] == "denied" and refused["event_category"] == "authorization"
    assert refused["patient_id"] == seen_here["id"]
    assert refused["actor_user_id"] == staff["recep_clinic"]
    meta = json.loads(refused["metadata_json"])
    assert meta["reason"] == "idempotency_key_names_another_booking"
    assert meta["requested_time"] == "09:15" and meta["requested_doctor_id"] == doctor
    # The inpatient's booking is untouched, and with a key of its own the
    # clinic desk's booking goes through.
    assert _row(first.json()["id"])["status"] == "booked"
    ok = _book(staff["recep_clinic"], "receptionist", seen_here["id"], doctor_id=doctor,
               at="09:15", key=_key())
    assert ok.status_code == 200, ok.text
    assert ok.json()["patient_id"] == seen_here["id"]


@pytest.mark.parametrize("change", ["patient", "time", "day", "doctor"])
def test_a_key_replayed_for_any_other_booking_is_refused(staff, opd_on, doctor, change):
    _session(doctor)
    other_doctor = _mk_user("doctor", CLINIC, f"Dr Slots {uuid.uuid4().hex[:4]}")
    _session(other_doctor)
    pid = _patient()["id"]
    key = _key()
    first = _book(staff["recep"], "receptionist", pid, doctor_id=doctor, key=key)
    assert first.status_code == 200, first.text
    target, asked = pid, {"doctor_id": doctor, "at": "09:00", "day": TUESDAY}
    if change == "patient":
        target = _patient()["id"]
    elif change == "time":
        asked["at"] = "09:15"
    elif change == "day":
        asked["day"] = TUESDAY + timedelta(days=7)
    else:
        asked["doctor_id"] = other_doctor
    again = _book(staff["recep"], "receptionist", target, key=key, **asked)
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["error"] == "idempotency_key_reused"
    assert first.json()["id"] not in again.text
    assert _count("SELECT COUNT(*) FROM appointments WHERE idempotency_key = ?", key) == 1
    if change == "patient":
        assert _count("SELECT COUNT(*) FROM appointments WHERE patient_id = ?", target) == 0
    assert _audit("appointment_replay_refused", first.json()["id"])


def test_the_same_request_again_answers_its_booking_and_audits_the_repeat(
        staff, opd_on, doctor):
    _session(doctor)
    p = _patient()
    key = _key()
    first = _book(staff["recep"], "receptionist", p["id"], doctor_id=doctor, key=key)
    again = _book(staff["recep"], "receptionist", p["id"], doctor_id=doctor, key=key)
    assert first.status_code == 200 and again.status_code == 200, again.text
    booking = first.json()["id"]
    assert again.json()["id"] == booking and again.json()["patient_name"] == p["name"]
    # One booking on the record, and the repeat that named the patient again.
    assert len(_audit("appointment_booked", booking)) == 1
    [repeat] = _audit("appointment_booking_repeated", booking)
    meta = json.loads(repeat["metadata_json"])
    assert meta["replay"] is True and meta["matched_by"] == "idempotency_key"
    assert repeat["event_category"] == "clinical_record" and repeat["patient_id"] == p["id"]
    assert repeat["actor_user_id"] == staff["recep"]
    # A double tap with no key answers the same booking and is audited too.
    tap = _book(staff["recep"], "receptionist", p["id"], doctor_id=doctor)
    assert tap.status_code == 200 and tap.json()["id"] == booking
    assert [json.loads(r["metadata_json"])["matched_by"]
            for r in _audit("appointment_booking_repeated", booking)] == [
        "idempotency_key", "same_patient_and_slot"]
    assert not _audit("appointment_replay_refused", booking)


def test_a_booking_with_no_doctor_binds_its_replay_the_same_way(staff, opd_on):
    pid = _patient()["id"]
    key = _key()
    first = _book(staff["recep"], "receptionist", pid, at="16:00", key=key, department=CLINIC)
    assert first.status_code == 200, first.text
    booking = first.json()["id"]
    again = _book(staff["recep"], "receptionist", pid, at="16:00", key=key, department=CLINIC)
    assert again.status_code == 200 and again.json()["id"] == booking

    other = _patient()["id"]
    for target, at, clinic in ((other, "16:00", CLINIC), (pid, "16:30", CLINIC),
                               (pid, "16:00", OTHER)):
        refused = _book(staff["recep"], "receptionist", target, at=at, key=key,
                        department=clinic)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["error"] == "idempotency_key_reused"
        assert booking not in refused.text and pid not in refused.text
    assert _count("SELECT COUNT(*) FROM appointments WHERE patient_id = ?", other) == 0
    assert _count("SELECT COUNT(*) FROM appointments WHERE patient_id = ?", pid) == 1
    assert len(_audit("appointment_replay_refused", booking)) == 3
    assert len(_audit("appointment_booked", booking)) == 1


def test_flag_off_a_replay_of_the_same_request_is_unchanged(opd_off, doctor):
    pid = _patient(admitted_in=CLINIC)["id"]
    day = (hospital_time.hospital_today() + timedelta(days=3)).isoformat()
    body = {"patient_id": pid, "scheduled_date": day, "scheduled_time": "11:30",
            "appointment_type": "follow_up"}
    headers = {**_hdr(doctor, "doctor"), "Idempotency-Key": _key()}
    first = client.post("/api/appointments", json=body, headers=headers)
    again = client.post("/api/appointments", json=body, headers=headers)
    assert first.status_code == 200 and again.status_code == 200, again.text
    assert again.json() == first.json()
    assert "patient_name" not in again.json()
    assert len(_audit("appointment_booked", first.json()["id"])) == 1
    assert not _audit("appointment_booking_repeated", first.json()["id"])
