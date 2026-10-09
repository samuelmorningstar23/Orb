"""Orb — Front Desk routes: the OPD queue and patient TRANSFERS.

    GET    /api/frontdesk/opd/queue                 today's clinics, by doctor
    POST   /api/frontdesk/opd/visits                register an outpatient visit
    GET    /api/frontdesk/opd/visits/{id}           one visit
    POST   /api/frontdesk/opd/visits/{id}/start     call the patient in
    POST   /api/frontdesk/opd/visits/{id}/complete  finish + raise the charge
    POST   /api/frontdesk/opd/visits/{id}/cancel    no-show / left without being seen
    POST   /api/frontdesk/opd/visits/{id}/call      OPD mode: call in (alias of start)
    POST   /api/frontdesk/opd/visits/{id}/skip      OPD mode: did not come when called
    POST   /api/frontdesk/opd/visits/{id}/recall    OPD mode: a skipped patient is back
    POST   /api/frontdesk/opd/visits/{id}/assign    OPD mode: the doctor takes an unassigned patient
    PATCH  /api/frontdesk/opd/visits/{id}           OPD mode: queue priority and room
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

── OPD MODE (opd-7, the 'opd' feature on) ──────────────────────────────────
Registering a visit can now open the patient's OUTPATIENT record view to the
clinic for ORB_OPD_SCOPE_DAYS days (permissions.patient_in_opd_scope; never the
inpatient chart). So in OPD mode:
  * a patient with no ACTIVE admission may be registered into the caller's
    clinic (frontdesk_service.opd_registration_allowed), so a patient
    discharged from Cardiology can be seen in the Ortho clinic;
  * the register, call-in, complete and cancel audit rows are written INSIDE
    the visit's own transaction instead of after its commit;
  * a visit for a patient with no active admission also writes
    'opd_scope_opened' (clinical_access, fail-closed) in that transaction,
    flagged self_registered when the registrant holds opd.consult (ruling
    OPD-N2: such visits count, flagged and audited). If that row cannot be
    written the visit is rolled back and the desk gets a 503;
  * an idempotent replay of a registration is bound to the request: the key
    must name a visit for the same patient in the same clinic, or the desk
    gets a 409 that says nothing about the other visit (and the refusal is
    audited). A matching replay writes its own 'opd_visit_registered' row
    (replay true) in its transaction, as the flag-off path writes one per
    call; it opens no new scope, so it writes no 'opd_scope_opened'.
With the feature off every route here behaves exactly as before.

── THE CONSULTING QUEUE (opd-8, the 'opd' feature on) ──────────────────────
  * calling a patient in and completing a consultation need opd.consult
    (doctor and clinician): the administrator's frontdesk.write and the
    receptionist's registration.* no longer run a consultation (403, audited);
  * call (alias of start), skip, recall and assign need opd.consult; PATCH of
    priority and room is the desk's write rule; every one is visit-scoped
    (404 outside the caller's clinic) and audited in the visit's transaction;
  * a cancel may carry a reason code ('left_without_being_seen' is LWBS) and
    never raises a charge;
  * visits carry their queue fields and the desk queue is in seeing order
    (priority, then token); its response says how often to re-read
    (ORB_OPD_QUEUE_POLL_SECONDS) until live desk nudges land (opd-9b);
  * after every change the clinic's open doctor queues get a content-free
    'opd:queue_changed' {department} on the socket (opd_queue_service).

── WHO THE PATIENT IS (opd-6a, the 'registration' feature on) ──────────────
With registration on, the desk finds a returning patient in the hospital-wide
index, so attaching a visit to the wrong record (and so to someone else's
allergies and history) is one tap away. Ruling OPD-S1, applied strictly:
EVERY visit registered here carries `identity_check`, the identifiers the
patient STATES (name; date of birth or age; the last 4 digits of their phone
or their UHID), and the server checks them against the record
(registration_service.check_identity, before the visit's transaction) and
answers match or no match, never a stored value. No check is 409
'identity_check_required'; no match is 409 'identity_not_confirmed' (the
failure is audited by the check, and the refused visit on the fail-closed
patient_identity chain); five failures by one user for one patient in an hour
is 429. A patient registered a moment ago is no exception: the desk sends
what it just typed and the UHID the registration returned. The passing check
is stored on opd_visits.identity_check_json and in the visit's audit row.
With the feature off the field is ignored and nothing here changes.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

import config
from database import db_session
from permissions import (
    FRONTDESK_WRITE,
    OPD_CONSULT,
    PATIENT_READ_ALL,
    PATIENT_WRITE,
    department_scope,
    enforce_patient_scope,
    has_permission,
    log_phi_read,
    opd_scope_window,
    require_patient_access,
    require_permission,
    require_registration_desk,
)
from routes.opt_in_gates import require_opd_feature
from services import registration_service
from services.adt import core as adt_core
from services.adt import transfer as adt_transfer
from services.audit_service import AuditWriteError, log_audit_event
from services.frontdesk_service import (
    FrontDeskError,
    OpdReplayConflict,
    cancel_opd_visit,
    complete_opd_visit,
    get_opd_visit,
    list_consulting_doctors,
    list_opd_queue,
    list_patient_movements,
    list_transfer_destinations,
    opd_mode,
    opd_registration_allowed,
    register_opd_visit,
    start_opd_consultation,
    transfer_patient,
)
from services.opd_queue_service import (
    ASSIGN_AUDIT_ACTION,
    ASSIGN_AUDIT_PREFIX,
    QueueConflict,
    assign_visit,
    nudge_queue_changed,
    recall_visit,
    skip_visit,
    update_queue_fields,
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

class IdentityStatedIn(BaseModel):
    """What the patient STATES at the desk (opd-6a, ruling OPD-S1): their
    name, their date of birth or age, and the last 4 digits of their phone or
    their UHID. Compared on the server; never echoed back."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, max_length=200)
    date_of_birth: str | None = Field(None, max_length=40)
    age_years: int | None = None
    phone_last4: str | None = Field(None, max_length=10)
    uhid: str | None = Field(None, max_length=40)


