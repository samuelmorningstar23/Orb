"""Orb — Front Desk routes: the OPD queue and patient TRANSFERS.

    GET    /api/frontdesk/opd/queue                 today's clinics, by doctor
    POST   /api/frontdesk/opd/visits                register an outpatient visit
    GET    /api/frontdesk/opd/visits/{id}           one visit
    POST   /api/frontdesk/opd/visits/{id}/start     call the patient in
    POST   /api/frontdesk/opd/visits/{id}/complete  finish + raise the charge
    POST   /api/frontdesk/opd/visits/{id}/cancel    no-show / left without being seen
    GET    /api/frontdesk/transfers/destinations    departments with clinical cover
    POST   /api/frontdesk/transfers                 move a patient between wards
    GET    /api/frontdesk/patients/{patient_id}/movements   movement history

EVERY HANDLER IS A PLAIN `def`. They open SQLite sessions, so FastAPI must
threadpool them — an `async def` here would put an encrypted-SQLite write on the
event loop, which is the defect class behind all three of Orb's production
freezes (tests/test_event_loop_discipline.py).

── SCOPE, ON EVERY ROUTE ────────────────────────────────────────────────────
The ward a caller may see comes from `permissions.department_scope(user)` — the
department on the AUTHENTICATED user's row — and never from the request. Two
shapes are in play here and they are not the same question:

  * INPATIENTS have a department derived from their active admission
    (`permissions.PATIENT_DEPARTMENT_SQL`). `enforce_patient_scope` and
    `require_patient_access` are the guards.
  * OUTPATIENTS have no admission, so that derivation yields nothing and
    `patient_in_scope` fail-closes them out of every ward. The OPD queue is
    therefore scoped on the VISIT's department instead — still compared against
    `department_scope(user)`, still not nameable by the caller. See
    `frontdesk_service.opd_registration_allowed` for why registration needs a
    third rule and what it stops (an OPD desk used to launder ward scope).

── A TRANSFER IS A PERMISSION CHANGE ────────────────────────────────────────
Moving a patient moves who can read their chart. The transfer audit row is
written in the fail-closed `clinical_access` category for exactly that reason:
if the move cannot be recorded on the hash chain, the move does not happen.

── WHO MAY WRITE HERE ───────────────────────────────────────────────────────
Every mutation takes `frontdesk.write` OR `patient.write`
(require_frontdesk_write, below). Clinicians reach the desk through
`patient.write`, exactly as they always did; the `frontdesk.write` arm replaced
`admin.overview` (ruled 2026-08-12) so the registration clerk no longer has to
hold the governance grant — the permission that also opens the audit log — just
to file an admission. Reads stay on `patient.read_all`.

── THE REGISTRATION DESK (opd-3, 3 October 2026) ───────────────────────────
The OPD queue, one visit, the doctors list, registering a visit and cancelling
one sit on permissions.require_registration_desk instead: read is
`patient.read_all` OR `registration.read`, write adds `frontdesk.write` OR
`patient.write` OR `registration.write`. The registration.* arms count only
while ORB_REGISTRATION is on, so with it off these five routes behave exactly
as before. That is what lets a receptionist (registration.* and nothing else)
work the queue without holding the chart.

Calling a patient in, completing a consultation, every transfer route and the
movement history are NOT on it and keep the rules above. A receptionist
therefore cannot move a patient (a transfer moves who can read the chart) and
cannot finish a consultation (that raises the charge and, with the OPD feature,
belongs to opd.consult).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import config
from database import db_session
from permissions import (
    FRONTDESK_WRITE,
    PATIENT_READ_ALL,
    PATIENT_WRITE,
    department_scope,
    enforce_patient_scope,
    has_permission,
    log_phi_read,
    require_patient_access,
    require_permission,
    require_registration_desk,
)
from services.audit_service import log_audit_event
from services.frontdesk_service import (
    FrontDeskError,
    cancel_opd_visit,
    complete_opd_visit,
    get_opd_visit,
    list_consulting_doctors,
    list_opd_queue,
    list_patient_movements,
    list_transfer_destinations,
    opd_registration_allowed,
    register_opd_visit,
    start_opd_consultation,
    transfer_patient,
)

router = APIRouter(prefix="/api/frontdesk", tags=["frontdesk"])


# ── Request bodies ───────────────────────────────────────────────────────────
#
# These two are DECLARED MODELS rather than `data: dict` for one specific
# reason beyond validation: both let the CALLER name the patient in the body,
# which is the exact defect shape tests/test_caller_named_patient_scope.py
# ratchets against — and that guard can only SEE a route whose patient_id
# arrives through a typed body (or a path param). A `dict` body makes a route
# invisible to the guard, which is worse than being listed by it.

class OpdVisitIn(BaseModel):
    patient_id: str
    department: str | None = None
    doctor_id: str | None = None
    visit_type: str = "new"
    chief_complaint: str | None = None
    appointment_id: str | None = None
    notes: str | None = None
    idempotency_key: str | None = None


class CompleteVisitIn(BaseModel):
    service_code: str | None = None
    raise_charge: bool = True
    notes: str | None = None


class CancelVisitIn(BaseModel):
    reason: str


class TransferIn(BaseModel):
    patient_id: str
    to_department: str
    to_ward: str | None = None
    to_bed: str | None = None
    reason: str
    notes: str | None = None
    idempotency_key: str | None = None


# ── Module switch ────────────────────────────────────────────────────────────

def require_frontdesk_module() -> None:
    """404 the whole module when the deployment has switched it off.

    404 rather than 403: a site that runs its own registration system has no
    front desk in Orb, so the honest answer is "no such endpoint", and the shell
    does not render the tab either (src/shellConfig.tsx reads the same flag off
    /api/health). Read through the `config` module rather than imported by value
    so a test — and an ops restart — sees the current setting.
    """
    if not config.FRONTDESK_ENABLED:
        raise HTTPException(status_code=404, detail="Front desk module is disabled")


# ── Permissions ──────────────────────────────────────────────────────────────
#
# A registration/transfer action needs `patient.write` (doctor/clinician) OR
# `frontdesk.write` (the registration clerk's own grant). Two arms because two
# different jobs walk up to the same desk:
#
#   * `patient.write` — a clinician registering their walk-in or moving their
#     own patient is ordinary clinical work and was always legitimate. This arm
#     is unchanged.
#   * `frontdesk.write` — until the 2026-08-12 ruling this arm was
#     `admin.overview`, the GOVERNANCE permission that also opens the audit log,
#     because permissions.py had no front-desk permission and this module could
#     not add one (the FOLLOWUP recorded here has now been executed: the
#     constant lives in permissions.py with the other back-office splits). The
#     over-grant ran one way only: clinicians were fine, but the clerk had to
#     hold the hospital's tamper-evidence record just to file an admission. The
#     desk's admin account still works — `admin` holds `frontdesk.write` — and
#     the clerk no longer needs governance; what changed is what a future
#     receptionist account has to be handed.
#
# A nurse holds neither arm and cannot move a patient between wards through this
# module — deliberately: a transfer changes who can read the chart, and that is
# not a bedside decision.

def require_frontdesk_write():
    """Dependency: `frontdesk.write` OR `patient.write`, with an audited denial."""
    def _checker(
        request: Request,
        user: dict = Depends(require_permission(PATIENT_READ_ALL)),
        _module: None = Depends(require_frontdesk_module),
    ) -> dict:
        role = user.get("role", "")
        if has_permission(role, PATIENT_WRITE) or has_permission(role, FRONTDESK_WRITE):
            return user
        # Sync def: FastAPI threadpools this, so the blocking audit write is off
        # the event loop without run_db.
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="permission_denied",
            outcome="denied",
            summary_message="Front-desk write requires patient.write or frontdesk.write",
            actor_user_id=user.get("id"),
            actor_role=role,
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            metadata={"required_permissions": [PATIENT_WRITE, FRONTDESK_WRITE]},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "permission_denied",
                "message": "Front-desk actions require patient.write or frontdesk.write",
                "required": [PATIENT_WRITE, FRONTDESK_WRITE],
            },
        )
    return _checker


def require_frontdesk_read():
    """Dependency: `patient.read_all` + the module switch."""
    def _checker(
        user: dict = Depends(require_permission(PATIENT_READ_ALL)),
        _module: None = Depends(require_frontdesk_module),
    ) -> dict:
        return user
    return _checker


def require_desk_read():
    """The registration desk's read rule (permissions.require_registration_desk)
    with the module switch where it always sat: after the permission check."""
    return require_registration_desk("read", gate=require_frontdesk_module)


def require_desk_write():
    """The registration desk's write rule with the module switch between the
    read and the write check, the order require_frontdesk_write always had."""
    return require_registration_desk("write", gate=require_frontdesk_module)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _bad_request(err: FrontDeskError) -> HTTPException:
    """FrontDeskError is always caller error (bad ward, wrong state) → 422."""
    return HTTPException(status_code=422, detail=str(err))


def _visit_or_404(visit_id: str, user: dict) -> dict:
    """Load a visit and prove the caller's department owns its clinic.

    404 on both "no such visit" and "not your clinic", never 403 — a 403 confirms
    the visit exists, and a visit id implies a patient attended a named clinic on
    a named day. Same rule as require_patient_access.
    """
    visit = get_opd_visit(visit_id)
    if visit is None:
        raise HTTPException(status_code=404, detail="OPD visit not found")
    dept = department_scope(user)
    if dept is not None and visit.get("department") != dept:
        raise HTTPException(status_code=404, detail="OPD visit not found")
    return visit


# ═════════════════════════════════════════════════════════════════════════════
#  OPD
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/opd/queue")
def opd_queue(
    request: Request,
    date: str | None = None,
    doctor_id: str | None = None,
    include_completed: bool = False,
    user: dict = Depends(require_desk_read()),
):
    """Today's OPD queues for the caller's department, grouped by doctor.

    THE DEPARTMENT IS NOT A PARAMETER. It is derived from the authenticated user;
    a caller can narrow to one doctor and one date, and nothing else. That is the
    lesson of /api/nurse/board, which once took `?department=` off the query
    string and let one ward's nurse pull six wards of patients.
    """
    dept = department_scope(user)
    statuses = (
        ("waiting", "in_consultation", "completed", "cancelled")
        if include_completed else None
    )
    try:
        result = list_opd_queue(
            department=dept, visit_date=date, doctor_id=doctor_id, statuses=statuses
        )
    except FrontDeskError as err:
        raise _bad_request(err)

    if result["total"]:
        log_phi_read(
            request=request,
            user=user,
            action_type="opd_queue_viewed",
            summary_message=(
                f"OPD queue viewed ({result['total']} visits) for "
                f"{dept or '(all, user has no department on record)'} "
                f"on {result['visit_date']}"
            ),
            metadata={"result_count": result["total"], "visit_date": result["visit_date"]},
        )
    return result


@router.get("/opd/doctors")
def opd_doctors(user: dict = Depends(require_desk_read())):
    """The doctors this desk may book a visit with, from the STAFF TABLE.

    Not from today's queue. The panel used to derive its dropdown from
    `/opd/queue`, which made the first booking of the day impossible: no visits
    meant no doctors meant no way to create a visit. A roster is a property of
    who works here, not of who has already been booked.

    Same scope rule as every read on this router: the department comes from the
    authenticated user, never from the request. Carries no PHI — names and
    employee ids of staff — so there is no `log_phi_read` here.
    """
    return {"doctors": list_consulting_doctors(department_scope(user))}


@router.post("/opd/visits")
def create_opd_visit(
    data: OpdVisitIn,
    request: Request,
    user: dict = Depends(require_desk_write()),
):
    """Register an outpatient visit and issue a token."""
    patient_id = (data.patient_id or "").strip()
    if not patient_id:
        raise HTTPException(status_code=422, detail="patient_id is required")
    department = (data.department or "").strip()
    dept = department_scope(user)
    if not department:
        # A scoped caller registers into their own clinic by default. An
        # unscoped governance account must say which one — there is no sensible
        # default for "all departments".
        if dept is None:
            raise HTTPException(
                status_code=422,
                detail="department is required for an account with no department on record",
            )
        department = dept
    elif dept is not None and department.strip().casefold() != dept.casefold():
        # A caller cannot register a patient into somebody else's clinic: they
        # would then read that patient off their own queue, which is the ward
        # boundary going the wrong way.
        raise HTTPException(
            status_code=403,
            detail={
                "error": "department_scope",
                "message": f"You can only register visits in {dept}",
            },
        )

    with db_session() as db:
        if not opd_registration_allowed(db, patient_id, dept):
            # 404, not 403 — the caller must not learn that this patient exists.
            raise HTTPException(status_code=404, detail="Patient not found")

    try:
        visit = register_opd_visit(
            patient_id=patient_id,
            department=department,
            doctor_id=data.doctor_id,
            visit_type=(data.visit_type or "new"),
            chief_complaint=data.chief_complaint,
            appointment_id=data.appointment_id,
            notes=data.notes,
            registered_by=user.get("id"),
            idempotency_key=data.idempotency_key,
        )
    except FrontDeskError as err:
        raise _bad_request(err)

    log_audit_event(
        correlation_id=f"opd-register-{visit['id']}",
        event_category="clinical_record",
        action_type="opd_visit_registered",
        outcome="success",
        summary_message=(
            f"OPD visit {visit['id']} registered for {patient_id} in "
            f"{visit['department']} (token {visit['token_number']})"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type="opd_visit",
        target_id=visit["id"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={
            "department": visit["department"],
            "token_number": visit["token_number"],
            "doctor_id": visit.get("doctor_id"),
        },
    )
    return visit


@router.get("/opd/visits/{visit_id}")
def read_opd_visit(
    visit_id: str,
    request: Request,
    user: dict = Depends(require_desk_read()),
):
    visit = _visit_or_404(visit_id, user)
    log_phi_read(
        request=request,
        user=user,
        action_type="opd_visit_viewed",
        summary_message=f"OPD visit {visit_id} viewed",
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=visit_id,
    )
    return visit


@router.post("/opd/visits/{visit_id}/start")
def start_visit(
    visit_id: str,
    request: Request,
    user: dict = Depends(require_frontdesk_write()),
):
    """Call the patient in — 'waiting' → 'in_consultation'."""
    _visit_or_404(visit_id, user)
    try:
        visit = start_opd_consultation(visit_id, called_by=user.get("id"))
    except FrontDeskError as err:
        raise _bad_request(err)

    log_audit_event(
        correlation_id=f"opd-start-{visit_id}",
        event_category="clinical_record",
        action_type="opd_consultation_started",
        outcome="success",
        summary_message=f"OPD visit {visit_id} called in",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=visit_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
    )
    return visit


@router.post("/opd/visits/{visit_id}/complete")
def complete_visit(
    visit_id: str,
    request: Request,
    data: CompleteVisitIn | None = None,
    user: dict = Depends(require_frontdesk_write()),
):
    """Finish the consultation and raise the consultation charge line.

    The charge goes into the billing spine's `encounter_charges` table with
    `encounter_type='op'`; this module never writes an invoice. If the site has
    not priced the consultation item, the row is written UNPRICED
    (`rate_is_configured = 0`) rather than at ₹0 — see
    frontdesk_service._raise_consultation_charge.
    """
    body = data or CompleteVisitIn()
    _visit_or_404(visit_id, user)
    try:
        visit = complete_opd_visit(
            visit_id,
            completed_by=user.get("id"),
            service_code=body.service_code,
            raise_charge=bool(body.raise_charge),
            notes=body.notes,
        )
    except FrontDeskError as err:
        raise _bad_request(err)

    charge = visit.get("consultation_charge") or {}
    log_audit_event(
        correlation_id=f"opd-complete-{visit_id}",
        event_category="clinical_record",
        action_type="opd_visit_completed",
        outcome="success",
        summary_message=(
            f"OPD visit {visit_id} completed; consultation charge "
            f"{charge.get('charge_id') or 'not raised'}"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=visit_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={
            "charge_id": charge.get("charge_id"),
            "rate_is_configured": charge.get("rate_is_configured"),
            "total_paise": charge.get("total_paise"),
        },
    )
    return visit


@router.post("/opd/visits/{visit_id}/cancel")
def cancel_visit(
    visit_id: str,
    data: CancelVisitIn,
    request: Request,
    user: dict = Depends(require_desk_write()),
):
    """No-show / left without being seen. Soft — the row is kept."""
    _visit_or_404(visit_id, user)
    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A cancellation reason is required")
    try:
        visit = cancel_opd_visit(visit_id, reason=reason, cancelled_by=user.get("id"))
    except FrontDeskError as err:
        raise _bad_request(err)

    log_audit_event(
        correlation_id=f"opd-cancel-{visit_id}",
        event_category="clinical_record",
        action_type="opd_visit_cancelled",
        outcome="success",
        summary_message=f"OPD visit {visit_id} cancelled: {reason}",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=visit_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
    )
    return visit


# ═════════════════════════════════════════════════════════════════════════════
#  Transfers
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/transfers/destinations")
def transfer_destinations(user: dict = Depends(require_frontdesk_read())):
    """Departments a patient may be transferred INTO, and the wards in use there.

    Carries no PHI (department names, ward labels and occupancy counts), so it is
    not scoped to the caller's ward: choosing a destination requires seeing the
    destinations. `staff_count` is included so the clerk can see WHY a department
    is offered — it is the anti-orphaning rule made visible rather than a silent
    filter.
    """
    return {"destinations": list_transfer_destinations()}


@router.post("/transfers")
def create_transfer(
    data: TransferIn,
    request: Request,
    user: dict = Depends(require_frontdesk_write()),
):
    """Move an admitted patient to another ward.

    THE SCOPE CHECK IS ON THE SENDING SIDE. `enforce_patient_scope` requires the
    caller to be able to see the patient WHERE THEY ARE NOW; the destination is
    deliberately not restricted to the caller's own department, because a
    transfer that could only move a patient within one ward is not a transfer.

    After this returns, the caller may no longer be able to read the patient —
    that is the strict-handover policy, and it is a consequence of scope being
    derived live from the admission row (see frontdesk_service's module docstring).
    """
    patient_id = (data.patient_id or "").strip()
    if not patient_id:
        raise HTTPException(status_code=422, detail="patient_id is required")
    to_department = (data.to_department or "").strip()
    if not to_department:
        raise HTTPException(status_code=422, detail="to_department is required")
    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A transfer reason is required")

    enforce_patient_scope(user, patient_id)

    try:
        result = transfer_patient(
            patient_id=patient_id,
            to_department=to_department,
            to_ward=data.to_ward,
            to_bed=data.to_bed,
            reason=reason,
            moved_by=user.get("id"),
            moved_by_name=user.get("name"),
            notes=data.notes,
            idempotency_key=data.idempotency_key,
        )
    except FrontDeskError as err:
        raise _bad_request(err)

    movement = result.get("movement") or {}
    # `clinical_access` is FAIL-CLOSED (services/audit_service). A transfer
    # changes which accounts can read this patient's chart; if that change cannot
    # be written to the hash chain, it must not be reported as having happened.
    log_audit_event(
        correlation_id=f"transfer-{movement.get('id')}",
        event_category="clinical_access",
        action_type="patient_transferred",
        outcome="success",
        summary_message=(
            f"{patient_id} transferred from "
            f"{movement.get('from_department') or 'unknown'} to "
            f"{movement.get('to_department')}: {reason}"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type="patient_movement",
        target_id=movement.get("id"),
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={
            "from_department": movement.get("from_department"),
            "from_ward": movement.get("from_ward"),
            "from_bed": movement.get("from_bed"),
            "to_department": movement.get("to_department"),
            "to_ward": movement.get("to_ward"),
            "to_bed": movement.get("to_bed"),
            "admission_id": result.get("admission_id"),
            # Recorded so a later audit can prove the admission was preserved.
            "admission_status": result.get("admission_status"),
        },
    )
    return result


@router.get("/patients/{patient_id}/movements")
def patient_movements(
    patient_id: str,
    limit: int = 50,
    user: dict = Depends(
        require_patient_access(PATIENT_READ_ALL, audit_action="patient_movements_viewed")
    ),
    _module: None = Depends(require_frontdesk_module),
):
    """Where this patient has been, newest first. Ward-scoped + read-audited by
    the dependency, which resolves `patient_id` from the PATH."""
    return {"patient_id": patient_id, "movements": list_patient_movements(patient_id, limit=limit)}
