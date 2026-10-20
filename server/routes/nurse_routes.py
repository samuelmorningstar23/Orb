"""Orb — Nurse Mode Routes.

Three endpoints behind the ward-nurse home:

  GET  /api/nurse/board                       — the whole shift as tasks
  POST /api/nurse/patients/{id}/observations  — record a nurse-taken obs set
  POST /api/nurse/patients/{id}/news2-preview — score a set WITHOUT saving it
  POST /api/nurse/observations/{id}/void       marks a set entered in error
  GET  /api/nurse/transcribe/patients          the ward clerk's patient list
  GET  /api/nurse/transcribe/patients/{id}/my-copies  her own copies, 24 hours

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

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from permissions import (
    VITALS_WRITE,
    department_scope,
    facility_scope,
    has_permission,
    patient_in_scope_blocking,
    require_any_permission,
    require_patient_access,
    require_permission,
)
from pydantic import BaseModel
from services.audit_service import log_audit_event

from services import facility_service, nurse_shift_service

router = APIRouter(prefix="/api/nurse", tags=["nurse"])


def _with_facility_scope(metadata: dict, facilities: frozenset[int] | None) -> dict:
    """The board audit rows' metadata plus the reader's facility scope, the
    key permissions.log_phi_read adds: only while more than one facility is on
    record (a list, or None for a group-wide account), so with one facility
    the row is exactly as before (multisite-8)."""
    if facility_service.multi_facility_data():
        metadata["facility_scope"] = None if facilities is None else sorted(facilities)
    return metadata


class ObservationRequest(BaseModel):
    heart_rate: float | None = None
    systolic_bp: float | None = None
    diastolic_bp: float | None = None
    respiratory_rate: float | None = None
    temperature: float | None = None
    spo2: float | None = None
    # ACVPU and air or oxygen. None means the box was left empty, never alert
    # and never on air (Orb Ward Record, 9 October 2026): a paper sheet can
    # leave either box blank, and the rounds file counts that as blank. A nurse
    # at the bedside must choose both (nurse_shift_service refuses her set
    # otherwise); the ward clerk may send None for a box the sheet left empty.
    consciousness: str | None = None
    on_oxygen: bool | None = None
    # Stamped client-side when the nurse hits Save. Doubles as the dedupe key
    # for an offline replay — see nurse_shift_service.record_observation.
    # For the ward clerk it is the time written on the paper sheet.
    taken_at: str | None = None
    # Readings the paper sheet showed but nobody could read (field names from
    # nurse_shift_service.ILLEGIBLE_FIELDS). Each is stored empty and named.
    illegible: list[str] = []


_GET_BOARD_USER_DEFAULT = Depends(require_permission("patient.read_all", "vitals.read"))


@router.get("/board")
# Plain def on purpose: this is a whole-ward scan across four tables, so it must
# run in FastAPI's threadpool rather than on the asyncio event loop.
def get_board(
    request: Request,
    user: dict = _GET_BOARD_USER_DEFAULT,
) -> dict:
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
    # Facility scope (multisite-8): with more than one facility on record the
    # board keeps to the caller's facilities; None (one facility) changes
    # nothing.
    facilities = facility_scope(user)

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="phi_access",  # fail-closed
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
        metadata=_with_facility_scope(
            {
                "department": department,
                # True only for a user with no department on record: an
                # admin-set property of the account, never a request parameter.
                # Recorded so a hospital-wide read is visibly hospital-wide.
                "hospital_wide": department is None,
                "scope_source": "authenticated_user",
            },
            facilities,
        ),
    )

    return nurse_shift_service.build_board(department, facilities=facilities)


_LIST_TRANSCRIBE_PATIENTS_USER_DEFAULT = Depends(require_permission("vitals.transcribe"))


@router.get("/transcribe/patients")
# Plain def: a database read, so it runs in the threadpool, off the event loop.
def list_transcribe_patients(
    request: Request,
    # vitals.transcribe only: the ward clerk (Orb Ward Record, 9 October 2026).
    # Her Observations screen used to read the bed board, which needs the beds
    # module (ORB_ADT) switched on; this list works without it.
    user: dict = _LIST_TRANSCRIBE_PATIENTS_USER_DEFAULT,
) -> dict:
    """The admitted patients of the clerk's own department: name, hospital
    number, ward, bed and when the stay began. Nothing clinical.

    SCOPE AS ON THE BOARD: the department comes from the authenticated user
    (permissions.department_scope) and nothing in the request can widen it.
    Audited as ``phi_access`` (fail-closed) before the list is built, as the
    board is: it names every patient on the ward.
    """
    department = department_scope(user)
    facilities = facility_scope(user)
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="phi_access",  # fail-closed
        action_type="transcribe_patients_viewed",
        outcome="success",
        summary_message=(
            f"Ward clerk patient list viewed for department "
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
        metadata=_with_facility_scope(
            {
                "department": department,
                "hospital_wide": department is None,
                "scope_source": "authenticated_user",
            },
            facilities,
        ),
    )
    return {
        "department": department,
        "patients": nurse_shift_service.transcribe_patients(department, facilities=facilities),
    }


_LIST_OWN_COPIES_USER_DEFAULT = Depends(
    require_patient_access("vitals.transcribe", audit_action="transcribe_own_copies_viewed")
)


@router.get("/transcribe/patients/{patient_id}/my-copies")
# Plain def: a database read, so it runs in the threadpool, off the event loop.
def list_own_copies(
    patient_id: str,
    # vitals.transcribe, on a patient of her own department (the same scope,
    # 404 and denial row as every per-patient route), audited as a read.
    user: dict = _LIST_OWN_COPIES_USER_DEFAULT,
):
    """The sets this ward clerk copied for this patient in the last 24 hours:
    the time on the sheet, the readings and whether each is struck out, so she
    can mark her own wrong copy entered in error. Nothing else, and never a
    set someone else keyed."""
    return {
        "patient_id": patient_id,
        "copies": nurse_shift_service.clerk_own_copies(patient_id, user["id"]),
    }


_PREVIEW_NEWS2_USER_DEFAULT = Depends(require_patient_access("vitals.read"))


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
    user: dict = _PREVIEW_NEWS2_USER_DEFAULT,
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
        raise HTTPException(status_code=422, detail=str(e)) from e


_RECORD_OBSERVATIONS_USER_DEFAULT = Depends(
    require_patient_access("vitals.write", "vitals.transcribe", any_of=True)
)
_RECORD_OBSERVATIONS_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")


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
    user: dict = _RECORD_OBSERVATIONS_USER_DEFAULT,
    idempotency_key: str | None = _RECORD_OBSERVATIONS_IDEMPOTENCY_KEY_DEFAULT,
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
    source = (
        nurse_shift_service.OBS_SOURCE
        if has_permission(user.get("role", ""), VITALS_WRITE)
        else nurse_shift_service.CLERK_SOURCE
    )
    try:
        # At the bedside both boxes are a choice the nurse must make: nothing
        # is preselected, and an empty one is refused in words she can act on.
        # The clerk may leave a box empty when the paper sheet did.
        if source == nurse_shift_service.OBS_SOURCE:
            nurse_shift_service.require_bedside_choices(
                payload, nurse_shift_service.normalise_illegible(illegible)
            )
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
            raise HTTPException(status_code=404, detail=str(e)) from e
        raise HTTPException(status_code=422, detail=str(e)) from e


class VoidRequest(BaseModel):
    # Required: a set is never struck out without saying why.
    reason: str


_VOID_OBSERVATION_USER_DEFAULT = Depends(
    require_any_permission("vitals.write", "vitals.transcribe")
)


@router.post("/observations/{vitals_id}/void")
def void_observation(
    vitals_id: int,
    req: VoidRequest,
    request: Request,
    # vitals.write: a nurse or doctor marks any set in scope entered in error.
    # vitals.transcribe alone (the ward clerk, Orb Ward Record): she strikes
    # out her OWN copies only, a set of source clerk_transcribed that her
    # account keyed (nurse_shift_service.void_observation own_copy_only), so a
    # wrong copy never needs a nurse to correct it. Anything else is a 403.
    user: dict = _VOID_OBSERVATION_USER_DEFAULT,
) -> dict:
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
    # Facility first (multisite-8), as require_patient_access checks it: with
    # one facility on record facilities is None and this reads nothing.
    facilities = facility_scope(user)
    outside_facility = not patient_in_scope_blocking(patient_id, None, facilities=facilities)
    if outside_facility or not patient_in_scope_blocking(patient_id, dept):
        metadata: dict = {
            "department": dept,
            "scope_source": "authenticated_user",
            "vitals_id": vitals_id,
        }
        summary = f"Patient {patient_id} is outside the caller's department ({dept})"
        if outside_facility and facilities is not None:
            metadata.update(scope_cause="facility", facility_scope=sorted(facilities))
            summary = f"Patient {patient_id} is outside the caller's facility scope"
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="patient_scope_denied",
            outcome="denied",
            summary_message=summary,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=patient_id,
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata=metadata,
        )
        raise not_found
    own_copy_only = not has_permission(user.get("role", ""), VITALS_WRITE)
    try:
        return nurse_shift_service.void_observation(
            vitals_id, user["id"], req.reason, own_copy_only=own_copy_only
        )
    except nurse_shift_service.ObservationNotFound:
        raise not_found from None
    except nurse_shift_service.ObservationNotYours as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except nurse_shift_service.ObservationAlreadyVoided as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
