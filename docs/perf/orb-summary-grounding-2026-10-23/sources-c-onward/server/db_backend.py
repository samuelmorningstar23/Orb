"""Orb — database backend abstraction.

A thin seam that decouples the app from "a single SQLite file". It selects the
backend from ``ORB_DB_BACKEND`` (default ``sqlite``), centralizes the parameter
placeholder style, and provides an opt-in connection pool. Today only the SQLite
backend is wired; a Postgres backend is scaffolded and fails fast with an
actionable message so the swap point is explicit rather than hidden across a
hundred call sites. See docs/DATA_LAYER.md for the Postgres path.

The SQLite connection *configuration* (SQLCipher support, WAL/PRAGMA tuning,
row_factory) still lives in database.py; this module owns backend selection,
the placeholder, and pooling so those concerns have one home.
"""

from __future__ import annotations

import logging
import os
import sqlite3 as _sqlite3
import threading

logger = logging.getLogger("orb.db_backend")

# Selected at import. Unsupported values fail fast (misconfiguration should not
# silently fall back to the wrong store for a PHI database).
BACKEND_NAME = os.getenv("ORB_DB_BACKEND", "sqlite").strip().lower()

_SUPPORTED = {"sqlite"}
_KNOWN_UNWIRED = {"postgres", "postgresql", "pg"}

if BACKEND_NAME in _KNOWN_UNWIRED:
    raise RuntimeError(
        f"ORB_DB_BACKEND={BACKEND_NAME} selected, but the Postgres backend is not yet wired. "
        "Orb's SQL currently uses SQLite-specific syntax (INSERT OR IGNORE, "
        "AUTOINCREMENT, PRAGMA, FTS5, sqlite-vec). See docs/DATA_LAYER.md for the "
        "migration path. Use ORB_DB_BACKEND=sqlite until then."
    )
if BACKEND_NAME not in _SUPPORTED:
    raise RuntimeError(
        f"ORB_DB_BACKEND={BACKEND_NAME!r} is not a recognized backend "
        f"(supported: {sorted(_SUPPORTED)})."
    )

# Parameter placeholder for the active backend, so query builders can stay
# backend-agnostic (sqlite: '?', postgres: '%s').
PLACEHOLDER = "?" if BACKEND_NAME == "sqlite" else "%s"

# ── Driver-agnostic DB-API exception aliases ─────────────────────────────────
#
# WHY THESE EXIST — this was a live, silent, product-wide bug.
#
# database.py::_open_raw_connection swaps the driver to sqlcipher3 (or
# pysqlcipher3) whenever ORB_DB_ENCRYPTION_KEY is set, which is the shipping
# appliance configuration. sqlcipher3 defines its OWN DB-API exception
# hierarchy rooted at builtins.Exception — it does NOT subclass the stdlib's:
#
#     issubclass(sqlcipher3.dbapi2.IntegrityError, sqlite3.IntegrityError) -> False
#
# So on every encrypted deployment, each `except sqlite3.IntegrityError` in this
# codebase was DEAD CODE. The handlers looked correct, passed review, and passed
# the unencrypted test suite — and in production a duplicate insert escaped as an
# unhandled traceback instead of the intended message. Reproduced 2026-07-31:
# scripts/create_admin.py died with a raw sqlcipher3.dbapi2.IntegrityError where
# it should have printed "A user with id 'X' already exists." and exited 1.
#
# These are TUPLES, not classes, for two reasons: `except` and `isinstance` both
# accept a tuple, and a tuple lets one alias match whichever driver actually
# raised. That matters because the driver is chosen at runtime from config, and a
# single process can legitimately touch both (a migration tool reading a
# plaintext file while the app runs encrypted). Matching on both is always
# correct and never over-matches — the class sets are disjoint.
#
# RULE: never write `except sqlite3.<X>Error` in this codebase. Use these.
# Enforced by tests/test_db_exception_aliases.py, which scans every .py under
# server/ (comments and docstrings excluded, so this block is allowed to name
# the stdlib classes) and fails on any reintroduction.

_DRIVER_MODULES = [_sqlite3]
for _mod_path in ("pysqlcipher3.dbapi2", "sqlcipher3.dbapi2"):
    try:
        _DRIVER_MODULES.append(__import__(_mod_path, fromlist=["dbapi2"]))
    except Exception:
        # Driver not installed on this machine — the stdlib entry alone is
        # correct there, because an absent driver can never raise.
        pass


def _alias(name: str) -> tuple:
    """Every installed driver's class for `name`, deduped, as an except-able tuple."""
    seen, out = set(), []
    for mod in _DRIVER_MODULES:
        cls = getattr(mod, name, None)
        if cls is not None and id(cls) not in seen:
            seen.add(id(cls))
            out.append(cls)
    return tuple(out)


DBError = _alias("Error")
DBDatabaseError = _alias("DatabaseError")
DBIntegrityError = _alias("IntegrityError")
DBOperationalError = _alias("OperationalError")
DBProgrammingError = _alias("ProgrammingError")
DBInterfaceError = _alias("InterfaceError")

