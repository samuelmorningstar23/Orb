"""The fast contract's marker: one reading, two deliberate fail directions.

WHY THIS FILE EXISTS. `parse_fast_response` located the marker with
`str.find()`, which scans the whole string. A model that answered FIRST and
emitted `NOACT|` at the END therefore had its body computed as everything after
that trailing marker — i.e. the empty string. The turn did not error and did not
fall through to the untagged-output rescue (on a patient turn the `<details>`
tags parse, so the rescue's "no tag matched at all" condition is false). It
rendered BLANK.

The existing suite passed throughout, because every marker fixture in it puts
the marker where the contract says it goes. That is the failure class this file
adds: the marker in the WRONG place, which is the only interesting case, because
a contract that is only tested when it is honoured is not tested at all.
"""
import pytest

from routes.sage_routes import (
    MarkerAnswerStreamer,
    SentenceStreamSanitizer,
    _leading_marker,
    _marker_still_possible,
    _strip_forbidden_streaming,
    parse_fast_response,
)

ANSWER = "The usual paracetamol dose is 500 mg to 1 g every 4-6 hours."
DETAILS = ("<details><display_mode>concise</display_mode>"
           "<patient_facts_summary>58y male, CAP.</patient_facts_summary>"
           "<cited_sources>[\"DOC-1\"]</cited_sources></details>")


def _stream(text: str, chunk: int = 5, mutation_locked: bool = False) -> str:
    s = MarkerAnswerStreamer(
        SentenceStreamSanitizer(_strip_forbidden_streaming), mutation_locked)
    out = "".join(s.feed(text[i:i + chunk]) or "" for i in range(0, len(text), chunk))
    return out + (s.finalize() or "")


# ── The live bug ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    f"{ANSWER}\nNOACT|",                       # the reproduced transcript
    f"{ANSWER}\nNOACT|\n{DETAILS}",            # the shape that rendered BLANK
    f"{ANSWER} NOACT|",
    f"{ANSWER}\n\nNOACT|\n",
])
def test_a_trailing_marker_no_longer_deletes_the_answer(raw):
    """This assertion FAILS on the pre-2026-08-04 parser: answer was ''."""
    parsed = parse_fast_response(raw)
    assert parsed["answer"], "a trailing marker blanked the answer"
    assert "usual paracetamol dose" in parsed["answer"]


def test_the_details_channel_still_parses_on_a_blanking_shaped_reply():
    """The blanking case was invisible precisely because the detail tags DID
    parse — which is what stopped the untagged-output rescue from firing."""
    parsed = parse_fast_response(f"{ANSWER}\nNOACT|\n{DETAILS}")
    assert parsed["details"]["display_mode"] == "concise"
    assert parsed["details"]["cited_sources"] == '["DOC-1"]'


# ── Marker syntax never renders ───────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    f"NOACT| {ANSWER}",
    f"{ANSWER}\nNOACT|",
    f"Okay. NOACT| {ANSWER}",
    f"NOACT| {ANSWER}\nNOACT|",
    f"NOACT| {ANSWER}\n{DETAILS}",
])
def test_contract_syntax_never_reaches_the_clinician(raw):
    answer = parse_fast_response(raw)["answer"]
    assert "NOACT|" not in answer and "ACT|" not in answer
    assert "  " not in answer, "removing the marker must not leave a doubled gap"


# ── The compliant paths are unchanged ─────────────────────────────────────────

def test_leading_noact_still_strips_the_marker_and_keeps_the_body():
    parsed = parse_fast_response(f"NOACT| {ANSWER}\n{DETAILS}")
    assert parsed["marker"] == "NOACT|"
    assert parsed["answer"] == ANSWER
    assert parsed["action_intent"] == "None."


def test_leading_act_still_yields_the_structured_intent():
    raw = ('ACT|{"action_type": "add_medication", "medication": "Paracetamol", '
           '"dose": "500 mg"}\nDrafting that request now.')
    parsed = parse_fast_response(raw)
    assert parsed["marker"] == "ACT|"
    assert parsed["action_intent"].startswith("{") and "add_medication" in parsed["action_intent"]
    assert parsed["answer"] == "Drafting that request now."


def test_a_stray_code_fence_before_the_marker_is_tolerated():
    parsed = parse_fast_response(f"```\nNOACT| {ANSWER}")
    assert parsed["marker"] == "NOACT|"
    assert parsed["answer"] == ANSWER


# ── The two fail directions, asserted as a pair ───────────────────────────────

NON_COMPLIANT = [
    f"{ANSWER}\nNOACT|",
    f"Okay. NOACT| {ANSWER}",
    ANSWER,                                   # no marker anywhere
    f"Sure — here is the answer. {ANSWER}",
]


