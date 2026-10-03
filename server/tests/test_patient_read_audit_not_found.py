"""A lookup of a patient who does not exist is not a successful PHI view.

require_patient_access(audit_action=...) wrote its "success" phi_access row
before the handler had looked the patient up, so an unscoped account asking for
an id that matches nobody put a successful chart view on the hash chain. The
row is now written once the patient is known: a real patient gets the success
row, a missing one gets a failure row with reason not_found, never a success.

GET /api/consent/{patient_id} is the probe because its handler answers 200 with
an empty list for an unknown id, so the route itself does not 404 first.
"""
import json
import uuid

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)


def _admin() -> str:
    uid = f"RDADM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        # department NULL: unscoped, so the ward check does not 404 first.
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES (?, 'Read Audit Admin', 'admin', 'x')", (uid,))
    return uid


def _rows(pid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT outcome, summary_message, metadata_json FROM audit_logs "
            "WHERE action_type = 'consent_register_read' AND patient_id = ? ORDER BY id",
            (pid,)).fetchall()]


def test_a_missing_patient_writes_a_failure_and_no_success_row():
    admin = _admin()
    pid = f"NOSUCH{uuid.uuid4().hex[:8].upper()}"

    resp = client.get(f"/api/consent/{pid}",
                      headers={"Authorization": f"Bearer {create_token(admin, 'admin')}"})
    assert resp.status_code == 200, resp.text

    rows = _rows(pid)
    assert [r["outcome"] for r in rows] == ["failure"], rows
    assert json.loads(rows[0]["metadata_json"])["reason"] == "not_found"
    assert "no such patient" in rows[0]["summary_message"]


def test_a_real_patient_still_gets_its_success_row():
    admin = _admin()
    pid = f"RDP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, diagnosis, status) "
            "VALUES (?, 'Read Audit Fixture', ?, 40, 'M', 'Fixture', 'discharged')",
            (pid, f"MRN-{pid}"))
    try:
        resp = client.get(f"/api/consent/{pid}",
                          headers={"Authorization": f"Bearer {create_token(admin, 'admin')}"})
        assert resp.status_code == 200, resp.text
        assert [r["outcome"] for r in _rows(pid)] == ["success"]
    finally:
        with db_session() as db:
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))
