"""Outpatient consultations: the /api/opd routes.

Created empty in wave 1 of the hospital-system build (3 October 2026) and
registered once in main.py. Later OPD increments (opd-7, opd-10, opd-10a,
opd-11, opd-12, opd-19) add their endpoints here.

BEHIND THE 'opd' FEATURE (ORB_OPD, which also needs ORB_REGISTRATION and the
front desk module; default off). The gate is a router-level dependency, so
every endpoint added here inherits it and answers 404 while the feature is off
(after a 401 for a caller with no session).

Rules for every endpoint added here (design: docs/his_design/opd.json):
  * opd.consult on every route; an OPD prescription needs opd.consult AND
    medication.prescribe;
  * patient-addressed reads use require_patient_access(..., opd_arm=True) or
    enforce_patient_scope(..., opd_arm=True) once the OPD scope arm exists
    (W3-A), and only on the whitelist that test_opd_scope_drift pins;
    visit-addressed routes resolve the visit and refuse writes when the patient
    now has an active admission;
  * OPD never computes or shows NEWS2, a band or AI advice, and never writes
    vitals, risk_scores or medications (test_opd_no_scoring);
  * signing a consultation and writing an OPD prescription need an allergy
    review (ruling OPD-S7, held);
  * plain `def` handlers; patient_id in the path or a typed body; target_id
    always a string in audit rows.

Built: GET /patients/{patient_id}/record (opd-7, W3-A); GET /my-queue
(opd-8, W4-F); GET /patients/{patient_id}/consultations, GET and PUT
/visits/{visit_id}/consultation, POST .../consultation/sign and
.../consultation/addenda, POST /visits/{visit_id}/complete (opd-10, W4-F);
POST /visits/{visit_id}/allergy-review (opd-10a, W5-A).

Planned endpoints: POST /visits/{visit_id}/orders, /prescriptions; GET
/terminology/search.

VISIT-ADDRESSED ROUTES (opd-10) resolve the visit and answer 404 unless its
clinic is the caller's department (an account with no department reads every
clinic); writes are refused 409 once the patient has an active admission.
Saves, signing, addenda and completion are audited INSIDE their transaction
(clinical_record), so the change and its record land together.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

import config
from permissions import (
    OPD_CONSULT,
    department_scope,
    log_phi_read,
    require_patient_access,
    require_permission,
)
from routes.opt_in_gates import require_opd_feature
from services.audit_service import AuditWriteError, log_audit_event
from services.frontdesk_service import FrontDeskError, complete_opd_visit, get_opd_visit
from services.opd_queue_service import (
    COMPLETION_DISPOSITIONS,
    list_my_queue,
    nudge_queue_changed,
)
from services.opd_service import (
    ConsultationError,
    add_addendum,
    consultation_signed,
    get_consultation,
    get_opd_record,
    list_patient_consultations,
    record_allergy_review,
    save_consultation,
    sign_consultation,
)

router = APIRouter(
    prefix="/api/opd",
    tags=["Outpatients"],
    dependencies=[Depends(require_opd_feature)],
)


@router.get("/meta")
def opd_meta(user: dict = Depends(require_permission(OPD_CONSULT))):
    """What this site's outpatient settings are. No patient data."""
    return {
        "feature": "opd",
        "enabled": config.feature_enabled("opd"),
        "settings": {
            "scope_days": config.OPD_SCOPE_DAYS,
            "charge_at": config.OPD_CHARGE_AT,
            "queue_poll_seconds": config.OPD_QUEUE_POLL_SECONDS,
        },
        "dispositions": [{"code": c, "label": lab} for c, lab in COMPLETION_DISPOSITIONS],
    }


