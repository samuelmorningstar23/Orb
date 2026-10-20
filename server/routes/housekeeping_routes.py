"""Orb — Housekeeping & facilities routes: room turnaround, cleaning, linen.

    GET    /api/housekeeping/meta                  vocabulary + the transition table
    GET    /api/housekeeping/overview              the board (runs the discharge sweep)
    GET    /api/housekeeping/units                 the unit register
    POST   /api/housekeeping/units                 register / re-describe a unit
                                                   (a bed is 409 while ORB_ADT is on:
                                                   Bed setup owns beds then)
    POST   /api/housekeeping/units/{id}/state      move a unit through the machine
    POST   /api/housekeeping/units/{id}/occupy     record an occupancy, clean or not
    GET    /api/housekeeping/tasks                 the cleaning queue
    GET    /api/housekeeping/tasks/{id}            one request + the unit's ledger
    POST   /api/housekeeping/tasks                 raise a cleaning request
    POST   /api/housekeeping/tasks/{id}/start      a cleaner picks it up
    POST   /api/housekeeping/tasks/{id}/complete   the clean is finished → ready
    POST   /api/housekeeping/tasks/{id}/cancel     withdraw a request
    POST   /api/housekeeping/sync-discharges       run the turnaround sweep now
    POST   /api/housekeeping/sync-movements        read the bed movement ledger now
                                                   (ORB_ADT on; adt-7)
    GET    /api/housekeeping/turnaround            turnaround time per ward
    GET    /api/housekeeping/readiness             readiness NEXT TO capacity's number
    GET    /api/housekeeping/linen                 linen movement summary
    POST   /api/housekeeping/linen                 record a linen issue / return

EVERY HANDLER IS A PLAIN `def`. They open SQLite sessions, so FastAPI must
threadpool them — an `async def` here would put an encrypted-SQLite write on the
event loop, which is the defect class behind Orb's production freezes
(tests/test_event_loop_discipline.py).

── NO PHI CROSSES THIS BOUNDARY ─────────────────────────────────────────────
Not one route here takes or returns a patient id, a name, an MRN or a diagnosis,
and none takes `patient_id` on a query string — so none needs
`enforce_patient_scope`, and the guard in tests/test_phi_read_scope_gaps.py has
nothing to find. That is a property of the DESIGN, not an oversight: a
housekeeping board is read on a corridor trolley by staff who hold no clinical
grant, and the moment it starts carrying "bed 4, Mrs Sharma, discharged" it
becomes a chart nobody is auditing.

The single clinical fact that reaches this module is the BOOLEAN
`clean_type = 'terminal'`, derived server-side by the discharge sweep from
whether the departing admission carried isolation precautions. The precaution
text is never copied. A housekeeper needs to know to gown up; they do not need
to know why.

── NOTHING HERE CAN BLOCK A CLINICAL ACTION ─────────────────────────────────
Every route 404s as a whole when the module is switched off, and no clinical
path calls any of them. `POST /units/{id}/state` refusing an illegal transition
refuses a HOUSEKEEPING RECORD; admission does not consult it. See the PILOT
SAFETY section of services/housekeeping_service.py.
"""

from __future__ import annotations

import uuid

import config
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import (
    ADMIN_OVERVIEW,
    FORECAST_READ,
    VITALS_WRITE,
    has_permission,
    require_permission,
)
from pydantic import BaseModel
from services.audit_service import log_audit_event
from services.housekeeping_service import (
    CLEAN_TYPES,
    LEGAL_TRANSITIONS,
    LINEN_DIRECTIONS,
    PRIORITIES,
    REQUEST_TYPES,
    UNIT_STATES,
    HousekeepingError,
    UnitAlreadyRegistered,
    UnitManagedByBedSetup,
    cancel_cleaning,
    complete_cleaning,
    expected_minutes_for,
    find_unit_by_code,
    force_occupied,
    get_task,
    get_unit,
    linen_summary,
    list_linen_items,
    list_tasks,
    list_units,
    overview,
    raise_cleaning_request,
    record_linen_movement,
    register_unit,
    start_cleaning,
    sync_adt_movements,
    sync_discharge_turnarounds,
    transition_unit,
    ward_readiness,
    ward_turnaround_report,
)

router = APIRouter(prefix="/api/housekeeping", tags=["housekeeping"])


