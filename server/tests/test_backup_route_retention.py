"""Admin retention obeys the shared producer's verified-new-backup contract."""
import json
import os
import time
from pathlib import Path

import pytest
from starlette.requests import Request


def _receipt(path: Path) -> None:
    path.with_name(path.stem + ".verified.json").write_text(json.dumps({
        "schema": "orb.backup.verification/1", "ok": True,
        "backup_file": path.name, "size_bytes": path.stat().st_size,
        "integrity_check": "ok",
    }))


@pytest.mark.parametrize("verified", [True, False])
def test_admin_retention_protects_new_backup_and_requires_verification(tmp_path, monkeypatch, verified):
    import database
    from routes import backup_routes as routes
    from fastapi import HTTPException
    from scripts.backup_db import prune_old_backups

    source = tmp_path / "source.db"
    source.write_bytes(b"synthetic source")
    out = tmp_path / "backups"
    out.mkdir()
    old = out / "orb-backup-20000101-000000.db"
    old.write_bytes(b"old synthetic snapshot")
    _receipt(old)
    old_time = time.time() - 90 * 86400
    os.utime(old, (old_time, old_time))

    def produce(target):
        target.write_bytes(b"new synthetic snapshot")
        # Even an unexpectedly old filesystem timestamp cannot delete this
        # request's newly produced file. Verification gates every rotation.
        os.utime(target, (old_time, old_time))
        if verified:
            _receipt(target)

    monkeypatch.setattr(routes, "DB_PATH", str(source))
    monkeypatch.setattr(routes, "_out_dir", lambda: out)
    monkeypatch.setattr(routes, "_RETAIN_DAYS", 14)
    monkeypatch.setattr(routes, "prune_old_backups", prune_old_backups)
    monkeypatch.setattr(database, "consistent_backup", produce)
    shared_producer = hasattr(routes, "make_backup")
    if shared_producer:
        def make_owned(_source, directory):
            target = directory / "orb-backup-20990101-000000.db"
            produce(target)
            return target

        def verify_owned(_target):
            if not verified:
                raise RuntimeError("Synthetic producer did not verify its snapshot")

        monkeypatch.setattr(routes, "make_backup", make_owned)
        monkeypatch.setattr(routes, "verify_receipt", verify_owned)
    monkeypatch.setattr(routes, "log_audit_event", lambda **_fields: None)
    request = Request({"type": "http", "method": "POST", "path": "/api/admin/backups",
                       "headers": [], "server": ("test", 80), "scheme": "http"})
    if shared_producer and not verified:
        with pytest.raises(HTTPException) as refused:
            routes.create_backup(request, {"id": "test-admin", "role": "admin"})
        assert refused.value.status_code == 500
        fresh = out / "orb-backup-20990101-000000.db"
    else:
        result = routes.create_backup(request, {"id": "test-admin", "role": "admin"})
        assert result["status"] == "success"
        fresh = out / result["backup"]["filename"]
    assert fresh.read_bytes() == b"new synthetic snapshot"
    assert old.exists() is not verified
