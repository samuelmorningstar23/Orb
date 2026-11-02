"""Bounded first-token penalty diagnostic for an owned native llama runner.

Call only with exact *synthetic* prompt IDs from the separately verified owned
runner. This does not verify the source weights, start a service, or qualify
clinical quality. Top-32 probability snapshots are not full distributions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PROMPT_TOKENS = 8192
REQUEST_TIMEOUT_S = 60.0
PROBABILITY_TOLERANCE = 1e-7
RELATIVE_LOG_ODDS_TOLERANCE = 1e-5
SEED = 1729


class PenaltyProbeRefused(ValueError):
    """The requested origin, input or output is outside the diagnostic scope."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PenaltyProbeRefused("Use an explicit owned loopback HTTP origin.") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None or not 1024 <= port <= 65535 or port == 11434
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise PenaltyProbeRefused("Only an owned explicit loopback HTTP port other than 11434 is allowed.")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{port}"


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON constants are not supported.")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON numbers are not supported.")
    return number


async def _request(client: httpx.AsyncClient, method: str, url: str,
                   body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retain bounded response evidence even for HTTP errors or unsupported APIs."""
    result: dict[str, Any] = {"method": method, "endpoint": urlsplit(url).path,
                              "status_code": None, "truncated": False}
    started = time.monotonic()
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            async with client.stream(method, url, content=_canonical(body) if body is not None else None,
                                     headers={"Content-Type": "application/json"} if body is not None else None) as response:
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
                    result["response_text_prefix"] = payload.decode("utf-8", errors="replace")
                else:
                    try:
                        result["response_json"] = json.loads(payload, parse_constant=_reject_constant,
                                                              parse_float=_finite_float)
                    except (ValueError, RecursionError):
                        result["response_text"] = payload.decode("utf-8", errors="replace")
                        result["json_valid"] = False
                    else:
                        result["json_valid"] = True
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        result["error_type"] = type(exc).__name__
    result["elapsed_s"] = time.monotonic() - started
    return result


def _identity(response: dict[str, Any]) -> dict[str, Any] | None:
    """Observe stable advertised labels, without claiming a weight-file hash."""
    data = response.get("response_json")
    if response.get("status_code") != 200 or not isinstance(data, dict) or response.get("truncated"):
        return None
    labels = {key: data[key] for key in ("model_path", "model_alias", "model", "model_name")
              if isinstance(data.get(key), str) and data[key]}
    if not labels:
        return None
    return {"advertised_fields": sorted(labels),
            "advertised_labels_sha256": hashlib.sha256(_canonical(labels)).hexdigest(),
            "source_weights_identity_verified": False}


def _token_id(row: dict[str, Any]) -> int | None:
    values = [row[key] for key in ("id", "tok_id", "token_id") if key in row]
    if not values or any(type(value) is not int or value < 0 for value in values):
        return None
    return values[0] if len(set(values)) == 1 else None


def _probability(row: dict[str, Any]) -> float | None:
    value = row.get("prob", row.get("p"))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1:
        return float(value)
    value = row.get("logprob")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value <= 0:
        return math.exp(value)
    return None


def _snapshot(response: dict[str, Any]) -> tuple[dict[int, float] | None, str]:
    if response.get("status_code") != 200 or response.get("truncated") or response.get("error_type"):
        return None, "response_unsuccessful_or_truncated"
    data = response.get("response_json")
    rows = data.get("completion_probabilities") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None, "expected_exactly_one_completion_probability_snapshot"
    first = rows[0]
    alternatives = first.get("probs", first.get("top_probs", first.get("top_logprobs")))
    if not isinstance(alternatives, list) or not 1 <= len(alternatives) <= 128:
        return None, "missing_or_unsupported_probability_alternatives"
    probabilities: dict[int, float] = {}
    for row in alternatives:
        if not isinstance(row, dict):
            return None, "invalid_probability_candidate"
        token_id, probability = _token_id(row), _probability(row)
        if token_id is None or probability is None or token_id in probabilities:
            return None, "missing_ambiguous_or_duplicate_token_id_or_invalid_probability"
        probabilities[token_id] = probability
    return probabilities, "probabilities_with_explicit_token_ids"


def compare_snapshots(responses: list[dict[str, Any]], prompt_ids: list[int]) -> dict[str, Any]:
    """Compare only explicitly identified tokens; never align by rank or text."""
    left, left_schema = _snapshot(responses[0])
    right, right_schema = _snapshot(responses[1])
    result: dict[str, Any] = {"schema_observations": [left_schema, right_schema],
                              "full_distribution_observed": False,
                              "probability_tolerance": PROBABILITY_TOLERANCE,
                              "observation": "inconclusive_unsupported_response",
                              "matched_tokens": []}
    if left is None or right is None:
        return result
    prompt_tail = set(prompt_ids[-256:])
    common = sorted(left.keys() & right.keys())
    result.update({"candidate_counts": [len(left), len(right)],
                   "same_candidate_token_ids": left.keys() == right.keys(),
                   "only_without_penalty_ids": sorted(left.keys() - right.keys()),
                   "only_with_penalty_ids": sorted(right.keys() - left.keys())})
    result["matched_tokens"] = [{"token_id": token_id, "in_repeat_last_n_prompt": token_id in prompt_tail,
                                 "without_penalty_probability": left[token_id],
                                 "with_penalty_probability": right[token_id],
                                 "probability_delta": right[token_id] - left[token_id]}
                                for token_id in common]
    if left.keys() != right.keys():
        result["observation"] = "inconclusive_different_partial_candidate_ids"
        return result
    changed = [row for row in result["matched_tokens"]
               if abs(row["probability_delta"]) > PROBABILITY_TOLERANCE]
    if not changed:
        result["observation"] = "no_change_observed_in_returned_candidates"
    elif any(row["in_repeat_last_n_prompt"] for row in changed):
        result["observation"] = "prompt_token_probability_change_observed"
    else:
        result["observation"] = "probability_change_observed_outside_prompt_tail"
    return result


def relative_odds_analysis(responses: list[dict[str, Any]], prompt_ids: list[int]) -> dict[str, Any]:
    """Inspect common-token odds without comparing incomplete distributions.

    Softmax normalisation cancels in p(token)/p(anchor). Thus matched-token odds
    remain interpretable when other candidates enter/leave the partial top list.
    This does not recover missing logits, a complete distribution or semantics
    for all prompts. Zero-probability candidates have no finite log odds.
    """
    left, _ = _snapshot(responses[0])
    right, _ = _snapshot(responses[1])
    result: dict[str, Any] = {"observation": "inconclusive_unsupported_response",
                              "full_distribution_observed": False,
                              "relative_log_odds_tolerance": RELATIVE_LOG_ODDS_TOLERANCE,
                              "clinical_quality_qualified": False}
    if left is None or right is None:
        return result
    prompt_tail = set(prompt_ids[-256:])
    common = sorted(left.keys() & right.keys())
    positive = [token_id for token_id in common if left[token_id] > 0 and right[token_id] > 0]
    controls = [token_id for token_id in positive if token_id not in prompt_tail]
    result.update({"matched_candidate_count": len(common), "positive_probability_matched_count": len(positive),
                   "excluded_zero_probability_ids": sorted(set(common) - set(positive)),
                   "same_partial_candidate_ids": left.keys() == right.keys(),
                   "nonprompt_control_count": len(controls), "rows": []})
    if not controls:
        result["observation"] = "inconclusive_no_positive_nonprompt_anchor"
        return result
    anchor = max(controls, key=lambda token_id: left[token_id])
    anchor_change = math.log(right[anchor]) - math.log(left[anchor])
    rows = [{"token_id": token_id, "in_repeat_last_n_prompt": token_id in prompt_tail,
             "without_penalty_probability": left[token_id], "with_penalty_probability": right[token_id],
             "relative_log_odds_change": math.log(right[token_id]) - math.log(left[token_id]) - anchor_change}
            for token_id in positive]
    suppressed = [row["token_id"] for row in rows
                  if row["in_repeat_last_n_prompt"] and row["relative_log_odds_change"] < -RELATIVE_LOG_ODDS_TOLERANCE]
    increased = [row["token_id"] for row in rows
                 if row["in_repeat_last_n_prompt"] and row["relative_log_odds_change"] > RELATIVE_LOG_ODDS_TOLERANCE]
    control_error = max(abs(row["relative_log_odds_change"]) for row in rows if not row["in_repeat_last_n_prompt"])
    result.update({"anchor_token_id": anchor, "anchor_absent_from_entire_prompt": anchor not in prompt_ids,
                   "rows": rows, "suppressed_prompt_tail_ids": suppressed, "increased_prompt_tail_ids": increased,
                   "maximum_absolute_nonprompt_relative_log_odds_change": control_error})
    first_token_attested = True
    settings: list[dict[str, Any]] = []
    for index, response in enumerate(responses):
        data = response.get("response_json", {})
        timings = data.get("timings", {})
        generation = data.get("generation_settings", {})
        if not isinstance(timings, dict) or not isinstance(generation, dict):
            first_token_attested = False
            continue
        expected = {"prompt_n": len(prompt_ids), "cache_n": 0, "predicted_n": 1}
        if any(type(timings.get(key)) is not int or timings[key] != value for key, value in expected.items()):
            first_token_attested = False
        penalty = generation.get("repeat_penalty")
        if (not isinstance(penalty, (int, float)) or isinstance(penalty, bool)
                or not math.isclose(penalty, (1.0, 1.15)[index], rel_tol=1e-6)
                or generation.get("post_sampling_probs") is not True
                or generation.get("seed") != SEED
                or generation.get("n_probs") != 32 or generation.get("n_predict") != 1
                or generation.get("repeat_last_n") != 256
                or generation.get("temperature") != 1.0 or generation.get("top_k") != 0
                or generation.get("top_p") != 1.0 or generation.get("min_p") != 0.0
                or generation.get("presence_penalty") != 0.0 or generation.get("frequency_penalty") != 0.0):
            first_token_attested = False
        settings.append({key: value for key, value in generation.items() if key != "repeat_penalty"})
    settings_equal = len(settings) == 2 and settings[0] == settings[1]
    result["fresh_first_token_and_settings_attested"] = first_token_attested and settings_equal
    pattern = len(controls) >= 2 and bool(suppressed) and not increased and control_error <= RELATIVE_LOG_ODDS_TOLERANCE
    if pattern:
        result["observation"] = "matched_prompt_tail_odds_suppressed_with_stable_nonprompt_controls"
        result["prompt_history_penalties_observed_at_first_token"] = first_token_attested and settings_equal
    else:
        result["observation"] = "inconclusive_no_selective_prompt_tail_suppression_pattern"
        result["prompt_history_penalties_observed_at_first_token"] = False
    return result


async def probe_native_penalties(runner_origin: str, exact_prompt_token_ids: list[int],
                                 output_path: Path, *,
                                 transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Issue two sequential, one-token synthetic diagnostic completions only.

    The caller must already have verified the owned runner's source identity.
    The optional transport exists for CPU-only fake-HTTP tests.
    """
    origin = _origin(runner_origin)
    if (not isinstance(exact_prompt_token_ids, list) or not 1 <= len(exact_prompt_token_ids) <= MAX_PROMPT_TOKENS
            or any(type(value) is not int or not 0 <= value <= 2**31 - 1 for value in exact_prompt_token_ids)):
        raise PenaltyProbeRefused("Use 1–8192 exact nonnegative synthetic integer token IDs.")
    output_path = Path(output_path)
    if not output_path.is_absolute() or not output_path.parent.is_dir():
        raise PenaltyProbeRefused("Use a new absolute output file in an existing local directory.")
    prompt_ids = exact_prompt_token_ids[:]
    report: dict[str, Any] = {"schema_version": 1, "status": "running", "benchmark_only": True,
                              "synthetic_data_only": True, "clinical_quality_qualified": False,
                              "source_weights_identity_verified_by_probe": False,
                              "runner_origin": origin, "prompt_token_ids": prompt_ids,
                              "prompt_token_ids_sha256": hashlib.sha256(_canonical(prompt_ids)).hexdigest(),
                              "requests": [], "responses": [],
                              "limitations": [
                                  "The caller, not this helper, verifies ownership and source weights before this call.",
                                  "Each request generates one token; no earlier generated token should be in its repeat history.",
                                  "Top-32 snapshots are partial; unchanged probabilities do not prove prompt penalties are ignored.",
                                  "Different partial candidate ID sets cannot establish distribution equality or penalty semantics.",
                                  "Probability changes are observations, not proof of exact sampler semantics or clinical equivalence.",
                                  "Unsupported response fields or silently ignored request options leave the diagnostic inconclusive."]}
    try:
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise PenaltyProbeRefused("The probe requires a new writable output file.") from exc
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=transport,
                                     timeout=REQUEST_TIMEOUT_S) as client:
            before = await _request(client, "GET", origin + "/props")
            report["runner_props_before"] = before
            for penalty in (1.0, 1.15):
                body = {"prompt": prompt_ids[:], "n_predict": 1, "temperature": 1.0,
                        "top_k": 0, "top_p": 1.0, "min_p": 0.0, "seed": SEED,
                        "n_probs": 32, "post_sampling_probs": True, "cache_prompt": False,
                        "repeat_last_n": 256, "repeat_penalty": penalty,
                        "frequency_penalty": 0.0, "presence_penalty": 0.0, "stream": False}
                report["requests"].append(body)
                report["responses"].append(await _request(client, "POST", origin + "/completion", body))
            after = await _request(client, "GET", origin + "/props")
            report["runner_props_after"] = after
        identity_before, identity_after = _identity(before), _identity(after)
        report["runner_advertised_identity"] = {"before": identity_before, "after": identity_after,
            "stable": identity_before == identity_after if identity_before is not None and identity_after is not None else None}
        report["comparison"] = compare_snapshots(report["responses"], prompt_ids)
        report["matched_relative_odds_analysis"] = relative_odds_analysis(report["responses"], prompt_ids)
        if identity_before is not None and identity_after is not None and identity_before != identity_after:
            report["comparison"]["observation"] = "inconclusive_runner_identity_changed"
            report["matched_relative_odds_analysis"]["observation"] = "inconclusive_runner_identity_changed"
            report["matched_relative_odds_analysis"]["prompt_history_penalties_observed_at_first_token"] = False
        report["status"] = "diagnostic_recorded"
    except BaseException as exc:
        report["status"] = "interrupted"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        report["elapsed_s"] = time.monotonic() - started
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False).encode() + b"\n")
    return report
