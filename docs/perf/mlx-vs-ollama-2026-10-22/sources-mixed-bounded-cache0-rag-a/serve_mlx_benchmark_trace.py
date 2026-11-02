#!/usr/bin/env python3
"""Startup-only, content-free measurements around an unchanged MLX server.

The parent benchmark selects standard or staged I/O using a pinned local
policy. This launcher never changes weights, dtypes, sampling, cache limits,
evaluation or networking. Hooks are removed after the first model load.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Sequence
from typing import Any


def emit(record: dict[str, Any]) -> None:
    print(json.dumps({"orb_mlx_startup_trace": True, **record}, allow_nan=False), flush=True)


def main(argv: Sequence[str] | None = None) -> None:
    mode = os.environ.get("ORB_MLX_STARTUP_TRACE_LOADING")
    if mode not in {"standard", "staged-uncached"}:
        raise ValueError("The benchmark must explicitly select the traced I/O policy")
    import mlx.core as mx
    import mlx.nn as nn
    from mlx_lm import server, utils
    from mlx_startup_trace import install_startup_trace

    inner = importlib.import_module(
        "serve_mlx_benchmark_staged" if mode == "staged-uncached" else "serve_mlx_benchmark")
    trace = install_startup_trace(mx, nn, utils, emit, server=server)
    original_argv = sys.argv
    if argv is not None:
        sys.argv = ["orb-mlx-startup-trace", *argv]
    try:
        inner.main()
    finally:
        trace.close()
        sys.argv = original_argv


if __name__ == "__main__":
    main()
