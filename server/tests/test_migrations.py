"""Deferred B — versioned migration framework + DB backend seam."""
from database import db_session, init_db
import json
import os

import pytest

import migrations
import db_backend


def test_all_migrations_recorded_after_init():
    """init_db runs the framework; every registered migration is in the ledger."""
    init_db()
    with db_session() as db:
        applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}
    for m in migrations.MIGRATIONS:
        assert m.version in applied, f"migration {m.version} ({m.name}) not recorded"
    with db_session() as db:
        assert migrations.current_version(db) >= max(m.version for m in migrations.MIGRATIONS)


def test_run_pending_is_idempotent():
    """Re-running applies nothing and records no duplicates."""
    with db_session() as db:
        first = migrations.run_pending_migrations(db)
        before = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    # Everything is already applied now.
    with db_session() as db:
        second = migrations.run_pending_migrations(db)
        after = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert second == []            # nothing new applied
    assert after == before         # no duplicate ledger rows


def test_migration_1_created_indexes():
    with db_session() as db:
        idx = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
    for expected in (
        "idx_medications_patient_status",
        "idx_medications_source_verified",
        "idx_audit_patient",
        "idx_clinical_docs_patient_created",
    ):
        assert expected in idx, f"expected index {expected} from migration 1"


def test_migration_2_messages_idempotency():
    """Migration 2 adds the messages.idempotency_key column + its unique index."""
    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(messages)").fetchall()}
        idx = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
    assert "idempotency_key" in cols, "messages.idempotency_key column missing"
    assert "idx_messages_idempotency_key" in idx, "unique idempotency index missing"


def test_migration_19_adds_scribe_ack_columns():
    """The sign-anyway attestation gets its own columns — not a key inside
    verification_json, which the verifier rewrites wholesale."""
    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(scribe_notes)").fetchall()}
    assert "unverified_ack_by" in cols
    assert "unverified_ack_at" in cols


def _insert_note_with_marker(db, marker: dict | None) -> int:
    payload = {"status": "not_requested"}
    if marker is not None:
        payload["signer_acknowledged_unverified"] = marker
    cur = db.execute(
        "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, structured_note, "
        "note_format, status, verification_json) VALUES (?,?,?,?,?,?,?)",
        (None, "D001", "dictation", "S: note", "soap", "signed", json.dumps(payload)),
    )
    return cur.lastrowid


def test_migration_19_backfills_legacy_json_markers():
    """A pre-migration acknowledgement is promoted into the columns — but ONLY
    onto the note it was actually made on.

    A marker whose note_id points elsewhere is a copy carried onto a revision
    draft by create_revision_from_signed; promoting THAT would manufacture an
    attestation the signer never gave for that note. Same for a marker with no
    note_id: unattributable, so not claimed. Neither is deleted — the JSON
    stays readable as the historical record.
    """
    with db_session() as db:
        own_id = _insert_note_with_marker(db, {"by": "D001", "at": "2026-08-01T09:00:00+00:00"})
        db.execute(
            "UPDATE scribe_notes SET verification_json = ? WHERE id = ?",
            (json.dumps({"status": "not_requested", "signer_acknowledged_unverified": {
                "by": "D001", "at": "2026-08-01T09:00:00+00:00", "note_id": own_id,
                "reasons": ["verified=None"]}}), own_id),
        )
        # A marker copied onto a different note (the revision-inheritance bug).
        copied_id = _insert_note_with_marker(db, {
            "by": "D001", "at": "2026-08-01T09:00:00+00:00", "note_id": own_id,
        })
        # A marker from before note_id was recorded at all.
        orphan_id = _insert_note_with_marker(db, {"by": "D001", "at": "2026-08-01T09:00:00+00:00"})
        # A note with no acknowledgement at all.
        clean_id = _insert_note_with_marker(db, None)

    with db_session() as db:
        migrations._m19_scribe_unverified_ack_columns(db)

    with db_session() as db:
        rows = {
            r["id"]: dict(r) for r in db.execute(
                "SELECT id, unverified_ack_by, unverified_ack_at, verification_json "
                "FROM scribe_notes WHERE id IN (?,?,?,?)",
                (own_id, copied_id, orphan_id, clean_id),
            ).fetchall()
        }

    assert rows[own_id]["unverified_ack_by"] == "D001"
    assert rows[own_id]["unverified_ack_at"] == "2026-08-01T09:00:00+00:00"
    assert rows[copied_id]["unverified_ack_by"] is None
    assert rows[orphan_id]["unverified_ack_by"] is None
    assert rows[clean_id]["unverified_ack_by"] is None
    # Nothing was destroyed on the way.
    assert "signer_acknowledged_unverified" in rows[copied_id]["verification_json"]
    assert "signer_acknowledged_unverified" in rows[orphan_id]["verification_json"]


