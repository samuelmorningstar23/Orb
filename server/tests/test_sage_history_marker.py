"""A markerless turn in the history must not cost the next turn its marker.

Section 7a of docs/perf/FAST_LANE_MARKER_2026-09-07.md, pinned.

THE DEFECT. `_history_turn_text(..., keep_marker=True)` replays the previous
assistant turn with its leading `NOACT|`/`ACT|` intact, because this model copies
its own last output and a markerless precedent makes it drop the contract. That
works while there IS a marker to keep. The fast tier emits none at all, so the
turn it stores has none, and the next CAREFUL turn reads a markerless example of
its own voice. Measured 2026-09-07 on medgemma:27b, one follow-up, 5 runs per
arm, twice, with and without this change
(`server/ops/history_marker_replay_{before,after_off,after_auto}.json`):

                                          before   rollback=off   shipped
    no history .........................  10/10       10/10        10/10
    history, a careful turn, marker in .  10/10       10/10        10/10
    history, a FAST turn, no marker ....   5/10        4/10        10/10

`before` is the pre-change code; `rollback=off` is this code with
ORB_SAGE_HISTORY_MARKER=off, which reproduces the defect and is therefore both
the second before-sample and the proof the switch works. The controls never
move. Section 7a's own first run of this scored 0/10 on the fast arm against a
different stored turn: the size of the effect depends on how strong a precedent
that particular markerless turn is, the direction does not.

THE FIX, and the line it must not cross. `NOACT|` is synthesized in front of a
stored turn that has none — which is what Orb already concluded about such a
turn (`parse_fast_response` reads a markerless reply as action_intent "None.").
But `sage_conversations` stores the model's RAW narration, not the safe message
the action-state override replaced it with, so on a MUTATION turn a blanket
synthesis would put "NOACT| I'll add paracetamol to the chart" into the next
prompt as an example — inside the one lock this contract exists to protect. So
the synthesis is gated on the turn's recorded action state, and both gates that
can lock a turn have to be asked: the model's own `ACT|` declaration, and
`detect_mutation_intent` on the query, which is the one that actually fires on
the fast lane because that tier declares nothing.

Section 6 then closes the exception rather than living with it: a mutation turn
is replayed as the SAFE MESSAGE the clinician actually read, which Orb wrote and
which claims nothing, so it can carry a marker like any other turn. The narration
stays in `sage_conversations.content` as the record of what the model said.

PROVE-CAN-FAIL is built in: `ORB_SAGE_HISTORY_MARKER=off` is the pre-change
behaviour for BOTH halves, and every synthesis and substitution assertion here
fails under it. Run
`ORB_SAGE_HISTORY_MARKER=off pytest tests/test_sage_history_marker.py` and expect
red.

The measurement harness is `dev_tools/check_history_marker_replay.py`.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from database import db_session
from main import app
from routes import sage_routes
from routes.sage_routes import _leading_marker
from services import sage_history_service
from services.sage_service import (
    SAGE_FAST_MARKER_NOACT,
    _build_chat_messages,
    _history_turn_proposed_action,
    _history_turn_text,
    _stored_turn_proposed_action,
    _stored_turn_safe_answer,
    declares_action,
    get_conversation_history,
    save_conversation,
    uses_marker_contract,
)

client = TestClient(app)

USER = "DOC001"  # seeded by conftest._reseed_users, password "password"
THREAD = "hist-marker-thread"

ANSWER = "Steroids are considered in moderate-to-severe ARDS."
#: What a mutation turn actually stores: the model's own narration, which the
#: live lane replaced with a safe message the clinician saw and the row did not.
MUTATION_NARRATION = "I'll add paracetamol 1g QDS to the chart for you."
MUTATION_QUERY = "add paracetamol to the chart"


def _replayed(messages):
    """The assistant turns as they reach the model."""
    return [m["content"] for m in messages[:-1] if m["role"] == "assistant"]


def _blob(**kw) -> str:
    """A stored turn's `sources` column."""
    return json.dumps(kw)


@pytest.fixture(autouse=True)
def _clean_thread():
    sage_history_service.reset()
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (USER,))
    yield
    sage_history_service.reset()
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (USER,))


