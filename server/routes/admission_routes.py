from fastapi import APIRouter, Depends, Header, HTTPException, BackgroundTasks, Request, Response
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from auth import get_current_user
from permissions import require_permission
from services.admission_service import (
    AdmissionInputError,
    admission_choices,
    admit_patient,
    require_listed_attending,
    resolve_admission_department,
)
from services.adt.core import AdtError, http_detail
from services.audit_service import AuditWriteError
from services.patient_name_rule import PatientNameError

router = APIRouter(prefix="/api/admissions", tags=["Admissions"])

class AdmissionRequest(BaseModel):
    # Patient Identity
    name: str
    # Bounded so a slip (-4, 440) is refused at the door (422), not charted.
    age: Optional[int] = Field(None, ge=0, le=130)
    # OPTIONAL, and it must stay optional: an admission is never blocked on
    # paperwork. It is the second factor on the patient's discharge link, so a
    # patient admitted without one gets a stated refusal at the discharge desk
    # instead of a QR code that cannot be opened (services/admission_service.
    # _record_dob, services/discharge_access_service).
    date_of_birth: Optional[str] = None
    sex: Optional[str] = None
    phone: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    address: Optional[str] = None
    mrn: Optional[str] = None
    
    # Admission Context
    admission_source: str
    admission_type: str
    department: str
    ward: str
    bed: str
    attending_doctor_id: str
    referring_clinician: Optional[str] = None
    
    # Clinical Presentation
    chief_complaint: str
    initial_diagnosis: Optional[str] = None
    triage_severity: Optional[int] = None
    
    # Clinical History
    comorbidities: Optional[List[str]] = []
    past_medical_history: Optional[str] = None
    past_surgical_history: Optional[str] = None
    allergies: Optional[List[str]] = []
    current_medications: Optional[List[str]] = []
    family_history: Optional[str] = None
    social_history: Optional[str] = None
    
    # Initial Vitals
    vitals: Optional[Dict[str, Any]] = None
    
    # Notes
    admitting_notes: Optional[str] = None
    nursing_notes: Optional[str] = None
    isolation_precautions: Optional[List[str]] = []
    code_status: Optional[str] = "full_code"

    # Beds and admissions (adt-5). Read ONLY while ORB_ADT is on; ignored while
    # it is off. Every one is optional and ward and bed keep their str types,
    # so a body without them is exactly what it always was, and a body missing
    # ward or bed is still a 422 whatever the module says.
    #: A ward and a bed from bed setup, as the bed picker chose them.
    ward_id: Optional[str] = None
    bed_id: Optional[str] = None
    #: 'No bed yet': admitted to the ward (or nowhere yet), awaiting a bed.
    awaiting_bed: Optional[bool] = None
    #: When the stay began, up to ORB_ADT_BACKTIME_HOURS back; now when omitted.
    admitted_at: Optional[str] = None
    #: Typed reason for a bed housekeeping records out of service.
    out_of_service_reason: Optional[str] = None
    #: The admitting clinician has read that host-ward nurses will not open a
    #: boarder's chart (ruling ADT-S6 is not applied).
    boarder_acknowledged: Optional[bool] = None
    #: Ruling ADT-S1 (a): bed_id is occupied, so register a temporary extra bed
    #: beside it (its own label) and admit the patient there.
    extra_bed: Optional[bool] = None
    #: Ruling ADT-S2 (c): typed reason for a bed not yet recorded clean.
    not_clean_reason: Optional[str] = None
    #: Ruling ADT-S2 (c): who authorises a bed awaiting a terminal clean, and
    #: why. The sender (their own id), or another doctor, nurse or
    #: administrator who confirms it with their own signing PIN, typed here;
    #: the PIN is checked and dropped, never stored. A name alone is refused.
    terminal_clean_authorised_by: Optional[str] = None
    terminal_clean_reason: Optional[str] = None
    terminal_clean_authoriser_pin: Optional[str] = None

@router.get("/options")
def admission_options(user: dict = Depends(require_permission("patient.write"))):
    """What the admission form chooses from: covered departments, attending
    doctors, and ward and bed labels with whether each bed is held.

    Served here rather than from the front desk's transfer destinations, which
    sit behind the front desk module switch and patient.read_all; an admission
    must not depend on either. No patient data, so no PHI read is logged.
    """
    return admission_choices(user.get("department"), user=user)


@router.post("/admit")
def process_admission(
    payload: AdmissionRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_permission("patient.write"))
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
            data, user, idempotency_key=idempotency_key,
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
        return result
    except PatientNameError as e:
        # The registration name rule: the clinician typed something that is
        # not a name. Their mistake to correct, so 400 with the rule's words,
        # not the generic 500 below.
        raise HTTPException(status_code=400, detail=str(e))
    except AdmissionInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AdtError as e:
        # Only with ORB_ADT on: the bed master refused the place, in words,
        # and nothing was written.
        raise HTTPException(status_code=e.http_status, detail=http_detail(e))
    except AuditWriteError:
        # The patient_movement audit is fail-closed: an admission whose record
        # could not be written is not made at all.
        raise HTTPException(status_code=503, detail={
            "error": "audit_unavailable",
            "message": "The record of this admission could not be written, so the patient "
                       "was not admitted. Try again in a moment.",
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Admission failed: {str(e)}")
