from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
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

@router.get("/options")
def admission_options(user: dict = Depends(require_permission("patient.write"))):
    """What the admission form chooses from: covered departments, attending
    doctors, and ward and bed labels with whether each bed is held.

    Served here rather than from the front desk's transfer destinations, which
    sit behind the front desk module switch and patient.read_all; an admission
    must not depend on either. No patient data, so no PHI read is logged.
    """
    return admission_choices(user.get("department"))


@router.post("/admit")
def process_admission(
    payload: AdmissionRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_permission("patient.write"))
):
    """
    Process a new patient admission.
    Transactional across all required Orb modules.
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
        result = admit_patient(data, user)
        return result
    except PatientNameError as e:
        # The registration name rule: the clinician typed something that is
        # not a name. Their mistake to correct, so 400 with the rule's words,
        # not the generic 500 below.
        raise HTTPException(status_code=400, detail=str(e))
    except AdmissionInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Admission failed: {str(e)}")
