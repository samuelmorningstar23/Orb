"""Sticky patient anchoring, one interactive num_ctx, and the chart-open prewarm.

THE DEFECT THESE PIN (2026-08-04). Sage's scope router resolved one UTTERANCE at
a time, so a ward-round thread lost its patient the moment the clinician stopped
naming them:

    "How is <patient> doing this morning?"  -> PATIENT_SPECIFIC, chart attached
    "What is driving that?"                 -> GENERAL_MEDICAL, CHART DROPPED

Measured on the live appliance before the fix, turn 2 of a five-turn round
answered "I am unsure what is driving it" holding no chart at all — and that
starved state is exactly the condition under which the 2026-08-04 tier
evaluation saw a model fabricate a chest X-ray result, a lactate and a GCS for a
patient whose chart said "no recent imaging" and "alert". A model given no facts
about a patient it is plainly being asked about does not decline; it invents.

The same flip cost latency twice over: scope decides num_ctx, num_ctx keys the
Ollama runner, and a runner change costs a ~2.8 s reload AND wipes the KV cache
that the previous turns paid to fill.
"""
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import db_session
from services import sage_service
from services.sage_service import (INTERACTIVE_NUM_CTX, SCOPE_CONFIGS,
                                   format_patient_channel_a,
                                   get_relevant_context)

DEPT = "Sticky Anchor Medicine"
OTHER_DEPT = "Sticky Anchor Cardiology"


def _mk_user(department=DEPT, role="doctor"):
    uid = f"STKY{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Sticky {role}", role, department),
        )
    return {"id": uid, "role": role, "department": department, "name": f"Sticky {role}"}


#: Every patient this module seeds, so teardown can put them back.
_CREATED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: this module seeds `status='admitted'` patients into a
    session DB shared by every other file.

    Left admitted they are not a cosmetic leak. `bot_service._generate_vitals`
    appends a reading to EVERY admitted patient on a simulation tick, so a
    leftover with one seeded vitals row grows a trend it was never meant to
    have and starts counting as `worsening` hospital-wide — carrying the
    `critical` risk score seeded below with it. That is how this file's
    patients turned up in another module's ward aggregate.

    Discharge rather than DELETE: rows written during the test legitimately
    reference these ids. Module-scoped, so teardown runs after this file's last
    test and its own assertions still see admitted patients.
    """
    yield
    if not _CREATED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _CREATED],
        )
    _CREATED.clear()


def _mk_patient(name, diagnosis, bed, drug, department):
    pid = f"STKYP{uuid.uuid4().hex[:8].upper()}"
    _CREATED.append(pid)
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('STKYADM', 'Sticky Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, admission_date) "
            "VALUES (?, ?, ?, ?, ?, 64, 'M', ?, 'admitted', ?)",
            (pid, name, f"MRN-{uuid.uuid4().hex[:8]}", bed, department, diagnosis, now),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, ?, 'active', 'STKYADM')",
            (pid, department, department, bed),
        )
        db.execute(
            "INSERT INTO vitals (patient_id, heart_rate, systolic_bp, diastolic_bp, spo2, "
            "temperature, respiratory_rate, timestamp) VALUES (?, 122, 88, 54, 89, 38.9, 30, ?)",
            (pid, now),
        )
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level, drivers, timestamp) "
            "VALUES (?, 77, 'critical', 'tachypnoea; hypoxaemia', ?)",
            (pid, now),
        )
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status) "
            "VALUES (?, ?, '1 g', 'IV', 'TDS', 'active')",
            (pid, drug),
        )
    return {"id": pid, "name": name, "diagnosis": diagnosis, "bed": bed, "drug": drug}


@pytest.fixture(scope="module")
def ward():
    doctor = _mk_user()
    return {
        "doctor": doctor,
        "a": _mk_patient("Ramaswamy Krishnamurthy", "Severe pneumonia", "SA-31",
                         "Piperacillin-tazobactam", DEPT),
        "b": _mk_patient("Vaidehi Subramaniam", "Acute pancreatitis", "SA-32",
                         "Ondansetron", DEPT),
        "foreign": _mk_patient("Harishchandra Gangopadhyay", "Anterior STEMI", "SC-33",
                               "Ticagrelor", OTHER_DEPT),
    }


def _turn(query, user, patient_id=None, history=None):
    return get_relevant_context(query, user, patient_id, history)


def _thread(patient_name):
    """The transcript after one answered question about this patient."""
    return [
        {"role": "user", "content": f"How is {patient_name} doing this morning?"},
        {"role": "assistant", "content":
            f"NOACT|{patient_name} is critically unwell; NEWS2 is 13."},
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  (a) ONE num_ctx — a scope flip must not restart the Ollama runner
# ══════════════════════════════════════════════════════════════════════════════

def test_every_interactive_scope_shares_one_context_window():
    """Ollama keys a runner by (model, num_ctx): a differing window is a runner
    teardown, a ~2.8 s reload and a wiped KV cache — paid mid-conversation, on
    the clinician's time."""
    windows = {scope: cfg["num_ctx"] for scope, cfg in SCOPE_CONFIGS.items()}
    assert set(windows.values()) == {INTERACTIVE_NUM_CTX}, (
        f"scopes disagree about num_ctx: {windows}")


