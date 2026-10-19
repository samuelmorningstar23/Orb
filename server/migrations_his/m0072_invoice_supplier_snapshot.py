"""Migration 72: the supplier identity a numbered bill or receipt was issued with
(multisite-2, W9-E, 10 October 2026).

A GST invoice must never change after issue. Until now the supplier block (the
hospital's name, address and GSTIN) was built at print time from whatever the
site settings said that day, so once facility identity became editable
(PATCH /api/facilities/{id}) a reprint of last month's invoice would have shown
this month's details. This migration gives invoices and payments (receipts)
the columns that hold the supplier block as it was when the document's number
was assigned:

  supplier_facility_id   the facility that issued it
  supplier_legal_name    the name printed as the supplier
  supplier_address       the registered address, one line
  supplier_gstin         the GSTIN
  supplier_state_code    the two-digit GST state code
  supplier_snapshot_at   when the snapshot was taken (UTC); NULL means the
                         document was numbered before this migration and
                         prints the old block exactly as it always has

The writers are services/billing_service.py (issue and credit note) and
services/payments_service.py (every receipt), through
services/facility_identity.supplier_snapshot. They write the snapshot in the
same statement that assigns the number: the UPDATE that issues an invoice, and
the INSERT of a credit note or a receipt. Drafts carry no snapshot and print
the live identity.

Two triggers keep the snapshot written once:
  * invoices: once a row has a number or a snapshot, no UPDATE may change any
    supplier column. A document numbered before this migration therefore
    keeps its NULL snapshot (and its legacy print) for good: filling one in
    later would change what a reprint says.
  * payments: a receipt is numbered at INSERT, so no UPDATE may change any
    supplier column at all.
DELETE is not refused (the patient erase plan keeps working).

The rules every migration here follows: one statement per conn.execute (never
executescript, which commits the runner's transaction), each ADD COLUMN only
when its table exists and lacks it, every column nullable with no default (an
O(1) ALTER that rewrites nothing), no REFERENCES (migrations 20 and 23 forbid
foreign keys from administrative tables), and a trigger is created only when
the table has every column it names, so an unusual table of the same name
cannot stop boot.

A NOTE FOR ANY FUTURE REBUILD OF invoices OR payments: carry these six columns
and recreate the triggers in TRIGGERS below.
"""
from migrations import Migration

#: The snapshot columns, in the order the writers bind them.
SNAPSHOT_COLUMNS = (
    "supplier_facility_id",
    "supplier_legal_name",
    "supplier_address",
    "supplier_gstin",
    "supplier_state_code",
    "supplier_snapshot_at",
)

_DECLARATIONS = {
    "supplier_facility_id": "INTEGER",
    "supplier_legal_name": "TEXT",
    "supplier_address": "TEXT",
    "supplier_gstin": "TEXT",
    "supplier_state_code": "TEXT",
    "supplier_snapshot_at": "TEXT",
}

TABLES_WITH_SNAPSHOT = ("invoices", "payments")

_ANY_CHANGE = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in SNAPSHOT_COLUMNS)

#: A numbered invoice keeps the supplier block it was issued with.
INVOICES_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS trg_invoices_supplier_snapshot_fixed "
    "BEFORE UPDATE ON invoices "
    "WHEN (OLD.invoice_number IS NOT NULL OR OLD.supplier_snapshot_at IS NOT NULL) "
    f"AND ({_ANY_CHANGE}) "
    "BEGIN SELECT RAISE(ABORT, 'a numbered invoice keeps the supplier details it was "
    "issued with'); END"
)

#: A receipt is numbered when it is written, so its supplier block never changes.
PAYMENTS_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS trg_payments_supplier_snapshot_fixed "
    "BEFORE UPDATE ON payments "
    f"WHEN {_ANY_CHANGE} "
    "BEGIN SELECT RAISE(ABORT, 'a receipt keeps the supplier details it was "
    "issued with'); END"
)

#: (table, trigger name, DDL, every column the trigger names)
TRIGGERS = (
    ("invoices", "trg_invoices_supplier_snapshot_fixed", INVOICES_TRIGGER,
     SNAPSHOT_COLUMNS + ("invoice_number",)),
    ("payments", "trg_payments_supplier_snapshot_fixed", PAYMENTS_TRIGGER,
     SNAPSHOT_COLUMNS),
)

TRIGGER_NAMES = frozenset(entry[1] for entry in TRIGGERS)


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_snapshot_columns(conn) -> list[str]:
    """Each ADD COLUMN only when its table exists and lacks it. Returns
    'table.column' for every column added."""
    added = []
    for table in TABLES_WITH_SNAPSHOT:
        present = _columns(conn, table)
        if not present:
            continue
        for column in SNAPSHOT_COLUMNS:
            if column in present:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {_DECLARATIONS[column]}")
            added.append(f"{table}.{column}")
    return added


def apply(conn) -> None:
    add_snapshot_columns(conn)
    for table, _name, ddl, needs in TRIGGERS:
        if set(needs) <= _columns(conn, table):
            conn.execute(ddl)


MIGRATION = Migration(72, "invoice_supplier_snapshot", apply)
