"""A filed Lens read always carries the investigational line.

The panel used to put the server's banner inside the editable findings box, so
whether the filed case-room message said "not for diagnostic use" depended on
the clinician not deleting it, and on the read being short enough to survive
the 1,500-character cut. The panel now keeps the banner out of the box (fixed
text instead), so the assign route has to add it, every time.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routes.lens_routes import _DISCLAIMER_LINE, _filed_message  # noqa: E402
from services.lens_service import LENS_DISCLAIMER_BANNER  # noqa: E402

ANALYSIS = {"image_type": "xray", "clinical_context": "Cough, 3 days"}


def test_the_line_is_the_services_banner_without_its_rule():
    assert "INVESTIGATIONAL. NOT FOR DIAGNOSTIC USE." in _DISCLAIMER_LINE
    assert "---" not in _DISCLAIMER_LINE
    assert _DISCLAIMER_LINE in LENS_DISCLAIMER_BANNER


def test_a_reviewed_read_without_the_banner_is_filed_with_it():
    body = _filed_message(ANALYSIS, "FINDINGS:\n- Right lower lobe opacity", "Dr Rao")
    assert "Right lower lobe opacity" in body
    assert body.rstrip().endswith(_DISCLAIMER_LINE)


def test_an_unreviewed_filing_carries_it_too():
    body = _filed_message(ANALYSIS, None, "Dr Rao")
    assert body.rstrip().endswith(_DISCLAIMER_LINE)


def test_a_long_read_cut_at_1500_characters_still_ends_with_it():
    body = _filed_message(ANALYSIS, "x" * 5000, "Dr Rao")
    assert body.rstrip().endswith(_DISCLAIMER_LINE)


def test_a_read_that_still_holds_the_banner_says_it_once():
    """An older client sends the box with the banner inside it."""
    text = "FINDINGS:\n- Clear lungs" + LENS_DISCLAIMER_BANNER
    body = _filed_message(ANALYSIS, text, "Dr Rao")
    assert body.count("INVESTIGATIONAL. NOT FOR DIAGNOSTIC USE.") == 1
    assert "Clear lungs" in body
