"""Streaming-sanitizer tests for /api/sage/query.

Proves the two properties the incremental pipeline must hold simultaneously:
  1. STREAMING IS REAL — a non-mutation turn produces MULTIPLE content events,
     and the first arrives before the generation is complete (not one buffered
     block at the end).
  2. SANITIZATION IS INTACT — no client-visible byte ever contains a forbidden
     self-confirmation phrase ("order placed.", "I have added metoprolol."),
     even when the phrase spans chunk boundaries; and a mutation turn emits
     NOTHING until the action-state override has rewritten the answer.

The end-to-end tests drive the real route with a mocked model stream; the unit
tests pin the sanitizer primitives (FORBIDDEN_PHRASE_PATTERNS/_strip_forbidden,
SentenceStreamSanitizer, XmlAnswerStreamer) in routes/sage_routes.py — exactly
where a regression would let a confirmation slip through mid-stream.
"""
import json
import re
from unittest import mock

import pytest
from fastapi.testclient import TestClient

with mock.patch("config.INGESTION_MODE", "hybrid"):
    from main import app

import routes.sage_routes as sage_routes
from routes.sage_routes import (
    FORBIDDEN_PHRASE_PATTERNS,
    SentenceStreamSanitizer,
    XmlAnswerStreamer,
    _strip_forbidden,
    _strip_forbidden_streaming,
)

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def helix_tables():
    # The action-state override looks meds up in helix_medication_master.
    from services.helix_db import init_helix_db
    init_helix_db()


def _login() -> str:
    r = client.post("/api/auth/login", json={"employee_id": "D001", "password": "password"})
    assert r.status_code == 200
    return r.json()["token"]


def _collect_events(resp_text: str) -> list[dict]:
    events = []
    for frame in resp_text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data: "):
            try:
                events.append(json.loads(frame[6:]))
            except json.JSONDecodeError:
                pass
    return events


def _fake_stream(chunks, *, model="test", marker_asserted=None):
    """Build a query_ollama stand-in yielding the given chunks.

    `marker_asserted` stands in for what the real query_ollama stamps into
    route_info once the thinking-mode and residency routers have chosen a model
    (`sage_service.server_asserts_marker`). It is the ONLY thing that tells the
    route who owns this turn's gate marker, so a fake that never sets it is
    exercising the careful tier.
    """
    async def fake(prompt, context="", conversation_history=None, scope="simple", **kwargs):
        info = kwargs.get("route_info")
        if info is not None:
            info.update(lane="local", provider="ollama", model=model)
            if marker_asserted is not None:
                info.update(marker_asserted=marker_asserted)
        for c in chunks:
            yield c
    return fake


XML_ANSWER = (
    "<action_intent>None.</action_intent>"
    "<concise_answer>Metoprolol commonly causes bradycardia. "
    "Order placed. Monitor the heart rate closely. "
    "Fatigue and dizziness are also frequent.</concise_answer>"
    "<display_mode>concise</display_mode>"
    "<patient_facts_summary>No patient context.</patient_facts_summary>"
    "<local_policy_guidance>No local policy retrieved.</local_policy_guidance>"
    "<external_guidance>No external guidance retrieved.</external_guidance>"
    "<clinical_assessment>Beta-blockade slows AV conduction.</clinical_assessment>"
    "<caveats>None.</caveats>"
    "<escalation>None.</escalation>"
    "<cited_sources>[]</cited_sources>"
)


