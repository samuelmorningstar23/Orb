"""Orb — Application-level field encryption for PHI-derived DB columns.

Whole-database encryption at rest (SQLCipher) shipped on 2026-07-30 and fails
closed since 2026-10-03 (see database.py). This module predates it and still
earns its place: it encrypts the PHI-derived columns of the persistent world
model and the warm decision cache with AES-256-GCM before they are written, so
those values stay unreadable in an export or a copied row even on a deployment
without a database key. It reuses the same authenticated cipher as the upload
path (services/crypto_service).

It is deliberately string-in / string-out (base64 envelope) so it drops into a
TEXT column with no schema change:

    stored = encrypt_field(json.dumps(payload))      # -> "ORBF1:<b64>" or plaintext
    payload = json.loads(decrypt_field(stored))      # transparent both ways

Key source: ORB_FIELD_ENCRYPTION_KEY, falling back to ORB_DB_ENCRYPTION_KEY so an
operator who already set a DB key gets field encryption for free. When neither is
set (or `cryptography` is unavailable) values are stored in cleartext, exactly as
today — turning the key on does not orphan previously-written plaintext rows
(reads sniff the envelope prefix), and turning it off still reads already-
encrypted rows as long as the key remains configured.

Fail-closed option: ORB_REQUIRE_FIELD_ENCRYPTION=1 makes an unavailable cipher a
hard error rather than a silent cleartext write, for regulated deployments.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

logger = logging.getLogger("orb.field_crypto")

_PREFIX = "ORBF1:"          # marks an encrypted field value
_NONCE_LEN = 12

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAVE_AESGCM = True
except Exception:  # pragma: no cover
    AESGCM = None  # type: ignore
    _HAVE_AESGCM = False

_warned = False


def _key_material() -> str | None:
    return os.getenv("ORB_FIELD_ENCRYPTION_KEY") or os.getenv("ORB_DB_ENCRYPTION_KEY")


def is_field_encryption_enabled() -> bool:
    return bool(_key_material()) and _HAVE_AESGCM


def _require() -> bool:
    return os.getenv("ORB_REQUIRE_FIELD_ENCRYPTION", "").strip().lower() in ("1", "true", "yes", "on")


def _derive_key() -> bytes:
    return hashlib.sha256((_key_material() or "").encode("utf-8")).digest()


def _warn_once() -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(
        "Field encryption DISABLED (no ORB_FIELD_ENCRYPTION_KEY/ORB_DB_ENCRYPTION_KEY "
        "or 'cryptography' unavailable): PHI-derived cache/world-model columns are "
        "stored in cleartext. See database.py encryption-at-rest note."
    )


def is_encrypted_field(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt_field(plaintext: str) -> str:
    """Return an encrypted envelope string, or the plaintext unchanged if
    encryption is unavailable (unless ORB_REQUIRE_FIELD_ENCRYPTION forces a hard
    error)."""
    if plaintext is None:
        return plaintext
    if not is_field_encryption_enabled():
        if _require():
            raise RuntimeError(
                "ORB_REQUIRE_FIELD_ENCRYPTION is set but field encryption is "
                "unavailable (missing key or 'cryptography'). Refusing to store "
                "PHI-derived data in cleartext."
            )
        _warn_once()
        return plaintext
    nonce = os.urandom(_NONCE_LEN)
    aes = AESGCM(_derive_key())
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_field(value: str) -> str:
    """Decrypt an ORBF1 envelope; pass through anything that isn't one."""
    if not is_encrypted_field(value):
        return value
    if not _key_material() or not _HAVE_AESGCM:
        raise ValueError(
            "Field value is encrypted but ORB_FIELD_ENCRYPTION_KEY / cryptography "
            "is unavailable; cannot decrypt."
        )
    raw = base64.b64decode(value[len(_PREFIX):].encode("ascii"))
    nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    aes = AESGCM(_derive_key())
    try:
        return aes.decrypt(nonce, ct, None).decode("utf-8")
    except Exception as e:
        raise ValueError(f"Field decryption failed (tampered or wrong key): {e}")
