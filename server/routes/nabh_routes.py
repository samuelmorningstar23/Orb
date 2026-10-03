"""Orb — NABH accreditation evidence API.

The incident / adverse-event register, the quality-indicator dashboard, and the
date-ranged evidence pack a hospital hands an assessor.

════════════════════════════════════════════════════════════════════════════════
 FIVE THINGS THIS ROUTER IS CAREFUL ABOUT
════════════════════════════════════════════════════════════════════════════════

1. IT IS NOT A CLINICAL PATH. Nothing here writes to medications, vitals,
   alerts, orders, admissions, escalations or the shadow-mode evidence capture,
   and nothing here may ever block one. Reporting an incident does not stop a
   dose, and recording a discharge disposition does not discharge anybody.

2. IT IS BEHIND A MODULE SWITCH. `config.NABH_ENABLED` (env `ORB_NABH`, DEFAULT
   ON per the 2026-08-11 ruling). A site that runs its quality department
   elsewhere sets it to 0; every route below then answers 404 with a body that
   says WHY, so a stale client gets an explanation rather than a mystery.

3. ANY STAFF ACCOUNT CAN REPORT; ONLY THE QUALITY DESK CAN ADJUDICATE.
   Reporting is deliberately the lowest-barrier write in this module — a
   register only a manager can write to is a register nobody writes to, and the
   near misses stop arriving first. Investigating, closing, voiding, recording a
   disposition, reading the register and generating a pack all need
   `admin.overview`.

   PERMISSION DEBT, STATED PLAINLY: Orb has no `quality.*` permission and no
   quality-manager role, and `permissions.py` is outside this module's scope. So
   reporting is gated on a ROLE ALLOW-LIST (`REPORTER_ROLES`) rather than on a
   permission, and everything else borrows `admin.overview`. Two consequences,
   both wrong for a real deployment and both recorded as follow-ups: a hospital
   cannot give its quality manager the register without making them a full
   admin, and a role added to `permissions.py` tomorrow will not be able to file
   a report until somebody edits the list here.

4. THE ANONYMOUS REPORT PATH NEVER RECEIVES THE CALLER'S IDENTITY. `report()`
   below does not pass `user["id"]` into the service when `is_anonymous` is set —
   there is no variable holding it at the point of insert — and the module-level
   audit event it writes carries no actor either. What it CANNOT do anything
   about is services/audit_middleware.py, which logs every POST with the bearer
   token's subject and the request path. That residual is printed to the user by
   `nabh_service.ANONYMITY_STATEMENT` on the form itself, and a follow-up asks
   for the middleware exemption that would close it.

5. A GET THAT TAKES `patient_id` OFF THE QUERY STRING IS SCOPED. `list()` calls
   `enforce_patient_scope`, because a register filtered by a caller-chosen
   patient id is a per-patient PHI read in a spelling the path-param drift guard
   cannot see (tests/test_phi_read_scope_gaps.py).

All handlers are plain `def`, so FastAPI runs them in a threadpool: everything
below opens SQLite, and blocking the event loop is the failure mode this repo
has shipped three production freezes from.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

import config
from auth import get_current_user
from permissions import (
    ADMIN_OVERVIEW,
    enforce_patient_scope,
    require_permission,
)
from services import nabh_service as svc
from services.audit_service import log_audit_event

logger = logging.getLogger("orb.nabh_routes")

router = APIRouter(prefix="/api/nabh", tags=["NABH Accreditation Evidence"])


# ── Module switch ────────────────────────────────────────────────────────────

#: Roles that may file a report. NOT a permission check, because Orb has no
#: `quality.report` permission and `permissions.py` is outside this module's
#: scope — see the PERMISSION DEBT note in the module docstring.
#:
#: It is an ALLOW-LIST of staff roles rather than a deny-list of `patient`,
#: because a deny-list means the next role somebody adds gets the register by
#: default, and the one role that must never reach it is the patient-facing one.
#: `pharmacist` is in the list deliberately: a pharmacist is the person most
#: likely to catch a medication error, and they hold neither PATIENT_READ_ALL
#: nor ADMIN_OVERVIEW, so any permission-based gate here would have excluded
#: exactly the wrong person.
REPORTER_ROLES = frozenset({"doctor", "clinician", "nurse", "pharmacist", "admin"})


def staff_reporter(user: dict = Depends(get_current_user)) -> dict:
    """Any staff account may file a report. A patient account may not."""
    if (user.get("role") or "") not in REPORTER_ROLES:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_staff",
                "message": ("The incident register is a staff surface. This "
                            "account cannot file a report."),
            },
        )
    return user


def require_module() -> None:
    """404 when the site has switched the NABH module off.

    404 rather than 403: the module is not disallowed for this user, it is not
    present at this deployment. The body says so, because a client built before
    the flag flipped deserves a sentence rather than a blank."""
    if not config.NABH_ENABLED:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "module_disabled",
                "module": "nabh",
                "message": (
                    "The NABH accreditation evidence module is switched off for "
                    "this deployment (ORB_NABH=0). This site keeps its quality "
                    "register elsewhere."
                ),
            },
        )


def _fail(exc: svc.NabhError) -> HTTPException:
    """Map a service refusal to HTTP, carrying the machine-readable code so a
    client can branch on it without matching English."""
    status = {
        "not_found": 404,
        "illegal_transition": 409,
        "open_actions": 409,
        "investigation_required": 409,
        "no_action_reason_required": 409,
        "integrity": 409,
        "bad_value": 422,
        "missing_field": 422,
        "bad_period": 422,
        "future_incident": 422,
        "future_disposition": 422,
    }.get(exc.code, 400)
    return HTTPException(status_code=status,
                         detail={"error": exc.code, "message": str(exc)})


def _audit(request: Request, user: Optional[dict], action: str, *, summary: str,
           target_type: str, target_id: Optional[str] = None,
           metadata: Optional[dict] = None) -> None:
    """Every NABH write leaves a trail.

    Category is `security` (fail-closed, awaited) rather than `clinical_access`:
    an incident report is an administrative act on a quality register, and
    mixing it into the clinical categories would dilute the trail a clinical
    investigation reads.

    `user` is None for an anonymous report, and then NO actor is recorded here.
    Passing the actor "just for the audit" would put the reporter's id one
    `audit.read` away from their anonymous report, which is the whole thing the
    register promises not to do.

    `patient_id` is deliberately never passed. An incident may name a patient,
    but an audit row for "somebody filed a report" is not the place to widen the
    patient's access trail.
    """
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome="success",
        summary_message=summary,
        actor_user_id=(user or {}).get("id"),
        actor_role=(user or {}).get("role"),
        target_type=target_type,
        target_id=target_id,
        request_path=str(request.url.path),
        http_method=request.method,
        metadata=metadata,
    )


# ── Request bodies ───────────────────────────────────────────────────────────

class IncidentIn(BaseModel):
    incident_type: str
    severity: str
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=8000)
    occurred_at: Optional[str] = None
    ward: Optional[str] = None
    department: Optional[str] = None
    location: Optional[str] = None
    patient_id: Optional[str] = None
    #: When true the register stores no reporter id and no reporter role, and
    #: the reporting time is truncated to the day. See ANONYMITY_STATEMENT.
    is_anonymous: bool = False
    externally_reportable: Optional[bool] = None


class NoteIn(BaseModel):
    note: Optional[str] = None


class InvestigationIn(BaseModel):
    summary: str = Field(min_length=1, max_length=8000)
    root_cause: Optional[str] = None
    contributing_factors: Optional[list[str]] = None


class ActionIn(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    action_type: str = "corrective"
    owner_user_id: Optional[str] = None
    owner_description: Optional[str] = None
    due_date: Optional[str] = None


class CloseIn(BaseModel):
    closure_note: Optional[str] = None
    no_action_reason: Optional[str] = None


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class DispositionIn(BaseModel):
    patient_id: str
    disposition: str
    occurred_at: Optional[str] = None
    admission_id: Optional[str] = None
    notes: Optional[str] = None


class ExportIn(BaseModel):
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    ward: Optional[str] = None
    #: DEFAULT FALSE, and it stays false unless somebody deliberately asks. A
    #: pack that leaves the hospital is the wrong place to discover it carried
    #: patient identifiers nobody needed.
    include_patient_identifiers: bool = False


# ── Meta ─────────────────────────────────────────────────────────────────────

@router.get("/meta")
def get_meta(_: None = Depends(require_module),
             user: dict = Depends(staff_reporter)) -> dict:
    """The vocabulary the client builds its forms from — types, severities, the
    lifecycle, the indicator catalogue, the thresholds each indicator was
    computed under, and the anonymity statement."""
    return svc.meta()


# ── Indicators ───────────────────────────────────────────────────────────────

@router.get("/indicators")
def get_indicators(
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(ADMIN_OVERVIEW)),
    period_start: Optional[str] = Query(None, description="ISO date, inclusive"),
    period_end: Optional[str] = Query(None, description="ISO date, inclusive"),
    ward: Optional[str] = Query(None),
) -> dict:
    """The quality-indicator dashboard for one period.

    Never raises on an empty database: a hospital that switched Orb on this
    morning gets a complete dashboard in which every indicator says what it is
    missing. That is the point of the module.
    """
    try:
        return svc.indicators(period_start=period_start, period_end=period_end,
                              ward=ward)
    except svc.NabhError as exc:
        raise _fail(exc) from exc


# ── Incident register ────────────────────────────────────────────────────────

@router.post("/incidents", status_code=201)
def report(request: Request, body: IncidentIn,
           _: None = Depends(require_module),
           user: dict = Depends(staff_reporter)) -> dict:
    """Report an incident, adverse event or near miss.

    THE LOWEST-PERMISSION WRITE IN THIS MODULE, on purpose. A register that only
    a manager can write to is a register nobody writes to, and the near misses —
    the only free lesson a hospital gets — are exactly what stops arriving first.

    ANONYMOUS REPORTS: the caller's identity is not passed to the service and no
    actor is recorded on this route's audit event. See the module docstring for
    what that does and does not guarantee.

    THE OPTIONAL `patient_id` IS WARD-SCOPED. A reporter may name a patient, and
    a caller-named patient id on a MUTATION is the defect class
    tests/test_caller_named_patient_scope.py exists for: without this, a
    Cardiology nurse could attach a report to an Oncology patient they cannot
    otherwise see, and then read the description back through the register. The
    refusal is a 404 — never a 403, which would confirm the patient exists.

    Scoping the report does NOT gate the report. A reporter who names a patient
    outside their ward is refused THAT LINKAGE, and the incident itself can
    still be filed with no patient on it. Losing a near miss because the id was
    wrong would be a worse trade than the disclosure it prevents.
    """
    enforce_patient_scope(user, body.patient_id)
    try:
        incident = svc.report_incident(
            incident_type=body.incident_type,
            severity=body.severity,
            title=body.title,
            description=body.description,
            occurred_at=body.occurred_at,
            ward=body.ward,
            department=body.department,
            location=body.location,
            patient_id=body.patient_id,
            is_anonymous=body.is_anonymous,
            reported_by=None if body.is_anonymous else user["id"],
            reporter_role=None if body.is_anonymous else user.get("role"),
            externally_reportable=body.externally_reportable,
        )
    except svc.NabhError as exc:
        raise _fail(exc) from exc

    _audit(request, None if body.is_anonymous else user, "nabh_incident_reported",
           summary=(f"Incident {incident['reference']} reported "
                    f"({incident['incident_type']}, {incident['severity']})"
                    + (", anonymous" if body.is_anonymous else "")),
           target_type="nabh_incident", target_id=incident["reference"],
           metadata={"anonymous": bool(body.is_anonymous),
                     "severity": incident["severity"],
                     "incident_type": incident["incident_type"]})
    return {"incident": incident, "anonymity_statement": svc.ANONYMITY_STATEMENT}


@router.get("/incidents")
def list_register(
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(ADMIN_OVERVIEW)),
    period_start: Optional[str] = Query(None),
    period_end: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    incident_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    ward: Optional[str] = Query(None),
    patient_id: Optional[str] = Query(None),
    include_void: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """The register, filtered.

    `patient_id` arrives on the QUERY STRING, which makes this a per-patient PHI
    read that the path-param drift guard cannot see — so it goes through
    `enforce_patient_scope` (404, never 403: a 403 confirms the patient exists).
    """
    enforce_patient_scope(user, patient_id)
    try:
        return svc.list_incidents(
            period_start=period_start, period_end=period_end, status=status,
            incident_type=incident_type, severity=severity, ward=ward,
            patient_id=patient_id, include_void=include_void, limit=limit)
    except svc.NabhError as exc:
        raise _fail(exc) from exc


@router.get("/incidents/{incident_id}")
def get_one(incident_id: int, _: None = Depends(require_module),
            user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    try:
        return svc.get_incident(incident_id)
    except svc.NabhError as exc:
        raise _fail(exc) from exc


@router.post("/incidents/{incident_id}/investigate")
def investigate(request: Request, incident_id: int, body: NoteIn,
                _: None = Depends(require_module),
                user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    """Take the incident on: `reported` → `under_investigation`. Also the
    REOPEN path from `closed`, which is recorded as a reopen, never as an edit
    that erases the closure."""
    try:
        incident = svc.start_investigation(incident_id, actor_user_id=user["id"],
                                           note=body.note)
    except svc.NabhError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "nabh_incident_investigation_started",
           summary=f"Investigation started on {incident['reference']}",
           target_type="nabh_incident", target_id=incident["reference"])
    return incident


@router.post("/incidents/{incident_id}/investigation")
def record_investigation(request: Request, incident_id: int, body: InvestigationIn,
                         _: None = Depends(require_module),
                         user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    try:
        incident = svc.record_investigation(
            incident_id, actor_user_id=user["id"], summary=body.summary,
            root_cause=body.root_cause,
            contributing_factors=body.contributing_factors)
    except svc.NabhError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "nabh_incident_investigated",
           summary=f"Investigation recorded on {incident['reference']}",
           target_type="nabh_incident", target_id=incident["reference"])
    return incident


@router.post("/incidents/{incident_id}/actions", status_code=201)
def add_action(request: Request, incident_id: int, body: ActionIn,
               _: None = Depends(require_module),
               user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    try:
        incident = svc.add_action(
            incident_id, actor_user_id=user["id"], description=body.description,
            action_type=body.action_type, owner_user_id=body.owner_user_id,
            owner_description=body.owner_description, due_date=body.due_date)
    except svc.NabhError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "nabh_incident_action_added",
           summary=f"Corrective action added to {incident['reference']}",
           target_type="nabh_incident", target_id=incident["reference"])
    return incident


@router.post("/incidents/{incident_id}/actions/{action_id}/complete")
def complete_action(request: Request, incident_id: int, action_id: int, body: NoteIn,
                    _: None = Depends(require_module),
                    user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    try:
        incident = svc.complete_action(incident_id, action_id,
                                       actor_user_id=user["id"], note=body.note)
    except svc.NabhError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "nabh_incident_action_completed",
           summary=f"Action {action_id} completed on {incident['reference']}",
           target_type="nabh_incident", target_id=incident["reference"])
    return incident


@router.post("/incidents/{incident_id}/close")
def close(request: Request, incident_id: int, body: CloseIn,
          _: None = Depends(require_module),
          user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    """Close an incident. Refuses on an unrecorded investigation, on outstanding
    open actions, and on a closure with neither an action nor a reason."""
    try:
        incident = svc.close_incident(incident_id, actor_user_id=user["id"],
                                      closure_note=body.closure_note,
                                      no_action_reason=body.no_action_reason)
    except svc.NabhError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "nabh_incident_closed",
           summary=f"Incident {incident['reference']} closed",
           target_type="nabh_incident", target_id=incident["reference"])
    return incident


@router.post("/incidents/{incident_id}/void")
def void(request: Request, incident_id: int, body: ReasonIn,
         _: None = Depends(require_module),
         user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    """Mark a report void — a duplicate, or something that turned out not to
    have happened. THE ROW STAYS: there is no delete in this module."""
    try:
        incident = svc.void_incident(incident_id, actor_user_id=user["id"],
                                     reason=body.reason)
    except svc.NabhError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "nabh_incident_voided",
           summary=f"Incident {incident['reference']} voided",
           target_type="nabh_incident", target_id=incident["reference"],
           metadata={"reason": body.reason})
    return incident


# ── Discharge disposition ────────────────────────────────────────────────────

@router.post("/dispositions", status_code=201)
def record_disposition(request: Request, body: DispositionIn,
                       _: None = Depends(require_module),
                       user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    """Record how one admission ended — routine, referred, LAMA, DAMA,
    absconded or death.

    NOT A DISCHARGE. This writes one row to this module's own table. It does not
    touch `patients.status`, does not create a discharge summary, and cannot
    block or delay a discharge. It exists because mortality and LAMA are not
    derivable from anything else Orb holds.

    THE PATIENT IS NAMED BY THE CALLER, so it is ward-scoped
    (tests/test_caller_named_patient_scope.py). Recording a death against a
    patient in a ward the caller cannot see is both a disclosure and a way to
    corrupt another ward's mortality figure.
    """
    enforce_patient_scope(user, body.patient_id)
    try:
        row = svc.record_disposition(
            patient_id=body.patient_id, disposition=body.disposition,
            occurred_at=body.occurred_at, admission_id=body.admission_id,
            recorded_by=user["id"], notes=body.notes)
    except svc.NabhError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "nabh_disposition_recorded",
           summary=f"Discharge disposition recorded: {body.disposition}",
           target_type="nabh_disposition", target_id=str(row.get("id")),
           metadata={"disposition": body.disposition})
    return row


@router.get("/dispositions")
def list_dispositions(_: None = Depends(require_module),
                      user: dict = Depends(require_permission(ADMIN_OVERVIEW)),
                      period_start: Optional[str] = Query(None),
                      period_end: Optional[str] = Query(None),
                      limit: int = Query(500, ge=1, le=2000)) -> dict:
    try:
        return svc.list_dispositions(period_start=period_start,
                                     period_end=period_end, limit=limit)
    except svc.NabhError as exc:
        raise _fail(exc) from exc


# ── Evidence pack ────────────────────────────────────────────────────────────

@router.post("/export")
def export_pack(request: Request, body: ExportIn,
                _: None = Depends(require_module),
                user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    """Generate the date-ranged evidence pack AND log that it was generated.

    POST, not GET, because it writes: `nabh_evidence_exports` records who
    produced which period, with what options, and the SHA-256 of the document.
    "Which pack did we hand the assessor in March, and is this that file" is a
    question a hospital gets asked.
    """
    try:
        pack = svc.record_evidence_export(
            period_start=body.period_start, period_end=body.period_end,
            ward=body.ward, generated_by=user["id"],
            include_patient_identifiers=body.include_patient_identifiers)
    except svc.NabhError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "nabh_evidence_pack_generated",
           summary=(f"NABH evidence pack {pack['reference']} generated for "
                    f"{pack['period']['start_date']}..{pack['period']['end_date']}"),
           target_type="nabh_evidence_pack", target_id=pack["reference"],
           metadata={"sha256": pack["content_sha256"],
                     "include_patient_identifiers": body.include_patient_identifiers,
                     "indicators_reported": pack["indicator_summary"]["reported"],
                     "indicators_not_collected":
                         pack["indicator_summary"]["not_collected"]})
    return pack


@router.post("/export.md")
def export_pack_markdown(request: Request, body: ExportIn,
                         _: None = Depends(require_module),
                         user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> Response:
    """The same pack, rendered as a Markdown document a person can read.

    Same honesty rule as the JSON: a non-reported indicator prints INSUFFICIENT
    DATA or NOT COLLECTED in the result column, never a number and never a dash
    that somebody downstream could read as a zero.
    """
    try:
        pack = svc.record_evidence_export(
            period_start=body.period_start, period_end=body.period_end,
            ward=body.ward, generated_by=user["id"],
            include_patient_identifiers=body.include_patient_identifiers)
    except svc.NabhError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "nabh_evidence_pack_generated",
           summary=f"NABH evidence pack {pack['reference']} generated (markdown)",
           target_type="nabh_evidence_pack", target_id=pack["reference"],
           metadata={"sha256": pack["content_sha256"], "format": "markdown"})
    return Response(
        content=svc.render_pack_markdown(pack),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="nabh-pack-{pack["reference"]}.md"'},
    )


@router.get("/exports")
def list_exports(_: None = Depends(require_module),
                 user: dict = Depends(require_permission(ADMIN_OVERVIEW)),
                 limit: int = Query(50, ge=1, le=500)) -> dict:
    """Every pack this hospital has produced, newest first, with its digest."""
    return svc.list_evidence_exports(limit=limit)
