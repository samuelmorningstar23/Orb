"""Bed moves and transfers (ADT): endpoints under /api/adt.

The package router in routes/adt/__init__.py adds the /api/adt prefix and the
ORB_ADT gate; this router has neither, so every endpoint added here inherits
both.

Built in adt-5 (W5-B):
  POST /admissions/{admission_id}/bed       adt.bed or patient.write, department scope:
                                            a bed for an admission awaiting one, or a
                                            correction of a bed recorded wrongly

Built in adt-6a (W6-C):
  GET  /admissions/{admission_id}/location  adt.bed, adt.transfer or patient.write, scope:
                                            where the admission lies now (no name or
                                            MRN), read-audited
  GET  /move-options                        adt.bed, adt.transfer or patient.write: the
                                            wards and beds of bed setup with what
                                            placing a patient there needs, who may
                                            authorise a bed awaiting a terminal clean,
                                            and the departments with clinical cover.
                                            No patient data.
  POST /admissions/{admission_id}/bed-change   adt.bed or patient.write, department scope:
                                            another bed, same treating department
  POST /transfers                           adt.transfer or patient.write, department
                                            scope on the SENDING side: another treating
                                            department (rules A, B and C of the front
                                            desk, services/adt/transfer.py)

Built in adt-6b (W7-C):
  POST /bed-swaps                           adt.bed or patient.write, BOTH admissions in
                                            the caller's scope (404 otherwise, nothing
                                            written): two patients exchange beds; both
                                            expected bed ids; one Idempotency-Key covers
                                            the pair
  POST /movements/{movement_id}/correct     adt.bed or patient.write, scope: undo the
                                            stay's latest move as recorded in error (a
                                            new 'correction' movement pointing at it);
                                            undoing a transfer also needs adt.transfer or
                                            patient.write, because the treating
                                            department goes back

Built in adt-17b (W9-A):
  POST /ordered-transfers                   the ward clerk (adt.bed, own department):
                                            carry out a transfer a doctor ordered, naming
                                            that doctor (ruling ADT-S8 (b), build default)

Every move carries an Idempotency-Key and expected_bed_id (409 'state_changed' on a mismatch) and writes its patient_movement audit in the same transaction; a transfer that changes the treating department also writes clinical_access 'patient_transferred' there. A ward clerk never changes the treating department on POST /transfers (the clerk holds adt.bed, not adt.transfer); the clerk's own lane is POST /ordered-transfers, which needs the ordering doctor named (ADT-S8 (b)).

After every move commits the bed boards hear a content-free 'adt:changed'
{ward_id} (adt-19, services/adt/live.py); a refusal or a replay pushes nothing.

Every handler is a plain def (FastAPI threadpools it); the work runs in one
immediate session in services/adt/core.assign_bed or services/adt/transfer.py.
A patient outside the caller's department is 404, never 403, with the attempt
audited (the same answer as routes/adt/end_stay.py). After a move commits the
housekeeping turnaround sync runs best-effort (adt-7); it can never fail the
move.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from permissions import (
    ADT_BED,
    ADT_TRANSFER,
    PATIENT_WRITE,
    has_permission,
    require_any_permission,
)
from pydantic import BaseModel
from services.adt import clerk as adt_clerk
from services.adt import core as adt_core
from services.adt import transfer as adt_transfer
from services.adt.live import notify_adt_changed
from services.audit_service import AuditWriteError

from routes.adt.end_stay import _meta, _scoped_admission, require_ward_clerk

router = APIRouter()


def _pushed(answer: dict, *admission_ids: str) -> dict:
    """adt-19: after a move committed (not a replay), the bed boards refetch."""
    if not answer.get("replayed"):
        notify_adt_changed(answer=answer, admission_ids=admission_ids)
    return answer


class AssignBedIn(BaseModel):
    #: A bed from bed setup, as the picker chose it. Either this or bed text.
    bed_id: str | None = None
    ward_id: str | None = None
    #: Typed ward and bed (a lane that only types); resolved against bed setup
    #: by design section 5, and kept unlinked when it names nothing there.
    #: Bed text with no ward resolves on the admission's recorded ward (its
    #: id, else its typed text); with no ward recorded it is 422 ward_required.
    ward: str | None = None
    bed: str | None = None
    #: Ruling ADT-S1 (a): bed_id is occupied, so register a temporary extra bed
    #: beside it with its own label and place the patient there.
    extra_bed: bool = False
    #: The admission already has a bed recorded and it was recorded wrongly.
    #: Needs expected_bed_id (the bed the screen showed, null for typed text
    #: only) and the reason; writes a 'correction' movement. With no bed
    #: recorded it is 409 no_bed_to_correct. A correction naming a different
    #: bed meets the cleaning rule (ADT-S2 (c)) like an assignment.
    correction: bool = False
    expected_bed_id: str | None = None
    reason: str | None = None
    #: When the patient was put in the bed; now when omitted. Up to
    #: ORB_ADT_BACKTIME_HOURS back, never before the admission.
    moved_at: str | None = None
    out_of_service_reason: str | None = None
    boarder_acknowledged: bool = False
    #: Ruling ADT-S2 (c): typed reason for a bed not yet recorded clean, and
    #: who authorises a bed awaiting a terminal clean: the sender (their own
    #: id), or another doctor, nurse or administrator who confirms it with
    #: their own signing PIN, typed here. A name alone is refused.
    not_clean_reason: str | None = None
    terminal_clean_authorised_by: str | None = None
    terminal_clean_reason: str | None = None
    terminal_clean_authoriser_pin: str | None = None


_ASSIGN_ADMISSION_BED_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_ASSIGN_ADMISSION_BED_USER_DEFAULT = Depends(require_any_permission(ADT_BED, PATIENT_WRITE))


@router.post("/admissions/{admission_id}/bed", response_model=None)
def assign_admission_bed(
    admission_id: str,
    body: AssignBedIn,
    request: Request,
    idempotency_key: str | None = _ASSIGN_ADMISSION_BED_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _ASSIGN_ADMISSION_BED_USER_DEFAULT,
) -> dict:
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
        placed = adt_core.assign_bed(
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
        raise _move_refused(err, "adt_bed_assignment", user, request) from err
    except AuditWriteError:
        raise _audit_unavailable("bed") from None
    if not placed.get("replayed"):
        # adt-7: the bed's housekeeping record follows, best-effort.
        adt_transfer.sync_housekeeping_after_commit()
    return _pushed(placed, admission_id)


def _audit_unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": f"The record of this {what} could not be written, so nothing was changed. "
            "Try again in a moment.",
        },
    )


def _move_refused(err: adt_core.AdtError, lane: str, user: dict, request: Request) -> HTTPException:
    """A move's refusal as its HTTP answer. A move into another facility
    (multisite-8h1, ruling MS-R3 (c)) writes its authorization row first,
    once, after the move's transaction has rolled back."""
    adt_core.audit_facility_refusal(err, lane=lane, actor=user, request_meta=_meta(request))
    return HTTPException(status_code=err.http_status, detail=adt_core.http_detail(err))