def _chunked(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


# The FAST (answer-first) contract shipped 2026-07-31 is what production emits;
# the XML contract survives behind ORB_SAGE_XML_CONTRACT=1 as the rollback path.
# The streaming properties below must hold on BOTH, so the tests run against
# each — same assertions, same forbidden phrase spanning chunk boundaries.
FAST_ANSWER = (
    "NOACT|Metoprolol commonly causes bradycardia. "
    "Order placed. Monitor the heart rate closely. "
    "Fatigue and dizziness are also frequent.\n"
    "<details>\n"
    "<display_mode>concise</display_mode>\n"
    "<clinical_assessment>Beta-blockade slows AV conduction.</clinical_assessment>\n"
    "<cited_sources>[]</cited_sources>\n"
    "</details>"
)


@pytest.mark.parametrize("contract", ["fast", "xml"])
def test_non_mutation_turn_streams_incrementally_and_sanitized(monkeypatch, contract):
    if contract == "xml":
        monkeypatch.setenv("ORB_SAGE_XML_CONTRACT", "1")
        payload = XML_ANSWER
    else:
        monkeypatch.delenv("ORB_SAGE_XML_CONTRACT", raising=False)
        payload = FAST_ANSWER
    token = _login()
    # 7-byte chunks: forbidden phrase "Order placed." is guaranteed to span
    # chunk boundaries.
    fake = _fake_stream(_chunked(payload, 7))
    with mock.patch.object(sage_routes, "query_ollama", fake):
        r = client.post(
            "/api/sage/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "what are the side effects of metoprolol"},
        )
    assert r.status_code == 200
    events = _collect_events(r.text)

    content_events = [e for e in events if "content" in e]
    # Property 1: incremental — the answer arrived as multiple content events.
    assert len(content_events) >= 2, f"expected streaming, got {content_events}"

    streamed = "".join(e["content"] for e in content_events)
    # Property 2: sanitized — the hallucinated confirmation never reached the wire.
    assert "order placed" not in streamed.lower()
    assert "bradycardia" in streamed.lower()
    assert "dizziness" in streamed.lower()
    # No XML markup leaked into the visible stream.
    assert "<" not in streamed and ">" not in streamed

    # Raw SSE bytes as a whole (belt over braces): the phrase appears nowhere
    # in any client-visible content event, only sanitized text does.
    for e in content_events:
        assert "order placed" not in e["content"].lower()

    # The final structured_response is also sanitized.
    structured = [e for e in events if "structured_response" in e][0]["structured_response"]
    assert "order placed" not in structured["concise_answer"].lower()


def test_mutation_turn_emits_nothing_until_override():
    token = _login()
    mutation_xml = (
        '<action_intent>{"action_type": "add_medication", "medication": "Paracetamol", "dose": "500 mg"}</action_intent>'
        "<concise_answer>I have added Paracetamol 500 mg to the chart. Order placed.</concise_answer>"
        "<display_mode>concise</display_mode>"
        "<patient_facts_summary>.</patient_facts_summary>"
        "<local_policy_guidance>.</local_policy_guidance>"
        "<external_guidance>.</external_guidance>"
        "<clinical_assessment>Done. The medication was added.</clinical_assessment>"
        "<caveats>None.</caveats><escalation>None.</escalation><cited_sources>[]</cited_sources>"
    )
    fake = _fake_stream(_chunked(mutation_xml, 7))
    with mock.patch.object(sage_routes, "query_ollama", fake):
        r = client.post(
            "/api/sage/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "add paracetamol 500mg to the patient"},
        )
    assert r.status_code == 200
    events = _collect_events(r.text)
    content_events = [e for e in events if "content" in e]

    # Exactly ONE content event: the override's safe message, nothing streamed
    # before it (a mutation turn is buffered end-to-end).
    assert len(content_events) == 1, f"mutation turn must not stream: {content_events}"
    visible = content_events[0]["content"].lower()
    assert "i have added" not in visible
    assert "order placed" not in visible

    metadata = [e for e in events if "metadata" in e][0]["metadata"]
    action_state = metadata["action_state"]
    assert action_state is not None
    assert action_state["status_code"] in ("request_drafted", "not_in_helix_database", "unsupported")
    # The safe message is exactly what the client saw.
    assert content_events[0]["content"] == action_state["safe_user_message"]


@pytest.mark.parametrize("contract", ["fast", "xml"])
def test_model_declared_mutation_locks_stream_even_without_deterministic_gate(monkeypatch, contract):
    """The deterministic regex can miss a phrasing ('he needs paracetamol on
    his chart'); the model may still declare an action. The gate marker precedes
    the answer in BOTH contracts (ACT| in the fast one, <action_intent> in the
    XML one), so the streamer must lock BEFORE any answer text exists.

    This is the defence-in-depth property that killed an earlier prose-lane
    experiment — it is checked on whichever contract is live."""
    token = _login()
    if contract == "xml":
        monkeypatch.setenv("ORB_SAGE_XML_CONTRACT", "1")
        sneaky = (
            '<action_intent>{"action_type": "add_medication", "medication": "Paracetamol"}</action_intent>'
            "<concise_answer>I've added it. Order placed.</concise_answer>"
            "<display_mode>concise</display_mode>"
            "<patient_facts_summary>.</patient_facts_summary>"
            "<local_policy_guidance>.</local_policy_guidance>"
            "<external_guidance>.</external_guidance>"
            "<clinical_assessment>.</clinical_assessment>"
            "<caveats>None.</caveats><escalation>None.</escalation><cited_sources>[]</cited_sources>"
        )
    else:
        monkeypatch.delenv("ORB_SAGE_XML_CONTRACT", raising=False)
        sneaky = (
            'ACT|{"action_type": "add_medication", "medication": "Paracetamol"}\n'
            "I've added it. Order placed.\n"
            "<details>\n<caveats>None.</caveats>\n<cited_sources>[]</cited_sources>\n</details>"
        )
    # Query with NO deterministic mutation keywords (and no broad-scope
    # keywords, so it rides the gated contract):
    query = "he really needs paracetamol for the fever"
    assert not sage_routes.detect_mutation_intent(query)

    fake = _fake_stream(_chunked(sneaky, 5))
    with mock.patch.object(sage_routes, "query_ollama", fake):
        r = client.post(
            "/api/sage/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": query},
        )
    events = _collect_events(r.text)
    content_events = [e for e in events if "content" in e]
    assert len(content_events) == 1
    assert "order placed" not in content_events[0]["content"].lower()
    assert "i've added" not in content_events[0]["content"].lower()


