"""Migration 77: a keyed observation set records when it was keyed, what was
illegible on the paper sheet, and whether it was later marked entered in error.

The Orb Ward Record (docs/fundraising/SALES_PLAN_2026-10.md section 3, step 2)
has the ward clerk copy each round from the paper observation sheet. That needs
five nullable columns on vitals:

* entered_at   the UTC time the set was keyed. `timestamp` stays the time the
               reading was taken (for a transcribed set, the time written on
               the sheet), so the clerk's entry lag can be reported on its own.
* illegible    a comma separated list of the fields that could not be read on
               the sheet. Those fields are stored NULL, never guessed, and the
               evidence file counts them apart from a blank.
* voided_at, voided_by, void_reason
               a set marked "entered in error". The row stays in the table and
               in the audit chain; every latest, window and trend reader skips it.

Plain ADD COLUMN with no default, so every existing row reads NULL: keyed at an
unknown time, nothing illegible, not voided. Idempotent through PRAGMA
introspection. No index: the void filter rides the existing patient and time
indexes, and almost every row has voided_at NULL.
"""

from __future__ import annotations

from migrations import Migration

_COLUMNS = ("entered_at", "illegible", "voided_at", "voided_by", "void_reason")


def apply(conn) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(vitals)").fetchall()}
    if not existing:
        # vitals is a baseline table that init_db creates before any migration
        # runs; a database without it has nothing to extend.
        return
    for column in _COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE vitals ADD COLUMN {column} TEXT")


MIGRATION = Migration(77, "vitals_entry_and_void", apply)
