#!/usr/bin/env python3
"""Bounded qualification of owned 1/2-slot Ollama serving with actual Orb.

All clinical records and answers are synthetic. No user database is opened,
no model is pulled and no existing model daemon is stopped or reconfigured.
Default is a plan only; execution requires both model-load acknowledgements.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import loadtest_capacity as capacity
import serve_ollama_candidate as candidate

ROOT = Path(__file__).resolve().parents[1]


class QualificationAborted(RuntimeError):
    """The owned run exceeded its resource or time budget."""


def vm_counters(snapshot: candidate.MemorySnapshot) -> dict[str, int]:
    page_size = re.search(r"page size of ([0-9]+) bytes", snapshot.vm_stat)
    if page_size is None:
        raise QualificationAborted("Cannot inspect page size.")
    output: dict[str, int] = {}
    for key, label in (("swapout_bytes", "Swapouts"),
                       ("compressed_bytes", "Pages occupied by compressor")):
        match = re.search(rf"^{re.escape(label)}:\s+([0-9]+)\.", snapshot.vm_stat, re.MULTILINE)
        if match is None:
            raise QualificationAborted("Cannot inspect pressure counters.")
        output[key] = int(match[1]) * int(page_size[1])
    return output


def pressure_abort_reason(initial: candidate.MemorySnapshot,
                          current: candidate.MemorySnapshot,
                          low_pressure_samples: int) -> str | None:
    """Conservative host protection; only this run's children may be stopped."""
    if current.free_percentage < 15 and low_pressure_samples >= 2:
        return "sustained_memory_pressure"
    before, after = vm_counters(initial), vm_counters(current)
    if after["swapout_bytes"] - before["swapout_bytes"] > 512 * 1024 ** 2:
        return "swap_growth_over_512_mib"
    if after["compressed_bytes"] - before["compressed_bytes"] > 4 * candidate.GIB:
        return "compression_growth_over_4_gib"
    return None


def system_pressure_summary(profile: dict[str, Any]) -> dict[str, object]:
    """Host-wide counters are not an individual daemon RSS/GPU measurement."""
    initial = profile["preflight"]["memory"]
    samples = [initial, *profile["pressure_samples"]]
    if "post_memory" in profile:
        samples.append(profile["post_memory"])
    counters = [vm_counters(candidate.MemorySnapshot(**{
        key: sample[key] for key in ("total_bytes", "free_percentage", "estimated_available_bytes", "vm_stat")
    })) for sample in samples]
    return {"scope": "system_wide_not_daemon_rss_or_gpu_peak", "samples": len(samples),
            "minimum_os_free_percentage": min(sample["free_percentage"] for sample in samples),
            "maximum_compressed_bytes": max(row["compressed_bytes"] for row in counters),
            "maximum_compression_growth_bytes": max(0, max(row["compressed_bytes"] for row in counters) - counters[0]["compressed_bytes"]),
            "swapout_growth_bytes": max(0, max(row["swapout_bytes"] for row in counters) - counters[0]["swapout_bytes"])}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fixture_settings(parallel: int) -> dict[str, str]:
    if parallel not in (1, 2):
        raise ValueError("Only measured candidate widths1/2 are supported by this supervisor.")
    return {"ORB_LLM_WORKERS": json.dumps([{
        "name": f"candidate-{parallel}", "origin": "{inference_url}",
        "models": [candidate.MODEL], "capacity": parallel, "lanes": ["interactive"],
        "qualified": True,
        "qualification": "EXPERIMENTAL serving qualification; not hospital capacity evidence",
    }]), "ORB_LLM_POOL_MAX_INFLIGHT": "200"}


def planned_phases(parallel: int, burst_only: bool) -> list[tuple[str, int, int, float]]:
    if burst_only:
        return [(f"burst_200_actual_capacity_{parallel}", 200, 200, 20.0)]
    return [("sequential_general_patient_department", 1, 3, 60.0)] if parallel == 1 else [
        ("two_concurrent_general_patient", 2, 2, 60.0),
        ("burst_200_actual_capacity_2", 200, 200, 20.0),
    ]


