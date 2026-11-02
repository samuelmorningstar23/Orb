"""EXPERIMENTAL source-pinned F16 projections with F32 Gemma3 residuals.

No MLX import, model load or patch occurs on import. Compose only with the
separate F32 embedding/residual scope and the original, UNPATCHED RMSNorm.
Changing RMSNorm output globally to its input dtype is explicitly refused: it
would also round post-attention/post-FFN norms and restore residual saturation.

Only two normalized projection inputs cast toF16; Q/K norm outputs cast to the
values dtype before original RoPE/cache/attention operations. Post-attention,
post-FFN and final normalization retain their original F32 output. Parameters,
context, masks, cache policy, head, sampler and quality gates are untouched.
This is a new arithmetic candidate, not a claim of native/clinical equivalence.
Caller policy must select the all-quantized checkpoint and F32 embedding scope;
this helper neither examines nor casts weights. Original methods are source
SHA-pinned, and decorated/foreign RMSNorm methods are rejected before mutation.
"""
from __future__ import annotations

import hashlib
import inspect
import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, Self

MODULE_NAME = "mlx_lm.models.gemma3_text"
EXPERIMENTAL = True
PINNED_CALL_SHA256 = {
    "TransformerBlock": "2647bb63d52239895927ae6ca90f5f31c7a2f7222eeb41a948a8d1060c3293f2",
    "Attention": "a293df9f54178d8a3a9b5962953f0d4d0cddfb6b711db03893fda75c4309b5b8",
    "RMSNorm": "a459ce2c95139b119c727b1a8438b55b7726f817a7d9324f907ec8e38ebdca00",
}


class MixedPrecisionRefused(ValueError):
    """The original methods/norm policy are not the explicitly pinned ones."""


class _State:
    def __init__(self, module: Any, classes: dict[str, Any], emit: Callable[[dict[str, Any]], Any] | None):
        self.classes, self.emit = classes, emit
        self.original_block = classes["TransformerBlock"].__call__
        self.original_attention = classes["Attention"].__call__
        self.original_norm = classes["RMSNorm"].__call__
        self.references, self.emitted = 0, False
        self.lock = threading.Lock()
        mx, clip_residual = module.mx, module.clip_residual
        scaled_dot_product_attention = module.scaled_dot_product_attention
        state = self

        @wraps(self.original_block)
        def block(self: Any, x: Any, mask: Any = None, cache: Any = None) -> Any:
            r = self.self_attn(self.input_layernorm(x).astype(mx.float16), mask, cache)
            h = clip_residual(x, self.post_attention_layernorm(r))
            r = self.mlp(self.pre_feedforward_layernorm(h).astype(mx.float16))
            out = clip_residual(h, self.post_feedforward_layernorm(r))
            state.first_event(x.dtype, out.dtype)
            return out

        @wraps(self.original_attention)
        def attention(self: Any, x: Any, mask: Any = None, cache: Any = None) -> Any:
            B, L, _ = x.shape
            queries, keys, values = self.q_proj(x), self.k_proj(x), self.v_proj(x)
            queries = queries.reshape(B, L, self.n_heads, -1).transpose(0, 2, 1, 3)

            keys = keys.reshape(B, L, self.n_kv_heads, -1).transpose(0, 2, 1, 3)
            values = values.reshape(B, L, self.n_kv_heads, -1).transpose(0, 2, 1, 3)

            queries = self.q_norm(queries).astype(values.dtype)
            keys = self.k_norm(keys).astype(values.dtype)

            if cache is not None:
                queries = self.rope(queries, offset=cache.offset)
                keys = self.rope(keys, offset=cache.offset)
                keys, values = cache.update_and_fetch(keys, values)
            else:
                queries = self.rope(queries)
                keys = self.rope(keys)

            # Original sliding-window attention and projection ordering.
            output = scaled_dot_product_attention(
                queries, keys, values, cache=cache, scale=self.scale, mask=mask
            )
            output = output.transpose(0, 2, 1, 3).reshape(B, L, -1)
            return self.o_proj(output)

        self.block_wrapper, self.attention_wrapper = block, attention

    def first_event(self, input_dtype: Any, output_dtype: Any) -> None:
        if self.emitted:
            return
        with self.lock:
            first = not self.emitted
            self.emitted = True
        if first and self.emit is not None:
            try:
                self.emit({"event": "gemma3_experimental_mixed_projection_first_call", "experimental": True,
                           "residual_input_dtype": str(input_dtype), "residual_output_dtype": str(output_dtype),
                           "projection_input_dtype": "float16", "qk_norm_output_policy": "cast to values dtype before RoPE/cache",
                           "norm_method_changed": False, "stored_weights_cast": False,
                           "context_or_cache_policy_changed": False, "token_or_tensor_values_emitted": False,
                           "clinical_quality_qualified": False, "original_method_sha256": dict(PINNED_CALL_SHA256)})
            except Exception:  # noqa: BLE001, S110 - Diagnostics cannot affect the arithmetic.
                pass


