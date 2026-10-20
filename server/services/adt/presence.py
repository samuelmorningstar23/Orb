"""Orb: leave and absconded (adt-10, W7-D). A patient who is not on the ward.

A patient on an admission is either on the ward (admissions.presence_status
NULL), on leave ('on_leave': home for a festival, out to another hospital for
a scan, with the doctor's leave) or absconded ('absconded': gone without
leave). Four writes move between them, each in ONE immediate session:

  start_leave     on the ward       -> on leave       movement 'leave_start'
  end_leave       on leave          -> on the ward    movement 'leave_end'
  mark_absconded  on the ward, or on leave and not back
                                    -> absconded      movement 'absconded'
  mark_returned   absconded         -> on the ward    movement 'absconded_return'

Each write is a patient_movements row (core.record_movement_on, the single
ledger writer), the admission's presence_status and presence_since, and one
fail-closed patient_movement audit row, all in the same transaction: an
unwritable audit rolls the whole change back.

THE BED IS HELD (ruling ADT-P6). Nothing here touches admissions.ward_id,
bed_id, ward or bed, or patients.ward or bed: the patient keeps the bed, the
bed board shows it 'Occupied, patient on leave' or 'Occupied, patient
absconded', and the movement records the same place on both sides. These
movement types are not in the bed board's or housekeeping's vacating lists,
so no turnaround clean is raised for a leave.

presence_since is when the patient was last on the ward. It is set when the
patient leaves (on leave, or absconded from the ward) and KEPT when a patient
on leave is then recorded absconded (they have been off the ward since the
leave began, and 'Not on ward since' must not say a later time). It is
cleared, with presence_status, when they are back: a leave out and back
leaves both NULL.

THE BOARDS AND THE CLINICAL LANES. The boards say 'Not on ward since HH:MM'
(adt-10). The obs-due and medication-due clocks, the escalation ladder, sepsis
auto-start, the vitals simulator and the downtime ward pack honour presence
through ONE helper, services/adt/clinical_pause.py (adt-11, ruling ADT-S4 (a)),
which reads presence_by_patient below.

WHO (ruling ADT-S7 held, strictest option): the routes need adt.presence or
patient.write, which today is the administrator and the doctors. Nurses and
ward clerks hold neither, so they are refused (403). The department scope is
the route's (404 outside it).

TIMES (ruling ADT-P3): when it happened may be entered up to
ADT_BACKTIME_HOURS back, never more than 5 minutes ahead, never before the
admission, and never before the patient's last recorded leave or return.

Everything here is inert while ORB_ADT is off: the routes are behind the
module gate, and presence_by_patient answers None so the boards add nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from database import db_session

from services.adt.core import (
    AdtBadTime,
    AdtConflict,
    AdtError,
    AdtNotFound,
    _actor_id,
    _row,
    _text,
    adt_on,
    audit_on,
    check_event_time,
    movement_for_key,
    parse_instant,
    record_movement_on,
)
from services.clinical_time import to_utc_iso

__all__ = [
    "PRESENCE_ACTIONS",
    "PRESENCE_WORDS",
    "PRESENCE_MOVEMENT_TYPES",
    "record_presence_on",
    "start_leave",
    "end_leave",
    "mark_absconded",
    "mark_returned",
    "presence_by_patient",
    "absconded_at_on",
]

_MAX_REASON = 2000

#: What each write does: the movement it records, the presence it may start
#: from (None is on the ward), the presence it leaves, the audit action, and
#: the reason recorded when none is typed (None: a typed reason is required).
PRESENCE_ACTIONS: dict[str, dict] = {
    "leave": {
        "movement_type": "leave_start",
        "from": (None,),
        "to": "on_leave",
        "audit": "leave_started",
        "default_reason": None,
        "what": "leave",
    },
    "leave_end": {
        "movement_type": "leave_end",
        "from": ("on_leave",),
        "to": None,
        "audit": "leave_ended",
        "default_reason": "Back on the ward from leave",
        "what": "return",
    },
    "absconded": {
        "movement_type": "absconded",
        "from": (None, "on_leave"),
        "to": "absconded",
        "audit": "patient_absconded",
        "default_reason": None,
        "what": "absconding",
    },
    "returned": {
        "movement_type": "absconded_return",
        "from": ("absconded",),
        "to": None,
        "audit": "absconded_patient_returned",
        "default_reason": "Back on the ward",
        "what": "return",
    },
}

#: The movement types this module writes (all four are in m0051's vocabulary).
PRESENCE_MOVEMENT_TYPES = frozenset(spec["movement_type"] for spec in PRESENCE_ACTIONS.values())

#: How a presence is named on a screen.
PRESENCE_WORDS: dict[str, str] = {"on_leave": "On leave", "absconded": "Absconded"}


def _refusal(action: str, current: str | None) -> AdtConflict:
    """Why this write cannot start from the patient's current presence."""
    if action == "leave":
        if current == "on_leave":
            return AdtConflict(
                "This patient is already recorded on leave. Record the return "
                "from leave before starting another.",
                code="already_on_leave",
                presence_status=current,
            )
        return AdtConflict(
            "This patient is recorded absconded. Record the return to the ward first.",
            code="patient_absconded",
            presence_status=current,
        )
    if action == "leave_end":
        if current is None:
            return AdtConflict(
                "This patient is not recorded on leave, so there is no leave to end.",
                code="not_on_leave",
                presence_status=current,
            )
        return AdtConflict(
            "This patient is recorded absconded, not on leave. Record the return "
            "to the ward instead.",
            code="patient_absconded",
            presence_status=current,
        )
    if action == "absconded":
        return AdtConflict(
            "This patient is already recorded absconded.",
            code="already_absconded",
            presence_status=current,
        )
    # returned
    if current is None:
        return AdtConflict(
            "This patient is not recorded absconded.", code="not_absconded", presence_status=current
        )
    return AdtConflict(
        "This patient is recorded on leave, not absconded. Record the return from leave instead.",
        code="patient_on_leave",
        presence_status=current,
    )


