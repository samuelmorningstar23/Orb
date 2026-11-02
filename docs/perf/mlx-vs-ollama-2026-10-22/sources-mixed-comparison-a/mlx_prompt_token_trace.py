"""Scoped, content-free observation of MLX server's actual tokenized prompt.

No MLX imports, tokenization, mutation, evaluation or generation occurs here.
The original ResponseGenerator._tokenize runs exactly once. Its original
five-tuple is returned by identity. Up to32 events describe only validated
integer-token counts/hashes, segment concatenation and the request-type enum.
This observes the server path, unlike an external tokenizer prediction.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, Self

MODULE_NAME = "mlx_lm.server"
VOCAB_SIZE = 262_208
MAX_TOKENS = 131_072
MAX_SEGMENTS = 16
MAX_EVENTS = 32


class PromptTraceRefused(ValueError):
    """Only the explicitly supplied pinned server method can be observed."""


def token_digest(tokens: list[int]) -> str:
    return hashlib.sha256(json.dumps(tokens, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _valid_tokens(value: Any) -> bool:
    return (isinstance(value, list) and len(value) <= MAX_TOKENS
            and all(type(token) is int and 0 <= token < VOCAB_SIZE for token in value))


def summarize_return(result: Any) -> dict[str, Any]:
    """Inspect only flat Python token lists; never evaluate/repr an array/text."""
    event: dict[str, Any] = {"returned_five_tuple": isinstance(result, tuple) and len(result) == 5,
                             "token_values_emitted": False, "text_emitted": False,
                             "forced_evaluation": False, "vocab_size_exclusive": VOCAB_SIZE}
    if not event["returned_five_tuple"]:
        return event
    prompt, segments = result[:2]
    prompt_valid = _valid_tokens(prompt)
    segments_valid = (isinstance(segments, list) and len(segments) <= MAX_SEGMENTS
                      and all(_valid_tokens(segment) for segment in segments)
                      and sum(len(segment) for segment in segments) <= MAX_TOKENS)
    event.update(prompt_tokens_valid=prompt_valid, segments_tokens_valid=segments_valid,
                 prompt_token_count=len(prompt) if isinstance(prompt, list) else None,
                 segment_count=len(segments) if isinstance(segments, list) else None)
    if prompt_valid:
        event["prompt_token_ids_sha256"] = token_digest(prompt)
    if segments_valid:
        concatenated = [token for segment in segments for token in segment]
        event.update(segment_token_counts=[len(segment) for segment in segments],
                     segments_concat_token_count=len(concatenated),
                     segments_concat_token_ids_sha256=token_digest(concatenated),
                     segments_concat_matches_prompt=prompt_valid and concatenated == prompt)
    return event


class _State:
    def __init__(self, generator_class: Any, original: Callable[..., Any], emit: Callable[[dict[str, Any]], Any]):
        self.generator_class, self.original, self.emit = generator_class, original, emit
        self.references = self.events = 0
        self.lock = threading.Lock()
        signature = inspect.signature(original)

        @wraps(original)
        def observed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            with self.lock:
                if self.events >= MAX_EVENTS:
                    return result
                self.events += 1
                sequence = self.events
            try:
                request = signature.bind(*args, **kwargs).arguments["request"]
                request_type = getattr(request, "request_type", None)
                event = summarize_return(result)
                event.update(event="mlx_actual_prompt_token_trace", event_sequence=sequence,
                             request_type=request_type if request_type in ("chat", "completion") else "other",
                             observational_only=True, maximum_events=MAX_EVENTS)
                self.emit(event)
            except Exception:  # noqa: BLE001, S110 - Observation must not change generation or expose exception contents.
                pass
            return result

        self.wrapper = observed


_LOCK = threading.RLock()
_ACTIVE: dict[type, _State] = {}


class PromptTokenTraceHandle:
    def __init__(self, state: _State):
        self._state, self._closed = state, False

    def close(self) -> None:
        with _LOCK:
            if self._closed:
                return
            self._closed = True
            state = self._state
            state.references -= 1
            if state.references == 0:
                if state.generator_class._tokenize is state.wrapper:
                    state.generator_class._tokenize = state.original
                if _ACTIVE.get(state.generator_class) is state:
                    del _ACTIVE[state.generator_class]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def install_prompt_token_trace(server: Any, emit: Callable[[dict[str, Any]], Any]) -> PromptTokenTraceHandle:
    """Observe a pinned server method without changing its return or errors.

    Nested installs share one wrapper, event budget and first emitter. Closing
    the last scope restores only this scope's own class method, never a foreign
    replacement. Token-array or malformed returns are not materialized: the
    event reports failed list/ID constraints, and generation remains untouched.
    """
    generator_class = getattr(server, "ResponseGenerator", None)
    if (getattr(server, "__name__", None) != MODULE_NAME or not isinstance(generator_class, type)
            or not callable(getattr(generator_class, "_tokenize", None)) or not callable(emit)):
        raise PromptTraceRefused("An explicitly pinned mlx_lm.server ResponseGenerator and callable emitter are required.")
    with _LOCK:
        target: Any = generator_class
        original = target._tokenize
        state = _ACTIVE.get(generator_class)
        if state is not None and original is not state.wrapper:
            raise PromptTraceRefused("The active tokenizer scope was replaced by foreign instrumentation.")
        if state is None:
            if tuple(inspect.signature(original).parameters) != ("self", "tokenizer", "request", "args"):
                raise PromptTraceRefused("The pinned actual-tokenizer method signature changed.")
            state = _State(generator_class, original, emit)
            target._tokenize = state.wrapper
            _ACTIVE[generator_class] = state
        state.references += 1
        return PromptTokenTraceHandle(state)
