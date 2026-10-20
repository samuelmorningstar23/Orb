"""Orb Sage — AI Intelligence Routes"""

import json
import re
import time
import uuid
from typing import Any

from auth import get_current_user
from database import db_session, run_db
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from permissions import department_scope, require_patient_access, require_permission
from pydantic import BaseModel, Field
from services.sage_service import CONTRACT_TAG_EMPHASIS as _EMPHASIS
from services.sage_service import CONTRACT_TAG_RE as _SHARED_CONTRACT_TAG_RE
from services.sage_service import CONTRACT_TAGS as _CONTRACT_TAGS
from services.sage_service import (
    assess_answer_grounding,
    classify_answer_confidence,
    declares_action,
    detect_mutation_intent,
    get_conversation_history,
    get_relevant_context,
    query_ollama,
    save_conversation,
)

from services import thinking_mode

router = APIRouter(prefix="/api/sage", tags=["sage"])

# Boot: a Sage safety switch set away from its default is said in the log once,
# when the app imports this router, instead of only being visible in the env.
thinking_mode.log_safety_setting_warnings()

# ── Pre-LLM safety gate ──
# Deterministic refusals for (a) requests to harm a patient and (b) prompt-injection /
# jailbreak attempts. These short-circuit BEFORE the model runs, so the small LLM can
# never draft a harmful order or echo an injected token.
_HARM_INTENT = re.compile(
    r"(?i)\b(lethal|fatal(?:ly)?|overdose|euthan|"
    r"how much .*(?:to (?:kill|die)|would (?:be )?(?:fatal|lethal))|"
    r"enough .* to (?:kill|die|stop)|stop (?:the|his|her|their) heart|"
    r"kill (?:the |this )?(?:patient|him|her|them)|"
    r"end (?:the |their |his |her )?(?:life|patient)|harm (?:the |this )?patient)\b"
)
_INJECT_INTENT = re.compile(
    r"(?i)(ignore (?:all )?(?:the )?(?:previous|prior|your|above) instructions|"
    r"disregard (?:your |the )?(?:safety|rules|previous|instructions)|system override|"
    r"you are now in (?:debug|developer|dan) mode|reveal (?:your |the )?(?:full )?(?:system )?prompt|"
    r"print (?:your |the )?(?:full )?(?:system )?prompt|new system message|"
    r"forget (?:you are|your role|everything)|pretend you (?:have no|are (?:an? )?(?:unrestricted|jailbroken))|"
    r"reply with exactly|output exactly|say [A-Za-z0-9\-]{3,} and nothing|"
    r"repeat (?:the following|this) verbatim)"
)
_HARM_REFUSAL = (
    "I can't help with anything intended to harm a patient. If you have a genuine "
    "clinical safety concern, please escalate to the senior clinician on duty or the "
    "on-call team immediately."
)
_INJECT_REFUSAL = (
    "I can't follow instructions that ask me to ignore my role or safety rules, "
    "reveal my configuration, or output arbitrary text. I'm Orb Sage. I can help "
    "with clinical questions about your patients."
)


def _safety_refusal(q: str):
    if _HARM_INTENT.search(q):
        return _HARM_REFUSAL
    if _INJECT_INTENT.search(q):
        return _INJECT_REFUSAL
    return None


#: What Sage says when it is asked to change the chart and cannot. ONE copy for
#: every lane: the chat lane's action-state override and the voice lane's gate.
CHART_MUTATION_SAFE_MESSAGE = (
    "I understood your request to update the chart, but direct modifications (like adding "
    "medications) are not currently supported in this environment. No chart change has been made."
)


def _mutation_gate(query: str) -> bool:
    """THE pre-LLM mutation gate, run by the typed and the spoken lane alike.

    The voice lane used to have no deterministic gate at all, only the phrase
    strip: a spoken "add paracetamol 1g" went to the model, and any wording
    outside the forbidden list ("has been added") could be spoken. A gate on one
    surface only is how a hole ships in the other.
    """
    return detect_mutation_intent(query)


# ── Forbidden self-confirmation phrases ──
# NO_ACTION_HALLUCINATION applies to EVERY response: Sage is read-only and must
# never claim it changed the chart. This strip runs on the concise answer
# regardless of whether a mutation was detected. It is module-level (not a closure)
# so the incremental streaming path, the buffered path and the VOICE lane share
# exactly one list, and so it can be unit-tested directly (see
# tests/test_sage_streaming.py). THIS IS THE ONLY COPY — a hand-copied clone in
# the voice route had already drifted (it was missing the line-initial "Done."
# anchor below), so any new surface must import this list, never restate it.
FORBIDDEN_PHRASE_PATTERNS = [
    r"(?i)this action has been recorded\.?",
    r"(?i)this was performed\.?",
    r"(?i)medication was added\.?",
    r"(?i)chart updated\.?",
    r"(?i)order placed\.?",
    r"(?i)order has been placed\.?",
    r"(?i)i have added\b[^.]*\.?",
    r"(?i)i've added\b[^.]*\.?",
    r"(?i)i have recorded\b[^.]*\.?",
    r"(?i)i've recorded\b[^.]*\.?",
    r"(?i)i have placed\b[^.]*\.?",
    r"(?i)i've placed\b[^.]*\.?",
    r"(?i)i have ordered\b[^.]*\.?",
    r"(?i)i've ordered\b[^.]*\.?",
    # ── Hospital work Sage cannot do, in the FUTURE tense (2026-09-07) ──
    # The patterns above cover the PERFECT tense only ("I have added"), and the
    # measured leak is a PROMISE: on the ungated imperatives of
    # docs/perf/FAST_LANE_MARKER_2026-09-07.md section 7b, medgemma:4b answered
    # "Okay, I understand. I will cancel the morning bloods." and "Okay, I can
    # schedule physiotherapy for tomorrow. I will add that to the patient's
    # plan." (3 of 48 turns), and medgemma:27b answered "Okay, I will record a
    # blood pressure of 130/80 mmHg in the patient's chart." (2 of 30). Those
    # turns trip neither deterministic gate, so nothing buffered them and
    # nothing rewrote them; the sentence reached the clinician whole. A promise
    # is the same defect as a confirmation — the bloods are still drawn — and
    # this list is where Orb says so.
    #
    # NOTE WHICH MODEL SAID WHICH. The chart claim came from the CAREFUL tier,
    # so this is not a fast-lane patch and routing alone would not have caught
    # it: `thinking_mode` floor rule 4 sends these turns to the 27B, and the
    # 27B is the one that offered to write the observation into the chart.
    #
    # WHICH VERBS, AND WHY NOT MORE. book/schedule/cancel/arrange are the verbs
    # whose FIRST-PERSON reading is necessarily false: Sage is read-only, there
    # is no booking, scheduling or cancellation endpoint behind it, and none of
    # the four has a benign first-person sense in clinical prose. All 26
    # patterns below were run over the 540 real Sage answers in
    # docs/perf/sage-*.json: zero sentences deleted, i.e. this removes nothing
    # Orb has ever actually said.
    #
    # DELIBERATELY EXCLUDED, because their first-person reading is often true:
    # "I can order the differentials by likelihood", "I will refer to the local
    # antibiotic policy", "I will raise the possibility of sepsis", "I can
    # request more detail", "I can place this in context". Every one of those is
    # a sentence Sage is allowed to say, and the tail here eats to the next
    # period, so a false positive costs a whole sentence of real clinical prose.
    # tests/test_action_lane_gates.py carries that must-NOT-strip table.
    #
    # THE RULE FOR WHICH AUXILIARIES EACH VERB GETS, so the next addition does
    # not have to re-derive it:
    #   * a verb Sage can NEVER do (book/schedule/cancel/arrange) gets the
    #     capability form too, because "I can schedule physiotherapy for
    #     tomorrow" is already false and was one of the measured leaks;
    #   * a verb with a benign non-chart sense (add, record) gets only the
    #     PROMISE forms. "I will record a blood pressure of 130/80 in the
    #     patient's chart" is a chart claim and was measured on the CAREFUL
    #     tier, 2 of 5 runs; "I can add some context on the mechanism" and
    #     "I can record the differentials" are things Sage may legitimately say,
    #     and the tail here eats to the next period.
    #
    # One pattern per (auxiliary, verb) pair on purpose. `_literal_trigger`
    # refuses an alternation after the leading literal (_tokenize_tail raises
    # "group/alternation in tail"), so `i (?:will|can) book` would silently drop
    # the whole sanitizer to sentence granularity. Flat is the shape this list
    # has to have.
    #
    # THE `(?<![^\W_])` IS NOT DECORATION, and it is why these carry a guard the
    # perfect-tense patterns above do not. "i can book" is a substring of
    # "Mumbai can book", "i can cancel" of "Ravi can cancel" — and this is an
    # Indian hospital, where names ending in -i are everywhere. Unguarded, "Dr
    # Ravi can cancel the morning bloods" would be deleted from a legitimate
    # answer about who does what on the ward. The older patterns are safe
    # without it only by grammar ("Ravi have added" is not a sentence anyone
    # writes), so this is not an inconsistency worth "fixing" upward.
    #
    # LETTERS AND DIGITS ONLY, NOT `\w`. `_` is a word character, so the first
    # version, `(?<!\w)`, failed on markdown underscore emphasis and
    # "_I will cancel the morning bloods._" reached the clinician whole on both
    # lanes while the `*...*` form was stripped. `[^\W_]` is `\w` minus the
    # underscore: a name still blocks the match, emphasis no longer does. A
    # lookbehind is zero-width, so `_literal_trigger` skips it and the derived
    # trigger is still "i will book" (tests/test_stream_sanitizer.py asserts the
    # whole list derives cleanly, which is what stops this being silent).
    r"(?i)(?<![^\W_])i will book\b[^.]*\.?",
    r"(?i)(?<![^\W_])i'll book\b[^.]*\.?",
    r"(?i)(?<![^\W_])i can book\b[^.]*\.?",
    r"(?i)(?<![^\W_])i have booked\b[^.]*\.?",
    r"(?i)(?<![^\W_])i've booked\b[^.]*\.?",
    r"(?i)(?<![^\W_])i will schedule\b[^.]*\.?",
    r"(?i)(?<![^\W_])i'll schedule\b[^.]*\.?",
    r"(?i)(?<![^\W_])i can schedule\b[^.]*\.?",
    r"(?i)(?<![^\W_])i have scheduled\b[^.]*\.?",
    r"(?i)(?<![^\W_])i've scheduled\b[^.]*\.?",
    r"(?i)(?<![^\W_])i will cancel\b[^.]*\.?",
    r"(?i)(?<![^\W_])i'll cancel\b[^.]*\.?",
    r"(?i)(?<![^\W_])i can cancel\b[^.]*\.?",
    r"(?i)(?<![^\W_])i have cancelled\b[^.]*\.?",
    r"(?i)(?<![^\W_])i've cancelled\b[^.]*\.?",
    r"(?i)(?<![^\W_])i have canceled\b[^.]*\.?",
    r"(?i)(?<![^\W_])i've canceled\b[^.]*\.?",
    r"(?i)(?<![^\W_])i will arrange\b[^.]*\.?",
    r"(?i)(?<![^\W_])i'll arrange\b[^.]*\.?",
    r"(?i)(?<![^\W_])i can arrange\b[^.]*\.?",
    r"(?i)(?<![^\W_])i have arranged\b[^.]*\.?",
    r"(?i)(?<![^\W_])i've arranged\b[^.]*\.?",
    r"(?i)(?<![^\W_])i will add\b[^.]*\.?",
    r"(?i)(?<![^\W_])i'll add\b[^.]*\.?",
    r"(?i)(?<![^\W_])i will record\b[^.]*\.?",
    r"(?i)(?<![^\W_])i'll record\b[^.]*\.?",
    # ── Retrieval markup (docs/DEMO_READINESS.md §3.4) ──
    # The prompt cites guideline passages as "(DOC: LOC-SAFE-007)"
    # (sage_service._get_guideline_context) and the model echoes that marker
    # into its prose. A clinician must never see a document id; the citation
    # METADATA path is untouched because <cited_sources> and the provenance
    # panel carry BARE ids, never this parenthesised form.
    #
    # No anchors: the marker is position-independent, so neither the zero-width
    # anchor vocabulary below nor the \n/\A twin rule applies. Both tails are
    # prefix-closed (every token matches empty — see _tail_is_incremental_safe)
    # so the streamed lane can hold from the "(doc:" trigger and release the
    # instant the marker cannot grow. `[^)\n]*` deliberately excludes newline:
    # an unterminated "(DOC:" then deletes at most to end-of-line instead of
    # swallowing the rest of the answer.
    #
    # Two alternatives, not one: the first eats the space BEFORE an inline
    # marker ("per policy (DOC: X), escalate" → "per policy, escalate"); a
    # leading ` ?` on a single pattern would make its trigger underivable (the
    # literal would start at an optional atom). The second catches markers at
    # answer/line start and eats the gap AFTER instead. They sit ABOVE the
    # bare-"Done." pattern because that one must stay LAST in this list —
    # tests/test_stream_sanitizer.py's voice-drift regression rebuilds the list
    # with [:-1] to swap it for its drifted ancestor.
    r"(?i) \(DOC:[^)\n]*\)?",
    r"(?i)\(DOC:[^)\n]*\)?[ ]?",
    # Only strip "Done" as a standalone confirmation sentence, not the word
    # "done" mid-sentence (which is legitimate clinical prose). A line-initial
    # "Done." counts as standalone too — the old sentence-chunked streamer
    # stripped it as a side effect of splitting on "\n", and dropping that
    # would have been a (small) regression when the sanitizer stopped splitting
    # there, so the anchor is now explicit and both paths get it.
    #
    # EVERY alternative here is ZERO-WIDTH (anchor or lookbehind) on purpose.
    # A consuming prefix like `[ \t]*` would be simpler, but it makes
    # _literal_trigger undecidable — the trigger would no longer start at
    # "done", the hold table would collapse, and SentenceStreamSanitizer would
    # fall back to sentence granularity, costing ~1.2 s of first-word latency.
    # Python allows lookbehinds of DIFFERENT fixed widths as separate
    # alternatives, so the whole gap vocabulary is spelled out instead:
    #   no gap / one whitespace / two whitespace after a sentence terminator
    #   (two spaces between sentences is common and used to defeat the anchor
    #   entirely), line start, indented line start, and a markdown bullet —
    #   which is exactly how this model formats action summaries.
    # `(?<=\A\s)`/`(?<=\A\s\s)` are not redundant with `^`: the streamed lane
    # feeds the sanitizer the body AFTER the NOACT| marker, and the model
    # virtually always emits a space there — so the whole answer "NOACT| Done."
    # arrives as " Done." and a bare `^` never fires on it. That is the single
    # most realistic way a fabricated confirmation reaches a clinician.
    r"(?i)(?:^|(?<=\A\s)|(?<=\A\s\s)"
    r"|(?<=[.!?])|(?<=[.!?]\s)|(?<=[.!?]\s\s)"
    r"|(?<=\n)|(?<=\n\s)|(?<=\n-\s)|(?<=\n\*\s)"
    # The bullet anchors are duplicated against \A because the STREAMING lane
    # applies the strip per released unit and the units are split on "\n" — so
    # the newline the `(?<=\n-\s)` anchor needs is in the PREVIOUS unit and can
    # never fire. Without these the buffered lane stripped "- Done." off the end
    # of an action summary and the streamed lane shipped it.
    r"|(?<=\A-\s)|(?<=\A\*\s)"
    r")done[.!]",
]


# ── How the strip is applied ──
#
# Two properties are needed and the old implementation (one re.sub per pattern,
# in list order, over one string) had neither:
#
#   1. ONE PASS, NOT A PIPELINE. Deleting an earlier phrase used to destroy the
#      context a later pattern's LOOKBEHIND depends on:
#          "Order placed. Done."  ->  "Done."
#      because once "Order placed." is gone the bare-"Done." pattern no longer
#      sees a sentence terminator in front of it. The patterns are therefore
#      OR-ed into a single alternation and applied in ONE re.sub: every
#      lookbehind/anchor is evaluated against the ORIGINAL text, so removing one
#      phrase cannot hide another.
#   2. FIXED POINT. A single pass is still not enough in the other direction —
#      a deletion can EXPOSE a phrase that did not match before:
#          "I've addedI have added Paracetamol."
#      the \b in "i've added\b" is suppressed by the word character after it, and
#      only becomes a word boundary once the later phrase is removed. So the pass
#      is repeated until the string stops changing.
#
# Why both rather than either alone: (1) alone leaves the exposure case, (2)
# alone cannot recover the lookbehind context destroyed by an earlier deletion
# (nothing later in the string restores a period that was deleted). Together the
# result is guaranteed to contain NO match of ANY forbidden pattern — that is
# what "fixed point" buys, and tests/test_stream_sanitizer.py asserts it directly
# over every phrase and every adjacent PAIR of phrases.
#
# Termination: the only replacement is the empty string, so a pass that changes
# the text strictly shortens it — the loop is bounded by len(text) regardless.
# _STRIP_MAX_PASSES is the belt-and-braces bound (a future pattern with a
# non-shrinking replacement could otherwise spin). It costs one pass per
# EXPOSURE LAYER: normal prose settles in 2 (one to strip, one to observe no
# change — so this is ~2 regex scans where the old pipeline did 15), and only
# deliberately stacked confirmations ("I've added" x N with no separators) need
# N. 32 is far past anything the 15 tok/s appliance could emit inside one
# sentence while staying well short of unbounded work. If it is ever exhausted
# the LAST (most-stripped) value is returned — the safe direction — and the
# exhaustion is printed, because it means text is still being removed and the
# result may still hold a confirmation.


def _scoped(pattern: str) -> str:
    """`(?i)foo` -> `(?i:foo)`. Python rejects a GLOBAL inline flag that is not
    at the start of the expression, so per-pattern flags have to become scoped
    ones before the patterns can be OR-ed together. Patterns without a leading
    `(?i)` keep their own (case-sensitive) semantics."""
    if pattern.startswith("(?i)"):
        return f"(?i:{pattern[4:]})"
    return f"(?:{pattern})"


_FORBIDDEN_ALTERNATION = re.compile("|".join(_scoped(p) for p in FORBIDDEN_PHRASE_PATTERNS))

_STRIP_MAX_PASSES = 32

# Cosmetic tidy-up for the buffered lane only. Deliberately NOT r"\s{2,}" -> " ":
# that collapses "\n\n" too, so an answer with paragraphs, a numbered plan or a
# bullet list rendered as flat prose when it came through the buffered path and
# as proper markdown when it streamed — the same answer, two different layouts.
# Runs of SPACES/TABS collapse; blank lines are preserved (capped at one).
_SPACE_RUN = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_NEWLINE = re.compile(r"[ \t]+\n")
_BLANK_LINE_RUN = re.compile(r"\n{3,}")


def _tidy_whitespace(text: str) -> str:
    text = _SPACE_RUN.sub(" ", text)
    text = _SPACE_BEFORE_NEWLINE.sub("\n", text)
    text = _BLANK_LINE_RUN.sub("\n\n", text)
    return text.strip()


def _strip_forbidden_core(text: str, collapse_whitespace: bool) -> str:
    """Apply the forbidden-phrase alternation to a fixed point.

    `collapse_whitespace` is the ONLY difference between the two public strips
    (see _strip_forbidden vs _strip_forbidden_streaming), and it applies AFTER
    the fixed point, never inside it. That ordering is load-bearing: when the
    collapse ran inside the loop it fed the next pass's anchors, so the
    buffered lane re-exposed phrases the streaming lane never saw and the two
    lanes stripped DIFFERENT phrase sets — "Order placed.  Done." (two spaces)
    came out "" buffered but "  Done." streamed, and the streamed one is what
    the clinician reads and what the voice lane SPEAKS. The gap variants are
    handled by the anchors themselves now, so phrase removal is byte-identical
    in both lanes and the collapse is purely cosmetic.
    """
    if not text:
        return text
    for _ in range(_STRIP_MAX_PASSES):
        out = _FORBIDDEN_ALTERNATION.sub("", text)
        if out == text:  # fixed point: nothing still matches
            return _tidy_whitespace(text) if collapse_whitespace else text
        text = out
    # FAIL CLOSED. Reaching here means text was still shrinking after 32 passes,
    # i.e. the result may STILL contain a self-confirmation — and this function's
    # whole contract is that it does not. Returning the partially-stripped text
    # would ship exactly the hallucinated confirmation the guardrail exists to
    # stop, so the answer is dropped instead. Unreachable in practice (prose
    # settles in 2 passes; it takes 32 stacked separator-less confirmations from
    # a 15 tok/s model to get here), which is why dropping the answer is the
    # right trade rather than a denial-of-service risk.
    print(
        "  [Sage] forbidden-phrase strip did not settle within "
        f"{_STRIP_MAX_PASSES} passes; DROPPING the text (fail closed)"
    )
    return ""


def _strip_forbidden(text: str) -> str:
    """Buffered/whole-answer strip: also normalises the whitespace a deleted
    phrase leaves behind (the answer is emitted as one block, so the gap would
    otherwise be visible)."""
    return _strip_forbidden_core(text, collapse_whitespace=True)


def _strip_forbidden_streaming(text: str) -> str:
    """Forbidden-phrase strip for STREAMED segments: the SAME patterns and the
    same fixed point, but whitespace/markdown is preserved, because segments
    concatenate client-side and a trailing-newline strip would glue list items
    together. Whitespace policy is the only permitted difference between the
    lanes — used by the chat stream AND the voice lane."""
    return _strip_forbidden_core(text, collapse_whitespace=False)


# ── Incremental streaming sanitizer ──
#
# The old pipeline buffered the ENTIRE generation before emitting anything, so
# the browser's first byte arrived at total-generation time (~10-60s on the
# local lane) even though ttft_ms was hundreds of ms. These two classes stream
# the answer as it forms while keeping the no-action-hallucination guarantee:
#
#   * SentenceStreamSanitizer releases text at SENTENCE granularity, running
#     the forbidden-phrase strip over each completed sentence before release.
#     Every forbidden pattern is intra-sentence (they all terminate at/before a
#     period), so a phrase can never span two released segments — the streamed
#     text is exactly as sanitized as the buffered text was.
#   * XmlAnswerStreamer gates streaming on the response contract's tag ORDER:
#     <action_intent> arrives before <concise_answer>, so we can decide whether
#     this is a mutation turn BEFORE any answer text exists. Mutation turns
#     (deterministic gate OR model-declared intent) emit NOTHING incrementally —
#     they stay fully buffered behind the action-state override exactly as
#     before. Only intent=="None" turns stream, and even those pass every
#     sentence through the sanitizer. If the model never produces well-formed
#     tags, nothing streams and the buffered path handles it — fail-closed.

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])[ \t]+|\n")

# The unit the streaming sanitizer strips in. Only a PERIOD followed by
# whitespace qualifies — not "!", "?" or a bare "\n" (all of which
# _SENTENCE_BOUNDARY, the release granularity of the old implementation and of
# the fail-safe path, does accept). The reason is r"i have added\b[^.]*\.?":
# its `[^.]*` happily runs across "!", "?" and newlines and stops only at a
# period, so cutting anywhere else could leave the tail of a confirmation in a
# separate unit where the pattern no longer sees it.
_STRIP_UNIT = re.compile(r"(?<=\.)\s+")


