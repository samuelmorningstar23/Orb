"""The opt-in OPD scope arm and the OPD record view (opd-7, W3-A, 4 October 2026).

An outpatient has no admission, so permissions.patient_in_scope fail-closes
them out of every department. opd-7 adds a SEPARATE predicate,
permissions.patient_in_opd_scope, reached only through
require_patient_access(..., opd_arm=True) on a pinned whitelist
(test_opd_scope_drift.py), so the clinic that saw a patient can open their
outpatient record view for ORB_OPD_SCOPE_DAYS days (ruling OPD-N2: 7 days,
extent ii, self-registered visits count, flagged and audited).

What this file defends:

  1. FLAG OFF, NOTHING CHANGED. The arm answers exactly what patient_in_scope
     answers on every fixture; the record route is 404; the registration rule
     and its post-commit audit are the old ones.
  2. FLAG ON, THE ARM IS NARROW. A walk-in is visible on the record view to the
     visit's department only; not after the window, not for a cancelled visit,
     not for a future booking, not once actively admitted anywhere; a visit
     cannot launder another ward's inpatient.
  3. THE RECORD IS EXTENT II AND NO MORE: no date of birth, phone or address,
     no other clinic's desk complaint, no vitals or score.
  4. REGISTRATION IN OPD MODE: a patient discharged from Cardiology can be
     registered into Ortho (not with the flag off); the audit rows commit with
     the visit; 'opd_scope_opened' is flagged self_registered for an opd.consult
     holder; an audit failure rolls the visit back; an idempotent replay is
     audited on every call and is bound to its patient and clinic (a key
     replayed across clinics gets a 409 that discloses nothing, audited).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from permissions import (
    enforce_patient_scope,
    opd_scope_window,
    patient_in_opd_scope,
    patient_in_scope,
)
from services.audit_service import AuditWriteError
from services.clinical_time import utc_iso_now
from services.hospital_time import hospital_today

client = TestClient(app)

DEPT_A = "OPD Scope Cardiology"
DEPT_B = "OPD Scope Orthopaedics"
DEPT_C = "OPD Scope Oncology"

SECRET_DOB = "1958-02-11"
SECRET_PHONE = "9990001234"
SECRET_ADDRESS = "17 Opdscope Lane"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department):
    uid = f"OSC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, f"Opd Scope {role} {uid[-4:]}", role, department),
        )
    return uid


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_patient(*, status="registered", admission_dept=None, admission_status=None,
                secrets=False):
    pid = f"OSP{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('OSCADMITTER', 'Opd Scope Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, diagnosis, "
            "date_of_birth, phone, address) VALUES (?, ?, ?, 47, 'F', ?, 'Opd scope fixture', "
            "?, ?, ?)",
            (pid, f"Opdscope Patient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}",
             status, SECRET_DOB if secrets else None, SECRET_PHONE if secrets else None,
             SECRET_ADDRESS if secrets else None))
        if admission_dept:
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                "status, created_at) VALUES (?, ?, 'OSCADMITTER', ?, ?, '1', ?, ?)",
                (f"OSA{uuid.uuid4().hex[:8].upper()}", pid, admission_dept,
                 f"{admission_dept} bay", admission_status or "active", now))
    return pid


def _outpatient(**kw):
    return _mk_patient(status="registered", **kw)


def _inpatient(dept):
    return _mk_patient(status="admitted", admission_dept=dept, admission_status="active")


def _discharged(dept):
    return _mk_patient(status="discharged", admission_dept=dept, admission_status="discharged")


def _admit(pid, dept):
    with db_session() as db:
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
            "status, created_at) VALUES (?, ?, 'OSCADMITTER', ?, ?, '2', 'active', ?)",
            (f"OSA{uuid.uuid4().hex[:8].upper()}", pid, dept, f"{dept} bay",
             datetime.now(timezone.utc).isoformat()))
        db.execute("UPDATE patients SET status = 'admitted' WHERE id = ?", (pid,))


def _visit(pid, dept, *, days_ago=0, status="waiting", complaint=None):
    vid = f"OPD-T{uuid.uuid4().hex[:7].upper()}"
    day = (hospital_today() - timedelta(days=days_ago)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
            "status, registered_by, registered_at, chief_complaint) "
            "VALUES (?, ?, ?, ?, ?, ?, 'OSCADMITTER', ?, ?)",
            (vid, pid, dept, day, 900 + abs(hash(vid)) % 9000, status,
             datetime.now(timezone.utc).isoformat(), complaint))
    return vid


@pytest.fixture(scope="module")
def staff():
    s = {
        "doc_a": _mk_user("doctor", DEPT_A),
        "doc_b": _mk_user("doctor", DEPT_B),
        "doc_c": _mk_user("doctor", DEPT_C),
        "nurse_a": _mk_user("nurse", DEPT_A),
        "recep_a": _mk_user("receptionist", DEPT_A),
        "admin": _mk_user("admin", None),
    }
    return s


@pytest.fixture
def opd_on(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(config, "OPD_SCOPE_DAYS", 7)
    assert config.feature_enabled("opd")
    yield


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    monkeypatch.setattr(config, "OPD_SCOPE_DAYS", 7)
    assert not config.feature_enabled("opd")
    yield


def _record(pid, uid, role="doctor"):
    return client.get(f"/api/opd/patients/{pid}/record", headers=_auth(uid, role))


def _audit_rows(action_type, patient_id):
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? ORDER BY id",
            (action_type, patient_id)).fetchall()
    return [dict(r) for r in rows]


def _meta(row):
    return json.loads(row["metadata_json"] or "{}")


def _register(uid, role, pid, department=None):
    body = {"patient_id": pid}
    if department:
        body["department"] = department
    return client.post("/api/frontdesk/opd/visits", json=body, headers=_auth(uid, role))


def _scope_matrix():
    """Patients covering every branch of both predicates."""
    walkin_a = _outpatient()
    _visit(walkin_a, DEPT_A)
    stale_a = _outpatient()
    _visit(stale_a, DEPT_A, days_ago=30)
    discharged_b = _discharged(DEPT_B)
    _visit(discharged_b, DEPT_A)
    admitted_c = _inpatient(DEPT_C)
    _visit(admitted_c, DEPT_A)
    return [walkin_a, stale_a, discharged_b, admitted_c, _outpatient(),
            _inpatient(DEPT_A), "NO-SUCH-PATIENT"]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Flag off: nothing changed
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_the_arm_answers_exactly_what_patient_in_scope_answers(staff, opd_off):
    with db_session() as db:
        for pid in _scope_matrix():
            for dept in (DEPT_A, DEPT_B, DEPT_C, None):
                assert patient_in_opd_scope(db, pid, dept) == patient_in_scope(db, pid, dept), (
                    f"flag off: the OPD arm disagrees with patient_in_scope for {pid} in {dept}")


def test_flag_off_the_record_route_is_switched_off(staff, opd_off):
    pid = _outpatient()
    _visit(pid, DEPT_A)
    resp = _record(pid, staff["doc_a"])
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "feature_disabled"


def test_flag_off_a_cardiology_discharge_cannot_be_registered_into_ortho(staff, opd_off):
    pid = _discharged(DEPT_A)
    resp = _register(staff["doc_b"], "doctor", pid)
    assert resp.status_code == 404, resp.text


def test_flag_off_registration_keeps_its_post_commit_audit_and_opens_nothing(staff, opd_off):
    pid = _outpatient()
    resp = _register(staff["doc_a"], "doctor", pid)
    assert resp.status_code == 200, resp.text
    visit_id = resp.json()["id"]
    rows = _audit_rows("opd_visit_registered", pid)
    assert len(rows) == 1
    assert "written" not in _meta(rows[0]) and "self_registered" not in _meta(rows[0])
    assert _audit_rows("opd_scope_opened", pid) == []
    with db_session() as db:
        flag = db.execute("SELECT scope_self_registered FROM opd_visits WHERE id = ?",
                          (visit_id,)).fetchone()[0]
    assert flag == 0
    # The response shape is the front desk's old one: no migration-50 columns.
    assert "scope_self_registered" not in resp.json()


# ═════════════════════════════════════════════════════════════════════════════
# 2. Flag on: the arm is narrow
# ═════════════════════════════════════════════════════════════════════════════

def test_a_walk_in_is_visible_to_the_visit_department_only(staff, opd_on):
    pid = _outpatient()
    _visit(pid, DEPT_A)
    assert _record(pid, staff["doc_a"]).status_code == 200
    assert _record(pid, staff["doc_b"]).status_code == 404
    assert _record(pid, staff["doc_c"]).status_code == 404


def test_the_record_view_needs_opd_consult(staff, opd_on):
    pid = _outpatient()
    _visit(pid, DEPT_A)
    assert _record(pid, staff["nurse_a"], "nurse").status_code == 403
    assert _record(pid, staff["recep_a"], "receptionist").status_code == 403
    assert _record(pid, staff["admin"], "admin").status_code == 403


def test_the_window_closes_after_scope_days(staff, opd_on):
    edge = _outpatient()
    _visit(edge, DEPT_A, days_ago=7)
    gone = _outpatient()
    _visit(gone, DEPT_A, days_ago=8)
    assert _record(edge, staff["doc_a"]).status_code == 200
    assert _record(gone, staff["doc_a"]).status_code == 404


def test_scope_days_zero_means_the_visit_day_only(staff, opd_on, monkeypatch):
    monkeypatch.setattr(config, "OPD_SCOPE_DAYS", 0)
    today = _outpatient()
    _visit(today, DEPT_A)
    yesterday = _outpatient()
    _visit(yesterday, DEPT_A, days_ago=1)
    assert _record(today, staff["doc_a"]).status_code == 200
    assert _record(yesterday, staff["doc_a"]).status_code == 404
    first, last = opd_scope_window()
    assert first == last == hospital_today().isoformat()


def test_a_cancelled_visit_opens_nothing(staff, opd_on):
    pid = _outpatient()
    _visit(pid, DEPT_A, status="cancelled")
    assert _record(pid, staff["doc_a"]).status_code == 404


def test_a_future_booking_opens_nothing_until_its_day(staff, opd_on):
    pid = _outpatient()
    _visit(pid, DEPT_A, days_ago=-2)
    assert _record(pid, staff["doc_a"]).status_code == 404


def test_the_department_is_compared_case_folded(staff, opd_on):
    pid = _outpatient()
    _visit(pid, DEPT_A.upper())
    assert _record(pid, staff["doc_a"]).status_code == 200


def test_an_active_admission_anywhere_closes_the_arm(staff, opd_on):
    pid = _outpatient()
    _visit(pid, DEPT_A)
    assert _record(pid, staff["doc_a"]).status_code == 200
    _admit(pid, DEPT_C)
    assert _record(pid, staff["doc_a"]).status_code == 404
    # The admitting ward reads them through the ordinary scope, as before.
    resp = _record(pid, staff["doc_c"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["access"]["arm"] == "admission"


def test_a_visit_cannot_launder_another_wards_inpatient(staff, opd_on):
    pid = _inpatient(DEPT_C)
    # The desk refuses to register them into Cardiology...
    assert _register(staff["doc_a"], "doctor", pid).status_code == 404
    # ...and a visit row that got there anyway opens nothing.
    _visit(pid, DEPT_A)
    assert _record(pid, staff["doc_a"]).status_code == 404
    with db_session() as db:
        assert patient_in_opd_scope(db, pid, DEPT_A) is False


def test_the_shared_scope_and_the_sage_scope_are_untouched_with_the_flag_on(staff, opd_on):
    """The arm is a separate predicate: patient_in_scope (and its pinned Sage
    duplicate) still say no for a walk-in, even while the OPD arm says yes."""
    from services.sage_service import _patient_in_scope as sage_in_scope

    pid = _outpatient()
    _visit(pid, DEPT_A)
    with db_session() as db:
        assert patient_in_opd_scope(db, pid, DEPT_A) is True
        assert patient_in_scope(db, pid, DEPT_A) is False
        assert sage_in_scope(db, pid, DEPT_A) is False
        for other in _scope_matrix():
            for dept in (DEPT_A, DEPT_B, None):
                assert patient_in_scope(db, other, dept) == sage_in_scope(db, other, dept)


def test_enforce_patient_scope_takes_the_arm_only_when_asked(staff, opd_on):
    from fastapi import HTTPException

    pid = _outpatient()
    _visit(pid, DEPT_A)
    user = {"id": staff["doc_a"], "role": "doctor", "department": DEPT_A}
    enforce_patient_scope(user, pid, opd_arm=True)
    with pytest.raises(HTTPException) as exc:
        enforce_patient_scope(user, pid)
    assert exc.value.status_code == 404
    other = {"id": staff["doc_b"], "role": "doctor", "department": DEPT_B}
    with pytest.raises(HTTPException) as exc:
        enforce_patient_scope(other, pid, opd_arm=True)
    assert exc.value.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# 3. The record view: extent ii and no more, audited
# ═════════════════════════════════════════════════════════════════════════════

def test_the_record_carries_extent_ii_and_never_the_second_factor(staff, opd_on):
    pid = _outpatient(secrets=True)
    own_visit = _visit(pid, DEPT_A, complaint="Opdscope own clinic complaint")
    _visit(pid, DEPT_B, days_ago=3, complaint="Opdscope ortho desk complaint")
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
            "status, recorded_by) VALUES (?, 'penicillin', 'anaphylaxis', 'severe', "
            "'active', 'OSCADMITTER')", (pid,))
        db.execute(
            "INSERT INTO problem_list (patient_id, problem, status, recorded_by) "
            "VALUES (?, 'Opdscope hypertension', 'active', 'OSCADMITTER')", (pid,))
        db.execute(
            "INSERT INTO orders (id, patient_id, order_type, description, status, "
            "ordered_by, result_text, result_at, opd_visit_id) VALUES (?, ?, 'lab', "
            "'Opdscope lipid profile', 'resulted', 'OSCADMITTER', 'LDL 3.1', ?, ?)",
            (f"ORD-OS{uuid.uuid4().hex[:6]}", pid, now, own_visit))
        db.execute(
            "INSERT INTO orders (id, patient_id, order_type, description, status, "
            "ordered_by) VALUES (?, ?, 'lab', 'Opdscope inpatient order', 'active', "
            "'OSCADMITTER')", (f"ORD-OS{uuid.uuid4().hex[:6]}", pid))
        db.execute(
            "INSERT INTO vitals (patient_id, heart_rate, timestamp, carried_from) "
            "VALUES (?, 131, ?, NULL)", (pid, utc_iso_now()))

    resp = _record(pid, staff["doc_a"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    text = resp.text

    assert body["patient"]["id"] == pid
    assert body["patient"]["date_of_birth_on_file"] is True
    for secret in (SECRET_DOB, SECRET_PHONE, SECRET_ADDRESS):
        assert secret not in text, f"{secret!r} leaked onto the OPD record view"
    assert "date_of_birth" not in body["patient"]

    assert [a["substance"] for a in body["allergies"]] == ["penicillin"]
    assert body["allergy_status"] == "recorded"
    assert [p["problem"] for p in body["problems"]] == ["Opdscope hypertension"]

    assert [r["description"] for r in body["results"]] == ["Opdscope lipid profile"]
    assert body["results"][0]["result_text"] == "LDL 3.1"

    by_clinic = {v["department"]: v for v in body["opd_visits"]}
    assert by_clinic[DEPT_A]["chief_complaint"] == "Opdscope own clinic complaint"
    assert by_clinic[DEPT_B]["chief_complaint"] is None
    assert "Opdscope ortho desk complaint" not in text

    assert body["consultations"] == [] and body["consultations_available"] is False
    assert body["access"]["arm"] == "opd_visit"
    assert body["access"]["visit_id"] == own_visit
    assert body["access"]["scope_until"] == (hospital_today() + timedelta(days=7)).isoformat()

    # No vitals, no score, no band anywhere on it.
    for word in ("vitals", "news2", "risk", "heart_rate"):
        assert word not in text.lower(), word


def test_an_empty_allergy_list_is_never_reported_as_no_known_allergies(staff, opd_on):
    pid = _outpatient()
    _visit(pid, DEPT_A)
    body = _record(pid, staff["doc_a"]).json()
    assert body["allergies"] == []
    assert body["allergy_status"] == "none_recorded"


def test_every_record_read_is_audited_with_its_arm(staff, opd_on):
    pid = _outpatient()
    vid = _visit(pid, DEPT_A)
    assert _record(pid, staff["doc_a"]).status_code == 200
    rows = _audit_rows("opd_record_viewed", pid)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_category"] == "phi_access" and row["outcome"] == "success"
    meta = _meta(row)
    assert meta["scope_arm"] == "opd_visit" and meta["opd_visit_id"] == vid
    assert meta["self_registered"] is False


def test_a_refused_record_read_is_recorded_as_denied(staff, opd_on):
    pid = _outpatient()
    _visit(pid, DEPT_A)
    assert _record(pid, staff["doc_b"]).status_code == 404
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'patient_scope_denied' "
            "AND patient_id = ? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    assert row is not None
    assert _meta(row)["opd_arm"] is True
    assert _audit_rows("opd_record_viewed", pid) == []


# ═════════════════════════════════════════════════════════════════════════════
# 4. Registration in OPD mode
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_on_a_cardiology_discharge_can_be_registered_into_ortho(staff, opd_on):
    pid = _discharged(DEPT_A)
    resp = _register(staff["doc_b"], "doctor", pid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["department"] == DEPT_B
    # And now Ortho's record view opens for them through the visit.
    rec = _record(pid, staff["doc_b"])
    assert rec.status_code == 200 and rec.json()["access"]["arm"] == "opd_visit"


def test_a_self_registered_visit_is_flagged_and_audited_in_the_same_transaction(staff, opd_on):
    pid = _outpatient()
    resp = _register(staff["doc_a"], "doctor", pid)
    assert resp.status_code == 200, resp.text
    visit_id = resp.json()["id"]
    with db_session() as db:
        flag = db.execute("SELECT scope_self_registered FROM opd_visits WHERE id = ?",
                          (visit_id,)).fetchone()[0]
    assert flag == 1

    registered = _audit_rows("opd_visit_registered", pid)
    opened = _audit_rows("opd_scope_opened", pid)
    assert len(registered) == 1 and len(opened) == 1
    assert _meta(registered[0])["written"] == "in_transaction"
    assert _meta(registered[0])["self_registered"] is True
    row = opened[0]
    assert row["event_category"] == "clinical_access"
    assert row["target_type"] == "opd_visit" and row["target_id"] == visit_id
    assert row["correlation_id"] == registered[0]["correlation_id"] == f"opd-register-{visit_id}"
    meta = _meta(row)
    assert meta["self_registered"] is True
    assert meta["department"] == DEPT_A
    assert meta["scope_days"] == 7
    assert meta["scope_until"] == (hospital_today() + timedelta(days=7)).isoformat()
    assert meta["extends_open_window"] is False
    # The record view says the arm came from a self-registered visit.
    rec = _record(pid, staff["doc_a"]).json()
    assert rec["access"]["self_registered"] is True


def test_a_receptionists_visit_opens_scope_unflagged(staff, opd_on):
    pid = _outpatient()
    resp = _register(staff["recep_a"], "receptionist", pid)
    assert resp.status_code == 200, resp.text
    opened = _audit_rows("opd_scope_opened", pid)
    assert len(opened) == 1 and _meta(opened[0])["self_registered"] is False
    with db_session() as db:
        flag = db.execute("SELECT scope_self_registered FROM opd_visits WHERE id = ?",
                          (resp.json()["id"],)).fetchone()[0]
    assert flag == 0


def test_a_second_visit_in_the_window_says_it_extends_the_open_window(staff, opd_on):
    pid = _outpatient()
    _visit(pid, DEPT_A, days_ago=2)
    assert _register(staff["recep_a"], "receptionist", pid).status_code == 200
    opened = _audit_rows("opd_scope_opened", pid)
    assert len(opened) == 1 and _meta(opened[0])["extends_open_window"] is True


def test_an_audit_failure_rolls_the_visit_back(staff, opd_on, monkeypatch):
    import routes.frontdesk_routes as fr

    real = fr.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("event_category") == "clinical_access":
            raise AuditWriteError("simulated audit outage")
        return real(*args, **kwargs)

    monkeypatch.setattr(fr, "log_audit_event", failing)
    pid = _outpatient()
    resp = _register(staff["doc_a"], "doctor", pid)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "audit_unavailable"
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM opd_visits WHERE patient_id = ?", (pid,)).fetchone()[0]
    assert n == 0, "the visit survived an audit row that could not be written"
    # The register row was in the same transaction, so it rolled back too.
    assert _audit_rows("opd_visit_registered", pid) == []
    assert _record(pid, staff["doc_a"]).status_code == 404


def test_the_registration_rule_is_rechecked_under_the_write_lock(staff, opd_on, monkeypatch):
    """An admission that lands between the pre-check and the visit write must
    not leave a visit that skipped the rule."""
    import routes.frontdesk_routes as fr

    calls = {"n": 0}

    def flips(db, patient_id, dept):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(fr, "opd_registration_allowed", flips)
    pid = _outpatient()
    resp = _register(staff["doc_a"], "doctor", pid)
    assert resp.status_code == 404, resp.text
    assert calls["n"] == 2
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM opd_visits WHERE patient_id = ?", (pid,)).fetchone()[0]
    assert n == 0


def test_an_inpatients_own_clinic_visit_opens_no_outpatient_scope(staff, opd_on):
    pid = _inpatient(DEPT_A)
    resp = _register(staff["doc_a"], "doctor", pid)
    assert resp.status_code == 200, resp.text
    assert len(_audit_rows("opd_visit_registered", pid)) == 1
    assert _audit_rows("opd_scope_opened", pid) == []


def _register_keyed(uid, role, pid, key, department=None):
    body = {"patient_id": pid, "idempotency_key": key}
    if department:
        body["department"] = department
    return client.post("/api/frontdesk/opd/visits", json=body, headers=_auth(uid, role))


@pytest.mark.parametrize("mode", ["opd_on", "opd_off"])
def test_an_idempotent_replay_is_audited_on_every_call(staff, request, mode):
    """The replay returns the patient's name and MRN again, so it is a
    disclosure and gets its own row in both modes. OPD mode opens the scope
    once: a repeat writes no second 'opd_scope_opened'."""
    request.getfixturevalue(mode)
    pid = _outpatient()
    key = f"osc-{uuid.uuid4().hex}"
    # A doctor, because a receptionist holds the desk only with the
    # registration feature on, and the flag-off leg must reach the route.
    first = _register_keyed(staff["doc_a"], "doctor", pid, key)
    again = _register_keyed(staff["doc_a"], "doctor", pid, key)
    assert first.status_code == again.status_code == 200, again.text
    assert first.json()["id"] == again.json()["id"]
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM opd_visits WHERE patient_id = ?", (pid,)).fetchone()[0]
    assert n == 1

    rows = _audit_rows("opd_visit_registered", pid)
    assert len(rows) == 2, "every successful call writes its row, in both modes"
    assert all(r["target_id"] == first.json()["id"] for r in rows)
    if mode == "opd_on":
        assert len(_audit_rows("opd_scope_opened", pid)) == 1
        metas = [_meta(r) for r in rows]
        assert [m["replay"] for m in metas] == [False, True]
        assert all(m["written"] == "in_transaction" for m in metas)
        assert rows[1]["correlation_id"] == f"opd-register-{first.json()['id']}"
        assert rows[1]["event_category"] == "clinical_record"
    else:
        assert _audit_rows("opd_scope_opened", pid) == []
        assert all("replay" not in _meta(r) for r in rows)


def test_a_key_replayed_for_another_patient_in_another_clinic_is_refused(staff, opd_on):
    """Ortho registers X with key K; Cardiology then posts Y with the same K.
    The key lookup is global, so before the replay was bound this returned X's
    Ortho visit, name and MRN included, to Cardiology. Now: 409, nothing about
    X in the reply, nothing saved for Y, and the refusal is on the trail."""
    x = _outpatient()
    y = _outpatient()
    key = f"osc-{uuid.uuid4().hex}"
    first = _register_keyed(staff["doc_b"], "doctor", x, key)
    assert first.status_code == 200, first.text
    x_visit = first.json()

    resp = _register_keyed(staff["doc_a"], "doctor", y, key)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "registration_key_conflict"
    text = resp.text
    for leaked in (x, x_visit["id"], x_visit["patient_name"], x_visit["patient_mrn"], DEPT_B):
        assert leaked not in text, f"the refusal disclosed {leaked!r}"

    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM opd_visits WHERE patient_id = ?", (y,)).fetchone()[0]
    assert n == 0
    assert _audit_rows("opd_visit_registered", y) == []
    assert _audit_rows("opd_scope_opened", y) == []
    # X's trail is the first registration only: the refused call disclosed nothing.
    assert len(_audit_rows("opd_visit_registered", x)) == 1
    assert len(_audit_rows("opd_scope_opened", x)) == 1

    refused = _audit_rows("opd_visit_replay_refused", y)
    assert len(refused) == 1
    row = refused[0]
    assert row["event_category"] == "authorization" and row["outcome"] == "denied"
    assert row["actor_user_id"] == staff["doc_a"]
    assert row["target_type"] == "opd_visit" and row["target_id"] == x_visit["id"]
    assert _meta(row)["requested_department"] == DEPT_A
    # And Cardiology still cannot open X's record view.
    assert _record(x, staff["doc_a"]).status_code == 404


def test_a_key_replayed_for_the_same_patient_in_another_clinic_is_refused(staff, opd_on):
    """Same patient, same key, different clinic: the replay would hand
    Cardiology a visit from Ortho's queue, so it is bound to the clinic too."""
    pid = _outpatient()
    key = f"osc-{uuid.uuid4().hex}"
    first = _register_keyed(staff["doc_b"], "doctor", pid, key)
    assert first.status_code == 200, first.text

    resp = _register_keyed(staff["doc_a"], "doctor", pid, key)
    assert resp.status_code == 409, resp.text
    assert first.json()["id"] not in resp.text
    with db_session() as db:
        depts = [r[0] for r in db.execute(
            "SELECT department FROM opd_visits WHERE patient_id = ?", (pid,)).fetchall()]
    assert depts == [DEPT_B]
    assert len(_audit_rows("opd_visit_registered", pid)) == 1
    refused = _audit_rows("opd_visit_replay_refused", pid)
    assert len(refused) == 1 and refused[0]["target_id"] == first.json()["id"]