@router.get("/patients/{patient_id}/record")
def opd_record(
    patient_id: str,
    request: Request,
    # The OPD scope arm (opd-7). The ordinary scope, OR (the 'opd' feature on,
    # no active admission anywhere, and a non-cancelled visit in the caller's
    # clinic within ORB_OPD_SCOPE_DAYS). On the whitelist in
    # tests/test_opd_scope_drift.py. Every read writes a fail-closed phi_access
    # row that names the arm (admission, opd_visit or hospital_wide).
    user: dict = Depends(
        require_patient_access(OPD_CONSULT, opd_arm=True, audit_action="opd_record_viewed")
    ),
):
    """The outpatient record view: identity, allergies, problems, OPD-linked
    results and signed OPD consultations of every clinic (ruling OPD-N2,
    extent ii). Never the inpatient chart, vitals, a score or AI advice."""
    record = get_opd_record(
        patient_id,
        viewer_department=department_scope(user),
        access=getattr(request.state, "orb_patient_scope", None),
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return record


# ═════════════════════════════════════════════════════════════════════════════
#  The doctor's queue (opd-8)
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/my-queue")
def my_queue(
    request: Request,
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """Today's queue for the signed-in doctor: the patient in the room, the
    waiting line in seeing order (priority, then token), the skipped, and the
    clinic's unassigned patients to take. The clinic is the caller's
    department, never a parameter. A non-empty read is audited (phi_access).
    No vitals, score or band."""
    dept = department_scope(user)
    result = list_my_queue(doctor_id=user.get("id") or "", department=dept)
    if result["total"]:
        log_phi_read(
            request=request,
            user=user,
            action_type="opd_my_queue_viewed",
            summary_message=(
                f"OPD queue of {user.get('id')} viewed ({result['total']} visits) for "
                f"{dept or '(all clinics, user has no department on record)'} "
                f"on {result['visit_date']}"
            ),
            metadata={"result_count": result["total"], "visit_date": result["visit_date"],
                      "visit_ids": [v["id"] for key in ("current", "waiting", "skipped",
                                                        "unassigned") for v in result[key]]},
        )
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  The consultation record (opd-10)
# ═════════════════════════════════════════════════════════════════════════════

class ConsultationIn(BaseModel):
    """PUT body. Only the fields sent change; row_version is the version the
    editor read (None or 0 for a new note)."""
    row_version: int | None = None
    chief_complaint: str | None = None
    history: str | None = None
    examination: str | None = None
    vitals: dict | None = None
    diagnoses: list | None = None
    plan: str | None = None
    advice: str | None = None
    follow_up_date: str | None = None
    follow_up_note: str | None = None
    referral_department: str | None = None
    admission_advised: bool | None = None


class SignIn(BaseModel):
    row_version: int | None = None


class AddendumIn(BaseModel):
    body: str


class AllergyReviewIn(BaseModel):
    """POST body of an allergy review (opd-10a). A field this route does not
    know is refused (422), so nobody believes it was saved."""
    model_config = ConfigDict(extra="forbid")
    # 'allergies_recorded', 'none_known' or 'unable_to_obtain'.
    status: str = Field(..., max_length=40)
    substances: list[str] | None = Field(None, max_length=30)
    reason: str | None = Field(None, max_length=500)
    note: str | None = Field(None, max_length=1000)


class OpdCompleteIn(BaseModel):
    disposition: str
    # Ruling OPD-N8: a visit may be completed without a signed note only with
    # a reason (for example, the note is on paper).
    no_signed_note_reason: str | None = None
    service_code: str | None = None


def _visit_in_clinic(visit_id: str, dept: str | None) -> dict:
    """The visit, or 404 when there is none or it belongs to another clinic.
    404 rather than 403: a visit id implies a patient attended a named clinic.
    `dept` is permissions.department_scope(user), derived by the endpoint so
    the derivation shows in the route (tests/test_nurse_write_scope.py)."""
    visit = get_opd_visit(visit_id)
    if visit is None:
        raise HTTPException(status_code=404, detail="OPD visit not found")
    if dept is not None and (visit.get("department") or "") != dept:
        raise HTTPException(status_code=404, detail="OPD visit not found")
    return visit


def _consultation_http_error(err: ConsultationError) -> HTTPException:
    return HTTPException(status_code=err.status,
                         detail={"error": err.code, "message": str(err), **err.extra})


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"error": "audit_unavailable",
                "message": ("This could not be recorded because the audit trail is not "
                            "available. Nothing was saved. Try again in a moment.")},
    )


def _in_txn_audit(request: Request, user: dict, *, action_type: str, summary, metadata):
    """A callback writing one clinical_record row inside the caller's
    transaction (opd_service and frontdesk_service run it before commit)."""

    def _write(db, record: dict, facts: dict | None = None) -> None:
        log_audit_event(
            correlation_id=f"{action_type}-{record.get('visit_id') or record.get('id')}",
            event_category="clinical_record",
            action_type=action_type,
            outcome="success",
            db=db,
            summary_message=summary(record, facts or {}),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=record.get("patient_id"),
            target_type="opd_consultation" if record.get("visit_id") else "opd_visit",
            target_id=str(record.get("id")),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            metadata={**metadata(record, facts or {}), "written": "in_transaction"},
        )

    return _write


