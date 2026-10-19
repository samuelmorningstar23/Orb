"""A REOPENED conversation must read exactly like a live one.

WHY THIS FILE EXISTS. `sage_conversations.content` stores the RAW model reply —
the fast contract's `NOACT|`/`ACT|{…}` marker, the trailing `<details>` section
block, the 27B era's `<concise_answer>` wrapper — because that blob is the
provenance record. The LIVE lane never shows any of it: MarkerAnswerStreamer
cuts the answer at the contract boundary and every released segment goes through
the forbidden-phrase strip. `/api/sage/history` returned `content` VERBATIM, so
the same answer read clean while it streamed and, on reload, printed

    NOACT| National Early Warning Score version 2 (NEWS2) aggregates six …
    <details> <cited_sources> ["SEP-001"]</cited_sources> </details>

into the chat bubble as if the ids were clinical text (observed in the browser
2026-08-11). That is the repo's guardrail law broken in the plainest way: a
phrase one lane strips, every lane must strip — and the replay lane is a lane.

WHAT IS PINNED HERE:

  1. THE INVARIANT. No assistant message on the /history payload contains
     contract syntax or a forbidden phrase — parameterised over stored fixtures
     from BOTH output contracts (fast-marker and XML) and over the shapes the
     4B lane actually emits (loose tags, bracket-less "Details:", trailing
     marker, no contract at all).
  2. PARITY WITH THE WIRE. The replayed answer carries the same PHRASES as the
     streamed one, at every chunk size — the model's tokenisation must not
     decide what a reopened conversation says.
  3. NOTHING ELSE MOVES. The clinical sentence survives, the clinician's own
     question is returned verbatim, and the row on disk is still raw.

The model is stubbed nowhere in this file: the fixtures ARE stored turns, which
is the actual failing input, and they are read back through the real HTTP route.
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from auth import hash_password  # noqa: E402
from database import db_session  # noqa: E402
from main import app  # noqa: E402
from routes.sage_routes import (  # noqa: E402
    FORBIDDEN_PHRASE_PATTERNS,
    MarkerAnswerStreamer,
    SentenceStreamSanitizer,
    _display_answer,
    _strip_forbidden_streaming,
)
from services.sage_service import save_conversation  # noqa: E402

client = TestClient(app)

DOC = "HIST-RENDER-DOC"
DEPT = "General Medicine"


@pytest.fixture(autouse=True)
def _fresh():
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (DOC, "Dr. Replay", "doctor", DEPT, hash_password("password")),
        )
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (DOC,))
    yield
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (DOC,))


def _login() -> dict:
    r = client.post("/api/auth/login", json={"employee_id": DOC, "password": "password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _history() -> list:
    r = client.get("/api/sage/history", headers=_login())
    assert r.status_code == 200, r.text
    return r.json()["messages"]


def _answers(messages) -> list:
    return [m for m in messages if m["role"] == "assistant"]


def _seed(raw: str, *, question: str = "clinical question", blob=None) -> None:
    """One stored turn, exactly as a real one is written: raw reply in content."""
    save_conversation(DOC, "user", question, conversation_id="thread-r")
    save_conversation(DOC, "assistant", raw, sources=blob, conversation_id="thread-r")


# ════════════════════════════════════════════════════════════════════════════
#  STORED FIXTURES — both contracts, and the shapes seen in the browser
# ════════════════════════════════════════════════════════════════════════════
#: (id, stored raw reply, a sentence fragment that MUST survive)
STORED_TURNS = [
    (
        # Tonight's defect, verbatim in shape: fast contract, 27B <details> block.
        "fast_marker_with_details",
        'NOACT| National Early Warning Score version 2 (NEWS2) aggregates six '
        'physiological parameters. A score of 5 or more triggers urgent review.\n'
        '<details>\n<display_mode>concise</display_mode>\n'
        '<patient_facts_summary>None</patient_facts_summary>\n'
        '<cited_sources>["SEP-001"]</cited_sources>\n</details>',
        "aggregates six physiological parameters",
    ),
    (
        # The 4B lane drops <details> and emits the sections loose.
        "fast_marker_loose_tags",
        'NOACT| The classic ECG findings of hyperkalaemia include peaked T waves '
        'and a widened QRS.\n<display_mode> concise <patient_facts_summary> None '
        '<cited_sources> ["EXT-K-001"]',
        "peaked T waves",
    ),
    (
        # Loose tags again, but the answer ends on a clinical comparison. The
        # boundary widening that pulls "<display_mode" back onto the contract
        # side must not touch a "<" that is arithmetic — if it ever does, this
        # fixture loses a threshold, which is the failure that actually harms.
        "fast_marker_loose_tags_after_comparison",
        'NOACT| Treat if K < 3.5 mmol/L or SpO2 < 92% on air.\n'
        '<display_mode> concise <cited_sources> ["EXT-K-002"]',
        "K < 3.5 mmol/L",
    ),
    (
        # The bracket-less form wearing markdown emphasis. `_BARE_CONTRACT_CUT_RE`
        # allows `[*_]{0,2}` in front of the label and cuts from the emphasis, so
        # the streamer's hold-back has to hold the "**" too — holding only the
        # label released "**" as prose and the cut then landed behind it.
        "fast_marker_emphasised_contract",
        'NOACT| Start oxygen immediately and reassess in five minutes.\n\n'
        '**display_mode** concise None None',
        "reassess in five minutes",
    ),
    (
        # The same contract in UNDERSCORE emphasis. "_" is a word character, so
        # the old `\b`-anchored pattern never matched this and it leaked whole —
        # in the replay lane as well as the wire, which is why the parity test
        # above could not see it either.
        "fast_marker_underscore_contract",
        'NOACT| Hold the ACE inhibitor until the creatinine settles.\n\n'
        '__display_mode__ concise __patient_facts_summary__ None',
        "until the creatinine settles",
    ),
    (
        # One level worse: the contract written as prose, no angle brackets.
        "fast_marker_bare_contract",
        'NOACT| Consider intubation and mechanical ventilation if the patient '
        'deteriorates.\n\nDetails: concise None None None',
        "mechanical ventilation",
    ),
    (
        # The model answered first and put the marker LAST.
        "trailing_marker",
        'Sepsis six should be completed within one hour of recognition.\nNOACT|',
        "within one hour",
    ),
    (
        # Mutation turn: marker carries the intent JSON.
        "act_marker_with_intent",
        'ACT|{"action_type": "add_medication", "medication": "Metoprolol", '
        '"dose": "25 mg"} Request drafted for pharmacy review; confirm before '
        'sending.\n<details><display_mode>concise</display_mode>'
        '<cited_sources>["LOC-CARD-003"]</cited_sources></details>',
        "confirm before sending",
    ),
    (
        # 27B XML contract — the pre-marker era still sitting in every ward DB.
        "xml_contract",
        '```xml\n<action_intent>None.</action_intent>\n'
        '<concise_answer>Give paracetamol 1 g orally every six hours as needed, '
        'to a maximum of 4 g in 24 hours.</concise_answer>\n<details>\n'
        '<display_mode>concise</display_mode>\n'
        '<clinical_assessment>Standard adult analgesic dosing.</clinical_assessment>\n'
        '<cited_sources>["LOC-ANALG-002"]</cited_sources>\n</details>\n```',
        "maximum of 4 g in 24 hours",
    ),
    (
        # XML contract, truncated generation: the answer never closed.
        "xml_contract_unclosed",
        '<action_intent>None.</action_intent>\n<concise_answer>Start empirical '
        'antibiotics within one hour of recognising sepsis.',
        "within one hour",
    ),
    (
        # Retrieval markup echoed into prose — the (DOC:) leak.
        "doc_marker_in_prose",
        'NOACT| Escalate per the Tier 2 protocol (DOC: LOC-SAFE-007) within '
        '30 minutes.',
        "Escalate per the Tier 2 protocol",
    ),
    (
        # A fabricated self-confirmation inside a stored answer.
        "forbidden_confirmation",
        'NOACT| Metoprolol slows the heart rate.  Order placed.  Monitor the '
        'pulse closely.',
        "Monitor the pulse closely",
    ),
    (
        # The prose lanes (broad/aggregate/quick) store plain text — nothing to
        # strip, and nothing may be damaged either.
        "plain_prose_no_contract",
        'The sepsis bundle requires blood cultures before the first dose of '
        'antibiotics.',
        "blood cultures before the first dose",
    ),
    (
        # Voice turns are stored with their lane prefix.
        "voice_prefixed",
        '[voice] Paracetamol one gram, six hourly, maximum four grams a day.',
        "six hourly",
    ),
]

STORED_IDS = [t[0] for t in STORED_TURNS]

#: Contract vocabulary that is never clinical content. `display_mode` and
#: `patient_facts_summary` appear bare (no brackets) in the 4B failure shape, so
#: the bare word is banned too — no clinical answer contains either.
BANNED_SUBSTRINGS = (
    "NOACT|", "ACT|", "<details>", "</details>", "<cited_sources>",
    "</cited_sources>", "<concise_answer>", "</concise_answer>",
    "<action_intent>", "<clinical_assessment>", "(DOC:", "(doc:",
    "display_mode", "patient_facts_summary", "local_policy_guidance",
    "external_guidance",
)

_COMPILED_FORBIDDEN = [re.compile(p) for p in FORBIDDEN_PHRASE_PATTERNS]


def _assert_clean(text: str, where: str) -> None:
    low = text.lower()
    for bad in BANNED_SUBSTRINGS:
        assert bad.lower() not in low, f"{where}: contract syntax {bad!r} reached the bubble"
    for pattern in _COMPILED_FORBIDDEN:
        assert not pattern.search(text), f"{where}: forbidden phrase {pattern.pattern!r} survived"


# ════════════════════════════════════════════════════════════════════════════
#  1. THE INVARIANT — over the HTTP route, per stored contract shape
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("_id,raw,keep", STORED_TURNS, ids=STORED_IDS)
def test_history_payload_never_carries_contract_syntax(_id, raw, keep):
    _seed(raw)

    answer = _answers(_history())[-1]
    _assert_clean(answer["content"], f"/history[{_id}]")
    assert keep in answer["content"], "the clinical sentence was lost, not cleaned"


def test_the_whole_payload_is_clean_when_every_shape_is_stored_at_once():
    """The real sidebar is a mixed bag: every contract era in one list."""
    for _id, raw, _keep in STORED_TURNS:
        _seed(raw, question=f"question for {_id}")

    messages = _history()
    # The browsing window now holds 200 messages, retaining multi-turn threads.
    # Every answer must still use the same safe display projection as live chat.
    assert len(messages) == min(2 * len(STORED_TURNS), 200)
    assert len(_answers(messages)) == min(len(STORED_TURNS), 100)
    for m in _answers(messages):
        _assert_clean(m["content"], "/history[mixed]")


@pytest.mark.parametrize("_id,raw,keep", STORED_TURNS, ids=STORED_IDS)
def test_the_projector_itself_is_clean(_id, raw, keep):
    """Same invariant one level down, so a failure says WHERE it broke."""
    _assert_clean(_display_answer(raw), f"_display_answer[{_id}]")


# ════════════════════════════════════════════════════════════════════════════
#  2. PARITY WITH THE WIRE — chunk-agnostic
# ════════════════════════════════════════════════════════════════════════════
#: The fixtures the live fast lane actually streams. `ACT|` turns are excluded
#: on purpose: MarkerAnswerStreamer LOCKS on a declared mutation and the
#: buffered action-state override rewrites the answer, so there is no streamed
#: text to compare against — those turns are covered by the invariant above.
STREAMED_TURNS = [t for t in STORED_TURNS if t[1].startswith("NOACT|")]
STREAMED_IDS = [t[0] for t in STREAMED_TURNS]


def _wire(raw: str, chunk: int) -> str:
    """What the clinician READ live, at this chunk size."""
    streamer = MarkerAnswerStreamer(
        SentenceStreamSanitizer(_strip_forbidden_streaming), False)
    out = "".join(streamer.feed(raw[i:i + chunk]) or ""
                  for i in range(0, len(raw), chunk))
    return out + (streamer.finalize() or "")


def _words(text: str) -> list:
    """Comparable tokens for the two lanes.

    STRICT — every token on one side must appear on the other. This helper used
    to drop a lone "<" from both sides, because the streamer emitted one when a
    contract tag was still arriving: `_BARE_CONTRACT_CUT_RE` matches the label
    INSIDE "<display_mode" before the ">" exists, the cut landed one character
    late, and a streamed character cannot be taken back. That allowance was the
    defect wearing a comment. `_widen_to_opener` now puts the bracket (and the
    "**" of the emphasised prose form) on the contract's side of the boundary in
    the cut AND in the streamer's hold-back, so the lanes agree character for
    character and nothing needs excusing. A clinical "K < 3.5" is untouched by
    that widening and stays comparable in both directions — the tokens below
    would catch it if it were not.
    """
    return text.split()


@pytest.mark.parametrize("_id,raw,keep", STREAMED_TURNS, ids=STREAMED_IDS)
@pytest.mark.parametrize("chunk", (1, 3, 7, 13, 64))
def test_the_replayed_answer_says_what_the_stream_said(_id, raw, keep, chunk):
    """Whitespace policy differs between the lanes (documented in
    _strip_forbidden_streaming); the surviving WORDS may not."""
    assert _words(_display_answer(raw)) == _words(_wire(raw, chunk)), (
        f"replay drifted from the wire at chunk={chunk}")


@pytest.mark.parametrize("_id,raw,keep", STREAMED_TURNS, ids=STREAMED_IDS)
@pytest.mark.parametrize("chunk", range(1, 21))
def test_the_wire_itself_is_clean_at_every_chunk_size(_id, raw, keep, chunk):
    """The reference lane has to hold up, or parity with it proves nothing."""
    _assert_clean(_wire(raw, chunk), f"wire[{_id},chunk={chunk}]")


# ════════════════════════════════════════════════════════════════════════════
#  3. NOTHING ELSE MOVES
# ════════════════════════════════════════════════════════════════════════════
def test_the_stored_row_is_still_raw_after_being_read():
    """Provenance depends on it. This projects on the way OUT; it does not
    rewrite the record, and services/sage_provenance_service reads its own
    tables (sage_provenance_turns/refs/blobs), never this one."""
    raw = STORED_TURNS[0][1]
    _seed(raw)
    _history()

    with db_session() as db:
        stored = db.execute(
            "SELECT content FROM sage_conversations WHERE user_id = ? AND role = 'assistant'",
            (DOC,),
        ).fetchone()["content"]
    assert stored == raw, "the raw provenance blob was mutated by a read"


def test_the_clinicians_own_question_is_returned_verbatim():
    """A user row is the record of what was ASKED. Running the answer guardrail
    over it would silently rewrite that record — and the live lane does not
    sanitize the prompt either."""
    asked = "Does the NOACT| marker mean the order was placed? Chart updated?"
    save_conversation(DOC, "user", asked, conversation_id="thread-r")

    assert _history()[-1]["content"] == asked


def test_timing_metadata_still_rides_along_untouched():
    """The projection is on `content` only; tonight's timing persistence work
    must pass through unchanged."""
    _seed(STORED_TURNS[0][1],
          blob=json.dumps({"timing": {"ttft_ms": 910, "total_ms": 5200},
                           "thinking_tier": "careful"}))

    answer = _answers(_history())[-1]
    assert answer["metadata"]["timing"]["ttft_ms"] == 910
    assert answer["metadata"]["timing"]["total_ms"] == 5200
    assert answer["metadata"]["provider"]["thinking_tier"] == "careful"
    _assert_clean(answer["content"], "/history[with timing]")


def test_the_sidebars_shape_is_unchanged():
    """Recent Conversations groups on conversation_id and labels on content."""
    _seed(STORED_TURNS[0][1], question="grouped question")

    msgs = _history()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "grouped question"
    assert {m["conversation_id"] for m in msgs} == {"thread-r"}
    assert all(m.get("timestamp") for m in msgs)


@pytest.mark.parametrize("raw", ["", None])
def test_an_empty_answer_survives_the_projection(raw):
    assert _display_answer(raw) == raw


def test_markdown_structure_in_a_stored_answer_is_preserved():
    """A reopened answer must still render as the list the clinician read."""
    raw = ("NOACT| Post-op day 2.\n\n- Vitals stable\n- Analgesia adequate\n\n"
           "**Plan:** discharge tomorrow.\n<details><display_mode>concise"
           "</display_mode></details>")
    _seed(raw)

    content = _answers(_history())[-1]["content"]
    assert content.count("\n\n") == 2
    assert "- Vitals stable\n- Analgesia adequate" in content
