"""Orb: the ADT writer core (adt-3, W3-B). Where an admission is.

WHY ONE WRITER. About 34 server files read where a patient lies from two text
mirrors, admissions.ward/bed and patients.ward/bed: the boards, Sage, I-PASS,
the downtime pack, housekeeping. Migration 58 adds the ids (admissions.ward_id,
admissions.bed_id) beside them. If two lanes wrote them, the ids and the text
would drift and a nurse, the eMAR round or Sage would find the patient in the
wrong bed. So exactly one function writes all four, locate_admission_on, and
every move, link and later admission goes through it. A text mirror is then
always the master's spelling of the linked ward and bed.

  current_location(patient_id)    the one reader for other epics (LIS
                                  collection lists, pharmacy, diet): where the
                                  patient's active admission is, and whether
                                  that place is linked to the bed master.
  record_movement_on(db, ...)     the single patient_movements writer. Callers
                                  audit in the same transaction.
  locate_admission_on(db, ...)    the single writer of ward_id, bed_id and both
                                  text mirrors, with its movement row and its
                                  patient_movement audit in the caller's
                                  transaction.

TEXT RESOLUTION (design section 5). Ids, when given, are used as given and
checked against the master. Text alone (a typed ward 'gm-1', a typed bed ' 12 ')
links when it matches a ward code, and then a bed label on that ward, without
regard to capital letters or surrounding spaces; the mirrors then take the
master's spelling (GM-1, 12). Text that matches nothing keeps the typed text
with the id left NULL ('unlinked'), unless the site has switched strict
locations on (adt-16, ORB_ADT_STRICT_LOCATIONS, ruling F104): then a ward not
in bed setup is refused on every lane, and so is a bed not in bed setup on a
ward whose bed register is marked complete (a ward still being set up keeps
the unlinked place, with the warning). See 'Strict locations' below.

REFUSALS, each with words and before anything is written:
  * a bed another active admission holds (the partial unique index
    idx_admissions_active_bed refuses it underneath too: ruling ADT-S1 (a),
    no shared beds);
  * a retired or switched-off bed, and a switched-off ward;
  * a bed housekeeping records as out of service, unless a reason is typed
    (it is recorded on the movement): refuse a record, never a patient;
  * a placement on a ward owned by another department, unless the caller
    acknowledges that the host ward's nurses will not be able to open the
    chart (ruling ADT-S6 is not applied, so scope does not change). An
    acknowledged placement writes a clinical_access 'boarder_placed' audit and
    a system line in the patient's case room, in the same transaction;
  * an expected bed that is no longer the admission's bed ('state_changed');
  * a bed a reservation holds for another patient, or for nobody named
    (adt-12, 'bed_reserved'). A bed held for this admission or this patient
    is taken, and the reservation fulfilled, in the same transaction
    (services/adt/reservations.py).

Every function that writes takes the caller's connection and opens no session:
the caller holds db_session(immediate=True) as the outermost session, resolves
anything that opens its own session first, and catches the errors below.
Audit writes go through services.audit_service with db=, so an unwritable
audit (patient_movement and clinical_access are both fail-closed) rolls the
whole write back.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import config
from database import db_session
from db_backend import DBIntegrityError
from migrations_his.m0051_adt_movement_ledger import MOVEMENT_TYPES
from migrations_his.m0058_adt_admission_location import (
    DISCHARGE_TYPES,
    PRESENCE_STATUSES,
)

from services.audit_service import log_audit_event
from services.clinical_time import parse_to_utc, to_utc_iso, utc_iso_now

logger = logging.getLogger("orb.adt")

__all__ = [
    "AdtError",
    "AdtConflict",
    "AdtNotFound",
    "AdtBadTime",
    "http_detail",
    "DISCHARGE_TYPES",
    "PRESENCE_STATUSES",
    "MOVEMENT_TYPES",
    "adt_on",
    "current_location",
    "current_admission_on",
    "record_movement_on",
    "match_location_on",
    "locate_admission_on",
    "audit_on",
    "parse_instant",
    # adt-5 (W5-B): admitting into a master bed and assigning a bed.
    "TERMINAL_CLEAN_AUTHORISER_ROLES",
    "check_event_time",
    "resolve_place_on",
    "bed_readiness_on",
    "bed_choices_on",
    "add_extra_bed_on",
    "prepare_place_on",
    "place_admission_on",
    "assign_bed",
    # adt-5 round 3: who may authorise a bed awaiting a terminal clean, and
    # how they confirm it.
    "AdtPinRefused",
    "AdtLocked",
    "carried_isolation",
    "confirm_terminal_authoriser",
    "terminal_authoriser_eligible_on",
    # adt-16 (W9-A): strict locations (ruling F104).
    "AdtUnlistedPlace",
    "STRICT_LOCATION_CODES",
    "strict_locations_on",
    "strict_place_refusal",
]

_MAX_REASON = 2000
_MAX_LABEL = 120

#: Sentinel for "the caller did not say which bed it expects".
_UNSET = object()


# ═════════════════════════════════════════════════════════════════════════════
#  Errors
# ═════════════════════════════════════════════════════════════════════════════


class _PinRefusalContext(TypedDict):
    actor: dict | None
    authoriser_id: str
    request_meta: dict | None
    patient_id: str | None
    admission_id: str | None
    bed_id: str | None


class AdtError(ValueError):
    """An ADT request that cannot be honoured. 422 unless a subclass says
    otherwise. `code` is a stable word a screen can react to; `detail` carries
    the facts the words name."""

    http_status = 422

    def __init__(self, message: str, *, code: str = "adt_refused", **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail


class AdtConflict(AdtError):
    """Well formed, but it collides with what is on file. 409."""

    http_status = 409


class AdtNotFound(AdtError):
    """No such admission, ward or bed. 404."""

    http_status = 404


class AdtBadTime(AdtError):
    """An event time that cannot be recorded: unreadable, in the future,
    before the stay began, or further back than ADT_BACKTIME_HOURS. 400, the
    admission form's status for a value the clinician must correct."""

    http_status = 400


class AdtPinRefused(AdtError):
    """A signing PIN that did not confirm the person it was typed for. 403."""

    http_status = 403


class AdtLocked(AdtError):
    """PIN entry is locked for that person after too many wrong PINs. 429."""

    http_status = 429


class AdtUnlistedPlace(AdtError):
    """A ward or bed that is not in bed setup, on a site with strict
    locations on (adt-16). 400: the person must choose a place from bed
    setup, or ask a bed manager to set it up."""

    http_status = 400


def http_detail(err: AdtError) -> dict:
    """The body a route answers with: {error, message, ...facts}."""
    detail = {"error": err.code, "message": str(err)}
    detail.update({k: v for k, v in err.detail.items() if k not in detail})
    return detail


# ═════════════════════════════════════════════════════════════════════════════
#  The switch and small helpers
# ═════════════════════════════════════════════════════════════════════════════


def adt_on() -> bool:
    """True only while ORB_ADT is on. Read at call time, so a test (or a site)
    that turns the module on or off is honoured at once."""
    return config.module_state("adt") == "on"


def _text(value: Any, limit: int = _MAX_LABEL) -> str | None:
    """Spaces tidied (ends trimmed, inner runs collapsed); None when blank."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _same(a: Any, b: Any) -> bool:
    """Equal without regard to capital letters or surrounding spaces."""
    return (_text(a) or "").casefold() == (_text(b) or "").casefold()


def parse_instant(value: Any) -> datetime | None:
    """Any stored spelling (ISO with or without an offset, SQLite's
    CURRENT_TIMESTAMP) as an aware UTC datetime; None when blank or unreadable.
    A stored timestamp with no offset is UTC, as every Orb writer stores it."""
    if value is None or value == "":
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


def _row(row: Any) -> dict | None:
    return dict(row) if row is not None else None


def _actor_id(actor: dict | None) -> str | None:
    return (actor or {}).get("id")


# ═════════════════════════════════════════════════════════════════════════════
#  Audit, inside the caller's transaction
# ═════════════════════════════════════════════════════════════════════════════


def audit_on(
    db,
    *,
    category: str,
    action: str,
    summary: str,
    actor: dict | None,
    patient_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    request_meta: dict | None = None,
    metadata: dict | None = None,
    correlation_id: str | None = None,
) -> None:
    """One audit line on the caller's connection. patient_movement and
    clinical_access are fail-closed: a write that cannot be persisted raises
    AuditWriteError and the caller's transaction rolls back. target_id is
    always passed as text (the int target_id hash trap)."""
    meta = request_meta or {}
    log_audit_event(
        correlation_id=correlation_id or str(uuid.uuid4()),
        event_category=category,
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=_actor_id(actor),
        actor_role=(actor or {}).get("role"),
        patient_id=patient_id,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata=metadata or None,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Reads
# ═════════════════════════════════════════════════════════════════════════════


def current_admission_on(db, patient_id: str) -> dict | None:
    """The patient's active admission, the newest if (wrongly) there are two.
    Same order as frontdesk_service.transfer_patient."""
    return _row(
        db.execute(
            """SELECT * FROM admissions
            WHERE patient_id = ? AND status = 'active'
            ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
    )


