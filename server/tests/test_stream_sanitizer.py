"""SentenceStreamSanitizer — early-release safety + latency tests (Lever A).

The sanitizer used to hold the whole trailing INCOMPLETE SENTENCE, which at the
appliance's ~15 tok/s cost the clinician ~1.2 s of dead air on every turn. It now
holds only the smallest suffix that could still BECOME a forbidden phrase.

These tests exist to prove that the speed-up did not buy latency with safety:

  SAFETY  no forbidden phrase — and not even its trigger literal ("i have
          added", "order placed") — may ever appear in the concatenation of what
          feed()/flush() release, at ANY point in the stream, for ANY chunking
          (1, 3, 7 chars, and random). Table-driven over every entry of
          FORBIDDEN_PHRASE_PATTERNS, with a coverage assert so a newly added
          pattern fails the suite until it gets an example here.
  EQUIVALENCE  the concatenation of all released text equals strip_fn(whole
          text) — the exact string the buffered path would have produced.
  LATENCY  benign text is released BEFORE any sentence boundary arrives.
"""
import inspect
import random
import re
import time

import pytest

import routes.sage_routes as sage_routes
from routes.sage_routes import (
    FORBIDDEN_PHRASE_PATTERNS,
    SentenceStreamSanitizer,
    _SENTENCE_BOUNDARY,
    _UndecidableTrigger,
    _hold_table,
    _literal_trigger,
    _strip_forbidden,
    _strip_forbidden_streaming,
)

# The REAL whitespace-preserving strip the chat stream and the voice lane both
# use. This used to be a hand-copied mirror in this file — a third copy of a
# clinical guardrail, which meant the suite could pass while the shipped strip
# was broken. Never re-mirror it: import it.
_streaming_strip = _strip_forbidden_streaming


class _LegacySanitizer:
    """The pre-Lever-A implementation, kept here as the latency baseline and as
    an oracle for the fully-stripped result."""

    def __init__(self, strip_fn):
        self._strip = strip_fn
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        last_end = None
        for m in _SENTENCE_BOUNDARY.finditer(self._buf):
            last_end = m.end()
        if last_end is None:
            return ""
        complete, self._buf = self._buf[:last_end], self._buf[last_end:]
        return self._strip(complete)

    def flush(self) -> str:
        out, self._buf = self._strip(self._buf), ""
        return out


def _chunks(text: str, size: int):
    return [text[i:i + size] for i in range(0, len(text), size)]


def _run(text, size, strip_fn=_streaming_strip):
    """Feed `text` in `size`-char chunks; return (released_pieces, cumulative)."""
    s = SentenceStreamSanitizer(strip_fn)
    pieces, cumulative = [], []
    for c in _chunks(text, size):
        out = s.feed(c)
        pieces.append(out)
        cumulative.append("".join(pieces))
    tail = s.flush()
    pieces.append(tail)
    cumulative.append("".join(pieces))
    return pieces, cumulative


# ─── The forbidden-phrase table ───
# One realistic example per pattern, in the SAME ORDER as
# FORBIDDEN_PHRASE_PATTERNS. The carrier sentence supplies the sentence-initial
# context the "Done." pattern needs, and benign prose that must survive.

EXAMPLES = [
    "This action has been recorded.",
    "This was performed.",
    "The medication was added.",
    "Chart updated.",
    "Order placed.",
    "Order has been placed.",
    "I have added Paracetamol 500mg.",
    "I've added Paracetamol 500mg.",
    "I have recorded the vitals.",
    "I've recorded the vitals.",
    "I have placed the order.",
    "I've placed the order.",
    "I have ordered a chest X-ray.",
    "I've ordered a chest X-ray.",
    # Hospital work Sage cannot do, in the future tense (2026-09-07). Measured
    # on the ungated imperatives of docs/perf/FAST_LANE_MARKER_2026-09-07.md
    # section 7b: the fast tier answered "I will cancel the morning bloods" and
    # "I can schedule physiotherapy for tomorrow" on turns neither deterministic
    # gate locked, so this list is the only thing between that promise and the
    # clinician. Must-strip / must-NOT-strip table:
    # tests/test_action_lane_gates.py.
    "I will book an urgent CT head.",
    "I'll book an urgent CT head.",
    "I can book an urgent CT head.",
    "I have booked an urgent CT head.",
    "I've booked an urgent CT head.",
    "I will schedule physiotherapy for tomorrow.",
    "I'll schedule physiotherapy for tomorrow.",
    "I can schedule physiotherapy for tomorrow.",
    "I have scheduled physiotherapy for tomorrow.",
    "I've scheduled physiotherapy for tomorrow.",
    "I will cancel the morning bloods.",
    "I'll cancel the morning bloods.",
    "I can cancel the morning bloods.",
    "I have cancelled the morning bloods.",
    "I've cancelled the morning bloods.",
    "I have canceled the morning bloods.",
    "I've canceled the morning bloods.",
    "I will arrange an echo.",
    "I'll arrange an echo.",
    "I can arrange an echo.",
    "I have arranged an echo.",
    "I've arranged an echo.",
    "I will add that to the patient's plan.",
    "I'll add that to the patient's plan.",
    "I will record a blood pressure of 130/80 in the patient's chart.",
    "I'll record a blood pressure of 130/80 in the patient's chart.",
    # Full sentences, not bare markers, ON PURPOSE: every other example ends at
    # a terminator, and the stress corpora below join EXAMPLES in sequence — a
    # bare "(DOC: …)" example would put a ")" where the bare-"Done." anchors
    # expect a sentence end. The marker inside each sentence is what the DOC
    # patterns strip; the surviving prose keeps its period.
    "Per local policy (DOC: LOC-SAFE-007).",
    "(DOC: EXT-METAB-001) per sepsis guidance.",
    "Done.",
]

