"""WHO owns the fast contract's gate marker, and what changes when Orb does.

WHY THIS FILE EXISTS. `MarkerAnswerStreamer` is fail-closed on the marker:
nothing reaches the clinician until `NOACT|` or `ACT|` opens the reply. That
reads as a safety property and it is one, but it rests on an assumption nobody
had measured until 2026-09-07: that the marker is something the model would
otherwise have given us. On `medgemma:4b`, which is exactly what THINKING OFF
routes to, it is not (0 of 9 on Orb's own prompts, and 0 of 15 on a wider
re-run). On that lane "fail closed" is not a gate that occasionally closes, it
is a gate that never opens, and the clinician waits for the last token to read
the first word. The toggle sold as speed was the only lane that never streamed.

`assert_marker()` moves ownership of the marker to Orb for such a model. The
tests below are the claims that make that safe, in the order they matter:

  1. The model's OWN marker still wins. A leading `ACT|` locks the stream on an
     asserted turn exactly as before, so the second lock is not removed, only
     stopped from being waited on.
  2. The deterministic gate still dominates everything. `mutation_locked` beats
     an asserted marker.
  3. Fail-closed is still the DEFAULT. A streamer nobody asserted for behaves
     bit for bit as it did before this change.
  4. The ACTION DECISION IS UNCHANGED. This is the claim that makes the whole
     thing a latency fix rather than a safety trade: on a reply with no leading
     marker, `parse_fast_response` reaches the same verdict whether or not the
     stream was released, because a marker out of position was never evidence
     of anything. Asserting changes WHEN the clinician reads the text, not what
     Orb concluded about it.
  5. Everything the streamer strips, it still strips: the loose 4B scaffolding,
     the forbidden phrases, the details block.
"""
import pytest

from routes.sage_routes import (
    MarkerAnswerStreamer,
    SentenceStreamSanitizer,
    _strip_forbidden_streaming,
    parse_fast_response,
)
from services import sage_service

ANSWER = ("Hypoxia is inadequate oxygen delivery to the tissues. It can follow "
          "hypoventilation, shunt, or low inspired oxygen.")

# The 4B's real shape, from the 2026-09-07 transcripts: answer first, no marker
# anywhere, then the detail contract emitted loose with no <details> wrapper.
LOOSE_TAIL = ("\nDetails:\n<display_mode> concise\n<patient_facts_summary> None\n"
              "<cited_sources> [\"\"]\n")
FOUR_B_REPLY = ANSWER + LOOSE_TAIL


def _stream(text, *, chunk=6, mutation_locked=False, asserted=False):
    s = MarkerAnswerStreamer(
        SentenceStreamSanitizer(_strip_forbidden_streaming), mutation_locked)
    if asserted:
        s.assert_marker()
    out = "".join(s.feed(text[i:i + chunk]) or "" for i in range(0, len(text), chunk))
    return out + (s.finalize() or ""), s


# ── 3. fail-closed is still the default ──────────────────────────────────────

def test_default_still_fails_closed_on_a_markerless_reply():
    """The behaviour this change is careful NOT to alter anywhere it was earning
    its keep. Nobody asserted the marker, so the gate stays shut."""
    streamed, s = _stream(FOUR_B_REPLY)
    assert streamed == ""
    assert s.streamed_any is False


# ── the fix ──────────────────────────────────────────────────────────────────

def test_asserted_marker_streams_the_markerless_reply():
    streamed, s = _stream(FOUR_B_REPLY, asserted=True)
    assert s.streamed_any is True
    assert "inadequate oxygen delivery" in streamed


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 13, 64, 4096])
def test_asserted_marker_holds_at_every_chunk_size(chunk):
    """Token boundaries must not decide what a clinician reads."""
    streamed, _ = _stream(FOUR_B_REPLY, chunk=chunk, asserted=True)
    assert "inadequate oxygen delivery" in streamed


def test_asserted_marker_still_cuts_the_loose_scaffolding():
    """Opening the gate must not open it onto the contract. The 4B lane emits
    its sections loose (test_fast_contract_scaffolding.py), and this is the lane
    that now streams, so the cut has to hold on the streaming path too."""
    streamed, _ = _stream(FOUR_B_REPLY, asserted=True)
    for leak in ("display_mode", "patient_facts_summary", "cited_sources", "Details:"):
        assert leak not in streamed, f"{leak!r} reached the clinician"


def test_asserted_marker_still_strips_forbidden_phrases():
    reply = "I have added paracetamol to the chart. " + ANSWER
    streamed, _ = _stream(reply, asserted=True)
    assert "I have added" not in streamed
    assert streamed.strip(), "the answer must not be blanked by the strip"


def test_asserted_marker_does_not_leak_a_details_block():
    reply = (ANSWER + "<details><display_mode>concise</display_mode>"
             "<cited_sources>[\"DOC-1\"]</cited_sources></details>")
    streamed, _ = _stream(reply, asserted=True)
    assert "DOC-1" not in streamed
    assert "display_mode" not in streamed
    assert "inadequate oxygen delivery" in streamed


# ── 1. the model's own marker still wins ─────────────────────────────────────

def test_a_leading_act_still_locks_an_asserted_turn():
    """THE CLAIM THAT MAKES THIS SAFE. Asserting the marker removes the
    DEPENDENCE on the model declaring a mutation, not the model's ability to.
    A model that does open with ACT| is still believed, and still locks."""
    raw = 'ACT|{"action_type": "add_medication", "medication": "Paracetamol"} ' + ANSWER
    streamed, s = _stream(raw, asserted=True)
    assert streamed == ""
    assert s.streamed_any is False


