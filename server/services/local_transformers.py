"""Offline inference for Orb's dense BGE embedder and single-logit reranker.

The application needs neither FlagEmbedding's training stack nor its sparse/
ColBERT heads. These adapters retain the dense CLS/L2 and sigmoid contracts,
using only Transformers' built-in model classes. Construction is lazy and must
remain in the knowledge worker (or the explicitly selected in-process mode).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def dependencies_available() -> bool:
    """Check optional packages without importing or constructing a model."""
    return all(importlib.util.find_spec(name) is not None
               for name in ("torch", "transformers"))


def local_snapshot(model_name: str) -> Path:
    """Return an existing local directory, never a remotely resolvable name.

    The explicit local-files flag remains effective even if a provisioning
    shell has disabled HF_HUB_OFFLINE. No failed local load retries a hub name.
    """
    path = Path(model_name).expanduser()
    if not path.is_dir():
        from huggingface_hub import snapshot_download

        path = Path(snapshot_download(model_name, local_files_only=True))
    if not path.is_dir():
        raise RuntimeError("Local model snapshot unavailable")
    return path.resolve()


def _weight_options(path: Path) -> dict:
    if any((path / name).is_file() for name in
           ("model.safetensors", "model.safetensors.index.json")):
        return {"use_safetensors": True, "weights_only": True}
    if any((path / name).is_file() for name in
           ("pytorch_model.bin", "pytorch_model.bin.index.json")):
        # Existing BGE-M3 installations ship a local tensor checkpoint. The
        # patched pinned torch supports it without arbitrary pickle objects.
        return {"use_safetensors": False, "weights_only": True}
    raise RuntimeError("Local model weights unavailable")


def _load(model_name: str, *, reranker: bool, use_fp16: bool):
    path = local_snapshot(model_name)
    weights = _weight_options(path)
    import torch
    from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer

    options = {"local_files_only": True, "trust_remote_code": False}
    tokenizer = AutoTokenizer.from_pretrained(str(path), **options)
    factory = AutoModelForSequenceClassification if reranker else AutoModel
    model = factory.from_pretrained(str(path), **options, **weights)
    if torch.cuda.is_available():
        device = torch.device("cuda")
    elif torch.backends.mps.is_available():
        device = torch.device("mps")
    else:
        device = torch.device("cpu")
    model = model.half() if use_fp16 and device.type != "cpu" else model.float()
    model.to(device)
    model.eval()
    return tokenizer, model, device


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


class LocalDenseEmbedder:
    """BGE-M3's normalized first-token dense vectors, in original input order."""

    def __init__(self, model_name: str, *, use_fp16: bool = True, dimension: int = 1024):
        self.tokenizer, self.model, self.device = _load(
            model_name, reranker=False, use_fp16=use_fp16)
        self.dimension = dimension
        if getattr(self.model.config, "hidden_size", None) != dimension:
            raise ValueError("Unexpected dense embedding dimension")

    def encode(self, texts: list[str], *, batch_size: int = 8, max_length: int = 512) -> dict:
        import numpy as np
        import torch
        from torch.nn.functional import normalize

        _positive(batch_size, "batch_size")
        _positive(max_length, "max_length")
        if not texts:
            return {"dense_vecs": np.empty((0, self.dimension), dtype=np.float32)}
        vectors = []
        with torch.inference_mode():
            for start in range(0, len(texts), batch_size):
                batch = texts[start:start + batch_size]
                inputs = self.tokenizer(batch, padding=True, truncation=True,
                                        max_length=max_length, return_tensors="pt")
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                hidden = self.model(**inputs, return_dict=True).last_hidden_state
                if hidden.ndim != 3 or hidden.shape[0] != len(batch) or hidden.shape[2] != self.dimension:
                    raise ValueError("Unexpected dense embedding shape")
                dense = hidden[:, 0].float()
                if not torch.isfinite(dense).all() or (dense.norm(dim=-1) == 0).any():
                    raise ValueError("Invalid dense embedding")
                vectors.append(normalize(dense, p=2, dim=-1).cpu().numpy())
        return {"dense_vecs": np.concatenate(vectors).astype(np.float32, copy=False)}


class LocalReranker:
    """BGE cross-encoder scores with the existing 384/512 token boundaries."""

    def __init__(self, model_name: str, *, use_fp16: bool = True):
        self.tokenizer, self.model, self.device = _load(
            model_name, reranker=True, use_fp16=use_fp16)
        if getattr(self.model.config, "num_labels", None) != 1:
            raise ValueError("Reranker must produce one relevance logit")

    def compute_score(self, pairs: list[list[str]], *, normalize: bool = True,
                      batch_size: int = 8, max_length: int = 512) -> list[float]:
        import torch

        _positive(batch_size, "batch_size")
        _positive(max_length, "max_length")
        if not pairs:
            return []
        scores = []
        with torch.inference_mode():
            for start in range(0, len(pairs), batch_size):
                batch = pairs[start:start + batch_size]
                queries = self.tokenizer([pair[0] for pair in batch], add_special_tokens=False,
                                         truncation=True, max_length=max_length * 3 // 4)
                passages = self.tokenizer([pair[1] for pair in batch], add_special_tokens=False,
                                          truncation=True, max_length=max_length)
                # Transformers 5 tokenizers no longer expose prepare_for_model.
                # The Rust post-processor supplies the model's special tokens
                # and truncates ONLY the passage after our separate query cap.
                backend = self.tokenizer.backend_tokenizer
                previous = backend.truncation
                try:
                    backend.enable_truncation(max_length=max_length, strategy="only_second")
                    encodings = [backend.post_process(query, passage, add_special_tokens=True)
                                 for query, passage in zip(queries.encodings, passages.encodings)]
                finally:
                    if previous is None:
                        backend.no_truncation()
                    else:
                        backend.enable_truncation(**previous)
                prepared = [{"input_ids": encoding.ids, "attention_mask": encoding.attention_mask,
                             **({"token_type_ids": encoding.type_ids}
                                if "token_type_ids" in self.tokenizer.model_input_names else {})}
                            for encoding in encodings]
                inputs = self.tokenizer.pad(prepared, padding=True, return_tensors="pt")
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                logits = self.model(**inputs, return_dict=True).logits.float()
                if tuple(logits.shape) != (len(batch), 1) or not torch.isfinite(logits).all():
                    raise ValueError("Invalid reranker scores")
                values = logits[:, 0].sigmoid() if normalize else logits[:, 0]
                scores.extend(values.cpu().tolist())
        return scores
