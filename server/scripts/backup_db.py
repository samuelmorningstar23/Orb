#!/usr/bin/env python3
"""Consistent SQLite backup for Orb Hospital OS.

Uses the sqlite3 *online backup API* (``sqlite3.Connection.backup``) so the copy
is taken from a transactionally-consistent snapshot even while the live server is
mid-write. This is safe to run on a hot ``orb.db`` (WAL mode included) — unlike a
naive ``cp orb.db backup.db`` which can capture a torn page / miss WAL frames.

The script is deliberately self-contained: it imports nothing from the Orb
application. The only app coupling is *optionally* reading ``ORB_DB_PATH`` from the
environment to locate the database (mirroring server/config.py's default), so it
can be dropped into a cron job / container sidecar without importing FastAPI, torch
or any heavy runtime dependency.

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
import logging
import os
import sqlite3
import sys

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

    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime(TIMESTAMP_FMT)
    backup_path = out_dir / f"{BACKUP_PREFIX}{timestamp}{BACKUP_SUFFIX}"

    logger.info("Backing up %s -> %s", db_path, backup_path)
    started = time.monotonic()

    # Codec-aware open: the stdlib sqlite3 driver CANNOT read a SQLCipher
    # database ("file is not a database"), so once encryption at rest is on this
    # script must go through the same driver the app uses. The destination is
    # keyed identically, so a backup of an encrypted DB is itself encrypted —
    # a plaintext backup would defeat the whole control.
    from database import _open_raw_connection_at
    from config import DB_ENCRYPTION_KEY
    encrypted = bool(DB_ENCRYPTION_KEY)
    src = _open_raw_connection_at(db_path, apply_key=encrypted)
    try:
        dest = _open_raw_connection_at(backup_path, apply_key=encrypted)
        try:
            src.backup(dest, pages=0, progress=_log_progress)
        finally:
            dest.close()
    finally:
        src.close()

    elapsed = time.monotonic() - started
    size_mb = backup_path.stat().st_size / (1024 * 1024)
    logger.info("Backup complete: %.2f MB in %.2fs", size_mb, elapsed)

    if run_integrity_check:
        _verify_backup(backup_path)

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


def prune_old_backups(out_dir: Path, retain_days: int) -> int:
    """Delete backups older than retain_days. Returns the number deleted."""
    if retain_days <= 0:
        logger.debug("Pruning disabled (retain_days=%d)", retain_days)
        return 0
    if not out_dir.is_dir():
        return 0

    cutoff = datetime.now() - timedelta(days=retain_days)
    cutoff_ts = cutoff.timestamp()
    deleted = 0
    for candidate in out_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
        try:
            if candidate.stat().st_mtime < cutoff_ts:
                candidate.unlink()
                deleted += 1
                logger.info("Pruned old backup: %s", candidate.name)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not prune %s: %s", candidate, exc)
    if deleted:
        logger.info("Pruned %d backup(s) older than %d day(s)", deleted, retain_days)
    else:
        logger.debug("No backups older than %d day(s) to prune", retain_days)
    return deleted


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path: Path = args.db_path
    out_dir: Path = _resolve_out_dir(db_path, args.out_dir)

    try:
        make_backup(
            db_path,
            out_dir,
            run_integrity_check=not args.no_integrity_check,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    prune_old_backups(out_dir, args.retain_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
