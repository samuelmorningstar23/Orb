"""Lane-parity regressions for the forbidden-phrase guardrail.

Every case here was a LIVE LEAK found by adversarially reviewing the
fixed-point strip on 2026-07-31. The pre-existing suite passed throughout,
because its fixtures only ever put ONE space after a period and never drove
`MarkerAnswerStreamer` with a body that starts with a space — which is exactly
what production emits.

The property under test is parity, not just stripping: the answer a clinician
READS (streamed, at whatever chunk size the model happened to produce) and the
answer the voice lane SPEAKS must contain the same phrases as the buffered
`concise_answer`. A safety property that depends on tokenisation is not a
property.
"""
import re

import pytest

from routes.sage_routes import (
    FORBIDDEN_PHRASE_PATTERNS,
    MarkerAnswerStreamer,
    SentenceStreamSanitizer,
    _strip_forbidden,
    _strip_forbidden_streaming,
)

# Gap vocabulary that used to defeat the bare-"Done." anchor. Two spaces after a
# period is ordinary sentence spacing; a leading space is what the NOACT| marker
# leaves behind; a bullet is how this model formats action summaries.
LEAK_CASES = [
    "Order placed. Done.",
    "Order placed.  Done.",          # two spaces — anchor was fixed-width \s
    " Done.",                        # leading space — `^` never fired
    "  Done.",
    "Reviewed the chart.  Done.",
    "Chart updated.  Order placed.  Done.",
    "- Chart updated.\n- Order placed.\n- Done.",
    "* Order placed.\n* Done.",
    "Order placed.\n\nDone.",
    "I have added paracetamol.  Done.",
]

PROSE = [
    "The procedure was done carefully.",
    "Continue until the infusion is done.",
    "Sepsis six done. Lactate 1.8.",
    "Dialysis was done yesterday; creatinine has fallen.",
    "Wound care:\n- Dressing change: Done.\n- Drain check: pending.",
]


def _stream(text: str, chunk: int, strip=_strip_forbidden_streaming) -> str:
    san = SentenceStreamSanitizer(strip)
    out = "".join(san.feed(text[i:i + chunk]) for i in range(0, len(text), chunk))
    return out + san.flush()


@pytest.mark.parametrize("text", LEAK_CASES)
def test_no_self_confirmation_survives_any_lane(text):
    """Buffered, streamed and voice lanes all remove the confirmation."""
    assert "done." not in _strip_forbidden(text).lower()
    assert "done." not in _strip_forbidden_streaming(text).lower()


@pytest.mark.parametrize("text", LEAK_CASES)
@pytest.mark.parametrize("chunk", range(1, 21))
def test_streamed_wire_bytes_never_leak_at_any_chunk_size(text, chunk):
    """The model's tokenisation must not decide whether a fabricated
    confirmation reaches the clinician. Chunk 7 and 14 used to leak where 1-6
    did not."""
    assert "done." not in _stream(text, chunk).lower()


@pytest.mark.parametrize("text", LEAK_CASES)
def test_streamed_and_buffered_lanes_agree_on_phrases(text):
    """Whitespace policy may differ between the lanes; the surviving PHRASES
    may not. Compared on whitespace-normalised tokens for that reason."""
    buffered = _strip_forbidden(text).split()
    for chunk in (1, 3, 7, 13, 64):
        assert _stream(text, chunk).split() == buffered, f"chunk={chunk}"


@pytest.mark.parametrize("chunk", (1, 4, 7, 12, 32))
def test_marker_streamer_strips_a_bare_done_answer(chunk):
    """THE most realistic leak: the model answers "NOACT| Done.", the streamer
    hands the sanitizer " Done.", and the leading space defeated `^`. The
    buffered concise_answer for the same turn was already "" — so the clinician
    saw a fabricated confirmation or not depending on which the UI rendered."""
    text = "NOACT| Done."
    streamer = MarkerAnswerStreamer(SentenceStreamSanitizer(_strip_forbidden_streaming), False)
    wire = "".join(streamer.feed(text[i:i + chunk]) or "" for i in range(0, len(text), chunk))
    wire += streamer.finalize() or ""
    assert "done." not in wire.lower()


@pytest.mark.parametrize("text", PROSE)
def test_legitimate_clinical_prose_is_untouched(text):
    assert "done" in _strip_forbidden(text).lower()
    assert "done" in _strip_forbidden_streaming(text).lower()


def test_buffered_lane_preserves_markdown_structure():
    """The buffered strip used to collapse `\\s{2,}` -> " ", which flattens
    paragraph breaks and lists. The identical answer then rendered as clean
    markdown when it streamed and as flat prose when it did not."""
    answer = ("Post-op day 2.\n\n"
              "- Vitals stable\n- Analgesia adequate\n\n"
              "**Plan:** discharge tomorrow.")
    out = _strip_forbidden(answer)
    assert out == answer
    assert out.count("\n\n") == 2


def test_buffered_lane_still_tidies_the_gap_a_deletion_leaves():
    assert _strip_forbidden("Metoprolol slows the heart rate.  Order placed.  Monitor closely.") == (
        "Metoprolol slows the heart rate. Monitor closely."
    )


