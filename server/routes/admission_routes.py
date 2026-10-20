from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
)
from permissions import department_scope, log_phi_read, require_permission
from pydantic import BaseModel, ConfigDict, Field
from services.admission_service import (
    CONVERSION_PREVIEW_AUDIT_ACTION,
    AdmissionInputError,
    AdmissionRefused,
    admission_choices,
    admit_existing_patient,
    admit_patient,
    clerk_admission_choices,
    clerk_attending,
    conversion_preview,
    readmit_patient,
    require_listed_attending,
    resolve_admission_department,
)
from services.adt import clerk as adt_clerk
from services.adt.core import AdtError, http_detail
from services.adt.live import notify_adt_changed
from services.audit_service import AuditWriteError
from services.patient_name_rule import PatientNameError

from routes.adt.end_stay import _scoped_admission, require_ward_clerk
from routes.opt_in_gates import feature_gate, require_adt_module, require_opd_feature
from services import registration_service

router = APIRouter(prefix="/api/admissions", tags=["Admissions"])


class AdmissionRequest(BaseModel):
    # Patient Identity
    name: str
    # Bounded so a slip (-4, 440) is refused at the door (422), not charted.
    age: int | None = Field(None, ge=0, le=130)
    # OPTIONAL, and it must stay optional: an admission is never blocked on
    # paperwork. It is the second factor on the patient's discharge link, so a
    # patient admitted without one gets a stated refusal at the discharge desk
    # instead of a QR code that cannot be opened (services/admission_service.
    # _record_dob, services/discharge_access_service).
    date_of_birth: str | None = None
    sex: str | None = None
    phone: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    address: str | None = None
    mrn: str | None = None

    # Admission Context
    admission_source: str
    admission_type: str
    department: str
    ward: str
    bed: str
    attending_doctor_id: str
    referring_clinician: str | None = None

    # Clinical Presentation
    chief_complaint: str
    initial_diagnosis: str | None = None
    triage_severity: int | None = None

    # Clinical History
    comorbidities: list[str] | None = []
    past_medical_history: str | None = None
    past_surgical_history: str | None = None
    allergies: list[str] | None = []
    current_medications: list[str] | None = []
    family_history: str | None = None
    social_history: str | None = None

    # Initial Vitals
    vitals: dict[str, Any] | None = None

    # Notes
    admitting_notes: str | None = None
    nursing_notes: str | None = None
    isolation_precautions: list[str] | None = []
    code_status: str | None = "full_code"

    # Beds and admissions (adt-5). Read ONLY while ORB_ADT is on; ignored while
    # it is off. Every one is optional and ward and bed keep their str types,
    # so a body without them is exactly what it always was, and a body missing
    # ward or bed is still a 422 whatever the module says.
    #: A ward and a bed from bed setup, as the bed picker chose them.
    ward_id: str | None = None
    bed_id: str | None = None
    #: 'No bed yet': admitted to the ward (or nowhere yet), awaiting a bed.
    awaiting_bed: bool | None = None
    #: When the stay began, up to ORB_ADT_BACKTIME_HOURS back; now when omitted.
    admitted_at: str | None = None
    #: Typed reason for a bed housekeeping records out of service.
    out_of_service_reason: str | None = None
    #: The admitting clinician has read that host-ward nurses will not open a
    #: boarder's chart (ruling ADT-S6 is not applied).
    boarder_acknowledged: bool | None = None
    #: Ruling ADT-S1 (a): bed_id is occupied, so register a temporary extra bed
    #: beside it (its own label) and admit the patient there.
    extra_bed: bool | None = None
    #: Ruling ADT-S2 (c): typed reason for a bed not yet recorded clean.
    not_clean_reason: str | None = None
    #: Ruling ADT-S2 (c): who authorises a bed awaiting a terminal clean, and
    #: why. The sender (their own id), or another doctor, nurse or
    #: administrator who confirms it with their own signing PIN, typed here;
    #: the PIN is checked and dropped, never stored. A name alone is refused.
    terminal_clean_authorised_by: str | None = None
    terminal_clean_reason: str | None = None
    terminal_clean_authoriser_pin: str | None = None

    # The patient index (opd-5b). Read ONLY while ORB_REGISTRATION is on.
    #: Past a reviewable possible duplicate: {decision: 'admit_new',
    #: candidate_ids: [every record the refusal listed], reason}. Never past
    #: the same hospital number, and never past a probable duplicate while
    #: ORB_DUPLICATE_MODE is 'block' (the shipped default).
    duplicate_review: dict[str, Any] | None = None


