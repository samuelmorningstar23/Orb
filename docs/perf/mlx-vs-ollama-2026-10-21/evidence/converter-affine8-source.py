#!/usr/bin/env python3
"""Build an OFFLINE, unqualified MLX text candidate from Orb's pinned GGUF.

This is a bounded, per-row conversion, not a lossless format translation. The
original Q4_K and Q6_K matrices are dequantized and re-quantized with MLX native
4/6-bit affine quantization by default, or explicitly selected affine8. Both
use group size 32 and read the original GGUF, never a previous MLX candidate.
All clinical-quality gates remain
pending. Vision/projector tensors are explicitly omitted: this candidate is for
Sage TEXT ONLY, and must never replace the imaging model.

Default execution only inspects metadata. --execute and the candidate
acknowledgement are required to write anything. No network client, model loader,
generation, source mutation or production configuration change is performed.

Architecture constants not present in this exact GGUF are pinned to Google's
Gemma3 27B configuration and Orb's recorded Ollama Gemma3 loader settings:
https://github.com/google/gemma_pytorch/blob/014acb7ac4563a5f77c76d7ff98f31b568c16508/gemma/config.py
https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/models/gemma3_text.py
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import re
import struct
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, ClassVar

SOURCE_SHA256 = "4b97da6b21a8fb1405a0d1efee462bfd6c4d3c46307bac8b34373835f07ca758"
SOURCE_BYTES = 17_397_211_520
TEMPLATE_SHA256 = "f79eab3dd094a88a34ba63d44a5b8a03504786626bd2316017752f1ecb14fac8"
SOURCE_TEXT_PARAMETERS = 27_009_346_304
SOURCE_TEXT_TENSORS = 808
GROUP_SIZE = 32
DEFAULT_CHUNK_BYTES = 64 * 1024 * 1024
MAX_CHUNK_BYTES = 128 * 1024 * 1024
CANDIDATE_LABEL = "medgemma-27b-text-mlx-native-mixed-4-6-requantized-candidate"
AFFINE8_CANDIDATE_LABEL = "medgemma-27b-text-mlx-native-affine-8-requantized-candidate"
QUANTISATION_PROFILES = ("source-mixed46", "affine8")

# This mirrors the pinned Ollama Go route, including its literal left-trim after
# the user heading. The actual Ollama0.40.4 oracle observed on 2026-10-21 proves
# that consecutive leading system bodies are joined with exactly two newlines,
# before the Go template adds one newline between that system and user content.
# No body is trimmed. The template embedded in the GGUF is a different route.
CHAT_TEMPLATE = """{{ bos_token }}{%- set ns = namespace(systems=[], turns=[]) -%}
{%- for message in messages -%}
{%- if message['content'] is not string -%}
{{ raise_exception('Only string text content is qualified') }}
{%- elif message['role'] == 'system' -%}
{%- if ns.turns -%}{{ raise_exception('System messages must form a contiguous leading prefix') }}{%- endif -%}
{%- set ns.systems = ns.systems + [message['content']] -%}
{%- elif message['role'] in ['user', 'assistant'] -%}
{%- set ns.turns = ns.turns + [message] -%}
{%- else -%}{{ raise_exception('Only text system/user/assistant messages are qualified') }}
{%- endif -%}
{%- endfor -%}
{%- if not ns.turns -%}{{ raise_exception('At least one text conversation turn is required') }}{%- endif -%}
{%- set system = ns.systems | join('\\n\\n') -%}
{%- for message in ns.turns -%}
{%- if message['role'] == 'user' -%}
{{ '<start_of_turn>user' }}
{{- system + '\\n' + message['content'] if loop.first and system else message['content'] -}}
{{ '<end_of_turn>\\n' }}
{%- elif message['role'] == 'assistant' -%}
{{ '<start_of_turn>model\\n' + message['content'] + '<end_of_turn>\\n' }}
{%- endif -%}
{%- if loop.last -%}{{ '<start_of_turn>model\\n' }}{%- endif -%}
{%- endfor -%}"""


class ConversionRefused(ValueError):
    """The input or output cannot be qualified for this bounded conversion."""


def candidate_label(quantisation_profile: str) -> str:
    if quantisation_profile not in QUANTISATION_PROFILES:
        raise ConversionRefused("Only source-mixed46 and affine8 quantisation profiles are supported.")
    return CANDIDATE_LABEL if quantisation_profile == "source-mixed46" else AFFINE8_CANDIDATE_LABEL


def target_bits(source_type: int, quantisation_profile: str) -> int | None:
    candidate_label(quantisation_profile)
    if source_type == 0:
        return None
    if source_type not in (12, 14):
        raise ConversionRefused("Only original float32 norms and Q4_K/Q6_K matrices are supported.")
    return 8 if quantisation_profile == "affine8" else 4 if source_type == 12 else 6


@dataclass(frozen=True)
class TensorSpec:
    source_name: str
    target_name: str
    shape: tuple[int, ...]
    source_type: int
    bits: int | None
    inverse_norm: bool


def expected_weight_data_bytes(specs: Sequence[TensorSpec]) -> int:
    """Packed matrices plus float16 affine scales/biases and float32 norms.

    This excludes the small safetensors headers and JSON/tokenizer metadata.
    It is a storage estimate, not an inference-memory or clinical-quality claim.
    """
    size = 0
    for spec in specs:
        values = math.prod(spec.shape)
        if spec.bits is None:
            size += values * 4
        else:
            size += values * spec.bits // 8 + values // GROUP_SIZE * 4
    return size


def sha256_file(path: Path, *, block_bytes: int = 1024 * 1024) -> str:
    """Hash sequentially, without reading a checkpoint into Python memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_bytes):
            digest.update(block)
    return digest.hexdigest()


