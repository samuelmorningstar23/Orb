"""DEF-4 — evidence must be relevant, and "no guideline matched" must be sayable.

WHAT WENT WRONG ON THE WARD (2026-08-14): "Ventilator Weaning Protocol" was
cited as evidence on EVERY drafted assessment — diabetic ketoacidosis, dengue,
community-acquired pneumonia with sepsis and a UTI alike — alongside "Acute
Heart Failure Management". Only three distinct evidence sets across every draft
produced, and the `evidence_note` path for "no guideline matched" had never once
fired.

WHY RETRIEVAL COULD NOT SAY NO (all measured against the seeded corpus):
  * dense retrieval runs `k = 15` unconditionally — 15 neighbours come back
    however far away they are (nearest chunk to a dengue query: cosine distance
    0.9963, i.e. orthogonal);
  * the lexical lane ORs every token longer than 3 characters, and "SpO2"
    appears in exactly ONE of the 19 seed documents — the Ventilator Weaning
    Protocol — so every vitals-shaped query matched it lexically;
  * RRF fuses on RANK ONLY, so `fusion_score` is capped at 2/(60+1) = 0.032787
    and is indistinguishable between a perfect hit ("diabetic ketoacidosis
    insulin infusion" → 0.032787) and garbage ("where is the hospital parking
    garage and how much does it cost?" → 0.032522);
  * consequently `search_guidelines` could not return [] on a non-empty corpus,
    so `sources` was never empty, so `evidence_note` could never fire.

THE INVARIANTS PINNED HERE:
  1. clinically unrelated conditions do not come back with the same documents;
  2. a condition the corpus does not cover produces NO evidence rather than a
     low-scoring document;
  3. relevance is scored separately from ranking — trust tier and fusion prior,
     which are ranking inputs, cannot make an off-topic passage citable;
  4. the floor that enforces all of the above is DERIVED from a labelled set and
     carries its provenance, rather than being a hand-picked magic number;
  5. THE QUERY SAYS WHAT THE PATIENT HAS (section 5, added 2026-08-15). The
     four above are all about the retriever, and the retriever was never the
     root cause. Both lanes built the query from the NEWS2 drivers alone —
     "Tachypnea (RR 24) Tachycardia (HR 122)" — which is why four unrelated
     conditions sent identical text. The floor turned the wrong citations into
     no citations, which is honest but is still a miss when the guideline is
     sitting in the corpus. See services/retrieval_query.py.

The corpus-level tests run on whichever relevance scorer the machine provides
(cross-encoder when the reranker weights are installed, lexical otherwise) —
both were verified to satisfy these assertions, and running on whatever ships is
the point.
"""
import json
import os

import pytest

from database import db_session
from services import clinical_knowledge as ck
from services import corpus_loader
from services import retrieval_calibration as rc
from services import retrieval_query as rq
from tests.knowledge_fixtures import lexical_knowledge_models

CORPUS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "corpus")

_SHARED_TABLES = (
    "clinical_knowledge_vec",
    "clinical_knowledge_fts",
    "clinical_knowledge",
    "clinical_knowledge_meta",
    "corpus_documents",
)


def _drop_store():
    with db_session() as db:
        # sqlite-vec must be loaded to DROP the vec0 virtual table and its shadow
        # tables; without it the drop errors and a later rebuild hits UNIQUE
        # constraint failures.
        try:
            import sqlite_vec
            db.enable_load_extension(True)
            sqlite_vec.load(db)
        except Exception:
            pass
        for t in _SHARED_TABLES:
            try:
                db.execute(f"DROP TABLE IF EXISTS {t}")
            except Exception:
                pass
        db.commit()


@pytest.fixture(scope="module")
def seeded_corpus():
    """Exercise the real seeded FTS store without depending on cached weights.

    The suite's fast-boot setting suppresses automatic knowledge seeding. Seed
    explicitly, with unavailable dense retrieval and cross-encoder, so these
    assertions verify genuine lexical relevance rather than an empty store.
    Actual local model compatibility is checked separately.
    """
    with lexical_knowledge_models() as seed_builtin:
        _drop_store()
        seed_builtin()
        corpus_loader.load_directory(CORPUS_DIR, default_trust_tier=3)
        yield
        _drop_store()


@pytest.fixture(autouse=True)
def _reset_threshold_cache():
    """load_thresholds() memoises; tests here move the file around."""
    rc.load_thresholds(force_reload=True)
    yield
    rc._cached = None


# ─────────────────────────────────────────────────────────────────────────────
# The five ward conditions from the dry run.
#
# Three are covered by the corpus and three are not — that split is the whole
# test. `expected_any` lists the document_ids that legitimately answer the
# query; None means the corpus genuinely has nothing (verified 2026-08-14: no
# dengue document, no COPD document, in either the built-in corpus or
# server/corpus/), so the honest answer is no evidence at all.
# ─────────────────────────────────────────────────────────────────────────────
CONDITIONS = [
    ("DKA", "diabetic ketoacidosis insulin infusion potassium replacement",
     {"EXT-METAB-001"}),
    ("dengue", "dengue fever warning signs plasma leakage platelet monitoring",
     None),
    ("sepsis_cap", "community acquired pneumonia with sepsis empiric antibiotics",
     {"EXT-ID-001", "REF-CAP-CURB65-001", "FORM-ID-001"}),
    ("UTI", "urinary tract infection treatment nitrofurantoin",
     {"EXT-ID-002"}),
    ("COPD", "COPD exacerbation bronchodilators steroids target oxygen saturation",
     None),
]


