"""Deferred A — HttpOnly cookie session + CSRF double-submit + refresh.

Login sets an HttpOnly `orb_session` cookie (JWT, unreadable to JS → XSS can't
exfiltrate the persistent session) plus a JS-readable `orb_csrf` token. A
cookie-authenticated *mutation* must echo the CSRF token in the X-CSRF-Token
header; Bearer-authenticated requests stay CSRF-exempt (an explicit header can't
be forged cross-site).
"""
import uuid

from fastapi.testclient import TestClient
from main import app
from auth import hash_password, SESSION_COOKIE, CSRF_COOKIE
from database import db_session

client = TestClient(app)

DOC = "COOKIE-DOC"
PWD = "cookiepass123"


def _mk_doctor():
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Dr Cookie', 'doctor', 'General Medicine', ?)",
            (DOC, hash_password(PWD)),
        )
        # Reset revocation state so a prior test's logout doesn't invalidate us.
        db.execute("UPDATE users SET token_version = 0, is_active = 1 WHERE id = ?", (DOC,))


def _login():
    _mk_doctor()
    r = client.post("/api/auth/login", json={"employee_id": DOC, "password": PWD})
    assert r.status_code == 200, r.text
    return r


def test_login_sets_httponly_session_and_csrf_cookies():
    r = _login()
    # Both cookies present in the response Set-Cookie headers.
    set_cookie = " ".join(r.headers.get_list("set-cookie"))
    assert SESSION_COOKIE in set_cookie
    assert CSRF_COOKIE in set_cookie
    # Session cookie is HttpOnly; CSRF cookie is NOT (JS must read it).
    session_line = next(c for c in r.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE + "="))
    csrf_line = next(c for c in r.headers.get_list("set-cookie") if c.startswith(CSRF_COOKIE + "="))
    assert "httponly" in session_line.lower()
    assert "httponly" not in csrf_line.lower()
    # Token still returned in the body for backward compat.
    assert r.json().get("token")
    client.cookies.clear()


def test_cookie_auth_reads_protected_route_without_bearer():
    """After login the session cookie alone authenticates a GET (no Authorization)."""
    _login()  # cookies now in the jar
    r = client.get("/api/vigil/patients")  # no Authorization header
    assert r.status_code == 200, r.text
    client.cookies.clear()


def test_cookie_mutation_without_csrf_header_is_rejected():
    """A cookie-authenticated POST lacking the X-CSRF-Token header → 403 CSRF."""
    _login()
    # logout is a session-protected POST (goes through get_current_user).
    r = client.post("/api/auth/logout")  # cookie present, NO csrf header
    assert r.status_code == 403
    assert "csrf" in r.text.lower()
    client.cookies.clear()


def test_cookie_mutation_with_valid_csrf_header_succeeds():
    _login()
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf
    r = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "logged_out"
    client.cookies.clear()


def test_bearer_mutation_is_csrf_exempt():
    """Bearer-authenticated mutations never require a CSRF token (the header can't
    be forged cross-site). Must work even though a session cookie is also present."""
    r = _login()
    token = r.json()["token"]
    # Explicit Bearer header, no CSRF header, cookie present in the jar.
    r2 = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200, r2.text
    client.cookies.clear()


def test_csrf_header_mismatch_is_rejected():
    _login()
    r = client.post("/api/auth/logout", headers={"X-CSRF-Token": "not-the-cookie-value"})
    assert r.status_code == 403
    client.cookies.clear()


def test_refresh_rotates_session_with_valid_csrf():
    """Refresh re-mints the session behind get_current_user; needs the CSRF header
    (cookie-authenticated POST) and rotates the cookies."""
    _login()
    csrf = client.cookies.get(CSRF_COOKIE)
    r = client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert r.json().get("token")
    new_set = " ".join(r.headers.get_list("set-cookie"))
    assert SESSION_COOKIE in new_set and CSRF_COOKIE in new_set
    client.cookies.clear()


def test_refresh_mints_new_token_that_authenticates():
    """Rotation: refresh returns a DIFFERENT token (fresh jti, expiry no earlier
    than the old one) and the re-set cookie authenticates subsequent requests —
    the contract the SPA's single-flight 401→refresh→retry path depends on."""
    from auth import decode_token

    r = _login()
    old_token = r.json()["token"]
    csrf = client.cookies.get(CSRF_COOKIE)
    r2 = client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 200, r2.text
    new_token = r2.json()["token"]
    assert new_token and new_token != old_token
    old_p, new_p = decode_token(old_token), decode_token(new_token)
    assert new_p["jti"] != old_p["jti"]
    assert new_p["exp"] >= old_p["exp"]
    # The rotated cookie (now in the jar) authenticates on its own.
    r3 = client.get("/api/vigil/patients")
    assert r3.status_code == 200, r3.text
    client.cookies.clear()


def test_revoked_session_cannot_refresh():
    """Old-session invalidation: logout bumps token_version, so a token minted
    before it can neither authenticate nor be laundered back to life through
    /refresh — the SPA's refresh-then-retry must dead-end in a hard logout."""
    r = _login()
    old_token = r.json()["token"]
    csrf = client.cookies.get(CSRF_COOKIE)
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    # Present the pre-logout token explicitly (logout also cleared the cookies).
    r2 = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {old_token}"})
    assert r2.status_code == 401
    client.cookies.clear()


def test_logout_clears_session_cookie():
    r = _login()
    token = r.json()["token"]
    r2 = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    # delete_cookie emits a Set-Cookie with an expired/empty value.
    set_cookie = " ".join(r2.headers.get_list("set-cookie"))
    assert SESSION_COOKIE in set_cookie
    client.cookies.clear()