def test_the_unlisted_clinical_scope_also_lands_on_that_window():
    """`clinical` is emitted by retrieval but absent from the table — it resolves
    through the `simple` default, which must be the same window as the rest."""
    assert SCOPE_CONFIGS.get("clinical", SCOPE_CONFIGS["simple"])["num_ctx"] == INTERACTIVE_NUM_CTX


def test_the_keep_warm_ping_loads_each_tier_at_its_live_window(monkeypatch):
    """The warmup was the last place still disagreeing about num_ctx: it loaded
    BOTH tiers at 4096, so the interactive runner Sage then asked for at 8192
    was a different runner — the very first clinical turn after a restart (and
    after every 5-minute heartbeat that found the model evicted) paid a reload
    and got a cold cache from a warmup whose whole purpose was the opposite.
    The ambient tier keeps 4096; that IS its live window."""
    import httpx as _httpx
    from fastapi.testclient import TestClient

    from auth import create_token
    from config import AMBIENT_LLM_MODEL, LLM_MODEL
    from main import app

    seen = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            seen[json["model"]] = json["options"]["num_ctx"]
            return _FakeResp()

    monkeypatch.setattr(_httpx, "AsyncClient", _FakeClient)

    admin = _mk_user(department="Sticky Admin", role="admin")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(admin['id'], 'admin')}"}

    for model in (LLM_MODEL, AMBIENT_LLM_MODEL):
        client.post("/api/health/models/load", headers=headers, json={"model": model})

    assert seen.get(LLM_MODEL) == INTERACTIVE_NUM_CTX
    if AMBIENT_LLM_MODEL != LLM_MODEL:
        assert seen.get(AMBIENT_LLM_MODEL) == 4096


# ══════════════════════════════════════════════════════════════════════════════
#  (b) STICKY ANCHORING — the thread keeps its patient
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("followup", [
    "What is driving that?",          # the measured failure: no pronoun, no keyword
    "Why?",
    "And overnight?",
    "What would you do next?",
])
def test_a_bound_thread_keeps_the_chart_on_a_bare_followup(ward, followup):
    a = ward["a"]
    context, scope, _s, _c, _e, pctx, qscope = _turn(
        followup, ward["doctor"], patient_id=a["id"], history=_thread(a["name"]))
    assert qscope == "PATIENT_ANCHORED", f"{followup!r} dropped the thread's patient"
    assert pctx is not None and pctx["demographics"]
    assert a["name"] in context and a["diagnosis"] in context
    assert scope == "patient", "an anchored turn must ride the patient lane"


def test_the_anchor_survives_a_general_clinical_question_mid_round(ward):
    """Turn 4 of a real ward round is a textbook question asked WHILE standing at
    the bedside. Answering it with the chart in hand is the point."""
    a = ward["a"]
    context, _scope, _s, _c, _e, pctx, qscope = _turn(
        # NB "hospital acquired pneumonia" cannot be used here: "hospital" is a
        # BROAD_QUERY_KEYWORD, so that phrasing lands on the census lane — a
        # pre-existing looseness in that keyword list, not an anchoring failure.
        "what is the empiric antibiotic protocol for community acquired pneumonia?",
        ward["doctor"], patient_id=a["id"], history=_thread(a["name"]))
    assert qscope == "PATIENT_ANCHORED"
    assert pctx is not None and a["name"] in context


