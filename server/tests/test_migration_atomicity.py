"""Migration runner atomicity + interrupted-rebuild recovery.

WHAT THESE PROVE, and why they are written against a SEPARATE temp database
rather than the shared session DB: the failure being tested is a half-committed
schema, so the test must be free to leave one behind and inspect it.

Pre-fix behaviour (verified 2026-08-06 by reverting migrations.py):
  * test_runner_is_atomic_without_an_open_transaction  -> FAILED
        run_pending_migrations ran every CREATE/ALTER/DROP in autocommit, so a
        migration that raised left `sage_insights` RENAMEd away and its rows
        stranded in `sage_insights_old`.
  * test_interrupted_rebuild_leaves_no_stranded_rows   -> FAILED
        same, via the real rebuild in migration 3.
  * test_m9_recovers_rows_stranded_by_an_interrupted_rebuild -> FAILED
        migration 9 did not exist; the rows stayed unreachable and the ledger
        recorded the rebuild as applied.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrations  # noqa: E402
# Driver-agnostic DB-API exception aliases. NEVER `except sqlite3.<X>Error` in
# this repo — on the encrypted appliance the driver is sqlcipher3, whose
# exception classes do not subclass the stdlib's, so such a handler is dead
# code. Enforced by tests/test_db_exception_aliases.py.
from db_backend import DBOperationalError  # noqa: E402

# The aliases are except-tuples; take a concrete class when we need to RAISE one.
_OperationalError = DBOperationalError[0]


_OLD_SAGE_INSIGHTS_DDL = """
CREATE TABLE sage_insights (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    insight_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('critical','warning','info')),
    title TEXT NOT NULL,
    detail TEXT,
    suggested_action_type TEXT,
    suggested_action_target TEXT,
    target_user_id TEXT,
    dedup_key TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','dismissed','acted')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# The post-migration-3 shape, i.e. what executescript(SCHEMA) recreates on the
# boot AFTER an interrupted rebuild.
_NEW_SAGE_INSIGHTS_DDL = _OLD_SAGE_INSIGHTS_DDL.replace(
    "CHECK (state IN ('active','dismissed','acted'))",
    "CHECK (state IN ('active','dismissed','acted','auto_resolved'))",
)


def _conn(tmp_path, name="m.db"):
    """A bare connection with the app's transaction semantics (legacy
    isolation_level), NOT wrapped in db_session — the point is to control
    exactly whether a transaction is open."""
    c = sqlite3.connect(str(tmp_path / name))
    c.row_factory = sqlite3.Row
    return c


def _seed_pre_m3(conn, rows=500):
    conn.executescript(_OLD_SAGE_INSIGHTS_DDL)
    conn.executemany(
        "INSERT INTO sage_insights (id, patient_id, insight_type, severity, title, state) "
        "VALUES (?, ?, 'deterioration', 'critical', ?, 'active')",
        [(f"SI-{i}", "P-1", f"SEPSIS RISK {i}") for i in range(rows)],
    )
    conn.commit()


def _only(monkeypatch, version, wrapper=None):
    """Restrict the registry to ONE migration so these tests exercise it without
    dragging in every other table's dependencies."""
    m = next(x for x in migrations.MIGRATIONS if x.version == version)
    apply_fn = wrapper(m.apply) if wrapper else m.apply
    monkeypatch.setattr(migrations, "MIGRATIONS",
                        [migrations.Migration(version, m.name, apply_fn)])


