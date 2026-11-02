#!/usr/bin/env python3
"""F32 residuals, F16 normalized projection inputs; benchmark candidate only."""
from __future__ import annotations


def main() -> None:
    import serve_mlx_benchmark_trace as traced
    from mlx_gemma3_f32_embedding import install_native_f32_embedding
    from mlx_gemma3_mixed_precision import install_mixed_projection_inputs
    from mlx_lm import server
    from mlx_lm.models import gemma3_text
    from mlx_prompt_token_trace import install_prompt_token_trace

    with (
        install_native_f32_embedding(gemma3_text, emit=traced.emit),
        install_mixed_projection_inputs(gemma3_text, emit=traced.emit),
        install_prompt_token_trace(server, traced.emit),
    ):
        traced.main()


if __name__ == "__main__":
    main()
