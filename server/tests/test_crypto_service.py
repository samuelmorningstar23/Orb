"""Tests for at-rest upload encryption (services/crypto_service).

These exercise the real AES-256-GCM path when `cryptography` + a key are
available, and the graceful plaintext passthrough otherwise.
"""
import importlib
import os

import pytest


def _reload_with_key(monkeypatch, key):
    """Reload crypto_service with UPLOAD_ENCRYPTION_KEY patched in config."""
    import config
    monkeypatch.setattr(config, "UPLOAD_ENCRYPTION_KEY", key, raising=False)
    import services.crypto_service as cs
    importlib.reload(cs)
    return cs


def test_roundtrip_when_enabled(monkeypatch):
    cs = _reload_with_key(monkeypatch, "test-upload-key-material")
    if not cs.is_encryption_enabled():
        pytest.skip("cryptography not installed; encryption path unavailable")
    plaintext = b"PHI: patient chest x-ray bytes \x00\x01\x02"
    blob = cs.encrypt_bytes(plaintext)
    assert cs.is_encrypted_blob(blob)
    assert blob != plaintext
    assert cs.decrypt_bytes(blob) == plaintext


def test_tamper_detection(monkeypatch):
    cs = _reload_with_key(monkeypatch, "test-upload-key-material")
    if not cs.is_encryption_enabled():
        pytest.skip("cryptography not installed")
    blob = bytearray(cs.encrypt_bytes(b"hello world"))
    # Flip a byte in the ciphertext body -> GCM auth must fail
    blob[-1] ^= 0xFF
    with pytest.raises(ValueError):
        cs.decrypt_bytes(bytes(blob))


def test_plaintext_passthrough_when_disabled(monkeypatch):
    # An explicit opt-out: the prod profile now requires encryption by default
    # (F111), so passthrough is only what an operator asks for.
    monkeypatch.setenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "0")
    cs = _reload_with_key(monkeypatch, None)
    assert cs.is_encryption_enabled() is False
    data = b"not encrypted"
    assert cs.encrypt_bytes(data) == data
    assert cs.decrypt_bytes(data) == data


def test_prod_profile_refuses_cleartext_without_a_key(monkeypatch):
    # F111: a hospital appliance missing its upload key used to store scans and
    # voice recordings in the clear. In the prod profile that is now an error.
    import config
    monkeypatch.delenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", raising=False)
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "prod", raising=False)
    cs = _reload_with_key(monkeypatch, None)
    with pytest.raises(RuntimeError, match="Upload encryption is required"):
        cs.encrypt_bytes(b"PHI that must not be written in the clear")


def test_dev_profile_still_passes_through_without_a_key(monkeypatch):
    import config
    monkeypatch.delenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", raising=False)
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "dev", raising=False)
    cs = _reload_with_key(monkeypatch, None)
    assert cs.encrypt_bytes(b"laptop data") == b"laptop data"


def test_decrypt_passes_through_legacy_plaintext(monkeypatch):
    cs = _reload_with_key(monkeypatch, "test-upload-key-material")
    # A pre-encryption plaintext file (no ORBENC1 magic) is returned unchanged
    assert cs.decrypt_bytes(b"legacy plaintext image") == b"legacy plaintext image"


def test_write_and_read_file_roundtrip(monkeypatch, tmp_path):
    cs = _reload_with_key(monkeypatch, "test-upload-key-material")
    p = tmp_path / "scan.bin"
    payload = b"audio-bytes-1234"
    cs.write_encrypted(str(p), payload)
    on_disk = p.read_bytes()
    if cs.is_encryption_enabled():
        assert cs.is_encrypted_blob(on_disk)
        assert on_disk != payload
    assert cs.read_decrypted(str(p)) == payload


def test_materialize_plaintext_yields_readable_path(monkeypatch, tmp_path):
    cs = _reload_with_key(monkeypatch, "test-upload-key-material")
    p = tmp_path / "audio.webm"
    payload = b"whisper-needs-a-real-path"
    cs.write_encrypted(str(p), payload)
    with cs.materialize_plaintext(str(p)) as read_path:
        with open(read_path, "rb") as f:
            assert f.read() == payload


def teardown_module(module):
    # Restore crypto_service to its config-driven default so later tests/modules
    # see the real environment rather than a monkeypatched key.
    import services.crypto_service as cs
    importlib.reload(cs)


def test_prod_profile_refuses_a_cleartext_upload_by_default(monkeypatch):
    """Upload encryption used to be opt-in everywhere, so a prod appliance with
    no upload key stored scans and recordings in the clear."""
    cs = _reload_with_key(monkeypatch, None)
    monkeypatch.delenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", raising=False)
    monkeypatch.setattr("config.DEPLOYMENT_PROFILE", "prod")
    with pytest.raises(RuntimeError, match="Refusing to store PHI in cleartext"):
        cs.encrypt_bytes(b"scan")


def test_prod_profile_explicit_opt_out_still_writes(monkeypatch):
    cs = _reload_with_key(monkeypatch, None)
    monkeypatch.setenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "0")
    monkeypatch.setattr("config.DEPLOYMENT_PROFILE", "prod")
    assert cs.encrypt_bytes(b"scan") == b"scan"


def test_dev_profile_keeps_the_plaintext_default(monkeypatch):
    cs = _reload_with_key(monkeypatch, None)
    monkeypatch.delenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", raising=False)
    monkeypatch.setattr("config.DEPLOYMENT_PROFILE", "dev")
    assert cs.encrypt_bytes(b"scan") == b"scan"
