"""Departments and admissions carry the facility (multisite-3, W10-B, 20 October 2026).

Ruling MS-R4 (a), built strictest: a department name is unique across the
hospital group and belongs to one facility (the departments master, migration
70). With more than one facility on record this file pins:

  * the departments master: a name another facility lists is refused in any
    capitalisation and with any padding; a department anything names cannot
    change facility; growth needs ORB_MULTI_SITE; each write is a security
    audit row (services/department_master.py);
  * the covering-department list and its resolver keep only listed
    departments, and narrow to a facility or a facility scope;
  * an admission into a facility's department stamps that facility on the
    admission and on the patient it creates; an admitter outside that
    facility, and a patient whose admissions are at another facility, are
    refused with a cross_facility_move_refused row and nothing written;
  * a transfer into another facility's department answers 409 with that row
    and moves nothing;
  * User Management stores a department in the master's own spelling;
  * site.toml [[facilities]] is validated with the file and the line.

With one facility on record everything is as before: the list, the stamps
(none), the transfer, the stored spelling.

TEST RULE (contracts, W7-J): the second facility is removed in teardown and
facility_service.invalidate_cache() called; every facility id written here is
put back to 1. Run with the test env, never the shipping .env.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import facility_fence as F
from auth import create_token
from database import db_session
from main import app
from services import admission_service as AS
from services import department_master as DM
from services import facility_service as FS
from services import frontdesk_service as fd
from services.site_config_service import SiteConfigError, load_site_config

client = TestClient(app)

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9311                      # an id no other module writes
F2_CODE = f"FD{TAG}"
DEPT_F1 = f"Quandmere Medicine {TAG}"
DEPT_F1_B = f"Quandmere Surgery {TAG}"
DEPT_F2 = f"Vasquell Cardiology {TAG}"
DEPT_UNLISTED = f"Brennholt Unlisted {TAG}"


def _mk_user(db, role, department, label):
    uid = f"FDP{uuid.uuid4().hex[:8].upper()}"
    db.execute(
        "INSERT INTO users (id, name, role, department, password_hash, is_active) "
        "VALUES (?, ?, ?, ?, 'x', 1)", (uid, f"Ardolai {label}", role, department))
    return uid


def _put_back_facility_two():
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Vasquell Satellite"))
    FS.invalidate_cache()


@pytest.fixture(scope="module")
def world():
    users: dict = {}
    with db_session() as db:
        for name, fid in ((DEPT_F1, 1), (DEPT_F1_B, 1), (DEPT_F2, F2)):
            db.execute("INSERT INTO departments (name, facility_id, seeded_from) "
                       "VALUES (?, ?, 'test')", (name, fid))
        users["doc_f1"] = _mk_user(db, "doctor", DEPT_F1, "F1 doctor")
        users["doc_f1_b"] = _mk_user(db, "doctor", DEPT_F1_B, "F1 surgeon")
        users["doc_f2"] = _mk_user(db, "doctor", DEPT_F2, "F2 doctor")
        users["nurse_f2"] = _mk_user(db, "nurse", DEPT_F2, "F2 nurse")
        users["doc_unlisted"] = _mk_user(db, "doctor", DEPT_UNLISTED, "unlisted doctor")
        users["admin_f1"] = _mk_user(db, "admin", None, "F1 admin")
        users["admin_f2"] = _mk_user(db, "admin", None, "F2 admin")
        db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                   "granted_at, reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, "
                   "'test')", (users["admin_f2"], F2))
    _put_back_facility_two()
    state = {"users": users, "patients": []}
    try:
        yield state
    finally:
        uids, pids = list(users.values()), list(state["patients"])
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
            db.execute("DELETE FROM departments WHERE name LIKE ?", (f"%{TAG}%",))
            db.executemany("DELETE FROM user_facilities WHERE user_id = ?", [(u,) for u in uids])
            db.executemany("UPDATE admissions SET facility_id = 1, status = 'discharged' "
                           "WHERE patient_id = ?", [(p,) for p in pids])
            db.executemany("UPDATE patients SET registered_facility_id = 1, "
                           "status = 'discharged' WHERE id = ?", [(p,) for p in pids])
            db.executemany("UPDATE users SET is_active = 0 WHERE id = ?", [(u,) for u in uids])
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh_cache():
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


@pytest.fixture()
def one_facility(world):
    """Facility two taken off record for one test, then put back."""
    with db_session() as db:
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        yield
    finally:
        _put_back_facility_two()


@pytest.fixture()
def fence_open(monkeypatch):
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


@pytest.fixture()
def multi_site_on(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)


def _user(world, key) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM users WHERE id = ?",
                               (world["users"][key],)).fetchone())


def _auth(world, key) -> dict:
    user = _user(world, key)
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


def _last_audit_id() -> int:
    with db_session() as db:
        return int(db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0])


def _rows(after: int, action: str) -> list[dict]:
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE id > ? AND action_type = ? ORDER BY id",
            (after, action)).fetchall()]
    for row in rows:
        row["meta"] = json.loads(row["metadata_json"] or "{}")
    return rows


def _admit(world, user_key, department, name):
    user = _user(world, user_key)
    result = AS.admit_patient({
        "name": name, "department": department, "ward": f"Zelk {TAG}",
        "bed": f"B-{uuid.uuid4().hex[:4]}", "attending_doctor_id": user["id"],
        "chief_complaint": "Fever", "admission_source": "OPD", "admission_type": "elective",
    }, user)
    world["patients"].append(result["patient_id"])
    return result


def _facilities_of(patient_id):
    with db_session() as db:
        adm = db.execute("SELECT facility_id FROM admissions WHERE patient_id = ?",
                         (patient_id,)).fetchone()
        pat = db.execute("SELECT registered_facility_id FROM patients WHERE id = ?",
                         (patient_id,)).fetchone()
    return int(adm[0]), int(pat[0])


def _patients_named(name) -> int:
    with db_session() as db:
        return int(db.execute("SELECT COUNT(*) FROM patients WHERE name = ?",
                              (name,)).fetchone()[0])


# ═════════════════════════════════════════════════════════════════════════════
#  1. The departments master (MS-R4 (a))
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("typed", [
    DEPT_F2.upper(), DEPT_F2.lower(), f"   {DEPT_F2}   ", DEPT_F2.replace(" ", "   "),
])
def test_a_name_another_facility_lists_is_refused_in_any_case_and_padding(world, typed):
    with pytest.raises(DM.DepartmentError) as err:
        DM.add_department(typed, 1, actor=_user(world, "admin_f1"))
    assert err.value.code == "department_exists"
    assert err.value.status == 409
    assert err.value.extra["facility_code"] == F2_CODE
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM departments WHERE LOWER(name) LIKE ?",
                          (f"%vasquell cardiology {TAG.lower()}%",)).fetchone()[0] == 1


def test_the_table_itself_refuses_a_second_spelling(world):
    from db_backend import DBIntegrityError
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("INSERT INTO departments (name, facility_id) VALUES (?, 1)",
                       (DEPT_F2.lower(),))
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("INSERT INTO departments (name, facility_id) VALUES (?, 1)",
                       (f" {DEPT_F2} ",))


def test_a_new_department_is_stored_tidied_and_audited(world):
    after = _last_audit_id()
    view = DM.add_department(f"  Orsolya   Renal {TAG}  ", 1, actor=_user(world, "admin_f1"))
    assert view["name"] == f"Orsolya Renal {TAG}"
    assert view["facility_id"] == 1 and view["facility_code"] == "MAIN"
    rows = _rows(after, "department_created")
    assert len(rows) == 1
    assert rows[0]["event_category"] == "security"
    assert rows[0]["meta"]["facility_id"] == 1
    assert FS.department_facility(f"orsolya renal {TAG}") == 1


def test_a_bed_label_and_a_blank_are_not_departments(world):
    for typed, code in (("GM-12", "looks_like_a_bed"), ("   ", "name_required")):
        with pytest.raises(DM.DepartmentError) as err:
            DM.add_department(typed, 1, actor=_user(world, "admin_f1"))
        assert err.value.code == code and err.value.status == 400


def test_another_facility_needs_the_switch_and_the_admins_scope(world, monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", False)
    with pytest.raises(DM.DepartmentError) as err:
        DM.add_department(f"Tibor Oncology {TAG}", F2, actor=_user(world, "admin_f2"))
    assert err.value.code == "multi_site_off"
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    with pytest.raises(DM.DepartmentError) as err:
        DM.add_department(f"Tibor Oncology {TAG}", F2, actor=_user(world, "admin_f1"))
    assert err.value.code == "facility_scope" and err.value.status == 403
    view = DM.add_department(f"Tibor Oncology {TAG}", F2, actor=_user(world, "admin_f2"))
    assert view["facility_id"] == F2


def test_only_an_administrator_changes_the_list(world):
    with pytest.raises(DM.DepartmentError) as err:
        DM.add_department(f"Nurse Typed {TAG}", 1, actor=_user(world, "nurse_f2"))
    assert err.value.code == "permission_denied"


def test_a_referenced_department_cannot_change_facility(world, multi_site_on):
    with db_session() as db:
        dept_id = db.execute("SELECT id FROM departments WHERE name = ?",
                             (DEPT_F1_B,)).fetchone()[0]
    # The admin with no facility limit would be the only one who could even ask.
    with db_session() as db:
        db.execute("INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
                   "VALUES (?, 'system', CURRENT_TIMESTAMP, 'test')",
                   (world["users"]["admin_f1"],))
    FS.invalidate_cache()
    try:
        with pytest.raises(DM.DepartmentError) as err:
            DM.move_department(dept_id, F2, actor=_user(world, "admin_f1"),
                               reason="Satellite opened")
        assert err.value.code == "department_in_use"
        assert "users" in err.value.extra["where"]
        with db_session() as db:
            assert db.execute("SELECT facility_id FROM departments WHERE id = ?",
                              (dept_id,)).fetchone()[0] == 1

        # An unused one moves, once, with a reason and a security row.
        fresh = DM.add_department(f"Ilka Unused {TAG}", 1, actor=_user(world, "admin_f1"))
        with pytest.raises(DM.DepartmentError) as err:
            DM.move_department(fresh["id"], F2, actor=_user(world, "admin_f1"), reason=" ")
        assert err.value.code == "reason_required"
        after = _last_audit_id()
        moved = DM.move_department(fresh["id"], F2, actor=_user(world, "admin_f1"),
                                   reason="Belongs to the satellite")
        assert moved["facility_id"] == F2
        rows = _rows(after, "department_facility_changed")
        assert len(rows) == 1 and rows[0]["event_category"] == "security"
        assert rows[0]["meta"]["from_facility_id"] == 1 and rows[0]["meta"]["to_facility_id"] == F2
        assert FS.department_facility(f"Ilka Unused {TAG}") == F2
    finally:
        with db_session() as db:
            db.execute("DELETE FROM enterprise_access_grants WHERE user_id = ?",
                       (world["users"]["admin_f1"],))
        FS.invalidate_cache()


def test_an_admitted_department_is_referenced_even_with_no_staff(world):
    with db_session() as db:
        db.execute("INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
                   (f"Fennick Annex {TAG}",))
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'discharged')",
                   (f"FDPR{TAG}", "Fennick Ardolai", f"MRN-FDPR{TAG}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'system', ?, 'discharged')",
                   (f"FDPRA{TAG}", f"FDPR{TAG}", f" fennick annex {TAG.lower()} "))
        used = DM.department_references(db, f"Fennick Annex {TAG}")
    world["patients"].append(f"FDPR{TAG}")
    assert {"where": "admissions", "column": "department"} in used


# ═════════════════════════════════════════════════════════════════════════════
#  2. The covering list and its resolver
# ═════════════════════════════════════════════════════════════════════════════

def test_the_covering_list_keeps_listed_departments_by_facility(world):
    every = fd.list_covering_departments()
    assert DEPT_F1 in every and DEPT_F2 in every
    assert DEPT_UNLISTED not in every            # no facility owns it
    assert fd.list_covering_departments(F2) == [d for d in every if d == DEPT_F2]
    assert DEPT_F2 not in fd.list_covering_departments(1)
    assert DEPT_F1 in fd.list_covering_departments(facilities=frozenset({1}))
    assert DEPT_F2 not in fd.list_covering_departments(facilities=frozenset({1}))
    assert fd.list_covering_departments(facilities=frozenset()) == []
    assert fd._resolve_covering_department(DEPT_F2.lower(), facility_id=1) is None
    assert fd._resolve_covering_department(DEPT_F2.lower(), facility_id=F2) == DEPT_F2
    with pytest.raises(AS.AdmissionInputError):
        AS.resolve_admission_department(DEPT_UNLISTED)
    assert AS.resolve_admission_department(f" {DEPT_F2.upper()} ") == DEPT_F2


def test_an_inactive_master_department_is_not_a_destination(world):
    with db_session() as db:
        db.execute("UPDATE departments SET is_active = 0 WHERE name = ?", (DEPT_F1_B,))
    try:
        assert DEPT_F1_B not in fd.list_covering_departments()
    finally:
        with db_session() as db:
            db.execute("UPDATE departments SET is_active = 1 WHERE name = ?", (DEPT_F1_B,))


def test_with_one_facility_the_covering_list_is_unchanged(world, one_facility):
    every = fd.list_covering_departments()
    assert DEPT_UNLISTED in every and DEPT_F2 in every
    assert fd.list_covering_departments(1) == every
    assert fd.list_covering_departments(facilities=frozenset({1})) == every
    assert fd.list_covering_departments(F2) == []
    assert AS.resolve_admission_department(DEPT_UNLISTED) == DEPT_UNLISTED


def test_the_admission_form_offers_only_the_callers_facility(world):
    f2 = AS.admission_choices(DEPT_F2, user=_user(world, "doc_f2"))["departments"]
    assert DEPT_F2 in f2 and DEPT_F1 not in f2
    f1 = AS.admission_choices(DEPT_F1, user=_user(world, "doc_f1"))["departments"]
    assert DEPT_F1 in f1 and DEPT_F2 not in f1


# ═════════════════════════════════════════════════════════════════════════════
#  3. Admissions stamp the facility
# ═════════════════════════════════════════════════════════════════════════════

def test_an_admission_at_facility_two_stamps_the_admission_and_the_patient(world):
    result = _admit(world, "doc_f2", DEPT_F2, "Orsolya Quandt")
    assert _facilities_of(result["patient_id"]) == (F2, F2)
    # And the facility's own staff can see the chart; the other facility's cannot.
    import permissions as P
    with db_session() as db:
        assert P.patient_in_scope(db, result["patient_id"], DEPT_F2,
                                  facilities=FS.facility_scope_for(_user(world, "nurse_f2")))
        assert not P.patient_in_scope(db, result["patient_id"], None,
                                      facilities=FS.facility_scope_for(_user(world, "admin_f1")))


def test_an_admission_at_facility_one_stays_facility_one(world):
    result = _admit(world, "doc_f1", DEPT_F1, "Tibor Vasquell")
    assert _facilities_of(result["patient_id"]) == (1, 1)


def test_an_admitter_outside_the_facility_is_refused_and_audited(world):
    after = _last_audit_id()
    with pytest.raises(AS.AdmissionRefused) as err:
        _admit(world, "doc_f1", DEPT_F2, "Ilka Brennholt")
    assert err.value.status == 403 and err.value.code == "facility_scope"
    assert _patients_named("Ilka Brennholt") == 0
    rows = _rows(after, "cross_facility_move_refused")
    assert len(rows) == 1
    assert rows[0]["event_category"] == "authorization" and rows[0]["outcome"] == "denied"
    assert rows[0]["meta"]["cause"] == "actor_outside_facility"
    assert rows[0]["meta"]["to_facility_id"] == F2


def test_the_admission_form_answers_the_refusal_in_words(world, fence_open):
    resp = client.post("/api/admissions/admit", headers=_auth(world, "doc_f1"), json={
        "name": "Wendeline Kosk", "age": 61, "sex": "F", "admission_source": "OPD",
        "admission_type": "elective", "department": DEPT_F2, "ward": f"Zelk {TAG}",
        "bed": "K-1", "attending_doctor_id": world["users"]["doc_f1"],
        "chief_complaint": "Breathless"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "facility_scope"
    assert _patients_named("Wendeline Kosk") == 0


def test_a_patient_admitted_at_another_facility_is_refused_and_audited(world):
    result = _admit(world, "doc_f1", DEPT_F1, "Quillon Marsk")
    pid = result["patient_id"]
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
    after = _last_audit_id()
    with pytest.raises(AS.AdmissionRefused) as err:
        AS.admission_facility(DEPT_F2, _user(world, "doc_f2"), lane="readmission",
                              patient_id=pid)
    assert err.value.status == 409 and err.value.code == "cross_facility_move_refused"
    rows = _rows(after, "cross_facility_move_refused")
    assert len(rows) == 1 and rows[0]["patient_id"] == pid
    assert rows[0]["meta"]["from_facility_ids"] == [1]
    assert rows[0]["meta"]["lane"] == "readmission"
    # At its own facility the same patient is admitted there.
    assert AS.admission_facility(DEPT_F1, _user(world, "doc_f1"), lane="readmission",
                                 patient_id=pid) == 1


def test_with_one_facility_an_admission_stamps_nothing(world, one_facility):
    after = _last_audit_id()
    assert AS.admission_facility(DEPT_F2, _user(world, "doc_f1"), lane="admission_form") is None
    result = _admit(world, "doc_f1", DEPT_UNLISTED, "Marisol Teqway")
    assert _facilities_of(result["patient_id"]) == (1, 1)
    assert _rows(after, "cross_facility_move_refused") == []


# ═════════════════════════════════════════════════════════════════════════════
#  4. Transfers never cross a facility
# ═════════════════════════════════════════════════════════════════════════════

def test_a_cross_facility_transfer_is_409_audited_and_moves_nothing(world, fence_open):
    result = _admit(world, "doc_f1", DEPT_F1, "Henrike Zolvane")
    pid = result["patient_id"]
    after = _last_audit_id()
    resp = client.post("/api/frontdesk/transfers", headers=_auth(world, "doc_f1"), json={
        "patient_id": pid, "to_department": DEPT_F2, "reason": "Needs cardiology"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "cross_facility_move_refused"
    rows = _rows(after, "cross_facility_move_refused")
    assert len(rows) == 1
    assert rows[0]["patient_id"] == pid and rows[0]["target_id"] == result["admission_id"]
    assert rows[0]["meta"]["lane"] == "frontdesk_transfer"
    assert rows[0]["meta"]["cause"] == "patient_at_another_facility"
    assert rows[0]["meta"]["from_facility_ids"] == [1]
    assert rows[0]["meta"]["to_facility_id"] == F2
    with db_session() as db:
        adm = db.execute("SELECT department, facility_id FROM admissions WHERE id = ?",
                         (result["admission_id"],)).fetchone()
        moves = db.execute("SELECT COUNT(*) FROM patient_movements WHERE patient_id = ?",
                           (pid,)).fetchone()[0]
    assert (adm["department"], adm["facility_id"]) == (DEPT_F1, 1)
    assert moves == 0
    assert _rows(after, "patient_transferred") == []


def test_a_transfer_inside_one_facility_still_moves(world):
    result = _admit(world, "doc_f1", DEPT_F1, "Petronel Vashti")
    moved = fd.transfer_patient(patient_id=result["patient_id"], to_department=DEPT_F1_B,
                                reason="Surgical review", moved_by=world["users"]["doc_f1"])
    assert moved["current_department"] == DEPT_F1_B
    with pytest.raises(fd.CrossFacilityMoveRefused):
        fd.transfer_patient(patient_id=result["patient_id"], to_department=DEPT_F2,
                            reason="Needs cardiology", moved_by=world["users"]["doc_f1"])


def test_with_one_facility_the_same_transfer_is_allowed(world, one_facility):
    result = _admit(world, "doc_f1", DEPT_F1, "Gisande Morrevay")
    moved = fd.transfer_patient(patient_id=result["patient_id"], to_department=DEPT_F2,
                                reason="Needs cardiology", moved_by=world["users"]["doc_f1"])
    assert moved["current_department"] == DEPT_F2


# ═════════════════════════════════════════════════════════════════════════════
#  5. User Management stores the master's spelling
# ═════════════════════════════════════════════════════════════════════════════

def test_a_department_is_stored_in_the_masters_spelling(world, fence_open):
    target = world["users"]["nurse_f2"]
    resp = client.patch(f"/api/admin/users/{target}", headers=_auth(world, "admin_f2"),
                        json={"department": f"  {DEPT_F2.lower()} "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["department"] == DEPT_F2
    with db_session() as db:
        assert db.execute("SELECT department FROM users WHERE id = ?",
                          (target,)).fetchone()[0] == DEPT_F2


def test_with_one_facility_the_typed_spelling_is_kept(world, one_facility):
    target = world["users"]["nurse_f2"]
    try:
        resp = client.patch(f"/api/admin/users/{target}", headers=_auth(world, "admin_f1"),
                            json={"department": DEPT_F2.lower()})
        assert resp.status_code == 200, resp.text
        assert resp.json()["department"] == DEPT_F2.lower()
    finally:
        with db_session() as db:
            db.execute("UPDATE users SET department = ? WHERE id = ?", (DEPT_F2, target))


# ═════════════════════════════════════════════════════════════════════════════
#  6. site.toml [[facilities]]
# ═════════════════════════════════════════════════════════════════════════════

SITE = """
[site]
name = "Quandmere Hospital"

