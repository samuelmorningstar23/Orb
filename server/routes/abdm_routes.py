"""Orb — ABDM / ABHA rail routes.

ABHA capture, verification records, care-context links, consent artefacts and the
work queue of encounters nobody has linked. EVERY ROUTE HERE RECORDS A STEP A
HUMAN PERFORMED SOMEWHERE ELSE. Orb is not connected to the ABDM gateway, no
handler in this file opens a socket to anything, and ``NO_GATEWAY_NOTICE`` rides
on every response so no client can render one of these payloads without the
disclaimer being available to it.

FIVE THINGS THIS FILE IS CAREFUL ABOUT
──────────────────────────────────────
1. IT NEVER TOUCHES A CLINICAL PATH. It reads `patients` and `admissions` for
   labels and an encounter list, and writes only to the five ABDM tables from
   migration 23. No route here can refuse, delay or gate an admission, an order,
   an administration, a vitals entry or a discharge. A patient with no ABHA is
   treated by every clinical surface exactly like a patient with one.

2. SYNC HANDLERS. Every handler is a plain `def`, so FastAPI runs it in the
   threadpool and the blocking SQLite calls in abdm_service never touch the event
   loop (tests/test_event_loop_discipline.py enforces the rule for `async def`;
   the honest way to satisfy it for a DB-heavy module is not to be async at all).

3. THE MODULE FLAG IS A 404, NOT A 403. A site that turns the rails off
   (ORB_ABDM=0) does not have an ABDM desk; a 403 would say "you are not
   allowed", which is a different and wrong statement.

4. NO ABHA IN A URL, EVER. Every lookup and check that carries an ABHA number or
   address is a POST with the value in the BODY. An ABHA is a national health
   identifier: in a query string it lands in the browser history, the proxy log
   and the server access log, none of which are the encrypted database. The one
   query-string parameter that names a person is `patient_id`, which is Orb's own
   internal id and which every route below passes through `enforce_patient_scope`.

5. PHI READS ARE AUDITED AND FAIL CLOSED. A payload here carries a patient's
   name, MRN and national health id. Category `phi_access` refuses the read if it
   cannot be written to the audit chain — same rule as every other PHI read
   surface in Orb.

WHY THE ROUTES ARE `admin.overview`
   ABDM work is done by the registration/medical-records desk, and Orb's role
   model has no ABDM or MRD role — that clerk holds an `admin` account, the same
   as the TPA desk (see routes/payer_routes.py for the same note). Inventing a
   permission this module cannot grant would produce a screen nobody can open. A
   dedicated `abdm.read` / `abdm.write` pair is recorded as a followup.

ONE FACILITY AT A TIME (multisite-9b)
   With more than one facility of a group on the appliance the lists, the
   summary and the linking queue are the caller's facility's
   (permissions.facility_scope, passed to the service as `facilities`): patients
   whose latest encounter is there, and for the queue encounters AT the
   caller's facility. Records named by id already pass enforce_patient_scope,
   which applies the facility; a care context of another facility answers 404
   at unlink. One facility on record: the scope is None and nothing changes.
"""

from __future__ import annotations

import logging

import config
from fastapi import APIRouter, Depends, HTTPException, Query
from permissions import (
    department_scope,
    enforce_patient_scope,
    facility_scope,
    require_permission,
)
from pydantic import BaseModel, Field
from services.abdm_service import (
    CONSENT_STATUSES,
    CONSENT_TRANSITIONS,
    ENCOUNTER_TYPES,
    HI_TYPES,
    IDENTITY_SOURCES,
    LINK_METHODS,
    NO_GATEWAY_NOTICE,
    NO_GATEWAY_SHORT,
    PURPOSE_CODES,
    QUEUE_BUCKETS,
    RECOGNISED_ABHA_SUFFIXES,
    REQUESTER_TYPES,
    VERIFICATION_METHODS,
    VERIFICATION_OUTCOMES,
    VERIFICATION_PLACES,
    VERIFICATION_STATUSES,
    AbdmError,
    AbhaFormatError,
    CareContextError,
    ConsentError,
    IdentityError,
    IllegalConsentTransition,
    abdm_summary,
    correct_abha,
    expire_consent,
    get_abha,
    get_consent,
    link_care_context,
    list_abha,
    list_care_contexts,
    list_consents,
    record_abha,
    record_consent_denial,
    record_consent_grant,
    record_consent_request,
    record_deactivation,
    record_verification,
    revoke_consent,
    unlink_care_context,
    unlinked_encounter_queue,
    validate_abha_address,
    validate_abha_number,
)
from services.audit_service import log_audit_event

