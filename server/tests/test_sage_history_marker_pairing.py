"""The synthesized `NOACT|` must not be spent on a verdict that is only half made.

A FOLLOW-UP TO SECTION 7a of docs/perf/FAST_LANE_MARKER_2026-09-07.md, found
verifying it. 7a synthesizes a leading `NOACT|` in front of a stored assistant
turn that has none, gated on the turn's recorded action state so that a MUTATION
turn's raw narration is never labelled no-action. That gate has two halves:

    the model's own `ACT|` declaration        stored on the row
    `detect_mutation_intent` on the question  RE-RUN here, against `previous`

A row written before 2026-09-07 carries the first half only, because
`proposed_action` did not exist yet, and on the fast lane the first half is
always "None." — that tier declares nothing. So a legacy row's False is not a
verdict, it is half of one, and `_history_turn_proposed_action` finishes it by
running the deterministic gate against the question the turn answered.

THE HOLE: that question is not always in view, and the code spent the half
anyway.

    get_conversation_history          cuts at `LIMIT 6`, on a ROW boundary
    routes.sage_routes._save_turn     writes the user row and SKIPS the
                                      assistant row when the response is empty

so one aborted turn makes the parity odd, and the oldest row that comes back can
be an assistant turn whose question is row seven. `previous` is then None, the
deterministic gate never runs, and "I'll add paracetamol 1g QDS to the chart for
you." reached the next prompt as `NOACT| I'll add paracetamol 1g QDS to the
chart for you.` — the exact example 7a's gate exists to keep out of it.

Measured 2026-09-07 on the shipped code, this same fixture:

    scope `patient`   REPRODUCES   (6-message window keeps the orphaned row)
    scope `clinical`  REPRODUCES   (same window)
    scope `simple`    does not     (4-message window slices it off, which is
                                    arithmetic, not a guard, and is why this
                                    file sweeps all three)

THE FIX. `_stored_turn_verdict` now reports whether a row's verdict was RESOLVED
by the route with both gates known, or derived from the declaration alone; an
unresolved False with no question in view is unknown, not False. A resolved
False is untouched, so nothing 7a fixed is given back.

PROVE-CAN-FAIL: delete the `proposed_action_resolved` clause from
`services/sage_service._history_turn_proposed_action` and
test_a_legacy_mutation_turn_whose_question_was_cut_is_not_cleared goes red on
`patient` and `clinical`.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import db_session
from services import sage_history_service
from services.sage_service import (
    SAGE_FAST_MARKER_NOACT,
    _build_chat_messages,
    _history_turn_proposed_action,
    _stored_turn_verdict,
    get_conversation_history,
    save_conversation,
)

USER = "DOC001"          # seeded by conftest._reseed_users
THREAD = "hist-marker-pairing"

#: What a mutation turn stores: the model's own narration. The clinician saw the
#: action-state override's safe message instead; the row kept this.
MUTATION_NARRATION = "I'll add paracetamol 1g QDS to the chart for you."
MUTATION_QUERY = "add paracetamol to the chart"

#: A row written before `proposed_action` existed. `action_intent` is "None."
#: because the model declared nothing, which on the fast lane it never does.
LEGACY_BLOB = json.dumps({"parsed_response": {"action_intent": "None."}})

#: Scopes and their history windows (`_build_chat_messages`: 4 for simple, 6
#: otherwise). Swept together on purpose: `simple` does not reproduce, and a
#: single-scope test would have called the defect fixed.
SCOPES = ("simple", "patient", "clinical")


@pytest.fixture(autouse=True)
def _clean_thread():
    sage_history_service.reset()
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (USER,))
    yield
    sage_history_service.reset()
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (USER,))


def _exchange(query: str, answer: str, blob: str | None = LEGACY_BLOB):
    save_conversation(USER, "user", query, conversation_id=THREAD)
    save_conversation(USER, "assistant", answer, sources=blob,
                      conversation_id=THREAD)


def _orphan(query: str):
    """An ABORTED turn. `_save_turn` writes the user row and skips the assistant
    row when `full_response` is empty, which is what shifts the row parity."""
    save_conversation(USER, "user", query, conversation_id=THREAD)


def _seed_with_the_question_cut():
    """Seven rows, so `LIMIT 6` drops the mutation turn's own question."""
    _exchange(MUTATION_QUERY, MUTATION_NARRATION)          # rows 1-2
    _orphan("What is hypoxia?")                            # row 3
    _exchange("What dose?", "Paracetamol 1g QDS.")         # rows 4-5
    _exchange("What about steroids?", "Steroids are considered in ARDS.")  # 6-7


def _assistant_turns(messages):
    return [m["content"] for m in messages[:-1] if m["role"] == "assistant"]