_ADMISSION_OPTIONS_USER_DEFAULT = Depends(require_permission("patient.write"))


@router.get("/options")
def admission_options(user: dict = _ADMISSION_OPTIONS_USER_DEFAULT):
    """What the admission form chooses from: covered departments, attending
    doctors, and ward and bed labels with whether each bed is held.

    Served here rather than from the front desk's transfer destinations, which
    sit behind the front desk module switch and patient.read_all; an admission
    must not depend on either. No patient data, so no PHI read is logged.
    """
    return admission_choices(user.get("department"), user=user)


_PROCESS_ADMISSION_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_PROCESS_ADMISSION_USER_DEFAULT = Depends(require_permission("patient.write"))


@router.post("/admit")
def process_admission(
    payload: AdmissionRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    idempotency_key: str | None = _PROCESS_ADMISSION_IDEMPOTENCY_KEY_DEFAULT,
    user: dict = _PROCESS_ADMISSION_USER_DEFAULT,
):
    """
    Process a new patient admission.
    Transactional across all required Orb modules.

    With ORB_ADT on (adt-5) the patient is placed through the bed master in
    the same transaction: 201 for a new admission, 200 for a replay of the
    same Idempotency-Key, and a refusal (an occupied, retired or switched-off
    bed, a bed not recorded clean without its reason, a boarder without the
    acknowledgement, an admission time outside the back-time window) answers
    {error, message, ...} with nothing written. With it off the answer is
    exactly as before (200), and the new fields and the header are ignored.
    """
    try:
        data = payload.dict()
        # Checked against the live lists before anything is written. The
        # department decides whose board the patient is on (a typo put them on
        # nobody's) and is stored in its canonical spelling; an unknown
        # attending id reached the foreign key as a raw 500.
        data["department"] = resolve_admission_department(payload.department)
        data["attending_doctor_id"] = require_listed_attending(payload.attending_doctor_id)
        # Trimmed so the bed-held check and the ward boards compare like with like.
        data["ward"] = payload.ward.strip()
        data["bed"] = payload.bed.strip()
        # Pass to service layer for transactional execution
        result = admit_patient(
            data,
            user,
            idempotency_key=idempotency_key,
            request_meta={
                "request_path": request.url.path,
                "http_method": request.method,
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
            },
        )
        if "movement_id" in result and not result.get("replayed"):
            # Only with ORB_ADT on: a new admission placed through the master.
            response.status_code = 201
            # adt-19: it has committed, so the bed boards refetch.
            notify_adt_changed(answer=result, departments=[data["department"]])
        return result
    except PatientNameError as e:
        # The registration name rule: the clinician typed something that is
        # not a name. Their mistake to correct, so 400 with the rule's words,
        # not the generic 500 below.
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AdmissionInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AdmissionRefused as e:
        # Always: a hospital number already on another record (409
        # mrn_in_use, naming nobody). With ORB_REGISTRATION on: the hospital
        # number rule (422 or 409) and the patient index (409
        # possible_existing_patient). Nothing was written.
        raise HTTPException(status_code=e.status, detail=e.detail()) from e
    except AdtError as e:
        # Only with ORB_ADT on: the bed master refused the place, in words,
        # and nothing was written.
        raise HTTPException(status_code=e.http_status, detail=http_detail(e)) from e
    except AuditWriteError:
        # The patient_movement audit is fail-closed: an admission whose record
        # could not be written is not made at all.
        raise HTTPException(
            status_code=503,
            detail={
                "error": "audit_unavailable",
                "message": "The record of this admission could not be written, so the patient "
                "was not admitted. Try again in a moment.",
            },
        ) from None
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Admission failed: {str(e)}") from e


