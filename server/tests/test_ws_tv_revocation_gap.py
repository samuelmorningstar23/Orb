"""Regression: the Socket.IO handshake must apply the SAME token_version
revocation rule the REST layer applies — including when the JWT carries no `tv`
claim at all.

auth.get_current_user treats an absent `tv` as 0 (``payload.get("tv", 0)``), so a
validly-signed token with no tv is rejected the moment the account's
token_version has been bumped by a logout / password-reset / deactivation. The WS
handshake used ``token_tv is not None and row_tv is not None`` as a guard, which
SKIPPED the check entirely for a tv-less token — a revoked session could keep
streaming PHI over the socket until the 24h JWT expiry.

Real staff tokens always carry tv (auth.create_token), so this closes a
defense-in-depth gap without rejecting anything legitimate. This test fails on
the pre-fix handshake (it returns the user) and passes after (it returns None).
"""
import jwt
from datetime import datetime, timedelta, timezone

from database import db_session
from auth import hash_password
from config import JWT_SECRET, JWT_ALGORITHM
from ws import realtime


def _mk_user(uid, token_version, is_active=1):
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"User {uid}", "doctor", hash_password("secretpass")),
        )
        db.execute(
            "UPDATE users SET token_version = ?, is_active = ? WHERE id = ?",
            (token_version, is_active, uid),
        )
    return uid


def _sign_without_tv(sub, role="doctor"):
    """A validly-signed staff token that deliberately OMITS the tv claim."""
    now = datetime.now(timezone.utc)
    payload = {"sub": sub, "role": role, "iat": now, "exp": now + timedelta(hours=1)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def test_ws_rejects_validly_signed_tvless_token_after_revocation():
    # Account has been revoked-all: token_version bumped to 5.
    _mk_user("WS-NOTV", token_version=5)
    token = _sign_without_tv("WS-NOTV")
    # REST would treat the absent tv as 0 (!= 5) and reject. The socket must too.
    assert realtime._authenticate({"token": token}) is None


def test_ws_still_accepts_valid_tvless_token_when_row_tv_zero():
    # A tv-less token is fine when the account has never revoked (token_version 0),
    # matching REST's "absent tv == 0" semantics — we tightened revocation, we did
    # not start rejecting legitimate zero-version sessions.
    _mk_user("WS-NOTV-OK", token_version=0)
    token = _sign_without_tv("WS-NOTV-OK")
    user = realtime._authenticate({"token": token})
    assert user is not None and user["id"] == "WS-NOTV-OK"
