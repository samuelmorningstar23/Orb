"""A ward clerk account can be made on an Orb Ward Record computer.

The ward clerk is the person Orb Ward Record is sold around: she copies each
observation round from the paper sheet (SALES_PLAN_2026-10.md section 3, step
2). Her role's own switch is beds and admissions (roles.ROLE_FEATURE 'adt'),
and User Management refused to create her while ORB_ADT was off, even with
ORB_WARD_RECORD on, so a Ward Record site could not staff the one role it is
sold for. roles.ROLE_FEATURE_ALSO now names the profile as a second switch:
either one on is enough. With both off the refusal stands, in words that name
both.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import roles
from auth import create_token, hash_password
from database import db_session
from main import app

client = TestClient(app)

BOTH_OFF = ("Beds and admissions and Orb Ward Record are both switched off on this "
            "site. Ward clerks need Beds and admissions or Orb Ward Record switched on.")


def _mk(role, department=None):
    uid = f"WS{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, department, hash_password("password")))
    return uid


@pytest.fixture()
def admin_hdr():
    uid = _mk("admin")
    return {"Authorization": f"Bearer {create_token(uid, 'admin')}"}


def _create(hdr, role="ward_clerk", department="Cardiology"):
    uid = f"WS{uuid.uuid4().hex[:6].upper()}"
    resp = client.post("/api/admin/users", headers=hdr, json={
        "id": uid, "name": f"New {role}", "role": role, "department": department,
        "password": "password1"})
    return uid, resp


def _stored_role(uid):
    with db_session() as db:
        row = db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
    return row["role"] if row else None


def test_the_profile_is_a_second_switch_for_the_ward_clerk_only():
    assert roles.ROLE_FEATURE_ALSO == {"ward_clerk": "ward_record"}
    assert roles.role_switches("ward_clerk") == ("adt", "ward_record")
    assert roles.role_switches("lab_technician") == ("lab",)
    assert roles.role_switches("nurse") == ()
    for switch in roles.ROLE_FEATURE_ALSO.values():
        assert switch in config.PROFILE_SWITCHES


def test_switch_enabled_reads_the_profile_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "WARD_RECORD", False)
    assert config.switch_enabled("ward_record") is False
    monkeypatch.setattr(config, "WARD_RECORD", True)
    assert config.switch_enabled("ward_record") is True


def test_a_ward_clerk_is_created_with_beds_off_and_ward_record_on(monkeypatch, admin_hdr):
    monkeypatch.setattr(config, "WARD_RECORD", True)
    assert not config.switch_enabled("adt")
    uid, resp = _create(admin_hdr)
    assert resp.status_code == 201, resp.text
    assert _stored_role(uid) == "ward_clerk"


def test_a_ward_clerk_is_refused_with_both_off(monkeypatch, admin_hdr):
    monkeypatch.setattr(config, "WARD_RECORD", False)
    assert not config.switch_enabled("adt")
    uid, resp = _create(admin_hdr)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == BOTH_OFF
    assert _stored_role(uid) is None


def test_moving_a_nurse_into_ward_clerk_follows_the_same_rule(monkeypatch, admin_hdr):
    nurse = _mk("nurse", "Cardiology")
    monkeypatch.setattr(config, "WARD_RECORD", False)
    resp = client.patch(f"/api/admin/users/{nurse}", headers=admin_hdr, json={"role": "ward_clerk"})
    assert resp.status_code == 400 and resp.json()["detail"] == BOTH_OFF
    monkeypatch.setattr(config, "WARD_RECORD", True)
    resp = client.patch(f"/api/admin/users/{nurse}", headers=admin_hdr, json={"role": "ward_clerk"})
    assert resp.status_code == 200, resp.text
    assert _stored_role(nurse) == "ward_clerk"


def test_the_profile_does_not_unlock_the_lab_or_the_desk(monkeypatch, admin_hdr):
    monkeypatch.setattr(config, "WARD_RECORD", True)
    for role, dept in (("lab_technician", None), ("receptionist", None)):
        uid, resp = _create(admin_hdr, role, dept)
        assert resp.status_code == 400, (role, resp.text)
        assert _stored_role(uid) is None
