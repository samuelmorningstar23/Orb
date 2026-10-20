"""Orb: transfers and bed changes through the bed master (adt-6a, W6-C).

With Beds and admissions (ORB_ADT) on, every move of an admitted patient goes
through here and then through the single location writer
(services/adt/core.locate_admission_on): the front desk's transfer route
delegates to transfer(), the bed board's Move action calls transfer() or
change_bed(), and a roster file's ward, bed or department update calls
roster_move_on() inside the import's own transaction. With the module off
nothing here runs and every lane behaves exactly as before.

THE FRONT DESK'S THREE RULES, CARRIED OVER VERBATIM
(services/frontdesk_service.py, the module docstring):

  A. THE DERIVATION IS NOT DUPLICATED. A transfer updates the department,
     ward and bed of the patient's EXISTING active admission. There is no
     second location column and no movement-derived scope: the movement row
     is a ledger, never a scope source. permissions.PATIENT_DEPARTMENT_SQL
     reads admissions.department, so the receiving department can open the
     chart and the sending department cannot the moment the write commits
     (strict handover).
  B. THE ADMISSION IS PRESERVED. Same admission id; admissions.status and
     patients.status are never written here. Both columns accept
     'transferred', and either value removes the patient from every board.
  C. A TRANSFER MAY NEVER ORPHAN A PATIENT. The destination department is
     resolved against the departments an active doctor, clinician or nurse
     covers (frontdesk_service.list_covering_departments), and a department
     nobody covers is refused with the front desk's own words. That list
     opens a session of its own, so it is resolved BEFORE the immediate
     session (db_session(immediate=True) must be the outermost session), as
     transfer_patient always did.

WHAT IS ADDED ON TOP (the design, adt-6a):
  * the bed lock (ADT-S1 (a)): an occupied bed is refused, by the writer and
    by the partial unique index underneath; an extra bed with its own label
    may be added beside it;
  * the cleaning rules (ADT-S2 (c)) for the bed a patient is moved INTO,
    through core.prepare_place_on, exactly as at admission;
  * expected_bed_id: the bed the screen showed; a stale one is 409
    'state_changed' and nothing is written;
  * an Idempotency-Key: a replay by the same person for the same admission
    returns the first movement and writes nothing;
  * to a bed by id (or typed text), awaiting a bed on the receiving ward, or
    keeping the bed the patient lies in (the patient then usually boards on
    the old ward);
  * the boarder acknowledgement (ADT-S6 is not applied, so scope does not
    change): the words say who will not be able to open the chart;
  * audit inside the transaction: the writer's patient_movement row for every
    move, plus a clinical_access 'patient_transferred' row when the treating
    department changes (who can read the chart changes). Both categories are
    fail-closed, so an unwritable audit rolls the move back.

A BED CHANGE never changes the treating department (ruling ADT-S8 held: the
ward clerk holds adt.bed only). It is for a patient who has a bed recorded;
one awaiting a bed is given one through POST /api/adt/admissions/{id}/bed.

After a move commits, the housekeeping turnaround sync (adt-7,
housekeeping_service.sync_adt_movements) runs best-effort: it can never fail
or undo a move.

A BED SWAP (adt-6b, W7-C) exchanges the beds of two admitted patients in one
transaction: swap_beds. Both admissions must be in the caller's department
scope, both expected bed ids must match what the screen showed, and one
Idempotency-Key covers the pair (the second movement is keyed '<key>#2'). See
the section's own comment for why it writes the two places itself.

A MOVE RECORDED IN ERROR (adt-6b) is undone by correct_movement: a new
'correction' movement pointing at the old one (corrects_movement_id) restores
the place, and for a transfer the treating department, through the single
location writer. Nothing is updated in place; both rows stay in the ledger.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from database import SERVICE_ACCOUNT_IDS, db_session
from db_backend import DBIntegrityError

from services.adt.core import (
    _UNSET,
    TERMINAL_CLEAN_AUTHORISER_ROLES,
    AdtConflict,
    AdtCrossFacility,
    AdtError,
    AdtNotFound,
    _actor_id,
    _bed_by_label,
    _row,
    _text,
    audit_facility_refusal,
    audit_on,
    bed_choices_on,
    boarder_words,
    carried_isolation,
    check_event_time,
    confirm_terminal_authoriser,
    cross_facility_refusal_on,
    current_admission_on,
    current_location,
    locate_admission_on,
    match_location_on,
    movement_for_key,
    parse_instant,
    place_admission_on,
    prepare_place_on,
    record_movement_on,
    resolve_place_on,
    terminal_authoriser_eligible_on,
)
from services.clinical_time import to_utc_iso, utc_iso_now

logger = logging.getLogger("orb.adt.transfer")

__all__ = [
    "transfer",
    "transfer_on",
    "change_bed",
    "change_bed_on",
    "roster_move_on",
    "resolve_cover",
    "move_choices",
    "sync_housekeeping_after_commit",
    "replayed_answer",
    "admission_location",
    # adt-6b (W7-C): swap and a move recorded in error.
    "AdtForbidden",
    "swap_beds",
    "swap_beds_on",
    "SWAP_SECOND_KEY_SUFFIX",
    "CORRECTABLE_MOVEMENT_TYPES",
    "correct_movement",
    "correct_movement_on",
    "movement_admission",
    "replayed_correction",
    "last_move_on",
]


class AdtForbidden(AdtError):
    """The caller's role cannot make this particular change. 403."""

    http_status = 403


_MAX_REASON = 2000


# ═════════════════════════════════════════════════════════════════════════════
#  Rule C, resolved before any immediate session
# ═════════════════════════════════════════════════════════════════════════════


def _uncovered(to_department: Any) -> AdtError:
    from services import frontdesk_service as fd

    return AdtError(
        fd.uncovered_department_words(to_department),
        code="department_not_covered",
        department=_text(to_department),
    )


def resolve_cover(to_department: Any, *, covering: list | None = None) -> str:
    """The canonical covered spelling of `to_department`, or AdtError with
    rule C's words. Opens a session of its own unless `covering` (the list
    frontdesk_service.list_covering_departments returned, read by the caller
    before its own transaction) is passed, so call it before any
    db_session(immediate=True)."""
    from services import frontdesk_service as fd

    wanted = (_text(to_department) or "").casefold()
    if not wanted:
        raise AdtError("Choose the receiving department.", code="department_required")
    if covering is None:
        canonical = fd.resolve_covering_department(str(to_department))
    else:
        canonical = next((c for c in covering if str(c).casefold() == wanted), None)
    if canonical is None:
        raise _uncovered(to_department)
    return canonical


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════


def _require_key(idempotency_key: Any) -> str:
    key = _text(idempotency_key)
    if not key:
        raise AdtError(
            "This move needs an Idempotency-Key header, so a retried request is "
            "never recorded twice.",
            code="idempotency_key_required",
        )
    return key


def _require_reason(reason: Any, what: str) -> str:
    why = _text(reason, _MAX_REASON)
    if not why:
        raise AdtError(
            f"A {what} needs a reason. A move with no reason is not a record.",
            code="reason_required",
        )
    return why


def _active_admission_on(db, *, admission_id: str | None, patient_id: str | None) -> dict:
    """The admission to move: by its id, or the patient's active one."""
    if admission_id:
        adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
        if adm is None:
            raise AdtNotFound("No admission with that id", code="admission_not_found")
    else:
        adm = current_admission_on(db, patient_id or "")
        if adm is None:
            exists = db.execute(
                "SELECT 1 FROM patients WHERE id = ?", (patient_id or "",)
            ).fetchone()
            if exists is None:
                raise AdtNotFound(f"No such patient: {patient_id}", code="patient_not_found")
            raise AdtError(
                f"{patient_id} has no active admission: an outpatient cannot be "
                "transferred between wards",
                code="admission_not_active",
            )
    if adm.get("status") != "active":
        # Rule B: an ended stay is never moved, and its status is never written.
        raise AdtConflict(
            "That admission has ended, so the patient cannot be moved",
            code="admission_not_active",
            status=adm.get("status"),
        )
    return adm


def _replayed_on(db, key: str, *, adm: dict, actor: dict | None, movement_type: str):
    """The first result for a replayed key, or None when the key is new.
    A key is bound to one admission, one person and one kind of move."""
    prior = movement_for_key(db, key)
    if prior is None:
        return None
    if (
        prior.get("admission_id") != adm["id"]
        or (prior.get("moved_by") or None) != (_actor_id(actor) or None)
        or prior.get("movement_type") != movement_type
    ):
        raise AdtConflict(
            "That Idempotency-Key was already used for another change",
            code="idempotency_key_reused",
        )
    return _result_on(db, adm["id"], prior, replayed=True)


def _check_expected(adm: dict, expected_bed_id: Any) -> None:
    if expected_bed_id is not _UNSET and (expected_bed_id or None) != (adm.get("bed_id") or None):
        raise AdtConflict(
            "Where this patient lies changed since the screen was loaded. Reload and try again.",
            code="state_changed",
            expected_bed_id=expected_bed_id,
            current_bed_id=adm.get("bed_id"),
        )


def _event_time(adm: dict, moved_at: Any, what: str) -> str:
    when = check_event_time(
        moved_at,
        what=what,
        not_before=parse_instant(adm.get("admitted_at") or adm.get("created_at")),
    )
    return to_utc_iso(when)


