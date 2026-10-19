#!/usr/bin/env python3
"""Consistent SQLite backup for Orb Hospital OS.

Uses the sqlite3 *online backup API* (``sqlite3.Connection.backup``) so the copy
is taken from a transactionally-consistent snapshot even while the live server is
mid-write. This is safe to run on a hot ``orb.db`` (WAL mode included) — unlike a
naive ``cp orb.db backup.db`` which can capture a torn page / miss WAL frames.

The script uses the application's codec-aware database helpers and the shared
recovery verifier. It does not load FastAPI or model runtimes. A normal CLI run
writes a matching verification receipt before considering any retention;
--snapshot-only is reserved for wrappers that verify the snapshot separately.

Usage:
    python scripts/backup_db.py
    python scripts/backup_db.py --db-path /data/orb.db --out-dir /data/backups
    python scripts/backup_db.py --retain-days 30

Exit codes:
    0  backup written (and old backups pruned) successfully
    1  the source database was not found / not a file
    2  the backup failed its post-write integrity check
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid

# Make the server package importable so this CLI can use database.py's
# codec-aware connection helpers (required now that orb.db is SQLCipher-
# encrypted — stdlib sqlite3 cannot open it). Mirrors the sibling scripts.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("orb.backup")

# Prefix used for both writing and pruning so we never touch unrelated files that
# happen to live in the backups directory.
BACKUP_PREFIX = "orb-backup-"
BACKUP_SUFFIX = ".db"
TIMESTAMP_FMT = "%Y%m%d-%H%M%S"


@contextlib.contextmanager
def backup_set_lock(out_dir: Path):
    """Serialize producers through final copy/rotation on local Mac/Linux disks."""
    import fcntl
    out_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(out_dir / ".orb-backup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _default_db_path() -> Path:
    """Resolve the default database path.

    Precedence:
      1. ``ORB_DB_PATH`` environment variable (same var server/config.py honours).
      2. ``<repo>/server/orb.db`` inferred from this script's location
         (scripts/ -> server/ -> orb.db), so ``python scripts/backup_db.py`` works
         with no arguments from a normal checkout.
    """
    env = os.getenv("ORB_DB_PATH")
    if env:
        return Path(env)
    # scripts/backup_db.py -> scripts/ -> server/
    server_dir = Path(__file__).resolve().parent.parent
    return server_dir / "orb.db"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backup_db.py",
        description="Take a consistent online backup of the Orb SQLite database.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--snapshot-only", action="store_true",
        help="Write an integrity-checked snapshot without a receipt or pruning (scheduled wrapper verifies it separately).",
    )
    parser.add_argument(
        "--print-path", action="store_true", help="Print only the exact produced path to stdout; diagnostics use stderr.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=_default_db_path(),
        help="Path to the SQLite database to back up "
        "(default: $ORB_DB_PATH, else <server>/orb.db).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Directory to write timestamped backups into "
        "(default: $ORB_BACKUP_DIR, else <repo>/backups, the same directory "
        "scripts/backup.sh, restore-drill.sh and standby-restore.sh use).",
    )
    parser.add_argument(
        "--retain-days",
        type=int,
        default=14,
        help="Delete backups in --out-dir older than this many days. "
        "Use 0 to disable pruning.",
    )
    parser.add_argument(
        "--no-integrity-check",
        action="store_true",
        help="Skip the PRAGMA integrity_check run against the fresh backup.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser.parse_args(argv)


def _resolve_out_dir(db_path: Path, out_dir: Path | None) -> Path:
    """Where to write: explicit --out-dir, else $ORB_BACKUP_DIR, else <repo>/backups.

    That last default used to be `db_path.parent / "backups"`, i.e. server/backups
    on the stock layout — and THIS FUNCTION is how server/backups/ came to exist.
    scripts/backup.sh always passes --out-dir <repo>/backups explicitly, so the
    scheduled nightly job was unaffected; but running this script directly, the
    documented way to take an ad-hoc snapshot, quietly wrote somewhere else. The
    two directories then diverged for a fortnight, and the restore drill picked
    the stale one. Anchored to this file's own location (server/scripts/ -> repo
    root) so it agrees with config.BACKUP_DIR and the shell scripts no matter
    where the database has been moved to.
    """
    if out_dir is not None:
        return out_dir
    env = os.getenv("ORB_BACKUP_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "backups"


def _log_progress(status: int, remaining: int, total: int) -> None:
    """Progress callback for sqlite3 backup (best-effort, debug only)."""
    if total:
        done = total - remaining
        logger.debug("Backup progress: %d/%d pages", done, total)


def make_backup(db_path: Path, out_dir: Path, run_integrity_check: bool = True) -> Path:
    """Create one timestamped online backup. Returns the backup file path.

    Raises FileNotFoundError if the source database is missing, and RuntimeError
    if the post-write integrity check fails.
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"Source database not found: {db_path}")

    from database import _open_raw_connection_at
    from config import DB_ENCRYPTION_KEY
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime(TIMESTAMP_FMT)
    backup_path = out_dir / f"{BACKUP_PREFIX}{timestamp}-{uuid.uuid4().hex}{BACKUP_SUFFIX}"
    fd, scratch_name = tempfile.mkstemp(prefix=".orb-backup-", suffix=".partial", dir=out_dir)
    os.close(fd)
    scratch = Path(scratch_name)

    logger.info("Backing up %s -> %s", db_path, backup_path)
    started = time.monotonic()

    # Codec-aware open: the stdlib sqlite3 driver CANNOT read a SQLCipher
    # database ("file is not a database"), so once encryption at rest is on this
    # script must go through the same driver the app uses. The destination is
    # keyed identically, so a backup of an encrypted DB is itself encrypted —
    # a plaintext backup would defeat the whole control.
    encrypted = bool(DB_ENCRYPTION_KEY)
    try:
        src = _open_raw_connection_at(db_path, apply_key=encrypted)
        try:
            dest = _open_raw_connection_at(scratch, apply_key=encrypted)
            try:
                src.backup(dest, pages=0, progress=_log_progress)
            finally:
                dest.close()
        finally:
            src.close()
        if run_integrity_check:
            _verify_backup(scratch)
        # Publish a complete private file without ever replacing a sibling.
        # A link fails closed even if a UUID collision or precreated target occurs.
        with scratch.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(scratch, backup_path)
    finally:
        for path in (scratch, Path(str(scratch) + "-wal"), Path(str(scratch) + "-shm")):
            path.unlink(missing_ok=True)

    elapsed = time.monotonic() - started
    size_mb = backup_path.stat().st_size / (1024 * 1024)
    logger.info("Backup complete: %.2f MB in %.2fs", size_mb, elapsed)

    return backup_path


