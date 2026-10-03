"""Login-path hardening regressions (password-guessing attack surface).

Every test here FAILS on the pre-fix code and passes after the fix:

  * test_unknown_user_pays_bcrypt_time  — the user-enumeration TIMING oracle:
      pre-fix an unknown employee_id skipped bcrypt (~2.5 ms) while a real id
      paid it (~220 ms), a 90x gap that leaks the staff list off latency. The
      fix routes both branches through auth.verify_password_ct, so the unknown
      branch spends the same bcrypt time.
  * test_mfa_failures_count_toward_account_lock — a wrong TOTP used to bump only
      the per-IP limiter, so a rotating-IP attacker with the password could brute
      the 6-digit code forever. The fix counts MFA failures toward the per-account
      lock.
  * test_admin_unlock_clears_lockout — the admin break-glass unlock endpoint the
      clinician-safety design needs; pre-fix it did not exist (404).

Plus guards that must always hold: identical response for real vs fake ids, and
that failed logins are audited WITHOUT the attempted password.
"""
import json
import statistics
import time

import pytest
from fastapi.testclient import TestClient

from main import app
from database import db_session
from auth import hash_password, create_token
from routes import auth_routes
from services import mfa_service

client = TestClient(app)


# ── helpers ───────────────────────────────────────────────────────────────────
def _mk_user(uid, role="doctor", pwd="secretpass", totp_secret=None):
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"User {uid}", role, hash_password(pwd)),
        )
        db.execute(
            "UPDATE users SET is_active=1, failed_login_attempts=0, login_locked_until=NULL, "
            "totp_enabled=?, totp_secret=?, totp_last_step=NULL WHERE id=?",
            (1 if totp_secret else 0, totp_secret, uid),
        )
    return uid


def _login(uid, pwd, totp=None):
    # Clear the per-IP limiter so we exercise the per-ACCOUNT / timing behaviour,
    # i.e. an attacker rotating source IPs (NAT). Mirrors the shipped
    # test_auth_lockout_active.py convention.
    auth_routes.LOGIN_ATTEMPTS.clear()
    body = {"employee_id": uid, "password": pwd}
    if totp is not None:
        body["totp_code"] = totp
    return client.post("/api/auth/login", json=body)


def _fails_row(uid):
    with db_session() as db:
        r = db.execute(
            "SELECT failed_login_attempts, login_locked_until FROM users WHERE id=?", (uid,)
        ).fetchone()
    return r["failed_login_attempts"], r["login_locked_until"]


# ── 1. timing oracle ──────────────────────────────────────────────────────────
def test_unknown_user_pays_bcrypt_time():
    """A wrong password on a FAKE id must cost about the same as on a REAL id.

    We self-calibrate to this machine: measure the real-id path (always bcrypt),
    then require the fake-id path to be at least half that. Pre-fix the fake path
    was ~1% of the real path (bcrypt skipped) and this assertion fails hard.
    """
    real = _mk_user("TMR-REAL", pwd="secretpass")

    def med_ms(uid):
        samples = []
        for _ in range(7):
            with db_session() as db:  # keep the real account off the lockout path
                db.execute(
                    "UPDATE users SET failed_login_attempts=0, login_locked_until=NULL WHERE id=?",
                    (uid,),
                )
            t0 = time.perf_counter()
            r = _login(uid, "wrong-password")
            samples.append((time.perf_counter() - t0) * 1000.0)
            assert r.status_code == 401
        return statistics.median(samples)

    # warm up bcrypt / imports
    med_ms(real)
    med_ms("TMR-GHOST-warm")

    real_ms = med_ms(real)
    fake_ms = med_ms("TMR-GHOST-1")

    assert real_ms > 20, f"real bcrypt path implausibly fast ({real_ms:.1f} ms) — test calibration off"
    # The oracle is closed when the two paths are within a small factor. Pre-fix
    # ratio ≈ 0.01; post-fix ≈ 1.0. 0.5 is a wide, non-flaky bound.
    assert fake_ms >= 0.5 * real_ms, (
        f"user-enumeration timing oracle: fake id {fake_ms:.1f} ms vs real id "
        f"{real_ms:.1f} ms (ratio {fake_ms / real_ms:.2f}); unknown users must "
        f"spend bcrypt time too"
    )


