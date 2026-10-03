"""ORB_SAGE_HISTORY_SUMMARIZE — conversation-history summarization, pinned.

History is the only GROWING term of the Sage prompt (604 → 1,238 tokens
measured across one session). services/sage_history_service.py lets older
turns reach the prompt as ≤~300-char summaries — but a generative rewrite now
sits between the transcript and what the clinical model reads on every
follow-up, so this file pins the properties that make that safe:

1. DETERMINISTIC FACT GUARD — plain-code extraction of doses, rates, vitals
   and scores; every extracted fact appears VERBATIM in the stored summary
   (missing ones appended on a ``FACTS:`` line), and over-budget output
   truncates PROSE, never facts. The model can drop a dose; the guard puts it
   back, byte-for-byte.
2. OFF IS TODAY — with the flag off (the shipped default: continuity Eval
   Groups 3+4 have not been re-run against summaries) prompt assembly is
   byte-identical to the pre-summarization code, verified against a literal
   copy of that code, including when stray summary keys are present.
3. ON IS BOUNDED — the 2 most recent messages stay verbatim; only older ones
   use summaries; a summary that is not ready falls back to the deterministic
   head-trim of the original.
4. NEVER ON THE CRITICAL PATH — generation is fire-and-forget at P3 through
   llm_queue (BackgroundGovernor sheds are a clean fallback, never an error),
   and an interactive turn completes while summarization is still pending.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import pytest

import config
from database import db_session
from services import llm_queue_service, sage_history_service, sage_service
from services.gpu_coordinator import PRIORITY_BACKGROUND
from services.sage_history_service import (
    SUMMARY_BUDGET_CHARS,
    VERBATIM_RECENT_TURNS,
    extract_facts,
    guard_summary,
    load_summaries,
    store_summary_sync,
)
from services.sage_service import (
    _build_chat_messages,
    _history_turn_text,
    _is_followup_turn,
    _xml_contract_forced,
    clear_sage_history,
    get_conversation_history,
    save_conversation,
)

USER = "DOC001"  # seeded by conftest._reseed_users
THREAD = "hist-summ-thread"


@pytest.fixture(autouse=True)
def _clean_state():
    sage_history_service.reset()
    with db_session() as db:
        sage_history_service._ensure_schema(db)
        db.execute("DELETE FROM sage_history_summaries")
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (USER,))
    yield
    sage_history_service.reset()
    with db_session() as db:
        db.execute("DELETE FROM sage_history_summaries")
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (USER,))


@pytest.fixture
def _flag_on(monkeypatch):
    monkeypatch.setattr(config, "SAGE_HISTORY_SUMMARIZE", True, raising=False)


class FakeQueue:
    """Records acquisitions; optionally sheds at admission like the governor."""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.acquired: list[tuple] = []
        self.released = 0

    async def acquire(self, priority=None, timeout=None):
        self.acquired.append((priority, timeout))
        if self.exc is not None:
            raise self.exc
        return True

    def release(self):
        self.released += 1


async def _not_evicted(model):
    return False


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Zero the shed-retry backoff for every test in this file.

    A shed is retried (see SUMMARIZE_RETRY_BACKOFF_SECONDS); without this the
    all-shed paths would sleep the real 5 s + 15 s. The schedule's SHAPE is
    asserted separately in test_retry_backoff_matches_attempt_count.
    """
    monkeypatch.setattr(
        sage_history_service, "SUMMARIZE_RETRY_BACKOFF_SECONDS",
        (0.0,) * (sage_history_service.SUMMARIZE_MAX_ATTEMPTS - 1))


