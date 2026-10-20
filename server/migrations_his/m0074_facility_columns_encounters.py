"""Migration 74: encounters and wards carry the facility (multisite-4, W11-A).

Samuel ruled on 3 October 2026 (multi-site M1) that a hospital group runs one
Orb appliance per legal entity with a row-level facility id. Migration 70 put
the id on patients and admissions. This one puts it on the other places a
patient meets a facility, and on the two places the ADT and lab epics left a
facility label for this epic to resolve:

  * opd_visits, appointments and surgeries: the outpatient visit, the booking
    and the operation each happen at one facility;
  * roster_import_runs and history_import_runs: the facility an import was
    run at;
  * wards: adt-1 (migration 53) gave every ward a facility_code label, 'MAIN'
    until a facilities table existed. The label is mapped here onto the
    facility that holds that code, so 'MAIN' becomes facility 1;
  * lab_specimens: lis-3 (migration 55) gave every specimen a nullable
    site_id, the lab's site hook. A site_id that names a facility code is
    mapped onto that facility; nothing writes site_id today, so every
    specimen reads facility 1.

Every column is facility_id INTEGER NOT NULL DEFAULT 1, so every existing row
reads facility 1 and a one-facility appliance (every install today) runs
exactly as before. Writers stamp the facility themselves while more than one
facility is on record (services/frontdesk_service, routes/appointments_routes,
services/surgery_service, services/roster_import,
services/history_import_service); with one facility the default stands.

WHAT THIS FILE DOES NOT DO
  * No index is rebuilt. idx_opd_token_unique (migration 50) stays (visit_date,
    department, doctor_id, token_number): under ruling MS-R4 (a) a department
    name is unique across the group and owned by one facility, so the
    department already implies the facility. It needs facility_id only if
    MS-R4 is ever ruled (b).
  * No new index. The patient-keyed reads of the facility go through the
    existing per-patient indexes (idx_opd_patient, idx_appointments_patient,
    idx_admissions_patient_status); the document-keyed queue reads that would
    want (facility_id, visit_date) belong to the front desk sweep.
  * A ward label or a specimen site_id that names no facility code is left at
    facility 1. adt-1 writes only 'MAIN' and nothing writes site_id, so no row
    is in that state; ward and specimen writers stamp the facility in the
    sweeps that open those screens to a second facility.

RULES THIS FILE KEEPS (the ones migration 70 keeps)
  * No REFERENCES: SQLite refuses ADD COLUMN ... REFERENCES with a non-NULL
    default, and administrative tables carry no foreign keys.
  * No users column, no triggers, no table rebuild. Every ALTER is ADD COLUMN
    with a constant default, which SQLite applies without rewriting the table.
  * ALTER only where the table exists and lacks the column, so a database
    without one of the module tables migrates, and a second run changes
    nothing. One statement per conn.execute, never executescript.
  * Nothing outside the migration runner is imported at load time: the
    restore verifier loads this package by path with server/ off sys.path.

A NOTE FOR ANY FUTURE REBUILD of opd_visits, appointments, surgeries, wards,
lab_specimens, roster_import_runs or history_import_runs: the new table must
carry facility_id INTEGER NOT NULL DEFAULT 1 (FACILITY_COLUMNS below).
services/frontdesk_service.ensure_frontdesk_schema adds the opd_visits column
on its lazy path too (ensure_opd_visit_facility_on), so the fallback and the
runner build one shape.
"""

from __future__ import annotations

from typing import Any

from migrations import Migration

#: The declaration every column below carries.
FACILITY_DECLARATION = "INTEGER NOT NULL DEFAULT 1"

#: (table, column) this migration adds, in the order it adds them.
FACILITY_COLUMNS: tuple = (
    ("opd_visits", "facility_id"),
    ("appointments", "facility_id"),
    ("surgeries", "facility_id"),
    ("roster_import_runs", "facility_id"),
    ("history_import_runs", "facility_id"),
    ("wards", "facility_id"),
    ("lab_specimens", "facility_id"),
)

TABLES: tuple = tuple(table for table, _ in FACILITY_COLUMNS)

#: The encounter tables permissions.FACILITY_PATIENT_SQL reads the facility of.
ENCOUNTER_TABLES: tuple = ("opd_visits", "appointments")

#: (table, label column): a facility label written before the facilities
#: table existed, mapped onto the facility holding that code (ignoring case
#: and outer spaces).
MAPPED_LABELS: tuple = (
    ("wards", "facility_code"),
    ("lab_specimens", "site_id"),
)


def _columns(conn: Any, table: str) -> set:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _add_column_on(conn: Any, table: str, column: str) -> bool:
    """ADD COLUMN when the table exists and lacks it. True when added."""
    cols = _columns(conn, table)
    if not cols or column in cols:
        return False
    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {FACILITY_DECLARATION}')
    return True


def ensure_opd_visit_facility_on(conn: Any) -> bool:
    """opd_visits.facility_id, for the front desk's lazy path
    (frontdesk_service.ensure_frontdesk_schema runs it right after migration
    50's ensure_opd_visits_on). Idempotent."""
    return _add_column_on(conn, "opd_visits", "facility_id")


def add_facility_columns(conn: Any) -> list[str]:
    """Every ADD COLUMN above that is needed. Returns 'table.column' added."""
    return [
        f"{table}.{column}"
        for table, column in FACILITY_COLUMNS
        if _add_column_on(conn, table, column)
    ]


def map_labels(conn: Any) -> dict[str, int]:
    """Point each labelled row at the facility its label names. Returns
    {table: rows changed}. A label that names no facility is left alone, and
    a row already pointing at the right facility is not touched, so a second
    run changes nothing."""
    changed: dict[str, int] = {}
    if not _columns(conn, "facilities"):
        return changed
    for table, label in MAPPED_LABELS:
        cols = _columns(conn, table)
        if label not in cols or "facility_id" not in cols:
            continue
        match = (
            f'(SELECT f.id FROM facilities f WHERE f.code = TRIM("{table}".{label}) COLLATE NOCASE)'
        )
        cur = conn.execute(
            f'UPDATE "{table}" SET facility_id = {match} '
            f"WHERE {label} IS NOT NULL AND TRIM({label}) != '' "
            f"AND {match} IS NOT NULL AND facility_id IS NOT {match}"
        )
        changed[table] = max(0, cur.rowcount or 0)
    return changed


def apply(conn: Any) -> None:
    add_facility_columns(conn)
    map_labels(conn)


MIGRATION = Migration(74, "facility_columns_encounters", apply)
