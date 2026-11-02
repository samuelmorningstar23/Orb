#!/usr/bin/env python3
"""Guarded, offline real-Orb benchmark of one owned local inference engine.

MLX is a separately labelled quantisation candidate, never an automatically
qualified replacement for the installed clinical model. No live Orb database
or existing inference daemon is used or modified.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import loadtest_capacity as capacity
import qualify_ollama_serving as qualification
import serve_ollama_candidate as candidate

ROOT = Path(__file__).resolve().parents[1]
MLX_LAUNCHERS = {"standard": "serve_mlx_benchmark.py",
                 "staged-uncached": "serve_mlx_benchmark_staged.py",
                 "trace-standard": "serve_mlx_benchmark_trace.py",
                 "trace-staged": "serve_mlx_benchmark_trace.py"}


def validate_mlx_candidate(directory: Path, policy: Path | None, *, loading_mode: str = "standard") -> dict[str, Any]:
    if not directory.is_absolute() or not directory.is_dir() or (directory / ".conversion-incomplete").exists():
        raise ValueError("MLX conversion must be complete before model load")
    if policy is None:
        raise ValueError("An explicit local MLX candidate policy is required before model load")
    from mlx_ollama_benchmark_proxy import load_policy
    policy_data = load_policy(policy, allow_unqualified=True)
    if loading_mode not in MLX_LAUNCHERS or policy_data.get("loading_mode", "standard") != loading_mode:
        raise ValueError("Explicit model loading mode must match its candidate policy")
    if loading_mode != "standard":
        loading_sources = {"loading_wrapper_sha256": MLX_LAUNCHERS[loading_mode]}
        if loading_mode.startswith("trace-"):
            loading_sources["startup_trace_sha256"] = "mlx_startup_trace.py"
            if loading_mode == "trace-staged":
                loading_sources["traced_inner_wrapper_sha256"] = MLX_LAUNCHERS["staged-uncached"]
        for policy_key, filename in loading_sources.items():
            launcher = ROOT / "scripts" / filename
            if (not launcher.is_file() or policy_data.get(policy_key) !=
                    hashlib.sha256(launcher.read_bytes()).hexdigest()):
                raise ValueError("Pinned loading instrumentation is absent or changed")
    manifest = json.loads((directory / "conversion-manifest.json").read_text())
    if (manifest.get("status") != "converted_candidate_unqualified" or
            manifest.get("source_sha256") != candidate.MODEL_DIGEST.removeprefix("sha256:") or
            manifest.get("candidate_label") != policy_data.get("conversion_label") or
            manifest.get("clinical_quality_qualified") is not False or
            not isinstance(manifest.get("files"), dict) or not manifest["files"]):
        raise ValueError("Converted model provenance is incomplete or mismatched")
    uncached_hash_files = 0
    for name, expected in manifest["files"].items():
        file = directory / name
        if (not file.resolve().is_relative_to(directory.resolve()) or not file.is_file() or
                file.stat().st_size != expected["bytes"]):
            raise ValueError("Converted model file is absent, outside its directory or changed")
        with file.open("rb") as handle:
            if sys.platform == "darwin" and file.suffix == ".safetensors" and file.stat().st_size >= 1024 ** 2:
                import fcntl
                # Per-FD cache control, not a global cache purge. Hash every byte
                # without populating an extra model-sized filesystem cache.
                fcntl.fcntl(handle.fileno(), fcntl.F_NOCACHE, 1)
                uncached_hash_files += 1
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected["sha256"]:
                raise ValueError("Converted model file hash changed")
    manifest["benchmark_checksum_io"] = {"large_safetensors_uncached_fd_count": uncached_hash_files}
    return manifest


def worker_settings(engine: str, parallel: int) -> dict[str, str]:
    if engine not in {"mlx", "ollama"} or parallel not in {1, 2, 4, 8}:
        raise ValueError("Unsupported bounded engine width")
    return {"ORB_LLM_WORKERS": json.dumps([{
        "name": f"benchmark-{engine}-{parallel}", "origin": "{inference_url}",
        "models": [candidate.MODEL], "capacity": parallel, "lanes": ["interactive"],
        "qualified": True, "qualification": "EXPERIMENTAL offline engine comparison only",
    }]), "ORB_LLM_POOL_MAX_INFLIGHT": "200", "ORB_SAGE_RETRIEVAL_WORKERS": "4"}


def mlx_kv_bytes(config: dict[str, Any], parallel: int) -> int:
    """Budget the actual Gemma3 global/local pattern; retain the full 8192 window."""
    cfg = config.get("text_config", config)
    pinned = {"num_hidden_layers": 62, "sliding_window_pattern": 6,
              "num_key_value_heads": 16, "head_dim": 128, "sliding_window": 1024}
    if parallel not in {1, 2, 4, 8} or any(type(cfg.get(key)) is not int or cfg[key] != value
                                           for key, value in pinned.items()):
        raise ValueError("KV budget requires the pinned Gemma3 text architecture")
    layers = cfg["num_hidden_layers"]
    pattern = cfg["sliding_window_pattern"]
    global_layers = sum((index + 1) % pattern == 0 for index in range(layers))
    local_layers = layers - global_layers
    return (parallel * 2 * cfg["num_key_value_heads"] * cfg["head_dim"] * 2 *
            (global_layers * 8192 + local_layers * cfg["sliding_window"]))


def offline_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.upper().endswith("_PROXY") or key.upper().startswith(("OLLAMA_", "LLAMA_", "GGML_", "MLX_")):
            del env[key]
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1",
               HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1", TOKENIZERS_PARALLELISM="false")
    return env


def inference_environment(env: dict[str, str], engine: str) -> dict[str, str]:
    # MLX text serving/tokenization never uses these installed frameworks.
    # Do not propagate this to Orb's real PyTorch retrieval subprocess.
    return ({**env, "USE_TORCH": "0", "USE_TF": "0", "USE_FLAX": "0"}
            if engine == "mlx" else dict(env))


async def native_ready(origin: str, process: subprocess.Popen[str], engine: str) -> None:
    path = "/api/version" if engine == "ollama" else "/v1/models"
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(600):
            if process.poll() is not None:
                raise qualification.QualificationAborted("Owned engine exited before readiness")
            try:
                response = await client.get(origin + path, timeout=.5)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)
    raise qualification.QualificationAborted("Owned engine readiness timed out")


async def proxy_ready(origin: str, process: subprocess.Popen[str]) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(100):
            if process.poll() is not None:
                raise qualification.QualificationAborted("Owned benchmark proxy exited before readiness")
            try:
                response = await client.get(origin + "/api/version", timeout=.5)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.1)
    raise qualification.QualificationAborted("Owned proxy readiness timed out")


async def phases(origin: str, args: argparse.Namespace, profile: dict[str, Any]) -> None:
    profile["warmup"] = await qualification.warm_model(origin)
    qualification.progress("warmup_completed", engine=args.engine,
                           seconds=profile["warmup"]["wall_s"])
    async with capacity.orb_stub_target(worker_settings(args.engine, args.parallel), origin,
            embedding_model=str(args.embedding_model), reranker_model=str(args.reranker_model)) as (base, manifest, _):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(base + "/__capacity_fixture__", timeout=2)
            response.raise_for_status()
            metadata = response.json()
        if metadata.get("retrieval_executor", {}).get("workers") != 4 or not metadata.get("full_retrieval_proof"):
            raise qualification.QualificationAborted("Full retrieval fixture did not attest expected configuration")
        profile["initial_fixture_metadata"] = metadata
        for name in args.phases:
            if name == "replay":
                from replay_llm_benchmark import replay
                replay_phase: dict[str, Any] = {"name": "replay", "status": "running", "requests": []}
                profile["phases"].append(replay_phase)
                try:
                    if args.replay_captures is None:
                        raise ValueError("Replay needs the original synthetic requests.jsonl")
                    result = await replay(origin, args.replay_captures, replay_phase["requests"].append)
                    replay_phase.update(result, status="complete")
                    qualification.progress("replay_completed", engine=args.engine,
                        successful=sum(row["success"] for row in replay_phase["requests"]))
                finally:
                    if replay_phase["status"] == "running":
                        replay_phase["status"] = "aborted_incomplete"
                continue
            if name == "quality":
                from llm_fidelity_cases import run_cases
                quality: dict[str, Any] = {"name": "quality", "status": "running", "cases": []}
                profile["phases"].append(quality)
                qualification.progress("phase_started", engine=args.engine, name="quality", requests=8)
                try:
                    result = await run_cases(origin, result_observer=quality["cases"].append)
                    quality.update(result, status="complete")
                    qualification.progress("quality_completed", engine=args.engine,
                        successful=result["successful"], total=result["total"], passed=result["all_cases_passed"])
                    if args.engine == "mlx" and not result["all_cases_passed"]:
                        raise qualification.QualificationAborted("synthetic_fidelity_gate_failed_before_load_test")
                finally:
                    if quality["status"] == "running":
                        quality["status"] = "aborted_incomplete"
                continue
            count, width, timeout = (3, 1, 60.0) if name == "sequential" else (200, 200, 20.0)
            phase: dict[str, Any] = {"name": name, "planned_requests": count,
                "concurrency": width, "hard_request_timeout_s": timeout,
                "status": "running", "requests": []}
            profile["phases"].append(phase)
            qualification.progress("phase_started", engine=args.engine, name=name, requests=count)
            try:
                summary, rows = await capacity.run_burst(base, manifest, mode="fresh-ai",
                    concurrency=width, requests=count, timeout=timeout, require_model=True,
                    run_id=uuid.uuid4().hex[:12], result_observer=phase["requests"].append)
                phase.update(status="complete", summary=summary, requests=rows)
                qualification.progress("phase_completed", engine=args.engine, name=name,
                                       successful=summary["successful"], failed=summary["failed"])
            finally:
                phase["observed_http_results"] = len(phase["requests"])
                phase["unobserved_http_results"] = count - len(phase["requests"])
                if phase["status"] == "running":
                    phase["status"] = "aborted_incomplete_not_acceptance_evidence"
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(base + "/__capacity_fixture__", timeout=2)
            response.raise_for_status()
            profile["final_fixture_metadata"] = response.json()


async def engine_only_phases(origin: str, args: argparse.Namespace, profile: dict[str, Any]) -> None:
    """Exact-body comparison when combined model/RAG residency is unqualified."""
    if any(name not in {"quality", "replay", "replay200"} for name in args.phases):
        raise ValueError("Engine-only mode supports fidelity and exact-body replay, not Orb SLA phases")
    profile["warmup"] = await qualification.warm_model(origin)
    qualification.progress("warmup_completed", engine=args.engine, scope="engine_only")
    for name in args.phases:
        phase: dict[str, Any] = {"name": name, "status": "running"}
        profile["phases"].append(phase)
        try:
            if name == "quality":
                from llm_fidelity_cases import run_cases
                phase["cases"] = []
                result = await run_cases(origin, result_observer=phase["cases"].append)
                phase.update(result, status="complete")
                qualification.progress("quality_completed", engine=args.engine,
                    successful=result["successful"], total=result["total"], passed=result["all_cases_passed"])
                if args.engine == "mlx" and not result["all_cases_passed"]:
                    raise qualification.QualificationAborted("synthetic_fidelity_gate_failed_before_timing")
            elif name == "replay":
                from replay_llm_benchmark import replay
                phase["requests"] = []
                if args.replay_captures is None:
                    raise ValueError("Replay requires the exact captured synthetic Sage bodies")
                result = await replay(origin, args.replay_captures, phase["requests"].append)
                phase.update(result, status="complete")
                qualification.progress("replay_completed", engine=args.engine,
                    successful=sum(row["success"] for row in phase["requests"]))
            else:
                from replay_200_llm_benchmark import replay_200
                phase.update(planned_requests=200, concurrency=200, requests=[])
                if args.replay_captures is None:
                    raise ValueError("The engine-only cohort requires 200 original captured Sage bodies")
                result = await replay_200(origin, args.replay_captures,
                                          result_observer=phase["requests"].append)
                phase.update(result, status="complete")
                qualification.progress("engine_only_200_completed", engine=args.engine,
                    observed=len(phase["requests"]), successful=sum(row["success"] for row in phase["requests"]))
        finally:
            if name == "replay200":
                phase["observed_http_results"] = len(phase.get("requests", []))
                phase["unobserved_http_results"] = 200 - phase["observed_http_results"]
            if phase["status"] == "running":
                phase["status"] = "aborted_incomplete"


def acceptance(report: dict[str, Any]) -> dict[str, bool]:
    profile = report["profile"]
    bursts = [phase for phase in profile["phases"] if phase["name"] == "burst"]
    runtime = profile.get("final_fixture_metadata", {}).get("retrieval_runtime", {})
    retrieval_ok = bool(runtime) and (
        runtime.get("embedding_calls", -1) == runtime.get("embedding_completed", -2) and
        runtime.get("rerank_calls", -1) == runtime.get("rerank_completed", -2) and
        runtime.get("embedding_failed") == runtime.get("rerank_failed") == 0 and
        runtime.get("results_lexical") == runtime.get("results_other") == 0 and
        runtime.get("results_cross_encoder", 0) > 0)
    latency_ok = bool(bursts) and report["status"] == "complete" and retrieval_ok and all(
        phase["status"] == "complete" and len(phase["requests"]) == 200 and
        phase.get("summary", {}).get("requests") == 200 and
        phase["summary"].get("failed") == 0 and
        phase["summary"].get("all_ai_requests_meet_completed_answer_targets") is True and
        all(row.get("success") is True and row.get("completed_within_target") is True
            and row.get("model") == candidate.MODEL and not row.get("cache_hit") for row in phase["requests"])
        for phase in bursts)
    return {"full_retrieval_integrity_verified": retrieval_ok,
            "strict_200_latency_targets_demonstrated": latency_ok,
            # The benchmark cannot award clinical validation to either engine.
            "clinical_equivalence_qualified": False, "production_purchase_gate_passed": False}


def failure_details(error: BaseException) -> dict[str, Any]:
    """Persist useful synthetic benchmark diagnostics without URLs or payloads."""
    details: dict[str, Any] = {"error_type": type(error).__name__,
                               "abort_reason": "Inspect owned local logs"}
    if isinstance(error, qualification.QualificationAborted):
        details["abort_reason"] = str(error)
    elif isinstance(error, httpx.HTTPStatusError):
        allowed = {"/api/generate", "/api/chat", "/api/ps", "/api/version",
                   "/v1/chat/completions", "/v1/models", "/__capacity_fixture__"}
        path = error.request.url.path
        path = path if path in allowed else "<other_path_redacted>"
        method = error.request.method
        method = method if method in {"GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"} else "<method_redacted>"
        details.update(http_status_code=error.response.status_code,
                       http_method=method, http_path=path,
                       abort_reason=f"HTTP status {error.response.status_code} on {method} {path}")
    return details


def record_task_failures(report: dict[str, Any], monitor: asyncio.Task[None] | None,
                         execution: asyncio.Task[None] | None, *,
                         promote_primary: bool = False) -> BaseException | None:
    """Retain completed exceptions and a guard trigger preceding termination.

    Never wait for or cancel a task here. The caller retains its existing
    cleanup behavior, then calls again to retain failures completed in cleanup.
    """
    failures = []
    for name, task in (("resource_monitor", monitor), ("execution", execution)):
        if task is not None and task.done() and not task.cancelled():
            error = task.exception()
            if error is not None:
                failures.append((name, error))
    failures.sort(key=lambda item: (
        0 if item[0] == "resource_monitor" and isinstance(item[1], qualification.QualificationAborted)
        else 1 if item[0] == "execution" else 2))
    trigger = next(((index, sample["guard_abort_reason"])
        for index, sample in enumerate(report.get("profile", {}).get("pressure_samples", []))
        if isinstance(sample.get("guard_abort_reason"), str) and sample["guard_abort_reason"]), None)
    if not failures and trigger is None:
        return None
    report["task_failures"] = [{"task": name, **failure_details(error)} for name, error in failures]
    primary = failures[0][1] if failures else None
    report["primary_failure_evidence"] = "completed_task_exception"
    if trigger is not None:
        index, reason = trigger
        # Termination can make the HTTP request fail before the monitor raises;
        # teardown may then cancel that still-pending monitor. The synchronous
        # marker proves the guard fired without inventing a completed exception.
        observed = any(name == "resource_monitor" and
            isinstance(error, qualification.QualificationAborted) and str(error) == reason
            for name, error in failures)
        report["resource_guard_trigger"] = {"pressure_sample_index": index, "reason": reason,
            "evidence": "recorded_before_owned_termination", "monitor_exception_observed": observed,
            "monitor_state": ("not_created" if monitor is None else "cancelled" if monitor.cancelled()
                              else "completed" if monitor.done() else "pending")}
        if not observed:
            primary = qualification.QualificationAborted(reason)
            report["primary_failure_evidence"] = "recorded_guard_trigger_before_termination"
    assert primary is not None
    if promote_primary:
        for key in ("http_status_code", "http_method", "http_path"):
            report.pop(key, None)
        report.update(failure_details(primary))
    return primary


async def run(args: argparse.Namespace) -> int:
    if args.output_dir.exists() or not args.output_dir.is_absolute() or not args.output_dir.parent.is_dir():
        raise ValueError("Output must be a new absolute directory with an existing parent")
    if not args.allow_model_load or (any(name in {"burst", "replay200"} for name in args.phases)
                                     and not args.allow_heavy_model_load):
        raise ValueError("Explicit model-load acknowledgements are required")
    if len(set(args.phases)) != len(args.phases):
        raise ValueError("Do not repeat phases in one bounded window")
    if args.engine_only and any(name not in {"quality", "replay", "replay200"} for name in args.phases):
        raise ValueError("Engine-only mode supports fidelity and exact-body replay, not Orb SLA phases")
    if not args.engine_only and "replay200" in args.phases:
        raise ValueError("Direct 200-input replay is engine-only and cannot be reported as Orb SLA")
    if args.engine == "mlx" and (not args.phases or args.phases[0] != "quality"):
        raise ValueError("The unqualified MLX candidate must pass fidelity before timing in this window")
    if not 30 <= args.budget_seconds <= 300:
        raise ValueError("Budget must be30..300seconds")
    if args.engine == "ollama" and args.parallel not in {1, 2, 4}:
        raise ValueError("Ollama launcher only supports widths1/2/4")
    for path in (args.embedding_model, args.reranker_model):
        if not args.engine_only and (path is None or not path.is_absolute() or not path.is_dir()):
            raise ValueError("Installed absolute retrieval snapshots are required")
    args.output_dir.mkdir(mode=0o700)
    preparation_memory = candidate.inspect_memory()
    native_port, proxy_port = qualification.free_port(), qualification.free_port()
    if native_port == proxy_port:
        raise ValueError("Owned ports collided; retry with a fresh output directory")
    native_origin, proxy_origin = f"http://127.0.0.1:{native_port}", f"http://127.0.0.1:{proxy_port}"
    env = offline_environment()
    engine_env = inference_environment(env, args.engine)
    if args.engine == "ollama":
        model = candidate.inspect_model(args.models_dir.resolve())
        required = candidate.required_available_bytes(model, args.parallel)
        command = [sys.executable, str(ROOT / "scripts/serve_ollama_candidate.py"),
            "--models-dir", str(args.models_dir.resolve()), "--run-dir", str(args.output_dir / "native"),
            "--port", str(native_port), "--parallel", str(args.parallel), "--swa-full", "on", "--execute"]
        model_info: dict[str, Any] = asdict(model)
    else:
        if args.model_dir is None or args.mlx_python is None or not args.model_dir.is_dir() or not args.mlx_python.is_file():
            raise ValueError("MLX requires an existing converted directory and isolated Python")
        conversion = validate_mlx_candidate(args.model_dir, args.mlx_policy, loading_mode=args.mlx_loading_mode)
        config = json.loads((args.model_dir / "config.json").read_text())
        model_bytes = sum(path.stat().st_size for path in args.model_dir.glob("*.safetensors"))
        if not model_bytes:
            raise ValueError("No local MLX weights")
        required = model_bytes + mlx_kv_bytes(config, args.parallel) + 20 * candidate.GIB
        launcher = MLX_LAUNCHERS[args.mlx_loading_mode]
        if args.mlx_loading_mode.startswith("trace-"):
            engine_env["ORB_MLX_STARTUP_TRACE_LOADING"] = (
                "staged-uncached" if args.mlx_loading_mode == "trace-staged" else "standard")
        command = [str(args.mlx_python), str(ROOT / "scripts" / launcher), "--model", str(args.model_dir),
            "--host", "127.0.0.1", "--port", str(native_port), "--decode-concurrency", str(args.parallel),
            "--prompt-concurrency", "1", "--prefill-step-size", "512", "--prompt-cache-bytes", "8589934592",
            "--prompt-cache-size", "10", "--log-level", "INFO"]
        model_info = {"model": candidate.MODEL, "kind": "same_source_separately_requantised_mlx_candidate",
                      "weights_bytes": model_bytes, "kv_budget_bytes": mlx_kv_bytes(config, args.parallel),
                      "quantised_kv": False, "clinical_equivalence": "not_qualified",
                      "loading_mode": args.mlx_loading_mode, "checksum_io": conversion["benchmark_checksum_io"],
                      "source_gguf_sha256": conversion["source_sha256"],
                      "conversion_manifest_sha256": hashlib.sha256((args.model_dir / "conversion-manifest.json").read_bytes()).hexdigest()}
    profile: dict[str, Any] = {"parallel": args.parallel, "phases": [],
        "preparation_memory": asdict(preparation_memory),
        "preflight": {"memory": asdict(preparation_memory),
                      "memory_sample_stage": "preparation_only_not_launch_baseline", "model": model_info,
                      "required_available_bytes_estimate": required}, "pressure_samples": []}
    report: dict[str, Any] = {"created_at": datetime.now(timezone.utc).isoformat(),
        "engine": args.engine, "hardware_runtime": capacity.hardware_metadata(),
        "scope": ("engine_only_exact_captured_synthetic_inputs_not_Orb_SLA" if args.engine_only
                  else "actual_Orb_full_RAG_synthetic_records_offline_candidate_comparison"),
        "profile": profile, "status": "planned", "synthetic_data_only": True,
        "runtime_network": "owned_loopback_only_no_cloud_inference",
        "unused_transformers_frameworks_disabled_in_mlx_inference_only": args.engine == "mlx",
        "limitations": ["Disposable unencrypted database; no production encryption/network qualification",
                        "No summary-refresh, ASR or imaging workload",
                        "MLX conversion is not identical quantised weights; speed alone cannot qualify clinical quality",
                        "Separate fixture timestamps can change prompts; use captured exact-body replay for engine-only parity"],
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "comparison_source_sha256": {name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
            for name in ("benchmark_mlx_ollama.py", "serve_mlx_benchmark.py", "mlx_ollama_benchmark_proxy.py",
                         "convert_medgemma_gguf_mlx.py", "llm_fidelity_cases.py",
                         "replay_llm_benchmark.py", "replay_200_llm_benchmark.py", "ollama_template_oracle.py",
                         "serve_ollama_candidate.py", "serve_mlx_benchmark_staged.py", "qualify_ollama_serving.py",
                         "serve_mlx_benchmark_trace.py", "mlx_startup_trace.py")
            if (ROOT / "scripts" / name).is_file()}}
    backend: subprocess.Popen[str] | None = None
    proxy: subprocess.Popen[str] | None = None
    monitor: asyncio.Task[None] | None = None
    execution: asyncio.Task[None] | None = None
    qualification.install_synthetic_answer_capture()
    try:
        # Model hashing/metadata collection can take tens of seconds. Preserve
        # that earlier sample, but make admission and pressure growth relative
        # to the current host state immediately before the owned launch.
        initial = candidate.inspect_memory()
        profile["preflight"].update(memory=asdict(initial),
            memory_sample_stage="immediately_before_launch_after_validation_and_metadata")
        if initial.estimated_available_bytes < required:
            report["status"] = "not_run_memory_preflight"
            return 1
        with (args.output_dir / "engine.log").open("w") as log, (args.output_dir / "proxy.log").open("w") as proxy_log:
            backend = subprocess.Popen(command, env=engine_env, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, text=True, start_new_session=True)
            profile["owned_engine_pid"] = backend.pid
            monitor = asyncio.create_task(qualification.monitor_pressure(initial, profile["pressure_samples"], backend))

            async def execute() -> None:
                nonlocal proxy
                await native_ready(native_origin, backend, args.engine)
                proxy_command = [sys.executable, str(ROOT / "scripts/mlx_ollama_benchmark_proxy.py"),
                    "--backend", args.engine, "--upstream", native_origin, "--port", str(proxy_port),
                    "--capture-dir", str(args.output_dir / "capture"), "--synthetic-data-only"]
                if args.engine == "mlx":
                    proxy_command += ["--model-dir", str(args.model_dir), "--allow-unqualified-mlx"]
                    if args.mlx_policy:
                        proxy_command += ["--mlx-policy", str(args.mlx_policy)]
                proxy = subprocess.Popen(proxy_command, env=engine_env, cwd=ROOT, stdout=proxy_log,
                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
                await proxy_ready(proxy_origin, proxy)
                if args.engine_only:
                    await engine_only_phases(proxy_origin, args, profile)
                else:
                    await phases(proxy_origin, args, profile)
                if args.engine == "ollama":
                    from ollama_template_oracle import (
                        collect_oracles,
                        runner_origin_from_log,
                    )
                    try:
                        runner = runner_origin_from_log(args.output_dir / "engine.log")
                        # Observe the actual passthrough bodies this engine saw,
                        # including fidelity cases before any captured Sage replay.
                        source_captures = args.output_dir / "capture/requests.jsonl"
                        oracle = await collect_oracles(native_origin, source_captures,
                            args.output_dir / "template-oracle.json", runner_origin=runner,
                            max_requests=200 if "replay200" in args.phases else 11)
                        profile["template_oracle"] = {"status": oracle["status"],
                            "path": str(args.output_dir / "template-oracle.json")}
                    except (ValueError, OSError, httpx.HTTPError) as error:
                        profile["template_oracle"] = {"status": "failed", "error_type": type(error).__name__}

            async with asyncio.timeout(args.budget_seconds):
                execution = asyncio.create_task(execute())
                await asyncio.wait((monitor, execution), return_when=asyncio.FIRST_COMPLETED)
                failure = record_task_failures(report, monitor, execution)
                if failure is not None:
                    raise failure
                report["status"] = "complete"
    except (TimeoutError, qualification.QualificationAborted, httpx.HTTPError, OSError, RuntimeError, ValueError) as error:
        report.update(status="aborted", **failure_details(error))
    finally:
        for cleanup_task in (execution, monitor):
            if cleanup_task is not None and not cleanup_task.done():
                cleanup_task.cancel()
        await asyncio.gather(*(cleanup_task for cleanup_task in (execution, monitor) if cleanup_task), return_exceptions=True)
        record_task_failures(report, monitor, execution, promote_primary=report["status"] == "aborted")
        await qualification.terminate_owned(proxy)
        await qualification.terminate_owned(backend)
        profile["post_memory"] = asdict(candidate.inspect_memory())
        profile["system_pressure_summary"] = qualification.system_pressure_summary(profile)
        profile["ports_closed_after"] = {}
        for port in (native_port, proxy_port):
            with socket.socket() as check:
                check.settimeout(.5)
                profile["ports_closed_after"][str(port)] = check.connect_ex(("127.0.0.1", port)) != 0
        if args.engine == "ollama":
            profile["runner_token_timings"] = qualification.runner_token_timings(args.output_dir / "engine.log")
        report.update(acceptance(report))
        (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        qualification.progress("benchmark_finished", engine=args.engine, status=report["status"],
            strict_200_targets=report["strict_200_latency_targets_demonstrated"], report=str(args.output_dir / "report.json"))
    return 0 if report["status"] == "complete" else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--engine", choices=("ollama", "mlx"), required=True)
    result.add_argument("--engine-only", action="store_true", help="No retrieval runtime; captured full-RAG inputs replayed exactly")
    result.add_argument("--parallel", type=int, choices=(1, 2, 4, 8), default=1)
    result.add_argument("--phases", nargs="+", choices=("quality", "replay", "replay200", "sequential", "burst"), default=["quality", "sequential", "burst"])
    result.add_argument("--replay-captures", type=Path)
    result.add_argument("--models-dir", type=Path, default=Path.home() / ".ollama/models")
    result.add_argument("--model-dir", type=Path)
    result.add_argument("--mlx-python", type=Path)
    result.add_argument("--mlx-policy", type=Path)
    result.add_argument("--mlx-loading-mode", choices=tuple(MLX_LAUNCHERS), default="standard")
    result.add_argument("--embedding-model", type=Path)
    result.add_argument("--reranker-model", type=Path)
    result.add_argument("--output-dir", type=Path, required=True)
    result.add_argument("--budget-seconds", type=float, default=300)
    result.add_argument("--allow-model-load", action="store_true")
    result.add_argument("--allow-heavy-model-load", action="store_true")
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
