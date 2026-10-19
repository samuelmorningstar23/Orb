"""Patient registration: the /api/registration routes.

Created empty in wave 1 of the hospital-system build (3 October 2026) and
registered once in main.py. opd-4, opd-5 and opd-6 (W4-A) add their endpoints
here.

BEHIND THE 'registration' FEATURE (ORB_REGISTRATION, default off). The gate is
a router-level dependency, so every endpoint added here inherits it and answers
404 while the feature is off (after a 401 for a caller with no session).

Rules for every endpoint added here (design: docs/his_design/opd.json):
  * the desk dependency permissions.require_registration_desk(mode) (W2-E):
    read is patient.read_all or registration.read, write is frontdesk.write,
    patient.write or registration.write;
  * only the minimal-field search is hospital-wide (UHID, name, sex, age, phone
    last 4, last visit date, inpatient now): at least 3 characters, at most 20
    results, a phi_access row naming the matched ids;
  * an identity check answers only match or no match and never returns a
    stored value (ruling OPD-S1, held), rate-limited and audited;
  * a probable duplicate follows config.DUPLICATE_MODE ('block' until ruling
    OPD-S2 is confirmed); an exact UHID or active ABHA match is always refused;
  * no allergy field at the desk (ruling OPD-S5, held);
  * every write audits as patient_identity (fail-closed) in the transaction.

Endpoints (opd-4 and opd-5, W4-A, 4 October 2026; services/
registration_service.py holds the rules):
  GET  /config                          desk read; site settings, no patient data
  GET  /search?q=                       desk read; hospital-wide minimal fields, audited
  POST /match                           desk read; duplicate candidates, audited; a
                                        scoped caller's date of birth is never
                                        compared outside their department
  POST /patients                        registration.write; register a patient
  POST /identity-check                  desk write; match or no match only, rate-limited
  GET  /patients/{patient_id}           registration.read + scope (OPD arm), audited
  PATCH /patients/{patient_id}          scope (OPD arm); fields need registration.write,
                                        the UHID needs the administrator
  GET  /patients/{patient_id}/changes   registration.read + scope (OPD arm), audited
  GET  /patients/{patient_id}/card      registration.read + scope (OPD arm), audited
  GET  /duplicates                      desk read; the worklist, audited
  POST /duplicates/{candidate_id}/resolve  registration.write; never the overrider
  POST /duplicates/sweep                administrator; bounded, audited
  GET  /payers                          desk read; the payer master's active rows (opd-13)
  GET  /charge-rules                    desk read; outpatient charge rules (opd-13)
  POST /charge-rules                    billing.tariff; add a rule, audited as configuration
  POST /charge-rules/{rule_id}/retire   billing.tariff; retire a rule with a reason
POST /patients also takes an optional payer {payer_code, policy_number,
member_id} (opd-13), recorded unverified with no admission; with a live
registration-fee rule the answer carries the fee line raised once.
POST /patients also takes an optional consent step (opd-19) {notice_acknowledged,
treatment_consent, notice_version, notice_language, method, given_by,
proxy_name, proxy_relationship, evidence_ref}: one consent row in the same
transaction, consent.record only (403 consent_permission_required otherwise,
ruling OPD-N3); the answer then carries "consents".
Every handler is a plain def (FastAPI runs it in the threadpool), so the
database work never touches the event loop.
"""
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

import config
from permissions import (
    ADMIN_OVERVIEW,
    BILLING_TARIFF,
    REGISTRATION_READ,
    REGISTRATION_WRITE,
    department_scope,
    has_permission,
    log_phi_read,
    require_patient_access,
    require_permission,
    require_registration_desk,
)
from routes.opt_in_gates import require_registration_feature
from services import registration_service as reg
from services.audit_service import AuditWriteError, log_audit_event

router = APIRouter(
    prefix="/api/registration",
    tags=["Registration"],
    dependencies=[Depends(require_registration_feature)],
)


@router.get("/meta")
def registration_meta(user: dict = Depends(require_permission(REGISTRATION_READ))):
    """What this site's registration settings are. No patient data."""
    return {
        "feature": "registration",
        "enabled": config.feature_enabled("registration"),
        "settings": {
            "uhid_source": config.UHID_SOURCE,
            "uhid_pattern_set": config.UHID_PATTERN is not None,
            "duplicate_mode": config.DUPLICATE_MODE,
            "queue_poll_seconds": config.OPD_QUEUE_POLL_SECONDS,
        },
    }


# ── Request bodies ───────────────────────────────────────────────────────────
# extra="forbid" on every body: a field the desk may not record (an allergy,
# ruling OPD-S5; a full identity document number, ruling OPD-N10) is refused
# with 422 rather than silently dropped, so nobody believes it was saved.