# ═════════════════════════════════════════════════════════════════════════════
#  Admitting a registered outpatient from their visit (opd-17a, ORB_OPD)
# ═════════════════════════════════════════════════════════════════════════════
#
#   GET  /api/admissions/admit-existing/visits/{visit_id}   what the form shows
#   POST /api/admissions/admit-existing                     the conversion
#
# Both sit behind the 'opd' feature (404 while it is off) and patient.write
# (the admitting doctor), and both are VISIT-ADDRESSED: the visit must be in
# the caller's own clinic, permissions.department_scope(user), never a
# department the request names (404 otherwise). The body names no patient:
# the patient is the visit's. See services/admission_service.py, 'A
# registered outpatient becomes an inpatient', for what is written and the
# rulings built strictest (OPD-S1, OPD-S3, OPD-S4, OPD-N8).


class StatedIdentityIn(BaseModel):
    """What the patient STATES (ruling OPD-S1): their name, their date of
    birth or age, and the last 4 digits of their phone or their UHID.
    Compared on the server; never echoed back."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, max_length=200)
    date_of_birth: str | None = Field(None, max_length=40)
    age_years: int | None = None
    phone_last4: str | None = Field(None, max_length=10)
    uhid: str | None = Field(None, max_length=40)


class AdmitExistingRequest(BaseModel):
    """The admission form's fields for a patient already on file. No identity
    field (the record's own is kept) and no observations (ruling OPD-S3 (a):
    the ward charts a fresh set), so either is a 422."""

    model_config = ConfigDict(extra="forbid")
    opd_visit_id: str = Field(..., min_length=1, max_length=80)
    identity_check: StatedIdentityIn | None = None
    #: Ruling OPD-N8: why the visit has no signed note, when it has none.
    no_signed_note_reason: str | None = Field(None, max_length=500)

    admission_source: str = Field("clinic", max_length=40)
    admission_type: str = Field("inpatient", max_length=40)
    department: str
    ward: str
    bed: str
    attending_doctor_id: str
    referring_clinician: str | None = None

    chief_complaint: str
    initial_diagnosis: str | None = None
    triage_severity: int | None = Field(None, ge=1, le=5)

    comorbidities: list[str] | None = []
    past_medical_history: str | None = None
    past_surgical_history: str | None = None
    #: Allergies to ADD; every allergy already charted stays as it is.
    allergies: list[str] | None = []
    current_medications: list[str] | None = []
    family_history: str | None = None
    social_history: str | None = None

    admitting_notes: str | None = None
    nursing_notes: str | None = None
    isolation_precautions: list[str] | None = []
    code_status: str | None = "full_code"

    # Beds and admissions (adt-5), read only while ORB_ADT is on, as on /admit.
    ward_id: str | None = None
    bed_id: str | None = None
    awaiting_bed: bool | None = None
    admitted_at: str | None = None
    out_of_service_reason: str | None = None
    boarder_acknowledged: bool | None = None
    extra_bed: bool | None = None
    not_clean_reason: str | None = None
    terminal_clean_authorised_by: str | None = None
    terminal_clean_reason: str | None = None
    terminal_clean_authoriser_pin: str | None = None


def _request_meta(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _audit_unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "audit_unavailable",
            "message": f"The record of this {what} could not be written, so nothing was done. "
            "Try again in a moment.",
        },
    )


_ADMIT_EXISTING_PREVIEW_OPD_DEFAULT = Depends(require_opd_feature)
_ADMIT_EXISTING_PREVIEW_USER_DEFAULT = Depends(require_permission("patient.write"))


@router.get("/admit-existing/visits/{visit_id}")
def admit_existing_preview(
    visit_id: str,
    request: Request,
    _opd: None = _ADMIT_EXISTING_PREVIEW_OPD_DEFAULT,
    user: dict = _ADMIT_EXISTING_PREVIEW_USER_DEFAULT,
):
    """What the admission form shows before admitting this visit's patient:
    who they are (never the date of birth), this visit's OPD observations and
    the patient's outpatient medicines with their times, for reference only,
    the charted allergies, the complaint and diagnosis to start from, and
    whether the patient can be admitted from this visit now. A visit the
    patient cannot be admitted from answers with the visit and the refusal
    only, nothing about the patient (the clinic may be outside the patient's
    OPD scope by then). Audited as a PHI read (fail-closed) either way."""
    dept = department_scope(user)
    try:
        preview, patient_id = conversion_preview(visit_id, scope_department=dept)
    except AdmissionRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail()) from e
    refusal = preview.get("refusal") or {}
    try:
        log_phi_read(
            request=request,
            user=user,
            action_type=CONVERSION_PREVIEW_AUDIT_ACTION,
            summary_message=f"Admission from outpatient visit {visit_id} prepared",
            patient_id=patient_id,
            target_type="opd_visit",
            target_id=visit_id,
            metadata={
                "opd_visit_id": visit_id,
                "admittable": preview["admittable"],
                "refusal": refusal.get("error"),
                "patient_details_shown": bool(preview["admittable"]),
                "opd_medicines_listed": len(preview.get("opd_medicines") or []),
            },
        )
    except AuditWriteError:
        raise _audit_unavailable("read") from None
    return preview


_ADMIT_EXISTING_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_ADMIT_EXISTING_OPD_DEFAULT = Depends(require_opd_feature)
_ADMIT_EXISTING_USER_DEFAULT = Depends(require_permission("patient.write"))


@router.post("/admit-existing")
def admit_existing(
    payload: AdmitExistingRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = _ADMIT_EXISTING_IDEMPOTENCY_KEY_DEFAULT,
    _opd: None = _ADMIT_EXISTING_OPD_DEFAULT,
    user: dict = _ADMIT_EXISTING_USER_DEFAULT,
):
    """Admit the registered outpatient of this open visit onto their own
    record: 201 for a new admission, 200 for a replay of the same
    Idempotency-Key (required). The visit is finished as 'admitted' in the
    same transaction. Refusals answer {error, message, ...} with nothing
    written."""
    dept = department_scope(user)
    try:
        data = payload.model_dump(exclude={"opd_visit_id", "identity_check"})
        data["department"] = resolve_admission_department(payload.department)
        data["attending_doctor_id"] = require_listed_attending(payload.attending_doctor_id)
        data["ward"] = payload.ward.strip()
        data["bed"] = payload.bed.strip()
        result = admit_existing_patient(
            payload.opd_visit_id,
            data,
            user,
            scope_department=dept,
            identity=(
                payload.identity_check.model_dump() if payload.identity_check is not None else None
            ),
            idempotency_key=idempotency_key,
            request_meta=_request_meta(request),
        )
    except AdmissionInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AdmissionRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail()) from e
    except registration_service.RegistrationNotFound:
        raise HTTPException(status_code=404, detail="Patient not found") from None
    except registration_service.RegistrationError as e:
        raise HTTPException(status_code=e.status, detail=e.detail()) from e
    except AdtError as e:
        raise HTTPException(status_code=e.http_status, detail=http_detail(e)) from e
    except AuditWriteError:
        raise _audit_unavailable("admission") from None
    response.status_code = 200 if result.get("replayed") else 201
    if not result.get("replayed"):
        from services.opd_queue_service import nudge_queue_changed

        # The patient left the clinic's queue: open queues refetch.
        nudge_queue_changed((result.get("visit") or {}).get("department"))
        if result.get("movement_id"):
            from services.adt import transfer as adt_transfer

            # ORB_ADT on: the admission movement reaches housekeeping now
            # (best-effort, never fails the admission), as a move's does.
            adt_transfer.sync_housekeeping_after_commit()
            # adt-19: and the bed boards refetch.
            notify_adt_changed(answer=result, admission_ids=[result.get("admission_id")])
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Readmitting a discharged patient onto the same record (opd-17c, W8-A)
# ═════════════════════════════════════════════════════════════════════════════
#
#   POST /api/admissions/readmit
#
# Behind the 'readmission' feature (ORB_READMISSION, which needs
# ORB_REGISTRATION; 404 while it is off) and patient.write (the admitting
# doctor). The patient named in the body is the record the admission form's
# patient index refusal listed as 'readmittable' (shown only to a caller
# holding registration.read); what the patient STATES is checked against it
# (ruling OPD-S1) before anything is written, and a patient with an active
# admission anywhere is refused. See services/admission_service.py,
# 'Readmitting a discharged patient onto the same record', for what one call
# writes and the rulings built strictest (OPD-S1, OPD-S8, ADT-S9, R029 (2)).

require_readmission_feature = feature_gate("readmission")


class ReadmitRequest(BaseModel):
    """The admission form's fields for a discharged patient readmitted onto
    their own record. No identity field (the record's own is kept) and no
    observations (ruling ADT-S9 (a): the ward charts a fresh set), so either
    is a 422."""

    model_config = ConfigDict(extra="forbid")
    patient_id: str = Field(..., min_length=1, max_length=80)
    identity_check: StatedIdentityIn | None = None

    admission_source: str = Field("er", max_length=40)
    admission_type: str = Field("inpatient", max_length=40)
    department: str
    ward: str
    bed: str
    attending_doctor_id: str
    referring_clinician: str | None = None

    chief_complaint: str
    initial_diagnosis: str | None = None
    triage_severity: int | None = Field(None, ge=1, le=5)

    comorbidities: list[str] | None = []
    past_medical_history: str | None = None
    past_surgical_history: str | None = None
    #: Allergies to ADD; every allergy already charted stays as it is.
    allergies: list[str] | None = []
    current_medications: list[str] | None = []
    family_history: str | None = None
    social_history: str | None = None

    admitting_notes: str | None = None
    nursing_notes: str | None = None
    isolation_precautions: list[str] | None = []
    code_status: str | None = "full_code"

    # Beds and admissions (adt-5), read only while ORB_ADT is on, as on /admit.
    ward_id: str | None = None
    bed_id: str | None = None
    awaiting_bed: bool | None = None
    admitted_at: str | None = None
    out_of_service_reason: str | None = None
    boarder_acknowledged: bool | None = None
    extra_bed: bool | None = None
    not_clean_reason: str | None = None
    terminal_clean_authorised_by: str | None = None
    terminal_clean_reason: str | None = None
    terminal_clean_authoriser_pin: str | None = None


_READMIT_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_READMIT_READMISSION_DEFAULT = Depends(require_readmission_feature)
_READMIT_USER_DEFAULT = Depends(require_permission("patient.write"))


@router.post("/readmit")
def readmit(
    payload: ReadmitRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = _READMIT_IDEMPOTENCY_KEY_DEFAULT,
    _readmission: None = _READMIT_READMISSION_DEFAULT,
    user: dict = _READMIT_USER_DEFAULT,
):
    """Readmit a discharged patient onto their own record: 201 for a new
    admission, 200 for a replay of the same Idempotency-Key (required). The
    answer lists the earlier stay's medicines it stopped, for the prescriber
    to prescribe again, and the fresh one-time portal password, shown once.
    Refusals answer {error, message, ...} with nothing written."""
    dept = department_scope(user)
    try:
        data = payload.model_dump(exclude={"patient_id", "identity_check"})
        data["department"] = resolve_admission_department(payload.department)
        data["attending_doctor_id"] = require_listed_attending(payload.attending_doctor_id)
        data["ward"] = payload.ward.strip()
        data["bed"] = payload.bed.strip()
        result = readmit_patient(
            payload.patient_id,
            data,
            user,
            scope_department=dept,
            identity=(
                payload.identity_check.model_dump() if payload.identity_check is not None else None
            ),
            idempotency_key=idempotency_key,
            request_meta=_request_meta(request),
        )
    except AdmissionInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AdmissionRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail()) from e
    except registration_service.RegistrationNotFound:
        raise HTTPException(status_code=404, detail="Patient not found") from None
    except registration_service.RegistrationError as e:
        raise HTTPException(status_code=e.status, detail=e.detail()) from e
    except AdtError as e:
        raise HTTPException(status_code=e.http_status, detail=http_detail(e)) from e
    except AuditWriteError:
        raise _audit_unavailable("readmission") from None
    response.status_code = 200 if result.get("replayed") else 201
    if not result.get("replayed") and result.get("movement_id"):
        from services.adt import transfer as adt_transfer

        # ORB_ADT on: the admission movement reaches housekeeping now
        # (best-effort, never fails the readmission), as a move's does.
        adt_transfer.sync_housekeeping_after_commit()
        # adt-19: and the bed boards refetch.
        notify_adt_changed(answer=result, admission_ids=[result.get("admission_id")])
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  The ward clerk's admission (adt-17b, W9-A; ORB_ADT, default off)
# ═════════════════════════════════════════════════════════════════════════════
#
#   GET  /api/admissions/clerk-options                  what the clerk's form offers
#   POST /api/admissions/clerk-admit                    a registration-level admission
#   POST /api/admissions/{admission_id}/assessment      a doctor's admission assessment
#
# Ruling ADT-S5 (a), applied as a build default (services/adt/clerk.py states
# it in full): the ward clerk admits at registration level. Every clinical
# field is stored as not yet assessed, no allergy and no 'no known allergies'
# is ever recorded by the clerk, the interlocks treat the patient as unknown,
# and the bed board flags 'Admission assessment due' until a doctor records
# the admission assessment. Behind the module switch (404 while ORB_ADT is
# off) and the clerk's own lane (require_ward_clerk: the ward clerk, holding
# adt.bed, with a department; 403 for anyone else, audited).


class ClerkAdmissionRequest(BaseModel):
    """The ward clerk's admission: who the patient is, the hospital's own
    number, where they lie and who attends. NOTHING CLINICAL: a presenting
    complaint, diagnosis, triage, allergy, history, medicine, observation,
    note, isolation precaution or code status in the body is a 422 (extra
    fields are refused), so a clerk can never type an allergy, or 'none'."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., max_length=200)
    age: int | None = Field(None, ge=0, le=130)
    date_of_birth: str | None = Field(None, max_length=40)
    sex: str | None = Field(None, max_length=10)
    phone: str | None = Field(None, max_length=40)
    emergency_contact_name: str | None = Field(None, max_length=200)
    emergency_contact_phone: str | None = Field(None, max_length=40)
    address: str | None = Field(None, max_length=500)
    #: The hospital's own number, typed from its register. Required unless
    #: the site's registration issues the number (ORB_UHID_SOURCE 'orb').
    mrn: str | None = Field(None, max_length=80)

    admission_source: str = Field(..., max_length=40)
    admission_type: str = Field("inpatient", max_length=40)
    #: Optional; when sent it must be the clerk's own department.
    department: str | None = Field(None, max_length=120)
    ward: str = Field("", max_length=120)
    bed: str = Field("", max_length=120)
    attending_doctor_id: str = Field(..., max_length=80)
    referring_clinician: str | None = Field(None, max_length=200)

    # The ADT placement fields, as on /admit.
    ward_id: str | None = None
    bed_id: str | None = None
    awaiting_bed: bool | None = None
    admitted_at: str | None = None
    out_of_service_reason: str | None = None
    boarder_acknowledged: bool | None = None
    extra_bed: bool | None = None
    not_clean_reason: str | None = None
    terminal_clean_authorised_by: str | None = None
    terminal_clean_reason: str | None = None
    terminal_clean_authoriser_pin: str | None = None

    #: The patient index (opd-5b), read only while ORB_REGISTRATION is on.
    duplicate_review: dict[str, Any] | None = None


def _site_issues_the_number() -> bool:
    """True when this site's registration issues the hospital number itself
    (ORB_REGISTRATION on with ORB_UHID_SOURCE 'orb'): a typed one is refused."""
    import config

    return bool(config.feature_enabled("registration")) and config.UHID_SOURCE == "orb"


_CLERK_ADMISSION_OPTIONS_ADT_DEFAULT = Depends(require_adt_module)
_CLERK_ADMISSION_OPTIONS_USER_DEFAULT = Depends(require_ward_clerk)


@router.get("/clerk-options")
def clerk_admission_options(
    _adt: None = _CLERK_ADMISSION_OPTIONS_ADT_DEFAULT,
    user: dict = _CLERK_ADMISSION_OPTIONS_USER_DEFAULT,
):
    """What the ward clerk's admission form chooses from: the clerk's own
    department, its attending doctors, and bed setup's wards and beds with
    their status. No patient data, so no PHI read is logged."""
    return clerk_admission_choices(user)


_CLERK_ADMIT_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")
_CLERK_ADMIT_ADT_DEFAULT = Depends(require_adt_module)
_CLERK_ADMIT_USER_DEFAULT = Depends(require_ward_clerk)


@router.post("/clerk-admit")
def clerk_admit(
    payload: ClerkAdmissionRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = _CLERK_ADMIT_IDEMPOTENCY_KEY_DEFAULT,
    _adt: None = _CLERK_ADMIT_ADT_DEFAULT,
    user: dict = _CLERK_ADMIT_USER_DEFAULT,
):
    """The ward clerk's registration-level admission: 201 for a new
    admission, 200 for a replay of the same Idempotency-Key (required). Into
    the clerk's own department only (403 department_not_own otherwise), with
    an attending doctor of that department (400), the hospital's own number
    (422 hospital_number_required unless the site issues it) and a place from
    the bed master under the same rules as every admission. Every clinical
    field is stored as not yet assessed; the answer says so
    (clerk_admission, assessment_due). Refusals answer {error, message, ...}
    with nothing written."""
    if not (idempotency_key or "").strip():
        raise HTTPException(
            status_code=422,
            detail={
                "error": "idempotency_key_required",
                "message": "This admission needs an Idempotency-Key header, so a retried request "
                "never admits the patient twice.",
            },
        )
    own = department_scope(user)
    try:
        department = resolve_admission_department(own or "")
        if payload.department and payload.department.strip().casefold() != department.casefold():
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "department_not_own",
                    "message": f"A ward clerk admits only to their own department, {department}.",
                },
            )
        attending = clerk_attending(payload.attending_doctor_id, department)
        mrn = (payload.mrn or "").strip()
        if not mrn and not _site_issues_the_number():
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "hospital_number_required",
                    "message": "Type the patient's hospital number from the hospital's register.",
                },
            )
        data = payload.model_dump()
        data.update(
            department=department,
            attending_doctor_id=attending,
            ward=payload.ward.strip(),
            bed=payload.bed.strip(),
            mrn=mrn or None,
        )
        result = admit_patient(
            data,
            user,
            idempotency_key=idempotency_key,
            request_meta=_request_meta(request),
            clerk_entry=True,
        )
    except PatientNameError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AdmissionInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AdmissionRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail()) from e
    except AdtError as e:
        raise HTTPException(status_code=e.http_status, detail=http_detail(e)) from e
    except AuditWriteError:
        raise _audit_unavailable("admission") from None
    response.status_code = 200 if result.get("replayed") else 201
    if not result.get("replayed") and result.get("movement_id"):
        from services.adt import transfer as adt_transfer

        adt_transfer.sync_housekeeping_after_commit()
        notify_adt_changed(answer=result, departments=[department])
    return result


