"""Tests for the stdlib TOTP MFA service (services/mfa_service)."""
import base64
import sys
import time
import xml.etree.ElementTree as ET

from services import mfa_service


def test_generate_secret_is_base32_and_unique():
    s1 = mfa_service.generate_secret()
    s2 = mfa_service.generate_secret()
    assert s1 != s2
    # base32 alphabet only (A-Z, 2-7), no padding
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in s1)
    assert len(s1) >= 16


def test_current_code_verifies():
    secret = mfa_service.generate_secret()
    code = mfa_service.current_code(secret)
    assert mfa_service.verify_code(secret, code) is True


def test_verify_rejects_wrong_code():
    secret = mfa_service.generate_secret()
    # A code that is not the current one (offset by a full period far outside skew)
    assert mfa_service.verify_code(secret, "000000") is False or \
        mfa_service.current_code(secret) == "000000"


def test_verify_rejects_malformed():
    secret = mfa_service.generate_secret()
    assert mfa_service.verify_code(secret, "") is False
    assert mfa_service.verify_code(secret, "abc") is False
    assert mfa_service.verify_code(secret, "1234567") is False
    assert mfa_service.verify_code("", "123456") is False


def test_skew_window_accepts_adjacent_step():
    secret = mfa_service.generate_secret()
    now = time.time()
    prev_code = mfa_service.current_code(secret, at=now - 30)
    # A code from the previous 30s step should still verify at 'now'
    assert mfa_service.verify_code(secret, prev_code, at=now) is True


def test_provisioning_uri_shape():
    secret = "JBSWY3DPEHPK3PXP"
    uri = mfa_service.provisioning_uri(secret, "D001", "Orb Hospital OS")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "issuer=Orb%20Hospital%20OS" in uri
    assert "digits=6" in uri and "period=30" in uri


# ── Enrolment QR (qr_svg_data_uri) ────────────────────────────────────────────

_URI = mfa_service.provisioning_uri("JBSWY3DPEHPK3PXP", "D001", "Orb Hospital OS")


def test_qr_svg_data_uri_is_wellformed_svg_with_white_quiet_zone():
    out = mfa_service.qr_svg_data_uri(_URI)
    assert out is not None and out.startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(out.split(",", 1)[1])
    # Well-formed XML whose root is an <svg> element containing a QR path.
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    ns = "{http://www.w3.org/2000/svg}"
    # SvgPathFillImage: white background rect (quiet zone stays white on dark
    # themes) + a single path of QR modules. No raster payload anywhere.
    rects = root.findall(f"{ns}rect")
    assert any(r.get("fill") == "white" for r in rects), "white background rect missing"
    assert root.findall(f"{ns}path"), "QR module path missing"
    assert b"image/png" not in svg and b"base64" not in svg  # no embedded raster


def test_qr_generation_receives_exact_otpauth_uri(monkeypatch):
    """The content handed to the QR encoder is the otpauth URI, verbatim."""
    import qrcode

    captured = {}
    real_make = qrcode.make

    def spy(data, **kwargs):
        captured["data"] = data
        return real_make(data, **kwargs)

    monkeypatch.setattr(qrcode, "make", spy)
    out = mfa_service.qr_svg_data_uri(_URI)
    assert out is not None
    assert captured["data"] == _URI


def test_qr_svg_data_uri_is_deterministic_and_content_dependent():
    """Same URI -> byte-identical SVG; different URI -> different SVG.

    The route test leans on this: it re-renders the returned otpauth_uri and
    compares bytes to prove the payload QR encodes exactly that URI.
    """
    a1 = mfa_service.qr_svg_data_uri(_URI)
    a2 = mfa_service.qr_svg_data_uri(_URI)
    b = mfa_service.qr_svg_data_uri(_URI + "x")
    assert a1 == a2
    assert a1 != b


def test_qr_svg_data_uri_returns_none_when_qrcode_missing(monkeypatch):
    """Absent/broken qrcode package degrades to None, never an exception."""
    monkeypatch.setitem(sys.modules, "qrcode", None)
    monkeypatch.setitem(sys.modules, "qrcode.image", None)
    monkeypatch.setitem(sys.modules, "qrcode.image.svg", None)
    assert mfa_service.qr_svg_data_uri(_URI) is None
