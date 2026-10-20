"""Bulk-load drug–drug interactions from a DDInter-format CSV.

DDInter (http://ddinter.scbdd.com/) is an open, comprehensive drug–drug
interaction database. Download the per-category CSV files from the download
page:

    http://ddinter.scbdd.com/download/

Each file (e.g. ``ddinter_downloads_code_A.csv``) has columns:

    DDInterID_A, Drug_A, DDInterID_B, Drug_B, Level

where ``Level`` is one of Major / Moderate / Minor / Unknown. This loader maps
those to our severities (Major→severe, Moderate→moderate, Minor→mild,
Unknown→moderate), normalises the drug names with the same
``drug_safety_service.ingredient_key`` the order-time screen matches on (a name
stored in any other form can never fire), and idempotently upserts them into
``drug_interactions`` with ``match_type='ingredient'`` and a configurable
``source`` (default ``ddinter``). It then prints how many formulary items the
knowledge base now covers, and names the ones it does not.

Usage
-----
    python scripts/load_interactions.py --file scripts/fixtures/ddinter_sample.csv
    python scripts/load_interactions.py --file ddinter_downloads_code_A.csv --source ddinter

A tiny sample lives at ``scripts/fixtures/ddinter_sample.csv`` to prove the
loader end-to-end without downloading the full dataset.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))


from database import db_session  # noqa: E402
from services.drug_safety_service import (  # noqa: E402
    formulary_interaction_coverage,
    ingredient_key,
)

_LEVEL_TO_SEVERITY = {
    "major": "severe",
    "moderate": "moderate",
    "minor": "mild",
    "unknown": "moderate",
}


def _pick(row: dict, *names: str) -> str:
    """Case-insensitive column fetch (DDInter headers vary in casing)."""
    lower = {k.lower().strip(): v for k, v in row.items() if k}
    for n in names:
        if n.lower() in lower and lower[n.lower()]:
            return lower[n.lower()].strip()
    return ""


def load_file(path: Path, source: str = "ddinter") -> tuple[int, int]:
    """Load a DDInter-format CSV. Returns (rows_read, rows_upserted)."""
    read = upserted = 0
    with path.open(newline="", encoding="utf-8") as fh, db_session() as db:
        reader = csv.DictReader(fh)
        for row in reader:
            drug_a = ingredient_key(_pick(row, "Drug_A", "drug_a", "A"))
            drug_b = ingredient_key(_pick(row, "Drug_B", "drug_b", "B"))
            level = _pick(row, "Level", "level").lower()
            if not drug_a or not drug_b:
                continue
            read += 1
            severity = _LEVEL_TO_SEVERITY.get(level, "moderate")
            db.execute(
                """INSERT INTO drug_interactions
                       (term_a, term_b, match_type, severity, mechanism, source)
                   VALUES (?, ?, 'ingredient', ?, ?, ?)
                   ON CONFLICT(term_a, term_b, match_type) DO UPDATE SET
                       severity=excluded.severity,
                       mechanism=excluded.mechanism,
                       source=excluded.source""",
                (drug_a, drug_b, severity, f"DDInter {level or 'unknown'} interaction.", source),
            )
            upserted += 1
    return read, upserted


def main() -> None:
    ap = argparse.ArgumentParser(description="Load DDInter-format interaction CSV.")
    ap.add_argument("--file", required=True, help="Path to the DDInter CSV file.")
    ap.add_argument(
        "--source", default="ddinter", help="Source tag stored on each row (default: ddinter)."
    )
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_absolute():
        path = (SERVER_DIR / path).resolve()
    if not path.exists():
        ap.error(f"file not found: {path}")

    read, upserted = load_file(path, source=args.source)
    print(
        f"Read {read} interaction rows; upserted {upserted} into drug_interactions "
        f"(source={args.source})."
    )
    print_coverage()


def print_coverage() -> None:
    """Per-formulary-item coverage: an uncovered item is screened for allergies only."""
    report = formulary_interaction_coverage()
    print(f"Formulary interaction coverage: {report['covered']} of {report['total']} items.")
    for item in report["items"]:
        if not item["covered"]:
            print(f"  NOT COVERED: {item['name']} ({item['generic_name'] or 'no generic name'})")


if __name__ == "__main__":
    main()