def _verify_backup(backup_path: Path) -> None:
    """Run PRAGMA integrity_check on the freshly written backup."""
    from database import _open_raw_connection_at
    from config import DB_ENCRYPTION_KEY
    check = _open_raw_connection_at(backup_path, apply_key=bool(DB_ENCRYPTION_KEY))
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    status = result[0] if result else None
    if status != "ok":
        raise RuntimeError(
            f"Integrity check failed for {backup_path}: {status!r}"
        )
    logger.info("Integrity check passed for %s", backup_path.name)


def receipt_for(backup: Path) -> Path:
    return backup.with_suffix(".verified.json")


def copy_artifact(source: Path, out_dir: Path) -> Path:
    """Publish an offsite set member completely, privately and without overwrite."""
    if not out_dir.is_dir() or source.is_symlink() or not source.is_file():
        raise ValueError("Backup copy requires a regular source and an existing destination directory")
    target = out_dir / source.name
    fd, name = tempfile.mkstemp(prefix=".orb-copy-", suffix=".partial", dir=out_dir)
    scratch = Path(name)
    try:
        with os.fdopen(fd, "wb") as dest, source.open("rb") as src:
            shutil.copyfileobj(src, dest)
            dest.flush()
            os.fsync(dest.fileno())
        os.link(scratch, target)
    finally:
        scratch.unlink(missing_ok=True)
    return target


def passing_receipt(backup: Path) -> bool:
    """Only a parsed verdict for this exact regular file permits pruning."""
    try:
        receipt = receipt_for(backup)
        if backup.is_symlink() or receipt.is_symlink() or not backup.is_file():
            return False
        data = json.loads(receipt.read_text(encoding="utf-8"))
        return (isinstance(data, dict) and data.get("schema") == "orb.backup.verification/1"
                and data.get("ok") is True and data.get("backup_file") == backup.name
                and type(data.get("size_bytes")) is int and data["size_bytes"] == backup.stat().st_size
                and data.get("integrity_check") == "ok")
    except (OSError, ValueError, TypeError):
        return False


