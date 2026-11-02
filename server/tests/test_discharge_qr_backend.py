"""A clean dependency install must support discharge QR PNGs without Pillow."""

from pathlib import Path
import sys

import png

from services import discharge_service


def test_discharge_qr_generates_a_png_with_the_declared_pure_python_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(discharge_service, "UPLOAD_DIR", tmp_path)
    # Importing qrcode.image.pil itself requires Pillow, defeating this clean
    # install regression. Refuse the optional dependency before QR generation.
    monkeypatch.setitem(sys.modules, "PIL", None)

    path = Path(discharge_service.generate_qr_code(
        "qr-backend-test", "https://ward.example/discharge/opaque-test-token",
    ))

    assert path.parent == tmp_path
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    width, height, rows, metadata = png.Reader(filename=str(path)).read()
    assert width == height and width > 0
    assert metadata["bitdepth"] == 1
    assert len(list(rows)) == height