def runner_token_timings(path: Path) -> dict[str, object]:
    """Owned engine timings are not a mapping to completed HTTP request IDs."""
    tasks: dict[int, dict[str, object]] = {}
    if not path.is_file():
        return {"source": "owned_llama_server_log", "tasks": [], "available": False}
    for line in path.read_text(errors="replace").splitlines():
        match = re.search(r"slot print_timing: id\s+([0-9]+)\s+\| task\s+([0-9]+)\s+\|\s+(prompt eval time|eval time)\s*=\s*([0-9.]+) ms /\s*([0-9]+) tokens", line)
        if match:
            slot, task, kind, milliseconds, tokens = match.groups()
            row = tasks.setdefault(int(task), {"task_id": int(task), "slot_id": int(slot)})
            prefix = "prompt" if kind == "prompt eval time" else "decode"
            row[prefix + "_tokens"] = int(tokens)
            row[prefix + "_seconds"] = float(milliseconds) / 1000
            row[prefix + "_tokens_per_second"] = int(tokens) * 1000 / float(milliseconds) if float(milliseconds) else None
        stop = re.search(r"slot\s+release: id\s+([0-9]+)\s+\| task\s+([0-9]+)\s+\| stop processing: n_tokens = ([0-9]+), truncated = ([01])", line)
        if stop:
            slot, task, tokens, truncated = stop.groups()
            row = tasks.setdefault(int(task), {"task_id": int(task), "slot_id": int(slot)})
            row["stop_event_seen"], row["final_context_tokens"], row["truncated"] = True, int(tokens), truncated == "1"
    return {"source": "owned_llama_server_log", "available": bool(tasks), "tasks": list(tasks.values()),
            "interpretation": "Includes warmup and potentially cancelled streams; HTTP/SSE rows determine answer success. These engine IDs are not clinical request IDs."}


def acceptance_flags(report: dict[str, Any]) -> dict[str, bool]:
    profiles = report["profiles"]
    planned = report.get("planned_profiles", [])
    all_profiles = (bool(planned) and len(profiles) == len(planned)
                    and all(profile.get("status") == "complete" for profile in profiles))
    retrieval_ok = bool(profiles) and report["retrieval_configuration"] == "installed_bge_m3_and_reranker"
    bursts: list[dict[str, Any]] = []
    for profile in profiles:
        observed = profile.get("orb_fixture_observations", {})
        proof, runtime = observed.get("full_retrieval_proof", {}), observed.get("retrieval_runtime", {})
        retrieval_ok = bool(retrieval_ok and proof.get("nonzero_embedding_probe") is True
            and proof.get("cross_encoder_probe") is True and proof.get("corpus_vectors", 0) > 0
            and proof.get("nonzero_corpus_vectors") == proof.get("corpus_vectors")
            and runtime.get("embedding_failed") == runtime.get("rerank_failed") == 0
            and runtime.get("embedding_calls", -1) == runtime.get("embedding_completed", -2)
            and runtime.get("rerank_calls", -1) == runtime.get("rerank_completed", -2)
            and runtime.get("results_lexical") == runtime.get("results_other") == 0
            and runtime.get("results_cross_encoder", 0) > 0)
        bursts.extend(phase for phase in profile["phases"]
                      if phase["planned_requests"] == 200 and phase["concurrency"] == 200)
    strict_bursts = bool(bursts) and all(phase.get("status") == "complete"
        and len(phase.get("requests", [])) == 200 and phase.get("summary", {}).get("requests") == 200
        and phase["summary"].get("failed") == 0
        and all(row.get("success") is True and row.get("completed_within_target") is True
                and row.get("model") == candidate.MODEL and not row.get("cache_hit")
                for row in phase["requests"])
        and phase["summary"].get("all_ai_requests_meet_completed_answer_targets") is True for phase in bursts)
    passed = report.get("status") == "complete" and all_profiles and retrieval_ok and strict_bursts
    return {"procedure_completed": report.get("status") == "complete",
            "all_planned_profiles_exercised": all_profiles, "full_retrieval_integrity_verified": retrieval_ok,
            "strict_200_request_targets_demonstrated": passed, "acceptance_passed": passed}