def verify_receipt(backup: Path) -> None:
    """Run the shared recovery checks and persist PASS/FAIL before any rotation."""
    from database import _open_raw_connection_at
    from config import DB_ENCRYPTION_KEY
    path = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "verify_restore.py"
    spec = importlib.util.spec_from_file_location("orb_backup_verifier", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = None
    try:
        conn = _open_raw_connection_at(backup, apply_key=bool(DB_ENCRYPTION_KEY))
        with contextlib.redirect_stdout(sys.stderr):
            report = module.verify(conn, prefix="backup", schema_policy="warn",
                require_nonempty=module._parse_groups(os.getenv("ORB_BACKUP_REQUIRE_NONEMPTY", "")))
        rows = {t: module._count(conn, t) for t in sorted(module.CLINICAL_FLOOR)}
        version = module._schema_version_found(conn)
    except Exception as exc:
        module.write_receipt(str(receipt_for(backup)), backup_file=backup.name, ok=False,
            integrity_check=None, size_bytes=backup.stat().st_size, row_counts={},
            schema_version=None, failures=["verification_error"], warnings=[],
            encrypted=bool(DB_ENCRYPTION_KEY))
        raise RuntimeError("Backup recovery verification failed; no backups were rotated") from exc
    finally:
        if conn is not None:
            conn.close()
    module.write_receipt(str(receipt_for(backup)), backup_file=backup.name, ok=not report.failures,
        integrity_check=report.integrity, size_bytes=backup.stat().st_size, row_counts=rows,
        schema_version=version, failures=report.failures, warnings=report.warnings,
        encrypted=bool(DB_ENCRYPTION_KEY))
    if report.failures or not passing_receipt(backup):
        raise RuntimeError("Backup recovery verification failed; no backups were rotated")


def rotate_backups(out_dir: Path, *, new_backup: Path, retain_days: int = 0,
                   retain_count: int | None = None) -> int:
    """Prune only matching verified sets, after this producer's fresh PASS."""
    if new_backup.parent.resolve() != out_dir.resolve() or not passing_receipt(new_backup):
        raise RuntimeError("Rotation requires the newly produced backup's passing receipt")
    if retain_days < 0 or (retain_count is not None and retain_count < 1):
        raise ValueError("Retention must keep at least one verified backup")
    verified = sorted((p for p in out_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if passing_receipt(p)),
                      key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    cutoff = (datetime.now() - timedelta(days=retain_days)).timestamp()
    candidates = verified[retain_count:] if retain_count is not None else verified
    deleted = 0
    for candidate in candidates:
        if candidate == new_backup or (retain_count is None and (not retain_days or candidate.stat().st_mtime >= cutoff)):
            continue
        try:
            if not passing_receipt(candidate):
                continue
            candidate.unlink()
            for companion in (receipt_for(candidate), candidate.with_suffix(".set.json"), candidate.with_name(candidate.stem + "-uploads.tar"),
                              candidate.with_name(candidate.stem + "-ward-packs.tar.enc")):
                companion.unlink(missing_ok=True)
            deleted += 1
        except OSError as exc:
            logger.warning("Could not prune %s: %s", candidate, exc)
    return deleted


def prune_old_backups(out_dir: Path, retain_days: int, *, new_backup: Path) -> int:
    return rotate_backups(out_dir, new_backup=new_backup, retain_days=retain_days)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path: Path = args.db_path
    out_dir: Path = _resolve_out_dir(db_path, args.out_dir)

    try:
        # Scheduled wrapper holds this lock around the whole set; its inner
        # snapshot-only child must not acquire it again on a second process.
        lock = contextlib.nullcontext() if args.snapshot_only else backup_set_lock(out_dir)
        with lock:
            backup = make_backup(db_path, out_dir, run_integrity_check=not args.no_integrity_check)
            if not args.snapshot_only:
                verify_receipt(backup)
                prune_old_backups(out_dir, args.retain_days, new_backup=backup)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except (RuntimeError, OSError, ValueError) as exc:
        logger.error("%s", exc)
        return 2

    if args.print_path:
        print(backup.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
