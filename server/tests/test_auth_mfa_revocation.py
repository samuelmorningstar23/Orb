"""A5 — MFA login flow + session revocation (token_version) integration tests."""
import base64
import sys

from fastapi.testclient import TestClient

from main import app
from database import db_session
from auth import hash_password
from services import mfa_service

client = TestClient(app)


def _mk_user(uid, role="doctor", pwd="secretpass"):
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"User {uid}", role, hash_password(pwd)),
        )
    return uid


def _login(uid, pwd="secretpass", totp_code=None):
    body = {"employee_id": uid, "password": pwd}
    if totp_code is not None:
        body["totp_code"] = totp_code
    return client.post("/api/auth/login", json=body)


def _reset_totp_step(uid):
    """Simulate the passage of time to a fresh TOTP window (so a new same-window
    code from current_code() is not rejected as a replay of an earlier step)."""
    with db_session() as db:
        db.execute("UPDATE users SET totp_last_step = -1 WHERE id = ?", (uid,))


# ── Session revocation via token_version ──────────────────────────────────────
def test_logout_revokes_existing_token():
    _mk_user("A5-REV")
    token = _login("A5-REV").json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    # Token is live: the first logout (an authenticated route) succeeds and bumps
    # token_version, revoking every token minted at the old version.
    assert client.post("/api/auth/logout", headers=h).status_code == 200
    # Re-using the SAME (now stale) token on any authenticated route is rejected.
    r = client.post("/api/auth/logout", headers=h)
    assert r.status_code == 401
    assert "revoked" in r.text.lower()
    # A fresh login issues a token at the new version that works again.
    token2 = _login("A5-REV").json()["token"]
    assert client.post("/api/auth/logout",
                       headers={"Authorization": f"Bearer {token2}"}).status_code == 200


# ── MFA enrolment + enforced login ────────────────────────────────────────────
def test_mfa_enroll_and_login_flow():
    _mk_user("A5-MFA")
    token = _login("A5-MFA").json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    # Begin enrolment -> get a secret
    begin = client.post("/api/auth/mfa/enroll/begin", headers=h)
    assert begin.status_code == 200, begin.text
    secret = begin.json()["secret"]
    assert begin.json()["otpauth_uri"].startswith("otpauth://totp/")

    # Confirm with a wrong code fails
    assert client.post("/api/auth/mfa/enroll/confirm", headers=h,
                       json={"totp_code": "000000"}).status_code in (400, 401)

    # Confirm with the real code succeeds
    code = mfa_service.current_code(secret)
    ok = client.post("/api/auth/mfa/enroll/confirm", headers=h, json={"totp_code": code})
    assert ok.status_code == 200, ok.text

    # Now login WITHOUT a code -> 401 mfa_required
    r = _login("A5-MFA")
    assert r.status_code == 401
    assert r.json()["detail"] == "mfa_required"

    # Simulate time advancing past the confirm code's window (single-use rejects a
    # code at/below the last consumed step; in production login is a later window).
    _reset_totp_step("A5-MFA")

    # Login WITH a valid code -> success
    r2 = _login("A5-MFA", totp_code=mfa_service.current_code(secret))
    assert r2.status_code == 200, r2.text
    assert "token" in r2.json()


def test_totp_code_cannot_be_replayed():
    """AUTH-1 regression: a captured code cannot mint a second session (RFC 6238 single-use)."""
    _mk_user("A5-REPLAY")
    token = _login("A5-REPLAY").json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    secret = client.post("/api/auth/mfa/enroll/begin", headers=h).json()["secret"]
    client.post("/api/auth/mfa/enroll/confirm", headers=h,
                json={"totp_code": mfa_service.current_code(secret)})
    _reset_totp_step("A5-REPLAY")

    code = mfa_service.current_code(secret)
    first = _login("A5-REPLAY", totp_code=code)
    assert first.status_code == 200, first.text
    # Immediately replay the SAME code -> must be rejected (already consumed).
    replay = _login("A5-REPLAY", totp_code=code)
    assert replay.status_code == 401
    assert "MFA" in replay.json()["detail"]


