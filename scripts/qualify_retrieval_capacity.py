"""Bounded full-RAG/fake-LLM qualification with owned-fixture memory protection."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from typing import Any

import loadtest_capacity as capacity
from qualify_ollama_serving import pressure_abort_reason, vm_counters
from serve_ollama_candidate import GIB, inspect_memory


async def qualify(arguments: list[str], output: Path) -> int:
    args = capacity.parser().parse_args(arguments)
    capacity.validate_safety(args, None)
    if not args.orb_stub or not args.embedding_model or args.profile_fixture:
        raise ValueError("This guard requires clean --orb-stub with both installed retrieval snapshots")
    output.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"scope": "system_wide_full_RAG_fake_LLM_only", "samples": [],
                              "maximum_elapsed_s": 300, "min_available_estimate_bytes": 12 * GIB}
    initial = await asyncio.to_thread(inspect_memory)
    report["initial_memory"] = asdict(initial)
    if initial.estimated_available_bytes < 12 * GIB or initial.free_percentage < 20:
        report["outcome"] = "refused_insufficient_preflight_headroom"
        output.write_text(json.dumps(report, indent=2) + "\n")
        return 2
    task = asyncio.create_task(capacity.main_async(args))
    started = time.monotonic()
    low = 0
    code = 2
    try:
        while not task.done():
            done, _pending = await asyncio.wait((task,), timeout=2)
            if done:
                break
            current = await asyncio.to_thread(inspect_memory)
            low = low + 1 if current.free_percentage < 15 else 0
            report["samples"].append({"elapsed_s": time.monotonic() - started, **asdict(current), **vm_counters(current)})
            reason = pressure_abort_reason(initial, current, low)
            if reason or time.monotonic() - started > 300:
                report["outcome"] = reason or "bounded_elapsed_limit"
                task.cancel()
                break
        if not task.cancelled() and not task.cancelling():
            code = await task
            report["outcome"] = "completed" if code in (0, 1) else "runner_refused"
            report["runner_exit_code"] = code
    except (OSError, RuntimeError, ValueError) as error:
        report["outcome"] = "startup_or_runner_refused"
        report["error_type"] = type(error).__name__
        # Capacity RuntimeErrors contain only our fixed stage/exception labels.
        message = str(error)
        if message.startswith("Disposable Orb"):
            report["safe_startup_diagnostic"] = message
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        final = await asyncio.to_thread(inspect_memory)
        report["final_memory"] = asdict(final)
        report["elapsed_s"] = time.monotonic() - started
        report["cleanup"] = "Capacity context cancelled/awaited; only its owned disposable process group is terminated"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"memory_guard_report": str(output), "outcome": report.get("outcome"),
                          "initial_os_free_percentage": initial.free_percentage,
                          "final_os_free_percentage": final.free_percentage}), flush=True)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("capacity_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.capacity_args[1:] if args.capacity_args[:1] == ["--"] else args.capacity_args
    try:
        return asyncio.run(qualify(arguments, args.output))
    except (ValueError, OSError) as error:
        print(f"Qualification refused: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
