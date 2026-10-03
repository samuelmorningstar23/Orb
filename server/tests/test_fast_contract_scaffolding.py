"""The output contract must never be printed to a clinician.

WHY THIS FILE EXISTS. The 27B lane closes its structured sections inside
``<details>…</details>``, and both the streamer and the buffered parser cut the
visible answer at that literal. The 4B "fast" lane — the one the demo uses as
its speed showcase — frequently does NOT emit ``<details>`` at all: it answers,
then emits the section tags loose. With nothing but ``<details>`` to cut on, the
entire contract was appended to the answer and rendered in the chat bubble,
right down to the contract's own placeholder:

    The classic ECG findings of hyperkalemia include peaked T waves …
    <display_mode> concise <patient_facts_summary> None <local_policy_guidance>
    None … <cited_sources> ["DOC-ID"]

Observed in the browser 2026-08-08 with Thinking OFF. Every fixture in
test_fast_contract_marker.py wraps the sections in ``<details>``, so the whole
existing suite was green while this shipped — a green suite cannot see a failure
class its fixtures do not express.

The cases below are the ones that matter: sections WITHOUT the wrapper, on both
the streaming and the buffered path, plus the two things a fix like this can
easily break — a clinical "<" must still stream, and a reply must never render
blank.
"""
import pytest

from routes.sage_routes import (
    _CONTRACT_TAGS,
    MarkerAnswerStreamer,
    SentenceStreamSanitizer,
    _contract_tag_prefix_start,
    _strip_contract_scaffolding,
    _strip_forbidden_streaming,
    parse_fast_response,
)

# ── The SAME failure class, one level worse: no angle brackets at all ─────────
# Observed in the browser 2026-08-08 06:20, DOC001, Thinking OFF, question
# "First-line management of an acute severe asthma attack in an adult?". The 4B
# model answered correctly in markdown and then wrote the detail contract as
# ORDINARY PROSE — label, display_mode enum, and three empty placeholders, with
# not a single "<" anywhere:
#
#   … Consider intubation and mechanical ventilation if the patient deteriorates.
#   Details: concise None None None The primary goals are bronchodilation and …
#
# Everything above keys on "<tag>", so nothing cut this and the clinician read
# "concise None None None" as if it were clinical text. The fixtures in this
# file all had brackets, so this file was green while that shipped — the same
# lesson as the file header, applied to itself.
BARE_SECTIONS = (
    "\n\nDetails: concise None None None The primary goals are bronchodilation "
    "and reducing airway inflammation. This inf"
)

ANSWER = ("The classic ECG findings of hyperkalemia include peaked T waves, "
          "a widened QRS, and loss of P waves.")

# The 4B lane's actual shape: no <details>, sections emitted loose, and the
# contract's literal placeholder in cited_sources.
LOOSE_SECTIONS = (
    ' <display_mode> concise <patient_facts_summary> None '
    '<local_policy_guidance> None <external_guidance> None '
    '<clinical_assessment> Peaked T waves appear earliest. '
    '<caveats> Confirm with a repeat ECG. <escalation> false '
    '<cited_sources> ["DOC-ID"]'
)

WRAPPED_SECTIONS = ('<details><display_mode>concise</display_mode>'
                    '<caveats>Confirm with a repeat ECG.</caveats>'
                    '<cited_sources>["DOC-1"]</cited_sources></details>')


def _stream(text: str, chunk: int = 5, mutation_locked: bool = False) -> str:
    s = MarkerAnswerStreamer(
        SentenceStreamSanitizer(_strip_forbidden_streaming), mutation_locked)
    out = "".join(s.feed(text[i:i + chunk]) or "" for i in range(0, len(text), chunk))
    return out + (s.finalize() or "")


# ── The live bug ──────────────────────────────────────────────────────────────

def test_buffered_parse_drops_loose_sections():
    parsed = parse_fast_response(f"NOACT| {ANSWER}{LOOSE_SECTIONS}")
    assert parsed["answer"] == ANSWER
    for leak in ("<display_mode>", "display_mode", "cited_sources", "DOC-ID",
                 "patient_facts_summary"):
        assert leak not in parsed["answer"], f"{leak!r} reached the clinician"