# ── 1. Deterministic fact extraction ─────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # Doses, including a range as ONE fact
    ("Give ceftriaxone 2 g IV once daily and paracetamol 500-1000 mg PO q6h.",
     ["2 g", "500-1000 mg"]),
    # Infusion rate + titration target, sentence-final period
    ("Start noradrenaline at 0.1 mcg/kg/min, titrate to MAP 65.",
     ["0.1 mcg/kg/min", "MAP 65"]),
    # Scores, vitals, percentages, blood pressure pair
    ("NEWS2 is 7, driven by RR 28 and SpO2 88%. BP was 90/60.",
     ["NEWS2 is 7", "RR 28", "88%", "90/60"]),
    # Labs with and without units
    ("Sodium 128 mmol/L, glucose 320 mg/dL, lactate 4.2.",
     ["128 mmol/L", "320 mg/dL", "lactate 4.2"]),
    # Weight-based bolus + insulin units
    ("Bolus 30 mL/kg crystalloid; then 10 units insulin.",
     ["30 mL/kg", "10 units"]),
    # Named vitals; GCS with denominator
    ("Temp 38.5, HR 110 bpm, GCS 13/15.",
     ["Temp 38.5", "HR 110", "GCS 13/15"]),
    ("Hydrocortisone 50 mg IV q6h. Vitamin D 1000 IU daily.",
     ["50 mg", "1000 IU"]),
    ("KCl 40 mEq in 500 ml over 4 hr.", ["40 mEq", "500 ml"]),
])
def test_fact_extraction_vectors(text, expected):
    got = extract_facts(text)
    for fact in expected:
        assert any(fact in g for g in got), (
            f"fact {fact!r} not extracted from {text!r}; got {got}")


@pytest.mark.parametrize("text", [
    "No numbers here at all.",
    "Vitamin B12 levels look fine.",
    "Reviewed on 12/08/2026 in the morning.",  # a date is not a clinical fact
    "",
])
def test_fact_extraction_negatives(text):
    assert extract_facts(text) == []


def test_extracted_facts_are_verbatim_substrings_of_the_original():
    """The guard's whole promise rests on facts being copy-paste substrings."""
    text = "Start noradrenaline at 0.1 mcg/kg/min; NEWS2 is 7, BP 90/60."
    for fact in extract_facts(text):
        assert fact in text


# ── 2. The fact guard ────────────────────────────────────────────────────────

def test_guard_appends_missing_facts_verbatim():
    facts = ["500 mg", "BP 120/80", "NEWS2 7"]
    out = guard_summary("Discussed sepsis antibiotics.", facts)
    for f in facts:
        assert f in out, f"fact {f!r} lost by the guard"
    assert "FACTS:" in out


def test_guard_does_not_duplicate_facts_already_present():
    out = guard_summary("Advised ceftriaxone 2 g IV daily.", ["2 g"])
    assert out == "Advised ceftriaxone 2 g IV daily."
    assert "FACTS:" not in out


def test_guard_on_empty_summary_is_facts_only():
    assert guard_summary("", ["500 mg"]) == "FACTS: 500 mg"
    assert guard_summary("", []) == ""


def test_facts_survive_prose_truncation():
    """A fact sitting past the budget in the prose must re-surface on the
    FACTS line after truncation — truncate PROSE, never facts."""
    prose = ("The clinician asked about empirical cover and de-escalation "
             "timing and was walked through the local policy in detail. " * 5
             + "The agreed dose was ceftriaxone 2 g IV daily.")
    assert len(prose) > SUMMARY_BUDGET_CHARS
    out = guard_summary(prose, ["2 g"])
    assert len(out) <= SUMMARY_BUDGET_CHARS
    assert "2 g" in out
    assert "FACTS: 2 g" in out  # it was truncated out of the prose, re-pinned


def test_facts_never_dropped_even_when_facts_alone_exceed_budget():
    many = [f"{i} mg" for i in range(100, 140)]
    out = guard_summary("prose that will be sacrificed", many, budget=100)
    for f in many:
        assert f in out, f"budget pressure dropped fact {f!r}"


def test_guard_output_is_bounded_when_facts_fit():
    out = guard_summary("x" * 2000, ["500 mg"])
    assert len(out) <= SUMMARY_BUDGET_CHARS
    assert "500 mg" in out