def _last_presence_change_on(db, admission_id: str) -> datetime | None:
    """When this admission's presence last changed (its latest leave,
    absconded or return movement), or None."""
    types = tuple(sorted(PRESENCE_MOVEMENT_TYPES))
    marks = ",".join("?" for _ in types)
    latest: datetime | None = None
    for row in db.execute(
        f"SELECT moved_at FROM patient_movements WHERE admission_id = ? "
        f"AND movement_type IN ({marks})",
        (admission_id, *types),
    ).fetchall():
        when = parse_instant(row["moved_at"])
        if when is not None and (latest is None or when > latest):
            latest = when
    return latest


def _answer(
    *,
    replayed: bool,
    admission_id: str,
    patient_id: str | None,
    presence_status: str | None,
    presence_since: str | None,
    movement: dict,
    bed: dict | None = None,
) -> dict:
    return {
        "replayed": replayed,
        "admission_id": admission_id,
        "patient_id": patient_id,
        "presence_status": presence_status,
        "presence_words": PRESENCE_WORDS.get(presence_status or "", "On the ward"),
        "presence_since": presence_since,
        "movement": {
            "id": movement.get("id"),
            "movement_type": movement.get("movement_type"),
            "moved_at": movement.get("moved_at"),
        },
        **({"bed_held": bed} if bed is not None else {}),
    }


