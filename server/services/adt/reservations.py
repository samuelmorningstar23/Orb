"""Orb: bed reservations (adt-12, W7-D). A bed held for a patient on the way.

A bed manager, a ward clerk or a doctor holds a master bed for a patient who
is coming: from the emergency room (purpose 'admission'), from another ward
('transfer'), back from theatre ('post_op'), for a planned admission
('elective'), or for anything else ('other'). The table is migration 69
(migrations_his/m0069_bed_reservations.py); this module is its only writer.

  reserve_bed(data, ...)             hold a free bed (one immediate session)
  release_reservation(id, ...)       let it go, with a reason
  expire_due_on(db, ...)             record the holds whose time ran out
  fulfil_on(db, ...)                 the reserved patient was placed in the bed
                                     (called by core.locate_admission_on)

THE RULES, each refused with words before anything is written:
  * One active reservation per bed (the partial unique index refuses a second
    underneath this module's own check: 409 bed_reserved).
  * Only a free master bed can be held: not one a patient lies in (by bed id
    or by typed ward and bed text naming it, 409 bed_occupied), not a retired
    or switched-off bed or ward, not a bed housekeeping records out of
    service.
  * A transfer or a return from theatre names the admission it is for. Any
    other purpose may name a patient, or nobody (a bed held for a patient not
    yet in Orb). A patient or admission holds at most one active reservation
    (409 patient_already_reserved), so nobody holds several beds at once.
  * Expiry (ruling ADT-P9): ORB_ADT_RESERVATION_MINUTES (120) for an
    admission, a transfer and other; ORB_ADT_RESERVATION_ELECTIVE_MINUTES
    (1440, 24 h) for elective and post_op. A shorter hold may be asked for,
    never a longer one.
  * PLACING A PATIENT (core.locate_admission_on, the single location writer,
    and core.prepare_place_on before it): a bed held for this admission, or
    for this patient, is fulfilled in the same transaction as the move; a bed
    held for anybody else, or for nobody named, is refused (409 bed_reserved)
    and the words never name who it is held for. Release the reservation
    first.
  * Retiring a held bed is refused (bed_master_service.retire_bed).

AN ACTIVE ROW PAST ITS expires_at HOLDS NOTHING. Every reader goes through
live_on_beds / live_on_bed here, which compare expires_at with the clock, so
an expired hold frees the bed the moment its time runs out, with no sweep. The
next reservation write records it as 'expired' (expire_due_on), so the row's
status catches up and the unique index never trips on it.

SCOPE. A bed carries no patient data, so a reservation for a patient the
caller names may be made on any active ward; one for nobody named only on a
ward of the caller's own department (or by an account with no department).
The patient or admission it names must be inside the caller's department: the
route checks every patient the request names (404 outside it, audited), and
reserve_bed checks the patient the row will name once more inside its own
transaction (ReservationOutOfScope, which the route audits too). The bed board
names the patient a bed is held for only inside the caller's scope.

AUDIT. bed_reserved, bed_reservation_released, bed_reservation_fulfilled and
bed_reservation_expired, category patient_movement (fail-closed), inside the
write's own transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import config
from database import db_session
from db_backend import DBIntegrityError
from migrations_his.m0069_bed_reservations import PURPOSES, STATUSES

from services.adt.core import (
    AdtConflict,
    AdtError,
    AdtNotFound,
    _actor_id,
    _holder_on,
    _row,
    _text,
    audit_on,
    parse_instant,
)
from services.clinical_time import format_utc

__all__ = [
    "PURPOSES",
    "STATUSES",
    "PURPOSE_WORDS",
    "NEEDS_ADMISSION",
    "LONG_HOLD_PURPOSES",
    "table_ready",
    "expiry_minutes",
    "live_on_beds",
    "live_on_bed",
    "is_for",
    "live_for_patient_on",
    "expire_due_on",
    "fulfil_on",
    "refusal",
    "reserve_bed",
    "release_reservation",
    "reservation_facts",
    "list_reservations",
    "reservation_out",
    "ReservationOutOfScope",
    "OUT_OF_SCOPE_WORDS",
]

_MAX_NOTE = 500
_MAX_REASON = 500

#: What a bed held for each purpose is called on the board.
PURPOSE_WORDS: dict[str, str] = {
    "admission": "Reserved for an admission",
    "transfer": "Reserved for transfer",
    "post_op": "Reserved for a patient coming back from surgery",
    "elective": "Reserved for a planned admission",
    "other": "Reserved",
}

#: These name the admission they are for: the patient is admitted already.
NEEDS_ADMISSION = frozenset({"transfer", "post_op"})

#: These hold the bed for ORB_ADT_RESERVATION_ELECTIVE_MINUTES (ADT-P9).
LONG_HOLD_PURPOSES = frozenset({"elective", "post_op"})

FULFILLED_REASON = "Patient placed in the bed"
EXPIRED_REASON = "Expired"

#: The words of every out-of-scope refusal on this module's routes. They never
#: say whether the patient exists.
OUT_OF_SCOPE_WORDS = "No such patient or reservation on your wards."


class ReservationOutOfScope(AdtNotFound):
    """The patient a reservation would name is outside the caller's department,
    found by the check inside the write's own transaction. 404 with the same
    words as the route's own refusal; the patient id travels as an attribute,
    never in the answer, so the route can audit the attempt after the rollback."""

    def __init__(self, patient_id: str):
        super().__init__(OUT_OF_SCOPE_WORDS, code="not_found")
        self.patient_id = patient_id


def _now() -> datetime:
    return datetime.now(UTC)


def table_ready(db) -> bool:
    """True once migration 69 has created the table."""
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bed_reservations'"
        ).fetchone()
        is not None
    )


def expiry_minutes(purpose: str) -> int:
    """How long a reservation for `purpose` holds the bed by default (ruling
    ADT-P9), read from config at call time."""
    if purpose in LONG_HOLD_PURPOSES:
        return int(getattr(config, "ADT_RESERVATION_ELECTIVE_MINUTES", 1440))
    return int(getattr(config, "ADT_RESERVATION_MINUTES", 120))


def _is_live(row: dict, now: datetime) -> bool:
    if row.get("status") != "active":
        return False
    until = parse_instant(row.get("expires_at"))
    # An unreadable expiry holds nothing rather than holding forever.
    return until is not None and until > now


def live_on_beds(db, bed_ids: Iterable[Any], *, now: datetime | None = None) -> dict:
    """bed id -> the reservation holding it now (active and not past its
    expiry), for the beds asked about. {} before migration 69."""
    ids = tuple(dict.fromkeys(b for b in bed_ids if b))
    if not ids or not table_ready(db):
        return {}
    now = now or _now()
    marks = ",".join("?" for _ in ids)
    out: dict[str, dict] = {}
    for row in db.execute(
        f"SELECT * FROM bed_reservations WHERE status = 'active' AND bed_id IN ({marks})", ids
    ).fetchall():
        res = dict(row)
        if _is_live(res, now):
            out[res["bed_id"]] = res
    return out


def live_on_bed(db, bed_id: Any, *, now: datetime | None = None) -> dict | None:
    """The reservation holding this bed now, or None."""
    return live_on_beds(db, (bed_id,), now=now).get(bed_id)


def is_for(reservation: dict, *, admission_id: str | None, patient_id: str | None) -> bool:
    """True when the reservation is held for this admission, or (when it names
    no admission) for this patient. A reservation naming nobody is for nobody:
    it is placeable only once released."""
    held_for = reservation.get("admission_id")
    if held_for:
        return bool(admission_id) and held_for == admission_id
    patient = reservation.get("patient_id")
    return bool(patient) and bool(patient_id) and patient == patient_id


def live_for_patient_on(
    db, *, patient_id: str | None, admission_id: str | None, now: datetime | None = None
) -> dict | None:
    """The live reservation already held for this patient or admission."""
    if not table_ready(db) or not (patient_id or admission_id):
        return None
    now = now or _now()
    for row in db.execute(
        "SELECT * FROM bed_reservations WHERE status = 'active' "
        "AND (patient_id = ? OR admission_id = ?)",
        (patient_id, admission_id),
    ).fetchall():
        res = dict(row)
        if _is_live(res, now):
            return res
    return None


def refusal(*, ward: dict | None, bed: dict, reservation: dict) -> AdtConflict:
    """409 bed_reserved for a bed held for somebody else. Never names who."""
    where = f"Bed {bed.get('bed_label')} on {(ward or {}).get('code') or bed.get('ward')}"
    return AdtConflict(
        f"{where} is reserved for another patient. Choose another bed, or release the "
        "reservation first.",
        code="bed_reserved",
        bed_id=bed.get("id"),
        reservation_id=reservation.get("id"),
        purpose=reservation.get("purpose"),
        expires_at=reservation.get("expires_at"),
    )


def _audit(
    db,
    action: str,
    summary: str,
    *,
    actor: dict | None,
    reservation: dict,
    request_meta: dict | None,
    extra: dict | None = None,
) -> None:
    audit_on(
        db,
        category="patient_movement",
        action=action,
        summary=summary,
        actor=actor,
        patient_id=reservation.get("patient_id"),
        target_type="bed_reservation",
        target_id=reservation.get("id"),
        request_meta=request_meta,
        metadata={
            "reservation_id": reservation.get("id"),
            "bed_id": reservation.get("bed_id"),
            "ward_id": reservation.get("ward_id"),
            "purpose": reservation.get("purpose"),
            "admission_id": reservation.get("admission_id"),
            "expires_at": reservation.get("expires_at"),
            **(extra or {}),
        },
    )


def expire_due_on(
    db, *, now: datetime | None = None, request_meta: dict | None = None
) -> list[str]:
    """Record every active reservation whose time has run out as 'expired',
    on the caller's immediate connection, each with its audit row. Returns
    the ids. The bed was already free (readers compare the clock); this makes
    the stored status say so."""
    if not table_ready(db):
        return []
    now = now or _now()
    expired: list[str] = []
    for row in db.execute("SELECT * FROM bed_reservations WHERE status = 'active'").fetchall():
        res = dict(row)
        if _is_live(res, now):
            continue
        until = parse_instant(res.get("expires_at"))
        closed_at = format_utc(until) if until is not None else format_utc(now)
        db.execute(
            "UPDATE bed_reservations SET status = 'expired', closed_at = ?, close_reason = ? "
            "WHERE id = ? AND status = 'active'",
            (closed_at, EXPIRED_REASON, res["id"]),
        )
        _audit(
            db,
            "bed_reservation_expired",
            f"Bed reservation {res['id']} expired",
            actor=None,
            reservation=res,
            request_meta=request_meta,
            extra={"recorded_at": format_utc(now)},
        )
        expired.append(res["id"])
    return expired


def fulfil_on(
    db,
    reservation: dict,
    *,
    admission_id: str,
    movement_id: str | None,
    actor: dict | None,
    request_meta: dict | None = None,
) -> dict:
    """The patient the bed was held for was placed in it: close the
    reservation as fulfilled, naming the movement, in the caller's
    transaction, with its audit row."""
    now = format_utc(_now())
    db.execute(
        "UPDATE bed_reservations SET status = 'fulfilled', closed_at = ?, closed_by = ?, "
        "close_reason = ?, movement_id = ?, admission_id = COALESCE(admission_id, ?) "
        "WHERE id = ? AND status = 'active'",
        (now, _actor_id(actor), FULFILLED_REASON, movement_id, admission_id, reservation["id"]),
    )
    after = (
        _row(
            db.execute(
                "SELECT * FROM bed_reservations WHERE id = ?", (reservation["id"],)
            ).fetchone()
        )
        or reservation
    )
    _audit(
        db,
        "bed_reservation_fulfilled",
        f"Bed reservation {reservation['id']} fulfilled by admission {admission_id}",
        actor=actor,
        reservation=after,
        request_meta=request_meta,
        extra={"movement_id": movement_id},
    )
    return after


# ═════════════════════════════════════════════════════════════════════════════
#  Reading one out
# ═════════════════════════════════════════════════════════════════════════════


def reservation_out(db, row: Any, *, now: datetime | None = None) -> dict | None:
    """The API shape of one reservation, with the bed and ward it holds. No
    patient name or MRN: a caller that shows them adds them inside scope.
    `status` reads 'expired' for an active row past its time."""
    if row is None:
        return None
    res = dict(row)
    now = now or _now()
    status = res.get("status")
    if status == "active" and not _is_live(res, now):
        status = "expired"
    bed = (
        _row(
            db.execute(
                "SELECT u.bed_label, u.unit_code, w.code AS ward_code, w.department AS ward_department "
                "FROM housekeeping_units u LEFT JOIN wards w ON w.id = u.ward_id WHERE u.id = ?",
                (res.get("bed_id"),),
            ).fetchone()
        )
        or {}
    )
    by = None
    if res.get("reserved_by"):
        name = db.execute("SELECT name FROM users WHERE id = ?", (res["reserved_by"],)).fetchone()
        by = name["name"] if name else None
    return {
        "id": res["id"],
        "bed_id": res.get("bed_id"),
        "ward_id": res.get("ward_id"),
        "bed_label": bed.get("bed_label"),
        "unit_code": bed.get("unit_code"),
        "ward_code": bed.get("ward_code"),
        "ward_department": bed.get("ward_department"),
        "patient_id": res.get("patient_id"),
        "admission_id": res.get("admission_id"),
        "purpose": res.get("purpose"),
        "purpose_words": PURPOSE_WORDS.get(res.get("purpose") or "", "Reserved"),
        "note": res.get("note"),
        "status": status,
        "reserved_by": res.get("reserved_by"),
        "reserved_by_name": by,
        "reserved_at": res.get("reserved_at"),
        "expires_at": res.get("expires_at"),
        "closed_at": res.get("closed_at"),
        "closed_by": res.get("closed_by"),
        "close_reason": res.get("close_reason"),
        "movement_id": res.get("movement_id"),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Reserve and release
# ═════════════════════════════════════════════════════════════════════════════


def _expiry_from(data: dict, purpose: str) -> int:
    default = expiry_minutes(purpose)
    asked = data.get("expires_in_minutes")
    if asked in (None, ""):
        return default
    try:
        minutes = int(asked)
    except (TypeError, ValueError):
        raise AdtError(
            "The hold time must be a whole number of minutes.", code="bad_expiry"
        ) from None
    if minutes < 1 or minutes > default:
        raise AdtError(
            f"A bed can be held for up to {default} minutes for this purpose (the site's "
            "setting). Ask for that or less.",
            code="bad_expiry",
            max_minutes=default,
        )
    return minutes


def reserve_bed(
    data: dict, *, actor: dict, idempotency_key: str | None = None, request_meta: dict | None = None
) -> dict:
    """Hold a free master bed, in ONE immediate session.

    data: bed_id (required), purpose (required), admission_id (required for a
    transfer or a return from surgery), patient_id (optional otherwise),
    note, expires_in_minutes (up to the purpose's default). The route has
    already checked that the patient or admission named is inside the
    caller's department; the patient the row will name is checked again here,
    in this transaction (ReservationOutOfScope, 404, nothing written). A bed
    held for nobody named may be held only on a
    ward of the caller's own department (or by an account with no
    department), so one team cannot hold another team's beds with no patient
    to show for it (404 ward_not_found otherwise). The Idempotency-Key is
    required; the same person's retry for the same bed answers the first
    reservation and writes nothing. Answers {replayed, reservation}.

    With more than one facility on record (multisite-8h1) the bed must be on
    a ward of the caller's facilities (404 ward_not_found otherwise), and the
    patient the row names must be inside the caller's facility scope as well
    as its department (ReservationOutOfScope): no bed is held at one facility
    for a patient of another (ruling MS-R3 (c))."""
    from permissions import department_scope, patient_in_scope

    from services import facility_lanes

    caller_department = department_scope(actor)
    facilities = facility_lanes.scope_of(actor)
    data = dict(data or {})
    key = _text(idempotency_key)
    if not key:
        raise AdtError(
            "This change needs an Idempotency-Key header, so a retried request is "
            "never recorded twice.",
            code="idempotency_key_required",
        )
    purpose = (_text(data.get("purpose")) or "").lower()
    if purpose not in PURPOSES:
        raise AdtError(
            "Say why the bed is held: an admission, a transfer, after surgery, a "
            "planned admission, or other.",
            code="bad_purpose",
            purposes=list(PURPOSES),
        )
    bed_id = _text(data.get("bed_id"))
    if not bed_id:
        raise AdtError("Choose the bed to hold.", code="bed_required")
    admission_id = _text(data.get("admission_id"))
    patient_id = _text(data.get("patient_id"))
    if purpose in NEEDS_ADMISSION and not admission_id:
        raise AdtError(
            "A transfer, or a patient coming back from surgery, is already admitted: "
            "name the admission the bed is held for.",
            code="admission_required",
        )
    note = _text(data.get("note"), _MAX_NOTE)
    minutes = _expiry_from(data, purpose)

    with db_session(immediate=True) as db:
        if not table_ready(db):
            raise AdtConflict(
                "Bed reservations are not set up on this database yet.",
                code="reservations_unavailable",
            )
        prior = _row(
            db.execute(
                "SELECT * FROM bed_reservations WHERE idempotency_key = ?", (key,)
            ).fetchone()
        )
        if prior is not None:
            if prior.get("reserved_by") != _actor_id(actor) or prior.get("bed_id") != bed_id:
                raise AdtConflict(
                    "That Idempotency-Key was already used for another change",
                    code="idempotency_key_reused",
                )
            return {"replayed": True, "reservation": reservation_out(db, prior)}
        now = _now()
        expire_due_on(db, now=now, request_meta=request_meta)

        bed = _row(
            db.execute(
                "SELECT * FROM housekeeping_units WHERE id = ? AND ward_id IS NOT NULL", (bed_id,)
            ).fetchone()
        )
        if bed is None:
            raise AdtNotFound("No bed with that id is set up in Bed setup", code="bed_not_found")
        ward = _row(db.execute("SELECT * FROM wards WHERE id = ?", (bed["ward_id"],)).fetchone())
        where = f"Bed {bed.get('bed_label')} on {(ward or {}).get('code') or bed.get('ward')}"
        if ward is not None and facility_lanes.outside(ward.get("facility_id", 1), facilities):
            raise AdtNotFound("No bed with that id is set up in Bed setup", code="bed_not_found")
        if ward is None or not ward.get("is_active"):
            raise AdtConflict(
                f"{where}: the ward is switched off in Bed setup.", code="ward_inactive"
            )
        if bed.get("retired_at"):
            raise AdtConflict(f"{where} is retired. Choose another bed.", code="bed_retired")
        if not bed.get("is_active"):
            raise AdtConflict(f"{where} is switched off. Choose another bed.", code="bed_inactive")
        if (
            not (patient_id or admission_id)
            and caller_department is not None
            and (ward.get("department") or "").casefold() != caller_department.casefold()
        ):
            raise AdtNotFound(
                "That ward is not on your bed board. A bed on another team's ward "
                "is held only for a patient you name.",
                code="ward_not_found",
            )
        if bed.get("state") == "out_of_service":
            raise AdtConflict(
                f"{where} is recorded out of service by housekeeping. Choose another bed.",
                code="bed_out_of_service",
            )
        if _holder_on(db, admission_id=None, ward=ward, bed=bed) is not None:
            raise AdtConflict(
                f"{where} has a patient in it, so it cannot be held for another.",
                code="bed_occupied",
                bed_id=bed_id,
            )
        held = live_on_bed(db, bed_id, now=now)
        if held is not None:
            raise AdtConflict(
                f"{where} is already {PURPOSE_WORDS.get(held.get('purpose') or '', 'reserved').lower()}. "
                "Release that reservation first.",
                code="bed_reserved",
                bed_id=bed_id,
                reservation_id=held.get("id"),
                purpose=held.get("purpose"),
                expires_at=held.get("expires_at"),
            )

        if admission_id:
            adm = _row(
                db.execute(
                    "SELECT id, patient_id, status FROM admissions WHERE id = ?", (admission_id,)
                ).fetchone()
            )
            if adm is None:
                raise AdtNotFound("No admission with that id", code="admission_not_found")
            if adm.get("status") != "active":
                raise AdtConflict(
                    "That admission has ended, so no bed can be held for it.",
                    code="admission_not_active",
                )
            if patient_id and patient_id != adm["patient_id"]:
                raise AdtError(
                    "The patient and the admission named do not match.", code="patient_mismatch"
                )
            patient_id = adm["patient_id"]
        elif patient_id:
            if db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is None:
                raise AdtNotFound("No patient with that id", code="patient_not_found")
            active = db.execute(
                "SELECT id FROM admissions WHERE patient_id = ? AND status = 'active' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (patient_id,),
            ).fetchone()
            if active is not None:
                # An admitted patient's bed is held for their admission.
                admission_id = active["id"]
        # Scope once more, inside this transaction, on the patient the row will
        # name: the route's check ran in a session of its own, and a stay moved
        # to another department in between must not let the hold through.
        if (
            patient_id
            and (caller_department is not None or facilities is not None)
            and not patient_in_scope(db, patient_id, caller_department, facilities=facilities)
        ):
            raise ReservationOutOfScope(patient_id)
        if patient_id or admission_id:
            existing = live_for_patient_on(
                db, patient_id=patient_id, admission_id=admission_id, now=now
            )
            if existing is not None:
                raise AdtConflict(
                    "A bed is already held for this patient. Release that reservation first, so "
                    "nobody holds two beds at once.",
                    code="patient_already_reserved",
                    reservation_id=existing.get("id"),
                    bed_id=existing.get("bed_id"),
                )

        reservation_id = f"RES-{uuid.uuid4().hex[:12].upper()}"
        reserved_at = format_utc(now)
        expires_at = format_utc(now + timedelta(minutes=minutes))
        try:
            db.execute(
                """INSERT INTO bed_reservations
                       (id, bed_id, ward_id, patient_id, admission_id, purpose, note, status,
                        reserved_by, reserved_at, expires_at, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (
                    reservation_id,
                    bed_id,
                    bed["ward_id"],
                    patient_id,
                    admission_id,
                    purpose,
                    note,
                    _actor_id(actor),
                    reserved_at,
                    expires_at,
                    key,
                ),
            )
        except DBIntegrityError as exc:
            raise AdtConflict(
                f"{where} was just reserved by someone else. Choose another bed.",
                code="bed_reserved",
                bed_id=bed_id,
            ) from exc
        row = _row(
            db.execute("SELECT * FROM bed_reservations WHERE id = ?", (reservation_id,)).fetchone()
        )
        assert row is not None
        _audit(
            db,
            "bed_reserved",
            f"{where} reserved ({purpose.replace('_', ' ')}) until {expires_at}",
            actor=actor,
            reservation=row,
            request_meta=request_meta,
            extra={"minutes": minutes, "note_given": bool(note)},
        )
        return {"replayed": False, "reservation": reservation_out(db, row, now=now)}


