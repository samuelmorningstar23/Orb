"""Orb Hospital OS — Backup Routes.

Surfaces the previously CLI-only consistent SQLite backup (server/scripts/backup_db.py)
through the admin dashboard. All endpoints are gated on the ``admin.overview``
permission — the same admin-only permission that guards the governance dashboard
and user management — so backups cannot be created or enumerated by clinicians.

Endpoints (prefix ``/api/admin/backups``):
  POST ""   create a consistent online backup NOW (audit-logged)
  GET  ""   list existing backups (filename, size, mtime)

There is deliberately NO restore endpoint. Restoring a backup over the live
``orb.db`` is destructive (it silently discards every write since the snapshot,
including audit rows, and would break the append-only audit hash chain) and there
is no safe way to expose it as a one-click HTTP action in v1. Restores must be
performed out-of-band by an operator with filesystem access, with the server
stopped. This is intentional.

GITIGNORE: backups are written as ``orb-backup-<timestamp>.db`` into a ``backups/``
directory beside the live DB. The repo ``.gitignore`` already excludes these via
the ``*.db`` (and ``*.db-wal`` / ``*.db-shm``) rules, so backup snapshots — which
contain PHI — can never be accidentally committed. No .gitignore change is needed.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from config import BACKUP_DIR, DB_PATH
from fastapi import APIRouter, Depends, HTTPException, Request
from permissions import require_permission
from services.audit_service import log_audit_event

logger = logging.getLogger("orb.backup_routes")

# The snapshot itself is written by database.consistent_backup — the SQLite
# online-backup API through CODEC-AWARE connections. The stdlib sqlite3 driver
# (used by scripts/backup_db.py) cannot read a SQLCipher-encrypted live DB: it
# raises "file is not a database" and the endpoint would 500 exactly on the
# deployment that most needs a backup. consistent_backup works for plaintext
# AND encrypted DBs, integrity-checks the fresh copy, and (when encryption is
# active) writes the backup encrypted with the same key so no plaintext PHI
# ever hits disk.
#
# Naming/pruning conventions are still shared with the standalone CLI script.
# scripts/ has no __init__.py (it is an implicit namespace package), and this
# module is imported at server boot, so a failed import must NOT take down the
# app. Fall back to an inline replica if the script is not importable.
try:
    from scripts.backup_db import (  # type: ignore
        BACKUP_PREFIX,
        BACKUP_SUFFIX,
        prune_old_backups,
    )
except Exception:  # pragma: no cover - defensive fallback
    logger.warning("scripts.backup_db not importable; using inline prune replica")

    BACKUP_PREFIX = "orb-backup-"
    BACKUP_SUFFIX = ".db"

    def prune_old_backups(out_dir: Path, retain_days: int, *, new_backup: Path) -> int:
        """Inline replica: delete backups older than retain_days."""
        if retain_days <= 0 or not out_dir.is_dir():
            return 0
        cutoff = (datetime.now() - timedelta(days=retain_days)).timestamp()
        deleted = 0
        for candidate in out_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
            if candidate == new_backup:
                continue
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
                    deleted += 1
            except OSError:
                pass
        return deleted


_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"

router = APIRouter(prefix="/api/admin/backups", tags=["backup"])

# Backups older than this are pruned after each successful create (mirrors the
# CLI script default). 0 disables pruning.
_RETAIN_DAYS = int(os.getenv("ORB_BACKUP_RETAIN_DAYS", "14"))


def _out_dir() -> Path:
    """Resolve the backups directory — config.BACKUP_DIR, the canonical one.

    This used to default to `DB_PATH.parent / "backups"` (= server/backups on the
    stock layout) while scripts/backup.sh wrote to <repo>/backups. A backup made
    from the admin UI therefore landed where the nightly rotation, the restore
    drill and the warm standby never looked — present on the status page,
    invisible to every mechanism that would actually restore it. Read at call
    time, not import time, so a test that sets ORB_BACKUP_DIR still wins.
    """
    env = os.getenv("ORB_BACKUP_DIR")
    return Path(env) if env else BACKUP_DIR


def _describe(path: Path) -> dict:
    """Serialize one backup file's filename / size / mtime for the listing."""
    stat = path.stat()
    when = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    return {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "modified_at": when,
        "created_at": when,
    }


# Cap the number of backups serialized per response; backup_count always
# reports the TRUE on-disk total (counted from the full glob, never from the
# already-sliced display list, which would silently cap the reported total).
_MAX_BACKUPS_SHOWN = 50


