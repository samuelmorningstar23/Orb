"""First boot on a brand new database.

Regression cover for a defect that took out the entire test suite: migration 35
(`ward_receipt`) issued `ALTER TABLE helix_patient_med_orders ...` while the
Helix tables did not exist yet. They are not in `database.SCHEMA`; they are
created by `services.helix_db.init_helix_db()`, which `main.py` calls AFTER
`init_db()`. So on a fresh appliance the migration ran against tables that had
not been created, `init_db()` raised `no such table: helix_patient_med_orders`,
and every pytest session died in the autouse session fixture
(`tests/conftest.py`, which calls `init_db()` on a fresh temp DB).

Nothing in the suite exercised this path. The fresh-DB call sites all reach
`init_db()` THROUGH that session fixture, so the one break they could not
survive was a break in the fixture itself: the suite could not report the bug
that stopped it from running. Hence a subprocess here, with its own empty
database, owing nothing to the fixture.

The migration is now a no-op when the tables are absent, which is correct only
because `helix_db.HELIX_SCHEMA` already creates them in their post-35 shape.
That is an assumption living in a different file from the migration that
depends on it, so it is pinned below: if someone trims those columns out of the
fresh schema, migration 35 keeps quietly no-opping and a new appliance boots
without the ward-receipt columns at all. The crash was loud. That would not be.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent


# ── Independence from the shared session database ────────────────────────────
# These three autouse fixtures in tests/conftest.py all reach for the suite's
# shared database: init_test_db calls init_db() on it, and the other two read and
# write it before and after every test. Inherit them and this module dies exactly
# where the rest of the suite dies, in fixture setup, reporting a conftest
# traceback instead of the diagnosis below. That is the failure this file exists
# to explain, so it cannot be a failure this file also suffers from. Overriding
# by name (a module-level fixture wins over a conftest one) leaves these tests
# resting on nothing but their own subprocess and tmp_path.
@pytest.fixture(scope="session", autouse=True)
def init_test_db():
    yield


@pytest.fixture(scope="module", autouse=True)
def _sweep_admitted_leftovers():
    yield


@pytest.fixture(autouse=True)
def _reseed_users():
    yield


def _boot(tmp_path, body: str, name: str = "fresh.db"):
    """Run `body` in a subprocess against a database file that does not exist yet.

    A subprocess rather than an in-process reload because `config.DB_PATH` binds
    at first import (see config.py), so pointing `init_db()` at another file
    after the suite has imported config is not reliably possible. The minimal
    environment mirrors the appliance's first boot and keeps the developer's own
    shell settings from steering the run.
    """
    db_path = tmp_path / name
    assert not db_path.exists(), "the point of this test is that the file is absent"

    proc = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(SERVER_DIR),
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "ORB_DEPLOYMENT_PROFILE": "dev",
            "ORB_DB_PATH": str(db_path),
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc, db_path


def _payload(proc):
    """Pull the one JSON line the boot script prints past the schema chatter."""
    assert proc.returncode == 0, (
        f"first boot failed (rc={proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("ORB_TEST_JSON:"):
            return json.loads(line[len("ORB_TEST_JSON:"):])
    raise AssertionError(f"boot script printed no result\nstdout:\n{proc.stdout}")


def test_init_db_succeeds_on_an_empty_database(tmp_path):
    """The regression proper: `init_db()` against a file that does not exist."""
    proc, db_path = _boot(tmp_path, "from database import init_db; init_db()")

    assert proc.returncode == 0, (
        "init_db() failed on a brand new database, which is the first thing a "
        "new appliance does and the first thing every pytest session does.\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    # Name the original failure so a re-break is recognised on sight.
    assert "no such table" not in proc.stderr, (
        "init_db() touched a table that does not exist yet on a fresh database. "
        "A migration is almost certainly altering a table that is created "
        "outside database.SCHEMA (the Helix tables come from "
        "services/helix_db.py, after init_db).\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    assert db_path.exists(), "init_db() reported success but created no database"


def test_fresh_boot_records_every_migration(tmp_path):
    """Success is not silence: the whole ledger must be applied, not skipped.

    A migration that swallowed its own failure would leave `init_db()` returning
    cleanly with work undone, which looks identical to a pass from the outside.
    """
    proc, _ = _boot(tmp_path, """