def test_the_thread_alone_anchors_when_no_chart_is_open(ward):
    """Sage on its own screen: no patient_id, so the binding comes from what the
    clinician actually said."""
    a = ward["a"]
    _ctx, _scope, _s, _c, _e, pctx, qscope = _turn(
        "what is driving that?", ward["doctor"], history=_thread(a["name"]))
    assert qscope == "PATIENT_ANCHORED"
    assert pctx["demographics"]


def test_a_new_chat_starts_unbound(ward):
    """The binding is derived from the transcript and the open chart, so clearing
    the transcript IS forgetting the patient — there is no separate anchor that
    can outlive the conversation."""
    _ctx, _scope, _s, _c, _e, pctx, qscope = _turn(
        "what is driving that?", ward["doctor"], history=[])
    assert pctx is None
    assert qscope != "PATIENT_ANCHORED"


def test_naming_a_different_in_scope_patient_rebinds_the_thread(ward):
    a, b = ward["a"], ward["b"]
    context, _scope, _s, _c, _e, pctx, qscope = _turn(
        f"How is {b['name']} doing?", ward["doctor"],
        patient_id=a["id"], history=_thread(a["name"]))
    assert qscope == "PATIENT_SPECIFIC"
    assert b["name"] in context and b["diagnosis"] in context
    assert a["diagnosis"] not in context, "the previous patient's chart rode along"


def test_the_rebind_persists_into_the_next_bare_followup(ward):
    """Having switched to B by name, the NEXT wordless follow-up must be about B,
    not about the patient whose chart happens to be on screen."""
    a, b = ward["a"], ward["b"]
    history = _thread(a["name"]) + [
        {"role": "user", "content": f"How is {b['name']} doing?"},
        {"role": "assistant", "content": f"NOACT|{b['name']} is stable."},
    ]
    context, _scope, _s, _c, _e, pctx, qscope = _turn(
        "and what is driving that?", ward["doctor"], history=history)
    assert qscope == "PATIENT_ANCHORED"
    assert b["name"] in context
    assert a["diagnosis"] not in context


def test_naming_an_out_of_department_patient_still_wins_over_the_anchor(ward):
    """The out-of-scope denial runs BEFORE anchoring and returns outright: the
    clinician is told plainly, and no chart — theirs or the anchored patient's —
    is assembled."""
    a, foreign = ward["a"], ward["foreign"]
    context, _scope, _s, _c, _e, pctx, qscope = _turn(
        f"How is {foreign['name']} doing?", ward["doctor"],
        patient_id=a["id"], history=_thread(a["name"]))
    assert qscope == "OUT_OF_SCOPE"
    assert pctx is None
    assert foreign["diagnosis"] not in context
    assert a["diagnosis"] not in context, (
        "the anchored patient's chart answered a question about someone else")


def test_naming_a_patient_who_exists_nowhere_suppresses_the_anchor(ward):
    """Fail-closed. Answering "what are Mr Nobody's vitals?" off the anchored
    patient's chart would be a silent wrong-patient answer — worse than no
    answer."""
    a = ward["a"]
    context, _scope, _s, _c, _e, pctx, qscope = _turn(
        "What are Ganapathy Viswanathan's latest vitals?", ward["doctor"],
        patient_id=a["id"], history=_thread(a["name"]))
    assert pctx is None
    assert "NO MATCHING PATIENT" in context
    assert a["diagnosis"] not in context


def test_an_out_of_department_chart_on_screen_never_anchors(ward):
    """view_context/patient_id is client-supplied. A stale or forged one must not
    become a chart — and, because the turn is not reaching for that patient, it
    is not a denial either: it simply anchors nothing."""
    foreign = ward["foreign"]
    context, _scope, _s, _c, _e, pctx, qscope = _turn(
        "what is driving that?", ward["doctor"], patient_id=foreign["id"])
    assert pctx is None
    assert foreign["name"] not in context and foreign["diagnosis"] not in context


def test_the_transcript_cannot_anchor_a_patient_from_another_ward(ward):
    """The history scan runs over the DEPARTMENT-SCOPED rows, so a foreign name
    pasted into the transcript resolves to nobody."""
    foreign = ward["foreign"]
    history = [
        {"role": "user", "content": f"tell me about {foreign['name']}"},
        {"role": "assistant", "content": "I cannot discuss that patient."},
    ]
    context, _scope, _s, _c, _e, pctx, _qscope = _turn(
        "what is driving that?", ward["doctor"], history=history)
    assert pctx is None
    assert foreign["diagnosis"] not in context