class OpdVisitIn(BaseModel):
    patient_id: str
    department: str | None = None
    doctor_id: str | None = None
    visit_type: str = "new"
    chief_complaint: str | None = None
    appointment_id: str | None = None
    notes: str | None = None
    idempotency_key: str | None = None
    # Required while the 'registration' feature is on (opd-6a); ignored off.
    identity_check: IdentityStatedIn | None = None


class CompleteVisitIn(BaseModel):
    service_code: str | None = None
    raise_charge: bool = True
    notes: str | None = None


class CancelVisitIn(BaseModel):
    reason: str
    # OPD mode only (opd-8): one of opd_queue_service.CANCEL_REASON_CODES,
    # e.g. 'left_without_being_seen'. Ignored while the 'opd' feature is off.
    reason_code: str | None = None


class QueueFieldsIn(BaseModel):
    """PATCH body for a visit's queue fields (opd-8). Omitted fields stay."""
    priority: str | None = None
    priority_reason: str | None = None
    room: str | None = None


class TransferIn(BaseModel):
    patient_id: str
    to_department: str
    to_ward: str | None = None
    to_bed: str | None = None
    reason: str
    notes: str | None = None
    idempotency_key: str | None = None
    # Read only while Beds and admissions (ORB_ADT) is on, when this route
    # delegates to the ADT writer (adt-6a); ignored with it off.
    expected_bed_id: str | None = None
    boarder_acknowledged: bool = False


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


def _visit_in_department(visit_id: str, dept: str | None) -> dict:
    """_visit_or_404 for a caller whose scope the ENDPOINT already derived
    (opd-8): `dept` is permissions.department_scope(user), passed in so the
    derivation is visible in the route itself (tests/test_nurse_write_scope.py
    reads the endpoint for it). 404 for no such visit and for another clinic."""
    visit = get_opd_visit(visit_id)
    if visit is None:
        raise HTTPException(status_code=404, detail="OPD visit not found")
    if dept is not None and visit.get("department") != dept:
        raise HTTPException(status_code=404, detail="OPD visit not found")
    return visit


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