_LOCK = threading.RLock()
_ACTIVE: dict[tuple[type, type], _State] = {}


class MixedProjectionHandle:
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
                if state.classes["TransformerBlock"].__call__ is state.block_wrapper:
                    state.classes["TransformerBlock"].__call__ = state.original_block
                if state.classes["Attention"].__call__ is state.attention_wrapper:
                    state.classes["Attention"].__call__ = state.original_attention
                key = (state.classes["TransformerBlock"], state.classes["Attention"])
                if _ACTIVE.get(key) is state:
                    del _ACTIVE[key]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def install_mixed_projection_inputs(module: Any, emit: Callable[[dict[str, Any]], Any] | None = None) -> MixedProjectionHandle:
    """Scope the candidate; the global norm-output dtype repair is incompatible."""
    classes: dict[str, Any] = {name: getattr(module, name, None) for name in PINNED_CALL_SHA256}
    if (getattr(module, "__name__", None) != MODULE_NAME
            or any(not isinstance(cls, type) or not callable(cls.__dict__.get("__call__")) for cls in classes.values())
            or not callable(getattr(module, "clip_residual", None))
            or not callable(getattr(module, "scaled_dot_product_attention", None))
            or getattr(module, "mx", None) is None or (emit is not None and not callable(emit))):
        raise MixedPrecisionRefused("An explicitly pinned Gemma3 text module and original class methods are required.")
    with _LOCK:
        key = (classes["TransformerBlock"], classes["Attention"])
        state = _ACTIVE.get(key)
        if state is not None:
            if (classes["TransformerBlock"].__call__ is not state.block_wrapper
                    or classes["Attention"].__call__ is not state.attention_wrapper
                    or classes["RMSNorm"].__call__ is not state.original_norm):
                raise MixedPrecisionRefused("Active mixed/norm methods were replaced by foreign instrumentation.")
        else:
            for name, expected in PINNED_CALL_SHA256.items():
                original = classes[name].__call__
                # inspect.getsource unwraps functools.wraps; identity alone
                # cannot let the incompatible dtype-restore norm hide here.
                if hasattr(original, "__wrapped__"):
                    raise MixedPrecisionRefused(f"{name} must remain original and undecorated; norm dtype restoration is incompatible.")
                try:
                    actual = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
                except (OSError, TypeError) as exc:
                    raise MixedPrecisionRefused("Original Gemma3 method source is unavailable for pinning.") from exc
                if actual != expected:
                    raise MixedPrecisionRefused(f"{name} source differs from the pinned MLX-LM0.32.2 method.")
            state = _State(module, classes, emit)
            classes["TransformerBlock"].__call__ = state.block_wrapper
            classes["Attention"].__call__ = state.attention_wrapper
            _ACTIVE[key] = state
        state.references += 1
        return MixedProjectionHandle(state)
