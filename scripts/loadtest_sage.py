#!/usr/bin/env python3
"""
Orb Hospital OS — Concurrency load test (A6)
============================================================================
Measures the "N concurrent clinicians" ceiling the pitch needs.

WHAT THIS MEASURES vs. WHAT IT CANNOT
-------------------------------------
The clinically interesting number is "how many clinicians can hit Sage (the
MedGemma/Ollama LLM) at once before latency blows up". That number is bounded by
TWO server-side admission gates, not by request volume:

  * services/llm_queue_service.py  — `PriorityLLMQueue.max_concurrent = 1`.
    Every Sage/Scribe LLM request serializes through a SINGLE slot; the rest
    wait in a priority queue. So LLM throughput is 1-in-flight BY DESIGN.
  * services/gpu_coordinator.py    — a process-wide `asyncio.Lock` (the GPU
    admission gate) shared by the Ollama lane AND the in-process ML lane, so at
    most ONE GPU job (LLM *or* ML) runs at a time across the whole process.

Consequence: the real Sage latency-under-load number is GPU-bound and requires a
GPU host with Ollama/MedGemma actually running. Ollama is NOT available in this
environment, so hitting /api/sage here would just measure the queue timing out.

WHAT WE DO INSTEAD
------------------
This harness measures the *server + queue concurrency ceiling* — the HTTP/ASGI
layer (uvicorn + FastAPI + SQLite) that every clinician session shares before it
ever reaches the GPU gate. We ramp concurrency (1,2,4,8,16,32,...) against a real
REST endpoint and report p50/p95 latency, throughput, and the max sustained
concurrency before errors appear. That is the "how many concurrent clinician
sessions can the server itself service" number, and it is the honest, reproducible
half of the story. The LLM-bound half is explicitly a single-slot gate (above).

Two target modes:
  --target login   (default) POST /api/auth/login  — write-ish path, exercises
                   auth + SQLite + password hashing per request.
  --target read    GET a cheap authenticated read endpoint (patients list).
  --target sage    POST /api/sage (LLM path). Included for completeness; will
                   time out / error without a GPU + Ollama. Demonstrates the
                   single-slot gate: throughput stays ~flat as concurrency rises.

USAGE
-----
  python scripts/loadtest_sage.py                       # login path, default ramp
  python scripts/loadtest_sage.py --target read
  python scripts/loadtest_sage.py --levels 1,2,4,8,16,32,64 --requests 40
  python scripts/loadtest_sage.py --base-url http://localhost:8000

Requires: httpx (already in the dev venv). Backend must be running on :8000.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time

try:
    import httpx
except ImportError:  # pragma: no cover
    print("httpx is required. Activate the dev venv: . server/.venv-dev/bin/activate")
    sys.exit(1)


DEFAULT_BASE_URL = "http://localhost:8000"
# Demo clinician from server/scripts/seed_clinical_data.py (password "password").
DEMO_ID = "DOC001"
DEMO_PW = "password"


async def _login(client: httpx.AsyncClient, base_url: str) -> str:
    r = await client.post(
        f"{base_url}/api/auth/login",
        json={"employee_id": DEMO_ID, "password": DEMO_PW},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["token"]


async def _one_login(client: httpx.AsyncClient, base_url: str) -> tuple[float, bool]:
    """One login request. Returns (latency_seconds, ok)."""
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"{base_url}/api/auth/login",
            json={"employee_id": DEMO_ID, "password": DEMO_PW},
            timeout=30.0,
        )
        ok = r.status_code == 200
    except Exception:
        ok = False
    return time.perf_counter() - t0, ok


async def _one_read(client: httpx.AsyncClient, base_url: str, token: str) -> tuple[float, bool]:
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{base_url}/api/vigil/patients",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        ok = r.status_code == 200
    except Exception:
        ok = False
    return time.perf_counter() - t0, ok


# A general medical question: no patient scope, so the harness measures the
# model path without dragging patient-context retrieval into the TTFT clock.
SAGE_QUERY = "What are the common side effects of metoprolol?"


async def _one_sage(
    client: httpx.AsyncClient, base_url: str, token: str, query: str = SAGE_QUERY
) -> tuple[float, float | None, bool]:
    """Stream /api/sage/query and record TTFT (time to first STREAMED content
    event) separately from total latency. Returns (total_s, ttft_s|None, ok).

    /api/sage/query is an SSE stream, so a buffered client.post would only ever
    see total time. We open a streaming request and stamp the clock on the
    first `data:` frame that carries a content delta — that is the number the
    'begins answering in under 2 seconds' promise is about."""
    t0 = time.perf_counter()
    ttft = None
    ok = False
    try:
        async with client.stream(
            "POST",
            f"{base_url}/api/sage/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": query, "patient_id": None},
            timeout=120.0,
        ) as r:
            ok = r.status_code == 200
            if ok:
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        data = json.loads(payload)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    # First visible content delta = time to first token.
                    if ttft is None and data.get("content"):
                        ttft = time.perf_counter() - t0
                    if data.get("done"):
                        break
    except Exception:
        ok = False
    return time.perf_counter() - t0, ttft, ok


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


async def run_level(
    base_url: str, target: str, concurrency: int, requests: int, token: str,
) -> dict:
    """Fire `requests` calls with at most `concurrency` in flight."""
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    ttfts: list[float] = []
    errors = 0

    limits = httpx.Limits(max_connections=concurrency + 4, max_keepalive_connections=concurrency + 4)
    async with httpx.AsyncClient(limits=limits) as client:

        async def worker():
            nonlocal errors
            async with sem:
                if target == "login":
                    lat, ok = await _one_login(client, base_url)
                elif target == "read":
                    lat, ok = await _one_read(client, base_url, token)
                else:  # sage — streaming, records TTFT
                    lat, ttft, ok = await _one_sage(client, base_url, token)
                    if ttft is not None:
                        ttfts.append(ttft)
                latencies.append(lat)
                if not ok:
                    errors += 1

        wall0 = time.perf_counter()
        await asyncio.gather(*[worker() for _ in range(requests)])
        wall = time.perf_counter() - wall0

    latencies.sort()
    ttfts.sort()

    return {
        "concurrency": concurrency,
        "requests": requests,
        "errors": errors,
        "wall_s": wall,
        "throughput": requests / wall if wall > 0 else float("nan"),
        "p50_ms": _pct(latencies, 50) * 1000,
        "p95_ms": _pct(latencies, 95) * 1000,
        "mean_ms": (statistics.mean(latencies) * 1000) if latencies else float("nan"),
        "max_ms": (latencies[-1] * 1000) if latencies else float("nan"),
        "ttft_p50_ms": (_pct(ttfts, 50) * 1000) if ttfts else float("nan"),
        "ttft_p95_ms": (_pct(ttfts, 95) * 1000) if ttfts else float("nan"),
        "ttft_samples": len(ttfts),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="Orb concurrency load test")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--target", choices=["login", "read", "sage"], default="login")
    ap.add_argument("--levels", default="1,2,4,8,16,32",
                    help="comma-separated concurrency levels")
    ap.add_argument("--requests", type=int, default=None,
                    help="requests per level (default: max(4*concurrency, 32))")
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    print("=" * 78)
    print("Orb Hospital OS — concurrency load test")
    print(f"  target      : {args.target}")
    print(f"  base URL    : {args.base_url}")
    print(f"  levels      : {levels}")
    print("=" * 78)
    if args.target == "sage":
        print(f"  sage query  : {SAGE_QUERY!r}")
        print("-" * 78)
        print("NOTE: --target sage measures TIME TO FIRST TOKEN (ttft), not just total")
        print("      latency, by streaming the SSE response and stamping the clock on the")
        print("      first content delta. Every Sage turn runs on the LOCAL model and")
        print("      serializes through llm_queue.max_concurrent=1 behind the GPU gate, so")
        print("      TTFT at concurrency N includes the wait behind N-1 queued turns.")
        print("      Without Ollama running, requests error — that is the gate, not a bug.")
        print("-" * 78)

    # A token for authenticated targets.
    token = ""
    async with httpx.AsyncClient() as client:
        try:
            token = await _login(client, args.base_url)
        except Exception as e:
            print(f"FATAL: could not log in as {DEMO_ID} at {args.base_url}: {e}")
            print("Is the backend running and seeded? See e2e/README.md.")
            return 2

    is_sage = args.target == "sage"
    if is_sage:
        header = (
            f"{'conc':>5} {'reqs':>5} {'errs':>5} "
            f"{'ttftP50':>9} {'ttftP95':>9} {'totP50':>9} {'totP95':>9} "
            f"{'req/s':>8} {'wall s':>7}"
        )
    else:
        header = (
            f"{'conc':>5} {'reqs':>5} {'errs':>5} "
            f"{'p50 ms':>9} {'p95 ms':>9} {'mean ms':>9} {'max ms':>9} "
            f"{'req/s':>8} {'wall s':>7}"
        )
    print(header)
    print("-" * len(header))

    ceiling = None
    sub2s_ceiling = None
    for c in levels:
        reqs = args.requests if args.requests else max(4 * c, 32)
        res = await run_level(args.base_url, args.target, c, reqs, token)
        if is_sage:
            print(
                f"{res['concurrency']:>5} {res['requests']:>5} {res['errors']:>5} "
                f"{res['ttft_p50_ms']:>9.1f} {res['ttft_p95_ms']:>9.1f} "
                f"{res['p50_ms']:>9.1f} {res['p95_ms']:>9.1f} "
                f"{res['throughput']:>8.1f} {res['wall_s']:>7.2f}"
            )
            # The sub-2s bar is about p95 TTFT (time to first streamed content).
            if res["errors"] == 0 and res["ttft_samples"] > 0 and res["ttft_p95_ms"] < 2000:
                sub2s_ceiling = c
        else:
            print(
                f"{res['concurrency']:>5} {res['requests']:>5} {res['errors']:>5} "
                f"{res['p50_ms']:>9.1f} {res['p95_ms']:>9.1f} {res['mean_ms']:>9.1f} "
                f"{res['max_ms']:>9.1f} {res['throughput']:>8.1f} {res['wall_s']:>7.2f}"
            )
        if res["errors"] == 0:
            ceiling = c

    print("-" * len(header))
    if is_sage:
        if sub2s_ceiling is not None:
            print(f"Max concurrency holding p95 TTFT < 2s: {sub2s_ceiling}")
        else:
            print("No level held p95 TTFT < 2s (or no TTFT samples — Ollama not running).")
        print()
        print("Interpretation:")
        print("  * ttftP50/P95 = time to the FIRST streamed content event — the number the")
        print("    'begins answering in under 2 seconds' claim is about.")
        print("  * TTFT degrades with concurrency by design: one local GPU, one LLM slot")
        print("    (llm_queue.max_concurrent=1), so turn N waits behind turns 1..N-1.")
    else:
        if ceiling is not None:
            print(f"Max sustained concurrency with ZERO errors: {ceiling}")
        else:
            print("No error-free level observed — the server errored even at the lowest concurrency.")
        print()
        print("Interpretation:")
        print("  * This is the SERVER/ASGI concurrency ceiling (uvicorn+FastAPI+SQLite),")
        print("    i.e. how many concurrent clinician SESSIONS the server itself can service.")
        print("  * The Sage/LLM latency-under-load number is SEPARATE: use --target sage")
        print("    to measure TTFT on the local lane (llm_queue.max_concurrent=1 + GPU gate).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
