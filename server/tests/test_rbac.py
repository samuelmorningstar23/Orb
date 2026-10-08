"""Orb Hospital OS — RBAC Permission Tests

Verifies that the centralized permission model enforces access control
correctly across all protected routes.
"""
import pytest
import json
from unittest import mock

# Explicitly import routes.ingestion_routes so it is loaded before mock.patch checks attributes
import routes.ingestion_routes

# Mock config before importing main
with mock.patch("config.INGESTION_MODE", "hybrid"), \
     mock.patch("routes.ingestion_routes.INGESTION_MODE", "hybrid"), \
     mock.patch("main.INGESTION_MODE", "hybrid"):
    from fastapi.testclient import TestClient
    from main import app
    from database import db_session

client = TestClient(app)


# ════════════════════════════════════════════
#  Fixtures
# ════════════════════════════════════════════

def _create_user(user_id: str, name: str, role: str, password: str = "testpass123"):
    """Insert a test user and return the user_id."""
    from auth import hash_password
    pwd = hash_password(password)
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (user_id, name, role, pwd),
        )
    return user_id


def _login(user_id: str, password: str = "testpass123") -> str:
    """Login and return the JWT token."""
    resp = client.post("/api/auth/login", json={"employee_id": user_id, "password": password})
    assert resp.status_code == 200, f"Login failed for {user_id}: {resp.text}"
    return resp.json()["token"]


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def setup_users():
    """Ensure test users exist for each role."""
    _create_user("RBAC-DOC", "Dr. RBAC Test", "doctor")
    _create_user("RBAC-NRS", "Nurse RBAC Test", "nurse")
    _create_user("RBAC-ADM", "Admin RBAC Test", "admin")
    _create_user("RBAC-PAT", "Patient RBAC Test", "patient")

    # Ensure a test patient record exists for patient self-access
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO patients (id, user_id, name, mrn) VALUES (?, ?, ?, ?)",
            ("PAT-RBAC-1", "RBAC-PAT", "Patient RBAC Test", "MRN-RBAC-1"),
        )
    yield
    # Don't delete users — they may be needed by other tests in the same run


# ════════════════════════════════════════════
#  Test: Admin can access governance routes
# ════════════════════════════════════════════

def test_admin_can_read_audit_logs():
    """Admin (with audit.read) can access /api/audit/logs."""
    token = _login("RBAC-ADM")
    resp = client.get("/api/audit/logs", headers=_auth_header(token))
    assert resp.status_code == 200
    data = resp.json()
    assert "logs" in data
    assert "total" in data


def test_admin_can_read_overview():
    """Admin (with admin.overview) can access /api/admin/overview."""
    token = _login("RBAC-ADM")
    resp = client.get("/api/admin/overview", headers=_auth_header(token))
    assert resp.status_code == 200
    data = resp.json()
    assert "ingestion_mode" in data


def test_admin_can_read_ingest_status():
    """Admin (with ingest.monitor) can access /api/ingest/status."""
    token = _login("RBAC-ADM")
    resp = client.get("/api/ingest/status", headers=_auth_header(token))
    assert resp.status_code == 200
    data = resp.json()
    assert "events" in data


# ════════════════════════════════════════════
#  Test: Doctor/Nurse CANNOT access admin routes
# ════════════════════════════════════════════

def test_doctor_cannot_read_audit_logs():
    """Doctor (no audit.read) gets 403 on /api/audit/logs."""
    token = _login("RBAC-DOC")
    resp = client.get("/api/audit/logs", headers=_auth_header(token))
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "permission_denied"
    assert "audit.read" in detail["missing"]


def test_nurse_cannot_read_admin_overview():
    """Nurse (no admin.overview) gets 403 on /api/admin/overview."""
    token = _login("RBAC-NRS")
    resp = client.get("/api/admin/overview", headers=_auth_header(token))
    assert resp.status_code == 403


def test_doctor_cannot_read_ingest_status():
    """Doctor (no ingest.monitor) gets 403 on /api/ingest/status."""
    token = _login("RBAC-DOC")
    resp = client.get("/api/ingest/status", headers=_auth_header(token))
    assert resp.status_code == 403


# ════════════════════════════════════════════
#  Test: Patient role restrictions
# ════════════════════════════════════════════

def test_patient_cannot_list_all_patients():
    """Patient (no patient.read_all) gets 403 on /api/vigil/patients."""
    token = _login("RBAC-PAT")
    resp = client.get("/api/vigil/patients", headers=_auth_header(token))
    assert resp.status_code == 403


def test_patient_cannot_access_sage():
    """Patient (no sage.query) gets 403 on /api/sage/query."""
    token = _login("RBAC-PAT")
    resp = client.post(
        "/api/sage/query",
        headers=_auth_header(token),
        json={"query": "What is sepsis?"},
    )
    assert resp.status_code == 403


def test_patient_cannot_access_relay():
    """Patient (no relay.read) gets 403 on /api/relay/channels."""
    token = _login("RBAC-PAT")
    resp = client.get("/api/relay/channels", headers=_auth_header(token))
    assert resp.status_code == 403


def test_patient_cannot_access_forecast():
    """Patient (no forecast.read) gets 403 on /api/forecast/hospital."""
    token = _login("RBAC-PAT")
    resp = client.get("/api/forecast/hospital", headers=_auth_header(token))
    assert resp.status_code == 403


# ════════════════════════════════════════════
#  Test: Clinician workflows still work
# ════════════════════════════════════════════

