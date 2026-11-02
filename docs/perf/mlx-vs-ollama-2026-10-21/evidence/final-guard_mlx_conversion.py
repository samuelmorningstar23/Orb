#!/usr/bin/env python3
"""Bounded conversion supervisor; no model inference or existing-service control."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import benchmark_mlx_ollama as benchmark
import qualify_ollama_serving as qualification
from convert_medgemma_gguf_mlx import QUANTISATION_PROFILES, candidate_label

ROOT = Path(__file__).resolve().parents[1]


async def run(args: argparse.Namespace) -> int:
    # Preserve the original affine8 default for programmatic Namespace callers
    # while making every other conversion profile an explicit choice.
    quantisation_profile = getattr(args, "quantisation_profile", "affine8")
    label = candidate_label(quantisation_profile)
    if args.output_dir.exists() or args.report.exists() or args.log.exists():
        raise ValueError("Conversion output, log and report must all be new")
    if not all(path.is_absolute() and path.parent.is_dir()
               for path in (args.output_dir, args.report, args.log)):
        raise ValueError("Use absolute outputs with existing parents")
    initial = benchmark.candidate.inspect_memory()
    command = [str(args.python), str(ROOT / "scripts/convert_medgemma_gguf_mlx.py"),
        "--source-gguf", str(args.source), "--ollama-models-dir", str(Path.home() / ".ollama/models"),
        "--output-dir", str(args.output_dir), "--max-dequant-bytes", str(64 * 1024 ** 2),
        "--quantisation-profile", quantisation_profile, "--execute", "--acknowledge-requantized-candidate"]
    profile = {"preflight": {"memory": asdict(initial)}, "pressure_samples": []}
    report = {"purpose": f"offline_bounded_{quantisation_profile.replace('-', '_')}_candidate_conversion_not_inference",
        "quantisation_profile": quantisation_profile, "candidate_label": label,
        "clinical_quality_qualified": False, "profile": profile, "command": command,
        "wall_budget_seconds": 900, "source_sha256_pin": benchmark.candidate.MODEL_DIGEST,
        "supervisor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "converter_sha256": hashlib.sha256((ROOT / "scripts/convert_medgemma_gguf_mlx.py").read_bytes()).hexdigest(),
        "status": "starting"}
    if quantisation_profile == "source-repack4-f16":
        report["repack_helper_sha256"] = hashlib.sha256((ROOT / "scripts/gguf_mlx_repack.py").read_bytes()).hexdigest()
    process = None
    monitor = waiter = None
    started = time.monotonic()
    try:
        with args.log.open("x") as log:
            # Retain the owned Popen handle; blocking wait runs in to_thread.
            process = subprocess.Popen(command, env=benchmark.inference_environment(benchmark.offline_environment(), "mlx"),  # noqa: ASYNC220
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            report["owned_pid"] = process.pid
            monitor = asyncio.create_task(qualification.monitor_pressure(initial, profile["pressure_samples"], process))
            waiter = asyncio.create_task(asyncio.to_thread(process.wait))
            async with asyncio.timeout(900):
                done, _ = await asyncio.wait((monitor, waiter), return_when=asyncio.FIRST_COMPLETED)
                if waiter in done:
                    code = await waiter
                    report.update(exit_code=code, status="complete" if code == 0 else "conversion_failed")
                else:
                    await monitor
    except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - Every failure must reach owned-child cleanup and an aborted report.
        report.update(status="aborted", error_type=type(error).__name__, error=str(error)[:1024])
    finally:
        await qualification.terminate_owned(process)
        for task in (monitor, waiter):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (monitor, waiter) if task is not None), return_exceptions=True)
        profile["post_memory"] = asdict(benchmark.candidate.inspect_memory())
        report.update(wall_seconds=time.monotonic() - started,
                      system_pressure_summary=qualification.system_pressure_summary(profile))
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"event": "conversion_finished", "status": report["status"],
                          "report": str(args.report)}), flush=True)
    return 0 if report["status"] == "complete" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--quantisation-profile", choices=QUANTISATION_PROFILES, default="affine8")
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