def _expected(body: BaseModel) -> dict:
    """expected_bed_id only when the request sent it: sent (even as null)
    means 'the screen showed this bed'; absent is refused by the mover."""
    if "expected_bed_id" in body.model_fields_set:
        return {"expected_bed_id": body.expected_bed_id}
    return {}


_MOVE_OPTIONS_USER_DEFAULT = Depends(require_any_permission(ADT_BED, ADT_TRANSFER, PATIENT_WRITE))


@router.get("/move-options")
def move_options(
    user: dict = _MOVE_OPTIONS_USER_DEFAULT,
):
    """What the Move screen offers: the wards and beds of bed setup, each with
    the status the bed board derives and what placing a patient there needs;
    who may authorise a bed awaiting a terminal clean; the back-time window;
    and the departments with clinical cover a transfer may go to. Staff names
    and ward and bed labels only, no patient data, so nothing is audited."""
    return adt_transfer.move_choices(user)


_ADMISSION_LOCATION_USER_DEFAULT = Depends(
    require_any_permission(ADT_BED, ADT_TRANSFER, PATIENT_WRITE)
)


@router.get("/admissions/{admission_id}/location")
def admission_location(
    admission_id: str,
    request: Request,
    user: dict = _ADMISSION_LOCATION_USER_DEFAULT,
):
    """Where an admission lies now (department, ward and bed, linked or not),
    for the Move screen to send back as expected_bed_id. No name or MRN.
    Department scope (404 outside it); the read is audited phi_access and is
    503 when that row cannot be written."""
    _scoped_admission(admission_id, user, request)
    try:
        return adt_transfer.admission_location(
            admission_id, actor=user, request_meta=_meta(request)
        )
    except adt_core.AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=adt_core.http_detail(err)) from err
    except AuditWriteError:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "audit_unavailable",
                "message": "This read could not be recorded, so nothing is shown. Try again in a moment.",
            },
        ) from None