import json
from database import init_db, get_db
import migrations
init_db()
conn = get_db()
applied = sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall())
print("ORB_TEST_JSON:" + json.dumps({
    "applied": applied,
    "registered": sorted(m.version for m in migrations.MIGRATIONS),
}))
""")
    got = _payload(proc)
    missing = set(got["registered"]) - set(got["applied"])
    assert not missing, f"migrations registered but never recorded on a fresh DB: {sorted(missing)}"


def test_fresh_helix_schema_carries_what_migration_35_skips(tmp_path):
    """Pin the assumption that lets migration 35 no-op on a fresh database.

    Migration 35 adds `received_by`/`received_at` and drops the two mock-event
    columns, but only when the Helix tables already exist. On a first boot it
    returns early, so the fresh schema in services/helix_db.py is the ONLY thing
    that puts the ward-receipt columns on a new appliance. This asserts the two
    files still agree. Booting them in `main.py`'s order (init_db, then
    init_helix_db) so the sequence itself is covered too.
    """
    proc, _ = _boot(tmp_path, """
import json
from database import init_db, get_db
from services.helix_db import init_helix_db
init_db()
init_helix_db()
conn = get_db()
print("ORB_TEST_JSON:" + json.dumps({
    "orders": [r[1] for r in conn.execute("PRAGMA table_info(helix_patient_med_orders)").fetchall()],
    "movements": [r[1] for r in conn.execute("PRAGMA table_info(helix_stock_movements)").fetchall()],
}))
""")
    cols = _payload(proc)
    orders, movements = cols["orders"], cols["movements"]

    assert orders, "helix_patient_med_orders was never created on a fresh boot"
    for col in ("received_by", "received_at"):
        assert col in orders, (
            f"a fresh appliance has no helix_patient_med_orders.{col}. Migration 35 "
            "skips a fresh database on purpose, so services/helix_db.py's schema has "
            "to carry this column. Ward receipt is silently unrecordable without it."
        )
    assert "is_mock_event" not in orders, (
        "the fresh schema still creates is_mock_event. Migration 35 drops it only on "
        "existing databases, so a new appliance would keep the column that made every "
        "dispensed drug non-billable."
    )
    assert "is_mock" not in movements, (
        "the fresh schema still creates helix_stock_movements.is_mock, which defaulted "
        "to 1 and marked every real FIFO decrement as simulator output."
    )


def test_first_boot_reports_no_migration_failure(tmp_path):
    """A first boot must not cry wolf about its own schema.

    `helix_db._migrate_helix_orders` had the same missing-table trap as migration
    35, one layer down: `PRAGMA table_info` returns [] for a table that does not
    exist, so on a fresh database it read every column as absent and sent
    `ALTER TABLE ... RENAME` at a table `init_helix_db` had not created yet. That
    one is caught, so the schema still came out right and only the log was wrong,
    which is the reason it survived: every first boot printed
    `helix_patient_med_orders migration failed` at ERROR with a traceback, under
    a handler whose comment says a failed migration leaves the schema in an
    unknown state. An alarm that is wrong on every clean boot is not a smaller
    version of the bug above, it is the thing that teaches people to scroll past
    the alarm that is real.
    """
    proc, _ = _boot(tmp_path, """
