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
