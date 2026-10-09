"""The ward clerk: scope and refusals (adt-17 remainder, HIS wave 8, W8-D).

The role, its grants and the creation gate landed in wave 1 (migration 48,
roles.py, permissions.py, admin_users_routes). This file is the increment's
own record that they hold together, in one place:

  * role parity: the users CHECK lists exactly roles.STORABLE_ROLES, and the
    clerk is a department-bound role behind Beds and admissions;
  * grants: adt.read and adt.bed (and the Ward Record's vitals.transcribe),
    never patient.read_all, patient.write, adt.transfer (ADT-S8, held),
    adt.presence (ADT-S7, held), discharge.generate, bed.master or relay.*;
  * creation: refused with the module off, refused with no department;
  * scope: the bed board and the awaiting list name only the clerk's own
    department's patients, and every read that names anybody is audited
    (phi_access, with the patient ids); another department's ward is 404;
    the census shows the clerk's department's wards and no hospital totals;
  * refusals: no chart, no transfer to another department, no leave or
    absconding, no admission, no end of stay, no bed setup, no Relay.

CODE VS SPEC. The design said a clerk opening /api/vigil/patients/{id} gets
404. The code answers 403: require_patient_access checks the permission
(patient.read_all, which the clerk does not hold) before the department
scope, and permissions.py belongs to another cluster this wave. 403 there is
the same answer for every patient id, real or not, so it discloses nothing;
it is pinned below as it is.
"""
import json
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import config
import main
import migrations
import permissions as P
from auth import create_token, hash_password
from database import db_session
from roles import ROLE_FEATURE, STORABLE_ROLES
from services import bed_master_service as bm
from services import relay_service
from services.adt import core as adt_core

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds doctors and a nurse in it
_SEEDED: list[str] = []


def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"WC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Clerk test {role}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


def _hdr(user: dict, key: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        out["Idempotency-Key"] = key
    return out


_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        _ADMIN = _user("admin", None)
    return _ADMIN


def _ward(department: str = DEPT, labels=("1", "2", "3")) -> dict:
    ward = bm.save_ward({"code": f"WC-{_tag()}", "department": department}, actor=_admin())
    if labels:
        bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
                               (ward_id, label)).fetchone())


def _admit(*, department: str = DEPT, name: str | None = None) -> dict:
    pid = f"PWC{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADM-WC{uuid.uuid4().hex[:8].upper()}"
    name = name or f"Clerk Patient {pid[-6:]}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, status, admission_date) "
            "VALUES (?, ?, ?, NULL, NULL, 'admitted', CURRENT_TIMESTAMP)",
            (pid, name, f"MRN-{pid}"),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
            "VALUES (?, ?, 'D001', ?, 'active')",
            (aid, pid, department),
        )
    _SEEDED.append(pid)
    return {"pid": pid, "aid": aid, "mrn": f"MRN-{pid}", "name": name}


def _place(p: dict, ward: dict, label: str, **kw) -> None:
    with db_session(immediate=True) as db:
        adt_core.locate_admission_on(db, admission_id=p["aid"], actor=_admin(),
                                     movement_type="bed_assigned", reason="test",
                                     ward_id=ward["id"], bed_id=_bed(ward["id"], label)["id"], **kw)


def _audits(action: str, actor_id: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND actor_user_id = ? ORDER BY id",
            (action, actor_id)).fetchall()]


@pytest.fixture(scope="module", autouse=True)
def _discharge_what_this_file_admitted():
    """Leave no admitted fixture patient behind: other files count the census."""
    yield
    with db_session() as db:
        for pid in _SEEDED:
            db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ? "
                       "AND status = 'active'", (pid,))
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    yield


@pytest.fixture()
def clerk() -> dict:
    return _user("ward_clerk")


@pytest.fixture()
def other_dept() -> str:
    dept = f"Cardiology {_tag()}"
    _user("nurse", dept)        # clinical cover, so a ward can be set up there
    return dept


# ═════════════════════════════════════════════════════════════════════════════
#  The role, its grants, its creation
# ═════════════════════════════════════════════════════════════════════════════

def test_the_users_check_lists_exactly_the_storable_roles():
    assert tuple(migrations.LATEST_USERS_ROLE_CHECK) == tuple(STORABLE_ROLES)
    assert "ward_clerk" in STORABLE_ROLES
    assert "ward_clerk" in P.DEPARTMENT_REQUIRED_ROLES
    assert ROLE_FEATURE["ward_clerk"] == "adt"


