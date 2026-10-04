"""The receptionist at the front desk (opd-3, W2-E, 3 October 2026).

permissions.require_registration_desk(mode) lets an account that holds
registration.read and registration.write, and nothing on the chart, work the OPD
queue: read the queue, one visit and the doctors list, register a visit and
cancel one. What this file defends:

  1. FLAG OFF, NOTHING CHANGED. With ORB_REGISTRATION off the five moved routes
     answer exactly as they did on require_frontdesk_read / _write: the same
     403 bodies, the same denial audit rows, the module switch in the same
     place. A receptionist account left over from a site that switched the
     feature off holds nothing.
  2. FLAG ON, THE DESK AND ONLY THE DESK. The receptionist reads the queue and
     the roster, registers and cancels; she cannot call a patient in, complete a
     consultation, transfer (a transfer moves who can read the chart), read any
     chart route, or touch money.
  3. EVERY REFUSAL IS AUDITED, on and off.
  4. ADMIN DOES NOT GAIN WRITING POWER. Admin keeps registering through
     frontdesk.write exactly as before, holds no registration.write and no
     opd.consult, and is refused on the OPD consultation surface.
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
from permissions import (
    FRONTDESK_WRITE,
    OPD_CONSULT,
    PATIENT_READ_ALL,
    PATIENT_WRITE,
    REGISTRATION_READ,
    REGISTRATION_WRITE,
    ROLE_PERMISSIONS,
    SCHEDULE_MANAGE,
    has_permission,
    may_use_registration_desk,
    registration_desk_arms,
    require_registration_desk,
)

client = TestClient(app)

DEPT = "Reception Test Cardiology"
OTHER_DEPT = "Reception Test Oncology"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department=None, *, name=None):
    uid = f"RC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"Reception {role} {uid[-4:]}", role, department),
        )
    return uid


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _hdr(role, department=None):
    return _auth(_mk_user(role, department), role)


def _mk_outpatient():
    pid = f"RCO{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status) "
            "VALUES (?, ?, ?, 41, 'F', 'discharged')",
            (pid, f"Reception Outpatient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}"),
        )
    return pid


def _mk_inpatient(department):
    pid = f"RCI{uuid.uuid4().hex[:8].upper()}"
    aid = f"RCA{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('RCADMITTER', 'Reception Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, "
            "admission_date) VALUES (?, ?, ?, '2', ?, 60, 'M', 'Reception fixture', "
            "'admitted', ?)",
            (pid, f"Reception Inpatient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}",
             f"{department} bay", now))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
            "status, created_at) VALUES (?, ?, 'RCADMITTER', ?, ?, '2', 'active', ?)",
            (aid, pid, department, f"{department} bay", now))
    return pid


@pytest.fixture(scope="module", autouse=True)
def _clinics():
    """Both clinics need clinical cover (an active doctor) or registration
    refuses them as departments nobody staffs."""
    for dept in (DEPT, OTHER_DEPT):
        _mk_user("doctor", dept, name=f"Dr Reception {dept[-8:]}")
    yield


@pytest.fixture
def desk_on(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    yield


@pytest.fixture
def desk_off(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    yield


def _register(headers, patient_id, department=None):
    body = {"patient_id": patient_id}
    if department:
        body["department"] = department
    return client.post("/api/frontdesk/opd/visits", json=body, headers=headers)


def _denials_since(marker_id, actor_id):
    with db_session() as db:
        rows = db.execute(
            "SELECT action_type, outcome, summary_message, metadata_json, event_category "
            "FROM audit_logs WHERE id > ? AND actor_user_id = ? "
            "AND action_type = 'permission_denied' ORDER BY id",
            (marker_id, actor_id)).fetchall()
    return [dict(r) for r in rows]


def _audit_marker():
    with db_session() as db:
        return db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0]


_OLD_READ_DENIAL = {
    "error": "permission_denied",
    "message": "Insufficient permissions",
    "required": ["patient.read_all"],
    "missing": ["patient.read_all"],
}
_OLD_WRITE_DENIAL = {
    "error": "permission_denied",
    "message": "Front-desk actions require patient.write or frontdesk.write",
    "required": ["patient.write", "frontdesk.write"],
}


# ── 1. The rule itself, every role, switch on and off ─────────────────────────

def _old_rule(role, mode):
    """The front-desk rule before 3 October 2026."""
    reads = has_permission(role, PATIENT_READ_ALL)
    if mode == "read":
        return reads
    return reads and (has_permission(role, PATIENT_WRITE) or has_permission(role, FRONTDESK_WRITE))


@pytest.mark.parametrize("role", sorted(ROLE_PERMISSIONS))
@pytest.mark.parametrize("mode", ["read", "write"])
def test_with_the_switch_off_every_role_gets_the_old_answer(role, mode):
    assert may_use_registration_desk(role, mode, desk_on=False) == _old_rule(role, mode)


@pytest.mark.parametrize("role", sorted(ROLE_PERMISSIONS))
@pytest.mark.parametrize("mode", ["read", "write"])
def test_with_the_switch_on_only_the_registration_arms_are_added(role, mode):
    expected = _old_rule(role, mode) or (
        has_permission(role, REGISTRATION_READ)
        and (mode == "read" or has_permission(role, REGISTRATION_WRITE)))
    assert may_use_registration_desk(role, mode, desk_on=True) == expected


def test_the_arms_are_the_designed_ones():
    assert registration_desk_arms("read", desk_on=True) == (PATIENT_READ_ALL, REGISTRATION_READ)
    assert registration_desk_arms("write", desk_on=True) == (
        FRONTDESK_WRITE, PATIENT_WRITE, REGISTRATION_WRITE)
    assert registration_desk_arms("read", desk_on=False) == (PATIENT_READ_ALL,)
    assert registration_desk_arms("write", desk_on=False) == (FRONTDESK_WRITE, PATIENT_WRITE)


def test_a_bad_mode_is_refused_at_import_time_not_at_request_time():
    with pytest.raises(ValueError):
        require_registration_desk("admin")
    with pytest.raises(ValueError):
        registration_desk_arms("everything", desk_on=True)


def test_the_receptionist_holds_the_desk_and_nothing_on_the_chart_or_the_till():
    perms = ROLE_PERMISSIONS["receptionist"]
    assert perms == {REGISTRATION_READ, REGISTRATION_WRITE}
    for chart_or_money in (PATIENT_READ_ALL, PATIENT_WRITE, FRONTDESK_WRITE, OPD_CONSULT,
                           "payments.read", "payments.record", "billing.read",
                           "consent.record", "relay.read"):
        assert chart_or_money not in perms


def test_admin_gains_no_writing_power():
    assert not has_permission("admin", REGISTRATION_WRITE)
    assert not has_permission("admin", OPD_CONSULT)
    assert has_permission("admin", REGISTRATION_READ)
    assert has_permission("admin", SCHEDULE_MANAGE)
    # Admin still registers visits, through frontdesk.write as before.
    assert may_use_registration_desk("admin", "write", desk_on=True)
    assert may_use_registration_desk("admin", "write", desk_on=False)


# ── 2. Switch off: byte for byte the old desk ─────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("get", "/api/frontdesk/opd/queue"),
    ("get", "/api/frontdesk/opd/doctors"),
    ("get", "/api/frontdesk/opd/visits/OPD-NOPE"),
])
def test_switch_off_a_receptionist_reads_nothing(desk_off, method, path):
    r = getattr(client, method)(path, headers=_hdr("receptionist"))
    assert r.status_code == 403
    assert r.json()["detail"] == _OLD_READ_DENIAL


def test_switch_off_a_receptionist_cannot_register_or_cancel(desk_off):
    headers = _hdr("receptionist")
    r = _register(headers, _mk_outpatient(), DEPT)
    assert r.status_code == 403 and r.json()["detail"] == _OLD_READ_DENIAL
    r = client.post("/api/frontdesk/opd/visits/OPD-NOPE/cancel", json={"reason": "x"},
                    headers=headers)
    assert r.status_code == 403 and r.json()["detail"] == _OLD_READ_DENIAL


def test_switch_off_the_pharmacist_refusal_and_its_audit_row_are_unchanged(desk_off):
    uid = _mk_user("pharmacist")
    marker = _audit_marker()
    r = client.get("/api/frontdesk/opd/queue", headers=_auth(uid, "pharmacist"))
    assert r.status_code == 403
    assert r.json()["detail"] == _OLD_READ_DENIAL
    [row] = _denials_since(marker, uid)
    assert row["event_category"] == "authorization"
    assert row["summary_message"] == "Missing permissions: patient.read_all"
    meta = json.loads(row["metadata_json"])
    assert meta == {"required_permissions": ["patient.read_all"],
                    "missing_permissions": ["patient.read_all"], "user_role": "pharmacist"}


def test_switch_off_a_nurse_write_refusal_is_the_front_desk_one(desk_off):
    uid = _mk_user("nurse", DEPT)
    marker = _audit_marker()
    r = _register(_auth(uid, "nurse"), _mk_outpatient())
    assert r.status_code == 403
    assert r.json()["detail"] == _OLD_WRITE_DENIAL
    [row] = _denials_since(marker, uid)
    assert row["summary_message"] == "Front-desk write requires patient.write or frontdesk.write"
    assert json.loads(row["metadata_json"]) == {
        "required_permissions": ["patient.write", "frontdesk.write"]}


def test_switch_off_the_module_switch_sits_where_it_always_did(monkeypatch):
    """Permission to read first, then the module's 404, then the write check."""
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", False)
    # A nurse may read, so she meets the 404 before any write refusal.
    assert _register(_hdr("nurse", DEPT), _mk_outpatient()).status_code == 404
    assert client.get("/api/frontdesk/opd/queue",
                      headers=_hdr("nurse", DEPT)).status_code == 404
    # A pharmacist may not read, so the 403 comes first, as before.
    r = client.get("/api/frontdesk/opd/queue", headers=_hdr("pharmacist"))
    assert r.status_code == 403 and r.json()["detail"] == _OLD_READ_DENIAL


