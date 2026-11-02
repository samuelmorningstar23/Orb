"""Offline model boundaries and the dense/reranker compatibility contracts.

Only synthetic tensors/configurations are used. No cache, network, clinical
database, downloaded weights or model provisioning is involved.
"""
from types import ModuleType, SimpleNamespace
import sys

import numpy as np
import pytest

from services import local_transformers as local


def test_cookie_isolation_does_not_trigger_lazy_module_imports(monkeypatch):
    from tests import conftest
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    probed = []

    class LazyModule(ModuleType):
        def __getattr__(self, name):
            probed.append(name)
            raise RuntimeError("optional model module must not be imported by cookie cleanup")

    declared = ModuleType("synthetic_declared_client")
    declared.client = TestClient(FastAPI())
    declared.client.cookies.set("synthetic-session", "old-session")
    monkeypatch.setitem(sys.modules, declared.__name__, declared)
    monkeypatch.setitem(sys.modules, "synthetic_lazy_model", LazyModule("synthetic_lazy_model"))
    fixture = conftest._isolate_session_cookies.__wrapped__()
    next(fixture)
    with pytest.raises(StopIteration):
        next(fixture)
    assert probed == []
    assert not declared.client.cookies


def test_local_snapshot_never_retries_a_remote_name(tmp_path, monkeypatch):
    calls = []

    def snapshot(name, **kwargs):
        calls.append((name, kwargs))
        raise OSError("not cached")

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot))
    with pytest.raises(OSError, match="not cached"):
        local.local_snapshot("synthetic/uncached-model")
    assert calls == [("synthetic/uncached-model", {"local_files_only": True})]


def test_cached_snapshot_resolution_is_explicitly_offline(tmp_path, monkeypatch):
    calls = []

    def snapshot(name, **kwargs):
        calls.append((name, kwargs))
        return str(tmp_path)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot))
    assert local.local_snapshot("synthetic/cached-model") == tmp_path.resolve()
    assert calls == [("synthetic/cached-model", {"local_files_only": True})]


@pytest.mark.parametrize("files,safe", [
    (["model.safetensors"], True),
    (["model.safetensors.index.json"], True),
    (["pytorch_model.bin"], False),
    (["pytorch_model.bin.index.json"], False),
    (["model.safetensors", "pytorch_model.bin"], True),
])
@pytest.mark.parametrize("reranker", [False, True])
def test_loaders_use_local_builtin_classes_and_safe_tensor_loading(tmp_path, monkeypatch, files, safe, reranker):
    for filename in files:
        (tmp_path / filename).touch()
    calls = []

    class Model:
        def float(self):
            calls.append("float")
            return self

        def half(self):
            calls.append("half")
            return self

        def to(self, device):
            calls.append(("device", str(device)))

        def eval(self):
            calls.append("eval")

    class Factory:
        def __init__(self, name):
            self.name = name

        def from_pretrained(self, path, **kwargs):
            calls.append((self.name, path, kwargs))
            return Model() if self.name != "tokenizer" else object()

    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=Factory("tokenizer"), AutoModel=Factory("dense"),
        AutoModelForSequenceClassification=Factory("reranker")))
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    local._load(str(tmp_path), reranker=reranker, use_fp16=True)
    assert calls[:2] == [
        ("tokenizer", str(tmp_path.resolve()), {"local_files_only": True, "trust_remote_code": False}),
        ("reranker" if reranker else "dense", str(tmp_path.resolve()), {
            "local_files_only": True, "trust_remote_code": False,
            "use_safetensors": safe, "weights_only": True}),
    ]
    assert calls[2:] == ["float", ("device", "cpu"), "eval"]


