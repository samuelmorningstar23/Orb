"""App shutdown must unblock a knowledge RPC before joining its reader thread."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Any

from fastapi.testclient import TestClient
from main import app
from services.sage_retrieval_executor import get_sage_retrieval_executor

import config
from services import knowledge_worker


def test_lifespan_stops_knowledge_worker_before_joining_waiting_retrieval(
    monkeypatch: Any,
) -> None:
    # conftest owns an isolated orb_test_ database. No real models, child
    # process, live records or model HTTP requests are needed for this race.
    monkeypatch.setattr(config, "MODELS_ENABLED", False)
    monkeypatch.setattr(config, "BACKGROUND_MODELS_ENABLED", False)
    entered, rpc_released = threading.Event(), threading.Event()
    observations: list[tuple[str, bool]] = []
    result: Future[bool] | None = None

    def waiting_rpc() -> bool:
        entered.set()
        # Bound the broken-order case: the old implementation returns False
        # after this wait, then shuts the worker down too late. The assertion
        # is about the release event and call order, not measured wall time.
        released = rpc_released.wait(2)
        observations.append(("retrieval_returned", released))
        return released

    def stop_synthetic_worker() -> None:
        observations.append(("knowledge_shutdown", True))
        rpc_released.set()

    monkeypatch.setattr(knowledge_worker, "shutdown", stop_synthetic_worker)
    try:
        with TestClient(app) as client:
            # Ignore any cleanup performed by unrelated startup validation.
            observations.clear()
            rpc_released.clear()
            executor = get_sage_retrieval_executor(app)
            assert client.portal is not None
            result = client.portal.start_task_soon(executor.run, waiting_rpc)
            assert entered.wait(2), "synthetic retrieval thread did not start"
            assert executor.metrics()["outstanding"] == 1
        assert result is not None
        assert result.result(timeout=2) is True
        assert observations == [
            ("knowledge_shutdown", True),
            ("retrieval_returned", True),
        ]
        assert executor.closed
        assert executor.metrics()["outstanding"] == 0
    finally:
        rpc_released.set()