def test_stream_drops_loose_sections():
    streamed = _stream(f"NOACT| {ANSWER}{LOOSE_SECTIONS}")
    assert "display_mode" not in streamed
    assert "DOC-ID" not in streamed
    assert "peaked T waves" in streamed


@pytest.mark.parametrize("chunk", [1, 3, 7, 13, 64, 4096])
def test_stream_drops_loose_sections_at_every_chunk_size(chunk):
    """Tag boundaries must not depend on how the tokens happen to arrive."""
    streamed = _stream(f"NOACT| {ANSWER}{LOOSE_SECTIONS}", chunk=chunk)
    assert "display_mode" not in streamed
    assert "cited_sources" not in streamed


def test_loose_sections_are_still_harvested_for_the_details_panel():
    """Cutting them out of the prose must not throw the evidence away."""
    parsed = parse_fast_response(f"NOACT| {ANSWER}{LOOSE_SECTIONS}")
    assert parsed["details"]["clinical_assessment"].startswith("Peaked T waves")
    assert parsed["details"]["caveats"].startswith("Confirm")
    assert parsed["details"]["cited_sources"] == '["DOC-ID"]'


# ── The bracket-less variant of the same bug ─────────────────────────────────

def test_buffered_parse_drops_bare_prose_sections():
    parsed = parse_fast_response(f"NOACT| {ANSWER}{BARE_SECTIONS}")
    assert parsed["answer"] == ANSWER
    for leak in ("Details:", "concise", "None None None"):
        assert leak not in parsed["answer"], f"{leak!r} reached the clinician"


def test_strip_contract_scaffolding_drops_bare_prose_sections():
    """The untagged-output rescue in sage_routes uses this directly — it is the
    path that actually rendered the leak."""
    stripped = _strip_contract_scaffolding(f"{ANSWER}{BARE_SECTIONS}")
    assert stripped == ANSWER


@pytest.mark.parametrize("chunk", [1, 3, 7, 13, 64, 4096])
def test_stream_drops_bare_prose_sections_at_every_chunk_size(chunk):
    streamed = _stream(f"NOACT| {ANSWER}{BARE_SECTIONS}", chunk=chunk)
    assert "Details:" not in streamed
    assert "None None None" not in streamed
    assert "peaked T waves" in streamed


def test_bare_display_mode_token_is_cut_even_without_the_label():
    """`display_mode` is the contract's own field name — never clinical English."""
    parsed = parse_fast_response(
        f"NOACT| {ANSWER} display_mode concise patient_facts_summary None"
    )
    assert parsed["answer"] == ANSWER


# ── The same class again, one shape further out (2026-09-07) ─────────────────
# The transcripts above all put the display_mode ENUM straight after the label
# ("Details: concise None…"). The 4B also writes it with the label on its own
# line and the sections as TAGS underneath:
#
#     Hypoxia refers to a condition where there isn't enough oxygen ...
#     Details:
#     <display_mode> concise
#     <patient_facts_summary> None
#
# The tag rule cut at the "<" and the bare rule needed the enum, so "Details:"
# survived as the last line of the clinical answer. It reached the clinician
# through the BUFFERED path, which until 2026-09-07 was the only path the fast
# lane had (its streaming gate never opened — see
# test_fast_contract_marker_source.py). Now that the lane streams, the same text
# goes out live, which is what turned a quiet defect into a visible one.
LABEL_THEN_TAGS = ("\nDetails:\n<display_mode> concise\n"
                   "<patient_facts_summary> None\n<cited_sources> [\"\"]\n")


def test_buffered_parse_drops_a_label_that_sits_in_front_of_the_tags():
    parsed = parse_fast_response(f"NOACT| {ANSWER}{LABEL_THEN_TAGS}")
    assert parsed["answer"] == ANSWER
    assert "Details" not in parsed["answer"]


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 13, 64, 4096])
def test_stream_drops_a_label_that_sits_in_front_of_the_tags(chunk):
    streamed = _stream(f"NOACT| {ANSWER}{LABEL_THEN_TAGS}", chunk=chunk)
    assert "Details" not in streamed
    assert "peaked T waves" in streamed


