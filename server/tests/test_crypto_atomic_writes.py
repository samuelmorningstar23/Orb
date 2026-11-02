"""Uploaded PHI is private before publication; failures preserve old media."""
import os
import stat

import pytest

from services import crypto_service as crypto


@pytest.fixture
def capture_staging(monkeypatch):
    created = []
    create = crypto.tempfile.mkstemp

    def capture(*args, **kwargs):
        fd, path = create(*args, **kwargs)
        created.append((fd, path, stat.S_IMODE(os.fstat(fd).st_mode)))
        return fd, path

    monkeypatch.setattr(crypto.tempfile, "mkstemp", capture)
    return created


@pytest.mark.parametrize("encrypted", [False, True])
def test_complete_upload_is_private_before_atomic_publication(
    tmp_path, monkeypatch, capture_staging, encrypted,
):
    monkeypatch.setattr(crypto, "UPLOAD_ENCRYPTION_KEY", "test-media-key" if encrypted else None)
    monkeypatch.setenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "0")
    if encrypted and not crypto._HAVE_AESGCM:
        pytest.skip("cryptography not installed")
    target = tmp_path / "scan.png"
    target.write_bytes(b"previous complete upload")
    payload = b"new confidential scan"
    replace = os.replace

    def inspect_publication(source, destination):
        assert target.read_bytes() == b"previous complete upload"
        assert os.path.dirname(source) == str(tmp_path)
        assert stat.S_IMODE(os.stat(source).st_mode) == 0o600
        with open(source, "rb") as stream:
            staged = stream.read()
        assert crypto.is_encrypted_blob(staged) is encrypted
        assert crypto.decrypt_bytes(staged) == payload
        replace(source, destination)

    monkeypatch.setattr(crypto.os, "replace", inspect_publication)
    previous_umask = os.umask(0)
    try:
        crypto.write_encrypted(target, payload)
    finally:
        os.umask(previous_umask)
    assert capture_staging[0][2] == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert crypto.read_decrypted(target) == payload
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("fault", ["open", "write", "sync", "replace"])
def test_failed_upload_removes_staging_and_preserves_the_destination(
    tmp_path, monkeypatch, capture_staging, fault,
):
    monkeypatch.setattr(crypto, "encrypt_bytes", lambda data: b"encrypted:" + data)
    target = tmp_path / "audio.webm"
    target.write_bytes(b"previous complete upload")
    fdopen = os.fdopen

    def fail(*args, **kwargs):
        raise OSError("simulated media storage failure")

    class PartialWriter:
        def __init__(self, fd, mode):
            self.stream = fdopen(fd, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            self.stream.write(data[:4])
            fail()

    if fault == "open":
        monkeypatch.setattr(crypto.os, "fdopen", fail)
    elif fault == "write":
        monkeypatch.setattr(crypto.os, "fdopen", PartialWriter)
    elif fault == "sync":
        monkeypatch.setattr(crypto.os, "fsync", fail)
    else:
        monkeypatch.setattr(crypto.os, "replace", fail)

    with pytest.raises(OSError, match="simulated media storage failure"):
        crypto.write_encrypted(str(target), b"new audio")
    assert target.read_bytes() == b"previous complete upload"
    assert list(tmp_path.iterdir()) == [target]
    assert len(capture_staging) == 1
    with pytest.raises(OSError):
        os.fstat(capture_staging[0][0])


def test_publication_replaces_a_symlink_without_overwriting_its_referent(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "encrypt_bytes", lambda data: data)
    referent = tmp_path / "unrelated.bin"
    referent.write_bytes(b"do not overwrite")
    target = tmp_path / "scan.png"
    target.symlink_to(referent)
    crypto.write_encrypted(target, b"new scan")
    assert not target.is_symlink()
    assert target.read_bytes() == b"new scan"
    assert referent.read_bytes() == b"do not overwrite"


def test_encryption_refusal_does_not_touch_the_destination(tmp_path, monkeypatch, capture_staging):
    monkeypatch.setattr(crypto, "UPLOAD_ENCRYPTION_KEY", None)
    monkeypatch.setenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "1")
    target = tmp_path / "scan.png"
    target.write_bytes(b"previous upload")
    with pytest.raises(RuntimeError, match="Upload encryption is required"):
        crypto.write_encrypted(target, b"new scan")
    assert target.read_bytes() == b"previous upload"
    assert capture_staging == []
