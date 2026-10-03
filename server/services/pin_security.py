"""Orb — Signing-PIN security helpers.

Centralizes PIN hashing/verification for the agentic-order sign-off flow (and
any future PIN-gated action). Properties:

  * New PINs are hashed with salted bcrypt (auth.hash_password) — never a bare
    SHA-256.
  * Verification is constant-time on both hash formats: bcrypt hashes ('$2…')
    go through auth.verify_password (bcrypt.checkpw), and legacy unsalted
    SHA-256 hex digests are compared with hmac.compare_digest. Legacy hashes
    are upgraded to bcrypt on first successful verification (best-effort).
  * A NULL/empty pin_hash NEVER verifies — the caller must fail closed and
    tell the user no PIN is provisioned (universal-PIN seeding was removed;
    provisioning is via POST /api/admin/users/{id}/pin).
  * A per-user sliding-window attempt limiter (5 failures / 15 minutes →
    locked) throttles online PIN guessing, mirroring the login limiter in
    auth_routes.py.

Limiter caveat (same as the login limiter it mirrors): this state is
per-process and lost on restart, so a multi-worker deploy would need a
persistent attempts table. The server currently runs single-process (uvicorn
workers=1), so the in-memory limiter is functional for the present deploy.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections import defaultdict
from typing import Any, Mapping, Optional

from auth import hash_password, verify_password

logger = logging.getLogger("orb.pin_security")

# user_id -> [failure timestamps], sliding window.
PIN_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
MAX_PIN_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 900  # 15 minutes
# Hard cap on tracked users to bound memory (mirrors auth_routes._MAX_TRACKED_IPS).
_MAX_TRACKED_USERS = 10000


def hash_pin(pin: str) -> str:
    """Hash a PIN for storage in users.pin_hash (salted bcrypt)."""
    return hash_password(pin)


def _pin_hash_of(user_row: Any) -> Optional[str]:
    """Extract pin_hash from a sqlite3.Row or a plain dict."""
    if user_row is None:
        return None
    if isinstance(user_row, Mapping):
        return user_row.get("pin_hash")
    try:  # sqlite3.Row
        return user_row["pin_hash"]
    except (IndexError, KeyError):
        return None


def verify_pin(user_row: Any, pin: str) -> bool:
    """Constant-time PIN verification against a users row.

    Returns False (fail closed) when the row has no pin_hash — callers should
    surface a distinct "no PIN provisioned" error in that case rather than a
    generic "invalid PIN". Legacy unsalted SHA-256 hashes verify via
    hmac.compare_digest and are upgraded to bcrypt on success (best-effort:
    an upgrade failure never fails the verification).
    """
    stored = _pin_hash_of(user_row)
    if not stored or not pin:
        return False

    if stored.startswith("$2"):
        return verify_password(pin, stored)

    # Legacy unsalted SHA-256 hex digest.
    legacy = hashlib.sha256(pin.encode()).hexdigest()
    if not hmac.compare_digest(legacy, stored):
        return False

    # Upgrade-on-success so the unsalted digest ages out of the users table.
    user_id = None
    try:
        user_id = user_row["id"] if not isinstance(user_row, Mapping) else user_row.get("id")
        if user_id:
            from database import db_session
            with db_session() as db:
                db.execute(
                    "UPDATE users SET pin_hash = ? WHERE id = ? AND pin_hash = ?",
                    (hash_pin(pin), user_id, stored),
                )
            logger.info("PIN hash upgraded to bcrypt for user %s", user_id)
    except Exception as e:
        logger.warning("Failed to upgrade PIN hash for user %s: %s", user_id, e)
    return True


# ── Per-user attempt limiter (mirrors auth_routes login limiter) ─────────────

def _prune_pin_attempts(now: float) -> None:
    """Drop user buckets whose failures have all aged out of the window."""
    stale = [uid for uid, ts in PIN_ATTEMPTS.items()
             if not ts or now - ts[-1] >= LOCKOUT_DURATION_SECONDS]
    for uid in stale:
        del PIN_ATTEMPTS[uid]
    # If still oversized (active spray), evict the oldest-touched buckets.
    if len(PIN_ATTEMPTS) > _MAX_TRACKED_USERS:
        for uid in sorted(PIN_ATTEMPTS, key=lambda k: PIN_ATTEMPTS[k][-1])[:len(PIN_ATTEMPTS) - _MAX_TRACKED_USERS]:
            del PIN_ATTEMPTS[uid]


def is_pin_locked(user_id: str) -> bool:
    """True when the user has exhausted their PIN attempts in the window."""
    if not user_id:
        return False
    now = time.time()
    _prune_pin_attempts(now)
    PIN_ATTEMPTS[user_id] = [t for t in PIN_ATTEMPTS[user_id] if now - t < LOCKOUT_DURATION_SECONDS]
    return len(PIN_ATTEMPTS[user_id]) >= MAX_PIN_ATTEMPTS


def record_pin_failure(user_id: str) -> None:
    if user_id:
        PIN_ATTEMPTS[user_id].append(time.time())


def clear_pin_failures(user_id: str) -> None:
    PIN_ATTEMPTS.pop(user_id, None)