_TEXT = Field(None, max_length=200)


class DuplicateReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(..., max_length=40)
    candidate_ids: List[str] = Field(default_factory=list, max_length=100)
    reason: Optional[str] = Field(None, max_length=1000)


class PayerIn(BaseModel):
    """Who the patient says pays (opd-13): a code from GET /payers and the
    policy or member number if they have one. Recorded unverified."""
    model_config = ConfigDict(extra="forbid")
    payer_code: str = Field(..., max_length=60)
    policy_number: Optional[str] = Field(None, max_length=60)
    member_id: Optional[str] = Field(None, max_length=60)


class ChargeRuleIn(BaseModel):
    """One outpatient charge rule (opd-13). See services/opd_charges_service."""
    model_config = ConfigDict(extra="forbid")
    rule_kind: str = Field(..., max_length=20)
    service_code: Optional[str] = Field(None, max_length=60)
    department: Optional[str] = Field(None, max_length=200)
    doctor_id: Optional[str] = Field(None, max_length=64)
    visit_type: Optional[str] = Field(None, max_length=20)
    follow_up_window_days: Optional[int] = None
    notes: Optional[str] = Field(None, max_length=500)


class RetireRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(None, max_length=500)


class RegistrationConsentIn(BaseModel):
    """The notice and consent step (opd-19): the patient was given the privacy
    notice (its version and language) and agrees to treatment. Needs
    consent.record (403 consent_permission_required otherwise; ruling OPD-N3).
    The rules are registration_service._consent_values."""
    model_config = ConfigDict(extra="forbid")
    notice_acknowledged: Optional[StrictBool] = None
    treatment_consent: Optional[StrictBool] = None
    notice_version: Optional[str] = Field(None, max_length=40)
    notice_language: Optional[str] = Field(None, max_length=40)
    method: Optional[str] = Field(None, max_length=40)
    given_by: Optional[str] = Field(None, max_length=20)
    proxy_name: Optional[str] = _TEXT
    proxy_relationship: Optional[str] = Field(None, max_length=80)
    evidence_ref: Optional[str] = Field(None, max_length=120)


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., max_length=200)
    sex: Optional[str] = Field(None, max_length=20)
    date_of_birth: Optional[str] = Field(None, max_length=40)
    age_years: Optional[int] = None
    phone: Optional[str] = Field(None, max_length=30)
    emergency_contact_name: Optional[str] = _TEXT
    emergency_contact_phone: Optional[str] = Field(None, max_length=30)
    uhid: Optional[str] = Field(None, max_length=40)
    guardian_relation: Optional[str] = _TEXT
    guardian_name: Optional[str] = _TEXT
    marital_status: Optional[str] = _TEXT
    preferred_language: Optional[str] = _TEXT
    address_line: Optional[str] = _TEXT
    city: Optional[str] = _TEXT
    district: Optional[str] = _TEXT
    state_code: Optional[str] = _TEXT
    pin_code: Optional[str] = Field(None, max_length=10)
    alt_phone: Optional[str] = Field(None, max_length=30)
    email: Optional[str] = _TEXT
    id_document_type: Optional[str] = Field(None, max_length=40)
    id_document_last4: Optional[str] = Field(None, max_length=4)
    occupation: Optional[str] = _TEXT
    abha_number: Optional[str] = Field(None, max_length=40)
    abha_address: Optional[str] = Field(None, max_length=120)
    abha_source: Optional[str] = Field(None, max_length=40)
    duplicate_review: Optional[DuplicateReview] = None
    idempotency_key: Optional[str] = Field(None, max_length=200)
    # opd-13: the payer category the patient names, optional.
    payer: Optional[PayerIn] = None
    # opd-19: the notice and consent step, optional.
    consent: Optional[RegistrationConsentIn] = None


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = _TEXT
    sex: Optional[str] = Field(None, max_length=20)
    date_of_birth: Optional[str] = Field(None, max_length=40)
    age_years: Optional[int] = None
    phone: Optional[str] = Field(None, max_length=30)
    uhid: Optional[str] = Field(None, max_length=40)
    abha_number: Optional[str] = Field(None, max_length=40)
    abha_address: Optional[str] = Field(None, max_length=120)


class IdentityCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_id: str = Field(..., max_length=80)
    name: Optional[str] = _TEXT
    date_of_birth: Optional[str] = Field(None, max_length=40)
    age_years: Optional[int] = None
    phone_last4: Optional[str] = Field(None, max_length=10)
    uhid: Optional[str] = Field(None, max_length=40)
    purpose: Optional[str] = Field(None, max_length=60)


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: Optional[int] = None
    reason: Optional[str] = Field(None, max_length=1000)
    name: Optional[str] = _TEXT
    sex: Optional[str] = Field(None, max_length=20)
    date_of_birth: Optional[str] = Field(None, max_length=40)
    age_years: Optional[int] = None
    phone: Optional[str] = Field(None, max_length=30)
    emergency_contact_name: Optional[str] = _TEXT
    emergency_contact_phone: Optional[str] = Field(None, max_length=30)
    uhid: Optional[str] = Field(None, max_length=40)
    guardian_relation: Optional[str] = _TEXT
    guardian_name: Optional[str] = _TEXT
    marital_status: Optional[str] = _TEXT
    preferred_language: Optional[str] = _TEXT
    address_line: Optional[str] = _TEXT
    city: Optional[str] = _TEXT
    district: Optional[str] = _TEXT
    state_code: Optional[str] = _TEXT
    pin_code: Optional[str] = Field(None, max_length=10)
    alt_phone: Optional[str] = Field(None, max_length=30)
    email: Optional[str] = _TEXT
    id_document_type: Optional[str] = Field(None, max_length=40)
    id_document_last4: Optional[str] = Field(None, max_length=4)
    occupation: Optional[str] = _TEXT


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: str = Field(..., max_length=40)
    reason: Optional[str] = Field(None, max_length=1000)


class SweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_pairs: Optional[int] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _request_meta(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _http(err: reg.RegistrationError) -> HTTPException:
    return HTTPException(status_code=err.status, detail=err.detail())


def _audit_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={
        "error": "audit_unavailable",
        "message": "The audit trail could not be written, so nothing was saved or shown. "
                   "Try again in a moment.",
    })


def _access(request: Request) -> Optional[dict]:
    scope = getattr(request.state, "orb_patient_scope", None)
    return dict(scope) if scope else None


def _through_opd_visit(request: Request) -> bool:
    """Did ONLY an outpatient visit let this reader in? Such a reader sees the
    record as the OPD record view draws it (registration_service.
    OPD_ARM_HIDDEN_FIELDS) and may not correct the hidden details."""
    return (_access(request) or {}).get("arm") == "opd_visit"