# ── Request bodies ───────────────────────────────────────────────────────────
#
# Declared models rather than `data: dict`, for the same reason the front-desk
# module gives: a typed body is one the scope-drift guards can SEE. None of these
# carries a patient id, and none may ever grow one.


class UnitIn(BaseModel):
    unit_code: str
    ward: str
    room: str | None = None
    bed_label: str | None = None
    unit_type: str = "bed"
    is_isolation_room: bool = False
    initial_state: str = "vacant_dirty"
    notes: str | None = None
    #: Set by the register form: a code already on the register is refused
    #: (409), not overwritten.
    create_only: bool = False


class StateIn(BaseModel):
    state: str
    reason: str | None = None


class OccupyIn(BaseModel):
    reason: str


class TaskIn(BaseModel):
    unit_id: str
    request_type: str = "routine"
    priority: str = "routine"
    clean_type: str = "standard"
    description: str | None = None
    precaution_note: str | None = None


class NoteIn(BaseModel):
    note: str | None = None


class CancelIn(BaseModel):
    reason: str


class LinenIn(BaseModel):
    ward: str
    item_code: str
    direction: str
    quantity: int
    task_id: str | None = None
    note: str | None = None
    idempotency_key: str | None = None


# ── Module switch ────────────────────────────────────────────────────────────


def require_housekeeping_module() -> None:
    """404 the whole module when the deployment has switched it off.

    404 rather than 403: a site whose housekeeping runs on a whiteboard has no
    housekeeping module in Orb, so "no such endpoint" is the honest answer, and
    the shell does not render the tab either (src/shellConfig.tsx reads the same
    flag off /api/health). Read through `config` rather than imported by value so
    a test — and an ops restart — sees the current setting.
    """
    if not config.HOUSEKEEPING_ENABLED:
        raise HTTPException(
            status_code=404,
            detail={"error": "module_disabled", "message": "Housekeeping module is disabled"},
        )


# ── Permissions ──────────────────────────────────────────────────────────────
#
# There is no `housekeeping` role and no `housekeeping.write` permission in
# Orb's model, and permissions.py is a file several parallel module agents share,
# so this COMPOSES existing grants rather than adding one:
#
#   READ  → `forecast.read`. Held by doctor, nurse and admin. It is the grant
#           that already means "may see how the hospital's beds are doing", which
#           is exactly what this board is. It is NOT `patient.read_all`, because
#           nothing here is a patient record and requiring a PHI grant to look at
#           a cleaning queue would be a reason to hand PHI grants to cleaners.
#
#   WRITE → `vitals.write` (doctor/nurse — the ward staff who mark a bed soiled
#           and know when the patient left) OR `admin.overview` (the housekeeping
#           supervisor's account; in Orb's user model every non-clinical hospital
#           worker holds an admin account). A pharmacist holds neither.
#
# FOLLOWUP recorded for the owner of permissions.py: a real `housekeeping.write`
# permission and a `housekeeping` role, so a hospital does not have to give its
# cleaning supervisor an administrator account, and so a ward clerk can mark a
# bed dirty without holding `vitals.write`.


def require_housekeeping_read():
    """Dependency: `forecast.read` + the module switch."""

    _CHECKER_USER_DEFAULT = Depends(require_permission(FORECAST_READ))
    _CHECKER_MODULE_DEFAULT = Depends(require_housekeeping_module)

    def _checker(
        user: dict = _CHECKER_USER_DEFAULT,
        _module: None = _CHECKER_MODULE_DEFAULT,
    ) -> dict:
        return user

    return _checker


def require_housekeeping_write():
    """Dependency: `vitals.write` OR `admin.overview`, with an audited denial."""

    _CHECKER_USER_DEFAULT = Depends(require_permission(FORECAST_READ))
    _CHECKER_MODULE_DEFAULT = Depends(require_housekeeping_module)

    def _checker(
        request: Request,
        user: dict = _CHECKER_USER_DEFAULT,
        _module: None = _CHECKER_MODULE_DEFAULT,
    ) -> dict:
        role = user.get("role", "")
        if has_permission(role, VITALS_WRITE) or has_permission(role, ADMIN_OVERVIEW):
            return user
        # Sync def: FastAPI threadpools this, so the blocking audit write is off
        # the event loop without run_db.
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="permission_denied",
            outcome="denied",
            summary_message="Housekeeping write requires vitals.write or admin.overview",
            actor_user_id=user.get("id"),
            actor_role=role,
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            metadata={"required_permissions": [VITALS_WRITE, ADMIN_OVERVIEW]},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "permission_denied",
                "message": "Housekeeping actions require vitals.write or admin.overview",
                "required": [VITALS_WRITE, ADMIN_OVERVIEW],
            },
        )

    return _checker


