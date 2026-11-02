"""Orb — At-rest encryption for uploaded PHI media (Lens images, Scribe audio).

Envelope format (single file, self-describing):

    b"ORBENC1\\n" + 12-byte nonce + AES-256-GCM ciphertext(+16-byte tag)

The key is derived from config.UPLOAD_ENCRYPTION_KEY via SHA-256 → 32 bytes, so
the operator can supply any-length key material (e.g. `openssl rand -hex 32`).
AES-256-GCM is authenticated: tampering with the file on disk fails decryption
rather than returning corrupted plaintext.

Encryption is enabled when UPLOAD_ENCRYPTION_KEY is set AND the `cryptography`
package is importable. Without them:
  * in the prod profile a write REFUSES (RuntimeError) unless the operator set
    ORB_REQUIRE_UPLOAD_ENCRYPTION=0;
  * in the dev profile the file is written in plaintext and a one-time warning
    is logged.
Reads are format-sniffing: any file beginning with the ORBENC1 magic is
decrypted; anything else is returned as-is. This means turning encryption on
does not orphan previously-written plaintext files, and turning it off still
lets you read already-encrypted files (as long as the key is still configured).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from contextlib import contextmanager

from config import UPLOAD_ENCRYPTION_KEY

logger = logging.getLogger("orb.crypto")

_MAGIC = b"ORBENC1\n"
_NONCE_LEN = 12

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAVE_AESGCM = True
except Exception:  # pragma: no cover - depends on deploy env
    AESGCM = None  # type: ignore
    _HAVE_AESGCM = False

_warned = False


def is_encryption_enabled() -> bool:
    """True when uploads will actually be encrypted at rest."""
    return bool(UPLOAD_ENCRYPTION_KEY) and _HAVE_AESGCM


def _warn_once() -> None:
    global _warned
    if _warned:
        return
    _warned = True
    if not UPLOAD_ENCRYPTION_KEY:
        logger.warning(
            "Upload encryption DISABLED: ORB_UPLOAD_ENCRYPTION_KEY is not set. "
            "Uploaded PHI media is stored in cleartext."
        )
    elif not _HAVE_AESGCM:
        logger.warning(
            "Upload encryption DISABLED: 'cryptography' package unavailable. "
            "Uploaded PHI media is stored in cleartext. Install requirements.txt."
        )


def _derive_key() -> bytes:
    """32-byte AES key derived from the configured key material."""
    assert UPLOAD_ENCRYPTION_KEY is not None
    return hashlib.sha256(UPLOAD_ENCRYPTION_KEY.encode("utf-8")).digest()


def is_encrypted_blob(data: bytes) -> bool:
    """Cheap magic-byte sniff used by the read path."""
    return data[: len(_MAGIC)] == _MAGIC


def _require_encryption() -> bool:
    """Whether encryption is enforced (hard-fail) rather than best-effort.

    Read live (not cached) so tests/deploys can toggle it. When true, a request
    to encrypt PHI that cannot actually be encrypted raises instead of silently
    storing cleartext — closing the fail-open gap for regulated deployments.

    ON BY DEFAULT IN THE PROD PROFILE. It used to default off everywhere, so a
    hospital appliance missing its upload key stored scans and voice recordings
    in the clear. An explicit ORB_REQUIRE_UPLOAD_ENCRYPTION=0 still opts out;
    the dev profile keeps the old default so a laptop runs without a key.
    """
    raw = os.getenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    import config

    return getattr(config, "DEPLOYMENT_PROFILE", "prod") == "prod"


def encrypt_bytes(plaintext: bytes) -> bytes:
    """Return an encrypted envelope, or the plaintext unchanged if disabled.

    A one-time warning is logged when encryption is unavailable. When it is
    required (see `_require_encryption`), an unavailable cipher is a hard error
    (RuntimeError) rather than a silent cleartext write — so an operator who
    intends encryption never unknowingly persists PHI in the clear.
    """
    if not is_encryption_enabled():
        if _require_encryption():
            raise RuntimeError(
                "Upload encryption is required (prod profile or "
                "ORB_REQUIRE_UPLOAD_ENCRYPTION=1) but unavailable (missing "
                "ORB_UPLOAD_ENCRYPTION_KEY or the 'cryptography' package). Refusing "
                "to store PHI in cleartext."
            )
        _warn_once()
        return plaintext
    import os

    nonce = os.urandom(_NONCE_LEN)
    aes = AESGCM(_derive_key())
    ct = aes.encrypt(nonce, plaintext, None)
    return _MAGIC + nonce + ct


def decrypt_bytes(data: bytes) -> bytes:
    """Decrypt an ORBENC1 envelope; pass through anything that isn't one.

    Raises ValueError if the file is an encrypted envelope but no key/driver is
    available, or if authentication fails (tampering / wrong key) — callers
    should surface this rather than serving corrupt bytes.
    """
    if not is_encrypted_blob(data):
        return data
    if not UPLOAD_ENCRYPTION_KEY or not _HAVE_AESGCM:
        raise ValueError(
            "File is encrypted but ORB_UPLOAD_ENCRYPTION_KEY / cryptography is "
            "unavailable; cannot decrypt."
        )
    body = data[len(_MAGIC) :]
    nonce, ct = body[:_NONCE_LEN], body[_NONCE_LEN:]
    aes = AESGCM(_derive_key())
    try:
        return aes.decrypt(nonce, ct, None)
    except Exception as e:
        raise ValueError(f"Upload decryption failed (tampered or wrong key): {e}") from e


def write_encrypted(path, plaintext: bytes) -> None:
    """Publish a complete upload atomically, private from its first disk byte.

    Stage in the destination directory so replacement stays on one filesystem.
    A failed write leaves the previous destination intact and removes the
    private staging file. Replacing a symlink replaces the link itself rather
    than following it to an unrelated file. The envelope format is unchanged.
    """
    data = encrypt_bytes(plaintext)
    destination = os.path.abspath(os.fsdecode(path))
    fd, temporary = tempfile.mkstemp(
        prefix=".orb_upload_", suffix=".tmp", dir=os.path.dirname(destination)
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1  # the file object now owns the descriptor, including errors
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass  # atomic replacement consumed the staging path
        except OSError:
            logger.warning("Upload staging file cleanup failed", exc_info=True)


def read_decrypted(path) -> bytes:
    """Read a file and transparently decrypt it if it's an ORBENC1 envelope."""
    with open(path, "rb") as f:
        return decrypt_bytes(f.read())


@contextmanager
def materialize_plaintext(path):
    """Yield a filesystem path that downstream tools (Whisper, vision LLM) can
    read directly.

    If ``path`` is an ORBENC1 envelope, it is decrypted into a temporary file
    (0o600, same suffix) which is yielded and then deleted. If it is already
    plaintext, ``path`` is yielded unchanged with no copy. This lets at-rest
    encryption sit under path-based ML consumers without changing their APIs.
    """
    import os
    import tempfile

    with open(path, "rb") as f:
        data = f.read()
    if not is_encrypted_blob(data):
        yield path
        return
    plain = decrypt_bytes(data)
    suffix = os.path.splitext(str(path))[1]
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="orb_dec_")
    try:
        with os.fdopen(fd, "wb") as t:
            t.write(plain)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        yield tmp
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
