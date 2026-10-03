"""Orb eMAR — Medication Administration Routes.

The bedside endpoint of the medication loop: a nurse/doctor records that a
charted dose was given (or withheld, with a reason) and the ward can read the
append-only administration history per patient. Guards live in
services.emar_service (active order only, unverified AI meds blocked,
mandatory not-given reason) and surface here as 422s.

The ALLERGY INTERLOCK and the DOSE GUARD are the two guards that are not flat
refusals, so they do not share the 422: a severe allergy match, an allergy list
that could not be read, or a dose/route the dose guard stopped returns 428
Precondition Required with the full screen in the body, and the caller
re-submits with ``allergy_override_reason`` / ``dose_override_reason``. Same
contract as the Scribe sign gate and the order-time medication safety gate, so a
client that already understands one understands all of them.

Clearing a CHARTED allergy is the one exception to "428 means you may proceed
with a reason": it is a prescribing decision (``medication.prescribe``), so an
attempt by anyone else is a 403 — the gate does not open for them however many
reasons they type — carrying the same allergy payload so the screen can still
say what the hazard is and who can authorise it.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from database import db_session
from permissions import (
    department_scope,
    patient_in_scope,
    require_patient_access,
    require_permission,
)
from services import allergy_interlock, drug_safety_service, emar_service
from services.allergy_interlock import AllergyInterlockBlocked, AllergyOverrideNotPermitted
from services.audit_service import log_audit_event
from services.drug_safety_service import DoseGuardBlocked

router = APIRouter(prefix="/api/emar", tags=["eMAR"])


class AdministerRequest(BaseModel):
    status: str  # 'given' | 'not_given'
    dose_given: Optional[str] = None
    route_given: Optional[str] = None
    not_given_reason: Optional[str] = None  # required (non-empty) when status='not_given'
    confirmation_method: str = "typed"  # 'typed' | 'barcode'
    notes: Optional[str] = None
    # Typed clinical justification for giving a drug the patient has a
    # documented allergy to (de-labelled allergy, desensitisation protocol,
    # supervised challenge), or for proceeding when the allergy list is
    # unreadable. Only consulted when the interlock actually fires.
    #
    # PRESCRIBER ONLY over a CHARTED allergy: the caller must hold
    # `medication.prescribe`, or this field is refused with a 403 and the dose is
    # not recorded (it is never quietly ignored — a dose charted while the reason
    # for it was dropped is the worst of both). An unreadable-allergy-list block
    # is a different case and any administering user may clear it; see
    # services/allergy_interlock.py.
    allergy_override_reason: Optional[str] = None
    # Typed clinical justification for a dose or route the DOSE GUARD stopped
    # (a digitalisation loading dose, a U-500 insulin regimen, a deliberate
    # bedside deviation). Only consulted when the dose guard actually fires.
    dose_override_reason: Optional[str] = None


def _enforce_medication_scope(med_id: int, user: dict, request: Request) -> None:
    """404 unless the medication's patient is on the caller's ward. BLOCKING.

    Same rule and same refusal as require_patient_access, resolved through the
    surrogate id: 404 rather than 403 (a 403 would confirm the order exists),
    and the attempt is audited before the refusal so a cross-ward probe shows in
    the trail. An unknown medication id falls through to the service, which
    answers it as before.
    """
    dept = department_scope(user)
    if dept is None:
        return
    with db_session() as db:
        med = db.execute(
            "SELECT patient_id FROM medications WHERE id = ?", (med_id,)
        ).fetchone()
        if med is None or patient_in_scope(db, med["patient_id"], dept):
            return
        patient_id = med["patient_id"]
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="patient_scope_denied",
        outcome="denied",
        summary_message=(
            f"Medication {med_id} belongs to a patient outside the caller's "
            f"department ({dept})"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type="medication",
        target_id=str(med_id),
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"department": dept, "scope_source": "authenticated_user"},
    )
    raise HTTPException(status_code=404, detail="Medication not found")


@router.post("/medications/{med_id}/administer")
def administer_medication(
    med_id: int,
    req: AdministerRequest,
    request: Request,
    user: dict = Depends(require_permission("medication.write")),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    """Record a bedside administration of a charted medication.

    Idempotent: offline replays resend the same Idempotency-Key header, so a
    key that already exists returns the existing record instead of
    double-charting a dose. The service audits every fresh record on the
    tamper-evident chain.

    428 → a guard stopped this dose, and it can be cleared with a typed reason.
    ``detail.allergy`` carries the allergy alerts, the decision and why;
    re-submit with ``allergy_override_reason``. ``detail.dose`` carries the dose
    guard's findings — an order-of-magnitude amount or a never-correct route;
    re-submit with ``dose_override_reason``. Either override is recorded against
    the order and the actor, in the same transaction as the dose.

    Both guards can be armed at once; the allergy one is reported first because
    it is the more serious finding. The client should therefore be prepared to
    receive a second 428 after clearing the first, and must not assume one
    acknowledgement clears everything.

    403 → the caller may record doses (`medication.write`) but may not CLEAR A
    CHARTED ALLERGY, which is a prescribing decision (`medication.prescribe`).
    Deliberately not a 428: 428 is this API's "you may do this, but say why", and
    answering a nurse's override attempt with it would invite her to keep typing
    reasons at a gate that will never open. The body still carries
    ``detail.allergy`` so the screen she is looking at can state the allergen,
    the severity and who can authorise it, rather than degrading to a bare
    "forbidden".

    404 → the medication belongs to a patient outside the caller's ward. The
    route is keyed by medication id, so require_patient_access (which reads
    {patient_id} off the path) could not guard it, and any nurse could chart a
    dose on any inpatient in the hospital.
    """
    _enforce_medication_scope(med_id, user, request)
    try:
        record = emar_service.record_administration(
            med_id,
            user["id"],
            status=req.status,
            dose_given=req.dose_given,
            route_given=req.route_given,
            not_given_reason=req.not_given_reason,
            confirmation_method=req.confirmation_method,
            idempotency_key=idempotency_key,
            notes=req.notes,
            allergy_override_reason=req.allergy_override_reason,
            dose_override_reason=req.dose_override_reason,
        )
    except AllergyOverrideNotPermitted as e:
        # CAUGHT BEFORE ITS PARENT — it is a subclass of AllergyInterlockBlocked
        # (so that any handler which does not know about it still stops the
        # dose), and Python takes the first matching clause. Moving this below
        # the 428 handler would silently downgrade the refusal into an
        # invitation to retry.
        #
        # Audited here rather than in the service because the service raises
        # from inside the administration transaction, which rolls back — the
        # denial has to be written on a connection that survives it. Same
        # placement, and the same reasoning, as the permission_denied audit in
        # permissions.require_permission. `security` is fail-closed: an
        # unrecordable attempt to clear an anaphylaxis interlock must not be
        # answered quietly with a 403.
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="security",
            action_type="medication_allergy_override_refused",
            outcome="denied",
            summary_message=(
                f"{user['id']} ({e.actor_role or 'unknown role'}) attempted to override "
                f"the allergy interlock on medication {med_id} without "
                f"'{e.required_permission}', dose refused"
            ),
            actor_user_id=user["id"],
            actor_role=e.actor_role or user.get("role"),
            target_type="medication",
            target_id=str(med_id),
            patient_id=e.screen.get("patient_id"),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={
                "medication_id": med_id,
                "drug_name": e.screen.get("drug"),
                "allergens": allergy_interlock.blocking_allergens(e.screen),
                "required_permission": e.required_permission,
                # The REASON THEY TYPED is kept. An unauthorised attempt with a
                # real clinical justification behind it ("consultant told me to
                # give it") is a working-practice signal the ward needs, and
                # discarding it would leave the review with only "someone tried".
                "attempted_reason": (req.allergy_override_reason or "").strip()[:500],
                "alerts": e.screen.get("alerts") or [],
            },
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "allergy_override_not_permitted",
                "message": str(e),
                "required_permission": e.required_permission,
                "allergy": allergy_interlock.summarize(e.screen),
            },
        )
    except AllergyInterlockBlocked as e:
        raise HTTPException(
            status_code=428,
            detail={
                "error": ("allergy_screen_unavailable"
                          if e.screen.get("screen_failed")
                          else "allergy_acknowledgement_required"),
                "message": str(e),
                "allergy": allergy_interlock.summarize(e.screen),
            },
        )
    except DoseGuardBlocked as e:
        raise HTTPException(
            status_code=428,
            detail={
                "error": "dose_acknowledgement_required",
                "message": str(e),
                "dose": drug_safety_service.summarize_dose_check(e.check),
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"status": "success", "administration": record}


@router.get("/patients/{patient_id}/administrations")
def get_patient_administrations(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("patient.read_all",
                               audit_action="emar_administrations_viewed")
    ),
):
    """Administration history for a patient, newest first."""
    with db_session() as db:
        exists = db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Patient not found")
    return {"administrations": emar_service.list_administrations(patient_id)}
