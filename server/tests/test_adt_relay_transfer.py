"""The Relay agentic transfer lane through the ADT writer (adt-6c, W7-C).

A confirmed Sage transfer card used to write admissions.department with a
bare UPDATE, checked only against "any department any login carries" (a
pharmacist's department, or a ward label written as a patient login's
department, included), with no movement row and no clinical_access audit.
With Beds and admissions (ORB_ADT) on it now transfers through
services/adt/transfer.transfer_on, inside the confirm's own transaction.

What this file defends:

  1. FLAG ON, ONE TRANSACTION. A confirmed card writes one 'transfer'
     movement (reason 'Confirmed in Relay', the bed kept), one clinical_access
     'patient_transferred' row and the department change; the patient boards
     on the old ward ('boarder_placed', a case-room line, a boarder tile on the
     bed board). An unwritable clinical_access row leaves NOTHING changed: no
     department change, no movement, no booked case, no room line.
  2. RULE C. A target that is only a pharmacist's or a patient login's
     department is refused with rule C's words and nothing changes; a target
     that names no department goes to General Surgery, which must itself be
     covered. The list is the front desk's covering list, read on the
     confirm's connection.
  3. THE CONFIRM ENDPOINT runs the lane inside its own transaction (the
     relay_routes.py confirm holds an outer session, so the lane can never
     open the immediate session transfer() would).
  4. FLAG OFF: the lane is what it was (the department written in place, no
     movement, no clinical_access row); tests/test_handoff*.py pass unchanged.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import bed_master_service as bm
from services import frontdesk_service as fd
from services import sage_handoff_service as handoff
from services.adt import transfer as adt_transfer
from services.audit_service import AuditWriteError

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds D001 and N001 in it
SURGERY = "General Surgery"
DOCTOR = {"id": "D001", "role": "doctor", "department": DEPT, "name": "Dr. Test"}

_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded():
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'", [(p,) for p in _SEEDED])
    _SEEDED.clear()


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


@pytest.fixture()
def adt_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"RL{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Relay {role} {uid[-4:]}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        _ADMIN = _user("admin", None)
    return _ADMIN


_COVERED: set[str] = set()


def _cover(department: str) -> None:
    """Clinical cover for a department: a doctor and a nurse working in it."""
    if department in _COVERED:
        return
    _user("doctor", department)
    _user("nurse", department)
    _COVERED.add(department)


def _hdr(user: dict, key: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        out["Idempotency-Key"] = key
    return out


def _ward(department: str = DEPT, labels=("1", "2")) -> dict:
    ward = bm.save_ward({"code": f"RL-{_tag()}", "department": department,
                         "cleaning_tracked": False}, actor=_admin())
    bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute(
            "SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
            (ward_id, label)).fetchone())


def _one(sql: str, params: tuple = ()) -> dict | None:
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def _count(sql: str, params: tuple = ()) -> int:
    with db_session() as db:
        return db.execute(sql, params).fetchone()[0]


def _admit(ward: dict, label: str = "1", *, department: str = DEPT, user: dict = DOCTOR,
           extra: dict | None = None) -> dict:
    bed = _bed(ward["id"], label)
    body = {"name": f"Relay Patient {_tag()}", "age": 58, "sex": "M",
            "admission_source": "er", "admission_type": "inpatient",
            "department": department, "ward": ward["code"], "bed": label,
            "ward_id": ward["id"], "bed_id": bed["id"], "attending_doctor_id": "D001",
            "chief_complaint": "Abdominal pain", **(extra or {})}
    resp = client.post("/api/admissions/admit", headers=_hdr(user, f"adm-{_tag()}"), json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    _SEEDED.append(out["patient_id"])
    room = _one("SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room'",
                (out["patient_id"],))
    return {"pid": out["patient_id"], "aid": out["admission_id"], "bed_id": bed["id"],
            "ward": ward, "room": room["id"] if room else None}


def _card(p: dict, target: str, action_type: str = "transfer_to_surgery",
          state: str = "confirmed") -> dict:
    action = {"id": str(uuid.uuid4()), "room_id": p["room"], "patient_id": p["pid"],
              "action_type": action_type, "target": target,
              "rationale": "Peritonism, needs a surgical team", "confidence": 0.9,
              "state": state, "version": 1}
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale,
                                         confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action["id"], action["room_id"], action["patient_id"], action["action_type"],
             action["target"], action["rationale"], action["confidence"], action["state"],
             action["version"]))
    return action


def _adm(aid: str) -> dict:
    return _one("SELECT * FROM admissions WHERE id = ?", (aid,))


def _transfers(aid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM patient_movements WHERE admission_id = ? AND movement_type = 'transfer' "
            "ORDER BY rowid", (aid,)).fetchall()]


def _audit_count(action: str, pid: str, category: str) -> int:
    return _count("SELECT COUNT(*) FROM audit_logs WHERE action_type = ? AND patient_id = ? "
                  "AND event_category = ?", (action, pid, category))


def _surgeries(pid: str) -> int:
    return _count("SELECT COUNT(*) FROM surgeries WHERE patient_id = ?", (pid,))


def _room_lines(room: str, like: str) -> int:
    return _count("SELECT COUNT(*) FROM messages WHERE channel_id = ? AND content LIKE ?",
                  (room, like))


# ═════════════════════════════════════════════════════════════════════════════
#  1. Flag on: one transaction through the writer
# ═════════════════════════════════════════════════════════════════════════════

def test_a_confirmed_card_transfers_through_the_writer_in_one_transaction(adt_on):
    _cover(SURGERY)
    p = _admit(_ward())
    result = handoff.create_and_dispatch_handoff(_card(p, SURGERY), "D001")
    assert result["status"] == "accepted", result
    adm = _adm(p["aid"])
    assert adm["department"] == SURGERY
    # The bed is kept: the patient now boards on the medical ward.
    assert adm["bed_id"] == p["bed_id"] and adm["status"] == "active"
    moves = _transfers(p["aid"])
    assert len(moves) == 1
    move = moves[0]
    assert move["reason"] == "Confirmed in Relay"
    assert (move["from_department"], move["to_department"]) == (DEPT, SURGERY)
    assert move["from_bed_id"] == move["to_bed_id"] == p["bed_id"]
    assert '"kept_place": true' in move["detail_json"]
    assert move["moved_by"] == "D001"
    assert _audit_count("patient_transferred", p["pid"], "clinical_access") == 1
    assert _audit_count("boarder_placed", p["pid"], "clinical_access") == 1
    assert _audit_count("patient_transferred", p["pid"], "patient_movement") == 1
    assert _room_lines(p["room"], "Placed as a boarder%") == 1
    assert _room_lines(p["room"], "SYSTEM: Patient transfer to General Surgery%") == 1
    assert _surgeries(p["pid"]) == 1
    # The bed board shows a boarder: named, 'Boarder from General Surgery', to
    # an account that may see the patient; 'Boarder' with nobody named to the
    # medical team that just handed the chart over.
    board = bm.bed_board(actor=_admin(), ward_id=p["ward"]["id"])
    tile = next(t for w in board["wards"] for t in w["tiles"] if t["bed_id"] == p["bed_id"])
    assert tile["occupant"]["boarder"] is True
    assert tile["occupant"]["boarder_from"] == SURGERY
    medical = bm.bed_board(actor=DOCTOR, ward_id=p["ward"]["id"])
    tile = next(t for w in medical["wards"] for t in w["tiles"] if t["bed_id"] == p["bed_id"])
    assert tile["status"] == "boarder" and tile["occupant"] is None


def test_an_unwritable_clinical_access_row_leaves_nothing_changed(adt_on, monkeypatch):
    _cover(SURGERY)
    p = _admit(_ward())
    real = adt_transfer.audit_on

    def _refuse(db, **kwargs):
        if kwargs.get("category") == "clinical_access" and kwargs.get("action") == "patient_transferred":
            raise AuditWriteError("audit store unavailable")
        return real(db, **kwargs)

    monkeypatch.setattr(adt_transfer, "audit_on", _refuse)
    result = handoff.create_and_dispatch_handoff(_card(p, SURGERY), "D001")
    assert result["status"] == "failed"
    adm = _adm(p["aid"])
    assert adm["department"] == DEPT and adm["bed_id"] == p["bed_id"]
    assert _transfers(p["aid"]) == []
    assert _surgeries(p["pid"]) == 0
    assert _room_lines(p["room"], "SYSTEM: Patient transfer%") == 0
    assert _room_lines(p["room"], "Placed as a boarder%") == 0
    assert _audit_count("boarder_placed", p["pid"], "clinical_access") == 0


# ═════════════════════════════════════════════════════════════════════════════
#  2. Rule C
# ═════════════════════════════════════════════════════════════════════════════

def test_a_target_that_is_only_a_pharmacists_department_is_refused(adt_on):
    pharmacy = f"Pharmacy Stores {_tag()}"
    _user("pharmacist", pharmacy)
    p = _admit(_ward())
    result = handoff.create_and_dispatch_handoff(_card(p, pharmacy), "D001")
    assert result["status"] == "failed"
    assert result["error_summary"] == fd.uncovered_department_words(pharmacy)
    assert _adm(p["aid"])["department"] == DEPT
    assert _transfers(p["aid"]) == []
    assert _surgeries(p["pid"]) == 0


def test_a_target_that_is_only_a_patient_logins_department_is_refused(adt_on):
    ward_label = f"Ward Label {_tag()}"
    _user("patient", ward_label)
    p = _admit(_ward())
    result = handoff.create_and_dispatch_handoff(_card(p, ward_label), "D001")
    assert result["status"] == "failed"
    assert "is not a department with clinical cover" in result["error_summary"]
    assert _adm(p["aid"])["department"] == DEPT
    assert _transfers(p["aid"]) == []


def test_a_target_naming_no_department_goes_to_covered_general_surgery(adt_on):
    _cover(SURGERY)
    p = _admit(_ward())
    result = handoff.create_and_dispatch_handoff(_card(p, "Exploratory Laparotomy"), "D001")
    assert result["status"] == "accepted", result
    assert _adm(p["aid"])["department"] == SURGERY
    surgery = _one("SELECT procedure_name FROM surgeries WHERE id = ?",
                   (result["downstream_ref_id"],))
    assert surgery["procedure_name"] == "Exploratory Laparotomy"


def test_with_no_cover_for_the_default_department_nothing_changes(adt_on, monkeypatch):
    uncovered = f"Uncovered Surgery {_tag()}"
    monkeypatch.setattr(handoff, "_DEFAULT_SURGICAL_DEPARTMENT", uncovered)
    p = _admit(_ward())
    result = handoff.create_and_dispatch_handoff(_card(p, "theatre"), "D001")
    assert result["status"] == "failed"
    assert result["error_summary"] == fd.uncovered_department_words(uncovered)
    assert _adm(p["aid"])["department"] == DEPT
    assert _surgeries(p["pid"]) == 0


def test_a_patient_already_treated_there_gets_the_case_and_no_movement(adt_on):
    _cover(SURGERY)
    surgical = _ward(department=SURGERY)
    p = _admit(surgical, department=SURGERY)
    result = handoff.create_and_dispatch_handoff(_card(p, SURGERY), "D001")
    assert result["status"] == "accepted", result
    assert _transfers(p["aid"]) == []
    assert _audit_count("patient_transferred", p["pid"], "clinical_access") == 0
    assert _surgeries(p["pid"]) == 1


def test_the_lane_reads_the_front_desks_covering_list(adt_on):
    _cover(SURGERY)
    _user("pharmacist", f"Pharmacy {_tag()}")
    with db_session() as db:
        on_connection = handoff._covering_departments_on(db)
    assert on_connection == fd.list_covering_departments()


def test_the_lane_refuses_to_run_outside_a_transaction(adt_on):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with pytest.raises(RuntimeError):
        handoff._transfer_through_adt_on(conn, action={"id": "x"}, actor_id="D001",
                                         admission_id="A", raw_target=SURGERY, named=set())


# ═════════════════════════════════════════════════════════════════════════════
#  3. The confirm endpoint
# ═════════════════════════════════════════════════════════════════════════════

def test_the_relay_confirm_runs_the_lane_inside_its_transaction(adt_on):
    _cover(SURGERY)
    p = _admit(_ward())
    assert p["room"], "the admission opens a case room"
    card = _card(p, SURGERY, state="suggested")
    resp = client.post(f"/api/relay/channels/{p['room']}/sage_actions/{card['id']}/transition",
                       headers=_hdr(DOCTOR), json={"state": "confirmed", "version": 1})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"]["state"] == "confirmed"
    assert body["handoff"]["status"] == "accepted"
    assert _adm(p["aid"])["department"] == SURGERY
    assert len(_transfers(p["aid"])) == 1
    assert _audit_count("patient_transferred", p["pid"], "clinical_access") == 1


# ═════════════════════════════════════════════════════════════════════════════
#  4. Flag off
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_the_lane_writes_the_department_in_place_as_before(adt_off):
    pid = f"PAT-RL-{_tag()}"
    aid = f"ADM-{pid}"
    room = f"case-{pid}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, diagnosis, status) "
                   "VALUES (?, 'Relay Off', ?, 'Appendicitis', 'admitted')", (pid, f"MRN-{pid}"))
        db.execute("INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                   (room, f"{pid} room", pid))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')", (aid, pid, DEPT))
    _SEEDED.append(pid)
    result = handoff.create_and_dispatch_handoff(
        _card({"pid": pid, "room": room}, SURGERY), "D001")
    assert result["status"] == "accepted", result
    assert _adm(aid)["department"] == SURGERY
    assert _count("SELECT COUNT(*) FROM patient_movements WHERE admission_id = ?", (aid,)) == 0
    assert _audit_count("patient_transferred", pid, "clinical_access") == 0
