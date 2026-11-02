"""CPU-only supervisor tests with no subprocess, hardware inspection or GPU."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("guard_mlx_conversion")


def arguments(tmp_path, profile=None):
    args = argparse.Namespace(python=tmp_path / "unused-python", source=tmp_path / "unused-source.gguf",
                              output_dir=tmp_path / "candidate", report=tmp_path / "report.json", log=tmp_path / "log.txt")
    if profile is not None:
        args.quantisation_profile = profile
    return args


def command_arguments(tmp_path):
    return ["--python", str(tmp_path / "python"), "--source", str(tmp_path / "source"),
            "--output-dir", str(tmp_path / "out"), "--report", str(tmp_path / "report"), "--log", str(tmp_path / "log")]


def test_parser_preserves_affine8_default_and_requires_a_known_explicit_profile(guard, tmp_path):
    assert guard.build_parser().parse_args(command_arguments(tmp_path)).quantisation_profile == "affine8"
    for profile in ("source-mixed46", "affine8", "source-repack4-f16"):
        args = guard.build_parser().parse_args(command_arguments(tmp_path) + ["--quantisation-profile", profile])
        assert args.quantisation_profile == profile
    with pytest.raises(SystemExit):
        guard.build_parser().parse_args(command_arguments(tmp_path) + ["--quantisation-profile", "unbounded"])


@dataclass
class Memory:
    synthetic_memory_sample: bool = True


def fake_supervisor(guard, monkeypatch, *, exit_code=0, pressure_failure=False):
    calls = {"commands": [], "terminated": [], "pressure_monitors": 0}
    release = threading.Event()

    class Process:
        pid = 43210

        def wait(self):
            if pressure_failure:
                release.wait(timeout=1)
            return exit_code

    process = Process()

    def popen(command, **kwargs):
        calls["commands"].append((command, kwargs))
        assert kwargs["start_new_session"] is True
        return process

    async def monitor(initial, samples, owned_process):
        calls["pressure_monitors"] += 1
        assert owned_process is process
        if pressure_failure:
            raise guard.qualification.QualificationAborted("synthetic memory-pressure limit")
        await asyncio.Event().wait()

    async def terminate(owned_process):
        calls["terminated"].append(owned_process)
        release.set()

    monkeypatch.setattr(guard.benchmark.candidate, "inspect_memory", lambda: Memory())
    monkeypatch.setattr(guard.benchmark, "offline_environment", lambda: {"HF_HUB_OFFLINE": "1"})
    monkeypatch.setattr(guard.benchmark, "inference_environment", lambda env, engine: {**env, "engine": engine})
    monkeypatch.setattr(guard.subprocess, "Popen", popen)
    monkeypatch.setattr(guard.qualification, "monitor_pressure", monitor)
    monkeypatch.setattr(guard.qualification, "terminate_owned", terminate)
    monkeypatch.setattr(guard.qualification, "system_pressure_summary", lambda profile: {"synthetic_only": True})
    return calls


@pytest.mark.parametrize("profile", [None, "affine8", "source-mixed46", "source-repack4-f16"])
def test_supervisor_passes_exact_profile_and_preserves_memory_monitor_budget_and_owned_cleanup(guard, tmp_path, monkeypatch, profile):
    calls = fake_supervisor(guard, monkeypatch)
    args = arguments(tmp_path, profile)
    assert asyncio.run(guard.run(args)) == 0
    report = json.loads(args.report.read_text())
    selected = profile or "affine8"
    command, kwargs = calls["commands"][0]
    assert command[command.index("--quantisation-profile") + 1] == selected
    assert command[command.index("--max-dequant-bytes") + 1] == str(64 * 1024 ** 2)
    assert command[-2:] == ["--execute", "--acknowledge-requantized-candidate"]
    assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    assert report["quantisation_profile"] == selected
    assert report["wall_budget_seconds"] == 900
    assert report["clinical_quality_qualified"] is False
    assert report["status"] == "complete" and report["exit_code"] == 0
    assert calls["pressure_monitors"] == 1
    assert len(calls["terminated"]) == 1
    if selected == "affine8":
        assert report["purpose"] == "offline_bounded_affine8_candidate_conversion_not_inference"
    if selected == "source-repack4-f16":
        assert report["candidate_label"] == "medgemma-27b-text-mlx-q4-integer-repack-q6-fp16-candidate"
        assert len(report["repack_helper_sha256"]) == 64


@pytest.mark.parametrize("pressure_failure,expected", [(False, "conversion_failed"), (True, "aborted")])
def test_failure_and_pressure_abort_never_report_success_and_always_clean_only_owned_child(guard, tmp_path, monkeypatch, pressure_failure, expected):
    calls = fake_supervisor(guard, monkeypatch, exit_code=7 if not pressure_failure else 0, pressure_failure=pressure_failure)
    args = arguments(tmp_path, "source-repack4-f16")
    assert asyncio.run(guard.run(args)) == 1
    report = json.loads(args.report.read_text())
    assert report["status"] == expected
    assert len(calls["terminated"]) == 1
    if pressure_failure:
        assert report["error_type"] == "QualificationAborted"
        assert "synthetic memory-pressure limit" in report["error"]


def test_unknown_programmatic_profile_is_refused_before_hardware_inspection_or_process_launch(guard, tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("Hardware inspection must not be reached")

    monkeypatch.setattr(guard.benchmark.candidate, "inspect_memory", forbidden)
    with pytest.raises(ValueError, match="quantisation profiles"):
        asyncio.run(guard.run(arguments(tmp_path, "unbounded")))
    assert not (tmp_path / "report.json").exists()