def _bad_request(err: HousekeepingError) -> HTTPException:
    """HousekeepingError is always caller error (illegal transition, unknown
    unit, bad quantity) → 422 with the machine-readable code intact, because the
    UI reacts differently to each: an illegal transition belongs on the state
    control, an unknown linen item belongs next to the item picker."""
    return HTTPException(
        status_code=422,
        detail={"error": "housekeeping_refused", "message": str(err)},
    )


def _audit(request: Request, user: dict, action: str, summary: str, **metadata) -> None:
    """One audit line per housekeeping write.

    Category `facilities`, which is deliberately NOT in
    audit_service._FAIL_CLOSED_CATEGORIES: an audit hiccup must not stop a
    cleaner recording that a bed is clean. The fail-closed categories are the
    ones where the RECORD is the point (clinical access, security, auth); here
    the bed being marked clean is the point, and the audit line is corroboration.
    No patient identifier is ever passed.
    """
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="facilities",
        action_type=action,
        outcome="success",
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata=metadata or None,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Vocabulary
# ═════════════════════════════════════════════════════════════════════════════


_HOUSEKEEPING_META_USER_DEFAULT = Depends(require_housekeeping_read())


@router.get("/meta")
def housekeeping_meta(_user: dict = _HOUSEKEEPING_META_USER_DEFAULT):
    """The vocabulary and THE TRANSITION TABLE ITSELF.

    The legal moves are published rather than reimplemented in the panel. A UI
    that hard-codes which buttons to show grows a second, silently divergent copy
    of the state machine — and the copy that is wrong is always the one the user
    can see.
    """
    return {
        "states": list(UNIT_STATES),
        "legal_transitions": {k: sorted(v) for k, v in LEGAL_TRANSITIONS.items()},
        "request_types": list(REQUEST_TYPES),
        "priorities": list(PRIORITIES),
        "clean_types": list(CLEAN_TYPES),
        "linen_directions": list(LINEN_DIRECTIONS),
        "linen_items": list_linen_items(),
        "expected_minutes": {
            "standard": expected_minutes_for("standard"),
            "terminal": expected_minutes_for("terminal"),
        },
    }


_HOUSEKEEPING_OVERVIEW_SYNC_DEFAULT = Query(
    True, description="Run the discharge turnaround sweep first"
)
_HOUSEKEEPING_OVERVIEW_USER_DEFAULT = Depends(require_housekeeping_read())


@router.get("/overview")
def housekeeping_overview(
    sync: bool = _HOUSEKEEPING_OVERVIEW_SYNC_DEFAULT,
    _user: dict = _HOUSEKEEPING_OVERVIEW_USER_DEFAULT,
):
    return overview(sync=sync)


# ═════════════════════════════════════════════════════════════════════════════
#  Units
# ═════════════════════════════════════════════════════════════════════════════


_GET_UNITS_WARD_DEFAULT = Query(None)
_GET_UNITS_INCLUDE_INACTIVE_DEFAULT = Query(False)
_GET_UNITS_USER_DEFAULT = Depends(require_housekeeping_read())


@router.get("/units")
def get_units(
    ward: str | None = _GET_UNITS_WARD_DEFAULT,
    include_inactive: bool = _GET_UNITS_INCLUDE_INACTIVE_DEFAULT,
    _user: dict = _GET_UNITS_USER_DEFAULT,
):
    return {"units": list_units(ward, include_inactive=include_inactive)}


#: The refusal a bed register request gets while Beds and admissions owns beds.
_MANAGED_IN_BEDS_SETUP = (
    "Beds are registered and changed in Bed setup while beds and admissions is "
    "switched on, so this bed was not saved here. Rooms, bays and theatres are "
    "still registered here."
)


def _managed_in_beds_setup(message: str = _MANAGED_IN_BEDS_SETUP) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"error": "managed_in_beds_setup", "message": message}
    )


