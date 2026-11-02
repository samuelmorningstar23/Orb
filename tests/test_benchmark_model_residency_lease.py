"""Authorised residency lease tests: fake HTTP and mocked benchmarks only."""
from __future__ import annotations

import asyncio
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import benchmark_model_residency_lease as lease

REAL_CLIENT = httpx.AsyncClient
DIGEST = "original-manifest-digest"


def original() -> dict[str, Any]:
    return {"name": lease.MODEL, "digest": DIGEST, "context_length": 8192,
            "expires_at": "2286-01-01T00:00:00+00:00", "size_vram": 20_000_000_000}


class Regular:
    def __init__(self) -> None:
        self.loaded = True
        self.other = {"name": "other:4b", "digest": "other-original", "context_length": 4096,
                      "expires_at": "2286-01-01T00:00:00+00:00"}
        self.calls: list[tuple[str, str, Any]] = []
        self.busy = False
        self.props_wrong = False
        self.pending_polls = 0
        self.ambiguous_unload = False
        self.reappear: asyncio.Event | None = None
        self.restore_started: asyncio.Event | None = None
        self.allow_restore: asyncio.Event | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.url.path == "/props":
            weights = Path.home() / ".ollama/models/blobs" / lease.benchmark.candidate.MODEL_DIGEST.replace(":", "-")
            return httpx.Response(200, json={"model_path": "wrong" if self.props_wrong else str(weights)})
        if request.url.path == "/slots":
            return httpx.Response(200, json=[{"is_processing": self.busy}])
        if request.url.path == "/api/ps":
            resident = self.loaded or self.pending_polls > 0 or (self.reappear is not None and self.reappear.is_set())
            self.pending_polls = max(0, self.pending_polls - 1)
            return httpx.Response(200, json={"models": [self.other, *([original()] if resident else [])]})
        assert request.method == "POST" and request.url.path == "/api/generate"
        assert isinstance(body, dict)
        assert body["model"] == lease.MODEL
        if body["keep_alive"] == 0:
            self.loaded = False
            if self.ambiguous_unload:
                self.ambiguous_unload = False
                raise httpx.ReadError("Synthetic ambiguous unload acknowledgement", request=request)
        else:
            assert body["keep_alive"] == -1 and body["stream"] is False
            assert body["options"] == {"num_ctx": 8192, "num_predict": 1}
            if self.restore_started is not None:
                self.restore_started.set()
            if self.allow_restore is not None:
                await self.allow_restore.wait()
            self.loaded = True
        return httpx.Response(200, json={"done": True})

    def unloads(self) -> int:
        return sum(path == "/api/generate" and body["keep_alive"] == 0 for _, path, body in self.calls)

    def restores(self) -> int:
        return sum(path == "/api/generate" and body["keep_alive"] == -1 for _, path, body in self.calls)


def install(monkeypatch: pytest.MonkeyPatch, regular: Regular) -> None:
    def client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return REAL_CLIENT(transport=httpx.MockTransport(regular.handler), **kwargs)

    monkeypatch.setattr(lease.httpx, "AsyncClient", client)
    monkeypatch.setattr(lease.benchmark.candidate, "inspect_model", lambda _: SimpleNamespace(manifest_sha256=DIGEST))


def arguments(tmp_path: Path) -> Namespace:
    return Namespace(output_dir=tmp_path / "new-lease", regular_runner_origin="http://127.0.0.1:55001",
                     authorise_temporary_unload_and_restore=True)


@pytest.mark.parametrize("kind,engine,phases", [
    ("baseline-only", "ollama", ["quality", "replay"]),
    ("quality-only", "mlx", ["quality"])])
def test_diagnostic_experiments_never_continue_into_other_timings(
        tmp_path, monkeypatch, kind, engine, phases):
    regular = Regular()
    install(monkeypatch, regular)
    args = arguments(tmp_path)
    args.experiment, args.mlx_prefix_cache_size = kind, 0
    profiles = []

    async def benchmark(selected):
        profiles.append(selected)
        selected.output_dir.mkdir()
        (selected.output_dir / "report.json").write_text(json.dumps({
            "status": "complete", "profile": {"phases": [{"name": "quality", "all_cases_passed": True}]}}))
        return 0

    monkeypatch.setattr(lease.benchmark, "run", benchmark)
    assert asyncio.run(lease.run(args)) == 0
    assert len(profiles) == 1 and profiles[0].engine == engine and profiles[0].phases == phases
    if engine == "mlx":
        assert profiles[0].mlx_prefix_cache_size == 0
    else:
        assert profiles[0].diagnose_native_penalties is True
    assert regular.unloads() == regular.restores() == 1


