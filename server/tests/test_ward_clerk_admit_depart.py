"""The ward clerk's admission, 'left the ward' and ordered transfer (adt-17b,
HIS wave 9, W9-A). Rulings ADT-S5 (a), ADT-S3 and ADT-S8 (b), applied as
build defaults behind ORB_ADT (default off) for Samuel to confirm.

What this file defends:

  1. SWITCHED OFF IS 404. With ORB_ADT off every new route answers 404.
  2. ONLY THE CLERK. The clerk's lanes refuse a doctor (no adt.bed) and the
     administrator (adt.bed, but not the clerk), audited; the doctor's own
     doors stay closed to the clerk (pinned in test_ward_clerk_role.py).
  3. A CLERK-ADMITTED PATIENT NEVER PRINTS NKDA. The admission stores every
     clinical field as not yet assessed: the complaint reads 'Not yet
     assessed', no diagnosis, triage, code status or note; NO allergy row and
     NO allergy review is written; the case room and Sage's memory say the
     allergies are unknown, never 'none'. A clinical field in the body is 422.
  4. THE INTERLOCK TREATS UNKNOWN AS UNKNOWN: the bedside screen reports no
     allergy on file (never a clean, reviewed list) and the OPD review status
     reads 'not_reviewed'.
  5. THE BOARD FLAGS 'Admission assessment due' on the bed, inside the
     viewer's scope only, until a doctor records the admission assessment;
     the assessment follows the OPD review's rules and is recorded once.
  6. THE CLERK'S OWN DEPARTMENT, an attending doctor of it, the hospital's
     own number and an Idempotency-Key are required; a replay admits once.
  7. 'LEFT THE WARD' (ADT-S3): only on an Orb Ward Record computer; through
     end_stay_on, so discharge_preconditions and the MRN read-back apply; no
     end type is guessed, no disposition, no summary, no link; a patient on
     leave is refused; a replay records once.
  8. ORDERED TRANSFER (ADT-S8 (b)): only with the ordering doctor named (the
     attending doctor or a doctor of the current department), recorded as
     named by the clerk, with a clinical_access row and a case-room line; a
     move inside one department is refused.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import allergy_interlock
from services import bed_master_service as bm
from services import opd_service
from services.adt import clerk as adt_clerk
from services.adt import end_stay as adt_end
from services.adt import presence as adt_presence

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds D001 (doctor) and N001 (nurse) in it
DOCTOR = {"id": "D001", "role": "doctor", "department": DEPT}

_SEEDED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _discharge_what_this_file_admitted():
    """Leave no admitted patient behind: other files count the census."""
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
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    yield


@pytest.fixture()
def ward_record(adt_on, monkeypatch):
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    yield


def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"WK{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Clerk test {role} {uid[-4:]}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


@pytest.fixture()
def clerk() -> dict:
    return _user("ward_clerk")


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
    ward = bm.save_ward({"code": f"WK-{_tag()}", "department": department,
                         "cleaning_tracked": False}, actor=_admin())
    if labels:
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


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _audits(action: str, **where) -> list[dict]:
    sql = "SELECT * FROM audit_logs WHERE action_type = ?"
    params: list = [action]
    for column, value in where.items():
        sql += f" AND {column} = ?"
        params.append(value)
    return _all(sql + " ORDER BY id", tuple(params))


# An invented name: a name holding an ordinary word ("Patient", "Admitted") is read by
# Sage's name matcher in later test files, so "the patient" there would match it.
def _clerk_body(ward: dict | None = None, label: str = "1", **over) -> dict:
    body = {"name": f"Clermira {_tag()}", "age": 63, "sex": "F",
            "mrn": f"HN-{uuid.uuid4().hex[:8].upper()}",
            "admission_source": "er", "admission_type": "inpatient",
            "attending_doctor_id": "D001", "ward": "", "bed": ""}
    if ward is not None:
        body.update(ward=ward["code"], bed=label, ward_id=ward["id"],
                    bed_id=_bed(ward["id"], label)["id"])
    else:
        body["awaiting_bed"] = True
    body.update(over)
    return body


def _clerk_admit(clerk: dict, body: dict, key: str | None = None):
    resp = client.post("/api/admissions/clerk-admit", headers=_hdr(clerk, key or f"ca-{_tag()}"),
                       json=body)
    if resp.status_code in (200, 201):
        _SEEDED.append(resp.json()["patient_id"])
    return resp


def _admitted(clerk: dict, ward: dict | None = None, label: str = "1") -> dict:
    body = _clerk_body(ward, label)
    resp = _clerk_admit(clerk, body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    return {"pid": out["patient_id"], "aid": out["admission_id"], "mrn": body["mrn"],
            "name": body["name"], "answer": out}


def _doctor_admit(ward: dict, label: str, department: str = DEPT, attending: str = "D001") -> dict:
    bed = _bed(ward["id"], label)
    resp = client.post("/api/admissions/admit", headers=_hdr(DOCTOR, f"da-{_tag()}"), json={
        "name": f"Doctavi {_tag()}", "age": 50, "sex": "M", "admission_source": "er",
        "admission_type": "inpatient", "department": department, "ward": ward["code"],
        "bed": label, "ward_id": ward["id"], "bed_id": bed["id"],
        "attending_doctor_id": attending, "chief_complaint": "Fever"})
    assert resp.status_code == 201, resp.text
    out = resp.json()
    _SEEDED.append(out["patient_id"])
    return {"pid": out["patient_id"], "aid": out["admission_id"], "bed_id": bed["id"]}


def _case_room_text(pid: str) -> str:
    rows = _all("SELECT m.content FROM messages m JOIN channels c ON c.id = m.channel_id "
                "WHERE c.patient_id = ? AND c.type = 'case_room' ORDER BY m.id", (pid,))
    return "\n".join(r["content"] for r in rows)


def _tile_for(user: dict, ward: dict, pid: str | None = None, label: str | None = None) -> dict:
    resp = client.get("/api/adt/board", params={"ward_id": ward["id"]}, headers=_hdr(user))
    assert resp.status_code == 200, resp.text
    for w in resp.json()["wards"]:
        for t in w["tiles"]:
            if label is not None and t["bed_label"] == label:
                return t
            if pid is not None and (t.get("occupant") or {}).get("patient_id") == pid:
                return t
    raise AssertionError("tile not found")


# ═════════════════════════════════════════════════════════════════════════════
#  1. Switched off, and only the clerk
# ═════════════════════════════════════════════════════════════════════════════

def test_every_new_route_is_404_with_the_module_off(monkeypatch, clerk):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    for method, path in (("get", "/api/admissions/clerk-options"),
                         ("post", "/api/admissions/clerk-admit"),
                         ("post", "/api/admissions/ADM-NOPE/assessment"),
                         ("post", "/api/adt/admissions/ADM-NOPE/left-ward"),
                         ("post", "/api/adt/ordered-transfers")):
        user = DOCTOR if path.endswith("/assessment") else clerk
        resp = getattr(client, method)(path, headers=_hdr(user, "k-off"),
                                       **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 404, (path, resp.text)


def test_the_doctor_and_the_administrator_cannot_use_the_clerks_lanes(adt_on):
    ward = _ward()
    body = _clerk_body(ward)
    doctor = client.post("/api/admissions/clerk-admit", headers=_hdr(DOCTOR, f"k-{_tag()}"),
                         json=body)
    assert doctor.status_code == 403, doctor.text
    admin = _admin()
    before = len(_audits("permission_denied", actor_user_id=admin["id"]))
    refused = client.post("/api/admissions/clerk-admit", headers=_hdr(admin, f"k-{_tag()}"),
                          json=body)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["error"] == "clerk_only"
    assert len(_audits("permission_denied", actor_user_id=admin["id"])) == before + 1
    assert _one("SELECT COUNT(*) AS n FROM patients WHERE name = ?", (body["name"],))["n"] == 0
    assert client.get("/api/admissions/clerk-options", headers=_hdr(admin)).status_code == 403


def test_a_clerk_with_no_department_is_refused(adt_on):
    stray = _user("ward_clerk", None)
    resp = client.get("/api/admissions/clerk-options", headers=_hdr(stray))
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "clerk_only"


# ═════════════════════════════════════════════════════════════════════════════
#  2. The registration-level admission, never NKDA
# ═════════════════════════════════════════════════════════════════════════════

def test_the_options_offer_the_clerks_department_and_its_doctors_only(adt_on, clerk):
    other = f"Clerk Other {_tag()}"
    stranger = _user("doctor", other)
    resp = client.get("/api/admissions/clerk-options", headers=_hdr(clerk))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["department"] == DEPT and out["departments"] == [DEPT]
    ids = {d["id"] for d in out["doctors"]}
    assert "D001" in ids and stranger["id"] not in ids
    assert "wards" in out["master"] and out["clinical_fields"] == "Not yet assessed"


def test_a_clerk_admission_stores_every_clinical_field_as_not_yet_assessed(adt_on, clerk):
    ward = _ward()
    p = _admitted(clerk, ward, "2")
    out = p["answer"]
    assert out["clerk_admission"] is True and out["assessment_due"] is True
    assert out["clinical_fields"] == "Not yet assessed"
    assert out["location"]["bed_id"] == _bed(ward["id"], "2")["id"]
    adm = _one("SELECT * FROM admissions WHERE id = ?", (p["aid"],))
    assert adm["chief_complaint"] == "Not yet assessed"
    assert adm["initial_diagnosis"] is None and adm["triage_severity"] is None
    assert adm["code_status"] is None and adm["admitting_notes"] is None
    assert adm["initial_vitals_json"] is None and adm["department"] == DEPT
    assert json.loads(adm["isolation_precautions"]) == []
    assert _one("SELECT diagnosis FROM patients WHERE id = ?",
                (p["pid"],))["diagnosis"] == "Not yet assessed"
    assert _one("SELECT mrn FROM patients WHERE id = ?", (p["pid"],))["mrn"] == p["mrn"]
    # No allergy row and no allergy review: nothing anywhere says 'none'.
    assert _all("SELECT * FROM patient_allergies WHERE patient_id = ?", (p["pid"],)) == []
    assert _all("SELECT * FROM patient_allergy_assessments WHERE patient_id = ?",
                (p["pid"],)) == []
    assert _all("SELECT * FROM vitals WHERE patient_id = ?", (p["pid"],)) == []
    movement = _one("SELECT * FROM patient_movements WHERE admission_id = ? "
                    "AND movement_type = 'admission'", (p["aid"],))
    assert json.loads(movement["detail_json"])["clerk_admission"]["clinical_fields"] == \
        "not_yet_assessed"
    rows = _audits("clerk_admission_recorded", patient_id=p["pid"])
    assert len(rows) == 1 and rows[0]["event_category"] == "clinical_access"


def test_a_clerk_admitted_patient_never_prints_nkda(adt_on, clerk):
    p = _admitted(clerk)
    room = _case_room_text(p["pid"])
    assert "Allergies: Not yet assessed" in room
    assert "Nobody has asked yet" in room and "Admission assessment due" in room
    assert "NKDA" not in room.upper().replace("NOT 'NO KNOWN ALLERGIES'", "")
    memory = _one("SELECT patient_summary FROM sage_room_memory WHERE patient_id = ?",
                  (p["pid"],))["patient_summary"]
    assert "allergies are unknown, not none" in memory and "NKDA" not in memory
    profile = _one("SELECT allergies FROM patient_profiles WHERE patient_id = ?", (p["pid"],))
    assert json.loads(profile["allergies"]) == []


def test_the_interlock_treats_unknown_allergy_status_as_unknown(adt_on, clerk):
    p = _admitted(clerk)
    with db_session() as db:
        screened = allergy_interlock.screen(db, p["pid"], "amoxicillin")
        review = opd_service.allergy_review_status(db, p["pid"])
    assert screened["allergies_on_file"] == 0 and screened["screen_failed"] is False
    assert screened["alerts"] == []
    assert review["status"] == "not_reviewed" and review["can_sign"] is False


@pytest.mark.parametrize("field,value", [
    ("allergies", ["NKDA"]), ("chief_complaint", "Fever"), ("code_status", "full_code"),
    ("vitals", {"hr": 90}), ("initial_diagnosis", "Sepsis"), ("admitting_notes", "x"),
])
def test_a_clinical_field_in_the_clerks_body_is_refused(adt_on, clerk, field, value):
    body = _clerk_body(**{field: value})
    resp = _clerk_admit(clerk, body)
    assert resp.status_code == 422, resp.text
    assert _one("SELECT COUNT(*) AS n FROM patients WHERE name = ?", (body["name"],))["n"] == 0


def test_the_clerk_admits_only_to_their_own_department(adt_on, clerk):
    other = f"Clerk Other {_tag()}"
    _user("nurse", other)
    body = _clerk_body(department=other)
    resp = _clerk_admit(clerk, body)
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "department_not_own"
    stranger = _user("doctor", other)
    resp = _clerk_admit(clerk, _clerk_body(attending_doctor_id=stranger["id"]))
    assert resp.status_code == 400 and DEPT in resp.json()["detail"]


def test_the_hospital_number_and_the_key_are_required_and_a_replay_admits_once(adt_on, clerk):
    resp = _clerk_admit(clerk, _clerk_body(mrn=""))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "hospital_number_required"
    body = _clerk_body()
    no_key = client.post("/api/admissions/clerk-admit", headers=_hdr(clerk), json=body)
    assert no_key.status_code == 422
    assert no_key.json()["detail"]["error"] == "idempotency_key_required"
    key = f"ca-{_tag()}"
    first = _clerk_admit(clerk, body, key)
    assert first.status_code == 201, first.text
    again = _clerk_admit(clerk, body, key)
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True and again.json()["clerk_admission"] is True
    assert again.json()["temporary_password"] is None
    assert _one("SELECT COUNT(*) AS n FROM patients WHERE name = ?", (body["name"],))["n"] == 1


def test_strict_locations_refuse_the_clerks_unlisted_ward_too(adt_on, clerk, monkeypatch):
    monkeypatch.setattr(config, "ADT_STRICT_LOCATIONS_ENABLED", True)
    body = _clerk_body()
    body.update(ward=f"Nowhere {_tag()}", bed="1")
    body.pop("awaiting_bed")
    resp = _clerk_admit(clerk, body)
    assert resp.status_code == 400 and resp.json()["detail"]["error"] == "ward_not_in_setup"
    assert _one("SELECT COUNT(*) AS n FROM patients WHERE name = ?", (body["name"],))["n"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  3. 'Admission assessment due' and the doctor's assessment
# ═════════════════════════════════════════════════════════════════════════════

def test_the_board_flags_admission_assessment_due_inside_scope_only(adt_on, clerk):
    ward = _ward()
    p = _admitted(clerk, ward, "3")
    tile = _tile_for(clerk, ward, pid=p["pid"])
    assert adt_clerk.ASSESSMENT_DUE_MARKER in tile["markers"]
    assert adt_clerk.ASSESSMENT_DUE_MARKER in _tile_for(DOCTOR, ward, pid=p["pid"])["markers"]
    # A board outside the patient's department names nobody and says nothing.
    outsider = _user("ward_clerk", f"Clerk Other {_tag()}")
    with db_session() as db:
        db.execute("UPDATE wards SET department = ? WHERE id = ?", (outsider["department"],
                                                                     ward["id"]))
    try:
        hidden = _tile_for(outsider, ward, label="3")
        assert hidden["occupant"] is None
        assert adt_clerk.ASSESSMENT_DUE_MARKER not in hidden["markers"]
    finally:
        with db_session() as db:
            db.execute("UPDATE wards SET department = ? WHERE id = ?", (DEPT, ward["id"]))


def test_a_doctors_admission_is_never_flagged(adt_on):
    ward = _ward()
    p = _doctor_admit(ward, "1")
    assert adt_clerk.ASSESSMENT_DUE_MARKER not in _tile_for(DOCTOR, ward, pid=p["pid"])["markers"]
    resp = client.post(f"/api/admissions/{p['aid']}/assessment", headers=_hdr(DOCTOR), json={
        "allergy_status": "none_known", "chief_complaint": "Fever", "code_status": "full_code"})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "not_clerk_admission"


def test_the_doctors_assessment_clears_the_flag_and_is_recorded_once(adt_on, clerk):
    ward = _ward()
    p = _admitted(clerk, ward, "1")
    path = f"/api/admissions/{p['aid']}/assessment"
    nurse = _user("nurse")
    assert client.post(path, headers=_hdr(nurse), json={}).status_code == 403
    assert client.post(path, headers=_hdr(clerk), json={}).status_code == 403
    resp = client.post(path, headers=_hdr(DOCTOR), json={
        "allergy_status": "allergies_recorded", "allergies": ["Penicillin"],
        "chief_complaint": "Breathless for two days", "initial_diagnosis": "Pneumonia",
        "code_status": "full_code"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["assessment_due"] is False and out["charted"] == ["penicillin"]
    review = _one("SELECT * FROM patient_allergy_assessments WHERE admission_id = ?", (p["aid"],))
    assert review["source"] == "admission" and review["status"] == "allergies_recorded"
    assert review["assessed_by"] == "D001"
    adm = _one("SELECT chief_complaint, initial_diagnosis, code_status FROM admissions "
               "WHERE id = ?", (p["aid"],))
    assert adm == {"chief_complaint": "Breathless for two days", "initial_diagnosis": "Pneumonia",
                   "code_status": "full_code"}
    assert adt_clerk.ASSESSMENT_DUE_MARKER not in _tile_for(clerk, ward, pid=p["pid"])["markers"]
    rows = _audits("admission_assessment_recorded", patient_id=p["pid"])
    assert len(rows) == 1 and rows[0]["event_category"] == "clinical_access"
    with db_session() as db:
        assert allergy_interlock.screen(db, p["pid"], "amoxicillin")["decision"] != "allow"
    again = client.post(path, headers=_hdr(DOCTOR), json={
        "allergy_status": "none_known", "chief_complaint": "x", "code_status": "dnr"})
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "assessment_already_recorded"


def test_the_assessment_follows_the_review_rules(adt_on, clerk):
    p = _admitted(clerk)
    path = f"/api/admissions/{p['aid']}/assessment"
    base = {"chief_complaint": "Fever", "code_status": "full_code"}
    cases = [
        ({"allergy_status": "maybe"}, 422, "bad_allergy_status"),
        ({"allergy_status": "none_known", "allergies": ["Sulfa"]}, 422, "substances_not_allowed"),
        ({"allergy_status": "unable_to_obtain"}, 422, "reason_required"),
        ({"allergy_status": "allergies_recorded"}, 422, "no_allergies_named"),
        ({"allergy_status": "none_known", "chief_complaint": "Not yet assessed"}, 422,
         "chief_complaint_required"),
        ({"allergy_status": "none_known", "code_status": "maybe"}, 422, "code_status_required"),
    ]
    for extra, status, code in cases:
        resp = client.post(path, headers=_hdr(DOCTOR), json={**base, **extra})
        assert resp.status_code == status, (extra, resp.text)
        assert resp.json()["detail"]["error"] == code, extra
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, severity, status, "
                   "source) VALUES (?, 'latex', 'unknown', 'active', 'clinician')", (p["pid"],))
    resp = client.post(path, headers=_hdr(DOCTOR), json={**base, "allergy_status": "none_known"})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "allergies_on_chart"
    assert _all("SELECT * FROM patient_allergy_assessments WHERE admission_id = ?",
                (p["aid"],)) == []
    done = client.post(path, headers=_hdr(DOCTOR), json={
        **base, "allergy_status": "unable_to_obtain", "allergies": None,
        "reason": "Unconscious, nobody with him knows"})
    assert done.status_code == 200, done.text


def test_an_out_of_department_doctor_gets_404_for_the_assessment(adt_on, clerk):
    p = _admitted(clerk)
    other = _user("doctor", f"Clerk Other {_tag()}")
    resp = client.post(f"/api/admissions/{p['aid']}/assessment", headers=_hdr(other), json={
        "allergy_status": "none_known", "chief_complaint": "Fever", "code_status": "full_code"})
    assert resp.status_code == 404, resp.text


# ═════════════════════════════════════════════════════════════════════════════
#  4. 'Left the ward' (ADT-S3)
# ═════════════════════════════════════════════════════════════════════════════

def _left(clerk: dict, p: dict, *, mrn: str | None = None, key: str | None = None, **body):
    return client.post(f"/api/adt/admissions/{p['aid']}/left-ward",
                       headers=_hdr(clerk, key or f"lw-{_tag()}"),
                       json={"confirm_mrn": p["mrn"] if mrn is None else mrn, **body})


def test_left_the_ward_is_refused_outside_the_ward_record(adt_on, clerk):
    p = _admitted(clerk, _ward(), "1")
    resp = _left(clerk, p)
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "left_ward_not_here"
    assert _one("SELECT status FROM admissions WHERE id = ?", (p["aid"],))["status"] == "active"


def test_left_the_ward_needs_the_mrn_read_back(ward_record, clerk):
    p = _admitted(clerk, _ward(), "1")
    resp = _left(clerk, p, mrn="HN-WRONG")
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "read_back_mismatch"
    assert _one("SELECT status FROM admissions WHERE id = ?", (p["aid"],))["status"] == "active"
    assert _all("SELECT * FROM patient_movements WHERE admission_id = ? "
                "AND movement_type = 'discharge'", (p["aid"],)) == []


def test_left_the_ward_ends_the_stay_with_no_guessed_type(ward_record, clerk):
    ward = _ward()
    p = _admitted(clerk, ward, "2")
    key = f"lw-{_tag()}"
    resp = _left(clerk, p, key=key, notes="Collected by family")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["left_ward"] is True and out["end_type"] is None
    assert out["discharge_link_issued"] is False and out["disposition"] is None
    adm = _one("SELECT status, discharge_type, discharged_by FROM admissions WHERE id = ?",
               (p["aid"],))
    assert adm == {"status": "discharged", "discharge_type": None, "discharged_by": clerk["id"]}
    move = _one("SELECT * FROM patient_movements WHERE admission_id = ? "
                "AND movement_type = 'discharge'", (p["aid"],))
    detail = json.loads(move["detail_json"])
    assert detail["door"] == "clerk_left_ward" and detail["end_type_not_stated"] is True
    assert "Left the ward" in move["reason"]
    assert _all("SELECT * FROM nabh_discharge_dispositions WHERE admission_id = ?",
                (p["aid"],)) == []
    assert _all("SELECT * FROM discharge_summaries WHERE patient_id = ?", (p["pid"],)) == []
    assert len(_audits("stay_ended", patient_id=p["pid"])) == 1
    # The bed is free on the board.
    assert _tile_for(clerk, ward, label="2")["occupant"] is None
    again = _left(clerk, p, key=key)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert len(_audits("stay_ended", patient_id=p["pid"])) == 1


def test_left_the_ward_goes_through_discharge_preconditions(ward_record, clerk, monkeypatch):
    p = _admitted(clerk)
    problem = [{"code": "r009", "message": "Held"}]
    monkeypatch.setattr(adt_end, "discharge_preconditions", lambda db, pid, t: problem)
    resp = _left(clerk, p)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "discharge_preconditions"
    assert _one("SELECT status FROM admissions WHERE id = ?", (p["aid"],))["status"] == "active"


def test_a_patient_on_leave_is_not_the_clerks_to_close(ward_record, clerk):
    p = _admitted(clerk, _ward(), "1")
    adt_presence.start_leave(p["aid"], actor=DOCTOR, reason="Home for a wedding",
                             idempotency_key=f"lv-{_tag()}")
    resp = _left(clerk, p)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "patient_not_on_ward"
    assert _one("SELECT status FROM admissions WHERE id = ?", (p["aid"],))["status"] == "active"


def test_left_the_ward_is_the_clerks_own_department_only(ward_record, clerk):
    other_clerk = _user("ward_clerk", f"Clerk Other {_tag()}")
    p = _admitted(clerk)
    resp = _left(other_clerk, p)
    assert resp.status_code == 404, resp.text
    admin = _admin()
    assert _left(admin, p).status_code == 403
    assert client.post(f"/api/adt/admissions/{p['aid']}/left-ward",
                       headers=_hdr(clerk, f"lw-{_tag()}"),
                       json={"confirm_mrn": p["mrn"], "end_type": "death"}).status_code == 422
    assert _one("SELECT status FROM admissions WHERE id = ?", (p["aid"],))["status"] == "active"


# ═════════════════════════════════════════════════════════════════════════════
#  5. A transfer the doctor ordered (ADT-S8 (b))
# ═════════════════════════════════════════════════════════════════════════════

def _receiving() -> dict:
    dept = f"Clerk Recv {_tag()}"
    doctor = _user("doctor", dept)
    _user("nurse", dept)
    return {"dept": dept, "doctor": doctor, "ward": _ward(department=dept)}


def _ordered(clerk: dict, p: dict, recv: dict, *, ordered_by: str | None = "D001",
             key: str | None = None, **over):
    body = {"admission_id": p["aid"], "to_department": recv["dept"],
            "ward_id": recv["ward"]["id"], "bed_id": _bed(recv["ward"]["id"], "1")["id"],
            "expected_bed_id": p["bed_id"], "reason": "Accepted by the receiving team"}
    if ordered_by is not None:
        body["ordered_by"] = ordered_by
    body.update(over)
    return client.post("/api/adt/ordered-transfers", headers=_hdr(clerk, key or f"ot-{_tag()}"),
                       json=body)


def test_the_clerk_transfers_only_naming_the_ordering_doctor(adt_on, clerk):
    p = _doctor_admit(_ward(), "1")
    recv = _receiving()
    missing = _ordered(clerk, p, recv, ordered_by=None)
    assert missing.status_code == 422, missing.text
    plain = client.post("/api/adt/transfers", headers=_hdr(clerk, f"tr-{_tag()}"), json={
        "admission_id": p["aid"], "to_department": recv["dept"], "keep_location": True,
        "expected_bed_id": p["bed_id"], "reason": "x"})
    assert plain.status_code == 403
    key = f"ot-{_tag()}"
    resp = _ordered(clerk, p, recv, key=key)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["current_department"] == recv["dept"] and out["department_changed"] is True
    assert out["ordered_by"]["id"] == "D001"
    move = _one("SELECT * FROM patient_movements WHERE admission_id = ? "
                "AND movement_type = 'transfer'", (p["aid"],))
    assert "ordered by" in move["reason"] and "named by the ward clerk" in move["reason"]
    assert move["moved_by"] == clerk["id"]
    rows = _audits("transfer_order_carried_out", patient_id=p["pid"])
    assert len(rows) == 1 and rows[0]["event_category"] == "clinical_access"
    meta = json.loads(rows[0]["metadata_json"])
    assert meta["ordered_by"] == "D001" and meta["named_by"] == clerk["id"]
    assert meta["confirmed_by_doctor"] is False
    assert len(_audits("patient_transferred", patient_id=p["pid"],
                       event_category="clinical_access")) == 1
    assert "on the order of" in _case_room_text(p["pid"])
    again = _ordered(clerk, p, recv, key=key)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert len(_audits("transfer_order_carried_out", patient_id=p["pid"])) == 1


def test_the_ordering_doctor_must_treat_the_patient(adt_on, clerk):
    p = _doctor_admit(_ward(), "1")
    recv = _receiving()
    for doctor, code in ((recv["doctor"]["id"], "ordering_doctor_not_treating"),
                         ("N001", "ordering_doctor_invalid"),
                         ("NOBODY-HERE", "ordering_doctor_invalid")):
        resp = _ordered(clerk, p, recv, ordered_by=doctor)
        assert resp.status_code == 422, (doctor, resp.text)
        assert resp.json()["detail"]["error"] == code
    assert _one("SELECT department FROM admissions WHERE id = ?",
                (p["aid"],))["department"] == DEPT
    colleague = _user("doctor", DEPT)
    assert _ordered(clerk, p, recv, ordered_by=colleague["id"]).status_code == 200


def test_a_move_inside_the_department_is_not_an_ordered_transfer(adt_on, clerk):
    p = _doctor_admit(_ward(), "1")
    same = {"dept": DEPT, "ward": _ward()}
    resp = _ordered(clerk, p, same)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "no_department_change"


def test_the_administrator_and_other_departments_cannot_use_the_ordered_lane(adt_on, clerk):
    p = _doctor_admit(_ward(), "1")
    recv = _receiving()
    resp = _ordered(_admin(), p, recv)
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "clerk_only"
    other_clerk = _user("ward_clerk", recv["dept"])
    assert _ordered(other_clerk, p, recv).status_code == 404
    assert _one("SELECT department FROM admissions WHERE id = ?",
                (p["aid"],))["department"] == DEPT
