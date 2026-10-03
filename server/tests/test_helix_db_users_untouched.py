"""init_helix_db must never write to, or rebuild, the users table.

It used to probe the users role CHECK with a trial INSERT and, on ANY failure
of that INSERT (not only a CHECK violation: SQLITE_BUSY while a backup held the
file would do), fall back to an executescript RENAME of users. executescript
commits before it runs, the 7-column copy then fails against the modern users
table, and every staff account is stranded in users_old. main.py treats a Helix
init failure as non-fatal, so the server came up with nobody able to log in.

Migration 33 owns the role CHECK and runs inside init_db() before main.py calls
init_helix_db(), so Helix has no business with the users table at all.
"""
import contextlib
import re

from database import db_session
import services.helix_db as helix_db

# A write aimed at the users table itself. REFERENCES users(id) inside a Helix
# CREATE TABLE is not one, so the patterns name the table as the target.
_USERS_WRITE = re.compile(
    r"\b(INSERT\s+(OR\s+\w+\s+)?INTO\s+users\b"
    r"|DELETE\s+FROM\s+users\b"
    r"|UPDATE\s+users\b"
    r"|ALTER\s+TABLE\s+users\b"
    r"|DROP\s+TABLE\s+(IF\s+EXISTS\s+)?users\b"
    r"|CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?users\b)",
    re.IGNORECASE,
)


def _users_state():
    with db_session() as db:
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()[0]
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        old = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users_old'"
        ).fetchone()
    return ddl, count, old


def test_init_helix_db_issues_no_statement_against_users(monkeypatch):
    statements: list[str] = []
    real = helix_db.db_session

    @contextlib.contextmanager
    def tracing_session(*args, **kwargs):
        with real(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(helix_db, "db_session", tracing_session)
    helix_db.init_helix_db()

    assert statements, "the trace saw nothing, so this test proves nothing"
    offending = [s for s in statements if _USERS_WRITE.search(s)]
    assert offending == []


def test_a_refused_users_insert_cannot_strand_staff_accounts():
    """The failure that used to trigger the rebuild, made real: every INSERT
    into users is refused. Helix init must leave users exactly as it was."""
    before = _users_state()
    assert before[1] > 0
    with db_session() as db:
        db.execute(
            "CREATE TRIGGER helix_probe_refused BEFORE INSERT ON users "
            "BEGIN SELECT RAISE(ABORT, 'database is locked'); END"
        )
    try:
        helix_db.init_helix_db()
    finally:
        with db_session() as db:
            db.execute("DROP TRIGGER IF EXISTS helix_probe_refused")

    after = _users_state()
    assert after == before
    assert after[2] is None, "users_old exists: the users table was rebuilt"
