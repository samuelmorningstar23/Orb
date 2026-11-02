#!/usr/bin/env python3
"""Bounded, PHI-free Orb capacity acceptance runner.

The default target is a disposable synthetic HTTP fixture, never the user's Orb.
Mock results exercise HTTP/SSE/auth/scope and the runner; they are NOT model or
hospital capacity evidence. See docs/engineering/capacity-load-test.md.
"""
from __future__ import annotations

import argparse
import asyncio
import codecs
import hashlib
import inspect
import ipaddress
import json
import math
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
TARGET_SECONDS = {"general": 3.0, "patient": 5.0, "department": 15.0}
SOURCE_FILES = (
    "scripts/loadtest_capacity.py", "scripts/loadtest_capacity_fixture.py",
    "scripts/capacity_profiler.py",
    "scripts/capacity_retrieval_fixture.py", "scripts/e2e_server.py",
    "server/main.py", "server/config.py", "server/database.py", "server/request_admission.py",
    "server/services/local_inference_pool.py", "server/services/ollama_gateway.py",
    "server/services/local_inference_routing.py", "server/services/llm_providers.py",
    "server/services/local_model_http.py",
    "server/services/disconnect_stream.py",
    "server/services/model_router.py",
    "server/services/ai_governance.py",
    "server/services/sage_retrieval_executor.py",
    "server/services/clinical_knowledge.py", "server/services/knowledge_worker.py", "server/services/local_transformers.py",
    "server/services/thinking_mode.py", "server/services/sage_service.py",
    "server/services/llm_queue_service.py", "server/services/gpu_coordinator.py",
    "server/services/patient_summary_service.py", "server/routes/sage_routes.py",
    "server/routes/patient_summary_routes.py", "server/migrations_his/m0089_patient_summary_snapshots.py",
    "server/migrations_his/m0090_sage_retrieval_indexes.py",
    "server/auth.py", "server/permissions.py", "server/db_backend.py",
)


class ProtocolError(ValueError):
    pass


class SSEDecoder:
    """Incremental UTF-8 SSE decoder, including split CRLF/multibyte frames."""

    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.data: list[str] = []
        self.event = "message"
        self.frame_bytes = 0

    def feed(self, chunk: bytes, *, final: bool = False) -> list[tuple[str, str]]:
        try:
            self.buffer += self.decoder.decode(chunk, final=final)
        except UnicodeDecodeError as exc:
            raise ProtocolError("invalid_utf8") from exc
        output = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            output.extend(self._line(line.removesuffix("\r")))
        if final and (self.buffer or self.data):
            # An unterminated event is not an acknowledged successful response.
            raise ProtocolError("unterminated_sse_frame")
        if len(self.buffer) + self.frame_bytes > 1_000_000:
            raise ProtocolError("oversize_sse_frame")
        return output

    def _line(self, line: str) -> list[tuple[str, str]]:
        if not line:
            output = [(self.event, "\n".join(self.data))] if self.data else []
            self.data, self.event, self.frame_bytes = [], "message", 0
            return output
        self.frame_bytes += len(line)
        if line.startswith(":"):
            return []
        key, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if key == "data":
            self.data.append(value)
        elif key == "event":
            self.event = value
        return []


@dataclass
class StreamObservation:
    first_content_s: float | None = None
    first_readable_s: float | None = None
    completion_s: float | None = None
    terminal: bool = False
    error: str | None = None
    content_chars: int = 0
    content_events: int = 0
    queue_ms: float | None = None
    retrieval_ms: float | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    provider: str | None = None
    cache_hit: bool = False
    _prefix: str = field(default="", repr=False)
    _digest: Any = field(default_factory=hashlib.sha256, repr=False)

    def accept(self, event: str, raw: str, elapsed: float) -> None:
        if self.terminal:
            raise ProtocolError("event_after_terminal")
        if raw == "[DONE]":
            self.terminal, self.completion_s = True, elapsed
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolError("invalid_sse_json") from exc
        if not isinstance(data, dict):
            raise ProtocolError("nonobject_sse_event")
        if event == "error" or data.get("error") or data.get("code") in {
            "SAGE_STREAM_FAILED", "SAGE_CAPACITY_REJECTED", "inference_overloaded",
        }:
            # Keep a fixed machine category, never the upstream error/PHI text.
            self.error = "capacity_rejected" if data.get("code") == "SAGE_CAPACITY_REJECTED" else "stream_error"
        for source in (data, data.get("timings"), data.get("timing"), data.get("metadata")):
            if isinstance(source, dict):
                self._metrics(source)
                if isinstance(source.get("timings"), dict):
                    self._metrics(source["timings"])
                provider = source.get("provider")
                if isinstance(provider, dict):
                    self.model = str(provider.get("model") or "") or self.model
                    self.provider = str(provider.get("provider") or provider.get("lane") or "") or self.provider
                    self._metrics(provider)
        content = data.get("content")
        if isinstance(content, str) and content:
            self.first_content_s = self.first_content_s if self.first_content_s is not None else elapsed
            self.content_chars += len(content)
            self.content_events += 1
            self._digest.update(content.encode())
            # A spinner, stage event or markdown heading isn't a readable answer.
            # This is an objective display milestone, not clinical usefulness.
            if self.first_readable_s is None:
                self._prefix = (self._prefix + content)[-1024:]
                prose = " ".join(
                    line for line in self._prefix.splitlines()
                    if line.strip() and not line.strip().startswith(("#", "**"))
                )
                if len(prose.strip()) >= 32 and len(prose.split()) >= 4:
                    self.first_readable_s = elapsed
        if data.get("done") is True:
            self.terminal, self.completion_s = True, elapsed
        elif data.get("terminal") is True:
            self.error = self.error or "unsuccessful_terminal"
            self.terminal, self.completion_s = True, elapsed

    def _metrics(self, source: dict) -> None:
        for destination, aliases in {
            "queue_ms": ("queue_ms", "queue_wait_ms", "admission_wait_ms"),
            "retrieval_ms": ("retrieval_ms",),
            "prompt_tokens": ("prompt_eval_count", "prompt_tokens", "input_tokens"),
            "output_tokens": ("eval_count", "output_tokens", "completion_tokens"),
        }.items():
            for name in aliases:
                value = source.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    setattr(self, destination, value)
                    break
        self.cache_hit = self.cache_hit or source.get("cache_hit") is True or source.get("cached") is True

    def report(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self) if not item.name.startswith("_")}


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * quantile) - 1)]


