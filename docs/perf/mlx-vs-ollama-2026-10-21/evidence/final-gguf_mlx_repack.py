"""Pure NumPy Q4_K integer-preserving repack for bounded CPU verification.

This module does not import MLX, load a model, perform fresh quantization, read
files, or change the active converter. It translates Q4_K's eight affine groups
of 32 values into native MLX 4-bit/group32 packing. The original unsigned integer
codes are unchanged; scale=d*subscale and bias=-dmin*min are retained in float32
or explicitly rounded to float16. Float16 parameter storage is not lossless.

The float32 decoder below measures representation error, not Metal arithmetic
or clinical equivalence. Q6_K's per 16 scale format is intentionally unsupported.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

Q4_K_BLOCK_BYTES = 144
Q4_K_BLOCK_VALUES = 256
GROUP_SIZE = 32
BITS = 4
MAX_DEQUANT_BYTES = 64 * 1024 * 1024


class RepackRefused(ValueError):
    """The source, bounded budget, or parameter representation is unsupported."""


@dataclass(frozen=True)
class RepackedQ4K:
    """Block-major native arrays; caller may reshape contiguous matrix rows."""

    weight: np.ndarray  # uint32 [blocks, 32], eight integer codes per word
    scales: np.ndarray  # float16/float32 [blocks, 8]
    biases: np.ndarray  # float16/float32 [blocks, 8]
    integer_codes_preserved: bool = True
    fresh_quantization: bool = False


def _bounded_blocks(blocks: np.ndarray, max_dequant_bytes: int) -> np.ndarray:
    source = np.asarray(blocks)
    if (source.dtype != np.dtype("uint8") or source.ndim != 2
            or source.shape[0] < 1 or source.shape[1] != Q4_K_BLOCK_BYTES):
        raise RepackRefused("Use nonempty uint8 Q4_K blocks shaped [n,144].")
    if (type(max_dequant_bytes) is not int or not 1024 <= max_dequant_bytes <= MAX_DEQUANT_BYTES
            or source.shape[0] * Q4_K_BLOCK_VALUES * 4 > max_dequant_bytes):
        raise RepackRefused("The float32 decoded slice must fit the explicit budget of at most 64 MiB.")
    if source.strides[-1] != 1:
        raise RepackRefused("The bytes in each Q4_K block must be contiguous.")
    return source


def repack_q4_k(blocks: np.ndarray, *, parameter_dtype: str = "float16",
                max_dequant_bytes: int = MAX_DEQUANT_BYTES) -> RepackedQ4K:
    """Reorder source codes; never fit scales or round integer weight codes.

    For float16 storage only scales and biases round. Float32 preserves the
    exact Q4_K float32 dequantization formula before runtime arithmetic changes.
    """
    source = _bounded_blocks(blocks, max_dequant_bytes)
    if parameter_dtype not in {"float16", "float32"}:
        raise RepackRefused("Parameter storage must be explicitly float16 or float32.")
    n = source.shape[0]
    d = source[:, :2].view("<f2").astype(np.float32)
    dmin = source[:, 2:4].view("<f2").astype(np.float32)
    if not np.isfinite(d).all() or not np.isfinite(dmin).all():
        raise RepackRefused("Q4_K block multipliers must be finite.")

    # The first four subscales/minima carry six low bits, with their upper two
    # unused bits carrying bits 4/5 for the last four groups. The last four bytes carry
    # the remaining low four bits of those groups' scales and minima.
    source_scales = source[:, 4:16].reshape(n, 3, 4)
    sd, sm, both = np.split(source_scales, 3, axis=1)
    subscale = np.concatenate([sd & 0x3F, (both & 0x0F) | ((sd >> 2) & 0x30)], axis=-1).reshape(n, 8)
    minimum = np.concatenate([sm & 0x3F, (both >> 4) | ((sm >> 2) & 0x30)], axis=-1).reshape(n, 8)
    scales32 = d * subscale.astype(np.float32)
    biases32 = -(dmin * minimum.astype(np.float32))

    # GGUF stores 32 low nibbles followed by 32 high nibbles for each 64 values.
    # MLX stores consecutive values in increasing nibble positions of uint32.
    qs = source[:, 16:].reshape(n, 4, 32)
    codes = np.stack([qs & 0x0F, qs >> 4], axis=2).reshape(n, 32, 8).astype(np.uint32)
    shifts: np.ndarray = np.arange(0, 32, 4, dtype=np.uint32)
    packed = np.bitwise_or.reduce(codes << shifts, axis=-1)
    with np.errstate(over="ignore", invalid="ignore"):
        scales = scales32.astype(parameter_dtype)
        biases = biases32.astype(parameter_dtype)
    if not np.isfinite(scales).all() or not np.isfinite(biases).all():
        raise RepackRefused("The selected parameter dtype cannot represent the source multipliers finitely.")
    return RepackedQ4K(packed, scales, biases)


def decode_affine4_float32(result: RepackedQ4K, *, max_dequant_bytes: int = MAX_DEQUANT_BYTES) -> np.ndarray:
    """Independent CPU reference of native uint32 nibble ordering and affine math."""
    packed, scales, biases = result.weight, result.scales, result.biases
    if (packed.dtype != np.dtype("uint32") or packed.ndim != 2 or packed.shape[1] != 32
            or packed.shape[0] < 1 or scales.shape != (packed.shape[0], 8) or biases.shape != scales.shape
            or scales.dtype not in {np.dtype("float16"), np.dtype("float32")} or biases.dtype != scales.dtype
            or not np.isfinite(scales).all() or not np.isfinite(biases).all()):
        raise RepackRefused("Native block arrays have an invalid shape, dtype or value.")
    if (type(max_dequant_bytes) is not int or not 1024 <= max_dequant_bytes <= MAX_DEQUANT_BYTES
            or packed.shape[0] * Q4_K_BLOCK_VALUES * 4 > max_dequant_bytes):
        raise RepackRefused("The decoded reference exceeds the bounded slice budget.")
    shifts: np.ndarray = np.arange(0, 32, 4, dtype=np.uint32)
    codes = ((packed[..., None] >> shifts) & 0x0F).reshape(packed.shape[0], 8, 32).astype(np.float32)
    return (codes * scales.astype(np.float32)[..., None] + biases.astype(np.float32)[..., None]).reshape(-1, 256)
