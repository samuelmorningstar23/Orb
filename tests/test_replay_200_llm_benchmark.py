"""200-engine replay protocol/accounting tests; fake transport, no inference."""
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
import replay_200_llm_benchmark as replay


def sources(count: int = 200) -> list[dict[str, Any]]:
    result = []
    for index in range(count):
        lane, budget, temperature = (("general", 512, .4), ("patient", 2048, .4), ("department", 1024, .2))[index % 3]
        body = {"model": "medgemma:27b", "stream": True, "keep_alive": "1h",
                "messages": [{"role": "system", "content": "Clinical rules."},
                             {"role": "system", "content": "Invented chart context only."},
                             {"role": "user", "content": f"Synthetic input {index}"}],
                "options": {"num_ctx": 8192, "num_predict": budget, "temperature": temperature,
                            "repeat_penalty": 1.15, "repeat_last_n": 256}}
        result.append({"request_id": f"{index + 1:032x}", "lane": lane, "body": body,
                       "body_sha256": hashlib.sha256(replay.canonical(body)).hexdigest()})
    return result


def text() -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": "A synthetic complete answer retains documented missing information."}, "done": False}


def terminal(**overrides: Any) -> dict[str, Any]:
    return {"model": "medgemma:27b", "message": {"role": "assistant", "content": ""},
            "done": True, "done_reason": "stop", "prompt_eval_count": 200, "eval_count": 20, **overrides}


def ndjson(*rows: Any) -> bytes:
    return b"".join((row.encode() if isinstance(row, str) else json.dumps(row).encode()) + b"\n" for row in rows)