def _evidence(query, **kw):
    return [r["document_id"] for r in ck.search_guidelines(query, limit=3, **kw)]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Unrelated conditions must not share an evidence set
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,query,expected_any", CONDITIONS,
                         ids=[c[0] for c in CONDITIONS])
def test_condition_gets_its_own_guideline_or_none(seeded_corpus, name, query, expected_any):
    """Each condition retrieves a guideline that is actually about it — or,
    where the corpus has nothing, retrieves nothing."""
    ids = _evidence(query)
    if expected_any is None:
        assert ids == [], (
            f"{name} is not covered by the corpus, so citing {ids} is a promise "
            "Orb cannot keep — the no-evidence path must fire instead"
        )
    else:
        assert set(ids) & expected_any, (
            f"{name} should surface one of {sorted(expected_any)}, got {ids}"
        )


def test_no_single_guideline_is_cited_for_every_condition(seeded_corpus):
    """The headline defect: ONE document appearing on every assessment.

    Asserted as an intersection over the conditions that return evidence at all,
    so it stays meaningful however retrieval is tuned later.
    """
    sets = {name: set(_evidence(query)) for name, query, _ in CONDITIONS}
    answered = [s for s in sets.values() if s]
    assert len(answered) >= 2, f"too few conditions answered to test sharing: {sets}"
    shared = set.intersection(*answered)
    assert not shared, (
        f"{sorted(shared)} is cited for every answered condition — that is the "
        f"DEF-4 signature. Full sets: { {k: sorted(v) for k, v in sets.items()} }"
    )


def test_ventilator_weaning_is_not_cited_for_unrelated_conditions(seeded_corpus):
    """The exact document the clinician saw on every draft. Named explicitly:
    a generic intersection test would go quiet if the corpus grew a second
    universally-retrieved document, and this one is the one that was seen."""
    for name, query, expected_any in CONDITIONS:
        if expected_any and "EXT-VENT-001" in expected_any:
            continue
        assert "EXT-VENT-001" not in _evidence(query), (
            f"Ventilator Weaning Protocol cited for {name}"
        )


def test_clinically_unrelated_queries_return_different_documents(seeded_corpus):
    """DKA and a UTI have nothing to do with each other; their evidence must
    have nothing in common."""
    dka = set(_evidence("diabetic ketoacidosis insulin infusion potassium replacement"))
    uti = set(_evidence("urinary tract infection treatment nitrofurantoin"))
    assert dka and uti, "both conditions are in the corpus and must return evidence"
    assert not (dka & uti), f"DKA and UTI share evidence: {sorted(dka & uti)}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. The honest "no guideline matched" path
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("query", [
    "dengue fever warning signs plasma leakage platelet monitoring",
    "COPD exacerbation bronchodilator and corticosteroid dosing",
    "Snakebite anti-snake venom dosing and observation period",
    "Eclampsia magnesium sulphate loading dose in pregnancy",
    "What is the wifi password for the visitor lounge?",
])
def test_no_corpus_match_produces_no_evidence(seeded_corpus, query):
    """A query with nothing relevant in the corpus returns [], not the closest
    low-scoring document. [] is what makes sage_assessment_service's
    `evidence_note` ("No guideline in Orb's corpus matched this alert") reachable
    — see sage_assessment_service.py:2355, which keys off `not evidence`."""
    assert ck.search_guidelines(query, limit=3) == []


def test_the_floor_is_what_produces_the_empty_result(seeded_corpus, monkeypatch):
    """Prove-can-fail, built in: disabling the floor restores the defect exactly.

    With ORB_RETRIEVAL_RELEVANCE_FLOOR=off the dengue query goes back to citing
    whatever the fusion surfaced. If this test ever stops seeing documents here,
    the empty result above is coming from something other than the floor and the
    test above has stopped proving what it claims.
    """
    query = "dengue fever warning signs plasma leakage platelet monitoring"
    assert ck.search_guidelines(query, limit=3) == []
    monkeypatch.setenv("ORB_RETRIEVAL_RELEVANCE_FLOOR", "off")
    unfiltered = ck.search_guidelines(query, limit=3)
    assert unfiltered, "the pre-fix pipeline always surfaced SOMETHING for dengue"
    assert all(r["relevance_score"] < 0.2 for r in unfiltered), (
        "the documents the old path cited for dengue scored near zero relevance: "
        f"{[(r['document_id'], round(r['relevance_score'], 4)) for r in unfiltered]}"
    )


def test_in_corpus_query_still_returns_evidence(seeded_corpus):
    """The floor must not be a blanket mute — retrieval that used to work still
    works. (Guards against 'fixing' DEF-4 by returning [] for everything.)"""
    assert _evidence("DKA insulin infusion potassium replacement bicarbonate")
    assert _evidence("sepsis qSOFA lactate screening criteria")
    assert _evidence("STEMI door to balloon time primary PCI")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Relevance is scored separately from ranking
