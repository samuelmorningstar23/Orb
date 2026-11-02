"""Bounded conversion and architecture-preservation tests; no model or GPU load."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from jinja2 import Environment
from safetensors.numpy import load_file

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "convert_medgemma_gguf_mlx.py"
SPEC = importlib.util.spec_from_file_location("orb_convert_medgemma_gguf_mlx", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
conversion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = conversion
SPEC.loader.exec_module(conversion)


def metadata():
    return {
        "general.architecture": "gemma3", "general.file_type": 15,
        "general.parameter_count": 27_432_406_640, "gemma3.block_count": 62,
        "gemma3.embedding_length": 5376, "gemma3.feed_forward_length": 21504,
        "gemma3.attention.head_count": 32, "gemma3.attention.head_count_kv": 16,
        "gemma3.attention.key_length": 128, "gemma3.attention.value_length": 128,
        "gemma3.attention.sliding_window": 1024, "gemma3.context_length": 131072,
        "tokenizer.ggml.bos_token_id": 2, "tokenizer.ggml.eos_token_id": 1,
        "tokenizer.ggml.add_bos_token": True, "tokenizer.ggml.add_eos_token": False,
        "tokenizer.ggml.eos_token_ids": [1, 106], "tokenizer.ggml.tokens": [""] * 262208,
    }


def test_configuration_preserves_27b_attention_instead_of_mlx_1b_defaults():
    config = conversion.model_config(metadata())
    assert config["query_pre_attn_scalar"] == 168
    assert config["head_dim"] == 128
    assert config["sliding_window"] == 1024
    assert config["sliding_window_pattern"] == 6
    assert config["rope_theta"] == 1_000_000
    assert config["rope_local_base_freq"] == 10_000
    assert config["rope_scaling"] == {"type": "linear", "factor": 8.0}
    assert config["max_position_embeddings"] == 131072
    assert config["eos_token_id"] == [1, 106]
    assert config["tie_word_embeddings"] is True
    assert config["orb_clinical_quality_qualified"] is False
    assert "text only" in config["orb_scope"]


@pytest.mark.parametrize("key,bad", [
    ("general.architecture", "llama"), ("general.file_type", 2), ("gemma3.block_count", 61),
    ("gemma3.attention.sliding_window", 512), ("gemma3.attention.head_count_kv", 8),
    ("tokenizer.ggml.bos_token_id", True), ("tokenizer.ggml.eos_token_ids", [1]),
    ("gemma3.rope.scaling.factor", 1.0), ("gemma3.rope.freq_base", 10_000.0),
    ("gemma3.attention.layer_norm_rms_epsilon", 1e-5),
])
def test_configuration_refuses_shape_precision_tokenizer_or_rope_changes(key, bad):
    values = metadata()
    values[key] = bad
    with pytest.raises(conversion.ConversionRefused):
        conversion.model_config(values)


@pytest.mark.parametrize("name,target", [
    ("output_norm.weight", "model.norm.weight"),
    ("blk.0.attn_norm.weight", "model.layers.0.input_layernorm.weight"),
    ("blk.61.post_attention_norm.weight", "model.layers.61.post_attention_layernorm.weight"),
    ("blk.4.ffn_norm.weight", "model.layers.4.pre_feedforward_layernorm.weight"),
    ("blk.4.post_ffw_norm.weight", "model.layers.4.post_feedforward_layernorm.weight"),
    ("blk.3.attn_q_norm.weight", "model.layers.3.self_attn.q_norm.weight"),
    ("blk.3.attn_k_norm.weight", "model.layers.3.self_attn.k_norm.weight"),
])
def test_all_six_layer_norms_and_final_norm_restore_gemma_offset(name, target):
    assert conversion.tensor_mapping(name) == (target, True)
    original = np.array([1.0, 0.5, 1.000003, -2.0], dtype=np.float32)
    restored = conversion.inverse_norm(original)
    assert restored.dtype == np.float32
    np.testing.assert_allclose(1 + restored, original, rtol=0, atol=np.finfo(np.float32).eps)
    np.testing.assert_array_equal(original, [1.0, 0.5, np.float32(1.000003), -2.0])


@pytest.mark.parametrize("name", ["blk.62.attn_q.weight", "blk.0.attn_q.bias", "output.weight", "mystery.weight"])
def test_unknown_text_tensors_are_rejected_rather_than_silently_omitted(name):
    with pytest.raises(conversion.ConversionRefused):
        conversion.tensor_mapping(name)


def test_vision_is_explicitly_omitted_and_matrix_names_are_mapped():
    assert conversion.tensor_mapping("v.blk.0.attn_q.weight") is None
    assert conversion.tensor_mapping("mm.mm_input_projection.weight") is None
    assert conversion.tensor_mapping("token_embd.weight") == ("model.embed_tokens.weight", False)
    assert conversion.tensor_mapping("blk.2.ffn_down.weight") == ("model.layers.2.mlp.down_proj.weight", False)


def tensor_plan_fixture():
    config = conversion.model_config(metadata())
    shapes = conversion.expected_shapes(config)
    sources = ["token_embd.weight", "output_norm.weight"]
    sources.extend(f"blk.{layer}.{suffix}.weight" for layer in range(62) for suffix in conversion._LAYER_NAMES)
    matrices = 0
    tensors = []
    for name in sources:
        target, inverse = conversion.tensor_mapping(name)
        kind = 0 if inverse else 14 if matrices < 59 else 12
        if not inverse:
            matrices += 1
        tensors.append(SimpleNamespace(name=name, shape=shapes[target][::-1], tensor_type=kind))
    tensors.extend(SimpleNamespace(name=f"v.synthetic_{i}", shape=(1,), tensor_type=1) for i in range(439))
    return config, tensors


def test_full_plan_accounts_for_every_text_parameter_and_explicit_vision_exclusion():
    config, tensors = tensor_plan_fixture()
    plan, excluded = conversion.plan_tensors(tensors, config)
    assert len(plan) == 808
    assert len(excluded) == 439
    assert sum(np.prod(item.shape) for item in plan) == 27_009_346_304
    assert sum(item.bits == 6 for item in plan) == 59
    assert sum(item.bits == 4 for item in plan) == 376
    assert sum(item.inverse_norm for item in plan) == 373


def test_affine8_plan_uses_original_q4_and_q6_sources_with_new_label_and_preserves_float32_norms():
    _, tensors = tensor_plan_fixture()
    config = conversion.model_config(metadata(), quantisation_profile="affine8")
    plan, excluded = conversion.plan_tensors(tensors, config)
    assert config["orb_candidate_label"] == conversion.AFFINE8_CANDIDATE_LABEL
    assert config["orb_candidate_label"] != conversion.CANDIDATE_LABEL
    assert config["orb_quantisation_profile"] == "affine8"
    assert config["orb_clinical_quality_qualified"] is False
    assert sum(item.bits == 8 for item in plan) == 435
    assert sum(item.bits is None and item.inverse_norm for item in plan) == 373
    assert {item.source_type for item in plan if item.bits == 8} == {12, 14}
    assert sum(np.prod(item.shape) for item in plan) == 27_009_346_304
    assert len(excluded) == 439


def test_affine8_storage_estimate_includes_packed_weights_both_affine_arrays_and_float32_norms():
    specs = [conversion.TensorSpec("q", "q", (2, 256), 12, 8, False),
             conversion.TensorSpec("n", "n", (128,), 0, None, True)]
    assert conversion.expected_weight_data_bytes(specs) == 512 + 16 * 4 + 128 * 4


def test_source_repack4_f16_plan_keeps_q4_codes_q6_dense_and_norms_float32():
    _, tensors = tensor_plan_fixture()
    config = conversion.model_config(metadata(), quantisation_profile="source-repack4-f16")
    plan, excluded = conversion.plan_tensors(tensors, config)
    assert config["orb_candidate_label"] == conversion.REPACK4_F16_CANDIDATE_LABEL
    assert config["orb_clinical_quality_qualified"] is False
    assert sum(item.source_type == 12 and item.bits == 4 for item in plan) == 376
    assert sum(item.source_type == 14 and item.bits is None and not item.inverse_norm for item in plan) == 59
    assert sum(item.inverse_norm and item.bits is None for item in plan) == 373
    assert len(excluded) == 439
    dense = conversion.TensorSpec("q6", "q6", (2, 256), 14, None, False)
    norm = conversion.TensorSpec("norm", "norm", (128,), 0, None, True)
    assert conversion.expected_weight_data_bytes([dense, norm]) == 2 * 256 * 2 + 128 * 4


def test_dense_fp16_source_rounding_refuses_nonfinite_or_overflow_and_never_clips():
    source = np.array([[0.00123456, -0.00234567]], dtype=np.float32)
    result = conversion.dense_source_fp16(source)
    assert result.dtype == np.float16
    np.testing.assert_array_equal(result, source.astype(np.float16))
    for bad in (np.array([[float("nan")]], np.float32), np.array([[70000.0]], np.float32), source.astype(np.float16)):
        with pytest.raises(conversion.ConversionRefused):
            conversion.dense_source_fp16(bad)


def test_small_repack_profile_conversion_writes_correct_dtypes_config_hashes_and_original_source_errors(tmp_path, monkeypatch):
    gguf = pytest.importorskip("gguf")
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.setitem(sys.modules, "mlx.core", None)  # New profile must run entirely on CPU.
    payload = b"synthetic-source-only"
    source = tmp_path / "source.gguf"
    source.write_bytes(payload)
    monkeypatch.setattr(conversion, "SOURCE_BYTES", len(payload))
    monkeypatch.setattr(conversion, "SOURCE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(conversion, "SOURCE_TEXT_TENSORS", 3)
    monkeypatch.setattr(conversion, "SOURCE_TEXT_PARAMETERS", 1026)
    monkeypatch.setattr(conversion, "version", lambda name: "test")
    monkeypatch.setattr(conversion, "model_config", lambda metadata, **kwargs: {"model_type": "synthetic"})

    def tokenizer_stub(source, output, metadata):
        (output / "tokenizer.json").write_text("{}")
        return {"synthetic_test_only": True}

    monkeypatch.setattr(conversion, "write_tokenizer", tokenizer_stub)
    q4 = np.zeros((2, 144), dtype=np.uint8)
    q4[:, :2] = np.array([0.001, 0.002], "<f2").view(np.uint8).reshape(2, 2)
    q4[:, 2:4] = np.array([0.0002, 0.0003], "<f2").view(np.uint8).reshape(2, 2)
    q4[:, 4:16] = np.arange(12, dtype=np.uint8) + 16
    q4[:, 16:] = np.arange(128, dtype=np.uint8)
    q6 = np.zeros((2, 210), dtype=np.uint8)
    q6[:, :128] = np.arange(128, dtype=np.uint8)
    q6[:, 128:192] = np.arange(64, dtype=np.uint8)
    q6[:, 192:208] = np.arange(1, 17, dtype=np.uint8)
    q6[:, 208:] = np.array([0.001, 0.002], "<f2").view(np.uint8).reshape(2, 2)
    norm = np.array([1.0, 0.5], np.float32)
    tensors = [SimpleNamespace(name="q4", data=q4, tensor_type=gguf.GGMLQuantizationType.Q4_K),
               SimpleNamespace(name="q6", data=q6, tensor_type=gguf.GGMLQuantizationType.Q6_K),
               SimpleNamespace(name="norm", data=norm, tensor_type=gguf.GGMLQuantizationType.F32)]
    specs = [conversion.TensorSpec("q4", "layer.q4.weight", (2, 256), 12, 4, False),
             conversion.TensorSpec("q6", "layer.q6.weight", (2, 256), 14, None, False),
             conversion.TensorSpec("norm", "norm.weight", (2,), 0, None, True)]
    out = tmp_path / "candidate"
    manifest = conversion.convert(source, out, SimpleNamespace(tensors=tensors), {}, specs, [],
                                  quantisation_profile="source-repack4-f16", max_dequant_bytes=1024 * 1024)
    assert manifest["status"] == "converted_candidate_unqualified"
    assert manifest["candidate_label"] == conversion.REPACK4_F16_CANDIDATE_LABEL
    assert manifest["lossless_conversion"] is False
    assert manifest["fresh_quantization_performed"] is False
    assert manifest["expected_weight_data_bytes_excluding_headers"] == 1352
    assert sum(row["tensor_data_bytes"] for row in manifest["files"].values() if "tensor_data_bytes" in row) == 1352
    q4_file = load_file(out / "model-00001-of-00003.safetensors")
    q6_file = load_file(out / "model-00002-of-00003.safetensors")
    norm_file = load_file(out / "model-00003-of-00003.safetensors")
    assert q4_file["layer.q4.weight"].dtype == np.uint32
    assert q4_file["layer.q4.scales"].dtype == np.float16
    assert q4_file["layer.q4.biases"].dtype == np.float16
    assert set(q6_file) == {"layer.q6.weight"}
    np.testing.assert_array_equal(q6_file["layer.q6.weight"], gguf.dequantize(q6, gguf.GGMLQuantizationType.Q6_K).astype(np.float16))
    np.testing.assert_array_equal(norm_file["norm.weight"], np.array([0.0, -0.5], np.float32))
    config = json.loads((out / "config.json").read_text())
    assert "layer.q4" in config["quantization"]
    assert "layer.q6" not in config["quantization"]
    assert "norm" not in config["quantization"]
    assert manifest["matrices"][0]["source_integer_codes_preserved"] is True
    assert manifest["matrices"][0]["first_row_relative_l2"] < 0.001
    assert manifest["matrices"][1]["native_storage"] == "float16_dense"
    assert manifest["matrices"][1]["first_row_relative_l2"] < 0.001
    for name, row in manifest["files"].items():
        assert row["sha256"] == hashlib.sha256((out / name).read_bytes()).hexdigest()
    assert source.read_bytes() == payload
    assert not (out / ".conversion-incomplete").exists()


@pytest.mark.parametrize("profile", ["float16", "affine4", "", None])
def test_unknown_quantisation_profiles_fail_closed(profile):
    with pytest.raises(conversion.ConversionRefused):
        conversion.model_config(metadata(), quantisation_profile=profile)


def test_convert_rejects_profile_plan_mismatch_before_any_gpu_import_or_source_access(tmp_path):
    mixed_spec = conversion.TensorSpec("q", "q", (1, 256), 12, 4, False)
    with pytest.raises(conversion.ConversionRefused, match="planned tensor precisions"):
        conversion.convert(tmp_path / "unopened-source", tmp_path / "uncreated-output", None, {}, [mixed_spec], [],
                           quantisation_profile="affine8")
    assert not (tmp_path / "uncreated-output").exists()


@pytest.mark.parametrize("change", ["missing", "duplicate", "shape", "precision", "vision"])
def test_full_plan_rejects_missing_duplicate_shape_precision_or_exclusion_changes(change):
    config, tensors = tensor_plan_fixture()
    if change == "missing":
        tensors.pop(0)
    elif change == "duplicate":
        tensors.append(tensors[0])
    elif change == "shape":
        tensors[0].shape = (5376, 262144)
    elif change == "precision":
        tensors[0].tensor_type = 12
    else:
        tensors.pop()
    with pytest.raises(conversion.ConversionRefused):
        conversion.plan_tensors(tensors, config)


class BoundedPackedRows:
    """A fake memmap that forbids conversion of the complete array."""
    shape = (11, 144)

    def __init__(self):
        self.reads = []

    def __array__(self, *args, **kwargs):
        raise AssertionError("The full packed tensor was materialized")

    def __getitem__(self, selection):
        assert isinstance(selection, slice)
        assert selection.stop - selection.start <= 3
        self.reads.append((selection.start, selection.stop))
        return np.arange(selection.start, selection.stop, dtype=np.float32)


def test_bounded_reader_slices_before_dequantization_and_preserves_every_row():
    rows = BoundedPackedRows()
    tensor = SimpleNamespace(data=rows, tensor_type=12)
    spec = conversion.TensorSpec("source", "target", (11, 256), 12, 4, False)

    def dequantize(values, kind):
        assert kind == 12
        return np.repeat(values[:, None], 256, axis=1)

    chunks = list(conversion.dequantized_chunks(tensor, spec, 3 * 256 * 4, dequantize))
    assert rows.reads == [(0, 3), (3, 6), (6, 9), (9, 11)]
    assert [row for row, _ in chunks] == [0, 3, 6, 9]
    restored = np.concatenate([values for _, values in chunks])
    np.testing.assert_array_equal(restored[:, 0], np.arange(11))
    assert max(values.nbytes for _, values in chunks) <= 3072


@pytest.mark.parametrize("bad", ["dtype", "shape", "nan"])
def test_bounded_reader_refuses_incomplete_or_nonfinite_dequantizer_outputs(bad):
    tensor = SimpleNamespace(data=BoundedPackedRows(), tensor_type=12)
    spec = conversion.TensorSpec("source", "target", (11, 256), 12, 4, False)

    def dequantize(values, kind):
        if bad == "dtype":
            return np.zeros((len(values), 256), dtype=np.float16)
        if bad == "shape":
            return np.zeros((len(values), 255), dtype=np.float32)
        return np.full((len(values), 256), np.nan, dtype=np.float32)

    with pytest.raises(conversion.ConversionRefused):
        list(conversion.dequantized_chunks(tensor, spec, 3072, dequantize))


def test_reader_refuses_even_one_oversized_row():
    with pytest.raises(conversion.ConversionRefused):
        list(conversion.chunk_ranges(10, 256, 1023))


def test_streaming_safetensors_is_standard_readable_and_preserves_exact_values(tmp_path):
    path = tmp_path / "model.safetensors"
    writer = conversion.StreamingSafetensors(path, {
        "q.weight": ("uint32", (4, 8)), "q.scales": ("float16", (4, 2)), "norm.weight": ("float32", (3,)),
    })
    weights = np.arange(32, dtype=np.uint32).reshape(4, 8)
    scales = np.arange(8, dtype=np.float16).reshape(4, 2)
    norm = np.array([0, -0.1, 0.5], dtype=np.float32)
    writer.write("q.weight", 0, weights[:2])
    writer.write("q.scales", 0, scales[:1])
    writer.write("q.weight", 2, weights[2:])
    writer.write("q.scales", 1, scales[1:])
    writer.write("norm.weight", 0, norm)
    writer.finish()
    loaded = load_file(path)
    np.testing.assert_array_equal(loaded["q.weight"], weights)
    np.testing.assert_array_equal(loaded["q.scales"], scales)
    np.testing.assert_array_equal(loaded["norm.weight"], norm)


@pytest.mark.parametrize("bad", ["missing", "overlap", "overflow", "dtype", "shape"])
def test_streaming_writer_refuses_gaps_overlap_overflow_and_wrong_precision(tmp_path, bad):
    writer = conversion.StreamingSafetensors(tmp_path / "model.safetensors", {"w": ("float16", (4, 2))})
    values = np.zeros((2, 2), dtype=np.float16)
    writer.write("w", 0, values)
    try:
        with pytest.raises(conversion.ConversionRefused):
            if bad == "missing":
                writer.finish()
            elif bad == "overlap":
                writer.write("w", 0, values)
            elif bad == "overflow":
                writer.write("w", 2, np.zeros((3, 2), dtype=np.float16))
            elif bad == "dtype":
                writer.write("w", 2, values.astype(np.float32))
            else:
                writer.write("w", 2, np.zeros((2, 3), dtype=np.float16))
    finally:
        writer.close()


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": "Hello"}],
    [{"role": "system", "content": "Keep facts."}, {"role": "user", "content": "  Na⁺ 141\nΔ  "}],
    [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"},
     {"role": "assistant", "content": "A\n"}, {"role": "user", "content": "Again"}],
    [{"role": "system", "content": " Clinical rules.\n"}, {"role": "system", "content": "\n[CONTEXT] Na⁺ 141\nΔ  "},
     {"role": "user", "content": " Summarise. "}],
])
def test_jinja_matches_pinned_go_literal_whitespace_and_preserves_content(messages):
    def refuse(message):
        raise ValueError(message)

    template = Environment().from_string(conversion.CHAT_TEMPLATE)
    actual = template.render(messages=messages, bos_token="<bos>", raise_exception=refuse)
    assert actual == conversion.render_ollama_text(messages)
    assert actual.count("<bos>") == 1
    assert actual.endswith("<start_of_turn>model\n")


def test_template_does_not_adopt_gguf_trim_or_double_newline_system_separator():
    actual = conversion.render_ollama_text([{"role": "system", "content": "S"}, {"role": "user", "content": " U "}])
    assert actual == "<bos><start_of_turn>userS\n U <end_of_turn>\n<start_of_turn>model\n"


def test_two_leading_systems_use_observed_double_newline_without_trimming_bodies():
    messages = [{"role": "system", "content": " Rules.\n"},
                {"role": "system", "content": "\nChart.  "},
                {"role": "user", "content": " Q "}]
    actual = conversion.render_ollama_text(messages)
    assert actual == "<bos><start_of_turn>user Rules.\n\n\n\nChart.  \n Q <end_of_turn>\n<start_of_turn>model\n"


def test_token_ids_are_explicitly_a_list_with_transformers_five_default_dict_api():
    from tokenizers import Tokenizer, models
    from transformers import PreTrainedTokenizerFast

    backend = Tokenizer(models.WordLevel({"<unk>": 0, "<bos>": 1, "Hello": 2}, unk_token="<unk>"))
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="<unk>", bos_token="<bos>",
                                       chat_template="{{ bos_token }}{{ messages[0]['content'] }}")
    messages = [{"role": "user", "content": "Hello"}]
    expected = tokenizer.encode("<bos>Hello", add_special_tokens=False)
    assert conversion.template_token_ids(tokenizer, messages) == expected
    assert type(conversion.template_token_ids(tokenizer, messages)) is list


def test_pinned_sentencepiece_prefix_is_added_after_special_tokens_without_changing_vocabulary():
    from tokenizers import AddedToken, Tokenizer, models, normalizers

    vocabulary = {"<unk>": 0, "<start_of_turn>": 1, "▁user": 2, "user": 3}
    backend = Tokenizer(models.WordLevel(vocabulary, unk_token="<unk>"))
    backend.add_special_tokens([AddedToken("<start_of_turn>", special=True)])
    backend.normalizer = normalizers.Replace(" ", "▁")
    assert backend.encode("<start_of_turn>user", add_special_tokens=False).ids == [1, 3]
    conversion.set_pinned_sentencepiece_prefix(backend)
    assert backend.encode("<start_of_turn>user", add_special_tokens=False).ids == [1, 2]
    assert backend.encode("user", add_special_tokens=False).ids == [2]
    assert backend.get_vocab() == vocabulary


@pytest.mark.parametrize("messages", [[], [{"role": "user", "content": [{"type": "image"}]}],
                                      [{"role": "user", "content": "Q"}, {"role": "system", "content": "S"}],
                                      [{"role": "system", "content": "No conversation"}]])
def test_text_renderer_refuses_unqualified_multimodal_or_system_shapes(messages):
    with pytest.raises(conversion.ConversionRefused):
        conversion.render_ollama_text(messages)


def test_hashing_reads_sequentially_and_matches_sha256(tmp_path):
    import hashlib

    path = tmp_path / "blob"
    payload = b"bounded-source-hash" * 1024
    path.write_bytes(payload)
    assert conversion.sha256_file(path, block_bytes=31) == hashlib.sha256(payload).hexdigest()
