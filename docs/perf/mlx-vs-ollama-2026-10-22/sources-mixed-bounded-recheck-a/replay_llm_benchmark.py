"""Replay three captured synthetic Sage inputs exactly; engine timing only."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from ollama_template_oracle import MODEL, canonical, captured_chats, owned_origin


async def replay(origin: str, capture_path: Path, observer: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    owned_origin(origin)
    recorded, provenance = captured_chats(capture_path)
    selected = [row for row in recorded if row["body"].get("options", {}).get("temperature") != 0][:3]
    if len(selected) != 3:
        raise ValueError("Need three captured original non-greedy Sage requests")
    rows = []
    async with httpx.AsyncClient(trust_env=False) as client:
        for source in selected:
            result: dict[str, Any] = {"source_body_sha256": source["body_sha256"], "success": False,
                "original_sampling_options": source["body"].get("options", {}), "output_text": ""}
            rows.append(result)
            started = asyncio.get_running_loop().time()
            terminals = 0
            try:
                async with asyncio.timeout(60):
                    async with client.stream("POST", origin + "/api/chat", content=canonical(source["body"]),
                            headers={"Content-Type": "application/json"}, timeout=60) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            if terminals:
                                raise ValueError("Data after terminal event")
                            data = json.loads(line)
                            if not isinstance(data, dict) or data.get("error") is not None:
                                raise ValueError("Replayed engine error")
                            message = data.get("message", {})
                            if not isinstance(message, dict):
                                raise ValueError("Invalid message object")
                            content = message.get("content", "")
                            if not isinstance(content, str):
                                raise ValueError("Invalid text stream")
                            result["output_text"] += content
                            if content and "first_raw_content_s" not in result:
                                result["first_raw_content_s"] = asyncio.get_running_loop().time() - started
                            if data.get("done") is True:
                                terminals += 1
                                result["terminal"] = {key: data.get(key) for key in
                                    ("done_reason", "model", "prompt_eval_count", "eval_count")}
                    result["success"] = (terminals == 1 and bool(result["output_text"].strip()) and
                        result["terminal"].get("done_reason") == "stop" and
                        result["terminal"].get("model") == MODEL and
                        type(result["terminal"].get("eval_count")) is int and result["terminal"]["eval_count"] > 0 and
                        type(result["terminal"].get("prompt_eval_count")) is int and result["terminal"]["prompt_eval_count"] > 0)
            except (TimeoutError, ValueError, httpx.HTTPError) as error:
                result["error_type"] = type(error).__name__
            finally:
                result["full_s"] = asyncio.get_running_loop().time() - started
                observer(result.copy())
    return {"requests": rows, "capture_provenance": provenance,
            "scope": "engine_only_exact_captured_bodies_not_Orb_latency_or_clinical_validation"}
