"""Resident-only harness safety checks, using fake HTTP/processes exclusively."""
from __future__ import annotations

import asyncio
import json
import sys
from argparse import Namespace
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_resident_grounding as resident


def original():
    return {"name": resident.lease.MODEL, "digest": resident.PINNED_MANIFEST,
            "context_length": 8192, "expires_at": "2286-01-01T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_narrow_child_proxy_forwards_identical_bytes_without_relaxing_generic_bridge(tmp_path):
    calls = []

    async def handler(request):
        calls.append((str(request.url), request.content))
        return httpx.Response(200, json={"done": True, "message": {"content": "Synthetic answer"}})

    validator = resident.bridge.local_origin
    app = resident.resident_proxy_app(tmp_path / "capture", transport=httpx.MockTransport(handler))
    assert resident.bridge.local_origin is validator
    with pytest.raises(resident.bridge.ProxyRefused):
        resident.bridge.local_origin(resident.REGULAR)
    body = b'{ "model":"medgemma:27b", "stream":false, "messages":[{"role":"user","content":"Synthetic only"}], "options":{"num_ctx":8192,"num_predict":512} }'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=("127.0.0.1", 50001)), base_url="http://127.0.0.1:50002") as client:
        result = await client.post("/api/chat", content=body)
    assert result.status_code == 200
    assert calls == [(resident.REGULAR + "/api/chat", body)]
    assert app.state.metadata["owns_inference_engine"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("busy,wrong_weights", [(True, False), (False, True)])
async def test_resident_attestation_rejects_busy_or_mismatched_runner(monkeypatch, busy, wrong_weights):
    async def residents(client):
        return [original()]

    async def handler(request):
        if request.url.path == "/props":
            weights = Path.home() / ".ollama/models/blobs" / resident.benchmark.candidate.MODEL_DIGEST.replace(":", "-")
            return httpx.Response(200, json={"model_path": "wrong" if wrong_weights else str(weights)})
        return httpx.Response(200, json=[{"is_processing": busy, "prompt": "MUST NOT BE STORED"}])

    monkeypatch.setattr(resident.lease, "residents", residents)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await resident.attest_resident(client, "http://127.0.0.1:55001", resident.PINNED_MANIFEST)


def test_runner_whitelist_never_retains_prompt_or_slot_content():
    value = resident.safe_runner_props({"model_path": "fixed", "total_slots": 2, "prompt": "private",
        "slots": [{"prompt": "private"}], "default_generation_settings": {"n_ctx": 8192, "prompt": "private"}})
    assert value == {"model_path": "fixed", "total_slots": 2, "default_generation_settings": {"n_ctx": 8192}}
    assert "private" not in json.dumps(value)


@pytest.mark.asyncio
async def test_restore_refreshes_only_original_ttl_then_calls_existing_lease(monkeypatch):
    current = original()
    current["expires_at"] = "2026-10-23T10:00:00+00:00"
    before = [original(), {"name": "medgemma:4b", "digest": "original4", "context_length": 4096}]
    posts, proofs = [], []

    async def residents(client):
        return [current, before[1]]

    async def restore(client, digest, originals):
        proofs.append((digest, originals))
        return {"status": "restored_and_verified"}

    async def handler(request):
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True})

    monkeypatch.setattr(resident.lease, "residents", residents)
    monkeypatch.setattr(resident.lease, "restore", restore)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resident.restore_original(client, resident.PINNED_MANIFEST, before)
    assert result["status"] == "restored_and_verified"
    assert posts == [{"model": "medgemma:27b", "stream": False, "keep_alive": -1,
                      "options": {"num_ctx": 8192, "num_predict": 1}}]
    assert proofs == [(resident.PINNED_MANIFEST, before)]


