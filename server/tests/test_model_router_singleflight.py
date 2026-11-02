"""Readiness bursts must not amplify local HTTP calls or change model quality."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import gc
from typing import Any
import weakref

import pytest

import config
from services import model_router


@pytest.fixture(autouse=True)
def clean_router(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "MODELS_ENABLED", True)
    monkeypatch.setattr(config, "WARD_RECORD", False)
    model_router.invalidate_residency_cache()
    yield
    model_router.invalidate_residency_cache()


def install_client(monkeypatch: pytest.MonkeyPatch,
                   handler: Callable[[str], Awaitable[dict[str, object]]]) -> list[str]:
    calls: list[str] = []

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self.payload

    class Client:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["trust_env"] is False
            assert kwargs["timeout"] == 2.0

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, origin: str) -> Response:
            calls.append(origin)
            return Response(await handler(origin))

    monkeypatch.setattr(model_router.httpx, "AsyncClient", Client)
    return calls


def test_200_cold_callers_share_one_probe_and_success_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        release, started = asyncio.Event(), asyncio.Event()

        async def handler(_origin: str) -> dict[str, object]:
            started.set()
            await release.wait()
            return {"models": [{"name": "clinical:27b"}]}

        calls = install_client(monkeypatch, handler)
        requests = [asyncio.create_task(model_router.probe_residency()) for _ in range(200)]
        await started.wait()
        assert len(calls) == 1
        release.set()
        assert await asyncio.gather(*requests) == [frozenset({"clinical:27b"})] * 200
        assert await model_router.probe_residency() == frozenset({"clinical:27b"})
        assert len(calls) == 1
        monkeypatch.setattr(model_router, "_PS_TTL_SECONDS", 0.0)
        assert await model_router.probe_residency() == frozenset({"clinical:27b"})
        assert len(calls) == 2

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_surviving_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        release, started = asyncio.Event(), asyncio.Event()

        async def handler(_origin: str) -> dict[str, object]:
            started.set()
            await release.wait()
            return {"models": [{"name": "clinical:27b"}]}

        calls = install_client(monkeypatch, handler)
        cancelled = asyncio.create_task(model_router.probe_residency())
        survivor = asyncio.create_task(model_router.probe_residency())
        await started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        assert await survivor == frozenset({"clinical:27b"})
        assert len(calls) == 1

    asyncio.run(scenario())


def test_short_wait_budget_does_not_poison_other_waiters(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        release, started = asyncio.Event(), asyncio.Event()

        async def handler(_origin: str) -> dict[str, object]:
            started.set()
            await release.wait()
            return {"models": []}

        calls = install_client(monkeypatch, handler)
        short = asyncio.create_task(model_router.probe_residency(timeout=0.001))
        survivor = asyncio.create_task(model_router.probe_residency())
        await started.wait()
        assert await short is None
        release.set()
        assert await survivor == frozenset()
        assert len(calls) == 1

    asyncio.run(scenario())


def test_invalidation_fences_inflight_reply_and_old_waiters(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        release, started = asyncio.Event(), asyncio.Event()
        sequence = 0

        async def handler(_origin: str) -> dict[str, object]:
            nonlocal sequence
            sequence += 1
            if sequence == 1:
                started.set()
                await release.wait()
                return {"models": [{"name": "old:27b"}]}
            return {"models": [{"name": "current:27b"}]}

        calls = install_client(monkeypatch, handler)
        old = asyncio.create_task(model_router.probe_residency())
        await started.wait()
        model_router.invalidate_residency_cache()
        assert await model_router.probe_residency() == frozenset({"current:27b"})
        release.set()
        assert await old is None
        assert await model_router.probe_residency() == frozenset({"current:27b"})
        assert len(calls) == 2

    asyncio.run(scenario())


def test_origin_change_and_return_cannot_restore_old_inflight_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        release, started = asyncio.Event(), asyncio.Event()
        sequence = 0

        async def handler(origin: str) -> dict[str, object]:
            nonlocal sequence
            sequence += 1
            current = sequence
            if current == 1:
                started.set()
                await release.wait()
            return {"models": [{"name": f"reply-{current}:{origin[-5:]}"}]}

        calls = install_client(monkeypatch, handler)
        monkeypatch.setattr(model_router, "OLLAMA_BASE_URL", "http://127.0.0.1:11438")
        old = asyncio.create_task(model_router.probe_residency())
        await started.wait()
        monkeypatch.setattr(model_router, "OLLAMA_BASE_URL", "http://127.0.0.1:11439")
        assert await model_router.probe_residency() == frozenset({"reply-2:pi/ps"})
        monkeypatch.setattr(model_router, "OLLAMA_BASE_URL", "http://127.0.0.1:11438")
        assert await model_router.probe_residency() == frozenset({"reply-3:pi/ps"})
        release.set()
        assert await old is None
        assert await model_router.probe_residency() == frozenset({"reply-3:pi/ps"})
        assert calls == ["http://127.0.0.1:11438/api/ps", "http://127.0.0.1:11439/api/ps",
                         "http://127.0.0.1:11438/api/ps"]

    asyncio.run(scenario())


def test_failures_are_unknown_and_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def handler(_origin: str) -> dict[str, object]:
            raise OSError("synthetic connection refusal")

        calls = install_client(monkeypatch, handler)
        assert await asyncio.gather(*(model_router.probe_residency() for _ in range(200))) == [None] * 200
        assert len(calls) == 1
        assert await model_router.probe_residency() is None
        assert len(calls) == 2

    asyncio.run(scenario())


def test_disabled_models_ignore_cached_probe_and_do_not_start_another(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def handler(_origin: str) -> dict[str, object]:
            return {"models": [{"name": "clinical:27b"}]}

        calls = install_client(monkeypatch, handler)
        assert await model_router.probe_residency() == frozenset({"clinical:27b"})
        monkeypatch.setattr(config, "MODELS_ENABLED", False)
        assert await model_router.probe_residency() is None
        assert len(calls) == 1

    asyncio.run(scenario())


def test_new_event_loop_gets_its_own_probe_and_completed_loops_are_collectible(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(_origin: str) -> dict[str, object]:
        return {"models": []}

    calls = install_client(monkeypatch, handler)
    loops: list[weakref.ReferenceType[asyncio.AbstractEventLoop]] = []

    async def scenario() -> None:
        loops.append(weakref.ref(asyncio.get_running_loop()))
        assert await model_router.probe_residency() == frozenset()

    asyncio.run(scenario())
    asyncio.run(scenario())
    assert len(calls) == 2
    gc.collect()
    assert all(loop() is None for loop in loops)
