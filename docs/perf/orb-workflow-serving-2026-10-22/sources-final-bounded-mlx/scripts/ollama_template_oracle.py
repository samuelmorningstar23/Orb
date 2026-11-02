#!/usr/bin/env python3
"""Record exact Ollama 0.40.4 prompt/tokenizer oracles for owned synthetic runs.

Only explicit loopback origins other than port 11434 are accepted. This tool
does not start/stop an engine, generate answers, fetch weights or change model
configuration. /api/chat's _debug_render_only response uses Ollama's ACTUAL
selected Go/renderer or native GGUF chat-template route. /api/show exposes the
selected template; a manifest TEMPLATE alone does not establish routing.

Primary implementation references:
https://github.com/ollama/ollama/blob/v0.40.4/server/routes.go
https://github.com/ollama/ollama/blob/v0.40.4/llm/llama_server.go
https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

MODEL = "medgemma:27b"
GO_TEMPLATE_SHA256 = "f79eab3dd094a88a34ba63d44a5b8a03504786626bd2316017752f1ecb14fac8"
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024 + 8192
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUESTS = 512


class OracleRefused(ValueError):
    """The source, origin or response cannot establish an exact local oracle."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def owned_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OracleRefused("Use an explicit owned loopback HTTP origin.") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None or not 1024 <= port <= 65535 or port == 11434
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise OracleRefused("Only an owned explicit loopback HTTP port other than 11434 is permitted.")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{port}"


def runner_origin_from_log(path: Path) -> str:
    """Read only the owned log's explicit llama-server command; never scan apps."""
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise OracleRefused("Use a bounded existing absolute owned engine log.")
    ports = set()
    for line in path.read_text(errors="replace").splitlines():
        if "starting llama-server" not in line or "cmd=" not in line:
            continue
        for match in re.finditer(r"(?:^|[\s\"\[])(?:--port|-p)(?:=|\s+)([0-9]+)(?=[\s\"\]]|$)", line):
            ports.add(int(match[1]))
    if len(ports) != 1:
        raise OracleRefused("The owned engine log does not identify one unambiguous runner port.")
    return owned_origin(f"http://127.0.0.1:{ports.pop()}")


