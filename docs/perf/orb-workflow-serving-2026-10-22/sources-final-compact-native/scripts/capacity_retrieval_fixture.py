"""Installed full-retrieval proof and content-free counters for owned fixtures."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any


def snapshot_proof(path: str, *, reranker: bool = False) -> dict[str, Any]:
    """Validate the production BGE architecture without loading or downloading."""
    source = Path(path)
    if not source.is_absolute() or not source.is_dir() or not (source / "config.json").is_file():
        raise ValueError("Provide an installed absolute model snapshot with config.json")
    raw = (source / "config.json").read_bytes()
    config = json.loads(raw)
    architecture = "XLMRobertaForSequenceClassification" if reranker else "XLMRobertaModel"
    architectures = config.get("architectures") if isinstance(config, dict) else None
    if (not isinstance(config, dict) or config.get("hidden_size") != 1024
            or not isinstance(architectures, list) or architecture not in architectures):
        raise ValueError("Full retrieval requires the production 1024-dimensional BGE-M3 / BGE reranker architectures")
    return {"expected_model": "BAAI/bge-reranker-v2-m3" if reranker else "BAAI/bge-m3",
            "architecture": architecture, "hidden_size": 1024,
            "config_sha256": hashlib.sha256(raw).hexdigest(),
            "snapshot_revision": source.name if re.fullmatch(r"[0-9a-f]{40}", source.name) else "local-installed-directory"}


class RetrievalCounters:
    """Count actual worker successes/failures and reranking degradation only."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counts = {name: 0 for name in (
            "embedding_calls", "embedding_completed", "embedding_failed", "rerank_calls", "rerank_completed",
            "rerank_failed", "results_cross_encoder", "results_lexical", "results_other",
        )}
        self.elapsed: dict[str, float] = {name: 0.0 for name in ("embedding_elapsed_s", "embedding_max_s",
                                                                "rerank_elapsed_s", "rerank_max_s")}

    def add(self, name: str, count: int = 1) -> None:
        with self.lock:
            self.counts[name] += count

    def duration(self, prefix: str, elapsed: float) -> None:
        with self.lock:
            self.elapsed[prefix + "_elapsed_s"] += elapsed
            self.elapsed[prefix + "_max_s"] = max(self.elapsed[prefix + "_max_s"], elapsed)

    def report(self) -> dict[str, int | float]:
        with self.lock:
            return {**self.counts, **self.elapsed}

    def install(self) -> None:
        import services.clinical_knowledge as knowledge
        original_embed = knowledge.embed_texts_isolated
        original_rerank = knowledge.rerank_scores_isolated
        original_results = knowledge.rerank_results
        def embed(*args: Any, **kwargs: Any) -> Any:
            self.add("embedding_calls")
            started = time.perf_counter()
            try:
                result = original_embed(*args, **kwargs)
            except BaseException:
                self.add("embedding_failed")
                raise
            finally:
                self.duration("embedding", time.perf_counter() - started)
            self.add("embedding_completed")
            return result
        def rerank(*args: Any, **kwargs: Any) -> Any:
            self.add("rerank_calls")
            started = time.perf_counter()
            try:
                result = original_rerank(*args, **kwargs)
            except BaseException:
                self.add("rerank_failed")
                raise
            finally:
                self.duration("rerank", time.perf_counter() - started)
            self.add("rerank_completed")
            return result
        def results(*args: Any, **kwargs: Any) -> Any:
            output = original_results(*args, **kwargs)
            for row in output:
                method = row.get("rerank_method") if isinstance(row, dict) else None
                self.add("results_" + (method if method in {"cross_encoder", "lexical"} else "other"))
            return output
        knowledge.embed_texts_isolated = embed
        knowledge.rerank_scores_isolated = rerank
        knowledge.rerank_results = results


