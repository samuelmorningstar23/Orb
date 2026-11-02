"""Scoped Gemma3 RMSNorm dtype restoration; importing this never imports MLX.

Local reference: Transformers5.19.0,
server/.venv/lib/python3.12/site-packages/transformers/models/gemma3/modeling_gemma3.py,
Gemma3RMSNorm.forward (lines135–149): normalize and multiply weights in float32,
then ``return output.type_as(x)``. Installed MLX-LM0.32.2 Gemma3 instead returns
``mx.fast.rms_norm(x, 1.0 + self.weight, self.eps)`` directly. Its mixed F16/F32
output promotion was confirmed by the owned2026-10-22 dtype probe.

This helper preserves the original normalization call and all stored weights,
and only applies ``result.astype(x.dtype)`` afterwards. It does not change
quantization, BF16/F16 weights, cache, context or sampling. The caller must pin
and qualify the selected MLX-LM version/module and the helper source separately.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, Self

MODULE_NAME = "mlx_lm.models.gemma3_text"
HF_REFERENCE = "Transformers5.19.0 Gemma3RMSNorm.forward: return output.type_as(x) after float32 normalization and weight multiplication"


class PrecisionPatchRefused(ValueError):
    """An unexpected module/class or active foreign patch cannot be modified."""


class _State:
    def __init__(self, norm_class: type, original: Callable[..., Any], emit: Callable[[dict[str, Any]], Any] | None):
        self.norm_class = norm_class
        self.original = original
        self.emit = emit
        self.references = 0
        self.emitted = False
        self.event_lock = threading.Lock()

        @wraps(original)
        def restored(instance: Any, x: Any, *args: Any, **kwargs: Any) -> Any:
            result = original(instance, x, *args, **kwargs)
            corrected = result.astype(x.dtype)
            if self.emitted:
                return corrected
            # No tensor values, shapes, prompts, identifiers or representations
            # enter this event. At most one callback runs per installed scope.
            with self.event_lock:
                first_event = not self.emitted
                if first_event:
                    self.emitted = True
            if first_event and self.emit is not None:
                try:
                    self.emit({"event": "gemma3_norm_dtype_restore_first_call", "module": MODULE_NAME,
                               "input_dtype": str(x.dtype), "original_output_dtype": str(result.dtype),
                               "corrected_output_dtype": str(corrected.dtype),
                               "normalization_formula_changed": False, "stored_weights_cast": False,
                               "tensor_values_emitted": False})
                except Exception:  # noqa: BLE001 - Optional diagnostics cannot change normalization behavior.
                    pass
            return corrected

        self.wrapper = restored


_LOCK = threading.RLock()
_ACTIVE: dict[type, _State] = {}


class NormDtypeRestoreHandle:
    """A closable, idempotent scope; nested handles share exactly one wrapper."""

    def __init__(self, state: _State):
        self._state = state
        self._closed = False

    def close(self) -> None:
        with _LOCK:
            if self._closed:
                return
            self._closed = True
            state = self._state
            state.references -= 1
            if state.references == 0:
                # Never overwrite instrumentation installed by someone else
                # after us. Remove only the class method that this scope owns.
                if state.norm_class.__call__ is state.wrapper:
                    setattr(state.norm_class, "__call__", state.original)  # noqa: B010 - Intentional dynamic class-method restoration.
                if _ACTIVE.get(state.norm_class) is state:
                    del _ACTIVE[state.norm_class]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def install_norm_dtype_restore(gemma3_module: Any, emit: Callable[[dict[str, Any]], Any] | None = None) -> NormDtypeRestoreHandle:
    """Wrap only the explicitly supplied Gemma3 text module's RMSNorm call.

    Repeated installs share one wrapper and first-event callback. Closing the
    last handle restores the original method; closing any handle twice is safe.
    Caller-owned foreign replacements are preserved instead of overwritten.
    """
    norm_class = getattr(gemma3_module, "RMSNorm", None)
    if (getattr(gemma3_module, "__name__", None) != MODULE_NAME or not isinstance(norm_class, type)
            or not callable(norm_class.__dict__.get("__call__"))):
        raise PrecisionPatchRefused("An explicitly pinned mlx_lm.models.gemma3_text RMSNorm class is required.")
    if emit is not None and not callable(emit):
        raise PrecisionPatchRefused("The optional dtype-event emitter must be callable.")
    with _LOCK:
        state = _ACTIVE.get(norm_class)
        if state is not None and norm_class.__call__ is not state.wrapper:
            raise PrecisionPatchRefused("The active RMSNorm scope was replaced by foreign instrumentation.")
        if state is None:
            state = _State(norm_class, norm_class.__call__, emit)
            setattr(norm_class, "__call__", state.wrapper)  # noqa: B010 - Intentional scoped dynamic class-method patch.
            _ACTIVE[norm_class] = state
        state.references += 1
        return NormDtypeRestoreHandle(state)
