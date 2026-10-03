"""Registering a bed never creates a phantom ward from a case or spacing slip.

'gm-1' typed beside a ward the hospital calls 'GM-1' used to become a second
ward: its own readiness row, its own beds, nothing to do with the real one.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import db_session  # noqa: E402
from services import housekeeping_service as hk  # noqa: E402


def _patient_on(ward: str) -> None:
    pid = f"HKC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status) "
            "VALUES (?, ?, ?, '1', ?, 50, 'F', 'HK fixture', 'admitted')",
            (pid, f"HK Canon {pid[-4:]}", f"MRN-{pid}", ward),
        )


def _code() -> str:
    return f"HKC-{uuid.uuid4().hex[:8].upper()}"


def test_a_lower_case_ward_is_stored_as_the_hospitals_ward():
    ward = f"HKC-{uuid.uuid4().hex[:5].upper()}"
    _patient_on(ward)
    unit = hk.register_unit(unit_code=_code(), ward=f"  {ward.lower()} ")
    assert unit["ward"] == ward


def test_a_second_bed_typed_differently_lands_on_the_same_ward():
    ward = f"HKC Bay {uuid.uuid4().hex[:5].upper()}"
    first = hk.register_unit(unit_code=_code(), ward=ward)
    second = hk.register_unit(unit_code=_code(), ward=f"{ward.lower()}  ")
    assert first["ward"] == second["ward"] == ward
    rows = [w for w in hk.ward_readiness()["wards"] if w["ward"].upper() == ward.upper()]
    assert len(rows) == 1 and rows[0]["registered_units"] == 2


def test_a_new_ward_is_kept_as_typed_with_its_spacing_tidied():
    unit = hk.register_unit(unit_code=_code(), ward="  New   Annexe  ")
    assert unit["ward"] == "New Annexe"
