"""Slice 4 — per-account lockout + is_active enforcement at login / on every request."""
import time

from fastapi.testclient import TestClient

from main import app
from database import db_session
from auth import hash_password
from routes import auth_routes

client = TestClient(app)


def _mk_user(uid, role="doctor", pwd="secretpass"):
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"User {uid}", role, hash_password(pwd)),
        )
        db.execute(
            "UPDATE users SET is_active = 1, failed_login_attempts = 0, login_locked_until = NULL WHERE id = ?",
            (uid,),
        )
    return uid


def _login(uid, pwd):
    # Clear the per-IP limiter each call: the per-account lock under test defends
    # against credential stuffing spread across MANY source IPs, which the per-IP
    # limiter (a lower threshold, keyed on one IP) can't see. Clearing simulates a
    # fresh source IP per attempt so the per-account counter is what trips.
    auth_routes.LOGIN_ATTEMPTS.clear()
    return client.post("/api/auth/login", json={"employee_id": uid, "password": pwd})


def test_account_locks_after_repeated_failures():
    uid = _mk_user("LOCK-1")
    for _ in range(auth_routes.ACCOUNT_MAX_FAILURES):
        assert _login(uid, "wrong").status_code == 401
    # The account is now locked even with the CORRECT password, from any IP.
    r = _login(uid, "secretpass")
    assert r.status_code == 429
    assert "locked" in r.text.lower()
    # Lock is persisted on the row.
    with db_session() as db:
        row = db.execute("SELECT login_locked_until FROM users WHERE id = ?", (uid,)).fetchone()
    assert row["login_locked_until"] and float(row["login_locked_until"]) > time.time()


def test_successful_login_clears_failures():
    uid = _mk_user("LOCK-2")
    for _ in range(auth_routes.ACCOUNT_MAX_FAILURES - 1):
        assert _login(uid, "wrong").status_code == 401
    # One below the threshold: correct password still works AND resets the counter.
    assert _login(uid, "secretpass").status_code == 200
    with db_session() as db:
        row = db.execute("SELECT failed_login_attempts, login_locked_until FROM users WHERE id = ?", (uid,)).fetchone()
    assert (row["failed_login_attempts"] or 0) == 0
    assert not row["login_locked_until"]


def test_deactivated_user_cannot_login():
    uid = _mk_user("INACTIVE-1")
    with db_session() as db:
        db.execute("UPDATE users SET is_active = 0, failed_login_attempts = 0, login_locked_until = NULL WHERE id = ?", (uid,))
    r = _login(uid, "secretpass")
    assert r.status_code == 403
    assert "deactivat" in r.text.lower()


def test_deactivation_blocks_existing_token():
    uid = _mk_user("INACTIVE-2")
    token = _login(uid, "secretpass").json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    # Token works while active (logout is an authenticated route via get_current_user).
    assert client.post("/api/auth/logout", headers=h).status_code == 200
    # Re-login (previous logout bumped token_version), then deactivate WITHOUT a
    # further tv bump — get_current_user must reject on is_active alone.
    token2 = _login(uid, "secretpass").json()["token"]
    h2 = {"Authorization": f"Bearer {token2}"}
    with db_session() as db:
        db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (uid,))
    r = client.post("/api/auth/logout", headers=h2)
    assert r.status_code == 403