def install_synthetic_answer_capture() -> None:
    """Observe existing fixture SSE without changing any clinical request/body."""
    original = capacity.StreamObservation
    if getattr(original, "_orb_synthetic_capture", False):
        return

    class SyntheticObservation(original):
        _orb_synthetic_capture = True

        def accept(self, event: str, raw: str, elapsed: float) -> None:
            super().accept(event, raw, elapsed)
            if raw == "[DONE]":
                return
            try:
                payload = json.loads(raw)
            except ValueError:
                return
            if isinstance(payload, dict) and isinstance(payload.get("content"), str):
                self.__dict__.setdefault("_synthetic_answer_parts", []).append(payload["content"])
            # This hook runs only for an owned synthetic fixture. Keep the
            # independently checkable grounding proof separate from prose;
            # words such as "unknown" cannot prove that an answer is grounded.
            if isinstance(payload, dict):
                metadata = payload.get("metadata")
                if isinstance(metadata, dict) and isinstance(metadata.get("summary_grounding"), dict):
                    self.__dict__.setdefault("_synthetic_summary_grounding", []).append(
                        metadata["summary_grounding"])

        def report(self) -> dict[str, Any]:
            result = super().report()
            result["synthetic_answer"] = "".join(self.__dict__.get("_synthetic_answer_parts", []))
            proofs = self.__dict__.get("_synthetic_summary_grounding", [])
            result["summary_grounding_proof_count"] = len(proofs)
            result["summary_grounding"] = proofs[0] if len(proofs) == 1 else None
            return result

    capacity.StreamObservation = SyntheticObservation


def progress(event: str, **details: object) -> None:
    print(json.dumps({"event": event, **details}), flush=True)


async def terminate_owned(process: subprocess.Popen[str] | None) -> None:
    """Never search for or signal an arbitrary Ollama process or port owner."""
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        await asyncio.to_thread(process.wait, 5)
    except ProcessLookupError:
        await asyncio.to_thread(process.wait, 5)
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await asyncio.to_thread(process.wait, 5)


async def wait_ready(origin: str, process: subprocess.Popen[str]) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(150):
            if process.poll() is not None:
                raise QualificationAborted("Owned candidate exited before readiness.")
            try:
                response = await client.get(origin + "/api/version", timeout=0.5)
                if response.status_code == 200 and response.json().get("version") == "0.40.4":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(0.1)
    raise QualificationAborted("Owned candidate did not become ready.")


async def warm_model(origin: str) -> dict[str, Any]:
    """One token loads exact weights; a truncated warmup is not an SLA answer."""
    started = time.perf_counter()
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(origin + "/api/generate", json={
            "model": candidate.MODEL, "prompt": "Orb synthetic serving qualification warmup.",
            "stream": False, "keep_alive": "30m", "options": {"num_ctx": 8192, "num_predict": 1},
        }, timeout=90)
        response.raise_for_status()
        data = response.json()
        if data.get("error") or data.get("done") is not True:
            raise QualificationAborted("Owned model warmup failed.")
        residency = await client.get(origin + "/api/ps", timeout=2)
        residency.raise_for_status()
    return {"purpose": "warmup_only_not_a_clinical_answer_or_target_pass",
            "wall_s": time.perf_counter() - started,
            "counters": {key: data.get(key) for key in (
                "model", "done_reason", "load_duration", "prompt_eval_count", "prompt_eval_duration",
                "eval_count", "eval_duration")},
            "residency": residency.json()}


async def monitor_pressure(initial: candidate.MemorySnapshot, samples: list[dict[str, Any]],
                           process: subprocess.Popen[str] | None, *,
                           process_resolver: Callable[[], subprocess.Popen[str] | None] | None = None) -> None:
    consecutive_low = 0
    while True:
        await asyncio.sleep(2)
        current = await asyncio.to_thread(candidate.inspect_memory)
        consecutive_low = consecutive_low + 1 if current.free_percentage < 15 else 0
        samples.append({"monotonic": time.monotonic(), **asdict(current), **vm_counters(current)})
        reason = pressure_abort_reason(initial, current, consecutive_low)
        owned = process_resolver() if process_resolver is not None else process
        if reason is not None:
            samples[-1]["guard_abort_reason"] = reason
            await terminate_owned(owned)
            raise QualificationAborted(reason)
        if owned is not None and owned.poll() is not None:
            raise QualificationAborted("Owned candidate exited during qualification.")