def test_switch_off_the_clinicians_and_admin_work_as_before(desk_off):
    pid = _mk_outpatient()
    r = _register(_hdr("doctor", DEPT), pid)
    assert r.status_code == 200, r.text
    r = _register(_hdr("admin"), _mk_outpatient(), DEPT)
    assert r.status_code == 200, r.text
    assert client.get("/api/frontdesk/opd/queue", headers=_hdr("nurse", DEPT)).status_code == 200


# ── 3. Switch on: the receptionist works the desk ─────────────────────────────

def test_switch_on_a_central_receptionist_works_the_queue(desk_on):
    headers = _hdr("receptionist")  # no department: a central desk (OPD-N3)
    assert client.get("/api/frontdesk/opd/doctors", headers=headers).status_code == 200
    pid = _mk_outpatient()
    r = _register(headers, pid, DEPT)
    assert r.status_code == 200, r.text
    visit = r.json()
    assert visit["department"] == DEPT and visit["status"] == "waiting"

    q = client.get("/api/frontdesk/opd/queue", headers=headers)
    assert q.status_code == 200
    ids = [v["id"] for g in q.json()["queues"] for v in g["visits"]]
    assert visit["id"] in ids

    assert client.get(f"/api/frontdesk/opd/visits/{visit['id']}",
                      headers=headers).status_code == 200
    r = client.post(f"/api/frontdesk/opd/visits/{visit['id']}/cancel",
                    json={"reason": "Left without being seen"}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_switch_on_the_registration_is_audited_in_the_receptionists_name(desk_on):
    uid = _mk_user("receptionist")
    pid = _mk_outpatient()
    visit = _register(_auth(uid, "receptionist"), pid, DEPT).json()
    with db_session() as db:
        row = db.execute(
            "SELECT actor_role, patient_id FROM audit_logs WHERE action_type = "
            "'opd_visit_registered' AND target_id = ? ORDER BY id DESC LIMIT 1",
            (visit["id"],)).fetchone()
    assert row["actor_role"] == "receptionist" and row["patient_id"] == pid


def test_switch_on_the_receptionist_cannot_call_in_or_complete(desk_on):
    headers = _hdr("receptionist")
    visit = _register(_hdr("doctor", DEPT), _mk_outpatient()).json()
    for action in ("start", "complete"):
        r = client.post(f"/api/frontdesk/opd/visits/{visit['id']}/{action}", headers=headers)
        assert r.status_code == 403, action


def test_switch_on_the_receptionist_cannot_transfer(desk_on):
    headers = _hdr("receptionist")
    pid = _mk_inpatient(DEPT)
    assert client.get("/api/frontdesk/transfers/destinations",
                      headers=headers).status_code == 403
    r = client.post("/api/frontdesk/transfers", headers=headers, json={
        "patient_id": pid, "to_department": OTHER_DEPT, "reason": "Needs a bed"})
    assert r.status_code == 403
    assert client.get(f"/api/frontdesk/patients/{pid}/movements",
                      headers=headers).status_code in (403, 404)
    with db_session() as db:
        dept = db.execute("SELECT department FROM admissions WHERE patient_id = ?",
                          (pid,)).fetchone()[0]
    assert dept == DEPT


def test_switch_on_the_receptionist_reads_no_chart(desk_on):
    headers = _hdr("receptionist")
    pid = _mk_inpatient(DEPT)
    for path in ("/api/vigil/patients", f"/api/vigil/patients/{pid}"):
        r = client.get(path, headers=headers)
        assert r.status_code in (403, 404), (path, r.status_code)


def test_switch_on_the_receptionist_touches_no_money(desk_on, monkeypatch):
    monkeypatch.setattr(config, "PAYMENTS_ENABLED", True)
    headers = _hdr("receptionist")
    assert client.get("/api/payments/receipts", headers=headers).status_code == 403
    r = client.post("/api/payments/receipts", headers=headers, json={})
    assert r.status_code == 403


def test_switch_on_a_departmented_receptionist_stays_in_her_clinic(desk_on):
    headers = _hdr("receptionist", DEPT)
    # Her own clinic by default.
    r = _register(headers, _mk_outpatient())
    assert r.status_code == 200 and r.json()["department"] == DEPT
    # Not somebody else's clinic.
    r = _register(headers, _mk_outpatient(), OTHER_DEPT)
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "department_scope"
    # Not another ward's inpatient: 404, the patient's existence is not confirmed.
    r = _register(headers, _mk_inpatient(OTHER_DEPT))
    assert r.status_code == 404
    # Another clinic's visit reads as not found.
    other = _register(_hdr("doctor", OTHER_DEPT), _mk_outpatient()).json()
    assert client.get(f"/api/frontdesk/opd/visits/{other['id']}",
                      headers=headers).status_code == 404
    r = client.post(f"/api/frontdesk/opd/visits/{other['id']}/cancel",
                    json={"reason": "x"}, headers=headers)
    assert r.status_code == 404


def test_switch_on_a_role_with_no_arm_is_refused_and_audited(desk_on):
    uid = _mk_user("pharmacist")
    marker = _audit_marker()
    r = client.get("/api/frontdesk/opd/queue", headers=_auth(uid, "pharmacist"))
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "permission_denied"
    assert detail["required"] == [PATIENT_READ_ALL, REGISTRATION_READ]
    assert "registration desk" in detail["message"]
    [row] = _denials_since(marker, uid)
    meta = json.loads(row["metadata_json"])
    assert meta["required_any_of"] == [PATIENT_READ_ALL, REGISTRATION_READ]
    assert meta["desk_mode"] == "read" and meta["registration_feature"] is True


def test_switch_on_a_nurse_reads_the_queue_but_cannot_register(desk_on):
    uid = _mk_user("nurse", DEPT)
    assert client.get("/api/frontdesk/opd/queue",
                      headers=_auth(uid, "nurse")).status_code == 200
    marker = _audit_marker()
    r = _register(_auth(uid, "nurse"), _mk_outpatient())
    assert r.status_code == 403
    assert r.json()["detail"]["required"] == [FRONTDESK_WRITE, PATIENT_WRITE, REGISTRATION_WRITE]
    [row] = _denials_since(marker, uid)
    assert json.loads(row["metadata_json"])["desk_mode"] == "write"


def test_switch_on_cashier_and_lab_roles_are_refused(desk_on):
    for role in ("cashier", "lab_technician", "ward_clerk"):
        dept = DEPT if role == "ward_clerk" else None
        r = client.get("/api/frontdesk/opd/queue", headers=_hdr(role, dept))
        assert r.status_code == 403, role


def test_switch_on_admin_still_registers_and_is_refused_consultation(desk_on, monkeypatch):
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    admin = _hdr("admin")
    assert _register(admin, _mk_outpatient(), DEPT).status_code == 200
    # The OPD consultation surface needs opd.consult, which admin does not hold.
    assert client.get("/api/opd/meta", headers=admin).status_code == 403
    assert client.get("/api/opd/meta", headers=_hdr("doctor", DEPT)).status_code == 200


def test_a_receptionist_left_over_after_the_switch_goes_off_holds_nothing(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    uid = _mk_user("receptionist")
    headers = _auth(uid, "receptionist")
    assert client.get("/api/frontdesk/opd/queue", headers=headers).status_code == 200
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    assert client.get("/api/frontdesk/opd/queue", headers=headers).status_code == 403
    assert _register(headers, _mk_outpatient(), DEPT).status_code == 403


# ── 4. User Management creates one only while the feature is on ───────────────

def test_user_management_creates_a_receptionist_only_with_the_feature_on(monkeypatch):
    admin = _hdr("admin")
    body = lambda: {"id": f"RCNEW{uuid.uuid4().hex[:6].upper()}", "name": "New Receptionist",  # noqa: E731
                    "role": "receptionist", "password": "a-long-enough-password-1"}
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    r = client.post("/api/admin/users", json=body(), headers=admin)
    assert r.status_code == 400
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    r = client.post("/api/admin/users", json=body(), headers=admin)
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "receptionist" and r.json()["department"] is None
