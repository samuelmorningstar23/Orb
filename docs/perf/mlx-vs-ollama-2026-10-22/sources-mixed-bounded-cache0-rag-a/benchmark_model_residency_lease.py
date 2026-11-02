#!/usr/bin/env python3
"""Explicitly authorised temporary residency release, with independent restoration.

Only the installed regular 27B model is unloaded. No model files, live Orb
records or daemon configuration are changed. All test engines remain owned and
isolated. Run under offline_benchmark.sb; this file never enables the network.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import benchmark_mlx_ollama as benchmark
import httpx

ROOT = Path(__file__).resolve().parents[1]
REGULAR = "http://127.0.0.1:11434"
MODEL = benchmark.candidate.MODEL
MODEL_DIR = Path("/private/tmp/orb-mlx-benchmark-20261021/model-mixed46-b")
MLX_PYTHON = Path("/private/tmp/orb-mlx-benchmark-20261021/venv/bin/python")
EVIDENCE = ROOT / "docs/perf/mlx-vs-ollama-2026-10-21"
BGE = Path("/Users/samuelmorningstar/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181")
RERANK = Path("/Users/samuelmorningstar/.cache/huggingface/hub/models--BAAI--bge-reranker-v2-m3/snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e")


def metadata(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: row.get(key) for key in ("name", "digest", "size", "size_vram", "context_length", "expires_at")}
            for row in models]


async def residents(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get(REGULAR + "/api/ps", timeout=3)
    response.raise_for_status()
    data = response.json()
    rows = data.get("models") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Regular-service residency metadata is malformed")
    return rows


def verify_original(models: list[dict[str, Any]], digest: str) -> dict[str, Any]:
    selected = [row for row in models if row.get("name") == MODEL]
    if (len(selected) != 1 or selected[0].get("digest") != digest
            or selected[0].get("context_length") != 8192):
        raise ValueError("Regular 27B model identity or context does not match the original")
    return selected[0]


def verify_others(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    for original in before:
        if original.get("name") == MODEL:
            continue
        selected = [row for row in after if row.get("name") == original.get("name")]
        if (len(selected) != 1 or any(selected[0].get(key) != original.get(key)
                                    for key in ("digest", "context_length"))):
            raise ValueError("Another original resident changed; no other model was controlled by the lease")


async def release(client: httpx.AsyncClient) -> None:
    response = await client.post(REGULAR + "/api/generate", json={
        "model": MODEL, "stream": False, "keep_alive": 0}, timeout=30)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("error") is not None or body.get("done") is not True:
        raise ValueError("Regular service did not acknowledge the authorised unload")
    for _ in range(60):
        if not any(row.get("name") == MODEL for row in await residents(client)):
            return
        await asyncio.sleep(.25)
    raise TimeoutError("Regular 27B residency did not clear after unload")


async def restore(client: httpx.AsyncClient, digest: str,
                  before: list[dict[str, Any]]) -> dict[str, Any]:
    # This deadline is independent of any expired benchmark timeout.
    async with asyncio.timeout(120):
        four_name = "medgemma:4b"
        four_digest = "9fe4e9a6c9bda2d007bb595515550eafbc8e226622adff9257f3241eba334c4d"
        originals_four = [row for row in before if row.get("name") == four_name]
        auto_reloaded: list[dict[str, Any]] = []

        def verify_restoration(rows: list[dict[str, Any]], *, allow_missing_four: bool) -> bool:
            original = verify_original(rows, digest)
            if datetime.fromisoformat(str(original.get("expires_at"))).year < 2100:
                raise ValueError("Original indefinite model residency was not restored")
            # Other missing or changed originals must fail before we consider
            # the one narrowly authorised recovery exception below.
            verify_others([row for row in before if row.get("name") != four_name], rows)
            if not originals_four:
                return False
            if (len(originals_four) != 1 or originals_four[0].get("digest") != four_digest
                    or originals_four[0].get("context_length") != 4096
                    or datetime.fromisoformat(str(originals_four[0].get("expires_at"))).year < 2100):
                raise ValueError("Original4B recovery identity, context or indefinite residency is not pinned")
            selected = [row for row in rows if row.get("name") == four_name]
            if not selected and allow_missing_four:
                return True
            if (len(selected) != 1 or selected[0].get("digest") != four_digest
                    or selected[0].get("context_length") != 4096):
                raise ValueError("Original4B resident is missing or has a different identity or context")
            if datetime.fromisoformat(str(selected[0].get("expires_at"))).year < 2100:
                raise ValueError("Original4B indefinite residency was not restored")
            return False

        current = await residents(client)
        if not any(row.get("name") == MODEL for row in current):
            response = await client.post(REGULAR + "/api/generate", json={
                "model": MODEL, "prompt": "Orb synthetic model residency restoration.",
                "stream": False, "keep_alive": -1,
                "options": {"num_ctx": 8192, "num_predict": 1}}, timeout=110)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("error") is not None or body.get("done") is not True:
                raise ValueError("Regular model restoration was not acknowledged")
        current = await residents(client)
        if verify_restoration(current, allow_missing_four=True):
            response = await client.get(REGULAR + "/api/tags", timeout=3)
            response.raise_for_status()
            tags = response.json()
            installed = tags.get("models") if isinstance(tags, dict) else None
            if not isinstance(installed, list) or any(not isinstance(row, dict) for row in installed):
                raise ValueError("Installed model tags cannot attest the original4B identity")
            selected_tags = [row for row in installed if row.get("name") == four_name]
            if len(selected_tags) != 1 or selected_tags[0].get("digest") != four_digest:
                raise ValueError("Installed original4B name or digest differs; no recovery load was attempted")
            # A concurrent requester may have repopulated the resident while
            # /api/tags was checked. Never overwrite a mismatched resident.
            current = await residents(client)
            if verify_restoration(current, allow_missing_four=True):
                response = await client.post(REGULAR + "/api/generate", json={
                    "model": four_name, "prompt": "Orb synthetic original4B residency restoration.",
                    "stream": False, "keep_alive": -1,
                    "options": {"num_ctx": 4096, "num_predict": 1}}, timeout=110)
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or body.get("error") is not None or body.get("done") is not True:
                    raise ValueError("Original4B restoration was not acknowledged")
                auto_reloaded.append({"name": four_name, "digest": four_digest,
                                      "context_length": 4096, "indefinite_residency_verified": True})
        current = await residents(client)
        verify_restoration(current, allow_missing_four=False)
        verify_others(before, current)
        return {"status": "restored_and_verified", "models": metadata(current),
                "identity_and_context_verified": True, "other_original_residents_unchanged": True,
                "all_original_residents_identity_and_context_verified": True,
                "auto_reloaded_original_residents": auto_reloaded}


def passed_quality(report: dict[str, Any]) -> bool:
    return report.get("status") == "complete" and any(
        phase.get("name") == "quality" and phase.get("all_cases_passed") is True
        for phase in report.get("profile", {}).get("phases", []))


def profile_args(directory: Path, engine: str, parallel: int,
                 phases: list[str], *, full_rag: bool = False,
                 model_dir: Path = MODEL_DIR, policy: Path = EVIDENCE / "mlx-candidate-policy.json",
                 loading_mode: str = "standard", prefix_cache_size: int = 10,
                 diagnose_native_penalties: bool = False,
                 distribution_oracle: Path | None = None) -> argparse.Namespace:
    capture = EVIDENCE / ("full-rag-input-capture-200-a" if "replay200" in phases else "full-rag-input-capture-c") / "capture/requests.jsonl"
    argv = ["--engine", engine, "--parallel", str(parallel), "--phases", *phases,
            "--replay-captures", str(capture), "--output-dir", str(directory),
            "--budget-seconds", "600",
            "--allow-model-load", "--allow-heavy-model-load"]
    if full_rag:
        argv += ["--embedding-model", str(BGE), "--reranker-model", str(RERANK)]
    else:
        argv += ["--engine-only"]
    if engine == "mlx":
        argv += ["--model-dir", str(model_dir), "--mlx-python", str(MLX_PYTHON),
                 "--mlx-policy", str(policy), "--mlx-loading-mode", loading_mode]
        argv += ["--mlx-prefix-cache-size", str(prefix_cache_size)]
    if diagnose_native_penalties:
        argv += ["--diagnose-native-penalties"]
    if distribution_oracle is not None:
        argv += ["--distribution-oracle", str(distribution_oracle)]
    return benchmark.parser().parse_args(argv)


async def watch_regular_residency(client: httpx.AsyncClient,
                                 originals: list[dict[str, Any]] | None = None) -> None:
    while True:
        current = await residents(client)
        if any(row.get("name") == MODEL for row in current):
            raise RuntimeError("Regular model was reloaded by another requester; stopping owned benchmarks")
        if originals is not None:
            verify_others(originals, current)
        await asyncio.sleep(1)


async def run(args: argparse.Namespace) -> int:
    if not args.authorise_temporary_unload_and_restore:
        raise ValueError("Explicit user authorisation to unload and restore is required")
    directory = args.output_dir
    if not directory.is_absolute() or directory.exists() or not directory.parent.is_dir():
        raise ValueError("Use a new absolute evidence directory")
    directory.mkdir(mode=0o700)
    report: dict[str, Any] = {"created_at": datetime.now(timezone.utc).isoformat(),
        "status": "preflight", "explicit_user_authorisation": True, "profiles": [],
        "runtime_network": "inherited_localhost_only_sandbox", "model_files_changed": False}
    report["experiment_kind"] = getattr(args, "experiment", "comparison")

    def save() -> None:
        temporary = directory / ".lease-report.tmp"
        temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        temporary.replace(directory / "lease-report.json")

    release_requested = False
    watcher: asyncio.Task[None] | None = None
    execution: asyncio.Task[None] | None = None
    before: list[dict[str, Any]] = []
    digest = benchmark.candidate.inspect_model(Path.home() / ".ollama/models").manifest_sha256
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        try:
            before = await residents(client)
            verify_original(before, digest)
            report["original_residents"] = metadata(before)
            response = await client.get(args.regular_runner_origin + "/props", timeout=3)
            response.raise_for_status()
            props = response.json()
            original_weights = Path.home() / ".ollama/models/blobs" / benchmark.candidate.MODEL_DIGEST.replace(":", "-")
            if not isinstance(props, dict) or props.get("model_path") != str(original_weights):
                raise ValueError("Saved regular runner no longer serves the authorised source weights")
            report["original_runner_identity_verified"] = True
            # /slots response may contain other fields: retain only idle booleans.
            response = await client.get(args.regular_runner_origin + "/slots", timeout=3)
            response.raise_for_status()
            slots = response.json()
            if (not isinstance(slots, list) or not slots
                    or any(not isinstance(row, dict) or row.get("is_processing") is not False for row in slots)):
                raise ValueError("The regular 27B worker is busy or its idle state cannot be verified")
            report["original_worker_idle"] = True
            report["status"] = "release_requested"
            save()
            release_requested = True  # Restore even if unload acknowledgement is ambiguous.
            await release(client)
            report["after_release"] = metadata(await residents(client))
            report["status"] = "benchmarking"
            save()
            watcher = asyncio.create_task(watch_regular_residency(client, before))

            async def run_profile(name: str, engine: str, width: int, phases: list[str],
                                  *, full_rag: bool = False) -> dict[str, Any]:
                path = directory / name
                await benchmark.run(profile_args(path, engine, width, phases, full_rag=full_rag,
                    model_dir=getattr(args, "model_dir", MODEL_DIR),
                    policy=getattr(args, "mlx_policy", EVIDENCE / "mlx-candidate-policy.json"),
                    loading_mode=getattr(args, "mlx_loading_mode", "standard"),
                    prefix_cache_size=getattr(args, "mlx_prefix_cache_size", 10),
                    distribution_oracle=getattr(args, "distribution_oracle", None),
                    diagnose_native_penalties=(getattr(args, "experiment", "comparison") == "baseline-only")))
                result = json.loads((path / "report.json").read_text())
                report["profiles"].append({"name": name, "path": str(path / "report.json"),
                    "status": result["status"], "quality_passed": passed_quality(result)})
                save()
                return result

            async def experiment() -> None:
                kind = getattr(args, "experiment", "comparison")
                if kind == "distribution-only":
                    if getattr(args, "distribution_oracle", None) is None:
                        raise ValueError("Distribution-only experiment requires an immutable source oracle")
                    await run_profile("mlx-p1", "mlx", 1, ["distribution"])
                    await run_profile("ollama-p1", "ollama", 1, ["distribution"])
                    report["experiment_outcome"] = "Token distribution diagnostic only; no timing or quality qualification"
                    return
                if kind == "baseline-only":
                    await run_profile("ollama-p1", "ollama", 1, ["quality", "replay"])
                    report["experiment_outcome"] = "Owned source baseline and input/sampling diagnostics; no MLX timing"
                    return
                if kind == "quality-only":
                    await run_profile("mlx-p1", "mlx", 1, ["quality"])
                    report["experiment_outcome"] = "MLX quality-only diagnostic; no timing or performance qualification"
                    return
                if kind == "full-rag-only":
                    await run_profile("mlx-p8-full-rag", "mlx", 8,
                                      ["quality", "sequential", "burst"], full_rag=True)
                    report["experiment_outcome"] = "Full retrieval resource diagnostic only; inspect individual quality and acceptance flags"
                    return
                if kind == "serving-recheck":
                    bulk = await run_profile("mlx-p8-200", "mlx", 8, ["quality", "replay200"])
                    if not passed_quality(bulk):
                        report["experiment_outcome"] = "Rechecked MLX failed quality; further serving tests omitted"
                        return
                    native = await run_profile("ollama-p2-200", "ollama", 2, ["quality", "replay200"])
                    if native["status"] == "not_run_memory_preflight":
                        # The just-reaped MLX process can precede macOS's
                        # memory-accounting update. One separately recorded
                        # retry uses a fresh sample and identical admission
                        # threshold; never retry an actual resource abort.
                        benchmark.qualification.progress("owned_memory_reclaim_wait", seconds=5)
                        await asyncio.sleep(5)
                        await run_profile("ollama-p2-200-after-reclaim", "ollama", 2, ["quality", "replay200"])
                    await run_profile("mlx-p8-full-rag", "mlx", 8,
                                      ["quality", "sequential", "burst"], full_rag=True)
                    report["experiment_outcome"] = "Transport/startup recheck exercised; individual acceptance flags decide performance"
                    return
                mlx = await run_profile("mlx-p1", "mlx", 1, ["quality", "replay"])
                if not passed_quality(mlx):
                    report["experiment_outcome"] = "MLX not qualified for further timing; inspect first profile"
                    return
                await run_profile("ollama-p1", "ollama", 1, ["quality", "replay"])
                await run_profile("ollama-p2-200", "ollama", 2, ["replay200"])
                for width in (8, 4, 2, 1):
                    bulk = await run_profile(f"mlx-p{width}-200", "mlx", width, ["quality", "replay200"])
                    if bulk["status"] != "not_run_memory_preflight":
                        break
                if not passed_quality(bulk):
                    report["experiment_outcome"] = "Higher-width MLX profile unqualified; combined run omitted"
                    return
                await run_profile(f"mlx-p{width}-full-rag", "mlx", width,
                                  ["quality", "sequential", "burst"], full_rag=True)
                report["experiment_outcome"] = "All planned profile procedures exercised; individual acceptance flags decide performance"

            # Quality plus three individually bounded60s replays can exceed
            #300s on this host. This outer procedure allowance never changes
            #the per-request60s/20s deadlines or3/5/15s acceptance targets.
            async with asyncio.timeout(2400):
                execution = asyncio.create_task(experiment())
                done, _ = await asyncio.wait((watcher, execution), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
                report["status"] = "experiment_finished"
        except (Exception, asyncio.CancelledError) as error:
            report.update(status="aborted", error_type=type(error).__name__, error=str(error)[:1024])
        finally:
            async def cleanup_and_restore() -> None:
                for cleanup_task in (execution, watcher):
                    if cleanup_task is not None and not cleanup_task.done():
                        cleanup_task.cancel()
                await asyncio.gather(*(cleanup_task for cleanup_task in (execution, watcher)
                                       if cleanup_task is not None), return_exceptions=True)
                if release_requested:
                    try:
                        report["restoration"] = await restore(client, digest, before)
                    except Exception as error:
                        report["restoration"] = {"status": "FAILED_REQUIRES_ATTENTION", "error_type": type(error).__name__, "error": str(error)[:1024]}
                save()

            recovery = asyncio.create_task(cleanup_and_restore())
            while not recovery.done():
                try:
                    await asyncio.shield(recovery)
                except asyncio.CancelledError:
                    report["cancellation_during_recovery"] = True
            await recovery
            save()
            print(json.dumps({"event": "residency_lease_finished", "status": report["status"],
                "restoration": report.get("restoration", {}).get("status"), "report": str(directory / "lease-report.json")}), flush=True)
    return 0 if (report["status"] == "experiment_finished"
                 and report.get("restoration", {}).get("status") == "restored_and_verified") else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--regular-runner-origin", required=True)
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    parser.add_argument("--mlx-policy", type=Path, default=EVIDENCE / "mlx-candidate-policy.json")
    parser.add_argument("--mlx-loading-mode", choices=tuple(benchmark.MLX_LAUNCHERS), default="standard")
    parser.add_argument("--mlx-prefix-cache-size", type=int, choices=(0, 10), default=10)
    parser.add_argument("--experiment", choices=("comparison", "baseline-only", "quality-only", "distribution-only", "serving-recheck", "full-rag-only"), default="comparison")
    parser.add_argument("--distribution-oracle", type=Path)
    parser.add_argument("--authorise-temporary-unload-and-restore", action="store_true")
    args = parser.parse_args()
    from ollama_template_oracle import owned_origin
    args.regular_runner_origin = owned_origin(args.regular_runner_origin)
    # Refuse an incomplete or mismatched candidate before pausing Orb's model.
    if args.experiment != "baseline-only":
        benchmark.validate_mlx_candidate(args.model_dir, args.mlx_policy, loading_mode=args.mlx_loading_mode)
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