# ══════════════════════════════════════════════════════════════════════
# 1. The rule itself
# ══════════════════════════════════════════════════════════════════════

def test_a_turn_orb_cleared_is_replayed_under_the_marker_it_never_emitted():
    """The 0/10 arm. A fast-lane turn stores no marker, so without this the
    careful model's next turn reads its own last answer as proof the contract is
    optional — and a turn with no marker does not stream."""
    out = _history_turn_text(ANSWER, 400, keep_marker=True, proposed_action=False)
    assert out == f"{SAGE_FAST_MARKER_NOACT} {ANSWER}"


def test_a_mutation_turns_narration_is_never_labelled_no_action():
    """THE LINE. The stored content of a mutation turn is the model's RAW
    narration — the safe message the clinician actually read lives in the audit
    blob, not in this row. Labelling that `NOACT|` teaches the model that
    narrating a chart change is a no-action turn."""
    out = _history_turn_text(MUTATION_NARRATION, 400, keep_marker=True,
                             proposed_action=True)
    assert out == MUTATION_NARRATION
    assert not out.startswith(SAGE_FAST_MARKER_NOACT)


def test_a_turn_with_no_recorded_decision_is_replayed_exactly_as_before():
    """Unknown is not False. A row nobody recorded a verdict for keeps today's
    behaviour, so nothing about this change depends on a backfill."""
    assert _history_turn_text(ANSWER, 400, keep_marker=True) == ANSWER
    assert _history_turn_text(ANSWER, 400, keep_marker=True,
                              proposed_action=None) == ANSWER


def test_a_stored_leading_marker_still_wins_over_a_synthesized_one():
    """Synthesis fills an absence; it never overwrites what the model declared.
    An `ACT|` turn replayed as `NOACT|` would invert the record."""
    act = 'ACT|{"action_type": "add_medication"} ' + MUTATION_NARRATION
    out = _history_turn_text(act, 400, keep_marker=True, proposed_action=False)
    assert out.startswith("ACT|")
    assert SAGE_FAST_MARKER_NOACT not in out


def test_the_prose_lanes_never_gain_a_synthesized_marker():
    """broad/aggregate have no marker contract and their streamer does not strip
    one, so a marker there is shown to the clinician. Same rule as keep_marker."""
    assert _history_turn_text(ANSWER, 400, keep_marker=False,
                              proposed_action=False) == ANSWER


@pytest.mark.parametrize("scope", ["broad", "aggregate", "quick"])
def test_no_prose_scope_replays_or_gains_a_marker(scope):
    """One list, asked in one place. Assembly used to restate the prose scopes as
    ("broad", "aggregate"), which is `uses_marker_contract` minus "quick" — so a
    quick turn would have been handed a marker its own streamer does not strip.
    Unreachable today (get_relevant_context never returns "quick"), which is
    exactly why it needs a test rather than a reader noticing."""
    assert not uses_marker_contract(scope)
    history = [{"role": "user", "content": "What are the criteria for ARDS?"},
               {"role": "assistant", "content": ANSWER, "proposed_action": False}]
    replayed = _replayed(_build_chat_messages("What about steroids?", "",
                                              history, scope))
    assert replayed and SAGE_FAST_MARKER_NOACT not in replayed[-1]


@pytest.mark.parametrize("scope", ["simple", "clinical", "patient"])
def test_every_marker_scope_gets_the_synthesis(scope):
    history = [{"role": "user", "content": "What are the criteria for ARDS?"},
               {"role": "assistant", "content": ANSWER, "proposed_action": False}]
    replayed = _replayed(_build_chat_messages("What about steroids?", "",
                                              history, scope))
    assert replayed[-1].startswith(SAGE_FAST_MARKER_NOACT), scope


def test_the_xml_contract_rollback_takes_the_synthesis_with_it(monkeypatch):
    """ORB_SAGE_XML_CONTRACT=1 restores a contract with no marker in it, so a
    replayed `NOACT|` there is scaffolding from a contract the model is no longer
    being asked to follow."""
    monkeypatch.setenv("ORB_SAGE_XML_CONTRACT", "1")
    history = [{"role": "user", "content": "What are the criteria for ARDS?"},
               {"role": "assistant", "content": ANSWER, "proposed_action": False}]
    replayed = _replayed(_build_chat_messages("What about steroids?", "",
                                              history, "clinical"))
    assert SAGE_FAST_MARKER_NOACT not in replayed[-1]


