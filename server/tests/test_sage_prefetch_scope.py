"""A speculative system warm cannot authorize the caller's patient chart."""
import json
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from auth import create_token
from database import db_session
from routes import sage_routes


@pytest.fixture
def world(isolated_module_database, monkeypatch):
    import config
    from services import nervous_system_triggers

    monkeypatch.setattr(config, "MODELS_ENABLED", False)
    suffix = uuid.uuid4().hex[:10]
    own_dept, foreign_dept = f"Prefetch Own {suffix}", f"Prefetch Foreign {suffix}"
    users, patients, calls = {}, {}, []
    with db_session() as db:
        for label, role, dept in (
            ("doctor", "doctor", own_dept), ("admin", "admin", None),
            ("pharmacist", "pharmacist", None),
        ):
            uid = f"PF-{label}-{suffix}"
            db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                       "VALUES (?, ?, ?, ?, 'test-only')", (uid, label, role, dept))
            users[label] = {"id": uid, "role": role, "department": dept}
        for label, dept in (("own", own_dept), ("foreign", foreign_dept)):
            pid = f"PF-P-{label}-{suffix}"
            db.execute("INSERT INTO patients (id, name, mrn, ward, status) "
                       "VALUES (?, ?, ?, ?, 'admitted')", (pid, f"Private {label}", pid, dept))
            db.execute("INSERT INTO admissions (patient_id, department, status, admitted_by) "
                       "VALUES (?, ?, 'active', ?)", (pid, dept, users["doctor"]["id"]))
            patients[label] = pid
        # Authorization also precedes a hit in an already warmed system cache.
        db.execute("INSERT INTO sage_decision_cache "
                   "(patient_id, mews_score, compiled_context, preloaded_at, expires_at) "
                   "VALUES (?, 0, ?, datetime('now'), datetime('now', '+5 minutes'))",
                   (patients["foreign"], json.dumps({"channel_a_facts": "PRIVATE FOREIGN FACTS"})))

    async def warm(patient_id, **kwargs):
        calls.append((patient_id, kwargs))
        return True

    monkeypatch.setattr(nervous_system_triggers, "warm_patient_context", warm)
    app = FastAPI()
    app.include_router(sage_routes.router)
    client = TestClient(app, raise_server_exceptions=False)
    yield {"users": users, "patients": patients, "calls": calls,
           "client": client, "foreign_department": foreign_dept}
    client.close()


def post(world, user="doctor", payload=None):
    actor = world["users"][user]
    return world["client"].post(
        "/api/sage/prefetch", json=payload,
        headers={"Authorization": f"Bearer {create_token(actor['id'], actor['role'])}"},
    )


def test_other_department_is_refused_before_cached_facts_or_models(world):
    response = post(world, payload={
        "patient_id": world["patients"]["foreign"],
        "department": world["foreign_department"], "role": "admin",
    })
    missing = post(world, payload={"patient_id": "PF-NO-SUCH-PATIENT"})
    assert response.status_code == missing.status_code == 404
    assert response.json() == missing.json() == {"detail": "Patient not found"}
    assert "PRIVATE FOREIGN FACTS" not in response.text
    assert world["calls"] == []
    with db_session() as db:
        stored = db.execute("SELECT compiled_context FROM sage_decision_cache WHERE patient_id = ?",
                            (world["patients"]["foreign"],)).fetchone()[0]
    assert json.loads(stored) == {"channel_a_facts": "PRIVATE FOREIGN FACTS"}


def test_sage_query_permission_alone_cannot_warm_a_patient(world):
    response = post(world, "pharmacist", {"patient_id": world["patients"]["own"]})
    assert response.status_code == 403
    assert world["calls"] == []


@pytest.mark.parametrize("user,patient", [("doctor", "own"), ("admin", "foreign")])
def test_authorized_chart_open_warms_and_records_phi_read(world, user, patient):
    pid = world["patients"][patient]
    response = post(world, user, {"patient_id": pid, "hint": "Synthetic recorded concern"})
    assert response.status_code == 200
    assert response.json() == {"status": "warmed", "patient_id": pid}
    assert world["calls"] == [(pid, {"query_hint": "Synthetic recorded concern",
                                    "ttl_minutes": 5, "reason": "ui_patient_opened"})]
    with db_session() as db:
        rows = db.execute("SELECT outcome, actor_user_id, patient_id FROM audit_logs "
                          "WHERE action_type = 'sage_context_prefetched' AND patient_id = ?",
                          (pid,)).fetchall()
    assert [(r["outcome"], r["actor_user_id"], r["patient_id"]) for r in rows] == [
        ("success", world["users"][user]["id"], pid),
    ]


def test_unscoped_admin_cannot_warm_a_nonexistent_patient(world):
    response = post(world, "admin", {"patient_id": "PF-NO-SUCH-PATIENT"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Patient not found"}
    assert world["calls"] == []


@pytest.mark.parametrize("payload", [{}, {"patient_id": None}, {"patient_id": ""},
                                     {"patient_id": 4}, {"patient_id": []}, []])
def test_absent_or_malformed_ids_never_reach_the_warm_path(world, payload):
    response = post(world, payload=payload)
    assert response.status_code == 422
    assert world["calls"] == []


def test_patient_read_audit_failure_prevents_warming(world, monkeypatch):
    def unavailable(**kwargs):
        raise RuntimeError("synthetic audit unavailable")

    monkeypatch.setattr("permissions.log_phi_read", unavailable)
    response = post(world, payload={"patient_id": world["patients"]["own"]})
    assert response.status_code == 500
    assert world["calls"] == []


def test_reused_token_obeys_current_user_department(world):
    uid = world["users"]["doctor"]["id"]
    with db_session() as db:
        db.execute("UPDATE users SET department = ? WHERE id = ?",
                   (world["foreign_department"], uid))
    response = post(world, payload={"patient_id": world["patients"]["own"]})
    assert response.status_code == 404
    assert world["calls"] == []
