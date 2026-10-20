"""Migration 75: money, stores and assets carry the facility (multisite-7b,
W11-C, 20 October 2026).

Samuel ruled on 3 October 2026 (multi-site M1) that a hospital group runs one
Orb appliance per legal entity, with a row-level facility id in the one
encrypted database. Migration 70 put the facility on admissions and patients.
This migration puts it on the administrative documents and physical resources
the design files at facility level, so the scope sweeps that follow
(multisite-9a, 9b, 12 and 13) can key money, stores and assets on the row's
own facility rather than guess it from the patient.

WHAT IT ADDS
  * facility_id INTEGER NOT NULL DEFAULT 1 on FACILITY_TABLES: invoices,
    payments, refunds, claims, patient_deposits, cash_counter_sessions,
    encounter_charges, stores, indents, purchase_orders, goods_receipts,
    housekeeping_units, biomedical_assets, kitchen_indents, nabh_incidents,
    nabh_evidence_exports, abdm_care_contexts, enrolled_devices,
    helix_inventory_lots and helix_stock_movements. Every existing row reads
    facility 1, which is where every existing row is: a second facility cannot
    be created through the product before this lands (the create gate), so no
    backfill is needed or attempted.
  * facility_id INTEGER, nullable, on GROUP_TABLES: tariff_plans,
    hospital_forecasts and hospital_holidays. NULL means the whole group,
    which is what every existing row is. Per-facility price lists are
    multisite-13; nothing here reads the column yet.
  * The one-active-dispensing-store index (migration 54, GRN-R17) becomes one
    per facility, as migration 54's own comment said the multi-site epic
    would: ux_stores_one_active_dispensing keeps its name and its predicate
    and gains facility_id ahead of pharmacy_kind. With one facility on record
    it refuses exactly what it refused before (every store is facility 1), and
    the store master's friendly refusal still finds 'pharmacy_kind' in the
    constraint's words.

THE LAZY SCHEMA OWNERS. services/helix_db.HELIX_SCHEMA declares the column on
the two Helix tables (tests/test_fresh_database_init.py compares the shapes),
and services/housekeeping_service.ensure_housekeeping_schema calls
add_facility_columns below for housekeeping_units after creating it, so a
table either path makes has the same shape the runner gives it.

THE WRITERS (same wave): billing_service, payments_service, payer_service,
procurement_service, housekeeping_service, biomedical_service, nabh_service,
abdm_service and dietary_service stamp the facility a document belongs to,
through services/facility_stamp.py. With one facility on record each of them
writes 1 without a new statement. enrolled_devices (routes/auth_routes.py),
the Helix lots and movements (services/pharmacy_stock_service.py), the OPD
charge writer (services/opd_charges_service.py), forecasts and holidays keep
the column's default until their own owners stamp them.

RULES THIS FILE KEEPS
  * One statement per conn.execute, never executescript (it would commit the
    runner's transaction).
  * Each ADD COLUMN only when its table exists and lacks the column, with a
    constant default (or none), so SQLite applies it without rewriting the
    table. No REFERENCES (migrations 20 and 23 forbid foreign keys from
    administrative tables) and no CHECK (the Helix tables are foreign-key
    parents and can never be rebuilt; facility ids are checked in the
    services and reported by the operations page).
  * The index swap runs only when stores has the three columns it names and
    the index is still in migration 54's shape. Idempotent throughout: a
    second run changes nothing.

A NOTE FOR ANY FUTURE REBUILD of a table listed here: carry facility_id with
its declaration below, and for stores the index in DISPENSING_INDEX_DDL.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from migrations import Migration

COLUMN = "facility_id"

#: Facility-level rows: every existing row is facility 1.
FACILITY_DECLARATION = "INTEGER NOT NULL DEFAULT 1"

#: Group-level rows that may later be narrowed to one facility: NULL is the group.
GROUP_DECLARATION = "INTEGER"

FACILITY_TABLES = (
    "invoices",
    "payments",
    "refunds",
    "claims",
    "patient_deposits",
    "cash_counter_sessions",
    "encounter_charges",
    "stores",
    "indents",
    "purchase_orders",
    "goods_receipts",
    "housekeeping_units",
    "biomedical_assets",
    "kitchen_indents",
    "nabh_incidents",
    "nabh_evidence_exports",
    "abdm_care_contexts",
    "enrolled_devices",
    "helix_inventory_lots",
    "helix_stock_movements",
)

#: The two Helix tables, whose DDL services/helix_db.HELIX_SCHEMA also carries.
HELIX_TABLES = ("helix_inventory_lots", "helix_stock_movements")

GROUP_TABLES = (
    "tariff_plans",
    "hospital_forecasts",
    "hospital_holidays",
)

#: (table, column, declaration) for every column this migration adds.
FACILITY_COLUMNS = tuple(
    [(table, COLUMN, FACILITY_DECLARATION) for table in FACILITY_TABLES]
    + [(table, COLUMN, GROUP_DECLARATION) for table in GROUP_TABLES]
)

DISPENSING_INDEX = "ux_stores_one_active_dispensing"
DISPENSING_PREDICATE = "pharmacy_kind = 'dispensing' AND is_active = 1"
DISPENSING_INDEX_DDL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {DISPENSING_INDEX} "
    f"ON stores(facility_id, pharmacy_kind) WHERE {DISPENSING_PREDICATE}"
)


def _columns(conn: Any, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_facility_columns(conn: Any, tables: Iterable[str] | None = None) -> list[str]:
    """Each ADD COLUMN only when its table exists and lacks it; `tables`
    narrows the list (the housekeeping lazy path passes its one table).
    Returns 'table.column' for every column added."""
    wanted = None if tables is None else set(tables)
    added: list[str] = []
    for table, column, declaration in FACILITY_COLUMNS:
        if wanted is not None and table not in wanted:
            continue
        present = _columns(conn, table)
        if not present or column in present:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        added.append(f"{table}.{column}")
    return added


def _index_columns(conn: Any, name: str) -> list[str]:
    return [r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall()]


def one_dispensing_store_per_facility(conn: Any) -> bool:
    """Move the one-active-dispensing-store rule from the site to the facility.
    True when the index was (re)created here, False when there was nothing to
    do (no stores table, a column missing, or already per facility)."""
    if not {"facility_id", "pharmacy_kind", "is_active"} <= _columns(conn, "stores"):
        return False
    exists = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (DISPENSING_INDEX,)
        ).fetchone()
        is not None
    )
    if exists and _index_columns(conn, DISPENSING_INDEX) == ["facility_id", "pharmacy_kind"]:
        return False
    if exists:
        conn.execute(f"DROP INDEX {DISPENSING_INDEX}")
    conn.execute(DISPENSING_INDEX_DDL)
    return True


def apply(conn: Any) -> None:
    add_facility_columns(conn)
    one_dispensing_store_per_facility(conn)


MIGRATION = Migration(75, "facility_columns_admin", apply)
