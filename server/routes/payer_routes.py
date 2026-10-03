"""Orb — Insurance / TPA desk routes.

Payer master, patient policy capture, pre-authorisation, the claim lifecycle,
the denial log and the aging work queue. Everything here is a RECORD of work the
desk did on the payer's own portal, by e-mail or on paper. Orb transmits nothing
to any TPA, insurer or scheme, and ``NO_TRANSMISSION_NOTICE`` rides on every
response so no client can render one of these payloads without the disclaimer
being available to it.

THREE THINGS THIS FILE IS CAREFUL ABOUT
───────────────────────────────────────
1. IT NEVER TOUCHES A CLINICAL PATH. It reads `patients.name` for a label and
   writes only to `payers`, `patient_payer_policies`, `claims` and
   `claim_events`. No route here can refuse, delay or gate an admission, an
   order, an administration or a discharge.

2. SYNC HANDLERS. Every handler is a plain `def`, so FastAPI runs it in the
   threadpool and the blocking SQLite calls in payer_service never touch the
   event loop (tests/test_event_loop_discipline.py enforces the rule for
   `async def`; the honest way to satisfy it for a DB-heavy module is not to be
   async at all).

3. THE MODULE FLAG IS A 404, NOT A 403. A site that turns the payer desk off
   (ORB_PAYER=0) does not have an insurance desk; a 403 would say "you are not
   allowed", which is a different and wrong statement.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import config
from permissions import (
    PAYER_READ,
    PAYER_WRITE,
    enforce_patient_scope,
    require_permission,
)
from services.audit_service import log_audit_event
from services.payer_service import (
    AGING_BUCKETS,
    CLAIM_STATUSES,
    CLAIM_TYPES,
    DENIAL_REASON_CODES,
    LEGAL_TRANSITIONS,
    NO_TRANSMISSION_NOTICE,
    NO_TRANSMISSION_SHORT,
    PAYER_TYPES,
    ClaimError,
    IllegalTransition,
    PayerError,
    add_note,
    cancel_claim,
    capture_policy,
    close_claim,
    create_claim,
    denial_log,
    desk_summary,
    get_claim,
    list_claims,
    list_payers,
    list_policies,
    record_appeal,
    record_decision,
    record_preauth_decision,
    record_preauth_request,
    record_query,
    record_settlement,
    record_submission,
    set_claimed_amount,
    set_policy_status,
    upsert_payer,
    verify_policy,
    work_queue,
)

logger = logging.getLogger("orb.payer.routes")

router = APIRouter(prefix="/api/payer", tags=["payer"])


# ── The module switch ────────────────────────────────────────────────────────

def require_module() -> None:
    """404 the whole desk when a deployment has hidden this module.

    Read from `config.PAYER_ENABLED` at REQUEST time, not import time, so a test
    (or an ops toggle that reloads config) that patches the constant actually
    changes behaviour rather than being shadowed by a value captured when the
    module was first imported.
    """
    if not getattr(config, "PAYER_ENABLED", True):
        raise HTTPException(
            status_code=404,
            detail="The insurance / TPA desk is not enabled for this deployment.",
        )


# ── Who can reach the desk ───────────────────────────────────────────────────
# `payer.read` to see it, `payer.write` to move a claim through it.
#
# This used to be `admin.overview`, the GOVERNANCE permission — the one that also
# opens the audit log and the admin dashboard — because the role table had no
# insurance role and this module could not add a permission. So a TPA clerk who
# needed to record a settlement was handed the hospital's tamper-evidence record
# to do it. The pair now exists in permissions.py and that is undone.
#
# Nothing about who can reach the desk changes TODAY: Orb's role table still has
# no insurance role (a clerk holds an `admin` account, which is why
# src/shellConfig.tsx shows these tabs to `admin` only) and `admin` holds both
# new permissions. What changes is that the desk no longer DEPENDS on a
# governance grant — a `tpa_clerk` role can now be given exactly these two.
#
# WRITES REQUIRE READ AS WELL, and that is not belt-and-braces. Every write
# handler answers with the claim it touched — `POST /claims/{id}/note` returns
# the whole file, patient name, policy number and all — so a write-only grant
# would be a read grant with an extra step. Requiring both means `payer.read` is
# the only way to see a claim, whichever verb was used to ask for it.
_DESK_READ = require_permission(PAYER_READ)
_DESK_WRITE = require_permission(PAYER_READ, PAYER_WRITE)


def _audit(
    user: dict,
    action: str,
    *,
    outcome: str = "success",
    summary: str,
    target_id: Optional[str] = None,
    patient_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    category: str = "financial_administration",
) -> None:
    """One audit line per desk action.

    Category is `financial_administration` (fail-soft) for money actions, and
    `phi_access` (FAIL-CLOSED, so the read is refused if it cannot be audited)
    for anything that returns a named patient's policy or claim file. A claim
    carries a patient's name, MRN, diagnosis-adjacent package code and insurance
    policy number; that is PHI by any reading, and reading it must leave a
    trace even though nobody would call it clinical.
    """
    log_audit_event(
        correlation_id=f"payer-{action}-{target_id or 'na'}",
        event_category=category,
        action_type=f"payer_{action}",
        outcome=outcome,
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type="claim",
        target_id=str(target_id) if target_id is not None else None,
        metadata=metadata,
    )


def _handle(fn, *args, **kwargs):
    """Run a service call, turning its refusals into the right HTTP status.

    409 for an illegal lifecycle move — it is a CONFLICT with the claim's
    current state, not a malformed request, and the desk's UI shows the message
    verbatim ("a claim that is 'denied' cannot become 'settled'"). 400 for
    figures that cannot be true. 404 for a claim that is not there.
    """
    try:
        return fn(*args, **kwargs)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ClaimError, PayerError) as exc:
        message = str(exc)
        if "not found" in message or message.startswith("unknown patient") \
                or message.startswith("unknown payer"):
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=400, detail=message)


def _wrap(payload: dict) -> dict:
    """Attach the honesty notice to every single response body.

    Not "on the screen that shows claims" — on every payload, because payloads
    get consumed by things that were not the screen: an export, a future report,
    somebody's curl. The sentence travels with the data.
    """
    payload.setdefault("notice", NO_TRANSMISSION_NOTICE)
    payload.setdefault("notice_short", NO_TRANSMISSION_SHORT)
    payload.setdefault("transmits_to_payer", False)
    return payload


# ══════════════════════════════════════════════════════════════════════════════
#  Request bodies
# ══════════════════════════════════════════════════════════════════════════════

class PayerIn(BaseModel):
    code: str
    name: str
    payer_type: str = "tpa"
    tariff_plan_id: Optional[int] = None
    gstin: Optional[str] = None
    state_code: Optional[str] = None
    address: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    claim_submission_mode: Optional[str] = None
    portal_url: Optional[str] = None
    credit_days: Optional[int] = None
    preauth_required: bool = False
    is_active: bool = True
    notes: Optional[str] = None
    #: Removing an existing tariff link has to be asked for; a missing or null
    #: tariff_plan_id never removes it (payer_service.upsert_payer).
    clear_tariff_plan: bool = False


class PolicyIn(BaseModel):
    patient_id: str
    payer_code: str
    admission_id: Optional[str] = None
    policy_number: Optional[str] = None
    member_id: Optional[str] = None
    scheme_beneficiary_id: Optional[str] = None
    subscriber_name: Optional[str] = None
    relationship_to_subscriber: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    # None means NOT KNOWN and is stored as such. It is NOT zero cover.
    sum_insured_paise: Optional[int] = None
    copay_percent_bp: int = 0
    preauth_required: Optional[bool] = None
    is_primary: bool = True


class PolicyStatusIn(BaseModel):
    status: str


class ClaimIn(BaseModel):
    patient_id: str
    claim_type: str = "cashless"
    payer_code: Optional[str] = None
    policy_id: Optional[int] = None
    admission_id: Optional[str] = None
    invoice_id: Optional[int] = None
    claimed_paise: Optional[int] = None
    package_code: Optional[str] = None
    notes: Optional[str] = None
    #: The same key again returns the claim it opened instead of a second one.
    idempotency_key: Optional[str] = None


class ClaimedAmountIn(BaseModel):
    claimed_paise: int


class PreauthIn(BaseModel):
    requested_paise: int
    reference: Optional[str] = None
    remark: Optional[str] = None
    occurred_at: Optional[str] = None


class PreauthDecisionIn(BaseModel):
    decision: str = Field(description="approved | partially_approved | denied")
    approved_paise: Optional[int] = None
    reference: Optional[str] = None
    denial_code: Optional[str] = None
    denial_reason: Optional[str] = None
    remark: Optional[str] = None
    occurred_at: Optional[str] = None


class SubmissionIn(BaseModel):
    """Recording a submission the desk made ELSEWHERE. Orb sends nothing."""
    claimed_paise: Optional[int] = None
    external_ref: Optional[str] = None
    submitted_at: Optional[str] = None
    remark: Optional[str] = None


class QueryIn(BaseModel):
    query_text: str
    answered: bool = False
    document_ref: Optional[str] = None
    occurred_at: Optional[str] = None


class DecisionIn(BaseModel):
    decision: str = Field(description="approved | partially_approved | denied")
    approved_paise: Optional[int] = None
    tds_paise: int = 0
    denial_code: Optional[str] = None
    denial_reason: Optional[str] = None
    external_ref: Optional[str] = None
    remark: Optional[str] = None
    occurred_at: Optional[str] = None


class SettlementIn(BaseModel):
    settled_paise: int
    tds_paise: Optional[int] = None
    utr_reference: Optional[str] = None
    remark: Optional[str] = None
    occurred_at: Optional[str] = None


class AppealIn(BaseModel):
    grounds: str
    document_ref: Optional[str] = None
    occurred_at: Optional[str] = None


class ReasonIn(BaseModel):
    reason: Optional[str] = None


class NoteIn(BaseModel):
    note: str


# ══════════════════════════════════════════════════════════════════════════════
#  Reference / meta
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/meta")
def meta(user: dict = Depends(_DESK_READ), _m: None = Depends(require_module)) -> dict:
    """Everything the panel needs to render its forms without hard-coding a
    vocabulary that could drift from the server's."""
    return _wrap({
        "payer_types": sorted(PAYER_TYPES),
        "claim_types": sorted(CLAIM_TYPES),
        "claim_statuses": list(CLAIM_STATUSES),
        "legal_transitions": {k: sorted(v) for k, v in LEGAL_TRANSITIONS.items()},
        "aging_buckets": [b[0] for b in AGING_BUCKETS],
        "denial_reason_codes": [{"code": c, "label": l} for c, l in DENIAL_REASON_CODES],
        "denial_codes_are_local": True,
    })


