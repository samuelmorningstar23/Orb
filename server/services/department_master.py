"""Orb: the departments master's writer (multisite-3, W10-B, 20 October 2026).

Ruling MS-R4 (a), built strictest: a department name is unique across the whole
hospital group, ignoring capital letters and spacing, and belongs to exactly
one facility. That is what keeps a ward-scoped account inside its facility
without touching a single scope call site (permissions compares
admissions.department with users.department, and
facility_service.facility_scope_for maps the department to its facility), so
the master's two rules carry weight:

  * no two departments share a name, whatever facility each is at and however
    the name is capitalised or spaced. The table's UNIQUE COLLATE NOCASE and
    trim CHECK (migration 70) catch capitals and outer spaces; this module
    also folds runs of inner spaces, so "General  Medicine" is
    "General Medicine";
  * a department's facility never changes once anything names the
    department: a staff account, an admission, a ward, an outpatient visit, a
    Relay department room, or any other row with a department column (every
    column named department or ending in _department, found from the schema
    itself, so a table added later is covered). Moving it would silently
    carry every one of those patients and staff to another facility.

Writes are audited 'security' (fail-closed, in the write's own transaction):
adding a department or moving an unused one changes which facility a
department-scoped account resolves to. Every write calls
facility_service.invalidate_cache() after its commit. Refusals raise
DepartmentError(code, message, status) and write nothing.

ORB_MULTI_SITE gates growth: a department at a facility other than 1, and any
move between facilities, need the switch on. Facility 1's list can be
completed with the switch off (the operations status page lists departments
in use that the list lacks), as facility 1's identity can be edited.

No route serves this yet: the design's GET and POST /api/facilities/{id}/
departments wait for the Facilities screen (see the wave report).
"""

from __future__ import annotations

import uuid

from database import db_session

#: Longest department name accepted; the master's names are shown on boards.
MAX_NAME = 120
#: Shortest reason accepted for moving a department between facilities.
MIN_REASON = 3


class DepartmentError(Exception):
    """A departments master change Orb refuses, with a machine word, plain
    words and the HTTP status a route answers with. Nothing was written."""

    def __init__(self, code: str, message: str, *, status: int = 409, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


def normalise_name(raw) -> str | None:
    """A department name as the master stores it: outer spaces dropped and
    every run of inner spaces made one. None when nothing is left."""
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.split())
    return name or None


def name_key(name: str) -> str:
    """The key two names are the same department under: case and spacing folded."""
    return " ".join(name.split()).casefold()


def _actor_id(actor) -> str | None:
    return None if actor is None else actor.get("id")


def _audit_on(
    db,
    actor: dict,
    action: str,
    summary: str,
    *,
    department_id: int,
    metadata: dict,
    request_meta: dict | None,
) -> None:
    from services.audit_service import log_audit_event

    meta = request_meta or {}
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=_actor_id(actor),
        actor_role=(actor or {}).get("role"),
        target_type="department",
        target_id=str(department_id),
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata=metadata,
    )


def _require_manager(actor: dict) -> None:
    from permissions import FACILITY_MANAGE, has_permission

    if not has_permission((actor or {}).get("role", ""), FACILITY_MANAGE):
        raise DepartmentError(
            "permission_denied",
            "Only an administrator can change the departments list.",
            status=403,
        )


def _facility_on(db, facility_id) -> dict:
    row = db.execute(
        "SELECT id, code, is_active FROM facilities WHERE id = ?", (facility_id,)
    ).fetchone()
    if row is None:
        raise DepartmentError(
            "facility_not_found", "There is no facility with that number.", status=404
        )
    if not row["is_active"]:
        raise DepartmentError(
            "facility_inactive",
            "That facility is closed, so its departments cannot change.",
            status=409,
        )
    return {"id": int(row["id"]), "code": row["code"]}


def _require_growth_switch(facility_id: int) -> None:
    import config

    from services import facility_service

    if facility_id != facility_service.MAIN_FACILITY_ID and not config.feature_enabled(
        "multi_site"
    ):
        raise DepartmentError(
            "multi_site_off",
            "Multi-site is switched off on this site, so departments belong to the first "
            "facility only.",
            status=404,
        )