from database import init_db
from services.helix_db import init_helix_db
init_db()
init_helix_db()
print("ORB_TEST_JSON:{}")
""")
    _payload(proc)  # first boot has to succeed before its logs mean anything
    noise = proc.stdout + proc.stderr

    assert "migration failed" not in noise, (
        "a clean first boot logged a migration failure. Something under "
        "init_helix_db is inspecting a Helix table before HELIX_SCHEMA creates "
        "it (PRAGMA table_info returns [] rather than raising, so the absent "
        "table reads as an outdated one).\n"
        f"--- output ---\n{noise}"
    )
    assert "Traceback" not in noise, f"a clean first boot logged a traceback\n{noise}"


def test_migration_35_still_upgrades_an_existing_helix_table(tmp_path):
    """The other branch: skipping a fresh DB must not stop it upgrading an old one.

    Driven directly against a hand-built pre-35 table, following the vintage-DB
    convention in test_migrations.py, because this shape cannot be reached by
    booting the current code.
    """
    import sqlite3

    import migrations

    raw = sqlite3.connect(str(tmp_path / "legacy.db"))
    raw.executescript("""
        CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE helix_patient_med_orders (
            id TEXT PRIMARY KEY,
            patient_id TEXT,
            medication_id TEXT,
            status TEXT,
            prescribed_by TEXT,
            is_mock_event INTEGER DEFAULT 0
        );
        CREATE TABLE helix_stock_movements (
            id INTEGER PRIMARY KEY,
            order_id TEXT,
            reason TEXT,
            is_mock INTEGER DEFAULT 1
        );
        INSERT INTO users (id, name) VALUES ('N001', 'Nurse');
        INSERT INTO helix_patient_med_orders (id, status) VALUES ('O1', 'delivered');
        INSERT INTO helix_stock_movements (id, reason) VALUES (1, 'mock_dispense');
        INSERT INTO helix_stock_movements (id, reason) VALUES (2, 'dispense');
    """)
    raw.commit()

    migrations._m35_ward_receipt(raw)
    raw.commit()

    orders = [r[1] for r in raw.execute("PRAGMA table_info(helix_patient_med_orders)").fetchall()]
    assert "received_by" in orders and "received_at" in orders, (
        "migration 35 did not add the ward-receipt columns to an existing table"
    )

    # A row delivered before the migration keeps a NULL receiver rather than
    # inheriting the dispensing pharmacist, which would forge the attestation
    # the column exists to record.
    assert raw.execute("SELECT received_by FROM helix_patient_med_orders WHERE id='O1'").fetchone()[0] is None

    # Historical dispenses are renamed, not dropped: they were real.
    reasons = sorted(r[0] for r in raw.execute("SELECT reason FROM helix_stock_movements").fetchall())
    assert reasons == ["dispense", "dispense"], f"mock_dispense rows not renamed: {reasons}"

    if sqlite3.sqlite_version_info >= (3, 35, 0):
        assert "is_mock_event" not in orders
        movements = [r[1] for r in raw.execute("PRAGMA table_info(helix_stock_movements)").fetchall()]
        assert "is_mock" not in movements

    # Re-running must not fail or double-apply.
    migrations._m35_ward_receipt(raw)
    raw.commit()
    again = [r[1] for r in raw.execute("PRAGMA table_info(helix_patient_med_orders)").fetchall()]
    assert again == orders, f"second run changed the schema: {orders} -> {again}"
    raw.close()


# ── Wave 1 of the hospital-system build: migrations 48 and 49 ────────────────

_HELIX_TABLES = ["helix_medication_master", "helix_inventory_lots", "helix_patient_med_orders",
                 "helix_pharmacy_verifications", "helix_stock_movements", "helix_transitions"]
_NEW_ROLES = ["receptionist", "ward_clerk", "lab_technician", "pathologist", "phlebotomist"]


def test_init_db_alone_creates_the_helix_tables_and_admits_the_new_roles(tmp_path):
    """Migration 49 moved the Helix tables into the runner, so init_db() alone
    (no init_helix_db) creates all six; migration 48's roles are in the fresh
    users CHECK, rendered from roles.STORABLE_ROLES, and a plain INSERT of each
    lands."""
    proc, _ = _boot(tmp_path, """
import json
from database import init_db, get_db
init_db()
conn = get_db()
tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
inserted = []
for role in %r:
    conn.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
                 ("FRESH-" + role, role, role))
    inserted.append(conn.execute("SELECT role FROM users WHERE id = ?", ("FRESH-" + role,)).fetchone()[0])
