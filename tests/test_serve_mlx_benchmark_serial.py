"""The serial diagnostic restores MLX's normal batching decision on exit."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.mark.parametrize("fail", [False, True])
def test_serial_diagnostic_preserves_arguments_and_restores(monkeypatch, fail):
    class Generator:
        def _is_batchable(self, args):
            return args

    original = Generator._is_batchable
    seen = []
    precision = types.ModuleType("serve_mlx_benchmark_precision")

    def serve():
        assert Generator()._is_batchable(object()) is False
        seen.append("serve")
        if fail:
            raise RuntimeError("synthetic server failure")

    precision.main = serve
    lm = types.ModuleType("mlx_lm")
    lm.server = types.SimpleNamespace(ResponseGenerator=Generator)
    monkeypatch.setitem(sys.modules, "mlx_lm", lm)
    monkeypatch.setitem(sys.modules, "serve_mlx_benchmark_precision", precision)
    path = Path(__file__).resolve().parents[1] / "scripts/serve_mlx_benchmark_serial.py"
    spec = importlib.util.spec_from_file_location("test_only_serial_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if fail:
        with pytest.raises(RuntimeError, match="synthetic server failure"):
            module.main()
    else:
        module.main()
    assert seen == ["serve"]
    assert Generator._is_batchable is original