def test_an_assistant_census_answer_cannot_bind_the_thread(ward):
    """Only the CLINICIAN's own turns bind. A census answer names the whole ward;
    letting one of those names anchor would hand the next follow-up a chart
    nobody asked for."""
    a, b = ward["a"], ward["b"]
    history = [
        {"role": "user", "content": "give me an overview of all my patients"},
        {"role": "assistant", "content":
            f"{a['name']} — {a['bed']}; {b['name']} — {b['bed']}"},
    ]
    _ctx, _scope, _s, _c, _e, pctx, qscope = _turn(
        "what is driving that?", ward["doctor"], history=history)
    assert pctx is None
    assert qscope != "PATIENT_ANCHORED"


def test_a_turn_naming_two_patients_is_ambiguous_and_anchors_nothing(ward):
    a, b = ward["a"], ward["b"]
    history = [
        {"role": "user", "content": f"compare {a['name']} and {b['name']}"},
        {"role": "assistant", "content": "NOACT|Both are unwell."},
    ]
    _ctx, _scope, _s, _c, _e, pctx, _qscope = _turn(
        "which one is worse?", ward["doctor"], history=history)
    assert pctx is None, "an ambiguous turn silently picked one of the two"


def test_an_aggregate_question_stays_phi_free_even_while_bound(ward):
    """Aggregate context is PHI-free BY CONSTRUCTION (counts and distributions,
    zero patient rows). Attaching an anchored chart to it would quietly convert
    a PHI-free lane into a PHI one — the anchor is not allowed to do that."""
    a = ward["a"]
    context, _scope, _s, _c, _e, pctx, qscope = _turn(
        "how many patients are admitted right now?", ward["doctor"],
        patient_id=a["id"], history=_thread(a["name"]))
    assert qscope == "AGGREGATE_STATS"
    assert pctx is None
    assert a["name"] not in context


def test_a_census_ask_still_gets_the_census_not_the_anchored_chart(ward):
    a = ward["a"]
    context, _scope, _s, _c, _e, pctx, qscope = _turn(
        "give me an overview of all my patients", ward["doctor"],
        patient_id=a["id"], history=_thread(a["name"]))
    assert qscope == "BROAD"
    assert ward["b"]["name"] in context, "the census lost a patient to the anchor"


def _anchor_probe(monkeypatch):
    """Capture what the route actually handed the model for one turn."""
    seen = {}

    def _capture(query, context, history, scope, **kw):
        seen["classification"] = kw.get("classification")
        seen["context"] = context

        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield "NOACT|ok."
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _capture)
    return seen


def test_an_anchored_turn_is_classified_as_phi(ward, monkeypatch):
    """The governance label must describe what the model was GIVEN, not what the
    clinician typed. "What is driving that?" carries no pronoun, no name and no
    patient_id, so the pre-retrieval classifier calls it general_knowledge —
    while the prompt in front of the model holds that patient's vitals, labs and
    medications.

    THREAD-SCOPED since migration 16. Prompt history is keyed on the
    conversation, not on the user, so the anchor lives inside ONE thread — which
    is what the Sage panel sends on every turn. The assertion is unchanged; only
    the vehicle is. The companion test below pins the other half: across threads
    the anchor must NOT carry, which is the whole point of the change.
    """
    from fastapi.testclient import TestClient

    from auth import create_token
    from main import app
    from services.sage_service import save_conversation

    seen = _anchor_probe(monkeypatch)

    a = ward["a"]
    doctor = ward["doctor"]
    thread = "thread-anchor-1"
    save_conversation(doctor["id"], "user", f"How is {a['name']} doing this morning?",
                      conversation_id=thread)
    save_conversation(doctor["id"], "assistant", f"NOACT|{a['name']} is critically unwell.",
                      conversation_id=thread)

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(doctor['id'], 'doctor')}"}
    resp = client.post("/api/sage/query", headers=headers,
                       json={"query": "what is driving that?", "conversation_id": thread})
    assert resp.status_code == 200

    assert a["name"] in seen["context"], "the thread lost its patient over the route"
    assert seen["classification"] is not None
    assert seen["classification"].contains_phi is True
    assert seen["classification"].query_class == "patient_scoped"