def test_broad_scope_streams_sanitized_sentences():
    token = _login()
    broad_text = (
        "**Critical patients:** Bed 4 needs eyes first due to rising heart rate. "
        "Order placed. The remaining patients are stable.\n"
        "- Ward A: 10 stable\n- Ward B: 8 stable\n"
    )
    fake = _fake_stream(_chunked(broad_text, 9))
    with mock.patch.object(sage_routes, "query_ollama", fake):
        r = client.post(
            "/api/sage/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "give me the hospital census overview"},
        )
    events = _collect_events(r.text)
    content_events = [e for e in events if "content" in e]
    assert len(content_events) >= 2  # streamed, not one buffered block
    streamed = "".join(e["content"] for e in content_events)
    assert "order placed" not in streamed.lower()
    assert "stable" in streamed.lower()
    # Markdown list formatting survives streaming (newlines preserved).
    assert "- Ward A: 10 stable" in streamed


# ─── Unit tests for the sanitizer primitives ───

FORBIDDEN_SUBSTRINGS = [
    "order placed",
    "order has been placed",
    "i have added",
    "i've added",
    "i have ordered",
    "chart updated",
    "medication was added",
]


def _assert_clean(text: str):
    low = text.lower()
    for bad in FORBIDDEN_SUBSTRINGS:
        assert bad not in low, f"forbidden phrase leaked: {bad!r} in {text!r}"


# The REAL whitespace-preserving strip, imported rather than mirrored: a local
# copy of the guardrail pipeline let this suite pass while the shipped strip was
# broken (it was not a fixed point — "Order placed. Done." kept the "Done.").
_real_streaming_strip = _strip_forbidden_streaming


def _mk_strip():
    pats = [r"(?i)order placed\.?", r"(?i)i have added\b[^.]*\.?"]

    def strip(text):
        for p in pats:
            text = re.sub(p, "", text)
        return text
    return strip


