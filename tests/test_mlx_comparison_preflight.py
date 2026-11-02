"""Fresh launch admission must follow slow model validation; CPU fakes only."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_mlx_ollama as benchmark


@pytest.mark.parametrize("phases,engine_only,oracle", [
    (["distribution", "replay"], True, True),
    (["distribution"], False, True),
    (["distribution"], True, False),
])
def test_distribution_diagnostic_cannot_bypass_timing_quality_gate(tmp_path, phases, engine_only, oracle):
    argv = ["--engine", "mlx", "--phases", *phases, "--output-dir", str(tmp_path / "evidence"), "--allow-model-load"]
    if engine_only:
        argv.append("--engine-only")
    if oracle:
        argv += ["--distribution-oracle", str(tmp_path / "oracle.json")]
    args = benchmark.parser().parse_args(argv)
    with pytest.raises(ValueError, match="separate engine-only diagnostic"):
        asyncio.run(benchmark.run(args))
    assert not args.output_dir.exists()


@pytest.mark.parametrize("preparation_available,launch_available,expected_launch", [
    (30, 55, True), (55, 30, False)])
def test_latest_memory_after_validation_controls_admission_and_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    preparation_available: int, launch_available: int, expected_launch: bool,
) -> None:
    gib = benchmark.candidate.GIB
    args = benchmark.parser().parse_args(["--engine", "ollama", "--engine-only", "--phases", "quality",
        "--output-dir", str(tmp_path / "evidence"), "--allow-model-load"])
    preparation = benchmark.candidate.MemorySnapshot(64 * gib, 54, preparation_available * gib, "preparation")
    launch = benchmark.candidate.MemorySnapshot(64 * gib, 78, launch_available * gib, "launch")
    after = benchmark.candidate.MemorySnapshot(64 * gib, 79, 56 * gib, "post")
    snapshots = iter((preparation, launch, after))
    events: list[str] = []
    launched: list[Any] = []
    monitor_baselines: list[Any] = []

    def inspect_memory() -> Any:
        snapshot = next(snapshots)
        events.append(snapshot.vm_stat)
        return snapshot

    def validate_model(_path: Path) -> Any:
        events.append("model_validation")
        return benchmark.candidate.ModelSnapshot("synthetic", "synthetic", 1, "synthetic", "synthetic")

    def hardware_metadata() -> dict[str, Any]:
        events.append("hardware_metadata")
        return {}

    class Process:
        def __init__(self, *_args: Any, **_kwargs: Any):
            assert events[-1] == "launch"
            self.pid = 12345
            launched.append(self)

    class ClosedSocket:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: object) -> None:
            return

        def settimeout(self, _value: float) -> None:
            return

        def connect_ex(self, _address: Any) -> int:
            return 1

    async def pressure(initial: Any, *_args: Any) -> None:
        monitor_baselines.append(initial)
        await asyncio.Event().wait()

    async def ready(*_args: Any) -> None:
        raise benchmark.qualification.QualificationAborted("Synthetic stop after launch")

    async def terminate(_process: Any) -> None:
        return

    monkeypatch.setattr(benchmark.candidate, "inspect_memory", inspect_memory)
    monkeypatch.setattr(benchmark.candidate, "inspect_model", validate_model)
    monkeypatch.setattr(benchmark.candidate, "required_available_bytes", lambda *_: 50 * gib)
    monkeypatch.setattr(benchmark.capacity, "hardware_metadata", hardware_metadata)
    ports = iter((10001, 10002))
    monkeypatch.setattr(benchmark.qualification, "free_port", lambda: next(ports))
    monkeypatch.setattr(benchmark.qualification, "install_synthetic_answer_capture", lambda: None)
    monkeypatch.setattr(benchmark.qualification, "system_pressure_summary", lambda _: {})
    monkeypatch.setattr(benchmark.qualification, "runner_token_timings", lambda _: {})
    monkeypatch.setattr(benchmark.qualification, "progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(benchmark.qualification, "terminate_owned", terminate)
    monkeypatch.setattr(benchmark.qualification, "monitor_pressure", pressure)
    monkeypatch.setattr(benchmark, "native_ready", ready)
    monkeypatch.setattr(benchmark.subprocess, "Popen", Process)
    monkeypatch.setattr(benchmark, "socket", SimpleNamespace(socket=lambda: ClosedSocket()))

    assert asyncio.run(benchmark.run(args)) == 1
    report = json.loads((args.output_dir / "report.json").read_text())
    assert events == ["preparation", "model_validation", "hardware_metadata", "launch", "post"]
    assert bool(launched) is expected_launch
    assert monitor_baselines == ([launch] if expected_launch else [])
    assert report["profile"]["preparation_memory"] == asdict(preparation)
    assert report["profile"]["preflight"]["memory"] == asdict(launch)
    assert report["profile"]["preflight"]["required_available_bytes_estimate"] == 50 * gib
    assert report["profile"]["preflight"]["memory_sample_stage"] == "immediately_before_launch_after_validation_and_metadata"
    assert report["status"] == ("aborted" if expected_launch else "not_run_memory_preflight")
    assert not report["strict_200_latency_targets_demonstrated"]
