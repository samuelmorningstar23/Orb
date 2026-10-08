"""Bed moves and transfers (ADT): endpoints under /api/adt.

The package router in routes/adt/__init__.py adds the /api/adt prefix and the
ORB_ADT gate; this router has neither, so every endpoint added here inherits
both.

Built in adt-5 (W5-B):
  POST /admissions/{admission_id}/bed       adt.bed or patient.write, department scope:
                                            a bed for an admission awaiting one, or a
                                            correction of a bed recorded wrongly

Still to come (docs/his_design/adt.json, section 7; adt-6a, adt-6b, adt-6c in W6-C
and W7-C):
  POST /admissions/{admission_id}/bed-change   adt.bed or patient.write
  POST /transfers                           adt.transfer or patient.write
  POST /bed-swaps                           adt.bed or patient.write (both admissions in scope)
  POST /movements/{movement_id}/correct     adt.bed or patient.write

Every move carries an Idempotency-Key and expected_bed_id (409 'state_changed' on a mismatch) and writes its patient_movement audit in the same transaction. A ward clerk never changes the treating department (ruling ADT-S8, held).

Every handler is a plain def (FastAPI threadpools it); the work runs in one
immediate session in services/adt/core.assign_bed. A patient outside the
caller's department is 404, never 403, with the attempt audited (the same
answer as routes/adt/end_stay.py).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from permissions import ADT_BED, PATIENT_WRITE, require_any_permission
from routes.adt.end_stay import _meta, _scoped_admission
from services.adt import core as adt_core
from services.audit_service import AuditWriteError

router = APIRouter()


class AssignBedIn(BaseModel):
    #: A bed from bed setup, as the picker chose it. Either this or bed text.
    bed_id: Optional[str] = None
    ward_id: Optional[str] = None
    #: Typed ward and bed (a lane that only types); resolved against bed setup
    #: by design section 5, and kept unlinked when it names nothing there.
    #: Bed text with no ward resolves on the admission's recorded ward (its
    #: id, else its typed text); with no ward recorded it is 422 ward_required.
    ward: Optional[str] = None
    bed: Optional[str] = None
    #: Ruling ADT-S1 (a): bed_id is occupied, so register a temporary extra bed
    #: beside it with its own label and place the patient there.
    extra_bed: bool = False
    #: The admission already has a bed recorded and it was recorded wrongly.
    #: Needs expected_bed_id (the bed the screen showed, null for typed text
    #: only) and the reason; writes a 'correction' movement. With no bed
    #: recorded it is 409 no_bed_to_correct. A correction naming a different
    #: bed meets the cleaning rule (ADT-S2 (c)) like an assignment.
    correction: bool = False
    expected_bed_id: Optional[str] = None
    reason: Optional[str] = None
    #: When the patient was put in the bed; now when omitted. Up to
    #: ORB_ADT_BACKTIME_HOURS back, never before the admission.
    moved_at: Optional[str] = None
    out_of_service_reason: Optional[str] = None
    boarder_acknowledged: bool = False
    #: Ruling ADT-S2 (c): typed reason for a bed not yet recorded clean, and
    #: who authorises a bed awaiting a terminal clean: the sender (their own
    #: id), or another doctor, nurse or administrator who confirms it with
    #: their own signing PIN, typed here. A name alone is refused.
    not_clean_reason: Optional[str] = None
    terminal_clean_authorised_by: Optional[str] = None
    terminal_clean_reason: Optional[str] = None
    terminal_clean_authoriser_pin: Optional[str] = None


@router.post("/admissions/{admission_id}/bed")
def assign_admission_bed(
    admission_id: str,
    body: AssignBedIn,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_any_permission(ADT_BED, PATIENT_WRITE)),
):
    """Give an admission awaiting a bed its bed. Refused (409
    bed_already_recorded) when a bed is already recorded, so nobody overwrites
    where a patient actually lies, unless the body says correction, which
    needs expected_bed_id and a reason and writes a 'correction' movement
    (refused, 409 no_bed_to_correct, when no bed is recorded). An occupied bed is refused with an extra bed offered (ADT-S1 (a)); a bed
    not recorded clean needs a reason, or for a terminal clean an authoriser
    who acts: the sender, or someone confirming with their signing PIN (403
    terminal_clean_pin_wrong, 429 when their PIN entry is locked) (ADT-S2
    (c)); a boarder placement needs the acknowledgement.
    The Idempotency-Key header is required."""
    _scoped_admission(admission_id, user, request)
    kwargs = {}
    if "expected_bed_id" in body.model_fields_set:
        # Sent (even as null) means 'the screen showed this bed'; absent means
        # the caller did not say, which a correction refuses.
        kwargs["expected_bed_id"] = body.expected_bed_id
    try:
        return adt_core.assign_bed(
            admission_id,
            actor=user,
            ward_id=body.ward_id,
            bed_id=body.bed_id,
            ward_text=body.ward,
            bed_text=body.bed,
            extra_bed=body.extra_bed,
            correction=body.correction,
            reason=body.reason,
            moved_at=body.moved_at,
            out_of_service_reason=body.out_of_service_reason,
            boarder_acknowledged=body.boarder_acknowledged,
            not_clean_reason=body.not_clean_reason,
            terminal_clean_authorised_by=body.terminal_clean_authorised_by,
            terminal_clean_reason=body.terminal_clean_reason,
            terminal_clean_authoriser_pin=body.terminal_clean_authoriser_pin,
            idempotency_key=idempotency_key,
            request_meta=_meta(request),
            **kwargs,
        )
    except adt_core.AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=adt_core.http_detail(err))
    except AuditWriteError:
        raise HTTPException(status_code=503, detail={
            "error": "audit_unavailable",
            "message": "The record of this bed could not be written, so nothing was changed. "
                       "Try again in a moment.",
        })