def render_ollama_text(messages: Sequence[Mapping[str, Any]], *, bos: str = "<bos>") -> str:
    """Pinned Go text template plus its observed leading-system preprocessing."""
    if not messages:
        raise ConversionRefused("An empty message sequence is not qualified.")
    systems: list[str] = []
    turns: list[Mapping[str, Any]] = []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if not isinstance(content, str):
            raise ConversionRefused("Only string text content is qualified.")
        if role == "system" and not turns:
            systems.append(content)
        elif role in {"user", "assistant"}:
            turns.append(message)
        else:
            raise ConversionRefused("Only contiguous leading system messages and text turns are qualified.")
    if not turns:
        raise ConversionRefused("At least one text conversation turn is required.")
    system = "\n\n".join(systems)
    result = [bos]
    for index, message in enumerate(turns):
        role, content = message["role"], message["content"]
        if role == "user":
            result.extend(("<start_of_turn>user", f"{system}\n{content}" if index == 0 and system else content,
                           "<end_of_turn>\n"))
        elif role == "assistant":
            result.extend(("<start_of_turn>model\n", content, "<end_of_turn>\n"))
        if index == len(turns) - 1:
            result.append("<start_of_turn>model\n")
    return "".join(result)


_LAYER_NAMES = {
    "attn_q": "self_attn.q_proj", "attn_k": "self_attn.k_proj", "attn_v": "self_attn.v_proj",
    "attn_output": "self_attn.o_proj", "ffn_gate": "mlp.gate_proj", "ffn_up": "mlp.up_proj",
    "ffn_down": "mlp.down_proj", "attn_norm": "input_layernorm",
    "post_attention_norm": "post_attention_layernorm", "ffn_norm": "pre_feedforward_layernorm",
    "post_ffw_norm": "post_feedforward_layernorm", "attn_q_norm": "self_attn.q_norm",
    "attn_k_norm": "self_attn.k_norm",
}
_NORMS = {"attn_norm", "post_attention_norm", "ffn_norm", "post_ffw_norm", "attn_q_norm", "attn_k_norm"}


def tensor_mapping(name: str) -> tuple[str, bool] | None:
    """Map every text tensor, and refuse unknown tensors instead of dropping them."""
    if name.startswith(("v.", "mm.")):
        return None
    if name == "token_embd.weight":
        return "model.embed_tokens.weight", False
    if name == "output_norm.weight":
        return "model.norm.weight", True
    match = re.fullmatch(r"blk\.([0-9]+)\.([a-z_]+)\.weight", name)
    if match is None or match[2] not in _LAYER_NAMES or not 0 <= int(match[1]) < 62:
        raise ConversionRefused(f"Unrecognised text tensor: {name}")
    return f"model.layers.{int(match[1])}.{_LAYER_NAMES[match[2]]}.weight", match[2] in _NORMS


