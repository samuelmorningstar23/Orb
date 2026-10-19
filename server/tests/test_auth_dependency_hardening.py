"""Malformed signed tokens must stop at Orb's authentication boundary with 401."""

import base64
import hashlib
import hmac
import sys
import time

import jwt
import pytest
from fastapi import HTTPException

import auth


@pytest.mark.parametrize("claim", ["iat", "nbf", "exp"])
@pytest.mark.parametrize("value", [None, [], {}])
def test_malformed_time_claim_is_an_authentication_error(claim, value):
    token = jwt.encode(
        {"sub": "malformed-token-test", claim: value},
        auth.JWT_SECRET,
        algorithm=auth.JWT_ALGORITHM,
    )

    with pytest.raises(HTTPException) as exc:
        auth.decode_token(token)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token"


def test_unknown_critical_header_is_rejected():
    token = jwt.encode(
        {"sub": "critical-header-test", "exp": int(time.time()) + 60},
        auth.JWT_SECRET,
        algorithm=auth.JWT_ALGORITHM,
        headers={"crit": ["orb-unknown-extension"], "orb-unknown-extension": True},
    )

    with pytest.raises(HTTPException) as exc:
        auth.decode_token(token)

    assert exc.value.status_code == 401


def test_deeply_nested_payload_is_an_authentication_error():
    # Build bytes directly: json.dumps would hit the same recursion guard before
    # the malformed token can reach the application's decoder.
    # CPython 3.12's JSON accelerator has a separate C recursion threshold.
    depth = max(sys.getrecursionlimit() + 100, 10_000)
    payload = b'{"nested":' + b"[" * depth + b"0" + b"]" * depth + b"}"

    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    header = b64(b'{"alg":"HS256","typ":"JWT"}')
    signing_input = header + b"." + b64(payload)
    signature = hmac.new(auth.JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    token = (signing_input + b"." + b64(signature)).decode()

    with pytest.raises(HTTPException) as exc:
        auth.decode_token(token)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token"
