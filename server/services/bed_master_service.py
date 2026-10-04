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
    record). Change the display name instead, or set up a new one. The full
    rename and retire guards over admissions.ward_id and bed_id arrive with
    adt-3; until then the text mirrors are what is checked.
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
  * Beds already typed into the housekeeping register on this ward with the
    same label are never duplicated. Registering one is refused with words
    unless the bed manager asks to adopt them, which links the existing row and
    keeps its cleaning state, ledger and open tasks.

NO PATIENT DATA. Nothing here reads or returns a patient. Reference checks
count admissions and patients by ward and bed text and return counts only.
Master edits are audited as category 'facilities' (design section 9) inside
the same transaction, with a bed_master_events row beside each.

Every function is plain sync and opens its own sessions; routes call it from
plain def handlers.
"""
from __future__ import annotations

import json
import logging
import re
import string
import uuid
from typing import Any, Iterable, Optional

from database import db_session
from db_backend import DBIntegrityError
from migrations_his.m0053_adt_bed_master import FACILITY_CODE, ROOM_TYPES, WARD_TYPES
from services import housekeeping_service as hk
from services.audit_service import log_audit_event
from services.clinical_time import utc_iso_now

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
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.casefold())
            for t in re.split(r"(\d+)", label)]


def _norm_code(value: Any, what: str, *, limit: int = _MAX_CODE) -> str:
    """A ward code, room code, bed label or bed class code: spaces tidied,
    required, bounded, and free of '/' and control characters."""
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        raise BedMasterError(f"{what} is required", code="required", field=what)
    if len(text) > limit:
        raise BedMasterError(f"{what} is longer than {limit} characters", code="too_long", field=what)
    if "/" in text:
        raise BedMasterError(
            f"{what} cannot contain '/'. A bed is shown as ward/bed, so a '/' inside a "
            "code would make two different beds read the same.",
            code="slash_not_allowed", field=what,
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise BedMasterError(f"{what} contains a character that cannot be printed", code="bad_character", field=what)
    return text


def _clean(value: Any, limit: int = _MAX_NAME) -> Optional[str]:
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


def _event(db, entity_type: str, entity_id: str, action: str, detail: dict | None,
           actor: dict, now: str, reason: str | None = None) -> None:
    db.execute(
        """INSERT INTO bed_master_events
               (entity_type, entity_id, action, detail_json, reason, actor_user_id, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            entity_type, str(entity_id), action,
            json.dumps(detail, sort_keys=True, default=str) if detail else None,
            _clean(reason, _MAX_NOTE), (actor or {}).get("id"), now,
        ),
    )


def _audit(db, actor: dict, action: str, summary: str, *, target_type: str,
           target_id: str, request_meta: dict | None, metadata: dict | None = None) -> None:
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


# ═════════════════════════════════════════════════════════════════════════════
#  Departments and cover
# ═════════════════════════════════════════════════════════════════════════════

def _department_lists() -> tuple[list[str], list[str]]:
    """(covering, declared). Covering: departments an active doctor or nurse
    account holds, the anti-orphaning list (frontdesk_service). Declared: the
    site configuration's departments. Each opens its own session or none, so
    this is called BEFORE any write session."""
    from services.frontdesk_service import list_covering_departments
    from services.site_config_service import current_site_config

    covering = list_covering_departments()
    declared: list[str] = []
    for dept in current_site_config().departments:
        name = " ".join(str(dept.get("name") or "").split())
        if name and name not in declared:
            declared.append(name)
    return covering, declared


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
        code="unknown_department", field="department",
    )


def _cover_note(department: str, covering: list[str]) -> Optional[str]:
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
            db, "SELECT COUNT(*) FROM housekeeping_units WHERE ward_id IS NULL "
                "AND LOWER(TRIM(ward)) = LOWER(TRIM(?))", (code,)),
        "admissions": _count(
            db, "SELECT COUNT(*) FROM admissions WHERE LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?))",
            (code,)),
        "patients": _count(
            db, "SELECT COUNT(*) FROM patients WHERE LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?))",
            (code,)),
    }
    if "ward_id" in _table_columns(db, "admissions"):
        refs["admissions"] += _count(db, "SELECT COUNT(*) FROM admissions WHERE ward_id = ?", (wid,))
    movement_cols = _table_columns(db, "patient_movements")
    if movement_cols:
        refs["movements"] = _count(
            db, "SELECT COUNT(*) FROM patient_movements WHERE LOWER(TRIM(COALESCE(from_ward, ''))) "
                "= LOWER(TRIM(?)) OR LOWER(TRIM(COALESCE(to_ward, ''))) = LOWER(TRIM(?))",
            (code, code))
        for col in ("from_ward_id", "to_ward_id"):
            if col in movement_cols:
                refs["movements"] += _count(
                    db, f"SELECT COUNT(*) FROM patient_movements WHERE {col} = ?", (wid,))
    return {k: v for k, v in refs.items() if v}