def distribution(values: list[float]) -> dict:
    return {"samples": len(values), "p50_s": percentile(values, .5), "p95_s": percentile(values, .95),
            "p99_s": percentile(values, .99), "max_s": max(values) if values else None}


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use an HTTP(S) origin without credentials/query/fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("base URL must be an origin, not a path")
    host = parsed.hostname or ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Do not resolve arbitrary domains or follow DNS rebinding to a live host.
        local = host == "localhost"
    if not local:
        raise ValueError("Only explicit loopback targets are supported; use an SSH tunnel to a disposable test host")


def validate_safety(args: argparse.Namespace, manifest: dict | None) -> None:
    if bool(args.embedding_model) != bool(args.reranker_model):
        raise ValueError("Provide both installed embedding and reranker snapshots")
    if args.embedding_model:
        if not (args.orb_stub or args.orb_model_origin):
            raise ValueError("Installed retrieval snapshots require an owned Orb fixture")
        from capacity_retrieval_fixture import snapshot_proof
        snapshot_proof(args.embedding_model)
        snapshot_proof(args.reranker_model, reranker=True)
    if args.profile_fixture and not args.orb_stub:
        raise ValueError("Profiling is supported only by the disposable fake-model Orb fixture")
    if args.profile_stages_only and not args.profile_fixture:
        raise ValueError("Stage-only profiling requires --profile-fixture")
    if args.fixture_thread_workers is not None and not args.orb_stub:
        raise ValueError("Diagnostic thread overrides require the disposable fake-model Orb fixture")
    if args.requests < 1 or args.concurrency < 1 or args.concurrency > 200 or args.requests > 10_000:
        raise ValueError("requests must be 1..10000 and concurrency 1..200")
    if args.timeout <= 0 or args.timeout > 300:
        raise ValueError("timeout must be >0 and <=300 seconds")
    if args.synthetic or args.orb_stub or args.orb_model_origin:
        if args.base_url or manifest:
            raise ValueError("A fixture creates its own disposable target; do not combine an external target")
        if args.orb_model_origin:
            validate_url(args.orb_model_origin)
            if not args.allow_model_load:
                raise ValueError("Installed-model fixture requires --allow-model-load")
            if (args.concurrency > 4 or args.requests > 12) and not args.allow_heavy_model_load:
                raise ValueError("Model probes default to <=4 concurrent/12 total; a dedicated host requires --allow-heavy-model-load")
        return
    if not args.base_url or not manifest or not args.allow_nondisposable or not args.ack_synthetic_data:
        raise ValueError("External targets require --base-url, --manifest, --allow-nondisposable and --ack-synthetic-data")
    validate_url(args.base_url)
    if manifest.get("synthetic_data_only") is not True:
        raise ValueError("Manifest must explicitly attest synthetic_data_only: true")
    if args.mode == "fresh-ai" and not args.allow_model_load:
        raise ValueError("Fresh AI target requires --allow-model-load; this may use model compute")
    if args.mode == "mixed" and not args.allow_model_load:
        raise ValueError("Mixed target includes AI; requires --allow-model-load")
    if args.allow_model_load and (args.concurrency > 4 or args.requests > 12) and not args.allow_heavy_model_load:
        raise ValueError("Model probes default to <=4 concurrent/12 total; a dedicated host requires --allow-heavy-model-load")