# ── OPD mode: audit rows inside the visit's own transaction (opd-7) ─────────
#
# Each helper returns the callback frontdesk_service runs INSIDE its immediate
# transaction, after the visit row changes and before it commits. A fail-closed
# row that cannot be written raises AuditWriteError there, the transaction rolls
# back, and the route answers 503: the change and its record land together or
# not at all. Used only while the 'opd' feature is on.

def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": ("The visit could not be recorded because the audit trail "
                        "is not available. Nothing was saved. Try again in a moment."),
        },
    )


def _request_audit_fields(request: Request, user: dict) -> dict:
    return {
        "actor_user_id": user.get("id"),
        "actor_role": user.get("role"),
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
    }


def _registration_audit(request: Request, user: dict, *, patient_id: str,
                        dept: str | None, self_registered: bool,
                        identity: dict | None = None):
    """The register callback: re-check the registration rule under the write
    lock, write 'opd_visit_registered', and for a patient with no active
    admission write 'opd_scope_opened' (clinical_access, fail-closed).
    `identity` is the passing stated-identifier check (registration feature
    on, opd-6a); its result rides in the 'opd_visit_registered' row."""

    def _write(db, visit: dict) -> None:
        # The rule was checked before the transaction began; an admission that
        # landed in between must not leave a visit that skipped it.
        if not opd_registration_allowed(db, patient_id, dept):
            raise HTTPException(status_code=404, detail="Patient not found")
        visit_id = str(visit["id"])
        fields = _request_audit_fields(request, user)
        correlation = f"opd-register-{visit_id}"
        log_audit_event(
            correlation_id=correlation,
            event_category="clinical_record",
            action_type="opd_visit_registered",
            outcome="success",
            db=db,
            summary_message=(
                f"OPD visit {visit_id} registered for {patient_id} in "
                f"{visit['department']} (token {visit['token_number']})"
            ),
            patient_id=patient_id,
            target_type="opd_visit",
            target_id=visit_id,
            metadata={
                "department": visit["department"],
                "token_number": visit["token_number"],
                "doctor_id": visit.get("doctor_id"),
                "self_registered": self_registered,
                "replay": False,
                **_identity_facts(identity),
                "written": "in_transaction",
            },
            **fields,
        )

        active = db.execute(
            "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
            (patient_id,),
        ).fetchone()
        if active is not None:
            # An inpatient's visit opens nothing: the OPD arm is closed for
            # anyone actively admitted (permissions.patient_in_opd_scope).
            return

        days = max(0, int(config.OPD_SCOPE_DAYS or 0))
        until = (date.fromisoformat(visit["visit_date"]) + timedelta(days=days)).isoformat()
        first_day, last_day = opd_scope_window()
        clinic = (visit["department"] or "").strip().casefold()
        earlier = db.execute(
            "SELECT department FROM opd_visits WHERE patient_id = ? AND id != ? "
            "AND status != 'cancelled' AND visit_date >= ? AND visit_date <= ?",
            (patient_id, visit_id, first_day, last_day),
        ).fetchall()
        extends = any((r["department"] or "").strip().casefold() == clinic for r in earlier)
        log_audit_event(
            correlation_id=correlation,
            event_category="clinical_access",
            action_type="opd_scope_opened",
            outcome="success",
            db=db,
            summary_message=(
                f"OPD visit {visit_id} opens the outpatient record of {patient_id} "
                f"to {visit['department']} until {until}"
                + ("; registered by a clinician who holds opd.consult"
                   if self_registered else "")
            ),
            patient_id=patient_id,
            target_type="opd_visit",
            target_id=visit_id,
            metadata={
                "department": visit["department"],
                "visit_date": visit["visit_date"],
                "scope_days": days,
                "scope_until": until,
                "self_registered": self_registered,
                "extends_open_window": extends,
                "registrant_role": user.get("role"),
            },
            **fields,
        )

    return _write


