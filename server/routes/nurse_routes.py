"""Orb — Nurse Mode Routes.

Three endpoints behind the ward-nurse home:

  GET  /api/nurse/board                       — the whole shift as tasks
  POST /api/nurse/patients/{id}/observations  — record a nurse-taken obs set
  POST /api/nurse/patients/{id}/news2-preview — score a set WITHOUT saving it
  POST /api/nurse/observations/{id}/void       marks a set entered in error

Everything else nurse mode does (administering a med, drafting a handoff) goes
through the endpoints that already own those workflows — /api/emar and
/api/ipass — so there is exactly one medication-administration write path in
the system and it keeps its guards. See services/nurse_shift_service.py.

SCOPE, ON ALL THREE. The board derives its ward from the authenticated user via
``permissions.department_scope``; the two per-patient routes hang
``permissions.require_patient_access`` off the path id, which is the same
derivation asked one patient at a time. Nothing in a request — query string,
body, or header — can widen either. The per-patient guards were added
2026-08-07: both routes previously checked only that the ACCOUNT held
`vitals.read`/`vitals.write` and never that the PATIENT was hers.
"""
from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from permissions import (
    VITALS_WRITE,
    department_scope,
    has_permission,
    patient_in_scope_blocking,
    require_patient_access,
    require_permission,
)
from services import nurse_shift_service
from services.audit_service import log_audit_event

router = APIRouter(prefix="/api/nurse", tags=["nurse"])


class ObservationRequest(BaseModel):
    heart_rate: Optional[float] = None
    systolic_bp: Optional[float] = None
    diastolic_bp: Optional[float] = None
    respiratory_rate: Optional[float] = None
    temperature: Optional[float] = None
    spo2: Optional[float] = None
    # ACVPU and air or oxygen. None means the box was left empty, never alert
    # and never on air (Orb Ward Record, 9 October 2026): a paper sheet can
    # leave either box blank, and the rounds file counts that as blank. A nurse
    # at the bedside must choose both (nurse_shift_service refuses her set
    # otherwise); the ward clerk may send None for a box the sheet left empty.
    consciousness: Optional[str] = None
    on_oxygen: Optional[bool] = None
    # Stamped client-side when the nurse hits Save. Doubles as the dedupe key
    # for an offline replay — see nurse_shift_service.record_observation.
    # For the ward clerk it is the time written on the paper sheet.
    taken_at: Optional[str] = None
    # Readings the paper sheet showed but nobody could read (field names from
    # nurse_shift_service.ILLEGIBLE_FIELDS). Each is stored empty and named.
    illegible: List[str] = []


@router.get("/board")
# Plain def on purpose: this is a whole-ward scan across four tables, so it must
# run in FastAPI's threadpool rather than on the asyncio event loop.
def get_board(
    request: Request,
    user: dict = Depends(require_permission("patient.read_all", "vitals.read")),
):
    """The shift board: every admitted patient the nurse covers, task-shaped.

    SCOPE IS NOT NEGOTIABLE FROM THE CLIENT. The ward comes from the
    authenticated user via ``permissions.department_scope`` — the same single
    derivation /api/vigil/patients uses — so nurse mode shows exactly the cohort
    the patient board already shows that user, and cannot be widened by anything
    in the request.

    This route previously accepted ``?department=`` and ``?all_departments=true``.
    Those are gone, and deliberately not replaced by a "float nurse" flag: a
    scope the caller names is not a scope. If a cross-ward view is ever needed
    (a float nurse, a night covering two wards), it belongs in the user's record
    or a new PERMISSION checked here — never in a query parameter. Unknown query
    parameters are ignored by FastAPI, so an old client still sending the flags
    gets its own ward, which is the safe outcome.

    Every successful read is audited as ``phi_access`` BEFORE the board is built:
    this screen returns the clinical state — NEWS2, diagnosis, allergies,
    medications — of every patient on the ward, which is a bulk PHI disclosure
    and the audit middleware only covers writes. ``phi_access`` is a fail-closed
    category, so if the audit row cannot be persisted no board is returned.
    """
    department = department_scope(user)

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="phi_access",          # fail-closed
        action_type="nurse_board_viewed",
        outcome="success",
        summary_message=(
            f"Nurse shift board viewed for department "
            f"{department or '(all, user has no department on record)'}"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type="department",
        target_id=department,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={
            "department": department,
            # True only for a user with no department on record — an admin-set
            # property of the account, never a request parameter. Recorded so a
            # hospital-wide read is visibly hospital-wide in the trail.
            "hospital_wide": department is None,
            "scope_source": "authenticated_user",
        },
    )

    return nurse_shift_service.build_board(department)


