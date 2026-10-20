#!/usr/bin/env python
"""CLI for RAG confidence-threshold calibration.

Replaces the hand-picked magic numbers in _get_guideline_context with values
derived from a labelled in-corpus/out-of-corpus set. Runs the calibration set
through the SAME retrieval + signal path the runtime uses, derives the
no-relevance floors + high/medium cutoffs, and writes
server/eval/retrieval_thresholds.json (loaded automatically at runtime).

Usage:
    ORB_DEPLOYMENT_PROFILE=dev python scripts/calibrate_retrieval.py
    ORB_DEPLOYMENT_PROFILE=dev python scripts/calibrate_retrieval.py --dry-run

Re-run after changing the corpus or installing the dense embedder — calibration
reflects whatever retrieval path is active.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description="Calibrate RAG confidence thresholds")
    ap.add_argument("--dry-run", action="store_true", help="Compute but do not write thresholds")
    ap.add_argument(
        "--skip-init", action="store_true", help="Do not (re)build the knowledge base first"
    )
    ap.add_argument(
        "--load-corpus",
        action="store_true",
        help="Also load server/corpus/ before calibrating (matches a shipped appliance)",
    )
    ap.add_argument(
        "--no-cross-encoder",
        action="store_true",
        help="Calibrate the LEXICAL relevance scorer (the path an appliance without "
        "the reranker weights runs on). Run once with and once without: each run "
        "derives the floor for the scorer it exercised and preserves the other.",
    )
    args = ap.parse_args()

    if args.no_cross_encoder:
        os.environ["ORB_SAGE_CROSS_ENCODER"] = "0"

    if not args.skip_init:
        try:
            from services.clinical_knowledge import init_knowledge_base

            init_knowledge_base()
        except Exception as e:
            print(f"WARNING: knowledge base init failed ({e}); calibrating against current state")

    if args.load_corpus:
        try:
            from services import corpus_loader

            corpus_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "corpus"
            )
            corpus_loader.load_directory(corpus_dir, default_trust_tier=3)
        except Exception as e:
            print(f"WARNING: corpus load failed ({e}); calibrating against current state")

    from services.retrieval_calibration import DEFAULT_THRESHOLDS, calibrate

    result = calibrate(write=not args.dry_run)
    print("=" * 70)
    print("RAG confidence-threshold calibration")
    print("=" * 70)
    print(
        f"  defaults : {json.dumps({k: dict(DEFAULT_THRESHOLDS)[k] for k in DEFAULT_THRESHOLDS if k != 'source'})}"
    )
    print(
        f"  derived  : no_relevance_score_floor={result['no_relevance_score_floor']} "
        f"no_relevance_lexical_floor={result['no_relevance_lexical_floor']}"
    )
    print(
        f"             high_score={result['high_score']} high_lexical={result['high_lexical']} "
        f"medium_score={result['medium_score']} medium_lexical={result['medium_lexical']}"
    )
    print(
        f"             min_relevance_cross_encoder={result.get('min_relevance_cross_encoder')} "
        f"min_relevance_lexical={result.get('min_relevance_lexical')}"
    )
    m = result.get("metrics", {})
    print(f"  labelled : {m.get('positives')} positives / {m.get('negatives')} negatives")
    print(
        f"  relevance-decision precision={m.get('relevance_precision')} recall={m.get('relevance_recall')} "
        f"(TN={m.get('true_negatives')}, FP={m.get('false_positives')})"
    )
    for method, fm in (m.get("relevance_floor") or {}).items():
        print(
            f"  floor[{method}] = {fm['floor']}  "
            f"(gap: max negative {fm['max_negative_score']} < min positive {fm['min_positive_score']})"
        )
        print(
            f"             cases     : evidence kept on {fm['evidence_kept_on_positives']}/{fm['positives']} positives, "
            f"{fm['evidence_kept_on_negatives']}/{fm['negatives']} negatives"
        )
        # Document level — the half this harness could not see before 2026-08-15.
        if "relevant_documents" in fm:
            print(
                f"             documents : {fm['relevant_documents_cited']}/{fm['relevant_documents']} relevant guidelines cited, "
                f"{fm['irrelevant_documents_cited']} irrelevant ones cited"
            )
            if fm.get("unlabelled_cases"):
                print(
                    f"             WARNING   : {fm['unlabelled_cases']} positive case(s) carry no `expected` "
                    f"document list — scored top-hit-only, blind to rank 2"
                )
    if args.dry_run:
        print("  (dry-run — thresholds NOT written)")
    else:
        from services.retrieval_calibration import THRESHOLDS_PATH

        print(f"  written  : {THRESHOLDS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