logger = logging.getLogger("orb.abdm.routes")

router = APIRouter(prefix="/api/abdm", tags=["abdm"])


# ── The module switch ────────────────────────────────────────────────────────


def require_module() -> None:
    """404 the whole desk when a deployment has hidden this module.

    Read from `config.ABDM_ENABLED` at REQUEST time, not import time, so a test
    (or an ops toggle that reloads config) that patches the constant actually
    changes behaviour rather than being shadowed by a value captured when the
    module was first imported.
    """
    if not getattr(config, "ABDM_ENABLED", True):
        raise HTTPException(
            status_code=404,
            detail="The ABDM / ABHA rails are not enabled for this deployment.",
        )


_DESK = require_permission("admin.overview")


def _audit(
    user: dict,
    action: str,
    *,
    summary: str,
    outcome: str = "success",
    target_id: str | int | None = None,
    target_type: str = "abha_identity",
    patient_id: str | None = None,
    metadata: dict | None = None,
    category: str = "compliance",
) -> None:
    """One audit line per desk action.

    `phi_access` (FAIL-CLOSED — the read is refused if it cannot be audited) for
    anything that returns a named patient's ABHA, care contexts or consents;
    `compliance` for the rest. An ABHA number tied to a name and an MRN is PHI
    under any reading, and reading it must leave a trace.
    """
    log_audit_event(
        correlation_id=f"abdm-{action}-{target_id or 'na'}",
        event_category=category,
        action_type=f"abdm_{action}",
        outcome=outcome,
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        metadata=metadata,
    )


def _handle(fn, *args, **kwargs):
    """Run a service call, turning its refusals into the right HTTP status.

    409 for a consent move the lifecycle forbids — it is a CONFLICT with the
    artefact's current state, not a malformed request, and the panel shows the
    message verbatim ("This consent was REVOKED by the patient and cannot be
    'granted' again"). 422 for a value that is not structurally an ABHA. 404 for
    a record that is not there. 400 for everything else that cannot be true.
    """
    try:
        return fn(*args, **kwargs)
    except IllegalConsentTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AbhaFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (IdentityError, CareContextError, ConsentError, AbdmError) as exc:
        message = str(exc)
        if "not found" in message or message.startswith("unknown patient"):
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc


def _wrap(payload: dict) -> dict:
    """Attach the honesty notice to every single response body.

    Not "on the screen that shows ABHA numbers" — on EVERY payload, because
    payloads get consumed by things that were not the screen: an export, a future
    report, somebody's curl, a slide. The sentence travels with the data.

    `gateway_connected: False` is a machine-readable form of the same claim, so a
    client that never renders prose still cannot mistake this for an integration.
    """
    payload.setdefault("notice", NO_GATEWAY_NOTICE)
    payload.setdefault("notice_short", NO_GATEWAY_SHORT)
    payload.setdefault("gateway_connected", False)
    payload.setdefault("transmitted_to_abdm", False)
    return payload


# ══════════════════════════════════════════════════════════════════════════════
#  Request bodies
# ══════════════════════════════════════════════════════════════════════════════


class AbhaCheckIn(BaseModel):
    """A structural check with nothing stored and nothing sent.

    POST rather than GET so the ABHA never appears in a URL — see the note at the
    top of this file.
    """

    abha_number: str | None = None
    abha_address: str | None = None


class AbhaIn(BaseModel):
    """Recording an ABHA a patient presented.

    THERE IS NO `verification_status` FIELD, deliberately. A number arriving over
    this route is unverified, whatever the client believes, and the only way to
    'verified' is the verification route below.
    """

    patient_id: str
    abha_number: str | None = None
    abha_address: str | None = None
    source: str = "counter_keyed"
    notes: str | None = None
    is_primary: bool = True


class AbhaCorrectionIn(BaseModel):
    abha_number: str | None = None
    abha_address: str | None = None
    reason: str


class VerificationIn(BaseModel):
    """Recording a verification performed ELSEWHERE. Orb verified nothing."""

    method: str = Field(description="aadhaar_otp | mobile_otp | demographic_match | …")
    outcome: str = Field(description="verified | failed | inconclusive")
    performed_by: str = Field(description="The person who did it, on the portal")
    performed_at: str | None = None
    performed_where: str = "outside_orb"
    reference: str | None = None
    failure_reason: str | None = None
    notes: str | None = None


class DeactivationIn(BaseModel):
    reason: str


