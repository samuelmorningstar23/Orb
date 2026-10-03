"""Driver-agnostic DB-API exception handling (db_backend.DB*Error).

WHY THIS FILE EXISTS
--------------------
database.py::_open_raw_connection swaps the driver to sqlcipher3 (or
pysqlcipher3) whenever ORB_DB_ENCRYPTION_KEY is set — the shipping appliance
configuration. sqlcipher3 declares its OWN DB-API exception hierarchy rooted at
builtins.Exception; it does not subclass the stdlib's. The consequence was that
every ``except sqlite3.IntegrityError`` in this codebase was DEAD CODE on an
encrypted deployment: correct-looking handlers that passed review and passed the
(unencrypted) test suite, then let the error escape as a raw traceback in
production.

Found 2026-07-31 via scripts/create_admin.py, which died with an unhandled
sqlcipher3.dbapi2.IntegrityError instead of printing "A user with id 'X' already
exists." and exiting 1.

The tests below pin three things:
  1. the type-system fact that motivated the aliases (so a future sqlcipher3
     release that fixes its hierarchy tells us, rather than silently making the
     aliases redundant),
  2. that the aliases actually catch a real error raised through the app's own
     connection helpers on an ENCRYPTED database,
  3. that no one reintroduces a bare ``except sqlite3.<X>Error`` — the
     source-level guard, which is the one that keeps this fixed.

Hermetic: every database is created under tmp_path with the key injected by
monkeypatch. The live orb.db is never touched.
"""
import pathlib
import re
import sqlite3
import tokenize

import pytest

import database
from db_backend import (
    DBDatabaseError,
    DBError,
    DBIntegrityError,
    DBOperationalError,
    DBProgrammingError,
)

sqlcipher3 = pytest.importorskip("sqlcipher3")
import sqlcipher3.dbapi2 as sqlcipher_dbapi2  # noqa: E402

TEST_KEY = "orb-unit-test-db-key"


# ── 1. The fact that makes the aliases necessary ─────────────────────────────

@pytest.mark.parametrize(
    "name", ["Error", "DatabaseError", "IntegrityError", "OperationalError", "ProgrammingError"]
)
def test_sqlcipher_exceptions_do_not_subclass_stdlib(name):
    """The whole reason db_backend.DB*Error exists.

    If this ever starts failing, sqlcipher3 has aligned its hierarchy with the
    stdlib and the aliases become belt-and-braces rather than load-bearing —
    still correct, but the urgency note in db_backend.py can be softened.
    """
    assert not issubclass(getattr(sqlcipher_dbapi2, name), getattr(sqlite3, name)), (
        f"sqlcipher3.dbapi2.{name} now subclasses sqlite3.{name} — update the "
        f"rationale in db_backend.py."
    )


@pytest.mark.parametrize(
    "alias,name",
    [
        (DBError, "Error"),
        (DBDatabaseError, "DatabaseError"),
        (DBIntegrityError, "IntegrityError"),
        (DBOperationalError, "OperationalError"),
        (DBProgrammingError, "ProgrammingError"),
    ],
)
def test_alias_covers_every_installed_driver(alias, name):
    """Each alias must match the stdlib class AND the SQLCipher class, because
    which one gets raised depends on runtime config, not on the call site."""
    assert getattr(sqlite3, name) in alias
    assert getattr(sqlcipher_dbapi2, name) in alias


# ── 2. End to end, on a real encrypted database ──────────────────────────────

def _encrypted_db_with_users(path: pathlib.Path):
    conn = sqlcipher_dbapi2.connect(str(path))
    conn.execute(f"PRAGMA key = \"x'{database._sqlcipher_key_hex(TEST_KEY)}'\"")
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO users (id, name) VALUES ('ADM001', 'First')")
    conn.commit()
    conn.close()


def test_duplicate_insert_is_caught_by_the_alias_on_an_encrypted_db(tmp_path, monkeypatch):
    """The exact production scenario: a UNIQUE violation through the app's own
    db_session on an encrypted DB is caught by DBIntegrityError."""
    db_path = tmp_path / "live.db"
    _encrypted_db_with_users(db_path)
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setattr(database, "DB_PATH", db_path)

    caught = False
    try:
        with database.db_session() as db:
            db.execute("INSERT INTO users (id, name) VALUES ('ADM001', 'Duplicate')")
    except DBIntegrityError:
        caught = True
    assert caught, "DBIntegrityError did not catch a UNIQUE violation on an encrypted DB"