def test_the_anchor_does_not_cross_into_a_new_conversation(ward, monkeypatch):
    """The other half of the anchor, and the reason migration 16 exists.

    An anchor that survives "New Chat" is not an anchor, it is a leak: a general
    question typed on a cleared screen came back answered about the patient from
    the conversation before it. Within a thread the anchor is a feature; across
    threads it is a PHI-crossing defect on a shared ward workstation.
    """
    from fastapi.testclient import TestClient

    from auth import create_token
    from main import app
    from services.sage_service import save_conversation

    seen = _anchor_probe(monkeypatch)

    a = ward["a"]
    doctor = ward["doctor"]
    save_conversation(doctor["id"], "user", f"How is {a['name']} doing this morning?",
                      conversation_id="thread-anchor-old")
    save_conversation(doctor["id"], "assistant", f"NOACT|{a['name']} is critically unwell.",
                      conversation_id="thread-anchor-old")

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(doctor['id'], 'doctor')}"}
    resp = client.post("/api/sage/query", headers=headers,
                       json={"query": "what is driving that?",
                             "conversation_id": "thread-anchor-new"})
    assert resp.status_code == 200

    assert a["name"] not in (seen["context"] or ""), (
        "the previous conversation's patient reached a NEW thread's prompt"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  (c) PREWARM — one renderer, and bounds that keep it from becoming a storm
# ══════════════════════════════════════════════════════════════════════════════

def test_the_prewarm_sends_the_same_chart_block_the_real_turn_will(ward):
    """Ollama's cache reuse is PREFIX-anchored. If the prewarm's chart block
    differs from the interactive turn's by one character it stops being a prefix
    and the prefill is simply wasted — so both must come from ONE renderer."""
    from services.nervous_system_triggers import _load_cached_facts  # noqa: F401
    from services.sage_service import _assemble_patient_context, _build_chat_messages

    a = ward["a"]
    from database import get_db
    db = get_db()
    try:
        facts = _assemble_patient_context(db, a["id"], DEPT)
    finally:
        db.close()

    prewarm_ctx = "\n".join(format_patient_channel_a(facts, a["id"]))
    real_ctx, _scope, _s, _c, _e, _p, _q = _turn(
        f"How is {a['name']} doing?", ward["doctor"])

    assert prewarm_ctx in real_ctx, (
        "the prewarmed chart block is not a prefix of the real turn's context")
    assert real_ctx.startswith(prewarm_ctx), (
        "Channel A must come FIRST — anything before it breaks the shared prefix")

    prewarm_msgs = _build_chat_messages("", prewarm_ctx, None, "patient")
    real_msgs = _build_chat_messages(f"How is {a['name']} doing?", real_ctx, None, "patient")
    assert prewarm_msgs[0] == real_msgs[0], "system prompt differs"
    assert real_msgs[1]["content"].startswith(
        prewarm_msgs[1]["content"].rsplit("\n<<<END_CLINICAL_CONTEXT>>>", 1)[0]), (
        "the fenced context block diverges before the chart block ends")


@pytest.mark.asyncio
async def test_the_prewarm_only_fires_on_a_chart_open(monkeypatch):
    """A critical-alert warm re-fires per critical patient per vitals cycle.
    A 27B prefill on that cadence is a background storm, not a prefetch."""
    from services import nervous_system_triggers as nst

    fired = []

    async def _fake_prewarm(patient_id, facts):
        fired.append(patient_id)
        return True

    async def _fresh(*a, **kw):
        return True

    monkeypatch.setattr(nst, "prewarm_patient_prefix", _fake_prewarm)
    monkeypatch.setattr(nst, "run_db", lambda fn, *a, **kw: _fresh())

    await nst.warm_patient_context("PATX", reason="vigil_critical_alert")
    assert fired == []

    await nst.warm_patient_context("PATX", reason="ui_patient_opened")
    assert fired == ["PATX"], "a chart open must warm the prefix even on a fresh cache"


@pytest.mark.asyncio
async def test_a_compile_already_in_flight_skips_the_second_prewarm(monkeypatch):
    """Two chart opens for the same patient must not each prefill the same
    prefix and hold the GPU gate twice for it."""
    import time as _time

    from services import nervous_system_triggers as nst

    fired = []

    async def _fake_prewarm(patient_id, facts):
        fired.append(patient_id)
        return True

    async def _fresh(*a, **kw):
        return True

    monkeypatch.setattr(nst, "prewarm_patient_prefix", _fake_prewarm)
    monkeypatch.setattr(nst, "run_db", lambda fn, *a, **kw: _fresh())

    nst._WARM_INFLIGHT["PATY"] = _time.monotonic()
    try:
        assert await nst.warm_patient_context("PATY", reason="ui_patient_opened") is True
        assert fired == []
    finally:
        nst._WARM_INFLIGHT.pop("PATY", None)


@pytest.mark.asyncio
async def test_an_abandoned_in_flight_marker_never_suppresses_a_compile(monkeypatch):
    """The marker gates the PREWARM only. warm_patient_context is dispatched
    fire-and-forget; a task destroyed with the loop under it never runs its
    `finally`, so a marker that gated the COMPILE would silently stop the next
    chart from being assembled at all (tests/test_hospital_nervous_system.py
    caught exactly that)."""
    import time as _time

    from services import nervous_system_triggers as nst

    compiled = []

    async def _not_fresh(*a, **kw):
        return False

    def _fake_run_db(fn, *a, **kw):
        return _not_fresh()

    monkeypatch.setattr(nst, "run_db", _fake_run_db)

    async def _fake_prewarm(patient_id, facts):
        return True

    monkeypatch.setattr(nst, "prewarm_patient_prefix", _fake_prewarm)
    monkeypatch.setattr(nst, "_compile_context",
                        lambda pid, hint: (compiled.append(pid) or ({}, "", [], "high")))

    nst._WARM_INFLIGHT["PATV"] = _time.monotonic()
    try:
        assert await nst.warm_patient_context("PATV", reason="ui_patient_opened") is True
        assert compiled == ["PATV"], "an abandoned marker suppressed a real compile"
    finally:
        nst._WARM_INFLIGHT.pop("PATV", None)


@pytest.mark.asyncio
async def test_the_prewarm_is_rate_limited_per_patient(monkeypatch):
    """Tabbing through a ward must not fire a 27B prefill per navigation."""
    from services import nervous_system_triggers as nst

    calls = []

    class _Choice:
        model = "medgemma:27b"
        downgraded = False
        preferred = "medgemma:27b"

    async def _select():
        calls.append("select")
        return _Choice()

    monkeypatch.setattr("services.model_router.select_chat_model", _select)
    nst._PREWARM_LAST.clear()
    import asyncio
    nst._PREWARM_LAST["PATZ"] = asyncio.get_running_loop().time()
    try:
        assert await nst.prewarm_patient_prefix("PATZ", {"demographics": "x"}) is False
        assert calls == [], "the rate limit did not short-circuit before the model call"
    finally:
        nst._PREWARM_LAST.clear()


@pytest.mark.asyncio
async def test_the_prewarm_gives_up_rather_than_waiting_out_a_long_background_job(monkeypatch):
    """It queues at background priority, so it can sit behind a 170-second
    narrator job. Landing that late is worse than not running at all — the
    clinician has already asked and their turn already prefilled the prefix."""
    from services import model_router
    from services import nervous_system_triggers as nst

    waits = []

    class _Choice:
        model = "medgemma:27b"
        downgraded = False
        preferred = "medgemma:27b"

    async def _select():
        return _Choice()

    async def _acquire(priority=3, timeout=None):
        waits.append(timeout)
        raise TimeoutError("background lane busy")

    async def _msgs(fn, *a, **kw):
        return [{"role": "system", "content": "x"}]

    monkeypatch.setattr(model_router, "select_chat_model", _select)
    monkeypatch.setattr(nst, "run_db", lambda fn, *a, **kw: _msgs(fn))
    monkeypatch.setattr(nst.llm_queue, "acquire", _acquire)

    nst._PREWARM_LAST.clear()
    assert await nst.prewarm_patient_prefix("PATQ", {"demographics": "x"}) is False
    assert waits and waits[0] is not None, "the prewarm waited on the queue unbounded"


@pytest.mark.asyncio
async def test_the_prewarm_never_runs_on_a_downgraded_tier(monkeypatch):
    """If the 27B has been evicted, warming the ambient 4B at the interactive
    window would spawn a SECOND 4B runner (Ollama keys a runner by model+ctx) to
    warm a prefix no clinical answer will use."""
    from services import model_router
    from services import nervous_system_triggers as nst

    posted = []

    class _Choice:
        model = "medgemma:4b"
        downgraded = True
        preferred = "medgemma:27b"

    async def _select():
        return _Choice()

    monkeypatch.setattr(model_router, "select_chat_model", _select)

    class _Boom:
        def __init__(self, *a, **kw):
            posted.append(kw)
            raise AssertionError("prewarm must not reach the model")

    monkeypatch.setattr(nst.httpx, "AsyncClient", _Boom)
    nst._PREWARM_LAST.clear()
    assert await nst.prewarm_patient_prefix("PATW", {"demographics": "x"}) is False
    assert posted == []


# ══════════════════════════════════════════════════════════════════════════════
# THE CONTRACTION GATE (regression, 2026-08-12)
#
# `_names_a_specific_patient` suppresses the anchor fail-closed, on the theory
# that a turn reaching for someone else must never be answered off the bound
# patient. Its possessive probe was `\b[A-Z][a-z]+'s\b`, which cannot tell the
# possessive "Rajesh's" from the CONTRACTION "What's" — so every question a
# clinician opens with "What's ...", "Who's ...", "How's ..." was read as naming
# a person, the anchor was suppressed, and the chart was dropped.
#
# The failure shape is the dangerous one: the turn came back FAST (1.7 s against
# 11-13 s for a real patient turn) and HIGH CONFIDENCE, saying "that's a very
# broad question without any specific patient context" about a patient whose
# chart was open. Measured in the 2026-08-11 A/B, both arms, groups 3 and 4.
#
# The module comment above calls a missing chart THE HALLUCINATION CONDITION:
# starving the model of the chart does not make it decline, it makes it invent.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("followup", [
    "What's the biggest risk in the next 24 hours?",   # the measured failure
    "What's the first thing I should do?",
    "What's changed since this morning?",
    "Who's best placed to review overnight?",
    "How's the response to treatment?",
    "That's reassuring - anything else to watch?",
])
def test_a_contraction_never_reads_as_naming_a_patient(ward, followup):
    """A sentence-initial contraction is not a possessive. These turns must keep
    the chart exactly like any other bare follow-up."""
    a = ward["a"]
    context, scope, _s, _c, _e, pctx, qscope = _turn(
        followup, ward["doctor"], patient_id=a["id"], history=_thread(a["name"]))
    assert qscope == "PATIENT_ANCHORED", (
        f"{followup!r} was read as naming a patient, so the anchor was "
        "suppressed and the chart dropped - the hallucination condition"
    )
    assert pctx is not None and pctx["demographics"]
    assert a["name"] in context
    assert scope == "patient"