def test_doctor_can_access_patients():
    """Doctor (with patient.read_all) can access /api/vigil/patients."""
    token = _login("RBAC-DOC")
    resp = client.get("/api/vigil/patients", headers=_auth_header(token))
    assert resp.status_code == 200


def test_nurse_can_access_patients():
    """Nurse (with patient.read_all) can access /api/vigil/patients."""
    token = _login("RBAC-NRS")
    resp = client.get("/api/vigil/patients", headers=_auth_header(token))
    assert resp.status_code == 200


def test_doctor_can_access_forecast():
    """Doctor (with forecast.read) can access /api/forecast/hospital."""
    token = _login("RBAC-DOC")
    resp = client.get("/api/forecast/hospital", headers=_auth_header(token))
    assert resp.status_code == 200


def test_nurse_cannot_discharge():
    """Nurse (no discharge.generate) gets 403 on discharge route."""
    token = _login("RBAC-NRS")
    resp = client.post(
        "/api/vigil/patients/PAT-RBAC-1/discharge",
        headers=_auth_header(token),
    )
    assert resp.status_code == 403


def test_admin_cannot_prescribe_medication():
    """Admin (no medication.write) gets 403 on medication route."""
    token = _login("RBAC-ADM")
    resp = client.post(
        "/api/vigil/patients/PAT-RBAC-1/medications",
        headers=_auth_header(token),
        json={"drug_name": "Test Drug", "dosage": "10mg", "frequency": "BD"},
    )
    assert resp.status_code == 403


# ════════════════════════════════════════════
#  Test: Permission denial generates audit entry
# ════════════════════════════════════════════

def test_permission_denial_generates_audit_log():
    """A 403 denial should create an audit log with action_type='permission_denied'."""
    # (audit_logs is append-only; the assertion below takes the most-recent
    #  permission_denied row, so no pre-clear is needed.)
    # Trigger a denial
    token = _login("RBAC-DOC")
    resp = client.get("/api/audit/logs", headers=_auth_header(token))
    assert resp.status_code == 403

    # Verify audit entry was created
    with db_session() as db:
        log = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'permission_denied' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

    assert log is not None
    assert log["actor_user_id"] == "RBAC-DOC"
    assert log["actor_role"] == "doctor"
    assert log["outcome"] == "denied"
    assert log["event_category"] == "authorization"

    # Verify metadata contains permission details
    metadata = json.loads(log["metadata_json"])
    assert "audit.read" in metadata["missing_permissions"]
    assert "/api/audit/logs" in log["request_path"]


# ════════════════════════════════════════════
#  Test: Bridge patient self-access
# ════════════════════════════════════════════

def test_patient_can_access_bridge():
    """Patient (with bridge.read_own) can access /api/bridge/status."""
    token = _login("RBAC-PAT")
    resp = client.get("/api/bridge/status", headers=_auth_header(token))
    # 200 or may error if patient record lookup fails, but should NOT be 403
    assert resp.status_code != 403


def test_doctor_cannot_access_bridge():
    """Doctor (no bridge.read_own) gets 403 on /api/bridge/status."""
    token = _login("RBAC-DOC")
    resp = client.get("/api/bridge/status", headers=_auth_header(token))
    assert resp.status_code == 403


# ════════════════════════════════════════════
#  Test: Unauthenticated access blocked
# ════════════════════════════════════════════

def test_unauthenticated_gets_401_on_protected_route():
    """No token → 401 on any protected route."""
    resp = client.get("/api/vigil/patients")
    assert resp.status_code in (401, 403)


def test_unauthenticated_gets_401_on_ingest_status():
    """No token → 401 on /api/ingest/status (was previously fully open)."""
    resp = client.get("/api/ingest/status")
    assert resp.status_code in (401, 403)


# ════════════════════════════════════════════
#  Medication orders (W5-E): the verify permission and the prescriber's steps
# ════════════════════════════════════════════

def test_medication_verify_is_the_pharmacists_alone():
    """Ruling MO-R4 (a): medication.verify is held by the pharmacist and by no
    other role, the administrator included (src/lib/roles.test.ts mirrors it)."""
    from permissions import MEDICATION_VERIFY, ROLE_PERMISSIONS
    assert MEDICATION_VERIFY == "medication.verify"
    holders = {role for role, perms in ROLE_PERMISSIONS.items() if MEDICATION_VERIFY in perms}
    assert holders == {"pharmacist"}


def test_a_nurse_cannot_sign_a_medication_order(monkeypatch):
    """With both medication-order switches on, signing needs medication.prescribe:
    the nurse is refused before anything is read, and the denial is audited."""
    import uuid
    import config
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    token = _login("RBAC-NRS")
    resp = client.post(f"/api/medorders/orders/{uuid.uuid4()}/sign",
                       headers=_auth_header(token), json={"expected_version": 1})
    assert resp.status_code == 403
    assert "medication.prescribe" in resp.json()["detail"]["missing"]
    with db_session() as db:
        log = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'permission_denied' "
            "AND actor_user_id = 'RBAC-NRS' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert log is not None and "/api/medorders/orders/" in log["request_path"]


def test_medication_order_routes_are_404_with_the_store_off():
    """Every /api/medorders route answers 404 feature_disabled while
    ORB_MED_ORDER_STORE is off, whoever asks."""
    token = _login("RBAC-DOC")
    resp = client.get("/api/medorders/queue/to-sign", headers=_auth_header(token))
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "feature_disabled"