def _require_in_scope(actor: dict, facility_ids, db) -> None:
    from services import facility_service

    scope = facility_service.facility_scope_for(actor, db=db)
    if scope is None or all(f in scope for f in facility_ids):
        return
    raise DepartmentError(
        "facility_scope",
        "That facility is not yours. Only an administrator at that facility, or one with "
        "group-wide access, can change its departments.",
        status=403,
    )


def _row_view(row, codes: dict) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "display_name": row["display_name"],
        "facility_id": int(row["facility_id"]),
        "facility_code": codes.get(int(row["facility_id"])),
        "is_active": bool(row["is_active"]),
        "seeded_from": row["seeded_from"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
    }


def _codes_on(db) -> dict:
    return {int(r["id"]): r["code"] for r in db.execute("SELECT id, code FROM facilities")}


def list_departments(*, facility_id: int | None = None) -> list[dict]:
    """The departments master, one facility's or every facility's, by name.
    Department names and facility codes only; no patient data."""
    with db_session() as db:
        codes = _codes_on(db)
        if facility_id is None:
            rows = db.execute("SELECT * FROM departments ORDER BY name COLLATE NOCASE").fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM departments WHERE facility_id = ? ORDER BY name COLLATE NOCASE",
                (facility_id,),
            ).fetchall()
    return [_row_view(r, codes) for r in rows]


def _same_name_on(db, name: str):
    """The master row that is already this department (case and spacing
    folded), or None."""
    wanted = name_key(name)
    for row in db.execute("SELECT * FROM departments").fetchall():
        if name_key(row["name"]) == wanted:
            return row
    return None


def canonical_name(name, *, db=None) -> str | None:
    """The master's own spelling of a department (case and spacing folded),
    or None when the master does not list it."""
    clean = normalise_name(name)
    if clean is None:
        return None
    if db is None:
        with db_session() as own:
            return canonical_name(clean, db=own)
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'departments'"
        ).fetchone()
        is None
    ):
        return None
    row = _same_name_on(db, clean)
    return None if row is None else row["name"]


def department_references(db, name: str) -> list[dict]:
    """Where a department name is in use: [{where, column}] for every table
    with a department column holding it (case and outer spaces ignored), plus
    Relay department rooms. The one rule a department's facility hangs on, so
    every such column in the schema is read, not a list kept by hand."""
    wanted = name.strip().lower()
    found: list[dict] = []
    tables = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'departments' ORDER BY name"
        ).fetchall()
    ]
    for table in tables:
        columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")').fetchall()]
        for column in columns:
            if column != "department" and not column.endswith("_department"):
                continue
            hit = db.execute(
                f'SELECT 1 FROM "{table}" WHERE LOWER(TRIM("{column}")) = ? LIMIT 1', (wanted,)
            ).fetchone()
            if hit is not None:
                found.append({"where": table, "column": column})
        if table == "channels" and {"name", "type"} <= set(columns):
            hit = db.execute(
                "SELECT 1 FROM channels WHERE type = 'department' AND LOWER(TRIM(name)) = ? "
                "LIMIT 1",
                (wanted,),
            ).fetchone()
            if hit is not None:
                found.append({"where": "channels", "column": "name"})
    return found