def test_a_replay_naming_the_clinic_in_another_case_still_matches(staff, opd_on):
    """The clinic is compared on its canonical spelling, so a repeat that typed
    the department in lower case is the same registration, not a conflict."""
    pid = _outpatient()
    key = f"osc-{uuid.uuid4().hex}"
    first = _register_keyed(staff["doc_a"], "doctor", pid, key)
    again = _register_keyed(staff["doc_a"], "doctor", pid, key, department=DEPT_A.lower())
    assert first.status_code == again.status_code == 200, again.text
    assert first.json()["id"] == again.json()["id"]
    assert [_meta(r)["replay"] for r in _audit_rows("opd_visit_registered", pid)] == [False, True]
    assert _audit_rows("opd_visit_replay_refused", pid) == []


def test_call_in_complete_and_cancel_audit_inside_their_transactions(staff, opd_on):
    seen = _outpatient()
    reg = _register(staff["doc_a"], "doctor", seen).json()
    hdr = _auth(staff["doc_a"], "doctor")
    assert client.post(f"/api/frontdesk/opd/visits/{reg['id']}/start", headers=hdr).status_code == 200
    done = client.post(f"/api/frontdesk/opd/visits/{reg['id']}/complete", json={}, headers=hdr)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"
    assert "consultation_charge" in done.json()
    for action in ("opd_consultation_started", "opd_visit_completed"):
        rows = _audit_rows(action, seen)
        assert len(rows) == 1, action
        assert _meta(rows[0])["written"] == "in_transaction"
    completed = _meta(_audit_rows("opd_visit_completed", seen)[0])
    assert completed["charge_id"] == done.json()["consultation_charge"]["charge_id"]

    left = _outpatient()
    reg2 = _register(staff["recep_a"], "receptionist", left).json()
    resp = client.post(f"/api/frontdesk/opd/visits/{reg2['id']}/cancel",
                       json={"reason": "Left without being seen"},
                       headers=_auth(staff["recep_a"], "receptionist"))
    assert resp.status_code == 200, resp.text
    rows = _audit_rows("opd_visit_cancelled", left)
    assert len(rows) == 1 and _meta(rows[0])["written"] == "in_transaction"
    # A cancelled visit closes the record view it had opened.
    assert _record(left, staff["doc_a"]).status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# 5. Repeat calls: OPD mode never writes fewer domain audit rows than flag off
