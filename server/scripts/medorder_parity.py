"""Medication-order store: the parity report, and adoption on demand (medorders-2).

Prints where the medication-order store (migration 61) and the ward's chart
rows disagree, and what is not in the store yet. Read-only unless --adopt.

    python scripts/medorder_parity.py              # the report, in words
    python scripts/medorder_parity.py --json       # the report as JSON
    python scripts/medorder_parity.py --adopt      # adopt unlinked chart rows,
                                                   # then the report

--adopt does what the server does at boot with ORB_MED_ORDER_STORE on, and is
refused while that switch is off: an adopted link is what the bedside check
follows from then on, so links are made only on a site that turned the store
on. It writes one clinical_access audit row per 500 rows adopted.

Every run writes one phi_access audit row (the report lists patient ids), so it
fails, and prints nothing, if the audit trail cannot be written.

Point it at the database the way the server is pointed at it: ORB_DB_PATH and,
on an encrypted appliance, ORB_DB_ENCRYPTION_KEY in the environment. It does not
run migrations; on a database the server has not booted since migration 61, it
says so and stops.

Exit codes: 0 clean; 1 the report found problems; 2 refused or could not run.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from database import db_session  # noqa: E402
from services.audit_service import log_audit_event  # noqa: E402
from services.medorders import adoption  # noqa: E402

_SECTION_WORDS = {
    "projection_mismatches": "Chart rows that disagree with their order",
    "broken_links": "Broken links between a chart row and an order",
    "terminal_orders_with_open_supply": "Pharmacy supply still open for an ended order",
    "unlinked_live_rows": "Active or as-needed chart rows with no order yet",
    "unadoptable_rows": "Chart rows whose status could not be read (not adopted)",
    "helix_rows_without_order": "Pharmacy rows with no order (not adopted by design)",
    "cpoe_medication_rows_without_order": "Ordered medicines (CPOE) with no order in the store",
}


def _print_report(report: dict) -> None:
    print("Medication-order store parity report")
    print(f"  generated {report['generated_at']}")
    print(f"  store {'on' if report['store_on'] else 'off'}, "
          f"lifecycle {'on' if report['lifecycle_on'] else 'off'}")
    print(f"  {report['orders']} orders, {report['events']} ledger events")
    for group in ("problems", "pending", "informational"):
        print(f"\n{group.capitalize()}")
        for key, section in report[group].items():
            extra = (f" ({section['open_count']} still open)"
                     if "open_count" in section else "")
            print(f"  {_SECTION_WORDS.get(key, key)}: {section['count']}{extra}")
            for item in section["sample"][:10]:
                print("    " + ", ".join(f"{k} {v}" for k, v in item.items()))
    print("\nClean." if report["clean"] else "\nProblems found: see above.")


def _audit_report(report: dict, actor: str) -> None:
    with db_session() as db:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="phi_access",
            action_type="med_order_parity_report",
            outcome="success",
            db=db,
            summary_message="Medication-order parity report read from the command line",
            actor_user_id=actor,
            actor_role="system",
            target_type="med_order_store",
            target_id="parity",
            metadata={
                "clean": report["clean"],
                "counts": {k: s["count"] for g in ("problems", "pending", "informational")
                           for k, s in report[g].items()},
            },
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Medication-order store parity report.")
    ap.add_argument("--adopt", action="store_true",
                    help="adopt unlinked chart rows first (needs ORB_MED_ORDER_STORE on)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--actor", default=adoption.SYSTEM_ACTOR,
                    help="the user id recorded on the audit rows (default: system)")
    ap.add_argument("--sample", type=int, default=50, help="ids listed per section")
    args = ap.parse_args(argv)

    adopted = None
    if args.adopt:
        try:
            adopted = adoption.adopt_legacy(actor_id=args.actor, actor_role="system",
                                            trigger="script")
        except adoption.AdoptionRefused as e:
            print(str(e), file=sys.stderr)
            return 2
    try:
        report = adoption.parity_report(sample_limit=max(1, args.sample))
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    _audit_report(report, args.actor)

    if args.json:
        print(json.dumps({"adopted": adopted, "report": report}, indent=2, default=str))
    else:
        if adopted is not None:
            print(f"Adopted {adopted['adopted']} chart row(s) in {adopted['batches']} batch(es); "
                  f"{adopted['unadoptable']} unreadable, {adopted['conflict']} with a broken link.\n")
        _print_report(report)
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