def test_the_label_rule_still_needs_a_contract_tag_behind_it():
    """The anchor is what keeps this narrow. A colon after the word is not
    enough; a real contract tag has to follow."""
    text = ("NOACT| Admit under medicine. Details: the family has been updated "
            "and the referral is in the notes.")
    parsed = parse_fast_response(text)
    assert parsed["answer"].endswith("in the notes.")
    assert "the family has been updated" in _stream(text, chunk=3)


# ── Things this fix must not break ────────────────────────────────────────────

def test_the_word_details_in_clinical_prose_is_not_cut():
    """"details" is ordinary English. Only the contract's own shape may cut."""
    text = ("NOACT| Admit under medicine. The referral details are in the "
            "handover note; more details follow on the ward round.")
    parsed = parse_fast_response(text)
    assert parsed["answer"].endswith("on the ward round.")
    assert "details are in the handover note" in _stream(text, chunk=3)


def test_a_bare_contract_only_reply_never_renders_blank():
    parsed = parse_fast_response(f"NOACT|{BARE_SECTIONS}")
    assert parsed["answer"] == "" or "None None None" not in parsed["answer"]
    assert _strip_contract_scaffolding(BARE_SECTIONS) == ""



def test_wrapped_sections_still_parse():
    parsed = parse_fast_response(f"NOACT| {ANSWER}{WRAPPED_SECTIONS}")
    assert parsed["answer"] == ANSWER
    assert parsed["details"]["cited_sources"] == '["DOC-1"]'
    assert parsed["details"]["caveats"] == "Confirm with a repeat ECG."


def test_clinical_less_than_still_streams():
    """A '<' in clinical prose is not a tag and must not stall or be eaten."""
    text = "NOACT| Potassium < 3.5 mmol/L is hypokalaemia and needs replacement."
    streamed = _stream(text, chunk=3)
    assert "< 3.5 mmol/L" in streamed
    assert parse_fast_response(text)["answer"].endswith("needs replacement.")


def test_a_contract_only_reply_never_renders_blank():
    """No prose before the scaffolding: the clinician must still see something.

    A blank answer is not a safe outcome, it is a silent one — the buffered
    path's whole fail-open rule.
    """
    parsed = parse_fast_response(f"NOACT|{LOOSE_SECTIONS}")
    assert parsed["details"], "sections must still be harvested"
    # The prose is empty by construction; what matters is that the caller's
    # untagged-output rescue can still find text. _strip_contract_scaffolding
    # returning '' is the signal for that path.
    assert _strip_contract_scaffolding(LOOSE_SECTIONS) == ""


def test_prefix_holdback_only_triggers_on_real_tag_prefixes():
    assert _contract_tag_prefix_start("peaked T waves <displ") >= 0
    assert _contract_tag_prefix_start("peaked T waves </cited_sou") >= 0
    assert _contract_tag_prefix_start("potassium < 3.5") == -1
    assert _contract_tag_prefix_start("no angle bracket here") == -1


def test_repeated_tag_mid_answer_is_scrubbed():
    parsed = parse_fast_response(
        "NOACT| First line. <escalation> second line still clinical."
    )
    assert "<escalation>" not in parsed["answer"]


# ── The TAG rule, across every tag and every wrapping the model uses ─────────
# `</?(?:tag)\s*>` recognised "<caveats >" but not "< caveats>", and none of the
# markdown forms — "<**escalation**>", "<_escalation_>", "</__caveats__>" all
# walked past BOTH the cut and the scrub and printed into the chat bubble
# (measured 2026-08-11). The bracket-less rule had already been made
# emphasis-aware; this is the same widening on the bracketed one, and the two
# now agree about what a contract tag looks like.
#
# Parameterised over EVERY tag rather than a favourite one: the earlier leak was
# invisible because the fixtures all used `display_mode`, which the bracket-less
# rule happened to catch anyway. `escalation` and `caveats` had no such backstop.
TAG_WRAPPINGS = (
    "<{t}>", "</{t}>", "<**{t}**>", "<_{t}_>", "<__{t}__>", "<*{t}*>",
    "<***{t}***>", "</__{t}__>", "<{t} >", "< {t}>", "</ ***{t}*** >",
)


