"""Migration 73: document numbering per facility (multisite-7a, W10-A).

Until this migration document_number_series held one row per series code
(series_code UNIQUE): one invoice series, one receipt series and so on for the
whole appliance. A group of facilities under one legal entity (ruling MS-R1 (a))
still issues GST documents per registration, so each facility needs its own
series for the same document: facility 2's invoices cannot share facility 1's
counter or prefix. This migration gives every series row a facility:

  facility_id   INTEGER NOT NULL DEFAULT 1, the facility whose documents the
                row numbers (facilities.id; facility 1 is MAIN, migration 70)

and replaces UNIQUE(series_code) with UNIQUE(facility_id, series_code). Every
existing row lands at facility 1, unchanged in every other column, so a site
with one facility numbers exactly as before: same rows, same ids, same
counters, same prefixes. The rows migrations 20, 25, 26, 50 and 54 seeded
(INV, RCPT, REF, CLM, IND, PO, GRN, KIND, INC, NABHPACK, UHID and the seven
pharmacy series) and any series a site added all stay at facility 1. Their
INSERT OR IGNORE statements, and billing_masters.ensure_default_masters, name
no facility, so they meet the facility 1 row through the default and still
add nothing twice.

WHY A REBUILD. SQLite cannot drop a column-level UNIQUE, so the table is
rebuilt. document_number_series is not a foreign-key parent (no table
references it, and the migration refuses to run if one does), so this is the
in-transaction rebuild of migrations 3 and 51 (rename aside, create, copy,
drop), not the FK-parent dance of database._atomic_table_rebuild: it runs
inside the migration runner's transaction and commits, or rolls back, with
the ledger row. The id is copied explicitly and the AUTOINCREMENT high-water
mark is carried over, so a series added later never takes the id of one a
site deleted.

The columns opd-1 (migration 50) added are carried: resets_yearly (a UHID
never resets) and check_digit (the UHID's Verhoeff digit). facility_id is
appended last, so every existing column keeps its position.

GUARDS, all checked before anything changes:
  * a live column this file does not know aborts (it would be dropped);
  * an index or trigger on the table, or a view or trigger elsewhere that
    names it, aborts (it would be dropped or broken by the rename);
  * a table that references document_number_series aborts (the rebuild is
    only safe for a table with no foreign-key children);
  * a leftover document_number_series_aside (an interrupted run) aborts, so
    nothing that might hold rows is overwritten.
After the copy, inside the same transaction: the row count must match, every
copied column must equal the original row for row, every row must sit at
facility 1, and PRAGMA foreign_key_check must report nothing for the table.
Any failure raises, and the runner rolls the whole migration back.

Idempotent: a table that already has facility_id and the composite unique
key is left alone (a fresh database rebuilds once, at its first boot, from
migration 20's shape). Facility-independent: it runs on every install.
"""
from migrations import Migration

TABLE = "document_number_series"
ASIDE = "document_number_series_aside"

#: Every column of the table before this migration, in order: migration 20's,
#: then migration 50's two.
LEGACY_COLUMNS: tuple = (
    "id", "series_code", "document_type", "prefix", "suffix", "financial_year",
    "pad_width", "next_number", "is_configured", "updated_at",
    "resets_yearly", "check_digit",
)

#: The columns after it: the same, then facility_id.
CANONICAL_COLUMNS: tuple = LEGACY_COLUMNS + ("facility_id",)

#: The unique key every lookup now goes through.
UNIQUE_KEY: tuple = ("facility_id", "series_code")

#: Facility 1 (MAIN, migration 70): where every existing series lands.
MAIN_FACILITY_ID = 1

TABLE_DDL = """CREATE TABLE IF NOT EXISTS document_number_series (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_code TEXT NOT NULL,
        document_type TEXT NOT NULL,
        prefix TEXT NOT NULL DEFAULT '',
        suffix TEXT NOT NULL DEFAULT '',
        financial_year TEXT,
        pad_width INTEGER NOT NULL DEFAULT 6,
        next_number INTEGER NOT NULL DEFAULT 1,
        is_configured INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resets_yearly INTEGER NOT NULL DEFAULT 1,
        check_digit TEXT,
        facility_id INTEGER NOT NULL DEFAULT 1,
        UNIQUE (facility_id, series_code)
    )"""


def _columns(conn) -> list:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{TABLE}")').fetchall()]


def _unique_keys(conn) -> list:
    """The column lists of every UNIQUE index on the table (the column-level
    and table-level UNIQUE constraints included)."""
    keys = []
    for row in conn.execute(f'PRAGMA index_list("{TABLE}")').fetchall():
        name, unique = row[1], row[2]
        if not unique:
            continue
        cols = tuple(r[2] for r in conn.execute(f'PRAGMA index_info("{name}")').fetchall())
        keys.append(cols)
    return keys


def is_new_shape(conn) -> bool:
    """True when the table has facility_id, the (facility_id, series_code)
    unique key, and no unique key on series_code alone."""
    cols = set(_columns(conn))
    if "facility_id" not in cols:
        return False
    keys = _unique_keys(conn)
    return UNIQUE_KEY in keys and ("series_code",) not in keys