def test_a_trailing_marker_is_moved_to_the_head_never_duplicated():
    """The 2026-08-04 rule (a misplaced marker must not be replayed as an
    example) and this one meet on the same turn: the trailing marker is still
    removed, and the one the contract asks for opens the turn instead."""
    out = _history_turn_text(f"{ANSWER}\nNOACT|", 400, keep_marker=True,
                             proposed_action=False)
    assert out == f"{SAGE_FAST_MARKER_NOACT} {ANSWER}"
    assert out.count(SAGE_FAST_MARKER_NOACT) == 1


def test_the_legacy_xml_branch_gets_the_same_treatment():
    """A turn stored under the old XML contract unwraps to bare prose, which is
    the same markerless precedent by a different route. Replayed on a lane that
    now runs the marker contract, it leaks the same way."""
    raw = (f"<action_intent>None.</action_intent>"
           f"<concise_answer>{ANSWER}</concise_answer>")
    out = _history_turn_text(raw, 400, keep_marker=True, proposed_action=False)
    assert out == f"{SAGE_FAST_MARKER_NOACT} {ANSWER}"


def test_synthesis_never_manufactures_a_turn_out_of_nothing():
    """An empty or scaffolding-only turn contributes no message at all. A bare
    `NOACT|` with no answer after it is the worst possible example."""
    for raw in ("", "   ", "NOACT|", "<details><caveats>None.</caveats></details>"):
        assert _history_turn_text(raw, 400, keep_marker=True,
                                  proposed_action=False) == ""


def test_the_rollback_switch_restores_the_previous_bytes(monkeypatch):
    """ORB_SAGE_HISTORY_MARKER=off must be exactly the pre-change behaviour, not
    approximately it."""
    monkeypatch.setenv("ORB_SAGE_HISTORY_MARKER", "off")
    for raw in (ANSWER, f"{ANSWER}\nNOACT|", f"NOACT| {ANSWER}"):
        assert _history_turn_text(raw, 400, keep_marker=True,
                                  proposed_action=False) == \
            _history_turn_text(raw, 400, keep_marker=True, proposed_action=None)


# ══════════════════════════════════════════════════════════════════════
# 2. Reading the action state — both gates, or neither is safe
# ══════════════════════════════════════════════════════════════════════

def test_declares_action_has_exactly_one_copy():
    """The route decides the action-state override with this predicate and the
    history path decides replay with it. Two copies drifting apart is how a
    mutation turn becomes a "no action here" example."""
    assert sage_routes.declares_action is declares_action


@pytest.mark.parametrize("intent,expected", [
    ("", False), ("None.", False), ("none", False), ("NONE.", False),
    ("   ", False), (None, False),
    ('{"action_type": "add_medication"}', True), ("add paracetamol", True),
])
def test_declares_action_reads_the_contract_vocabulary(intent, expected):
    assert declares_action(intent) is expected


@pytest.mark.parametrize("blob,expected", [
    (_blob(proposed_action=False, parsed_response={"action_intent": "None."}), False),
    (_blob(proposed_action=True, parsed_response={"action_intent": "None."}), True),
    # Legacy rows carry only the model's declaration.
    (_blob(parsed_response={"action_intent": "None."}), False),
    (_blob(parsed_response={"action_intent": ""}), False),
    (_blob(parsed_response={"action_intent": '{"action_type": "x"}'}), True),
    # Unknown, every way a row gets there.
    (_blob(parsed_response={}), None),
    (_blob(timing={}), None),
    ("not json at all", None),
    (None, None),
    ("", None),
])
def test_the_stored_verdict_is_read_newest_first(blob, expected):
    """The resolved `proposed_action` is authoritative because it was written
    with BOTH gates known; `parsed_response.action_intent` is the best an older
    row can do, and neither present means unknown."""
    assert _stored_turn_proposed_action(blob) is expected