CARRIER_HEAD = "The patient is stable. "
CARRIER_TAIL = " Continue monitoring the heart rate."


def test_every_pattern_has_an_example_and_a_derived_trigger():
    """Coverage gate: a new forbidden pattern must come with an example here AND
    must reduce to a literal trigger, or the sanitizer silently falls back to the
    old (slow) sentence granularity."""
    assert len(EXAMPLES) == len(FORBIDDEN_PHRASE_PATTERNS), (
        "add an example phrase for every FORBIDDEN_PHRASE_PATTERNS entry"
    )
    triggers = [_literal_trigger(p) for p in FORBIDDEN_PHRASE_PATTERNS]
    assert all(triggers), f"pattern with no derivable trigger: {triggers}"
    # Derivation actually succeeded for the shipped list (prefix set is not None
    # → the fast path is in use, the fail-safe path is not).
    _compiled, prefixes, maxtrig, _trig = _hold_table(tuple(FORBIDDEN_PHRASE_PATTERNS))
    assert prefixes is not None, "sanitizer fell back to sentence granularity"
    assert maxtrig == max(len(t) for t in triggers)
    # Each example really is matched by its own pattern, and really does start
    # with its derived trigger (otherwise the table would be testing nothing).
    for pat, trig, ex in zip(FORBIDDEN_PHRASE_PATTERNS, triggers, EXAMPLES):
        probe = CARRIER_HEAD + ex + CARRIER_TAIL
        assert re.search(pat, probe), f"{ex!r} is not matched by {pat!r}"
        assert trig in probe.lower(), f"{ex!r} does not contain trigger {trig!r}"


@pytest.mark.parametrize("size", [1, 3, 7])
@pytest.mark.parametrize("idx", range(len(EXAMPLES)))
def test_forbidden_phrase_never_released_at_any_chunk_size(idx, size):
    """THE core safety property. Every forbidden phrase, fed one character at a
    time (and in 3- and 7-char chunks so triggers span boundaries), must never
    surface — not the phrase, not its trigger — in the released concatenation at
    any point in the stream."""
    phrase = EXAMPLES[idx]
    trigger = _literal_trigger(FORBIDDEN_PHRASE_PATTERNS[idx])
    text = CARRIER_HEAD + phrase + CARRIER_TAIL

    pieces, cumulative = _run(text, size)
    for step, seen in enumerate(cumulative):
        low = seen.lower()
        assert trigger not in low, (
            f"trigger {trigger!r} leaked at step {step} (chunk={size}): {seen!r}"
        )
        assert phrase.lower() not in low, f"phrase leaked at step {step}: {seen!r}"

    released = cumulative[-1]
    # The benign clinical prose around it survives intact.
    assert "The patient is stable." in released
    assert "Continue monitoring the heart rate." in released
    # And the released text is exactly what the buffered path would produce.
    assert released == _streaming_strip(text)


@pytest.mark.parametrize("size", [1, 3, 7])
def test_all_phrases_in_one_stream(size):
    """All fifteen confirmations in a single answer, chunk-spanning.

    UPDATED when the sequential-substitution defect was fixed: this input used
    to be where the BUFFERED strip was WEAKER than the streamed one. The strip
    substituted its patterns one after another over one string, so a bare
    "Done." that directly followed another stripped phrase lost the sentence
    boundary its lookbehind needs and survived — the test pinned that gap
    (`assert "done." in _streaming_strip(text)`). The strip now applies one
    alternation to a fixed point, so the two paths agree: BOTH remove it."""
    text = CARRIER_HEAD + " ".join(EXAMPLES) + CARRIER_TAIL
    _pieces, cumulative = _run(text, size)
    for seen in cumulative:
        low = seen.lower()
        for trig in (_literal_trigger(p) for p in FORBIDDEN_PHRASE_PATTERNS):
            assert trig not in low, f"trigger {trig!r} leaked: {seen!r}"
    assert "Continue monitoring the heart rate." in cumulative[-1]
    # No divergence left: the buffered strip is as strict as the streamed one.
    assert "done." not in _streaming_strip(text).lower()
    assert "done." not in _strip_forbidden(text).lower()
    assert "done." not in cumulative[-1].lower()


