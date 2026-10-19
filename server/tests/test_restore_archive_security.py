"""SEC-05: authenticate, contain and roll back actual synthetic restore media."""
import importlib.util
import io
import os
from pathlib import Path
import tarfile

import pytest

SPEC = importlib.util.spec_from_file_location('orb_backup_set', Path(__file__).resolve().parents[2] / 'scripts/lib/backup_set.py')
sets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sets)


@pytest.fixture
def backup(tmp_path, monkeypatch):
    monkeypatch.setenv('ORB_DB_ENCRYPTION_KEY', 'synthetic-escrow-key')
    database = tmp_path / 'orb-backup-test.db'
    database.write_bytes(b'new synthetic database')
    return database


def archive(backup, entries):
    path = backup.with_name(backup.stem + '-uploads.tar')
    with tarfile.open(path, 'w') as tar:
        for name, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(b'synthetic media')
                tar.addfile(info, io.BytesIO(b'synthetic media'))
            else:
                info.linkname = '../sentinel'
                tar.addfile(info)
    return path


@pytest.mark.parametrize('name,kind', [
    ('sentinel', tarfile.REGTYPE), ('uploads/../sentinel', tarfile.REGTYPE),
    ('/uploads/absolute', tarfile.REGTYPE), ('uploads/link', tarfile.SYMTYPE),
    ('uploads/hardlink', tarfile.LNKTYPE), ('uploads/device', tarfile.CHRTYPE),
    ('uploads/fifo', tarfile.FIFOTYPE), ('uploads/../../sentinel', tarfile.REGTYPE),
])
def test_even_authenticated_bad_headers_cannot_escape(backup, tmp_path, name, kind):
    media = archive(backup, [(name, kind)])
    sets.seal(backup, media)
    sentinel = tmp_path / 'sentinel'
    sentinel.write_bytes(b'original')
    with pytest.raises(ValueError):
        sets.stage(backup, tmp_path / 'uploads', tmp_path / 'stage')
    assert sentinel.read_bytes() == b'original'
    assert not (tmp_path / 'stage/uploads/sentinel').exists()


@pytest.mark.parametrize('tamper', ['media', 'database', 'manifest', 'missing'])
def test_backup_set_authentication(backup, tmp_path, tamper):
    media = archive(backup, [('uploads/image.enc', tarfile.REGTYPE)])
    manifest = sets.seal(backup, media)
    if tamper == 'media':
        media.write_bytes(b'tampered')
    elif tamper == 'database':
        backup.write_bytes(b'tampered database')
    elif tamper == 'manifest':
        manifest.write_text(manifest.read_text().replace('orb.backup.set/1', 'orb.backup.set/2'))
    else:
        manifest.unlink()
    with pytest.raises(ValueError):
        sets.stage(backup, tmp_path / 'uploads', tmp_path / 'stage')


def test_valid_pair_installs_private_media_and_preserves_generation(backup, tmp_path):
    media = archive(backup, [('uploads/image.enc', tarfile.REGTYPE)])
    sets.seal(backup, media)
    staged, dest = tmp_path / 'stage', tmp_path / 'live.db'
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    (uploads / 'old').write_bytes(b'old image')
    dest.write_bytes(b'old db')
    assert sets.stage(backup, uploads, staged)
    sets.install(staged, dest, uploads)
    assert dest.read_bytes() == backup.read_bytes()
    assert (uploads / 'image.enc').read_bytes() == b'synthetic media'
    assert (uploads / 'image.enc').stat().st_mode & 0o777 == 0o600
    assert dest.with_name('live.db.previous').read_bytes() == b'old db'


def test_failed_media_install_rolls_back_both_members(backup, tmp_path, monkeypatch):
    media = archive(backup, [('uploads/image.enc', tarfile.REGTYPE)])
    sets.seal(backup, media)
    stage, db, uploads = tmp_path / 'stage', tmp_path / 'live.db', tmp_path / 'uploads'
    db.write_bytes(b'old db')
    uploads.mkdir()
    (uploads / 'old').write_bytes(b'old image')
    sets.stage(backup, uploads, stage)
    replace = os.replace
    def fail_once(source, target):
        if Path(source).name == 'incoming-uploads':
            raise OSError('injected publication failure')
        return replace(source, target)
    monkeypatch.setattr(sets.os, 'replace', fail_once)
    with pytest.raises(OSError):
        sets.install(stage, db, uploads)
    assert db.read_bytes() == b'old db'
    assert (uploads / 'old').read_bytes() == b'old image'


def test_unpack_limits_and_duplicate_paths(backup, tmp_path, monkeypatch):
    media = archive(backup, [('uploads/one', tarfile.REGTYPE)])
    sets.seal(backup, media)
    monkeypatch.setenv('ORB_RESTORE_MAX_MEDIA_BYTES', '1')
    with pytest.raises(ValueError, match='limits'):
        sets.stage(backup, tmp_path / 'uploads', tmp_path / 'size-stage')
    monkeypatch.delenv('ORB_RESTORE_MAX_MEDIA_BYTES')
    media = archive(backup, [('uploads/one', tarfile.REGTYPE), ('uploads/one', tarfile.REGTYPE)])
    sets.seal(backup, media)
    with pytest.raises(ValueError):
        sets.stage(backup, tmp_path / 'uploads', tmp_path / 'duplicate-stage')
