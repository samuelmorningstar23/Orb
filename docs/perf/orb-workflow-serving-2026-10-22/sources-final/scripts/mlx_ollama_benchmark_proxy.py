#!/usr/bin/env python3
"""Synthetic-only, loopback benchmark bridge; never a production provider.

Ollama mode captures and forwards the original request bytes unchanged. MLX
mode translates the explicitly supported text-chat contract to MLX-LM 0.32.2's
OpenAI endpoint. A verified local sampling/template policy is mandatory: native
MLX parameter names alone do not establish Ollama sampling-order equivalence.
No model, tokenizer or network listener is loaded by importing this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

MODEL = "medgemma:27b"
WEIGHT_DIGEST = "sha256:4b97da6b21a8fb1405a0d1efee462bfd6c4d3c46307bac8b34373835f07ca758"
MLX_VERSION = "0.32.2"
WARMUP = "Orb synthetic serving qualification warmup."
MAX_BODY_BYTES = 2 * 1024 * 1024
READ_TIMEOUT = 60.0


class ProxyRefused(ValueError):
    """A benchmark contract cannot be preserved; contains no clinical text."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def local_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProxyRefused("Use an explicit owned loopback HTTP origin") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None or not 1024 <= port <= 65535 or port == 11434
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ProxyRefused("Use an explicit owned loopback HTTP origin other than port11434")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{port}"


def load_policy(path: Path, *, allow_unqualified: bool = False) -> dict[str, Any]:
    """Verify locally supplied evidence files; do not infer clinical validation."""
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 65536:
        raise ProxyRefused("MLX requires an existing absolute local policy JSON")
    try:
        policy = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise ProxyRefused("MLX policy is invalid") from exc
    if (not isinstance(policy, dict) or policy.get("mlx_lm_version") != MLX_VERSION
            or policy.get("source_ollama_weight_digest") != WEIGHT_DIGEST
            or not isinstance(policy.get("conversion_label"), str)
            or not policy["conversion_label"].strip()
            or policy.get("context_policy") != "reject_input_plus_output_over_8192"):
        raise ProxyRefused("MLX checkpoint, conversion, sampling, template or context parity is unverified")
    if (not allow_unqualified and (policy.get("sampling_parity_verified") is not True
                                  or policy.get("template_parity_verified") is not True)):
        raise ProxyRefused("Native MLX sampling/template parity is unverified; explicit diagnostic override required")
    for name in ("sampling_evidence", "template_evidence"):
        if allow_unqualified and policy.get(name.replace("_evidence", "_parity_verified")) is not True:
            continue
        evidence = policy.get(name)
        if not isinstance(evidence, dict):
            raise ProxyRefused("MLX parity evidence is missing")
        file = Path(str(evidence.get("path", "")))
        if (not file.is_absolute() or not file.is_file() or file.stat().st_size > 2 * 1024 * 1024
                or hashlib.sha256(file.read_bytes()).hexdigest() != evidence.get("sha256")):
            raise ProxyRefused("MLX parity evidence is absent or changed")
    return {**policy, "policy_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@dataclass(frozen=True)
class PromptProof:
    rendered_prompt: str
    token_ids: list[int]


def tokenizer_encoder(directory: Path) -> Callable[[list[dict[str, str]]], PromptProof]:
    """Load local tokenizer metadata only; never weights, remote code or hub data."""
    if not directory.is_absolute() or not directory.is_dir():
        raise ProxyRefused("MLX model/tokenizer directory must already exist locally")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(directory), local_files_only=True,
                                              trust_remote_code=False)
    if not tokenizer.chat_template:
        raise ProxyRefused("MLX tokenizer must have the verified explicit chat template")

    def encode(messages: list[dict[str, str]]) -> PromptProof:
        rendered = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        # Transformers5 defaults to a BatchEncoding dictionary here. Iterating
        # that would hash field names, not the model's token IDs.
        tokens = tokenizer.apply_chat_template(messages, tokenize=True, return_dict=False,
                                               add_generation_prompt=True)
        if (not isinstance(rendered, str) or not isinstance(tokens, list) or not tokens
                or any(type(token) is not int or token < 0 for token in tokens)):
            raise ProxyRefused("Local tokenizer did not return a flat sequence of integer IDs")
        return PromptProof(rendered, list(tokens))

    return encode


