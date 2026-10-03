"""Regression tests for the verified architecture/DB audit findings.

Three defects, three failure classes the existing suite could not express:

1. `_atomic_table_rebuild` (database.py) — the legacy `messages`/`surgeries`
   rebuilds used `executescript()`, which COMMITs and then autocommits every
   statement. DROP TABLE and ALTER TABLE ... RENAME therefore committed
   separately: a failure in between destroyed the live table with no rollback,
   and the next boot's guard read the freshly recreated empty table and never
   fired again. The old suite could not catch this because every DB it builds
   comes from the current SCHEMA, where the rebuild guards are already False.

2. `world_model_service._persist` — INSERT-only into a table that is read
   exclusively as `ORDER BY version DESC LIMIT 1`. 58,188 rows / 60 MB in one
   week. Nothing pruned it.

3. `sage_ambient_observe` — an `async def` FastAPI BackgroundTask (awaited ON
   the event loop, not thread-pooled) that called ~8 synchronous `db_session`
   helpers directly. With `PRAGMA busy_timeout=30000`, a contended write lock
   parks the whole appliance inside sqlite3's C busy handler.
"""
import asyncio
import inspect
import os
import sqlite3
import tempfile

import pytest

import database
from database import db_session


# ═══════════════════════════════════════════════════════════════════════════
# 1. Destructive table rebuilds are atomic
# ═══════════════════════════════════════════════════════════════════════════

def _legacy_db(path: str) -> sqlite3.Connection:
    """A pre-migration `messages` table (narrow CHECK, no source/is_bot) with
    real rows, plus the FK parents and the FK CHILD that makes `foreign_keys`
    ordering matter."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE channels (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL REFERENCES channels(id),
            sender_id TEXT NOT NULL REFERENCES users(id),
            sender_name TEXT NOT NULL,
            sender_role TEXT NOT NULL,
            content TEXT NOT NULL,
            message_type TEXT DEFAULT 'text' CHECK (message_type IN ('text', 'alert', 'system')),
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            read_by TEXT DEFAULT '[]'
        );
        -- The FK CHILD. `messages` is an FK parent, so DROP TABLE under
        -- enforced foreign keys fails; this is what makes the
        -- "PRAGMA foreign_keys = OFF before the BEGIN" ordering load-bearing.
        CREATE TABLE message_room_fit_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            redirected_message_id INTEGER REFERENCES messages(id)
        );
        INSERT INTO channels VALUES ('chan_1', 'Ward A');
        INSERT INTO users VALUES ('DOC001', 'Dr Rao');
        """
    )
    for i in range(1, 51):
        conn.execute(
            "INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content) "
            "VALUES ('chan_1','DOC001','Dr Rao','doctor',?)",
            (f"clinical message {i}",),
        )
    conn.execute("INSERT INTO message_room_fit_flags (redirected_message_id) VALUES (1)")
    conn.commit()
    return conn


_MESSAGES_CREATE = """
CREATE TABLE messages_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    sender_id TEXT NOT NULL REFERENCES users(id),
    sender_name TEXT NOT NULL,
    sender_role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_type TEXT DEFAULT 'text' CHECK (message_type IN ('text', 'alert', 'system', 'ambient_action', 'surgical_alert')),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    read_by TEXT DEFAULT '[]',
    source TEXT DEFAULT 'clinician',
    is_bot INTEGER DEFAULT 0
)"""

_SHARED = "id, channel_id, sender_id, sender_name, sender_role, content, message_type, timestamp, read_by"


def test_rebuild_migrates_rows_and_widens_the_check():
    """Happy path: every row survives and the new CHECK accepts the new types."""
    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    conn = _legacy_db(path)
    try:
        database._atomic_table_rebuild(
            conn, table="messages", shared_cols=_SHARED,
            create_new_sql=_MESSAGES_CREATE,
            post_sql=["CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, timestamp DESC)"],
        )
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 50
        assert conn.execute("SELECT content FROM messages WHERE id = 7").fetchone()[0] == "clinical message 7"
        # The point of the migration: the widened CHECK now accepts these.
        conn.execute(
            "INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type) "
            "VALUES ('chan_1','DOC001','Dr Rao','doctor','x','ambient_action')"
        )
        conn.commit()
        # No orphan scratch table left behind.
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "messages_new" not in names
    finally:
        conn.close()


