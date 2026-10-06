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
  * The worker never outlives the process that owns the pool: it exits on
    its own within seconds of the owner's death, however the owner died
    (``_start_parent_watchdog``), and ``_shutdown_pool`` reaps it on an
    orderly shutdown (lifespan teardown, atexit).

The module top stays import-light: the spawn child imports this module, and
anything imported here runs in both processes.
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

# Strictly offline (standing rule, 4 October 2026): the same guard as config.py,
# repeated because this child never imports server app modules. A missing model
# fails fast; it is never downloaded.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

logger = logging.getLogger("orb.knowledge_worker")

# Generous ceilings: the FIRST call after boot pays the ~2 GB model load (the
# models are installed with the appliance, never downloaded). A slow worker blocks
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


_WATCHDOG_POLL_S = 1.0


def _start_parent_watchdog() -> None:
    """Exit the worker as soon as the process that owns the pool is gone.

    Why this exists: this child holds 2-4 GB of models, and the executor's
    shutdown sentinel never reaches a child that is still bootstrapping or
    mid-model-load. On 2026-10-04 thirty-five orphaned workers (PPID 1) held
    62 GB and exhausted swap. Every one of them had been orphaned while still
    importing the app: the dev server reloads on any file change, so its
    worker's parent often dies 1-5 s after spawning it.

    Two signals, neither of which trusts anything sampled in this process:

    * ``parent_process().sentinel``: the read end of the spawn pipe. Its
      write end lives ONLY in the owner, so it reads EOF the instant the
      owner exits, by any route including SIGKILL. (The call/result queue
      pipes are useless for this: the child inherits their write ends too,
      so a read on them never sees EOF.)
    * ``os.getppid() != parent_process().pid``: the kernel reparents an
      orphan. The comparison is against the pid the OWNER recorded at spawn
      time. The previous watchdog compared against ``os.getppid()`` sampled
      in the initializer, and a child orphaned during its own bootstrap had
      already been reparented, so it locked in ppid 1 as "normal" and could
      never fire. That is the whole orphan class.

    The check runs once synchronously (a child orphaned during bootstrap
    exits here, before it loads a model for nobody) and then on a daemon
    thread. The thread needs the GIL to act, so a worker inside one long
    native call exits when that call returns; measured on the real reranker
    load this is seconds, not minutes.
    """
    import multiprocessing as mp

    parent = mp.parent_process()
    if parent is None:
        return  # not a multiprocessing child (direct call in tests)
    parent_pid = parent.pid
    try:
        sentinel = parent.sentinel
    except ValueError:
        sentinel = None

    def _owner_gone(timeout: float) -> bool:
        nonlocal sentinel
        if sentinel is not None:
            try:
                from multiprocessing.connection import wait
                if wait([sentinel], timeout=timeout):
                    return True
            except Exception:
                sentinel = None  # fall back to ppid polling
                time.sleep(timeout)
        else:
            time.sleep(timeout)
        return parent_pid is not None and os.getppid() != parent_pid

    def _exit_now() -> None:
        try:
            os.write(2, (
                f"knowledge worker {os.getpid()}: pool owner {parent_pid} is gone, "
                "exiting\n"
            ).encode())
        except Exception:
            pass
        os._exit(0)

    if _owner_gone(0.0):
        _exit_now()

    def _watch() -> None:
        while not _owner_gone(_WATCHDOG_POLL_S):
            pass
        _exit_now()

    threading.Thread(target=_watch, daemon=True, name="parent-watchdog").start()


def _worker_init() -> None:
    """Child-process initializer: parent watchdog first, then thread caps.

    The watchdog goes before anything slow (the torch import, and the model
    loads the first job triggers) so a child whose owner is already gone
    exits before it costs anything.

    The thread caps matter: torch + OpenMP default to one thread per core,
    and on an appliance already running Ollama that oversubscribes the box.
    Four threads keep encode latency reasonable without a core-count blowup.
    """
    os.environ["ORB_KNOWLEDGE_WORKER"] = "1"
    _start_parent_watchdog()
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        import torch
        torch.set_num_threads(int(os.environ["OMP_NUM_THREADS"]))
    except Exception:
        pass


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
    # Models switched off (ORB_MODELS_ENABLED, or the Orb Ward Record): no
    # worker starts. KnowledgeWorkerError, so every caller takes its lexical
    # fallback as it does for any worker failure.
    from services.model_switch import models_enabled
    if not models_enabled():
        raise KnowledgeWorkerError("the knowledge workers are switched off on this computer")
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


_SHUTDOWN_GRACE_S = 3.0


def _terminate_workers(workers: list) -> None:
    """SIGTERM the worker(s), then SIGKILL any still alive after a short grace.

    SIGTERM's default disposition ends a worker wherever it is, even inside a
    native model load; the grace only covers a child that has not finished
    exec'ing yet. Liveness comes from each worker's sentinel (readable once
    it exits), never from waitpid: the executor's manager thread reaps these
    pids and a second waitpid would race it.
    """
    from multiprocessing.connection import wait

    for proc in workers:
        try:
            proc.terminate()
        except Exception:
            pass
    deadline = time.monotonic() + _SHUTDOWN_GRACE_S
    for proc in workers:
        try:
            if not wait([proc.sentinel], timeout=max(0.0, deadline - time.monotonic())):
                proc.kill()
        except Exception:
            pass


def _shutdown_pool() -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is None:
        return
    # Snapshot the workers BEFORE shutdown(): ProcessPoolExecutor.shutdown()
    # sets _processes to None, so the terminate loop that used to run after
    # it raised AttributeError into a bare except and never terminated
    # anything. Every dev reload's "pool shut down" line was logged over a
    # worker that lived on. _processes is private API, but the alternative
    # is leaking gigabytes per reload.
    workers = list((getattr(pool, "_processes", None) or {}).values())
    # cancel_futures: a wedged/backed-up queue must not stall shutdown;
    # pending retrievals are best-effort prefetches.
    pool.shutdown(wait=False, cancel_futures=True)
    _terminate_workers(workers)
    logger.info("knowledge worker pool shut down (%d worker(s) reaped)", len(workers))


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
