"""The command center's scoped deterioration list carries its own boundary.

A ward-scoped viewer gets only their department's named deteriorations. The
payload must say which department that is AND how many there are house-wide,
or an empty scoped list reads as a quiet hospital.
"""
import uuid

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)

DEPT_A = "CC Scope Label Cardiology"
DEPT_B = "CC Scope Label Oncology"
_SEEDED: list[str] = []


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _user(role, dept):
    uid = f"CCSL{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'CC Scope Label', ?, 'x', ?)", (uid, role, dept))
    return uid


def _critical_patient(dept):
    pid = f"CCSLP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('CCSLADM', 'CC Scope Label Admitter', 'doctor', 'x')")
        db.execute("INSERT INTO patients (id, name, mrn, ward, bed, status) "
                   "VALUES (?, 'CC Scope Label', ?, ?, 'B-1', 'admitted')",
                   (pid, f"MRN-{pid}", dept))
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, status, admitted_by) "
                   "VALUES (?, ?, ?, ?, 'active', 'CCSLADM')", (f"ADM-{pid}", pid, dept, dept))
        db.execute("INSERT INTO risk_scores (patient_id, score, level) VALUES (?, 0.9, 'critical')",
                   (pid,))
    _SEEDED.append(pid)
    return pid


def teardown_module(_module):
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])


def test_a_scoped_viewer_is_told_the_boundary_and_the_house_count():
    _critical_patient(DEPT_B)
    nurse = _user("nurse", DEPT_A)

    resp = client.get("/api/command-center", headers=_auth(nurse, "nurse"))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deteriorations_scope"] == DEPT_A
    # The other ward's critical patient is not named here but IS counted.
    assert body["deteriorations_house_count"] >= len(body["deteriorations"]) + 1


def test_an_unscoped_admin_gets_no_boundary_label():
    _critical_patient(DEPT_B)
    admin = _user("admin", None)

    body = client.get("/api/command-center", headers=_auth(admin, "admin")).json()

    assert body.get("deteriorations_scope") is None
    assert "deteriorations_house_count" not in body