class AdmissionAssessmentIn(BaseModel):
    """A doctor's admission assessment of a ward clerk's admission."""

    model_config = ConfigDict(extra="forbid")

    #: allergies_recorded, none_known or unable_to_obtain.
    allergy_status: str = Field(..., max_length=40)
    #: The allergies the patient named (allergies_recorded only).
    allergies: list[str] | None = None
    #: Why the allergies could not be obtained (unable_to_obtain only).
    reason: str | None = Field(None, max_length=500)
    chief_complaint: str = Field(..., max_length=500)
    initial_diagnosis: str | None = Field(None, max_length=500)
    #: full_code, dnr, dni or comfort.
    code_status: str = Field(..., max_length=20)
    note: str | None = Field(None, max_length=1000)


_RECORD_ADMISSION_ASSESSMENT_ADT_DEFAULT = Depends(require_adt_module)
_RECORD_ADMISSION_ASSESSMENT_USER_DEFAULT = Depends(require_permission("patient.write"))


@router.post("/{admission_id}/assessment")
def record_admission_assessment(
    admission_id: str,
    body: AdmissionAssessmentIn,
    request: Request,
    _adt: None = _RECORD_ADMISSION_ASSESSMENT_ADT_DEFAULT,
    user: dict = _RECORD_ADMISSION_ASSESSMENT_USER_DEFAULT,
):
    """A doctor completes a ward clerk's admission: the allergy review (what
    the patient said, or why they could not be asked), the presenting
    complaint, the diagnosis and the code status, in one transaction with a
    fail-closed clinical_access row. The bed board's 'Admission assessment
    due' then clears. The patient must be in the doctor's department (404
    otherwise, audited). 409 for an admission a clerk did not enter, one
    already assessed, or one that has ended."""
    _scoped_admission(admission_id, user, request)
    try:
        result = adt_clerk.record_admission_assessment(
            admission_id,
            actor=user,
            allergy_status=body.allergy_status,
            allergies=body.allergies,
            reason=body.reason,
            chief_complaint=body.chief_complaint,
            initial_diagnosis=body.initial_diagnosis,
            code_status=body.code_status,
            note=body.note,
            request_meta=_request_meta(request),
        )
    except AdtError as e:
        raise HTTPException(status_code=e.http_status, detail=http_detail(e)) from e
    except AuditWriteError:
        raise _audit_unavailable("assessment") from None
    # adt-19: the board's 'Admission assessment due' marker cleared.
    notify_adt_changed(admission_ids=[admission_id])
    return result
