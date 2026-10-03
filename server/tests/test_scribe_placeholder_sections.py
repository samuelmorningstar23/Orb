"""Model scaffolding must not reach a note the doctor is asked to sign.

WHY THIS FILE EXISTS. Every structured SOAP note opened with two lines the
model wrote to say it had nothing:

    DIALOGUE:
    Doctor: [Not explicitly stated in dictation]
    Patient: [Not explicitly stated in dictation]

    SUBJECTIVE:
    67-year-old gentleman presenting on day three of a COPD exacerbation...

Those are the first two lines of a clinical document with a "Sign & Finalize"
button under it. A section with nothing in it is not evidence of anything.

The half of this that needs pinning hardest is the CONSERVATIVE half: a section
holding one real line and one placeholder must keep BOTH. Losing a line the
model actually extracted is a far worse failure than an untidy note, and a
prune that is too eager is exactly how that happens.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.scribe_service import prune_placeholder_sections


NOTE_WITH_EMPTY_DIALOGUE = """DIALOGUE:
Doctor: [Not explicitly stated in dictation]
Patient: [Not explicitly stated in dictation]

SUBJECTIVE:
67-year-old gentleman, day three of a COPD exacerbation.

OBJECTIVE:
ABG on 08:00 gas: pH 7.31, pCO2 7.4 kPa.

ASSESSMENT:
Type 2 respiratory failure.

PLAN:
Continue NIV; repeat ABG at 08:00."""


def test_the_empty_dialogue_section_is_dropped():
    out = prune_placeholder_sections(NOTE_WITH_EMPTY_DIALOGUE)
    assert "DIALOGUE" not in out
    assert "Not explicitly stated" not in out
    assert out.startswith("SUBJECTIVE:")


def test_every_real_section_survives_verbatim():
    out = prune_placeholder_sections(NOTE_WITH_EMPTY_DIALOGUE)
    for kept in ("SUBJECTIVE:", "OBJECTIVE:", "ASSESSMENT:", "PLAN:",
                 "day three of a COPD exacerbation",
                 "pH 7.31, pCO2 7.4 kPa",
                 "Continue NIV; repeat ABG at 08:00."):
        assert kept in out


# ── The conservative half ─────────────────────────────────────────────────────

def test_a_section_with_one_real_line_keeps_everything():
    note = """DIALOGUE:
Doctor: Are you still short of breath at rest?
Patient: [Not explicitly stated in dictation]

PLAN:
Continue NIV."""
    out = prune_placeholder_sections(note)
    assert "Are you still short of breath at rest?" in out
    assert "DIALOGUE:" in out
    # The placeholder rides along rather than risking a dropped clinical line.
    assert "Patient: [Not explicitly stated in dictation]" in out


def test_an_all_scaffolding_note_is_returned_unchanged_not_blanked():
    """A blank note is not a safe outcome — it is a silent one."""
    note = """DIALOGUE:
Doctor: [Not explicitly stated in dictation]

SUBJECTIVE:
[Not documented]"""
    assert prune_placeholder_sections(note) == note


def test_text_before_the_first_heading_is_untouched():
    note = """Dictated by Dr Sharma, 08:12.

DIALOGUE:
Doctor: [None]

PLAN:
Discharge tomorrow."""
    out = prune_placeholder_sections(note)
    assert out.startswith("Dictated by Dr Sharma, 08:12.")
    assert "DIALOGUE" not in out
    assert "Discharge tomorrow." in out


def test_notes_without_placeholders_are_returned_identical():
    note = "SUBJECTIVE:\nCough.\n\nPLAN:\nAmoxicillin 500mg TDS."
    assert prune_placeholder_sections(note) == note


def test_none_and_empty_are_safe():
    assert prune_placeholder_sections(None) is None
    assert prune_placeholder_sections("") == ""


def test_a_bracketed_clinical_value_is_not_mistaken_for_scaffolding():
    """"[K+ 6.8]" is content. Only the model's own not-found phrasing goes."""
    note = "OBJECTIVE:\n[K+ 6.8 mmol/L — haemolysed, repeat sent]\n\nPLAN:\nRepeat U&E."
    assert prune_placeholder_sections(note) == note