def test_the_deterministic_gate_alone_can_lock_a_legacy_row():
    """The hole in reading the declaration only: the fast tier declares nothing,
    so every mutation it has handled was locked on the QUERY with action_intent
    left at "None.". An older row therefore says False about a turn that
    proposed a chart change."""
    row = {"role": "assistant", "content": MUTATION_NARRATION,
           "proposed_action": _stored_turn_proposed_action(
               _blob(parsed_response={"action_intent": "None."}))}
    assert row["proposed_action"] is False, "the stored declaration alone is wrong here"
    assert _history_turn_proposed_action(
        row, {"role": "user", "content": MUTATION_QUERY}) is True


def test_the_deterministic_gate_never_lowers_an_unknown_to_false():
    """It can only raise. A turn with no recorded verdict stays unknown however
    innocent its question looked, because "no evidence of a mutation" is not
    evidence of none."""
    unknown = {"role": "assistant", "content": ANSWER}
    assert _history_turn_proposed_action(
        unknown, {"role": "user", "content": "What about steroids?"}) is None


def test_a_user_turn_is_never_given_an_action_state():
    assert _history_turn_proposed_action(
        {"role": "user", "content": MUTATION_QUERY}, None) is None


# ══════════════════════════════════════════════════════════════════════
# 3. End to end — a real stored turn, read back, assembled
# ══════════════════════════════════════════════════════════════════════

def _save_exchange(query: str, answer: str, blob: str | None):
    save_conversation(USER, "user", query, conversation_id=THREAD)
    save_conversation(USER, "assistant", answer, sources=blob,
                      conversation_id=THREAD)


def _assemble(followup="What about steroids?", scope="clinical"):
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    assert history, "seeded turns must come back"
    return _build_chat_messages(followup, "", history, scope)


def test_a_stored_fast_turn_reaches_the_next_prompt_with_a_marker():
    """The whole path: the row the fast lane writes, read back through
    get_conversation_history, assembled into the careful lane's next prompt."""
    _save_exchange("What are the diagnostic criteria for ARDS?", ANSWER,
                   _blob(proposed_action=False,
                         parsed_response={"action_intent": "None."}))
    assert _replayed(_assemble())[-1].startswith(SAGE_FAST_MARKER_NOACT)


def test_a_stored_mutation_turn_with_nothing_safe_to_say_is_replayed_bare():
    """The fallback, and the shape section 6 replaces where it can. With no
    stored safe message there is nothing honest to put a marker in front of, so
    this row keeps the pre-2026-09-07 behaviour rather than inventing one."""
    _save_exchange(MUTATION_QUERY, MUTATION_NARRATION,
                   _blob(proposed_action=True,
                         parsed_response={"action_intent": "None."}))
    replayed = _replayed(_assemble())[-1]
    assert not replayed.startswith(SAGE_FAST_MARKER_NOACT)
    assert SAGE_FAST_MARKER_NOACT not in replayed


def test_get_conversation_history_carries_the_verdict_not_the_audit_blob():
    """Only the one bit prompt assembly needs travels with the history. The blob
    is the whole audit record — the parsed reply, the retrieved document ids —
    and it has never been on this path."""
    _save_exchange("What are the diagnostic criteria for ARDS?", ANSWER,
                   _blob(proposed_action=False, retrieved_sources=["SEP-001"],
                         parsed_response={"action_intent": "None."}))
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    assert history[-1]["proposed_action"] is False
    assert history[0]["proposed_action"] is None, "a user turn has no verdict"
    assert "sources" not in history[-1]
    assert all("SEP-001" not in json.dumps(m) for m in history)


def test_a_legacy_mutation_turn_is_caught_by_its_own_question_in_the_window():
    """No backfill exists for rows written before the verdict was stored, so the
    deterministic gate is re-run against the question that turn answered."""
    _save_exchange(MUTATION_QUERY, MUTATION_NARRATION,
                   _blob(parsed_response={"action_intent": "None."}))
    replayed = _replayed(_assemble())[-1]
    assert SAGE_FAST_MARKER_NOACT not in replayed