def _ledger(conn) -> set:
    """Applied versions, treating an ABSENT ledger as 'nothing applied' — a
    fully rolled-back run legitimately un-creates the ledger table itself."""
    try:
        return {r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations").fetchall()}
    except DBOperationalError:
        return set()


def test_runner_is_atomic_without_an_open_transaction(tmp_path, monkeypatch):
    """A migration that FAILS must leave the schema byte-for-byte unchanged,
    even when the caller had no transaction open.

    Pre-fix this failed: `ALTER TABLE sage_insights RENAME TO sage_insights_old`
    committed on its own, so the live table was destroyed by a migration that
    never completed.
    """
    conn = _conn(tmp_path)
    _seed_pre_m3(conn)

    def blow_up_after_the_rename(real_apply):
        def _apply(c):
            c.execute("ALTER TABLE sage_insights RENAME TO sage_insights_old")
            raise RuntimeError("simulated mid-migration failure (disk full)")
        return _apply

    _only(monkeypatch, 3, wrapper=blow_up_after_the_rename)

    assert conn.in_transaction is False, "precondition: caller holds no transaction"
    with pytest.raises(RuntimeError):
        migrations.run_pending_migrations(conn)

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "sage_insights" in tables, "the live table was destroyed by a failed migration"
    assert "sage_insights_old" not in tables, "a failed migration left a stranded rebuild table"
    assert conn.execute("SELECT COUNT(*) FROM sage_insights").fetchone()[0] == 500
    assert 3 not in _ledger(conn), "a migration that raised was still recorded as applied"
    conn.close()


def test_runner_still_joins_an_open_caller_transaction(tmp_path, monkeypatch):
    """The historical contract is preserved: when the caller already owns a
    transaction (init_db's db_session), the runner must NOT commit it early —
    the caller's rollback still has to undo the migrations."""
    conn = _conn(tmp_path)
    _seed_pre_m3(conn, rows=3)
    conn.execute("BEGIN")
    conn.execute("INSERT INTO sage_insights (id, patient_id, insight_type, severity, title) "
                 "VALUES ('SI-CALLER', 'P-1', 'x', 'info', 'caller work')")
    assert conn.in_transaction is True

    _only(monkeypatch, 3)
    migrations.run_pending_migrations(conn)
    conn.rollback()  # the caller decides — this must undo the migration too

    assert 3 not in _ledger(conn), "runner committed on its own inside a caller transaction"
    assert conn.execute(
        "SELECT COUNT(*) FROM sage_insights WHERE id = 'SI-CALLER'").fetchone()[0] == 0
    conn.close()


def test_interrupted_rebuild_leaves_no_stranded_rows(tmp_path, monkeypatch):
    """The real migration 3, interrupted partway through by an I/O error, must
    not strand rows in sage_insights_old."""
    conn = _conn(tmp_path)
    _seed_pre_m3(conn, rows=120)

    def die_on_the_row_copy(real_apply):
        def _apply(c):
            class _Proxy:
                def __init__(self, inner):
                    self._c = inner

                def execute(self, sql, *a, **k):
                    if "INSERT INTO sage_insights SELECT" in str(sql):
                        raise _OperationalError("disk I/O error")
                    return self._c.execute(sql, *a, **k)

                def __getattr__(self, n):
                    return getattr(self._c, n)

            return real_apply(_Proxy(c))
        return _apply

    _only(monkeypatch, 3, wrapper=die_on_the_row_copy)

    with pytest.raises(DBOperationalError):
        migrations.run_pending_migrations(conn)

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "sage_insights_old" not in tables
    assert conn.execute("SELECT COUNT(*) FROM sage_insights").fetchone()[0] == 120
    conn.close()


def test_m9_recovers_rows_stranded_by_an_interrupted_rebuild(tmp_path, monkeypatch):
    """Repair path for a database damaged BEFORE the atomicity guard existed.

    Reconstructs the exact post-crash state: rows in sage_insights_old, an empty
    freshly-recreated sage_insights, and a ledger that has already recorded the
    rebuild as applied.
    """
    conn = _conn(tmp_path, "stranded.db")
    conn.executescript(_NEW_SAGE_INSIGHTS_DDL)  # recreated empty by executescript(SCHEMA)
    conn.executescript(_OLD_SAGE_INSIGHTS_DDL.replace(
        "CREATE TABLE sage_insights", "CREATE TABLE sage_insights_old"))
    conn.executemany(
        "INSERT INTO sage_insights_old (id, patient_id, insight_type, severity, title, state) "
        "VALUES (?, ?, 'deterioration', 'critical', ?, ?)",
        [(f"SI-{i}", "P-1", f"SEPSIS RISK {i}", "active" if i % 2 else "dismissed")
         for i in range(500)],
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM sage_insights").fetchone()[0] == 0

    _only(monkeypatch, 9)
    migrations.run_pending_migrations(conn)

    recovered = conn.execute("SELECT COUNT(*) FROM sage_insights").fetchone()[0]
    assert recovered == 500, f"only {recovered} of 500 stranded insights recovered"
    # original state preserved, not rewritten
    assert conn.execute(
        "SELECT COUNT(*) FROM sage_insights WHERE state = 'dismissed'").fetchone()[0] == 250
    # non-destructive: the evidence is left for an operator
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "sage_insights_old" in tables, "migration 9 must not drop the old table"

    # idempotent: a second run (or a wiped ledger) inserts nothing new
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()
    migrations.run_pending_migrations(conn)
    assert conn.execute("SELECT COUNT(*) FROM sage_insights").fetchone()[0] == 500
    conn.close()


def test_m9_is_a_noop_on_a_healthy_database(tmp_path):
    """The overwhelmingly common case: no sage_insights_old, nothing to do,
    nothing touched."""
    conn = _conn(tmp_path, "healthy.db")
    conn.executescript(_NEW_SAGE_INSIGHTS_DDL)
    conn.execute("INSERT INTO sage_insights (id, patient_id, insight_type, severity, title) "
                 "VALUES ('SI-1', 'P-1', 'x', 'info', 'live')")
    conn.commit()

    migrations._m9_recover_stranded_sage_insights(conn)

    assert conn.execute("SELECT COUNT(*) FROM sage_insights").fetchone()[0] == 1
    conn.close()