class MoveIn(BaseModel):
    """A place to move a patient to, as the bed picker chose it."""

    bed_id: str | None = None
    ward_id: str | None = None
    #: Typed ward and bed (a lane that only types), resolved against bed setup.
    ward: str | None = None
    bed: str | None = None
    #: Ruling ADT-S1 (a): bed_id is occupied, so register a temporary extra
    #: bed beside it with its own label and move the patient there.
    extra_bed: bool = False
    #: The bed the screen showed (null for none). Required.
    expected_bed_id: str | None = None
    #: Why the patient moves. Required: a move with no reason is not a record.
    reason: str | None = None
    #: When the patient moved; now when omitted. Up to ORB_ADT_BACKTIME_HOURS
    #: back, never before the admission, never in the future.
    moved_at: str | None = None
    notes: str | None = None
    out_of_service_reason: str | None = None
    boarder_acknowledged: bool = False
    #: Ruling ADT-S2 (c), as on the bed route.
    not_clean_reason: str | None = None
    terminal_clean_authorised_by: str | None = None
    terminal_clean_reason: str | None = None
    terminal_clean_authoriser_pin: str | None = None


class TransferIn(MoveIn):
    #: The admission that moves (its patient must be in the caller's scope).
    admission_id: str
    #: The receiving department: one with clinical cover (rule C).
    to_department: str
    #: Moved off the old bed, waiting for a bed (on `ward_id`/`ward` when named).
    awaiting_bed: bool = False
    #: The treating department changes and the patient stays where they lie
    #: (a boarder on that ward when it belongs to another department).
    keep_location: bool = False


def _move_kwargs(body: MoveIn) -> dict:
    return {
        "ward_id": body.ward_id,
        "bed_id": body.bed_id,
        "ward_text": body.ward,
        "bed_text": body.bed,
        "extra_bed": body.extra_bed,
        "moved_at": body.moved_at,
        "notes": body.notes,
        "out_of_service_reason": body.out_of_service_reason,
        "boarder_acknowledged": body.boarder_acknowledged,
        "not_clean_reason": body.not_clean_reason,
        "terminal_clean_authorised_by": body.terminal_clean_authorised_by,
        "terminal_clean_reason": body.terminal_clean_reason,
        "terminal_clean_authoriser_pin": body.terminal_clean_authoriser_pin,
        **_expected(body),
    }


_CHANGE_ADMISSION_BED_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_CHANGE_ADMISSION_BED_USER_DEFAULT = Depends(require_any_permission(ADT_BED, PATIENT_WRITE))