class CareContextIn(BaseModel):
    """Recording a link a human performed on a HIP console elsewhere."""

    identity_id: int
    care_context_reference: str
    hi_type: str = "OPConsultation"
    display: str | None = None
    encounter_type: str = "admission"
    encounter_ref: str | None = None
    encounter_date: str | None = None
    linked_by: str
    linked_at: str | None = None
    link_method: str = "abdm_portal"
    link_reference: str | None = None
    notes: str | None = None


class UnlinkIn(BaseModel):
    """A reason is REQUIRED by the schema of this request, not just by the
    service. A linked care context cannot silently unlink."""

    reason: str
    unlinked_at: str | None = None


class ConsentRequestIn(BaseModel):
    patient_id: str
    requester_name: str
    purpose_code: str = "CAREMGT"
    purpose_text: str | None = None
    hi_types: list[str] = Field(default_factory=list)
    date_range_from: str | None = None
    date_range_to: str | None = None
    expires_at: str | None = None
    request_id: str | None = None
    identity_id: int | None = None
    requester_type: str = "hiu"
    recorded_where: str = "consent_manager"
    notes: str | None = None


class ConsentGrantIn(BaseModel):
    artefact_id: str
    granted_at: str | None = None
    expires_at: str | None = None
    hi_types: list[str] | None = None


class ReasonIn(BaseModel):
    reason: str
    occurred_at: str | None = None


# ══════════════════════════════════════════════════════════════════════════════
#  Reference / meta
# ══════════════════════════════════════════════════════════════════════════════


_META_USER_DEFAULT = Depends(_DESK)
_META_M_DEFAULT = Depends(require_module)


@router.get("/meta")
def meta(user: dict = _META_USER_DEFAULT, _m: None = _META_M_DEFAULT) -> dict:
    """Everything the panel needs to render its forms without hard-coding a
    vocabulary that could drift from the server's."""
    return _wrap(
        {
            "verification_statuses": list(VERIFICATION_STATUSES),
            "verification_methods": [
                {"code": c, "label": left_item} for c, left_item in VERIFICATION_METHODS
            ],
            "verification_outcomes": list(VERIFICATION_OUTCOMES),
            "verification_places": [
                {"code": c, "label": left_item} for c, left_item in VERIFICATION_PLACES
            ],
            "identity_sources": [
                {"code": c, "label": left_item} for c, left_item in IDENTITY_SOURCES
            ],
            "hi_types": list(HI_TYPES),
            "link_methods": [{"code": c, "label": left_item} for c, left_item in LINK_METHODS],
            "encounter_types": list(ENCOUNTER_TYPES),
            "consent_statuses": list(CONSENT_STATUSES),
            "consent_transitions": {k: sorted(v) for k, v in CONSENT_TRANSITIONS.items()},
            "purpose_codes": [{"code": c, "label": left_item} for c, left_item in PURPOSE_CODES],
            "requester_types": list(REQUESTER_TYPES),
            "queue_buckets": [{"id": b, "label": left_item} for b, left_item in QUEUE_BUCKETS],
            "recognised_abha_suffixes": sorted(RECOGNISED_ABHA_SUFFIXES),
            # Stated in the meta payload so even a client that renders none of the
            # prose above still receives the fact in a form it cannot miss.
            "checks_abha_check_digit": False,
            "milestone_status": (
                "Orb records ABDM steps performed elsewhere. It is not an onboarded "
                "HIP or HIU, holds no NHA milestone certification, and performs no "
                "M1/M2/M3 transaction."
            ),
        }
    )


_SUMMARY_USER_DEFAULT = Depends(_DESK)
_SUMMARY_M_DEFAULT = Depends(require_module)


@router.get("/summary")
def summary(user: dict = _SUMMARY_USER_DEFAULT, _m: None = _SUMMARY_M_DEFAULT) -> dict:
    return _wrap(abdm_summary(department=department_scope(user), facilities=facility_scope(user)))


_CHECK_USER_DEFAULT = Depends(_DESK)
_CHECK_M_DEFAULT = Depends(require_module)


@router.post("/check")
def check(
    body: AbhaCheckIn,
    user: dict = _CHECK_USER_DEFAULT,
    _m: None = _CHECK_M_DEFAULT,
) -> dict:
    """Structural check of an ABHA number and/or address. STORES NOTHING.

    Lets the panel tell a clerk "that is 13 digits" while they are still typing,
    without creating a row and without implying that anything was looked up. Both
    result payloads carry `checked_against_abdm: False`.
    """
    out: dict = {}
    if body.abha_number is not None:
        out["number"] = validate_abha_number(body.abha_number).as_dict()
    if body.abha_address is not None:
        out["address"] = validate_abha_address(body.abha_address).as_dict()
    if not out:
        raise HTTPException(
            status_code=400, detail="Give an ABHA number or an ABHA address to check."
        )
    return _wrap(out)