def test_phrase_split_mid_trigger_is_still_stripped():
    """The task's worked example: the trigger itself is torn in half."""
    s = SentenceStreamSanitizer(_streaming_strip)
    first = s.feed("I have add")
    assert first == "", f"released a partial trigger: {first!r}"
    rest = s.feed("ed the medication.") + s.flush()
    joined = first + rest
    assert "i have add" not in joined.lower()
    assert "medication" not in joined.lower()
    assert joined.strip() == ""


def test_trigger_completed_but_phrase_still_open_is_held():
    """`i have added\\b[^.]*\\.?` is UNBOUNDED — once the trigger has landed the
    match can still grow, so nothing from the match start may be released until
    the terminating period arrives."""
    s = SentenceStreamSanitizer(_streaming_strip)
    out = [s.feed("Noted. ")]
    # Released BEFORE the answer is complete. The trailing space is withheld
    # for one chunk since the "(DOC:" strip landed: " " is a prefix of its
    # space-eating variant's trigger " (doc:", so it could still become a
    # marker. It is released with the next feed — timing, never bytes.
    assert out[0] == "Noted."
    out.append(s.feed("I have added Paracetamol 500"))
    out.append(s.feed("mg to the chart."))
    out.append(s.feed(" Recheck in one hour."))
    out.append(s.flush())
    joined = "".join(out)
    assert "i have added" not in joined.lower()
    assert "paracetamol" not in joined.lower()
    assert joined == _streaming_strip("Noted. I have added Paracetamol 500mg to "
                                      "the chart. Recheck in one hour.")


# ─── Latency property ───

BENIGN_OPENING = "The CURB-65 score is a clinical prediction rule used to"


def test_benign_text_released_before_any_sentence_boundary():
    """THE latency property: no period has arrived, yet the clinician already has
    words on screen. The legacy sanitizer released nothing here."""
    s = SentenceStreamSanitizer(_streaming_strip)
    out = "".join(s.feed(c) for c in _chunks(BENIGN_OPENING, 4))
    assert out, "nothing released before the first sentence boundary"
    assert BENIGN_OPENING.startswith(out)
    # Baseline: the old behaviour released nothing at all.
    legacy = _LegacySanitizer(_streaming_strip)
    assert "".join(legacy.feed(c) for c in _chunks(BENIGN_OPENING, 4)) == ""


def test_first_release_is_immediate_for_benign_prose():
    """First words reach the client within the first few tokens, not at the first
    period. Worst case is bounded by the longest trigger, not by sentence length."""
    s = SentenceStreamSanitizer(_streaming_strip)
    first_at = None
    for i, c in enumerate(_chunks(BENIGN_OPENING, 4)):
        if s.feed(c):
            first_at = i
            break
    assert first_at is not None and first_at <= 1, (
        f"first release took {first_at} chunks"
    )


def test_hold_back_is_bounded_by_longest_trigger():
    """Invariant behind the latency claim: for text with no forbidden phrase in
    progress, the unreleased tail never exceeds the longest trigger literal."""
    _c, _p, maxtrig, _t = _hold_table(tuple(FORBIDDEN_PHRASE_PATTERNS))
    text = ("Escalate to the registrar if the respiratory rate stays above 30 "
            "breaths per minute or the patient becomes confused")
    s = SentenceStreamSanitizer(_streaming_strip)
    released = 0
    for i, c in enumerate(_chunks(text, 3)):
        released += len(s.feed(c))
        fed = min(len(text), (i + 1) * 3)
        assert fed - released <= maxtrig, f"held {fed - released} chars at {fed}"


# ─── Equivalence with the old behaviour ───

CORPUS = [
    "",
    "Short.",
    "No terminal period at all",
    "Line one\nLine two\n- bullet\n- bullet two",
    "The dose is 500 mg. He is done with the antibiotic course.",
    "Order placed. The remaining patients are stable.",
    "I have added metoprolol 25mg to the chart. Monitor the heart rate.",
    "Chart updated. I've ordered a chest X-ray. Escalate if SpO2 drops.",
    "Done. That is all.",
    "The patient is done. Reassess in the morning.",
    "**Critical patients:** Bed 4 needs eyes first. Order placed. Ward B is stable.\n"
    "- Ward A: 10 stable\n- Ward B: 8 stable\n",
    ("Sepsis six within one hour: take blood cultures, give IV antibiotics, "
     "measure lactate, give IV fluids, check urine output and give oxygen if "
     "SpO2 is below 94 percent. Reassess after the fluid bolus and escalate to "
     "critical care if the MAP stays under 65."),
    "I have added foo\nbar. Next.",             # unbounded match across a newline
    "Multi\n\nparagraph.\n\nI have added x\n\ny. End.",
]

