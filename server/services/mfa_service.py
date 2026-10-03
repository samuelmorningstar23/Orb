"""Orb — Multi-factor authentication (TOTP).

Dependency-free TOTP (RFC 6238) built on the Python standard library only
(hmac/hashlib/base64/struct), so enabling MFA does not pull in a new native or
pip dependency. Compatible with Google Authenticator, Authy, 1Password, etc.

Secrets are per-user, stored base32-encoded in users.totp_secret. Enrolment is
two-phase: generate a secret + provisioning URI, then confirm one live code
before totp_enabled is flipped on (so a user can never lock themselves out with
a mis-scanned QR). Verification allows a +/-1 time-step window to tolerate clock
skew, and is done with hmac.compare_digest to stay timing-safe.

The one exception to "stdlib only" is qr_svg_data_uri, which renders the
provisioning URI as an SVG QR via the pure-Python `qrcode` package (approved
dependency; SVG factory only, no Pillow). It is strictly optional: the import
is deferred and every failure degrades to None, so TOTP itself never gains a
hard dependency and enrolment keeps working on the manual key alone.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import struct
import time
from urllib.parse import quote

# Standard TOTP parameters (matches authenticator-app defaults).
_DIGITS = 6
_PERIOD = 30
_ALGORITHM = "SHA1"
# Number of time-steps to check on each side of "now" to tolerate clock skew.
_SKEW_STEPS = 1


def generate_secret(num_bytes: int = 20) -> str:
    """Return a fresh base32-encoded TOTP secret (no padding, upper-case).

    20 random bytes = 160 bits, the RFC 6238 recommended secret length for
    SHA-1. base32 is what authenticator apps expect in the otpauth URI.
    """
    raw = os.urandom(num_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _b32decode(secret: str) -> bytes:
    """Decode a (possibly unpadded) base32 secret back to raw bytes."""
    s = secret.strip().replace(" ", "").upper()
    # Restore base32 padding to a multiple of 8 characters.
    pad = (-len(s)) % 8
    return base64.b32decode(s + ("=" * pad))


def _hotp(secret: str, counter: int) -> str:
    """RFC 4226 HOTP value for a given counter, as a zero-padded string."""
    key = _b32decode(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = ((digest[offset] & 0x7F) << 24
              | (digest[offset + 1] & 0xFF) << 16
              | (digest[offset + 2] & 0xFF) << 8
              | (digest[offset + 3] & 0xFF))
    return str(binary % (10 ** _DIGITS)).zfill(_DIGITS)


def current_code(secret: str, at: float | None = None) -> str:
    """Return the TOTP code valid at the given unix time (default now)."""
    now = time.time() if at is None else at
    counter = int(now // _PERIOD)
    return _hotp(secret, counter)


def verify_code_step(secret: str, code: str, at: float | None = None) -> int | None:
    """Timing-safe TOTP verification returning the MATCHED time-step (or None).

    The returned counter step lets callers enforce single-use (RFC 6238 §5.2):
    reject a code whose step is <= the last consumed step, and persist the step
    on success. Returns None for any non-match or malformed input.
    """
    if not secret or not code:
        return None
    code = str(code).strip().replace(" ", "")
    if not code.isdigit() or len(code) != _DIGITS:
        return None
    now = time.time() if at is None else at
    counter = int(now // _PERIOD)
    for step in range(-_SKEW_STEPS, _SKEW_STEPS + 1):
        try:
            candidate = _hotp(secret, counter + step)
        except Exception:
            return None
        if hmac.compare_digest(candidate, code):
            return counter + step
    return None


def verify_code(secret: str, code: str, at: float | None = None) -> bool:
    """Timing-safe TOTP verification with a +/-1 step skew window.

    Returns False for any malformed input rather than raising. NOTE: this is
    stateless and does NOT enforce single-use — callers that need replay
    protection must use verify_code_step and track the consumed step.
    """
    return verify_code_step(secret, code, at) is not None


def provisioning_uri(secret: str, account_name: str, issuer: str) -> str:
    """Build an otpauth:// URI for QR-code enrolment in authenticator apps."""
    label = quote(f"{issuer}:{account_name}")
    params = (
        f"secret={secret}"
        f"&issuer={quote(issuer)}"
        f"&algorithm={_ALGORITHM}"
        f"&digits={_DIGITS}"
        f"&period={_PERIOD}"
    )
    return f"otpauth://totp/{label}?{params}"


def qr_svg_data_uri(otpauth_uri: str) -> str | None:
    """Render an otpauth URI as an SVG QR code, returned as a data: URI.

    Pure-Python `qrcode` with its SVG path factory ONLY — no raster path, no
    PIL. SvgPathFillImage paints a white background rect and the default
    4-module border supplies the spec quiet zone, so the QR scans on a dark
    theme regardless of what the page puts behind it.

    Contract: NEVER raises. The QR is a convenience layered over the manual
    setup key; on an appliance where the package is missing or broken this
    returns None and the enrolment payload simply omits the field. The import
    lives inside the function so a broken/absent package cannot take down
    module import (and so tests can simulate absence via sys.modules).

    The render is deterministic for a given URI (same modules, same path),
    which the route tests rely on to prove the QR encodes exactly the
    otpauth URI returned alongside it.
    """
    try:
        import qrcode
        import qrcode.image.svg
    except Exception:
        return None
    try:
        img = qrcode.make(
            otpauth_uri,
            image_factory=qrcode.image.svg.SvgPathFillImage,
            border=4,
        )
        buf = io.BytesIO()
        img.save(buf)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"
    except Exception:
        return None
