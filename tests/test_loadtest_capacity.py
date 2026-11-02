"""Runner/protocol/safety checks; no Orb imports, databases or real models."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import loadtest_capacity as capacity
from loadtest_capacity import (
    ProtocolError,
    SSEDecoder,
    StreamObservation,
    execute_one,
    make_plan,
    parser,
    run_burst,
    summarise,
    validate_safety,
    validate_url,
)
from loadtest_capacity_fixture import create_app, make_manifest


def test_sse_multibyte_crlf_split_every_byte_and_multiline_event():
    raw = ': heartbeat\r\nevent: message\r\ndata: {"content":\r\ndata: "A readable café answer has enough words."}\r\n\r\ndata: {"done":true}\r\n\r\n'.encode()
    decoder, observation = SSEDecoder(), StreamObservation()
    for value in raw:
        for event, data in decoder.feed(bytes([value])):
            observation.accept(event, data, .1)
    assert decoder.feed(b"", final=True) == []
    assert observation.first_readable_s == .1
    assert observation.terminal and observation.error is None
    assert observation.content_chars == len("A readable café answer has enough words.")
    assert "_digest" not in observation.report()


@pytest.mark.parametrize("raw", [b'data: {"done":true}', b'data: {"done":true}\n', b'data: \xff\n\n'])
def test_truncated_or_invalid_utf8_is_not_success(raw):
    decoder = SSEDecoder()
    with pytest.raises(ProtocolError):
        decoder.feed(raw)
        decoder.feed(b"", final=True)


def test_headers_and_status_do_not_start_readable_clock():
    observation = StreamObservation()
    observation.accept("message", json.dumps({"stage": "generating"}), .01)
    observation.accept("message", json.dumps({"content": "**General clinical information**\n\n"}), .02)
    assert observation.first_content_s == .02 and observation.first_readable_s is None
    observation.accept("message", json.dumps({"content": "A substantive sentence about the query arrives."}), 1)
    assert observation.first_readable_s == 1


def test_http_200_stream_error_with_done_is_failure_and_does_not_log_message():
    async def exercise():
        async def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content='data: {"content":"Synthetic text with more than four words."}\n\ndata: {"error":"SECRET-MRN patient text","code":"SAGE_STREAM_FAILED"}\n\ndata: {"done":true}\n\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await execute_one(client, "http://127.0.0.1", {"request_id": "test", "lane": "general", "body": {}},
                                       {"role": "doctor", "_token": "secret"}, {}, 5, True)
        assert not result["success"] and result["error"] == "stream_error"
        assert not result["completed_within_target"]
        assert "SECRET" not in json.dumps(result)
    asyncio.run(exercise())


@pytest.mark.parametrize("body", [
    'data: {"content":"A complete looking answer without terminal acknowledgement."}\n\n',
    'data: {"stage":"generating"}\n\ndata: {"done":true}\n\n',
    'data: {"content":"A cached response must never count as fresh inference."}\n\ndata: {"metadata":{"cached":true}}\n\ndata: {"done":true}\n\n',
    'data: {"content":"A valid answer requires selected model provenance."}\n\ndata: {"done":true}\n\n',
])
def test_empty_incomplete_cached_and_unproven_model_do_not_pass(body):
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body))) as client:
            result = await execute_one(client, "http://127.0.0.1", {"request_id": "test", "lane": "patient", "body": {}},
                                       {"role": "doctor", "_token": "secret"}, {}, 5, True)
        assert not result["success"]
    asyncio.run(exercise())


def test_error_then_done_and_event_after_terminal_rejected():
    observation = StreamObservation()
    observation.accept("error", '{"error":"private"}', .1)
    observation.accept("message", '{"done":true}', .2)
    assert observation.error == "stream_error"
    with pytest.raises(ProtocolError, match="event_after_terminal"):
        observation.accept("message", '{"content":"late"}', .3)


@pytest.mark.parametrize("url", ["https://hospital.example", "http://10.0.0.1:8000", "http://127.0.0.1@evil.example", "http://localhost:80/patient", "http://localhost:80?token=secret"])
def test_remote_or_credential_targets_refused(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_200_real_model_burst_requires_explicit_separate_override():
    args = parser().parse_args(["--base-url", "http://127.0.0.1:18000", "--manifest", "unused", "--allow-nondisposable", "--ack-synthetic-data", "--allow-model-load"])
    with pytest.raises(ValueError, match="dedicated host"):
        validate_safety(args, {"synthetic_data_only": True})
    args.concurrency, args.requests = 4, 12
    validate_safety(args, {"synthetic_data_only": True})


def test_200_unique_patient_scoped_prompts_and_threads_no_fake_dedup():
    manifest = make_manifest("token")
    rows = make_plan(200, "fresh-ai", manifest, "run")
    assert len({row["body"]["query"] for row in rows}) == 200
    assert len({row["body"]["conversation_id"] for row in rows}) == 200
    assert all(row["patient_id"] in manifest["identities"][0]["patient_ids"] for row in rows if row["lane"] == "patient")


def test_failures_are_in_target_denominator():
    result = summarise([
        {"lane": "general", "success": True, "full_s": .1, "first_readable_s": .05, "first_readable_within_target": True,
         "completed_within_target": True, "status": 200, "error": None},
        {"lane": "general", "success": False, "full_s": .01, "first_readable_within_target": False,
         "completed_within_target": False, "status": 503, "error": "http_503"},
    ], .11)
    assert result["failed"] == 1 and result["lanes"]["general"]["requests"] == 2
    assert not result["all_ai_requests_meet_completed_answer_targets"]


def test_terminal_latency_does_not_relax_http_close_acceptance():
    result = summarise([
        {"lane": "general", "success": True, "full_s": 4.0, "completion_s": 2.0,
         "first_readable_s": 1.0, "first_readable_within_target": True,
         "completed_within_target": False, "status": 200, "error": None},
    ], 4.0)
    lane = result["lanes"]["general"]
    assert lane["terminal_completed_answer"]["max_s"] == 2.0
    assert lane["post_terminal_stream_close"]["max_s"] == 2.0
    assert not result["all_ai_requests_meet_completed_answer_targets"]


def test_profiler_restricted_to_disposable_fake_model_fixture():
    args = parser().parse_args(["--synthetic", "--profile-fixture"])
    with pytest.raises(ValueError, match="disposable fake-model"):
        validate_safety(args, None)
    args = parser().parse_args(["--orb-stub", "--profile-fixture"])
    validate_safety(args, None)


def test_stage_and_executor_diagnostics_require_the_owned_orb_fixture():
    args = parser().parse_args(["--orb-stub", "--profile-stages-only"])
    with pytest.raises(ValueError, match="requires --profile-fixture"):
        validate_safety(args, None)
    args = parser().parse_args(["--synthetic", "--fixture-thread-workers", "4"])
    with pytest.raises(ValueError, match="Diagnostic thread overrides"):
        validate_safety(args, None)
    args = parser().parse_args(["--orb-stub", "--profile-fixture", "--profile-stages-only", "--fixture-thread-workers", "4"])
    validate_safety(args, None)


def test_whole_request_deadline_stops_continuous_stream_and_keeps_partial_timing():
    class ContinuesForever(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"content":"A readable partial answer has enough words."}\n\n'
            while True:
                await asyncio.sleep(.005)  # Read gaps never exceed the deadline.
                yield b': heartbeat\n\n'
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=ContinuesForever()))) as client:
            result = await execute_one(client, "http://127.0.0.1", {"request_id": "bounded", "lane": "general", "body": {}},
                                       {"role": "doctor", "_token": "secret"}, {}, .03, True)
        assert result["error"] == "timeout" and not result["success"]
        assert .02 <= result["full_s"] < .5 and result["first_readable_s"] is not None
        assert result["content_chars"] > 32 and not result["completed_within_target"]
    asyncio.run(exercise())


@pytest.mark.parametrize("summary,error", [
    ({"patient_id": "OTHER", "status": "ready", "summary": {}, "generated_at": "2026-01-01T00:00:00Z"}, "summary_patient_mismatch"),
    ({"patient_id": "SCOPED", "status": "outdated", "summary": {}, "generated_at": "2026-01-01T00:00:00Z"}, "summary_not_ready"),
    ({"patient_id": "SCOPED", "status": "ready", "summary": {}, "generated_at": "2000-01-01T00:00:00Z", "source_captured_at": "2000-01-01T00:00:00Z"}, "summary_stale_or_clock_skew"),
    ({"patient_id": "SCOPED", "status": "ready", "summary": {}, "generated_at": "2026-01-01T00:00:00Z"}, "summary_source_timestamp_missing"),
])
def test_summary_mismatch_outdated_expired_never_count_as_current(summary, error):
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=summary))) as client:
            result = await execute_one(client, "http://127.0.0.1", {"request_id": "summary", "lane": "summary", "patient_id": "SCOPED"},
                                       {"role": "doctor", "_token": "secret"}, {}, 2, False)
        assert result["error"] == error and not result["success"]
    asyncio.run(exercise())


def test_recently_generated_summary_with_old_sources_fails_freshness():
    async def exercise():
        now = datetime.now(timezone.utc)
        data = {"patient_id": "SCOPED", "status": "ready", "summary": {}, "generated_at": now.isoformat(),
                "source_captured_at": (now - timedelta(minutes=31)).isoformat()}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as client:
            result = await execute_one(client, "http://127.0.0.1", {"request_id": "source-age", "lane": "summary", "patient_id": "SCOPED"},
                                       {"role": "doctor", "_token": "secret"}, {}, 2, False)
        assert result["error"] == "summary_stale_or_clock_skew" and not result["success"]
        assert result["summary_source_age_s"] >= 1860 and result["summary_generation_age_s"] < 1
    asyncio.run(exercise())


def test_capacity_sse_rejection_is_explicit_failure_without_done():
    observation = StreamObservation()
    observation.accept("message", '{"error":"Busy. Retry later.","code":"SAGE_CAPACITY_REJECTED"}', .1)
    assert observation.error == "capacity_rejected" and not observation.terminal


def test_actual_200_request_http_fixture_and_mixed_workload():
    # Real TCP streaming is exercised by the CLI smoke run. ASGI here proves
    # auth and patient-scope handling without launching models/network servers.
    async def exercise():
        app = create_app("nonce")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            unauthorized = await client.get("/api/vigil/patients")
            forbidden = await client.get("/api/patients/UNSCOPED/summary", headers={"Authorization": "Bearer nonce"})
            assert unauthorized.status_code == 401 and forbidden.status_code == 404
            good = await client.get("/api/patients/SYNTH-001/summary", headers={"Authorization": "Bearer nonce"})
            assert good.json()["status"] == "ready"
    asyncio.run(exercise())


def returned_result(item):
    return {"request_id": item["request_id"], "lane": item["lane"], "success": True,
            "full_s": .01, "first_readable_s": .005, "first_readable_within_target": True,
            "completed_within_target": True, "status": 200, "error": None}


def test_result_observer_retains_completion_order_without_mutating_returned_rows(monkeypatch):
    async def exercise():
        observed = []

        async def execute(_client, _base, item, *_args):
            # All return order is deterministic without relying on a timer.
            if item["lane"] == "general":
                await second_returned.wait()
            else:
                second_returned.set()
            return returned_result(item)

        second_returned = asyncio.Event()
        monkeypatch.setattr(capacity, "execute_one", execute)

        def observe(row):
            observed.append(row["lane"])
            row["success"] = False  # Observers cannot alter acceptance rows.

        summary, rows = await run_burst("http://127.0.0.1:1", make_manifest("nonce"),
            mode="fresh-ai", concurrency=2, requests=2, timeout=1,
            require_model=True, run_id="observer", result_observer=observe)
        assert observed == ["patient", "general"]
        assert [row["lane"] for row in rows] == ["general", "patient"]
        assert summary["successful"] == 2 and summary["failed"] == 0
    asyncio.run(exercise())


def test_cancelled_cohort_keeps_returned_rows_and_drains_pending_requests(monkeypatch):
    async def exercise():
        observed, cancelled = [], []
        first_observed, never = asyncio.Event(), asyncio.Event()

        async def execute(_client, _base, item, *_args):
            if item["lane"] == "general":
                return returned_result(item)
            try:
                await never.wait()
            finally:
                cancelled.append(item["request_id"])

        def observe(row):
            observed.append(row)
            first_observed.set()

        monkeypatch.setattr(capacity, "execute_one", execute)
        cohort = asyncio.create_task(run_burst("http://127.0.0.1:1", make_manifest("nonce"),
            mode="fresh-ai", concurrency=2, requests=2, timeout=1,
            require_model=True, run_id="cancel", result_observer=observe))
        await first_observed.wait()
        cohort.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cohort
        assert len(observed) == 1 and observed[0]["lane"] == "general"
        assert cancelled == ["capacity-cancel-00001"]
    asyncio.run(exercise())


def test_observer_failure_cancels_and_drains_sibling_requests(monkeypatch):
    async def exercise():
        pending_started, never = asyncio.Event(), asyncio.Event()
        cancelled = []

        async def execute(_client, _base, item, *_args):
            if item["lane"] == "general":
                await pending_started.wait()
                return returned_result(item)
            pending_started.set()
            try:
                await never.wait()
            finally:
                cancelled.append(item["request_id"])

        def observe(_row):
            raise RuntimeError("Diagnostic observer failed")

        monkeypatch.setattr(capacity, "execute_one", execute)
        with pytest.raises(RuntimeError, match="Diagnostic observer"):
            await run_burst("http://127.0.0.1:1", make_manifest("nonce"),
                mode="fresh-ai", concurrency=2, requests=2, timeout=1,
                require_model=True, run_id="failure", result_observer=observe)
        assert cancelled == ["capacity-failure-00001"]
    asyncio.run(exercise())


@pytest.mark.parametrize("wrapped", [False, True])
def test_nonreturning_async_observer_is_rejected_before_any_request(monkeypatch, wrapped):
    async def exercise():
        invoked = []

        async def observe(_row):
            await asyncio.Event().wait()

        class AsyncObserver:
            async def __call__(self, row):
                await observe(row)

        async def execute(*_args):
            invoked.append(True)
            raise AssertionError("No request should start with an invalid observer")

        monkeypatch.setattr(capacity, "execute_one", execute)
        with pytest.raises(TypeError, match="synchronous"):
            await run_burst("http://127.0.0.1:1", make_manifest("nonce"),
                mode="fresh-ai", concurrency=1, requests=1, timeout=1,
                require_model=True, run_id="async", result_observer=AsyncObserver() if wrapped else observe)
        assert not invoked
    asyncio.run(exercise())


def test_sync_wrapper_cannot_sneak_in_a_nonreturning_coroutine(monkeypatch):
    async def exercise():
        body_started = []

        async def never_returns(_row):
            body_started.append(True)
            await asyncio.Event().wait()

        async def execute(_client, _base, item, *_args):
            return returned_result(item)

        monkeypatch.setattr(capacity, "execute_one", execute)
        with pytest.raises(TypeError, match="return None"):
            await run_burst("http://127.0.0.1:1", make_manifest("nonce"),
                mode="fresh-ai", concurrency=1, requests=1, timeout=1,
                require_model=True, run_id="wrapped", result_observer=lambda row: never_returns(row))
        assert not body_started
    asyncio.run(exercise())