@pytest.mark.parametrize("query", [
    "Is Chest Physiotherapy indicated?",
    "Should I start Broad Spectrum antibiotics?",
    "Would Oxygen Therapy help here?",
])
def test_capitalised_clinical_terms_do_not_read_as_a_patient_name(ward, query):
    """Two capitalised words are not a person. `[A-Z][a-z]+ [A-Z][a-z]+` could
    not tell "Rajesh Iyer" from "Chest Physiotherapy"."""
    a = ward["a"]
    _ctx, _scope, _s, _c, _e, pctx, qscope = _turn(
        query, ward["doctor"], patient_id=a["id"], history=_thread(a["name"]))
    assert qscope == "PATIENT_ANCHORED", f"{query!r} suppressed the anchor"
    assert pctx is not None


@pytest.mark.parametrize("query,names", [
    # MUST still be recognised - these are the wrong-patient protections.
    ("Rajesh's potassium is high, what now?", True),
    ("How is Rajesh doing?", True),          # was FALSE: the probe was written
    ("How are Rajesh Iyer's obs?", True),    # lower-case with no IGNORECASE
    ("Tell me about Rajesh Iyer", True),
    ("Show me Mr. Sharma", True),
    ("Tell me about bed 12", True),
    # MUST NOT fire.
    ("What's the biggest risk in the next 24 hours?", False),
    ("What is the plan for today?", False),
    ("When would you add a second agent?", False),
])
def test_the_name_gate_is_correct_in_both_directions(query, names):
    """A false positive drops the chart; a false negative disarms the
    wrong-patient protections. Both directions are pinned."""
    assert sage_service._names_a_specific_patient(query, query.lower()) is names