def _deny(request: Request, user: dict, err: reg.RegistrationError, patient_id: str) -> None:
    """The permission refusal on the PATCH, on the trail before the 403
    (authorization, like require_permission's own denials)."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="permission_denied",
        outcome="denied",
        summary_message=f"Registration correction refused: {err.code}",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"reason_code": err.code, "user_role": user.get("role")},
    )


def _index_read_audit(request: Request, user: dict, *, action: str, ids: list,
                      summary: str, extra: Optional[dict] = None) -> None:
    """A hospital-wide minimal-field disclosure, on the trail (phi_access,
    fail-closed) with the ids it showed, BEFORE anything is returned."""
    try:
        log_phi_read(
            request=request, user=user, action_type=action, summary_message=summary,
            target_type="patient_index", target_id=None,
            metadata={"matched_patient_ids": list(ids), "result_count": len(ids), **(extra or {})},
        )
    except AuditWriteError:
        raise _audit_unavailable()


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/config")
def registration_settings(user: dict = Depends(require_registration_desk("read"))):
    """UHID mode, whether the UHID series is ready, the duplicate mode and
    whether ABHA can be recorded. No patient data."""
    return reg.registration_config()


@router.get("/search")
def search_patient_index(
    request: Request,
    q: str = Query("", max_length=200),
    user: dict = Depends(require_registration_desk("read")),
):
    """Hospital-wide minimal-field search (ruling OPD-N4): UHID, name, sex, age,
    phone last 4, last visit date and inpatient now. Audited with the ids."""
    try:
        found = reg.search_index(q)
    except reg.RegistrationError as err:
        raise _http(err)
    ids = [c["patient_id"] for c in found["results"]]
    _index_read_audit(request, user, action="patient_index_searched", ids=ids,
                      summary=f"Patient index searched: {len(ids)} result(s)",
                      extra={"query_kinds": found["kinds"], "query_length": len(q.strip()),
                             "truncated": found["truncated"]})
    return {**found, "min_chars": reg.SEARCH_MIN_CHARS, "max_results": reg.SEARCH_MAX_RESULTS}


@router.post("/match")
def match_patient(
    body: MatchRequest,
    request: Request,
    user: dict = Depends(require_registration_desk("read")),
):
    """Who these details might already be (opd-5), before registering. A
    caller with a department never has a typed date of birth compared with a
    record outside it (the desk read includes patient.read_all, so a ward
    nurse reaches this route). check_incomplete says a cut scan left out
    records that could have scored in a band; blocked and review_required
    already count them, exactly as POST /patients will."""
    try:
        found = reg.find_matches(body.model_dump(), user=user)
    except reg.RegistrationError as err:
        raise _http(err)
    ids = [c["patient_id"] for c in found["candidates"]]
    _index_read_audit(request, user, action="registration_match_checked", ids=ids,
                      summary=f"Registration duplicate check: {len(ids)} candidate(s)",
                      extra={"bands": [c["band"] for c in found["candidates"]],
                             "truncated": found["truncated"],
                             "check_incomplete": found["check_incomplete"]})
    return found


@router.post("/patients", status_code=201)
def register_patient(
    body: RegistrationRequest,
    request: Request,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_permission(REGISTRATION_WRITE)),
):
    """Register a new patient (status 'registered'): one transaction, audited.
    A replay of the same Idempotency-Key answers 200 with the patient id and
    hospital number only (never the current record, which is read through the
    scoped GET); after 24 hours the key is refused with 409."""
    data = body.model_dump()
    key = (idempotency_key or "").strip() or data.pop("idempotency_key", None)
    data.pop("idempotency_key", None)
    try:
        result = reg.register_patient(data, user=user, idempotency_key=key,
                                      request_meta=_request_meta(request))
    except reg.RegistrationError as err:
        raise _http(err)
    except AuditWriteError:
        raise _audit_unavailable()
    if result.get("replayed"):
        response.status_code = 200
    return result


@router.post("/identity-check")
def identity_check(
    body: IdentityCheckRequest,
    request: Request,
    user: dict = Depends(require_registration_desk("write")),
):
    """What the patient STATES against the record (ruling OPD-S1): the answer
    is 'match' or 'no_match' and nothing else. Rate-limited per user and
    patient; every answer is audited. Scope comes from the authenticated
    caller (department_scope): a ward-scoped caller reaches its own patients
    and anyone with no active admission, never another ward's inpatient."""
    stated = body.model_dump()
    patient_id = stated.pop("patient_id")
    purpose = stated.pop("purpose", None)
    try:
        return reg.check_identity(patient_id, stated, user=user,
                                  department=department_scope(user),
                                  request_meta=_request_meta(request), purpose=purpose)
    except reg.RegistrationError as err:
        raise _http(err)
    except AuditWriteError:
        raise _audit_unavailable()


@router.get("/patients/{patient_id}")
def registration_record(
    patient_id: str,
    request: Request,
    user: dict = Depends(require_patient_access(
        REGISTRATION_READ, audit_action="registration_record_viewed", opd_arm=True)),
):
    """The full registration record, for a reader in scope."""
    try:
        record = reg.get_registration(patient_id)
    except reg.RegistrationError as err:
        raise _http(err)
    if _through_opd_visit(request):
        record = reg.redact_for_opd_arm(record)
    record["access"] = _access(request)
    return record


@router.patch("/patients/{patient_id}")
def correct_registration(
    patient_id: str,
    body: CorrectionRequest,
    request: Request,
    user: dict = Depends(require_patient_access(REGISTRATION_READ, opd_arm=True)),
):
    """A reasoned correction against the row_version the editor read. Fields
    need registration.write; the hospital number only the administrator may
    correct."""
    changes = body.model_dump(exclude_unset=True)
    row_version = changes.pop("row_version", None)
    reason = changes.pop("reason", None)
    role = user.get("role", "")
    try:
        result = reg.correct_demographics(
            patient_id, changes, row_version=row_version, reason=reason, user=user,
            may_edit_fields=has_permission(role, REGISTRATION_WRITE),
            may_correct_uhid=has_permission(role, ADMIN_OVERVIEW),
            request_meta=_request_meta(request),
            opd_arm_only=_through_opd_visit(request))
    except reg.RegistrationForbidden as err:
        _deny(request, user, err, patient_id)
        raise _http(err)
    except reg.RegistrationError as err:
        raise _http(err)
    except AuditWriteError:
        raise _audit_unavailable()
    if _through_opd_visit(request):
        result["patient"] = reg.redact_for_opd_arm(result["patient"])
    result["patient"]["access"] = _access(request)
    return result