# What a restore rolls back (ruling MS-R12 (a), multisite-15). One appliance
# can hold several facilities of a hospital group in one database (MS-R1 (a)),
# and a backup is that whole database, so a restore puts every facility back
# together; there is no per-facility restore. The Data and Backups screen and
# CIO questionnaire 1.5 say so in these words.
RESTORE_STATEMENT_ONE = (
    "A backup holds this appliance's whole database. Restoring one puts every "
    "record back to the moment the backup was taken: anything recorded after "
    "that moment is lost and must be entered again."
)
RESTORE_STATEMENT_MANY = (
    "This appliance holds {count} facilities in one database, and a backup holds "
    "all of them. Restoring one puts every facility back to the moment it was "
    "taken, not just one: anything recorded at any facility after that moment "
    "is lost and must be entered again. One facility cannot be restored on its own."
)


def _restore_scope() -> dict:
    """{whole_appliance, facilities_on_record, statement, facilities?}: what
    restoring a backup rolls back. Facilities are listed (id, code, name)
    only while more than one is on record; nothing here names a patient."""
    from services import facility_service

    try:
        rows = facility_service.list_facilities()
    except Exception:  # the posture page must still answer
        logger.warning("Could not read the facilities table for the restore statement")
        rows = []
    count = max(len(rows), 1)
    scope: dict = {
        "whole_appliance": True,
        "facilities_on_record": count,
        "statement": (
            RESTORE_STATEMENT_MANY.format(count=count) if count > 1 else RESTORE_STATEMENT_ONE
        ),
    }
    if count > 1:
        scope["facilities"] = [
            {
                "id": int(r["id"]),
                "code": r.get("code"),
                "name": facility_service.display_name(int(r["id"]), facility=r),
            }
            for r in rows
        ]
    return scope


_LIST_BACKUPS_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.get("")
def list_backups(user: dict = _LIST_BACKUPS_USER_DEFAULT) -> dict:
    """Database posture (size, journal mode, encryption, row counts) and the
    list of existing backup snapshots (newest first)."""
    from database import db_session, encryption_at_rest_status

    from services import crypto_service

    db_path = Path(DB_PATH)
    with db_session() as db:
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        tables: dict = {}
        for t in (
            "users",
            "patients",
            "admissions",
            "vitals",
            "audit_logs",
            "medications",
            "orders",
            "scribe_notes",
        ):
            try:
                tables[t] = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                tables[t] = None

    encryption_status = encryption_at_rest_status()
    database_info = {
        "filename": db_path.name,
        "size_bytes": db_path.stat().st_size if db_path.is_file() else 0,
        "journal_mode": journal_mode,
        # Observed, not configured: a key in .env with no SQLCipher codec on the
        # connection used to read "encrypted" here over a plaintext file.
        "encrypted_at_rest": bool(encryption_status["active"]),
        "cipher_version": encryption_status["cipher_version"],
        "uploads_encrypted": crypto_service.is_encryption_enabled(),
        "tables": tables,
    }

    out_dir = _out_dir()
    files = (
        sorted(
            out_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if out_dir.is_dir()
        else []
    )
    backups = [_describe(p) for p in files[:_MAX_BACKUPS_SHOWN]]

    return {
        "database": database_info,
        "backups": backups,
        "backup_count": len(files),
        "backups_shown": len(backups),
        "retention_days": _RETAIN_DAYS,
        "directory": str(out_dir),
        "restore_scope": _restore_scope(),
    }


_CREATE_BACKUP_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("")
def create_backup(request: Request, user: dict = _CREATE_BACKUP_USER_DEFAULT):
    """Create a consistent online backup of the live SQLite database NOW.

    Uses sqlite3's online backup API through database.consistent_backup (codec-
    aware, so it also works when the DB is SQLCipher-encrypted at rest) so the
    snapshot is transactionally consistent even while the server is mid-write,
    then runs PRAGMA integrity_check against the fresh copy. Old backups beyond
    the retention window are pruned. The creation is audit-logged.

    Defined as a sync endpoint so FastAPI runs it in the threadpool — the backup
    is blocking I/O and must not stall the event loop.
    """
    from database import consistent_backup

    if not Path(DB_PATH).is_file():
        raise HTTPException(status_code=500, detail=f"Source database not found: {DB_PATH}")

    out_dir = _out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime(_TIMESTAMP_FMT)
    backup_path = out_dir / f"{BACKUP_PREFIX}{timestamp}{BACKUP_SUFFIX}"
    try:
        consistent_backup(backup_path)
    except RuntimeError as exc:
        # Integrity check failed on the fresh copy — surface as a server error.
        backup_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail=f"Backup integrity check failed: {exc}"
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        backup_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}") from exc

    # Best-effort pruning; never fail the request if pruning hits a stray file.
    try:
        prune_old_backups(out_dir, _RETAIN_DAYS, new_backup=backup_path)
    except Exception:
        pass

    info = _describe(backup_path)

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="data_management",
        action_type="backup_created",
        outcome="success",
        summary_message=f"Created database backup {info['filename']} ({info['size_bytes']} bytes)",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type="database_backup",
        target_id=info["filename"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"size_bytes": info["size_bytes"], "directory": str(out_dir)},
    )

    return {"status": "success", "backup": info}