# ── Enrolment QR in the /mfa/enroll/begin payload ────────────────────────────
def test_enroll_begin_payload_includes_qr_of_exactly_the_returned_uri():
    _mk_user("A5-QR")
    token = _login("A5-QR").json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    begin = client.post("/api/auth/mfa/enroll/begin", headers=h)
    assert begin.status_code == 200, begin.text
    body = begin.json()
    # Manual key + URI are untouched by the QR addition.
    assert body["secret"] and body["otpauth_uri"].startswith("otpauth://totp/")

    qr = body["qr_svg"]
    assert qr.startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(qr.split(",", 1)[1])
    assert svg.lstrip().startswith(b"<?xml") or svg.lstrip().startswith(b"<svg")
    assert b"<svg" in svg and b"<path" in svg

    # The render is deterministic (test_mfa_service proves it), so re-rendering
    # the returned otpauth_uri and comparing bytes proves the payload QR encodes
    # EXACTLY that URI — not a stale secret, not a constant, not something else.
    assert qr == mfa_service.qr_svg_data_uri(body["otpauth_uri"])
    assert qr != mfa_service.qr_svg_data_uri(body["otpauth_uri"] + "x")


def test_enroll_begin_qr_issued_on_enrolment_ticket_flow_too():
    """The pre-login enrolment ticket is the other (and only other) authorized
    caller of enroll/begin — the QR rides the same payload there."""
    from routes.auth_routes import issue_mfa_enroll_ticket

    _mk_user("A5-QR-TKT")
    ticket = issue_mfa_enroll_ticket("A5-QR-TKT", "doctor", 0)
    r = client.post("/api/auth/mfa/enroll/begin",
                    headers={"Authorization": f"Bearer {ticket}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qr_svg"].startswith("data:image/svg+xml;base64,")
    assert body["qr_svg"] == mfa_service.qr_svg_data_uri(body["otpauth_uri"])


def test_enroll_begin_unauthorized_still_refused_and_leaks_no_qr():
    # No credentials at all.
    anon = client.post("/api/auth/mfa/enroll/begin")
    assert anon.status_code == 401
    assert "qr_svg" not in anon.text and "otpauth" not in anon.text
    # A garbage bearer is neither a session nor an enrolment ticket.
    junk = client.post("/api/auth/mfa/enroll/begin",
                       headers={"Authorization": "Bearer not-a-real-ticket"})
    assert junk.status_code == 401
    assert "qr_svg" not in junk.text and "otpauth" not in junk.text


def test_enroll_begin_omits_qr_gracefully_when_qrcode_missing(monkeypatch):
    """No qrcode package -> 200 with the field absent, never a 500."""
    _mk_user("A5-QR-NOLIB")
    token = _login("A5-QR-NOLIB").json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    monkeypatch.setitem(sys.modules, "qrcode", None)
    monkeypatch.setitem(sys.modules, "qrcode.image", None)
    monkeypatch.setitem(sys.modules, "qrcode.image.svg", None)

    r = client.post("/api/auth/mfa/enroll/begin", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "qr_svg" not in body
    # Manual-key enrolment is fully intact.
    assert body["secret"] and body["otpauth_uri"].startswith("otpauth://totp/")


def test_mfa_disable_requires_password_and_code():
    _mk_user("A5-MFA-DIS")
    token = _login("A5-MFA-DIS").json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    secret = client.post("/api/auth/mfa/enroll/begin", headers=h).json()["secret"]
    client.post("/api/auth/mfa/enroll/confirm", headers=h,
                json={"totp_code": mfa_service.current_code(secret)})
    _reset_totp_step("A5-MFA-DIS")

    # Wrong password rejected (password is checked before the code is consumed).
    # 400, not 401: the caller is already authenticated, and a 401 would trip the
    # web client's global session-expired interceptor (see auth_routes.mfa_disable).
    bad = client.post("/api/auth/mfa/disable", headers=h,
                      json={"password": "wrong", "totp_code": mfa_service.current_code(secret)})
    assert bad.status_code == 400

    # Correct password + code disables
    good = client.post("/api/auth/mfa/disable", headers=h,
                       json={"password": "secretpass", "totp_code": mfa_service.current_code(secret)})
    assert good.status_code == 200

    # Login no longer needs a code
    assert _login("A5-MFA-DIS").status_code == 200
