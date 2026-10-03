"""Encryption-at-rest (SQLCipher) tests for the orb.db access paths.

orb.db is SQLCipher-encrypted at rest whenever ORB_DB_ENCRYPTION_KEY is set, and
an encrypted file is completely opaque to the stdlib sqlite3 driver — it raises
"file is not a database". Everything that opens orb.db therefore has to go
through the codec-aware helpers in database.py, and the driver those helpers hand
back is NOT interchangeable with sqlite3 at the type level. These tests pin the
facts the rest of the codebase leans on:

  * the Row factory must match whichever driver opened the connection,
  * the helpers really do write / read an encrypted file, and reject a bad key,
  * vec0 (sqlite-vec) and FTS5 still work through the codec,
  * with no key configured the behaviour is byte-for-byte the old plaintext path.

Hermetic by construction: every database here is created under tmp_path and the
key is injected with monkeypatch, so the live orb.db is never touched and the
default test environment (ORB_DB_ENCRYPTION_KEY unset, plaintext temp DB — see
conftest.py) is left exactly as it was.
"""
import sqlite3

import pytest

# SQLCipher is a native dependency and encryption at rest is opt-in, so a
# checkout without the driver must skip these tests rather than fail them.
sqlcipher3 = pytest.importorskip("sqlcipher3")

import sqlcipher3.dbapi2 as sqlcipher_dbapi2  # noqa: E402

import database  # noqa: E402

TEST_KEY = "orb-unit-test-db-key"

# First 16 bytes of any plaintext SQLite file. A SQLCipher file encrypts its
# header too, so the magic's absence is the on-disk proof of encryption (same
# check scripts/encrypt_db.py uses).
SQLITE_PLAINTEXT_MAGIC = b"SQLite format 3\x00"


def _apply_key(conn, key=TEST_KEY):
    """Key a connection exactly the way the app does.

    Going through database._sqlcipher_key_hex rather than hand-rolling the
    derivation is the point: if the app's derivation ever changed, files written
    here would stop being readable by the app and these tests would say so.
    """
    conn.execute(f"PRAGMA key = \"x'{database._sqlcipher_key_hex(key)}'\"")


def _make_encrypted_db(path, key=TEST_KEY):
    """Create a small SQLCipher-encrypted DB at `path` and close it."""
    conn = sqlcipher_dbapi2.connect(str(path))
    _apply_key(conn, key)
    conn.execute("CREATE TABLE vitals (patient_id TEXT, hr INTEGER)")
    conn.execute("INSERT INTO vitals VALUES ('P001', 88)")
    conn.commit()
    conn.close()


# ── Row factory ───────────────────────────────────────────────────────────────
# This is the regression that broke EVERY query the first time encryption was
# switched on: sqlite3.Row is a C type that type-checks its argument, so it
# cannot wrap a SQLCipher cursor.


def test_row_factory_for_stdlib_connection_is_stdlib_row():
    conn = sqlite3.connect(":memory:")
    try:
        assert database._row_factory_for(conn) is sqlite3.Row
    finally:
        conn.close()


def test_row_factory_for_sqlcipher_connection_is_sqlcipher_row():
    conn = sqlcipher_dbapi2.connect(":memory:")
    try:
        factory = database._row_factory_for(conn)
        assert factory is sqlcipher_dbapi2.Row
        # Distinct C types, not an alias — which is exactly why the helper exists.
        assert factory is not sqlite3.Row
    finally:
        conn.close()


def test_sqlcipher_rows_are_addressable_by_name_with_the_matched_factory():
    """Named column access is what the whole codebase does with rows, so assert
    the matched factory actually delivers it on a SQLCipher cursor."""
    conn = sqlcipher_dbapi2.connect(":memory:")
    try:
        conn.row_factory = database._row_factory_for(conn)
        row = conn.execute("SELECT 88 AS hr, 'P001' AS patient_id").fetchone()
        assert row["hr"] == 88
        assert row["patient_id"] == "P001"
    finally:
        conn.close()


def test_hardcoded_sqlite3_row_still_rejects_a_sqlcipher_cursor():
    """Pin the failure mode the helper works around.

    If a future driver ever made sqlite3.Row accept a SQLCipher cursor this test
    would fail loudly, which is the signal to revisit _row_factory_for — better
    than silently keeping a workaround nobody understands.
    """
    conn = sqlcipher_dbapi2.connect(":memory:")
    try:
        conn.row_factory = sqlite3.Row
        with pytest.raises(TypeError):
            conn.execute("SELECT 1 AS a").fetchone()
    finally:
        conn.close()


# ── On-disk encryption ────────────────────────────────────────────────────────