def current_location(patient_id: str, *, db: Any = None) -> dict | None:
    """Where the patient's active admission is, or None when they have none.

    THE ONE READER for other epics (LIS collection lists, pharmacy indents,
    diet). It reads the admission, never patients.ward/bed, and says whether
    the place is linked to the bed master:

      linked        bed_id is set: ward and bed are the master's spelling.
      awaiting_bed  no bed text and no bed_id.
      unlinked_bed  bed text that matches no master bed (bed_id NULL).
      boarder       the ward is set up under a department other than the
                    treating one.

    No audit: a caller that shows this to a person audits its own surface."""

    def _work(conn) -> dict | None:
        row = conn.execute(
            """SELECT a.id AS admission_id, a.patient_id, a.status, a.department,
                      a.ward, a.bed, a.ward_id, a.bed_id, a.presence_status,
                      a.presence_since, a.admitted_at, a.created_at,
                      w.code AS ward_code, w.name AS ward_name,
                      w.department AS ward_department,
                      u.bed_label, u.unit_code, u.room
                 FROM admissions a
                 LEFT JOIN wards w ON w.id = a.ward_id
                 LEFT JOIN housekeeping_units u ON u.id = a.bed_id
                WHERE a.patient_id = ? AND a.status = 'active'
                ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
        if row is None:
            return None
        loc = dict(row)
        has_bed_text = bool(_text(loc.get("bed")))
        loc["linked"] = loc.get("bed_id") is not None
        loc["awaiting_bed"] = loc.get("bed_id") is None and not has_bed_text
        loc["unlinked_bed"] = loc.get("bed_id") is None and has_bed_text
        loc["boarder"] = bool(loc.get("ward_department")) and not _same(
            loc.get("ward_department"), loc.get("department")
        )
        loc["on_ward"] = loc.get("presence_status") is None
        return loc

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


# ═════════════════════════════════════════════════════════════════════════════
#  The ledger writer
# ═════════════════════════════════════════════════════════════════════════════


def _location_cols(loc: dict | None) -> tuple:
    loc = loc or {}
    return (
        loc.get("department"),
        loc.get("ward"),
        loc.get("bed"),
        loc.get("ward_id"),
        loc.get("bed_id"),
    )


def movement_for_key(db, idempotency_key: str | None) -> dict | None:
    """The movement already written under this Idempotency-Key, if any."""
    key = _text(idempotency_key)
    if not key:
        return None
    return _movement_out(
        db.execute("SELECT * FROM patient_movements WHERE idempotency_key = ?", (key,)).fetchone()
    )


def _movement_out(row: Any) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    raw = out.get("detail_json")
    if raw:
        try:
            out["detail"] = json.loads(raw)
        except (TypeError, ValueError):
            out["detail"] = None
    else:
        out["detail"] = None
    return out


def record_movement_on(
    db,
    *,
    patient_id: str,
    admission_id: str | None,
    movement_type: str,
    reason: str,
    moved_at: Any = None,
    moved_by: str | None = None,
    moved_by_name: str | None = None,
    from_location: dict | None = None,
    to_location: dict | None = None,
    end_type: str | None = None,
    discharge_summary_id: int | None = None,
    corrects_movement_id: str | None = None,
    notes: str | None = None,
    idempotency_key: str | None = None,
    detail: dict | None = None,
) -> dict:
    """Write ONE patient_movements row on the caller's connection and return it.

    The single ledger writer for ADT (frontdesk_service.transfer_patient keeps
    its own flag-off lane until adt-6a delegates it here). `moved_at` is when
    it happened, which may be back-timed; `recorded_at` is the server clock.
    Both are stored in the one UTC spelling (services.clinical_time). A
    location is a dict of department, ward, bed, ward_id and bed_id.

    The caller audits the movement in the same transaction; this function
    writes no audit of its own, because the action and its category depend
    on what moved (a bed change is patient_movement, an end of stay is
    clinical_access)."""
    if movement_type not in MOVEMENT_TYPES:
        raise AdtError(f"Unknown movement type {movement_type!r}", code="unknown_movement_type")
    if end_type is not None and end_type not in DISCHARGE_TYPES:
        raise AdtError(f"Unknown end type {end_type!r}", code="unknown_end_type")
    clean_reason = _text(reason, _MAX_REASON)
    if not clean_reason:
        raise AdtError(
            "A movement needs a reason. A move with no reason is not a record.",
            code="reason_required",
        )
    when = to_utc_iso(moved_at) if moved_at not in (None, "") else utc_iso_now()
    movement_id = f"MOV-{uuid.uuid4().hex[:12].upper()}"
    f_dept, f_ward, f_bed, f_ward_id, f_bed_id = _location_cols(from_location)
    t_dept, t_ward, t_bed, t_ward_id, t_bed_id = _location_cols(to_location)
    db.execute(
        """INSERT INTO patient_movements (
                id, patient_id, admission_id, movement_type,
                from_department, from_ward, from_bed,
                to_department, to_ward, to_bed,
                reason, moved_at, moved_by, moved_by_name, notes, idempotency_key,
                from_ward_id, from_bed_id, to_ward_id, to_bed_id, end_type,
                discharge_summary_id, corrects_movement_id, recorded_at, detail_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            movement_id,
            patient_id,
            admission_id,
            movement_type,
            f_dept,
            f_ward,
            f_bed,
            t_dept,
            t_ward,
            t_bed,
            clean_reason,
            when,
            moved_by,
            moved_by_name,
            _text(notes, _MAX_REASON),
            _text(idempotency_key),
            f_ward_id,
            f_bed_id,
            t_ward_id,
            t_bed_id,
            end_type,
            discharge_summary_id,
            corrects_movement_id,
            utc_iso_now(),
            json.dumps(detail, sort_keys=True, default=str) if detail else None,
        ),
    )
    _written_row = _movement_out(
        db.execute("SELECT * FROM patient_movements WHERE id = ?", (movement_id,)).fetchone()
    )
    assert _written_row is not None, "Expected the row written in this transaction"
    return _written_row


# ═════════════════════════════════════════════════════════════════════════════
#  Text and id resolution against the bed master
# ═════════════════════════════════════════════════════════════════════════════


def _ward_by_code(db, text: str | None) -> dict | None:
    if not text:
        return None
    return _row(
        db.execute("SELECT * FROM wards WHERE LOWER(TRIM(code)) = LOWER(?)", (text,)).fetchone()
    )


def _bed_by_label(db, ward_id: str, text: str | None) -> dict | None:
    if not text:
        return None
    return _row(
        db.execute(
            """SELECT * FROM housekeeping_units
            WHERE ward_id = ? AND LOWER(TRIM(bed_label)) = LOWER(?)""",
            (ward_id, text),
        ).fetchone()
    )


def match_location_on(db, ward_text: Any, bed_text: Any) -> dict:
    """What typed ward and bed text resolve to in the master, without refusing
    anything: {ward: row or None, bed: row or None, ward_text, bed_text}.
    The bed is looked up only on the matched ward. A retired bed is returned
    as matched; the caller decides (the reconciliation lists it, a move
    refuses it)."""
    w_text, b_text = _text(ward_text), _text(bed_text)
    ward = _ward_by_code(db, w_text)
    bed = _bed_by_label(db, ward["id"], b_text) if ward else None
    return {"ward": ward, "bed": bed, "ward_text": w_text, "bed_text": b_text}


def _bed_words(ward: dict | None, bed: dict) -> str:
    return f"Bed {bed.get('bed_label')} on {ward.get('code') if ward else bed.get('ward')}"