def _result_on(db, admission_id: str, movement: dict, *, replayed: bool) -> dict:
    """The move's answer: the movement and where the admission is after it.
    The keys of the front desk's transfer answer are all here (patient_name,
    admission_status, patient_status, current_department, current_ward,
    current_bed), so a delegated front-desk call answers a superset."""
    adm = (
        _row(
            db.execute(
                "SELECT id, patient_id, department, ward, bed, ward_id, bed_id, status "
                "FROM admissions WHERE id = ?",
                (admission_id,),
            ).fetchone()
        )
        or {}
    )
    patient = (
        _row(
            db.execute(
                "SELECT name, status FROM patients WHERE id = ?",
                (adm.get("patient_id") or movement.get("patient_id"),),
            ).fetchone()
        )
        or {}
    )
    loc = current_location(adm.get("patient_id") or "", db=db) or {}
    return {
        "replayed": replayed,
        "movement": movement,
        "patient_id": adm.get("patient_id") or movement.get("patient_id"),
        "patient_name": patient.get("name"),
        "admission_id": admission_id,
        # Proof, in the answer, that the admission survived the move (rule B).
        "admission_status": adm.get("status"),
        "patient_status": patient.get("status"),
        "current_department": adm.get("department"),
        "current_ward": adm.get("ward"),
        "current_bed": adm.get("bed"),
        "ward_id": adm.get("ward_id"),
        "bed_id": adm.get("bed_id"),
        "linked": adm.get("bed_id") is not None,
        "awaiting_bed": bool(loc.get("awaiting_bed")) if loc else False,
        "boarder": bool(loc.get("boarder")) if loc else False,
        "department_changed": (movement.get("from_department") or None)
        != (movement.get("to_department") or None),
    }


def _transfer_audit_on(
    db, *, adm: dict, movement: dict, actor: dict | None, reason: str, request_meta: dict | None
) -> None:
    """clinical_access 'patient_transferred' (fail-closed), in the move's own
    transaction: the treating department changed, so who can read the chart
    changed. Written for a department change only; a move inside one
    department is recorded by the writer's patient_movement row."""
    audit_on(
        db,
        category="clinical_access",
        action="patient_transferred",
        summary=(
            f"{adm['patient_id']} transferred from {movement.get('from_department') or 'unknown'} "
            f"to {movement.get('to_department')}: {reason}"
        ),
        actor=actor,
        patient_id=adm["patient_id"],
        target_type="patient_movement",
        target_id=movement["id"],
        request_meta=request_meta,
        correlation_id=f"transfer-{movement['id']}",
        metadata={
            "from_department": movement.get("from_department"),
            "from_ward": movement.get("from_ward"),
            "from_bed": movement.get("from_bed"),
            "to_department": movement.get("to_department"),
            "to_ward": movement.get("to_ward"),
            "to_bed": movement.get("to_bed"),
            "to_ward_id": movement.get("to_ward_id"),
            "to_bed_id": movement.get("to_bed_id"),
            "admission_id": adm["id"],
            # Recorded so a later audit can prove the admission was preserved.
            "admission_status": "active",
            "movement_id": movement["id"],
        },
    )


def sync_housekeeping_after_commit() -> None:
    """The housekeeping turnaround sync (adt-7), best-effort, after a move has
    committed. Any failure is logged and swallowed: the move stands."""
    try:
        from services import housekeeping_service as hk

        hk.sync_adt_movements()
    except Exception:  # noqa: BLE001 - housekeeping can never fail an ADT write
        logger.warning("housekeeping sync after an ADT move failed; the move stands", exc_info=True)