@pytest.mark.parametrize("swa_full", ["on", "off"])
@pytest.mark.parametrize("prefix_cache_mib", [0, 8192])
def test_original_weight_full_workflow_run_keeps_fidelity_before_actual_burst(tmp_path, monkeypatch, swa_full, prefix_cache_mib):
    regular = Regular()
    install(monkeypatch, regular)
    args = arguments(tmp_path)
    args.experiment = "native-full-rag-only"
    args.native_swa_full = swa_full
    args.native_prefix_cache_mib = prefix_cache_mib
    profiles = []

    async def benchmark(selected):
        profiles.append(selected)
        selected.output_dir.mkdir()
        (selected.output_dir / "report.json").write_text(json.dumps({
            "status": "complete", "profile": {"phases": [{"name": "quality", "all_cases_passed": True}]}}))
        return 0

    monkeypatch.setattr(lease.benchmark, "run", benchmark)
    assert asyncio.run(lease.run(args)) == 0
    assert len(profiles) == 1
    selected = profiles[0]
    assert selected.engine == "ollama" and selected.parallel == 2
    assert selected.native_swa_full == swa_full
    assert selected.native_prefix_cache_mib == prefix_cache_mib
    assert selected.phases == ["quality", "sequential", "burst"]
    assert not selected.engine_only and selected.workflow_quality_checks
    assert selected.embedding_model == lease.BGE and selected.reranker_model == lease.RERANK
    assert not selected.diagnose_native_penalties
    assert regular.unloads() == regular.restores() == 1


@pytest.mark.parametrize("mlx_passed", [True, False])
def test_serving_recheck_retains_quality_gate_and_full_retrieval(
        tmp_path, monkeypatch, mlx_passed):
    regular = Regular()
    install(monkeypatch, regular)
    args = arguments(tmp_path)
    args.experiment = "serving-recheck"
    profiles = []

    async def benchmark(selected):
        profiles.append(selected)
        selected.output_dir.mkdir()
        (selected.output_dir / "report.json").write_text(json.dumps({
            "status": "complete", "profile": {"phases": [
                {"name": "quality", "all_cases_passed": mlx_passed}]}}))
        return 0

    monkeypatch.setattr(lease.benchmark, "run", benchmark)
    assert asyncio.run(lease.run(args)) == 0
    expected = [("mlx", 8, True, ["quality", "replay200"])]
    if mlx_passed:
        expected += [("ollama", 2, True, ["quality", "replay200"]),
                     ("mlx", 8, False, ["quality", "sequential", "burst"])]
    assert [(p.engine, p.parallel, p.engine_only, p.phases) for p in profiles] == expected
    if mlx_passed:
        assert profiles[-1].embedding_model == lease.BGE
        assert profiles[-1].reranker_model == lease.RERANK
    assert regular.unloads() == regular.restores() == 1


@pytest.mark.parametrize("engine,width", [("mlx", 1), ("mlx", 2), ("mlx", 4), ("mlx", 8),
                                          ("ollama", 1), ("ollama", 2), ("ollama", 4)])
def test_full_rag_diagnostic_keeps_real_retrieval_and_quality_gate(tmp_path, monkeypatch, engine, width):
    regular = Regular()
    install(monkeypatch, regular)
    args = arguments(tmp_path)
    args.experiment = "full-rag-only" if engine == "mlx" else "native-full-rag-only"
    args.mlx_prefix_cache_size = 0
    args.mlx_full_rag_parallel = args.native_full_rag_parallel = width
    profiles = []

    async def benchmark(selected):
        profiles.append(selected)
        selected.output_dir.mkdir()
        (selected.output_dir / "report.json").write_text(json.dumps({
            "status": "aborted", "profile": {"phases": []}}))
        return 1

    monkeypatch.setattr(lease.benchmark, "run", benchmark)
    assert asyncio.run(lease.run(args)) == 0  # Lease completion, not benchmark acceptance.
    assert len(profiles) == 1
    p = profiles[0]
    assert p.engine == engine and p.parallel == width
    if engine == "mlx":
        assert p.mlx_prefix_cache_size == 0
    assert p.phases == ["quality", "sequential", "burst"] and not p.engine_only
    assert p.embedding_model == lease.BGE and p.reranker_model == lease.RERANK
    assert regular.unloads() == regular.restores() == 1


