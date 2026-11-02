#!/usr/bin/env python3
"""Experimental native-like F32 Gemma3 activation path; unchanged weights."""
from __future__ import annotations


def main() -> None:
    import serve_mlx_benchmark_precision as precision
    import serve_mlx_benchmark_trace as traced
    from mlx_gemma3_f32_embedding import install_native_f32_embedding
    from mlx_lm.models import gemma3_text

    repair = install_native_f32_embedding(gemma3_text, emit=traced.emit)
    try:
        precision.main()
    finally:
        repair.close()


if __name__ == "__main__":
    main()
