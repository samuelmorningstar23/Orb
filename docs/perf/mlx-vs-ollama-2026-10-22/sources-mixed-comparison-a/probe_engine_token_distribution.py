"""Owned-local, synthetic token-distribution diagnostic; never a timing test.

Native accepts the immutable oracle's IDs directly. Pinned MLX-LM0.32.2
requires the corresponding chat messages and permits at most11 top logprobs.
Its outer logprob row copies an unsorted top-candidate entry, so that ID is
preserved as an API artifact, not treated as an observed generated token.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_S = 120.0
MAX_OUTPUT_TOKENS = 128
MODEL = "medgemma:27b"


class DistributionProbeRefused(ValueError):
    """The source fixture, origin or options cannot support this diagnostic."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def owned_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise DistributionProbeRefused("Use an explicit owned loopback HTTP origin.") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None or not 1024 <= port <= 65535 or port == 11434
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise DistributionProbeRefused("Only an owned explicit loopback HTTP port other than 11434 is allowed.")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{port}"


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON constants are unsupported.")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON numbers are unsupported.")
    return result


def _read_json(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise DistributionProbeRefused("Use a bounded existing absolute local JSON fixture.")
    before = path.stat()
    with path.open("rb") as stream:
        payload = stream.read(MAX_SOURCE_BYTES + 1)
    after = path.stat()
    if (len(payload) > MAX_SOURCE_BYTES or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise DistributionProbeRefused("The JSON fixture is oversized or changed while being read.")
    try:
        data = json.loads(payload, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError) as exc:
        raise DistributionProbeRefused("The fixture is not a valid finite JSON object.") from exc
    if not isinstance(data, dict):
        raise DistributionProbeRefused("The fixture must be a JSON object.")
    return data, {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def load_case_fixture(oracle_path: Path, case_report_path: Path, case_id: str,
                      max_tokens: int = MAX_OUTPUT_TOKENS) -> dict[str, Any]:
    """Join an unchanged fidelity request to exactly one native token oracle."""
    oracle, oracle_info = _read_json(oracle_path)
    cases_report, report_info = _read_json(case_report_path)
    if (oracle.get("benchmark_only") is not True or oracle.get("synthetic_data_only") is not True
            or oracle.get("generation_performed") is not False or oracle.get("status") != "oracles_collected"
            or oracle.get("all_effective_token_ids_observed") is not True or oracle.get("model") != MODEL
            or cases_report.get("synthetic_data_only") is not True):
        raise DistributionProbeRefused("Only synthetic, observed native prompt-oracle fixtures are accepted.")
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_OUTPUT_TOKENS:
        raise DistributionProbeRefused("The diagnostic output budget must be between1 and128 tokens.")
    profile = cases_report.get("profile")
    phases = profile.get("phases") if isinstance(profile, dict) else None
    if not isinstance(phases, list):
        raise DistributionProbeRefused("The synthetic fidelity report has no phase list.")
    matches = [case for phase in phases if isinstance(phase, dict) and isinstance(phase.get("cases"), list)
               for case in phase["cases"] if isinstance(case, dict) and case.get("case_id") == case_id]
    if len(matches) != 1 or matches[0].get("synthetic_data_only") is not True:
        raise DistributionProbeRefused("Select exactly one attested synthetic fidelity case.")
    case = matches[0]
    body = case.get("request_body")
    if not isinstance(body, dict) or body.get("model") != MODEL:
        raise DistributionProbeRefused("The source case model or request is unsupported.")
    # Fidelity reports use the documented json.dumps(sort_keys=True) hash;
    # the oracle/capture uses compact canonical JSON. Verify both conventions.
    if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != case.get("body_sha256"):
        raise DistributionProbeRefused("The original fidelity request-body hash differs.")
    messages = body.get("messages")
    if (not isinstance(messages, list) or not messages
            or any(not isinstance(message, dict) or set(message) != {"role", "content"}
                   or message.get("role") not in {"system", "user", "assistant"}
                   or not isinstance(message.get("content"), str) for message in messages)):
        raise DistributionProbeRefused("Only the original text-only chat messages are supported.")
    options = body.get("options")
    required = {"temperature": 0.0, "repeat_penalty": 1.15, "repeat_last_n": 256,
                "top_k": 40, "top_p": .9, "min_p": 0.0, "num_ctx": 8192, "stop": ["<end_of_turn>"]}
    if not isinstance(options, dict) or any(options.get(key) != value for key, value in required.items()):
        raise DistributionProbeRefused("The source case differs from the pinned fidelity policy.")
    entries = oracle.get("oracles")
    selected = [row for row in entries if isinstance(row, dict) and row.get("source_body_sha256") == digest(body)] if isinstance(entries, list) else []
    if len(selected) != 1:
        raise DistributionProbeRefused("The source request does not map to exactly one immutable native oracle.")
    entry = selected[0]
    ids = entry.get("native_token_ids")
    if (not isinstance(ids, list) or not ids or len(ids) + max_tokens > 8192
            or any(type(value) is not int or not 0 <= value < 2**31 for value in ids)
            or digest(ids) != entry.get("native_token_ids_sha256")
            or entry.get("native_prompt_tokens") != len(ids)):
        raise DistributionProbeRefused("The observed native token IDs or their digest/budget differ.")
    rendered = entry.get("rendered_template")
    if (not isinstance(rendered, str) or hashlib.sha256(rendered.encode()).hexdigest()
            != entry.get("rendered_template_sha256")):
        raise DistributionProbeRefused("The observed rendered template hash differs.")
    return {"case_id": case_id, "request_body": body, "native_token_ids": ids,
            "native_token_ids_sha256": digest(ids), "source_body_sha256": digest(body),
            "oracle_request_id": entry.get("request_id"), "oracle": oracle_info, "case_report": report_info}


def build_request(engine: str, fixture: dict[str, Any], max_tokens: int, repeat_penalty: float) -> tuple[str, dict[str, Any]]:
    if engine == "native":
        return "/completion", {"prompt": fixture["native_token_ids"], "n_predict": max_tokens,
            "temperature": 0.0, "repeat_penalty": repeat_penalty, "repeat_last_n": 256,
            "top_k": 40, "top_p": .9, "min_p": 0.0, "presence_penalty": 0.0,
            "frequency_penalty": 0.0, "n_probs": 20, "post_sampling_probs": False,
            "cache_prompt": False, "stream": False, "stop": ["<end_of_turn>"]}
    return "/v1/chat/completions", {"model": "default_model", "messages": fixture["request_body"]["messages"],
        "max_tokens": max_tokens, "temperature": 0.0, "repetition_penalty": repeat_penalty,
        "repetition_context_size": 256, "top_k": 40, "top_p": .9, "min_p": 0.0,
        "presence_penalty": 0.0, "frequency_penalty": 0.0, "num_draft_tokens": 0,
        "xtc_probability": 0.0, "logprobs": True, "top_logprobs": 11,
        "stream": False, "stop": ["<end_of_turn>"]}


async def _post(client: httpx.AsyncClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"status_code": None, "truncated": False}
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            async with client.stream("POST", url, content=canonical(body), headers={"Content-Type": "application/json"}) as response:
                result["status_code"] = response.status_code
                result["content_type"] = response.headers.get("content-type", "")[:256]
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = MAX_RESPONSE_BYTES - len(payload)
                    payload.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        result["truncated"] = True
                        break
                result["captured_bytes"] = len(payload)
                result["captured_bytes_sha256"] = hashlib.sha256(payload).hexdigest()
                if result["truncated"]:
                    result["response_text_prefix"] = payload.decode(errors="replace")
                else:
                    try:
                        result["response_json"] = json.loads(payload, parse_constant=_reject_constant, parse_float=_finite_float)
                    except (ValueError, RecursionError):
                        result["response_text"] = payload.decode(errors="replace")
                        result["json_valid"] = False
                    else:
                        result["json_valid"] = True
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        result["error_type"] = type(exc).__name__
    return result


def _identifier(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _logprob(row: dict[str, Any]) -> float | None:
    if "logprob" in row:
        value = row["logprob"]
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value <= 0 else None
    value = row.get("prob")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= 1:
        return math.log(value)
    return None


def parse_token_evidence(engine: str, response: dict[str, Any], expected_prompt_tokens: int,
                         max_tokens: int) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "inconclusive_unsupported_response", "tokens": [],
                              "full_distribution_observed": False, "input_token_ids_independently_observed": engine == "native"}
    data = response.get("response_json")
    if (response.get("status_code") != 200 or response.get("truncated") or response.get("error_type")
            or not isinstance(data, dict)):
        return result
    if engine == "native":
        snapshots = data.get("completion_probabilities")
        timings = data.get("timings", {})
        prompt_count = timings.get("prompt_n") if isinstance(timings, dict) else None
        cache_count = timings.get("cache_n") if isinstance(timings, dict) else None
    else:
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return result
        logprobs = choices[0].get("logprobs")
        snapshots = logprobs.get("content") if isinstance(logprobs, dict) else None
        usage = data.get("usage", {})
        prompt_count = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        details = usage.get("prompt_tokens_details", {}) if isinstance(usage, dict) else {}
        cache_count = details.get("cached_tokens") if isinstance(details, dict) else None
    result["prompt_tokens_reported"] = prompt_count
    result["prompt_count_matches_oracle"] = type(prompt_count) is int and prompt_count == expected_prompt_tokens
    result["cached_prompt_tokens_reported"] = cache_count
    result["fresh_prompt_attested"] = type(cache_count) is int and cache_count == 0
    if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= max_tokens:
        return result
    parsed = []
    for index, row in enumerate(snapshots):
        if not isinstance(row, dict):
            return result
        alternatives = row.get("top_probs", row.get("probs", row.get("top_logprobs"))) if engine == "native" else row.get("top_logprobs")
        if not isinstance(alternatives, list) or not 1 <= len(alternatives) <= 64:
            return result
        candidates: dict[int, float] = {}
        for candidate in alternatives:
            if not isinstance(candidate, dict):
                return result
            token_id = _identifier(candidate.get("id", candidate.get("tok_id")))
            probability = _logprob(candidate)
            if token_id is None or probability is None or token_id in candidates:
                return result
            candidates[token_id] = probability
        token: dict[str, Any] = {"index": index, "candidate_logprobs": [
            {"id": token_id, "logprob": logprob} for token_id, logprob in sorted(candidates.items())]}
        if engine == "native":
            selected_id = _identifier(row.get("id"))
            if selected_id is None:
                return result
            token.update({"selected_token_id": selected_id, "selected_identity_evidence": "native_completion_probability_id",
                          "selected_logprob": _logprob(row)})
        else:
            maximum = max(candidates.values())
            maxima = [token_id for token_id, probability in candidates.items() if math.isclose(probability, maximum, rel_tol=0, abs_tol=1e-7)]
            token.update({"selected_token_id": maxima[0] if len(maxima) == 1 else None,
                          "selected_identity_evidence": "inferred_unique_greedy_argmax_not_observed_generated_id",
                          "api_outer_id_artifact": _identifier(row.get("id")), "argmax_candidate_ids": maxima,
                          "selected_logprob": maximum})
        parsed.append(token)
    result.update({"status": "token_probability_evidence_recorded", "tokens": parsed,
                   "selected_token_ids": [row["selected_token_id"] for row in parsed],
                   "has_ambiguous_selected_ids": any(row["selected_token_id"] is None for row in parsed)})
    return result


def candidate_overlap_summary(native_token: dict[str, Any], mlx_token: dict[str, Any]) -> dict[str, Any]:
    """Record common-ID logprob deltas without filling missing alternatives."""
    native = {row["id"]: row["logprob"] for row in native_token["candidate_logprobs"]}
    mlx = {row["id"]: row["logprob"] for row in mlx_token["candidate_logprobs"]}
    common = sorted(native.keys() & mlx.keys())
    return {"index": native_token["index"], "candidate_counts": {"native": len(native), "mlx": len(mlx)},
            "common_candidate_count": len(common), "full_distributions_compared": False,
            "probability_stage_equivalence_verified": False,
            "common_ids": [{"id": token_id, "native_logprob": native[token_id], "mlx_logprob": mlx[token_id],
                            "mlx_minus_native_logprob": mlx[token_id] - native[token_id]} for token_id in common],
            "native_only_candidate_ids": sorted(native.keys() - mlx.keys()),
            "mlx_only_candidate_ids": sorted(mlx.keys() - native.keys())}


def compare_token_evidence(native: dict[str, Any], mlx: dict[str, Any]) -> dict[str, Any]:
    """Locate the first token-ID divergence without comparing later contexts."""
    result: dict[str, Any] = {"status": "inconclusive", "clinical_quality_qualified": False,
                              "full_distributions_compared": False,
                              "aligned_context_candidate_observations": [],
                              "requires_separate_mlx_input_tokenization_parity_evidence": True}
    for key in ("source_body_sha256", "native_token_ids_sha256", "case_id", "diagnostic_policy"):
        if native.get(key) != mlx.get(key) or native.get(key) is None:
            result["reason"] = "different_or_missing_source_fixture_or_policy"
            return result
    left = native.get("token_evidence", {})
    right = mlx.get("token_evidence", {})
    if (left.get("status") != "token_probability_evidence_recorded"
            or right.get("status") != "token_probability_evidence_recorded"
            or not left.get("prompt_count_matches_oracle") or not right.get("prompt_count_matches_oracle")
            or not left.get("fresh_prompt_attested") or not right.get("fresh_prompt_attested")):
        result["reason"] = "token_evidence_or_fresh_prompt_counts_not_attested"
        return result
    for index, (a, b) in enumerate(zip(left["tokens"], right["tokens"])):
        if a["selected_token_id"] is None or b["selected_token_id"] is None:
            result.update({"reason": "ambiguous_greedy_identity", "matched_prefix_tokens": index})
            return result
        result["aligned_context_candidate_observations"].append(candidate_overlap_summary(a, b))
        if a["selected_token_id"] != b["selected_token_id"]:
            result.update({"status": "first_greedy_token_divergence_recorded", "first_divergence_index": index,
                           "matched_prefix_tokens": index, "native": a, "mlx": b,
                           "later_token_distributions_not_compared": True})
            return result
    result.update({"status": "no_divergence_in_common_observed_prefix",
                   "matched_prefix_tokens": min(len(left["tokens"]), len(right["tokens"])),
                   "observed_lengths": [len(left["tokens"]), len(right["tokens"])]})
    return result


async def probe_engine_token_distribution(origin: str, engine: str, oracle_path: Path, output_path: Path, *,
        case_id: str = "numbers_and_units", case_report_path: Path | None = None,
        max_tokens: int = MAX_OUTPUT_TOKENS, repeat_penalty: float = 1.15,
        transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    origin = owned_origin(origin)
    if engine not in {"native", "mlx"} or type(repeat_penalty) not in (int, float) or repeat_penalty not in {1.0, 1.15}:
        raise DistributionProbeRefused("Use native or mlx and an explicit diagnostic penalty1.0 or1.15.")
    oracle_path, output_path = Path(oracle_path), Path(output_path)
    report_path = Path(case_report_path) if case_report_path is not None else oracle_path.parent / "report.json"
    fixture = load_case_fixture(oracle_path, report_path, case_id, max_tokens)
    if not output_path.is_absolute() or not output_path.parent.is_dir():
        raise DistributionProbeRefused("Use a new absolute output file in an existing local directory.")
    endpoint, body = build_request(engine, fixture, max_tokens, repeat_penalty)
    report: dict[str, Any] = {"schema_version": 1, "status": "running", "benchmark_only": True,
        "synthetic_data_only": True, "clinical_quality_qualified": False, "timing_or_SLA_evidence": False,
        "source_weights_identity_verified_by_probe": False, "engine": engine, "origin": origin,
        "case_id": case_id, "oracle": fixture["oracle"], "case_report": fixture["case_report"],
        "source_body_sha256": fixture["source_body_sha256"], "native_token_ids_sha256": fixture["native_token_ids_sha256"],
        "oracle_request_id": fixture["oracle_request_id"], "source_request_body": fixture["request_body"],
        "expected_native_prompt_token_ids": fixture["native_token_ids"], "endpoint": endpoint, "request_body": body,
        "request_body_sha256": digest(body), "diagnostic_policy": {"max_tokens": max_tokens,
            "temperature": 0.0, "repeat_penalty": repeat_penalty, "repeat_last_n": 256, "top_k": 40,
            "top_p": .9, "min_p": 0.0, "presence_penalty": 0.0, "frequency_penalty": 0.0},
        "limitations": [
            "The caller separately verifies the owned model process and source weights; this helper does not.",
            "Native receives exact observed IDs. MLX receives unchanged chat messages; prompt counts alone do not prove token-ID parity.",
            "Native n_probs20 versus MLX maximum top_logprobs11 reflects the pinned MLX-LM0.32.2 API limit.",
            "MLX outer logprob IDs come from an unsorted candidate list; unique argmax IDs are inferred for temperature0, not directly observed generated IDs.",
            "Native post_sampling_probsFalse is requested; its exact probability stage must be interpreted separately from MLX post-processor/pre-sampler logprobs.",
            "MLX has no per-request prefix-cache disable option; launch its diagnostic server with prompt-cache-size0 and verify cached_tokens0.",
            "A128-token cap changes the fidelity workload. These diagnostics do not qualify clinical accuracy, throughput, or latency."]}
    try:
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise DistributionProbeRefused("The output must be a new writable local file.") from exc
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=transport,
                                     timeout=REQUEST_TIMEOUT_S) as client:
            report["response"] = await _post(client, origin + endpoint, body)
        report["token_evidence"] = parse_token_evidence(engine, report["response"], len(fixture["native_token_ids"]), max_tokens)
        unchanged = all(_read_json(Path(info["path"]))[1]["sha256"] == info["sha256"]
                        for info in [fixture["oracle"], fixture["case_report"]])
        report["source_files_unchanged_after_response"] = unchanged
        report["status"] = "diagnostic_recorded" if unchanged else "inconclusive_source_fixture_changed"
        if not unchanged:
            report["token_evidence"]["status"] = "inconclusive_source_fixture_changed"
    except BaseException as exc:
        report["status"] = "interrupted"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--engine", choices=["native", "mlx"], required=True)
    parser.add_argument("--oracle", required=True, type=Path)
    parser.add_argument("--case-report", type=Path)
    parser.add_argument("--case-id", default="numbers_and_units")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--repeat-penalty", type=float, choices=[1.0, 1.15], default=1.15)
    args = parser.parse_args()
    report = asyncio.run(probe_engine_token_distribution(args.origin, args.engine, args.oracle, args.output,
        case_id=args.case_id, case_report_path=args.case_report, max_tokens=args.max_tokens, repeat_penalty=args.repeat_penalty))
    print(json.dumps({"status": report["status"], "case_id": report["case_id"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