def expected_shapes(config: Mapping[str, Any]) -> dict[str, tuple[int, ...]]:
    hidden, head = int(config["hidden_size"]), int(config["head_dim"])
    q, kv, ff = int(config["num_attention_heads"]) * head, int(config["num_key_value_heads"]) * head, int(config["intermediate_size"])
    result = {"model.embed_tokens.weight": (int(config["vocab_size"]), hidden), "model.norm.weight": (hidden,)}
    for layer in range(int(config["num_hidden_layers"])):
        prefix = f"model.layers.{layer}."
        for name, shape in {
            "self_attn.q_proj": (q, hidden), "self_attn.k_proj": (kv, hidden), "self_attn.v_proj": (kv, hidden),
            "self_attn.o_proj": (hidden, q), "mlp.gate_proj": (ff, hidden), "mlp.up_proj": (ff, hidden),
            "mlp.down_proj": (hidden, ff), "self_attn.q_norm": (head,), "self_attn.k_norm": (head,),
            "input_layernorm": (hidden,), "post_attention_layernorm": (hidden,),
            "pre_feedforward_layernorm": (hidden,), "post_feedforward_layernorm": (hidden,),
        }.items():
            result[f"{prefix}{name}.weight"] = shape
    return result


def model_config(metadata: Mapping[str, Any], *, quantisation_profile: str = "source-mixed46") -> dict[str, Any]:
    """Explicitly pin the architecture; do not inherit MLX's 1B defaults."""
    label = candidate_label(quantisation_profile)
    required = {
        "general.architecture": "gemma3", "general.file_type": 15,
        "general.parameter_count": 27_432_406_640, "gemma3.block_count": 62,
        "gemma3.embedding_length": 5376, "gemma3.feed_forward_length": 21504,
        "gemma3.attention.head_count": 32, "gemma3.attention.head_count_kv": 16,
        "gemma3.attention.key_length": 128, "gemma3.attention.value_length": 128,
        "gemma3.attention.sliding_window": 1024, "gemma3.context_length": 131072,
        "tokenizer.ggml.bos_token_id": 2, "tokenizer.ggml.eos_token_id": 1,
        "tokenizer.ggml.add_bos_token": True, "tokenizer.ggml.add_eos_token": False,
    }
    for key, value in required.items():
        if key not in metadata or metadata[key] != value or type(metadata[key]) is not type(value):
            raise ConversionRefused(f"Pinned metadata differs: {key}")
    for key, value in {
        "gemma3.attention.layer_norm_rms_epsilon": 1e-6,
        "gemma3.rope.freq_base": 1_000_000.0,
        "gemma3.rope.freq_base_swa": 10_000.0,
        "gemma3.rope.scaling.type": "linear", "gemma3.rope.scaling.factor": 8.0,
    }.items():
        if key in metadata and (not math.isclose(float(metadata[key]), value, rel_tol=1e-6) if isinstance(value, float) else metadata[key] != value):
            raise ConversionRefused(f"Pinned architecture constant differs: {key}")
    tokens = metadata.get("tokenizer.ggml.tokens")
    if not isinstance(tokens, (list, tuple)) or len(tokens) != 262208:
        raise ConversionRefused("The pinned 262208-token vocabulary is required.")
    eos_ids = metadata.get("tokenizer.ggml.eos_token_ids")
    if eos_ids != [1, 106]:
        raise ConversionRefused("The pinned EOS/end-of-turn IDs are required.")
    return {
        "model_type": "gemma3_text", "architectures": ["Gemma3ForCausalLM"],
        "hidden_size": 5376, "num_hidden_layers": 62, "intermediate_size": 21504,
        "num_attention_heads": 32, "num_key_value_heads": 16, "head_dim": 128,
        "vocab_size": 262208, "rms_norm_eps": 1e-6, "query_pre_attn_scalar": 168,
        "sliding_window": 1024, "sliding_window_pattern": 6, "max_position_embeddings": 131072,
        "rope_theta": 1_000_000.0, "rope_local_base_freq": 10_000.0,
        "rope_scaling": {"type": "linear", "factor": 8.0},
        "tie_word_embeddings": True, "eos_token_id": [1, 106], "bos_token_id": 2,
        "pad_token_id": 0, "torch_dtype": "float16",
        "orb_candidate_label": label, "orb_quantisation_profile": quantisation_profile,
        "orb_clinical_quality_qualified": False,
        "orb_scope": "Sage text only; vision and imaging are not supported",
    }