@router.post("/admissions/{admission_id}/bed-change", response_model=None)
def change_admission_bed(
    admission_id: str,
    body: MoveIn,
    request: Request,
    idempotency_key: str | None = _CHANGE_ADMISSION_BED_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _CHANGE_ADMISSION_BED_USER_DEFAULT,
) -> dict:
    """Move an admitted patient to another bed. The treating department never
    changes here (ruling ADT-S8). Needs expected_bed_id (stale is 409
    state_changed), a reason and the Idempotency-Key header; a replay by the
    same person returns the first movement. An occupied bed is 409 with an
    extra bed offered (ADT-S1 (a)); a bed not recorded clean needs a reason or,
    for a terminal clean, an authoriser who acts (ADT-S2 (c)); a boarder
    placement needs the acknowledgement. A patient with no bed recorded is
    409 no_bed_to_change: assign the bed instead."""
    replay = adt_transfer.replayed_answer(
        idempotency_key, admission_id=admission_id, actor=user, movement_type="bed_change"
    )
    if replay is not None:
        return replay
    _scoped_admission(admission_id, user, request)
    try:
        moved = adt_transfer.change_bed(
            admission_id,
            actor=user,
            reason=body.reason,
            idempotency_key=idempotency_key,
            request_meta=_meta(request),
            **_move_kwargs(body),
        )
    except adt_core.AdtError as err:
        raise _move_refused(err, "adt_bed_change", user, request) from err
    except AuditWriteError:
        raise _audit_unavailable("move") from None
    return _pushed(moved, admission_id)


_CREATE_ADT_TRANSFER_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_CREATE_ADT_TRANSFER_USER_DEFAULT = Depends(require_any_permission(ADT_TRANSFER, PATIENT_WRITE))


@router.post("/transfers", response_model=None)
def create_adt_transfer(
    body: TransferIn,
    request: Request,
    idempotency_key: str | None = _CREATE_ADT_TRANSFER_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _CREATE_ADT_TRANSFER_USER_DEFAULT,
) -> dict:
    """Transfer an admitted patient to another treating department: the same
    admission, its status untouched, the department, ward and bed updated in
    one transaction (rules A and B). The receiving department must have
    clinical cover (rule C; 422 department_not_covered with the front desk's
    words). The scope check is on the SENDING side: the caller must be able
    to see the patient where they are now (404 otherwise). After the commit
    the sending department can no longer open the chart (strict handover).
    A department change writes clinical_access 'patient_transferred' in the
    same transaction (503 audit_unavailable rolls the move back). The
    sender's own retry with the same Idempotency-Key answers that it was
    replayed, with no patient field, even once the chart has been handed
    over."""
    replay = adt_transfer.replayed_answer(
        idempotency_key, admission_id=body.admission_id, actor=user, movement_type="transfer"
    )
    if replay is not None:
        return replay
    _scoped_admission(body.admission_id, user, request)
    try:
        moved = adt_transfer.transfer(
            body.admission_id,
            actor=user,
            to_department=body.to_department,
            reason=body.reason,
            awaiting_bed=body.awaiting_bed,
            keep_location=body.keep_location,
            idempotency_key=idempotency_key,
            request_meta=_meta(request),
            **_move_kwargs(body),
        )
    except adt_core.AdtError as err:
        raise _move_refused(err, "adt_transfer", user, request) from err
    except AuditWriteError:
        raise _audit_unavailable("transfer") from None
    return _pushed(moved, body.admission_id)


class OrderedTransferIn(TransferIn):
    """A transfer the ward clerk carries out on a doctor's order (ADT-S8 (b)).
    The transfer's own fields, plus the doctor who ordered it."""

    #: The doctor who ordered the transfer: the patient's attending doctor or
    #: a doctor of the patient's current department. Recorded as named by the
    #: clerk, never as the doctor's own act.
    ordered_by: str


_CREATE_ORDERED_TRANSFER_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_CREATE_ORDERED_TRANSFER_USER_DEFAULT = Depends(require_ward_clerk)