def _resolve_target(
    db,
    *,
    ward_id: str | None,
    bed_id: str | None,
    ward_text: Any,
    bed_text: Any,
    out_of_service_reason: str | None,
) -> dict:
    """The place to put the admission, checked against the master. Raises an
    AdtError with words for anything that must not be written."""
    ward: dict | None = None
    bed: dict | None = None
    w_text, b_text = _text(ward_text), _text(bed_text)
    if bed_id:
        bed = _row(
            db.execute(
                "SELECT * FROM housekeeping_units WHERE id = ? AND ward_id IS NOT NULL", (bed_id,)
            ).fetchone()
        )
        if bed is None:
            raise AdtNotFound("No bed with that id is set up in Bed setup", code="bed_not_found")
        if ward_id and bed["ward_id"] != ward_id:
            raise AdtError("That bed is not on the chosen ward", code="bed_not_on_ward")
        ward = _row(db.execute("SELECT * FROM wards WHERE id = ?", (bed["ward_id"],)).fetchone())
    elif ward_id:
        ward = _row(db.execute("SELECT * FROM wards WHERE id = ?", (ward_id,)).fetchone())
        if ward is None:
            raise AdtNotFound("No ward with that id is set up", code="ward_not_found")
        bed = _bed_by_label(db, ward["id"], b_text)
    else:
        match = match_location_on(db, w_text, b_text)
        ward, bed = match["ward"], match["bed"]

    if ward is not None and not ward.get("is_active"):
        raise AdtConflict(
            f"Ward {ward['code']} is switched off in Bed setup",
            code="ward_inactive",
            ward_id=ward["id"],
        )
    oos_reason = _text(out_of_service_reason, _MAX_REASON)
    if bed is not None:
        if bed.get("retired_at"):
            raise AdtConflict(
                f"{_bed_words(ward, bed)} is retired. Choose another bed.",
                code="bed_retired",
                bed_id=bed["id"],
            )
        if not bed.get("is_active"):
            raise AdtConflict(
                f"{_bed_words(ward, bed)} is switched off. Choose another bed.",
                code="bed_inactive",
                bed_id=bed["id"],
            )
        if bed.get("state") == "out_of_service" and not oos_reason:
            raise AdtConflict(
                f"{_bed_words(ward, bed)} is recorded out of service by housekeeping. "
                "Type the reason the patient is placed there anyway; it is recorded.",
                code="out_of_service_reason_required",
                bed_id=bed["id"],
            )
    return {
        "ward": ward,
        "bed": bed,
        "ward_id": ward["id"] if ward else None,
        "bed_id": bed["id"] if bed else None,
        # The master's spelling when linked, else the typed text as typed
        # (spaces tidied).
        "ward_text": ward["code"] if ward else w_text,
        "bed_text": bed["bed_label"] if bed else b_text,
        "out_of_service_reason": oos_reason
        if (bed and bed.get("state") == "out_of_service")
        else None,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Strict locations (adt-16, W9-A; ORB_ADT_STRICT_LOCATIONS, ruling F104)
# ═════════════════════════════════════════════════════════════════════════════
#
# F104 as ruled (the architect's recommendation): a ward or bed that is not in
# bed setup is admitted unlinked and said so (the warning) until the bed
# manager has set the hospital up; then the site turns strict on. With strict
# on (default off; it needs ORB_ADT, config.FEATURE_PREREQUISITES):
#   * a ward bed setup does not hold is refused on every lane that names a
#     place: the admission form, Scribe's quick admission, a bed assignment, a
#     bed change, a transfer (the front desk's included, which delegates to
#     services/adt/transfer.py), a correction, and a roster file (its rows are
#     rejected in the dry run too, services/roster_import.py);
#   * a bed bed setup does not hold is refused on a ward whose bed register
#     the bed manager has marked complete. On a ward still being set up the
#     typed bed is kept unlinked with the warning, as before: the per-ward
#     half of 'warn until setup is complete';
#   * a bed label with no ward names no place and is refused;
#   * the place an admission is ALREADY recorded at is never refused (a
#     transfer that keeps the patient where they lie, a stay recorded before
#     strict was switched on): strict governs the places a request names
#     anew, and the bed manager links an old one from the reconciliation list.
# A refusal is 400 with words and is raised before anything is written.

#: The refusal codes strict locations answers with.
STRICT_LOCATION_CODES: tuple[str, ...] = ("ward_not_in_setup", "bed_not_in_setup")


def strict_locations_on() -> bool:
    """True while ORB_ADT_STRICT_LOCATIONS is on and its prerequisite (ORB_ADT
    on) holds. Read at call time."""
    return bool(config.feature_enabled("adt_strict_locations"))


def _recorded_place_on(db, admission_id: str | None) -> dict | None:
    """The place an admission is recorded at now, or None (no admission yet)."""
    if not admission_id:
        return None
    return _row(
        db.execute(
            "SELECT ward, bed, ward_id, bed_id FROM admissions WHERE id = ?", (admission_id,)
        ).fetchone()
    )


def strict_place_refusal(target: dict, *, recorded: dict | None = None) -> AdtError | None:
    """The refusal strict locations gives the place `target` (what
    resolve_place_on returned), or None: always None while strict is off.

    `recorded` is the admission's current place (ward, bed, ward_id, bed_id);
    a place equal to it is never refused (see the section comment)."""
    if not strict_locations_on():
        return None
    ward, bed = target.get("ward"), target.get("bed")
    w_text, b_text = _text(target.get("ward_text")), _text(target.get("bed_text"))
    rec = recorded or {}
    if ward is None and w_text:
        # Exempt only the very place recorded (ward AND bed): another bed on
        # an unlisted ward is a new place bed setup does not hold.
        if (
            not _text(rec.get("ward_id"))
            and _same(w_text, rec.get("ward"))
            and not _text(rec.get("bed_id"))
            and _same(b_text, rec.get("bed"))
        ):
            return None
        return AdtUnlistedPlace(
            f"Ward {w_text} is not in bed setup, and this hospital records only wards and beds "
            "from bed setup. Choose a ward from the list, or ask a bed manager to set this ward "
            "up. Nothing was saved.",
            code="ward_not_in_setup",
            ward=w_text,
        )
    if bed is None and b_text:
        if ward is None:
            if (
                not _text(rec.get("ward"))
                and not _text(rec.get("bed_id"))
                and _same(b_text, rec.get("bed"))
            ):
                return None
            return AdtUnlistedPlace(
                f"Bed {b_text} names no ward from bed setup. Choose the ward and the bed from "
                "the list. Nothing was saved.",
                code="bed_not_in_setup",
                bed=b_text,
            )
        if not ward.get("register_complete"):
            # F104, per ward: this ward is still being set up, so the typed
            # bed is kept unlinked with the warning.
            return None
        same_ward = rec.get("ward_id") == ward["id"] or (
            not _text(rec.get("ward_id")) and _same(rec.get("ward"), ward["code"])
        )
        if same_ward and not _text(rec.get("bed_id")) and _same(b_text, rec.get("bed")):
            return None
        return AdtUnlistedPlace(
            f"Bed {b_text} is not in bed setup on {ward['code']}, whose bed register is "
            "complete. Choose a bed from the list or No bed yet, or ask a bed manager to add "
            "the bed. Nothing was saved.",
            code="bed_not_in_setup",
            ward_id=ward["id"],
            bed=b_text,
        )
    return None


# ═════════════════════════════════════════════════════════════════════════════
#  Moves between facilities (multisite-8h1, W12-F; ruling MS-R3 (c))
# ═════════════════════════════════════════════════════════════════════════════
#
# An admission's facility never changes (admissions.facility_id, migration 70),
# and Orb tells which facility a chart belongs to from the patient's admissions,
# which is exact only while they are all at one facility (the multi-site
# design's attribution invariant). So while more than one facility is on record
# every lane that writes a place or a treating department refuses a ward, or a
# department, at another facility than the admission's own: a bed assignment, a
# bed change, a transfer (the front desk's, the ward clerk's ordered transfer and
# the Relay transfer lane included, which all come through here), a swap, a
# correction and a reconciliation link. MS-R3 stays at (c), refuse, until it and
# R009 are ruled. The refusal is raised before anything is written and carries
# the facts of the authorization row (cross_facility_move_refused), which the
# caller writes once its own session has rolled back (audit_facility_refusal):
# a row written inside the rolled-back transaction would be lost. With one
# facility on record nothing is read.

CROSS_FACILITY_CODE = "cross_facility_move_refused"

CROSS_FACILITY_WARD_WORDS = (
    "That ward belongs to another facility of the group. Orb does not move a patient "
    "between facilities yet, so nothing was changed. Choose a ward of the facility the "
    "patient is at."
)


class AdtCrossFacility(AdtConflict):
    """A move into another facility refused (multisite-8h1, MS-R3 (c)). 409.
    `facts` holds the authorization row's fields; `audited` turns True once
    audit_facility_refusal has written it, so it is written once."""

    def __init__(self, message: str, *, facts: dict) -> None:
        super().__init__(message, code=CROSS_FACILITY_CODE)
        self.facts = facts
        self.audited = False


def cross_facility_refusal_on(
    db: Any,
    *,
    admission_id: str | None,
    ward: dict | None,
    to_department: str | None = None,
) -> AdtCrossFacility | None:
    """The refusal for putting this admission on `ward`, or giving it the
    treating department `to_department`, at another facility than the
    admission's own; None when neither crosses, when the admission is not
    written yet (the admission door checks its own facility first), and
    always while one facility is on record (nothing is read)."""
    from services import facility_lanes

    if not admission_id or not facility_lanes.multi_facility(db):
        return None
    adm = db.execute(
        "SELECT id, patient_id, department, facility_id FROM admissions WHERE id = ?",
        (admission_id,),
    ).fetchone()
    if adm is None:
        return None
    home = int(adm["facility_id"] or 1)
    to_facility: int | None = None
    cause: str | None = None
    if ward is not None and int(ward.get("facility_id") or 1) != home:
        to_facility, cause = int(ward.get("facility_id") or 1), "ward_at_another_facility"
    elif to_department is not None and not _same(to_department, adm["department"]):
        from services.frontdesk_service import department_facility_on

        at = department_facility_on(db, to_department)
        if at is None:
            cause = "department_not_listed"
        elif at != home:
            to_facility, cause = at, "patient_at_another_facility"
    if cause is None:
        return None
    from services.frontdesk_service import CROSS_FACILITY_TRANSFER_WORDS, patient_facility_ids_on

    return AdtCrossFacility(
        CROSS_FACILITY_WARD_WORDS
        if cause == "ward_at_another_facility"
        else CROSS_FACILITY_TRANSFER_WORDS,
        facts={
            "cause": cause,
            "patient_id": adm["patient_id"],
            "admission_id": adm["id"],
            "to_department": to_department,
            "to_ward_id": (ward or {}).get("id"),
            "to_facility_id": to_facility,
            "from_facility_ids": sorted(patient_facility_ids_on(db, adm["patient_id"])),
        },
    )


def audit_facility_refusal(
    err: BaseException, *, lane: str, actor: dict | None, request_meta: dict | None = None
) -> None:
    """The authorization 'cross_facility_move_refused' row for a refusal
    cross_facility_refusal_on raised, once, on a connection of its own. Call
    it after the caller's session has closed. Does nothing for any other
    error. BLOCKING; fail-soft like every authorization row."""
    if not isinstance(err, AdtCrossFacility) or err.audited:
        return
    from services.frontdesk_service import audit_cross_facility_refusal

    facts = err.facts
    audit_cross_facility_refusal(
        lane=lane,
        cause=str(facts.get("cause")),
        patient_id=facts.get("patient_id"),
        actor_id=_actor_id(actor),
        actor_role=(actor or {}).get("role"),
        to_department=facts.get("to_department"),
        to_facility_id=facts.get("to_facility_id"),
        from_facility_ids=facts.get("from_facility_ids") or (),
        admission_id=facts.get("admission_id"),
        request_meta=request_meta,
    )
    err.audited = True


# ═════════════════════════════════════════════════════════════════════════════
#  The single location writer
# ═════════════════════════════════════════════════════════════════════════════

#: The patient_movement audit action for each movement type this writer
#: records (design section 9).
MOVEMENT_AUDIT_ACTIONS: dict[str, str] = {
    "admission": "patient_admitted_to_bed",
    "bed_assigned": "bed_assigned",
    "bed_change": "bed_changed",
    "transfer": "patient_transferred",
    "location_linked": "admission_location_linked",
    "correction": "movement_corrected",
}

_LOCATING_TYPES = frozenset(MOVEMENT_AUDIT_ACTIONS)


def boarder_words(ward: dict, treating_department: str | None) -> str:
    """What a boarder placement means for who can open the chart (ADT-S6 is
    not applied: scope follows the treating department)."""
    return (
        f"Nurses on {ward['code']} ({ward.get('department')}) will not be able to open "
        f"this chart. The {treating_department or 'treating'} team keeps it."
    )


def _case_room_line_on(db, patient_id: str, text: str) -> bool:
    """A system line in the patient's case room, on the caller's connection.
    False when the patient has no case room (nothing is created here)."""
    room = db.execute(
        "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room' "
        "ORDER BY created_at DESC LIMIT 1",
        (patient_id,),
    ).fetchone()
    if room is None:
        return False
    # messages.timestamp in SQLite's own UTC form, like every other message in
    # the room (admission_service), so the line sorts where it happened.
    stamp = parse_to_utc(utc_iso_now()).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content,
                                 message_type, timestamp, is_bot, source)
           VALUES (?, 'system', 'Orb System', 'system', ?, 'system', ?, 1, 'system')""",
        (room["id"], text, stamp),
    )
    return True


def locate_admission_on(
    db: Any,
    *,
    admission_id: str,
    actor: dict | None,
    movement_type: str,
    reason: str,
    ward_id: str | None = None,
    bed_id: str | None = None,
    ward_text: Any = None,
    bed_text: Any = None,
    expected_bed_id: Any = _UNSET,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    moved_at: Any = None,
    idempotency_key: str | None = None,
    notes: str | None = None,
    request_meta: dict | None = None,
    extra_detail: dict | None = None,
    to_department: str | None = None,
) -> dict:
    """Put an active admission in a place, on the caller's connection.

    THE ONLY WRITER of admissions.ward_id, admissions.bed_id, admissions.ward,
    admissions.bed and (for the patient's current admission) patients.ward and
    patients.bed. Writes one movement of `movement_type` and one
    patient_movement audit in the same transaction; a boarder placement adds a
    clinical_access audit and a case-room line. Returns the new location, the
    movement and whether it was a boarder placement. A replayed
    Idempotency-Key returns the first result and writes nothing.

    `extra_detail` (adt-5) is merged into the movement's detail_json and the
    audit metadata: what the placement rules recorded (a reason a bed was used
    before it was cleaned, who authorised a bed awaiting a terminal clean, the
    extra bed that was added). Callers that place a patient into a bed go
    through place_admission_on, which applies those rules first.

    `to_department` (adt-6a, a transfer only) is the treating department the
    admission moves to, already resolved against clinical cover by the caller
    (services/adt/transfer.py, rule C) before its session opened. It is written
    to admissions.department in the same statement as the place (rule A: the
    same admission row, status untouched), the movement records it as 'to',
    and the boarder rule reads it: the ward is judged against the department
    the patient will be treated by, and a patient who stays on a ward while
    their treating department changes becomes a boarder there. Absent (every
    other caller), the department is never written and nothing here differs.

    The caller must hold db_session(immediate=True) as the outermost session,
    so the occupancy check and the write see the same state."""
    if movement_type not in _LOCATING_TYPES:
        raise AdtError(f"{movement_type!r} does not place a patient", code="unknown_movement_type")
    prior = movement_for_key(db, idempotency_key)
    if prior is not None:
        if prior.get("admission_id") != admission_id:
            raise AdtConflict(
                "That Idempotency-Key was already used for another admission",
                code="idempotency_key_reused",
            )
        return {
            "replayed": True,
            "movement": prior,
            "admission_id": admission_id,
            "patient_id": prior.get("patient_id"),
        }

    adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
    if adm is None:
        raise AdtNotFound("No admission with that id", code="admission_not_found")
    if adm.get("status") != "active":
        raise AdtConflict(
            "That admission has ended, so it has no bed to change",
            code="admission_not_active",
            status=adm.get("status"),
        )
    if expected_bed_id is not _UNSET and (expected_bed_id or None) != (adm.get("bed_id") or None):
        raise AdtConflict(
            "Where this patient lies changed since the screen was loaded. Reload and try again.",
            code="state_changed",
            expected_bed_id=expected_bed_id,
            current_bed_id=adm.get("bed_id"),
        )

    target = _resolve_target(
        db,
        ward_id=ward_id,
        bed_id=bed_id,
        ward_text=ward_text,
        bed_text=bed_text,
        out_of_service_reason=out_of_service_reason,
    )
    ward, bed = target["ward"], target["bed"]
    # adt-16: with strict locations on, a place not in bed setup is refused
    # here too, so no lane that writes a place can leave one unlinked.
    unlisted = strict_place_refusal(target, recorded=adm)
    if unlisted is not None:
        raise unlisted
    # multisite-8h1: never a ward or a treating department at another
    # facility than the admission's own (ruling MS-R3 (c)).
    crossing = cross_facility_refusal_on(
        db, admission_id=admission_id, ward=ward, to_department=to_department
    )
    if crossing is not None:
        raise crossing

    if bed is not None:
        holder = db.execute(
            "SELECT id FROM admissions WHERE bed_id = ? AND status = 'active' AND id <> ?",
            (bed["id"], admission_id),
        ).fetchone()
        if holder is not None:
            raise AdtConflict(
                f"{_bed_words(ward, bed)} already holds a patient. Choose another bed, or "
                "add an extra bed with its own label.",
                code="bed_occupied",
                bed_id=bed["id"],
            )

    # adt-12: a bed held by a reservation takes only the admission or patient
    # it is held for, whatever lane writes the place (a link and a correction
    # included), and that reservation is fulfilled below in this transaction.
    # A bed the admission already lies in is not re-checked.
    held = None
    if bed is not None and bed["id"] != adm.get("bed_id"):
        from services.adt import reservations as adt_res

        held = adt_res.live_on_bed(db, bed["id"])
        if held is not None and not adt_res.is_for(
            held, admission_id=admission_id, patient_id=adm["patient_id"]
        ):
            raise adt_res.refusal(ward=ward, bed=bed, reservation=held)

    # The treating department after this write: unchanged, except for a
    # transfer that names the department it moves to (adt-6a).
    treating = to_department if to_department is not None else adm.get("department")
    department_changes = to_department is not None and to_department != adm.get("department")
    boarder = (
        ward is not None
        and ward.get("department") is not None
        and not _same(ward.get("department"), treating)
        and (ward["id"] != adm.get("ward_id") or department_changes)
    )
    if boarder and not boarder_acknowledged:
        raise AdtConflict(
            boarder_words(ward, treating),
            code="boarder_acknowledgement_required",
            ward_department=ward.get("department"),
            treating_department=treating,
        )

    before = {
        "department": adm.get("department"),
        "ward": adm.get("ward"),
        "bed": adm.get("bed"),
        "ward_id": adm.get("ward_id"),
        "bed_id": adm.get("bed_id"),
    }
    after = {
        "department": treating,
        "ward": target["ward_text"],
        "bed": target["bed_text"],
        "ward_id": target["ward_id"],
        "bed_id": target["bed_id"],
    }
    if all(before[k] == after[k] for k in ("department", "ward", "bed", "ward_id", "bed_id")):
        raise AdtConflict("The admission is already recorded at that place", code="no_change")

    try:
        if department_changes:
            # Rule A: the same admission row carries the new department; its
            # status is not touched (rule B).
            db.execute(
                "UPDATE admissions SET ward_id = ?, bed_id = ?, ward = ?, bed = ?, department = ? "
                "WHERE id = ? AND status = 'active'",
                (
                    after["ward_id"],
                    after["bed_id"],
                    after["ward"],
                    after["bed"],
                    treating,
                    admission_id,
                ),
            )
        else:
            db.execute(
                "UPDATE admissions SET ward_id = ?, bed_id = ?, ward = ?, bed = ? "
                "WHERE id = ? AND status = 'active'",
                (after["ward_id"], after["bed_id"], after["ward"], after["bed"], admission_id),
            )
    except DBIntegrityError as exc:
        # The partial unique index: another writer took the bed between the
        # check above and this statement (only possible without an immediate
        # session). Words, not a stack trace.
        raise AdtConflict(
            "That bed was just taken by another admission. Choose another bed.",
            code="bed_occupied",
            bed_id=after["bed_id"],
        ) from exc

    # The patients mirror holds the CURRENT admission's place only.
    current = current_admission_on(db, adm["patient_id"])
    if current is not None and current["id"] == admission_id:
        db.execute(
            "UPDATE patients SET ward = ?, bed = ? WHERE id = ?",
            (after["ward"], after["bed"], adm["patient_id"]),
        )

    detail: dict[str, Any] = dict(extra_detail or {})
    if target["out_of_service_reason"]:
        detail["out_of_service_reason"] = target["out_of_service_reason"]
    if boarder:
        detail["boarder"] = {
            "ward_department": ward.get("department"),
            "treating_department": treating,
        }
    actor_name = None
    if _actor_id(actor):
        name_row = db.execute("SELECT name FROM users WHERE id = ?", (_actor_id(actor),)).fetchone()
        actor_name = name_row["name"] if name_row else None
    movement = record_movement_on(
        db,
        patient_id=adm["patient_id"],
        admission_id=admission_id,
        movement_type=movement_type,
        reason=reason,
        moved_at=moved_at,
        moved_by=_actor_id(actor),
        moved_by_name=actor_name,
        from_location=before,
        to_location=after,
        notes=notes,
        idempotency_key=idempotency_key,
        detail=detail or None,
    )
    audit_on(
        db,
        category="patient_movement",
        action=MOVEMENT_AUDIT_ACTIONS[movement_type],
        summary=(
            f"Admission {admission_id} placed at {after['ward'] or 'no ward'}"
            f"/{after['bed'] or 'no bed'} ({movement_type.replace('_', ' ')})"
        ),
        actor=actor,
        patient_id=adm["patient_id"],
        target_type="admission",
        target_id=admission_id,
        request_meta=request_meta,
        metadata={
            "movement_id": movement["id"],
            "movement_type": movement_type,
            "from": before,
            "to": after,
            **({"detail": detail} if detail else {}),
        },
    )
    if boarder:
        audit_on(
            db,
            category="clinical_access",
            action="boarder_placed",
            summary=(
                f"Admission {admission_id} placed on {ward['code']} "
                f"({ward.get('department')}) while treated by {treating}"
            ),
            actor=actor,
            patient_id=adm["patient_id"],
            target_type="admission",
            target_id=admission_id,
            request_meta=request_meta,
            metadata={
                "movement_id": movement["id"],
                "ward_id": ward["id"],
                "ward_department": ward.get("department"),
                "treating_department": treating,
                "acknowledged": True,
            },
        )
        _case_room_line_on(
            db,
            adm["patient_id"],
            f"Placed as a boarder on {ward['code']} ({ward.get('department')}), "
            f"bed {after['bed'] or 'not yet chosen'}. {boarder_words(ward, treating)}",
        )

    fulfilled = None
    if held is not None:
        from services.adt import reservations as adt_res

        fulfilled = adt_res.fulfil_on(
            db,
            held,
            admission_id=admission_id,
            movement_id=movement["id"],
            actor=actor,
            request_meta=request_meta,
        )

    return {
        "replayed": False,
        "admission_id": admission_id,
        "patient_id": adm["patient_id"],
        "ward_id": after["ward_id"],
        "bed_id": after["bed_id"],
        "ward": after["ward"],
        "bed": after["bed"],
        "linked": after["bed_id"] is not None,
        "boarder": boarder,
        "movement": movement,
        **({"reservation_fulfilled": fulfilled["id"]} if fulfilled else {}),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Admitting into a master bed, and assigning a bed (adt-5, W5-B)
# ═════════════════════════════════════════════════════════════════════════════
#
# With ORB_ADT on, two doors place a patient into a bed: the admission itself
# (admission_service.admit_patient, used by the admission form and by Scribe's
# quick admission) and POST /api/adt/admissions/{id}/bed (a bed for an
# admission awaiting one, or a correction of a bed recorded wrongly). Both run
# prepare_place_on and then place_admission_on in ONE immediate session: the
# rules below are applied first, then locate_admission_on writes the place,
# the movement and the patient_movement audit.
#
# RULINGS APPLIED AS BUILD DEFAULTS (the strictest option named; ORB_ADT stays
# off at every site until Samuel confirms them):
#   ADT-S1 (a)  A bed another active admission holds is refused, whether it
#               holds it by bed_id (the unique index refuses it underneath as
#               well) or by typed ward and bed text that names it. 'Add an
#               extra bed' registers a temporary bed beside the occupied one,
#               with its own label (GM-1/12A), and places the patient there.
#               There is no shared bed and no shared-bed reason. A place NOT
#               in bed setup (kept unlinked under ruling F104) is refused the
#               same way when another admitted patient is recorded at the same
#               typed ward and bed (round 3 review: the module used to drop the
#               old double-booking warning there and put nothing in its
#               place); there is no master bed to add an extra bed beside, so
#               the words ask for a bed label of its own.
#   ADT-S2 (c)  On a ward whose cleaning housekeeping tracks, a bed waiting for
#               or under a STANDARD clean takes a typed reason, recorded on the
#               movement. A bed whose next clean is TERMINAL is refused unless
#               a doctor, nurse or administrator authorises it, with a reason;
#               both are recorded. The next clean is terminal when an open
#               terminal clean is raised on the bed, when the bed or its room
#               is an isolation room (housekeeping's own rule: every clean of
#               one is terminal), or when a stay that carried isolation
#               precautions left the bed after it was last recorded clean. That
#               is read from the ledger, not from the cleaning task alone,
#               because housekeeping raises its task only when its board is
#               next opened (round 3 review). An out-of-service bed takes a
#               typed reason (the adt-3 rule) and, when its next clean is
#               terminal, the authorisation as well: being out of service never
#               hides a terminal clean. THE AUTHORISER ACTS: it is the person
#               sending the request, when their role allows, or a person who
#               confirms it on the spot with their own signing PIN
#               (services/pin_security, its lockout counted and every refused
#               PIN audited), as a pharmacy transfer's witness does. Naming
#               someone is never enough (round 3 review: a clerk named a nurse
#               and the record said the nurse authorised it). The authoriser
#               must work in the ward's department or the treating department,
#               or hold no department (hospital-wide). A ward whose cleaning is
#               not tracked raises nothing.
#   ADT-P3      An admission or bed time may be entered up to
#               ORB_ADT_BACKTIME_HOURS (72) back, never in the future. Older
#               needs an administrator with a reason, which is not built, so it
#               is refused.
# A link (location_linked) records where a patient already lies, so the
# cleaning rule never refuses it. A correction is only for an admission with
# a bed recorded (design section 5: 'when bed text is present'). One that
# names the bed the patient is already recorded in (linking typed text to its
# master bed) is not refused by the cleaning rule either; one that names a
# DIFFERENT bed puts the patient into that bed, so ADT-S2 (c) applies to it in
# full and the reason or the named authoriser is recorded on the movement,
# exactly as at admission. The occupancy rule refuses a correction into a bed
# another admission holds.

#: Who may authorise a patient into a bed still waiting for its terminal clean
#: (ruling ADT-S2 (c)): a clinician or an administrator, never a clerk, a desk
#: role or a service account; the sender themself, or someone who confirms with
#: their own signing PIN. For Samuel to confirm.
TERMINAL_CLEAN_AUTHORISER_ROLES: tuple[str, ...] = ("doctor", "clinician", "nurse", "admin")

#: Movement types that put a patient INTO a bed, so the cleaning rule applies.
#: A 'correction' does too whenever it names a bed other than the one the
#: admission is recorded in (prepare_place_on, _recorded_in_on).
_PLACING_TYPES = frozenset({"admission", "bed_assigned", "bed_change", "transfer"})

#: An event time may sit this far in the future before it is refused (clock
#: skew between a ward tablet and the appliance), as for an end of stay.
_FUTURE_SLACK = timedelta(minutes=5)

#: Bed board statuses that mean a patient lies in the bed.
_HELD_STATUSES = frozenset({"occupied", "occupied_on_leave", "occupied_absconded", "boarder"})

#: Bed board statuses of a bed nobody has recorded clean.
_NOT_CLEAN_STATUSES = frozenset({"needs_cleaning", "cleaning"})

#: Statuses in which a pending terminal clean is looked for. Out of service is
#: one: housekeeping takes such a bed back only through a clean
#: (out_of_service to vacant_dirty), so a patient placed there skips that
#: clean, and the out-of-service status must not hide that it is terminal.
_TERMINAL_CHECK_STATUSES = _NOT_CLEAN_STATUSES | frozenset({"out_of_service"})

#: Letters an extra bed's label takes after the occupied bed's label (12A).
_EXTRA_BED_SUFFIXES = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def check_event_time(
    value: Any, *, what: str = "admission", not_before: datetime | None = None
) -> datetime:
    """When an admission or a bed move happened: now when blank. Refused (400)
    when unreadable, in the future, before `not_before` (the stay's start), or
    further back than ADT_BACKTIME_HOURS (ruling ADT-P3)."""
    now = datetime.now(UTC)
    if value is None or (isinstance(value, str) and not value.strip()):
        return now
    when = parse_instant(value)
    if when is None:
        raise AdtBadTime(
            f"The {what} time could not be read. Give a date and a time.", code="bad_time"
        )
    if when > now + _FUTURE_SLACK:
        raise AdtBadTime(f"The {what} time is in the future.", code="future_time")
    if not_before is not None and when < not_before:
        raise AdtBadTime(
            f"The {what} time is before the patient was admitted.", code="before_admission"
        )
    hours = int(getattr(config, "ADT_BACKTIME_HOURS", 72))
    if now - when > timedelta(hours=hours):
        raise AdtBadTime(
            f"The {what} time can be entered up to {hours} hours back. Older entries "
            "need an administrator.",
            code="beyond_backtime",
            backtime_hours=hours,
        )
    return when


def resolve_place_on(
    db,
    *,
    ward_id: Any = None,
    bed_id: Any = None,
    ward_text: Any = None,
    bed_text: Any = None,
    out_of_service_reason: str | None = None,
) -> dict:
    """The place chosen or typed, checked against the master, writing nothing:
    {ward, bed, ward_id, bed_id, ward_text, bed_text, out_of_service_reason}.
    Ids are used as given; text resolves by design section 5. Refuses, with
    words, a bed or ward that is not set up, retired or switched off, and an
    out-of-service bed with no reason."""
    return _resolve_target(
        db,
        ward_id=_text(ward_id),
        bed_id=_text(bed_id),
        ward_text=ward_text,
        bed_text=bed_text,
        out_of_service_reason=out_of_service_reason,
    )


def _terminal_clean_beds_on(db, bed_ids: Any) -> set:
    """The beds among `bed_ids` with an open or running TERMINAL clean."""
    ids = tuple(b for b in bed_ids if b)
    if not ids:
        return set()
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'housekeeping_tasks'"
        ).fetchone()
        is None
    ):
        return set()
    marks = ",".join("?" for _ in ids)
    return {
        r[0]
        for r in db.execute(
            f"""SELECT DISTINCT unit_id FROM housekeeping_tasks
             WHERE status IN ('open', 'in_progress') AND clean_type = 'terminal'
               AND unit_id IN ({marks})""",
            ids,
        ).fetchall()
    }


def _board_tiles_on(db, wards: list, *, reservations: bool = True) -> dict:
    """The bed board's tiles for `wards` with no patient field read or returned
    (identity off, so no read audit is owed). bed_master_service derives every
    status; this module never derives one of its own. With reservations False
    a held bed reads its cleaning status (adt-12: the patient a bed is held
    for still meets the cleaning rule when placed in it)."""
    from services import bed_master_service as bm

    return bm._board_on(db, wards=wards, dept=None, identity=False, reservations=reservations)


def carried_isolation(value: Any) -> bool:
    """Whether a stored admissions.isolation_precautions value names any
    precaution. admission_service stores a JSON list ('[]' when there are
    none); older rows may hold free text, which counts when it is not blank.
    This is housekeeping's boolean (did the departing stay carry isolation
    precautions?) read so that an empty list is not a precaution."""
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return any(_text(v) for v in value)
    text = str(value).strip()
    if not text:
        return False
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return True
    if isinstance(parsed, (list, tuple)):
        return any(_text(v) for v in parsed)
    if isinstance(parsed, str):
        return bool(_text(parsed))
    return parsed not in (None, False, {})


def _vacated_with_isolation_on(db, *, wards: list, bed_ids: Any) -> set:
    """The beds among `bed_ids` that a stay carrying isolation precautions
    left after the bed was last recorded clean (or that has never been
    recorded clean). A departure is any vacating movement out of the bed in
    the ledger (bed_master_service.VACATING_MOVEMENT_TYPES, an end of stay
    included), by its master bed id or, for a movement written before the bed
    was linked, by its typed ward and bed text, read the way the bed board
    reads it. Every such departure since the last clean counts, not only the
    latest: a terminal clean skipped once is still owed."""
    from services import bed_master_service as bm

    ids = tuple(b for b in bed_ids if b)
    if (
        not ids
        or db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'patient_movements'"
        ).fetchone()
        is None
    ):
        return set()
    marks = ",".join("?" for _ in ids)
    beds = [
        dict(r)
        for r in db.execute(
            f"SELECT id, ward_id, bed_label, state, state_since FROM housekeeping_units "
            f"WHERE id IN ({marks})",
            ids,
        ).fetchall()
    ]
    if not beds:
        return set()
    ready = bm._last_ready_jd(db, beds)
    last_clean: dict[str, float | None] = {}
    for b in beds:
        jd = ready.get(b["id"])
        if jd is None and b.get("state") == "vacant_ready":
            jd = bm._jd(b.get("state_since"))
        last_clean[b["id"]] = jd

    types = tuple(bm.VACATING_MOVEMENT_TYPES)
    type_marks = ",".join("?" for _ in types)
    departures: list[tuple[str, float | None, Any]] = [
        (r[0], r[1], r[2])
        for r in db.execute(
            f"""SELECT m.from_bed_id, julianday(m.moved_at), a.isolation_precautions
                  FROM patient_movements m LEFT JOIN admissions a ON a.id = m.admission_id
                 WHERE m.from_bed_id IN ({marks}) AND m.movement_type IN ({type_marks})
                   AND (m.to_bed_id IS NULL OR m.to_bed_id <> m.from_bed_id)""",
            ids + types,
        ).fetchall()
    ]
    codes = {bm._key(w["code"]): w for w in wards if w.get("code")}
    labels = {(b["ward_id"], bm._key(b.get("bed_label") or "")): b["id"] for b in beds}
    if codes:
        ward_marks = ",".join("?" for _ in codes)
        for r in db.execute(
            f"""SELECT LOWER(TRIM(m.from_ward)), LOWER(TRIM(m.from_bed)), julianday(m.moved_at),
                       a.isolation_precautions
                  FROM patient_movements m LEFT JOIN admissions a ON a.id = m.admission_id
                 WHERE m.from_bed_id IS NULL AND m.movement_type IN ({type_marks})
                   AND LOWER(TRIM(COALESCE(m.from_ward, ''))) IN ({ward_marks})
                   AND NOT (LOWER(TRIM(COALESCE(m.to_ward, ''))) = LOWER(TRIM(COALESCE(m.from_ward, '')))
                            AND LOWER(TRIM(COALESCE(m.to_bed, ''))) = LOWER(TRIM(COALESCE(m.from_bed, ''))))""",
            types + tuple(codes),
        ).fetchall():
            ward = codes.get(r[0] or "")
            bed_id = labels.get((ward["id"], r[1] or "")) if ward else None
            if bed_id is not None:
                departures.append((bed_id, r[2], r[3]))

    out: set = set()
    for bed_id, left_jd, precautions in departures:
        clean_jd = last_clean.get(bed_id)
        # A departure and a clean in the same instant read as not cleaned
        # since, as on the bed board; an unreadable time counts as after.
        since_clean = left_jd is None or clean_jd is None or left_jd >= clean_jd
        if since_clean and carried_isolation(precautions):
            out.add(bed_id)
    return out


def _terminal_pending_on(db, *, wards: list, tiles: list) -> set:
    """The beds among the bed board `tiles` (of wards whose cleaning is
    tracked) whose next clean is TERMINAL and has not been done: an open or
    running terminal clean, a bed or room set up as an isolation room, or a
    departure with isolation precautions since the last clean. Only tiles not
    recorded clean or out of service are looked at."""
    candidates = [t for t in tiles if t["status"] in _TERMINAL_CHECK_STATUSES]
    if not candidates:
        return set()
    ids = [t["bed_id"] for t in candidates]
    pending = set(_terminal_clean_beds_on(db, ids))
    pending |= {t["bed_id"] for t in candidates if t.get("isolation")}
    rest = [i for i in ids if i not in pending]
    if rest:
        pending |= _vacated_with_isolation_on(db, wards=wards, bed_ids=rest)
    return pending


def bed_readiness_on(db, *, ward: dict, bed: dict, reservations: bool = True) -> dict:
    """What the bed board says about one bed now: {status, status_words,
    cleaning_tracked, terminal_clean}. terminal_clean is True only for a bed
    on a tracked ward, not recorded clean or out of service, whose next clean
    is terminal (_terminal_pending_on). With reservations False a bed held by
    a reservation reads the status under the hold (adt-12)."""
    board = _board_tiles_on(db, [ward], reservations=reservations)
    tile = next((t for w in board["wards"] for t in w["tiles"] if t["bed_id"] == bed["id"]), None)
    tracked = bool(board["wards"][0]["cleaning_tracked"]) if board["wards"] else False
    status = tile["status"] if tile else "switched_off"
    terminal = (
        tile is not None
        and tracked
        and status in _TERMINAL_CHECK_STATUSES
        and tile["bed_id"] in _terminal_pending_on(db, wards=board["wards"], tiles=[tile])
    )
    return {
        "status": status,
        "status_words": tile["status_words"] if tile else None,
        "cleaning_tracked": tracked,
        "terminal_clean": terminal,
    }


def bed_choices_on(db: Any, *, facilities: frozenset[int] | None = None) -> list:
    """Every active ward with its beds, for the admission form's bed picker.

    Each bed says what placing a patient there needs: nothing, a typed reason
    (out of service, or not yet recorded clean), the authorisation of a
    doctor, nurse or administrator (its next clean is terminal; an
    out-of-service bed then says out_of_service True and needs its reason as
    well), or it cannot be chosen (occupied or reserved; an occupied bed
    offers an extra bed beside it instead). NO PATIENT DATA:
    an occupied bed says 'Occupied' and nothing about who lies in it.

    `facilities` (multisite-8h1) is the caller's facility scope: with one,
    the wards of those facilities only."""
    from services import bed_master_service as bm
    from services.facility_lanes import wards_in_scope

    wards = [
        dict(r)
        for r in db.execute(
            "SELECT * FROM wards WHERE is_active = 1 ORDER BY LOWER(code)"
        ).fetchall()
    ]
    wards = wards_in_scope(wards, facilities)
    board = _board_tiles_on(db, wards)
    tracked = [w for w in board["wards"] if w["cleaning_tracked"]]
    terminal = _terminal_pending_on(
        db, wards=tracked, tiles=[t for w in tracked for t in w["tiles"]]
    )
    occupied_words = bm.BOARD_STATUSES["occupied"][0]
    out = []
    for ward in board["wards"]:
        beds = []
        for tile in ward["tiles"]:
            status = tile["status"]
            if status == "switched_off":
                continue
            entry = {
                "bed_id": tile["bed_id"],
                "bed_label": tile["bed_label"],
                "unit_code": tile["unit_code"],
                "room": tile["room"],
                "bed_class": tile["bed_class"],
                "isolation": tile["isolation"],
                "temporary": tile["temporary"],
                "status": status,
                "status_words": tile["status_words"],
                "tone": tile["tone"],
                "markers": list(tile["markers"]),
                "occupied": False,
                "selectable": True,
                "needs": None,
                "terminal_clean": False,
                "out_of_service": False,
            }
            if status in _HELD_STATUSES:
                # Whoever lies here, nothing about them leaves this function.
                entry.update(
                    status="occupied",
                    status_words=occupied_words,
                    tone="occupied",
                    markers=[],
                    occupied=True,
                    selectable=False,
                )
            elif status == "reserved":
                entry.update(selectable=False)
            elif status == "out_of_service":
                # Its typed reason always; the terminal-clean authorisation as
                # well when its next clean is terminal (out_of_service says so).
                is_terminal = tile["bed_id"] in terminal
                entry.update(
                    needs="terminal_clean_authorisation"
                    if is_terminal
                    else "out_of_service_reason",
                    terminal_clean=is_terminal,
                    out_of_service=True,
                )
            elif status in _NOT_CLEAN_STATUSES:
                is_terminal = tile["bed_id"] in terminal
                entry.update(
                    needs="terminal_clean_authorisation" if is_terminal else "not_clean_reason",
                    terminal_clean=is_terminal,
                )
            beds.append(entry)
        out.append(
            {
                "id": ward["id"],
                "code": ward["code"],
                "name": ward.get("name"),
                "department": ward.get("department"),
                "ward_type": ward.get("ward_type"),
                "cleaning_tracked": ward["cleaning_tracked"],
                "register_complete": ward["register_complete"],
                "beds": beds,
            }
        )
    return out


def _typed_holder_on(db, *, admission_id: str | None, ward_text: Any, bed_text: Any) -> str | None:
    """The id of another active admission, or of an admitted patient, recorded
    at this typed ward and bed (without regard to capital letters or spaces),
    other than `admission_id` and its own patient. This is the check the
    admission form always made (admission_service.bed_in_use_warning reads the
    patients mirror), made here on the caller's immediate connection so the
    check and the write see the same state. None when either text is blank: a
    bed label with no ward names no place."""
    w, b = _text(ward_text), _text(bed_text)
    if not w or not b:
        return None
    own_patient = None
    if admission_id:
        own = db.execute(
            "SELECT patient_id FROM admissions WHERE id = ?", (admission_id,)
        ).fetchone()
        own_patient = own["patient_id"] if own is not None else None
    # SQLite's LOWER folds ASCII only, so the narrowing is by squeezed ASCII
    # text and the comparison that decides is _same, in Python.
    if b.isascii():
        args: tuple[str, ...]
        narrow, args = (
            "LOWER(REPLACE(COALESCE(bed, ''), ' ', '')) = ?",
            (b.replace(" ", "").lower(),),
        )
    else:
        narrow, args = "bed IS NOT NULL", ()
    for row in db.execute(
        f"SELECT id, ward, bed FROM admissions WHERE status = 'active' "
        f"AND (? IS NULL OR id <> ?) AND {narrow}",
        (admission_id, admission_id, *args),
    ).fetchall():
        if _same(row["ward"], w) and _same(row["bed"], b):
            return row["id"]
    for row in db.execute(
        f"SELECT id, ward, bed FROM patients WHERE status = 'admitted' "
        f"AND (? IS NULL OR id <> ?) AND {narrow}",
        (own_patient, own_patient, *args),
    ).fetchall():
        if _same(row["ward"], w) and _same(row["bed"], b):
            return row["id"]
    return None


def _holder_on(db, *, admission_id: str | None, ward: dict | None, bed: dict) -> str | None:
    """The id of another active admission lying in `bed`: by bed_id, or by
    typed ward and bed text naming it (it lies there as far as every screen
    that prints the text knows; _typed_holder_on). None when the bed is free."""
    row = db.execute(
        "SELECT id FROM admissions WHERE bed_id = ? AND status = 'active' "
        "AND (? IS NULL OR id <> ?) LIMIT 1",
        (bed["id"], admission_id, admission_id),
    ).fetchone()
    if row is not None:
        return row["id"]
    if ward is None:
        return None
    return _typed_holder_on(
        db, admission_id=admission_id, ward_text=ward["code"], bed_text=bed.get("bed_label")
    )


def _recorded_in_on(db, *, admission_id: str | None, ward: dict | None, bed: dict) -> bool:
    """True when the admission is already recorded in `bed`: by its master
    id, or (with no master bed recorded) by typed ward and bed text naming it,
    the same text match _holder_on uses. A correction that names this bed
    puts nobody into a new bed, so the cleaning rule does not apply to it."""
    if not admission_id:
        return False
    adm = _row(
        db.execute(
            "SELECT ward, bed, bed_id FROM admissions WHERE id = ?", (admission_id,)
        ).fetchone()
    )
    if adm is None:
        return False
    if _text(adm.get("bed_id")):
        return adm["bed_id"] == bed["id"]
    return (
        ward is not None
        and bool(_text(adm.get("bed")))
        and _same(adm.get("ward"), ward["code"])
        and _same(adm.get("bed"), bed.get("bed_label"))
    )


_AUTHORISER_INVALID_WORDS = (
    "The person authorising this bed must be an active doctor, nurse or "
    "administrator on this system."
)


def _authoriser_row_on(db, user_id: Any) -> dict | None:
    """The users row of a possible terminal-clean authoriser, or None when
    the id names nobody who may be one: an active account in
    TERMINAL_CLEAN_AUTHORISER_ROLES, never a service account."""
    from database import SERVICE_ACCOUNT_IDS

    uid = _text(user_id)
    if not uid or uid in SERVICE_ACCOUNT_IDS or uid.lower().startswith("bot_"):
        return None
    row = _row(
        db.execute(
            "SELECT id, name, role, department, pin_hash, COALESCE(is_active, 1) AS is_active "
            "FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
    )
    if (
        row is None
        or not row.get("is_active")
        or row.get("role") not in TERMINAL_CLEAN_AUTHORISER_ROLES
    ):
        return None
    return row


def _authoriser_department_ok(
    person: dict, *, ward: dict | None, treating_department: str | None
) -> bool:
    """The authoriser works in the ward's department or the treating
    department, or holds no department (a hospital-wide account), read
    through permissions.department_scope, the one derivation."""
    from permissions import department_scope

    dept = department_scope(person)
    if dept is None:
        return True
    return _same(dept, (ward or {}).get("department")) or _same(dept, treating_department)


def terminal_authoriser_eligible_on(
    db, user_id: Any, *, ward: dict | None = None, treating_department: str | None = None
) -> bool:
    """True when this person may authorise a bed awaiting a terminal clean on
    `ward` for a patient of `treating_department` (role, active, department).
    Without a ward and a department only the role and the account are read."""
    row = _authoriser_row_on(db, user_id)
    if row is None:
        return False
    if ward is None and treating_department is None:
        return True
    return _authoriser_department_ok(row, ward=ward, treating_department=treating_department)


def _authoriser_on(
    db, user_id: Any, *, ward: dict | None = None, treating_department: str | None = None
) -> dict:
    """The person authorising a bed awaiting a terminal clean: an active
    account in TERMINAL_CLEAN_AUTHORISER_ROLES, never a service account,
    working in the ward's department or the treating one (or hospital-wide).
    Raises with words otherwise."""
    row = _authoriser_row_on(db, user_id)
    if row is None:
        raise AdtError(_AUTHORISER_INVALID_WORDS, code="terminal_clean_authoriser_invalid")
    if not _authoriser_department_ok(row, ward=ward, treating_department=treating_department):
        where = (ward or {}).get("code") or "this ward"
        raise AdtError(
            f"{row.get('name') or row['id']} works in {row.get('department')}, so cannot "
            f"authorise a bed on {where} for a patient of {treating_department or 'another team'}. "
            "The authoriser must work in the ward's department or the treating team, or hold "
            "no department.",
            code="terminal_clean_authoriser_out_of_department",
            authoriser_id=row["id"],
        )
    return row


def _pin_refusal_audit(
    *,
    actor: dict | None,
    authoriser_id: str,
    why: str,
    request_meta: dict | None,
    patient_id: str | None,
    admission_id: str | None,
    bed_id: str | None,
) -> None:
    """A refused signing PIN for a terminal-clean authorisation: one
    fail-closed security line in a session of its own, written BEFORE the
    refusal is answered (the placement's own transaction never opened, so
    there is nothing for it to roll back with)."""
    meta = request_meta or {}
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type="terminal_clean_authoriser_pin_refused",
        outcome="failure",
        summary_message=(
            f"Signing PIN for {authoriser_id} refused ({why.replace('_', ' ')}) "
            "while authorising a bed awaiting a terminal clean"
        ),
        actor_user_id=_actor_id(actor),
        actor_role=(actor or {}).get("role"),
        patient_id=patient_id,
        target_type="admission" if admission_id else ("bed" if bed_id else None),
        target_id=str(admission_id or bed_id) if (admission_id or bed_id) else None,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata={
            "authoriser_id": authoriser_id,
            "why": why,
            "admission_id": admission_id,
            "bed_id": bed_id,
        },
    )


def confirm_terminal_authoriser(
    *,
    actor: dict | None,
    authorised_by: Any,
    pin: Any,
    admission_id: str | None = None,
    bed_id: Any = None,
    request_meta: dict | None = None,
) -> dict | None:
    """Check the signing PIN of the person named as authorising a bed
    awaiting a terminal clean, BEFORE the placement's own immediate session
    opens (verify_pin may open a session of its own to upgrade an old hash,
    and a refused PIN is audited in its own session so the refusal is on
    record). The same rule as a pharmacy transfer's witness.

    Returns {id, how: 'signing_pin'} for a PIN that confirms them, or None
    when there is nothing to confirm here: nobody named, the sender named
    themself (their own session is their confirmation; the placement rule
    checks their role), or no PIN typed (the placement rule refuses that
    with terminal_clean_pin_required if the bed needs it). Refuses:
    terminal_clean_authoriser_locked (429, after 5 wrong PINs in 15 minutes),
    terminal_clean_authoriser_invalid and terminal_clean_authoriser_no_pin
    (422), terminal_clean_pin_wrong (403, counted toward their lockout)."""
    from services import pin_security

    uid = _text(authorised_by)
    typed = pin if isinstance(pin, str) else ""
    if not uid or uid == _actor_id(actor) or not typed:
        return None
    patient_id = None
    if admission_id:
        with db_session() as db:
            found = db.execute(
                "SELECT patient_id FROM admissions WHERE id = ?", (admission_id,)
            ).fetchone()
        patient_id = found["patient_id"] if found is not None else None
    audit: _PinRefusalContext = {
        "actor": actor,
        "authoriser_id": uid,
        "request_meta": request_meta,
        "patient_id": patient_id,
        "admission_id": admission_id,
        "bed_id": _text(bed_id),
    }
    if pin_security.is_pin_locked(uid):
        _pin_refusal_audit(why="locked", **audit)
        raise AdtLocked(
            "Too many wrong PINs for this person. Their PIN entry is locked for 15 "
            "minutes. Nothing was saved.",
            code="terminal_clean_authoriser_locked",
        )
    with db_session() as db:
        person = _authoriser_row_on(db, uid)
    if person is None:
        raise AdtError(_AUTHORISER_INVALID_WORDS, code="terminal_clean_authoriser_invalid")
    if not person.get("pin_hash"):
        raise AdtError(
            f"{person.get('name') or uid} has no signing PIN yet, so cannot confirm here. Ask an "
            "administrator to set one, or have them send this from their own login.",
            code="terminal_clean_authoriser_no_pin",
        )
    if not pin_security.verify_pin(person, typed):
        pin_security.record_pin_failure(uid)
        _pin_refusal_audit(why="wrong_pin", **audit)
        raise AdtPinRefused(
            f"The signing PIN for {person.get('name') or uid} is not right. Nothing was saved.",
            code="terminal_clean_pin_wrong",
        )
    pin_security.clear_pin_failures(uid)
    return {"id": uid, "how": "signing_pin"}


def _placement_rules_on(
    db,
    *,
    ward: dict,
    bed: dict,
    actor: dict | None,
    treating_department: str | None,
    not_clean_reason: Any,
    terminal_clean_authorised_by: Any,
    terminal_clean_reason: Any,
    terminal_clean_confirmed: dict | None = None,
    admission_id: str | None = None,
    patient_id: str | None = None,
) -> dict:
    """ADT-S2 (c) and reservations for a bed a patient is being placed INTO.
    Returns what is recorded on the movement; raises with words otherwise.

    A bed held by a reservation (adt-12) is refused unless it is held for this
    admission or this patient (services/adt/reservations.is_for); the words
    never say who it is held for. The patient it is held for still meets the
    cleaning rule below, read from the bed's status under the hold; the
    reservation itself is fulfilled by locate_admission_on in the same
    transaction as the move.

    A bed whose next clean is terminal (out of service or not) needs a reason
    and an authoriser who ACTS: the sender, when their role and department
    allow, or the person named, when their signing PIN confirmed it
    (`terminal_clean_confirmed`, from confirm_terminal_authoriser). A name on
    its own is refused (terminal_clean_pin_required)."""
    from services.adt import reservations as adt_res

    held = adt_res.live_on_bed(db, bed["id"])
    if held is not None and not adt_res.is_for(
        held, admission_id=admission_id, patient_id=patient_id
    ):
        raise adt_res.refusal(ward=ward, bed=bed, reservation=held)
    ready = bed_readiness_on(db, ward=ward, bed=bed, reservations=False)
    status = ready["status"]
    if status == "reserved":
        # Not reachable with reservations=False; kept so a status the board
        # adds later can never let a held bed through.
        raise AdtConflict(
            f"{_bed_words(ward, bed)} is reserved for another patient. Choose another bed.",
            code="bed_reserved",
            bed_id=bed["id"],
        )
    if ready["terminal_clean"]:
        authoriser = _text(terminal_clean_authorised_by)
        reason = _text(terminal_clean_reason, _MAX_REASON)
        if not authoriser or not reason:
            raise AdtConflict(
                f"{_bed_words(ward, bed)} is waiting for a terminal clean (an isolation room, or "
                "an isolation patient left it). It can be used before that clean only if a "
                "doctor, nurse or administrator authorises it, with the reason: you, if your "
                "role allows, or someone who confirms it here with their own signing PIN. Both "
                "are recorded.",
                code="terminal_clean_authorisation_required",
                bed_id=bed["id"],
                bed_status=status,
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
            "bed_status": status,
            "terminal_clean_override": {
                "authorised_by": person["id"],
                "authorised_by_name": person.get("name"),
                "authoriser_role": person.get("role"),
                "authoriser_department": person.get("department") or None,
                "confirmed_by": confirmed_by,
                "reason": reason,
            },
        }
    if status not in _NOT_CLEAN_STATUSES:
        return {}
    words = (ready["status_words"] or status.replace("_", " ")).lower()
    reason = _text(not_clean_reason, _MAX_REASON)
    if not reason:
        raise AdtConflict(
            f"{_bed_words(ward, bed)} has not been recorded clean ({words}). Type the reason "
            "the patient is placed there before it is cleaned; it is recorded.",
            code="bed_not_clean_reason_required",
            bed_id=bed["id"],
            bed_status=status,
        )
    return {"bed_status": status, "not_clean_reason": reason}


def _extra_label_on(db, ward_id: str, beside_label: Any) -> str:
    """The first free label beside `beside_label` on the ward: 12A, then 12B.
    Retired beds keep their labels, so they count as taken."""
    base = _text(beside_label, 38) or "Extra"
    taken = {
        str(r[0]).strip().casefold()
        for r in db.execute(
            "SELECT bed_label FROM housekeeping_units WHERE ward_id = ? AND bed_label IS NOT NULL",
            (ward_id,),
        ).fetchall()
    }
    for letter in _EXTRA_BED_SUFFIXES:
        label = f"{base}{letter}"
        if label.casefold() not in taken:
            return label
    raise AdtConflict(
        f"Every extra bed label beside {base} is taken on this ward ({base}A to {base}Z). "
        "Retire the extra beds no longer in use in Bed setup.",
        code="extra_bed_labels_exhausted",
    )


def add_extra_bed_on(
    db, *, beside_bed_id: str, actor: dict | None, request_meta: dict | None = None
) -> dict:
    """Register a temporary bed beside an occupied one, on the caller's
    connection (ruling ADT-S1 (a): 'add an extra bed', so every patient keeps
    a bed label of their own and the extra bed is counted honestly).

    The new bed is a master bed like any other (housekeeping_service.
    register_unit_on with the ward id), marked temporary, labelled after the
    bed beside it (12A), in the same room and bed class, and registered as
    occupied because a patient is placed in it in the same transaction. A
    bed_master_events row and a 'facilities' audit line record it. Refused
    when the bed beside it is free (choose that bed instead)."""
    from services import housekeeping_service as hk

    beside = _row(
        db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ? AND ward_id IS NOT NULL",
            (beside_bed_id,),
        ).fetchone()
    )
    if beside is None:
        raise AdtNotFound("No bed with that id is set up in Bed setup", code="bed_not_found")
    ward = _row(db.execute("SELECT * FROM wards WHERE id = ?", (beside["ward_id"],)).fetchone())
    if ward is None or not ward.get("is_active"):
        raise AdtConflict("That ward is switched off in Bed setup", code="ward_inactive")
    if beside.get("retired_at") or not beside.get("is_active"):
        raise AdtConflict(
            f"{_bed_words(ward, beside)} is not in use. Choose a bed in use to add "
            "an extra bed beside.",
            code="bed_inactive",
            bed_id=beside["id"],
        )
    if _holder_on(db, admission_id=None, ward=ward, bed=beside) is None:
        raise AdtConflict(
            f"{_bed_words(ward, beside)} is free. Choose it rather than adding an extra bed.",
            code="extra_bed_not_needed",
            bed_id=beside["id"],
        )
    label = _extra_label_on(db, ward["id"], beside.get("bed_label"))
    try:
        unit = hk.register_unit_on(
            db,
            ward_id=ward["id"],
            room_id=beside.get("room_id"),
            bed_label=label,
            bed_class=beside.get("bed_class"),
            is_isolation_room=bool(beside.get("is_isolation_room")),
            initial_state="occupied",
            notes=f"Extra bed beside {beside.get('bed_label')}",
            actor_user_id=_actor_id(actor),
            actor_role=(actor or {}).get("role"),
            create_only=True,
            is_temporary=True,
        )
    except hk.HousekeepingError as exc:
        raise AdtConflict(
            f"The extra bed could not be added: {exc}", code="extra_bed_refused"
        ) from exc
    except DBIntegrityError as exc:
        raise AdtConflict(
            "The extra bed's label was just taken. Try again.", code="extra_bed_refused"
        ) from exc
    detail = {
        "unit_code": unit["unit_code"],
        "label": label,
        "ward_id": ward["id"],
        "ward_code": ward["code"],
        "temporary": True,
        "beside_bed_id": beside["id"],
        "beside_unit_code": beside.get("unit_code"),
        "room_id": unit.get("room_id"),
        "bed_class": unit.get("bed_class"),
    }
    db.execute(
        """INSERT INTO bed_master_events
               (entity_type, entity_id, action, detail_json, reason, actor_user_id, occurred_at)
           VALUES ('bed', ?, 'extra_bed_added', ?, ?, ?, ?)""",
        (
            unit["id"],
            json.dumps(detail, sort_keys=True, default=str),
            f"Extra bed added because {beside.get('unit_code')} holds a patient",
            _actor_id(actor),
            utc_iso_now(),
        ),
    )
    audit_on(
        db,
        category="facilities",
        action="bed_master_extra_bed_added",
        summary=f"Extra bed {unit['unit_code']} added beside {beside.get('unit_code')}",
        actor=actor,
        target_type="bed",
        target_id=unit["id"],
        request_meta=request_meta,
        metadata=detail,
    )
    return unit


def prepare_place_on(
    db: Any,
    *,
    admission_id: str | None,
    actor: dict | None,
    movement_type: str,
    ward_id: Any = None,
    bed_id: Any = None,
    ward_text: Any = None,
    bed_text: Any = None,
    extra_bed: bool = False,
    out_of_service_reason: str | None = None,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    request_meta: dict | None = None,
    treating_department: str | None = None,
    terminal_clean_confirmed: dict | None = None,
    patient_id: str | None = None,
) -> dict:
    """Apply the placement rules for a patient about to be put somewhere, on
    the caller's immediate session, BEFORE anything about the patient is
    written: resolve the place, refuse an occupied bed (ADT-S1 (a)), whether
    it is a master bed or a typed place not in bed setup, and, for a movement
    that puts the patient INTO a bed (a correction naming a bed other than the
    one recorded counts), a reserved bed and a bed not recorded clean without
    its reason or authorisation (ADT-S2 (c)).

    With `extra_bed`, `bed_id` is the OCCUPIED bed the extra bed goes beside:
    the temporary bed is registered first (add_extra_bed_on) and becomes the
    target; it needs no cleaning reason, as it was registered for this patient.

    `treating_department` is the patient's department (read from the
    admission when it is written already); the terminal-clean authoriser must
    work in it or in the ward's. `terminal_clean_confirmed` is what
    confirm_terminal_authoriser returned before the session opened.

    Returns {target, detail, out_of_service_reason} for place_admission_on.
    `admission_id` is None while the admission is not yet written (the
    admission door), so no admission is excluded from the occupancy check.

    `patient_id` (adt-12) is the patient being placed, read from the
    admission when it is written already. A door that admits a patient
    already on file (an admission written after this check) passes it, so a
    bed reserved for that patient is not refused here; with neither an
    admission nor a patient a reserved bed is always refused."""
    detail: dict[str, Any] = {}
    if admission_id and (treating_department is None or patient_id is None):
        own = db.execute(
            "SELECT department, patient_id FROM admissions WHERE id = ?", (admission_id,)
        ).fetchone()
        if own is not None:
            if treating_department is None:
                treating_department = own["department"]
            if patient_id is None:
                patient_id = own["patient_id"]
    if extra_bed:
        beside = _text(bed_id)
        if not beside:
            raise AdtError(
                "Choose the occupied bed the extra bed goes beside.", code="extra_bed_needs_bed"
            )
        unit = add_extra_bed_on(db, beside_bed_id=beside, actor=actor, request_meta=request_meta)
        detail["extra_bed"] = {
            "bed_id": unit["id"],
            "label": unit["bed_label"],
            "unit_code": unit["unit_code"],
            "beside_bed_id": beside,
        }
        ward_id, bed_id = unit["ward_id"], unit["id"]
    target = resolve_place_on(
        db,
        ward_id=ward_id,
        bed_id=bed_id,
        ward_text=ward_text,
        bed_text=bed_text,
        out_of_service_reason=out_of_service_reason,
    )
    ward, bed = target["ward"], target["bed"]
    if strict_locations_on():
        # adt-16: a place not in bed setup, refused before anything about the
        # patient is written (the admission door writes its rows after this).
        unlisted = strict_place_refusal(target, recorded=_recorded_place_on(db, admission_id))
        if unlisted is not None:
            raise unlisted
    # multisite-8h1: a ward or treating department at another facility is
    # refused here, before the cleaning and occupancy rules read that ward.
    crossing = cross_facility_refusal_on(
        db, admission_id=admission_id, ward=ward, to_department=treating_department
    )
    if crossing is not None:
        raise crossing
    if bed is not None:
        if _holder_on(db, admission_id=admission_id, ward=ward, bed=bed) is not None:
            raise AdtConflict(
                f"{_bed_words(ward, bed)} already holds a patient. Choose another bed, or "
                "add an extra bed with its own label.",
                code="bed_occupied",
                bed_id=bed["id"],
                extra_bed_offered=True,
            )
        # A correction into a different bed puts the patient INTO that bed
        # (round 2 review: it used to skip ADT-S2 (c), so a correction was a
        # way round the terminal-clean authoriser and the not-clean reason).
        placing = movement_type in _PLACING_TYPES or (
            movement_type == "correction"
            and not _recorded_in_on(db, admission_id=admission_id, ward=ward, bed=bed)
        )
        if placing and not extra_bed:
            detail.update(
                _placement_rules_on(
                    db,
                    ward=ward,
                    bed=bed,
                    actor=actor,
                    treating_department=treating_department,
                    not_clean_reason=not_clean_reason,
                    terminal_clean_authorised_by=terminal_clean_authorised_by,
                    terminal_clean_reason=terminal_clean_reason,
                    terminal_clean_confirmed=terminal_clean_confirmed,
                    admission_id=admission_id,
                    patient_id=patient_id,
                )
            )
    elif target["bed_text"]:
        # A place not in bed setup, kept unlinked (ruling F104). Another
        # admitted patient recorded at the same typed ward and bed is refused
        # as for a master bed (ADT-S1 (a)); with the module off the admission
        # form only warned (bed_in_use_warning), and with it on that warning
        # used to be dropped with nothing in its place (round 3 review).
        if (
            _typed_holder_on(
                db,
                admission_id=admission_id,
                ward_text=target["ward_text"],
                bed_text=target["bed_text"],
            )
            is not None
        ):
            label = target["bed_text"]
            raise AdtConflict(
                f"Bed {label} on {target['ward_text']} is already recorded for another admitted "
                "patient. This place is not in bed setup, so no extra bed can be added beside "
                f"it: give the bed a label of its own (for example {label}A), or ask a bed "
                "manager to set the ward up.",
                code="bed_occupied",
                extra_bed_offered=False,
                linked=False,
            )
    else:
        detail["awaiting_bed"] = True
    return {"target": target, "detail": detail, "out_of_service_reason": out_of_service_reason}


def _admission_without_place_on(
    db,
    *,
    admission_id: str,
    actor: dict | None,
    reason: str,
    moved_at: Any,
    idempotency_key: str | None,
    notes: str | None,
    request_meta: dict | None,
    detail: dict,
) -> dict:
    """The 'admission' movement and its patient_movement audit for an admission
    with no ward and no bed named (awaiting a bed, nowhere yet)."""
    adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
    if adm is None:
        raise AdtNotFound("No admission with that id", code="admission_not_found")
    actor_name = None
    if _actor_id(actor):
        name_row = db.execute("SELECT name FROM users WHERE id = ?", (_actor_id(actor),)).fetchone()
        actor_name = name_row["name"] if name_row else None
    to_location = {"department": adm.get("department")}
    movement = record_movement_on(
        db,
        patient_id=adm["patient_id"],
        admission_id=admission_id,
        movement_type="admission",
        reason=reason,
        moved_at=moved_at,
        moved_by=_actor_id(actor),
        moved_by_name=actor_name,
        to_location=to_location,
        notes=notes,
        idempotency_key=idempotency_key,
        detail=detail or None,
    )
    audit_on(
        db,
        category="patient_movement",
        action=MOVEMENT_AUDIT_ACTIONS["admission"],
        summary=f"Admission {admission_id} recorded with no ward or bed yet",
        actor=actor,
        patient_id=adm["patient_id"],
        target_type="admission",
        target_id=admission_id,
        request_meta=request_meta,
        metadata={
            "movement_id": movement["id"],
            "movement_type": "admission",
            "from": None,
            "to": to_location,
            **({"detail": detail} if detail else {}),
        },
    )
    return {
        "replayed": False,
        "admission_id": admission_id,
        "patient_id": adm["patient_id"],
        "ward_id": None,
        "bed_id": None,
        "ward": None,
        "bed": None,
        "linked": False,
        "boarder": False,
        "movement": movement,
    }


def place_admission_on(
    db,
    *,
    admission_id: str,
    actor: dict | None,
    movement_type: str,
    reason: str,
    prepared: dict,
    boarder_acknowledged: bool = False,
    expected_bed_id: Any = _UNSET,
    moved_at: Any = None,
    idempotency_key: str | None = None,
    notes: str | None = None,
    request_meta: dict | None = None,
    to_department: str | None = None,
) -> dict:
    """Write the place prepare_place_on checked, through the single location
    writer (locate_admission_on), with what the rules recorded in the
    movement's detail. An admission with no ward and no bed writes its
    'admission' movement and audit directly, as there is no place to write.
    The caller holds the immediate session the preparation ran in.
    `to_department` is a transfer's new treating department (adt-6a), passed
    through to locate_admission_on."""
    target = prepared["target"]
    if movement_type == "admission" and not any(
        (target["ward_id"], target["bed_id"], target["ward_text"], target["bed_text"])
    ):
        return _admission_without_place_on(
            db,
            admission_id=admission_id,
            actor=actor,
            reason=reason,
            moved_at=moved_at,
            idempotency_key=idempotency_key,
            notes=notes,
            request_meta=request_meta,
            detail=prepared["detail"],
        )
    return locate_admission_on(
        db,
        admission_id=admission_id,
        actor=actor,
        movement_type=movement_type,
        reason=reason,
        ward_id=target["ward_id"],
        bed_id=target["bed_id"],
        ward_text=target["ward_text"],
        bed_text=target["bed_text"],
        expected_bed_id=expected_bed_id,
        out_of_service_reason=prepared.get("out_of_service_reason"),
        boarder_acknowledged=boarder_acknowledged,
        moved_at=moved_at,
        idempotency_key=idempotency_key,
        notes=notes,
        request_meta=request_meta,
        extra_detail=prepared["detail"] or None,
        to_department=to_department,
    )


def assign_bed(
    admission_id: str,
    *,
    actor: dict,
    ward_id: Any = None,
    bed_id: Any = None,
    ward_text: Any = None,
    bed_text: Any = None,
    extra_bed: bool = False,
    correction: bool = False,
    reason: Any = None,
    expected_bed_id: Any = _UNSET,
    moved_at: Any = None,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_authoriser_pin: Any = None,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    """Give an active admission its bed, in ONE immediate session
    (POST /api/adt/admissions/{id}/bed).

    A bed whose next clean is terminal is authorised by the sender, when
    their role allows, or by the person named confirming with their signing
    PIN (`terminal_clean_authoriser_pin`, checked before the session opens by
    confirm_terminal_authoriser).

    For an admission with no bed recorded: a 'bed_assigned' movement, under
    the placement rules (ADT-S1 (a), ADT-S2 (c)). An admission that already
    has a bed recorded (typed text or a master bed) is REFUSED, so nobody can
    overwrite where a patient actually lies by assigning a second bed, unless
    the request is a correction: then it needs the bed the screen showed
    (expected_bed_id) and the reason the record was wrong, and writes a
    'correction' movement. A correction for an admission with no bed recorded
    is refused (409 no_bed_to_correct); one naming a different bed meets the
    placement rules in full, as an assignment does. Moving a patient is a bed
    change (adt-6a), not this.

    The Idempotency-Key header is required; a replay by the same person for
    the same admission returns the first movement and writes nothing. The
    treating department never changes here (ruling ADT-S8)."""
    from services import housekeeping_service as hk

    key = _text(idempotency_key)
    if not key:
        raise AdtError(
            "This change needs an Idempotency-Key header, so a retried request is "
            "never recorded twice.",
            code="idempotency_key_required",
        )
    if not (_text(bed_id) or _text(bed_text)):
        raise AdtError("Choose the bed.", code="bed_required")
    hk.ensure_housekeeping_schema()
    # Before the immediate session: a PIN check may open a session of its own,
    # and a refused PIN is audited in one so the refusal stays on record.
    confirmed = confirm_terminal_authoriser(
        actor=actor,
        authorised_by=terminal_clean_authorised_by,
        pin=terminal_clean_authoriser_pin,
        admission_id=admission_id,
        bed_id=bed_id,
        request_meta=request_meta,
    )
    with db_session(immediate=True) as db:
        prior = movement_for_key(db, key)
        if prior is not None:
            if (
                prior.get("admission_id") != admission_id
                or (prior.get("moved_by") or None) != (_actor_id(actor) or None)
                or prior.get("movement_type") not in ("bed_assigned", "correction")
            ):
                raise AdtConflict(
                    "That Idempotency-Key was already used for another change",
                    code="idempotency_key_reused",
                )
            return {
                "replayed": True,
                "movement": prior,
                "admission_id": admission_id,
                "patient_id": prior.get("patient_id"),
            }
        adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
        if adm is None:
            raise AdtNotFound("No admission with that id", code="admission_not_found")
        if adm.get("status") != "active":
            raise AdtConflict(
                "That admission has ended, so it has no bed to assign",
                code="admission_not_active",
                status=adm.get("status"),
            )
        when = check_event_time(
            moved_at,
            what="bed",
            not_before=parse_instant(adm.get("admitted_at") or adm.get("created_at")),
        )
        recorded = adm.get("bed_id") or _text(adm.get("bed"))
        if recorded and not correction:
            where = ", ".join(
                x
                for x in (
                    _text(adm.get("ward")),
                    f"bed {_text(adm.get('bed'))}" if _text(adm.get("bed")) else None,
                )
                if x
            )
            raise AdtConflict(
                f"This patient already has a bed recorded ({where or 'a bed in setup'}). "
                "Assigning a bed is for a patient awaiting one. To move the patient use a bed "
                "change; if the bed was recorded wrongly, send a correction with the reason.",
                code="bed_already_recorded",
                ward=adm.get("ward"),
                bed=adm.get("bed"),
                bed_id=adm.get("bed_id"),
            )
        if correction and not recorded:
            # Design section 5 allows a correction only when a bed is
            # recorded. With none there is nothing to correct, and treating
            # it as one skipped the rules a bed assignment applies.
            raise AdtConflict(
                "This patient has no bed recorded, so there is no bed to correct. Assign the "
                "bed instead.",
                code="no_bed_to_correct",
            )
        if correction:
            if expected_bed_id is _UNSET:
                raise AdtError(
                    "A correction must say which bed the screen showed "
                    "(expected_bed_id), so it never overwrites a change made since.",
                    code="expected_bed_required",
                )
            why = _text(reason, _MAX_REASON)
            if not why:
                raise AdtError(
                    "A correction needs the reason the recorded bed was wrong.",
                    code="reason_required",
                )
            movement_type, why = "correction", f"Bed corrected: {why}"
        else:
            movement_type, why = "bed_assigned", _text(reason, _MAX_REASON) or "Bed assigned"
        if not (_text(bed_id) or _text(ward_id) or _text(ward_text)):
            # Bed text alone names a bed on the ward the patient is already
            # on: its master id, else its typed text. Resolving it against no
            # ward would match nothing and write the ward away, leaving the
            # patient in an unlinked bed on no ward. With no ward recorded
            # either, a bed label names no place, so it is refused.
            if _text(adm.get("ward_id")):
                ward_id = adm["ward_id"]
            elif _text(adm.get("ward")):
                ward_text = adm["ward"]
            else:
                raise AdtError(
                    "Choose the ward as well. This patient has no ward recorded, "
                    "so a bed label on its own names no place.",
                    code="ward_required",
                )
        prepared = prepare_place_on(
            db,
            admission_id=admission_id,
            actor=actor,
            movement_type=movement_type,
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
            terminal_clean_confirmed=confirmed,
        )
        return place_admission_on(
            db,
            admission_id=admission_id,
            actor=actor,
            movement_type=movement_type,
            reason=why,
            prepared=prepared,
            boarder_acknowledged=boarder_acknowledged,
            expected_bed_id=expected_bed_id,
            moved_at=to_utc_iso(when),
            idempotency_key=key,
            request_meta=request_meta,
        )