def test_real_and_fake_id_responses_are_identical():
    """Status code AND detail must not distinguish a real id from a fake one."""
    _mk_user("PARITY-REAL", pwd="secretpass")
    r_real = _login("PARITY-REAL", "wrong-password")
    r_fake = _login("PARITY-NOPE-XYZ", "wrong-password")
    assert r_real.status_code == r_fake.status_code == 401
    assert r_real.json() == r_fake.json()


# ── 2. MFA brute-force now trips the account lock ─────────────────────────────
def _wrong_code(secret):
    live = {mfa_service.current_code(secret)}
    for cand in ("000000", "111111", "222222"):
        if cand not in live:
            return cand
    return "090909"


def test_mfa_failures_count_toward_account_lock():
    secret = mfa_service.generate_secret()
    uid = _mk_user("MFA-LOCK", pwd="secretpass", totp_secret=secret)
    bad = _wrong_code(secret)

    # Correct password, wrong second factor, ACCOUNT_MAX_FAILURES times.
    for _ in range(auth_routes.ACCOUNT_MAX_FAILURES):
        r = _login(uid, "secretpass", totp=bad)
        assert r.status_code == 401  # "Invalid MFA code"

    # Account must now be locked: even a correct password + correct code is
    # refused with 429 (the lock check runs before verification). Pre-fix the
    # failure counter never moved and this stayed a 200/401.
    live = mfa_service.current_code(secret)
    r = _login(uid, "secretpass", totp=live)
    assert r.status_code == 429, f"expected account lock after MFA brute-force, got {r.status_code}"
    assert "locked" in r.text.lower()


# ── 3. admin break-glass unlock ───────────────────────────────────────────────
def _admin_token():
    _mk_user("UNLOCK-ADMIN", role="admin", pwd="adminpass")
    return create_token("UNLOCK-ADMIN", "admin")


def test_admin_unlock_clears_lockout():
    uid = _mk_user("UNLOCK-TARGET", pwd="secretpass")
    # Drive the account into a hard lock.
    for _ in range(auth_routes.ACCOUNT_MAX_FAILURES):
        assert _login(uid, "wrong").status_code == 401
    assert _login(uid, "secretpass").status_code == 429  # locked, correct pw refused

    token = _admin_token()
    r = client.post(f"/api/auth/unlock/{uid}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["was_locked"] is True

    # Lock cleared on the row, and the correct password works again immediately.
    fails, locked_until = _fails_row(uid)
    assert (fails or 0) == 0 and not locked_until
    assert _login(uid, "secretpass").status_code == 200

    # The unlock is audited as a security event.
    with db_session() as db:
        row = db.execute(
            "SELECT actor_user_id, target_id FROM audit_logs "
            "WHERE action_type='account_unlocked' AND target_id=? ORDER BY id DESC LIMIT 1",
            (uid,),
        ).fetchone()
    assert row is not None and row["actor_user_id"] == "UNLOCK-ADMIN"


def test_admin_unlock_requires_admin():
    uid = _mk_user("UNLOCK-TARGET2", pwd="secretpass")
    _mk_user("UNLOCK-NURSE", role="nurse", pwd="nursepass")
    token = create_token("UNLOCK-NURSE", "nurse")
    r = client.post(f"/api/auth/unlock/{uid}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# ── 4. legacy unsalted-SHA-256 accounts are refused a session by default ──────
def _mk_legacy_user(uid, pwd="legacypass1"):
    """An account still on the pre-bcrypt hash tier. The UPDATE forces the hash
    back to SHA-256 even if an earlier run's escape-hatch login upgraded it."""
    import hashlib
    legacy = hashlib.sha256(pwd.encode()).hexdigest()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"User {uid}", "doctor", legacy),
        )
        db.execute(
            "UPDATE users SET password_hash=?, is_active=1, failed_login_attempts=0, "
            "login_locked_until=NULL, totp_enabled=0, totp_secret=NULL WHERE id=?",
            (legacy, uid),
        )
    return uid


def _stored_hash(uid):
    with db_session() as db:
        return db.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()[
            "password_hash"
        ]


def test_legacy_hash_login_rejected_by_default(monkeypatch):
    """The acceptance window is CLOSED unless ORB_ALLOW_LEGACY_SHA256 re-opens
    it: a correct password on a SHA-256 account gets the distinct
    reset-required error, no session, and the hash is left un-upgraded."""
    monkeypatch.delenv("ORB_ALLOW_LEGACY_SHA256", raising=False)
    uid = _mk_legacy_user("LEGACY-DENY")

    r = _login(uid, "legacypass1")
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Password reset required by an administrator."
    assert not r.json().get("token")
    # No silent upgrade happened on the refused path.
    assert not _stored_hash(uid).startswith("$2")

    # The denial is on the audit trail.
    with db_session() as db:
        row = db.execute(
            "SELECT actor_user_id FROM audit_logs WHERE action_type='login_denied_legacy_hash' "
            "AND actor_user_id=? ORDER BY id DESC LIMIT 1", (uid,),
        ).fetchone()
    assert row is not None