@pytest.mark.parametrize("tag", _CONTRACT_TAGS)
@pytest.mark.parametrize("wrapping", TAG_WRAPPINGS)
def test_every_tag_in_every_wrapping_is_cut_from_the_buffered_answer(tag, wrapping):
    raw = f"NOACT| {ANSWER}\n{wrapping.format(t=tag)} value"
    answer = parse_fast_response(raw)["answer"]
    assert "peaked T waves" in answer, "the clinical answer was lost"
    for residue in ("<", ">", "*", "_"):
        assert residue not in answer, (
            f"{wrapping.format(t=tag)!r} reached the clinician: {answer[-40:]!r}")


@pytest.mark.parametrize("tag", _CONTRACT_TAGS)
def test_every_tag_is_cut_from_the_wire_at_every_chunk_size(tag):
    """The hold-back has to recognise the same wrappings the cut does. It did
    not: it tested "escalation*" against the tag names, failed, and released
    "<**escalation" one character before the cut fired."""
    for wrapping in TAG_WRAPPINGS:
        raw = f"NOACT| {ANSWER}\n{wrapping.format(t=tag)} value"
        replayed = parse_fast_response(raw)["answer"].split()
        for chunk in range(1, len(raw) + 1):
            streamed = _stream(raw, chunk=chunk)
            for residue in ("<", ">", "*", "_"):
                assert residue not in streamed, (
                    f"{wrapping.format(t=tag)!r} leaked at chunk={chunk}: "
                    f"{streamed[-40:]!r}")
            assert streamed.split() == replayed, (
                f"{wrapping.format(t=tag)!r} drifted from the replay at "
                f"chunk={chunk}")


@pytest.mark.parametrize("wrapped,closing", (
    ("<cited_sources>", "</cited_sources>"),
    ("<**cited_sources**>", "</**cited_sources**>"),
    ("<_cited_sources_>", "</_cited_sources_>"),
    ("<__cited_sources__>", "</__cited_sources__>"),
    ("< cited_sources >", "</ cited_sources >"),
    ("<***cited_sources***>", None),          # loose form, never closed
))
def test_a_tag_the_cut_removes_is_still_harvested_for_the_details_panel(wrapped, closing):
    """Cutting a tag you cannot harvest is not a fix, it is a better-looking
    loss: the scaffolding leaves the prose and the CITATION leaves with it,
    which is the quietest way this file can fail. The harvester tracks the cut."""
    tail = f"{wrapped}[\"EXT-K-001\"]{closing or ''}"
    parsed = parse_fast_response(f"NOACT| {ANSWER}\n{tail}")

    assert "peaked T waves" in parsed["answer"]
    for residue in ("<", ">", "*", "_"):
        assert residue not in parsed["answer"], f"{wrapped!r} reached the clinician"
    assert parsed["details"].get("cited_sources") == '["EXT-K-001"]', (
        f"the evidence was dropped for {wrapped!r} — clean answer, no citation")


@pytest.mark.parametrize("text", (
    "Treat if K < 3.5 mmol/L or SpO2 < 92% on air.",
    "Escalate when HR < 40 or > 130.",
    "Use < 500 mL boluses.",
    "Check 5 < 6 and 7 > 6 before dosing.",
))
def test_the_tag_widening_leaves_clinical_comparisons_alone(text):
    """The tag NAME must still match `_CONTRACT_TAGS` exactly, so a clinical "<"
    cannot reach any of this — and a threshold lost is the failure that harms."""
    raw = f"NOACT| {text}"
    assert parse_fast_response(raw)["answer"] == text
    for chunk in range(1, len(raw) + 1):
        assert _stream(raw, chunk=chunk).strip() == text, (
            f"a comparison was damaged at chunk={chunk}")