@pytest.mark.parametrize("first_native_status", ["not_run_memory_preflight", "aborted", "complete"])
def test_serving_recheck_only_retries_a_preflight_skip_once(tmp_path, monkeypatch, first_native_status):
    regular = Regular()
    install(monkeypatch, regular)
    args = arguments(tmp_path)
    args.experiment = "serving-recheck"
    names = []
    waits = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        if seconds == 5:
            waits.append(seconds)
            return
        await real_sleep(seconds)

    async def benchmark(selected):
        names.append(selected.output_dir.name)
        selected.output_dir.mkdir()
        status = first_native_status if selected.engine == "ollama" else "complete"
        # Even an unchanged second skip must end the retry sequence.
        (selected.output_dir / "report.json").write_text(json.dumps({
            "status": status, "profile": {"phases": [
                {"name": "quality", "all_cases_passed": True}]}}))
        return 0

    monkeypatch.setattr(lease.asyncio, "sleep", sleep)
    monkeypatch.setattr(lease.benchmark, "run", benchmark)
    assert asyncio.run(lease.run(args)) == 0
    assert names.count("ollama-p2-200-after-reclaim") == int(first_native_status == "not_run_memory_preflight")
    assert waits == ([5] if first_native_status == "not_run_memory_preflight" else [])
    assert names[-1] == "mlx-p8-full-rag"
    assert regular.unloads() == regular.restores() == 1


@pytest.mark.parametrize("change", [{"digest": "wrong"}, {"context_length": 4096}])
def test_wrong_digest_or_context_cannot_authorise_unload(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="identity or context"):
        lease.verify_original([{**original(), **change}], DIGEST)


@pytest.mark.parametrize("issue", ["busy", "props_wrong"])
def test_busy_or_wrong_runner_is_refused_before_unload(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issue: str) -> None:
    regular = Regular()
    setattr(regular, issue, True)
    install(monkeypatch, regular)
    assert asyncio.run(lease.run(arguments(tmp_path))) == 1
    assert regular.unloads() == regular.restores() == 0


def test_release_polls_until_asynchronous_unload_really_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        regular = Regular()
        regular.pending_polls = 2
        sleeps = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr(lease.asyncio, "sleep", sleep)
        async with REAL_CLIENT(transport=httpx.MockTransport(regular.handler)) as client:
            await lease.release(client)
        assert regular.unloads() == 1 and sleeps == [.25, .25]
        assert sum(path == "/api/ps" for _, path, _ in regular.calls) == 3
    asyncio.run(exercise())


