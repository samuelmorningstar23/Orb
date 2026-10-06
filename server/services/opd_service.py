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

Rules this module keeps (tests/test_opd_no_scoring.py enforces the first):
  * it never writes vitals, risk_scores or medications, and never scores or
    bands anything: no NEWS2, no colour, no AI advice;
  * plain sync functions, each opening its own db_session; async callers go
    through database.run_db;
  * it never decides scope. The caller's access was decided by
    permissions.require_patient_access(..., opd_arm=True), or for a
    visit-addressed consultation write by the route's department check,
    before this runs. Its only writes are the consultation note, its addenda
    and, at signing, the problem-list rows the doctor ticked (opd-10).
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from database import db_session
from services.clinical_time import parse_to_utc, to_utc_iso, utc_iso_now

__all__ = [
    "RECORD_RESULT_LIMIT",
    "RECORD_VISIT_LIMIT",
    "OPD_VITAL_FIELDS",
    "ConsultationError",
    "add_addendum",
    "allergy_review_status",
    "consultation_signed",
    "get_consultation",
    "get_opd_record",
    "list_patient_consultations",
    "normalise_diagnoses",
    "save_consultation",
    "sign_consultation",
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

    Consultations: the signed OPD consultations of every clinic (OPD-N2
    extent ii), newest first, as summaries: clinic, doctor, date, diagnoses,
    plan and advice. Never the vitals typed into them (opd-10), and never a
    draft, which is not yet a record. `consultations_available` is False only
    on a database without migration 62.
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
        if _table_exists(db, "opd_consultations"):
            record["consultations"] = _signed_summaries(db, patient_id)
            record["consultations_available"] = True
    return record


# ═════════════════════════════════════════════════════════════════════════════
#  The consultation record (opd-10, W4-F, 4 October 2026)
# ═════════════════════════════════════════════════════════════════════════════
# One note per OPD visit (opd_consultations, migration 62): a draft saved with
# row_version, then signed, after which it never changes and corrections are
# append-only addenda (opd_consultation_addenda). The database refuses an
# UPDATE of a signed note and of any addendum.
#
# OPD VITALS ARE NOT VITALS. What the consulting doctor measures is kept inside
# the note, in vitals_json with the time it was measured and by whom. It is
# never written to vitals or risk_scores, never scored, banded or coloured, and
# never becomes anyone's latest vitals (rulings OPD-S3 and OPD-S6, applied;
# tests/test_opd_no_scoring.py fails this module if it imports a scoring
# function or writes either table).
#
# DIAGNOSES are free text, with an optional code (ruling OPD-N11): a code found
# active in `terminology` is stored 'validated', any other code is kept exactly
# as typed and stored 'unvalidated', never refused; no code is 'none'. At
# signing, a validated diagnosis the doctor ticked is added to the problem
# list.
#
# SIGNING NEEDS AN ALLERGY REVIEW (ruling OPD-S7, strictest option, applied).
# Until opd-10a records 'no known allergies' and 'unable to obtain' as an
# assessment, the only review Orb can see is an allergy charted on the
# patient. So a note is signed only when the patient has at least one active
# charted allergy; otherwise signing is refused with 'allergy_review_required'
# and the visit can still be completed with a reason (ruling OPD-N8). This is
# listed for Samuel with the other safety defaults.
#
# Writes here are visit-scoped and refused once the patient has an active
# admission anywhere (the inpatient chart owns them then). Every function opens
# its own session; writes take db_session(immediate=True); `on_done(db, ...)`
# runs inside the transaction so the route's audit row commits with the write.

#: Free-text fields a draft carries, with their caps.
_TEXT_FIELDS: dict[str, int] = {
    "chief_complaint": 2000,
    "history": 8000,
    "examination": 8000,
    "plan": 8000,
    "advice": 4000,
    "follow_up_note": 2000,
    "referral_department": 120,
}

#: OPD vitals a consulting doctor may type, with the plausible range a value
#: must fall in (a typo guard, not a clinical judgement: nothing here is scored
#: or banded). Temperature is in degrees Celsius.
OPD_VITAL_FIELDS: dict[str, tuple[float, float]] = {
    "heart_rate": (1, 350),
    "systolic_bp": (1, 350),
    "diastolic_bp": (1, 250),
    "respiratory_rate": (1, 100),
    "spo2": (1, 100),
    "temperature": (25, 46),
    "weight_kg": (0.2, 500),
    "height_cm": (10, 260),
}

_MAX_DIAGNOSES = 20
_MAX_DIAGNOSIS_TEXT = 500
_MAX_ADDENDUM = 8000
_CODE_SYSTEMS = ("ICD10", "SNOMED")
#: How far ahead of the server clock a measured time may be (a device clock).
_MEASURED_AT_SKEW = timedelta(minutes=5)


class ConsultationError(ValueError):
    """A consultation request that cannot be honoured. `status` is the HTTP
    status the route answers with; `code` is detail.error."""

    def __init__(self, code: str, message: str, status: int = 422, **extra: Any):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


def _new_consultation_id() -> str:
    return f"OPC-{uuid.uuid4().hex[:8].upper()}"


def _user_names(db, ids) -> dict:
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    rows = db.execute(f"SELECT id, name FROM users WHERE id IN ({marks})", wanted).fetchall()
    return {r["id"]: r["name"] for r in rows}


def _loads(text: Optional[str], default):
    if not text:
        return default
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _clean_text(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def normalise_diagnoses(raw: Any) -> list[dict]:
    """Diagnoses as stored: [{text, code_system, code, code_status,
    add_to_problems}]. Free text is required on every item; a code is
    optional and checked with terminology_service.validate_code. An unknown
    code is kept as typed with code_status 'unvalidated', never refused.

    Opens its own read session (validate_code does), so call it BEFORE a
    write transaction begins.
    """
    from services.terminology_service import validate_code

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConsultationError("bad_diagnoses", "Diagnoses must be a list")
    if len(raw) > _MAX_DIAGNOSES:
        raise ConsultationError("bad_diagnoses", f"At most {_MAX_DIAGNOSES} diagnoses")
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise ConsultationError("bad_diagnoses", "Each diagnosis needs its text")
        text = _clean_text(item.get("text"), _MAX_DIAGNOSIS_TEXT)
        if not text:
            raise ConsultationError("bad_diagnoses", "Each diagnosis needs its text")
        code = _clean_text(item.get("code"), 40)
        system = (_clean_text(item.get("code_system"), 20) or "").upper() or None
        if code and not system:
            system = "ICD10"
        if code and system not in _CODE_SYSTEMS:
            raise ConsultationError("bad_diagnoses",
                                    f"Code system must be one of {', '.join(_CODE_SYSTEMS)}")
        if code:
            status = "validated" if validate_code(system, code) else "unvalidated"
        else:
            system, status = None, "none"
        out.append({
            "text": text,
            "code_system": system,
            "code": code,
            "code_status": status,
            "add_to_problems": bool(item.get("add_to_problems")) and status == "validated",
        })
    return out


def _normalise_vitals(raw: Any, *, actor_id: str) -> Optional[dict]:
    """OPD vitals as stored: {values, measured_at, measured_by}, or None.

    Plain numbers in their plausible range, with the time they were measured
    (required whenever a value is given, never in the future). Never scored.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConsultationError("bad_vitals", "Vitals must be an object")
    values_in = raw.get("values") if isinstance(raw.get("values"), dict) else {}
    values: dict[str, float] = {}
    for key, value in values_in.items():
        if key not in OPD_VITAL_FIELDS:
            raise ConsultationError("bad_vitals", f"{key} is not an outpatient vital")
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            raise ConsultationError("bad_vitals", f"{key} must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ConsultationError("bad_vitals", f"{key} must be a number") from None
        low, high = OPD_VITAL_FIELDS[key]
        if not (low <= number <= high):
            raise ConsultationError("bad_vitals", f"{key} {value} is outside {low} to {high}")
        values[key] = int(number) if number.is_integer() else number
    if not values:
        return None
    measured = raw.get("measured_at")
    if not measured:
        raise ConsultationError("bad_vitals", "Say when the vitals were measured")
    try:
        when = parse_to_utc(str(measured))
    except ValueError:
        raise ConsultationError("bad_vitals", "The measured time is not a valid time") from None
    if when > datetime.now(timezone.utc) + _MEASURED_AT_SKEW:
        raise ConsultationError("bad_vitals", "The measured time is in the future")
    return {"values": values, "measured_at": to_utc_iso(when), "measured_by": actor_id}


def _follow_up_date(value: Any) -> Optional[str]:
    text = _clean_text(value, 10)
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ConsultationError("bad_follow_up_date", "Follow-up date must be YYYY-MM-DD") from None


def _visit_row(db, visit_id: str):
    return db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()


def _consultation_row(db, visit_id: str):
    return db.execute("SELECT * FROM opd_consultations WHERE visit_id = ?",
                      (visit_id,)).fetchone()


def _active_admission(db, patient_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
        (patient_id,),
    ).fetchone() is not None


def _writable_visit(db, visit_id: str):
    """The visit a consultation write may touch, or a refusal."""
    visit = _visit_row(db, visit_id)
    if visit is None:
        raise ConsultationError("visit_not_found", "OPD visit not found", status=404)
    if visit["status"] == "cancelled":
        raise ConsultationError("visit_cancelled",
                                "This visit was cancelled; it has no consultation", status=409)
    if _active_admission(db, visit["patient_id"]):
        raise ConsultationError(
            "patient_admitted",
            "This patient is now admitted. Record it on the inpatient chart.", status=409)
    return visit


def allergy_review_status(db, patient_id: str) -> dict:
    """Whether a note for this patient may be signed (ruling OPD-S7).

    'recorded': at least one active charted allergy, so the allergies were
    asked and charted; signing is allowed. 'not_reviewed': nothing charted,
    which Orb cannot tell apart from never asked; signing is refused until
    opd-10a records 'no known allergies' or 'unable to obtain'.
    """
    charted = db.execute(
        "SELECT COUNT(*) AS n FROM patient_allergies WHERE patient_id = ? AND status = 'active'",
        (patient_id,),
    ).fetchone()["n"]
    if charted:
        return {"status": "recorded", "can_sign": True, "charted_count": int(charted)}
    return {"status": "not_reviewed", "can_sign": False, "charted_count": 0}


def _serialize(db, row, *, with_addenda: bool = True) -> Optional[dict]:
    if row is None:
        return None
    data = dict(row)
    vitals = _loads(data.pop("vitals_json", None), {}) or None
    diagnoses = _loads(data.pop("diagnoses_json", None), [])
    addenda_rows = []
    if with_addenda:
        addenda_rows = db.execute(
            "SELECT id, author_id, body, created_at FROM opd_consultation_addenda "
            "WHERE consultation_id = ? ORDER BY id",
            (data["id"],),
        ).fetchall()
    names = _user_names(db, [data.get("author_id"), data.get("signed_by"),
                             data.get("updated_by"),
                             (vitals or {}).get("measured_by"),
                             *(r["author_id"] for r in addenda_rows)])
    if vitals:
        vitals = {**vitals, "measured_by_name": names.get(vitals.get("measured_by"))}
    data.update({
        "author_name": names.get(data.get("author_id")),
        "signed_by_name": names.get(data.get("signed_by")),
        "updated_by_name": names.get(data.get("updated_by")),
        "admission_advised": bool(data.get("admission_advised")),
        "vitals": vitals,
        "diagnoses": diagnoses,
    })
    if with_addenda:
        data["addenda"] = [
            {"id": r["id"], "author_id": r["author_id"],
             "author_name": names.get(r["author_id"]), "body": r["body"],
             "created_at": r["created_at"]}
            for r in addenda_rows
        ]
    return data


def consultation_signed(db, visit_id: str) -> bool:
    """Is this visit's consultation note signed? (The OPD-N8 completion rule.)"""
    if not _table_exists(db, "opd_consultations"):
        return False
    return db.execute(
        "SELECT 1 FROM opd_consultations WHERE visit_id = ? AND status = 'signed'",
        (visit_id,),
    ).fetchone() is not None


def get_consultation(visit_id: str) -> dict:
    """The visit's note (None when not started), its addenda, and whether it
    may be signed. The caller has already proved the visit is in its clinic."""
    with db_session() as db:
        visit = _visit_row(db, visit_id)
        if visit is None:
            raise ConsultationError("visit_not_found", "OPD visit not found", status=404)
        note = _serialize(db, _consultation_row(db, visit_id))
        return {
            "visit_id": visit_id,
            "patient_id": visit["patient_id"],
            "visit_status": visit["status"],
            "consultation": note,
            "allergy_review": allergy_review_status(db, visit["patient_id"]),
            "patient_admitted": _active_admission(db, visit["patient_id"]),
        }


def save_consultation(
    visit_id: str,
    fields: dict,
    *,
    row_version: Optional[int],
    actor_id: str,
    on_done: Optional[Callable[[Any, dict, dict], None]] = None,
) -> dict:
    """Start or update the visit's draft note.

    `fields` holds only what the request named (any of the text fields,
    'vitals', 'diagnoses', 'follow_up_date', 'admission_advised'). The first
    save takes row_version None or 0; every later save must name the version
    it read, or it is refused 409 'stale_version' with the current version, so
    nobody overwrites a colleague's edit unseen. A signed note is refused 409
    'consultation_signed': add an addendum instead.
    """
    updates: dict[str, Any] = {}
    for key, limit in _TEXT_FIELDS.items():
        if key in fields:
            updates[key] = _clean_text(fields[key], limit)
    if "diagnoses" in fields:
        updates["diagnoses_json"] = json.dumps(normalise_diagnoses(fields["diagnoses"]))
    if "follow_up_date" in fields:
        updates["follow_up_date"] = _follow_up_date(fields["follow_up_date"])
    if "admission_advised" in fields:
        updates["admission_advised"] = 1 if fields["admission_advised"] else 0
    vitals_given = "vitals" in fields

    with db_session(immediate=True) as db:
        visit = _writable_visit(db, visit_id)
        if vitals_given:
            normalised = _normalise_vitals(fields["vitals"], actor_id=actor_id)
            updates["vitals_json"] = json.dumps(normalised) if normalised else None
        now = utc_iso_now()
        row = _consultation_row(db, visit_id)
        if row is None:
            if row_version not in (None, 0):
                raise ConsultationError(
                    "stale_version", "This note was not found at that version", status=409,
                    current_version=None)
            cid = _new_consultation_id()
            db.execute(
                "INSERT INTO opd_consultations (id, visit_id, patient_id, department, "
                "author_id, status, row_version, created_at, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?)",
                (cid, visit_id, visit["patient_id"], visit["department"], actor_id,
                 now, now, actor_id),
            )
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                db.execute(f"UPDATE opd_consultations SET {sets} WHERE id = ?",
                           (*updates.values(), cid))
            facts = {"created": True, "fields": sorted(updates)}
        else:
            if row["status"] == "signed":
                raise ConsultationError(
                    "consultation_signed",
                    "This note is signed and cannot change. Add an addendum.", status=409)
            if row_version != row["row_version"]:
                raise ConsultationError(
                    "stale_version",
                    "Someone saved this note after you opened it. Reload it, then save again.",
                    status=409, current_version=row["row_version"])
            updates.update({"row_version": row["row_version"] + 1, "updated_at": now,
                            "updated_by": actor_id})
            sets = ", ".join(f"{k} = ?" for k in updates)
            db.execute(f"UPDATE opd_consultations SET {sets} WHERE id = ? AND row_version = ?",
                       (*updates.values(), row["id"], row["row_version"]))
            facts = {"created": False,
                     "fields": sorted(k for k in updates
                                      if k not in ("row_version", "updated_at", "updated_by"))}
        note = _serialize(db, _consultation_row(db, visit_id))
        if on_done is not None:
            on_done(db, note, facts)
        return note


def _add_signed_problems(db, note_row, diagnoses: list[dict], *, actor_id: str,
                         now: str) -> list[int]:
    """Problem-list rows for the validated diagnoses the doctor ticked, inside
    the signing transaction. The same columns clinical_records_service.
    add_problem writes; an active problem with the same code is not added
    twice."""
    added: list[int] = []
    for d in diagnoses:
        if not d.get("add_to_problems") or d.get("code_status") != "validated":
            continue
        column = "icd10_code" if d.get("code_system") == "ICD10" else "snomed_code"
        exists = db.execute(
            f"SELECT 1 FROM problem_list WHERE patient_id = ? AND status = 'active' "
            f"AND {column} = ?",
            (note_row["patient_id"], d["code"]),
        ).fetchone()
        if exists:
            continue
        icd = d["code"] if column == "icd10_code" else None
        snomed = d["code"] if column == "snomed_code" else None
        cur = db.execute(
            "INSERT INTO problem_list (patient_id, problem, icd10_code, snomed_code, status, "
            "onset_date, source, recorded_by, recorded_at) "
            "VALUES (?, ?, ?, ?, 'active', NULL, 'clinician', ?, ?)",
            (note_row["patient_id"], d["text"], icd, snomed, actor_id, now),
        )
        added.append(int(cur.lastrowid))
    return added


def sign_consultation(
    visit_id: str,
    *,
    row_version: Optional[int],
    actor_id: str,
    on_done: Optional[Callable[[Any, dict, dict], None]] = None,
) -> dict:
    """Sign the draft. After this it never changes (the database refuses).

    Refused: no note (404 'no_consultation'); already signed (409
    'consultation_signed'); a version other than the one read (409
    'stale_version'); an empty note (422 'empty_note'); no allergy review
    (409 'allergy_review_required', ruling OPD-S7).
    """
    with db_session(immediate=True) as db:
        visit = _writable_visit(db, visit_id)
        row = _consultation_row(db, visit_id)
        if row is None:
            raise ConsultationError("no_consultation", "There is no note to sign yet",
                                    status=404)
        if row["status"] == "signed":
            raise ConsultationError("consultation_signed", "This note is already signed",
                                    status=409)
        if row_version != row["row_version"]:
            raise ConsultationError(
                "stale_version",
                "Someone saved this note after you opened it. Reload it, then sign.",
                status=409, current_version=row["row_version"])
        diagnoses = _loads(row["diagnoses_json"], [])
        if not (diagnoses or any(_clean_text(row[k], 10) for k in
                                 ("chief_complaint", "history", "examination", "plan"))):
            raise ConsultationError("empty_note",
                                    "Write the findings, a diagnosis or a plan before signing")
        review = allergy_review_status(db, visit["patient_id"])
        if not review["can_sign"]:
            raise ConsultationError(
                "allergy_review_required",
                "Record the patient's allergies before signing. Orb cannot yet record "
                "'no known allergies'; until it can, finish the visit with a reason.",
                status=409, allergy_review=review)
        now = utc_iso_now()
        db.execute(
            "UPDATE opd_consultations SET status = 'signed', signed_by = ?, signed_at = ?, "
            "row_version = row_version + 1, updated_at = ?, updated_by = ? "
            "WHERE id = ? AND status = 'draft' AND row_version = ?",
            (actor_id, now, now, actor_id, row["id"], row["row_version"]),
        )
        problems = _add_signed_problems(db, row, diagnoses, actor_id=actor_id, now=now)
        note = _serialize(db, _consultation_row(db, visit_id))
        if on_done is not None:
            on_done(db, note, {"problem_ids": problems,
                               "diagnosis_count": len(diagnoses),
                               "allergy_review": review["status"]})
        return note


def add_addendum(
    visit_id: str,
    body: Any,
    *,
    actor_id: str,
    on_done: Optional[Callable[[Any, dict, dict], None]] = None,
) -> dict:
    """Append an addendum to a signed note. Addenda are never edited."""
    text = _clean_text(body, _MAX_ADDENDUM)
    if not text:
        raise ConsultationError("empty_addendum", "Write the addendum first")
    with db_session(immediate=True) as db:
        _writable_visit(db, visit_id)
        row = _consultation_row(db, visit_id)
        if row is None or row["status"] != "signed":
            raise ConsultationError(
                "not_signed", "Only a signed note takes an addendum; edit the draft instead",
                status=409)
        cur = db.execute(
            "INSERT INTO opd_consultation_addenda (consultation_id, visit_id, patient_id, "
            "author_id, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (row["id"], visit_id, row["patient_id"], actor_id, text, utc_iso_now()),
        )
        note = _serialize(db, row)
        if on_done is not None:
            on_done(db, note, {"addendum_id": int(cur.lastrowid)})
        return note


def _signed_summaries(db, patient_id: str) -> list[dict]:
    """Every clinic's signed notes for the record view, newest first, as
    summaries without vitals."""
    rows = db.execute(
        "SELECT c.id, c.visit_id, c.department, c.diagnoses_json, c.plan, c.advice, "
        "       c.follow_up_date, c.signed_by, c.signed_at, v.visit_date "
        "  FROM opd_consultations c LEFT JOIN opd_visits v ON v.id = c.visit_id "
        " WHERE c.patient_id = ? AND c.status = 'signed' "
        " ORDER BY c.signed_at DESC, c.id DESC LIMIT ?",
        (patient_id, RECORD_VISIT_LIMIT),
    ).fetchall()
    names = _user_names(db, [r["signed_by"] for r in rows])
    return [{
        "id": r["id"],
        "visit_id": r["visit_id"],
        "department": r["department"],
        "visit_date": r["visit_date"],
        "diagnoses": [d.get("text") for d in _loads(r["diagnoses_json"], [])
                      if isinstance(d, dict) and d.get("text")],
        "plan": r["plan"],
        "advice": r["advice"],
        "follow_up_date": r["follow_up_date"],
        "signed_by_name": names.get(r["signed_by"]),
        "signed_at": r["signed_at"],
    } for r in rows]


def list_patient_consultations(patient_id: str, *, viewer_department: Optional[str]) -> dict:
    """The patient's OPD notes in full (with addenda), newest first: every
    clinic's signed notes (OPD-N2 extent ii), plus the viewer's own clinic's
    drafts. Another clinic's draft is not a record yet and is not shown."""
    with db_session() as db:
        if not _table_exists(db, "opd_consultations"):
            return {"patient_id": patient_id, "consultations": []}
        rows = db.execute(
            "SELECT * FROM opd_consultations WHERE patient_id = ? "
            "ORDER BY COALESCE(signed_at, updated_at) DESC, id DESC LIMIT ?",
            (patient_id, RECORD_VISIT_LIMIT),
        ).fetchall()
        out = []
        for r in rows:
            own = viewer_department is None or _same_clinic(r["department"], viewer_department)
            if r["status"] != "signed" and not own:
                continue
            note = _serialize(db, r)
            note["own_clinic"] = own
            out.append(note)
    return {"patient_id": patient_id, "consultations": out}
