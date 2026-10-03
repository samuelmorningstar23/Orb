"""Tests for Sage's conversational continuity, refusal voice, and pinned scores.

These pin the four accuracy defects the 70-turn evaluation of 2026-08-02 found
(docs/perf/SAGE_EVAL_2026-08-02.md). All four were in prompt assembly, not in
the model:

  (a) A refusal that stated something FALSE about the system — "I do not have
      access to real-time patient medical records… would require direct
      connection to her electronic health record". Orb IS that record system.
      The turn reached the model with an EMPTY context because the relevance
      early-exit ran before the no-such-patient grounding was computed.

  (b) An impossible sub-score — "SpO2 80.1% — 4 points", where 3 is the NEWS2
      per-parameter maximum. The aggregate was pinned; the decomposition was
      not, so the model derived it.

  (c) Over-refusal of hypothetical clinical questions ("What dose?" → "I am an
      AI and cannot determine specific dosages").

  (d) Total loss of the thread on follow-ups, including summarising its own
      system prompt when asked what had been discussed.

(c) and (d) share ONE root cause, which is what most of this file is about:
_build_chat_messages dropped conversation history for scope "simple", and
"simple" is the scope every short follow-up lands in.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import sage_service
from services.sage_service import (
    SAGE_BROAD_SYSTEM_PROMPT,
    SAGE_FAST_PATIENT_PROMPT,
    SAGE_FAST_SIMPLE_PROMPT,
    SAGE_SIMPLE_SYSTEM_PROMPT,
    SAGE_SYSTEM_PROMPT,
    _build_chat_messages,
    _classify_risk_level,
    _history_turn_text,
    _is_followup_turn,
    _news2_component_line,
    _risk_classification_text,
)


def _history_msgs(messages):
    """The replayed conversation turns — everything between the system
    block(s) and the live user turn."""
    return [m for m in messages[:-1] if m["role"] in ("user", "assistant")]


# ══════════════════════════════════════════════════════════════════════
# (d) CONTINUITY — the follow-up detector
# ══════════════════════════════════════════════════════════════════════

# Every one of these is a real turn from the evaluation that lost its thread.
FOLLOWUPS = [
    "What dose?",                                 # G3 turn 11
    "What if they're penicillin allergic?",       # G3 turn 17
    "Summarise the key points we've discussed.",  # G3 turn 20
    "Why is it that high?",                       # G4 turn 3
    "How much fluid exactly?",
    "How is that different from severe sepsis?",
    "Does it affect mortality?",
    "How long do I continue it?",
    "How do I know it's working?",
    "Why that one over dopamine?",
    "And for renal failure?",
    "What about steroids?",
    "You said norepinephrine — why?",
    "Recap what we covered.",
]

# Self-contained questions. These must NOT drag a previous patient or topic
# into an unrelated answer — that contamination is what the original
# scope != "simple" guard existed to prevent, and it still must not happen.
SELF_CONTAINED = [
    "What is the mechanism of action of aspirin?",
    "What is Rohit Deshpande's latest blood pressure?",
    "What are the CURB-65 criteria for community acquired pneumonia?",
    "Give me a summary of Anil Bhatnagar's admission today please",
    "What are the indications for renal replacement therapy in AKI?",
]


@pytest.mark.parametrize("query", FOLLOWUPS)
def test_followup_turns_are_recognised(query):
    assert _is_followup_turn(query) is True, f"{query!r} would lose its thread"


@pytest.mark.parametrize("query", SELF_CONTAINED)
def test_self_contained_turns_are_not_treated_as_followups(query):
    assert _is_followup_turn(query) is False, f"{query!r} would inherit stale context"


@pytest.mark.parametrize("query", ["", "   ", None])
def test_followup_detector_is_safe_on_empty_input(query):
    assert _is_followup_turn(query) is False


# ══════════════════════════════════════════════════════════════════════
# (d) CONTINUITY — history actually reaches the model
# ══════════════════════════════════════════════════════════════════════

THREAD = [
    {"role": "user", "content": "Which vasopressor would you start in septic shock?"},
    {"role": "assistant", "content": "NOACT| Norepinephrine is first line, targeting a MAP of at least 65 mmHg."},
    {"role": "user", "content": "What about steroids?"},
    {"role": "assistant", "content": "NOACT| Hydrocortisone if shock persists despite fluids and vasopressors."},
]


@pytest.mark.parametrize("query", [
    "What dose?",
    "Why is it that high?",
    "What if they're penicillin allergic?",
    "Summarise the key points we've discussed.",
])
def test_simple_scope_followup_now_carries_the_thread(query):
    """THE REGRESSION THIS FILE EXISTS FOR.

    Before 2026-08-03 every one of these reached the model as system prompt +
    the bare question, because scope "simple" dropped history unconditionally.
    "Summarise the key points we've discussed" then had nothing to summarise
    but the system prompt — which is exactly what it summarised.
    """
    msgs = _build_chat_messages(query, "", THREAD, "simple")
    replayed = _history_msgs(msgs)
    assert replayed, f"{query!r} reached the model with no conversation"
    assert any("Norepinephrine" in m["content"] or "Hydrocortisone" in m["content"]
               for m in replayed)
    assert msgs[-1] == {"role": "user", "content": query}


def test_self_contained_simple_turn_still_gets_no_history():
    msgs = _build_chat_messages(
        "What is the mechanism of action of aspirin?", "", THREAD, "simple")
    assert _history_msgs(msgs) == []


@pytest.mark.parametrize("scope", ["patient", "clinical", "broad", "aggregate"])
def test_non_simple_scopes_keep_history_unconditionally(scope):
    msgs = _build_chat_messages(
        "What is the mechanism of action of aspirin?", "", THREAD, scope)
    assert len(_history_msgs(msgs)) == len(THREAD)


def test_simple_scope_history_is_bounded_for_the_2048_token_window():
    """simple runs at num_ctx 2048; an unbounded tail would evict the context."""
    long_thread = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 4000}
        for i in range(12)
    ]
    msgs = _build_chat_messages("What dose?", "", long_thread, "simple")
    replayed = _history_msgs(msgs)
    assert len(replayed) <= 4
    assert all(len(m["content"]) <= 600 for m in replayed)


def test_no_history_is_still_fine():
    for hist in (None, []):
        msgs = _build_chat_messages("What dose?", "", hist, "simple")
        assert _history_msgs(msgs) == []


# ══════════════════════════════════════════════════════════════════════
# (d) CONTINUITY — what a replayed turn looks like
# ══════════════════════════════════════════════════════════════════════

RAW_PATIENT_ANSWER = (
    'NOACT| His **NEWS2** is 13.<details><display_mode>concise</display_mode>'
    '<patient_facts_summary>67M, COPD exacerbation, GM-102.</patient_facts_summary>'
    '<caveats>None.</caveats><cited_sources>[]</cited_sources></details>'
)


def test_history_replay_drops_the_details_scaffolding():
    out = _history_turn_text(RAW_PATIENT_ANSWER, 1000, keep_marker=True)
    assert "<details>" not in out
    assert "patient_facts_summary" not in out
    assert "**NEWS2** is 13." in out


def test_history_replay_keeps_the_marker_on_the_fast_lanes():
    """LOAD-BEARING. Measured on medgemma:27b, 5 samples per arm, same
    question and history: marker emitted 5/5 with no history, 0/5 when history
    was replayed with the marker stripped, 5/5 with it preserved. The marker is
    the model-declared mutation gate; without an in-context precedent the model
    copies its own most recent prose turn and drops the contract."""
    out = _history_turn_text(RAW_PATIENT_ANSWER, 1000, keep_marker=True)
    assert out.startswith("NOACT|")


def test_history_replay_strips_the_marker_on_the_prose_lanes():
    """broad/aggregate have no marker contract, and their streamer does not
    strip one — a replayed NOACT| teaches the model to show it to the user."""
    out = _history_turn_text(RAW_PATIENT_ANSWER, 1000, keep_marker=False)
    assert not out.startswith("NOACT|")
    assert "NEWS2" in out


def test_build_chat_messages_applies_the_marker_rule_per_scope():
    for scope in ("simple", "clinical", "patient"):
        msgs = _build_chat_messages("What dose?", "", [
            {"role": "assistant", "content": RAW_PATIENT_ANSWER}], scope)
        assert _history_msgs(msgs)[0]["content"].startswith("NOACT|"), scope
    for scope in ("broad", "aggregate"):
        msgs = _build_chat_messages("What dose?", "", [
            {"role": "assistant", "content": RAW_PATIENT_ANSWER}], scope)
        assert not _history_msgs(msgs)[0]["content"].startswith("NOACT|"), scope


def test_user_turns_never_gain_a_marker():
    msgs = _build_chat_messages("What dose?", "", [
        {"role": "user", "content": "What about steroids?"}], "simple")
    assert _history_msgs(msgs)[0]["content"] == "What about steroids?"


def test_history_replay_unwraps_the_legacy_xml_contract():
    raw = ("```xml\n<action_intent>None.</action_intent>"
           "<concise_answer>Norepinephrine is first line.</concise_answer>"
           "<caveats>None.</caveats>```")
    out = _history_turn_text(raw, 1000, keep_marker=True)
    assert out.strip() == "Norepinephrine is first line."


def test_history_replay_is_safe_on_empty_and_scaffolding_only_turns():
    assert _history_turn_text("", 100) == ""
    assert _history_turn_text(None, 100) == ""
    assert _history_turn_text("NOACT|", 100, keep_marker=True) == ""


# ══════════════════════════════════════════════════════════════════════
# (d)/safety — an elliptical follow-up is classified with its referent
# ══════════════════════════════════════════════════════════════════════

STROKE_THREAD = [
    {"role": "user", "content": "Talk me through thrombolysis in acute ischaemic stroke."},
    {"role": "assistant", "content": "NOACT| Alteplase within 4.5 hours of onset once haemorrhage is excluded."},
]
BENIGN_THREAD = [
    {"role": "user", "content": "What are the CURB-65 criteria?"},
    {"role": "assistant", "content": "NOACT| Confusion, urea, respiratory rate, blood pressure, age 65."},
]


def test_high_risk_referent_is_inherited_by_the_followup():
    """"What dose?" carries no high-risk token of its own. While follow-ups
    were refused this did not matter; now that they are answered, the turn
    would otherwise lose its escalation badge."""
    assert _classify_risk_level("What dose?") == "standard"
    assert _classify_risk_level(
        _risk_classification_text("What dose?", STROKE_THREAD)) == "high"


def test_risk_is_not_inherited_from_a_benign_thread():
    assert _classify_risk_level(
        _risk_classification_text("What dose?", BENIGN_THREAD)) == "standard"


def test_a_self_contained_question_never_inherits_thread_risk():
    q = "What is the mechanism of action of aspirin?"
    assert _risk_classification_text(q, STROKE_THREAD) == q


def test_risk_classification_text_is_safe_without_history():
    assert _risk_classification_text("What dose?", None) == "What dose?"
    assert _risk_classification_text("What dose?", []) == "What dose?"


# ══════════════════════════════════════════════════════════════════════
# (b) PINNED SUB-SCORES
# ══════════════════════════════════════════════════════════════════════

def test_news2_component_line_renders_chart_shaped_values():
    # REALs off the vitals row must not reach the model as "35.0" beside a
    # chart that says 35.
    assert _news2_component_line(
        {"parameter": "Respiratory rate", "value": 35.0, "points": 3}) == "Respiratory rate 35 = 3"
    # Since 2026-10-02 NEWS2 scores the value AS CHARTED, so the engine returns
    # 80 for a raw 80.1 and the line prints the number that earned the points
    # (tests/test_news2_charted_value.py).
    assert _news2_component_line(
        {"parameter": "SpO2", "value": 80.1, "points": 3}) == "SpO2 80 = 3"
    assert _news2_component_line(
        {"parameter": "Temperature", "value": 35.0, "points": 3}) == "Temperature 35.0 = 3"
    assert _news2_component_line(
        {"parameter": "Temperature", "value": 38.05, "points": 1}) == "Temperature 38.1 = 1"
    assert _news2_component_line(
        {"parameter": "Supplemental O2", "value": True, "points": 2}) == "Supplemental O2 yes = 2"
    assert _news2_component_line(
        {"parameter": "Consciousness (ACVPU)", "value": "confused", "points": 3}
    ) == "Consciousness (ACVPU) confused = 3"


def test_news2_component_line_survives_a_missing_value():
    assert _news2_component_line({"parameter": "Temperature", "points": 0}) == "Temperature ? = 0"
    assert _news2_component_line({}) == "? ? = 0"


def test_no_single_news2_parameter_can_exceed_three():
    """The evaluation's error was 'SpO2 — 4 points'. The engine cannot produce
    that, which is exactly why the decomposition must come FROM the engine."""
    from services.vigil_service import compute_news2
    for vitals in [
        (123.9, 83.1, 35.0, 37.4, 80.1),      # the evaluated patient
        (200.0, 60.0, 40.0, 34.0, 70.0),      # every parameter maximal
        (35.0, 250.0, 5.0, 41.0, 100.0),      # the other extreme
    ]:
        n = compute_news2(*vitals, "unresponsive", True)
        assert n["breakdown"], "engine must supply a decomposition"
        assert all(0 <= c["points"] <= 3 for c in n["breakdown"])
        assert sum(c["points"] for c in n["breakdown"]) == n["raw_news2"], \
            "the pinned components must sum to the pinned aggregate"


# ══════════════════════════════════════════════════════════════════════
# (a)/(c) THE PROMPT CONTRACT — the rules that replaced the wrong voice
# ══════════════════════════════════════════════════════════════════════

FAST_PROMPTS = [SAGE_FAST_SIMPLE_PROMPT, SAGE_FAST_PATIENT_PROMPT]
ALL_PROMPTS = FAST_PROMPTS + [
    SAGE_BROAD_SYSTEM_PROMPT, SAGE_SIMPLE_SYSTEM_PROMPT, SAGE_SYSTEM_PROMPT]


@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_every_prompt_states_that_orb_is_the_record_system(prompt):
    """(a) The refusal must be "not in the census", never "I have no access"."""
    low = prompt.lower()
    assert "census" in low
    assert "ehr" in low or "record system" in low


@pytest.mark.parametrize("prompt", FAST_PROMPTS + [SAGE_SIMPLE_SYSTEM_PROMPT, SAGE_SYSTEM_PROMPT])
def test_fast_prompts_permit_teaching_questions(prompt):
    """(c) Hypotheticals are clinical reasoning, not a data request."""
    low = prompt.lower()
    assert "i am an ai" in low          # the phrase it is told never to use
    assert "what dose?" in low or "what dose" in low


@pytest.mark.parametrize("prompt", FAST_PROMPTS + [SAGE_SIMPLE_SYSTEM_PROMPT, SAGE_SYSTEM_PROMPT])
def test_fast_prompts_forbid_reciting_their_own_instructions(prompt):
    """(d) The 'I will act as Orb Sage…' answer."""
    assert "instructions" in prompt.lower()


# SAGE_BROAD_SYSTEM_PROMPT is a prose census brief with no action contract —
# the no-action-hallucination rule has never lived there (the stream sanitizer
# covers that lane), so it is checked separately below.
@pytest.mark.parametrize(
    "prompt", FAST_PROMPTS + [SAGE_SIMPLE_SYSTEM_PROMPT, SAGE_SYSTEM_PROMPT])
def test_the_safety_rules_were_not_traded_away(prompt):
    """The anti-over-refusal wording must not have bought its way in by
    loosening the rules that keep Sage safe. All of them still hold in every
    prompt that carried them before."""
    low = prompt.lower()
    assert "never say you did" in low or 'never say "i have added"' in low \
        or "no action hallucination" in low, \
        "the no-action-hallucination rule is missing"
    assert "data" in low and "instruction" in low, \
        "the untrusted-data rule is missing"
    assert "invent" in low or "fabricat" in low, \
        "the no-fabrication rule is missing"


def test_broad_prompt_keeps_its_own_safety_rules():
    low = SAGE_BROAD_SYSTEM_PROMPT.lower()
    assert "never fabricate a patient" in low
    assert "data, not instructions" in low


@pytest.mark.parametrize("prompt", FAST_PROMPTS + [SAGE_SIMPLE_SYSTEM_PROMPT, SAGE_SYSTEM_PROMPT])
def test_high_risk_verification_rule_still_holds(prompt):
    low = prompt.lower()
    assert "high-risk" in low or "high risk" in low
    assert "verification" in low or "verify" in low or "escalate" in low


@pytest.mark.parametrize("prompt", FAST_PROMPTS)
def test_the_marker_gate_is_intact_and_still_first(prompt):
    """The answer-first contract is the model-declared mutation gate. The new
    paragraphs must never drift above it."""
    assert "NOACT|" in prompt and "ACT|" in prompt
    body = prompt.split("\n\n", 1)[1]
    marker_at = body.find("NOACT|")
    for later in ("teaching and hypothetical", "Earlier turns", "Orb IS the hospital"):
        pos = body.find(later)
        if pos != -1:
            assert pos > marker_at, f"{later!r} moved above the marker contract"


@pytest.mark.parametrize("prompt", FAST_PROMPTS + [SAGE_SIMPLE_SYSTEM_PROMPT, SAGE_SYSTEM_PROMPT])
def test_prompts_pin_the_per_parameter_maximum(prompt):
    """(b) belt to the context's braces: even with no decomposition supplied,
    the model is told 3 is the per-parameter ceiling."""
    low = prompt.lower()
    assert "per-parameter" in low
    assert "3" in prompt


def _shared_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def test_the_interactive_prompts_share_one_long_prefix():
    """THE LATENCY ASSET (2026-08-04). Ollama's KV reuse is prefix-anchored, so
    what a scope flip costs is not the prompt's length — it is the length of the
    part that DIFFERS. These two constants shared 41 characters, so every flip
    between a general question and a patient question diverged 10 tokens in and
    re-prefilled everything. Measured on the appliance, 4/4 trials with the arms
    alternated: median flip prefill 9,680 ms before vs 9,471 ms after, for a
    prompt that is 92 tokens LONGER — about 0.8 s recovered per flip.

    This test pins the STRUCTURE, not the timing, because prose edits are what
    silently destroy it: a rule reworded in only one of the two constants, or a
    scope clause moved up into the body, puts the divergence point back near the
    top and gives the 0.8 s back with nothing to show for it."""
    shared = _shared_prefix_len(SAGE_FAST_SIMPLE_PROMPT, SAGE_FAST_PATIENT_PROMPT)
    assert shared > 2000, (
        f"the interactive prompts share only {shared} chars — a scope flip now "
        "re-prefills the system prompt again")
    for name, prompt in (("simple", SAGE_FAST_SIMPLE_PROMPT),
                         ("patient", SAGE_FAST_PATIENT_PROMPT)):
        tail = len(prompt) - shared
        assert tail // 4 < 80, f"{name} scope tail is {tail // 4} tokens — keep it short"


def test_the_shared_body_stays_within_its_latency_budget():
    """The prefix is only free once it is warm; on a cold prefix its length IS
    the floor on time-to-first-word (~160 tok/s, so each 100 tokens is ~0.63 s).
    It was ~478 tokens (simple) / ~536 (patient) as two separate prompts and is
    ~596 shared + ~45 per scope now, so the simple lane carries ~167 tokens
    (~1.0 s) more on a genuinely cold prefix. main.py's warmup sends this same
    body every 5 minutes, which is what makes the cold case the exception — and
    the warmup now primes the patient lane too. The ceiling is a tripwire, not a
    target: if a rule must be added, take one out."""
    shared = _shared_prefix_len(SAGE_FAST_SIMPLE_PROMPT, SAGE_FAST_PATIENT_PROMPT)
    assert shared // 4 < 650
    assert len(SAGE_FAST_SIMPLE_PROMPT) // 4 < 700