conn.commit()
ledger = sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations"))
print("ORB_TEST_JSON:" + json.dumps({"tables": tables, "ddl": ddl, "inserted": inserted,
                                     "ledger": ledger}))
""" % (_NEW_ROLES,))
    got = _payload(proc)
    missing = [t for t in _HELIX_TABLES if t not in got["tables"]]
    assert not missing, f"init_db() alone did not create {missing} (migration 49)"
    for role in _NEW_ROLES:
        assert f"'{role}'" in got["ddl"], role
    assert got["inserted"] == _NEW_ROLES
    assert {48, 49} <= set(got["ledger"])


def test_a_fresh_database_has_every_table_the_restore_verifier_checks(tmp_path):
    """The other half of test_warm_standby's AST guard, at run time.

    scripts/lib/verify_restore.py fails a backup that lacks any table in its
    manifest. Every one of them must therefore exist after init_db() alone on a
    brand new database, or the first backup of a new appliance fails its drill.
    Pinned for the Helix group in particular: migration 49 creates those six in
    the runner, so they are checked as migrated tables, not as baseline."""
    lib_dir = SERVER_DIR.parent / "scripts" / "lib"
    proc, _ = _boot(tmp_path, """
import json, sys
from database import init_db, get_db
init_db()
sys.path.insert(0, %r)
import verify_restore as vr
conn = get_db()
present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print("ORB_TEST_JSON:" + json.dumps({
    "missing": sorted(set(vr.ALL_MIGRATED_TABLES) - present),
    "helix_group": sorted(vr.MODULE_TABLES.get("helix pharmacy", ())),
}))
""" % (str(lib_dir),))
    got = _payload(proc)
    assert got["missing"] == [], (
        f"a fresh init_db() lacks tables the restore verifier requires: {got['missing']}"
    )
    assert got["helix_group"] == sorted(_HELIX_TABLES)


def test_runner_created_helix_tables_match_helix_schema(tmp_path):
    """A fresh appliance's Helix tables (made by migration 49, then every later
    migration) agree with HELIX_SCHEMA, the fallback init_helix_db still runs:
    same columns and foreign keys per table, compared by name so a column a
    later migration appends still matches."""
    import sqlite3

    proc, _ = _boot(tmp_path, """
import json
from database import init_db, get_db
init_db()
conn = get_db()
out = {}
for t in %r:
    cols = sorted(list(r)[1:] for r in conn.execute("PRAGMA table_info(%%s)" %% t).fetchall())
    fks = sorted(list(r)[2:] for r in conn.execute("PRAGMA foreign_key_list(%%s)" %% t).fetchall())
    out[t] = [cols, fks]
print("ORB_TEST_JSON:" + json.dumps(out))
""" % (_HELIX_TABLES,))
    runner = _payload(proc)

    from services.helix_db import HELIX_SCHEMA
    ref = sqlite3.connect(str(tmp_path / "reference.db"))
    ref.executescript(HELIX_SCHEMA)
    for t in _HELIX_TABLES:
        cols = sorted(list(r)[1:] for r in ref.execute(f"PRAGMA table_info({t})").fetchall())
        fks = sorted(list(r)[2:] for r in ref.execute(f"PRAGMA foreign_key_list({t})").fetchall())
        assert runner[t] == json.loads(json.dumps([cols, fks])), (
            f"{t}: the migration-created table and HELIX_SCHEMA disagree. A Helix column "
            "added by a migration must be added to HELIX_SCHEMA too, and the reverse.")
    ref.close()


def test_a_fresh_boot_no_longer_migrates_a_stock_table_that_is_not_there(tmp_path):
    """W027: init_helix_db logged "Migrating helix_stock_movements" on every
    first boot, against a table that did not exist yet. Logging at INFO so the
    line would show if it came back."""
    proc, _ = _boot(tmp_path, """