# Inputs that used to expose the sequential-substitution defect: removing the
# FIRST phrase destroyed the sentence boundary the bare-"Done." lookbehind
# needs, so the second confirmation survived the buffered strip. The streamed
# path masked it (it strips per sentence unit, so "Done." was still sentence
# initial there), which is why the hole only ever showed on the buffered lane —
# the JSON/action-state path, and any caller of `_strip_forbidden`.
PIPELINE_REGRESSIONS = [
    "Order placed. Done.",
    CARRIER_HEAD + " ".join(EXAMPLES) + CARRIER_TAIL,
]


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 64, 4096])
@pytest.mark.parametrize("idx", range(len(CORPUS)))
def test_feed_plus_flush_equals_fully_stripped_text(idx, size):
    """feed()+flush() for arbitrary input equals the old behaviour's semantic
    result — the fully stripped text — at every chunk size."""
    text = CORPUS[idx]
    _pieces, cumulative = _run(text, size)
    assert cumulative[-1] == _streaming_strip(text)


@pytest.mark.parametrize("idx", range(len(CORPUS)))
def test_random_chunkings_match_fully_stripped_text(idx):
    """Property test over random (uneven) chunk boundaries — how a real token
    stream actually arrives."""
    text = CORPUS[idx]
    rng = random.Random(1234 + idx)
    for _ in range(25):
        s = SentenceStreamSanitizer(_streaming_strip)
        out, i = [], 0
        while i < len(text):
            step = rng.randint(1, 9)
            out.append(s.feed(text[i:i + step]))
            i += step
        out.append(s.flush())
        joined = "".join(out)
        assert joined == _streaming_strip(text), f"mismatch on {text!r}"


@pytest.mark.parametrize("idx", range(len(CORPUS) + len(PIPELINE_REGRESSIONS)))
def test_output_is_independent_of_chunk_size(idx):
    """The client sees the same bytes whether the model dribbles one character
    at a time or the whole answer lands in a single chunk."""
    texts = CORPUS + PIPELINE_REGRESSIONS
    text = texts[idx]
    outs = {"".join(_run(text, size)[0]) for size in (1, 2, 3, 5, 7, 13, 64, 4096)}
    assert len(outs) <= 1, f"chunking changed the output: {outs}"


@pytest.mark.parametrize("text", PIPELINE_REGRESSIONS)
def test_stranded_done_is_stripped_by_every_path(text):
    """UPDATED from `test_stricter_than_buffered_strip_on_a_stranded_done`,
    which asserted the buffered strip KEPT the stranded "Done." and only the
    streamed path removed it ("divergence in the safe direction"). That was the
    defect, not a design: a self-confirmation that the buffered lane emitted
    verbatim. Every path must now remove it, and they must agree."""
    assert "done." not in _streaming_strip(text).lower()
    assert "done." not in _strip_forbidden(text).lower()
    for size in (1, 4, 4096):
        released = "".join(_run(text, size)[0])
        assert "done." not in released.lower()
        assert released == _streaming_strip(text)


# ══════════════════════════════════════════════════════════════════════
# The strip must be a FIXED POINT (defect 1)
#
# The strip used to be a PIPELINE: one re.sub per pattern, in list order, over
# one string. Deleting an earlier phrase destroyed the sentence boundary a later
# pattern's lookbehind needed, so a self-confirmation SURVIVED:
#     _strip_forbidden("Order placed. Done.")  ->  "Done."
# It is now one alternation applied until the string stops changing, which makes
# the guarantee statable and testable directly: the OUTPUT CONTAINS NO MATCH OF
# ANY FORBIDDEN PATTERN, for any input.
# ══════════════════════════════════════════════════════════════════════

TRIGGERS = [_literal_trigger(p) for p in FORBIDDEN_PHRASE_PATTERNS]

_STRIPS = {"buffered": _strip_forbidden, "streaming": _strip_forbidden_streaming}


def _assert_fixed_point(out: str, src: str, *, triggers: bool = True):
    """The defining property: nothing in `out` is still matched by any forbidden
    pattern (and, where the surrounding prose is known-clean, no trigger literal
    survives either)."""
    for pat in FORBIDDEN_PHRASE_PATTERNS:
        m = re.search(pat, out)
        assert m is None, (
            f"{pat!r} still matches {m.group(0)!r} in the stripped output "
            f"{out!r} (from {src!r}) — the strip is not a fixed point"
        )
    if triggers:
        low = out.lower()
        for trig in TRIGGERS:
            assert trig not in low, f"trigger {trig!r} survived in {out!r} (from {src!r})"


@pytest.mark.parametrize("lane", sorted(_STRIPS))
def test_stranded_done_regression_order_placed_then_done(lane):
    """THE reported regression, pinned verbatim. Sequentially, "Order placed."
    was removed first, which left " Done." with no sentence terminator in front
    of it, so `(?:^|(?<=[.!?]\\s)|(?<=\\n))done[.!]` no longer matched and the
    self-confirmation reached the clinician."""
    out = _STRIPS[lane]("Order placed. Done.")
    assert "done" not in out.lower(), f"self-confirmation survived: {out!r}"
    assert "order placed" not in out.lower()
    assert out.strip() == ""
    _assert_fixed_point(out, "Order placed. Done.")