def reservation_facts(reservation_id: str) -> dict | None:
    """What a route needs to check scope before a release, or None when there
    is no such reservation: {patient_id, admission_id, reserved_by,
    ward_department, ward_id, status}. No patient name. ward_id (multisite-8h1)
    is how the route tells the facility of the held bed."""
    with db_session() as db:
        if not table_ready(db):
            return None
        row = _row(
            db.execute(
                "SELECT r.id, r.patient_id, r.admission_id, r.reserved_by, r.status, r.ward_id, "
                "w.department AS ward_department FROM bed_reservations r "
                "LEFT JOIN wards w ON w.id = r.ward_id WHERE r.id = ?",
                (reservation_id,),
            ).fetchone()
        )
    return row


def release_reservation(
    reservation_id: str, *, actor: dict, reason: Any, request_meta: dict | None = None
) -> dict:
    """Let a held bed go, with the reason, in ONE immediate session. Refused
    (409 reservation_not_active) once it is fulfilled, released or expired.
    The route has already checked scope."""
    why = _text(reason, _MAX_REASON)
    if not why:
        raise AdtError(
            "Type why the bed is no longer held: it is part of the record.", code="reason_required"
        )
    with db_session(immediate=True) as db:
        if not table_ready(db):
            raise AdtNotFound("No reservation with that id", code="reservation_not_found")
        now = _now()
        expire_due_on(db, now=now, request_meta=request_meta)
        row = _row(
            db.execute("SELECT * FROM bed_reservations WHERE id = ?", (reservation_id,)).fetchone()
        )
        if row is None:
            raise AdtNotFound("No reservation with that id", code="reservation_not_found")
        if row.get("status") != "active":
            words = {
                "fulfilled": "the patient was placed in the bed",
                "released": "it was already released",
                "expired": "its time ran out",
            }.get(row.get("status") or "", "it was closed")
            raise AdtConflict(
                f"This reservation is no longer holding the bed: {words}.",
                code="reservation_not_active",
                status=row.get("status"),
            )
        db.execute(
            "UPDATE bed_reservations SET status = 'released', closed_at = ?, closed_by = ?, "
            "close_reason = ? WHERE id = ? AND status = 'active'",
            (format_utc(now), _actor_id(actor), why, reservation_id),
        )
        after = _row(
            db.execute("SELECT * FROM bed_reservations WHERE id = ?", (reservation_id,)).fetchone()
        )
        assert after is not None
        _audit(
            db,
            "bed_reservation_released",
            f"Bed reservation {reservation_id} released",
            actor=actor,
            reservation=after,
            request_meta=request_meta,
            extra={"reason": why},
        )
        return {"reservation": reservation_out(db, after, now=now)}