def test_missing_weights_are_not_converted_or_downloaded(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    # No Transformers import is reached when a local snapshot has no weights.
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(RuntimeError, match="Local model weights unavailable"):
        local._load(str(tmp_path), reranker=False, use_fp16=False)


class Tokenizer:
    def __init__(self):
        self.calls = []
        self.prepared = []
        self.model_input_names = ["input_ids", "attention_mask"]
        self.backend_tokenizer = PairBackend(self)

    def __call__(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        ids = [[ord(char) % 25 + 3 for char in text][:kwargs.get("max_length", 512)] for text in texts]
        if kwargs.get("return_tensors") == "pt":
            return self.pad([{"input_ids": row or [0]} for row in ids], padding=True, return_tensors="pt")
        return SimpleNamespace(encodings=[SimpleNamespace(ids=row) for row in ids])

    def pad(self, rows, **kwargs):
        import torch
        size = max(len(row["input_ids"]) for row in rows)
        ids = [row["input_ids"] + [1] * (size - len(row["input_ids"])) for row in rows]
        mask = [[1] * len(row["input_ids"]) + [0] * (size - len(row["input_ids"])) for row in rows]
        return {"input_ids": torch.tensor(ids), "attention_mask": torch.tensor(mask)}


class PairBackend:
    def __init__(self, owner):
        self.owner = owner
        self.truncation = None

    def enable_truncation(self, **kwargs):
        self.truncation = kwargs

    def no_truncation(self):
        self.truncation = None

    def post_process(self, query, passage, *, add_special_tokens):
        assert add_special_tokens is True
        assert self.truncation["strategy"] == "only_second"
        maximum = self.truncation["max_length"]
        self.owner.prepared.append((list(query.ids), list(passage.ids), maximum))
        remaining = maximum - len(query.ids) - 4
        assert remaining >= 0
        ids = [0, *query.ids, 2, 2, *passage.ids[:remaining], 2]
        return SimpleNamespace(ids=ids, attention_mask=[1] * len(ids))


class DenseModel:
    config = SimpleNamespace(hidden_size=3)

    def __init__(self, *, invalid=None):
        self.invalid = invalid
        self.calls = []

    def __call__(self, input_ids, **kwargs):
        import torch
        assert not torch.is_grad_enabled()
        self.calls.append(input_ids.clone())
        first = torch.stack((input_ids[:, 0].float(), torch.ones(len(input_ids)) * 4,
                             torch.ones(len(input_ids)) * 2), dim=-1)
        if self.invalid == "zero":
            first[:] = 0
        if self.invalid == "nan":
            first[0, 0] = float("nan")
        hidden = torch.stack((first, first * -10), dim=1)
        if self.invalid == "shape":
            hidden = hidden[:, :, :2]
        return SimpleNamespace(last_hidden_state=hidden)


def test_dense_vectors_use_normalized_cls_and_preserve_order_and_float32(monkeypatch):
    tokenizer, model = Tokenizer(), DenseModel()
    monkeypatch.setattr(local, "_load", lambda *a, **k: (tokenizer, model, "cpu"))
    embedder = local.LocalDenseEmbedder("synthetic", dimension=3)
    result = embedder.encode(["a", "b", "c"], batch_size=2, max_length=7)["dense_vecs"]
    expected = np.asarray([[ord(c) % 25 + 3, 4, 2] for c in "abc"], dtype=np.float32)
    expected /= np.linalg.norm(expected, axis=1, keepdims=True)
    np.testing.assert_allclose(result, expected, atol=1e-7)
    assert result.dtype == np.float32 and result.shape == (3, 3)
    assert [len(call) for call in model.calls] == [2, 1]
    assert all(kwargs == {"padding": True, "truncation": True, "max_length": 7,
                          "return_tensors": "pt"} for _, kwargs in tokenizer.calls)
    assert embedder.encode([])["dense_vecs"].shape == (0, 3)
    assert len(model.calls) == 2


@pytest.mark.parametrize("invalid", ["zero", "nan", "shape"])
def test_invalid_dense_output_is_refused(monkeypatch, invalid):
    monkeypatch.setattr(local, "_load", lambda *a, **k: (Tokenizer(), DenseModel(invalid=invalid), "cpu"))
    embedder = local.LocalDenseEmbedder("synthetic", dimension=3)
    with pytest.raises(ValueError, match="dense embedding"):
        embedder.encode(["alpha"])


class ScoreModel:
    config = SimpleNamespace(num_labels=1)

    def __init__(self, *, invalid=None):
        self.invalid = invalid
        self.cursor = 0
        self.calls = []

    def __call__(self, input_ids, **kwargs):
        import torch
        assert not torch.is_grad_enabled()
        self.calls.append(input_ids.clone())
        logits = torch.tensor([[-2.0], [0.0], [2.0]])[self.cursor:self.cursor + len(input_ids)]
        self.cursor += len(input_ids)
        if self.invalid == "nan":
            logits[0, 0] = float("nan")
        if self.invalid == "shape":
            logits = torch.cat((logits, logits), dim=1)
        return SimpleNamespace(logits=logits)


@pytest.mark.parametrize("normalize", [False, True])
def test_reranker_preserves_sigmoid_order_and_separate_query_bound(monkeypatch, normalize):
    tokenizer, model = Tokenizer(), ScoreModel()
    monkeypatch.setattr(local, "_load", lambda *a, **k: (tokenizer, model, "cpu"))
    reranker = local.LocalReranker("synthetic")
    result = reranker.compute_score([["q" * 800, "p" * 900], ["x", "y"], ["a", "b"]],
                                    batch_size=2, normalize=normalize)
    expected = [-2.0, 0.0, 2.0]
    if normalize:
        expected = [1 / (1 + np.exp(-value)) for value in expected]
    np.testing.assert_allclose(result, expected, atol=1e-7)
    assert [kwargs["max_length"] for _, kwargs in tokenizer.calls] == [384, 512, 384, 512]
    assert len(tokenizer.prepared[0][0]) == 384
    assert len(tokenizer.prepared[0][1]) == 512
    assert all(maximum == 512 for _, _, maximum in tokenizer.prepared)
    assert tokenizer.backend_tokenizer.truncation is None
    assert reranker.compute_score([]) == []


@pytest.mark.parametrize("invalid", ["nan", "shape"])
def test_invalid_reranker_scores_are_refused(monkeypatch, invalid):
    monkeypatch.setattr(local, "_load", lambda *a, **k: (Tokenizer(), ScoreModel(invalid=invalid), "cpu"))
    with pytest.raises(ValueError, match="reranker scores"):
        local.LocalReranker("synthetic").compute_score([["query", "passage"]])


@pytest.mark.parametrize("count,expected_batches", [(12, [8, 4]), (17, [8, 8, 1])])
def test_default_reranker_batch_preserves_pair_order_and_explicit_eight_scores(
    monkeypatch, count, expected_batches
):
    """The CPU default and explicit sizes retain every score in original order."""
    class PairScoreModel:
        config = SimpleNamespace(num_labels=1)

        def __init__(self):
            self.calls = []

        def __call__(self, input_ids, attention_mask, **kwargs):
            self.calls.append(input_ids.clone())
            # Ignore padding: scores depend on the exact ordered pair, not batch shape.
            logits = (input_ids * attention_mask).sum(dim=1).float() / 100 - 2
            return SimpleNamespace(logits=logits[:, None])

    models = []

    def load(*args, **kwargs):
        model = PairScoreModel()
        models.append(model)
        return Tokenizer(), model, "cpu"

    monkeypatch.setattr(local, "_load", load)
    pairs = [[f"query {index}", "passage " + "x" * (index + 1)] for index in range(count)]
    default = local.LocalReranker("synthetic").compute_score(pairs)
    baseline = local.LocalReranker("synthetic").compute_score(pairs, batch_size=8)
    larger = local.LocalReranker("synthetic").compute_score(pairs, batch_size=16)
    assert [len(call) for call in models[0].calls] == expected_batches
    assert [len(call) for call in models[2].calls] == ([12] if count == 12 else [16, 1])
    assert len(default) == count and default == baseline == larger
    assert len(set(default)) == count


@pytest.mark.parametrize("device,dtype_name,expected", [
    ("cpu", "float32", 8), ("cpu", "float16", 8),
    ("cuda", "float16", 8), ("mps", "float32", 8), ("mps", "float16", 16),
])
def test_implicit_reranker_batch_is_scoped_to_actual_qualified_device_dtype(
    monkeypatch, device, dtype_name, expected
):
    """Metadata selects a default without allocating on an unavailable accelerator."""
    import torch

    model = SimpleNamespace(config=SimpleNamespace(num_labels=1), dtype=getattr(torch, dtype_name))
    monkeypatch.setattr(local, "_load", lambda *a, **k: (Tokenizer(), model, torch.device(device)))
    reranker = local.LocalReranker("synthetic")
    assert reranker._default_batch_size == expected
    assert reranker.compute_score([]) == []
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        reranker.compute_score([], batch_size=0)


def test_real_transformers5_tokenizer_preserves_pair_special_tokens_and_query_cap(monkeypatch):
    """Use the real Rust tokenizer API which replaced prepare_for_model in v5."""
    from tokenizers import Tokenizer as RustTokenizer, models, pre_tokenizers, processors
    from transformers import PreTrainedTokenizerFast

    backend = RustTokenizer(models.WordLevel(
        {"[CLS]": 0, "[PAD]": 1, "[SEP]": 2, "[UNK]": 3, "query": 4, "passage": 5},
        unk_token="[UNK]"))
    backend.pre_tokenizer = pre_tokenizers.Whitespace()
    backend.post_processor = processors.RobertaProcessing(("[SEP]", 2), ("[CLS]", 0))
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="[UNK]", pad_token="[PAD]",
                                        cls_token="[CLS]", sep_token="[SEP]")
    model = ScoreModel()
    monkeypatch.setattr(local, "_load", lambda *a, **k: (tokenizer, model, "cpu"))
    result = local.LocalReranker("synthetic").compute_score([["query " * 600, "passage " * 600]])
    assert len(result) == 1
    ids = model.calls[0][0].tolist()
    assert ids == [0, *([4] * 384), 2, 2, *([5] * 124), 2]
    assert len(ids) == 512
    assert tokenizer.backend_tokenizer.truncation["strategy"] == "longest_first"


@pytest.mark.parametrize("kind", ["dense", "reranker"])
def test_builtin_transformers_random_configuration_forward_is_offline(monkeypatch, kind):
    """Exercise the actual pinned built-in model API without weights or hub I/O."""
    from transformers import AutoModel, AutoModelForSequenceClassification, XLMRobertaConfig
    config = XLMRobertaConfig(vocab_size=32, hidden_size=8, num_hidden_layers=1,
                              num_attention_heads=2, intermediate_size=16,
                              max_position_embeddings=64, num_labels=1)
    factory = AutoModel if kind == "dense" else AutoModelForSequenceClassification
    model = factory.from_config(config).eval()
    monkeypatch.setattr(local, "_load", lambda *a, **k: (Tokenizer(), model, "cpu"))
    if kind == "dense":
        vectors = local.LocalDenseEmbedder("synthetic", dimension=8).encode(["alpha", "beta"])["dense_vecs"]
        assert vectors.shape == (2, 8)
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), [1, 1], atol=1e-6)
    else:
        scores = local.LocalReranker("synthetic").compute_score([["query", "passage"], ["x", "y"]])
        assert len(scores) == 2 and all(0 <= value <= 1 for value in scores)


def test_coreml_converter_refuses_arbitrary_pickle_objects_before_conversion(tmp_path, monkeypatch):
    """The offline developer converter has the same tensor-only load boundary."""
    import torch
    from scripts.convert_to_coreml import convert_vigil_cfc

    checkpoint = tmp_path / "synthetic-checkpoint.pt"
    torch.save({"model_state_dict": {}, "payload": SimpleNamespace(value="not-a-tensor")}, checkpoint)
    monkeypatch.setitem(sys.modules, "coremltools", SimpleNamespace(
        convert=lambda *a, **k: pytest.fail("unsafe checkpoint reached CoreML conversion")))
    with pytest.raises(Exception, match="Weights only load failed|Unsupported global"):
        convert_vigil_cfc(str(checkpoint), str(tmp_path / "unused-output"))
    assert not (tmp_path / "unused-output").exists()
