"""Post-deadline cleanup cannot improve HTTP latency or manufacture task evidence."""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_mlx_ollama as benchmark
from capacity_answer_quality import _grounding_module
from llm_fidelity_cases import CASES

_GROUNDING = _grounding_module()
_BUNDLE = _GROUNDING.compile_sage_context({"patient_id": "p", "demographics": "XqzAA VyxA",
                                         "diagnosis": "Synthetic evaluation record"})
_SELECTION = tuple(fact.fact_id for fact in _BUNDLE.facts)
EXPECTED = {"grounding_bundle": _GROUNDING.bundle_to_dict(_BUNDLE)}
GOOD = _GROUNDING.render_sage(_BUNDLE, _SELECTION)
PROOF = _GROUNDING.grounding_metadata(_BUNDLE, _SELECTION)


class Client:
    def __init__(self, handler: Any) -> None:
        self.handler = handler

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, *, timeout: float) -> httpx.Response:
        return self.handler(url, timeout)


def metadata(outstanding: int = 0, pending: int = 0) -> dict:
    return {"retrieval_executor": {"workers": 4, "outstanding": outstanding},
            "knowledge_worker_output_reuse": {"pending": pending},
            "retrieval_runtime": {"embedding_calls": 3, "embedding_completed": 3,
                                  "rerank_calls": 3, "rerank_completed": 3,
                                  "embedding_failed": 0, "rerank_failed": 0,
                                  "results_lexical": 0, "results_other": 0,
                                  "results_cross_encoder": 36},
            "full_retrieval_proof": {"installed_models_verified": True}}


def virtual_cleanup(monkeypatch: Any, responses: list[dict], *, delay: float = 0) -> tuple:
    clock = [0.0]
    requests: list[dict] = []

    async def sleep(seconds: float) -> None:
        assert seconds >= 0
        clock[0] += seconds

    def get(url: str, timeout: float) -> httpx.Response:
        requests.append({"started": clock[0], "timeout": timeout, "url": url})
        request = httpx.Request("GET", url)
        if delay > timeout:
            clock[0] += timeout
            raise httpx.ReadTimeout("owned local probe exceeded budget", request=request)
        clock[0] += delay
        index = min(len(requests) - 1, len(responses) - 1)
        return httpx.Response(200, request=request, json=responses[index])

    monkeypatch.setattr(benchmark, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: clock[0]), sleep=sleep,
        timeout=asyncio.timeout))
    monkeypatch.setattr(benchmark.httpx, "AsyncClient", lambda **kwargs: Client(get))
    return clock, requests


def test_cleanup_observes_eventual_physical_completion_and_keeps_initial_counters(
    monkeypatch: Any,
) -> None:
    clock, requests = virtual_cleanup(monkeypatch, [metadata(2, 1), metadata(1, 0), metadata()])
    result = asyncio.run(benchmark.settle_retrieval("http://127.0.0.1:1111", maximum_seconds=1))
    assert result["settled"] is True
    assert result["initial"]["executor"]["outstanding"] == 2
    assert result["initial"]["model_outputs"]["pending"] == 1
    assert result["final"]["executor"]["outstanding"] == 0
    assert result["wall_s"] == pytest.approx(clock[0])
    assert len(requests) == 3
    assert result["included_in_request_latency"] is False


@pytest.mark.parametrize("remaining", [metadata(1, 0), metadata(0, 1), {}])
def test_cleanup_requires_both_executor_and_model_zero_and_never_assumes_missing_evidence(
    monkeypatch: Any, remaining: dict,
) -> None:
    clock, requests = virtual_cleanup(monkeypatch, [remaining])
    result = asyncio.run(benchmark.settle_retrieval("http://127.0.0.1:1111", maximum_seconds=.25))
    assert result["settled"] is False
    assert clock[0] <= .25
    assert len(requests) <= 4
    assert result["included_in_request_latency"] is False


