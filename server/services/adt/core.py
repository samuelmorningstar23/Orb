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
with the id left NULL ('unlinked'). Strict refusal of unknown places is adt-16
(ORB_ADT_STRICT_LOCATIONS), not this module.

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
  * an expected bed that is no longer the admission's bed ('state_changed').

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
from datetime import datetime
from typing import Any, Optional

import config
from database import db_session
from db_backend import DBIntegrityError
from migrations_his.m0051_adt_movement_ledger import MOVEMENT_TYPES
from migrations_his.m0058_adt_admission_location import DISCHARGE_TYPES, PRESENCE_STATUSES
from services.audit_service import log_audit_event
from services.clinical_time import parse_to_utc, to_utc_iso, utc_iso_now

logger = logging.getLogger("orb.adt")

__all__ = [
    "AdtError", "AdtConflict", "AdtNotFound", "http_detail",
    "DISCHARGE_TYPES", "PRESENCE_STATUSES", "MOVEMENT_TYPES",
    "adt_on", "current_location", "current_admission_on", "record_movement_on",
    "match_location_on", "locate_admission_on", "audit_on", "parse_instant",
]

_MAX_REASON = 2000
_MAX_LABEL = 120

#: Sentinel for "the caller did not say which bed it expects".
_UNSET = object()


# ═════════════════════════════════════════════════════════════════════════════
#  Errors
# ═════════════════════════════════════════════════════════════════════════════

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


def _text(value: Any, limit: int = _MAX_LABEL) -> Optional[str]:
    """Spaces tidied (ends trimmed, inner runs collapsed); None when blank."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _same(a: Any, b: Any) -> bool:
    """Equal without regard to capital letters or surrounding spaces."""
    return (_text(a) or "").casefold() == (_text(b) or "").casefold()


def parse_instant(value: Any) -> Optional[datetime]:
    """Any stored spelling (ISO with or without an offset, SQLite's
    CURRENT_TIMESTAMP) as an aware UTC datetime; None when blank or unreadable.
    A stored timestamp with no offset is UTC, as every Orb writer stores it."""
    if value is None or value == "":
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


def _row(row: Any) -> Optional[dict]:
    return dict(row) if row is not None else None


def _actor_id(actor: Optional[dict]) -> Optional[str]:
    return (actor or {}).get("id")


# ═════════════════════════════════════════════════════════════════════════════
#  Audit, inside the caller's transaction
# ═════════════════════════════════════════════════════════════════════════════

def audit_on(db, *, category: str, action: str, summary: str, actor: Optional[dict],
             patient_id: Optional[str] = None, target_type: Optional[str] = None,
             target_id: Optional[str] = None, request_meta: Optional[dict] = None,
             metadata: Optional[dict] = None, correlation_id: Optional[str] = None) -> None:
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

def current_admission_on(db, patient_id: str) -> Optional[dict]:
    """The patient's active admission, the newest if (wrongly) there are two.
    Same order as frontdesk_service.transfer_patient."""
    return _row(db.execute(
        """SELECT * FROM admissions
            WHERE patient_id = ? AND status = 'active'
            ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (patient_id,),
    ).fetchone())


def current_location(patient_id: str, *, db: Any = None) -> Optional[dict]:
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
    def _work(conn) -> Optional[dict]:
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
            loc.get("ward_department"), loc.get("department"))
        loc["on_ward"] = loc.get("presence_status") is None
        return loc

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


# ═════════════════════════════════════════════════════════════════════════════
#  The ledger writer
# ═════════════════════════════════════════════════════════════════════════════

def _location_cols(loc: Optional[dict]) -> tuple:
    loc = loc or {}
    return (loc.get("department"), loc.get("ward"), loc.get("bed"),
            loc.get("ward_id"), loc.get("bed_id"))


def movement_for_key(db, idempotency_key: Optional[str]) -> Optional[dict]:
    """The movement already written under this Idempotency-Key, if any."""
    key = _text(idempotency_key)
    if not key:
        return None
    return _movement_out(db.execute(
        "SELECT * FROM patient_movements WHERE idempotency_key = ?", (key,)).fetchone())


def _movement_out(row: Any) -> Optional[dict]:
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
    db, *, patient_id: str, admission_id: Optional[str], movement_type: str, reason: str,
    moved_at: Any = None, moved_by: Optional[str] = None, moved_by_name: Optional[str] = None,
    from_location: Optional[dict] = None, to_location: Optional[dict] = None,
    end_type: Optional[str] = None, discharge_summary_id: Optional[int] = None,
    corrects_movement_id: Optional[str] = None, notes: Optional[str] = None,
    idempotency_key: Optional[str] = None, detail: Optional[dict] = None,
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
        raise AdtError("A movement needs a reason. A move with no reason is not a record.",
                       code="reason_required")
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
            movement_id, patient_id, admission_id, movement_type,
            f_dept, f_ward, f_bed, t_dept, t_ward, t_bed,
            clean_reason, when, moved_by, moved_by_name, _text(notes, _MAX_REASON),
            _text(idempotency_key), f_ward_id, f_bed_id, t_ward_id, t_bed_id, end_type,
            discharge_summary_id, corrects_movement_id, utc_iso_now(),
            json.dumps(detail, sort_keys=True, default=str) if detail else None,
        ),
    )
    return _movement_out(db.execute(
        "SELECT * FROM patient_movements WHERE id = ?", (movement_id,)).fetchone())


