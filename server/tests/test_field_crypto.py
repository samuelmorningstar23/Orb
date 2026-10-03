"""Application-level field encryption tests (PHI-derived DB columns)."""
import importlib
import os
from unittest import mock

import pytest


def _fresh(monkeypatch, key="unit-test-field-key-abc123"):
    monkeypatch.setenv("ORB_FIELD_ENCRYPTION_KEY", key)
    monkeypatch.delenv("ORB_REQUIRE_FIELD_ENCRYPTION", raising=False)
    import services.field_crypto as fc
    importlib.reload(fc)
    return fc


def test_roundtrip_when_key_set(monkeypatch):
    fc = _fresh(monkeypatch)
    if not fc._HAVE_AESGCM:
        pytest.skip("cryptography not installed")
    plain = '{"patient":"Kavya","dx":"sepsis","na":132}'
    enc = fc.encrypt_field(plain)
    assert enc != plain
    assert fc.is_encrypted_field(enc)
    assert "Kavya" not in enc  # ciphertext, not cleartext
    assert fc.decrypt_field(enc) == plain


def test_passthrough_when_no_key(monkeypatch):
    monkeypatch.delenv("ORB_FIELD_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ORB_DB_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ORB_REQUIRE_FIELD_ENCRYPTION", raising=False)
    import services.field_crypto as fc
    importlib.reload(fc)
    plain = '{"a":1}'
    assert fc.encrypt_field(plain) == plain          # cleartext, unchanged
    assert not fc.is_encrypted_field(plain)
    assert fc.decrypt_field(plain) == plain           # transparent


def test_reading_encrypted_still_works_after_key_present(monkeypatch):
    fc = _fresh(monkeypatch)
    if not fc._HAVE_AESGCM:
        pytest.skip("cryptography not installed")
    enc = fc.encrypt_field("secret")
    # Simulate a later read with the key still configured.
    assert fc.decrypt_field(enc) == "secret"


def test_require_flag_hard_fails_without_cipher(monkeypatch):
    monkeypatch.delenv("ORB_FIELD_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ORB_DB_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("ORB_REQUIRE_FIELD_ENCRYPTION", "1")
    import services.field_crypto as fc
    importlib.reload(fc)
    with pytest.raises(RuntimeError):
        fc.encrypt_field("phi")


def test_tampered_ciphertext_fails_authentication(monkeypatch):
    fc = _fresh(monkeypatch)
    if not fc._HAVE_AESGCM:
        pytest.skip("cryptography not installed")
    enc = fc.encrypt_field("secret")
    tampered = enc[:-4] + ("AAAA" if not enc.endswith("AAAA") else "BBBB")
    with pytest.raises(ValueError):
        fc.decrypt_field(tampered)


@pytest.fixture(autouse=True)
def _restore_module():
    yield
    # Reload with the ambient env so other tests see the default module state.
    import services.field_crypto as fc
    importlib.reload(fc)