def validate_request(body: object, *, generate: bool = False) -> dict[str, Any]:
    if not isinstance(body, dict) or body.get("model") != MODEL:
        raise ProxyRefused("Only the pinned MedGemma27B benchmark model is supported")
    allowed = {"model", "stream", "keep_alive", "options", "prompt" if generate else "messages"}
    if set(body) - allowed or type(body.get("stream", True)) is not bool:
        raise ProxyRefused("Unsupported benchmark request fields")
    if body.get("keep_alive", "-1") not in {"-1", -1, "30m", "1h"}:
        raise ProxyRefused("Benchmark model must remain resident for its owned lifetime")
    options = body.get("options", {})
    if not isinstance(options, dict):
        raise ProxyRefused("Invalid benchmark options")
    if generate:
        if (body.get("prompt") != WARMUP or body.get("stream") is not False
                or options != {"num_ctx": 8192, "num_predict": 1}):
            raise ProxyRefused("Generate endpoint is restricted to the existing one-token synthetic warmup")
    else:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ProxyRefused("Text chat messages are required")
        non_system_seen = False
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] not in {"system", "user", "assistant"}
                    or not isinstance(message["content"], str)
                    or (message["role"] == "system" and non_system_seen)):
                raise ProxyRefused("Only text-only clinical chat roles are supported")
            non_system_seen = non_system_seen or message["role"] != "system"
        if messages[-1]["role"] != "user":
            raise ProxyRefused("Benchmark chat must end in the user turn")
    return body