@router.post("/ordered-transfers", response_model=None)
def create_ordered_transfer(
    body: OrderedTransferIn,
    request: Request,
    idempotency_key: str | None = _CREATE_ORDERED_TRANSFER_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _CREATE_ORDERED_TRANSFER_USER_DEFAULT,
) -> dict:
    """The ward clerk carries out a transfer a doctor ordered: another
    treating department, through the same transfer writer as POST /transfers
    (rule C, the bed lock, the cleaning rules, the boarder acknowledgement,
    patient_movement and clinical_access 'patient_transferred'), plus the
    ordering doctor checked (422 ordering_doctor_required, _invalid or
    _not_treating) and named in the movement's reason, a fail-closed
    clinical_access 'transfer_order_carried_out' row naming the doctor and the
    clerk, and a case-room line. A move inside one department is 409
    no_department_change (a bed change needs no order). The patient must be
    in the clerk's department (404 otherwise, audited). The sender's own retry
    with the same Idempotency-Key answers that it was replayed, with no
    patient field, even once the chart has been handed over."""
    replay = adt_transfer.replayed_answer(
        idempotency_key, admission_id=body.admission_id, actor=user, movement_type="transfer"
    )
    if replay is not None:
        return replay
    _scoped_admission(body.admission_id, user, request)
    try:
        moved = adt_clerk.ordered_transfer(
            body.admission_id,
            actor=user,
            ordered_by=body.ordered_by,
            to_department=body.to_department,
            reason=body.reason,
            awaiting_bed=body.awaiting_bed,
            keep_location=body.keep_location,
            idempotency_key=idempotency_key,
            request_meta=_meta(request),
            **_move_kwargs(body),
        )
    except adt_core.AdtError as err:
        raise _move_refused(err, "clerk_ordered_transfer", user, request) from err
    except AuditWriteError:
        raise _audit_unavailable("transfer") from None
    return _pushed(moved, body.admission_id)


# ═════════════════════════════════════════════════════════════════════════════
#  Bed swap and a move recorded in error (adt-6b, W7-C)
# ═════════════════════════════════════════════════════════════════════════════


class SwapIn(BaseModel):
    """Two admitted patients exchange beds. A treating department never
    changes in a swap (ruling ADT-S8 held)."""

    first_admission_id: str
    second_admission_id: str
    #: The bed the screen showed for each patient. Both required.
    expected_first_bed_id: str | None = None
    expected_second_bed_id: str | None = None
    #: Why they swap. Required: a move with no reason is not a record.
    reason: str | None = None
    #: When they swapped; now when omitted. Up to ORB_ADT_BACKTIME_HOURS back.
    moved_at: str | None = None
    notes: str | None = None
    out_of_service_reason: str | None = None
    #: A patient moved onto a ward of another department lies there as a
    #: boarder (ruling ADT-S6 is not applied): the words name who cannot open
    #: the chart, and this confirms them read.
    boarder_acknowledged: bool = False
    #: Ruling ADT-S2 (c): neither bed is cleaned between the two patients.
    not_clean_reason: str | None = None
    terminal_clean_authorised_by: str | None = None
    terminal_clean_reason: str | None = None
    terminal_clean_authoriser_pin: str | None = None


_SWAP_ADMISSION_BEDS_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_SWAP_ADMISSION_BEDS_USER_DEFAULT = Depends(require_any_permission(ADT_BED, PATIENT_WRITE))