def _visit_event_audit(request: Request, user: dict, *, correlation_prefix: str,
                       action_type: str, summary, metadata=None):
    """A start, complete or cancel callback writing its clinical_record row in
    the visit's transaction. `summary(visit)` and `metadata(visit)` build the
    same words and fields the post-commit row always carried.

    The service calls it on EVERY successful call, a repeat included, exactly
    as the flag-off path writes a row per call. `facts` from the service (for
    example {"replay": True, "charge_raised_now": True}) go into the metadata,
    so a repeat that changed nothing reads as one and a repeat that raised a
    charge names it."""

    def _write(db, visit: dict, facts: dict | None = None) -> None:
        visit_id = str(visit["id"])
        extra = {**(metadata(visit) if metadata else {}), **(facts or {})}
        log_audit_event(
            correlation_id=f"{correlation_prefix}-{visit_id}",
            event_category="clinical_record",
            action_type=action_type,
            outcome="success",
            db=db,
            summary_message=summary(visit),
            patient_id=visit.get("patient_id"),
            target_type="opd_visit",
            target_id=visit_id,
            metadata={**extra, "written": "in_transaction"},
            **_request_audit_fields(request, user),
        )

    return _write


def _registration_replay_audit(request: Request, user: dict):
    """The replay callback for a registration whose idempotency key names a
    visit for the SAME patient in the SAME clinic (the service checks that
    before it calls this). The response carries the patient's name and MRN
    again, so the repeat gets its own 'opd_visit_registered' row, replay true,
    in the transaction: the flag-off path writes one per call and OPD mode
    must never write fewer. The first call opened the scope; a repeat opens
    nothing, so it writes no 'opd_scope_opened'."""
    return _visit_event_audit(
        request, user, correlation_prefix="opd-register",
        action_type="opd_visit_registered",
        summary=lambda v: (
            f"OPD visit {v['id']} registration repeated for {v.get('patient_id')} in "
            f"{v['department']} (token {v['token_number']}); nothing new was saved"
        ),
        metadata=lambda v: {
            "department": v["department"],
            "token_number": v["token_number"],
            "doctor_id": v.get("doctor_id"),
        },
    )


def _audit_replay_refused(request: Request, user: dict, *, patient_id: str,
                          department: str, err: OpdReplayConflict) -> None:
    """Record a registration refused because its idempotency key already names
    a visit for a different patient or clinic. Written after the rollback (the
    refusal saved nothing), on the same 'authorization' / denied footing as a
    patient_scope_denied row, so a key replayed across clinics is visible in
    the trail even though the caller learns nothing about the other visit."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="opd_visit_replay_refused",
        outcome="denied",
        summary_message=(
            f"OPD registration for {patient_id} in {department} refused: its "
            f"idempotency key already names OPD visit {err.visit_id}, which is "
            "for a different patient or clinic"
        ),
        patient_id=patient_id,
        target_type="opd_visit",
        target_id=err.visit_id,
        metadata={"requested_department": department,
                  "reason": "idempotency_key_names_another_visit"},
        **_request_audit_fields(request, user),
    )


def _replay_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "registration_key_conflict",
            "message": ("This registration was already submitted for a different "
                        "patient or clinic. Nothing was saved. Start the "
                        "registration again."),
        },
    )


# ── Registration on: who the patient is (opd-6a, ruling OPD-S1) ──────────────

def _identity_refused(request: Request, user: dict, *, patient_id: str,
                      reason_code: str) -> None:
    """A visit refused because the patient's identity was not confirmed, on
    the fail-closed patient_identity chain (written after nothing was saved).
    A failed check also has its own identity_check_failed row; this one says
    a visit was refused because of it. Raises AuditWriteError when it cannot
    be written, and the caller answers 503."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="patient_identity",
        action_type="opd_visit_identity_refused",
        outcome="denied",
        summary_message=(
            f"OPD visit for {patient_id} refused: "
            + ("no identity check was sent" if reason_code == "identity_check_required"
               else "what the patient stated did not match the record")),
        patient_id=patient_id,
        target_type="patient",
        target_id=patient_id,
        metadata={"reason_code": reason_code, "purpose": "opd_visit"},
        **_request_audit_fields(request, user),
    )


