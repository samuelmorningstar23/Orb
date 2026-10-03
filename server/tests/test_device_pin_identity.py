"""PIN-on-device identity for a shared ward tablet.

THE PROBLEM THIS EXISTS FOR. A full password per patient interaction is what
makes staff share logins, and a shared login destroys the audit trail — every
row says "NUR001" when three different nurses touched the chart. So the tablet
is enrolled ONCE with a long-lived device credential, and a person then proves
who they are with a short PIN on top of it.

Every test here FAILS before the feature exists (404 / missing column / missing
helper) and passes after. The properties under test are the ones that make the
mechanism safe rather than merely convenient:

  1. DEVICE BINDING  — a PIN is useless without an enrolled device. Stolen PIN +
     personal laptop must be nothing at all (test_pin_alone_is_worthless_*).
  2. ATTRIBUTION     — the audit row names the PERSON; the device is context,
     never the actor (test_audit_row_names_the_person_not_the_device).
  3. NO TIMING ORACLE — a 4-6 digit PIN is trivially guessable, so it is
     rate-limited; and PIN verification must cost the same whether the account
     exists, has no PIN, or has one. The 90x unknown-user oracle fixed on the
     password path this week must not be reintroduced here
     (test_pin_verification_is_constant_time_for_unknown_user).
  4. SHORT SESSION   — a tablet on a trolley is the threat, so a PIN session
     expires in minutes, not the 24 hours a password session gets, and refresh
     cannot launder it into a long one.
  5. LOCKOUT IS CLEARABLE — locking a clinician out mid-shift is itself a
     patient-safety event: the existing admin unlock path must clear a PIN lock,
     and a PIN lock must NOT take the password login down with it.
  6. REVOCATION      — revoking the tablet kills the sessions minted on it.
"""
import statistics
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from main import app
from config import JWT_SECRET, JWT_ALGORITHM
from database import db_session
from auth import hash_password, create_token
from routes import auth_routes

client = TestClient(app)


# ── helpers ───────────────────────────────────────────────────────────────────

def _mk_user(uid, role="nurse", pwd="secretpass", pin=None, dept="General Medicine"):
    from services import pin_security
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?,?,?,?,?)",
            (uid, f"User {uid}", role, dept, hash_password(pwd)),
        )
        db.execute(
            "UPDATE users SET is_active=1, role=?, department=?, password_hash=?, "
            "failed_login_attempts=0, login_locked_until=NULL, "
            "device_pin_failed_attempts=0, device_pin_locked_until=NULL, "
            "device_pin_hash=?, totp_enabled=0, totp_secret=NULL WHERE id=?",
            (role, dept, hash_password(pwd),
             pin_security.hash_pin(pin) if pin else None, uid),
        )
    return uid


def _admin_headers(uid="DEV-ADMIN"):
    _mk_user(uid, role="admin", pwd="adminpass")
    with db_session() as db:
        tv = db.execute("SELECT token_version FROM users WHERE id=?", (uid,)).fetchone()
    return {"Authorization": f"Bearer {create_token(uid, 'admin', token_version=(tv['token_version'] or 0) if tv else 0)}"}


