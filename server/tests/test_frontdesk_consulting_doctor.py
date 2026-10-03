"""An OPD visit is booked with a consulting doctor OF THAT CLINIC (F151).

register_opd_visit checked only that `doctor_id` existed in `users`, so any id
arriving in the request was accepted: a nurse, a pharmacist, another clinic's
doctor, a doctor who had left. That name was then printed on the token and the
queue as the consultant. The rule is now the one list_consulting_doctors already
applies to the dropdown: an active doctor or clinician whose department is the
clinic the visit is for.
"""
import uuid

import pytest

from database import db_session
from services import frontdesk_service as fd

DEPT = "FDC Consulting Dermatology"
OTHER = "FDC Consulting Neurology"


def _user(role: str, department: str | None, *, active: int = 1) -> str:
    uid = f"FDC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, ?)",
            (uid, f"FDC {role}", role, department, active),
        )
    return uid


def _outpatient() -> str:
    pid = f"FDCP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status) "
            "VALUES (?, 'FDC Outpatient', ?, 40, 'F', 'discharged')",
            (pid, f"MRN-{pid}"),
        )
    return pid


@pytest.fixture(scope="module")
def clinic_doctor() -> str:
    # Also gives both departments clinical cover, so they are real clinics.
    _user("doctor", OTHER)
    return _user("doctor", DEPT)


def _book(doctor_id: str) -> dict:
    return fd.register_opd_visit(patient_id=_outpatient(), department=DEPT, doctor_id=doctor_id)


def test_a_doctor_of_the_clinic_is_booked(clinic_doctor):
    visit = _book(clinic_doctor)
    assert visit["doctor_id"] == clinic_doctor
    assert visit["doctor_name"] == "FDC doctor"


@pytest.mark.parametrize("who", ["nurse", "other_clinic_doctor", "inactive_doctor", "unknown"])
def test_anyone_else_is_refused(clinic_doctor, who):
    doctor_id = {
        "nurse": lambda: _user("nurse", DEPT),
        "other_clinic_doctor": lambda: _user("doctor", OTHER),
        "inactive_doctor": lambda: _user("doctor", DEPT, active=0),
        "unknown": lambda: "FDC-NOBODY",
    }[who]()
    with pytest.raises(fd.FrontDeskError) as exc:
        _book(doctor_id)
    assert DEPT in str(exc.value)


def test_the_department_match_ignores_case_and_spacing(clinic_doctor):
    visit = fd.register_opd_visit(
        patient_id=_outpatient(), department=f"  {DEPT.lower()} ", doctor_id=clinic_doctor,
    )
    assert visit["doctor_id"] == clinic_doctor