def test_legacy_gate_does_not_enumerate_on_wrong_password(monkeypatch):
    """The distinct error is shown only to a caller who KNOWS the password. A
    wrong password on a legacy account must stay byte-identical to a wrong
    password on an id that does not exist."""
    monkeypatch.delenv("ORB_ALLOW_LEGACY_SHA256", raising=False)
    uid = _mk_legacy_user("LEGACY-ENUM")
    r_legacy = _login(uid, "wrong-password")
    r_ghost = _login("LEGACY-GHOST-XYZ", "wrong-password")
    assert r_legacy.status_code == r_ghost.status_code == 401
    assert r_legacy.json() == r_ghost.json()


def test_legacy_hash_escape_hatch_allows_and_upgrades(monkeypatch):
    """ORB_ALLOW_LEGACY_SHA256=1 re-opens the window AND the upgrade-on-login
    path still rotates the hash to bcrypt — so the next login succeeds even
    after the hatch is closed again."""
    uid = _mk_legacy_user("LEGACY-HATCH")

    monkeypatch.setenv("ORB_ALLOW_LEGACY_SHA256", "1")
    r = _login(uid, "legacypass1")
    assert r.status_code == 200, r.text
    assert _stored_hash(uid).startswith("$2"), "hash was not upgraded to bcrypt on login"

    monkeypatch.delenv("ORB_ALLOW_LEGACY_SHA256", raising=False)
    assert _login(uid, "legacypass1").status_code == 200


def test_legacy_hash_count_names_no_accounts(caplog):
    """Startup logs a COUNT of remaining legacy accounts — never a username."""
    import logging
    from auth import count_legacy_password_hashes
    from routes.auth_routes import _log_legacy_hash_posture

    uid = _mk_legacy_user("LEGACY-COUNT")
    assert count_legacy_password_hashes() >= 1
    with caplog.at_level(logging.INFO, logger="orb.auth"):
        _log_legacy_hash_posture()
    assert "legacy unsalted-SHA-256" in caplog.text
    assert uid not in caplog.text


def test_legacy_hash_count_ignores_non_login_sentinels():
    """The built-in system accounts hold sentinel strings, not passwords
    ('system_hash' for the Sage system user, 'system' for the bots). Counting
    them made every fresh appliance tell its operator to reset passwords on
    accounts that have none. Only a 64-hex SHA-256 digest is a legacy hash."""
    from auth import count_legacy_password_hashes

    real_legacy = _mk_legacy_user("LEGACY-SHAPED")
    before = count_legacy_password_hashes()
    with db_session() as db:
        for uid, sentinel in (("SENTINEL-A", "system_hash"), ("SENTINEL-B", "system"),
                              ("SENTINEL-C", "x")):
            db.execute(
                "INSERT OR REPLACE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
                (uid, f"User {uid}", "doctor", sentinel),
            )
    try:
        assert count_legacy_password_hashes() == before, (
            "a sentinel non-password value was counted as a legacy SHA-256 hash")
        assert before >= 1, f"{real_legacy} holds a real SHA-256 digest and must still count"
    finally:
        with db_session() as db:
            db.execute("DELETE FROM users WHERE id IN ('SENTINEL-A','SENTINEL-B','SENTINEL-C')")


# ── 5. failed logins are audited WITHOUT the attempted password ───────────────
def test_failed_login_is_audited_without_password():
    uid = _mk_user("AUDIT-USER", pwd="secretpass")
    secret_password = "hunter2-do-not-log-me"
    r = _login(uid, secret_password)
    assert r.status_code == 401

    with db_session() as db:
        row = db.execute(
            "SELECT summary_message, metadata_json, actor_user_id FROM audit_logs "
            "WHERE action_type='login_failure' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "no login_failure audit row written"
    blob = json.dumps(dict(row))
    assert secret_password not in blob, "attempted password leaked into the audit log"
    # The attempted id IS recorded (it is not a secret) so the event is useful.
    assert uid in (row["metadata_json"] or "")
