"""Pure validation and mocked HTTP tests; no socket, GPU or model is used."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/llm_fidelity_cases.py"
SPEC = importlib.util.spec_from_file_location("orb_llm_fidelity_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
fidelity: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fidelity
SPEC.loader.exec_module(fidelity)
ORIGIN = "http://127.0.0.1:18491"
REAL_CLIENT = httpx.AsyncClient


def _event(content: str = "", *, done: Any = False, **changes: Any) -> dict[str, Any]:
    value = {"model": fidelity.MODEL, "message": {"role": "assistant", "content": content}, "done": done}
    if done:
        value.update(done_reason="stop", eval_count=64, prompt_eval_count=3000)
    value.update(changes)
    return value


def _stream(*events: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler: Any, seen_options: list[dict[str, Any]] | None = None) -> None:
    def factory(**kwargs: Any) -> httpx.AsyncClient:
        if seen_options is not None:
            seen_options.append(kwargs.copy())
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(fidelity.httpx, "AsyncClient", factory)


def test_first_object_handles_markdown_and_malformed_prefix_without_cherry_picking() -> None:
    assert fidelity.first_json_object('Preface {not-json}.\n```json\n{"x": 1}\n```') == {"x": 1}
    first = fidelity.first_json_object('{"x": false}\n{"x": 1}')
    assert first == {"x": False}
    assert not fidelity.expected_checks(first, {"x": 1})[0]["passed"]


@pytest.mark.parametrize("text", ['{"x": 1, "x": 2}', '{"x": NaN}', '{"x": Infinity}',
                                   '{"x": -Infinity}', '{"x": 1e309}', "not an object", "[1, 2]"])
def test_duplicate_nonfinite_or_missing_objects_are_rejected(text: str) -> None:
    with pytest.raises(fidelity.FidelityFailure):
        fidelity.first_json_object(text)


@pytest.mark.parametrize(("actual", "expected", "passed"), [
    (104, 104, True), (104.0, 104, True), (104.01, 104, False), (True, 1, False),
    (False, 0.0, False), (0, False, False), (False, False, True), ("false", False, False),
    (None, None, True), ("null", None, False), (0, None, False), ("mg", "mcg", False),
    ({"value": 38.2, "unit": "degrees C", "extra": "visible"}, {"value": 38.2, "unit": "degrees C"}, True),
    ({"value": 38.2, "unit": "F"}, {"value": 38.2, "unit": "degrees C"}, False),
    ({}, {"missing": None}, False), (["a"], ["a"], True), (["a", "b"], ["a"], False),
    ([], ["a"], False), ([True], [1], False),
])
def test_expected_subset_preserves_numbers_types_units_missingness_and_list_membership(
        actual: Any, expected: Any, passed: bool) -> None:
    checks = fidelity.expected_checks(actual, expected)
    assert all(row["passed"] for row in checks) is passed


def test_cases_and_request_bodies_are_synthetic_fixed_replayable_and_keep_limits() -> None:
    assert len(fidelity.CASES) == 8
    assert len({case.case_id for case in fidelity.CASES}) == 8
    long = next(case for case in fidelity.CASES if case.case_id.startswith("long_context"))
    assert 1500 < len(long.source.split()) < 4000
    assert long.source.index("penicillin") < long.source.index("Neutral indexing")
    assert long.source.rindex("potassium") > long.source.rindex("Neutral indexing")
    assert "VOIDED" in long.source and long.expected["potassium"] == 4.6
    for case in fidelity.CASES:
        assert "synthetic" in case.source.lower()
        body = fidelity.request_body(case)
        assert body["model"] == "medgemma:27b" and body["stream"] is True
        assert body["options"] == {"temperature": 0.0, "repeat_penalty": 1.15, "repeat_last_n": 256,
            "top_k": 40, "top_p": 0.9, "min_p": 0.0, "num_ctx": 8192, "num_predict": 512,
            "stop": ["<end_of_turn>"]}
        body["options"]["stop"].append("bad override")
        assert fidelity.request_body(case)["options"]["stop"] == ["<end_of_turn>"]


@pytest.mark.parametrize("origin", ["https://127.0.0.1:18000", "http://example.com:18000",
    "http://localhost:18000", "http://127.0.0.1:11434", "http://127.0.0.1:80",
    "http://127.0.0.1:18000/api", "http://user:password@127.0.0.1:18000",
    "http://127.0.0.1:18000?x=1", "http://127.0.0.1:18000#x", "http://127.0.0.1"])
def test_only_owned_explicit_loopback_origins_are_allowed(origin: str) -> None:
    with pytest.raises((fidelity.FidelityFailure, ValueError)):
        fidelity.validate_origin(origin)
    assert fidelity.validate_origin(ORIGIN + "/") == ORIGIN
    assert fidelity.validate_origin("http://[::1]:18001") == "http://[::1]:18001"


@pytest.mark.asyncio
async def test_all_cases_run_sequentially_report_checks_and_cannot_mutate_observed_evidence(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    options: list[dict[str, Any]] = []
    active = peak = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        case = fidelity.CASES[len(calls)]
        body = json.loads(request.content)
        calls.append(body)
        assert str(request.url) == ORIGIN + "/api/chat"
        assert body == fidelity.request_body(case)
        await asyncio.sleep(0)
        active -= 1
        text = json.dumps(case.expected)
        return httpx.Response(200, content=_stream(_event(text[:10]), _event(text[10:]), _event(done=True)))

    _mock_client(monkeypatch, handle, options)
    observed = []

    def observe(case: dict[str, Any]) -> None:
        observed.append(copy.deepcopy(case))
        case["passed"] = False
        case["request_body"]["options"]["num_predict"] = 1

    report = await fidelity.run_cases(ORIGIN, observe)
    assert report["total"] == report["successful"] == report["completed_cases"] == 8
    assert report["all_cases_passed"] is True and peak == 1
    assert report["evidence_kind"] == "synthetic_fidelity_diagnostic_not_clinical_validation"
    assert report["clinical_accuracy_qualified"] is report["same_clinical_quality_demonstrated"] is False
    assert options[0]["trust_env"] is options[0]["follow_redirects"] is False
    assert options[0]["timeout"].read == 60.0
    assert len(observed) == 8
    for result, body in zip(report["cases"], calls):
        assert result["passed"] is result["completed"] is result["terminal_stop_seen"] is True
        assert result["body_sha256"] == hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        assert result["request_body"] == body
        assert result["error"] is None and result["elapsed_s"] >= 0


@pytest.mark.parametrize("events", [
    [_event('{"x": 1}'), _event(done=True, done_reason="length")],
    [_event('{"x": 1}')],
    [_event('{"x": 1}'), _event(done=True, done_reason=None)],
    [_event('{"x": 1}'), _event(done=True, eval_count=True)],
    [_event('{"x": 1}'), _event(done=True, eval_count=0)],
    [_event('{"x": 1}'), _event(done=True, eval_count=513)],
    [_event('{"x": 1}'), _event(done=True, model="different-model")],
    [_event('{"x": 1}'), _event(done=True), _event("more")],
    [{"error": "candidate failed"}],
    [{"model": fidelity.MODEL, "done": True, "done_reason": "stop", "eval_count": 64}],
    [_event("", done=True)],
    [_event('{"x": 1}', done=1)],
])
@pytest.mark.asyncio
async def test_errors_length_incomplete_or_wrong_model_streams_cannot_pass(
        monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]) -> None:
    case = fidelity.FidelityCase("synthetic", "Synthetic observation x=1", "Extract x", {"x": 1})
    _mock_client(monkeypatch, lambda _request: httpx.Response(200, content=_stream(*events)))
    async with fidelity.httpx.AsyncClient(trust_env=False) as client:
        result = await fidelity._run_case(client, ORIGIN, case)
    assert result["passed"] is result["completed"] is False
    assert result["error"] is not None


@pytest.mark.asyncio
async def test_wrong_facts_fail_even_with_complete_terminal_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    case = fidelity.FidelityCase("identity", "Synthetic patient SYNTH-ALPHA", "Extract patient ID", {"patient_id": "SYNTH-ALPHA"})
    _mock_client(monkeypatch, lambda _request: httpx.Response(200, content=_stream(
        _event('{"patient_id": "SYNTH-BETA"}'), _event(done=True))))
    async with fidelity.httpx.AsyncClient(trust_env=False) as client:
        result = await fidelity._run_case(client, ORIGIN, case)
    assert result["completed"] is result["terminal_stop_seen"] is True
    assert result["passed"] is False and result["fact_checks"][0]["passed"] is False
    assert result["error"] is None


@pytest.mark.parametrize("prompt_count", [None, True, 1024, 6000])
@pytest.mark.asyncio
async def test_long_context_case_requires_measured_nontrivial_prompt_length(
        monkeypatch: pytest.MonkeyPatch, prompt_count: Any) -> None:
    case = next(case for case in fidelity.CASES if case.case_id.startswith("long_context"))
    _mock_client(monkeypatch, lambda _request: httpx.Response(200, content=_stream(
        _event(json.dumps(case.expected)), _event(done=True, prompt_eval_count=prompt_count))))
    async with fidelity.httpx.AsyncClient(trust_env=False) as client:
        result = await fidelity._run_case(client, ORIGIN, case)
    assert result["passed"] is False and result["error"] is not None


@pytest.mark.parametrize("status", [307, 429, 500])
@pytest.mark.asyncio
async def test_http_errors_and_redirects_fail_without_following_another_origin(
        monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://external.invalid/"})

    _mock_client(monkeypatch, handle)
    report = await fidelity.run_cases(ORIGIN)
    assert report["successful"] == 0 and report["all_cases_passed"] is False
    assert requests == [ORIGIN + "/api/chat"] * 8
    assert all(case["error"]["type"] == "HTTPStatusError" for case in report["cases"])


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, entered: asyncio.Event | None = None):
        self.entered, self.closed = entered, False

    async def __aiter__(self) -> Any:
        if self.entered is not None:
            self.entered.set()
        while True:
            yield b"\n"
            await asyncio.sleep(0.005)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_whole_call_deadline_stops_frequently_arriving_bytes_and_closes_stream(
        monkeypatch: pytest.MonkeyPatch) -> None:
    streams = []

    def handle(_request: httpx.Request) -> httpx.Response:
        stream = SlowStream()
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    _mock_client(monkeypatch, handle)
    monkeypatch.setattr(fidelity, "WHOLE_CALL_SECONDS", 0.015)
    report = await fidelity.run_cases(ORIGIN)
    assert report["successful"] == 0
    assert all(case["error"]["type"] == "TimeoutError" for case in report["cases"])
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_the_current_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    stream = SlowStream(entered)
    _mock_client(monkeypatch, lambda _request: httpx.Response(200, stream=stream))
    observed: list[dict[str, Any]] = []
    task = asyncio.create_task(fidelity.run_cases(ORIGIN, observed.append))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True and not observed


@pytest.mark.asyncio
async def test_async_observer_is_rejected_before_any_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    _mock_client(monkeypatch, lambda request: calls.append(request))

    async def observer(_result: dict[str, Any]) -> None:
        return None

    with pytest.raises(fidelity.FidelityFailure, match="synchronous"):
        await fidelity.run_cases(ORIGIN, observer)
    assert not calls


@pytest.mark.asyncio
async def test_sync_observer_returning_coroutine_is_rejected_and_coroutine_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    first = fidelity.CASES[0]
    _mock_client(monkeypatch, lambda _request: httpx.Response(200, content=_stream(
        _event(json.dumps(first.expected)), _event(done=True))))

    async def async_work() -> None:
        raise AssertionError("Should never run")

    coroutines: list[Any] = []

    def observer(_result: dict[str, Any]) -> Any:
        result = async_work()
        coroutines.append(result)
        return result

    with pytest.raises(fidelity.FidelityFailure, match="awaitable"):
        await fidelity.run_cases(ORIGIN, observer)
    assert coroutines[0].cr_frame is None
