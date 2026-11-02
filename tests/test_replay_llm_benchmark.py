"""Exact synthetic-input replay contract; no model, sockets or live records."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import replay_llm_benchmark as replay_module

REAL_ASYNC_CLIENT = httpx.AsyncClient


def captures() -> list[dict[str, Any]]:
    rows = []
    for index, (temperature, budget) in enumerate(((.4, 512), (.4, 2048), (.2, 1024))):
        body = {"model": "medgemma:27b", "stream": True, "keep_alive": "-1",
                "messages": [{"role": "system", "content": "Invented clinical records only; retain uncertainty."},
                             {"role": "user", "content": f"Synthetic scenario {index}: Na⁺ and Δ are recorded."}],
                "options": {"temperature": temperature, "num_predict": budget, "num_ctx": 8192,
                            "repeat_penalty": 1.15, "repeat_last_n": 256}}
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        rows.append({"request_id": f"{index + 1:032x}", "endpoint": "/api/chat", "body": body,
                     "body_sha256": hashlib.sha256(encoded).hexdigest(), "backend": "ollama"})
    return rows


def content() -> dict[str, Any]:
    return {"model": "medgemma:27b", "message": {"role": "assistant", "content": "A complete synthetic answer preserves documented gaps."},
            "done": False}


def terminal(**overrides: Any) -> dict[str, Any]:
    return {"model": "medgemma:27b", "message": {"role": "assistant", "content": ""},
            "done": True, "done_reason": "stop", "prompt_eval_count": 123, "eval_count": 20, **overrides}


def lines(*rows: Any) -> bytes:
    return b"".join((row.encode() if isinstance(row, str) else json.dumps(row).encode()) + b"\n" for row in rows)


def install(monkeypatch: pytest.MonkeyPatch, handler: Any,
            rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    recorded = captures() if rows is None else rows
    monkeypatch.setattr(replay_module, "captured_chats", lambda _path: (recorded, {"synthetic_data_only": True, "fixture": "test"}))

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(replay_module.httpx, "AsyncClient", factory)
    return recorded


def test_replay_sends_three_exact_canonical_bodies_and_retains_original_sampling(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def exercise() -> None:
        seen, observed = [], []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=lines(content(), terminal()))

        recorded = install(monkeypatch, handler)
        original = json.loads(json.dumps(recorded))
        result = await replay_module.replay("http://127.0.0.1:18080", tmp_path / "synthetic.jsonl", observed.append)
        assert len(seen) == len(observed) == len(result["requests"]) == 3
        for request, source, row in zip(seen, original, result["requests"], strict=True):
            expected = json.dumps(source["body"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            assert request.method == "POST" and request.url.path == "/api/chat"
            assert request.content == expected
            assert hashlib.sha256(request.content).hexdigest() == source["body_sha256"]
            assert row["source_body_sha256"] == source["body_sha256"]
            assert row["original_sampling_options"] == source["body"]["options"]
            assert row["success"] is True and row["terminal"]["eval_count"] == 20
            assert 0 <= row["first_raw_content_s"] <= row["full_s"]
        assert recorded == original  # No greedy/temp-zero shortcuts or budget edits.
        assert result["capture_provenance"]["synthetic_data_only"] is True
        assert result["scope"] == "engine_only_exact_captured_bodies_not_Orb_latency_or_clinical_validation"
        assert "strict_200_latency_targets_demonstrated" not in result
        assert "all_ai_requests_meet_completed_answer_targets" not in result
    asyncio.run(exercise())


def test_greedy_fidelity_inputs_are_skipped_without_modifying_original_sampling(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def exercise() -> None:
        recorded = captures()
        greedy = json.loads(json.dumps(recorded[0]))
        greedy["body"]["options"]["temperature"] = 0
        greedy["body_sha256"] = hashlib.sha256(replay_module.canonical(greedy["body"])).hexdigest()
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, content=lines(content(), terminal()))

        install(monkeypatch, handler, [greedy, *recorded])
        report = await replay_module.replay("http://127.0.0.1:18080", tmp_path / "synthetic.jsonl", lambda _: None)
        assert len(report["requests"]) == 3
        assert [body["options"]["temperature"] for body in seen] == [.4, .4, .2]
        assert [body["options"]["num_predict"] for body in seen] == [512, 2048, 1024]
        assert greedy["body"]["options"]["temperature"] == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("case", ["engine_error", "empty_engine_error", "length", "missing_terminal", "duplicate_terminal",
    "late_content", "missing_model", "wrong_model", "missing_eval_count", "zero_eval_count", "bool_eval_count",
    "missing_prompt_count", "negative_prompt_count", "invalid_json", "message_list", "bad_content", "empty_answer", "http_error"])
def test_failed_or_incomplete_replay_rows_never_pass_and_remaining_rows_are_attempted(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    async def exercise() -> None:
        bad_terminal = terminal()
        data = lines(content(), bad_terminal)
        if case in {"engine_error", "empty_engine_error"}:
            bad_terminal["error"] = "Synthetic engine failure" if case == "engine_error" else ""
            data = lines(content(), bad_terminal)
        elif case == "length":
            data = lines(content(), terminal(done_reason="length"))
        elif case == "missing_terminal":
            data = lines(content())
        elif case == "duplicate_terminal":
            data = lines(content(), terminal(), terminal())
        elif case == "late_content":
            data = lines(content(), terminal(), content())
        elif case in {"missing_model", "missing_eval_count", "missing_prompt_count"}:
            bad_terminal.pop({"missing_model": "model", "missing_eval_count": "eval_count",
                              "missing_prompt_count": "prompt_eval_count"}[case])
            data = lines(content(), bad_terminal)
        elif case == "wrong_model":
            data = lines(content(), terminal(model="medgemma:4b"))
        elif case == "zero_eval_count":
            data = lines(content(), terminal(eval_count=0))
        elif case == "bool_eval_count":
            data = lines(content(), terminal(eval_count=True))
        elif case == "negative_prompt_count":
            data = lines(content(), terminal(prompt_eval_count=-1))
        elif case == "invalid_json":
            data = lines(content(), "{invalid")
        elif case == "message_list":
            data = lines({"message": []}, terminal())
        elif case == "bad_content":
            data = lines({"message": {"content": ["wrong type"]}}, terminal())
        elif case == "empty_answer":
            data = lines(terminal())
        attempted, observed = [], []

        def handler(request: httpx.Request) -> httpx.Response:
            attempted.append(request)
            return httpx.Response(503 if case == "http_error" else 200, content=data)

        install(monkeypatch, handler)
        result = await replay_module.replay("http://127.0.0.1:18080", tmp_path / "synthetic.jsonl", observed.append)
        assert len(attempted) == len(observed) == len(result["requests"]) == 3
        assert all(row["success"] is False for row in result["requests"])
        assert all(row["full_s"] >= 0 for row in observed)
    asyncio.run(exercise())


def test_cancellation_retains_completed_row_and_closes_incomplete_http_stream(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def exercise() -> None:
        second_started, stream_closed = asyncio.Event(), asyncio.Event()
        never = asyncio.Event()
        attempted, observed = [], []

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                second_started.set()
                yield lines(content())
                await never.wait()

            async def aclose(self) -> None:
                stream_closed.set()

        def handler(request: httpx.Request) -> httpx.Response:
            attempted.append(request)
            if len(attempted) == 1:
                return httpx.Response(200, content=lines(content(), terminal()))
            return httpx.Response(200, stream=BlockingStream())

        install(monkeypatch, handler)
        task = asyncio.create_task(replay_module.replay("http://127.0.0.1:18080",
            tmp_path / "synthetic.jsonl", observed.append))
        await second_started.wait()
        assert len(observed) == 1 and observed[0]["success"] is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream_closed.is_set()
        assert len(attempted) == 2 and observed[0]["success"] is True
        assert all(row["success"] is False for row in observed[1:])
    asyncio.run(exercise())


def test_insufficient_original_inputs_refuses_before_any_upstream_request(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def exercise() -> None:
        seen = []
        install(monkeypatch, lambda request: seen.append(request), captures()[:2])
        with pytest.raises(ValueError, match="three captured"):
            await replay_module.replay("http://127.0.0.1:18080", tmp_path / "synthetic.jsonl", lambda _: None)
        assert not seen
    asyncio.run(exercise())
