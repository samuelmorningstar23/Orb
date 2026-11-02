"""No model, daemon or live application is used by these supervisor tests."""
from __future__ import annotations

import asyncio
import importlib
import json
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
supervisor: Any = importlib.import_module("qualify_ollama_serving")


def snapshot(free: int = 75, *, swap: int = 0, compressed: int = 0) -> Any:
    return supervisor.candidate.MemorySnapshot(64 * 2**30, free, 64 * 2**30 * free // 100,
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        f"Swapouts: {swap}.\nPages occupied by compressor: {compressed}.\n")


def test_pressure_guard_bounds_swap_compression_and_sustained_pressure() -> None:
    initial = snapshot()
    assert supervisor.pressure_abort_reason(initial, snapshot(free=14), 1) is None
    assert supervisor.pressure_abort_reason(initial, snapshot(free=14), 2) == "sustained_memory_pressure"
    assert supervisor.pressure_abort_reason(initial, snapshot(swap=32769), 0) == "swap_growth_over_512_mib"
    assert supervisor.pressure_abort_reason(initial, snapshot(compressed=262145), 0) == "compression_growth_over_4_gib"
    assert supervisor.pressure_abort_reason(initial, snapshot(free=60, swap=100, compressed=200), 0) is None


def test_guard_reason_survives_cancellation_during_owned_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        termination_started = asyncio.Event()
        termination_cancelled = []
        samples: list[dict[str, Any]] = []

        async def immediate_sleep(_delay: float) -> None:
            return

        async def immediate_thread(function: Any) -> Any:
            return function()

        async def termination(_process: Any) -> None:
            termination_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                termination_cancelled.append(True)
                raise

        monkeypatch.setattr(supervisor, "asyncio", SimpleNamespace(sleep=immediate_sleep, to_thread=immediate_thread))
        monkeypatch.setattr(supervisor.candidate, "inspect_memory", lambda: snapshot(compressed=262145))
        monkeypatch.setattr(supervisor, "terminate_owned", termination)
        task = asyncio.create_task(supervisor.monitor_pressure(snapshot(), samples, object()))
        await termination_started.wait()
        assert not task.done() and len(samples) == 1
        assert samples[0]["guard_abort_reason"] == "compression_growth_over_4_gib"
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError) and termination_cancelled == [True]
        assert samples[0]["guard_abort_reason"] == "compression_growth_over_4_gib"

    asyncio.run(scenario())


def test_worker_pool_matches_real_engine_width_without_model_substitution() -> None:
    for parallel in (1, 2):
        settings = supervisor.fixture_settings(parallel)
        worker = json.loads(settings["ORB_LLM_WORKERS"])[0]
        assert worker["capacity"] == parallel
        assert worker["models"] == ["medgemma:27b"]
        assert worker["origin"] == "{inference_url}"
        assert worker["lanes"] == ["interactive"]
        assert settings["ORB_LLM_POOL_MAX_INFLIGHT"] == "200"
    with pytest.raises(ValueError):
        supervisor.fixture_settings(200)


def test_burst_only_keeps_all200_attempts_and_uses_actual_selected_capacity() -> None:
    for parallel in (1, 2):
        phases = supervisor.planned_phases(parallel, True)
        assert phases == [(f"burst_200_actual_capacity_{parallel}", 200, 200, 20.0)]
        args = supervisor.parser().parse_args(["--models-dir", "/unused", "--output-dir", "/unused-new",
                                              "--profiles", str(parallel), "--burst-only"])
        assert args.profiles == [parallel] and args.burst_only is True


def test_engine_token_metrics_do_not_claim_cancelled_http_answer_success(tmp_path: Path) -> None:
    log = tmp_path / "owned.log"
    log.write_text("slot print_timing: id  0 | task 2 | prompt eval time = 200.00 ms / 100 tokens\n"
                   "slot print_timing: id  0 | task 2 |        eval time = 400.00 ms / 20 tokens\n"
                   "slot      release: id  0 | task 2 | stop processing: n_tokens = 119, truncated = 0\n")
    report = supervisor.runner_token_timings(log)
    row = report["tasks"][0]
    assert row["prompt_tokens"] == 100 and row["prompt_tokens_per_second"] == 500
    assert row["decode_tokens"] == 20 and row["decode_tokens_per_second"] == 50
    assert row["truncated"] is False and "success" not in row
    assert "cancelled" in report["interpretation"]