def plan_tensors(tensors: Sequence[Any], config: Mapping[str, Any]) -> tuple[list[TensorSpec], list[str]]:
    quantisation_profile = config.get("orb_quantisation_profile", "source-mixed46")
    candidate_label(quantisation_profile)
    expected = expected_shapes(config)
    result, excluded, seen = [], [], set()
    for tensor in tensors:
        mapped = tensor_mapping(tensor.name)
        if mapped is None:
            excluded.append(tensor.name)
            continue
        target, inverse = mapped
        shape = tuple(int(value) for value in reversed(tensor.shape))
        kind = int(tensor.tensor_type)
        if target in seen or expected.get(target) != shape:
            raise ConversionRefused(f"Duplicate or incorrect tensor shape: {tensor.name}")
        if inverse:
            if kind != 0 or len(shape) != 1:
                raise ConversionRefused("All pinned text norms must be original float32 vectors.")
            bits = None
        else:
            if kind not in (12, 14) or len(shape) != 2 or shape[-1] % 256:
                raise ConversionRefused("Only pinned row-aligned Q4_K/Q6_K matrices are supported.")
            bits = target_bits(kind, quantisation_profile)
        seen.add(target)
        result.append(TensorSpec(tensor.name, target, shape, kind, bits, inverse))
    if seen != set(expected) or len(result) != SOURCE_TEXT_TENSORS:
        raise ConversionRefused("The complete pinned text backbone is required.")
    if sum(math.prod(item.shape) for item in result) != SOURCE_TEXT_PARAMETERS:
        raise ConversionRefused("The pinned text parameter count differs.")
    if {kind: sum(item.source_type == kind for item in result) for kind in (0, 12, 14)} != {0: 373, 12: 376, 14: 59}:
        raise ConversionRefused("The pinned per-tensor precision counts differ.")
    if len(excluded) != 439:
        raise ConversionRefused("The pinned vision/projector exclusion count differs.")
    return result, excluded


def chunk_ranges(rows: int, columns: int, max_dequant_bytes: int) -> Iterator[tuple[int, int]]:
    """Each float32 dequantized slice fits the explicit memory budget."""
    if min(rows, columns) < 1 or not 4 <= max_dequant_bytes <= MAX_CHUNK_BYTES:
        raise ConversionRefused("Invalid bounded row-conversion dimensions or budget.")
    row_bytes = columns * 4
    count = max_dequant_bytes // row_bytes
    if count < 1:
        raise ConversionRefused("One row exceeds the dequantization budget.")
    for start in range(0, rows, count):
        yield start, min(rows, start + count)


def dequantized_chunks(tensor: Any, spec: TensorSpec, max_dequant_bytes: int,
                      dequantize: Callable[[Any, Any], Any]) -> Iterator[tuple[int, Any]]:
    """Slice the memmap BEFORE dequantization, never materialize the entire matrix."""
    import numpy as np

    if spec.bits is None:
        raise ConversionRefused("The bounded matrix reader does not accept norm vectors.")
    rows, columns = spec.shape
    if tensor.data.shape[0] != rows:
        raise ConversionRefused("GGUF packed rows differ from the pinned tensor shape.")
    for start, end in chunk_ranges(rows, columns, max_dequant_bytes):
        values = dequantize(tensor.data[start:end], tensor.tensor_type)
        if values.shape != (end - start, columns) or values.dtype != np.float32:
            raise ConversionRefused("Dequantizer returned incorrect shape or precision.")
        if values.nbytes > max_dequant_bytes or not np.isfinite(values).all():
            raise ConversionRefused("Dequantized rows exceeded bounds or contain non-finite values.")
        yield start, values