@router.get("/summary")
def summary(user: dict = Depends(_DESK_READ), _m: None = Depends(require_module)) -> dict:
    return _wrap(desk_summary())


# ══════════════════════════════════════════════════════════════════════════════
#  Payer master
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/payers")
def payers(
    active_only: bool = True,
    payer_type: Optional[str] = None,
    include_self_pay: bool = True,
    user: dict = Depends(_DESK_READ),
    _m: None = Depends(require_module),
) -> dict:
    return _wrap({
        "payers": list_payers(
            active_only=active_only,
            payer_type=payer_type,
            include_self_pay=include_self_pay,
        ),
    })


@router.post("/payers")
def save_payer(
    body: PayerIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    # Only the fields the client sent: a payer update is partial, so a form that
    # does not show a column cannot overwrite it with a default.
    row = _handle(upsert_payer, **body.model_dump(exclude_unset=True))
    _audit(
        user, "payer_saved",
        summary=f"Payer {row['code']} ({row['payer_type']}) saved",
        target_id=str(row["id"]),
        metadata={"code": row["code"], "payer_type": row["payer_type"]},
    )
    return _wrap({"payer": row})


# ══════════════════════════════════════════════════════════════════════════════
#  Patient policies — the capture path the admission form does not have
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/policies")
def policies(
    patient_id: Optional[str] = None,
    admission_id: Optional[str] = None,
    active_only: bool = False,
    user: dict = Depends(_DESK_READ),
    _m: None = Depends(require_module),
) -> dict:
    # PHI SCOPE (2026-08-12). patient_id arrives on the QUERY STRING, so
    # require_patient_access — which resolves it from the PATH — cannot guard
    # this route, and the path-param drift guard greps straight past it. An
    # insurance file names its patient, so an unscoped filter is the same
    # disclosure as a chart read. 404, never 403: a 403 confirms the patient
    # exists. Plain `def` route, so FastAPI threadpools this blocking check.
    enforce_patient_scope(user, patient_id)
    rows = list_policies(
        patient_id=patient_id, admission_id=admission_id, active_only=active_only
    )
    if patient_id:
        # Fail-closed: a named patient's insurance file is PHI, and a read that
        # cannot be audited is a read that does not happen.
        _audit(
            user, "policies_read",
            category="phi_access",
            summary=f"Insurance policies read for {patient_id}",
            patient_id=patient_id,
            metadata={"count": len(rows)},
        )
    return _wrap({"policies": rows})


@router.post("/policies")
def save_policy(
    body: PolicyIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    """Capture a patient's cover.

    This exists because the clinical admission route captures NO payer
    information — an admission must never wait on an insurance card — so the
    desk records cover here, after the fact. Nothing about this write can affect
    the admission it refers to.
    """
    # PHI SCOPE (2026-08-12). The patient is named in the REQUEST BODY, so no
    # path-based guard can see it. Writing a financial record against another
    # ward's patient is the same boundary breach as reading their chart, and it
    # leaves a durable row behind. 404, never 403 — a 403 confirms they exist.
    enforce_patient_scope(user, body.patient_id)
    row = _handle(capture_policy, created_by=user.get("id"), **body.model_dump())
    _audit(
        user, "policy_captured",
        category="phi_access",
        summary=(
            f"Insurance policy captured for {body.patient_id}: "
            f"{row.get('payer_name') or body.payer_code}"
        ),
        patient_id=body.patient_id,
        target_id=str(row["id"]),
        metadata={
            "payer_code": body.payer_code,
            "sum_insured_is_configured": row["sum_insured_is_configured"],
        },
    )
    return _wrap({"policy": row})


@router.post("/policies/{policy_id}/verify")
def do_verify_policy(
    policy_id: int,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    row = _handle(verify_policy, policy_id=policy_id, verified_by=user.get("id") or "unknown")
    _audit(
        user, "policy_verified",
        summary=f"Policy #{policy_id} marked verified with the payer (outside Orb)",
        patient_id=row.get("patient_id"),
        target_id=str(policy_id),
    )
    return _wrap({"policy": row})


@router.post("/policies/{policy_id}/status")
def do_policy_status(
    policy_id: int,
    body: PolicyStatusIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    row = _handle(set_policy_status, policy_id=policy_id, status=body.status)
    _audit(
        user, "policy_status",
        summary=f"Policy #{policy_id} set to {body.status}",
        patient_id=row.get("patient_id"),
        target_id=str(policy_id),
        metadata={"status": body.status},
    )
    return _wrap({"policy": row})


# ══════════════════════════════════════════════════════════════════════════════
#  Claims
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/claims")
def claims(
    status: Optional[str] = None,
    payer_code: Optional[str] = None,
    patient_id: Optional[str] = None,
    claim_type: Optional[str] = None,
    open_only: bool = False,
    search: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(_DESK_READ),
    _m: None = Depends(require_module),
) -> dict:
    # PHI SCOPE (2026-08-12). patient_id arrives on the QUERY STRING, so
    # require_patient_access — which resolves it from the PATH — cannot guard
    # this route, and the path-param drift guard greps straight past it. An
    # insurance file names its patient, so an unscoped filter is the same
    # disclosure as a chart read. 404, never 403: a 403 confirms the patient
    # exists. Plain `def` route, so FastAPI threadpools this blocking check.
    enforce_patient_scope(user, patient_id)
    rows = list_claims(
        status=status, payer_code=payer_code, patient_id=patient_id,
        claim_type=claim_type, open_only=open_only, search=search, limit=limit,
    )
    return _wrap({"claims": rows, "total": len(rows)})


@router.get("/claims/{claim_id}")
def claim_detail(
    claim_id: int,
    user: dict = Depends(_DESK_READ),
    _m: None = Depends(require_module),
) -> dict:
    claim = _handle(get_claim, claim_id)
    _audit(
        user, "claim_read",
        category="phi_access",
        summary=f"Claim {claim.get('claim_number') or claim_id} opened",
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
    )
    return _wrap({"claim": claim})


@router.post("/claims")
def new_claim(
    body: ClaimIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    # PHI SCOPE (2026-08-12). The patient is named in the REQUEST BODY, so no
    # path-based guard can see it. Writing a financial record against another
    # ward's patient is the same boundary breach as reading their chart, and it
    # leaves a durable row behind. 404, never 403 — a 403 confirms they exist.
    enforce_patient_scope(user, body.patient_id)
    claim = _handle(create_claim, created_by=user.get("id"), **body.model_dump())
    replayed = bool(claim.get("replayed"))
    _audit(
        user, "claim_create_replayed" if replayed else "claim_created",
        summary=(
            f"Repeat of the request that opened claim {claim.get('claim_number') or claim['id']}; "
            "no new claim"
            if replayed else
            f"Claim {claim.get('claim_number') or claim['id']} opened for "
            f"{claim['patient_id']} against {claim.get('payer_name')}"
        ),
        patient_id=claim["patient_id"],
        target_id=str(claim["id"]),
        metadata={"claim_type": claim["claim_type"], "payer_code": claim.get("payer_code"),
                  "replayed": replayed},
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/amount")
def set_amount(
    claim_id: int,
    body: ClaimedAmountIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    claim = _handle(
        set_claimed_amount,
        claim_id=claim_id, claimed_paise=body.claimed_paise, actor_id=user.get("id"),
    )
    _audit(
        user, "claim_amount_set",
        summary=f"Claim {claim.get('claim_number') or claim_id} amount set to "
                f"{claim['settlement']['claimed_display']}",
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
        metadata={"claimed_paise": body.claimed_paise},
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/preauth")
def preauth(
    claim_id: int,
    body: PreauthIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    """RECORD that a pre-authorisation was raised with the payer elsewhere.

    This endpoint does not contact anybody. If it ever does, it will not be this
    endpoint — it will be a new one, named for what it does.
    """
    claim = _handle(
        record_preauth_request,
        claim_id=claim_id, actor_id=user.get("id"), **body.model_dump(),
    )
    _audit(
        user, "preauth_recorded",
        summary=f"Pre-auth request RECORDED (not transmitted) on claim "
                f"{claim.get('claim_number') or claim_id}",
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
        metadata={"requested_paise": body.requested_paise, "transmitted": False},
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/preauth/decision")
def preauth_decision(
    claim_id: int,
    body: PreauthDecisionIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    claim = _handle(
        record_preauth_decision,
        claim_id=claim_id, actor_id=user.get("id"), **body.model_dump(),
    )
    _audit(
        user, "preauth_decision",
        summary=f"Pre-auth {body.decision} recorded on claim "
                f"{claim.get('claim_number') or claim_id}",
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
        metadata={"decision": body.decision, "approved_paise": body.approved_paise},
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/submission")
def submission(
    claim_id: int,
    body: SubmissionIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    """RECORD a submission the desk made on the payer's portal / by e-mail /
    on paper. The route is `/submission`, not `/submit`, on purpose: it records
    a noun that already happened, it does not perform a verb."""
    claim = _handle(
        record_submission,
        claim_id=claim_id, actor_id=user.get("id"), **body.model_dump(),
    )
    _audit(
        user, "submission_recorded",
        summary=f"Claim {claim.get('claim_number') or claim_id} RECORDED as submitted "
                f"to {claim.get('payer_name')} (recorded in Orb only: Orb did not transmit)",
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
        metadata={"external_ref": body.external_ref, "transmitted": False},
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/query")
def query(
    claim_id: int,
    body: QueryIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    claim = _handle(
        record_query,
        claim_id=claim_id, actor_id=user.get("id"), **body.model_dump(),
    )
    _audit(
        user, "query_recorded",
        summary=("Query answered on claim " if body.answered else "Payer query recorded on claim ")
                + str(claim.get("claim_number") or claim_id),
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
        metadata={"answered": body.answered},
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/decision")
def decision(
    claim_id: int,
    body: DecisionIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    claim = _handle(
        record_decision,
        claim_id=claim_id, actor_id=user.get("id"), **body.model_dump(),
    )
    _audit(
        user, "decision_recorded",
        summary=f"Payer decision '{body.decision}' recorded on claim "
                f"{claim.get('claim_number') or claim_id}",
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
        metadata={
            "decision": body.decision,
            "approved_paise": body.approved_paise,
            "denial_code": body.denial_code,
        },
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/settlement")
def settlement(
    claim_id: int,
    body: SettlementIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    """Record money that arrived. Short settlement is COMPUTED from the approved
    amount, the co-pay and the TDS — never typed in."""
    claim = _handle(
        record_settlement,
        claim_id=claim_id, actor_id=user.get("id"), **body.model_dump(),
    )
    short = claim["settlement"]["short_settled_paise"] or 0
    _audit(
        user, "settlement_recorded",
        summary=(
            f"Settlement recorded on claim {claim.get('claim_number') or claim_id}: "
            f"{claim['settlement']['settled_display']} received"
            + (f", SHORT by {claim['settlement']['short_settled_display']}" if short > 0 else "")
        ),
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
        metadata={
            "settled_paise": body.settled_paise,
            "short_settled_paise": short,
            "is_short_settled": claim["settlement"]["is_short_settled"],
        },
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/appeal")
def appeal(
    claim_id: int,
    body: AppealIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    claim = _handle(
        record_appeal,
        claim_id=claim_id, actor_id=user.get("id"), **body.model_dump(),
    )
    _audit(
        user, "appeal_recorded",
        summary=f"Appeal recorded on claim {claim.get('claim_number') or claim_id}",
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/close")
def close(
    claim_id: int,
    body: ReasonIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    claim = _handle(close_claim, claim_id=claim_id, reason=body.reason, actor_id=user.get("id"))
    _audit(
        user, "claim_closed",
        summary=f"Claim {claim.get('claim_number') or claim_id} closed",
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/cancel")
def cancel(
    claim_id: int,
    body: ReasonIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    claim = _handle(
        cancel_claim, claim_id=claim_id, reason=body.reason or "", actor_id=user.get("id")
    )
    _audit(
        user, "claim_cancelled",
        summary=f"Claim {claim.get('claim_number') or claim_id} cancelled",
        patient_id=claim.get("patient_id"),
        target_id=str(claim_id),
        metadata={"reason": body.reason},
    )
    return _wrap({"claim": claim})


@router.post("/claims/{claim_id}/note")
def note(
    claim_id: int,
    body: NoteIn,
    user: dict = Depends(_DESK_WRITE),
    _m: None = Depends(require_module),
) -> dict:
    claim = _handle(add_note, claim_id=claim_id, note=body.note, actor_id=user.get("id"))
    return _wrap({"claim": claim})


# ══════════════════════════════════════════════════════════════════════════════
#  The work queue + the denial log
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/work-queue")
def queue(
    payer_code: Optional[str] = None,
    bucket: Optional[str] = None,
    limit: int = Query(300, ge=1, le=1000),
    user: dict = Depends(_DESK_READ),
    _m: None = Depends(require_module),
) -> dict:
    return _wrap(work_queue(payer_code=payer_code, bucket=bucket, limit=limit))


@router.get("/denials")
def denials(
    payer_code: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(_DESK_READ),
    _m: None = Depends(require_module),
) -> dict:
    return _wrap(denial_log(payer_code=payer_code, limit=limit))