import logging
logging.basicConfig(level=logging.INFO)
from database import init_db
from services.helix_db import init_helix_db
init_db()
init_helix_db()
print("ORB_TEST_JSON:{}")
""")
    _payload(proc)
    noise = proc.stdout + proc.stderr
    assert "Migrating helix_stock_movements" not in noise, noise
    assert "migration failed" not in noise


def test_the_stock_table_guard_still_upgrades_an_old_table(tmp_path, caplog):
    """The early return is for an ABSENT table only: an old table without
    medication_id is still widened."""
    import logging
    import sqlite3

    from services.helix_db import _migrate_stock_movements

    empty = sqlite3.connect(str(tmp_path / "empty.db"))
    with caplog.at_level(logging.INFO, logger="orb.helix_db"):
        _migrate_stock_movements(empty)
    assert "Migrating helix_stock_movements" not in caplog.text
    assert empty.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    empty.close()

    old = sqlite3.connect(str(tmp_path / "old.db"))
    old.execute("CREATE TABLE helix_stock_movements (id TEXT PRIMARY KEY, lot_id TEXT, "
                "quantity_change INTEGER)")
    old.commit()
    with caplog.at_level(logging.INFO, logger="orb.helix_db"):
        _migrate_stock_movements(old)
    cols = {r[1] for r in old.execute("PRAGMA table_info(helix_stock_movements)").fetchall()}
    assert {"medication_id", "order_id"} <= cols
    assert "Migrating helix_stock_movements" in caplog.text
    old.close()


def test_migration_48_rebuilds_users_on_a_database_that_predates_it(tmp_path):
    """The deploy path on a live appliance, end to end in a subprocess: a
    database whose users CHECK stops at 'cashier' (the migration 33 shape),
    with users that FK children point at (a patient's attending doctor, a Helix
    order's prescriber), boots through init_db() and init_helix_db(). Migration
    48 rebuilds users: every row and child survives, the foreign key check is
    clean, the five roles insert, and both migrations are ledgered."""
    proc, _ = _boot(tmp_path, """
import json
import database
from database import init_db, get_db
from services.helix_db import init_helix_db

init_db()
init_helix_db()
conn = get_db()
conn.execute("INSERT INTO users (id, name, role, department, password_hash, token_version) "
             "VALUES ('OLD-D1', 'Dr Old', 'doctor', 'Cardiology', 'h', 3)")
conn.execute("INSERT INTO users (id, name, role, password_hash) VALUES ('OLD-C1', 'Csh', 'cashier', 'h')")
conn.execute("INSERT INTO patients (id, name, mrn, attending_doctor_id) VALUES ('OLD-P1', 'Pt', 'MRN-OLD-1', 'OLD-D1')")
conn.execute("INSERT INTO helix_patient_med_orders (id, patient_id, prescribed_by, status) "
             "VALUES ('OLD-O1', 'OLD-P1', 'OLD-D1', 'pending')")
conn.commit()

# Narrow the CHECK back to the migration 33 list, the shape every live
# appliance has today, through the same atomic rebuild helper.
ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
start = ddl.index("CHECK (role IN (")
end = ddl.index("))", start) + 2
narrow = (ddl[:start] + "CHECK (role IN ('doctor', 'nurse', 'admin', 'patient', 'pharmacist', 'cashier'))"
          + ddl[end:]).replace("CREATE TABLE users", "CREATE TABLE users_new", 1)
cols = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall())
database._atomic_table_rebuild(conn, table="users", create_new_sql=narrow, shared_cols=cols, post_sql=[])
conn.execute("DELETE FROM schema_migrations WHERE version IN (48, 49)")
conn.commit()
before_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
before_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

init_db()
init_helix_db()
conn = get_db()
after_ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
inserted = []
for role in %r:
    conn.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
                 ("NEW-" + role, role, role))
    inserted.append(role)
