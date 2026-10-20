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
POST /visits/{visit_id}/allergy-review (opd-10a, W5-A); GET
/terminology/search, GET and POST /visits/{visit_id}/orders, POST
/visits/{visit_id}/orders/{order_id}/cancel, GET /visits/{visit_id}/print
(opd-11, W6-A); GET and POST /visits/{visit_id}/prescriptions, POST
/visits/{visit_id}/prescriptions/{order_id}/cancel and GET
/visits/{visit_id}/prescriptions/print (opd-12, W8-C).

PRESCRIPTIONS (opd-12) are medication orders in the shared store
(services/medorders/outpatient.py: encounter_type 'op', no ward row, no Helix
row). Each prescription route also needs ORB_MED_ORDER_STORE (404
feature_disabled while it is off) and answers 404 on an Orb Ward Record
computer, whose drug chart stays on paper (services/drug_chart). Writing or
cancelling a line needs opd.consult AND medication.prescribe; reading and
printing need opd.consult. A line is refused until the allergy review lets the
note be signed (ruling OPD-S7 (a)).

INVESTIGATIONS (opd-11) are orders tied to the visit (orders.opd_visit_id)
through orders_service.create_order, never through /api/orders, and order sets
are never offered for an outpatient. Laboratory tests and imaging only, the
code checked by terminology_service.describe_investigation and the order's
words taken from the code list, not the client. The visit must be the
caller's clinic's, current (inside the OPD scope window), neither cancelled
nor finished, and the patient not admitted; the last three are re-read inside
the order's own transaction, which also writes the audit row.

PRINTS (opd-11) carry the patient's name, UHID, age and sex, what the record
holds and nothing written for the print, and the doctor's name and
users.registration_number only. A consultation prints only once signed.

VISIT-ADDRESSED ROUTES (opd-10) resolve the visit and answer 404 unless its
clinic is the caller's department (an account with no department reads every
clinic of its own facility once more than one facility is on record,
multisite-8h1); writes are refused 409 once the patient has an active admission.
Saves, signing, addenda and completion are audited INSIDE their transaction
(clinical_record), so the change and its record land together.
"""

from typing import Literal

import config
from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from permissions import (
    MEDICATION_PRESCRIBE,
    OPD_CONSULT,
    department_scope,
    log_phi_read,
    opd_scope_window,
    require_patient_access,
    require_permission,
)
from pydantic import BaseModel, ConfigDict, Field
from services.audit_service import AuditWriteError, log_audit_event
from services.clinical_time import utc_iso_now
from services.drug_chart import require_drug_chart
from services.frontdesk_service import FrontDeskError, complete_opd_visit, get_opd_visit
from services.medorders import core as medorder_core
from services.medorders import outpatient
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
from services.orders_service import OpdVisitOrderRefused, OrderManagedByLab

from routes.opt_in_gates import feature_gate, require_opd_feature
from services import facility_lanes, orders_service, registration_service, terminology_service

router = APIRouter(
    prefix="/api/opd",
    tags=["Outpatients"],
    dependencies=[Depends(require_opd_feature)],
)


_OPD_META_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.get("/meta")
def opd_meta(user: dict = _OPD_META_USER_DEFAULT):
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


_OPD_RECORD_USER_DEFAULT = Depends(
    require_patient_access(OPD_CONSULT, opd_arm=True, audit_action="opd_record_viewed")
)


@router.get("/patients/{patient_id}/record")
def opd_record(
    patient_id: str,
    request: Request,
    # The OPD scope arm (opd-7). The ordinary scope, OR (the 'opd' feature on,
    # no active admission anywhere, and a non-cancelled visit in the caller's
    # clinic within ORB_OPD_SCOPE_DAYS). On the whitelist in
    # tests/test_opd_scope_drift.py. Every read writes a fail-closed phi_access
    # row that names the arm (admission, opd_visit or hospital_wide).
    user: dict = _OPD_RECORD_USER_DEFAULT,
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


_MY_QUEUE_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.get("/my-queue", response_model=None)
def my_queue(
    request: Request,
    user: dict = _MY_QUEUE_USER_DEFAULT,
) -> dict:
    """Today's queue for the signed-in doctor: the patient in the room, the
    waiting line in seeing order (priority, then token), the skipped, and the
    clinic's unassigned patients to take. The clinic is the caller's
    department, never a parameter. A non-empty read is audited (phi_access).
    No vitals, score or band. With more than one facility on record the
    unassigned line is the caller's own facility's clinics only
    (multisite-8h1)."""
    dept = department_scope(user)
    result = list_my_queue(
        doctor_id=user.get("id") or "",
        department=dept,
        facilities=facility_lanes.scope_of(user),
    )
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
            metadata={
                "result_count": result["total"],
                "visit_date": result["visit_date"],
                "visit_ids": [
                    v["id"]
                    for key in ("current", "waiting", "skipped", "unassigned")
                    for v in result[key]
                ],
            },
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


def _visit_in_clinic(visit_id: str, dept: str | None, *, user: dict, request: Request) -> dict:
    """The visit, or 404 when there is none or it belongs to another clinic.
    404 rather than 403: a visit id implies a patient attended a named clinic.
    `dept` is permissions.department_scope(user), derived by the endpoint so
    the derivation shows in the route (tests/test_nurse_write_scope.py).

    With more than one facility on record (multisite-8h1), a visit at a
    facility outside the caller's scope is 404 too, and the attempt writes an
    authorization patient_scope_denied row (scope_cause 'facility'): an
    account with no department no longer reads every clinic of the group,
    only its own facility's. With one facility nothing more is read."""
    visit = get_opd_visit(visit_id)
    if visit is None:
        raise HTTPException(status_code=404, detail="OPD visit not found")
    if dept is not None and (visit.get("department") or "") != dept:
        raise HTTPException(status_code=404, detail="OPD visit not found")
    facilities = facility_lanes.scope_of(user)
    if facilities is not None:
        with db_session() as db:
            at = facility_lanes.visit_facility_on(db, visit_id)
        if facility_lanes.outside(at, facilities):
            facility_lanes.audit_denial(
                user=user,
                patient_id=visit.get("patient_id"),
                dept=dept,
                facilities=facilities,
                facility_id=at,
                helper="opd_visit_in_clinic",
                request_meta=_request_meta(request),
            )
            raise HTTPException(status_code=404, detail="OPD visit not found")
    return visit