def _bed_references(db, unit: dict, ward_code: str) -> dict[str, int]:
    """Everything that refers to this bed, as counts. Any of them makes the
    label immutable: its cleaning record beyond registration, its cleaning
    tasks, admissions, patients and movements that carry the ward and bed
    text, and the bed id columns later migrations add."""
    uid, label = unit["id"], unit["bed_label"] or ""
    refs = {
        "cleaning_record": max(0, _count(
            db, "SELECT COUNT(*) FROM housekeeping_unit_events WHERE unit_id = ?", (uid,)) - 1),
        "cleaning_tasks": _count(db, "SELECT COUNT(*) FROM housekeeping_tasks WHERE unit_id = ?", (uid,)),
        "admissions": _count(
            db, "SELECT COUNT(*) FROM admissions WHERE LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?)) "
                "AND LOWER(TRIM(COALESCE(bed, ''))) = LOWER(TRIM(?))", (ward_code, label)),
        "patients": _count(
            db, "SELECT COUNT(*) FROM patients WHERE LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?)) "
                "AND LOWER(TRIM(COALESCE(bed, ''))) = LOWER(TRIM(?))", (ward_code, label)),
    }
    if "bed_id" in _table_columns(db, "admissions"):
        refs["admissions"] += _count(db, "SELECT COUNT(*) FROM admissions WHERE bed_id = ?", (uid,))
    movement_cols = _table_columns(db, "patient_movements")
    if movement_cols:
        refs["movements"] = _count(
            db, "SELECT COUNT(*) FROM patient_movements WHERE "
                "(LOWER(TRIM(COALESCE(from_ward, ''))) = LOWER(TRIM(?)) "
                " AND LOWER(TRIM(COALESCE(from_bed, ''))) = LOWER(TRIM(?))) OR "
                "(LOWER(TRIM(COALESCE(to_ward, ''))) = LOWER(TRIM(?)) "
                " AND LOWER(TRIM(COALESCE(to_bed, ''))) = LOWER(TRIM(?)))",
            (ward_code, label, ward_code, label))
        for col in ("from_bed_id", "to_bed_id"):
            if col in movement_cols:
                refs["movements"] += _count(
                    db, f"SELECT COUNT(*) FROM patient_movements WHERE {col} = ?", (uid,))
    return {k: v for k, v in refs.items() if v}


_REFERENCE_WORDS = {
    "beds": "beds", "rooms": "rooms", "housekeeping": "housekeeping register entries",
    "admissions": "admissions", "patients": "patient records", "movements": "patient movements",
    "cleaning_record": "cleaning records", "cleaning_tasks": "cleaning requests",
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


def _bed_class_code(db, value: Any, *, require_active: bool = True) -> Optional[str]:
    """The stored spelling of a bed class, or None for a blank value."""
    text = _clean(value, _MAX_CODE)
    if text is None:
        return None
    row = db.execute(
        "SELECT code, is_active FROM bed_classes WHERE LOWER(TRIM(code)) = LOWER(TRIM(?))", (text,)
    ).fetchone()
    if row is None:
        raise BedMasterError(f"There is no bed class {text}. Set it up under bed classes first.",
                             code="bed_class_not_found", field="bed_class")
    if require_active and not row["is_active"]:
        raise BedMasterError(f"Bed class {row['code']} is switched off", code="bed_class_inactive",
                             field="bed_class")
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
            code="duplicate_code", existing_id=row["id"],
        )


# ═════════════════════════════════════════════════════════════════════════════
#  Read
# ═════════════════════════════════════════════════════════════════════════════