def _sequence(conn, name: str):
    """The AUTOINCREMENT high-water mark for `name`, or None."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchone() is None:
        return None
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (name,)).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def _refuse_strangers(conn, old_cols: list) -> None:
    unknown = [c for c in old_cols if c not in CANONICAL_COLUMNS]
    if unknown:
        raise RuntimeError(f"{TABLE} rebuild would drop live column(s) {unknown}")
    missing = [c for c in ("id", "series_code", "document_type", "next_number")
               if c not in old_cols]
    if missing:
        raise RuntimeError(f"{TABLE} has no {missing}; not a series table this migration knows")
    assoc = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE tbl_name = ? "
        "AND type IN ('index', 'trigger') AND name NOT LIKE 'sqlite_autoindex%'",
        (TABLE,),
    ).fetchall()
    if assoc:
        raise RuntimeError(
            f"{TABLE} rebuild would drop associated objects it does not know how to "
            f"preserve: {[(r[0], r[1]) for r in assoc]}"
        )
    dependants = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('view', 'trigger') "
        "AND tbl_name != ? AND sql LIKE '%document_number_series%'",
        (TABLE,),
    ).fetchall()
    if dependants:
        raise RuntimeError(
            f"{TABLE} rebuild: other schema objects name the table "
            f"{[(r[0], r[1]) for r in dependants]}; refusing to rename it"
        )
    children = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        try:
            fks = conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
        except Exception:  # noqa: BLE001 - a virtual table whose module is not loaded
            continue
        if any(str(r[2]).lower() == TABLE for r in fks):
            children.append(name)
    if children:
        raise RuntimeError(
            f"{TABLE} rebuild: {children} reference the table by foreign key; this "
            "in-transaction rebuild is only safe for a table with no children"
        )
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (ASIDE,)).fetchone():
        raise RuntimeError(
            f"{ASIDE} already exists; it may hold series rows from an interrupted "
            "rebuild. Inspect it before booting again; nothing was changed."
        )


def _rebuild(conn) -> None:
    """Old shape to new, inside the caller's open transaction."""
    if not getattr(conn, "in_transaction", True):
        raise RuntimeError(
            f"{TABLE} rebuild needs an open transaction (the migration runner holds "
            "one); refusing to rebuild in autocommit"
        )
    old_cols = _columns(conn)
    _refuse_strangers(conn, old_cols)
    shared_list = [c for c in LEGACY_COLUMNS if c in old_cols]
    shared = ", ".join(shared_list)
    before_rows = conn.execute(f"SELECT {shared} FROM {TABLE} ORDER BY id").fetchall()
    before_seq = _sequence(conn, TABLE)

    conn.execute("ALTER TABLE document_number_series RENAME TO document_number_series_aside")
    conn.execute(TABLE_DDL)
    conn.execute(
        f"INSERT INTO document_number_series ({shared}, facility_id) "
        f"SELECT {shared}, {MAIN_FACILITY_ID} FROM document_number_series_aside"
    )

    after_rows = conn.execute(f"SELECT {shared} FROM {TABLE} ORDER BY id").fetchall()
    if len(after_rows) != len(before_rows):
        raise RuntimeError(
            f"{TABLE} rebuild aborted: copied {len(after_rows)} of {len(before_rows)} rows"
        )
    if [tuple(r) for r in after_rows] != [tuple(r) for r in before_rows]:
        raise RuntimeError(f"{TABLE} rebuild aborted: a copied row differs from the original")
    stray = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE facility_id != ?", (MAIN_FACILITY_ID,)
    ).fetchone()[0]
    if stray:
        raise RuntimeError(f"{TABLE} rebuild aborted: {stray} row(s) not at facility 1")
    conn.execute("DROP TABLE document_number_series_aside")

    # Carry the AUTOINCREMENT high-water mark: the explicit-id copy sets it to
    # the largest surviving id, which is lower than before when a site deleted
    # its newest series.
    after_seq = _sequence(conn, TABLE)
    if before_seq is not None and (after_seq is None or after_seq < before_seq):
        if after_seq is None:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                         (TABLE, before_seq))
        else:
            conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                         (before_seq, TABLE))

    violations = conn.execute(f'PRAGMA foreign_key_check("{TABLE}")').fetchall()
    if violations:
        raise RuntimeError(f"{TABLE} rebuild aborted: foreign key check reports {violations[:5]}")
    if not is_new_shape(conn):
        raise RuntimeError(f"{TABLE} rebuild produced an unexpected shape")


def apply(conn) -> None:
    if not _columns(conn):
        # Migration 20 creates the table; a database without it has nothing
        # to number yet, and gets the new shape directly.
        conn.execute(TABLE_DDL)
        return
    if is_new_shape(conn):
        return
    _rebuild(conn)


MIGRATION = Migration(73, "document_series_per_facility", apply)