def test_encrypted_db_is_unreadable_by_stdlib_sqlite3(tmp_path):
    """The constraint that forces every orb.db caller through the helpers."""
    path = tmp_path / "encrypted.db"
    _make_encrypted_db(path)

    with path.open("rb") as fh:
        assert fh.read(16) != SQLITE_PLAINTEXT_MAGIC  # header itself is encrypted

    conn = sqlite3.connect(str(path))
    try:
        # sqlite3 only fails when it first has to read a page, not at connect().
        # Deliberately the STDLIB class, not db_backend.DBDatabaseError: this
        # test is an assertion ABOUT the stdlib driver (it opened the handle
        # itself), so the specific class is the stronger claim. Everywhere the
        # connection comes from database.py's helpers, use the alias instead.
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT COUNT(*) FROM vitals").fetchone()
    finally:
        conn.close()


def test_encrypted_db_is_readable_with_the_right_key(tmp_path):
    path = tmp_path / "encrypted.db"
    _make_encrypted_db(path)

    conn = sqlcipher_dbapi2.connect(str(path))
    try:
        _apply_key(conn)
        assert conn.execute("SELECT hr FROM vitals WHERE patient_id='P001'").fetchone()[0] == 88
    finally:
        conn.close()


def test_wrong_key_is_rejected(tmp_path):
    path = tmp_path / "encrypted.db"
    _make_encrypted_db(path)

    conn = sqlcipher_dbapi2.connect(str(path))
    try:
        _apply_key(conn, "not-the-right-key")
        # SQLCipher cannot distinguish a bad key from a non-database file, so the
        # error is the same generic DatabaseError — the important part is that it
        # refuses rather than returning garbage.
        with pytest.raises(sqlcipher_dbapi2.DatabaseError):
            conn.execute("SELECT COUNT(*) FROM vitals").fetchone()
    finally:
        conn.close()


def test_vec0_and_fts5_both_work_on_an_encrypted_connection(tmp_path):
    """sqlite-vec is a loadable C extension and FTS5 a virtual table; the two
    were believed to conflict with SQLCipher, which is why encryption was
    deferred for so long. They do not — this test is the standing evidence, so
    the concern is not re-litigated from memory.
    """
    sqlite_vec = pytest.importorskip("sqlite_vec")
    path = tmp_path / "encrypted_search.db"

    conn = sqlcipher_dbapi2.connect(str(path))
    try:
        _apply_key(conn)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        conn.execute("CREATE VIRTUAL TABLE knowledge_vec USING vec0(embedding float[4])")
        conn.execute(
            "INSERT INTO knowledge_vec(rowid, embedding) VALUES (1, ?)",
            (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
        )
        conn.execute(
            "INSERT INTO knowledge_vec(rowid, embedding) VALUES (2, ?)",
            (sqlite_vec.serialize_float32([0.0, 1.0, 0.0, 0.0]),),
        )
        nearest = conn.execute(
            "SELECT rowid FROM knowledge_vec WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT 1",
            (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
        ).fetchone()
        assert nearest[0] == 1

        conn.execute("CREATE VIRTUAL TABLE knowledge_fts USING fts5(body)")
        conn.execute("INSERT INTO knowledge_fts(rowid, body) VALUES (1, 'sepsis bundle lactate')")
        assert conn.execute(
            "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'sepsis'"
        ).fetchall() == [(1,)]
        conn.commit()
    finally:
        conn.close()

    # Both virtual tables live inside the encrypted file, not beside it.
    with path.open("rb") as fh:
        assert fh.read(16) != SQLITE_PLAINTEXT_MAGIC


# ── The codec-aware helpers ───────────────────────────────────────────────────


def test_open_raw_connection_at_with_apply_key_writes_an_encrypted_file(tmp_path, monkeypatch):
    """consistent_backup() relies on this: a backup of an encrypted DB must not
    land plaintext PHI on disk."""
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", TEST_KEY)
    path = tmp_path / "backup.db"

    conn = database._open_raw_connection_at(path, apply_key=True)
    try:
        assert type(conn).__module__.startswith("sqlcipher3")
        conn.execute("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, detail TEXT)")
        conn.execute("INSERT INTO audit_logs (detail) VALUES ('P001 chart opened')")
        conn.commit()
    finally:
        conn.close()

    with path.open("rb") as fh:
        assert fh.read(16) != SQLITE_PLAINTEXT_MAGIC
    # The PHI string must not be findable anywhere in the file.
    assert b"P001 chart opened" not in path.read_bytes()

    reopened = database._open_raw_connection_at(path, apply_key=True)
    try:
        assert reopened.execute("SELECT detail FROM audit_logs").fetchone()[0] == "P001 chart opened"
    finally:
        reopened.close()


def test_open_raw_connection_at_without_apply_key_stays_plaintext(tmp_path, monkeypatch):
    """apply_key=False is how callers deliberately write/read a plaintext copy
    (e.g. the pre-migration original) even while a key is configured."""
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", TEST_KEY)
    path = tmp_path / "plaintext.db"

    conn = database._open_raw_connection_at(path, apply_key=False)
    try:
        assert isinstance(conn, sqlite3.Connection)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
    finally:
        conn.close()

    with path.open("rb") as fh:
        assert fh.read(16) == SQLITE_PLAINTEXT_MAGIC


def test_open_raw_connection_is_plaintext_when_no_key_configured(tmp_path, monkeypatch):
    """No key configured must behave exactly like the pre-encryption code path —
    stdlib driver, plaintext file — which is what the default test environment
    and any un-migrated deployment run on."""
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", None)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "plain_live.db")

    conn = database._open_raw_connection()
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert database._row_factory_for(conn) is sqlite3.Row
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
    finally:
        conn.close()

    with (tmp_path / "plain_live.db").open("rb") as fh:
        assert fh.read(16) == SQLITE_PLAINTEXT_MAGIC


def test_build_connection_yields_named_rows_on_an_encrypted_db(tmp_path, monkeypatch):
    """End-to-end shape of what the app actually does: open the live DB path with
    a key set, apply the WAL/mmap PRAGMA tuning, and read a row by column name.
    This is the exact combination that failed when encryption was first enabled.
    """
    db_path = tmp_path / "live.db"
    _make_encrypted_db(db_path)
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setattr(database, "DB_PATH", db_path)

    conn = database._build_connection()
    try:
        assert type(conn).__module__.startswith("sqlcipher3")
        row = conn.execute("SELECT patient_id, hr FROM vitals").fetchone()
        assert row["patient_id"] == "P001"
        assert row["hr"] == 88
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_key_derivation_is_a_stable_32_byte_raw_key():
    """database._sqlcipher_key_hex is the shared contract between the app and
    scripts/encrypt_db.py; if it drifted, a migrated DB would stop opening."""
    key_hex = database._sqlcipher_key_hex(TEST_KEY)
    assert len(key_hex) == 64  # 32 raw bytes, hex-encoded
    assert int(key_hex, 16) >= 0  # pure hex, safe to inline in the PRAGMA
    assert key_hex == database._sqlcipher_key_hex(TEST_KEY)
    assert key_hex != database._sqlcipher_key_hex(TEST_KEY + "!")


# ── Fail closed: a key with no driver is refused, never plaintext ─────────────


def _hide_sqlcipher_drivers(monkeypatch):
    """Make both SQLCipher imports fail the way they do on a box without them."""
    import sys
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)
    monkeypatch.setitem(sys.modules, "pysqlcipher3", None)


def test_key_without_driver_refuses_the_live_connection(tmp_path, monkeypatch):
    """It used to log one warning and open the file with stdlib sqlite3, so an
    appliance configured for encryption wrote PHI in the clear."""
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "would_be_plaintext.db")
    _hide_sqlcipher_drivers(monkeypatch)

    with pytest.raises(RuntimeError, match="Refusing to open the database in plaintext"):
        database._open_raw_connection()
    assert not (tmp_path / "would_be_plaintext.db").exists()