@pytest.mark.parametrize("lane", sorted(_STRIPS))
@pytest.mark.parametrize("idx", range(len(EXAMPLES)))
def test_every_forbidden_phrase_is_stripped_by_the_buffered_strip(idx, lane):
    """Table-driven over FORBIDDEN_PHRASE_PATTERNS: each phrase, in a carrier
    that supplies the sentence-initial context, is removed and the clinical
    prose around it survives."""
    text = CARRIER_HEAD + EXAMPLES[idx] + CARRIER_TAIL
    out = _STRIPS[lane](text)
    _assert_fixed_point(out, text)
    assert "The patient is stable." in out
    assert "Continue monitoring the heart rate." in out


@pytest.mark.parametrize("lane", sorted(_STRIPS))
@pytest.mark.parametrize("first", range(len(EXAMPLES)))
def test_adjacent_pairs_of_forbidden_phrases_are_both_stripped(first, lane):
    """Every ADJACENT PAIR "<phrase A>. <phrase B>." — 15x15 combinations, the
    shape that broke the old pipeline. Removing A must not make B invisible, and
    removing B must not make A invisible."""
    strip = _STRIPS[lane]
    for second in range(len(EXAMPLES)):
        pair = f"{EXAMPLES[first]} {EXAMPLES[second]}"
        for text in (pair, CARRIER_HEAD + pair + CARRIER_TAIL):
            out = strip(text)
            # No pattern still matches AND no trigger literal survives, so
            # neither phrase can have been hidden by the other's removal.
            _assert_fixed_point(out, text)
        # Adjacency must not change WHAT survives: the pair's residue is
        # exactly each phrase's own residue, in order ("The" from "The
        # medication was added.", the surviving prose around a "(DOC: …)"
        # marker) — never a confirmation the neighbour's removal exposed or
        # hid. Stronger than the old ≤2-word bound: it pins the tokens, and it
        # holds for the DOC examples whose sentences legitimately keep words.
        residue = strip(pair).strip()
        expected = f"{strip(EXAMPLES[first])} {strip(EXAMPLES[second])}"
        assert residue.split() == expected.split(), (
            f"residue {residue!r} from {pair!r} (expected {expected!r})"
        )


@pytest.mark.parametrize("lane", sorted(_STRIPS))
@pytest.mark.parametrize("text", [
    "The procedure was done carefully.",
    "Continue the antibiotics until the infusion is done.",
    "He is done with the antibiotic course. Reassess in the morning.",
    "Ask the registrar whether the tap has been done.",
    "The dose is 500 mg. She is done fasting.",
])
def test_legitimate_prose_survives(text, lane):
    """The strip must not become destructive to real clinical prose: "done"
    mid-sentence is legitimate English and is NOT a self-confirmation."""
    out = _STRIPS[lane](text)
    assert "done" in out.lower(), f"legitimate 'done' was destroyed: {out!r}"
    # Only the trailing whitespace policy may differ from the input.
    assert out.strip() == text.strip()


@pytest.mark.parametrize("lane", sorted(_STRIPS))
@pytest.mark.parametrize("text", CORPUS + PIPELINE_REGRESSIONS + [
    "Order placed. Done. I have added metoprolol. Done!",
    "Chart updated.\nDone.\nOrder placed.",
    "I've addedI have added Paracetamol.",          # \b exposed by a deletion
    "Done. Done. Done. Done. Done. Done. Done. Done. Done.",
])
def test_strip_is_idempotent_and_settles(text, lane):
    """A fixed point by definition: re-running the strip changes nothing. This is
    also the termination check — if the loop had not settled within its cap the
    second application would still be removing text."""
    strip = _STRIPS[lane]
    once = strip(text)
    assert strip(once) == once, f"not settled: {text!r} -> {once!r} -> {strip(once)!r}"
    _assert_fixed_point(once, text, triggers=False)


def _passes_to_settle(text: str) -> int:
    """How many passes the shipped loop needs on this input (the loop body,
    replayed here uncapped)."""
    from routes.sage_routes import _FORBIDDEN_ALTERNATION
    n = 0
    while n < 500:
        nxt = re.sub(r"\s{2,}", " ", _FORBIDDEN_ALTERNATION.sub("", text)).strip()
        n += 1
        if nxt == text:
            return n
        text = nxt
    raise AssertionError("did not settle in 500 passes")


@pytest.mark.parametrize("text,limit", [
    (CARRIER_HEAD + " ".join(EXAMPLES) + CARRIER_TAIL, 3),   # every phrase, once
    ("Order placed. Done.", 3),                              # the regression
    ("Escalate to the registrar. " * 40, 3),                 # 1 kB of benign prose
    ("I've added" * 14 + ".", 16),                           # 14 stacked exposures
])
def test_loop_settles_well_inside_the_iteration_cap(text, limit):
    """The cap is a guard against a pathological pattern spinning forever, NOT
    the mechanism: it must never be what stops a real strip. One pass per
    EXPOSURE LAYER — normal prose (however long) settles in 2, and even 14
    deliberately stacked confirmations settle far inside the cap."""
    from routes.sage_routes import _STRIP_MAX_PASSES
    needed = _passes_to_settle(text)
    assert needed <= limit, f"took {needed} passes"
    assert needed < _STRIP_MAX_PASSES, (
        f"cap {_STRIP_MAX_PASSES} is too tight: this input needs {needed}"
    )
    _assert_fixed_point(_strip_forbidden(text), text, triggers=False)


