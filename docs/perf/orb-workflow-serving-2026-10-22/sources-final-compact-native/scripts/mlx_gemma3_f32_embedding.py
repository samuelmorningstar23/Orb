"""EXPERIMENTAL Gemma3 F32 embedding/residual arithmetic compatibility scope.

Imports no MLX and is never enabled on import. It reproduces installed MLX-LM
0.32.2 Gemma3Model.__call__ unchanged except the embedding expression: cast its
result toF32 and multiply by sqrt(hidden_size) represented directly inF32.
Native ARM Gemma3 graph uses F32 input embedding and that unrounded gain. MLX's
original uses a BF16-rounded gain and can retain F16 residuals. Stored weights,
normalization formula, projections, context, masks, caches and sampling are not
changed here. This new arithmetic policy is experimental, not a clinical or
numerical parity claim; the caller must retain unchanged quality/resource gates.

The original method source is SHA-pinned before patching. The existing scoped
norm-output dtype repair may coexist; with F32 inputs it preserves F32 output.
All-quantized candidate weights are required by caller policy to avoid expanding
dense F16 matrix weights during F32 matmul; this helper does not inspect or cast
weights and cannot establish that memory requirement by itself.
"""
from __future__ import annotations

import hashlib
import inspect
import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, Self

MODULE_NAME = "mlx_lm.models.gemma3_text"
ORIGINAL_CALL_SHA256 = "e65de4285bbd483539024393fa63f30974a7a05b1f551a53ff5b4196c9889653"
EXPERIMENTAL = True


class F32EmbeddingRefused(ValueError):
    """The expected module/method or source identity changed."""


class _State:
    def __init__(self, module: Any, model_class: Any, original: Callable[..., Any],
                 emit: Callable[[dict[str, Any]], Any] | None):
        self.model_class, self.original, self.emit = model_class, original, emit
        self.references, self.emitted = 0, False
        self.lock = threading.Lock()
        mx, create_attention_mask = module.mx, module.create_attention_mask
        state = self

        @wraps(original)
        def native_f32(self: Any, inputs: Any, cache: Any = None, input_embeddings: Any = None) -> Any:
            if input_embeddings is not None:
                h = input_embeddings
            else:
                h = self.embed_tokens(inputs)
            original_dtype = h.dtype
            h = h.astype(mx.float32) * mx.array(self.args.hidden_size**0.5, mx.float32)

            # The following cache/mask/layer/norm operations are copied from the
            # pinned original method without changing ordering or arguments.
            if cache is None:
                cache = [None] * len(self.layers)

            global_mask = create_attention_mask(h, cache[self.sliding_window_pattern - 1])

            if self.sliding_window_pattern > 1:
                sliding_window_mask = create_attention_mask(
                    h,
                    cache[0],
                    window_size=self.window_size,
                )
            else:
                sliding_window_mask = None
            for i, (layer, c) in enumerate(zip(self.layers, cache)):
                is_global = (
                    i % self.sliding_window_pattern == self.sliding_window_pattern - 1
                )
                mask = global_mask if is_global else sliding_window_mask
                h = layer(h, mask, c)

            result = self.norm(h)
            state.first_event(original_dtype, result.dtype)
            return result

        self.wrapper = native_f32

    def first_event(self, original_dtype: Any, output_dtype: Any) -> None:
        if self.emitted:
            return
        with self.lock:
            first = not self.emitted
            self.emitted = True
        if first and self.emit is not None:
            try:
                self.emit({"event": "gemma3_experimental_f32_embedding_first_call", "experimental": True,
                           "original_method_sha256": ORIGINAL_CALL_SHA256,
                           "embedding_source_dtype": str(original_dtype), "model_output_dtype": str(output_dtype),
                           "gain_policy": "sqrt(hidden_size) directly inF32, without BF16 rounding",
                           "stored_weights_cast": False, "context_or_cache_changed": False,
                           "token_or_tensor_values_emitted": False, "clinical_quality_qualified": False})
            except Exception:  # noqa: BLE001, S110 - Optional diagnostics cannot affect arithmetic.
                pass


_LOCK = threading.RLock()
_ACTIVE: dict[type, _State] = {}


class F32EmbeddingHandle:
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
                if state.model_class.__call__ is state.wrapper:
                    state.model_class.__call__ = state.original
                if _ACTIVE.get(state.model_class) is state:
                    del _ACTIVE[state.model_class]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def install_native_f32_embedding(module: Any, emit: Callable[[dict[str, Any]], Any] | None = None) -> F32EmbeddingHandle:
    """Install this explicit, source-pinned experimental arithmetic scope only."""
    model_class = getattr(module, "Gemma3Model", None)
    if (getattr(module, "__name__", None) != MODULE_NAME or not isinstance(model_class, type)
            or not callable(model_class.__dict__.get("__call__"))
            or not callable(getattr(module, "create_attention_mask", None))
            or getattr(module, "mx", None) is None or (emit is not None and not callable(emit))):
        raise F32EmbeddingRefused("An explicitly pinned Gemma3 text module, class and callable emitter are required.")
    with _LOCK:
        target: Any = model_class
        state = _ACTIVE.get(model_class)
        if state is not None and model_class.__call__ is not state.wrapper:
            raise F32EmbeddingRefused("The active embedding scope was replaced by foreign instrumentation.")
        if state is None:
            try:
                source_hash = hashlib.sha256(inspect.getsource(model_class.__call__).encode()).hexdigest()
            except (OSError, TypeError) as exc:
                raise F32EmbeddingRefused("Original Gemma3Model.__call__ source is unavailable for pinning.") from exc
            if source_hash != ORIGINAL_CALL_SHA256:
                raise F32EmbeddingRefused("Original Gemma3Model.__call__ source differs from the pinned MLX-LM0.32.2 method.")
            state = _State(module, model_class, model_class.__call__, emit)
            target.__call__ = state.wrapper
            _ACTIVE[model_class] = state
        state.references += 1
        return F32EmbeddingHandle(state)
