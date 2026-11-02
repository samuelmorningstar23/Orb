"""Synthetic CPU fake-HTTP tests; no model, listener or GPU is started."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_engine_token_distribution.py"
SPEC = importlib.util.spec_from_file_location("orb_token_distribution_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def fixtures(tmp_path):
    import hashlib
    body = {"model": "medgemma:27b", "stream": True,
        "messages": [{"role": "system", "content": "Synthetic facts only"},
                     {"role": "user", "content": "Synthetic sodium141"}],
        "options": {"temperature": 0.0, "repeat_penalty": 1.15, "repeat_last_n": 256, "top_k": 40,
                    "top_p": .9, "min_p": 0.0, "num_ctx": 8192, "num_predict": 512, "stop": ["<end_of_turn>"]}}
    case = {"case_id": "numbers_and_units", "synthetic_data_only": True, "request_body": body,
            "body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()}
    rendered = "<bos>synthetic"
    oracle = {"benchmark_only": True, "synthetic_data_only": True, "generation_performed": False,
        "status": "oracles_collected", "all_effective_token_ids_observed": True, "model": "medgemma:27b",
        "oracles": [{"source_body_sha256": probe.digest(body), "native_token_ids": [2, 7, 8],
            "native_prompt_tokens": 3, "native_token_ids_sha256": probe.digest([2, 7, 8]),
            "rendered_template": rendered, "rendered_template_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "request_id": "a" * 32}]}
    oracle_path = tmp_path / "template-oracle.json"
    report_path = tmp_path / "report.json"
    oracle_path.write_text(json.dumps(oracle))
    report_path.write_text(json.dumps({"synthetic_data_only": True, "profile": {"phases": [{"cases": [case]}]}}))
    return oracle_path, report_path, body


def native_response(ids=(10, 11)):
    return {"completion_probabilities": [{"id": token_id, "prob": .8,
        "top_probs": [{"id": token_id, "prob": .8}, {"id": 99, "prob": .1}]} for token_id in ids],
        "timings": {"prompt_n": 3, "cache_n": 0, "predicted_n": len(ids)}}


def mlx_response(ids=(10, 11)):
    # The unsorted API outer ID is intentionally wrong; it cannot be used as
    # generated-token identity. The unique greedy argmax is still recoverable.
    return {"choices": [{"finish_reason": "length", "message": {"content": "synthetic"}, "logprobs": {
        "content": [{"id": 99, "logprob": -3.0, "top_logprobs": [
            {"id": 99, "logprob": -3.0}, {"id": token_id, "logprob": -.2}]} for token_id in ids]}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": len(ids), "prompt_tokens_details": {"cached_tokens": 0}}}


@pytest.mark.parametrize("origin", ["http://127.0.0.1:11434", "http://localhost:19001", "https://127.0.0.1:19001",
    "http://127.0.0.1", "http://127.0.0.1:80", "http://example.com:19001", "http://127.0.0.1:19001/a",
    "http://a@127.0.0.1:19001", "http://127.0.0.1:19001?x=1"])
def test_only_explicit_owned_loopback_ports_are_allowed(origin):
    with pytest.raises(probe.DistributionProbeRefused):
        probe.owned_origin(origin)


def test_explicit_ipv6_is_supported():
    assert probe.owned_origin("http://[::1]:19001/") == "http://[::1]:19001"


@pytest.mark.parametrize("tamper", ["synthetic", "case_hash", "body_oracle_join", "token_hash", "render_hash", "duplicate_case", "duplicate_oracle", "bool_token"])
def test_immutable_fixture_attestations_and_exact_case_joins_are_required(tmp_path, tamper):
    oracle, report, _ = fixtures(tmp_path)
    a, b = json.loads(oracle.read_text()), json.loads(report.read_text())
    case = b["profile"]["phases"][0]["cases"][0]
    if tamper == "synthetic":
        a["synthetic_data_only"] = False
    elif tamper == "case_hash":
        case["request_body"]["messages"][1]["content"] = "changed"
    elif tamper == "body_oracle_join":
        a["oracles"][0]["source_body_sha256"] = "0" * 64
    elif tamper == "token_hash":
        a["oracles"][0]["native_token_ids_sha256"] = "0" * 64
    elif tamper == "render_hash":
        a["oracles"][0]["rendered_template"] += "changed"
    elif tamper == "duplicate_case":
        b["profile"]["phases"][0]["cases"].append(case)
    elif tamper == "duplicate_oracle":
        a["oracles"].append(a["oracles"][0])
    else:
        a["oracles"][0]["native_token_ids"] = [2, True, 8]
        a["oracles"][0]["native_token_ids_sha256"] = probe.digest([2, True, 8])
    oracle.write_text(json.dumps(a))
    report.write_text(json.dumps(b))
    with pytest.raises(probe.DistributionProbeRefused):
        probe.load_case_fixture(oracle, report, "numbers_and_units")


@pytest.mark.parametrize("budget", [0, 129, True, 1.5])
def test_output_budget_is_bounded(tmp_path, budget):
    oracle, report, _ = fixtures(tmp_path)
    with pytest.raises(probe.DistributionProbeRefused):
        probe.load_case_fixture(oracle, report, "numbers_and_units", budget)


@pytest.mark.asyncio
async def test_native_gets_exact_ids_and_requested_presampling_probabilities(tmp_path):
    oracle, _, _ = fixtures(tmp_path)
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.path == "/completion"
        body = json.loads(request.content)
        assert body == {"prompt": [2, 7, 8], "n_predict": 128, "temperature": 0.0,
            "repeat_penalty": 1.15, "repeat_last_n": 256, "top_k": 40, "top_p": .9, "min_p": 0.0,
            "presence_penalty": 0.0, "frequency_penalty": 0.0, "n_probs": 20,
            "post_sampling_probs": False, "cache_prompt": False, "stream": False, "stop": ["<end_of_turn>"]}
        return httpx.Response(200, json=native_response())

    output = tmp_path / "native.json"
    report = await probe.probe_engine_token_distribution("http://127.0.0.1:19001", "native", oracle, output,
                                                        transport=httpx.MockTransport(respond))
    assert len(calls) == 1
    assert report["token_evidence"]["selected_token_ids"] == [10, 11]
    assert report["source_files_unchanged_after_response"] is True
    assert report["timing_or_SLA_evidence"] is False
    assert json.loads(output.read_text()) == report
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_mlx_gets_original_chat_messages_and_supported_top11_without_seed_disabling_batching(tmp_path):
    oracle, _, body = fixtures(tmp_path)

    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        request_body = json.loads(request.content)
        assert request_body["messages"] == body["messages"]
        assert request_body["top_logprobs"] == 11
        assert request_body["logprobs"] is True
        assert "seed" not in request_body and "prompt" not in request_body
        assert request_body["repetition_penalty"] == 1.15
        assert request_body["repetition_context_size"] == 256
        return httpx.Response(200, json=mlx_response())

    report = await probe.probe_engine_token_distribution("http://127.0.0.1:19002", "mlx", oracle,
        tmp_path / "mlx.json", transport=httpx.MockTransport(respond))
    evidence = report["token_evidence"]
    assert evidence["selected_token_ids"] == [10, 11]
    assert evidence["tokens"][0]["api_outer_id_artifact"] == 99
    assert evidence["input_token_ids_independently_observed"] is False


def test_mlx_greedy_ties_are_ambiguous_and_never_resolved_from_outer_id():
    data = mlx_response((10,))
    data["choices"][0]["logprobs"]["content"][0]["top_logprobs"][0]["logprob"] = -.2
    parsed = probe.parse_token_evidence("mlx", {"status_code": 200, "response_json": data}, 3, 128)
    assert parsed["selected_token_ids"] == [None]
    assert parsed["has_ambiguous_selected_ids"] is True


def test_native_presampling_actual_logprob_schema_keeps_observed_selected_id():
    data = {"completion_probabilities": [{"id": 2717, "token": "```", "bytes": [96, 96, 96],
        "logprob": -.0001298273, "top_logprobs": [
            {"id": 2717, "token": "```", "bytes": [96, 96, 96], "logprob": -.0001298273},
            {"id": 106, "token": "", "bytes": [], "logprob": -9.6567087}]}],
        "timings": {"prompt_n": 3, "cache_n": 0, "predicted_n": 1}}
    parsed = probe.parse_token_evidence("native", {"status_code": 200, "response_json": data}, 3, 128)
    assert parsed["status"] == "token_probability_evidence_recorded"
    assert parsed["selected_token_ids"] == [2717]
    assert parsed["tokens"][0]["selected_logprob"] == pytest.approx(-.0001298273)
    assert parsed["tokens"][0]["selected_identity_evidence"] == "native_completion_probability_id"


def compared_report(engine, ids):
    data = native_response(ids) if engine == "native" else mlx_response(ids)
    return {"case_id": "numbers_and_units", "source_body_sha256": "a", "native_token_ids_sha256": "b",
            "diagnostic_policy": {"max_tokens": 128, "repeat_penalty": 1.15},
            "token_evidence": probe.parse_token_evidence(engine, {"status_code": 200, "response_json": data}, 3, 128)}


def test_first_divergence_stops_probability_comparison_after_contexts_differ():
    native, mlx = compared_report("native", (10, 11, 12)), compared_report("mlx", (10, 20, 12))
    result = probe.compare_token_evidence(native, mlx)
    assert result["first_divergence_index"] == 1
    assert result["matched_prefix_tokens"] == 1
    assert result["native"]["selected_token_id"] == 11
    assert result["mlx"]["selected_token_id"] == 20
    assert result["later_token_distributions_not_compared"] is True
    assert result["requires_separate_mlx_input_tokenization_parity_evidence"] is True
    assert [row["index"] for row in result["aligned_context_candidate_observations"]] == [0, 1]
    assert result["aligned_context_candidate_observations"][0]["probability_stage_equivalence_verified"] is False


def test_candidate_overlap_summary_matches_only_explicit_shared_ids():
    native = {"index": 0, "candidate_logprobs": [{"id": 7, "logprob": -.001}, {"id": 8, "logprob": -9.0}]}
    mlx = {"index": 0, "candidate_logprobs": [{"id": 9, "logprob": -2.0}, {"id": 7, "logprob": -.0625}]}
    result = probe.candidate_overlap_summary(native, mlx)
    assert result["common_ids"] == [{"id": 7, "native_logprob": -.001, "mlx_logprob": -.0625,
                                     "mlx_minus_native_logprob": -.0615}]
    assert result["native_only_candidate_ids"] == [8]
    assert result["mlx_only_candidate_ids"] == [9]
    assert result["full_distributions_compared"] is False


@pytest.mark.parametrize("change", ["source", "policy", "cache", "count"])
def test_comparisons_refuse_mixed_fixtures_policies_and_unattested_fresh_prompts(change):
    native, mlx = compared_report("native", (10,)), compared_report("mlx", (10,))
    if change == "source":
        mlx["native_token_ids_sha256"] = "changed"
    elif change == "policy":
        mlx["diagnostic_policy"] = {"repeat_penalty": 1.0}
    elif change == "cache":
        mlx["token_evidence"]["fresh_prompt_attested"] = False
    else:
        mlx["token_evidence"]["prompt_count_matches_oracle"] = False
    assert probe.compare_token_evidence(native, mlx)["status"] == "inconclusive"


@pytest.mark.asyncio
async def test_source_file_changes_during_response_are_recorded_and_disqualify_evidence(tmp_path):
    oracle, _, _ = fixtures(tmp_path)

    def respond(request):
        data = json.loads(oracle.read_text())
        data["extra"] = "changed"
        oracle.write_text(json.dumps(data))
        return httpx.Response(200, json=native_response())

    result = await probe.probe_engine_token_distribution("http://127.0.0.1:19001", "native", oracle,
        tmp_path / "out.json", transport=httpx.MockTransport(respond))
    assert result["status"] == "inconclusive_source_fixture_changed"
    assert result["token_evidence"]["status"] == "inconclusive_source_fixture_changed"


@pytest.mark.asyncio
async def test_http_errors_redirects_and_oversized_data_are_bounded_without_following(tmp_path, monkeypatch):
    oracle, _, _ = fixtures(tmp_path)
    monkeypatch.setattr(probe, "MAX_RESPONSE_BYTES", 128)

    def respond(request):
        assert request.url.host == "127.0.0.1"
        return httpx.Response(302, content=b"x" * 1000, headers={"location": "https://example.com"})

    result = await probe.probe_engine_token_distribution("http://127.0.0.1:19001", "native", oracle,
        tmp_path / "out.json", transport=httpx.MockTransport(respond))
    assert result["response"]["captured_bytes"] == 128
    assert result["response"]["truncated"] is True
    assert result["token_evidence"]["status"] == "inconclusive_unsupported_response"


@pytest.mark.asyncio
async def test_whole_call_timeout_persists_inconclusive_evidence(tmp_path, monkeypatch):
    import asyncio
    oracle, _, _ = fixtures(tmp_path)
    monkeypatch.setattr(probe, "REQUEST_TIMEOUT_S", .001)

    async def respond(request):
        await asyncio.sleep(.1)
        pytest.fail("Whole-call timeout should stop this response")

    result = await probe.probe_engine_token_distribution("http://127.0.0.1:19001", "native", oracle,
        tmp_path / "out.json", transport=httpx.MockTransport(respond))
    assert result["response"]["error_type"] == "TimeoutError"
    assert result["token_evidence"]["status"] == "inconclusive_unsupported_response"


@pytest.mark.asyncio
async def test_existing_evidence_is_never_overwritten(tmp_path):
    oracle, _, _ = fixtures(tmp_path)
    output = tmp_path / "out.json"
    output.write_text("keep")
    with pytest.raises(probe.DistributionProbeRefused):
        await probe.probe_engine_token_distribution("http://127.0.0.1:19001", "native", oracle, output)
    assert output.read_text() == "keep"
