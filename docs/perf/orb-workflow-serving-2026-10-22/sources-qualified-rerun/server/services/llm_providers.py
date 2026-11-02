"""Orb — AI Provider Layer (local inference only)

One thin async seam through which every chat-shaped LLM call flows, so the
backend can be swapped without touching business logic:

  * OllamaChatProvider — the local MedGemma lane. Identical wire behavior to
    the previous inline httpx code in sage_service.query_ollama. This is the
    offline/PHI engine and the ONLY lane — every token is generated on the
    box, so no prompt or completion ever crosses the network boundary.

Callers acquire llm_queue before streaming. The default serving lane stays
conservative; an explicitly qualified worker pool routes each task to its own
bounded local worker without changing shared provider objects.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

import httpx

from config import (
    LLM_MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
)
from services.local_inference_routing import (
    inference_url,
    record_worker_status,
    record_worker_transport_failure,
)
from services.local_model_http import local_model_ssl_context

logger = logging.getLogger("orb.llm_providers")


def _log_completion_metrics(model: str, data: dict) -> None:
    """Log counts only; optional engine diagnostics must not break an answer.

    Ollama 0.35.1 reports cached + evaluated tokens in prompt_eval_count.
    Its separate cached counter is authoritative; character estimates cannot
    distinguish a reused prefix from a full prefill. Older engines that omit
    that counter leave the evaluated/cache counts explicitly unknown.
    """

    def count(value):
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None

    def milliseconds(value):
        if (
            type(value) not in (int, float)
            or not 0 <= value <= 2**63 - 1
            or not math.isfinite(value)
        ):
            return 0.0
        return value / 1e6

    total = count(data.get("prompt_eval_count"))
    cached = count(data.get("prompt_eval_cached_count"))
    if total is None or cached is not None and cached > total:
        cached = None
    evaluated = total - cached if total is not None and cached is not None else None
    prefill_ms = milliseconds(data.get("prompt_eval_duration"))
    decoded = count(data.get("eval_count"))
    decode_ms = milliseconds(data.get("eval_duration"))
    prefill_rate = (
        f"{evaluated / prefill_ms * 1000:.0f}"
        if evaluated is not None and prefill_ms
        else "unknown"
    )
    decode_rate = (
        f"{decoded / decode_ms * 1000:.1f}" if decoded is not None and decode_ms else "unknown"
    )
    logger.info(
        "llm split model=%s prefill=%s tok in %.0f ms (%s tok/s) "
        "decode=%s tok in %.0f ms (%s tok/s) "
        "prompt_total=%s tok, prompt_cached=%s tok",
        model,
        evaluated if evaluated is not None else "unknown",
        prefill_ms,
        prefill_rate,
        decoded if decoded is not None else "unknown",
        decode_ms,
        decode_rate,
        total if total is not None else "unknown",
        cached if cached is not None else "unknown",
    )


class LocalModelStreamError(RuntimeError):
    """A local engine response did not complete successfully; contains no prompt data."""


def _stream_read_timeout() -> float:
    """Seconds to wait for the NEXT byte of a stream before calling it dead.

    THIS NUMBER IS A LANE BUDGET, NOT A NETWORK SETTING, and it was 300.0
    (2026-08-15). httpx `read` bounds the gap BETWEEN reads, so on a streaming
    response it is a stall detector — and every Ollama stream in this process
    runs while holding the ONE background lane slot (`llm_queue`,
    max_concurrent=1). So the read timeout is the real ceiling on how long a
    wedged engine can black out the lane, whatever budget the caller thinks it
    set. At 300.0 a single silent stream took the lane away from Sage's alert
    advice for five minutes; three of those were measured on 2026-08-14/15, one
    of them covering an entire five-minute quiet window, and every draft that
    arrived during them resolved to `queue_timeout` with an EMPTY queue. That is
    the failure the delivery-rate work could not explain from scheduling alone.

    60 s IS SIZED ON MEASURED BEHAVIOUR, not picked round. Two gaps matter:
      * time to FIRST byte — prefill. Measured 3.4-12.7 s idle on medgemma:27b
        and 10.2-29.6 s under an alert storm. 60 s is ~2x the worst observed.
      * time BETWEEN tokens — decode. 10-15 tok/s, so ~70-100 ms. 60 s is
        several hundred times a healthy gap.
    Anything past that is not a slow answer, it is a stopped one, and the right
    thing to do with a stopped one is give the lane back.

    Raising this trades Tier-2 delivery for the tail of a very slow cold load;
    prefer fixing residency (the tiers are pinned with keep_alive=-1) over
    widening it.
    """
    try:
        return max(1.0, float(os.getenv("ORB_LLM_STREAM_READ_TIMEOUT", "60")))
    except (TypeError, ValueError):
        return 60.0


class ChatProvider(ABC):
    """Minimal async chat interface: messages in, text chunks out."""

    name: str = "abstract"

    @abstractmethod
    def astream_chat(
        self, messages: list[dict], options: dict | None = None
    ) -> AsyncGenerator[str, None]:
        """Stream response text chunks for a chat-formatted message list.
        `options` carries sampling params (temperature, num_predict/max_tokens,
        num_ctx) in Ollama naming; providers map them to their own API."""
        ...


class OllamaChatProvider(ChatProvider):
    """Local Ollama /api/chat streaming — the offline/PHI engine."""

    name = "ollama"

    def __init__(self, base_url: str = OLLAMA_BASE_URL, model: str = LLM_MODEL):
        self.base_url = base_url
        self.model = model

    async def astream_chat(
        self, messages: list[dict], options: dict | None = None
    ) -> AsyncGenerator[str, None]:
        # ORB_MODELS_ENABLED off (or the Orb Ward Record): refused before any
        # request leaves this process (services/model_switch.py).
        from services.model_switch import refuse_when_models_off

        refuse_when_models_off("Sage chat (OllamaChatProvider)")
        opts = options or {}
        # first_token vs the client's first rendered chunk is the whole question:
        # the 70-turn eval showed 9.3 s mean prefill but 35 s median to first
        # words, so ~26 s is spent somewhere downstream of generation starting.
        _t0 = time.monotonic()
        _first_tok_at = None
        try:
            async with httpx.AsyncClient(verify=local_model_ssl_context(), 
                timeout=httpx.Timeout(connect=30.0, read=_stream_read_timeout(), write=30.0, pool=30.0),
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    inference_url("/api/chat", model=self.model, base_url=self.base_url),
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": True,
                        "keep_alive": OLLAMA_KEEP_ALIVE,
                        "options": {
                            "temperature": opts.get("temperature", 0.4),
                            "repeat_penalty": opts.get("repeat_penalty", 1.15),
                            "repeat_last_n": opts.get("repeat_last_n", 256),
                            "num_predict": opts.get("num_predict", 2048),
                            "num_ctx": opts.get("num_ctx", 8192),
                        },
                    },
                ) as response:
                    if response.status_code >= 400:
                        record_worker_status(response.status_code)
                    completed = False
                    meaningful_content = False
                    try:
                        if response.is_error:
                            raise LocalModelStreamError("Local model request failed.")
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError as exc:
                                raise LocalModelStreamError(
                                    "Local model returned an invalid stream."
                                ) from exc
                            if not isinstance(data, dict) or data.get("error") is not None:
                                raise LocalModelStreamError("Local model returned an invalid stream.")
                            message = data.get("message")
                            if message is not None and (
                                not isinstance(message, dict)
                                or not isinstance(message.get("content"), str)
                            ):
                                raise LocalModelStreamError("Local model returned an invalid stream.")
                            content = message.get("content", "") if message else ""
                            if content:
                                meaningful_content = meaningful_content or bool(content.strip())
                                if _first_tok_at is None:
                                    _first_tok_at = time.monotonic()
                                    logger.info(
                                        "llm first_token model=%s at %.0f ms after request",
                                        self.model,
                                        (_first_tok_at - _t0) * 1000,
                                    )
                                yield content
                            if data.get("done") is True:
                                if data.get("done_reason") == "length":
                                    raise LocalModelStreamError(
                                        "Local model answer exceeded its response budget."
                                    )
                                completed = True
                                record_worker_status(response.status_code)
                                # Prefill/decode split, straight from Ollama's own
                                # counters. Counts and durations only — the messages
                                # carry PHI and must never reach a log line.
                                _log_completion_metrics(self.model, data)
                                break
                        if not completed or not meaningful_content:
                            raise LocalModelStreamError("Local model did not complete an answer.")
                    except GeneratorExit:
                        logger.info("Ollama stream cancelled — closing httpx connection")
                    finally:
                        await response.aclose()
        except httpx.TransportError:
            record_worker_transport_failure()
            raise


# Module-level singleton (cheap, stateless besides config).
local_provider = OllamaChatProvider()

# ── Per-model provider handles ───────────────────────────────────────────────
# Tiered routing (residency fallback, and now thinking mode) needs a provider
# bound to a model name chosen PER TURN. Callers used to write
# `OllamaChatProvider(model=choice.model)` inline, which is correct but builds a
# fresh object on every downgraded turn and, more importantly, means the
# singleton above is NOT returned for the primary tier — so a test that patches
# `local_provider.astream_chat` silently stops covering the routed path.
#
# `provider_for_model` fixes both: the singleton is returned for its own model,
# and every other tier gets one cached handle. The cache is unbounded on purpose
# — its keys are Ollama tag names from config, a set of size ~2, never user input.
_provider_cache: dict[str, OllamaChatProvider] = {}


def provider_for_model(model: str | None) -> OllamaChatProvider:
    """The local provider that speaks to `model`.

    Returns the module singleton when `model` is the configured primary tier (or
    is falsy), so patching `local_provider` in tests still intercepts the call.
    """
    if not model or model == local_provider.model:
        return local_provider
    provider = _provider_cache.get(model)
    if provider is None:
        provider = OllamaChatProvider(model=model)
        _provider_cache[model] = provider
    return provider
