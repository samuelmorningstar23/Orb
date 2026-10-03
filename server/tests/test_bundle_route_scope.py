"""Sepsis Six writes are clinical writes, ward-scoped, and a cancel says why.

Complete and cancel were gated on `alert.read`, which the admin account holds,
took a bare bundle id with no ward check, and cancel wrote no audit row and took
no reason. A completed or cancelled bundle could still have elements stamped.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services.sepsis_bundle_service import start_bundle

client = TestClient(app)

DEPT_A = "Bundle Scope Medicine"
DEPT_B = "Bundle Scope Surgery"


def _mk_user(role, department=None):
    uid = f"BSU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Bundle {role}", role, department),
        )
    return {"Authorization": f"Bearer {create_token(uid, role)}"}, uid


def _mk_patient(department):
    pid = f"BSP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('BSADM', 'Bundle Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
            (pid, f"Bundle Case {pid[-4:]}", f"MRN-{pid}"),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'B-1', 'active', 'BSADM')",
            (pid, department, department),
        )
    return pid


@pytest.fixture()
def bundle():
    pid = _mk_patient(DEPT_A)
    return start_bundle(pid, trigger_reason="test")


def _status(bundle_id):
    with db_session() as db:
        return db.execute("SELECT status FROM care_bundles WHERE id = ?", (bundle_id,)).fetchone()[0]


def test_admin_cannot_complete_or_cancel(bundle):
    hdr, _ = _mk_user("admin")
    r1 = client.post(f"/api/bundles/{bundle['id']}/complete/oxygen", headers=hdr)
    r2 = client.post(f"/api/bundles/{bundle['id']}/cancel", json={"reason": "x"}, headers=hdr)
    assert r1.status_code == 403, r1.text
    assert r2.status_code == 403, r2.text
    assert _status(bundle["id"]) == "active"


def test_admin_cannot_start_a_bundle():
    hdr, _ = _mk_user("admin")
    pid = _mk_patient(DEPT_A)
    resp = client.post(f"/api/bundles/patient/{pid}/start", json={}, headers=hdr)
    assert resp.status_code == 403, resp.text


def test_out_of_ward_clinician_gets_404(bundle):
    hdr, _ = _mk_user("nurse", DEPT_B)
    r1 = client.post(f"/api/bundles/{bundle['id']}/complete/oxygen", headers=hdr)
    r2 = client.post(f"/api/bundles/{bundle['id']}/cancel", json={"reason": "wrong patient"}, headers=hdr)
    assert r1.status_code == 404, r1.text
    assert r2.status_code == 404, r2.text
    assert _status(bundle["id"]) == "active"


def test_ward_nurse_completes_an_element(bundle):
    hdr, _ = _mk_user("nurse", DEPT_A)
    resp = client.post(f"/api/bundles/{bundle['id']}/complete/oxygen", headers=hdr)
    assert resp.status_code == 200, resp.text


def test_cancel_requires_a_reason(bundle):
    hdr, _ = _mk_user("doctor", DEPT_A)
    for body in ({}, {"reason": ""}, {"reason": "   "}):
        resp = client.post(f"/api/bundles/{bundle['id']}/cancel", json=body, headers=hdr)
        assert resp.status_code == 422, (body, resp.text)
    assert _status(bundle["id"]) == "active"


def test_cancel_writes_an_audit_row_with_actor_and_reason(bundle):
    hdr, uid = _mk_user("doctor", DEPT_A)
    reason = "Sepsis excluded: urinary retention, cultures negative"
    resp = client.post(f"/api/bundles/{bundle['id']}/cancel", json={"reason": reason}, headers=hdr)
    assert resp.status_code == 200, resp.text
    assert _status(bundle["id"]) == "cancelled"
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'bundle_cancelled' AND target_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (str(bundle["id"]),),
        ).fetchone()
    assert row is not None, "a cancelled sepsis bundle must leave an audit row"
    assert row["actor_user_id"] == uid
    assert row["patient_id"] == bundle["patient_id"]
    assert reason in row["summary_message"]


def test_a_cancelled_bundle_refuses_completion(bundle):
    hdr, _ = _mk_user("nurse", DEPT_A)
    assert client.post(f"/api/bundles/{bundle['id']}/cancel",
                       json={"reason": "duplicate"}, headers=hdr).status_code == 200
    resp = client.post(f"/api/bundles/{bundle['id']}/complete/fluids", headers=hdr)
    assert resp.status_code == 409, resp.text
    with db_session() as db:
        assert db.execute("SELECT fluids_at FROM care_bundles WHERE id = ?",
                          (bundle["id"],)).fetchone()[0] is None
    again = client.post(f"/api/bundles/{bundle['id']}/cancel", json={"reason": "again"}, headers=hdr)
    assert again.status_code == 409


def test_unknown_bundle_is_404():
    hdr, _ = _mk_user("nurse", DEPT_A)
    assert client.post("/api/bundles/987654321/complete/oxygen", headers=hdr).status_code == 404
    assert client.post("/api/bundles/987654321/cancel", json={"reason": "x"}, headers=hdr).status_code == 404


def test_active_list_is_ward_scoped(bundle):
    other = start_bundle(_mk_patient(DEPT_B), trigger_reason="test")
    hdr, _ = _mk_user("nurse", DEPT_A)
    resp = client.get("/api/bundles/active", headers=hdr)
    assert resp.status_code == 200, resp.text
    ids = {b["id"] for b in resp.json()["bundles"]}
    assert bundle["id"] in ids
    assert other["id"] not in ids, "another ward's active bundles are not this nurse's"
