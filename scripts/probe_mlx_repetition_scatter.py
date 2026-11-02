"""Tiny synthetic installed-MLX penalty check; no model/weights/network.

Defaults to CPU. GPU is an explicit separate invocation owned by the caller.
Tests duplicate scatter indices against one sign-aware penalty application per
unique token in the unchanged final256 history. It does not establish native
Ollama sampling parity or clinical quality.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np


def cases() -> list[tuple[str, list[int]]]:
    return [("positive_duplicate256", [1] * 256), ("negative_duplicate256", [2] * 256),
            ("mixed_duplicate256", [1, 2, 3, 4] * 64),
            ("tail256_excludes_earlier_token", [5] * 513 + [1, 2] * 128)]


def run_probe(device: str) -> dict[str, Any]:
    if device not in {"cpu", "gpu"}:
        raise ValueError("Only explicit CPU/GPU tiny synthetic devices are supported.")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import mlx.core as mx
    mx.set_default_device(mx.cpu if device == "cpu" else mx.gpu)
    from mlx_lm import sample_utils

    original = np.array([[0., 8., -8., 4., -4., 12., -12., 2.]], dtype=np.float32)
    rows = []
    for name, tokens in cases():
        unique = sorted(set(tokens[-256:]))
        for dtype_name, dtype in (("float16", mx.float16), ("float32", mx.float32)):
            for penalty in (1., 1.15, 2.):
                for compiled in (False, True):
                    processor = sample_utils.make_repetition_penalty(penalty, 256)
                    if compiled:
                        processor = mx.compile(processor)
                    duplicate_logits = processor(mx.array(tokens, dtype=mx.int32), mx.array(original, dtype=dtype))
                    unique_logits = processor(mx.array(unique, dtype=mx.int32), mx.array(original, dtype=dtype))
                    mx.eval(duplicate_logits, unique_logits)
                    actual = np.array(duplicate_logits).astype(np.float32)
                    deduplicated = np.array(unique_logits).astype(np.float32)
                    expected = original.copy()
                    expected[:, unique] = np.where(expected[:, unique] < 0,
                                                    expected[:, unique] * penalty,
                                                    expected[:, unique] / penalty)
                    tolerance = .01 if dtype_name == "float16" else 2e-6
                    max_error = float(np.abs(actual - expected).max())
                    equal = bool(np.array_equal(actual, deduplicated))
                    rows.append({"case": name, "history_count": len(tokens), "effective_unique_count": len(unique),
                                 "dtype": dtype_name, "penalty": penalty, "compiled": compiled,
                                 "duplicate_vs_unique_exact": equal, "max_abs_error_vs_single_application": max_error,
                                 "absolute_tolerance": tolerance, "passed": equal and max_error <= tolerance})
    return {"created_at": datetime.now(UTC).isoformat(), "status": "complete", "device": device,
            "mlx_lm_version": importlib.metadata.version("mlx-lm"), "mlx_version": importlib.metadata.version("mlx"),
            "sample_utils_sha256": hashlib.sha256(Path(sample_utils.__file__).read_bytes()).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "synthetic_data_only": True, "model_loaded": False, "weights_read": False,
            "native_ollama_sampling_parity_proven": False, "clinical_quality_qualified": False,
            "all_passed": all(row["passed"] for row in rows), "checks": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_probe(args.device)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"device": result["device"], "checks": len(result["checks"]), "all_passed": result["all_passed"]}))


if __name__ == "__main__":
    main()