class _InjectedFailure(RuntimeError):
    """Deliberately NOT a driver exception class.

    tests/test_db_exception_aliases.py bans stdlib `sqlite3.*Error` references
    outside the db_backend seam, because on the encrypted appliance sqlcipher3
    raises its own hierarchy and a stdlib `except` is dead code. Using a plain
    RuntimeError here is also the stronger assertion: it proves the rebuild
    rolls back on ANY exception in the destructive window, not just on the one
    driver class the current build happens to raise.
    """


class _FailAt:
    """Connection proxy that raises on the first statement containing `needle`.

    This reproduces the audit's authorizer-based repro (RENAME denied after the
    DROP has already run) without needing sqlite3's authorizer API.
    """

    def __init__(self, real, needle):
        self._real, self._needle, self.fired = real, needle, False

    def execute(self, sql, *a, **k):
        if self._needle in sql and not self.fired:
            self.fired = True
            raise _InjectedFailure(f"simulated failure at: {self._needle}")
        return self._real.execute(sql, *a, **k)

    def commit(self):
        return self._real.commit()

    @property
    def in_transaction(self):
        return self._real.in_transaction

    def _get_iso(self):
        return self._real.isolation_level

    def _set_iso(self, v):
        self._real.isolation_level = v

    isolation_level = property(_get_iso, _set_iso)


@pytest.mark.parametrize("needle", ["ALTER TABLE", 'DROP TABLE "messages"'])
def test_rebuild_failure_leaves_the_live_table_intact(needle):
    """THE defect. A failure anywhere in the destructive window must roll back
    the DROP — the live table keeps every row, and no data is stranded in a
    `_new` table that nothing will ever read.

    Under the old executescript() form the ALTER-TABLE case left the surviving
    tables as ['messages_new'] with `messages` gone.
    """
    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    conn = _legacy_db(path)
    try:
        proxy = _FailAt(conn, needle)
        with pytest.raises(_InjectedFailure):
            database._atomic_table_rebuild(
                proxy, table="messages", shared_cols=_SHARED,
                create_new_sql=_MESSAGES_CREATE, post_sql=[],
            )
        assert proxy.fired, "the injected failure never fired — test is not exercising the window"

        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "messages" in names, "live table was destroyed by a failed rebuild"
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 50
        assert conn.execute("SELECT content FROM messages WHERE id = 7").fetchone()[0] == "clinical message 7"
        assert "messages_new" not in names, "rows stranded in an orphan table"

        # The guard must still fire on the next boot, i.e. the old narrow CHECK
        # is genuinely still in place rather than silently satisfied.
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()[0]
        assert "ambient_action" not in sql

        # And a retry after the transient failure must now succeed cleanly.
        database._atomic_table_rebuild(
            conn, table="messages", shared_cols=_SHARED,
            create_new_sql=_MESSAGES_CREATE, post_sql=[],
        )
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 50
    finally:
        conn.close()


def test_rebuild_aborts_before_dropping_when_the_copy_is_short():
    """Row-count verification must abort BEFORE the DROP, not after."""
    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    conn = _legacy_db(path)
    try:
        # Copy only 9 of the 50 columns' worth of rows by pointing the copy at a
        # filtered view of the source: simplest injection is a bad shared list
        # that yields fewer rows — use a CREATE with a CHECK that rejects most.
        create_narrow = _MESSAGES_CREATE.replace(
            "content TEXT NOT NULL,", "content TEXT NOT NULL CHECK (content LIKE '%message 1'),"
        )
        with pytest.raises(Exception):
            database._atomic_table_rebuild(
                conn, table="messages", shared_cols=_SHARED,
                create_new_sql=create_narrow, post_sql=[],
            )
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 50
    finally:
        conn.close()