def test_key_without_driver_refuses_the_backup_path(tmp_path, monkeypatch):
    """The backup copy of an encrypted database must not land in plaintext."""
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", TEST_KEY)
    _hide_sqlcipher_drivers(monkeypatch)

    with pytest.raises(RuntimeError, match="Refusing to open the database in plaintext"):
        database._open_raw_connection_at(tmp_path / "backup.db", apply_key=True)
    assert not (tmp_path / "backup.db").exists()


def test_encryption_status_reads_the_connection_not_the_key(tmp_path, monkeypatch):
    db_path = tmp_path / "live.db"
    _make_encrypted_db(db_path)
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setattr(database, "DB_PATH", db_path)

    status = database.encryption_at_rest_status()
    assert status["key_set"] is True
    assert status["active"] is True
    assert status["cipher_version"]
    database.verify_encryption_at_rest()  # does not raise


def test_plaintext_connection_is_reported_inactive(tmp_path, monkeypatch):
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", None)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "plain.db")

    status = database.encryption_at_rest_status()
    assert status == {"key_set": False, "cipher_version": None, "active": False}
    database.verify_encryption_at_rest()  # no key, nothing to enforce


def test_boot_probe_refuses_a_key_with_no_cipher_on_the_connection(tmp_path, monkeypatch):
    """A driver that imports but carries no codec answers no cipher_version. With
    a key configured that is a plaintext database, and boot must stop."""
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "plain.db")
    monkeypatch.setattr(database, "_open_raw_connection",
                        lambda pooled=False: sqlite3.connect(str(tmp_path / "plain.db")))

    assert database.encryption_at_rest_status()["active"] is False
    with pytest.raises(RuntimeError, match="no SQLCipher cipher version"):
        database.verify_encryption_at_rest()