def make_plan(count: int, mode: str, manifest: dict, run_id: str) -> list[dict]:
    identities = manifest.get("identities") or []
    if not identities:
        raise ValueError("At least one scoped identity is required")
    plan = []
    for index in range(count):
        identity = identities[index % len(identities)]
        if identity.get("role") not in {"doctor", "nurse", "department_head", "consultant"}:
            raise ValueError("Each identity must declare its real clinical role")
        pattern = ("general", "patient", "department") if mode == "fresh-ai" else (
            ("summary",) if mode == "summary" else ("general", "patient", "department", "summary", "read")
        )
        lane = pattern[index % len(pattern)]
        patients = identity.get("patient_ids") or []
        if lane in {"patient", "summary"} and not patients:
            raise ValueError("Patient/summary traffic requires authorised synthetic patient_ids")
        if lane == "department" and not identity.get("department"):
            raise ValueError("Department traffic requires an authorised department")
        patient_id = patients[(index // len(pattern)) % len(patients)] if patients and lane in {"patient", "summary"} else None
        request_id = f"capacity-{run_id}-{index:05d}"
        query = {
            "general": "What are the common side effects of metoprolol?",
            "patient": "Summarise this patient's recorded observations, medication changes and unresolved tasks; separate documented facts from missing data.",
            "department": "Which patients in my ward need review? Summarise documented outstanding work and escalation priorities, identifying missing information.",
        }.get(lane, "")
        if query:
            # Unique query AND history thread defeat answer/inflight dedup. The
            # fixture varies the seeded chart; changing nonce alone isn't called
            # a diverse hospital workload in the report.
            # Avoid words such as "reference/case/record <nonce>": those are
            # deliberately parsed as patient locators by Orb's safety router.
            query += f" (evaluation iteration {number_words(index + 1)})"
        body = {"query": query, "patient_id": patient_id, "conversation_id": request_id,
                "thinking": True, "view_context": {"tab": "patient" if patient_id else "department", "patient_id": patient_id,
                                                    "department": identity.get("department")}}
        plan.append({"request_id": request_id, "identity": index % len(identities), "lane": lane,
                     "patient_id": patient_id, "body": body})
    return plan


def number_words(value: int) -> str:
    """A nonce must not resemble a bed locator or patient identifier."""
    small = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
             "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
    tens = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
    if value < 20:
        return small[value]
    if value < 100:
        return tens[value // 10] + (" " + small[value % 10] if value % 10 else "")
    scale, label = (1000, "thousand") if value >= 1000 else (100, "hundred")
    return number_words(value // scale) + " " + label + (" " + number_words(value % scale) if value % scale else "")


async def execute_one(client: httpx.AsyncClient, base_url: str, item: dict, identity: dict,
                      manifest: dict, timeout: float, require_model: bool) -> dict:
    started = time.perf_counter()
    result = {"request_id": item["request_id"], "lane": item["lane"], "role": identity["role"],
              "status": None, "success": False, "error": None, "full_s": None}
    # No token, query, patient/case identifier or clinical answer is persisted.
    token = identity.get("_token")
    headers = {"Authorization": f"Bearer {token}", "X-Orb-Capacity-Request": item["request_id"]}
    lane = item["lane"]
    observation = None
    try:
        async with asyncio.timeout(timeout):
            if lane not in TARGET_SECONDS:
                path = (manifest.get("summary_path_template", "/api/patients/{patient_id}/summary").format(patient_id=item["patient_id"])
                        if lane == "summary" else manifest.get("read_path", "/api/vigil/patients"))
                response = await client.get(base_url + path, headers=headers, timeout=timeout)
                result["status"] = response.status_code
                if response.status_code != 200:
                    result["error"] = f"http_{response.status_code}"
                elif lane == "summary":
                    data = response.json()
                    generated = data.get("generated_at")
                    captured = data.get("source_captured_at")
                    if data.get("patient_id") != item["patient_id"]:
                        result["error"] = "summary_patient_mismatch"
                    elif data.get("status") != "ready" or not isinstance(data.get("summary"), dict):
                        result["error"] = "summary_not_ready"
                    elif not isinstance(generated, str):
                        result["error"] = "summary_timestamp_missing"
                    elif not isinstance(captured, str):
                        result["error"] = "summary_source_timestamp_missing"
                    else:
                        dt = datetime.fromisoformat(captured.replace("Z", "+00:00"))
                        generated_dt = datetime.fromisoformat(generated.replace("Z", "+00:00"))
                        if dt.tzinfo is None or generated_dt.tzinfo is None:
                            raise ValueError("summary_timestamp_has_no_timezone")
                        now = datetime.now(timezone.utc)
                        age = (now - dt).total_seconds()
                        result["summary_source_age_s"] = round(age, 3)
                        result["summary_generation_age_s"] = round((now - generated_dt).total_seconds(), 3)
                        result["summary_source_version"] = data.get("source_version")
                        # A long generation does not reset the age of its
                        # source snapshot. At 1800 seconds it is expired.
                        result["success"] = -5 <= age < 1800 and generated_dt >= dt
                        if not result["success"]:
                            result["error"] = "summary_stale_or_clock_skew"
                else:
                    data = response.json()
                    result["success"] = isinstance(data, (dict, list)) and not (isinstance(data, dict) and data.get("error"))
            else:
                observation = StreamObservation()
                parser = SSEDecoder()
                async with client.stream("POST", base_url + manifest.get("query_path", "/api/sage/query"),
                                         headers=headers, json=item["body"], timeout=timeout) as response:
                    result["status"] = response.status_code
                    if response.status_code != 200:
                        result["error"] = f"http_{response.status_code}"
                    elif "text/event-stream" not in response.headers.get("content-type", ""):
                        result["error"] = "not_sse"
                    else:
                        async for chunk in response.aiter_bytes():
                            for event, data in parser.feed(chunk):
                                observation.accept(event, data, time.perf_counter() - started)
                        parser.feed(b"", final=True)
                        result.update(observation.report())
                        result["answer_sha256"] = observation._digest.hexdigest()
                        result["success"] = (observation.terminal and observation.error is None and
                                             observation.first_readable_s is not None and not observation.cache_hit)
                        if not result["success"]:
                            result["error"] = observation.error or ("fresh_ai_returned_cache" if observation.cache_hit else "incomplete_or_empty_stream")
                        elif require_model and not observation.model:
                            result["success"], result["error"] = False, "model_provenance_missing"
    except (httpx.HTTPError, TimeoutError) as exc:
        result["error"] = "timeout" if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else "transport_error"
    except (ProtocolError, ValueError, TypeError):
        result["error"] = "invalid_response_protocol"
    finally:
        if observation is not None:
            error = result["error"]
            result.update(observation.report())
            result["error"] = error or observation.error
            result["answer_sha256"] = observation._digest.hexdigest()
    result["full_s"] = time.perf_counter() - started
    if lane in TARGET_SECONDS:
        limit = TARGET_SECONDS[lane]
        result["target_s"] = limit
        result["first_readable_within_target"] = bool(result["success"] and result.get("first_readable_s") is not None and result["first_readable_s"] < limit)
        result["completed_within_target"] = bool(result["success"] and result["full_s"] < limit)
    return result


def summarise(results: list[dict], wall: float) -> dict:
    lanes = {}
    for lane in sorted({row["lane"] for row in results}):
        rows = [row for row in results if row["lane"] == lane]
        success = [row for row in rows if row["success"]]
        lanes[lane] = {"requests": len(rows), "successful": len(success),
                       "failures": dict(Counter(row["error"] or "unknown" for row in rows if not row["success"])),
                       "full": distribution([row["full_s"] for row in success]),
                       "terminal_completed_answer": distribution([row["completion_s"] for row in success if row.get("completion_s") is not None]),
                       "post_terminal_stream_close": distribution([max(0.0, row["full_s"] - row["completion_s"]) for row in success if row.get("completion_s") is not None]),
                       "first_readable": distribution([row["first_readable_s"] for row in success if row.get("first_readable_s") is not None]),
                       "queue_wait": distribution([row["queue_ms"] / 1000 for row in rows if row.get("queue_ms") is not None]),
                       "retrieval": distribution([row["retrieval_ms"] / 1000 for row in rows if row.get("retrieval_ms") is not None])}
        if lane in TARGET_SECONDS:
            lanes[lane].update(target_s=TARGET_SECONDS[lane],
                               first_readable_meeting_target=sum(row["first_readable_within_target"] for row in rows),
                               full_meeting_target=sum(row["completed_within_target"] for row in rows),
                               all_requests_meet_target=all(row["completed_within_target"] for row in rows))
    success = sum(row["success"] for row in results)
    ai = [row for row in results if row["lane"] in TARGET_SECONDS]
    return {"requests": len(results), "successful": success, "failed": len(results) - success,
            "http_status_counts": dict(Counter(str(row["status"]) for row in results)),
            "wall_s": wall, "completed_successfully_per_second": success / wall if wall else None,
            "all_ai_requests_meet_completed_answer_targets": bool(ai) and all(row["completed_within_target"] for row in ai), "lanes": lanes}


async def run_burst(base_url: str, manifest: dict, *, mode: str, concurrency: int, requests: int,
                    timeout: float, require_model: bool, run_id: str,
                    result_observer: Callable[[dict], object] | None = None) -> tuple[dict, list[dict]]:
    """Return a complete cohort, optionally retaining returned rows as they arrive.

    The observer is a synchronous, promptly returning diagnostic callback. Its
    time is included in cohort wall time. It receives a copy, cannot change the
    returned row, and must not start asynchronous or external work. It is never
    awaited: an accidentally async observer is rejected rather than hanging the
    benchmark. Cancelling the cohort preserves rows already observed without
    manufacturing results for requests that have not returned.
    """
    if result_observer is not None and (not callable(result_observer)
            or any(inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target)
                   for target in (result_observer, type(result_observer).__call__))):
        raise TypeError("Result observer must be a synchronous, promptly returning callback")
    plan = make_plan(requests, mode, manifest, run_id)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    identities = manifest["identities"]
    semaphore = asyncio.Semaphore(concurrency)
    gate = asyncio.Event()
    async with httpx.AsyncClient(limits=limits, follow_redirects=False, trust_env=False) as client:
        for identity in identities:
            token = identity.get("fixture_token") or os.environ.get(identity.get("token_env", ""))
            if not token:
                raise ValueError("Each identity needs an existing token_env (or disposable fixture token)")
            identity["_token"] = token
        async def worker(item):
            await gate.wait()
            async with semaphore:
                row = await execute_one(client, base_url, item, identities[item["identity"]], manifest, timeout, require_model)
                if result_observer is not None:
                    observed = result_observer(dict(row))
                    if observed is not None:
                        # Closing a mistakenly returned coroutine prevents an
                        # unawaited-coroutine warning; never execute its body.
                        if inspect.iscoroutine(observed):
                            observed.close()
                        raise TypeError("Result observer must return None without asynchronous work")
                return row
        tasks = [asyncio.create_task(worker(item)) for item in plan]
        start = time.perf_counter()
        gate.set()
        try:
            results = await asyncio.gather(*tasks)
            wall = time.perf_counter() - start
        finally:
            # gather propagates an observer/error exception without cancelling
            # siblings itself. Always drain owned request tasks before the
            # client/fixture closes, including cancellation by a host guard.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return summarise(results, wall), results


def hardware_metadata() -> dict:
    def command(args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    metadata: dict[str, Any] = {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
                "cpu_count": os.cpu_count(), "client_hardware_only": True,
                "server_hardware_verified": False}
    if platform.system() == "Darwin":
        metadata["memory_bytes"] = command(["sysctl", "-n", "hw.memsize"])
        metadata["chip"] = command(["sysctl", "-n", "machdep.cpu.brand_string"])
    metadata["git_commit"] = command(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    metadata["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                 for name in SOURCE_FILES if (ROOT / name).is_file()}
    metadata["httpx_version"] = httpx.__version__
    return metadata


def startup_diagnostic(directory: Path) -> str:
    """Return only fixed stage/exception identifiers, never raw worker logs."""
    path = directory / "capacity-startup-stage.json"
    try:
        if path.stat().st_size > 4096:
            return "stage=unknown"
        data = json.loads(path.read_text())
        stage = data.get("stage")
        if stage not in {"corpus_initialization", "embedding_probe", "reranker_probe", "full_retrieval_ready"}:
            stage = "unknown"
        error = data.get("exception_type", "unknown")
        if error not in {"RuntimeError", "ValueError", "TypeError", "ImportError", "ModuleNotFoundError",
                          "KnowledgeWorkerError", "MemoryError", "OSError", "AssertionError", "TimeoutError",
                          "KeyboardInterrupt", "SystemExit"}:
            error = "unknown"
        return f"stage={stage}; exception={error}"
    except (OSError, ValueError, AttributeError):
        return "stage=unknown"


_MPS_RELEASE_FIELDS = ("driver_before_bytes", "live_before_bytes", "driver_after_bytes",
                       "live_after_bytes", "released_bytes", "trigger_bytes")
_MPS_RELEASE_LINE = re.compile(
    r"(?:WARNING:orb\.knowledge_worker:)?MPS unused-cache release "
    + " ".join(name + r"=([0-9]{1,19})" for name in _MPS_RELEASE_FIELDS) + r"\Z")
_MPS_RELEASE_LIMIT = 256


def extract_mps_cache_events(lines: Iterable[str]) -> dict[str, Any]:
    """Keep bounded numeric allocator observations; discard all other log data."""
    records = []
    total, released = 0, 0
    for line in lines:
        if not isinstance(line, str) or len(line) > 512:
            continue
        match = _MPS_RELEASE_LINE.fullmatch(line.rstrip("\r\n"))
        if match is None:
            continue
        values = [int(value) for value in match.groups()]
        if any(value > 2 ** 63 - 1 for value in values):
            continue
        row = dict(zip(_MPS_RELEASE_FIELDS, values, strict=True))
        if (row["live_before_bytes"] > row["driver_before_bytes"]
                or row["live_after_bytes"] > row["driver_after_bytes"]
                or row["released_bytes"] != max(0, row["driver_before_bytes"] - row["driver_after_bytes"])):
            continue
        total += 1
        released += row["released_bytes"]
        if len(records) < _MPS_RELEASE_LIMIT:
            records.append(row)
    return {"observation_status": "numeric_events_only", "records": records,
            "total_records": total, "total_released_bytes": released,
            "records_truncated": total > len(records)}


def mps_cache_diagnostic(directory: Path) -> dict[str, Any]:
    """Read only the owned fixture log; observation failure never breaks cleanup."""
    try:
        with (directory / "worker.log").open("r", encoding="utf-8", errors="replace") as log:
            return extract_mps_cache_events(log)
    except Exception:
        # Never retain a path, raw line, exception text or patient data.
        return {"observation_status": "unavailable", "records": [],
                "total_records": 0, "total_released_bytes": 0, "records_truncated": False}


@asynccontextmanager
async def synthetic_target():
    """Ephemeral TCP fixture; no Orb imports, real databases, inference or bots."""
    import uvicorn
    from loadtest_capacity_fixture import create_app, make_manifest
    run_nonce = uuid.uuid4().hex
    fixture = create_app(run_nonce)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(256)
    base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(fixture, log_level="error", access_log=False, lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        if not server.started:
            raise RuntimeError("Disposable fixture did not start")
        yield base_url, make_manifest(run_nonce), fixture
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        sock.close()


@asynccontextmanager
async def orb_stub_target(fixture_settings: dict | None = None, real_origin: str | None = None, *, prewarm_summaries: bool = False,
                          profile: bool = False, profile_stages_only: bool = False, thread_workers: int | None = None,
                          embedding_model: str | None = None, reranker_model: str | None = None,
                          startup_diagnostics: dict | None = None):
    """Real copied Orb, private throwaway DB, local fake inference service.

    This does NOT copy databases, model weights, site credentials or patient
    uploads. Its child and temporary directory are always owned by this run.
    """
    import signal

    import uvicorn
    from e2e_server import (
        copy_application,
        isolated_environment,
        local_inference_environment,
    )
    from loadtest_capacity_fixture import create_mock_ollama
    if bool(embedding_model) != bool(reranker_model):
        raise ValueError("Provide both installed embedding and reranker snapshots")
    if embedding_model and reranker_model:
        from capacity_retrieval_fixture import snapshot_proof
        snapshot_proof(embedding_model)
        snapshot_proof(reranker_model, reranker=True)
    nonce = uuid.uuid4().hex
    mock = create_mock_ollama()
    mock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    mock_socket.bind(("127.0.0.1", 0))
    mock_socket.listen(256)
    inference_url = real_origin or f"http://127.0.0.1:{mock_socket.getsockname()[1]}"
    mock_server = uvicorn.Server(uvicorn.Config(mock, log_level="error", access_log=False, lifespan="off"))
    mock_task = asyncio.create_task(mock_server.serve(sockets=[mock_socket]))
    process = None
    temporary = tempfile.TemporaryDirectory(prefix="orb_capacity_")
    directory = Path(temporary.name)
    try:
        copy_application(ROOT, directory)
        env = (local_inference_environment(directory, inference_url, embedding_model, reranker_model)
               if embedding_model else isolated_environment(directory))
        if embedding_model:
            env["ORB_CAPACITY_FULL_RETRIEVAL"] = "1"
        env.update({"ORB_OLLAMA_BASE_URL": inference_url, "ORB_BACKGROUND_MODELS_ENABLED": "0",
                    "ORB_SAGE_AMBIENT": "false", "ORB_SAGE_PREWARM": "false", "ORB_SAGE_ASSESSMENT_ENABLED": "false",
                    "ORB_SAGE_HISTORY_SUMMARIZE": "0", "ORB_VOICE_AGENT_ENABLED": "false",
                    "ORB_LLM_MODEL": "medgemma:27b", "ORB_MODEL_FALLBACK_ENABLED": "false"})
        if prewarm_summaries:
            env["ORB_SUMMARY_ENABLED"] = "1"
        # Caller settings are an explicit allowlisted fixture configuration,
        # never inherited appliance ORB_* flags.
        allowlist = {"ORB_HTTP_MAX_ACTIVE", "ORB_HTTP_MAX_INFLIGHT", "ORB_HTTP_RESERVED_CONTROL", "ORB_INTERACTIVE_LLM_CONCURRENCY",
                     "ORB_LLM_WORKERS", "ORB_LLM_POOL_MAX_INFLIGHT", "ORB_LLM_INTERACTIVE_CONCURRENCY", "ORB_LLM_INTERACTIVE_MAX_PENDING",
                     "ORB_LLM_INTERACTIVE_ADMISSION_TIMEOUT_SECONDS", "ORB_LLM_WORKERS_JSON",
                     "ORB_OLLAMA_WORKERS_JSON", "ORB_PATIENT_SUMMARY_ENABLED", "ORB_PATIENT_SUMMARIES_ENABLED",
                     "ORB_SUMMARY_ENABLED", "ORB_SUMMARY_REFRESH_SECONDS", "ORB_SUMMARY_WORKERS", "ORB_SUMMARY_SCAN_SECONDS",
                     "ORB_SAGE_QUEUE_WAIT_SECONDS", "ORB_SAGE_RETRIEVAL_WORKERS"}
        for key, value in (fixture_settings or {}).items():
            if key not in allowlist:
                raise ValueError(f"Fixture setting is not allowlisted: {key}")
            env[key] = str(value).replace("{inference_url}", inference_url)
        reserved = socket.socket()
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        reserved.close()
        base_url = f"http://127.0.0.1:{port}"
        log_path = directory / "worker.log"
        with log_path.open("w") as log:
            command = [sys.executable, str(ROOT / "scripts/loadtest_capacity_fixture.py"),
                       "--orb-worker", "--directory", str(directory), "--port", str(port), "--nonce", nonce]
            if prewarm_summaries:
                if real_origin:
                    raise ValueError("Synthetic summary prewarming cannot be combined with real-model qualification")
                command.append("--prewarm-summaries")
            if profile:
                command.append("--profile")
            if profile_stages_only:
                command.append("--profile-stages-only")
            if thread_workers is not None:
                command.extend(("--thread-workers", str(thread_workers)))
            process = subprocess.Popen(command,
                                       cwd=directory / "server", env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                ready = False
                startup_seconds = 180 if embedding_model else 60
                startup_deadline = time.monotonic() + startup_seconds
                while time.monotonic() < startup_deadline:
                    if process.poll() is not None:
                        # A temporary log may contain exception context; do
                        # not dump it into a user report or durable artifact.
                        raise RuntimeError("Disposable Orb exited during startup (" + startup_diagnostic(directory) + ")")
                    try:
                        response = await client.get(base_url + "/__capacity_fixture__", timeout=.5)
                        ready = response.status_code == 200 and response.json().get("nonce") == nonce
                    except (httpx.HTTPError, ValueError):
                        ready = False
                    if ready:
                        break
                    await asyncio.sleep(.1)
                if not ready:
                    raise RuntimeError(f"Disposable Orb did not become ready within {startup_seconds} seconds (" + startup_diagnostic(directory) + ")")
                manifest = json.loads((directory / "capacity-manifest.json").read_text())
                yield base_url, manifest, mock
    finally:
        if startup_diagnostics is not None:
            # Only fixed stage/exception identifiers survive cancellation;
            # raw logs, paths, records and temporary credentials never do.
            startup_diagnostics["last_stage"] = startup_diagnostic(directory)
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.to_thread(process.wait, 10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.to_thread(process.wait, 5)
        if startup_diagnostics is not None:
            # Collect only fixed numeric events after the owned worker exits,
            # before its potentially sensitive temporary log is deleted.
            startup_diagnostics["retrieval_mps_cache"] = mps_cache_diagnostic(directory)
        temporary.cleanup()
        mock_server.should_exit = True
        await asyncio.wait_for(mock_task, timeout=5)
        mock_socket.close()


async def main_async(args) -> int:
    manifest = json.loads(Path(args.manifest).read_text()) if args.manifest else None
    validate_safety(args, manifest)
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "run_id": uuid.uuid4().hex[:12],
              "evidence_kind": ("synthetic_http_fixture_no_model" if args.synthetic else
                                "actual_orb_disposable_mock_ollama_no_model" if args.orb_stub else
                                "actual_orb_disposable_real_local_model" if args.orb_model_origin else "actual_target_requires_provenance_review"),
              "synthetic_data_only": True, "concurrency": args.concurrency, "mode": args.mode,
              "hardware_runtime": hardware_metadata(),
              "limitations": ["Readable milestone is >=32 nonheading characters / four words, not a clinical usefulness verdict.",
                              "No clinical correctness, monitoring, ASR or imaging capacity is certified.",
                              "Unique prompts/threads prevent trivial answer dedup; this is a bounded scripted case mix.",
                              "Failures and rejections remain in target denominators; successful-only quantiles are labelled.",
                              "Summary cache reads do not prove 30-minute generation throughput or scheduler coverage."],
              "background_workloads": {"asr": "not_run", "imaging": "not_run", "monitoring": "not_run", "summary_refresh": "not_run"}}
    if args.synthetic or args.orb_stub or args.orb_model_origin:
        settings = json.loads(Path(args.fixture_settings).read_text()) if args.fixture_settings else {}
        target = synthetic_target() if args.synthetic else orb_stub_target(settings, args.orb_model_origin,
                                                                          prewarm_summaries=args.prewarm_summaries,
                                                                          profile=args.profile_fixture,
                                                                          profile_stages_only=args.profile_stages_only,
                                                                          thread_workers=args.fixture_thread_workers,
                                                                          embedding_model=args.embedding_model,
                                                                          reranker_model=args.reranker_model)
        async with target as (base_url, fixture_manifest, fixture):
            profile_headers = None
            if args.profile_fixture:
                async with httpx.AsyncClient(trust_env=False) as client:
                    marker = await client.get(base_url + "/__capacity_fixture__", timeout=2)
                    marker.raise_for_status()
                    profile_headers = {"X-Orb-Capacity-Nonce": marker.json()["nonce"]}
                    response = await client.post(base_url + "/__capacity_profile__/start", headers=profile_headers, timeout=2)
                    response.raise_for_status()
            summary, results = await run_burst(base_url, fixture_manifest, mode=args.mode, concurrency=args.concurrency,
                                               requests=args.requests, timeout=args.timeout, require_model=True, run_id=report["run_id"])
            if args.profile_fixture:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(base_url + "/__capacity_profile__/stop", headers=profile_headers, timeout=30)
                    response.raise_for_status()
                    report["diagnostic_profile"] = response.json()
                report["limitations"].append("Profiling instrumentation adds overhead; this run is diagnostic and cannot qualify latency targets.")
            report["fixture_observations"] = fixture.state.counters
            report["fixture_configuration"] = settings
            report["fixture_summary_prewarm"] = bool(args.prewarm_summaries)
            report["summary_prewarm_proof"] = fixture_manifest.get("summary_prewarm")
            report["full_retrieval_proof"] = fixture_manifest.get("full_retrieval_proof")
            report["retrieval_configuration"] = ("installed_BGE_M3_and_cross_encoder_seeded_corpus"
                                                    if args.embedding_model else "empty_offline_cache_chart_and_auth_only_no_full_RAG")
            if not args.synthetic:
                async with httpx.AsyncClient(trust_env=False) as client:
                    marker = await client.get(base_url + "/__capacity_fixture__", timeout=2)
                    if marker.status_code == 200 and marker.json().get("nonce"):
                        report["orb_fixture_observations"] = {
                            key: value for key, value in marker.json().items()
                            if key in {"http_admission", "copied_source_sha256", "summary_prewarm", "database_encryption",
                                       "diagnostic_default_executor_workers", "effective_default_executor_workers", "retrieval_executor",
                                       "full_retrieval_proof", "retrieval_runtime"}}
                report["hardware_runtime"]["server_colocated_owned_child"] = True
                report["hardware_runtime"]["server_hardware_verified"] = True
                report["hardware_runtime"]["client_hardware_only"] = False
    else:
        assert manifest is not None  # Non-fixture validation requires an explicit manifest.
        summary, results = await run_burst(args.base_url.rstrip("/"), manifest, mode=args.mode, concurrency=args.concurrency,
                                           requests=args.requests, timeout=args.timeout, require_model=True, run_id=report["run_id"])
        report["operator_server_description"] = manifest.get("server_description", "not supplied; client hardware is not server hardware")
    report["summary"], report["requests"] = summary, results
    from capacity_retrieval_fixture import retrieval_integrity
    report["full_retrieval_integrity"] = retrieval_integrity(
        report.get("full_retrieval_proof"), report.get("orb_fixture_observations", {}).get("retrieval_runtime"))
    accepted = (summary["failed"] == 0 and (args.mode == "summary" or summary["all_ai_requests_meet_completed_answer_targets"])
                and report["full_retrieval_integrity"]["passed"] and not args.profile_fixture)
    report["acceptance_passed"] = accepted
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"report": str(output.resolve()), "evidence_kind": report["evidence_kind"], "summary": summary}, indent=2))
    return 0 if accepted else 1


def parser() -> argparse.ArgumentParser:
    from e2e_server import local_snapshot_path
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--synthetic", action="store_true", help="Start disposable transport fixture; never invokes AI")
    group.add_argument("--orb-stub", action="store_true", help="Real Orb copied app/DB with fake Ollama; never invokes real AI")
    group.add_argument("--orb-model-origin", help="Owned disposable Orb using an existing loopback model; explicit --allow-model-load required")
    ap.add_argument("--prewarm-summaries", action="store_true", help="Owned fake-model fixture only: preload 100 real-service cache snapshots using a synthetic generator")
    ap.add_argument("--profile-fixture", action="store_true", help="Diagnostic only: content-free function/DB queue profiling in the owned fake-model Orb child")
    ap.add_argument("--profile-stages-only", action="store_true", help="Use low-overhead explicit elapsed/thread-CPU stages without global cProfile; requires --profile-fixture")
    ap.add_argument("--fixture-thread-workers", type=int, choices=(1, 4, 8, 16, 32), help="Owned fake-model diagnostic only: override the asyncio default executor width")
    ap.add_argument("--embedding-model", type=local_snapshot_path, help="Existing offline production BGE-M3 snapshot; requires --reranker-model and an owned Orb fixture")
    ap.add_argument("--reranker-model", type=local_snapshot_path, help="Existing offline BGE-reranker-v2-m3 snapshot; requires --embedding-model")
    ap.add_argument("--fixture-settings", help="JSON object of allowlisted environment settings for the owned fixture only")
    ap.add_argument("--base-url")
    ap.add_argument("--manifest", help="Scoped synthetic identities and routes; never a production database")
    ap.add_argument("--mode", choices=("fresh-ai", "mixed", "summary"), default="fresh-ai")
    ap.add_argument("--concurrency", type=int, default=200)
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--output", default="/tmp/orb-capacity-report.json")
    ap.add_argument("--allow-nondisposable", action="store_true")
    ap.add_argument("--ack-synthetic-data", action="store_true")
    ap.add_argument("--allow-model-load", action="store_true")
    ap.add_argument("--allow-heavy-model-load", action="store_true")
    return ap


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main_async(parser().parse_args())))
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Capacity runner refused: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Capacity runner interrupted; owned child/temporary resources cleaned up.", file=sys.stderr)
        sys.exit(130)
