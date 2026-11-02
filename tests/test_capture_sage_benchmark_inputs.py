"""Capture-origin/provenance/guard checks; no models, listeners or real DBs."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import capture_sage_benchmark_inputs as capture


def snapshot(free: int = 75, compressed: int = 0) -> Any:
    return capture.qualification.candidate.MemorySnapshot(64 * 2**30, free, 64 * 2**30 * free // 100,
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        f"Swapouts: 0.\nPages occupied by compressor: {compressed}.\n")


def args(tmp_path: Path) -> Any:
    return capture.parser().parse_args(["--embedding-model", str(tmp_path), "--reranker-model", str(tmp_path),
        "--output-dir", str(tmp_path / "new-output"), "--synthetic-data-only", "--allow-retrieval-model-load"])


def captured_rows() -> list[dict[str, Any]]:
    result = []
    for index in range(3):
        body = {"model": "medgemma:27b", "stream": True,
                "messages": [{"role": "user", "content": f"Distinct synthetic final input {index}"}],
                "options": {"num_ctx": 8192, "num_predict": 2048, "temperature": .4,
                            "repeat_penalty": 1.15, "repeat_last_n": 256}}
        result.append({"request_id": f"{index + 1:032x}", "endpoint": "/api/chat", "body": body,
                       "body_sha256": capture.proxy.digest(body), "backend": "ollama"})
    return result


def test_capture_metadata_cannot_be_mistaken_for_actual_ollama_generation(tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    app = capture.proxy.create_app(backend="ollama", upstream="http://127.0.0.1:18080",
        capture_dir=directory, synthetic_data_only=True)
    capture.attest_stub_capture(app, directory)
    metadata = json.loads((directory / "proxy-metadata.json").read_text())
    assert metadata["upstream_kind"] == "synthetic_stub_for_input_capture_only"
    assert metadata["clinical_model_invoked"] is False and metadata["clinical_model_not_invoked"] is True
    assert metadata["upstream_protocol_version"] == "synthetic-fixture"
    assert metadata["latency_acceptance_evidence"] is False and metadata["clinical_accuracy_evidence"] is False
    assert app.state.metadata == metadata
    assert (directory / "proxy-metadata.json").stat().st_mode & 0o077 == 0
    (directory / "requests.jsonl").write_bytes(b"".join(capture.proxy.canonical(row) + b"\n" for row in captured_rows()))
    proof = capture.capture_proof(directory / "requests.jsonl")
    assert proof["captured_inputs"] == 3 and proof["clinical_model_invoked"] is False
    assert len({row["body_sha256"] for row in proof["inputs"]}) == 3


def test_capture_proof_refuses_missing_stub_origin_label(tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    capture.proxy.create_app(backend="ollama", upstream="http://127.0.0.1:18080",
        capture_dir=directory, synthetic_data_only=True)
    (directory / "requests.jsonl").write_bytes(b"".join(capture.proxy.canonical(row) + b"\n" for row in captured_rows()))
    with pytest.raises(ValueError, match="origin"):
        capture.capture_proof(directory / "requests.jsonl")


def test_no_acknowledgement_creates_nothing_and_never_starts_capture(tmp_path: Path) -> None:
    arguments = args(tmp_path)
    arguments.allow_retrieval_model_load = False
    with pytest.raises(ValueError, match="acknowledgements"):
        asyncio.run(capture.run(arguments))
    assert not arguments.output_dir.exists()


@pytest.mark.parametrize("budget", [0, 301, float("nan"), float("inf")])
def test_unbounded_capture_window_refused_before_writes(tmp_path: Path, budget: float) -> None:
    arguments = args(tmp_path)
    arguments.budget_seconds = budget
    with pytest.raises(ValueError, match="bounded"):
        asyncio.run(capture.run(arguments))
    assert not arguments.output_dir.exists()


def test_preflight_refusal_never_starts_retrieval_or_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = []

    async def work(*_args: Any) -> None:
        started.append(True)

    monkeypatch.setattr(capture, "capture_inputs", work)
    monkeypatch.setattr(capture.capacity, "hardware_metadata", lambda: {"synthetic_test": True})
    monkeypatch.setattr(capture.qualification.candidate, "inspect_memory", lambda: snapshot(free=19))
    arguments = args(tmp_path)
    assert asyncio.run(capture.run(arguments)) == 2
    report = json.loads((arguments.output_dir / "report.json").read_text())
    assert report["status"] == "not_run_insufficient_memory_preflight" and not started
    assert report["clinical_model_invoked"] is False and report["latency_acceptance_evidence"] is False


def test_compression_guard_cancels_and_awaits_only_owned_capture_context(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        cleaned = []
        readings = iter([snapshot(), snapshot(compressed=262145), snapshot()])
        original_wait = asyncio.wait

        async def fast_wait(tasks: Any, *, timeout: float) -> Any:
            assert timeout == 2
            return await original_wait(tasks, timeout=.001)

        async def work(_args: Any, _report: Any) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        monkeypatch.setattr(capture, "capture_inputs", work)
        monkeypatch.setattr(capture.asyncio, "wait", fast_wait)
        monkeypatch.setattr(capture.capacity, "hardware_metadata", lambda: {"synthetic_test": True})
        monkeypatch.setattr(capture.qualification.candidate, "inspect_memory", lambda: next(readings))
        arguments = args(tmp_path)
        assert await capture.run(arguments) == 1
        report = json.loads((arguments.output_dir / "report.json").read_text())
        assert report["status"] == "aborted_or_incomplete"
        assert report["abort_reason"] == "compression_growth_over_4_gib" and cleaned == [True]
        assert report["clinical_model_invoked"] is False
        assert "strict_200_latency_targets_demonstrated" not in report

    asyncio.run(exercise())


def test_successful_input_capture_report_still_cannot_claim_inference_capacity(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def work(_args: Any, report: dict[str, Any]) -> None:
        report["input_capture"] = {"captured_inputs": 3, "clinical_model_invoked": False}

    monkeypatch.setattr(capture, "capture_inputs", work)
    monkeypatch.setattr(capture.capacity, "hardware_metadata", lambda: {"synthetic_test": True})
    monkeypatch.setattr(capture.qualification.candidate, "inspect_memory", lambda: snapshot())
    arguments = args(tmp_path)
    assert asyncio.run(capture.run(arguments)) == 0
    report = json.loads((arguments.output_dir / "report.json").read_text())
    assert report["status"] == "three_inputs_captured_full_retrieval_verified"
    assert report["clinical_model_invoked"] is False
    assert report["latency_acceptance_evidence"] is False and report["clinical_accuracy_evidence"] is False


def test_200_input_capture_is_explicit_sequential_and_not_sla_evidence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def work(arguments: Any, report: dict[str, Any]) -> None:
        assert arguments.requests == 200
        report["input_capture"] = {"captured_inputs": 200, "clinical_model_invoked": False}

    monkeypatch.setattr(capture, "capture_inputs", work)
    monkeypatch.setattr(capture.capacity, "hardware_metadata", lambda: {"synthetic_test": True})
    monkeypatch.setattr(capture.qualification.candidate, "inspect_memory", lambda: snapshot())
    arguments = args(tmp_path)
    arguments.requests = 200
    assert asyncio.run(capture.run(arguments)) == 0
    report = json.loads((arguments.output_dir / "report.json").read_text())
    assert report["status"] == "200_inputs_captured_full_retrieval_verified"
    assert report["planned_inputs"] == 200 and report["input_capture_concurrency"] == 1
    assert report["latency_acceptance_evidence"] is False and report["clinical_model_invoked"] is False