def _consultation_http_error(err: ConsultationError) -> HTTPException:
    return HTTPException(
        status_code=err.status, detail={"error": err.code, "message": str(err), **err.extra}
    )


def _audit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": (
                "This could not be recorded because the audit trail is not "
                "available. Nothing was saved. Try again in a moment."
            ),
        },
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


_PATIENT_CONSULTATIONS_USER_DEFAULT = Depends(
    require_patient_access(OPD_CONSULT, opd_arm=True, audit_action="opd_consultations_viewed")
)


@router.get("/patients/{patient_id}/consultations")
def patient_consultations(
    patient_id: str,
    user: dict = _PATIENT_CONSULTATIONS_USER_DEFAULT,
):
    """The patient's OPD notes in full: every clinic's signed notes (ruling
    OPD-N2 extent ii) and the caller's own clinic's drafts, each with its
    addenda. OPD vitals are shown as typed, with their time; never scored."""
    return list_patient_consultations(patient_id, viewer_department=department_scope(user))


_READ_CONSULTATION_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.get("/visits/{visit_id}/consultation", response_model=None)
def read_consultation(
    visit_id: str,
    request: Request,
    user: dict = _READ_CONSULTATION_USER_DEFAULT,
) -> dict:
    """This visit's note (or null before it is started), its addenda, and
    whether it may be signed yet (the allergy review, ruling OPD-S7). The
    review is carried as status, label and time only (opd_service.
    visit_review_summary): a visit id of any date reaches this read, so it
    names no allergen, note, reason, clinic or clinician."""
    visit = _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    try:
        result = get_consultation(visit_id)
    except ConsultationError as err:
        raise _consultation_http_error(err) from err
    log_phi_read(
        request=request,
        user=user,
        action_type="opd_consultation_viewed",
        summary_message=f"OPD consultation for visit {visit_id} viewed",
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=visit_id,
    )
    return result


_WRITE_CONSULTATION_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.put("/visits/{visit_id}/consultation", response_model=None)
def write_consultation(
    visit_id: str,
    data: ConsultationIn,
    request: Request,
    user: dict = _WRITE_CONSULTATION_USER_DEFAULT,
) -> dict:
    """Start or update the draft note. Refused once signed (409), when the
    version read is stale (409 with current_version), when the patient is
    now admitted (409). A diagnosis with no code saves; an unknown code saves
    as unvalidated."""
    _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    fields = {k: getattr(data, k) for k in data.model_fields_set if k != "row_version"}
    try:
        return save_consultation(
            visit_id,
            fields,
            row_version=data.row_version,
            actor_id=user.get("id") or "",
            on_done=_in_txn_audit(
                request,
                user,
                action_type="opd_consultation_saved",
                summary=lambda n, f: (
                    f"OPD consultation {n['id']} "
                    f"{'started' if f.get('created') else 'saved'} "
                    f"(version {n['row_version']})"
                ),
                metadata=lambda n, f: {
                    "visit_id": n["visit_id"],
                    "row_version": n["row_version"],
                    "fields": f.get("fields"),
                    "created": f.get("created"),
                },
            ),
        )
    except ConsultationError as err:
        raise _consultation_http_error(err) from err
    except AuditWriteError:
        raise _audit_unavailable() from None


