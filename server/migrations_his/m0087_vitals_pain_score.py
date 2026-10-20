"""Migration 87: a pain score on the observation set.

The founding ward's observation sheet carries a pain score beside the vital
signs (docs/WORKFLOWS_2026-10-10.md section 6, item 1), and until now Orb had
no field for it, so the clerk could copy every column of the sheet but that
one. One nullable column on vitals:

* pain_score   the 0 to 10 numeric rating written on the sheet, a whole number.
               NULL is a blank box: the rounds file counts it as a blank like
               any other missing reading, and a box the clerk could not read
               is named in vitals.illegible and stored NULL as before.

The score NEVER feeds NEWS2. The scorer (vigil_service.compute_news2) reads
the seven NEWS2 parameters by name and nothing else, so the column changes no
band, no alert and no schedule; under the Orb Ward Record profile scoring is
off anyway.

Plain ADD COLUMN with no default, so every existing row reads NULL: no pain
score was recorded. Idempotent through PRAGMA introspection. No index.
"""

from __future__ import annotations

from typing import Any

from migrations import Migration

COLUMN = "pain_score"


def apply(conn: Any) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(vitals)").fetchall()}
    if not existing:
        # vitals is a baseline table that init_db creates before any migration
        # runs; a database without it has nothing to extend.
        return
    if COLUMN not in existing:
        conn.execute(f"ALTER TABLE vitals ADD COLUMN {COLUMN} INTEGER")


MIGRATION = Migration(87, "vitals_pain_score", apply)
