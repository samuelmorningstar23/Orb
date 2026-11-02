"""Orb: the bed master (adt-1). Wards, rooms, beds and bed classes.

WHAT THIS IS. Until migration 53 a ward and a bed were whatever somebody typed:
free text on patients, admissions and the housekeeping register. The beds and
admissions module (ORB_ADT, default off) needs them as records, so this module
keeps four of them:

  wards         code (the label admissions.ward carries), display name, the
                department that owns it, ward type, default bed class, whether
                housekeeping tracks its cleaning, and the register and ledger
                columns later increments set (adt-3: register complete).
  ward_rooms    rooms and bays in a ward. An isolation room makes its beds
                isolation units, which is what drives a terminal clean.
  beds          NOT a table of their own. A bed is a housekeeping_units row with
                a ward_id, so cleaning state and the bed are one record.
                housekeeping_service writes it (register_unit_on,
                update_master_bed_on, adopt_unit_on); this module decides what
                to write and records why.
  bed_classes   general, private, ICU and so on, with the bed-day charge code
                billing will price by (adt-15). No seed.

THE RULES IT HOLDS
  * Ward codes are unique without regard to capital letters or surrounding
    spaces, across the whole hospital group (multi-site ruling MS-R4 (a)): a
    unique index refuses 'gm-1' beside 'GM-1', and this module says so in
    words first. Bed labels are unique inside a ward the same way, and a bed's
    unit code is '<ward code>/<label>', so it never collides across wards.
    Neither may contain '/'.
  * A ward code, a room code and a bed label are immutable once anything
    refers to them (beds, rooms, admissions, patients, movements, the cleaning
    record). Change the display name instead, or set up a new one. Since
    migration 58 (adt-3) the guards count admissions.ward_id and bed_id and
    the movement ledger's ward and bed ids as well as the text mirrors.
  * A bed is retired, never deleted, and never while an active admission
    holds it (by bed_id, or by typed ward and bed text that names it) or a
    reservation holds it. A retired bed keeps its label, so a recreated bed
    takes a new one.

RECONCILIATION (adt-3). Admissions written before ORB_ADT carry typed ward and
bed text and no ids. reconciliation_report sorts the active admissions into
linked, matched (typed text names exactly one master bed nobody else claims),
doubled (two or more admissions claim one bed), unlinked (text that names no
master ward or bed) and awaiting a bed. link_admissions links the matched ones
through services.adt.core.locate_admission_on, the single location writer, and
never a doubled bed. mark_register_complete stamps wards.ledger_complete_from,
from which the ward's ADT figures count, and is refused while the ward has an
unlinked admission or a doubled bed.
  * A ward's department must be one a doctor or nurse covers, or one the site
    configuration declares. A declared department nobody covers is saved and
    flagged 'Admissions refused until a doctor or nurse covers <department>'.
    Cover is resolved BEFORE the write session opens, because
    list_covering_departments opens a session of its own and an immediate
    session must be the outermost one.
  * Every write runs in ONE db_session(immediate=True): the master rows, the
    housekeeping rows, a bed_master_events row per change and the audit line
    commit together or not at all. Registering 24 beds with a clash at bed 13
    leaves zero new beds and zero events.
  * Registered beds start 'vacant_dirty' (housekeeping's rule: registering a
    bed is inventory, not a claim that somebody cleaned it).
  * After a write that can change a ward's bed count (a ward, beds, a bed
    class, the site import, a retirement, register complete) commits, the
    capacity screens re-read their counts (adt-14, _refresh_capacity): with
    Beds and admissions on, a register-complete ward's count comes from here.
  * Beds already typed into the housekeeping register on this ward with the
    same label are never duplicated. Registering one is refused with words
    unless the bed manager asks to adopt them, which links the existing row and
    keeps its cleaning state, ledger and open tasks.

THE BED BOARD (adt-4a). bed_board derives each bed's status from the admission
that holds it, housekeeping's record and the movement ledger, in the design's
precedence (occupancy and reservation above out of service; a bed vacated
after its last clean needs cleaning whatever housekeeping says; wards whose
cleaning is not tracked read 'Vacant, cleaning not tracked'). awaiting_bed
lists active admissions with no bed and those with typed bed text not linked
to the master, never merged. available_beds is the picker's read. The section
at the end of this file states the rules.

PATIENT DATA. The master edits read no patient: reference checks count
admissions and patients by ward and bed text and return counts only. Master
edits are audited as category 'facilities' (design section 9) inside the same
transaction, with a bed_master_events row beside each. The reconciliation is
the exception: it lists admission and patient ids against typed places (no
name, no MRN), so reading it writes a fail-closed phi_access audit naming the
patient ids, and each link writes a fail-closed patient_movement audit. The
bed board and the awaiting list name patients only inside the caller's
department scope, and a read that names anybody writes one fail-closed
phi_access audit with their ids; available_beds names nobody.

Every function is plain sync and opens its own sessions; routes call it from
plain def handlers.
"""

from __future__ import annotations

import json
import logging
import re
import string
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from database import db_session
from db_backend import DBIntegrityError
from migrations_his.m0053_adt_bed_master import FACILITY_CODE, ROOM_TYPES, WARD_TYPES

from services import housekeeping_service as hk
from services.audit_service import log_audit_event
from services.clinical_time import format_utc, parse_to_utc, utc_iso_now

logger = logging.getLogger("orb.bed_master")

__all__ = [
    "BedMasterError",
    "BedMasterConflict",
    "BedMasterNotFound",
    "MAX_BEDS_PER_REGISTRATION",
    "list_master",
    "save_ward",
    "save_room",
    "register_beds",
    "update_bed",
    "save_bed_class",
    "import_site_config_wards",
    "retire_bed",
    "reconciliation_report",
    "link_admissions",
    "mark_register_complete",
    "BOARD_STATUSES",
    "FREE_STATUSES",
    "OCCUPIED_STATUSES",
    "VACATING_MOVEMENT_TYPES",
    "bed_board",
    "available_beds",
    "awaiting_bed",
]

#: One registration call registers at most this many beds. A ward is rarely
#: more than 60; this stops a typo in a range (1 to 2400) from writing a
#: register nobody can retire until adt-3.
MAX_BEDS_PER_REGISTRATION = 200

_MAX_CODE = 40
_MAX_NAME = 120
_MAX_NOTE = 2000