_SIGN_VISIT_CONSULTATION_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.post("/visits/{visit_id}/consultation/sign", response_model=None)
def sign_visit_consultation(
    visit_id: str,
    data: SignIn,
    request: Request,
    user: dict = _SIGN_VISIT_CONSULTATION_USER_DEFAULT,
) -> dict:
    """Sign the draft; it never changes afterwards. Needs an allergy review
    (ruling OPD-S7): 409 allergy_review_required until one exists."""
    _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    try:
        return sign_consultation(
            visit_id,
            row_version=data.row_version,
            actor_id=user.get("id") or "",
            on_done=_in_txn_audit(
                request,
                user,
                action_type="opd_consultation_signed",
                summary=lambda n, f: f"OPD consultation {n['id']} signed",
                metadata=lambda n, f: {"visit_id": n["visit_id"], **f},
            ),
        )
    except ConsultationError as err:
        raise _consultation_http_error(err) from err
    except AuditWriteError:
        raise _audit_unavailable() from None


_ADD_CONSULTATION_ADDENDUM_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.post("/visits/{visit_id}/consultation/addenda", response_model=None)
def add_consultation_addendum(
    visit_id: str,
    data: AddendumIn,
    request: Request,
    user: dict = _ADD_CONSULTATION_ADDENDUM_USER_DEFAULT,
) -> dict:
    """Append an addendum to the signed note. Addenda are never edited."""
    _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    try:
        return add_addendum(
            visit_id,
            data.body,
            actor_id=user.get("id") or "",
            on_done=_in_txn_audit(
                request,
                user,
                action_type="opd_consultation_addendum_added",
                summary=lambda n, f: f"Addendum added to OPD consultation {n['id']}",
                metadata=lambda n, f: {"visit_id": n["visit_id"], **f},
            ),
        )
    except ConsultationError as err:
        raise _consultation_http_error(err) from err
    except AuditWriteError:
        raise _audit_unavailable() from None


_RECORD_VISIT_ALLERGY_REVIEW_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.post("/visits/{visit_id}/allergy-review", response_model=None)
def record_visit_allergy_review(
    visit_id: str,
    data: AllergyReviewIn,
    request: Request,
    user: dict = _RECORD_VISIT_ALLERGY_REVIEW_USER_DEFAULT,
) -> dict:
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
    _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)

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
                + (f"; made active again {', '.join(again)}" if again else "")
            ),
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
        raise _consultation_http_error(err) from err
    except AuditWriteError:
        raise _audit_unavailable() from None


# ═════════════════════════════════════════════════════════════════════════════
#  The code lists, investigations and prints (opd-11, W6-A)
# ═════════════════════════════════════════════════════════════════════════════

#: What an outpatient order may be refused for, in the clinic's words.
_INVESTIGATION_REFUSALS = {
    "unknown_code": (422, "That code is not in the code list. Choose a test from the search."),
    "not_an_investigation": (
        422,
        "Only laboratory tests and imaging can be ordered for an outpatient here.",
    ),
    "bedside_test": (422, "That is a bedside test, done on a ward, not ordered from a clinic."),
    "not_in_lab_catalogue": (
        422,
        "The laboratory does not offer that test. Choose one from the search.",
    ),
}


class OpdOrderIn(BaseModel):
    """POST body of an outpatient investigation. The order type and the words
    on the order come from the code list, never from the client."""

    model_config = ConfigDict(extra="forbid")
    code_system: str = Field(..., max_length=20)
    code: str = Field(..., max_length=64)
    priority: Literal["routine", "urgent", "stat"] = "routine"


class OpdOrderCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., max_length=500)


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _require_current_visit(visit: dict) -> None:
    """The visit must be inside the OPD scope window (permissions.
    opd_scope_window, the window the OPD arm uses): a visit booked for a later
    day opens nothing yet, and an old visit takes no new orders."""
    first_day, last_day = opd_scope_window()
    day = str(visit.get("visit_date") or "").strip()[:10]
    if day and day > last_day:
        raise _refuse(
            409,
            "visit_not_current",
            "This visit is booked for a later day. Order on the day of the visit.",
        )
    if not day or day < first_day:
        raise _refuse(
            409,
            "visit_not_current",
            "This visit is too old to order on. Order from the patient's current visit.",
        )


def _lab_catalogue_in_use() -> bool:
    """A laboratory test comes from the laboratory's own catalogue while the
    laboratory module is on (the rule the Orders screen keeps)."""
    return config.LAB_ENABLED is True


def _clinicians(ids) -> dict:
    """{user id: {name, registration_number}}: what a print says about a
    clinician, and nothing else."""
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    with db_session() as db:
        rows = db.execute(
            f"SELECT id, name, registration_number FROM users WHERE id IN ({marks})", wanted
        ).fetchall()
    return {
        r["id"]: {"name": r["name"], "registration_number": r["registration_number"]} for r in rows
    }


def _order_view(order: dict, names: dict | None = None) -> dict:
    """An outpatient order as the clinic sees it: no internal detail."""
    who = (names or {}).get(order.get("ordered_by")) or {}
    return {
        "id": order["id"],
        "opd_visit_id": order.get("opd_visit_id"),
        "order_type": order["order_type"],
        "code_system": order.get("code_system"),
        "code": order.get("code"),
        "description": order["description"],
        "priority": order["priority"],
        "status": order["status"],
        "result_text": order.get("result_text"),
        "result_at": order.get("result_at"),
        "ordered_by_name": who.get("name"),
        "created_at": order.get("created_at"),
    }


