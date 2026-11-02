#!/usr/bin/env python3
"""Capture actual full-RAG Sage inputs while generation is a labelled stub.

The real disposable Orb/auth/retrieval route builds the messages. A synthetic
Ollama-shaped response merely lets that route finish. No clinical language
model is invoked; this procedure establishes neither latency nor accuracy.
Every listener/process belongs to this run, and no live database is opened.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import loadtest_capacity as capacity
import mlx_ollama_benchmark_proxy as proxy
import qualify_ollama_serving as qualification
from capacity_retrieval_fixture import retrieval_integrity
from loadtest_capacity_fixture import create_mock_ollama
from ollama_template_oracle import captured_chats

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_KIND = "synthetic_stub_for_input_capture_only"


def write_private(path: Path, data: dict[str, Any], *, replace_owned: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if replace_owned else os.O_EXCL)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        stream.write(proxy.canonical(data) + b"\n")


def attest_stub_capture(app: Any, directory: Path) -> None:
    """Add an explicit origin label before the new owned proxy accepts traffic."""
    additions = {"upstream_kind": UPSTREAM_KIND, "clinical_model_invoked": False,
                 "clinical_model_not_invoked": True, "model_outputs_are_synthetic_stub": True,
                 "latency_acceptance_evidence": False, "clinical_accuracy_evidence": False,
                 "upstream_protocol_version": "synthetic-fixture"}
    app.state.metadata.update(additions)
    write_private(directory / "proxy-metadata.json", app.state.metadata, replace_owned=True)


@asynccontextmanager
async def local_server(app: Any):
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(256)
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        if not server.started:
            raise RuntimeError("Owned synthetic capture server did not start")
        yield origin
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            sock.close()


def capture_proof(path: Path, expected_inputs: int = 3) -> dict[str, Any]:
    rows, provenance = captured_chats(path)
    if (expected_inputs not in {3, 200} or len(rows) != expected_inputs
            or len({row["body_sha256"] for row in rows}) != expected_inputs
            or any(row["body"].get("options", {}).get("num_ctx") != 8192
                   or row["body"].get("options", {}).get("temperature") not in {.2, .4} for row in rows)):
        raise ValueError("Capture did not contain the declared number of distinct original clinical-scope inputs")
    metadata = json.loads((path.parent / "proxy-metadata.json").read_text())
    if (metadata.get("upstream_kind") != UPSTREAM_KIND or metadata.get("clinical_model_invoked") is not False
            or metadata.get("clinical_model_not_invoked") is not True):
        raise ValueError("Synthetic generation origin was not explicitly attested")
    return {"captured_inputs": len(rows), "source": UPSTREAM_KIND, "clinical_model_invoked": False,
            "provenance": provenance,
            "inputs": [{"request_id": row["request_id"], "body_sha256": row["body_sha256"],
                        "options": row["body"]["options"]} for row in rows]}


async def capture_inputs(args: argparse.Namespace, report: dict[str, Any]) -> None:
    stub = create_mock_ollama()
    async with local_server(stub) as stub_origin:
        capture_dir = args.output_dir / "capture"
        app = proxy.create_app(backend="ollama", upstream=stub_origin, capture_dir=capture_dir,
                               synthetic_data_only=True)
        attest_stub_capture(app, capture_dir)
        async with local_server(app) as proxy_origin:
            settings = qualification.fixture_settings(1)
            settings["ORB_SAGE_RETRIEVAL_WORKERS"] = "4"
            async with capacity.orb_stub_target(settings, proxy_origin,
                    embedding_model=str(args.embedding_model), reranker_model=str(args.reranker_model)) as (base, manifest, _):
                if manifest.get("synthetic_data_only") is not True:
                    raise ValueError("Disposable fixture did not attest synthetic records")
                summary, rows = await capacity.run_burst(base, manifest, mode="fresh-ai", concurrency=1,
                    requests=args.requests, timeout=30, require_model=True, run_id=uuid.uuid4().hex[:12])
                # Do not save fake-model SLA counts/quantiles as performance
                # evidence. Only route completion and input provenance matter.
                report["stub_route_observations"] = [{"lane": row["lane"],
                    "stub_route_completed": row["success"], "error": row["error"]} for row in rows]
                if summary["requests"] != args.requests or summary["failed"] != 0:
                    raise ValueError("The declared synthetic capture routes did not finish")
                async with httpx.AsyncClient(trust_env=False) as client:
                    marker = await client.get(base + "/__capacity_fixture__", timeout=2)
                    marker.raise_for_status()
                    metadata = marker.json()
                report["full_retrieval_fixture"] = {key: metadata.get(key) for key in (
                    "copied_source_sha256", "database_encryption", "full_retrieval_proof",
                    "retrieval_runtime", "retrieval_executor")}
                proof = retrieval_integrity(metadata.get("full_retrieval_proof"), metadata.get("retrieval_runtime"))
                report["full_retrieval_integrity"] = proof
                runtime = metadata.get("retrieval_runtime", {})
                if (not proof["passed"] or not proof["required"]
                        or runtime.get("embedding_calls", -1) != runtime.get("embedding_completed", -2)
                        or runtime.get("rerank_calls", -1) != runtime.get("rerank_completed", -2)
                        or metadata.get("retrieval_executor", {}).get("workers") != 4):
                    raise ValueError("Full retrieval integrity or executor width was not verified")
            path = capture_dir / "requests.jsonl"
            captured, _ = captured_chats(path)
            if len(captured) != len(rows):
                raise ValueError("Cannot match captured bodies to the sequential Sage route results")
            # Input building is deliberately sequential, so captured provider
            # calls match these API results in order. Keep route IDs and lane
            # labels OUTSIDE the unchanged original model body/hash.
            for source, route in zip(captured, rows, strict=True):
                source.update(lane=route["lane"], source_route_request_id=route["request_id"],
                              lane_source="matched_sequential_original_Sage_route_result")
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_TRUNC), "wb") as stream:
                for source in captured:
                    stream.write(proxy.canonical(source) + b"\n")
            report["input_capture"] = capture_proof(path, args.requests)
            report["stub_counters"] = dict(stub.state.counters)
            if (stub.state.counters["chat_calls"] != args.requests
                    or stub.state.counters["unique_chat_prompts"] != args.requests):
                raise ValueError("Synthetic stub did not observe the declared distinct clinical-model-shaped inputs")


async def run(args: argparse.Namespace) -> int:
    if not args.allow_retrieval_model_load or not args.synthetic_data_only:
        raise ValueError("Explicit synthetic-only and local retrieval-model-load acknowledgements are required")
    if not 30 <= args.budget_seconds <= 300:
        raise ValueError("Capture window must be bounded to30..300seconds")
    if (not args.output_dir.is_absolute() or args.output_dir.exists() or args.output_dir.is_symlink()
            or not args.output_dir.parent.is_dir()):
        raise ValueError("Output must be a new absolute directory with an existing parent")
    for path in (args.embedding_model, args.reranker_model):
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Existing absolute local retrieval snapshots are required")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    args.output_dir.mkdir(mode=0o700)
    initial = await asyncio.to_thread(qualification.candidate.inspect_memory)
    profile: dict[str, Any] = {"preflight": {"memory": asdict(initial)}, "pressure_samples": []}
    report: dict[str, Any] = {"created_at": datetime.now(UTC).isoformat(),
        "scope": "actual_full_RAG_Sage_input_capture_with_explicit_synthetic_generation_stub",
        "status": "running", "synthetic_data_only": True, "upstream_kind": UPSTREAM_KIND,
        "clinical_model_invoked": False, "clinical_model_not_invoked": True,
        "planned_inputs": args.requests, "input_capture_concurrency": 1,
        "latency_acceptance_evidence": False, "clinical_accuracy_evidence": False,
        "hardware_runtime": capacity.hardware_metadata(), "memory_guard": profile,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": ["Stub answers are not clinical outputs or inference-throughput evidence.",
            "Input captures are suitable for exact engine replay after retrieval workers exit.",
            "Temporary unencrypted synthetic database; no real patient records or production qualification.",
            "Only loopback endpoints are used; external network denial is inherited from the caller's sandbox-exec policy."]}
    task: asyncio.Task[None] | None = None
    started, low = time.monotonic(), 0
    try:
        if initial.estimated_available_bytes < 12 * qualification.candidate.GIB or initial.free_percentage < 20:
            report["status"] = "not_run_insufficient_memory_preflight"
            return 2
        task = asyncio.create_task(capture_inputs(args, report))
        while not task.done():
            done, _ = await asyncio.wait((task,), timeout=2)
            if done:
                break
            current = await asyncio.to_thread(qualification.candidate.inspect_memory)
            low = low + 1 if current.free_percentage < 15 else 0
            profile["pressure_samples"].append({"elapsed_s": time.monotonic() - started,
                **asdict(current), **qualification.vm_counters(current)})
            reason = qualification.pressure_abort_reason(initial, current, low)
            if reason or time.monotonic() - started > args.budget_seconds:
                raise qualification.QualificationAborted(reason or "bounded_capture_window_expired")
        await task
        report["status"] = ("three_inputs_captured_full_retrieval_verified" if args.requests == 3
                            else "200_inputs_captured_full_retrieval_verified")
    except (qualification.QualificationAborted, OSError, RuntimeError, ValueError, httpx.HTTPError) as error:
        report.update(status="aborted_or_incomplete", error_type=type(error).__name__,
                      abort_reason=str(error)[:1024])
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        profile["post_memory"] = asdict(await asyncio.to_thread(qualification.candidate.inspect_memory))
        profile["system_pressure_summary"] = qualification.system_pressure_summary(profile)
        report["elapsed_s"] = time.monotonic() - started
        report["cleanup"] = "All owned fixture/server contexts awaited; no existing daemon controlled"
        write_private(args.output_dir / "report.json", report)
        print(json.dumps({"status": report["status"], "clinical_model_invoked": False,
                          "report": str(args.output_dir / "report.json")}), flush=True)
    return 0 if report["status"] in {"three_inputs_captured_full_retrieval_verified",
                                    "200_inputs_captured_full_retrieval_verified"} else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--embedding-model", type=Path, required=True)
    result.add_argument("--reranker-model", type=Path, required=True)
    result.add_argument("--output-dir", type=Path, required=True)
    result.add_argument("--budget-seconds", type=float, default=180)
    result.add_argument("--requests", type=int, choices=(3, 200), default=3,
                        help="Sequential input building only; stub timings never qualify inference or a200-request SLA")
    result.add_argument("--synthetic-data-only", action="store_true")
    result.add_argument("--allow-retrieval-model-load", action="store_true")
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
