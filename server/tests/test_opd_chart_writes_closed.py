"""With OPD on, an outpatient's chart stays closed (opd-7, W3-A, 4 October 2026).

The OPD scope arm opens ONE thing to a clinic that saw a patient recently: the
outpatient record view (GET /api/opd/patients/{patient_id}/record). It must not
open the inpatient chart. require_patient_access is one guard for reads and
writes, and several chart writes have no admitted-status check of their own
(medication add, nurse obs), so widening the shared scope would have handed
them to any clinician whose clinic registered a visit.

Every case below uses a patient the arm DOES open to the caller's clinic (the
positive control proves it), and asserts the chart route still refuses:
404 where the caller holds the route's permission (scope decides), 403 where
the role lacks it (the permission decides first). Never a 2xx, and nothing is
written.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from permissions import has_permission
from services.hospital_time import hospital_today

client = TestClient(app)

DEPT = "OPD Closed Cardiology"


def _mk_user(role, department):
    uid = f"OCW{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, f"Opd Closed {role} {uid[-4:]}", role, department),
        )
    return uid


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def world():
    doctor = _mk_user("doctor", DEPT)
    nurse = _mk_user("nurse", DEPT)
    pid = f"OCWP{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        # Never admitted: the OPD walk-in. Discharged-from-elsewhere is covered
        # by the same predicate (no ACTIVE admission) in test_opd_scope.py.
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status) "
            "VALUES (?, 'Opdclosed Walkin', ?, 52, 'M', 'registered')",
            (pid, f"MRN-{uuid.uuid4().hex[:8].upper()}"))
        db.execute(
            "INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
            "status, registered_by, registered_at) VALUES (?, ?, ?, ?, 1, 'waiting', ?, ?)",
            (f"OPD-C{uuid.uuid4().hex[:7].upper()}", pid, DEPT,
             hospital_today().isoformat(), doctor, now))
    return {"doctor": doctor, "nurse": nurse, "pid": pid}


@pytest.fixture(autouse=True)
def opd_on(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(config, "OPD_SCOPE_DAYS", 7)
    assert config.feature_enabled("opd")
    yield


OBS = {"heart_rate": 88, "systolic_bp": 124, "diastolic_bp": 80, "respiratory_rate": 16,
       "temperature": 37.0, "spo2": 97, "consciousness": "alert", "on_oxygen": False}

#: (label, method, path template, json body, the permission the route checks first)
CHART_ROUTES = [
    ("medication add", "POST", "/api/vigil/patients/{pid}/medications",
     {"drug_name": "Amoxicillin", "dose": "500 mg", "route": "oral", "frequency": "TDS"},
     "medication.write"),
    ("nurse observations", "POST", "/api/nurse/patients/{pid}/observations", OBS,
     "vitals.write"),
    ("news2 preview", "POST", "/api/nurse/patients/{pid}/news2-preview", OBS,
     "vitals.read"),
    ("allergy write", "POST", "/api/patients/{pid}/allergies",
     {"substance": "penicillin", "severity": "severe"}, "patient.write"),
    ("problem write", "POST", "/api/patients/{pid}/problems",
     {"problem": "Opdclosed hypertension"}, "patient.write"),
    ("care bundle start", "POST", "/api/bundles/patient/{pid}/start",
     {"bundle_type": "sepsis6", "reason": "probe"}, "vitals.write"),
    ("discharge", "POST", "/api/vigil/patients/{pid}/discharge", None,
     "discharge.generate"),
    ("vigil chart", "GET", "/api/vigil/patients/{pid}", None, "patient.read_all"),
    ("fhir patient export", "GET", "/api/fhir/Patient/{pid}", None, "patient.read_all"),
    ("fhir everything export", "GET", "/api/fhir/patient/{pid}/$everything", None,
     "patient.read_all"),
]


def _call(method, path, body, headers):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.request(method, path, json=body, headers=headers)


def _row_counts(pid):
    with db_session() as db:
        def n(table):
            return db.execute(f"SELECT COUNT(*) FROM {table} WHERE patient_id = ?",
                              (pid,)).fetchone()[0]
        return {t: n(t) for t in ("medications", "vitals", "patient_allergies",
                                   "problem_list", "care_bundles", "risk_scores")}


def test_the_arm_really_is_open_to_this_clinic(world):
    """Positive control: without it every 404 below would prove nothing."""
    resp = client.get(f"/api/opd/patients/{world['pid']}/record",
                      headers=_auth(world["doctor"], "doctor"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["access"]["arm"] == "opd_visit"


@pytest.mark.parametrize("role", ["doctor", "nurse"])
@pytest.mark.parametrize("label,method,path,body,perm", CHART_ROUTES,
                         ids=[c[0] for c in CHART_ROUTES])
def test_chart_routes_stay_closed_to_an_outpatient_only_patient(world, role, label, method,
                                                                path, body, perm):
    before = _row_counts(world["pid"])
    resp = _call(method, path.format(pid=world["pid"]), body,
                 _auth(world[role], role))
    expected = 404 if has_permission(role, perm) else 403
    assert resp.status_code == expected, (
        f"{role} {label}: {resp.status_code} {resp.text[:300]}")
    assert _row_counts(world["pid"]) == before, f"{role} {label} wrote to the chart"


def test_every_chart_case_is_exercised_by_a_role_that_holds_its_permission(world):
    """Non-vacuity: each route is refused on SCOPE (404) for at least one role,
    not only on permission."""
    for label, _m, _p, _b, perm in CHART_ROUTES:
        assert has_permission("doctor", perm) or has_permission("nurse", perm), label
