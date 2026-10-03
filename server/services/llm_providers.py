"""Orb — AI Provider Layer (local inference only)

One thin async seam through which every chat-shaped LLM call flows, so the
backend can be swapped without touching business logic:

  * OllamaChatProvider — the local MedGemma lane. Identical wire behavior to
    the previous inline httpx code in sage_service.query_ollama. This is the
    offline/PHI engine and the ONLY lane — every token is generated on the
    box, so no prompt or completion ever crosses the network boundary.

Concurrency posture: the local lane serializes through llm_queue
(max_concurrent=1) behind the process-wide GPU gate — one physical GPU, one
job at a time. Callers acquire the queue before streaming.
"""
from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import AsyncGenerator

import httpx

from config import (
    LLM_MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
)

logger = logging.getLogger("orb.llm_providers")


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
        opts = options or {}
        # ~4 chars/token is close enough to tell a cache hit from a miss; the
        # authoritative prefilled count comes back from Ollama on the done chunk.
        _prompt_tokens_est = sum(len(m.get("content") or "") for m in messages) // 4
        # first_token vs the client's first rendered chunk is the whole question:
        # the 70-turn eval showed 9.3 s mean prefill but 35 s median to first
        # words, so ~26 s is spent somewhere downstream of generation starting.
        _t0 = time.monotonic()
        _first_tok_at = None
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=_stream_read_timeout(),
                                  write=30.0, pool=30.0),
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
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
                try:
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if "message" in data and "content" in data["message"]:
                            if _first_tok_at is None:
                                _first_tok_at = time.monotonic()
                                logger.info(
                                    "llm first_token model=%s at %.0f ms after request",
                                    self.model, (_first_tok_at - _t0) * 1000,
                                )
                            yield data["message"]["content"]
                        if data.get("done", False):
                            # Prefill/decode split, straight from Ollama's own
                            # counters. Counts and durations only — the messages
                            # carry PHI and must never reach a log line.
                            _pc = data.get("prompt_eval_count")
                            if _pc:
                                _pd = (data.get("prompt_eval_duration") or 0) / 1e6
                                _ec = data.get("eval_count") or 0
                                _ed = (data.get("eval_duration") or 0) / 1e6
                                _cached = _prompt_tokens_est - _pc
                                logger.info(
                                    "llm split model=%s prefill=%d tok in %.0f ms "
                                    "(%.0f tok/s) decode=%d tok in %.0f ms (%.1f tok/s) "
                                    "prompt~%d tok, cache_reused~%d tok",
                                    self.model, _pc, _pd,
                                    (_pc / _pd * 1000) if _pd else 0,
                                    _ec, _ed, (_ec / _ed * 1000) if _ed else 0,
                                    _prompt_tokens_est,
                                    _cached if _cached > 0 else 0,
                                )
                            break
                except GeneratorExit:
                    logger.info("Ollama stream cancelled — closing httpx connection")
                finally:
                    await response.aclose()


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