# ═════════════════════════════════════════════════════════════════════════════
#  Text and id resolution against the bed master
# ═════════════════════════════════════════════════════════════════════════════

def _ward_by_code(db, text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    return _row(db.execute(
        "SELECT * FROM wards WHERE LOWER(TRIM(code)) = LOWER(?)", (text,)).fetchone())


def _bed_by_label(db, ward_id: str, text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    return _row(db.execute(
        """SELECT * FROM housekeeping_units
            WHERE ward_id = ? AND LOWER(TRIM(bed_label)) = LOWER(?)""",
        (ward_id, text),
    ).fetchone())


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


def _bed_words(ward: Optional[dict], bed: dict) -> str:
    return f"Bed {bed.get('bed_label')} on {ward.get('code') if ward else bed.get('ward')}"


def _resolve_target(db, *, ward_id: Optional[str], bed_id: Optional[str],
                    ward_text: Any, bed_text: Any, out_of_service_reason: Optional[str]) -> dict:
    """The place to put the admission, checked against the master. Raises an
    AdtError with words for anything that must not be written."""
    ward: Optional[dict] = None
    bed: Optional[dict] = None
    w_text, b_text = _text(ward_text), _text(bed_text)
    if bed_id:
        bed = _row(db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ? AND ward_id IS NOT NULL",
            (bed_id,)).fetchone())
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
        raise AdtConflict(f"Ward {ward['code']} is switched off in Bed setup", code="ward_inactive",
                          ward_id=ward["id"])
    oos_reason = _text(out_of_service_reason, _MAX_REASON)
    if bed is not None:
        if bed.get("retired_at"):
            raise AdtConflict(f"{_bed_words(ward, bed)} is retired. Choose another bed.",
                              code="bed_retired", bed_id=bed["id"])
        if not bed.get("is_active"):
            raise AdtConflict(f"{_bed_words(ward, bed)} is switched off. Choose another bed.",
                              code="bed_inactive", bed_id=bed["id"])
        if bed.get("state") == "out_of_service" and not oos_reason:
            raise AdtConflict(
                f"{_bed_words(ward, bed)} is recorded out of service by housekeeping. "
                "Type the reason the patient is placed there anyway; it is recorded.",
                code="out_of_service_reason_required", bed_id=bed["id"])
    return {
        "ward": ward,
        "bed": bed,
        "ward_id": ward["id"] if ward else None,
        "bed_id": bed["id"] if bed else None,
        # The master's spelling when linked, else the typed text as typed
        # (spaces tidied).
        "ward_text": ward["code"] if ward else w_text,
        "bed_text": bed["bed_label"] if bed else b_text,
        "out_of_service_reason": oos_reason if (bed and bed.get("state") == "out_of_service") else None,
    }


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


def boarder_words(ward: dict, treating_department: Optional[str]) -> str:
    """What a boarder placement means for who can open the chart (ADT-S6 is
    not applied: scope follows the treating department)."""
    return (f"Nurses on {ward['code']} ({ward.get('department')}) will not be able to open "
            f"this chart. The {treating_department or 'treating'} team keeps it.")


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
    db, *, admission_id: str, actor: Optional[dict], movement_type: str, reason: str,
    ward_id: Optional[str] = None, bed_id: Optional[str] = None,
    ward_text: Any = None, bed_text: Any = None, expected_bed_id: Any = _UNSET,
    out_of_service_reason: Optional[str] = None, boarder_acknowledged: bool = False,
    moved_at: Any = None, idempotency_key: Optional[str] = None, notes: Optional[str] = None,
    request_meta: Optional[dict] = None,
) -> dict:
    """Put an active admission in a place, on the caller's connection.

    THE ONLY WRITER of admissions.ward_id, admissions.bed_id, admissions.ward,
    admissions.bed and (for the patient's current admission) patients.ward and
    patients.bed. Writes one movement of `movement_type` and one
    patient_movement audit in the same transaction; a boarder placement adds a
    clinical_access audit and a case-room line. Returns the new location, the
    movement and whether it was a boarder placement. A replayed
    Idempotency-Key returns the first result and writes nothing.

    The caller must hold db_session(immediate=True) as the outermost session,
    so the occupancy check and the write see the same state."""
    if movement_type not in _LOCATING_TYPES:
        raise AdtError(f"{movement_type!r} does not place a patient", code="unknown_movement_type")
    prior = movement_for_key(db, idempotency_key)
    if prior is not None:
        if prior.get("admission_id") != admission_id:
            raise AdtConflict("That Idempotency-Key was already used for another admission",
                              code="idempotency_key_reused")
        return {"replayed": True, "movement": prior, "admission_id": admission_id,
                "patient_id": prior.get("patient_id")}

    adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
    if adm is None:
        raise AdtNotFound("No admission with that id", code="admission_not_found")
    if adm.get("status") != "active":
        raise AdtConflict("That admission has ended, so it has no bed to change",
                          code="admission_not_active", status=adm.get("status"))
    if expected_bed_id is not _UNSET and (expected_bed_id or None) != (adm.get("bed_id") or None):
        raise AdtConflict(
            "Where this patient lies changed since the screen was loaded. Reload and try again.",
            code="state_changed", expected_bed_id=expected_bed_id, current_bed_id=adm.get("bed_id"))

    target = _resolve_target(db, ward_id=ward_id, bed_id=bed_id, ward_text=ward_text,
                             bed_text=bed_text, out_of_service_reason=out_of_service_reason)
    ward, bed = target["ward"], target["bed"]

    if bed is not None:
        holder = db.execute(
            "SELECT id FROM admissions WHERE bed_id = ? AND status = 'active' AND id <> ?",
            (bed["id"], admission_id),
        ).fetchone()
        if holder is not None:
            raise AdtConflict(
                f"{_bed_words(ward, bed)} already holds a patient. Choose another bed, or "
                "add an extra bed with its own label.",
                code="bed_occupied", bed_id=bed["id"])

    boarder = (ward is not None and ward.get("department") is not None
               and not _same(ward.get("department"), adm.get("department"))
               and ward["id"] != adm.get("ward_id"))
    if boarder and not boarder_acknowledged:
        raise AdtConflict(boarder_words(ward, adm.get("department")),
                          code="boarder_acknowledgement_required",
                          ward_department=ward.get("department"),
                          treating_department=adm.get("department"))

    before = {"department": adm.get("department"), "ward": adm.get("ward"), "bed": adm.get("bed"),
              "ward_id": adm.get("ward_id"), "bed_id": adm.get("bed_id")}
    after = {"department": adm.get("department"), "ward": target["ward_text"],
             "bed": target["bed_text"], "ward_id": target["ward_id"], "bed_id": target["bed_id"]}
    if all(before[k] == after[k] for k in ("ward", "bed", "ward_id", "bed_id")):
        raise AdtConflict("The admission is already recorded at that place", code="no_change")

    try:
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
            code="bed_occupied", bed_id=after["bed_id"]) from exc

    # The patients mirror holds the CURRENT admission's place only.
    current = current_admission_on(db, adm["patient_id"])
    if current is not None and current["id"] == admission_id:
        db.execute("UPDATE patients SET ward = ?, bed = ? WHERE id = ?",
                   (after["ward"], after["bed"], adm["patient_id"]))

    detail: dict[str, Any] = {}
    if target["out_of_service_reason"]:
        detail["out_of_service_reason"] = target["out_of_service_reason"]
    if boarder:
        detail["boarder"] = {"ward_department": ward.get("department"),
                             "treating_department": adm.get("department")}
    actor_name = None
    if _actor_id(actor):
        name_row = db.execute("SELECT name FROM users WHERE id = ?", (_actor_id(actor),)).fetchone()
        actor_name = name_row["name"] if name_row else None
    movement = record_movement_on(
        db, patient_id=adm["patient_id"], admission_id=admission_id,
        movement_type=movement_type, reason=reason, moved_at=moved_at,
        moved_by=_actor_id(actor), moved_by_name=actor_name,
        from_location=before, to_location=after, notes=notes,
        idempotency_key=idempotency_key, detail=detail or None,
    )
    audit_on(
        db, category="patient_movement", action=MOVEMENT_AUDIT_ACTIONS[movement_type],
        summary=(f"Admission {admission_id} placed at {after['ward'] or 'no ward'}"
                 f"/{after['bed'] or 'no bed'} ({movement_type.replace('_', ' ')})"),
        actor=actor, patient_id=adm["patient_id"], target_type="admission",
        target_id=admission_id, request_meta=request_meta,
        metadata={"movement_id": movement["id"], "movement_type": movement_type,
                  "from": before, "to": after, **({"detail": detail} if detail else {})},
    )
    if boarder:
        audit_on(
            db, category="clinical_access", action="boarder_placed",
            summary=(f"Admission {admission_id} placed on {ward['code']} "
                     f"({ward.get('department')}) while treated by {adm.get('department')}"),
            actor=actor, patient_id=adm["patient_id"], target_type="admission",
            target_id=admission_id, request_meta=request_meta,
            metadata={"movement_id": movement["id"], "ward_id": ward["id"],
                      "ward_department": ward.get("department"),
                      "treating_department": adm.get("department"),
                      "acknowledged": True},
        )
        _case_room_line_on(
            db, adm["patient_id"],
            f"Placed as a boarder on {ward['code']} ({ward.get('department')}), "
            f"bed {after['bed'] or 'not yet chosen'}. {boarder_words(ward, adm.get('department'))}")

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
    }