# ── The boundary itself: where the OPENING of a tag lands ────────────────────
# `_CONTRACT_CUT_RE` only matches a COMPLETE tag. While one is still arriving on
# the stream ("<display_mode", no ">" yet) the only pattern that fires is the
# bracket-less one — and it matches the LABEL, one character past the "<". The
# cut therefore landed one character late, the streamer had already put the "<"
# on the wire, and a streamed character cannot be taken back: the live answer
# ended on a dangling "<" that the buffered replay of the same reply did not
# have. The emphasised prose form ("**display_mode**") failed the same way two
# characters wider, because the hold-back released the "**" as ordinary prose and
# the cut then landed BEHIND `_emit_pos`, where it could no longer act.
#
# These sweep EVERY chunk size rather than sampling a handful. The original
# defect fired at chunk 1, 2 and 13 but not at 3, 5 or 7 — a fixed sample is a
# coin toss against a boundary bug, which is how it survived a green suite.
#
# The emphasis forms below are the SAME contract in markdown. `[*_]{0,2}\b…`
# read as if it covered them; it did not, because "_" is a word character and
# `\b` never holds between "_" and "d" — measured 2026-08-11, every underscore
# form leaked whole, in the buffered lane as well as the streamed one. The
# boundaries are emphasis-aware now (`_EMPH_OPEN`/`_EMPH_CLOSE`); these pin it.
HALF_ARRIVED_SHAPES = {
    "loose tag": f"NOACT| {ANSWER}\n<display_mode> concise <cited_sources> [\"D-1\"]",
    "closing tag": f"NOACT| {ANSWER}\n</display_mode> concise </cited_sources> [\"D-1\"]",
    "asterisk prose": f"NOACT| {ANSWER}\n\n**display_mode** concise None None",
    "asterisk tag": f"NOACT| {ANSWER}\n<**display_mode**> concise",
    "underscore prose": f"NOACT| {ANSWER}\n\n_display_mode_ concise None None",
    "double underscore prose": f"NOACT| {ANSWER}\n\n__display_mode__ concise None None",
    "underscore tag": f"NOACT| {ANSWER}\n<_display_mode_> concise",
    "asterisk details label": f"NOACT| {ANSWER}\n\n**Details:** concise None None",
    "underscore details label": f"NOACT| {ANSWER}\n\n__Details:__ concise None None",
}


