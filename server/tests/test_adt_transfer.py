"""Transfers and bed changes through the bed master (adt-6a, W6-C).

What this file defends:

  1. FLAG OFF CHANGES NOTHING. With ORB_ADT off every new route answers 404,
     the front desk's transfer is its old lane (one post-commit
     clinical_access row, the movement written by frontdesk_service), and a
     roster update writes no movement.
  2. RULES A, B AND C, VERBATIM. A transfer keeps the same admission id with
     admissions.status 'active' and patients.status 'admitted'; it moves the
     department, ward and bed of that row; a department nobody covers is
     refused with the front desk's own words; STRICT HANDOVER: the sending
     department gets 404 on the chart after the commit.
  3. THE BED LOCK AND THE SCREEN'S STATE. A transfer into an occupied bed is
     409 bed_occupied with nothing written; a stale expected_bed_id is 409
     state_changed; a replayed Idempotency-Key returns the first movement and
     writes nothing; a key reused for another change is 409.
  4. AUDIT INSIDE THE TRANSACTION. A cross-department move writes exactly one
     clinical_access 'patient_transferred' row, through the front desk's
     delegated route too (it skips its own post-commit row); an unwritable
     clinical_access row rolls the whole move back (503).
  5. SCOPE AND PERMISSION. A doctor in another department gets 404 (audited);
     a nurse gets 403; the ward clerk may change a bed (adt.bed) but never
     transfer (no adt.transfer; ruling ADT-S8 held).
  6. THE PLACE. A boarder placement needs the acknowledgement (words name who
     cannot open the chart), then writes boarder_placed and one case-room
     line; 'keep the current bed' changes the department only; 'no bed yet'
     leaves the patient awaiting a bed on the receiving ward; the cleaning
     rule (ADT-S2 (c)) applies to the bed moved into.
  7. BED CHANGE keeps the treating department, needs expected_bed_id and a
     reason, and refuses a patient with no bed recorded.
  8. ROSTER. A roster file's ward and bed update writes a movement through
     the writer; a department change is a transfer (rule C); a move the file
     cannot make (a boarder) is left as it was and reported.
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
from services import housekeeping_service as hk
from services import roster_import
from services.adt import transfer as adt_transfer
from services.audit_service import AuditWriteError

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds D001 and N001 in it
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
    uid = f"TR{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Transfer {role} {uid[-4:]}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department, "name": f"Transfer {role}"}


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


def _ward(department: str = DEPT, labels=("1", "2", "3"), *, tracked: bool = False) -> dict:
    ward = bm.save_ward({"code": f"TR-{_tag()}", "department": department,
                         "cleaning_tracked": tracked}, actor=_admin())
    if labels:
        bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute(
            "SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
            (ward_id, label)).fetchone())


def _ready(bed_id: str) -> None:
    hk.transition_unit(bed_id, "cleaning_in_progress", actor_user_id="TRHK", reason="test")
    hk.transition_unit(bed_id, "vacant_ready", actor_user_id="TRHK", reason="test")


def _one(sql: str, params: tuple = ()) -> dict | None:
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _admit(ward: dict, label: str, *, user: dict = DOCTOR, department: str = DEPT,
           attending: str = "D001") -> dict:
    """An admission into a master bed through the admission route (module on)."""
    bed = _bed(ward["id"], label)
    body = {"name": f"Transfer Patient {_tag()}", "age": 52, "sex": "M",
            "admission_source": "er", "admission_type": "inpatient",
            "department": department, "ward": ward["code"], "bed": label,
            "ward_id": ward["id"], "bed_id": bed["id"],
            "attending_doctor_id": attending, "chief_complaint": "Breathless"}
    resp = client.post("/api/admissions/admit", headers=_hdr(user, f"adm-{_tag()}"), json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    _SEEDED.append(out["patient_id"])
    return {"pid": out["patient_id"], "aid": out["admission_id"], "bed_id": bed["id"]}


def _receiving() -> dict:
    """A receiving department with clinical cover (a doctor and a nurse), and a
    ward of its own with three beds."""
    dept = f"Recv Dept {_tag()}"
    doctor = _user("doctor", dept)
    nurse = _user("nurse", dept)
    ward = _ward(department=dept)
    return {"dept": dept, "doctor": doctor, "nurse": nurse, "ward": ward}


def _movements(admission_id: str) -> list[dict]:
    return _all("SELECT * FROM patient_movements WHERE admission_id = ? ORDER BY rowid",
                (admission_id,))


def _audits(action: str, patient_id: str, category: str | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ?"
    params: tuple = (action, patient_id)
    if category:
        sql += " AND event_category = ?"
        params += (category,)
    return _all(sql, params)


def _transfer(body: dict, *, user: dict = DOCTOR, key: str | None = None):
    return client.post("/api/adt/transfers", headers=_hdr(user, key or f"tr-{_tag()}"), json=body)


def _detail(resp) -> dict:
    return resp.json()["detail"]


# ═════════════════════════════════════════════════════════════════════════════
#  1. Flag off
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_the_new_routes_answer_404(adt_off):
    for method, path in (("get", "/api/adt/move-options"),
                         ("post", "/api/adt/transfers"),
                         ("post", "/api/adt/admissions/ADM-NOPE/bed-change")):
        resp = getattr(client, method)(path, headers=_hdr(DOCTOR, "k-off"), **(
            {"json": {}} if method == "post" else {}))
        assert resp.status_code == 404, (path, resp.text)


def test_flag_off_the_front_desk_transfer_is_its_old_lane(adt_off):
    recv = _receiving()
    with db_session() as db:
        pid = f"PAT-TRO{_tag()}"
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, 'Old Lane Patient', ?, 'admitted', 'GM Typed', '4')",
                   (pid, f"MRN-{pid}"))
        aid = f"ADM-TRO{_tag()}"
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, 'GM Typed', '4', 'active', 'D001')",
                   (aid, pid, DEPT))
    _SEEDED.append(pid)
    resp = client.post("/api/frontdesk/transfers", headers=_hdr(DOCTOR), json={
        "patient_id": pid, "to_department": recv["dept"], "to_ward": "Typed Bay",
        "to_bed": "7", "reason": "Escalation", "boarder_acknowledged": True,
        "expected_bed_id": "ignored-while-off"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    # The flag-off answer: the movement without the ledger's adt columns.
    assert "to_bed_id" not in out["movement"] and "replayed" not in out
    adm = _one("SELECT department, ward, bed, ward_id, bed_id FROM admissions WHERE id = ?", (aid,))
    assert adm == {"department": recv["dept"], "ward": "Typed Bay", "bed": "7",
                   "ward_id": None, "bed_id": None}
    assert len(_audits("patient_transferred", pid, "clinical_access")) == 1
    assert _all("SELECT * FROM audit_logs WHERE event_category = 'patient_movement' "
                "AND patient_id = ?", (pid,)) == []


def test_flag_on_the_old_service_lane_refuses_to_write(adt_on):
    with pytest.raises(fd.FrontDeskError) as exc:
        fd.transfer_patient(patient_id="PAT-ANY", to_department=DEPT, reason="x")
    assert "Beds and admissions is switched on" in str(exc.value)


# ═════════════════════════════════════════════════════════════════════════════
#  2. Rules A, B, C and strict handover
# ═════════════════════════════════════════════════════════════════════════════

def test_a_transfer_keeps_the_admission_and_hands_the_chart_over(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    target = _bed(recv["ward"]["id"], "2")
    # Before: the sending doctor sees the chart, the receiving doctor does not.
    assert client.get(f"/api/vigil/patients/{p['pid']}", headers=_hdr(DOCTOR)).status_code == 200
    assert client.get(f"/api/vigil/patients/{p['pid']}",
                      headers=_hdr(recv["doctor"])).status_code == 404

    resp = _transfer({"admission_id": p["aid"], "to_department": recv["dept"].lower(),
                      "bed_id": target["id"], "expected_bed_id": p["bed_id"],
                      "reason": "Needs closer monitoring"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    # Rule C: the department typed in lower case is stored in its covered spelling.
    assert out["current_department"] == recv["dept"]
    # Rule B, in the answer and in the rows.
    assert out["admission_id"] == p["aid"] and out["admission_status"] == "active"
    assert out["patient_status"] == "admitted" and out["department_changed"] is True
    adm = _one("SELECT * FROM admissions WHERE id = ?", (p["aid"],))
    assert (adm["status"], adm["department"], adm["ward_id"], adm["bed_id"]) == \
        ("active", recv["dept"], recv["ward"]["id"], target["id"])
    assert (adm["ward"], adm["bed"]) == (recv["ward"]["code"], "2")
    pat = _one("SELECT status, ward, bed FROM patients WHERE id = ?", (p["pid"],))
    assert pat == {"status": "admitted", "ward": recv["ward"]["code"], "bed": "2"}
    assert len(_all("SELECT id FROM admissions WHERE patient_id = ?", (p["pid"],))) == 1
    moves = _movements(p["aid"])
    assert [m["movement_type"] for m in moves] == ["admission", "transfer"]
    move = moves[-1]
    assert (move["from_department"], move["to_department"]) == (DEPT, recv["dept"])
    assert (move["from_bed_id"], move["to_bed_id"]) == (p["bed_id"], target["id"])
    # STRICT HANDOVER: the sending department loses the chart at once.
    assert client.get(f"/api/vigil/patients/{p['pid']}", headers=_hdr(DOCTOR)).status_code == 404
    assert client.get(f"/api/vigil/patients/{p['pid']}",
                      headers=_hdr(recv["doctor"])).status_code == 200


def test_a_department_with_no_cover_is_refused_with_rule_cs_words(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    nowhere = f"Ward 7 {_tag()}"
    resp = _transfer({"admission_id": p["aid"], "to_department": nowhere, "keep_location": True,
                      "expected_bed_id": p["bed_id"], "reason": "Typo"})
    assert resp.status_code == 422
    detail = _detail(resp)
    assert detail["error"] == "department_not_covered"
    assert detail["message"] == fd.uncovered_department_words(nowhere)
    assert _one("SELECT department FROM admissions WHERE id = ?", (p["aid"],))["department"] == DEPT
    assert [m["movement_type"] for m in _movements(p["aid"])] == ["admission"]


def test_a_department_covered_only_by_a_pharmacist_is_not_cover(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    pharmacy_only = f"Pharmacy Only {_tag()}"
    _user("pharmacist", pharmacy_only)
    resp = _transfer({"admission_id": p["aid"], "to_department": pharmacy_only,
                      "keep_location": True, "expected_bed_id": p["bed_id"], "reason": "x"})
    assert resp.status_code == 422 and _detail(resp)["error"] == "department_not_covered"


# ═════════════════════════════════════════════════════════════════════════════
#  3. The bed lock, the screen's state, replays
# ═════════════════════════════════════════════════════════════════════════════

def test_a_transfer_into_an_occupied_bed_is_409_and_writes_nothing(adt_on):
    recv = _receiving()
    _admit(recv["ward"], "1", user=recv["doctor"], department=recv["dept"],
           attending=recv["doctor"]["id"])
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    resp = _transfer({"admission_id": p["aid"], "to_department": recv["dept"],
                      "bed_id": _bed(recv["ward"]["id"], "1")["id"],
                      "expected_bed_id": p["bed_id"], "reason": "Escalation"})
    assert resp.status_code == 409
    assert _detail(resp)["error"] == "bed_occupied" and _detail(resp)["extra_bed_offered"] is True
    adm = _one("SELECT department, bed_id FROM admissions WHERE id = ?", (p["aid"],))
    assert adm == {"department": DEPT, "bed_id": p["bed_id"]}
    assert [m["movement_type"] for m in _movements(p["aid"])] == ["admission"]


def test_an_extra_bed_beside_the_occupied_one_takes_the_patient(adt_on):
    recv = _receiving()
    _admit(recv["ward"], "1", user=recv["doctor"], department=recv["dept"],
           attending=recv["doctor"]["id"])
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    resp = _transfer({"admission_id": p["aid"], "to_department": recv["dept"],
                      "bed_id": _bed(recv["ward"]["id"], "1")["id"], "extra_bed": True,
                      "expected_bed_id": p["bed_id"], "reason": "Ward full"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_bed"] == "1A"


def test_a_stale_expected_bed_is_409_state_changed(adt_on):
    home = _ward(labels=("1", "2"))
    p = _admit(home, "1")
    recv = _receiving()
    resp = _transfer({"admission_id": p["aid"], "to_department": recv["dept"],
                      "bed_id": _bed(recv["ward"]["id"], "3")["id"],
                      "expected_bed_id": _bed(home["id"], "2")["id"], "reason": "Escalation"})
    assert resp.status_code == 409 and _detail(resp)["error"] == "state_changed"
    assert [m["movement_type"] for m in _movements(p["aid"])] == ["admission"]


def test_expected_bed_and_the_key_are_required(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    body = {"admission_id": p["aid"], "to_department": recv["dept"], "keep_location": True,
            "reason": "x"}
    resp = _transfer(body)
    assert resp.status_code == 422 and _detail(resp)["error"] == "expected_bed_required"
    resp = client.post("/api/adt/transfers", headers=_hdr(DOCTOR),
                       json={**body, "expected_bed_id": p["bed_id"]})
    assert resp.status_code == 422 and _detail(resp)["error"] == "idempotency_key_required"


def test_a_replayed_key_returns_the_first_movement(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    key = f"replay-{_tag()}"
    body = {"admission_id": p["aid"], "to_department": recv["dept"],
            "bed_id": _bed(recv["ward"]["id"], "1")["id"], "expected_bed_id": p["bed_id"],
            "reason": "Escalation"}
    first = _transfer(body, key=key)
    assert first.status_code == 200, first.text
    # The sender has handed the chart over, yet their retry is answered as a
    # replay (with no patient field), not as a 404 that says nothing happened.
    again = _transfer(body, key=key)
    assert again.status_code == 200, again.text
    replay = again.json()
    assert replay["replayed"] is True
    assert replay["movement"]["id"] == first.json()["movement"]["id"]
    assert "patient_id" not in replay and "patient_name" not in replay
    assert [m["movement_type"] for m in _movements(p["aid"])] == ["admission", "transfer"]
    assert len(_audits("patient_transferred", p["pid"], "clinical_access")) == 1
    # The same key from another person in scope is refused, never replayed.
    reused = _transfer(body, user=recv["doctor"], key=key)
    assert reused.status_code == 409 and _detail(reused)["error"] == "idempotency_key_reused"
    assert [m["movement_type"] for m in _movements(p["aid"])] == ["admission", "transfer"]


def test_an_in_scope_replay_through_the_service_returns_the_full_first_answer(adt_on):
    home = _ward(labels=("1", "2"))
    p = _admit(home, "1")
    key = f"svc-{_tag()}"
    kwargs = dict(actor=DOCTOR, reason="Window bed", bed_text="2",
                  expected_bed_id=p["bed_id"], idempotency_key=key)
    first = adt_transfer.change_bed(p["aid"], **kwargs)
    again = adt_transfer.change_bed(p["aid"], **kwargs)
    assert again["replayed"] is True and again["movement"]["id"] == first["movement"]["id"]
    assert again["current_bed"] == "2"


# ═════════════════════════════════════════════════════════════════════════════
#  4. Audit inside the transaction
# ═════════════════════════════════════════════════════════════════════════════

def test_the_front_desk_route_delegates_and_writes_one_clinical_access_row(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    resp = client.post("/api/frontdesk/transfers", headers=_hdr(DOCTOR), json={
        "patient_id": p["pid"], "to_department": recv["dept"], "to_ward": recv["ward"]["code"],
        "to_bed": "3", "reason": "Escalation of care", "idempotency_key": f"fd-{_tag()}"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    # A superset of the front desk's answer.
    for key in ("movement", "patient_id", "patient_name", "admission_id", "admission_status",
                "patient_status", "current_department", "current_ward", "current_bed"):
        assert key in out
    assert out["admission_status"] == "active" and out["current_bed"] == "3"
    assert out["bed_id"] == _bed(recv["ward"]["id"], "3")["id"]
    rows = _audits("patient_transferred", p["pid"], "clinical_access")
    assert len(rows) == 1, "the delegated route must not add its own post-commit row"
    assert len(_audits("patient_transferred", p["pid"], "patient_movement")) == 1


def test_the_front_desk_route_with_no_ward_or_bed_keeps_the_place(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    refused = client.post("/api/frontdesk/transfers", headers=_hdr(DOCTOR), json={
        "patient_id": p["pid"], "to_department": recv["dept"], "reason": "Accepted, no bed yet"})
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "boarder_acknowledgement_required"
    resp = client.post("/api/frontdesk/transfers", headers=_hdr(DOCTOR), json={
        "patient_id": p["pid"], "to_department": recv["dept"], "reason": "Accepted, no bed yet",
        "boarder_acknowledged": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["bed_id"] == p["bed_id"] and resp.json()["boarder"] is True


def test_an_unwritable_clinical_access_row_rolls_the_move_back(adt_on, monkeypatch):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    real = adt_transfer.audit_on

    def _refuse(db, **kwargs):
        if kwargs.get("category") == "clinical_access":
            raise AuditWriteError("audit chain unavailable")
        return real(db, **kwargs)

    monkeypatch.setattr(adt_transfer, "audit_on", _refuse)
    resp = _transfer({"admission_id": p["aid"], "to_department": recv["dept"],
                      "bed_id": _bed(recv["ward"]["id"], "1")["id"],
                      "expected_bed_id": p["bed_id"], "reason": "Escalation"})
    assert resp.status_code == 503 and _detail(resp)["error"] == "audit_unavailable"
    adm = _one("SELECT department, bed_id, ward_id FROM admissions WHERE id = ?", (p["aid"],))
    assert adm == {"department": DEPT, "bed_id": p["bed_id"], "ward_id": home["id"]}
    pat = _one("SELECT ward, bed FROM patients WHERE id = ?", (p["pid"],))
    assert pat == {"ward": home["code"], "bed": "1"}
    assert [m["movement_type"] for m in _movements(p["aid"])] == ["admission"]
    assert _audits("patient_transferred", p["pid"]) == []


# ═════════════════════════════════════════════════════════════════════════════
#  5. Scope and permission
# ═════════════════════════════════════════════════════════════════════════════

def test_a_doctor_in_another_department_gets_404_and_it_is_audited(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    resp = _transfer({"admission_id": p["aid"], "to_department": recv["dept"],
                      "keep_location": True, "expected_bed_id": p["bed_id"],
                      "boarder_acknowledged": True, "reason": "Taking over"},
                     user=recv["doctor"])
    assert resp.status_code == 404
    assert _one("SELECT department FROM admissions WHERE id = ?", (p["aid"],))["department"] == DEPT
    denied = _all("SELECT * FROM audit_logs WHERE action_type = 'patient_scope_denied' "
                  "AND patient_id = ? AND actor_user_id = ?", (p["pid"], recv["doctor"]["id"]))
    assert len(denied) == 1
    bed_change = client.post(f"/api/adt/admissions/{p['aid']}/bed-change",
                             headers=_hdr(recv["doctor"], f"bc-{_tag()}"),
                             json={"bed": "2", "expected_bed_id": p["bed_id"], "reason": "x"})
    assert bed_change.status_code == 404


def test_a_nurse_cannot_move_a_patient(adt_on):
    home = _ward(labels=("1", "2"))
    p = _admit(home, "1")
    nurse = {"id": "N001", "role": "nurse"}
    assert client.get("/api/adt/move-options", headers=_hdr(nurse)).status_code == 403
    assert _transfer({"admission_id": p["aid"], "to_department": DEPT, "keep_location": True,
                      "expected_bed_id": p["bed_id"], "reason": "x"}, user=nurse).status_code == 403
    resp = client.post(f"/api/adt/admissions/{p['aid']}/bed-change",
                       headers=_hdr(nurse, f"bc-{_tag()}"),
                       json={"bed": "2", "expected_bed_id": p["bed_id"], "reason": "x"})
    assert resp.status_code == 403


def test_the_ward_clerk_changes_a_bed_but_never_transfers(adt_on):
    home = _ward(labels=("1", "2"))
    p = _admit(home, "1")
    clerk = _user("ward_clerk", DEPT)
    recv = _receiving()
    refused = _transfer({"admission_id": p["aid"], "to_department": recv["dept"],
                         "keep_location": True, "expected_bed_id": p["bed_id"], "reason": "x"},
                        user=clerk)
    assert refused.status_code == 403
    resp = client.post(f"/api/adt/admissions/{p['aid']}/bed-change",
                       headers=_hdr(clerk, f"bc-{_tag()}"),
                       json={"bed": "2", "expected_bed_id": p["bed_id"],
                             "reason": "Nearer the nursing station"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["current_department"] == DEPT and out["current_bed"] == "2"
    assert out["movement"]["movement_type"] == "bed_change"
    assert _audits("patient_transferred", p["pid"]) == []
    assert len(_audits("bed_changed", p["pid"], "patient_movement")) == 1
    options = client.get("/api/adt/move-options", headers=_hdr(clerk))
    assert options.status_code == 200


def test_move_options_carry_no_patient_data(adt_on):
    home = _ward(labels=("1", "2"))
    p = _admit(home, "1")
    resp = client.get("/api/adt/move-options", headers=_hdr(DOCTOR))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert set(out) == {"master", "departments"}
    assert DEPT in out["departments"]
    assert set(out["master"]) == {"wards", "authorisers", "self_authoriser", "backtime_hours"}
    text = resp.text
    name = _one("SELECT name, mrn FROM patients WHERE id = ?", (p["pid"],))
    assert p["pid"] not in text and name["name"] not in text and name["mrn"] not in text
    ward = next(w for w in out["master"]["wards"] if w["id"] == home["id"])
    taken = next(b for b in ward["beds"] if b["bed_label"] == "1")
    assert taken["occupied"] is True and taken["selectable"] is False


# ═════════════════════════════════════════════════════════════════════════════
#  6. The place: boarder, keep, awaiting, cleaning rule
# ═════════════════════════════════════════════════════════════════════════════

def test_keeping_the_bed_after_a_department_change_needs_the_boarder_acknowledgement(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    body = {"admission_id": p["aid"], "to_department": recv["dept"], "keep_location": True,
            "expected_bed_id": p["bed_id"], "reason": "Accepted, waiting for a bed"}
    refused = _transfer(body)
    assert refused.status_code == 409
    detail = _detail(refused)
    assert detail["error"] == "boarder_acknowledgement_required"
    assert detail["message"] == (f"Nurses on {home['code']} ({DEPT}) will not be able to open "
                                 f"this chart. The {recv['dept']} team keeps it.")
    assert _one("SELECT department FROM admissions WHERE id = ?", (p["aid"],))["department"] == DEPT

    resp = _transfer({**body, "boarder_acknowledged": True})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["boarder"] is True and out["bed_id"] == p["bed_id"]
    assert out["current_department"] == recv["dept"]
    assert len(_audits("boarder_placed", p["pid"], "clinical_access")) == 1
    assert len(_audits("patient_transferred", p["pid"], "clinical_access")) == 1
    lines = _all("SELECT content FROM messages m JOIN channels c ON c.id = m.channel_id "
                 "WHERE c.patient_id = ? AND m.content LIKE 'Placed as a boarder%'", (p["pid"],))
    assert len(lines) == 1
    move = _movements(p["aid"])[-1]
    assert move["from_bed_id"] == move["to_bed_id"] == p["bed_id"]


def test_no_bed_yet_leaves_the_patient_awaiting_a_bed_on_the_receiving_ward(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    resp = _transfer({"admission_id": p["aid"], "to_department": recv["dept"],
                      "ward_id": recv["ward"]["id"], "awaiting_bed": True,
                      "expected_bed_id": p["bed_id"], "reason": "Moved to the ICU corridor"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["awaiting_bed"] is True and out["bed_id"] is None
    assert out["current_ward"] == recv["ward"]["code"]
    adm = _one("SELECT bed, bed_id FROM admissions WHERE id = ?", (p["aid"],))
    assert adm == {"bed": None, "bed_id": None}
    with_bed = _transfer({"admission_id": p["aid"], "to_department": DEPT, "awaiting_bed": True,
                          "bed": "1", "ward_id": home["id"], "expected_bed_id": None,
                          "reason": "x"}, user=recv["doctor"])
    assert with_bed.status_code == 422 and _detail(with_bed)["error"] == "awaiting_bed_with_bed"


def test_a_bed_not_recorded_clean_needs_a_reason_on_a_tracked_ward(adt_on, monkeypatch):
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    dept = f"Clean Dept {_tag()}"
    _user("doctor", dept)
    ward = _ward(department=dept, labels=("1",), tracked=True)
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    body = {"admission_id": p["aid"], "to_department": dept,
            "bed_id": _bed(ward["id"], "1")["id"], "expected_bed_id": p["bed_id"],
            "reason": "Escalation"}
    refused = _transfer(body)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "bed_not_clean_reason_required"
    resp = _transfer({**body, "not_clean_reason": "Only bed free tonight"})
    assert resp.status_code == 200, resp.text
    move = _movements(p["aid"])[-1]
    assert '"not_clean_reason": "Only bed free tonight"' in move["detail_json"]


# ═════════════════════════════════════════════════════════════════════════════
#  7. Bed change
# ═════════════════════════════════════════════════════════════════════════════

def test_a_bed_change_keeps_the_department_and_needs_its_fields(adt_on):
    home = _ward(labels=("1", "2", "3"))
    p = _admit(home, "1")
    path = f"/api/adt/admissions/{p['aid']}/bed-change"
    no_expected = client.post(path, headers=_hdr(DOCTOR, f"bc-{_tag()}"),
                              json={"bed": "2", "reason": "x"})
    assert no_expected.status_code == 422
    assert _detail(no_expected)["error"] == "expected_bed_required"
    no_reason = client.post(path, headers=_hdr(DOCTOR, f"bc-{_tag()}"),
                            json={"bed": "2", "expected_bed_id": p["bed_id"]})
    assert no_reason.status_code == 422 and _detail(no_reason)["error"] == "reason_required"
    stale = client.post(path, headers=_hdr(DOCTOR, f"bc-{_tag()}"),
                        json={"bed": "2", "expected_bed_id": None, "reason": "x"})
    assert stale.status_code == 409 and _detail(stale)["error"] == "state_changed"
    resp = client.post(path, headers=_hdr(DOCTOR, f"bc-{_tag()}"),
                       json={"bed_id": _bed(home["id"], "3")["id"],
                             "expected_bed_id": p["bed_id"], "reason": "Window bed"})
    assert resp.status_code == 200, resp.text
    adm = _one("SELECT department, bed FROM admissions WHERE id = ?", (p["aid"],))
    assert adm == {"department": DEPT, "bed": "3"}


def test_a_bed_change_for_a_patient_awaiting_a_bed_is_refused(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    moved = _transfer({"admission_id": p["aid"], "to_department": recv["dept"],
                       "ward_id": recv["ward"]["id"], "awaiting_bed": True,
                       "expected_bed_id": p["bed_id"], "reason": "Corridor"})
    assert moved.status_code == 200, moved.text
    resp = client.post(f"/api/adt/admissions/{p['aid']}/bed-change",
                       headers=_hdr(recv["doctor"], f"bc-{_tag()}"),
                       json={"bed": "1", "expected_bed_id": None, "reason": "x"})
    assert resp.status_code == 409 and _detail(resp)["error"] == "no_bed_to_change"


# ═════════════════════════════════════════════════════════════════════════════
#  8. Roster
# ═════════════════════════════════════════════════════════════════════════════

_HEADER = "MRN,Name,Age,Sex,Ward,Bed,Diagnosis,Department"
_MAPPING = {"mrn": "MRN", "name": "Name", "age": "Age", "sex": "Sex", "ward": "Ward",
            "bed": "Bed", "diagnosis": "Diagnosis", "department": "Department"}


def _roster(mrn: str, ward: str, bed: str, dept: str) -> bytes:
    return f"{_HEADER}\n{mrn},Roster Rao,44,F,{ward},{bed},Sepsis,{dept}\n".encode()


def _roster_patient(mrn: str) -> tuple[str, str]:
    pid = _one("SELECT id FROM patients WHERE mrn = ?", (mrn,))["id"]
    _SEEDED.append(pid)
    aid = _one("SELECT id FROM admissions WHERE patient_id = ? AND status = 'active'", (pid,))["id"]
    return pid, aid


def test_a_roster_update_writes_a_movement_through_the_writer(adt_on):
    admin = _admin()
    ward = _ward(labels=("1", "2"))
    mrn = f"TRR{_tag()}"
    roster_import.apply_import(_roster(mrn, ward["code"], "1", DEPT), _MAPPING,
                               actor_user_id=admin["id"])
    pid, aid = _roster_patient(mrn)
    res = roster_import.apply_import(_roster(mrn, ward["code"].lower(), "2", DEPT), _MAPPING,
                                     actor_user_id=admin["id"])
    assert res["admissions"]["updated"] == 1 and res["admissions"]["update_refused"] == 0
    adm = _one("SELECT ward, bed, ward_id, bed_id FROM admissions WHERE id = ?", (aid,))
    assert adm == {"ward": ward["code"], "bed": "2", "ward_id": ward["id"],
                   "bed_id": _bed(ward["id"], "2")["id"]}
    pat = _one("SELECT ward, bed FROM patients WHERE id = ?", (pid,))
    assert pat == {"ward": ward["code"], "bed": "2"}
    moves = _movements(aid)
    assert moves and moves[-1]["movement_type"] == "bed_change"
    assert '"door": "roster_import"' in moves[-1]["detail_json"]
    assert len(_audits("bed_changed", pid, "patient_movement")) == 1


def test_a_roster_department_change_is_a_transfer_under_rule_c(adt_on):
    admin = _admin()
    mrn = f"TRR{_tag()}"
    roster_import.apply_import(_roster(mrn, "TR Typed", "5", DEPT), _MAPPING,
                               actor_user_id=admin["id"])
    pid, aid = _roster_patient(mrn)
    nowhere = f"Nobody Covers {_tag()}"
    refused = roster_import.apply_import(_roster(mrn, "TR Typed", "5", nowhere), _MAPPING,
                                         actor_user_id=admin["id"])
    assert refused["admissions"]["update_refused"] == 1
    assert _one("SELECT department FROM admissions WHERE id = ?", (aid,))["department"] == DEPT
    row = next(r for r in refused["rows"] if r.get("mrn") == mrn)
    assert row["admission"]["refusal"]["code"] == "department_not_covered"

    recv_dept = f"Roster Recv {_tag()}"
    _user("doctor", recv_dept)
    moved = roster_import.apply_import(_roster(mrn, "TR Typed", "5", recv_dept), _MAPPING,
                                       actor_user_id=admin["id"])
    assert moved["admissions"]["updated"] == 1, moved["admissions"]
    assert _one("SELECT department, status FROM admissions WHERE id = ?", (aid,)) == \
        {"department": recv_dept, "status": "active"}
    assert _movements(aid)[-1]["movement_type"] == "transfer"
    assert len(_audits("patient_transferred", pid, "clinical_access")) == 1


def test_a_roster_move_the_file_cannot_make_is_reported_and_left(adt_on):
    admin = _admin()
    host = f"Host Dept {_tag()}"
    _user("nurse", host)
    host_ward = _ward(department=host, labels=("1",))
    mrn = f"TRR{_tag()}"
    roster_import.apply_import(_roster(mrn, "TR Typed B", "1", DEPT), _MAPPING,
                               actor_user_id=admin["id"])
    pid, aid = _roster_patient(mrn)
    res = roster_import.apply_import(_roster(mrn, host_ward["code"], "1", DEPT), _MAPPING,
                                     actor_user_id=admin["id"])
    assert res["admissions"]["update_refused"] == 1
    row = next(r for r in res["rows"] if r.get("mrn") == mrn)
    assert row["admission"]["refusal"]["code"] == "boarder_acknowledgement_required"
    adm = _one("SELECT ward, bed, bed_id FROM admissions WHERE id = ?", (aid,))
    assert adm == {"ward": "TR Typed B", "bed": "1", "bed_id": None}
    # The patients row is the writer's alone: the refused move left it too.
    assert _one("SELECT ward FROM patients WHERE id = ?", (pid,))["ward"] == "TR Typed B"


def test_flag_off_a_roster_update_writes_no_movement(adt_off):
    admin = _admin()
    mrn = f"TRR{_tag()}"
    roster_import.apply_import(_roster(mrn, "TR Off A", "1", DEPT), _MAPPING,
                               actor_user_id=admin["id"])
    pid, aid = _roster_patient(mrn)
    res = roster_import.apply_import(_roster(mrn, "TR Off B", "2", DEPT), _MAPPING,
                                     actor_user_id=admin["id"])
    assert res["admissions"]["updated"] == 1 and "update_refused" not in res["admissions"]
    assert _movements(aid) == []
    assert _one("SELECT ward, bed FROM admissions WHERE id = ?", (aid,)) == \
        {"ward": "TR Off B", "bed": "2"}


# ═════════════════════════════════════════════════════════════════════════════
#  9. Where the admission lies, for the Move screen
# ═════════════════════════════════════════════════════════════════════════════

def test_the_location_read_answers_the_bed_and_is_audited(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    resp = client.get(f"/api/adt/admissions/{p['aid']}/location", headers=_hdr(DOCTOR))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["bed_id"], out["ward_id"], out["department"]) == (p["bed_id"], home["id"], DEPT)
    assert out["linked"] is True and out["boarder"] is False
    name = _one("SELECT name, mrn FROM patients WHERE id = ?", (p["pid"],))
    assert name["name"] not in resp.text and name["mrn"] not in resp.text
    assert len(_audits("admission_location_viewed", p["pid"], "phi_access")) == 1


def test_the_location_read_is_scoped_and_permissioned(adt_on):
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    assert client.get(f"/api/adt/admissions/{p['aid']}/location",
                      headers=_hdr(recv["doctor"])).status_code == 404
    assert client.get(f"/api/adt/admissions/{p['aid']}/location",
                      headers=_hdr({"id": "N001", "role": "nurse"})).status_code == 403
    clerk = _user("ward_clerk", DEPT)
    assert client.get(f"/api/adt/admissions/{p['aid']}/location",
                      headers=_hdr(clerk)).status_code == 200


def _location(admission_id: str) -> dict:
    resp = client.get(f"/api/adt/admissions/{admission_id}/location", headers=_hdr(DOCTOR))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_keeping_an_out_of_service_bed_needs_its_reason_and_the_location_says_so(adt_on):
    # Housekeeping may mark an occupied bed out of service (a legal
    # transition). Keeping that bed on a transfer then needs the reason, as
    # placing a patient there does; the location read says so before the
    # Move screen sends anything.
    home = _ward(labels=("1",))
    p = _admit(home, "1")
    recv = _receiving()
    assert _location(p["aid"])["kept_place"] == {
        "ward": home["code"], "ward_department": DEPT, "bed": "1", "out_of_service": False}
    hk.transition_unit(p["bed_id"], "out_of_service", actor_user_id="TRHK", reason="Broken rail")
    assert _location(p["aid"])["kept_place"] == {
        "ward": home["code"], "ward_department": DEPT, "bed": "1", "out_of_service": True}

    body = {"admission_id": p["aid"], "to_department": recv["dept"], "keep_location": True,
            "boarder_acknowledged": True, "expected_bed_id": p["bed_id"],
            "reason": "Accepted, no bed on the unit"}
    refused = _transfer(body)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "out_of_service_reason_required"
    assert _one("SELECT department FROM admissions WHERE id = ?", (p["aid"],))["department"] == DEPT
    resp = _transfer({**body, "out_of_service_reason": "Rail taped, patient stable"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_department"] == recv["dept"]
    move = _movements(p["aid"])[-1]
    assert '"out_of_service_reason": "Rail taped, patient stable"' in move["detail_json"]


def test_keeping_an_unlinked_place_on_a_ward_of_another_department_is_a_boarder(adt_on):
    # A stay recorded before bed setup (typed ward, no ward_id): its linked
    # ward_department is empty, but keeping the place resolves the typed ward
    # to the ward of bed setup it names. The location read reports that
    # ward's department, so the Move screen can ask for the boarder
    # confirmation the writer will ask for.
    other = f"Other Dept {_tag()}"
    _user("doctor", other)
    typed = _ward(department=other, labels=("1",))
    recv = _receiving()
    with db_session() as db:
        pid = f"PAT-TRK{_tag()}"
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, 'Kept Place Patient', ?, 'admitted', ?, '9')",
                   (pid, f"MRN-{pid}", typed["code"].lower()))
        aid = f"ADM-TRK{_tag()}"
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, ?, '9', 'active', 'D001')",
                   (aid, pid, DEPT, typed["code"].lower()))
    _SEEDED.append(pid)

    here = _location(aid)
    assert here["ward_id"] is None and here["ward_department"] is None
    assert here["kept_place"] == {"ward": typed["code"], "ward_department": other, "bed": None,
                                  "out_of_service": False}

    body = {"admission_id": aid, "to_department": recv["dept"], "keep_location": True,
            "expected_bed_id": None, "reason": "Accepted by the receiving team"}
    refused = _transfer(body)
    assert refused.status_code == 409
    detail = _detail(refused)
    assert detail["error"] == "boarder_acknowledgement_required"
    assert detail["ward_department"] == other
    resp = _transfer({**body, "boarder_acknowledged": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["boarder"] is True
    assert len(_audits("boarder_placed", pid, "clinical_access")) == 1


def test_a_place_that_matches_no_ward_of_bed_setup_keeps_nothing_to_ask(adt_on):
    with db_session() as db:
        pid = f"PAT-TRN{_tag()}"
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, 'Unmatched Place Patient', ?, 'admitted', 'Nowhere Bay', '2')",
                   (pid, f"MRN-{pid}"))
        aid = f"ADM-TRN{_tag()}"
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, 'Nowhere Bay', '2', 'active', 'D001')",
                   (aid, pid, DEPT))
    _SEEDED.append(pid)
    assert _location(aid)["kept_place"] == {"ward": None, "ward_department": None, "bed": None,
                                            "out_of_service": False}