def replayed_answer(
    idempotency_key: Any, *, admission_id: str, actor: dict | None, movement_type: str
) -> dict | None:
    """A retried move's answer, read BEFORE the route's scope check, or None.

    A transfer hands the chart over (strict handover), so the sender's own
    retry of a transfer that has already committed would otherwise meet the
    scope check and answer 404, as if nothing had happened. When the key
    names a movement this same person wrote for this admission, of this kind,
    the retry answers what the first request did, minus everything about the
    patient: replayed, the admission id the caller sent and the movement's
    id, type and time. Nothing is written and nothing about the patient is
    disclosed. Any other use of the key falls through to the normal checks
    (and the writer's 409 idempotency_key_reused)."""
    key = _text(idempotency_key)
    if not key:
        return None
    with db_session() as db:
        prior = movement_for_key(db, key)
    if (
        prior is None
        or prior.get("admission_id") != admission_id
        or (prior.get("moved_by") or None) != (_actor_id(actor) or None)
        or prior.get("movement_type") != movement_type
    ):
        return None
    return {
        "replayed": True,
        "admission_id": admission_id,
        "movement": {
            "id": prior["id"],
            "movement_type": prior["movement_type"],
            "moved_at": prior.get("moved_at"),
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Transfer
# ═════════════════════════════════════════════════════════════════════════════


def transfer_on(
    db,
    *,
    actor: dict | None,
    to_department: str,
    reason: str,
    admission_id: str | None = None,
    patient_id: str | None = None,
    ward_id: Any = None,
    bed_id: Any = None,
    ward_text: Any = None,
    bed_text: Any = None,
    awaiting_bed: bool = False,
    keep_location: bool = False,
    extra_bed: bool = False,
    expected_bed_id: Any = _UNSET,
    moved_at: Any = None,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_confirmed: dict | None = None,
    notes: str | None = None,
    idempotency_key: str,
    request_meta: dict | None = None,
) -> dict:
    """Move an active admission to `to_department` (ALREADY resolved by
    resolve_cover before the caller's immediate session) and to a place, on
    the caller's connection. The caller holds db_session(immediate=True).

    The place is one of: a bed (bed_id, or ward and bed text), with an extra
    bed beside it when it is occupied; awaiting a bed (optionally on a named
    ward); or keep_location, the place the patient lies in now."""
    adm = _active_admission_on(db, admission_id=admission_id, patient_id=patient_id)
    replay = _replayed_on(db, idempotency_key, adm=adm, actor=actor, movement_type="transfer")
    if replay is not None:
        return replay
    _check_expected(adm, expected_bed_id)
    when = _event_time(adm, moved_at, "transfer")
    why = _require_reason(reason, "transfer")
    has_bed = bool(_text(bed_id) or _text(bed_text))

    if keep_location:
        if has_bed or _text(ward_id) or _text(ward_text) or awaiting_bed or extra_bed:
            raise AdtError(
                "Keeping the current bed names no other place. Choose either the "
                "current bed or a new place.",
                code="keep_location_with_place",
            )
        if to_department == adm.get("department"):
            raise AdtConflict(
                f"This patient is already treated by {to_department} and stays "
                "where they are, so there is nothing to record.",
                code="no_change",
            )
        located = locate_admission_on(
            db,
            admission_id=adm["id"],
            actor=actor,
            movement_type="transfer",
            reason=why,
            ward_id=adm.get("ward_id"),
            bed_id=adm.get("bed_id"),
            ward_text=adm.get("ward"),
            bed_text=adm.get("bed"),
            expected_bed_id=expected_bed_id,
            out_of_service_reason=out_of_service_reason,
            boarder_acknowledged=boarder_acknowledged,
            moved_at=when,
            idempotency_key=idempotency_key,
            notes=notes,
            request_meta=request_meta,
            extra_detail={"kept_place": True},
            to_department=to_department,
        )
    else:
        if awaiting_bed and (has_bed or extra_bed):
            raise AdtError(
                "A patient waiting for a bed has no bed to name. Choose a bed or "
                "No bed yet, not both.",
                code="awaiting_bed_with_bed",
            )
        if not awaiting_bed and not has_bed:
            raise AdtError(
                "Choose the bed on the receiving ward, No bed yet, or keep the current bed.",
                code="bed_required",
            )
        if _text(bed_text) and not (_text(bed_id) or _text(ward_id) or _text(ward_text)):
            # Bed text alone names a bed on the ward the patient is on, as a
            # bed change and an assignment read it.
            if _text(adm.get("ward_id")):
                ward_id = adm["ward_id"]
            elif _text(adm.get("ward")):
                ward_text = adm["ward"]
            else:
                raise AdtError(
                    "Choose the receiving ward as well. A bed label on its own names no place.",
                    code="ward_required",
                )
        prepared = prepare_place_on(
            db,
            admission_id=adm["id"],
            actor=actor,
            movement_type="transfer",
            ward_id=ward_id,
            bed_id=None if awaiting_bed else bed_id,
            ward_text=ward_text,
            bed_text=None if awaiting_bed else bed_text,
            extra_bed=extra_bed,
            out_of_service_reason=out_of_service_reason,
            not_clean_reason=not_clean_reason,
            terminal_clean_authorised_by=terminal_clean_authorised_by,
            terminal_clean_reason=terminal_clean_reason,
            request_meta=request_meta,
            treating_department=to_department,
            terminal_clean_confirmed=terminal_clean_confirmed,
        )
        located = place_admission_on(
            db,
            admission_id=adm["id"],
            actor=actor,
            movement_type="transfer",
            reason=why,
            prepared=prepared,
            boarder_acknowledged=boarder_acknowledged,
            expected_bed_id=expected_bed_id,
            moved_at=when,
            idempotency_key=idempotency_key,
            notes=notes,
            request_meta=request_meta,
            to_department=to_department,
        )

    movement = located["movement"]
    if (movement.get("from_department") or None) != (movement.get("to_department") or None):
        _transfer_audit_on(
            db, adm=adm, movement=movement, actor=actor, reason=why, request_meta=request_meta
        )
    return _result_on(db, adm["id"], movement, replayed=False)


def transfer(
    admission_id: str | None = None,
    *,
    actor: dict,
    to_department: Any,
    reason: Any,
    patient_id: str | None = None,
    ward_id: Any = None,
    bed_id: Any = None,
    ward_text: Any = None,
    bed_text: Any = None,
    awaiting_bed: bool = False,
    keep_location: bool = False,
    extra_bed: bool = False,
    expected_bed_id: Any = _UNSET,
    require_expected_bed: bool = True,
    moved_at: Any = None,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_authoriser_pin: Any = None,
    notes: str | None = None,
    idempotency_key: Any = None,
    request_meta: dict | None = None,
) -> dict:
    """Transfer an admitted patient, in ONE immediate session (POST
    /api/adt/transfers, and POST /api/frontdesk/transfers while the module is
    on). Name the admission, or the patient whose active admission moves.

    Before the session: the Idempotency-Key and the reason are required, rule
    C resolves the department against clinical cover, and a terminal-clean
    authoriser's signing PIN is checked (each may open a session of its own).
    `require_expected_bed` is False only for the front desk's old request,
    which never carried the bed its screen showed."""
    from services import housekeeping_service as hk

    key = _require_key(idempotency_key)
    _require_reason(reason, "transfer")
    if require_expected_bed and expected_bed_id is _UNSET:
        raise AdtError(
            "A move must say which bed the screen showed (expected_bed_id), so it "
            "never overwrites a change made since.",
            code="expected_bed_required",
        )
    canonical = resolve_cover(to_department)
    hk.ensure_housekeeping_schema()
    confirmed = confirm_terminal_authoriser(
        actor=actor,
        authorised_by=terminal_clean_authorised_by,
        pin=terminal_clean_authoriser_pin,
        admission_id=admission_id,
        bed_id=bed_id,
        request_meta=request_meta,
    )
    try:
        with db_session(immediate=True) as db:
            result = transfer_on(
                db,
                actor=actor,
                to_department=canonical,
                reason=reason,
                admission_id=admission_id,
                patient_id=patient_id,
                ward_id=ward_id,
                bed_id=bed_id,
                ward_text=ward_text,
                bed_text=bed_text,
                awaiting_bed=awaiting_bed,
                keep_location=keep_location,
                extra_bed=extra_bed,
                expected_bed_id=expected_bed_id,
                moved_at=moved_at,
                out_of_service_reason=out_of_service_reason,
                boarder_acknowledged=boarder_acknowledged,
                not_clean_reason=not_clean_reason,
                terminal_clean_authorised_by=terminal_clean_authorised_by,
                terminal_clean_reason=terminal_clean_reason,
                terminal_clean_confirmed=confirmed,
                notes=notes,
                idempotency_key=key,
                request_meta=request_meta,
            )
    except AdtCrossFacility as err:
        # multisite-8h1: the move rolled back; its refusal is recorded now,
        # for every lane that transfers through here (the front desk's too).
        audit_facility_refusal(err, lane="adt_transfer", actor=actor, request_meta=request_meta)
        raise
    if not result.get("replayed"):
        sync_housekeeping_after_commit()
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Bed change (same treating department)
# ═════════════════════════════════════════════════════════════════════════════


def change_bed_on(
    db,
    *,
    admission_id: str,
    actor: dict | None,
    reason: str,
    ward_id: Any = None,
    bed_id: Any = None,
    ward_text: Any = None,
    bed_text: Any = None,
    extra_bed: bool = False,
    expected_bed_id: Any = _UNSET,
    moved_at: Any = None,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_confirmed: dict | None = None,
    notes: str | None = None,
    idempotency_key: str,
    request_meta: dict | None = None,
) -> dict:
    """Move an active admission to another bed, keeping its treating
    department, on the caller's immediate connection."""
    adm = _active_admission_on(db, admission_id=admission_id, patient_id=None)
    replay = _replayed_on(db, idempotency_key, adm=adm, actor=actor, movement_type="bed_change")
    if replay is not None:
        return replay
    _check_expected(adm, expected_bed_id)
    when = _event_time(adm, moved_at, "bed change")
    why = _require_reason(reason, "bed change")
    if not (adm.get("bed_id") or _text(adm.get("bed"))):
        raise AdtConflict(
            "This patient has no bed recorded, so there is no bed to change. "
            "Assign the bed instead.",
            code="no_bed_to_change",
        )
    if not (_text(bed_id) or _text(bed_text)):
        raise AdtError("Choose the bed.", code="bed_required")
    if not (_text(bed_id) or _text(ward_id) or _text(ward_text)):
        # Bed text alone names a bed on the ward the patient is on (its master
        # id, else its typed text), as an assignment reads it.
        if _text(adm.get("ward_id")):
            ward_id = adm["ward_id"]
        elif _text(adm.get("ward")):
            ward_text = adm["ward"]
        else:
            raise AdtError(
                "Choose the ward as well. This patient has no ward recorded, so a "
                "bed label on its own names no place.",
                code="ward_required",
            )
    prepared = prepare_place_on(
        db,
        admission_id=adm["id"],
        actor=actor,
        movement_type="bed_change",
        ward_id=ward_id,
        bed_id=bed_id,
        ward_text=ward_text,
        bed_text=bed_text,
        extra_bed=extra_bed,
        out_of_service_reason=out_of_service_reason,
        not_clean_reason=not_clean_reason,
        terminal_clean_authorised_by=terminal_clean_authorised_by,
        terminal_clean_reason=terminal_clean_reason,
        request_meta=request_meta,
        treating_department=adm.get("department"),
        terminal_clean_confirmed=terminal_clean_confirmed,
    )
    located = place_admission_on(
        db,
        admission_id=adm["id"],
        actor=actor,
        movement_type="bed_change",
        reason=why,
        prepared=prepared,
        boarder_acknowledged=boarder_acknowledged,
        expected_bed_id=expected_bed_id,
        moved_at=when,
        idempotency_key=idempotency_key,
        notes=notes,
        request_meta=request_meta,
    )
    return _result_on(db, adm["id"], located["movement"], replayed=False)


def change_bed(
    admission_id: str,
    *,
    actor: dict,
    reason: Any,
    ward_id: Any = None,
    bed_id: Any = None,
    ward_text: Any = None,
    bed_text: Any = None,
    extra_bed: bool = False,
    expected_bed_id: Any = _UNSET,
    moved_at: Any = None,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_authoriser_pin: Any = None,
    notes: str | None = None,
    idempotency_key: Any = None,
    request_meta: dict | None = None,
) -> dict:
    """Change an admitted patient's bed in ONE immediate session (POST
    /api/adt/admissions/{id}/bed-change). The Idempotency-Key, the reason and
    the bed the screen showed (expected_bed_id) are required."""
    from services import housekeeping_service as hk

    key = _require_key(idempotency_key)
    _require_reason(reason, "bed change")
    if expected_bed_id is _UNSET:
        raise AdtError(
            "A move must say which bed the screen showed (expected_bed_id), so it "
            "never overwrites a change made since.",
            code="expected_bed_required",
        )
    hk.ensure_housekeeping_schema()
    confirmed = confirm_terminal_authoriser(
        actor=actor,
        authorised_by=terminal_clean_authorised_by,
        pin=terminal_clean_authoriser_pin,
        admission_id=admission_id,
        bed_id=bed_id,
        request_meta=request_meta,
    )
    with db_session(immediate=True) as db:
        result = change_bed_on(
            db,
            admission_id=admission_id,
            actor=actor,
            reason=reason,
            ward_id=ward_id,
            bed_id=bed_id,
            ward_text=ward_text,
            bed_text=bed_text,
            extra_bed=extra_bed,
            expected_bed_id=expected_bed_id,
            moved_at=moved_at,
            out_of_service_reason=out_of_service_reason,
            boarder_acknowledged=boarder_acknowledged,
            not_clean_reason=not_clean_reason,
            terminal_clean_authorised_by=terminal_clean_authorised_by,
            terminal_clean_reason=terminal_clean_reason,
            terminal_clean_confirmed=confirmed,
            notes=notes,
            idempotency_key=key,
            request_meta=request_meta,
        )
    if not result.get("replayed"):
        sync_housekeeping_after_commit()
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Roster import updates
# ═════════════════════════════════════════════════════════════════════════════


def roster_move_on(
    db,
    *,
    admission_id: str,
    actor: dict | None,
    changes: dict,
    covering: list,
    reason: str,
    expected_bed_id: Any = _UNSET,
) -> dict:
    """A roster file's ward, bed or department change for an open admission,
    on the import's own immediate connection (services/roster_import.
    apply_import), through the same writer and rules as every other move.

    `changes` is the import plan's {column: {from, to}} for ward, bed and
    department. A department change is a transfer, resolved against
    `covering` (read before the import's session; rule C); otherwise it is a
    bed change. A file only states where a patient is, so it cannot type a
    cleaning reason, name an authoriser or acknowledge a boarder: any move
    that needs one is refused here with words, and the caller leaves the
    admission as it was and reports the refusal."""
    adm = _active_admission_on(db, admission_id=admission_id, patient_id=None)
    _check_expected(adm, expected_bed_id)
    dept_change = (changes.get("department") or {}).get("to")
    to_department = None
    if (
        dept_change is not None
        and str(dept_change).strip()
        and dept_change != adm.get("department")
    ):
        to_department = resolve_cover(dept_change, covering=covering)
        if to_department == adm.get("department"):
            to_department = None
    new_ward = (changes.get("ward") or {}).get("to")
    new_bed = (changes.get("bed") or {}).get("to")
    ward_text = new_ward if new_ward is not None else adm.get("ward")
    bed_text = new_bed if new_bed is not None else adm.get("bed")
    key = f"roster-{uuid.uuid4().hex}"
    place_changes = new_ward is not None or new_bed is not None
    movement_type = "transfer" if to_department is not None else "bed_change"
    if not place_changes:
        if to_department is None:
            raise AdtConflict(
                "The file names the place and department already recorded.", code="no_change"
            )
        located = locate_admission_on(
            db,
            admission_id=adm["id"],
            actor=actor,
            movement_type="transfer",
            reason=reason,
            ward_id=adm.get("ward_id"),
            bed_id=adm.get("bed_id"),
            ward_text=adm.get("ward"),
            bed_text=adm.get("bed"),
            expected_bed_id=expected_bed_id,
            idempotency_key=key,
            extra_detail={"kept_place": True, "door": "roster_import"},
            to_department=to_department,
        )
    else:
        # A new ward with the bed label unchanged in the file names that label
        # on the NEW ward (the file says ward X, bed 3): bed_text above is the
        # recorded label then.
        prepared = prepare_place_on(
            db,
            admission_id=adm["id"],
            actor=actor,
            movement_type=movement_type,
            ward_text=ward_text,
            bed_text=bed_text,
            treating_department=to_department or adm.get("department"),
        )
        prepared["detail"] = {**(prepared.get("detail") or {}), "door": "roster_import"}
        located = place_admission_on(
            db,
            admission_id=adm["id"],
            actor=actor,
            movement_type=movement_type,
            reason=reason,
            prepared=prepared,
            expected_bed_id=expected_bed_id,
            idempotency_key=key,
            to_department=to_department,
        )
    movement = located["movement"]
    if to_department is not None:
        _transfer_audit_on(
            db, adm=adm, movement=movement, actor=actor, reason=reason, request_meta=None
        )
    return {"movement_id": movement["id"], "movement_type": movement["movement_type"]}


# ═════════════════════════════════════════════════════════════════════════════
#  Where an admission is now, for the Move screen
# ═════════════════════════════════════════════════════════════════════════════


def _kept_place_on(db, loc: dict) -> dict:
    """What 'stays where they lie' resolves to in bed setup, read the way the
    keep-location transfer resolves it (core._resolve_target with the
    admission's own ward_id, bed_id, ward and bed: the bed by id, else the
    ward by id and the bed by label on it, else the typed ward and bed
    matched to bed setup), refusing nothing. The Move screen reads it to ask
    BEFORE the first press for what the writer will ask for: the boarder
    acknowledgement when that ward belongs to another department (a typed,
    unlinked ward that matches a ward of bed setup included, which the linked
    ward_department cannot show), and a reason when that bed is recorded out
    of service. The writer checks all of it again."""
    ward: dict | None = None
    bed: dict | None = None
    bed_id, ward_id = _text(loc.get("bed_id")), _text(loc.get("ward_id"))
    if bed_id:
        bed = _row(
            db.execute(
                "SELECT * FROM housekeeping_units WHERE id = ? AND ward_id IS NOT NULL", (bed_id,)
            ).fetchone()
        )
        if bed is not None:
            ward = _row(
                db.execute("SELECT * FROM wards WHERE id = ?", (bed["ward_id"],)).fetchone()
            )
    elif ward_id:
        ward = _row(db.execute("SELECT * FROM wards WHERE id = ?", (ward_id,)).fetchone())
        bed = _bed_by_label(db, ward["id"], _text(loc.get("bed"))) if ward else None
    else:
        match = match_location_on(db, loc.get("ward"), loc.get("bed"))
        ward, bed = match["ward"], match["bed"]
    return {
        "ward": ward.get("code") if ward else None,
        "ward_department": (_text(ward.get("department")) if ward else None),
        "bed": bed.get("bed_label") if bed else None,
        "out_of_service": bool(bed is not None and bed.get("state") == "out_of_service"),
    }


def admission_location(
    admission_id: str, *, actor: dict | None, request_meta: dict | None = None
) -> dict:
    """Where an active admission lies now, for the Move screen to send back
    as expected_bed_id: its treating department, ward and bed (ids and the
    master's spelling), and whether it is linked, awaiting a bed or a boarder.
    `kept_place` says what keeping that place on a transfer resolves to
    (_kept_place_on): the ward's code and department and whether the bed is
    recorded out of service. No name and no MRN. The caller has checked the
    patient is in scope. The read is audited phi_access (fail-closed) in its
    own session: an unwritable row means no answer."""
    with db_session() as db:
        adm = _row(
            db.execute(
                "SELECT patient_id, status FROM admissions WHERE id = ?", (admission_id,)
            ).fetchone()
        )
        if adm is None:
            raise AdtNotFound("No admission with that id", code="admission_not_found")
        if adm.get("status") != "active":
            raise AdtConflict(
                "That admission has ended, so the patient cannot be moved",
                code="admission_not_active",
                status=adm.get("status"),
            )
        loc = current_location(adm["patient_id"], db=db) or {}
        if loc.get("admission_id") != admission_id:
            raise AdtConflict(
                "That admission is not the patient's current stay", code="admission_not_active"
            )
        kept = _kept_place_on(db, loc)
        # adt-6b: the latest move of this stay and whether it can be undone
        # as recorded in error, for the Move screen.
        last_move = last_move_on(db, admission_id)
        audit_on(
            db,
            category="phi_access",
            action="admission_location_viewed",
            summary=f"Where admission {admission_id} lies, read for a move",
            actor=actor,
            patient_id=adm["patient_id"],
            target_type="admission",
            target_id=admission_id,
            request_meta=request_meta,
        )
    return {
        "admission_id": admission_id,
        "department": loc.get("department"),
        "ward": loc.get("ward"),
        "bed": loc.get("bed"),
        "ward_id": loc.get("ward_id"),
        "bed_id": loc.get("bed_id"),
        "ward_department": loc.get("ward_department"),
        "linked": bool(loc.get("linked")),
        "awaiting_bed": bool(loc.get("awaiting_bed")),
        "boarder": bool(loc.get("boarder")),
        "kept_place": kept,
        "last_move": last_move,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  What the Move screen offers
# ═════════════════════════════════════════════════════════════════════════════


def move_choices(user: dict | None) -> dict:
    """The Move screen's lists, with NO patient data: every active ward and
    its beds with what placing a patient there needs (core.bed_choices_on,
    the same lists the admission form's bed picker reads), who may authorise
    a bed awaiting a terminal clean (the caller when their role allows; the
    others only with a signing PIN set), the back-time window, and the
    departments a transfer may go to (those with clinical cover, rule C).
    With more than one facility on record (multisite-8h1) the wards and the
    departments are the caller's facility's only."""
    import config

    from services import facility_lanes
    from services import frontdesk_service as fd
    from services import housekeeping_service as hk

    # Each opens a session of its own, so they run before the read below.
    hk.ensure_housekeeping_schema()
    facilities = facility_lanes.scope_of(user or {})
    departments = fd.list_covering_departments(facilities=facilities)
    me = str((user or {}).get("id") or "")
    service_ids = sorted(SERVICE_ACCOUNT_IDS)
    role_marks = ",".join("?" for _ in TERMINAL_CLEAN_AUTHORISER_ROLES)
    service_marks = ",".join("?" for _ in service_ids)
    with db_session() as db:
        wards = bed_choices_on(db, facilities=facilities)
        authorisers = [
            {
                "id": r["id"],
                "name": r["name"],
                "role": r["role"],
                "department": r["department"] or None,
            }
            for r in db.execute(
                f"""SELECT id, name, role, TRIM(COALESCE(department, '')) AS department
                      FROM users
                     WHERE role IN ({role_marks}) AND COALESCE(is_active, 1) = 1
                       AND id NOT IN ({service_marks}) AND id <> ?
                       AND LOWER(TRIM(id)) NOT LIKE 'bot\\_%' ESCAPE '\\'
                       AND pin_hash IS NOT NULL AND pin_hash <> ''
                     ORDER BY name""",
                (*TERMINAL_CLEAN_AUTHORISER_ROLES, *service_ids, me),
            ).fetchall()
        ]
        self_authoriser = None
        if me and terminal_authoriser_eligible_on(db, me):
            row = db.execute(
                "SELECT id, name, role, TRIM(COALESCE(department, '')) AS department "
                "FROM users WHERE id = ?",
                (me,),
            ).fetchone()
            if row is not None:
                self_authoriser = {
                    "id": row["id"],
                    "name": row["name"],
                    "role": row["role"],
                    "department": row["department"] or None,
                }
    return {
        "master": {
            "wards": wards,
            "authorisers": authorisers,
            "self_authoriser": self_authoriser,
            "backtime_hours": int(getattr(config, "ADT_BACKTIME_HOURS", 72)),
        },
        "departments": departments,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Bed swap (adt-6b, W7-C)
# ═════════════════════════════════════════════════════════════════════════════
#
# WHY THIS WRITES THE TWO PLACES ITSELF. core.locate_admission_on is the single
# writer of a place, and it moves ONE admission into a bed nobody else holds:
# its occupancy check and the partial unique index idx_admissions_active_bed
# both refuse the second patient's bed while the first still holds it. A swap
# is the one move that cannot be written as two such calls without a third,
# false movement (one patient 'moved off' to no bed), which would put a
# departure that never happened into the ledger, the census and housekeeping.
# So the swap writes the same columns locate_admission_on writes
# (admissions.ward_id, bed_id, ward and bed in the master's spelling, and the
# patients mirror for the current admission), under the same checks, in ONE
# immediate transaction:
#
#   1. the NULL step: the first admission's bed_id is set NULL, so for one
#      statement nobody holds its bed and the index stays quiet;
#   2. the second admission takes the first bed;
#   3. the first admission takes the second bed.
#
# The ledger rows go through the single ledger writer (core.record_movement_on)
# as two 'bed_swap' movements with their true from and to places, keyed by the
# request's Idempotency-Key and '<key>#2'. Each admission gets its own
# fail-closed patient_movement audit 'beds_swapped'. (services/adt/end_stay.
# reopen_stay_on is the other writer of a place outside locate_admission_on,
# for the same kind of reason.)
#
# RULINGS APPLIED AS BUILD DEFAULTS (the strictest option named; ORB_ADT stays
# off at every site until Samuel confirms them):
#   ADT-S1 (a)  no shared bed at any moment: the NULL step, and the index
#               underneath, refuse it.
#   ADT-S2 (c)  each patient goes into a bed the other has just left, with no
#               clean between. On a ward whose cleaning housekeeping tracks,
#               the swap needs a typed reason for that (one reason, recorded on
#               both rows); a bed whose next clean is TERMINAL (an isolation
#               bed or room, a patient under isolation precautions leaving it,
#               an open terminal clean, or an isolation stay that left it since
#               it was last recorded clean) needs an authoriser who acts, plus
#               a reason, exactly as at admission. A bed recorded out of
#               service needs its typed reason; a bed a live reservation holds
#               is refused.
#   ADT-S6      not applied: scope does not change; a patient moved onto a ward
#               of another department is a boarder, acknowledged, audited
#               (clinical_access 'boarder_placed') and told to the case room.
#   ADT-S8      held: the treating departments never change in a swap.

#: The second movement of a swap is keyed '<Idempotency-Key>#2'.
SWAP_SECOND_KEY_SUFFIX = "#2"


def _in_scope_on(db, patient_id: str, actor: dict | None) -> bool:
    """The caller may see this patient (permissions.department_scope, the one
    derivation), read on the caller's connection so the check and the write
    see the same state."""
    from permissions import department_scope, patient_in_scope

    dept = department_scope(actor or {})
    return dept is None or patient_in_scope(db, patient_id, dept)


def _bed_reserved_on(db, bed_id: Any) -> bool:
    """A live reservation holds the bed (bed_reservations, migration 69): an
    active row whose expiry has not passed. False while the table does not
    exist."""
    if (
        not bed_id
        or db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bed_reservations'"
        ).fetchone()
        is None
    ):
        return False
    cols = {r[1] for r in db.execute("PRAGMA table_info(bed_reservations)").fetchall()}
    if not {"bed_id", "status"} <= cols:
        return False
    sql = "SELECT 1 FROM bed_reservations WHERE bed_id = ? AND status = 'active'"
    params: list[Any] = [bed_id]
    if "expires_at" in cols:
        sql += " AND (expires_at IS NULL OR julianday(expires_at) > julianday(?))"
        params.append(utc_iso_now())
    return db.execute(sql + " LIMIT 1", tuple(params)).fetchone() is not None


def _same_text(a: Any, b: Any) -> bool:
    return (_text(a) or "").casefold() == (_text(b) or "").casefold()


def _bed_label_words(target: dict) -> str:
    ward = target.get("ward") or {}
    return f"Bed {target.get('bed_text')} on {ward.get('code') or target.get('ward_text')}"


def _swap_cleaning_on(
    db,
    *,
    target: dict,
    leaving: dict,
    actor: dict | None,
    treating_department: str | None,
    not_clean_reason: Any,
    terminal_clean_authorised_by: Any,
    terminal_clean_reason: Any,
    terminal_clean_confirmed: dict | None,
) -> dict:
    """ADT-S2 (c) for one side of a swap: the patient goes into `target`, the
    bed `leaving` lay in until this moment, with no clean between. Returns
    what is recorded on the movement; raises with words otherwise. Nothing
    is asked on a ward whose cleaning housekeeping does not track."""
    from services.adt.core import (
        _authoriser_on,
        _board_tiles_on,
        _terminal_clean_beds_on,
        _vacated_with_isolation_on,
    )

    ward, bed = target.get("ward"), target.get("bed")
    if ward is None or bed is None:
        return {}
    board = _board_tiles_on(db, [ward])
    board_wards = board.get("wards") or []
    if not board_wards or not board_wards[0].get("cleaning_tracked"):
        return {}
    tile = next(
        (t for t in board_wards[0].get("tiles") or [] if t.get("bed_id") == bed["id"]), None
    )
    terminal = (
        carried_isolation(leaving.get("isolation_precautions"))
        or bool(tile and tile.get("isolation"))
        or bed["id"] in _terminal_clean_beds_on(db, [bed["id"]])
        or bed["id"] in _vacated_with_isolation_on(db, wards=board_wards, bed_ids=[bed["id"]])
    )
    where = _bed_label_words(target)
    if terminal:
        authoriser = _text(terminal_clean_authorised_by)
        why = _text(terminal_clean_reason, _MAX_REASON)
        if not authoriser or not why:
            raise AdtConflict(
                f"{where} needs a terminal clean before another patient lies in it (an "
                "isolation bed, or a patient under isolation precautions is leaving it or left "
                "it). Swapping now needs a doctor, nurse or administrator to authorise it, with "
                "the reason: you, if your role allows, or someone who confirms it here with "
                "their own signing PIN. Both are recorded.",
                code="terminal_clean_authorisation_required",
                bed_id=bed["id"],
                bed_status="swap",
            )
        person = _authoriser_on(db, authoriser, ward=ward, treating_department=treating_department)
        if person["id"] == _actor_id(actor):
            confirmed_by = "own_session"
        elif (terminal_clean_confirmed or {}).get("id") == person["id"]:
            confirmed_by = "signing_pin"
        else:
            raise AdtConflict(
                f"{person.get('name') or person['id']} must confirm this here with their own "
                "signing PIN, or authorise it yourself if your role allows. Naming someone is "
                "not enough, because the record would say they authorised it.",
                code="terminal_clean_pin_required",
                bed_id=bed["id"],
                authoriser_id=person["id"],
            )
        return {
            "bed_status": "swap",
            "terminal_clean_override": {
                "authorised_by": person["id"],
                "authorised_by_name": person.get("name"),
                "authoriser_role": person.get("role"),
                "authoriser_department": person.get("department") or None,
                "confirmed_by": confirmed_by,
                "reason": why,
            },
        }
    why = _text(not_clean_reason, _MAX_REASON)
    if not why:
        raise AdtConflict(
            f"Neither bed is cleaned between the two patients. Type the reason they swap "
            f"before {where} is cleaned; it is recorded.",
            code="bed_not_clean_reason_required",
            bed_id=bed["id"],
            bed_status="swap",
        )
    return {"bed_status": "swap", "not_clean_reason": why}


def _swap_replayed_on(
    db, key: str, second_key: str, *, first_id: str, second_id: str, actor: dict | None
) -> dict | None:
    """The first answer for a retried swap, or None when the key is new. The
    key is bound to one person and the same two admissions in the same order;
    any other use is 409 idempotency_key_reused."""
    prior_first = movement_for_key(db, key)
    prior_second = movement_for_key(db, second_key)
    if prior_first is None and prior_second is None:
        return None
    me = _actor_id(actor) or None
    if (
        prior_first is None
        or prior_second is None
        or prior_first.get("movement_type") != "bed_swap"
        or prior_second.get("movement_type") != "bed_swap"
        or prior_first.get("admission_id") != first_id
        or prior_second.get("admission_id") != second_id
        or (prior_first.get("moved_by") or None) != me
        or (prior_second.get("moved_by") or None) != me
    ):
        raise AdtConflict(
            "That Idempotency-Key was already used for another change",
            code="idempotency_key_reused",
        )
    return {
        "replayed": True,
        "swap_key": key,
        "first": _result_on(db, first_id, prior_first, replayed=True),
        "second": _result_on(db, second_id, prior_second, replayed=True),
    }


def swap_beds_on(
    db: Any,
    *,
    first_admission_id: str,
    second_admission_id: str,
    actor: dict | None,
    reason: Any,
    expected_first_bed_id: Any = _UNSET,
    expected_second_bed_id: Any = _UNSET,
    moved_at: Any = None,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_confirmed: dict | None = None,
    notes: str | None = None,
    idempotency_key: str,
    request_meta: dict | None = None,
) -> dict:
    """Swap the beds of two active admissions on the caller's immediate
    connection (see the section comment above). Every check runs before the
    first write; a replayed key returns the first answer and writes nothing."""
    key = idempotency_key
    second_key = f"{key}{SWAP_SECOND_KEY_SUFFIX}"
    replay = _swap_replayed_on(
        db, key, second_key, first_id=first_admission_id, second_id=second_admission_id, actor=actor
    )
    if replay is not None:
        return replay
    if (_text(first_admission_id) or "") == (_text(second_admission_id) or ""):
        raise AdtError("Choose two different patients to swap beds.", code="swap_needs_two")
    first = _active_admission_on(db, admission_id=first_admission_id, patient_id=None)
    second = _active_admission_on(db, admission_id=second_admission_id, patient_id=None)
    for adm in (first, second):
        # Both sides in the caller's scope (the route checked them already;
        # this closes the gap to the write). Same answer as an unknown id.
        if not _in_scope_on(db, adm["patient_id"], actor):
            raise AdtNotFound("No admission with that id", code="admission_not_found")
    _check_expected(first, expected_first_bed_id)
    _check_expected(second, expected_second_bed_id)
    missing = [a["id"] for a in (first, second) if not _text(a.get("bed_id"))]
    if missing:
        raise AdtConflict(
            "Both patients must lie in beds set up in Bed setup to swap beds. A bed that is "
            "typed or not yet chosen is linked or assigned first; a single patient is moved "
            "with a bed change.",
            code="swap_needs_beds",
            admission_ids=missing,
        )
    starts = [parse_instant(a.get("admitted_at") or a.get("created_at")) for a in (first, second)]
    not_before = max((s for s in starts if s is not None), default=None)
    when = to_utc_iso(check_event_time(moved_at, what="bed swap", not_before=not_before))
    why = _require_reason(reason, "bed swap")

    # Where each one goes: the other's bed, checked against the master as any
    # move is (retired, switched off, out of service without its reason).
    to_first = resolve_place_on(
        db, bed_id=second["bed_id"], out_of_service_reason=out_of_service_reason
    )
    to_second = resolve_place_on(
        db, bed_id=first["bed_id"], out_of_service_reason=out_of_service_reason
    )
    legs = ((first, to_first, second), (second, to_second, first))
    for adm, target, _leaving in legs:
        # multisite-8h1: neither patient moves onto another facility's ward
        # (an account that sees both facilities could otherwise swap across).
        crossing = cross_facility_refusal_on(db, admission_id=adm["id"], ward=target.get("ward"))
        if crossing is not None:
            raise crossing
    for _adm, target, _leaving in legs:
        if _bed_reserved_on(db, target["bed_id"]):
            raise AdtConflict(
                f"{_bed_label_words(target)} is held by a reservation. Release the "
                "reservation before swapping.",
                code="bed_reserved",
                bed_id=target["bed_id"],
            )

    boarders: dict[str, dict] = {}
    for adm, target, _leaving in legs:
        ward = target.get("ward")
        if (
            ward is not None
            and _text(ward.get("department"))
            and not _same_text(ward.get("department"), adm.get("department"))
            and ward["id"] != adm.get("ward_id")
        ):
            boarders[adm["id"]] = ward
    if boarders and not boarder_acknowledged:
        words = " ".join(
            f"{_bed_label_words(target)}: {boarder_words(target['ward'], adm.get('department'))}"
            for adm, target, _leaving in legs
            if adm["id"] in boarders
        )
        first_boarder = next(adm for adm, _t, _l in legs if adm["id"] in boarders)
        raise AdtConflict(
            words,
            code="boarder_acknowledgement_required",
            ward_department=boarders[first_boarder["id"]].get("department"),
            treating_department=first_boarder.get("department"),
            admission_ids=list(boarders),
        )

    cleaning: dict[str, dict] = {}
    for adm, target, leaving in legs:
        cleaning[adm["id"]] = _swap_cleaning_on(
            db,
            target=target,
            leaving=leaving,
            actor=actor,
            treating_department=adm.get("department"),
            not_clean_reason=not_clean_reason,
            terminal_clean_authorised_by=terminal_clean_authorised_by,
            terminal_clean_reason=terminal_clean_reason,
            terminal_clean_confirmed=terminal_clean_confirmed,
        )

    before = {
        a["id"]: {
            "department": a.get("department"),
            "ward": a.get("ward"),
            "bed": a.get("bed"),
            "ward_id": a.get("ward_id"),
            "bed_id": a.get("bed_id"),
        }
        for a in (first, second)
    }
    after = {
        adm["id"]: {
            "department": adm.get("department"),
            "ward": target["ward_text"],
            "bed": target["bed_text"],
            "ward_id": target["ward_id"],
            "bed_id": target["bed_id"],
        }
        for adm, target, _l in legs
    }

    try:
        # 1. The NULL step: for one statement nobody holds the first bed.
        db.execute(
            "UPDATE admissions SET bed_id = NULL WHERE id = ? AND status = 'active'", (first["id"],)
        )
        # 2 and 3. Each takes the other's bed, in the master's spelling.
        for adm in (second, first):
            place = after[adm["id"]]
            done = db.execute(
                "UPDATE admissions SET ward_id = ?, bed_id = ?, ward = ?, bed = ? "
                "WHERE id = ? AND status = 'active'",
                (place["ward_id"], place["bed_id"], place["ward"], place["bed"], adm["id"]),
            )
            if done.rowcount != 1:
                raise AdtConflict(
                    "One of the two stays changed while the swap was being "
                    "recorded. Nothing was changed. Reload and try again.",
                    code="state_changed",
                )
    except DBIntegrityError as exc:
        raise AdtConflict(
            "One of the two beds was just taken by another admission. Nothing was changed. "
            "Reload and try again.",
            code="bed_occupied",
        ) from exc
    # The patients mirror holds the CURRENT admission's place only.
    for adm in (first, second):
        current = current_admission_on(db, adm["patient_id"])
        if current is not None and current["id"] == adm["id"]:
            db.execute(
                "UPDATE patients SET ward = ?, bed = ? WHERE id = ?",
                (after[adm["id"]]["ward"], after[adm["id"]]["bed"], adm["patient_id"]),
            )

    actor_name = None
    if _actor_id(actor):
        name_row = db.execute("SELECT name FROM users WHERE id = ?", (_actor_id(actor),)).fetchone()
        actor_name = name_row["name"] if name_row else None
    movements: dict[str, dict] = {}
    for index, (adm, target, leaving) in enumerate(legs, start=1):
        detail: dict[str, Any] = {
            "swap": {"leg": index, "with_admission_id": leaving["id"], "pair_key": key}
        }
        if index == 2:
            detail["swap"]["with_movement_id"] = movements[first["id"]]["id"]
        detail.update(cleaning[adm["id"]])
        if target.get("out_of_service_reason"):
            detail["out_of_service_reason"] = target["out_of_service_reason"]
        if adm["id"] in boarders:
            detail["boarder"] = {
                "ward_department": boarders[adm["id"]].get("department"),
                "treating_department": adm.get("department"),
            }
        movements[adm["id"]] = record_movement_on(
            db,
            patient_id=adm["patient_id"],
            admission_id=adm["id"],
            movement_type="bed_swap",
            reason=why,
            moved_at=when,
            moved_by=_actor_id(actor),
            moved_by_name=actor_name,
            from_location=before[adm["id"]],
            to_location=after[adm["id"]],
            notes=notes,
            idempotency_key=key if index == 1 else second_key,
            detail=detail,
        )

    from services.adt.core import _case_room_line_on

    correlation = f"swap-{movements[first['id']]['id']}"
    for adm, _target, leaving in legs:
        mov = movements[adm["id"]]
        place = after[adm["id"]]
        audit_on(
            db,
            category="patient_movement",
            action="beds_swapped",
            summary=(
                f"Admission {adm['id']} swapped beds with admission {leaving['id']}: now at "
                f"{place['ward'] or 'no ward'}/{place['bed'] or 'no bed'}"
            ),
            actor=actor,
            patient_id=adm["patient_id"],
            target_type="admission",
            target_id=adm["id"],
            request_meta=request_meta,
            correlation_id=correlation,
            metadata={
                "movement_id": mov["id"],
                "movement_type": "bed_swap",
                "from": before[adm["id"]],
                "to": place,
                "with_admission_id": leaving["id"],
                "with_movement_id": movements[leaving["id"]]["id"],
                **({"detail": mov["detail"]} if mov.get("detail") else {}),
            },
        )
        if adm["id"] in boarders:
            ward = boarders[adm["id"]]
            audit_on(
                db,
                category="clinical_access",
                action="boarder_placed",
                summary=(
                    f"Admission {adm['id']} placed on {ward['code']} "
                    f"({ward.get('department')}) in a bed swap while treated by "
                    f"{adm.get('department')}"
                ),
                actor=actor,
                patient_id=adm["patient_id"],
                target_type="admission",
                target_id=adm["id"],
                request_meta=request_meta,
                metadata={
                    "movement_id": mov["id"],
                    "ward_id": ward["id"],
                    "ward_department": ward.get("department"),
                    "treating_department": adm.get("department"),
                    "acknowledged": True,
                },
            )
            _case_room_line_on(
                db,
                adm["patient_id"],
                f"Placed as a boarder on {ward['code']} ({ward.get('department')}), bed "
                f"{place['bed']}, in a bed swap. {boarder_words(ward, adm.get('department'))}",
            )

    return {
        "replayed": False,
        "swap_key": key,
        "first": _result_on(db, first["id"], movements[first["id"]], replayed=False),
        "second": _result_on(db, second["id"], movements[second["id"]], replayed=False),
    }


def swap_beds(
    first_admission_id: str,
    second_admission_id: str,
    *,
    actor: dict,
    reason: Any,
    expected_first_bed_id: Any = _UNSET,
    expected_second_bed_id: Any = _UNSET,
    moved_at: Any = None,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_authoriser_pin: Any = None,
    notes: str | None = None,
    idempotency_key: Any = None,
    request_meta: dict | None = None,
) -> dict:
    """Swap two admitted patients' beds in ONE immediate session (POST
    /api/adt/bed-swaps). The Idempotency-Key, the reason and both beds the
    screen showed are required; a terminal-clean authoriser's signing PIN is
    checked before the session opens (it may open a session of its own)."""
    from services import housekeeping_service as hk

    key = _require_key(idempotency_key)
    _require_reason(reason, "bed swap")
    if expected_first_bed_id is _UNSET or expected_second_bed_id is _UNSET:
        raise AdtError(
            "A swap must say which bed the screen showed for each patient "
            "(expected_first_bed_id and expected_second_bed_id), so it never "
            "overwrites a change made since.",
            code="expected_bed_required",
        )
    hk.ensure_housekeeping_schema()
    confirmed = confirm_terminal_authoriser(
        actor=actor,
        authorised_by=terminal_clean_authorised_by,
        pin=terminal_clean_authoriser_pin,
        admission_id=first_admission_id,
        request_meta=request_meta,
    )
    with db_session(immediate=True) as db:
        result = swap_beds_on(
            db,
            first_admission_id=first_admission_id,
            second_admission_id=second_admission_id,
            actor=actor,
            reason=reason,
            expected_first_bed_id=expected_first_bed_id,
            expected_second_bed_id=expected_second_bed_id,
            moved_at=moved_at,
            out_of_service_reason=out_of_service_reason,
            boarder_acknowledged=boarder_acknowledged,
            not_clean_reason=not_clean_reason,
            terminal_clean_authorised_by=terminal_clean_authorised_by,
            terminal_clean_reason=terminal_clean_reason,
            terminal_clean_confirmed=confirmed,
            notes=notes,
            idempotency_key=key,
            request_meta=request_meta,
        )
    if not result.get("replayed"):
        sync_housekeeping_after_commit()
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  A move recorded in error (adt-6b, W7-C)
# ═════════════════════════════════════════════════════════════════════════════
#
# The design (section 6): correct_movement writes a NEW 'correction' movement
# pointing at the old one (corrects_movement_id) and restores the earlier place
# through the single location writer; nothing is updated in place and both
# rows stay. Every ledger reader treats a movement a correction points at as
# void (the W4-B ledger rule), so the census never counts a move that did not
# happen.
#
# WHAT CAN BE UNDONE, strictly:
#   * a bed assignment, a bed change or a transfer: the moves a person records
#     on a screen (CORRECTABLE_MOVEMENT_TYPES);
#   * only the stay's LATEST place-changing movement, and only while the
#     patient is still recorded where it put them: anything recorded since is
#     undone by moving the patient, never by unwinding history;
#   * only once. A correction is not itself undone, a swap is undone by
#     swapping back, a bed recorded at admission by the bed route's correction,
#     an end of stay by reopening it.
# The earlier place must still be free (ADT-S1 (a)), and putting the patient
# back into a bed not recorded clean meets ADT-S2 (c) in full, as every
# correction naming another bed does (core.prepare_place_on). Undoing a
# transfer restores the treating department too, so it needs what a transfer
# needs: adt.transfer or patient.write (ruling ADT-S8 held: the ward clerk
# holds adt.bed only), clinical cover for the department it returns to (rule
# C), and a clinical_access row, because who can open the chart changes.
#
# locate_admission_on (services/adt/core.py) writes the correction movement
# with no corrects_movement_id argument, so the pointer is stamped on the row
# it has just written, in the same transaction, before anything commits: the
# row is complete the first time anyone can read it. (A corrects_movement_id
# parameter on locate_admission_on would remove the stamp; core.py was another
# cluster's file in the wave this was built.)

#: The movements a person may undo as recorded in error.
CORRECTABLE_MOVEMENT_TYPES: tuple[str, ...] = ("bed_assigned", "bed_change", "transfer")

#: Movements that change where a stay lies or end it: a later one of these
#: means the movement asked about is no longer the latest.
_PLACE_MOVEMENT_TYPES: tuple[str, ...] = (
    "admission",
    "bed_assigned",
    "bed_change",
    "transfer",
    "bed_swap",
    "location_linked",
    "correction",
    "discharge",
)

_NOT_CORRECTABLE_WORDS = {
    "bed_swap": "A swap is undone by swapping the two patients back.",
    "correction": "A correction cannot itself be undone. Move the patient instead.",
    "admission": (
        "The bed recorded at admission is put right by correcting the bed, not by "
        "undoing the admission."
    ),
    "location_linked": "A link to bed setup is put right in bed setup, not undone here.",
    "discharge": "An end of stay recorded in error is reopened from the end of stay screen.",
}


def _changes_department(mov: dict) -> bool:
    return (
        mov.get("movement_type") == "transfer"
        and bool(_text(mov.get("from_department")))
        and not _same_text(mov.get("from_department"), mov.get("to_department"))
    )


def _at_movement_place(adm: dict, mov: dict) -> bool:
    """The admission is still recorded where `mov` put it."""
    if (adm.get("bed_id") or None) != (mov.get("to_bed_id") or None):
        return False
    if (adm.get("ward_id") or None) != (mov.get("to_ward_id") or None):
        return False
    if not mov.get("to_bed_id") and not _same_text(adm.get("bed"), mov.get("to_bed")):
        return False
    if not mov.get("to_ward_id") and not _same_text(adm.get("ward"), mov.get("to_ward")):
        return False
    if _text(mov.get("to_department")) and not _same_text(
        adm.get("department"), mov.get("to_department")
    ):
        return False
    return True


def _undo_refusal_on(db, mov: dict, adm: dict | None) -> tuple[str, str] | None:
    """Why `mov` cannot be undone as recorded in error, as (code, words), or
    None when it can."""
    kind = mov.get("movement_type")
    if kind not in CORRECTABLE_MOVEMENT_TYPES:
        return (
            "not_correctable",
            _NOT_CORRECTABLE_WORDS.get((kind or ""), "This kind of record cannot be undone here."),
        )
    if adm is None or adm.get("status") != "active":
        return ("admission_not_active", "That stay has ended, so its moves cannot be undone here.")
    if (
        db.execute(
            "SELECT 1 FROM patient_movements WHERE corrects_movement_id = ? LIMIT 1", (mov["id"],)
        ).fetchone()
        is not None
    ):
        return ("already_corrected", "This move has already been undone as recorded in error.")
    marks = ",".join("?" for _ in _PLACE_MOVEMENT_TYPES)
    later = db.execute(
        f"""SELECT 1 FROM patient_movements
             WHERE admission_id = ? AND movement_type IN ({marks})
               AND rowid > (SELECT rowid FROM patient_movements WHERE id = ?) LIMIT 1""",
        (mov.get("admission_id"), *_PLACE_MOVEMENT_TYPES, mov["id"]),
    ).fetchone()
    if later is not None:
        return (
            "not_latest_movement",
            "Only the latest move of this stay can be undone. Something was recorded after "
            "it, so move the patient instead.",
        )
    if not _at_movement_place(adm, mov):
        return (
            "state_changed",
            "Where this patient lies changed since this move was recorded, so move the "
            "patient instead.",
        )
    return None


def last_move_on(db, admission_id: str) -> dict | None:
    """The stay's latest place-changing movement (latest WRITTEN, by rowid,
    never by its back-timeable moved_at) and whether it can be undone as
    recorded in error, for the Move screen. None when the stay has none."""
    marks = ",".join("?" for _ in _PLACE_MOVEMENT_TYPES)
    row = db.execute(
        f"""SELECT * FROM patient_movements
             WHERE admission_id = ? AND movement_type IN ({marks})
             ORDER BY rowid DESC LIMIT 1""",
        (admission_id, *_PLACE_MOVEMENT_TYPES),
    ).fetchone()
    if row is None:
        return None
    mov = dict(row)
    adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
    refusal = _undo_refusal_on(db, mov, adm)
    return {
        "id": mov["id"],
        "movement_type": mov.get("movement_type"),
        "moved_at": mov.get("moved_at"),
        "recorded_at": mov.get("recorded_at"),
        "moved_by_name": mov.get("moved_by_name"),
        "reason": mov.get("reason"),
        "from": {
            "department": mov.get("from_department"),
            "ward": mov.get("from_ward"),
            "bed": mov.get("from_bed"),
        },
        "to": {
            "department": mov.get("to_department"),
            "ward": mov.get("to_ward"),
            "bed": mov.get("to_bed"),
        },
        "changes_department": _changes_department(mov),
        "correctable": refusal is None,
        "not_correctable": ({"code": refusal[0], "message": refusal[1]} if refusal else None),
    }


def movement_admission(movement_id: str) -> dict | None:
    """The admission and patient a movement belongs to, for the route's scope
    check before the write, or None for an unknown movement."""
    with db_session() as db:
        row = db.execute(
            "SELECT id, admission_id, patient_id, movement_type FROM "
            "patient_movements WHERE id = ?",
            (movement_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _correction_replay_on(db, key: str, *, movement_id: str, actor: dict | None) -> dict | None:
    """The movement this person wrote under `key` to undo `movement_id`, or
    None when the key is new; any other use of the key is 409."""
    prior = movement_for_key(db, key)
    if prior is None:
        return None
    if (
        prior.get("movement_type") != "correction"
        or prior.get("corrects_movement_id") != movement_id
        or (prior.get("moved_by") or None) != (_actor_id(actor) or None)
    ):
        raise AdtConflict(
            "That Idempotency-Key was already used for another change",
            code="idempotency_key_reused",
        )
    return prior


def replayed_correction(
    idempotency_key: Any, *, movement_id: str, actor: dict | None
) -> dict | None:
    """A retried correction's answer, read BEFORE the route's scope check, or
    None. Undoing a transfer hands the chart back, so the sender's own retry
    would otherwise meet the scope check and answer 404. Nothing about the
    patient is disclosed: replayed, the admission id and the movement's id,
    type and time. Any other use of the key falls through to the normal
    checks."""
    key = _text(idempotency_key)
    if not key:
        return None
    with db_session() as db:
        prior = movement_for_key(db, key)
    if (
        prior is None
        or prior.get("movement_type") != "correction"
        or prior.get("corrects_movement_id") != movement_id
        or (prior.get("moved_by") or None) != (_actor_id(actor) or None)
    ):
        return None
    return {
        "replayed": True,
        "admission_id": prior.get("admission_id"),
        "corrects_movement_id": movement_id,
        "movement": {
            "id": prior["id"],
            "movement_type": prior["movement_type"],
            "moved_at": prior.get("moved_at"),
        },
    }


def correct_movement_on(
    db,
    *,
    movement_id: str,
    actor: dict | None,
    reason: Any,
    restore_department: str | None = None,
    expected_bed_id: Any = _UNSET,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_confirmed: dict | None = None,
    notes: str | None = None,
    idempotency_key: str,
    request_meta: dict | None = None,
) -> dict:
    """Undo one movement recorded in error, on the caller's immediate
    connection. `restore_department` is the department a transfer goes back
    to, ALREADY resolved against clinical cover (rule C) before the session
    opened; it is required for a transfer that changed the department."""
    prior = _correction_replay_on(db, idempotency_key, movement_id=movement_id, actor=actor)
    if prior is not None:
        return {
            **_result_on(db, prior["admission_id"], prior, replayed=True),
            "corrects_movement_id": movement_id,
        }
    mov = _row(
        db.execute("SELECT * FROM patient_movements WHERE id = ?", (movement_id,)).fetchone()
    )
    if mov is None:
        raise AdtNotFound("No movement with that id", code="movement_not_found")
    adm = _active_admission_on(db, admission_id=mov.get("admission_id"), patient_id=None)
    if not _in_scope_on(db, adm["patient_id"], actor):
        raise AdtNotFound("No admission with that id", code="admission_not_found")
    refusal = _undo_refusal_on(db, mov, adm)
    if refusal is not None:
        raise AdtConflict(refusal[1], code=refusal[0], movement_id=movement_id)
    changes_dept = _changes_department(mov)
    if changes_dept and not _text(restore_department):
        raise AdtForbidden(
            f"Undoing this transfer gives the patient back to {mov.get('from_department')}, "
            "which changes who can open the chart. Your role cannot change the treating "
            "department.",
            code="transfer_permission_required",
        )
    _check_expected(adm, expected_bed_id)
    why = _require_reason(reason, "correction")
    treating = restore_department if changes_dept else adm.get("department")
    try:
        prepared = prepare_place_on(
            db,
            admission_id=adm["id"],
            actor=actor,
            movement_type="correction",
            ward_id=mov.get("from_ward_id"),
            bed_id=mov.get("from_bed_id"),
            ward_text=mov.get("from_ward"),
            bed_text=mov.get("from_bed"),
            out_of_service_reason=out_of_service_reason,
            not_clean_reason=not_clean_reason,
            terminal_clean_authorised_by=terminal_clean_authorised_by,
            terminal_clean_reason=terminal_clean_reason,
            request_meta=request_meta,
            treating_department=treating,
            terminal_clean_confirmed=terminal_clean_confirmed,
        )
    except AdtConflict as err:
        if err.code == "bed_occupied":
            raise AdtConflict(
                "Another patient now lies in the bed this patient was moved from, so the move "
                "cannot be undone. Move the patient instead.",
                code="bed_occupied",
                extra_bed_offered=False,
                movement_id=movement_id,
            ) from err
        raise
    prepared["detail"] = {
        **(prepared.get("detail") or {}),
        "recorded_in_error": True,
        "corrects_movement_id": mov["id"],
        "corrected_movement_type": mov.get("movement_type"),
    }
    located = place_admission_on(
        db,
        admission_id=adm["id"],
        actor=actor,
        movement_type="correction",
        reason=f"Recorded in error: {why}",
        prepared=prepared,
        boarder_acknowledged=boarder_acknowledged,
        expected_bed_id=expected_bed_id,
        idempotency_key=idempotency_key,
        notes=notes,
        request_meta=request_meta,
        to_department=treating if changes_dept else None,
    )
    written = located["movement"]
    # The pointer, on the row just written, before anything commits (see the
    # section comment).
    db.execute(
        "UPDATE patient_movements SET corrects_movement_id = ? "
        "WHERE id = ? AND corrects_movement_id IS NULL",
        (mov["id"], written["id"]),
    )
    correction = movement_for_key(db, idempotency_key) or written
    if changes_dept:
        audit_on(
            db,
            category="clinical_access",
            action="patient_transfer_corrected",
            summary=(
                f"{adm['patient_id']} transfer to {mov.get('to_department')} recorded in "
                f"error; treated by {treating} again: {why}"
            ),
            actor=actor,
            patient_id=adm["patient_id"],
            target_type="patient_movement",
            target_id=correction["id"],
            request_meta=request_meta,
            correlation_id=f"transfer-correction-{correction['id']}",
            metadata={
                "corrects_movement_id": mov["id"],
                "from_department": mov.get("to_department"),
                "to_department": treating,
                "admission_id": adm["id"],
                "admission_status": "active",
                "movement_id": correction["id"],
            },
        )
    return {
        **_result_on(db, adm["id"], correction, replayed=False),
        "corrects_movement_id": mov["id"],
    }


def correct_movement(
    movement_id: str,
    *,
    actor: dict,
    reason: Any,
    expected_bed_id: Any = _UNSET,
    may_change_department: bool = False,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_authoriser_pin: Any = None,
    notes: str | None = None,
    idempotency_key: Any = None,
    request_meta: dict | None = None,
) -> dict:
    """Undo a move recorded in error in ONE immediate session (POST
    /api/adt/movements/{id}/correct). The Idempotency-Key, the reason and the
    bed the screen showed are required. Undoing a transfer that changed the
    department needs `may_change_department` (adt.transfer or patient.write,
    decided by the route) and clinical cover for that department, resolved
    here before the session opens."""
    from services import housekeeping_service as hk

    key = _require_key(idempotency_key)
    _require_reason(reason, "correction")
    if expected_bed_id is _UNSET:
        raise AdtError(
            "A correction must say which bed the screen showed (expected_bed_id), "
            "so it never overwrites a change made since.",
            code="expected_bed_required",
        )
    with db_session() as db:
        prior = _correction_replay_on(db, key, movement_id=movement_id, actor=actor)
        if prior is not None:
            return {
                **_result_on(db, prior["admission_id"], prior, replayed=True),
                "corrects_movement_id": movement_id,
            }
        mov = _row(
            db.execute("SELECT * FROM patient_movements WHERE id = ?", (movement_id,)).fetchone()
        )
    if mov is None:
        raise AdtNotFound("No movement with that id", code="movement_not_found")
    restore = None
    if _changes_department(mov):
        if not may_change_department:
            raise AdtForbidden(
                f"Undoing this transfer gives the patient back to {mov.get('from_department')}, "
                "which changes who can open the chart. Your role cannot change the treating "
                "department.",
                code="transfer_permission_required",
            )
        restore = resolve_cover(mov.get("from_department"))
    hk.ensure_housekeeping_schema()
    confirmed = confirm_terminal_authoriser(
        actor=actor,
        authorised_by=terminal_clean_authorised_by,
        pin=terminal_clean_authoriser_pin,
        admission_id=mov.get("admission_id"),
        bed_id=mov.get("from_bed_id"),
        request_meta=request_meta,
    )
    with db_session(immediate=True) as db:
        result = correct_movement_on(
            db,
            movement_id=movement_id,
            actor=actor,
            reason=reason,
            restore_department=restore,
            expected_bed_id=expected_bed_id,
            out_of_service_reason=out_of_service_reason,
            boarder_acknowledged=boarder_acknowledged,
            not_clean_reason=not_clean_reason,
            terminal_clean_authorised_by=terminal_clean_authorised_by,
            terminal_clean_reason=terminal_clean_reason,
            terminal_clean_confirmed=confirmed,
            notes=notes,
            idempotency_key=key,
            request_meta=request_meta,
        )
    if not result.get("replayed"):
        sync_housekeeping_after_commit()
    return result
