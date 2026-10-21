"""Migration 88: the per-shift nursing note.

Nurses hold vitals.write and not patient.write, so until now the only nursing
text Orb could store was the admission form's one box, and nursing
documentation stayed on paper everywhere (docs/WORKFLOWS_2026-10-10.md section
6, item 4). One table, written by services/nursing_note_service.py:

* patient_id   the patient the note is about. Scoped on the way in by the same
               dependency the observations route wears (require_patient_access:
               the patient must be on the writer's ward), never by a column.
* author_id    the account that wrote it (users.id, no FOREIGN KEY by the
               package contract).
* shift_date, shift_label
               the hospital-local day the shift began and its label ("07:00 to
               15:00"), from the ward shifts the rounds file uses
               (ward_record_service.shift_at), stamped at the time of writing.
* content      the note, free text, at most 4000 characters.
* written_at   the UTC time it was written, in the one stored spelling
               (services/clinical_time.py).

Append only: there is no edit route, and the audit row written in the same
transaction (nursing_note_written) is on the hash chain. The index serves the
chart, the timeline and the handover, all of which read one patient's notes
newest first. Idempotent through CREATE TABLE IF NOT EXISTS.
"""

from __future__ import annotations

from typing import Any

from migrations import Migration


def apply(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS nursing_notes (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               patient_id TEXT NOT NULL,
               author_id TEXT NOT NULL,
               shift_date TEXT NOT NULL,
               shift_label TEXT NOT NULL,
               content TEXT NOT NULL,
               written_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nursing_notes_patient_written "
        "ON nursing_notes(patient_id, written_at)"
    )


MIGRATION = Migration(88, "nursing_notes", apply)