def _phi_read(request: Request, user: dict, **kwargs) -> None:
    """A fail-closed phi_access row before anything is returned."""
    try:
        log_phi_read(request=request, user=user, **kwargs)
    except AuditWriteError:
        raise _audit_unavailable() from None


_TERMINOLOGY_SEARCH_Q_DEFAULT = Query("", max_length=120)
_TERMINOLOGY_SEARCH_KIND_DEFAULT = Query("diagnosis")
_TERMINOLOGY_SEARCH_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.get("/terminology/search")
def terminology_search(
    q: str = _TERMINOLOGY_SEARCH_Q_DEFAULT,
    kind: Literal["diagnosis", "investigation"] = _TERMINOLOGY_SEARCH_KIND_DEFAULT,
    user: dict = _TERMINOLOGY_SEARCH_USER_DEFAULT,
):
    """The code lists behind the diagnosis and investigation pickers. No
    patient data is read, so nothing is audited. A diagnosis stays free text
    with an optional code (ruling OPD-N11); an investigation is a laboratory
    test or imaging only."""
    return terminology_service.search_terms(q, kind=kind, lab_catalogue=_lab_catalogue_in_use())


_VISIT_ORDERS_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.get("/visits/{visit_id}/orders", response_model=None)
def visit_orders(
    visit_id: str,
    request: Request,
    user: dict = _VISIT_ORDERS_USER_DEFAULT,
) -> dict:
    """The investigations ordered on this visit, with their results when
    back. Bounded by the visit's clinic, like the visit's note. Audited."""
    visit = _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    orders = orders_service.list_visit_orders(visit_id)
    _phi_read(
        request,
        user,
        action_type="opd_visit_orders_viewed",
        summary_message=f"Investigations on OPD visit {visit_id} viewed ({len(orders)})",
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=visit_id,
        metadata={"order_ids": [o["id"] for o in orders]},
    )
    names = _clinicians(o.get("ordered_by") for o in orders)
    return {"visit_id": visit_id, "orders": [_order_view(o, names) for o in orders]}


_ORDER_INVESTIGATION_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.post("/visits/{visit_id}/orders", status_code=201, response_model=None)
def order_investigation(
    visit_id: str,
    data: OpdOrderIn,
    request: Request,
    user: dict = _ORDER_INVESTIGATION_USER_DEFAULT,
) -> dict:
    """Order a laboratory test or imaging for this outpatient visit, active at
    once (the patient goes to the laboratory from the clinic).

    Refused: another clinic's visit (404); a visit cancelled, finished, booked
    for a later day or too old, or a patient now admitted (409); the same test
    already ordered on the visit (409 already_ordered); a code that is not a
    laboratory test or imaging, a bedside test, or (laboratory module on) a
    test the laboratory does not offer (422). Order sets are not offered for
    outpatients. Written with its audit row in one transaction."""
    visit = _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    if visit.get("status") == "cancelled":
        raise _refuse(
            409, "visit_cancelled", "This visit was cancelled; nothing can be ordered on it."
        )
    if visit.get("status") == "completed":
        raise _refuse(
            409,
            "visit_completed",
            "This visit is finished. Order from the patient's current visit.",
        )
    _require_current_visit(visit)
    found = terminology_service.describe_investigation(
        data.code_system, data.code, lab_catalogue=_lab_catalogue_in_use()
    )
    if found["refusal"]:
        status, message = _INVESTIGATION_REFUSALS[found["refusal"]]
        raise _refuse(status, found["refusal"], message)
    system = data.code_system.strip().upper()
    code = data.code.strip()

    def _audit(db, order: dict) -> None:
        log_audit_event(
            correlation_id=f"opd-order-{order['id']}",
            event_category="clinical_record",
            action_type="opd_investigation_ordered",
            outcome="success",
            db=db,
            summary_message=(
                f"{order['order_type'].capitalize()} order {order['id']} on OPD "
                f"visit {visit_id}: {order['description']} ({order['priority']})"
            ),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=order["patient_id"],
            target_type="order",
            target_id=str(order["id"]),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            metadata={
                "visit_id": visit_id,
                "order_type": order["order_type"],
                "code_system": order["code_system"],
                "code": order["code"],
                "priority": order["priority"],
                "written": "in_transaction",
            },
        )

    try:
        order = orders_service.create_order(
            patient_id=visit["patient_id"],
            order_type=found["order_type"],
            description=found["display"],
            code=code,
            code_system=system,
            priority=data.priority,
            ordered_by=user.get("id"),
            source="clinician",
            status="active",
            opd_visit_id=visit_id,
            in_transaction=_audit,
        )
    except OpdVisitOrderRefused as err:
        raise _refuse(err.status, err.code, str(err)) from err
    except AuditWriteError:
        raise _audit_unavailable() from None
    except ValueError as err:
        raise _refuse(422, "order_invalid", str(err)) from err
    return _order_view(order, _clinicians([order.get("ordered_by")]))


