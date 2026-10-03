"""Sage RAG Retrieval Evaluation Harness.

Runs a fixed set of realistic clinical questions through
``clinical_knowledge.search_guidelines`` and scores retrieval quality with the
standard IR metrics used to catch regressions in CI:

  * recall@k    — fraction of a question's expected documents found in the top-k,
                  averaged over questions,
  * hit-rate@k  — fraction of questions with at least one expected document in
                  the top-k (a.k.a. success@k),
  * MRR         — mean reciprocal rank of the first relevant document.

A question is defined by a query plus the ``expected_source`` document ids it
should surface (and, optionally, ``expected_keywords`` that must appear in a
retrieved chunk). Relevance is decided primarily by document id; when a question
declares no ids, keyword membership is used instead.

The harness is embedder-agnostic: with the BGE-M3 dense embedder installed it
exercises the full hybrid path; without it (the dev venv) retrieval degrades to
the FTS/BM25 lexical path plus the lexical reranker — the harness still runs and
produces metrics. It is pure/read-only over the store, so it is safe to run in CI.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

# Default question bank ships alongside the server at server/eval/.
_DEFAULT_QUESTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "eval",
    "retrieval_questions.json",
)

# k values reported by default (primary k is the largest).
_DEFAULT_KS: Sequence[int] = (1, 3, 5)


def load_questions(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load the evaluation question bank from JSON.

    Accepts either a top-level list, or an object with a ``questions`` list. Each
    question needs a ``query`` and at least one of ``expected_source`` (a doc id or
    list of ids) / ``expected_keywords``.
    """
    p = path or _DEFAULT_QUESTIONS_PATH
    with open(p, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    questions = data["questions"] if isinstance(data, dict) else data
    if not isinstance(questions, list):
        raise ValueError(f"Question bank at {p} must be a list or have a 'questions' list")
    return [_normalize_question(q, i) for i, q in enumerate(questions)]


def _normalize_question(q: Dict[str, Any], idx: int) -> Dict[str, Any]:
    """Coerce a raw question dict into a canonical shape."""
    expected = q.get("expected_source", q.get("expected_doc_ids", []))
    if isinstance(expected, str):
        expected = [expected]
    keywords = q.get("expected_keywords", [])
    if isinstance(keywords, str):
        keywords = [keywords]
    return {
        "id": str(q.get("id", f"Q{idx + 1}")),
        "query": q["query"],
        "expected_source": [str(e) for e in expected],
        "expected_keywords": [str(k) for k in keywords],
        "category": q.get("category"),
        # An "exact-match" question is one whose answer is a specific known doc id;
        # the eval test asserts hit-rate > 0 over these regardless of embedder.
        "exact_match": bool(q.get("exact_match", bool(expected))),
    }


def _is_relevant(result: Dict[str, Any], question: Dict[str, Any]) -> bool:
    """Whether a single retrieved result satisfies the question.

    Primary signal: the result's document_id is one of the expected ids. Fallback
    (only when the question declares no expected ids): every expected keyword
    appears in the retrieved title/content.
    """
    expected_ids = question["expected_source"]
    if expected_ids:
        return result.get("document_id") in expected_ids
    keywords = question["expected_keywords"]
    if not keywords:
        return False
    hay = f"{result.get('title', '')} {result.get('content', '')}".lower()
    return all(kw.lower() in hay for kw in keywords)


def _expected_count(question: Dict[str, Any]) -> int:
    """Denominator for recall — number of distinct target documents."""
    return max(1, len(set(question["expected_source"]))) if question["expected_source"] else 1


def evaluate_question(
    question: Dict[str, Any],
    search_fn: Callable[..., List[Dict[str, Any]]],
    k: int,
    approved_only: bool = False,
    rerank: bool = True,
) -> Dict[str, Any]:
    """Run one question through retrieval and compute its per-question signals."""
    results = search_fn(question["query"], limit=k, rerank=rerank, approved_only=approved_only)
    retrieved_ids = [r.get("document_id") for r in results]

    # Rank (1-based) of the first relevant result, else None.
    first_rel_rank: Optional[int] = None
    relevant_flags: List[bool] = []
    for rank, r in enumerate(results, start=1):
        rel = _is_relevant(r, question)
        relevant_flags.append(rel)
        if rel and first_rel_rank is None:
            first_rel_rank = rank

    return {
        "id": question["id"],
        "query": question["query"],
        "expected_source": question["expected_source"],
        "retrieved": retrieved_ids,
        "relevant_flags": relevant_flags,
        "first_relevant_rank": first_rel_rank,
        "expected_count": _expected_count(question),
        "exact_match": question["exact_match"],
    }


def _recall_at_k(per_q: List[Dict[str, Any]], k: int) -> float:
    """Mean over questions of (relevant found in top-k / expected count)."""
    if not per_q:
        return 0.0
    total = 0.0
    for q in per_q:
        found = sum(1 for i, rel in enumerate(q["relevant_flags"][:k]) if rel)
        # Cap at expected_count so recall stays in [0, 1] even with dup ids.
        total += min(found, q["expected_count"]) / q["expected_count"]
    return total / len(per_q)


def _hit_rate_at_k(per_q: List[Dict[str, Any]], k: int) -> float:
    """Fraction of questions with >= 1 relevant result in the top-k."""
    if not per_q:
        return 0.0
    hits = sum(1 for q in per_q if any(q["relevant_flags"][:k]))
    return hits / len(per_q)


def _mrr(per_q: List[Dict[str, Any]]) -> float:
    """Mean reciprocal rank of the first relevant result (0 if none found)."""
    if not per_q:
        return 0.0
    total = 0.0
    for q in per_q:
        r = q["first_relevant_rank"]
        total += (1.0 / r) if r else 0.0
    return total / len(per_q)


def run_eval(
    questions: Optional[List[Dict[str, Any]]] = None,
    k: int = 5,
    ks: Optional[Sequence[int]] = None,
    search_fn: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    approved_only: bool = False,
    rerank: bool = True,
    questions_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate retrieval over the question bank and return an aggregate metrics dict.

    Args:
        questions: pre-loaded questions; if None they are loaded from
            ``questions_path`` (or the default bank).
        k: primary retrieval depth (results requested per query, and the largest k
            reported).
        ks: additional cut-offs to report recall/hit-rate at (defaults to 1/3/5,
            filtered to <= k).
        search_fn: retrieval callable; defaults to
            ``clinical_knowledge.search_guidelines``. Must accept
            ``(query, limit=, rerank=, approved_only=)``.
        approved_only / rerank: forwarded to the search function.

    Returns a dict with ``num_questions``, ``recall@{k}``, ``hit_rate@{k}``,
    ``mrr``, ``hit_rate`` (at primary k), and ``per_question`` details.
    """
    if search_fn is None:
        from services.clinical_knowledge import search_guidelines
        search_fn = search_guidelines
    if questions is None:
        questions = load_questions(questions_path)

    report_ks = sorted({kk for kk in (ks or _DEFAULT_KS) if kk <= k} | {k})

    per_q = [
        evaluate_question(q, search_fn, k=k, approved_only=approved_only, rerank=rerank)
        for q in questions
    ]

    metrics: Dict[str, Any] = {
        "num_questions": len(per_q),
        "k": k,
        "ks": report_ks,
        "mrr": round(_mrr(per_q), 4),
        "hit_rate": round(_hit_rate_at_k(per_q, k), 4),
        "rerank": rerank,
        "approved_only": approved_only,
    }
    for kk in report_ks:
        metrics[f"recall@{kk}"] = round(_recall_at_k(per_q, kk), 4)
        metrics[f"hit_rate@{kk}"] = round(_hit_rate_at_k(per_q, kk), 4)

    # Hit-rate restricted to the exact-match questions (the CI floor: these must be
    # retrievable even on the lexical-only path).
    exact = [q for q in per_q if q["exact_match"]]
    metrics["num_exact_match"] = len(exact)
    metrics["exact_match_hit_rate"] = round(_hit_rate_at_k(exact, k), 4) if exact else 0.0

    metrics["per_question"] = [
        {
            "id": q["id"],
            "query": q["query"],
            "expected_source": q["expected_source"],
            "retrieved": q["retrieved"][:k],
            "first_relevant_rank": q["first_relevant_rank"],
            "hit": bool(q["first_relevant_rank"]),
        }
        for q in per_q
    ]
    return metrics
