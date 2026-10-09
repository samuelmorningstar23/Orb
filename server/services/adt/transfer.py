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
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from database import SERVICE_ACCOUNT_IDS, db_session
from services.adt.core import (
    _UNSET,
    TERMINAL_CLEAN_AUTHORISER_ROLES,
    AdtConflict,
    AdtError,
    AdtNotFound,
    _actor_id,
    _bed_by_label,
    _row,
    _text,
    audit_on,
    bed_choices_on,
    check_event_time,
    confirm_terminal_authoriser,
    current_admission_on,
    current_location,
    locate_admission_on,
    match_location_on,
    movement_for_key,
    parse_instant,
    place_admission_on,
    prepare_place_on,
    terminal_authoriser_eligible_on,
)
from services.clinical_time import to_utc_iso

logger = logging.getLogger("orb.adt.transfer")

__all__ = [
    "transfer", "transfer_on", "change_bed", "change_bed_on", "roster_move_on",
    "resolve_cover", "move_choices", "sync_housekeeping_after_commit", "replayed_answer",
    "admission_location",
]

_MAX_REASON = 2000


# ═════════════════════════════════════════════════════════════════════════════
#  Rule C, resolved before any immediate session
# ═════════════════════════════════════════════════════════════════════════════

def _uncovered(to_department: Any) -> AdtError:
    from services import frontdesk_service as fd

    return AdtError(fd.uncovered_department_words(to_department),
                    code="department_not_covered", department=_text(to_department))


def resolve_cover(to_department: Any, *, covering: Optional[list] = None) -> str:
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
        raise AdtError("This move needs an Idempotency-Key header, so a retried request is "
                       "never recorded twice.", code="idempotency_key_required")
    return key


def _require_reason(reason: Any, what: str) -> str:
    why = _text(reason, _MAX_REASON)
    if not why:
        raise AdtError(f"A {what} needs a reason. A move with no reason is not a record.",
                       code="reason_required")
    return why


def _active_admission_on(db, *, admission_id: Optional[str], patient_id: Optional[str]) -> dict:
    """The admission to move: by its id, or the patient's active one."""
    if admission_id:
        adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
        if adm is None:
            raise AdtNotFound("No admission with that id", code="admission_not_found")
    else:
        adm = current_admission_on(db, patient_id or "")
        if adm is None:
            exists = db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id or "",)).fetchone()
            if exists is None:
                raise AdtNotFound(f"No such patient: {patient_id}", code="patient_not_found")
            raise AdtError(f"{patient_id} has no active admission: an outpatient cannot be "
                           "transferred between wards", code="admission_not_active")
    if adm.get("status") != "active":
        # Rule B: an ended stay is never moved, and its status is never written.
        raise AdtConflict("That admission has ended, so the patient cannot be moved",
                          code="admission_not_active", status=adm.get("status"))
    return adm


def _replayed_on(db, key: str, *, adm: dict, actor: Optional[dict], movement_type: str):
    """The first result for a replayed key, or None when the key is new.
    A key is bound to one admission, one person and one kind of move."""
    prior = movement_for_key(db, key)
    if prior is None:
        return None
    if (prior.get("admission_id") != adm["id"]
            or (prior.get("moved_by") or None) != (_actor_id(actor) or None)
            or prior.get("movement_type") != movement_type):
        raise AdtConflict("That Idempotency-Key was already used for another change",
                          code="idempotency_key_reused")
    return _result_on(db, adm["id"], prior, replayed=True)


def _check_expected(adm: dict, expected_bed_id: Any) -> None:
    if expected_bed_id is not _UNSET and (expected_bed_id or None) != (adm.get("bed_id") or None):
        raise AdtConflict(
            "Where this patient lies changed since the screen was loaded. Reload and try again.",
            code="state_changed", expected_bed_id=expected_bed_id,
            current_bed_id=adm.get("bed_id"))


def _event_time(adm: dict, moved_at: Any, what: str) -> str:
    when = check_event_time(moved_at, what=what,
                            not_before=parse_instant(adm.get("admitted_at") or adm.get("created_at")))
    return to_utc_iso(when)


