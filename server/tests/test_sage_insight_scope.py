"""Recipient assignment cannot authorize a proactive insight's patient chart."""
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from routes.sage_proactive_routes import _create_action_card
from services import facility_service
from services.sage_proactive_service import evaluate_all_admitted, get_active_insights, transition_insight

client = TestClient(app)


@pytest.fixture
def world(isolated_module_database):
    suffix = uuid.uuid4().hex[:10]
    users = {}
    patients = {}
    own_dept, foreign_dept = f"Insight Own {suffix}", f"Insight Foreign {suffix}"
    with db_session() as db:
        for name, role, dept in (("doctor", "doctor", own_dept),
                                 ("other", "doctor", foreign_dept),
                                 ("admin", "admin", None),
                                 ("unscoped", "doctor", None)):
            uid = f"INS-{name}-{suffix}"
            db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                       "VALUES (?, ?, ?, ?, 'test-only')", (uid, name, role, dept))
            users[name] = {"id": uid, "name": name, "role": role, "department": dept}
        for name, dept in (("own", own_dept), ("foreign", foreign_dept)):
            pid = f"INS-P-{name}-{suffix}"
            db.execute("INSERT INTO patients (id, name, mrn, status, ward) "
                       "VALUES (?, ?, ?, 'admitted', ?)",
                       (pid, f"Private {name} chart {suffix}", pid, dept))
            db.execute("INSERT INTO admissions (patient_id, department, status, admitted_by) "
                       "VALUES (?, ?, 'active', ?)", (pid, dept, users["doctor"]["id"]))
            db.execute("INSERT INTO channels (id, name, type, patient_id) "
                       "VALUES (?, ?, 'case_room', ?)", (f"case-{pid}", name, pid))
            patients[name] = pid
    data = {**users, "patients": patients, "department": own_dept, "insights": []}
    yield data
    with db_session() as db:
        for pid in patients.values():
            db.execute("DELETE FROM sage_action_events WHERE action_id IN "
                       "(SELECT id FROM sage_actions WHERE patient_id = ?)", (pid,))
            db.execute("DELETE FROM sage_actions WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM insight_feedback WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM sage_insights WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM channels WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM medications WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))


def _insight(world, patient="foreign", recipient=None, state="active", severity="info"):
    iid = str(uuid.uuid4())
    target = world[recipient]["id"] if recipient else None
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_insights
               (id, patient_id, insight_type, severity, title, detail,
                suggested_action_type, suggested_action_target, target_user_id,
                dedup_key, state, created_at, updated_at)
               VALUES (?, ?, 'stewardship', ?, 'Review investigation', 'Private chart detail',
                       'order_lab', 'CBC', ?, ?, ?, datetime('now'), datetime('now'))""",
            (iid, world["patients"][patient], severity, target, iid, state),
        )
    world["insights"].append(iid)
    return iid


def _headers(user):
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


def _write(iid, operation, user):
    return client.post(f"/api/sage/insights/{iid}/{operation}",
                       json={"state": "dismissed"} if operation == "transition" else {},
                       headers=_headers(user))


def _state_and_writes(iid):
    with db_session() as db:
        row = db.execute("SELECT state, patient_id FROM sage_insights WHERE id = ?", (iid,)).fetchone()
        actions = db.execute("SELECT COUNT(*) FROM sage_actions WHERE patient_id = ?",
                             (row["patient_id"],)).fetchone()[0]
        feedback = db.execute("SELECT COUNT(*) FROM insight_feedback WHERE insight_id = ?",
                              (iid,)).fetchone()[0]
        return row["state"], actions, feedback


@pytest.mark.parametrize("recipient", [None, "doctor"])
def test_feed_scopes_null_and_explicit_recipients_before_disclosing_names(world, recipient):
    foreign = _insight(world, recipient=recipient)
    own = _insight(world, patient="own", recipient=recipient)
    response = client.get("/api/sage/insights", headers=_headers(world["doctor"]))
    assert response.status_code == 200
    rows = response.json()["insights"]
    assert own in {r["id"] for r in rows}
    assert foreign not in {r["id"] for r in rows}
    assert world["patients"]["foreign"] not in json.dumps(rows)
    assert "Private foreign chart" not in json.dumps(rows)


def test_foreign_critical_rows_do_not_consume_the_scoped_feed_limit(world):
    for _ in range(55):
        _insight(world, severity="critical")
    own = _insight(world, patient="own")
    rows = get_active_insights(world["doctor"]["id"], "doctor")
    assert [row["id"] for row in rows] == [own]


@pytest.mark.parametrize("recipient", [None, "doctor", "other"])
@pytest.mark.parametrize("state", ["active", "dismissed"])
@pytest.mark.parametrize("operation", ["transition", "act"])
def test_foreign_insights_are_denied_before_any_state_or_recipient_branch(
    world, recipient, state, operation,
):
    iid = _insight(world, recipient=recipient, state=state)
    before = _state_and_writes(iid)
    response = _write(iid, operation, world["doctor"])
    assert response.status_code == 404, response.text
    assert _state_and_writes(iid) == before


@pytest.mark.parametrize("recipient", [None, "doctor"])
@pytest.mark.parametrize("operation", ["transition", "act"])
def test_authorized_patient_insights_keep_the_existing_write_workflow(world, recipient, operation):
    iid = _insight(world, patient="own", recipient=recipient)
    response = _write(iid, operation, world["doctor"])
    assert response.status_code == 200, response.text
    state, actions, feedback = _state_and_writes(iid)
    assert state == ("acted" if operation == "act" else "dismissed")
    assert actions == (1 if operation == "act" else 0)
    assert feedback == 1


@pytest.mark.parametrize("operation", ["transition", "act"])
def test_patient_access_does_not_override_another_clinicians_assignment(world, operation):
    iid = _insight(world, patient="own", recipient="other")
    before = _state_and_writes(iid)
    assert _write(iid, operation, world["doctor"]).status_code == 403
    assert _state_and_writes(iid) == before


@pytest.mark.parametrize("caller", ["admin", "unscoped"])
def test_existing_hospital_wide_accounts_retain_their_patient_scope(world, caller):
    user = world[caller]
    iid = _insight(world)
    assert iid in {r["id"] for r in get_active_insights(user["id"], user["role"])}
    assert _write(iid, "transition", user).status_code == 200
    iid = _insight(world)
    assert _write(iid, "act", user).status_code == 200


def test_service_scope_uses_the_stored_identity_and_refuses_missing_users(world):
    iid = _insight(world, recipient="doctor")
    # A caller cannot turn this doctor's stored department into admin scope.
    assert get_active_insights(world["doctor"]["id"], "admin") == []
    assert transition_insight(iid, "dismissed", world["doctor"]["id"], "admin") == {}
    assert get_active_insights("missing-clinician", "admin") == []
    assert transition_insight(iid, "dismissed", "missing-clinician", "admin") == {}
    assert _state_and_writes(iid) == ("active", 0, 0)


@pytest.mark.parametrize("recipient", [None, "doctor"])
def test_same_department_spelling_does_not_cross_the_facility_boundary(world, recipient):
    code = f"INS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        facility = db.execute("INSERT INTO facilities (uuid, code, name, is_active) "
                              "VALUES (?, ?, 'Insight Test Facility', 1)",
                              (str(uuid.uuid4()), code)).lastrowid
        db.execute("INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
                   (world["department"],))
        db.execute("UPDATE admissions SET department = ?, facility_id = ? WHERE patient_id = ?",
                   (world["department"], facility, world["patients"]["foreign"]))
    facility_service.invalidate_cache()
    try:
        iid = _insight(world, recipient=recipient)
        assert get_active_insights(world["doctor"]["id"], "doctor") == []
        assert transition_insight(iid, "dismissed", world["doctor"]["id"], "doctor") == {}
        with pytest.raises(HTTPException) as denied:
            _create_action_card(iid, world["doctor"])
        assert denied.value.status_code == 404
        assert _state_and_writes(iid) == ("active", 0, 0)
        # The default unfinished-multisite fence stays enabled on HTTP routes.
        assert client.get("/api/sage/insights", headers=_headers(world["doctor"])).status_code == 503
    finally:
        with db_session() as db:
            db.execute("UPDATE admissions SET facility_id = 1 WHERE patient_id = ?",
                       (world["patients"]["foreign"],))
            db.execute("DELETE FROM departments WHERE name = ?", (world["department"],))
            db.execute("DELETE FROM facilities WHERE id = ?", (facility,))
        facility_service.invalidate_cache()


def _seed_review_due(world):
    with db_session() as db:
        for pid in world["patients"].values():
            db.execute("INSERT INTO medications "
                       "(patient_id, drug_name, dosage, route, frequency, status, started_at) "
                       "VALUES (?, 'Ceftriaxone', '2g', 'IV', 'OD', 'active', datetime('now', '-96 hours'))",
                       (pid,))


@pytest.mark.parametrize("caller", ["doctor", "admin", "unscoped"])
def test_manual_all_patient_sweep_uses_only_the_callers_chart_scope(world, caller):
    _seed_review_due(world)
    response = client.post("/api/sage/insights/evaluate", json={}, headers=_headers(world[caller]))
    assert response.status_code == 200, response.text
    with db_session() as db:
        patients = {r[0] for r in db.execute("SELECT patient_id FROM sage_insights WHERE state = 'active'")}
    expected = {world["patients"]["own"]} if caller == "doctor" else set(world["patients"].values())
    assert patients == expected
    assert response.json()["created"] > 0


@pytest.mark.parametrize("patient", ["own", "foreign"])
def test_manual_patient_sweep_authorizes_before_running_any_rule(world, patient):
    _seed_review_due(world)
    response = client.post("/api/sage/insights/evaluate",
                           json={"patient_id": world["patients"][patient]},
                           headers=_headers(world["doctor"]))
    assert response.status_code == (200 if patient == "own" else 404), response.text
    with db_session() as db:
        patients = {r[0] for r in db.execute("SELECT patient_id FROM sage_insights WHERE state = 'active'")}
    assert patients == ({world["patients"]["own"]} if patient == "own" else set())


@pytest.mark.parametrize("system", [False, True])
def test_only_the_scheduled_sweep_resolves_discharged_insights_hospital_wide(world, system):
    own, foreign = _insight(world, patient="own"), _insight(world)
    with db_session() as db:
        for pid in world["patients"].values():
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
    if system:
        evaluate_all_admitted()
    else:
        response = client.post("/api/sage/insights/evaluate", json={}, headers=_headers(world["doctor"]))
        assert response.status_code == 200
    assert _state_and_writes(own)[0] == "auto_resolved"
    assert _state_and_writes(foreign)[0] == ("auto_resolved" if system else "active")


@pytest.mark.parametrize("raw", ["broken json", "[]", "null", "42", '"text"'])
@pytest.mark.parametrize("operation", ["evaluate", "transition"])
def test_invalid_json_never_becomes_a_global_sweep_or_transition(world, raw, operation):
    iid = _insight(world, patient="own")
    before = _state_and_writes(iid)
    path = "/api/sage/insights/evaluate" if operation == "evaluate" else f"/api/sage/insights/{iid}/transition"
    response = client.post(path, content=raw, headers={**_headers(world["doctor"]), "Content-Type": "application/json"})
    assert response.status_code == 400, response.text
    assert _state_and_writes(iid) == before


@pytest.mark.parametrize("patient_id", [None, 0, False, "", "  ", [], {}])
def test_invalid_patient_identifier_never_requests_the_all_patient_sweep(world, patient_id):
    iid = _insight(world, patient="own")
    before = _state_and_writes(iid)
    response = client.post("/api/sage/insights/evaluate", json={"patient_id": patient_id},
                           headers=_headers(world["doctor"]))
    assert response.status_code == 400
    assert _state_and_writes(iid) == before


@pytest.mark.parametrize("state", [None, [], {}])
def test_invalid_transition_state_returns_a_client_error_without_writes(world, state):
    iid = _insight(world, patient="own")
    before = _state_and_writes(iid)
    response = client.post(f"/api/sage/insights/{iid}/transition", json={"state": state},
                           headers=_headers(world["doctor"]))
    assert response.status_code == 400
    assert _state_and_writes(iid) == before
