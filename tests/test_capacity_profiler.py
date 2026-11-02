"""Diagnostic profiling is aggregate, content-free and fixture-restricted."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from capacity_profiler import CapacityProfiler


def test_profiler_collects_code_timings_without_arguments_or_local_values(tmp_path: Path) -> None:
    profiler = CapacityProfiler(tmp_path / "server")
    def work(secret: str) -> int:
        return sum(range(len(secret)))
    profiler.start()
    assert profiler.sync_call(work, "DO-NOT-STORE-SQL-OR-JWT") > 0
    profiler.record("fixed-stage", .2, .01)
    report = profiler.stop()
    encoded = json.dumps(report)
    assert "DO-NOT-STORE-SQL-OR-JWT" not in encoded
    assert str(tmp_path) not in encoded
    assert report["diagnostic_only"] and report["instrumentation_overhead_invalidates_acceptance"]
    assert report["stages"]["fixed-stage"]["calls"] == 1
    assert report["stages"]["fixed-stage"]["elapsed_p95_s"] == .2


def test_stage_wrapper_preserves_return_exceptions_and_signature(tmp_path: Path) -> None:
    profiler = CapacityProfiler(tmp_path / "server")
    def work(value: int) -> int:
        if value < 0:
            raise ValueError("private exception details")
        return value * 2
    wrapped = profiler.wrap_stage(work)
    assert wrapped.__name__ == work.__name__
    assert wrapped(2) == 4
    profiler.start()
    assert wrapped(3) == 6
    with pytest.raises(ValueError):
        wrapped(-1)
    report = profiler.stop()
    assert report["stages"]["stage/external_callable"]["calls"] == 2
    assert "private exception details" not in json.dumps(report)


def test_install_refuses_normal_or_unowned_source_tree(tmp_path: Path) -> None:
    profiler = CapacityProfiler(tmp_path / "server")
    with pytest.raises(RuntimeError, match="owned disposable"):
        profiler.install()


def test_concurrent_calls_share_one_profiler_and_preserve_all_work(tmp_path: Path) -> None:
    profiler = CapacityProfiler(tmp_path / "server")
    def work(value: int) -> int:
        return sum(range(value))
    profiler.start()
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda value: profiler.sync_call(work, value), range(20)))
    finally:
        report = profiler.stop()
    assert results == [sum(range(value)) for value in range(20)]
    stage = report["stages"]["call/external_callable"]
    assert stage["calls"] == 20 and stage["cpu_sum_s"] >= 0
    assert all(row["self_elapsed_s"] >= 0 for row in report["functions"])


def test_stage_only_mode_omits_global_cprofile_and_keeps_elapsed_cpu_samples(tmp_path: Path) -> None:
    profiler = CapacityProfiler(tmp_path / "server", collect_functions=False)
    profiler.start()
    assert profiler.sync_call(sum, range(10)) == 45
    report = profiler.stop()
    assert not report["function_profiling_enabled"]
    assert report["functions"] == []
    assert report["stages"]["call/external_callable"]["calls"] == 1
    assert report["stages"]["call/external_callable"]["cpu_sum_s"] >= 0