def test_migration_19_does_not_overwrite_a_column_ack_on_rerun():
    """Idempotent in the way that matters: a re-run (or a reset ledger) must not
    resurrect a stale JSON marker over the column the app has since written."""
    with db_session() as db:
        note_id = _insert_note_with_marker(db, {"by": "OLD-DOC", "at": "2026-01-01T00:00:00+00:00"})
        db.execute(
            "UPDATE scribe_notes SET verification_json = ?, unverified_ack_by = ?, "
            "unverified_ack_at = ? WHERE id = ?",
            (json.dumps({"status": "not_requested", "signer_acknowledged_unverified": {
                "by": "OLD-DOC", "at": "2026-01-01T00:00:00+00:00", "note_id": note_id}}),
             "REAL-DOC", "2026-08-11T10:00:00+00:00", note_id),
        )

    with db_session() as db:
        migrations._m19_scribe_unverified_ack_columns(db)

    with db_session() as db:
        row = db.execute(
            "SELECT unverified_ack_by, unverified_ack_at FROM scribe_notes WHERE id = ?",
            (note_id,),
        ).fetchone()
    assert row["unverified_ack_by"] == "REAL-DOC"
    assert row["unverified_ack_at"] == "2026-08-11T10:00:00+00:00"


def test_backend_seam_defaults_to_sqlite():
    assert db_backend.BACKEND_NAME == "sqlite"
    assert db_backend.PLACEHOLDER == "?"
    # Pool is ON by DEFAULT since 2026-08-01, and that default is a SAFETY
    # property, not a preference — see db_backend.POOL_ENABLED's comment. The
    # unpooled path does a real conn.close() per query, and on the SQLCipher
    # driver we ship, close holds the GIL across the WAL checkpoint; under ward
    # churn that deadlocked the appliance permanently (kill -9 the only exit).
    # An appliance booting without ORB_DB_POOL in its .env must NOT silently get
    # the dangerous path, so this asserts the default rather than tolerating it.
    # Opting out is explicit: ORB_DB_POOL=0/false/off.
    if not os.getenv("ORB_DB_POOL"):
        assert db_backend.POOL_ENABLED is True
    elif os.getenv("ORB_DB_POOL", "").strip().lower() in ("0", "false", "no", "off"):
        assert db_backend.POOL_ENABLED is False


def test_pending_applies_a_fresh_ledger():
    """If the ledger is wiped, run_pending re-applies all migrations (idempotent
    SQL means this is safe) and re-records them."""
    with db_session() as db:
        db.execute("DELETE FROM schema_migrations")
    with db_session() as db:
        ran = migrations.run_pending_migrations(db)
    assert sorted(ran) == sorted(m.version for m in migrations.MIGRATIONS)


# ── Migration 33: users.role CHECK rebuild ────────────────────────────────────

_USERS_CANONICAL_COLS = [
    "id", "name", "role", "department", "registration_number", "password_hash",
    "pin_hash", "created_at", "failed_login_attempts", "login_locked_until",
    "totp_secret", "totp_enabled", "token_version", "totp_last_step",
    "is_active", "device_pin_hash", "device_pin_set_at",
    "device_pin_failed_attempts", "device_pin_locked_until",
]


# The rebuild must behave identically on the stdlib driver and the sqlcipher3
# driver the appliance actually ships (db_backend picks it up when installed).
_REBUILD_DRIVERS = ["sqlite3"]
try:
    import sqlcipher3.dbapi2  # noqa: F401 — presence probe only

    _REBUILD_DRIVERS.append("sqlcipher3.dbapi2")