def _refuse_beds_while_adt_is_on(body: UnitIn) -> None:
    """With ORB_ADT on, the bed master is the only writer of bed units (adt-1,
    critique C5): a bed created or relabelled here would be a second register
    whose ward and label drift from the master's. So a request that creates a
    bed, or changes a unit that is a bed, is refused with 409. Rooms, bays and
    theatres are unaffected. Read at request time; with the switch off nothing
    here runs and the register behaves exactly as before."""
    if config.module_state("adt") == "off":
        return
    is_bed = (body.unit_type or "").strip().lower() == "bed"
    if not is_bed:
        existing = find_unit_by_code(body.unit_code)
        is_bed = (existing is not None and bool(existing)) and existing.get("unit_type") == "bed"
    if is_bed:
        raise _managed_in_beds_setup()


_POST_UNIT_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/units")
def post_unit(body: UnitIn, request: Request, user: dict = _POST_UNIT_USER_DEFAULT):
    _refuse_beds_while_adt_is_on(body)
    try:
        unit = register_unit(
            unit_code=body.unit_code,
            ward=body.ward,
            room=body.room,
            bed_label=body.bed_label,
            unit_type=body.unit_type,
            is_isolation_room=body.is_isolation_room,
            initial_state=body.initial_state,
            notes=body.notes,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            create_only=body.create_only,
        )
    except UnitAlreadyRegistered as e:
        raise HTTPException(
            status_code=409, detail={"error": "already_exists", "message": str(e)}
        ) from e
    except UnitManagedByBedSetup as e:
        # A bed Bed setup registered (it carries a ward_id) is changed there,
        # whatever the switch says now.
        raise _managed_in_beds_setup(str(e)) from e
    except HousekeepingError as e:
        raise _bad_request(e) from e
    _audit(
        request,
        user,
        "housekeeping_unit_registered",
        f"Housekeeping unit {unit['unit_code']} registered on {unit['ward']}",
        unit_code=unit["unit_code"],
        ward=unit["ward"],
        state=unit["state"],
    )
    return unit


_GET_ONE_UNIT_USER_DEFAULT = Depends(require_housekeeping_read())


@router.get("/units/{unit_id}")
def get_one_unit(unit_id: str, _user: dict = _GET_ONE_UNIT_USER_DEFAULT):
    try:
        return get_unit(unit_id)
    except HousekeepingError as e:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": str(e)}
        ) from e


_POST_UNIT_STATE_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/units/{unit_id}/state")
def post_unit_state(
    unit_id: str,
    body: StateIn,
    request: Request,
    user: dict = _POST_UNIT_STATE_USER_DEFAULT,
):
    try:
        unit = transition_unit(
            unit_id,
            body.state,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            reason=body.reason,
        )
    except HousekeepingError as e:
        raise _bad_request(e) from e
    _audit(
        request,
        user,
        "housekeeping_unit_state_changed",
        f"{unit['unit_code']} → {unit['state']}",
        unit_code=unit["unit_code"],
        state=unit["state"],
    )
    return unit


_POST_UNIT_OCCUPY_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/units/{unit_id}/occupy")
def post_unit_occupy(
    unit_id: str,
    body: OccupyIn,
    request: Request,
    user: dict = _POST_UNIT_OCCUPY_USER_DEFAULT,
):
    """Record that a unit is occupied — INCLUDING when the clean was skipped.

    This is the honest-record path described in `force_occupied`. It records what
    happened; it does not authorise anything, and no admission consults it.
    """
    try:
        unit = force_occupied(
            unit_id,
            reason=body.reason,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
        )
    except HousekeepingError as e:
        raise _bad_request(e) from e
    _audit(
        request,
        user,
        "housekeeping_unit_occupied_without_clean"
        if unit.get("clean_was_skipped")
        else "housekeeping_unit_occupied",
        f"{unit['unit_code']} recorded occupied"
        + (" WITHOUT a completed clean" if unit.get("clean_was_skipped") else ""),
        unit_code=unit["unit_code"],
        clean_skipped=bool(unit.get("clean_was_skipped")),
    )
    return unit


# ═════════════════════════════════════════════════════════════════════════════
#  Cleaning requests
# ═════════════════════════════════════════════════════════════════════════════


_GET_TASKS_WARD_DEFAULT = Query(None)
_GET_TASKS_STATUS_DEFAULT = Query(None)
_GET_TASKS_PRIORITY_DEFAULT = Query(None)
_GET_TASKS_LIMIT_DEFAULT = Query(200, ge=1, le=1000)
_GET_TASKS_USER_DEFAULT = Depends(require_housekeeping_read())


