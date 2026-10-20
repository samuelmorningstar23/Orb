"""Migration 52: patients.status admits 'registered' (opd-2).

'registered' means a person is on the patient index and has never been
admitted: the registration desk and OPD create such rows (later increments,
behind the registration feature, which is off by default). A discharged
patient seen again in OPD stays 'discharged'. The ADT epic adds no patients
status (presence and discharge type are admissions columns), so this is the
one patients rebuild the hospital-system build needs.

SQLite cannot ALTER a CHECK constraint, so this is a table rebuild, and
patients is the FK parent of about 35 tables. That makes it the same rebuild as
the users role batch (migration 48): it goes through
database._atomic_table_rebuild, which owns an autocommit window (PRAGMA
foreign_keys and legacy_alter_table are no-ops inside a transaction) and makes
the rebuild itself all-or-nothing. migrations._rebuild_users_role_check
explains why no in-transaction shape works for an FK parent. The cost, stated
the same way: entering that window COMMITs the runner's pending work, so
migrations 50 and 51 land before this one when they run in the same batch.
That is safe because every migration is idempotent: a crash after this
rebuild commits and before its ledger row is written re-enters here at the
next boot, sees 'registered' in the stored CHECK, and does nothing.

FROZEN. The column list and the CHECK's status list are literals, never read
from database.SCHEMA: a shipped migration must build the same table forever.
tests/test_patients_registered_status_migration.py pins database.SCHEMA, a
fresh database and this file to one shape.

GUARDS, all checked before anything changes:
  * a live column this list does not know aborts the rebuild (it would be
    dropped);
  * an index or trigger on patients other than the two migration 15 indexes
    aborts it (it would be dropped with the old table);
  * the copy's row count must equal the source's (database.
    _atomic_table_rebuild), and, inside the rebuild's own transaction, no row
    in any table that references patients may point at a missing patient
    unless it already did before the rebuild. Either failure rolls the whole
    rebuild back and leaves patients untouched.

DEPLOYING IT on a database that already holds patients: take a verified
scripts/backup_db.py backup first, and dry-run init_db() on a copy of that
backup on the isolated verify stack before the real database (docs/
HIS_BUILD_PLAN_2026-10.md, "The two FK-parent rebuilds").
"""

import re

from migrations import Migration

FROZEN_STATUSES: tuple = ("admitted", "discharged", "transferred", "registered")

#: Every patients column, in the order a fresh database has them: the base
#: database.SCHEMA columns, then init_db's on_scale_2, then migration 15's three
#: import provenance columns. Migration 50 adds none (patient_demographics
#: holds the registration fields instead).
CANONICAL_COLUMNS: tuple = (
    "id",
    "user_id",
    "name",
    "mrn",
    "bed",
    "ward",
    "admission_date",
    "diagnosis",
    "status",
    "age",
    "date_of_birth",
    "sex",
    "phone",
    "emergency_contact_name",
    "emergency_contact_phone",
    "address",
    "attending_doctor_id",
    "created_at",
    "on_scale_2",
    "import_run_id",
    "import_source_system",
    "import_external_id",
)

#: The two indexes migration 15 put on patients. Dropped with the old table
#: and recreated by post_sql, exactly as migration 15 wrote them.
KNOWN_INDEXES: dict = {
    "idx_patients_import_external": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_patients_import_external "
        'ON "patients"(import_source_system, import_external_id) '
        "WHERE import_external_id IS NOT NULL"
    ),
    "idx_patients_import_run": (
        'CREATE INDEX IF NOT EXISTS idx_patients_import_run ON "patients"(import_run_id)'
    ),
}

_STATUS_CHECK = re.compile(r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)", re.I)