# ── Forbidden-trigger derivation (used by SentenceStreamSanitizer) ──
#
# To release text EARLIER than the next sentence boundary the sanitizer needs to
# know, for every forbidden pattern, the leading LITERAL that any match of that
# pattern must begin with ("i have added", "order placed", "done", ...). Given
# those triggers it can hold back only the smallest suffix that could still
# BECOME a forbidden phrase, instead of the whole trailing sentence.
#
# The triggers are DERIVED FROM FORBIDDEN_PHRASE_PATTERNS — never hand-listed —
# so adding a pattern automatically extends the hold-back set. Derivation is
# deliberately strict: any regex construct the scanner does not understand
# raises _UndecidableTrigger, and the sanitizer then falls back to the old
# sentence-granularity behaviour (slower, never less safe).
# tests/test_stream_sanitizer.py asserts the shipped list derives cleanly, so
# that fallback can never be entered silently.


class _UndecidableTrigger(ValueError):
    """A forbidden pattern whose leading literal cannot be derived safely."""


# An alternative inside a group is zero-width if it is an anchor, a word-boundary
# assertion, or a lookaround — such a group contributes nothing to the literal
# and can be skipped (e.g. `(?:^|(?<=[.!?]\s))` before "done").
_ZERO_WIDTH_ALT = re.compile(r"^(?:\^|\$|\\b|\\B|\(\?<[=!].*\)|\(\?[=!].*\))$", re.DOTALL)
_ESCAPED_LITERAL = ".^$*+?()[]{}|\\/-'\""
_QUANTIFIERS = "*+?{"


def _scan_group(src: str, i: int):
    """src[i] == '(' → (whole group text incl. parens, index just after it)."""
    depth, j, in_class = 0, i, False
    while j < len(src):
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            j += 1
            continue
        if c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return src[i : j + 1], j + 1
        j += 1
    raise _UndecidableTrigger(f"unbalanced group in {src!r}")


def _top_level_alts(body: str) -> list:
    """Split a group body on top-level '|' (ignoring nested groups/classes)."""
    parts, cur, depth, in_class, i = [], [], 0, False, 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            cur.append(body[i : i + 2])
            i += 2
            continue
        if in_class:
            cur.append(c)
            if c == "]":
                in_class = False
            i += 1
            continue
        if c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def _tokenize_tail(rest: str) -> list:
    """Tokenize the pattern text that follows the literal trigger into
    (matches_empty, width_one) pairs. Raises on anything unrecognised."""
    toks, i, n = [], 0, len(rest)
    while i < n:
        c = rest[i]
        if c == "\\":
            if i + 1 >= n:
                raise _UndecidableTrigger(f"trailing backslash in tail {rest!r}")
            e = rest[i + 1]
            if e in "bBAZ":
                toks.append((True, False))  # zero-width assertion
                i += 2
                continue
            if e not in _ESCAPED_LITERAL and not e.isalpha():
                raise _UndecidableTrigger(f"unsupported escape \\{e} in {rest!r}")
            if e.isalpha() and e not in "bBAZ":
                raise _UndecidableTrigger(f"unsupported class \\{e} in {rest!r}")
            i += 2
            atom_width_one = True
        elif c == "[":
            j = rest.find("]", i + 2)  # +2: "[]" and "[^]" are literals
            if j == -1:
                raise _UndecidableTrigger(f"unterminated class in {rest!r}")
            i = j + 1
            atom_width_one = True
        elif c in "()|":
            raise _UndecidableTrigger(f"group/alternation in tail {rest!r}")
        elif c in _QUANTIFIERS:
            raise _UndecidableTrigger(f"dangling quantifier in {rest!r}")
        else:
            i += 1
            atom_width_one = True
        quant = rest[i] if i < n else ""
        if quant in "*?":
            toks.append((True, False))
            i += 1
        elif quant in "+{":
            raise _UndecidableTrigger(f"unsupported quantifier {quant!r} in {rest!r}")
        else:
            toks.append((False, atom_width_one))
    return toks


def _tail_is_incremental_safe(rest: str) -> bool:
    """Is the pattern text after the trigger safe for incremental matching?

    The hold logic needs this: every proper prefix of a match that is LONGER
    than the trigger must ITSELF be matched by the pattern, so that case (a)
    (a match reaching the end of the buffer) catches a phrase in progress.
    Two shapes qualify:
      * every token can match the empty string, so the tail is prefix-closed —
        r"\\b[^.]*\\.?" over "I have added Parac" still matches; or
      * the tail is exactly ONE mandatory single-character token, r"[.!]" after
        "done", where the only longer prefix IS the complete match.
    Anything else (r"order (?:placed|submitted)", r"chart [ud]pdated") would let
    the sanitizer release "order pla" before the phrase completed, so it is
    refused and the caller falls back to sentence granularity.
    """
    toks = _tokenize_tail(rest)
    if all(empty for empty, _w in toks):
        return True
    return len(toks) == 1 and toks[0] == (False, True)


def _literal_trigger(pattern: str) -> str:
    """The longest leading literal that EVERY match of `pattern` starts with,
    lower-cased. Zero-width constructs are skipped; the first construct that can
    vary the text (char class, quantifier, real alternation) ends the literal.
    Raises _UndecidableTrigger if what follows the literal is not safe to match
    incrementally (see _tail_is_incremental_safe).

    r"(?i)i have added\\b[^.]*\\.?"            -> "i have added"
    r"(?i)(?:^|(?<=[.!?]\\s))done[.!]"         -> "done"
    """
    src = pattern
    if src.startswith("(?i)"):
        src = src[4:]
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            if i + 1 >= n:
                raise _UndecidableTrigger(f"trailing backslash in {pattern!r}")
            e = src[i + 1]
            if e in "bBAZ":  # zero-width assertion
                i += 2
                continue
            if e in _ESCAPED_LITERAL:
                if i + 2 < n and src[i + 2] in _QUANTIFIERS:
                    break  # optional/repeated → literal ends
                out.append(e)
                i += 2
                continue
            raise _UndecidableTrigger(f"unsupported escape \\{e} in {pattern!r}")
        if c == "(":
            grp, after = _scan_group(src, i)
            body = grp[1:-1]
            if body[:2] in ("?=", "?!") or body[:3] in ("?<=", "?<!"):
                i = after  # lookaround → zero-width
                continue
            if body.startswith("?:"):
                body = body[2:]
            elif body.startswith("?"):
                raise _UndecidableTrigger(f"unsupported group {grp!r} in {pattern!r}")
            if all(_ZERO_WIDTH_ALT.match(a.strip()) for a in _top_level_alts(body)):
                i = after  # e.g. (?:^|(?<=[.!?]\s))
                continue
            break  # real alternation → literal ends
        if c in "^$":
            i += 1  # zero-width anchor
            continue
        if c in "[|)" or c in _QUANTIFIERS:
            break
        if i + 1 < n and src[i + 1] in _QUANTIFIERS:
            break  # this char is optional/repeated
        out.append(c)
        i += 1
    trigger = "".join(out).lower()
    if not trigger:
        raise _UndecidableTrigger(f"no literal trigger in {pattern!r}")
    if not _tail_is_incremental_safe(src[i:]):
        raise _UndecidableTrigger(
            f"tail {src[i:]!r} of {pattern!r} cannot be matched incrementally"
        )
    return trigger


# Cache: pattern tuple → (compiled patterns, set of trigger prefixes, max len).
# `None` prefixes means derivation failed → sanitizer uses the old sentence path.
_HOLD_TABLES: dict[
    tuple, tuple[tuple[re.Pattern, ...], frozenset[str] | None, int, tuple[str, ...]]
]
_HOLD_TABLES = {}


def _hold_table(patterns: tuple):
    cached = _HOLD_TABLES.get(patterns)
    if cached is not None:
        return cached
    compiled = tuple(re.compile(p) for p in patterns)
    try:
        triggers = [_literal_trigger(p) for p in patterns]
        # Every non-empty prefix of every trigger. A buffer tail that is in this
        # set could be the beginning of a forbidden phrase and must be held.
        prefixes = {t[:k] for t in triggers for k in range(1, len(t) + 1)}
        table: tuple[tuple[re.Pattern, ...], frozenset[str] | None, int, tuple[str, ...]] = (
            compiled,
            frozenset(prefixes),
            max(len(t) for t in triggers),
            tuple(triggers),
        )
    except _UndecidableTrigger as exc:
        # Fail SAFE, never open: without triggers the sanitizer cannot bound how
        # far back a phrase might start, so it reverts to holding the whole
        # trailing sentence. Slower, identical safety.
        print(
            f"  [Sage] forbidden-trigger derivation failed ({exc}); "
            f"sanitizer falls back to sentence granularity"
        )
        table = (compiled, None, 0, ())
    _HOLD_TABLES[patterns] = table
    return table


class SentenceStreamSanitizer:
    """Incrementally release forbidden-phrase-stripped text, holding back only
    the SMALLEST SUFFIX that could still become a forbidden phrase.

    The old version held the whole trailing incomplete sentence, which at the
    appliance's ~15 tok/s cost the clinician ~1.2 s before the first word. It had
    to, because several FORBIDDEN_PHRASE_PATTERNS are unbounded — r"i have
    added\\b[^.]*\\.?" only terminates at the next period, so releasing early
    could leak "I have added Paracetamol" before the strip could remove it.

    Given the buffer `B`, the release point is the EARLIEST index picked out by
    any of these four rules; everything before it is provably unable to take part
    in a forbidden phrase, whatever arrives next:

      (a) a pattern match whose end is the end of B — greedy, so it may still be
          growing. Held from the match start.
      (a2) a trigger LITERAL sitting in B's unterminated tail (nothing after it
          but text without a period). The pattern may not match there yet and may
          never match in isolation — the r"\\b" in "i've added\\b" is suppressed by
          the word character that follows in "I've addedI have added…" — and yet
          the strip still deletes it, because removing the LATER phrase exposes
          the word boundary the earlier one needed. Held from the trigger.
      (b) a suffix of B that is a prefix of some trigger ("…I have ad"). Held
          from where that suffix begins.
      (c) a complete match straddling the point chosen so far. The strip runs
          over the released prefix, so cutting a match in half would leave its
          first half unmatched and emit it verbatim.

    Why (a)+(b) are the core: if a forbidden phrase starts at index j and is not
    yet complete in B, then B[j:] is a proper prefix of the match — either
    shorter than the trigger, so (b) holds from j, or already containing the
    whole trigger, in which case the pattern matches from j to the end of B and
    (a) holds from j. (a2) and (c) close the two ways the strip's behaviour is
    non-local: it is a PIPELINE of substitutions, so one deletion can change how
    the text around it matches.

    Release runs the strip over the whole active buffer prefix (not the fresh
    segment alone) so anchors and lookbehinds see their real context, and then
    applies the SAME four rules to the strip's OUTPUT — a deletion can push two
    halves of a confirmation together ("done" + "medication was added" + "!"
    strips to "done!"), so the bytes on the wire get the same treatment as the
    bytes in the buffer.

    Text is stripped one sentence unit at a time (since the last ". "), which
    makes the released bytes independent of how the model chunked the stream and
    bounds per-chunk regex work to O(sentence) rather than O(answer). Only a
    PERIOD ends a unit: r"[^.]*" runs happily across "!", "?" and newlines.

    Public API is unchanged: feed(chunk) -> str, flush() -> str.
    """

    def __init__(self, strip_fn, patterns=None):
        self._strip = strip_fn
        pats = tuple(FORBIDDEN_PHRASE_PATTERNS if patterns is None else patterns)
        (self._compiled, self._prefixes, self._maxtrig, self._triggers) = _hold_table(pats)
        # Barrier cuts (see _settled_cut) rest on properties of THIS strip and
        # THIS pattern list, so a caller with its own strip keeps sentence units.
        self._barrier_cuts = (
            strip_fn is _strip_forbidden_streaming
            and patterns is None
            and self._prefixes is not None
        )
        if self._barrier_cuts:
            self._trigger_re = re.compile(
                "|".join(re.escape(t) for t in self._triggers), re.IGNORECASE
            )
            self._barriers = frozenset(
                c for c in _CUT_BARRIER_CANDIDATES if not any(c in t for t in self._triggers)
            )
        self._pinned = False  # see _is_pinned
        self._win = ""  # active raw text (never released un-stripped)
        self._rel = 0  # index in _win proven safe and already released
        self._emitted = 0  # chars of strip(_win[:_rel]) already handed out

    # ── public API ──
    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._win += chunk
        if self._pinned and "." not in chunk:
            # Rule (a2) holds the release at a trigger already behind the
            # release point, and only a period can move it, so the full scan
            # would release nothing and commit nothing. Skipping it keeps a
            # period-free answer after "done" linear instead of quadratic.
            return ""
        out = self._pump(self._safe_end(self._win))
        self._pinned = self._is_pinned()
        return out

    def flush(self) -> str:
        return self._pump(len(self._win), final=True)

    # ── internals ──
    def _pump(self, end: int, final: bool = False) -> str:
        """Release everything proven safe up to `end`, one sentence unit at a
        time, committing each completed unit as it goes. `final` (flush) drains
        whatever is still held: the text is complete, so nothing can grow."""
        out = []
        # A unit whose trailing whitespace run was still open last time may have
        # completed since, so try to commit before releasing anything more.
        end -= self._commit()
        while end > self._rel:
            stop = end
            m = self._unit_end(self._win, self._rel)
            if m is not None and m <= end:
                stop = m
            piece = self._release(stop)
            if piece:
                out.append(piece)
            end -= self._commit()
        if final:
            stripped = self._strip(self._win[: self._rel]) or ""
            if len(stripped) > self._emitted:
                out.append(stripped[self._emitted :])
                self._emitted = len(stripped)
        return "".join(out)

    @staticmethod
    def _unit_end(buf: str, start: int):
        """End of the first complete sentence unit at/after `start`, or None.

        The run of whitespace after the period must be TERMINATED (a non-space
        follows it) before the unit counts as complete: cutting inside a run
        would leave the next unit starting with a space, and the "Done." pattern
        anchors on the unit start."""
        m = _STRIP_UNIT.search(buf, start)
        if m is None or m.end() >= len(buf):
            return None
        return m.end()

    def _safe_end(self, buf: str) -> int:
        """The largest index of `buf` that can be handed on: no forbidden phrase
        can begin before it and finish after it, whatever arrives next."""
        n = len(buf)
        if n == 0:
            return 0
        if self._prefixes is None:
            # Fail-safe: trigger derivation failed → old sentence granularity.
            end = 0
            for m in _SENTENCE_BOUNDARY.finditer(buf):
                end = m.end()
            return end
        hold = n
        low = buf.lower()
        # (a) a match that runs to the end of the buffer may still be growing.
        for pat in self._compiled:
            last = None
            for m in pat.finditer(buf):
                last = m
            if last is not None and last.end() == n and last.start() < hold:
                hold = last.start()
        # (a2) a trigger LITERAL sits in the unterminated tail of the buffer.
        #      The pattern may not match there yet and may never match in
        #      isolation — r"i've added\b" is suppressed by the word character
        #      that follows it in "I've addedI have added…" — and yet the strip
        #      still removes it, because deleting the later phrase exposes the
        #      word boundary the earlier one needed. That cascade only runs
        #      inside one sentence (no pattern crosses a period), so a trigger
        #      with no period after it is not settled and must be held.
        settled = buf.rfind(".") + 1
        for t in self._triggers:
            k = low.find(t, settled)
            if k != -1 and k < hold:
                hold = k
        # (b) the tail may be a partial (or complete-but-unmatched) trigger.
        for k in range(max(0, n - self._maxtrig), min(hold, n)):
            if low[k:] in self._prefixes:
                hold = k
                break
        # (c) never cut a COMPLETE match in half. Releasing win[:hold] runs the
        #     strip over that prefix alone, so a match straddling `hold` would
        #     no longer match and its first half would be emitted verbatim —
        #     "Order has been placedorder placed" holds at the "do" of "...ed|o"
        #     by rule (b), which sits inside the first confirmation. Patterns can
        #     overlap each other, so iterate until the point stops moving.
        moved = True
        while moved:
            moved = False
            for pat in self._compiled:
                for m in pat.finditer(buf):
                    if m.start() < hold < m.end():
                        hold = m.start()
                        moved = True
        return hold

    def _release(self, end: int) -> str:
        if end <= self._rel:
            return ""
        self._rel = end
        stripped = self._strip(self._win[:end]) or ""
        # The strip is a PIPELINE of substitutions, so deleting one phrase can
        # push two halves of another together: "done" + "medication was added"
        # + "!" strips to "done!", a confirmation that exists in no single
        # position of the raw buffer. The same smallest-safe-suffix rule is
        # therefore applied to the strip's OUTPUT — the bytes that actually go
        # on the wire — and only that prefix is handed out.
        safe = self._safe_end(stripped)
        out = stripped[self._emitted : safe] if safe > self._emitted else ""
        # Never un-say text: if a strip_fn shortened an already-emitted prefix,
        # hold the counter rather than re-emitting from an earlier offset.
        self._emitted = max(self._emitted, safe)
        return out

    def _commit(self) -> int:
        """Drop released, completed sentence units from the buffer. Returns the
        number of characters dropped so the caller can rebase its indices."""
        cut = 0
        while True:
            nxt = self._unit_end(self._win, cut)
            if nxt is None or nxt > self._rel:
                break
            cut = nxt
        cut = max(cut, self._settled_cut())
        if not cut:
            return 0
        dropped = len(self._strip(self._win[:cut]) or "")
        if dropped > self._emitted:
            # Part of this unit's output is still held back (the release rules
            # are applied to the strip's output too). Committing now would
            # rebase the counter past text that was never emitted and drop it —
            # keep the unit until it has all gone out.
            return 0
        self._win = self._win[cut:]
        self._rel -= cut
        self._emitted = max(0, self._emitted - dropped)
        return cut

    def _settled_cut(self) -> int:
        r"""A committable index that no forbidden match can ever reach, or 0.

        WHY. Only ". " ends a sentence unit, so an answer with no periods (a
        bullet list, a table, one long comma-separated line) was ONE unit, and
        every feed re-ran every pattern over all of it: 21 s of event-loop CPU
        for a 6.6 kB period-free answer at 4-character chunks, against 0.5 s
        with periods.

        WHY IT IS EXACT. Let b be a barrier: a character that occurs in no
        trigger literal (newline, comma, digit; see _CUT_BARRIER_CANDIDATES),
        with no trigger occurrence anywhere before it. Every match begins with
        its trigger, a trigger cannot span b, so no match starts at or before
        b in ANY pass of the fixed point, and the text up to b is never
        touched; a deletion after b cannot join across it either. Committing
        _MAX_LOOKBEHIND - 1 characters short of b leaves every match at least
        _MAX_LOOKBEHIND characters into the remaining window, so each
        lookbehind reads the same characters it reads in the whole text and no
        `\A`/`^` alternative can fire. Stripping the rest alone therefore gives
        exactly the bytes stripping the whole text gives, pass for pass. (The
        first version cut straight after a newline and relied on `\A`/`\n`
        twins; a deletion could then pull "  Done." up to the start of the
        rest, where `(?<=\A\s\s)` fired with no twin in the whole text.)
        """
        if not self._barrier_cuts or self._rel <= 0:
            return 0
        win = self._win
        m = self._trigger_re.search(win)
        limit = m.start() if m else len(win)
        back = _MAX_LOOKBEHIND - 1
        # The cut (b - back) must not pass the release point.
        b = min(limit, self._rel + back + 1) - 1
        while b >= _MAX_LOOKBEHIND:
            if win[b] in self._barriers:
                return b - back
            b -= 1
        return 0

    def _is_pinned(self) -> bool:
        """Rule (a2) of _safe_end holds the release at a trigger after the last
        period. When that trigger is at or before the release point, nothing
        can be released until a period arrives (feed's short-circuit)."""
        if self._prefixes is None:
            return False
        win = self._win
        low = win.lower()
        if len(low) != len(win):  # case-folding moved the indices
            return False
        settled = win.rfind(".") + 1
        for t in self._triggers:
            k = low.find(t, settled)
            if k != -1 and k <= self._rel:
                return True
        return False


#: Candidate cut barriers for SentenceStreamSanitizer._settled_cut: characters
#: common in period-free answers. Any that occurs in a trigger literal is
#: dropped when the sanitizer is built, so a new pattern cannot make one unsafe.
_CUT_BARRIER_CANDIDATES = "\n,;|/-0123456789"
#: Widest lookbehind in FORBIDDEN_PHRASE_PATTERNS ("(?<=[.!?]\s\s)",
#: "(?<=\n-\s)"). tests/test_stream_sanitizer.py derives it from the patterns
#: and fails if a wider one is added without raising this.
_MAX_LOOKBEHIND = 3


# ─── The fast contract's marker, read the SAME way by both readers ───────────
#
# The contract says the marker comes FIRST, with nothing before it. Two pieces
# of code read that contract — the streaming gate below and the buffered parser
# `parse_fast_response` — and until 2026-08-04 they disagreed about what "first"
# meant. The parser used `str.find()`, which scans the WHOLE string:
#
#     text = "…the usual dose is 500 mg.\nNOACT|"     (marker arrives LAST)
#     idx  = text.find("NOACT|")                       -> 26, not -1
#     body = text[idx + 6:]                            -> ""
#
# so a trailing marker silently deleted the entire answer. The turn did not
# error; it rendered BLANK. (Blank, not "untagged fallback": on a patient turn
# the <details> tags DO parse, so the untagged-output rescue below never fires.)
# Replaying a 67-turn medgemma:4b transcript through the old parser, ELEVEN
# turns came back with answer == "".
#
# Both readers now go through `_leading_marker`, which is anchored to the start.
# A marker anywhere else is not a marker — which is exactly the rule the prompt
# states, and it makes a trailing marker a no-op instead of a data-destroying
# one.
_MARKER_NOACT = "NOACT|"
_MARKER_ACT = "ACT|"
# Leading whitespace and a stray ``` fence are tolerated because models emit
# them; a sentence before the marker is not.
_MARKER_LEAD_RE = re.compile(r"^\s*(?:```[a-zA-Z]*\s*)?(NOACT\||ACT\|)")
# Contract syntax must never reach a clinician, wherever the model put it. The
# trailing `[ \t]?` swallows the space the marker was separated by, so removing
# it does not leave a doubled gap mid-sentence.
_MARKER_ANYWHERE_RE = re.compile(r"(?:NOACT\||(?<![A-Z])ACT\|)[ \t]?")

# ── A raw action-intent payload printed as a line of the answer ──
# The contract puts `{"action_type": ...}` straight after `ACT|` at the head of
# the reply, where the parser lifts it out. The model also writes it as a line
# of its own, usually the last one, and nothing removed it there: a trailing
# '{"action_type":"answer","medication":null}' reached the wire unchanged at
# every chunk size and rendered in the chat bubble. Contract syntax is never
# clinical content, so a line that IS such an object (optionally behind a stray
# marker) is dropped, on the buffered lane (`_drop_action_json_lines`) and at
# the streamer's hold-back alike. Deliberately narrow: the whole line must
# parse as a JSON object carrying an "action_type" key, so prose that merely
# mentions braces or the word is untouched.
_ACTION_LINE_MARKER_RE = re.compile(r"^[ \t]*(?:NOACT\||ACT\|)[ \t]*")
#: A line (no newline in it yet) that could still turn into one of those.
_ACTION_LINE_OPEN_RE = re.compile(r"[ \t]*(?:(?:NOACT|ACT)\|[ \t]*)?(?:\{|$)")


