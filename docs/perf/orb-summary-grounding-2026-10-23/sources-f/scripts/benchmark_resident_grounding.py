#!/usr/bin/env python3
"""Offline synthetic Orb qualification using an already-resident original model.

This separately labelled experiment owns only a passthrough proxy and a
throwaway Orb/retrieval fixture. It never launches or terminates an inference
engine. Resident-weight reuse is a distinct allocation policy: at least12GiB
of additional OS-estimated headroom is required before loading real retrieval;
the existing 4GiB-compression/512MiB-swap pressure guards remain unchanged.
Run under offline_benchmark.sb. No model is downloaded or reconfigured.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import signal
import subprocess
import sys
from contextlib import AsyncExitStack, suppress
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import benchmark_mlx_ollama as benchmark
import benchmark_model_residency_lease as lease
import httpx
import mlx_ollama_benchmark_proxy as bridge
from ollama_template_oracle import owned_origin

ROOT = Path(__file__).resolve().parents[1]
REGULAR = "http://127.0.0.1:11434"
PINNED_MANIFEST = "58238ae38f99827496301de22016b2f94157552971f5a38db226f13802c2437e"
EXTRA_HEADROOM_BYTES = 12 * benchmark.candidate.GIB
SCOPE = "already_resident_original_service_not_owned_engine_comparison"


def resident_proxy_app(capture_dir: Path, *, transport: httpx.AsyncBaseTransport | None = None):
    """Narrow capability used only by this script's authorized child process.

    The existing generic bridge still rejects11434. Temporarily supply its
    factory an exact fixed upstream; immediately restore the generic validator.
    The resulting closure has no caller-configurable upstream or engine control.
    """
    ordinary_validator = bridge.local_origin

    def fixed_resident_capability(value: str) -> str:
        if value != REGULAR:
            raise bridge.ProxyRefused("Resident experiment pins the regular loopback service")
        return REGULAR

    try:
        bridge.local_origin = fixed_resident_capability
        app = bridge.create_app(backend="ollama", upstream=REGULAR, capture_dir=capture_dir,
                                synthetic_data_only=True, transport=transport)
    finally:
        bridge.local_origin = ordinary_validator
    app.state.metadata.update(ownership_scope=SCOPE, owns_inference_engine=False)
    return app


def safe_runner_props(props: dict[str, Any]) -> dict[str, Any]:
    """Fixed configuration whitelist; never persist slot prompts or outputs."""
    allowed = ("model_path", "total_slots", "n_ctx", "n_batch", "n_ubatch", "n_parallel",
               "cache_prompt", "swa_full", "flash_attn", "type_k", "type_v")
    result = {key: props[key] for key in allowed if key in props
              and isinstance(props[key], (str, int, float, bool, type(None)))}
    settings = props.get("default_generation_settings")
    if isinstance(settings, dict):
        result["default_generation_settings"] = {
            key: settings[key] for key in ("n_ctx", "n_predict", "n_batch", "n_ubatch", "cache_prompt")
            if key in settings and isinstance(settings[key], (str, int, float, bool, type(None)))}
    return result


async def attest_resident(client: httpx.AsyncClient, runner: str, digest: str) -> dict[str, Any]:
    residents = await lease.residents(client)
    original = lease.verify_original(residents, digest)
    if datetime.fromisoformat(str(original.get("expires_at"))).year < 2100:
        raise ValueError("Original indefinite residency must be present before testing")
    response = await client.get(runner + "/props", timeout=3)
    response.raise_for_status()
    props = response.json()
    weights = Path.home() / ".ollama/models/blobs" / benchmark.candidate.MODEL_DIGEST.replace(":", "-")
    if not isinstance(props, dict) or props.get("model_path") != str(weights):
        raise ValueError("Explicit regular runner does not serve the original pinned weights")
    response = await client.get(runner + "/slots", timeout=3)
    response.raise_for_status()
    slots = response.json()
    if (not isinstance(slots, list) or not slots
            or any(not isinstance(row, dict) or row.get("is_processing") is not False for row in slots)):
        raise ValueError("Original worker is busy or idle state cannot be verified")
    return {"models": lease.metadata(residents), "runner_props": safe_runner_props(props),
            "runner_idle_slots": len(slots), "identity_context_and_idle_verified": True}


async def restore_original(client: httpx.AsyncClient, digest: str,
                           before: list[dict[str, Any]]) -> dict[str, Any]:
    """Restore finite warmup TTL without unloading, then use the existing lease proof."""
    async with asyncio.timeout(120):
        current = await lease.residents(client)
        selected = [row for row in current if row.get("name") == lease.MODEL]
        if selected:
            original = lease.verify_original(current, digest)
            if datetime.fromisoformat(str(original.get("expires_at"))).year < 2100:
                response = await client.post(REGULAR + "/api/generate", json={
                    "model": lease.MODEL, "stream": False, "keep_alive": -1,
                    "options": {"num_ctx": 8192, "num_predict": 1}}, timeout=110)
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or body.get("done") is not True or body.get("error") is not None:
                    raise ValueError("Original indefinite TTL restoration was not acknowledged")
        return await lease.restore(client, digest, before)


async def run(args: argparse.Namespace) -> int:
    if not args.authorise_existing_resident_inference:
        raise ValueError("Explicit authorization to test the existing resident model is required")
    runner = owned_origin(args.regular_runner_origin)
    directory = args.output_dir
    if not directory.is_absolute() or directory.exists() or not directory.parent.is_dir():
        raise ValueError("Use a new absolute evidence directory with an existing parent")
    for path in (lease.BGE, lease.RERANK):
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Installed retrieval checkpoints are required; no download is permitted")
    model = benchmark.candidate.inspect_model(Path.home() / ".ollama/models")
    if model.manifest_sha256 != PINNED_MANIFEST:
        raise ValueError("Installed original model manifest differs from the pinned original")
    directory.mkdir(mode=0o700)
    profile: dict[str, Any] = {"parallel": 1, "phases": [], "procedure_budget_seconds": 600,
        "individual_deadlines_unchanged": True, "pressure_samples": [],
        "preflight": {"required_available_bytes_estimate": EXTRA_HEADROOM_BYTES,
            "memory_estimate_policy": "12_gib_extra_headroom_for_real_retrieval_already_resident_weights",
            "model": asdict(model)}, "retrieval_startup": {}}
    report: dict[str, Any] = {"created_at": datetime.now(timezone.utc).isoformat(), "status": "preflight",
        "scope": SCOPE, "engine": "ollama", "synthetic_data_only": True,
        "runtime_network": "inherited_localhost_only_sandbox", "owns_inference_engine": False,
        "model_files_changed": False, "engine_configuration_changed": False, "profile": profile,
        "hardware_runtime": benchmark.capacity.hardware_metadata(),
        "regular_service": REGULAR, "explicit_runner_origin": runner,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": ["Resident service default SWA/cache/batch configuration differs from owned-engine candidates",
                        "Not an equal-configuration engine comparison or clinical validation",
                        "Initial idle-state verification cannot exclude later requests from other applications",
                        "No summary-refresh, ASR or imaging workload is included"]}
    proxy: subprocess.Popen[str] | None = None
    monitor: asyncio.Task[None] | None = None
    execution: asyncio.Task[None] | None = None
    before: list[dict[str, Any]] = []
    attested = False
    benchmark.qualification.install_synthetic_answer_capture()
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        try:
            report["original_resident_attestation"] = await attest_resident(client, runner, model.manifest_sha256)
            before = await lease.residents(client)
            lease.verify_original(before, model.manifest_sha256)
            attested = True
            initial = benchmark.candidate.inspect_memory()
            profile["preflight"]["memory"] = asdict(initial)
            profile["preflight"]["memory_sample_stage"] = "before_owned_proxy_and_real_retrieval_allocation"
            if initial.estimated_available_bytes < EXTRA_HEADROOM_BYTES:
                report["status"] = "not_run_memory_preflight"
                return 1
            proxy_port = benchmark.qualification.free_port()
            proxy_origin = f"http://127.0.0.1:{proxy_port}"
            with (directory / "proxy.log").open("w") as log:
                proxy = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    "--resident-proxy-child", "--authorise-existing-resident-inference",
                    "--port", str(proxy_port), "--capture-dir", str(directory / "capture")],
                    env=benchmark.offline_environment(), cwd=ROOT, stdout=log,
                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
                profile["owned_proxy_pid"] = proxy.pid
                monitor = asyncio.create_task(benchmark.qualification.monitor_pressure(
                    initial, profile["pressure_samples"], proxy))

                async def execute() -> None:
                    assert proxy is not None
                    await benchmark.proxy_ready(proxy_origin, proxy)
                    async with AsyncExitStack() as stack:
                        prepared = await stack.enter_async_context(benchmark.capacity.orb_stub_target(
                            benchmark.worker_settings("ollama", 1), proxy_origin,
                            embedding_model=str(lease.BGE), reranker_model=str(lease.RERANK),
                            startup_diagnostics=profile["retrieval_startup"]))
                        # Do not let the normal warmup transparently allocate a
                        # missing model after the resident-only admission gate.
                        lease.verify_original(await lease.residents(client), model.manifest_sha256)
                        phase_args = benchmark.parser().parse_args([
                            "--engine", "ollama", "--parallel", "1", "--phases", "quality", "sequential", "burst",
                            "--embedding-model", str(lease.BGE), "--reranker-model", str(lease.RERANK),
                            "--workflow-quality-checks", "--output-dir", str(directory), "--budget-seconds", "600"])
                        await benchmark.phases(proxy_origin, phase_args, profile, prepared)

                async with asyncio.timeout(600):
                    execution = asyncio.create_task(execute())
                    await asyncio.wait((monitor, execution), return_when=asyncio.FIRST_COMPLETED)
                    failure = benchmark.record_task_failures(report, monitor, execution)
                    if failure is not None:
                        raise failure
                    report["status"] = "complete"
        except asyncio.CancelledError:
            report["status"] = "cancelled"
            raise
        except (TimeoutError, RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
            report.update(status="aborted", **benchmark.failure_details(error))
        finally:
            for task in (execution, monitor):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (execution, monitor) if task), return_exceptions=True)
            benchmark.record_task_failures(report, monitor, execution, promote_primary=report["status"] == "aborted")
            await benchmark.qualification.terminate_owned(proxy)
            if attested:
                try:
                    report["restoration"] = await restore_original(client, model.manifest_sha256, before)
                except (TimeoutError, RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                    report["restoration"] = {"status": "failed", "error_type": type(error).__name__}
                    report["status"] = "restoration_failed"
            profile["post_memory"] = asdict(benchmark.candidate.inspect_memory())
            if "memory" in profile["preflight"]:
                profile["system_pressure_summary"] = benchmark.qualification.system_pressure_summary(profile)
            report.update(benchmark.acceptance(report))
            temporary = directory / ".report.tmp"
            temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
            temporary.replace(directory / "report.json")
            benchmark.qualification.progress("resident_benchmark_finished", status=report["status"], report=str(directory / "report.json"))
    return 0 if report["status"] == "complete" else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output-dir", type=Path)
    result.add_argument("--regular-runner-origin")
    result.add_argument("--authorise-existing-resident-inference", action="store_true")
    result.add_argument("--resident-proxy-child", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--port", type=int, help=argparse.SUPPRESS)
    result.add_argument("--capture-dir", type=Path, help=argparse.SUPPRESS)
    return result


async def main() -> int:
    args = parser().parse_args()
    if args.resident_proxy_child:
        if (not args.authorise_existing_resident_inference or args.port is None
                or not 1024 <= args.port <= 65535 or args.port == 11434
                or args.capture_dir is None or not args.capture_dir.is_absolute()):
            raise ValueError("Authorized resident proxy requires a private loopback port and absolute capture directory")
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(resident_proxy_app(args.capture_dir), host="127.0.0.1",
                                              port=args.port, access_log=False, log_level="warning"))
        await server.serve()
        return 0
    if args.output_dir is None or args.regular_runner_origin is None:
        raise ValueError("An evidence directory and explicit current regular runner are required")
    task = asyncio.current_task()
    assert task is not None
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
