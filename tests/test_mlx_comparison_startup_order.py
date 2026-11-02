"""Owned startup/lifecycle tests with fake processes/HTTP/models; no listeners/GPU."""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_mlx_ollama as benchmark
import llm_fidelity_cases


def memory(*, compression_pages=0):
    gib = benchmark.candidate.GIB
    return benchmark.candidate.MemorySnapshot(64 * gib, 80, 60 * gib,
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        f"Swapouts: 0.\nPages occupied by compressor: {compression_pages}.\n")


def prepare(tmp_path, monkeypatch, *, engine_only=False):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text(json.dumps({"num_hidden_layers": 62, "sliding_window_pattern": 6,
        "num_key_value_heads": 16, "head_dim": 128, "sliding_window": 1024}))
    (model / "conversion-manifest.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"not-a-model")
    python = tmp_path / "fake-python"
    python.touch()
    embedding, reranker = tmp_path / "embedding", tmp_path / "reranker"
    embedding.mkdir()
    reranker.mkdir()
    argv = ["--engine", "mlx", "--parallel", "8", "--phases", "quality", "--allow-model-load",
            "--model-dir", str(model), "--mlx-python", str(python), "--mlx-loading-mode", "mixed-staged",
            "--output-dir", str(tmp_path / "evidence")]
    if engine_only:
        argv.append("--engine-only")
    else:
        argv += ["--embedding-model", str(embedding), "--reranker-model", str(reranker)]
    args = benchmark.parser().parse_args(argv)
    events, processes, terminated = [], [], []
    state = {"fixture_alive": False}
    monkeypatch.setattr(benchmark.candidate, "inspect_memory", memory)
    monkeypatch.setattr(benchmark, "validate_mlx_candidate", lambda *a, **kw: {
        "source_sha256": "synthetic-source", "benchmark_checksum_io": {}})
    monkeypatch.setattr(benchmark.capacity, "hardware_metadata", dict)
    ports = iter((10001, 10002))
    monkeypatch.setattr(benchmark.qualification, "free_port", lambda: next(ports))
    monkeypatch.setattr(benchmark.qualification, "install_synthetic_answer_capture", lambda: None)
    monkeypatch.setattr(benchmark.qualification, "system_pressure_summary", lambda _: {})
    monkeypatch.setattr(benchmark.qualification, "progress", lambda *a, **kw: None)

    class Process:
        def __init__(self, *a, **kw):
            self.pid, self.returncode = 100 + len(processes), None
            self.command = a[0]
            processes.append(self)
            events.append("engine_popen" if len(processes) == 1 else "proxy_popen")
            if not engine_only:
                assert state["fixture_alive"]
                assert "fixture_attested" in events
        def poll(self):
            return self.returncode
    monkeypatch.setattr(benchmark.subprocess, "Popen", Process)

    class ClosedSocket:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return None
        def settimeout(self, value):
            return None
        def connect_ex(self, address):
            return 1
    monkeypatch.setattr(benchmark, "socket", SimpleNamespace(socket=lambda: ClosedSocket()))

    async def ready(*a):
        events.append("engine_or_proxy_ready")
        if not engine_only:
            assert state["fixture_alive"]
    async def terminate(process):
        if process is not None:
            events.append("terminate_owned")
            process.returncode = 0
            terminated.append(process)
    async def pressure(initial, samples, process, *, process_resolver=None):
        events.append("monitor_started")
        if not engine_only:
            assert process is None and process_resolver() is None
        else:
            assert process is processes[0] and process_resolver is None
        try:
            await asyncio.Event().wait()
        finally:
            events.append("monitor_cancelled")
    monkeypatch.setattr(benchmark, "native_ready", ready)
    monkeypatch.setattr(benchmark, "proxy_ready", ready)
    monkeypatch.setattr(benchmark.qualification, "terminate_owned", terminate)
    monkeypatch.setattr(benchmark.qualification, "monitor_pressure", pressure)

    @asynccontextmanager
    async def fixture(settings, origin, **kwargs):
        events.append("fixture_entered")
        assert processes == []
        assert origin == "http://127.0.0.1:10002"
        assert settings["ORB_SAGE_RETRIEVAL_WORKERS"] == "4"
        state["fixture_alive"] = True
        try:
            await asyncio.sleep(0)
            events.append("fixture_attested")
            yield "http://fixture.invalid", {"synthetic_data_only": True}, object()
        finally:
            kwargs["startup_diagnostics"]["last_stage"] = "stage=full_retrieval_ready; exception=unknown"
            state["fixture_alive"] = False
            events.append("fixture_closed")
    monkeypatch.setattr(benchmark.capacity, "orb_stub_target", fixture)

    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"retrieval_executor": {"workers": 4}, "full_retrieval_proof": {"synthetic": True}}
    class Client:
        def __init__(self, **kw):
            assert kw == {"trust_env": False}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url, **kw):
            assert url == "http://fixture.invalid/__capacity_fixture__" and state["fixture_alive"]
            events.append("fixture_http_attestation")
            return Response()
    monkeypatch.setattr(benchmark.httpx, "AsyncClient", Client)

    async def warm(origin):
        assert origin == "http://127.0.0.1:10002" and state["fixture_alive"]
        events.append("model_warmup")
        return {"wall_s": .01}
    async def cases(origin, result_observer):
        assert state["fixture_alive"]
        events.append("quality_started")
        await asyncio.sleep(0)
        assert state["fixture_alive"]
        events.append("quality_finished")
        return {"successful": 8, "total": 8, "all_cases_passed": True}
    monkeypatch.setattr(benchmark.qualification, "warm_model", warm)
    monkeypatch.setattr(llm_fidelity_cases, "run_cases", cases)
    return args, events, processes, terminated, state


