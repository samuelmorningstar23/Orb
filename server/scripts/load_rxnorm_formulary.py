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

A DRUG IN USE KEEPS ITS CLINICAL IDENTITY (grn-5). generic_name, form and
strength decide how allergies and interactions are screened, so the loader
never rewrites them on a row that has a Helix order (as the prescribed drug or
as a substitute) or a stock lot. Such a row is left exactly as it is, listed
in the report and printed, whichever way ruling GRN-RS1 (who may change a
drug's identity) is eventually answered: the change belongs to a retire and
replace step, not to a bulk load. A brand-name change on an in-use row with
the same identity is still applied. Each run writes one 'security' audit row
with its counts, in the load's own transaction.

Usage
-----
    python scripts/load_rxnorm_formulary.py --file scripts/fixtures/rxnorm_sample.csv

A small sample lives at ``scripts/fixtures/rxnorm_sample.csv``.
"""
from __future__ import annotations

import argparse
import csv
import sys
import uuid
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


IDENTITY_COLUMNS = ("generic_name", "form", "strength")


def _in_use(db, med_id: str) -> bool:
    """A drug with an order (prescribed or substituted) or a stock lot."""
    return db.execute(
        "SELECT 1 FROM helix_patient_med_orders WHERE medication_id = ? "
        "OR substitute_medication_id = ? "
        "UNION ALL SELECT 1 FROM helix_inventory_lots WHERE medication_id = ? LIMIT 1",
        (med_id, med_id, med_id),
    ).fetchone() is not None


def load_file_report(path: Path) -> dict:
    """Load an RxNorm-style formulary CSV. Returns {"read", "upserted",
    "refused": [{"id", "name", "current", "incoming"}]}, where refused lists
    the in-use rows whose identity the file would have changed and which were
    left exactly as they were."""
    from services.audit_service import log_audit_event

    read = upserted = 0
    refused: list[dict] = []
    # Immediate: the in-use check and the write it guards sit under one write
    # lock, so an order placed mid-load cannot slip between them.
    with path.open(newline="", encoding="utf-8") as fh, db_session(immediate=True) as db:
        reader = csv.DictReader(fh)
        for row in reader:
            rxcui = _pick(row, "rxcui", "RXCUI", "id")
            name = _pick(row, "name", "str", "STR")
            if not rxcui or not name:
                continue
            read += 1
            med_id = rxcui if rxcui.upper().startswith("RX-") else f"RX-{rxcui}"
            incoming = {
                "generic_name": _pick(row, "generic_name", "generic", "ingredient") or None,
                "form": _pick(row, "form", "dose_form", "df") or None,
                "strength": _pick(row, "strength") or None,
            }
            current = db.execute(
                "SELECT generic_name, form, strength FROM helix_medication_master WHERE id = ?",
                (med_id,),
            ).fetchone()
            if current is not None:
                held = {c: current[c] for c in IDENTITY_COLUMNS}
                if held != incoming and _in_use(db, med_id):
                    refused.append({"id": med_id, "name": name, "current": held,
                                    "incoming": incoming})
                    continue
            db.execute(
                """INSERT INTO helix_medication_master
                       (id, name, generic_name, form, strength)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name,
                       generic_name=excluded.generic_name,
                       form=excluded.form,
                       strength=excluded.strength""",
                (med_id, name, incoming["generic_name"], incoming["form"],
                 incoming["strength"]),
            )
            upserted += 1
        log_audit_event(
            correlation_id=f"formulary-load-{uuid.uuid4()}",
            event_category="security",
            action_type="pharmacy_formulary_loaded",
            outcome="success",
            db=db,
            summary_message=(f"Formulary file loaded: {read} rows read, {upserted} written, "
                             f"{len(refused)} in-use rows left unchanged"),
            actor_user_id="system",
            target_type="formulary_file",
            target_id=str(path.name),
            metadata={"read": read, "upserted": upserted,
                      "refused_ids": [r["id"] for r in refused][:200]},
        )
    return {"read": read, "upserted": upserted, "refused": refused}


def load_file(path: Path) -> tuple[int, int]:
    """Load an RxNorm-style formulary CSV. Returns (rows_read, rows_upserted);
    see load_file_report for the rows it refused."""
    report = load_file_report(path)
    return report["read"], report["upserted"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Load RxNorm-style formulary CSV.")
    ap.add_argument("--file", required=True, help="Path to the formulary CSV file.")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_absolute():
        path = (SERVER_DIR / path).resolve()
    if not path.exists():
        ap.error(f"file not found: {path}")

    report = load_file_report(path)
    print(f"Read {report['read']} formulary rows; upserted {report['upserted']} into "
          f"helix_medication_master.")
    if report["refused"]:
        print(f"Left {len(report['refused'])} drug(s) unchanged because they have orders or "
              f"stock and the file would change their generic name, form or strength:")
        for item in report["refused"]:
            print(f"  {item['id']} {item['name']}: on file {item['current']}, "
                  f"in the file {item['incoming']}")


if __name__ == "__main__":
    main()
