"""Small CPU fake/native-pointer and safetensors-row tests; no model/GPU."""
from __future__ import annotations

import ctypes
import importlib.util
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SPEC = importlib.util.spec_from_file_location("orb_probe_gguf_native_rows", Path(__file__).resolve().parents[1] / "scripts/probe_gguf_native_rows.py")
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def write_sf(path, arrays):
    header, payload = {}, b""
    for name, (dtype, array) in arrays.items():
        raw = array.tobytes()
        header[name] = {"dtype": dtype, "shape": list(array.shape), "data_offsets": [len(payload), len(payload) + len(raw)]}
        payload += raw
    data = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(data)) + data + payload)


def test_native_c_signature_and_bounded_pointer_buffers_are_explicit():
    calls = []
    class FakeFunction:
        def __call__(self, source, output, columns):
            assert isinstance(source, ctypes.c_void_p)
            first = ctypes.cast(source, ctypes.POINTER(ctypes.c_uint8))[0]
            calls.append(columns)
            for index in range(columns):
                output[index] = first + index
    q4, q6 = FakeFunction(), FakeFunction()
    decoder = probe.NativeDecoder(Path("unused"), loader=lambda path: SimpleNamespace(dequantize_row_q4_K=q4, dequantize_row_q6_K=q6))
    assert q4.argtypes == [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int64]
    assert q4.restype is None
    values = decoder.decode(bytes([5]) + bytes(143), 12, 256)
    np.testing.assert_array_equal(values, np.arange(256, dtype=np.float32) + 5)
    for kind, columns, raw in [(0, 256, bytes(144)), (12, 255, bytes(144)), (14, 256, bytes(144)), (12, 256 * 100, bytes(144 * 100))]:
        with pytest.raises(probe.ProbeRefused):
            decoder.decode(raw, kind, columns)
    assert calls == [256]


def test_dense_candidate_row_read_is_bounded_and_uses_correct_middle_offset(tmp_path):
    matrix = (np.arange(3 * 256, dtype=np.float32) / 32).astype(np.float16).reshape(3, 256)
    path = tmp_path / "dense.safetensors"
    write_sf(path, {"x.weight": ("F16", matrix)})
    values, digest, nbytes = probe.candidate_row(path, "x.weight", matrix.shape, 14, 1)
    np.testing.assert_array_equal(values, matrix[1].astype(np.float32))
    assert nbytes == 512 and len(digest) == 64


def test_q4_candidate_row_preserves_nibble_order_and_affine_groups(tmp_path):
    codes = (np.arange(256, dtype=np.uint32) % 16).reshape(32, 8)
    packed = np.bitwise_or.reduce(codes << np.arange(0, 32, 4, dtype=np.uint32), axis=-1)
    weights = np.stack([packed, packed[::-1]])
    scales = np.ones((2, 8), dtype=np.float16) * 2
    biases = np.ones((2, 8), dtype=np.float16) * -3
    path = tmp_path / "q4.safetensors"
    write_sf(path, {"x.weight": ("U32", weights), "x.scales": ("F16", scales), "x.biases": ("F16", biases)})
    values, digest, nbytes = probe.candidate_row(path, "x.weight", (2, 256), 12, 0)
    np.testing.assert_array_equal(values, np.arange(256, dtype=np.float32) % 16 * 2 - 3)
    assert len(digest) == 64 and nbytes == 160


def test_candidate_shape_dtype_bounds_and_extra_keys_are_refused(tmp_path):
    path = tmp_path / "dense.safetensors"
    write_sf(path, {"x.weight": ("F16", np.ones((2, 256), dtype=np.float16))})
    for shape, row in [((1, 256), 0), ((2, 256), 2), ((2, 255), 0)]:
        with pytest.raises(probe.ProbeRefused):
            probe.candidate_row(path, "x.weight", shape, 14, row)
    write_sf(path, {"x.weight": ("U32", np.ones((2, 256), dtype=np.uint32))})
    with pytest.raises(probe.ProbeRefused):
        probe.candidate_row(path, "x.weight", (2, 256), 14, 0)
    write_sf(path, {"x.weight": ("F16", np.ones((2, 256), dtype=np.float16)), "extra": ("F16", np.ones(1, dtype=np.float16))})
    with pytest.raises(probe.ProbeRefused):
        probe.candidate_row(path, "x.weight", (2, 256), 14, 0)


def test_deterministic_representative_rows_and_no_false_full_audit_claim():
    assert probe.selected_rows(1) == [0]
    assert probe.selected_rows(2) == [0, 1]
    assert probe.selected_rows(100) == [0, 50, 99]
    pairs = probe.selected_matrices()
    assert len(pairs) == 22 and len(set(pairs)) == 22
    assert ("blk.5.attn_q.weight", "model.layers.5.self_attn.q_proj.weight") in pairs


def test_error_metrics_are_exact_and_finite_and_reject_mismatches():
    assert probe.error_metrics(np.array([1., 2.]), np.array([1., 2.]))["relative_l2"] == 0
    assert probe.error_metrics(np.zeros(2), np.zeros(2))["relative_l2"] == 0
    assert probe.error_metrics(np.zeros(2), np.ones(2))["relative_l2"] is None
    with pytest.raises(probe.ProbeRefused):
        probe.error_metrics(np.zeros(2), np.array([np.nan, 0]))