def test_iteration_cap_is_bounded_and_termination_is_monotone():
    """Termination does not actually depend on the cap: the only replacement is
    the empty string, so any pass that changes the text strictly shortens it."""
    from routes.sage_routes import _FORBIDDEN_ALTERNATION, _STRIP_MAX_PASSES
    assert 2 <= _STRIP_MAX_PASSES <= 64
    for text in CORPUS + PIPELINE_REGRESSIONS + ["I've added" * 20 + "."]:
        cur = text
        for _ in range(_STRIP_MAX_PASSES):
            nxt = _FORBIDDEN_ALTERNATION.sub("", cur)
            if nxt == cur:
                break
            assert len(nxt) < len(cur), "a pass changed the text without shortening it"
            cur = nxt


# ══════════════════════════════════════════════════════════════════════
# ONE pattern list, shared by every surface (defect 2)
#
# The voice route used to carry a hand-copied clone of FORBIDDEN_PHRASE_PATTERNS
# and the clone had already drifted: it was missing the `(?<=\n)` anchor added to
# the bare-"Done." pattern, so a line-initial "Done." was SPOKEN while the chat
# lane stripped it. Two divergent copies of a clinical guardrail is the defect
# class, not the specific miss.
# ══════════════════════════════════════════════════════════════════════

def test_voice_route_declares_no_pattern_list_of_its_own():
    """Source-level guard: every forbidden pattern literal in this module starts
    with `r"(?i)`. The voice route must contain none of them."""
    src = inspect.getsource(sage_routes.sage_voice_query)
    assert 'r"(?i)' not in src, (
        "the voice route defines its own regex literals again — it must use "
        "FORBIDDEN_PHRASE_PATTERNS / _strip_forbidden_streaming"
    )
    assert "SentenceStreamSanitizer(_strip_forbidden_streaming)" in src, (
        "the voice lane must build its sanitizer from the shared strip"
    )
    # And the list itself is declared exactly once in the whole module.
    module_src = inspect.getsource(sage_routes)
    assert module_src.count("FORBIDDEN_PHRASE_PATTERNS = [") == 1


def test_voice_lane_and_text_lane_use_the_same_pattern_list():
    """Not 'the same patterns' by eyeball — the SAME LIST OBJECT, reached
    through the same hold table."""
    voice = SentenceStreamSanitizer(_strip_forbidden_streaming)    # as the voice route builds it
    chat = SentenceStreamSanitizer(_strip_forbidden_streaming)     # as the chat route builds it
    shared, _p, _m, _t = _hold_table(tuple(FORBIDDEN_PHRASE_PATTERNS))
    assert voice._compiled is chat._compiled is shared
    assert voice._strip is chat._strip is _strip_forbidden_streaming
    assert voice._triggers == chat._triggers == tuple(TRIGGERS)


@pytest.mark.parametrize("size", [1, 5, 4096])
def test_voice_lane_strips_the_same_set_as_the_text_lane(size):
    """Behavioural parity over every phrase, every pair-with-carrier and the
    whole corpus: byte-for-byte identical output on both lanes."""
    texts = (CORPUS + PIPELINE_REGRESSIONS
             + [CARRIER_HEAD + e + CARRIER_TAIL for e in EXAMPLES]
             + [f"{a} {b}" for a in EXAMPLES for b in EXAMPLES[-3:]])
    for text in texts:
        voice = SentenceStreamSanitizer(_strip_forbidden_streaming)
        chat = SentenceStreamSanitizer(_strip_forbidden_streaming)
        spoken = "".join(voice.feed(c) for c in _chunks(text, size)) + voice.flush()
        streamed = "".join(chat.feed(c) for c in _chunks(text, size)) + chat.flush()
        assert spoken == streamed, f"lanes diverged on {text!r}"
        _assert_fixed_point(spoken, text, triggers=False)


def test_the_drifted_voice_copy_would_have_spoken_a_confirmation():
    """The concrete hole the duplicate had opened, kept as documentation: the
    voice clone's "Done." pattern lacked the `(?<=\\n)` anchor, so a line-initial
    "Done." after a heading was spoken aloud. The shared list removes it."""
    drifted = FORBIDDEN_PHRASE_PATTERNS[:-1] + [r"(?i)(?:^|(?<=[.!?]\s))done[.!]"]

    def drifted_strip(text):
        for pat in drifted:
            text = re.sub(pat, "", text)
        return text

    text = "Ward round complete:\nDone."
    old = SentenceStreamSanitizer(drifted_strip, patterns=drifted)
    leaked = "".join(old.feed(c) for c in _chunks(text, 5)) + old.flush()
    assert "done." in leaked.lower(), "input no longer exercises the drift"

    now = SentenceStreamSanitizer(_strip_forbidden_streaming)
    spoken = "".join(now.feed(c) for c in _chunks(text, 5)) + now.flush()
    assert "done" not in spoken.lower(), f"voice lane still speaks it: {spoken!r}"
    assert "Ward round complete:" in spoken