# _strip_forbidden (module-level: shared by the buffered path, the streaming
# strip, and the action-state override) — ported pins from the earlier suite.

@pytest.mark.parametrize(
    "raw",
    [
        "The patient is stable. Order placed. Continue monitoring.",
        "I have added metoprolol 25mg to the chart.",
        "Chart updated. The vitals look reassuring.",
    ],
)
def test_strip_forbidden_removes_confirmations(raw):
    _assert_clean(_strip_forbidden(raw))


def test_strip_forbidden_keeps_clinical_prose():
    text = "The dose is 500 mg. He is done with the antibiotic course."
    out = _strip_forbidden(text)
    # "done" mid-sentence is legitimate and must survive.
    assert "done with the antibiotic course" in out.lower()
    assert "500 mg" in out


@pytest.mark.parametrize("strip", [_strip_forbidden, _strip_forbidden_streaming])
def test_strip_forbidden_is_a_fixed_point(strip):
    """Regression: the strip used to apply its patterns SEQUENTIALLY, so removing
    "Order placed." destroyed the sentence boundary the bare-"Done." lookbehind
    needs and the second self-confirmation survived. Both lanes now iterate one
    alternation to a fixed point — no forbidden pattern may match the output.
    (Exhaustive per-phrase and per-PAIR coverage: tests/test_stream_sanitizer.py.)"""
    for raw in ("Order placed. Done.",
                "Chart updated. Done!",
                "I have added metoprolol. Done."):
        out = strip(raw)
        assert "done" not in out.lower(), f"self-confirmation survived: {out!r}"
        for pat in FORBIDDEN_PHRASE_PATTERNS:
            assert re.search(pat, out) is None, f"{pat!r} still matches in {out!r}"


def test_sentence_sanitizer_holds_incomplete_sentence():
    """A partially-arrived forbidden phrase is held until it can be stripped,
    and the benign tail is released exactly once.

    The assertions are deliberately granularity-agnostic: the sanitizer now
    holds only the smallest unsafe SUFFIX rather than the whole trailing
    sentence, so "Next point" may be released by feed() or by flush() (see
    tests/test_stream_sanitizer.py). What must never change is that
    "order placed" never reaches the client and the tail is emitted intact.
    """
    s = SentenceStreamSanitizer(_mk_strip())
    assert s.feed("Order pla") == ""          # incomplete phrase — held
    out = s.feed("ced. Next point")           # phrase completed — stripped
    assert "order placed" not in out.lower()
    assert (out + s.flush()).strip() == "Next point"


def test_xml_streamer_locks_on_model_mutation():
    x = XmlAnswerStreamer(SentenceStreamSanitizer(_mk_strip()), mutation_locked=False)
    assert x.feed('<action_intent>{"a": 1}</action_intent><concise_answer>Hi. ') == ""
    assert x.feed("There.</concise_answer>") == ""
    assert x.streamed_any is False


def test_xml_streamer_streams_after_none_intent():
    x = XmlAnswerStreamer(SentenceStreamSanitizer(_mk_strip()), mutation_locked=False)
    out1 = x.feed("<action_intent>None.</action_intent><concise_answer>First sentence. Sec")
    # Granularity-agnostic: the sanitizer may also release part of the trailing
    # incomplete sentence (it only holds a possible forbidden-phrase prefix), so
    # pin the guarantee — answer text in order, no markup, nothing duplicated —
    # rather than the exact split point.
    assert out1.startswith("First sentence. ")
    assert "<" not in out1 and ">" not in out1
    out2 = x.feed("ond sentence.</concise_answer><display_mode>")
    assert "ond sentence." in out2
    assert out1 + out2 == "First sentence. Second sentence."
    assert x.streamed_any is True


def test_xml_streamer_never_leaks_partial_tags():
    x = XmlAnswerStreamer(SentenceStreamSanitizer(_mk_strip()), mutation_locked=False)
    x.feed("<action_intent>None.</action_intent><concise_answer>Value is fine. Tail")
    out = x.feed(" text here.</conc")
    assert "<" not in out
    tail = x.finalize()
    assert "<" not in tail


