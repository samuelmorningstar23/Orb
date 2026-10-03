"""The chart-open prefetch must actually be CONSUMED by the interactive turn.

THE DEFECT: warm_patient_context pre-assembled Channel B/C guideline RAG into
sage_decision_cache ('channel_b_c_guidelines'), but the interactive turn read
ONLY 'channel_a_facts' — so the prefetched retrieval was dead weight and
_get_guideline_context still ran the full embed+rerank live inside every
guideline-backed turn (0.7-5.0 s the clinician waited, measured 2026-07-31).

THE FIX under test: the warmer now also stores the RAW candidate pool
('channel_b_c_candidates'); a patient turn reranks that pool against the LIVE
question (cross-encoder/lexical scoring only — no embed, no FTS) and falls back
to full live retrieval whenever the cache is missing, stale (young-half-of-TTL
gate, mirroring SagePreloader's _cache_is_fresh), or the pool cannot ground the
question. The cache may only ever buy latency — these tests are the proof it
never buys it with grounding.

LANE PARITY (DEF-4): grounding on this lane means the SAME calibrated relevance
floor the live lane runs inside search_guidelines, applied to EVERY candidate.
The warm lane once carried its own hand-picked floor checked against the top
candidate's ordering score only, so candidates 2..limit reached `sources`
unexamined; the last two tests in section 2 are the regression proof.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import db_session
import services.clinical_knowledge as ck
import services.nervous_system_triggers as nst
import services.sage_service as sage
from services.sage_service import (
    _get_guideline_context,
    _rerank_warm_pool,
    _warm_pool_is_fresh,
    get_relevant_context,
)

DEPT = "Warm Retrieval Medicine"

# A general clinical question (no name, no pronoun) so a request-supplied
# patient_id anchors the turn — the exact path the chart-open prewarm serves.
QUERY = "When starting empiric antibiotics in sepsis, how often must lactate be remeasured?"


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _force_lexical_reranker(monkeypatch):
    """Pin rerank_results to the deterministic lexical scorer (same idiom as
    test_reranker.py): on a full install the ~2 GB cross-encoder would otherwise
    load mid-test and score with model weights instead of arithmetic."""
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", True)


def _mk_user():
    uid = f"WRMU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, 'doctor', 'x', ?)",
            (uid, "Warm Retrieval Doctor", DEPT),
        )
    return {"id": uid, "role": "doctor", "department": DEPT, "name": "Warm Retrieval Doctor"}


def _mk_patient():
    pid = f"WRMP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WRMADM', 'Warm Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, admission_date) "
            "VALUES (?, ?, ?, 'WR-1', ?, 58, 'M', 'Community-acquired pneumonia', 'admitted', ?)",
            (pid, f"Warmpool Testpatient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8]}", DEPT,
             datetime.now(timezone.utc).isoformat()),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'WR-1', 'active', 'WRMADM')",
            (pid, DEPT, DEPT),
        )
    return pid


def _facts(pid):
    return {
        "patient_id": pid,
        "demographics": f"PATIENT: Warmpool Testpatient, 58y/M, Bed WR-1, Ward {DEPT}",
        "vitals": ["HR:104 BP:98/60 SpO2:93% Temp:38.4°C RR:24"],
        "vital_trends": "", "risk": None,
        "medications": [], "recent_labs": [], "recent_notes": [],
        "scribe_notes": "", "imaging": "",
        "completeness": {"has_vitals": True, "has_meds": False, "has_labs": False,
                         "has_imaging": False, "has_notes": False,
                         "has_risk_score": False, "missing": []},
    }


def _doc(document_id, title, content, tier, fusion=0.03):
    """One candidate in the shape search_guidelines returns (and the warmer
    JSON-round-trips into the cache)."""
    return {
        "id": abs(hash(document_id)) % 100000, "document_id": document_id,
        "title": title, "content": content, "source": "Warm Test Corpus",
        "source_type": "policy", "source_origin": "test", "trust_tier": tier,
        "is_synthetic": False, "retrieval_method": "hybrid", "fusion_score": fusion,
    }


# The three sepsis documents must clear the relevance floor by a real margin,
# because that floor is CALIBRATED DATA (server/eval/retrieval_thresholds.json)
# and moves when the corpus is recalibrated — on 2026-08-15 the lexical floor
# went 0.28976 -> 0.41877 in one commit. Written to the old floor these bodies
# sat +0.0003 above it and the file's green status meant nothing. So they use
# the question's own vocabulary verbatim ("starting", "often", "must",
# "remeasured") rather than near-misses the tokenizer does not stem together.
ON_TOPIC_POOL = [
    _doc("LOC-SEP-101", "Sepsis empiric antibiotics and lactate remeasurement",
         "Starting empiric antibiotics in sepsis must not be delayed beyond one "
         "hour; lactate must then be remeasured within two hours, and how often "
         "it is repeated depends on clearance.", tier=1, fusion=0.035),
    _doc("LOC-SEP-102", "Sepsis fluid resuscitation and lactate clearance",
         "Give 30 ml/kg crystalloid for hypotension in sepsis; lactate must be "
         "remeasured after the bolus, and how often perfusion is reassessed "
         "depends on the response to starting fluids.", tier=2, fusion=0.030),
    _doc("EXT-SEP-201", "Surviving sepsis bundle guidance",
         "Obtain cultures before starting empiric antibiotics in sepsis; "
         "antibiotics must be given promptly and lactate remeasured as often as "
         "clearance requires.", tier=3, fusion=0.028),
    # Deliberate junk: shares no content term with QUERY, so it is the pool's
    # built-in proof that the floor drops what does not belong.
    _doc("EXT-GEN-301", "General ward observation frequency",
         "Observation frequency depends on acuity and local escalation policy.",
         tier=3, fusion=0.020),
]

# Zero term overlap with QUERY. Note the ORDERING score still reaches 0.25 here
# (trust 0.15 + prior 0.10 are free), which is why the gate must be the pure
# relevance score — no trust, no fusion prior — where these score exactly 0.0.
OFF_TOPIC_POOL = [
    _doc("LOC-ORT-401", "Plaster cast aftercare",
         "Elevate the limb, check distal colour and swelling daily, keep the "
         "plaster dry.", tier=1, fusion=0.035),
    _doc("LOC-ORT-402", "Crutch fitting", "Adjust crutch height to wrist crease; "
         "review gait before discharge.", tier=2, fusion=0.030),
]

# One genuinely relevant hit trailed by irrelevant ones — the pool shape that
# passes a top-candidate-only gate and then cites the tail as evidence.
MIXED_POOL = [ON_TOPIC_POOL[0], *OFF_TOPIC_POOL]

# Also orthopaedics, but each document shares ONE incidental word with the
# question ("must", "starting"). On the ordering blend that trivial overlap
# (0.071) rides on top of the free trust+prior 0.25 to reach 0.3213 — over the
# 0.30 the warm lane used to gate on — while the relevance score, which has no
# priors to ride, puts them at 0.0950, far under any calibrated floor. This is
# why the gate has to be the relevance score: on the lexical path a threshold
# applied to the ordering score is a threshold on trust tier and fusion rank,
# not on topic.
PRIOR_RIDING_POOL = [
    _doc("LOC-ORT-501", "Plaster cast aftercare",
         "Elevate the limb and check distal colour daily; the plaster must be "
         "kept dry.", tier=1, fusion=0.035),
    _doc("LOC-ORT-502", "Crutch fitting",
         "Adjust crutch height to the wrist crease before starting mobilisation.",
         tier=1, fusion=0.035),
]

LIVE_RESULTS = [
    _doc("LIVE-SEP-901", "Live retrieval sepsis document",
         "Empiric antibiotics for sepsis with lactate remeasurement guidance.",
         tier=1, fusion=0.040),
]


def _seed_cache(pid, candidates, *, age_minutes=0.0, ttl_minutes=10.0, facts=None):
    now = datetime.now(timezone.utc)
    preloaded = (now - timedelta(minutes=age_minutes)).isoformat()
    expires = (now - timedelta(minutes=age_minutes) + timedelta(minutes=ttl_minutes)).isoformat()
    compiled = {
        "channel_a_facts": facts if facts is not None else _facts(pid),
        "channel_b_c_guidelines": "",
        "sources": [c["document_id"] for c in (candidates or [])],
        "confidence": "high",
        "preloaded_at": preloaded,
        "reason": "test",
    }
    if candidates is not None:
        compiled["channel_b_c_candidates"] = candidates
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO sage_decision_cache "
            "(patient_id, mews_score, compiled_context, preloaded_at, expires_at) "
            "VALUES (?, 0, ?, ?, ?)",
            (pid, json.dumps(compiled), preloaded, expires),
        )


def _poison_live_retrieval(monkeypatch):
    """Any live retrieval work — embed OR full search — fails the test."""
    def _no_embed(*a, **k):
        raise AssertionError("live embed ran on a warm-cache turn")

    def _no_search(*a, **k):
        raise AssertionError("live search_guidelines ran on a warm-cache turn")

    monkeypatch.setattr(ck, "_embed_text", _no_embed)
    monkeypatch.setattr(ck, "search_guidelines", _no_search)


def _record_live_retrieval(monkeypatch):
    calls = []

    def _recorder(query, limit=5, **kw):
        calls.append(query)
        return [dict(d) for d in LIVE_RESULTS]

    monkeypatch.setattr(ck, "search_guidelines", _recorder)
    return calls


# ── 1. The cache-hit turn consumes the pool — no live retrieval at all ────────

def test_fresh_cache_turn_uses_prefetched_pool_and_never_embeds(monkeypatch):
    user, pid = _mk_user(), _mk_patient()
    _seed_cache(pid, ON_TOPIC_POOL)
    _poison_live_retrieval(monkeypatch)

    context, scope, sources, conf, esc, pctx, qscope = get_relevant_context(
        QUERY, user, patient_id=pid)

    assert pctx is not None, "cached Channel A facts were not served"
    assert sources, "warm pool was not consumed (retrieval came back empty)"
    # Reranked against the LIVE question: the doc holding the query's terms wins.
    assert sources[0]["document_id"] == "LOC-SEP-101"
    assert "CHANNEL B (LOCAL POLICY/FORMULARY):" in context
    assert "(DOC: LOC-SEP-101)" in context


def test_warm_hit_logs_timing_and_cache_flag_without_content(caplog):
    caplog.set_level(logging.INFO, logger="orb.sage")
    text, results, conf = _get_guideline_context(QUERY, warm_candidates=ON_TOPIC_POOL)
    assert results
    lines = [r.getMessage() for r in caplog.records
             if "retrieval_ms=" in r.getMessage()]
    assert lines, "no retrieval timing log emitted"
    assert "cache_used=True" in lines[-1]
    # Counts only: neither the question nor a document title may reach the log.
    assert "lactate" not in lines[-1].lower()
    assert "sepsis" not in lines[-1].lower()


# ── 2. Every degraded state falls back to FULL live retrieval ─────────────────

def test_cache_row_without_candidates_falls_back_live(monkeypatch):
    user, pid = _mk_user(), _mk_patient()
    _seed_cache(pid, None)  # pre-fix row shape: facts cached, no candidate pool
    calls = _record_live_retrieval(monkeypatch)

    context, scope, sources, conf, esc, pctx, qscope = get_relevant_context(
        QUERY, user, patient_id=pid)

    assert pctx is not None
    assert calls, "live retrieval did not run despite an unconsumable cache"
    assert sources and sources[0]["document_id"] == "LIVE-SEP-901"


def test_stale_cache_falls_back_live_but_still_serves_facts(monkeypatch):
    user, pid = _mk_user(), _mk_patient()
    # Unexpired but past the young half of its TTL (age 8 of 10 minutes): facts
    # may still be served (their perishable risk block is recomputed), the
    # retrieval pool may not.
    _seed_cache(pid, ON_TOPIC_POOL, age_minutes=8.0, ttl_minutes=10.0)
    calls = _record_live_retrieval(monkeypatch)

    context, scope, sources, conf, esc, pctx, qscope = get_relevant_context(
        QUERY, user, patient_id=pid)

    assert pctx is not None, "stale-pool gate must not discard cached facts"
    assert calls, "stale pool must trigger full live retrieval"
    assert sources and sources[0]["document_id"] == "LIVE-SEP-901"


def test_off_topic_pool_falls_back_live(monkeypatch):
    """NEVER trade grounding for speed: a pool that cannot cover the question
    (no candidate clears the shared relevance floor) pays the full live path."""
    user, pid = _mk_user(), _mk_patient()
    _seed_cache(pid, OFF_TOPIC_POOL)
    calls = _record_live_retrieval(monkeypatch)

    context, scope, sources, conf, esc, pctx, qscope = get_relevant_context(
        QUERY, user, patient_id=pid)

    assert calls, "an off-topic warm pool was consumed instead of falling back"
    assert sources and sources[0]["document_id"] == "LIVE-SEP-901"


def test_missing_cache_row_runs_live(monkeypatch):
    user, pid = _mk_user(), _mk_patient()  # no cache row at all
    calls = _record_live_retrieval(monkeypatch)

    context, scope, sources, conf, esc, pctx, qscope = get_relevant_context(
        QUERY, user, patient_id=pid)

    assert calls
    assert sources and sources[0]["document_id"] == "LIVE-SEP-901"


def test_no_relevance_verdict_is_never_concluded_off_the_warm_pool(monkeypatch):
    """A pool can clear the rerank floor and still trip the calibrated
    no-relevance gate (its fusion priors are hint-shaped). Declaring "no
    relevant guideline exists" is a grounding claim, so it must be re-checked
    against FULL live retrieval before the turn ships an empty context."""
    import services.retrieval_calibration as cal
    monkeypatch.setattr(cal, "load_thresholds", lambda force_reload=False: {
        **cal.DEFAULT_THRESHOLDS,
        "no_relevance_score_floor": 10.0, "no_relevance_lexical_floor": 10.0,
    })
    calls = _record_live_retrieval(monkeypatch)

    text, results, conf = _get_guideline_context(QUERY, warm_candidates=ON_TOPIC_POOL)

    assert calls, "warm 'none' verdict was not re-checked against live retrieval"
    # Live retrieval also classified 'none' under these floors → empty context,
    # which is now a verdict about the CORPUS, not about a prefetched pool.
    assert (text, results, conf) == ("", [], "low")


def test_every_warm_citation_clears_the_shared_relevance_floor():
    """LANE PARITY (DEF-4). The floor is per-CANDIDATE, not per-pool: it is not
    enough that the pool's best hit is on topic, because every entry returned
    here becomes a citation in `sources`.

    MIXED_POOL is the shape the old gate let through — one strong hit followed
    by tier-1/tier-2 documents with zero query overlap. Checking only the top
    candidate passed the pool, and the ortho documents were then cited to a
    clinician as sepsis evidence.
    """
    results = _rerank_warm_pool(QUERY, MIXED_POOL, 3)

    assert results, "the on-topic candidate should still ground the turn"
    ids = [r["document_id"] for r in results]
    assert ids == ["LOC-SEP-101"], f"irrelevant candidates were cited: {ids}"
    # The survivor cleared the SHARED floor — same function, same calibrated
    # numbers (server/eval/retrieval_thresholds.json) as the live lane.
    for r in results:
        assert r["relevance_score"] >= ck._relevance_floor(r["relevance_method"])


@pytest.mark.parametrize("pool_name", ["OFF_TOPIC_POOL", "PRIOR_RIDING_POOL"])
def test_all_irrelevant_warm_pool_yields_no_citations(monkeypatch, pool_name):
    """A pool where NOTHING is relevant must produce no citations at all.

    PRIOR_RIDING_POOL is the case a threshold on the ORDERING score cannot
    catch: those documents reach 0.3213 — past the 0.30 this lane used to gate
    on — on trust tier, fusion rank, and one incidental shared word. Scored for
    relevance, with no priors to ride, they sit at 0.0950 and are refused.
    """
    pool = globals()[pool_name]
    assert _rerank_warm_pool(QUERY, pool, 3) is None

    # End to end, with the corpus holding nothing either: an empty context is
    # the honest answer, and no ortho document reaches `sources`.
    monkeypatch.setattr(ck, "search_guidelines", lambda *a, **kw: [])
    text, results, conf = _get_guideline_context(QUERY, warm_candidates=pool)

    assert (text, results, conf) == ("", [], "low")


def test_warm_lane_floors_the_whole_pool_before_truncating(monkeypatch):
    """Truncation happens AFTER the floor, mirroring search_guidelines.

    Sequencing is the invariant, so the ranker is stubbed to a fixed order
    rather than pinned to score arithmetic near a calibrated threshold: with the
    only relevant document ranked below `limit`, a `top_k=limit` cut taken first
    would keep two zero-relevance documents, floor them both, and send the turn
    live with the answer already sitting in the pool.
    """
    monkeypatch.setattr(ck, "rerank_results", lambda q, c, top_k=None: (
        [dict(d) for d in (OFF_TOPIC_POOL[0], OFF_TOPIC_POOL[1], ON_TOPIC_POOL[0])]))

    results = _rerank_warm_pool(QUERY, MIXED_POOL, 2)

    assert results and [r["document_id"] for r in results] == ["LOC-SEP-101"]


# ── 3. The freshness gate itself ──────────────────────────────────────────────

@pytest.mark.parametrize("age_min,ttl_min,fresh", [
    (0.0, 10.0, True),      # just written
    (4.9, 10.0, True),      # young half
    (5.1, 10.0, False),     # old half — stale for consumption
    (9.5, 10.0, False),
    (2.9, 6.0, True),       # shorter chart-open TTL, same idiom
    (3.1, 6.0, False),
])
def test_warm_pool_freshness_mirrors_the_ttl_half_idiom(age_min, ttl_min, fresh):
    now = datetime.now(timezone.utc)
    row = {
        "preloaded_at": (now - timedelta(minutes=age_min)).isoformat(),
        "expires_at": (now - timedelta(minutes=age_min)
                       + timedelta(minutes=ttl_min)).isoformat(),
    }
    assert _warm_pool_is_fresh(row) is fresh


def test_warm_pool_freshness_fails_closed_on_garbage():
    assert _warm_pool_is_fresh({"preloaded_at": "not-a-date", "expires_at": "also-no"}) is False
    assert _warm_pool_is_fresh({"preloaded_at": None, "expires_at": None}) is False


# ── 4. Prompt-budget invariants hold on the warm path ─────────────────────────

def test_warm_path_keeps_budget_top3_and_local_first_ordering(monkeypatch):
    """The formatting contract is shared with the live path and must survive the
    new results source: ORB_SAGE_CONTEXT_CHARS budget, top-3, local policy
    served (and clipped) before the external tail, omitted bodies keep their
    DOC_ID citable."""
    monkeypatch.setenv("ORB_SAGE_CONTEXT_CHARS", "400")
    # No sentence boundaries in the bodies → _clip cannot shorten below the
    # per-entry cap, so two local bodies exhaust the 400-char budget and the
    # external entry must be header-only.
    #
    # The bodies carry the question's vocabulary because the warm lane now runs
    # the same relevance floor as the live lane: this test is about the
    # FORMATTING contract, so its documents have to be ones Orb would actually
    # cite. With on-topic titles over an off-topic body they scored below the
    # floor and only one survived to be formatted.
    soup = " ".join(["sepsis empiric antibiotics lactate remeasured monitoring"] * 26)
    pool = [
        _doc("LOC-BUD-1", "Sepsis empiric antibiotics lactate remeasurement policy",
             soup, tier=1, fusion=0.035),
        _doc("LOC-BUD-2", "Sepsis antibiotics timing formulary lactate",
             soup, tier=2, fusion=0.033),
        _doc("EXT-BUD-3", "External sepsis antibiotics lactate guidance",
             soup, tier=3, fusion=0.031),
        _doc("EXT-BUD-4", "External sepsis lactate appendix",
             soup, tier=3, fusion=0.020),
    ]
    text, results, conf = _get_guideline_context(QUERY, warm_candidates=pool)

    assert len(results) == 3, "top-3 invariant broke on the warm path"
    assert text.count("(DOC: ") == 3
    assert text.index("CHANNEL B (LOCAL POLICY/FORMULARY):") < text.index(
        "CHANNEL C (EXTERNAL GUIDANCE):")
    # Local bodies spent the budget; the external tail is clipped to its header
    # but stays citable by DOC_ID.
    assert "[passage omitted for length: cite by DOC_ID if relevant]" in text
    ext_at = text.index("CHANNEL C (EXTERNAL GUIDANCE):")
    assert "(DOC: EXT-BUD-3)" in text[ext_at:]
    assert "omitted for length" in text[ext_at:]
    assert "omitted for length" not in text[:ext_at]
    # The citation metadata path is whole: full dicts, never truncated.
    assert all(r.get("content") for r in results)


# ── 5. Round trip: the warmer writes the pool, the turn consumes it ───────────

async def test_warm_write_then_interactive_consume_round_trip(monkeypatch):
    user, pid = _mk_user(), _mk_patient()

    monkeypatch.setattr(nst, "_assemble_patient_context",
                        lambda db, patient_id, dept: _facts(patient_id))
    monkeypatch.setattr(nst, "_get_guideline_context",
                        lambda hint, limit=None: ("FORMATTED B/C", [dict(d) for d in ON_TOPIC_POOL], "high"))
    import services.world_model_service as wms
    monkeypatch.setattr(wms, "update_patient_state", lambda *a, **k: None)

    assert await nst.warm_patient_context(pid, query_hint="sepsis",
                                          ttl_minutes=10, reason="manual")

    with db_session() as db:
        row = db.execute(
            "SELECT compiled_context FROM sage_decision_cache WHERE patient_id = ?",
            (pid,)).fetchone()
    compiled = json.loads(row["compiled_context"])
    assert compiled["channel_b_c_candidates"] == ON_TOPIC_POOL
    assert compiled["sources"] == [d["document_id"] for d in ON_TOPIC_POOL]
    assert "channel_b_c_guidelines" in compiled  # legacy cache shape intact

    _poison_live_retrieval(monkeypatch)
    context, scope, sources, conf, esc, pctx, qscope = get_relevant_context(
        QUERY, user, patient_id=pid)
    assert sources and sources[0]["document_id"] == "LOC-SEP-101"
    assert "(DOC: LOC-SEP-101)" in context