def captured_chats(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
        raise OracleRefused("Use an existing bounded absolute synthetic requests.jsonl file.")
    metadata_path = path.parent / "proxy-metadata.json"
    if not metadata_path.is_file() or metadata_path.stat().st_size > 65536:
        raise OracleRefused("The synthetic-only proxy metadata attestation is required.")
    metadata = json.loads(metadata_path.read_text())
    if (not isinstance(metadata, dict) or metadata.get("benchmark_only") is not True
            or metadata.get("synthetic_data_only") is not True or metadata.get("backend") != "ollama"):
        raise OracleRefused("Only unchanged Ollama passthrough synthetic captures are accepted.")
    before = path.stat()
    result, seen, skipped = [], set(), 0
    file_hash = hashlib.sha256()
    with path.open("rb") as stream:
        for _ in range(MAX_REQUESTS + 1):
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            file_hash.update(line)
            if len(line) > MAX_LINE_BYTES:
                raise OracleRefused("A captured request line exceeds its byte limit.")
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("backend") != "ollama":
                raise OracleRefused("A captured request has an unexpected source.")
            body = row.get("body")
            if not isinstance(body, dict) or row.get("body_sha256") != digest(body) or body.get("model") != MODEL:
                raise OracleRefused("A captured request body hash or model differs.")
            if row.get("endpoint") == "/api/generate":
                skipped += 1
                continue
            if row.get("endpoint") != "/api/chat" or set(body) - {"model", "messages", "stream", "options", "keep_alive"}:
                raise OracleRefused("Only original text chat capture fields are supported.")
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages or not isinstance(body.get("options", {}), dict):
                raise OracleRefused("Invalid captured text chat shape.")
            non_system_seen = False
            for message in messages:
                if (not isinstance(message, dict) or set(message) != {"role", "content"}
                        or message.get("role") not in {"system", "user", "assistant"}
                        or not isinstance(message.get("content"), str)
                        or (message["role"] == "system" and non_system_seen)):
                    raise OracleRefused("Only synthetic text turns and contiguous leading system messages are supported.")
                non_system_seen = non_system_seen or message["role"] != "system"
            if messages[-1]["role"] != "user" or row.get("request_id") in seen:
                raise OracleRefused("Duplicate capture IDs or a non-user final turn are not qualified.")
            identifier = row.get("request_id")
            if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
                raise OracleRefused("A capture request ID is invalid.")
            seen.add(identifier)
            result.append(row)
        else:
            raise OracleRefused("The captured request count exceeds its explicit limit.")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OracleRefused("The capture changed while being read.")
    if not result:
        raise OracleRefused("No original synthetic chat requests were captured.")
    return result, {"path": str(path), "sha256": file_hash.hexdigest(), "bytes": after.st_size,
                    "chat_requests": len(result), "skipped_generate_requests": skipped,
                    "synthetic_metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest()}


def selected_template_info(show: Any) -> dict[str, Any]:
    if not isinstance(show, dict) or show.get("remote_host") or show.get("remote_model"):
        raise OracleRefused("The oracle requires the installed local model.")
    template = show.get("template")
    if not isinstance(template, str) or not template or len(template.encode()) > 1024 * 1024:
        raise OracleRefused("Ollama did not expose a bounded selected template.")
    sha = hashlib.sha256(template.encode()).hexdigest()
    if show.get("renderer"):
        source = "ollama_renderer"
    elif sha == GO_TEMPLATE_SHA256:
        source = "pinned_ollama_go_template"
    elif "{%" in template and ("messages[" in template or " in messages" in template):
        source = "selected_gguf_jinja_template"
    else:
        source = "unclassified_selected_template"
    info = show.get("model_info", {})
    if not isinstance(info, dict):
        raise OracleRefused("Ollama model metadata is invalid.")
    return {"source": source, "selected_template": template, "selected_template_sha256": sha,
            "renderer": show.get("renderer"), "parser": show.get("parser"), "details": show.get("details"),
            "tokenizer_add_bos_token": info.get("tokenizer.ggml.add_bos_token"),
            "bos_token_id": info.get("tokenizer.ggml.bos_token_id"),
            "eos_token_id": info.get("tokenizer.ggml.eos_token_id"),
            "selection_evidence": "Ollama0.40.4 selectedModelTemplate() /api/show result; rendered prompt is separately observed"}


async def _json_request(client: httpx.AsyncClient, method: str, url: str, body: Any = None) -> Any:
    headers = {"Content-Type": "application/json"} if body is not None else None
    async with client.stream(method, url, content=canonical(body) if body is not None else None, headers=headers) as response:
        if response.status_code != 200:
            raise OracleRefused(f"The local oracle endpoint returned HTTP {response.status_code}.")
        payload = bytearray()
        async for chunk in response.aiter_bytes():
            payload.extend(chunk)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise OracleRefused("The oracle response exceeds its bounded size.")
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise OracleRefused("The local oracle endpoint did not return valid JSON.") from exc


def rendered_template(response: Any) -> str:
    if not isinstance(response, dict) or response.get("error"):
        raise OracleRefused("Ollama did not provide a valid render-only response.")
    debug = response.get("_debug_info")
    prompt = debug.get("rendered_template") if isinstance(debug, dict) else None
    if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > MAX_RESPONSE_BYTES:
        raise OracleRefused("Ollama's exact _debug_info.rendered_template field is required.")
    message = response.get("message", {})
    if (not isinstance(message, dict) or message.get("content") or message.get("thinking")
            or response.get("eval_count", 0) or response.get("prompt_eval_count", 0)
            or response.get("done_reason") in {"stop", "length"}):
        raise OracleRefused("A generated/completed answer is not a render-only oracle.")
    return prompt


def token_ids(response: Any) -> list[int]:
    ids = response.get("tokens") if isinstance(response, dict) else None
    if not isinstance(ids, list) or not ids or any(type(value) is not int or value < 0 for value in ids):
        raise OracleRefused("The native tokenizer did not return integer IDs.")
    return ids


async def collect_oracles(native_origin: str, capture_jsonl: Path, output: Path,
                          runner_origin: str | None = None, *,
                          max_requests: int = 11,
                          transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Observe a declared prefix of synthetic chats, without engine mutation."""
    origin = owned_origin(native_origin)
    runner = owned_origin(runner_origin) if runner_origin is not None else None
    captures, capture_info = captured_chats(capture_jsonl)
    if type(max_requests) is not int or not 1 <= max_requests <= MAX_REQUESTS:
        raise OracleRefused("The explicit oracle selection must contain between 1 and 512 requests.")
    selected = captures[:max_requests]
    if not output.is_absolute() or output.exists() or not output.parent.is_dir():
        raise OracleRefused("Use a new absolute local oracle output file.")
    started = time.monotonic()
    report: dict[str, Any] = {"status": "running", "benchmark_only": True, "synthetic_data_only": True,
                            "generation_performed": False, "model": MODEL, "ollama_origin": origin,
                            "runner_origin": runner, "capture": capture_info, "oracles": [],
                            "selection": {"maximum_requests": max_requests, "total_captured_chats": len(captures),
                                          "selected_count": len(selected), "unselected_count": len(captures) - len(selected),
                                          "rule": "First max_requests chat turns in capture file order; generate warmups excluded. In the planned run this covers eight fidelity cases and three Sage turns."},
                            "template_parity_verified": False, "clinical_quality_qualified": False,
                            "limitations": ["This records observed prompts/token IDs; it does not establish MLX parity without an exact comparison."]}
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=transport, timeout=5.0) as client:
            software = await _json_request(client, "GET", origin + "/api/version")
            if not isinstance(software, dict) or software.get("version") != "0.40.4":
                raise OracleRefused("The oracle pins Ollama0.40.4.")
            report["software"] = software
            selection = selected_template_info(await _json_request(client, "POST", origin + "/api/show", {"model": MODEL}))
            report["template_selection"] = selection
            for row in selected:
                if time.monotonic() - started > 90:
                    raise OracleRefused("The bounded oracle collection time budget expired.")
                body = dict(row["body"])
                body.update({"_debug_render_only": True, "stream": False})
                prompt = rendered_template(await _json_request(client, "POST", origin + "/api/chat", body))
                record: dict[str, Any] = {"request_id": row["request_id"], "source_body_sha256": row["body_sha256"],
                                         "render_only_body_sha256": digest(body), "rendered_template": prompt,
                                         "rendered_template_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                                         "selected_template_source": selection["source"], "native_token_ids": None}
                if runner is not None:
                    raw = token_ids(await _json_request(client, "POST", runner + "/tokenize",
                        {"content": prompt, "add_special": False, "parse_special": True}))
                    record["rendered_prompt_token_ids_no_added_special"] = raw
                    if selection["source"] == "selected_gguf_jinja_template":
                        effective, policy = raw, "Native chat applies the selected GGUF template; tokenize its explicit BOS with add_special=false"
                    elif selection["source"] in {"pinned_ollama_go_template", "ollama_renderer"} and selection["tokenizer_add_bos_token"] is True:
                        # completionPrompt() removes a leading BOS before llama
                        # completion tokenization adds the model's BOS once.
                        stripped = prompt.removeprefix("<bos>")
                        effective = token_ids(await _json_request(client, "POST", runner + "/tokenize",
                            {"content": stripped, "add_special": True, "parse_special": True}))
                        policy = "Ollama completionPrompt strips leading <bos>; tokenizer adds the model BOS exactly once"
                    else:
                        effective, policy = None, "Effective BOS policy is unclassified; raw tokenizer observation only"
                    record["native_token_ids"] = effective
                    record["native_token_ids_sha256"] = digest(effective) if effective is not None else None
                    record["native_prompt_tokens"] = len(effective) if effective is not None else None
                    record["effective_tokenization_policy"] = policy
                report["oracles"].append(record)
        report["status"] = "oracles_collected"
        report["all_effective_token_ids_observed"] = all(row["native_token_ids"] is not None for row in report["oracles"])
        report["all_captured_effective_token_ids_observed"] = len(selected) == len(captures) and report["all_effective_token_ids_observed"]
    except (OracleRefused, httpx.HTTPError, OSError, ValueError) as exc:
        report.update(status="failed_or_incomplete", error_type=type(exc).__name__, error=str(exc))
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(report) + b"\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-origin", required=True)
    parser.add_argument("--capture-jsonl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=11,
                        help="Observe the first N captured chats; default11 covers planned fidelity plus sequential phases")
    runner = parser.add_mutually_exclusive_group()
    runner.add_argument("--runner-origin")
    runner.add_argument("--owned-engine-log", type=Path)
    parser.add_argument("--acknowledge-owned-synthetic", action="store_true", required=True)
    args = parser.parse_args()
    try:
        runner_origin = runner_origin_from_log(args.owned_engine_log) if args.owned_engine_log else args.runner_origin
        report = asyncio.run(collect_oracles(args.native_origin, args.capture_jsonl, args.output, runner_origin,
                                             max_requests=args.max_requests))
        print(json.dumps({"status": report["status"], "oracles": len(report["oracles"]),
                          "all_effective_token_ids_observed": report.get("all_effective_token_ids_observed", False),
                          "template_parity_verified": False, "output": str(args.output)}))
        return 0 if report["status"] == "oracles_collected" else 2
    except (OracleRefused, OSError, ValueError) as exc:
        print(json.dumps({"status": "refused", "error_type": type(exc).__name__, "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
