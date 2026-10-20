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

FACILITIES (multisite-9b). With more than one facility of a group on the
appliance, an account bound to one facility (permissions.facility_scope) sees
the erasure requests of its facility's patients only, and only its facility's
patients among a breach's affected people (the breach itself is the legal
entity's and stays visible, with the number withheld stated). The retention
review counts every facility's records, so it is answered for a named
patient, or to an account with group-wide access (403 group_wide_only
otherwise). One facility on record: the scope is None and nothing changes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Request
from permissions import (
    CONSENT_RECORD,
    department_scope,
    enforce_patient_scope,
    facility_scope,
    require_patient_access,
    require_permission,
    scope_patient_ids_sql,
)
from pydantic import BaseModel, Field
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
    scope_detail: str | None = None
    notice_version: str | None = None
    notice_language: str | None = None
    granted_at: str | None = None
    expires_at: str | None = None
    given_by: str = "patient"
    proxy_name: str | None = None
    proxy_relationship: str | None = None
    evidence_ref: str | None = None


class WithdrawalRequest(BaseModel):
    method: str
    reason: str | None = None


class ErasureRequestBody(BaseModel):
    patient_id: str
    categories: list[str] | None = None
    requester_identity: str = "patient"
    channel: str = "in_person"
    note: str | None = None


class BreachBody(BaseModel):
    nature: str
    description: str
    discovered_at: str | None = None
    occurred_at: str | None = None
    exposed_to: str | None = None
    data_categories: list[str] | None = None
    records_affected: int | None = None
    principals_affected: int | None = None
    affected_patient_ids: list[str] | None = None
    containment_at: str | None = None
    containment_actions: str | None = None


class BreachNotificationBody(BaseModel):
    party_type: str = Field(
        ..., description="board | data_principal | regulator | internal | other"
    )
    notified_party: str
    method: str | None = None
    reference: str | None = None
    content_summary: str | None = None
    notified_at: str | None = None


def _patients_at(ids: Iterable[str | None], facilities: frozenset[int]) -> set[str]:
    """The patient ids among `ids` whose latest encounter is at `facilities`
    (the one scope derivation, permissions.scope_patient_ids_sql)."""
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return set()
    scope_sql, scope_params = scope_patient_ids_sql(None, facilities=facilities)
    out: set[str] = set()
    with db_session() as db:
        for i in range(0, len(wanted), 500):
            chunk = wanted[i : i + 500]
            marks = ", ".join("?" for _ in chunk)
            out |= {
                str(r[0])
                for r in db.execute(
                    f"SELECT id FROM ({scope_sql}) WHERE id IN ({marks})",
                    (*scope_params, *chunk),
                ).fetchall()
            }
    return out


def _audit(
    request: Request,
    user: dict,
    *,
    action_type: str,
    summary: str,
    category: str = "compliance",
    patient_id: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    metadata: dict | None = None,
    outcome: str = "success",
) -> None:
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
        target_id=str(target_id) if target_id is not None else None,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata=meta,
    )


# ════════════════════════════════════════════════════════════════
#  Trust Center posture (unchanged contract)
# ════════════════════════════════════════════════════════════════


_GET_COMPLIANCE_REPORT_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.get("/")
def get_compliance_report(user: dict = _GET_COMPLIANCE_REPORT_USER_DEFAULT) -> dict:
    """Return the aggregated, live compliance posture report. The audit
    coverage counts the reader's facilities' rows (multisite-11)."""
    return compliance_report(user=user)


# ════════════════════════════════════════════════════════════════
#  Consent — bedside
# ════════════════════════════════════════════════════════════════


# ORDER MATTERS: this literal path must stay ABOVE `/{patient_id}` or FastAPI
# will match "vocabulary" as a patient id and 404 the whole vocabulary lookup.
_CONSENT_VOCABULARY_USER_DEFAULT = Depends(require_permission("patient.read_all"))


@consent_router.get("/vocabulary")
def consent_vocabulary(user: dict = _CONSENT_VOCABULARY_USER_DEFAULT):
    """The closed sets a consent form may use. Lets the UI render the same
    options the server will accept instead of guessing."""
    return {
        "purposes": [{"value": k, "label": v} for k, v in consent.CONSENT_PURPOSES.items()],
        "methods": [{"value": k, "label": v} for k, v in consent.CONSENT_METHODS.items()],
        "given_by": list(consent.GIVEN_BY),
    }