@router.post("/bed-swaps", response_model=None)
def swap_admission_beds(
    body: SwapIn,
    request: Request,
    idempotency_key: str | None = _SWAP_ADMISSION_BEDS_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _SWAP_ADMISSION_BEDS_USER_DEFAULT,
) -> dict:
    """Swap the beds of two admitted patients in one transaction: two
    'bed_swap' movements, each patient in the other's bed, the index never
    tripped. BOTH admissions must be in the caller's department scope (404
    otherwise, audited, nothing written). Both expected bed ids are required
    (stale is 409 state_changed). One Idempotency-Key covers the pair: a
    retry returns the first answer and never swaps back. On a ward whose
    cleaning is tracked the swap needs the reason neither bed is cleaned
    between the patients, and a bed whose next clean is terminal an
    authoriser who acts (ADT-S2 (c)); a boarder needs the acknowledgement."""
    _scoped_admission(body.first_admission_id, user, request)
    _scoped_admission(body.second_admission_id, user, request)
    kwargs = {}
    if "expected_first_bed_id" in body.model_fields_set:
        kwargs["expected_first_bed_id"] = body.expected_first_bed_id
    if "expected_second_bed_id" in body.model_fields_set:
        kwargs["expected_second_bed_id"] = body.expected_second_bed_id
    try:
        swapped = adt_transfer.swap_beds(
            body.first_admission_id,
            body.second_admission_id,
            actor=user,
            reason=body.reason,
            moved_at=body.moved_at,
            notes=body.notes,
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
        raise _move_refused(err, "adt_bed_swap", user, request) from err
    except AuditWriteError:
        raise _audit_unavailable("swap") from None
    return _pushed(swapped, body.first_admission_id, body.second_admission_id)


class CorrectionIn(BaseModel):
    """Undo a move recorded in error: the patient goes back to where the
    move took them from (and, for a transfer, to the department)."""

    #: The bed the screen showed (null for none). Required.
    expected_bed_id: str | None = None
    #: Why the move was recorded in error. Required.
    reason: str | None = None
    notes: str | None = None
    out_of_service_reason: str | None = None
    boarder_acknowledged: bool = False
    #: Ruling ADT-S2 (c), for the bed the patient goes back to.
    not_clean_reason: str | None = None
    terminal_clean_authorised_by: str | None = None
    terminal_clean_reason: str | None = None
    terminal_clean_authoriser_pin: str | None = None


_CORRECT_ADMISSION_MOVEMENT_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_CORRECT_ADMISSION_MOVEMENT_USER_DEFAULT = Depends(require_any_permission(ADT_BED, PATIENT_WRITE))


@router.post("/movements/{movement_id}/correct", response_model=None)
def correct_admission_movement(
    movement_id: str,
    body: CorrectionIn,
    request: Request,
    idempotency_key: str | None = _CORRECT_ADMISSION_MOVEMENT_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _CORRECT_ADMISSION_MOVEMENT_USER_DEFAULT,
) -> dict:
    """Undo the stay's latest move (a bed assignment, a bed change or a
    transfer) as recorded in error: a new 'correction' movement pointing at
    it restores the earlier place through the single location writer, and
    both rows stay. Refused, in words, for anything but the latest move, a
    move already undone, or a patient moved since. Undoing a transfer gives
    the chart back to the sending department, so it also needs adt.transfer
    or patient.write (403 for the ward clerk, ruling ADT-S8 held) and that
    department's clinical cover (rule C), and writes clinical_access. The
    sender's own retry with the same Idempotency-Key answers that it was
    replayed, with no patient field, even once the chart is handed back."""
    replay = adt_transfer.replayed_correction(idempotency_key, movement_id=movement_id, actor=user)
    if replay is not None:
        return replay
    found = adt_transfer.movement_admission(movement_id)
    if found is None or not found.get("admission_id"):
        raise HTTPException(
            status_code=404,
            detail={"error": "movement_not_found", "message": "No movement with that id"},
        )
    _scoped_admission(found["admission_id"], user, request)
    role = user.get("role", "")
    may_change_department = has_permission(role, ADT_TRANSFER) or has_permission(
        role, PATIENT_WRITE
    )
    kwargs = {}
    if "expected_bed_id" in body.model_fields_set:
        kwargs["expected_bed_id"] = body.expected_bed_id
    try:
        corrected = adt_transfer.correct_movement(
            movement_id,
            actor=user,
            reason=body.reason,
            may_change_department=may_change_department,
            notes=body.notes,
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
        raise _move_refused(err, "adt_movement_correction", user, request) from err
    except AuditWriteError:
        raise _audit_unavailable("correction") from None
    return _pushed(corrected, found["admission_id"])