def test_strip_fails_closed_when_it_cannot_settle():
    """Exhausting the pass cap means the text may STILL hold a confirmation.
    Returning the partially-stripped text would ship precisely what the
    guardrail exists to stop, so the answer is dropped."""
    assert _strip_forbidden("I have added" * 40 + " x.") == ""


@pytest.mark.parametrize("pattern", FORBIDDEN_PHRASE_PATTERNS)
def test_output_matches_no_pattern_for_every_leak_case(pattern):
    """The fixed point's stated guarantee, asserted per pattern."""
    compiled = re.compile(pattern)
    for text in LEAK_CASES:
        assert not compiled.search(_strip_forbidden(text))
        assert not compiled.search(_strip_forbidden_streaming(text))


# ── Retrieval markup (docs/DEMO_READINESS.md §3.4) ────────────────────────────
# The prompt cites guideline passages as "(DOC: LOC-SAFE-007)" and the model
# echoes the marker into prose — observed verbatim in the browser AND on the
# raw SSE stream. The strip lives in FORBIDDEN_PHRASE_PATTERNS so every lane
# gets it from the one list; these cases mirror the observed leak shapes.

DOC_MARKER_CASES = [
    "According to Tier 2 Escalation Protocol (DOC: LOC-SAFE-007), escalate now.",
    "Start ceftriaxone per local policy (DOC: LOC-ABX-001).",
    "(DOC: EXT-METAB-001) Sepsis guidance recommends early lactate.",
    "Per the DKA pathway (doc: loc-dka-002), begin insulin.",   # model lowercased it
    "Two sources agree (DOC: LOC-SEP-001) (DOC: EXT-SEP-002) on timing.",
    "- **Protocol (DOC: LOC-SAFE-007)**, then escalate.\n- Recheck in 1 h.",
    "Generation cut off mid-marker (DOC: LOC-SAFE-0",
]

_DOC_MARKER = re.compile(r"\(doc:", re.IGNORECASE)
# Distinctive id stems, so a leaked id body is caught even when the "(DOC:"
# prefix itself was consumed (e.g. the truncated-stream case).
_DOC_ID_STEMS = ("loc-safe", "loc-abx", "ext-metab", "loc-dka", "loc-sep", "ext-sep")


def _no_doc_markup(out: str) -> bool:
    low = out.lower()
    return not _DOC_MARKER.search(out) and not any(s in low for s in _DOC_ID_STEMS)


@pytest.mark.parametrize("text", DOC_MARKER_CASES)
def test_doc_marker_never_survives_any_lane(text):
    assert _no_doc_markup(_strip_forbidden(text))
    assert _no_doc_markup(_strip_forbidden_streaming(text))


@pytest.mark.parametrize("text", DOC_MARKER_CASES)
@pytest.mark.parametrize("chunk", range(1, 21))
def test_doc_marker_never_leaks_on_the_wire_at_any_chunk_size(text, chunk):
    """Same law as the confirmation phrases: the model's tokenisation must not
    decide whether a document id reaches the clinician."""
    assert _no_doc_markup(_stream(text, chunk))


@pytest.mark.parametrize("text", DOC_MARKER_CASES)
def test_doc_marker_lanes_agree_on_phrases(text):
    buffered = _strip_forbidden(text).split()
    for chunk in (1, 3, 7, 13, 64):
        assert _stream(text, chunk).split() == buffered, f"chunk={chunk}"


@pytest.mark.parametrize("chunk", (1, 4, 7, 12, 32))
def test_marker_streamer_strips_doc_markup_from_the_answer_body(chunk):
    text = "NOACT| Escalate per local policy (DOC: LOC-SAFE-007) immediately."
    streamer = MarkerAnswerStreamer(SentenceStreamSanitizer(_strip_forbidden_streaming), False)
    wire = "".join(streamer.feed(text[i:i + chunk]) or "" for i in range(0, len(text), chunk))
    wire += streamer.finalize() or ""
    assert _no_doc_markup(wire)
    assert "escalate per local policy" in wire.lower()


def test_doc_strip_reads_as_clean_prose():
    """The inline form eats the gap it leaves — no orphaned space before the
    comma, no doubled space mid-sentence."""
    assert _strip_forbidden(
        "According to Tier 2 Escalation Protocol (DOC: LOC-SAFE-007), escalate now."
    ) == "According to Tier 2 Escalation Protocol, escalate now."


def test_doc_strip_leaves_citation_metadata_intact():
    """The citation channel carries BARE ids — <cited_sources> in the response
    contract, document_id dicts in the provenance panel. Only the parenthesised
    prose marker is markup; the metadata path must render untouched."""
    cited = '<cited_sources>["LOC-SAFE-007", "EXT-METAB-001"]</cited_sources>'
    assert _strip_forbidden(cited) == cited
    assert _strip_forbidden_streaming(cited) == cited


def test_legitimate_parenthesised_prose_is_untouched():
    text = ("Documentation (see the escalation policy) is unchanged; "
            "the doctor (Dr. Rao) reviewed it.")
    assert _strip_forbidden(text) == text
