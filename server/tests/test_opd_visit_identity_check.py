"""The returning-patient identity check on a visit (opd-6a, W5-A, ruling OPD-S1).

With the registration feature on, the desk finds a returning patient in the
hospital-wide index, so attaching a visit to the wrong record is one tap away.
Every visit registered through POST /api/frontdesk/opd/visits then carries
the identifiers the patient STATES; the server compares them with the record
(registration_service.check_identity) and answers match or no match.

What this file defends:

  1. FLAG OFF, NOTHING CHANGED: no check is asked for, identity_check is
     ignored, nothing is stored on the visit and the audit row is the old one.
  2. NO CHECK, NO VISIT: a visit with no identity_check is 409
     identity_check_required, nothing is saved, and the refusal is on the
     fail-closed patient_identity chain.
  3. A WRONG DATE OF BIRTH IS REFUSED AND AUDITED (identity_check_failed plus
     opd_visit_identity_refused), and no answer ever carries the stored date
     of birth or phone.
  4. A MATCH IS STORED on opd_visits.identity_check_json and in the visit's
     audit row: the result, what was compared, when and by whom, never a
     stated or stored value. OPD mode writes it in the visit's transaction.
  5. Too few identifiers is 422; the sixth failure in an hour is 429, even
     with the right answers; another ward's inpatient stays 404; an unknown
     field in the check is 422.
  6. A patient registered a moment ago gets a visit with what the desk just
     typed and the UHID the registration returned.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app

client = TestClient(app)

DEPT = "Identity Check Medicine"
OTHER_DEPT = "Identity Check Surgery"

STORED_DOB = "1979-04-23"
STORED_PHONE = "9812345671"


def _mk_user(role, department=None, name=None):
    uid = f"IDC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"Identity {role} {uid[-4:]}", role, department))
    return uid


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _patient(*, dob=STORED_DOB, phone=STORED_PHONE, status="registered", admitted_in=None):
    pid = f"IDP{uuid.uuid4().hex[:8].upper()}"
    name = f"Kavitha Identcheck {pid[-4:]}"
    mrn = f"IDC-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, diagnosis, date_of_birth, "
            "phone) VALUES (?, ?, ?, 47, 'F', ?, 'Identity fixture', ?, ?)",
            (pid, name, mrn, status, dob, phone))
        if admitted_in:
            db.execute(
                "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                "VALUES ('IDCADMIT', 'Identity Check Admitter', 'doctor', 'x')")
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                "status, created_at) VALUES (?, ?, 'IDCADMIT', ?, 'Bay', '3', 'active', ?)",
                (f"IDA{uuid.uuid4().hex[:8].upper()}", pid, admitted_in,
                 datetime.now(timezone.utc).isoformat()))
    return {"id": pid, "name": name, "mrn": mrn}


def _stated(p, **over):
    body = {"name": p["name"], "date_of_birth": STORED_DOB, "phone_last4": STORED_PHONE[-4:]}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def _visit(uid, role, pid, identity=None, **extra):
    body = {"patient_id": pid, **extra}
    if identity is not None:
        body["identity_check"] = identity
    return client.post("/api/frontdesk/opd/visits", json=body, headers=_auth(uid, role))


def _visits_for(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM opd_visits WHERE patient_id = ?", (pid,)).fetchall()]


def _audit(action, pid):
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? ORDER BY id",
            (action, pid)).fetchall()
    return [dict(r) for r in rows]


def _meta(row):
    return json.loads(row["metadata_json"] or "{}")


@pytest.fixture(scope="module")
def staff():
    return {
        "doc": _mk_user("doctor", DEPT, "Dr Identity Medicine"),
        "doc_other": _mk_user("doctor", OTHER_DEPT, "Dr Identity Surgery"),
        "admin": _mk_user("admin", None),
    }


@pytest.fixture
def registration_on(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    assert config.feature_enabled("registration")
    yield


@pytest.fixture
def opd_on(monkeypatch):
    from ws import realtime
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    assert config.feature_enabled("opd")
    yield


@pytest.fixture
def registration_off(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    assert not config.feature_enabled("registration")
    yield


def _no_secrets(resp):
    text = resp.text
    assert STORED_DOB not in text
    assert STORED_PHONE not in text


# ── 1. Flag off ──────────────────────────────────────────────────────────────

def test_flag_off_needs_no_check_and_stores_nothing(staff, registration_off):
    p = _patient()
    r = _visit(staff["doc"], "doctor", p["id"])
    assert r.status_code == 200, r.text
    assert "identity_check" not in r.json()
    (row,) = _visits_for(p["id"])
    assert row["identity_check_json"] is None
    (audit,) = _audit("opd_visit_registered", p["id"])
    assert "identity_check" not in _meta(audit)
    assert _audit("identity_check_passed", p["id"]) == []


def test_flag_off_ignores_a_check_that_is_sent(staff, registration_off):
    p = _patient()
    r = _visit(staff["doc"], "doctor", p["id"], identity=_stated(p, date_of_birth="1999-01-01"))
    assert r.status_code == 200, r.text
    (row,) = _visits_for(p["id"])
    assert row["identity_check_json"] is None
    assert _audit("identity_check_failed", p["id"]) == []


# ── 2. No check, no visit ────────────────────────────────────────────────────

def test_a_searched_patient_without_a_check_is_refused(staff, registration_on):
    p = _patient()
    r = _visit(staff["doc"], "doctor", p["id"])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "identity_check_required"
    assert _visits_for(p["id"]) == []
    (refused,) = _audit("opd_visit_identity_refused", p["id"])
    assert refused["event_category"] == "patient_identity"
    assert refused["outcome"] == "denied"
    assert _meta(refused)["reason_code"] == "identity_check_required"


def test_the_administrator_needs_the_check_too(staff, registration_on):
    p = _patient()
    r = _visit(staff["admin"], "admin", p["id"], department=DEPT)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "identity_check_required"
    ok = _visit(staff["admin"], "admin", p["id"], identity=_stated(p), department=DEPT)
    assert ok.status_code == 200, ok.text


# ── 3. A wrong date of birth ─────────────────────────────────────────────────

def test_a_wrong_date_of_birth_is_refused_and_audited(staff, registration_on):
    p = _patient()
    r = _visit(staff["doc"], "doctor", p["id"], identity=_stated(p, date_of_birth="1979-04-24"))
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "identity_not_confirmed"
    _no_secrets(r)
    assert _visits_for(p["id"]) == []
    (failed,) = _audit("identity_check_failed", p["id"])
    assert _meta(failed)["purpose"] == "opd_visit"
    assert "date_of_birth" in _meta(failed)["mismatched"]
    (refused,) = _audit("opd_visit_identity_refused", p["id"])
    assert _meta(refused)["reason_code"] == "identity_not_confirmed"


def test_a_wrong_phone_or_name_is_refused(staff, registration_on):
    p = _patient()
    bad_phone = _visit(staff["doc"], "doctor", p["id"], identity=_stated(p, phone_last4="0000"))
    assert bad_phone.status_code == 409
    bad_name = _visit(staff["doc"], "doctor", p["id"],
                      identity=_stated(p, name="Somebody Else Entirely"))
    assert bad_name.status_code == 409
    assert _visits_for(p["id"]) == []


def test_no_answer_in_the_flow_carries_the_stored_dob_or_phone(staff, registration_on):
    p = _patient()
    uid = staff["doc"]
    check = client.post("/api/registration/identity-check",
                        json={"patient_id": p["id"], **_stated(p, date_of_birth="1980-01-01")},
                        headers=_auth(uid, "doctor"))
    assert check.status_code == 200 and check.json()["result"] == "no_match"
    _no_secrets(check)
    good = client.post("/api/registration/identity-check",
                       json={"patient_id": p["id"], **_stated(p)}, headers=_auth(uid, "doctor"))
    assert good.json()["result"] == "match"
    assert set(good.json()) == {"result", "checked", "checked_at"}
    visit = _visit(uid, "doctor", p["id"], identity=_stated(p))
    assert visit.status_code == 200
    _no_secrets(visit)


# ── 4. A match is stored ─────────────────────────────────────────────────────

def test_a_match_is_stored_on_the_visit_and_in_its_audit_row(staff, registration_on):
    p = _patient()
    r = _visit(staff["doc"], "doctor", p["id"], identity=_stated(p))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["identity_check"]["result"] == "match"
    assert set(body["identity_check"]) == {"result", "checked", "checked_at"}
    assert set(body["identity_check"]["checked"]) == {"name", "date_of_birth", "phone_last4"}
    (row,) = _visits_for(p["id"])
    stored = json.loads(row["identity_check_json"])
    assert stored["result"] == "match"
    assert stored["checked_by"] == staff["doc"]
    assert stored["purpose"] == "opd_visit" and stored["method"] == "stated_identifiers"
    assert STORED_DOB not in row["identity_check_json"]
    assert STORED_PHONE[-4:] not in row["identity_check_json"]
    (audit,) = _audit("opd_visit_registered", p["id"])
    assert _meta(audit)["identity_check"]["result"] == "match"
    (passed,) = _audit("identity_check_passed", p["id"])
    assert _meta(passed)["purpose"] == "opd_visit"


def test_a_stated_age_and_the_uhid_also_confirm(staff, registration_on):
    p = _patient()
    from services import patient_matching as pm
    from datetime import date
    age = pm.full_years(date.fromisoformat(STORED_DOB), date.today())
    r = _visit(staff["doc"], "doctor", p["id"],
               identity={"name": p["name"], "age_years": age, "uhid": p["mrn"]})
    assert r.status_code == 200, r.text
    assert set(r.json()["identity_check"]["checked"]) == {"name", "age_years", "uhid"}


def test_opd_mode_writes_the_check_in_the_visit_transaction(staff, opd_on):
    p = _patient()
    r = _visit(staff["doc"], "doctor", p["id"], identity=_stated(p))
    assert r.status_code == 200, r.text
    (audit,) = _audit("opd_visit_registered", p["id"])
    meta = _meta(audit)
    assert meta["written"] == "in_transaction"
    assert meta["identity_check"]["result"] == "match"
    assert len(_audit("opd_scope_opened", p["id"])) == 1
    (row,) = _visits_for(p["id"])
    assert json.loads(row["identity_check_json"])["result"] == "match"


def test_opd_mode_refuses_without_a_check_and_opens_no_scope(staff, opd_on):
    p = _patient()
    r = _visit(staff["doc"], "doctor", p["id"])
    assert r.status_code == 409
    assert _visits_for(p["id"]) == []
    assert _audit("opd_scope_opened", p["id"]) == []


# ── 5. Edges ─────────────────────────────────────────────────────────────────

def test_too_few_identifiers_is_422(staff, registration_on):
    p = _patient()
    r = _visit(staff["doc"], "doctor", p["id"], identity={"name": p["name"]})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "identifiers_required"
    assert _visits_for(p["id"]) == []


def test_an_unknown_field_in_the_check_is_refused(staff, registration_on):
    p = _patient()
    r = _visit(staff["doc"], "doctor", p["id"],
               identity={**_stated(p), "allergies": "nkda"})
    assert r.status_code == 422
    assert _visits_for(p["id"]) == []


def test_the_sixth_failure_in_an_hour_is_429_even_with_the_right_answers(registration_on):
    uid = _mk_user("doctor", DEPT)
    p = _patient()
    for _ in range(5):
        r = _visit(uid, "doctor", p["id"], identity=_stated(p, phone_last4="1111"))
        assert r.status_code == 409
    locked = _visit(uid, "doctor", p["id"], identity=_stated(p))
    assert locked.status_code == 429
    assert locked.json()["detail"]["error"] == "identity_check_locked"
    assert _visits_for(p["id"]) == []


def test_another_wards_inpatient_is_still_404(staff, registration_on):
    p = _patient(status="admitted", admitted_in=OTHER_DEPT)
    r = _visit(staff["doc"], "doctor", p["id"], identity=_stated(p))
    assert r.status_code == 404
    assert _visits_for(p["id"]) == []


def test_an_unknown_patient_is_404_with_a_check(staff, registration_on):
    r = _visit(staff["admin"], "admin", "IDP-NO-SUCH-PATIENT",
               identity={"name": "Nobody", "age_years": 40, "uhid": "X1"}, department=DEPT)
    assert r.status_code == 404


# ── 6. A patient registered a moment ago ─────────────────────────────────────

def test_a_new_registration_then_a_visit_with_what_was_typed(staff, registration_on, monkeypatch):
    monkeypatch.setattr(config, "UHID_SOURCE", "hospital")
    monkeypatch.setattr(config, "UHID_PATTERN", None)
    uid = _mk_user("receptionist", DEPT)
    typed = {"name": f"Newcomer Walkin {uuid.uuid4().hex[:6]}", "sex": "M", "age_years": 33,
             "uhid": f"NEW-{uuid.uuid4().hex[:8].upper()}"}
    reg = client.post("/api/registration/patients", json=typed,
                      headers=_auth(uid, "receptionist"))
    assert reg.status_code == 201, reg.text
    patient = reg.json()["patient"]
    r = _visit(uid, "receptionist", patient["patient_id"],
               identity={"name": typed["name"], "age_years": typed["age_years"],
                         "uhid": patient["uhid"]})
    assert r.status_code == 200, r.text
    assert r.json()["identity_check"]["result"] == "match"
