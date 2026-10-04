"""Orb: ends of stay with a type, a read-back and one disposition (adt-8a, W3-B).

WHAT AN END OF STAY WRITES. Before the beds and admissions module (ORB_ADT,
default off) a stay ended only as a status flip in discharge_patient: no
discharge type, no movement row, admissions.discharged_at left NULL, and a
death recorded as a discharge. With ORB_ADT on, every end writes, in ONE
transaction:

  * admissions.status 'discharged' (a roster close keeps the file's
    'transferred'), discharged_at, discharge_type and discharged_by;
  * patients.status when the patient has no other active admission, and the
    patient's open care bundles cancelled with an audit row each (F050: the
    same closure discharge_patient always did, now in close_admission_on);
  * a 'discharge' movement in patient_movements, carrying the end type and the
    discharge summary id when there is one;
  * the NABH disposition, de-duplicated (see _record_disposition_on): a row
    the records desk already keyed for this admission, or for this patient
    inside the stay window, is linked and kept, and a differing type is
    flagged for a person, never inserted twice. A death within 48 hours of
    the stay start is recorded as 'death_within_48h';
  * on a death, the patient's portal login is deactivated (ruling ADT-P8,
    portal login only; reopen restores it, adt-8b), and no discharge link is
    ever issued;
  * a clinical_access audit, 'death_recorded' or 'stay_ended'.

The bed is free at commit: occupancy is an ACTIVE admission holding bed_id
(idx_admissions_active_bed), and the row leaves the index when its status
leaves 'active'. bed_id itself is kept as the record of where the stay was.

READ-BACK (ADT-S3, strictest option). POST /api/adt/admissions/{id}/end must
echo the patient's MRN as confirm_mrn; a mismatch is 409 'read_back_mismatch'
and nothing is written. On the doctor's discharge route a DAMA needs the same
read-back. LAMA, DAMA and absconded also need a typed reason. Death, LAMA and
DAMA are recorded by a doctor only (discharge.generate plus scope).

discharge_preconditions(db, patient_id, end_type) is called by EVERY door
(the discharge route through discharge_patient, POST /end, the roster close).
It returns [] today. Held ruling R009 (what must be true before a discharge)
lands there and nowhere else; it adds no NEWS2 and no bundle gate (pinned by
an AST test). Open escalation chains are LISTED in the response and never
closed (R009 held).

Every *_on function takes the caller's connection and opens no session; the
public end_stay() opens the one immediate session for POST /end.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
from database import db_session
from db_backend import DBIntegrityError
from services.adt.core import (
    DISCHARGE_TYPES,
    AdtConflict,
    AdtError,
    AdtNotFound,
    _text,
    adt_on,
    audit_on,
    movement_for_key,
    parse_instant,
    record_movement_on,
)
from services.audit_service import log_audit_event
from services.clinical_time import format_utc

logger = logging.getLogger("orb.adt.end_stay")

__all__ = [
    "END_TYPES", "DISCHARGE_ROUTE_TYPES", "END_STAY_ONLY_TYPES", "READ_BACK_ON_DISCHARGE_ROUTE",
    "REASON_REQUIRED_TYPES", "DISCHARGE_BUNDLE_REASON", "DEATH_WITHIN_HOURS",
    "adt_on", "discharge_preconditions", "close_admission_on", "open_escalations_on",
    "check_read_back", "require_reason", "discharge_route_type", "record_end_on",
    "end_stay_on", "end_stay", "patient_for_admission",
]

#: Every end type the module records (migration 58's vocabulary).
END_TYPES: tuple = DISCHARGE_TYPES

#: What the doctor's discharge route (POST /api/vigil/patients/{id}/discharge)
#: accepts with ORB_ADT on. It drafts a summary; these are departures a summary
#: describes.
DISCHARGE_ROUTE_TYPES: tuple = ("routine", "referred_out", "transferred_out", "dama")

#: Ends recorded only through POST /api/adt/admissions/{id}/end, with the
#: typed MRN read-back: no summary is drafted for them.
END_STAY_ONLY_TYPES: tuple = ("lama", "absconded", "death")

#: The discharge route asks for the MRN read-back for these (ADT-S3: death,
#: LAMA and DAMA are doctor-only with read-back; LAMA and death cannot reach
#: that route at all).
READ_BACK_ON_DISCHARGE_ROUTE = frozenset({"dama"})

#: Ends that need a typed reason on every door (strictest reading of ADT-S3).
REASON_REQUIRED_TYPES = frozenset({"lama", "dama", "absconded"})

#: Why discharge_patient cancels a still-active care bundle. The exact string
#: the flag-off discharge always audited; discharge_service re-exports it.
DISCHARGE_BUNDLE_REASON = "patient discharged"

#: A death this many hours or fewer after the stay start is NABH's
#: 'death_within_48h'.
DEATH_WITHIN_HOURS = 48

#: The future an end time may sit in before it is refused (clock skew between
#: a ward tablet and the appliance).
_FUTURE_SLACK = timedelta(minutes=5)

TYPE_WORDS: dict[str, str] = {
    "routine": "routine discharge",
    "referred_out": "referred out",
    "transferred_out": "transferred out",
    "dama": "discharged against medical advice",
    "lama": "left against medical advice",
    "absconded": "absconded",
    "death": "death",
}


# ═════════════════════════════════════════════════════════════════════════════
#  The R009 hook
# ═════════════════════════════════════════════════════════════════════════════

def discharge_preconditions(db, patient_id: str, end_type: Optional[str]) -> list[dict]:
    """What must be true before this stay may end, as a list of problems.

    EMPTY TODAY, ON PURPOSE. Held ruling R009 decides what (if anything) must
    be done before a discharge; until Samuel rules, no door refuses an end of
    stay for a clinical reason. Every end-of-stay door calls this function
    and refuses when it returns anything, so R009 lands here, once, for every
    door and every end type. tests/test_adt_end_stay.py pins that each door
    calls it and that it reads no NEWS2 and no care bundle."""
    return []


# ═════════════════════════════════════════════════════════════════════════════
#  Reads
# ═════════════════════════════════════════════════════════════════════════════

def open_escalations_on(db, patient_id: str) -> list[dict]:
    """The patient's unresolved escalation chains. Listed at every end of
    stay so the person ending it sees them; never closed here (R009 held)."""
    rows = db.execute(
        """SELECT id, status, level, news2, peak_news2, current_tier, tier_label, raised_at
             FROM clinical_escalations
            WHERE patient_id = ? AND status != 'resolved'
            ORDER BY raised_at ASC""",
        (patient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def stay_start_of(admission: dict) -> Optional[datetime]:
    """When the stay began: admitted_at when ADT recorded it, else created_at."""
    return parse_instant(admission.get("admitted_at")) or parse_instant(admission.get("created_at"))


def patient_for_admission(admission_id: str) -> Optional[dict]:
    """{admission_id, patient_id, status, department} or None. For a route's
    scope check before it calls end_stay."""
    with db_session() as db:
        row = db.execute(
            "SELECT id AS admission_id, patient_id, status, department FROM admissions WHERE id = ?",
            (admission_id,),
        ).fetchone()
    return dict(row) if row is not None else None


# ═════════════════════════════════════════════════════════════════════════════
#  Checks
# ═════════════════════════════════════════════════════════════════════════════

def _mrn_key(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def check_read_back(patient: Optional[dict], confirm_mrn: Any) -> None:
    """The typed MRN must be this patient's. Compared without regard to
    spaces or capital letters; anything else is refused before any write."""
    if patient is None or not _mrn_key(confirm_mrn) or _mrn_key(confirm_mrn) != _mrn_key(patient.get("mrn")):
        raise AdtConflict(
            "The MRN typed does not match this patient's record. Check the wristband "
            "and type it again. Nothing was recorded.",
            code="read_back_mismatch")


def require_reason(end_type: Optional[str], reason: Any) -> Optional[str]:
    clean = _text(reason, 2000)
    if end_type in REASON_REQUIRED_TYPES and not clean:
        raise AdtError(f"Type the reason for {TYPE_WORDS[end_type]}. It is part of the record.",
                       code="reason_required", end_type=end_type)
    return clean


def discharge_route_type(value: Any) -> str:
    """The end type the doctor's discharge route may record (routine when
    none is given). LAMA, absconded and death are refused with the route that
    records them."""
    end_type = (_text(value) or "routine").lower()
    if end_type in END_STAY_ONLY_TYPES:
        raise AdtConflict(
            f"Record {TYPE_WORDS[end_type]} through End of stay, which asks for the MRN to "
            "be typed back and drafts no discharge summary.",
            code="use_end_of_stay", end_type=end_type)
    if end_type not in DISCHARGE_ROUTE_TYPES:
        words = ", ".join(TYPE_WORDS[t] for t in DISCHARGE_ROUTE_TYPES)
        raise AdtError(f"How the stay ended must be one of: {words}", code="unknown_end_type")
    return end_type


def _end_time(occurred_at: Any, stay_start: Optional[datetime], *,
              enforce_backtime: bool) -> datetime:
    """When the stay ended: now when not given. Refused in the future, before
    the stay began, or further back than ADT_BACKTIME_HOURS (ADT-P3; an older
    entry needs an administrator with a reason, which is not built, so it is
    refused)."""
    now = datetime.now(timezone.utc)
    if occurred_at in (None, ""):
        return now
    when = parse_instant(occurred_at)
    if when is None:
        raise AdtError("The time the stay ended could not be read", code="bad_time")
    if when > now + _FUTURE_SLACK:
        raise AdtError("The time the stay ended is in the future", code="future_time")
    if stay_start is not None and when < stay_start:
        raise AdtError("The time the stay ended is before the patient was admitted",
                       code="before_admission")
    hours = int(getattr(config, "ADT_BACKTIME_HOURS", 72))
    if enforce_backtime and now - when > timedelta(hours=hours):
        raise AdtError(f"An end of stay can be recorded up to {hours} hours back. Older entries "
                       "need an administrator.", code="beyond_backtime", backtime_hours=hours)
    return when


# ═════════════════════════════════════════════════════════════════════════════
#  The status flips and bundle closure (extracted from discharge_patient)
# ═════════════════════════════════════════════════════════════════════════════

def close_admission_on(db, patient_id: str, *, actor_user_id: Optional[str],
                       admission_id: Optional[str] = None,
                       admission_status: str = "discharged",
                       bundle_reason: str = DISCHARGE_BUNDLE_REASON) -> Optional[dict]:
    """Close a stay on the caller's connection. None when there was nothing to
    close.

    Without `admission_id` (discharge_patient, every flag): exactly the writes
    discharge_patient always made, in the same order. Claim the patient
    (patients.status 'admitted' to 'discharged'; None if they were not
    admitted, so a second discharge writes nothing), close every active
    admission, cancel every open care bundle with an audit row each.

    With `admission_id` (end of stay, ORB_ADT on): close that admission to
    `admission_status`; the patient row follows (to 'discharged', or
    'transferred' for a roster close) and their bundles close only when no
    other active admission remains."""
    patient_status = "transferred" if admission_status == "transferred" else "discharged"
    if admission_id is None:
        claim = db.execute(
            "UPDATE patients SET status = 'discharged' WHERE id = ? AND status = 'admitted'",
            (patient_id,),
        )
        if claim.rowcount == 0:
            return None
        # Close the encounter too. patients.status alone is not enough:
        # consumers that key off admissions.status = 'active' (admin
        # erase_patient's 409 guard, sage_handoff_service's transfer
        # resolution) otherwise treat every ever-admitted patient as a live
        # inpatient forever.
        db.execute(
            "UPDATE admissions SET status = 'discharged' WHERE patient_id = ? AND status = 'active'",
            (patient_id,),
        )
        patient_left = True
    else:
        closed = db.execute(
            "UPDATE admissions SET status = ? WHERE id = ? AND status = 'active'",
            (admission_status, admission_id),
        )
        if closed.rowcount == 0:
            return None
        remaining = db.execute(
            "SELECT COUNT(*) FROM admissions WHERE patient_id = ? AND status = 'active'",
            (patient_id,),
        ).fetchone()[0]
        patient_left = not remaining
        if patient_left:
            db.execute(
                "UPDATE patients SET status = ? WHERE id = ? AND status = 'admitted'",
                (patient_status, patient_id),
            )

    cancelled: list[dict] = []
    if patient_left:
        # Close the patient's open care bundles in the same transaction. A
        # Sepsis Six left 'active' after discharge kept ticking on the Command
        # Center and counted against compliance forever (F050). Each
        # cancellation is audited with the reason, as the bundle route's own
        # cancel is.
        open_bundles = db.execute(
            "SELECT id, bundle_type FROM care_bundles WHERE patient_id = ? AND status = 'active'",
            (patient_id,),
        ).fetchall()
        for bundle in open_bundles:
            done = db.execute(
                "UPDATE care_bundles SET status = 'cancelled' WHERE id = ? AND status = 'active'",
                (bundle["id"],),
            )
            if done.rowcount:
                log_audit_event(
                    correlation_id=f"bundle-cancel-{bundle['id']}", event_category="care_bundle",
                    action_type="bundle_cancelled", outcome="success", db=db,
                    summary_message=f"Bundle {bundle['id']} cancelled: {bundle_reason}",
                    actor_user_id=actor_user_id, patient_id=patient_id,
                    target_type="care_bundle", target_id=str(bundle["id"]),
                    metadata={"reason": bundle_reason, "bundle_type": bundle["bundle_type"]},
                )
                cancelled.append({"id": bundle["id"], "bundle_type": bundle["bundle_type"]})
    return {"patient_left": patient_left, "bundles_cancelled": cancelled}


# ═════════════════════════════════════════════════════════════════════════════
#  The NABH disposition, de-duplicated
# ═════════════════════════════════════════════════════════════════════════════

def _nabh_iso(when: datetime) -> str:
    """The spelling nabh_service writes dispositions in (seconds, UTC
    offset), so an automatic row and a desk row for one instant collide on
    the unique (patient_id, occurred_at) index instead of doubling."""
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _disposition_for(end_type: str, occurred: datetime, stay_start: Optional[datetime]) -> str:
    if (end_type == "death" and stay_start is not None
            and occurred - stay_start <= timedelta(hours=DEATH_WITHIN_HOURS)):
        return "death_within_48h"
    return end_type


def _record_disposition_on(db, *, patient_id: str, admission_id: str, disposition: str,
                           occurred: datetime, stay_start: Optional[datetime],
                           actor_user_id: Optional[str], notes: Optional[str]) -> dict:
    """Find the disposition for this stay, or write one. Never two.

    1. A live row the records desk keyed with this admission_id.
    2. Else a live row for this patient with no admission, inside
       [stay start, end + 24 h], nearest the end: linked to this admission.
    3. Else a live row for this patient at this very instant.
    Found: kept as the desk keyed it; when its type differs from the end type
    it is flagged needs_reconciliation (the desk's value stays, a person
    decides). Not found: a new row with source 'adt'."""
    end_iso = _nabh_iso(occurred)
    now_iso = _nabh_iso(datetime.now(timezone.utc))
    found = db.execute(
        """SELECT * FROM nabh_discharge_dispositions
            WHERE admission_id = ? AND voided_at IS NULL ORDER BY id DESC LIMIT 1""",
        (admission_id,),
    ).fetchone()
    action = "matched_admission"
    if found is None:
        lower = _nabh_iso(stay_start) if stay_start is not None else _nabh_iso(occurred - timedelta(hours=24))
        upper = _nabh_iso(occurred + timedelta(hours=24))
        found = db.execute(
            """SELECT * FROM nabh_discharge_dispositions
                WHERE patient_id = ? AND admission_id IS NULL AND voided_at IS NULL
                  AND julianday(occurred_at) >= julianday(?)
                  AND julianday(occurred_at) <= julianday(?)
                ORDER BY ABS(julianday(occurred_at) - julianday(?)) ASC, id DESC LIMIT 1""",
            (patient_id, lower, upper, end_iso),
        ).fetchone()
        action = "linked_stay_window"
    if found is None:
        found = db.execute(
            """SELECT * FROM nabh_discharge_dispositions
                WHERE patient_id = ? AND occurred_at = ? AND voided_at IS NULL""",
            (patient_id, end_iso),
        ).fetchone()
        action = "matched_instant"

    if found is not None:
        row = dict(found)
        differs = row["disposition"] != disposition
        db.execute(
            """UPDATE nabh_discharge_dispositions
                  SET admission_id = COALESCE(admission_id, ?),
                      needs_reconciliation = CASE WHEN ? THEN 1 ELSE needs_reconciliation END,
                      updated_at = ?
                WHERE id = ?""",
            (admission_id, 1 if differs else 0, now_iso, row["id"]),
        )
        return {
            "id": row["id"],
            "disposition": row["disposition"],
            "end_of_stay_disposition": disposition,
            "source": row.get("source") or "admin",
            "action": action + ("_flagged" if differs else ""),
            "needs_reconciliation": bool(differs or row.get("needs_reconciliation")),
        }

    # A voided row may still hold this exact instant (unique per patient and
    # instant, voided or not). Step past it by whole seconds rather than fail.
    when = occurred
    for _ in range(5):
        taken = db.execute(
            "SELECT 1 FROM nabh_discharge_dispositions WHERE patient_id = ? AND occurred_at = ?",
            (patient_id, _nabh_iso(when)),
        ).fetchone()
        if taken is None:
            break
        when = when + timedelta(seconds=1)
    cur = db.execute(
        """INSERT INTO nabh_discharge_dispositions
               (patient_id, admission_id, disposition, occurred_at, recorded_by, notes,
                created_at, updated_at, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'adt')""",
        (patient_id, admission_id, disposition, _nabh_iso(when), actor_user_id, notes,
         now_iso, now_iso),
    )
    return {"id": cur.lastrowid, "disposition": disposition, "end_of_stay_disposition": disposition,
            "source": "adt", "action": "inserted", "needs_reconciliation": False}


# ═════════════════════════════════════════════════════════════════════════════
#  The portal login on a death (ADT-P8, portal login only)
# ═════════════════════════════════════════════════════════════════════════════

def _deactivate_portal_on(db, patient_id: str) -> Optional[str]:
    """Deactivate the patient's portal login, if one is active. Returns its
    user id (recorded on the movement so a reopen restores exactly what the
    death changed), or None. Takes effect on the next request: get_current_user
    refuses a deactivated account on every call."""
    row = db.execute(
        """SELECT u.id FROM patients p JOIN users u ON u.id = p.user_id
            WHERE p.id = ? AND u.role = 'patient' AND COALESCE(u.is_active, 1) = 1""",
        (patient_id,),
    ).fetchone()
    if row is None:
        return None
    db.execute("UPDATE users SET is_active = 0 WHERE id = ? AND role = 'patient'", (row["id"],))
    return row["id"]


# ═════════════════════════════════════════════════════════════════════════════
#  The record of the end
# ═════════════════════════════════════════════════════════════════════════════

def record_end_on(
    db, *, admission: dict, patient: Optional[dict], end_type: Optional[str],
    occurred: datetime, actor: Optional[dict], reason: Optional[str] = None,
    notes: Optional[str] = None, summary_id: Optional[int] = None, door: str,
    idempotency_key: Optional[str] = None, request_meta: Optional[dict] = None,
    bundles_cancelled: Optional[list] = None, open_escalations: Optional[list] = None,
    admission_status: str = "discharged",
) -> dict:
    """Everything an end of stay records beyond the status flips, on the
    caller's connection, for an admission close_admission_on has just closed.
    `admission` is the row as it was BEFORE the close (its place is the
    movement's 'from'). `end_type` None is an end whose type nobody stated
    (a roster file saying only 'discharged'): no disposition is written,
    because a guessed 'routine' would understate mortality."""
    patient_id = admission["patient_id"]
    admission_id = admission["id"]
    actor_id = (actor or {}).get("id")
    occurred_iso = format_utc(occurred)
    stay_start = stay_start_of(admission)

    db.execute(
        "UPDATE admissions SET discharged_at = ?, discharge_type = ?, discharged_by = ? WHERE id = ?",
        (occurred_iso, end_type, actor_id, admission_id),
    )

    portal_user = _deactivate_portal_on(db, patient_id) if end_type == "death" else None

    disposition = None
    if end_type is not None:
        disposition = _record_disposition_on(
            db, patient_id=patient_id, admission_id=admission_id,
            disposition=_disposition_for(end_type, occurred, stay_start),
            occurred=occurred, stay_start=stay_start, actor_user_id=actor_id,
            notes=reason or notes)

    words = TYPE_WORDS.get(end_type or "", "type not stated")
    escalations = open_escalations or []
    bundles = bundles_cancelled or []
    detail = {
        "door": door,
        "admission_status": admission_status,
        "open_escalations": [e["id"] for e in escalations],
        "bundles_cancelled": [b["id"] for b in bundles],
    }
    if portal_user:
        detail["portal_login_deactivated"] = portal_user
    if disposition:
        detail["disposition_id"] = disposition["id"]
        detail["disposition_action"] = disposition["action"]
    if end_type is None:
        detail["end_type_not_stated"] = True
    actor_name = None
    if actor_id:
        name_row = db.execute("SELECT name FROM users WHERE id = ?", (actor_id,)).fetchone()
        actor_name = name_row["name"] if name_row else None
    movement = record_movement_on(
        db, patient_id=patient_id, admission_id=admission_id, movement_type="discharge",
        reason=f"Stay ended: {words}" + (f". {reason}" if reason else ""),
        moved_at=occurred_iso, moved_by=actor_id, moved_by_name=actor_name,
        from_location={"department": admission.get("department"), "ward": admission.get("ward"),
                       "bed": admission.get("bed"), "ward_id": admission.get("ward_id"),
                       "bed_id": admission.get("bed_id")},
        end_type=end_type, discharge_summary_id=summary_id, notes=notes,
        idempotency_key=idempotency_key, detail=detail,
    )

    metadata = {
        "end_type": end_type, "occurred_at": occurred_iso, "door": door,
        "movement_id": movement["id"], "admission_status": admission_status,
        "disposition": disposition, "open_escalations": detail["open_escalations"],
        "bundles_cancelled": detail["bundles_cancelled"],
        "portal_login_deactivated": bool(portal_user), "summary_id": summary_id,
    }
    audit_on(
        db, category="clinical_access",
        action="death_recorded" if end_type == "death" else "stay_ended",
        summary=(f"Death recorded for admission {admission_id}" if end_type == "death"
                 else f"Stay {admission_id} ended: {words}"),
        actor=actor, patient_id=patient_id, target_type="admission", target_id=admission_id,
        request_meta=request_meta, metadata=metadata,
    )
    if disposition and disposition["needs_reconciliation"]:
        audit_on(
            db, category="clinical_access", action="disposition_reconcile_flagged",
            summary=(f"Disposition {disposition['id']} for admission {admission_id} reads "
                     f"{disposition['disposition']}; the end of stay recorded "
                     f"{disposition['end_of_stay_disposition']}"),
            actor=actor, patient_id=patient_id, target_type="nabh_disposition",
            target_id=disposition["id"], request_meta=request_meta,
            metadata={"admission_id": admission_id, "kept": disposition["disposition"],
                      "end_of_stay": disposition["end_of_stay_disposition"]},
        )
    if portal_user:
        audit_on(
            db, category="security", action="patient_portal_login_deactivated",
            summary=f"Portal login {portal_user} deactivated: death recorded (ADT-P8)",
            actor=actor, patient_id=patient_id, target_type="user", target_id=portal_user,
            request_meta=request_meta, metadata={"admission_id": admission_id,
                                                 "movement_id": movement["id"]},
        )

    return {
        "status": "ended",
        "admission_id": admission_id,
        "patient_id": patient_id,
        "end_type": end_type,
        "admission_status": admission_status,
        "discharged_at": occurred_iso,
        "discharged_by": actor_id,
        "movement_id": movement["id"],
        "disposition": disposition,
        "open_escalations": escalations,
        "bundles_cancelled": bundles,
        "portal_login_deactivated": bool(portal_user),
        "bed_freed": admission.get("bed_id"),
        "preconditions": [],
    }


def end_stay_on(
    db, *, admission_id: str, end_type: Optional[str], actor: Optional[dict],
    confirm_mrn: Any = None, require_read_back: bool = True, occurred_at: Any = None,
    reason: Any = None, notes: Any = None, admission_status: str = "discharged",
    door: str = "end_of_stay", idempotency_key: Optional[str] = None,
    request_meta: Optional[dict] = None, enforce_backtime: bool = True,
) -> dict:
    """End one active admission on the caller's connection (the caller holds
    the outermost immediate session). Every check runs before the first write.

    `end_type` None is accepted only together with require_read_back False
    (the roster close of a file that says only 'discharged')."""
    adm = db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone()
    if adm is None:
        raise AdtNotFound("No admission with that id", code="admission_not_found")
    admission = dict(adm)
    if admission.get("status") != "active":
        raise AdtConflict("That stay has already ended", code="admission_not_active",
                          status=admission.get("status"))
    if end_type is None:
        if require_read_back:
            raise AdtError("Say how the stay ended", code="end_type_required")
    elif end_type not in END_TYPES:
        words = ", ".join(TYPE_WORDS[t] for t in END_TYPES)
        raise AdtError(f"How the stay ended must be one of: {words}", code="unknown_end_type")
    if admission_status not in ("discharged", "transferred"):
        raise AdtError("An ended admission is discharged or transferred", code="bad_status")
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (admission["patient_id"],)).fetchone()
    patient = dict(patient) if patient is not None else None
    if require_read_back:
        check_read_back(patient, confirm_mrn)
    clean_reason = require_reason(end_type, reason)
    occurred = _end_time(occurred_at, stay_start_of(admission), enforce_backtime=enforce_backtime)

    problems = discharge_preconditions(db, admission["patient_id"], end_type)
    if problems:
        raise AdtConflict("This stay cannot end yet", code="discharge_preconditions",
                          preconditions=problems)
    escalations = open_escalations_on(db, admission["patient_id"])
    closed = close_admission_on(
        db, admission["patient_id"], actor_user_id=(actor or {}).get("id"),
        admission_id=admission_id, admission_status=admission_status,
        bundle_reason=f"stay ended: {TYPE_WORDS.get(end_type or '', 'type not stated')}")
    if closed is None:
        raise AdtConflict("That stay has already ended", code="admission_not_active")
    result = record_end_on(
        db, admission=admission, patient=patient, end_type=end_type, occurred=occurred,
        actor=actor, reason=clean_reason, notes=_text(notes, 2000), door=door,
        idempotency_key=idempotency_key, request_meta=request_meta,
        bundles_cancelled=closed["bundles_cancelled"], open_escalations=escalations,
        admission_status=admission_status)
    result["discharge_link_issued"] = False
    result["replayed"] = False
    return result


def end_stay(admission_id: str, *, end_type: str, actor: dict, confirm_mrn: Any,
             occurred_at: Any = None, reason: Any = None, notes: Any = None,
             idempotency_key: Optional[str] = None, request_meta: Optional[dict] = None) -> dict:
    """POST /api/adt/admissions/{id}/end: one immediate session, the typed
    MRN read-back always required. A replayed Idempotency-Key returns what the
    first call recorded and writes nothing."""
    try:
        with db_session(immediate=True) as db:
            prior = movement_for_key(db, idempotency_key)
            if prior is not None:
                if prior.get("admission_id") != admission_id or prior.get("movement_type") != "discharge":
                    raise AdtConflict("That Idempotency-Key was already used for something else",
                                      code="idempotency_key_reused")
                return {"status": "ended", "replayed": True, "admission_id": admission_id,
                        "patient_id": prior.get("patient_id"), "end_type": prior.get("end_type"),
                        "discharged_at": prior.get("moved_at"), "movement_id": prior.get("id"),
                        "discharge_link_issued": False}
            return end_stay_on(
                db, admission_id=admission_id, end_type=end_type, actor=actor,
                confirm_mrn=confirm_mrn, require_read_back=True, occurred_at=occurred_at,
                reason=reason, notes=notes, door="end_of_stay",
                idempotency_key=idempotency_key, request_meta=request_meta)
    except DBIntegrityError as exc:
        raise AdtConflict("The end of stay clashed with a record written at the same moment. "
                          "Nothing was recorded; reload and try again.",
                          code="write_conflict") from exc