@router.post("/patients/{patient_id}/news2-preview")
def preview_news2(
    patient_id: str,
    req: ObservationRequest,
    # WARD-SCOPED EVEN THOUGH IT PERSISTS NOTHING. "Stores nothing" is not the
    # same as "discloses nothing", and this route disclosed two things about a
    # patient the caller cannot open:
    #
    #   1. EXISTENCE. score_observation resolves the patient to read
    #      `on_scale_2`, so a real id answered 200 and an invented one 422
    #      ("patient … not found"). That is a free enumeration oracle over the
    #      whole census — the DISCOVERY half of the 2026-08-05 leak, rebuilt out
    #      of a status code, on a route that only claims to do arithmetic.
    #   2. THE SpO2 SCALE. The response carries `news2.spo2_scale`. Scale 2 is
    #      charted only for a patient in chronic hypercapnic respiratory failure
    #      (target 88-92%), so submitting any probe value against a stranger's id
    #      and reading the scale back discloses a diagnosis.
    #
    # `audit_action` is deliberately None: the tablet calls this on every
    # keystroke, and one encrypted-SQLite write per keystroke is exactly the
    # shape blamed for the 2026-07-30 loop stalls. The SCOPE check is the
    # control here and it still runs on every call — see require_patient_access.
    user: dict = Depends(require_patient_access("vitals.read")),
):
    """Score a candidate observation set without persisting anything.

    Lets the entry screen show the NEWS2 the nurse is about to create as they
    type, from the same scoring code the record path uses.
    """
    # The Orb Ward Record runs with scoring switched off: no score computed or
    # shown, so the preview does not exist on that computer.
    if not nurse_shift_service.scoring_enabled():
        raise HTTPException(status_code=404, detail="Scoring is switched off on this computer")
    try:
        return nurse_shift_service.score_observation(patient_id, req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/patients/{patient_id}/observations")
def record_observations(
    patient_id: str,
    req: ObservationRequest,
    # THE PATIENT IS NOT A SCOPE THE CALLER GETS TO NAME. `vitals.write` alone
    # said "this account may chart observations"; it never said "on THIS
    # patient". Until 2026-08-07 that was the whole check, so a logged-in ward
    # nurse — or a stolen unlocked ward tablet — could chart a full observation
    # set onto ANY admitted patient in the hospital, including patients she is
    # correctly 404'd from READING.
    #
    # Write-without-read is the sharper half. Two harms, both real:
    #   * she modifies a record she cannot see, so she can neither notice nor
    #     undo what she did to it;
    #   * record_observation computes a NEWS2 and dispatches a Vigil escalation
    #     to the patient's Relay case room, so peri-arrest values keyed onto the
    #     wrong id summon a clinical response for a stranger who is fine.
    #
    # This survived the 2026-08-05/06 read sweep because the drift guard that
    # sweep installed (tests/test_patient_scope_routes.py:522) filters on
    # `"GET" not in methods`. The leak is never in the endpoint everyone was
    # looking at. tests/test_nurse_write_scope.py adds the mutation-side guard.
    #
    # require_patient_access, not a hand-rolled check: it is the one derivation
    # (admissions.department, never patients.ward — `ward` is a bed label like
    # "GM-1", `department` is a name like "General Medicine"), it 404s rather
    # than 403s so the route never confirms an out-of-ward patient exists, and
    # it writes the `patient_scope_denied` row that makes a cross-ward probe
    # visible in the trail. A department of None stays UNSCOPED — a governance
    # account keeps writing hospital-wide.
    #
    # ANY ONE of vitals.write or vitals.transcribe (Orb Ward Record, 6 October
    # 2026): the ward clerk copies the paper observation sheet under
    # vitals.transcribe, with exactly the same patient scope, 404 and denial row.
    # Which permission let the caller in decides the stored source below.
    user: dict = Depends(require_patient_access("vitals.write", "vitals.transcribe",
                                                any_of=True)),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    """Record a nurse-taken observation set and the NEWS2 it produces.

    Idempotent on (patient, source, observation time): a replay of the same
    bedside set — offline queue, double-tap, second device — returns the
    existing reading rather than charting a duplicate measurement.
    """
    payload = req.model_dump()
    taken_at = payload.pop("taken_at", None)
    illegible = payload.pop("illegible", None) or []
    # A bedside account (vitals.write) charts as the nurse; an account that
    # only transcribes is the clerk copying the sheet, stored as such.
    source = (nurse_shift_service.OBS_SOURCE
              if has_permission(user.get("role", ""), VITALS_WRITE)
              else nurse_shift_service.CLERK_SOURCE)
    try:
        # At the bedside both boxes are a choice the nurse must make: nothing
        # is preselected, and an empty one is refused in words she can act on.
        # The clerk may leave a box empty when the paper sheet did.
        if source == nurse_shift_service.OBS_SOURCE:
            nurse_shift_service.require_bedside_choices(
                payload, nurse_shift_service.normalise_illegible(illegible))
        return nurse_shift_service.record_observation(
            patient_id,
            user["id"],
            payload,
            idempotency_key=idempotency_key,
            taken_at=taken_at,
            source=source,
            illegible=illegible,
        )
    except ValueError as e:
        # "patient not found" is a 404; everything else is a validation failure.
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=422, detail=str(e))


class VoidRequest(BaseModel):
    # Required: a set is never struck out without saying why.
    reason: str


@router.post("/observations/{vitals_id}/void")
def void_observation(
    vitals_id: int,
    req: VoidRequest,
    request: Request,
    # vitals.write only: a nurse or doctor marks a set entered in error. The
    # ward clerk (vitals.transcribe) keys sets but never strikes one out; a
    # wrong copy goes back to the nurse in charge.
    user: dict = Depends(require_permission("vitals.write")),
):
    """Mark a recorded observation set as entered in error, with a reason.

    PATIENT SCOPE THROUGH THE ROW. The path names a vitals id, not a patient,
    so the patient is read from the row and checked with the same derivation
    require_patient_access uses (permissions.patient_in_scope_blocking). Out
    of scope answers 404 exactly like an id that does not exist, after writing
    the same `patient_scope_denied` row, so the route never confirms that a
    set on another ward exists.
    """
    not_found = HTTPException(status_code=404, detail="Observation not found")
    patient_id = nurse_shift_service.observation_patient(vitals_id)
    if patient_id is None:
        raise not_found
    dept = department_scope(user)
    if not patient_in_scope_blocking(patient_id, dept):
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="patient_scope_denied",
            outcome="denied",
            summary_message=(
                f"Patient {patient_id} is outside the caller's department ({dept})"
            ),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=patient_id,
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={"department": dept, "scope_source": "authenticated_user",
                      "vitals_id": vitals_id},
        )
        raise not_found
    try:
        return nurse_shift_service.void_observation(vitals_id, user["id"], req.reason)
    except nurse_shift_service.ObservationNotFound:
        raise not_found
    except nurse_shift_service.ObservationAlreadyVoided as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
