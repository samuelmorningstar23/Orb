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

WITH THE PHARMACY STORE ON (ORB_PHARMACY_STORE, any state but 'off'), the
loader follows ruling GRN-RS1 option (b) as the masters screen does (grn-6):
  * a NEW row lands as a DRAFT with a pending identity request, which a
    pharmacist must confirm before anything can order it (the file is not a
    person, so any pharmacist may confirm or refuse it). It is listed under
    `drafted`;
  * a draft's identity is refreshed from the file, and its pending request
    with it, so the pharmacist confirms what is on the row;
  * a CONFIRMED or RETIRED row's generic name, form and strength are never
    rewritten by a file, in use or not: a correction goes through Masters
    with a second pharmacist. Such rows are listed under `governed` and left
    exactly as they are (a brand-name change with the same identity still
    applies).
With the module off nothing above applies and the loader behaves as before:
there is no screen to confirm a draft on, so a draft would be a dead end.

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


def _governed() -> bool:
    """Ruling GRN-RS1 applies to the loader while the pharmacy store is on
    (or draining): the screen a pharmacist confirms a draft on exists."""
    import config

    return config.module_state("pharmacy-store") != "off"


def _load_governed(db, path: Path, reader) -> dict:
    """The loader with the pharmacy store on (see the module docstring)."""
    from services import pharmacy_master_service as masters

    read = upserted = 0
    refused: list[dict] = []
    governed: list[dict] = []
    drafted: list[str] = []
    for row in reader:
        rxcui = _pick(row, "rxcui", "RXCUI", "id")
        name = masters.identity_text(_pick(row, "name", "str", "STR"))
        if not rxcui or not name:
            continue
        read += 1
        med_id = rxcui if rxcui.upper().startswith("RX-") else f"RX-{rxcui}"
        incoming = {
            "generic_name": masters.identity_text(_pick(row, "generic_name", "generic",
                                                        "ingredient")),
            "form": masters.identity_text(_pick(row, "form", "dose_form", "df")),
            "strength": masters.identity_text(_pick(row, "strength")),
        }
        current = db.execute(
            "SELECT name, generic_name, form, strength, identity_status "
            "FROM helix_medication_master WHERE id = ?", (med_id,)).fetchone()
        if current is None:
            db.execute(
                """INSERT INTO helix_medication_master (id, name, generic_name, form, strength,
                       identity_status, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'draft', ?, CURRENT_TIMESTAMP)""",
                (med_id, name, incoming["generic_name"], incoming["form"], incoming["strength"],
                 masters.LOADER_REQUESTER))
            db.execute(
                """INSERT INTO pharmacy_master_change_requests (helix_medication_id, change_kind,
                       field, from_value, to_value, reason, requested_by)
                   VALUES (?, 'identity_create', 'identity', NULL, ?, ?, ?)""",
                (med_id, masters.identity_snapshot({"name": name, **incoming}),
                 f"Added by the formulary file {path.name}", masters.LOADER_REQUESTER))
            drafted.append(med_id)
            upserted += 1
            continue
        held = {c: current[c] for c in IDENTITY_COLUMNS}
        status = current["identity_status"] or "active"
        if held != incoming:
            if masters.drug_in_use(db, med_id):
                refused.append({"id": med_id, "name": name, "current": held,
                                "incoming": incoming})
                continue
            if status != "draft":
                governed.append({"id": med_id, "name": name, "current": held,
                                 "incoming": incoming, "identity_status": status})
                continue
        db.execute(
            "UPDATE helix_medication_master SET name = ?, generic_name = ?, form = ?, "
            "strength = ? WHERE id = ?",
            (name, incoming["generic_name"], incoming["form"], incoming["strength"], med_id))
        if status == "draft":
            # The pharmacist confirms what is on the row, so the pending
            # request's identity follows the file.
            db.execute(
                "UPDATE pharmacy_master_change_requests SET to_value = ? "
                "WHERE helix_medication_id = ? AND change_kind = 'identity_create' "
                "AND decision = 'pending'",
                (masters.identity_snapshot({"name": name, **incoming}), med_id))
        upserted += 1
    return {"read": read, "upserted": upserted, "refused": refused,
            "governed": governed, "drafted": drafted}


def load_file_report(path: Path) -> dict:
    """Load an RxNorm-style formulary CSV. Returns {"read", "upserted",
    "refused": [{"id", "name", "current", "incoming"}]}, where refused lists
    the in-use rows whose identity the file would have changed and which were
    left exactly as they were. With the pharmacy store on the report also
    carries "drafted" (new rows waiting for a pharmacist) and "governed"
    (confirmed or retired rows whose identity the file would have changed,
    left as they were)."""
    from services.audit_service import log_audit_event

    if _governed():
        with path.open(newline="", encoding="utf-8") as fh, \
                db_session(immediate=True) as db:
            report = _load_governed(db, path, csv.DictReader(fh))
            log_audit_event(
                correlation_id=f"formulary-load-{uuid.uuid4()}",
                event_category="security",
                action_type="pharmacy_formulary_loaded",
                outcome="success",
                db=db,
                summary_message=(
                    f"Formulary file loaded: {report['read']} rows read, {report['upserted']} "
                    f"written, {len(report['drafted'])} new drafts waiting for a pharmacist, "
                    f"{len(report['refused']) + len(report['governed'])} rows left unchanged"),
                actor_user_id="system",
                target_type="formulary_file",
                target_id=str(path.name),
                metadata={"read": report["read"], "upserted": report["upserted"],
                          "refused_ids": [r["id"] for r in report["refused"]][:200],
                          "governed_ids": [r["id"] for r in report["governed"]][:200],
                          "drafted_ids": report["drafted"][:200]},
            )
        return report

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
    if report.get("drafted"):
        print(f"Added {len(report['drafted'])} new drug(s) as drafts. A pharmacist confirms each "
              "in Pharmacy store, Masters, before it can be ordered.")
    if report.get("governed"):
        print(f"Left {len(report['governed'])} confirmed or retired drug(s) unchanged because "
              "the file would change their generic name, form or strength. Correct them in "
              "Masters, with a second pharmacist:")
        for item in report["governed"]:
            print(f"  {item['id']} {item['name']}: on file {item['current']}, "
                  f"in the file {item['incoming']}")
    if report["refused"]:
        print(f"Left {len(report['refused'])} drug(s) unchanged because they have orders or "
              f"stock and the file would change their generic name, form or strength:")
        for item in report["refused"]:
            print(f"  {item['id']} {item['name']}: on file {item['current']}, "
                  f"in the file {item['incoming']}")


if __name__ == "__main__":
    main()
