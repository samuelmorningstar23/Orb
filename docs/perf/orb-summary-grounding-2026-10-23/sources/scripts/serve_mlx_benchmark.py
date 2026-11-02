#!/usr/bin/env python3
"""Owned MLX-LM benchmark server with the pinned active Ollama sampler order.

Not a production provider or clinical-quality qualification. The installed
Ollama log explicitly records top-k -> top-p -> temperature; MLX-LM's native
order differs. Only that sampler factory is overridden, with all other engine
code and numerical/quantisation differences retained as candidate limitations.
"""
from __future__ import annotations

import importlib.metadata
import sys
from typing import Any


def sampler_factory(utils: Any, temp: float = 0.0, *, top_p: float = 0.0,
                    min_p: float = 0.0, min_tokens_to_keep: int = 1, top_k: int = 0,
                    xtc_probability: float = 0.0, xtc_threshold: float = .1,
                    xtc_special_tokens: list[int] | None = None) -> Any:
    if xtc_probability != 0 or min_p != 0 or min_tokens_to_keep != 1:
        raise ValueError("This benchmark only implements the pinned inactive min-p/XTC policy")
    if temp == 0:
        return utils.greedy_sampler

    def sample(logprobs: Any) -> Any:
        if top_k > 0:
            logprobs = utils.apply_top_k(logprobs, top_k)
        if 0 < top_p < 1:
            # MLX's implementation uses the surviving mass, so top-k is
            # correctly renormalised before the nucleus cutoff.
            logprobs = utils.apply_top_p(logprobs, top_p)
        return utils.categorical_sampling(logprobs, temp)

    return sample


def main() -> None:
    if importlib.metadata.version("mlx-lm") != "0.32.2":
        raise ValueError("This benchmark wrapper pins MLX-LM0.32.2")
    from mlx_lm import sample_utils, server

    def factory(temp: float = 0.0, **kwargs: Any) -> Any:
        return sampler_factory(sample_utils, temp, **kwargs)

    # server.py imports the factory into its own namespace. Leave installed
    # package files untouched; the override exists only in this owned process.
    server.make_sampler = factory
    print("Orb benchmark: active sampler order top-k -> top-p -> temperature; candidate, not clinical qualification", flush=True)
    server.main()


if __name__ == "__main__":
    sys.argv[0] = "orb-mlx-benchmark"
    main()