_CANCEL_INVESTIGATION_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.post("/visits/{visit_id}/orders/{order_id}/cancel", response_model=None)
def cancel_investigation(
    visit_id: str,
    order_id: str,
    data: OpdOrderCancelIn,
    request: Request,
    user: dict = _CANCEL_INVESTIGATION_USER_DEFAULT,
) -> dict:
    """Cancel an investigation ordered on this visit, with a reason. An order
    the laboratory has taken in is cancelled from the laboratory request
    instead (409). Refused once the patient is admitted, on a visit outside
    the OPD window, and for an order of another visit (404)."""
    visit = _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    _require_current_visit(visit)
    reason = (data.reason or "").strip()
    if len(reason) < 3:
        raise _refuse(422, "reason_required", "Say why the investigation is cancelled.")

    def _belongs(db, order: dict) -> None:
        if order.get("opd_visit_id") != visit_id:
            raise OpdVisitOrderRefused("order_not_found", "Order not found", status=404)
        if (
            db.execute(
                "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
                (order["patient_id"],),
            ).fetchone()
            is not None
        ):
            raise OpdVisitOrderRefused(
                "patient_admitted",
                "This patient is now admitted. Manage the order from the inpatient chart.",
            )

    def _audit(db, prior: dict, moved: dict) -> None:
        log_audit_event(
            correlation_id=f"opd-order-cancel-{order_id}",
            event_category="clinical_record",
            action_type="opd_investigation_cancelled",
            outcome="success",
            db=db,
            summary_message=f"Order {order_id} on OPD visit {visit_id} cancelled",
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=moved["patient_id"],
            target_type="order",
            target_id=str(order_id),
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            metadata={
                "visit_id": visit_id,
                "prior_status": prior.get("status"),
                "reason": reason,
                "written": "in_transaction",
            },
        )

    try:
        moved = orders_service.transition_order(
            order_id,
            "cancelled",
            actor_id=user.get("id"),
            detail=reason,
            before_move=_belongs,
            in_transaction=_audit,
        )
    except OpdVisitOrderRefused as err:
        raise _refuse(err.status, err.code, str(err)) from err
    except OrderManagedByLab as err:
        raise _refuse(409, "order_managed_by_lab", str(err)) from err
    except AuditWriteError:
        raise _audit_unavailable() from None
    except ValueError as err:
        message = str(err)
        if "not found" in message:
            raise _refuse(404, "order_not_found", "Order not found") from err
        raise _refuse(
            409, "order_not_cancellable", "This order can no longer be cancelled."
        ) from err
    return _order_view(moved, _clinicians([moved.get("ordered_by")]))


_VISIT_PRINT_KIND_DEFAULT = Query("consultation")
_VISIT_PRINT_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.get("/visits/{visit_id}/print", response_model=None)
def visit_print(
    visit_id: str,
    request: Request,
    kind: Literal["consultation", "investigations"] = _VISIT_PRINT_KIND_DEFAULT,
    user: dict = _VISIT_PRINT_USER_DEFAULT,
) -> dict:
    """What the consultation print or the investigation slip carries, read as
    it is printed (audited as a print, fail-closed).

    Only what the record holds: the patient's name, UHID, age and sex; the
    signed note (a draft is never printed: 409 not_signed) or the visit's
    investigations not cancelled (409 no_investigations when there are none);
    and the doctor's name and registration number. No date of birth, phone,
    address or allergy list (a visit-addressed answer never names allergens),
    and no text written for the print."""
    visit = _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    orders = [
        o for o in orders_service.list_visit_orders(visit_id) if o.get("status") != "cancelled"
    ]
    note = None
    if kind == "consultation":
        try:
            note = get_consultation(visit_id).get("consultation")
        except ConsultationError as err:
            raise _consultation_http_error(err) from err
        if not note or note.get("status") != "signed":
            raise _refuse(409, "not_signed", "Only a signed note can be printed. Sign it first.")
        doctor_ids = [note.get("signed_by")]
    else:
        if not orders:
            raise _refuse(409, "no_investigations", "No investigations are ordered on this visit.")
        doctor_ids = [o.get("ordered_by") for o in orders]
    names = _clinicians(doctor_ids + [o.get("ordered_by") for o in orders])
    doctors = []
    for uid in dict.fromkeys(i for i in doctor_ids if i):
        who = names.get(uid) or {}
        doctors.append(
            {"name": who.get("name"), "registration_number": who.get("registration_number")}
        )
    _phi_read(
        request,
        user,
        action_type=(
            "opd_consultation_printed"
            if kind == "consultation"
            else "opd_investigation_slip_printed"
        ),
        summary_message=f"OPD {kind} print for visit {visit_id}",
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=visit_id,
        metadata={
            "kind": kind,
            "order_ids": [o["id"] for o in orders],
            "consultation_id": (note or {}).get("id"),
        },
    )
    consultation = None
    if note:
        consultation = {
            "id": note["id"],
            "chief_complaint": note.get("chief_complaint"),
            "history": note.get("history"),
            "examination": note.get("examination"),
            "vitals": (
                {
                    "values": note["vitals"].get("values") or {},
                    "measured_at": note["vitals"].get("measured_at"),
                }
                if note.get("vitals")
                else None
            ),
            "diagnoses": [
                {"text": d.get("text"), "code_system": d.get("code_system"), "code": d.get("code")}
                for d in note.get("diagnoses") or []
            ],
            "plan": note.get("plan"),
            "advice": note.get("advice"),
            "follow_up_date": note.get("follow_up_date"),
            "follow_up_note": note.get("follow_up_note"),
            "referral_department": note.get("referral_department"),
            "signed_at": note.get("signed_at"),
            "addenda": [
                {
                    "body": a.get("body"),
                    "created_at": a.get("created_at"),
                    "author_name": a.get("author_name"),
                }
                for a in note.get("addenda") or []
            ],
        }
    try:
        # The age as of today, the way the UHID card prints it (a stated age
        # is aged from the day it was stated), never a date of birth.
        card = registration_service.uhid_card(visit["patient_id"])
    except registration_service.RegistrationError:
        card = {
            "name": visit.get("patient_name"),
            "uhid": visit.get("patient_mrn"),
            "age": visit.get("patient_age"),
            "sex": visit.get("patient_sex"),
        }
    return {
        "kind": kind,
        "printed_at": utc_iso_now(),
        "visit": {
            "id": visit["id"],
            "visit_date": visit.get("visit_date"),
            "department": visit.get("department"),
            "token_number": visit.get("token_number"),
        },
        "patient": {
            "name": card.get("name"),
            "uhid": card.get("uhid"),
            "age": card.get("age"),
            "sex": card.get("sex"),
        },
        "consultation": consultation,
        "investigations": [
            {
                k: v
                for k, v in _order_view(o, names).items()
                if k
                in (
                    "description",
                    "order_type",
                    "priority",
                    "status",
                    "created_at",
                    "ordered_by_name",
                )
            }
            for o in orders
        ],
        "doctors": doctors,
    }