def test_the_window_slice_does_not_hide_the_question_that_locked_a_turn():
    """`simple` keeps only the last 4 messages. A turn at the head of that slice
    still has its own question one row earlier in the FULL history, and the gate
    is asked there — not against the slice, which would silently clear it."""
    _save_exchange("What is hypoxia?", "Hypoxia is low tissue oxygenation.",
                   _blob(parsed_response={"action_intent": "None."}))
    _save_exchange(MUTATION_QUERY, MUTATION_NARRATION,
                   _blob(parsed_response={"action_intent": "None."}))
    _save_exchange("What dose?", "Paracetamol 1g QDS is the usual adult dose.",
                   _blob(parsed_response={"action_intent": "None."}))
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    # 6 rows, a 4-message window: the mutation turn is at window index 1 and its
    # own question is at window index 0 only by luck of this arithmetic — assert
    # the case the slice really does cut.
    messages = _build_chat_messages("What dose?", "", history[1:], "simple")
    replayed = _replayed(messages)
    locked = [m for m in replayed if MUTATION_NARRATION in m]
    assert locked, "the mutation turn must still be in the replayed window"
    assert SAGE_FAST_MARKER_NOACT not in locked[0]


def test_the_rollback_switch_restores_byte_identical_assembly(monkeypatch):
    _save_exchange("What are the diagnostic criteria for ARDS?", ANSWER,
                   _blob(proposed_action=False,
                         parsed_response={"action_intent": "None."}))
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    stripped = [{k: v for k, v in m.items() if k != "proposed_action"}
                for m in history]
    monkeypatch.setenv("ORB_SAGE_HISTORY_MARKER", "off")
    with_flag = _build_chat_messages("What about steroids?", "", history, "clinical")
    unknown = _build_chat_messages("What about steroids?", "", stripped, "clinical")
    assert json.dumps(with_flag, sort_keys=True) == json.dumps(unknown, sort_keys=True)


# ══════════════════════════════════════════════════════════════════════
# 4. The summarized lane is a lane
# ══════════════════════════════════════════════════════════════════════

def test_a_summarized_turn_gets_the_same_marker_rule(monkeypatch):
    """Summaries are generated with keep_marker=False, so a summarized assistant
    turn is a markerless precedent by construction — the same shape a fast turn
    is. Without this, switching ORB_SAGE_HISTORY_SUMMARIZE on quietly puts 7a
    back."""
    import config
    monkeypatch.setattr(config, "SAGE_HISTORY_SUMMARIZE", True, raising=False)
    history = [
        {"role": "user", "content": "What are the diagnostic criteria for ARDS?"},
        {"role": "assistant", "content": ANSWER, "proposed_action": False,
         "turn_id": 1, "conversation_id": THREAD,
         "summary": "ARDS is diagnosed by the Berlin criteria."},
        {"role": "user", "content": MUTATION_QUERY},
        {"role": "assistant", "content": MUTATION_NARRATION, "proposed_action": True,
         "turn_id": 2, "conversation_id": THREAD,
         "summary": "Asked to add paracetamol to the chart."},
        {"role": "user", "content": "And the P/F ratio cutoff?"},
        {"role": "assistant", "content": "300 mmHg on PEEP 5.", "proposed_action": False,
         "turn_id": 3, "conversation_id": THREAD, "summary": "P/F under 300."},
    ]
    replayed = _replayed(_build_chat_messages("What about steroids?", "",
                                              history, "clinical"))
    summarized = [m for m in replayed if m.endswith("Berlin criteria.")]
    assert summarized, "the oldest assistant turn must be served from its summary"
    assert summarized[0].startswith(SAGE_FAST_MARKER_NOACT)
    mutation = [m for m in replayed if "paracetamol" in m and "Asked to" in m]
    assert mutation, "the mutation turn must still be replayed"
    assert SAGE_FAST_MARKER_NOACT not in mutation[0]


# ══════════════════════════════════════════════════════════════════════
# 5. The writer — a real turn through the route records its own verdict
# ══════════════════════════════════════════════════════════════════════
# The reader above is only as good as what the route stores, and the field it
# needs did not exist until this change. These drive /api/sage/query with a
# stubbed retrieval and model, and read the row back out of the DB.

def _stub_chat(monkeypatch, chunks, *, scope="clinical", model="medgemma:4b"):
    def _ctx(query, user, patient_id=None, history=None):
        return ("CONTEXT BLOCK", scope, [], "high", False, None, "GENERAL_MEDICAL")

    async def _llm(*args, **kwargs):
        route_info = kwargs.get("route_info")
        if route_info is not None:
            route_info.update({"provider": "stub", "model": model,
                               "thinking": False, "thinking_tier": "fast"})
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr("routes.sage_routes.get_relevant_context", _ctx)
    monkeypatch.setattr("routes.sage_routes.query_ollama", _llm)