def stored_statuses(conn) -> tuple | None:
    """The statuses the live patients CHECK allows, or None when there is no
    status CHECK at all."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='patients'"
    ).fetchone()
    ddl = (row[0] or "") if row else ""
    match = _STATUS_CHECK.search(ddl)
    if match is None:
        return None
    return tuple(re.findall(r"'([^']*)'", match.group(1)))


def _patient_fk_violations(conn) -> set:
    """Rows in any table with a declared FOREIGN KEY to patients that point at
    a patient id that does not exist, as (table, rowid, fk index). Read with
    PRAGMA foreign_key_check per child table, which works inside a transaction
    and whatever PRAGMA foreign_keys is set to."""
    children = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        try:
            fks = conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
        except Exception:  # noqa: BLE001 - a virtual table whose module is not loaded
            continue
        if any(r[2] == "patients" for r in fks):
            children.append(name)
    found = set()
    for child in children:
        for r in conn.execute(f'PRAGMA foreign_key_check("{child}")').fetchall():
            if r[2] == "patients":
                found.add((r[0], r[1], r[3]))
    return found


def apply(conn) -> None:
    statuses = stored_statuses(conn)
    if statuses is None or set(FROZEN_STATUSES) <= set(statuses):
        return  # already widened (or nothing to widen): the re-entry no-op

    # Anything hanging off the table drops with it. Fail loudly on anything
    # this migration does not know how to carry across.
    assoc = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE tbl_name='patients' "
        "AND type IN ('index', 'trigger') AND name NOT LIKE 'sqlite_autoindex%'"
    ).fetchall()
    strangers = [(r[0], r[1]) for r in assoc if r[0] not in KNOWN_INDEXES]
    if strangers:
        raise RuntimeError(
            "patients rebuild would drop associated objects it does not know how "
            f"to preserve: {strangers}"
        )
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(patients)").fetchall()]
    unknown = [c for c in old_cols if c not in CANONICAL_COLUMNS]
    if unknown:
        raise RuntimeError(f"patients rebuild would drop live column(s) {unknown}")
    shared = ", ".join(c for c in CANONICAL_COLUMNS if c in old_cols)
    status_list = ", ".join(f"'{s}'" for s in FROZEN_STATUSES)
    present_indexes = {r[0] for r in assoc}
    # Recreate the indexes the table had; a database that somehow lacks one is
    # left as it was, so this rebuild changes nothing but the CHECK.
    post_sql = [sql for name, sql in KNOWN_INDEXES.items() if name in present_indexes]

    violations_before = _patient_fk_violations(conn)

    def _verify(rebuilt) -> None:
        widened = stored_statuses(rebuilt)
        if widened is None or set(widened) != set(FROZEN_STATUSES):
            raise RuntimeError(f"patients rebuild produced the status list {widened}")
        new = _patient_fk_violations(rebuilt) - violations_before
        if new:
            raise RuntimeError(
                f"patients rebuild aborted: rows would point at missing patients {sorted(new)[:10]}"
            )

    was_in_txn = bool(getattr(conn, "in_transaction", False))
    from database import _atomic_table_rebuild  # lazy: mirrors database→migrations

    _atomic_table_rebuild(
        conn,
        table="patients",
        shared_cols=shared,
        create_new_sql=f"""CREATE TABLE patients_new (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id),
            name TEXT NOT NULL,
            mrn TEXT UNIQUE NOT NULL,
            bed TEXT,
            ward TEXT,
            admission_date TIMESTAMP,
            diagnosis TEXT,
            status TEXT DEFAULT 'admitted' CHECK (status IN ({status_list})),
            age INTEGER,
            date_of_birth DATE,
            sex TEXT,
            phone TEXT,
            emergency_contact_name TEXT,
            emergency_contact_phone TEXT,
            address TEXT,
            attending_doctor_id TEXT REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            on_scale_2 INTEGER DEFAULT 0,
            import_run_id TEXT,
            import_source_system TEXT,
            import_external_id TEXT
        )""",
        post_sql=post_sql,
        before_commit=_verify,
    )
    # Restore the runner's world: it handed us an open transaction and its
    # ledger INSERT and final COMMIT assume one is still there.
    if was_in_txn and not getattr(conn, "in_transaction", False):
        conn.execute("BEGIN IMMEDIATE")


MIGRATION = Migration(52, "patients_status_registered", apply)
