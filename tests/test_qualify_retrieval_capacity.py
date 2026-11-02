"""The retrieval-only supervisor refuses pressure and awaits owned cleanup."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qualify_retrieval_capacity as guard
from serve_ollama_candidate import GIB, MemorySnapshot


def arguments(path: Path) -> list[str]:
    for name, architecture in (("embedding", "XLMRobertaModel"), ("reranker", "XLMRobertaForSequenceClassification")):
        model = path / name
        model.mkdir()
        (model / "config.json").write_text(json.dumps({"hidden_size": 1024, "architectures": [architecture]}))
    return ["--orb-stub", "--embedding-model", str(path / "embedding"), "--reranker-model", str(path / "reranker")]


def memory(free: int) -> MemorySnapshot:
    return MemorySnapshot(64 * GIB, free, 64 * GIB * free // 100,
                          "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nSwapouts: 0.\nPages occupied by compressor: 0.\n")


def test_low_headroom_refused_before_fixture_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "inspect_memory", lambda: memory(10))
    output = tmp_path / "report" / "guard.json"
    code = asyncio.run(guard.qualify(arguments(tmp_path), output))
    assert code == 2 and json.loads(output.read_text())["outcome"] == "refused_insufficient_preflight_headroom"


def test_completed_runner_records_guard_without_loading_any_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "inspect_memory", lambda: memory(50))
    async def fake_main(args: Any) -> int:
        return 1
    monkeypatch.setattr(guard.capacity, "main_async", fake_main)
    output = tmp_path / "guard.json"
    assert asyncio.run(guard.qualify(arguments(tmp_path), output)) == 1
    report = json.loads(output.read_text())
    assert report["outcome"] == "completed" and report["runner_exit_code"] == 1


def test_pressure_cancels_runner_and_awaits_its_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = iter((memory(50), memory(10), memory(10), memory(50)))
    monkeypatch.setattr(guard, "inspect_memory", lambda: next(snapshots))
    cleaned = []
    async def fake_main(args: Any) -> int:
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)
        return 0
    async def immediate_wait(*args: Any, **kwargs: Any) -> tuple[set[Any], set[Any]]:
        await asyncio.sleep(0)
        return set(), set()
    monkeypatch.setattr(guard.capacity, "main_async", fake_main)
    monkeypatch.setattr(guard.asyncio, "wait", immediate_wait)
    output = tmp_path / "guard.json"
    assert asyncio.run(guard.qualify(arguments(tmp_path), output)) == 2
    assert cleaned and json.loads(output.read_text())["outcome"] == "sustained_memory_pressure"