# ══════════════════════════════════════════════════════════════════════════════
#  ABHA identities
# ══════════════════════════════════════════════════════════════════════════════


_IDENTITIES_PATIENT_ID_DEFAULT = Query(None)
_IDENTITIES_VERIFICATION_STATUS_DEFAULT = Query(None)
_IDENTITIES_ACTIVE_ONLY_DEFAULT = Query(True)
_IDENTITIES_LIMIT_DEFAULT = Query(200, ge=1, le=1000)
_IDENTITIES_USER_DEFAULT = Depends(_DESK)
_IDENTITIES_M_DEFAULT = Depends(require_module)


@router.get("/identities")
def identities(
    patient_id: str | None = _IDENTITIES_PATIENT_ID_DEFAULT,
    verification_status: str | None = _IDENTITIES_VERIFICATION_STATUS_DEFAULT,
    active_only: bool = _IDENTITIES_ACTIVE_ONLY_DEFAULT,
    limit: int = _IDENTITIES_LIMIT_DEFAULT,
    user: dict = _IDENTITIES_USER_DEFAULT,
    _m: None = _IDENTITIES_M_DEFAULT,
) -> dict:
    """ABHA records. `patient_id` arrives on the QUERY STRING, so the scope check
    is explicit — enforce_patient_scope, not require_patient_access."""
    enforce_patient_scope(user, patient_id)
    rows = list_abha(
        patient_id=patient_id,
        verification_status=verification_status,
        department=department_scope(user),
        active_only=active_only,
        limit=limit,
        facilities=facility_scope(user),
    )
    if patient_id:
        # Fail-closed: a named patient's national health id is PHI, and a read
        # that cannot be audited is a read that does not happen.
        _audit(
            user,
            "identities_read",
            category="phi_access",
            summary=f"ABHA records read for {patient_id}",
            patient_id=patient_id,
            metadata={"count": len(rows)},
        )
    return _wrap({"identities": rows, "total": len(rows)})


_IDENTITY_DETAIL_USER_DEFAULT = Depends(_DESK)
_IDENTITY_DETAIL_M_DEFAULT = Depends(require_module)


@router.get("/identities/{identity_id}")
def identity_detail(
    identity_id: int,
    user: dict = _IDENTITY_DETAIL_USER_DEFAULT,
    _m: None = _IDENTITY_DETAIL_M_DEFAULT,
) -> dict:
    """One ABHA record with its verifications, links and history.

    `identity_id` is a SURROGATE that resolves to a patient — the same disclosure
    as naming the patient directly — so the scope check runs on the resolved
    patient id before the payload is returned.
    """
    record = _handle(get_abha, identity_id)
    enforce_patient_scope(user, record.get("patient_id"))
    _audit(
        user,
        "identity_read",
        category="phi_access",
        summary=f"ABHA record {identity_id} read",
        target_id=str(identity_id),
        patient_id=record.get("patient_id"),
    )
    return _wrap({"identity": record})


_CREATE_IDENTITY_USER_DEFAULT = Depends(_DESK)
_CREATE_IDENTITY_M_DEFAULT = Depends(require_module)


@router.post("/identities")
def create_identity(
    body: AbhaIn,
    user: dict = _CREATE_IDENTITY_USER_DEFAULT,
    _m: None = _CREATE_IDENTITY_M_DEFAULT,
) -> dict:
    """Record an ABHA a patient presented. It is stored UNVERIFIED."""
    enforce_patient_scope(user, body.patient_id)
    record = _handle(
        record_abha,
        patient_id=body.patient_id,
        abha_number=body.abha_number,
        abha_address=body.abha_address,
        source=body.source,
        recorded_by=user.get("id"),
        notes=body.notes,
        is_primary=body.is_primary,
        # A duplicate refusal names the other patient only inside this scope.
        department=department_scope(user),
        facilities=facility_scope(user),
    )
    _audit(
        user,
        "identity_recorded",
        summary=f"ABHA recorded for {body.patient_id} (unverified)",
        target_id=str(record["id"]),
        patient_id=body.patient_id,
        metadata={"source": body.source, "verification_status": record["verification_status"]},
    )
    return _wrap({"identity": record})


_CREATE_VERIFICATION_USER_DEFAULT = Depends(_DESK)
_CREATE_VERIFICATION_M_DEFAULT = Depends(require_module)