def test_cleanup_http_timeout_cannot_extend_its_remaining_budget(monkeypatch: Any) -> None:
    clock, requests = virtual_cleanup(monkeypatch, [metadata(1, 1)], delay=.5)
    result = asyncio.run(benchmark.settle_retrieval("http://127.0.0.1:1111", maximum_seconds=.2))
    assert not result["settled"]
    assert result["included_in_request_latency"] is False
    assert clock[0] <= .2
    assert all(request["timeout"] <= .2 - request["started"] for request in requests)


def completed_phase(name: str, count: int) -> dict:
    rows = []
    for index in range(count):
        lane = ("general", "patient", "department")[index % 3]
        row = {"request_id": f"{name}-{index}", "lane": lane,
               "success": True, "transport_success": True, "completed_within_target": True,
               "model": "medgemma:27b", "cache_hit": False,
               "full_s": 2.0, "completion_s": 1.9,
               "workflow_quality": {"passed": True, "checks": {"source_fact_preserved": True}}}
        rows.append(row)
    patients = sum(row["lane"] == "patient" for row in rows)
    return {"name": name, "status": "complete", "requests": rows,
            "planned_requests": count, "observed_http_results": count,
            "unobserved_http_results": 0,
            "summary": {"requests": count, "failed": 0,
                        "all_ai_requests_meet_completed_answer_targets": True},
            "workflow_quality": {"required": True, "patient_requests": patients,
                                 "transport_completed": patients, "task_checks_passed": patients,
                                 "all_patient_requests_passed": True, "clinical_accuracy_qualified": False}}


def workflow_report() -> dict:
    return {"status": "complete", "profile": {
        "workflow_quality_required": True,
        "phases": [completed_phase("sequential", 3), completed_phase("burst", 200), fidelity_phase()],
        "final_fixture_metadata": metadata()}}


def fidelity_phase() -> dict:
    return {"name": "quality", "status": "complete", "total": 8, "completed_cases": 8,
            "successful": 8, "all_cases_passed": True, "cases": [
                {"case_id": case.case_id, "completed": True, "terminal_stop_seen": True, "passed": True}
                for case in CASES]}


def test_complete_summary_evidence_can_pass_engineering_gate_without_clinical_award() -> None:
    result = benchmark.acceptance(workflow_report())
    assert result["summary_task_regression_passed"] is True
    assert result["strict_200_latency_targets_demonstrated"] is True
    assert not result["clinical_equivalence_qualified"]
    assert not result["production_purchase_gate_passed"]


@pytest.mark.parametrize("change", [
    "missing_sequential", "missing_burst", "missing_workflow", "bare_success_flag",
    "not_required", "zero_patients", "missing_count", "wrong_transport_count", "wrong_passed_count",
    "missing_row_evidence", "empty_row_checks", "failed_row_check", "row_transport_failed",
    "phase_incomplete",
])
def test_workflow_gate_refuses_vacuous_missing_or_contradictory_evidence(change: str) -> None:
    report = workflow_report()
    phase = report["profile"]["phases"][1]
    patient = next(row for row in phase["requests"] if row["lane"] == "patient")
    if change == "missing_sequential":
        report["profile"]["phases"].pop(0)
    elif change == "missing_burst":
        report["profile"]["phases"].pop(1)
    elif change == "missing_workflow":
        del phase["workflow_quality"]
    elif change == "bare_success_flag":
        phase["workflow_quality"] = {"all_patient_requests_passed": True}
    elif change == "not_required":
        phase["workflow_quality"]["required"] = False
    elif change == "zero_patients":
        phase["workflow_quality"].update(patient_requests=0, transport_completed=0, task_checks_passed=0)
    elif change == "missing_count":
        del phase["workflow_quality"]["task_checks_passed"]
    elif change == "wrong_transport_count":
        phase["workflow_quality"]["transport_completed"] -= 1
    elif change == "wrong_passed_count":
        phase["workflow_quality"]["task_checks_passed"] -= 1
    elif change == "missing_row_evidence":
        del patient["workflow_quality"]
    elif change == "empty_row_checks":
        patient["workflow_quality"]["checks"] = {}
    elif change == "failed_row_check":
        patient["workflow_quality"]["checks"]["source_fact_preserved"] = False
    elif change == "row_transport_failed":
        patient["transport_success"] = False
    elif change == "phase_incomplete":
        phase["status"] = "aborted_incomplete_not_acceptance_evidence"
    result = benchmark.acceptance(report)
    assert result["summary_task_regression_passed"] is False
    assert result["strict_200_latency_targets_demonstrated"] is False


