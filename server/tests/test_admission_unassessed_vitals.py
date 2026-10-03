"""The admit form's untouched ACVPU and O2 reach the chart as NOT ASSESSED.

The form used to start both at Alert and room air, so an admission nobody
assessed was stored as assessed and NEWS2 scored the set as complete. It now
sends null for each (src/components/AdmitPatientModal.acvpu.test.tsx). This pins
the other half: that payload, through the real admission service, stores NULL in
both columns and scores as an incomplete set. Ruled 2026-10-03.
"""
import pytest

from database import db_session
from services.admission_service import admit_patient
from services.vigil_service import score_vitals_row

_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """Close both rows each admission opens (see test_admission_provenance)."""
    yield
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'", [(p,) for p in _SEEDED])
    _SEEDED.clear()


def _clinician():
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            ("UNASSESSED-DOC", "Dr Unassessed", "doctor", hash_password("x")),
        )
    return {"id": "UNASSESSED-DOC", "name": "Dr Unassessed", "role": "doctor"}


def _admitted_row(vitals):
    res = admit_patient({"name": "Unassessed Vitals", "ward": "ICU", "vitals": vitals},
                        _clinician())
    _SEEDED.append(res["patient_id"])
    with db_session() as db:
        return dict(db.execute(
            "SELECT * FROM vitals WHERE patient_id = ? ORDER BY id ASC LIMIT 1",
            (res["patient_id"],),
        ).fetchone())


def test_the_untouched_form_payload_stores_not_assessed():
    """Exactly what the form now sends when neither select was touched."""
    row = _admitted_row({"hr": 96, "bp_sys": 120, "spo2": 97, "temperature": 37.0, "rr": 16,
                         "consciousness": None, "on_oxygen": None})
    assert row["consciousness"] is None
    assert row["on_oxygen"] is None

    news2 = score_vitals_row(row, on_scale_2=False, overlay=False)
    assert news2["complete"] is False
    assert news2["missing_parameters"] == ["Consciousness (ACVPU)", "Supplemental O2"]


def test_a_recorded_room_air_and_alert_are_stored_as_recorded():
    row = _admitted_row({"hr": 96, "bp_sys": 120, "spo2": 97, "temperature": 37.0, "rr": 16,
                         "consciousness": "alert", "on_oxygen": False})
    assert row["consciousness"] == "alert"
    assert row["on_oxygen"] == 0
    assert score_vitals_row(row, on_scale_2=False, overlay=False)["complete"] is True