@router.post("/identities/{identity_id}/verification")
def create_verification(
    identity_id: int,
    body: VerificationIn,
    user: dict = _CREATE_VERIFICATION_USER_DEFAULT,
    _m: None = _CREATE_VERIFICATION_M_DEFAULT,
) -> dict:
    """Record a verification a human performed on the ABDM portal / ABHA app /
    another onboarded system. ORB PERFORMED NO VERIFICATION."""
    existing = _handle(get_abha, identity_id, include_events=False)
    enforce_patient_scope(user, existing.get("patient_id"))
    record = _handle(
        record_verification,
        identity_id=identity_id,
        method=body.method,
        outcome=body.outcome,
        performed_by=body.performed_by,
        performed_at=body.performed_at,
        performed_where=body.performed_where,
        reference=body.reference,
        failure_reason=body.failure_reason,
        notes=body.notes,
        recorded_by=user.get("id"),
    )
    _audit(
        user,
        "verification_recorded",
        summary=(
            f"ABHA verification recorded for {record['patient_id']}: "
            f"{body.method} → {body.outcome} (performed outside Orb)"
        ),
        target_id=str(identity_id),
        patient_id=record.get("patient_id"),
        metadata={
            "method": body.method,
            "outcome": body.outcome,
            "performed_where": body.performed_where,
            "performed_by": body.performed_by,
            "transmitted_to_abdm": False,
        },
    )
    return _wrap({"identity": record})


_CORRECT_IDENTITY_USER_DEFAULT = Depends(_DESK)
_CORRECT_IDENTITY_M_DEFAULT = Depends(require_module)


@router.post("/identities/{identity_id}/correction")
def correct_identity(
    identity_id: int,
    body: AbhaCorrectionIn,
    user: dict = _CORRECT_IDENTITY_USER_DEFAULT,
    _m: None = _CORRECT_IDENTITY_M_DEFAULT,
) -> dict:
    """Correct a mis-keyed ABHA. The verification is RESET to unverified."""
    existing = _handle(get_abha, identity_id, include_events=False)
    enforce_patient_scope(user, existing.get("patient_id"))
    record = _handle(
        correct_abha,
        identity_id=identity_id,
        abha_number=body.abha_number,
        abha_address=body.abha_address,
        reason=body.reason,
        actor_id=user.get("id"),
    )
    _audit(
        user,
        "identity_corrected",
        summary=f"ABHA corrected for {record['patient_id']}; verification reset",
        target_id=str(identity_id),
        patient_id=record.get("patient_id"),
        metadata={"reason": body.reason},
    )
    return _wrap({"identity": record})


_DEACTIVATE_IDENTITY_USER_DEFAULT = Depends(_DESK)
_DEACTIVATE_IDENTITY_M_DEFAULT = Depends(require_module)


@router.post("/identities/{identity_id}/deactivation")
def deactivate_identity(
    identity_id: int,
    body: DeactivationIn,
    user: dict = _DEACTIVATE_IDENTITY_USER_DEFAULT,
    _m: None = _DEACTIVATE_IDENTITY_M_DEFAULT,
) -> dict:
    """Record that the desk was TOLD this ABHA is no longer valid."""
    existing = _handle(get_abha, identity_id, include_events=False)
    enforce_patient_scope(user, existing.get("patient_id"))
    record = _handle(
        record_deactivation,
        identity_id=identity_id,
        reason=body.reason,
        actor_id=user.get("id"),
    )
    _audit(
        user,
        "deactivation_recorded",
        summary=f"ABHA reported deactivated for {record['patient_id']}",
        target_id=str(identity_id),
        patient_id=record.get("patient_id"),
        metadata={"reason": body.reason},
    )
    return _wrap({"identity": record})


# ══════════════════════════════════════════════════════════════════════════════
#  Care contexts
# ══════════════════════════════════════════════════════════════════════════════


_CARE_CONTEXTS_PATIENT_ID_DEFAULT = Query(None)
_CARE_CONTEXTS_IDENTITY_ID_DEFAULT = Query(None)
_CARE_CONTEXTS_STATUS_DEFAULT = Query(None)
_CARE_CONTEXTS_LIMIT_DEFAULT = Query(200, ge=1, le=1000)
_CARE_CONTEXTS_USER_DEFAULT = Depends(_DESK)
_CARE_CONTEXTS_M_DEFAULT = Depends(require_module)


