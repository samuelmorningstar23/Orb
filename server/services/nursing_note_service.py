"""Orb: the per-shift nursing note (founding ward list item 4, 21 October 2026).

WHAT IT IS. Free text a nurse writes about one patient on her own ward, filed
under the shift it was written in, with her name and the time. It is the
nursing documentation a full hospital cannot run without: nurses hold
vitals.write and not patient.write, so before this the only nursing text in
Orb was the admission form's one box.

THE WARD RULE IS THE OBSERVATIONS ROUTE'S. The routes
(routes/nursing_note_routes.py) wear require_patient_access, the one
dependency every per-patient route wears: the patient must be on the caller's
department (admissions.department, never patients.ward), an out-of-ward
patient is a 404 that writes a patient_scope_denied row, and a department of
None stays unscoped. Nothing here derives scope by hand, and no column stores
one. services/ward_reference.py is the OTHER ward rule, the one that decides
whether a Sage question names a ward; it does not apply to a per-patient
write and is not read here.

THE AUDIT ROW IS IN THE SAME TRANSACTION. A note and its nursing_note_written
row commit or roll back together (services/audit_service.log_audit_event with
db=, the fail-closed shape every clinical write uses), so a note can never
exist unaudited and the hash chain covers it.

THE SHIFT IS STAMPED BY THE SERVER. ward_record_service.shift_at reads the
same ward shifts the rounds file uses, so a note and a round agree on which
shift an instant belongs to; the client sends no shift.

SWITCHED OFF UNDER THE ORB WARD RECORD PROFILE. The sales plan keeps nursing
notes on paper there (docs/fundraising/SALES_PLAN_2026-10.md section 3, "What
stays on paper"), so every route answers 404 in the words of SWITCHED_OFF, the
timeline stream names no table, and the chart carries no notes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from database import db_session

from services.audit_service import log_audit_event
from services.clinical_time import parse_to_utc, utc_iso_now
from services.ward_record_service import shift_at

logger = logging.getLogger("orb.nursing_notes")

#: The longest note the table accepts. A shift note is a few paragraphs; a
#: pasted document is not a nursing note.
MAX_CHARACTERS = 4000

#: What every nursing note route answers under the Orb Ward Record profile.
SWITCHED_OFF = "Nursing notes are switched off on this computer"

#: The columns a reader gets, with the author's name beside the id so the
#: screen and the print never show DOC001 to a person who knows Sister Mary.
_SELECT = (
    "SELECT nn.id, nn.patient_id, nn.author_id, u.name AS author_name, "
    "nn.shift_date, nn.shift_label, nn.content, nn.written_at "
    "FROM nursing_notes nn LEFT JOIN users u ON u.id = nn.author_id"
)


class NursingNoteError(ValueError):
    """A refusal the route turns into a 422, with a machine-readable code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def nursing_notes_enabled() -> bool:
    """False on an Orb Ward Record computer (ORB_WARD_RECORD), where nursing
    notes stay on paper. Read at call time, like the other profile switches."""
    import config

    return not getattr(config, "WARD_RECORD", False)


def clean_content(raw: Any) -> str:
    """The note as stored: stripped, never empty, never over MAX_CHARACTERS."""
    text = ("" if raw is None else str(raw)).strip()
    if not text:
        raise NursingNoteError("Write the note before saving it.", code="empty")
    if len(text) > MAX_CHARACTERS:
        raise NursingNoteError(
            f"A nursing note holds at most {MAX_CHARACTERS} characters; this one has {len(text)}.",
            code="too_long",
        )
    return text


def _out(row: Any) -> dict:
    return dict(row)


def write_note(
    patient_id: str, author_id: str, content: Any, *, author_role: str | None = None
) -> dict:
    """Write one nursing note and its audit row in one transaction.

    The caller (the route) has already checked the patient is on the author's
    ward. Returns the stored note with the author's name. Raises
    NursingNoteError for an empty or over-long note.
    """
    text = clean_content(content)
    written_at = utc_iso_now()
    shift_date, shift_label = shift_at(parse_to_utc(written_at))
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO nursing_notes
                   (patient_id, author_id, shift_date, shift_label, content, written_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (patient_id, author_id, shift_date, shift_label, text, written_at),
        )
        note_id = int(cur.lastrowid or 0)
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="clinical_record",
            action_type="nursing_note_written",
            outcome="success",
            db=db,
            summary_message=(
                f"Nursing note written for {patient_id} by {author_id} "
                f"({shift_label}, {shift_date})"
            ),
            actor_user_id=author_id,
            actor_role=author_role,
            target_type="nursing_note",
            target_id=str(note_id),
            patient_id=patient_id,
            metadata={
                "note_id": note_id,
                "shift_date": shift_date,
                "shift_label": shift_label,
                "written_at": written_at,
                "characters": len(text),
            },
        )
        row = db.execute(f"{_SELECT} WHERE nn.id = ?", (note_id,)).fetchone()
    logger.info(
        "nursing note: patient %s note %s by %s (%s)", patient_id, note_id, author_id, shift_label
    )
    return _out(row)


def list_notes(patient_id: str, limit: int = 50) -> list[dict]:
    """This patient's notes, newest first."""
    limit = max(1, min(int(limit or 50), 200))
    with db_session() as db:
        rows = db.execute(
            f"{_SELECT} WHERE nn.patient_id = ? ORDER BY nn.written_at DESC, nn.id DESC LIMIT ?",
            (patient_id, limit),
        ).fetchall()
    return [_out(r) for r in rows]


def recent_notes(db: Any, patient_id: str, cutoff_iso: str, limit: int = 5) -> list[dict]:
    """The notes written since `cutoff_iso`, newest first, on the caller's
    connection: the handover reads them inside its own session. Empty on an
    Orb Ward Record computer and on a database the migration has not reached."""
    if not nursing_notes_enabled():
        return []
    try:
        rows = db.execute(
            f"{_SELECT} WHERE nn.patient_id = ? AND datetime(nn.written_at) >= datetime(?) "
            "ORDER BY nn.written_at DESC, nn.id DESC LIMIT ?",
            (patient_id, cutoff_iso, limit),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a database without the table has no notes
        logger.debug("nursing_notes unreadable; treating as none", exc_info=True)
        return []
    return [_out(r) for r in rows]
