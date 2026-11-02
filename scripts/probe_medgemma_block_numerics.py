#!/usr/bin/env python3
"""Offline diagnostic: selected embeddings and a streamed Gemma3 block prefix.

The independent NumPy reference uses original GGUF dequantized float32 weights.
MLX uses the candidate's existing weights and installed Gemma3 arithmetic. Each
source matrix is dequantized in bounded row chunks; only one target matrix is
loaded at a time, even for all 62 blocks. This does not instantiate a full model,
generate text, or establish clinical equivalence.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

SOURCE_SHA256 = "4b97da6b21a8fb1405a0d1efee462bfd6c4d3c46307bac8b34373835f07ca758"
MAX_CHUNK_BYTES = 32 * 1024 * 1024
DTYPES: dict[str, Any] = {"F16": np.dtype("<f2"), "F32": np.dtype("<f4"), "U32": np.dtype("<u4")}


def read_rows(path: Path, name: str, rows: list[int] | None = None) -> np.ndarray:
    """Read a tiny vector or explicitly selected rows without mapping a matrix."""
    with path.open("rb") as stream:
        raw = stream.read(8)
        if len(raw) != 8:
            raise ValueError("Truncated safetensors prefix")
        n = struct.unpack("<Q", raw)[0]
        if not 1 <= n <= 1024 * 1024:
            raise ValueError("Unbounded safetensors header")
        header = json.loads(stream.read(n))
        spec = header[name]
        dtype = DTYPES[spec["dtype"]]
        shape = tuple(spec["shape"])
        start, end = spec["data_offsets"]
        if end - start != math.prod(shape) * dtype.itemsize:
            raise ValueError("Incorrect tensor byte extent")
        base = 8 + n + start
        if rows is None:
            if len(shape) != 1 or end - start > 1024 * 1024:
                raise ValueError("Only small vectors can be read whole")
            stream.seek(base)
            return np.frombuffer(stream.read(end - start), dtype=dtype).copy()
        if len(shape) != 2 or not 1 <= len(rows) <= 32:
            raise ValueError("Select between 1 and 32 matrix rows")
        width = shape[1] * dtype.itemsize
        result = []
        for row in rows:
            if not 0 <= row < shape[0]:
                raise ValueError("Matrix row outside tensor")
            stream.seek(base + row * width)
            data = stream.read(width)
            if len(data) != width:
                raise ValueError("Truncated matrix row")
            result.append(np.frombuffer(data, dtype=dtype).copy())
        return np.stack(result)


def metrics(reference: np.ndarray, actual: np.ndarray) -> dict[str, Any]:
    reference = np.asarray(reference, dtype=np.float32)
    actual = np.asarray(actual, dtype=np.float32)
    if reference.shape != actual.shape:
        raise ValueError("Metric shapes differ")
    finite = np.isfinite(reference) & np.isfinite(actual)
    result: dict[str, Any] = {
        "shape": list(reference.shape),
        "reference_nonfinite": int((~np.isfinite(reference)).sum()),
        "actual_nonfinite": int((~np.isfinite(actual)).sum()),
        "actual_max_abs": float(np.max(np.abs(actual[np.isfinite(actual)])))
        if np.isfinite(actual).any() else None,
        "reference_min": float(np.min(reference[np.isfinite(reference)])) if np.isfinite(reference).any() else None,
        "reference_max": float(np.max(reference[np.isfinite(reference)])) if np.isfinite(reference).any() else None,
        "actual_min": float(np.min(actual[np.isfinite(actual)])) if np.isfinite(actual).any() else None,
        "actual_max": float(np.max(actual[np.isfinite(actual)])) if np.isfinite(actual).any() else None,
        "actual_abs_ge_fp16_max": int((np.abs(actual) >= 65504).sum()),
    }
    if finite.all():
        a: Any = reference.astype(np.float64).ravel()
        b: Any = actual.astype(np.float64).ravel()
        denominator = float(np.linalg.norm(a))
        result.update({
            "max_abs_error": float(np.max(np.abs(a - b))),
            "relative_l2": float(np.linalg.norm(a - b) / max(denominator, 1e-30)),
            "cosine_similarity": float(np.dot(a, b) / max(denominator * np.linalg.norm(b), 1e-30)),
        })
    return result


def source_linear(tensor: Any, inputs: np.ndarray, dequantize: Any) -> np.ndarray:
    rows, columns = reversed(tuple(int(x) for x in tensor.shape))
    if inputs.ndim != 2 or inputs.shape[1] != columns or inputs.dtype != np.float32:
        raise ValueError("Reference input must be float32 token rows")
    result: Any = np.empty((len(inputs), rows), dtype=np.float32)
    chunk_rows = max(1, MAX_CHUNK_BYTES // (4 * columns))
    for start in range(0, rows, chunk_rows):
        end = min(rows, start + chunk_rows)
        weight = dequantize(tensor.data[start:end], tensor.tensor_type)
        if weight.dtype != np.float32 or weight.shape != (end - start, columns):
            raise ValueError("Invalid bounded source dequantization")
        result[:, start:end] = inputs @ weight.T
        del weight
    return result


def rms_reference(x: np.ndarray, weight: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    return (x / np.sqrt(np.mean(x * x, axis=-1, keepdims=True) + eps)) * weight


def rope_reference(x: np.ndarray, base: float = 10_000.0, scale: float = 1.0) -> np.ndarray:
    length, _, dim = x.shape
    inverse = np.power(np.float32(base), -np.arange(0, dim, 2, dtype=np.float32) / dim)
    theta = (np.arange(length, dtype=np.float32) * np.float32(scale))[:, None, None] * inverse[None, None, :]
    first, second = np.split(x, 2, axis=-1)
    return np.concatenate((first * np.cos(theta) - second * np.sin(theta),
                           second * np.cos(theta) + first * np.sin(theta)), axis=-1)


def attention_reference(q: np.ndarray, k: np.ndarray, v: np.ndarray) -> np.ndarray:
    length = len(q)
    q = q.reshape(length, 32, 128).transpose(1, 0, 2)
    k = np.repeat(k.reshape(length, 16, 128).transpose(1, 0, 2), 2, axis=0)
    v = np.repeat(v.reshape(length, 16, 128).transpose(1, 0, 2), 2, axis=0)
    score = (q @ k.transpose(0, 2, 1)) * np.float32(168**-0.5)
    score = np.where(np.tri(length, dtype=bool)[None, :, :], score, -np.inf)
    score = np.exp(score - np.max(score, axis=-1, keepdims=True))
    score /= np.sum(score, axis=-1, keepdims=True)
    return (score @ v).transpose(1, 0, 2).reshape(length, 4096)


def block_schedule(blocks: int) -> list[dict[str, Any]]:
    if type(blocks) is not int or not 1 <= blocks <= 62:
        raise ValueError("Evaluate between 1 and 62 consecutive blocks")
    return [{"layer": layer, "source_prefix": f"blk.{layer}",
             "is_global": (layer + 1) % 6 == 0,
             "rope_base": 1_000_000.0 if (layer + 1) % 6 == 0 else 10_000.0,
             "rope_scale": 0.125 if (layer + 1) % 6 == 0 else 1.0}
            for layer in range(blocks)]


def evaluate_prefix(blocks: int, reference: Any, actual: Any,
                    evaluate_block: Callable[[dict[str, Any], Any, Any], tuple[Any, Any]]) -> tuple[Any, Any]:
    """Carry both numerical states across the exact source block sequence."""
    for metadata in block_schedule(blocks):
        reference, actual = evaluate_block(metadata, reference, actual)
    return reference, actual


def run(source: Path, candidate: Path, ids: list[int], device: str, *, blocks: int = 1,
        head_ids: list[int] | None = None) -> dict[str, Any]:
    import mlx.core as mx
    import mlx.nn as nn
    from gguf import GGUFReader, dequantize

    if not 1 <= len(ids) <= 32 or any(type(x) is not int or not 0 <= x < 262208 for x in ids):
        raise ValueError("Use 1 to 32 valid token IDs")
    schedule = block_schedule(blocks)
    if head_ids is not None and (not 1 <= len(head_ids) <= 32 or
                                any(type(x) is not int or not 0 <= x < 262208 for x in head_ids)):
        raise ValueError("Select 1 to 32 valid head token rows; full vocabulary projection is forbidden")
    manifest = json.loads((candidate / "conversion-manifest.json").read_text())
    if manifest["source_sha256"] != SOURCE_SHA256 or source.stat().st_size != 17_397_211_520:
        raise ValueError("Pinned source metadata differs; caller must already verify its hash")
    index = json.loads((candidate / "model.safetensors.index.json").read_text())["weight_map"]
    mx.set_default_device(mx.cpu if device == "cpu" else mx.gpu)
    reader = GGUFReader(str(source), mode="r")
    tensors = {tensor.name: tensor for tensor in reader.tensors}
    records: list[dict[str, Any]] = []

    def record(stage: str, reference: np.ndarray, actual: Any, **extra: Any) -> None:
        mx.eval(actual)
        records.append({"stage": stage, **metrics(reference, np.asarray(actual)), **extra})
        print(json.dumps({"stage": stage, "relative_l2": records[-1].get("relative_l2"),
                          "actual_nonfinite": records[-1]["actual_nonfinite"]}), flush=True)

    def norm(name: str, reference: np.ndarray, actual: Any) -> tuple[np.ndarray, Any]:
        source_weight = np.asarray(tensors[name + ".weight"].data, dtype=np.float32)
        from convert_medgemma_gguf_mlx import tensor_mapping
        mapped = tensor_mapping(name + ".weight")
        assert mapped is not None
        target = mapped[0]
        weight = mx.array(read_rows(candidate / index[target], target))
        new_reference = rms_reference(reference, source_weight)
        new_actual = mx.fast.rms_norm(actual, 1.0 + weight, 1e-6).astype(actual.dtype)
        record(name, new_reference, new_actual)
        return new_reference, new_actual

    def linear(name: str, reference: np.ndarray, actual: Any) -> tuple[np.ndarray, Any]:
        from convert_medgemma_gguf_mlx import tensor_mapping
        mapped = tensor_mapping(name + ".weight")
        assert mapped is not None
        target = mapped[0]
        mx.eval(actual)
        both = np.concatenate((reference, np.asarray(actual).astype(np.float32)), axis=0)
        projected = source_linear(tensors[name + ".weight"], both, dequantize)
        new_reference, same_input = np.split(projected, 2, axis=0)
        weights = mx.load(str(candidate / index[target]))
        prefix = target.removesuffix(".weight")
        if prefix + ".scales" in weights:
            output = mx.quantized_matmul(actual, weights[target], scales=weights[prefix + ".scales"],
                                         biases=weights[prefix + ".biases"], transpose=True,
                                         group_size=32, bits=4, mode="affine")
        else:
            output = actual @ weights[target].T
        mx.eval(output)
        record(name, new_reference, output, same_input=metrics(same_input, np.asarray(output)),
               input_dtype=str(actual.dtype), output_dtype=str(output.dtype))
        del weights, both, projected
        gc.collect()
        mx.clear_cache()
        return new_reference, output

    embedding_tensor = tensors["token_embd.weight"]
    emb_ref = np.concatenate([dequantize(embedding_tensor.data[i:i + 1], embedding_tensor.tensor_type)
                              for i in ids], axis=0)
    target = "model.embed_tokens.weight"
    emb_actual = mx.array(read_rows(candidate / index[target], target, ids))
    record("embedding_selected_rows_storage", emb_ref, emb_actual)
    reference = emb_ref * np.float32(math.sqrt(5376))
    actual = emb_actual * mx.array(math.sqrt(5376), mx.bfloat16).astype(emb_actual.dtype)
    record("embedding_gain", reference, actual)

    def evaluate_block(metadata: dict[str, Any], reference: np.ndarray, actual: Any) -> tuple[np.ndarray, Any]:
        prefix = metadata["source_prefix"]
        base, scale = metadata["rope_base"], metadata["rope_scale"]
        residual_ref, residual_actual = reference, actual
        reference, actual = norm(prefix + ".attn_norm", reference, actual)
        q_ref, q = linear(prefix + ".attn_q", reference, actual)
        k_ref, k = linear(prefix + ".attn_k", reference, actual)
        v_ref, v = linear(prefix + ".attn_v", reference, actual)
        q_ref, q = norm(prefix + ".attn_q_norm", q_ref.reshape(len(ids), 32, 128), q.reshape(len(ids), 32, 128))
        k_ref, k = norm(prefix + ".attn_k_norm", k_ref.reshape(len(ids), 16, 128), k.reshape(len(ids), 16, 128))
        q_ref, k_ref = rope_reference(q_ref, base, scale), rope_reference(k_ref, base, scale)
        q = mx.fast.rope(q.transpose(1, 0, 2)[None], dims=128, traditional=False, base=base, scale=scale, offset=0)
        k = mx.fast.rope(k.transpose(1, 0, 2)[None], dims=128, traditional=False, base=base, scale=scale, offset=0)
        record(prefix + ".q_rope", q_ref, q[0].transpose(1, 0, 2), **metadata)
        record(prefix + ".k_rope", k_ref, k[0].transpose(1, 0, 2), **metadata)
        reference = attention_reference(q_ref, k_ref, v_ref)
        v = v.reshape(len(ids), 16, 128).transpose(1, 0, 2)[None]
        actual = mx.fast.scaled_dot_product_attention(q, k, v, scale=168**-0.5, mask="causal")
        actual = actual[0].transpose(1, 0, 2).reshape(len(ids), 4096)
        record(prefix + ".attention", reference, actual, **metadata)
        reference, actual = linear(prefix + ".attn_output", reference, actual)
        reference, actual = norm(prefix + ".post_attention_norm", reference, actual)
        reference = residual_ref + reference
        actual = mx.clip(residual_actual.astype(mx.float32) + actual.astype(mx.float32), -65504, 65504).astype(mx.float16)
        record(prefix + ".attention_residual", reference, actual)
        residual_ref, residual_actual = reference, actual
        reference, actual = norm(prefix + ".ffn_norm", reference, actual)
        gate_ref, gate = linear(prefix + ".ffn_gate", reference, actual)
        up_ref, up = linear(prefix + ".ffn_up", reference, actual)
        reference = (0.5 * gate_ref * (1 + np.tanh(math.sqrt(2 / math.pi) *
                     (gate_ref + 0.044715 * gate_ref**3)))) * up_ref
        actual = nn.gelu_approx(gate) * up
        record(prefix + ".gelu_product", reference, actual)
        reference, actual = linear(prefix + ".ffn_down", reference, actual)
        reference, actual = norm(prefix + ".post_ffw_norm", reference, actual)
        reference = residual_ref + reference
        actual = mx.clip(residual_actual.astype(mx.float32) + actual.astype(mx.float32), -65504, 65504).astype(mx.float16)
        record(prefix + ".block_output", reference, actual, **metadata)
        return reference, actual

    reference, actual = evaluate_prefix(blocks, reference, actual, evaluate_block)
    reference, actual = norm("output_norm", reference, actual)
    if head_ids is not None:
        head_reference = np.concatenate([dequantize(embedding_tensor.data[i:i + 1], embedding_tensor.tensor_type)
                                         for i in head_ids], axis=0)
        head_actual = mx.array(read_rows(candidate / index[target], target, head_ids))
        record("tied_head_selected_rows_storage", head_reference, head_actual, token_ids=head_ids)
        same_input = np.asarray(actual).astype(np.float32) @ head_reference.T
        head_logits = actual @ head_actual.T
        mx.eval(head_logits)
        record("tied_head_selected_logits", reference @ head_reference.T, head_logits,
               token_ids=head_ids, same_input=metrics(same_input, np.asarray(head_logits)))
    return {"status": "streamed_block_prefix_diagnostic_complete", "clinical_quality_qualified": False,
            "generation_performed": False, "full_model_instantiated": False, "full_model_resident": False,
            "evaluated_block_count": blocks, "full_block_stack_evaluated": blocks == 62,
            "final_norm_evaluated": True, "selected_head_token_ids": head_ids,
            "block_schedule": schedule, "source_sha256": SOURCE_SHA256,
            "source_hash_verified_in_this_probe": False, "caller_must_verify_source_hash": True,
            "candidate_label": manifest["candidate_label"], "device": device, "token_ids": ids,
            "source_dequant_chunk_bytes": MAX_CHUNK_BYTES, "records": records,
            "limitations": ["NumPy float32 is an independent reference, not native Ollama intermediate capture.",
                            "Only the recorded block prefix and selected embedding/head rows are evaluated.",
                            "Output norm is applied after that prefix; prefixes shorter than 62 omit remaining blocks.",
                            "No full vocabulary logits, token sampling or prompt/KV cache behavior is evaluated.",
                            "No timing, medical quality or full model parity is established."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-gguf", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--token-ids", default="2,105,2430,82138,31037,10797,699,11716")
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--blocks", type=int, choices=range(1, 63), default=1, metavar="1..62")
    parser.add_argument("--head-token-ids", help="Optional comma-separated 1..32 tied-head token rows")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ids = [int(value) for value in args.token_ids.split(",")]
    head_ids = [int(value) for value in args.head_token_ids.split(",")] if args.head_token_ids else None
    if args.output.exists() or not args.output.is_absolute():
        parser.error("Use a new absolute diagnostic output path")
    started = time.monotonic()
    result = run(args.source_gguf, args.model_dir, ids, args.device, blocks=args.blocks, head_ids=head_ids)
    result["elapsed_seconds"] = time.monotonic() - started
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
