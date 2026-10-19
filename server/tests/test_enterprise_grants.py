"""Facility users, enterprise grants and their console tools (multisite-6, W9-H).

Samuel ruled on 3 October 2026 that one Orb appliance can serve the facilities
of one hospital group (multi-site M1), that isolation follows the data (M2),
and that group-wide access is a named, audited grant held only by an
administrator (MS-R6 (a)). This file builds a two-facility world by SQL and
pins User Management's half of that:

  * only an administrator can hold a grant, and only a holder can give, list
    or revoke one; grant and revoke each write a fail-closed security row in
    the same transaction (an unwritable audit row saves nothing);
  * a revoke narrows the account's scope on its very next request: the scope
    cache is forgotten after the commit (the clock is frozen here, so nothing
    else could refresh it);
  * a home facility row is refused for an account with a department; a
    department-less account an administrator creates is placed at their own
    facility, its home row and security row in the account's transaction;
  * an F2 administrator can neither create nor change an account at F1, and
    every such refusal writes an authorization row;
  * a move is a revoke plus an insert;
  * Orb's system accounts are refused;
  * with one facility and ORB_MULTI_SITE off, the list and the create answer
    exactly as before and no facility row is written;
  * scripts/grant_enterprise_access.py (the first grant, at the console) and
    create_admin.py --facility.

The runtime fence (W7-J) answers 503 on /api/admin while the blind list is the
sentinel '*' and two facilities exist; the HTTP tests open it, as multisite-5b
will once the screens are swept.

TEST RULE (contracts, W7-J): the second facility is removed in teardown and
facility_service.invalidate_cache() called, or every later test's patient routes
answer 503.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import facility_fence as F
from auth import create_token, hash_password
from database import db_session
from main import app
from routes import admin_users_routes as R
from services import facility_service as FS
from services.audit_service import AuditWriteError

client = TestClient(app)

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9281                       # an id no other module writes
F2_CODE = f"EG{TAG}"
DEPT_F1 = f"EG Medicine {TAG}"
DEPT_F2 = f"EG Cardiology {TAG}"
PREFIX = f"EG{TAG}"

_made_users: list[str] = []


# ── the world ────────────────────────────────────────────────────────────────

def _mk_user(db, key, role, department=None):
    uid = f"{PREFIX}{key.upper()}"[:60]
    db.execute(
        "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
        (uid, f"Grants {key}", role, department, hash_password("password")))
    _made_users.append(uid)
    return uid


def _put_back_facility_two():
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Grants Facility Two"))
    FS.invalidate_cache()


def _home(db, uid, fid):
    db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, "
               "reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, 'test')", (uid, fid))


def _grant(db, uid):
    db.execute("INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
               "VALUES (?, 'system', CURRENT_TIMESTAMP, 'test')", (uid,))


@pytest.fixture(scope="module")
def world():
    users = {}
    with db_session() as db:
        db.execute("INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
                   (DEPT_F1,))
        db.execute("INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
                   (DEPT_F2, F2))
        users["holder"] = _mk_user(db, "holder", "admin")
        users["admin_f1"] = _mk_user(db, "adminf1", "admin")
        users["admin_f2"] = _mk_user(db, "adminf2", "admin")
        users["nurse_f1"] = _mk_user(db, "nursef1", "nurse", DEPT_F1)
        users["nurse_f2"] = _mk_user(db, "nursef2", "nurse", DEPT_F2)
        users["pharm_f1"] = _mk_user(db, "pharmf1", "pharmacist")
        users["pharm_f2"] = _mk_user(db, "pharmf2", "pharmacist")
        _home(db, users["admin_f2"], F2)
        _home(db, users["pharm_f2"], F2)
        _grant(db, users["holder"])
    _put_back_facility_two()
    try:
        yield users
    finally:
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
            db.executemany("DELETE FROM departments WHERE name = ?", [(DEPT_F1,), (DEPT_F2,)])
            db.executemany("DELETE FROM user_facilities WHERE user_id = ?",
                           [(u,) for u in _made_users])
            db.executemany("DELETE FROM enterprise_access_grants WHERE user_id = ?",
                           [(u,) for u in _made_users])
            db.executemany("UPDATE users SET is_active = 0 WHERE id = ?",
                           [(u,) for u in _made_users])
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh_cache():
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


@pytest.fixture(autouse=True)
def fence_open(monkeypatch):
    """The blind list as multisite-5b leaves it once every screen is swept."""
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


@pytest.fixture()
def site_on(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)


@pytest.fixture()
def one_facility(world):
    """Facility two taken off record for one test, then put back."""
    with db_session() as db:
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        yield
    finally:
        _put_back_facility_two()


@pytest.fixture()
def frozen_clock(monkeypatch):
    """No cache entry expires: only invalidate_cache() can refresh one."""
    monkeypatch.setattr(FS, "_clock", lambda: 5_000_000.0)


@pytest.fixture()
def fresh_admin(world):
    """An active F1 administrator with no grant, made for one test."""
    with db_session() as db:
        uid = _mk_user(db, f"adm{uuid.uuid4().hex[:5]}", "admin")
    return uid


def _row(uid) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def _auth(uid) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, _row(uid)['role'])}"}


def _last_audit_id() -> int:
    with db_session() as db:
        return int(db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0])


def _audits(after: int, action_type: str) -> list[dict]:
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE id > ? AND action_type = ? ORDER BY id",
            (after, action_type)).fetchall()]
    for row in rows:
        row["meta"] = json.loads(row["metadata_json"] or "{}")
    return rows


def _home_rows(uid) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM user_facilities WHERE user_id = ? ORDER BY id", (uid,)).fetchall()]


def _live_grants(uid) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM enterprise_access_grants WHERE user_id = ? AND revoked_at IS NULL",
            (uid,)).fetchall()]


def _new_id(label: str) -> str:
    uid = f"{PREFIX}{label}{uuid.uuid4().hex[:4].upper()}"
    _made_users.append(uid)
    return uid


def _create(actor, **body):
    body.setdefault("name", "Grants New")
    body.setdefault("password", "longpassword1")
    return client.post("/api/admin/users", headers=_auth(actor), json=body)


def _failing_security_audit(monkeypatch):
    """Every security row in admin_users_routes fails to persist."""
    real = R.log_audit_event

    def flaky(*args, **kwargs):
        if kwargs.get("event_category") == "security":
            raise AuditWriteError("audit store unavailable (test)")
        return real(*args, **kwargs)
    monkeypatch.setattr(R, "log_audit_event", flaky)


# ═════════════════════════════════════════════════════════════════════════════
#  1. One facility, ORB_MULTI_SITE off: exactly as before
# ═════════════════════════════════════════════════════════════════════════════

def test_the_switch_ships_off():
    assert config.feature_enabled("multi_site") is False


def test_the_list_keeps_its_shape_with_one_facility_and_the_switch_off(world, one_facility):
    body = client.get("/api/admin/users", headers=_auth(world["admin_f1"])).json()
    assert set(body) == {"users", "departments"}
    for u in body["users"]:
        assert "facility" not in u and "enterprise_access" not in u
    ids = {u["id"] for u in body["users"]}
    assert world["nurse_f2"] in ids and world["pharm_f2"] in ids   # nobody is filtered


def test_create_writes_no_facility_row_with_one_facility_and_the_switch_off(world, one_facility):
    uid = _new_id("PH")
    resp = _create(world["admin_f1"], id=uid, role="pharmacist")
    assert resp.status_code == 201, resp.text
    assert "facility" not in resp.json()
    assert _home_rows(uid) == []


def test_a_facility_other_than_one_is_refused_with_the_switch_off(world, one_facility):
    uid = _new_id("PH")
    resp = _create(world["admin_f1"], id=uid, role="pharmacist", facility_id=F2)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "multi_site_off"
    assert _home_rows(uid) == []


def test_the_move_and_grant_routes_are_absent_with_the_switch_off(world):
    resp = client.post(f"/api/admin/users/{world['pharm_f1']}/facility",
                       headers=_auth(world["holder"]), json={"facility_id": F2})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "feature_disabled"
    resp = client.post("/api/admin/users/enterprise-grants", headers=_auth(world["holder"]),
                       json={"user_id": world["admin_f1"], "reason": "Group CIO"})
    assert resp.status_code == 404
    assert _live_grants(world["admin_f1"]) == []


# ═════════════════════════════════════════════════════════════════════════════
#  2. Enterprise grants
# ═════════════════════════════════════════════════════════════════════════════

def test_a_non_holder_cannot_grant_list_or_revoke(world, site_on, fresh_admin):
    before = _last_audit_id()
    for actor in (world["admin_f1"], world["admin_f2"]):
        resp = client.post("/api/admin/users/enterprise-grants", headers=_auth(actor),
                           json={"user_id": fresh_admin, "reason": "Group CIO"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "enterprise_grant_required"
        assert client.get("/api/admin/users/enterprise-grants",
                          headers=_auth(actor)).status_code == 403
    holder_grant = _live_grants(world["holder"])[0]["id"]
    resp = client.post(f"/api/admin/users/enterprise-grants/{holder_grant}/revoke",
                       headers=_auth(world["admin_f2"]), json={"reason": "not mine"})
    assert resp.status_code == 403
    assert _live_grants(fresh_admin) == []
    assert len(_live_grants(world["holder"])) == 1
    denials = _audits(before, "enterprise_access_denied")
    assert len(denials) == 5
    assert {d["event_category"] for d in denials} == {"authorization"}
    assert {d["outcome"] for d in denials} == {"denied"}


def test_a_non_admin_cannot_reach_the_grant_routes(world, site_on):
    for actor in (world["nurse_f1"], world["pharm_f1"]):
        assert client.get("/api/admin/users/enterprise-grants",
                          headers=_auth(actor)).status_code == 403
        assert client.post("/api/admin/users/enterprise-grants", headers=_auth(actor),
                           json={"user_id": actor, "reason": "Group CIO"}).status_code == 403
    assert _live_grants(world["pharm_f1"]) == []


def test_only_an_admin_can_hold_a_grant(world, site_on):
    before = _last_audit_id()
    for target in (world["pharm_f1"], world["nurse_f1"]):
        resp = client.post("/api/admin/users/enterprise-grants", headers=_auth(world["holder"]),
                           json={"user_id": target, "reason": "Central pharmacy"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "enterprise_grant_admin_only"
        assert _live_grants(target) == []
    rows = _audits(before, "enterprise_access_denied")
    assert [r["meta"]["target_role"] for r in rows] == ["pharmacist", "nurse"]


@pytest.mark.parametrize("target", ["system", "bot_vigil", "bot_newcomer"])
def test_service_accounts_are_refused(world, site_on, target):
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('bot_vigil', 'bot_vigil', 'doctor', 'system')")
    resp = client.post("/api/admin/users/enterprise-grants", headers=_auth(world["holder"]),
                       json={"user_id": target, "reason": "Group CIO"})
    assert resp.status_code == 409
    assert _live_grants(target) == []


def test_a_service_account_cannot_be_moved(world, site_on):
    resp = client.post("/api/admin/users/bot_vigil/facility", headers=_auth(world["holder"]),
                       json={"facility_id": F2})
    assert resp.status_code == 409
    assert _home_rows("bot_vigil") == []


def test_grant_and_revoke_write_security_rows_and_a_revoke_narrows_the_next_request(
        world, site_on, fresh_admin, frozen_clock):
    holder = world["holder"]
    before = _last_audit_id()
    resp = client.post("/api/admin/users/enterprise-grants", headers=_auth(holder),
                       json={"user_id": fresh_admin, "reason": "  Group CIO  "})
    assert resp.status_code == 201, resp.text
    grant = resp.json()
    assert grant["user_id"] == fresh_admin and grant["live"] is True
    assert grant["reason"] == "Group CIO" and grant["granted_by"] == holder
    granted = _audits(before, "enterprise_access_granted")
    assert len(granted) == 1
    assert granted[0]["event_category"] == "security"
    assert granted[0]["actor_user_id"] == holder and granted[0]["target_id"] == fresh_admin
    assert granted[0]["meta"] == {"grant_id": grant["id"], "reason": "Group CIO"}

    # Group-wide on the next request: the new admin lists F2's staff too.
    ids = {u["id"] for u in client.get("/api/admin/users",
                                       headers=_auth(fresh_admin)).json()["users"]}
    assert world["nurse_f2"] in ids and world["nurse_f1"] in ids
    assert FS.facility_scope_for(_row(fresh_admin)) is None

    # A second live grant is refused.
    again = client.post("/api/admin/users/enterprise-grants", headers=_auth(holder),
                        json={"user_id": fresh_admin, "reason": "Twice"})
    assert again.status_code == 409

    # The holder lists it.
    listed = client.get("/api/admin/users/enterprise-grants", headers=_auth(holder)).json()
    assert any(g["id"] == grant["id"] and g["live"] for g in listed["grants"])

    mid = _last_audit_id()
    resp = client.post(f"/api/admin/users/enterprise-grants/{grant['id']}/revoke",
                       headers=_auth(holder), json={"reason": "Left the group office"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["live"] is False and resp.json()["revoked_by"] == holder
    revoked = _audits(mid, "enterprise_access_revoked")
    assert len(revoked) == 1 and revoked[0]["event_category"] == "security"
    assert revoked[0]["meta"] == {"grant_id": grant["id"], "reason": "Left the group office"}

    # The clock is frozen, so only the invalidation could have refreshed the
    # cached maps: the very next request is facility-bound again.
    assert FS.facility_scope_for(_row(fresh_admin)) == frozenset({1})
    ids = {u["id"] for u in client.get("/api/admin/users",
                                       headers=_auth(fresh_admin)).json()["users"]}
    assert world["nurse_f2"] not in ids and world["nurse_f1"] in ids

    # Revoking twice is refused.
    assert client.post(f"/api/admin/users/enterprise-grants/{grant['id']}/revoke",
                       headers=_auth(holder), json={"reason": "again"}).status_code == 409


def test_a_grant_is_fail_closed(world, site_on, fresh_admin, monkeypatch):
    _failing_security_audit(monkeypatch)
    resp = client.post("/api/admin/users/enterprise-grants", headers=_auth(world["holder"]),
                       json={"user_id": fresh_admin, "reason": "Group CIO"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "audit_unavailable"
    assert _live_grants(fresh_admin) == []


def test_a_revoke_is_fail_closed(world, site_on, fresh_admin, monkeypatch):
    with db_session() as db:
        _grant(db, fresh_admin)
    grant_id = _live_grants(fresh_admin)[0]["id"]
    _failing_security_audit(monkeypatch)
    resp = client.post(f"/api/admin/users/enterprise-grants/{grant_id}/revoke",
                       headers=_auth(world["holder"]), json={"reason": "Left"})
    assert resp.status_code == 503
    assert len(_live_grants(fresh_admin)) == 1


def test_a_revoke_works_with_the_switch_off(world, fresh_admin):
    assert config.feature_enabled("multi_site") is False
    with db_session() as db:
        _grant(db, fresh_admin)
    grant_id = _live_grants(fresh_admin)[0]["id"]
    resp = client.post(f"/api/admin/users/enterprise-grants/{grant_id}/revoke",
                       headers=_auth(world["holder"]), json={"reason": "Switch is off"})
    assert resp.status_code == 200
    assert _live_grants(fresh_admin) == []


def test_a_grant_needs_a_reason_and_an_active_account(world, site_on, fresh_admin):
    resp = client.post("/api/admin/users/enterprise-grants", headers=_auth(world["holder"]),
                       json={"user_id": fresh_admin, "reason": " a "})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "reason_required"
    with db_session() as db:
        db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (fresh_admin,))
    resp = client.post("/api/admin/users/enterprise-grants", headers=_auth(world["holder"]),
                       json={"user_id": fresh_admin, "reason": "Group CIO"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "account_inactive"
    assert _live_grants(fresh_admin) == []


def test_demoting_an_admin_revokes_the_grant(world, site_on, fresh_admin):
    with db_session() as db:
        _grant(db, fresh_admin)
    before = _last_audit_id()
    resp = client.patch(f"/api/admin/users/{fresh_admin}", headers=_auth(world["holder"]),
                        json={"role": "cashier"})
    assert resp.status_code == 200, resp.text
    assert _live_grants(fresh_admin) == []
    rows = _audits(before, "enterprise_access_revoked")
    assert len(rows) == 1 and rows[0]["event_category"] == "security"
    assert "cashier" in rows[0]["meta"]["reason"]


@pytest.mark.parametrize("facilities", ["one", "two"])
def test_only_a_holder_can_change_a_holders_account(world, fresh_admin, facilities, request):
    """Otherwise any administrator could reset a holder's password and take
    group-wide access over. Held with one facility on record too."""
    if facilities == "one":
        request.getfixturevalue("one_facility")
    with db_session() as db:
        _grant(db, fresh_admin)
    hdr = _auth(world["admin_f1"])
    before = _last_audit_id()
    assert client.post(f"/api/admin/users/{fresh_admin}/reset-password", headers=hdr,
                       json={"new_password": "longpassword4"}).status_code == 403
    assert client.post(f"/api/admin/users/{fresh_admin}/deactivate", headers=hdr).status_code == 403
    assert client.post(f"/api/admin/users/{fresh_admin}/pin", headers=hdr,
                       json={"pin": "2468"}).status_code == 403
    resp = client.patch(f"/api/admin/users/{fresh_admin}", headers=hdr, json={"role": "cashier"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "enterprise_grant_required"
    assert _row(fresh_admin)["role"] == "admin" and _row(fresh_admin)["is_active"] == 1
    assert len(_live_grants(fresh_admin)) == 1
    rows = _audits(before, "enterprise_access_denied")
    assert len(rows) == 4 and all(r["meta"]["target_holds_grant"] for r in rows)
    # A holder may.
    assert client.post(f"/api/admin/users/{fresh_admin}/reset-password",
                       headers=_auth(world["holder"]),
                       json={"new_password": "longpassword5"}).status_code == 200


def test_a_non_holder_cannot_move_a_holder(world, site_on, fresh_admin):
    with db_session() as db:
        _grant(db, fresh_admin)
    resp = client.post(f"/api/admin/users/{fresh_admin}/facility",
                       headers=_auth(world["admin_f1"]), json={"facility_id": 1})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "enterprise_grant_required"
    assert _home_rows(fresh_admin) == []


def test_the_list_shows_grants_to_a_holder_only(world, site_on):
    holder_view = client.get("/api/admin/users", headers=_auth(world["holder"])).json()
    assert holder_view["grants_manageable"] is True
    me = next(u for u in holder_view["users"] if u["id"] == world["holder"])
    assert me["enterprise_access"] is True and me["enterprise_grant_id"]
    other = client.get("/api/admin/users", headers=_auth(world["admin_f1"])).json()
    assert other["grants_manageable"] is False
    assert all("enterprise_access" not in u for u in other["users"])


# ═════════════════════════════════════════════════════════════════════════════
#  3. Home facility rows, scope refusals and moves
# ═════════════════════════════════════════════════════════════════════════════

def test_a_home_row_for_a_nurse_is_refused(world, site_on):
    uid = _new_id("NU")
    resp = _create(world["holder"], id=uid, role="nurse", department=DEPT_F2, facility_id=F2)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "home_facility_refused"
    assert _home_rows(uid) == []
    resp = client.post(f"/api/admin/users/{world['nurse_f1']}/facility",
                       headers=_auth(world["holder"]), json={"facility_id": F2})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "home_facility_refused"
    assert _home_rows(world["nurse_f1"]) == []


def test_an_f2_admin_cannot_create_a_user_at_f1(world, site_on):
    before = _last_audit_id()
    uid = _new_id("PH")
    resp = _create(world["admin_f2"], id=uid, role="pharmacist", facility_id=1)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "facility_scope"
    nurse = _new_id("NU")
    resp = _create(world["admin_f2"], id=nurse, role="nurse", department=DEPT_F1)
    assert resp.status_code == 403
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM users WHERE id IN (?, ?)",
                          (uid, nurse)).fetchone()[0] == 0
    rows = _audits(before, "user_outside_facility_scope")
    assert len(rows) == 2
    assert {r["event_category"] for r in rows} == {"authorization"}
    assert rows[0]["meta"]["facility_id"] == 1 and rows[0]["meta"]["facility_scope"] == [F2]


def test_an_f2_admin_places_department_less_staff_at_f2(world, site_on):
    before = _last_audit_id()
    uid = _new_id("CA")
    resp = _create(world["admin_f2"], id=uid, role="cashier")
    assert resp.status_code == 201, resp.text
    assert resp.json()["facility"]["id"] == F2
    rows = _home_rows(uid)
    assert len(rows) == 1
    assert rows[0]["facility_id"] == F2 and rows[0]["kind"] == "home"
    assert rows[0]["granted_by"] == world["admin_f2"] and rows[0]["revoked_at"] is None
    assigned = _audits(before, "user_facility_assigned")
    assert len(assigned) == 1 and assigned[0]["event_category"] == "security"
    assert assigned[0]["meta"]["facility_id"] == F2
    assert FS.facility_scope_for(_row(uid)) == frozenset({F2})
    # A nurse in an F2 department is created with no home row.
    nurse = _new_id("NU")
    resp = _create(world["admin_f2"], id=nurse, role="nurse", department=DEPT_F2)
    assert resp.status_code == 201, resp.text
    assert _home_rows(nurse) == []
    assert resp.json()["facility"]["id"] == F2


def test_a_create_with_a_home_row_is_fail_closed(world, site_on, monkeypatch):
    _failing_security_audit(monkeypatch)
    uid = _new_id("CA")
    resp = _create(world["holder"], id=uid, role="cashier", facility_id=F2)
    assert resp.status_code == 503
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM users WHERE id = ?", (uid,)).fetchone()[0] == 0
    assert _home_rows(uid) == []


def test_a_closed_or_unknown_facility_is_refused(world, site_on):
    uid = _new_id("CA")
    assert _create(world["holder"], id=uid, role="cashier",
                   facility_id=987654).json()["detail"]["error"] == "facility_not_found"
    with db_session() as db:
        db.execute("UPDATE facilities SET is_active = 0 WHERE id = ?", (F2,))
    try:
        resp = _create(world["holder"], id=uid, role="cashier", facility_id=F2)
        assert resp.json()["detail"]["error"] == "facility_inactive"
        resp = client.post(f"/api/admin/users/{world['pharm_f1']}/facility",
                           headers=_auth(world["holder"]), json={"facility_id": F2})
        assert resp.json()["detail"]["error"] == "facility_inactive"
    finally:
        with db_session() as db:
            db.execute("UPDATE facilities SET is_active = 1 WHERE id = ?", (F2,))
        FS.invalidate_cache()
    assert _home_rows(uid) == [] and _home_rows(world["pharm_f1"]) == []


def test_a_move_is_a_revoke_plus_an_insert(world, site_on, frozen_clock):
    with db_session() as db:
        uid = _mk_user(db, f"mv{uuid.uuid4().hex[:5]}", "pharmacist")
        _home(db, uid, F2)
    assert FS.facility_scope_for(_row(uid)) == frozenset({F2})
    before = _last_audit_id()
    resp = client.post(f"/api/admin/users/{uid}/facility", headers=_auth(world["holder"]),
                       json={"facility_id": 1, "reason": "Joined the main pharmacy"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"] is True and resp.json()["facility"]["id"] == 1
    rows = _home_rows(uid)
    assert len(rows) == 2
    old, new = rows
    assert old["facility_id"] == F2 and old["revoked_at"] is not None
    assert old["revoked_by"] == world["holder"]
    assert old["revoke_reason"] == "Joined the main pharmacy"
    assert new["facility_id"] == 1 and new["revoked_at"] is None
    assert [r["action_type"] for r in _audits(before, "user_facility_revoked")
            + _audits(before, "user_facility_assigned")] == [
        "user_facility_revoked", "user_facility_assigned"]
    # The clock is frozen: only the invalidation refreshed the maps.
    assert FS.facility_scope_for(_row(uid)) == frozenset({1})
    # Moving to where it already is changes nothing.
    resp = client.post(f"/api/admin/users/{uid}/facility", headers=_auth(world["holder"]),
                       json={"facility_id": 1})
    assert resp.json()["moved"] is False
    assert len(_home_rows(uid)) == 2


def test_a_move_is_fail_closed(world, site_on, monkeypatch):
    with db_session() as db:
        uid = _mk_user(db, f"mv{uuid.uuid4().hex[:5]}", "cashier")
    _failing_security_audit(monkeypatch)
    resp = client.post(f"/api/admin/users/{uid}/facility", headers=_auth(world["holder"]),
                       json={"facility_id": F2})
    assert resp.status_code == 503
    assert _home_rows(uid) == []


def test_an_f2_admin_cannot_move_staff_in_or_out_of_f1(world, site_on):
    before = _last_audit_id()
    resp = client.post(f"/api/admin/users/{world['pharm_f1']}/facility",
                       headers=_auth(world["admin_f2"]), json={"facility_id": F2})
    assert resp.status_code == 403
    resp = client.post(f"/api/admin/users/{world['pharm_f2']}/facility",
                       headers=_auth(world["admin_f2"]), json={"facility_id": 1})
    assert resp.status_code == 403
    assert _home_rows(world["pharm_f1"]) == []
    assert [r["facility_id"] for r in _home_rows(world["pharm_f2"])
            if r["revoked_at"] is None] == [F2]
    rows = _audits(before, "user_outside_facility_scope")
    assert len(rows) == 2
    assert rows[1]["meta"]["from_facility_id"] == F2 and rows[1]["meta"]["to_facility_id"] == 1


def test_an_f2_admin_cannot_change_an_f1_account(world):
    """Isolation follows the data: these refusals hold with the switch off."""
    hdr = _auth(world["admin_f2"])
    target = world["nurse_f1"]
    before = _last_audit_id()
    assert client.post(f"/api/admin/users/{target}/reset-password", headers=hdr,
                       json={"new_password": "longpassword2"}).status_code == 403
    assert client.post(f"/api/admin/users/{target}/deactivate", headers=hdr).status_code == 403
    assert client.post(f"/api/admin/users/{target}/pin", headers=hdr,
                       json={"pin": "4321"}).status_code == 403
    assert client.patch(f"/api/admin/users/{target}", headers=hdr,
                        json={"department": DEPT_F2}).status_code == 403
    assert _row(target)["is_active"] == 1 and _row(target)["department"] == DEPT_F1
    assert len(_audits(before, "user_outside_facility_scope")) == 4
    # Its own facility's accounts it changes as before.
    assert client.post(f"/api/admin/users/{world['pharm_f2']}/reset-password", headers=hdr,
                       json={"new_password": "longpassword3"}).status_code == 200


def test_an_f2_admin_cannot_move_its_nurse_into_an_f1_department(world):
    resp = client.patch(f"/api/admin/users/{world['nurse_f2']}",
                        headers=_auth(world["admin_f2"]), json={"department": DEPT_F1})
    assert resp.status_code == 403
    assert _row(world["nurse_f2"])["department"] == DEPT_F2


def test_the_list_is_bound_to_the_admins_facilities(world, site_on):
    body = client.get("/api/admin/users", headers=_auth(world["admin_f2"])).json()
    by_id = {u["id"]: u for u in body["users"]}
    assert world["nurse_f2"] in by_id and world["pharm_f2"] in by_id
    assert world["nurse_f1"] not in by_id and world["pharm_f1"] not in by_id
    assert by_id[world["nurse_f2"]]["facility"]["id"] == F2
    assert by_id[world["nurse_f2"]]["facility"]["code"] == F2_CODE
    assert [f["id"] for f in body["facilities"]] == [F2]
    assert body["department_facilities"] == {DEPT_F2: F2}
    assert body["own_facility_id"] == F2
    assert DEPT_F1 not in body["departments"]
    # The group-wide holder sees both facilities and every department.
    whole = client.get("/api/admin/users", headers=_auth(world["holder"])).json()
    ids = {u["id"] for u in whole["users"]}
    assert {world["nurse_f1"], world["nurse_f2"]} <= ids
    assert {1, F2} <= {f["id"] for f in whole["facilities"]}
    assert whole["department_facilities"][DEPT_F1] == 1


def test_giving_a_department_revokes_the_home_row(world, site_on):
    with db_session() as db:
        uid = _mk_user(db, f"pd{uuid.uuid4().hex[:5]}", "pharmacist")
        _home(db, uid, F2)
    before = _last_audit_id()
    resp = client.patch(f"/api/admin/users/{uid}", headers=_auth(world["holder"]),
                        json={"role": "nurse", "department": DEPT_F2})
    assert resp.status_code == 200, resp.text
    assert [r for r in _home_rows(uid) if r["revoked_at"] is None] == []
    assert len(_audits(before, "user_facility_revoked")) == 1
    assert FS.facility_scope_for(_row(uid)) == frozenset({F2})


def test_removing_a_department_keeps_the_account_at_its_facility(world, site_on):
    with db_session() as db:
        uid = _mk_user(db, f"rd{uuid.uuid4().hex[:5]}", "nurse", DEPT_F2)
    resp = client.patch(f"/api/admin/users/{uid}", headers=_auth(world["admin_f2"]),
                        json={"role": "pharmacist", "department": ""})
    assert resp.status_code == 200, resp.text
    live = [r for r in _home_rows(uid) if r["revoked_at"] is None]
    assert [r["facility_id"] for r in live] == [F2]
    assert FS.facility_scope_for(_row(uid)) == frozenset({F2})
    assert resp.json()["facility"]["id"] == F2


# ═════════════════════════════════════════════════════════════════════════════
#  4. The console tools
# ═════════════════════════════════════════════════════════════════════════════

def test_the_grant_script_grants_and_revokes_with_security_rows(world, monkeypatch, fresh_admin):
    from scripts import grant_enterprise_access as G
    with pytest.raises(G.GrantRefused, match="switched off"):
        G.grant(fresh_admin, "Group CIO")
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    before = _last_audit_id()
    done = G.grant(fresh_admin, "Group CIO")
    live = _live_grants(fresh_admin)
    assert len(live) == 1 and live[0]["id"] == done["grant_id"]
    assert live[0]["granted_by"] == "console" and live[0]["reason"] == "Group CIO"
    rows = _audits(before, "enterprise_access_granted")
    assert len(rows) == 1 and rows[0]["event_category"] == "security"
    assert rows[0]["actor_role"] == "console" and rows[0]["target_id"] == fresh_admin
    assert rows[0]["meta"]["script"] == "grant_enterprise_access.py"
    assert FS.facility_scope_for(_row(fresh_admin)) is None
    assert any(g["user_id"] == fresh_admin for g in G.list_grants())

    with pytest.raises(G.GrantRefused, match="already"):
        G.grant(fresh_admin, "Again")
    G.revoke(fresh_admin, "Left the group")
    assert _live_grants(fresh_admin) == []
    assert len(_audits(before, "enterprise_access_revoked")) == 1
    assert FS.facility_scope_for(_row(fresh_admin)) == frozenset({1})


def test_the_grant_script_refuses_what_the_screen_refuses(world, monkeypatch):
    from scripts import grant_enterprise_access as G
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    for target, words in ((world["pharm_f1"], "only for administrators"),
                          ("system", "system accounts"), ("bot_vigil", "system accounts"),
                          (f"{PREFIX}NOBODY", "no account")):
        with pytest.raises(G.GrantRefused, match=words):
            G.grant(target, "Group CIO")
    with pytest.raises(G.GrantRefused, match="reason"):
        G.grant(world["admin_f1"], "ab")
    assert _live_grants(world["pharm_f1"]) == [] and _live_grants(world["admin_f1"]) == []


def test_the_grant_script_needs_a_console_and_a_typed_confirmation(world, monkeypatch, fresh_admin):
    from scripts import grant_enterprise_access as G
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    monkeypatch.setattr(G.sys.stdin, "isatty", lambda: False, raising=False)
    assert G.main(["--grant", fresh_admin, "--reason", "Group CIO"]) == 2
    assert _live_grants(fresh_admin) == []
    monkeypatch.setattr(G.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "WRONG")
    assert G.main(["--grant", fresh_admin, "--reason", "Group CIO"]) == 1
    assert _live_grants(fresh_admin) == []
    monkeypatch.setattr("builtins.input", lambda prompt="": fresh_admin)
    assert G.main(["--grant", fresh_admin, "--reason", "Group CIO"]) == 0
    assert len(_live_grants(fresh_admin)) == 1
    # A revoke may skip the typed confirmation with --yes, even with no console.
    monkeypatch.setattr(G.sys.stdin, "isatty", lambda: False, raising=False)
    assert G.main(["--revoke", fresh_admin, "--reason", "Left", "--yes"]) == 0
    assert _live_grants(fresh_admin) == []


def test_the_grant_script_is_fail_closed(world, monkeypatch, fresh_admin):
    from scripts import grant_enterprise_access as G
    from services import audit_service
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)

    def fail(*args, **kwargs):
        raise AuditWriteError("audit store unavailable (test)")
    monkeypatch.setattr(audit_service, "log_audit_event", fail)
    with pytest.raises(AuditWriteError):
        G.grant(fresh_admin, "Group CIO")
    assert _live_grants(fresh_admin) == []


def test_create_admin_resolves_its_facility(world, monkeypatch):
    from scripts import create_admin as C
    with db_session() as db:
        assert C.resolve_facility(db, None, None) == (None, None)
        assert C.resolve_facility(db, "main", None) == (None, None)
        assert "switched off" in C.resolve_facility(db, F2_CODE, None)[1]
        monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
        assert C.resolve_facility(db, F2_CODE.lower(), None) == (F2, None)
        assert "department" in C.resolve_facility(db, F2_CODE, "Cardiology")[1]
        assert "no facility" in C.resolve_facility(db, "NOPE99", None)[1]


def test_create_admin_writes_the_home_row_with_its_security_row(world):
    from scripts import create_admin as C
    uid = _new_id("CADM")
    before = _last_audit_id()
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Console Admin', "
                   "'admin', 'x')", (uid,))
        C._write_home_row(db, uid, F2, F2_CODE)
    FS.invalidate_cache()
    rows = _home_rows(uid)
    assert [(r["facility_id"], r["granted_by"]) for r in rows] == [(F2, "console")]
    audit = _audits(before, "user_facility_assigned")
    assert len(audit) == 1 and audit[0]["event_category"] == "security"
    assert audit[0]["actor_role"] == "console"
    assert FS.facility_scope_for(_row(uid)) == frozenset({F2})