# ─────────────────────────────────────────────────────────────────────────────
def _candidate(**kw):
    base = {"document_id": "D", "title": "", "content": "", "trust_tier": 3,
            "fusion_score": 0.0}
    base.update(kw)
    return base


def test_zero_overlap_scores_zero_relevance_but_nonzero_rank():
    """The bug in one assertion.

    The RANKING blend gives an off-topic passage _W_TRUST * trust + _W_PRIOR *
    prior — up to 0.25 — for sharing not one word with the query. A relevance
    score used to decide whether Orb makes a citation claim must give it zero.
    """
    q_terms = ck._rerank_terms("dengue plasma leakage platelet")
    cand = _candidate(title="Ventilator Weaning Protocol",
                      content="spontaneous breathing trial readiness RSBI cuff leak",
                      trust_tier=1, fusion_score=0.0328)

    assert ck._lexical_relevance_score(q_terms, cand) == 0.0

    ranking = ck._lexical_rerank_score(q_terms, cand, max_prior=0.0328)
    assert ranking >= ck._W_TRUST, (
        "the ranking blend is expected to carry a trust/prior floor — that is "
        "why it must not be reused as the relevance decision"
    )


def test_trust_tier_cannot_rescue_an_irrelevant_candidate():
    """A local hospital SOP is the most TRUSTED source in the corpus. Trust is
    how much a source is to be believed; it is never evidence that the source is
    on topic."""
    tier1_offtopic = _candidate(document_id="SOP-INF-001", trust_tier=1,
                                title="Infection Control Policy",
                                content="hand hygiene gowns gloves sporicidal wipes",
                                fusion_score=0.0328)
    kept = ck.apply_relevance_floor("thyroid storm beta blockade management",
                                    [tier1_offtopic])
    assert kept == []


def test_apply_floor_returns_empty_when_nothing_clears():
    cands = [_candidate(document_id=f"D{i}", content="unrelated filler text about weather")
             for i in range(4)]
    assert ck.apply_relevance_floor("dengue platelet plasma leakage", cands) == []


def test_apply_floor_keeps_the_relevant_and_drops_the_rest():
    cands = [
        _candidate(document_id="HIT",
                   content="diabetic ketoacidosis insulin infusion potassium bicarbonate"),
        _candidate(document_id="MISS", content="ventilator weaning cuff leak RSBI"),
    ]
    kept = ck.apply_relevance_floor("diabetic ketoacidosis insulin potassium", cands)
    assert [c["document_id"] for c in kept] == ["HIT"]
    assert kept[0]["relevance_method"] == "lexical"


def test_cross_encoder_score_is_used_verbatim_as_relevance():
    """When the cross-encoder ran, its normalized output IS P(relevant) and is
    reused rather than recomputed — the model already answered the question."""
    cands = [
        _candidate(document_id="CE-HIGH", rerank_method="cross_encoder", rerank_score=0.97,
                   content="nothing lexically in common with the query at all"),
        _candidate(document_id="CE-LOW", rerank_method="cross_encoder", rerank_score=0.004,
                   content="diabetic ketoacidosis insulin potassium"),
    ]
    scored = ck.score_relevance("diabetic ketoacidosis insulin potassium", cands)
    assert scored[0]["relevance_score"] == pytest.approx(0.97)
    assert scored[0]["relevance_method"] == "cross_encoder"
    assert scored[1]["relevance_score"] == pytest.approx(0.004)
    kept = ck.apply_relevance_floor("diabetic ketoacidosis insulin potassium", cands)
    assert [c["document_id"] for c in kept] == ["CE-HIGH"]


def test_query_with_no_content_terms_does_not_delete_the_corpus():
    """A query that tokenizes to nothing scoreable ("what is the ...?") has
    UNKNOWABLE relevance, not zero. A floor that silenced the whole corpus for a
    stopword-only query would be a new failure, not a fix — so the lexical
    scorer returns 1.0 there and lets the ranking decide."""
    assert ck._rerank_terms("what is the") == [], "precondition: no scoreable terms"
    cands = [_candidate(document_id="D1", content="sepsis lactate antibiotics")]
    assert ck.apply_relevance_floor("what is the", cands) == cands


def test_query_whose_terms_match_nothing_still_yields_no_evidence():
    """The complement of the case above: real content terms that appear nowhere
    in the passage score zero and are dropped."""
    cands = [_candidate(document_id="D1", content="sepsis lactate antibiotics")]
    assert ck.apply_relevance_floor("why", cands) == []


def test_floor_env_override_and_kill_switch(monkeypatch):
    monkeypatch.setenv("ORB_RETRIEVAL_RELEVANCE_FLOOR", "0.9")
    assert ck._relevance_floor("cross_encoder") == pytest.approx(0.9)
    monkeypatch.setenv("ORB_RETRIEVAL_RELEVANCE_FLOOR", "off")
    assert ck._relevance_floor("cross_encoder") == 0.0
    # An unparseable value must not silently become 0.0 (which would disable the
    # floor on a typo); it falls through to the calibrated value.
    monkeypatch.setenv("ORB_RETRIEVAL_RELEVANCE_FLOOR", "high")
    assert ck._relevance_floor("cross_encoder") > 0.0


