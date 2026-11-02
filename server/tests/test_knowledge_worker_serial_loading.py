"""CPU-only same-checkpoint proof for serial Transformers parameter loading."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services import knowledge_worker as kw


def test_worker_forces_serial_loading_before_torch_import_even_if_environment_disabled_it(
    monkeypatch: Any,
) -> None:
    import os

    events = []
    monkeypatch.delenv("ORB_KNOWLEDGE_WORKER", raising=False)
    monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
    monkeypatch.setenv("HF_DEACTIVATE_ASYNC_LOAD", "0")
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    monkeypatch.setattr(kw, "_start_parent_watchdog", lambda: events.append(
        ("watchdog", os.environ["HF_DEACTIVATE_ASYNC_LOAD"])))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(set_num_threads=lambda count: events.append(
        ("torch_threads", os.environ["HF_DEACTIVATE_ASYNC_LOAD"], count))))
    kw._worker_init()
    assert os.environ["HF_DEACTIVATE_ASYNC_LOAD"] == "1"
    assert os.environ["ORB_KNOWLEDGE_WORKER"] == "1"
    assert events == [("watchdog", "1"), ("torch_threads", "1", 4)]


@pytest.mark.parametrize("reranker", [False, True])
def test_complete_local_checkpoint_async_and_serial_loading_are_bitwise_identical(
    tmp_path: Path, monkeypatch: Any, reranker: bool,
) -> None:
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        monkeypatch.setenv(name, "1")
    import torch
    from transformers import (
        AutoModel,
        AutoModelForSequenceClassification,
        XLMRobertaConfig,
        XLMRobertaForSequenceClassification,
        XLMRobertaModel,
        core_model_loading,
    )

    # Both production BGE architectures, reduced to a complete tiny synthetic
    # CPU checkpoint. No existing model, device allocator or DB is touched.
    torch.manual_seed(20261023)
    config = XLMRobertaConfig(vocab_size=32, hidden_size=16, intermediate_size=24,
                             num_hidden_layers=1, num_attention_heads=2,
                             max_position_embeddings=32, type_vocab_size=1,
                             hidden_dropout_prob=0, attention_probs_dropout_prob=0,
                             num_labels=1)
    with torch.device("cpu"):
        original = (XLMRobertaForSequenceClassification(config) if reranker
                    else XLMRobertaModel(config))
    original.eval()
    original.save_pretrained(tmp_path, safe_serialization=True)
    checkpoint = tmp_path / "model.safetensors"
    checkpoint_sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest()

    pools = []
    actual_pool = core_model_loading.ThreadPoolExecutor

    def observed_pool(*args: Any, **kwargs: Any) -> Any:
        pools.append(kwargs.get("max_workers"))
        return actual_pool(*args, **kwargs)

    monkeypatch.setattr(core_model_loading, "ThreadPoolExecutor", observed_pool)
    factory = AutoModelForSequenceClassification if reranker else AutoModel
    loaded = []
    for flag in ("0", "1"):
        monkeypatch.setenv("HF_DEACTIVATE_ASYNC_LOAD", flag)
        model, info = factory.from_pretrained(str(tmp_path), local_files_only=True,
                                              trust_remote_code=False, weights_only=True,
                                              use_safetensors=True, output_loading_info=True)
        assert info["missing_keys"] == set() or info["missing_keys"] == []
        assert info["unexpected_keys"] == set() or info["unexpected_keys"] == []
        assert not info["mismatched_keys"]
        assert all(parameter.device.type == "cpu" for parameter in model.parameters())
        assert all(parameter.dtype == torch.float32 for parameter in model.parameters())
        loaded.append(model.eval())
        assert len(pools) == 1  # Async path exercised; serial path creates no pool.

    def bitwise_equal(left: Any, right: Any) -> bool:
        return (left.shape == right.shape and left.dtype == right.dtype
                and torch.equal(left.contiguous().view(torch.uint8), right.contiguous().view(torch.uint8)))

    reference = original.state_dict()
    assert set(reference) == set(loaded[0].state_dict()) == set(loaded[1].state_dict())
    for name, tensor in reference.items():
        assert bitwise_equal(tensor, loaded[0].state_dict()[name]), name
        assert bitwise_equal(tensor, loaded[1].state_dict()[name]), name
    first_buffers, second_buffers = dict(loaded[0].named_buffers()), dict(loaded[1].named_buffers())
    assert set(first_buffers) == set(second_buffers)
    assert all(bitwise_equal(tensor, second_buffers[name]) for name, tensor in first_buffers.items())
    inputs = {"input_ids": torch.tensor([[0, 3, 4, 2, 1], [0, 5, 6, 7, 2]], device="cpu"),
              "attention_mask": torch.tensor([[1, 1, 1, 1, 0], [1, 1, 1, 1, 1]], device="cpu")}
    with torch.inference_mode():
        outputs = [model(**inputs, return_dict=True) for model in loaded]
    if reranker:
        assert bitwise_equal(outputs[0].logits, outputs[1].logits)
    else:
        assert bitwise_equal(outputs[0].last_hidden_state, outputs[1].last_hidden_state)
        assert bitwise_equal(outputs[0].pooler_output, outputs[1].pooler_output)
    assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == checkpoint_sha
