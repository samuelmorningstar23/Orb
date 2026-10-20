"""Migration 79: a medicine charted from a signed note keeps which note and which sentence.

Signed-note extraction (services/nervous_system_triggers.py) charts each
medicine it reads from a signed Scribe note as an unverified llm_extracted row
for the pharmacist. The row kept no note id and no sentence, so the Verify
Queue could not show where a drug came from, and a pharmacist had to find the
note by hand to check the model read it right.

Two nullable columns on medications:

* source_note_id   the scribe_notes.id the medicine was read from.
* source_sentence  the sentence of that note that names the drug, as signed.

Plain ADD COLUMN with no default, so every existing row reads NULL (not from a
note, or from a note before this migration). Idempotent through PRAGMA
introspection. No foreign key: scribe_notes is never an FK parent here, and an
ALTER cannot add one in SQLite anyway. No index: the queue reads these by row.
"""

from __future__ import annotations

from migrations import Migration

_COLUMNS = (("source_note_id", "INTEGER"), ("source_sentence", "TEXT"))


def apply(conn) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(medications)").fetchall()}
    if not existing:
        # medications is a baseline table that init_db creates before any
        # migration runs; a database without it has nothing to extend.
        return
    for column, kind in _COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE medications ADD COLUMN {column} {kind}")


MIGRATION = Migration(79, "medication_source_note", apply)