def test_never_less_sanitized_than_the_legacy_implementation():
    """Nothing the old sanitizer removed may reappear in the new one's output."""
    triggers = [_literal_trigger(p) for p in FORBIDDEN_PHRASE_PATTERNS]
    for text in CORPUS + PIPELINE_REGRESSIONS:
        for size in (1, 4, 11, 4096):
            new = "".join(_run(text, size)[0]).lower()
            legacy = _LegacySanitizer(_streaming_strip)
            old = ("".join(legacy.feed(c) for c in _chunks(text, size))
                   + legacy.flush()).lower()
            for trig in triggers:
                if trig in new:
                    assert trig in old, (
                        f"{trig!r} survives the new sanitizer but not the old "
                        f"one, on {text!r} at chunk {size}"
                    )


# ─── Randomised adversarial soak ───
# Concatenated confirmations with no separators ("I've addedI have addedorder
# placed") are what broke three earlier versions of this class: a release point
# that fell inside a complete match, a phrase the strip PIPELINE created by
# deleting the text between its halves, and a \b that only became a word
# boundary once a later phrase was removed. Fixed seeds, so a regression is
# reproducible rather than flaky.

_SOAK_FRAGMENTS = [
    "I have added", "I've added", "order placed", "Order has been placed",
    "chart updated", "Done", "done", "medication was added",
    "This was performed", "this action has been recorded", " Paracetamol 500mg",
    ".", "!", "?", " ", "\n", "the patient", "escalate", "I", "have", "ad",
    "Ord", "er", "plac", "ed", "  ", "\n\n", "-", "**bold**", "X-ray",
    "recorded", "placed", "ordered",
]


def _triggers_in(text: str) -> set:
    low = text.lower()
    return {t for t in (_literal_trigger(p) for p in FORBIDDEN_PHRASE_PATTERNS)
            if t in low}


@pytest.mark.parametrize("seed", [1, 7, 99, 20260731])
def test_adversarial_soak_never_worse_than_legacy_and_never_leaks(seed):
    rng = random.Random(seed)
    for _ in range(250):
        text = "".join(rng.choice(_SOAK_FRAGMENTS)
                       for _ in range(rng.randint(1, 14)))
        # 3. Deterministic: how the model happens to chunk its output cannot
        #    change a single released byte.
        fixed = {"".join(_run(text, size)[0]) for size in (1, 3, 8, 4096)}
        assert len(fixed) == 1, f"chunking changed the output on {text!r}: {fixed}"
        for _ in range(2):
            parts, i = [], 0
            while i < len(text):
                step = rng.randint(1, 6)
                parts.append(text[i:i + step])
                i += step
            s = SentenceStreamSanitizer(_streaming_strip)
            pieces, cumulative = [], []
            for c in parts:
                pieces.append(s.feed(c))
                cumulative.append("".join(pieces))
            pieces.append(s.flush())
            new = "".join(pieces)

            legacy = _LegacySanitizer(_streaming_strip)
            old = "".join(legacy.feed(c) for c in parts) + legacy.flush()

            # 1. Never less sanitized than the implementation this replaced.
            assert not (_triggers_in(new) - _triggers_in(old)), (
                f"worse than legacy on {text!r}: {new!r} vs {old!r}"
            )
            # 2. Nothing may flash on screen that the final text does not keep —
            #    a released byte cannot be taken back.
            for seen in cumulative:
                assert not (_triggers_in(seen) - _triggers_in(new)), (
                    f"mid-stream leak on {text!r}: {seen!r}"
                )


def test_trimming_strip_no_longer_glues_markdown_together():
    """With the trimming/collapsing `_strip_forbidden`, the legacy sanitizer
    trimmed EVERY released line and so welded list items together. The new one
    strips whole sentence units, so internal newlines survive."""
    text = "Line one\nLine two\n- bullet\n- bullet two"
    new_pieces, _c = _run(text, 4, strip_fn=_strip_forbidden)
    legacy = _LegacySanitizer(_strip_forbidden)
    old = "".join(legacy.feed(c) for c in _chunks(text, 4)) + legacy.flush()
    assert "Line oneLine two" in old                      # the old defect
    assert "".join(new_pieces) == _strip_forbidden(text)   # fixed


def test_flush_releases_everything_remaining():
    text = "Escalate if the lactate is ri"
    s = SentenceStreamSanitizer(_streaming_strip)
    first = s.feed(text)
    # Only the trailing "i" is held back — it is a prefix of the "i have added"
    # family of triggers. Everything before it is provably safe.
    assert first == "Escalate if the lactate is r"
    assert s.flush() == "i"
    assert s.flush() == ""
    assert first + "i" == text