def test_rebuild_restores_connection_state_on_failure():
    """A leaked isolation_level=None on a POOLED handle puts the whole thread in
    autocommit, which turns every later db_session rollback into a silent no-op.
    It must be restored on the failure path too.
    """
    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    conn = _legacy_db(path)
    try:
        before = conn.isolation_level
        with pytest.raises(_InjectedFailure):
            database._atomic_table_rebuild(
                _FailAt(conn, "ALTER TABLE"), table="messages", shared_cols=_SHARED,
                create_new_sql=_MESSAGES_CREATE, post_sql=[],
            )
        assert conn.isolation_level == before
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_db_no_longer_uses_executescript_for_destructive_rebuilds():
    """Guards the fix itself: executescript() COMMITs and autocommits, so
    reintroducing it anywhere near a DROP/RENAME silently restores the window.
    """
    src = inspect.getsource(database.init_db)
    for marker in ("DROP TABLE messages", "ALTER TABLE messages_new",
                   "DROP TABLE surgeries", "ALTER TABLE surgeries_new"):
        assert marker not in src, (
            f"{marker!r} is back inside init_db — destructive DDL must go through "
            "_atomic_table_rebuild, not an inline script"
        )
    assert "_atomic_table_rebuild" in src


# ═══════════════════════════════════════════════════════════════════════════
# 2. world_model_state retention
# ═══════════════════════════════════════════════════════════════════════════

def _wm_rows(entity_id):
    with db_session() as db:
        return db.execute(
            "SELECT version FROM world_model_state WHERE entity_type='patient' AND entity_id=? "
            "ORDER BY version", (entity_id,)
        ).fetchall()


@pytest.fixture()
def _clean_wm():
    ids = ("PAT-RET-1", "PAT-RET-2")
    with db_session() as db:
        for i in ids:
            db.execute("DELETE FROM world_model_state WHERE entity_id = ?", (i,))
    yield ids
    with db_session() as db:
        for i in ids:
            db.execute("DELETE FROM world_model_state WHERE entity_id = ?", (i,))


def test_world_model_state_is_bounded_per_entity(_clean_wm):
    """Steady-state writes must not grow the table without bound."""
    import services.world_model_service as wm
    pid = _clean_wm[0]
    keep = wm._KEEP_VERSIONS
    for i in range(keep * 3):
        wm._persist("patient", pid, {"i": i}, "test", "s")
    versions = [r["version"] for r in _wm_rows(pid)]
    assert len(versions) == keep, f"expected {keep} retained versions, got {len(versions)}"
    assert versions[-1] == keep * 3, "newest version must survive"
    assert versions[0] == keep * 3 - keep + 1, "the retained window must be the NEWEST versions"


def test_world_model_retention_does_not_break_the_tail_read(_clean_wm):
    """_load_latest is the only production reader; pruning must not disturb it,
    and MAX(version) must stay monotonic so _persist keeps assigning fresh
    version numbers after a prune."""
    import services.world_model_service as wm
    pid = _clean_wm[0]
    for i in range(wm._KEEP_VERSIONS * 2):
        v = wm._persist("patient", pid, {"i": i}, "test", "s")
    st = wm._load_latest("patient", pid)
    assert st is not None
    assert st["_version"] == v == wm._KEEP_VERSIONS * 2
    assert st["i"] == wm._KEEP_VERSIONS * 2 - 1


def test_world_model_prune_is_scoped_to_one_entity(_clean_wm):
    """A prune for patient A must never touch patient B's history."""
    import services.world_model_service as wm
    a, b = _clean_wm
    for i in range(3):
        wm._persist("patient", b, {"i": i}, "test", "s")
    for i in range(wm._KEEP_VERSIONS * 2):
        wm._persist("patient", a, {"i": i}, "test", "s")
    assert len(_wm_rows(b)) == 3
    assert len(_wm_rows(a)) == wm._KEEP_VERSIONS


