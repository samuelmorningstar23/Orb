#!/usr/bin/env python
"""CLI for the Sage RAG retrieval evaluation harness.

Runs the clinical question bank (server/eval/retrieval_questions.json) through
``clinical_knowledge.search_guidelines`` and prints recall@k / hit-rate@k / MRR.
This is the harness intended to run in CI to catch retrieval-quality regressions.

Usage:
    ORB_DEPLOYMENT_PROFILE=dev python scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --k 5 --questions eval/retrieval_questions.json
    python scripts/eval_retrieval.py --load-corpus corpus --no-rerank --verbose

Exit code is non-zero when exact-match hit-rate falls below --min-hit-rate, so CI
can gate on it.
"""
import argparse
import json
import os
import sys

# Make the server package importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fmt_pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="Sage RAG retrieval eval harness")
    parser.add_argument("--questions", default=None, help="Path to the question bank JSON")
    parser.add_argument("--k", type=int, default=5, help="Retrieval depth (top-k)")
    parser.add_argument("--no-rerank", action="store_true", help="Disable second-stage reranking")
    parser.add_argument("--approved-only", action="store_true", help="Restrict to approved documents")
    parser.add_argument("--load-corpus", default=None,
                        help="Optional corpus directory to load before evaluating (e.g. 'corpus')")
    parser.add_argument("--skip-init", action="store_true",
                        help="Do not (re)build the seeded knowledge base first")
    parser.add_argument("--min-hit-rate", type=float, default=0.0,
                        help="Fail (non-zero exit) if exact-match hit-rate is below this")
    parser.add_argument("--json", action="store_true", help="Emit metrics as JSON")
    parser.add_argument("--verbose", action="store_true", help="Print per-question detail")
    args = parser.parse_args()

    # Build the seeded corpus so there is something to retrieve against.
    if not args.skip_init:
        try:
            from services.clinical_knowledge import init_knowledge_base
            init_knowledge_base()
        except Exception as e:
            print(f"! Knowledge base init failed: {e}", file=sys.stderr)

    if args.load_corpus:
        from services import corpus_loader
        summary = corpus_loader.load_directory(args.load_corpus, default_trust_tier=3)
        print(f"Loaded corpus '{args.load_corpus}': {summary['files_loaded']} file(s), "
              f"{summary['chunks_loaded']} chunk(s), mode={summary['mode']}")

    from services.retrieval_eval import run_eval

    metrics = run_eval(
        k=args.k,
        rerank=not args.no_rerank,
        approved_only=args.approved_only,
        questions_path=args.questions,
    )

    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print("\n" + "=" * 56)
        print(f"  Sage Retrieval Eval  —  {metrics['num_questions']} questions, k={metrics['k']}")
        print(f"  rerank={metrics['rerank']}  approved_only={metrics['approved_only']}")
        print("=" * 56)
        for kk in metrics["ks"]:
            print(f"  recall@{kk:<2d}   {_fmt_pct(metrics[f'recall@{kk}'])}"
                  f"     hit_rate@{kk:<2d}  {_fmt_pct(metrics[f'hit_rate@{kk}'])}")
        print("-" * 56)
        print(f"  MRR                {metrics['mrr']:.4f}")
        print(f"  hit_rate (k={metrics['k']})     {_fmt_pct(metrics['hit_rate'])}")
        print(f"  exact-match hit    {_fmt_pct(metrics['exact_match_hit_rate'])} "
              f"({metrics['num_exact_match']} q)")
        print("=" * 56)
        if args.verbose:
            print("\n  Per-question:")
            for q in metrics["per_question"]:
                mark = "HIT " if q["hit"] else "MISS"
                rank = q["first_relevant_rank"] or "-"
                print(f"   [{mark}] rank={rank!s:>3}  {q['id']:<22} {q['expected_source']}")
                if not q["hit"]:
                    print(f"           retrieved: {q['retrieved']}")

    if args.min_hit_rate > 0 and metrics["exact_match_hit_rate"] < args.min_hit_rate:
        print(f"\nFAIL: exact-match hit-rate {metrics['exact_match_hit_rate']:.3f} "
              f"< threshold {args.min_hit_rate:.3f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
