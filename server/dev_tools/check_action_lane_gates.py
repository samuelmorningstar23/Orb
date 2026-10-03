"""Which Sage turns get a gate, and which get none.

Two deterministic mechanisms decide what happens to an instruction typed into
Sage, both before any model runs and neither depending on model compliance:

    sage_service.detect_mutation_intent(q)  -> the CHART-MUTATION gate.
        True buffers the whole turn (`mutation_locked` in routes/sage_routes.py),
        streams nothing, and lets the action-state override rewrite the answer.

    thinking_mode.classify_floor(q, ...)    -> the SAFETY FLOOR.
        Non-empty routes the turn to the careful 27B even with thinking OFF.

`docs/perf/FAST_LANE_MARKER_2026-09-07.md` section 7b recorded six
mutation-shaped imperatives that tripped NEITHER, so they reached `medgemma:4b`
with no deterministic lock on them. They are now caught by floor rule 4
(`work_request_imperative`) and still, deliberately, NOT by the mutation gate.
This script prints the matrix so that split can be re-checked in a second rather
than believed, and so the next widening can be measured against the questions it
must not swallow.

Pure and offline: both functions are pure string predicates, so this needs no
database, no Ollama and no server. The corpus lives in
`tests/action_lane_corpus.py` and is shared with
`tests/test_action_lane_gates.py`, so the tool and the regression suite can never
disagree about what the table says.

    cd server && .venv/bin/python dev_tools/check_action_lane_gates.py
    cd server && .venv/bin/python dev_tools/check_action_lane_gates.py --with-patient

WHAT `--with-patient` SHOWS, and why it matters more than the bare table. The
route passes `effective_patient_id` (the open chart, `routes/sage_routes.py`) and
the assembled context into `classify_floor`, so on a ward round rule 1 fires on
EVERY turn and the ungated set is empty. The six are ungated only from a
chart-free Sage: no patient open, none named, no pronoun.

Exit status is 0 when every row matches the corpus and 1 when any row has moved,
so this is also usable as a quick check after touching either predicate.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import sage_service, thinking_mode  # noqa: E402
from tests.action_lane_corpus import ALL_ROWS  # noqa: E402

WITH_PATIENT = "--with-patient" in sys.argv

#: Stands in for the open chart. `_assemble_patient_context` emits `PATIENT:` as
#: the first line of the chart block, and `classify_floor` matches on it.
CHART_CONTEXT = "PATIENT: Ananya Kapoor, 54y/F, Bed GM-101\n"


def main() -> int:
    rows, drift = [], 0
    for section, query, want_mut, want_floor in ALL_ROWS:
        mut = sage_service.detect_mutation_intent(query)
        floor = thinking_mode.classify_floor(
            query,
            patient_id="P-GM1" if WITH_PATIENT else None,
            context=CHART_CONTEXT if WITH_PATIENT else None,
        )
        if WITH_PATIENT:
            moved = False          # the corpus records the BARE verdict only
        else:
            moved = (mut != want_mut) or (floor != tuple(want_floor))
        drift += bool(moved)
        rows.append((section, query, mut, floor, moved))

    width = min(max(len(r[1]) for r in rows), 58)
    print(f"{'section':<16} {'query':<{width}} {'mutation':<9} floor")
    print("-" * (width + 46))
    last = None
    for section, query, mut, floor, moved in rows:
        if section != last:
            print()
            last = section
        gate = "LOCK" if mut else ("floor" if floor else "NONE")
        mark = "  <-- MOVED" if moved else ""
        print(f"{section:<16} {query[:width]:<{width}} "
              f"{('True' if mut else 'False'):<9} "
              f"{','.join(floor) or '-'}   [{gate}]{mark}")

    # An INSTRUCTION with no gate is the thing section 7b was about. A question
    # or a statement with no gate is the fast lane working, so the two are
    # counted apart — a single "ungated" number would call the healthy case a
    # hole and give the next person a reason to widen something.
    ungated = [r for r in rows if not r[2] and not r[3]]
    not_orders = ("question", "statement")
    ungated_orders = [r for r in ungated if r[0] not in not_orders]
    print()
    print(f"{len(rows)} phrasings"
          + (" (patient in scope)" if WITH_PATIENT else " (bare query, no patient)")
          + f": {len(ungated_orders)} INSTRUCTION(S) with neither gate, "
          + f"{len(ungated) - len(ungated_orders)} question(s)/statement(s) "
            "left on the fast lane as intended")
    if not WITH_PATIENT:
        print("Re-run with --with-patient for the ward-round case, where the "
              "route passes the open chart\nand rule 1 fires on every row.")
    if drift:
        print(f"\n{drift} row(s) no longer match tests/action_lane_corpus.py. "
              "Either a predicate changed or the\ncorpus is stale — decide "
              "which, then update the corpus WITH the verdict you measured.")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