def _confirm_identity(request: Request, user: dict, *, patient_id: str,
                      dept: str | None, stated: IdentityStatedIn | None) -> dict:
    """Run the stated-identifier check for a visit (registration feature on).

    Before the visit's transaction, as registration_service.check_identity
    opens its own. Returns what is stored on the visit and its audit row:
    {result, checked, checked_at, checked_by, purpose, method}, never a stated
    or stored value. Refusals: no check 409 'identity_check_required'; too
    few identifiers 422 (the check's own codes); no match 409
    'identity_not_confirmed'; too many failures 429; another ward's inpatient
    404 (the check audits that as patient_scope_denied)."""
    if stated is None:
        try:
            _identity_refused(request, user, patient_id=patient_id,
                              reason_code="identity_check_required")
        except AuditWriteError:
            raise _audit_unavailable()
        raise HTTPException(status_code=409, detail={
            "error": "identity_check_required",
            "message": ("Confirm who the patient is first: ask for their name, their date "
                        "of birth or age, and the last 4 digits of their phone or their "
                        "UHID. Nothing was saved."),
        })
    try:
        verdict = registration_service.check_identity(
            patient_id, stated.model_dump(), user=user, department=dept,
            request_meta={
                "request_path": request.url.path,
                "http_method": request.method,
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
            },
            purpose="opd_visit")
    except registration_service.RegistrationNotFound:
        raise HTTPException(status_code=404, detail="Patient not found")
    except registration_service.RegistrationError as err:
        raise HTTPException(status_code=err.status, detail=err.detail())
    except AuditWriteError:
        raise _audit_unavailable()
    if verdict.get("result") != "match":
        try:
            _identity_refused(request, user, patient_id=patient_id,
                              reason_code="identity_not_confirmed")
        except AuditWriteError:
            raise _audit_unavailable()
        raise HTTPException(status_code=409, detail={
            "error": "identity_not_confirmed",
            "message": ("What the patient told you does not match this record. Check you "
                        "have the right patient. Nothing was saved."),
        })
    return {
        "result": "match",
        "checked": list(verdict.get("checked") or []),
        "checked_at": verdict.get("checked_at"),
        "checked_by": user.get("id"),
        "purpose": "opd_visit",
        "method": "stated_identifiers",
    }


def _identity_facts(identity: dict | None) -> dict:
    """What a visit's audit row and response say about the check: the
    result, which identifiers were compared, and when."""
    if not identity:
        return {}
    return {"identity_check": {k: identity.get(k) for k in ("result", "checked", "checked_at")}}


# ── OPD mode: the consulting doctor's actions (opd-8) ───────────────────────

def _require_opd_consult(request: Request, user: dict) -> None:
    """In OPD mode, calling a patient in, completing a consultation and the
    doctor's queue actions need opd.consult (doctor and clinician only). The
    administrator holds frontdesk.write and the receptionist registration.*,
    and neither may run a consultation. The denial is audited like every
    permission refusal. Sync: the handlers calling it are threadpooled."""
    role = user.get("role", "")
    if has_permission(role, OPD_CONSULT):
        return
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="permission_denied",
        outcome="denied",
        summary_message="Running an outpatient consultation requires opd.consult",
        actor_user_id=user.get("id"),
        actor_role=role,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={"required_permissions": [OPD_CONSULT], "opd_mode": True},
    )
    raise HTTPException(
        status_code=403,
        detail={
            "error": "permission_denied",
            "message": "Only a consulting doctor can do this.",
            "required": [OPD_CONSULT],
        },
    )


def _queue_conflict(err: QueueConflict) -> HTTPException:
    return HTTPException(status_code=409, detail={"error": err.code, "message": str(err)})


def _queue_action_audit(request: Request, user: dict, *, prefix: str, action_type: str,
                        summary, metadata=None):
    """The in-transaction clinical_record row for a queue action, written on
    every successful call (a repeat says replay true)."""
    return _visit_event_audit(
        request, user, correlation_prefix=prefix,
        action_type=action_type, summary=summary, metadata=metadata,
    )


