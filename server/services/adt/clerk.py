"""Orb: the ward clerk's admission, departure and ordered transfer (adt-17b,
HIS wave 9, W9-A). Beds and admissions (ORB_ADT) only: every route that
reaches this module sits behind the module switch, default off.

RULINGS APPLIED AS BUILD DEFAULTS (new safety rulings raised by the ADT
design; built to the strictest reading of the recommendation, and ORB_ADT
stays off at every site until Samuel confirms them):

  ADT-S5 (a)  A ward clerk may make a REGISTRATION-LEVEL admission: who the
              patient is, the hospital's own number, the department (the
              clerk's own, never another), the attending doctor (of that
              department) and the place. Every clinical field is stored as not
              yet assessed: the presenting complaint reads 'Not yet assessed',
              and no diagnosis, triage, history, current medicine,
              observation, note, isolation precaution or code status is taken
              (the request refuses every such field). NO allergy is charted
              and NO allergy review is written, so nothing anywhere records
              'no known allergies'. The allergy interlock then treats the
              patient exactly as it treats any patient with nothing charted:
              unknown, never cleared (services/allergy_interlock reports
              allergies_on_file 0, and every screen says an empty list is not
              'no known allergies'). The admission movement carries the clerk
              marker, and the bed board flags 'Admission assessment due' on the
              patient's bed (assessment_due_ids_on) until a doctor records the
              admission assessment (record_admission_assessment): the allergy
              review (allergies named, none known, or why they could not be
              asked), the presenting complaint and the code status.
  ADT-S3      The clerk records 'left the ward' only on an Orb Ward Record
              computer (ORB_WARD_RECORD), where Orb writes no discharge
              summary: an end of stay through end_stay_on, so it asks
              discharge_preconditions and needs the MRN typed back, with the
              time, no discharge summary and no patient link. The clerk does
              not say how the stay ended, so no end type is guessed and no
              NABH disposition is written (the records desk keys it): a guessed
              'routine' would hide a LAMA. Death, LAMA, DAMA and absconded are
              never the clerk's, and a patient recorded on leave or absconded
              is refused (a doctor records how that stay ended). Outside the
              Ward Record a doctor's discharge ends the stay, so there is
              nothing for the clerk to close.
  ADT-S8 (b)  The clerk changes a patient's treating department only to carry
              out a transfer a doctor has ordered, naming that doctor: an
              active doctor who is the patient's attending doctor or works in
              the patient's current department. The record says the clerk
              NAMED the doctor, never that the doctor acted: the movement's
              reason names them, a fail-closed clinical_access row names both
              people, and the case room gets a line so the treating team sees
              it at once.

Only the ward clerk (CLERK_ROLES, holding adt.bed, with a department) reaches
these lanes, and only for patients of their own department.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import config
from database import SERVICE_ACCOUNT_IDS, db_session
from permissions import ADT_BED, department_scope, has_permission

from services.adt.core import (
    _UNSET,
    AdtConflict,
    AdtError,
    AdtNotFound,
    _case_room_line_on,
    _row,
    _same,
    _text,
    audit_on,
    confirm_terminal_authoriser,
    movement_for_key,
)
from services.clinical_time import utc_iso_now

logger = logging.getLogger("orb.adt.clerk")

__all__ = [
    "CLERK_ROLES",
    "NOT_YET_ASSESSED",
    "ASSESSMENT_DUE_MARKER",
    "CLERK_ADMISSION_KEY",
    "CODE_STATUSES",
    "ALLERGY_STATUSES",
    "LEFT_WARD_DOOR",
    "AdtForbidden",
    "is_clerk",
    "ward_record_on",
    "clerk_entered_ids_on",
    "assessment_due_ids_on",
    "assessment_due_on",
    "ordering_doctor_on",
    "ordered_transfer",
    "record_left_ward",
    "record_admission_assessment",
]

#: Who holds the clerk's lanes (ADT-S5, ADT-S3, ADT-S8 (b)): the ward clerk,
#: never a desk role or the administrator. roles.py is the authority for the
#: role string itself.
CLERK_ROLES: tuple[str, ...] = ("ward_clerk",)

#: What every clinical field of a clerk's admission reads until a clinician
#: records the admission assessment. Never 'no known allergies'.
NOT_YET_ASSESSED = "Not yet assessed"

#: The bed board's marker on the bed of a clerk-admitted patient whose
#: admission assessment is due. src/api/adt/clerk.ts holds the same words.
ASSESSMENT_DUE_MARKER = "Admission assessment due"

#: The key the admission movement's detail_json carries for a clerk's admission.
CLERK_ADMISSION_KEY = "clerk_admission"

#: Code statuses the admission form offers (AdmitPatientModal), the only values
#: the admission assessment accepts.
CODE_STATUSES: tuple[str, ...] = ("full_code", "dnr", "dni", "comfort")

#: What an admission assessment can say about allergies (the OPD review's
#: words, migration 63).
ALLERGY_STATUSES: tuple[str, ...] = ("allergies_recorded", "none_known", "unable_to_obtain")

#: The end-of-stay door the clerk's 'left the ward' records.
LEFT_WARD_DOOR = "clerk_left_ward"

_ALLERGY_STATUS_WORDS = {
    "allergies_recorded": "Allergies recorded",
    "none_known": "No known allergies",
    "unable_to_obtain": "Allergies could not be obtained",
}
_MAX_COMPLAINT = 500
_MAX_TEXT = 1000
_MAX_REASON = 500
_MIN_REASON = 3
_MAX_SUBSTANCES = 30
_MAX_SUBSTANCE_TEXT = 120


class AdtForbidden(AdtError):
    """The caller's role, or this computer's profile, does not allow this. 403."""

    http_status = 403


# ═════════════════════════════════════════════════════════════════════════════
#  Who, and where
# ═════════════════════════════════════════════════════════════════════════════


def is_clerk(user: dict | None) -> bool:
    """True for a ward clerk who holds adt.bed and has a department."""
    role = (user or {}).get("role") or ""
    return (
        role in CLERK_ROLES
        and has_permission(role, ADT_BED)
        and department_scope(user or {}) is not None
    )


def ward_record_on() -> bool:
    """True on an Orb Ward Record computer (ORB_WARD_RECORD). Read at call time."""
    return bool(getattr(config, "WARD_RECORD", False))


# ═════════════════════════════════════════════════════════════════════════════
#  'Admission assessment due' (ADT-S5 (a))
# ═════════════════════════════════════════════════════════════════════════════


def _ids(values: Any) -> tuple:
    return tuple(sorted({str(v) for v in (values or ()) if v}))


def _table_exists(db, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def clerk_entered_ids_on(db, admission_ids: Any) -> set:
    """The admissions among `admission_ids` a ward clerk entered (their
    'admission' movement carries the clerk marker)."""
    ids = _ids(admission_ids)
    if not ids or not _table_exists(db, "patient_movements"):
        return set()
    out: set = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        out |= {
            r[0]
            for r in db.execute(
                f"""SELECT DISTINCT admission_id FROM patient_movements
                 WHERE admission_id IN ({marks}) AND movement_type = 'admission'
                   AND detail_json LIKE '%"{CLERK_ADMISSION_KEY}"%'""",
                chunk,
            ).fetchall()
        }
    return out


def _assessed_ids_on(db, admission_ids: Any) -> set:
    ids = _ids(admission_ids)
    if not ids or not _table_exists(db, "patient_allergy_assessments"):
        return set()
    out: set = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        out |= {
            r[0]
            for r in db.execute(
                f"""SELECT DISTINCT admission_id FROM patient_allergy_assessments
                 WHERE admission_id IN ({marks}) AND source = 'admission'""",
                chunk,
            ).fetchall()
        }
    return out


def assessment_due_ids_on(db, admission_ids: Any) -> set:
    """The admissions among `admission_ids` a ward clerk entered and no doctor
    has yet recorded the admission assessment for. The bed board flags their
    beds (bed_master_service._board_on, inside the caller's scope only)."""
    entered = clerk_entered_ids_on(db, admission_ids)
    if not entered:
        return set()
    return entered - _assessed_ids_on(db, entered)


def assessment_due_on(db, admission_id: str) -> bool:
    return admission_id in assessment_due_ids_on(db, [admission_id])


# ═════════════════════════════════════════════════════════════════════════════
#  The doctor's admission assessment, which clears the flag
# ═════════════════════════════════════════════════════════════════════════════


def _active_allergies_on(db, patient_id: str) -> list[str]:
    return [
        r["substance"]
        for r in db.execute(
            "SELECT substance FROM patient_allergies WHERE patient_id = ? AND status = 'active' "
            "ORDER BY substance",
            (patient_id,),
        ).fetchall()
    ]


def _substances(raw: Any) -> list[str]:
    """The allergens named, free text through allergy_entries, so 'nkda' or
    'nil' is never charted as an allergen."""
    from services.clinical_records_service import allergy_entries

    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise AdtError("List the allergies as text.", code="bad_allergies")
    entries = allergy_entries(raw)
    if len(entries) > _MAX_SUBSTANCES:
        raise AdtError(f"At most {_MAX_SUBSTANCES} allergies at once.", code="bad_allergies")
    for entry in entries:
        if len(entry) > _MAX_SUBSTANCE_TEXT:
            raise AdtError(
                f"Keep each allergy under {_MAX_SUBSTANCE_TEXT} characters.", code="bad_allergies"
            )
    return entries


def record_admission_assessment(
    admission_id: str,
    *,
    actor: dict,
    allergy_status: Any,
    allergies: Any = None,
    reason: Any = None,
    chief_complaint: Any = None,
    initial_diagnosis: Any = None,
    code_status: Any = None,
    note: Any = None,
    request_meta: dict | None = None,
) -> dict:
    """A doctor completes a ward clerk's admission, in ONE immediate session:
    the allergy review (written to patient_allergy_assessments, source
    'admission', with this admission's id), any allergy named charted through
    clinical_records_service.record_allergies_on, the presenting complaint,
    diagnosis and code status written on the admission (and the diagnosis on
    the patients mirror), a fail-closed clinical_access row and a case-room
    line. The bed board's 'Admission assessment due' then clears.

    The OPD review's rules apply to the allergy part: 'none_known' is refused
    while any allergy is active on the chart (a review cannot say 'none' over
    a charted allergy) and with an allergy named; 'unable_to_obtain' needs the
    reason; 'allergies_recorded' needs an allergy named or on the chart.
    Refused, before anything is written: an admission that has ended, one a
    clerk did not enter, and one whose assessment is already recorded."""
    from services.clinical_records_service import record_allergies_on

    status = (_text(allergy_status) or "").lower()
    if status not in ALLERGY_STATUSES:
        raise AdtError(
            "Choose what the patient said about allergies: allergies recorded, no "
            "known allergies, or unable to obtain.",
            code="bad_allergy_status",
        )
    named = _substances(allergies)
    why = _text(reason, _MAX_REASON)
    if status == "none_known" and named:
        raise AdtError(
            "You named an allergy, so record 'allergies recorded', not 'no known allergies'.",
            code="substances_not_allowed",
        )
    if status == "unable_to_obtain":
        if named:
            raise AdtError(
                "Chart the allergies you were told about with 'allergies recorded'.",
                code="substances_not_allowed",
            )
        if not why or len(why) < _MIN_REASON:
            raise AdtError(
                "Say why the allergies could not be obtained, for example the patient "
                "cannot speak and nobody with them knows.",
                code="reason_required",
            )
    complaint = _text(chief_complaint, _MAX_COMPLAINT)
    if not complaint or _same(complaint, NOT_YET_ASSESSED):
        raise AdtError("Type the presenting complaint.", code="chief_complaint_required")
    diagnosis = _text(initial_diagnosis, _MAX_COMPLAINT)
    code = (_text(code_status) or "").lower()
    if code not in CODE_STATUSES:
        raise AdtError(
            "Choose the code status: full code, DNR, DNI or comfort care only.",
            code="code_status_required",
        )
    remark = _text(note, _MAX_TEXT)
    actor_id = (actor or {}).get("id")

    with db_session(immediate=True) as db:
        adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
        if adm is None:
            raise AdtNotFound("No admission with that id", code="admission_not_found")
        if adm.get("status") != "active":
            raise AdtConflict("That stay has ended.", code="admission_not_active")
        if admission_id not in clerk_entered_ids_on(db, [admission_id]):
            raise AdtConflict(
                "This admission was not entered by the ward clerk, so no admission "
                "assessment is due for it.",
                code="not_clerk_admission",
            )
        if not _table_exists(db, "patient_allergy_assessments"):
            raise AdtError(
                "Allergy reviews cannot be recorded on this database yet.",
                code="allergy_review_unavailable",
            )
        if admission_id in _assessed_ids_on(db, [admission_id]):
            raise AdtConflict(
                "The admission assessment for this stay is already recorded.",
                code="assessment_already_recorded",
            )
        pid = adm["patient_id"]
        active = _active_allergies_on(db, pid)
        if status == "none_known" and active:
            raise AdtConflict(
                "This patient has allergies on the chart: "
                + ", ".join(active)
                + ". Confirm them with 'allergies recorded' instead.",
                code="allergies_on_chart",
                charted=active,
            )
        if status == "allergies_recorded" and not named and not active:
            raise AdtError(
                "Name at least one allergy, or record 'no known allergies'.",
                code="no_allergies_named",
            )
        now = utc_iso_now()
        charted = record_allergies_on(
            db, pid, named, source="admission", recorded_by=actor_id, recorded_at=now
        )
        reactivated: list[str] = []
        for entry in named:
            norm = entry.strip().lower()
            if norm in charted:
                continue
            row = db.execute(
                "SELECT id, status FROM patient_allergies WHERE patient_id = ? AND substance = ?",
                (pid, norm),
            ).fetchone()
            if row is not None and row["status"] != "active":
                db.execute(
                    "UPDATE patient_allergies SET status = 'active' WHERE id = ?", (row["id"],)
                )
                reactivated.append(norm)
        on_chart = _active_allergies_on(db, pid)
        cur = db.execute(
            "INSERT INTO patient_allergy_assessments (patient_id, status, substances_json, "
            "reason, note, opd_visit_id, admission_id, department, source, assessed_by, "
            "assessed_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'admission', ?, ?)",
            (
                pid,
                status,
                json.dumps(on_chart) if status == "allergies_recorded" else None,
                why if status == "unable_to_obtain" else None,
                remark,
                admission_id,
                adm.get("department"),
                actor_id,
                now,
            ),
        )
        assessment_id = cur.lastrowid
        db.execute(
            "UPDATE admissions SET chief_complaint = ?, initial_diagnosis = ?, code_status = ? "
            "WHERE id = ?",
            (complaint, diagnosis, code, admission_id),
        )
        db.execute("UPDATE patients SET diagnosis = ? WHERE id = ?", (diagnosis or complaint, pid))
        audit_on(
            db,
            category="clinical_access",
            action="admission_assessment_recorded",
            summary=(
                f"Admission assessment recorded for the clerk's admission {admission_id}: "
                f"{_ALLERGY_STATUS_WORDS[status].lower()}"
            ),
            actor=actor,
            patient_id=pid,
            target_type="admission",
            target_id=admission_id,
            request_meta=request_meta,
            metadata={
                "assessment_id": assessment_id,
                "allergy_status": status,
                "charted": charted,
                "reactivated": reactivated,
                "on_chart_count": len(on_chart),
                "code_status": code,
            },
        )
        allergy_words = {
            "allergies_recorded": "Allergies: " + ", ".join(on_chart),
            "none_known": "Allergies: no known allergies (asked at the admission assessment)",
            "unable_to_obtain": f"Allergies: could not be obtained ({why})",
        }[status]
        name_row = db.execute("SELECT name FROM users WHERE id = ?", (actor_id,)).fetchone()
        who = name_row["name"] if name_row else actor_id
        _case_room_line_on(
            db,
            pid,
            f"Admission assessment recorded by {who}. {allergy_words}. Presenting complaint: "
            f"{complaint}." + (f" Diagnosis: {diagnosis}." if diagnosis else ""),
        )
    return {
        "admission_id": admission_id,
        "patient_id": pid,
        "assessment": {
            "id": assessment_id,
            "status": status,
            "status_label": _ALLERGY_STATUS_WORDS[status],
            "assessed_at": now,
        },
        "charted": charted,
        "reactivated": reactivated,
        "assessment_due": False,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  'Left the ward' (ADT-S3), on an Orb Ward Record computer only
# ═════════════════════════════════════════════════════════════════════════════

_PRESENCE_WORDS = {"on_leave": "on leave", "absconded": "absconded"}


def record_left_ward(
    admission_id: str,
    *,
    actor: dict,
    confirm_mrn: Any,
    occurred_at: Any = None,
    notes: Any = None,
    idempotency_key: Any = None,
    request_meta: dict | None = None,
) -> dict:
    """The ward clerk records that the patient left the ward, in ONE immediate
    session, through end_stay_on (discharge_preconditions, the typed MRN
    read-back, the back-time window, the 'discharge' movement, the bundles
    closed, a fail-closed clinical_access 'stay_ended' row). No end type is
    stated, so none is guessed and no NABH disposition is written; no
    discharge summary is drafted and no patient link is issued.

    Refused: anywhere but an Orb Ward Record computer (403), without an
    Idempotency-Key, and for a patient recorded on leave or absconded (409).
    The same key from the same clerk for the same stay answers what the first
    request recorded and writes nothing."""
    from services.adt.end_stay import end_stay_on
    from services.adt.transfer import sync_housekeeping_after_commit

    if not ward_record_on():
        raise AdtForbidden(
            "On this computer a doctor's discharge ends the stay. 'Left the ward' is recorded by "
            "the ward clerk only on an Orb Ward Record computer.",
            code="left_ward_not_here",
        )
    key = _text(idempotency_key)
    if not key:
        raise AdtError(
            "This needs an Idempotency-Key header, so a retried request is never recorded twice.",
            code="idempotency_key_required",
        )
    actor_id = (actor or {}).get("id")
    with db_session(immediate=True) as db:
        prior = movement_for_key(db, key)
        if prior is not None:
            if (
                prior.get("admission_id") != admission_id
                or prior.get("movement_type") != "discharge"
                or (prior.get("moved_by") or None) != (actor_id or None)
                or (prior.get("detail") or {}).get("door") != LEFT_WARD_DOOR
            ):
                raise AdtConflict(
                    "That Idempotency-Key was already used for something else.",
                    code="idempotency_key_reused",
                )
            return {
                "status": "ended",
                "replayed": True,
                "left_ward": True,
                "admission_id": admission_id,
                "patient_id": prior.get("patient_id"),
                "end_type": prior.get("end_type"),
                "discharged_at": prior.get("moved_at"),
                "movement_id": prior.get("id"),
                "discharge_link_issued": False,
            }
        adm = _row(
            db.execute(
                "SELECT status, presence_status FROM admissions WHERE id = ?", (admission_id,)
            ).fetchone()
        )
        if adm is None:
            raise AdtNotFound("No admission with that id", code="admission_not_found")
        presence = adm.get("presence_status")
        if adm.get("status") == "active" and presence:
            raise AdtConflict(
                f"This patient is recorded as {_PRESENCE_WORDS.get(presence, presence)}, so a "
                "doctor records how the stay ended. Nothing was recorded.",
                code="patient_not_on_ward",
                presence_status=presence,
            )
        result = end_stay_on(
            db,
            admission_id=admission_id,
            end_type=None,
            allow_unstated_type=True,
            actor=actor,
            confirm_mrn=confirm_mrn,
            require_read_back=True,
            occurred_at=occurred_at,
            reason="Left the ward, recorded by the ward clerk",
            notes=notes,
            door=LEFT_WARD_DOOR,
            idempotency_key=key,
            request_meta=request_meta,
        )
    sync_housekeeping_after_commit()
    result["left_ward"] = True
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  A transfer a doctor ordered (ADT-S8 (b))
# ═════════════════════════════════════════════════════════════════════════════


def ordering_doctor_on(db, doctor_id: Any, admission: dict) -> dict:
    """The doctor the clerk names as having ordered a transfer: an active
    doctor (never a service account or a bot) who is the patient's attending
    doctor or works in the patient's current treating department. Raises with
    words otherwise."""
    from services.admission_service import ATTENDING_ROLES

    uid = _text(doctor_id)
    if not uid:
        raise AdtError(
            "Name the doctor who ordered this transfer.", code="ordering_doctor_required"
        )
    row = None
    if uid not in SERVICE_ACCOUNT_IDS and not uid.lower().startswith("bot_"):
        row = _row(
            db.execute(
                "SELECT id, name, role, department, COALESCE(is_active, 1) AS is_active "
                "FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
        )
    if row is None or not row.get("is_active") or row.get("role") not in ATTENDING_ROLES:
        raise AdtError(
            "The doctor named must be an active doctor on this system.",
            code="ordering_doctor_invalid",
        )
    if row["id"] == admission.get("attending_doctor_id"):
        return row
    dept = department_scope(row)
    if dept is not None and _same(dept, admission.get("department")):
        return row
    raise AdtError(
        f"{row.get('name') or row['id']} is not this patient's attending doctor and does not "
        f"work in {admission.get('department') or 'the treating department'}, so cannot be named "
        "as ordering this transfer. Name the doctor who ordered it.",
        code="ordering_doctor_not_treating",
        ordered_by=row["id"],
    )


def ordered_transfer(
    admission_id: str,
    *,
    actor: dict,
    ordered_by: Any,
    to_department: Any,
    reason: Any,
    ward_id: Any = None,
    bed_id: Any = None,
    ward_text: Any = None,
    bed_text: Any = None,
    awaiting_bed: bool = False,
    keep_location: bool = False,
    extra_bed: bool = False,
    expected_bed_id: Any = _UNSET,
    moved_at: Any = None,
    out_of_service_reason: str | None = None,
    boarder_acknowledged: bool = False,
    not_clean_reason: Any = None,
    terminal_clean_authorised_by: Any = None,
    terminal_clean_reason: Any = None,
    terminal_clean_authoriser_pin: Any = None,
    notes: str | None = None,
    idempotency_key: Any = None,
    request_meta: dict | None = None,
) -> dict:
    """The ward clerk carries out a transfer a doctor ordered (POST
    /api/adt/ordered-transfers), in ONE immediate session, through the same
    transfer writer every lane uses (services/adt/transfer.transfer_on: rule
    C, the bed lock, the cleaning rules, the boarder acknowledgement, the
    patient_movement and clinical_access 'patient_transferred' rows). On top:
    the ordering doctor is checked (ordering_doctor_on) and named in the
    movement's reason, a fail-closed clinical_access 'transfer_order_carried_out'
    row names the doctor and the clerk, and the case room gets a line. A move
    inside one department is refused here (it is a bed change, which needs no
    order). The Idempotency-Key, the reason and the bed the screen showed are
    required; the clerk's own retry answers what the first request did."""
    from services import housekeeping_service as hk
    from services.adt import transfer as adt_transfer

    key = _text(idempotency_key)
    if not key:
        raise AdtError(
            "This move needs an Idempotency-Key header, so a retried request is never "
            "recorded twice.",
            code="idempotency_key_required",
        )
    why = _text(reason, 2000)
    if not why:
        raise AdtError(
            "A transfer needs a reason. A move with no reason is not a record.",
            code="reason_required",
        )
    if expected_bed_id is _UNSET:
        raise AdtError(
            "A move must say which bed the screen showed (expected_bed_id), so it "
            "never overwrites a change made since.",
            code="expected_bed_required",
        )
    canonical = adt_transfer.resolve_cover(to_department)
    hk.ensure_housekeeping_schema()
    confirmed = confirm_terminal_authoriser(
        actor=actor,
        authorised_by=terminal_clean_authorised_by,
        pin=terminal_clean_authoriser_pin,
        admission_id=admission_id,
        bed_id=bed_id,
        request_meta=request_meta,
    )
    move = dict(
        ward_id=ward_id,
        bed_id=bed_id,
        ward_text=ward_text,
        bed_text=bed_text,
        awaiting_bed=awaiting_bed,
        keep_location=keep_location,
        extra_bed=extra_bed,
        expected_bed_id=expected_bed_id,
        moved_at=moved_at,
        out_of_service_reason=out_of_service_reason,
        boarder_acknowledged=boarder_acknowledged,
        not_clean_reason=not_clean_reason,
        terminal_clean_authorised_by=terminal_clean_authorised_by,
        terminal_clean_reason=terminal_clean_reason,
        terminal_clean_confirmed=confirmed,
        notes=notes,
        idempotency_key=key,
        request_meta=request_meta,
    )
    with db_session(immediate=True) as db:
        if movement_for_key(db, key) is not None:
            # A retry: the transfer writer answers the first result, or refuses
            # a key used for anything else, before reading the order again.
            return adt_transfer.transfer_on(
                db,
                actor=actor,
                to_department=canonical,
                reason=why,
                admission_id=admission_id,
                **move,
            )
        adm = _row(db.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,)).fetchone())
        if adm is None:
            raise AdtNotFound("No admission with that id", code="admission_not_found")
        if adm.get("status") != "active":
            raise AdtConflict(
                "That admission has ended, so the patient cannot be moved",
                code="admission_not_active",
                status=adm.get("status"),
            )
        if canonical == adm.get("department"):
            raise AdtConflict(
                f"This patient is already treated by {canonical}. A move inside one department "
                "is a bed change, which needs no doctor's order.",
                code="no_department_change",
            )
        doctor = ordering_doctor_on(db, ordered_by, adm)
        doctor_words = doctor.get("name") or doctor["id"]
        result = adt_transfer.transfer_on(
            db,
            actor=actor,
            to_department=canonical,
            reason=f"{why} (ordered by {doctor_words}, named by the ward clerk)",
            admission_id=admission_id,
            **move,
        )
        movement = result["movement"]
        clerk_row = db.execute(
            "SELECT name FROM users WHERE id = ?", ((actor or {}).get("id"),)
        ).fetchone()
        clerk_words = clerk_row["name"] if clerk_row else (actor or {}).get("id")
        audit_on(
            db,
            category="clinical_access",
            action="transfer_order_carried_out",
            summary=(
                f"{adm['patient_id']} transferred from {adm.get('department')} to "
                f"{canonical} by the ward clerk on the order of {doctor_words}"
            ),
            actor=actor,
            patient_id=adm["patient_id"],
            target_type="patient_movement",
            target_id=movement["id"],
            request_meta=request_meta,
            correlation_id=f"transfer-{movement['id']}",
            metadata={
                "movement_id": movement["id"],
                "admission_id": admission_id,
                "ordered_by": doctor["id"],
                "ordered_by_name": doctor.get("name"),
                "ordered_by_department": doctor.get("department") or None,
                "named_by": (actor or {}).get("id"),
                "confirmed_by_doctor": False,
                "from_department": adm.get("department"),
                "to_department": canonical,
            },
        )
        _case_room_line_on(
            db,
            adm["patient_id"],
            f"Transferred from {adm.get('department')} to {canonical} by {clerk_words} (ward "
            f"clerk), on the order of {doctor_words}. {why}",
        )
    adt_transfer.sync_housekeeping_after_commit()
    result["ordered_by"] = {"id": doctor["id"], "name": doctor.get("name")}
    return result