def _result_on(db, admission_id: str, movement: dict, *, replayed: bool) -> dict:
    """The move's answer: the movement and where the admission is after it.
    The keys of the front desk's transfer answer are all here (patient_name,
    admission_status, patient_status, current_department, current_ward,
    current_bed), so a delegated front-desk call answers a superset."""
    adm = _row(db.execute(
        "SELECT id, patient_id, department, ward, bed, ward_id, bed_id, status "
        "FROM admissions WHERE id = ?", (admission_id,)).fetchone()) or {}
    patient = _row(db.execute("SELECT name, status FROM patients WHERE id = ?",
                              (adm.get("patient_id") or movement.get("patient_id"),)).fetchone()) or {}
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


def _transfer_audit_on(db, *, adm: dict, movement: dict, actor: Optional[dict], reason: str,
                       request_meta: Optional[dict]) -> None:
    """clinical_access 'patient_transferred' (fail-closed), in the move's own
    transaction: the treating department changed, so who can read the chart
    changed. Written for a department change only; a move inside one
    department is recorded by the writer's patient_movement row."""
    audit_on(
        db, category="clinical_access", action="patient_transferred",
        summary=(f"{adm['patient_id']} transferred from {movement.get('from_department') or 'unknown'} "
                 f"to {movement.get('to_department')}: {reason}"),
        actor=actor, patient_id=adm["patient_id"], target_type="patient_movement",
        target_id=movement["id"], request_meta=request_meta,
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
        logger.warning("housekeeping sync after an ADT move failed; the move stands",
                       exc_info=True)


def replayed_answer(idempotency_key: Any, *, admission_id: str, actor: Optional[dict],
                    movement_type: str) -> Optional[dict]:
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
    if (prior is None or prior.get("admission_id") != admission_id
            or (prior.get("moved_by") or None) != (_actor_id(actor) or None)
            or prior.get("movement_type") != movement_type):
        return None
    return {"replayed": True, "admission_id": admission_id,
            "movement": {"id": prior["id"], "movement_type": prior["movement_type"],
                         "moved_at": prior.get("moved_at")}}


# ═════════════════════════════════════════════════════════════════════════════
#  Transfer
# ═════════════════════════════════════════════════════════════════════════════

def transfer_on(
    db, *, actor: Optional[dict], to_department: str, reason: str,
    admission_id: Optional[str] = None, patient_id: Optional[str] = None,
    ward_id: Any = None, bed_id: Any = None, ward_text: Any = None, bed_text: Any = None,
    awaiting_bed: bool = False, keep_location: bool = False, extra_bed: bool = False,
    expected_bed_id: Any = _UNSET, moved_at: Any = None,
    out_of_service_reason: Optional[str] = None, boarder_acknowledged: bool = False,
    not_clean_reason: Any = None, terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None, terminal_clean_confirmed: Optional[dict] = None,
    notes: Optional[str] = None, idempotency_key: str, request_meta: Optional[dict] = None,
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
            raise AdtError("Keeping the current bed names no other place. Choose either the "
                           "current bed or a new place.", code="keep_location_with_place")
        if to_department == adm.get("department"):
            raise AdtConflict(f"This patient is already treated by {to_department} and stays "
                              "where they are, so there is nothing to record.", code="no_change")
        located = locate_admission_on(
            db, admission_id=adm["id"], actor=actor, movement_type="transfer", reason=why,
            ward_id=adm.get("ward_id"), bed_id=adm.get("bed_id"), ward_text=adm.get("ward"),
            bed_text=adm.get("bed"), expected_bed_id=expected_bed_id,
            out_of_service_reason=out_of_service_reason,
            boarder_acknowledged=boarder_acknowledged, moved_at=when,
            idempotency_key=idempotency_key, notes=notes, request_meta=request_meta,
            extra_detail={"kept_place": True}, to_department=to_department)
    else:
        if awaiting_bed and (has_bed or extra_bed):
            raise AdtError("A patient waiting for a bed has no bed to name. Choose a bed or "
                           "No bed yet, not both.", code="awaiting_bed_with_bed")
        if not awaiting_bed and not has_bed:
            raise AdtError("Choose the bed on the receiving ward, No bed yet, or keep the "
                           "current bed.", code="bed_required")
        if _text(bed_text) and not (_text(bed_id) or _text(ward_id) or _text(ward_text)):
            # Bed text alone names a bed on the ward the patient is on, as a
            # bed change and an assignment read it.
            if _text(adm.get("ward_id")):
                ward_id = adm["ward_id"]
            elif _text(adm.get("ward")):
                ward_text = adm["ward"]
            else:
                raise AdtError("Choose the receiving ward as well. A bed label on its own "
                               "names no place.", code="ward_required")
        prepared = prepare_place_on(
            db, admission_id=adm["id"], actor=actor, movement_type="transfer",
            ward_id=ward_id, bed_id=None if awaiting_bed else bed_id, ward_text=ward_text,
            bed_text=None if awaiting_bed else bed_text, extra_bed=extra_bed,
            out_of_service_reason=out_of_service_reason, not_clean_reason=not_clean_reason,
            terminal_clean_authorised_by=terminal_clean_authorised_by,
            terminal_clean_reason=terminal_clean_reason, request_meta=request_meta,
            treating_department=to_department, terminal_clean_confirmed=terminal_clean_confirmed)
        located = place_admission_on(
            db, admission_id=adm["id"], actor=actor, movement_type="transfer", reason=why,
            prepared=prepared, boarder_acknowledged=boarder_acknowledged,
            expected_bed_id=expected_bed_id, moved_at=when, idempotency_key=idempotency_key,
            notes=notes, request_meta=request_meta, to_department=to_department)

    movement = located["movement"]
    if (movement.get("from_department") or None) != (movement.get("to_department") or None):
        _transfer_audit_on(db, adm=adm, movement=movement, actor=actor, reason=why,
                           request_meta=request_meta)
    return _result_on(db, adm["id"], movement, replayed=False)


def transfer(
    admission_id: Optional[str] = None, *, actor: dict, to_department: Any, reason: Any,
    patient_id: Optional[str] = None, ward_id: Any = None, bed_id: Any = None,
    ward_text: Any = None, bed_text: Any = None, awaiting_bed: bool = False,
    keep_location: bool = False, extra_bed: bool = False, expected_bed_id: Any = _UNSET,
    require_expected_bed: bool = True, moved_at: Any = None,
    out_of_service_reason: Optional[str] = None, boarder_acknowledged: bool = False,
    not_clean_reason: Any = None, terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None, terminal_clean_authoriser_pin: Any = None,
    notes: Optional[str] = None, idempotency_key: Any = None,
    request_meta: Optional[dict] = None,
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
        raise AdtError("A move must say which bed the screen showed (expected_bed_id), so it "
                       "never overwrites a change made since.", code="expected_bed_required")
    canonical = resolve_cover(to_department)
    hk.ensure_housekeeping_schema()
    confirmed = confirm_terminal_authoriser(
        actor=actor, authorised_by=terminal_clean_authorised_by,
        pin=terminal_clean_authoriser_pin, admission_id=admission_id, bed_id=bed_id,
        request_meta=request_meta)
    with db_session(immediate=True) as db:
        result = transfer_on(
            db, actor=actor, to_department=canonical, reason=reason, admission_id=admission_id,
            patient_id=patient_id, ward_id=ward_id, bed_id=bed_id, ward_text=ward_text,
            bed_text=bed_text, awaiting_bed=awaiting_bed, keep_location=keep_location,
            extra_bed=extra_bed, expected_bed_id=expected_bed_id, moved_at=moved_at,
            out_of_service_reason=out_of_service_reason,
            boarder_acknowledged=boarder_acknowledged, not_clean_reason=not_clean_reason,
            terminal_clean_authorised_by=terminal_clean_authorised_by,
            terminal_clean_reason=terminal_clean_reason, terminal_clean_confirmed=confirmed,
            notes=notes, idempotency_key=key, request_meta=request_meta)
    if not result.get("replayed"):
        sync_housekeeping_after_commit()
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Bed change (same treating department)
# ═════════════════════════════════════════════════════════════════════════════

def change_bed_on(
    db, *, admission_id: str, actor: Optional[dict], reason: str, ward_id: Any = None,
    bed_id: Any = None, ward_text: Any = None, bed_text: Any = None, extra_bed: bool = False,
    expected_bed_id: Any = _UNSET, moved_at: Any = None,
    out_of_service_reason: Optional[str] = None, boarder_acknowledged: bool = False,
    not_clean_reason: Any = None, terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None, terminal_clean_confirmed: Optional[dict] = None,
    notes: Optional[str] = None, idempotency_key: str, request_meta: Optional[dict] = None,
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
        raise AdtConflict("This patient has no bed recorded, so there is no bed to change. "
                          "Assign the bed instead.", code="no_bed_to_change")
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
            raise AdtError("Choose the ward as well. This patient has no ward recorded, so a "
                           "bed label on its own names no place.", code="ward_required")
    prepared = prepare_place_on(
        db, admission_id=adm["id"], actor=actor, movement_type="bed_change", ward_id=ward_id,
        bed_id=bed_id, ward_text=ward_text, bed_text=bed_text, extra_bed=extra_bed,
        out_of_service_reason=out_of_service_reason, not_clean_reason=not_clean_reason,
        terminal_clean_authorised_by=terminal_clean_authorised_by,
        terminal_clean_reason=terminal_clean_reason, request_meta=request_meta,
        treating_department=adm.get("department"), terminal_clean_confirmed=terminal_clean_confirmed)
    located = place_admission_on(
        db, admission_id=adm["id"], actor=actor, movement_type="bed_change", reason=why,
        prepared=prepared, boarder_acknowledged=boarder_acknowledged,
        expected_bed_id=expected_bed_id, moved_at=when, idempotency_key=idempotency_key,
        notes=notes, request_meta=request_meta)
    return _result_on(db, adm["id"], located["movement"], replayed=False)


def change_bed(
    admission_id: str, *, actor: dict, reason: Any, ward_id: Any = None, bed_id: Any = None,
    ward_text: Any = None, bed_text: Any = None, extra_bed: bool = False,
    expected_bed_id: Any = _UNSET, moved_at: Any = None,
    out_of_service_reason: Optional[str] = None, boarder_acknowledged: bool = False,
    not_clean_reason: Any = None, terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None, terminal_clean_authoriser_pin: Any = None,
    notes: Optional[str] = None, idempotency_key: Any = None,
    request_meta: Optional[dict] = None,
) -> dict:
    """Change an admitted patient's bed in ONE immediate session (POST
    /api/adt/admissions/{id}/bed-change). The Idempotency-Key, the reason and
    the bed the screen showed (expected_bed_id) are required."""
    from services import housekeeping_service as hk

    key = _require_key(idempotency_key)
    _require_reason(reason, "bed change")
    if expected_bed_id is _UNSET:
        raise AdtError("A move must say which bed the screen showed (expected_bed_id), so it "
                       "never overwrites a change made since.", code="expected_bed_required")
    hk.ensure_housekeeping_schema()
    confirmed = confirm_terminal_authoriser(
        actor=actor, authorised_by=terminal_clean_authorised_by,
        pin=terminal_clean_authoriser_pin, admission_id=admission_id, bed_id=bed_id,
        request_meta=request_meta)
    with db_session(immediate=True) as db:
        result = change_bed_on(
            db, admission_id=admission_id, actor=actor, reason=reason, ward_id=ward_id,
            bed_id=bed_id, ward_text=ward_text, bed_text=bed_text, extra_bed=extra_bed,
            expected_bed_id=expected_bed_id, moved_at=moved_at,
            out_of_service_reason=out_of_service_reason,
            boarder_acknowledged=boarder_acknowledged, not_clean_reason=not_clean_reason,
            terminal_clean_authorised_by=terminal_clean_authorised_by,
            terminal_clean_reason=terminal_clean_reason, terminal_clean_confirmed=confirmed,
            notes=notes, idempotency_key=key, request_meta=request_meta)
    if not result.get("replayed"):
        sync_housekeeping_after_commit()
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Roster import updates
# ═════════════════════════════════════════════════════════════════════════════

def roster_move_on(
    db, *, admission_id: str, actor: Optional[dict], changes: dict, covering: list,
    reason: str, expected_bed_id: Any = _UNSET,
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
    if dept_change is not None and str(dept_change).strip() and dept_change != adm.get("department"):
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
            raise AdtConflict("The file names the place and department already recorded.",
                              code="no_change")
        located = locate_admission_on(
            db, admission_id=adm["id"], actor=actor, movement_type="transfer", reason=reason,
            ward_id=adm.get("ward_id"), bed_id=adm.get("bed_id"), ward_text=adm.get("ward"),
            bed_text=adm.get("bed"), expected_bed_id=expected_bed_id, idempotency_key=key,
            extra_detail={"kept_place": True, "door": "roster_import"},
            to_department=to_department)
    else:
        # A new ward with the bed label unchanged in the file names that label
        # on the NEW ward (the file says ward X, bed 3): bed_text above is the
        # recorded label then.
        prepared = prepare_place_on(
            db, admission_id=adm["id"], actor=actor, movement_type=movement_type,
            ward_text=ward_text, bed_text=bed_text,
            treating_department=to_department or adm.get("department"))
        prepared["detail"] = {**(prepared.get("detail") or {}), "door": "roster_import"}
        located = place_admission_on(
            db, admission_id=adm["id"], actor=actor, movement_type=movement_type, reason=reason,
            prepared=prepared, expected_bed_id=expected_bed_id, idempotency_key=key,
            to_department=to_department)
    movement = located["movement"]
    if to_department is not None:
        _transfer_audit_on(db, adm=adm, movement=movement, actor=actor, reason=reason,
                           request_meta=None)
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
    ward: Optional[dict] = None
    bed: Optional[dict] = None
    bed_id, ward_id = _text(loc.get("bed_id")), _text(loc.get("ward_id"))
    if bed_id:
        bed = _row(db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ? AND ward_id IS NOT NULL",
            (bed_id,)).fetchone())
        if bed is not None:
            ward = _row(db.execute("SELECT * FROM wards WHERE id = ?",
                                   (bed["ward_id"],)).fetchone())
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


def admission_location(admission_id: str, *, actor: Optional[dict],
                       request_meta: Optional[dict] = None) -> dict:
    """Where an active admission lies now, for the Move screen to send back
    as expected_bed_id: its treating department, ward and bed (ids and the
    master's spelling), and whether it is linked, awaiting a bed or a boarder.
    `kept_place` says what keeping that place on a transfer resolves to
    (_kept_place_on): the ward's code and department and whether the bed is
    recorded out of service. No name and no MRN. The caller has checked the
    patient is in scope. The read is audited phi_access (fail-closed) in its
    own session: an unwritable row means no answer."""
    with db_session() as db:
        adm = _row(db.execute("SELECT patient_id, status FROM admissions WHERE id = ?",
                              (admission_id,)).fetchone())
        if adm is None:
            raise AdtNotFound("No admission with that id", code="admission_not_found")
        if adm.get("status") != "active":
            raise AdtConflict("That admission has ended, so the patient cannot be moved",
                              code="admission_not_active", status=adm.get("status"))
        loc = current_location(adm["patient_id"], db=db) or {}
        if loc.get("admission_id") != admission_id:
            raise AdtConflict("That admission is not the patient's current stay",
                              code="admission_not_active")
        kept = _kept_place_on(db, loc)
        audit_on(db, category="phi_access", action="admission_location_viewed",
                 summary=f"Where admission {admission_id} lies, read for a move",
                 actor=actor, patient_id=adm["patient_id"], target_type="admission",
                 target_id=admission_id, request_meta=request_meta)
    return {
        "admission_id": admission_id,
        "department": loc.get("department"),
        "ward": loc.get("ward"), "bed": loc.get("bed"),
        "ward_id": loc.get("ward_id"), "bed_id": loc.get("bed_id"),
        "ward_department": loc.get("ward_department"),
        "linked": bool(loc.get("linked")), "awaiting_bed": bool(loc.get("awaiting_bed")),
        "boarder": bool(loc.get("boarder")),
        "kept_place": kept,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  What the Move screen offers
# ═════════════════════════════════════════════════════════════════════════════

def move_choices(user: Optional[dict]) -> dict:
    """The Move screen's lists, with NO patient data: every active ward and
    its beds with what placing a patient there needs (core.bed_choices_on,
    the same lists the admission form's bed picker reads), who may authorise
    a bed awaiting a terminal clean (the caller when their role allows; the
    others only with a signing PIN set), the back-time window, and the
    departments a transfer may go to (those with clinical cover, rule C)."""
    import config
    from services import frontdesk_service as fd
    from services import housekeeping_service as hk

    # Each opens a session of its own, so both run before the read below.
    hk.ensure_housekeeping_schema()
    departments = fd.list_covering_departments()
    me = str((user or {}).get("id") or "")
    service_ids = sorted(SERVICE_ACCOUNT_IDS)
    role_marks = ",".join("?" for _ in TERMINAL_CLEAN_AUTHORISER_ROLES)
    service_marks = ",".join("?" for _ in service_ids)
    with db_session() as db:
        wards = bed_choices_on(db)
        authorisers = [
            {"id": r["id"], "name": r["name"], "role": r["role"],
             "department": r["department"] or None}
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
                "FROM users WHERE id = ?", (me,)).fetchone()
            if row is not None:
                self_authoriser = {"id": row["id"], "name": row["name"], "role": row["role"],
                                   "department": row["department"] or None}
    return {
        "master": {
            "wards": wards,
            "authorisers": authorisers,
            "self_authoriser": self_authoriser,
            "backtime_hours": int(getattr(config, "ADT_BACKTIME_HOURS", 72)),
        },
        "departments": departments,
    }
