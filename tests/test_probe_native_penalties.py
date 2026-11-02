"""CPU-only fake HTTP checks; no listener, model or GPU is started."""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_native_penalties.py"
SPEC = importlib.util.spec_from_file_location("orb_probe_native_penalties", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def snapshot(rows):
    return {"status_code": 200, "truncated": False, "response_json": {
        "completion_probabilities": [{"id": 7, "probs": rows}]}}


@pytest.mark.parametrize("origin", ["http://127.0.0.1:11434", "http://localhost:18001",
    "http://example.com:18001", "https://127.0.0.1:18001", "http://127.0.0.1",
    "http://127.0.0.1:80", "http://127.0.0.1:18001/a", "http://a@127.0.0.1:18001",
    "http://127.0.0.1:18001?x=1"])
def test_only_explicit_owned_loopback_http_origin_is_allowed(origin):
    with pytest.raises(probe.PenaltyProbeRefused):
        probe._origin(origin)


def test_ipv6_and_trailing_slash_are_canonicalised():
    assert probe._origin("http://[::1]:18002/") == "http://[::1]:18002"


@pytest.mark.asyncio
async def test_exact_request_pair_records_full_returned_json_and_token_matched_change(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path == "/props":
            return httpx.Response(200, json={"model_path": "/owned/pinned.gguf", "total_slots": 1})
        assert request.url.path == "/completion"
        body = json.loads(request.content)
        probability = .4 if body["repeat_penalty"] == 1 else .3
        return httpx.Response(200, json={"content": "synthetic", "tokens": [7], "extra": {"retained": True},
            "completion_probabilities": [{"id": 7, "probs": [
                {"tok_id": 7, "prob": probability, "tok_str": "same"},
                {"tok_id": 8, "prob": .2, "tok_str": "other"}]}]})

    output = tmp_path / "probe.json"
    report = await probe.probe_native_penalties("http://127.0.0.1:18002", [2, 7, 7], output,
                                               transport=httpx.MockTransport(respond))
    assert [request.url.path for request in calls] == ["/props", "/completion", "/completion", "/props"]
    bodies = [json.loads(request.content) for request in calls if request.url.path == "/completion"]
    expected = {"prompt": [2, 7, 7], "n_predict": 1, "temperature": 1.0, "top_k": 0,
                "top_p": 1.0, "min_p": 0.0, "seed": probe.SEED, "n_probs": 32,
                "post_sampling_probs": True, "cache_prompt": False, "repeat_last_n": 256,
                "repeat_penalty": 1.0, "frequency_penalty": 0.0, "presence_penalty": 0.0, "stream": False}
    assert bodies == [expected, {**expected, "repeat_penalty": 1.15}]
    assert report["responses"][0]["response_json"]["extra"] == {"retained": True}
    assert report["comparison"]["observation"] == "prompt_token_probability_change_observed"
    assert report["runner_advertised_identity"]["stable"] is True
    assert report["source_weights_identity_verified_by_probe"] is False
    assert report["clinical_quality_qualified"] is False
    assert report["comparison"]["full_distribution_observed"] is False
    assert json.loads(output.read_text()) == report
    assert output.stat().st_mode & 0o777 == 0o600


def test_partial_id_sets_are_never_compared_by_rank_or_text():
    left = snapshot([{"id": 7, "prob": .4}, {"id": 8, "prob": .2}])
    right = snapshot([{"id": 9, "prob": .4}, {"id": 7, "prob": .3}])
    result = probe.compare_snapshots([left, right], [7])
    assert result["observation"] == "inconclusive_different_partial_candidate_ids"
    assert [row["token_id"] for row in result["matched_tokens"]] == [7]


def test_unchanged_partial_snapshot_is_an_observation_not_proof():
    row = snapshot([{"id": 7, "prob": .4}])
    result = probe.compare_snapshots([row, row], [7])
    assert result["observation"] == "no_change_observed_in_returned_candidates"
    assert result["full_distribution_observed"] is False


def test_new_logprob_schema_is_matched_by_explicit_id():
    row = snapshot([])
    first = row["response_json"]["completion_probabilities"][0]
    del first["probs"]
    first["top_logprobs"] = [{"id": 8, "logprob": math.log(.2)}]
    result = probe.compare_snapshots([row, row], [8])
    assert result["matched_tokens"][0]["without_penalty_probability"] == pytest.approx(.2)


def test_actual_native_top_probs_schema_uses_ids_and_retains_raw_snapshot():
    left = {"status_code": 200, "response_json": {"completion_probabilities": [
        {"id": 2717, "token": "```", "prob": .9, "top_probs": [
            {"id": 2717, "token": "```", "bytes": [96, 96, 96], "prob": .9},
            {"id": 106, "token": "", "bytes": [], "prob": .05}]}]}}
    right = {"status_code": 200, "response_json": {"completion_probabilities": [
        {"id": 2717, "token": "```", "prob": .95, "top_probs": [
            {"id": 106, "token": "", "bytes": [], "prob": .01},
            {"id": 2717, "token": "```", "bytes": [96, 96, 96], "prob": .95}]}]}}
    before = json.dumps([left, right], sort_keys=True)
    result = probe.compare_snapshots([left, right], [106])
    assert result["observation"] == "prompt_token_probability_change_observed"
    assert [row["token_id"] for row in result["matched_tokens"]] == [106, 2717]
    assert json.dumps([left, right], sort_keys=True) == before


def native_response(rows, penalty):
    return {"status_code": 200, "response_json": {
        "completion_probabilities": [{"id": 8, "token": "chosen", "prob": .4, "top_probs": rows}],
        "timings": {"prompt_n": 3, "cache_n": 0, "predicted_n": 1},
        "generation_settings": {"seed": probe.SEED, "repeat_penalty": penalty, "post_sampling_probs": True,
            "n_probs": 32, "n_predict": 1, "repeat_last_n": 256, "temperature": 1.0, "top_k": 0,
            "top_p": 1.0, "min_p": 0.0, "presence_penalty": 0.0, "frequency_penalty": 0.0}}}


def relative_pair():
    # Nonprompt controls8/9 have a shared normalisation change. Prompt7 loses
    # relative odds; another top candidate is replaced. Missing candidates are
    # not needed to calculate odds for these explicitly matched IDs.
    left = native_response([{"id": 7, "prob": .2}, {"id": 8, "prob": .4},
                            {"id": 9, "prob": .1}, {"id": 10, "prob": .01}], 1.0)
    right = native_response([{"id": 7, "prob": .1}, {"id": 8, "prob": .45},
                             {"id": 9, "prob": .1125}, {"id": 11, "prob": .01}], 1.149999976158142)
    return [left, right]


def test_matched_relative_odds_can_identify_selective_prompt_penalty_with_different_partial_sets():
    pair = relative_pair()
    assert probe.compare_snapshots(pair, [2, 7, 7])["observation"] == "inconclusive_different_partial_candidate_ids"
    result = probe.relative_odds_analysis(pair, [2, 7, 7])
    assert result["same_partial_candidate_ids"] is False
    assert result["full_distribution_observed"] is False
    assert result["suppressed_prompt_tail_ids"] == [7]
    assert result["nonprompt_control_count"] == 2
    assert result["maximum_absolute_nonprompt_relative_log_odds_change"] < 1e-12
    assert result["prompt_history_penalties_observed_at_first_token"] is True
    row = next(row for row in result["rows"] if row["token_id"] == 7)
    assert row["relative_log_odds_change"] == pytest.approx(math.log((.1 / .45) / (.2 / .4)))


@pytest.mark.parametrize("change", ["earlier_generated_token", "cached_prompt", "settings_differ"])
def test_unattested_fresh_first_token_settings_cannot_establish_prompt_penalty(change):
    pair = relative_pair()
    data = pair[1]["response_json"]
    if change == "earlier_generated_token":
        data["timings"]["predicted_n"] = 2
    elif change == "cached_prompt":
        data["timings"]["cache_n"] = 3
    else:
        data["generation_settings"]["seed"] = 111
    result = probe.relative_odds_analysis(pair, [2, 7, 7])
    assert result["fresh_first_token_and_settings_attested"] is False
    assert result["prompt_history_penalties_observed_at_first_token"] is False


def test_uniform_probability_rescaling_has_no_selective_prompt_signal():
    pair = relative_pair()
    pair[1]["response_json"]["completion_probabilities"][0]["top_probs"][0]["prob"] = .225
    result = probe.relative_odds_analysis(pair, [2, 7, 7])
    assert result["suppressed_prompt_tail_ids"] == []
    assert result["prompt_history_penalties_observed_at_first_token"] is False


def test_zero_probabilities_are_excluded_and_missing_nonprompt_anchor_is_inconclusive():
    pair = [native_response([{"id": 7, "prob": 0}, {"id": 8, "prob": .5}], penalty)
            for penalty in [1.0, 1.15]]
    result = probe.relative_odds_analysis(pair, [2, 7, 8])
    assert result["excluded_zero_probability_ids"] == [7]
    assert result["observation"] == "inconclusive_no_positive_nonprompt_anchor"


def test_only_last_256_prompt_ids_are_marked_as_penalty_history():
    left = snapshot([{"id": 7, "prob": .4}])
    right = snapshot([{"id": 7, "prob": .3}])
    result = probe.compare_snapshots([left, right], [7] + [8] * 256)
    assert result["observation"] == "probability_change_observed_outside_prompt_tail"
    assert result["matched_tokens"][0]["in_repeat_last_n_prompt"] is False


@pytest.mark.parametrize("rows", [[{"token": "same", "prob": .4}], [{"id": True, "prob": .4}],
    [{"id": 7, "prob": True}], [{"id": 7, "prob": float("nan")}],
    [{"id": 7, "prob": 1.1}], [{"id": 7, "tok_id": 8, "prob": .4}],
    [{"id": 7, "prob": .4}, {"id": 7, "prob": .3}]])
def test_missing_ids_boolean_numbers_ambiguous_ids_and_invalid_probabilities_are_inconclusive(rows):
    result = probe.compare_snapshots([snapshot(rows), snapshot(rows)], [7])
    assert result["observation"] == "inconclusive_unsupported_response"


@pytest.mark.asyncio
async def test_unsupported_http_and_nonjson_response_are_retained_without_claim(tmp_path):
    def respond(request):
        if request.url.path == "/props":
            return httpx.Response(404, text="unsupported props")
        return httpx.Response(400, json={"error": "unknown post_sampling_probs"})

    report = await probe.probe_native_penalties("http://127.0.0.1:18002", [2], tmp_path / "out.json",
                                               transport=httpx.MockTransport(respond))
    assert report["responses"][0]["status_code"] == 400
    assert report["runner_props_before"]["response_text"] == "unsupported props"
    assert report["runner_advertised_identity"]["stable"] is None
    assert report["comparison"]["observation"] == "inconclusive_unsupported_response"


@pytest.mark.asyncio
async def test_oversized_response_is_bounded_and_never_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "MAX_RESPONSE_BYTES", 128)

    def respond(request):
        return httpx.Response(200, content=b"x" * 1000)

    report = await probe.probe_native_penalties("http://127.0.0.1:18002", [2], tmp_path / "out.json",
                                               transport=httpx.MockTransport(respond))
    assert all(row["truncated"] and row["captured_bytes"] == 128 for row in report["responses"])
    assert report["comparison"]["observation"] == "inconclusive_unsupported_response"


@pytest.mark.asyncio
async def test_nonfinite_json_and_redirects_stay_inconclusive(tmp_path):
    def respond(request):
        if request.url.path == "/props":
            return httpx.Response(302, headers={"location": "http://example.com/"})
        return httpx.Response(200, content=b'{"completion_probabilities":[],"bad":1e999}')

    report = await probe.probe_native_penalties("http://127.0.0.1:18002", [2], tmp_path / "out.json",
                                               transport=httpx.MockTransport(respond))
    assert report["runner_props_before"]["status_code"] == 302
    assert report["responses"][0]["json_valid"] is False
    assert report["comparison"]["observation"] == "inconclusive_unsupported_response"


@pytest.mark.asyncio
async def test_each_whole_http_call_has_a_bounded_timeout(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(probe, "REQUEST_TIMEOUT_S", .001)

    async def respond(request):
        await asyncio.sleep(.1)
        pytest.fail("The whole-call timeout should interrupt this response")

    report = await probe.probe_native_penalties("http://127.0.0.1:18002", [2], tmp_path / "out.json",
                                               transport=httpx.MockTransport(respond))
    assert all(row["error_type"] == "TimeoutError" for row in report["responses"])
    assert report["comparison"]["observation"] == "inconclusive_unsupported_response"


@pytest.mark.asyncio
async def test_advertised_identity_change_overrides_probability_observation(tmp_path):
    props = 0

    def respond(request):
        nonlocal props
        if request.url.path == "/props":
            props += 1
            return httpx.Response(200, json={"model_path": f"/owned/model-{props}.gguf"})
        return httpx.Response(200, json=snapshot([{"id": 7, "prob": .4}])["response_json"])

    report = await probe.probe_native_penalties("http://127.0.0.1:18002", [7], tmp_path / "out.json",
                                               transport=httpx.MockTransport(respond))
    assert report["runner_advertised_identity"]["stable"] is False
    assert report["comparison"]["observation"] == "inconclusive_runner_identity_changed"


@pytest.mark.parametrize("ids", [[], [True], [1.0], [-1], [2**31], [2] * 8193, (2,)])
@pytest.mark.asyncio
async def test_invalid_ids_are_refused_before_any_http_or_output(tmp_path, ids):
    def forbidden(request):
        pytest.fail("No HTTP request should occur")

    with pytest.raises(probe.PenaltyProbeRefused):
        await probe.probe_native_penalties("http://127.0.0.1:18002", ids, tmp_path / "out.json",
                                           transport=httpx.MockTransport(forbidden))
    assert not (tmp_path / "out.json").exists()


@pytest.mark.asyncio
async def test_existing_output_is_never_replaced(tmp_path):
    path = tmp_path / "out.json"
    path.write_text("preserve")
    with pytest.raises(probe.PenaltyProbeRefused):
        await probe.probe_native_penalties("http://127.0.0.1:18002", [2], path)
    assert path.read_text() == "preserve"