def _is_action_json_line(line: str) -> bool:
    s = _ACTION_LINE_MARKER_RE.sub("", line).strip()
    if not (s.startswith("{") and s.endswith("}")) or "action_type" not in s:
        return False
    try:
        obj = json.loads(s)
    except ValueError:
        return False
    return isinstance(obj, dict) and "action_type" in obj


def _action_line_undecided(line: str) -> bool:
    """True while `line`, still arriving, could become a standalone action line.
    Costs at most one chunk on an ordinary line ("A" might be "ACT|")."""
    if _ACTION_LINE_OPEN_RE.match(line):
        return True
    head = line.lstrip(" \t")
    return any(m.startswith(head) for m in (_MARKER_ACT, _MARKER_NOACT))


def _drop_action_json_lines(text: str) -> str:
    """The buffered lane's half of the rule above."""
    if not text or "action_type" not in text:
        return text
    return "\n".join(ln for ln in text.split("\n") if not _is_action_json_line(ln))


# ── Output-contract scaffolding ──────────────────────────────────────────────
# The names of every section tag in the structured output contract. The 27B
# model reliably wraps them in <details>; the 4B fast lane frequently does NOT —
# it answers, then emits the section tags loose, with no <details> to mark where
# the clinician-visible answer ended. Cutting the answer only at "<details>"
# therefore left the whole contract printed in the chat bubble, placeholders
# included:
#
#   The classic ECG findings of hyperkalemia include peaked T waves …
#   <display_mode> concise <patient_facts_summary> None <local_policy_guidance>
#   None … <cited_sources> ["DOC-ID"]
#
# Observed in the browser 2026-08-08 with Thinking OFF, i.e. on the exact lane
# the demo uses as its speed showcase. `_CONTRACT_CUT_RE` finds the first place
# the scaffolding starts — either <details> or any bare section tag — so the
# visible answer can be cut there whatever the model did. `_CONTRACT_TAG_RE`
# scrubs any survivor (an unmatched close tag, a tag the model repeated
# mid-answer). Neither is a substitute for the parser: the sections are still
# harvested for the details panel, they are simply not printed as prose.
#
# The tag list, the emphasis run and the tag regex are DEFINED ONCE, in
# services.sage_service (CONTRACT_TAGS / CONTRACT_TAG_EMPHASIS /
# CONTRACT_TAG_RE), because the history replay scrubs the next prompt with the
# same regex. Two copies had drifted: the replay scrub never learned the
# emphasis and padding forms below.
# (imported at the top of this module as _CONTRACT_TAGS, _EMPHASIS and
# _SHARED_CONTRACT_TAG_RE).
#: `_EMPHASIS` is one markdown emphasis run: `*`, `**`, `***`, and the `_`
#: forms. The model writes the contract in markdown as readily as in tags, so
#: both the tag rule below and the bracket-less rule further down see through it.

# THE TAG RULE SEES THROUGH EMPHASIS AND PADDING TOO. `</?(?:tag)\s*>` covered
# "<caveats >" but not "< caveats>", and none of the markdown forms the model
# actually emits — "<**escalation**>", "<_escalation_>", "</__caveats__>" all
# walked past BOTH the cut and the scrub and printed into the chat bubble
# (measured 2026-08-11). This is the same widening already applied to the
# bracket-less rule, and it is safe for the same reason: the tag NAME must still
# match one of `_CONTRACT_TAGS` exactly, so nothing here can fire on a clinical
# "<". "K < 3.5" contains no contract tag name and never will.
_CONTRACT_CUT_RE = _SHARED_CONTRACT_TAG_RE
_CONTRACT_TAG_RE = _CONTRACT_CUT_RE

# The same failure one level worse: the 4B lane sometimes drops the angle
# brackets ENTIRELY and writes the detail contract as ordinary prose —
#
#   … Consider intubation and mechanical ventilation if the patient deteriorates.
#   Details: concise None None None The primary goals are bronchodilation and …
#
# — label, display_mode enum, three empty placeholders, no "<" anywhere. Every
# rule above keys on "<tag>", so nothing cut this and the clinician read
# "concise None None None" as clinical text (browser, 2026-08-08 06:20, Thinking
# OFF, "First-line management of an acute severe asthma attack in an adult?").
#
# THE PATTERN IS DELIBERATELY NARROW, because the two failure directions are not
# symmetric: leaking scaffolding is embarrassing, but cutting real clinical prose
# LOSES INFORMATION A CLINICIAN NEEDED. So only two shapes count, and both are
# things no clinical answer says:
#
#   (a) `display_mode` — the contract's own field name, never English; and
#   (b) a `Details:` label followed by the display_mode enum AND the next
#       field's `None` placeholder (or end of reply).
#
# "the referral details are in the handover note" is untouched by both, and
# there is a test that says so.
#
# The match must NOT swallow the whitespace in front of it. `_contract_cut`'s
# callers only honour a cut > 0 (a reply that is nothing but the contract must
# not be blanked), so a match that started at the leading "\n" of "…\n\nDetails:"
# would report 0 and leave the whole contract standing. Starting at the "D"
# reports 2, the caller cuts, and `.strip()` removes the newlines anyway.
#
# EMPHASIS IS PUNCTUATION, AND `\b` COULD NOT SAY SO. The model writes the same
# contract in markdown — "**display_mode**", "_display_mode_", "__Details:__" —
# and the first version of this pattern wrote `[*_]{0,2}\bdisplay_mode\b`, which
# reads as if it handled all of them. It did not: "_" is a WORD character, so
# `\b` never holds between "_" and "d", and every underscore-emphasised form
# walked past the cut untouched in BOTH lanes (measured 2026-08-11:
# "_display_mode_", "__display_mode__" and "<_display_mode_>" all leaked whole).
# The asterisk `Details:` form leaked too, for a plainer reason — there was no
# `\s*` between the closing "**" and "concise", so "**Details:** concise None"
# could not match either.
#
# The boundaries below therefore sit OUTSIDE the emphasis run, using `(?<!\w)` /
# `(?!\w)` instead of `\b`. The run becomes punctuation the label may wear, while
# the label itself still may not be part of a larger identifier — the widening
# does NOT start matching "final_display_mode", "display_mode_override" or
# "display_modes", and there are tests that say so.
#: Emphasis-aware word boundaries — see the note above on why `\b` cannot serve.
#: (`_EMPHASIS` itself is defined with the tag rule, which needs it first.)
_EMPH_OPEN = r"(?<!\w)" + _EMPHASIS
_EMPH_CLOSE = _EMPHASIS + r"(?!\w)"

#: `Details:` immediately in front of the contract's own opening tag. A THIRD
#: shape, from the 2026-09-07 fast-lane transcripts, that neither rule above
#: reached:
#:
#:     Hypoxia refers to a condition where there isn't enough oxygen ...
#:     Details:
#:     <display_mode> concise
#:     <patient_facts_summary> None
#:
#: The label is followed by a TAG, not by the display_mode enum, so the
#: `Details:` rule below could not fire; the tag rule cut at the "<" and left
#: "Details:" standing as the last line of the clinical answer. It had never
#: streamed before (this lane's gate never opened, so the whole reply went out
#: through the buffered path), but the clinician read it there just the same.
#: Anchored on a real contract tag name for the same reason every other rule
#: here is: a sentence that ends "...in the referral details:" cannot fire it.
_BARE_LABEL_BEFORE_TAG = (
    _EMPH_OPEN
    + r"details"
    + _EMPHASIS
    + r"\s*:"
    + r"(?=\s*</?\s*{}(?:{}))".format(_EMPHASIS, "|".join(_CONTRACT_TAGS))
)

_BARE_CONTRACT_CUT_RE = re.compile(
    _EMPH_OPEN
    + r"display_mode"
    + _EMPH_CLOSE
    + r"|"
    + _EMPH_OPEN
    + r"details"
    + _EMPHASIS
    + r"\s*:\s*"
    + _EMPHASIS
    + r"\s*"
    + r"(?:concise|detailed)"
    + _EMPH_CLOSE
    + r"(?=\s*(?:None\b|$))"
    + r"|"
    + _BARE_LABEL_BEFORE_TAG,
    re.IGNORECASE,
)


#: Punctuation a contract label can hide behind. `_BARE_CONTRACT_CUT_RE` matches
#: the LABEL, so a tag still arriving ("<display_mode", no ">" yet) and an
#: emphasised prose label ("**display_mode**", "__display_mode__") both report an
#: index INSIDE their own opening.
_OPENER_TAIL_RE = re.compile(r"[*_]{1,3}$")


def _widen_to_opener(text: str, start: int) -> int:
    """Move `start` back over the punctuation the contract opens with.

    THE OPENING BELONGS TO THE CONTRACT SIDE OF THE BOUNDARY. `_CONTRACT_CUT_RE`
    only matches a COMPLETE tag, so while one is still arriving on the stream the
    only pattern that fires is the bracket-less one — and it matches the label,
    one or three characters past the "<" or "**" that opened it. Leaving those
    characters on the clinician's side put them on the wire, where the ">" that
    arrives a moment later cannot take them back: the live answer ended on a
    dangling "<" that the buffered replay of the same reply did not have
    (chunk=1 and chunk=13 over the loose-tag shape, 2026-08-11).

    Applied to the CUT and to the streamer's HOLD alike, because the two must
    agree: a hold that releases "**" and a cut that then lands behind it is the
    same defect with an extra step. It can only ever move an existing boundary a
    few characters earlier, and only across characters that are themselves the
    opening of the thing being cut at — once the ">" lands, `_CONTRACT_CUT_RE`
    matches at that same bracket anyway. Making the half-arrived tag agree with
    the whole one is what parity between the two lanes means.
    """
    emphasis = _OPENER_TAIL_RE.search(text[:start])
    if emphasis:
        start = emphasis.start()
    head = text[:start]
    if head.endswith("</"):
        start -= 2
    elif head.endswith("<"):
        start -= 1
    return start


def _contract_cut(text: str, *, still_arriving: bool = False) -> int:
    """Index where output-contract scaffolding starts in `text`, or -1.

    Only counted from the SECOND character onward when the reply opens with a
    tag: a reply that is nothing but the contract has no prose to keep, and
    cutting at 0 would blank it — the buffered path must never render blank
    (see MarkerAnswerStreamer's fail-direction note).

    `still_arriving=True` is the STREAMER's view, where `text` is a buffer that
    can still grow, so a boundary that leans on the END OF THE TEXT is not yet
    knowable. Two ways that bit, both of which truncated clinical prose on the
    live lane while the buffered replay of the same reply kept it:

      * the word boundary — "The display_mode" is a cut, and one character later
        "The display_modes" is ordinary prose (answer truncated to "The");
      * the `$` in the `Details:` lookahead — it is satisfied by the buffer's
        end, so "Admit today. Details: detailed notes follow" cut after "Admit
        today." while the whole reply plainly never said `None`.

    Both are deferred by the same rule: if nothing but whitespace follows the
    match, the evidence for the boundary is the buffer's edge rather than the
    reply's, so wait. It costs one character of latency,
    `_contract_tag_prefix_start` holds the label meanwhile, and `finalize()`
    applies the undeferred cut to whatever it releases — a reply truncated
    mid-contract must not leak it. The COMPLETE-tag match is never deferred:
    "<display_mode>" is closed and cannot grow into a longer word.
    """
    cuts = []
    complete = _CONTRACT_CUT_RE.search(text)
    if complete:
        cuts.append(complete.start())
    bare = _BARE_CONTRACT_CUT_RE.search(text)
    if bare and not (still_arriving and not text[bare.end() :].strip()):
        cuts.append(_widen_to_opener(text, bare.start()))
    return min(cuts) if cuts else -1


#: Longest fragment that could still become a contract tag. Budgeted from the
#: parts `_CONTRACT_CUT_RE` actually tolerates rather than a magic number: the
#: "</" opener, an emphasis run, the longest tag name, the closing emphasis, and
#: the padding before ">". Sized for the unpadded tag it silently cut the window
#: off mid-"</ ***patient_facts_summary*** >", releasing the whole opening one
#: character before the cut could fire.
_MAX_CONTRACT_TAG_LEN = len("</") + 3 + max(len(t) for t in _CONTRACT_TAGS) + 3 + len("  >")

#: Padding that can sit between "<" (or "</") and the tag name: the emphasis the
#: model wraps tags in, and the whitespace `_CONTRACT_CUT_RE` now tolerates. The
#: hold-back strips it before testing the fragment against `_CONTRACT_TAGS`,
#: because a hold that cannot see "<**cited_sou" while the cut CAN see
#: "<**cited_sources>" is the round-one defect again: the cut arrives to find
#: the opening already streamed and `_emit_pos` past the point it needed to act.
_TAG_PADDING = "*_ \t\r\n"


#: Bracket-less labels that may OPEN the prose form of the contract. A trailing
#: one is held back until enough characters arrive to tell scaffolding from
#: English — without this the streamer emits "Details:" and can never take it
#: back, since `_emit_pos` has already passed it. Emphasis-aware for the same
#: reason `_BARE_CONTRACT_CUT_RE` is: a hold that cannot see "__Details__" holds
#: nothing, and the cut that fires a moment later lands behind `_emit_pos`.
_BARE_LABEL_RE = re.compile(_EMPH_OPEN + r"(?:details|display_mode)" + _EMPH_CLOSE, re.IGNORECASE)
#: How far past the label we wait before ruling it ordinary prose. Long enough
#: to cover "Details: concise None" (20 chars), short enough that a sentence
#: ending "...in the referral details." resumes streaming almost immediately.
_BARE_DECIDE_WINDOW = 24


def _contract_tag_prefix_start(body: str) -> int:
    """Index of a TRAILING partial contract tag in `body`, or -1.

    The streamer must not emit "<pat" and then "ient_facts_summary>" — but it
    also must not stall on a clinical "<". So only a fragment that is a genuine
    prefix of `</?tag>` is held back. The same rule is applied to the
    bracket-less prose form (`_BARE_CONTRACT_CUT_RE`): a trailing "Details" or
    "display_mode", or a partial word that could still become one, is held until
    it is decided. `MarkerAnswerStreamer.finalize()` releases anything still
    held, so ordinary prose is delayed but never lost.

    Every bracket-less candidate is widened over its opening punctuation by
    `_widen_to_opener`, because a hold that stops at the LABEL releases the
    "**" or "<" in front of it as ordinary prose — and the cut that fires one
    character later then lands behind `_emit_pos`, where it can no longer act.
    The hold and the cut have to draw the boundary in the same place.
    """
    candidates = []

    lt = body.rfind("<")
    if lt >= 0 and (len(body) - lt) <= _MAX_CONTRACT_TAG_LEN:
        # Strip what `_CONTRACT_CUT_RE` tolerates around the name, so "<", "</",
        # "<**", "< " and "</__" are all measured against the tag names rather
        # than failing the prefix test on their own punctuation. BOTH ends: the
        # closing half arrives before the ">" does, and testing "escalation*"
        # against the tag names fails, which let the hold jump forward past the
        # bracket and stream "<**escalation" a moment before the cut fired.
        frag = body[lt + 1 :].lstrip("/").strip(_TAG_PADDING)
        if frag == "":
            candidates.append(lt)  # bracket + padding only: could open anything
        elif any(t.startswith(frag.lower()) for t in _CONTRACT_TAGS):
            candidates.append(lt)

    # A whole label near the tail whose meaning is not yet decided.
    last = None
    for match in _BARE_LABEL_RE.finditer(body):
        last = match
    if last is not None and (len(body) - last.start()) <= _BARE_DECIDE_WINDOW:
        candidates.append(_widen_to_opener(body, last.start()))

    # A partly-arrived label at the very end ("Detai", "display_mo"). A LEADING
    # underscore run belongs to the opening, not to the label: "__displ" is
    # "display_mode" five characters in, wearing its emphasis. Underscores are
    # word characters, so `[A-Za-z_]+$` swallows them and the prefix test below
    # would compare "__displ" against "display_mode" and hold nothing.
    word = re.search(r"[A-Za-z_]+$", body)
    if word:
        frag = word.group(0).lower().lstrip("_")
        if frag and any(w.startswith(frag) and w != frag for w in ("details", "display_mode")):
            candidates.append(_widen_to_opener(body, word.start()))

    # Emphasis that has arrived but whose label has not ("**" before
    # "**display_mode**"). Released on the very next character if no label
    # follows, so ordinary bold prose is delayed by one chunk, never lost.
    emph = _OPENER_TAIL_RE.search(body)
    if emph:
        candidates.append(_widen_to_opener(body, emph.start()))

    return min(candidates) if candidates else -1


def _strip_contract_scaffolding(text: str) -> str:
    """Everything before the contract scaffolding, with stray tags scrubbed."""
    if not text:
        return text
    cut = _contract_cut(text)
    body = text[:cut] if cut > 0 else text
    body = _drop_action_json_lines(body)
    return _CONTRACT_TAG_RE.sub("", body).strip()


def _leading_marker(text: str) -> tuple[str | None, int]:
    """(marker, index just past it) when the contract marker OPENS `text`.

    Returns (None, 0) otherwise — including when a marker appears later in the
    string. See the note above: that is the whole point.
    """
    m = _MARKER_LEAD_RE.match(text)
    return (m.group(1), m.end()) if m else (None, 0)


def _marker_still_possible(buf: str) -> bool:
    """True while a partially-received `buf` could still GROW into a marker.

    Used only by the streamer, which sees the reply a few characters at a time
    and must decide when to stop waiting. Giving up early matters: every
    character spent waiting is a character the clinician is not reading yet.
    """
    s = buf.lstrip()
    if s.startswith("`"):
        m = re.match(r"^```[a-zA-Z]*\s*", s)
        if not m:
            return len(s) <= len("```python")  # a fence may still be arriving
        s = s[m.end() :]
    head = s[: len(_MARKER_NOACT)]
    return _MARKER_NOACT.startswith(head) or _MARKER_ACT.startswith(head)


class MarkerAnswerStreamer:
    """Gate the stream on the FAST contract's leading marker.

    The fast contract (services.sage_service.SAGE_FAST_*) puts the answer FIRST
    and the reasoning channels last, with a cheap 3-token marker carrying the
    mutation decision that `<action_intent>…</action_intent>` used to carry at
    ~25 tokens. Measured saving to the first readable character: 2,750 ms.

    The SAFETY CONTRACT is unchanged, term for term:
      * nothing is released until the marker is known (previously: until
        </action_intent> closed),
      * `ACT|…` locks the stream exactly as a model-declared action_intent did —
        the buffered action-state override rewrites the answer,
      * a deterministic-gate mutation is `mutation_locked` and never streams,
      * everything released still passes through the forbidden-phrase sanitizer,
      * FAIL-CLOSED: if no marker ever OPENS the reply, nothing streams and the
        buffered end-of-turn path handles the response.

    THE FAIL DIRECTIONS ARE DELIBERATELY OPPOSITE, and this is the reasoning
    (stated here because it was previously an accident):

      streamer, no marker -> FAIL CLOSED (stream nothing)
      parser,   no marker -> FAIL OPEN   (show the text, gated deterministically)

    They are not in conflict; they are the same rule applied to two different
    risks. Streaming is the one path where text reaches the clinician BEFORE any
    end-of-turn guardrail can act on it — the action-state override cannot
    rewrite an answer that has already been read — so an ungated stream is
    unrecoverable and must not happen. The buffered path has the opposite
    problem: by the time it runs, the whole reply exists, every guardrail
    (deterministic mutation lock, action-state override, forbidden-phrase strip,
    cited-source validation) has already had its say, and refusing to render
    would produce a BLANK answer — which is not a safe outcome, it is a silent
    one, and a clinician cannot audit a blank. So the buffered path renders,
    and the ACTION decision for a non-compliant turn is carried by
    `detect_mutation_intent`'s regex, which never depended on model compliance
    in the first place. That is what the deterministic half of the two-gate
    design is for.

    ONE ASSUMPTION UNDER ALL OF THAT, AND IT DOES NOT HOLD ON EVERY MODEL
    (2026-09-07). "Fail closed" assumes the marker is something the model would
    otherwise have given us, so withholding costs only the turns where it did
    not. Measured on Orb's own prompts (dev_tools/check_fast_model_contract.py,
    3 questions x 3 runs): medgemma:27b opens with the marker 9/9, at character
    0 every time; medgemma:4b, which is exactly what THINKING OFF routes to,
    0/9. On that lane the branch above is not a gate that occasionally closes,
    it is a gate that never opens, and the clinician gets the whole answer in
    one lump at the end of the turn. The toggle sold as speed was the only lane
    that never streamed.

    `assert_marker()` moves OWNERSHIP of the marker to Orb for such a model. It
    changes who decided, not what was decided: the mutation verdict still comes
    from `detect_mutation_intent`, deterministically, before the request leaves
    the building, and it still arrives here as `mutation_locked`. So the table
    above gains a third row rather than losing its first:

      streamer, no marker, model-asserted -> FAIL CLOSED (stream nothing)
      streamer, no marker, Orb-asserted   -> STREAM (Orb already decided)
      parser,   no marker                 -> FAIL OPEN (gated deterministically)

    What is given up is narrow and worth naming: an `ACT|` this model would have
    emitted LATE, after prose we have already streamed. It is given up on a lane
    that emits no marker at all in nine runs out of nine, and where the safety
    floor (services/thinking_mode.py) has already routed every patient, dosing
    and Orb-score question to the careful model. An `ACT|` the model emits at the
    HEAD still locks the stream, unchanged. `sage_service.server_asserts_marker`
    owns the policy and carries the rest of the measurements.

    `<details>` ends the visible answer; everything after it is reasoning for the
    detail panel and is withheld from the stream.
    """

    _NOACT = _MARKER_NOACT
    _ACT = _MARKER_ACT
    _DETAILS = "<details>"
    # Backstop on how long we may hold output waiting for a marker that is not
    # coming. `_marker_still_possible` normally gives up long before this.
    _MAX_MARKER_WAIT = 64

    def __init__(
        self,
        sanitizer: "SentenceStreamSanitizer",
        mutation_locked: bool,
        *,
        marker_asserted: bool = False,
    ):
        self._sanitizer = sanitizer
        self._buf = ""
        self._emit_pos = 0  # consumed offset within the answer body
        self._body_start: int | None = None  # index just past the marker
        self._state = "locked" if mutation_locked else "wait_marker"
        self._asserted = bool(marker_asserted)
        self.streamed_any = False

    def assert_marker(self) -> None:
        """Orb has decided this turn's marker; stop requiring the model to emit one.

        Call BEFORE the first `feed()`. `mutation_locked` still dominates: a turn
        the deterministic gate locked stays locked, because the only decision
        being delegated here is the one the model was measured not to make.
        `sage_service.server_asserts_marker` owns when this is called and why.
        """
        if self._state == "wait_marker":
            self._asserted = True

    def feed(self, chunk: str) -> str:
        if self._state == "locked":
            self._buf += chunk
            return ""
        self._buf += chunk

        if self._state == "wait_marker":
            marker, end = _leading_marker(self._buf)
            if marker == self._ACT:
                # Model declared a chart mutation — buffer everything.
                self._state = "locked"
                return ""
            if marker == self._NOACT:
                self._body_start = end
                self._state = "stream_answer"
            elif (
                not _marker_still_possible(self._buf)
                or len(self._buf.strip()) > self._MAX_MARKER_WAIT
            ):
                # The reply can no longer open with a marker. Which way that
                # fails is decided per turn, by WHO OWNS THE MARKER:
                #
                #   model-asserted (default) -> FAIL CLOSED. The model was going
                #     to tell us whether this turn mutates the chart and did not,
                #     so nothing ungated may go out; the buffered path takes it.
                #   Orb-asserted -> STREAM. Orb already decided the marker before
                #     the request left the building (the deterministic mutation
                #     gate, whose verdict is `mutation_locked` above), so there
                #     is nothing left to wait for. Waiting anyway is not caution,
                #     it is a lump delivered at the end of the turn.
                #
                # An ACT| the model DOES emit still locks, above: asserting the
                # marker removes the DEPENDENCE on the model's answer, not the
                # model's ability to give one.
                if not self._asserted:
                    self._state = "locked"
                    return ""
                self._body_start = 0
                self._state = "stream_answer"
            else:
                return ""  # a marker may still be arriving

        if self._state == "stream_answer":
            body = self._buf[self._body_start :]
            cut = body.find(self._DETAILS)
            if cut == -1:
                # The 4B lane often ends the answer with a bare section tag and
                # no <details> at all. Treat the first one as the end of the
                # visible answer, otherwise the entire output contract streams
                # into the chat bubble (observed 2026-08-08, Thinking OFF).
                cut = _contract_cut(body, still_arriving=True)
            if cut == -1:
                # Hold back a trailing fragment that could still GROW into a
                # contract tag, so markup never leaks mid-tag. Only a genuine
                # tag prefix is held: a clinical "<" ("K < 3.5", "SpO2 < 92%")
                # streams straight through instead of stalling the answer.
                safe_end = _contract_tag_prefix_start(body)
                if safe_end == -1:
                    safe_end = len(body)
                out = self._release(body, safe_end, final=False)
            else:
                out = self._release(body, cut, final=True)
                self._state = "done"
                out += self._sanitizer.flush()
            if out:
                self.streamed_any = True
            return out

        return ""

    def _release(self, body: str, end: int, *, final: bool) -> str:
        """Feed body[_emit_pos:end] to the sanitizer a line at a time, dropping
        a standalone action-intent JSON line (see `_is_action_json_line`).

        The last line, while it is still arriving, is held from its start if it
        could still become one; `final` means `end` is the end of the answer, so
        that line is decided now. A held line always starts at `_emit_pos`
        (nothing of it was released), so dropping it drops all of it.
        """
        pieces = []
        pos = self._emit_pos
        while pos < end:
            nl = body.find("\n", pos, end)
            line_end = end if nl == -1 else nl
            line = body[body.rfind("\n", 0, pos) + 1 : line_end]
            if nl == -1 and not final and _action_line_undecided(line):
                break
            nxt = end if nl == -1 else nl + 1
            if not _is_action_json_line(line):
                pieces.append(body[pos:nxt])
            pos = nxt
        self._emit_pos = max(self._emit_pos, pos)
        fresh = "".join(pieces)
        return self._sanitizer.feed(fresh) if fresh else ""

    def finalize(self) -> str:
        """Release the sanitizer's held tail when the answer opened but never
        reached <details> (truncation / client disconnect).

        The tail is cut at the contract first. `feed` defers a bare label that
        ends exactly at the buffer's end, because it cannot yet tell
        "display_mode" from "display_modes" — but at finalize the buffer IS the
        whole reply, so the boundary is knowable and a generation that stopped
        mid-contract must not have its scaffolding released here.
        """
        if self._state != "stream_answer":
            return ""
        body = self._buf[self._body_start :]
        cut = body.find(self._DETAILS)
        if cut == -1:
            cut = _contract_cut(body)
        end = len(body) if cut < 0 else cut
        out = self._release(body, end, final=True) + self._sanitizer.flush()
        if out:
            self.streamed_any = True
        return out