def record_presence_on(
    db,
    *,
    admission_id: str,
    action: str,
    actor: dict | None,
    reason: Any = None,
    occurred_at: Any = None,
    expected_return_at: Any = None,
    notes: Any = None,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    """Record one presence change on the caller's connection (the caller holds
    db_session(immediate=True) as the outermost session).

    `action` is one of PRESENCE_ACTIONS. A replayed Idempotency-Key by the same
    person, for the same admission and the same change, answers the first
    result and writes nothing; any other reuse is 409 idempotency_key_reused.
    Refused (409) when the change cannot start from the patient's current
    presence: a second leave while one is open, ending a leave that was never
    started, and so on."""
    spec = PRESENCE_ACTIONS.get(action)
    if spec is None:
        raise AdtError(f"Unknown presence change {action!r}", code="unknown_presence_change")
    key = _text(idempotency_key)
    if not key:
        raise AdtError(
            "This change needs an Idempotency-Key header, so a retried request is "
            "never recorded twice.",
            code="idempotency_key_required",
        )

    prior = movement_for_key(db, key)
    if prior is not None:
        if (
            prior.get("admission_id") != admission_id
            or (prior.get("moved_by") or None) != (_actor_id(actor) or None)
            or prior.get("movement_type") != spec["movement_type"]
        ):
            raise AdtConflict(
                "That Idempotency-Key was already used for another change",
                code="idempotency_key_reused",
            )
        adm: dict | None
        adm = (
            _row(
                db.execute(
                    "SELECT patient_id, presence_status, presence_since FROM admissions "
                    "WHERE id = ?",
                    (admission_id,),
                ).fetchone()
            )
            or {}
        )
        return _answer(
            replayed=True,
            admission_id=admission_id,
            patient_id=prior.get("patient_id"),
            presence_status=adm.get("presence_status"),
            presence_since=adm.get("presence_since"),
            movement=prior,
        )

    adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
    if adm is None:
        raise AdtNotFound("No admission with that id", code="admission_not_found")
    if adm.get("status") != "active":
        raise AdtConflict(
            "That admission has ended, so leave or absconding cannot be recorded on it.",
            code="admission_not_active",
            status=adm.get("status"),
        )
    current = adm.get("presence_status") or None
    if current not in spec["from"]:
        raise _refusal(action, current)

    why = _text(reason, _MAX_REASON) or spec["default_reason"]
    if not why:
        raise AdtError(
            "Type the reason: it is part of the record."
            if action == "leave"
            else "Type what is known: when and how the patient was found missing, and who was "
            "told. It is part of the record.",
            code="reason_required",
        )

    # When it happened: inside the back-time window, not before the stay
    # began, and not before the patient's last leave or return.
    stay_start = parse_instant(adm.get("admitted_at") or adm.get("created_at"))
    when = check_event_time(occurred_at, what=spec["what"], not_before=stay_start)
    last_change = _last_presence_change_on(db, admission_id)
    off_since = parse_instant(adm.get("presence_since")) if current else None
    floor = max((t for t in (last_change, off_since) if t is not None), default=None)
    if floor is not None and when < floor:
        raise AdtBadTime(
            f"The {spec['what']} time is before the patient's last recorded leave or return. "
            "Check the time.",
            code="before_last_change",
        )

    expected_iso = None
    if action == "leave" and expected_return_at not in (None, ""):
        expected = parse_instant(expected_return_at)
        if expected is None:
            raise AdtError(
                "The expected return time could not be read. Give a date and a time.",
                code="bad_expected_return",
            )
        if expected <= when:
            raise AdtError(
                "The expected return time must be after the leave starts.",
                code="bad_expected_return",
            )
        expected_iso = to_utc_iso(expected)

    when_iso = to_utc_iso(when)
    new_status = spec["to"]
    if new_status is None:
        new_since = None
    elif current is None:
        new_since = when_iso
    else:
        # On leave and not back, now recorded absconded: off the ward since
        # the leave began.
        new_since = adm.get("presence_since") or when_iso

    changed = db.execute(
        "UPDATE admissions SET presence_status = ?, presence_since = ? "
        "WHERE id = ? AND status = 'active' AND presence_status IS ?",
        (new_status, new_since, admission_id, current),
    ).rowcount
    if changed != 1:
        raise AdtConflict(
            "This patient's record changed while this was being saved. Reload and try again.",
            code="state_changed",
        )

    place = {
        "department": adm.get("department"),
        "ward": adm.get("ward"),
        "bed": adm.get("bed"),
        "ward_id": adm.get("ward_id"),
        "bed_id": adm.get("bed_id"),
    }
    actor_name = None
    if _actor_id(actor):
        name_row = db.execute("SELECT name FROM users WHERE id = ?", (_actor_id(actor),)).fetchone()
        actor_name = name_row["name"] if name_row else None
    detail = {
        "presence_from": current,
        "presence_to": new_status,
        # The bed stays the patient's (ADT-P6): the same place on both sides.
        "bed_held": True,
        "off_ward_since": new_since,
    }
    if expected_iso:
        detail["expected_return_at"] = expected_iso
    movement = record_movement_on(
        db,
        patient_id=adm["patient_id"],
        admission_id=admission_id,
        movement_type=spec["movement_type"],
        reason=why,
        moved_at=when_iso,
        moved_by=_actor_id(actor),
        moved_by_name=actor_name,
        from_location=place,
        to_location=place,
        notes=_text(notes, _MAX_REASON),
        idempotency_key=key,
        detail=detail,
    )
    audit_on(
        db,
        category="patient_movement",
        action=spec["audit"],
        summary=(
            f"Admission {admission_id}: "
            f"{PRESENCE_WORDS.get(current or '', 'on the ward').lower()} to "
            f"{PRESENCE_WORDS.get(new_status or '', 'on the ward').lower()} "
            f"({spec['movement_type'].replace('_', ' ')})"
        ),
        actor=actor,
        patient_id=adm["patient_id"],
        target_type="admission",
        target_id=admission_id,
        request_meta=request_meta,
        metadata={
            "movement_id": movement["id"],
            "movement_type": spec["movement_type"],
            "presence_from": current,
            "presence_to": new_status,
            "moved_at": when_iso,
            "bed_id": adm.get("bed_id"),
            **({"expected_return_at": expected_iso} if expected_iso else {}),
        },
    )
    return _answer(
        replayed=False,
        admission_id=admission_id,
        patient_id=adm["patient_id"],
        presence_status=new_status,
        presence_since=new_since,
        movement=movement,
        bed={
            "ward_id": adm.get("ward_id"),
            "bed_id": adm.get("bed_id"),
            "ward": adm.get("ward"),
            "bed": adm.get("bed"),
        },
    )


def _record(
    action: str,
    admission_id: str,
    *,
    actor: dict,
    reason: Any = None,
    occurred_at: Any = None,
    expected_return_at: Any = None,
    notes: Any = None,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    with db_session(immediate=True) as db:
        return record_presence_on(
            db,
            admission_id=admission_id,
            action=action,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
            expected_return_at=expected_return_at,
            notes=notes,
            idempotency_key=idempotency_key,
            request_meta=request_meta,
        )


def start_leave(
    admission_id: str,
    *,
    actor: dict,
    reason: Any = None,
    occurred_at: Any = None,
    expected_return_at: Any = None,
    notes: Any = None,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    """The patient goes on leave; the bed stays theirs. A reason is required;
    an expected return time is optional and recorded on the movement."""
    return _record(
        "leave",
        admission_id,
        actor=actor,
        reason=reason,
        occurred_at=occurred_at,
        expected_return_at=expected_return_at,
        notes=notes,
        idempotency_key=idempotency_key,
        request_meta=request_meta,
    )


def end_leave(
    admission_id: str,
    *,
    actor: dict,
    reason: Any = None,
    occurred_at: Any = None,
    notes: Any = None,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    """The patient is back on the ward from leave."""
    return _record(
        "leave_end",
        admission_id,
        actor=actor,
        reason=reason,
        occurred_at=occurred_at,
        notes=notes,
        idempotency_key=idempotency_key,
        request_meta=request_meta,
    )


def mark_absconded(
    admission_id: str,
    *,
    actor: dict,
    reason: Any = None,
    occurred_at: Any = None,
    notes: Any = None,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    """The patient has gone without leave, or did not come back from leave.
    What is known is required as the reason. The stay is NOT ended here: an
    absconded stay is closed through End of stay (ruling ADT-S3)."""
    return _record(
        "absconded",
        admission_id,
        actor=actor,
        reason=reason,
        occurred_at=occurred_at,
        notes=notes,
        idempotency_key=idempotency_key,
        request_meta=request_meta,
    )


def mark_returned(
    admission_id: str,
    *,
    actor: dict,
    reason: Any = None,
    occurred_at: Any = None,
    notes: Any = None,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    """An absconded patient is back on the ward."""
    return _record(
        "returned",
        admission_id,
        actor=actor,
        reason=reason,
        occurred_at=occurred_at,
        notes=notes,
        idempotency_key=idempotency_key,
        request_meta=request_meta,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Readers for the boards
# ═════════════════════════════════════════════════════════════════════════════


def presence_by_patient(patient_ids: Any, *, db: Any = None) -> dict | None:
    """patient id -> {presence_status, presence_since} of their active
    admission (the newest, if several), for the patients asked about.

    None while ORB_ADT is not 'on', so a board adds no key at all and its
    payload is exactly what it was (the flag-off promise). A patient with no
    active admission is left out. No audit: the caller is a board that audits
    its own read."""
    if not adt_on():
        return None
    ids = tuple(dict.fromkeys(p for p in (patient_ids or ()) if p))
    if not ids:
        return {}

    def _work(conn) -> dict:
        marks = ",".join("?" for _ in ids)
        out: dict[str, dict] = {}
        # Ascending, so the newest active admission is read last and wins.
        for row in conn.execute(
            f"""SELECT patient_id, presence_status, presence_since FROM admissions
                     WHERE status = 'active' AND patient_id IN ({marks})
                     ORDER BY created_at ASC, rowid ASC""",
            ids,
        ).fetchall():
            out[row["patient_id"]] = {
                "presence_status": row["presence_status"] or None,
                "presence_since": row["presence_since"] or None,
            }
        return out

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def absconded_at_on(db, admission_ids: Any) -> dict:
    """admission id -> when its current absconding was recorded (the latest
    'absconded' movement's moved_at, UTC), for the board's advisory close
    prompt. Admissions with none are left out."""
    ids = tuple(dict.fromkeys(a for a in (admission_ids or ()) if a))
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    out: dict[str, datetime] = {}
    for row in db.execute(
        f"SELECT admission_id, moved_at FROM patient_movements "
        f"WHERE movement_type = 'absconded' AND admission_id IN ({marks})",
        ids,
    ).fetchall():
        when = parse_instant(row["moved_at"])
        if when is not None and (row["admission_id"] not in out or when > out[row["admission_id"]]):
            out[row["admission_id"]] = when
    return out