@pytest.mark.parametrize("shape", sorted(HALF_ARRIVED_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_the_wire_never_keeps_the_opening_of_a_half_arrived_tag(shape):
    raw = HALF_ARRIVED_SHAPES[shape]
    for chunk in range(1, len(raw) + 1):
        streamed = _stream(raw, chunk=chunk)
        for residue in ("<", "*", "_"):
            assert residue not in streamed, (
                f"{shape}: contract opening {residue!r} reached the clinician "
                f"at chunk={chunk}: {streamed[-40:]!r}")
        assert "peaked T waves" in streamed, f"{shape}: answer lost at chunk={chunk}"


@pytest.mark.parametrize("shape", sorted(HALF_ARRIVED_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_the_wire_and_the_buffered_replay_agree_at_every_chunk_size(shape):
    """The lanes must draw the boundary in the same place. Whitespace policy
    differs between them (see _strip_forbidden_streaming); the WORDS may not."""
    raw = HALF_ARRIVED_SHAPES[shape]
    replayed = parse_fast_response(raw)["answer"].split()
    for chunk in range(1, len(raw) + 1):
        assert _stream(raw, chunk=chunk).split() == replayed, (
            f"{shape}: the wire drifted from the replay at chunk={chunk}")


def test_widening_the_boundary_never_eats_a_clinical_comparison():
    """The one thing this fix could break. "<" that is arithmetic carries a
    threshold, and losing a threshold is the failure that actually harms."""
    raw = ("NOACT| Treat if K < 3.5 mmol/L or SpO2 < 92% on air.\n"
           "<display_mode> concise <cited_sources> [\"EXT-K-002\"]")
    for chunk in range(1, len(raw) + 1):
        streamed = _stream(raw, chunk=chunk)
        assert "K < 3.5 mmol/L" in streamed, f"threshold lost at chunk={chunk}"
        assert "SpO2 < 92%" in streamed, f"threshold lost at chunk={chunk}"
        assert "display_mode" not in streamed


#: What the emphasis-aware boundaries must NOT start matching. `\b` used to do
#: this job by accident — "_" being a word character blocked the underscore
#: contract AND the underscore identifier alike. Replacing it with `(?<!\w)` /
#: `(?!\w)` outside the emphasis run keeps the identifiers out on purpose, and
#: this is the half of the widening that carries the real risk: cutting clinical
#: prose loses information a clinician needed.
NOT_THE_CONTRACT = (
    "Set final_display_mode to concise in the config.",
    "Check display_mode_override before rendering.",
    "The display_modes vary by ward.",
    "Full patient_details are attached to the referral.",
    "The referral details are in the handover note.",
    "The **details** of the referral are in the note.",
    "Discuss the details: the patient improved overnight.",
    "Use _italic_ emphasis and **bold** in the ward note.",
)


@pytest.mark.parametrize("text", NOT_THE_CONTRACT)
def test_the_emphasis_widening_does_not_cut_prose_or_identifiers(text):
    assert _strip_contract_scaffolding(text) == text, "clinical prose was cut"
    assert parse_fast_response(f"NOACT| {text}")["answer"] == text
    for chunk in (1, 3, 7, 64):
        assert _stream(f"NOACT| {text}", chunk=chunk).strip() == text, (
            f"the wire dropped prose at chunk={chunk}")


#: Generations that STOPPED inside the contract. `feed` defers a bare label that
#: ends exactly at the buffer's end (it cannot yet tell "display_mode" from
#: "display_modes"), so the label is still held when the stream ends — and
#: `finalize` is what releases held text. It therefore has to apply the cut
#: itself, or truncation becomes the one path that leaks the whole contract.
TRUNCATED_MID_CONTRACT = (
    "\n\ndisplay_mode",
    "\n\n**display_mode**",
    "\n\n__display_mode__",
    "\n<display_mode",
    "\n<__display_",
    "\n\nDetails: concise",
    "\n\n__Details:__ concise",
)


@pytest.mark.parametrize("tail", TRUNCATED_MID_CONTRACT)
@pytest.mark.parametrize("chunk", [1, 3, 7, 4096])
def test_a_generation_that_stops_mid_contract_releases_no_scaffolding(tail, chunk):
    streamed = _stream(f"NOACT| {ANSWER}{tail}", chunk=chunk)
    assert "peaked T waves" in streamed, "the clinical answer was lost"
    for leak in ("display_mode", "Details:", "details:"):
        assert leak.lower() not in streamed.lower(), (
            f"finalize released {leak!r}: {streamed[-40:]!r}")


#: Prose whose contract-shaped prefix is only decided by a character that has
#: not arrived yet. Both truncated the visible answer on the live lane while the
#: buffered replay kept the sentence whole — the boundary was being read off the
#: BUFFER's end rather than the reply's.
DECIDED_LATE = (
    # the word boundary: "display_mode" vs "display_modes"
    ("NOACT| The display_modes vary by ward.", "The display_modes vary by ward."),
    ("NOACT| Check display_mode_override first.", "Check display_mode_override first."),
    # the `$` in the Details: lookahead, satisfied by the buffer's edge
    ("NOACT| Admit today. Details: detailed notes follow in the chart.",
     "Admit today. Details: detailed notes follow in the chart."),
    ("NOACT| Details: concise summaries help the ward round.",
     "Details: concise summaries help the ward round."),
)


@pytest.mark.parametrize("raw,expected", DECIDED_LATE, ids=lambda v: v.split()[-1])
def test_a_boundary_that_needs_the_next_character_is_not_cut_early(raw, expected):
    for chunk in range(1, len(raw) + 1):
        assert _stream(raw, chunk=chunk).strip() == expected, (
            f"the answer was truncated at chunk={chunk}")
    assert parse_fast_response(raw)["answer"] == expected, "the replay lane disagrees"


def test_a_contract_only_reply_is_still_not_blanked_by_the_widening():
    """`_contract_cut`'s callers only honour a cut > 0. Moving a cut back onto
    the bracket must not turn a cut of 1 into a cut of 0 that silently drops the
    whole reply — a blank answer is not a safe outcome, it is a silent one."""
    assert _strip_contract_scaffolding("<display_mode> concise") != ""
    assert _strip_contract_scaffolding("<display_mode") != ""
    assert _strip_contract_scaffolding("**display_mode** concise") != ""
