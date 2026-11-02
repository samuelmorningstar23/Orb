"""Bounded CPU-only original GGUF / bundled C decoder / SF-row audit.

Never imports MLX, initializes a model/GPU, performs inference, downloads or
changes checkpoints. Each read/decode is at most one selected matrix row.
An unavailable/incompatible native dylib is reported explicitly; the Python
GGUF-to-safetensors check still runs and never masquerades as native parity.
The bundled x86_64 library is not the ARM runner's embedded implementation.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import struct
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

SOURCE_SHA256 = "4b97da6b21a8fb1405a0d1efee462bfd6c4d3c46307bac8b34373835f07ca758"
SOURCE_BYTES = 17_397_211_520
LABEL = "medgemma-27b-text-mlx-q4-integer-repack-q6-fp16-candidate"
LIBRARY = Path("/Applications/Ollama.app/Contents/Resources/libggml-base.0.26.0.dylib")
MAX_COLUMNS = 21_504
HEADER_LIMIT = 16 * 1024
_KINDS = {12: (144, "dequantize_row_q4_K"), 14: (210, "dequantize_row_q6_K")}
_NAMES = {"attn_q": "self_attn.q_proj", "attn_k": "self_attn.k_proj",
          "attn_v": "self_attn.v_proj", "attn_output": "self_attn.o_proj",
          "ffn_gate": "mlp.gate_proj", "ffn_up": "mlp.up_proj", "ffn_down": "mlp.down_proj"}


class ProbeRefused(ValueError):
    """The selected bounded row cannot safely be inspected."""


def validate_packed(raw: bytes, kind: int, columns: int) -> None:
    if (type(columns) is not int or not 256 <= columns <= MAX_COLUMNS or columns % 256
            or kind not in _KINDS or len(raw) != columns // 256 * _KINDS[kind][0]):
        raise ProbeRefused("Only complete bounded row-aligned Q4_K/Q6_K input is supported.")


class NativeDecoder:
    def __init__(self, library: Path, *, loader: Callable[[str], Any] = ctypes.CDLL):
        self.library = loader(str(library))
        self.functions = {}
        for kind, (_, name) in _KINDS.items():
            function = getattr(self.library, name)
            # GGML C API: void dequantize_row_q{4,6}_K(const block*, float*, int64_t).
            function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int64]
            function.restype = None
            self.functions[kind] = function

    def decode(self, raw: bytes, kind: int, columns: int) -> np.ndarray:
        validate_packed(raw, kind, columns)
        source = ctypes.create_string_buffer(raw, len(raw))
        output = (ctypes.c_float * columns)()
        self.functions[kind](ctypes.cast(source, ctypes.c_void_p), output, columns)
        result = np.ctypeslib.as_array(output).copy()
        if not np.isfinite(result).all():
            raise ProbeRefused("The native decoder returned non-finite values.")
        return result


def error_metrics(reference: np.ndarray, actual: np.ndarray) -> dict[str, Any]:
    if reference.shape != actual.shape or not np.isfinite(reference).all() or not np.isfinite(actual).all():
        raise ProbeRefused("Comparison arrays must have matching finite shapes.")
    difference = actual.astype(np.float64) - reference.astype(np.float64)
    denominator = float(np.sum(reference.astype(np.float64) ** 2))
    squared = float(np.sum(difference ** 2))
    return {"values": int(reference.size), "exact_values": int(np.count_nonzero(reference == actual)),
            "max_abs_error": float(np.abs(difference).max()), "rmse": math.sqrt(squared / reference.size),
            "relative_l2": math.sqrt(squared / denominator) if denominator else (0.0 if squared == 0 else None)}


def selected_rows(rows: int) -> list[int]:
    if type(rows) is not int or rows < 1:
        raise ProbeRefused("A positive matrix row count is required.")
    return sorted({0, rows // 2, rows - 1})


def selected_matrices() -> list[tuple[str, str]]:
    pairs = [("token_embd.weight", "model.embed_tokens.weight")]
    for layer in (0, 5, 61):
        pairs.extend((f"blk.{layer}.{source}.weight", f"model.layers.{layer}.{target}.weight")
                     for source, target in _NAMES.items())
    return pairs


def read_header(file: Path) -> tuple[dict[str, Any], int]:
    with file.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise ProbeRefused("Truncated safetensors header.")
        length = struct.unpack("<Q", prefix)[0]
        if not 2 <= length <= HEADER_LIMIT:
            raise ProbeRefused("Safetensors metadata exceeds the bounded limit.")
        raw = stream.read(length)
    header = json.loads(raw)
    if not isinstance(header, dict):
        raise ProbeRefused("Safetensors metadata must be an object.")
    header.pop("__metadata__", None)
    return header, 8 + length


def read_sf_row(file: Path, header: dict[str, Any], data_at: int, name: str,
                row: int, expected_shape: tuple[int, int], dtype: str) -> np.ndarray:
    entry = header.get(name)
    widths = {"U32": ("<u4", 4), "F16": ("<f2", 2)}
    if dtype not in widths:
        raise ProbeRefused("Only packed uint32 and dense/scalar FP16 rows are selected.")
    numpy_dtype, width = widths[dtype]
    if (not isinstance(entry, dict) or entry.get("dtype") != dtype
            or entry.get("shape") != list(expected_shape) or not 0 <= row < expected_shape[0]
            or not 1 <= expected_shape[1] <= MAX_COLUMNS):
        raise ProbeRefused("Candidate row shape/dtype differs from the selected source.")
    offsets = entry.get("data_offsets")
    if (not isinstance(offsets, list) or len(offsets) != 2 or any(type(value) is not int for value in offsets)
            or offsets[0] < 0 or offsets[1] - offsets[0] != math.prod(expected_shape) * width
            or data_at + offsets[1] > file.stat().st_size):
        raise ProbeRefused("Candidate row byte span is invalid.")
    offset = data_at + offsets[0] + row * expected_shape[1] * width
    mapped: Any = np.memmap(file, mode="r", dtype=numpy_dtype, offset=offset, shape=(expected_shape[1],))
    result = mapped.copy()
    mapped._mmap.close()
    return result


def candidate_row(file: Path, target: str, shape: tuple[int, int], kind: int, row: int) -> tuple[np.ndarray, str, int]:
    header, data_at = read_header(file)
    rows, columns = shape
    if not 256 <= columns <= MAX_COLUMNS or columns % 256:
        raise ProbeRefused("Candidate row exceeds the pinned source bounds.")
    if kind == 14:
        if set(header) != {target}:
            raise ProbeRefused("Dense Q6 source candidate must own exactly its matrix.")
        values = read_sf_row(file, header, data_at, target, row, shape, "F16")
        return values.astype(np.float32), hashlib.sha256(values.tobytes()).hexdigest(), values.nbytes
    if kind != 12:
        raise ProbeRefused("Only Q4_K/Q6_K matrices are selected.")
    prefix = target.removesuffix(".weight")
    if set(header) != {target, prefix + ".scales", prefix + ".biases"}:
        raise ProbeRefused("Packed Q4 candidate must own weight/scales/biases only.")
    weight = read_sf_row(file, header, data_at, target, row, (rows, columns // 8), "U32")
    scale = read_sf_row(file, header, data_at, prefix + ".scales", row, (rows, columns // 32), "F16")
    bias = read_sf_row(file, header, data_at, prefix + ".biases", row, (rows, columns // 32), "F16")
    shifts: np.ndarray = np.arange(0, 32, 4, dtype=np.uint32)
    codes = ((weight[:, None] >> shifts) & 15).reshape(columns // 32, 32).astype(np.float32)
    values = (codes * scale.astype(np.float32)[:, None] + bias.astype(np.float32)[:, None]).reshape(columns)
    raw = weight.tobytes() + scale.tobytes() + bias.tobytes()
    return values, hashlib.sha256(raw).hexdigest(), len(raw)


def run_probe(source: Path, candidate: Path, library: Path) -> dict[str, Any]:
    from gguf import GGUFReader, dequantize

    manifest_raw = (candidate / "conversion-manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    if (source.name != "sha256-" + SOURCE_SHA256 or source.stat().st_size != SOURCE_BYTES
            or manifest.get("source_sha256") != SOURCE_SHA256 or manifest.get("source_bytes") != SOURCE_BYTES
            or manifest.get("candidate_label") != LABEL or manifest.get("quantisation_profile") != "source-repack4-f16"
            or manifest.get("status") != "converted_candidate_unqualified" or (candidate / ".conversion-incomplete").exists()):
        raise ProbeRefused("The complete pinned original source/repack candidate is required.")
    native_info: dict[str, Any] = {"path": str(library), "arm_runner_kernel_parity_proven": False}
    native = None
    if library.is_file():
        native_info.update(bytes=library.stat().st_size, sha256=hashlib.sha256(library.read_bytes()).hexdigest())
        try:
            native = NativeDecoder(library)
        except (OSError, AttributeError) as exc:
            native_info.update(status="unavailable", reason=str(exc))
        else:
            native_info["status"] = "available_bundled_cpu_decoder"
    else:
        native_info.update(status="unavailable", reason="Bundled CPU library is absent.")
    reader = GGUFReader(str(source), mode="r")
    tensors = {tensor.name: tensor for tensor in reader.tensors}
    index = json.loads((candidate / "model.safetensors.index.json").read_text())["weight_map"]
    rows_output = []
    source_read = candidate_read = max_decoded = 0
    for source_name, target in selected_matrices():
        tensor = tensors[source_name]
        shape = tuple(int(value) for value in reversed(tensor.shape))
        if len(shape) != 2:
            raise ProbeRefused("Selected GGUF tensor is not a matrix.")
        kind = int(tensor.tensor_type)
        file_name = index[target]
        if not isinstance(file_name, str) or Path(file_name).name != file_name:
            raise ProbeRefused("A selected checkpoint filename escapes the candidate.")
        file = candidate / file_name
        if file.stat().st_size != manifest["files"][file_name]["bytes"]:
            raise ProbeRefused("Selected candidate file size differs from its manifest.")
        for row in selected_rows(shape[0]):
            packed = np.ascontiguousarray(tensor.data[row:row + 1])
            raw = packed.tobytes()
            validate_packed(raw, kind, shape[1])
            reference = dequantize(packed, tensor.tensor_type).reshape(shape[1])
            if reference.dtype != np.float32 or not np.isfinite(reference).all():
                raise ProbeRefused("Python GGUF returned an invalid finite float32 row.")
            decoded, selected_sha, selected_bytes = candidate_row(file, target, shape, kind, row)
            item = {"source": source_name, "target": target, "shape": list(shape), "row": row,
                    "source_type": kind, "source_packed_row_sha256": hashlib.sha256(raw).hexdigest(),
                    "candidate_selected_row_sha256": selected_sha,
                    "candidate_vs_python_gguf": error_metrics(reference, decoded)}
            if native is not None:
                c_reference = native.decode(raw, kind, shape[1])
                item["native_vs_python_gguf"] = error_metrics(reference, c_reference)
                item["candidate_vs_native"] = error_metrics(c_reference, decoded)
            rows_output.append(item)
            source_read += len(raw)
            candidate_read += selected_bytes
            max_decoded = max(max_decoded, reference.nbytes)
    return {"created_at": datetime.now(UTC).isoformat(), "status": "bounded_rows_checked",
            "scope": "One first/middle/last row for embedding and Q/K/V/O/gate/up/down in layers0,5,61; not a full checkpoint audit",
            "cpu_only": True, "mlx_imported": any(name == "mlx" or name.startswith("mlx.") for name in sys.modules),
            "model_loaded": False, "gpu_used": False,
            "clinical_quality_qualified": False, "source_claimed_sha256": SOURCE_SHA256,
            "full_source_rehashed": False, "full_candidate_weights_rehashed": False,
            "candidate_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "candidate_label": LABEL, "native_decoder": native_info,
            "matrix_count": len(selected_matrices()), "row_count": len(rows_output),
            "source_packed_payload_bytes_read": source_read, "candidate_payload_bytes_read": candidate_read,
            "largest_single_decoded_row_bytes": max_decoded, "rows": rows_output,
            "limitations": ["CPU affine decoding does not exercise MLX Metal kernels, activation arithmetic, caches, sampling or model quality.",
                            "A compatible bundled native CPU decoder, if available, is not an ARM runtime kernel identity proof.",
                            "Selected row digests are observations; full existing source/checkpoint digests were not recomputed."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--native-library", type=Path, default=LIBRARY)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run_probe(args.source, args.candidate, args.native_library)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": result["status"], "matrices": result["matrix_count"], "rows": result["row_count"],
                      "native_decoder_status": result["native_decoder"]["status"],
                      "maximum_relative_l2": max(item["candidate_vs_python_gguf"]["relative_l2"] or 0 for item in result["rows"])}))


if __name__ == "__main__":
    main()