@router.get("/patients/{patient_id}/changes")
def registration_changes(
    patient_id: str,
    request: Request,
    user: dict = Depends(require_patient_access(
        REGISTRATION_READ, audit_action="registration_changes_viewed", opd_arm=True)),
):
    """Every correction to this registration, newest first, with its reason."""
    try:
        history = reg.change_history(patient_id)
    except reg.RegistrationError as err:
        raise _http(err)
    if _through_opd_visit(request):
        history = reg.redact_changes_for_opd_arm(history)
    return history


@router.get("/patients/{patient_id}/card")
def registration_card(
    patient_id: str,
    user: dict = Depends(require_patient_access(
        REGISTRATION_READ, audit_action="uhid_card_printed", opd_arm=True)),
):
    """What the printed UHID card carries. The read is audited as a print."""
    try:
        return reg.uhid_card(patient_id)
    except reg.RegistrationError as err:
        raise _http(err)


@router.get("/duplicates")
def duplicate_worklist(
    request: Request,
    status: str = Query("open", max_length=40),
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(require_registration_desk("read")),
):
    """The possible-duplicate worklist, minimal fields per side, audited."""
    try:
        found = reg.list_duplicates(status=status, limit=limit)
    except reg.RegistrationError as err:
        raise _http(err)
    ids = found.pop("patient_ids")
    _index_read_audit(request, user, action="duplicate_worklist_viewed", ids=ids,
                      summary=f"Duplicate worklist viewed: {len(found['items'])} item(s)",
                      extra={"status": status})
    return found


@router.post("/duplicates/sweep")
def duplicate_sweep(
    request: Request,
    body: Optional[SweepRequest] = None,
    user: dict = Depends(require_permission(REGISTRATION_READ, ADMIN_OVERVIEW)),
):
    """A bounded pass over the index that opens worklist items (administrator)."""
    try:
        return reg.sweep_duplicates(
            user=user, max_pairs=(body.max_pairs if body and body.max_pairs else reg.SWEEP_MAX_PAIRS),
            request_meta=_request_meta(request))
    except reg.RegistrationError as err:
        raise _http(err)
    except AuditWriteError:
        raise _audit_unavailable()


@router.get("/payers")
def registration_payers(user: dict = Depends(require_registration_desk("read"))):
    """The payers the desk may record at registration (opd-13): active rows of
    the payer master, code, name and type only. Configuration, no patient
    data."""
    from services.billing_masters import list_payers
    return {"payers": [{"code": p["code"], "name": p["name"], "payer_type": p["payer_type"]}
                       for p in list_payers(active_only=True)]}


def _charge_rule_error(err) -> HTTPException:
    return HTTPException(status_code=err.status, detail=err.detail())


@router.get("/charge-rules")
def outpatient_charge_rules(
    include_retired: bool = Query(False),
    user: dict = Depends(require_registration_desk("read")),
):
    """Which code each outpatient consultation, follow-up and the registration
    fee is charged against (opd-13), and when the consultation is charged.
    Configuration, no patient data."""
    from services import opd_charges_service as charges
    return charges.list_rules(include_retired=include_retired)


@router.post("/charge-rules", status_code=201)
def create_outpatient_charge_rule(
    body: ChargeRuleIn,
    request: Request,
    user: dict = Depends(require_permission(BILLING_TARIFF)),
):
    """Add a charge rule (billing.tariff: the price list's own permission).
    Audited as configuration in the same transaction."""
    from services import opd_charges_service as charges
    try:
        return charges.create_rule(body.model_dump(), actor=user,
                                   request_meta=_request_meta(request))
    except charges.OpdChargeError as err:
        raise _charge_rule_error(err)


@router.post("/charge-rules/{rule_id}/retire")
def retire_outpatient_charge_rule(
    rule_id: str,
    body: RetireRuleIn,
    request: Request,
    user: dict = Depends(require_permission(BILLING_TARIFF)),
):
    """Retire a charge rule with a reason. Nothing is deleted."""
    from services import opd_charges_service as charges
    try:
        return charges.retire_rule(rule_id, reason=body.reason, actor=user,
                                   request_meta=_request_meta(request))
    except charges.OpdChargeError as err:
        raise _charge_rule_error(err)


@router.post("/duplicates/{candidate_id}/resolve")
def resolve_duplicate(
    candidate_id: int,
    body: ResolveRequest,
    request: Request,
    user: dict = Depends(require_permission(REGISTRATION_WRITE)),
):
    """Not the same person, or the same person (records are never merged
    here). The person who registered past the warning may not resolve it."""
    try:
        return reg.resolve_duplicate(candidate_id, resolution=body.resolution,
                                     reason=body.reason, user=user,
                                     request_meta=_request_meta(request))
    except reg.RegistrationError as err:
        raise _http(err)
    except AuditWriteError:
        raise _audit_unavailable()