def test_the_clerk_holds_bed_moves_and_reads_and_nothing_clinical():
    grants = P.ROLE_PERMISSIONS["ward_clerk"]
    assert {P.ADT_READ, P.ADT_BED} <= grants
    for withheld in (P.ADT_TRANSFER, P.ADT_PRESENCE, P.PATIENT_READ_ALL, "patient.write",
                     "discharge.generate", "bed.master", P.RELAY_READ):
        assert withheld not in grants, withheld
    assert not any(g.startswith("relay.") for g in grants)


def _create(role: str, department: str | None):
    uid = f"WCN{uuid.uuid4().hex[:6].upper()}"
    resp = client.post("/api/admin/users", headers=_hdr(_admin()), json={
        "id": uid, "name": "New Ward Clerk", "role": role, "department": department,
        "password": "password1"})
    return uid, resp


def test_no_clerk_can_be_created_with_the_module_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    uid, resp = _create("ward_clerk", DEPT)
    assert resp.status_code == 400, resp.text
    assert "switched off" in resp.json()["detail"]
    with db_session() as db:
        assert db.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None


def test_a_clerk_without_a_department_is_refused(adt_on):
    uid, resp = _create("ward_clerk", None)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Department is required for a ward clerk"
    with db_session() as db:
        assert db.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None


def test_a_clerk_with_a_department_is_created_with_the_module_on(adt_on):
    uid, resp = _create("ward_clerk", DEPT)
    assert resp.status_code == 201, resp.text
    with db_session() as db:
        row = db.execute("SELECT role, department FROM users WHERE id = ?", (uid,)).fetchone()
    assert (row["role"], row["department"]) == ("ward_clerk", DEPT)


# ═════════════════════════════════════════════════════════════════════════════
#  Scope: only the clerk's department, and every naming read audited
# ═════════════════════════════════════════════════════════════════════════════

def test_the_board_names_only_the_clerks_department_and_audits_the_read(adt_on, clerk, other_dept):
    ward = _ward()
    own = _admit(name="Own Department Patient")
    _place(own, ward, "1")
    boarder = _admit(department=other_dept, name="Other Department Boarder")
    _place(boarder, ward, "2", boarder_acknowledged=True)
    before = len(_audits("bed_board_viewed", clerk["id"]))

    resp = client.get("/api/adt/board", params={"ward_id": ward["id"]}, headers=_hdr(clerk))
    assert resp.status_code == 200, resp.text
    tiles = {t["bed_label"]: t for w in resp.json()["wards"] if w["id"] == ward["id"]
             for t in w["tiles"]}
    assert tiles["1"]["occupant"]["name"] == "Own Department Patient"
    assert tiles["2"]["occupant"] is None and tiles["2"]["status"] == "boarder"
    for secret in (boarder["pid"], boarder["mrn"], "Other Department Boarder"):
        assert secret not in resp.text

    audits = _audits("bed_board_viewed", clerk["id"])
    assert len(audits) == before + 1
    assert audits[-1]["event_category"] == "phi_access"
    named = json.loads(audits[-1]["metadata_json"])
    assert own["pid"] in json.dumps(named) and boarder["pid"] not in json.dumps(named)


def test_another_departments_ward_is_404_to_the_clerk(adt_on, clerk, other_dept):
    theirs = _ward(department=other_dept)
    resp = client.get("/api/adt/board", params={"ward_id": theirs["id"]}, headers=_hdr(clerk))
    assert resp.status_code == 404, resp.text
    census = client.get("/api/adt/census", params={"ward_id": theirs["id"]}, headers=_hdr(clerk))
    assert census.status_code == 404, census.text


def test_the_awaiting_list_holds_only_the_clerks_patients_and_is_audited(adt_on, clerk, other_dept):
    waiting = _admit(name="Waiting Own Patient")
    outsider = _admit(department=other_dept, name="Waiting Outsider")
    resp = client.get("/api/adt/awaiting-bed", headers=_hdr(clerk))
    assert resp.status_code == 200, resp.text
    assert waiting["pid"] in resp.text
    assert outsider["pid"] not in resp.text and "Waiting Outsider" not in resp.text
    audit = _audits("awaiting_bed_viewed", clerk["id"])[-1]
    assert audit["event_category"] == "phi_access"
    assert waiting["pid"] in audit["metadata_json"] and outsider["pid"] not in audit["metadata_json"]