@router.get("/tasks")
def get_tasks(
    ward: str | None = _GET_TASKS_WARD_DEFAULT,
    status: str | None = _GET_TASKS_STATUS_DEFAULT,
    priority: str | None = _GET_TASKS_PRIORITY_DEFAULT,
    limit: int = _GET_TASKS_LIMIT_DEFAULT,
    _user: dict = _GET_TASKS_USER_DEFAULT,
):
    try:
        return {"tasks": list_tasks(ward=ward, status=status, priority=priority, limit=limit)}
    except HousekeepingError as e:
        raise _bad_request(e) from e


_POST_TASK_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/tasks")
def post_task(body: TaskIn, request: Request, user: dict = _POST_TASK_USER_DEFAULT):
    try:
        task = raise_cleaning_request(
            unit_id=body.unit_id,
            request_type=body.request_type,
            priority=body.priority,
            clean_type=body.clean_type,
            description=body.description,
            precaution_note=body.precaution_note,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
        )
    except HousekeepingError as e:
        raise _bad_request(e) from e
    _audit(
        request,
        user,
        "housekeeping_request_raised",
        f"{task['request_type']} ({task['priority']}) raised on {task['ward']}",
        task_id=task["id"],
        priority=task["priority"],
        clean_type=task["clean_type"],
    )
    return task


_GET_ONE_TASK_USER_DEFAULT = Depends(require_housekeeping_read())


@router.get("/tasks/{task_id}")
def get_one_task(task_id: str, _user: dict = _GET_ONE_TASK_USER_DEFAULT):
    try:
        return get_task(task_id)
    except HousekeepingError as e:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": str(e)}
        ) from e


_POST_TASK_START_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/tasks/{task_id}/start")
def post_task_start(
    task_id: str,
    request: Request,
    user: dict = _POST_TASK_START_USER_DEFAULT,
):
    try:
        task = start_cleaning(task_id, actor_user_id=user.get("id"), actor_role=user.get("role"))
    except HousekeepingError as e:
        raise _bad_request(e) from e
    _audit(
        request,
        user,
        "housekeeping_clean_started",
        f"Cleaning started on {task.get('unit_code') or task['unit_id']}",
        task_id=task_id,
    )
    return task


_POST_TASK_COMPLETE_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/tasks/{task_id}/complete")
def post_task_complete(
    task_id: str,
    body: NoteIn,
    request: Request,
    user: dict = _POST_TASK_COMPLETE_USER_DEFAULT,
):
    try:
        task = complete_cleaning(
            task_id,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            note=body.note,
        )
    except HousekeepingError as e:
        raise _bad_request(e) from e
    _audit(
        request,
        user,
        "housekeeping_clean_completed",
        f"Clean completed; unit is ready ({task['ward']})",
        task_id=task_id,
        turnaround_minutes=task.get("turnaround_minutes"),
    )
    return task


_POST_TASK_CANCEL_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/tasks/{task_id}/cancel")
def post_task_cancel(
    task_id: str,
    body: CancelIn,
    request: Request,
    user: dict = _POST_TASK_CANCEL_USER_DEFAULT,
):
    try:
        task = cancel_cleaning(task_id, reason=body.reason, actor_user_id=user.get("id"))
    except HousekeepingError as e:
        raise _bad_request(e) from e
    _audit(
        request,
        user,
        "housekeeping_request_cancelled",
        f"Cleaning request cancelled: {body.reason}",
        task_id=task_id,
    )
    return task


_POST_SYNC_DISCHARGES_LOOKBACK_HOURS_DEFAULT = Query(None, ge=1, le=720)
_POST_SYNC_DISCHARGES_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/sync-discharges")
def post_sync_discharges(
    request: Request,
    lookback_hours: int | None = _POST_SYNC_DISCHARGES_LOOKBACK_HOURS_DEFAULT,
    user: dict = _POST_SYNC_DISCHARGES_USER_DEFAULT,
):
    """Run the discharge turnaround sweep now.

    Idempotent by construction (see `sync_discharge_turnarounds`): calling it ten
    times creates the same tasks once. Exposed as its own endpoint so an operator
    can run it after registering a ward's beds, without waiting for someone to
    open the board.
    """
    result = sync_discharge_turnarounds(lookback_hours=lookback_hours)
    _audit(
        request,
        user,
        "housekeeping_discharge_sweep",
        f"Discharge turnaround sweep: {result['created']} created, "
        f"{result['unmatched']} discharges with no registered bed",
        **{k: v for k, v in result.items() if k != "unmatched_wards"},
    )
    return result


