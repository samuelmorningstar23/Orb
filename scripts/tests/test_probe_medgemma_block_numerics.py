"""Tiny CPU-only tests; MLX and model payloads are never imported."""
import importlib.util
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SPEC = importlib.util.spec_from_file_location(
    "probe_medgemma_block_numerics", Path(__file__).parents[1] / "probe_medgemma_block_numerics.py")
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def write_tensor(path, name, values):
    dtype = {np.dtype("float16"): "F16", np.dtype("float32"): "F32"}[values.dtype]
    header = json.dumps({name: {"dtype": dtype, "shape": list(values.shape),
                                "data_offsets": [0, values.nbytes]}}).encode()
    path.write_bytes(struct.pack("<Q", len(header)) + header + values.tobytes())


def test_selected_safetensors_rows_keep_order_and_duplicates(tmp_path):
    values = np.arange(24, dtype=np.float16).reshape(6, 4)
    path = tmp_path / "tiny.safetensors"
    write_tensor(path, "weight", values)
    np.testing.assert_array_equal(probe.read_rows(path, "weight", [5, 0, 5]), values[[5, 0, 5]])


def test_safetensors_refuses_whole_matrix(tmp_path):
    path = tmp_path / "tiny.safetensors"
    write_tensor(path, "weight", np.ones((2, 4), dtype=np.float32))
    with pytest.raises(ValueError, match="Only small vectors"):
        probe.read_rows(path, "weight")


def test_safetensors_vector(tmp_path):
    path = tmp_path / "tiny.safetensors"
    values = np.arange(4, dtype=np.float32)
    write_tensor(path, "weight", values)
    np.testing.assert_array_equal(probe.read_rows(path, "weight"), values)


def test_source_linear_slices_before_dequantization(monkeypatch):
    monkeypatch.setattr(probe, "MAX_CHUNK_BYTES", 3 * 4 * 2)
    weight = np.arange(30, dtype=np.float32).reshape(10, 3)
    tensor = SimpleNamespace(shape=[3, 10], tensor_type=12, data=weight)
    seen = []

    def decode(chunk, kind):
        assert kind == 12
        seen.append(chunk.shape[0])
        assert chunk.nbytes <= probe.MAX_CHUNK_BYTES
        return chunk

    inputs = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32)
    np.testing.assert_array_equal(probe.source_linear(tensor, inputs, decode), inputs @ weight.T)
    assert seen == [2, 2, 2, 2, 2]


def test_metrics_exact_and_nonfinite():
    x = np.array([1, 2, 3], dtype=np.float32)
    assert probe.metrics(x, x)["relative_l2"] == 0
    assert probe.metrics(x, x)["cosine_similarity"] == pytest.approx(1)
    result = probe.metrics(x, np.array([1, np.inf, np.nan]))
    assert result["actual_nonfinite"] == 2
    assert "relative_l2" not in result


def test_rms_reference_preserves_source_multiplier():
    x = np.array([[3, 4]], dtype=np.float32)
    weight = np.array([2, 3], dtype=np.float32)
    np.testing.assert_allclose(probe.rms_reference(x, weight), x / np.sqrt(12.5 + 1e-6) * weight)


def test_split_half_rope_position_zero_and_norm():
    x = np.arange(2 * 3 * 128, dtype=np.float32).reshape(2, 3, 128) / 100
    result = probe.rope_reference(x)
    np.testing.assert_array_equal(result[0], x[0])
    np.testing.assert_allclose(np.linalg.norm(result, axis=-1), np.linalg.norm(x, axis=-1), rtol=1e-6)


def test_global_rope_uses_scaled_positions():
    x = np.ones((9, 1, 128), dtype=np.float32)
    global_rotated = probe.rope_reference(x, base=1_000_000, scale=0.125)
    unscaled = probe.rope_reference(x, base=1_000_000, scale=1)
    np.testing.assert_allclose(global_rotated[8], unscaled[1])


def test_two_block_tiny_fake_preserves_separate_states_and_provenance():
    records = []

    def fake_block(metadata, reference, actual):
        records.append({**metadata, "incoming_reference": reference, "incoming_actual": actual})
        return reference + metadata["layer"] + 1, actual + 10 * (metadata["layer"] + 1)

    reference, actual = probe.evaluate_prefix(2, 7, 70, fake_block)
    assert (reference, actual) == (10, 100)
    assert [row["source_prefix"] for row in records] == ["blk.0", "blk.1"]
    assert [row["incoming_reference"] for row in records] == [7, 8]
    assert [row["incoming_actual"] for row in records] == [70, 80]
    assert all(row["rope_base"] == 10000 and row["rope_scale"] == 1 for row in records)


def test_sixth_global_block_route_is_explicit_and_repeats():
    schedule = probe.block_schedule(62)
    assert [row["layer"] for row in schedule if row["is_global"]] == list(range(5, 62, 6))
    assert schedule[5] == {"layer": 5, "source_prefix": "blk.5", "is_global": True,
                           "rope_base": 1000000, "rope_scale": 0.125}
    assert schedule[6]["rope_base"] == 10000 and schedule[6]["rope_scale"] == 1


@pytest.mark.parametrize("blocks", [0, 63, -1, True, 1.5])
def test_block_schedule_refuses_invalid_count(blocks):
    with pytest.raises(ValueError, match="1 and62|1 and 62"):
        probe.block_schedule(blocks)


def test_grouped_causal_attention_single_token_copies_correct_kv_head():
    q = np.zeros((1, 32, 128), dtype=np.float32)
    k = np.zeros((1, 16, 128), dtype=np.float32)
    v = np.arange(16, dtype=np.float32)[None, :, None] * np.ones((1, 16, 128), dtype=np.float32)
    result = probe.attention_reference(q, k, v).reshape(1, 32, 128)
    np.testing.assert_array_equal(result, np.repeat(v, 2, axis=1))


def test_grouped_causal_attention_two_tokens_does_not_read_future():
    q = np.zeros((2, 32, 128), dtype=np.float32)
    k = np.zeros((2, 16, 128), dtype=np.float32)
    v = np.stack((np.ones((16, 128), dtype=np.float32), np.full((16, 128), 3, dtype=np.float32)))
    result = probe.attention_reference(q, k, v)
    np.testing.assert_array_equal(result[0], np.ones(4096, dtype=np.float32))
    np.testing.assert_array_equal(result[1], np.full(4096, 2, dtype=np.float32))
