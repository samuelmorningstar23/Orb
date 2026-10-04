"""Migration files: import order, and migration 48's foreign-key check.

(a) A migration file starts with "from migrations import Migration", and
importing migrations collects server/migrations_his/. So importing a migration
file first in a fresh process ("from migrations_his.m0048_users_role_batch
import FROZEN_ROLES") re-entered the half-imported file and failed with a
misleading "defines no MIGRATION". The package now finishes collection before
any file import completes, and a genuine cycle gets an error that says what
happened.

(b) Migration 48 rebuilds users, an FK parent, with foreign_keys OFF. It now
runs PRAGMA foreign_key_check over the tables that reference users before and
after the rebuild, and refuses if the rebuild orphaned a child row. Its
version, name and effect are unchanged: it is already applied on existing
databases.
"""
import importlib
import os
import sqlite3
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

import migrations
from migrations_his import MigrationDiscoveryError

SERVER_DIR = Path(__file__).resolve().parents[1]
M48 = importlib.import_module("migrations_his.m0048_users_role_batch")


def _run(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", body], cwd=str(SERVER_DIR),
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
             "ORB_DEPLOYMENT_PROFILE": "dev",
             "ORB_JWT_SECRET": "probe-only-secret-0123456789abcdef"},
        capture_output=True, text=True, timeout=120)


# ── (a) Import order ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("module", ["m0048_users_role_batch", "m0049_helix_tables_in_runner"])
def test_a_migration_file_can_be_imported_first(module):
    proc = _run(
        f"from migrations_his.{module} import MIGRATION\n"
        "import migrations\n"
        "vs = {m.version for m in migrations.MIGRATIONS}\n"
        "print('OK', MIGRATION.version, MIGRATION.version in vs, {48, 49} <= vs)\n")
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout and "True True" in proc.stdout, proc.stdout