conn.commit()
print("ORB_TEST_JSON:" + json.dumps({
    "before_ddl": before_ddl,
    "after_ddl": after_ddl,
    "before_users": before_users,
    "after_users": conn.execute("SELECT COUNT(*) FROM users WHERE id NOT LIKE 'NEW-%%'").fetchone()[0],
    "doctor": list(conn.execute("SELECT name, role, department, token_version FROM users WHERE id='OLD-D1'").fetchone()),
    "patient_doctor": conn.execute("SELECT attending_doctor_id FROM patients WHERE id='OLD-P1'").fetchone()[0],
    "order_prescriber": conn.execute("SELECT prescribed_by FROM helix_patient_med_orders WHERE id='OLD-O1'").fetchone()[0],
    "fk_check": [list(r) for r in conn.execute("PRAGMA foreign_key_check").fetchall()],
    "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
    "ledger": sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations")),
    "inserted": inserted,
}))
""" % (_NEW_ROLES,))
    got = _payload(proc)
    assert "'ward_clerk'" not in got["before_ddl"], "precondition: the CHECK was narrowed"
    for role in _NEW_ROLES:
        assert f"'{role}'" in got["after_ddl"], role
    assert got["after_users"] == got["before_users"], "users rows lost in the rebuild"
    assert got["doctor"] == ["Dr Old", "doctor", "Cardiology", 3]
    assert got["patient_doctor"] == "OLD-D1"
    assert got["order_prescriber"] == "OLD-D1"
    assert got["fk_check"] == []
    assert got["integrity"] == "ok"
    assert {48, 49} <= set(got["ledger"])
    assert got["inserted"] == _NEW_ROLES


# ── Wave 2: migration 54, the pharmacy store schema ──────────────────────────

def test_a_fresh_database_has_the_pharmacy_store_schema(tmp_path):
    """init_db() alone on a brand new database runs migration 54 after 49 has
    made the Helix tables: the twelve pharmacy tables, the NDPS register's two
    triggers, the new Helix and procurement columns and the seven pharmacy
    document series all exist, and an empty drug ledger needs no backfill.
    Then init_helix_db runs over it (main.py's order) without complaint, which
    is what HELIX_SCHEMA carrying the same columns guarantees."""
    proc, _ = _boot(tmp_path, """
import json
from database import init_db, get_db
from services.helix_db import init_helix_db
from migrations_his.m0054_pharmacy_store_schema import TABLE_NAMES
init_db()
init_helix_db()
conn = get_db()
cols = lambda t: [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t).fetchall()]
print("ORB_TEST_JSON:" + json.dumps({
    "missing_tables": [t for t in TABLE_NAMES if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()],
    "triggers": sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='ndps_register'")),
    "lots": cols("helix_inventory_lots"),
    "movements": cols("helix_stock_movements"),
    "master": cols("helix_medication_master"),
    "po": cols("purchase_orders"),
    "stores": cols("stores"),
    "series": sorted(r[0] for r in conn.execute(
        "SELECT series_code FROM document_number_series WHERE series_code LIKE 'PH%'")),
    "ledger": sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations")),
    "movement_rows": conn.execute("SELECT COUNT(*) FROM helix_stock_movements").fetchone()[0],
}))
""")
    got = _payload(proc)
    assert got["missing_tables"] == []
    assert got["triggers"] == ["trg_ndps_register_no_delete", "trg_ndps_register_no_update"]
    assert {"store_id", "status", "receipt_line_ref", "unit_cost_paise"} <= set(got["lots"])
    assert {"seq", "occurred_at", "movement_type", "balance_after_units"} <= set(got["movements"])
    assert {"drug_schedule", "ndps_category", "identity_status", "is_active"} <= set(got["master"])
    assert "domain" in got["po"] and "pharmacy_kind" in got["stores"]
    assert got["series"] == ["PHCNT", "PHGRN", "PHIND", "PHPO", "PHRET", "PHTRF", "PHVR"]
    assert 54 in got["ledger"]
    assert got["movement_rows"] == 0
    noise = proc.stdout + proc.stderr
    assert "migration failed" not in noise and "Traceback" not in noise