def test_a_leading_noact_opens_after_the_marker_not_at_zero():
    """An asserted turn whose model DID comply must not print its own marker."""
    streamed, _ = _stream("NOACT|" + ANSWER, asserted=True)
    assert "NOACT|" not in streamed
    assert streamed.lstrip().startswith("Hypoxia")


# ── 2. the deterministic gate still dominates ────────────────────────────────

def test_mutation_locked_beats_an_asserted_marker():
    """`mutation_locked` is detect_mutation_intent's verdict, and it is the gate
    that never depended on the model in the first place. Asserting a marker must
    not be a way around it."""
    streamed, s = _stream(FOUR_B_REPLY, mutation_locked=True, asserted=True)
    assert streamed == ""
    assert s.streamed_any is False


def test_assert_marker_after_the_gate_has_decided_is_ignored():
    """The route calls this before the first chunk. Calling it late must not
    re-open a stream that already locked, which is the one way this API could
    turn into a bypass."""
    s = MarkerAnswerStreamer(
        SentenceStreamSanitizer(_strip_forbidden_streaming), mutation_locked=False)
    assert s.feed(FOUR_B_REPLY[:40]) == ""      # gate gives up, locks
    s.assert_marker()
    assert s.feed(FOUR_B_REPLY[40:]) == ""
    assert (s.finalize() or "") == ""
    assert s.streamed_any is False


# ── 4. the action decision is unchanged ──────────────────────────────────────

def test_the_action_verdict_is_identical_with_and_without_the_assertion():
    """A LATENCY FIX, NOT A SAFETY TRADE.

    On a reply with no leading marker the buffered parser fails open and reads
    the action decision as "None.", because a marker out of position was never
    evidence of anything (see parse_fast_response). That verdict is computed
    from the same text whether or not the stream was released, so Orb concludes
    exactly what it did before; only the moment the clinician reads it moves.
    """
    for raw in (FOUR_B_REPLY,
                ANSWER + "\nNOACT|",                     # marker at the END
                ANSWER + '\nACT|{"action_type": "order"}'):
        parsed = parse_fast_response(raw)
        assert parsed["action_intent"] == "None."
        assert parsed["marker"] is None
        assert parsed["answer"], "the buffered path must never render blank"


def test_streamed_text_matches_what_the_buffered_path_would_have_rendered():
    """LANE PARITY. The clinician now reads the fast lane's answer live instead
    of in one lump at the end; both must be the same answer. A live lane that
    shows more (or less) than the record is the defect class this suite keeps
    finding."""
    streamed, _ = _stream(FOUR_B_REPLY, asserted=True)
    buffered = parse_fast_response(FOUR_B_REPLY)["answer"]
    assert streamed.strip() == buffered.strip()


# ── the policy that decides who asserts ──────────────────────────────────────

def test_the_careful_tier_keeps_the_model_asserted_marker(monkeypatch):
    """It complies 9/9. Its second lock is real, so it stays armed."""
    monkeypatch.delenv("ORB_SAGE_MARKER_SOURCE", raising=False)
    assert sage_service.server_asserts_marker("simple", sage_service.LLM_MODEL) is False


@pytest.mark.parametrize("model", ["medgemma:4b", "medgemma:1.5-4b", "some-future-4b"])
def test_every_other_model_gets_a_server_asserted_marker(monkeypatch, model):
    """`auto` keys on the MODEL, not on the thinking flag: the residency router
    can serve a THINKING ON turn on the ambient 4B, and that turn has the same
    problem. Unmeasured models are treated as unmeasured."""
    monkeypatch.delenv("ORB_SAGE_MARKER_SOURCE", raising=False)
    assert sage_service.server_asserts_marker("simple", model) is True


@pytest.mark.parametrize("scope", ["broad", "aggregate", "quick"])
def test_prose_scopes_never_assert_a_marker(monkeypatch, scope):
    """They carry no marker contract and are streamed by the plain sanitizer."""
    monkeypatch.delenv("ORB_SAGE_MARKER_SOURCE", raising=False)
    assert sage_service.server_asserts_marker(scope, "medgemma:4b") is False


def test_the_xml_contract_rollback_takes_the_marker_with_it(monkeypatch):
    """ORB_SAGE_XML_CONTRACT=1 restores the XML contract and XmlAnswerStreamer,
    which has no marker and no assert_marker(). The flag must be False there or
    the route would call a method that does not exist."""
    monkeypatch.delenv("ORB_SAGE_MARKER_SOURCE", raising=False)
    monkeypatch.setenv("ORB_SAGE_XML_CONTRACT", "1")
    assert sage_service.server_asserts_marker("simple", "medgemma:4b") is False


def test_marker_source_env_switch(monkeypatch):
    """The rollback, and the forward override."""
    monkeypatch.setenv("ORB_SAGE_MARKER_SOURCE", "model")
    assert sage_service.server_asserts_marker("simple", "medgemma:4b") is False
    monkeypatch.setenv("ORB_SAGE_MARKER_SOURCE", "server")
    assert sage_service.server_asserts_marker("simple", sage_service.LLM_MODEL) is True
    monkeypatch.setenv("ORB_SAGE_MARKER_SOURCE", "nonsense")
    assert sage_service.server_asserts_marker("simple", "medgemma:4b") is True
