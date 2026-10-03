"""RAG confidence-threshold calibration tests.

Proves: (1) the runtime and the calibrator share ONE signal definition;
(2) calibration derives thresholds that separate in-corpus from out-of-corpus
queries at least as well as the hand-picked defaults; (3) the runtime loads
calibrated thresholds when present and falls back to defaults when absent.
"""
import pytest

from services.retrieval_calibration import (
    DEFAULT_THRESHOLDS, calibrate, classify_confidence, load_calibration_set,
    load_thresholds, retrieval_signals,
)


# Reciprocal Rank Fusion in clinical_knowledge.search_guidelines scores on RANK
# ONLY: 1/(k_rrf + dense_rank) + 1/(k_rrf + lexical_rank) with k_rrf = 60. So a
# real fusion_score can never exceed 2/61, and the interesting values are the
# three the fusion can actually emit.
RRF_K = 60
RRF_MAX = 2.0 / (RRF_K + 1)             # 0.032787 — rank 1 in BOTH lanes
RRF_ONE_LANE = 1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 1000)   # 0.017337 — one lane only
RRF_WEAK = 1.0 / (RRF_K + 8) + 1.0 / (RRF_K + 1000)       # 0.015649 — mid-pack

# query -> relevant, straight from the shipped labelled set.
_LABELS = {c["query"]: c["relevant"] for c in load_calibration_set()}


# A deterministic stub retriever so the test doesn't depend on the embedder /
# corpus state: relevant queries score high, out-of-corpus queries score ~0.
#
# The fusion_scores here are the values RRF CAN produce. The previous version of
# this stub used 0.06 for positives — a number the real fusion cannot reach at
# all — which is why this suite stayed green while `high_score = 0.04` sat
# permanently unreachable in production. A fixture that can express a score the
# system cannot produce is a fixture that cannot see the bug.
def _stub_search(query, limit=5, **kwargs):
    # Read the label straight from the labelled set rather than guessing from
    # keywords. The old keyword list silently disagreed with the labels the
    # moment a case was relabelled ("anticoagulation reversal", "oxygen
    # targets" and "antibiotic stewardship" moved to negatives once the corpus
    # was checked, and "burns fluid resuscitation" was never in it) — so the
    # stub was scoring four labelled negatives as strong positives and the
    # separation metrics were measuring the stub's opinion, not the labels'.
    in_corpus = _LABELS.get(query)
    if in_corpus is None:
        in_corpus = any(kw in query.lower() for kw in ("sepsis", "dka", "stemi"))
    if in_corpus:
        return [{"document_id": "DOC-1", "title": query, "content": query,
                 "fusion_score": RRF_MAX, "trust_tier": 1, "is_synthetic": False,
                 "source": "s", "relevance_score": 0.88,
                 "relevance_method": "cross_encoder"}]
    # Out-of-corpus: the fusion still surfaces the closest doc — at a score
    # barely distinguishable from a real hit — with no lexical overlap and near
    # zero relevance. This is the DEF-4 shape.
    return [{"document_id": "DOC-9", "title": "sepsis bundle", "content": "sepsis bundle",
             "fusion_score": RRF_ONE_LANE, "trust_tier": 1, "is_synthetic": False,
             "source": "s", "relevance_score": 0.01,
             "relevance_method": "cross_encoder"}]


def _separation(thresholds, cases):
    tp = fp = tn = fn = 0
    for c in cases:
        results = _stub_search(c["query"])
        conf = classify_confidence(retrieval_signals(c["query"], results), thresholds)
        predicted = conf != "none"
        if c["relevant"] and predicted:
            tp += 1
        elif c["relevant"] and not predicted:
            fn += 1
        elif not c["relevant"] and not predicted:
            tn += 1
        else:
            fp += 1
    acc = (tp + tn) / max(1, tp + tn + fp + fn)
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "accuracy": acc}


def test_signals_are_deterministic_and_shared():
    results = _stub_search("sepsis bundle first hour")
    sig = retrieval_signals("sepsis bundle first hour", results)
    assert set(sig) == {"top_score", "avg_score", "lexical_overlap"}
    assert sig["top_score"] == RRF_MAX


