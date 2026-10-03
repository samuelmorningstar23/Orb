"""Care bundles are named in words on every screen that lists them.

`bundle_type` is a storage key ("sepsis6") and the auto-start reason was
written as "NEWS2 12 (>=7)". The chart timeline title-cased the key into
"Sepsis6 bundle started" and printed the reason as it was stored.
"""
from services import patient_timeline_service as pts
from services.sepsis_bundle_service import _decorate, bundle_label, trigger_reason_text


def test_bundle_keys_read_as_names():
    assert bundle_label("sepsis6") == "Sepsis Six"
    assert bundle_label("SEPSIS6") == "Sepsis Six"
    assert bundle_label("deterioration") == "Deterioration"
    assert bundle_label("falls_risk") == "Falls risk"
    assert bundle_label(None) == "Care"


def test_threshold_marks_read_as_words_old_rows_included():
    assert trigger_reason_text("NEWS2 12 (>=7)") == "NEWS2 12, at or above 7"
    assert trigger_reason_text("NEWS2 8 ( >= 7 )") == "NEWS2 8, at or above 7"
    assert trigger_reason_text("NEWS2 12, at or above 7") == "NEWS2 12, at or above 7"
    assert trigger_reason_text("Critical Vigil alert") == "Critical Vigil alert"
    assert trigger_reason_text(None) is None


def test_timeline_bundle_row_names_the_bundle():
    row = pts._r_bundle({"bundle_type": "sepsis6", "trigger_reason": "NEWS2 12 (>=7)"})
    assert row["title"] == "Sepsis Six bundle started"
    assert row["detail"] == "NEWS2 12, at or above 7"


def test_bundle_api_rows_carry_the_label_and_tidy_reason():
    row = _decorate({"bundle_type": "sepsis6", "trigger_reason": "NEWS2 9 (>=7)",
                     "triggered_at": "2026-10-01T10:00:00+00:00"})
    assert row["bundle_label"] == "Sepsis Six"
    assert row["trigger_reason"] == "NEWS2 9, at or above 7"
