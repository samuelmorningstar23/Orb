"""Outpatients: the OPD record view (opd-7, W3-A, 4 October 2026).

What a clinic's doctor sees about a patient their clinic has seen recently,
composed for GET /api/opd/patients/{patient_id}/record. Later OPD increments
(opd-10 consultations, opd-10a allergy review, opd-11 investigations) add their
functions to this module.

THE EXTENT IS A RULING, NOT A DESIGN CHOICE (OPD-N2, applied 3 October 2026,
extent ii): identity, allergies, problems, OPD-linked results, and the signed
OPD consultations of EVERY clinic. Not the inpatient chart, not vitals, not
notes, not discharge summaries (extent iii stays behind the ordinary inpatient
scope), and never anything that leaves the building. The route that serves this
is the ONLY route the OPD scope arm opens (tests/test_opd_scope_drift.py), so
what this function returns is everything a self-registered visit can reach.

Identity is deliberately narrow. The date of birth is the discharge-link second
factor (discharge_access_service), and in this wave nothing yet checks the
patient's identity before a clinician attaches a visit to an existing record
(ruling OPD-S1 builds that in opd-4 and opd-6a). So the record says whether a
date of birth is on file and never what it is; phone, address and next of kin
are not here either. The registration record (opd-4) carries demographics
under its own scope.

Rules this module keeps (and test_opd_no_scoring will enforce from opd-10):
  * it never writes vitals, risk_scores or medications, and never scores or
    bands anything: no NEWS2, no colour, no AI advice;
  * plain sync functions, each opening its own db_session; async callers go
    through database.run_db;
  * it reads; it never decides scope. The caller's access was decided by
    permissions.require_patient_access(..., opd_arm=True) before this runs.
"""
from __future__ import annotations

from typing import Optional

from database import db_session

__all__ = [
    "RECORD_RESULT_LIMIT",
    "RECORD_VISIT_LIMIT",
    "get_opd_record",
]

#: How many of the patient's OPD visits the record lists, newest first.
RECORD_VISIT_LIMIT = 25
#: How many OPD-linked investigations the record lists, newest first.
RECORD_RESULT_LIMIT = 50

#: Order statuses that never reach the record: an unsigned draft is not an
#: investigation anybody asked for, and a cancelled one never happened.
_HIDDEN_ORDER_STATUSES = ("draft", "cancelled")


def _same_clinic(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _table_exists(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _identity(db, patient) -> dict:
    """Name, UHID, age, sex and the record's state. No date of birth, phone,
    address or next of kin (see the module docstring)."""
    pid = patient["id"]
    inpatient_now = db.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
        (pid,),
    ).fetchone() is not None
    demo = None
    if _table_exists(db, "patient_demographics"):
        demo = db.execute(
            "SELECT age_recorded_on, preferred_language FROM patient_demographics "
            "WHERE patient_id = ?",
            (pid,),
        ).fetchone()
    return {
        "id": pid,
        "name": patient["name"],
        "uhid": patient["mrn"],
        "age": patient["age"],
        "age_recorded_on": demo["age_recorded_on"] if demo else None,
        "sex": patient["sex"],
        "preferred_language": demo["preferred_language"] if demo else None,
        "record_status": patient["status"],
        "inpatient_now": inpatient_now,
        "date_of_birth_on_file": bool((patient["date_of_birth"] or "").strip()),
    }


def _allergies(db, patient_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT substance, reaction, severity, source, recorded_at "
        "  FROM patient_allergies WHERE patient_id = ? AND status = 'active' "
        " ORDER BY recorded_at DESC, id DESC",
        (patient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _problems(db, patient_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT problem, icd10_code, onset_date, recorded_at "
        "  FROM problem_list WHERE patient_id = ? AND status = 'active' "
        " ORDER BY recorded_at DESC, id DESC",
        (patient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _visits(db, patient_id: str, viewer_department: Optional[str]) -> list[dict]:
    """The patient's OPD visits, every clinic, cancelled ones left out.

    Which clinic saw the patient and when is part of extent ii (every clinic's
    signed consultation names both). The chief complaint is the desk's free
    text, not a signed consultation, so it is shown only for the viewer's own
    clinic (or to an account with no department).
    """
    rows = db.execute(
        "SELECT id, department, doctor_name, visit_date, visit_type, status, "
        "       chief_complaint "
        "  FROM opd_visits WHERE patient_id = ? AND status != 'cancelled' "
        " ORDER BY visit_date DESC, registered_at DESC, id DESC LIMIT ?",
        (patient_id, RECORD_VISIT_LIMIT),
    ).fetchall()
    visits = []
    for r in rows:
        own = viewer_department is None or _same_clinic(r["department"], viewer_department)
        visits.append({
            "id": r["id"],
            "department": r["department"],
            "doctor_name": r["doctor_name"],
            "visit_date": r["visit_date"],
            "visit_type": r["visit_type"],
            "status": r["status"],
            "chief_complaint": r["chief_complaint"] if own else None,
            "own_clinic": own,
        })
    return visits


def _opd_results(db, patient_id: str) -> list[dict]:
    """Investigations ordered from an OPD visit (orders.opd_visit_id, migration
    50), with their result when one has come back. Inpatient orders are not
    here: they belong to the chart."""
    marks = ",".join("?" for _ in _HIDDEN_ORDER_STATUSES)
    rows = db.execute(
        f"""SELECT o.id, o.order_type, o.description, o.status, o.result_text,
                   o.result_at, o.created_at, o.opd_visit_id,
                   v.department AS visit_department, v.visit_date
              FROM orders o
              LEFT JOIN opd_visits v ON v.id = o.opd_visit_id
             WHERE o.patient_id = ? AND o.opd_visit_id IS NOT NULL
               AND o.status NOT IN ({marks})
             ORDER BY COALESCE(o.result_at, o.created_at) DESC, o.id DESC
             LIMIT ?""",
        (patient_id, *_HIDDEN_ORDER_STATUSES, RECORD_RESULT_LIMIT),
    ).fetchall()
    return [dict(r) for r in rows]


def get_opd_record(
    patient_id: str,
    *,
    viewer_department: Optional[str],
    access: Optional[dict] = None,
) -> Optional[dict]:
    """The OPD record view for one patient, or None when there is no such patient.

    `viewer_department` is permissions.department_scope(user), never a value
    the request named. `access` is the scope decision the dependency left on
    request.state (which arm let the reader in, and until when); it is echoed
    so the screen can say why the record is open.

    Allergy status: Orb cannot tell "no known allergies" from "never asked"
    (services/allergy_interlock.py), so an empty list is reported as
    'none_recorded', never as no known allergies. The allergy review that
    records 'none known' is opd-10a (ruling OPD-S7).

    Consultations: the signed OPD consultations of every clinic belong here
    (OPD-N2 extent ii). The consultation record is opd-10 and does not exist
    yet, so the list is empty and `consultations_available` is False; opd-10
    fills it in this function.
    """
    with db_session() as db:
        patient = db.execute(
            "SELECT id, name, mrn, age, sex, status, date_of_birth FROM patients WHERE id = ?",
            (patient_id,),
        ).fetchone()
        if patient is None:
            return None
        allergies = _allergies(db, patient_id)
        record = {
            "patient": _identity(db, patient),
            "access": dict(access or {}),
            "allergies": allergies,
            "allergy_status": "recorded" if allergies else "none_recorded",
            "problems": _problems(db, patient_id),
            "opd_visits": _visits(db, patient_id, viewer_department),
            "results": _opd_results(db, patient_id),
            "consultations": [],
            "consultations_available": False,
        }
    return record
