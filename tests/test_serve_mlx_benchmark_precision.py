"""The precision launcher scopes its same-weight repair around the server."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.mark.parametrize("fail", [False, True])
def test_precision_repair_is_scoped_and_always_restored(monkeypatch, fail):
    events = []
    model = object()
    models = types.ModuleType("mlx_lm.models")
    models.gemma3_text = model
    lm = types.ModuleType("mlx_lm")
    lm.models = models
    lm.server = object()
    traced = types.ModuleType("serve_mlx_benchmark_trace")
    traced.emit = lambda _: None

    def main():
        events.append("serve")
        if fail:
            raise RuntimeError("synthetic server failure")

    traced.main = main
    helper = types.ModuleType("mlx_gemma3_precision")

    def install(module, *, emit):
        assert module is model and emit is traced.emit
        events.append("install")
        return types.SimpleNamespace(close=lambda: events.append("close"))

    helper.install_norm_dtype_restore = install
    prompt_helper = types.ModuleType("mlx_prompt_token_trace")

    def install_trace(server, emit):
        assert server is lm.server and emit is traced.emit
        events.append("trace_install")
        return types.SimpleNamespace(close=lambda: events.append("trace_close"))

    prompt_helper.install_prompt_token_trace = install_trace
    for name, value in {"mlx_lm": lm, "mlx_lm.models": models,
                        "serve_mlx_benchmark_trace": traced, "mlx_gemma3_precision": helper,
                        "mlx_prompt_token_trace": prompt_helper}.items():
        monkeypatch.setitem(sys.modules, name, value)
    path = Path(__file__).resolve().parents[1] / "scripts/serve_mlx_benchmark_precision.py"
    spec = importlib.util.spec_from_file_location("test_only_precision_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if fail:
        with pytest.raises(RuntimeError, match="synthetic server failure"):
            module.main()
    else:
        module.main()
    assert events == ["install", "trace_install", "serve", "trace_close", "close"]
