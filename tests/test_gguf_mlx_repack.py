"""CPU-only integer-preserving Q4_K repack tests; no MLX or model load."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gguf_mlx_repack.py"
SPEC = importlib.util.spec_from_file_location("orb_gguf_mlx_repack", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
repack = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repack
SPEC.loader.exec_module(repack)


def synthetic_blocks(n=16):
    rng = np.random.default_rng(105)
    blocks = np.zeros((n, 144), dtype=np.uint8)
    blocks[:, :2] = np.linspace(0.0002, 0.003, n).astype("<f2").view(np.uint8).reshape(n, 2)
    blocks[:, 2:4] = np.linspace(0.0003, 0.004, n).astype("<f2").view(np.uint8).reshape(n, 2)
    scales, minima = rng.integers(0, 64, (2, n, 8), dtype=np.uint8)
    # Construct the documented GGUF source layout independently of the helper.
    blocks[:, 4:8] = scales[:, :4] | ((scales[:, 4:] >> 4) << 6)
    blocks[:, 8:12] = minima[:, :4] | ((minima[:, 4:] >> 4) << 6)
    blocks[:, 12:16] = (scales[:, 4:] & 15) | ((minima[:, 4:] & 15) << 4)
    codes = rng.integers(0, 16, (n, 4, 2, 32), dtype=np.uint8)
    codes[0, 0, 0, :8] = np.arange(8)
    blocks[:, 16:] = (codes[:, :, 0] | (codes[:, :, 1] << 4)).reshape(n, 128)
    return blocks, codes.reshape(n, 256)


def test_native_packing_preserves_every_integer_code_and_source_without_fitting():
    blocks, source_codes = synthetic_blocks()
    original = blocks.copy()
    result = repack.repack_q4_k(blocks, parameter_dtype="float16")
    decoded_codes = ((result.weight[..., None] >> np.arange(0, 32, 4, dtype=np.uint32)) & 15).reshape(-1, 256)
    np.testing.assert_array_equal(decoded_codes, source_codes)
    np.testing.assert_array_equal(blocks, original)
    assert result.weight[0, 0] == 0x76543210
    assert result.integer_codes_preserved is True
    assert result.fresh_quantization is False


@pytest.mark.parametrize("dtype", ["float16", "float32"])
def test_repacked_values_match_installed_gguf_formula_exactly_or_with_only_parameter_rounding(dtype):
    gguf = pytest.importorskip("gguf")
    blocks, _ = synthetic_blocks()
    expected = gguf.dequantize(blocks, gguf.GGMLQuantizationType.Q4_K)
    result = repack.repack_q4_k(blocks, parameter_dtype=dtype)
    actual = repack.decode_affine4_float32(result)
    assert actual.shape == (16, 256)
    assert actual.dtype == np.float32
    assert result.scales.dtype == np.dtype(dtype)
    if dtype == "float32":
        np.testing.assert_array_equal(actual, expected)
    else:
        relative_l2 = np.linalg.norm((actual - expected).astype(np.float64)) / np.linalg.norm(expected.astype(np.float64))
        assert relative_l2 < 0.001


def test_parameter_precision_changes_no_integer_codes():
    blocks, _ = synthetic_blocks()
    half = repack.repack_q4_k(blocks, parameter_dtype="float16")
    full = repack.repack_q4_k(blocks, parameter_dtype="float32")
    np.testing.assert_array_equal(half.weight, full.weight)
    np.testing.assert_array_equal(half.scales, full.scales.astype(np.float16))
    np.testing.assert_array_equal(half.biases, full.biases.astype(np.float16))


@pytest.mark.parametrize("source", [np.zeros((0, 144), np.uint8), np.zeros((1, 210), np.uint8),
                                    np.zeros((1, 144), np.float32), np.zeros(144, np.uint8)])
def test_non_q4_k_shapes_and_dtypes_are_refused(source):
    with pytest.raises(repack.RepackRefused):
        repack.repack_q4_k(source)


def test_both_repack_and_reference_decode_enforce_small_explicit_slice_budget():
    source, _ = synthetic_blocks(2)
    with pytest.raises(repack.RepackRefused, match="budget"):
        repack.repack_q4_k(source, max_dequant_bytes=1024)
    packed = repack.repack_q4_k(source)
    with pytest.raises(repack.RepackRefused, match="budget"):
        repack.decode_affine4_float32(packed, max_dequant_bytes=1024)


def test_finite_source_that_overflows_fp16_parameters_is_refused_without_clipping():
    source, _ = synthetic_blocks(1)
    source[:, :2] = np.array([65504.0], dtype="<f2").view(np.uint8).reshape(1, 2)
    source[:, 4:8] = 63
    with pytest.raises(repack.RepackRefused, match="finitely"):
        repack.repack_q4_k(source, parameter_dtype="float16")
    assert np.isfinite(repack.repack_q4_k(source, parameter_dtype="float32").scales).all()


def test_unsupported_parameter_dtype_is_refused():
    source, _ = synthetic_blocks(1)
    with pytest.raises(repack.RepackRefused, match="float16 or float32"):
        repack.repack_q4_k(source, parameter_dtype="bfloat16")
