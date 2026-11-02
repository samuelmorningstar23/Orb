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
import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Literal

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
_w_embedder_name: str | None = None
_w_reranker_name: str | None = None
_w_reranker: Callable[[list[tuple[str, str]]], list[float]] | Literal[False] | None = (
    None  # callable(pairs) -> list[float], or the sentinel False when unavailable
)


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
            os.write(
                2,
                (
                    f"knowledge worker {os.getpid()}: pool owner {parent_pid} is gone, exiting\n"
                ).encode(),
            )
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


def _worker_get_embedder(model_name: str | None = None):
    global _w_embedder, _w_embedder_name
    model_name = model_name or os.getenv("ORB_EMBEDDING_MODEL", "BAAI/bge-m3")
    if _w_embedder_name is not None and _w_embedder_name != model_name:
        raise RuntimeError("embedding model changed; restart the knowledge worker")
    if _w_embedder is None:
        from services.local_transformers import LocalDenseEmbedder

        _w_embedder = LocalDenseEmbedder(model_name, use_fp16=True)
        _w_embedder_name = model_name
    return _w_embedder


def _worker_get_reranker(model_name: str | None = None):
    global _w_reranker, _w_reranker_name
    model_name = model_name or os.getenv("SAGE_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    if _w_reranker_name is not None and _w_reranker_name != model_name:
        raise RuntimeError("reranker model changed; restart the knowledge worker")
    if _w_reranker is None:
        _w_reranker_name = model_name
        try:
            from services.local_transformers import LocalReranker

            rr = LocalReranker(model_name, use_fp16=True)

            def _score(pairs):
                out = rr.compute_score(pairs, normalize=True)
                return out if isinstance(out, list) else [out]

            _w_reranker = _score
        except Exception:
            _w_reranker = False  # definitively unavailable in this env
    return _w_reranker or None


_MPS_UNUSED_CACHE_RELEASE_TRIGGER_BYTES = 1024 ** 3


def _release_unused_mps_cache() -> None:
    """Return unused Torch allocator blocks to the hospital-local GPU peers.

    Dense vectors and reranker scores have already been copied to the CPU by
    the adapters before their successful calls return. MPS empty_cache releases
    unoccupied allocator buffers only; it does not evict model tensors, change
    precision or recompute outputs. Driver-minus-live also includes MPSGraph
    allocations, so this is a conservative release trigger, not a hard cache
    cap or a promise that the whole difference can be freed. It cannot reduce
    the peak live workspace of an in-progress model call.
    """
    try:
        import torch

        if not torch.backends.mps.is_available():
            return
        driver_before = int(torch.mps.driver_allocated_memory())
        live_before = int(torch.mps.current_allocated_memory())
        estimated_unused = max(0, driver_before - live_before)
        if estimated_unused <= _MPS_UNUSED_CACHE_RELEASE_TRIGGER_BYTES:
            return
        torch.mps.empty_cache()
        driver_after = int(torch.mps.driver_allocated_memory())
        live_after = int(torch.mps.current_allocated_memory())
        # Content-free and visible in the spawned worker's stderr, including
        # when no application logging handlers have been configured there.
        logger.warning(
            "MPS unused-cache release driver_before_bytes=%d live_before_bytes=%d "
            "driver_after_bytes=%d live_after_bytes=%d released_bytes=%d trigger_bytes=%d",
            driver_before, live_before, driver_after, live_after,
            max(0, driver_before - driver_after), _MPS_UNUSED_CACHE_RELEASE_TRIGGER_BYTES,
        )
    except Exception as error:
        # Allocator diagnostics/cleanup must never replace a clinical model's
        # original error or discard a valid CPU result. Log the type, not an
        # exception message that could contain input or environment details.
        try:
            logger.warning("MPS unused-cache cleanup unavailable error_type=%s", type(error).__name__)
        except Exception:
            pass


def _worker_embed(texts: list, model_name: str, batch_size: int, max_length: int) -> list:
    """Embed `texts`; returns a list of float32 byte blobs (sqlite-vec format)."""
    import numpy as np

    try:
        os.environ["ORB_EMBEDDING_MODEL"] = model_name
        model = _worker_get_embedder(model_name)
        vecs = model.encode(texts, batch_size=batch_size, max_length=max_length)["dense_vecs"]
        return [np.asarray(v, dtype=np.float32).tobytes() for v in vecs]
    finally:
        _release_unused_mps_cache()


def _worker_rerank(query: str, passages: list, model_name: str | None = None) -> list:
    """Cross-encoder relevance scores for (query, passage) pairs.

    Raises RuntimeError when no cross-encoder is installed in this env, so the
    parent's lexical fallback takes over (same degradation as before).
    """
    try:
        scorer = _worker_get_reranker(model_name)
        if scorer is None:
            raise RuntimeError("cross-encoder unavailable in knowledge worker")
        pairs = [[query, p] for p in passages]
        return [float(s) for s in scorer(pairs)]
    finally:
        _release_unused_mps_cache()


# ─────────────────────────────────────────────────────────────────
# Parent-side pool management + sync bridge
# ─────────────────────────────────────────────────────────────────

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_pool_failures = 0
_MAX_POOL_RESTARTS = 2

# Reuse mathematical model outputs only. Search results, approvals, patient
# records and permissions are never cached here: their normal DB/ACL checks run
# on every retrieval. Rerank keys include the exact ordered passage content, so
# a corpus edit or a different permitted candidate set cannot reuse old scores.
# The loaded child pins each model identifier for its lifetime; replacing local
# weights requires the same worker restart that clears this output cache.
_REUSE_TTL_S = 30.0
_REUSE_MAX_ENTRIES = 128
_REUSE_MAX_BYTES = 8 * 1024 * 1024
_REUSE_MAX_PENDING = 256
_reuse_lock = threading.RLock()
_reuse_results: OrderedDict[tuple, tuple[float, tuple, int]] = OrderedDict()
_reuse_pending: dict[tuple, Future] = {}
_reuse_bytes = 0
_reuse_counts = {"submitted": 0, "coalesced": 0, "hits": 0, "evicted": 0}
_batch_lock = threading.RLock()
_embedding_batches: dict[tuple, tuple[threading.Timer, list[tuple[Future, str]]]] = {}
_batch_counts = {"embedding_batch_calls": 0, "embedding_batched_requests": 0,
                 "embedding_batch_max": 0}
_EMBED_BATCH_WINDOW_S = 0.003


def _output_key(kind: str, model_name: str, texts: list, *configuration: int) -> tuple:
    """Unambiguous exact-input digest; retain no query/passage text in cache keys."""
    digest = hashlib.sha256()
    for text in texts:
        if not isinstance(text, str):
            raise KnowledgeWorkerError("knowledge model inputs must be strings")
        encoded = text.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return kind, model_name, configuration, len(texts), digest.digest()


def _clear_output_reuse() -> None:
    global _reuse_bytes
    with _reuse_lock:
        _reuse_results.clear()
        _reuse_pending.clear()
        _reuse_bytes = 0


def output_reuse_metrics() -> dict[str, int | float]:
    """Content-free counters, never model inputs, cache keys or result values."""
    with _reuse_lock, _batch_lock:
        return {
            **_reuse_counts,
            **_batch_counts,
            "embedding_batch_pending": sum(len(rows) for _, rows in _embedding_batches.values()),
            "pending": len(_reuse_pending),
            "entries": len(_reuse_results),
            "bytes": _reuse_bytes,
            "ttl_s": _REUSE_TTL_S,
            "max_entries": _REUSE_MAX_ENTRIES,
            "max_bytes": _REUSE_MAX_BYTES,
            "max_pending": _REUSE_MAX_PENDING,
        }


def _clear_embedding_batches() -> None:
    with _batch_lock:
        batches = list(_embedding_batches.values())
        _embedding_batches.clear()
    for timer, rows in batches:
        timer.cancel()
        for result, _ in rows:
            result.set_exception(KnowledgeWorkerError("knowledge worker shut down before embedding"))


def _flush_embedding_batch(key: tuple, expected_timer: threading.Timer) -> None:
    with _batch_lock:
        group = _embedding_batches.get(key)
        if group is None or group[0] is not expected_timer:
            return
        del _embedding_batches[key]
    _dispatch_embedding_batch(key, group)


def _dispatch_embedding_batch(
    key: tuple, group: tuple[threading.Timer, list[tuple[Future, str]]]
) -> None:
    timer, rows = group
    timer.cancel()
    with _batch_lock:
        _batch_counts["embedding_batch_calls"] += 1
        _batch_counts["embedding_batched_requests"] += len(rows)
        _batch_counts["embedding_batch_max"] = max(_batch_counts["embedding_batch_max"], len(rows))
    pool, model_name, batch_size, max_length = key

    def finish(batch: Future) -> None:
        try:
            values = batch.result()
            if len(values) != len(rows) or not all(type(value) is bytes for value in values):
                raise KnowledgeWorkerError("embedding microbatch returned incomplete vectors")
        except BaseException as error:
            for result, _ in rows:
                result.set_exception(error)
        else:
            for (result, _), value in zip(rows, values, strict=True):
                result.set_result([value])

    try:
        physical = pool.submit(_worker_embed, [text for _, text in rows],
                               model_name, batch_size, max_length)
    except BaseException as error:
        for result, _ in rows:
            result.set_exception(error)
    else:
        physical.add_done_callback(finish)


def _submit_worker_output(pool: ProcessPoolExecutor, fn, args: tuple) -> Future:
    """Optional qualified batch4 for independent single-query embeddings only.

    This is off by default: MPS batch1/batch4 token, vector and retrieval-rank
    equivalence must first be measured on the installed model. No text is
    shortened, normalized or dropped; the existing batch_size/max_length and
    model are retained. Multi-text corpus encoding and all reranking keep the
    original path, model batches and candidate counts.
    """
    setting = os.getenv("ORB_KNOWLEDGE_QUERY_MICROBATCH", "1")
    if setting not in {"1", "4"}:
        raise KnowledgeWorkerError("ORB_KNOWLEDGE_QUERY_MICROBATCH must be 1 or qualified 4")
    if fn is not _worker_embed or setting == "1" or len(args[0]) != 1:
        return pool.submit(fn, *args)
    texts, model_name, batch_size, max_length = args
    key = (pool, model_name, batch_size, max_length)
    result: Future = Future()
    ready = None
    with _batch_lock:
        if key not in _embedding_batches:
            timer = threading.Timer(_EMBED_BATCH_WINDOW_S,
                                    lambda: _flush_embedding_batch(key, timer))
            timer.daemon = True
            _embedding_batches[key] = timer, []
            timer.start()
        _, rows = _embedding_batches[key]
        rows.append((result, texts[0]))
        if len(rows) == 4:
            # Detach while still locked. A fifth arriving caller must start a
            # new group instead of racing the physical submit into width5+.
            ready = _embedding_batches.pop(key)
    if ready is not None:
        _dispatch_embedding_batch(key, ready)
    return result


def _finish_reused_output(key: tuple, future: Future) -> None:
    """Publish only successful immutable outputs; late old-pool results are ignored."""
    global _reuse_bytes
    with _reuse_lock:
        if _reuse_pending.get(key) is not future:
            return
        del _reuse_pending[key]
        if future.cancelled() or future.exception() is not None:
            return
        result = future.result()
        if not isinstance(result, list) or not all(type(v) in (bytes, float) for v in result):
            return
        frozen = tuple(result)
        size = sum(len(v) if isinstance(v, bytes) else 8 for v in frozen)
        # Include a minimum entry cost so even empty outputs cannot grow memory
        # independently of the byte and entry bounds.
        size += 128
        if size > _REUSE_MAX_BYTES:
            return
        while _reuse_results and (
            len(_reuse_results) >= _REUSE_MAX_ENTRIES or _reuse_bytes + size > _REUSE_MAX_BYTES
        ):
            _, (_, _, removed) = _reuse_results.popitem(last=False)
            _reuse_bytes -= removed
            _reuse_counts["evicted"] += 1
        _reuse_results[key] = (time.monotonic() + _REUSE_TTL_S, frozen, size)
        _reuse_bytes += size


def _shared_output_future(pool: ProcessPoolExecutor, key: tuple, fn, args: tuple) -> Future:
    """Submit once per exact request; timeout of one waiter cannot cancel others."""
    global _reuse_bytes
    # Pool object identity is scoped by explicit clearing on shutdown. Holding
    # the pool itself in the key prevents Python id reuse from crossing pools.
    key = (pool, *key)
    with _reuse_lock:
        now = time.monotonic()
        for expired in [key for key, entry in _reuse_results.items() if entry[0] <= now]:
            _, _, size = _reuse_results.pop(expired)
            _reuse_bytes -= size
        if key in _reuse_results:
            _, frozen, _ = _reuse_results[key]
            _reuse_results.move_to_end(key)
            _reuse_counts["hits"] += 1
            completed: Future = Future()
            completed.set_result(list(frozen))
            return completed
        if key in _reuse_pending:
            _reuse_counts["coalesced"] += 1
            return _reuse_pending[key]
        if len(_reuse_pending) >= _REUSE_MAX_PENDING:
            raise KnowledgeWorkerError("knowledge worker pending request bound reached")
        future = _submit_worker_output(pool, fn, args)
        _reuse_pending[key] = future
        _reuse_counts["submitted"] += 1
        future.add_done_callback(lambda done: _finish_reused_output(key, done))
        return future


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
            _pool = ProcessPoolExecutor(max_workers=1, mp_context=ctx, initializer=_worker_init)
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


def _shutdown_pool(expected_pool: ProcessPoolExecutor | None = None) -> bool:
    global _pool
    with _pool_lock:
        if expected_pool is not None and _pool is not expected_pool:
            return False
        pool, _pool = _pool, None
        _clear_output_reuse()
    _clear_embedding_batches()
    if pool is None:
        return False
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
    return True


atexit.register(_shutdown_pool)


def _submit(fn, *args, timeout: float, reuse_key: tuple | None = None):
    """Run `fn(*args)` in the worker, restarting a broken pool a bounded number
    of times. Blocks the calling THREAD only; raises KnowledgeWorkerError on
    any failure so callers take their degraded path."""
    global _pool_failures
    if _pool_failures > _MAX_POOL_RESTARTS:
        raise KnowledgeWorkerError("knowledge worker disabled after repeated failures")
    pool = None
    try:
        # Check model-switch authorization before looking at a completed cache
        # entry. Cached inference must never bypass the appliance's off switch.
        pool = _ensure_pool()
        fut = (
            pool.submit(fn, *args)
            if reuse_key is None
            else _shared_output_future(pool, reuse_key, fn, args)
        )
        result = fut.result(timeout=timeout)
        # Concurrent waiters must not share a mutable result list.
        return list(result) if reuse_key is not None else result
    except BrokenProcessPool as e:
        # A shared broken future may wake many callers. Retire/count its pool
        # once; a late waiter must not tear down a subsequently created pool.
        if pool is not None and _shutdown_pool(expected_pool=pool):
            _pool_failures += 1
        logger.error(
            "knowledge worker died (%s) — restart budget %d/%d used",
            e,
            _pool_failures,
            _MAX_POOL_RESTARTS + 1,
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
        _worker_embed,
        list(texts),
        model_name,
        batch_size,
        max_length,
        timeout=EMBED_TIMEOUT_S,
        reuse_key=_output_key("embed", model_name, texts, batch_size, max_length),
    )


def rerank_scores_isolated(query: str, passages: list) -> list:
    """Cross-encoder scores from the isolated worker."""
    model_name = os.getenv("SAGE_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    return _submit(
        _worker_rerank,
        query,
        list(passages),
        model_name,
        timeout=RERANK_TIMEOUT_S,
        reuse_key=_output_key("rerank", model_name, [query, *passages]),
    )


def shutdown() -> None:
    """Explicit shutdown for app lifespan teardown (atexit also covers crashes)."""
    _shutdown_pool()