_LIST_PATIENT_CONSENTS_USER_DEFAULT = Depends(
    require_patient_access("patient.read_all", audit_action="consent_register_read")
)


@consent_router.get("/{patient_id}")
def list_patient_consents(
    patient_id: str,
    user: dict = _LIST_PATIENT_CONSENTS_USER_DEFAULT,
):
    """Every consent this patient gave, with withdrawals and effective status."""
    return {"patient_id": patient_id, "consents": consent.list_consents(patient_id)}


_CREATE_CONSENT_USER_DEFAULT = Depends(require_patient_access(CONSENT_RECORD))


@consent_router.post("/{patient_id}")
def create_consent(
    patient_id: str,
    body: ConsentRequest,
    request: Request,
    user: dict = _CREATE_CONSENT_USER_DEFAULT,
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        user,
        action_type="consent_recorded",
        summary=f"Consent recorded for {patient_id}: {body.purpose} via {body.method}",
        patient_id=patient_id,
        target_type="consent_record",
        target_id=rec["id"],
        metadata={
            "purpose": body.purpose,
            "method": body.method,
            "notice_version": body.notice_version,
        },
    )
    return rec


_WITHDRAW_PATIENT_CONSENT_USER_DEFAULT = Depends(require_patient_access(CONSENT_RECORD))


@consent_router.post("/{patient_id}/{consent_id}/withdraw")
def withdraw_patient_consent(
    patient_id: str,
    consent_id: str,
    body: WithdrawalRequest,
    request: Request,
    user: dict = _WITHDRAW_PATIENT_CONSENT_USER_DEFAULT,
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
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        request,
        user,
        action_type="consent_withdrawn",
        summary=f"Consent {consent_id} withdrawn for {patient_id}",
        patient_id=patient_id,
        target_type="consent_record",
        target_id=consent_id,
        metadata={"purpose": wd["purpose"], "categories_for_review": wd["categories_for_review"]},
    )
    return {"withdrawal": wd, "consent": consent.get_consent(consent_id)}


# ════════════════════════════════════════════════════════════════
#  Data-principal rights — the export
# ════════════════════════════════════════════════════════════════


_EXPORT_DATA_PRINCIPAL_USER_DEFAULT = Depends(require_patient_access("admin.overview"))


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
    user: dict = _EXPORT_DATA_PRINCIPAL_USER_DEFAULT,
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
        raise HTTPException(status_code=404, detail="Patient not found") from None

    _audit(
        request,
        user,
        category="phi_access",
        action_type="data_principal_export",
        summary=(
            f"DPDP data-principal export for {patient_id}: "
            f"{payload['record_count']} records across "
            f"{payload['section_count']} sections"
        ),
        patient_id=patient_id,
        target_type="patient",
        target_id=patient_id,
        metadata={
            "record_count": payload["record_count"],
            "section_count": payload["section_count"],
            "sections": {k: v["row_count"] for k, v in payload["sections"].items()},
            "basis": "DPDP Act 2023 s.11",
        },
    )
    return payload


# ════════════════════════════════════════════════════════════════
#  Retention
# ════════════════════════════════════════════════════════════════


_GET_RETENTION_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.get("/retention", response_model=None)
def get_retention(
    request: Request,
    patient_id: str | None = None,
    user: dict = _GET_RETENTION_USER_DEFAULT,
) -> dict:
    """The retention matrix plus what is past it. Deletes nothing, ever.

    Without a patient the review counts every facility's records, so a
    facility-bound account gets it only for a patient it names (multisite-9b).
    """
    if patient_id:
        enforce_patient_scope(user, patient_id)
    elif facility_scope(user) is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "group_wide_only",
                "message": (
                    "The retention review counts the records of every facility of the "
                    "group, so it needs an account with group-wide access. Name a "
                    "patient to see theirs."
                ),
            },
        )
    return {
        "policies": consent.list_retention_policies(),
        "review": consent.retention_review(patient_id=patient_id),
    }