# ══════════════════════════════════════════════════════════════════════════════
# A CAPITALISED DRUG NAME IS NOT A PATIENT (regression, 2026-08-14)
#
# The gate treated ANY capitalised word outside a hand-written stop list as a
# patient's name. Verified on the appliance the same day, all four returning
# True and therefore dropping the chart mid-thread:
#
#     "Is Ceftriaxone appropriate?"   "Should I start Vancomycin?"
#     "Is Monocef ok here?"           "Run the Sepsis Six"
#
# The previous battery only parametrised phrases whose words were ALREADY in
# the stop list ("Chest Physiotherapy", "Broad Spectrum", "Oxygen Therapy"), so
# it could not have caught this: it tested the list, not the gate. This battery
# is real drug names — generics, morphological stems, and the Indian brand
# names no international generic list reaches.
#
# The chart is what stands between the model and invention; the module comment
# above calls losing it THE HALLUCINATION CONDITION.
# ══════════════════════════════════════════════════════════════════════════════

DRUG_PHRASES = [
    # The four measured failures.
    "Is Ceftriaxone appropriate?",
    "Should I start Vancomycin?",
    "Is Monocef ok here?",
    "Run the Sepsis Six",
    # Generics from the built-in lexicon.
    "Should we give Paracetamol?",
    "Continue Enoxaparin?",
    "Metoprolol or Bisoprolol - which?",
    "Would Piperacillin help here?",
    "Is Noradrenaline the next step?",
    "Has the Pantoprazole been charted?",
    # Reached by morphological stem, not by name.
    "Is Cefoperazone reasonable?",
    "Would Levofloxacin cover this?",
    "Is Esomeprazole needed?",
    # Indian brand names.
    "Is Dolo enough for this fever?",
    "What about Augmentin?",
    "Would Clavam be better?",
    "Is Taxim ok overnight?",
    # Conditions and procedures reached by morphology, not by enumeration.
    "Is Pancreatitis still the working diagnosis?",
    "Would Bronchoscopy add anything?",
    "Is Thrombocytopenia expected here?",
    # Ward imperatives that open a sentence.
    "Repeat lactate in 2 hours",
    "Nebulise the salbutamol",
    "Escalate to the registrar?",
]