@router.get("/care-contexts")
def care_contexts(
    patient_id: str | None = _CARE_CONTEXTS_PATIENT_ID_DEFAULT,
    identity_id: int | None = _CARE_CONTEXTS_IDENTITY_ID_DEFAULT,
    status: str | None = _CARE_CONTEXTS_STATUS_DEFAULT,
    limit: int = _CARE_CONTEXTS_LIMIT_DEFAULT,
    user: dict = _CARE_CONTEXTS_USER_DEFAULT,
    _m: None = _CARE_CONTEXTS_M_DEFAULT,
) -> dict:
    enforce_patient_scope(user, patient_id)
    rows = list_care_contexts(
        patient_id=patient_id,
        identity_id=identity_id,
        status=status,
        department=department_scope(user),
        limit=limit,
        facilities=facility_scope(user),
    )
    if patient_id:
        _audit(
            user,
            "care_contexts_read",
            category="phi_access",
            summary=f"Care-context links read for {patient_id}",
            target_type="care_context",
            patient_id=patient_id,
            metadata={"count": len(rows)},
        )
    return _wrap({"care_contexts": rows, "total": len(rows)})


_CREATE_CARE_CONTEXT_USER_DEFAULT = Depends(_DESK)
_CREATE_CARE_CONTEXT_M_DEFAULT = Depends(require_module)


@router.post("/care-contexts")
def create_care_context(
    body: CareContextIn,
    user: dict = _CREATE_CARE_CONTEXT_USER_DEFAULT,
    _m: None = _CREATE_CARE_CONTEXT_M_DEFAULT,
) -> dict:
    """RECORD a link a human performed elsewhere. Orb linked nothing."""
    existing = _handle(get_abha, body.identity_id, include_events=False)
    enforce_patient_scope(user, existing.get("patient_id"))
    record = _handle(
        link_care_context,
        identity_id=body.identity_id,
        care_context_reference=body.care_context_reference,
        hi_type=body.hi_type,
        display=body.display,
        encounter_type=body.encounter_type,
        encounter_ref=body.encounter_ref,
        encounter_date=body.encounter_date,
        linked_by=body.linked_by,
        linked_at=body.linked_at,
        link_method=body.link_method,
        link_reference=body.link_reference,
        notes=body.notes,
        recorded_by=user.get("id"),
    )
    _audit(
        user,
        "care_context_linked",
        summary=(
            f"Care context '{body.care_context_reference}' recorded as linked "
            f"for {record['patient_id']} (linked outside Orb)"
        ),
        target_type="care_context",
        target_id=str(record["id"]),
        patient_id=record.get("patient_id"),
        metadata={
            "hi_type": body.hi_type,
            "link_method": body.link_method,
            "linked_by": body.linked_by,
            "transmitted_to_abdm": False,
        },
    )
    return _wrap({"care_context": record})


_UNLINK_USER_DEFAULT = Depends(_DESK)
_UNLINK_M_DEFAULT = Depends(require_module)


@router.post("/care-contexts/{care_context_id}/unlink")
def unlink(
    care_context_id: int,
    body: UnlinkIn,
    user: dict = _UNLINK_USER_DEFAULT,
    _m: None = _UNLINK_M_DEFAULT,
) -> dict:
    """RECORD an unlink a human performed elsewhere.

    The row is never deleted and the reason is mandatory — a linked care context
    cannot silently unlink. See services/abdm_service.unlink_care_context.
    """
    record = _handle(
        unlink_care_context,
        care_context_id=care_context_id,
        reason=body.reason,
        actor_id=user.get("id") or "",
        unlinked_at=body.unlinked_at,
        facilities=facility_scope(user),
    )
    _audit(
        user,
        "care_context_unlinked",
        summary=(
            f"Care context '{record['care_context_reference']}' recorded as "
            f"unlinked for {record['patient_id']}"
        ),
        target_type="care_context",
        target_id=str(care_context_id),
        patient_id=record.get("patient_id"),
        metadata={"reason": body.reason},
    )
    return _wrap({"care_context": record})


# ══════════════════════════════════════════════════════════════════════════════
#  Consent artefacts
# ══════════════════════════════════════════════════════════════════════════════


_CONSENTS_PATIENT_ID_DEFAULT = Query(None)
_CONSENTS_STATUS_DEFAULT = Query(None)
_CONSENTS_LIMIT_DEFAULT = Query(200, ge=1, le=1000)
_CONSENTS_USER_DEFAULT = Depends(_DESK)
_CONSENTS_M_DEFAULT = Depends(require_module)