def _call_in(visit_id: str, request: Request, user: dict, *, dept: str | None) -> dict:
    """OPD mode: call a patient in (the start route and its 'call' alias).
    The caller's opd.consult was checked first; the visit must be in the
    caller's clinic, `dept` = department_scope(user) (404 otherwise). Audited
    in the visit's transaction, then the clinic's open queues are nudged."""
    _visit_in_department(visit_id, dept)
    try:
        visit = start_opd_consultation(
            visit_id, called_by=user.get("id"),
            on_done=_visit_event_audit(
                request, user, correlation_prefix="opd-start",
                action_type="opd_consultation_started",
                summary=lambda v: f"OPD visit {v['id']} called in",
            ),
        )
    except FrontDeskError as err:
        raise _bad_request(err)
    except AuditWriteError:
        raise _audit_unavailable()
    nudge_queue_changed(visit.get("department"))
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

    # opd-6a (ruling OPD-S1): with the registration feature on, the patient
    # confirms who they are before any visit is attached to the record. Off,
    # nothing here runs and identity_check is ignored.
    identity = None
    if config.feature_enabled("registration"):
        identity = _confirm_identity(request, user, patient_id=patient_id, dept=dept,
                                     stated=data.identity_check)

    if opd_mode():
        # OPD mode (opd-7): the audit rows commit with the visit, and the visit
        # that opens the outpatient record view says so on the fail-closed chain.
        # A replay is bound to this patient and clinic and audited too.
        self_registered = has_permission(user.get("role", ""), OPD_CONSULT)
        try:
            registered = register_opd_visit(
                patient_id=patient_id,
                department=department,
                doctor_id=data.doctor_id,
                visit_type=(data.visit_type or "new"),
                chief_complaint=data.chief_complaint,
                appointment_id=data.appointment_id,
                notes=data.notes,
                registered_by=user.get("id"),
                idempotency_key=data.idempotency_key,
                self_registered=self_registered,
                on_registered=_registration_audit(
                    request, user, patient_id=patient_id, dept=dept,
                    self_registered=self_registered, identity=identity,
                ),
                on_replay=_registration_replay_audit(request, user),
                identity_check=identity,
            )
        except OpdReplayConflict as err:
            _audit_replay_refused(request, user, patient_id=patient_id,
                                  department=department, err=err)
            raise _replay_conflict()
        except FrontDeskError as err:
            raise _bad_request(err)
        except AuditWriteError:
            raise _audit_unavailable()
        # opd-8: the clinic's open queues refetch (content-free, after commit).
        nudge_queue_changed(registered.get("department"))
        return {**registered, **_identity_facts(identity)}

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
            identity_check=identity,
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
            **_identity_facts(identity),
        },
    )
    return {**visit, **_identity_facts(identity)}


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
    """Call the patient in: 'waiting' → 'in_consultation'.

    In OPD mode this needs opd.consult (opd-8): the administrator's
    frontdesk.write no longer runs a consultation."""
    if opd_mode():
        _require_opd_consult(request, user)
        return _call_in(visit_id, request, user, dept=department_scope(user))
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

    In OPD mode this needs opd.consult (opd-8). The consultation route's own
    completion, with a disposition and the signed-note rule (OPD-N8), is
    POST /api/opd/visits/{visit_id}/complete (opd-10).
    """
    body = data or CompleteVisitIn()
    if opd_mode():
        _require_opd_consult(request, user)
    _visit_or_404(visit_id, user)
    if opd_mode():
        def _charge_summary(v: dict) -> str:
            c = v.get("consultation_charge") or {}
            return (f"OPD visit {v['id']} completed; consultation charge "
                    f"{c.get('charge_id') or 'not raised'}")

        def _charge_fields(v: dict) -> dict:
            c = v.get("consultation_charge") or {}
            return {
                "charge_id": c.get("charge_id"),
                "rate_is_configured": c.get("rate_is_configured"),
                "total_paise": c.get("total_paise"),
            }

        try:
            completed = complete_opd_visit(
                visit_id,
                completed_by=user.get("id"),
                service_code=body.service_code,
                raise_charge=bool(body.raise_charge),
                notes=body.notes,
                on_done=_visit_event_audit(
                    request, user, correlation_prefix="opd-complete",
                    action_type="opd_visit_completed",
                    summary=_charge_summary, metadata=_charge_fields,
                ),
            )
        except FrontDeskError as err:
            raise _bad_request(err)
        except AuditWriteError:
            raise _audit_unavailable()
        nudge_queue_changed(completed.get("department"))
        return completed
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
    """No-show / left without being seen. Soft: the row is kept.

    In OPD mode (opd-8) the body may carry a reason_code; the LWBS code is
    'left_without_being_seen'. A cancel never raises a charge."""
    _visit_or_404(visit_id, user)
    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A cancellation reason is required")
    if opd_mode():
        code = (data.reason_code or "").strip().lower() or None
        try:
            cancelled = cancel_opd_visit(
                visit_id, reason=reason, cancelled_by=user.get("id"),
                reason_code=code,
                on_done=_visit_event_audit(
                    request, user, correlation_prefix="opd-cancel",
                    action_type="opd_visit_cancelled",
                    summary=lambda v: f"OPD visit {v['id']} cancelled: {reason}",
                    metadata=lambda v: {"reason_code": v.get("cancel_reason_code")},
                ),
            )
        except FrontDeskError as err:
            raise _bad_request(err)
        except AuditWriteError:
            raise _audit_unavailable()
        nudge_queue_changed(cancelled.get("department"))
        return cancelled
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


def _run_queue_action(visit_id: str, dept: str | None, action) -> dict:
    """The shared shape of an OPD-mode queue action: the visit must be in the
    caller's clinic (404), the service refuses a state that does not allow the
    action (409) or bad input (422), a fail-closed audit outage rolls back
    (503), and after the commit the clinic's open queues are nudged. `dept`
    is department_scope(user), derived by the endpoint."""
    _visit_in_department(visit_id, dept)
    try:
        visit = action()
    except QueueConflict as err:
        raise _queue_conflict(err)
    except FrontDeskError as err:
        raise _bad_request(err)
    except AuditWriteError:
        raise _audit_unavailable()
    nudge_queue_changed(visit.get("department"))
    return visit


@router.post("/opd/visits/{visit_id}/call")
def call_visit(
    visit_id: str,
    request: Request,
    _opd: None = Depends(require_opd_feature),
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """OPD mode: call the patient in. The same as /start, under the name the
    doctor's queue uses; counts the call and ends a skip."""
    return _call_in(visit_id, request, user, dept=department_scope(user))