async def execute_phases(origin: str, parallel: int, profile: dict[str, Any], *,
                         embedding_model: str | None = None,
                         reranker_model: str | None = None, burst_only: bool = False,
                         expected_retrieval_workers: int | None = None) -> None:
    profile["warmup"] = await warm_model(origin)
    progress("warmup_completed", parallel=parallel, seconds=profile["warmup"]["wall_s"])
    async with capacity.orb_stub_target(fixture_settings(parallel), origin,
        embedding_model=embedding_model, reranker_model=reranker_model) as (base_url, manifest, _fixture):
        if manifest.get("synthetic_data_only") is not True:
            raise QualificationAborted("Disposable fixture did not attest synthetic data.")
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(base_url + "/__capacity_fixture__", timeout=2)
            response.raise_for_status()
            initial_metadata = response.json()
        profile["initial_retrieval_executor"] = initial_metadata.get("retrieval_executor")
        executor_info = initial_metadata.get("retrieval_executor")
        if expected_retrieval_workers is not None and (not isinstance(executor_info, dict)
                or executor_info.get("workers") != expected_retrieval_workers):
            raise QualificationAborted("Disposable child has a different retrieval worker width than requested.")
        for name, concurrency, requests, timeout in planned_phases(parallel, burst_only):
            phase: dict[str, Any] = {"name": name, "planned_requests": requests,
                                     "concurrency": concurrency, "hard_request_timeout_s": timeout,
                                     "status": "running", "requests": [],
                                     "observed_request_results": 0,
                                     "unobserved_request_results": requests,
                                     "result_observation_scope": "Returned HTTP runner results only; unobserved requests have unknown physical engine completion, not assumed failures or rejections."}
            profile["phases"].append(phase)
            progress("phase_started", parallel=parallel, name=name, requests=requests)
            try:
                summary, rows = await capacity.run_burst(base_url, manifest, mode="fresh-ai",
                    concurrency=concurrency, requests=requests, timeout=timeout,
                    require_model=True, run_id=uuid.uuid4().hex[:12],
                    result_observer=phase["requests"].append)
                phase.update(status="complete", summary=summary, requests=rows)
            finally:
                phase["observed_request_results"] = len(phase["requests"])
                phase["unobserved_request_results"] = requests - len(phase["requests"])
                if phase["status"] != "complete":
                    phase["status"] = "aborted_incomplete_not_acceptance_evidence"
                    # A successful-only subset is not a completed cohort and
                    # must never carry accepted timing quantiles or a pass.
                    phase.pop("summary", None)
            progress("phase_completed", parallel=parallel, name=name,
                     successful=summary["successful"], failed=summary["failed"])
        # Preserve content-free proof before the context deletes the copied
        # application/database. Root source hashes alone cannot prove the
        # disposable child ran those same source files.
        async with httpx.AsyncClient(trust_env=False) as client:
            marker = await client.get(base_url + "/__capacity_fixture__", timeout=2)
            marker.raise_for_status()
            metadata = marker.json()
            if not metadata.get("nonce"):
                raise QualificationAborted("Lost disposable fixture provenance.")
            profile["orb_fixture_observations"] = {key: value for key, value in metadata.items()
                if key in {"copied_source_sha256", "database_encryption", "http_admission",
                           "diagnostic_default_executor_workers", "effective_default_executor_workers",
                           "full_retrieval_proof", "retrieval_runtime", "retrieval_executor"}}
            if embedding_model is not None and not metadata.get("full_retrieval_proof"):
                raise QualificationAborted("Full clinical retrieval provenance was not supplied by the child.")


