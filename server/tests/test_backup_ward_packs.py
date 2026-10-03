"""scripts/backup.sh carries the latest ward print packs, encrypted (F082).

The downtime packs exist for the day the appliance cannot be used, so a copy
must leave the box with the nightly set. Every other file in that set is
encrypted, so the packs go in as an openssl archive under the database key and
never as plaintext. These tests run the real script against throwaway files.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_backup_receipt import (  # noqa: E402
    BACKUP_SH, TEST_KEY, VENV_PY, _fake_backup, _make_db,
)

pytestmark = pytest.mark.skipif(
    not BACKUP_SH.exists() or not VENV_PY.exists() or not Path("/usr/bin/openssl").exists(),
    reason="needs scripts/backup.sh, the server venv and openssl",
)

PACK_TEXT = "<!DOCTYPE html><p>MRN-PACK-7731 Ward 4 census</p>"


def _env(tmp_path: Path, live: Path, out: Path, exports: Path, offsite: Path) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "ORB_DEPLOYMENT_PROFILE": "dev",
        "ORB_JWT_SECRET": "suite-only-secret-0123456789abcdef0123456789",
        "ORB_DB_ENCRYPTION_KEY": TEST_KEY,
        "ORB_DB_PATH": str(live),
        "ORB_BACKUP_DIR": str(out),
        "ORB_UPLOADS_DIR": str(tmp_path / "no-uploads"),
        "ORB_FHIR_EXPORT_DIR": f" {exports}\r",  # stripped like the app does
        "ORB_BACKUP_OFFSITE_DIR": str(offsite),
        "ORB_BACKUP_MIN_BYTES": "1",
        "ORB_BACKUP_RETAIN": "1",
        "ORB_OLLAMA_BASE_URL": "http://127.0.0.1:9",
    }


def test_latest_packs_are_archived_encrypted_copied_off_box_and_rotated(tmp_path):
    live = _make_db(tmp_path / "live.db")
    out, exports, offsite = tmp_path / "backups", tmp_path / "exports", tmp_path / "offsite"
    for d in (out, exports, offsite):
        d.mkdir()
    (exports / "ward-pack.Ward_4.abcdef12.latest.html").write_text(PACK_TEXT)
    # Timestamped history and FHIR bundles stay on the appliance.
    (exports / "ward-pack.Ward_4.abcdef12.20261003-010000.html").write_text("history")
    (exports / "PAT1.12345678.fhir.json").write_text("{}")
    # An older verified backup whose packs archive must rotate out with it.
    old = _fake_backup(out, "20260901-020000", ok=True, age_s=3 * 86400)
    old_packs = out / "orb-backup-20260901-020000-ward-packs.tar.enc"
    old_packs.write_bytes(b"old")

    result = subprocess.run(["/bin/bash", str(BACKUP_SH)], capture_output=True, text=True,
                            env=_env(tmp_path, live, out, exports, offsite), timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr

    archives = list(out.glob("orb-backup-2*-ward-packs.tar.enc"))
    assert len(archives) == 1, list(out.iterdir())
    archive = archives[0]
    assert (archive.stat().st_mode & 0o777) == 0o600
    assert b"MRN-PACK-7731" not in archive.read_bytes()  # never plaintext

    # Decrypts with the database key to exactly the latest pack.
    restore = tmp_path / "restore"
    restore.mkdir()
    dec = subprocess.run(
        f'openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:ORB_DB_ENCRYPTION_KEY '
        f'-in "{archive}" | tar -xf - -C "{restore}"',
        shell=True, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "ORB_DB_ENCRYPTION_KEY": TEST_KEY}, timeout=60)
    assert dec.returncode == 0, dec.stderr
    assert sorted(p.name for p in restore.iterdir()) == ["ward-pack.Ward_4.abcdef12.latest.html"]
    assert (restore / "ward-pack.Ward_4.abcdef12.latest.html").read_text() == PACK_TEXT

    assert (offsite / archive.name).exists()
    # RETAIN=1: the older backup goes, and its packs archive goes with it.
    assert not old.exists() and not old_packs.exists()


def test_no_packs_is_a_warning_not_a_failure(tmp_path):
    live = _make_db(tmp_path / "live.db")
    out, exports, offsite = tmp_path / "backups", tmp_path / "exports", tmp_path / "offsite"
    for d in (out, exports, offsite):
        d.mkdir()

    result = subprocess.run(["/bin/bash", str(BACKUP_SH)], capture_output=True, text=True,
                            env=_env(tmp_path, live, out, exports, offsite), timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no ward print packs" in result.stderr
    assert not list(out.glob("*-ward-packs.tar.enc"))
