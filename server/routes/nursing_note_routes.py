"""Orb: nursing note routes (founding ward list item 4, 21 October 2026).

    GET   /api/patients/{patient_id}/nursing-notes   this patient's notes, newest first
    POST  /api/patients/{patient_id}/nursing-notes   write one note for the shift running now

Both wear require_patient_access, the dependency the observations route wears:
the patient must be on the caller's ward (admissions.department), an
out-of-ward patient is a 404 with a patient_scope_denied audit row, and the
read writes a phi_access row. The permissions are nursing_note.write (the
nurse) and nursing_note.read (the nurse and the doctor); the ward clerk holds
neither, and a doctor reads but does not write a nursing note.

Under the Orb Ward Record profile every route here answers 404 in the words of
nursing_note_service.SWITCHED_OFF before any patient is read, as the I-PASS
routes do: the sales plan keeps nursing notes on paper there.

Handlers are plain `def`: they open SQLite, so FastAPI runs them in its
threadpool, off the event loop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from permissions import NURSING_NOTE_READ, NURSING_NOTE_WRITE, require_patient_access
from pydantic import BaseModel

from services import nursing_note_service as svc


def _switched_on() -> None:
    if not svc.nursing_notes_enabled():
        raise HTTPException(status_code=404, detail=svc.SWITCHED_OFF)


router = APIRouter(
    prefix="/api/patients", tags=["nursing-notes"], dependencies=[Depends(_switched_on)]
)


class NursingNoteIn(BaseModel):
    content: str


_LIST_LIMIT_DEFAULT = Query(50, ge=1, le=200)
_LIST_USER_DEFAULT = Depends(
    require_patient_access(NURSING_NOTE_READ, audit_action="nursing_notes_viewed")
)
_WRITE_USER_DEFAULT = Depends(require_patient_access(NURSING_NOTE_WRITE))


@router.get("/{patient_id}/nursing-notes")
def list_nursing_notes(
    patient_id: str,
    limit: int = _LIST_LIMIT_DEFAULT,
    user: dict = _LIST_USER_DEFAULT,
) -> dict:
    return {"patient_id": patient_id, "notes": svc.list_notes(patient_id, limit)}


@router.post("/{patient_id}/nursing-notes", status_code=201)
def write_nursing_note(
    patient_id: str,
    body: NursingNoteIn,
    user: dict = _WRITE_USER_DEFAULT,
) -> dict:
    try:
        return svc.write_note(patient_id, user["id"], body.content, author_role=user.get("role"))
    except svc.NursingNoteError as e:
        raise HTTPException(status_code=422, detail={"error": e.code, "message": str(e)}) from e
