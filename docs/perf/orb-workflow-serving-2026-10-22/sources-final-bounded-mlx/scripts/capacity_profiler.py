"""Opt-in, content-free instrumentation for the disposable capacity fixture.

This module is never imported by the deployed application. Profiling adds
overhead, so its results identify causes and cannot qualify latency targets.
No arguments, local variables, SQL strings, request bodies or tokens are kept.
"""
from __future__ import annotations

import asyncio
import cProfile
import importlib
import inspect
import pstats
import re
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, cast


class CapacityProfiler:
    """Aggregate elapsed/CPU samples and per-code-object call counts only."""

    def __init__(self, server: Path, *, collect_functions: bool = True) -> None:
        self.server = server.resolve()
        self.lock = threading.Lock()
        self.active = False
        self.samples: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.functions: dict[tuple[str, int, str], list[float]] = {}
        self.loop_profile: cProfile.Profile | None = None
        self.started = 0.0
        self.collect_functions = collect_functions
        self.labels: dict[Any, str] = {}
        self.filenames: dict[str, str | None] = {}

    def start(self) -> None:
        with self.lock:
            if self.active:
                raise RuntimeError("Profiling is already active")
            self.samples.clear()
            self.functions.clear()
            self.started = time.perf_counter()
            self.active = True
        if self.collect_functions:
            self.loop_profile = cProfile.Profile()
            self.loop_profile.enable()

    def _label(self, function: Callable[..., Any]) -> str:
        code = getattr(inspect.unwrap(function), "__code__", None)
        if code is None:
            return "external_callable"
        if code in self.labels:
            return self.labels[code]
        filename = self._filename(code.co_filename)
        label = f"{filename}:{code.co_name}" if filename else "external_callable"
        self.labels[code] = label
        return label

    def _filename(self, filename: str) -> str | None:
        if filename not in self.filenames:
            self.filenames[filename] = self._safe_filename(filename)
        return self.filenames[filename]

    def _safe_filename(self, filename: str) -> str | None:
        if filename == "~":
            return "runtime/builtins"
        if filename.startswith("<"):
            return None
        path = Path(filename)
        try:
            return "server/" + str(path.resolve().relative_to(self.server))
        except ValueError:
            # Fixed module basenames and a narrow package suffix are safe;
            # never emit an absolute home/temp path or dynamic function repr.
            if path.name in {"ssl.py", "contextlib.py", "base_events.py", "thread.py", "threading.py"}:
                return "runtime/" + path.name
            for package in ("httpx", "httpcore", "anyio"):
                if package in path.parts:
                    return "runtime/" + "/".join(path.parts[path.parts.index(package):])
        return None

    def record(self, label: str, wall: float, cpu: float = 0.0) -> None:
        with self.lock:
            if self.active and len(self.samples[label]) < 10_000:
                self.samples[label].append((wall, cpu))

    def _merge(self, profile: cProfile.Profile) -> None:
        stats = pstats.Stats(profile)
        stats_data: dict[tuple[str, int, str], tuple[int, int, float, float, Any]] = getattr(stats, "stats")
        with self.lock:
            for (filename, line, name), values in stats_data.items():
                safe_filename = self._filename(filename)
                # Builtin function names come from the interpreter, not args.
                if safe_filename is None:
                    continue
                key = (safe_filename, line, re.sub(r"0x[0-9a-fA-F]+", "0xADDRESS", name))
                target = self.functions.setdefault(key, [0.0] * 4)
                for index, value in enumerate(values[:4]):
                    target[index] += value

    def sync_call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if not self.active:
            return function(*args, **kwargs)
        start, cpu = time.perf_counter(), time.thread_time()
        try:
            # Python 3.12 cProfile monitoring already covers worker threads.
            # A second profiler competes for the interpreter-wide tool slot.
            return function(*args, **kwargs)
        finally:
            self.record("call/" + self._label(function), time.perf_counter() - start, time.thread_time() - cpu)

    def wrap_stage(self, function: Callable[..., Any]) -> Callable[..., Any]:
        label = "stage/" + self._label(function)
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not self.active:
                return function(*args, **kwargs)
            start, cpu = time.perf_counter(), time.thread_time()
            try:
                return function(*args, **kwargs)
            finally:
                self.record(label, time.perf_counter() - start, time.thread_time() - cpu)
        return wrapped

    def install(self) -> None:
        """Patch only a confirmed owned source-copy process before main imports."""
        if Path.cwd().resolve() != self.server or not self.server.parent.name.startswith("orb_capacity_"):
            raise RuntimeError("Profiling requires an owned disposable source copy")
        import database
        original_run_db = database.run_db

        @wraps(original_run_db)
        async def run_db(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            if not self.active or asyncio.iscoroutinefunction(function):
                return await original_run_db(function, *args, **kwargs)
            queued = time.perf_counter()
            label = self._label(function)
            def execute() -> Any:
                self.record("db_queue/" + label, time.perf_counter() - queued)
                return self.sync_call(function, *args, **kwargs)
            return await original_run_db(execute)
        database.run_db = run_db
        # Seeding imports auth before this hook is installed. Retarget already
        # imported aliases to the same wrapper so auth queueing is visible too.
        for module in tuple(sys.modules.values()):
            if module is not None and vars(module).get("run_db") is original_run_db:
                setattr(module, "run_db", run_db)

        original_to_thread = cast(Callable[..., Any], asyncio.to_thread)
        @wraps(original_to_thread)
        async def to_thread(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
            if not self.active:
                return await original_to_thread(function, *args, **kwargs)
            queued = time.perf_counter()
            label = self._label(function)
            def execute() -> Any:
                self.record("thread_queue/" + label, time.perf_counter() - queued)
                return self.sync_call(function, *args, **kwargs)
            return await original_to_thread(execute)
        asyncio.to_thread = to_thread
        stages = {
            "database": ("get_db", "_build_connection"),
            "db_backend": ("get_pooled",),
            "auth": ("authenticate_session_token", "_load_session_snapshot"),
            "services.sage_service": ("get_relevant_context", "get_conversation_history", "_get_guideline_context",
                                      "_assemble_patient_context", "_match_patients", "_locate_patients",
                                      "_names_a_specific_patient", "_match_out_of_scope", "_refresh_census_name_tokens"),
            "services.ai_governance": ("classify_query_sensitivity",),
            "services.shadow_service": ("open_episode", "emit_many"),
            "services.sage_provenance_service": ("record_turn",),
            "httpx._transports.default": ("create_ssl_context",),
        }
        for module_name, names in stages.items():
            module = importlib.import_module(module_name)
            for name in names:
                function = getattr(module, name, None)
                if callable(function):
                    setattr(module, name, self.wrap_stage(function))

    def stop(self) -> dict[str, Any]:
        if not self.active:
            raise RuntimeError("Profiling has not started")
        if self.loop_profile is not None:
            self.loop_profile.disable()
            self._merge(self.loop_profile)
            self.loop_profile = None
        elapsed = time.perf_counter() - self.started
        with self.lock:
            self.active = False
            stages = {label: self._distribution(samples) for label, samples in sorted(self.samples.items())}
            functions: list[dict[str, Any]] = [
                {"file": file, "line": line, "function": name, "primitive_calls": int(values[0]),
                 "total_calls": int(values[1]), "self_elapsed_s": values[2], "cumulative_elapsed_s": values[3]}
                for (file, line, name), values in self.functions.items()
            ]
        functions.sort(key=lambda row: float(row["cumulative_elapsed_s"]), reverse=True)
        sage = importlib.import_module("services.sage_service") if "services.sage_service" in sys.modules else None
        cache_counts = {}
        for name in ("_census_vocabulary", "_ward_chunks_cached", "_label_chunks_cached"):
            function = getattr(sage, name, None)
            cache_info = getattr(function, "cache_info", None)
            if callable(cache_info):
                cache_counts[name] = cache_info()._asdict()
        probes = {"_census_vocabulary", "_ward_chunks_cached", "_label_chunks_cached", "_build_connection", "get_pooled",
                  "create_ssl_context", "load_verify_locations", "_tokenize", "_scan", "_locate_patients"}
        return {"diagnostic_only": True, "instrumentation_overhead_invalidates_acceptance": True,
                "timer": ("cProfile monotonic elapsed; explicit stage CPU uses thread_time; elapsed includes contention"
                          if self.collect_functions else "Explicit perf_counter elapsed / thread_time CPU only; no global cProfile"),
                "wall_s": elapsed,
                "function_profiling_enabled": self.collect_functions,
                "stages": stages, "functions": functions[:250],
                "selected_function_counts": [row for row in functions if row["function"] in probes],
                "pure_string_cache_counters": cache_counts,
                "function_timing_limitations": "cProfile elapsed/call counts span interleaved threads; do not sum or treat as exclusive CPU. Explicit synchronous stage CPU counters use per-thread time.",
                "privacy": "Aggregate code names, counts and timings only; no args, locals, SQL or clinical content."}

    @staticmethod
    def _distribution(samples: list[tuple[float, float]]) -> dict[str, float | int]:
        walls = sorted(wall for wall, _cpu in samples)
        return {"calls": len(samples), "elapsed_sum_s": sum(walls), "cpu_sum_s": sum(cpu for _wall, cpu in samples),
                "elapsed_p50_s": walls[(len(walls) - 1) // 2],
                "elapsed_p95_s": walls[max(0, (len(walls) * 95 + 99) // 100 - 1)], "elapsed_max_s": walls[-1]}