def parse_fast_response(raw: str) -> dict:
    """Parse a fast-contract response into the same shape the XML parser produced.

    Returns {answer, action_intent, marker, details:{tag: value}}. `action_intent`
    is the raw JSON string after ACT| (or "None."), so the downstream
    action-state machinery keeps working unchanged. `marker` is the marker that
    actually opened the reply, or None when the model ignored the contract —
    that field is how the marker's benefit gets COLLECTED rather than merely
    paid for in latency (see the compliance log at the end of this function).

    FAILS OPEN when no marker opens the reply: the clinician still sees the
    model's text. The streamer above fails CLOSED on the same input. Both
    directions are deliberate and the reasoning is in MarkerAnswerStreamer's
    docstring.
    """
    out: dict[str, Any]
    out = {"answer": "", "action_intent": "None.", "marker": None, "details": {}}
    if not raw:
        return out
    text = raw.strip()
    if text.startswith("```"):  # stray fence, be forgiving
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text).rstrip("`").strip()

    marker, end = _leading_marker(text)
    out["marker"] = marker
    if marker == _MARKER_ACT:
        rest = text[end:]
        # The intent is the first balanced {...}; the answer follows it.
        depth, close = 0, None
        for i, c in enumerate(rest):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    close = i + 1
                    break
        if close:
            out["action_intent"] = rest[:close].strip()
            body = rest[close:]
        else:
            body = rest
    elif marker == _MARKER_NOACT:
        body = text[end:]
    else:
        # NON-COMPLIANT TURN. The model wrote something that does not open with
        # a marker — most often it answered first and appended the marker at the
        # END, which is what used to blank the answer. Keep the text (see the
        # fail-open note above) and let the deterministic gate own the action
        # decision; `action_intent` stays "None." because a marker we did not
        # see in the position the contract specifies is not evidence of anything.
        body = text

    # Where the visible answer ends. `<details>` is the contract's own marker,
    # but the 4B lane routinely omits it and emits the section tags loose — so
    # fall back to the first section tag of any kind. Without this the whole
    # contract, placeholders and all, printed into the chat bubble.
    cut = body.find(MarkerAnswerStreamer._DETAILS)
    if cut == -1:
        cut = _contract_cut(body)
    if cut > 0:
        detail_blob = body[cut:]
        body = body[:cut]
        for tag in (
            "display_mode",
            "patient_facts_summary",
            "local_policy_guidance",
            "external_guidance",
            "clinical_assessment",
            "caveats",
            "escalation",
            "cited_sources",
        ):
            # Properly closed section first; then the loose `<tag> value` form
            # the small model emits, which ends at the next tag or end-of-text.
            #
            # Emphasis-aware, like the cut that decided where this blob starts.
            # It has to be: once the cut learned to recognise
            # "<**cited_sources**>", a harvester that still matched only the bare
            # form produced a CLEAN ANSWER WITH NO EVIDENCE — the scaffolding
            # correctly removed from the prose and the citation silently absent
            # from the details panel, which is the quietest way this file could
            # fail. Cutting a tag you cannot harvest is not a fix, it is a
            # better-looking loss.
            open_tag = rf"<\s*{_EMPHASIS}{tag}{_EMPHASIS}\s*>"
            close_tag = rf"</\s*{_EMPHASIS}{tag}{_EMPHASIS}\s*>"
            m = re.search(f"{open_tag}(.*?){close_tag}", detail_blob, re.DOTALL | re.IGNORECASE)
            if not m:
                m = re.search(f"{open_tag}(.*?)(?=<|$)", detail_blob, re.DOTALL | re.IGNORECASE)
            if m:
                out["details"][tag] = m.group(1).strip()

    # Contract syntax is never clinical content. A stray marker left anywhere in
    # the body — the trailing "NOACT|" above, or one the model repeated
    # mid-answer — is removed so it cannot render to a clinician as if it were
    # part of the answer.
    body = _drop_action_json_lines(body)
    body = _MARKER_ANYWHERE_RE.sub("", body)
    # Final scrub: a section tag the model repeated mid-answer, or an unmatched
    # close tag, must not render to a clinician as if it were clinical text.
    body = _CONTRACT_TAG_RE.sub("", body)
    out["answer"] = body.strip()
    if marker is None and out["answer"]:
        # Visible so marker non-compliance is a measurable RATE, not a silent
        # degradation — this is the signal that would have exposed the blanking
        # bug on day one. Deliberately content-free: the answer is clinical text
        # and may name a patient, and Orb does not put PHI in the process log.
        # `trailing` distinguishes the two failures worth telling apart — the
        # model answered then emitted a marker, versus it emitted none at all.
        trailing = bool(_MARKER_ANYWHERE_RE.search(text))
        print(
            f"  [Sage] fast contract: no LEADING marker "
            f"(trailing_marker={trailing}, chars={len(text)}) — "
            f"answer kept, deterministic gate only"
        )
    return out


class XmlAnswerStreamer:
    """Extract <concise_answer> text from a streaming XML-contract response,
    only once <action_intent> has closed as 'None'. feed() returns sanitized
    text safe to emit immediately ('' when there is nothing safe yet).

    Retained for the ORB_SAGE_XML_CONTRACT=1 rollback path and for any scope
    still on the XML contract."""

    _INTENT_CLOSE = re.compile(r"<action_intent>(.*?)</action_intent>", re.DOTALL)
    _ANSWER_OPEN = "<concise_answer>"
    _ANSWER_CLOSE = "</concise_answer>"

    def __init__(self, sanitizer: SentenceStreamSanitizer, mutation_locked: bool):
        self._sanitizer = sanitizer
        self._buf = ""
        self._answer_pos = 0  # consumed offset within the answer body
        self._state = "locked" if mutation_locked else "wait_intent"
        self.streamed_any = False

    def feed(self, chunk: str) -> str:
        if self._state == "locked":
            return ""
        self._buf += chunk

        if self._state == "wait_intent":
            m = self._INTENT_CLOSE.search(self._buf)
            if not m:
                return ""
            intent = m.group(1).strip().lower()
            if intent not in ("none", "none."):
                # Model declared a mutation — buffer everything; the
                # action-state override rewrites the answer downstream.
                self._state = "locked"
                return ""
            self._state = "stream_answer"

        if self._state == "stream_answer":
            open_idx = self._buf.find(self._ANSWER_OPEN)
            if open_idx == -1:
                return ""
            body_start = open_idx + len(self._ANSWER_OPEN)
            close_idx = self._buf.find(self._ANSWER_CLOSE, body_start)
            if close_idx == -1:
                # Hold back any tail that could be the start of the closing tag
                # (or any other tag) so markup never leaks into the stream.
                safe_end = len(self._buf)
                tail_lt = self._buf.rfind("<", body_start)
                if tail_lt >= body_start:
                    safe_end = tail_lt
                fresh = self._buf[body_start + self._answer_pos : safe_end]
                self._answer_pos = max(self._answer_pos, safe_end - body_start)
                out = self._sanitizer.feed(fresh) if fresh else ""
            else:
                fresh = self._buf[body_start + self._answer_pos : close_idx]
                self._answer_pos = close_idx - body_start
                self._state = "done"
                out = (self._sanitizer.feed(fresh) if fresh else "") + self._sanitizer.flush()
            if out:
                self.streamed_any = True
            return out

        return ""

    def finalize(self) -> str:
        """End-of-stream: release the sanitizer's held tail for an answer that
        opened but never closed (model truncation). The sanitizer's buffer never
        contains markup — text from the last '<' onward is held in _buf here —
        so this cannot leak partial tags."""
        if self._state != "stream_answer":
            return ""
        self._state = "done"
        out = self._sanitizer.flush()
        if out:
            self.streamed_any = True
        return out