def test_the_census_shows_the_clerks_wards_and_no_hospital_totals(adt_on, clerk, other_dept):
    mine = _ward()
    theirs = _ward(department=other_dept)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    resp = client.get("/api/adt/census", params={"from": yesterday, "to": yesterday},
                      headers=_hdr(clerk))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {w["id"] for w in body["wards"]}
    assert mine["id"] in ids and theirs["id"] not in ids
    assert body["hospital"] is None
    assert body["department"] == DEPT


def test_the_clerks_routes_are_404_with_the_module_off(monkeypatch, clerk):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    for path in ("/api/adt/board", "/api/adt/awaiting-bed", "/api/adt/census"):
        resp = client.get(path, headers=_hdr(clerk))
        assert resp.status_code == 404, (path, resp.text)


# ═════════════════════════════════════════════════════════════════════════════
#  Refusals
# ═════════════════════════════════════════════════════════════════════════════

def test_the_clerk_cannot_open_a_chart(adt_on, clerk, other_dept):
    own = _admit(name="Chart Own Patient")
    outsider = _admit(department=other_dept, name="Chart Outsider")
    for p in (own, outsider):
        resp = client.get(f"/api/vigil/patients/{p['pid']}", headers=_hdr(clerk))
        assert resp.status_code == 403, resp.text
        assert p["name"] not in resp.text and p["mrn"] not in resp.text
    # The same answer for an id that names nobody: nothing is disclosed.
    assert client.get("/api/vigil/patients/NO-SUCH-PATIENT",
                      headers=_hdr(clerk)).status_code == 403


def test_the_clerk_cannot_move_a_patient_to_another_department(adt_on, clerk, other_dept):
    ward = _ward()
    p = _admit()
    _place(p, ward, "1")
    resp = client.post("/api/adt/transfers", headers=_hdr(clerk, f"tr-{_tag()}"), json={
        "admission_id": p["aid"], "to_department": other_dept, "keep_location": True,
        "expected_bed_id": _bed(ward["id"], "1")["id"], "reason": "Cardiology review"})
    assert resp.status_code == 403, resp.text
    with db_session() as db:
        row = db.execute("SELECT department FROM admissions WHERE id = ?", (p["aid"],)).fetchone()
    assert row["department"] == DEPT


def test_the_clerk_cannot_record_leave_or_absconding(adt_on, clerk):
    ward = _ward()
    p = _admit()
    _place(p, ward, "1")
    for path in ("leave", "absconded"):
        resp = client.post(f"/api/adt/admissions/{p['aid']}/{path}",
                           headers=_hdr(clerk, f"pr-{_tag()}"), json={"reason": "Day leave"})
        assert resp.status_code == 403, (path, resp.text)
    with db_session() as db:
        row = db.execute("SELECT presence_status FROM admissions WHERE id = ?",
                         (p["aid"],)).fetchone()
    assert row["presence_status"] is None


def test_the_clerk_cannot_admit_or_end_a_stay(adt_on, clerk):
    admit = client.post("/api/admissions/admit", headers=_hdr(clerk, f"adm-{_tag()}"), json={
        "name": f"Clerk Admit {_tag()}", "age": 40, "sex": "F", "admission_source": "er",
        "admission_type": "inpatient", "department": DEPT, "ward": "", "bed": "",
        "attending_doctor_id": "D001", "chief_complaint": "Fever"})
    assert admit.status_code == 403, admit.text
    p = _admit()
    end = client.post(f"/api/adt/admissions/{p['aid']}/end", headers=_hdr(clerk, f"end-{_tag()}"),
                      json={"end_type": "routine", "confirm_mrn": p["mrn"]})
    assert end.status_code == 403, end.text
    with db_session() as db:
        assert db.execute("SELECT status FROM admissions WHERE id = ?",
                          (p["aid"],)).fetchone()["status"] == "active"


def test_the_clerk_cannot_touch_bed_setup(adt_on, clerk):
    ward = _ward()
    created = client.post("/api/adt/master/wards", headers=_hdr(clerk),
                          json={"code": f"WC-{_tag()}", "department": DEPT})
    assert created.status_code == 403, created.text
    complete = client.post(f"/api/adt/master/wards/{ward['id']}/register-complete",
                           headers=_hdr(clerk), json={})
    assert complete.status_code == 403, complete.text
    recon = client.get("/api/adt/master/reconciliation", headers=_hdr(clerk))
    assert recon.status_code == 403, recon.text
    # Reading the hospital's structure (no patient in it) is allowed.
    assert client.get("/api/adt/master", headers=_hdr(clerk)).status_code == 200


def test_the_clerk_has_no_relay(clerk):
    assert relay_service.get_channels_for_user(clerk["id"], "ward_clerk") == []