async def run_profile(parallel: int, args: argparse.Namespace, report: dict[str, Any]) -> None:
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    directory = args.output_dir / f"parallel-{parallel}"
    initial = await asyncio.to_thread(candidate.inspect_memory)
    model = candidate.inspect_model(args.models_dir.resolve())
    required = candidate.required_available_bytes(model, parallel)
    profile: dict[str, Any] = {"parallel": parallel, "origin": origin, "phases": [],
        "fixture_requested_configuration": fixture_settings(parallel),
        "preflight": {"memory": asdict(initial), "model": asdict(model),
                       "required_available_bytes_estimate": required}, "pressure_samples": []}
    report["profiles"].append(profile)
    if initial.estimated_available_bytes < required:
        profile.update(status="not_run_memory_guard", port_closed_after=True)
        return
    command = [sys.executable, str(ROOT / "scripts/serve_ollama_candidate.py"),
               "--port", str(port), "--parallel", str(parallel), "--swa-full", args.swa_full,
               "--flash-attention", args.flash_attention, "--models-dir", str(args.models_dir.resolve()),
               "--run-dir", str(directory), "--execute"]
    process: subprocess.Popen[str] | None = None
    pressure: asyncio.Task[None] | None = None
    execution: asyncio.Task[None] | None = None
    try:
        with (args.output_dir / f"parallel-{parallel}-daemon.log").open("w") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       text=True, start_new_session=True)
            profile["owned_daemon_pid"] = process.pid
            await wait_ready(origin, process)
            pressure = asyncio.create_task(monitor_pressure(initial, profile["pressure_samples"], process))
            execution = asyncio.create_task(execute_phases(origin, parallel, profile,
                embedding_model=str(args.embedding_model) if args.embedding_model else None,
                reranker_model=str(args.reranker_model) if args.reranker_model else None,
                burst_only=args.burst_only, expected_retrieval_workers=args.expected_retrieval_workers))
            done, _pending = await asyncio.wait((pressure, execution), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
            profile["status"] = "complete"
    finally:
        for task in (execution, pressure):
            if task is not None and not task.done():
                task.cancel()
        # Do not let a task exception skip daemon cleanup. The exception from
        # the try body still propagates after this finally block completes.
        await asyncio.gather(*(task for task in (execution, pressure) if task is not None),
                             return_exceptions=True)
        await terminate_owned(process)
        # A short client connection only verifies cleanup of the port this run
        # allocated. It never contacts or controls the shared11434 service.
        with socket.socket() as check:
            check.settimeout(0.5)
            profile["port_closed_after"] = check.connect_ex(("127.0.0.1", port)) != 0
        profile["post_memory"] = asdict(await asyncio.to_thread(candidate.inspect_memory))
        profile["system_pressure_summary"] = system_pressure_summary(profile)
        profile["runner_token_timings"] = runner_token_timings(args.output_dir / f"parallel-{parallel}-daemon.log")
        for phase in profile["phases"]:
            if phase["status"] == "running":
                phase["status"] = "aborted_incomplete_not_acceptance_evidence"
        if profile.get("status") != "complete":
            profile["status"] = "aborted"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--models-dir", type=Path, required=True)
    result.add_argument("--output-dir", type=Path, required=True, help="New directory with an existing parent")
    result.add_argument("--budget-seconds", type=float, default=300)
    result.add_argument("--swa-full", choices=("on", "off"), default="on")
    result.add_argument("--flash-attention", choices=("auto", "on", "off"), default="auto")
    result.add_argument("--embedding-model", type=Path, help="Existing absolute installed BGE-m3 snapshot; paired with reranker")
    result.add_argument("--reranker-model", type=Path, help="Existing absolute installed reranker snapshot; paired with embedding")
    result.add_argument("--execute", action="store_true")
    result.add_argument("--allow-model-load", action="store_true")
    result.add_argument("--allow-heavy-model-load", action="store_true")
    result.add_argument("--warm-host-retry", action="store_true", help="Label a retry after an earlier cold-start guard abort; never relaxes resource guards")
    result.add_argument("--profiles", type=int, choices=(1, 2), nargs="+", default=[1, 2])
    result.add_argument("--burst-only", action="store_true", help="After warmup and full-RAG startup, exercise200requests at the actual selected capacity")
    result.add_argument("--expected-retrieval-workers", type=int, choices=(1, 4, 8, 16, 32),
                        help="Refuse before the query phase if the actual copied child reports another width")
    return result


async def main_async(args: argparse.Namespace) -> int:
    if not 30 <= args.budget_seconds <= 300:
        raise ValueError("The dedicated model window must be bounded to30–300seconds.")
    if len(set(args.profiles)) != len(args.profiles):
        raise ValueError("Each bounded serving profile may be requested only once.")
    if bool(args.embedding_model) != bool(args.reranker_model):
        raise ValueError("Embedding and reranker snapshots must be supplied together.")
    for path in (args.embedding_model, args.reranker_model):
        if path is not None and (not path.is_absolute() or not path.is_dir()):
            raise ValueError("Clinical retrieval requires existing absolute installed snapshot directories.")
    if not args.execute:
        progress("plan_only", profiles=args.profiles, total_budget_s=args.budget_seconds,
                 phases=[{"parallel": parallel, "phases": planned_phases(parallel, args.burst_only)} for parallel in args.profiles],
                 retrieval="installed_embedding_and_reranker" if args.embedding_model else "chart_only_knowledge_rag_disabled",
                 note="No daemon, fixture or model request was started.")
        return 0
    if not args.allow_model_load or not args.allow_heavy_model_load:
        raise ValueError("Execution requires explicit ordinary and200-request model-load acknowledgements.")
    if (not args.output_dir.is_absolute() or args.output_dir.exists() or args.output_dir.is_symlink()
            or not args.output_dir.parent.is_dir()):
        raise ValueError("Use a new absolute output directory with an existing parent.")
    if not args.models_dir.is_absolute() or not args.models_dir.is_dir():
        raise ValueError("An existing absolute local model cache is required.")
    args.output_dir.mkdir(mode=0o700)
    install_synthetic_answer_capture()
    report: dict[str, Any] = {"created_at": datetime.now(timezone.utc).isoformat(),
        "evidence_kind": "actual_orb_synthetic_records_owned_real_local_model",
        "host_condition": "warm_host_after_retained_cold_start_guard_abort" if args.warm_host_retry else "initial_host_state_not_a_cold_stability_qualification",
        "hardware_runtime": capacity.hardware_metadata(), "profiles": [],
        "planned_profiles": args.profiles, "burst_only": args.burst_only,
        "retrieval_configuration": "installed_bge_m3_and_reranker" if args.embedding_model else "chart_only_knowledge_rag_disabled",
        "qualification_source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__), ROOT / "scripts/serve_ollama_candidate.py",
                         ROOT / "server/services/model_router.py", ROOT / "server/services/local_model_http.py")},
        "quality": "Unchanged clinical model/prompts/context/budgets; synthetic answer capture permits review, not clinical validation.",
        "pressure_monitor": "Every2s; stop only owned children on2samples<15%free, >512MiBswap growth or >4GiBcompression growth.",
        "limitations": ["No real patient records, ASR, imaging or summary-refresh load.",
                        "Synthetic outputs are local artifacts; no output is sent to an external service.",
                        "Pressure monitoring is included in elapsed latency; incomplete phases cannot qualify targets."]}
    if args.embedding_model is None:
        report["limitations"].append("Knowledge embedding/reranking are disabled in this fixture: chart-only timing cannot qualify full clinical retrieval.")
    try:
        async with asyncio.timeout(args.budget_seconds):
            for parallel in args.profiles:
                await run_profile(parallel, args, report)
        report["status"] = "complete"
    except (TimeoutError, QualificationAborted, httpx.HTTPError, OSError) as exc:
        report["status"] = "aborted"
        report["abort_category"] = "window_timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
        report["abort_reason"] = str(exc) if isinstance(exc, QualificationAborted) else "See owned local logs; no arbitrary host process was changed."
    finally:
        report.update(acceptance_flags(report))
        (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    progress("qualification_finished", status=report["status"], acceptance_passed=report["acceptance_passed"],
             report=str(args.output_dir / "report.json"))
    return 0 if report["acceptance_passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main_async(parser().parse_args())))
    except (ValueError, OSError) as error:
        print(f"Orb serving qualification refused: {error}", file=sys.stderr)
        raise SystemExit(2) from error