class StreamingSafetensors:
    """Write predeclared, contiguous row slices without whole-tensor buffers."""
    _DTYPES: ClassVar[dict[str, str]] = {"uint32": "U32", "float16": "F16", "float32": "F32"}

    def __init__(self, path: Path, arrays: Mapping[str, tuple[str, tuple[int, ...]]]):
        if path.exists() or not arrays:
            raise ConversionRefused("A new safetensors destination is required.")
        self.path, self.arrays, self.next_rows, self.offsets = path, dict(arrays), {}, {}
        header: dict[str, Any] = {"__metadata__": {"format": "mlx", "orb_candidate": CANDIDATE_LABEL}}
        offset = 0
        for name, (dtype, shape) in arrays.items():
            if dtype not in self._DTYPES or not shape or min(shape) < 1:
                raise ConversionRefused("Invalid safetensors array declaration.")
            width = 2 if dtype == "float16" else 4
            nbytes = math.prod(shape) * width
            header[name] = {"dtype": self._DTYPES[dtype], "shape": list(shape), "data_offsets": [offset, offset + nbytes]}
            self.offsets[name], self.next_rows[name] = offset, 0
            offset += nbytes
        encoded = json.dumps(header, separators=(",", ":")).encode()
        encoded += b" " * ((-len(encoded)) % 8)
        self.header_bytes, self.data_bytes = 8 + len(encoded), offset
        self.stream = path.open("xb")
        self.stream.write(struct.pack("<Q", len(encoded)))
        self.stream.write(encoded)
        self.stream.truncate(self.header_bytes + offset)

    def write(self, name: str, row: int, values: Any) -> None:
        import numpy as np

        if name not in self.arrays or row != self.next_rows[name]:
            raise ConversionRefused("Safetensors writes must cover each array sequentially exactly once.")
        dtype, shape = self.arrays[name]
        values = np.asarray(values)
        if str(values.dtype) != dtype or values.ndim != len(shape) or tuple(values.shape[1:]) != shape[1:]:
            raise ConversionRefused("Safetensors row dtype or shape differs.")
        if not 0 < len(values) <= shape[0] - row:
            raise ConversionRefused("Safetensors row range exceeds the declared array.")
        row_bytes = math.prod(shape[1:]) * values.dtype.itemsize
        self.stream.seek(self.header_bytes + self.offsets[name] + row * row_bytes)
        data = np.ascontiguousarray(values).tobytes()
        if self.stream.write(data) != len(data):
            raise ConversionRefused("A checkpoint write was incomplete.")
        self.next_rows[name] += len(values)

    def finish(self) -> None:
        if any(self.next_rows[name] != shape[0] for name, (_, shape) in self.arrays.items()):
            raise ConversionRefused("A checkpoint array has unwritten rows.")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        if self.path.stat().st_size != self.header_bytes + self.data_bytes:
            raise ConversionRefused("A checkpoint file has an incorrect size.")

    def close(self) -> None:
        self.stream.close()


def inverse_norm(values: Any) -> Any:
    import numpy as np

    values = np.asarray(values)
    if values.dtype != np.float32 or values.ndim != 1 or not np.isfinite(values).all():
        raise ConversionRefused("A source norm must be a finite float32 vector.")
    return values - np.float32(1.0)


def template_token_ids(tokenizer: Any, messages: Sequence[Mapping[str, Any]]) -> list[int]:
    """Request IDs explicitly: Transformers 5 defaults to a BatchEncoding dict."""
    ids = tokenizer.apply_chat_template(messages, tokenize=True, return_dict=False, add_generation_prompt=True)
    if not isinstance(ids, list) or any(type(token) is not int for token in ids):
        raise ConversionRefused("The tokenizer did not return a flat sequence of integer IDs.")
    return ids


def set_pinned_sentencepiece_prefix(backend: Any) -> None:
    """Match the observed GGUF runner's dummy prefix around added specials.

    Transformers5.19's Gemma GGUF conversion only replaces spaces with U+2581.
    This pinned GGUF/Ollama runner also prefixes every ordinary text segment,
    including segments following <start_of_turn>. The 2026-10-21 observed Sage
    oracle establishes exact token IDs after this change; the prompt is untouched.
    """
    from tokenizers import normalizers

    backend.normalizer = normalizers.Sequence([normalizers.Prepend("▁"), normalizers.Replace(" ", "▁")])