@pytest.mark.asyncio
async def test_insufficient_extra_headroom_never_launches_proxy_or_model(tmp_path, monkeypatch):
    monkeypatch.setattr(resident.benchmark.capacity, "hardware_metadata", lambda: {"synthetic_test": True})
    monkeypatch.setattr(resident.lease, "BGE", tmp_path)
    monkeypatch.setattr(resident.lease, "RERANK", tmp_path)
    model = resident.benchmark.candidate.ModelSnapshot("medgemma:27b", resident.benchmark.candidate.MODEL_DIGEST,
        17000000000, resident.PINNED_MANIFEST, "Q4_K_M")
    monkeypatch.setattr(resident.benchmark.candidate, "inspect_model", lambda _: model)
    memory = resident.benchmark.candidate.MemorySnapshot(64 * 1024 ** 3, 18, 11 * 1024 ** 3,
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nSwapouts: 0.\nPages occupied by compressor: 0.")
    monkeypatch.setattr(resident.benchmark.candidate, "inspect_memory", lambda: memory)

    async def attest(*args):
        return {"identity_context_and_idle_verified": True}

    async def residents(client):
        return [original()]

    restored = []

    async def restore(*args):
        restored.append(True)
        return {"status": "restored_and_verified"}

    def forbidden(*args, **kwargs):
        raise AssertionError("No process may launch without retrieval headroom")

    monkeypatch.setattr(resident, "attest_resident", attest)
    monkeypatch.setattr(resident.lease, "residents", residents)
    monkeypatch.setattr(resident, "restore_original", restore)
    monkeypatch.setattr(resident.subprocess, "Popen", forbidden)
    args = Namespace(authorise_existing_resident_inference=True, regular_runner_origin="http://127.0.0.1:55001", output_dir=tmp_path / "report")
    assert await resident.run(args) == 1
    report = json.loads((args.output_dir / "report.json").read_text())
    assert report["status"] == "not_run_memory_preflight"
    assert report["scope"] == resident.SCOPE
    assert report["profile"]["preflight"]["required_available_bytes_estimate"] == 12 * 1024 ** 3
    assert report["strict_200_latency_targets_demonstrated"] is False
    assert restored == [True]


@pytest.mark.asyncio
async def test_explicit_authorization_is_required_before_any_metadata_or_process_access(tmp_path):
    with pytest.raises(ValueError, match="authorization"):
        await resident.run(SimpleNamespace(authorise_existing_resident_inference=False))


@pytest.mark.asyncio
async def test_pressure_guard_terminates_only_owned_proxy_and_restores_regular_residency(tmp_path, monkeypatch):
    monkeypatch.setattr(resident.lease, "BGE", tmp_path)
    monkeypatch.setattr(resident.lease, "RERANK", tmp_path)
    monkeypatch.setattr(resident.benchmark.capacity, "hardware_metadata", lambda: {"synthetic_test": True})
    monkeypatch.setattr(resident.benchmark.qualification, "free_port", lambda: 55002)
    model = resident.benchmark.candidate.ModelSnapshot("medgemma:27b", resident.benchmark.candidate.MODEL_DIGEST,
        17000000000, resident.PINNED_MANIFEST, "Q4_K_M")
    monkeypatch.setattr(resident.benchmark.candidate, "inspect_model", lambda _: model)
    memory = resident.benchmark.candidate.MemorySnapshot(64 * 1024 ** 3, 40, 25 * 1024 ** 3,
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nSwapouts: 0.\nPages occupied by compressor: 0.")
    monkeypatch.setattr(resident.benchmark.candidate, "inspect_memory", lambda: memory)

    async def attest(*args):
        return {"identity_context_and_idle_verified": True}

    async def residents(client):
        return [original()]

    restored, terminated, launches = [], [], []

    async def restore(*args):
        restored.append(True)
        return {"status": "restored_and_verified"}

    process = SimpleNamespace(pid=99001)

    def launch(command, **kwargs):
        launches.append(command)
        return process

    async def terminate(actual):
        if actual is not None:
            assert actual is process
            terminated.append(actual.pid)

    async def monitor(initial, samples, owned):
        assert owned is process
        samples.append({**asdict(memory), "guard_abort_reason": "compression_growth_over_4_gib"})
        await terminate(owned)
        raise resident.benchmark.qualification.QualificationAborted("compression_growth_over_4_gib")

    async def ready(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(resident, "attest_resident", attest)
    monkeypatch.setattr(resident.lease, "residents", residents)
    monkeypatch.setattr(resident, "restore_original", restore)
    monkeypatch.setattr(resident.subprocess, "Popen", launch)
    monkeypatch.setattr(resident.benchmark.qualification, "terminate_owned", terminate)
    monkeypatch.setattr(resident.benchmark.qualification, "monitor_pressure", monitor)
    monkeypatch.setattr(resident.benchmark, "proxy_ready", ready)
    args = Namespace(authorise_existing_resident_inference=True, regular_runner_origin="http://127.0.0.1:55001", output_dir=tmp_path / "report")
    assert await resident.run(args) == 1
    report = json.loads((args.output_dir / "report.json").read_text())
    assert report["status"] == "aborted"
    assert report["abort_reason"] == "compression_growth_over_4_gib"
    assert restored == [True]
    assert len(launches) == 1 and "--resident-proxy-child" in launches[0]
    assert set(terminated) == {99001}