# ── 3. OFF = byte-identical assembly (regression pin) ────────────────────────

# Raw stored turns, the shapes sage_conversations actually holds: fast-contract
# markers, an XML-era turn, a <details> channel block, and an over-clamp tail.
RAW_THREAD = [
    {"role": "user", "content": "What is Rajesh Iyer's NEWS2?"},
    {"role": "assistant", "content": (
        "NOACT| NEWS2 is 7, driven by RR 28 and SpO2 88%."
        "<details>PATIENT FACTS: long channel summary…</details>")},
    {"role": "user", "content": "Should we start ceftriaxone?"},
    {"role": "assistant", "content": (
        "```xml\n<concise_answer>Ceftriaxone 2 g IV daily is standard for "
        "this indication.</concise_answer>\n```")},
    {"role": "user", "content": "What about steroids?"},
    {"role": "assistant", "content": (
        "NOACT| Hydrocortisone 50 mg IV q6h if shock is refractory. "
        + "Further discussion of taper strategy. " * 40)},
]


def _legacy_history_segment(prompt: str, history: list, scope: str) -> list:
    """A LITERAL copy of the pre-summarization assembly loop from
    _build_chat_messages (2026-08-10). If this and the live code ever disagree
    with the flag OFF, the OFF path has drifted — which is the one thing the
    default configuration promises cannot happen."""
    out = []
    if history and (scope != "simple" or _is_followup_turn(prompt)):
        keep, clamp = (4, 600) if scope == "simple" else (6, 1000)
        keep_marker = scope not in ("broad", "aggregate") and not _xml_contract_forced()
        for msg in history[-keep:]:
            content = _history_turn_text(
                msg.get("content") or "", clamp,
                keep_marker=keep_marker and msg.get("role") == "assistant",
            )
            if content:
                out.append({"role": msg["role"], "content": content})
    return out


