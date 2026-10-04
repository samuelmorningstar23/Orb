"""The per-IP failed sign-in limiter, and the suite's per-test reset of it.

routes/auth_routes.LOGIN_ATTEMPTS refuses an IP after MAX_LOGIN_ATTEMPTS failed
sign-ins inside LOCKOUT_DURATION_SECONDS, checked before the password is
verified. Every TestClient call comes from 'testclient', so until 2026-10-04
one test's failures counted against every later test in the session, and five
in a row locked the whole session out of /api/auth/login.
conftest._isolate_login_ip_limiter now resets it around each test.

The other sign-in lock tests (test_auth_lockout_active, test_auth_login_hardening,
test_device_pin_identity) clear LOGIN_ATTEMPTS before each call, on purpose, to
measure the per-account and per-device locks. These two are the only ones that
exercise the per-IP limiter itself. They prove the reset did not
switch it off, and that it really does stop at the test boundary. They run in
file order.
"""
from fastapi.testclient import TestClient

from main import app
from routes import auth_routes

client = TestClient(app)


def _login(employee_id, password):
    return client.post("/api/auth/login", json={"employee_id": employee_id, "password": password})


def test_the_ip_limiter_refuses_even_a_correct_password_within_one_test():
    """A different unknown account on every attempt, so the per-account lock
    never engages and only the per-IP limiter can say no."""
    for i in range(auth_routes.MAX_LOGIN_ATTEMPTS):
        r = _login(f"NO-SUCH-USER-{i}", "wrong-password")
        assert r.status_code == 401, (
            f"attempt {i + 1} of {auth_routes.MAX_LOGIN_ATTEMPTS}: {r.status_code} {r.text}"
        )

    r = _login("DOC001", "password")
    assert r.status_code == 429, f"a correct password got through a locked-out IP: {r.status_code}"


def test_the_next_test_starts_with_no_failed_sign_ins():
    """Runs straight after the test above has locked this address out."""
    spent = len(auth_routes.LOGIN_ATTEMPTS.get("testclient", []))
    assert spent == 0, f"{spent} failed sign-in(s) from an earlier test still count against this one"

    r = _login("DOC001", "password")
    assert r.status_code == 200, r.text
