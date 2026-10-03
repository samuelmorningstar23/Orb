"""The backup receipt: the one thing that lets the status page say "verified".

server/services/ops_health_service.py calls a backup verified only when a
receipt beside it, <stamp>.verified.json (schema orb.backup.verification/1),
says it passed. Until 2026-10-02 nothing wrote one: the reader existed, the
writer did not, so a good nightly backup could only ever be shown as
"unchecked". These tests drive the real writer (scripts/lib/verify_restore.py
--receipt) and the real producer (scripts/backup.sh) against throwaway
SQLCipher files, then read the result back through the status page.

Nothing here touches the real database, the real backups/ directory or the
real encryption key.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"
VENV_PY = REPO_ROOT / "server" / ".venv" / "bin" / "python"
TEST_KEY = "receipt-test-key-not-the-real-one"

pytest.importorskip("sqlcipher3", reason="the verifier opens SQLCipher files")
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
import verify_restore as vr  # noqa: E402

from services import ops_health_service  # noqa: E402


def _make_db(path: Path, *, patients: int = 3) -> Path:
    """A SQLCipher file shaped like an Orb database, as far as the verifier reads.

    `patients=0` builds the empty-but-valid database that must FAIL: it opens,
    integrity_check says ok, and it holds nothing (the zero-byte-backup lesson).
    """
    from sqlcipher3 import dbapi2 as sq

    conn = sq.connect(str(path))
    conn.execute(f"PRAGMA key = \"x'{hashlib.sha256(TEST_KEY.encode()).hexdigest()}'\"")
    conn.executescript(
        """
        CREATE TABLE patients (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE vitals (id INTEGER PRIMARY KEY, patient_id TEXT);
        CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, role TEXT,
                            password_hash TEXT, is_active INTEGER DEFAULT 1);
        CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, prev_hash TEXT, row_hash TEXT);
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT);
        """
    )
    for table in vr.ALL_MIGRATED_TABLES:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, v TEXT)")
    for table in vr.SEEDED_REFERENCE:
        conn.execute(f"INSERT INTO {table} (v) VALUES ('seeded')")
    for v in range(1, vr.expected_schema_version() + 1):
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (v, f"m{v}"))
    conn.execute("INSERT INTO users VALUES ('ADM001','Admin','admin','x',1)")
    prev = None
    for i in range(patients):
        conn.execute("INSERT INTO patients VALUES (?, ?)", (f"P{i}", f"Patient {i}"))
        conn.execute("INSERT INTO vitals VALUES (?, ?)", (i, f"P{i}"))
    for i in range(3):
        row_hash = hashlib.sha256(f"r{i}".encode()).hexdigest()
        conn.execute("INSERT INTO audit_logs VALUES (?, ?, ?)", (i + 1, prev, row_hash))
        prev = row_hash
    conn.commit()
    conn.close()
    for side in ("-wal", "-shm"):
        Path(str(path) + side).unlink(missing_ok=True)
    return path


def _backups_check(directory: Path, monkeypatch) -> dict:
    monkeypatch.setenv("ORB_BACKUP_DIR", str(directory))
    return next(c for c in ops_health_service.build_status()["checks"]
                if c["key"] == "backups")


# ── The writer ───────────────────────────────────────────────────────────────
def test_a_passing_verification_writes_a_receipt_the_status_page_accepts(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_DB_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.delenv("ORB_BACKUP_REQUIRE_NONEMPTY", raising=False)
    backup = _make_db(tmp_path / "orb-backup-20261002-020000.db")
    receipt_path = tmp_path / "orb-backup-20261002-020000.verified.json"

    assert vr.main(["--db", str(backup), "--prefix", "backup", "--schema-policy", "warn",
                    "--receipt", str(receipt_path)]) == 0

    receipt = json.loads(receipt_path.read_text())
    assert receipt["schema"] == ops_health_service.VERIFICATION_SCHEMA
    assert receipt["ok"] is True
    assert receipt["backup_file"] == backup.name
    assert receipt["integrity_check"] == "ok"
    assert receipt["row_counts"]["patients"] == 3
    assert receipt["schema_version"] == receipt["expected_schema_version"]
    assert time.strptime(receipt["verified_utc"], "%Y-%m-%dT%H:%M:%SZ")
    assert oct(receipt_path.stat().st_mode & 0o777) == "0o600"

    # Read back through the page: verified, and the timestamp is carried.
    for side in ("-wal", "-shm"):
        Path(str(backup) + side).unlink(missing_ok=True)
    check = _backups_check(tmp_path, monkeypatch)
    assert check["detail"]["newest_verified"] is True
    assert check["status"] == "ok", check
    assert check["detail"]["verification"]["checked_at"] == receipt["verified_utc"]


def test_a_failed_verification_writes_a_failing_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_DB_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.delenv("ORB_BACKUP_REQUIRE_NONEMPTY", raising=False)
    backup = _make_db(tmp_path / "orb-backup-20261002-030000.db", patients=0)
    receipt_path = tmp_path / "orb-backup-20261002-030000.verified.json"

    assert vr.main(["--db", str(backup), "--receipt", str(receipt_path)]) == 2

    receipt = json.loads(receipt_path.read_text())
    assert receipt["ok"] is False
    assert "rows:patients" in receipt["failures"]
    check = _backups_check(tmp_path, monkeypatch)
    assert check["status"] == "urgent"


def test_a_wrong_key_writes_a_failing_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_DB_ENCRYPTION_KEY", "some-other-key")
    backup = _make_db(tmp_path / "orb-backup-20261002-040000.db")
    receipt_path = tmp_path / "orb-backup-20261002-040000.verified.json"
    assert vr.main(["--db", str(backup), "--receipt", str(receipt_path)]) == 2
    receipt = json.loads(receipt_path.read_text())
    assert receipt["ok"] is False and receipt["failures"] == ["decrypt+open"]


def test_no_receipt_when_the_check_could_not_run(tmp_path, monkeypatch):
    """No key at all is not a verdict on the file, so nothing is written."""
    monkeypatch.delenv("ORB_DB_ENCRYPTION_KEY", raising=False)
    backup = _make_db(tmp_path / "orb-backup-20261002-050000.db")
    receipt_path = tmp_path / "orb-backup-20261002-050000.verified.json"
    assert vr.main(["--db", str(backup), "--receipt", str(receipt_path)]) == 1
    assert not receipt_path.exists()


# ── The producer ─────────────────────────────────────────────────────────────
def _fake_backup(directory: Path, stamp: str, *, ok: bool | None, age_s: int) -> Path:
    """A file in the backups directory with a receipt (or none when ok is None)."""
    path = directory / f"orb-backup-{stamp}.db"
    path.write_bytes(b"x" * 64)
    if ok is not None:
        (directory / f"orb-backup-{stamp}.verified.json").write_text(
            json.dumps({"schema": vr.RECEIPT_SCHEMA, "ok": ok}, indent=2))
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


@pytest.mark.skipif(not BACKUP_SH.exists() or not VENV_PY.exists(),
                    reason="needs scripts/backup.sh and the server venv")
def test_backup_sh_writes_the_receipt_and_rotates_only_verified_backups(tmp_path):
    live = _make_db(tmp_path / "live.db")
    out = tmp_path / "backups"
    out.mkdir()
    # Older history: two verified, one failed, one with no receipt at all.
    old_ok_1 = _fake_backup(out, "20260901-020000", ok=True, age_s=4 * 86400)
    old_ok_2 = _fake_backup(out, "20260902-020000", ok=True, age_s=3 * 86400)
    failed = _fake_backup(out, "20260903-020000", ok=False, age_s=2 * 86400)
    unchecked = _fake_backup(out, "20260904-020000", ok=None, age_s=1 * 86400)

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "ORB_DEPLOYMENT_PROFILE": "dev",
        "ORB_JWT_SECRET": "suite-only-secret-0123456789abcdef0123456789",
        "ORB_DB_ENCRYPTION_KEY": TEST_KEY,
        "ORB_DB_PATH": str(live),
        "ORB_BACKUP_DIR": str(out),
        "ORB_UPLOADS_DIR": str(tmp_path / "no-uploads"),
        # backup.sh also archives the ward print packs; never the real ones here.
        "ORB_FHIR_EXPORT_DIR": str(tmp_path / "no-exports"),
        "ORB_BACKUP_MIN_BYTES": "1",
        "ORB_BACKUP_RETAIN": "2",
        "ORB_OLLAMA_BASE_URL": "http://127.0.0.1:9",
    }
    result = subprocess.run(["/bin/bash", str(BACKUP_SH)], capture_output=True, text=True,
                            env=env, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr

    new = sorted(p for p in out.glob("orb-backup-2*.db")
                 if p not in (old_ok_1, old_ok_2, failed, unchecked))
    assert len(new) == 1, list(out.iterdir())
    receipt = json.loads(new[0].with_name(new[0].stem + ".verified.json").read_text())
    assert receipt["ok"] is True and receipt["backup_file"] == new[0].name

    # Retain 2 among VERIFIED: the new one and the newest old verified one stay;
    # the oldest verified goes, with its receipt. The failed and unchecked files
    # are never counted or pruned.
    assert new[0].exists() and old_ok_2.exists()
    assert not old_ok_1.exists()
    assert not old_ok_1.with_name(old_ok_1.stem + ".verified.json").exists()
    assert failed.exists() and unchecked.exists()
    assert "no passing receipt" in result.stderr