@pytest.mark.parametrize("prompt,scope", [
    ("What about steroids?", "patient"),
    ("What dose?", "simple"),                       # follow-up → history flows
    ("What is the mechanism of aspirin?", "simple"),  # self-contained → none
    ("How busy are the wards today?", "broad"),
    ("Any pattern in admissions?", "aggregate"),
    ("Renal dosing for this?", "clinical"),
])
def test_off_path_assembly_is_byte_identical_to_legacy(prompt, scope):
    assert not sage_history_service.enabled(), "flag must default OFF"
    got = _build_chat_messages(prompt, "some clinical context", RAW_THREAD, scope)
    expected = (
        got[:2]                       # system + fenced context (not under test)
        + _legacy_history_segment(prompt, RAW_THREAD, scope)
        + [{"role": "user", "content": prompt}]
    )
    assert json.dumps(got, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_off_path_ignores_summary_keys_entirely():
    """Even a history whose dicts CARRY summaries (flag flipped off after
    running on) must assemble byte-identically to one that does not."""
    with_keys = [
        dict(m, summary="SHOULD NEVER APPEAR", turn_id=i + 1,
             conversation_id=THREAD)
        for i, m in enumerate(RAW_THREAD)
    ]
    got = _build_chat_messages("What about steroids?", "", with_keys, "patient")
    plain = _build_chat_messages("What about steroids?", "", RAW_THREAD, "patient")
    assert json.dumps(got, sort_keys=True) == json.dumps(plain, sort_keys=True)
    assert all("SHOULD NEVER APPEAR" not in m["content"] for m in got)


def test_off_path_history_rows_carry_no_summarization_keys():
    """What this protects is that SUMMARIZATION adds nothing to a row while the
    flag is off — turn_id, conversation_id and summary are the keys that must be
    absent. It was written as an exact key set; `proposed_action` (the turn's
    recorded action state, unrelated to this feature and always present since
    2026-09-07 — see tests/test_sage_history_marker.py) made that constant wrong
    while the invariant held, so the invariant is what is asserted now."""
    save_conversation(USER, "user", "q1", conversation_id=THREAD)
    save_conversation(USER, "assistant", "a1", conversation_id=THREAD)
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    assert history, "seeded turns must come back"
    for msg in history:
        assert not {"turn_id", "conversation_id", "summary"} & set(msg.keys())


# ── 4. ON = 2 recent verbatim + older summaries, head-trim fallback ─────────

def _summarized_thread():
    return [
        dict(m, turn_id=i + 1, conversation_id=THREAD,
             summary=f"S{i}: cached summary of turn {i}.")
        for i, m in enumerate(RAW_THREAD)
    ]


def test_on_path_serves_summaries_for_older_and_verbatim_for_recent(_flag_on):
    hist = _summarized_thread()
    msgs = _build_chat_messages("What about steroids?", "", hist, "patient")
    assembled = msgs[1:-1]  # strip system prompt and the user turn
    assert len(assembled) == 6

    older, recent = assembled[:-VERBATIM_RECENT_TURNS], assembled[-VERBATIM_RECENT_TURNS:]
    for i, m in enumerate(older):
        assert m["content"] == f"S{i}: cached summary of turn {i}."

    # The 2 most recent messages go through today's head-trim, unchanged.
    for m, raw in zip(recent, RAW_THREAD[-VERBATIM_RECENT_TURNS:]):
        expected = _history_turn_text(
            raw["content"], 1000, keep_marker=(raw["role"] == "assistant"))
        assert m["content"] == expected
        assert not m["content"].startswith("S")


def test_on_path_missing_summary_falls_back_to_head_trim(_flag_on):
    hist = _summarized_thread()
    hist[1]["summary"] = None    # not ready yet
    hist[2]["summary"] = "   "   # degenerate cache row → also a miss
    msgs = _build_chat_messages("What about steroids?", "", hist, "patient")
    assembled = msgs[1:-1]
    assert assembled[1]["content"] == _history_turn_text(
        RAW_THREAD[1]["content"], 1000, keep_marker=True)
    assert assembled[2]["content"] == _history_turn_text(
        RAW_THREAD[2]["content"], 1000, keep_marker=False)
    # Turns whose summaries WERE ready still use them.
    assert assembled[0]["content"] == "S0: cached summary of turn 0."
    assert assembled[3]["content"] == "S3: cached summary of turn 3."


def test_on_path_simple_scope_summarizes_only_its_shorter_window(_flag_on):
    hist = _summarized_thread()
    msgs = _build_chat_messages("What dose?", "", hist, "simple")
    assembled = msgs[1:-1]
    assert len(assembled) == 4  # simple keeps 4
    assert assembled[0]["content"] == "S2: cached summary of turn 2."
    assert assembled[1]["content"] == "S3: cached summary of turn 3."
    for m, raw in zip(assembled[2:], RAW_THREAD[-2:]):
        assert m["content"] == _history_turn_text(
            raw["content"], 600, keep_marker=(raw["role"] == "assistant"))


# ── 5. Cache: per (conversation, turn), hash-validated, PHI-clean ────────────

def _seed_thread(n_pairs=3):
    for i in range(n_pairs):
        save_conversation(USER, "user", f"question {i}", conversation_id=THREAD)
        save_conversation(USER, "assistant", f"answer {i} with {i+1} mg",
                          conversation_id=THREAD)
    with db_session() as db:
        return db.execute(
            "SELECT id, content FROM sage_conversations "
            "WHERE user_id = ? AND conversation_id = ? ORDER BY id",
            (USER, THREAD)).fetchall()


def test_summary_cache_round_trip_and_hash_validation():
    rows = _seed_thread()
    tid0, c0 = rows[0]["id"], rows[0]["content"]
    tid1, c1 = rows[1]["id"], rows[1]["content"]
    store_summary_sync(tid0, THREAD, c0, "cached summary 0", "test-model")
    # A row whose hash does not match the stored turn must be a MISS.
    store_summary_sync(tid1, THREAD, "DIFFERENT CONTENT", "stale summary", "m")
    with db_session() as db:
        got = load_summaries(db, THREAD, [(tid0, c0), (tid1, c1)])
    assert got == {tid0: "cached summary 0"}


def test_get_conversation_history_attaches_summaries_when_on(_flag_on):
    rows = _seed_thread()
    store_summary_sync(rows[0]["id"], THREAD, rows[0]["content"],
                       "first turn, summarized", "test-model")
    history = get_conversation_history(USER, limit=6, conversation_id=THREAD)
    assert history[0]["summary"] == "first turn, summarized"
    assert history[0]["turn_id"] == rows[0]["id"]
    assert history[0]["conversation_id"] == THREAD
    assert all(m["summary"] is None for m in history[1:])
    # Content is still the raw stored turn — the read never substitutes.
    assert history[0]["content"] == rows[0]["content"]


def test_clear_sage_history_also_deletes_cached_summaries():
    rows = _seed_thread()
    store_summary_sync(rows[0]["id"], THREAD, rows[0]["content"], "phi", "m")
    clear_sage_history(USER)
    with db_session() as db:
        left = db.execute(
            "SELECT COUNT(*) AS n FROM sage_history_summaries").fetchone()["n"]
    assert left == 0, "summary outlived its deleted transcript (retained PHI)"


# ── 6. Generation: P3 + careful tier, shed/failure = clean fallback ──────────

def _turn_dict(row):
    return {"turn_id": row["id"], "conversation_id": THREAD,
            "role": "assistant", "content": row["content"]}


async def test_summarize_turn_success_is_fact_guarded_and_persisted(monkeypatch):
    rows = _seed_thread(1)
    row = rows[1]  # "answer 0 with 1 mg"
    fq = FakeQueue()
    monkeypatch.setattr(llm_queue_service, "llm_queue", fq)
    import services.model_router as model_router
    monkeypatch.setattr(model_router, "ambient_tier_evicted", _not_evicted)

    async def fake_model(prompt):
        return "Advised on the antibiotic."  # the model DROPPED the dose

    monkeypatch.setattr(sage_history_service, "_call_careful_model", fake_model)
    out = await sage_history_service.summarize_turn(_turn_dict(row))

    assert out is not None and "1 mg" in out, "fact guard did not restore the dose"
    assert fq.acquired == [(PRIORITY_BACKGROUND,
                            sage_history_service.SUMMARIZE_QUEUE_WAIT_SECONDS)]
    assert PRIORITY_BACKGROUND == 3
    assert fq.released == 1
    with db_session() as db:
        got = load_summaries(db, THREAD, [(row["id"], row["content"])])
    assert got == {row["id"]: out}


async def test_governor_shed_falls_back_cleanly(monkeypatch):
    """BackgroundAdmissionRejected at admission = no summary, no error, no
    release of a slot that was never held, nothing persisted."""
    rows = _seed_thread(1)
    row = rows[1]
    fq = FakeQueue(exc=llm_queue_service.BackgroundAdmissionRejected("shed"))
    monkeypatch.setattr(llm_queue_service, "llm_queue", fq)
    import services.model_router as model_router
    monkeypatch.setattr(model_router, "ambient_tier_evicted", _not_evicted)

    async def must_not_run(prompt):  # pragma: no cover — the assertion
        raise AssertionError("model called despite a governor shed")

    monkeypatch.setattr(sage_history_service, "_call_careful_model", must_not_run)
    out = await sage_history_service.summarize_turn(_turn_dict(row))

    assert out is None
    assert fq.released == 0
    with db_session() as db:
        assert load_summaries(db, THREAD, [(row["id"], row["content"])]) == {}


async def test_generation_failure_releases_queue_and_stores_nothing(monkeypatch):
    rows = _seed_thread(1)
    row = rows[1]
    fq = FakeQueue()
    monkeypatch.setattr(llm_queue_service, "llm_queue", fq)
    import services.model_router as model_router
    monkeypatch.setattr(model_router, "ambient_tier_evicted", _not_evicted)

    async def boom(prompt):
        raise RuntimeError("ollama fell over mid-call")

    monkeypatch.setattr(sage_history_service, "_call_careful_model", boom)
    out = await sage_history_service.summarize_turn(_turn_dict(row))

    assert out is None
    assert fq.released == 1, "a failed generation leaked the queue slot"
    with db_session() as db:
        assert load_summaries(db, THREAD, [(row["id"], row["content"])]) == {}


async def test_evicted_careful_tier_skips_without_touching_the_queue(monkeypatch):
    rows = _seed_thread(1)
    row = rows[1]
    fq = FakeQueue()
    monkeypatch.setattr(llm_queue_service, "llm_queue", fq)
    import services.model_router as model_router

    async def evicted(model):
        return True

    monkeypatch.setattr(model_router, "ambient_tier_evicted", evicted)
    out = await sage_history_service.summarize_turn(_turn_dict(row))
    assert out is None
    assert fq.acquired == [] and fq.released == 0


# ── 7. The interactive turn never awaits summary generation ──────────────────

def test_schedule_summaries_is_synchronous_and_off_is_a_noop():
    assert not asyncio.iscoroutinefunction(sage_history_service.schedule_summaries)
    assert not sage_history_service.enabled()
    assert sage_history_service.schedule_summaries(_summarized_thread()) == 0


def test_schedule_summaries_without_a_loop_is_a_noop(_flag_on):
    hist = _summarized_thread()
    for m in hist:
        m["summary"] = None
    assert sage_history_service.schedule_summaries(hist) == 0  # no running loop


async def test_schedule_targets_only_older_unsummarized_turns(_flag_on, monkeypatch):
    seen: list[int] = []
    gate = asyncio.Event()

    async def fake_summarize(turn):
        seen.append(turn["turn_id"])
        await gate.wait()
        return None

    monkeypatch.setattr(sage_history_service, "summarize_turn", fake_summarize)
    hist = _summarized_thread()
    for m in hist:
        m["summary"] = None
    hist[0]["summary"] = "already cached"       # cached → not rescheduled

    n = sage_history_service.schedule_summaries(hist)
    assert n == 3  # 6 turns − 2 verbatim − 1 already cached
    # Idempotent while in flight: a second assembly must not double-schedule.
    assert sage_history_service.schedule_summaries(hist) == 0
    await asyncio.sleep(0)
    assert sorted(seen) == [2, 3, 4]  # turn_ids 2..4; 5,6 are the verbatim two
    gate.set()
    await asyncio.gather(*sage_history_service._tasks, return_exceptions=True)


async def test_interactive_turn_streams_while_summaries_still_pending(
        _flag_on, monkeypatch):
    """End to end through query_ollama: the clinician's stream must complete
    while summarization is still hung on its gate — i.e. the turn never awaited
    it — and scheduling must nonetheless have happened."""
    started = asyncio.Event()
    gate = asyncio.Event()

    async def hung_summarize(turn):
        started.set()
        await gate.wait()
        return None

    monkeypatch.setattr(sage_history_service, "summarize_turn", hung_summarize)

    async def fake_stream(messages, options=None):
        yield "local "
        yield "answer"

    provider = SimpleNamespace(
        name="ollama",
        astream_chat=lambda m, o=None: fake_stream(m, o),
    )
    choice = SimpleNamespace(model="fake", downgraded=False, preferred="fake")

    async def fake_select(prompt, context, patient_id, classification,
                          route_info=None):
        return choice, provider

    from services import thinking_mode
    monkeypatch.setattr(thinking_mode, "select_provider", fake_select)

    hist = _summarized_thread()
    for m in hist:
        m["summary"] = None

    got = []
    async for chunk in sage_service.query_ollama(
            "What about steroids?", "", hist, "patient"):
        got.append(chunk)

    # The turn fully completed BEFORE the scheduled tasks were ever given the
    # loop — the strongest possible form of "never awaited": at this instant
    # summarization has not even STARTED, let alone finished.
    assert "".join(got) == "local answer"
    assert len(sage_history_service._tasks) == 4   # scheduling DID happen
    assert not started.is_set()
    await asyncio.sleep(0)                         # first post-stream tick
    assert started.is_set()                        # they run only afterwards…
    pending = [t for t in sage_history_service._tasks if not t.done()]
    assert pending, "summary tasks finished before the stream — it awaited them"
    gate.set()                                     # …and were still hung.
    await asyncio.gather(*pending, return_exceptions=True)


# ── 6. The scheduling POINT (regression, 2026-08-11) ─────────────────────────
#
# The feature shipped scheduling on query_ollama's assembly path, BEFORE it
# acquired the interactive lane. Every task was therefore created inside the
# turn's own preempted window, and the governor shed it at the 3 s yield bound
# while the turn streamed on for 13-20 s. Measured end to end: 1 summary
# survived out of 12 attempts — the flag was on, no test failed, and the
# feature did nothing. These pin the two properties that make it actually run.

async def test_summaries_are_scheduled_only_after_the_lane_is_released(
        _flag_on, monkeypatch):
    """The invariant is ORDERING: scheduling must happen after the turn has
    RELEASED the interactive lane.

    Note what is deliberately NOT asserted: `preempt_requested` at scheduling
    time. At the old broken call site that flag reads False too, because
    acquire() has not been called yet — the damage is done a moment later, when
    the already-created task makes its own acquire attempt while the turn is
    mid-stream. An earlier version of this test checked the flag, passed
    against the broken code, and would have certified the bug as fixed.
    """
    from services.llm_queue_service import llm_queue

    # EVERY call is recorded, not just the last: scheduling once too early and
    # again correctly still creates the doomed tasks the first time (and the
    # _inflight dedupe then makes the correct call a no-op).
    events: list[str] = []
    seen = []
    real_schedule = sage_history_service.schedule_summaries
    real_release = llm_queue.release

    def release_spy():
        events.append("release")
        return real_release()

    def spy(history):
        events.append("schedule")
        seen.append({
            "preempt_requested": llm_queue.preempt_requested,
            "interactive_active": llm_queue.interactive_active,
        })
        return real_schedule(history)

    monkeypatch.setattr(llm_queue, "release", release_spy)

    monkeypatch.setattr(sage_history_service, "schedule_summaries", spy)

    async def noop_summarize(turn):
        return None

    monkeypatch.setattr(sage_history_service, "summarize_turn", noop_summarize)

    async def fake_stream(messages, options=None):
        # Assert mid-stream that the turn really does hold the lane, so a
        # False reading at schedule time cannot be a queue that never engaged.
        assert llm_queue.preempt_requested is True
        yield "local "
        yield "answer"

    provider = SimpleNamespace(
        name="ollama", astream_chat=lambda m, o=None: fake_stream(m, o))
    choice = SimpleNamespace(model="fake", downgraded=False, preferred="fake")

    async def fake_select(prompt, context, patient_id, classification,
                          route_info=None):
        return choice, provider

    from services import thinking_mode
    monkeypatch.setattr(thinking_mode, "select_provider", fake_select)

    hist = _summarized_thread()
    for m in hist:
        m["summary"] = None

    got = []
    async for chunk in sage_service.query_ollama(
            "What about steroids?", "", hist, "patient"):
        got.append(chunk)

    assert "".join(got) == "local answer"
    assert seen, "schedule_summaries was never called"
    assert "release" in events, "the turn never released the lane"
    assert events.index("schedule") > events.index("release"), (
        f"summaries scheduled before the lane was released (order: {events}) — "
        "every task created there makes its acquire attempt while this turn is "
        "still streaming, and the governor sheds it at the yield bound"
    )
    for i, call in enumerate(seen):
        assert call["interactive_active"] == 0, (
            f"scheduling call {i + 1} of {len(seen)} ran while the clinician's "
            "turn still held the lane"
        )
    await asyncio.gather(*sage_history_service._tasks, return_exceptions=True)


async def test_a_shed_is_retried_and_can_succeed_on_a_later_attempt(monkeypatch):
    """A shed is a "not now", not a "never". On a busy ward the next
    clinician's turn can claim the lane before this summary's quiet tail
    elapses; single-shot admission would make the feature inert exactly when
    conversations are longest."""
    attempts = {"n": 0}
    released = {"n": 0}

    async def flaky_acquire(priority=None, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < sage_history_service.SUMMARIZE_MAX_ATTEMPTS:
            raise llm_queue_service.BackgroundAdmissionRejected("shed")
        return True

    fq = SimpleNamespace(
        acquire=flaky_acquire,
        release=lambda: released.__setitem__("n", released["n"] + 1),
    )
    monkeypatch.setattr(llm_queue_service, "llm_queue", fq)
    import services.model_router as model_router
    monkeypatch.setattr(model_router, "ambient_tier_evicted", _not_evicted)

    async def model(prompt):
        return "Summary after the ward went quiet."

    monkeypatch.setattr(sage_history_service, "_call_careful_model", model)

    out = await sage_history_service._generate("Some clinical turn text.")

    assert out == "Summary after the ward went quiet."
    assert attempts["n"] == sage_history_service.SUMMARIZE_MAX_ATTEMPTS
    assert released["n"] == 1, "the slot must be released exactly once"


async def test_every_attempt_shed_falls_back_after_a_bounded_number_of_tries(
        monkeypatch):
    """Bounded: a ward that is never quiet must not accumulate retrying tasks.
    After the last attempt the head-trim stands and no slot is released."""
    attempts = {"n": 0}

    async def always_shed(priority=None, timeout=None):
        attempts["n"] += 1
        raise llm_queue_service.BackgroundAdmissionRejected("shed")

    fq = SimpleNamespace(
        acquire=always_shed,
        release=lambda: pytest.fail("released a slot that was never acquired"),
    )
    monkeypatch.setattr(llm_queue_service, "llm_queue", fq)
    import services.model_router as model_router
    monkeypatch.setattr(model_router, "ambient_tier_evicted", _not_evicted)

    async def must_not_run(prompt):  # pragma: no cover — the assertion
        raise AssertionError("model called despite every attempt shedding")

    monkeypatch.setattr(sage_history_service, "_call_careful_model", must_not_run)

    assert await sage_history_service._generate("text") is None
    assert attempts["n"] == sage_history_service.SUMMARIZE_MAX_ATTEMPTS


async def test_cancellation_is_never_retried(monkeypatch):
    """Shutdown must end the attempt immediately — retrying through a
    CancelledError would sleep a dying loop and delay shutdown."""
    attempts = {"n": 0}

    async def cancelled(priority=None, timeout=None):
        attempts["n"] += 1
        raise asyncio.CancelledError()

    fq = SimpleNamespace(
        acquire=cancelled,
        release=lambda: pytest.fail("released a slot that was never acquired"),
    )
    monkeypatch.setattr(llm_queue_service, "llm_queue", fq)
    import services.model_router as model_router
    monkeypatch.setattr(model_router, "ambient_tier_evicted", _not_evicted)

    assert await sage_history_service._generate("text") is None
    assert attempts["n"] == 1, "a cancellation was retried"


def test_retry_backoff_matches_attempt_count():
    """The backoff tuple is indexed by attempt; if the two constants drift
    apart the retry path raises IndexError on a background task instead of
    falling back. Asserted on the real module values, which the autouse
    fixture deliberately does not patch here."""
    import importlib
    fresh = importlib.reload(sage_history_service)
    assert (len(fresh.SUMMARIZE_RETRY_BACKOFF_SECONDS)
            == fresh.SUMMARIZE_MAX_ATTEMPTS - 1)
    assert all(s >= 0 for s in fresh.SUMMARIZE_RETRY_BACKOFF_SECONDS)
