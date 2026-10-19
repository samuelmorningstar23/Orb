"""Live bed board updates (adt-19, HIS wave 9, W9-A; ORB_ADT, default off).

What this file defends:

  1. ROOMS. While the module is on, an adt.read holder's socket joins
     'adt:<their department>' ('adt:__all__' with none): the department comes
     from department_scope, never the client. A nurse (no adt.read) joins no
     adt room. With the module off nobody joins one and every room is what it
     was.
  2. THE PAYLOAD carries {ward_id} and nothing else, never a patient field;
     one emit per ward, to the __all__ room and every department involved as
     ONE list (a socket in two of them hears it once); no ward is
     {ward_id: null}. Nothing is pushed while the module is off.
  3. AFTER COMMIT, FROM THE ROUTE. A move, an admission, an end of stay and a
     presence change each schedule one push after their transaction
     committed (the movement is visible on a fresh connection when the push
     is scheduled); a refused write and a replay push nothing.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import bed_master_service as bm
from services.adt import live as adt_live
from ws import realtime

client = TestClient(main.app)
DEPT = "General Medicine"
DOCTOR = {"id": "D001", "role": "doctor", "department": DEPT}
_SEEDED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _discharge_what_this_file_admitted():
    yield
    with db_session() as db:
        for pid in _SEEDED:
            db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ? "
                       "AND status = 'active'", (pid,))
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "ADT_STRICT_LOCATIONS_ENABLED", False)
    yield


@pytest.fixture()
def pushes(monkeypatch):
    """Record every scheduled 'adt:changed' with what the database held at
    that moment, read on a fresh connection (so only committed rows)."""
    calls: list[dict] = []

    def record(ward_ids, departments):
        with db_session() as db:
            moves = db.execute("SELECT COUNT(*) FROM patient_movements").fetchone()[0]
        calls.append({"wards": list(ward_ids), "departments": list(departments),
                      "movements_committed": moves})
        return True

    monkeypatch.setattr(realtime, "schedule_adt_changed", record)
    return calls


def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"LB{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Live {role} {uid[-4:]}", role, department, hash_password("password")),
        )
    return {"id": uid, "name": f"Live {role}", "role": role, "department": department}


_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        _ADMIN = _user("admin", None)
    return _ADMIN


def _hdr(user: dict, key: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        out["Idempotency-Key"] = key
    return out


def _ward(department: str = DEPT, labels=("1", "2", "3")) -> dict:
    ward = bm.save_ward({"code": f"LB-{_tag()}", "department": department,
                         "cleaning_tracked": False}, actor=_admin())
    bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM housekeeping_units WHERE ward_id = ? "
                               "AND bed_label = ?", (ward_id, label)).fetchone())


# An invented name: a name holding an ordinary word ("Patient", "Admitted") is read by
# Sage's name matcher in later test files, so "the patient" there would match it.
def _admit(ward: dict, label: str, key: str | None = None):
    bed = _bed(ward["id"], label)
    resp = client.post("/api/admissions/admit", headers=_hdr(DOCTOR, key or f"lb-{_tag()}"), json={
        "name": f"Zelvani {_tag()}", "age": 40, "sex": "F", "admission_source": "er",
        "admission_type": "inpatient", "department": DEPT, "ward": ward["code"], "bed": label,
        "ward_id": ward["id"], "bed_id": bed["id"], "attending_doctor_id": "D001",
        "chief_complaint": "Fever"})
    if resp.status_code in (200, 201):
        _SEEDED.append(resp.json()["patient_id"])
    return resp


def _connect(monkeypatch, user) -> set:
    rooms: set = set()

    async def save_session(sid, session):
        return None

    async def enter_room(sid, room):
        rooms.add(room)

    monkeypatch.setattr(realtime, "_authenticate", lambda auth, environ=None: dict(user))
    monkeypatch.setattr(realtime.sio, "save_session", save_session)
    monkeypatch.setattr(realtime.sio, "enter_room", enter_room)
    assert asyncio.run(realtime.connect("sid-" + user["id"], {}, {"token": "x"})) is True
    return rooms


# ═════════════════════════════════════════════════════════════════════════════
#  1. Rooms
# ═════════════════════════════════════════════════════════════════════════════

def test_board_readers_join_their_departments_adt_room(monkeypatch, adt_on):
    clerk = _user("ward_clerk")
    assert _connect(monkeypatch, clerk) == {f"user:{clerk['id']}", f"adt:{DEPT}"}
    doctor = _user("doctor")
    assert f"adt:{DEPT}" in _connect(monkeypatch, doctor)
    admin = _admin()
    assert "adt:__all__" in _connect(monkeypatch, admin)


def test_a_nurse_joins_no_adt_room(monkeypatch, adt_on):
    nurse = _user("nurse")
    assert not any(r.startswith("adt:") for r in _connect(monkeypatch, nurse))


def test_with_the_module_off_nobody_joins_an_adt_room(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    for role in ("ward_clerk", "doctor", "admin"):
        user = _user(role, None if role == "admin" else DEPT)
        assert not any(r.startswith("adt:") for r in _connect(monkeypatch, user)), role


# ═════════════════════════════════════════════════════════════════════════════
#  2. The payload and the rooms it goes to
# ═════════════════════════════════════════════════════════════════════════════

def test_the_event_carries_the_ward_and_nothing_else(monkeypatch):
    sent: list[tuple] = []

    async def emit(event, payload, room=None, **kw):
        sent.append((event, payload, room))

    monkeypatch.setattr(realtime.sio, "emit", emit)
    asyncio.run(realtime.emit_adt_changed(["WRD-B", "WRD-A", "WRD-A"], ["Cardiology", DEPT]))
    assert [s[0] for s in sent] == ["adt:changed", "adt:changed"]
    assert [s[1] for s in sent] == [{"ward_id": "WRD-A"}, {"ward_id": "WRD-B"}]
    for _, payload, room in sent:
        assert set(payload) == {"ward_id"}
        assert room == ["adt:__all__", "adt:Cardiology", f"adt:{DEPT}"]
    sent.clear()
    asyncio.run(realtime.emit_adt_changed([], [DEPT]))
    assert sent == [("adt:changed", {"ward_id": None}, ["adt:__all__", f"adt:{DEPT}"])]


def test_nothing_is_scheduled_while_the_module_is_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    assert realtime.schedule_adt_changed(["WRD-A"], [DEPT]) is False
    assert adt_live.notify_adt_changed(ward_ids=["WRD-A"]) is False


def test_the_wards_and_departments_come_from_the_answer_and_the_record(adt_on):
    ward = _ward()
    other_dept = f"Live Other {_tag()}"
    _user("nurse", other_dept)    # clinical cover, so a ward can be set up there
    other = _ward(department=other_dept)
    answer = {"movement": {"from_ward_id": ward["id"], "to_ward_id": other["id"],
                           "from_department": DEPT, "to_department": "Cardiology"},
              "patient_name": "Never Collected"}
    wards, depts = adt_live.wards_and_departments(answer)
    assert wards == {ward["id"], other["id"]}
    assert depts == {DEPT, "Cardiology", other["department"]}


# ═════════════════════════════════════════════════════════════════════════════
#  3. After commit, from the route
# ═════════════════════════════════════════════════════════════════════════════

def test_an_admission_pushes_once_after_it_committed(adt_on, pushes):
    ward = _ward()
    before = len(pushes)
    with db_session() as db:
        committed_before = db.execute("SELECT COUNT(*) FROM patient_movements").fetchone()[0]
    resp = _admit(ward, "1")
    assert resp.status_code == 201, resp.text
    assert len(pushes) == before + 1
    push = pushes[-1]
    assert push["wards"] == [ward["id"]] and DEPT in push["departments"]
    assert push["movements_committed"] > committed_before


def test_a_bed_change_pushes_both_wards_and_a_replay_pushes_nothing(adt_on, pushes):
    home, away = _ward(), _ward()
    admitted = _admit(home, "1").json()
    key = f"bc-{_tag()}"
    body = {"bed_id": _bed(away["id"], "2")["id"], "reason": "Nearer the station",
            "expected_bed_id": _bed(home["id"], "1")["id"]}
    before = len(pushes)
    moved = client.post(f"/api/adt/admissions/{admitted['admission_id']}/bed-change",
                        headers=_hdr(DOCTOR, key), json=body)
    assert moved.status_code == 200, moved.text
    assert len(pushes) == before + 1
    assert set(pushes[-1]["wards"]) == {home["id"], away["id"]}
    again = client.post(f"/api/adt/admissions/{admitted['admission_id']}/bed-change",
                        headers=_hdr(DOCTOR, key), json=body)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert len(pushes) == before + 1


def test_a_refused_write_pushes_nothing(adt_on, pushes):
    ward = _ward()
    first = _admit(ward, "1")
    assert first.status_code == 201
    before = len(pushes)
    taken = _admit(ward, "1")
    assert taken.status_code == 409, taken.text
    assert len(pushes) == before


def test_presence_and_the_end_of_stay_push(adt_on, pushes):
    ward = _ward()
    out = _admit(ward, "1").json()
    aid = out["admission_id"]
    before = len(pushes)
    leave = client.post(f"/api/adt/admissions/{aid}/leave", headers=_hdr(DOCTOR, f"lv-{_tag()}"),
                        json={"reason": "Home for the day"})
    assert leave.status_code == 200, leave.text
    assert len(pushes) == before + 1 and pushes[-1]["wards"] == [ward["id"]]
    back = client.post(f"/api/adt/admissions/{aid}/leave-end",
                       headers=_hdr(DOCTOR, f"le-{_tag()}"), json={})
    assert back.status_code == 200, back.text
    with db_session() as db:
        mrn = db.execute("SELECT mrn FROM patients WHERE id = ?",
                         (out["patient_id"],)).fetchone()["mrn"]
    ended = client.post(f"/api/adt/admissions/{aid}/end", headers=_hdr(DOCTOR, f"en-{_tag()}"),
                        json={"end_type": "death", "confirm_mrn": mrn})
    assert ended.status_code == 200, ended.text
    assert len(pushes) == before + 3 and pushes[-1]["wards"] == [ward["id"]]


def test_with_the_module_off_the_admission_form_pushes_nothing(monkeypatch, pushes):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    before = len(pushes)
    resp = client.post("/api/admissions/admit", headers=_hdr(DOCTOR), json={
        "name": f"Ovrena {_tag()}", "age": 30, "sex": "M", "admission_source": "er",
        "admission_type": "inpatient", "department": DEPT, "ward": "Typed", "bed": "1",
        "attending_doctor_id": "D001", "chief_complaint": "Cough"})
    assert resp.status_code == 200, resp.text
    _SEEDED.append(resp.json()["patient_id"])
    assert len(pushes) == before