@router.get("/patients/{patient_id}/consultations")
def patient_consultations(
    patient_id: str,
    user: dict = Depends(
        require_patient_access(OPD_CONSULT, opd_arm=True,
                               audit_action="opd_consultations_viewed")
    ),
):
    """The patient's OPD notes in full: every clinic's signed notes (ruling
    OPD-N2 extent ii) and the caller's own clinic's drafts, each with its
    addenda. OPD vitals are shown as typed, with their time; never scored."""
    return list_patient_consultations(patient_id, viewer_department=department_scope(user))


@router.get("/visits/{visit_id}/consultation")
def read_consultation(
    visit_id: str,
    request: Request,
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """This visit's note (or null before it is started), its addenda, and
    whether it may be signed yet (the allergy review, ruling OPD-S7). The
    review is carried as status, label and time only (opd_service.
    visit_review_summary): a visit id of any date reaches this read, so it
    names no allergen, note, reason, clinic or clinician."""
    visit = _visit_in_clinic(visit_id, department_scope(user))
    try:
        result = get_consultation(visit_id)
    except ConsultationError as err:
        raise _consultation_http_error(err)
    log_phi_read(
        request=request, user=user, action_type="opd_consultation_viewed",
        summary_message=f"OPD consultation for visit {visit_id} viewed",
        patient_id=visit.get("patient_id"), target_type="opd_visit", target_id=visit_id,
    )
    return result


@router.put("/visits/{visit_id}/consultation")
def write_consultation(
    visit_id: str,
    data: ConsultationIn,
    request: Request,
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """Start or update the draft note. Refused once signed (409), when the
    version read is stale (409 with current_version), when the patient is
    now admitted (409). A diagnosis with no code saves; an unknown code saves
    as unvalidated."""
    _visit_in_clinic(visit_id, department_scope(user))
    fields = {k: getattr(data, k) for k in data.model_fields_set if k != "row_version"}
    try:
        return save_consultation(
            visit_id, fields, row_version=data.row_version, actor_id=user.get("id") or "",
            on_done=_in_txn_audit(
                request, user, action_type="opd_consultation_saved",
                summary=lambda n, f: (f"OPD consultation {n['id']} "
                                      f"{'started' if f.get('created') else 'saved'} "
                                      f"(version {n['row_version']})"),
                metadata=lambda n, f: {"visit_id": n["visit_id"], "row_version": n["row_version"],
                                       "fields": f.get("fields"), "created": f.get("created")},
            ),
        )
    except ConsultationError as err:
        raise _consultation_http_error(err)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/visits/{visit_id}/consultation/sign")
def sign_visit_consultation(
    visit_id: str,
    data: SignIn,
    request: Request,
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """Sign the draft; it never changes afterwards. Needs an allergy review
    (ruling OPD-S7): 409 allergy_review_required until one exists."""
    _visit_in_clinic(visit_id, department_scope(user))
    try:
        return sign_consultation(
            visit_id, row_version=data.row_version, actor_id=user.get("id") or "",
            on_done=_in_txn_audit(
                request, user, action_type="opd_consultation_signed",
                summary=lambda n, f: f"OPD consultation {n['id']} signed",
                metadata=lambda n, f: {"visit_id": n["visit_id"], **f},
            ),
        )
    except ConsultationError as err:
        raise _consultation_http_error(err)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/visits/{visit_id}/consultation/addenda")
def add_consultation_addendum(
    visit_id: str,
    data: AddendumIn,
    request: Request,
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """Append an addendum to the signed note. Addenda are never edited."""
    _visit_in_clinic(visit_id, department_scope(user))
    try:
        return add_addendum(
            visit_id, data.body, actor_id=user.get("id") or "",
            on_done=_in_txn_audit(
                request, user, action_type="opd_consultation_addendum_added",
                summary=lambda n, f: f"Addendum added to OPD consultation {n['id']}",
                metadata=lambda n, f: {"visit_id": n["visit_id"], **f},
            ),
        )
    except ConsultationError as err:
        raise _consultation_http_error(err)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/visits/{visit_id}/allergy-review")
def record_visit_allergy_review(
    visit_id: str,
    data: AllergyReviewIn,
    request: Request,
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """The consulting doctor's allergy review (opd-10a, ruling OPD-S7): the
    allergies the patient has (charted now, the same writer the admission
    lane uses), no known allergies, or why they could not be asked. Written
    with its audit row in one transaction. Visit-scoped (404 outside the
    caller's clinic); refused once the patient is admitted (409) and on a
    visit outside the OPD scope window (409 visit_not_current, before the
    chart is read), so it is exactly as wide as the OPD arm and an old visit
    id reads nothing. The answer carries the review as status, label and
    time only, never the allergy list: that is read from the record view.
    A review never changes how the allergy interlocks screen."""
    _visit_in_clinic(visit_id, department_scope(user))

    def _audit(db, result: dict, facts: dict) -> None:
        charted, again = facts.get("charted") or [], facts.get("reactivated") or []
        log_audit_event(
            correlation_id=f"opd-allergy-review-{visit_id}",
            event_category="clinical_record",
            action_type="opd_allergy_review_recorded",
            outcome="success",
            db=db,
            summary_message=(
                f"Allergy review on OPD visit {visit_id}: {facts.get('status')}"
                + (f"; charted {', '.join(charted)}" if charted else "")
                + (f"; made active again {', '.join(again)}" if again else "")),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=result.get("patient_id"),
            target_type="patient_allergy_assessment",
            target_id=str(facts.get("assessment_id")),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            metadata={"visit_id": visit_id, **facts, "written": "in_transaction"},
        )

    try:
        return record_allergy_review(
            visit_id,
            status=data.status,
            substances=data.substances,
            reason=data.reason,
            note=data.note,
            actor_id=user.get("id") or "",
            on_done=_audit,
        )
    except ConsultationError as err:
        raise _consultation_http_error(err)
    except AuditWriteError:
        raise _audit_unavailable()


@router.post("/visits/{visit_id}/complete")
def complete_with_disposition(
    visit_id: str,
    data: OpdCompleteIn,
    request: Request,
    user: dict = Depends(require_permission(OPD_CONSULT)),
):
    """Finish the visit with a disposition and raise the consultation charge
    (the front desk's charge logic, unchanged: one charge per visit, unpriced
    rather than free when the site has not set a rate).

    Ruling OPD-N8: a signed note, or a reason (for example, the note is on
    paper). 'admitted' is not a disposition here: converting a patient to an
    inpatient is its own step (opd-17a).
    """
    _visit_in_clinic(visit_id, department_scope(user))
    disposition = (data.disposition or "").strip().lower()
    if disposition not in dict(COMPLETION_DISPOSITIONS):
        raise HTTPException(status_code=422, detail={
            "error": "bad_disposition",
            "message": "Choose how the visit ended: "
                       + ", ".join(lab.lower() for _, lab in COMPLETION_DISPOSITIONS),
        })
    reason = (data.no_signed_note_reason or "").strip()[:500] or None
    facts: dict = {}

    def _precheck(db, row) -> None:
        if db.execute("SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' "
                      "LIMIT 1", (row["patient_id"],)).fetchone() is not None:
            raise ConsultationError(
                "patient_admitted",
                "This patient is now admitted. Finish the visit from the inpatient chart.",
                status=409)
        signed = consultation_signed(db, visit_id)
        if not signed and not reason:
            raise ConsultationError(
                "signed_note_required",
                "Sign the consultation note first, or say why there is none "
                "(for example, the note is on paper).", status=409)
        facts.update(signed_note=signed, no_signed_note_reason=None if signed else reason)

    def _audit(db, visit: dict, svc_facts: dict) -> None:
        charge = visit.get("consultation_charge") or {}
        log_audit_event(
            correlation_id=f"opd-complete-{visit['id']}",
            event_category="clinical_record",
            action_type="opd_visit_completed",
            outcome="success",
            db=db,
            summary_message=(f"OPD visit {visit['id']} completed ({visit.get('disposition')}); "
                             f"consultation charge {charge.get('charge_id') or 'not raised'}"),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=visit.get("patient_id"),
            target_type="opd_visit",
            target_id=str(visit["id"]),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            metadata={
                "disposition": visit.get("disposition"),
                **facts,
                "charge_id": charge.get("charge_id"),
                "rate_is_configured": charge.get("rate_is_configured"),
                "total_paise": charge.get("total_paise"),
                **svc_facts,
                "written": "in_transaction",
            },
        )

    try:
        visit = complete_opd_visit(
            visit_id,
            completed_by=user.get("id"),
            service_code=data.service_code,
            raise_charge=True,
            disposition=disposition,
            before_complete=_precheck,
            on_done=_audit,
        )
    except ConsultationError as err:
        raise _consultation_http_error(err)
    except FrontDeskError as err:
        raise HTTPException(status_code=422, detail=str(err))
    except AuditWriteError:
        raise _audit_unavailable()
    nudge_queue_changed(visit.get("department"))
    return visit