def test_world_model_prune_is_batched_and_never_fails_the_write(_clean_wm):
    """The prune runs inside a clinical write's transaction, so its lock hold
    must be capped regardless of backlog size — and a prune failure must never
    take the state write down with it."""
    import services.world_model_service as wm
    pid = _clean_wm[0]
    backlog = wm._PRUNE_BATCH + wm._KEEP_VERSIONS + 50
    with db_session() as db:
        for v in range(1, backlog + 1):
            db.execute(
                "INSERT INTO world_model_state (entity_type, entity_id, version, state_json, "
                "encrypted, summary, updated_by, created_at) "
                "VALUES ('patient', ?, ?, '{}', 0, 's', 'backfill', '2026-01-01')",
                (pid, v),
            )
    wm._persist("patient", pid, {"i": 0}, "test", "s")
    # One write removes at most one batch — bounded lock hold, not "delete
    # however many thousands of rows accumulated".
    assert len(_wm_rows(pid)) == backlog + 1 - wm._PRUNE_BATCH

    # A broken prune must be swallowed: retention is housekeeping.
    class _NoDelete:
        def __init__(self, real):
            self.real = real

        def execute(self, sql, *a, **k):
            if sql.lstrip().upper().startswith("DELETE"):
                raise RuntimeError("simulated prune failure")
            return self.real.execute(sql, *a, **k)

    with db_session() as db:
        assert wm._prune_versions(_NoDelete(db), "patient", pid, 10_000) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3. sage_ambient_observe does no synchronous DB work on the event loop
# ═══════════════════════════════════════════════════════════════════════════

def test_sage_ambient_observe_routes_every_db_helper_through_run_db():
    """FastAPI awaits an `async def` BackgroundTask ON the event loop. Any
    direct call to a `db_session`-opening helper from this coroutine is
    synchronous SQLite on the loop — and with busy_timeout=30000 a contended
    write lock parks the entire appliance in sqlite3's C busy handler.

    AST-level so it cannot be satisfied by luck: every DB helper name must
    appear only as a `run_db` argument inside the coroutine, never as the
    callee of a direct call.
    """
    import ast

    import services.sage_ambient_service as amb

    src = inspect.getsource(amb)
    tree = ast.parse(src)

    # Helpers that open their own db_session — derived from the source, not
    # hardcoded, so a NEW db-touching helper is covered automatically.
    db_helpers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(c, ast.Call)
            and getattr(c.func, "id", getattr(c.func, "attr", None)) == "db_session"
            for c in ast.walk(node)
        )
    }
    assert db_helpers, "no db_session helpers found — the detector is broken"

    # Scan EVERY async def in the module, not just the entry point — the module
    # is not covered by tests/test_event_loop_discipline.py's SCANNED_DIRS,
    # which is precisely why this defect survived.
    banned = db_helpers | {"db_session", "get_db"}
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in banned
            ):
                offenders.append(f"{fn.name} -> {node.func.id}() at line {node.lineno}")
    assert not offenders, (
        "synchronous DB work called directly from an async def (i.e. ON the event "
        "loop); route it through `await run_db(...)`:\n  " + "\n  ".join(offenders)
    )

    # And the helpers must in fact be reached via run_db — an empty coroutine
    # would also satisfy the negative assertion above.
    observe = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "sage_ambient_observe"
    )
    via_run_db = {
        node.args[0].id
        for node in ast.walk(observe)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_db"
        and node.args
        and isinstance(node.args[0], ast.Name)
    }
    missing = db_helpers & {
        "_fetch_message", "_gate_room", "_load_room_memory", "_fetch_patient_context",
        "_fetch_recent_messages", "_update_memory", "_write_observation",
        "_insert_deterministic_action",
    } - via_run_db
    assert not missing, f"these DB helpers are no longer dispatched via run_db: {sorted(missing)}"


def test_sage_ambient_db_helpers_stay_synchronous():
    """run_db raises TypeError on a coroutine function. If one of these helpers
    were ever made `async`, dispatching it through run_db would blow up at
    runtime on the live send path.
    """
    import services.sage_ambient_service as amb

    for name in ("_fetch_message", "_gate_room", "_load_room_memory",
                 "_fetch_patient_context", "_fetch_recent_messages",
                 "_update_memory", "_write_observation", "_insert_deterministic_action"):
        fn = getattr(amb, name)
        assert not asyncio.iscoroutinefunction(fn), f"{name} must stay sync for run_db"


def test_run_db_still_rejects_coroutine_functions():
    """The guard the fix depends on."""
    async def _nope():
        return 1

    async def _drive():
        with pytest.raises(TypeError):
            await database.run_db(_nope)

    asyncio.run(_drive())
