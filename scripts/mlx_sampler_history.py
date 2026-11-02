"""EXPERIMENTAL generated-only penalty history adapter; imports no MLX.

This is not enabled by importing the file. Native Ollama history-scope parity
must be established before a caller installs it in any comparative benchmark.
It changes only the history supplied to the repetition/presence/frequency
processors returned by the pinned MLX-LM 0.32.2 factory. Bias, penalty values,
context limits, logits, sampling filters, temperature and weights are untouched.

Local source references: mlx_lm/sample_utils.py:76-130 orders optional bias,
then active repetition/presence/frequency processors; generate.py:416-423 and
451-453 accumulate prompt and generated tokens for unbatched processing.
generate.py:1242-1266,1347,1400-1411 seed TokenBuffer with processed prompt and
append the final prompt token on the first batched callback. TokenBuffer in
models/cache.py:1462-1495 grows and returns the entire visible history. It does
not truncate to the penalty context limit. A prefix-cache hit can omit cached
prompt tokens from the unbatched visible history. The first callback therefore
defines the *processor-visible* prompt boundary, not a claimed full prompt.

The caller must pin/qualify that source and ensure one fresh factory call per
request. A shrinking history is refused, never silently treated as a reset.
Existing request processors keep their scope if the factory patch is closed.
"""
from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, Self

MODULE_NAME = "mlx_lm.server"
EXPERIMENTAL = True
NATIVE_HISTORY_PARITY_REQUIRED = True
_PARAMETERS = (
    "logit_bias", "repetition_penalty", "repetition_context_size",
    "presence_penalty", "presence_context_size", "frequency_penalty",
    "frequency_context_size",
)
_PENALTIES = ("repetition_penalty", "presence_penalty", "frequency_penalty")


class HistoryPatchRefused(ValueError):
    """An unexpected factory or request history cannot be safely adapted."""


def _emit_safely(emit: Callable[[dict[str, Any]], Any] | None, event: dict[str, Any]) -> None:
    if emit is not None:
        try:
            emit(event)
        except Exception:  # noqa: BLE001, S110 - Optional diagnostics cannot affect sampling.
            pass


class _RequestHistory:
    def __init__(self, emit: Callable[[dict[str, Any]], Any] | None):
        self.emit = emit
        self.first_count: int | None = None
        self.last_count = 0
        self.failed = False
        self.lock = threading.Lock()

    def generated(self, tokens: Any) -> Any:
        # Only len and a native slice are used. Never materialize a host list,
        # stringify token contents, change dtype or touch logits.
        with self.lock:
            if self.failed:
                raise HistoryPatchRefused("A failed history scope requires a new per-request factory call.")
            try:
                count = len(tokens)
                if self.first_count is None:
                    self.first_count = count
                    first = True
                else:
                    first = False
                    if count < self.last_count:
                        raise HistoryPatchRefused("Processor-visible history shrank; refusing an implicit prompt reset.")
                generated = tokens[self.first_count:]
                self.last_count = count
            except Exception:
                self.failed = True
                _emit_safely(self.emit, {"event": "generated_only_penalty_history_refused",
                                         "experimental": True, "native_history_parity_required": True,
                                         "token_values_emitted": False})
                raise
        if first:
            _emit_safely(self.emit, {"event": "generated_only_penalty_history_first_call",
                                     "experimental": True, "native_history_parity_required": True,
                                     "processor_visible_prompt_token_count": count,
                                     "token_values_emitted": False, "logit_values_emitted": False})
        return generated


class _State:
    def __init__(self, server: Any, original: Callable[..., Any], signature: inspect.Signature,
                 emit: Callable[[dict[str, Any]], Any] | None):
        self.server, self.original, self.signature, self.emit = server, original, signature, emit
        self.references = 0

        @wraps(original)
        def factory(*args: Any, **kwargs: Any) -> list[Callable[..., Any]]:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            processors = original(*args, **kwargs)
            bias_count = int(bool(bound.arguments["logit_bias"]))
            penalty_count = sum(bound.arguments[name] is not None and bound.arguments[name] != 0 for name in _PENALTIES)
            if (not isinstance(processors, list) or len(processors) != bias_count + penalty_count
                    or any(not callable(processor) for processor in processors)):
                raise HistoryPatchRefused("The pinned factory's bias/penalty processor layout changed.")
            if penalty_count == 0:
                return processors
            history = _RequestHistory(emit)

            def adapt(processor: Callable[..., Any]) -> Callable[..., Any]:
                @wraps(processor)
                def penalty(tokens: Any, logits: Any) -> Any:
                    return processor(history.generated(tokens), logits)
                return penalty

            return processors[:bias_count] + [adapt(processor) for processor in processors[bias_count:]]

        self.wrapper = factory


_LOCK = threading.RLock()
_ACTIVE: dict[int, _State] = {}


class GeneratedOnlyPenaltiesHandle:
    """Closable experimental factory scope; nested handles share one wrapper."""

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
                if state.server.make_logits_processors is state.wrapper:
                    state.server.make_logits_processors = state.original
                if _ACTIVE.get(id(state.server)) is state:
                    del _ACTIVE[id(state.server)]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def install_generated_only_penalties(server: Any, emit: Callable[[dict[str, Any]], Any] | None = None) -> GeneratedOnlyPenaltiesHandle:
    """Install the experimental adapter only on an explicitly supplied server.

    This API does not assert that generated-only history matches native Ollama.
    Caller-owned foreign replacements are never overwritten on restoration.
    Native repeat-penalty 0/1 semantics and every original factory argument are
    preserved; only the original factory's active penalty callbacks are wrapped.
    """
    original = getattr(server, "make_logits_processors", None)
    if getattr(server, "__name__", None) != MODULE_NAME or not callable(original):
        raise HistoryPatchRefused("An explicitly pinned mlx_lm.server factory is required.")
    if emit is not None and not callable(emit):
        raise HistoryPatchRefused("The optional history-event emitter must be callable.")
    with _LOCK:
        state = _ACTIVE.get(id(server))
        if state is not None and original is not state.wrapper:
            raise HistoryPatchRefused("The active factory scope was replaced by foreign instrumentation.")
        if state is None:
            signature = inspect.signature(original)
            if (tuple(signature.parameters) != _PARAMETERS
                    or any(parameter.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
                           or parameter.default is inspect.Parameter.empty for parameter in signature.parameters.values())):
                raise HistoryPatchRefused("The pinned MLX-LM bias/penalty factory signature changed.")
            state = _State(server, original, signature, emit)
            server.make_logits_processors = state.wrapper
            _ACTIVE[id(server)] = state
        state.references += 1
        return GeneratedOnlyPenaltiesHandle(state)