except ImportError:
    pass


@pytest.mark.parametrize("driver", _REBUILD_DRIVERS)
def test_migration_33_widens_users_role_check_on_legacy_db(tmp_path, driver):
    """The rebuild against a real pre-33 vintage: narrow CHECK plus every
    later column, an FK child pointing at users, and foreign_keys=ON like
    every Orb connection — run inside an open IMMEDIATE transaction exactly
    as run_pending_migrations holds one (the rebuild commits that transaction,
    owns its own autocommit window, then re-opens one for the runner — see
    _rebuild_users_role_check). Proves rows, column order and children
    survive, the widened CHECK admits 'cashier' and still rejects arbitrary
    strings, and a second run is a byte-identical no-op."""
    import importlib

    drv = importlib.import_module(driver)

    raw = drv.connect(str(tmp_path / "legacy.db"))
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("""CREATE TABLE users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('doctor', 'nurse', 'admin', 'patient', 'pharmacist')),
        department TEXT,
        registration_number TEXT,
        password_hash TEXT NOT NULL,
        pin_hash TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    for stmt in (
        "ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN login_locked_until REAL",
        "ALTER TABLE users ADD COLUMN totp_secret TEXT",
        "ALTER TABLE users ADD COLUMN totp_enabled INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN totp_last_step INTEGER DEFAULT -1",
        "ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1",
        "ALTER TABLE users ADD COLUMN device_pin_hash TEXT",
        "ALTER TABLE users ADD COLUMN device_pin_set_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN device_pin_failed_attempts INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN device_pin_locked_until REAL",
    ):
        raw.execute(stmt)
    raw.execute(
        "CREATE TABLE audit_like (id INTEGER PRIMARY KEY, actor TEXT REFERENCES users(id))"
    )
    raw.execute(
        "INSERT INTO users (id, name, role, password_hash, token_version, is_active) "
        "VALUES ('D1', 'Dr A', 'doctor', 'x', 7, 0)"
    )
    raw.execute("INSERT INTO audit_like (actor) VALUES ('D1')")
    raw.commit()

    raw.isolation_level = None  # mirror the runner: migration inside our BEGIN
    raw.execute("BEGIN IMMEDIATE")
    migrations._m33_users_role_cashier(raw)
    raw.execute("COMMIT")

    ddl = raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()[0]
    assert "'cashier'" in ddl, "CHECK not widened"
    cols = [r[1] for r in raw.execute("PRAGMA table_info(users)").fetchall()]
    assert cols == _USERS_CANONICAL_COLS, f"column drift after rebuild: {cols}"
    assert raw.execute(
        "SELECT name, role, token_version, is_active FROM users WHERE id='D1'"
    ).fetchone() == ("Dr A", "doctor", 7, 0), "row values not preserved"
    assert raw.execute("SELECT actor FROM audit_like").fetchone()[0] == "D1"
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # the widened CHECK admits cashier and still rejects the unknown
    raw.execute(
        "INSERT INTO users (id, name, role, password_hash) VALUES ('C1', 'Csh', 'cashier', 'x')"
    )
    try:
        raw.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES ('W1', 'W', 'wormhole', 'x')"
        )
        raise AssertionError("CHECK accepted an unknown role")
    except drv.IntegrityError:
        pass

    # idempotent: a second run leaves the stored DDL byte-identical
    raw.execute("BEGIN IMMEDIATE")
    migrations._m33_users_role_cashier(raw)
    raw.execute("COMMIT")
    ddl2 = raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()[0]
    assert ddl2 == ddl
    raw.close()


def test_migration_33_admits_cashier_on_session_db():
    """The live schema (fresh via SCHEMA or rebuilt via migration 33) takes a
    cashier row through a PLAIN insert — the exact write `INSERT OR IGNORE`
    used to swallow."""
    uid = "CSH-M33-PROOF"
    with db_session() as db:
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()[0]
        assert "'cashier'" in ddl
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES (?, 'M33 proof', 'cashier', 'x')",
            (uid,),
        )
        row = db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
        assert row and row["role"] == "cashier"
        db.execute("DELETE FROM users WHERE id = ?", (uid,))


def test_users_role_check_parity_with_storable_roles():
    """The tripwire that makes 'add a role' a loud three-step change (see
    roles.py). If any leg fails, a role exists somewhere but not everywhere —
    the drift that made cashier unstaffable for six days."""
    import permissions
    from roles import ROLE_ALIASES, STORABLE_ROLES, role_check_sql

    # 1. permission model ↔ storable set (aliases are lookup-only vocabulary)
    assert set(permissions.ROLE_PERMISSIONS) == set(STORABLE_ROLES) | set(ROLE_ALIASES)
    for alias, target in ROLE_ALIASES.items():
        assert permissions.ROLE_PERMISSIONS[alias] is permissions.ROLE_PERMISSIONS[target]

    # 2. the newest users-rebuild migration froze the current list
    assert tuple(migrations.LATEST_USERS_ROLE_CHECK) == tuple(STORABLE_ROLES), (
        "roles.STORABLE_ROLES changed without a users-rebuild migration: add a "
        "_m<next> calling _rebuild_users_role_check with the new frozen list, "
        "and reassign LATEST_USERS_ROLE_CHECK beside it"
    )

    # 3. the live table renders exactly that list
    with db_session() as db:
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()[0]
    assert f"CHECK (role IN ({role_check_sql()}))" in ddl


# ── Migration 48: the hospital-system role batch (users rebuild) ─────────────

_ROLES_33 = ("doctor", "nurse", "admin", "patient", "pharmacist", "cashier")
_ROLES_48_NEW = ("receptionist", "ward_clerk", "lab_technician", "pathologist", "phlebotomist")


def _legacy_users_db(drv, path, roles: tuple):
    """A users table in the full column shape with a narrow role CHECK, two FK
    children (one like audit trails, one like patients.attending_doctor_id),
    foreign_keys ON like every Orb connection, and a few rows."""
    raw = drv.connect(str(path))
    raw.execute("PRAGMA foreign_keys = ON")
    role_list = ", ".join(f"'{r}'" for r in roles)
    raw.execute(f"""CREATE TABLE users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ({role_list})),
        department TEXT,
        registration_number TEXT,
        password_hash TEXT NOT NULL,
        pin_hash TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        failed_login_attempts INTEGER DEFAULT 0,
        login_locked_until REAL,
        totp_secret TEXT,
        totp_enabled INTEGER DEFAULT 0,
        token_version INTEGER DEFAULT 0,
        totp_last_step INTEGER DEFAULT -1,
        is_active INTEGER DEFAULT 1,
        device_pin_hash TEXT,
        device_pin_set_at TIMESTAMP,
        device_pin_failed_attempts INTEGER DEFAULT 0,
        device_pin_locked_until REAL)""")
    raw.execute("CREATE TABLE audit_like (id INTEGER PRIMARY KEY, actor TEXT REFERENCES users(id))")
    raw.execute("CREATE TABLE patients_like (id TEXT PRIMARY KEY, "
                "attending_doctor_id TEXT REFERENCES users(id))")
    raw.execute("INSERT INTO users (id, name, role, department, password_hash, token_version, "
                "is_active, totp_enabled) VALUES ('D1', 'Dr A', 'doctor', 'Cardiology', 'x', 7, 0, 1)")
    raw.execute("INSERT INTO users (id, name, role, password_hash) VALUES ('A1', 'Adm', 'admin', 'y')")
    if "cashier" in roles:
        raw.execute("INSERT INTO users (id, name, role, password_hash) VALUES ('C1', 'Csh', 'cashier', 'z')")
    raw.execute("INSERT INTO audit_like (actor) VALUES ('D1')")
    raw.execute("INSERT INTO patients_like (id, attending_doctor_id) VALUES ('P1', 'D1')")
    raw.commit()
    return raw


@pytest.mark.parametrize("driver", _REBUILD_DRIVERS)
def test_migration_48_widens_users_role_check_on_a_post_33_db(tmp_path, driver):
    """The rebuild against the shape every live appliance has today (the
    migration 33 CHECK), inside an IMMEDIATE transaction exactly as the runner
    holds one. Rows, column order and FK children survive, the five new roles
    insert, an unknown role is still refused, the runner gets its transaction
    back, and a second run is a byte-identical no-op."""
    import importlib

    from migrations_his.m0048_users_role_batch import MIGRATION as M48

    drv = importlib.import_module(driver)
    raw = _legacy_users_db(drv, tmp_path / "post33.db", _ROLES_33)
    before = raw.execute("SELECT * FROM users ORDER BY id").fetchall()

    raw.isolation_level = None
    raw.execute("BEGIN IMMEDIATE")
    M48.apply(raw)
    assert raw.in_transaction, "the rebuild must hand the runner an open transaction back"
    raw.execute("COMMIT")

    ddl = raw.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
    for role in _ROLES_33 + _ROLES_48_NEW:
        assert f"'{role}'" in ddl, f"CHECK lost or never gained {role}"
    cols = [r[1] for r in raw.execute("PRAGMA table_info(users)").fetchall()]
    assert cols == _USERS_CANONICAL_COLS, f"column drift after rebuild: {cols}"
    assert raw.execute("SELECT * FROM users ORDER BY id").fetchall() == before, "rows changed"
    assert raw.execute("SELECT actor FROM audit_like").fetchone()[0] == "D1"
    assert raw.execute("SELECT attending_doctor_id FROM patients_like").fetchone()[0] == "D1"
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "FK enforcement not restored"

    for i, role in enumerate(_ROLES_48_NEW):
        raw.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
                    (f"N{i}", role, role))
    try:
        raw.execute("INSERT INTO users (id, name, role, password_hash) VALUES ('W1', 'W', 'storekeeper', 'x')")
        raise AssertionError("CHECK accepted an unknown role")
    except drv.IntegrityError:
        pass

    raw.execute("BEGIN IMMEDIATE")
    M48.apply(raw)
    raw.execute("COMMIT")
    assert raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0] == ddl
    raw.close()


def test_the_runner_applies_33_then_48_to_a_pre_33_db(tmp_path, monkeypatch):
    """Two FK-parent rebuilds in ONE runner call, with no transaction open at
    entry (the ops-script path): each rebuild commits and re-opens the runner's
    transaction, and both land in the ledger."""
    import sqlite3

    raw = _legacy_users_db(sqlite3, tmp_path / "pre33.db",
                           ("doctor", "nurse", "admin", "patient", "pharmacist"))
    keep = [m for m in migrations.MIGRATIONS if m.version in (33, 48)]
    assert [m.version for m in keep] == [33, 48]
    monkeypatch.setattr(migrations, "MIGRATIONS", keep)

    assert raw.in_transaction is False
    ran = migrations.run_pending_migrations(raw)
    assert ran == [33, 48]
    ledger = {r[0] for r in raw.execute("SELECT version FROM schema_migrations").fetchall()}
    assert ledger == {33, 48}
    ddl = raw.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
    assert all(f"'{r}'" in ddl for r in _ROLES_33 + _ROLES_48_NEW)
    assert raw.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrations.run_pending_migrations(raw) == []
    raw.close()


def test_migration_48_froze_the_current_role_list():
    from migrations_his.m0048_users_role_batch import FROZEN_ROLES
    from roles import STORABLE_ROLES
    assert tuple(FROZEN_ROLES) == tuple(migrations.LATEST_USERS_ROLE_CHECK) == tuple(STORABLE_ROLES)


def test_the_session_db_admits_every_new_role():
    with db_session() as db:
        for role in _ROLES_48_NEW:
            uid = f"M48-{role}"
            db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
                       (uid, role, role))
            assert db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()["role"] == role
            db.execute("DELETE FROM users WHERE id = ?", (uid,))


# ── Migration 49: the Helix tables inside the runner ─────────────────────────

_HELIX_TABLES = ("helix_medication_master", "helix_inventory_lots", "helix_patient_med_orders",
                 "helix_pharmacy_verifications", "helix_stock_movements", "helix_transitions")
_HELIX_INDEXES = {"idx_helix_orders_patient", "idx_helix_orders_status",
                  "idx_helix_transitions_order", "idx_helix_transitions_idem"}


def _shape(conn, table):
    cols = sorted(tuple(r[1:]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall())
    fks = sorted(tuple(r[2:]) for r in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall())
    return cols, fks


def test_migration_49_creates_exactly_what_helix_schema_creates(tmp_path):
    """Frozen copy versus the live fallback: same columns (name, type, not
    null, default, key) and the same foreign keys, table by table."""
    import sqlite3

    from migrations_his.m0049_helix_tables_in_runner import MIGRATION as M49
    from services.helix_db import HELIX_SCHEMA

    by_runner = sqlite3.connect(str(tmp_path / "runner.db"))
    M49.apply(by_runner)
    by_runner.commit()
    by_schema = sqlite3.connect(str(tmp_path / "schema.db"))
    by_schema.executescript(HELIX_SCHEMA)
    for table in _HELIX_TABLES:
        assert _shape(by_runner, table) == _shape(by_schema, table), table
    idx = lambda c: {r[0] for r in c.execute(  # noqa: E731
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_helix%'").fetchall()}
    assert idx(by_runner) == idx(by_schema) == _HELIX_INDEXES
    by_runner.close()
    by_schema.close()


def test_migration_49_is_idempotent(tmp_path):
    import sqlite3

    from migrations_his.m0049_helix_tables_in_runner import MIGRATION as M49

    conn = sqlite3.connect(str(tmp_path / "twice.db"))
    M49.apply(conn)
    conn.commit()
    first = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    M49.apply(conn)
    conn.commit()
    assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == first
    conn.close()


def test_migration_49_never_touches_an_existing_legacy_helix_table(tmp_path):
    """An old narrow orders table stays for init_helix_db to widen, and an
    index whose column a legacy table lacks is skipped rather than failing
    boot."""
    import sqlite3

    from migrations_his.m0049_helix_tables_in_runner import MIGRATION as M49

    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.execute("CREATE TABLE helix_patient_med_orders (id TEXT PRIMARY KEY, patient_id TEXT, "
                 "medication_id TEXT, status TEXT, prescribed_by TEXT)")
    conn.execute("CREATE TABLE helix_transitions (id TEXT PRIMARY KEY, order_id TEXT, "
                 "from_status TEXT, to_status TEXT)")
    conn.execute("INSERT INTO helix_patient_med_orders (id, status) VALUES ('O1', 'pending')")
    conn.commit()
    legacy_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='helix_patient_med_orders'").fetchone()[0]

    M49.apply(conn)
    conn.commit()

    assert conn.execute("SELECT sql FROM sqlite_master WHERE name='helix_patient_med_orders'"
                        ).fetchone()[0] == legacy_ddl
    assert conn.execute("SELECT id FROM helix_patient_med_orders").fetchall() == [("O1",)]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert set(_HELIX_TABLES) <= tables
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_helix_transitions_idem" not in idx, "legacy table has no idempotency_key"
    assert {"idx_helix_orders_patient", "idx_helix_orders_status", "idx_helix_transitions_order"} <= idx
    conn.close()


def test_a_failure_inside_migration_49_rolls_back_the_whole_run(tmp_path, monkeypatch):
    """test_migration_atomicity's pattern: 47 and 49 pending together, 49 fails
    after creating its tables, and the database is exactly as it was."""
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "atomic.db"))
    conn.execute("CREATE TABLE vitals (id INTEGER PRIMARY KEY, patient_id TEXT)")
    conn.commit()
    real = {m.version: m for m in migrations.MIGRATIONS}

    def failing_49(c):
        real[49].apply(c)
        raise RuntimeError("simulated failure after the Helix tables were created")

    monkeypatch.setattr(migrations, "MIGRATIONS", [
        real[47], migrations.Migration(49, real[49].name, failing_49)])
    assert conn.in_transaction is False
    with pytest.raises(RuntimeError):
        migrations.run_pending_migrations(conn)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert not (set(_HELIX_TABLES) & tables), "a failed run left Helix tables behind"
    assert "carried_from" not in {r[1] for r in conn.execute("PRAGMA table_info(vitals)").fetchall()}
    assert "schema_migrations" not in tables or not conn.execute(
        "SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    conn.close()


def test_the_session_db_has_the_helix_tables_and_both_migrations_ledgered():
    with db_session() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        ledger = {r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}
    assert set(_HELIX_TABLES) <= tables
    assert {48, 49} <= ledger