# ── Per-answer timing, in the shape the client renders ──
# The TTFT/total footer (src/components/SageAnswerMeta.tsx) used to exist only
# for the answer streamed in front of the clinician: the turn was persisted
# without its timing, so reopening the conversation dropped the one number that
# makes Sage's slowness legible. Both save paths now store this dict and
# /history hands it back, so a reloaded turn renders through the same component
# as a live one.
#
# UNMEASURED IS None, NEVER 0. A zero renders as "0.0s", which is a lie about a
# turn nothing timed — the voice refusal path shipped literal
# {ttft_ms: 0, total_ms: 0, tokens: 0} for a turn the model never ran. The
# client already treats 0 as unmeasured; that defence exists because the server
# lied, and it should not have to. A genuine sub-millisecond duration rounds to
# 0 and is likewise reported as unmeasured, which is the honest reading of "too
# fast for this clock". Same rule for the token count, which is a measurement
# too: zero words is nothing worth stating.
def _measured(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _answer_timing(ttft_ms=None, total_ms=None, tokens=None) -> dict:
    return {
        "ttft_ms": _measured(ttft_ms),
        "total_ms": _measured(total_ms),
        "tokens": _measured(tokens),
    }


def _stored_answer_meta(sources_json):
    """Project the client-facing answer metadata out of a stored turn's blob.

    Returns None for a turn written before timing was persisted AND for a turn
    whose timing is entirely unmeasured: the footer's contract is that an
    unmeasured answer renders nothing, so the absence has to survive the trip
    rather than arriving as zeros for the client to defend against again.

    Only `timing`, `thinking_tier` and `patient_name` are read out. The rest of
    the blob is the audit record (the parsed response, the cited and retrieved
    document ids), and this feeds a browsing endpoint. None of it has ever been
    on this wire and adding timing does not put it there. `patient_name` is the
    name the same user was shown on the live turn (metadata.patient_name); the
    history sidebar labels the thread with it, and an old row without one
    labels nothing rather than calling a patient chat "General Medical".
    """
    if not sources_json:
        return None
    try:
        blob = json.loads(sources_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    meta: dict[str, Any] = {}
    stored = blob.get("timing")
    if isinstance(stored, dict):
        timing = _answer_timing(
            ttft_ms=stored.get("ttft_ms"),
            total_ms=stored.get("total_ms"),
            tokens=stored.get("tokens"),
        )
        if timing["ttft_ms"] is not None or timing["total_ms"] is not None:
            meta["timing"] = timing
            tier = blob.get("thinking_tier")
            if tier:
                # Same nesting the live metadata event uses
                # (`provider.thinking_tier`), so the panel reads one path for
                # a streamed turn and a reloaded one.
                meta["provider"] = {"thinking_tier": tier}
    name = blob.get("patient_name")
    if isinstance(name, str) and name.strip():
        meta["patient_name"] = name.strip()
    return meta or None


def _chart_patient_name(patient_context_dict) -> str | None:
    """The patient's name off an assembled chart's demographics line
    ("PATIENT: <name>, <age>y/<sex>, Bed ..."), or None. One definition for
    the live metadata and the stored turn."""
    if not isinstance(patient_context_dict, dict):
        return None
    demo = patient_context_dict.get("demographics")
    if not isinstance(demo, str) or not demo.strip():
        return None
    return demo.split(",")[0].replace("PATIENT: ", "").strip() or None


# ── Blocking DB helpers (module level, always called via run_db) ──
# These live out here rather than nested inside the handlers so that the rule is
# visible at a glance: every `with db_session()` in this file belongs to a plain
# sync function that only ever reaches the DB from a worker thread.


def _match_medication(med_name):
    with db_session() as db:
        # Case-insensitive partial match, over CONFIRMED drugs only (grn-6): a
        # draft waiting for its second pharmacist, or a retired drug, is never
        # drafted into a pharmacy request.
        match = db.execute(
            "SELECT id, name FROM helix_medication_master WHERE (name LIKE ? OR generic_name LIKE ?) AND identity_status = 'active'",
            (f"%{med_name}%", f"%{med_name}%"),
        ).fetchone()
        return match


def _save_turn(
    user_id: str, query: str, full_response: str, meta_json, conversation_id: str | None = None
):
    """Persist the user turn and (if any) the assistant turn. Both writes go in one
    executor hop because they are a pair and the caller is a streaming generator's
    finally block — the fewer suspension points in there, the fewer chances a
    cancellation lands between them.

    Both rows carry the same `conversation_id` so the pair can only ever be read
    back together as prompt context for the thread they were spoken in."""
    save_conversation(user_id, "user", query, conversation_id=conversation_id)
    if full_response:
        # Note: We save the parsed JSON metadata to support governance and auditing
        save_conversation(
            user_id, "assistant", full_response, sources=meta_json, conversation_id=conversation_id
        )


def _with_label(text: str | None, label: str | None) -> str | None:
    """Plain answer text with the ruling E label in front, once."""
    if not label or not text or text.startswith(label):
        return text
    return f"{label}\n\n{text}"


def _labelled(raw: str, label: str | None) -> str:
    """The saved answer with the ruling-E label in it, so a reopened
    conversation shows which patient it was about: inside <concise_answer> for
    the XML contract (the screen shows only that), after a NOACT| marker, or
    first. Never into an ACT| reply: its action JSON must follow the marker
    for parse_fast_response, and attempt 6 broke that on reload. The action
    card names its patient."""
    if not label or not raw or label in raw:
        return raw
    if "<concise_answer>" in raw:
        return raw.replace("<concise_answer>", f"<concise_answer>{label}\n\n", 1)
    marker, end = _leading_marker(raw.strip())
    if marker == _MARKER_ACT:
        return raw
    if marker == _MARKER_NOACT:
        lead = len(raw) - len(raw.lstrip())
        return raw[: lead + end] + f"{label}\n\n" + raw[lead + end :]
    return f"{label}\n\n{raw}"


def _save_voice_turn(user_id: str, query: str, full_response: str, meta_json=None):
    save_conversation(user_id, "user", f"[voice] {query}")
    if full_response:
        save_conversation(user_id, "assistant", f"[voice] {full_response}", sources=meta_json)


#: Every tag the live buffered path sweeps for before it decides a reply was
#: "untagged". `cited_sources` is deliberately absent — it is metadata, never the
#: answer, and the live check excludes it for the same reason.
_LIVE_ANSWER_TAGS = (
    "action_intent",
    "concise_answer",
    "display_mode",
    "patient_facts_summary",
    "local_policy_guidance",
    "external_guidance",
    "clinical_assessment",
    "caveats",
    "escalation",
)


def _display_answer(raw: str) -> str:
    """The clinician-visible answer inside a STORED raw model reply.

    THE LAW THIS ENFORCES: a phrase any lane strips, every lane must strip. The
    replay lane is a lane. `sage_conversations.content` holds the RAW model
    output — that is the provenance record and it stays raw — but /history was
    handing that blob to the chat bubble verbatim, so a REOPENED conversation
    rendered contract syntax as clinical text where the live stream had shown
    clean prose (observed 2026-08-11: "NOACT| National Early Warning Score…"
    followed by a trailing `<details> <cited_sources> ["SEP-001"] </details>`).

    This is NOT a new sanitizer. It is the live buffered path's own answer
    projection, in the same order, out of the same functions:

        1. XML-contract tag sweep            (sage_query_stream, ~line 2035)
        2. fast-contract parse               (`parse_fast_response`, ~line 2055)
        3. untagged-output fallback          (`_strip_contract_scaffolding`, ~2083)
        4. UNCONDITIONAL forbidden-phrase strip  (`_strip_forbidden`, ~line 2193)

    Both output contracts therefore render clean from one code path: a 27B-era
    turn is picked up by (1) as `<concise_answer>`, a fast-lane turn by (2) at
    its marker, and a checkpoint that honoured neither by (3).

    WHAT THIS DOES NOT REPLAY. On a mutation (`ACT|`) turn the live lane
    REWRITES the answer to the action-state `safe_user_message`; that message
    lives in the audit blob, not in the raw reply, and this endpoint has never
    projected the audit blob (see tests/test_sage_answer_timing.py). A reopened
    mutation turn therefore shows the model's own drafted text, stripped —
    never a fabricated confirmation, because step 4 runs on every path.

    Only ASSISTANT rows go through here. A clinician's own question is their
    text; running the guardrail over it would silently alter the record of what
    was asked, and the live lane does not sanitize the prompt either.
    """
    if not raw:
        return raw

    # (1) XML contract — the 27B-era `<concise_answer>` shape.
    answer = ""
    tagged = False
    for tag in _LIVE_ANSWER_TAGS:
        m = re.search(f"<{tag}>(.*?)</{tag}>", raw, re.DOTALL)
        if m and m.group(1).strip():
            tagged = True
            if tag == "concise_answer":
                answer = m.group(1).strip()

    # (2) Fast contract — answer first, marker leading, sections trailing.
    if not answer:
        fast = parse_fast_response(raw)
        answer = fast["answer"]
        tagged = tagged or bool(answer) or bool(fast["details"])

    # (3) Untagged output — a checkpoint that ignored the contract entirely.
    if not tagged:
        answer = _strip_contract_scaffolding(raw)
        if not answer:
            answer = re.sub(r"</?[a-zA-Z_]+>", "", raw).strip()
        # The live lane reaches this branch only for a reply that carried no
        # contract at all, so a marker here would be a leftover the parser in
        # (2) never got to see. Scrubbed with the parser's OWN regex rather than
        # a new one: contract syntax is never clinical content, on any lane.
        answer = _MARKER_ANYWHERE_RE.sub("", answer)

    # (4) The guardrail every lane shares.
    return _strip_forbidden(answer)


def _history_safe_answer(sources_json, previous: dict | None) -> str | None:
    """The answer the clinician was shown on a mutation turn, or None.

    Same two gates as prompt assembly (sage_service._history_turn_proposed_action),
    so /history and the next prompt agree on which turns were mutations. An
    explicit `safe_answer` in the blob is enough on its own; the legacy
    `concise_answer` reading is only the safe message on a mutation turn.
    """
    from services.sage_service import (
        _history_turn_proposed_action,
        _safe_answer_of,
        _stored_turn_blob,
        _verdict_of,
    )

    blob = _stored_turn_blob(sources_json)
    if blob is None:
        return None
    explicit = blob.get("safe_answer")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    verdict, resolved = _verdict_of(blob)
    turn = {"role": "assistant", "proposed_action": verdict, "proposed_action_resolved": resolved}
    if _history_turn_proposed_action(turn, previous):
        return _safe_answer_of(blob)
    return None


def _history_rows(user_id: str, limit: int = 200) -> list:
    """This user's recent turns ACROSS threads, WITH each answer's timing.

    Deliberately not sage_service.list_conversation_turns: that reader does not
    return the `sources` column, and the persisted timing lives in it. Reading
    the turns from one query and their metadata from another would be worse
    than this duplication — two snapshots means a turn written between them
    shifts the LIMIT window and lands one answer's timing under a different
    answer, which is a wrong number on a clinical surface rather than a missing
    one. Merging the two readers is a sage_service change (see followups).

    An ASSISTANT row is projected through `_display_answer` on the way out: the
    stored content is the raw model reply (contract markers, trailing sections
    and all) and this endpoint feeds a chat bubble. The row on disk is NOT
    rewritten — provenance reads its own store (services/sage_provenance_service
    keeps sage_provenance_turns/refs/blobs and never touches this table), so the
    raw output remains available for replay and audit exactly as written.

    A MUTATION turn shows the message the clinician read, not the stored
    narration: the action-state override replaced the model's "I'll add
    paracetamol" with Orb's safe message on screen, and a reopened thread used
    to bring the narration back (see `_history_safe_answer`).

    Called from a plain `def` route, so it is already on a worker thread.
    """
    with db_session() as db:
        rows = db.execute(
            """SELECT role, content, sources, conversation_id, timestamp
               FROM sage_conversations
               WHERE user_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        out = []
        previous_by_thread: dict[str, dict]
        previous_by_thread = {}
        for r in reversed(rows):
            content = r["content"]
            if r["role"] == "assistant":
                content = _history_safe_answer(
                    r["sources"], previous_by_thread.get(r["conversation_id"])
                ) or _display_answer(content)
            previous_by_thread[r["conversation_id"]] = {"role": r["role"], "content": r["content"]}
            msg = {
                "role": r["role"],
                "content": content,
                "conversation_id": r["conversation_id"],
                "timestamp": r["timestamp"],
            }
            meta = _stored_answer_meta(r["sources"])
            if meta:
                msg["metadata"] = meta
            out.append(msg)
        return out


def _describe_view(view_context: dict | None, patient_in_scope: bool) -> str:
    """Render the client's ActiveViewContext as one compact DATA line for the
    fenced clinical-context block. Only whitelisted, length-clamped fields are
    included — the view context is client-supplied and untrusted.

    `patient_in_scope` is required and has no default. The patient NAME on this
    line comes from the request body, so a client can assert any name it likes;
    echoing it into the prompt for a patient outside the clinician's department
    puts an out-of-ward patient's name in front of the model on a turn that
    retrieval deliberately gave no chart for. Nothing is read from the DB for
    it, so it is not a disclosure — but it is the one place a foreign name could
    still reach the prompt, and a reviewer auditing this fix should not have to
    work that out.
    """
    if not view_context:
        return ""
    parts = []
    tab = str(view_context.get("tab") or "")[:32]
    if tab:
        parts.append(f"clinician is on the '{tab}' screen")
    pname = str(view_context.get("patient_name") or "")[:64]
    pid = str(view_context.get("patient_id") or "")[:32]
    if (pname or pid) and patient_in_scope:
        parts.append(f"viewing patient {pname or pid}".strip())
    sel = view_context.get("selection")
    if isinstance(sel, dict):
        kind = str(sel.get("kind") or "")[:24]
        label = str(sel.get("label") or "")[:96]
        if kind and label:
            parts.append(f"open {kind}: {label}")
    if not parts:
        return ""
    return "ACTIVE VIEW (what is on the clinician's screen right now): " + "; ".join(parts) + "."


# ══════════════════════════════════════════════════════════════════════════════
#  SHADOW INSTRUMENT — the Sage lane's producers
# ══════════════════════════════════════════════════════════════════════════════
# Until this block existed, `sage.drafted`, `sage.refused`, `sage.failed`,
# `action.proposed` and `context.compiled` were declared in shadow_service's
# vocabulary and written by NOTHING. The visible consequence was that the
# scorecard's `time_saved` and `agreement` metrics could never leave
# `insufficient_data` — the pilot's Sage evidence did not merely read low, it had
# no source at all.
#
# WHY THE ROUTE AND NOT sage_service.query_ollama. query_ollama is also driven by
# the proactive sweep, the ward narrator and the handoff service. A row emitted
# there would count a machine-initiated generation as a clinician's answer, and
# `time_saved` is literally `timed_answers x (assumed manual baseline - measured
# p50)` — inflating the count inflates the headline hours. Only a turn a human
# actually asked for gets a row, so the figure most likely to be quoted to an
# investor is the conservative one.
#
# Three rules from shadow_service govern every call below:
#   1. never fail the clinical action  -> every helper swallows its own errors;
#   2. never on the event loop         -> `_sh_*_sync` bodies run under run_db /
#                                         to_thread, loop-side callers use the
#                                         a-forms;
#   3. bounded                         -> payloads are key-whitelisted upstream.
# None of these helpers is ever called while holding an open write transaction,
# so none needs the `db=` self-deadlock escape.

#: The Sage turn's lane. `chat` and `voice` are clinician lanes and DO count as
#: pilot evidence; `simulator` (which the scorecard excludes) is deliberately not
#: used here, because a Sage turn is always a real person typing or speaking.
SHADOW_CHAT_LANE = "chat"
SHADOW_VOICE_LANE = "voice"


def _sh_open_episode_sync(conn, patient_id, lane: str, trigger: str):
    """Resolve-or-mint the shadow episode for one Sage turn, on `conn`.

    A Sage turn about a patient JOINS that patient's open episode — the same
    episode Vigil opened when NEWS2 crossed, if one is still inside the window.
    That is what puts the alert, the draft, the proposal and the clinician's
    response on ONE timeline, which is the whole basis of the agreement metric.

    A patient-less turn ("what is the sepsis bundle?") is still a real answer
    that took real time, so it gets its own single-turn episode rather than being
    dropped. It carries no alert, so it can never reach the false-alert or
    lead-time denominators (both require an `alert.fired` in the episode).
    """
    from services import shadow_service

    if patient_id:
        episode_id, _ = shadow_service.open_episode(patient_id, lane, trigger=trigger, db=conn)
        if episode_id:
            return episode_id
    episode_id = shadow_service.new_episode_id()
    shadow_service.emit_many(
        [shadow_service.build("episode.opened", episode_id, lane, payload={"trigger": trigger})],
        db=conn,
    )
    return episode_id


def _sh_context_compiled_sync(
    patient_id, lane: str, facts_n: int, guidelines_n: int, compile_ms: int, trigger: str
):
    """Open/resolve the episode AND write `context.compiled` in ONE transaction.

    SYNC — called from `_prepare_context`, which is already on a to_thread worker.
    One transaction rather than two because this sits on the clinician's
    time-to-first-token path: retrieval has just finished and generation has not
    started, so every extra WAL lock acquisition here is latency the clinician
    pays. Returns the episode id, or None if the instrument is unavailable.
    """
    try:
        from services import shadow_service

        with db_session() as conn:
            episode_id = _sh_open_episode_sync(conn, patient_id, lane, "sage_query")
            shadow_service.emit_many(
                [
                    shadow_service.build(
                        "context.compiled",
                        episode_id,
                        lane,
                        patient_id=patient_id,
                        payload={
                            "facts_n": facts_n,
                            "guidelines_n": guidelines_n,
                            "compile_ms": compile_ms,
                            "trigger": trigger,
                        },
                    )
                ],
                db=conn,
            )
            return episode_id
    except Exception:
        # Rule 1. A broken instrument must never cost the clinician an answer.
        return None


def _sh_episode_only_sync(patient_id, lane: str):
    try:
        with db_session() as conn:
            return _sh_open_episode_sync(conn, patient_id, lane, "sage_query")
    except Exception:
        return None


async def _sh_episode(patient_id, lane: str):
    """Loop-safe episode resolution — one run_db hop, never a session on the loop."""
    try:
        return await run_db(_sh_episode_only_sync, patient_id, lane)
    except Exception:
        return None


async def _sh_emit(kind: str, episode_id, lane: str, **kwargs):
    if not episode_id:
        return None
    try:
        from services import shadow_service

        return await shadow_service.aemit(kind, episode_id, lane, **kwargs)
    except Exception:
        return None


async def _sh_turn_failed(
    patient_id, lane: str, failure: str, user: dict | None = None, episode_id=None
):
    """`sage.failed` — the turn that did not produce an answer.

    Without this row the record contains only the turns that worked, and a
    reviewer counting Sage answers would be counting Sage successes.
    """
    try:
        episode_id = episode_id or await _sh_episode(patient_id, lane)
        await _sh_emit(
            "sage.failed",
            episode_id,
            lane,
            patient_id=patient_id,
            actor_user_id=(user or {}).get("id"),
            actor_role=(user or {}).get("role"),
            payload={"failure": failure},
        )
    except Exception:
        pass


async def _sh_refused(patient_id, lane: str, refusal_class: str, stage: str, user: dict):
    """`sage.refused` — the deterministic pre-LLM safety gate fired.

    A refusal is a SUCCESS of the guardrail and a NON-ANSWER of the assistant. It
    must never land in `sage.drafted` (that would add a fast, cheap turn to the
    time-saved denominator and flatter both the count and the latency p50).
    """
    try:
        episode_id = await _sh_episode(patient_id, lane)
        await _sh_emit(
            "sage.refused",
            episode_id,
            lane,
            patient_id=patient_id,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            payload={"refusal_class": refusal_class, "stage": stage},
        )
    except Exception:
        pass


def _sh_refusal_class(refusal_text: str) -> str:
    return "prompt_injection" if refusal_text == _INJECT_REFUSAL else "harm_intent"


def _sh_facts_n(patient_context_dict) -> int:
    """How many patient fact CHANNELS were actually populated (vitals, meds,
    labs, notes, imaging) — read off the completeness map the context assembler
    already maintains. Counting characters or context lines would make the number
    grow with prompt formatting rather than with evidence."""
    if not patient_context_dict:
        return 0
    completeness = patient_context_dict.get("completeness") or {}
    return sum(1 for key, value in completeness.items() if key.startswith("has_") and value is True)


# ══════════════════════════════════════════════════════════════════════════════
#  REPLAYABLE PROVENANCE — the record that makes an incident review possible
# ══════════════════════════════════════════════════════════════════════════════
# The shadow log above answers "how is the pilot going?" in aggregate. It cannot
# answer "why did Sage say THAT, to THAT clinician, about THAT patient, on the
# 14th?" — it stores counts and ids, not the inputs. This does: the question, the
# resolved scope, the chart AS IT WAS, the retrieved passages, the model and the
# thinking-mode decision, the answer and its citations. See
# services/sage_provenance_service.py for the storage design (content-addressed
# dedup + compression + retention) and the cost arithmetic.
#
# THREE RULES, in the order they matter:
#   1. It never costs the clinician an answer. Every call site swallows.
#   2. It never runs on the event loop. Always through run_db().
#   3. It never runs before the answer is delivered. Every call site sits AFTER
#      the terminal SSE event, so the capture cannot be on the critical path even
#      when the store is slow.
async def _prov_record(**kwargs):
    """Capture one Sage turn. Fire-safe: returns None rather than raising."""
    try:
        from services import sage_provenance_service as _prov

        # Bound late, by attribute, so a test (or an ops kill-switch) that
        # replaces `record_turn` on the module is honoured — and so this stays a
        # SYNC callable handed to run_db, which refuses coroutine functions.
        return await run_db(lambda: _prov.record_turn(**kwargs))
    except Exception as e:  # pragma: no cover — defence in depth
        print(f"  [Sage/Provenance] capture failed: {e}")
        return None


def _prov_no_model_route_info() -> dict:
    """`route_info` for a turn where the model never ran.

    A pre-LLM refusal and a retrieval failure select no provider, but the
    clinician's toggle position is still part of what happened and a reviewer
    will ask about it. Read HERE, in the request's own context — the ContextVar
    that holds the flag belongs to the request task, and a streaming generator is
    not guaranteed to be iterated inside it.
    """
    try:
        return {"thinking_requested": thinking_mode.current_thinking_flag()}
    except Exception:  # pragma: no cover — defence in depth
        return {}


def _voice_model_name() -> str | None:
    """The MLX checkpoint serving the voice lane, or None. Never raises — this is
    only ever called to decorate a provenance row, and a config shape change must
    not be able to turn a spoken answer into an error event."""
    try:
        from config import VOICE as _V

        return str(_V["conversational"]["model_path"]).rsplit("/", 1)[-1]
    except Exception:
        return None


_SAGE_QUERY_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.post("/query", response_model=None)
async def sage_query(
    request: Request, user: dict = _SAGE_QUERY_USER_DEFAULT
) -> StreamingResponse | dict[str, str]:
    """Query Sage with streaming response."""
    body = await request.json()
    query = body.get("query", "")
    patient_id = body.get("patient_id")
    # ── Page/view awareness ──
    # The clinician app mounts one Sage on every screen; the client publishes a
    # small view_context (ActiveViewContext) describing what is on screen
    # ({tab, patient_id, patient_name, selection}). We use it two ways:
    # (1) as a pronoun-resolution fallback patient when the request carries no
    #     explicit patient_id, so "summarise this chart" works on any patient
    #     screen; and (2) as a compact, data-only hint of the current screen
    #     (_describe_view) folded into the fenced DATA block. It is never
    #     treated as an instruction.
    view_context = body.get("view_context") if isinstance(body.get("view_context"), dict) else None

    # ── Conversation thread ──
    # The id of the Sage thread this turn belongs to. "New Chat" mints a fresh
    # one client-side, and prompt history is scoped to it (see
    # get_conversation_history). Absent/blank ⇒ no prior context, which is the
    # safe direction: the previous user-scoped behaviour meant a general
    # question asked on a clean screen could be answered about the patient
    # discussed in an earlier, dismissed conversation. Clamped and sanitised
    # because it is client-supplied and reaches a SQL parameter and nothing else.
    _cid = body.get("conversation_id")
    conversation_id = (
        "".join(c for c in str(_cid) if c.isalnum() or c in "-_")[:64]
        if isinstance(_cid, str) and _cid.strip()
        else None
    ) or None

    if not query:
        return {"error": "Query is required"}

    # The on-screen patient is the pronoun-resolution fallback: "what are her
    # latest labs?" scopes to the open chart even when no patient was named.
    # Resolved BEFORE the safety gate (it is pure dict access, no DB) so a
    # refusal can be threaded onto the patient's episode too — a refused turn on
    # a deteriorating patient is exactly the row a reviewer will want.
    _vc = view_context or {}
    effective_patient_id = patient_id or _vc.get("patient_id") or _vc.get("patientId") or None

    # THINKING MODE, bound for the whole turn. An absent/invalid flag resolves to
    # the configured default (ON = the careful model), so an old client or a
    # malformed body can only ever get the SAFER model, never the faster one.
    # Deliberately not applied to /sage/voice: a spoken turn has no visible
    # toggle, so it takes the default until voice grows a control of its own.
    # No reset token kept: the ContextVar lives in this request's asyncio task
    # and dies with it, so one clinician's fast-mode choice cannot bleed into
    # the next request. `end_turn` exists for tests that share a context.
    thinking_mode.begin_turn(body.get("thinking"))

    #: Links every shadow row this turn writes (`context.compiled`,
    #: `sage.drafted`, `action.proposed`) without exposing the user or the query.
    #: It is ALSO the provenance turn id, so a scorecard row and its replayable
    #: reconstruction join on one value.
    shadow_turn_id = uuid.uuid4().hex[:16]

    #: Thinking-mode flag captured in the REQUEST's context (see
    #: _prov_no_model_route_info) for the exits where no provider is ever chosen.
    _no_model_route_info = _prov_no_model_route_info()

    # ── Pre-LLM safety gate (harm + injection) ──
    refusal = _safety_refusal(query)
    if refusal:
        _refusal_class = _sh_refusal_class(refusal)

        async def _refuse():
            yield f"data: {json.dumps({'content': refusal})}\n\n"
            yield f"data: {json.dumps({'metadata': {'action_state': None, 'sources': [], 'confidence': 'high', 'escalation': True, 'display_mode': 'concise', 'query_scope': 'SAFETY_REFUSAL', 'patient_context_used': False, 'patient_id': None, 'patient_name': None}})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            # SHADOW, after the clinician already has the refusal: the guardrail's
            # own record. Emitted last so the instrument can never delay a refusal.
            await _sh_refused(
                effective_patient_id, SHADOW_CHAT_LANE, _refusal_class, "pre_llm", user
            )
            # PROVENANCE. "Why did Sage refuse me?" is one of the two questions a
            # clinician actually escalates, and a store that only kept answers
            # could not answer it. The refused QUESTION is the whole evidence.
            await _prov_record(
                turn_id=shadow_turn_id,
                lane=SHADOW_CHAT_LANE,
                outcome="refused",
                user=user,
                question=query,
                patient_id=effective_patient_id,
                query_scope="SAFETY_REFUSAL",
                refusal_class=_refusal_class,
                answer=refusal,
                route_info=_no_model_route_info,
                confidence="high",
                escalation=True,
            )

        return StreamingResponse(_refuse(), media_type="text/event-stream")

    # ── Deterministic Intent Gate (shared with the voice lane) ──
    is_deterministic_mutation = _mutation_gate(query)

    # Get conversation history BEFORE saving this message.
    # Off the loop — a SELECT over sage_conversations that ran inline on every turn.
    # It only feeds the prompt and is never written back, so the new suspension
    # point here cannot invalidate anything; the history/save pair was already
    # separated by the entire LLM stream.
    history = await run_db(
        get_conversation_history, user["id"], limit=6, conversation_id=conversation_id
    )
    # The which-patient exchange a reply may settle, read further back than the
    # prompt window (bed locator attempt 13b): a re-ask chain must not lose the
    # message that started it.
    from services.sage_service import _last_turn_asked_which_patient

    question_history = (
        await run_db(
            get_conversation_history, user["id"], limit=20, conversation_id=conversation_id
        )
        if _last_turn_asked_which_patient(history)
        else None
    )

    # ── Retrieval, deferred into the stream (2026-07 UX/latency pass) ──
    # Previously get_relevant_context() ran HERE, synchronously in the async
    # handler: it blocked the event loop for the whole embed+rerank pass
    # (10–30s on this hardware) and the client saw zero bytes until it was
    # done. It now runs inside the stream generator via asyncio.to_thread,
    # bracketed by SSE `stage` events so the UI can show honest progress
    # ("Searching guidelines…" → "Generating…").
    t_start = time.perf_counter()

    def _prepare_context() -> tuple[Any, ...]:
        # `user` is passed FIRST-CLASS, not derived from anything in the body.
        # get_relevant_context requires it: retrieval scopes every patient query
        # to permissions.department_scope(user), and `effective_patient_id` —
        # which comes from the request/view_context — is vetted against that
        # same department rather than trusted.
        # Passed only when there is an exchange to read, so every other turn
        # makes the same call as before.
        _qh = {"question_history": question_history} if question_history is not None else {}
        _context, _scope, _sources, _confidence, _escalation, _pcd, _qscope = get_relevant_context(
            query, user, effective_patient_id, history, **_qh
        )
        # A mutation turn must NEVER ride a prose lane: broad/aggregate scopes
        # use the prose prompt (no <action_intent> contract), so the
        # action-state override could not run and a drafted-order confirmation
        # would only be phrase-stripped. Force the XML contract.
        if is_deterministic_mutation and _scope in ("broad", "aggregate"):
            _scope = "simple"
        # NOTE (2026-07-31, deliberately NOT done): a "quick" prose lane for
        # non-mutation simple turns was prototyped to skip the ~25-token
        # <action_intent> preamble (~1.8 s of the clinician's wait). It was
        # REVERTED. The preamble is the model-declared-mutation gate: it is what
        # catches a chart mutation the deterministic regex missed, and prose mode
        # also streams raw XML tags on a model that emits them anyway (medgemma
        # opens with a ```xml fence unprompted). Both properties are covered by
        # tests/test_sage_streaming.py and tests/test_adversarial.py. Latency is
        # bought elsewhere — see the LLM queue's reserved interactive lane and
        # the lean 3-section contract for simple scope.
        # Surface the live view to the model as a compact context line (inside
        # the fenced data block, so it is data, not instructions).
        #
        # The on-screen patient is named on that line only if the AUTHENTICATED
        # user's department covers them. One indexed lookup, on this worker
        # thread (never the loop), and only when the client actually sent a
        # patient.
        _view_patient_in_scope = True
        if effective_patient_id:
            from permissions import department_scope as _dept_scope
            from permissions import facility_scope as _facility_scope
            from services.sage_service import _patient_in_scope

            with db_session() as _db:
                # With its facility half (multisite-10).
                _view_patient_in_scope = _patient_in_scope(
                    _db,
                    effective_patient_id,
                    _dept_scope(user),
                    facilities=_facility_scope(user),
                )
        # Not on a no-such-patient turn (R026): the clinician asked about a
        # bed or name nobody here holds, no chart was attached, and the open
        # patient's name on this line would be the one patient the model
        # could drift to.
        from services.sage_service import NO_MATCHING_PATIENT_NOTICE

        view_line = _describe_view(
            view_context,
            _view_patient_in_scope and NO_MATCHING_PATIENT_NOTICE not in (_context or ""),
        )
        # NEVER add the view line to an OUT_OF_SCOPE notice. For every other
        # scope `_context` is prompt material the model reads and the clinician
        # never sees, so describing their screen in it is exactly right. The
        # out-of-scope path is the one case where `_context` IS the answer — it
        # is streamed verbatim, model never invoked (see the short-circuit
        # below) — so putting it in front of that text showed an internal debug
        # line at the top of what the clinician reads: "ACTIVE VIEW (what is on
        # the clinician's screen right now): clinician is on the 'sage'
        # screen." Observed in the browser 2026-08-03.
        #
        # IT GOES LAST, NOT FIRST (2026-08-04). Ollama's KV reuse is
        # PREFIX-anchored: the shared prefix ends at the first byte that
        # differs. This line is the most volatile thing in the whole prompt —
        # it changes with the screen, and with `selection` it can change
        # BETWEEN TURNS ON THE SAME PATIENT — and prepending it put that
        # volatility ahead of the chart block, so the stable half of the prompt
        # could never be a shared prefix. Measured consequence upstream: a chart
        # block warmed by the chart-open prewarm shared only the system prompt
        # with the real turn, and the patient facts were re-prefilled from
        # scratch every time. Stable first, volatile last — the same rule the
        # A → B → C channel order already follows.
        # Nor to the which-patient question (R026): it is streamed verbatim
        # too, and attempt 4 showed the clinician this line inside it.
        if view_line and _qscope not in ("OUT_OF_SCOPE", "DISAMBIGUATE", "NOT_CARRIED"):
            _context = f"{_context}\n{view_line}" if _context else view_line
        # ── SHADOW: `context.compiled`, and the episode this whole turn hangs on ──
        # Done HERE because this body is already on a to_thread worker (rule 2:
        # never a db_session on the event loop) and because the episode must
        # exist before generation starts — the action-state override later in the
        # stream needs somewhere to put `action.proposed`. One transaction, at
        # the one point in the turn where the clinician is not waiting on a token.
        _episode = _sh_context_compiled_sync(
            effective_patient_id,
            SHADOW_CHAT_LANE,
            facts_n=_sh_facts_n(_pcd),
            guidelines_n=len(_sources or []),
            compile_ms=round((time.perf_counter() - t_start) * 1000),
            trigger=_qscope,
        )
        return _context, _scope, _sources, _confidence, _escalation, _pcd, _qscope, _episode

    # SAFETY (finding "Sage streams hallucinated 'order placed' confirmations"):
    # raw model output must NEVER reach the client, because the model can assert
    # "I've added X, order placed" in the raw <concise_answer> before the
    # end-of-stream policy override lands. Historically that meant buffering the
    # ENTIRE generation (first client byte ≈ total generation time). The
    # incremental sanitizer (SentenceStreamSanitizer + XmlAnswerStreamer above)
    # restores streaming WITHOUT weakening the guarantee: only sentence-complete,
    # forbidden-phrase-stripped answer text is released, and mutation turns
    # (deterministic gate OR model-declared <action_intent>) emit nothing until
    # the buffered action-state override has rewritten the answer. This flag now
    # means "stream SANITIZED content incrementally" — raw chunks still never
    # leave the server.
    stream_raw_to_client = True

    # (The streamed-segment strip is the module-level _strip_forbidden_streaming
    #  — same patterns, same fixed point, whitespace preserved.)

    # The governance classification is LOCAL-ONLY (regex + local DB match) and
    # decides which lane the router may use; it also lands in the per-inference
    # audit record. The view context participates: an on-screen patient makes
    # the turn PHI even when the text itself names nobody.
    # It is also a DB read (it scans the patient roster for name/bed matches), so
    # it goes to a worker thread like every other read on this path.
    from services.ai_governance import classify_query_sensitivity

    classification = await run_db(
        classify_query_sensitivity, query, effective_patient_id, view_context
    )

    async def stream_response():
        # Raised below when a reply settles the which-patient question of an
        # order (attempt 13b).
        nonlocal is_deterministic_mutation
        full_response = ""
        cancelled = False
        t_first_token = None
        t_first_emit = None
        route_info: dict[str, Any]
        route_info = {}
        emitted_stream = False
        # The patient this turn read, for the audit row and provenance. Starts
        # as the request's patient; retrieval may attach a chart the request
        # never named (see below).
        turn_patient_id = effective_patient_id
        # ── Shadow state for this turn ──
        # Deliberately NOT the same names the finally block probes with locals()
        # below: those probes mean "was this ever bound?", and pre-initialising
        # them would silently change what the audit record stores.
        sh_episode = None  # episode this turn's rows hang on
        sh_failure = None  # set => sage.failed instead of sage.drafted
        sh_action = None  # payload for action.proposed
        sh_retrieval_ms = None
        broad_sanitizer = SentenceStreamSanitizer(_strip_forbidden_streaming)
        # The fast (answer-first) contract is the default; ORB_SAGE_XML_CONTRACT=1
        # rolls the whole app back to the XML contract, so the streamer that
        # enforces the gate has to follow the same switch.
        from services.sage_service import _xml_contract_forced

        _streamer_cls = XmlAnswerStreamer if _xml_contract_forced() else MarkerAnswerStreamer
        xml_streamer = _streamer_cls(
            SentenceStreamSanitizer(_strip_forbidden_streaming),
            mutation_locked=is_deterministic_mutation,
        )

        # ── Stage 1: retrieval (threaded — keeps the event loop free) ──
        import asyncio as _aio

        yield f"data: {json.dumps({'stage': 'retrieving'})}\n\n"
        try:
            (
                context,
                scope,
                sources,
                confidence,
                escalation,
                patient_context_dict,
                query_scope,
                sh_episode,
            ) = await _aio.to_thread(_prepare_context)
        except Exception as e:
            print(f"  [Sage] Retrieval error: {e}")
            # SHADOW: this `return` leaves the generator WITHOUT passing through
            # the finally below, so the failed-turn row has to be written here or
            # a retrieval outage would be invisible in the record — the log would
            # contain only the turns that survived retrieval.
            await _sh_turn_failed(effective_patient_id, SHADOW_CHAT_LANE, "retrieval", user)
            yield f"data: {json.dumps({'error': 'Guideline retrieval failed. Please retry.'})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            # PROVENANCE: same reasoning as the shadow row above — this `return`
            # skips the finally, so without its own capture a retrieval outage
            # would leave the store describing only the turns that survived.
            await _prov_record(
                turn_id=shadow_turn_id,
                lane=SHADOW_CHAT_LANE,
                outcome="failed",
                user=user,
                question=query,
                patient_id=effective_patient_id,
                query_scope="RETRIEVAL_FAILED",
                refusal_class=None,
                answer="",
                route_info=_no_model_route_info,
                timings={"retrieval_ms": round((time.perf_counter() - t_start) * 1000)},
            )
            return
        t_retrieval = (time.perf_counter() - t_start) * 1000  # ms
        sh_retrieval_ms = round(t_retrieval)

        # The chart retrieval actually attached: found by name, or by sticky
        # anchoring, when the request carried no patient_id at all. Recording
        # the request's (blank) patient left a PHI read that could not be found
        # by patient in provenance or the inference audit, nor in a DPDP export.
        # Retrieval only attaches an in-scope chart, so this names nobody the
        # caller could not already see.
        if isinstance(patient_context_dict, dict) and patient_context_dict.get("patient_id"):
            turn_patient_id = patient_context_dict["patient_id"]

        # ── A reply settled WHO; the earlier message says WHAT (attempt 13b) ──
        # "give her lasix 40 iv", the which-patient question, then "yes": the
        # gate above read only "yes", so a model's "the order is placed" reached
        # the screen with nothing drafted (review 13, S1). Every message of the
        # exchange goes through the gate, and the model and the grounding
        # check are given the clinician's words instead of "yes".
        model_query = query
        if isinstance(patient_context_dict, dict) and patient_context_dict.get("question_what"):
            model_query = patient_context_dict["question_what"]
            if not is_deterministic_mutation and any(
                _mutation_gate(t) for t in patient_context_dict.get("question_exchange") or ()
            ):
                is_deterministic_mutation = True
                xml_streamer = _streamer_cls(
                    SentenceStreamSanitizer(_strip_forbidden_streaming),
                    mutation_locked=True,
                )

        # ── The classification must describe what the model was actually GIVEN ──
        # classify_query_sensitivity runs before retrieval and reads the words
        # plus the request (patient_id / view_context). Sticky anchoring can
        # attach a chart that neither of those announced: "what is driving
        # that?" typed in the Sage tab, on a thread already bound to a patient,
        # carries no pronoun, no name and no patient_id — it would be labelled
        # `general_knowledge` while the prompt in front of the model holds that
        # patient's vitals, labs and medications. Nothing egresses either way
        # (every Sage turn is served locally, PHI or not), but the per-inference
        # audit record would understate the turn, and the router's contract is
        # that a PHI-free label means PHI-free CONTENT. Upgraded here, where the
        # chart is known — no DB work, no re-classification, just the truth.
        if patient_context_dict is not None and not classification.contains_phi:
            classification.contains_phi = True
            classification.query_class = "patient_scoped"
            classification.reasons.append("retrieval attached a chart (sticky patient anchoring)")
        # The same truth for patient ROWS with no chart (2026-10-04): DISAMBIGUATE
        # hands the model each matching patient's name, bed and ward, and BROAD a
        # census row per patient, both with patient_context_dict None. Probed: a
        # misspelt name that fuzzily matched two patients ("thiruvengadem") put
        # both names in the prompt under `general_knowledge`, and the thinking
        # floor's context markers do not see a disambiguation list either.
        _rows_class = {
            "DISAMBIGUATE": "patient_scoped",
            "NOT_CARRIED": "patient_scoped",
            "BROAD": "census_broad",
        }.get(query_scope)
        if _rows_class and not classification.contains_phi:
            classification.contains_phi = True
            classification.query_class = _rows_class
            classification.reasons.append(
                f"retrieval put patient rows in the prompt ({query_scope})"
            )

        # ── Cross-department reach: answered DETERMINISTICALLY, no model ──
        # Retrieval refused to assemble a chart outside this clinician's
        # department and already wrote the fail-closed security audit row. The
        # answer is emitted verbatim from OUT_OF_SCOPE_NOTICE and the LLM is
        # never invoked: a PHI boundary must not be enforced by asking a 4B
        # model to please decline. Nothing patient-derived is in scope here to
        # leak — `context` IS the notice.
        if query_scope == "OUT_OF_SCOPE":
            yield f"data: {json.dumps({'content': context})}\n\n"
            yield f"data: {json.dumps({'metadata': {'action_state': None, 'sources': [], 'confidence': 'high', 'escalation': False, 'display_mode': 'concise', 'query_scope': 'OUT_OF_SCOPE', 'patient_context_used': False, 'patient_id': None, 'patient_name': None, 'timing': {'retrieval_ms': round(t_retrieval)}}})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            # A scope denial is a guardrail SUCCESS and a Sage NON-ANSWER — same
            # accounting as the pre-LLM safety refusal, so it must not land in
            # `sage.drafted` and flatter the time-saved denominator.
            await _sh_refused(None, SHADOW_CHAT_LANE, "out_of_scope_patient", "retrieval", user)
            # PROVENANCE, with `patient_id=None` deliberately. Retrieval refused
            # to confirm this patient exists; writing their id into a store an
            # auditor can list would re-disclose exactly what the 404-shaped
            # refusal above was protecting. The QUESTION is kept — that is the
            # evidence of the attempted cross-department reach — and the security
            # audit row that names the patient was already written, fail-closed,
            # by retrieval itself.
            await _prov_record(
                turn_id=shadow_turn_id,
                lane=SHADOW_CHAT_LANE,
                outcome="refused",
                user=user,
                question=query,
                patient_id=None,
                query_scope="OUT_OF_SCOPE",
                refusal_class="out_of_scope_patient",
                answer=context,
                route_info=route_info or _no_model_route_info,
                confidence="high",
                timings={"retrieval_ms": round(t_retrieval)},
            )
            return

        # ── Which patient? Asked DETERMINISTICALLY, no model (ruling A, R026) ──
        # Retrieval wrote the question (sage_service._which_patient_question):
        # the candidates' beds and names from this clinician's own department.
        # It used to be a bare list handed to the model under the general
        # prompt with nothing telling it to ask, and in a replay the model
        # answered generically with invented citations instead. Same shape and
        # accounting as the out-of-scope answer above: a non-answer, so never
        # `sage.drafted`, and provenance names no patient.
        # Also a request a reply did not carry over (bed locator attempt 13d):
        # the server's notice, the same no-model accounting.
        if query_scope in ("DISAMBIGUATE", "NOT_CARRIED"):
            _no_model_class = "which_patient" if query_scope == "DISAMBIGUATE" else "not_carried"
            yield f"data: {json.dumps({'content': context})}\n\n"
            yield f"data: {json.dumps({'metadata': {'action_state': None, 'sources': [], 'confidence': 'high', 'escalation': False, 'display_mode': 'concise', 'query_scope': query_scope, 'patient_context_used': False, 'patient_id': None, 'patient_name': None, 'timing': {'retrieval_ms': round(t_retrieval)}}})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            await _sh_refused(None, SHADOW_CHAT_LANE, _no_model_class, "retrieval", user)
            await _prov_record(
                turn_id=shadow_turn_id,
                lane=SHADOW_CHAT_LANE,
                outcome="refused",
                user=user,
                question=query,
                patient_id=None,
                query_scope=query_scope,
                refusal_class=_no_model_class,
                answer=context,
                route_info=route_info or _no_model_route_info,
                confidence="high",
                timings={"retrieval_ms": round(t_retrieval)},
            )
            # SAVED, unlike the out-of-scope answer: the next turn must know it
            # followed this question, or "yes" goes to the open chart. This
            # branch returns before the try/finally that saves every other
            # turn, which is how attempt 4 lost it in production.
            await run_db(
                _save_turn, user["id"], query, context, None, conversation_id=conversation_id
            )
            return

        yield f"data: {json.dumps({'stage': 'generating', 'retrieval_ms': round(t_retrieval)})}\n\n"

        # Ruling E: an answer from a chart the clinician did not name in this
        # turn (the open chart, or the conversation's patient) starts with that
        # patient's name and bed, written here by the server, so a reference
        # to another patient that retrieval missed is visible, not silent.
        _answer_label = (
            patient_context_dict.get("answer_label")
            if isinstance(patient_context_dict, dict)
            else None
        )
        if _answer_label:
            yield f"data: {json.dumps({'content': _answer_label + chr(10) + chr(10)})}\n\n"

        # Never in front of the model: a request Sage answered with the
        # which-patient question, and the question (attempt 13c). A settled
        # request arrives as `model_query`; an unsettled order cannot be
        # narrated by a model that never saw it.
        from services.sage_service import history_for_model

        try:
            async for chunk in query_ollama(
                model_query,
                context,
                history_for_model(history),
                scope,
                classification=classification,
                user_id=user["id"],
                patient_id=turn_patient_id,
                route_info=route_info,
            ):
                if await request.is_disconnected():
                    print("  [Sage] Client disconnected, aborting LLM stream")
                    cancelled = True
                    break
                if t_first_token is None:
                    t_first_token = (time.perf_counter() - t_start) * 1000
                    # WHO OWNS THE GATE MARKER is a property of the MODEL, and
                    # the model is not chosen until query_ollama runs. It
                    # stamps the answer into route_info before it yields
                    # anything, so the first chunk is the earliest moment this
                    # is knowable and still the last moment it can be acted on:
                    # nothing has been fed to the streamer yet.
                    # (False for every prose scope and for the XML contract, so
                    # this can only ever reach MarkerAnswerStreamer.)
                    if route_info.get("marker_asserted"):
                        assert isinstance(xml_streamer, MarkerAnswerStreamer)
                        xml_streamer.assert_marker()
                full_response += chunk
                # ── INCREMENTAL SANITIZED STREAMING ──
                # Broad scope: plain prose → sentence-sanitized release.
                # XML scopes: answer text released only after <action_intent>
                # closed as "None"; mutation turns stay fully buffered.
                if stream_raw_to_client:
                    safe_piece = (
                        broad_sanitizer.feed(chunk)
                        if scope in ("broad", "aggregate", "quick")
                        else xml_streamer.feed(chunk)
                    )
                    if safe_piece:
                        if t_first_emit is None:
                            t_first_emit = (time.perf_counter() - t_start) * 1000
                        emitted_stream = True
                        yield f"data: {json.dumps({'content': safe_piece})}\n\n"

            if not cancelled:
                t_total = (time.perf_counter() - t_start) * 1000

                # ── Fast path for prose scopes (broad + aggregate + quick; no XML) ──
                if scope in ("broad", "aggregate", "quick"):
                    if emitted_stream:
                        # Streamed incrementally — emit only the sanitized tail.
                        tail = broad_sanitizer.flush()
                        if tail:
                            yield f"data: {json.dumps({'content': tail})}\n\n"
                    else:
                        # Nothing streamed (sanitizer never saw a boundary) —
                        # emit the whole stripped answer as one event, as before.
                        safe_broad = _strip_forbidden(full_response)
                        yield f"data: {json.dumps({'content': safe_broad})}\n\n"
                    # Quick scope DOES retrieve guidelines, so its citations must
                    # ride out with the answer (the census/aggregate scopes have
                    # no guideline channel — they stay empty by construction).
                    #
                    # And its confidence is scored on the SAME evidence rules as
                    # the XML lane below. It was previously emitted raw from
                    # retrieval here and formatting-downgraded there, which is
                    # one of the ways the same question with the same documents
                    # produced two different labels across eval arms.
                    if scope == "quick":
                        _q_answer = _strip_forbidden(full_response)
                        _q_grounding = assess_answer_grounding(
                            answer=_q_answer,
                            sources=sources or [],
                            query=model_query,
                            prompt_context=context,
                            patient_context=None,
                        )
                        _q_conf, _q_basis = classify_answer_confidence(
                            retrieval_confidence=confidence,
                            source_count=len(sources) if sources else 0,
                            grounded=_q_grounding["grounded"],
                            query_scope=query_scope,
                            degraded=not _q_answer.strip(),
                        )
                        _q_attributed = list(_q_grounding["attributed"])
                        _q_citation_basis = (
                            "system_attributed"
                            if _q_attributed
                            else "retrieved"
                            if sources
                            else "none"
                        )
                    else:
                        _q_conf, _q_basis = "high", "deterministic_scope"
                        _q_attributed, _q_citation_basis = [], "none"
                    metadata = {
                        "action_state": None,
                        "sources": sources if scope == "quick" else [],
                        "confidence": _q_conf,
                        "confidence_basis": _q_basis,
                        "citation_basis": _q_citation_basis,
                        "attributed_count": len(_q_attributed),
                        "attributed_sources": _q_attributed,
                        "escalation": bool(escalation) if scope == "quick" else False,
                        "display_mode": "concise",
                        "query_scope": query_scope,
                        "patient_context_used": False,
                        "patient_id": None,
                        "patient_name": None,
                        "patient_source": None,
                        "data_completeness": None,
                        "local_policy_found": False,
                        "provider": route_info or None,
                        "timing": {
                            "retrieval_ms": round(t_retrieval),
                            "ttft_ms": round(t_first_token) if t_first_token else None,
                            "first_content_ms": round(t_first_emit) if t_first_emit else None,
                            "total_ms": round(t_total),
                        },
                    }
                    yield f"data: {json.dumps({'metadata': metadata})}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    return

                # End-of-stream for XML scopes: release the sanitizer's held
                # tail if the answer opened but never closed (truncation).
                xml_tail = xml_streamer.finalize()
                if xml_tail:
                    emitted_stream = True
                    yield f"data: {json.dumps({'content': xml_tail})}\n\n"

                # --- PARSE THE RESPONSE AND VALIDATE EVIDENCE ---
                parsed_json = {}
                tags = [
                    "action_intent",
                    "concise_answer",
                    "display_mode",
                    "patient_facts_summary",
                    "local_policy_guidance",
                    "external_guidance",
                    "clinical_assessment",
                    "caveats",
                    "escalation",
                    "cited_sources",
                ]
                for tag in tags:
                    match = re.search(f"<{tag}>(.*?)</{tag}>", full_response, re.DOTALL)
                    if match:
                        val = match.group(1).strip()
                        if tag == "cited_sources":
                            try:
                                parsed_json[tag] = json.loads(val)
                            except json.JSONDecodeError:
                                parsed_json[tag] = []
                        else:
                            parsed_json[tag] = val
                    else:
                        parsed_json[tag] = [] if tag == "cited_sources" else ""

                # --- FAST-CONTRACT PARSE ---
                # The answer-first contract carries the answer BEFORE the tags,
                # so the XML sweep above finds no <concise_answer>. Map it onto
                # the same parsed_json shape so every downstream guardrail
                # (action-state override, cited-source validation, forbidden
                # phrase strip) keeps working untouched.
                if not parsed_json.get("concise_answer"):
                    _fast = parse_fast_response(full_response)
                    if _fast["answer"]:
                        parsed_json["concise_answer"] = _fast["answer"]
                        parsed_json["action_intent"] = _fast["action_intent"]
                        for _t, _v in _fast["details"].items():
                            if _t == "cited_sources":
                                try:
                                    parsed_json[_t] = json.loads(_v)
                                except json.JSONDecodeError:
                                    parsed_json[_t] = []
                            else:
                                parsed_json[_t] = _v

                # --- UNTAGGED-OUTPUT FALLBACK ---
                # Some checkpoints (e.g. the official Ollama-library medgemma builds)
                # answer well but ignore the XML output contract entirely; every tag
                # then parses empty and Sage rendered a BLANK reply while the real
                # markdown answer sat unused in full_response. If NO tag matched at
                # all, treat the whole response (minus stray tag fragments) as the
                # concise answer so the clinician always sees the model's text.
                # Downstream guardrails — forbidden-phrase strip, action-state
                # override, cited-source validation — still apply to it.
                if not any(parsed_json.get(t) for t in tags if t != "cited_sources"):
                    # `_strip_contract_scaffolding` rather than a bare tag strip:
                    # deleting only the angle brackets left the contract's own
                    # VALUES in the answer ("display_mode concise
                    # patient_facts_summary None … cited_sources ["DOC-ID"]"),
                    # which is what the clinician actually read on the fast lane.
                    untagged = _strip_contract_scaffolding(full_response)
                    if not untagged:
                        # Contract-only reply with no prose before it: never
                        # render blank — fall back to the old bracket strip.
                        untagged = re.sub(r"</?[a-zA-Z_]+>", "", full_response).strip()
                    if untagged:
                        parsed_json["concise_answer"] = untagged
                        parsed_json["display_mode"] = "detailed"

                # --- ACTION-STATE POLICY OVERRIDE ---
                raw_action_intent = parsed_json.get("action_intent", "").strip()
                # `declares_action` is sage_service's — the history assembly
                # path reads the SAME stored action_intent to decide whether a
                # turn may be replayed under a synthesized `NOACT|`, and the two
                # readings must agree by construction rather than by two copies
                # of one predicate staying in step.
                is_model_mutation = declares_action(raw_action_intent)

                action_state = None
                if is_deterministic_mutation or is_model_mutation:
                    try:
                        parsed_payload = (
                            json.loads(raw_action_intent)
                            if is_model_mutation and raw_action_intent.startswith("{")
                            else raw_action_intent
                        )
                    except json.JSONDecodeError:
                        parsed_payload = raw_action_intent

                    target_patient_name = (
                        patient_context_dict["demographics"].split(",")[0].replace("PATIENT: ", "")
                        if patient_context_dict
                        else "Unknown"
                    )

                    if (
                        isinstance(parsed_payload, dict)
                        and parsed_payload.get("action_type") == "add_medication"
                    ):
                        med_name = parsed_payload.get("medication", "")

                        match = await run_db(_match_medication, med_name)

                        if match:
                            safe_user_message = f"Drafted request for {match['name']}. Please review and approve to send to the Pharmacy."
                            action_state = {
                                "action_type": "add_medication",
                                "target_entity": "helix_queue",
                                "target_patient": target_patient_name,
                                "requested_payload": parsed_payload,
                                "supported": True,
                                "status_code": "request_drafted",
                                "safe_user_message": safe_user_message,
                                "safe_detail_message": "Action available but requires explicit clinician approval before sending to Helix.",
                            }
                        else:
                            safe_user_message = f"The requested medication ({med_name}) is not available in the pharmacy database."
                            action_state = {
                                "action_type": "add_medication",
                                "target_entity": "helix_queue",
                                "target_patient": target_patient_name,
                                "requested_payload": parsed_payload,
                                "supported": False,
                                "status_code": "not_in_helix_database",
                                "safe_user_message": safe_user_message,
                                "safe_detail_message": "Medication not found in formulary.",
                            }
                    else:
                        # Construct strict action state (mapped to unsupported for now)
                        safe_user_message = CHART_MUTATION_SAFE_MESSAGE

                        action_state = {
                            "action_type": "chart_mutation",
                            "target_entity": "chart",
                            "target_patient": target_patient_name,
                            "requested_payload": parsed_payload,
                            "supported": False,
                            "attempted": False,
                            "success": False,
                            "verified": False,
                            "requires_confirmation": False,
                            "mutation_endpoint_exists": False,
                            "backend_response_present": False,
                            "post_write_readback_passed": False,
                            "status_code": "unsupported",
                            "safe_user_message": safe_user_message,
                            "safe_detail_message": "Direct execution not supported. Clinician review required.",
                        }

                    # Policy Layer: Override hallucinated confirmation language
                    parsed_json["concise_answer"] = safe_user_message
                    # …and the same message, named for what it IS, so the replay
                    # lane does not have to know that `concise_answer` means
                    # something different on this branch than on every other.
                    # sage_service._stored_turn_safe_answer prefers this and
                    # falls back to concise_answer for rows written before it.
                    # This is the answer the CLINICIAN read; the raw narration
                    # it replaced stays in sage_conversations.content, which is
                    # the provenance record and is not what the next prompt
                    # should be shown.
                    turn_safe_answer = safe_user_message

                    # Strip forbidden phrases from detailed assessment
                    parsed_json["clinical_assessment"] = _strip_forbidden(
                        parsed_json.get("clinical_assessment", "")
                    )

                    # ── SHADOW: `action.proposed` ──
                    # Sage proposed something and the clinician has not answered
                    # yet. This is the numerator's left-hand side for `agreement`
                    # and the row the sweeper turns into `action.abandoned` if
                    # nobody ever responds. It is captured for UNSUPPORTED
                    # proposals too: "Sage tried to change the chart and was
                    # blocked" is a fact about the model, and dropping it would
                    # leave only the proposals the gate happened to allow.
                    # `drug_rxnorm` stays None unless the model actually supplied
                    # an RxNorm code — the Helix master id is NOT an RxNorm code,
                    # and putting it in a field named rxnorm would be a lie the
                    # export would carry off-appliance.
                    _payload_dict = parsed_payload if isinstance(parsed_payload, dict) else {}
                    sh_action = {
                        "action_class": action_state.get("action_type"),
                        "status_code": action_state.get("status_code"),
                        "supported": bool(action_state.get("supported")),
                        "gate": (
                            "clinician_approval"
                            if action_state.get("supported")
                            else "blocked_unsupported"
                        ),
                        "target_patient_present": bool(patient_context_dict),
                        "drug_rxnorm": _payload_dict.get("rxnorm"),
                        "dose_present": bool(
                            _payload_dict.get("dose") or _payload_dict.get("dosage")
                        ),
                        "conversation_id": shadow_turn_id,
                    }

                # UNCONDITIONAL forbidden-phrase strip on concise_answer + assessment.
                # NO_ACTION_HALLUCINATION applies to every response, not only when a
                # mutation was detected — detect_mutation_intent/action_intent miss
                # phrasings like "he needs 500 of metoprolol", so a hallucinated
                # "order placed" could otherwise survive into the final answer.
                parsed_json["concise_answer"] = _strip_forbidden(
                    parsed_json.get("concise_answer", "")
                )
                parsed_json["clinical_assessment"] = _strip_forbidden(
                    parsed_json.get("clinical_assessment", "")
                )

                # Validation: cited_sources ⊆ retrieved_sources
                retrieved_ids = [s.get("document_id") for s in sources] if sources else []
                model_named_sources = list(parsed_json.get("cited_sources", []) or [])
                valid_cited_sources = [sid for sid in model_named_sources if sid in retrieved_ids]

                # ── DID THE ANSWER USE WHAT WAS RETRIEVED? ──
                # One measurement, two consumers: the deterministic citation
                # fallback immediately below, and the confidence label further
                # down. Both used to be functions of whether the model closed a
                # <cited_sources> tag; neither is now. See
                # sage_service.assess_answer_grounding for how "used" is decided
                # and why it is deliberately conservative.
                #
                # The rendered prompt and the assembled chart are both passed:
                # a term the model never saw cannot be evidence, and a term it
                # could have taken from the chart is not evidence about a
                # guideline.
                grounding = assess_answer_grounding(
                    answer=parsed_json.get("concise_answer", ""),
                    sources=sources or [],
                    query=model_query,
                    prompt_context=context,
                    patient_context=patient_context_dict,
                )
                # The model's own citations always win — this is a FALLBACK, not
                # a second opinion. It fires only when the model named nothing
                # usable, which on stock MedGemma is the overwhelming majority
                # of turns (0 of 40 carried a citation, 2026-08-14).
                system_attributed = [] if valid_cited_sources else list(grounding["attributed"])

                # Why the answer is grounded the way it is. Stock MedGemma honours the
                # <cited_sources> contract inconsistently (the same reason the untagged
                # -output fallback exists above), so "the model named no source" is the
                # common case, NOT an error. Previously that case shipped an empty
                # source list to the UI and the clinician saw "0 sources" for an answer
                # that had in fact been generated from retrieved hospital policy — the
                # single worst thing a clinical assistant can do to its own credibility.
                #   model_cited       — the model named specific documents, all retrieved.
                #   system_attributed — the model named nothing, but the answer
                #                       demonstrably used specific retrieved passages, so
                #                       ORB attributed them. Labelled distinctly on
                #                       purpose: the clinician and the auditor must be
                #                       able to tell Orb's attribution from the model's.
                #   retrieved         — documents were retrieved and put in the prompt,
                #                       but nothing in the answer shows they were used.
                #                       Still shown, labelled as context, never as a
                #                       citation.
                #   none              — nothing was retrieved; the answer is model-only.
                if valid_cited_sources:
                    citation_basis = "model_cited"
                elif system_attributed:
                    citation_basis = "system_attributed"
                elif sources:
                    citation_basis = "retrieved"
                else:
                    citation_basis = "none"

                # A FABRICATED CITATION MUST NEVER LEAVE THE SERVER.
                # `valid_cited_sources` (above) already filters the model's list
                # down to documents that were actually retrieved, but it was only
                # used to pick citation_basis and to downgrade confidence —
                # `parsed_json["cited_sources"]` still carried the RAW list and is
                # emitted verbatim to the client at the `structured_response`
                # yield below. Measured 2026-08-03 over 70 turns: of 4 citations
                # the model produced, 3 were invented identifiers — "EXT",
                # "SSC_Guidelines", "SSC23" — against a 19-document corpus whose
                # real ids look like "EXT-SEPSIS-004". Only that fourth one was
                # real.
                # A citation that points at nothing is worse than no citation:
                # it looks checkable and is not, and this product's entire trust
                # argument is that a clinician can verify what they are told.
                # The UI happens to render the retrieved-documents panel rather
                # than this field, so nothing was shown to a clinician — but the
                # fabricated ids were on the wire for any consumer (an export, an
                # integration, the shadow log) to pick up and present as real.
                parsed_json["cited_sources"] = list(valid_cited_sources)

                # ── CONFIDENCE IS A STATEMENT ABOUT EVIDENCE ──
                # This block used to read, in full:
                #     final_confidence = confidence
                #     if not valid_cited_sources and sources:
                #         final_confidence = "low"
                # which made the label a function of the model's FORMATTING.
                # Measured over 40 eval turns (2026-08-14): answers that
                # retrieved three guidelines read "low" while elliptical
                # follow-ups that retrieved nothing read "high", and the same
                # question with the same documents and identical fusion scores
                # scored "medium" in one arm and "low" in the other. The rules
                # now live in sage_service.classify_answer_confidence, which the
                # eval harness scores against directly.
                _missing_critical = bool(
                    patient_context_dict
                    and patient_context_dict.get("completeness", {}).get("missing")
                )
                final_confidence, confidence_basis = classify_answer_confidence(
                    retrieval_confidence=confidence,
                    source_count=len(sources) if sources else 0,
                    grounded=grounding["grounded"],
                    query_scope=query_scope,
                    # An empty answer is degraded however good the retrieval was.
                    degraded=not (parsed_json.get("concise_answer") or "").strip(),
                    # The model named documents and NOT ONE was retrieved.
                    fabricated_citation=bool(model_named_sources) and not valid_cited_sources,
                    missing_critical_context=_missing_critical,
                )

                local_policy_found = (
                    any(s.get("trust_tier") in [1, 2] for s in sources) if sources else False
                )

                # --- APP-LEVEL DISPLAY MODE DECISION ---
                # Model's display_mode is advisory; app logic makes the final call
                model_display_mode = parsed_json.get("display_mode", "concise").lower().strip()
                has_escalation = escalation or (
                    parsed_json.get("escalation")
                    and parsed_json["escalation"].lower().strip() not in ("none", "none.")
                )
                has_missing_critical = bool(
                    patient_context_dict
                    and patient_context_dict.get("completeness", {}).get("missing")
                )

                # App determines final display mode
                should_expand = (
                    model_display_mode == "detailed"
                    or has_escalation
                    or final_confidence == "low"
                    or has_missing_critical
                )
                final_display_mode = "detailed" if should_expand else "concise"

                metadata = {
                    "action_state": action_state,
                    # EVERY retrieved document is returned, each flagged with whether the
                    # model actually cited it. Filtering to model-cited only (the old
                    # behaviour) discarded the evidence the answer was built from
                    # whenever the model skipped the citation tag. The clinician needs to
                    # see what Sage read, not only what Sage remembered to name.
                    "sources": [
                        {
                            "document_id": s.get("document_id"),
                            "title": s.get("title"),
                            "source": s.get("source"),
                            "source_type": s.get("source_type"),
                            "trust_tier": s.get("trust_tier"),
                            "is_synthetic": s.get("is_synthetic", False),
                            "category": s.get("category"),
                            "fusion_score": round(s.get("fusion_score", 0), 4),
                            # `cited` keeps its ONE meaning: the model named this
                            # document. The UI badges it, and a system
                            # attribution rendered under that badge would be Orb
                            # putting words in the model's mouth.
                            "cited": s.get("document_id") in valid_cited_sources,
                            # …so the attribution rides alongside, named for what
                            # it is. `context_only` = retrieved and shown, but
                            # nothing in the answer shows it was used.
                            "attribution": (
                                "model_cited"
                                if s.get("document_id") in valid_cited_sources
                                else "system_attributed"
                                if s.get("document_id") in system_attributed
                                else "context_only"
                            ),
                        }
                        for s in sources
                    ]
                    if sources
                    else [],
                    "citation_basis": citation_basis,
                    "cited_count": len(valid_cited_sources),
                    "attributed_count": len(system_attributed),
                    "attributed_sources": list(system_attributed),
                    "confidence": final_confidence,
                    # Why it reads the way it does. The eval harness scores this
                    # directly, and a clinician asking "why medium?" deserves an
                    # answer that is not "the model forgot a tag".
                    "confidence_basis": confidence_basis,
                    "escalation": has_escalation,
                    "display_mode": final_display_mode,
                    "query_scope": query_scope,
                    "patient_context_used": patient_context_dict is not None,
                    "patient_id": patient_context_dict["patient_id"]
                    if patient_context_dict
                    else None,
                    "patient_name": _chart_patient_name(patient_context_dict),
                    "patient_source": patient_context_dict.get("source_of_truth")
                    if patient_context_dict
                    else None,
                    "data_completeness": patient_context_dict["completeness"]
                    if patient_context_dict
                    else None,
                    "local_policy_found": local_policy_found,
                    "provider": route_info or None,
                    "timing": {
                        "retrieval_ms": round(t_retrieval),
                        "ttft_ms": round(t_first_token) if t_first_token else None,
                        "first_content_ms": round(t_first_emit) if t_first_emit else None,
                        "total_ms": round(t_total),
                    },
                }
                # Turns that streamed incrementally already delivered the answer;
                # re-emitting it would duplicate text client-side (the client
                # concatenates content events). Buffered turns — mutation turns
                # (answer was REWRITTEN by the action-state override) and
                # malformed-XML turns — emit the sanitized answer as one event.
                if not emitted_stream:
                    safe_answer = parsed_json.get("concise_answer", "")
                    if safe_answer:
                        yield f"data: {json.dumps({'content': safe_answer})}\n\n"

                # Ruling E: the screen replaces the streamed text with
                # `structured_response.concise_answer` once it arrives, so the
                # label must be IN it, not only in the stream (attempt 5 lost it
                # here).
                if (
                    _answer_label
                    and parsed_json.get("concise_answer")
                    and not parsed_json["concise_answer"].startswith(_answer_label)
                ):
                    parsed_json["concise_answer"] = (
                        f"{_answer_label}\n\n{parsed_json['concise_answer']}"
                    )

                yield f"data: {json.dumps({'metadata': metadata})}\n\n"

                # Also yield the structured parsed JSON as a separate event for frontend rendering
                yield f"data: {json.dumps({'structured_response': parsed_json})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            print(f"  [Sage] Stream error: {e}")
            sh_failure = "stream"
            yield f"data: {json.dumps({'error': 'Sage could not complete this answer. Please retry.', 'code': 'SAGE_STREAM_FAILED'})}\n\n"
        finally:
            meta_json = None
            if full_response:
                meta_json = json.dumps(
                    {
                        # The audit record keeps BOTH lists. "What did the model claim to
                        # cite" and "what evidence was actually in front of it" are different
                        # questions, and a retrospective review of a bad answer needs the
                        # second one — storing only the citations lost the entire retrieval
                        # context for every turn where the model skipped the tag.
                        "sources": valid_cited_sources
                        if not cancelled and "valid_cited_sources" in locals()
                        else [],
                        # System-attributed documents are kept SEPARATE from
                        # `sources` here rather than merged into it: a reopened
                        # conversation must not be able to grow model citations that
                        # never existed. `citation_basis` names which of the two the
                        # turn actually shipped.
                        "attributed_sources": (
                            list(system_attributed)
                            if not cancelled and "system_attributed" in locals()
                            else []
                        ),
                        "retrieved_sources": (
                            [s.get("document_id") for s in sources]
                            if not cancelled and "sources" in locals() and sources
                            else []
                        ),
                        "citation_basis": citation_basis
                        if not cancelled and "citation_basis" in locals()
                        else "none",
                        "confidence_basis": (
                            confidence_basis
                            if not cancelled and "confidence_basis" in locals()
                            else None
                        ),
                        "confidence": final_confidence
                        if not cancelled and "final_confidence" in locals()
                        else confidence,
                        "escalation": escalation,
                        "timing_ms": round((time.perf_counter() - t_start) * 1000),
                        # The speed footer's own numbers, persisted so a RELOADED
                        # conversation renders the SAME footer the clinician saw
                        # live. Kept apart from `timing_ms` above, which is the
                        # audit record's single wall-clock number in a different
                        # shape. `t_total` is reused rather than re-measured here
                        # for exactly that reason — re-measuring in the finally
                        # would make a reopened turn quote a slightly larger total
                        # than the one it showed while streaming. It is unbound on
                        # a cancelled turn, which is honestly unmeasured.
                        "timing": _answer_timing(
                            ttft_ms=round(t_first_token) if t_first_token else None,
                            total_ms=round(t_total) if "t_total" in locals() else None,
                        ),
                        "thinking_tier": (route_info or {}).get("thinking_tier"),
                        # The patient this turn was about, as the live metadata
                        # named them, so the history sidebar can label the thread.
                        "patient_name": (
                            _chart_patient_name(patient_context_dict)
                            if "patient_context_dict" in locals()
                            else None
                        ),
                        # DID THIS TURN PROPOSE A CHART MUTATION — resolved here,
                        # where BOTH gates are known, rather than left to be
                        # re-derived from `parsed_response.action_intent` later.
                        # That field alone is not the answer: the deterministic gate
                        # locks a turn on the QUERY with the model declaring
                        # nothing, which is every mutation the fast tier has ever
                        # handled (it declares nothing at all, 0/15 measured), and
                        # `action_intent` on those rows reads "None.". The history
                        # assembly path (sage_service._history_turn_proposed_action)
                        # reads this to decide whether the turn may be replayed
                        # under a synthesized `NOACT|`, so recording only half the
                        # decision would label a mutation turn action-free.
                        #
                        # True means locked; False means both gates cleared it; null
                        # means the turn ended before the model's half was decided,
                        # and unknown is treated as unknown rather than as False.
                        "proposed_action": (
                            True
                            if is_deterministic_mutation
                            else (
                                bool(is_model_mutation) if "is_model_mutation" in locals() else None
                            )
                        ),
                        # Set only on a turn the action-state override rewrote, so
                        # its presence IS the statement "content is not what was
                        # shown". Unbound on every other path, hence the locals()
                        # probe the rest of this literal already uses.
                        # With the ruling E label when the screen showed one: a
                        # reloaded order turn is read from here (/history), and
                        # attempt 7 reloaded "Drafted request for ..." unlabelled.
                        "safe_answer": (
                            _with_label(turn_safe_answer, _answer_label)
                            if "turn_safe_answer" in locals()
                            else None
                        ),
                        "parsed_response": parsed_json
                        if not cancelled and "parsed_json" in locals()
                        else {},
                    }
                )
            # Two SQLCipher INSERTs that ran on the loop at the end of EVERY Sage
            # turn, right where the next request is waiting to be served. The
            # meta_json literal above stays in this frame on purpose: it probes
            # `locals()` for names this generator may never have bound, and that
            # only works in the frame that owns them.
            #
            # Awaiting inside a generator's finally is safe here — an executor
            # await does not yield a value, so aclose()/cancellation on client
            # disconnect still lets the save complete rather than raising
            # "async generator ignored GeneratorExit". Both writes are appends,
            # so ordering against a concurrent turn does not matter.
            await run_db(
                _save_turn,
                user["id"],
                query,
                "" if sh_failure else _labelled(full_response, _answer_label),
                meta_json,
                conversation_id=conversation_id,
            )

            # ══ PROVENANCE: everything needed to REPLAY this turn ══════════════
            # Same "every exit lands here" property as the shadow block below, and
            # the same swallow-everything discipline — but a different artefact.
            # The shadow row says a turn happened and how it scored; this says
            # what the turn WAS. In particular `patient_context_dict` is stored BY
            # VALUE: the chart will not look like this in six months, and a
            # provenance record that re-reads it then is a record of the wrong day.
            #
            # The client already has `done: True` at this point on every path, so
            # nothing here is on the clinician's critical path; `_prov_record`
            # additionally puts the write on the DB executor, never the loop.
            # Same accounting split the shadow block below makes, and made HERE
            # too rather than read from it: `sh_failure` is not set to
            # "empty_response" until after this call, so trusting it alone would
            # file a zero-content turn as a delivered answer.
            _prov_outcome = (
                "failed"
                if (sh_failure or (not full_response and not cancelled))
                else "cancelled"
                if cancelled
                else "answered"
            )
            await _prov_record(
                turn_id=shadow_turn_id,
                lane=SHADOW_CHAT_LANE,
                outcome=_prov_outcome,
                user=user,
                question=query,
                patient_id=turn_patient_id,
                # `patient_context_dict` is the assembled chart; `context` is the
                # rendered prompt block. Only the first is stored (the second is
                # the first plus the passages, so keeping it would duplicate — and
                # defeat the dedup on — both). The prompt's sha256 goes with it as
                # the integrity anchor tying the two together.
                patient_context=(
                    patient_context_dict if "patient_context_dict" in locals() else None
                ),
                prompt_context=(context if "context" in locals() else None),
                sources=(sources if "sources" in locals() else None),
                route_info=(route_info or _no_model_route_info),
                answer=(
                    (parsed_json.get("concise_answer") or "")
                    if "parsed_json" in locals() and parsed_json
                    else _strip_forbidden(full_response)
                ),
                raw_output=full_response,
                structured=(parsed_json if "parsed_json" in locals() else None),
                # The documents this turn ATTRIBUTED, by whichever route —
                # `citation_basis` alongside says which, so a replay can tell
                # "the model cited this" from "Orb attributed this". Storing
                # only the model's list would have lost the attribution
                # entirely, which on this model means losing all of it.
                cited_doc_ids=(
                    (
                        valid_cited_sources
                        or (system_attributed if "system_attributed" in locals() else [])
                    )
                    if "valid_cited_sources" in locals()
                    else []
                ),
                citation_basis=(citation_basis if "citation_basis" in locals() else None),
                confidence=(
                    final_confidence
                    if "final_confidence" in locals()
                    else (confidence if "confidence" in locals() else None)
                ),
                escalation=bool(has_escalation) if "has_escalation" in locals() else False,
                query_scope=(query_scope if "query_scope" in locals() else None),
                action_status=(
                    (action_state or {}).get("status_code")
                    if "action_state" in locals() and action_state
                    else None
                ),
                timings={
                    "retrieval_ms": sh_retrieval_ms,
                    "ttft_ms": round(t_first_token) if t_first_token else None,
                    "total_ms": round((time.perf_counter() - t_start) * 1000),
                },
            )

            # ══ SHADOW: the turn's own row(s) ══════════════════════════════════
            # EVERY exit from the try above lands here — the prose fast-path
            # `return`, the XML path, a client disconnect, and an exception — so
            # this is the one place where "a Sage turn happened" can be recorded
            # without a path that quietly skips it. (The retrieval failure above
            # is the single exception; it returns before the try and emits its
            # own row.)
            #
            # Which row, and why the split matters: `_time_saved` counts
            # `sage.drafted` rows and multiplies them by an ASSUMED manual
            # baseline. So anything that was not a delivered answer must not
            # arrive as a clean draft:
            #   * a client disconnect -> sage.drafted with cancelled=True, which
            #     the metric explicitly excludes (the row still exists, so the
            #     abandonment rate is visible);
            #   * a stream exception  -> sage.failed;
            #   * an empty generation -> sage.failed("empty_response"), NOT a
            #     zero-content draft that would still be counted as an answer.
            try:
                from services import shadow_service as _shadow

                if not sh_failure and not full_response and not cancelled:
                    sh_failure = "empty_response"
                if sh_episode is None:
                    sh_episode = await _sh_episode(effective_patient_id, SHADOW_CHAT_LANE)
                if sh_episode:
                    _sh_sources = sources if "sources" in locals() and sources else []
                    _sh_events = []
                    if sh_failure:
                        _sh_events.append(
                            _shadow.build(
                                "sage.failed",
                                sh_episode,
                                SHADOW_CHAT_LANE,
                                patient_id=effective_patient_id,
                                actor_user_id=user["id"],
                                actor_role=user.get("role"),
                                payload={"failure": sh_failure},
                            )
                        )
                    else:
                        _sh_events.append(
                            _shadow.build(
                                "sage.drafted",
                                sh_episode,
                                SHADOW_CHAT_LANE,
                                patient_id=effective_patient_id,
                                actor_user_id=user["id"],
                                actor_role=user.get("role"),
                                payload={
                                    "conversation_id": shadow_turn_id,
                                    "citation_basis": (
                                        citation_basis if "citation_basis" in locals() else None
                                    ),
                                    "confidence": (
                                        final_confidence
                                        if "final_confidence" in locals()
                                        else (confidence if "confidence" in locals() else None)
                                    ),
                                    "retrieved_doc_ids": [
                                        s.get("document_id") for s in _sh_sources
                                    ],
                                    # Same union as the provenance record, and for
                                    # the same reason: `citation_basis` in this very
                                    # payload says whether the model named them or
                                    # Orb attributed them, so the shadow analysis can
                                    # count each honestly. (No new payload key — the
                                    # sage.drafted schema in shadow_service is a
                                    # whitelist.)
                                    "cited_doc_ids": (
                                        (
                                            valid_cited_sources
                                            or (
                                                system_attributed
                                                if "system_attributed" in locals()
                                                else []
                                            )
                                        )
                                        if "valid_cited_sources" in locals()
                                        else []
                                    ),
                                    # key=str, not a bare sort: a corpus row with a
                                    # string tier next to an int one would raise, and
                                    # the except below would then drop the WHOLE turn
                                    # — draft and proposal together — over a
                                    # cosmetic field.
                                    "trust_tiers": sorted(
                                        {
                                            s.get("trust_tier")
                                            for s in _sh_sources
                                            if s.get("trust_tier") is not None
                                        },
                                        key=str,
                                    ),
                                    "ttft_ms": round(t_first_token) if t_first_token else None,
                                    "retrieval_ms": sh_retrieval_ms,
                                    "total_ms": round((time.perf_counter() - t_start) * 1000),
                                    "query_class": (
                                        query_scope if "query_scope" in locals() else None
                                    ),
                                    "cancelled": bool(cancelled),
                                },
                            )
                        )
                    if sh_action:
                        _sh_events.append(
                            _shadow.build(
                                "action.proposed",
                                sh_episode,
                                SHADOW_CHAT_LANE,
                                patient_id=effective_patient_id,
                                actor_user_id=user["id"],
                                actor_role=user.get("role"),
                                payload=sh_action,
                            )
                        )
                    await _shadow.aemit_many(_sh_events)
            except Exception:
                # Rule 1: the observational log never breaks the clinical path,
                # and it is the last thing this generator does.
                pass

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PROVENANCE REPLAY — admin / audit only
# ══════════════════════════════════════════════════════════════════════════════
# THIS IS A PHI SURFACE. It returns whole chart snapshots, so it is held to the
# same three rules as every other one:
#   * `audit.read` (admin-only in ROLE_PERMISSIONS) — a clinician has no business
#     reading back another clinician's turns;
#   * scope resolved ONLY through permissions.department_scope(user), and 404 —
#     never 403 — on an out-of-department turn, because a 403 confirms that a
#     turn about that patient exists;
#   * one `phi_access` audit row per read, fail-closed.
#
# Every handler is a plain `def`. FastAPI threadpools those, so the decrypt +
# decompress of a chart-sized blob never runs on the event loop. The literal
# `/storage` path is declared BEFORE `/{turn_id}` — FastAPI matches in
# declaration order and the parametrised route would otherwise swallow it.


_GET_PROVENANCE_STORAGE_USER_DEFAULT = Depends(require_permission("audit.read"))


@router.get("/provenance/storage")
def get_provenance_storage(
    request: Request,
    turns_per_day: int = 200,
    user: dict = _GET_PROVENANCE_STORAGE_USER_DEFAULT,
):
    """What the provenance store costs, measured — not estimated.

    NO PHI: counts and byte totals only, so it needs no patient scope. The
    projection is straight-line arithmetic over the MEASURED bytes-per-turn, so
    the number an operator reads here is the number their disk will see."""
    from services import sage_provenance_service as prov

    return prov.storage_stats(turns_per_day=max(1, min(int(turns_per_day or 200), 100000)))


_LIST_PROVENANCE_USER_DEFAULT = Depends(require_permission("audit.read"))


@router.get("/provenance")
def list_provenance(
    request: Request,
    patient_id: str | None = None,
    actor_user_id: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: dict = _LIST_PROVENANCE_USER_DEFAULT,
) -> dict:
    """Metadata index of recorded turns. NO question text, NO chart, NO answer.

    Reading a turn's CONTENT means naming it on /provenance/{turn_id}, which is
    the call that writes the per-patient audit row. Keeping the index to metadata
    means an auditor scanning for "which turns did the safety floor fire on"
    does not incidentally read fifty charts.

    `patient_id` is a caller-supplied id and is therefore vetted, not trusted —
    enforce_patient_scope 404s an out-of-ward patient rather than returning an
    empty list, which would itself confirm the ward.

    With more than one facility on record (multisite-10) the id set keeps to
    the reader's facilities too, and a facility-bound reader with no
    department lists no patient-less turn: the turn records a department, not
    a facility, so whose it was cannot be told."""
    from permissions import enforce_patient_scope, facility_scope, scope_patient_ids_sql

    from services import sage_provenance_service as prov

    if patient_id:
        enforce_patient_scope(user, patient_id)
    dept = department_scope(user)
    facilities = facility_scope(user)
    # `scope_department` travels WITH the id set: patient-less turns cannot be
    # scoped through a patient's admission (there is no patient), so they are
    # scoped by the department recorded on the turn at write time instead.
    turns = prov.list_turns(
        patient_ids_sql=(
            None
            if dept is None and facilities is None
            else scope_patient_ids_sql(dept, facilities=facilities)
        ),
        scope_department=dept,
        patient_id=patient_id,
        actor_user_id=actor_user_id,
        since=since,
        limit=limit,
        offset=offset,
    )
    return {
        "turns": turns,
        "count": len(turns),
        "scope": {"department": dept, "hospital_wide": dept is None and facilities is None},
    }


_REPLAY_PROVENANCE_USER_DEFAULT = Depends(require_permission("audit.read"))


@router.get("/provenance/{turn_id}")
def replay_provenance(
    turn_id: str,
    request: Request,
    user: dict = _REPLAY_PROVENANCE_USER_DEFAULT,
) -> dict:
    """Reconstruct one Sage turn from what was stored AT THE TIME.

    The chart in this response is a snapshot, not a live read: it is what the
    model was shown, which is the only thing that can answer "why did it say
    that?" months later."""
    from permissions import enforce_patient_scope, facility_scope, log_phi_read

    from services import sage_provenance_service as prov

    row = prov.get_turn_row(turn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provenance turn not found")

    # Resolve scope on the STORED patient before reconstructing anything. Same
    # 404-not-403 rule as permissions.enforce_patient_scope, for the same reason.
    # A PATIENT-LESS turn is scoped by the department recorded on it at write
    # time — the asking user's ward — exactly as the listing scopes it. A turn
    # whose recorded department is NULL was asked by an unscoped governance
    # account and is readable only by unscoped auditors.
    if row.get("patient_id"):
        enforce_patient_scope(user, row.get("patient_id"))
    else:
        dept = department_scope(user)
        if dept is not None and row.get("department") != dept:
            raise HTTPException(status_code=404, detail="Provenance turn not found")
        # A facility-bound reader with no department replays no patient-less
        # turn, exactly as the listing shows none (multisite-10).
        if dept is None and facility_scope(user) is not None:
            raise HTTPException(status_code=404, detail="Provenance turn not found")

    record = prov.replay(turn_id)
    if record is None:  # pragma: no cover — raced with a retention prune
        raise HTTPException(status_code=404, detail="Provenance turn not found")

    # Fail-closed: if this cannot be written, the PHI is not returned.
    log_phi_read(
        request=request,
        user=user,
        action_type="sage_provenance_replay",
        summary_message=f"Replayed Sage provenance turn {turn_id}",
        patient_id=row.get("patient_id"),
        target_type="sage_provenance_turn",
        target_id=turn_id,
        metadata={
            "lane": row.get("lane"),
            "outcome": row.get("outcome"),
            "turn_occurred_at": row.get("occurred_at"),
        },
    )
    return record


_GET_HISTORY_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.get("/history")
def get_history(user: dict = _GET_HISTORY_USER_DEFAULT):
    """This user's own past turns, for the "Recent Conversations" sidebar.

    Deliberately NOT get_conversation_history: that function is prompt context
    and is scoped to a single thread. This is a browsing list across threads.

    An assistant turn's `content` is the DISPLAY answer, projected out of the
    stored raw reply by the same parse+strip the live stream applies (see
    `_display_answer`). Storage keeps the raw output; this surface is a chat
    bubble and never shows contract syntax.

    An assistant turn carries `metadata.timing` (and `metadata.provider.
    thinking_tier` when the turn recorded one) so a reloaded conversation shows
    the same per-answer speed footer the clinician saw live. A turn stored
    before that was persisted carries no `metadata` key at all, and the footer
    renders nothing for it — which is the truth about a turn nobody timed.
    """
    return {"messages": _history_rows(user["id"])}


_CLEAR_HISTORY_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.delete("/history")
def clear_history(user: dict = _CLEAR_HISTORY_USER_DEFAULT):
    """Clear conversation history."""
    from database import get_db

    db = get_db()
    db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (user["id"],))
    db.commit()
    db.close()
    return {"message": "History cleared"}


# ═══════════════════════════════════════════════════
# Voice Agent Endpoint (Conversational AI via MedGemma 4B)
# ═══════════════════════════════════════════════════


_SAGE_VOICE_QUERY_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.post("/voice")
async def sage_voice_query(request: Request, user: dict = _SAGE_VOICE_QUERY_USER_DEFAULT):
    """Conversational voice query via MedGemma 4B.

    Accepts transcribed speech text, injects patient context from local DB,
    and streams response tokens for real-time TTS playback.

    Request body:
        {"query": "What are the side effects of Metoprolol?", "patient_id": "P001"}

    Response (SSE stream):
        data: {"token": "Metoprolol "}
        data: {"token": "can cause "}
        ...
        data: {"done": true, "timing": {"ttft_ms": 450, "total_ms": 2300}}
    """
    body = await request.json()
    query = body.get("query", "").strip()
    patient_id = body.get("patient_id")
    view_context = body.get("view_context") if isinstance(body.get("view_context"), dict) else None
    conversation_history = body.get("conversation_history", [])
    # The on-screen patient scopes the voice turn too.
    effective_patient_id = patient_id or ((view_context or {}).get("patient_id") or None)

    if not query:
        return {"error": "Query is required"}

    # Pre-LLM safety gate applies to spoken queries exactly like typed ones.
    refusal = _safety_refusal(query)
    if refusal:
        _refusal_class = _sh_refusal_class(refusal)

        async def _refuse_voice():
            yield f"data: {json.dumps({'token': refusal})}\n\n"
            # Nulls, not zeros: nothing ran, so nothing was timed. This used to
            # ship {ttft_ms: 0, total_ms: 0, tokens: 0}, which the footer would
            # have rendered as a "0.0s" answer had the client not defended
            # against it — see _answer_timing.
            yield f"data: {json.dumps({'done': True, 'timing': _answer_timing()})}\n\n"
            # SHADOW: the voice lane records refusals on the same footing as the
            # chat lane. A guardrail that is only measured on one surface is how
            # a hole ships in the other (see FORBIDDEN_PHRASE_PATTERNS above).
            await _sh_refused(
                effective_patient_id, SHADOW_VOICE_LANE, _refusal_class, "pre_llm", user
            )

        return StreamingResponse(_refuse_voice(), media_type="text/event-stream")

    #: Links this voice turn's shadow rows (see the chat lane's equivalent).
    shadow_turn_id = uuid.uuid4().hex[:16]

    # The chat lane's mutation gate, before any model runs. Voice drafts
    # nothing, so a spoken chart change is never generated at all: Sage says the
    # chat lane's fixed message, and that is what is stored and replayed.
    if _mutation_gate(query):
        # Read in the request's own context, not inside the generator (see
        # _prov_no_model_route_info).
        _gate_route_info = _prov_no_model_route_info()

        async def _decline_voice_mutation():
            yield f"data: {json.dumps({'token': CHART_MUTATION_SAFE_MESSAGE})}\n\n"
            timing = _answer_timing()  # nothing ran, so nothing was timed
            yield f"data: {json.dumps({'done': True, 'timing': timing})}\n\n"
            # The stored content IS the safe message, so no separate safe_answer;
            # the verdict marks the turn as a mutation for every replay reader.
            await run_db(
                _save_voice_turn,
                user["id"],
                query,
                CHART_MUTATION_SAFE_MESSAGE,
                json.dumps({"timing": timing, "proposed_action": True}),
            )
            await _prov_record(
                turn_id=shadow_turn_id,
                lane=SHADOW_VOICE_LANE,
                outcome="answered",
                user=user,
                question=query,
                patient_id=effective_patient_id,
                answer=CHART_MUTATION_SAFE_MESSAGE,
                query_scope="VOICE",
                citation_basis="none",
                action_status="unsupported",
                route_info=_gate_route_info,
            )

        return StreamingResponse(_decline_voice_mutation(), media_type="text/event-stream")

    # Guardrails apply to every provider AND every surface: a hallucinated
    # "order placed" must not be SPOKEN either. The voice lane uses the SHARED
    # FORBIDDEN_PHRASE_PATTERNS through the same sentence-granularity sanitizer
    # as the chat stream — it must never keep its own copy of the list. It did
    # once, and the copy silently drifted (it missed the line-initial "Done."
    # anchor added to the shared pattern), which is exactly how a guardrail
    # ships with a hole in one surface only.
    #
    # Whitespace: the voice lane needs the STREAMING policy (whitespace
    # preserved), because its tokens are concatenated by the TTS client just
    # like the chat stream's segments — hence _strip_forbidden_streaming rather
    # than _strip_forbidden. That whitespace policy is the ONLY difference
    # between the two lanes; the patterns and the fixed-point behaviour are one.

    async def stream_voice_response():
        cancelled = False
        try:
            from config import VOICE

            if not VOICE["conversational"]["enabled"]:
                yield f"data: {json.dumps({'error': 'Voice agent is disabled'})}\n\n"
                return

            from services.voice_agent_service import (
                collect_voice_retrieval,
                get_voice_agent,
            )

            agent = get_voice_agent()
            # Filled by the agent's retrieval inside this request's context.
            voice_retrieval = collect_voice_retrieval()

            t0 = time.perf_counter()
            first_token = True
            ttft_ms = None
            full_response = ""
            sanitizer = SentenceStreamSanitizer(_strip_forbidden_streaming)

            # `user` first-class, exactly as the chat lane passes it: the voice
            # agent builds its prompt from get_relevant_context, which is
            # department-scoped and vets effective_patient_id against the user's
            # own department rather than trusting the request.
            async for token in agent.generate_stream(
                query,
                user,
                patient_id=effective_patient_id,
                conversation_history=conversation_history,
            ):
                if await request.is_disconnected():
                    cancelled = True
                    break
                if first_token:
                    ttft_ms = round((time.perf_counter() - t0) * 1000)
                    first_token = False
                full_response += token
                safe = sanitizer.feed(token)
                if safe:
                    yield f"data: {json.dumps({'token': safe})}\n\n"

            tail = sanitizer.flush()
            if tail:
                yield f"data: {json.dumps({'token': tail})}\n\n"

            total_ms = round((time.perf_counter() - t0) * 1000)

            answer_timing = _answer_timing(
                ttft_ms=ttft_ms, total_ms=total_ms, tokens=len(full_response.split())
            )
            yield f"data: {json.dumps({'done': True, 'timing': answer_timing})}\n\n"

            # Save to conversation history (off the loop; both writes in one hop).
            # The stored turn carries the SAME timing the done-event just sent,
            # so reopening this conversation shows the footer the clinician saw
            # rather than dropping it. No thinking tier: the voice lane never
            # goes through the tier decision, and inventing "fast" here would be
            # asserting a routing choice that was never made.
            # The patient this spoken turn read: the chart retrieval attached
            # (by name or sticky anchoring as well as by id), and nobody on a
            # cross-department refusal, exactly as the chat lane records it.
            voice_chart = voice_retrieval.get("patient_context")
            if voice_retrieval.get("query_scope") == "OUT_OF_SCOPE":
                voice_patient_id = None
            elif isinstance(voice_chart, dict) and voice_chart.get("patient_id"):
                voice_patient_id = voice_chart["patient_id"]
            else:
                voice_patient_id = effective_patient_id

            await run_db(
                _save_voice_turn,
                user["id"],
                query,
                full_response,
                json.dumps(
                    {
                        "timing": answer_timing,
                        "patient_name": (
                            _chart_patient_name(voice_chart) if voice_patient_id else None
                        ),
                    }
                ),
            )

            # Per-inference provider record (local voice lane; fail-soft).
            try:
                from config import VOICE as _V
                from services.ai_governance import record_inference

                # Whole call in ONE executor hop, never split: record_inference
                # appends to the tamper-evident audit hash chain, which is a
                # read-last-hash-then-write. It defends itself against parallel
                # writers (BEGIN IMMEDIATE + a UNIQUE index on prev_hash with a
                # retry), but only if the sequence stays inside one call.
                await run_db(
                    lambda: record_inference(
                        provider="mlx",
                        model=str(_V["conversational"]["model_path"]).rsplit("/", 1)[-1],
                        purpose="sage_voice",
                        outcome="served",
                        user_id=user["id"],
                        patient_id=voice_patient_id,
                        timing_ms={"ttft": ttft_ms, "total": total_ms},
                    )
                )
            except Exception:
                pass

            # ── SHADOW: the spoken turn's row ──
            # The voice lane is a clinician lane, so its answers belong in
            # `time_saved` exactly like typed ones — and its failures must not
            # quietly vanish from the same denominator. `retrieval_ms` is None
            # here on purpose: the voice agent assembles its own context and
            # nothing measured a retrieval phase, and a fabricated 0 would look
            # like an instantaneous retrieval on the scorecard.
            try:
                _v_episode = await _sh_episode(effective_patient_id, SHADOW_VOICE_LANE)
                if not full_response and not cancelled:
                    await _sh_turn_failed(
                        effective_patient_id,
                        SHADOW_VOICE_LANE,
                        "empty_response",
                        user,
                        episode_id=_v_episode,
                    )
                else:
                    await _sh_emit(
                        "sage.drafted",
                        _v_episode,
                        SHADOW_VOICE_LANE,
                        patient_id=effective_patient_id,
                        actor_user_id=user["id"],
                        actor_role=user.get("role"),
                        payload={
                            "conversation_id": shadow_turn_id,
                            "citation_basis": "none",
                            "ttft_ms": ttft_ms,
                            "total_ms": total_ms,
                            "query_class": "VOICE",
                            "cancelled": bool(cancelled),
                        },
                    )
            except Exception:
                pass

            # ── PROVENANCE: the spoken turn ──
            # A guardrail measured on one surface only is how a hole ships in the
            # other, and the same is true of the record: "we can replay typed
            # turns" is not an answer when the question is about a spoken one.
            # The voice agent assembles its own context, and what it was given
            # is collected as it builds the prompt (voice_retrieval): the chart
            # by value, the retrieved passages, and the prompt's hash. A turn
            # the agent built with no chart still stores none rather than a
            # faked one.
            await _prov_record(
                turn_id=shadow_turn_id,
                lane=SHADOW_VOICE_LANE,
                outcome=("cancelled" if cancelled else "answered" if full_response else "failed"),
                user=user,
                question=query,
                patient_id=voice_patient_id,
                patient_context=(voice_chart if voice_patient_id else None),
                prompt_context=voice_retrieval.get("prompt"),
                answer=_strip_forbidden(full_response),
                raw_output=full_response,
                query_scope="VOICE",
                citation_basis="none",
                sources=voice_retrieval.get("sources") or None,
                route_info={"provider": "mlx", "model": _voice_model_name()},
                timings={"ttft_ms": ttft_ms, "total_ms": total_ms},
            )

        except Exception as e:
            print(f"  [Sage/Voice] Stream error: {e}")
            await _sh_turn_failed(effective_patient_id, SHADOW_VOICE_LANE, "stream", user)
            # PROVENANCE for the turn that broke. The happy-path capture above is
            # inside the try, so without this a voice outage would be the one
            # class of turn the store cannot show — and "it crashed" is a thing a
            # reviewer needs to be able to read, not infer from a gap.
            # `locals()` probed rather than pre-initialised: the names below are
            # bound inside the try and the exception may have preceded any of them.
            await _prov_record(
                turn_id=shadow_turn_id,
                lane=SHADOW_VOICE_LANE,
                outcome="failed",
                user=user,
                question=query,
                patient_id=effective_patient_id,
                answer="",
                raw_output=(full_response if "full_response" in locals() else ""),
                query_scope="VOICE",
                refusal_class=None,
                route_info={"provider": "mlx", "model": _voice_model_name()},
            )
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        stream_voice_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ═══════════════════════════════════════════════════
# AI Governance Posture + Speculative Context Prefetch
# ═══════════════════════════════════════════════════


_GET_PATIENT_WORLD_USER_DEFAULT = Depends(
    require_patient_access("patient.read_all", audit_action="sage_patient_world_viewed")
)


@router.get("/world/patient/{patient_id}")
def get_patient_world(
    patient_id: str,
    # Was gated on `sage.query` alone, twelve lines above the ward-brief sibling
    # that already carries the comment explaining why that is not enough. Two
    # separate holes, and only the second one is closed by ward scoping:
    #
    #  1. WARD — any nurse could read any patient's diagnosis, risk history and
    #     vital trends. require_patient_access resolves patient_id off the path
    #     and 404s outside the caller's department.
    #  2. ROLE — `sage.query` is granted to PHARMACIST, who deliberately has no
    #     patient.read_all and is 403'd by /api/vigil/patients/{id}. Scoping
    #     alone does NOT close that: a pharmacy account with a blank department
    #     scopes to None (unscoped governance account, by convention) and would
    #     sail through. Requiring patient.read_all closes it deterministically.
    #     Holding `sage.query` says this account may ask Sage things; it does
    #     not say which patients it may see.
    user: dict = _GET_PATIENT_WORLD_USER_DEFAULT,
):
    """The persistent, versioned temporal state for one patient (trend
    directions + risk trajectory over time). PHI — local only; the state_json is
    field-encrypted at rest when a key is configured."""
    from services.world_model_service import get_patient_state

    state = get_patient_state(patient_id)
    if state is None:
        return {"error": "no such patient"}
    return state


_GET_WARD_BRIEF_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.get("/ward-brief")
def get_ward_brief(request: Request, user: dict = _GET_WARD_BRIEF_USER_DEFAULT):
    """The situational-awareness narrator's live 'state of the ward' briefing:
    a deterministic snapshot (census, risk mix, watchlist, recent events) plus
    an optional local-LLM narrative. Local engine only — the snapshot is PHI.

    Scoped since 2026-08-05 and READ-AUDITED since the sweep that followed it:
    scoping stopped the disclosure crossing wards, but the watchlist still names
    the deteriorating patients on the caller's own ward, and a bulk PHI read
    that leaves no row is a read the hospital cannot account for afterwards.
    """
    from permissions import department_scope, log_phi_read
    from services.sage_narrator_service import ward_narrator

    # Scoped to the AUTHENTICATED user. Holding `sage.query` says this account may
    # ask Sage things; it does not say which patients it may see.
    dept = department_scope(user)
    brief = ward_narrator.get_brief(dept)

    # Only when patients were actually named. The card refreshes on the
    # narrator's socket event, so an empty watchlist is a common no-op.
    watchlist = ((brief or {}).get("snapshot") or {}).get("watchlist") or []
    if watchlist:
        log_phi_read(
            request=request,
            user=user,
            action_type="ward_brief_viewed",
            summary_message=(
                f"Ward brief viewed ({len(watchlist)} on the watchlist) for "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={
                "result_count": len(watchlist),
                "patient_ids": [w.get("patient_id") for w in watchlist],
            },
        )

    return brief


_GET_AI_POSTURE_USER_DEFAULT = Depends(get_current_user)


@router.get("/ai-posture")
def get_ai_posture(user: dict = _GET_AI_POSTURE_USER_DEFAULT):
    """Client-visible AI governance posture (no secrets): the local model the
    UI names as provenance, and whether on-appliance transcription can run."""
    from services.ai_governance import ai_posture

    return ai_posture()


class SagePrefetchRequest(BaseModel):
    patient_id: str = Field(min_length=1, strict=True)
    hint: str | None = None


def _audit_prefetch_patient(request: Request, user: dict, patient_id: str) -> None:
    """Verify existence and audit the authorized chart warm off the loop."""
    from permissions import log_phi_read

    with db_session() as db:
        exists = db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    log_phi_read(
        request=request,
        user=user,
        action_type="sage_context_prefetched",
        summary_message=f"Sage context prefetched for patient {patient_id}",
        patient_id=patient_id,
    )


_SAGE_PREFETCH_USER_DEFAULT = Depends(require_permission("sage.query", "patient.read_all"))


@router.post("/prefetch")
async def sage_prefetch(
    request: Request,
    body: SagePrefetchRequest,
    user: dict = _SAGE_PREFETCH_USER_DEFAULT,
):
    """Speculative context prefetch: the client calls this when a clinician
    OPENS a patient, so retrieval (facts + guideline RAG) leaves the TTFT
    critical path. Re-keys the same warm path a critical Vigil alert uses
    (nervous_system_triggers → sage_decision_cache), with a shorter TTL since
    a UI navigation is a weaker signal than a critical alert.

    This caller-named chart must pass the same permission and department/
    facility boundary as a patient read before the system warm path can touch
    its cache or assemble any facts. A cache hit never bypasses authorization.
    """
    from permissions import enforce_patient_scope

    patient_id = body.patient_id
    await run_db(enforce_patient_scope, user, patient_id)
    await run_db(_audit_prefetch_patient, request, user, patient_id)
    from services.nervous_system_triggers import warm_patient_context

    ok = await warm_patient_context(
        patient_id,
        query_hint=body.hint or "",
        ttl_minutes=5,
        reason="ui_patient_opened",
    )
    return {"status": "warmed" if ok else "skipped", "patient_id": patient_id}