def test_rerank_disabled_still_applies_the_floor(seeded_corpus):
    """Opting out of REORDERING is not opting into citing irrelevant guidelines."""
    assert ck.search_guidelines(
        "dengue fever warning signs plasma leakage", limit=3, rerank=False) == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. The floor is derived, not magic
# ─────────────────────────────────────────────────────────────────────────────
def test_shipped_thresholds_are_calibrated_with_provenance():
    """A thresholds file with no record of what produced it is a fresh set of
    magic numbers with extra steps."""
    assert os.path.exists(rc.THRESHOLDS_PATH), (
        "no committed calibration — the runtime would fall back to the "
        "hand-picked defaults this work exists to replace"
    )
    with open(rc.THRESHOLDS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["source"] == "calibrated"
    prov = data["_provenance"]
    assert prov["cases"] >= 40
    assert prov["corpus"]["documents"] > 0
    # Both scorers must have been calibrated: the lexical path is what an
    # appliance without the reranker weights actually runs on.
    assert set(prov["scorers"]) == {"cross_encoder", "lexical"}


def test_shipped_floors_are_derived_and_separate_the_labelled_set():
    data = json.load(open(rc.THRESHOLDS_PATH, encoding="utf-8"))
    for key in ("min_relevance_cross_encoder", "min_relevance_lexical"):
        floor = data[key]
        assert floor != ck._FALLBACK_MIN_RELEVANCE[key.replace("min_relevance_", "")], (
            f"{key} equals the uncalibrated fallback — it was not derived"
        )
        assert 0.0 < floor < 1.0

    # Both scorers must carry their own evidence, measured on the labelled set.
    floors = data["metrics"]["relevance_floor"]
    assert set(floors) == {"cross_encoder", "lexical"}

    ce = floors["cross_encoder"]
    # Placed inside the gap between the two labelled classes, with margin on
    # BOTH sides — not sitting on top of the weakest positive.
    assert ce["max_negative_score"] < ce["floor"]
    assert ce["evidence_kept_on_negatives"] == 0, (
        "the derived floor still lets an out-of-corpus query be cited"
    )
    for method, fm in floors.items():
        assert fm["evidence_kept_on_positives"] >= _min_evidence_kept(method, fm["positives"]), (
            f"the {method} floor is withholding evidence from in-corpus queries: "
            f"{fm['evidence_kept_on_positives']}/{fm['positives']}"
        )
        # The side of the trade the floor exists to hold. BOTH scorers reach
        # zero once the derivation stops treating the false-positive budget as
        # an allowance to spend — the budget is a ceiling for a scorer that
        # cannot reach its own minimum, not a target.
        assert fm["evidence_kept_on_negatives"] == 0, (
            f"the {method} floor lets {fm['evidence_kept_on_negatives']} "
            f"out-of-corpus queries be cited"
        )
        # ── document level (2026-08-15) ──
        # The case-level numbers above go quiet when a floor keeps a query's
        # top guideline and drops its second, equally relevant one. These do
        # not. Without them the harness cannot see the thing it exists to
        # measure — see test_the_harness_scores_every_hit_not_just_the_top_one.
        assert fm["irrelevant_documents_cited"] == 0, (
            f"the {method} floor cites {fm['irrelevant_documents_cited']} "
            f"documents for queries the corpus cannot answer"
        )
        assert fm["unlabelled_cases"] == 0, (
            f"{fm['unlabelled_cases']} positive case(s) carry no `expected` "
            "document list, so this run was scored top-hit-only and is blind "
            "to a relevant guideline at rank 2 — the defect this harness had"
        )
        assert fm["relevant_documents_cited"] >= _min_documents_cited(
            method, fm["relevant_documents"]), (
            f"the {method} floor cites only {fm['relevant_documents_cited']} of "
            f"{fm['relevant_documents']} relevant guidelines"
        )


def _min_evidence_kept(method: str, positives: int) -> float:
    """The CASE-level recall bar, PER SCORER — because they are not the same
    instrument and one bar for both would hide which one an appliance is running.

    cross_encoder: a relevance MODEL. It reads query and passage together, so an
        alert-shaped query (a diagnosis followed by vitals) is still a clinical
        question to it. Measured 2026-08-15 on the 62-case set at floor 0.31541:
        29/30 positive cases and 36/43 relevant guidelines kept, 0/32 negatives.
        Held to near-perfect case recall.

    lexical: term overlap, and nothing more. On alert-shaped queries the driver
        terms ("Low SpO2", "Tachypnea", "Tachycardia") are precisely the terms
        shared with documents about OTHER conditions, so they dilute the signal
        on the right guideline and inflate it on the wrong one. The two classes
        genuinely overlap — alert-shaped positives score 0.17-0.35 while
        out-of-corpus negatives reach 0.42 — so the only cut that cites nothing
        wrong sits at 0.41877, above most of the positives: 16/30 cases and
        18/40 guidelines.

        THE DEPLOYMENT CONSEQUENCE, stated here because this is where it is
        measured: on an appliance without the reranker weights, a drafted
        assessment mostly gets NO guideline evidence and says so. That is the
        honest failure and it is the right one — the alternative at this
        scorer's separation is citing ventilator weaning to a dengue patient —
        but it means the reranker is not optional equipment for the assessment
        lane. If either number is ever raised, raise it because the scorer got
        better, never because the floor got lower.
    """
    return positives - 3 if method == "cross_encoder" else positives * 0.5


def _min_documents_cited(method: str, relevant_documents: int) -> float:
    """The DOCUMENT-level recall bar. Same argument as above, one level down:
    of every guideline the corpus holds that legitimately answers a labelled
    query, how many survive the floor. Measured 2026-08-15 — cross-encoder
    36/43, lexical 18/40."""
    return relevant_documents * (0.8 if method == "cross_encoder" else 0.4)


def test_max_margin_floor_leaves_room_on_both_sides():
    """Youden's J alone returns an OBSERVED value, so it lands exactly on the
    weakest positive and leaves zero headroom beneath it. The shipped rule must
    sit strictly inside the gap."""
    pos = [0.55, 0.75, 0.99]
    neg = [0.00002, 0.01, 0.09]

    youden = rc._best_floor(pos, neg)
    assert youden == min(pos), "precondition: the naive cut sits on a real positive"

    floor = rc._max_margin_floor(pos, neg)
    assert max(neg) < floor < min(pos)


def test_max_margin_floor_returns_none_without_both_classes():
    assert rc._max_margin_floor([], [0.1]) is None
    assert rc._max_margin_floor([0.9], []) is None


def test_calibration_derives_the_floor_from_the_labelled_data(monkeypatch, tmp_path):
    """End-to-end: a stub retriever with a KNOWN score separation must produce a
    floor inside that separation — not the fallback constant."""
    def stub(query, limit=5, **kwargs):
        relevant = "sepsis" in query.lower() or "dka" in query.lower()
        score = 0.8 if relevant else 0.02
        return [{"document_id": "D", "title": "t", "content": "c", "trust_tier": 3,
                 "fusion_score": 0.03, "is_synthetic": False, "source": "s",
                 "relevance_score": score, "relevance_method": "cross_encoder"}]

    cal = tmp_path / "cases.json"
    cal.write_text(json.dumps({"cases": [
        {"query": "sepsis bundle first hour", "relevant": True},
        {"query": "DKA insulin infusion", "relevant": True},
        {"query": "sepsis vasopressor choice", "relevant": True},
        {"query": "where is the parking garage", "relevant": False},
        {"query": "wifi password", "relevant": False},
        {"query": "cafeteria hours", "relevant": False},
    ]}))
    monkeypatch.setattr(rc, "THRESHOLDS_PATH", str(tmp_path / "out.json"))
    out = rc.calibrate(search_fn=stub, write=False, calibration_path=str(cal))

    floor = out["min_relevance_cross_encoder"]
    assert 0.02 < floor < 0.8, f"derived floor {floor} is not inside the observed gap"
    assert floor != ck._FALLBACK_MIN_RELEVANCE["cross_encoder"]
    fm = out["metrics"]["relevance_floor"]["cross_encoder"]
    assert fm["evidence_kept_on_positives"] == 3
    assert fm["evidence_kept_on_negatives"] == 0


def test_runtime_reads_the_calibrated_floor(tmp_path, monkeypatch):
    p = tmp_path / "thresholds.json"
    p.write_text(json.dumps({
        **rc.DEFAULT_THRESHOLDS,
        "min_relevance_cross_encoder": 0.42,
        "min_relevance_lexical": 0.37,
        "source": "calibrated",
    }))
    monkeypatch.setattr(rc, "THRESHOLDS_PATH", str(p))
    rc.load_thresholds(force_reload=True)
    assert ck._relevance_floor("cross_encoder") == pytest.approx(0.42)
    assert ck._relevance_floor("lexical") == pytest.approx(0.37)


def test_runtime_falls_back_when_no_calibration_is_committed(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "THRESHOLDS_PATH", str(tmp_path / "absent.json"))
    rc.load_thresholds(force_reload=True)
    assert ck._relevance_floor("cross_encoder") == pytest.approx(
        ck._FALLBACK_MIN_RELEVANCE["cross_encoder"])


def test_the_floor_is_derived_under_a_false_positive_budget():
    """Youden's J weighs a wrong citation exactly as heavily as a withheld one.
    For a safety floor those costs are nowhere near equal, and on the lexical
    scorer that difference was worth eleven irrelevant citations — so the cut is
    chosen for the most recall INSIDE a false-positive budget instead.

    Built to the real measured shape (lexical scorer, 62-case set): a band of
    weak positives sitting inside the negative distribution, so the naive cut
    drops low to collect them and drags a crowd of negatives up with it.
    """
    pos = [0.17] * 11 + [0.40] * 19
    neg = [0.02] * 19 + [0.20] * 11 + [0.50] * 2

    naive = rc._best_floor(pos, neg)
    assert sum(1 for v in neg if v >= naive) >= 10, (
        "precondition: the naive cut admits a crowd of negatives to buy recall"
    )

    floor = rc._max_margin_floor(pos, neg)
    assert sum(1 for v in neg if v >= floor) <= int(len(neg) * rc._MAX_NEGATIVES_KEPT_FRACTION)
    assert sum(1 for v in pos if v >= floor) >= 2, (
        "the budget must buy safety out of recall, not delete the corpus"
    )


def test_the_harness_scores_every_hit_not_just_the_top_one():
    """The blind spot itself. A query whose SECOND result is a relevant
    guideline must put that document's score into the derivation — otherwise a
    floor that drops it moves no number the harness reports, which is how the
    UTI alert lost its antibiotic guideline unnoticed.
    """
    rel = {}
    rc._collect_relevance(
        {"query": "q", "relevant": True, "expected": ["WANTED", "ALSO-WANTED"]},
        [{"document_id": "WANTED", "relevance_score": 0.9, "relevance_method": "cross_encoder"},
         {"document_id": "ALSO-WANTED", "relevance_score": 0.31, "relevance_method": "cross_encoder"},
         {"document_id": "OFF-TOPIC", "relevance_score": 0.5, "relevance_method": "cross_encoder"}],
        rel)
    bucket = rel["cross_encoder"]
    assert sorted(bucket["relevant"]) == [0.31, 0.9], (
        "the rank-2 relevant guideline must reach the derivation"
    )
    assert 0.5 not in bucket["relevant"], (
        "an UNLABELLED candidate under a positive query is not evidence of "
        "anything — it must not be counted as relevant"
    )
    assert bucket["irrelevant"] == [] and bucket["irrelevant_all"] == [], (
        "nor as irrelevant — only negatives, where the label is certain, "
        "contribute irrelevant samples"
    )


def test_a_documents_score_is_its_best_chunk_not_its_worst():
    """`search_guidelines` returns CHUNKS, and one guideline can occupy several
    at very different scores — the sepsis-six query retrieves the NICE sepsis
    document at both 0.959 and 0.006. A guideline is cited when ANY of its
    chunks clears the floor, so pooling chunks would put a relevant document's
    WORST chunk into the relevant distribution. Measured when that happened:
    the derived cross-encoder floor fell from 0.341 to 0.104 and the bare-vitals
    query went back to citing ventilator weaning.
    """
    rel = {}
    rc._collect_relevance(
        {"query": "q", "relevant": True, "expected": ["DOC"]},
        [{"document_id": "DOC", "relevance_score": 0.95, "relevance_method": "cross_encoder"},
         {"document_id": "DOC", "relevance_score": 0.006, "relevance_method": "cross_encoder"}],
        rel)
    assert rel["cross_encoder"]["relevant"] == [0.95]


def test_every_hit_on_a_negative_is_a_false_positive():
    """Negatives need no labels — nothing in the corpus answers them — so every
    candidate they return is a wrong citation waiting to happen, and all of them
    count. The CASE still counts once: three bogus citations on one draft
    mislead one clinician, one citation on three drafts misleads three."""
    rel = {}
    rc._collect_relevance(
        {"query": "q", "relevant": False, "expected": []},
        [{"document_id": "A", "relevance_score": 0.31, "relevance_method": "cross_encoder"},
         {"document_id": "B", "relevance_score": 0.20, "relevance_method": "cross_encoder"},
         {"document_id": "C", "relevance_score": 0.01, "relevance_method": "cross_encoder"}],
        rel)
    bucket = rel["cross_encoder"]
    assert bucket["irrelevant"] == [0.31], "one value per case: its best score"
    assert sorted(bucket["irrelevant_all"]) == [0.01, 0.2, 0.31], "every document"
    assert bucket["relevant"] == []


def test_an_unlabelled_positive_falls_back_to_its_top_hit_and_says_so():
    """Back-compatible: a calibration set with no `expected` lists still
    calibrates, exactly as it did before — and reports how much of the run was
    scored blind, so nobody reads those numbers as document-level truth."""
    rel = {}
    rc._collect_relevance(
        {"query": "q", "relevant": True, "expected": []},
        [{"document_id": "A", "relevance_score": 0.7, "relevance_method": "lexical"},
         {"document_id": "B", "relevance_score": 0.4, "relevance_method": "lexical"}],
        rel)
    bucket = rel["lexical"]
    assert bucket["relevant"] == [0.7]
    assert bucket["unlabelled_cases"] == 1 and bucket["labelled_cases"] == 0


def test_the_budget_is_a_ceiling_not_an_allowance_to_spend():
    """A scorer that can reach ZERO false positives must not be dragged down to
    the budget just because spending it buys a little recall.

    This is measured, not hypothetical: once the harness could see every hit,
    the cross-encoder's relevant pool grew a low tail, and a budget read as an
    allowance moved the floor from 0.318 (35 guidelines, 0 wrong citations) to
    0.156 (41 guidelines, THREE out-of-corpus queries cited — including the
    bare-vitals query citing ventilator weaning).
    """
    pos = [0.05, 0.10, 0.32, 0.60, 0.90]
    neg = [0.31, 0.20, 0.15] + [0.01] * 17

    assert rc._min_achievable_negatives(pos, neg) == 0
    floor = rc._max_margin_floor(pos, neg)
    assert sum(1 for v in neg if v >= floor) == 0, (
        "the budget was spent on recall the scorer did not need to buy"
    )
    assert sum(1 for v in pos if v >= floor) == 3, (
        "having taken zero false positives, it must still keep every positive "
        "above the negatives — safety first, recall second, not safety only"
    )


def test_the_budget_falls_back_rather_than_muting_an_inseparable_scorer():
    """When NO cut meets the budget, the derivation must still return a usable
    floor (Youden's J) rather than None or a floor above every positive — a
    scorer that cannot separate is a reason to report, not to silently stop
    retrieving."""
    pos = [0.30, 0.40]
    neg = [0.35, 0.45, 0.50, 0.55, 0.60]
    assert rc._budgeted_cut(pos, neg, 0) is None, "precondition: inseparable"
    floor = rc._max_margin_floor(pos, neg)
    assert floor is not None
    assert floor <= max(pos), "a floor above every positive would mute the corpus"


# ─────────────────────────────────────────────────────────────────────────────
# 5. The query says what the patient HAS — the root cause of DEF-4
#
# Everything above tests the retriever. This section tests what the retriever is
# ASKED, which is where the defect actually lived: both lanes built the query
# from the NEWS2 drivers alone, so four unrelated conditions with similar vitals
# sent identical text. See services/retrieval_query.py.
# ─────────────────────────────────────────────────────────────────────────────
def test_the_query_leads_with_the_diagnosis_and_keeps_the_drivers():
    q = rq.build_guideline_query(
        {"diagnosis": "Diabetic ketoacidosis", "problems": ["Type 1 DM"]},
        ["Tachypnea (RR 28)", "Tachycardia (HR 118)"])
    assert q.startswith("Diabetic ketoacidosis"), (
        "the clinical picture must lead — truncation drops from the far end"
    )
    assert "Type 1 DM" in q
    assert "Tachypnea (RR 28)" in q, "the drivers are secondary, not discarded"


def test_two_conditions_with_the_same_vitals_no_longer_send_the_same_query():
    """The defect in one assertion. Identical drivers, different patients."""
    drivers = ["Tachypnea (RR 28)", "Tachycardia (HR 118)", "Low SpO2 (91%)"]
    dka = rq.build_guideline_query({"diagnosis": "Diabetic ketoacidosis"}, drivers)
    uti = rq.build_guideline_query({"diagnosis": "UTI with delirium"}, drivers)
    assert dka != uti
    assert " ".join(drivers) not in (dka, uti), (
        "a query that is only the drivers is the pre-fix query"
    )


def test_a_patient_with_nothing_stated_still_gets_a_query():
    """Degraded, in stated order — never empty, never None. An empty query would
    reach search_guidelines as an empty FTS MATCH."""
    assert rq.build_guideline_query({"diagnosis": "Sepsis"}, None) == "Sepsis"
    assert rq.build_guideline_query({}, ["RR 32", "SpO2 86%"]) == "RR 32, SpO2 86%"
    assert rq.build_guideline_query(None, None) == rq.FALLBACK_QUERY
    assert rq.build_guideline_query({"diagnosis": "", "problems": []}, []) == rq.FALLBACK_QUERY


def test_the_query_carries_no_patient_identity():
    """The demographics line is the one field that reads like a summary of the
    patient, and it is what the prefetch lane used to fall back to. It is
    `PATIENT: <name>, …, Diagnosis: <dx>` — a named human being sent to the
    knowledge child so that one trailing field could match."""
    facts = {
        "demographics": "PATIENT: Ananya Kapoor, 34y/Female, Bed GM-101, "
                        "Ward: GM-1, Diagnosis: Diabetic ketoacidosis",
        "diagnosis": "Diabetic ketoacidosis",
        "problems": [],
    }
    q = rq.build_guideline_query(facts, ["Tachypnea (RR 28)"])
    assert "Ananya" not in q and "Kapoor" not in q
    assert "GM-101" not in q and "Ward" not in q
    assert "Diabetic ketoacidosis" in q


def test_the_query_is_bounded():
    """Every term is one more cross-encoder token and one more OR in the FTS
    lane. A pasted-paragraph diagnosis must not become the query."""
    q = rq.build_guideline_query(
        {"diagnosis": "Sepsis " * 200, "problems": ["CKD " * 200] * 6},
        ["Tachycardia (HR 130) " * 50] * 10)
    assert len(q) <= rq._MAX_QUERY_CHARS


def test_a_problem_that_repeats_the_diagnosis_is_not_said_twice():
    q = rq.build_guideline_query(
        {"diagnosis": "Community acquired pneumonia",
         "problems": ["community acquired pneumonia!", "Type 2 DM"]},
        [])
    assert q.count("ommunity") == 1
    assert "Type 2 DM" in q, "a DIFFERENT problem must survive the dedup"


def test_similar_problems_are_not_collapsed_into_one():
    """Dedup is containment over content tokens INCLUDING digits — "Type 1 DM"
    and "Type 2 DM" are different problems."""
    assert rq.clinical_picture(
        {"diagnosis": "Type 1 DM", "problems": ["Type 2 DM"]}) == ["Type 1 DM", "Type 2 DM"]


# ── and against the real corpus ──────────────────────────────────────────────
# The five conditions from the dry run, as `_assemble_patient_context` hands
# them over (server/scripts/seed_clinical_data.py is where these charts come
# from). `expected_any` is the same corpus truth as CONDITIONS above.
ALERT_CASES = [
    ("DKA", {"diagnosis": "Diabetic ketoacidosis", "problems": ["Type 1 DM"]},
     ["Tachypnea (RR 28)", "Tachycardia (HR 118)"],
     {"EXT-METAB-001"}),
    ("dengue", {"diagnosis": "Dengue fever with thrombocytopenia", "problems": []},
     ["Pyrexia (Temp 39.2)", "Tachycardia (HR 108)"],
     None),
    ("sepsis_cap", {"diagnosis": "CAP with early sepsis",
                    "problems": ["Type 2 DM", "CKD Stage 2"]},
     ["Tachypnea (RR 28)", "Low SpO2 (91%)", "Hypotension (SBP 85)", "Pyrexia (Temp 39.8)"],
     {"EXT-ID-001", "REF-CAP-CURB65-001", "FORM-ID-001"}),
    # BOTH are legitimate for "UTI with delirium", and this pair is the case
    # that made the harness honest. On the first 2026-08-15 calibration the
    # derived floor was 0.341 while EXT-ID-002 (UTI treatment) scores 0.318, so
    # the antibiotic guideline was silently dropped and only the PADIS delirium
    # guideline (0.588) survived — and NOTHING in the calibration metrics moved,
    # because the harness scored the TOP hit per case only. Once it scored every
    # hit, EXT-ID-002 entered the derivation, the floor came down to 0.31541 and
    # both are cited. The margin is REAL BUT THIN: the highest-scoring
    # out-of-corpus query (bare vitals → ventilator weaning) sits at 0.313, so
    # the whole separation lives in [0.313, 0.318]. If a future corpus or model
    # narrows that band further, this is the case that will say so first.
    ("UTI", {"diagnosis": "UTI with delirium",
             "problems": ["Hypertension", "Osteoarthritis"]},
     ["Pyrexia (Temp 38.4)", "Low SpO2 (94%)"],
     {"EXT-ID-002", "EXT-SEDATION-001"}),
    ("COPD", {"diagnosis": "COPD exacerbation",
              "problems": ["COPD GOLD III", "Hypertension"]},
     ["Low SpO2 (88%)", "Tachypnea (RR 26)"],
     None),
]

@pytest.fixture(scope="module")
def cross_encoder_scorer():
    """The two corpus tests below are the only ones in this file that assert a
    SPECIFIC document comes back for an alert-shaped query, and that is the one
    thing the lexical scorer provably cannot do — see `_min_evidence_kept`,
    where the measurement and the deployment consequence are recorded. Asserting
    it anyway would either fail on a lexical-only machine or, if written loosely
    enough to pass there, assert nothing anywhere.

    Probed rather than assumed: `rerank_results` reports which scorer actually
    ran, whether it is in-process or in the knowledge worker.
    """
    if ck._cross_encoder_disabled():
        pytest.skip("cross-encoder disabled (ORB_SAGE_CROSS_ENCODER)")
    probe = ck.rerank_results("sepsis screening", [{
        "document_id": "PROBE", "title": "Sepsis", "trust_tier": 3,
        "content": "sepsis lactate antibiotics screening", "fusion_score": 0.03,
    }], top_k=1)
    if not probe or probe[0].get("rerank_method") != "cross_encoder":
        pytest.skip("no cross-encoder on this machine — lexical relevance cannot "
                    "separate alert-shaped queries, which is measured, not assumed")


@pytest.mark.parametrize("name,facts,drivers,expected_any", ALERT_CASES,
                         ids=[c[0] for c in ALERT_CASES])
def test_an_alert_retrieves_the_patients_own_guideline(cross_encoder_scorer, seeded_corpus,
                                                       name, facts,
                                                       drivers, expected_any):
    """What a clinician gets on a critical alert, end to end.

    Before the query was built from the chart, these five sent vitals-shaped
    text and got the SAME documents. Now each retrieves its own guideline — or,
    where the corpus genuinely has nothing (dengue, COPD), still nothing.
    """
    ids = _evidence(rq.build_guideline_query(facts, drivers))
    if expected_any is None:
        assert ids == [], f"{name} is not in the corpus; citing {ids} is a promise Orb cannot keep"
    else:
        assert set(ids) & expected_any, f"{name} should surface one of {sorted(expected_any)}, got {ids}"


def test_the_drivers_alone_are_what_used_to_fail(cross_encoder_scorer, seeded_corpus):
    """Prove-can-fail, built in. Send the PRE-FIX query — the drivers with no
    clinical picture — for the conditions the corpus covers, and it comes back
    with nothing or with the wrong document. If this ever starts passing, the
    test above has stopped proving that the query is what fixed it."""
    recovered = []
    for name, _facts, drivers, expected_any in ALERT_CASES:
        if expected_any is None:
            continue
        if set(_evidence(" ".join(drivers))) & expected_any:
            recovered.append(name)
    assert not recovered, (
        f"{recovered} retrieved their own guideline from the drivers alone — "
        "the vitals-only query is no longer the defect this fix was for"
    )