# ════════════════════════════════════════════════════════════════
#  Erasure
# ════════════════════════════════════════════════════════════════


_CREATE_ERASURE_REQUEST_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/erasure")
def create_erasure_request(
    body: ErasureRequestBody,
    request: Request,
    user: dict = _CREATE_ERASURE_REQUEST_USER_DEFAULT,
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        user,
        action_type="erasure_request_recorded",
        summary=f"Erasure request recorded for {body.patient_id}",
        patient_id=body.patient_id,
        target_type="erasure_request",
        target_id=req["id"],
        metadata={"categories": body.categories},
    )
    return req


_LIST_ERASURE_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.get("/erasure", response_model=None)
def list_erasure(
    patient_id: str | None = None,
    status: str | None = None,
    user: dict = _LIST_ERASURE_USER_DEFAULT,
) -> dict:
    if patient_id:
        enforce_patient_scope(user, patient_id)
    # Every row names a patient, so the LISTING is ward-scoped too — not just
    # the by-id lookups. department_scope() returns None for a genuinely
    # unscoped governance account, which the service reads as "no filter".
    facilities = facility_scope(user)
    if facilities is None:
        return {
            "requests": consent.list_erasure_requests(
                patient_id=patient_id, status=status, department=department_scope(user)
            )
        }
    # And to the caller's facilities' patients (multisite-9b), the limit
    # applied after that filter.
    rows = consent.list_erasure_requests(
        patient_id=patient_id, status=status, department=department_scope(user), limit=2000
    )
    mine = _patients_at((r.get("patient_id") for r in rows), facilities)
    return {"requests": [r for r in rows if r.get("patient_id") in mine][:200]}