def test_xml_streamer_never_emits_forbidden_across_chunk_boundaries():
    """Ported pin: the confirmation is split across chunks; a naive per-token
    stream could leak "Ord"/"er placed". Sentence-gated emission through the
    REAL forbidden patterns must never surface any fragment of it, while the
    surrounding clinical prose survives."""
    x = XmlAnswerStreamer(
        SentenceStreamSanitizer(_real_streaming_strip), mutation_locked=False
    )
    chunks = [
        "<action_intent>none</action_intent><concise_answer>The dose ",
        "is 500mg. Ord",
        "er placed. Continue as ",
        "planned.</concise_answer>",
    ]
    deltas = [x.feed(c) for c in chunks]
    deltas.append(x.finalize())
    for d in deltas:
        _assert_clean(d)
        assert "<" not in d and ">" not in d
    joined = "".join(deltas)
    _assert_clean(joined)
    assert "500mg" in joined
    assert "Continue as planned." in joined


# ── WHO OWNS THE GATE MARKER, end to end (2026-09-07) ────────────────────────
# The unit tests in test_fast_contract_marker_source.py pin the streamer and the
# policy. These two pin the WIRING between them, which is the part neither can
# see: query_ollama stamps `marker_asserted` into route_info before it yields
# anything, and the route has to read it at the first chunk, before the streamer
# has been fed. Get that order wrong and the fix is inert in production while
# every unit test stays green.
#
# The reply below is the fast tier's real shape from the 2026-09-07 transcripts:
# no marker anywhere, answer first, contract appended as loose tags.
FOUR_B_REPLY = (
    "Hypoxia is inadequate oxygen delivery to the tissues. "
    "It can follow hypoventilation, shunt, or a low inspired oxygen fraction. "
    "Order placed. Treatment targets the underlying cause.\n"
    "Details:\n<display_mode> concise\n<patient_facts_summary> None\n"
    "<cited_sources> [\"\"]\n"
)


def test_a_fast_tier_turn_streams_even_though_the_model_emitted_no_marker(monkeypatch):
    """THE FIX. medgemma:4b emits no marker (0/9 measured), so this turn used to
    reach the clinician as one buffered block at the end of the generation."""
    monkeypatch.delenv("ORB_SAGE_XML_CONTRACT", raising=False)
    token = _login()
    fake = _fake_stream(_chunked(FOUR_B_REPLY, 7),
                        model="medgemma:4b", marker_asserted=True)
    with mock.patch.object(sage_routes, "query_ollama", fake):
        r = client.post(
            "/api/sage/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "what is hypoxia", "thinking": False},
        )
    events = _collect_events(r.text)
    content_events = [e for e in events if "content" in e]
    assert len(content_events) >= 2, f"expected streaming, got {content_events}"

    streamed = "".join(e["content"] for e in content_events)
    assert "inadequate oxygen delivery" in streamed
    # Everything the gate did before it opened, it still does after.
    assert "order placed" not in streamed.lower()
    for leak in ("display_mode", "cited_sources", "Details"):
        assert leak not in streamed, f"{leak!r} reached the clinician"


def test_the_same_reply_on_the_careful_tier_still_fails_closed(monkeypatch):
    """The other half of the claim: nothing changed where the marker was being
    honoured. An unmarked reply from the careful tier is still withheld from the
    stream and rendered once, by the buffered path."""
    monkeypatch.delenv("ORB_SAGE_XML_CONTRACT", raising=False)
    token = _login()
    fake = _fake_stream(_chunked(FOUR_B_REPLY, 7), model="medgemma:27b")
    with mock.patch.object(sage_routes, "query_ollama", fake):
        r = client.post(
            "/api/sage/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "what is hypoxia", "thinking": True},
        )
    events = _collect_events(r.text)
    content_events = [e for e in events if "content" in e]
    assert len(content_events) == 1, f"careful tier must still buffer: {content_events}"
    assert "order placed" not in content_events[0]["content"].lower()
