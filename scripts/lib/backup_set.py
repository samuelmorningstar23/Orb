"""Authenticate backup companions and restore only validated, staged regular media.

No application imports. The operator supplies the existing DB escrow key via
ORB_DB_ENCRYPTION_KEY; keys and bearer media never enter command arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile


def manifest_path(backup: Path) -> Path:
    return backup.with_suffix('.set.json')


def _key() -> bytes:
    secret = os.environ.get('ORB_DB_ENCRYPTION_KEY', '')
    if not secret:
        raise ValueError('Backup authentication requires the database escrow key')
    return hmac.new(secret.encode(), b'orb.backup.set/v1', hashlib.sha256).digest()


def _digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError('Backup members must be regular files')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def seal(backup: Path, media: Path | None = None) -> Path:
    payload = {'schema': 'orb.backup.set/1', 'database': {'name': backup.name, 'sha256': _digest(backup)},
               'media': {'name': media.name, 'sha256': _digest(media)} if media else None}
    envelope = {'payload': payload, 'hmac_sha256': hmac.new(_key(), _canonical(payload), hashlib.sha256).hexdigest()}
    path = manifest_path(backup)
    fd, tmp = tempfile.mkstemp(prefix='.orb-set-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(envelope, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path


def stage(backup: Path, uploads: Path, destination: Path) -> bool:
    """Copy first, authenticate those bytes, validate every header, then extract.

    Legacy database-only backups retain the established verification path. A
    media companion always requires a signed set; no untrusted tar is unpacked.
    """
    media = backup.with_name(backup.stem + '-uploads.tar')
    manifest = manifest_path(backup)
    if backup.is_symlink() or media.is_symlink() or manifest.is_symlink():
        raise ValueError('Backup set links are forbidden')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    db_copy = destination / 'candidate.db'
    shutil.copyfile(backup, db_copy)
    db_copy.chmod(0o600)
    if not manifest.exists():
        if media.exists():
            raise ValueError('Media restore requires an authenticated backup-set manifest')
        return False
    envelope = json.loads(manifest.read_text())
    payload = envelope['payload']
    expected = hmac.new(_key(), _canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(envelope.get('hmac_sha256', ''))):
        raise ValueError('Backup-set authentication failed')
    if (payload.get('schema') != 'orb.backup.set/1'
            or payload['database'] != {'name': backup.name, 'sha256': _digest(db_copy)}):
        raise ValueError('Database does not match the authenticated set')
    if payload.get('media') is None:
        if media.exists():
            raise ValueError('Unexpected media companion')
        return False
    if payload['media'].get('name') != media.name:
        raise ValueError('Unexpected media name')
    archive = destination / 'media.tar'
    shutil.copyfile(media, archive)
    archive.chmod(0o600)
    if _digest(archive) != payload['media']['sha256']:
        raise ValueError('Media does not match the authenticated set')
    # Never use extract/extractall: even a signed archive must respect the
    # expected upload subtree, have bounded expansion and contain no links.
    max_bytes = int(os.environ.get('ORB_RESTORE_MAX_MEDIA_BYTES', 100 * 1024**3))
    extracted = destination / 'uploads'
    extracted.mkdir(mode=0o700)
    with tarfile.open(archive) as tar:
        members, total_bytes, paths = [], 0, set()
        for member in tar:
            total_bytes += member.size
            if len(members) >= 100_000 or total_bytes > max_bytes:
                raise ValueError('Media archive exceeds restore limits')
            parts = PurePosixPath(member.name).parts
            if (not parts or parts[0] != uploads.name or '..' in parts
                    or '\\' in member.name or not (member.isdir() or member.isreg())
                    or parts in paths):
                raise ValueError('Unexpected media archive path or type')
            paths.add(parts)
            members.append(member)
        for member in members:
            relative = PurePosixPath(member.name).parts[1:]
            target = extracted.joinpath(*relative)
            if member.isdir():
                target.mkdir(parents=True, mode=0o700, exist_ok=True)
            else:
                if not relative:
                    raise ValueError('Upload root must be a directory')
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with tar.extractfile(member) as source, target.open('xb') as output:
                    target.chmod(0o600)
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
    return True


def install(staged: Path, database: Path, uploads: Path) -> None:
    """Publish validated candidates; roll back both on any installation failure.

    Kept generations are explicit paths. No existing previous generation is
    removed as part of validation. Power-loss drills remain site acceptance.
    """
    database.parent.mkdir(parents=True, exist_ok=True)
    uploads.parent.mkdir(parents=True, exist_ok=True)
    if database.is_symlink() or uploads.is_symlink():
        raise ValueError('Restore destinations must not be links')
    db_work = Path(tempfile.mkdtemp(prefix='.orb-db-restore-', dir=database.parent))
    media_work = Path(tempfile.mkdtemp(prefix='.orb-media-restore-', dir=uploads.parent))
    old_db, old_media = db_work / 'previous.db', media_work / 'previous-uploads'
    has_media = (staged / 'uploads').is_dir()
    db_saved = media_saved = db_installed = media_installed = False
    try:
        shutil.copyfile(staged / 'candidate.db', db_work / 'incoming.db')
        (db_work / 'incoming.db').chmod(0o600)
        if has_media:
            shutil.copytree(staged / 'uploads', media_work / 'incoming-uploads')
        if database.exists():
            os.replace(database, old_db)
            db_saved = True
        if has_media and uploads.exists():
            os.replace(uploads, old_media)
            media_saved = True
        os.replace(db_work / 'incoming.db', database)
        db_installed = True
        if has_media:
            os.replace(media_work / 'incoming-uploads', uploads)
            media_installed = True
    except BaseException:
        if db_installed:
            database.unlink()
        if media_installed:
            shutil.rmtree(uploads)
        if db_saved:
            os.replace(old_db, database)
        if media_saved:
            os.replace(old_media, uploads)
        raise
    else:
        if db_saved:
            os.replace(old_db, database.with_name(database.name + '.previous'))
        # Prior media stays private in a uniquely named generation directory.
        # It may contain references from the previous database; never discard it.
        if media_saved:
            print('Prior media retained in a private restore generation')
        for suffix in ('-wal', '-shm'):
            database.with_name(database.name + suffix).unlink(missing_ok=True)
    finally:
        # On rollback failure retain recovery bytes rather than hiding them.
        if not old_db.exists():
            shutil.rmtree(db_work)
        if not old_media.exists():
            shutil.rmtree(media_work)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('seal', 'stage', 'install'))
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--media', type=Path)
    parser.add_argument('--uploads', type=Path)
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--database', type=Path)
    args = parser.parse_args()
    if args.operation == 'seal':
        seal(args.backup, args.media)
    elif args.operation == 'stage':
        stage(args.backup, args.uploads, args.stage)
    else:
        install(args.stage, args.database, args.uploads)


if __name__ == '__main__':
    main()
