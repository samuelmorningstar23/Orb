"""Process-isolated embedding + reranking for the clinical knowledge base.

WHY THIS EXISTS
---------------
The BGE-M3 embedder and the bge-reranker cross-encoder are ~2 GB each and run
fp16-on-CPU. Loading them takes tens of seconds and every encode/score call
churns HuggingFace's Rust tokenizer plus torch — work that holds the GIL for
most of its wall time. Running them inside the API process (even under
``asyncio.to_thread``) starved the uvloop event loop of the GIL: with the
vitals simulator generating critical alerts (each one triggers a guideline
prefetch), ``GET /api/health`` timed out for MINUTES and SIGTERM was ignored
(a starved loop cannot run its signal handlers). Diagnosed 2026-07-30 with
macOS ``sample``: main thread stalled in ``take_gil``, hot frames in
``tokenizers.abi3.so`` / libomp.

THE FIX
-------
Host both models in a single-worker child process (its own interpreter, its
own GIL), the same isolation philosophy as ``whisper_daemon``. Callers block
only their worker THREAD on a future (the GIL is released while waiting), so
the event loop never competes with model work again.

Contract:
  * ``embed_texts_isolated(texts)``   -> list[bytes]  (float32 vectors)
  * ``rerank_scores_isolated(q, ps)`` -> list[float]
  * Both raise ``KnowledgeWorkerError`` on failure — callers degrade to their
    documented fallbacks (lexical-only retrieval / lexical reranker) and the
    PARENT process never loads the heavy models.
  * ``ORB_KNOWLEDGE_INPROCESS=1`` disables isolation (tests, constrained envs);
    clinical_knowledge.py then uses its legacy in-process path.

The module top stays import-light: the spawn child imports this module, and
anything imported here runs in both processes.
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

logger = logging.getLogger("orb.knowledge_worker")

# Generous ceilings: the FIRST call after boot pays the ~2 GB model load (and,
# on a pristine appliance, possibly a model download). A slow worker blocks
# only the calling thread — never the event loop — so patience is cheap.
EMBED_TIMEOUT_S = float(os.getenv("ORB_KNOWLEDGE_EMBED_TIMEOUT_S", "420"))
RERANK_TIMEOUT_S = float(os.getenv("ORB_KNOWLEDGE_RERANK_TIMEOUT_S", "420"))


class KnowledgeWorkerError(RuntimeError):
    """The isolated worker could not produce a result. Callers must degrade."""


def isolation_enabled() -> bool:
    return os.getenv("ORB_KNOWLEDGE_INPROCESS", "").lower() not in ("1", "true", "yes")


# ─────────────────────────────────────────────────────────────────
# Worker-side (runs in the CHILD process only)
# ─────────────────────────────────────────────────────────────────

_w_embedder = None
_w_reranker = None  # callable(pairs) -> list[float], or the sentinel False when unavailable


def _worker_init() -> None:
    """Child-process initializer: mark the process and bound native threading.

    The thread caps matter: torch + OpenMP default to one thread per core,
    and on an appliance already running Ollama that oversubscribes the box.
    Four threads keep encode latency reasonable without a core-count blowup.
    """
    os.environ["ORB_KNOWLEDGE_WORKER"] = "1"
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        import torch
        torch.set_num_threads(int(os.environ["OMP_NUM_THREADS"]))
    except Exception:
        pass

    # Parent-death watchdog: this child holds 2-4 GB of models, and executor
    # shutdown sentinels are unreliable when the parent is dying (SIGTERM on
    # dev reload, SIGKILL on a hard recycle). An orphaned model process
    # silently holding gigabytes is exactly the "orphaned worker" ops hazard
    # this repo has been bitten by before — if our parent goes away
    # (ppid becomes 1/launchd), exit immediately.
    def _watch_parent(original_ppid: int = os.getppid()) -> None:
        import time as _time
        while True:
            if os.getppid() != original_ppid:
                os._exit(0)
            _time.sleep(2.0)

    threading.Thread(target=_watch_parent, daemon=True, name="parent-watchdog").start()


def _local_snapshot_or_name(model_name: str) -> str:
    """Resolve to the fully-cached local HF snapshot path when available.

    Same rationale as clinical_knowledge._local_snapshot_or_name (duplicated so
    the child never imports server app modules): FlagEmbedding revalidates hub
    etags on every constructor call, which hangs on blocked networks; a local
    path skips the hub entirely.
    """
    try:
        from huggingface_hub import snapshot_download
        return snapshot_download(model_name, local_files_only=True)
    except Exception:
        return model_name


def _worker_get_embedder():
    global _w_embedder
    if _w_embedder is None:
        from FlagEmbedding import BGEM3FlagModel
        model_name = os.getenv("ORB_EMBEDDING_MODEL", "BAAI/bge-m3")
        resolved = _local_snapshot_or_name(model_name)
        try:
            _w_embedder = BGEM3FlagModel(resolved, use_fp16=True)
        except Exception:
            if resolved == model_name:
                raise
            _w_embedder = BGEM3FlagModel(model_name, use_fp16=True)
    return _w_embedder


def _worker_get_reranker():
    global _w_reranker
    if _w_reranker is None:
        model_name = os.getenv("SAGE_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
        try:
            from FlagEmbedding import FlagReranker
            resolved = _local_snapshot_or_name(model_name)
            try:
                rr = FlagReranker(resolved, use_fp16=True)
            except Exception:
                if resolved == model_name:
                    raise
                rr = FlagReranker(model_name, use_fp16=True)

            def _score(pairs):
                out = rr.compute_score(pairs, normalize=True)
                return out if isinstance(out, list) else [out]

            _w_reranker = _score
        except Exception:
            try:
                from sentence_transformers import CrossEncoder
                ce = CrossEncoder(_local_snapshot_or_name(model_name))

                def _score(pairs):
                    return [float(s) for s in ce.predict(pairs)]

                _w_reranker = _score
            except Exception:
                _w_reranker = False  # definitively unavailable in this env
    return _w_reranker or None


def _worker_embed(texts: list, model_name: str, batch_size: int, max_length: int) -> list:
    """Embed `texts`; returns a list of float32 byte blobs (sqlite-vec format)."""
    import numpy as np
    os.environ["ORB_EMBEDDING_MODEL"] = model_name
    model = _worker_get_embedder()
    vecs = model.encode(texts, batch_size=batch_size, max_length=max_length)["dense_vecs"]
    return [np.asarray(v, dtype=np.float32).tobytes() for v in vecs]


def _worker_rerank(query: str, passages: list) -> list:
    """Cross-encoder relevance scores for (query, passage) pairs.

    Raises RuntimeError when no cross-encoder is installed in this env, so the
    parent's lexical fallback takes over (same degradation as before).
    """
    scorer = _worker_get_reranker()
    if scorer is None:
        raise RuntimeError("cross-encoder unavailable in knowledge worker")
    pairs = [[query, p] for p in passages]
    return [float(s) for s in scorer(pairs)]


# ─────────────────────────────────────────────────────────────────
# Parent-side pool management + sync bridge
# ─────────────────────────────────────────────────────────────────

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_pool_failures = 0
_MAX_POOL_RESTARTS = 2


def _ensure_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            import multiprocessing as mp
            # spawn, never fork: the API process runs an event loop + threads,
            # and forking that is undefined behaviour city.
            ctx = mp.get_context("spawn")
            _pool = ProcessPoolExecutor(
                max_workers=1, mp_context=ctx, initializer=_worker_init
            )
            logger.info("knowledge worker pool started (1 spawn worker)")
    return _pool


def _shutdown_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            # cancel_futures: a wedged/backed-up queue must not stall shutdown —
            # pending retrievals are best-effort prefetches.
            _pool.shutdown(wait=False, cancel_futures=True)
            # Belt-and-braces with the child's parent-watchdog: shutdown
            # sentinels don't reach a child that is mid-model-load, and a
            # 2-4 GB orphan must never outlive us. _processes is private API
            # but the alternative is leaking gigabytes on every dev reload.
            try:
                for proc in list(getattr(_pool, "_processes", {}).values()):
                    proc.terminate()
            except Exception:
                pass
            _pool = None
            logger.info("knowledge worker pool shut down")


atexit.register(_shutdown_pool)


def _submit(fn, *args, timeout: float):
    """Run `fn(*args)` in the worker, restarting a broken pool a bounded number
    of times. Blocks the calling THREAD only; raises KnowledgeWorkerError on
    any failure so callers take their degraded path."""
    global _pool_failures
    if _pool_failures > _MAX_POOL_RESTARTS:
        raise KnowledgeWorkerError("knowledge worker disabled after repeated failures")
    try:
        fut = _ensure_pool().submit(fn, *args)
        return fut.result(timeout=timeout)
    except BrokenProcessPool as e:
        _pool_failures += 1
        _shutdown_pool()
        logger.error(
            "knowledge worker died (%s) — restart budget %d/%d used",
            e, _pool_failures, _MAX_POOL_RESTARTS + 1,
        )
        raise KnowledgeWorkerError(f"knowledge worker crashed: {e}") from e
    except KnowledgeWorkerError:
        raise
    except Exception as e:
        raise KnowledgeWorkerError(str(e)) from e


def embed_texts_isolated(
    texts: list, model_name: str, batch_size: int = 8, max_length: int = 512
) -> list:
    """Embed in the isolated worker; list of float32 byte blobs."""
    return _submit(
        _worker_embed, list(texts), model_name, batch_size, max_length,
        timeout=EMBED_TIMEOUT_S,
    )


def rerank_scores_isolated(query: str, passages: list) -> list:
    """Cross-encoder scores from the isolated worker."""
    return _submit(_worker_rerank, query, list(passages), timeout=RERANK_TIMEOUT_S)


def shutdown() -> None:
    """Explicit shutdown for app lifespan teardown (atexit also covers crashes)."""
    _shutdown_pool()