def list_master(*, ward_id: str | None = None) -> dict:
    """The whole master for the Bed setup screen: wards (with cover), rooms,
    beds, bed classes, the department choices, and beds still only in the
    housekeeping register (candidates to adopt). No patient data."""
    hk.ensure_housekeeping_schema()
    covering, declared = _department_lists()
    ward_filter = " WHERE id = ?" if ward_id else ""
    by_ward = " AND ward_id = ?" if ward_id else ""
    params: tuple = (ward_id,) if ward_id else ()
    with db_session() as db:
        wards = [_ward_out(r, covering) for r in db.execute(
            f"SELECT * FROM wards{ward_filter} ORDER BY LOWER(code)", params).fetchall()]
        rooms = [_bool(dict(r), "is_isolation", "is_active") for r in db.execute(
            f"SELECT * FROM ward_rooms WHERE 1 = 1{by_ward} ORDER BY ward_id, LOWER(code)",
            params).fetchall()]
        beds = [_bool(dict(r), "is_isolation_room", "is_temporary", "is_active") for r in db.execute(
            f"""SELECT id, unit_code, ward_id, ward, room_id, room, bed_label, bed_class,
                       is_isolation_room, is_temporary, is_active, retired_at, state,
                       state_since, notes, created_at, updated_at
                  FROM housekeeping_units WHERE ward_id IS NOT NULL{by_ward}""", params).fetchall()]
        beds.sort(key=lambda b: (b["ward_id"], _natural_key(b.get("bed_label") or "")))
        has_items = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'service_items'"
        ).fetchone() is not None
        listed_sql = ("(SELECT COUNT(*) FROM service_items s WHERE s.code = b.bed_day_service_code)"
                      if has_items else "NULL")
        bed_classes = [_bool(dict(r), "is_critical_care", "counts_in_census", "is_active") for r in db.execute(
            f"SELECT b.*, {listed_sql} AS bed_day_charge_listed FROM bed_classes b ORDER BY LOWER(b.code)"
        ).fetchall()]
        legacy = [dict(r) for r in db.execute(
            """SELECT id, unit_code, ward, room, bed_label, state
                 FROM housekeeping_units
                WHERE ward_id IS NULL AND unit_type = 'bed' AND is_active = 1
                ORDER BY ward, unit_code"""
        ).fetchall()]

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

_WARD_FIELDS = ("code", "name", "department", "ward_type", "default_bed_class",
                "cleaning_tracked", "is_active")


