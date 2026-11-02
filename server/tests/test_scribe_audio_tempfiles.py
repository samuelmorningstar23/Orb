"""Patient audio is private from file creation and is removed on WAV failure."""
import os
import stat
import wave

import pytest

from ws import scribe_stream


def _capture_creation(monkeypatch, tmp_path):
    create = scribe_stream.tempfile.mkstemp
    created = []

    def capture(**kwargs):
        fd, path = create(dir=tmp_path, **kwargs)
        created.append((fd, path, stat.S_IMODE(os.stat(path).st_mode)))
        return fd, path

    monkeypatch.setattr(scribe_stream.tempfile, "mkstemp", capture)
    return created


def test_audio_is_private_from_creation_even_with_a_permissive_umask(monkeypatch, tmp_path):
    created = _capture_creation(monkeypatch, tmp_path)
    pcm = b"\x01\x00\x02\x00" * 100
    previous_umask = os.umask(0)
    try:
        path = scribe_stream._write_wav(pcm, sample_rate=8000)
    finally:
        os.umask(previous_umask)
    try:
        assert created[0][2] == 0o600
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        with wave.open(path, "rb") as audio:
            assert audio.getnchannels() == 1
            assert audio.getsampwidth() == 2
            assert audio.getframerate() == 8000
            assert audio.readframes(audio.getnframes()) == pcm
    finally:
        os.remove(path)


@pytest.mark.parametrize("failure", ["open", "write"])
def test_failed_wav_creation_removes_partial_audio_and_closes_its_descriptor(
        monkeypatch, tmp_path, failure):
    created = _capture_creation(monkeypatch, tmp_path)
    message = "injected WAV failure"

    if failure == "open":
        def fail_open(*args, **kwargs):
            raise OSError(message)
        monkeypatch.setattr(scribe_stream.wave, "open", fail_open)
    else:
        def fail_write(self, data):
            self._file.write(b"partial patient audio")
            raise OSError(message)
        monkeypatch.setattr(wave.Wave_write, "writeframes", fail_write)

    with pytest.raises(OSError, match=message):
        scribe_stream._write_wav(b"\x01\x00" * 100)

    fd, path, _ = created[0]
    assert not os.path.exists(path)
    with pytest.raises(OSError):
        os.fstat(fd)