def test_restore_has_independent_deadline_pins_original_context_and_preserves_other_models(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        regular = Regular()
        regular.loaded = False
        deadlines = []
        real_timeout = asyncio.timeout

        def timeout(delay: float):
            deadlines.append(delay)
            return real_timeout(delay)

        monkeypatch.setattr(lease.asyncio, "timeout", timeout)
        before = [original(), dict(regular.other)]
        async with REAL_CLIENT(transport=httpx.MockTransport(regular.handler)) as client:
            result = await lease.restore(client, DIGEST, before)
        assert deadlines == [120] and regular.restores() == 1 and regular.unloads() == 0
        assert result["status"] == "restored_and_verified" and result["other_original_residents_unchanged"]
        lease.verify_others(before, [original(), regular.other])
        with pytest.raises(ValueError, match="Another original resident"):
            lease.verify_others(before, [original(), {**regular.other, "context_length": 2048}])
    asyncio.run(exercise())


FOUR_DIGEST = "9fe4e9a6c9bda2d007bb595515550eafbc8e226622adff9257f3241eba334c4d"


def original_four() -> dict[str, Any]:
    return {"name": "medgemma:4b", "digest": FOUR_DIGEST, "context_length": 4096,
            "expires_at": "2286-01-01T00:00:00+00:00"}


class FourRecovery:
    """Fake27B reload evicts4B, reproducing the actual residency failure."""

    def __init__(self) -> None:
        self.models = [original_four()]
        self.after_27b = [original()]
        self.tags: Any = {"models": [{"name": "medgemma:4b", "digest": FOUR_DIGEST}]}
        self.calls: list[tuple[str, str, Any]] = []
        self.after_tags: list[dict[str, Any]] | None = None
        self.acknowledge_four = True
        self.expire_four = False
        self.evict_27b_after_four = False

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": self.models})
        if request.url.path == "/api/tags":
            if self.after_tags is not None:
                self.models = self.after_tags
            return httpx.Response(200, json=self.tags)
        assert request.method == "POST" and request.url.path == "/api/generate"
        assert isinstance(body, dict)
        assert body["keep_alive"] == -1 and body["options"]["num_predict"] == 1
        if body["model"] == lease.MODEL:
            assert body["options"]["num_ctx"] == 8192
            self.models = self.after_27b
        else:
            assert body["model"] == "medgemma:4b" and body["options"]["num_ctx"] == 4096
            if not self.acknowledge_four:
                return httpx.Response(200, json={"done": False})
            four = original_four()
            if self.expire_four:
                four["expires_at"] = "2026-10-22T00:00:00+00:00"
            self.models = [four] if self.evict_27b_after_four else [original(), four]
        return httpx.Response(200, json={"done": True})

    def four_loads(self) -> list[Any]:
        return [body for method, path, body in self.calls
                if method == "POST" and path == "/api/generate" and body["model"] == "medgemma:4b"]


def test_restore_reload_only_missing_pinned_original_four_after_27b_with_one_total_deadline(monkeypatch):
    async def exercise():
        regular = FourRecovery()
        deadlines = []
        real_timeout = asyncio.timeout

        def timeout(delay):
            deadlines.append(delay)
            return real_timeout(delay)

        monkeypatch.setattr(lease.asyncio, "timeout", timeout)
        async with REAL_CLIENT(transport=httpx.MockTransport(regular.handler)) as client:
            result = await lease.restore(client, DIGEST, [original(), original_four()])
        assert deadlines == [120]
        assert [(method, path) for method, path, _ in regular.calls] == [
            ("GET", "/api/ps"), ("POST", "/api/generate"), ("GET", "/api/ps"),
            ("GET", "/api/tags"), ("GET", "/api/ps"), ("POST", "/api/generate"), ("GET", "/api/ps")]
        assert len(regular.four_loads()) == 1
        assert result["auto_reloaded_original_residents"] == [{"name": "medgemma:4b", "digest": FOUR_DIGEST,
            "context_length": 4096, "indefinite_residency_verified": True}]
        assert result["all_original_residents_identity_and_context_verified"] is True
        assert result["other_original_residents_unchanged"] is True
    asyncio.run(exercise())


@pytest.mark.parametrize("tags", [{"models": [{"name": "medgemma:4b", "digest": "wrong"}]},
    {"models": [{"name": "alias:4b", "digest": FOUR_DIGEST}]}, {"models": []}, {"models": "malformed"}])
def test_restore_missing_four_refuses_unattested_installed_name_or_digest(tags):
    async def exercise():
        regular = FourRecovery()
        regular.tags = tags
        async with REAL_CLIENT(transport=httpx.MockTransport(regular.handler)) as client:
            with pytest.raises(ValueError, match="original4B"):
                await lease.restore(client, DIGEST, [original(), original_four()])
        assert not regular.four_loads()
    asyncio.run(exercise())


@pytest.mark.parametrize("change", [{"digest": "wrong"}, {"context_length": 2048}])
@pytest.mark.parametrize("reappears_after_tags", [False, True])
def test_restore_never_changes_an_existing_wrong_four_resident(change, reappears_after_tags):
    async def exercise():
        regular = FourRecovery()
        wrong = [original(), {**original_four(), **change}]
        if reappears_after_tags:
            regular.after_tags = wrong
        else:
            regular.after_27b = wrong
        async with REAL_CLIENT(transport=httpx.MockTransport(regular.handler)) as client:
            with pytest.raises(ValueError, match="different identity or context"):
                await lease.restore(client, DIGEST, [original(), original_four()])
        assert not regular.four_loads()
    asyncio.run(exercise())


def test_restore_never_loads_another_missing_original_model_or_attempts_four_first():
    async def exercise():
        regular = FourRecovery()
        other = {"name": "unapproved:7b", "digest": "other", "context_length": 2048}
        async with REAL_CLIENT(transport=httpx.MockTransport(regular.handler)) as client:
            with pytest.raises(ValueError, match="Another original resident"):
                await lease.restore(client, DIGEST, [original(), original_four(), other])
        assert not regular.four_loads()
        assert all(body is None or body["model"] != "unapproved:7b" for _, _, body in regular.calls)
    asyncio.run(exercise())


def test_restore_skips_four_load_if_correct_resident_reappears_during_tag_check():
    async def exercise():
        regular = FourRecovery()
        regular.after_tags = [original(), original_four()]
        async with REAL_CLIENT(transport=httpx.MockTransport(regular.handler)) as client:
            result = await lease.restore(client, DIGEST, [original(), original_four()])
        assert result["auto_reloaded_original_residents"] == []
        assert not regular.four_loads()
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["not_acknowledged", "expires", "evicts_27b"])
def test_restoration_cannot_succeed_without_final_acknowledgement_both_models_and_indefinite_four(failure):
    async def exercise():
        regular = FourRecovery()
        if failure == "not_acknowledged":
            regular.acknowledge_four = False
        elif failure == "expires":
            regular.expire_four = True
        else:
            regular.evict_27b_after_four = True
        async with REAL_CLIENT(transport=httpx.MockTransport(regular.handler)) as client:
            with pytest.raises(ValueError):
                await lease.restore(client, DIGEST, [original(), original_four()])
    asyncio.run(exercise())