@pytest.mark.parametrize("query", DRUG_PHRASES)
def test_a_drug_name_never_reads_as_a_patient_name(query):
    assert sage_service._names_a_specific_patient(query, query.lower()) is False, (
        f"{query!r} read as naming a patient — the sticky anchor is suppressed "
        "and the chart is dropped mid-thread")


@pytest.mark.parametrize("query", DRUG_PHRASES)
def test_a_drug_name_keeps_the_chart_on_a_bound_thread(ward, query):
    """The gate in isolation is not the claim — the claim is that the chart
    survives the turn. Same assertions as the contraction battery above."""
    a = ward["a"]
    context, scope, _s, _c, _e, pctx, qscope = _turn(
        query, ward["doctor"], patient_id=a["id"], history=_thread(a["name"]))
    assert qscope == "PATIENT_ANCHORED", f"{query!r} suppressed the anchor"
    assert pctx is not None and pctx["demographics"]
    assert a["name"] in context
    assert scope == "patient"


def test_a_brand_name_from_the_hospital_formulary_is_recognised():
    """The gate reads the SAME catalogue the mutation gate does
    (helix_medication_master, folded in at startup). A brand the hospital
    stocks and the built-in floor has never heard of must still not read as a
    person."""
    before = sage_service._FORMULARY_TERMS
    try:
        assert sage_service._names_a_specific_patient(
            "Is Xyzalcef right for her?", "is xyzalcef right for her?") is True, (
            "precondition: this brand is unknown before the catalogue loads")
        sage_service.register_formulary_terms(["Xyzalcef 1g Injection"])
        assert sage_service._names_a_specific_patient(
            "Is Xyzalcef right for her?", "is xyzalcef right for her?") is False
    finally:
        sage_service._FORMULARY_TERMS = before


@pytest.mark.parametrize("query", [
    # A drug in the sentence must not launder a REAL name out of it.
    "Is Ceftriaxone right for Rajesh?",
    "Should Priya have Vancomycin?",
    "Start Monocef for Mr. Sharma",
    "Does bed 12 need Paracetamol?",
    # …and the plain wrong-patient shapes still fire.
    "Rajesh is deteriorating",
    "Priya needs review",
    "Sharma for discharge today?",
    "Any update on Priya?",
])
def test_a_real_patient_name_still_reads_as_naming_a_patient(query):
    """The other direction, and the expensive one: a false negative here
    disarms the cross-department denial and the no-such-patient grounding, which
    is how a turn gets answered off the WRONG chart."""
    assert sage_service._names_a_specific_patient(query, query.lower()) is True
