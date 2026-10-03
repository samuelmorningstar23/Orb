"""Scribe Quick Admission goes through admit_patient, not a bare patients row.

Until 2026-10-02 the panel posted to POST /api/vigil/patients, which wrote a
patient marked 'admitted' with no admissions row: the patient was on no ward
board and failed closed in every department scope, while the modal reported
success. It also offered three hard-coded seed doctor ids, which fail the
foreign key at any real site.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app
from services.admission_service import DEPARTMENT_NOT_COVERED
from services.clinical_records_service import list_allergies
from services.frontdesk_service import list_covering_departments

client = TestClient(app)
_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded():
    """Close what this module admits (see test_admission_provenance for why
    both rows are closed)."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'",
                       [(p,) for p in _SEEDED])
    _SEEDED.clear()


def _user(role="doctor", department="General Medicine", active=1):
    uid = f"QAD{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?,?,?,?,?,?)",
            (uid, f"Dr {uid}", role, department, hash_password("secretpass"), active),
        )
    return uid


def _hdr(uid):
    resp = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _body(attending, **over):
    body = {"name": "Quick Admit Patient", "age": 54, "sex": "F",
            "department": "General Medicine", "ward": "GM-1", "bed": "7",
            "attending_doctor_id": attending, "admission_source": "er",
            "chief_complaint": "Fever and productive cough",
            "allergies": "Penicillin; NKDA"}
    body.update(over)
    return body


def test_quick_admitted_patient_is_on_the_admitting_departments_board():
    doc = _user()
    hdr = _hdr(doc)
    resp = client.post("/api/scribe/quick-admit", json=_body(doc), headers=hdr)
    assert resp.status_code == 200, resp.text
    res = resp.json()
    _SEEDED.append(res["patient_id"])
    assert res["temporary_password"] and res["patient_login_id"]

    with db_session() as db:
        adm = db.execute("SELECT department, ward, status, attending_doctor_id "
                         "FROM admissions WHERE patient_id = ?",
                         (res["patient_id"],)).fetchone()
        audit = db.execute("SELECT actor_user_id FROM audit_logs "
                           "WHERE action_type = 'patient_admitted' AND patient_id = ?",
                           (res["patient_id"],)).fetchone()
    assert adm is not None, "a quick admission must open an admission"
    assert (adm["department"], adm["ward"], adm["status"]) == ("General Medicine", "GM-1", "active")
    assert adm["attending_doctor_id"] == doc
    assert audit is not None and audit["actor_user_id"] == doc

    board = client.get("/api/vigil/patients", headers=hdr)
    assert board.status_code == 200, board.text
    assert res["patient_id"] in {p["id"] for p in board.json()}

    # Allergies typed on the quick form reach the interlock, not only a narrative.
    assert [a["substance"] for a in list_allergies(res["patient_id"])] == ["penicillin"]


def test_attending_must_be_an_active_doctor_from_the_staff_table():
    doc = _user()
    hdr = _hdr(doc)
    for bad in ("DOC-NOT-ON-STAFF", _user(role="nurse"), _user(active=0)):
        resp = client.post("/api/scribe/quick-admit", json=_body(bad), headers=hdr)
        assert resp.status_code == 422, (bad, resp.text)


def test_missing_department_or_name_is_refused_not_defaulted():
    doc = _user()
    hdr = _hdr(doc)
    assert client.post("/api/scribe/quick-admit", json=_body(doc, department=""),
                       headers=hdr).status_code == 422
    assert client.post("/api/scribe/quick-admit", json=_body(doc, name=" "),
                       headers=hdr).status_code == 422


def test_nurse_cannot_quick_admit_or_list_options():
    nurse = _user(role="nurse")
    hdr = _hdr(nurse)
    assert client.get("/api/scribe/admission-options", headers=hdr).status_code == 403
    assert client.post("/api/scribe/quick-admit", json=_body(_user()),
                       headers=hdr).status_code == 403


def test_admission_options_list_active_doctors_only():
    doc = _user(department="Cardiology")
    inactive = _user(active=0)
    nurse = _user(role="nurse")
    resp = client.get("/api/scribe/admission-options", headers=_hdr(doc))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {d["id"] for d in body["doctors"]}
    assert doc in ids
    assert inactive not in ids and nurse not in ids
    assert body["default_department"] == "Cardiology"


# ── department (same rule as POST /api/admissions/admit) ──────────────────────
# permissions.department_scope matches admissions.department to users.department
# exactly. Until 2026-10-03 this lane passed the typed department straight
# through, so "cardiology" or "Cardiolgy" admitted a patient onto nobody's board.

def _named(name):
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM patients WHERE name = ?", (name,)).fetchone()[0]


def test_a_department_nobody_covers_is_refused_with_422_and_nothing_written():
    doc = _user()
    hdr = _hdr(doc)
    name = f"Orphan Risk {uuid.uuid4().hex[:6]}"
    resp = client.post("/api/scribe/quick-admit",
                       json=_body(doc, name=name, department=f"Cardiolgy {uuid.uuid4().hex[:4]}"),
                       headers=hdr)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == DEPARTMENT_NOT_COVERED
    assert _named(name) == 0


def test_a_differently_spelled_department_is_stored_in_its_canonical_spelling():
    doc = _user()
    hdr = _hdr(doc)
    resp = client.post("/api/scribe/quick-admit",
                       json=_body(doc, department="  general MEDICINE "), headers=hdr)
    assert resp.status_code == 200, resp.text
    res = resp.json()
    _SEEDED.append(res["patient_id"])

    with db_session() as db:
        adm = db.execute("SELECT department FROM admissions WHERE patient_id = ?",
                         (res["patient_id"],)).fetchone()
        audit = db.execute("SELECT summary_message, metadata_json FROM audit_logs "
                           "WHERE action_type = 'patient_admitted' AND patient_id = ?",
                           (res["patient_id"],)).fetchone()
    assert adm["department"] == "General Medicine"
    assert json.loads(audit["metadata_json"])["department"] == "General Medicine"
    assert "General Medicine" in audit["summary_message"]

    board = client.get("/api/vigil/patients", headers=hdr)
    assert board.status_code == 200, board.text
    assert res["patient_id"] in {p["id"] for p in board.json()}


def test_admission_options_offer_the_covered_departments_with_a_canonical_default():
    doc = _user(department="Cardiology")
    body = client.get("/api/scribe/admission-options", headers=_hdr(doc)).json()
    assert body["departments"] == list_covering_departments()
    assert "Cardiology" in body["departments"]
    assert body["default_department"] == "Cardiology"
