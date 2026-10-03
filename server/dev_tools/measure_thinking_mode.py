"""Measure what thinking OFF actually buys: 4B vs 27B on REAL Orb prompts.

Not a synthetic benchmark. For each question this script runs Orb's own
retrieval (`sage_service.get_relevant_context`), builds Orb's own chat messages
(`sage_service._build_chat_messages`) with Orb's own per-scope sampling options
(`SCOPE_CONFIGS`), and then streams that identical payload to both tiers. The
only variable between the two columns is the model name.

Three numbers per run, because they mean different things to a clinician:

  retrieval_ms   guideline/chart assembly. Paid once per turn, identical for
                 both models — it is not a model property, but it IS part of
                 what the clinician waits for, so it is reported separately
                 rather than hidden inside the total.
  ttft_ms        first token out of Ollama.
  first_words_ms FIRST TEXT THE CLINICIAN CAN READ. This is the honest number.
                 It runs the real streaming gate from routes/sage_routes.py
                 (MarkerAnswerStreamer / SentenceStreamSanitizer), which holds
                 output back until the answer marker is known — so a model that
                 emits its first token quickly but takes 30 tokens to reach the
                 answer does not get credit for it.

                 WHO OWNS THAT MARKER is now a per-model question, and this
                 script applies the production policy
                 (sage_service.server_asserts_marker) rather than assuming the
                 model emits one. Before 2026-09-07 it did assume that, and the
                 4B's three rows came back `first_words_ms: null` with
                 `gated_runs: 3` — nothing ever streamed, because the gate was
                 waiting for a marker that model has never once emitted. Keeping
                 this in step matters: a harness measuring a gate production no
                 longer runs would have gone on reporting nulls after the lump
                 was fixed.

  wall_ms        retrieval_ms + first_words_ms — what the clinician actually
                 counts from pressing Enter.

Usage (env is required — this imports config directly):
    cd server && set -a && . ../.env && set +a && \
        .venv/bin/python dev_tools/measure_thinking_mode.py
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
from datetime import datetime
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from config import LLM_MODEL, OLLAMA_BASE_URL, THINKING_FAST_MODEL  # noqa: E402

CAREFUL = LLM_MODEL
FAST = THINKING_FAST_MODEL if THINKING_FAST_MODEL != LLM_MODEL else "medgemma:4b"

REPEATS = 3


def _pick_user_and_patient():
    """A real doctor and a real patient inside that doctor's department."""
    from database import db_session

    with db_session() as db:
        user = dict(db.execute(
            "SELECT id, name, role, department FROM users "
            "WHERE role = 'doctor' AND department IS NOT NULL LIMIT 1"
        ).fetchone())
        row = db.execute(
            "SELECT p.id, p.name, p.diagnosis FROM patients p "
            "JOIN admissions a ON a.patient_id = p.id AND a.status = 'active' "
            "WHERE a.department = ? LIMIT 1",
            (user["department"],),
        ).fetchone()
        patient = dict(row) if row else None
    return user, patient


async def _stream_once(model: str, messages: list[dict], options: dict, scope: str,
                       query: str) -> dict:
    """Stream one turn and time it through Orb's real client-visible gate."""
    from routes.sage_routes import (
        MarkerAnswerStreamer,
        SentenceStreamSanitizer,
        XmlAnswerStreamer,
        _strip_forbidden_streaming,
    )
    from services.sage_service import (
        _xml_contract_forced,
        detect_mutation_intent,
        server_asserts_marker,
    )

    prose_scope = scope in ("broad", "aggregate", "quick")
    sanitizer = SentenceStreamSanitizer(_strip_forbidden_streaming)
    streamer_cls = XmlAnswerStreamer if _xml_contract_forced() else MarkerAnswerStreamer
    gate = streamer_cls(
        SentenceStreamSanitizer(_strip_forbidden_streaming),
        mutation_locked=detect_mutation_intent(query),
    )
    # The gate is per-MODEL, not per-scope: on a model Orb asserts the marker
    # for, the route calls this before the first chunk. Without it this script
    # would measure a gate production no longer runs, and would keep reporting
    # `first_words_ms: null` for the fast tier after the lump was fixed.
    if not prose_scope and server_asserts_marker(scope, model):
        gate.assert_marker()

    t0 = time.perf_counter()
    ttft = None
    first_words = None
    chunks = 0
    text = ""

    async with httpx.AsyncClient(
        # 180 s, not 600: a hung scheduler should surface in three minutes, not
        # burn ten. Nothing here legitimately takes longer once the tier is
        # already loaded (the load itself happens in `_load`).
        timeout=httpx.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0),
        trust_env=False,
    ) as client:
        async with client.stream(
            "POST", f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": model, "messages": messages, "stream": True,
                "keep_alive": "1h",
                "options": {
                    "temperature": options["temperature"],
                    "repeat_penalty": 1.15, "repeat_last_n": 256,
                    "num_predict": options["num_predict"],
                    "num_ctx": options["num_ctx"],
                },
            },
        ) as resp:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = (data.get("message") or {}).get("content")
                if piece:
                    chunks += 1
                    text += piece
                    if ttft is None:
                        ttft = (time.perf_counter() - t0) * 1000
                    if first_words is None:
                        visible = sanitizer.feed(piece) if prose_scope else gate.feed(piece)
                        if visible and visible.strip():
                            first_words = (time.perf_counter() - t0) * 1000
                if data.get("done"):
                    break
    total = (time.perf_counter() - t0) * 1000
    return {
        "ttft_ms": ttft, "first_words_ms": first_words, "total_ms": total,
        "chunks": chunks, "chars": len(text), "preview": text[:110].replace("\n", " "),
    }