def install(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    captured = sources() if rows is None else rows
    monkeypatch.setattr(replay, "captured_chats", lambda _: (captured, {"synthetic_data_only": True, "capture_fixture": True}))
    return captured


def test_200_simultaneous_original_bodies_sampling_and_all_target_denominators(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def exercise() -> None:
        selected = install(monkeypatch)
        original = json.loads(json.dumps(selected))
        all_started = asyncio.Event()
        seen, observed = [], []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if len(seen) == 200:
                all_started.set()
            await all_started.wait()
            return httpx.Response(200, content=ndjson(text(), terminal()))

        def observe(row: dict[str, Any]) -> None:
            observed.append(row)
            row["success"] = False  # A diagnostic copy cannot alter the acceptance row.

        report = await replay.replay_200("http://127.0.0.1:18080", tmp_path / "capture.jsonl", observe,
                                         transport=httpx.MockTransport(handler))
        assert len(seen) == len(observed) == len(report["requests"]) == 200
        for request, source in zip(seen, original, strict=True):
            assert request.content == replay.canonical(source["body"])
            assert hashlib.sha256(request.content).hexdigest() == source["body_sha256"]
        assert selected == original
        summary = report["summary"]
        assert summary["successful"] == 200 and summary["failed"] == 0
        assert [summary["lanes"][lane]["requests"] for lane in ("general", "patient", "department")] == [67, 67, 66]
        assert report["concurrency"] == 200 and report["hard_request_timeout_s"] == 20
        assert report["engine_only_200_targets_demonstrated"] is True
        assert report["combined_Orb_200_targets_demonstrated"] is False
        assert report["clinical_equivalence_qualified"] is False and report["production_purchase_gate_passed"] is False
    asyncio.run(exercise())


def test_fast_failures_and_truncation_remain_in_the200_denominator(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def exercise() -> None:
        install(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            index = int(json.loads(request.content)["messages"][-1]["content"].split()[-1])
            if index == 1:
                return httpx.Response(503)
            return httpx.Response(200, content=ndjson(text(), terminal(done_reason="length" if index == 0 else "stop")))

        report = await replay.replay_200("http://127.0.0.1:18080", tmp_path / "capture.jsonl",
                                         transport=httpx.MockTransport(handler))
        assert report["summary"]["requests"] == 200 and report["summary"]["failed"] == 2
        assert report["summary"]["failures"] == {"truncated": 1, "http_503": 1}
        assert report["summary"]["lanes"]["general"]["requests"] == 67
        assert report["summary"]["lanes"]["general"]["full_meeting_target"] == 66
        assert report["engine_only_200_targets_demonstrated"] is False
    asyncio.run(exercise())


@pytest.mark.parametrize("issue", ["short", "duplicate", "scope", "context", "budget", "repeat"])
def test_original_source_cohort_cannot_be_short_duplicated_relabelled_or_weakened(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, issue: str) -> None:
    rows = sources(199 if issue == "short" else 200)
    if issue == "duplicate":
        rows[-1]["body_sha256"] = rows[0]["body_sha256"]
    elif issue == "scope":
        rows[0]["lane"] = "department"
    elif issue == "context":
        rows[0]["body"]["options"]["num_ctx"] = 4096
    elif issue == "budget":
        rows[0]["body"]["options"]["num_predict"] = 128
    elif issue == "repeat":
        rows[0]["body"]["options"]["repeat_penalty"] = 1
    install(monkeypatch, rows)
    with pytest.raises(ValueError):
        replay.original_inputs(tmp_path / "capture.jsonl")


@pytest.mark.parametrize("issue", ["missing_terminal", "duplicate_terminal", "late_content", "message_none",
    "wrong_role", "missing_model", "wrong_model", "missing_tokens", "bool_tokens", "empty_error", "answer_cache",
    "context_overflow", "output_overflow", "missing_stop", "invalid_json"])
def test_invalid_complete_looking_streams_never_become_successful_answers(issue: str) -> None:
    async def exercise() -> None:
        end = terminal()
        data = ndjson(text(), end)
        if issue == "missing_terminal":
            data = ndjson(text())
        elif issue == "duplicate_terminal":
            data = ndjson(text(), end, end)
        elif issue == "late_content":
            data = ndjson(text(), end, text())
        elif issue == "message_none":
            data = ndjson({"message": None}, end)
        elif issue == "wrong_role":
            data = ndjson({"message": {"role": "tool", "content": "Hidden tool result"}}, end)
        elif issue == "missing_model":
            end.pop("model")
            data = ndjson(text(), end)
        elif issue == "wrong_model":
            data = ndjson(text(), terminal(model="medgemma:4b"))
        elif issue == "missing_tokens":
            end.pop("prompt_eval_count")
            data = ndjson(text(), end)
        elif issue == "bool_tokens":
            data = ndjson(text(), terminal(eval_count=True))
        elif issue == "empty_error":
            data = ndjson(text(), terminal(error=""))
        elif issue == "answer_cache":
            data = ndjson(text(), terminal(cache_hit=True))
        elif issue == "context_overflow":
            data = ndjson(text(), terminal(prompt_eval_count=8000))
        elif issue == "output_overflow":
            data = ndjson(text(), terminal(eval_count=513))
        elif issue == "missing_stop":
            data = ndjson(text(), terminal(done_reason=None))
        elif issue == "invalid_json":
            data = ndjson(text(), "invalid JSON")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data))) as client:
            row = await replay.execute_one(client, "http://127.0.0.1:18080", sources(1)[0])
        assert row["success"] is False and row["completed_within_target"] is False and row["error"]
    asyncio.run(exercise())


def test_whole_http_deadline_includes_stream_close_after_early_terminal() -> None:
    async def exercise() -> None:
        closed = asyncio.Event()

        class NoClose(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield ndjson(text(), terminal())
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                closed.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=NoClose()))) as client:
            row = await replay.execute_one(client, "http://127.0.0.1:18080", sources(1)[0], timeout=.015)
        assert row["error"] == "timeout" and row["success"] is False and row["full_s"] >= .015
        assert row["terminal"]["done_reason"] == "stop" and closed.is_set()
    asyncio.run(exercise())


def test_external_guard_cancellation_retains_only_returned_rows_and_drains_owned_tasks(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def exercise() -> None:
        install(monkeypatch)
        first_observed, other_started = asyncio.Event(), asyncio.Event()
        observed, closed = [], []

        class Waits(httpx.AsyncByteStream):
            async def __aiter__(self):
                other_started.set()
                await asyncio.Event().wait()
                yield b"unreachable"

            async def aclose(self) -> None:
                closed.append(True)

        def handler(request: httpx.Request) -> httpx.Response:
            if json.loads(request.content)["messages"][-1]["content"] == "Synthetic input 0":
                return httpx.Response(200, content=ndjson(text(), terminal()))
            return httpx.Response(200, stream=Waits())

        def observe(row: dict[str, Any]) -> None:
            observed.append(row)
            first_observed.set()

        task = asyncio.create_task(replay.replay_200("http://127.0.0.1:18080", tmp_path / "capture.jsonl", observe,
                                                    transport=httpx.MockTransport(handler)))
        await first_observed.wait()
        await other_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(observed) == 1 and observed[0]["success"] is True
        assert closed  # Incomplete requests were closed, not fabricated as finished results.
    asyncio.run(exercise())
