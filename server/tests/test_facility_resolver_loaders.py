"""Every user loader gets the same facility scope (multisite-5a, W8-K).

Six places build the user dict straight from SELECT * FROM users, each in its
own way: auth._load_session_context (every REST request), the password login,
auth_routes._load_user_row (MFA enrolment), the PIN login on a ward tablet,
admin_users_routes._get_user_row, and the socket handshake
(ws/realtime._authenticate). The design critique's point: a scope that leans
on a field only one loader joins in would read as "no home row" from the other
five. facility_service.facility_scope_for therefore reads ONLY the id, role
and department, plus in-process maps, and this file pins that:

  * a recording mapping proves the resolver (and the facility label) touch no
    other key;
  * for every account of a two-facility world, the dict from each loader that
    can be called directly gives the same scope as the bare
    {id, role, department};
  * the password login, the PIN login, the session refresh and /api/auth/me
    return the same facility in UserOut.

TEST RULE (contracts, W7-J): the second facility is removed in teardown and
facility_service.invalidate_cache() called. Every facility id this module
writes is put back.

Run with the test env, never the shipping .env.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient

from auth import _load_session_context, create_token, hash_password
from database import db_session
from main import app
from routes import admin_users_routes, auth_routes
from services import facility_service as FS
from ws import realtime

client = TestClient(app)

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9202                      # an id no other module writes
F2_CODE = f"FL{TAG}"
DEPT_ONE = f"FL General Medicine {TAG}"
DEPT_TWO = f"FL Cardiology {TAG}"
DEPT_UNKNOWN = f"FL Nowhere {TAG}"
PASSWORD = "loader-test-pass"
PIN = "4826"
DEVICE_LABEL = f"TEST-facility loaders {TAG}"

#: account key: (role, department, home facility row, enterprise grant, expected scope)
ACCOUNTS = {
    "nurse_f1": ("nurse", DEPT_ONE, None, False, frozenset({1})),
    "nurse_f2": ("nurse", DEPT_TWO, None, False, frozenset({F2})),
    "nurse_unknown": ("nurse", DEPT_UNKNOWN, None, False, frozenset()),
    "pharm_f2": ("pharmacist", None, F2, False, frozenset({F2})),
    "cashier_f1": ("cashier", None, None, False, frozenset({1})),
    "admin_f2": ("admin", None, F2, False, frozenset({F2})),
    "admin_ent": ("admin", None, None, True, None),
}

#: The facility each account works at (UserOut.facility.id).
EXPECTED_FACILITY = {"nurse_f1": 1, "nurse_f2": F2, "nurse_unknown": None, "pharm_f2": F2,
                     "cashier_f1": 1, "admin_f2": F2, "admin_ent": 1}


@pytest.fixture(scope="module")
def world():
    users = {}
    with db_session() as db:
        for name, fid in ((DEPT_ONE, 1), (DEPT_TWO, F2)):
            db.execute("INSERT INTO departments (name, facility_id, seeded_from) "
                       "VALUES (?, ?, 'test')", (name, fid))
        for key, (role, dept, home, grant, _) in ACCOUNTS.items():
            uid = f"FLD{uuid.uuid4().hex[:8].upper()}"
            db.execute(
                "INSERT INTO users (id, name, role, department, password_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, f"Loader {key}", role, dept, hash_password(PASSWORD)))
            if home is not None:
                db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                           "granted_at, reason) VALUES (?, ?, 'home', 'system', "
                           "CURRENT_TIMESTAMP, 'test')", (uid, home))
            if grant:
                db.execute("INSERT INTO enterprise_access_grants (user_id, granted_by, "
                           "granted_at, reason) VALUES (?, 'system', CURRENT_TIMESTAMP, 'test')",
                           (uid,))
            users[key] = uid
        db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Loader Facility Two"))
    FS.invalidate_cache()
    try:
        yield users
    finally:
        uids = list(users.values())
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
            db.executemany("DELETE FROM departments WHERE name = ?",
                           [(d,) for d in (DEPT_ONE, DEPT_TWO)])
            db.executemany("DELETE FROM user_facilities WHERE user_id = ?", [(u,) for u in uids])
            db.executemany("DELETE FROM enterprise_access_grants WHERE user_id = ?",
                           [(u,) for u in uids])
            try:
                db.execute("DELETE FROM enrolled_devices WHERE label = ?", (DEVICE_LABEL,))
            except Exception:
                pass
            db.executemany("UPDATE users SET is_active = 0, device_pin_hash = NULL WHERE id = ?",
                           [(u,) for u in uids])
        auth_routes.LOGIN_ATTEMPTS.clear()
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh_cache():
    FS.invalidate_cache()
    auth_routes.LOGIN_ATTEMPTS.clear()
    yield
    FS.invalidate_cache()
    auth_routes.LOGIN_ATTEMPTS.clear()


def _token(uid: str) -> str:
    with db_session() as db:
        row = db.execute("SELECT role, token_version FROM users WHERE id = ?", (uid,)).fetchone()
    return create_token(uid, row["role"], token_version=row["token_version"] or 0)


def _loaders(uid: str) -> dict[str, dict]:
    """The user dict each directly callable loader builds."""
    row, _device, _portal = _load_session_context(uid, None)
    with db_session() as db:
        admin_row = admin_users_routes._get_user_row(db, uid)
    sock = realtime._authenticate({"token": _token(uid)})
    assert sock is not None, "the socket handshake refused a valid token"
    return {
        "auth._load_session_context": dict(row),
        "auth_routes._load_user_row": dict(auth_routes._load_user_row(uid)),
        "admin_users_routes._get_user_row": dict(admin_row),
        "realtime._authenticate": dict(sock),
    }


class RecordingUser(Mapping):
    """A user dict that records every key read from it."""

    def __init__(self, data: dict):
        self._data = dict(data)
        self.read: set[str] = set()

    def __getitem__(self, key):
        self.read.add(key)
        return self._data[key]

    def get(self, key, default=None):
        self.read.add(key)
        return self._data.get(key, default)

    def __iter__(self):
        raise AssertionError("the resolver iterated over the whole user row")

    def __len__(self):
        return len(self._data)


# ── 1. The resolver reads only id, role and department ──────────────────────

def test_the_resolver_and_the_label_read_only_id_role_and_department(world):
    for key, uid in world.items():
        full = _loaders(uid)["auth._load_session_context"]
        recorded = RecordingUser(full)
        assert FS.facility_scope_for(recorded) == ACCOUNTS[key][4], key
        assert recorded.read <= {"id", "role", "department"}, (key, recorded.read)
        recorded = RecordingUser(full)
        assert FS.user_facility_id(recorded) == EXPECTED_FACILITY[key], key
        assert recorded.read <= {"id", "role", "department"}, (key, recorded.read)


def test_the_bare_three_fields_give_the_full_rows_answer(world):
    for key, uid in world.items():
        full = _loaders(uid)["auth._load_session_context"]
        bare = {k: full[k] for k in ("id", "role", "department")}
        assert FS.facility_scope_for(bare) == FS.facility_scope_for(full) == ACCOUNTS[key][4]


# ── 2. Every loader agrees ───────────────────────────────────────────────────

def test_every_directly_callable_loader_gives_the_same_scope(world):
    for key, uid in world.items():
        scopes = {name: FS.facility_scope_for(user) for name, user in _loaders(uid).items()}
        assert set(scopes.values()) == {ACCOUNTS[key][4]}, (key, scopes)


def test_a_sqlite_row_from_a_loader_resolves_like_its_dict(world):
    with db_session() as db:
        for key, uid in world.items():
            row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            assert FS.facility_scope_for(row) == ACCOUNTS[key][4], key


def test_the_password_login_returns_the_facility(world):
    for key, uid in world.items():
        auth_routes.LOGIN_ATTEMPTS.clear()
        resp = client.post("/api/auth/login", json={"employee_id": uid, "password": PASSWORD})
        assert resp.status_code == 200, (key, resp.text)
        facility = resp.json()["user"]["facility"]
        expected = EXPECTED_FACILITY[key]
        assert (facility or {}).get("id") == expected, (key, facility)
        if expected == F2:
            assert facility == {"id": F2, "code": F2_CODE, "name": "Loader Facility Two"}


def test_the_session_lanes_return_the_same_facility(world):
    for key, uid in world.items():
        hdr = {"Authorization": f"Bearer {_token(uid)}"}
        me = client.get("/api/auth/me", headers=hdr)
        assert me.status_code == 200, me.text
        refreshed = client.post("/api/auth/refresh", headers=hdr)
        assert refreshed.status_code == 200, refreshed.text
        got = {(me.json()["facility"] or {}).get("id"),
               (refreshed.json()["user"]["facility"] or {}).get("id")}
        assert got == {EXPECTED_FACILITY[key]}, (key, got)


def test_the_pin_login_returns_the_facility(world):
    from services import pin_security
    admin = {"Authorization": f"Bearer {_token(world['admin_f2'])}"}
    enrol = client.post("/api/auth/devices", json={"label": DEVICE_LABEL, "department": DEPT_TWO},
                        headers=admin)
    assert enrol.status_code == 201, enrol.text
    secret = enrol.json()["device_secret"]
    uid = world["nurse_f2"]
    with db_session() as db:
        db.execute("UPDATE users SET device_pin_hash = ? WHERE id = ?",
                   (pin_security.hash_pin(PIN), uid))
    auth_routes.LOGIN_ATTEMPTS.clear()
    resp = client.post("/api/auth/pin-login", json={"employee_id": uid, "pin": PIN},
                       headers={"X-Orb-Device-Token": secret})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["facility"] == {"id": F2, "code": F2_CODE,
                                               "name": "Loader Facility Two"}


def test_one_facility_every_loader_gives_no_limit_and_facility_one(world):
    with db_session() as db:
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        for key, uid in world.items():
            for name, user in _loaders(uid).items():
                assert FS.facility_scope_for(user) is None, (key, name)
                assert FS.user_facility_id(user) == FS.MAIN_FACILITY_ID, (key, name)
    finally:
        with db_session() as db:
            db.execute("INSERT OR IGNORE INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                       (F2, str(uuid.uuid4()), F2_CODE, "Loader Facility Two"))
        FS.invalidate_cache()