def test_empty_and_noop_feeds():
    s = SentenceStreamSanitizer(_streaming_strip)
    assert s.feed("") == ""
    assert s.flush() == ""
    assert s.feed("Hi.") == "Hi."
    assert s.feed("") == ""


def test_custom_pattern_set_is_honoured():
    """A caller with its own patterns (the voice lane mirrors the list) gets
    hold-back derived from those patterns."""
    pats = [r"(?i)zebra crossing\b[^.]*\.?"]

    def strip(text):
        for p in pats:
            text = re.sub(p, "", text)
        return text

    s = SentenceStreamSanitizer(strip, patterns=pats)
    assert s.feed("Watch the zebra cro") == "Watch the "
    assert "zebra" not in (s.feed("ssing now.") + s.flush()).lower()


@pytest.mark.parametrize("pattern", [
    r"(?i)\d+ units given\.?",                  # leading \d — no literal at all
    r"(?i)order (?:placed|submitted)\.?",       # alternation after the literal
    r"(?i)chart [ud]pdated",                    # mandatory class mid-pattern
    r"(?i)done\s*[.!]",                         # mandatory class after an optional
    r"(?i)i have added\b[^.]{0,40}\.?",         # counted repetition
    r"(?i)^(?:done|fine)[.!]",                  # leading alternation
])
def test_unsafe_pattern_shapes_are_refused(pattern):
    """A trigger may only be derived when every proper prefix of a match that is
    longer than the trigger is itself matched by the pattern. Otherwise the
    sanitizer could release "order pla" before "order placed." completed, so the
    derivation must refuse the pattern rather than guess."""
    with pytest.raises(_UndecidableTrigger):
        _literal_trigger(pattern)


def test_undecidable_pattern_falls_back_to_sentence_granularity():
    """Fail-safe: a pattern whose trigger cannot be derived must not silently
    disable hold-back — the sanitizer reverts to the old sentence behaviour,
    which is slower but never leaks."""
    pats = [r"(?i)order (?:placed|submitted)\.?"]

    def strip(text):
        for p in pats:
            text = re.sub(p, "", text)
        return text

    _c, prefixes, _m, _t = _hold_table(tuple(pats))
    assert prefixes is None                      # derivation refused this pattern
    s = SentenceStreamSanitizer(strip, patterns=pats)
    assert s.feed("Order sub") == ""             # no boundary yet → holds all
    assert s.feed("mitted. Next.") .strip() == ""  # phrase stripped on release
    out = s.flush()
    assert "order sub" not in out.lower()
    assert out.strip() == "Next."


# ─── Cost guard ───

def test_release_cost_is_linear_enough_for_a_long_answer():
    """The buffer is committed at sentence boundaries, so a long answer does not
    turn into O(n^2) regex work on the streaming path."""
    sentence = ("Escalate to the on-call registrar if the respiratory rate "
                "remains above thirty breaths per minute. ")
    text = sentence * 40                       # ~4 kB, a long broad-scope answer
    t0 = time.perf_counter()
    s = SentenceStreamSanitizer(_streaming_strip)
    out = "".join(s.feed(c) for c in _chunks(text, 4)) + s.flush()
    elapsed = time.perf_counter() - t0
    assert out == _streaming_strip(text)
    assert elapsed < 0.5, f"sanitizer took {elapsed * 1000:.0f} ms for {len(text)} chars"


# ─── The measured latency saving (reported, not just asserted) ───

TYPICAL_OPENING = (
    "The CURB-65 score is a clinical prediction rule used to assess the "
    "severity of community-acquired pneumonia and to decide whether a patient "
    "can be managed at home. It scores one point each for confusion, urea above "
    "7 mmol/L, respiratory rate of 30 or more, blood pressure below 90 systolic "
    "or 60 diastolic, and age 65 or over."
)


def test_measured_saving_versus_legacy():
    """Characters released earlier than the legacy sentence-based logic, for a
    typical answer opening streamed at ~4 chars (one token) per chunk."""
    chunks = _chunks(TYPICAL_OPENING, 4)
    new = SentenceStreamSanitizer(_streaming_strip)
    old = _LegacySanitizer(_streaming_strip)
    new_at = old_at = None
    lead_chars = 0
    n_out = o_out = 0
    for i, c in enumerate(chunks):
        n_out += len(new.feed(c))
        o_out += len(old.feed(c))
        if new_at is None and n_out:
            new_at = i
        if old_at is None and o_out:
            old_at = i
        lead_chars = max(lead_chars, n_out - o_out)
    print(f"\n[latency] first release: new chunk #{new_at}, legacy chunk #{old_at} "
          f"({(old_at - new_at)} tokens earlier ≈ "
          f"{(old_at - new_at) / 15.0:.2f}s at 15 tok/s); "
          f"max lead {lead_chars} chars")
    assert new_at is not None and old_at is not None
    assert new_at < old_at, "new sanitizer must release before the legacy one"
    assert lead_chars > 0