def acceptance_report() -> dict[str, Any]:
    return {"status": "complete", "planned_profiles": [1],
        "retrieval_configuration": "installed_bge_m3_and_reranker", "profiles": [{
        "status": "complete", "orb_fixture_observations": {
            "full_retrieval_proof": {"nonzero_embedding_probe": True, "cross_encoder_probe": True,
                                     "corpus_vectors": 19, "nonzero_corpus_vectors": 19},
            "retrieval_runtime": {"embedding_failed": 0, "rerank_failed": 0,
                "embedding_calls": 200, "embedding_completed": 200,
                "rerank_calls": 200, "rerank_completed": 200,
                "results_lexical": 0, "results_other": 0, "results_cross_encoder": 2400}},
        "phases": [{"planned_requests": 200, "concurrency": 200, "status": "complete",
            "summary": {"requests": 200, "failed": 0, "all_ai_requests_meet_completed_answer_targets": True},
            "requests": [{"success": True, "completed_within_target": True, "model": "medgemma:27b"}
                         for _ in range(200)]}]}]}


def test_acceptance_is_separate_from_procedure_completion_and_preserves_denominators() -> None:
    report = acceptance_report()
    assert supervisor.acceptance_flags(report)["acceptance_passed"] is True
    report["profiles"][0]["phases"][0]["requests"].pop()
    flags = supervisor.acceptance_flags(report)
    assert flags["procedure_completed"] is True and flags["acceptance_passed"] is False
    report = acceptance_report()
    report["profiles"].append({"status": "not_run_memory_guard", "phases": []})
    report["planned_profiles"] = [1, 2]
    flags = supervisor.acceptance_flags(report)
    assert flags["all_planned_profiles_exercised"] is False
    assert flags["acceptance_passed"] is False


@pytest.mark.parametrize("issue", ["pending", "failed", "lexical", "wrong_model", "deadline", "cached"])
def test_acceptance_requires_completed_full_retrieval_and_same_clinical_model(issue: str) -> None:
    report = acceptance_report()
    runtime = report["profiles"][0]["orb_fixture_observations"]["retrieval_runtime"]
    row = report["profiles"][0]["phases"][0]["requests"][0]
    if issue == "pending":
        runtime["rerank_completed"] -= 1
    elif issue == "failed":
        runtime["rerank_failed"] += 1
    elif issue == "lexical":
        runtime["results_lexical"] += 1
    elif issue == "wrong_model":
        row["model"] = "medgemma:4b"
    elif issue == "deadline":
        row["completed_within_target"] = False
    else:
        row["cache_hit"] = True
    assert supervisor.acceptance_flags(report)["acceptance_passed"] is False


def test_pressure_summary_explicitly_reports_host_counters_not_daemon_peak() -> None:
    from dataclasses import asdict

    summary = supervisor.system_pressure_summary({
        "preflight": {"memory": asdict(snapshot(compressed=5))},
        "pressure_samples": [asdict(snapshot(free=50, swap=10, compressed=20))],
        "post_memory": asdict(snapshot(free=70, swap=11, compressed=8)),
    })
    assert summary["scope"] == "system_wide_not_daemon_rss_or_gpu_peak"
    assert summary["minimum_os_free_percentage"] == 50
    assert summary["maximum_compression_growth_bytes"] == 15 * 16384
    assert summary["swapout_growth_bytes"] == 11 * 16384


