#!/usr/bin/env python3
"""Offline, sub-kilobyte GGUF compatibility probe; never opens model weights.

This is a storage-format probe, not an inference or clinical-quality benchmark.
It loads five synthetic zero blocks through the available MLX runtime and
records whether that runtime keeps them packed or expands them to FP16.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORMATS = {"Q4_0": (2, 32, 18), "Q4_1": (3, 32, 20),
           "Q8_0": (8, 32, 34), "Q4_K": (12, 256, 144),
           "Q6_K": (14, 256, 210)}


def zero_gguf(name: str) -> bytes:
    """One 1x256 zero-valued tensor; the file has no model/tokenizer metadata."""
    kind, block_elements, block_bytes = FORMATS[name]
    label = b"probe.weight"
    header = b"GGUF" + struct.pack("<IQQ", 3, 1, 0)
    tensor = (struct.pack("<Q", len(label)) + label + struct.pack("<IQQIQ", 2, 256, 1, kind, 0))
    index = header + tensor
    return index + bytes((-len(index)) % 32) + bytes(256 // block_elements * block_bytes)


def run_probe() -> dict[str, Any]:
    # Import only when explicitly executing this small probe, not during tests
    # or metadata-only preflight. Sandbox access to Metal may need escalation.
    import mlx.core as mx

    report: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evidence_kind": "synthetic_gguf_format_probe_not_llm_inference",
        "offline": True, "clinical_model_opened": False,
        "mlx_version": importlib.metadata.version("mlx"),
        "mlx_metal_version": importlib.metadata.version("mlx-metal"),
        "platform": platform.platform(), "formats": {},
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    with tempfile.TemporaryDirectory(prefix="orb-mlx-format-") as directory:
        for name, (_kind, block_elements, block_bytes) in FORMATS.items():
            path = Path(directory) / (name + ".gguf")
            path.write_bytes(zero_gguf(name))
            arrays, metadata = mx.load(path, return_metadata=True)
            mx.eval(*arrays.values())
            descriptions = {key: {"shape": list(value.shape), "dtype": str(value.dtype),
                                  "bytes": value.nbytes} for key, value in arrays.items()}
            report["formats"][name] = {
                "synthetic_elements": 256,
                "packed_source_tensor_bytes": 256 // block_elements * block_bytes,
                "loaded_tensor_bytes": sum(value.nbytes for value in arrays.values()),
                "arrays": descriptions, "metadata_keys": sorted(metadata),
                "expanded_to_fp16": (len(arrays) == 1 and
                    next(iter(arrays.values())).dtype == mx.float16 and
                    next(iter(arrays.values())).size == 256),
            }
    report["k_quant_expansion_confirmed"] = all(
        report["formats"][name]["expanded_to_fp16"] for name in ("Q4_K", "Q6_K"))
    report["native_control_packing_confirmed"] = all(
        not report["formats"][name]["expanded_to_fp16"] for name in ("Q4_0", "Q4_1", "Q8_0"))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New JSON file in an existing directory")
    args = parser.parse_args()
    if not args.output.is_absolute() or args.output.exists() or not args.output.parent.is_dir():
        parser.error("Use a new absolute output file with an existing parent.")
    report = run_probe()
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"report": str(args.output),
                      "k_quant_expansion_confirmed": report["k_quant_expansion_confirmed"],
                      "native_control_packing_confirmed": report["native_control_packing_confirmed"]}))
    return 0 if report["k_quant_expansion_confirmed"] and report["native_control_packing_confirmed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
