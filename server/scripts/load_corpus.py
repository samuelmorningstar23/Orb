#!/usr/bin/env python3
"""CLI — load a directory of clinical guideline files into Sage's RAG store.

Reads ``.md`` / ``.txt`` guideline files (with optional YAML front-matter or JSON
sidecar metadata) from a directory, chunks them, and inserts them into the same
``clinical_knowledge`` store Sage's ``search_guidelines`` retrieves from. Embeddings
are computed when the BGE-M3 embedder is installed; otherwise chunks are indexed
lexically (FTS5) so retrieval still works — the mode is printed in the summary.

Usage:
    PYTHONPATH=. python scripts/load_corpus.py corpus
    PYTHONPATH=. python scripts/load_corpus.py /path/to/guidelines --trust-tier 2
    PYTHONPATH=. python scripts/load_corpus.py corpus --json

The database is chosen exactly like the rest of the app: ``ORB_DB_PATH`` env var,
falling back to server/orb.db (see config.py). Point it at a scratch DB to dry-run.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Allow running as `python scripts/load_corpus.py` (without PYTHONPATH=.) too.
_SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_ROOT not in sys.path:
    sys.path.insert(0, _SERVER_ROOT)


from services.corpus_loader import corpus_stats, load_directory  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load clinical guideline files into Sage's RAG corpus store."
    )
    parser.add_argument("path", help="Directory of .md/.txt guideline files to load")
    parser.add_argument(
        "--trust-tier",
        type=int,
        default=3,
        choices=(1, 2, 3),
        help="Default trust tier for files without one "
        "(1=hospital protocol, 2=national guideline, 3=reference). Default: 3",
    )
    parser.add_argument("--json", action="store_true", help="Emit the result summary as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable INFO logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        result = load_directory(args.path, default_trust_tier=args.trust_tier)
    except NotADirectoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    stats = corpus_stats()

    if args.json:
        print(json.dumps({"load": result, "stats": stats}, indent=2))
        return 0

    print(f"\nCorpus load — {result['directory']}")
    print(
        f"  mode:            {result['mode']}  (fts={result['fts_available']}, vec={result['vec_available']})"
    )
    print(f"  files found:     {result['files_found']}")
    print(f"  files loaded:    {result['files_loaded']}")
    print(f"  chunks loaded:   {result['chunks_loaded']}")
    if result["documents"]:
        print("\n  Per document:")
        for d in result["documents"]:
            if d.get("error"):
                print(f"    - {d['file']:<44} ERROR: {d['error']}")
            else:
                emb = "embedded" if d.get("embedded") else "lexical"
                print(
                    f"    - {d['file']:<44} tier {d.get('trust_tier', '?')} "
                    f"ver {d.get('version', '?')}  {d['chunks']:>2} chunks  [{emb}]"
                )

    print("\nCorpus stats (whole store):")
    print(f"  documents:            {stats['documents']}")
    print(f"  chunks:               {stats['chunks']}")
    print(f"  embedded chunks:      {stats['embedded_chunks']}")
    print(f"  lexical-only chunks:  {stats['lexical_only_chunks']}")
    print(f"  loader-tracked docs:  {stats['loader_documents']}")
    print(f"  store mode:           {stats['mode']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