async def _load(model: str, attempts: int = 4) -> float:
    """Cold-load `model` with a 1-token request. Returns wall ms.

    RETRIES ON 500. Measured 2026-08-05: asking for the 27B in the seconds
    immediately after releasing the 4B returns `500 Internal Server Error` from
    /api/chat while Ollama's scheduler is still tearing the previous runner
    down. It clears on its own within ~30 s — it is a narrower version of the
    same swap hazard `_unload` documents, and it is the reason a two-tier
    appliance must not treat a single 500 from Ollama as "the model is gone".
    """
    t = time.perf_counter()
    last: Exception | None = None
    for i in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
                r = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json={
                    "model": model, "messages": [{"role": "user", "content": "hi"}],
                    "stream": False, "keep_alive": "10m", "options": {"num_predict": 1},
                })
                r.raise_for_status()
            return (time.perf_counter() - t) * 1000
        except Exception as exc:  # noqa: BLE001 - transient scheduler states
            last = exc
            print(f"   ! load {model} attempt {i + 1}/{attempts} failed ({exc}); "
                  f"retrying in {10 * (i + 1)} s", flush=True)
            await asyncio.sleep(10 * (i + 1))
    raise RuntimeError(f"could not load {model} after {attempts} attempts") from last


async def _unload(model: str) -> None:
    """Explicitly release `model` (keep_alive=0).

    THE MEASUREMENT DEADLOCKED WITHOUT THIS (2026-08-05). The first version of
    this script asked for the fast tier while a concurrently-running Orb server
    held the 27B PINNED (keep_alive=-1, ORB_OLLAMA_KEEP_ALIVE). Ollama's
    scheduler wedged trying to make room: the runner went idle, /api/ps kept
    answering, and every /api/chat — including for the already-resident 27B —
    hung indefinitely. It took a restart of `ollama serve` to clear.

    So the tiers are measured ONE AT A TIME, with an explicit release between
    them, and never by asking the scheduler to swap under pressure. This is a
    real operational hazard for a two-tier deployment, not a quirk of the
    harness — see the note in the report.
    """
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        try:
            await client.post(f"{OLLAMA_BASE_URL}/api/chat", json={
                "model": model, "messages": [], "keep_alive": 0, "stream": False,
            })
        except Exception:
            pass


