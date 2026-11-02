"""Fake-only normalization scope tests; never import or evaluate MLX."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("orb_mlx_gemma3_precision", Path(__file__).resolve().parents[1] / "scripts/mlx_gemma3_precision.py")
assert SPEC is not None and SPEC.loader is not None
precision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(precision)


class Array:
    def __init__(self, dtype, *, token=None):
        self.dtype, self.token, self.casts = dtype, token, []

    def astype(self, dtype):
        self.casts.append(dtype)
        return Array(dtype, token=self.token)

    def __repr__(self):
        raise AssertionError("Tensor representations must never enter diagnostics")


def module_fixture():
    class RMSNorm:
        def __init__(self):
            self.weight = Array("float32", token=object())
            self.calls = []
            self.result = Array("float32", token=object())

        def __call__(self, x):
            self.calls.append(x)
            return self.result

    return SimpleNamespace(__name__=precision.MODULE_NAME, RMSNorm=RMSNorm)


def test_original_formula_runs_once_output_restores_input_dtype_and_f32_weight_is_untouched():
    module = module_fixture()
    norm = module.RMSNorm()
    original_weight, original_call = norm.weight, module.RMSNorm.__call__
    events = []
    with precision.install_norm_dtype_restore(module, emit=events.append):
        x = Array("float16")
        y = norm(x)
        assert norm.calls == [x]
        assert norm.result.casts == ["float16"]
        assert y.dtype == "float16" and y.token is norm.result.token
        assert norm.weight is original_weight and norm.weight.dtype == "float32"
        assert norm.weight.casts == []
        norm(Array("bfloat16"))
        assert len(norm.calls) == 2
    assert module.RMSNorm.__call__ is original_call
    assert len(events) == 1
    assert events[0] == {"event": "gemma3_norm_dtype_restore_first_call", "module": precision.MODULE_NAME,
                         "input_dtype": "float16", "original_output_dtype": "float32", "corrected_output_dtype": "float16",
                         "normalization_formula_changed": False, "stored_weights_cast": False, "tensor_values_emitted": False}


def test_no_default_emitter_prints_or_reads_tensor_content(monkeypatch):
    monkeypatch.setattr("builtins.print", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("No default logging")))
    module = module_fixture()
    with precision.install_norm_dtype_restore(module):
        assert module.RMSNorm()(Array("float32")).dtype == "float32"


def test_optional_diagnostic_failure_cannot_fail_or_repeat_normalization():
    module = module_fixture()
    emitted = []

    def broken(event):
        emitted.append(event)
        raise OSError("synthetic diagnostic failure")

    norm = module.RMSNorm()
    with precision.install_norm_dtype_restore(module, emit=broken):
        assert norm(Array("float16")).dtype == "float16"
        assert norm(Array("float16")).dtype == "float16"
    assert len(norm.calls) == 2 and len(emitted) == 1


def test_nested_installs_share_one_wrapper_and_restore_only_after_last_idempotent_close():
    module = module_fixture()
    original = module.RMSNorm.__call__
    first_events, second_events = [], []
    first = precision.install_norm_dtype_restore(module, first_events.append)
    wrapper = module.RMSNorm.__call__
    second = precision.install_norm_dtype_restore(module, second_events.append)
    assert module.RMSNorm.__call__ is wrapper
    norm = module.RMSNorm()
    norm(Array("float16"))
    assert len(norm.calls) == 1 and len(first_events) == 1 and second_events == []
    first.close()
    first.close()
    assert module.RMSNorm.__call__ is wrapper
    second.close()
    second.close()
    assert module.RMSNorm.__call__ is original
    with precision.install_norm_dtype_restore(module):
        assert module.RMSNorm.__call__ is not original
    assert module.RMSNorm.__call__ is original


def test_other_classes_are_untouched_and_foreign_replacement_is_never_overwritten():
    module, other = module_fixture(), module_fixture()
    untouched = other.RMSNorm.__call__
    handle = precision.install_norm_dtype_restore(module)
    assert other.RMSNorm.__call__ is untouched

    def foreign(instance, x):
        return x

    module.RMSNorm.__call__ = foreign
    with pytest.raises(precision.PrecisionPatchRefused, match="foreign"):
        precision.install_norm_dtype_restore(module)
    handle.close()
    assert module.RMSNorm.__call__ is foreign


def test_original_errors_propagate_and_scope_still_restores_without_emission():
    module = module_fixture()

    def failure(instance, x):
        raise RuntimeError("synthetic normalization failure")

    module.RMSNorm.__call__ = failure
    events = []
    with pytest.raises(RuntimeError, match="synthetic normalization failure"), precision.install_norm_dtype_restore(module, events.append):
        module.RMSNorm()(Array("float16"))
    assert events == [] and module.RMSNorm.__call__ is failure


@pytest.mark.parametrize("change", ["wrong_module", "missing_class", "bad_emitter"])
def test_wrong_target_or_emitter_is_refused(change):
    module = module_fixture()
    emit = None
    if change == "wrong_module":
        module.__name__ = "mlx_lm.models.other"
    elif change == "missing_class":
        module.RMSNorm = None
    else:
        emit = "not callable"
    with pytest.raises(precision.PrecisionPatchRefused):
        precision.install_norm_dtype_restore(module, emit=emit)