def test_plan_only_never_starts_daemon_or_creates_directory(tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / "untouched"
    args = supervisor.parser().parse_args(["--models-dir", str(tmp_path), "--output-dir", str(directory)])
    assert asyncio.run(supervisor.main_async(args)) == 0
    assert not directory.exists()
    assert json.loads(capsys.readouterr().out)["event"] == "plan_only"


def test_retrieval_snapshots_must_be_paired_and_installed(tmp_path: Path) -> None:
    base = ["--models-dir", str(tmp_path), "--output-dir", str(tmp_path / "untouched")]
    with pytest.raises(ValueError, match="together"):
        asyncio.run(supervisor.main_async(supervisor.parser().parse_args([
            *base, "--embedding-model", str(tmp_path)])))
    with pytest.raises(ValueError, match="installed"):
        asyncio.run(supervisor.main_async(supervisor.parser().parse_args([
            *base, "--embedding-model", str(tmp_path), "--reranker-model", str(tmp_path / "missing")])))


def test_full_retrieval_plan_is_explicit_without_loading_models(tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    args = supervisor.parser().parse_args(["--models-dir", str(tmp_path), "--output-dir", str(tmp_path / "untouched"),
                                          "--embedding-model", str(tmp_path), "--reranker-model", str(tmp_path)])
    assert asyncio.run(supervisor.main_async(args)) == 0
    assert json.loads(capsys.readouterr().out)["retrieval"] == "installed_embedding_and_reranker"


@pytest.mark.parametrize("flags", [["--execute"], ["--execute", "--allow-model-load"],
                                    ["--execute", "--allow-heavy-model-load"]])
def test_real_model_execution_requires_both_load_acknowledgements(tmp_path: Path, flags: list[str]) -> None:
    directory = tmp_path / "untouched"
    args = supervisor.parser().parse_args(["--models-dir", str(tmp_path), "--output-dir", str(directory), *flags])
    with pytest.raises(ValueError, match="acknowledgements"):
        asyncio.run(supervisor.main_async(args))
    assert not directory.exists()


@pytest.mark.parametrize("budget", ["0", "301", "nan", "inf"])
def test_unbounded_or_invalid_window_is_refused(tmp_path: Path, budget: str) -> None:
    args = supervisor.parser().parse_args(["--models-dir", str(tmp_path), "--output-dir", str(tmp_path / "x"),
                                          "--budget-seconds", budget])
    with pytest.raises(ValueError, match="bounded"):
        asyncio.run(supervisor.main_async(args))


def test_cleanup_signals_only_the_owned_process_group_even_if_leader_exited(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, int]] = []

    class OwnedProcess:
        pid = 123456

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float) -> int:
            assert timeout == 5
            return 0

    monkeypatch.setattr(supervisor.os, "killpg", lambda pid, value: signals.append((pid, value)))
    asyncio.run(supervisor.terminate_owned(OwnedProcess()))
    assert signals == [(123456, signal.SIGTERM)]


def test_cleanup_escalates_only_its_owned_group(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, int]] = []

    class OwnedProcess:
        pid = 123456
        attempts = 0

        def wait(self, timeout: float) -> int:
            self.attempts += 1
            if self.attempts == 1:
                raise subprocess.TimeoutExpired("synthetic owned child", timeout)
            return 0

    monkeypatch.setattr(supervisor.os, "killpg", lambda pid, value: signals.append((pid, value)))
    asyncio.run(supervisor.terminate_owned(OwnedProcess()))
    assert signals == [(123456, signal.SIGTERM), (123456, signal.SIGKILL)]


def test_synthetic_capture_observes_same_full_stream_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor.capacity, "StreamObservation", supervisor.capacity.StreamObservation)
    supervisor.install_synthetic_answer_capture()
    installed = supervisor.capacity.StreamObservation
    supervisor.install_synthetic_answer_capture()
    assert supervisor.capacity.StreamObservation is installed
    observation = installed()
    text = "Synthetic patient has a documented note; no other clinical fact is inferred."
    observation.accept("message", json.dumps({"content": text}), 0.1)
    observation.accept("message", json.dumps({"done": True}), 0.2)
    report = observation.report()
    assert report["synthetic_answer"] == text
    assert report["terminal"] is True and report["content_chars"] == len(text)


