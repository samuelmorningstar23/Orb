"""Strict engine-only replay of 200 distinct original synthetic Sage inputs.

Retrieval is deliberately absent from the timed generation phase. These results
cannot establish combined Orb latency or clinical equivalence. Input bodies,
sampling and output budgets are preserved exactly; failures stay in the cohort.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from loadtest_capacity import TARGET_SECONDS, distribution
from ollama_template_oracle import MODEL, canonical, captured_chats, owned_origin

REQUESTS = 200
HARD_TIMEOUT_SECONDS = 20.0


def original_inputs(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    recorded, provenance = captured_chats(path)
    rows = [row for row in recorded if row["body"].get("options", {}).get("temperature") != 0]
    if len(rows) != REQUESTS or len({row["body_sha256"] for row in rows}) != REQUESTS:
        raise ValueError("Need exactly200distinct captured original non-greedy Sage inputs")
    for row in rows:
        body, options = row["body"], row["body"].get("options", {})
        budget = options.get("num_predict")
        if type(budget) is not int or type(options.get("num_ctx")) is not int:
            raise ValueError("Clinical token budgets must remain integer values")
        inferred_lane = {512: "general", 2048: "patient", 1024: "department", 768: "department"}.get(budget)
        if (body.get("stream") is not True or options.get("num_ctx") != 8192
                or options.get("temperature") not in {.2, .4}
                or options.get("repeat_penalty") != 1.15 or options.get("repeat_last_n") != 256
                or inferred_lane is None or row.get("lane", inferred_lane) != inferred_lane):
            raise ValueError("Captured input does not preserve the original clinical scope and serving contract")
    return rows, provenance


async def execute_one(client: httpx.AsyncClient, origin: str, source: dict[str, Any], *,
                      started: float | None = None, timeout: float = HARD_TIMEOUT_SECONDS) -> dict[str, Any]:
    clock = asyncio.get_running_loop().time
    started = clock() if started is None else started
    body, options = source["body"], source["body"]["options"]
    lane = {512: "general", 2048: "patient", 1024: "department", 768: "department"}[options["num_predict"]]
    result: dict[str, Any] = {"request_id": source["request_id"], "source_body_sha256": source["body_sha256"],
        "lane": lane, "lane_source": "original_scope_output_budget", "original_sampling_options": dict(options),
        "success": False, "status": None, "error": None, "output_text": "",
        "hard_timeout_s": timeout, "target_s": TARGET_SECONDS[lane]}
    terminal = None
    try:
        # The shared start includes client scheduling/connection waits. A
        # continuous token stream cannot evade this whole-request deadline.
        async with asyncio.timeout_at(started + timeout):
            async with client.stream("POST", origin + "/api/chat", content=canonical(body),
                    headers={"Content-Type": "application/json"}, timeout=timeout) as response:
                result["status"] = response.status_code
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if terminal is not None:
                        raise ValueError("Data after terminal response")
                    data = json.loads(line)
                    if not isinstance(data, dict) or data.get("error") is not None:
                        raise ValueError("Engine returned an error")
                    if data.get("cache_hit") is True or data.get("cached") is True:
                        raise ValueError("A fresh replay cannot accept an answer-cache hit")
                    message = data.get("message", {})
                    if (not isinstance(message, dict) or not isinstance(message.get("content", ""), str)
                            or message.get("role", "assistant") != "assistant"
                            or set(message) - {"role", "content"}):
                        raise TypeError("Invalid text stream")
                    text = message.get("content", "")
                    result["output_text"] += text
                    if text and "first_raw_content_s" not in result:
                        result["first_raw_content_s"] = clock() - started
                    if data.get("done") is True:
                        terminal = {key: data.get(key) for key in
                                    ("done_reason", "model", "prompt_eval_count", "eval_count")}
                        result["terminal"] = terminal
            if terminal is None:
                result["error"] = "missing_terminal"
            elif terminal["done_reason"] == "length":
                result["error"] = "truncated"
            elif terminal["done_reason"] != "stop":
                result["error"] = "not_normal_stop"
            elif terminal["model"] != MODEL:
                result["error"] = "model_mismatch"
            elif (type(terminal["prompt_eval_count"]) is not int or terminal["prompt_eval_count"] <= 0
                    or type(terminal["eval_count"]) is not int or not 0 < terminal["eval_count"] <= options["num_predict"]):
                result["error"] = "invalid_token_counts"
            elif terminal["prompt_eval_count"] + options["num_predict"] > 8192:
                result["error"] = "context_budget_exceeded"
            elif not result["output_text"].strip():
                result["error"] = "empty_answer"
            else:
                result["success"] = True
    except (TimeoutError, httpx.TimeoutException):
        result["error"] = "timeout"
    except httpx.HTTPStatusError:
        result["error"] = f"http_{result['status']}"
    except httpx.HTTPError:
        result["error"] = "transport_error"
    except (ValueError, TypeError, KeyError):
        result["error"] = "invalid_response_protocol"
    finally:
        result["full_s"] = clock() - started
        result["completed_within_target"] = bool(result["success"] and result["full_s"] < result["target_s"])
        result["answer_sha256"] = hashlib.sha256(result["output_text"].encode()).hexdigest()
    return result


async def replay_200(origin: str, capture_path: Path,
                     result_observer: Callable[[dict[str, Any]], object] | None = None, *,
                     transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    origin = owned_origin(origin)
    selected, provenance = original_inputs(capture_path)
    if result_observer is not None and (not callable(result_observer)
            or any(inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target)
                   for target in (result_observer, type(result_observer).__call__))):
        raise TypeError("Result observer must be a synchronous, promptly returning callback")
    gate = asyncio.Event()
    limits = httpx.Limits(max_connections=REQUESTS, max_keepalive_connections=REQUESTS)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, limits=limits, transport=transport) as client:
        started = asyncio.get_running_loop().time()

        async def worker(source: dict[str, Any]) -> dict[str, Any]:
            await gate.wait()
            row = await execute_one(client, origin, source, started=started)
            if result_observer is not None:
                observed = result_observer(dict(row))
                if observed is not None:
                    if inspect.iscoroutine(observed):
                        observed.close()
                    raise TypeError("Result observer must return None without asynchronous work")
            return row

        tasks = [asyncio.create_task(worker(source)) for source in selected]
        gate.set()
        try:
            rows = await asyncio.gather(*tasks)
            wall = asyncio.get_running_loop().time() - started
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    lanes = {}
    for lane in TARGET_SECONDS:
        lane_rows = [row for row in rows if row["lane"] == lane]
        lanes[lane] = {"requests": len(lane_rows), "successful": sum(row["success"] for row in lane_rows),
            "full_meeting_target": sum(row["completed_within_target"] for row in lane_rows),
            "target_s": TARGET_SECONDS[lane],
            "successful_full_http": distribution([row["full_s"] for row in lane_rows if row["success"]])}
    successful = sum(row["success"] for row in rows)
    passed = len(rows) == REQUESTS and all(row["completed_within_target"] for row in rows)
    return {"scope": "engine_only_200_exact_captured_bodies_without_live_retrieval_not_combined_Orb_capacity",
        "planned_requests": REQUESTS, "concurrency": REQUESTS, "hard_request_timeout_s": HARD_TIMEOUT_SECONDS,
        "requests": rows, "capture_provenance": provenance,
        "summary": {"requests": len(rows), "successful": successful, "failed": len(rows) - successful,
            "failures": dict(Counter(row["error"] or "unknown" for row in rows if not row["success"])),
            "wall_s": wall, "lanes": lanes,
            "quantiles_scope": "Successful complete HTTP responses only; all failures remain in target denominators"},
        "engine_only_200_targets_demonstrated": passed, "combined_Orb_200_targets_demonstrated": False,
        "clinical_equivalence_qualified": False, "production_purchase_gate_passed": False}