class BedMasterError(ValueError):
    """A bed setup request that cannot be honoured. 422 unless a subclass says
    otherwise. `code` is a stable word the setup screen can react to."""

    http_status = 422

    def __init__(self, message: str, *, code: str = "bed_master_refused", **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail


class BedMasterConflict(BedMasterError):
    """The request is well formed but collides with what is on file. 409."""

    http_status = 409


class BedMasterNotFound(BedMasterError):
    """No such ward, room, bed or bed class. 404."""

    http_status = 404


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════

# SQLite's LOWER() folds ASCII only (no ICU), so the Python side of every
# case-blind comparison folds ASCII only too. Otherwise a pre-check and the
# unique index could disagree about whether two codes are the same.
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _key(value: str) -> str:
    return value.strip(" ").translate(_ASCII_LOWER)


def _natural_key(label: str) -> list:
    """Bed 2 before bed 10. re.split with a group alternates text and digits,
    so like is always compared with like (admission_service._natural_key)."""
    return [
        (0, int(t), "") if t.isdigit() else (1, 0, t.casefold()) for t in re.split(r"(\d+)", label)
    ]


def _norm_code(value: Any, what: str, *, limit: int = _MAX_CODE) -> str:
    """A ward code, room code, bed label or bed class code: spaces tidied,
    required, bounded, and free of '/' and control characters."""
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        raise BedMasterError(f"{what} is required", code="required", field=what)
    if len(text) > limit:
        raise BedMasterError(
            f"{what} is longer than {limit} characters", code="too_long", field=what
        )
    if "/" in text:
        raise BedMasterError(
            f"{what} cannot contain '/'. A bed is shown as ward/bed, so a '/' inside a "
            "code would make two different beds read the same.",
            code="slash_not_allowed",
            field=what,
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise BedMasterError(
            f"{what} contains a character that cannot be printed", code="bad_character", field=what
        )
    return text


def _clean(value: Any, limit: int = _MAX_NAME) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _one_of(value: Any, allowed: Iterable[str], what: str) -> str:
    text = str(value or "").strip().lower()
    allowed = tuple(allowed)
    if text not in allowed:
        words = ", ".join(a.replace("_", " ") for a in allowed)
        raise BedMasterError(f"{what} must be one of: {words}", code="not_allowed", field=what)
    return text


def _bool(row: dict, *names: str) -> dict:
    for name in names:
        if name in row and row[name] is not None:
            row[name] = bool(row[name])
    return row


def _table_columns(db, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _event(
    db,
    entity_type: str,
    entity_id: str,
    action: str,
    detail: dict | None,
    actor: dict,
    now: str,
    reason: str | None = None,
) -> None:
    db.execute(
        """INSERT INTO bed_master_events
               (entity_type, entity_id, action, detail_json, reason, actor_user_id, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            entity_type,
            str(entity_id),
            action,
            json.dumps(detail, sort_keys=True, default=str) if detail else None,
            _clean(reason, _MAX_NOTE),
            (actor or {}).get("id"),
            now,
        ),
    )


def _audit(
    db,
    actor: dict,
    action: str,
    summary: str,
    *,
    target_type: str,
    target_id: str | int | None,
    request_meta: dict | None,
    metadata: dict | None = None,
) -> None:
    """One audit line per master write, inside the write's own transaction.
    Category 'facilities' (design section 9); no patient identifier."""
    meta = request_meta or {}
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="facilities",
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type=target_type,
        target_id=str(target_id),
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata=metadata or None,
    )


def _refresh_capacity() -> None:
    """Re-read the capacity screens' bed counts after a master write (adt-14).

    With Beds and admissions on, a ward whose register is complete takes its
    bed count from this register (forecast_service.refresh_ward_capacity), so
    a bed registered, retired or reclassed, or a register marked complete,
    must reach Command Center, the Vigil occupancy tile, the forecast and
    housekeeping readiness without a restart. Called AFTER the write's session
    has committed, never inside it: the refresh opens a session of its own.
    Never raises: the write has already committed, and a capacity figure a
    moment stale is a smaller problem than a saved bed reported as failed."""
    try:
        import config

        if config.module_state("adt") != "on":
            return
        from services.forecast_service import refresh_ward_capacity

        refresh_ward_capacity()
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("capacity refresh after a bed setup write failed")


# ═════════════════════════════════════════════════════════════════════════════
#  Departments and cover
# ═════════════════════════════════════════════════════════════════════════════


def _department_lists(
    facilities: frozenset[int] | None = None,
) -> tuple[list[str], list[str]]:
    """(covering, declared). Covering: departments an active doctor or nurse
    account holds, the anti-orphaning list (frontdesk_service). Declared: the
    site configuration's departments. Each opens its own session or none, so
    this is called BEFORE any write session.

    `facilities` (multisite-8h1) is the actor's facility scope: with one,
    both lists keep only the departments of those facilities, so the setup
    screen never offers a department a save would refuse."""
    from services.frontdesk_service import list_covering_departments
    from services.site_config_service import current_site_config

    covering = list_covering_departments(facilities=facilities)
    declared: list[str] = []
    for dept in current_site_config().departments:
        name = " ".join(str(dept.get("name") or "").split())
        if name and name not in declared:
            declared.append(name)
    if facilities is not None:
        from services import facility_lanes, facility_service

        declared = [
            d
            for d in declared
            if not facility_lanes.outside(facility_service.department_facility(d), facilities)
        ]
    return covering, declared


# ── Facility scope (multisite-8h1, W12-F) ────────────────────────────────────
# A ward belongs to the facility of its department (ruling MS-R4 (a)), and
# wards.facility_id (migration 74) records it. With more than one facility on
# record: a ward is stamped with its department's facility when it is set up
# and when its department changes; a department no facility lists is refused;
# a ward never moves to another facility once anything refers to it (MS-R3
# (c)); and an account bound to a facility sets up, sees and counts only its
# own facility's wards, rooms and beds (another facility's ward, room or bed
# answers 404 exactly like one that does not exist). With one facility none
# of this reads anything and every ward stays at facility 1.

DEPARTMENT_NOT_LISTED_WORDS = (
    "That department is not on any facility's departments list, so Orb cannot tell which "
    "facility the ward belongs to. Nothing was saved. Add the department to its facility "
    "first."
)
DEPARTMENT_OTHER_FACILITY_WORDS = (
    "That department belongs to another facility of the group. Set up wards only for your "
    "own facility's departments. Nothing was saved."
)
WARD_FACILITY_FIXED_WORDS = (
    "This ward is already in use, and the department chosen belongs to another facility of "
    "the group. A ward in use never moves between facilities. Set up a new ward for that "
    "department instead. Nothing was changed."
)


def _actor_facilities(actor: dict | None) -> frozenset[int] | None:
    """The actor's facility scope, or None for no facility limit. BLOCKING
    on a cache miss: call it before any immediate session."""
    from services import facility_lanes

    return facility_lanes.scope_of(actor or {})


def _outside_scope(db: Any, ward_id: Any, facilities: frozenset[int] | None) -> bool:
    """Is the ward with this id at a facility outside `facilities`?"""
    if facilities is None:
        return False
    from services import facility_lanes

    return facility_lanes.outside(facility_lanes.ward_facility_on(db, ward_id), facilities)


def _ward_facility_for(
    db: Any, department: str, facilities: frozenset[int] | None
) -> tuple[int, str] | None:
    """(facility id, facility code) for a ward of `department`, read on this
    connection; None while one facility is on record (nothing is read and
    the ward stays at facility 1). Refused in words for a department no
    active master row lists, and for one at a facility outside the actor's
    scope."""
    from services import facility_lanes, facility_service

    if not facility_lanes.multi_facility(db):
        return None
    from services.frontdesk_service import department_facility_on

    at = department_facility_on(db, department)
    if at is None:
        raise BedMasterError(
            DEPARTMENT_NOT_LISTED_WORDS, code="department_not_listed", field="department"
        )
    if facility_lanes.outside(at, facilities):
        raise BedMasterError(
            DEPARTMENT_OTHER_FACILITY_WORDS, code="department_other_facility", field="department"
        )
    ident = facility_service.facility_identity(at, db=db) or {}
    return at, str(ident.get("code") or FACILITY_CODE)


def _resolve_department(name: Any, covering: list[str], declared: list[str]) -> str:
    """The department as the access boundary spells it. A covered spelling
    wins over a declared one, because permissions.department_scope compares
    users.department exactly."""
    wanted = " ".join(str(name or "").split()).casefold()
    if not wanted:
        raise BedMasterError("Department is required", code="required", field="department")
    for canonical in covering:
        if canonical.casefold() == wanted:
            return canonical
    for canonical in declared:
        if canonical.casefold() == wanted:
            return canonical
    raise BedMasterError(
        "Choose a department a doctor or nurse covers, or one the site configuration "
        "declares. A ward in any other department would hold patients nobody's "
        "board shows.",
        code="unknown_department",
        field="department",
    )


def _cover_note(department: str, covering: list[str]) -> str | None:
    if any(c.casefold() == (department or "").casefold() for c in covering):
        return None
    return f"Admissions refused until a doctor or nurse covers {department}."


def _ward_out(row: Any, covering: list[str]) -> dict:
    ward = dict(row)
    _bool(ward, "cleaning_tracked", "register_complete", "is_active")
    note = _cover_note(ward.get("department") or "", covering)
    ward["has_cover"] = note is None
    ward["cover_note"] = note
    return ward


# ═════════════════════════════════════════════════════════════════════════════
#  Reference checks (what makes a code immutable)
# ═════════════════════════════════════════════════════════════════════════════


def _count(db, sql: str, params: tuple) -> int:
    return int(db.execute(sql, params).fetchone()[0] or 0)


def _ward_references(db, ward: dict) -> dict[str, int]:
    """Everything that refers to this ward, as counts. Any of them makes the
    ward code immutable. The id columns later migrations add (admissions.
    ward_id, patient_movements to_ward_id and from_ward_id) are counted when
    they exist."""
    code, wid = ward["code"], ward["id"]
    refs = {
        "beds": _count(db, "SELECT COUNT(*) FROM housekeeping_units WHERE ward_id = ?", (wid,)),
        "rooms": _count(db, "SELECT COUNT(*) FROM ward_rooms WHERE ward_id = ?", (wid,)),
        "housekeeping": _count(
            db,
            "SELECT COUNT(*) FROM housekeeping_units WHERE ward_id IS NULL "
            "AND LOWER(TRIM(ward)) = LOWER(TRIM(?))",
            (code,),
        ),
        "admissions": _count(
            db,
            "SELECT COUNT(*) FROM admissions WHERE LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?))",
            (code,),
        ),
        "patients": _count(
            db,
            "SELECT COUNT(*) FROM patients WHERE LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?))",
            (code,),
        ),
    }
    if "ward_id" in _table_columns(db, "admissions"):
        refs["admissions"] += _count(
            db, "SELECT COUNT(*) FROM admissions WHERE ward_id = ?", (wid,)
        )
    movement_cols = _table_columns(db, "patient_movements")
    if movement_cols:
        refs["movements"] = _count(
            db,
            "SELECT COUNT(*) FROM patient_movements WHERE LOWER(TRIM(COALESCE(from_ward, ''))) "
            "= LOWER(TRIM(?)) OR LOWER(TRIM(COALESCE(to_ward, ''))) = LOWER(TRIM(?))",
            (code, code),
        )
        for col in ("from_ward_id", "to_ward_id"):
            if col in movement_cols:
                refs["movements"] += _count(
                    db, f"SELECT COUNT(*) FROM patient_movements WHERE {col} = ?", (wid,)
                )
    return {k: v for k, v in refs.items() if v}


def _bed_references(db, unit: dict, ward_code: str) -> dict[str, int]:
    """Everything that refers to this bed, as counts. Any of them makes the
    label immutable: its cleaning record beyond registration, its cleaning
    tasks, admissions, patients and movements that carry the ward and bed
    text, and the bed id columns later migrations add."""
    uid, label = unit["id"], unit["bed_label"] or ""
    refs = {
        "cleaning_record": max(
            0,
            _count(db, "SELECT COUNT(*) FROM housekeeping_unit_events WHERE unit_id = ?", (uid,))
            - 1,
        ),
        "cleaning_tasks": _count(
            db, "SELECT COUNT(*) FROM housekeeping_tasks WHERE unit_id = ?", (uid,)
        ),
        "admissions": _count(
            db,
            "SELECT COUNT(*) FROM admissions WHERE LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?)) "
            "AND LOWER(TRIM(COALESCE(bed, ''))) = LOWER(TRIM(?))",
            (ward_code, label),
        ),
        "patients": _count(
            db,
            "SELECT COUNT(*) FROM patients WHERE LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?)) "
            "AND LOWER(TRIM(COALESCE(bed, ''))) = LOWER(TRIM(?))",
            (ward_code, label),
        ),
    }
    if "bed_id" in _table_columns(db, "admissions"):
        refs["admissions"] += _count(db, "SELECT COUNT(*) FROM admissions WHERE bed_id = ?", (uid,))
    movement_cols = _table_columns(db, "patient_movements")
    if movement_cols:
        refs["movements"] = _count(
            db,
            "SELECT COUNT(*) FROM patient_movements WHERE "
            "(LOWER(TRIM(COALESCE(from_ward, ''))) = LOWER(TRIM(?)) "
            " AND LOWER(TRIM(COALESCE(from_bed, ''))) = LOWER(TRIM(?))) OR "
            "(LOWER(TRIM(COALESCE(to_ward, ''))) = LOWER(TRIM(?)) "
            " AND LOWER(TRIM(COALESCE(to_bed, ''))) = LOWER(TRIM(?)))",
            (ward_code, label, ward_code, label),
        )
        for col in ("from_bed_id", "to_bed_id"):
            if col in movement_cols:
                refs["movements"] += _count(
                    db, f"SELECT COUNT(*) FROM patient_movements WHERE {col} = ?", (uid,)
                )
    return {k: v for k, v in refs.items() if v}


_REFERENCE_WORDS = {
    "beds": "beds",
    "rooms": "rooms",
    "housekeeping": "housekeeping register entries",
    "admissions": "admissions",
    "patients": "patient records",
    "movements": "patient movements",
    "cleaning_record": "cleaning records",
    "cleaning_tasks": "cleaning requests",
}


def _references_words(refs: dict[str, int]) -> str:
    return ", ".join(f"{n} {_REFERENCE_WORDS.get(k, k)}" for k, n in refs.items())


# ═════════════════════════════════════════════════════════════════════════════
#  Lookups inside a session
# ═════════════════════════════════════════════════════════════════════════════


def _ward_row(db, ward_id: str) -> dict:
    row = db.execute("SELECT * FROM wards WHERE id = ?", (ward_id,)).fetchone()
    if row is None:
        raise BedMasterNotFound("No ward with that id is set up", code="ward_not_found")
    return dict(row)


def _room_row(db, room_id: str) -> dict:
    row = db.execute("SELECT * FROM ward_rooms WHERE id = ?", (room_id,)).fetchone()
    if row is None:
        raise BedMasterNotFound("No room with that id is set up", code="room_not_found")
    return dict(row)


def _bed_class_code(db, value: Any, *, require_active: bool = True) -> str | None:
    """The stored spelling of a bed class, or None for a blank value."""
    text = _clean(value, _MAX_CODE)
    if text is None:
        return None
    row = db.execute(
        "SELECT code, is_active FROM bed_classes WHERE LOWER(TRIM(code)) = LOWER(TRIM(?))", (text,)
    ).fetchone()
    if row is None:
        raise BedMasterError(
            f"There is no bed class {text}. Set it up under bed classes first.",
            code="bed_class_not_found",
            field="bed_class",
        )
    if require_active and not row["is_active"]:
        raise BedMasterError(
            f"Bed class {row['code']} is switched off", code="bed_class_inactive", field="bed_class"
        )
    return row["code"]


def _clash_ward_code(db, code: str, *, exclude_id: str | None = None) -> None:
    row = db.execute(
        "SELECT id, code FROM wards WHERE LOWER(TRIM(code)) = LOWER(TRIM(?))"
        + (" AND id <> ?" if exclude_id else ""),
        (code, exclude_id) if exclude_id else (code,),
    ).fetchone()
    if row is not None:
        raise BedMasterConflict(
            f"Ward {row['code']} is already set up. Ward codes are compared without regard "
            f"to capital letters, so {code} and {row['code']} would be the same ward.",
            code="duplicate_code",
            existing_id=row["id"],
        )


# ═════════════════════════════════════════════════════════════════════════════
#  Read
# ═════════════════════════════════════════════════════════════════════════════


def list_master(*, ward_id: str | None = None, actor: dict | None = None) -> dict:
    """The whole master for the Bed setup screen: wards (with cover), rooms,
    beds, bed classes, the department choices, and beds still only in the
    housekeeping register (candidates to adopt). No patient data.

    `actor` (multisite-8h1): with more than one facility on record, an
    account bound to a facility sees its own facility's wards, rooms, beds,
    departments and housekeeping beds only; bed classes are the group's."""
    hk.ensure_housekeeping_schema()
    facilities = _actor_facilities(actor) if actor is not None else None
    covering, declared = _department_lists(facilities)
    ward_filter = " WHERE id = ?" if ward_id else ""
    by_ward = " AND ward_id = ?" if ward_id else ""
    params: tuple = (ward_id,) if ward_id else ()
    with db_session() as db:
        wards = [
            _ward_out(r, covering)
            for r in db.execute(
                f"SELECT * FROM wards{ward_filter} ORDER BY LOWER(code)", params
            ).fetchall()
        ]
        rooms = [
            _bool(dict(r), "is_isolation", "is_active")
            for r in db.execute(
                f"SELECT * FROM ward_rooms WHERE 1 = 1{by_ward} ORDER BY ward_id, LOWER(code)",
                params,
            ).fetchall()
        ]
        beds = [
            _bool(dict(r), "is_isolation_room", "is_temporary", "is_active")
            for r in db.execute(
                f"""SELECT id, unit_code, ward_id, ward, room_id, room, bed_label, bed_class,
                       is_isolation_room, is_temporary, is_active, retired_at, state,
                       state_since, notes, created_at, updated_at
                  FROM housekeeping_units WHERE ward_id IS NOT NULL{by_ward}""",
                params,
            ).fetchall()
        ]
        beds.sort(key=lambda b: (b["ward_id"], _natural_key(b.get("bed_label") or "")))
        has_items = (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'service_items'"
            ).fetchone()
            is not None
        )
        listed_sql = (
            "(SELECT COUNT(*) FROM service_items s WHERE s.code = b.bed_day_service_code)"
            if has_items
            else "NULL"
        )
        bed_classes = [
            _bool(dict(r), "is_critical_care", "counts_in_census", "is_active")
            for r in db.execute(
                f"SELECT b.*, {listed_sql} AS bed_day_charge_listed FROM bed_classes b ORDER BY LOWER(b.code)"
            ).fetchall()
        ]
        legacy = [
            dict(r)
            for r in db.execute(
                """SELECT id, unit_code, ward, room, bed_label, state
                 FROM housekeeping_units
                WHERE ward_id IS NULL AND unit_type = 'bed' AND is_active = 1
                ORDER BY ward, unit_code"""
            ).fetchall()
        ]
        if facilities is not None:
            # multisite-8h1: this facility's setup only (housekeeping units
            # carry their own facility since migration 75).
            from services.facility_lanes import facility_marks, wards_in_scope

            keep = {w["id"] for w in wards_in_scope(wards, facilities)}
            wards = [w for w in wards if w["id"] in keep]
            rooms = [r for r in rooms if r["ward_id"] in keep]
            beds = [b for b in beds if b["ward_id"] in keep]
            marks, fids = facility_marks(facilities)
            at_home = {
                r[0]
                for r in db.execute(
                    f"SELECT id FROM housekeeping_units WHERE ward_id IS NULL "
                    f"AND facility_id IN ({marks})",
                    fids,
                ).fetchall()
            }
            legacy = [u for u in legacy if u["id"] in at_home]

    for bc in bed_classes:
        if not bc.get("bed_day_service_code"):
            bc["bed_day_charge_listed"] = None
        elif bc.get("bed_day_charge_listed") is not None:
            bc["bed_day_charge_listed"] = bool(bc["bed_day_charge_listed"])
    counts: dict[str, dict] = {}
    for bed in beds:
        c = counts.setdefault(bed["ward_id"], {"beds": 0, "retired_beds": 0})
        if bed.get("retired_at"):
            c["retired_beds"] += 1
        elif bed.get("is_active"):
            c["beds"] += 1
    room_counts: dict[str, int] = {}
    for room in rooms:
        room_counts[room["ward_id"]] = room_counts.get(room["ward_id"], 0) + 1
    for ward in wards:
        ward.update(counts.get(ward["id"], {"beds": 0, "retired_beds": 0}))
        ward["rooms"] = room_counts.get(ward["id"], 0)
    if ward_id:
        codes = {_key(w["code"]) for w in wards}
        legacy = [u for u in legacy if _key(u["ward"] or "") in codes]
    return {
        "facility_code": FACILITY_CODE,
        "wards": wards,
        "rooms": rooms,
        "beds": beds,
        "bed_classes": bed_classes,
        "housekeeping_beds_not_in_setup": legacy,
        "departments": {"covering": covering, "declared": declared},
        "ward_types": list(WARD_TYPES),
        "room_types": list(ROOM_TYPES),
        "max_beds_per_registration": MAX_BEDS_PER_REGISTRATION,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Wards
# ═════════════════════════════════════════════════════════════════════════════

_WARD_FIELDS = (
    "code",
    "name",
    "department",
    "ward_type",
    "default_bed_class",
    "cleaning_tracked",
    "is_active",
)


def save_ward(
    data: dict, *, actor: dict, ward_id: str | None = None, request_meta: dict | None = None
) -> dict:
    """Create a ward (ward_id None) or change the fields `data` carries.

    The department is resolved before the session (covering, else declared,
    else refused). A ward code is refused if another ward holds it in any
    capitalisation, and cannot change once anything refers to the ward. A ward
    with active beds cannot be switched off (retire its beds first, adt-3)."""
    data = dict(data or {})
    reason = data.pop("reason", None)
    hk.ensure_housekeeping_schema()
    facilities = _actor_facilities(actor)
    covering, declared = _department_lists(facilities)
    department = (
        _resolve_department(data["department"], covering, declared)
        if "department" in data
        else None
    )
    now = utc_iso_now()

    if ward_id is None:
        code = _norm_code(data.get("code"), "Ward code")
        name = _clean(data.get("name")) or code
        ward_type = _one_of(data.get("ward_type") or "general", WARD_TYPES, "Ward type")
        if department is None:
            raise BedMasterError("Department is required", code="required", field="department")
        tracked = data.get("cleaning_tracked")
        tracked = True if tracked is None else bool(tracked)
        try:
            with db_session(immediate=True) as db:
                _clash_ward_code(db, code)
                bed_class = _bed_class_code(db, data.get("default_bed_class"))
                placed = _ward_facility_for(db, department, facilities)
                new_id = _new_id("WRD")
                db.execute(
                    """INSERT INTO wards
                           (id, facility_code, code, name, department, ward_type,
                            default_bed_class, cleaning_tracked, is_active, created_by,
                            created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (
                        new_id,
                        FACILITY_CODE,
                        code,
                        name,
                        department,
                        ward_type,
                        bed_class,
                        1 if tracked else 0,
                        (actor or {}).get("id"),
                        now,
                        now,
                    ),
                )
                if placed is not None:
                    # multisite-8h1: the ward is at its department's facility.
                    db.execute(
                        "UPDATE wards SET facility_id = ?, facility_code = ? WHERE id = ?",
                        (placed[0], placed[1], new_id),
                    )
                detail: dict[str, Any] = {
                    "code": code,
                    "name": name,
                    "department": department,
                    "ward_type": ward_type,
                    "default_bed_class": bed_class,
                    "cleaning_tracked": tracked,
                }
                if placed is not None:
                    detail["facility_id"] = placed[0]
                _event(db, "ward", new_id, "created", detail, actor, now, reason)
                _audit(
                    db,
                    actor,
                    "bed_master_ward_created",
                    f"Ward {code} set up in {department}",
                    target_type="ward",
                    target_id=new_id,
                    request_meta=request_meta,
                    metadata=detail,
                )
                row = db.execute("SELECT * FROM wards WHERE id = ?", (new_id,)).fetchone()
        except DBIntegrityError as exc:
            raise BedMasterConflict(
                f"Ward {code} could not be saved because it clashes with "
                "one already set up. Nothing was changed.",
                code="duplicate_code",
            ) from exc
        _refresh_capacity()
        return _ward_out(row, covering)

    changes: dict[str, list] = {}
    try:
        with db_session(immediate=True) as db:
            ward = _ward_row(db, ward_id)
            if _outside_scope(db, ward_id, facilities):
                raise BedMasterNotFound("No ward with that id is set up", code="ward_not_found")
            new: dict[str, Any] = {}
            if department is not None and department != ward.get("department"):
                placed = _ward_facility_for(db, department, facilities)
                if placed is not None and placed[0] != int(ward.get("facility_id") or 1):
                    if _ward_references(db, ward):
                        raise BedMasterConflict(
                            WARD_FACILITY_FIXED_WORDS, code="ward_facility_fixed"
                        )
                    new["facility_id"] = placed[0]
                    new["facility_code"] = placed[1]
            if "code" in data and data["code"] is not None:
                code = _norm_code(data["code"], "Ward code")
                if code != ward["code"]:
                    refs = _ward_references(db, ward)
                    if refs:
                        raise BedMasterConflict(
                            f"The ward code {ward['code']} is already in use "
                            f"({_references_words(refs)}), so it cannot be changed. Change "
                            "the display name instead, or set up a new ward.",
                            code="code_in_use",
                            references=refs,
                        )
                    _clash_ward_code(db, code, exclude_id=ward_id)
                    new["code"] = code
            if "name" in data and data["name"] is not None:
                new["name"] = _clean(data["name"]) or ward["code"]
            if department is not None:
                new["department"] = department
            if "ward_type" in data and data["ward_type"] is not None:
                new["ward_type"] = _one_of(data["ward_type"], WARD_TYPES, "Ward type")
            if "default_bed_class" in data:
                new["default_bed_class"] = _bed_class_code(db, data["default_bed_class"])
            if "cleaning_tracked" in data and data["cleaning_tracked"] is not None:
                new["cleaning_tracked"] = 1 if data["cleaning_tracked"] else 0
            if "is_active" in data and data["is_active"] is not None:
                active = 1 if data["is_active"] else 0
                if not active and ward["is_active"]:
                    beds = _count(
                        db,
                        "SELECT COUNT(*) FROM housekeeping_units WHERE ward_id = ? "
                        "AND is_active = 1 AND retired_at IS NULL",
                        (ward_id,),
                    )
                    held = _count(
                        db,
                        "SELECT COUNT(*) FROM admissions WHERE status = 'active' "
                        "AND LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?))",
                        (ward["code"],),
                    )
                    if beds or held:
                        raise BedMasterConflict(
                            f"Ward {ward['code']} still has {beds} beds and {held} patients "
                            "admitted to it, so it cannot be switched off.",
                            code="ward_in_use",
                            beds=beds,
                            admitted=held,
                        )
                new["is_active"] = active
            changes = {k: [ward.get(k), v] for k, v in new.items() if ward.get(k) != v}
            if changes:
                cols = ", ".join(f"{k} = ?" for k in changes)
                db.execute(
                    f"UPDATE wards SET {cols}, updated_at = ? WHERE id = ?",
                    (*[v[1] for v in changes.values()], now, ward_id),
                )
                action = "updated"
                if set(changes) == {"is_active"}:
                    action = "switched_on" if changes["is_active"][1] else "switched_off"
                _event(db, "ward", ward_id, action, {"changes": changes}, actor, now, reason)
                _audit(
                    db,
                    actor,
                    "bed_master_ward_updated",
                    f"Ward {ward['code']} changed: {', '.join(sorted(changes))}",
                    target_type="ward",
                    target_id=ward_id,
                    request_meta=request_meta,
                    metadata={"changes": changes},
                )
            row = db.execute("SELECT * FROM wards WHERE id = ?", (ward_id,)).fetchone()
    except DBIntegrityError as exc:
        raise BedMasterConflict(
            "That change clashes with a ward already set up. Nothing was changed.",
            code="duplicate_code",
        ) from exc
    if changes:
        _refresh_capacity()
    return _ward_out(row, covering)


# ═════════════════════════════════════════════════════════════════════════════
#  Rooms
# ═════════════════════════════════════════════════════════════════════════════


def save_room(
    data: dict, *, actor: dict, room_id: str | None = None, request_meta: dict | None = None
) -> dict:
    """Create a room on a ward, or change one. A room code is unique inside its
    ward without regard to capital letters and immutable once a bed is in the
    room. Changing whether a room is an isolation room changes its beds too,
    because that is what makes housekeeping send a terminal clean. A change of
    room type to or from 'isolation' with no isolation flag in the request sets
    the flag as create would, and so reaches the beds."""
    data = dict(data or {})
    reason = data.pop("reason", None)
    hk.ensure_housekeeping_schema()
    facilities = _actor_facilities(actor)
    now = utc_iso_now()
    try:
        with db_session(immediate=True) as db:
            if room_id is None:
                ward = _ward_row(db, str(data.get("ward_id") or ""))
                if _outside_scope(db, ward["id"], facilities):
                    raise BedMasterNotFound("No ward with that id is set up", code="ward_not_found")
                if not ward["is_active"]:
                    raise BedMasterConflict(
                        f"Ward {ward['code']} is switched off", code="ward_inactive"
                    )
                code = _norm_code(data.get("code"), "Room code")
                room_type = _one_of(data.get("room_type") or "bay", ROOM_TYPES, "Room type")
                isolation = data.get("is_isolation")
                isolation = (room_type == "isolation") if isolation is None else bool(isolation)
                _clash_room_code(db, ward["id"], code)
                new_id = _new_id("RM")
                name = _clean(data.get("name")) or code
                db.execute(
                    """INSERT INTO ward_rooms
                           (id, ward_id, code, name, room_type, is_isolation, is_active,
                            created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (new_id, ward["id"], code, name, room_type, 1 if isolation else 0, now, now),
                )
                detail: dict[str, Any] = {
                    "ward_id": ward["id"],
                    "ward_code": ward["code"],
                    "code": code,
                    "name": name,
                    "room_type": room_type,
                    "is_isolation": isolation,
                }
                _event(db, "room", new_id, "created", detail, actor, now, reason)
                _audit(
                    db,
                    actor,
                    "bed_master_room_created",
                    f"Room {code} set up on {ward['code']}",
                    target_type="room",
                    target_id=new_id,
                    request_meta=request_meta,
                    metadata=detail,
                )
                room_id = new_id
            else:
                room = _room_row(db, room_id)
                if _outside_scope(db, room["ward_id"], facilities):
                    raise BedMasterNotFound("No room with that id is set up", code="room_not_found")
                if data.get("ward_id") not in (None, room["ward_id"]):
                    raise BedMasterError(
                        "A room cannot move to another ward. Set up a room on that ward instead.",
                        code="ward_fixed",
                        field="ward_id",
                    )
                new: dict[str, Any] = {}
                if data.get("code") is not None:
                    code = _norm_code(data["code"], "Room code")
                    if code != room["code"]:
                        beds = _count(
                            db,
                            "SELECT COUNT(*) FROM housekeeping_units WHERE room_id = ?",
                            (room_id,),
                        )
                        if beds:
                            raise BedMasterConflict(
                                f"Room {room['code']} has {beds} beds, so its code cannot be "
                                "changed. Change the display name instead.",
                                code="code_in_use",
                                references={"beds": beds},
                            )
                        _clash_room_code(db, room["ward_id"], code, exclude_id=room_id)
                        new["code"] = code
                if data.get("name") is not None:
                    new["name"] = _clean(data["name"]) or room["code"]
                if data.get("room_type") is not None:
                    new["room_type"] = _one_of(data["room_type"], ROOM_TYPES, "Room type")
                isolation_follows_type = False
                if data.get("is_isolation") is not None:
                    new["is_isolation"] = 1 if data["is_isolation"] else 0
                elif new.get("room_type", room["room_type"]) != room["room_type"]:
                    # A new room type with no isolation flag in the request sets
                    # the flag as create derives it, and the beds follow below:
                    # made an isolation room, it is one; no longer typed as an
                    # isolation room, it is not. Between two other types an
                    # isolation flag the bed manager chose is left as it is.
                    if new["room_type"] == "isolation":
                        new["is_isolation"] = 1
                        isolation_follows_type = True
                    elif room["room_type"] == "isolation":
                        new["is_isolation"] = 0
                        isolation_follows_type = True
                if data.get("is_active") is not None:
                    active = 1 if data["is_active"] else 0
                    if not active and room["is_active"]:
                        beds = _count(
                            db,
                            "SELECT COUNT(*) FROM housekeeping_units WHERE room_id = ? "
                            "AND is_active = 1 AND retired_at IS NULL",
                            (room_id,),
                        )
                        if beds:
                            raise BedMasterConflict(
                                f"Room {room['code']} still has {beds} beds, so it cannot be "
                                "switched off. Move or retire its beds first.",
                                code="room_in_use",
                                beds=beds,
                            )
                    new["is_active"] = active
                changes = {k: [room.get(k), v] for k, v in new.items() if room.get(k) != v}
                if changes:
                    cols = ", ".join(f"{k} = ?" for k in changes)
                    db.execute(
                        f"UPDATE ward_rooms SET {cols}, updated_at = ? WHERE id = ?",
                        (*[v[1] for v in changes.values()], now, room_id),
                    )
                    beds_changed: list[str] = []
                    if "is_isolation" in changes:
                        for unit in db.execute(
                            "SELECT id FROM housekeeping_units WHERE room_id = ? AND retired_at IS NULL",
                            (room_id,),
                        ).fetchall():
                            hk.update_master_bed_on(
                                db, unit["id"], is_isolation_room=bool(changes["is_isolation"][1])
                            )
                            beds_changed.append(unit["id"])
                    detail = {"changes": changes}
                    if isolation_follows_type and "is_isolation" in changes:
                        detail["isolation_follows_room_type"] = True
                    if beds_changed:
                        detail["beds_isolation_changed"] = beds_changed
                    _event(db, "room", room_id, "updated", detail, actor, now, reason)
                    _audit(
                        db,
                        actor,
                        "bed_master_room_updated",
                        f"Room {room['code']} changed: {', '.join(sorted(changes))}",
                        target_type="room",
                        target_id=room_id,
                        request_meta=request_meta,
                        metadata=detail,
                    )
            row = db.execute("SELECT * FROM ward_rooms WHERE id = ?", (room_id,)).fetchone()
    except DBIntegrityError as exc:
        raise BedMasterConflict(
            "That room clashes with one already set up on this ward. Nothing was changed.",
            code="duplicate_code",
        ) from exc
    except hk.HousekeepingError as exc:
        raise BedMasterConflict(
            f"{exc}. Nothing was changed.", code="housekeeping_refused"
        ) from exc
    return _bool(dict(row), "is_isolation", "is_active")


def _clash_room_code(db, ward_id: str, code: str, *, exclude_id: str | None = None) -> None:
    row = db.execute(
        "SELECT id, code FROM ward_rooms WHERE ward_id = ? AND LOWER(TRIM(code)) = LOWER(TRIM(?))"
        + (" AND id <> ?" if exclude_id else ""),
        (ward_id, code, exclude_id) if exclude_id else (ward_id, code),
    ).fetchone()
    if row is not None:
        raise BedMasterConflict(
            f"Room {row['code']} is already set up on this ward. Room codes are compared "
            "without regard to capital letters.",
            code="duplicate_code",
            existing_id=row["id"],
        )


# ═════════════════════════════════════════════════════════════════════════════
#  Beds
# ═════════════════════════════════════════════════════════════════════════════


def _expand_labels(data: dict) -> list[str]:
    """The labels to register: a list, or a numbered range with an optional
    prefix and zero padding (prefix '' from 1 to 24, padding 2: '01' to '24')."""
    labels = data.get("labels")
    rng = data.get("range")
    if labels and rng:
        raise BedMasterError(
            "Give either a list of bed labels or a range, not both", code="labels_or_range"
        )
    out: list[str] = []
    if rng:
        try:
            start, end = int(rng.get("start")), int(rng.get("end"))
            pad = int(rng.get("pad") or 0)
        except (TypeError, ValueError, AttributeError):
            raise BedMasterError(
                "A range needs a whole-number start and end", code="bad_range"
            ) from None
        prefix = " ".join(str(rng.get("prefix") or "").split())
        if start < 0 or end < start:
            raise BedMasterError(
                "A range must run from a start up to an end at or above it", code="bad_range"
            )
        if not 0 <= pad <= 4:
            raise BedMasterError("Padding is between 0 and 4 digits", code="bad_range")
        if end - start + 1 > MAX_BEDS_PER_REGISTRATION:
            raise BedMasterError(
                f"At most {MAX_BEDS_PER_REGISTRATION} beds can be registered at once",
                code="too_many_beds",
            )
        out = [f"{prefix}{str(n).zfill(pad)}" for n in range(start, end + 1)]
    elif labels:
        if not isinstance(labels, (list, tuple)):
            raise BedMasterError("Bed labels must be a list", code="bad_labels")
        out = [str(x) if x is not None else "" for x in labels]
    if not out:
        raise BedMasterError("Give at least one bed label", code="required", field="labels")
    if len(out) > MAX_BEDS_PER_REGISTRATION:
        raise BedMasterError(
            f"At most {MAX_BEDS_PER_REGISTRATION} beds can be registered at once",
            code="too_many_beds",
        )
    return [_norm_code(x, "Bed label") for x in out]


def _bed_out(row: Any) -> dict:
    return _bool(dict(row), "is_isolation_room", "is_temporary", "is_active")


def register_beds(data: dict, *, actor: dict, request_meta: dict | None = None) -> dict:
    """Register beds on one ward in ONE immediate session: every bed, its
    housekeeping registration event, a bed_master_events row each and one audit
    line commit together, or nothing is saved.

    Refused, with nothing saved: a label repeated in the request, a label the
    ward already has (any capitalisation, retired beds included), a unit code
    already on the housekeeping register as something else, and beds already
    typed into the housekeeping register on this ward with the same label
    unless `adopt_existing` is set, in which case those rows are linked to the
    master and keep their cleaning state, ledger and open tasks."""
    data = dict(data or {})
    reason = data.pop("reason", None)
    labels = _expand_labels(data)
    adopt = bool(data.get("adopt_existing"))
    hk.ensure_housekeeping_schema()
    now = utc_iso_now()

    seen: dict[str, str] = {}
    repeated: list[str] = []
    for label in labels:
        k = _key(label)
        if k in seen:
            repeated.append(label)
        seen.setdefault(k, label)
    if repeated:
        raise BedMasterConflict(
            f"These bed labels appear more than once: {', '.join(repeated)}. Nothing was saved.",
            code="labels_repeated",
            labels=repeated,
        )

    registered: list[dict] = []
    adopted: list[dict] = []
    facilities = _actor_facilities(actor)
    try:
        with db_session(immediate=True) as db:
            ward = _ward_row(db, str(data.get("ward_id") or ""))
            if _outside_scope(db, ward["id"], facilities):
                raise BedMasterNotFound("No ward with that id is set up", code="ward_not_found")
            if not ward["is_active"]:
                raise BedMasterConflict(
                    f"Ward {ward['code']} is switched off", code="ward_inactive"
                )
            room = None
            if data.get("room_id"):
                room = _room_row(db, str(data["room_id"]))
                if room["ward_id"] != ward["id"]:
                    raise BedMasterError(
                        f"That room is not on {ward['code']}", code="room_not_on_ward"
                    )
                if not room["is_active"]:
                    raise BedMasterConflict(
                        f"Room {room['code']} is switched off", code="room_inactive"
                    )
            bed_class = (
                _bed_class_code(db, data["bed_class"])
                if data.get("bed_class")
                else ward.get("default_bed_class")
            )
            isolation = data.get("is_isolation_room")
            # An adopted bed keeps its own isolation flag unless the request or
            # its room says otherwise.
            adopt_isolation = (
                bool(isolation)
                if isolation is not None
                else bool(room["is_isolation"])
                if room
                else None
            )
            if isolation is None:
                isolation = bool(room["is_isolation"]) if room else False

            keys = [_key(x) for x in labels]
            marks = ",".join("?" for _ in keys)
            taken = [
                r["bed_label"]
                for r in db.execute(
                    f"""SELECT bed_label FROM housekeeping_units
                     WHERE ward_id = ? AND LOWER(TRIM(bed_label)) IN ({marks})
                     ORDER BY bed_label""",
                    (ward["id"], *keys),
                ).fetchall()
            ]
            if taken:
                raise BedMasterConflict(
                    f"{ward['code']} already has beds {', '.join(taken)}. Nothing was saved.",
                    code="labels_taken",
                    labels=taken,
                )

            codes = [hk.master_unit_code(ward["code"], x) for x in labels]
            code_marks = ",".join("?" for _ in codes)
            legacy = [
                dict(r)
                for r in db.execute(
                    f"""SELECT id, unit_code, ward, bed_label, unit_type, state, is_active
                      FROM housekeeping_units
                     WHERE ward_id IS NULL
                       AND (unit_code IN ({code_marks})
                            OR (unit_type = 'bed'
                                AND LOWER(TRIM(ward)) = LOWER(TRIM(?))
                                AND LOWER(TRIM(COALESCE(bed_label, ''))) IN ({marks})))""",
                    (*codes, ward["code"], *keys),
                ).fetchall()
            ]
            # Which legacy row (if any) is this label's bed? Only a BED on THIS
            # ward with THIS label may be adopted (a switched-off one is
            # switched back on); a unit code that merely matches is a different
            # thing and is refused.
            adopt_for: dict[str, dict] = {}
            blocked: list[str] = []
            for unit in legacy:
                same_bed = (
                    unit["unit_type"] == "bed"
                    and _key(unit["ward"] or "") == _key(ward["code"])
                    and _key(unit["bed_label"] or "") in seen
                )
                if not same_bed:
                    blocked.append(unit["unit_code"])
                    continue
                k = _key(unit["bed_label"])
                if k in adopt_for:
                    blocked += [adopt_for[k]["unit_code"], unit["unit_code"]]
                    continue
                adopt_for[k] = unit
            if blocked:
                raise BedMasterConflict(
                    "These codes are already on the housekeeping register and are not "
                    f"clearly the same beds: {', '.join(sorted(set(blocked)))}. Use different "
                    "bed labels, or change those units in housekeeping. Nothing was saved.",
                    code="unit_codes_taken",
                    unit_codes=sorted(set(blocked)),
                )
            if adopt_for and not adopt:
                found = sorted(u["unit_code"] for u in adopt_for.values())
                raise BedMasterConflict(
                    f"{len(found)} of these beds are already on the housekeeping register "
                    f"({', '.join(found)}). Choose to adopt them to link them here and keep "
                    "their cleaning state, or use different labels. Nothing was saved.",
                    code="already_in_housekeeping",
                    unit_codes=found,
                )

            for label in labels:
                legacy_unit = adopt_for.get(_key(label))
                if legacy_unit is not None:
                    unit = hk.adopt_unit_on(
                        db,
                        legacy_unit["id"],
                        ward_id=ward["id"],
                        bed_label=label,
                        room_id=room["id"] if room else None,
                        bed_class=bed_class,
                        is_isolation_room=adopt_isolation,
                    )
                    adopted.append(_bed_out(unit))
                    _event(
                        db,
                        "bed",
                        unit["id"],
                        "adopted",
                        {
                            "unit_code": unit["unit_code"],
                            "previous_unit_code": legacy_unit["unit_code"],
                            "ward_id": ward["id"],
                            "ward_code": ward["code"],
                            "label": label,
                            "room_id": unit["room_id"],
                            "bed_class": unit["bed_class"],
                            "state": unit["state"],
                        },
                        actor,
                        now,
                        reason,
                    )
                else:
                    unit = hk.register_unit_on(
                        db,
                        ward_id=ward["id"],
                        room_id=room["id"] if room else None,
                        bed_label=label,
                        bed_class=bed_class,
                        is_isolation_room=bool(isolation),
                        notes=data.get("notes"),
                        actor_user_id=(actor or {}).get("id"),
                        actor_role=(actor or {}).get("role"),
                        create_only=True,
                    )
                    registered.append(_bed_out(unit))
                    _event(
                        db,
                        "bed",
                        unit["id"],
                        "registered",
                        {
                            "unit_code": unit["unit_code"],
                            "ward_id": ward["id"],
                            "ward_code": ward["code"],
                            "label": label,
                            "room_id": unit["room_id"],
                            "bed_class": unit["bed_class"],
                            "is_isolation_room": bool(isolation),
                        },
                        actor,
                        now,
                        reason,
                    )
            _audit(
                db,
                actor,
                "bed_master_beds_registered",
                f"{len(registered)} beds registered and {len(adopted)} adopted on {ward['code']}",
                target_type="ward",
                target_id=ward["id"],
                request_meta=request_meta,
                metadata={
                    "ward_code": ward["code"],
                    "registered": len(registered),
                    "adopted": len(adopted),
                    "labels": labels[:MAX_BEDS_PER_REGISTRATION],
                    "room_id": room["id"] if room else None,
                    "bed_class": bed_class,
                },
            )
    except hk.HousekeepingError as exc:
        raise BedMasterConflict(f"{exc} Nothing was saved.", code="housekeeping_refused") from exc
    except DBIntegrityError as exc:
        raise BedMasterConflict(
            "One of these beds clashes with a unit already on the "
            "housekeeping register. Nothing was saved.",
            code="unit_codes_taken",
        ) from exc
    _refresh_capacity()
    return {
        "ward_id": ward["id"],
        "ward_code": ward["code"],
        "registered": registered,
        "adopted": adopted,
        "count": len(registered) + len(adopted),
    }


def update_bed(bed_id: str, data: dict, *, actor: dict, request_meta: dict | None = None) -> dict:
    """Change a master bed's room, bed class, isolation flag, notes or label.

    The label is immutable once anything refers to the bed (its cleaning record
    beyond registration, cleaning requests, admissions, patients, movements).
    A retired bed cannot be changed. The cleaning state is never touched here.
    A bed moved to another room without an isolation flag in the request takes
    the new room's flag, or none when the room is cleared."""
    data = dict(data or {})
    reason = data.pop("reason", None)
    hk.ensure_housekeeping_schema()
    facilities = _actor_facilities(actor)
    now = utc_iso_now()
    try:
        with db_session(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM housekeeping_units WHERE id = ? AND ward_id IS NOT NULL", (bed_id,)
            ).fetchone()
            if row is None or _outside_scope(db, row["ward_id"], facilities):
                raise BedMasterNotFound("No bed with that id is set up", code="bed_not_found")
            unit = dict(row)
            if unit.get("retired_at"):
                raise BedMasterConflict(f"Bed {unit['unit_code']} is retired", code="bed_retired")
            ward = _ward_row(db, unit["ward_id"])
            kwargs: dict[str, Any] = {}
            room_isolation = False
            if "room_id" in data:
                if data["room_id"]:
                    room = _room_row(db, str(data["room_id"]))
                    if room["ward_id"] != ward["id"]:
                        raise BedMasterError(
                            f"That room is not on {ward['code']}", code="room_not_on_ward"
                        )
                    if not room["is_active"]:
                        raise BedMasterConflict(
                            f"Room {room['code']} is switched off", code="room_inactive"
                        )
                    kwargs["room_id"] = room["id"]
                    room_isolation = bool(room["is_isolation"])
                else:
                    kwargs["room_id"] = None
            if "bed_class" in data:
                kwargs["bed_class"] = _bed_class_code(db, data["bed_class"])
            isolation_follows_room = False
            if data.get("is_isolation_room") is not None:
                kwargs["is_isolation_room"] = bool(data["is_isolation_room"])
            elif "room_id" in kwargs and kwargs["room_id"] != unit.get("room_id"):
                # A bed moved to another room takes that room's isolation flag
                # (none with no room), as register_beds sets it. The flag is
                # what makes housekeeping send a terminal clean after discharge,
                # so a bed moved into an isolation room must not keep a bay's
                # standard clean.
                kwargs["is_isolation_room"] = room_isolation
                isolation_follows_room = True
            if "notes" in data:
                kwargs["notes"] = data["notes"]
            if data.get("bed_label") is not None:
                label = _norm_code(data["bed_label"], "Bed label")
                if label != unit["bed_label"]:
                    refs = _bed_references(db, unit, ward["code"])
                    if refs:
                        raise BedMasterConflict(
                            f"Bed {unit['bed_label']} on {ward['code']} is already in use "
                            f"({_references_words(refs)}), so its label cannot be changed.",
                            code="label_in_use",
                            references=refs,
                        )
                    clash = db.execute(
                        "SELECT bed_label FROM housekeeping_units WHERE ward_id = ? AND id <> ? "
                        "AND LOWER(TRIM(bed_label)) = LOWER(TRIM(?))",
                        (ward["id"], bed_id, label),
                    ).fetchone()
                    if clash is not None:
                        raise BedMasterConflict(
                            f"{ward['code']} already has bed {clash['bed_label']}",
                            code="labels_taken",
                            labels=[clash["bed_label"]],
                        )
                    # The same legacy check register_beds makes: a bed typed into
                    # the housekeeping register on this ward with this label
                    # (switched off ones included), or any older unit already
                    # holding the new unit code. Relabelling onto it would leave
                    # two housekeeping rows for one physical bed, and the
                    # discharge turnaround could land on the stale one. A rename
                    # never adopts; register_beds with adopt_existing does that.
                    legacy = [
                        dict(r)
                        for r in db.execute(
                            """SELECT unit_code, ward, bed_label, unit_type
                             FROM housekeeping_units
                            WHERE ward_id IS NULL
                              AND (unit_code = ?
                                   OR (unit_type = 'bed'
                                       AND LOWER(TRIM(ward)) = LOWER(TRIM(?))
                                       AND LOWER(TRIM(COALESCE(bed_label, ''))) = ?))
                            ORDER BY unit_code""",
                            (hk.master_unit_code(ward["code"], label), ward["code"], _key(label)),
                        ).fetchall()
                    ]
                    if legacy:
                        same_bed = sorted(
                            u["unit_code"]
                            for u in legacy
                            if u["unit_type"] == "bed"
                            and _key(u["ward"] or "") == _key(ward["code"])
                            and _key(u["bed_label"] or "") == _key(label)
                        )
                        if same_bed:
                            raise BedMasterConflict(
                                f"Bed {label} on {ward['code']} is already on the housekeeping "
                                f"register ({', '.join(same_bed)}), so this bed cannot take that "
                                "label. Adopt that bed in Bed setup, or choose a different "
                                "label. Nothing was changed.",
                                code="already_in_housekeeping",
                                unit_codes=same_bed,
                            )
                        held = sorted(u["unit_code"] for u in legacy)
                        raise BedMasterConflict(
                            f"{', '.join(held)} is already on the housekeeping register as "
                            "something else. Choose a different label, or change that unit in "
                            "housekeeping. Nothing was changed.",
                            code="unit_codes_taken",
                            unit_codes=held,
                        )
                    kwargs["bed_label"] = label
            after = hk.update_master_bed_on(db, bed_id, **kwargs)
            changes = {
                k: [unit.get(k), after.get(k)]
                for k in (
                    "room_id",
                    "room",
                    "bed_class",
                    "is_isolation_room",
                    "notes",
                    "bed_label",
                    "unit_code",
                )
                if unit.get(k) != after.get(k)
            }
            if changes:
                detail: dict[str, Any] = {"changes": changes}
                if isolation_follows_room and "is_isolation_room" in changes:
                    detail["isolation_follows_room"] = True
                _event(db, "bed", bed_id, "updated", detail, actor, now, reason)
                _audit(
                    db,
                    actor,
                    "bed_master_bed_updated",
                    f"Bed {after['unit_code']} changed: {', '.join(sorted(changes))}",
                    target_type="bed",
                    target_id=bed_id,
                    request_meta=request_meta,
                    metadata=detail,
                )
    except hk.HousekeepingError as exc:
        raise BedMasterConflict(
            f"{exc}. Nothing was changed.", code="housekeeping_refused"
        ) from exc
    except DBIntegrityError as exc:
        raise BedMasterConflict(
            "That label clashes with a unit already on the housekeeping "
            "register. Nothing was changed.",
            code="unit_codes_taken",
        ) from exc
    _refresh_capacity()
    return _bed_out(after)


# ═════════════════════════════════════════════════════════════════════════════
#  Bed classes
# ═════════════════════════════════════════════════════════════════════════════


def save_bed_class(
    data: dict, *, actor: dict, code: str | None = None, request_meta: dict | None = None
) -> dict:
    """Create a bed class (code None) or change one. The code is the key and
    never changes. A class a ward or an active bed uses cannot be switched off."""
    data = dict(data or {})
    reason = data.pop("reason", None)
    hk.ensure_housekeeping_schema()
    now = utc_iso_now()
    try:
        with db_session(immediate=True) as db:
            if code is None:
                new_code = _norm_code(data.get("code"), "Bed class code")
                name = _clean(data.get("name"))
                if not name:
                    raise BedMasterError(
                        "Bed class name is required", code="required", field="name"
                    )
                clash = db.execute(
                    "SELECT code FROM bed_classes WHERE LOWER(TRIM(code)) = LOWER(TRIM(?))",
                    (new_code,),
                ).fetchone()
                if clash is not None:
                    raise BedMasterConflict(
                        f"Bed class {clash['code']} is already set up. Codes are compared without "
                        "regard to capital letters.",
                        code="duplicate_code",
                    )
                charge = _clean(data.get("bed_day_service_code"), _MAX_CODE)
                critical = bool(data.get("is_critical_care") or False)
                census = data.get("counts_in_census")
                census = True if census is None else bool(census)
                db.execute(
                    """INSERT INTO bed_classes
                           (code, name, bed_day_service_code, is_critical_care, counts_in_census,
                            is_active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                    (new_code, name, charge, 1 if critical else 0, 1 if census else 0, now, now),
                )
                detail: dict[str, Any] = {
                    "code": new_code,
                    "name": name,
                    "bed_day_service_code": charge,
                    "is_critical_care": critical,
                    "counts_in_census": census,
                }
                _event(db, "bed_class", new_code, "created", detail, actor, now, reason)
                _audit(
                    db,
                    actor,
                    "bed_master_bed_class_created",
                    f"Bed class {new_code} set up",
                    target_type="bed_class",
                    target_id=new_code,
                    request_meta=request_meta,
                    metadata=detail,
                )
                key = new_code
            else:
                row = db.execute("SELECT * FROM bed_classes WHERE code = ?", (code,)).fetchone()
                if row is None:
                    raise BedMasterNotFound(
                        "No bed class with that code is set up", code="bed_class_not_found"
                    )
                current = dict(row)
                new: dict[str, Any] = {}
                if (
                    data.get("code") is not None
                    and _norm_code(data["code"], "Bed class code") != code
                ):
                    raise BedMasterError(
                        "A bed class code cannot be changed. Set up a new class instead.",
                        code="code_fixed",
                        field="code",
                    )
                if data.get("name") is not None:
                    name = _clean(data["name"])
                    if not name:
                        raise BedMasterError(
                            "Bed class name is required", code="required", field="name"
                        )
                    new["name"] = name
                if "bed_day_service_code" in data:
                    new["bed_day_service_code"] = _clean(data["bed_day_service_code"], _MAX_CODE)
                if data.get("is_critical_care") is not None:
                    new["is_critical_care"] = 1 if data["is_critical_care"] else 0
                if data.get("counts_in_census") is not None:
                    new["counts_in_census"] = 1 if data["counts_in_census"] else 0
                if data.get("is_active") is not None:
                    active = 1 if data["is_active"] else 0
                    if not active and current["is_active"]:
                        beds = _count(
                            db,
                            "SELECT COUNT(*) FROM housekeeping_units WHERE bed_class = ? "
                            "AND is_active = 1 AND retired_at IS NULL",
                            (code,),
                        )
                        wards = _count(
                            db,
                            "SELECT COUNT(*) FROM wards WHERE default_bed_class = ? "
                            "AND is_active = 1",
                            (code,),
                        )
                        if beds or wards:
                            raise BedMasterConflict(
                                f"Bed class {code} is used by {beds} beds and {wards} wards, so it "
                                "cannot be switched off.",
                                code="bed_class_in_use",
                                beds=beds,
                                wards=wards,
                            )
                    new["is_active"] = active
                changes = {k: [current.get(k), v] for k, v in new.items() if current.get(k) != v}
                if changes:
                    cols = ", ".join(f"{k} = ?" for k in changes)
                    db.execute(
                        f"UPDATE bed_classes SET {cols}, updated_at = ? WHERE code = ?",
                        (*[v[1] for v in changes.values()], now, code),
                    )
                    _event(
                        db, "bed_class", code, "updated", {"changes": changes}, actor, now, reason
                    )
                    _audit(
                        db,
                        actor,
                        "bed_master_bed_class_updated",
                        f"Bed class {code} changed: {', '.join(sorted(changes))}",
                        target_type="bed_class",
                        target_id=code,
                        request_meta=request_meta,
                        metadata={"changes": changes},
                    )
                key = code
            out = dict(db.execute("SELECT * FROM bed_classes WHERE code = ?", (key,)).fetchone())
    except DBIntegrityError as exc:
        raise BedMasterConflict(
            "That bed class clashes with one already set up. Nothing was changed.",
            code="duplicate_code",
        ) from exc
    _refresh_capacity()
    return _bool(out, "is_critical_care", "counts_in_census", "is_active")


# ═════════════════════════════════════════════════════════════════════════════
#  Site configuration import
# ═════════════════════════════════════════════════════════════════════════════


def import_site_config_wards(*, actor: dict, request_meta: dict | None = None) -> dict:
    """Set up one ward per bed prefix the site configuration declares, in the
    department that declares it. Idempotent: a ward already set up (any
    capitalisation) is reported as existing and left alone, and one set up in a
    different department is reported as a conflict and left alone. One
    immediate session for every ward, event and the audit line."""
    from services.site_config_service import current_site_config

    cfg = current_site_config()
    facilities = _actor_facilities(actor)
    covering, declared = _department_lists(facilities)
    plan: list[tuple[str, str]] = []
    skipped: list[dict] = []
    for dept in cfg.departments:
        dept_name = " ".join(str(dept.get("name") or "").split())
        if facilities is not None:
            # multisite-8h1: another facility's wards are set up from there.
            from services import facility_lanes, facility_service

            if facility_lanes.outside(facility_service.department_facility(dept_name), facilities):
                skipped.extend(
                    {
                        "code": str(prefix),
                        "department": dept_name,
                        "reason": DEPARTMENT_OTHER_FACILITY_WORDS,
                    }
                    for prefix in dept.get("wards") or []
                )
                continue
        for prefix in dept.get("wards") or []:
            try:
                plan.append((_norm_code(prefix, "Ward code"), dept_name))
            except BedMasterError as exc:
                skipped.append({"code": str(prefix), "department": dept_name, "reason": str(exc)})
    created: list[dict] = []
    existing: list[dict] = []
    conflicts: list[dict] = []
    if not plan:
        return {
            "created": created,
            "existing": existing,
            "conflicts": conflicts,
            "skipped": skipped,
            "file_present": bool(cfg.present),
            "message": "The site configuration declares no wards, so nothing was set up.",
        }
    now = utc_iso_now()
    try:
        with db_session(immediate=True) as db:
            for code, dept_name in plan:
                department = _resolve_department(dept_name, covering, declared)
                row = db.execute(
                    "SELECT * FROM wards WHERE LOWER(TRIM(code)) = LOWER(TRIM(?))", (code,)
                ).fetchone()
                if row is not None:
                    if (row["department"] or "").casefold() == department.casefold():
                        existing.append(_ward_out(row, covering))
                    else:
                        conflicts.append(
                            {
                                "code": row["code"],
                                "ward_id": row["id"],
                                "department_in_setup": row["department"],
                                "department_in_file": department,
                            }
                        )
                    continue
                placed = _ward_facility_for(db, department, facilities)
                new_id = _new_id("WRD")
                db.execute(
                    """INSERT INTO wards
                           (id, facility_code, code, name, department, ward_type,
                            cleaning_tracked, is_active, created_by, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'general', 1, 1, ?, ?, ?)""",
                    (
                        new_id,
                        FACILITY_CODE,
                        code,
                        code,
                        department,
                        (actor or {}).get("id"),
                        now,
                        now,
                    ),
                )
                if placed is not None:
                    # multisite-8h1: the ward is at its department's facility.
                    db.execute(
                        "UPDATE wards SET facility_id = ?, facility_code = ? WHERE id = ?",
                        (placed[0], placed[1], new_id),
                    )
                _event(
                    db,
                    "ward",
                    new_id,
                    "imported",
                    {"code": code, "department": department, "source": "site configuration"},
                    actor,
                    now,
                )
                created.append(
                    _ward_out(
                        db.execute("SELECT * FROM wards WHERE id = ?", (new_id,)).fetchone(),
                        covering,
                    )
                )
            if created:
                _audit(
                    db,
                    actor,
                    "bed_master_site_config_imported",
                    f"{len(created)} wards set up from the site configuration",
                    target_type="ward",
                    target_id=created[0]["id"],
                    request_meta=request_meta,
                    metadata={
                        "created": [w["code"] for w in created],
                        "existing": [w["code"] for w in existing],
                        "conflicts": [c["code"] for c in conflicts],
                    },
                )
    except DBIntegrityError as exc:
        raise BedMasterConflict(
            "A ward in the site configuration clashes with one already set up. Nothing was saved.",
            code="duplicate_code",
        ) from exc
    if created:
        _refresh_capacity()
    return {
        "created": created,
        "existing": existing,
        "conflicts": conflicts,
        "skipped": skipped,
        "file_present": bool(cfg.present),
        "message": None,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Retire a bed (adt-3)
# ═════════════════════════════════════════════════════════════════════════════


def _typed_occupants(db, ward_code: str, label: str) -> list[str]:
    """Active admissions with no bed_id whose typed ward and bed text name this
    bed. They lie in it as far as every screen that prints the text knows."""
    return [
        r["id"]
        for r in db.execute(
            """SELECT id FROM admissions
            WHERE status = 'active' AND bed_id IS NULL
              AND LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?))
              AND LOWER(TRIM(COALESCE(bed, ''))) = LOWER(TRIM(?))""",
            (ward_code, label),
        ).fetchall()
    ]


def _bed_reserved(db, bed_id: str) -> bool:
    """A reservation holds the bed now (adt-12: active and not past its
    expiry, read through services/adt/reservations.py). Before migration 69
    nothing can be reserved."""
    from services.adt import reservations as adt_res

    return adt_res.live_on_bed(db, bed_id) is not None


def retire_bed(
    bed_id: str, data: dict | None = None, *, actor: dict, request_meta: dict | None = None
) -> dict:
    """Retire a master bed: it leaves the board and every picker, keeps its
    cleaning record, its label and its history, and is never deleted.

    Refused while an active admission holds the bed (by bed_id, or by typed
    ward and bed text naming it), while a reservation holds it, and when it is
    already retired. A retired label stays taken on the ward: set up a bed
    with a new label instead."""
    data = dict(data or {})
    reason = data.pop("reason", None)
    hk.ensure_housekeeping_schema()
    facilities = _actor_facilities(actor)
    now = utc_iso_now()
    with db_session(immediate=True) as db:
        row = db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ? AND ward_id IS NOT NULL", (bed_id,)
        ).fetchone()
        if row is None or _outside_scope(db, row["ward_id"], facilities):
            raise BedMasterNotFound("No bed with that id is set up", code="bed_not_found")
        unit = dict(row)
        if unit.get("retired_at"):
            raise BedMasterConflict(
                f"Bed {unit['unit_code']} is already retired", code="bed_retired"
            )
        ward = _ward_row(db, unit["ward_id"])
        held = [
            r["id"]
            for r in db.execute(
                "SELECT id FROM admissions WHERE bed_id = ? AND status = 'active'", (bed_id,)
            ).fetchall()
        ]
        held += _typed_occupants(db, ward["code"], unit.get("bed_label") or "")
        if held:
            raise BedMasterConflict(
                f"Bed {unit['bed_label']} on {ward['code']} has a patient in it, so it cannot be "
                "retired. Move the patient first.",
                code="bed_occupied",
                admissions=len(held),
            )
        if _bed_reserved(db, bed_id):
            raise BedMasterConflict(
                f"Bed {unit['bed_label']} on {ward['code']} is reserved, so it cannot be retired. "
                "Release the reservation first.",
                code="bed_reserved",
            )
        db.execute(
            "UPDATE housekeeping_units SET retired_at = ?, is_active = 0, updated_at = ? WHERE id = ?",
            (now, now, bed_id),
        )
        detail: dict[str, Any] = {
            "unit_code": unit["unit_code"],
            "ward_id": ward["id"],
            "ward_code": ward["code"],
            "label": unit.get("bed_label"),
            "state": unit.get("state"),
        }
        _event(db, "bed", bed_id, "retired", detail, actor, now, reason)
        _audit(
            db,
            actor,
            "bed_master_bed_retired",
            f"Bed {unit['unit_code']} retired",
            target_type="bed",
            target_id=bed_id,
            request_meta=request_meta,
            metadata=detail,
        )
        after = db.execute("SELECT * FROM housekeeping_units WHERE id = ?", (bed_id,)).fetchone()
    _refresh_capacity()
    return _bed_out(after)


# ═════════════════════════════════════════════════════════════════════════════
#  Reconciliation, link and register complete (adt-3)
# ═════════════════════════════════════════════════════════════════════════════

_LINK_REASON = "Linked to the bed master by reconciliation"


def _entry(
    adm: dict, *, ward: dict | None = None, bed: dict | None = None, reason: str | None = None
) -> dict:
    return {
        "admission_id": adm["id"],
        "patient_id": adm["patient_id"],
        "department": adm.get("department"),
        "ward": adm.get("ward"),
        "bed": adm.get("bed"),
        "ward_id": adm.get("ward_id"),
        "bed_id": adm.get("bed_id"),
        "master_ward_id": ward["id"] if ward else None,
        "master_ward_code": ward["code"] if ward else None,
        "master_bed_id": bed["id"] if bed else None,
        "master_bed_label": bed["bed_label"] if bed else None,
        "master_unit_code": bed["unit_code"] if bed else None,
        "reason": reason,
    }


def _reconcile_on(
    db: Any,
    *,
    ward_id: str | None,
    dept: str | None,
    facilities: frozenset[int] | None = None,
) -> dict:
    """Sort every active admission (inside `dept`, and on `ward_id` when
    given) against the master. Pure read on the caller's connection.

    `facilities` (multisite-8h1) is the caller's facility scope: with one,
    only admissions of patients at those facilities are sorted, and a ward
    of another facility is 404 like one that does not exist."""
    from permissions import scope_patient_ids_sql

    from services.adt.core import match_location_on

    ward_filter = _ward_row(db, ward_id) if ward_id else None
    if ward_filter is not None and _outside_scope(db, ward_filter["id"], facilities):
        raise BedMasterNotFound("No ward with that id is set up", code="ward_not_found")
    scope_sql, scope_params = scope_patient_ids_sql(dept, facilities=facilities)
    rows = [
        dict(r)
        for r in db.execute(
            f"""SELECT id, patient_id, department, ward, bed, ward_id, bed_id, created_at
              FROM admissions
             WHERE status = 'active' AND patient_id IN ({scope_sql})
             ORDER BY created_at, rowid""",
            scope_params,
        ).fetchall()
    ]

    linked: list[dict] = []
    candidates: list[dict] = []
    unlinked: list[dict] = []
    awaiting: list[dict] = []
    mismatches: list[dict] = []
    claims: dict[str, list[dict]] = {}
    beds: dict[str, dict] = {}
    for adm in rows:
        if adm.get("bed_id"):
            bed = db.execute(
                "SELECT * FROM housekeeping_units WHERE id = ?", (adm["bed_id"],)
            ).fetchone()
            bed = dict(bed) if bed else None
            ward = _ward_row(db, bed["ward_id"]) if bed and bed.get("ward_id") else None
            if ward_filter and (ward is None or ward["id"] != ward_filter["id"]):
                continue
            entry = _entry(adm, ward=ward, bed=bed)
            linked.append(entry)
            if bed is not None:
                assert ward is not None
                beds[bed["id"]] = bed
                claims.setdefault(bed["id"], []).append(entry)
                if adm.get("ward") != ward["code"] or adm.get("bed") != bed["bed_label"]:
                    mismatches.append(entry)
            continue
        match = match_location_on(db, adm.get("ward"), adm.get("bed"))
        ward, bed = match["ward"], match["bed"]
        on_ward = (
            ward is not None and ward_filter is not None and ward["id"] == ward_filter["id"]
        ) or (ward_filter is not None and adm.get("ward_id") == ward_filter["id"])
        if ward_filter and not on_ward:
            continue
        if not match["bed_text"]:
            awaiting.append(_entry(adm, ward=ward, reason="awaiting_bed"))
        elif ward is None:
            unlinked.append(_entry(adm, reason="ward_not_in_setup"))
        elif bed is None:
            unlinked.append(_entry(adm, ward=ward, reason="bed_not_in_setup"))
        elif bed.get("retired_at") or not bed.get("is_active"):
            unlinked.append(_entry(adm, ward=ward, bed=bed, reason="bed_retired"))
        else:
            entry = _entry(adm, ward=ward, bed=bed)
            candidates.append(entry)
            beds[bed["id"]] = bed
            claims.setdefault(bed["id"], []).append(entry)

    doubled_ids = {bid for bid, who in claims.items() if len(who) > 1}
    matched = [e for e in candidates if e["master_bed_id"] not in doubled_ids]
    doubled = [
        {
            "bed_id": bid,
            "unit_code": beds[bid].get("unit_code"),
            "bed_label": beds[bid].get("bed_label"),
            "ward_id": beds[bid].get("ward_id"),
            "admissions": [dict(e, reason="doubled_bed") for e in claims[bid]],
        }
        for bid in sorted(doubled_ids)
    ]
    return {
        "ward_id": ward_filter["id"] if ward_filter else None,
        "ward_code": ward_filter["code"] if ward_filter else None,
        "linked": linked,
        "matched": matched,
        "doubled": doubled,
        "unlinked": unlinked,
        "awaiting": awaiting,
        "mirror_mismatches": mismatches,
        "counts": {
            "active_admissions": len(linked) + len(candidates) + len(unlinked) + len(awaiting),
            "linked": len(linked),
            "matched": len(matched),
            "doubled_beds": len(doubled),
            "unlinked": len(unlinked),
            "awaiting": len(awaiting),
            "mirror_mismatches": len(mismatches),
        },
    }


def _patient_ids(report: dict) -> list[str]:
    seen: list[str] = []
    for key in ("linked", "matched", "unlinked", "awaiting"):
        for e in report[key]:
            if e["patient_id"] not in seen:
                seen.append(e["patient_id"])
    for d in report["doubled"]:
        for e in d["admissions"]:
            if e["patient_id"] not in seen:
                seen.append(e["patient_id"])
    return seen


def reconciliation_report(
    *, actor: dict, ward_id: str | None = None, request_meta: dict | None = None
) -> dict:
    """The active admissions sorted against the master: linked, matched,
    doubled, unlinked and awaiting a bed, with counts. Lists admission and
    patient ids against typed places (no name, no MRN), so the read is
    audited as phi_access with the patient ids, inside the read's session
    (fail-closed: no audit, no report)."""
    from permissions import department_scope

    dept = department_scope(actor)
    facilities = _actor_facilities(actor)
    hk.ensure_housekeeping_schema()
    with db_session() as db:
        report = _reconcile_on(db, ward_id=ward_id, dept=dept, facilities=facilities)
        patient_ids = _patient_ids(report)
        if patient_ids:
            meta = request_meta or {}
            log_audit_event(
                correlation_id=str(uuid.uuid4()),
                event_category="phi_access",
                action_type="bed_reconciliation_viewed",
                outcome="success",
                db=db,
                summary_message=(
                    f"Bed reconciliation viewed: {len(patient_ids)} patients"
                    + (f" on {report['ward_code']}" if report["ward_code"] else "")
                ),
                actor_user_id=(actor or {}).get("id"),
                actor_role=(actor or {}).get("role"),
                target_type="ward" if ward_id else "bed_master",
                target_id=str(ward_id) if ward_id else "all",
                request_path=meta.get("request_path"),
                http_method=meta.get("http_method"),
                ip_address=meta.get("ip_address"),
                user_agent=meta.get("user_agent"),
                metadata={
                    "patient_ids": patient_ids[:300],
                    "patient_count": len(patient_ids),
                    "department": dept,
                },
            )
    return report


def link_admissions(data: dict, *, actor: dict, request_meta: dict | None = None) -> dict:
    """Link matched admissions to their master beds, confirmed by the bed
    manager, in ONE immediate session.

    data: ward_id (only that ward), admission_ids (only these; default every
    matched one in scope), boarder_acknowledged, out_of_service_reason, reason.
    The match is recomputed inside the session, so what is linked is what the
    master says now. A doubled bed is never linked; an admission that is not
    matched is reported, not linked. A placement that needs words (a boarder,
    an out-of-service bed) without them is skipped with the words. Each link
    is one 'location_linked' movement and one patient_movement audit written
    by services.adt.core.locate_admission_on; an unwritable audit rolls every
    link back."""
    from permissions import department_scope

    from services.adt.core import (
        AdtCrossFacility,
        AdtError,
        audit_facility_refusal,
        locate_admission_on,
    )

    crossings: list[AdtCrossFacility] = []
    data = dict(data or {})
    reason = _clean(data.get("reason"), _MAX_NOTE) or _LINK_REASON
    wanted = data.get("admission_ids")
    if wanted is not None and not isinstance(wanted, (list, tuple)):
        raise BedMasterError("admission_ids must be a list", code="bad_admission_ids")
    wanted_set = {str(x) for x in wanted} if wanted else None
    dept = department_scope(actor)
    facilities = _actor_facilities(actor)
    hk.ensure_housekeeping_schema()
    linked: list[dict] = []
    skipped: list[dict] = []
    with db_session(immediate=True) as db:
        report = _reconcile_on(db, ward_id=data.get("ward_id"), dept=dept, facilities=facilities)
        targets = [
            e for e in report["matched"] if wanted_set is None or e["admission_id"] in wanted_set
        ]
        if wanted_set is not None:
            classes = {
                "linked": "already_linked",
                "unlinked": "not_matched",
                "awaiting": "awaiting_bed",
            }
            for key, why in classes.items():
                for e in report[key]:
                    if e["admission_id"] in wanted_set:
                        skipped.append(
                            {"admission_id": e["admission_id"], "code": why, "message": None}
                        )
            for d in report["doubled"]:
                for e in d["admissions"]:
                    if e["admission_id"] in wanted_set and e.get("bed_id") is None:
                        skipped.append(
                            {
                                "admission_id": e["admission_id"],
                                "code": "doubled_bed",
                                "message": f"{d['unit_code']} is claimed by more than one "
                                "admission. Settle where each patient lies on "
                                "the ward first.",
                            }
                        )
            known = {e["admission_id"] for e in targets} | {s["admission_id"] for s in skipped}
            for missing in sorted(wanted_set - known):
                skipped.append({"admission_id": missing, "code": "not_found", "message": None})
        for e in targets:
            try:
                placed = locate_admission_on(
                    db,
                    admission_id=e["admission_id"],
                    actor=actor,
                    movement_type="location_linked",
                    reason=reason,
                    ward_id=e["master_ward_id"],
                    bed_id=e["master_bed_id"],
                    expected_bed_id=None,
                    out_of_service_reason=data.get("out_of_service_reason"),
                    boarder_acknowledged=bool(data.get("boarder_acknowledged")),
                    request_meta=request_meta,
                )
            except AdtError as exc:
                if isinstance(exc, AdtCrossFacility):
                    crossings.append(exc)
                skipped.append(
                    {"admission_id": e["admission_id"], "code": exc.code, "message": str(exc)}
                )
                continue
            linked.append(
                {
                    "admission_id": e["admission_id"],
                    "patient_id": e["patient_id"],
                    "bed_id": placed["bed_id"],
                    "ward_id": placed["ward_id"],
                    "ward": placed["ward"],
                    "bed": placed["bed"],
                    "movement_id": placed["movement"]["id"],
                    "boarder": placed["boarder"],
                }
            )
    # multisite-8h1: a link refused because the bed is at another facility is
    # recorded once the links have committed.
    for crossing in crossings:
        audit_facility_refusal(
            crossing, lane="bed_reconciliation_link", actor=actor, request_meta=request_meta
        )
    return {"linked": linked, "skipped": skipped, "count": len(linked)}


def mark_register_complete(
    ward_id: str, data: dict | None = None, *, actor: dict, request_meta: dict | None = None
) -> dict:
    """Say this ward's bed register is complete. From then on the ward's ADT
    figures count from wards.ledger_complete_from; earlier days read
    'insufficient data', never zero.

    Refused, with the admissions and beds named by id, while any active
    admission on the ward is unlinked (typed bed text with no master bed) or
    any bed is claimed by more than one admission. Hospital-wide on purpose:
    a ward is complete for everyone or for no one. Marking a complete ward
    again changes nothing."""
    data = dict(data or {})
    reason = data.pop("reason", None)
    hk.ensure_housekeeping_schema()
    covering, _declared = _department_lists()
    facilities = _actor_facilities(actor)
    now = utc_iso_now()
    with db_session(immediate=True) as db:
        ward = _ward_row(db, ward_id)
        if _outside_scope(db, ward_id, facilities):
            raise BedMasterNotFound("No ward with that id is set up", code="ward_not_found")
        if ward.get("register_complete"):
            return _ward_out(ward, covering)
        report = _reconcile_on(db, ward_id=ward_id, dept=None)
        if report["unlinked"] or report["doubled"]:
            raise BedMasterConflict(
                f"{ward['code']} cannot be marked complete: {len(report['unlinked'])} admissions "
                f"are not linked to a bed in setup and {len(report['doubled'])} beds are claimed "
                "by more than one admission. Link or correct them first.",
                code="register_incomplete",
                unlinked=[e["admission_id"] for e in report["unlinked"]],
                doubled=[d["bed_id"] for d in report["doubled"]],
            )
        db.execute(
            """UPDATE wards SET register_complete = 1, register_complete_at = ?,
                      register_complete_by = ?, ledger_complete_from = COALESCE(ledger_complete_from, ?),
                      updated_at = ?
                WHERE id = ?""",
            (now, (actor or {}).get("id"), now, now, ward_id),
        )
        detail: dict[str, Any] = {
            "code": ward["code"],
            "ledger_complete_from": now,
            "linked_admissions": report["counts"]["linked"],
            "awaiting_bed": report["counts"]["awaiting"],
        }
        _event(db, "ward", ward_id, "register_completed", detail, actor, now, reason)
        _audit(
            db,
            actor,
            "bed_master_register_completed",
            f"Ward {ward['code']} bed register marked complete",
            target_type="ward",
            target_id=ward_id,
            request_meta=request_meta,
            metadata=detail,
        )
        row = db.execute("SELECT * FROM wards WHERE id = ?", (ward_id,)).fetchone()
    # From now on the capacity screens read this ward's count from the register.
    _refresh_capacity()
    return _ward_out(row, covering)


# ═════════════════════════════════════════════════════════════════════════════
#  The bed board, awaiting a bed, and available beds (adt-4a, W4-B)
# ═════════════════════════════════════════════════════════════════════════════
#
# A BED'S STATUS IS DERIVED, NEVER STORED (design section 4). Occupancy comes
# from the admission (an active admission holding bed_id, or typed ward and bed
# text that names the bed); cleaning comes from housekeeping read together with
# the movement ledger. In this precedence, first match wins:
#
#   1. occupied (on leave, absconded): an active admission holds the bed. When
#      housekeeping says out_of_service the tile still shows the occupant and
#      says 'bed reported out of service' (critique C2: a marker must never
#      hide a patient). This sits ABOVE retired on purpose: retire_bed refuses
#      an occupied bed, so the pair cannot arise through the writers, and if it
#      ever did the patient is what the board must show.
#   2. reserved (a bed_reservations row that is active and not past its
#      expiry, adt-12), with the same out-of-service marker. The tile says
#      what the bed is held for ('Reserved for transfer'), never who, outside
#      the caller's scope.
#   3. retired beds leave the board (retire_bed says so); a switched-off bed is
#      shown as switched off.
#   4. out_of_service.
#   5. needs_cleaning when housekeeping says vacant_dirty, or says occupied and
#      nobody occupies the bed, or says vacant_ready while the bed's latest
#      vacating movement is NEWER than its latest event into vacant_ready
#      (critique C3: a bed vacated before housekeeping caught up never reads
#      available, and no sync is needed for that).
#   6. cleaning (cleaning_in_progress).
#   7. available (vacant_ready and no newer vacating movement).
#
# CLEANING NOT TRACKED. While ORB_HOUSEKEEPING is off, or the ward's
# cleaning_tracked is 0, rules 4 to 7 read 'Vacant, cleaning not tracked' and
# raise no clean-related warning: a site without housekeeping would otherwise
# see every bed as needing a clean.
#
# IDENTITY. A tile names its occupant (name, MRN, treating department, 'Boarder
# from <department>') only when that patient is inside the caller's department
# scope (permissions.department_scope, the one derivation). Any other occupied
# tile says 'Occupied', or 'Boarder' when the patient is treated by another
# department than the ward's, and carries no patient field at all. A read that
# names anybody writes ONE fail-closed phi_access audit with the patient ids,
# inside the read's own session (no audit, no board); a read that names nobody
# writes none.
#
# WHICH WARDS. An account with no department (a hospital-wide administrator)
# sees every ward. A department account sees its department's wards and any
# ward where one of its own patients lies as a boarder; any other ward id is
# 404, never 403.

#: status -> (words, tone). The board prints `status_words` as the server
#: wrote them and styles by `tone`, so a later status (a leave or reservation
#: state) needs no client change. Sentence case, plain words.
BOARD_STATUSES: dict[str, tuple[str, str]] = {
    "occupied": ("Occupied", "occupied"),
    "occupied_on_leave": ("Occupied, patient on leave", "occupied"),
    "occupied_absconded": ("Occupied, patient absconded", "attention"),
    "boarder": ("Boarder", "occupied"),
    "reserved": ("Reserved", "held"),
    "switched_off": ("Switched off in bed setup", "off"),
    "out_of_service": ("Out of service", "off"),
    "needs_cleaning": ("Needs cleaning", "attention"),
    "cleaning": ("Being cleaned", "progress"),
    "available": ("Available", "ready"),
    "vacant_untracked": ("Vacant, cleaning not tracked", "neutral"),
}

#: Statuses a new patient could be placed in without a clean being skipped.
FREE_STATUSES = frozenset({"available", "vacant_untracked"})

#: Statuses that mean a patient lies in the bed.
OCCUPIED_STATUSES = frozenset({"occupied", "occupied_on_leave", "occupied_absconded", "boarder"})

#: Movement types that can take a patient OUT of a bed. A movement whose
#: destination is the same bed (a department-only transfer) vacates nothing.
VACATING_MOVEMENT_TYPES: tuple[str, ...] = (
    "discharge",
    "transfer",
    "bed_change",
    "bed_swap",
    "correction",
    "location_linked",
)

_OUT_OF_SERVICE_MARKER = "Bed reported out of service"


def _board_words(status: str) -> str:
    return BOARD_STATUSES.get(status, (status.replace("_", " ").capitalize(), "neutral"))[0]


def _board_tone(status: str) -> str:
    return BOARD_STATUSES.get(status, ("", "neutral"))[1]


def _cleaning_tracked(ward: dict) -> bool:
    """Housekeeping tracks this ward's cleaning: the module is on AND the
    ward says so. Read at call time."""
    import config

    return bool(getattr(config, "HOUSEKEEPING_ENABLED", False)) and bool(
        ward.get("cleaning_tracked")
    )


_UNIX_EPOCH_JD = 2440587.5


def _jd(value: Any) -> float | None:
    """The julian day of any stored spelling (what SQLite's julianday() gives),
    or None when blank or unreadable. A stored time with no offset is UTC."""
    if value in (None, ""):
        return None
    try:
        when = parse_to_utc(value)
    except (TypeError, ValueError):
        return None
    return when.timestamp() / 86400.0 + _UNIX_EPOCH_JD


def _jd_text(jd: float | None) -> str | None:
    """A julian day in the canonical UTC spelling, or None."""
    if jd is None:
        return None
    return format_utc(datetime.fromtimestamp((jd - _UNIX_EPOCH_JD) * 86400.0, tz=UTC))


def _minutes_since(jd: float | None, now_jd: float) -> int | None:
    if jd is None:
        return None
    return max(0, int(round((now_jd - jd) * 24 * 60)))


def _in(values: Iterable[Any]) -> tuple[str, tuple]:
    vals = tuple(values)
    return ", ".join("?" for _ in vals) or "NULL", vals


def _visible_wards(
    db: Any, dept: str | None, facilities: frozenset[int] | None = None
) -> list[dict]:
    """Every ward this caller's board may show (see WHICH WARDS above).

    `facilities` (multisite-8h1) is the caller's facility scope: with one,
    only the wards of those facilities, and the boarders counted only among
    the caller's patients there."""
    wards = [dict(r) for r in db.execute("SELECT * FROM wards ORDER BY LOWER(code)").fetchall()]
    if facilities is not None:
        from services.facility_lanes import wards_in_scope

        wards = wards_in_scope(wards, facilities)
    if dept is None:
        return wards
    from permissions import scope_patient_ids_sql

    scope_sql, scope_params = scope_patient_ids_sql(dept, facilities=facilities)
    holding = {
        r["ward_id"]
        for r in db.execute(
            f"""SELECT DISTINCT ward_id FROM admissions
             WHERE status = 'active' AND ward_id IS NOT NULL AND patient_id IN ({scope_sql})""",
            scope_params,
        ).fetchall()
    }
    key = dept.casefold()
    return [w for w in wards if (w.get("department") or "").casefold() == key or w["id"] in holding]


def _occupants_on(db, wards: list[dict], beds: list[dict]) -> dict[str, list[dict]]:
    """bed id -> the active admissions lying in it: by bed_id, else by typed
    ward and bed text naming exactly that ward code and bed label."""
    by_bed: dict[str, list[dict]] = {}
    if not beds:
        return by_bed
    marks, ids = _in(b["id"] for b in beds)
    for row in db.execute(
        f"""SELECT id, patient_id, department, ward, bed, ward_id, bed_id, presence_status,
                   presence_since, admitted_at, created_at
              FROM admissions WHERE status = 'active' AND bed_id IN ({marks})""",
        ids,
    ).fetchall():
        adm = dict(row)
        adm["linked"] = True
        by_bed.setdefault(adm["bed_id"], []).append(adm)
    codes = {_key(w["code"]): w for w in wards}
    labels = {(b["ward_id"], _key(b.get("bed_label") or "")): b["id"] for b in beds}
    ward_marks, ward_keys = _in(codes)
    for row in db.execute(
        f"""SELECT id, patient_id, department, ward, bed, ward_id, bed_id, presence_status,
                   presence_since, admitted_at, created_at
              FROM admissions
             WHERE status = 'active' AND bed_id IS NULL AND COALESCE(TRIM(bed), '') <> ''
               AND LOWER(TRIM(COALESCE(ward, ''))) IN ({ward_marks})""",
        ward_keys,
    ).fetchall():
        adm = dict(row)
        ward = codes.get(_key(" ".join(str(adm.get("ward") or "").split())))
        if ward is None:
            continue
        bed_id = labels.get((ward["id"], _key(" ".join(str(adm.get("bed") or "").split()))))
        if bed_id is None:
            continue
        adm["linked"] = False
        by_bed.setdefault(bed_id, []).append(adm)
    return by_bed


def _presence_prompt(holder: dict, *, absconded_at: datetime | None, now: datetime) -> str | None:
    """The board's advisory line for a patient off the ward too long (adt-10),
    or None. Advisory only: nothing closes or discharges on a timer.

      absconded  over ORB_ADT_ABSCONDED_CLOSE_HOURS since it was recorded
                 (ruling ADT-S3: the stay may then be closed through End of
                 stay)
      on leave   over ORB_ADT_LEAVE_PROMPT_HOURS (ruling ADT-P6: the doctor
                 is prompted to discharge)"""
    import config

    presence = holder.get("presence_status")
    if presence == "absconded":
        hours = int(getattr(config, "ADT_ABSCONDED_CLOSE_HOURS", 24))
        since = absconded_at or _parse_utc(holder.get("presence_since"))
        if since is not None and (now - since).total_seconds() >= hours * 3600:
            return (
                f"Absconded for over {hours} hours. The stay can be closed with Record end of stay."
            )
    elif presence == "on_leave":
        hours = int(getattr(config, "ADT_LEAVE_PROMPT_HOURS", 48))
        since = _parse_utc(holder.get("presence_since"))
        if since is not None and (now - since).total_seconds() >= hours * 3600:
            return f"On leave for over {hours} hours. The doctor may discharge."
    return None


def _parse_utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


def _last_ready_jd(db, beds: list[dict]) -> dict[str, float]:
    """bed id -> julian day of its latest housekeeping event into vacant_ready."""
    if not beds:
        return {}
    marks, ids = _in(b["id"] for b in beds)
    return {
        r[0]: float(r[1])
        for r in db.execute(
            f"""SELECT unit_id, MAX(julianday(occurred_at)) FROM housekeeping_unit_events
             WHERE to_state = 'vacant_ready' AND unit_id IN ({marks}) GROUP BY unit_id""",
            ids,
        ).fetchall()
        if r[1] is not None
    }


def _last_vacated_jd(db, wards: list[dict], beds: list[dict]) -> dict[str, float]:
    """bed id -> julian day of its latest vacating movement: by from_bed_id,
    or, for a movement written before the bed was linked, by its typed ward
    and bed text. A movement into the same bed vacates nothing."""
    out: dict[str, float] = {}
    if not beds or not _table_columns(db, "patient_movements"):
        return out
    types_marks, types = _in(VACATING_MOVEMENT_TYPES)
    marks, ids = _in(b["id"] for b in beds)
    for bed_id, jd in db.execute(
        f"""SELECT from_bed_id, MAX(julianday(moved_at)) FROM patient_movements
             WHERE from_bed_id IN ({marks}) AND movement_type IN ({types_marks})
               AND (to_bed_id IS NULL OR to_bed_id <> from_bed_id)
             GROUP BY from_bed_id""",
        ids + types,
    ).fetchall():
        if jd is not None:
            out[bed_id] = float(jd)
    codes = {_key(w["code"]): w for w in wards}
    labels = {(b["ward_id"], _key(b.get("bed_label") or "")): b["id"] for b in beds}
    ward_marks, ward_keys = _in(codes)
    for ward_text, bed_text, jd in db.execute(
        f"""SELECT LOWER(TRIM(from_ward)), LOWER(TRIM(from_bed)), MAX(julianday(moved_at))
              FROM patient_movements
             WHERE from_bed_id IS NULL AND movement_type IN ({types_marks})
               AND LOWER(TRIM(COALESCE(from_ward, ''))) IN ({ward_marks})
               AND NOT (LOWER(TRIM(COALESCE(to_ward, ''))) = LOWER(TRIM(COALESCE(from_ward, '')))
                        AND LOWER(TRIM(COALESCE(to_bed, ''))) = LOWER(TRIM(COALESCE(from_bed, ''))))
             GROUP BY 1, 2""",
        types + ward_keys,
    ).fetchall():
        ward = codes.get(ward_text or "")
        bed_id = labels.get((ward["id"], bed_text or "")) if ward else None
        if bed_id is not None and jd is not None:
            out[bed_id] = max(out.get(bed_id, 0.0), float(jd))
    return out


def _derive_status(
    bed: dict,
    ward: dict,
    *,
    occupied: bool,
    presence: str | None,
    reserved: bool,
    ready_jd: float | None,
    vacated_jd: float | None,
    state_jd: float | None,
) -> tuple[str, list[str], float | None]:
    """(status, markers, since julian day) in the precedence above, for a bed
    whose occupant identity is decided by the caller."""
    state = bed.get("state")
    markers: list[str] = []
    if occupied or reserved:
        if state == "out_of_service":
            markers.append(_OUT_OF_SERVICE_MARKER)
        if occupied:
            status = {"on_leave": "occupied_on_leave", "absconded": "occupied_absconded"}.get(
                presence or "", "occupied"
            )
            return status, markers, None
        return "reserved", markers, None
    if not bed.get("is_active"):
        return "switched_off", markers, None
    if not _cleaning_tracked(ward):
        if state == "out_of_service":
            return "out_of_service", markers, state_jd
        return "vacant_untracked", markers, None
    if state == "out_of_service":
        return "out_of_service", markers, state_jd
    last_ready = (
        ready_jd if ready_jd is not None else (state_jd if state == "vacant_ready" else None)
    )
    if state == "vacant_dirty":
        return "needs_cleaning", markers, state_jd
    if state == "occupied":
        # Housekeeping still says occupied, but nobody lies in the bed.
        return "needs_cleaning", markers, vacated_jd if vacated_jd is not None else state_jd
    if state == "cleaning_in_progress":
        return "cleaning", markers, state_jd
    if state == "vacant_ready":
        # julianday() keeps milliseconds, so a departure and a clean stamped
        # in the same millisecond compare equal. A tie reads as needing a
        # clean: when the order cannot be told, the bed is not offered as
        # clean.
        if vacated_jd is not None and (last_ready is None or vacated_jd >= last_ready):
            return "needs_cleaning", markers, vacated_jd
        return "available", markers, state_jd
    return "needs_cleaning", markers, state_jd


def _scope_filter(
    db: Any,
    patient_ids: Iterable[str],
    dept: str | None,
    facilities: frozenset[int] | None = None,
) -> set[str]:
    """The ids among `patient_ids` inside `dept` (all of them for None).

    `facilities` (multisite-8h1) is the caller's facility scope: with one,
    an id is kept only when the patient's latest encounter is at one of
    those facilities as well (permissions.scope_patient_ids_sql)."""
    ids = tuple({p for p in patient_ids if p})
    if not ids:
        return set()
    if facilities is not None:
        from permissions import scope_patient_ids_sql

        scope_sql, scope_params = scope_patient_ids_sql(dept, facilities=facilities)
        marks, params = _in(ids)
        return {
            r[0]
            for r in db.execute(
                f"SELECT p.id FROM patients p WHERE p.id IN ({marks}) AND p.id IN ({scope_sql})",
                params + tuple(scope_params),
            ).fetchall()
        }
    if dept is None:
        return set(ids)
    from permissions import PATIENT_DEPARTMENT_SQL

    marks, params = _in(ids)
    return {
        r[0]
        for r in db.execute(
            f"SELECT p.id FROM patients p WHERE p.id IN ({marks}) AND {PATIENT_DEPARTMENT_SQL} = ?",
            params + (dept,),
        ).fetchall()
    }


def _board_on(
    db: Any,
    *,
    wards: list[dict],
    dept: str | None,
    identity: bool,
    reservations: bool = True,
    facilities: frozenset[int] | None = None,
) -> dict:
    """Tiles for `wards` on the caller's connection. With identity False no
    patient field is ever read or returned (the available-beds read).
    `facilities` (multisite-8h1) is the caller's facility scope: an occupant
    is named only inside it as well as inside `dept` (_scope_filter).

    With reservations False a bed a reservation holds reads the status under
    the hold (services/adt/core.bed_readiness_on uses it so the patient a bed
    is held for still meets the cleaning rule). A held tile (adt-12) says what
    it is held for ('Reserved for transfer') and carries `reservation` {id,
    purpose, purpose_words, reserved_at, expires_at, patient, patient_hidden};
    the patient it is held for is named only inside the caller's scope.
    An occupant inside scope carries presence_since (adt-10) and, off the
    ward too long, an advisory marker (_presence_prompt)."""
    from services.adt import reservations as adt_res
    from services.adt.presence import absconded_at_on

    now = datetime.now(UTC)
    now_jd = _jd(now)
    assert now_jd is not None
    ward_ids = [w["id"] for w in wards]
    beds: list[dict] = []
    if ward_ids:
        marks, ids = _in(ward_ids)
        beds = [
            dict(r)
            for r in db.execute(
                f"""SELECT u.id, u.unit_code, u.ward_id, u.room_id, u.room, u.bed_label, u.bed_class,
                       u.is_isolation_room, u.is_temporary, u.is_active, u.retired_at, u.state,
                       u.state_since, r.code AS room_code, r.name AS room_name,
                       COALESCE(r.is_isolation, 0) AS room_is_isolation
                  FROM housekeeping_units u LEFT JOIN ward_rooms r ON r.id = u.room_id
                 WHERE u.ward_id IN ({marks})""",
                ids,
            ).fetchall()
        ]
    occupants = _occupants_on(db, wards, beds)
    held = adt_res.live_on_beds(db, (b["id"] for b in beds), now=now) if reservations else {}
    reserved = set(held)
    ready = _last_ready_jd(db, beds)
    vacated = _last_vacated_jd(db, wards, beds)
    in_scope: set[str] = set()
    absconded_at: dict[str, datetime] = {}
    if identity:
        in_scope = _scope_filter(
            db,
            [a["patient_id"] for who in occupants.values() for a in who]
            + [r["patient_id"] for r in held.values() if r.get("patient_id")],
            dept,
            facilities,
        )
        absconded_at = absconded_at_on(
            db,
            [
                a["id"]
                for who in occupants.values()
                for a in who
                if a.get("presence_status") == "absconded"
            ],
        )
    names: dict[str, dict] = {}
    assessment_due: set = set()
    if in_scope:
        marks, ids = _in(in_scope)
        names = {
            r["id"]: dict(r)
            for r in db.execute(
                f"SELECT id, name, mrn FROM patients WHERE id IN ({marks})", ids
            ).fetchall()
        }
        # adt-17b (ruling ADT-S5 (a)): a ward clerk's admission whose admission
        # assessment no doctor has recorded yet, flagged on its bed inside the
        # caller's scope only (services/adt/clerk.py).
        from services.adt.clerk import assessment_due_ids_on

        assessment_due = assessment_due_ids_on(
            db, [a["id"] for who in occupants.values() for a in who if a["patient_id"] in in_scope]
        )

    by_ward = {w["id"]: w for w in wards}
    tiles_by_ward: dict[str, list[dict]] = {w["id"]: [] for w in wards}
    shown: list[str] = []
    for bed in sorted(beds, key=lambda b: (b["ward_id"], _natural_key(b.get("bed_label") or ""))):
        ward = by_ward[bed["ward_id"]]
        who = sorted(occupants.get(bed["id"], []), key=lambda a: (not a["linked"], a["id"]))
        if bed.get("retired_at") and not who:
            continue
        holder = who[0] if who else None
        status, markers, since_jd = _derive_status(
            bed,
            ward,
            occupied=holder is not None,
            presence=holder.get("presence_status") if holder else None,
            reserved=bed["id"] in reserved,
            ready_jd=ready.get(bed["id"]),
            vacated_jd=vacated.get(bed["id"]),
            state_jd=_jd(bed.get("state_since")),
        )
        occupant = None
        hidden = False
        if holder is not None:
            boarder = (
                bool(ward.get("department"))
                and (holder.get("department") or "").casefold()
                != (ward.get("department") or "").casefold()
            )
            if holder["patient_id"] in in_scope:
                person = names.get(holder["patient_id"], {})
                occupant = {
                    "admission_id": holder["id"],
                    "patient_id": holder["patient_id"],
                    "name": person.get("name"),
                    "mrn": person.get("mrn"),
                    "department": holder.get("department"),
                    "boarder": boarder,
                    "boarder_from": holder.get("department") if boarder else None,
                    "presence_status": holder.get("presence_status"),
                    # adt-10: when the patient was last on the ward (UTC), for
                    # 'Not on ward since'; None while they are on it.
                    "presence_since": holder.get("presence_since"),
                    "linked": holder["linked"],
                }
                if holder["patient_id"] not in shown:
                    shown.append(holder["patient_id"])
                since_jd = _jd(holder.get("admitted_at") or holder.get("created_at"))
                prompt = _presence_prompt(
                    holder, absconded_at=absconded_at.get(holder["id"]), now=now
                )
                if prompt:
                    markers.append(prompt)
                if holder["id"] in assessment_due:
                    from services.adt.clerk import ASSESSMENT_DUE_MARKER

                    markers.append(ASSESSMENT_DUE_MARKER)
            else:
                hidden = True
                # Nothing about a patient outside the caller's scope: not the
                # name, not the stay, not whether they are on leave.
                status = "boarder" if boarder else "occupied"
            if not holder["linked"]:
                markers.append("Not linked to bed setup")
            if len(who) > 1:
                markers.append(f"{len(who)} admissions are recorded in this bed")
            if bed.get("retired_at"):
                markers.append("Retired bed")
        words = _board_words(status)
        reservation = None
        if status == "reserved" and bed["id"] in held:
            res = held[bed["id"]]
            # What the bed is held for, never who, outside the caller's scope.
            words = adt_res.PURPOSE_WORDS.get(res.get("purpose") or "", words)
            since_jd = _jd(res.get("reserved_at"))
            # The hold hides the cleaning status, so say it as a marker: a
            # held bed may still be waiting for its clean.
            under, _, _ = _derive_status(
                bed,
                ward,
                occupied=False,
                presence=None,
                reserved=False,
                ready_jd=ready.get(bed["id"]),
                vacated_jd=vacated.get(bed["id"]),
                state_jd=_jd(bed.get("state_since")),
            )
            if under in ("needs_cleaning", "cleaning"):
                markers.append(_board_words(under))
            patient = None
            if identity and res.get("patient_id") in in_scope:
                person = names.get(res["patient_id"], {})
                patient = {
                    "patient_id": res["patient_id"],
                    "admission_id": res.get("admission_id"),
                    "name": person.get("name"),
                    "mrn": person.get("mrn"),
                }
                if res["patient_id"] not in shown:
                    shown.append(res["patient_id"])
            reservation = {
                "id": res["id"],
                "purpose": res.get("purpose"),
                "purpose_words": adt_res.PURPOSE_WORDS.get(res.get("purpose") or "", "Reserved"),
                "reserved_at": res.get("reserved_at"),
                "expires_at": res.get("expires_at"),
                "patient": patient,
                "patient_hidden": bool(res.get("patient_id")) and patient is None,
            }
        if markers and markers[0] == _OUT_OF_SERVICE_MARKER and status != "out_of_service":
            words = f"{words}, bed reported out of service"
        isolation = bool(bed.get("is_isolation_room")) or bool(bed.get("room_is_isolation"))
        tiles_by_ward[bed["ward_id"]].append(
            {
                "bed_id": bed["id"],
                "unit_code": bed["unit_code"],
                "bed_label": bed.get("bed_label"),
                "ward_id": bed["ward_id"],
                "room_id": bed.get("room_id"),
                "room": bed.get("room_code") or bed.get("room"),
                "room_name": bed.get("room_name"),
                "bed_class": bed.get("bed_class"),
                "isolation": isolation,
                "temporary": bool(bed.get("is_temporary")),
                "status": status,
                "status_words": words,
                "tone": _board_tone(status),
                "markers": markers,
                "free": status in FREE_STATUSES,
                "housekeeping_state": bed.get("state") if _cleaning_tracked(ward) else None,
                "since": _jd_text(since_jd),
                "minutes_in_state": _minutes_since(since_jd, now_jd),
                "occupant": occupant,
                "occupant_hidden": hidden,
                "reservation": reservation,
            }
        )

    out_wards = []
    for ward in wards:
        tiles = tiles_by_ward[ward["id"]]
        counts: dict[str, int] = {"beds": len(tiles)}
        for tile in tiles:
            counts[tile["status"]] = counts.get(tile["status"], 0) + 1
        counts["free"] = sum(1 for t in tiles if t["free"])
        counts["occupied_total"] = sum(1 for t in tiles if t["status"] in OCCUPIED_STATUSES)
        out_wards.append(
            {
                "id": ward["id"],
                "code": ward["code"],
                "name": ward.get("name"),
                "department": ward.get("department"),
                "ward_type": ward.get("ward_type"),
                "is_active": bool(ward.get("is_active")),
                "cleaning_tracked": _cleaning_tracked(ward),
                "register_complete": bool(ward.get("register_complete")),
                "counts": counts,
                "tiles": tiles,
            }
        )
    return {"wards": out_wards, "patient_ids": shown}


def _ward_choice(ward: dict) -> dict:
    return {
        "id": ward["id"],
        "code": ward["code"],
        "name": ward.get("name"),
        "department": ward.get("department"),
        "is_active": bool(ward.get("is_active")),
    }


def _phi_read_audit(
    db,
    actor: dict,
    action: str,
    summary: str,
    *,
    patient_ids: list[str],
    target_type: str,
    target_id: str,
    request_meta: dict | None,
    metadata: dict | None = None,
) -> None:
    """One fail-closed phi_access line naming the patients a read showed,
    inside the read's own session."""
    meta = request_meta or {}
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="phi_access",
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type=target_type,
        target_id=str(target_id),
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata={
            "patient_ids": patient_ids[:300],
            "patient_count": len(patient_ids),
            **(metadata or {}),
        },
    )


def bed_board(*, actor: dict, ward_id: str | None = None, request_meta: dict | None = None) -> dict:
    """The bed board: every bed on the caller's wards (or on one of them) with
    its derived status, minutes in that status, isolation and temporary
    markers, the bed class, and the occupant when the caller may see them.
    Counts per ward. A ward the caller may not see is 404. With more than one
    facility on record (multisite-8h1) the caller's facility's wards only,
    and an occupant named only inside the caller's facility scope."""
    from permissions import department_scope

    dept = department_scope(actor)
    facilities = _actor_facilities(actor)
    hk.ensure_housekeeping_schema()
    with db_session() as db:
        visible = _visible_wards(db, dept, facilities)
        if ward_id:
            chosen = [w for w in visible if w["id"] == ward_id]
            if not chosen:
                raise BedMasterNotFound(
                    "No ward with that id on your bed board", code="ward_not_found"
                )
        else:
            chosen = [w for w in visible if w.get("is_active")]
        board = _board_on(db, wards=chosen, dept=dept, identity=True, facilities=facilities)
        # A switched-off ward with nobody in it has nothing to show.
        if not ward_id:
            board["wards"] = [
                w for w in board["wards"] if w["is_active"] or w["counts"].get("occupied_total")
            ]
        if board["patient_ids"]:
            _phi_read_audit(
                db,
                actor,
                "bed_board_viewed",
                f"Bed board viewed: {len(board['patient_ids'])} patients named",
                patient_ids=board["patient_ids"],
                target_type="ward" if ward_id else "bed_board",
                target_id=ward_id or "all",
                request_meta=request_meta,
                metadata={"department": dept, "ward_ids": [w["id"] for w in board["wards"]]},
            )
    return {
        "generated_at": utc_iso_now(),
        "department": dept,
        "ward_id": ward_id,
        "ward_choices": [_ward_choice(w) for w in visible if w.get("is_active")],
        "wards": board["wards"],
        "status_words": {k: v[0] for k, v in BOARD_STATUSES.items()},
    }


def available_beds(*, actor: dict, ward_id: str | None = None) -> dict:
    """Beds a patient could be placed in now, for a picker: status available,
    or vacant on a ward whose cleaning is not tracked. Beds waiting for or
    under a clean are listed apart, with whether the clean is a terminal one,
    so a picker can say why. No patient data: nothing here is audited. With
    more than one facility on record (multisite-8h1) the caller's facility's
    wards only."""
    from services.facility_lanes import wards_in_scope

    hk.ensure_housekeeping_schema()
    facilities = _actor_facilities(actor)
    with db_session() as db:
        wards = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM wards WHERE is_active = 1"
                + (" AND id = ?" if ward_id else "")
                + " ORDER BY LOWER(code)",
                (ward_id,) if ward_id else (),
            ).fetchall()
        ]
        wards = wards_in_scope(wards, facilities)
        if ward_id and not wards:
            raise BedMasterNotFound("No ward with that id is set up", code="ward_not_found")
        board = _board_on(db, wards=wards, dept=None, identity=False)
        terminal: set[str] = set()
        bed_ids = [t["bed_id"] for w in board["wards"] for t in w["tiles"]]
        if bed_ids:
            marks, ids = _in(bed_ids)
            terminal = {
                r[0]
                for r in db.execute(
                    f"""SELECT DISTINCT unit_id FROM housekeeping_tasks
                     WHERE status IN ('open', 'in_progress') AND clean_type = 'terminal'
                       AND unit_id IN ({marks})""",
                    ids,
                ).fetchall()
            }

    def _entry(ward: dict, tile: dict) -> dict:
        return {
            "bed_id": tile["bed_id"],
            "unit_code": tile["unit_code"],
            "bed_label": tile["bed_label"],
            "ward_id": ward["id"],
            "ward_code": ward["code"],
            "ward_department": ward["department"],
            "room": tile["room"],
            "bed_class": tile["bed_class"],
            "isolation": tile["isolation"],
            "temporary": tile["temporary"],
            "status": tile["status"],
            "status_words": tile["status_words"],
            "markers": tile["markers"],
            "terminal_clean": tile["bed_id"] in terminal,
        }

    free: list[dict] = []
    not_ready: list[dict] = []
    for ward in board["wards"]:
        for tile in ward["tiles"]:
            if tile["free"]:
                free.append(_entry(ward, tile))
            elif tile["status"] in ("needs_cleaning", "cleaning"):
                not_ready.append(_entry(ward, tile))
    return {
        "ward_id": ward_id,
        "beds": free,
        "not_ready": not_ready,
        "status_words": {k: v[0] for k, v in BOARD_STATUSES.items()},
    }


def awaiting_bed(
    *, actor: dict, ward_id: str | None = None, request_meta: dict | None = None
) -> dict:
    """Active admissions in the caller's scope with no master bed, as two
    lists that are never merged:

      awaiting  no bed text and no bed_id: nobody has said where they lie.
      unlinked  bed text with no bed_id: they lie somewhere a person typed,
                not yet linked to bed setup. Each says why (no such ward or
                bed in setup, a retired bed, a bed claimed by more than one
                admission, or 'matches' a bed a bed manager can link).

    Names and MRNs are shown, so a read that lists anybody writes one
    fail-closed phi_access audit with the patient ids. With more than one
    facility on record (multisite-8h1) the caller's facility's only."""
    from permissions import department_scope

    dept = department_scope(actor)
    facilities = _actor_facilities(actor)
    hk.ensure_housekeeping_schema()
    with db_session() as db:
        report = _reconcile_on(db, ward_id=ward_id, dept=dept, facilities=facilities)
        unlinked = [dict(e) for e in report["unlinked"]]
        unlinked += [dict(e, reason="matches_bed") for e in report["matched"]]
        for doubled in report["doubled"]:
            for e in doubled["admissions"]:
                if e.get("bed_id") is None:
                    unlinked.append(dict(e, reason="doubled_bed"))
        awaiting = [dict(e) for e in report["awaiting"]]
        everyone = [e["patient_id"] for e in awaiting + unlinked]
        people: dict[str, dict] = {}
        stays: dict[str, dict] = {}
        if everyone:
            marks, ids = _in(set(everyone))
            people = {
                r["id"]: dict(r)
                for r in db.execute(
                    f"SELECT id, name, mrn FROM patients WHERE id IN ({marks})", ids
                ).fetchall()
            }
            adm_marks, adm_ids = _in({e["admission_id"] for e in awaiting + unlinked})
            stays = {
                r["id"]: dict(r)
                for r in db.execute(
                    f"SELECT id, admitted_at, created_at FROM admissions WHERE id IN ({adm_marks})",
                    adm_ids,
                ).fetchall()
            }
        reason_words = {
            "awaiting_bed": "No bed recorded yet",
            "ward_not_in_setup": "The typed ward is not in bed setup",
            "bed_not_in_setup": "The typed bed is not in bed setup",
            "bed_retired": "The typed bed is retired in bed setup",
            "doubled_bed": "Another admission is recorded in the same bed",
            "matches_bed": "Matches a bed in setup; a bed manager can link it",
        }
        for entry in awaiting + unlinked:
            person = people.get(entry["patient_id"], {})
            stay = stays.get(entry["admission_id"], {})
            entry["name"] = person.get("name")
            entry["mrn"] = person.get("mrn")
            entry["since"] = stay.get("admitted_at") or stay.get("created_at")
            entry["reason_words"] = reason_words.get(entry.get("reason") or "", None)
        shown = list(dict.fromkeys(everyone))
        if shown:
            _phi_read_audit(
                db,
                actor,
                "awaiting_bed_viewed",
                f"Awaiting a bed and unlinked admissions viewed: {len(shown)} patients",
                patient_ids=shown,
                target_type="ward" if ward_id else "bed_board",
                target_id=ward_id or "all",
                request_meta=request_meta,
                metadata={"department": dept, "awaiting": len(awaiting), "unlinked": len(unlinked)},
            )
    return {
        "ward_id": ward_id,
        "awaiting": awaiting,
        "unlinked": unlinked,
        "counts": {"awaiting": len(awaiting), "unlinked": len(unlinked)},
    }
