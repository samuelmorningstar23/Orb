"""Orb — Compliance & Trust Center + DPDP Act 2023 routes.

Two routers live here, and they are separate on purpose:

  * ``router`` (``/api/admin/compliance``) — GOVERNANCE. The live compliance
    posture report, the retention view, the data-principal export, erasure
    adjudication and the breach register. Every one is gated on
    ``admin.overview``: these are the records officer's surfaces, not the ward's.

  * ``consent_router`` (``/api/consent``) — BEDSIDE. Recording and withdrawing a
    patient's consent happens where the patient is, so it is gated on ordinary
    clinical permissions and department-scoped through ``require_patient_access``
    like any other per-patient route.

SCOPING. Every endpoint taking a patient id enforces the caller's department
scope and returns 404 (never 403) when out of scope — a 403 confirms the patient
exists, which is itself a disclosure. The data-principal export is the largest
PHI disclosure Orb can perform, so it carries an explicit, fail-closed
``phi_access`` audit row naming the sections and row counts it produced.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from permissions import (require_permission, require_patient_access,
                         enforce_patient_scope, department_scope, CONSENT_RECORD)
from services.audit_service import log_audit_event
from services.compliance_service import compliance_report
from services import consent_service as consent

router = APIRouter(prefix="/api/admin/compliance", tags=["compliance"])
consent_router = APIRouter(prefix="/api/consent", tags=["consent"])


# ════════════════════════════════════════════════════════════════
#  Request bodies
# ════════════════════════════════════════════════════════════════

class ConsentRequest(BaseModel):
    purpose: str
    method: str
    scope_detail: Optional[str] = None
    notice_version: Optional[str] = None
    notice_language: Optional[str] = None
    granted_at: Optional[str] = None
    expires_at: Optional[str] = None
    given_by: str = "patient"
    proxy_name: Optional[str] = None
    proxy_relationship: Optional[str] = None
    evidence_ref: Optional[str] = None


class WithdrawalRequest(BaseModel):
    method: str
    reason: Optional[str] = None


class ErasureRequestBody(BaseModel):
    patient_id: str
    categories: Optional[list[str]] = None
    requester_identity: str = "patient"
    channel: str = "in_person"
    note: Optional[str] = None


class BreachBody(BaseModel):
    nature: str
    description: str
    discovered_at: Optional[str] = None
    occurred_at: Optional[str] = None
    exposed_to: Optional[str] = None
    data_categories: Optional[list[str]] = None
    records_affected: Optional[int] = None
    principals_affected: Optional[int] = None
    affected_patient_ids: Optional[list[str]] = None
    containment_at: Optional[str] = None
    containment_actions: Optional[str] = None


class BreachNotificationBody(BaseModel):
    party_type: str = Field(..., description="board | data_principal | regulator | internal | other")
    notified_party: str
    method: Optional[str] = None
    reference: Optional[str] = None
    content_summary: Optional[str] = None
    notified_at: Optional[str] = None


def _audit(request: Request, user: dict, *, action_type: str, summary: str,
           category: str = "compliance", patient_id: str | None = None,
           target_type: str | None = None, target_id: str | None = None,
           metadata: dict | None = None, outcome: str = "success") -> None:
    """One audit row for a compliance action. BLOCKING — every route here is a
    sync `def`, which FastAPI runs in a threadpool, so this stays off the loop."""
    meta = {"department": department_scope(user), "scope_source": "authenticated_user"}
    if metadata:
        meta.update(metadata)
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category=category,
        action_type=action_type,
        outcome=outcome,
        summary_message=summary,
        actor_user_id=(user or {}).get("id"),
        actor_role=(user or {}).get("role"),
        patient_id=patient_id,
        target_type=target_type,
        target_id=target_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata=meta,
    )


# ════════════════════════════════════════════════════════════════
#  Trust Center posture (unchanged contract)
# ════════════════════════════════════════════════════════════════

@router.get("/")
def get_compliance_report(user: dict = Depends(require_permission("admin.overview"))):
    """Return the aggregated, live compliance posture report."""
    return compliance_report()


# ════════════════════════════════════════════════════════════════
#  Consent — bedside
# ════════════════════════════════════════════════════════════════

# ORDER MATTERS: this literal path must stay ABOVE `/{patient_id}` or FastAPI
# will match "vocabulary" as a patient id and 404 the whole vocabulary lookup.
@consent_router.get("/vocabulary")
def consent_vocabulary(user: dict = Depends(require_permission("patient.read_all"))):
    """The closed sets a consent form may use. Lets the UI render the same
    options the server will accept instead of guessing."""
    return {
        "purposes": [{"value": k, "label": v} for k, v in consent.CONSENT_PURPOSES.items()],
        "methods": [{"value": k, "label": v} for k, v in consent.CONSENT_METHODS.items()],
        "given_by": list(consent.GIVEN_BY),
    }


@consent_router.get("/{patient_id}")
def list_patient_consents(
    patient_id: str,
    user: dict = Depends(require_patient_access("patient.read_all",
                                                audit_action="consent_register_read")),
):
    """Every consent this patient gave, with withdrawals and effective status."""
    return {"patient_id": patient_id, "consents": consent.list_consents(patient_id)}


@consent_router.post("/{patient_id}")
def create_consent(
    patient_id: str,
    body: ConsentRequest,
    request: Request,
    user: dict = Depends(require_patient_access(CONSENT_RECORD)),
):
    """Record one consent. Immutable once written — see migration 14.

    GATED ON `consent.record`, which today means DOCTORS ONLY, the same set that
    held `patient.write` when these routes borrowed it. In practice a
    registration clerk takes the treatment consent at the front desk; giving
    that desk consent capture is now a grant in permissions.py and no longer
    hands it `patient.write` (editing the patient record) along the way.
    """
    try:
        rec = consent.record_consent(
            patient_id=patient_id,
            purpose=body.purpose,
            method=body.method,
            recorded_by=user["id"],
            recorded_by_role=user.get("role"),
            scope_detail=body.scope_detail,
            notice_version=body.notice_version,
            notice_language=body.notice_language,
            granted_at=body.granted_at,
            expires_at=body.expires_at,
            given_by=body.given_by,
            proxy_name=body.proxy_name,
            proxy_relationship=body.proxy_relationship,
            evidence_ref=body.evidence_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(request, user, action_type="consent_recorded",
           summary=f"Consent recorded for {patient_id}: {body.purpose} via {body.method}",
           patient_id=patient_id, target_type="consent_record", target_id=rec["id"],
           metadata={"purpose": body.purpose, "method": body.method,
                     "notice_version": body.notice_version})
    return rec


@consent_router.post("/{patient_id}/{consent_id}/withdraw")
def withdraw_patient_consent(
    patient_id: str,
    consent_id: str,
    body: WithdrawalRequest,
    request: Request,
    user: dict = Depends(require_patient_access(CONSENT_RECORD)),
):
    """Withdraw a consent. Writes a NEW row; the original is never edited."""
    existing = consent.get_consent(consent_id)
    if not existing or existing["patient_id"] != patient_id:
        # 404 rather than 403: the path already proved this caller may see this
        # patient, and a consent belonging to a different one must not be
        # confirmed to exist.
        raise HTTPException(status_code=404, detail="Consent record not found")
    try:
        wd = consent.withdraw_consent(
            consent_id=consent_id,
            recorded_by=user["id"],
            recorded_by_role=user.get("role"),
            method=body.method,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(request, user, action_type="consent_withdrawn",
           summary=f"Consent {consent_id} withdrawn for {patient_id}",
           patient_id=patient_id, target_type="consent_record", target_id=consent_id,
           metadata={"purpose": wd["purpose"],
                     "categories_for_review": wd["categories_for_review"]})
    return {"withdrawal": wd, "consent": consent.get_consent(consent_id)}


# ════════════════════════════════════════════════════════════════
#  Data-principal rights — the export
# ════════════════════════════════════════════════════════════════

@router.get("/data-principal/{patient_id}/export")
def export_data_principal(
    patient_id: str,
    request: Request,
    # THE SAME GUARD EVERY OTHER /{patient_id} ROUTE WEARS, and it is worn here
    # rather than open-coded deliberately. The first cut of this route did the
    # scope check by hand with enforce_patient_scope(); it was correct, and
    # tests/test_patient_scope_routes.py::
    # test_every_patient_id_route_that_returns_phi_carries_the_guard failed it
    # anyway, because the drift guard looks for the DEPENDENCY. That guard is
    # right and the hand-rolled version was wrong: the largest PHI disclosure in
    # the system is the last place to have its own private spelling of the ward
    # check. `admin.overview` keeps it admin-only; the dependency adds the 404
    # (never 403) and the cross-ward denial audit.
    #
    # No `audit_action=` — the explicit phi_access row below carries the section
    # and row counts, which a generic read audit cannot.
    user: dict = Depends(require_patient_access("admin.overview")),
):
    """Everything Orb holds about one patient (DPDP s.11).

    THIS ENDPOINT IS ITSELF A PHI DISCLOSURE — the largest one in the system. It
    is admin-only, department-scoped, and audited on the fail-closed
    ``phi_access`` category, so if the audit row cannot be persisted the export
    does not return.
    """
    try:
        payload = consent.data_principal_export(patient_id)
    except ValueError:
        # Same 404 as an out-of-scope patient: an admin probing ids learns
        # nothing from the difference.
        raise HTTPException(status_code=404, detail="Patient not found")

    _audit(request, user, category="phi_access", action_type="data_principal_export",
           summary=(f"DPDP data-principal export for {patient_id}: "
                    f"{payload['record_count']} records across "
                    f"{payload['section_count']} sections"),
           patient_id=patient_id, target_type="patient", target_id=patient_id,
           metadata={
               "record_count": payload["record_count"],
               "section_count": payload["section_count"],
               "sections": {k: v["row_count"] for k, v in payload["sections"].items()},
               "basis": "DPDP Act 2023 s.11",
           })
    return payload


# ════════════════════════════════════════════════════════════════
#  Retention
# ════════════════════════════════════════════════════════════════

@router.get("/retention")
def get_retention(
    request: Request,
    patient_id: Optional[str] = None,
    user: dict = Depends(require_permission("admin.overview")),
):
    """The retention matrix plus what is past it. Deletes nothing, ever."""
    if patient_id:
        enforce_patient_scope(user, patient_id)
    return {
        "policies": consent.list_retention_policies(),
        "review": consent.retention_review(patient_id=patient_id),
    }


# ════════════════════════════════════════════════════════════════
#  Erasure
# ════════════════════════════════════════════════════════════════

@router.post("/erasure")
def create_erasure_request(
    body: ErasureRequestBody,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """Record a right-to-erasure request. Decides nothing, deletes nothing."""
    enforce_patient_scope(user, body.patient_id)
    try:
        req = consent.record_erasure_request(
            patient_id=body.patient_id,
            requested_by=user["id"],
            categories=body.categories,
            requester_identity=body.requester_identity,
            channel=body.channel,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(request, user, action_type="erasure_request_recorded",
           summary=f"Erasure request recorded for {body.patient_id}",
           patient_id=body.patient_id, target_type="erasure_request",
           target_id=req["id"], metadata={"categories": body.categories})
    return req


@router.get("/erasure")
def list_erasure(
    patient_id: Optional[str] = None,
    status: Optional[str] = None,
    user: dict = Depends(require_permission("admin.overview")),
):
    if patient_id:
        enforce_patient_scope(user, patient_id)
    # Every row names a patient, so the LISTING is ward-scoped too — not just
    # the by-id lookups. department_scope() returns None for a genuinely
    # unscoped governance account, which the service reads as "no filter".
    return {"requests": consent.list_erasure_requests(
        patient_id=patient_id, status=status, department=department_scope(user))}


@router.get("/erasure/{request_id}")
def get_erasure(
    request_id: str,
    user: dict = Depends(require_permission("admin.overview")),
):
    try:
        detail = consent.get_erasure_request(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Erasure request not found")
    enforce_patient_scope(user, detail["request"]["patient_id"])
    return detail


@router.post("/erasure/{request_id}/adjudicate")
def adjudicate_erasure(
    request_id: str,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """Apply the retention matrix: honour what may be erased, refuse the rest
    with a stated reason and the authority that compels retention."""
    try:
        detail = consent.get_erasure_request(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Erasure request not found")
    enforce_patient_scope(user, detail["request"]["patient_id"])
    try:
        result = consent.adjudicate_erasure_request(request_id, decided_by=user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(request, user, action_type="erasure_request_adjudicated",
           summary=(f"Erasure request {request_id} decided: "
                    f"{result['honoured_count']} honoured, "
                    f"{result['refused_count']} refused"),
           patient_id=result["patient_id"], target_type="erasure_request",
           target_id=request_id,
           metadata={"honoured": result["honoured_count"],
                     "refused": result["refused_count"],
                     "executable": result["executable_count"]})
    return result


@router.post("/erasure/{request_id}/execute")
def execute_erasure(
    request_id: str,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """Carry out only what was adjudicated honourable AND executable."""
    try:
        detail = consent.get_erasure_request(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Erasure request not found")
    enforce_patient_scope(user, detail["request"]["patient_id"])

    # INTENT FIRST, and this ordering is the point. Erasure is irreversible, and
    # `phi_access` is a FAIL-CLOSED audit category — so writing the intent row
    # before touching any data means a failed audit write aborts the request with
    # nothing destroyed. Auditing only afterwards would put the one case that
    # matters (audit unavailable) on the wrong side of the line: data gone, no
    # record that anyone asked for it.
    _audit(request, user, category="phi_access", action_type="erasure_execution_started",
           summary=(f"About to execute erasure request {request_id} for patient "
                    f"{detail['request']['patient_id']}"),
           patient_id=detail["request"]["patient_id"], target_type="erasure_request",
           target_id=request_id,
           metadata={"categories": [d["category"] for d in detail["decisions"]
                                    if d["disposition"] == "honoured" and d["executable"]]})

    try:
        result = consent.execute_erasure_request(request_id, executed_by=user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # And the outcome row, also fail-closed: what was actually destroyed. Audio
    # files are removed after the commit, so one that would not go is named
    # here and the row says partial rather than success.
    left = result.get("files_not_removed") or []
    _audit(request, user, category="phi_access", action_type="erasure_executed",
           outcome="partial_success" if left else "success",
           summary=(f"Erasure request {request_id} executed: "
                    f"{result['rows_erased']} rows erased"
                    + (f"; {len(left)} audio file(s) could not be removed" if left else "")),
           patient_id=result["patient_id"], target_type="erasure_request",
           target_id=request_id,
           metadata={"rows_erased": result["rows_erased"],
                     "categories": [c["category"] for c in result["categories"]],
                     "files_not_removed": left,
                     "retained": [t["table"] for t in result.get("retained") or []]})
    return result


# ════════════════════════════════════════════════════════════════
#  Breach register
# ════════════════════════════════════════════════════════════════

@router.post("/breaches")
def create_breach(
    body: BreachBody,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    try:
        rec = consent.record_breach(
            nature=body.nature,
            description=body.description,
            reported_by=user["id"],
            discovered_at=body.discovered_at,
            occurred_at=body.occurred_at,
            exposed_to=body.exposed_to,
            data_categories=body.data_categories,
            records_affected=body.records_affected,
            principals_affected=body.principals_affected,
            affected_patient_ids=body.affected_patient_ids,
            containment_at=body.containment_at,
            containment_actions=body.containment_actions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(request, user, category="security", action_type="breach_recorded",
           summary=f"Personal data breach recorded: {body.nature}",
           target_type="breach_record", target_id=rec["id"],
           metadata={"nature": body.nature,
                     "principals_affected": body.principals_affected})
    return rec


@router.get("/breaches")
def get_breaches(user: dict = Depends(require_permission("admin.overview"))):
    return {"breaches": consent.list_breaches()}


@router.get("/breaches/{breach_id}")
def get_breach_detail(breach_id: str,
                      user: dict = Depends(require_permission("admin.overview"))):
    """One breach. The incident is visible hospital-wide; the list of affected
    PATIENTS is filtered to the caller's ward, with the withheld count stated."""
    try:
        return consent.get_breach(breach_id, department=department_scope(user))
    except ValueError:
        raise HTTPException(status_code=404, detail="Breach record not found")


@router.post("/breaches/{breach_id}/notifications")
def create_breach_notification(
    breach_id: str,
    body: BreachNotificationBody,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """Record that the Board, or the affected people, were told — and when."""
    try:
        notif = consent.record_breach_notification(
            breach_id=breach_id,
            party_type=body.party_type,
            notified_party=body.notified_party,
            method=body.method,
            reference=body.reference,
            content_summary=body.content_summary,
            notified_at=body.notified_at,
            recorded_by=user["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(request, user, category="security", action_type="breach_notification_recorded",
           summary=(f"Breach {breach_id} notification to {body.party_type}: "
                    f"{body.notified_party}"),
           target_type="breach_record", target_id=breach_id,
           metadata={"party_type": body.party_type, "reference": body.reference})
    return notif