def write_tokenizer(source: Path, output: Path, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the local GGUF vocabulary; no Hugging Face lookup is performed."""
    from tokenizers.processors import TemplateProcessing
    from transformers import PreTrainedTokenizerFast
    from transformers.integrations.gguf.gguf_tokenizer_mapping import (
        convert_gguf_tokenizer,
        get_gguf_tokenizer,
    )

    architecture, values, _ = get_gguf_tokenizer(str(source))
    if architecture != "gemma3":
        raise ConversionRefused("Only the pinned Gemma3 tokenizer is supported.")
    backend, _ = convert_gguf_tokenizer(architecture, values)
    set_pinned_sentencepiece_prefix(backend)
    tokens = metadata["tokenizer.ggml.tokens"]
    backend.post_processor = TemplateProcessing(single="<bos>:0 $A:0", special_tokens=[("<bos>", 2)])
    for token_id, token in enumerate(tokens):
        spelling = "▁" * len(token) if " " in token and not token.strip() else token
        if backend.token_to_id(spelling) != token_id:
            raise ConversionRefused("Extracted tokenizer vocabulary IDs differ from the source.")
    if backend.get_vocab_size() != 262208:
        raise ConversionRefused("Extracted tokenizer added or removed vocabulary entries.")
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, bos_token=tokens[2], eos_token=tokens[1],
                                       unk_token=tokens[3], pad_token=tokens[0], chat_template=CHAT_TEMPLATE,
                                       model_max_length=131072, padding_side="left")
    probes = [
        [{"role": "user", "content": "Hello"}],
        [{"role": "system", "content": "Use only the chart.\nDo not invent facts."},
         {"role": "user", "content": "  Na⁺ 141 mmol/L\nPatient Δ, bed 7.  "}],
        [{"role": "system", "content": "System"}, {"role": "user", "content": "A"},
         {"role": "assistant", "content": "B\n"}, {"role": "user", "content": "C"}],
        [{"role": "system", "content": "Keep facts.\n"}, {"role": "system", "content": "  Retrieved context.\n"},
         {"role": "user", "content": " Summarise Δ. "}],
    ]
    probe_hashes = []
    for messages in probes:
        expected = render_ollama_text(messages, bos=tokens[2])
        rendered = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        if rendered != expected:
            raise ConversionRefused("Extracted tokenizer does not preserve the pinned text template.")
        ids = tokenizer.encode(rendered, add_special_tokens=False)
        templated_ids = template_token_ids(tokenizer, messages)
        if ids != templated_ids or not ids or ids[0] != 2 or ids.count(2) != 1:
            raise ConversionRefused("Extracted chat template changed BOS or tokenization.")
        probe_hashes.append({"prompt_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
                             "token_ids_sha256": hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
                             "prompt_tokens": len(ids)})
    tokenizer.save_pretrained(str(output))
    return {"vocabulary_entries": len(tokens), "all_vocabulary_ids_checked": True,
            "template_sha256": hashlib.sha256(CHAT_TEMPLATE.encode()).hexdigest(),
            "ordinary_segment_prefix": "U+2581 before space replacement, including ordinary text after added special tokens; pinned Ollama0.40.4 runner oracle",
            "local_template_self_checks": probe_hashes,
            "ollama_rendered_prompt_oracle": "PENDING; self-checks alone do not establish cross-engine parity"}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def convert(source: Path, output: Path, reader: Any, metadata: Mapping[str, Any], specs: Sequence[TensorSpec],
            excluded: Sequence[str], *, max_dequant_bytes: int = DEFAULT_CHUNK_BYTES,
            quantisation_profile: str = "source-mixed46") -> dict[str, Any]:
    """Execute bounded conversion only; NEVER loads the complete MLX model."""
    label = candidate_label(quantisation_profile)
    if any(spec.bits != target_bits(spec.source_type, quantisation_profile) for spec in specs):
        raise ConversionRefused("The planned tensor precisions differ from the explicit quantisation profile.")

    import mlx.core as mx
    import numpy as np
    from gguf import dequantize

    started = time.monotonic()
    if source.stat().st_size != SOURCE_BYTES or sha256_file(source) != SOURCE_SHA256:
        raise ConversionRefused("The installed GGUF does not match the pinned source SHA-256/size.")
    if output.exists() or not output.is_absolute() or not output.parent.is_dir():
        raise ConversionRefused("Use a new absolute output directory with an existing parent.")
    if not 1024 * 1024 <= max_dequant_bytes <= MAX_CHUNK_BYTES:
        raise ConversionRefused("The conversion slice budget must be between 1 and 128 MiB.")
    output.mkdir(mode=0o700)
    marker = output / ".conversion-incomplete"
    marker.write_text("Do not load: conversion is incomplete.\n")
    manifest: dict[str, Any] = {
        "status": "converting", "candidate_label": label,
        "quantisation_profile": quantisation_profile,
        "clinical_quality_qualified": False, "lossless_conversion": False,
        "source_sha256": SOURCE_SHA256, "source_bytes": SOURCE_BYTES,
        "source_text_parameters": SOURCE_TEXT_PARAMETERS, "source_text_tensors": len(specs),
        "excluded_vision_projector_tensors": list(excluded),
        "scope": "Sage text only; vision and imaging are not supported",
        "quantization": f"Original pinned GGUF Q4_K/Q6_K -> float32 dequantization -> float16 -> native MLX affine {'8-bit' if quantisation_profile == 'affine8' else '4/6-bit'}, group32; additional rounding/requantization; no cascading from a prior MLX candidate",
        "expected_weight_data_bytes_excluding_headers": expected_weight_data_bytes(specs),
        "maximum_float32_dequantized_slice_bytes": max_dequant_bytes,
        "architecture_constant_provenance": "Google Gemma3 27B config 014acb7ac4563a5f77c76d7ff98f31b568c16508; Orb recorded Ollama Gemma3 loader settings",
        "software_versions": {name: version(name) for name in ("mlx", "mlx-lm", "gguf", "numpy", "transformers", "tokenizers")},
        "converter_sha256": sha256_file(Path(__file__).resolve()), "files": {}, "matrices": [],
        "quality_gates": {"token_ids_vs_ollama": "pending", "clinical_regressions": "pending", "long_context": "pending", "load_test": "pending"},
    }
    _write_json(output / "conversion-manifest.json", manifest)
    config = model_config(metadata, quantisation_profile=quantisation_profile)
    quantization: dict[str, Any] = {"group_size": GROUP_SIZE,
                                    "bits": 8 if quantisation_profile == "affine8" else 4, "mode": "affine"}
    index: dict[str, str] = {}
    tensors_by_name = {tensor.name: tensor for tensor in reader.tensors}
    try:
        # Refuse tokenizer/template inconsistencies before writing gigabytes of
        # converted tensors. These are local self-checks, not the Ollama oracle.
        manifest["tokenizer"] = write_tokenizer(source, output, metadata)
        # The active source and materialized MLX chunks stay bounded. Completed
        # packed rows go straight to disk; no list of whole tensor arrays exists.
        for number, spec in enumerate(specs, 1):
            tensor = tensors_by_name[spec.source_name]
            filename = f"model-{number:05d}-of-{len(specs):05d}.safetensors"
            path = output / filename
            writer: StreamingSafetensors | None = None
            try:
                if spec.bits is None:
                    values = inverse_norm(tensor.data)
                    writer = StreamingSafetensors(path, {spec.target_name: ("float32", spec.shape)})
                    writer.write(spec.target_name, 0, values)
                    index[spec.target_name] = filename
                    del values
                else:
                    rows, columns = spec.shape
                    prefix = spec.target_name.removesuffix(".weight")
                    arrays = {
                        spec.target_name: ("uint32", (rows, columns * spec.bits // 32)),
                        f"{prefix}.scales": ("float16", (rows, columns // GROUP_SIZE)),
                        f"{prefix}.biases": ("float16", (rows, columns // GROUP_SIZE)),
                    }
                    writer = StreamingSafetensors(path, arrays)
                    quantization[prefix] = {"group_size": GROUP_SIZE, "bits": spec.bits, "mode": "affine"}
                    n_chunks = 0
                    squared_error, squared_reference, max_abs_error, n_values = 0.0, 0.0, 0.0, 0
                    # Record sampled quantization error, rather than pretending
                    # equivalent bits make this an identical checkpoint.
                    for row, values in dequantized_chunks(tensor, spec, max_dequant_bytes, dequantize):
                        half_values = values.astype(np.float16)
                        if not np.isfinite(half_values).all():
                            raise ConversionRefused("A source value cannot be represented in candidate float16 precision.")
                        x = mx.array(half_values)
                        del half_values
                        packed, scales, biases = mx.quantize(x, group_size=GROUP_SIZE, bits=spec.bits, mode="affine")
                        mx.eval(packed, scales, biases)
                        if not np.isfinite(np.asarray(scales)).all() or not np.isfinite(np.asarray(biases)).all():
                            raise ConversionRefused("Re-quantization produced non-finite scales or biases.")
                        writer.write(spec.target_name, row, np.asarray(packed))
                        writer.write(f"{prefix}.scales", row, np.asarray(scales))
                        writer.write(f"{prefix}.biases", row, np.asarray(biases))
                        if row == 0:
                            restored = np.asarray(mx.dequantize(packed[:1], scales[:1], biases[:1], group_size=GROUP_SIZE, bits=spec.bits)).astype(np.float32)
                            reference = values[:1]
                            difference = restored - reference
                            if not np.isfinite(restored).all():
                                raise ConversionRefused("Re-quantized matrix produced non-finite values.")
                            squared_error += float(np.sum(difference.astype(np.float64) ** 2))
                            squared_reference += float(np.sum(reference.astype(np.float64) ** 2))
                            max_abs_error = max(max_abs_error, float(np.abs(difference).max()))
                            n_values += reference.size
                            del restored, reference, difference
                        n_chunks += 1
                        del packed, scales, biases, x, values
                        mx.clear_cache()
                    manifest["matrices"].append({"source": spec.source_name, "target": spec.target_name,
                                                  "shape": list(spec.shape), "source_type": spec.source_type,
                                                  "native_bits": spec.bits, "group_size": GROUP_SIZE, "row_chunks": n_chunks,
                                                  "error_sample_values": n_values, "first_row_rmse": math.sqrt(squared_error / n_values),
                                                  "first_row_relative_l2": math.sqrt(squared_error / squared_reference) if squared_reference else None,
                                                  "first_row_max_abs_error": max_abs_error})
                    index.update({name: filename for name in arrays})
                writer.finish()
            finally:
                if writer is not None:
                    writer.close()
            manifest["files"][filename] = {"sha256": sha256_file(path), "bytes": path.stat().st_size,
                                             "tensor_data_bytes": writer.data_bytes}
            if number == 1 or number % 25 == 0 or number == len(specs):
                print(json.dumps({"event": "conversion_progress", "completed_tensors": number, "total_tensors": len(specs),
                                  "elapsed_seconds": round(time.monotonic() - started, 3)}), flush=True)
            gc.collect()
        config["quantization"] = quantization
        _write_json(output / "generation_config.json", {"bos_token_id": 2, "eos_token_id": [1, 106], "pad_token_id": 0})
        _write_json(output / "model.safetensors.index.json", {"metadata": {"total_size": sum(x["tensor_data_bytes"] for x in manifest["files"].values())}, "weight_map": index})
        # config.json is written only after every weight and tokenizer succeeds.
        _write_json(output / "config.json", config)
        for path in sorted(output.iterdir()):
            if path.is_file() and path.name not in manifest["files"] and path.name not in {marker.name, "conversion-manifest.json"}:
                manifest["files"][path.name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
        manifest["status"] = "converted_candidate_unqualified"
        manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_json(output / "conversion-manifest.json", manifest)
        marker.unlink()
        return manifest
    except BaseException as exc:
        manifest["status"] = "failed_incomplete_do_not_load"
        manifest["error_type"] = type(exc).__name__
        manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_json(output / "conversion-manifest.json", manifest)
        raise


def inspect(source: Path, models_dir: Path, *, quantisation_profile: str = "source-mixed46") -> tuple[Any, dict[str, Any], list[TensorSpec], list[str]]:
    from gguf import GGUFReader

    if not source.is_absolute() or not source.is_file() or source.stat().st_size != SOURCE_BYTES:
        raise ConversionRefused("Use the existing absolute pinned MedGemma GGUF blob.")
    template = models_dir / "blobs" / f"sha256-{TEMPLATE_SHA256}"
    if not template.is_file() or sha256_file(template) != TEMPLATE_SHA256:
        raise ConversionRefused("The pinned installed Ollama template must be present and unchanged.")
    reader = GGUFReader(str(source), mode="r")
    metadata = {name: field.contents() for name, field in reader.fields.items() if not name.startswith("GGUF.")}
    config = model_config(metadata, quantisation_profile=quantisation_profile)
    specs, excluded = plan_tensors(reader.tensors, config)
    return reader, metadata, specs, excluded


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-gguf", type=Path, required=True)
    parser.add_argument("--ollama-models-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-dequant-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument("--quantisation-profile", choices=QUANTISATION_PROFILES, default="source-mixed46")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-requantized-candidate", action="store_true")
    args = parser.parse_args(argv)
    try:
        reader, metadata, specs, excluded = inspect(args.source_gguf, args.ollama_models_dir,
                                                   quantisation_profile=args.quantisation_profile)
        if not args.execute:
            print(json.dumps({"status": "plan_only_no_weights_loaded", "candidate_label": candidate_label(args.quantisation_profile),
                              "quantisation_profile": args.quantisation_profile,
                              "clinical_quality_qualified": False, "lossless_conversion": False,
                              "source_sha256_expected": SOURCE_SHA256, "source_digest_verified": False,
                              "source_text_tensors": len(specs), "source_text_parameters": SOURCE_TEXT_PARAMETERS,
                              "excluded_vision_projector_tensors": len(excluded), "output_dir": str(args.output_dir),
                              "maximum_float32_dequantized_slice_bytes": args.max_dequant_bytes,
                              "expected_weight_data_bytes_excluding_headers": expected_weight_data_bytes(specs),
                              "scope": "Sage text only"}, indent=2))
            return 0
        if not args.acknowledge_requantized_candidate:
            raise ConversionRefused("Explicitly acknowledge a re-quantized, clinically unqualified candidate.")
        os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"})
        manifest = convert(args.source_gguf, args.output_dir, reader, metadata, specs, excluded,
                           max_dequant_bytes=args.max_dequant_bytes, quantisation_profile=args.quantisation_profile)
        print(json.dumps({"status": manifest["status"], "clinical_quality_qualified": False,
                          "output_dir": str(args.output_dir), "elapsed_seconds": manifest["elapsed_seconds"]}, indent=2))
        return 0
    except (ConversionRefused, OSError, ImportError, ValueError) as exc:
        print(json.dumps({"status": "refused_or_incomplete", "error_type": type(exc).__name__, "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