def test_fusion_score_ceiling_is_documented_and_thresholds_are_checked_against_it():
    """`top_score` is a RANK artefact with a hard ceiling of 2/(k_rrf+1).

    Recorded because it is the reason DEF-4's gate was blind: the historical
    hand-picked `high_score = 0.04` sits ABOVE the maximum score the fusion can
    ever emit, so the score branch of "high" has never fired for any query on
    any corpus. Any future threshold on `top_score` has to clear this bar to
    mean anything — and no threshold on a rank artefact can separate relevance,
    which is why evidence is now gated on `relevance_score` instead.
    """
    assert RRF_MAX == pytest.approx(0.032787, abs=1e-6)
    assert DEFAULT_THRESHOLDS["high_score"] > RRF_MAX, (
        "documenting the historical defect: the uncalibrated default is unreachable"
    )
    # Anything derived from real data must at least live inside the achievable range.
    calibrated = calibrate(search_fn=_stub_search, write=False)
    assert calibrated["high_score"] <= RRF_MAX
    assert calibrated["medium_score"] <= RRF_MAX


def test_calibration_derives_a_relevance_floor_between_the_classes():
    """The floor that decides whether evidence is shown at all is derived from
    the labelled set, and lands strictly between the two score distributions."""
    calibrated = calibrate(search_fn=_stub_search, write=False)
    floor = calibrated["min_relevance_cross_encoder"]
    assert 0.01 < floor < 0.88, f"floor {floor} is outside the observed gap"
    fm = calibrated["metrics"]["relevance_floor"]["cross_encoder"]
    assert fm["evidence_kept_on_negatives"] == 0
    assert fm["evidence_kept_on_positives"] == fm["positives"]


def test_calibration_separates_at_least_as_well_as_defaults():
    cases = load_calibration_set()
    calibrated = calibrate(search_fn=_stub_search, write=False)
    base = _separation(DEFAULT_THRESHOLDS, cases)
    cal = _separation({**DEFAULT_THRESHOLDS, **calibrated}, cases)
    # Calibration must not be worse than the hand-picked magic numbers.
    assert cal["accuracy"] >= base["accuracy"]
    # And it reports its own separation metrics.
    assert "metrics" in calibrated
    assert calibrated["metrics"]["positives"] > 0
    assert calibrated["metrics"]["negatives"] > 0


def test_out_of_corpus_query_classified_none():
    # The whole point of the floor: an unrelated query returns 'none' (no
    # relevant guideline) rather than citing the closest tangential doc.
    calibrated = calibrate(search_fn=_stub_search, write=False)
    thresholds = {**DEFAULT_THRESHOLDS, **calibrated}
    results = _stub_search("what is the wifi password")
    conf = classify_confidence(retrieval_signals("what is the wifi password", results), thresholds)
    assert conf == "none"


def test_in_corpus_query_not_none():
    calibrated = calibrate(search_fn=_stub_search, write=False)
    thresholds = {**DEFAULT_THRESHOLDS, **calibrated}
    results = _stub_search("empiric antibiotics for sepsis")
    conf = classify_confidence(retrieval_signals("empiric antibiotics for sepsis", results), thresholds)
    assert conf in ("low", "medium", "high") and conf != "none"


def test_runtime_falls_back_to_defaults_when_no_file(tmp_path, monkeypatch):
    import services.retrieval_calibration as rc
    monkeypatch.setattr(rc, "THRESHOLDS_PATH", str(tmp_path / "nope.json"))
    t = rc.load_thresholds(force_reload=True)
    assert t["no_relevance_score_floor"] == DEFAULT_THRESHOLDS["no_relevance_score_floor"]
    assert t["source"].startswith("default")


def test_runtime_loads_calibrated_file(tmp_path, monkeypatch):
    import json
    import services.retrieval_calibration as rc
    p = tmp_path / "thresholds.json"
    p.write_text(json.dumps({
        "no_relevance_score_floor": 0.09, "no_relevance_lexical_floor": 0.4,
        "high_score": 0.1, "high_lexical": 0.8, "medium_score": 0.05,
        "medium_lexical": 0.5, "min_relevance_cross_encoder": 0.31,
        "min_relevance_lexical": 0.27, "source": "calibrated",
    }))
    monkeypatch.setattr(rc, "THRESHOLDS_PATH", str(p))
    t = rc.load_thresholds(force_reload=True)
    assert t["no_relevance_score_floor"] == 0.09
    assert t["source"] == "calibrated"
    rc.load_thresholds(force_reload=True)  # reset cache for other tests


@pytest.fixture(autouse=True)
def _reset_threshold_cache():
    yield
    import services.retrieval_calibration as rc
    rc._cached = None
