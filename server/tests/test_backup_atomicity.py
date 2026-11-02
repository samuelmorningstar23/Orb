"""Disposable file-only backup publication and retention boundary tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
import select
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import config
from scripts import backup_db as backups


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_ENCRYPTION_KEY", "")
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE payload(value TEXT)")
        db.execute("INSERT INTO payload VALUES ('synthetic-only')")
    return path


def verdict(path, **changes):
    data = {"schema": "orb.backup.verification/1", "ok": True,
            "backup_file": path.name, "size_bytes": path.stat().st_size,
            "integrity_check": "ok"}
    data.update(changes)
    backups.receipt_for(path).write_text(json.dumps(data))


def test_same_second_parallel_snapshots_are_distinct_complete_and_private(source, tmp_path, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 11, 12, 0, 0)
    monkeypatch.setattr(backups, "datetime", Frozen)
    out = tmp_path / "out"
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: backups.make_backup(source, out), range(8)))
    assert len(set(paths)) == 8
    for path in paths:
        assert path.name.startswith("orb-backup-20261011-120000-")
        assert path.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT value FROM payload").fetchone()[0] == "synthetic-only"
    assert not list(out.glob("*.partial*"))
    assert not list(out.glob(".*.partial*"))


def test_publication_collision_never_overwrites_existing_backup(source, tmp_path, monkeypatch):
    monkeypatch.setattr(backups.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    class Frozen(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 11, 12, 0, 0)
    monkeypatch.setattr(backups, "datetime", Frozen)
    out = tmp_path / "out"
    first = backups.make_backup(source, out)
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        backups.make_backup(source, out)
    assert first.read_bytes() == before
    assert list(out.iterdir()) == [first]


def test_integrity_failure_never_publishes_partial_backup(source, tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(backups, "_verify_backup", lambda _: (_ for _ in ()).throw(RuntimeError("injected integrity failure")))
    with pytest.raises(RuntimeError, match="injected"):
        backups.make_backup(source, out)
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("changes", [{"ok": "true"}, {"schema": "other/1"},
    {"backup_file": "another.db"}, {"size_bytes": 1}, {"integrity_check": "failed"}])
def test_rotation_requires_a_matching_parsed_pass(tmp_path, changes):
    new = tmp_path / "orb-backup-new.db"
    new.write_bytes(b"synthetic")
    verdict(new, **changes)
    with pytest.raises(RuntimeError, match="passing receipt"):
        backups.rotate_backups(tmp_path, new_backup=new, retain_count=1)
    assert new.exists()


@pytest.mark.parametrize("mode", ["age", "count"])
def test_rotation_keeps_failed_unchecked_malformed_sets_and_prunes_companions(tmp_path, mode):
    paths = {}
    for kind in ("good", "failed", "unchecked", "malformed", "new"):
        path = tmp_path / f"orb-backup-{kind}.db"
        path.write_bytes(kind.encode())
        os.utime(path, (time.time() - (0 if kind == "new" else 30 * 86400),) * 2)
        paths[kind] = path
        if kind in {"good", "new"}:
            verdict(path)
        elif kind == "failed":
            verdict(path, ok=False)
        elif kind == "malformed":
            backups.receipt_for(path).write_text('{"ok":true this is not JSON')
    companions = [paths["good"].with_name(paths["good"].stem + suffix)
                  for suffix in ("-uploads.tar", "-ward-packs.tar.enc")]
    for path in companions:
        path.write_bytes(b"synthetic")
    kwargs = {"retain_days": 14} if mode == "age" else {"retain_count": 1}
    assert backups.rotate_backups(tmp_path, new_backup=paths["new"], **kwargs) == 1
    assert not paths["good"].exists() and not backups.receipt_for(paths["good"]).exists()
    assert all(not p.exists() for p in companions)
    assert all(paths[kind].exists() for kind in ("failed", "unchecked", "malformed", "new"))


def test_symlink_receipt_never_authorizes_rotation(tmp_path):
    path = tmp_path / "orb-backup-new.db"
    path.write_bytes(b"synthetic")
    verdict(path)
    receipt = backups.receipt_for(path)
    target = tmp_path / "elsewhere.json"
    receipt.rename(target)
    receipt.symlink_to(target)
    assert not backups.passing_receipt(path)


def test_manual_producer_writes_full_matching_encrypted_receipt(tmp_path, monkeypatch):
    from tests.test_backup_receipt import TEST_KEY, _make_db
    monkeypatch.setattr(config, "DB_ENCRYPTION_KEY", TEST_KEY)
    source = _make_db(tmp_path / "source.db")
    backup = backups.make_backup(source, tmp_path / "out")
    backups.verify_receipt(backup)
    assert backups.passing_receipt(backup)
    receipt = json.loads(backups.receipt_for(backup).read_text())
    assert receipt["encrypted"] is True and receipt["row_counts"]["patients"] == 3


def test_manual_failed_verification_persists_failure_without_rotation(tmp_path, monkeypatch):
    from tests.test_backup_receipt import TEST_KEY, _make_db
    monkeypatch.setattr(config, "DB_ENCRYPTION_KEY", TEST_KEY)
    source = _make_db(tmp_path / "source.db", patients=0)
    backup = backups.make_backup(source, tmp_path / "out")
    with pytest.raises(RuntimeError, match="verification failed"):
        backups.verify_receipt(backup)
    assert json.loads(backups.receipt_for(backup).read_text())["ok"] is False
    assert backup.exists() and not backups.passing_receipt(backup)


def test_verifier_io_failure_writes_fail_receipt(tmp_path, monkeypatch):
    import database
    path = tmp_path / "orb-backup-synthetic.db"
    path.write_bytes(b"synthetic unreadable snapshot")
    monkeypatch.setattr(database, "_open_raw_connection_at", lambda *_a, **_k: (_ for _ in ()).throw(OSError("injected IO failure")))
    with pytest.raises(RuntimeError, match="verification failed"):
        backups.verify_receipt(path)
    assert json.loads(backups.receipt_for(path).read_text())["failures"] == ["verification_error"]
    assert not backups.passing_receipt(path)


@pytest.mark.parametrize("patients", [0, 3])
def test_cli_full_verification_controls_age_rotation_and_exact_path_output(tmp_path, patients):
    from tests.test_backup_receipt import TEST_KEY, _make_db
    source = _make_db(tmp_path / "source.db", patients=patients)
    out = tmp_path / "out"
    out.mkdir()
    old = out / "orb-backup-old.db"
    old.write_bytes(b"old synthetic")
    verdict(old)
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    env = dict(os.environ, ORB_DB_PATH=str(source), ORB_DEPLOYMENT_PROFILE="dev",
               ORB_DB_ENCRYPTION_KEY=TEST_KEY, ORB_BACKUP_REQUIRE_NONEMPTY="")
    result = subprocess.run([sys.executable, str(Path(backups.__file__).resolve()),
        "--db-path", str(source), "--out-dir", str(out), "--retain-days", "14", "--print-path"],
        env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == (0 if patients else 2), result.stderr
    produced = [p for p in out.glob("orb-backup-*.db") if p != old]
    assert len(produced) == 1
    if patients:
        assert result.stdout.strip() == str(produced[0].resolve())
        assert backups.passing_receipt(produced[0]) and not old.exists()
    else:
        assert not result.stdout.strip() and old.exists()
        assert json.loads(backups.receipt_for(produced[0]).read_text())["ok"] is False


def test_producer_lock_serializes_separate_processes(tmp_path):
    code = "from pathlib import Path; import sys; from scripts.backup_db import backup_set_lock; print('waiting',flush=True)\nwith backup_set_lock(Path(sys.argv[1])):\n print('acquired',flush=True)\n sys.stdin.readline()\n"
    processes = []
    try:
        for index in range(2):
            process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(process)
            assert process.stdout.readline().strip() == "waiting"
            if index == 0:
                assert process.stdout.readline().strip() == "acquired"
        assert not select.select([processes[1].stdout], [], [], 0.1)[0]
        processes[0].communicate("release\n", timeout=10)
        assert processes[1].stdout.readline().strip() == "acquired"
        processes[1].communicate("release\n", timeout=10)
        assert all(process.returncode == 0 for process in processes)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


def test_offsite_copy_is_private_complete_and_never_overwrites(tmp_path, monkeypatch):
    source = tmp_path / "orb-backup-source.db"
    source.write_bytes(b"synthetic complete artifact")
    out = tmp_path / "offsite"
    out.mkdir()
    target = backups.copy_artifact(source, out)
    assert target.read_bytes() == source.read_bytes() and target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        backups.copy_artifact(source, out)
    assert target.read_bytes() == b"synthetic complete artifact"
    target.unlink()
    def fail(src, dest):
        dest.write(b"partial")
        raise OSError("injected copy failure")
    monkeypatch.setattr(backups.shutil, "copyfileobj", fail)
    with pytest.raises(OSError, match="injected"):
        backups.copy_artifact(source, out)
    assert list(out.iterdir()) == []