def test_real_fixture_attests_before_engine_and_stays_alive_through_real_phases(tmp_path, monkeypatch):
    args, events, processes, terminated, state = prepare(tmp_path, monkeypatch)
    assert asyncio.run(benchmark.run(args)) == 0
    assert events.index("monitor_started") < events.index("fixture_entered")
    assert events.index("fixture_attested") < events.index("engine_popen") < events.index("model_warmup")
    assert events.index("quality_finished") < events.index("fixture_closed") < events.index("terminate_owned")
    assert events.count("fixture_entered") == 1  # phases reuses the exact live worker.
    assert events.count("fixture_http_attestation") == 2
    assert terminated == list(reversed(processes)) and not state["fixture_alive"]
    report = json.loads((args.output_dir / "report.json").read_text())
    assert report["status"] == "complete"
    assert report["profile"]["retrieval_startup"]["fixture_ready_before_engine_launch"] is True
    assert report["profile"]["retrieval_startup"]["last_stage"].startswith("stage=full_retrieval_ready")
    assert report["profile"]["preflight"]["model"]["http_listen_backlog"] == 256
    assert report["strict_200_latency_targets_demonstrated"] is False


def test_engine_only_keeps_original_launch_order_and_never_starts_fixture(tmp_path, monkeypatch):
    args, events, processes, terminated, _ = prepare(tmp_path, monkeypatch, engine_only=True)
    async def engine_phases(origin, selected, profile):
        events.append("engine_only_phases")
    monkeypatch.setattr(benchmark, "engine_only_phases", engine_phases)
    assert asyncio.run(benchmark.run(args)) == 0
    assert events.index("engine_popen") < events.index("monitor_started")
    assert "fixture_entered" not in events and "model_warmup" not in events
    assert len(processes) == 2 and terminated == list(reversed(processes))


def test_prelaunch_guard_cancels_and_cleans_fixture_without_spawning_engine(tmp_path, monkeypatch):
    args, events, processes, terminated, state = prepare(tmp_path, monkeypatch)
    async def scenario():
        entered = asyncio.Event()
        @asynccontextmanager
        async def fixture(*a, **kw):
            state["fixture_alive"] = True
            entered.set()
            try:
                await asyncio.Event().wait()
                yield None
            finally:
                state["fixture_alive"] = False
                kw["startup_diagnostics"]["last_stage"] = "stage=embedding_probe; exception=unknown"
                events.append("fixture_closed")
        async def pressure(initial, samples, process, *, process_resolver=None):
            assert process is None
            await entered.wait()
            assert process_resolver() is None and processes == []
            samples.append({"guard_abort_reason": "compression_growth_over_4_gib"})
            raise benchmark.qualification.QualificationAborted("compression_growth_over_4_gib")
        monkeypatch.setattr(benchmark.capacity, "orb_stub_target", fixture)
        monkeypatch.setattr(benchmark.qualification, "monitor_pressure", pressure)
        return await benchmark.run(args)
    assert asyncio.run(scenario()) == 1
    assert processes == terminated == [] and events == ["fixture_closed"]
    assert not state["fixture_alive"]
    report = json.loads((args.output_dir / "report.json").read_text())
    assert report["abort_reason"] == "compression_growth_over_4_gib"
    assert report["profile"]["retrieval_startup"]["last_stage"].startswith("stage=embedding_probe")
    assert report["profile"]["phases"] == []


@pytest.mark.parametrize("handoff", [False, True])
def test_real_pressure_monitor_handles_none_then_resolves_only_owned_process(monkeypatch, handoff):
    qualification = benchmark.qualification
    initial = memory()
    safe, over = memory(), memory(compression_pages=4 * benchmark.candidate.GIB // 16384 + 1)
    snapshots = iter([safe, over] if handoff else [over])
    monkeypatch.setattr(qualification.candidate, "inspect_memory", lambda: next(snapshots))
    original_sleep = asyncio.sleep
    owned = SimpleNamespace(poll=lambda: None, pid=12345)
    state = {"owned": None, "ticks": 0}
    terminated, samples = [], []
    async def quick_sleep(seconds):
        assert seconds == 2
        state["ticks"] += 1
        if handoff and state["ticks"] == 2:
            state["owned"] = owned
        await original_sleep(0)
    async def terminate(process):
        terminated.append(process)
    monkeypatch.setattr(qualification.asyncio, "sleep", quick_sleep)
    monkeypatch.setattr(qualification, "terminate_owned", terminate)
    with pytest.raises(qualification.QualificationAborted, match="compression_growth_over_4_gib"):
        asyncio.run(qualification.monitor_pressure(initial, samples, None, process_resolver=lambda: state["owned"]))
    assert terminated == ([owned] if handoff else [None])
    assert len(samples) == (2 if handoff else 1)
    assert samples[-1]["guard_abort_reason"] == "compression_growth_over_4_gib"