def test_later_report_abort_never_qualifies_latency_despite_completed_task_evidence() -> None:
    report = workflow_report()
    report["status"] = "aborted"
    result = benchmark.acceptance(report)
    assert result["strict_200_latency_targets_demonstrated"] is False
    assert result["summary_task_regression_passed"] is True


def test_post_request_cleanup_stays_outside_summary_and_each_request_latency(monkeypatch: Any) -> None:
    manifest = {"identities": [{"role": "doctor", "patient_ids": ["p"], "department": "General"}],
                "summary_expectations": {"p": EXPECTED}}
    events = []

    async def warm(origin: str) -> dict:
        return {"wall_s": .01}

    async def run_burst(base: str, manifest: dict, **kwargs: Any) -> tuple:
        events.append("http_completed")
        assert kwargs["timeout"] == 20.0
        plan = benchmark.capacity.make_plan(kwargs["requests"], "fresh-ai", manifest, kwargs["run_id"])
        rows = [{"request_id": item["request_id"], "lane": item["lane"], "success": True,
                 "error": None, "status": 200, "model": "medgemma:27b", "cache_hit": False,
                 "full_s": 2.0, "completion_s": 1.9, "first_readable_s": .2,
                 "completed_within_target": True, "first_readable_within_target": True,
                 "synthetic_answer": GOOD, "summary_grounding": PROOF,
                 "summary_grounding_proof_count": 1} for item in plan]
        for row in rows:
            kwargs["result_observer"](dict(row))
        return {"wall_s": 2.5}, rows

    async def settle(base: str) -> dict:
        assert events == ["http_completed"]
        events.append("physical_cleanup")
        return {"wall_s": 9.0, "maximum_seconds": 10, "settled": True,
                "included_in_request_latency": False}

    def get(url: str, timeout: float) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url), json=metadata())

    monkeypatch.setattr(benchmark.httpx, "AsyncClient", lambda **kwargs: Client(get))
    monkeypatch.setattr(benchmark.qualification, "warm_model", warm)
    monkeypatch.setattr(benchmark.qualification, "progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(benchmark.capacity, "run_burst", run_burst)
    monkeypatch.setattr(benchmark, "settle_retrieval", settle)
    profile = {"phases": [fidelity_phase()]}
    args = SimpleNamespace(engine="mlx", parallel=1, phases=["burst"], workflow_quality_checks=True)
    asyncio.run(benchmark.phases("http://127.0.0.1:2222", args, profile,
                                 prepared_fixture=("http://127.0.0.1:1111", copy.deepcopy(manifest), None)))
    phase = profile["phases"][-1]
    assert events == ["http_completed", "physical_cleanup"]
    assert phase["summary"]["wall_s"] == 2.5
    assert phase["summary"]["completed_successfully_per_second"] == 80
    assert all(row["full_s"] == 2.0 and row["completion_s"] == 1.9 for row in phase["requests"])
    assert phase["post_request_retrieval_settle"]["wall_s"] == 9
    assert phase["post_request_retrieval_settle"]["included_in_request_latency"] is False
    assert phase["workflow_quality"]["patient_requests"] == 67
    assert phase["workflow_quality"]["all_patient_requests_passed"] is True
