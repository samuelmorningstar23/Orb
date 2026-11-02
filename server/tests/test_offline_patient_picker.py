"""Patient identity search preserves module, department and disclosure boundaries."""
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app


def headers(uid, role):
    return {"Authorization": "Bearer " + create_token(uid, role)}


@pytest.fixture
def search_context(monkeypatch):
    for constant in config._OPT_IN_MODULE_CONSTANTS.values():
        monkeypatch.setattr(config, constant, False)
    monkeypatch.setattr(config, "RADIOLOGY_ENABLED", True)
    prefix = "Picker" + uuid.uuid4().hex[:8]
    dept = "Picker department " + prefix
    uid = prefix + "-doctor"
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,department,password_hash) VALUES (?,?,'doctor',?,'disabled')", (uid, prefix, dept))

    def patient(suffix, department=dept, admitted=True):
        pid = prefix + "-" + suffix
        with db_session() as db:
            db.execute("INSERT INTO patients(id,name,mrn,status) VALUES (?,?,?,?)", (pid, prefix + suffix, pid, "admitted" if admitted else "registered"))
            if admitted:
                db.execute("INSERT INTO admissions(id,patient_id,department,status,admitted_by,created_at) VALUES (?,?,?,'active',?,'2026-10-01T00:00:00Z')", ("adm-" + pid, pid, department, uid))
        return pid
    return TestClient(app), prefix, uid, dept, patient


def test_search_is_scoped_before_limit_and_escapes_like_metacharacters(search_context):
    client, prefix, uid, dept, patient = search_context
    expected = patient("%own")
    patient("Xown")
    for index in range(25):
        patient(f"%foreign{index}", department="Other picker department")
    response = client.get("/api/local-workflows/patients", params={"q": prefix + "%"}, headers=headers(uid, "doctor"))
    assert response.status_code == 200, response.text
    assert [p["id"] for p in response.json()["patients"]] == [expected]


def test_enabled_unrelated_module_does_not_unlock_identity_search(search_context, monkeypatch):
    client, prefix, uid, _, patient = search_context
    patient("own")
    monkeypatch.setattr(config, "RADIOLOGY_ENABLED", False)
    monkeypatch.setattr(config, "FEEDBACK_ENABLED", True)
    response = client.get("/api/local-workflows/patients", params={"q": prefix}, headers=headers(uid, "doctor"))
    assert response.status_code == 404
    assert client.get("/api/local-workflows/patients", params={"q": prefix}).status_code == 401


def test_registered_intake_needs_registration_authority(search_context):
    client, prefix, uid, _, patient = search_context
    expected = patient("new", admitted=False)
    ordinary = client.get("/api/local-workflows/patients", params={"q": prefix}, headers=headers(uid, "doctor"))
    assert ordinary.status_code == 200 and ordinary.json()["patients"] == []
    intake = client.get("/api/local-workflows/patients", params={"q": prefix, "include_registered": True}, headers=headers(uid, "doctor"))
    assert intake.status_code == 200 and [p["id"] for p in intake.json()["patients"]] == [expected]
    with db_session() as db:
        db.execute("UPDATE users SET role='nurse' WHERE id=?", (uid,))
    refused = client.get("/api/local-workflows/patients", params={"q": prefix, "include_registered": True}, headers=headers(uid, "doctor"))
    assert refused.status_code == 403


def test_search_audit_failure_refuses_disclosure(search_context, monkeypatch):
    _, prefix, uid, _, patient = search_context
    patient("own")
    from routes import offline_workflow_routes
    def fail(**kwargs):
        raise RuntimeError("Audit unavailable")
    monkeypatch.setattr(offline_workflow_routes, "log_phi_read", fail)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/local-workflows/patients", params={"q": prefix}, headers=headers(uid, "doctor"))
    assert response.status_code == 500
    assert prefix not in response.text


def test_short_or_unbounded_query_is_refused(search_context):
    client, _, uid, _, _ = search_context
    for query in ("ab", "x" * 121, "   "):
        assert client.get("/api/local-workflows/patients", params={"q": query}, headers=headers(uid, "doctor")).status_code == 422