# ═════════════════════════════════════════════════════════════════════════════

def _charge_ids(pid):
    with db_session() as db:
        rows = db.execute(
            "SELECT id FROM encounter_charges WHERE patient_id = ? ORDER BY id", (pid,)
        ).fetchall()
    return [int(r["id"]) for r in rows]


def _complete(vid, uid, body):
    return client.post(f"/api/frontdesk/opd/visits/{vid}/complete", json=body,
                       headers=_auth(uid, "doctor"))


@pytest.mark.parametrize("mode", ["opd_on", "opd_off"])
def test_a_repeat_complete_that_raises_the_charge_is_named_on_the_audit_trail(
        staff, request, mode):
    """Complete without a charge, then again with one: the second call is a
    replay of the visit but a NEW billing write, so it needs its own row."""
    request.getfixturevalue(mode)
    pid = _outpatient()
    reg = _register(staff["doc_a"], "doctor", pid)
    assert reg.status_code == 200, reg.text
    vid = reg.json()["id"]

    first = _complete(vid, staff["doc_a"], {"raise_charge": False})
    assert first.status_code == 200, first.text
    assert _charge_ids(pid) == []

    second = _complete(vid, staff["doc_a"], {"raise_charge": True})
    assert second.status_code == 200, second.text
    charges = _charge_ids(pid)
    assert len(charges) == 1
    assert second.json()["consultation_charge"]["charge_id"] == charges[0]

    rows = _audit_rows("opd_visit_completed", pid)
    assert len(rows) == 2, "every successful call writes its row, in both modes"
    assert _meta(rows[0])["charge_id"] is None
    assert _meta(rows[1])["charge_id"] == charges[0]

    # A third call finds the charge by its idempotency key: no new charge,
    # still a row, still naming the one charge.
    third = _complete(vid, staff["doc_a"], {"raise_charge": True})
    assert third.status_code == 200, third.text
    assert _charge_ids(pid) == charges
    rows = _audit_rows("opd_visit_completed", pid)
    assert len(rows) == 3
    assert _meta(rows[2])["charge_id"] == charges[0]

    if mode == "opd_on":
        metas = [_meta(r) for r in rows]
        assert all(m["written"] == "in_transaction" for m in metas)
        assert [m["replay"] for m in metas] == [False, True, True]
        assert [m["charge_raised_now"] for m in metas] == [False, True, False]
    else:
        # The flag-off row is the old post-commit one, carrying no new keys.
        assert all("replay" not in _meta(r) for r in rows)


