"""Does a MARKERLESS turn in the history cost the CAREFUL lane its marker?

Section 7a of docs/perf/FAST_LANE_MARKER_2026-09-07.md, made runnable.

The fast tier emits no `NOACT|`/`ACT|`, so `sage_conversations` stores its turns
without one. `sage_service._history_turn_text(..., keep_marker=True)` replays the
previous assistant turn verbatim, and this model copies its own last output — so
the turn AFTER a fast turn can lose the contract on the careful lane too, which
means it does not stream either. The 2026-09-07 marker fix does not touch this:
it asserts the marker for models measured not to emit one, and the careful model
does emit one, right up until history teaches it not to.

Arms, all against the CAREFUL tier (config.LLM_MODEL), one follow-up question:

    none          no history at all
    careful       a previous careful turn, its leading marker intact
    fast          a previous FAST turn, no marker (what the fast lane stores)
    fast+mutation the same markerless turn, stored action state = a proposed
                  chart mutation. This arm exists to price the fix rather than
                  flatter it: a mutation turn's stored content is the model's
                  RAW narration, so it deliberately gets NO synthesized marker
                  and must come out of a change UNMOVED.

Read `fast+mutation` against ITSELF across a change, never against `fast`. Its
question is a mutation request, so the user turn differs too and the two arms
are not comparable in one run: measured 2026-09-07 before any fix, with the
identical markerless assistant turn in both, `fast` scored 5/10 and
`fast+mutation` 10/10. Which is its own small finding, and the reason this arm
is not evidence that a markerless turn is harmless.

Measured 2026-09-07, medgemma:27b, 5 runs x 2 sweeps, prior turns held
byte-identical across all three runs:

                    before   ORB_SAGE_HISTORY_MARKER=off   shipped
    none            10/10              10/10                10/10
    careful         10/10              10/10                10/10
    fast             5/10               4/10                10/10
    fast+mutation   10/10              10/10                10/10

Artifacts: `ops/history_marker_replay_{before,after_off,after_auto}.json`.

Every arm goes through Orb's own `_build_chat_messages` with the history in the
shape `get_conversation_history` returns, so whatever prompt assembly does to a
stored turn is what is being measured. Run it before and after a change to that
path.

    cd server && set -a && . ../.env && set +a && \
        .venv/bin/python dev_tools/check_history_marker_replay.py --runs 5

The prior turns are generated once and cached in
`ops/history_marker_replay_turns.json` so a later run measures against
BYTE-IDENTICAL history; --reseed regenerates them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from config import LLM_MODEL, OLLAMA_BASE_URL, THINKING_FAST_MODEL  # noqa: E402

SEED_QUERY = "What are the diagnostic criteria for ARDS?"
FOLLOWUP_QUERY = "What about steroids?"

# ── suite "mutation": which marker should open a REPLAYED mutation turn? ──
# Item 10 of docs/guide/41-thinking-mode.md. A mutation turn is now replayed as
# the safe message the CLINICIAN read rather than the narration the model
# drafted, which removes the objection to giving it a marker at all. Two
# candidates remain and they fail in opposite directions, so both follow-ups are
# needed to tell them apart:
#
#   benign follow-up    an `ACT|` here is a FALSE action state. The route builds
#                       one, and the clinician's clinical answer is replaced by
#                       "I understood your request to update the chart…". This
#                       is the loud failure and it is what the `act` arm risks.
#   mutation follow-up  a `NOACT|` here is the model's second opinion going
#                       quiet on a real chart request. The deterministic gate
#                       still fires, so this is the quiet failure, and it is what
#                       the `noact` arm risks.
MUTATION_QUERY = "add paracetamol to the chart"
# Short on purpose: _is_followup_turn gates whether a `simple` turn carries
# history at all, and a 7-word imperative does not qualify, so a longer phrasing
# silently measures "no history" in every arm. The premise check below refuses to
# run rather than let that happen again.
MUTATION_FOLLOWUP = "add ibuprofen too"
#: Verbatim from routes.sage_routes, the supported add_medication branch.
SAFE_ANSWER = ("Drafted request for Paracetamol. Please review and approve to "
               "send to the Pharmacy.")

TURNS_CACHE = Path(__file__).resolve().parent.parent / "ops" / "history_marker_replay_turns.json"

#: A stored mutation turn's action state, in the shape `_save_turn` persists
#: (`sources` = the meta blob, `parsed_response` = the parsed reply).
MUTATION_INTENT = '{"action_type": "add_medication", "medication": "Paracetamol 1g PO QDS"}'


async def _complete(model: str, messages: list[dict], options: dict) -> str:
    async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
        r = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json={
            "model": model, "messages": messages, "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": options["temperature"],
                        "num_predict": options["num_predict"],
                        "num_ctx": options["num_ctx"]},
        })
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content") or ""


def _stored_assistant(content: str, *, proposed_action: bool,
                      safe_answer: str | None = None) -> dict:
    """One stored assistant turn in the shape `get_conversation_history` returns.

    The `sources` blob is built the way `_save_turn` writes it and then read
    back through Orb's own derivation, so this measures the production rule
    rather than a hand-set flag. Against a tree that predates that derivation
    the key is simply absent, which is the same "unknown" the pre-fix assembly
    path saw anyway — so one probe file measures both sides of the change.
    """
    blob = {"proposed_action": proposed_action,
            "parsed_response": {"action_intent":
                                MUTATION_INTENT if proposed_action else "None."}}
    if safe_answer:
        blob["safe_answer"] = safe_answer
    blob = json.dumps(blob)
    row = {"role": "assistant", "content": content, "sources": blob}
    try:
        from services.sage_service import _stored_turn_proposed_action
    except ImportError:
        return row
    row["proposed_action"] = _stored_turn_proposed_action(blob)
    if safe_answer:
        from services.sage_service import _stored_turn_safe_answer
        row["safe_answer"] = _stored_turn_safe_answer(blob)
    return row


async def _seed_turns(messages, options, mutation_messages) -> dict:
    """One real reply per tier to the SAME seed question, held fixed after this,
    plus one real fast-tier reply to the MUTATION query: the drafted narration
    that the action-state override threw away and that the replay lane was still
    handing to the next prompt."""
    careful = await _complete(LLM_MODEL, messages, options)
    fast = await _complete(THINKING_FAST_MODEL, messages, options)
    narration = await _complete(THINKING_FAST_MODEL, mutation_messages, options)
    return {"seed_query": SEED_QUERY, "careful": careful, "fast": fast,
            "mutation_query": MUTATION_QUERY, "narration": narration,
            "generated": datetime.now().isoformat(timespec="seconds")}


def main_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", type=int, default=5, help="runs per arm (default 5)")
    p.add_argument("--repeat", type=int, default=2,
                   help="times to repeat the whole sweep (default 2, as in 7a)")
    p.add_argument("--reseed", action="store_true",
                   help="regenerate the cached prior turns")
    p.add_argument("--suite", default="replay", choices=("replay", "mutation"),
                   help="replay: does a MARKERLESS turn cost the next turn its "
                        "marker (section 7a). mutation: which marker should open "
                        "a replayed MUTATION turn (item 10)")
    p.add_argument("--label", default="",
                   help="suffix for the results file, so two runs of one sweep "
                        "(say auto vs ORB_SAGE_HISTORY_MARKER=off) do not "
                        "rotate each other out")
    return p.parse_args()


async def main() -> None:
    args = main_args()

    from database import db_session
    from routes.sage_routes import _leading_marker
    from services.sage_service import (
        SCOPE_CONFIGS,
        _build_chat_messages,
        get_relevant_context,
    )

    with db_session() as db:
        user = dict(db.execute(
            "SELECT id, name, role, department FROM users "
            "WHERE role = 'doctor' AND department IS NOT NULL LIMIT 1").fetchone())

    seed_context, seed_scope, *_ = get_relevant_context(SEED_QUERY, user, None, [])
    seed_cfg = SCOPE_CONFIGS.get(seed_scope, SCOPE_CONFIGS["simple"])
    seed_opts = {"temperature": seed_cfg.get("temperature", 0.4),
                 "num_predict": seed_cfg["num_predict"], "num_ctx": seed_cfg["num_ctx"]}

    if args.reseed or not TURNS_CACHE.exists() \
            or "narration" not in json.loads(TURNS_CACHE.read_text()):
        mut_context, mut_scope, *_ = get_relevant_context(MUTATION_QUERY, user, None, [])
        turns = await _seed_turns(
            _build_chat_messages(SEED_QUERY, seed_context, [], seed_scope), seed_opts,
            _build_chat_messages(MUTATION_QUERY, mut_context, [], mut_scope))
        TURNS_CACHE.parent.mkdir(exist_ok=True)
        TURNS_CACHE.write_text(json.dumps(turns, indent=2))
        print(f"seeded prior turns -> {TURNS_CACHE.name}")
    turns = json.loads(TURNS_CACHE.read_text())

    careful_raw, fast_raw = turns["careful"], turns["fast"]
    print(f"prior turns generated {turns['generated']} on the seed question "
          f"{turns['seed_query']!r}")
    for name, raw in (("careful", careful_raw), ("fast", fast_raw)):
        mk, _ = _leading_marker(raw)
        print(f"  stored {name:<8} {len(raw):>5} chars, leading marker "
              f"{mk or 'NONE'}: {raw.lstrip()[:56]!r}")
    if _leading_marker(careful_raw)[0] is None:
        print("  !! the careful prior turn has NO leading marker — the `careful` "
              "arm is not the control it is supposed to be; --reseed")
    if _leading_marker(fast_raw)[0] is not None:
        print("  !! the fast prior turn HAS a leading marker — the `fast` arm is "
              "not reproducing what the fast lane stores; --reseed")

    user_turn = {"role": "user", "content": SEED_QUERY}
    mutation_turn = {"role": "user", "content": MUTATION_QUERY}
    narration = turns["narration"]
    # THE ARM ITEM 10 IS ABOUT is a mutation turn with NO marker in storage, which
    # is what the DETERMINISTIC gate produces: it locks on the query with the
    # model declaring nothing. The 4B does not reliably oblige here. Asked an
    # imperative rather than a question it often leads with `ACT|` (section 7b
    # measured 4 of 8 on "order a chest X-ray"), and `_history_turn_text` keeps a
    # leading marker that is really there, so seeding this arm from a raw reply
    # would quietly measure the model-declared lane instead. So the marker is
    # taken off deliberately, and both forms are printed.
    # Strip with sage_service's OWN leading-marker pattern, not the route's:
    # `ACT|` in the contract is `ACT|{payload}`, and _leading_marker knows only
    # about the token, so taking that off alone leaves the JSON payload sitting
    # at the head of the "markerless" turn. (The 2026-09-07 run of this suite
    # did exactly that, which is why its narration/bare arm shows a stray
    # `{"action_type":…}`; safe/bare is the clean control for the same question
    # and agreed with it.)
    from services.sage_service import _HISTORY_MARKER_RE
    bare_narration = _HISTORY_MARKER_RE.sub("", narration).lstrip() or narration

    if args.suite == "replay":
        followups = [("benign", FOLLOWUP_QUERY)]
        # `mutation_marker` is the per-arm ORB_SAGE_HISTORY_MUTATION_MARKER.
        arms = {
            "none": ([], None),
            "careful": ([user_turn,
                         _stored_assistant(careful_raw, proposed_action=False)], None),
            "fast": ([user_turn,
                      _stored_assistant(fast_raw, proposed_action=False)], None),
            "fast+mutation": ([mutation_turn,
                               _stored_assistant(fast_raw, proposed_action=True)], None),
        }
    else:
        followups = [("benign", FOLLOWUP_QUERY), ("mutation", MUTATION_FOLLOWUP)]
        arms = {
            # No safe_answer on the row = a turn stored before item 10, and the
            # behaviour this suite is trying to beat.
            "narration/bare": ([mutation_turn,
                                _stored_assistant(bare_narration,
                                                  proposed_action=True)], None),
            "safe/bare": ([mutation_turn,
                           _stored_assistant(narration, proposed_action=True,
                                             safe_answer=SAFE_ANSWER)], "off"),
            "safe/NOACT": ([mutation_turn,
                            _stored_assistant(narration, proposed_action=True,
                                              safe_answer=SAFE_ANSWER)], "noact"),
            "safe/ACT": ([mutation_turn,
                          _stored_assistant(narration, proposed_action=True,
                                            safe_answer=SAFE_ANSWER)], "act"),
        }
        print(f"  stored narration {len(narration):>4} chars, leading marker "
              f"{_leading_marker(narration)[0] or 'NONE'}: "
              f"{narration.lstrip()[:52]!r}")
        print(f"  markerless form  {len(bare_narration):>4} chars: "
              f"{bare_narration.lstrip()[:52]!r}")

    prepared = {}
    for label, query in followups:
        context, scope, *_ = get_relevant_context(query, user, None, [])
        cfg = SCOPE_CONFIGS.get(scope, SCOPE_CONFIGS["simple"])
        prepared[label] = (query, context, scope,
                           {"temperature": cfg.get("temperature", 0.4),
                            "num_predict": cfg["num_predict"],
                            "num_ctx": cfg["num_ctx"]})

    print(f"\nsuite {args.suite!r} on {LLM_MODEL}, "
          f"{args.runs} runs x {args.repeat} sweeps per arm x follow-up\n")
    print(f"{'arm':<15} {'follow-up':<10} {'sweep':<6} {'NOACT':>6} "
          f"{'ACT':>4} {'none':>5}  replayed history head")
    print("-" * 118)

    results = []
    for sweep in range(1, args.repeat + 1):
        for arm, (history, mutation_marker) in arms.items():
            if mutation_marker is None:
                os.environ.pop("ORB_SAGE_HISTORY_MUTATION_MARKER", None)
            else:
                os.environ["ORB_SAGE_HISTORY_MUTATION_MARKER"] = mutation_marker
            for label, (query, context, scope, options) in prepared.items():
                messages = _build_chat_messages(query, context, history, scope)
                replayed = [m["content"] for m in messages
                            if m["role"] == "assistant"]
                # PROVE THE PREMISE. `_is_followup_turn` decides whether a
                # `simple` turn carries history at all, so a follow-up phrased a
                # word too long turns every arm into the no-history control and
                # the table still prints a clean-looking result. Refuse instead.
                if history and not replayed:
                    raise SystemExit(
                        f"arm {arm!r} / follow-up {label!r} ({query!r}, scope "
                        f"{scope}) reached the prompt with NO replayed assistant "
                        f"turn, so this arm would measure nothing. Shorten the "
                        f"follow-up until _is_followup_turn accepts it.")
                replayed = replayed or [""]
                counts = {"NOACT|": 0, "ACT|": 0, None: 0}
                for _ in range(args.runs):
                    text = await _complete(LLM_MODEL, messages, options)
                    marker, _end = _leading_marker(text)
                    counts[marker] = counts.get(marker, 0) + 1
                    results.append({"arm": arm, "followup": label, "sweep": sweep,
                                    "leading_marker": marker, "chars": len(text),
                                    "mutation_marker": mutation_marker,
                                    "replayed": replayed[-1][:200],
                                    "head": text.lstrip()[:60]})
                print(f"{arm:<15} {label:<10} {sweep:<6} "
                      f"{counts['NOACT|']:>6} {counts['ACT|']:>4} "
                      f"{counts[None]:>5}  {replayed[-1][:46]!r}")
    os.environ.pop("ORB_SAGE_HISTORY_MUTATION_MARKER", None)

    print("-" * 118)
    summary = {}
    for arm in arms:
        for label, _ in followups:
            rows = [r for r in results
                    if r["arm"] == arm and r["followup"] == label]
            got = sum(1 for r in rows if r["leading_marker"])
            act = sum(1 for r in rows if r["leading_marker"] == "ACT|")
            summary[f"{arm} / {label}"] = {
                "leading_marker": f"{got}/{len(rows)}", "ACT": f"{act}/{len(rows)}"}
            print(f"{arm:<15} {label:<10} leading marker {got}/{len(rows)}, "
                  f"of which ACT| {act}/{len(rows)}")

    name = (f"history_marker_replay_{args.suite}"
            f"{'_' + args.label if args.label else ''}.json")
    out = TURNS_CACHE.with_name(name)
    if out.exists():
        stamp = datetime.fromtimestamp(out.stat().st_mtime).strftime("%Y-%m-%d_%H%M")
        keep = out.with_name(f"{out.stem}_{stamp}{out.suffix}")
        if not keep.exists():
            keep.write_text(out.read_text())
            print(f"kept previous run as {keep.name}")
    out.write_text(json.dumps(
        {"model": LLM_MODEL, "suite": args.suite,
         "followups": dict(followups),
         "runs_per_arm": args.runs * args.repeat, "summary": summary,
         "history_marker": os.getenv("ORB_SAGE_HISTORY_MARKER", "auto"),
         "prior_turns_generated": turns["generated"], "results": results}, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
