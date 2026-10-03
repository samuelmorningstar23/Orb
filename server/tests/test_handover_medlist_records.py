"""What the handover and the printed medication list say (F055, F057, F073).

  * An empty allergy list is "none recorded", never NKDA: NKDA is a clinician's
    assertion and an empty list is also every patient nobody asked.
  * A saved handover is read back with its author's name, newest first, so the
    incoming nurse sees who handed over and what they wrote.
  * An AI-extracted order nobody has verified is labelled as such on the
    handover, and a pharmacist's rejection names the pharmacist.
"""
import uuid
from datetime import datetime, timezone

import pytest

from database import db_session
from services import ipass_service, pharmacy_verification_service
from services.ipass_service import (
    NO_ALLERGIES_RECORDED,
    UNVERIFIED_MED_LABEL,
    build_ipass,
    get_recent_handoffs,
    save_ipass_handoff,
)


@pytest.fixture()
def patient():
    pid = f"PATHO{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status, age, sex) "
            "VALUES (?, ?, ?, '4A', 'GM', 'Pneumonia', 'admitted', 64, 'M')",
            (pid, f"Handover {pid}", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                                   spo2, temperature, respiratory_rate, consciousness)
               VALUES (?, ?, 80, 124, 76, 97, 37.0, 16, 'alert')""",
            (pid, datetime.now(timezone.utc).isoformat()),
        )
    yield pid
    with db_session() as db:
        db.execute("DELETE FROM scribe_notes WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _user(role: str, name: str) -> str:
    uid = f"HO{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
                   (uid, name, role))
    return uid


# ── F055 ─────────────────────────────────────────────────────────────────────
def test_an_empty_allergy_list_is_none_recorded_never_nkda(patient):
    draft = build_ipass(patient)
    summary = draft["sections"]["patient_summary"]
    assert summary["allergies"] == []
    assert f"Allergies: {NO_ALLERGIES_RECORDED}" in summary["text"]
    assert "NKDA" not in summary["text"]
    assert "NKDA" not in draft["plain_text"]


def test_a_charted_allergy_is_still_named(patient):
    with db_session() as db:
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status) "
            "VALUES (?, 'penicillin', 'rash', 'moderate', 'active')", (patient,))
    try:
        summary = build_ipass(patient)["sections"]["patient_summary"]
        assert any("penicillin" in a.lower() for a in summary["allergies"])
        assert NO_ALLERGIES_RECORDED not in summary["text"]
    finally:
        with db_session() as db:
            db.execute("DELETE FROM patient_allergies WHERE patient_id = ?", (patient,))


# ── F057 ─────────────────────────────────────────────────────────────────────
def test_recent_handoffs_carry_the_authors_name_newest_first(patient):
    first = _user("nurse", "S. Nair")
    second = _user("nurse", "R. Iyer")
    save_ipass_handoff(patient, first, "I-PASS from the night nurse")
    save_ipass_handoff(patient, second, "I-PASS from the day nurse")
    rows = get_recent_handoffs(patient)
    assert [r["author_name"] for r in rows[:2]] == ["R. Iyer", "S. Nair"], (
        "same-second saves must still read newest first")
    assert rows[0]["author_id"] == second


# ── F073 ─────────────────────────────────────────────────────────────────────
def test_an_unverified_ai_order_is_labelled_on_the_handover(patient):
    with db_session() as db:
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Vancomycin', '1 g', 'IV', 'BD', 'active', 'llm_extracted')""", (patient,))
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Paracetamol', '1 g', 'PO', 'QDS', 'active', 'clinician')""", (patient,))
    draft = build_ipass(patient)
    meds = {m["drug_name"]: m for m in draft["sections"]["action_list"]["active_medications"]}
    assert meds["Vancomycin"]["unverified"] is True
    assert meds["Paracetamol"]["unverified"] is False
    line = next(l for l in draft["plain_text"].splitlines() if "Vancomycin" in l)
    assert UNVERIFIED_MED_LABEL in line
    assert UNVERIFIED_MED_LABEL not in next(l for l in draft["plain_text"].splitlines() if "Paracetamol" in l)


def test_a_rejection_stamp_names_the_pharmacist(patient):
    pharmacist = _user("pharmacist", "A. Fernandes")
    with db_session() as db:
        med_id = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Enoxaparin', 'REVIEW REQUIRED', 'Review', 'Review', 'active', 'llm_extracted')""",
            (patient,)).lastrowid
    out = pharmacy_verification_service.reject_medication(med_id, pharmacist, "Duplicate of the charted heparin")
    assert "[REJECTED by A. Fernandes @ " in out["medication"]["notes"]
    assert pharmacist not in out["medication"]["notes"]
