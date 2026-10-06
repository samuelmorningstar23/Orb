"""Orb — I-PASS Shift-Handoff Automation Routes.

Exposes endpoints to auto-draft a structured I-PASS handoff from the last ~12h
of a patient's data, persist it as a draft handoff note, and list recent
handoffs. See services/ipass_service.py for the assembly logic.

ALSO SERVES THE WARD ROUND, in two calls that are deliberately separate:

  POST /{patient_id}/reasoned-assessment/prepare   ask early, return at once
  GET  /{patient_id}/reasoned-assessment           read what is there, or nothing

Sage's full reasoned assessment takes 42-57 s. That is useless at a
deteriorating bedside and fine here, but ONLY because the asking and the
reading are different moments: the handover is composed ahead of the
change-over, and the round can prepare the next patient while the clinician is
still at this one. No endpoint in this file ever blocks on a model.

Both serve the same block, from the same builder as the handover sheet's
supplement, so the round and the handover cannot show a clinician two different
versions of the machine's opinion.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from permissions import require_patient_access, require_permission
from services.ipass_service import (
    HANDOFF_WINDOW_HOURS,
    build_ipass,
    save_ipass_handoff,
    get_recent_handoffs,
    get_reasoned_assessment,
    prepare_reasoned_assessment,
    render_assessment_text,
)
from services.audit_service import log_audit_event

def _handover_switched_on() -> None:
    """The Orb Ward Record profile switches the handover off (SALES_PLAN
    section 3, step 2: "The handover is off"). Every I-PASS route then answers
    404 in these words, before any patient is read."""
    import config
    if getattr(config, "WARD_RECORD", False):
        raise HTTPException(status_code=404, detail=HANDOVER_SWITCHED_OFF)


#: What every I-PASS route answers under the Orb Ward Record profile.
HANDOVER_SWITCHED_OFF = "The handover is switched off on this computer"

router = APIRouter(prefix="/api/ipass", tags=["ipass"],
                   dependencies=[Depends(_handover_switched_on)])


@router.get("/{patient_id}/draft")
def get_draft(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("vitals.read", audit_action="ipass_draft_viewed")
    ),
):
    """Auto-draft a structured I-PASS handoff from the patient's last ~12h of data."""
    try:
        return build_ipass(patient_id)
    except ValueError:
        # Byte-identical to require_patient_access's scope refusal (which fires
        # first for ward-scoped callers): an unknown id and an out-of-ward id
        # must be indistinguishable — the established posture on the chart,
        # timeline, orders and eMAR (DEMO_READINESS §15.1). str(e) echoed the
        # raw id, giving unscoped callers a different body than scoped ones.
        raise HTTPException(status_code=404, detail="Patient not found")


@router.post("/{patient_id}/save")
def save_draft(
    patient_id: str,
    data: dict,
    request: Request,
    user: dict = Depends(require_patient_access("scribe.create_draft")),
):
    """Persist an I-PASS handoff draft as a scribe note (note_format='handoff')."""
    content = data.get("content")
    if content is None:
        raise HTTPException(status_code=400, detail="content is required")

    try:
        note_id = save_ipass_handoff(patient_id, user["id"], content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    log_audit_event(
        correlation_id=f"ipass-save-{note_id}",
        event_category="scribe",
        action_type="ipass_handoff_saved",
        outcome="success",
        summary_message=f"I-PASS handoff draft {note_id} saved for {patient_id}",
        actor_user_id=user["id"],
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type="scribe_note",
        target_id=str(note_id),
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata={"note_id": note_id, "note_format": "handoff"},
    )

    return {"id": note_id, "status": "draft", "note_format": "handoff"}


@router.get("/{patient_id}/reasoned-assessment")
def reasoned_assessment(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("vitals.read", audit_action="ipass_assessment_viewed")
    ),
):
    """Sage's full reasoned assessment for this patient, or nothing.

    THE WARD ROUND'S READ, and a pure DB read — no model call, so it costs
    milliseconds however slow the appliance is. It only ever RETURNS a draft;
    asking for one is the `prepare` call below, made earlier, so that nobody is
    ever waiting on this endpoint.

    `assessment` is null when there is no recent, readable draft — including
    while one is still being written — and the surface is expected to render
    NOTHING in that case: no heading, no "unavailable" chip, no spinner. Absence
    of a machine opinion is not a fact worth a line on a round, and a spinner
    would be promising an assessment that may be 40 seconds away or may never
    arrive.

    ON THE ACCESS GATE. `relay_routes` guards the alert-time surface with
    case-room MEMBERSHIP, which is right there: the assessment belongs to the
    conversation the alert opened. It would be the WRONG gate here. The oncoming
    nurse and the consultant walking the round are routinely not members of a
    case room opened at 3 a.m., and a handover that hides what the outgoing
    shift was shown is the failure this whole surface exists to prevent. So the
    gate is the one already on the handover: the caller must hold `vitals.read`
    AND the patient must be in their ward scope — the same gate that has already
    disclosed the entire chart this assessment reasons over.
    """
    block = get_reasoned_assessment(patient_id)
    return {
        "patient_id": patient_id,
        "assessment": block,
        # Stated on every response, present or absent, so the client never has
        # to hardcode the window it is being told about.
        "stale_after_hours": HANDOFF_WINDOW_HOURS,
        # The print/copy form, identical to the block the handover sheet
        # renders. "" when there is nothing — never a heading on its own.
        "plain_text": render_assessment_text(block),
    }


@router.post("/{patient_id}/reasoned-assessment/prepare")
async def prepare_reasoned_assessment_route(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("vitals.read", "sage.query",
                               audit_action="ipass_assessment_requested")
    ),
):
    """Ask Sage for the full reasoned assessment EARLY, and return at once.

    Called ahead of the moment it will be read — when the handover list is
    opened before the change-over, or when the round moves on to the next
    patient. The 46 seconds are then spent against something a human is already
    doing, which is the entire reason this tier lives on these surfaces and not
    at a deteriorating bedside.

    NEVER WAITS. The response is `preparing` the instant a generation is
    started; the result is read later through the ordinary GET. There is no
    endpoint anywhere that blocks a clinician on a 46-second model call.

    PER PATIENT ONLY, deliberately. A "prepare the whole ward" call would queue
    thirty serialized 46-second generations behind one tap; pacing that belongs
    to the client, which knows the order it is walking the ward in.

    Needs `sage.query` ON TOP of the handover's own `vitals.read` + ward scope:
    reading a draft Sage already wrote is part of seeing the chart, but spending
    the appliance's GPU to write a new one is using Sage, and the permission
    should say so.
    """
    return await prepare_reasoned_assessment(patient_id)


@router.get("/{patient_id}/recent")
def recent_handoffs(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("vitals.read", audit_action="ipass_handoffs_viewed")
    ),
):
    """List recent I-PASS handoff notes for a patient."""
    return {"handoffs": get_recent_handoffs(patient_id)}