[[departments]]
name = "General Medicine"
"""


@pytest.fixture()
def site_file(tmp_path):
    def write(body: str):
        path = tmp_path / "site.toml"
        path.write_text(SITE + body, encoding="utf-8")
        return path
    yield write
    load_site_config(tmp_path / "no-such-site.toml", apply_env=False)


def _refused(path) -> SiteConfigError:
    with pytest.raises(SiteConfigError) as err:
        load_site_config(path, apply_env=False)
    return err.value


def test_facilities_blocks_load_and_bind_sources(site_file):
    cfg = load_site_config(site_file("""
[[facilities]]
code = "main"
ingestion_sources = ["monitor_hl7"]

[[facilities]]
code = "SAT1"
downtime_mirror_dir = "/Volumes/WardPacks/SAT1"
ingestion_sources = ["icu-feed"]
"""), apply_env=False)
    assert [f["code"] for f in cfg.facilities] == ["MAIN", "SAT1"]
    assert cfg.facility_for_source(" MONITOR_HL7 ") == "MAIN"
    assert cfg.facility_for_source("icu-feed") == "SAT1"
    assert cfg.facility_for_source("ward-7-feed") is None
    from services.site_config_service import facility_for_ingestion_source, site_config_snapshot
    assert facility_for_ingestion_source("icu-feed") == "SAT1"
    snap = site_config_snapshot()["facilities"]
    assert snap[1] == {"code": "SAT1", "ingestion_sources": ["icu-feed"],
                       "downtime_mirror_set": True}
    assert "/Volumes" not in json.dumps(snap)


def test_an_unknown_key_under_facilities_fails_with_the_file_and_line(site_file):
    path = site_file("""
