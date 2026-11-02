#!/usr/bin/env python3
"""Diagnostic single-request path for the unchanged Gemma3 benchmark.

This scopes MLX-LM's existing unbatched generator around the precision launcher.
Weights, prompts, context, sampling and quality checks remain unchanged. This
mode cannot demonstrate batched serving capacity.
"""
from __future__ import annotations


def main() -> None:
    import serve_mlx_benchmark_precision as precision
    from mlx_lm import server

    original = server.ResponseGenerator._is_batchable

    def serial_only(self, args):
        return False

    server.ResponseGenerator._is_batchable = serial_only
    try:
        print("Orb benchmark: diagnostic existing unbatched generator; unchanged model and sampler", flush=True)
        precision.main()
    finally:
        if server.ResponseGenerator._is_batchable is serial_only:
            server.ResponseGenerator._is_batchable = original


if __name__ == "__main__":
    main()