def retrieval_integrity(proof: dict[str, Any] | None, counters: dict[str, Any] | None) -> dict[str, Any]:
    """A fast degraded lexical path must never qualify as full model retrieval."""
    if proof is None:
        return {"required": False, "passed": True, "scope": "chart/auth only; full retrieval not exercised"}
    counts = counters or {}
    checks = {"startup_models_and_corpus_verified": bool(proof.get("nonzero_embedding_probe") and proof.get("cross_encoder_probe")),
              "embedding_exercised": counts.get("embedding_completed", 0) > 0,
              "cross_encoder_exercised": counts.get("rerank_completed", 0) > 0,
              "no_embedding_failure": counts.get("embedding_failed", 0) == 0,
              "no_rerank_failure": counts.get("rerank_failed", 0) == 0,
              "no_lexical_downgrade": counts.get("results_lexical", 0) == 0 and counts.get("results_other", 0) == 0}
    return {"required": True, "passed": all(checks.values()), "checks": checks}


def prepare_full_retrieval(embedding_model: str, reranker_model: str) -> tuple[dict[str, Any], RetrievalCounters]:
    """Fail closed on empty/zero-vector corpora or an unavailable real reranker."""
    here = Path.cwd().resolve()
    if (here.name != "server" or not here.parent.name.startswith("orb_capacity_")
            or Path(os.environ.get("ORB_DB_PATH", "")).resolve() != here.parent / "demo.db"):
        raise RuntimeError("Full retrieval preparation requires an owned disposable capacity database")
    proof: dict[str, Any] = {"embedding": snapshot_proof(embedding_model), "reranker": snapshot_proof(reranker_model, reranker=True)}
    import numpy as np
    import sqlite_vec
    from database import db_session
    from services.clinical_knowledge import init_knowledge_base
    from services.knowledge_worker import embed_texts_isolated, rerank_scores_isolated
    def stage(name: str) -> None:
        (here.parent / "capacity-startup-stage.json").write_text(json.dumps({"stage": name}))
    stage("corpus_initialization")
    init_knowledge_base()
    with db_session() as db:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        sources = db.execute("SELECT COUNT(*) FROM clinical_knowledge").fetchone()[0]
        vectors = db.execute("SELECT COUNT(*) FROM clinical_knowledge_vec").fetchone()[0]
        fingerprint = db.execute("SELECT value FROM clinical_knowledge_meta WHERE key='corpus_fingerprint'").fetchone()
        stored_vectors = db.execute("SELECT embedding FROM clinical_knowledge_vec").fetchall()
    if sources <= 0 or vectors != sources or fingerprint is None:
        raise RuntimeError("Real clinical corpus/vector initialization failed")
    for row in stored_vectors:
        stored = np.frombuffer(row[0], dtype=np.float32)
        if stored.size != 1024 or not np.isfinite(stored).all() or not np.any(stored != 0):
            raise RuntimeError("Clinical corpus contains an invalid or zero embedding")
    stage("embedding_probe")
    embeddings = embed_texts_isolated(["Synthetic capacity qualification: documented sepsis observations."], embedding_model)
    vector = np.frombuffer(embeddings[0], dtype=np.float32) if len(embeddings) == 1 else np.array([])
    if vector.size != 1024 or not np.isfinite(vector).all() or not np.any(vector != 0):
        raise RuntimeError("Installed embedder failed the nonzero-vector qualification probe")
    stage("reranker_probe")
    scores = rerank_scores_isolated("sepsis escalation", ["Escalate suspected sepsis for urgent clinical review.",
                                                          "Unrelated synthetic administrative information."])
    if len(scores) != 2 or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in scores):
        raise RuntimeError("Installed cross-encoder failed the qualification probe")
    proof.update({"corpus_sources": sources, "corpus_vectors": vectors, "corpus_fingerprint": fingerprint[0],
                  "nonzero_corpus_vectors": len(stored_vectors),
                  "nonzero_embedding_probe": True, "cross_encoder_probe": True,
                  "qualification_scope": "Installed full retrieval over the disposable seeded corpus; no clinical accuracy claim"})
    counters = RetrievalCounters()
    counters.install()
    stage("full_retrieval_ready")
    return proof, counters