[[facilities]]
code = "MAIN"
ingestion_source = ["monitor_hl7"]
""")
    err = _refused(path)
    assert err.line == 10, str(err)
    assert "ingestion_source" in str(err) and str(path) in str(err)
    assert "ingestion_sources" in str(err)      # the near miss is offered


@pytest.mark.parametrize("body,needle", [
    ('[[facilities]]\ncode = "M"\n', "not a facility code"),
    ('[[facilities]]\ncode = "MAIN-1"\n', "not a facility code"),
    ('[[facilities]]\ncode = "MAIN"\n[[facilities]]\ncode = "main"\n', "listed twice"),
    ('[[facilities]]\ncode = "MAIN"\ndowntime_mirror_dir = "packs/main"\n', "full folder path"),
    ('[[facilities]]\ncode = "MAIN"\ningestion_sources = ["a"]\n'
     '[[facilities]]\ncode = "SAT1"\ningestion_sources = ["A"]\n', "listed twice"),
    ('[[facilities]]\ncode = "MAIN"\ningestion_sources = []\n', "empty list"),
    ('[[facilities]]\ncode = ""\n', "is empty"),
    ('[facilities]\ncode = "MAIN"\n', "[[facilities]] blocks"),
    ('[[facilities]]\ningestion_sources = ["a"]\n', "missing the required setting 'code'"),
])
def test_a_bad_facilities_block_is_refused_with_its_line(site_file, body, needle):
    err = _refused(site_file("\n" + body))
    assert needle in str(err), str(err)
    assert err.line is not None, str(err)