def add_department(
    name, facility_id: int, *, actor: dict, display_name=None, request_meta: dict | None = None
) -> dict:
    """Add a department to a facility's list. Refused: not an administrator
    (403), a blank, overlong or bed-prefix-shaped name (400), a facility that
    does not exist (404) or is closed (409), a facility other than 1 with
    ORB_MULTI_SITE off (404 multi_site_off), a facility outside the
    administrator's (403), and a name any facility already lists, however it
    is capitalised or spaced (409 department_exists, naming the facility)."""
    from services import facility_service
    from services.site_config_service import looks_like_bed_prefix

    _require_manager(actor)
    clean = normalise_name(name)
    if clean is None:
        raise DepartmentError("name_required", "Type the department's name.", status=400)
    if len(clean) > MAX_NAME:
        raise DepartmentError(
            "name_too_long", f"A department name is at most {MAX_NAME} characters.", status=400
        )
    if looks_like_bed_prefix(clean):
        raise DepartmentError(
            "looks_like_a_bed",
            f"'{clean}' looks like a bed or ward label, not a department. A department is "
            "the clinical service, such as General Medicine.",
            status=400,
        )
    shown = normalise_name(display_name) if display_name is not None else None
    _require_growth_switch(int(facility_id))
    with db_session(immediate=True) as db:
        facility = _facility_on(db, facility_id)
        _require_in_scope(actor, (facility["id"],), db)
        existing = _same_name_on(db, clean)
        if existing is not None:
            codes = _codes_on(db)
            raise DepartmentError(
                "department_exists",
                f"'{existing['name']}' is already on the departments list of facility "
                f"{codes.get(int(existing['facility_id']), existing['facility_id'])}. A "
                "department name belongs to one facility of the group.",
                facility_code=codes.get(int(existing["facility_id"])),
            )
        cur = db.execute(
            "INSERT INTO departments (facility_id, name, display_name, is_active, seeded_from, "
            "created_by) VALUES (?, ?, ?, 1, 'admin', ?)",
            (facility["id"], clean, shown, _actor_id(actor)),
        )
        department_id = int(cur.lastrowid)
        _audit_on(
            db,
            actor,
            "department_created",
            f"Department {clean!r} added to facility {facility['code']}",
            department_id=department_id,
            metadata={
                "name": clean,
                "facility_id": facility["id"],
                "facility_code": facility["code"],
            },
            request_meta=request_meta,
        )
        row = db.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()
        view = _row_view(row, _codes_on(db))
    facility_service.invalidate_cache()
    return view


def move_department(
    department_id: int, facility_id: int, *, actor: dict, reason, request_meta: dict | None = None
) -> dict:
    """Move a department nothing names yet to another facility. Refused: not
    an administrator (403), ORB_MULTI_SITE off (404 multi_site_off), an
    unknown department (404), a facility that does not exist (404) or is
    closed (409), the facility it is already at (409 no_change), either
    facility outside the administrator's (403), no reason (400), and a
    department any record names (409 department_in_use, listing where): its
    facility is fixed from then on."""
    from services import facility_service

    _require_manager(actor)
    import config

    if not config.feature_enabled("multi_site"):
        raise DepartmentError(
            "multi_site_off",
            "Multi-site is switched off on this site, so departments belong to the first "
            "facility only.",
            status=404,
        )
    why = normalise_name(reason)
    if why is None or len(why) < MIN_REASON:
        raise DepartmentError("reason_required", "Say why the department moves.", status=400)
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()
        if row is None:
            raise DepartmentError(
                "department_not_found", "There is no such department.", status=404
            )
        target = _facility_on(db, facility_id)
        current = int(row["facility_id"])
        if current == target["id"]:
            raise DepartmentError("no_change", "The department is already at that facility.")
        _require_in_scope(actor, (current, target["id"]), db)
        used = department_references(db, row["name"])
        if used:
            raise DepartmentError(
                "department_in_use",
                f"'{row['name']}' is already in use, so it stays at its facility: moving it "
                "would move every patient and member of staff recorded under it.",
                where=sorted({u["where"] for u in used}),
            )
        db.execute(
            "UPDATE departments SET facility_id = ? WHERE id = ?", (target["id"], department_id)
        )
        _audit_on(
            db,
            actor,
            "department_facility_changed",
            f"Department {row['name']!r} moved from facility {current} to {target['code']}",
            department_id=int(row["id"]),
            metadata={
                "name": row["name"],
                "from_facility_id": current,
                "to_facility_id": target["id"],
                "reason": why,
            },
            request_meta=request_meta,
        )
        moved = db.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()
        view = _row_view(moved, _codes_on(db))
    facility_service.invalidate_cache()
    return view
