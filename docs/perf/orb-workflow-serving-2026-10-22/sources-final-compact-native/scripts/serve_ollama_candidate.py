#!/usr/bin/env python3
"""Inspect or launch a bounded, isolated Ollama 0.40.4 serving candidate.

This is an engineering qualification tool, not a production qualification. It
does not call a model, pull weights, stop another daemon or edit model files.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
from typing import Mapping


GIB = 1024 ** 3
MODEL = "medgemma:27b"
MODEL_DIGEST = "sha256:4b97da6b21a8fb1405a0d1efee462bfd6c4d3c46307bac8b34373835f07ca758"
RUNNER_COMMIT = "b42b7e6d3"
RUNNER_CONFIG = "[default]\nslot-prompt-similarity=0.6\n"


class CandidateRefused(ValueError):
    """The controlled candidate cannot be launched safely on this host."""


@dataclass(frozen=True)
class MemorySnapshot:
    total_bytes: int
    free_percentage: int
    estimated_available_bytes: int
    vm_stat: str


@dataclass(frozen=True)
class ModelSnapshot:
    model: str
    model_digest: str
    weight_bytes: int
    manifest_sha256: str
    quantization: str


def command_output(command: list[str], *, env: Mapping[str, str] | None = None) -> str:
    """Run metadata-only commands; never use a shell or print the environment."""
    try:
        result = subprocess.run(command, env=env, text=True, capture_output=True,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateRefused(f"Cannot inspect {Path(command[0]).name}.") from exc
    if result.returncode:
        raise CandidateRefused(f"Inspection failed: {Path(command[0]).name}.")
    return result.stdout + result.stderr


def executable_version(output: str) -> str:
    """Prefer the executable version over a differently versioned daemon reply."""
    client = re.findall(r"^Warning: client version is ([^\s]+)$", output, re.MULTILINE)
    server = re.findall(r"^ollama version is ([^\s]+)$", output, re.MULTILINE)
    versions = client or server
    if len(versions) != 1:
        raise CandidateRefused("Cannot identify exactly one Ollama executable version.")
    return versions[0]


def parse_memory_snapshot(pressure: str, vm_stat: str) -> MemorySnapshot:
    """Use the OS pressure estimate conservatively, not merely unused pages."""
    total = re.search(r"The system has ([0-9]+) ", pressure)
    free = re.search(r"System-wide memory free percentage: ([0-9]+)%", pressure)
    if total is None or free is None or "page size" not in vm_stat:
        raise CandidateRefused("Cannot read a complete macOS memory-pressure snapshot.")
    total_bytes, free_percentage = int(total[1]), int(free[1])
    if total_bytes < 64 * GIB or not 0 <= free_percentage <= 100:
        raise CandidateRefused("Candidate requires at least 64 GiB and valid pressure data.")
    return MemorySnapshot(total_bytes, free_percentage,
                          total_bytes * free_percentage // 100, vm_stat)


def inspect_memory() -> MemorySnapshot:
    return parse_memory_snapshot(command_output(["/usr/bin/memory_pressure", "-Q"]),
                                 command_output(["/usr/bin/vm_stat"]))


def _read_json(path: Path) -> tuple[dict[str, object], bytes]:
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise CandidateRefused("Missing or oversized cached model metadata.")
    content = path.read_bytes()
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CandidateRefused("Invalid cached model metadata.") from exc
    if not isinstance(value, dict):
        raise CandidateRefused("Cached model metadata must be an object.")
    return value, content


def _cached_blob(models_dir: Path, digest: object) -> Path:
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise CandidateRefused("Invalid cached model digest.")
    blob = models_dir / "blobs" / digest.replace(":", "-")
    if not blob.is_file() or not blob.resolve().is_relative_to(models_dir):
        raise CandidateRefused("Cached model blob is absent or outside the model directory.")
    return blob


def inspect_model(models_dir: Path) -> ModelSnapshot:
    """Read only metadata/stat information for the unchanged installed 27B model."""
    manifest, content = _read_json(models_dir / "manifests" / "registry.ollama.ai" /
                                    "library" / "medgemma" / "27b")
    layers = manifest.get("layers")
    config = manifest.get("config")
    if not isinstance(layers, list) or not isinstance(config, dict):
        raise CandidateRefused("Cached model manifest is incomplete.")
    weights = [layer for layer in layers if isinstance(layer, dict) and
               layer.get("mediaType") == "application/vnd.ollama.image.model"]
    if len(weights) != 1 or weights[0].get("digest") != MODEL_DIGEST:
        raise CandidateRefused("This candidate pins the existing MedGemma 27B weight digest.")
    blob = _cached_blob(models_dir, MODEL_DIGEST)
    weight_bytes = weights[0].get("size")
    if not isinstance(weight_bytes, int) or isinstance(weight_bytes, bool) or weight_bytes != blob.stat().st_size:
        raise CandidateRefused("Cached model size does not match its manifest.")
    details, _ = _read_json(_cached_blob(models_dir, config.get("digest")))
    if (details.get("model_family"), details.get("model_type"), details.get("file_type")) != ("gemma3", "27.4B", "Q4_K_M"):
        raise CandidateRefused("Candidate requires unchanged Gemma3 27.4B/Q4_K_M metadata.")
    return ModelSnapshot(MODEL, MODEL_DIGEST, weight_bytes,
                         hashlib.sha256(content).hexdigest(), "Q4_K_M")


def required_available_bytes(model: ModelSnapshot, parallel: int) -> int:
    """Preflight estimate, not a measured high-water mark or a capacity promise.

    Full f16 8192-token KV is budgeted at 4 GiB per slot, alongside 8 GiB
    RAM-prefix cache, 4 GiB working buffers and 8 GiB additional OS headroom.
    Compact-SWA controls retain this conservative budget. Ongoing pressure
    and actual peak RSS still need monitoring by the benchmark supervisor.
    """
    if parallel not in (1, 2, 4):
        raise CandidateRefused("Candidate parallelism must be 1, 2 or 4.")
    return model.weight_bytes + (4 * parallel + 8 + 4 + 8) * GIB


def controlled_environment(inherited: Mapping[str, str], *, models_dir: Path,
                           run_dir: Path, port: int, parallel: int,
                           swa_full: bool, flash_attention: str) -> dict[str, str]:
    """Do not inherit engine overrides, proxies, diagnostic prompt logging or XDG INI."""
    env = {key: value for key, value in inherited.items()
           if not key.upper().startswith(("OLLAMA_", "LLAMA_", "GGML_"))
           and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "XDG_CONFIG_HOME"}}
    env.update({
        "OLLAMA_HOST": f"127.0.0.1:{port}", "OLLAMA_MODELS": str(models_dir),
        "OLLAMA_NO_CLOUD": "1", "OLLAMA_NOPRUNE": "1", "OLLAMA_NUM_PARALLEL": str(parallel),
        "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_MAX_QUEUE": str(parallel),
        "OLLAMA_CONTEXT_LENGTH": "8192", "OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_KV_CACHE_TYPE": "f16",
        "OLLAMA_DEBUG_LOG_REQUESTS": "0", "LLAMA_ARG_SWA_FULL": "1" if swa_full else "0",
        "LLAMA_ARG_KV_UNIFIED": "false", "LLAMA_ARG_CACHE_RAM": "8192",
        "LLAMA_ARG_CONT_BATCHING": "true", "LLAMA_ARG_CACHE_PROMPT": "true",
        "LLAMA_ARG_CACHE_IDLE_SLOTS": "true",
        "LLAMA_ARG_FIT": "off", "LLAMA_ARG_OFFLINE": "true",
        "XDG_CONFIG_HOME": str(run_dir / "xdg"),
    })
    if flash_attention not in ("auto", "on", "off"):
        raise CandidateRefused("Flash attention must be auto, on or off.")
    # Ollama's BoolWithDefault treats the literal string 'auto' as true.
    # An absent variable selects the runner's genuine automatic policy.
    if flash_attention != "auto":
        env["OLLAMA_FLASH_ATTENTION"] = "1" if flash_attention == "on" else "0"
    return env


def check_port(port: int) -> None:
    if port == 11434 or not 1024 <= port <= 65535:
        raise CandidateRefused("Use an explicit unprivileged loopback port other than 11434.")
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise CandidateRefused("This environment does not permit inspecting loopback binding.") from exc
        raise CandidateRefused("Candidate port is occupied; no other process will be stopped.") from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--port", type=int, required=True)
    result.add_argument("--parallel", type=int, choices=(1, 2, 4), default=1)
    result.add_argument("--swa-full", choices=("on", "off"), default="on")
    result.add_argument("--flash-attention", choices=("auto", "on", "off"), default="auto")
    result.add_argument("--models-dir", type=Path, required=True)
    result.add_argument("--run-dir", type=Path, required=True,
                        help="New private directory; never an existing configuration directory")
    result.add_argument("--ollama", type=Path, default=Path(shutil.which("ollama") or "/nonexistent/ollama"))
    result.add_argument("--execute", action="store_true", help="Launch after successful inspection; otherwise print plan only")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise CandidateRefused("This pinned candidate is for Darwin arm64 only.")
        check_port(args.port)
        if not args.models_dir.is_absolute() or not args.models_dir.is_dir():
            raise CandidateRefused("--models-dir must be an existing absolute cache directory.")
        models_dir = args.models_dir.resolve()
        run_dir = args.run_dir
        if not run_dir.is_absolute() or run_dir.exists() or run_dir.is_symlink() or not run_dir.parent.is_dir():
            raise CandidateRefused("--run-dir must name a new absolute directory with an existing parent.")
        run_dir = run_dir.parent.resolve() / run_dir.name
        binary = args.ollama.resolve(strict=True)
        runner = binary.with_name("llama-server")
        if not runner.is_file():
            raise CandidateRefused("Cannot inspect the sibling llama-server binary.")
        env = controlled_environment(os.environ, models_dir=models_dir, run_dir=run_dir,
                                     port=args.port, parallel=args.parallel,
                                     swa_full=args.swa_full == "on", flash_attention=args.flash_attention)
        version = command_output([str(binary), "--version"], env=env)
        if executable_version(version) != "0.40.4":
            raise CandidateRefused("Candidate requires exactly Ollama 0.40.4.")
        runner_version = command_output([str(runner), "--version"], env=env).strip()
        if f"commit {RUNNER_COMMIT})" not in runner_version:
            raise CandidateRefused("Candidate requires the inspected llama-server b42b7e6d3 build.")
        model, memory = inspect_model(models_dir), inspect_memory()
        required = required_available_bytes(model, args.parallel)
        plan = {"status": "candidate_not_qualified", "ollama_version": "0.40.4",
                "runner_version": runner_version, "model": asdict(model), "memory": asdict(memory),
                "required_available_bytes_estimate": required, "memory_guard_passed": memory.estimated_available_bytes >= required,
                "context_per_request": 8192, "parallel": args.parallel, "swa_full": args.swa_full,
                "flash_attention": args.flash_attention, "kv_type": "f16", "origin": f"http://127.0.0.1:{args.port}",
                "config_sha256": hashlib.sha256(RUNNER_CONFIG.encode()).hexdigest(),
                "quality_policy": "Unchanged model digest, sampling, clinical prompts and request output budgets; no speculative decoding."}
        print(json.dumps(plan, indent=2), flush=True)
        if not plan["memory_guard_passed"]:
            raise CandidateRefused("Insufficient estimated memory headroom; candidate was not launched. Do not unload another user's model to bypass this guard.")
        if args.execute:
            run_dir.mkdir(mode=0o700)
            config_dir = run_dir / "xdg" / "llama.cpp"
            config_dir.mkdir(parents=True, mode=0o700)
            (config_dir / "config.ini").write_text(RUNNER_CONFIG)
            (run_dir / "candidate.json").write_text(json.dumps(plan, indent=2) + "\n")
            os.execve(binary, [str(binary), "serve"], env)
        return 0
    except (CandidateRefused, OSError) as exc:
        print(f"Orb serving candidate refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
