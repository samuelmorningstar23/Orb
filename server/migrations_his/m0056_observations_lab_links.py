"""Migration 56: link a structured result to the laboratory work behind it (lis-5).

Adds four nullable columns to observations:
  order_id       the CPOE order (orders.id) the result answers;
  specimen_id    the laboratory accession the result was measured on;
  lab_result_id  the lab_results row an LIS release wrote this observation
                 from. The chart's lab card reads it to tell an LIS result
                 (whose flag is the laboratory's own, against a range a
                 pathologist approved) from an interfaced one;
  superseded_by  the observation that replaced this one after an amendment,
                 a withdrawal or an external correction.

Every existing row keeps NULL in all four, which reads exactly as before: an
interfaced or imported result with no laboratory link, live unless its status
says otherwise. Nothing is backfilled and no row changes.

The live-result filter in services/lab_observations.py leaves out a row whose
superseded_by is set, as well as a row whose status is 'superseded'; a writer
that supersedes a result sets both.

ALTER TABLE ADD COLUMN with no default (NULL), which SQLite allows on a table
of any size without rewriting it, guarded by PRAGMA table_info so running it
twice adds nothing. No foreign key: observations stays free of new
constraints, and lab_results is not a parent of anything. Indexes are partial
where the column is NULL for every interfaced row, so they cost nothing until
the laboratory module writes linked rows.
"""

from migrations import Migration

_COLUMNS = (
    ("order_id", "TEXT"),
    ("specimen_id", "TEXT"),
    ("lab_result_id", "INTEGER"),
    ("superseded_by", "INTEGER"),
)

# (index name, columns, WHERE clause or "")
_INDEXES = (
    ("idx_observations_order", ("order_id",), "order_id IS NOT NULL"),
    ("idx_observations_lab_result", ("lab_result_id",), "lab_result_id IS NOT NULL"),
    ("idx_observations_patient_code_time", ("patient_id", "observation_code", "observed_at"), ""),
)


def _columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()}


def apply(conn) -> None:
    existing = _columns(conn)
    if not existing:
        # observations is a baseline table that init_db creates before any
        # migration runs; a database without it has nothing to link.
        return
    for name, sql_type in _COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {name} {sql_type}")
    present = _columns(conn)
    for name, columns, where in _INDEXES:
        if not set(columns) <= present:
            continue
        clause = f" WHERE {where}" if where else ""
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON observations({', '.join(columns)}){clause}"
        )


MIGRATION = Migration(56, "observations_lab_links", apply)
