"""Pure symbolic fake checks: no MLX, models, weights or GPU evaluation."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import math
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("orb_mlx_gemma3_mixed_precision", Path(__file__).resolve().parents[1] / "scripts/mlx_gemma3_mixed_precision.py")
assert SPEC is not None and SPEC.loader is not None
mixed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mixed)


class Array:
    def __init__(self, dtype, value=1., shape=(1, 2, 5376)):
        self.dtype, self.value, self.shape, self.casts = dtype, value, shape, []
    def astype(self, dtype):
        self.casts.append(dtype)
        return Array(dtype, self.value, self.shape)
    def reshape(self, *shape):
        if -1 in shape:
            size = math.prod(self.shape) // -math.prod(shape)
            shape = tuple(size if value == -1 else value for value in shape)
        return Array(self.dtype, self.value, tuple(shape))
    def transpose(self, *axes):
        return Array(self.dtype, self.value, tuple(self.shape[axis] for axis in axes))
    def __repr__(self):
        raise AssertionError("Tensor contents must never enter diagnostics")


def module_fixture(monkeypatch):
    calls = {"projection": [], "norm": [], "rope": [], "attention": [], "residual": []}
    class RMSNorm:
        def __init__(self, output=1.):
            self.weight, self.output = Array("float32"), output
        def __call__(self, x):
            result = Array("float32", self.output, x.shape)
            calls["norm"].append((self, x, result))
            return result
    def project(name, width):
        def projection(x):
            calls["projection"].append((name, x))
            return Array(x.dtype, 1., (*x.shape[:-1], width))
        return projection
    class Attention:
        def __init__(self):
            self.n_heads, self.n_kv_heads, self.scale = 32, 16, 168 ** -.5
            self.q_proj, self.k_proj, self.v_proj = project("q", 4096), project("k", 2048), project("v", 2048)
            self.o_proj = project("o", 5376)
            self.q_norm, self.k_norm = RMSNorm(), RMSNorm()
            def rope(x, **kwargs):
                calls["rope"].append((x, kwargs))
                return x
            self.rope = rope
        def __call__(self, x, mask=None, cache=None):
            raise AssertionError("No duplicate original attention forward")
    class TransformerBlock:
        def __init__(self):
            self.self_attn = Attention()
            self.mlp = project("mlp", 5376)
            self.input_layernorm, self.pre_feedforward_layernorm = RMSNorm(), RMSNorm()
            self.post_attention_layernorm, self.post_feedforward_layernorm = RMSNorm(90_000), RMSNorm(90_000)
            self.weights = [Array("float16"), Array("float32")]
        def __call__(self, x, mask=None, cache=None):
            raise AssertionError("No duplicate original block forward")
    def clip_residual(x, y):
        calls["residual"].append((x, y))
        value = x.value + y.value
        if x.dtype == "float16":
            value = max(-65504, min(65504, value))
        return Array(x.dtype, value, x.shape)
    def scaled_dot_product_attention(q, k, v, **kwargs):
        calls["attention"].append((q, k, v, kwargs))
        return Array(q.dtype, 1., q.shape)
    module = SimpleNamespace(__name__=mixed.MODULE_NAME, RMSNorm=RMSNorm, Attention=Attention,
                             TransformerBlock=TransformerBlock, mx=SimpleNamespace(float16="float16"),
                             clip_residual=clip_residual, scaled_dot_product_attention=scaled_dot_product_attention)
    monkeypatch.setattr(mixed, "PINNED_CALL_SHA256", {name: hashlib.sha256(inspect.getsource(getattr(module, name).__call__).encode()).hexdigest()
                                                    for name in mixed.PINNED_CALL_SHA256})
    return module, calls


def test_f32_residuals_above65504_survive_while_only_projection_and_qk_inputs_cast(monkeypatch):
    module, calls = module_fixture(monkeypatch)
    block, events = module.TransformerBlock(), []
    norm_original, weights = module.RMSNorm.__call__, list(block.weights)
    with mixed.install_mixed_projection_inputs(module, events.append):
        result = block(Array("float32", 100_000), mask="mask")
        assert result.dtype == "float32" and result.value == 280_000
        assert module.RMSNorm.__call__ is norm_original
        assert all(x.dtype == "float16" for _, x in calls["projection"])
        assert [name for name, _ in calls["projection"]] == ["q", "k", "v", "o", "mlp"]
        assert all(x.dtype == y.dtype == "float32" for x, y in calls["residual"])
        assert all(norm.weight.casts == [] for norm, _, _ in calls["norm"])
        assert block.weights == weights and all(weight.casts == [] for weight in block.weights)
        block(Array("float32"))
    assert len(events) == 1 and events[0]["stored_weights_cast"] is False


def test_attention_qk_casts_before_rope_preserves_cache_offset_mask_scale_and_layout(monkeypatch):
    module, calls = module_fixture(monkeypatch)
    updates = []
    def update(k, v):
        updates.append((k, v))
        return k, v
    cache = SimpleNamespace(offset=777, update_and_fetch=update)
    with mixed.install_mixed_projection_inputs(module):
        result = module.Attention()(Array("float16", shape=(2, 3, 5376)), mask="original-mask", cache=cache)
    assert result.dtype == "float16" and result.shape == (2, 3, 5376)
    assert [kwargs for _, kwargs in calls["rope"]] == [{"offset": 777}, {"offset": 777}]
    assert all(x.dtype == "float16" for x, _ in calls["rope"])
    assert len(updates) == 1 and all(x.dtype == "float16" for x in updates[0])
    q, k, v, kwargs = calls["attention"][0]
    assert q.shape == (2, 32, 3, 128) and k.shape == v.shape == (2, 16, 3, 128)
    assert kwargs == {"cache": cache, "scale": 168 ** -.5, "mask": "original-mask"}
    assert calls["projection"][-1][1].shape == (2, 3, 4096)


def test_qk_follow_values_dtype_and_none_cache_has_no_added_offset(monkeypatch):
    module, calls = module_fixture(monkeypatch)
    with mixed.install_mixed_projection_inputs(module):
        module.Attention()(Array("bfloat16"))
    assert all(x.dtype == "bfloat16" and kwargs == {} for x, kwargs in calls["rope"])


def test_source_drift_or_decorated_norm_restore_refused_before_patching(monkeypatch):
    module, _ = module_fixture(monkeypatch)
    block_original, attention_original = module.TransformerBlock.__call__, module.Attention.__call__
    norm_original = module.RMSNorm.__call__
    @wraps(norm_original)
    def repaired(self, x):
        return norm_original(self, x).astype(x.dtype)
    module.RMSNorm.__call__ = repaired
    with pytest.raises(mixed.MixedPrecisionRefused, match="incompatible"):
        mixed.install_mixed_projection_inputs(module)
    assert module.TransformerBlock.__call__ is block_original and module.Attention.__call__ is attention_original
    module.RMSNorm.__call__ = norm_original
    monkeypatch.setitem(mixed.PINNED_CALL_SHA256, "Attention", "0" * 64)
    with pytest.raises(mixed.MixedPrecisionRefused, match="source differs"):
        mixed.install_mixed_projection_inputs(module)
    assert module.TransformerBlock.__call__ is block_original and module.Attention.__call__ is attention_original


def test_nested_close_restores_both_methods_and_preserves_foreign_replacements(monkeypatch):
    module, _ = module_fixture(monkeypatch)
    originals = module.TransformerBlock.__call__, module.Attention.__call__
    first = mixed.install_mixed_projection_inputs(module)
    wrappers = module.TransformerBlock.__call__, module.Attention.__call__
    second = mixed.install_mixed_projection_inputs(module)
    first.close()
    first.close()
    assert (module.TransformerBlock.__call__, module.Attention.__call__) == wrappers
    second.close()
    assert (module.TransformerBlock.__call__, module.Attention.__call__) == originals
    handle = mixed.install_mixed_projection_inputs(module)
    foreign = lambda *args, **kwargs: None
    module.Attention.__call__ = foreign
    with pytest.raises(mixed.MixedPrecisionRefused, match="foreign"):
        mixed.install_mixed_projection_inputs(module)
    handle.close()
    assert module.TransformerBlock.__call__ is originals[0] and module.Attention.__call__ is foreign


def test_failed_optional_diagnostics_do_not_change_arithmetic(monkeypatch):
    module, _ = module_fixture(monkeypatch)
    calls = []
    def fail(event):
        calls.append(event)
        raise OSError("synthetic observation failure")
    with mixed.install_mixed_projection_inputs(module, fail):
        block = module.TransformerBlock()
        assert block(Array("float32")).dtype == "float32"
        assert block(Array("float32")).dtype == "float32"
    assert len(calls) == 1


def test_installed_method_hashes_match_without_importing_mlx():
    path = Path("/private/tmp/orb-mlx-benchmark-20261021/venv/lib/python3.12/site-packages/mlx_lm/models/gemma3_text.py")
    if not path.is_file():
        pytest.skip("Pinned local benchmark software is not provisioned")
    text = path.read_text()
    for name, expected in mixed.PINNED_CALL_SHA256.items():
        cls = next(node for node in ast.parse(text).body if isinstance(node, ast.ClassDef) and node.name == name)
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__call__")
        source = "".join(text.splitlines(keepends=True)[method.lineno - 1:method.end_lineno])
        assert hashlib.sha256(source.encode()).hexdigest() == expected
