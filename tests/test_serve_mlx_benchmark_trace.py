"""Trace launcher forwards original CLI/engine calls without importing MLX."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import serve_mlx_benchmark_trace as launcher


@pytest.mark.parametrize("mode,inner", [
    ("standard", "serve_mlx_benchmark"), ("staged-uncached", "serve_mlx_benchmark_staged")])
@pytest.mark.parametrize("fail", [False, True])
def test_original_arguments_and_lifecycle_are_preserved(monkeypatch, mode, inner, fail):
    monkeypatch.setenv("ORB_MLX_STARTUP_TRACE_LOADING", mode)
    mx, nn = types.ModuleType("mlx.core"), types.ModuleType("mlx.nn")
    mlx = types.ModuleType("mlx")
    mlx.core, mlx.nn = mx, nn
    server, utils = object(), object()
    lm = types.ModuleType("mlx_lm")
    lm.server, lm.utils = server, utils
    events = []
    handle = types.SimpleNamespace(close=lambda: events.append("close"))
    helper = types.ModuleType("mlx_startup_trace")

    def install(*args, **kwargs):
        assert args == (mx, nn, utils, launcher.emit)
        assert kwargs == {"server": server}
        events.append("install")
        return handle

    helper.install_startup_trace = install
    original = sys.argv
    arguments = ["--model", "/synthetic/local-model", "--decode-concurrency", "8"]
    backend = types.ModuleType(inner)

    def main():
        assert sys.argv[1:] == arguments
        events.append("inner")
        if fail:
            raise RuntimeError("synthetic failure")

    backend.main = main
    for name, value in {"mlx": mlx, "mlx.core": mx, "mlx.nn": nn,
                        "mlx_lm": lm, "mlx_startup_trace": helper, inner: backend}.items():
        monkeypatch.setitem(sys.modules, name, value)
    if fail:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            launcher.main(arguments)
    else:
        launcher.main(arguments)
    assert sys.argv is original and events == ["install", "inner", "close"]


def test_trace_loading_requires_explicit_policy_before_imports(monkeypatch):
    monkeypatch.delenv("ORB_MLX_STARTUP_TRACE_LOADING", raising=False)
    with pytest.raises(ValueError, match="explicitly select"):
        launcher.main([])