def save_ward(data: dict, *, actor: dict, ward_id: str | None = None,
              request_meta: dict | None = None) -> dict:
    """Create a ward (ward_id None) or change the fields `data` carries.

    The department is resolved before the session (covering, else declared,
    else refused). A ward code is refused if another ward holds it in any
    capitalisation, and cannot change once anything refers to the ward. A ward
    with active beds cannot be switched off (retire its beds first, adt-3)."""
    data = dict(data or {})
    reason = data.pop("reason", None)
    hk.ensure_housekeeping_schema()
    covering, declared = _department_lists()
    department = (_resolve_department(data["department"], covering, declared)
                  if "department" in data else None)
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
                new_id = _new_id("WRD")
                db.execute(
                    """INSERT INTO wards
                           (id, facility_code, code, name, department, ward_type,
                            default_bed_class, cleaning_tracked, is_active, created_by,
                            created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (new_id, FACILITY_CODE, code, name, department, ward_type, bed_class,
                     1 if tracked else 0, (actor or {}).get("id"), now, now),
                )
                detail = {"code": code, "name": name, "department": department,
                          "ward_type": ward_type, "default_bed_class": bed_class,
                          "cleaning_tracked": tracked}
                _event(db, "ward", new_id, "created", detail, actor, now, reason)
                _audit(db, actor, "bed_master_ward_created", f"Ward {code} set up in {department}",
                       target_type="ward", target_id=new_id, request_meta=request_meta,
                       metadata=detail)
                row = db.execute("SELECT * FROM wards WHERE id = ?", (new_id,)).fetchone()
        except DBIntegrityError as exc:
            raise BedMasterConflict(f"Ward {code} could not be saved because it clashes with "
                                    "one already set up. Nothing was changed.",
                                    code="duplicate_code") from exc
        return _ward_out(row, covering)

    changes: dict[str, list] = {}
    try:
        with db_session(immediate=True) as db:
            ward = _ward_row(db, ward_id)
            new: dict[str, Any] = {}
            if "code" in data and data["code"] is not None:
                code = _norm_code(data["code"], "Ward code")
                if code != ward["code"]:
                    refs = _ward_references(db, ward)
                    if refs:
                        raise BedMasterConflict(
                            f"The ward code {ward['code']} is already in use "
                            f"({_references_words(refs)}), so it cannot be changed. Change "
                            "the display name instead, or set up a new ward.",
                            code="code_in_use", references=refs,
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
                    beds = _count(db, "SELECT COUNT(*) FROM housekeeping_units WHERE ward_id = ? "
                                      "AND is_active = 1 AND retired_at IS NULL", (ward_id,))
                    held = _count(db, "SELECT COUNT(*) FROM admissions WHERE status = 'active' "
                                      "AND LOWER(TRIM(COALESCE(ward, ''))) = LOWER(TRIM(?))",
                                  (ward["code"],))
                    if beds or held:
                        raise BedMasterConflict(
                            f"Ward {ward['code']} still has {beds} beds and {held} patients "
                            "admitted to it, so it cannot be switched off.",
                            code="ward_in_use", beds=beds, admitted=held,
                        )
                new["is_active"] = active
            changes = {k: [ward.get(k), v] for k, v in new.items() if ward.get(k) != v}
            if changes:
                cols = ", ".join(f"{k} = ?" for k in changes)
                db.execute(f"UPDATE wards SET {cols}, updated_at = ? WHERE id = ?",
                           (*[v[1] for v in changes.values()], now, ward_id))
                action = "updated"
                if set(changes) == {"is_active"}:
                    action = "switched_on" if changes["is_active"][1] else "switched_off"
                _event(db, "ward", ward_id, action, {"changes": changes}, actor, now, reason)
                _audit(db, actor, "bed_master_ward_updated",
                       f"Ward {ward['code']} changed: {', '.join(sorted(changes))}",
                       target_type="ward", target_id=ward_id, request_meta=request_meta,
                       metadata={"changes": changes})
            row = db.execute("SELECT * FROM wards WHERE id = ?", (ward_id,)).fetchone()
    except DBIntegrityError as exc:
        raise BedMasterConflict("That change clashes with a ward already set up. Nothing was "
                                "changed.", code="duplicate_code") from exc
    return _ward_out(row, covering)


# ═════════════════════════════════════════════════════════════════════════════
#  Rooms
# ═════════════════════════════════════════════════════════════════════════════

def save_room(data: dict, *, actor: dict, room_id: str | None = None,
              request_meta: dict | None = None) -> dict:
    """Create a room on a ward, or change one. A room code is unique inside its
    ward without regard to capital letters and immutable once a bed is in the
    room. Changing whether a room is an isolation room changes its beds too,
    because that is what makes housekeeping send a terminal clean. A change of
    room type to or from 'isolation' with no isolation flag in the request sets
    the flag as create would, and so reaches the beds."""
    data = dict(data or {})
    reason = data.pop("reason", None)
    hk.ensure_housekeeping_schema()
    now = utc_iso_now()
    try:
        with db_session(immediate=True) as db:
            if room_id is None:
                ward = _ward_row(db, str(data.get("ward_id") or ""))
                if not ward["is_active"]:
                    raise BedMasterConflict(f"Ward {ward['code']} is switched off", code="ward_inactive")
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
                detail = {"ward_id": ward["id"], "ward_code": ward["code"], "code": code,
                          "name": name, "room_type": room_type, "is_isolation": isolation}
                _event(db, "room", new_id, "created", detail, actor, now, reason)
                _audit(db, actor, "bed_master_room_created", f"Room {code} set up on {ward['code']}",
                       target_type="room", target_id=new_id, request_meta=request_meta,
                       metadata=detail)
                room_id = new_id
            else:
                room = _room_row(db, room_id)
                if data.get("ward_id") not in (None, room["ward_id"]):
                    raise BedMasterError("A room cannot move to another ward. Set up a room on "
                                         "that ward instead.", code="ward_fixed", field="ward_id")
                new: dict[str, Any] = {}
                if data.get("code") is not None:
                    code = _norm_code(data["code"], "Room code")
                    if code != room["code"]:
                        beds = _count(db, "SELECT COUNT(*) FROM housekeeping_units WHERE room_id = ?",
                                      (room_id,))
                        if beds:
                            raise BedMasterConflict(
                                f"Room {room['code']} has {beds} beds, so its code cannot be "
                                "changed. Change the display name instead.",
                                code="code_in_use", references={"beds": beds},
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
                        beds = _count(db, "SELECT COUNT(*) FROM housekeeping_units WHERE room_id = ? "
                                          "AND is_active = 1 AND retired_at IS NULL", (room_id,))
                        if beds:
                            raise BedMasterConflict(
                                f"Room {room['code']} still has {beds} beds, so it cannot be "
                                "switched off. Move or retire its beds first.",
                                code="room_in_use", beds=beds,
                            )
                    new["is_active"] = active
                changes = {k: [room.get(k), v] for k, v in new.items() if room.get(k) != v}
                if changes:
                    cols = ", ".join(f"{k} = ?" for k in changes)
                    db.execute(f"UPDATE ward_rooms SET {cols}, updated_at = ? WHERE id = ?",
                               (*[v[1] for v in changes.values()], now, room_id))
                    beds_changed: list[str] = []
                    if "is_isolation" in changes:
                        for unit in db.execute(
                            "SELECT id FROM housekeeping_units WHERE room_id = ? AND retired_at IS NULL",
                            (room_id,),
                        ).fetchall():
                            hk.update_master_bed_on(db, unit["id"],
                                                    is_isolation_room=bool(changes["is_isolation"][1]))
                            beds_changed.append(unit["id"])
                    detail = {"changes": changes}
                    if isolation_follows_type and "is_isolation" in changes:
                        detail["isolation_follows_room_type"] = True
                    if beds_changed:
                        detail["beds_isolation_changed"] = beds_changed
                    _event(db, "room", room_id, "updated", detail, actor, now, reason)
                    _audit(db, actor, "bed_master_room_updated",
                           f"Room {room['code']} changed: {', '.join(sorted(changes))}",
                           target_type="room", target_id=room_id, request_meta=request_meta,
                           metadata=detail)
            row = db.execute("SELECT * FROM ward_rooms WHERE id = ?", (room_id,)).fetchone()
    except DBIntegrityError as exc:
        raise BedMasterConflict("That room clashes with one already set up on this ward. "
                                "Nothing was changed.", code="duplicate_code") from exc
    except hk.HousekeepingError as exc:
        raise BedMasterConflict(f"{exc}. Nothing was changed.", code="housekeeping_refused") from exc
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
            code="duplicate_code", existing_id=row["id"],
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
        raise BedMasterError("Give either a list of bed labels or a range, not both",
                             code="labels_or_range")
    out: list[str] = []
    if rng:
        try:
            start, end = int(rng.get("start")), int(rng.get("end"))
            pad = int(rng.get("pad") or 0)
        except (TypeError, ValueError, AttributeError):
            raise BedMasterError("A range needs a whole-number start and end", code="bad_range")
        prefix = " ".join(str(rng.get("prefix") or "").split())
        if start < 0 or end < start:
            raise BedMasterError("A range must run from a start up to an end at or above it",
                                 code="bad_range")
        if not 0 <= pad <= 4:
            raise BedMasterError("Padding is between 0 and 4 digits", code="bad_range")
        if end - start + 1 > MAX_BEDS_PER_REGISTRATION:
            raise BedMasterError(
                f"At most {MAX_BEDS_PER_REGISTRATION} beds can be registered at once",
                code="too_many_beds")
        out = [f"{prefix}{str(n).zfill(pad)}" for n in range(start, end + 1)]
    elif labels:
        if not isinstance(labels, (list, tuple)):
            raise BedMasterError("Bed labels must be a list", code="bad_labels")
        out = [str(x) if x is not None else "" for x in labels]
    if not out:
        raise BedMasterError("Give at least one bed label", code="required", field="labels")
    if len(out) > MAX_BEDS_PER_REGISTRATION:
        raise BedMasterError(f"At most {MAX_BEDS_PER_REGISTRATION} beds can be registered at once",
                             code="too_many_beds")
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
            code="labels_repeated", labels=repeated,
        )

    registered: list[dict] = []
    adopted: list[dict] = []
    try:
        with db_session(immediate=True) as db:
            ward = _ward_row(db, str(data.get("ward_id") or ""))
            if not ward["is_active"]:
                raise BedMasterConflict(f"Ward {ward['code']} is switched off", code="ward_inactive")
            room = None
            if data.get("room_id"):
                room = _room_row(db, str(data["room_id"]))
                if room["ward_id"] != ward["id"]:
                    raise BedMasterError(f"That room is not on {ward['code']}", code="room_not_on_ward")
                if not room["is_active"]:
                    raise BedMasterConflict(f"Room {room['code']} is switched off", code="room_inactive")
            bed_class = (_bed_class_code(db, data["bed_class"]) if data.get("bed_class")
                         else ward.get("default_bed_class"))
            isolation = data.get("is_isolation_room")
            # An adopted bed keeps its own isolation flag unless the request or
            # its room says otherwise.
            adopt_isolation = (bool(isolation) if isolation is not None
                               else bool(room["is_isolation"]) if room else None)
            if isolation is None:
                isolation = bool(room["is_isolation"]) if room else False

            keys = [_key(x) for x in labels]
            marks = ",".join("?" for _ in keys)
            taken = [r["bed_label"] for r in db.execute(
                f"""SELECT bed_label FROM housekeeping_units
                     WHERE ward_id = ? AND LOWER(TRIM(bed_label)) IN ({marks})
                     ORDER BY bed_label""",
                (ward["id"], *keys),
            ).fetchall()]
            if taken:
                raise BedMasterConflict(
                    f"{ward['code']} already has beds {', '.join(taken)}. Nothing was saved.",
                    code="labels_taken", labels=taken,
                )

            codes = [hk.master_unit_code(ward["code"], x) for x in labels]
            code_marks = ",".join("?" for _ in codes)
            legacy = [dict(r) for r in db.execute(
                f"""SELECT id, unit_code, ward, bed_label, unit_type, state, is_active
                      FROM housekeeping_units
                     WHERE ward_id IS NULL
                       AND (unit_code IN ({code_marks})
                            OR (unit_type = 'bed'
                                AND LOWER(TRIM(ward)) = LOWER(TRIM(?))
                                AND LOWER(TRIM(COALESCE(bed_label, ''))) IN ({marks})))""",
                (*codes, ward["code"], *keys),
            ).fetchall()]
            # Which legacy row (if any) is this label's bed? Only a BED on THIS
            # ward with THIS label may be adopted (a switched-off one is
            # switched back on); a unit code that merely matches is a different
            # thing and is refused.
            adopt_for: dict[str, dict] = {}
            blocked: list[str] = []
            for unit in legacy:
                same_bed = (unit["unit_type"] == "bed"
                            and _key(unit["ward"] or "") == _key(ward["code"])
                            and _key(unit["bed_label"] or "") in seen)
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
                    code="unit_codes_taken", unit_codes=sorted(set(blocked)),
                )
            if adopt_for and not adopt:
                found = sorted(u["unit_code"] for u in adopt_for.values())
                raise BedMasterConflict(
                    f"{len(found)} of these beds are already on the housekeeping register "
                    f"({', '.join(found)}). Choose to adopt them to link them here and keep "
                    "their cleaning state, or use different labels. Nothing was saved.",
                    code="already_in_housekeeping", unit_codes=found,
                )

            for label in labels:
                legacy_unit = adopt_for.get(_key(label))
                if legacy_unit is not None:
                    unit = hk.adopt_unit_on(
                        db, legacy_unit["id"], ward_id=ward["id"], bed_label=label,
                        room_id=room["id"] if room else None, bed_class=bed_class,
                        is_isolation_room=adopt_isolation,
                    )
                    adopted.append(_bed_out(unit))
                    _event(db, "bed", unit["id"], "adopted", {
                        "unit_code": unit["unit_code"], "previous_unit_code": legacy_unit["unit_code"],
                        "ward_id": ward["id"], "ward_code": ward["code"], "label": label,
                        "room_id": unit["room_id"], "bed_class": unit["bed_class"],
                        "state": unit["state"],
                    }, actor, now, reason)
                else:
                    unit = hk.register_unit_on(
                        db, ward_id=ward["id"], room_id=room["id"] if room else None,
                        bed_label=label, bed_class=bed_class, is_isolation_room=bool(isolation),
                        notes=data.get("notes"), actor_user_id=(actor or {}).get("id"),
                        actor_role=(actor or {}).get("role"), create_only=True,
                    )
                    registered.append(_bed_out(unit))
                    _event(db, "bed", unit["id"], "registered", {
                        "unit_code": unit["unit_code"], "ward_id": ward["id"],
                        "ward_code": ward["code"], "label": label, "room_id": unit["room_id"],
                        "bed_class": unit["bed_class"], "is_isolation_room": bool(isolation),
                    }, actor, now, reason)
            _audit(
                db, actor, "bed_master_beds_registered",
                f"{len(registered)} beds registered and {len(adopted)} adopted on {ward['code']}",
                target_type="ward", target_id=ward["id"], request_meta=request_meta,
                metadata={"ward_code": ward["code"], "registered": len(registered),
                          "adopted": len(adopted), "labels": labels[:MAX_BEDS_PER_REGISTRATION],
                          "room_id": room["id"] if room else None, "bed_class": bed_class},
            )
    except hk.HousekeepingError as exc:
        raise BedMasterConflict(f"{exc} Nothing was saved.", code="housekeeping_refused") from exc
    except DBIntegrityError as exc:
        raise BedMasterConflict("One of these beds clashes with a unit already on the "
                                "housekeeping register. Nothing was saved.",
                                code="unit_codes_taken") from exc
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
    now = utc_iso_now()
    try:
        with db_session(immediate=True) as db:
            row = db.execute("SELECT * FROM housekeeping_units WHERE id = ? AND ward_id IS NOT NULL",
                             (bed_id,)).fetchone()
            if row is None:
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
                        raise BedMasterError(f"That room is not on {ward['code']}",
                                             code="room_not_on_ward")
                    if not room["is_active"]:
                        raise BedMasterConflict(f"Room {room['code']} is switched off",
                                                code="room_inactive")
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
                            code="label_in_use", references=refs,
                        )
                    clash = db.execute(
                        "SELECT bed_label FROM housekeeping_units WHERE ward_id = ? AND id <> ? "
                        "AND LOWER(TRIM(bed_label)) = LOWER(TRIM(?))",
                        (ward["id"], bed_id, label),
                    ).fetchone()
                    if clash is not None:
                        raise BedMasterConflict(f"{ward['code']} already has bed {clash['bed_label']}",
                                                code="labels_taken", labels=[clash["bed_label"]])
                    # The same legacy check register_beds makes: a bed typed into
                    # the housekeeping register on this ward with this label
                    # (switched off ones included), or any older unit already
                    # holding the new unit code. Relabelling onto it would leave
                    # two housekeeping rows for one physical bed, and the
                    # discharge turnaround could land on the stale one. A rename
                    # never adopts; register_beds with adopt_existing does that.
                    legacy = [dict(r) for r in db.execute(
                        """SELECT unit_code, ward, bed_label, unit_type
                             FROM housekeeping_units
                            WHERE ward_id IS NULL
                              AND (unit_code = ?
                                   OR (unit_type = 'bed'
                                       AND LOWER(TRIM(ward)) = LOWER(TRIM(?))
                                       AND LOWER(TRIM(COALESCE(bed_label, ''))) = ?))
                            ORDER BY unit_code""",
                        (hk.master_unit_code(ward["code"], label), ward["code"], _key(label)),
                    ).fetchall()]
                    if legacy:
                        same_bed = sorted(
                            u["unit_code"] for u in legacy
                            if u["unit_type"] == "bed"
                            and _key(u["ward"] or "") == _key(ward["code"])
                            and _key(u["bed_label"] or "") == _key(label))
                        if same_bed:
                            raise BedMasterConflict(
                                f"Bed {label} on {ward['code']} is already on the housekeeping "
                                f"register ({', '.join(same_bed)}), so this bed cannot take that "
                                "label. Adopt that bed in Bed setup, or choose a different "
                                "label. Nothing was changed.",
                                code="already_in_housekeeping", unit_codes=same_bed,
                            )
                        held = sorted(u["unit_code"] for u in legacy)
                        raise BedMasterConflict(
                            f"{', '.join(held)} is already on the housekeeping register as "
                            "something else. Choose a different label, or change that unit in "
                            "housekeeping. Nothing was changed.",
                            code="unit_codes_taken", unit_codes=held,
                        )
                    kwargs["bed_label"] = label
            after = hk.update_master_bed_on(db, bed_id, **kwargs)
            changes = {k: [unit.get(k), after.get(k)]
                       for k in ("room_id", "room", "bed_class", "is_isolation_room", "notes",
                                 "bed_label", "unit_code")
                       if unit.get(k) != after.get(k)}
            if changes:
                detail = {"changes": changes}
                if isolation_follows_room and "is_isolation_room" in changes:
                    detail["isolation_follows_room"] = True
                _event(db, "bed", bed_id, "updated", detail, actor, now, reason)
                _audit(db, actor, "bed_master_bed_updated",
                       f"Bed {after['unit_code']} changed: {', '.join(sorted(changes))}",
                       target_type="bed", target_id=bed_id, request_meta=request_meta,
                       metadata=detail)
    except hk.HousekeepingError as exc:
        raise BedMasterConflict(f"{exc}. Nothing was changed.", code="housekeeping_refused") from exc
    except DBIntegrityError as exc:
        raise BedMasterConflict("That label clashes with a unit already on the housekeeping "
                                "register. Nothing was changed.", code="unit_codes_taken") from exc
    return _bed_out(after)


# ═════════════════════════════════════════════════════════════════════════════
#  Bed classes
# ═════════════════════════════════════════════════════════════════════════════

def save_bed_class(data: dict, *, actor: dict, code: str | None = None,
                   request_meta: dict | None = None) -> dict:
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
                    raise BedMasterError("Bed class name is required", code="required", field="name")
                clash = db.execute("SELECT code FROM bed_classes WHERE LOWER(TRIM(code)) = LOWER(TRIM(?))",
                                   (new_code,)).fetchone()
                if clash is not None:
                    raise BedMasterConflict(
                        f"Bed class {clash['code']} is already set up. Codes are compared without "
                        "regard to capital letters.", code="duplicate_code")
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
                detail = {"code": new_code, "name": name, "bed_day_service_code": charge,
                          "is_critical_care": critical, "counts_in_census": census}
                _event(db, "bed_class", new_code, "created", detail, actor, now, reason)
                _audit(db, actor, "bed_master_bed_class_created", f"Bed class {new_code} set up",
                       target_type="bed_class", target_id=new_code, request_meta=request_meta,
                       metadata=detail)
                key = new_code
            else:
                row = db.execute("SELECT * FROM bed_classes WHERE code = ?", (code,)).fetchone()
                if row is None:
                    raise BedMasterNotFound("No bed class with that code is set up",
                                            code="bed_class_not_found")
                current = dict(row)
                new: dict[str, Any] = {}
                if data.get("code") is not None and _norm_code(data["code"], "Bed class code") != code:
                    raise BedMasterError("A bed class code cannot be changed. Set up a new class "
                                         "instead.", code="code_fixed", field="code")
                if data.get("name") is not None:
                    name = _clean(data["name"])
                    if not name:
                        raise BedMasterError("Bed class name is required", code="required", field="name")
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
                        beds = _count(db, "SELECT COUNT(*) FROM housekeeping_units WHERE bed_class = ? "
                                          "AND is_active = 1 AND retired_at IS NULL", (code,))
                        wards = _count(db, "SELECT COUNT(*) FROM wards WHERE default_bed_class = ? "
                                           "AND is_active = 1", (code,))
                        if beds or wards:
                            raise BedMasterConflict(
                                f"Bed class {code} is used by {beds} beds and {wards} wards, so it "
                                "cannot be switched off.", code="bed_class_in_use",
                                beds=beds, wards=wards)
                    new["is_active"] = active
                changes = {k: [current.get(k), v] for k, v in new.items() if current.get(k) != v}
                if changes:
                    cols = ", ".join(f"{k} = ?" for k in changes)
                    db.execute(f"UPDATE bed_classes SET {cols}, updated_at = ? WHERE code = ?",
                               (*[v[1] for v in changes.values()], now, code))
                    _event(db, "bed_class", code, "updated", {"changes": changes}, actor, now, reason)
                    _audit(db, actor, "bed_master_bed_class_updated",
                           f"Bed class {code} changed: {', '.join(sorted(changes))}",
                           target_type="bed_class", target_id=code, request_meta=request_meta,
                           metadata={"changes": changes})
                key = code
            out = dict(db.execute("SELECT * FROM bed_classes WHERE code = ?", (key,)).fetchone())
    except DBIntegrityError as exc:
        raise BedMasterConflict("That bed class clashes with one already set up. Nothing was "
                                "changed.", code="duplicate_code") from exc
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
    covering, declared = _department_lists()
    plan: list[tuple[str, str]] = []
    skipped: list[dict] = []
    for dept in cfg.departments:
        dept_name = " ".join(str(dept.get("name") or "").split())
        for prefix in dept.get("wards") or []:
            try:
                plan.append((_norm_code(prefix, "Ward code"), dept_name))
            except BedMasterError as exc:
                skipped.append({"code": str(prefix), "department": dept_name, "reason": str(exc)})
    created: list[dict] = []
    existing: list[dict] = []
    conflicts: list[dict] = []
    if not plan:
        return {"created": created, "existing": existing, "conflicts": conflicts,
                "skipped": skipped, "file_present": bool(cfg.present),
                "message": "The site configuration declares no wards, so nothing was set up."}
    now = utc_iso_now()
    try:
        with db_session(immediate=True) as db:
            for code, dept_name in plan:
                department = _resolve_department(dept_name, covering, declared)
                row = db.execute("SELECT * FROM wards WHERE LOWER(TRIM(code)) = LOWER(TRIM(?))",
                                 (code,)).fetchone()
                if row is not None:
                    if (row["department"] or "").casefold() == department.casefold():
                        existing.append(_ward_out(row, covering))
                    else:
                        conflicts.append({
                            "code": row["code"], "ward_id": row["id"],
                            "department_in_setup": row["department"],
                            "department_in_file": department,
                        })
                    continue
                new_id = _new_id("WRD")
                db.execute(
                    """INSERT INTO wards
                           (id, facility_code, code, name, department, ward_type,
                            cleaning_tracked, is_active, created_by, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'general', 1, 1, ?, ?, ?)""",
                    (new_id, FACILITY_CODE, code, code, department, (actor or {}).get("id"), now, now),
                )
                _event(db, "ward", new_id, "imported",
                       {"code": code, "department": department, "source": "site configuration"},
                       actor, now)
                created.append(_ward_out(
                    db.execute("SELECT * FROM wards WHERE id = ?", (new_id,)).fetchone(), covering))
            if created:
                _audit(db, actor, "bed_master_site_config_imported",
                       f"{len(created)} wards set up from the site configuration",
                       target_type="ward", target_id=created[0]["id"], request_meta=request_meta,
                       metadata={"created": [w["code"] for w in created],
                                 "existing": [w["code"] for w in existing],
                                 "conflicts": [c["code"] for c in conflicts]})
    except DBIntegrityError as exc:
        raise BedMasterConflict("A ward in the site configuration clashes with one already set "
                                "up. Nothing was saved.", code="duplicate_code") from exc
    return {"created": created, "existing": existing, "conflicts": conflicts,
            "skipped": skipped, "file_present": bool(cfg.present), "message": None}
