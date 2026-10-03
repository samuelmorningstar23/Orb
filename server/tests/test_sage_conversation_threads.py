"""Prompt history must not cross conversations.

WHY THIS FILE EXISTS. ``get_conversation_history`` was keyed on the USER alone,
and the query route prepended that clinician's last six turns to EVERY prompt.
The Sage panel's "New Chat" cleared its own message array and nothing else, so
the clinician saw an empty thread while the model still read the previous one.

Observed in the browser 2026-08-08: a fresh login went straight to Sage, opened
no chart, selected no patient, and asked a purely general question — "What is
the diagnostic threshold for diabetic ketoacidosis?" The answer named a specific
admitted patient and quoted a glucose value for them, and the provenance bar
read "<patient name> · 3 retrieved". The same question posted with no history
returned a clean, patient-free answer, twice.

That is a PHI-crossing problem on a shared ward workstation, not a cosmetic one,
and it is also why Sage got slower the longer a session ran: every turn re-paid
the prefill cost of the six before it.

The tests below pin the behaviour that fixes it AND the default that makes the
fix safe — a caller that forgets to pass a thread id gets NO history rather than
silently falling back to "everything this user ever said".
"""
import pytest

from database import db_session
from services.sage_service import (
    get_conversation_history,
    list_conversation_turns,
    save_conversation,
)

USER = "DOC001"


@pytest.fixture(autouse=True)
def _clean_sage_rows():
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (USER,))
    yield
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (USER,))


def _turn(cid, q, a):
    save_conversation(USER, "user", q, conversation_id=cid)
    save_conversation(USER, "assistant", a, conversation_id=cid)


# ── The live bug ──────────────────────────────────────────────────────────────

def test_a_new_thread_does_not_inherit_the_previous_one():
    _turn("thread-a", "What is Rajesh Iyer's NEWS2?",
          "Rajesh Iyer's NEWS2 is 9, driven by RR 28 and SpO2 88.")

    fresh = get_conversation_history(USER, limit=6, conversation_id="thread-b")

    assert fresh == [], "the previous conversation reached a new thread's prompt"
    joined = " ".join(m["content"] for m in fresh)
    assert "Rajesh Iyer" not in joined


def test_the_same_thread_still_carries_its_own_context():
    """The follow-up question ("and what should I do about it?") must work."""
    _turn("thread-a", "What is Rajesh Iyer's NEWS2?", "NEWS2 is 9.")

    same = get_conversation_history(USER, limit=6, conversation_id="thread-a")

    assert [m["role"] for m in same] == ["user", "assistant"]
    assert "NEWS2 is 9." in [m["content"] for m in same]


def test_missing_thread_id_yields_no_context_not_everything():
    """The safe default. A caller that forgets the id must not re-open the leak."""
    _turn("thread-a", "What is Rajesh Iyer's NEWS2?", "NEWS2 is 9.")

    assert get_conversation_history(USER, limit=6) == []
    assert get_conversation_history(USER, limit=6, conversation_id="") == []
    assert get_conversation_history(USER, limit=6, conversation_id=None) == []


def test_pre_thread_rows_are_never_selected_as_context():
    """Rows written before threads existed carry NULL — unknown thread, no reuse."""
    save_conversation(USER, "user", "legacy question about a patient")
    save_conversation(USER, "assistant", "legacy answer naming Rajesh Iyer")

    assert get_conversation_history(USER, limit=6, conversation_id="thread-a") == []


# ── What the fix must not break ───────────────────────────────────────────────

def test_recent_conversations_sidebar_still_sees_every_thread():
    """"New Chat" opens a thread; it must never delete the clinician's record."""
    _turn("thread-a", "Question A", "Answer A")
    _turn("thread-b", "Question B", "Answer B")

    listed = list_conversation_turns(USER, limit=20)
    contents = [m["content"] for m in listed]

    assert "Question A" in contents and "Question B" in contents
    assert {m["conversation_id"] for m in listed} == {"thread-a", "thread-b"}


def test_history_is_chronological_within_a_thread():
    _turn("thread-a", "first", "first answer")
    _turn("thread-a", "second", "second answer")

    rows = get_conversation_history(USER, limit=6, conversation_id="thread-a")
    assert [r["content"] for r in rows] == [
        "first", "first answer", "second", "second answer",
    ]
