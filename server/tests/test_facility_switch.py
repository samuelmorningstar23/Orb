"""Visiting consultants: one account, a session facility switch (multisite-19, W13-E).

Ruling MS-R7 (a), 3 October 2026: a consultant who works at two facilities of
the group keeps one account, each session works at one facility at a time,
and the switch is audited. Separate accounts stay the first go-live option, so
all of this sits behind ORB_MULTI_SITE and does nothing until a live
'visiting' user_facilities row exists.

A database this module owns (isolated_module_database) holds facility 1 with
Quorvane Medicine, a second facility with Halvenor Cardiology and a third with
Brisketh Surgery. Accounts: a doctor at facility 1 with a visiting row at the
second facility (the consultant), a facility 1 doctor with no visiting row, a
facility 1 nurse holding a visiting row (refused: doctors only), a facility 1
doctor whose visiting row names the third facility's department, and the
second facility's own doctor and nurse. One admitted patient at facility 1 and
one at the second facility.

Pinned:
  * a home token carries no 'fac' claim and reads exactly as before; with
    ORB_MULTI_SITE off both routes answer 404 and nothing is written;
  * a switch is refused without a live, valid access row (none, revoked, a
    department listed at another facility, a nurse, a shared tablet, the
    facility already active), each refusal audited, nothing changed;
  * a switch bumps token_version (every other session ends, its socket
    included), mints a 'fac' token, and commits with its security row (an
    unwritable row moves nothing); the session then reads the visiting
    department and facility, and only that facility's patients;
  * the claim is checked on every request: a revoked row, the switch turned
    off, or a department move that makes it the home facility is a 401;
  * socket rooms follow the active facility;
  * every PHI read audit names the facility (the patient's for a chart read,
    the session's for a board read) and the visiting session;
  * escalation pages the named attending who has visiting access at the
    patient's facility, and otherwise the patient's own department roster
    (home staff), exactly as before with the switch off;
  * with one facility on record nothing changes.

Test patients carry invented names. Run with the test env, never the shipping .env.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient

import config
import facility_fence as F
import main
import permissions
from auth import (
    _load_session_context,
    _visiting_session,
    authenticate_session_token,
    create_token,
    decode_token,
    hash_device_secret,
)
from database import db_session
from fastapi import HTTPException
from routes import auth_routes
from services import escalation_service as E
from services import facility_service as FS
from services.audit_service import AuditWriteError
from services.clinical_time import utc_iso_now
from ws import realtime

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9741                       # ids no other module writes
F3 = 9742
F2_CODE = f"VS{TAG[:4]}"
F3_CODE = f"VT{TAG[:4]}"
F1_DEPT = f"Quorvane Medicine {TAG}"
F2_DEPT = f"Halvenor Cardiology {TAG}"
F3_DEPT = f"Brisketh Surgery {TAG}"

P1 = f"VSP1{TAG}"
P2 = f"VSP2{TAG}"

client = TestClient(main.app, raise_server_exceptions=False)


# ── the world ────────────────────────────────────────────────────────────────

def _user(db, key: str, role: str, dept: str | None) -> str:
    uid = f"VS{key.upper().replace('_', '')}{TAG}"[:30]
    db.execute(
        "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, 'x')",
        (uid, f"Visitor {key}", role, dept),
    )
    return uid


def _visiting(db, uid: str, facility_id: int, dept: str) -> int:
    cur = db.execute(
        "INSERT INTO user_facilities (user_id, facility_id, kind, department, granted_by, "
        "granted_at, reason) VALUES (?, ?, 'visiting', ?, 'system', CURRENT_TIMESTAMP, "
        "'visiting consultant test')",
        (uid, facility_id, dept),
    )
    return int(cur.lastrowid)


@pytest.fixture(scope="module")
def world(isolated_module_database):
    now = utc_iso_now()
    with db_session() as db:
        for fid, code, name in ((F2, F2_CODE, "Halvenor Satellite"),
                                (F3, F3_CODE, "Brisketh Annexe")):
            db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                       (fid, str(uuid.uuid4()), code, name))
        for dept, fid in ((F1_DEPT, 1), (F2_DEPT, F2), (F3_DEPT, F3)):
            db.execute("INSERT INTO departments (name, facility_id, seeded_from) "
                       "VALUES (?, ?, 'test')", (dept, fid))
        u = {
            "consultant": _user(db, "consultant", "doctor", F1_DEPT),
            "no_row": _user(db, "no_row", "doctor", F1_DEPT),
            "nurse_v": _user(db, "nurse_v", "nurse", F1_DEPT),
            "mis_dept": _user(db, "mis_dept", "doctor", F1_DEPT),
            "f2_doctor": _user(db, "f2_doctor", "doctor", F2_DEPT),
            "f2_nurse": _user(db, "f2_nurse", "nurse", F2_DEPT),
        }
        rows = {
            "consultant": _visiting(db, u["consultant"], F2, F2_DEPT),
            "nurse_v": _visiting(db, u["nurse_v"], F2, F2_DEPT),
            # Names the third facility's department at the second facility.
            "mis_dept": _visiting(db, u["mis_dept"], F2, F3_DEPT),
        }
        for pid, name, dept, fid in ((P1, "Ostrava Pelliwick", F1_DEPT, 1),
                                     (P2, "Merivane Tolkasz", F2_DEPT, F2)):
            db.execute(
                "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status, "
                "registered_facility_id, attending_doctor_id) VALUES (?, ?, ?, 61, 'F', ?, "
                "'V-2', 'Quillan fever', 'admitted', ?, ?)",
                (pid, name, f"VSMRN{pid}", f"Ward {dept}", fid,
                 u["consultant"] if pid == P2 else None),
            )
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                "status, facility_id, created_at) VALUES (?, ?, 'system', ?, ?, 'V-2', "
                "'active', ?, ?)",
                (f"A{pid}", pid, dept, f"Ward {dept}", fid, now),
            )
            db.execute(
                "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
                "diastolic_bp, spo2, temperature, respiratory_rate, consciousness, "
                "carried_from) VALUES (?, ?, 84, 124, 78, 97, 36.9, 16, 'alert', NULL)",
                (pid, now),
            )
    FS.invalidate_cache()
    yield {"u": u, "rows": rows}
    with db_session() as db:
        db.executemany("DELETE FROM facilities WHERE id = ?", [(F2,), (F3,)])
    FS.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    # The blind route groups (Vigil here) as multisite-5b leaves them once every
    # screen is swept; /api/auth is always open.
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    FS.invalidate_cache()
    realtime._patient_dept_cache.clear()
    realtime._patient_facility_cache.clear()
    realtime._FACILITY_ROOMS.clear()
    yield
    FS.invalidate_cache()


@pytest.fixture()
def site_on(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)


@pytest.fixture()
def fresh_consultant(world):
    """The consultant with a live row, token_version reset, for one test."""
    uid = world["u"]["consultant"]
    with db_session() as db:
        db.execute("UPDATE user_facilities SET revoked_at = NULL, revoked_by = NULL "
                   "WHERE id = ?", (world["rows"]["consultant"],))
        db.execute("UPDATE users SET token_version = 0, department = ?, is_active = 1 "
                   "WHERE id = ?", (F1_DEPT, uid))
    FS.invalidate_cache()
    return uid


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _home_token(uid: str, role: str = "doctor") -> str:
    return create_token(uid, role)


def _tv(uid: str) -> int:
    with db_session() as db:
        return int(db.execute("SELECT COALESCE(token_version, 0) FROM users WHERE id = ?",
                              (uid,)).fetchone()[0])


def _audit(action: str, actor: str) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT event_category, outcome, patient_id, metadata_json FROM audit_logs "
            "WHERE action_type = ? AND actor_user_id = ? ORDER BY id",
            (action, actor),
        ).fetchall()
    return [{"category": r[0], "outcome": r[1], "patient_id": r[2],
             "meta": json.loads(r[3]) if r[3] else {}} for r in rows]


def _switch(token: str, facility_id):
    return client.post("/api/auth/facility", headers=_hdr(token),
                       json={"facility_id": facility_id})


def _switched_token(uid: str) -> str:
    resp = _switch(_home_token(uid), F2)
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ═══════════════════════════════════════════════════════════════════════════
# 1. Home sessions and the switch off: nothing changes
# ═══════════════════════════════════════════════════════════════════════════

def test_a_home_token_carries_no_claim_and_reads_as_before(world, fresh_consultant):
    token = _home_token(fresh_consultant)
    assert "fac" not in decode_token(token)
    user = authenticate_session_token(token)
    assert user["department"] == F1_DEPT
    for key in (FS.SESSION_FACILITY_KEY, FS.HOME_DEPARTMENT_KEY, FS.VISITING_ACCESS_KEY):
        assert key not in user
    assert FS.session_audit_fields(user) == {}
    assert permissions.facility_scope(user) == frozenset({1})
    row, device, portal = _load_session_context(fresh_consultant, None)
    assert row["id"] == fresh_consultant and device is None and portal is None


def test_with_the_switch_off_both_routes_answer_switched_off(world, fresh_consultant):
    token = _home_token(fresh_consultant)
    got = client.get("/api/auth/facilities", headers=_hdr(token))
    assert got.status_code == 404 and got.json()["detail"]["error"] == "feature_disabled"
    moved = _switch(token, F2)
    assert moved.status_code == 404 and moved.json()["detail"]["error"] == "feature_disabled"
    assert _tv(fresh_consultant) == 0
    assert _audit("session_facility_switched", fresh_consultant) == []
    assert client.get("/api/auth/facilities").status_code == 401
    assert client.post("/api/auth/facility", json={}).status_code == 401


def test_with_the_switch_off_a_visiting_token_is_refused(world, fresh_consultant):
    token = create_token(fresh_consultant, "doctor", facility_id=F2)
    assert decode_token(token)["fac"] == F2
    resp = client.get("/api/auth/me", headers=_hdr(token))
    assert resp.status_code == 401
    assert resp.json()["detail"] == FS.VISITING_ENDED_MESSAGE


# ═══════════════════════════════════════════════════════════════════════════
# 2. Where a session may work
# ═══════════════════════════════════════════════════════════════════════════

def test_the_options_list_home_first_then_the_live_visiting_row(world, site_on,
                                                                 fresh_consultant):
    resp = client.get("/api/auth/facilities", headers=_hdr(_home_token(fresh_consultant)))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["visiting"] is False and body["can_switch"] is True and body["reason"] is None
    assert body["active_facility"]["id"] == 1
    assert [(o["facility"]["id"], o["department"], o["kind"], o["active"])
            for o in body["options"]] == [(1, F1_DEPT, "home", True),
                                          (F2, F2_DEPT, "visiting", False)]
    assert body["options"][1]["facility"]["code"] == F2_CODE


def test_a_nurse_with_a_visiting_row_is_offered_nothing_and_refused(world, site_on):
    nurse = world["u"]["nurse_v"]
    token = _home_token(nurse, "nurse")
    body = client.get("/api/auth/facilities", headers=_hdr(token)).json()
    assert body["can_switch"] is False and "doctor" in body["reason"]
    assert [o["kind"] for o in body["options"]] == ["home"]
    resp = _switch(token, F2)
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "visiting_role"
    refused = _audit("session_facility_switch_refused", nurse)
    assert refused[-1]["category"] == "authorization"
    assert refused[-1]["meta"]["reason"] == "visiting_role"
    assert _tv(nurse) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3. A switch is refused without a live access row
# ═══════════════════════════════════════════════════════════════════════════

def test_a_switch_is_refused_without_a_live_access_row(world, site_on):
    uid = world["u"]["no_row"]
    resp = _switch(_home_token(uid), F2)
    assert resp.status_code == 404
    assert resp.json()["detail"] == {"error": "no_access",
                                     "message": auth_routes._SWITCH_WORDS["no_access"]}
    row = _audit("session_facility_switch_refused", uid)[-1]
    assert row["outcome"] == "denied"
    assert row["meta"]["requested_facility_id"] == F2 and row["meta"]["reason"] == "no_access"
    assert _tv(uid) == 0
    assert _audit("session_facility_switched", uid) == []


def test_a_row_naming_another_facilitys_department_is_no_access(world, site_on):
    uid = world["u"]["mis_dept"]
    body = client.get("/api/auth/facilities", headers=_hdr(_home_token(uid))).json()
    assert [o["kind"] for o in body["options"]] == ["home"]
    assert _switch(_home_token(uid), F2).status_code == 404
    with db_session() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        access = FS.visiting_access_on(db, uid, F2)
        assert FS.visiting_access_problem(row, access, F2, db=db) == "department_not_listed"


def test_a_revoked_row_cannot_be_switched_to(world, site_on, fresh_consultant):
    with db_session() as db:
        db.execute("UPDATE user_facilities SET revoked_at = CURRENT_TIMESTAMP WHERE id = ?",
                   (world["rows"]["consultant"],))
    resp = _switch(_home_token(fresh_consultant), F2)
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "no_access"
    assert _tv(fresh_consultant) == 0


def test_the_active_facility_is_refused_as_already_active(world, site_on, fresh_consultant):
    home = _switch(_home_token(fresh_consultant), None)
    assert home.status_code == 409 and home.json()["detail"]["error"] == "already_active"
    token = _switched_token(fresh_consultant)
    again = _switch(token, F2)
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_active"
    assert _tv(fresh_consultant) == 1


def test_a_shared_tablet_session_never_switches(world, site_on, fresh_consultant):
    device = f"VSDEV{TAG}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO enrolled_devices (id, label, department, secret_hash, "
                   "enrolled_by) VALUES (?, 'Halvenor bay tablet', ?, ?, NULL)",
                   (device, F1_DEPT, hash_device_secret(device)))
    token = create_token(fresh_consultant, "doctor", device_id=device, expires_in_minutes=15)
    body = client.get("/api/auth/facilities", headers=_hdr(token)).json()
    assert body["can_switch"] is False and "tablet" in body["reason"]
    resp = _switch(token, F2)
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "shared_device"
    assert _tv(fresh_consultant) == 0
    # A tablet token can never carry the claim, even one minted by hand.
    both = create_token(fresh_consultant, "doctor", device_id=device, facility_id=F2)
    assert client.get("/api/auth/me", headers=_hdr(both)).status_code == 401
    with pytest.raises(HTTPException) as err:
        _visiting_session({"id": fresh_consultant}, device,
                          {"access": {"id": 1, "facility_id": F2, "department": F2_DEPT},
                           "problem": None})
    assert err.value.status_code == 401


def test_a_claim_that_is_not_a_whole_number_is_a_bad_token(world, site_on, fresh_consultant):
    payload = decode_token(_home_token(fresh_consultant))
    for bad in ("9741", 0, True, None, 97.41):
        forged = jwt.encode({**payload, "fac": bad}, config.JWT_SECRET,
                            algorithm=config.JWT_ALGORITHM)
        with pytest.raises(HTTPException) as err:
            authenticate_session_token(forged)
        assert err.value.status_code == 401 and err.value.detail == "Invalid token payload"


# ═══════════════════════════════════════════════════════════════════════════
# 4. The switch itself
# ═══════════════════════════════════════════════════════════════════════════

def test_a_switch_moves_the_session_and_ends_every_other_one(world, site_on, fresh_consultant):
    old = _home_token(fresh_consultant)
    resp = _switch(old, F2)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["department"] == F2_DEPT
    assert body["user"]["facility"]["id"] == F2
    assert "orb_session" in resp.cookies
    new = body["token"]
    assert decode_token(new)["fac"] == F2
    assert decode_token(new)["tv"] == 1 == _tv(fresh_consultant)
    # Every other session of the account is over.
    assert client.get("/api/auth/me", headers=_hdr(old)).status_code == 401
    me = client.get("/api/auth/me", headers=_hdr(new)).json()
    assert me["department"] == F2_DEPT and me["facility"]["id"] == F2
    row = _audit("session_facility_switched", fresh_consultant)[-1]
    assert row["category"] == "security" and row["outcome"] == "success"
    assert row["meta"]["from_facility_id"] == 1 and row["meta"]["to_facility_id"] == F2
    assert row["meta"]["kind"] == "visiting"
    assert row["meta"]["user_facility_id"] == world["rows"]["consultant"]
    assert row["meta"]["department"] == F2_DEPT and row["meta"]["home_department"] == F1_DEPT
    assert row["meta"]["facility_id"] == F2


def test_the_visiting_session_reads_through_the_visiting_department(world, site_on,
                                                                    fresh_consultant):
    user = authenticate_session_token(_switched_token(fresh_consultant))
    assert user["department"] == F2_DEPT
    assert user[FS.HOME_DEPARTMENT_KEY] == F1_DEPT
    assert user[FS.SESSION_FACILITY_KEY] == F2
    assert user[FS.VISITING_ACCESS_KEY] == world["rows"]["consultant"]
    assert permissions.department_scope(user) == F2_DEPT
    assert permissions.facility_scope(user) == frozenset({F2})
    assert FS.user_facility(user)["id"] == F2


def test_the_switch_and_its_security_row_commit_together(world, site_on, fresh_consultant,
                                                         monkeypatch):
    real = auth_routes.log_audit_event

    def no_security_rows(*args, **kwargs):
        if kwargs.get("event_category") == "security":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(auth_routes, "log_audit_event", no_security_rows)
    old = _home_token(fresh_consultant)
    resp = _switch(old, F2)
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
    assert _tv(fresh_consultant) == 0
    assert client.get("/api/auth/me", headers=_hdr(old)).status_code == 200


def test_refresh_keeps_the_visiting_facility(world, site_on, fresh_consultant):
    token = _switched_token(fresh_consultant)
    resp = client.post("/api/auth/refresh", headers=_hdr(token))
    assert resp.status_code == 200, resp.text
    refreshed = decode_token(resp.json()["token"])
    assert refreshed["fac"] == F2 and refreshed["tv"] == 1
    assert resp.json()["user"]["department"] == F2_DEPT


def test_switching_home_drops_the_claim(world, site_on, fresh_consultant):
    visiting = _switched_token(fresh_consultant)
    resp = _switch(visiting, None)
    assert resp.status_code == 200, resp.text
    home = resp.json()["token"]
    assert "fac" not in decode_token(home)
    assert resp.json()["user"]["department"] == F1_DEPT
    assert client.get("/api/auth/me", headers=_hdr(visiting)).status_code == 401
    row = _audit("session_facility_switched", fresh_consultant)[-1]
    assert row["meta"]["kind"] == "home"
    assert row["meta"]["from_facility_id"] == F2 and row["meta"]["to_facility_id"] == 1
    assert row["meta"]["user_facility_id"] is None
    # Naming the home facility's id works the same as null.
    again = _switch(home, F2)
    assert again.status_code == 200
    assert _switch(again.json()["token"], 1).status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# 5. The claim is checked on every request
# ═══════════════════════════════════════════════════════════════════════════

def test_a_revoked_row_ends_the_session_at_the_next_request(world, site_on, fresh_consultant):
    token = _switched_token(fresh_consultant)
    assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 200
    assert realtime._authenticate({"token": token}) is not None
    with db_session() as db:
        db.execute("UPDATE user_facilities SET revoked_at = CURRENT_TIMESTAMP, revoked_by = "
                   "'system', revoke_reason = 'test' WHERE id = ?",
                   (world["rows"]["consultant"],))
    # No cache to wait for: the row is read on every request.
    resp = client.get("/api/auth/me", headers=_hdr(token))
    assert resp.status_code == 401 and resp.json()["detail"] == FS.VISITING_ENDED_MESSAGE
    assert client.post("/api/auth/refresh", headers=_hdr(token)).status_code == 401
    assert client.get("/api/vigil/patients", headers=_hdr(token)).status_code == 401
    assert realtime._authenticate({"token": token}) is None


def test_turning_the_switch_off_ends_a_visiting_session(world, site_on, fresh_consultant,
                                                        monkeypatch):
    token = _switched_token(fresh_consultant)
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", False)
    assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 401


def test_a_department_move_to_the_visiting_facility_ends_the_session(world, site_on,
                                                                    fresh_consultant):
    token = _switched_token(fresh_consultant)
    try:
        with db_session() as db:
            db.execute("UPDATE users SET department = ? WHERE id = ?",
                       (F2_DEPT, fresh_consultant))
            row = db.execute("SELECT * FROM users WHERE id = ?", (fresh_consultant,)).fetchone()
            access = FS.visiting_access_on(db, fresh_consultant, F2)
            FS.invalidate_cache()
            assert FS.visiting_access_problem(row, access, F2, db=db) == "home_facility"
        assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 401
    finally:
        with db_session() as db:
            db.execute("UPDATE users SET department = ? WHERE id = ?",
                       (F1_DEPT, fresh_consultant))
        FS.invalidate_cache()


def test_a_deactivated_account_or_closed_facility_ends_the_session(world, site_on,
                                                                   fresh_consultant):
    token = _switched_token(fresh_consultant)
    with db_session() as db:
        db.execute("UPDATE facilities SET is_active = 0 WHERE id = ?", (F2,))
    try:
        assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 401
    finally:
        with db_session() as db:
            db.execute("UPDATE facilities SET is_active = 1 WHERE id = ?", (F2,))
    assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 200
    with db_session() as db:
        db.execute("UPDATE departments SET is_active = 0 WHERE name = ?", (F2_DEPT,))
    try:
        assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 401
    finally:
        with db_session() as db:
            db.execute("UPDATE departments SET is_active = 1 WHERE name = ?", (F2_DEPT,))


# ═══════════════════════════════════════════════════════════════════════════
# 6. What a visiting session sees, and what its audit rows say
# ═══════════════════════════════════════════════════════════════════════════

def test_a_visiting_session_sees_the_visiting_facility_only(world, site_on, fresh_consultant):
    token = _switched_token(fresh_consultant)
    board = client.get("/api/vigil/patients", headers=_hdr(token))
    assert board.status_code == 200, board.text
    assert {r["id"] for r in board.json()} == {P2}
    assert client.get(f"/api/vigil/patients/{P2}", headers=_hdr(token)).status_code == 200
    assert client.get(f"/api/vigil/patients/{P1}", headers=_hdr(token)).status_code == 404
    denied = [r for r in _audit("patient_scope_denied", fresh_consultant)
              if r["patient_id"] == P1][-1]
    assert denied["meta"]["scope_cause"] == "facility"
    assert denied["meta"]["facility_scope"] == [F2]
    assert denied["meta"][FS.SESSION_FACILITY_KEY] == F2
    assert denied["meta"][FS.HOME_DEPARTMENT_KEY] == F1_DEPT
    # A home session of the same account sees its own facility again.
    home = _switch(token, None).json()["token"]
    assert {r["id"] for r in client.get("/api/vigil/patients", headers=_hdr(home)).json()} \
        == {P1}


def test_every_phi_read_audit_names_the_facility(world, site_on, fresh_consultant):
    token = _switched_token(fresh_consultant)
    assert client.get(f"/api/vigil/patients/{P2}", headers=_hdr(token)).status_code == 200
    chart = [r for r in _audit("patient_chart_viewed", fresh_consultant)
             if r["patient_id"] == P2][-1]
    assert chart["category"] == "phi_access"
    assert chart["meta"]["facility_id"] == F2
    assert chart["meta"]["facility_scope"] == [F2]
    assert chart["meta"][FS.SESSION_FACILITY_KEY] == F2
    assert chart["meta"][FS.VISITING_ACCESS_KEY] == world["rows"]["consultant"]
    assert chart["meta"][FS.HOME_DEPARTMENT_KEY] == F1_DEPT
    assert chart["meta"]["department"] == F2_DEPT
    # A read naming no patient is filed under the session's facility, not the
    # account's own.
    assert client.get("/api/vigil/patients", headers=_hdr(token)).status_code == 200
    board = _audit("patient_board_viewed", fresh_consultant)[-1]
    assert board["patient_id"] is None
    assert board["meta"]["facility_id"] == F2
    assert board["meta"][FS.SESSION_FACILITY_KEY] == F2
    # A home session's rows carry none of the session keys.
    home = _switch(token, None).json()["token"]
    assert client.get("/api/vigil/patients", headers=_hdr(home)).status_code == 200
    home_row = _audit("patient_board_viewed", fresh_consultant)[-1]
    assert home_row["meta"]["facility_id"] == 1
    assert FS.SESSION_FACILITY_KEY not in home_row["meta"]
    assert FS.HOME_DEPARTMENT_KEY not in home_row["meta"]


# ═══════════════════════════════════════════════════════════════════════════
# 7. Socket rooms follow the active facility
# ═══════════════════════════════════════════════════════════════════════════

def _rooms(monkeypatch, token: str, uid: str) -> set:
    rooms: set = set()
    sid = f"sid-{uid}-{uuid.uuid4().hex[:6]}"

    async def save_session(_sid, session):
        return None

    async def enter_room(_sid, room):
        rooms.add(room)

    monkeypatch.setattr(realtime.sio, "save_session", save_session)
    monkeypatch.setattr(realtime.sio, "enter_room", enter_room)
    try:
        assert asyncio.run(realtime.connect(sid, {}, {"token": token})) is True
    finally:
        realtime.sio.session_principals.pop(sid, None)
    return rooms


def test_socket_rooms_follow_the_active_facility(world, site_on, fresh_consultant,
                                                 monkeypatch):
    home = _home_token(fresh_consultant)
    assert _rooms(monkeypatch, home, fresh_consultant) == {f"user:{fresh_consultant}",
                                                           f"dept:{F1_DEPT}"}
    # The home socket, connected before the switch.
    sid = f"sid-old-{fresh_consultant}"
    realtime.sio.session_principals[sid] = realtime._authenticate({"token": home})
    dropped: list = []

    async def disconnect(_sid, *args, **kwargs):
        dropped.append(_sid)

    monkeypatch.setattr(realtime.sio, "disconnect", disconnect)
    resp = _switch(home, F2)
    assert resp.status_code == 200
    visiting = resp.json()["token"]
    try:
        assert asyncio.run(realtime.sio._fresh(sid)) is None
        assert dropped == [sid]
    finally:
        realtime.sio.session_principals.pop(sid, None)
    sock = realtime._authenticate({"token": visiting})
    assert sock["department"] == F2_DEPT
    assert sock[realtime.FACILITY_SCOPE_KEY] == frozenset({F2})
    assert sock[realtime.MULTI_FACILITY_KEY] is True
    assert _rooms(monkeypatch, visiting, fresh_consultant) == {f"user:{fresh_consultant}",
                                                               f"dept:{F2_DEPT}"}


# ═══════════════════════════════════════════════════════════════════════════
# 8. Escalation: home staff plus the named attending
# ═══════════════════════════════════════════════════════════════════════════

def _consultant_tier(patient: str, dept: str, attending: str | None):
    with db_session() as db:
        return E._derive_target(db, {"id": patient, "department": dept, "attending": attending},
                                "consultant", "Consultant", None)


def test_escalation_pages_the_named_attending_with_visiting_access(world, site_on,
                                                                   fresh_consultant):
    target, words = _consultant_tier(P2, F2_DEPT, fresh_consultant)
    assert target == fresh_consultant and "named attending" in words
    with db_session() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (fresh_consultant,)).fetchone()
        assert FS.reachable_facilities_for(row, db=db) == frozenset({1, F2})
        assert FS.facility_scope_for(row, db=db) == frozenset({1})


def test_escalation_falls_to_home_staff_without_live_visiting_access(world, fresh_consultant,
                                                                    monkeypatch):
    # Switch off: exactly the wave 12 rule, the patient's own department roster.
    target, _ = _consultant_tier(P2, F2_DEPT, fresh_consultant)
    assert target == world["u"]["f2_doctor"]
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    with db_session() as db:
        db.execute("UPDATE user_facilities SET revoked_at = CURRENT_TIMESTAMP WHERE id = ?",
                   (world["rows"]["consultant"],))
    target, _ = _consultant_tier(P2, F2_DEPT, fresh_consultant)
    assert target == world["u"]["f2_doctor"]
    # No attending named: home staff of the patient's department.
    assert _consultant_tier(P2, F2_DEPT, None)[0] == world["u"]["f2_doctor"]


def test_a_nurse_with_a_visiting_row_is_not_paged_across_facilities(world, site_on):
    nurse = world["u"]["nurse_v"]
    with db_session() as db:
        target, _ = E._derive_target(db, {"id": P2, "department": F2_DEPT, "attending": None},
                                     "nurse", "Nurse", nurse)
        row = db.execute("SELECT * FROM users WHERE id = ?", (nurse,)).fetchone()
        assert FS.reachable_facilities_for(row, db=db) == FS.facility_scope_for(row, db=db)
    assert target == world["u"]["f2_nurse"]


# ═══════════════════════════════════════════════════════════════════════════
# 9. One facility on record: nothing changes
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def one_facility(world):
    with db_session() as db:
        db.executemany("DELETE FROM facilities WHERE id = ?", [(F2,), (F3,)])
    FS.invalidate_cache()
    try:
        yield
    finally:
        with db_session() as db:
            for fid, code, name in ((F2, F2_CODE, "Halvenor Satellite"),
                                    (F3, F3_CODE, "Brisketh Annexe")):
                db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                           (fid, str(uuid.uuid4()), code, name))
        FS.invalidate_cache()


def test_with_one_facility_on_record_nothing_changes(world, site_on, fresh_consultant,
                                                     one_facility):
    token = _home_token(fresh_consultant)
    body = client.get("/api/auth/facilities", headers=_hdr(token)).json()
    assert [o["kind"] for o in body["options"]] == ["home"] and body["can_switch"] is False
    assert _switch(token, F2).status_code == 404
    forged = create_token(fresh_consultant, "doctor", facility_id=F2)
    assert client.get("/api/auth/me", headers=_hdr(forged)).status_code == 401
    user = authenticate_session_token(token)
    assert permissions.facility_scope(user) is None
    with db_session() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (fresh_consultant,)).fetchone()
        assert FS.reachable_facilities_for(row, db=db) is None
        assert FS.visiting_access_problem(row, FS.visiting_access_on(db, fresh_consultant, F2),
                                          F2, db=db) == "single_facility"