_COMPLETE_WITH_DISPOSITION_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.post("/visits/{visit_id}/complete", response_model=None)
def complete_with_disposition(
    visit_id: str,
    data: OpdCompleteIn,
    request: Request,
    user: dict = _COMPLETE_WITH_DISPOSITION_USER_DEFAULT,
) -> dict:
    """Finish the visit with a disposition and raise the consultation charge
    (the front desk's charge logic, unchanged: one charge per visit, unpriced
    rather than free when the site has not set a rate).

    Ruling OPD-N8: a signed note, or a reason (for example, the note is on
    paper). 'admitted' is not a disposition here: converting a patient to an
    inpatient is its own step (opd-17a).
    """
    _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    disposition = (data.disposition or "").strip().lower()
    if disposition not in dict(COMPLETION_DISPOSITIONS):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "bad_disposition",
                "message": "Choose how the visit ended: "
                + ", ".join(lab.lower() for _, lab in COMPLETION_DISPOSITIONS),
            },
        )
    reason = (data.no_signed_note_reason or "").strip()[:500] or None
    facts: dict = {}

    def _precheck(db, row) -> None:
        if (
            db.execute(
                "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
                (row["patient_id"],),
            ).fetchone()
            is not None
        ):
            raise ConsultationError(
                "patient_admitted",
                "This patient is now admitted. Finish the visit from the inpatient chart.",
                status=409,
            )
        signed = consultation_signed(db, visit_id)
        if not signed and not reason:
            raise ConsultationError(
                "signed_note_required",
                "Sign the consultation note first, or say why there is none "
                "(for example, the note is on paper).",
                status=409,
            )
        facts.update(signed_note=signed, no_signed_note_reason=None if signed else reason)

    def _audit(db, visit: dict, svc_facts: dict) -> None:
        charge = visit.get("consultation_charge") or {}
        log_audit_event(
            correlation_id=f"opd-complete-{visit['id']}",
            event_category="clinical_record",
            action_type="opd_visit_completed",
            outcome="success",
            db=db,
            summary_message=(
                f"OPD visit {visit['id']} completed ({visit.get('disposition')}); "
                f"consultation charge {charge.get('charge_id') or 'not raised'}"
            ),
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
        raise _consultation_http_error(err) from err
    except FrontDeskError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    except AuditWriteError:
        raise _audit_unavailable() from None
    nudge_queue_changed(visit.get("department"))
    return visit


# ═════════════════════════════════════════════════════════════════════════════
#  The outpatient prescription (opd-12, W8-C)
# ═════════════════════════════════════════════════════════════════════════════
# Lines are medication orders in the shared store (services/medorders/
# outpatient.py), encounter_type 'op'. Never a medications row (the eMAR's
# source), never a Helix supply row (outpatient dispensing is the pharmacy
# epic's lane). Every route here is visit-addressed: the visit must be the
# caller's clinic's (404 otherwise), derived from department_scope in the
# route. The store switch and the Ward Record drug chart switch come before
# the permission, so a site without the store, or an Orb Ward Record
# computer, answers 404 before anything about a patient or a drug is read.

#: ORB_MED_ORDER_STORE at request time (404 feature_disabled while off).
_require_med_order_store = feature_gate("med_order_store")

_PRESCRIPTION_GATES = [Depends(_require_med_order_store), Depends(require_drug_chart)]

#: The longest Idempotency-Key a client may send for a line.
_MAX_KEY_LENGTH = 128


class PrescriptionLineIn(BaseModel):
    """POST body of one prescription line. A field this route does not know
    is refused (422), so nobody believes it was prescribed. The route is
    never defaulted: the prescriber names it."""

    model_config = ConfigDict(extra="forbid")
    drug_name: str = Field(..., max_length=200)
    dose: str = Field(..., max_length=60)
    route: str = Field(..., max_length=40)
    frequency: str = Field(..., max_length=60)
    is_prn: bool = False
    prn_indication: str | None = Field(None, max_length=200)
    duration_days: int | None = Field(None, ge=1, le=outpatient.MAX_DURATION_DAYS)
    quantity: int | None = Field(None, ge=1, le=outpatient.MAX_QUANTITY)
    instructions: str | None = Field(None, max_length=500)
    indication: str | None = Field(None, max_length=200)
    # {acknowledged: true, reason}: the prescriber's written reason after a
    # 428 drug_safety_acknowledgement_required.
    safety_ack: dict | None = None


class PrescriptionCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., max_length=500)
    # The line's version as the screen showed it; a later change is 409.
    expected_version: int | None = None