_POST_SYNC_MOVEMENTS_LOOKBACK_HOURS_DEFAULT = Query(None, ge=1, le=720)
_POST_SYNC_MOVEMENTS_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/sync-movements")
def post_sync_movements(
    request: Request,
    lookback_hours: int | None = _POST_SYNC_MOVEMENTS_LOOKBACK_HOURS_DEFAULT,
    user: dict = _POST_SYNC_MOVEMENTS_USER_DEFAULT,
):
    """Read the bed movement ledger now (Beds and admissions, adt-7): a
    turnaround clean for each bed a patient left, and each bed a patient went
    into marked occupied (the skipped clean recorded when it was not clean).

    Idempotent (see `sync_adt_movements`): ten calls act once. The board runs
    it on every load and the bed moves run it after they commit; this is for
    an operator who has just registered a ward's beds. With ORB_ADT off it
    reads and writes nothing and says so. Counts only: no movement id, bed of
    a named patient or patient field is returned.
    """
    result = sync_adt_movements(lookback_hours=lookback_hours)
    _audit(
        request,
        user,
        "housekeeping_movement_sync",
        f"Bed movement sync: {result.get('vacated', 0) + result.get('tasks_raised', 0)} "
        f"turnarounds raised, {result.get('occupied', 0) + result.get('occupied_unclean', 0)} "
        "beds marked occupied",
        **result,
    )
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Reporting
# ═════════════════════════════════════════════════════════════════════════════


_GET_TURNAROUND_WARD_DEFAULT = Query(None)
_GET_TURNAROUND_DAYS_DEFAULT = Query(30, ge=1, le=365)
_GET_TURNAROUND_USER_DEFAULT = Depends(require_housekeeping_read())


@router.get("/turnaround")
def get_turnaround(
    ward: str | None = _GET_TURNAROUND_WARD_DEFAULT,
    days: int = _GET_TURNAROUND_DAYS_DEFAULT,
    _user: dict = _GET_TURNAROUND_USER_DEFAULT,
):
    return ward_turnaround_report(ward=ward, days=days)


_GET_READINESS_WARD_DEFAULT = Query(None)
_GET_READINESS_USER_DEFAULT = Depends(require_housekeeping_read())


@router.get("/readiness")
def get_readiness(
    ward: str | None = _GET_READINESS_WARD_DEFAULT,
    _user: dict = _GET_READINESS_USER_DEFAULT,
):
    """Housekeeping readiness, ALWAYS beside the capacity screen's own number.

    See the CAPACITY section of services/housekeeping_service.py: this module
    does not change what `available` means anywhere else in Orb, and the response
    says so in words as well as numbers.
    """
    return ward_readiness(ward=ward)


# ═════════════════════════════════════════════════════════════════════════════
#  Linen
# ═════════════════════════════════════════════════════════════════════════════


_GET_LINEN_WARD_DEFAULT = Query(None)
_GET_LINEN_DAYS_DEFAULT = Query(30, ge=1, le=365)
_GET_LINEN_USER_DEFAULT = Depends(require_housekeeping_read())


@router.get("/linen")
def get_linen(
    ward: str | None = _GET_LINEN_WARD_DEFAULT,
    days: int = _GET_LINEN_DAYS_DEFAULT,
    _user: dict = _GET_LINEN_USER_DEFAULT,
):
    return {**linen_summary(ward=ward, days=days), "items": list_linen_items()}


_POST_LINEN_USER_DEFAULT = Depends(require_housekeeping_write())


@router.post("/linen")
def post_linen(body: LinenIn, request: Request, user: dict = _POST_LINEN_USER_DEFAULT):
    try:
        movement = record_linen_movement(
            ward=body.ward,
            item_code=body.item_code,
            direction=body.direction,
            quantity=body.quantity,
            task_id=body.task_id,
            note=body.note,
            actor_user_id=user.get("id"),
            idempotency_key=body.idempotency_key,
        )
    except HousekeepingError as e:
        raise _bad_request(e) from e
    _audit(
        request,
        user,
        "housekeeping_linen_movement",
        f"{body.direction} {body.quantity} x {body.item_code} ({body.ward})",
        ward=body.ward,
        item_code=body.item_code,
        direction=body.direction,
        quantity=body.quantity,
    )
    return movement