# ═════════════════════════════════════════════════════════════════════════════
#  The list
# ═════════════════════════════════════════════════════════════════════════════


def _may_read_unnamed_note(row: dict, entry: dict, actor: dict, dept: str | None) -> bool:
    """The note on a hold for nobody named is read by the people who may
    release it (routes/adt/reservations.release_reservation): whoever made
    it, an account with no department, and the department that owns the
    ward. Another team that sees the ward because one of its patients lies
    there does not read it."""
    if dept is None:
        return True
    if row.get("reserved_by") and row.get("reserved_by") == _actor_id(actor):
        return True
    return (entry.get("ward_department") or "").casefold() == dept.casefold()


def list_reservations(
    *, actor: dict, ward_id: str | None = None, request_meta: dict | None = None
) -> dict:
    """Every bed held now on the caller's wards (bed_master_service's ward
    rule), plus the beds held anywhere for the caller's own patients. The
    patient's name and MRN are shown only inside the caller's department, and
    a read that names anybody writes one fail-closed phi_access row. The
    free-text note goes with the name: none for a patient outside scope, and
    for a hold naming nobody only to those who may release it. A ward the
    caller may not see is 404. With more than one facility on record
    (multisite-8h1) the wards and the patients are the caller's facility's
    only."""
    from permissions import department_scope

    from services import bed_master_service as bm
    from services import facility_lanes

    dept = department_scope(actor)
    facilities = facility_lanes.scope_of(actor)
    with db_session() as db:
        visible = {w["id"]: w for w in bm._visible_wards(db, dept, facilities)}
        if ward_id and ward_id not in visible:
            raise AdtNotFound("No ward with that id on your bed board", code="ward_not_found")
        if not table_ready(db):
            return {"ward_id": ward_id, "reservations": [], "purposes": dict(PURPOSE_WORDS)}
        now = _now()
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM bed_reservations WHERE status = 'active' ORDER BY expires_at, id"
            ).fetchall()
        ]
        rows = [r for r in rows if _is_live(r, now)]
        in_scope = bm._scope_filter(
            db, (r["patient_id"] for r in rows if r.get("patient_id")), dept, facilities
        )
        keep = []
        for r in rows:
            if ward_id:
                if r["ward_id"] != ward_id:
                    continue
            elif r["ward_id"] not in visible and r.get("patient_id") not in in_scope:
                continue
            keep.append(r)
        names: dict[str, dict] = {}
        shown = sorted({r["patient_id"] for r in keep if r.get("patient_id") in in_scope})
        if shown:
            marks = ",".join("?" for _ in shown)
            names = {
                p["id"]: dict(p)
                for p in db.execute(
                    f"SELECT id, name, mrn FROM patients WHERE id IN ({marks})", tuple(shown)
                ).fetchall()
            }
        out = []
        for r in keep:
            entry = reservation_out(db, r, now=now)
            assert entry is not None
            pid = r.get("patient_id")
            if pid in in_scope:
                person = names.get(pid, {})
                entry["patient"] = {
                    "patient_id": pid,
                    "admission_id": r.get("admission_id"),
                    "name": person.get("name"),
                    "mrn": person.get("mrn"),
                }
                entry["patient_hidden"] = False
            else:
                entry["patient_id"] = None
                entry["admission_id"] = None
                entry["patient"] = None
                entry["patient_hidden"] = bool(pid)
                # The note is free text and often names the patient, so it
                # goes only where the patient could be named. A hold for
                # nobody named keeps its note for the people who may release
                # it: whoever made it, an account with no department, and the
                # department that owns the ward.
                if pid or not _may_read_unnamed_note(r, entry, actor, dept):
                    entry["note"] = None
            out.append(entry)
        if shown:
            bm._phi_read_audit(
                db,
                actor,
                "bed_reservations_viewed",
                f"Bed reservations viewed: {len(shown)} patients named",
                patient_ids=list(shown),
                target_type="ward" if ward_id else "bed_board",
                target_id=ward_id or "all",
                request_meta=request_meta,
                metadata={"department": dept, "reservations": len(out)},
            )
    return {"ward_id": ward_id, "reservations": out, "purposes": dict(PURPOSE_WORDS)}