# ── Opt-in connection pool ────────────────────────────────────────────────────
# With ORB_DB_POOL=1 we reuse ONE connection per (thread, db-path) with the
# PRAGMAs already applied, skipping ~8 PRAGMA statements per call. Keyed by
# thread because SQLite connections have thread affinity, and by db-path so a
# test/tool that repoints the DB never reuses a stale handle.
#
# STATUS (re-verified 2026-07-31): the full suite passes in BOTH modes —
# 773 passed unpooled, 773 passed with ORB_DB_POOL=1. An earlier revision of
# this comment predated the reentrancy fixes and read as though pooling were
# untested; it is not. What makes it safe now is specific, and any change here
# must preserve all three:
#   1. nested db_session on a shared handle uses SAVEPOINTs — only the outermost
#      frame commits or rolls back (see database.py);
#   2. pooled connections are a subclass whose close() is a lifecycle no-op that
#      RESETS connection state. This is the one that bit hardest: the repo's
#      `get_db(); isolation_level = None; BEGIN IMMEDIATE; ...; close()` idiom
#      leaked autocommit onto the shared handle, which silently turned every
#      LATER rollback into a no-op — an eMAR administration once survived its
#      own failed audit write that way;
#   3. standalone audit writes JOIN an already-open pooled transaction rather
#      than issuing BEGIN IMMEDIATE, which would discard the outer frame's work.
#
# DEFAULT ON since 2026-08-01. It used to default OFF on the reasoning that
# enabling it was an operator decision — that reasoning is now inverted, because
# the UNPOOLED path is the dangerous one and it was the default:
#
#   Per-call connections mean a real conn.close() per query. On the SQLCipher
#   driver we actually ship (sqlcipher3-wheels), `pysqlite_connection_close`
#   holds the GIL across sqlite3_close_v2 — verified by disassembling the shipped
#   binary; upstream CPython's own _sqlite3 releases it there, this fork does not.
#   sqlite3_close_v2 by the LAST closer checkpoints the WAL, so the whole process
#   freezes for ~1 ms per MB of WAL (measured: 17.5 MB WAL -> 17.0 ms of GIL
#   denied to every other thread, linear to 300 MB). Under ward-level churn on a
#   213 MB encrypted DB, two threads contending the per-inode VFS mutex while one
#   holds the GIL deadlocked the appliance permanently: /api/health dead, 0.2%
#   CPU, kill -9 the only exit. Captured live 2026-08-01, three identical
#   faulthandler dumps 8 s apart.
#
# Safety must not depend on remembering a line in .env — an appliance that boots
# without it would silently run the freeze-prone path with no warning. So the
# safe path is the default and ORB_DB_POOL=0 is the explicit opt-out.
#
# NOTE the test-suite blind spot this creates: the suite runs PLAINTEXT stdlib
# sqlite3, whose close DOES release the GIL, so it can never reproduce the above.
# Green tests are not evidence about this failure mode.
_pool_env = os.getenv("ORB_DB_POOL", "").strip().lower()
POOL_ENABLED = _pool_env not in ("0", "false", "no", "off")

_local = threading.local()
# sqlite3.Connection is a C type that rejects arbitrary attributes, so we track
# pooled connections by identity here. Bounded by threads × db-paths (tiny).
_pooled_ids: set[int] = set()


def is_pooled(conn) -> bool:
    """True if this connection is a live pooled connection (must not be closed
    by db_session — it's kept for reuse)."""
    return id(conn) in _pooled_ids


def _retire(conn) -> None:
    """Actually close a pooled handle. Pooled connections neuter .close() (so
    legacy direct closes can't destroy the shared handle mid-frame); the pool
    retires them through pool_close()."""
    _pooled_ids.discard(id(conn))
    try:
        real_close = getattr(conn, "pool_close", None)
        (real_close or conn.close)()
    except Exception:
        pass


def discard_pooled(conn) -> None:
    """Drop a pooled handle from THIS thread's cache (self-heal after the
    handle is found dead). Next get_pooled() builds a fresh one."""
    cache = getattr(_local, "conns", None)
    if cache:
        for path, cached in list(cache.items()):
            if cached is conn:
                cache.pop(path, None)
    _retire(conn)


def get_pooled(make_connection, db_path: str):
    """Return a per-thread pooled connection (creating it once), or None when
    pooling is disabled — in which case the caller opens a fresh connection.

    ``make_connection`` is database.get_db's raw builder; ``db_path`` keys the
    cache so a changed DB target invalidates the handle.
    """
    if not POOL_ENABLED:
        return None
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    conn = cache.get(db_path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")  # liveness probe; rebuild if the handle died
            return conn
        except Exception:
            cache.pop(db_path, None)
            _retire(conn)
    conn = make_connection()
    cache[db_path] = conn
    _pooled_ids.add(id(conn))
    return conn


def validate_backend_at_startup() -> None:
    """Called once at init so a bad ORB_DB_BACKEND surfaces loudly at boot."""
    logger.info("Database backend: %s (pool=%s)", BACKEND_NAME, "on" if POOL_ENABLED else "off")