def mlx_body(body: dict[str, Any], *, generate: bool = False) -> dict[str, Any]:
    """Map supported values explicitly; reject any silently ignored option."""
    options = body.get("options", {})
    known = {"num_ctx", "num_predict", "temperature", "repeat_penalty", "repeat_last_n",
             "top_k", "top_p", "min_p", "seed", "stop"}
    if set(options) - known:
        raise ProxyRefused("MLX cannot preserve an unsupported Ollama option")
    def number(name: str, default: float, minimum: float, maximum: float) -> float:
        value = options.get(name, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ProxyRefused("Invalid benchmark numeric option")
        return float(value)

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        value = options.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ProxyRefused("Invalid benchmark integer option")
        return value

    if integer("num_ctx", 8192, 8192, 8192) != 8192:
        raise ProxyRefused("Clinical context must remain8192")
    maximum = integer("num_predict", 2048, 1, 2048)
    if not generate and maximum not in {512, 768, 1024, 2048}:
        raise ProxyRefused("Clinical output budget must match an existing scope")
    stop = options.get("stop", ["<end_of_turn>"])
    if stop != ["<end_of_turn>"]:
        raise ProxyRefused("The model's original end-of-turn stop must be preserved")
    result: dict[str, Any] = {
        "model": "default_model", "messages" if not generate else "prompt": body["messages" if not generate else "prompt"],
        "stream": body.get("stream", True), "max_tokens": maximum,
        "temperature": number("temperature", .4, 0, 2),
        "top_k": integer("top_k", 40, 40, 40), "top_p": number("top_p", .9, .9, .9),
        "min_p": number("min_p", 0, 0, 0),
        "repetition_penalty": number("repeat_penalty", 1.15, 1.15, 1.15),
        "repetition_context_size": integer("repeat_last_n", 256, 256, 256),
        "presence_penalty": 0.0, "frequency_penalty": 0.0,
        "xtc_probability": 0.0, "num_draft_tokens": 0, "stop": ["<end_of_turn>"],
    }
    if "seed" in options:
        result["seed"] = integer("seed", 0, 0, 2**32 - 1)
    if result["stream"]:
        result["stream_options"] = {"include_usage": True}
    return result


class Captures:
    def __init__(self, directory: Path):
        if (not directory.is_absolute() or directory.exists() or directory.is_symlink()
                or not directory.parent.is_dir()):
            raise ProxyRefused("Capture directory must be a new absolute local directory")
        directory.mkdir(mode=0o700)
        self.directory = directory

    def write(self, name: str, data: dict[str, Any]) -> None:
        file = self.directory / name
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical(data) + b"\n")

    def append_request(self, row: dict[str, Any]) -> None:
        # Each write runs synchronously on this one process's event-loop
        # thread. The capture overhead remains part of benchmark latency.
        descriptor = os.open(self.directory / "requests.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "ab") as output:
            output.write(canonical(row) + b"\n")


def usage_fields(data: dict[str, Any]) -> dict[str, int]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise ProxyRefused("MLX did not supply final token usage")
    fields = {}
    for old, new in (("prompt_tokens", "prompt_eval_count"), ("completion_tokens", "eval_count")):
        value = usage.get(old)
        if type(value) is not int or value < 0:
            raise ProxyRefused("MLX final token usage is invalid")
        fields[new] = value
    details = usage.get("prompt_tokens_details", {})
    if not isinstance(details, dict):
        raise ProxyRefused("MLX cached token usage is invalid")
    cached = details.get("cached_tokens")
    if type(cached) is int and 0 <= cached <= fields["prompt_eval_count"]:
        fields["prompt_eval_cached_count"] = cached
    return fields


def content_from_choice(data: dict[str, Any], *, streaming: bool) -> tuple[str, str | None]:
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProxyRefused("MLX returned an invalid choice")
    choice = choices[0]
    message = choice.get("delta" if streaming else "message")
    if (not isinstance(message, dict) or set(message) - {"role", "content"}
            or message.get("role", "assistant") != "assistant"
            or not isinstance(message.get("content", ""), str)):
        raise ProxyRefused("MLX returned unsupported reasoning, tools or message content")
    finish = choice.get("finish_reason")
    if finish not in {None, "stop", "length"}:
        raise ProxyRefused("MLX did not finish a normal text answer")
    return message.get("content", ""), finish


def create_app(*, backend: str, upstream: str, capture_dir: Path,
               synthetic_data_only: bool, model_dir: Path | None = None,
               mlx_policy_path: Path | None = None,
               allow_unqualified_mlx: bool = False,
               prompt_encoder: Callable[[list[dict[str, str]]], PromptProof] | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    if synthetic_data_only is not True or backend not in {"ollama", "mlx"}:
        raise ProxyRefused("An explicit synthetic-data-only benchmark attestation is mandatory")
    origin = local_origin(upstream)
    policy = None
    if backend == "mlx":
        if mlx_policy_path is None:
            raise ProxyRefused("MLX requires verified sampling/template policy evidence")
        policy = load_policy(mlx_policy_path, allow_unqualified=allow_unqualified_mlx)
        if prompt_encoder is None:
            if model_dir is None:
                raise ProxyRefused("MLX requires the installed local tokenizer directory")
            prompt_encoder = tokenizer_encoder(model_dir)
    capture = Captures(capture_dir)
    metadata = {"benchmark_only": True, "synthetic_data_only": True, "backend": backend,
                "transport": "ollama_compatible" if backend == "mlx" else "unchanged_ollama_passthrough",
                "model": MODEL, "mlx_policy": policy, "clinical_validation": "not_established_by_this_proxy",
                "unqualified_mlx_diagnostic": backend == "mlx" and allow_unqualified_mlx,
                "upstream": origin, "model_loaded_by_proxy_import": False}
    capture.write("proxy-metadata.json", metadata)
    app = FastAPI()
    app.state.loaded = False
    app.state.metadata = metadata

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(connect=2, read=READ_TIMEOUT, write=30, pool=2))

    @app.middleware("http")
    async def loopback_only(request: Request, call_next: Any) -> Response:
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            return JSONResponse({"error": "Benchmark proxy accepts loopback clients only"}, status_code=403)
        return await call_next(request)

    @app.get("/__benchmark__")
    async def benchmark() -> dict[str, Any]:
        return {**metadata, "completed_model_request_observed": app.state.loaded}

    @app.get("/api/ps")
    @app.get("/api/tags")
    async def models(request: Request) -> Response:
        try:
            async with client() as http:
                if backend == "ollama":
                    response = await http.get(origin + request.url.path)
                    return Response(response.content, status_code=response.status_code, media_type="application/json")
                # Native /health only proves the generation thread is alive,
                # not that weights are resident. Report residency only after a
                # real completion, and withdraw it if health is lost.
                health = await http.get(origin + "/health")
                payload = health.json()
                healthy = health.status_code == 200 and isinstance(payload, dict) and payload.get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            app.state.loaded = False
            return JSONResponse({"error": "Owned upstream residency is unknown"}, status_code=503)
        if not healthy:
            app.state.loaded = False
        assert policy is not None  # Ollama passthrough returned above.
        return JSONResponse({"models": [{"name": MODEL, "model": MODEL,
            "digest": "mlx-candidate:" + policy["policy_sha256"],
            "source_ollama_weight_digest": WEIGHT_DIGEST,
            "backend": "mlx", "conversion_label": policy["conversion_label"]}]
            if healthy and app.state.loaded else []})

    @app.get("/api/version")
    async def version() -> Response:
        if backend == "ollama":
            async with client() as http:
                response = await http.get(origin + "/api/version")
                return Response(response.content, status_code=response.status_code, media_type="application/json")
        return JSONResponse({"version": f"mlx-lm-{MLX_VERSION}-benchmark-proxy", "backend": "mlx"})

    async def infer(request: Request, *, generate: bool) -> Response:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"error": "Benchmark request exceeds byte limit"}, status_code=413)
        try:
            body = validate_request(json.loads(raw), generate=generate)
            mapped = mlx_body(body, generate=generate) if backend == "mlx" else None
            proof = prompt_encoder(body["messages"]) if prompt_encoder is not None and not generate else None
            if proof is not None and (not isinstance(proof, PromptProof)
                    or not proof.token_ids or any(type(token) is not int or token < 0 for token in proof.token_ids)):
                raise ProxyRefused("Tokenizer did not provide valid local prompt proof")
            if proof is not None and len(proof.token_ids) + body.get("options", {}).get("num_predict", 2048) > 8192:
                raise ProxyRefused("Full input plus unchanged output budget exceeds8192; no prompt was truncated")
        except (ValueError, TypeError, KeyError, OverflowError):
            return JSONResponse({"error": "Benchmark request or strict mapping refused"}, status_code=400)
        identifier = uuid.uuid4().hex
        record = {"synthetic_data_only": True, "backend": backend, "request_path": request.url.path,
            "raw_request_sha256": hashlib.sha256(raw).hexdigest(), "request_json_sha256": digest(body),
            "messages_sha256": digest(body.get("messages")), "options_sha256": digest(body.get("options", {})),
            "request": body, "mapped_request": mapped,
            "rendered_prompt": proof.rendered_prompt if proof else None,
            "rendered_prompt_sha256": hashlib.sha256(proof.rendered_prompt.encode()).hexdigest() if proof else None,
            "prompt_token_ids": proof.token_ids if proof else None,
            "prompt_token_ids_sha256": digest(proof.token_ids) if proof else None}
        capture.write(identifier + "-request.json", record)
        capture.append_request({"request_id": identifier, "endpoint": request.url.path,
            "body": body, "body_sha256": record["request_json_sha256"], "backend": backend,
            "rendered_prompt_sha256": record["rendered_prompt_sha256"],
            "prompt_token_ids_sha256": record["prompt_token_ids_sha256"]})
        started = time.perf_counter()
        http = client()
        path = request.url.path if backend == "ollama" else "/v1/completions" if generate else "/v1/chat/completions"
        try:
            upstream_request = http.build_request("POST", origin + path, content=raw if mapped is None else canonical(mapped),
                                                 headers={"Content-Type": "application/json"})
            upstream_response = await http.send(upstream_request, stream=True)
        except httpx.HTTPError:
            await http.aclose()
            return JSONResponse({"error": "Owned benchmark upstream unavailable"}, status_code=502)
        if upstream_response.status_code != 200:
            status = upstream_response.status_code
            await upstream_response.aclose()
            await http.aclose()
            return JSONResponse({"error": "Owned benchmark upstream refused the request"}, status_code=status)
        if backend == "ollama":
            async def unchanged():
                try:
                    async for chunk in upstream_response.aiter_bytes():
                        yield chunk
                finally:
                    await upstream_response.aclose()
                    await http.aclose()
            return StreamingResponse(unchanged(), media_type=upstream_response.headers.get("content-type", "application/x-ndjson"))
        if not body.get("stream", True):
            try:
                data = json.loads(await upstream_response.aread())
                if not isinstance(data, dict):
                    raise ProxyRefused("MLX did not return an object")
                if generate:
                    choices = data.get("choices", [])
                    if len(choices) != 1 or choices[0].get("finish_reason") not in {"stop", "length"}:
                        raise ProxyRefused("MLX warmup did not finish")
                    text, reason = choices[0].get("text", ""), choices[0]["finish_reason"]
                else:
                    text, reason = content_from_choice(data, streaming=False)
                if reason is None or not isinstance(text, str):
                    raise ProxyRefused("MLX did not return a terminal text answer")
                usage = usage_fields(data)
                if proof is not None and usage["prompt_eval_count"] != len(proof.token_ids):
                    raise ProxyRefused("Native and captured tokenizer counts differ")
                app.state.loaded = True
                return JSONResponse({"model": MODEL, "message": {"role": "assistant", "content": text},
                    "response": text, "done": True, "done_reason": reason, **usage, "backend": "mlx"})
            except (ValueError, KeyError, TypeError, httpx.HTTPError):
                return JSONResponse({"error": "Invalid or incomplete MLX result"}, status_code=502)
            finally:
                await upstream_response.aclose()
                await http.aclose()

        async def translated():
            from loadtest_capacity import SSEDecoder
            decoder, reason, usage, acknowledged = SSEDecoder(), None, None, False
            text_parts: list[str] = []
            outcome = "incomplete"
            try:
                async for chunk in upstream_response.aiter_bytes():
                    for _event, event in decoder.feed(chunk):
                        if acknowledged:
                            raise ProxyRefused("MLX sent content after stream acknowledgement")
                        if event == "[DONE]":
                            acknowledged = True
                            continue
                        data = json.loads(event)
                        if not isinstance(data, dict) or data.get("error"):
                            raise ProxyRefused("MLX stream failed")
                        if data.get("choices") == [] and data.get("usage") is not None:
                            if usage is not None:
                                raise ProxyRefused("MLX repeated final usage")
                            usage = usage_fields(data)
                            continue
                        if reason is not None:
                            raise ProxyRefused("MLX sent another choice after finishing")
                        text, finish = content_from_choice(data, streaming=True)
                        if text:
                            text_parts.append(text)
                            yield canonical({"model": MODEL, "message": {"role": "assistant", "content": text},
                                             "done": False, "backend": "mlx"}) + b"\n"
                        if finish is not None:
                            reason = finish
                decoder.feed(b"", final=True)
                if not acknowledged or reason is None or usage is None or not "".join(text_parts).strip():
                    raise ProxyRefused("MLX stream was incomplete")
                if proof is not None and usage["prompt_eval_count"] != len(proof.token_ids):
                    raise ProxyRefused("Native and captured tokenizer counts differ")
                app.state.loaded = True
                outcome = reason
                yield canonical({"model": MODEL, "message": {"role": "assistant", "content": ""},
                    "done": True, "done_reason": reason, **usage, "backend": "mlx"}) + b"\n"
            except (ValueError, TypeError, KeyError, httpx.HTTPError):
                outcome = "error"
                yield canonical({"error": "Owned MLX stream failed strict benchmark validation", "backend": "mlx"}) + b"\n"
            finally:
                await upstream_response.aclose()
                await http.aclose()
                capture.write(identifier + "-result.json", {"synthetic_data_only": True, "backend": "mlx",
                    "outcome": outcome, "full_proxy_wall_s": time.perf_counter() - started,
                    "usage": usage, "answer_sha256": hashlib.sha256("".join(text_parts).encode()).hexdigest()})
        return StreamingResponse(translated(), media_type="application/x-ndjson")

    @app.post("/api/chat")
    async def chat(request: Request) -> Response:
        return await infer(request, generate=False)

    @app.post("/api/generate")
    async def generate(request: Request) -> Response:
        return await infer(request, generate=True)

    return app


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--backend", choices=("ollama", "mlx"), required=True)
    result.add_argument("--upstream", required=True)
    result.add_argument("--port", type=int, required=True)
    result.add_argument("--capture-dir", type=Path, required=True)
    result.add_argument("--synthetic-data-only", action="store_true")
    result.add_argument("--model-dir", type=Path)
    result.add_argument("--mlx-policy", type=Path)
    result.add_argument("--allow-unqualified-mlx", action="store_true",
                        help="Diagnostic measurements only; explicitly disclose unresolved native sampler/template parity")
    return result


def main() -> int:
    args = parser().parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 11434:
        raise ProxyRefused("Use a separate unprivileged owned proxy port")
    app = create_app(backend=args.backend, upstream=args.upstream, capture_dir=args.capture_dir,
        synthetic_data_only=args.synthetic_data_only, model_dir=args.model_dir, mlx_policy_path=args.mlx_policy,
        allow_unqualified_mlx=args.allow_unqualified_mlx)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