def test_the_reported_one_liner_works():
    proc = _run("from migrations_his.m0048_users_role_batch import FROZEN_ROLES\n"
                "print(len(FROZEN_ROLES))\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "11"


def test_a_real_import_cycle_gets_an_error_that_says_so(tmp_path, monkeypatch):
    """A file whose own import triggers collection of its directory is the
    cycle. The collector names it instead of claiming the file is broken."""
    pkg = f"orb_cycle_probe_{uuid.uuid4().hex[:8]}"
    pkg_dir = tmp_path / pkg
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "m0090_cycle.py").write_text(textwrap.dedent(f"""
        from pathlib import Path
        from migrations_his import collect_from
        collect_from(Path(__file__).parent, package={pkg!r})
        from migrations import Migration

        def apply(conn):
            pass

        MIGRATION = Migration(90, "cycle", apply)
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        with pytest.raises(MigrationDiscoveryError, match="was being imported") as exc:
            importlib.import_module(f"{pkg}.m0090_cycle")
        assert "defines no MIGRATION" not in str(exc.value)
        assert "import migrations" in str(exc.value)
    finally:
        for name in [n for n in sys.modules if n == pkg or n.startswith(pkg + ".")]:
            del sys.modules[name]


def test_a_file_with_no_migration_is_still_refused_as_before(tmp_path):
    from migrations_his import collect_from
    (tmp_path / "m0090_empty.py").write_text("X = 1\n")
    with pytest.raises(MigrationDiscoveryError, match="defines no MIGRATION"):
        collect_from(tmp_path)


# ── (b) Migration 48's foreign-key check ──────────────────────────────────────

_ROLES_33 = ("doctor", "nurse", "admin", "patient", "pharmacist", "cashier")


def _users_db(path, roles):
    """The full users shape with the given role CHECK, two FK children and a
    few rows, foreign_keys ON like every Orb connection."""
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA foreign_keys = ON")
    role_list = ", ".join(f"'{r}'" for r in roles)
    raw.execute(f"""CREATE TABLE users (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ({role_list})),
        department TEXT, registration_number TEXT, password_hash TEXT NOT NULL,
        pin_hash TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        failed_login_attempts INTEGER DEFAULT 0, login_locked_until REAL,
        totp_secret TEXT, totp_enabled INTEGER DEFAULT 0, token_version INTEGER DEFAULT 0,
        totp_last_step INTEGER DEFAULT -1, is_active INTEGER DEFAULT 1,
        device_pin_hash TEXT, device_pin_set_at TIMESTAMP,
        device_pin_failed_attempts INTEGER DEFAULT 0, device_pin_locked_until REAL)""")
    raw.execute("CREATE TABLE audit_like (id INTEGER PRIMARY KEY, actor TEXT REFERENCES users(id))")
    raw.execute("CREATE TABLE patients_like (id TEXT PRIMARY KEY, "
                "attending_doctor_id TEXT REFERENCES users(id))")
    raw.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, note TEXT)")
    raw.execute("INSERT INTO users (id, name, role, password_hash) VALUES ('D1', 'Dr A', 'doctor', 'x')")
    raw.execute("INSERT INTO users (id, name, role, password_hash) VALUES ('A1', 'Adm', 'admin', 'y')")
    raw.execute("INSERT INTO audit_like (actor) VALUES ('D1')")
    raw.execute("INSERT INTO patients_like (id, attending_doctor_id) VALUES ('P1', 'A1')")
    raw.commit()
    return raw


def _apply_in_runner_txn(raw):
    raw.isolation_level = None
    raw.execute("BEGIN IMMEDIATE")
    M48.apply(raw)
    assert raw.in_transaction, "the runner must get its transaction back"
    raw.execute("COMMIT")


def test_identity_is_unchanged():
    assert M48.MIGRATION.version == 48
    assert M48.MIGRATION.name == "users_role_batch"
    assert M48.FROZEN_ROLES == (
        "doctor", "nurse", "admin", "patient", "pharmacist", "cashier",
        "receptionist", "ward_clerk", "lab_technician", "pathologist", "phlebotomist")
    assert tuple(M48.FROZEN_ROLES) == tuple(migrations.LATEST_USERS_ROLE_CHECK)


def test_a_clean_rebuild_passes_the_check(tmp_path):
    raw = _users_db(tmp_path / "clean.db", _ROLES_33)
    _apply_in_runner_txn(raw)
    ddl = raw.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
    assert "'phlebotomist'" in ddl
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    assert M48._users_fk_violations(raw) == set()
    raw.close()


def test_a_pre_existing_orphan_is_not_blamed_on_the_rebuild(tmp_path):
    raw = _users_db(tmp_path / "orphan.db", _ROLES_33)
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute("INSERT INTO audit_like (actor) VALUES ('GONE01')")
    raw.commit()
    raw.execute("PRAGMA foreign_keys = ON")
    before = M48._users_fk_violations(raw)
    assert len(before) == 1

    _apply_in_runner_txn(raw)
    assert "'ward_clerk'" in raw.execute(
        "SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
    assert M48._users_fk_violations(raw) == before
    raw.close()


def test_a_rebuild_that_orphans_a_child_is_refused(tmp_path, monkeypatch):
    real = M48._rebuild_users_role_check

    def breaking_rebuild(conn, roles):
        real(conn, roles)
        # Model a rebuild that lost a parent row the children still point at.
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM users WHERE id = 'D1'")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

    monkeypatch.setattr(M48, "_rebuild_users_role_check", breaking_rebuild)
    raw = _users_db(tmp_path / "broken.db", _ROLES_33)
    raw.isolation_level = None
    raw.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="left 1 child row"):
        M48.apply(raw)
    raw.execute("ROLLBACK")
    raw.close()


def test_the_no_op_path_skips_the_scan_and_still_calls_the_rebuild(tmp_path, monkeypatch):
    """A fresh database already names every role, so the rebuild no-ops and
    there is nothing to check: the scan does not run, and the rebuild helper is
    still called exactly as before (the effect is unchanged)."""
    calls = []
    real = M48._rebuild_users_role_check

    def spy(conn, roles):
        calls.append(tuple(roles))
        real(conn, roles)

    def no_scan(conn):
        raise AssertionError("the foreign-key scan ran on a no-op")

    monkeypatch.setattr(M48, "_rebuild_users_role_check", spy)
    monkeypatch.setattr(M48, "_users_fk_violations", no_scan)
    raw = _users_db(tmp_path / "wide.db", M48.FROZEN_ROLES)
    ddl = raw.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
    _apply_in_runner_txn(raw)
    assert calls == [M48.FROZEN_ROLES]
    assert raw.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0] == ddl
    raw.close()


def test_the_runner_records_48_after_a_checked_rebuild(tmp_path, monkeypatch):
    raw = _users_db(tmp_path / "runner.db", _ROLES_33)
    keep = [m for m in migrations.MIGRATIONS if m.version == 48]
    monkeypatch.setattr(migrations, "MIGRATIONS", keep)
    assert migrations.run_pending_migrations(raw) == [48]
    assert {r[0] for r in raw.execute("SELECT version FROM schema_migrations")} == {48}
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    raw.close()