def _request_meta(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _prescription_refusal(err: "outpatient.PrescriptionRefused") -> HTTPException:
    return HTTPException(
        status_code=err.status, detail={"error": err.code, "message": str(err), **err.extra}
    )


def _medorder_refusal(err: medorder_core.MedOrderError) -> HTTPException:
    return HTTPException(status_code=err.http_status, detail=medorder_core.http_detail(err))


def _line_key(request: Request, user: dict) -> str | None:
    """The client's Idempotency-Key for this line, kept per prescriber so one
    doctor's key can never replay another's line."""
    raw = (request.headers.get("Idempotency-Key") or "").strip()
    if not raw:
        return None
    if len(raw) > _MAX_KEY_LENGTH or not raw.isprintable():
        raise _refuse(
            422,
            "bad_idempotency_key",
            f"The request key must be at most {_MAX_KEY_LENGTH} plain characters.",
        )
    return f"opd-rx:{user.get('id')}:{raw}"


_VISIT_PRESCRIPTIONS_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.get(
    "/visits/{visit_id}/prescriptions", dependencies=_PRESCRIPTION_GATES, response_model=None
)
def visit_prescriptions(
    visit_id: str,
    request: Request,
    user: dict = _VISIT_PRESCRIPTIONS_USER_DEFAULT,
) -> dict:
    """The medicines prescribed on this visit, in every state (a cancelled
    line says why). Bounded by the visit's clinic, like the visit's note and
    investigations. Audited fail-closed (phi_access) with the line ids. The
    stored screen is not returned: a visit id of any date reaches this read,
    so it names no allergen."""
    visit = _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    try:
        result = outpatient.list_visit_prescriptions(visit_id)
    except outpatient.PrescriptionRefused as err:
        raise _prescription_refusal(err) from err
    _phi_read(
        request,
        user,
        action_type=outpatient.LIST_AUDIT_ACTION,
        summary_message=(
            f"Prescription on OPD visit {visit_id} viewed ({len(result['prescriptions'])} lines)"
        ),
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=visit_id,
        metadata={"order_ids": [p["id"] for p in result["prescriptions"]]},
    )
    return result


_WRITE_VISIT_PRESCRIPTION_USER_DEFAULT = Depends(
    require_permission(OPD_CONSULT, MEDICATION_PRESCRIBE)
)


@router.post(
    "/visits/{visit_id}/prescriptions",
    status_code=201,
    dependencies=_PRESCRIPTION_GATES,
    response_model=None,
)
def write_visit_prescription(
    visit_id: str,
    data: PrescriptionLineIn,
    request: Request,
    user: dict = _WRITE_VISIT_PRESCRIPTION_USER_DEFAULT,
) -> dict | JSONResponse:
    """Prescribe one medicine on this outpatient visit, signed by the caller
    as its prescriber (opd-12).

    Screened before anything is written: the allergy review first (ruling
    OPD-S7 (a): 409 allergy_review_required until one lets the note be
    signed), then the medication safety gate (allergy, interaction and dose,
    entry point 'opd_prescription'; 428 until a prescriber acknowledges a
    severe alert or a dose block with a reason) and the patient's other live
    outpatient prescriptions. Refused: another clinic's visit (404); a visit
    cancelled, finished, outside the OPD window, or a patient now admitted
    (409); an allergy or drug charted while it was being checked (409
    screen_stale). Written with its fail-closed audit row in one transaction.
    Never a medications row, never a Helix row. A repeated Idempotency-Key
    for the same line answers 200 with the line it wrote."""
    _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    key = _line_key(request, user)
    fields = data.model_dump(exclude={"safety_ack"})
    try:
        result = outpatient.write_prescription(
            visit_id,
            fields,
            actor_id=user.get("id") or "",
            actor_role=user.get("role"),
            safety_ack=data.safety_ack,
            idempotency_key=key,
            request_meta=_request_meta(request),
        )
    except outpatient.PrescriptionRefused as err:
        raise _prescription_refusal(err) from err
    except medorder_core.MedOrderError as err:
        raise _medorder_refusal(err) from err
    except AuditWriteError:
        raise _audit_unavailable() from None
    if result["replayed"]:
        return JSONResponse(status_code=200, content=jsonable_encoder(result))
    return result


_CANCEL_VISIT_PRESCRIPTION_USER_DEFAULT = Depends(
    require_permission(OPD_CONSULT, MEDICATION_PRESCRIBE)
)


@router.post(
    "/visits/{visit_id}/prescriptions/{order_id}/cancel",
    dependencies=_PRESCRIPTION_GATES,
    response_model=None,
)
def cancel_visit_prescription(
    visit_id: str,
    order_id: str,
    data: PrescriptionCancelIn,
    request: Request,
    user: dict = _CANCEL_VISIT_PRESCRIPTION_USER_DEFAULT,
) -> dict:
    """Cancel one prescribed medicine on this open visit, with a reason (at
    least 3 characters). Refused once the visit is finished or cancelled, on
    a visit outside the OPD window, once the patient is admitted (409), and
    for a line of another visit (404). The answer says whether a print that
    listed the line was already made, so the clinic takes that copy back."""
    _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    try:
        return outpatient.cancel_prescription(
            visit_id,
            order_id,
            reason=data.reason,
            expected_version=data.expected_version,
            actor_id=user.get("id") or "",
            actor_role=user.get("role"),
            request_meta=_request_meta(request),
        )
    except outpatient.PrescriptionRefused as err:
        raise _prescription_refusal(err) from err
    except medorder_core.MedOrderError as err:
        raise _medorder_refusal(err) from err
    except AuditWriteError:
        raise _audit_unavailable() from None


_VISIT_PRESCRIPTION_PRINT_USER_DEFAULT = Depends(require_permission(OPD_CONSULT))


@router.get(
    "/visits/{visit_id}/prescriptions/print", dependencies=_PRESCRIPTION_GATES, response_model=None
)
def visit_prescription_print(
    visit_id: str,
    request: Request,
    user: dict = _VISIT_PRESCRIPTION_PRINT_USER_DEFAULT,
) -> dict:
    """What the printed prescription carries, read as it is printed (audited
    as a print, fail-closed, naming the lines printed).

    The patient's name, UHID, age and sex; the allergy review as status,
    label and time (never an allergen: a visit-addressed answer); the signed
    note's diagnoses (a draft is never printed); the live lines; and each
    prescriber's name and registration number. No date of birth, phone,
    address, score or text written for the print. 409 no_prescriptions when
    no line is live."""
    visit = _visit_in_clinic(visit_id, department_scope(user), user=user, request=request)
    try:
        data = outpatient.print_lines(visit_id)
    except outpatient.PrescriptionRefused as err:
        raise _prescription_refusal(err) from err
    names = _clinicians(data["prescriber_ids"])
    _phi_read(
        request,
        user,
        action_type=outpatient.PRINT_AUDIT_ACTION,
        summary_message=f"OPD prescription print for visit {visit_id}",
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=visit_id,
        metadata={"kind": "prescription", "order_ids": [line["id"] for line in data["lines"]]},
    )
    try:
        card = registration_service.uhid_card(visit["patient_id"])
    except registration_service.RegistrationError:
        card = {
            "name": visit.get("patient_name"),
            "uhid": visit.get("patient_mrn"),
            "age": visit.get("patient_age"),
            "sex": visit.get("patient_sex"),
        }
    return {
        "kind": "prescription",
        "printed_at": utc_iso_now(),
        "visit": {
            "id": visit["id"],
            "visit_date": visit.get("visit_date"),
            "department": visit.get("department"),
            "token_number": visit.get("token_number"),
        },
        "patient": {
            "name": card.get("name"),
            "uhid": card.get("uhid"),
            "age": card.get("age"),
            "sex": card.get("sex"),
        },
        "allergy_review": data["allergy_review"],
        "diagnoses": data["diagnoses"],
        "prescriptions": [
            {
                k: line[k]
                for k in (
                    "drug_name",
                    "dose",
                    "route",
                    "frequency",
                    "is_prn",
                    "prn_indication",
                    "duration_days",
                    "quantity",
                    "instructions",
                    "prescribed_at",
                    "prescriber_name",
                )
            }
            for line in data["lines"]
        ],
        "doctors": [
            {
                "name": (names.get(uid) or {}).get("name"),
                "registration_number": (names.get(uid) or {}).get("registration_number"),
            }
            for uid in data["prescriber_ids"]
        ],
    }