_GET_ERASURE_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.get("/erasure/{request_id}")
def get_erasure(
    request_id: str,
    user: dict = _GET_ERASURE_USER_DEFAULT,
):
    try:
        detail = consent.get_erasure_request(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Erasure request not found") from None
    enforce_patient_scope(user, detail["request"]["patient_id"])
    return detail


_ADJUDICATE_ERASURE_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/erasure/{request_id}/adjudicate")
def adjudicate_erasure(
    request_id: str,
    request: Request,
    user: dict = _ADJUDICATE_ERASURE_USER_DEFAULT,
):
    """Apply the retention matrix: honour what may be erased, refuse the rest
    with a stated reason and the authority that compels retention."""
    try:
        detail = consent.get_erasure_request(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Erasure request not found") from None
    enforce_patient_scope(user, detail["request"]["patient_id"])
    try:
        result = consent.adjudicate_erasure_request(request_id, decided_by=user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        request,
        user,
        action_type="erasure_request_adjudicated",
        summary=(
            f"Erasure request {request_id} decided: "
            f"{result['honoured_count']} honoured, "
            f"{result['refused_count']} refused"
        ),
        patient_id=result["patient_id"],
        target_type="erasure_request",
        target_id=request_id,
        metadata={
            "honoured": result["honoured_count"],
            "refused": result["refused_count"],
            "executable": result["executable_count"],
        },
    )
    return result


_EXECUTE_ERASURE_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/erasure/{request_id}/execute")
def execute_erasure(
    request_id: str,
    request: Request,
    user: dict = _EXECUTE_ERASURE_USER_DEFAULT,
):
    """Carry out only what was adjudicated honourable AND executable."""
    try:
        detail = consent.get_erasure_request(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Erasure request not found") from None
    enforce_patient_scope(user, detail["request"]["patient_id"])

    # INTENT FIRST, and this ordering is the point. Erasure is irreversible, and
    # `phi_access` is a FAIL-CLOSED audit category — so writing the intent row
    # before touching any data means a failed audit write aborts the request with
    # nothing destroyed. Auditing only afterwards would put the one case that
    # matters (audit unavailable) on the wrong side of the line: data gone, no
    # record that anyone asked for it.
    _audit(
        request,
        user,
        category="phi_access",
        action_type="erasure_execution_started",
        summary=(
            f"About to execute erasure request {request_id} for patient "
            f"{detail['request']['patient_id']}"
        ),
        patient_id=detail["request"]["patient_id"],
        target_type="erasure_request",
        target_id=request_id,
        metadata={
            "categories": [
                d["category"]
                for d in detail["decisions"]
                if d["disposition"] == "honoured" and d["executable"]
            ]
        },
    )

    try:
        result = consent.execute_erasure_request(request_id, executed_by=user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # And the outcome row, also fail-closed: what was actually destroyed. Audio
    # files are removed after the commit, so one that would not go is named
    # here and the row says partial rather than success.
    left = result.get("files_not_removed") or []
    _audit(
        request,
        user,
        category="phi_access",
        action_type="erasure_executed",
        outcome="partial_success" if left else "success",
        summary=(
            f"Erasure request {request_id} executed: "
            f"{result['rows_erased']} rows erased"
            + (f"; {len(left)} audio file(s) could not be removed" if left else "")
        ),
        patient_id=result["patient_id"],
        target_type="erasure_request",
        target_id=request_id,
        metadata={
            "rows_erased": result["rows_erased"],
            "categories": [c["category"] for c in result["categories"]],
            "files_not_removed": left,
            "retained": [t["table"] for t in result.get("retained") or []],
        },
    )
    return result


# ════════════════════════════════════════════════════════════════
#  Breach register
# ════════════════════════════════════════════════════════════════


_CREATE_BREACH_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/breaches")
def create_breach(
    body: BreachBody,
    request: Request,
    user: dict = _CREATE_BREACH_USER_DEFAULT,
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        user,
        category="security",
        action_type="breach_recorded",
        summary=f"Personal data breach recorded: {body.nature}",
        target_type="breach_record",
        target_id=rec["id"],
        metadata={"nature": body.nature, "principals_affected": body.principals_affected},
    )
    return rec


_GET_BREACHES_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.get("/breaches")
def get_breaches(user: dict = _GET_BREACHES_USER_DEFAULT):
    return {"breaches": consent.list_breaches()}


def _withhold_other_facilities(breach: dict, facilities: frozenset[int]) -> None:
    """Keep a breach's affected patients to the caller's facilities, in
    place, and add the rest to the number withheld."""
    import json

    raw = breach.get("affected_patient_ids")
    if raw is None and breach.get("affected_patient_ids_json"):
        try:
            raw = json.loads(breach["affected_patient_ids_json"])
        except (TypeError, ValueError):
            raw = []
    affected = [str(p) for p in (raw or [])]
    mine = _patients_at(affected, facilities)
    visible = [p for p in affected if p in mine]
    breach["affected_patient_ids_withheld"] = int(
        breach.get("affected_patient_ids_withheld") or 0
    ) + (len(affected) - len(visible))
    breach["affected_patient_ids"] = visible
    if "affected_patient_ids_json" in breach:
        breach["affected_patient_ids_json"] = json.dumps(visible)


_GET_BREACH_DETAIL_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.get("/breaches/{breach_id}", response_model=None)
def get_breach_detail(breach_id: str, user: dict = _GET_BREACH_DETAIL_USER_DEFAULT) -> dict:
    """One breach. The incident is visible hospital-wide; the list of affected
    PATIENTS is filtered to the caller's ward (and facility, multisite-9b),
    with the withheld count stated."""
    try:
        breach = consent.get_breach(breach_id, department=department_scope(user))
    except ValueError:
        raise HTTPException(status_code=404, detail="Breach record not found") from None
    facilities = facility_scope(user)
    if facilities is not None:
        _withhold_other_facilities(breach, facilities)
    return breach


_CREATE_BREACH_NOTIFICATION_USER_DEFAULT = Depends(require_permission("admin.overview"))


@router.post("/breaches/{breach_id}/notifications")
def create_breach_notification(
    breach_id: str,
    body: BreachNotificationBody,
    request: Request,
    user: dict = _CREATE_BREACH_NOTIFICATION_USER_DEFAULT,
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        user,
        category="security",
        action_type="breach_notification_recorded",
        summary=(f"Breach {breach_id} notification to {body.party_type}: {body.notified_party}"),
        target_type="breach_record",
        target_id=breach_id,
        metadata={"party_type": body.party_type, "reference": body.reference},
    )
    return notif
