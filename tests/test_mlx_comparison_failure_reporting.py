"""Concurrent benchmark failures remain diagnostic evidence; no real runtime."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_mlx_ollama as benchmark


def status_error(path: str = "/api/generate") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://user:secret@127.0.0.1:1234" + path + "?private=never_log_this")
    response = httpx.Response(503, request=request, text="never log response body")
    return httpx.HTTPStatusError("never log raw exception", request=request, response=response)


async def fail(error: BaseException) -> None:
    raise error


def test_both_completed_failures_are_recorded_with_guard_primary() -> None:
    async def scenario() -> None:
        guard = benchmark.qualification.QualificationAborted("Compression grew beyond the unchanged guard")
        monitor = asyncio.create_task(fail(guard))
        execution = asyncio.create_task(fail(status_error()))
        await asyncio.gather(monitor, execution, return_exceptions=True)
        report: dict[str, Any] = {}
        assert benchmark.record_task_failures(report, monitor, execution, promote_primary=True) is guard
        assert report["error_type"] == "QualificationAborted"
        assert report["abort_reason"] == str(guard)
        assert [row["task"] for row in report["task_failures"]] == ["resource_monitor", "execution"]
        assert report["task_failures"][1]["http_status_code"] == 503
        assert report["task_failures"][1]["http_path"] == "/api/generate"
        assert "never" not in json.dumps(report) and "secret" not in json.dumps(report)

    asyncio.run(scenario())


def test_guard_completing_during_cleanup_is_retained_and_promoted() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        guard = benchmark.qualification.QualificationAborted("Memory guard stopped the owned engine")

        async def delayed_guard() -> None:
            await gate.wait()
            raise guard

        monitor = asyncio.create_task(delayed_guard())
        execution = asyncio.create_task(fail(status_error()))
        await asyncio.gather(execution, return_exceptions=True)
        report: dict[str, Any] = {"status": "aborted"}
        benchmark.record_task_failures(report, monitor, execution, promote_primary=True)
        assert report["error_type"] == "HTTPStatusError" and not monitor.done()
        gate.set()
        await asyncio.gather(monitor, return_exceptions=True)
        benchmark.record_task_failures(report, monitor, execution, promote_primary=True)
        assert report["error_type"] == "QualificationAborted"
        assert "http_status_code" not in report and len(report["task_failures"]) == 2

    asyncio.run(scenario())


def test_cancelled_and_successful_tasks_are_not_reported_as_failures() -> None:
    async def scenario() -> None:
        async def success() -> None:
            return

        monitor = asyncio.create_task(asyncio.sleep(10))
        execution = asyncio.create_task(success())
        await execution
        report: dict[str, Any] = {}
        assert benchmark.record_task_failures(report, monitor, execution) is None
        assert not monitor.done() and not report
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        assert benchmark.record_task_failures(report, monitor, execution) is None and not report

    asyncio.run(scenario())


def test_guard_marker_remains_primary_if_monitor_is_cancelled_while_terminating() -> None:
    async def scenario() -> None:
        monitor = asyncio.create_task(asyncio.Event().wait())
        execution = asyncio.create_task(fail(status_error()))
        await asyncio.gather(execution, return_exceptions=True)
        reason = "compression_growth_over_4_gib"
        report: dict[str, Any] = {"status": "aborted", **benchmark.failure_details(status_error()),
            "profile": {"pressure_samples": [{"guard_abort_reason": reason}]}}
        primary = benchmark.record_task_failures(report, monitor, execution, promote_primary=True)
        assert isinstance(primary, benchmark.qualification.QualificationAborted)
        assert report["resource_guard_trigger"]["monitor_state"] == "pending"
        assert report["resource_guard_trigger"]["monitor_exception_observed"] is False
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        benchmark.record_task_failures(report, monitor, execution, promote_primary=True)
        assert report["error_type"] == "QualificationAborted" and report["abort_reason"] == reason
        assert report["primary_failure_evidence"] == "recorded_guard_trigger_before_termination"
        assert report["resource_guard_trigger"] == {
            "pressure_sample_index": 0, "reason": reason,
            "evidence": "recorded_before_owned_termination",
            "monitor_exception_observed": False, "monitor_state": "cancelled"}
        assert [row["task"] for row in report["task_failures"]] == ["execution"]
        assert report["task_failures"][0]["http_status_code"] == 503
        assert "http_status_code" not in report

    asyncio.run(scenario())


@pytest.mark.parametrize("path,expected", [("/api/chat", "/api/chat"),
                                              ("/patients/synthetic-person", "<other_path_redacted>")])
def test_http_diagnostics_exclude_credentials_query_body_and_exception(path: str, expected: str) -> None:
    details = benchmark.failure_details(status_error(path))
    assert details["http_status_code"] == 503 and details["http_method"] == "POST"
    assert details["http_path"] == expected
    rendered = json.dumps(details)
    assert all(value not in rendered for value in ("secret", "private", "never", "127.0.0.1", "synthetic-person"))


def test_run_reports_both_simultaneous_failures_and_cleans_owned_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = benchmark.parser().parse_args(["--engine", "ollama", "--engine-only",
        "--phases", "quality", "--output-dir", str(tmp_path / "evidence"), "--allow-model-load"])
    memory = benchmark.candidate.MemorySnapshot(64 * 1024 ** 3, 80, 50 * 1024 ** 3, "synthetic")
    model = benchmark.candidate.ModelSnapshot("synthetic", "sha256:synthetic", 1, "synthetic", "synthetic")
    monkeypatch.setattr(benchmark.candidate, "inspect_memory", lambda: memory)
    monkeypatch.setattr(benchmark.candidate, "inspect_model", lambda _: model)
    monkeypatch.setattr(benchmark.candidate, "required_available_bytes", lambda *_: 1)
    monkeypatch.setattr(benchmark.capacity, "hardware_metadata", dict)
    ports = iter((10001, 10002))
    monkeypatch.setattr(benchmark.qualification, "free_port", lambda: next(ports))
    monkeypatch.setattr(benchmark.qualification, "install_synthetic_answer_capture", lambda: None)
    monkeypatch.setattr(benchmark.qualification, "system_pressure_summary", lambda _: {})
    monkeypatch.setattr(benchmark.qualification, "runner_token_timings", lambda _: {})
    monkeypatch.setattr(benchmark.qualification, "progress", lambda *_args, **_kwargs: None)
    processes = []
    terminated = []

    class Process:
        def __init__(self, *_args: Any, **_kwargs: Any):
            self.pid = 100 + len(processes)
            processes.append(self)

    monkeypatch.setattr(benchmark.subprocess, "Popen", Process)

    class ClosedSocket:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: object) -> None:
            return

        def settimeout(self, _timeout: float) -> None:
            return

        def connect_ex(self, _address: Any) -> int:
            return 1

    monkeypatch.setattr(benchmark, "socket", SimpleNamespace(AF_INET=2, SOCK_STREAM=1,
                                                            socket=lambda *_: ClosedSocket()))

    async def ready(*_args: Any) -> None:
        return

    async def terminate(process: Any) -> None:
        terminated.append(process)

    async def scenario() -> int:
        gate = asyncio.Event()

        async def pressure(*_args: Any) -> None:
            await gate.wait()
            raise benchmark.qualification.QualificationAborted("Compression guard exceeded")

        async def phases(*_args: Any) -> None:
            gate.set()
            raise status_error()

        monkeypatch.setattr(benchmark.qualification, "monitor_pressure", pressure)
        monkeypatch.setattr(benchmark, "engine_only_phases", phases)
        return await benchmark.run(args)

    monkeypatch.setattr(benchmark, "native_ready", ready)
    monkeypatch.setattr(benchmark, "proxy_ready", ready)
    monkeypatch.setattr(benchmark.qualification, "terminate_owned", terminate)
    assert asyncio.run(scenario()) == 1
    report = json.loads((args.output_dir / "report.json").read_text())
    assert report["status"] == "aborted" and report["error_type"] == "QualificationAborted"
    assert report["abort_reason"] == "Compression guard exceeded"
    assert {row["error_type"] for row in report["task_failures"]} == {"QualificationAborted", "HTTPStatusError"}
    assert terminated == list(reversed(processes))
    assert not report["strict_200_latency_targets_demonstrated"]
