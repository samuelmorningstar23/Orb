"""Pure symbolic CPU fakes for the experimental arithmetic scope; no MLX."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("orb_mlx_gemma3_f32_embedding", Path(__file__).resolve().parents[1] / "scripts/mlx_gemma3_f32_embedding.py")
assert SPEC is not None and SPEC.loader is not None
f32 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(f32)


class Array:
    def __init__(self, dtype, value=1.):
        self.dtype, self.value, self.casts = dtype, value, []
    def astype(self, dtype):
        self.casts.append(dtype)
        return Array(dtype, self.value)
    def __mul__(self, other):
        assert self.dtype == other.dtype == "float32"
        return Array(self.dtype, self.value * other.value)
    def __repr__(self):
        raise AssertionError("Never log array contents")


def module_fixture(monkeypatch, *, layers=6, pattern=6):
    scalar_calls, mask_calls, layer_calls = [], [], []
    def scalar(value, dtype):
        scalar_calls.append((value, dtype))
        return Array(dtype, value)
    def create_attention_mask(h, cache, window_size=None):
        mask_calls.append((h, cache, window_size))
        return "global" if window_size is None else "sliding"
    class Gemma3Model:
        def __init__(self):
            self.args = SimpleNamespace(hidden_size=5376)
            self.sliding_window_pattern, self.window_size = pattern, 1024
            self.embedded, self.embedding_calls, self.norm_calls = Array("float16"), [], []
            self.weights = [Array("float16"), Array("float32")]
            def embed(inputs):
                self.embedding_calls.append(inputs)
                return self.embedded
            self.embed_tokens = embed
            def layer(h, mask, cache):
                layer_calls.append((h, mask, cache))
                return h
            self.layers = [layer for _ in range(layers)]
            def norm(h):
                self.norm_calls.append(h)
                return h
            self.norm = norm
        def __call__(self, inputs, cache=None, input_embeddings=None):
            raise AssertionError("Original must not run an additional forward pass")
    module = SimpleNamespace(__name__=f32.MODULE_NAME, Gemma3Model=Gemma3Model,
                             mx=SimpleNamespace(float32="float32", array=scalar),
                             create_attention_mask=create_attention_mask)
    monkeypatch.setattr(f32, "ORIGINAL_CALL_SHA256", hashlib.sha256(inspect.getsource(Gemma3Model.__call__).encode()).hexdigest())
    return module, scalar_calls, mask_calls, layer_calls


def test_f32_exact_gain_masks_caches_norm_and_weights_are_preserved(monkeypatch):
    module, scalars, masks, layers = module_fixture(monkeypatch)
    model, events = module.Gemma3Model(), []
    original, weights, caches = module.Gemma3Model.__call__, list(model.weights), [object() for _ in range(6)]
    with f32.install_native_f32_embedding(module, events.append):
        result = model("input-token-sentinel", cache=caches)
        assert model.embedding_calls == ["input-token-sentinel"]
        assert result.dtype == "float32" and result.value == math.sqrt(5376)
        assert scalars == [(math.sqrt(5376), "float32")]
        assert model.embedded.dtype == "float16" and model.embedded.casts == ["float32"]
        assert [mask for _, mask, _ in layers] == ["sliding"] * 5 + ["global"]
        assert [cache for _, _, cache in layers] == caches
        assert [(cache, window) for _, cache, window in masks] == [(caches[5], None), (caches[0], 1024)]
        assert model.norm_calls == [result]
        assert model.weights == weights and all(weight.casts == [] for weight in model.weights)
        model("second")
    assert module.Gemma3Model.__call__ is original and len(events) == 1
    assert events[0]["stored_weights_cast"] is False and events[0]["clinical_quality_qualified"] is False


def test_supplied_input_embeddings_are_copied_without_reembedding_and_none_cache_behavior_matches_source(monkeypatch):
    module, _, masks, layers = module_fixture(monkeypatch)
    model, input_embeddings = module.Gemma3Model(), Array("bfloat16", 2.)
    with f32.install_native_f32_embedding(module):
        result = model(None, input_embeddings=input_embeddings)
    assert model.embedding_calls == [] and input_embeddings.dtype == "bfloat16"
    assert result.dtype == "float32" and result.value == 2 * math.sqrt(5376)
    assert [cache for _, _, cache in layers] == [None] * 6
    assert [cache for _, cache, _ in masks] == [None, None]


def test_pattern_one_uses_original_global_mask_only(monkeypatch):
    module, _, masks, layers = module_fixture(monkeypatch, layers=2, pattern=1)
    with f32.install_native_f32_embedding(module):
        module.Gemma3Model()(None)
    assert len(masks) == 1 and [mask for _, mask, _ in layers] == ["global", "global"]


def test_nested_scopes_are_idempotent_and_do_not_overwrite_foreign_patch(monkeypatch):
    module, _, _, _ = module_fixture(monkeypatch)
    original = module.Gemma3Model.__call__
    first = f32.install_native_f32_embedding(module)
    wrapper = module.Gemma3Model.__call__
    second = f32.install_native_f32_embedding(module)
    first.close()
    first.close()
    assert module.Gemma3Model.__call__ is wrapper
    second.close()
    assert module.Gemma3Model.__call__ is original
    handle = f32.install_native_f32_embedding(module)
    foreign = lambda *args, **kwargs: None
    module.Gemma3Model.__call__ = foreign
    with pytest.raises(f32.F32EmbeddingRefused, match="foreign"):
        f32.install_native_f32_embedding(module)
    handle.close()
    assert module.Gemma3Model.__call__ is foreign


def test_source_drift_is_refused_before_any_change(monkeypatch):
    module, _, _, _ = module_fixture(monkeypatch)
    original = module.Gemma3Model.__call__
    monkeypatch.setattr(f32, "ORIGINAL_CALL_SHA256", "0" * 64)
    with pytest.raises(f32.F32EmbeddingRefused, match="source differs"):
        f32.install_native_f32_embedding(module)
    assert module.Gemma3Model.__call__ is original


def test_diagnostic_failure_cannot_change_arithmetic_or_repeat_event(monkeypatch):
    module, _, _, _ = module_fixture(monkeypatch)
    events = []
    def failure(event):
        events.append(event)
        raise RuntimeError("diagnostic failure")
    with f32.install_native_f32_embedding(module, failure):
        model = module.Gemma3Model()
        assert model(None).dtype == "float32" and model(None).dtype == "float32"
    assert len(events) == 1


def test_current_installed_method_source_pin_without_importing_mlx():
    path = Path("/private/tmp/orb-mlx-benchmark-20261021/venv/lib/python3.12/site-packages/mlx_lm/models/gemma3_text.py")
    if not path.is_file():
        pytest.skip("Pinned local benchmark software is not provisioned")
    text = path.read_text()
    cls = next(node for node in ast.parse(text).body if isinstance(node, ast.ClassDef) and node.name == "Gemma3Model")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__call__")
    source = "".join(text.splitlines(keepends=True)[method.lineno - 1:method.end_lineno])
    assert hashlib.sha256(source.encode()).hexdigest() == f32.ORIGINAL_CALL_SHA256