@router.get("/consents")
def consents(
    patient_id: str | None = _CONSENTS_PATIENT_ID_DEFAULT,
    status: str | None = _CONSENTS_STATUS_DEFAULT,
    limit: int = _CONSENTS_LIMIT_DEFAULT,
    user: dict = _CONSENTS_USER_DEFAULT,
    _m: None = _CONSENTS_M_DEFAULT,
) -> dict:
    enforce_patient_scope(user, patient_id)
    rows = list_consents(
        patient_id=patient_id,
        status=status,
        department=department_scope(user),
        limit=limit,
        facilities=facility_scope(user),
    )
    if patient_id:
        _audit(
            user,
            "consents_read",
            category="phi_access",
            summary=f"ABDM consent artefacts read for {patient_id}",
            target_type="consent_artefact",
            patient_id=patient_id,
            metadata={"count": len(rows)},
        )
    return _wrap({"consents": rows, "total": len(rows)})


_CONSENT_DETAIL_USER_DEFAULT = Depends(_DESK)
_CONSENT_DETAIL_M_DEFAULT = Depends(require_module)


@router.get("/consents/{consent_id}")
def consent_detail(
    consent_id: int,
    user: dict = _CONSENT_DETAIL_USER_DEFAULT,
    _m: None = _CONSENT_DETAIL_M_DEFAULT,
) -> dict:
    record = _handle(get_consent, consent_id)
    enforce_patient_scope(user, record.get("patient_id"))
    _audit(
        user,
        "consent_read",
        category="phi_access",
        summary=f"ABDM consent artefact {consent_id} read",
        target_type="consent_artefact",
        target_id=str(consent_id),
        patient_id=record.get("patient_id"),
    )
    return _wrap({"consent": record})


_CREATE_CONSENT_USER_DEFAULT = Depends(_DESK)
_CREATE_CONSENT_M_DEFAULT = Depends(require_module)


@router.post("/consents")
def create_consent(
    body: ConsentRequestIn,
    user: dict = _CREATE_CONSENT_USER_DEFAULT,
    _m: None = _CREATE_CONSENT_M_DEFAULT,
) -> dict:
    enforce_patient_scope(user, body.patient_id)
    record = _handle(
        record_consent_request,
        patient_id=body.patient_id,
        requester_name=body.requester_name,
        purpose_code=body.purpose_code,
        purpose_text=body.purpose_text,
        hi_types=body.hi_types,
        date_range_from=body.date_range_from,
        date_range_to=body.date_range_to,
        expires_at=body.expires_at,
        request_id=body.request_id,
        identity_id=body.identity_id,
        requester_type=body.requester_type,
        recorded_where=body.recorded_where,
        recorded_by=user.get("id"),
        notes=body.notes,
    )
    _audit(
        user,
        "consent_request_recorded",
        summary=(
            f"ABDM consent request recorded for {body.patient_id} by "
            f"{body.requester_name} ({body.purpose_code})"
        ),
        target_type="consent_artefact",
        target_id=str(record["id"]),
        patient_id=body.patient_id,
    )
    return _wrap({"consent": record})


_GRANT_CONSENT_USER_DEFAULT = Depends(_DESK)
_GRANT_CONSENT_M_DEFAULT = Depends(require_module)


@router.post("/consents/{consent_id}/grant")
def grant_consent(
    consent_id: int,
    body: ConsentGrantIn,
    user: dict = _GRANT_CONSENT_USER_DEFAULT,
    _m: None = _GRANT_CONSENT_M_DEFAULT,
) -> dict:
    """Record that the patient granted it on their consent manager.

    A REVOKED artefact refuses here with 409 and the reason in words. That is the
    invariant this module is built around: revocation is the one control the
    patient actually has.
    """
    existing = _handle(get_consent, consent_id, include_events=False)
    enforce_patient_scope(user, existing.get("patient_id"))
    record = _handle(
        record_consent_grant,
        consent_id=consent_id,
        artefact_id=body.artefact_id,
        granted_at=body.granted_at,
        expires_at=body.expires_at,
        hi_types=body.hi_types,
        actor_id=user.get("id"),
    )
    _audit(
        user,
        "consent_granted",
        summary=f"ABDM consent {consent_id} recorded as granted by the patient",
        target_type="consent_artefact",
        target_id=str(consent_id),
        patient_id=record.get("patient_id"),
        metadata={"artefact_id": body.artefact_id},
    )
    return _wrap({"consent": record})


_DENY_CONSENT_USER_DEFAULT = Depends(_DESK)
_DENY_CONSENT_M_DEFAULT = Depends(require_module)