def _login() -> dict:
    r = client.post("/api/auth/login",
                    json={"employee_id": USER, "password": "password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _last_blob() -> dict:
    with db_session() as db:
        row = db.execute(
            "SELECT sources FROM sage_conversations WHERE user_id = ? "
            "AND role = 'assistant' ORDER BY id DESC LIMIT 1", (USER,)).fetchone()
    assert row is not None and row["sources"], "the turn must have been stored"
    return json.loads(row["sources"])


def _ask(query: str, headers) -> None:
    r = client.post("/api/sage/query", headers=headers,
                    json={"query": query, "conversation_id": THREAD})
    assert r.status_code == 200, r.text
    r.text  # drain the stream so the generator's finally block runs


def test_a_plain_turn_records_that_it_proposed_nothing(monkeypatch):
    _stub_chat(monkeypatch, ("Steroids are ", "sometimes used."))
    _ask("What about steroids?", _login())
    assert _last_blob()["proposed_action"] is False


def test_a_deterministic_mutation_records_true_though_the_model_declared_nothing(
        monkeypatch):
    """THE HOLE THIS CLOSES. The fast tier emits no marker, so the parser reads
    its reply as action_intent "None." — while the deterministic gate has
    locked the turn. A reader that trusted the declaration alone would call this
    turn action-free and hand its narration to the next prompt as a `NOACT|`
    example."""
    _stub_chat(monkeypatch, (MUTATION_NARRATION,))
    _ask(MUTATION_QUERY, _login())
    blob = _last_blob()
    assert blob["proposed_action"] is True
    assert not declares_action(blob["parsed_response"].get("action_intent")), \
        "the model declared nothing here; that is the whole point"
    assert _stored_turn_proposed_action(json.dumps(blob)) is True


def test_a_model_declared_act_records_true(monkeypatch):
    _stub_chat(monkeypatch,
               ('ACT|{"action_type": "add_medication", "medication": "Paracetamol"} ',
                MUTATION_NARRATION))
    _ask("the patient could use something for the fever", _login())
    assert _last_blob()["proposed_action"] is True


def test_the_turn_a_route_stored_is_replayed_the_way_the_route_decided(
        monkeypatch):
    """Writer to reader, end to end: a mutation turn the route locked comes back
    out of get_conversation_history as a mutation, and reaches the next prompt as
    the SAFE MESSAGE the route showed the clinician, not the narration the model
    drafted and the route threw away."""
    _stub_chat(monkeypatch, (MUTATION_NARRATION,))
    _ask(MUTATION_QUERY, _login())
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    assert history[-1]["proposed_action"] is True
    assert history[-1]["safe_answer"], "the route must have stored what it showed"
    replayed = _replayed(_build_chat_messages("What dose?", "", history, "clinical"))[-1]
    assert history[-1]["safe_answer"] in replayed
    assert "paracetamol 1g QDS to the chart" not in replayed
    assert _leading_marker(replayed)[0], "and it carries a marker like any turn"


def test_the_route_stores_the_answer_it_showed_beside_the_one_it_replaced(
        monkeypatch):
    """`sage_conversations.content` stays the RAW narration, because that is the
    provenance record of what the model actually said. The blob carries what the
    clinician read. Both, separately, or the replay lane has to guess."""
    _stub_chat(monkeypatch, (MUTATION_NARRATION,))
    _ask(MUTATION_QUERY, _login())
    blob = _last_blob()
    assert blob["safe_answer"], "a mutation turn must record its safe message"
    assert blob["safe_answer"] != MUTATION_NARRATION
    assert "not currently supported" in blob["safe_answer"] \
        or "Drafted request" in blob["safe_answer"] \
        or "not available in the pharmacy database" in blob["safe_answer"]
    with db_session() as db:
        row = db.execute(
            "SELECT content FROM sage_conversations WHERE user_id = ? "
            "AND role = 'assistant' ORDER BY id DESC LIMIT 1", (USER,)).fetchone()
    assert MUTATION_NARRATION in row["content"], "the raw record must survive"


def test_a_plain_turn_records_no_safe_message(monkeypatch):
    """`safe_answer` is set ONLY where the override rewrote the answer, so its
    presence is itself the statement "content is not what was shown"."""
    _stub_chat(monkeypatch, ("Steroids are ", "sometimes used."))
    _ask("What about steroids?", _login())
    assert _last_blob()["safe_answer"] is None


# ══════════════════════════════════════════════════════════════════════
# 6. A mutation turn is replayed as what the CLINICIAN read (item 10)
# ══════════════════════════════════════════════════════════════════════
# Section 5 above pinned the compromise this replaces: a mutation turn kept the
# defect, because its stored content is the model's drafted narration and no
# marker could honestly be put in front of that. Substituting the safe message
# removes the objection rather than living with it, and the turn rejoins the
# contract. Which marker opens it was measured, not argued: see section 7a of
# docs/perf/FAST_LANE_MARKER_2026-09-07.md.

SAFE_ANSWER = ("Drafted request for Paracetamol. Please review and approve to "
               "send to the Pharmacy.")


def test_a_mutation_turn_is_replayed_as_the_safe_message(monkeypatch):
    out = _history_turn_text(MUTATION_NARRATION, 400, keep_marker=True,
                             proposed_action=True, safe_answer=SAFE_ANSWER)
    assert SAFE_ANSWER in out
    assert _leading_marker(out)[0], "and it can now carry a marker"


def test_the_drafted_narration_never_reaches_the_next_prompt():
    """THE POINT. "I'll add paracetamol to the chart" is a claim Orb overrode
    before anyone saw it. It is kept in sage_conversations.content as the record
    of what the model said; it is not what the model should be shown next."""
    out = _history_turn_text(MUTATION_NARRATION, 400, keep_marker=True,
                             proposed_action=True, safe_answer=SAFE_ANSWER)
    assert MUTATION_NARRATION not in out
    assert "add paracetamol" not in out.lower()


@pytest.mark.parametrize("switch,expected", [
    ("noact", f"{SAGE_FAST_MARKER_NOACT} {SAFE_ANSWER}"),
    ("act", f'ACT|{{"action_type": "chart_mutation"}} {SAFE_ANSWER}'),
    ("off", SAFE_ANSWER),
])
def test_the_mutation_marker_switch(monkeypatch, switch, expected):
    """`off` still substitutes the content: the two halves are separable because
    only one of them is a question about the contract."""
    monkeypatch.setenv("ORB_SAGE_HISTORY_MUTATION_MARKER", switch)
    assert _history_turn_text(MUTATION_NARRATION, 400, keep_marker=True,
                              proposed_action=True,
                              safe_answer=SAFE_ANSWER) == expected


def test_the_default_mutation_marker_is_the_measured_one(monkeypatch):
    """`act`, and it is a MEASURED default rather than a reasoned one. On a
    benign follow-up the feared copied-ACT was 0 of 10; on a real mutation
    follow-up `noact` silenced the model's own ACT| 10 times out of 10. Flipping
    this without re-running dev_tools/check_history_marker_replay.py --suite
    mutation is changing a result, not a preference."""
    monkeypatch.delenv("ORB_SAGE_HISTORY_MUTATION_MARKER", raising=False)
    out = _history_turn_text(MUTATION_NARRATION, 400, keep_marker=True,
                             proposed_action=True, safe_answer=SAFE_ANSWER)
    assert _leading_marker(out)[0] == "ACT|"


def test_the_replayed_act_payload_is_orbs_and_never_the_models(monkeypatch):
    """A drug and a dose replayed as an EXAMPLE is the one thing on this lane
    that could come back as a fabricated order. The payload says only that a
    chart action was proposed."""
    monkeypatch.setenv("ORB_SAGE_HISTORY_MUTATION_MARKER", "act")
    out = _history_turn_text(MUTATION_NARRATION, 400, keep_marker=True,
                             proposed_action=True, safe_answer=SAFE_ANSWER)
    marker, end = _leading_marker(out)
    assert marker == "ACT|"
    payload = json.loads(out[end:out.index("}") + 1])
    assert payload == {"action_type": "chart_mutation"}
    assert "1g" not in out and "QDS" not in out


def test_the_prose_lanes_get_the_safe_message_but_still_no_marker():
    """Which TEXT belongs in the next prompt is a question about the
    conversation, and broad/aggregate replay the same conversation. Only the
    marker is a contract question."""
    out = _history_turn_text(MUTATION_NARRATION, 400, keep_marker=False,
                             proposed_action=True, safe_answer=SAFE_ANSWER)
    assert out == SAFE_ANSWER
    assert SAGE_FAST_MARKER_NOACT not in out


def test_a_mutation_turn_with_no_stored_safe_message_is_replayed_bare():
    """A cancelled turn, or a row written before the message was stored. There
    is nothing safe to substitute, so this falls all the way back to the
    behaviour section 5 pinned, rather than inventing something."""
    out = _history_turn_text(MUTATION_NARRATION, 400, keep_marker=True,
                             proposed_action=True)
    assert out == MUTATION_NARRATION
    assert not _leading_marker(out)[0]


def test_the_rollback_switch_takes_the_substitution_with_it(monkeypatch):
    monkeypatch.setenv("ORB_SAGE_HISTORY_MARKER", "off")
    assert _history_turn_text(MUTATION_NARRATION, 400, keep_marker=True,
                              proposed_action=True,
                              safe_answer=SAFE_ANSWER) == MUTATION_NARRATION


@pytest.mark.parametrize("blob,expected", [
    (_blob(safe_answer=SAFE_ANSWER), SAFE_ANSWER),
    # An older row carries it only as the overridden concise_answer.
    (_blob(parsed_response={"concise_answer": SAFE_ANSWER}), SAFE_ANSWER),
    # Explicit wins over the legacy reading.
    (_blob(safe_answer=SAFE_ANSWER,
           parsed_response={"concise_answer": "stale"}), SAFE_ANSWER),
    (_blob(safe_answer="   "), None),
    (_blob(parsed_response={}), None),
    ("not json", None), (None, None),
])
def test_the_safe_message_is_read_newest_first(blob, expected):
    assert _stored_turn_safe_answer(blob) == expected


def test_a_mutation_turn_is_never_served_from_its_summary(monkeypatch):
    """The cached summary was generated FROM the narration, so serving it would
    only make the wrong content shorter. The safe message is already the
    compressed truthful version of that turn."""
    import config
    monkeypatch.setattr(config, "SAGE_HISTORY_SUMMARIZE", True, raising=False)
    history = [
        {"role": "user", "content": MUTATION_QUERY},
        {"role": "assistant", "content": MUTATION_NARRATION, "proposed_action": True,
         "safe_answer": SAFE_ANSWER, "turn_id": 1, "conversation_id": THREAD,
         "summary": "Added paracetamol to the chart."},
        {"role": "user", "content": "What are the criteria for ARDS?"},
        {"role": "assistant", "content": ANSWER, "proposed_action": False,
         "turn_id": 2, "conversation_id": THREAD, "summary": "Berlin criteria."},
        {"role": "user", "content": "And the P/F cutoff?"},
        {"role": "assistant", "content": "300 mmHg on PEEP 5.", "proposed_action": False,
         "turn_id": 3, "conversation_id": THREAD, "summary": "P/F under 300."},
    ]
    replayed = _replayed(_build_chat_messages("What about steroids?", "",
                                              history, "clinical"))
    mutation = [m for m in replayed if "Paracetamol" in m or "paracetamol" in m]
    assert mutation, "the mutation turn must still be replayed"
    assert SAFE_ANSWER in mutation[0]
    assert "Added paracetamol to the chart." not in mutation[0], \
        "the summary of the narration is the wrong content, shorter"


def test_a_stored_mutation_turn_reaches_the_next_prompt_as_the_safe_message():
    """The DB round trip, on a row carrying both readings."""
    _save_exchange(MUTATION_QUERY, MUTATION_NARRATION,
                   _blob(proposed_action=True, safe_answer=SAFE_ANSWER,
                         parsed_response={"action_intent": "None."}))
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    assert history[-1]["safe_answer"] == SAFE_ANSWER
    replayed = _replayed(_assemble())[-1]
    assert SAFE_ANSWER in replayed
    assert MUTATION_NARRATION not in replayed