@pytest.mark.parametrize("raw", NON_COMPLIANT)
def test_the_streamer_fails_closed_when_no_marker_opens_the_reply(raw):
    """Streamed text reaches the clinician BEFORE the end-of-turn guardrails can
    rewrite it, so an ungated stream is unrecoverable. Nothing goes out."""
    assert _stream(raw) == ""
    for chunk in (1, 3, 7, 13, 64):
        assert _stream(raw, chunk) == "", f"chunk={chunk}"


@pytest.mark.parametrize("raw", NON_COMPLIANT)
def test_the_parser_fails_open_on_the_same_input(raw):
    """The buffered path runs after every guardrail has had its say, and a blank
    answer is a silent failure a clinician cannot audit. It renders."""
    parsed = parse_fast_response(raw)
    assert parsed["marker"] is None
    assert parsed["answer"]
    assert parsed["action_intent"] == "None.", \
        "a marker outside its contracted position is not evidence of an action"


@pytest.mark.parametrize("chunk", (1, 3, 7, 13, 64))
def test_a_compliant_reply_streams_the_same_words_the_parser_returns(chunk):
    """Parity: the gate decision and the words must not depend on chunking."""
    raw = f"NOACT| {ANSWER}\n{DETAILS}"
    assert _stream(raw, chunk).split() == parse_fast_response(raw)["answer"].split()


def test_a_model_declared_mutation_still_streams_nothing():
    raw = 'ACT|{"action_type": "add_medication", "medication": "Paracetamol"}\nOK.'
    assert _stream(raw) == ""
    assert parse_fast_response(raw)["marker"] == "ACT|"


# ── The anchor itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("NOACT| x", "NOACT|"),
    ("  \n NOACT| x", "NOACT|"),
    ("```json\nNOACT| x", "NOACT|"),
    ('ACT|{"a":1}', "ACT|"),
    ("x NOACT| y", None),
    ("x\nNOACT|", None),
    ("REACT| y", None),
    ("", None),
])
def test_leading_marker_is_anchored_to_the_start(text, expected):
    assert _leading_marker(text)[0] == expected


@pytest.mark.parametrize("buf,possible", [
    ("", True), ("N", True), ("NOAC", True), ("A", True), ("ACT", True),
    ("  \n", True), ("```", True),
    ("The", False), ("Sure", False), ("x", False), ("NOACX", False),
])
def test_marker_still_possible_gives_up_as_soon_as_it_can(buf, possible):
    """Every character spent waiting is a character the clinician is not
    reading, so the streamer must not wait out the 64-char backstop on text
    that could never have been a marker."""
    assert _marker_still_possible(buf) is possible


# ── The feedback loop ─────────────────────────────────────────────────────────

def test_a_misplaced_marker_is_never_replayed_into_the_next_prompt():
    """sage_conversations stores the RAW model output, and _history_turn_text
    replays it. Left alone, one turn that put the marker at the END becomes an
    EXAMPLE in the next prompt — and this model copies its own last output
    strongly enough that history alone decided marker compliance 5/5 vs 0/5
    (see the note on _history_turn_text). The LEADING marker is still kept: that
    is what keeps the contract anchored.

    REVISITED 2026-09-07 (section 7a of FAST_LANE_MARKER_2026-09-07.md). The
    invariant is unchanged and is what the first two assertions state: a marker
    OUT OF POSITION is never replayed. What changed underneath it is that a turn
    Orb has recorded as proposing no action may now be replayed under a
    synthesized LEADING marker, so the bare-answer results below are pinned as
    what happens when the action state is UNKNOWN — which is what this function
    sees when nobody passes one, and what a row with no recorded verdict gets.
    The synthesized case is pinned next door, in
    tests/test_sage_history_marker.py; the two must not disagree about the
    trailing marker, so it is asserted here in both readings."""
    from services.sage_service import _history_turn_text

    assert _history_turn_text(f"{ANSWER}\nNOACT|", 400, keep_marker=True) == ANSWER
    assert _history_turn_text(f"NOACT| {ANSWER}\nNOACT|", 400, keep_marker=True) == \
        f"NOACT| {ANSWER}"
    # unchanged for a compliant turn
    assert _history_turn_text(f"NOACT| {ANSWER}", 400, keep_marker=True) == f"NOACT| {ANSWER}"
    assert _history_turn_text(f"NOACT| {ANSWER}", 400, keep_marker=False) == ANSWER
    # …and with the action state known, the trailing marker is still gone — the
    # answer simply gains the leading one the contract asks for.
    assert _history_turn_text(f"{ANSWER}\nNOACT|", 400, keep_marker=True,
                              proposed_action=False) == f"NOACT| {ANSWER}"


def test_empty_and_whitespace_input_are_safe():
    for raw in ("", "   ", "\n\n"):
        parsed = parse_fast_response(raw)
        assert parsed["answer"] == ""
        assert parsed["action_intent"] == "None."