def test_stdlib_handler_alone_would_have_missed_it(tmp_path, monkeypatch):
    """The regression itself, asserted directly.

    This is the test that would have failed before the fix and is the reason the
    aliases cannot be 'simplified' back to `except sqlite3.IntegrityError`.
    """
    db_path = tmp_path / "live.db"
    _encrypted_db_with_users(db_path)
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setattr(database, "DB_PATH", db_path)

    with pytest.raises(sqlcipher_dbapi2.IntegrityError):
        try:
            with database.db_session() as db:
                db.execute("INSERT INTO users (id, name) VALUES ('ADM001', 'Duplicate')")
        except sqlite3.IntegrityError:  # noqa: ORB001 - deliberate, proves the gap
            pytest.fail("stdlib handler unexpectedly caught the SQLCipher error")


# ── 3. The guard that keeps it fixed ─────────────────────────────────────────

# `except sqlite3.SomethingError` / `isinstance(x, sqlite3.SomethingError)`.
# Deliberately narrow: sqlite3.Connection, sqlite3.Row, sqlite3.connect and
# friends are fine — only the EXCEPTION classes are driver-specific.
_STDLIB_EXC = re.compile(
    r"\bsqlite3\.(Error|Warning|InterfaceError|DatabaseError|DataError|"
    r"OperationalError|IntegrityError|InternalError|ProgrammingError|"
    r"NotSupportedError)\b"
)

_SERVER_ROOT = pathlib.Path(__file__).resolve().parent.parent
# db_backend builds the aliases FROM the stdlib classes; this file asserts facts
# about them; and test_db_encryption.py opens a handle with the stdlib driver on
# purpose, to prove an encrypted file is opaque to it — there the specific class
# is the stronger assertion, not an oversight.
_ALLOWED = {"db_backend.py", "test_db_exception_aliases.py", "test_db_encryption.py"}


def _code_only(path: pathlib.Path):
    """Return (lines with comments/strings blanked out, original lines).

    Comments and docstrings that DISCUSS `except sqlite3.IntegrityError` are
    documentation, not handlers — this fix deliberately left several. A raw
    line scan flagged them, which would have pushed the next person to delete
    the explanation instead of the bug. Blanking by character span keeps line
    numbers intact so offenders still report a usable location.
    """
    lines = path.read_text().splitlines()
    grid = [list(ln) for ln in lines]
    with tokenize.open(path) as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            (srow, scol), (erow, ecol) = tok.start, tok.end
            for row in range(srow, erow + 1):
                if row - 1 >= len(grid):
                    break
                cells = grid[row - 1]
                lo = scol if row == srow else 0
                hi = ecol if row == erow else len(cells)
                for col in range(lo, min(hi, len(cells))):
                    cells[col] = " "
    return ["".join(c) for c in grid], lines


def test_no_stdlib_sqlite3_exception_classes_outside_the_seam():
    """Fail if anyone reintroduces a driver-specific exception reference.

    A handler written as `except sqlite3.IntegrityError` is not a style problem —
    on an encrypted appliance it simply never runs, and the bug it was written to
    contain reaches the clinician as a 500. Use db_backend.DB*Error instead.
    """
    offenders = []
    for path in _SERVER_ROOT.rglob("*.py"):
        if ".venv" in path.parts or path.name in _ALLOWED:
            continue
        rel = path.relative_to(_SERVER_ROOT)
        try:
            masked, raw_lines = _code_only(path)
        except (SyntaxError, tokenize.TokenError, UnicodeDecodeError):
            continue  # not importable Python; the test suite will fail elsewhere
        for lineno, line in enumerate(masked, 1):
            if _STDLIB_EXC.search(line):
                offenders.append(f"{rel}:{lineno}: {raw_lines[lineno - 1].strip()}")

    assert not offenders, (
        "Driver-specific sqlite3 exception classes found. These are dead code "
        "when ORB_DB_ENCRYPTION_KEY is set (the shipping config) because "
        "sqlcipher3 raises its own hierarchy. Import the alias from db_backend "
        "instead (DBIntegrityError, DBOperationalError, DBError, ...):\n  "
        + "\n  ".join(offenders)
    )