@router.post("/consents/{consent_id}/denial")
def deny_consent(
    consent_id: int,
    body: ReasonIn,
    user: dict = _DENY_CONSENT_USER_DEFAULT,
    _m: None = _DENY_CONSENT_M_DEFAULT,
) -> dict:
    existing = _handle(get_consent, consent_id, include_events=False)
    enforce_patient_scope(user, existing.get("patient_id"))
    record = _handle(
        record_consent_denial,
        consent_id=consent_id,
        reason=body.reason,
        denied_at=body.occurred_at,
        actor_id=user.get("id"),
    )
    _audit(
        user,
        "consent_denied",
        summary=f"ABDM consent {consent_id} recorded as refused by the patient",
        target_type="consent_artefact",
        target_id=str(consent_id),
        patient_id=record.get("patient_id"),
        metadata={"reason": body.reason},
    )
    return _wrap({"consent": record})


_REVOKE_USER_DEFAULT = Depends(_DESK)
_REVOKE_M_DEFAULT = Depends(require_module)


@router.post("/consents/{consent_id}/revocation")
def revoke(
    consent_id: int,
    body: ReasonIn,
    user: dict = _REVOKE_USER_DEFAULT,
    _m: None = _REVOKE_M_DEFAULT,
) -> dict:
    """Record that the patient WITHDREW consent. One way. No return.

    There is deliberately no route that puts a revoked artefact back to granted;
    a fresh consent is a NEW artefact with the consent manager's new id.
    """
    existing = _handle(get_consent, consent_id, include_events=False)
    enforce_patient_scope(user, existing.get("patient_id"))
    record = _handle(
        revoke_consent,
        consent_id=consent_id,
        reason=body.reason,
        actor_id=user.get("id") or "",
        revoked_at=body.occurred_at,
    )
    _audit(
        user,
        "consent_revoked",
        summary=f"ABDM consent {consent_id} recorded as revoked by the patient",
        target_type="consent_artefact",
        target_id=str(consent_id),
        patient_id=record.get("patient_id"),
        metadata={"reason": body.reason, "terminal": True},
    )
    return _wrap({"consent": record})


_EXPIRE_USER_DEFAULT = Depends(_DESK)
_EXPIRE_M_DEFAULT = Depends(require_module)


@router.post("/consents/{consent_id}/expiry")
def expire(
    consent_id: int,
    user: dict = _EXPIRE_USER_DEFAULT,
    _m: None = _EXPIRE_M_DEFAULT,
) -> dict:
    existing = _handle(get_consent, consent_id, include_events=False)
    enforce_patient_scope(user, existing.get("patient_id"))
    record = _handle(expire_consent, consent_id=consent_id, actor_id=user.get("id"))
    _audit(
        user,
        "consent_expired",
        summary=f"ABDM consent {consent_id} recorded as expired",
        target_type="consent_artefact",
        target_id=str(consent_id),
        patient_id=record.get("patient_id"),
    )
    return _wrap({"consent": record})


# ══════════════════════════════════════════════════════════════════════════════
#  The work queue
# ══════════════════════════════════════════════════════════════════════════════


_QUEUE_BUCKET_DEFAULT = Query(None)
_QUEUE_INCLUDE_DISCHARGED_DEFAULT = Query(True)
_QUEUE_LIMIT_DEFAULT = Query(200, ge=1, le=1000)
_QUEUE_USER_DEFAULT = Depends(_DESK)
_QUEUE_M_DEFAULT = Depends(require_module)


@router.get("/queue")
def queue(
    bucket: str | None = _QUEUE_BUCKET_DEFAULT,
    include_discharged: bool = _QUEUE_INCLUDE_DISCHARGED_DEFAULT,
    limit: int = _QUEUE_LIMIT_DEFAULT,
    user: dict = _QUEUE_USER_DEFAULT,
    _m: None = _QUEUE_M_DEFAULT,
) -> dict:
    """Encounters with no live care-context link, oldest first.

    Returns named patients, so it is a PHI read and is audited as one. The
    department scope comes from the AUTHENTICATED USER, never from the request —
    there is no `department=` parameter here and there must never be one.
    """
    facilities = facility_scope(user)
    payload = _handle(
        unlinked_encounter_queue,
        department=department_scope(user),
        bucket=bucket,
        include_discharged=include_discharged,
        limit=limit,
        facilities=facilities,
    )
    metadata: dict = {"counts": payload["counts"], "bucket": bucket}
    if facilities is not None:
        metadata["facility_scope"] = sorted(facilities)
    _audit(
        user,
        "queue_read",
        category="phi_access",
        summary=f"ABDM linking queue read ({payload['total']} encounters)",
        target_type="care_context",
        metadata=metadata,
    )
    return _wrap(payload)
