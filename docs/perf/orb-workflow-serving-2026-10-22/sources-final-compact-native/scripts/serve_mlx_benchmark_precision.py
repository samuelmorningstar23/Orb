#!/usr/bin/env python3
"""Same-weight Gemma3 norm dtype repair, with startup-only memory tracing."""
from __future__ import annotations

from mlx_gemma3_precision import install_norm_dtype_restore
from mlx_prompt_token_trace import install_prompt_token_trace


def main() -> None:
    import serve_mlx_benchmark_trace as traced
    from mlx_lm import server
    from mlx_lm.models import gemma3_text

    repair = install_norm_dtype_restore(gemma3_text, emit=traced.emit)
    prompt_trace = install_prompt_token_trace(server, traced.emit)
    try:
        traced.main()
    finally:
        prompt_trace.close()
        repair.close()


if __name__ == "__main__":
    main()