async def main() -> None:
    from services.sage_service import (
        SCOPE_CONFIGS,
        _build_chat_messages,
        get_relevant_context,
    )

    user, patient = _pick_user_and_patient()
    if not patient:
        print("No active patient in this doctor's department — cannot run (c).")
        return

    cases = [
        ("a. simple definition",  "What is hypoxia?", None),
        ("b. general clinical",   "What are the diagnostic criteria for ARDS?", None),
        ("c. patient question",   f"Summarise {patient['name']}'s current status "
                                  f"and what is driving it.", patient["id"]),
    ]

    print(f"careful tier = {CAREFUL}   fast tier = {FAST}", flush=True)
    print(f"user = {user['name']} ({user['department']})   "
          f"patient = {patient['name']} — {patient['diagnosis']}", flush=True)
    print(f"repeats = {REPEATS} measured runs per cell (plus one discarded warm-up)\n",
          flush=True)

    # ── Build every prompt ONCE, through Orb's real retrieval ──
    # Shared by both tiers, so the only variable in the comparison is the model.
    prepared = []
    for label, query, patient_id in cases:
        t_r = time.perf_counter()
        (context, scope, _sources, _conf, _esc, _pcd, qscope) = await asyncio.to_thread(
            get_relevant_context, query, user, patient_id, [])
        retrieval_ms = (time.perf_counter() - t_r) * 1000
        cfg = SCOPE_CONFIGS.get(scope, SCOPE_CONFIGS["simple"])
        prepared.append({
            "label": label, "query": query, "scope": scope, "query_scope": qscope,
            "retrieval_ms": retrieval_ms,
            "options": {"temperature": cfg.get("temperature", 0.4),
                        "num_predict": cfg["num_predict"], "num_ctx": cfg["num_ctx"]},
            "messages": _build_chat_messages(query, context, [], scope),
        })
        prompt_tok = sum(len(m.get("content") or "")
                         for m in prepared[-1]["messages"]) // 4
        print(f"── {label}: {query!r}", flush=True)
        print(f"   scope={scope} query_scope={qscope} "
              f"retrieval={retrieval_ms:.0f} ms prompt≈{prompt_tok} tok", flush=True)
    print(flush=True)

    results = []
    # Model OUTER, case inner: one load per tier for the whole run.
    for model in (FAST, CAREFUL):
        load_ms = await _load(model)
        print(f"\n### {model} — cold load {load_ms:.0f} ms", flush=True)
        for p in prepared:
            warm = await _stream_once(model, p["messages"], p["options"],
                                      p["scope"], p["query"])
            runs = [await _stream_once(model, p["messages"], p["options"],
                                       p["scope"], p["query"])
                    for _ in range(REPEATS)]

            def med(key, rows=runs):
                vals = [r[key] for r in rows if r[key] is not None]
                return statistics.median(vals) if vals else None

            row = {
                "case": p["label"], "model": model, "scope": p["scope"],
                "retrieval_ms": p["retrieval_ms"], "cold_load_ms": load_ms,
                "cold_ttft_ms": warm["ttft_ms"],
                "ttft_ms": med("ttft_ms"),
                "first_words_ms": med("first_words_ms"),
                "total_ms": med("total_ms"),
                "gated_runs": sum(1 for r in runs if r["first_words_ms"] is None),
                "preview": runs[0]["preview"],
            }
            # WHAT THE CLINICIAN ACTUALLY WAITS FOR.
            #
            # When the streaming gate never opens — the model never emitted the
            # `NOACT|`/`ACT|` marker, which is FAIL-CLOSED by design — the route
            # does not show nothing: the buffered end-of-turn path releases the
            # whole answer at once. So the honest first-words time for a
            # non-compliant model is its time to its LAST token, and reporting a
            # blank here would flatter it enormously. Measured 2026-08-05:
            # medgemma:4b emits no marker on any Orb prompt, so this fallback is
            # the ONLY number that describes it.
            row["streamed"] = row["first_words_ms"] is not None
            row["effective_first_words_ms"] = (
                row["first_words_ms"] if row["streamed"] else row["total_ms"])
            row["wall_ms"] = (p["retrieval_ms"] + row["effective_first_words_ms"]
                              if row["effective_first_words_ms"] is not None else None)
            results.append(row)
            fw = f"{row['first_words_ms']:.0f}" if row["first_words_ms"] else "never"
            print(f"   {p['label']:<22} ttft={row['ttft_ms']:.0f} ms  "
                  f"first_words={fw} ms  total={row['total_ms']:.0f} ms", flush=True)
            print(f"        > {row['preview']}", flush=True)
        # Release before touching the other tier — see _unload's docstring.
        await _unload(model)

    print("\n" + "=" * 116, flush=True)
    print(f"{'case':<22} {'model':<14} {'retrieval':>10} {'ttft':>8} {'gate':>8} "
          f"{'total':>9} {'CLINICIAN SEES':>15} {'<3s?':>6}")
    print("-" * 116)
    for r in sorted(results, key=lambda x: (x["case"], x["model"])):
        wall = r["wall_ms"]
        gate = f"{r['first_words_ms']:.0f}m" if r["streamed"] else "never"
        print(f"{r['case']:<22} {r['model']:<14} {r['retrieval_ms']:>9.0f}m "
              f"{r['ttft_ms']:>7.0f}m {gate:>8} {r['total_ms']:>8.0f}m "
              f"{(wall or 0) / 1000:>14.2f}s "
              f"{('YES' if wall and wall < 3000 else 'NO'):>6}")
    print("=" * 116)
    print("gate 'never' = nothing was released before the end of the turn, so the")
    print("clinician's first word is this row's 'total'. Before 2026-09-07 that was")
    print("every fast-tier row, because the gate was waiting for a marker medgemma:4b")
    print("has never emitted; Orb now asserts that marker itself for such a model")
    print("(sage_service.server_asserts_marker), and this script applies the same")
    print("policy, so a 'never' here again would be a real regression.", flush=True)

    out = Path(__file__).resolve().parent.parent / "ops" / "thinking_mode_measurements.json"
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
