"""Inspect local GGUF identity and MLX feasibility without loading model weights.

This is a metadata-only, offline preflight, not an inference benchmark or a
clinical-equivalence qualification. Package metadata is inspected without
importing MLX, torch, model code or any GPU runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import stat
import struct
import sys
from dataclasses import asdict
from importlib import metadata
from pathlib import Path
from typing import Any, BinaryIO

if __package__:
    from .serve_ollama_candidate import (
        MODEL_DIGEST,
        CandidateRefused,
        ModelSnapshot,
        inspect_memory,
        inspect_model,
        required_available_bytes,
    )
else:
    from serve_ollama_candidate import (
        MODEL_DIGEST,
        CandidateRefused,
        ModelSnapshot,
        inspect_memory,
        inspect_model,
        required_available_bytes,
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_METADATA_ENTRIES = 4096
MAX_ARRAY_ELEMENTS = 1_000_000
MAX_TOTAL_ARRAY_ELEMENTS = 2_000_000
MAX_TENSORS = 100_000
MAX_STRING_BYTES = 8 * 1024 * 1024
MAX_NAME_BYTES = 512
MAX_TENSOR_ELEMENTS = 1 << 40
MAX_TOTAL_ELEMENTS = 1 << 42
SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f",
           7: "?", 10: "Q", 11: "q", 12: "d"}
# GGML type code -> (name, elements per stored block, bytes per stored block).
# Unknown formats fail closed because their byte ranges cannot be validated.
TENSOR_TYPES = {0: ("F32", 1, 4), 1: ("F16", 1, 2), 2: ("Q4_0", 32, 18),
                3: ("Q4_1", 32, 20), 6: ("Q5_0", 32, 22), 7: ("Q5_1", 32, 24),
                8: ("Q8_0", 32, 34), 9: ("Q8_1", 32, 40), 10: ("Q2_K", 256, 84),
                11: ("Q3_K", 256, 110), 12: ("Q4_K", 256, 144),
                13: ("Q5_K", 256, 176), 14: ("Q6_K", 256, 210),
                15: ("Q8_K", 256, 292), 30: ("BF16", 1, 2)}


class PreflightRefused(ValueError):
    """The source cannot be inspected within the bounded preflight policy."""


class _Reader:
    def __init__(self, stream: BinaryIO, file_size: int):
        self.stream, self.file_size = stream, file_size
        self.array_elements = 0

    def _bound(self, size: int) -> None:
        end = self.stream.tell() + size
        if size < 0 or end > self.file_size or end > MAX_INDEX_BYTES:
            raise PreflightRefused("GGUF header/index is truncated or exceeds the bounded index limit.")

    def read(self, size: int) -> bytes:
        self._bound(size)
        value = self.stream.read(size)
        if len(value) != size:
            raise PreflightRefused("GGUF header/index is truncated.")
        return value

    def skip(self, size: int) -> None:
        self._bound(size)
        self.stream.seek(size, os.SEEK_CUR)

    def number(self, fmt: str) -> Any:
        return struct.unpack("<" + fmt, self.read(struct.calcsize("<" + fmt)))[0]

    def string(self, *, capture: bool, name: bool = False) -> str | dict[str, int]:
        size = self.number("Q")
        if size > (MAX_NAME_BYTES if name else MAX_STRING_BYTES):
            raise PreflightRefused("GGUF string exceeds its bounded size limit.")
        if not capture:
            self.skip(size)
            return {"bytes": size}
        try:
            return self.read(size).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PreflightRefused("GGUF metadata/name is not valid UTF-8.") from exc

    def value(self, kind: int, *, capture: bool) -> Any:
        if kind in SCALARS:
            value = self.number(SCALARS[kind])
            if isinstance(value, float) and not math.isfinite(value):
                raise PreflightRefused("Non-finite GGUF scalar metadata is unsupported.")
            return value if capture else None
        if kind == 8:
            return self.string(capture=capture)
        if kind != 9:
            raise PreflightRefused("Unknown GGUF metadata value type.")
        subtype, count = self.number("I"), self.number("Q")
        self.array_elements += count
        if count > MAX_ARRAY_ELEMENTS or self.array_elements > MAX_TOTAL_ARRAY_ELEMENTS:
            raise PreflightRefused("GGUF metadata array exceeds its bounded element limit.")
        if subtype in SCALARS:
            self.skip(struct.calcsize("<" + SCALARS[subtype]) * count)
        elif subtype == 8:
            for _ in range(count):
                self.string(capture=False)
        else:
            raise PreflightRefused("Nested or unknown GGUF array types are unsupported.")
        return {"element_type": subtype, "count": count}


def parse_gguf(stream: BinaryIO, file_size: int) -> dict[str, Any]:
    """Read only the bounded GGUF header and tensor directory; never its payload."""
    reader = _Reader(stream, file_size)
    if reader.read(4) != b"GGUF" or reader.number("I") != 3:
        raise PreflightRefused("This preflight requires little-endian GGUF version 3.")
    tensor_count, metadata_count = reader.number("Q"), reader.number("Q")
    if not 1 <= tensor_count <= MAX_TENSORS or metadata_count > MAX_METADATA_ENTRIES:
        raise PreflightRefused("GGUF entry counts exceed the bounded parser limits.")
    selected: dict[str, Any] = {}
    metadata_keys: set[str] = set()
    alignment = 32
    for _ in range(metadata_count):
        key = reader.string(capture=True, name=True)
        assert isinstance(key, str)
        if key in metadata_keys:
            raise PreflightRefused("Duplicate GGUF metadata key.")
        metadata_keys.add(key)
        capture = key.startswith(("general.", "gemma3.")) or key in {
            "tokenizer.ggml.bos_token_id", "tokenizer.ggml.eos_token_id",
            "tokenizer.ggml.padding_token_id", "tokenizer.ggml.unknown_token_id"}
        value = reader.value(reader.number("I"), capture=capture)
        if capture or key in {"tokenizer.ggml.tokens", "tokenizer.ggml.merges"}:
            selected[key] = value
        if key == "general.alignment":
            if type(value) is not int or not 1 <= value <= 4096 or value & (value - 1):
                raise PreflightRefused("GGUF alignment must be a bounded power of two.")
            alignment = value
    names: set[str] = set()
    ranges: list[tuple[int, int]] = []
    types: dict[str, dict[str, int | str]] = {}
    total_elements = text_elements = 0
    for _ in range(tensor_count):
        name = reader.string(capture=True, name=True)
        assert isinstance(name, str)
        if not name or name in names:
            raise PreflightRefused("Empty or duplicate GGUF tensor name.")
        names.add(name)
        dimensions = reader.number("I")
        if not 1 <= dimensions <= 4:
            raise PreflightRefused("GGUF tensors must have between one and four dimensions.")
        shape = [reader.number("Q") for _ in range(dimensions)]
        if any(dimension == 0 or dimension > (1 << 31) for dimension in shape):
            raise PreflightRefused("Invalid or oversized GGUF tensor dimension.")
        elements = math.prod(shape)
        if elements > MAX_TENSOR_ELEMENTS or total_elements + elements > MAX_TOTAL_ELEMENTS:
            raise PreflightRefused("GGUF tensor element counts exceed the bounded parser limits.")
        kind, offset = reader.number("I"), reader.number("Q")
        if kind not in TENSOR_TYPES:
            raise PreflightRefused("Unknown GGUF tensor type; byte ranges cannot be validated.")
        type_name, block_elements, block_bytes = TENSOR_TYPES[kind]
        if shape[0] % block_elements or offset % alignment:
            raise PreflightRefused("Misaligned GGUF tensor block or byte offset.")
        stored_bytes = elements // block_elements * block_bytes
        ranges.append((offset, offset + stored_bytes))
        row = types.setdefault(str(kind), {"name": type_name, "tensors": 0, "elements": 0,
                                         "stored_bytes": 0})
        for field, increment in (("tensors", 1), ("elements", elements), ("stored_bytes", stored_bytes)):
            row[field] = int(row[field]) + increment
        total_elements += elements
        if not name.startswith("v."):
            text_elements += elements
    index_end = stream.tell()
    data_start = (index_end + alignment - 1) // alignment * alignment
    previous_end = 0
    for start, end in sorted(ranges):
        if start < previous_end or end > file_size - data_start:
            raise PreflightRefused("GGUF tensor payload ranges overlap or exceed the file size.")
        previous_end = end
    # This digest covers only the inspected prefix, not any model weight bytes.
    stream.seek(0)
    digest = hashlib.sha256()
    remaining = index_end
    while remaining:
        chunk = reader.read(min(remaining, 1024 * 1024))
        digest.update(chunk)
        remaining -= len(chunk)
    return {"version": 3, "tensor_count": tensor_count, "metadata_count": metadata_count,
            "metadata": selected, "tensor_types": types, "total_tensor_elements": total_elements,
            "non_vision_prefix_tensor_elements": text_elements,
            "all_weights_fp16_expansion_bytes": total_elements * 2,
            "non_vision_prefix_fp16_expansion_bytes": text_elements * 2,
            "tensor_index_end_bytes": index_end, "tensor_payload_start_bytes": data_start,
            "header_and_tensor_index_sha256": digest.hexdigest(),
            "full_weight_sha256": None, "weight_payload_read": False,
            "fp16_estimate_note": "Two bytes per element; excludes KV, workspace, retrieval and conversion peaks."
            }


def _fingerprint(value: os.stat_result) -> dict[str, int]:
    return {"device": value.st_dev, "inode": value.st_ino, "size_bytes": value.st_size,
            "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}


def inspect_checkpoint(model_path: Path, models_dir: Path | None = None) -> dict[str, Any]:
    if not model_path.is_absolute():
        raise PreflightRefused("--model-path must be an absolute local path.")
    path = model_path.resolve(strict=True)
    if not stat.S_ISREG(path.stat().st_mode):
        raise PreflightRefused("Model path must identify a regular local file.")
    manifest: dict[str, Any] | None = None
    if models_dir is not None:
        if not models_dir.is_absolute():
            raise PreflightRefused("--models-dir must be absolute.")
        models_dir = models_dir.resolve(strict=True)
        pinned = inspect_model(models_dir)
        expected = models_dir / "blobs" / MODEL_DIGEST.replace(":", "-")
        if path != expected.resolve(strict=True):
            raise PreflightRefused("Model path does not match the pinned Ollama manifest blob.")
        manifest = asdict(pinned)
    with path.open("rb") as stream:
        before = _fingerprint(os.fstat(stream.fileno()))
        result = parse_gguf(stream, before["size_bytes"])
        after = _fingerprint(os.fstat(stream.fileno()))
    if before != after or before != _fingerprint(path.stat()):
        raise PreflightRefused("Model source changed during metadata inspection.")
    return {"path": str(path), "file_stat_fingerprint": before, "gguf": result,
            "pinned_ollama_manifest": manifest, "manifest_pin_verified": manifest is not None,
            "identity_assurance": "Manifest digest/size and stat/index fingerprint; full weight hash NOT verified."
            if manifest else "Stat/index fingerprint only; no manifest pin or full weight hash verified."}


def installed_packages() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in ("mlx", "mlx-metal", "mlx-lm", "mlx-vlm", "transformers", "torch", "safetensors"):
        try:
            result[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            result[name] = None
    return result


def build_report(model_path: Path, models_dir: Path | None = None) -> dict[str, Any]:
    checkpoint = inspect_checkpoint(model_path, models_dir)
    packages = installed_packages()
    memory = inspect_memory()
    weight_bytes = checkpoint["file_stat_fingerprint"]["size_bytes"]
    fp16_bytes = checkpoint["gguf"]["all_weights_fp16_expansion_bytes"]
    model = ModelSnapshot("metadata-only", "not-full-hash-verified", weight_bytes, "", "source")
    dequant = ModelSnapshot("metadata-only", "not-full-hash-verified", fp16_bytes, "", "FP16-estimate")
    budgets = [{"parallel": parallel,
                "existing_ollama_required_available_bytes": required_available_bytes(model, parallel),
                "existing_ollama_guard_passed": memory.estimated_available_bytes >= required_available_bytes(model, parallel),
                "fp16_same_guard_required_available_bytes": required_available_bytes(dequant, parallel),
                "fp16_same_guard_passed": memory.estimated_available_bytes >= required_available_bytes(dequant, parallel)}
               for parallel in (1, 2, 4)]
    reasons = []
    for name in ("mlx", "mlx-lm"):
        if packages[name] is None:
            reasons.append(f"{name}_not_installed_in_selected_interpreter")
    reasons.append("no_qualified_mlx_path_for_this_exact_gguf_quantization_and_tokenizer")
    if not budgets[0]["fp16_same_guard_passed"]:
        reasons.append("fp16_expansion_fails_existing_single_slot_headroom_guard")
    return {"schema_version": 1, "status": "metadata_preflight_complete",
            "offline": True, "models_loaded": False, "network_used": False,
            "python": sys.version, "platform": platform.platform(),
            "checkpoint": checkpoint, "installed_package_metadata": packages,
            "memory": asdict(memory), "memory_budgets": budgets,
            "budget_note": "Existing conservative guard: weights + 4 GiB KV/slot + 8 GiB prefix cache + 4 GiB workspace + 8 GiB OS headroom. FP16 is a comparison, not a measured MLX allocation.",
            "mlx_identical_checkpoint_ready": False, "mlx_block_reasons": reasons,
            "clinical_quality_qualified": False, "sla_benchmark_performed": False,
            "conversion_policy": "No conversion performed. Requantizing mixed Q4_K/Q6_K to MLX affine 4-bit is a numerical change requiring separate quality validation."}


def write_new_report(path: Path, report: dict[str, Any]) -> None:
    if not path.is_absolute() or path.suffix != ".json":
        raise PreflightRefused("--report must be a new absolute workspace JSON path.")
    path = path.resolve(strict=False)
    if not path.is_relative_to(REPO_ROOT):
        raise PreflightRefused("Report must be inside the repository workspace.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path,
                        help="Optional installed Ollama directory; enforces existing 27B manifest pin.")
    parser.add_argument("--report", type=Path, required=True,
                        help="New JSON file inside the workspace; existing files are never replaced.")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.model_path, args.models_dir)
        write_new_report(args.report, report)
    except (PreflightRefused, CandidateRefused, OSError, ValueError) as exc:
        print(json.dumps({"status": "refused", "error": str(exc), "models_loaded": False}), file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "report": str(args.report),
                      "models_loaded": False, "mlx_identical_checkpoint_ready": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