# ══════════════════════════════════════════════════════════════════════
#  The defect
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("scope", SCOPES)
def test_a_legacy_mutation_turn_whose_question_was_cut_is_not_cleared(scope):
    """THE REGRESSION. The narration must never be replayed under `NOACT|`.

    Asserted on the narration specifically rather than on "no marker anywhere":
    the other turns in this window are ordinary answers and SHOULD get the
    synthesized marker, so a blanket assertion would pass by breaking 7a.
    """
    _seed_with_the_question_cut()
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    replayed = _assistant_turns(_build_chat_messages("And in renal failure?", "",
                                                     history, scope))
    narration = [m for m in replayed if MUTATION_NARRATION[:22] in m]
    for turn in narration:
        assert not turn.startswith(SAGE_FAST_MARKER_NOACT), (
            f"[{scope}] a mutation narration was replayed as a no-action example")


@pytest.mark.parametrize("scope", SCOPES)
def test_the_fix_does_not_cost_the_other_turns_their_marker(scope):
    """The cost side. 7a's whole point is that ordinary turns DO get the marker,
    so a fix that silenced the synthesis everywhere would pass the test above and
    give back the thing it was written to protect."""
    _seed_with_the_question_cut()
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    replayed = _assistant_turns(_build_chat_messages("And in renal failure?", "",
                                                     history, scope))
    ordinary = [m for m in replayed if MUTATION_NARRATION[:22] not in m]
    assert ordinary, f"[{scope}] the fixture produced no ordinary turns to check"
    assert all(m.startswith(SAGE_FAST_MARKER_NOACT) for m in ordinary), (
        f"[{scope}] an ordinary turn lost its synthesized marker: {ordinary}")


def test_the_fixture_really_does_cut_the_question():
    """The repro's own precondition. Without this the tests above could pass
    because the seed changed shape, not because the code is right — the
    orphan-user row is what makes the parity odd, and `LIMIT 6` is what cuts."""
    _seed_with_the_question_cut()
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    assert history[0]["role"] == "assistant", (
        "the oldest row returned should be an assistant turn with no question "
        f"in front of it, got {[m['role'] for m in history]}")
    assert MUTATION_NARRATION[:22] in history[0]["content"]
    assert not any(m["role"] == "user" and MUTATION_QUERY in m["content"]
                   for m in history), "the question was supposed to be cut"


# ══════════════════════════════════════════════════════════════════════
#  The rule, stated directly
# ══════════════════════════════════════════════════════════════════════

def test_an_unresolved_false_with_no_question_in_view_is_unknown():
    legacy = {"role": "assistant", "content": MUTATION_NARRATION,
              "proposed_action": False, "proposed_action_resolved": False}
    assert _history_turn_proposed_action(legacy, None) is None
    assert _history_turn_proposed_action(
        legacy, {"role": "assistant", "content": "an earlier answer"}) is None


def test_an_unresolved_false_is_still_spent_when_the_question_is_there():
    """Unchanged behaviour: the second gate ran, said no, and that is a verdict."""
    legacy = {"role": "assistant", "content": "Hypoxia is low tissue oxygenation.",
              "proposed_action": False, "proposed_action_resolved": False}
    assert _history_turn_proposed_action(
        legacy, {"role": "user", "content": "What is hypoxia?"}) is False


def test_a_resolved_false_is_not_weakened_by_any_of_this():
    """The route wrote it with both gates known, so a missing question proves
    nothing about it. Narrowing this to unknown would hand back 7a's fix on the
    first turn of every window that opens on an assistant row."""
    resolved = {"role": "assistant", "content": "Steroids are considered in ARDS.",
                "proposed_action": False, "proposed_action_resolved": True}
    assert _history_turn_proposed_action(resolved, None) is False


def test_a_declared_mutation_never_depends_on_the_question_being_in_view():
    locked = {"role": "assistant", "content": MUTATION_NARRATION,
              "proposed_action": True, "proposed_action_resolved": True}
    assert _history_turn_proposed_action(locked, None) is True


@pytest.mark.parametrize("blob,verdict,resolved", [
    (json.dumps({"proposed_action": False}), False, True),
    (json.dumps({"proposed_action": True}), True, True),
    (json.dumps({"parsed_response": {"action_intent": "None."}}), False, False),
    (json.dumps({"parsed_response": {"action_intent": '{"action_type":"order"}'}}),
     True, False),
    (json.dumps({"parsed_response": {}}), None, False),
    (json.dumps({}), None, False),
    (None, None, False),
    ("not json", None, False),
])
def test_the_verdict_reports_its_own_provenance(blob, verdict, resolved):
    """`resolved` is the whole point: it is what tells the reader whether the
    False in front of it is an answer or half of one."""
    assert _stored_turn_verdict(blob) == (verdict, resolved)


def test_the_history_read_carries_the_provenance_through():
    """The bit is useless if it does not survive get_conversation_history."""
    _exchange("What is hypoxia?", "Hypoxia is low tissue oxygenation.",
              LEGACY_BLOB)
    _exchange("What about steroids?", "Steroids are considered in ARDS.",
              json.dumps({"proposed_action": False}))
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    legacy, resolved = history[1], history[3]
    assert (legacy["proposed_action"], legacy["proposed_action_resolved"]) == (False, False)
    assert (resolved["proposed_action"], resolved["proposed_action_resolved"]) == (False, True)