def _enrol_device(label="Ward GM-1 trolley", department="General Medicine"):
    """Admin enrols a tablet; returns (device_id, one-time secret)."""
    r = client.post(
        "/api/auth/devices",
        json={"label": label, "department": department},
        headers=_admin_headers(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return body["device_id"], body["device_secret"]


def _pin_login(employee_id, pin, secret=None, extra_headers=None):
    """PIN login carrying the device credential as a header (the tablet's
    pre-bind path). Clears the per-IP limiter so the per-ACCOUNT / per-DEVICE
    behaviour is what is being measured — mirrors test_auth_login_hardening."""
    auth_routes.LOGIN_ATTEMPTS.clear()
    headers = dict(extra_headers or {})
    if secret is not None:
        headers["X-Orb-Device-Token"] = secret
    return client.post(
        "/api/auth/pin-login",
        json={"employee_id": employee_id, "pin": pin},
        headers=headers,
    )


def _device_row(device_id):
    with db_session() as db:
        return db.execute(
            "SELECT * FROM enrolled_devices WHERE id=?", (device_id,)
        ).fetchone()


def _pin_lock_state(uid):
    with db_session() as db:
        r = db.execute(
            "SELECT device_pin_failed_attempts, device_pin_locked_until FROM users WHERE id=?",
            (uid,),
        ).fetchone()
    return r["device_pin_failed_attempts"], r["device_pin_locked_until"]


@pytest.fixture(autouse=True)
def _clean_devices():
    """TEST-ISOLATION LAW: this module enrols devices and provisions PINs on a
    SHARED session DB. Remove every artefact it creates so a later module never
    measures our leftovers (no patients are admitted here, so there is nothing
    to discharge)."""
    yield
    auth_routes.LOGIN_ATTEMPTS.clear()
    with db_session() as db:
        try:
            db.execute("DELETE FROM enrolled_devices WHERE label LIKE 'Ward %' OR label LIKE 'TEST-%'")
        except Exception:
            pass
        db.execute(
            "UPDATE users SET device_pin_hash=NULL, device_pin_set_at=NULL, "
            "device_pin_failed_attempts=0, device_pin_locked_until=NULL "
            "WHERE id LIKE 'DEV-%' OR id LIKE 'PIN-%'"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  1. A PIN is worthless without the device
# ══════════════════════════════════════════════════════════════════════════════

def test_pin_alone_is_worthless_off_an_enrolled_device():
    """Stolen PIN + personal laptop = nothing. No device credential, no session."""
    uid = _mk_user("PIN-NODEV", pin="4417")
    r = _pin_login(uid, "4417", secret=None)
    assert r.status_code == 401, r.text
    assert "device" in r.text.lower()
    from auth import SESSION_COOKIE
    assert SESSION_COOKIE not in r.cookies


def test_pin_is_worthless_with_a_forged_device_token():
    _mk_user("PIN-FORGE", pin="4417")
    r = _pin_login("PIN-FORGE", "4417", secret="not-a-real-device-secret")
    assert r.status_code == 401
    assert "device" in r.text.lower()


def test_pin_is_worthless_on_a_revoked_device():
    device_id, secret = _enrol_device()
    _mk_user("PIN-REVOKE", pin="4417")
    assert _pin_login("PIN-REVOKE", "4417", secret=secret).status_code == 200

    r = client.post(f"/api/auth/devices/{device_id}/revoke", headers=_admin_headers())
    assert r.status_code == 200, r.text
    assert _pin_login("PIN-REVOKE", "4417", secret=secret).status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
#  2. Attribution: the audit row names the PERSON
# ══════════════════════════════════════════════════════════════════════════════

def test_audit_row_names_the_person_not_the_device():
    """The whole point. actor_user_id is the human; the device is metadata."""
    device_id, secret = _enrol_device()
    uid = _mk_user("PIN-ATTRIB", pin="9021")
    assert _pin_login(uid, "9021", secret=secret).status_code == 200

    with db_session() as db:
        row = db.execute(
            "SELECT actor_user_id, actor_role, metadata_json FROM audit_logs "
            "WHERE action_type='pin_login_success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "PIN login wrote no audit row"
    assert row["actor_user_id"] == uid, (
        f"audit actor is {row['actor_user_id']!r}, must be the person ({uid})"
    )
    assert row["actor_user_id"] != device_id
    assert device_id in (row["metadata_json"] or ""), "device id must be recorded as context"


def test_session_carries_the_person_not_the_device():
    device_id, secret = _enrol_device()
    uid = _mk_user("PIN-WHOAMI", pin="9022")
    r = _pin_login(uid, "9022", secret=secret)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["id"] == uid
    claims = jwt.decode(body["token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert claims["sub"] == uid, "the subject of the session is the PERSON"
    assert claims.get("did") == device_id, "the device rides as context, not as subject"


# ══════════════════════════════════════════════════════════════════════════════
#  3. Brute force + the timing oracle
# ══════════════════════════════════════════════════════════════════════════════

def test_pin_verification_is_constant_time_for_unknown_user():
    """The 90x oracle fixed on the password path must not reappear here.

    Self-calibrating: measure a real account with a PIN (always bcrypt), then
    require the unknown-account path to cost at least half as much. Without the
    constant-work helper, the unknown path skips bcrypt entirely (~1% of the
    time) and this fails hard — which is a live staff-roster disclosure, since
    the response body is identical and latency is the only signal.
    """
    _, secret = _enrol_device()
    real = _mk_user("PIN-TIMING", pin="1357")

    def med_ms(uid):
        samples = []
        for _ in range(7):
            # Keep the real account off the lockout path — a lock short-circuits
            # before verification and would corrupt the measurement.
            with db_session() as db:
                db.execute(
                    "UPDATE users SET device_pin_failed_attempts=0, "
                    "device_pin_locked_until=NULL WHERE id=?", (uid,),
                )
            _reset_device_lock(secret)
            t0 = time.perf_counter()
            r = _pin_login(uid, "0000", secret=secret)
            samples.append((time.perf_counter() - t0) * 1000.0)
            assert r.status_code == 401
        return statistics.median(samples)

    med_ms(real)                 # warm bcrypt / imports
    med_ms("PIN-GHOST-warm")

    real_ms = med_ms(real)
    fake_ms = med_ms("PIN-GHOST-1")

    assert real_ms > 20, f"real bcrypt path implausibly fast ({real_ms:.1f} ms) — calibration off"
    assert fake_ms >= 0.5 * real_ms, (
        f"PIN user-enumeration timing oracle: unknown id {fake_ms:.1f} ms vs real id "
        f"{real_ms:.1f} ms (ratio {fake_ms / real_ms:.2f})"
    )


def test_user_with_no_pin_provisioned_also_pays_bcrypt_time():
    """"Account exists but has no PIN" must be indistinguishable from "wrong PIN"
    — otherwise the timing tells an attacker which staff use the tablet."""
    _, secret = _enrol_device()
    with_pin = _mk_user("PIN-HASPIN", pin="2468")
    without = _mk_user("PIN-NOPIN", pin=None)

    def med_ms(uid):
        samples = []
        for _ in range(5):
            with db_session() as db:
                db.execute(
                    "UPDATE users SET device_pin_failed_attempts=0, "
                    "device_pin_locked_until=NULL WHERE id=?", (uid,),
                )
            _reset_device_lock(secret)
            t0 = time.perf_counter()
            assert _pin_login(uid, "0000", secret=secret).status_code == 401
            samples.append((time.perf_counter() - t0) * 1000.0)
        return statistics.median(samples)

    med_ms(with_pin)
    a = med_ms(with_pin)
    b = med_ms(without)
    assert b >= 0.5 * a, f"no-PIN account is {b:.1f} ms vs {a:.1f} ms with a PIN — oracle"


def _reset_device_lock(secret):
    from auth import hash_device_secret
    with db_session() as db:
        db.execute(
            "UPDATE enrolled_devices SET failed_pin_attempts=0, pin_locked_until=NULL "
            "WHERE secret_hash=?", (hash_device_secret(secret),),
        )


def test_pin_brute_force_locks_the_account():
    _, secret = _enrol_device()
    uid = _mk_user("PIN-BRUTE", pin="1111")

    for _ in range(auth_routes.DEVICE_PIN_MAX_FAILURES):
        _reset_device_lock(secret)
        assert _pin_login(uid, "0000", secret=secret).status_code == 401

    _reset_device_lock(secret)
    # Even the CORRECT pin is now refused — the lock is checked before use.
    r = _pin_login(uid, "1111", secret=secret)
    assert r.status_code == 429, f"expected PIN lock, got {r.status_code}: {r.text}"
    _, locked_until = _pin_lock_state(uid)
    assert locked_until and float(locked_until) > time.time()


def test_a_correct_pin_clears_the_failure_counter():
    """The ordinary fat-finger must not creep toward a lock across a shift."""
    _, secret = _enrol_device()
    uid = _mk_user("PIN-RESET", pin="1212")
    for _ in range(auth_routes.DEVICE_PIN_MAX_FAILURES - 1):
        assert _pin_login(uid, "0000", secret=secret).status_code == 401
    assert _pin_login(uid, "1212", secret=secret).status_code == 200
    fails, locked = _pin_lock_state(uid)
    assert (fails or 0) == 0 and not locked


def test_sweeping_pins_across_many_staff_locks_the_device():
    """Per-account limits alone do not stop a sweep: 5 guesses each across 40
    staff is 200 guesses on one tablet. The DEVICE is throttled too."""
    device_id, secret = _enrol_device()
    for i in range(auth_routes.DEVICE_SWEEP_MAX_FAILURES):
        uid = _mk_user(f"PIN-SWEEP{i}", pin="7777")
        r = _pin_login(uid, "0000", secret=secret)
        assert r.status_code in (401, 429)

    victim = _mk_user("PIN-SWEEPVICTIM", pin="7777")
    r = _pin_login(victim, "7777", secret=secret)
    assert r.status_code == 429, f"device sweep was not throttled: {r.status_code}"
    row = _device_row(device_id)
    assert row["pin_locked_until"] and float(row["pin_locked_until"]) > time.time()


# ══════════════════════════════════════════════════════════════════════════════
#  4. Short session / auto-logout
# ══════════════════════════════════════════════════════════════════════════════

def test_pin_session_expires_in_minutes_not_a_day():
    _, secret = _enrol_device()
    uid = _mk_user("PIN-SHORT", pin="3141")
    r = _pin_login(uid, "3141", secret=secret)
    assert r.status_code == 200
    claims = jwt.decode(r.json()["token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    lifetime_min = (claims["exp"] - claims["iat"]) / 60.0
    assert lifetime_min <= auth_routes.DEVICE_PIN_SESSION_MINUTES + 1, (
        f"PIN session lives {lifetime_min:.0f} min — a tablet on a trolley must not "
        f"hold a long session"
    )
    assert lifetime_min < 60, "a PIN session must not last an hour"


def test_refresh_cannot_launder_a_pin_session_into_a_long_one():
    """Otherwise the short expiry is theatre: one /refresh and the trolley tablet
    is holding a 24-hour session again."""
    _, secret = _enrol_device()
    uid = _mk_user("PIN-REFRESH", pin="3142")
    r = _pin_login(uid, "3142", secret=secret)
    assert r.status_code == 200
    token = r.json()["token"]

    r2 = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200, r2.text
    claims = jwt.decode(r2.json()["token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    lifetime_min = (claims["exp"] - claims["iat"]) / 60.0
    assert lifetime_min <= auth_routes.DEVICE_PIN_SESSION_MINUTES + 1, (
        f"refresh extended a PIN session to {lifetime_min:.0f} min"
    )
    assert claims.get("did"), "refresh dropped the device binding from the session"


def test_revoking_the_device_kills_live_pin_sessions():
    device_id, secret = _enrol_device()
    uid = _mk_user("PIN-KILL", pin="5150")
    r = _pin_login(uid, "5150", secret=secret)
    assert r.status_code == 200
    token = r.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/device", headers=auth).status_code in (200, 404)

    assert client.post(
        f"/api/auth/devices/{device_id}/revoke", headers=_admin_headers()
    ).status_code == 200

    # The token is still cryptographically valid and unexpired — the DEVICE check
    # is what must reject it.
    r = client.get("/api/vigil/patients", headers=auth)
    assert r.status_code == 401, f"revoked tablet still holds a live session ({r.status_code})"


# ══════════════════════════════════════════════════════════════════════════════
#  5. Lockout is clearable, and does not strand the clinician
# ══════════════════════════════════════════════════════════════════════════════

def test_pin_lockout_does_not_lock_the_password_login():
    """A PIN lock must degrade to "use your password", not "you are out of the
    EHR". Locking a clinician out mid-shift is itself a patient-safety event."""
    _, secret = _enrol_device()
    uid = _mk_user("PIN-DEGRADE", pwd="secretpass", pin="1414")
    for _ in range(auth_routes.DEVICE_PIN_MAX_FAILURES):
        _reset_device_lock(secret)
        _pin_login(uid, "0000", secret=secret)
    _reset_device_lock(secret)
    assert _pin_login(uid, "1414", secret=secret).status_code == 429

    auth_routes.LOGIN_ATTEMPTS.clear()
    r = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert r.status_code == 200, (
        f"a PIN lockout stranded the clinician on the password path too ({r.status_code})"
    )


def test_admin_unlock_clears_the_pin_lockout():
    """Requirement 5: the existing admin unlock path must clear this."""
    _, secret = _enrol_device()
    uid = _mk_user("PIN-UNLOCK", pin="1616")
    for _ in range(auth_routes.DEVICE_PIN_MAX_FAILURES):
        _reset_device_lock(secret)
        _pin_login(uid, "0000", secret=secret)
    _reset_device_lock(secret)
    assert _pin_login(uid, "1616", secret=secret).status_code == 429

    r = client.post(f"/api/auth/unlock/{uid}", headers=_admin_headers())
    assert r.status_code == 200, r.text
    _reset_device_lock(secret)
    assert _pin_login(uid, "1616", secret=secret).status_code == 200


def test_admin_can_clear_a_locked_device():
    device_id, secret = _enrol_device()
    with db_session() as db:
        db.execute(
            "UPDATE enrolled_devices SET failed_pin_attempts=99, pin_locked_until=? WHERE id=?",
            (time.time() + 900, device_id),
        )
    uid = _mk_user("PIN-DEVUNLOCK", pin="1717")
    assert _pin_login(uid, "1717", secret=secret).status_code == 429

    r = client.post(f"/api/auth/devices/{device_id}/unlock", headers=_admin_headers())
    assert r.status_code == 200, r.text
    assert _pin_login(uid, "1717", secret=secret).status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
#  6. Enrolment is admin-only, and the secret is shown once
# ══════════════════════════════════════════════════════════════════════════════

def test_device_enrolment_requires_admin():
    _mk_user("DEV-NURSE", role="nurse")
    with db_session() as db:
        tv = db.execute("SELECT token_version FROM users WHERE id='DEV-NURSE'").fetchone()
    tok = create_token("DEV-NURSE", "nurse", token_version=(tv["token_version"] or 0))
    r = client.post(
        "/api/auth/devices",
        json={"label": "TEST-rogue"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 403


def test_device_secret_is_never_listed_after_enrolment():
    device_id, secret = _enrol_device()
    r = client.get("/api/auth/devices", headers=_admin_headers())
    assert r.status_code == 200, r.text
    blob = r.text
    assert device_id in blob
    assert secret not in blob, "the device secret must never be readable after enrolment"
    assert "secret_hash" not in blob


def test_pin_login_is_refused_for_mfa_mandated_roles():
    """A PIN must not become the back door around a TOTP mandate."""
    _, secret = _enrol_device()
    uid = _mk_user("PIN-MFAROLE", role="admin", pin="1818")
    import config
    original = list(config.MFA_REQUIRED_ROLES)
    config.MFA_REQUIRED_ROLES.append("admin")
    try:
        r = _pin_login(uid, "1818", secret=secret)
        assert r.status_code == 403, f"PIN login bypassed the MFA mandate ({r.status_code})"
    finally:
        config.MFA_REQUIRED_ROLES[:] = original


def test_binding_the_device_stores_the_secret_out_of_javascripts_reach():
    """The tablet exchanges the raw secret ONCE for an HttpOnly cookie, so the
    long-lived device credential never has to sit in localStorage where an XSS
    could read it (the same hole the session JWT was moved out of)."""
    from auth import DEVICE_COOKIE
    device_id, secret = _enrol_device()
    r = client.post("/api/auth/device/bind", json={"device_secret": secret})
    assert r.status_code == 200, r.text
    assert r.json()["device_id"] == device_id
    set_cookie = r.headers.get("set-cookie", "")
    assert DEVICE_COOKIE in set_cookie and "HttpOnly" in set_cookie

    # And the bound cookie alone is enough for a PIN login (no header).
    uid = _mk_user("PIN-BOUND", pin="1919")
    r2 = client.post("/api/auth/pin-login", json={"employee_id": uid, "pin": "1919"})
    assert r2.status_code == 200, r2.text
    client.cookies.clear()


def test_pin_can_be_set_by_the_owner_with_their_password():
    _mk_user("PIN-SELFSET", pwd="secretpass", pin=None)
    with db_session() as db:
        tv = db.execute("SELECT token_version FROM users WHERE id='PIN-SELFSET'").fetchone()
    tok = create_token("PIN-SELFSET", "nurse", token_version=(tv["token_version"] or 0))
    r = client.post(
        "/api/auth/device-pin",
        json={"password": "secretpass", "pin": "2020"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text

    _, secret = _enrol_device()
    assert _pin_login("PIN-SELFSET", "2020", secret=secret).status_code == 200


def test_short_and_nonnumeric_pins_are_refused():
    _mk_user("PIN-WEAK", pwd="secretpass", pin=None)
    with db_session() as db:
        tv = db.execute("SELECT token_version FROM users WHERE id='PIN-WEAK'").fetchone()
    hdr = {"Authorization": f"Bearer {create_token('PIN-WEAK', 'nurse', token_version=(tv['token_version'] or 0))}"}
    for bad in ("123", "123456789", "abcd", "12 34", "1111", "1234"):
        r = client.post(
            "/api/auth/device-pin", json={"password": "secretpass", "pin": bad}, headers=hdr
        )
        assert r.status_code in (400, 422), f"weak PIN {bad!r} accepted ({r.status_code})"


def test_device_pin_is_separate_from_the_cosigning_pin():
    """The co-sign PIN authorises agentic medication dispatch. If unlocking the
    tablet used the same secret, anyone who shoulder-surfs the unlock can also
    countersign a med order in that nurse's name — two controls collapsed into
    one."""
    _, secret = _enrol_device()
    from services import pin_security
    uid = _mk_user("PIN-SEPARATE", pin=None)
    with db_session() as db:
        db.execute(
            "UPDATE users SET pin_hash=?, device_pin_hash=? WHERE id=?",
            (pin_security.hash_pin("4242"), pin_security.hash_pin("8888"), uid),
        )
    assert _pin_login(uid, "4242", secret=secret).status_code == 401, (
        "the co-signing PIN unlocked the tablet — the two credentials are fused"
    )
    _reset_device_lock(secret)
    with db_session() as db:
        db.execute(
            "UPDATE users SET device_pin_failed_attempts=0, device_pin_locked_until=NULL WHERE id=?",
            (uid,),
        )
    assert _pin_login(uid, "8888", secret=secret).status_code == 200
