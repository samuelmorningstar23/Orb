"""Private audio conversion shared by the speech daemon and subprocess worker.

Stdlib only: importing the subprocess worker must not load the server or MLX.
"""
import os
import subprocess
import tempfile


def convert_to_wav(input_path: str, *, skip_wav: bool = False) -> str:
    """Return a private 16kHz mono WAV, or the original input on failure.

    The caller owns a returned conversion and must remove it after transcription.
    Failed or interrupted conversions never retain partial plaintext audio.
    """
    if skip_wav and input_path.endswith(".wav"):
        return input_path
    fd, wav_path, complete = None, None, False
    try:
        fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="orb_whisper_")
        os.close(fd)
        fd = None
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", wav_path],
            capture_output=True, timeout=30, check=True,
        )
        if os.path.getsize(wav_path) > 100:
            complete = True
            return wav_path
    except Exception:
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if wav_path is not None and not complete:
            try:
                os.remove(wav_path)
            except OSError:
                pass
    return input_path