def test_ambiguous_unload_still_restores_and_failed_experiment_is_nonzero(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    regular = Regular()
    regular.ambiguous_unload = True
    install(monkeypatch, regular)
    assert asyncio.run(lease.run(arguments(tmp_path))) == 1
    report = json.loads((tmp_path / "new-lease/lease-report.json").read_text())
    assert report["status"] == "aborted" and report["restoration"]["status"] == "restored_and_verified"
    assert regular.unloads() == regular.restores() == 1


@pytest.mark.parametrize("repeated_cancel", [False, True])
def test_benchmark_cancellation_cleans_owned_work_then_restores_even_after_second_cancel(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repeated_cancel: bool) -> None:
    async def exercise() -> None:
        regular = Regular()
        regular.restore_started, regular.allow_restore = asyncio.Event(), asyncio.Event()
        started, cleaned = asyncio.Event(), asyncio.Event()
        install(monkeypatch, regular)

        async def benchmark(_args: Any) -> int:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
            return 1

        monkeypatch.setattr(lease.benchmark, "run", benchmark)
        task = asyncio.create_task(lease.run(arguments(tmp_path)))
        await started.wait()
        task.cancel()
        await regular.restore_started.wait()
        assert cleaned.is_set()  # Never restore atop our still-running test model.
        if repeated_cancel:
            task.cancel()
            await asyncio.sleep(0)
        regular.allow_restore.set()
        assert await task == 1
        report = json.loads((tmp_path / "new-lease/lease-report.json").read_text())
        assert report["status"] == "aborted" and report["restoration"]["status"] == "restored_and_verified"
        assert regular.unloads() == regular.restores() == 1
        if repeated_cancel:
            assert report["cancellation_during_recovery"] is True

    asyncio.run(exercise())


def test_external_reappearance_aborts_owned_benchmark_without_second_unload(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        regular = Regular()
        started, cleaned = asyncio.Event(), asyncio.Event()
        regular.reappear = started
        install(monkeypatch, regular)
        real_sleep = asyncio.sleep

        async def sleep(_delay: float) -> None:
            await real_sleep(.001)

        async def benchmark(_args: Any) -> int:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
            return 1

        monkeypatch.setattr(lease.asyncio, "sleep", sleep)
        monkeypatch.setattr(lease.benchmark, "run", benchmark)
        assert await lease.run(arguments(tmp_path)) == 1
        report = json.loads((tmp_path / "new-lease/lease-report.json").read_text())
        assert report["status"] == "aborted" and "reloaded by another requester" in report["error"]
        assert cleaned.is_set() and regular.unloads() == 1 and regular.restores() == 0
        assert report["restoration"]["status"] == "restored_and_verified"
    asyncio.run(exercise())
