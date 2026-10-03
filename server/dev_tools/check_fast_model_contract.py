"""Does the fast tier obey Orb's fast-answer contract?

Sage's streaming gate (routes/sage_routes.MarkerAnswerStreamer) is FAIL-CLOSED:
nothing reaches the clinician until the model has emitted `NOACT|` or `ACT|`.
That is a safety property — an ungated stream cannot be un-read, so a turn whose
action decision is still unknown must not be shown.

The consequence for thinking mode is not obvious and is worth measuring rather
than assuming: a model that ignores the marker streams NOTHING. Its answer is
released in one lump by the buffered end-of-turn path, so its time-to-first-word
is its time-to-LAST-token, no matter how fast it decodes.

WHAT CHANGED ON 2026-09-07. The prediction above was measured and confirmed:
medgemma:27b emits the marker 9/9 at character 0, medgemma:4b 0/9, and the fast
lane therefore streamed nothing at all. The gate no longer waits on a model that
was measured not to honour it. `sage_service.server_asserts_marker` hands the
marker to Orb for those models, and the deterministic mutation gate, which never
depended on the model, still decides the turn.

So marker presence alone can no longer be read as "does the clinician get words
early". This script therefore stops INFERRING what the gate does and runs it:
every reply is pushed through the real `MarkerAnswerStreamer` twice, once owned
by the model (the pre-fix behaviour) and once under the policy production now
applies, and both answers are reported next to the marker count.

    cd server && set -a && . ../.env && set +a && \
        .venv/bin/python dev_tools/check_fast_model_contract.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from config import LLM_MODEL, OLLAMA_BASE_URL, THINKING_FAST_MODEL  # noqa: E402

RUNS_PER_CASE = 3


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



def _run_gate(text: str, *, mutation_locked: bool, asserted: bool,
              chunk: int = 8) -> dict:
    """Push a finished reply through Orb's REAL streaming gate, one chunk at a
    time, and report what a clinician would have been able to read and when.

    `at_char` is how much of the model's output had to arrive before the first
    visible character was released. It is a proxy for latency that does not
    depend on this machine's decode speed: `at_char == len(text)` means the
    answer was released only once the generation had finished, which is the lump
    this whole file exists to detect.
    """
    from routes.sage_routes import (
        MarkerAnswerStreamer,
        SentenceStreamSanitizer,
        _strip_forbidden_streaming,
    )

    gate = MarkerAnswerStreamer(
        SentenceStreamSanitizer(_strip_forbidden_streaming), mutation_locked)
    if asserted:
        gate.assert_marker()
    at_char, visible = None, ""
    for i in range(0, len(text), chunk):
        out = gate.feed(text[i:i + chunk]) or ""
        if out and at_char is None:
            at_char = min(i + chunk, len(text))
        visible += out
    visible += gate.finalize() or ""
    return {"streamed": bool(at_char), "at_char": at_char,
            "of_chars": len(text), "visible_chars": len(visible)}


def _gate_label(g: dict) -> str:
    if not g["streamed"]:
        return "LUMP"
    pct = 100 * g["at_char"] / max(g["of_chars"], 1)
    return f"stream @{pct:.0f}%"


async def main() -> None:
    from database import db_session
    from services.sage_service import (
        SAGE_FAST_MARKER_ACT,
        SAGE_FAST_MARKER_NOACT,
        SCOPE_CONFIGS,
        _build_chat_messages,
        detect_mutation_intent,
        get_relevant_context,
        server_asserts_marker,
    )

    with db_session() as db:
        user = dict(db.execute(
            "SELECT id, name, role, department FROM users "
            "WHERE role = 'doctor' AND department IS NOT NULL LIMIT 1").fetchone())
        row = db.execute(
            "SELECT p.id, p.name FROM patients p "
            "JOIN admissions a ON a.patient_id = p.id AND a.status = 'active' "
            "WHERE a.department = ? LIMIT 1", (user["department"],)).fetchone()
        patient = dict(row)

    cases = [
        ("a. simple definition", "What is hypoxia?", None),
        ("b. general clinical", "What are the diagnostic criteria for ARDS?", None),
        ("c. patient question",
         f"Summarise {patient['name']}'s current status and what is driving it.",
         patient["id"]),
    ]

    prepared = []
    for label, query, pid in cases:
        context, scope, *_rest = get_relevant_context(query, user, pid, [])
        cfg = SCOPE_CONFIGS.get(scope, SCOPE_CONFIGS["simple"])
        prepared.append((label, scope, query,
                         _build_chat_messages(query, context, [], scope),
                         {"temperature": cfg.get("temperature", 0.4),
                          "num_predict": cfg["num_predict"],
                          "num_ctx": cfg["num_ctx"]}))

    results = []
    print(f"{'model':<14} {'case':<22} {'marker':<8} {'at':>5} "
          f"{'gate: model':>12} {'gate: Orb policy':>17}  opening text")
    print("-" * 118)
    for model in (THINKING_FAST_MODEL, LLM_MODEL):
        for label, scope, query, messages, options in prepared:
            asserts = server_asserts_marker(scope, model)
            for i in range(RUNS_PER_CASE):
                text = await _complete(model, messages, options)
                head = text.lstrip()[:48].replace("\n", " ")
                if SAGE_FAST_MARKER_NOACT in text:
                    marker, at = "NOACT|", text.index(SAGE_FAST_MARKER_NOACT)
                elif SAGE_FAST_MARKER_ACT in text:
                    marker, at = "ACT|", text.index(SAGE_FAST_MARKER_ACT)
                else:
                    marker, at = "NONE", -1
                locked = detect_mutation_intent(query)
                as_model = _run_gate(text, mutation_locked=locked, asserted=False)
                as_prod = _run_gate(text, mutation_locked=locked,
                                    asserted=server_asserts_marker(scope, model))
                results.append({"model": model, "case": label, "scope": scope,
                                "run": i, "marker": marker, "at": at,
                                "chars": len(text),
                                "marker_owner": "orb" if asserts else "model",
                                "gate_model_owned": as_model,
                                "gate_production": as_prod})
                print(f"{model:<14} {label:<22} {marker:<8} {at:>5} "
                      f"{_gate_label(as_model):>12} {_gate_label(as_prod):>17}  {head}")
    print("-" * 118)

    for model in (THINKING_FAST_MODEL, LLM_MODEL):
        rows = [r for r in results if r["model"] == model]
        ok = sum(1 for r in rows if r["marker"] != "NONE")
        was = sum(1 for r in rows if r["gate_model_owned"]["streamed"])
        now = sum(1 for r in rows if r["gate_production"]["streamed"])
        owners = sorted({r["marker_owner"] for r in rows})
        print(f"{model}: contract marker present in {ok}/{len(rows)} responses; "
              f"marker owned by {'/'.join(o.upper() for o in owners)}")
        print(f"{'':<{len(model)}}  gate as the model owned it: streams {was}/{len(rows)}"
              f"{'' if was == len(rows) else '  => the rest arrive in one lump at the end of the turn'}")
        print(f"{'':<{len(model)}}  gate as production runs it: streams {now}/{len(rows)}")

    out = Path(__file__).resolve().parent.parent / "ops" / "fast_model_contract_check.json"
    out.parent.mkdir(exist_ok=True)
    # KEEP THE PREVIOUS RUN. This file is the "before" the next change will be
    # argued against, and it was very nearly lost on 2026-09-07 by re-running
    # this script: a fixed output path means every run destroys the baseline it
    # is being compared to. The stable name still points at the latest run.
    if out.exists():
        stamp = datetime.fromtimestamp(out.stat().st_mtime).strftime("%Y-%m-%d_%H%M")
        keep = out.with_name(f"{out.stem}_{stamp}{out.suffix}")
        if not keep.exists():
            keep.write_text(out.read_text())
            print(f"kept previous run as {keep.name}")
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
