"""Load a formulary (medication master catalog) into helix_medication_master.

The catalog is keyed by RxNorm concept (RxCUI). Two ways to obtain the data:

1. RxNorm full monthly release (RRF files), from the NLM:
       https://www.nlm.nih.gov/research/umls/rxnorm/docs/rxnormfiles.html
   The concepts live in ``RXNCONSO.RRF`` (pipe-delimited). Filter to the rows
   you want (e.g. TTY in {SCD, SBD} for dispensable products) and reshape to the
   simple CSV this loader expects. The full file is large and licence-adjacent
   (UMLS Metathesaurus License, free but registration-gated), so it is NOT
   shipped here.

2. RxNav REST API (no download, no licence key), for targeted lookups:
       https://rxnav.nlm.nih.gov/REST/
   e.g. GET https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/allProperties.json
   Use this to enrich individual drugs rather than bulk-load.

Expected CSV columns (case-insensitive; extra columns ignored):

    rxcui, name, generic_name, form, strength

``rxcui`` maps to ``helix_medication_master.id`` (prefixed ``RX-``). The upsert
is idempotent on that id, so re-running refreshes names/forms without
duplicating rows.

Usage
-----
    python scripts/load_rxnorm_formulary.py --file scripts/fixtures/rxnorm_sample.csv

A small sample lives at ``scripts/fixtures/rxnorm_sample.csv``.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from database import db_session  # noqa: E402


def _pick(row: dict, *names: str) -> str:
    lower = {k.lower().strip(): v for k, v in row.items() if k}
    for n in names:
        if n.lower() in lower and lower[n.lower()] is not None:
            return (lower[n.lower()] or "").strip()
    return ""


def load_file(path: Path) -> tuple[int, int]:
    """Load an RxNorm-style formulary CSV. Returns (rows_read, rows_upserted)."""
    read = upserted = 0
    with path.open(newline="", encoding="utf-8") as fh, db_session() as db:
        reader = csv.DictReader(fh)
        for row in reader:
            rxcui = _pick(row, "rxcui", "RXCUI", "id")
            name = _pick(row, "name", "str", "STR")
            if not rxcui or not name:
                continue
            read += 1
            med_id = rxcui if rxcui.upper().startswith("RX-") else f"RX-{rxcui}"
            db.execute(
                """INSERT INTO helix_medication_master
                       (id, name, generic_name, form, strength)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name,
                       generic_name=excluded.generic_name,
                       form=excluded.form,
                       strength=excluded.strength""",
                (med_id, name,
                 _pick(row, "generic_name", "generic", "ingredient") or None,
                 _pick(row, "form", "dose_form", "df") or None,
                 _pick(row, "strength") or None),
            )
            upserted += 1
    return read, upserted


def main() -> None:
    ap = argparse.ArgumentParser(description="Load RxNorm-style formulary CSV.")
    ap.add_argument("--file", required=True, help="Path to the formulary CSV file.")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_absolute():
        path = (SERVER_DIR / path).resolve()
    if not path.exists():
        ap.error(f"file not found: {path}")

    read, upserted = load_file(path)
    print(f"Read {read} formulary rows; upserted {upserted} into "
          f"helix_medication_master.")


if __name__ == "__main__":
    main()