@pytest.fixture
def phase_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise real phase accounting without a socket, model or application."""
    @asynccontextmanager
    async def target(*_args: Any, **_kwargs: Any):
        yield "http://127.0.0.1:1", {"synthetic_data_only": True}, None

    async def warm(_origin: str) -> dict[str, float]:
        return {"wall_s": .01}

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def get(self, *_args: Any, **_kwargs: Any) -> Any:
            class Response:
                def raise_for_status(self) -> None:
                    pass

                def json(self) -> dict[str, Any]:
                    return {"nonce": "owned", "retrieval_executor": {"workers": 4}}

            return Response()

    monkeypatch.setattr(supervisor.capacity, "orb_stub_target", target)
    monkeypatch.setattr(supervisor, "warm_model", warm)
    monkeypatch.setattr(supervisor.httpx, "AsyncClient", Client)


@pytest.mark.parametrize("observed_count", [0, 1, 200])
def test_aborted_phase_preserves_observed_results_without_partial_acceptance(
        monkeypatch: pytest.MonkeyPatch, phase_harness: None, observed_count: int) -> None:
    async def exercise() -> None:
        observed, never = asyncio.Event(), asyncio.Event()

        async def burst(*_args: Any, **kwargs: Any) -> Any:
            observer = kwargs["result_observer"]
            for index in range(observed_count):
                observer({"request_id": f"synthetic-{index}", "success": True,
                          "model": "medgemma:27b", "completed_within_target": True})
            observed.set()
            await never.wait()

        monkeypatch.setattr(supervisor.capacity, "run_burst", burst)
        profile: dict[str, Any] = {"phases": []}
        task = asyncio.create_task(supervisor.execute_phases("http://127.0.0.1:1", 1,
            profile, burst_only=True, expected_retrieval_workers=4))
        await observed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        phase = profile["phases"][0]
        assert phase["planned_requests"] == 200
        assert phase["observed_request_results"] == observed_count
        assert phase["unobserved_request_results"] == 200 - observed_count
        assert len(phase["requests"]) == observed_count
        assert phase["status"] == "aborted_incomplete_not_acceptance_evidence"
        assert "summary" not in phase
        assert "unknown physical engine completion" in phase["result_observation_scope"]
        report = acceptance_report()
        report["profiles"][0]["phases"] = [phase]
        flags = supervisor.acceptance_flags(report)
        assert not flags["acceptance_passed"]
        assert not flags["strict_200_request_targets_demonstrated"]

    asyncio.run(exercise())


def test_completed_phase_keeps_normal_summary_order_and_complete_accounting(
        monkeypatch: pytest.MonkeyPatch, phase_harness: None) -> None:
    rows = [{"request_id": f"synthetic-{index}", "success": True,
             "model": "medgemma:27b", "completed_within_target": True} for index in range(200)]
    summary = {"successful": 200, "failed": 0, "requests": 200,
               "all_ai_requests_meet_completed_answer_targets": True}

    async def burst(*_args: Any, **kwargs: Any) -> Any:
        for row in reversed(rows):
            kwargs["result_observer"](dict(row))
        return summary, rows

    monkeypatch.setattr(supervisor.capacity, "run_burst", burst)
    profile: dict[str, Any] = {"phases": []}
    asyncio.run(supervisor.execute_phases("http://127.0.0.1:1", 1, profile,
        burst_only=True, expected_retrieval_workers=4))
    phase = profile["phases"][0]
    assert phase["status"] == "complete"
    assert phase["observed_request_results"] == 200 and phase["unobserved_request_results"] == 0
    assert phase["summary"] is summary and phase["requests"] is rows
    report = acceptance_report()
    report["profiles"][0]["phases"] = [phase]
    assert supervisor.acceptance_flags(report)["acceptance_passed"] is True


def test_phase_execution_error_also_retains_returned_results(
        monkeypatch: pytest.MonkeyPatch, phase_harness: None) -> None:
    async def burst(*_args: Any, **kwargs: Any) -> Any:
        kwargs["result_observer"]({"request_id": "returned", "success": False, "error": "timeout"})
        raise RuntimeError("Synthetic diagnostic failure")

    monkeypatch.setattr(supervisor.capacity, "run_burst", burst)
    profile: dict[str, Any] = {"phases": []}
    with pytest.raises(RuntimeError, match="diagnostic failure"):
        asyncio.run(supervisor.execute_phases("http://127.0.0.1:1", 1, profile, burst_only=True))
    phase = profile["phases"][0]
    assert phase["observed_request_results"] == 1 and phase["unobserved_request_results"] == 199
    assert phase["requests"][0]["error"] == "timeout"
    assert phase["status"] == "aborted_incomplete_not_acceptance_evidence" and "summary" not in phase
