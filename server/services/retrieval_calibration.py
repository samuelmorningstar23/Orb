"""Orb — RAG threshold calibration.

Two runtime decisions used to be made with hand-picked numbers, and this module
derives both from a labelled set instead:

  * WHETHER A GUIDELINE IS SHOWN AT ALL — `min_relevance_cross_encoder` /
    `min_relevance_lexical`, the floors clinical_knowledge.apply_relevance_floor
    uses to decide that NOTHING in the corpus is relevant. This is the gate that
    lets Sage say "no guideline in Orb's corpus matched" instead of citing the
    closest thing it found (DEF-4: ventilator weaning cited on a dengue draft).
  * HOW CONFIDENT THE ANSWER IS — the no-relevance/high/medium cutoffs
    `sage_service._get_guideline_context` applies to the fusion signals, which
    its own comment admitted were "not tuned against a held-out set".

How:
  1. A labelled calibration set (server/eval/retrieval_calibration.json) of
     in-corpus queries (should retrieve a relevant guideline) and out-of-corpus
     queries (should return NO relevant guideline) — including CLINICAL
     negatives, which are the hard case, not just administrative ones.
  2. calibrate() runs each query through the SAME retrieval and the SAME signal
     computation the runtime uses, with the relevance floor disabled so it
     observes the raw distribution, then derives:
       * relevance floors at the maximum-margin point inside a false-positive
         BUDGET — the most recall the scorer can have without citing more than
         a stated share of the out-of-corpus queries, per scorer (see
         _max_margin_floor; the budget is why this is not Youden's J, which
         weighs a wrong citation as lightly as a withheld one),
       * no-relevance floors maximising Youden's J on "is this query answerable
         from the corpus?",
       * high/medium confidence cutoffs from the positives' distribution.
  3. It writes server/eval/retrieval_thresholds.json with its metrics and
     provenance. The runtime loads it via load_thresholds(), falling back to the
     documented defaults when absent.

The shared signal math lives in retrieval_signals() so the runtime and the
calibrator can never drift apart. Calibration reflects whatever retrieval path
is active, so re-run it after changing the corpus or the models — once per
relevance scorer:

    python scripts/calibrate_retrieval.py --load-corpus --no-cross-encoder
    python scripts/calibrate_retrieval.py --load-corpus --skip-init

Each run derives the floor for the scorer it exercised and carries the other
forward untouched.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypedDict

logger = logging.getLogger("orb.retrieval_calibration")

_EVAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval")
CALIBRATION_SET_PATH = os.path.join(_EVAL_DIR, "retrieval_calibration.json")
THRESHOLDS_PATH = os.path.join(_EVAL_DIR, "retrieval_thresholds.json")


# The documented fallback (the historical magic numbers). Used verbatim when no
# calibration file has been committed, so runtime behaviour is unchanged until a
# calibration exists.
class _Thresholds(TypedDict):
    no_relevance_score_floor: float
    no_relevance_lexical_floor: float
    high_score: float
    high_lexical: float
    medium_score: float
    medium_lexical: float
    min_relevance_cross_encoder: float
    min_relevance_lexical: float
    source: str


DEFAULT_THRESHOLDS: _Thresholds = {
    "no_relevance_score_floor": 0.02,
    "no_relevance_lexical_floor": 0.15,
    "high_score": 0.04,
    "high_lexical": 0.5,
    "medium_score": 0.025,
    "medium_lexical": 0.3,
    # Per-method relevance floors consumed by
    # clinical_knowledge.apply_relevance_floor — the gate that lets retrieval
    # return NOTHING rather than cite a tangential guideline. Two keys because
    # the two scorers live on different scales: the cross-encoder emits
    # P(relevant | query, passage), the lexical scorer emits weighted term
    # coverage. See the RELEVANCE FLOOR block in clinical_knowledge.py for why
    # each fallback is what it is.
    "min_relevance_cross_encoder": 0.10,
    "min_relevance_lexical": 0.10,
    "source": "default (uncalibrated)",
}

# ── A NOTE ON `high_score`, recorded 2026-08-14 ──────────────────────────────
# The RRF fusion in search_guidelines scores on RANK ONLY:
#     rrf = 1/(60 + dense_rank) + 1/(60 + lexical_rank)
# so its maximum possible value, for a document ranked first in BOTH lanes, is
# 2/61 = 0.032787. The historical hand-picked `high_score` of 0.04 is therefore
# UNREACHABLE — the score branch of "high" has never fired for any query on any
# corpus, and "high" has only ever come from `lexical_overlap > high_lexical`.
# It is also why `top_score` cannot separate relevance: measured on the seeded
# corpus, "diabetic ketoacidosis insulin infusion" scored 0.032787 and "where is
# the hospital parking garage and how much does it cost?" scored 0.032522.
# That is the whole argument for gating evidence on `relevance_score` (a real
# query↔passage measurement) instead of on `top_score` (a rank artefact).


def retrieval_signals(query: str, results: list) -> dict:
    """The confidence signals for a query + its retrieved results. SHARED by the
    runtime gate and the calibrator so they can never diverge."""
    scores = [r.get("fusion_score", 0) or 0 for r in results]
    top_score = scores[0] if scores else 0.0
    avg_score = (sum(scores) / len(scores)) if scores else 0.0
    query_words = set(w.lower() for w in query.split() if len(w) > 3)
    top = results[0] if results else {}
    top_content = ((top.get("content", "") or "") + " " + (top.get("title", "") or "")).lower()
    lexical_overlap = (
        (sum(1 for w in query_words if w in top_content) / max(len(query_words), 1))
        if query_words
        else 0.0
    )
    return {"top_score": top_score, "avg_score": avg_score, "lexical_overlap": lexical_overlap}


def classify_confidence(signals: dict, thresholds: dict) -> str:
    """Map signals → 'none' | 'low' | 'medium' | 'high' using thresholds.
    'none' means the no-relevance floor tripped (treat as irrelevant)."""
    ts, lex = signals["top_score"], signals["lexical_overlap"]
    if (
        ts < thresholds["no_relevance_score_floor"]
        and lex < thresholds["no_relevance_lexical_floor"]
    ):
        return "none"
    if ts > thresholds["high_score"] or lex > thresholds["high_lexical"]:
        return "high"
    if ts > thresholds["medium_score"] or lex > thresholds["medium_lexical"]:
        return "medium"
    return "low"


_cached: dict | None = None


def load_thresholds(force_reload: bool = False) -> dict:
    """Load calibrated thresholds from disk, or the documented defaults."""
    global _cached
    if _cached is not None and not force_reload:
        return _cached
    try:
        if os.path.exists(THRESHOLDS_PATH):
            with open(THRESHOLDS_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            merged = {**DEFAULT_THRESHOLDS, **{k: data[k] for k in DEFAULT_THRESHOLDS if k in data}}
            merged["source"] = data.get("source", "calibrated")
            _cached = merged
            return _cached
    except Exception as e:
        logger.warning("Failed to load calibrated thresholds (%s); using defaults", e)
    _cached = dict(DEFAULT_THRESHOLDS)
    return _cached


def _read_thresholds_file() -> dict:
    """Raw contents of the committed thresholds file, or {} if there is none."""
    try:
        if os.path.exists(THRESHOLDS_PATH):
            with open(THRESHOLDS_PATH, encoding="utf-8") as fh:
                return json.load(fh) or {}
    except Exception as e:
        logger.warning("Could not read %s (%s)", THRESHOLDS_PATH, e)
    return {}


def _corpus_shape() -> dict:
    """Chunk/document counts of the store the calibration ran against."""
    try:
        from database import db_session

        with db_session() as db:
            return {
                "chunks": db.execute("SELECT COUNT(*) FROM clinical_knowledge").fetchone()[0],
                "documents": db.execute(
                    "SELECT COUNT(DISTINCT document_id) FROM clinical_knowledge"
                ).fetchone()[0],
            }
    except Exception:
        return {}


def _search_unfiltered(search_fn: Callable, query: str, limit: int = 5) -> list:
    """Retrieve WITHOUT the relevance floor applied.

    The calibrator has to see the score distribution the floor is derived from;
    if it ran through the floor it would be measuring its own previous output
    and would converge on whatever it already believed. `min_relevance=0.0`
    disables the floor while still annotating `relevance_score`. Custom
    search_fns in tests/evals need not accept the kwarg — they are called
    plainly and simply contribute no relevance samples.
    """
    try:
        return search_fn(query, limit=limit, min_relevance=0.0)
    except TypeError:
        return search_fn(query, limit=limit)


def load_calibration_set(path: str | None = None) -> list:
    p = path or CALIBRATION_SET_PATH
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data["cases"] if isinstance(data, dict) else data
    out = []
    for it in items:
        out.append(
            {
                "query": it["query"],
                # relevant = the corpus SHOULD answer this (in-corpus positive).
                "relevant": bool(it.get("relevant", True)),
                # The document_ids that legitimately answer this query — OPTIONAL,
                # and only meaningful on a positive. It is what lets the calibrator
                # score every hit rather than only the top one: without it, a
                # relevant guideline at rank 2 is indistinguishable from an
                # off-topic one and cannot be counted either way. Negatives need no
                # labels — by definition NO document answers them, so every
                # candidate they return is a false positive.
                "expected": [str(d) for d in (it.get("expected") or [])],
            }
        )
    return out


def _best_floor(pos_vals: list, neg_vals: list) -> float:
    """Pick a floor that best separates negatives (below) from positives (above),
    maximising (true-negatives-below + true-positives-above) — Youden's J on a
    single threshold. Candidate cut points are the observed values."""
    candidates = sorted(set(pos_vals + neg_vals))
    if not candidates:
        return 0.0
    best_j, best_t = -1.0, candidates[0]
    for t in candidates:
        # A query is classified "irrelevant" if its value < t.
        tn = sum(1 for v in neg_vals if v < t)  # correctly below
        tp = sum(1 for v in pos_vals if v >= t)  # correctly above
        tnr = tn / len(neg_vals) if neg_vals else 0.0
        tpr = tp / len(pos_vals) if pos_vals else 0.0
        j = tnr + tpr - 1.0
        if j > best_j:
            best_j, best_t = j, t
    return best_t


# The MOST labelled negatives the floor may ever let through, as a share of
# them. A CEILING OF LAST RESORT, not an allowance to spend — see
# `_min_achievable_negatives`, which is what normally binds. It is not a tuning
# knob; it is the statement that this floor is a SAFETY device with an
# asymmetric cost. A negative that survives is a guideline cited to a clinician
# for a condition it does not cover. A positive that does not survive is "No
# guideline in Orb's corpus matched this alert" — a sentence the product already
# knows how to say (`evidence_note`), on a draft that still carries Tier 1 and
# the model's reasoning over the chart.
_MAX_NEGATIVES_KEPT_FRACTION = 0.10


def _min_achievable_negatives(pos_vals: list, neg_vals: list) -> int:
    """The fewest negatives ANY usable cut admits.

    This, not the budget, is what normally decides the floor. Spelling out why:
    `_budgeted_cut` returns the most permissive cut inside a budget, so a budget
    read as an allowance gets SPENT whenever spending it buys any recall at all.
    Measured on this corpus after the harness learned to score every hit
    (2026-08-15): the cross-encoder reaches ZERO false positives at 0.31541 and
    still cites 36 of 43 relevant guidelines, but the 10% budget read as an
    allowance dropped it to 0.156 — 41 guidelines, and THREE out-of-corpus
    queries cited, among them the bare-vitals query citing ventilator weaning,
    which is DEF-4 itself. Five more guidelines does not buy that.

    So the rule is lexicographic: fewest wrong citations first, most evidence
    second. The budget survives only as the ceiling for a scorer that cannot
    reach its minimum inside it — where `_budgeted_cut` returns None and the
    caller falls back to Youden's J.

    Every candidate cut is itself a positive value, so each keeps at least one
    piece of evidence and the corpus can never be muted outright. Written as a
    scan rather than `max(pos_vals)` (which is where the minimum always lands,
    since false positives fall monotonically as the cut rises) so it stays
    correct if the candidate set ever stops being the positives.
    """
    if not pos_vals:
        return len(neg_vals)
    return min(sum(1 for v in neg_vals if v >= cut) for cut in set(pos_vals))


def _budgeted_cut(pos_vals: list, neg_vals: list, max_negatives: int) -> float | None:
    """The most permissive cut that still keeps at most `max_negatives`.

    Candidates are the observed POSITIVE values — a floor placed anywhere else
    keeps exactly the same positives as the next positive above it while
    admitting more negatives, so it is never the better choice. Ascending, so
    the first qualifying cut is the one that keeps the most evidence.

    Returns None when no cut meets the budget (the classes are inseparable at
    that budget), and the caller falls back to Youden's J.
    """
    for cut in sorted(set(pos_vals)):
        if sum(1 for v in neg_vals if v >= cut) <= max_negatives:
            return float(cut)
    return None


def _max_margin_floor(
    pos_vals: list, neg_vals: list, max_negatives: int | None = None
) -> float | None:
    """A cut point between the negatives and the positives, placed for MARGIN.

    Two steps, and they answer two different questions:

      1. WHICH gap to cut in. Fewest wrong citations first
         (`_min_achievable_negatives`), most evidence second (`_budgeted_cut`),
         with `_MAX_NEGATIVES_KEPT_FRACTION` as the ceiling for a scorer that
         cannot reach its minimum inside it — and Youden's J only when even
         that is unreachable.
      2. WHERE IN that gap to sit — the geometric midpoint of the empty interval
         below the cut. On its own the cut is an OBSERVED value, so it lands
         exactly ON the weakest surviving positive and leaves zero headroom
         beneath it: one slightly weaker true positive next month and the floor
         starts eating real evidence. Widening to the middle of the gap costs
         nothing on the labelled set — nothing is observed in there, so the
         budget is preserved exactly — and buys symmetric margin.

    WHY NOT YOUDEN'S J ALONE (changed 2026-08-15). It maximises TPR + TNR, which
    weighs a wrong citation exactly as heavily as a withheld one, and on the
    ALERT-SHAPED half of the calibration set — the queries the drafted-assessment
    lane actually sends since services/retrieval_query.py — those two are nowhere
    near equal in cost. Measured on the lexical scorer with the 62-case set:
    Youden picked 0.1727 (J = 0.594; 30/30 positives kept, 13/32 NEGATIVES kept)
    over 0.3256 (J = 0.571; 19/30 positives, 2/32 negatives). A 0.023 difference
    in J was buying eleven irrelevant guidelines cited to a clinician, which is
    DEF-4 rebuilt out of the calibration harness. The cross-encoder separates
    cleanly enough that both rules agree on it (0.34067 either way) — this only
    ever binds where the scorer is weak, which is exactly where it should.

    Geometric, not arithmetic, because relevance scores span orders of magnitude
    (measured on this corpus: negatives 1.7e-05 … 9.1e-02, positives 5.5e-01 …
    1.0) and an arithmetic midpoint of such a gap sits absurdly close to the
    positives.

    Returns None when there is nothing to derive from (either class empty), so
    the caller keeps the documented fallback rather than inventing a number.
    """
    if not pos_vals or not neg_vals:
        return None
    if max_negatives is None:
        # The ceiling, and then the better of it and what this scorer can
        # actually achieve. On a scorer that separates, the second term is 0.
        max_negatives = min(
            int(len(neg_vals) * _MAX_NEGATIVES_KEPT_FRACTION),
            _min_achievable_negatives(pos_vals, neg_vals),
        )
    cut = _budgeted_cut(pos_vals, neg_vals, max_negatives)
    if cut is None:
        cut = float(_best_floor(pos_vals, neg_vals))
    values = sorted(set(pos_vals + neg_vals))
    # The empty interval containing `cut`: highest observed value strictly
    # below it, and the cut itself (the lowest value at or above it).
    below = [v for v in values if v < cut]
    if not below:
        return cut
    eps = 1e-6
    return float((max(max(below), eps) * max(cut, eps)) ** 0.5)


def _round_down(value: float, places: int = 5) -> float:
    """Round a derived threshold DOWN, never up.

    Every threshold here is compared with `>` or `>=` against a score, and most
    are derived AS an observed score. Rounding one up by a hair moves it past
    the very value it was derived from — which is how `high_score` ended up at
    0.04 while the fusion's arithmetic maximum is 0.032787, a band that could
    never fire. Rounding down can only ever be permissive by <1e-5.
    """
    scale = 10**places
    return math.floor(float(value) * scale) / scale


def _percentile(vals: list, p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[idx]


def _new_bucket() -> dict:
    return {
        "relevant": [],
        "irrelevant": [],
        "irrelevant_all": [],
        "positive_case_top": [],
        "labelled_cases": 0,
        "unlabelled_cases": 0,
    }


def _collect_relevance(case: dict, results: list, rel: dict) -> None:
    """Fold one case's candidates into the per-scorer score pools.

    EVERY HIT, NOT `results[0]` (changed 2026-08-15). Sampling the top candidate
    only was wrong in two ways, and the second one shipped a worse floor:

      1. RANK IS NOT RELEVANCE — this module's own invariant, pinned by
         tests/test_evidence_relevance.test_zero_overlap_scores_zero_relevance_
         but_nonzero_rank. On the lexical path the ORDER comes from
         `_lexical_rerank_score` (which carries a trust and fusion-prior floor)
         while the relevance decision comes from `_lexical_relevance_score`
         (pure overlap). So `results[0]` is not reliably the highest-relevance
         candidate, and the harness was sampling the wrong number.
      2. A RELEVANT GUIDELINE AT RANK 2 WAS INVISIBLE. Measured on this corpus:
         "How do I run the sepsis six bundle in the first hour?" ranks the
         Sepsis 1-Hour Bundle SECOND (0.890); "Hyperkalemia emergency treatment
         steps" ranks the Potassium Correction Protocol second (0.517). The
         harness saw neither, so a floor that dropped one changed no number it
         reported — which is exactly how the 2026-08-15 recalibration silently
         cost the UTI alert its antibiotic guideline (0.318 against a 0.341
         floor). That was found by a test, not by this harness. Now it is a
         number the harness prints.

    The pools, and the different questions they answer:

      relevant          every candidate KNOWN to answer its query — the ids in
                        the case's `expected`. This is the pool the floor is
                        derived from, so a rank-2 relevant document can now
                        pull the cut down to keep it.
      irrelevant        one value per NEGATIVE case: the best score anything
                        scored for it. "Does this out-of-corpus query get
                        cited at all" is the safety question, and it is a
                        question about cases, not documents — three bogus
                        citations on one draft mislead one clinician; one
                        citation on three drafts misleads three.
      irrelevant_all    every candidate from a negative case, for counting
                        wrong citations rather than misled clinicians.
      positive_case_top the same best-score-per-case for positives, which is
                        what "this case still gets evidence" means.

    Candidates from a positive case that are NOT in `expected` are IGNORED, not
    counted as irrelevant. They are genuinely unlabelled: a query about septic
    shock returning the sepsis bundle may or may not be a legitimate answer, and
    guessing wrong there would push the floor up on made-up evidence. Negatives
    carry no such doubt, which is why they contribute every hit.

    A positive with no `expected` list falls back to its top score, exactly as
    before, so an unlabelled set still calibrates (and `unlabelled_cases` says
    how much of the run was flying blind).
    """
    expected = set(case.get("expected") or ())
    # PER DOCUMENT, not per row. `search_guidelines` returns CHUNKS, and one
    # guideline can occupy several of them at wildly different scores — the
    # sepsis-six query retrieves REF-SEPSIS-NICE-001 at both 0.959 and 0.006.
    # A guideline is cited when ANY of its chunks clears the floor, so the
    # document's best chunk is its score. Pooling chunks instead put a relevant
    # document's WORST chunk into the relevant distribution, which dragged the
    # derived cross-encoder floor from 0.341 to 0.104 and let the bare-vitals
    # query cite ventilator weaning again — DEF-4, rebuilt out of a unit error.
    by_method: dict = {}
    for cand in results:
        score = cand.get("relevance_score")
        if score is None:
            continue
        method = cand.get("relevance_method") or "lexical"
        docs = by_method.setdefault(method, {})
        doc_id = str(cand.get("document_id") or "")
        docs[doc_id] = max(docs.get(doc_id, 0.0), float(score))

    for method, docs in by_method.items():
        bucket = rel.setdefault(method, _new_bucket())
        best = max(docs.values())
        if not case["relevant"]:
            bucket["irrelevant"].append(best)
            bucket["irrelevant_all"].extend(docs.values())
            continue
        bucket["positive_case_top"].append(best)
        if expected:
            bucket["labelled_cases"] += 1
            bucket["relevant"].extend(s for doc, s in docs.items() if doc in expected)
        else:
            bucket["unlabelled_cases"] += 1
            bucket["relevant"].append(best)


def calibrate(
    search_fn: Callable | None = None, write: bool = True, calibration_path: str | None = None
) -> dict:
    """Run the labelled calibration set through retrieval, derive data-driven
    thresholds, and (optionally) write them to THRESHOLDS_PATH. Returns the
    threshold dict plus a `metrics` block describing the separation achieved."""
    if search_fn is None:
        from services.clinical_knowledge import search_guidelines

        search_fn = search_guidelines

    cases = load_calibration_set(calibration_path)
    pos_top, pos_lex, neg_top, neg_lex = [], [], [], []
    # Relevance scores bucketed by the scorer that produced them — see
    # _collect_relevance for what each list holds and why.
    rel: dict = {}
    per_case = []
    for c in cases:
        results = _search_unfiltered(search_fn, c["query"])
        per_case.append(results)
        sig = retrieval_signals(c["query"], results)
        if c["relevant"]:
            pos_top.append(sig["top_score"])
            pos_lex.append(sig["lexical_overlap"])
        else:
            neg_top.append(sig["top_score"])
            neg_lex.append(sig["lexical_overlap"])
        _collect_relevance(c, results, rel)

    # No-relevance floors: the separating cut between negatives and positives.
    floor_score = (
        _best_floor(pos_top, neg_top)
        if (pos_top and neg_top)
        else DEFAULT_THRESHOLDS["no_relevance_score_floor"]
    )
    floor_lex = (
        _best_floor(pos_lex, neg_lex)
        if (pos_lex and neg_lex)
        else DEFAULT_THRESHOLDS["no_relevance_lexical_floor"]
    )

    # High/medium cutoffs from the positive distribution (upper third = high,
    # lower third boundary = medium). Falls back to defaults if too few positives.
    if len(pos_top) >= 4:
        high_score = _percentile(pos_top, 66)
        medium_score = _percentile(pos_top, 33)
        high_lex = _percentile(pos_lex, 66)
        medium_lex = _percentile(pos_lex, 33)
    else:
        high_score, medium_score = (
            DEFAULT_THRESHOLDS["high_score"],
            DEFAULT_THRESHOLDS["medium_score"],
        )
        high_lex, medium_lex = (
            DEFAULT_THRESHOLDS["high_lexical"],
            DEFAULT_THRESHOLDS["medium_lexical"],
        )

    # Carry forward any previously-calibrated values so a run that only exercises
    # ONE relevance scorer (e.g. this appliance has the cross-encoder installed)
    # does not clobber the floor derived for the other on an earlier run.
    previous = _read_thresholds_file()
    thresholds: dict[str, Any] = {
        "no_relevance_score_floor": _round_down(floor_score),
        "no_relevance_lexical_floor": _round_down(floor_lex),
        "high_score": _round_down(max(high_score, medium_score)),
        "high_lexical": _round_down(max(high_lex, medium_lex)),
        "medium_score": _round_down(medium_score),
        "medium_lexical": _round_down(medium_lex),
        "source": "calibrated",
    }
    for key, method in (
        ("min_relevance_cross_encoder", "cross_encoder"),
        ("min_relevance_lexical", "lexical"),
    ):
        bucket = rel.get(method)
        # Relevant DOCUMENTS against irrelevant CASES, deliberately mixed
        # granularity: the cut may land on any relevant document's score (so a
        # rank-2 guideline can pull it down), while the false-positive budget
        # counts out-of-corpus QUERIES that get cited at all. Read as one
        # sentence: "keep as many relevant documents as possible without more
        # than N out-of-corpus queries citing anything."
        derived = _max_margin_floor(bucket["relevant"], bucket["irrelevant"]) if bucket else None
        if derived is not None:
            thresholds[key] = _round_down(derived)
        elif key in previous:
            thresholds[key] = previous[key]

    # Report the no-relevance separation quality on the labelled set.
    merged = {**DEFAULT_THRESHOLDS, **thresholds}
    tp = fp = tn = fn = 0
    for c, results in zip(cases, per_case, strict=False):
        conf = classify_confidence(retrieval_signals(c["query"], results), merged)
        predicted_relevant = conf != "none"
        if c["relevant"] and predicted_relevant:
            tp += 1
        elif c["relevant"] and not predicted_relevant:
            fn += 1
        elif (not c["relevant"]) and (not predicted_relevant):
            tn += 1
        else:
            fp += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    thresholds["metrics"] = {
        "positives": len(pos_top),
        "negatives": len(neg_top),
        "relevance_precision": round(precision, 4),
        "relevance_recall": round(recall, 4),
        "true_negatives": tn,
        "false_positives": fp,
    }

    # The metric that actually decides whether a clinician sees a bogus
    # citation: does the DERIVED relevance floor keep evidence for in-corpus
    # queries and withhold it for out-of-corpus ones? Measured on the same
    # labelled set, per scorer, with the floors this run produced.
    floor_metrics = {}
    for key, method in (
        ("min_relevance_cross_encoder", "cross_encoder"),
        ("min_relevance_lexical", "lexical"),
    ):
        bucket = rel.get(method)
        if not bucket:
            continue
        floor = merged.get(key, dict(DEFAULT_THRESHOLDS)[key])
        pos_cases = bucket["positive_case_top"]
        floor_metrics[method] = {
            "floor": floor,
            "positives": len(pos_cases),
            "negatives": len(bucket["irrelevant"]),
            # ── CASE level: is this clinician shown evidence at all? ──
            # Same meaning these two keys have always had, so a floor's history
            # in an older thresholds file still reads against these.
            "evidence_kept_on_positives": sum(1 for v in pos_cases if v >= floor),
            "evidence_kept_on_negatives": sum(1 for v in bucket["irrelevant"] if v >= floor),
            # ── DOCUMENT level: WHICH guidelines, of the ones that exist ──
            # The blind spot this harness had until 2026-08-15. A floor that
            # keeps every case's top hit and silently drops its second, equally
            # relevant one moves none of the numbers above and both of these.
            "relevant_documents": len(bucket["relevant"]),
            "relevant_documents_cited": sum(1 for v in bucket["relevant"] if v >= floor),
            "irrelevant_documents_cited": sum(1 for v in bucket["irrelevant_all"] if v >= floor),
            "labelled_cases": bucket["labelled_cases"],
            # Positives with no `expected` list contribute their top score only
            # — the pre-2026-08-15 behaviour — so this is how much of the run
            # is still blind to rank 2.
            "unlabelled_cases": bucket["unlabelled_cases"],
            "max_negative_score": (
                round(max(bucket["irrelevant"]), 6) if bucket["irrelevant"] else None
            ),
            "min_positive_score": (
                round(min(bucket["relevant"]), 6) if bucket["relevant"] else None
            ),
        }
    # Keep the other scorer's evidence from the run that derived it, so the
    # committed file documents BOTH floors rather than only the last one run.
    prior_floor_metrics = (previous.get("metrics") or {}).get("relevance_floor") or {}
    for method, fm in prior_floor_metrics.items():
        floor_metrics.setdefault(method, fm)
    if floor_metrics:
        thresholds["metrics"]["relevance_floor"] = floor_metrics

    # Provenance. A thresholds file with no record of what produced it is a new
    # set of magic numbers with extra steps — the reader has to be able to tell
    # which corpus and which scorer these were derived against, and when.
    provenance = dict(previous.get("_provenance") or {})
    provenance.update(
        {
            "calibrated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "calibration_set": os.path.basename(calibration_path or CALIBRATION_SET_PATH),
            "cases": len(cases),
            "corpus": _corpus_shape(),
        }
    )
    for method in rel:
        provenance.setdefault("scorers", {})[method] = provenance.get("calibrated_at")
    thresholds["_provenance"] = provenance

    if write:
        try:
            os.makedirs(_EVAL_DIR, exist_ok=True)
            with open(THRESHOLDS_PATH, "w", encoding="utf-8") as fh:
                json.dump(thresholds, fh, indent=2)
            load_thresholds(force_reload=True)
        except Exception as e:
            logger.warning("Failed to write calibrated thresholds: %s", e)
    return thresholds
