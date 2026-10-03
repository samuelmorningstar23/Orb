"""A reseed must not leave rows pointing at parents it just deleted.

`scripts/reset_db_and_seed.py` derives its wipe set from the live schema instead
of a hand-maintained constant, precisely because the constant went stale and left
orphans. That derivation had one blind spot: it followed only DECLARED foreign
keys, so a table that references a patient-scoped parent through a plain
`<parent>_id` column with no constraint was unreachable. Measured on 2026-09-07,
after a real reseed: 7 of 7 `invoice_lines` rows orphaned against `invoices`, and
56 of 61 `claim_events` rows orphaned against `claims`.

The invariant below is deliberately a PROPERTY over the whole schema rather than
an assertion about those two tables. A list of the tables that happened to be
broken on one day is the same mistake the constant was.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from database import db_session
from scripts.reset_db_and_seed import (  # noqa: E402
    TABLES_TO_KEEP,
    patient_scoped_tables,
)


def _schema(conn):
    names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    cols, fk_target, tables = {}, {}, set()
    for t in names:
        try:
            cols[t] = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
            fks = conn.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
        except Exception:
            continue  # virtual table with no module loaded; holds no rows
        tables.add(t)
        fk_target[t] = {r[3]: r[2] for r in fks}   # column -> referenced table
    return tables, cols, fk_target


def _referenced_table(table, col, cols, fk_target, tables):
    """Where `col` points: the declaration if there is one, else the name match.

    A DECLARATION ALWAYS WINS. `helix_inventory_lots.medication_id` reads like
    `medications` (patient-scoped) but declares `helix_medication_master` (not),
    so a bare name match would wipe the pharmacy catalogue on every reseed.
    """
    declared = fk_target.get(table, {}).get(col)
    if declared:
        return declared
    stem = col[:-3]
    for candidate in (stem + "s", stem, stem + "es"):
        if candidate in tables and candidate != table:
            return candidate
    return None


def test_no_surviving_table_references_a_wiped_parent():
    """THE INVARIANT. If a table survives a reseed, nothing it points at may be
    deleted by that reseed, or the surviving rows are orphans the moment it runs.

    This is what catches the NEXT `invoice_lines` without anyone editing a list.
    """
    with db_session() as conn:
        wiped = set(patient_scoped_tables(conn))
        tables, cols, fk_target = _schema(conn)

    offenders = []
    for table in sorted(tables - wiped):
        # audit_logs is the sole member of TABLES_TO_KEEP and is EXEMPT on purpose:
        # a reseed re-seals it rather than wiping it, so audit rows about deleted
        # patients are correct. An audit log that forgot its subject on deletion
        # would be the actual defect.
        if table in TABLES_TO_KEEP:
            continue
        for col in cols.get(table, []):
            if col == "id" or not col.endswith("_id"):
                continue
            parent = _referenced_table(table, col, cols, fk_target, tables)
            if parent in wiped:
                offenders.append(f"{table}.{col} -> {parent}")

    assert not offenders, (
        "these tables SURVIVE a reseed but reference a table it deletes, so every "
        "such row is orphaned the moment the reseed runs:\n  "
        + "\n  ".join(offenders)
        + "\nEither add the table to the wipe set (ideally by making "
          "patient_scoped_tables derive it) or declare the foreign key."
    )


def test_the_known_orphan_tables_are_in_the_wipe_set():
    """The three concrete cases, named so a regression says which one broke.

    payment_allocations held zero rows the day this was found and would have been
    invisible to any data-driven check; it is here because the derivation reaches
    it, which is the point of deriving rather than listing.
    """
    with db_session() as conn:
        wiped = set(patient_scoped_tables(conn))
    for table in ("invoice_lines", "claim_events", "payment_allocations"):
        assert table in wiped, f"{table} would survive a reseed and orphan its rows"


def test_a_declared_foreign_key_beats_the_name_match():
    """The safety property that makes the undeclared-reference walk usable.

    `helix_inventory_lots.medication_id` name-matches `medications`, which IS
    wiped. Only its declaration against `helix_medication_master` keeps the
    hospital's stock and catalogue out of the wipe set.
    """
    with db_session() as conn:
        wiped = set(patient_scoped_tables(conn))
    assert "helix_inventory_lots" not in wiped
    assert "helix_medication_master" not in wiped