@router.post("/opd/visits/{visit_id}/skip")
def skip_opd_visit(
    visit_id: str,
    request: Request,
    _opd: None = Depends(require_opd_feature),
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """OPD mode: the patient did not come when called. Back to waiting,
    marked skipped, until recalled or called again."""
    return _run_queue_action(visit_id, department_scope(user), lambda: skip_visit(
        visit_id, actor_id=user.get("id"),
        on_done=_queue_action_audit(
            request, user, prefix="opd-skip", action_type="opd_visit_skipped",
            summary=lambda v: f"OPD visit {v['id']} skipped (token {v['token_number']})",
        ),
    ))


@router.post("/opd/visits/{visit_id}/recall")
def recall_opd_visit(
    visit_id: str,
    request: Request,
    _opd: None = Depends(require_opd_feature),
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """OPD mode: a skipped patient is back in the line at their own token."""
    return _run_queue_action(visit_id, department_scope(user), lambda: recall_visit(
        visit_id, actor_id=user.get("id"),
        on_done=_queue_action_audit(
            request, user, prefix="opd-recall", action_type="opd_visit_recalled",
            summary=lambda v: f"OPD visit {v['id']} back in the queue (token {v['token_number']})",
        ),
    ))


@router.post("/opd/visits/{visit_id}/assign")
def assign_opd_visit(
    visit_id: str,
    request: Request,
    _opd: None = Depends(require_opd_feature),
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """OPD mode: the signed-in doctor takes an unassigned patient of their own
    clinic. The doctor is always the caller; nobody assigns another doctor.

    The answer carries token_changed and previous_token_number: a take issues
    a new token when the doctor's own line already holds the patient's, and
    the slip in the patient's hand then shows a number that is someone else's.
    The audit row below is where the queues read that change back from
    (opd_queue_service.reissued_tokens), so its prefix and action are the
    service's constants."""
    return _run_queue_action(visit_id, department_scope(user), lambda: assign_visit(
        visit_id, doctor_id=user.get("id") or "",
        on_done=_queue_action_audit(
            request, user, prefix=ASSIGN_AUDIT_PREFIX, action_type=ASSIGN_AUDIT_ACTION,
            summary=lambda v: (f"OPD visit {v['id']} taken by {v.get('doctor_name')} "
                               f"(token {v['token_number']})"),
            metadata=lambda v: {"doctor_id": v.get("doctor_id")},
        ),
    ))


@router.patch("/opd/visits/{visit_id}")
def update_opd_visit_queue(
    visit_id: str,
    data: QueueFieldsIn,
    request: Request,
    _opd: None = Depends(require_opd_feature),
    user: dict = Depends(require_desk_write()),
):
    """OPD mode: the desk (or a doctor) sets a visit's queue priority, with a
    reason for anything but normal (ruling OPD-N9), and its room. Only the
    fields the body names change."""
    given = data.model_fields_set
    kwargs: dict = {}
    if "priority" in given:
        kwargs["priority"] = data.priority
    if "priority_reason" in given:
        kwargs["priority_reason"] = data.priority_reason
    if "room" in given:
        kwargs["room"] = data.room
    if not kwargs:
        raise HTTPException(status_code=422, detail="Nothing to change")
    return _run_queue_action(visit_id, department_scope(user), lambda: update_queue_fields(
        visit_id, actor_id=user.get("id"), **kwargs,
        on_done=_queue_action_audit(
            request, user, prefix="opd-queue", action_type="opd_visit_queue_updated",
            summary=lambda v: (f"OPD visit {v['id']} priority {v.get('priority')}"
                               + (f", room {v['room']}" if v.get("room") else "")),
            metadata=lambda v: {"priority": v.get("priority"),
                                "priority_reason": v.get("priority_reason"),
                                "room": v.get("room")},
        ),
    ))


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

    BEDS AND ADMISSIONS ON (ORB_ADT, adt-6a): the move is made by the ADT
    writer (services/adt/transfer.py) with the same three rules, plus the bed
    lock, the cleaning rules for the bed moved into, the boarder
    acknowledgement and expected_bed_id when sent. Its clinical_access
    'patient_transferred' row is written INSIDE the move's transaction, so
    this route writes no audit row of its own after the commit (one row per
    transfer, never two). A typed ward and bed resolve against bed setup.
    Refusals answer with the ADT writer's {error, message}. Off: unchanged.
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

    if adt_core.adt_on():
        return _delegated_transfer(data, request, user, patient_id=patient_id,
                                   to_department=to_department, reason=reason)

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


def _delegated_transfer(data: TransferIn, request: Request, user: dict, *, patient_id: str,
                        to_department: str, reason: str) -> dict:
    """POST /transfers while ORB_ADT is on: the ADT writer moves the patient
    and audits inside its own transaction. The old request carries typed ward
    and bed text and an optional body idempotency key; with no key one is
    made here, so a retried request without one is refused as no change
    rather than recorded twice. The patient's scope was checked by the
    caller, on the sending side."""
    key = (request.headers.get("Idempotency-Key") or data.idempotency_key or "").strip()
    kwargs = {}
    if "expected_bed_id" in data.model_fields_set:
        kwargs["expected_bed_id"] = data.expected_bed_id
    has_bed = bool((data.to_bed or "").strip())
    has_ward = bool((data.to_ward or "").strip())
    try:
        return adt_transfer.transfer(
            None, patient_id=patient_id, actor=user, to_department=to_department,
            reason=reason, ward_text=data.to_ward, bed_text=data.to_bed,
            # The old form left ward and bed blank to keep them: the patient
            # stays where they lie. A ward with no bed is awaiting a bed there.
            keep_location=not has_bed and not has_ward,
            awaiting_bed=has_ward and not has_bed,
            boarder_acknowledged=data.boarder_acknowledged,
            require_expected_bed=False, notes=data.notes,
            idempotency_key=key or f"frontdesk-{uuid.uuid4().hex}",
            request_meta={
                "request_path": request.url.path,
                "http_method": request.method,
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
            },
            **kwargs,
        )
    except adt_core.AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=adt_core.http_detail(err))
    except AuditWriteError:
        raise HTTPException(status_code=503, detail={
            "error": "audit_unavailable",
            "message": ("The record of this transfer could not be written, so nothing was "
                        "changed. Try again in a moment."),
        })


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