def test_a_repeat_complete_whose_audit_fails_rolls_the_new_charge_back(
        staff, opd_on, monkeypatch):
    import routes.frontdesk_routes as fr

    pid = _outpatient()
    vid = _register(staff["doc_a"], "doctor", pid).json()["id"]
    assert _complete(vid, staff["doc_a"], {"raise_charge": False}).status_code == 200

    real = fr.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "opd_visit_completed":
            raise AuditWriteError("simulated audit outage")
        return real(*args, **kwargs)

    monkeypatch.setattr(fr, "log_audit_event", failing)
    resp = _complete(vid, staff["doc_a"], {"raise_charge": True})
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "audit_unavailable"
    assert _charge_ids(pid) == [], "the charge survived an audit row that could not be written"
    with db_session() as db:
        linked = db.execute(
            "SELECT consultation_charge_id FROM opd_visits WHERE id = ?", (vid,)
        ).fetchone()[0]
    assert linked is None
    assert len(_audit_rows("opd_visit_completed", pid)) == 1


@pytest.mark.parametrize("mode", ["opd_on", "opd_off"])
def test_a_double_tap_call_in_and_a_repeat_cancel_still_write_their_rows(
        staff, request, mode):
    request.getfixturevalue(mode)
    hdr = _auth(staff["doc_a"], "doctor")

    seen = _outpatient()
    vid = _register(staff["doc_a"], "doctor", seen).json()["id"]
    for _ in range(2):
        resp = client.post(f"/api/frontdesk/opd/visits/{vid}/start", headers=hdr)
        assert resp.status_code == 200, resp.text
    started = _audit_rows("opd_consultation_started", seen)
    assert len(started) == 2

    left = _outpatient()
    vid2 = _register(staff["doc_a"], "doctor", left).json()["id"]
    for _ in range(2):
        resp = client.post(f"/api/frontdesk/opd/visits/{vid2}/cancel",
                           json={"reason": "Left without being seen"}, headers=hdr)
        assert resp.status_code == 200, resp.text
    cancelled = _audit_rows("opd_visit_cancelled", left)
    assert len(cancelled) == 2

    if mode == "opd_on":
        assert [_meta(r)["replay"] for r in started] == [False, True]
        assert [_meta(r)["replay"] for r in cancelled] == [False, True]
