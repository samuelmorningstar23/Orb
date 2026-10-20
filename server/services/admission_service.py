"""Orb Admission Service — Handles the cross-module orchestration of admitting a patient"""

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from database import SERVICE_ACCOUNT_IDS, db_session

from services.clinical_time import to_utc_iso
from services.drug_chart import drug_chart_enabled as _drug_chart_enabled
from services.patient_name_rule import validate_patient_name
from services.vitals_source import manual_entry_source_or_standin

logger = logging.getLogger("orb.admission")


def _record_dob(raw) -> str | None:
    """Normalize an optional date of birth for the patient record, or None.

    NEVER RAISES, NEVER BLOCKS. An admission must not stop for paperwork, so a
    missing, malformed or impossible date is recorded as absent and logged —
    the patient is admitted either way. The cost of absence is visible and
    honest at the other end: services/discharge_service refuses to print a
    secure discharge link for a patient with no usable date of birth (it is the
    only second factor the patient portal can check) and says so, rather than
    printing a QR code that can never be opened.

    Shares its definition of "usable" with the discharge portal, so a date
    accepted here is a date that will unlock the link.
    """
    from services.discharge_access_service import usable_dob

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    normalized = usable_dob(raw)
    if normalized is None:
        logger.warning(
            "Admission: date of birth %r not stored (needs YYYY-MM-DD, not future) — "
            "the patient is admitted, but no secure discharge link can be issued "
            "until a valid date of birth is recorded.",
            raw,
        )
    return normalized


# Roles a picker may offer as the attending doctor. Same pair the front desk
# books OPD visits with (services/frontdesk_service._CONSULTING_ROLES).
ATTENDING_ROLES = ("doctor", "clinician")


def attending_doctors() -> list[dict]:
    """Active doctors who may be named attending, read from the staff table.

    Exists so no admission surface ships its own list of doctor ids: the
    Scribe quick admission used to offer three hard-coded seed ids, and at any
    site whose staff table does not hold them the admission failed on the
    foreign key after the clinician had filled the form. Staff names and ids
    only, no patient data.

    Automation accounts are left out. The Relay bots are seeded with role
    'doctor', so "Orb Vigil" was offered, and accepted, as a patient's
    attending. The bot_ prefix is excluded too, as User Management reserves it
    for bots (admin_users_routes._is_reserved_id), so a bot seeded before
    anyone adds it to SERVICE_ACCOUNT_IDS is still never a responsible doctor.
    """
    service_ids = sorted(SERVICE_ACCOUNT_IDS)
    marks = ",".join("?" for _ in ATTENDING_ROLES)
    service_marks = ",".join("?" for _ in service_ids)
    with db_session() as db:
        rows = db.execute(
            f"""SELECT id, name, TRIM(COALESCE(department, '')) AS department
                  FROM users
                 WHERE role IN ({marks}) AND COALESCE(is_active, 1) = 1
                   AND id NOT IN ({service_marks})
                   AND LOWER(TRIM(id)) NOT LIKE 'bot\\_%' ESCAPE '\\'
                 ORDER BY name""",
            (*ATTENDING_ROLES, *service_ids),
        ).fetchall()
    return [{"id": r["id"], "name": r["name"], "department": r["department"] or None} for r in rows]


class AdmissionInputError(ValueError):
    """A choice on the admission form the clinician must correct. Raised before
    a single row is written; the admit route answers it with 400 and these words."""


class _ScoringOff(Exception):
    """Skips the baseline NEWS2 at admission while ORB_SCORING_ENABLED is off.
    Raised and caught inside admit_patient only; never leaves this module."""


ATTENDING_NOT_LISTED = "Choose an attending doctor from the list."
DEPARTMENT_NOT_COVERED = (
    "Choose a department from the list. A department no doctor or nurse covers "
    "would put the patient on nobody's ward board."
)


def resolve_admission_department(
    name, *, facility_id: int | None = None, facilities: frozenset | None = None
) -> str:
    """The department as the access boundary spells it, or AdmissionInputError.

    permissions.department_scope matches admissions.department to users.department
    exactly, so a typed "cardiology" or "Cardiolgy" admitted a patient onto no
    doctor's board. Same rule and helper as an OPD registration or a transfer
    (frontdesk_service._resolve_covering_department).

    multisite-3: `facility_id` or `facilities` keep the departments of that
    facility or set; with more than one facility on record a department the
    departments master does not list never resolves. The admission itself
    checks the facility again (admission_facility) whatever the caller passed.
    """
    from services.frontdesk_service import _resolve_covering_department

    canonical = _resolve_covering_department(
        name if isinstance(name, str) else "", facility_id=facility_id, facilities=facilities
    )
    if canonical is None:
        raise AdmissionInputError(DEPARTMENT_NOT_COVERED)
    return canonical


# ═════════════════════════════════════════════════════════════════════════════
#  Admissions carry the facility (multisite-3, W10-B)
# ═════════════════════════════════════════════════════════════════════════════
# With one facility on record (every site today) nothing here reads or writes
# anything: every admission and patient keeps migration 70's default,
# facility 1. With more than one, an admission takes the facility of its
# department in the departments master (ruling MS-R4 (a): a department belongs
# to exactly one facility), stamped on admissions.facility_id and, for a
# patient the admission creates, on patients.registered_facility_id, in the
# admission's own transaction. Refused first, each after an authorization
# 'cross_facility_move_refused' row and with nothing written:
#   * a department no facility lists (400 department_not_listed);
#   * an admitting account whose facilities do not include it (403
#     facility_scope): the admitter could not open the chart they created;
#   * an existing patient with an admission at another facility (409
#     cross_facility_move_refused): the facility of a chart is derived from
#     its admissions, so they must all be at one facility until encounters
#     carry their own (ruling MS-R3 stays at (c), refuse).

OUTSIDE_FACILITY_WORDS = (
    "That department belongs to another facility of the group. Admit the patient to a "
    "department of your own facility."
)
CROSS_FACILITY_ADMISSION_WORDS = (
    "This patient has been admitted at another facility of the group. Orb does not "
    "admit a patient at a second facility yet, so nothing was saved."
)


def admission_facility(
    department,
    user: dict,
    *,
    lane: str,
    patient_id: str | None = None,
    request_meta: dict | None = None,
) -> int | None:
    """The facility a new admission into `department` is at, or None while one
    facility is on record (nothing to stamp). Raises AdmissionRefused, after
    the audit row, for the three refusals above. Opens sessions of its own:
    call it before the admission's transaction opens."""
    from services import facility_service
    from services.frontdesk_service import (
        audit_cross_facility_refusal,
        patient_facility_ids_on,
    )

    if not facility_service.multi_facility_data():
        return None
    to_facility = facility_service.department_facility(department)
    scope = facility_service.facility_scope_for(user)
    held: set = set()
    if patient_id:
        with db_session() as db:
            held = patient_facility_ids_on(db, patient_id)
    if to_facility is None:
        cause, refused = (
            "department_not_listed",
            AdmissionRefused("department_not_listed", DEPARTMENT_NOT_COVERED, status=400),
        )
    elif scope is not None and to_facility not in scope:
        cause, refused = (
            "actor_outside_facility",
            AdmissionRefused("facility_scope", OUTSIDE_FACILITY_WORDS, status=403),
        )
    elif held - {to_facility}:
        cause, refused = (
            "patient_at_another_facility",
            AdmissionRefused(
                "cross_facility_move_refused", CROSS_FACILITY_ADMISSION_WORDS, status=409
            ),
        )
    else:
        return to_facility
    audit_cross_facility_refusal(
        lane=lane,
        cause=cause,
        patient_id=patient_id,
        actor_id=user.get("id"),
        actor_role=user.get("role"),
        to_department=department,
        to_facility_id=to_facility,
        from_facility_ids=held,
        request_meta=request_meta,
    )
    raise refused


def _stamp_admission_facility_on(
    db, *, admission_id: str, patient_id: str, facility_id: int | None, new_patient: bool
) -> None:
    """Write the admission's facility on the admission's own transaction, and
    for a patient this admission creates, the facility they were registered
    at. None (one facility on record): the columns' default, 1, stands."""
    if facility_id is None:
        return
    db.execute("UPDATE admissions SET facility_id = ? WHERE id = ?", (facility_id, admission_id))
    if new_patient:
        db.execute(
            "UPDATE patients SET registered_facility_id = ? WHERE id = ?", (facility_id, patient_id)
        )


def require_listed_attending(doctor_id) -> str:
    """The attending id when it is an active doctor on the list, else
    AdmissionInputError. An unknown id used to reach the foreign key and come
    back as a raw 500 after the whole form was filled."""
    wanted = doctor_id.strip() if isinstance(doctor_id, str) else ""
    if not wanted or wanted not in {d["id"] for d in attending_doctors()}:
        raise AdmissionInputError(ATTENDING_NOT_LISTED)
    return wanted


def bed_in_use_warning(db, ward, bed) -> str | None:
    """A sentence when this ward and bed are already held by an admitted patient.

    WARN, NEVER BLOCK (block or warn is the founder's call; warn until then).
    Bed labels are free text, a site's bed register may be incomplete, and a
    ward under pressure does double up a bed space, so the admission stands and
    the clinician is told. Takes the caller's handle so admit_patient checks
    inside its own transaction, before its own patient row exists. Trimmed and
    case-blind, so "gm-101 " is GM-101.
    """
    ward_t = ward.strip() if isinstance(ward, str) else ""
    bed_t = bed.strip() if isinstance(bed, str) else ""
    if not ward_t or not bed_t:
        return None
    held = db.execute(
        """SELECT 1 FROM patients
            WHERE status = 'admitted'
              AND LOWER(TRIM(COALESCE(ward, ''))) = LOWER(?)
              AND LOWER(TRIM(COALESCE(bed, ''))) = LOWER(?)
            LIMIT 1""",
        (ward_t, bed_t),
    ).fetchone()
    if held is None:
        return None
    return (
        f"Bed {bed_t} on {ward_t} is already held by another admitted patient. "
        "Check the bed with the ward."
    )


def _natural_key(label: str) -> list:
    """GM-2 before GM-10. re.split with a group alternates text and digits, so
    like is always compared with like."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", label)]


def admission_choices(user_department=None, *, user: dict | None = None) -> dict:
    """What the admission form offers: covered departments, attending doctors,
    and ward and bed labels with whether each bed is held.

    `user` (the caller, optional) is read only with ORB_ADT on, to say whether
    the caller may authorise a bed awaiting a terminal clean themself.

    No patient data leaves: department names, staff names and ids, ward and bed
    labels, and a held flag (never by whom). Beds come from the housekeeping
    register where a site keeps one, plus every bed an admitted patient is in,
    so a site with no register still gets the held warning. Wards and beds are
    suggestions; an unlisted one is still accepted (a new bay is legitimate).
    The DEPARTMENT is the one validated choice, because it carries the access
    boundary.
    """
    from services import facility_service
    from services.frontdesk_service import list_covering_departments

    # multisite-3: with more than one facility on record, only the departments
    # of the caller's own facilities (admission_facility refuses the rest).
    scope = (
        facility_service.facility_scope_for(user)
        if user is not None and facility_service.multi_facility_data()
        else None
    )
    departments = list_covering_departments(facilities=scope)
    with db_session() as db:
        held_rows = db.execute(
            """SELECT TRIM(ward) AS ward, TRIM(bed) AS bed FROM patients
                WHERE status = 'admitted'
                  AND TRIM(COALESCE(ward, '')) != '' AND TRIM(COALESCE(bed, '')) != ''"""
        ).fetchall()
        ward_departments = db.execute(
            """SELECT DISTINCT TRIM(ward) AS ward, TRIM(department) AS department
                 FROM admissions
                WHERE status = 'active'
                  AND TRIM(COALESCE(ward, '')) != '' AND TRIM(COALESCE(department, '')) != ''"""
        ).fetchall()
        # Read straight from the table, never through housekeeping_service: that
        # module must never sit in the admission path (its module note).
        has_register = (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'housekeeping_units'"
            ).fetchone()
            is not None
        )
        register_rows = (
            db.execute(
                """SELECT TRIM(ward) AS ward,
                      COALESCE(NULLIF(TRIM(bed_label), ''), TRIM(unit_code)) AS bed
                 FROM housekeeping_units
                WHERE unit_type = 'bed' AND COALESCE(is_active, 1) = 1
                  AND TRIM(COALESCE(ward, '')) != ''"""
            ).fetchall()
            if has_register
            else []
        )

    def key(ward: str, bed: str) -> tuple:
        return ward.casefold(), bed.casefold()

    held = {key(r["ward"], r["bed"]) for r in held_rows}
    beds: dict = {}
    # Register first, so its spelling of a label wins over a typed one.
    for r in [*register_rows, *held_rows]:
        if r["bed"]:
            k = key(r["ward"], r["bed"])
            beds.setdefault(k, {"ward": r["ward"], "bed": r["bed"], "in_use": k in held})
    wards: dict = {}
    for b in beds.values():
        wards.setdefault(b["ward"].casefold(), {"ward": b["ward"], "departments": []})
    for r in ward_departments:
        entry = wards.setdefault(r["ward"].casefold(), {"ward": r["ward"], "departments": []})
        if r["department"] not in entry["departments"]:
            entry["departments"].append(r["department"])

    wanted = (user_department or "").strip().casefold() if isinstance(user_department, str) else ""
    choices: dict[str, Any] = {
        "departments": departments,
        "default_department": next((d for d in departments if d.casefold() == wanted), None),
        "doctors": attending_doctors(),
        "wards": sorted(wards.values(), key=lambda w: _natural_key(w["ward"])),
        "beds": sorted(
            beds.values(), key=lambda b: (_natural_key(b["ward"]), _natural_key(b["bed"]))
        ),
    }
    # Beds and admissions (ORB_ADT, default off): the bed master's wards and
    # beds with the status the bed board derives, for the form's bed picker
    # (adt-5). Absent while the module is off, so the answer is unchanged.
    if _adt_admitting():
        choices["master"] = _master_choices(user)
    return choices


# ═════════════════════════════════════════════════════════════════════════════
#  Admitting into a master bed (adt-5, W5-B; ORB_ADT, default off)
# ═════════════════════════════════════════════════════════════════════════════
#
# With the module on, admit_patient runs in ONE immediate session and puts the
# patient in a place through services.adt.core (prepare_place_on, then
# place_admission_on, which writes through the single location writer
# locate_admission_on): the 'admission' movement and its fail-closed
# patient_movement audit commit with the patient, or nothing does. The rules
# (an occupied bed refused with an extra bed offered, a bed not recorded clean
# needing a reason or a named authoriser, a boarder needing the
# acknowledgement, a back-time window) are stated in services/adt/core.py.
# Text from lanes that only type (Scribe's quick admission) resolves against
# the master the same way (design section 5): 'gm-1' links to GM-1. With the
# module off nothing here runs and admit_patient is exactly as before.


def _adt_admitting() -> bool:
    """True while ORB_ADT is on (read at call time; draining counts as off,
    so a module being switched off takes no new master placements)."""
    from services.adt.core import adt_on

    return adt_on()


def _master_choices(user: dict | None = None) -> dict:
    """The bed picker's lists: every active ward and its beds with what placing
    a patient there needs (services.adt.core.bed_choices_on), who may
    authorise a bed awaiting a terminal clean, and the back-time window.

    Authorising such a bed is an act, never a name (ruling ADT-S2 (c), round
    3): `self_authoriser` is the caller when their role allows (their own
    session confirms it), and `authorisers` lists the other doctors, nurses and
    administrators who can confirm it on the spot with their own signing PIN,
    so only those with a PIN set. Each carries a department; the picker offers
    only those the server accepts for the ward (the ward's department, the
    treating one, or none). Staff names and ward and bed labels only; no
    patient data."""
    import config

    from services import housekeeping_service as hk
    from services.adt.core import (
        TERMINAL_CLEAN_AUTHORISER_ROLES,
        bed_choices_on,
        terminal_authoriser_eligible_on,
    )

    # Opens a session of its own, so it runs before the read session below.
    hk.ensure_housekeeping_schema()
    me = str((user or {}).get("id") or "")
    service_ids = sorted(SERVICE_ACCOUNT_IDS)
    role_marks = ",".join("?" for _ in TERMINAL_CLEAN_AUTHORISER_ROLES)
    service_marks = ",".join("?" for _ in service_ids)
    with db_session() as db:
        wards = bed_choices_on(db)
        authorisers = [
            {
                "id": r["id"],
                "name": r["name"],
                "role": r["role"],
                "department": r["department"] or None,
            }
            for r in db.execute(
                f"""SELECT id, name, role, TRIM(COALESCE(department, '')) AS department
                      FROM users
                     WHERE role IN ({role_marks}) AND COALESCE(is_active, 1) = 1
                       AND id NOT IN ({service_marks}) AND id <> ?
                       AND LOWER(TRIM(id)) NOT LIKE 'bot\\_%' ESCAPE '\\'
                       AND pin_hash IS NOT NULL AND pin_hash <> ''
                     ORDER BY name""",
                (*TERMINAL_CLEAN_AUTHORISER_ROLES, *service_ids, me),
            ).fetchall()
        ]
        self_authoriser = None
        if me and terminal_authoriser_eligible_on(db, me):
            row = db.execute(
                "SELECT id, name, role, TRIM(COALESCE(department, '')) AS department "
                "FROM users WHERE id = ?",
                (me,),
            ).fetchone()
            self_authoriser = {
                "id": row["id"],
                "name": row["name"],
                "role": row["role"],
                "department": row["department"] or None,
            }
    from services.adt.core import strict_locations_on

    return {
        "wards": wards,
        "authorisers": authorisers,
        "self_authoriser": self_authoriser,
        "backtime_hours": int(getattr(config, "ADT_BACKTIME_HOURS", 72)),
        # adt-16: with strict locations on the server refuses a ward or bed
        # not in bed setup, so the picker offers no typed place.
        "strict_locations": strict_locations_on(),
    }


def _plain(value) -> str | None:
    """Spaces tidied, None when blank or not text."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _adt_admission_plan(
    data: dict,
    idempotency_key,
    user: dict | None = None,
    request_meta: dict | None = None,
    authoriser_pin=None,
) -> dict:
    """The placement fields of an admission, checked BEFORE any session opens
    (the immediate session must be the outermost one). Raises AdtError (or
    AdtBadTime, 400) with words. A signing PIN typed for a terminal-clean
    authoriser is checked here too (services.adt.core.confirm_terminal_
    authoriser), so a refused PIN is audited in a session of its own; only
    whether it confirmed somebody is kept, never the PIN."""
    from services import housekeeping_service as hk
    from services.adt.core import (
        AdtError,
        check_event_time,
        confirm_terminal_authoriser,
    )
    from services.clinical_time import to_utc_iso as _iso

    awaiting = data.get("awaiting_bed") is True
    extra = data.get("extra_bed") is True
    plan = {
        "key": _plain(idempotency_key),
        "ward_id": _plain(data.get("ward_id")),
        "bed_id": _plain(data.get("bed_id")),
        "ward_text": _plain(data.get("ward")),
        "bed_text": _plain(data.get("bed")),
        "extra_bed": extra,
        "boarder_acknowledged": data.get("boarder_acknowledged") is True,
        "out_of_service_reason": data.get("out_of_service_reason"),
        "not_clean_reason": data.get("not_clean_reason"),
        "terminal_clean_authorised_by": data.get("terminal_clean_authorised_by"),
        "terminal_clean_reason": data.get("terminal_clean_reason"),
    }
    if awaiting and (plan["bed_id"] or plan["bed_text"] or extra):
        raise AdtError(
            "No bed yet was chosen, but a bed was named as well. Choose one or the other.",
            code="awaiting_bed_with_bed",
        )
    admitted = check_event_time(data.get("admitted_at"), what="admission")
    plan["admitted"] = admitted
    plan["admitted_iso"] = _iso(admitted)
    # Opens a session of its own (the lazy housekeeping path), so before ours.
    hk.ensure_housekeeping_schema()
    plan["terminal_clean_confirmed"] = confirm_terminal_authoriser(
        actor=user,
        authorised_by=plan["terminal_clean_authorised_by"],
        pin=authoriser_pin,
        bed_id=plan["bed_id"],
        request_meta=request_meta,
    )
    return plan


def _adt_replay_on(db, plan: dict, user: dict, name: str) -> dict | None:
    """The first admission when this Idempotency-Key was already used for an
    admission by this person for a patient of this name; None for a new key.
    A key used for anything else is refused and nothing is written. The
    one-time portal password was shown once and cannot be shown again."""
    from services.adt.core import AdtConflict, movement_for_key

    if not plan["key"]:
        return None
    prior = movement_for_key(db, plan["key"])
    if prior is None:
        return None
    row = db.execute(
        """SELECT p.id, p.name, p.mrn, p.user_id, p.date_of_birth,
                  a.id AS admission_id, a.admitted_at
             FROM admissions a JOIN patients p ON p.id = a.patient_id
            WHERE a.id = ?""",
        (prior.get("admission_id"),),
    ).fetchone()
    same = (
        row is not None
        and prior.get("movement_type") == "admission"
        and (prior.get("moved_by") or None) == user.get("id")
        and (_plain(row["name"]) or "").casefold() == (_plain(name) or "").casefold()
    )
    if not same:
        raise AdtConflict(
            "That Idempotency-Key was already used for another admission. Nothing was saved.",
            code="idempotency_key_reused",
        )
    room = db.execute(
        "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room' "
        "ORDER BY created_at LIMIT 1",
        (row["id"],),
    ).fetchone()
    return {
        "success": True,
        "replayed": True,
        "patient_id": row["id"],
        "admission_id": row["admission_id"],
        "channel_id": room["id"] if room else None,
        "mrn": row["mrn"],
        "patient_login_id": row["user_id"],
        "temporary_password": None,
        "date_of_birth_recorded": row["date_of_birth"] is not None,
        "bed_warning": None,
        "admitted_at": row["admitted_at"],
        "movement_id": prior["id"],
        "location": {
            "ward_id": prior.get("to_ward_id"),
            "bed_id": prior.get("to_bed_id"),
            "ward": prior.get("to_ward"),
            "bed": prior.get("to_bed"),
            "linked": prior.get("to_bed_id") is not None,
            "awaiting_bed": not prior.get("to_bed_id") and not prior.get("to_bed"),
            "boarder": bool((prior.get("detail") or {}).get("boarder")),
            "extra_bed": ((prior.get("detail") or {}).get("extra_bed") or {}).get("label"),
        },
        "location_warning": None,
        # adt-17b: a replayed ward clerk's admission says so again.
        **(
            {"clerk_admission": True, "clinical_fields": "Not yet assessed"}
            if (prior.get("detail") or {}).get("clerk_admission")
            else {}
        ),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  The admission doors join the patient index (opd-5b, W6-B)
# ═════════════════════════════════════════════════════════════════════════════
#
# ALWAYS ON, whatever the switches say: a hospital number (MRN) typed on the
# admission form that is already on another record is refused with 409
# 'mrn_in_use' naming nobody. It used to reach the UNIQUE constraint on
# patients.mrn and come back as a raw 500 after the whole form was filled.
#
# WITH REGISTRATION ON (ORB_REGISTRATION, default off), both doors (the
# admission form and Scribe's quick admission) behave as the registration desk
# does (services/registration_service.py), so neither door can open a second
# chart for a patient the index already holds:
#   * the hospital number follows ORB_UHID_SOURCE (ruling OPD-N1). Hospital
#     mode needs the typed number (422 uhid_required; never a random MRN) in
#     the site's format; Orb mode issues it from the UHID series inside the
#     admission's transaction (a typed one is 422 uhid_assigned_by_orb; 409
#     uhid_series_not_configured until the series is set up);
#   * the patient index is searched with what the form says (name, phone,
#     date of birth or age, sex, the hospital number) through
#     patient_matching.find_candidates_on, and the answer is judged by the
#     desk's own rule (registration_service._review_needs, config.
#     DUPLICATE_MODE, ruling OPD-S2 built strictest): the same hospital
#     number is a hard refusal in every mode; 'block' (the shipped default)
#     refuses a probable duplicate and a check that could not compare every
#     record that may be a probable one; anything else the index found needs
#     a duplicate_review naming every candidate with a reason, audited under
#     patient_identity, with a worklist row per candidate. Every such refusal
#     is 409 error 'possible_existing_patient' with the candidates as minimal
#     index cards (no date of birth, only the phone's last 4 digits), shown
#     only to a caller who holds registration.read; the disclosure is audited
#     fail-closed in its own write, so a refusal whose record cannot be
#     written is 503 and shows nobody;
#   * the new patient's match keys are written in the same transaction, and a
#     fail-closed patient_identity row records the check.
# Admitting an existing patient onto their own record: a registered outpatient
# in an open clinic visit today is admitted from that visit (opd-17a,
# admit_existing_patient below, while ORB_OPD is on), and the refusal says so;
# a discharged patient is readmitted onto their record (opd-17c, readmit_patient
# below, while ORB_READMISSION is on), and the refusal says so and marks which
# candidates may be readmitted ('readmittable'). Otherwise the
# override is the only way past a reviewable match. With registration off none
# of this runs and the doors are exactly as before, except the 409 above.

#: The admission doors, as the audit names them.
DOOR_ADMISSION_FORM = "admission_form"
DOOR_SCRIBE_QUICK_ADMIT = "scribe_quick_admit"
ADMISSION_DOORS = (DOOR_ADMISSION_FORM, DOOR_SCRIBE_QUICK_ADMIT)

#: The one error word every index refusal at an admission door carries; the
#: detail's 'reason' says which rule refused it.
POSSIBLE_EXISTING_PATIENT = "possible_existing_patient"

MRN_IN_USE_MESSAGE = (
    "That hospital number is already on another record, so this admission was not "
    "saved. Check the number on the patient's papers."
)
#: Added while registration is off, where a blank number is minted.
MRN_IN_USE_BLANK_HINT = " Or leave it blank to give this patient a new one."
#: Said on every index refusal: the route that would admit the existing
#: patient onto their own record is not built yet (opd-17a, opd-17c).
ADMIT_EXISTING_NOT_BUILT = (
    "Admitting a patient who already has a record onto that record is not available "
    "yet, so ask an administrator before admitting them."
)
#: Said instead while outpatients (ORB_OPD) is on, where opd-17a is built.
ADMIT_EXISTING_FROM_VISIT = (
    "If this is the patient and they are in an outpatient clinic today, the consulting "
    "doctor admits them onto their own record from that visit with Advise admission. "
    "Readmitting a discharged patient onto their record is not available yet."
)
#: Said while readmission (ORB_READMISSION, opd-17c) is on, with outpatients off.
READMIT_POINTER = (
    "If this is a patient who was an inpatient here before, readmit them onto their own "
    "record instead: choose Readmit and confirm who they are."
)
#: Said instead at Scribe's quick admission, which has no Readmit choice.
READMIT_FROM_FORM_POINTER = (
    "If this is a patient who was an inpatient here before, readmit them onto their own "
    "record from the admission form instead, where you confirm who they are."
)
#: Said while readmission and outpatients are both on.
READMIT_FROM_VISIT_POINTER = (
    "If this is the patient and they are in an outpatient clinic today, the consulting "
    "doctor admits them onto their own record from that visit with Advise admission. If "
    "they were an inpatient here before, readmit them onto their own record instead: choose "
    "Readmit and confirm who they are."
)


class AdmissionRefused(Exception):
    """A refusal at an admission door, with a machine word, plain words and the
    HTTP status the routes answer with. Raised before the admission commits;
    nothing of it is saved. Not a ValueError on purpose, so no route's broad
    ValueError handler can turn it into a 500."""

    def __init__(self, code: str, message: str, *, status: int = 409, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def detail(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


class _DoorRefusal(Exception):
    """Carries an index refusal out of the admission's transaction, so its
    disclosure audit is written on a write of its own (an audit row written
    inside a rolled-back transaction is lost)."""

    def __init__(self, refused: AdmissionRefused, *, metadata: dict, shown: bool):
        super().__init__(refused.message)
        self.refused = refused
        self.metadata = metadata
        self.shown = shown


def _registration_on() -> bool:
    """True while ORB_REGISTRATION is on (read at call time)."""
    import config

    return bool(config.feature_enabled("registration"))


def _mrn_in_use() -> AdmissionRefused:
    hint = "" if _registration_on() else MRN_IN_USE_BLANK_HINT
    return AdmissionRefused("mrn_in_use", MRN_IN_USE_MESSAGE + hint, status=409)


def _door_review(raw) -> dict | None:
    """The duplicate_review an admission door may send past a reviewable
    match: {decision: 'admit_new', candidate_ids: [...], reason}. None when
    absent. Same reason rule as the registration desk."""
    from services import registration_service as reg

    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("decision") != "admit_new":
        raise AdmissionRefused(
            "bad_duplicate_review",
            "A duplicate review must say 'admit_new' and list the records you checked.",
            status=422,
        )
    ids = raw.get("candidate_ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, str) and i.strip() for i in ids):
        raise AdmissionRefused(
            "bad_duplicate_review", "List the records you checked by their ids.", status=422
        )
    reason = raw.get("reason")
    reason = " ".join(reason.split()) if isinstance(reason, str) else ""
    if len(reason) < reg.REASON_MIN_CHARS:
        raise AdmissionRefused(
            "override_reason_required",
            f"Give a reason of at least {reg.REASON_MIN_CHARS} characters. It is the only "
            "thing that explains this to whoever reads it next.",
            status=422,
        )
    return {
        "decision": "admit_new",
        "candidate_ids": sorted({i.strip() for i in ids}),
        "reason": reason[:1000],
    }


def _door_identity_plan(data: dict, door: str) -> dict:
    """What the patient index asks of this admission, checked BEFORE any
    session opens. {'on': False} while registration is off (the door is as it
    was). Raises AdmissionRefused (422 or 409) for a hospital number the site's
    rule refuses."""
    if not _registration_on():
        return {"on": False}
    import config

    from services import registration_service as reg

    source = config.UHID_SOURCE
    typed = data.get("mrn")
    typed = typed.strip() if isinstance(typed, str) else None
    uhid = None
    if source == "orb":
        if typed:
            raise AdmissionRefused(
                "uhid_assigned_by_orb",
                "This site issues hospital numbers itself. Leave the hospital number blank.",
                status=422,
            )
    else:
        try:
            uhid = reg._hospital_uhid(typed)
        except reg.RegistrationError as exc:
            raise AdmissionRefused(exc.code, exc.message, status=exc.status) from exc
    return {
        "on": True,
        "door": door,
        "uhid_source": source,
        "uhid": uhid,
        "review": _door_review(data.get("duplicate_review")),
    }


def _may_see_index_cards(user: dict) -> bool:
    """Index cards go to a caller who may search the patient index (ruling
    OPD-N4): today every admitting role does; anyone else is told how many
    records were found, never whose."""
    from permissions import REGISTRATION_READ, has_permission

    return bool(has_permission(user.get("role") or "", REGISTRATION_READ))


def _door_refusal(
    reason: str,
    message: str,
    candidates: list[dict],
    *,
    mode: str,
    scan: dict,
    user: dict,
    door: str,
    override_allowed: bool,
) -> _DoorRefusal:
    shown = _may_see_index_cards(user)
    ids = [c["patient_id"] for c in candidates]
    # opd-17a: with outpatients on, a registered patient in a clinic today is
    # admitted onto their own record from the visit, so the refusal says how.
    from_visit = _conversion_on()
    pointer = ADMIT_EXISTING_FROM_VISIT if from_visit else ADMIT_EXISTING_NOT_BUILT
    # opd-17c: with readmission on, a discharged patient is readmitted onto
    # their own record, so the refusal says how and which records allow it
    # (each card's 'readmittable', set by _door_index_check_on). Absent while
    # it is off, so the refusal is exactly as before.
    readmit_extra: dict = {}
    if readmission_on():
        pointer = (
            READMIT_FROM_FORM_POINTER
            if door == DOOR_SCRIBE_QUICK_ADMIT
            else READMIT_FROM_VISIT_POINTER
            if from_visit
            else READMIT_POINTER
        )
        readmit_extra = {"readmission_available": any(c.get("readmittable") for c in candidates)}
    refused = AdmissionRefused(
        POSSIBLE_EXISTING_PATIENT,
        f"{message} {pointer}",
        status=409,
        reason=reason,
        candidates=candidates if shown else [],
        candidate_count=len(candidates),
        candidates_hidden=not shown,
        mode=mode,
        check_incomplete=scan["check_incomplete"],
        override_allowed=override_allowed,
        admit_existing_available=from_visit,
        **readmit_extra,
    )
    return _DoorRefusal(
        refused,
        shown=shown,
        metadata={
            "reason_code": reason,
            "door": door,
            "mode": mode,
            "matched_patient_ids": ids,
            "bands": [c["band"] for c in candidates],
            "candidates_shown": shown,
            **scan,
        },
    )


def _door_index_check_on(
    db, plan: dict, *, name: str, data: dict, date_of_birth, user: dict
) -> dict:
    """Search the patient index for this admission inside its own immediate
    transaction and apply the desk's duplicate rule (section note above).
    Raises _DoorRefusal (candidates to disclose) or AdmissionRefused. Returns
    the facts the admission writes after its rows: the hospital number to
    store and what the check found."""
    from permissions import department_scope

    from services import patient_matching as pm
    from services import registration_service as reg

    series = None
    if plan["uhid_source"] == "orb":
        series = reg._uhid_series_on(db)
        if not series or not int(series.get("is_configured") or 0):
            raise AdmissionRefused(
                "uhid_series_not_configured",
                "Hospital numbers cannot be issued yet: the UHID series is not set up on "
                "this site. Ask an administrator to configure it.",
                status=409,
            )
    probe = pm.probe_from(
        name=name,
        phone=data.get("phone"),
        date_of_birth=date_of_birth,
        age_years=None if date_of_birth else data.get("age"),
        sex=data.get("sex"),
        uhid=plan["uhid"],
    )
    found = pm.find_candidates_on(
        db, probe, dob_visible=reg._dob_visible_to(db, department_scope(user))
    )
    candidates = reg._decorate_candidates(db, found["candidates"])
    if readmission_on():
        # opd-17c: which of these records a discharged patient may be
        # readmitted onto (the same rule readmit_patient applies).
        readmittable = _readmittable_ids_on(db, [c["patient_id"] for c in candidates])
        for c in candidates:
            c["readmittable"] = c["patient_id"] in readmittable
    needs = reg._review_needs(candidates, unseen_band=found.get("unseen_band"))
    scan = reg._scan_facts(found, needs)
    mode, door = needs["mode"], plan["door"]
    if needs["hard"]:
        raise _door_refusal(
            "existing_record",
            "A patient with this hospital number already has a record, so this admission "
            "was not saved and the patient does not get a second chart.",
            needs["hard"],
            mode=mode,
            scan=scan,
            user=user,
            door=door,
            override_allowed=False,
        )
    if needs["blocked"] and needs["probable"]:
        raise _door_refusal(
            "probable_duplicate",
            "This looks like a patient who already has a record, so this admission was not "
            "saved. This site does not admit past a probable duplicate.",
            needs["probable"] + needs["possible"],
            mode=mode,
            scan=scan,
            user=user,
            door=door,
            override_allowed=False,
        )
    if needs["blocked"]:
        raise _door_refusal(
            "duplicate_check_incomplete",
            "Too many records share this phone number, name or date of birth for the "
            "duplicate check to compare them all, and one it did not compare may be this "
            "patient, so this admission was not saved. Search the patient index for them, "
            "and leave out a phone number that is not the patient's own.",
            needs["possible"],
            mode=mode,
            scan=scan,
            user=user,
            door=door,
            override_allowed=False,
        )
    reviewed: list[dict] = []
    review = plan["review"]
    if needs["review_required"]:
        listed = needs["probable"] + needs["possible"]
        if review is None:
            message = (
                "This may be a patient who already has a record, so this admission was not "
                "saved yet. Check each record. To admit as a new patient anyway, give a reason."
                if listed
                else "Too many records share this phone number, name or date of birth for the "
                "duplicate check to compare them all, so this may be a patient who already "
                "has a record and this admission was not saved yet. Search the patient index "
                "for them. To admit as a new patient anyway, give a reason."
            )
            raise _door_refusal(
                "duplicate_review_required",
                message,
                listed,
                mode=mode,
                scan=scan,
                user=user,
                door=door,
                override_allowed=_may_see_index_cards(user),
            )
        if any(c["patient_id"] not in review["candidate_ids"] for c in listed):
            raise _door_refusal(
                "duplicate_review_stale",
                "More possible matches were found since you checked, so this admission was "
                "not saved. Review the list again.",
                listed,
                mode=mode,
                scan=scan,
                user=user,
                door=door,
                override_allowed=_may_see_index_cards(user),
            )
        reviewed = listed
    uhid = plan["uhid"]
    if series is not None:
        from services.billing_masters import SeriesNotConfigured, allocate_series_number

        try:
            uhid = allocate_series_number(db, reg.UHID_SERIES, require_configured=True)["number"]
        except SeriesNotConfigured:
            raise AdmissionRefused(
                "uhid_series_not_configured",
                "Hospital numbers cannot be issued yet: the UHID series is not set up on "
                "this site.",
                status=409,
            ) from None
        if reg._uhid_in_use_on(db, uhid):
            raise AdmissionRefused(
                "uhid_series_collision",
                "The next number in the UHID series is already in use. Ask an administrator "
                "to check the series before admitting anyone.",
                status=409,
            )
    return {
        "mrn": uhid,
        "mode": mode,
        "scan": scan,
        "reviewed": reviewed,
        "review": review if needs["review_required"] else None,
        "review_used": bool(needs["review_required"]),
    }


def _door_record_on(
    db,
    facts: dict,
    plan: dict,
    *,
    patient_id: str,
    admission_id: str,
    user: dict,
    now: str,
    request_meta: dict | None,
) -> None:
    """After the admission's rows: the worklist row for each candidate the
    clinician admitted past, and the fail-closed patient_identity rows, in the
    admission's transaction (an unwritable row rolls the admission back)."""
    from permissions import department_scope

    from services.audit_service import log_audit_event

    meta = dict(request_meta or {})
    review = facts["review"]
    for c in facts["reviewed"]:
        a, b = sorted((patient_id, c["patient_id"]))
        db.execute(
            """INSERT INTO patient_duplicate_candidates (patient_id, candidate_patient_id,
                   score, reasons_json, status, source, detected_at, detected_by,
                   overridden_by, override_reason)
               VALUES (?, ?, ?, ?, 'open', 'admission', ?, ?, ?, ?)
               ON CONFLICT(patient_id, candidate_patient_id) DO NOTHING""",
            (
                a,
                b,
                int(c["score"]),
                json.dumps(c["reasons"]),
                now,
                user["id"],
                user["id"],
                review["reason"] if review else None,
            ),
        )

    def audit(action: str, summary: str, metadata: dict) -> None:
        log_audit_event(
            correlation_id=f"admission-identity-{admission_id}",
            event_category="patient_identity",
            action_type=action,
            outcome="success",
            db=db,
            summary_message=summary,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            target_type="patient",
            target_id=str(patient_id),
            patient_id=patient_id,
            request_path=meta.get("request_path"),
            http_method=meta.get("http_method"),
            ip_address=meta.get("ip_address"),
            user_agent=meta.get("user_agent"),
            metadata={
                "department": department_scope(user),
                "door": plan["door"],
                "admission_id": admission_id,
                **metadata,
            },
        )

    scan = facts["scan"]
    audit(
        "admission_identity_checked",
        f"Patient {patient_id} created at an admission door after a patient index check",
        {
            "uhid_source": plan["uhid_source"],
            "mode": facts["mode"],
            "duplicate_review": facts["review_used"],
            "candidate_scan_truncated": scan["truncated"],
            "check_incomplete": scan["check_incomplete"],
        },
    )
    if facts["review_used"]:
        audit(
            "admission_duplicate_override",
            (
                f"Admitted {patient_id} past {len(facts['reviewed'])} possible duplicate(s)"
                if facts["reviewed"]
                else f"Admitted {patient_id} past a duplicate check that could not compare every "
                "record"
            ),
            {
                "mode": facts["mode"],
                "reason": review["reason"] if review else None,
                "candidate_ids": [c["patient_id"] for c in facts["reviewed"]],
                "scores": [c["score"] for c in facts["reviewed"]],
                "bands": [c["band"] for c in facts["reviewed"]],
                **scan,
            },
        )


def _audit_door_refusal(refusal: _DoorRefusal, user: dict, request_meta: dict | None) -> None:
    """The disclosure record of an index refusal, on its own write and
    fail-closed: AuditWriteError reaches the route (503) and nobody is shown."""
    import uuid as _uuid

    from permissions import department_scope

    from services.audit_service import log_audit_event

    meta = dict(request_meta or {})
    with db_session() as db:
        log_audit_event(
            correlation_id=str(_uuid.uuid4()),
            event_category="patient_identity",
            action_type="admission_index_refused",
            outcome="denied",
            db=db,
            summary_message=f"Admission refused at the patient index: "
            f"{refusal.metadata.get('reason_code')}",
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            target_type="patient_index",
            target_id=None,
            patient_id=None,
            request_path=meta.get("request_path"),
            http_method=meta.get("http_method"),
            ip_address=meta.get("ip_address"),
            user_agent=meta.get("user_agent"),
            metadata={"department": department_scope(user), **refusal.metadata},
        )


def _location_warning(target: dict) -> str | None:
    """Ruling F104 until a site turns strict on: a ward or bed not in bed
    setup is admitted unlinked, and said so."""
    if target["ward"] is None and target["ward_text"]:
        return (
            f"Ward {target['ward_text']} is not in bed setup, so this admission is not "
            "linked to a bed. A bed manager can link it once the ward is set up."
        )
    if target["ward"] is not None and target["bed"] is None and target["bed_text"]:
        return (
            f"Bed {target['bed_text']} on {target['ward_text']} is not in bed setup, so "
            "this admission is not linked to a bed. A bed manager can link it once the "
            "bed is set up."
        )
    return None


# ── The rows every admission writes, shared by both writers ─────────────────
#
# admit_patient (a new patient) and admit_existing_patient (a registered
# outpatient, opd-17a) write the portal login, the case room and its system
# line through these, so neither can drift from the other. The case room has
# ONE writer in the codebase (tests/test_stay_keying.py pins it): a patient
# keeps one room for life, and readers fetch it by patient_id alone.


def _insert_portal_login_on(db, *, user_id: str, name: str, department, password_hash: str) -> None:
    """The patient's Bridge portal login. Only the password's hash is stored."""
    db.execute(
        "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, 'patient', ?, ?)",
        (user_id, name, department, password_hash),
    )


def _insert_case_room_on(db, *, channel_id: str, room_name: str, patient_id: str, now: str) -> None:
    """The patient's Relay case room. Call it only after _case_room_of found none."""
    db.execute(
        "INSERT INTO channels (id, name, type, patient_id, created_at) VALUES (?, ?, 'case_room', ?, ?)",
        (channel_id, room_name, patient_id, now),
    )


def _case_room_of(db, patient_id: str) -> str | None:
    """The patient's existing case room, the oldest when (wrongly) there are two."""
    row = db.execute(
        "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room' "
        "ORDER BY created_at, id LIMIT 1",
        (patient_id,),
    ).fetchone()
    return row["id"] if row else None


def _post_system_line_on(db, *, channel_id: str | None, content: str, now: str) -> None:
    """A system line in a case room.

    messages.timestamp is written in SQLite's own UTC form, the one its
    CURRENT_TIMESTAMP default gives every other message in the room. The ISO
    `now` sorted after them as text and pinned this line to the top."""
    db.execute(
        """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, timestamp, is_bot, source)
           VALUES (?, 'system', 'Orb System', 'system', ?, 'system', ?, 1, 'system')""",
        (channel_id, content, datetime.fromisoformat(now).strftime("%Y-%m-%d %H:%M:%S")),
    )


def admit_patient(
    data: dict,
    user: dict,
    *,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
    door: str = DOOR_ADMISSION_FORM,
    clerk_entry: bool = False,
) -> dict:
    """Admit a patient through one of the admission doors (`door`: the
    admission form or Scribe's quick admission). The transaction is _admit's.

    Here, whatever the switches say, a hospital number another record already
    holds is AdmissionRefused 'mrn_in_use' (409, naming nobody), including the
    race where two admissions with one typed number pass their checks at once
    and the UNIQUE constraint refuses the second. With registration on, an
    index refusal (section 'The admission doors join the patient index') has
    its disclosure audited on a write of its own before it is raised; if that
    row cannot be written the caller gets AuditWriteError (503) and nobody is
    shown.

    `clerk_entry` (adt-17b, ORB_ADT on only) is the ward clerk's
    registration-level admission: see 'The ward clerk's admission' below."""
    from db_backend import DBIntegrityError

    try:
        return _admit(
            data,
            user,
            idempotency_key=idempotency_key,
            request_meta=request_meta,
            door=door,
            clerk_entry=clerk_entry,
        )
    except _DoorRefusal as refusal:
        _audit_door_refusal(refusal, user, request_meta)
        raise refusal.refused from None
    except DBIntegrityError as exc:
        if "patients.mrn" in str(exc):
            raise _mrn_in_use() from exc
        raise


def _admit(
    data: dict,
    user: dict,
    *,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
    door: str = DOOR_ADMISSION_FORM,
    clerk_entry: bool = False,
) -> dict:
    """
    Transactional admission orchestrator.
    Creates records in patients, admissions, patient_profiles, channels, messages, and sage_room_memory.

    With ORB_ADT on (adt-5) the same transaction is an immediate one and also
    places the patient through the bed master (see the section above): the
    'admission' movement, its patient_movement audit and any boarder line
    commit with the patient or not at all, an Idempotency-Key replay returns
    the first admission, and the answer adds admitted_at, movement_id,
    location and location_warning. With the module off, idempotency_key,
    request_meta and data['terminal_clean_authoriser_pin'] are ignored and
    nothing differs from before.

    With ORB_REGISTRATION on (opd-5b) the transaction is immediate too, the
    hospital number follows ORB_UHID_SOURCE, the patient index is searched
    before a row is written, and the new patient's match keys and the
    patient_identity record of the check commit with the admission; the answer
    adds 'index_check'. data['duplicate_review'] is read only then.
    """
    # THE registration name rule (services/patient_name_rule.py). A provided
    # name with fewer than two letters raises PatientNameError here, before a
    # single row is written; the admit route maps it to HTTP 400. A caller
    # that passes no name at all keeps the historical placeholder, which the
    # route never does (AdmissionRequest.name is required).
    raw_name = data.get("name")
    name = "Unknown Patient" if raw_name is None else validate_patient_name(raw_name)

    # A signing PIN typed for a terminal-clean authoriser (ORB_ADT only) is
    # taken out of the admission's fields at once, so nothing below can store
    # or print it; with the module off it is dropped unread.
    data = dict(data)
    authoriser_pin = data.pop("terminal_clean_authoriser_pin", None)

    # The ward clerk's registration-level admission (adt-17b, ruling ADT-S5
    # (a)): every clinical field is overwritten here as not yet assessed,
    # whatever the caller sent, so no lane can store a clerk-typed allergy,
    # a 'no known allergies' or a code status. Only with ORB_ADT on.
    if clerk_entry:
        if not _adt_admitting():
            from services.adt.core import AdtError

            raise AdtError(
                "The ward clerk admits through Beds and admissions, which is switched "
                "off on this site.",
                code="module_disabled",
            )
        data.update(_clerk_admission_fields())

    # The patient index (opd-5b, ORB_REGISTRATION): {'on': False} while it is
    # off. Checked first, so a hospital number the site's rule refuses is said
    # before a signing PIN is checked or a session opens.
    door_plan = _door_identity_plan(data, door)

    # Beds and admissions (ORB_ADT): None while the module is off.
    adt_plan = (
        _adt_admission_plan(data, idempotency_key, user, request_meta, authoriser_pin)
        if _adt_admitting()
        else None
    )
    authoriser_pin = None

    # multisite-3: the facility this admission is at; None (nothing read)
    # while one facility is on record. Refusals are raised here, before any
    # session of the admission opens.
    admission_facility_id = admission_facility(
        data.get("department"),
        user,
        lane="clerk_admission" if clerk_entry else door,
        request_meta=request_meta,
    )

    now = datetime.now(UTC).isoformat()
    admitting_user_id = user["id"]
    admitting_user_name = user.get("name", "Unknown")

    # Bridge portal login for the patient: a UNIQUE one-time password (never a
    # shared default), returned once so the clinician can hand it over.
    import secrets

    from auth import hash_password

    patient_user_id = f"P{uuid.uuid4().hex[:6].upper()}"
    temp_password = secrets.token_urlsafe(9)

    # Generate unique IDs
    patient_id = f"PAT-{str(uuid.uuid4())[:8].upper()}"
    mrn = data.get("mrn") or f"MRN-{str(uuid.uuid4())[:6].upper()}"
    date_of_birth = _record_dob(data.get("date_of_birth"))
    admission_id = f"ADM-{str(uuid.uuid4())[:8].upper()}"
    channel_id = f"chan_{uuid.uuid4().hex[:12]}"

    # What the rows and the case-room lines say about where the patient is.
    # With ORB_ADT off these are the typed values, exactly as before; with it
    # on the location writer fills the rows and the lines use the master's
    # spelling of the checked place.
    row_ward, row_bed = data.get("ward"), data.get("bed")
    portal_ward = data.get("ward", "General")
    summary_ward, summary_bed = data.get("ward", "Unassigned"), data.get("bed", "Unassigned")
    sage_ward = data.get("ward")
    admission_date = now
    adt_prepared = None

    # Immediate only with ORB_ADT or ORB_REGISTRATION on, so the bed check, the
    # patient index check and the write see the same state: two admissions to
    # one bed give one admission and one 409, and two admissions of one person
    # give one chart and one 409.
    with db_session(immediate=adt_plan is not None or door_plan["on"]) as db:
        if adt_plan is not None:
            # A replay answers the first admission and writes nothing, so it
            # comes before every check (its own patient would match itself).
            replay = _adt_replay_on(db, adt_plan, user, name)
            if replay is not None:
                return replay
        door_facts = (
            _door_index_check_on(
                db, door_plan, name=name, data=data, date_of_birth=date_of_birth, user=user
            )
            if door_plan["on"]
            else None
        )
        if door_facts is not None:
            mrn = door_facts["mrn"]
        elif (
            data.get("mrn")
            and db.execute("SELECT 1 FROM patients WHERE mrn = ?", (mrn,)).fetchone() is not None
        ):
            # Always on (opd-5b): a typed number already on a record is a 409
            # naming nobody, never the UNIQUE constraint's raw 500.
            raise _mrn_in_use()
        if adt_plan is None:
            # Checked before this admission writes its own patient row, so the
            # patient being admitted is never counted as the bed's other occupant.
            bed_warning = bed_in_use_warning(db, data.get("ward"), data.get("bed"))
            if bed_warning:
                logger.warning("Admission %s: %s", admission_id, bed_warning)
        else:
            from services.adt.core import prepare_place_on

            # Every placement rule is applied here, before a row about the
            # patient is written; a refusal leaves nothing behind. An occupied
            # bed is refused, never warned (ruling ADT-S1 (a)), and so is a
            # typed place not in bed setup that another admitted patient is
            # recorded at: the module-off warning becomes a refusal, so the
            # answer's bed_warning is always None here.
            bed_warning = None
            adt_prepared = prepare_place_on(
                db,
                admission_id=None,
                actor=user,
                movement_type="admission",
                ward_id=adt_plan["ward_id"],
                bed_id=adt_plan["bed_id"],
                ward_text=adt_plan["ward_text"],
                bed_text=adt_plan["bed_text"],
                extra_bed=adt_plan["extra_bed"],
                out_of_service_reason=adt_plan["out_of_service_reason"],
                not_clean_reason=adt_plan["not_clean_reason"],
                terminal_clean_authorised_by=adt_plan["terminal_clean_authorised_by"],
                terminal_clean_reason=adt_plan["terminal_clean_reason"],
                request_meta=request_meta,
                treating_department=data.get("department"),
                terminal_clean_confirmed=adt_plan["terminal_clean_confirmed"],
            )
            place = adt_prepared["target"]
            # The location writer fills both mirrors from the checked place.
            row_ward, row_bed = None, None
            portal_ward = place["ward_text"] or portal_ward
            summary_ward = place["ward_text"] or "Not chosen yet"
            summary_bed = place["bed_text"] or "Awaiting a bed"
            sage_ward = place["ward_text"] or "a ward not yet chosen"
            admission_date = adt_plan["admitted"].isoformat()

        # 0. Create the patient's Bridge portal login (unique one-time password).
        _insert_portal_login_on(
            db,
            user_id=patient_user_id,
            name=name,
            department=portal_ward,
            password_hash=hash_password(temp_password),
        )
        # 1. Create Patient Master Record (Identity), linked to the login user.
        db.execute(
            """INSERT INTO patients (
                id, user_id, name, mrn, age, date_of_birth, sex, phone,
                emergency_contact_name, emergency_contact_phone, address,
                ward, bed, diagnosis, attending_doctor_id, admission_date,
                created_at, status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted')""",
            (
                patient_id,
                patient_user_id,
                name,
                mrn,
                data.get("age"),
                # Optional and never blocking (see _record_dob). It is the second
                # factor on the patient's discharge link: without it the discharge
                # desk gets a stated refusal instead of an unopenable QR code.
                date_of_birth,
                data.get("sex"),
                data.get("phone"),
                data.get("emergency_contact_name"),
                data.get("emergency_contact_phone"),
                data.get("address"),
                # Mirror ward/bed/diagnosis onto the patients row — Vigil, Sage and the
                # discharge summary read these from `patients`, not `admissions`.
                # (With ORB_ADT on the location writer fills ward and bed below.)
                row_ward,
                row_bed,
                data.get("initial_diagnosis") or data.get("chief_complaint"),
                data.get("attending_doctor_id"),
                admission_date,
                now,
            ),
        )
        if door_facts is not None:
            # The new record's match keys, from the values just stored, so the
            # next door or desk finds this patient without a resync.
            from services import patient_matching as _pm

            _pm.upsert_keys_on(
                db,
                patient_id,
                {
                    "name": name,
                    "phone": data.get("phone"),
                    "date_of_birth": date_of_birth,
                    "sex": data.get("sex"),
                },
            )

        # 2. Create Admission Record
        db.execute(
            """INSERT INTO admissions (
                id, patient_id, admitted_by, admission_source, admission_type,
                department, ward, bed, attending_doctor_id, referring_clinician,
                chief_complaint, initial_diagnosis, triage_severity,
                admitting_notes, nursing_notes, isolation_precautions, code_status,
                initial_vitals_json, status, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (
                admission_id,
                patient_id,
                admitting_user_id,
                data.get("admission_source"),
                data.get("admission_type"),
                data.get("department"),
                row_ward,
                row_bed,
                data.get("attending_doctor_id"),
                data.get("referring_clinician"),
                data.get("chief_complaint"),
                data.get("initial_diagnosis"),
                data.get("triage_severity"),
                data.get("admitting_notes"),
                data.get("nursing_notes"),
                json.dumps(data.get("isolation_precautions", [])),
                data.get("code_status", "full_code"),
                json.dumps(data.get("vitals")) if data.get("vitals") else None,
                now,
            ),
        )
        _stamp_admission_facility_on(
            db,
            admission_id=admission_id,
            patient_id=patient_id,
            facility_id=admission_facility_id,
            new_patient=True,
        )
        if adt_plan is not None:
            # When the stay began, which may be up to ADT_BACKTIME_HOURS back
            # (ruling ADT-P3); created_at stays the time it was recorded.
            db.execute(
                "UPDATE admissions SET admitted_at = ? WHERE id = ?",
                (adt_plan["admitted_iso"], admission_id),
            )

        # 3. Create Patient Profile (Longitudinal Data)
        db.execute(
            """INSERT INTO patient_profiles (
                patient_id, comorbidities, past_medical_history,
                past_surgical_history, allergies, current_medications,
                family_history, social_history, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                patient_id,
                json.dumps(data.get("comorbidities", [])),
                data.get("past_medical_history"),
                data.get("past_surgical_history"),
                json.dumps(data.get("allergies", [])),
                # None on an Orb Ward Record computer, whose drug chart stays
                # on paper: the form has no such field there (services/drug_chart).
                json.dumps(data.get("current_medications", []) if _drug_chart_enabled() else []),
                data.get("family_history"),
                data.get("social_history"),
                now,
            ),
        )

        # 3b. Chart the allergy chips as structured allergies. The narrative
        # above is for reading; patient_allergies is what the order-time and
        # bedside interlocks screen against, so a chip kept only in the profile
        # let a penicillin-allergic patient be ordered amoxicillin unchallenged.
        # Same handle, so the allergy and the admission commit together.
        from services.clinical_records_service import (
            allergy_entries,
            record_allergies_on,
        )

        record_allergies_on(
            db,
            patient_id,
            allergy_entries(data.get("allergies") or []),
            source="admission",
            recorded_by=admitting_user_id,
            recorded_at=now,
        )

        # 4. Insert Initial Vitals (if provided) to trigger Vigil visibility immediately
        vitals = data.get("vitals", {})
        if vitals and (vitals.get("hr") or vitals.get("bp_sys") or vitals.get("spo2")):
            # Through the manual-entry seam — the one writer for vitals a human
            # typed — so an admission set carries the same provenance contract
            # as a nurse-charted one (a label classified MANUAL, no wire event)
            # and is counted on the same source-health surface. It writes inside
            # THIS transaction and raises into it: see
            # ManualEntrySource.record_at_admission. on_oxygen is persisted so
            # NEWS2 scores the +2 supplemental-oxygen term from recorded data.
            manual_entry_source_or_standin().record_at_admission(
                db,
                patient_id,
                {
                    "HR": vitals.get("hr"),
                    "SBP": vitals.get("bp_sys"),
                    "DBP": vitals.get("bp_dia"),
                    "SpO2": vitals.get("spo2"),
                    "Temp": vitals.get("temperature"),
                    "RR": vitals.get("rr"),
                },
                observed_at=now,
                # As supplied, None when the form left them out (ruled
                # 2026-10-03): never a default 'alert' / on air.
                consciousness=vitals.get("consciousness"),
                on_oxygen=(
                    None if vitals.get("on_oxygen") is None else bool(vitals.get("on_oxygen"))
                ),
            )
            # Inline an initial NEWS2 risk score so the patient shows a real risk in
            # Vigil immediately (the simulator bot won't seed a vitals-less patient).
            # Not while scoring is switched off on this computer
            # (ORB_SCORING_ENABLED, the Orb Ward Record): no score is computed,
            # shown or logged, so the admission set is stored and nothing else,
            # the same as the observation write path.
            try:
                from services.vigil_service import scoring_enabled as _scoring_enabled

                if not _scoring_enabled():
                    raise _ScoringOff()
                from services.vigil_service import score_vitals_row

                # On the patient's recorded SpO2 scale, read back from the row
                # inserted above on THIS connection. Admission has no Scale 2
                # input today (gap register X003), so this reads 0, but a
                # baseline that assumes Scale 1 would quietly disagree with
                # every other lane the day one is added. overlay=False: this is
                # inside the admission's open write transaction, where the
                # overlay's second connection is the documented WAL self-deadlock.
                _prow = db.execute(
                    "SELECT on_scale_2 FROM patients WHERE id = ?", (patient_id,)
                ).fetchone()
                _risk = score_vitals_row(
                    {
                        "heart_rate": vitals.get("hr"),
                        "systolic_bp": vitals.get("bp_sys"),
                        "respiratory_rate": vitals.get("rr"),
                        "temperature": vitals.get("temperature"),
                        "spo2": vitals.get("spo2"),
                        "consciousness": vitals.get("consciousness"),
                        "on_oxygen": (
                            None
                            if vitals.get("on_oxygen") is None
                            else bool(vitals.get("on_oxygen"))
                        ),
                    },
                    on_scale_2=bool(_prow and _prow["on_scale_2"]),
                    overlay=False,
                )
                assert _risk is not None
                # Stamped with the admission instant the vitals row above
                # carries, in the one stored spelling (services/clinical_time.py).
                # The column's CURRENT_TIMESTAMP default wrote "... HH:MM:SS",
                # which every ISO row outranked on a text ORDER BY.
                db.execute(
                    "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        patient_id,
                        to_utc_iso(now),
                        _risk["score"],
                        _risk["level"],
                        json.dumps(_risk["drivers"]),
                    ),
                )
            except _ScoringOff:
                pass
            except Exception as e:
                # Non-fatal: admission still succeeds without a seeded baseline
                # risk score, but log it so the gap is visible rather than silent.
                logger.warning("Baseline risk-score seeding failed for %s: %s", patient_id, e)

        # 5. Initialize Relay (Case Room)
        room_name = f"Case: {data.get('name', 'Unknown')}"
        _insert_case_room_on(
            db, channel_id=channel_id, room_name=room_name, patient_id=patient_id, now=now
        )

        # Build structured initial system message for Relay
        summary_lines = [
            f"**ADMISSION**: {data.get('name')} ({data.get('age', '?')}y {data.get('sex', '?')})",
            f"Ward: {summary_ward} | Bed: {summary_bed}",
            f"Source: {data.get('admission_source', 'Direct')} | Type: {data.get('admission_type', 'Inpatient')}",
            f"Chief Complaint: {data.get('chief_complaint', 'Not provided')}",
            f"Initial Diagnosis: {data.get('initial_diagnosis', 'Pending')}",
        ]
        if data.get("allergies"):
            summary_lines.append(f"Allergies: {', '.join(data['allergies'])}")
        if data.get("comorbidities"):
            summary_lines.append(f"Comorbidities: {', '.join(data['comorbidities'])}")
        if clerk_entry:
            # adt-17b: said in the room the treating team reads first. Never
            # 'no known allergies': nobody has asked yet.
            summary_lines = summary_lines[:3] + _clerk_admission_lines()

        initial_msg = "\n".join(summary_lines)

        _post_system_line_on(db, channel_id=channel_id, content=initial_msg, now=now)

        # 6. Initialize Sage Room Memory
        sage_patient_summary = f"{data.get('name')}, {data.get('age', '?')}y {data.get('sex', '?')} admitted to {sage_ward} for {data.get('initial_diagnosis')}."
        if clerk_entry:
            sage_patient_summary = (
                f"{data.get('name')}, {data.get('age', '?')}y {data.get('sex', '?')} admitted to "
                f"{sage_ward} by the ward clerk. The presenting complaint, allergies and every "
                "other clinical detail are not yet assessed: allergies are unknown, not none."
            )
        if data.get("comorbidities"):
            sage_patient_summary += f" PMH: {', '.join(data['comorbidities'])}."

        sage_discussion_summary = f"Patient admitted via {data.get('admission_source')}. {data.get('admitting_notes', '')}"
        if clerk_entry:
            sage_discussion_summary = (
                f"Patient admitted via {data.get('admission_source')} by the "
                "ward clerk. Admission assessment due."
            )

        # Initialize risk flags for Sage based on triage and infection/fall risks
        risk_flags = []
        if data.get("triage_severity") and int(data["triage_severity"]) <= 2:
            risk_flags.append(
                {
                    "detail": f"High acuity admission (Triage {data.get('triage_severity')})",
                    "confidence": 1.0,
                }
            )

        db.execute(
            """INSERT INTO sage_room_memory (
                room_id, patient_id, patient_summary, discussion_summary,
                current_concern, risk_flags, participants,
                message_count, llm_call_count, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
            (
                channel_id,
                patient_id,
                sage_patient_summary[:1000],
                sage_discussion_summary[:1000],
                data.get("chief_complaint", "")[:200],
                json.dumps(risk_flags),
                json.dumps([admitting_user_name]),
                now,
                now,
            ),
        )

        if door_facts is not None:
            # The worklist rows and the fail-closed patient_identity record of
            # the index check, with the admission or not at all.
            _door_record_on(
                db,
                door_facts,
                door_plan,
                patient_id=patient_id,
                admission_id=admission_id,
                user=user,
                now=now,
                request_meta=request_meta,
            )

        placed = None
        if adt_plan is not None:
            assert adt_prepared is not None
            from services.adt.core import place_admission_on

            if clerk_entry:
                # adt-17b: the marker the bed board reads to flag 'Admission
                # assessment due', on the admission movement itself.
                adt_prepared["detail"][_CLERK_ADMISSION_KEY] = {
                    "clinical_fields": "not_yet_assessed",
                    "entered_by_role": user.get("role"),
                }
            # Last, so the case room exists for a boarder line and every row
            # above commits with the movement and its fail-closed audit, or
            # none of them does.
            placed = place_admission_on(
                db,
                admission_id=admission_id,
                actor=user,
                movement_type="admission",
                reason=(
                    "Patient admitted by the ward clerk (registration only)"
                    if clerk_entry
                    else "Patient admitted"
                ),
                prepared=adt_prepared,
                boarder_acknowledged=adt_plan["boarder_acknowledged"],
                moved_at=adt_plan["admitted_iso"],
                idempotency_key=adt_plan["key"],
                request_meta=request_meta,
            )
            if clerk_entry:
                _audit_clerk_admission_on(
                    db,
                    user=user,
                    patient_id=patient_id,
                    admission_id=admission_id,
                    movement_id=placed["movement"]["id"],
                    department=data.get("department"),
                    request_meta=request_meta,
                )

    adt_answer = {}
    if placed is not None:
        assert adt_prepared is not None and adt_plan is not None
        extra = adt_prepared["detail"].get("extra_bed") or {}
        adt_answer = {
            "replayed": False,
            "admitted_at": adt_plan["admitted_iso"] if adt_plan is not None else None,
            "movement_id": placed["movement"]["id"],
            "location": {
                "ward_id": placed["ward_id"],
                "bed_id": placed["bed_id"],
                "ward": placed["ward"],
                "bed": placed["bed"],
                "linked": placed["linked"],
                "awaiting_bed": placed["bed_id"] is None and not placed["bed"],
                "boarder": placed["boarder"],
                "extra_bed": extra.get("label"),
            },
            "location_warning": _location_warning(adt_prepared["target"]),
        }

    return {
        "success": True,
        "patient_id": patient_id,
        "admission_id": admission_id,
        "channel_id": channel_id,
        "mrn": mrn,
        # Returned exactly once — the caller must surface it to the patient; only
        # the hash is stored, so it cannot be retrieved again.
        "patient_login_id": patient_user_id,
        "temporary_password": temp_password,
        # Whether this patient can be handed a secure discharge link when they
        # leave. False is not an error and never blocked the admission — it is a
        # piece of paperwork the ward can still complete, surfaced at the moment
        # someone could act on it rather than at the discharge desk.
        "date_of_birth_recorded": date_of_birth is not None,
        # None, or the sentence saying this bed was already held by an admitted
        # patient. The admission stands either way (bed_in_use_warning).
        "bed_warning": bed_warning,
        # With ORB_ADT on: when the stay began, the admission movement and
        # where the patient was placed. Empty, so absent, while it is off.
        **adt_answer,
        # With ORB_REGISTRATION on: how the hospital number was set and any
        # duplicate review the clinician admitted past. Absent while it is off.
        **(
            {
                "index_check": {
                    "uhid_source": door_plan["uhid_source"],
                    "duplicate_review": (
                        {
                            "reason": door_facts["review"]["reason"]
                            if door_facts["review"]
                            else None,
                            "candidate_ids": [c["patient_id"] for c in door_facts["reviewed"]],
                            "check_incomplete": door_facts["scan"]["check_incomplete"],
                        }
                        if door_facts["review_used"]
                        else None
                    ),
                }
            }
            if door_facts is not None
            else {}
        ),
        # adt-17b: only on the ward clerk's admission.
        **(
            {"clerk_admission": True, "assessment_due": True, "clinical_fields": _NOT_YET_ASSESSED}
            if clerk_entry
            else {}
        ),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  The ward clerk's admission (adt-17b, HIS wave 9, W9-A; ORB_ADT, default off)
# ═════════════════════════════════════════════════════════════════════════════
#
# Ruling ADT-S5 (a), applied as a build default (the full statement is in
# services/adt/clerk.py): a ward clerk makes a REGISTRATION-LEVEL admission.
# admit_patient(..., clerk_entry=True) runs the same one transaction as the
# admission form, with the module on only, and:
#   * overwrites every clinical field before anything is read from it
#     (_clerk_admission_fields): the presenting complaint is 'Not yet
#     assessed'; no diagnosis, triage, history, current medicine, observation,
#     note, isolation precaution or code status is stored (code_status is
#     NULL, which every reader prints as 'Not recorded'); NO allergy is
#     charted and no allergy review is written, so the interlocks see what
#     they see for any patient with nothing charted: unknown, never 'no known
#     allergies';
#   * marks the admission movement (detail 'clerk_admission'), which the bed
#     board reads to flag 'Admission assessment due' until a doctor records
#     the admission assessment (services/adt/clerk.record_admission_assessment);
#   * says so in the case room and in Sage's room memory;
#   * writes a fail-closed clinical_access 'clerk_admission_recorded' row in
#     the same transaction.
# The route (POST /api/admissions/clerk-admit) also refuses every clinical
# field in the body (extra fields are 422), forces the clerk's own department
# and an attending doctor of that department, and needs the hospital's own
# number unless the site's registration issues it.

_NOT_YET_ASSESSED = "Not yet assessed"
_CLERK_ADMISSION_KEY = "clerk_admission"


def _clerk_admission_fields() -> dict:
    """The clinical fields of a ward clerk's admission: not yet assessed."""
    return {
        "chief_complaint": _NOT_YET_ASSESSED,
        "initial_diagnosis": None,
        "triage_severity": None,
        "comorbidities": [],
        "past_medical_history": None,
        "past_surgical_history": None,
        "allergies": [],
        "current_medications": [],
        "family_history": None,
        "social_history": None,
        "vitals": None,
        "admitting_notes": None,
        "nursing_notes": None,
        "isolation_precautions": [],
        "code_status": None,
    }


def _clerk_admission_lines() -> list[str]:
    """The case room's admission lines for a ward clerk's admission."""
    return [
        f"Presenting complaint: {_NOT_YET_ASSESSED}",
        f"Diagnosis: {_NOT_YET_ASSESSED}",
        (
            f"Allergies: {_NOT_YET_ASSESSED}. Nobody has asked yet, so this is not 'no known "
            "allergies'."
        ),
        "Admitted by the ward clerk (registration only). Admission assessment due: a doctor "
        "records the allergies, the presenting complaint and the code status.",
    ]


def _audit_clerk_admission_on(
    db,
    *,
    user: dict,
    patient_id: str,
    admission_id: str,
    movement_id: str,
    department,
    request_meta,
) -> None:
    """clinical_access 'clerk_admission_recorded', fail-closed, in the
    admission's own transaction."""
    from services.adt.core import audit_on

    audit_on(
        db,
        category="clinical_access",
        action="clerk_admission_recorded",
        summary=(
            f"Admission {admission_id} entered by the ward clerk at registration level; "
            "clinical fields not yet assessed"
        ),
        actor=user,
        patient_id=patient_id,
        target_type="admission",
        target_id=admission_id,
        request_meta=request_meta,
        metadata={
            "movement_id": movement_id,
            "department": department,
            "clinical_fields": "not_yet_assessed",
            "allergies_recorded": False,
            "assessment_due": True,
        },
    )


def clerk_admission_choices(user: dict) -> dict:
    """What the ward clerk's admission form chooses from: the clerk's own
    department (the only one it admits to), the attending doctors of that
    department, and bed setup's wards and beds (the admission form's
    'master' block). No patient data. Only with ORB_ADT on; the department
    is None when the clerk's department has no clinical cover."""
    from permissions import department_scope

    from services.frontdesk_service import list_covering_departments

    own = department_scope(user)
    covering = list_covering_departments()
    department = next((d for d in covering if own and d.casefold() == own.casefold()), None)
    doctors = [
        d
        for d in attending_doctors()
        if department is not None
        and d.get("department")
        and d["department"].casefold() == department.casefold()
    ]
    import config

    return {
        "department": department,
        "departments": [department] if department else [],
        "default_department": department,
        "doctors": doctors,
        "master": _master_choices(user),
        "clinical_fields": _NOT_YET_ASSESSED,
        # True when this site's registration issues the hospital number
        # (ORB_REGISTRATION with ORB_UHID_SOURCE 'orb'): the form then asks for
        # none, and a typed one is refused. Otherwise the clerk types it.
        "hospital_number_issued": bool(config.feature_enabled("registration"))
        and config.UHID_SOURCE == "orb",
    }


def clerk_attending(doctor_id, department: str) -> str:
    """The attending id for a ward clerk's admission: an active doctor on the
    list who works in the admitting department. AdmissionInputError otherwise."""
    wanted = require_listed_attending(doctor_id)
    doctor = next((d for d in attending_doctors() if d["id"] == wanted), None)
    if (
        doctor is None
        or not doctor.get("department")
        or doctor["department"].casefold() != (department or "").casefold()
    ):
        raise AdmissionInputError(f"Choose an attending doctor who works in {department}.")
    return wanted


# ═════════════════════════════════════════════════════════════════════════════
#  A registered outpatient becomes an inpatient (opd-17a, W7-B; ORB_OPD)
# ═════════════════════════════════════════════════════════════════════════════
#
# The consulting doctor admits a patient who is on the patient index but has
# never been an inpatient (patients.status 'registered') from their open
# outpatient visit today ('Advise admission' on the OPD tab). The SAME patient
# row and the SAME hospital number are kept, and nothing about who the patient
# is gets typed again, so nothing about it can be typed wrongly.
#
# ONE IMMEDIATE TRANSACTION. The admission runs inside the visit's own
# completion (frontdesk_service.complete_opd_visit and its before_complete
# hook): the visit is finished with disposition 'admitted' and its consultation
# charge is raised there exactly as any finished visit's is (encounter_type
# 'op', no admission id, so the stay's discharge advisory still counts it,
# billing_service.patient_financial_summary). In the same transaction the
# patient becomes 'admitted', the admission row is written (admissions.
# opd_visit_id names the visit and opd_visits.admission_id the admission), the
# profile is created, or filled and never blanked, the portal login is created
# only when the patient has none, the case room is created only when the
# patient has none (an existing one gets a 'new admission' line), allergies the
# doctor adds are charted beside the ones already charted (add-only), and with
# ORB_ADT on the patient is placed through the bed master exactly as the
# admission form places a new patient. The clinical_access record
# 'opd_converted_to_admission' is written in that transaction too
# (fail-closed): without it there is no admission.
#
# RULINGS BUILT STRICTEST, each for Samuel to confirm before go-live:
#   * OPD-S1 (a): a passing stated-identifier check (name; date of birth or
#     age; phone last 4 or UHID) through registration_service.check_identity,
#     before the transaction, on every conversion. No check, or no match, is
#     refused and audited, and nothing is written.
#   * OPD-S3 (a): nothing from outpatients is written to vitals or
#     risk_scores, and this door takes no observations at all (its body
#     refuses a 'vitals' field): the ward charts a fresh set on the
#     observation chart. The OPD values are shown on the form, with their
#     time, for reference only (conversion_preview).
#   * OPD-S4 (a): no outpatient prescription becomes an inpatient medicine;
#     nothing is written to medications or med_orders. Outpatient medicines
#     are listed on the form with their times for the admitting prescriber to
#     reconcile one by one.
#   * OPD-N8 still applies to the visit this finishes: a signed note, or a
#     reason why there is none.
#   * Only an open visit (waiting, or with the doctor) dated today, in the
#     caller's own clinic, can be admitted from. A patient who has been an
#     inpatient before (a 'discharged' patient) is refused here unless
#     readmission (ORB_READMISSION, opd-17c, W8-A) is on; then the same
#     transaction also applies the readmission rules (section 'Readmitting a
#     discharged patient onto the same record' below).
#   * The form's read of a visit (conversion_preview) shows the patient only
#     while they can be admitted from it; a refused visit answers with the
#     visit and the refusal alone, so it never reads past the OPD arm.
# With ORB_OPD off both routes answer 404 and nothing here runs.

#: The conversion's in-transaction record (clinical_access, fail-closed).
CONVERSION_AUDIT_ACTION = "opd_converted_to_admission"
#: The admission form's read of the visit before a conversion (phi_access).
CONVERSION_PREVIEW_AUDIT_ACTION = "opd_admission_preview_viewed"
#: A refused identity check at this door (patient_identity, fail-closed).
CONVERSION_IDENTITY_REFUSED_ACTION = "admission_identity_refused"
#: The visits a patient may be admitted from: still open.
CONVERTIBLE_VISIT_STATUSES = ("waiting", "in_consultation")
#: What the conversion writes as the visit's disposition (migration 50).
ADMITTED_DISPOSITION = "admitted"
#: OPD-N8: the shortest reason for finishing a visit with no signed note.
NO_NOTE_REASON_MIN = 3
_NO_NOTE_REASON_MAX = 500


def _conversion_on() -> bool:
    """True while outpatients (ORB_OPD, with its prerequisites) is on."""
    import config

    return bool(config.feature_enabled("opd"))


def _hospital_today() -> str:
    from services.frontdesk_service import _hospital_day

    return _hospital_day()


def conversion_admission_id(user_id: str, idempotency_key: str) -> str:
    """The admission id a conversion takes from its sender and its
    Idempotency-Key, so a retried send finds the first admission whatever
    the switches say (admissions has no key column). Another person sending
    the same key gets another id."""
    import hashlib

    digest = hashlib.sha256(f"opd-17a\x00{user_id}\x00{idempotency_key}".encode()).hexdigest()
    return f"ADM-{digest[:12].upper()}"


def _table_exists(db, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _json_or(raw, default):
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return default if value is None else value


def _conversion_visit_on(db, visit_id: str, scope_department):
    """The visit, or AdmissionRefused 404 both for no such visit and for
    another clinic's: a 403 would confirm that a patient attended a named
    clinic. `scope_department` is permissions.department_scope(user)."""
    row = None
    if visit_id and _table_exists(db, "opd_visits"):
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
    if row is None or (
        scope_department is not None and (row["department"] or "") != scope_department
    ):
        raise AdmissionRefused("visit_not_found", "Outpatient visit not found.", status=404)
    return row


def _conversion_patient_on(db, patient_id: str):
    return db.execute(
        """SELECT p.id, p.user_id, p.name, p.mrn, p.age, p.date_of_birth, p.sex, p.status,
                  p.created_at, d.age_recorded_on
             FROM patients p LEFT JOIN patient_demographics d ON d.patient_id = p.id
            WHERE p.id = ?""",
        (patient_id,),
    ).fetchone()


def _conversion_refusal_on(db, visit, patient) -> AdmissionRefused | None:
    """Why this visit's patient cannot be admitted from it now, or None."""
    status = visit["status"]
    if status == "cancelled":
        return AdmissionRefused(
            "visit_cancelled",
            "This visit was cancelled, so the patient cannot be admitted from it.",
            status=409,
        )
    if status == "completed":
        return AdmissionRefused(
            "visit_completed",
            "This visit is already finished, so the patient cannot be admitted from it. "
            "Register a new visit at the front desk, then admit the patient from that visit.",
            status=409,
        )
    if status not in CONVERTIBLE_VISIT_STATUSES:
        return AdmissionRefused(
            "visit_closed",
            "This visit cannot be finished, so the patient cannot be admitted from it.",
            status=409,
        )
    if (visit["visit_date"] or "") != _hospital_today():
        return AdmissionRefused(
            "visit_not_current",
            "A patient is admitted from today's visit only. Register today's visit at the "
            "front desk first.",
            status=409,
        )
    if patient is None:
        return AdmissionRefused("visit_not_found", "Outpatient visit not found.", status=404)
    active = db.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
        (patient["id"],),
    ).fetchone()
    if patient["status"] == "admitted" or active is not None:
        return AdmissionRefused(
            "patient_already_admitted",
            "This patient is already admitted. Open their inpatient chart.",
            status=409,
        )
    earlier = db.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? LIMIT 1", (patient["id"],)
    ).fetchone()
    if (patient["status"] != "registered" or earlier is not None) and readmission_on():
        # opd-17c (ORB_READMISSION): a discharged patient is readmitted onto
        # their own record from this visit, under the readmission rules.
        return _readmission_refusal_on(db, patient)
    if patient["status"] != "registered" or earlier is not None:
        return AdmissionRefused(
            "readmission_not_available",
            "This patient has been an inpatient before. Readmitting a patient onto their "
            "earlier record is not available yet, so ask an administrator before admitting "
            "them.",
            status=409,
        )
    return None


def _user_names_on(db, ids) -> dict:
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    return {
        r["id"]: r["name"]
        for r in db.execute(f"SELECT id, name FROM users WHERE id IN ({marks})", wanted).fetchall()
    }


def _carried_from_note(visit, note) -> dict:
    """The complaint and the provisional diagnosis the admission form starts
    from: the consultation's, else the complaint the desk wrote on the visit.
    The doctor may change both before admitting."""
    complaint = _plain(note["chief_complaint"]) if note is not None else None
    diagnoses = _json_or(note["diagnoses_json"], []) if note is not None else []
    texts = [_plain(d.get("text")) for d in diagnoses if isinstance(d, dict)]
    return {
        "chief_complaint": complaint or _plain(visit["chief_complaint"]),
        "initial_diagnosis": "; ".join(t for t in texts if t) or None,
    }


def _conversion_visit_reference(visit) -> dict:
    """The visit itself, as the clinic's queue already lists it: nothing about
    the patient."""
    return {
        "id": visit["id"],
        "visit_date": visit["visit_date"],
        "department": visit["department"],
        "doctor_name": visit["doctor_name"],
        "token_number": visit["token_number"],
        "status": visit["status"],
    }


def conversion_preview(visit_id: str, *, scope_department) -> tuple[dict, str]:
    """What the admission form shows before admitting a registered outpatient
    from this visit (opd-17a), and the patient's id for the read's audit row
    (never in the body of a refusal). Read-only; the route audits it as
    phi_access.

    ONLY AN ADMITTABLE VISIT SHOWS THE PATIENT. While the patient cannot be
    admitted from this visit (finished, cancelled, not today's, admitted
    anywhere, an earlier inpatient) the body is the visit reference, the
    refusal and admittable False, and nothing else: no identity, allergies,
    medicines, observations or note. The visit check alone (same clinic) is
    not the patient's scope: a clinic reads a patient through the OPD arm
    (permissions.opd_scope_decision), which needs a visit that is not
    cancelled inside ORB_OPD_SCOPE_DAYS and no active admission anywhere. An
    admittable visit (open, today, this clinic, no admission) always holds
    that arm open, so the form loses nothing; a refused one may not, and the
    queue lists every visit id of the clinic for any day.

    For an admittable visit identity is shown and never edited: name, UHID,
    age and sex (never the date of birth, which is the discharge link's second
    factor, and never the phone or address). `opd_vitals` are this visit's
    values with their time and `opd_medicines` the patient's live outpatient
    prescriptions with theirs, FOR REFERENCE ONLY (rulings OPD-S3 (a) and
    OPD-S4 (a)): neither is ever written to the inpatient chart. On an Orb
    Ward Record computer, whose drug chart stays on paper, no medicine is
    listed. `refusal` says why the patient cannot be admitted from this visit
    now, in the words the admission itself would refuse with."""
    from services import patient_matching as pm

    with db_session() as db:
        visit = _conversion_visit_on(db, visit_id, scope_department)
        patient = _conversion_patient_on(db, visit["patient_id"])
        if patient is None:
            raise AdmissionRefused("visit_not_found", "Outpatient visit not found.", status=404)
        refusal = _conversion_refusal_on(db, visit, patient)
        if refusal is not None:
            return (
                {
                    "visit": _conversion_visit_reference(visit),
                    "admittable": False,
                    "refusal": refusal.detail(),
                },
                patient["id"],
            )
        note = None
        if _table_exists(db, "opd_consultations"):
            note = db.execute(
                "SELECT status, chief_complaint, diagnoses_json, vitals_json, signed_at "
                "FROM opd_consultations WHERE visit_id = ?",
                (visit["id"],),
            ).fetchone()
        allergies = [
            {"substance": r["substance"], "reaction": r["reaction"], "severity": r["severity"]}
            for r in db.execute(
                "SELECT substance, reaction, severity FROM patient_allergies "
                "WHERE patient_id = ? AND status = 'active' ORDER BY substance",
                (patient["id"],),
            ).fetchall()
        ]
        medicines_listed = _drug_chart_enabled()
        medicines: list[dict] = []
        if medicines_listed and _table_exists(db, "med_orders"):
            from services.medorders.core import LIVE_STATUSES

            marks = ",".join("?" for _ in LIVE_STATUSES)
            medicines = [
                {
                    "drug_name": r["drug_name"],
                    "dose": r["dose"],
                    "route": r["route"],
                    "frequency": r["frequency"],
                    "is_prn": bool(r["is_prn"]),
                    "status": r["status"],
                    "prescribed_at": r["prescribed_at"] or r["created_at"],
                }
                for r in db.execute(
                    f"SELECT drug_name, dose, route, frequency, is_prn, status, prescribed_at, "
                    f"created_at FROM med_orders WHERE patient_id = ? AND encounter_type = 'op' "
                    f"AND status IN ({marks}) ORDER BY COALESCE(prescribed_at, created_at), id",
                    (patient["id"], *LIVE_STATUSES),
                ).fetchall()
            ]
        vitals = _json_or(note["vitals_json"], None) if note is not None else None
        names = _user_names_on(
            db, [(vitals or {}).get("measured_by") if isinstance(vitals, dict) else None]
        )
    age, _stated = pm.current_age(
        date_of_birth=patient["date_of_birth"],
        stated_age=patient["age"],
        age_recorded_on=patient["age_recorded_on"],
        created_at=patient["created_at"],
    )
    opd_vitals = []
    if isinstance(vitals, dict) and isinstance(vitals.get("values"), dict) and vitals["values"]:
        opd_vitals.append(
            {
                "measured_at": vitals.get("measured_at"),
                "measured_by_name": names.get(vitals.get("measured_by")),
                "values": vitals["values"],
            }
        )
    return {
        "visit": _conversion_visit_reference(visit),
        "patient": {
            "id": patient["id"],
            "name": patient["name"],
            "uhid": patient["mrn"],
            "age": age,
            "sex": patient["sex"],
            "portal_login": bool(patient["user_id"]),
            "date_of_birth_on_file": patient["date_of_birth"] is not None,
        },
        "admittable": True,
        "refusal": None,
        "note": (
            {
                "status": note["status"],
                "signed": note["status"] == "signed",
                "signed_at": note["signed_at"],
            }
            if note is not None
            else None
        ),
        "carried": _carried_from_note(visit, note),
        "opd_vitals": opd_vitals,
        "medicines_listed": medicines_listed,
        "opd_medicines": medicines,
        "allergies": allergies,
        "allergy_status": "recorded" if allergies else "none_recorded",
        # opd-17c: a discharged patient readmitted onto their record from this
        # visit. What that does is said, never what the earlier stay held
        # (the clinic reads the patient through the outpatient arm only).
        **(
            {"readmission": True, "readmission_note": READMISSION_FORM_NOTE}
            if patient["status"] != "registered"
            else {}
        ),
    }, patient["id"]


def _audit_conversion_identity_refused(
    patient_id: str,
    user: dict,
    reason_code: str,
    request_meta: dict | None,
    purpose: str = "opd_admission",
) -> None:
    """A conversion (or, with purpose 'readmission', a readmission) refused
    because who the patient is was not confirmed, on the fail-closed
    patient_identity chain, in a write of its own (nothing else was saved).
    AuditWriteError reaches the route as 503."""
    from services.audit_service import log_audit_event

    meta = dict(request_meta or {})
    what = "Readmission of" if purpose == READMISSION_PURPOSE else "Admission of"
    where = "" if purpose == READMISSION_PURPOSE else " from outpatients"
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="patient_identity",
        action_type=CONVERSION_IDENTITY_REFUSED_ACTION,
        outcome="denied",
        summary_message=(
            f"{what} {patient_id}{where} refused: "
            + (
                "no identity check was sent"
                if reason_code == "identity_check_required"
                else "what the patient stated did not match the record"
            )
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type="patient",
        target_id=str(patient_id),
        patient_id=patient_id,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata={"reason_code": reason_code, "purpose": purpose},
    )


def _confirm_conversion_identity(
    patient_id: str,
    stated: dict | None,
    *,
    user: dict,
    scope_department,
    request_meta: dict | None,
    purpose: str = "opd_admission",
) -> dict:
    """Ruling OPD-S1 (a): what the patient states, checked against the record
    by registration_service.check_identity (its own immediate session, so it
    runs before the conversion's). Returns what the conversion's record keeps:
    {result, checked, checked_at, checked_by, purpose, method}, never a stated
    or stored value. RegistrationError (422, 429, 404) reaches the route.
    `purpose` is 'opd_admission' or, for a readmission (opd-17c), 'readmission'."""
    from services import registration_service as reg

    if not stated:
        _audit_conversion_identity_refused(
            patient_id, user, "identity_check_required", request_meta, purpose
        )
        raise AdmissionRefused(
            "identity_check_required",
            "Confirm who the patient is first: ask for their name, their date of birth or "
            "age, and the last 4 digits of their phone or their UHID. Nothing was saved.",
            status=409,
        )
    verdict = reg.check_identity(
        patient_id,
        stated,
        user=user,
        department=scope_department,
        request_meta=request_meta,
        purpose=purpose,
    )
    if verdict.get("result") != "match":
        _audit_conversion_identity_refused(
            patient_id, user, "identity_not_confirmed", request_meta, purpose
        )
        raise AdmissionRefused(
            "identity_not_confirmed",
            "What the patient told you does not match this record. Check you have the right "
            "patient. Nothing was saved.",
            status=409,
        )
    return {
        "result": "match",
        "checked": list(verdict.get("checked") or []),
        "checked_at": verdict.get("checked_at"),
        "checked_by": user.get("id"),
        "purpose": purpose,
        "method": "stated_identifiers",
    }


def _conversion_replay_on(
    db, admission_id: str, visit_id: str, user: dict | None = None
) -> dict | None:
    """The first conversion when this sender already sent this key; None for
    a new key. The same key naming another visit is refused and nothing is
    written. The one-time portal password was shown once and is never shown
    again. A readmission from a visit (opd-17c) adds its own keys, read back
    from its record; `user` (the sender) decides whether they are named."""
    row = db.execute(
        """SELECT a.id, a.patient_id, a.opd_visit_id, a.admitted_at, a.department, p.mrn,
                  p.user_id, p.date_of_birth
             FROM admissions a JOIN patients p ON p.id = a.patient_id
            WHERE a.id = ?""",
        (admission_id,),
    ).fetchone()
    if row is None:
        return None
    if row["opd_visit_id"] != visit_id:
        raise AdmissionRefused(
            "idempotency_key_reused",
            "That Idempotency-Key was already used for another admission. Nothing was saved.",
            status=409,
        )
    visit = db.execute(
        "SELECT department, status, disposition FROM opd_visits WHERE id = ?", (visit_id,)
    ).fetchone()
    return {
        "success": True,
        "replayed": True,
        "patient_id": row["patient_id"],
        "admission_id": row["id"],
        "channel_id": _case_room_of(db, row["patient_id"]),
        "mrn": row["mrn"],
        "patient_login_id": row["user_id"],
        "temporary_password": None,
        "portal_login_created": False,
        "date_of_birth_recorded": row["date_of_birth"] is not None,
        "bed_warning": None,
        "opd_visit_id": visit_id,
        "visit": (
            {
                "id": visit_id,
                "department": visit["department"],
                "status": visit["status"],
                "disposition": visit["disposition"],
            }
            if visit is not None
            else None
        ),
        "admitted_at": row["admitted_at"],
        **(
            _readmission_replay_parts_on(db, row["id"], user, row["department"])
            if user is not None
            else {}
        ),
    }


def _profile_on(db, patient_id: str, data: dict, now: str) -> bool:
    """The patient's profile: created when there is none (True), else filled
    without blanking anything (False): list fields gain the new entries, a
    blank text field takes the new text, and nothing already written is
    replaced. A registered outpatient has none, so it is created."""
    meds = data.get("current_medications", []) if _drug_chart_enabled() else []
    row = db.execute(
        "SELECT * FROM patient_profiles WHERE patient_id = ?", (patient_id,)
    ).fetchone()
    if row is None:
        db.execute(
            """INSERT INTO patient_profiles (
                patient_id, comorbidities, past_medical_history,
                past_surgical_history, allergies, current_medications,
                family_history, social_history, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                patient_id,
                json.dumps(data.get("comorbidities") or []),
                data.get("past_medical_history"),
                data.get("past_surgical_history"),
                json.dumps(data.get("allergies") or []),
                json.dumps(meds or []),
                data.get("family_history"),
                data.get("social_history"),
                now,
            ),
        )
        return True
    changes: dict = {}
    for field, new in (
        ("comorbidities", data.get("comorbidities") or []),
        ("allergies", data.get("allergies") or []),
        ("current_medications", meds or []),
    ):
        old = _json_or(row[field], [])
        if not isinstance(old, list):
            continue  # a free-text narrative is kept exactly as written
        merged = list(old) + [x for x in new if x not in old]
        if merged != old:
            changes[field] = json.dumps(merged)
    for field in (
        "past_medical_history",
        "past_surgical_history",
        "family_history",
        "social_history",
    ):
        if not _plain(row[field]) and _plain(data.get(field)):
            changes[field] = data.get(field)
    if changes:
        sets = ", ".join(f"{k} = ?" for k in changes)
        db.execute(
            f"UPDATE patient_profiles SET {sets}, updated_at = ? WHERE patient_id = ?",
            (*changes.values(), now, patient_id),
        )
    return False


def admit_existing_patient(
    visit_id: str,
    data: dict,
    user: dict,
    *,
    scope_department,
    identity: dict | None,
    idempotency_key: str | None,
    request_meta: dict | None = None,
) -> dict:
    """Admit the registered outpatient of this open visit onto their own
    record (opd-17a). See the section note above for what one call writes and
    refuses.

    `scope_department` is permissions.department_scope(user), derived by the
    route: the visit must be that clinic's (404 otherwise). `identity` is what
    the patient states (OPD-S1). `idempotency_key` is required: a retried send
    returns the first admission (replayed true, no password) and writes
    nothing. `data` holds the admission fields the admission form sends for
    a new patient except identity and observations, plus
    no_signed_note_reason (OPD-N8) and, with ORB_ADT on, the bed picker's.

    Raises AdmissionRefused (404, 409, 422), AdmissionInputError, AdtError
    (ORB_ADT on), registration_service.RegistrationError (from the identity
    check) and AuditWriteError (503: a record that cannot be written means no
    admission)."""
    import secrets

    from auth import hash_password

    from services import patient_matching as pm
    from services.audit_service import log_audit_event
    from services.clinical_records_service import allergy_entries, record_allergies_on
    from services.frontdesk_service import FrontDeskError, complete_opd_visit

    key = _plain(idempotency_key)
    if not key:
        raise AdmissionRefused(
            "idempotency_key_required",
            "Send an Idempotency-Key with the admission, so a retry never admits the patient "
            "twice.",
            status=422,
        )
    visit_id = _plain(visit_id) or ""
    data = dict(data)
    # A signing PIN for a terminal-clean authoriser (ORB_ADT) is taken out at
    # once, so nothing below can store or print it.
    authoriser_pin = data.pop("terminal_clean_authoriser_pin", None)
    reason = _plain(data.pop("no_signed_note_reason", None))
    reason = reason[:_NO_NOTE_REASON_MAX] if reason else None
    admission_id = conversion_admission_id(str(user["id"]), key)

    # 1. The visit, in the caller's clinic, and a replay of this key.
    with db_session() as db:
        visit = _conversion_visit_on(db, visit_id, scope_department)
        replay = _conversion_replay_on(db, admission_id, visit_id, user)
        if replay is not None:
            return replay
        first_read = _conversion_patient_on(db, visit["patient_id"])
        refusal = _conversion_refusal_on(db, visit, first_read)
    if refusal is not None:
        raise refusal
    patient_id = visit["patient_id"]
    # opd-17c (ORB_READMISSION): a discharged patient is readmitted onto
    # their record from this visit; the refusal above let them through only
    # under the readmission rules. Re-read inside the transaction below.
    readmitting = first_read is not None and first_read["status"] != "registered"

    # 2. The bed master's checks that open sessions of their own (ORB_ADT).
    adt_plan = (
        _adt_admission_plan(data, key, user, request_meta, authoriser_pin)
        if _adt_admitting()
        else None
    )
    authoriser_pin = None

    # 3. Who the patient is (OPD-S1), before the transaction.
    from services.registration_service import RegistrationNotFound

    try:
        identity_facts = _confirm_conversion_identity(
            patient_id,
            identity,
            user=user,
            scope_department=scope_department,
            request_meta=request_meta,
            purpose=READMISSION_PURPOSE if readmitting else "opd_admission",
        )
    except RegistrationNotFound:
        # The check hides another ward's inpatient as 'not found'. When the
        # patient was admitted from this visit a moment ago (a second send
        # racing the first), say that instead.
        with db_session() as db:
            replay = _conversion_replay_on(db, admission_id, visit_id, user)
            if replay is not None:
                return replay
            now_refused = _conversion_refusal_on(
                db,
                _conversion_visit_on(db, visit_id, scope_department),
                _conversion_patient_on(db, patient_id),
            )
        if now_refused is not None:
            raise now_refused from None
        raise

    # multisite-3: the facility of this admission (None with one facility on
    # record); refused when the patient's admissions are at another facility.
    admission_facility_id = admission_facility(
        data.get("department"),
        user,
        lane="opd_admission",
        patient_id=patient_id,
        request_meta=request_meta,
    )

    meta = dict(request_meta or {})
    state: dict = {}

    def _convert(db, row) -> None:
        """complete_opd_visit's before_complete: inside its immediate
        transaction, before the visit is finished. Anything raised here rolls
        the visit, the charge and every row below back together."""
        now = datetime.now(UTC).isoformat()
        if scope_department is not None and (row["department"] or "") != scope_department:
            raise AdmissionRefused("visit_not_found", "Outpatient visit not found.", status=404)
        if row["patient_id"] != patient_id:
            raise AdmissionRefused(
                "state_changed",
                "This visit changed while the form was open. Nothing was "
                "saved. Open the patient again.",
                status=409,
            )
        patient = _conversion_patient_on(db, patient_id)
        refused = _conversion_refusal_on(db, row, patient)
        if refused is not None:
            raise refused
        # opd-17c: the identity was checked for the record as it was read
        # before the transaction; a record that changed kind since is refused.
        readmit_now = patient["status"] != "registered"
        if readmit_now != readmitting:
            raise AdmissionRefused(
                "state_changed",
                "This patient's record changed while the form was open. "
                "Nothing was saved. Open the patient again.",
                status=409,
            )
        if adt_plan is not None:
            from services.adt.core import AdtConflict, movement_for_key

            if movement_for_key(db, key) is not None:
                raise AdtConflict(
                    "That Idempotency-Key was already used for another "
                    "admission. Nothing was saved.",
                    code="idempotency_key_reused",
                )
        if readmit_now:
            # opd-17c, ADT-S9 (a): never before the earlier stay ended.
            _check_readmission_time_on(db, patient_id, adt_plan)
        # OPD-N8 on the visit this finishes.
        from services.opd_service import consultation_signed

        signed = consultation_signed(db, row["id"])
        if not signed and (reason is None or len(reason) < NO_NOTE_REASON_MIN):
            raise AdmissionRefused(
                "signed_note_required",
                "Sign the consultation note first, or say why there is none (for example, "
                "the note is on paper).",
                status=409,
            )

        name = patient["name"]
        row_ward, row_bed = data.get("ward"), data.get("bed")
        portal_ward = data.get("ward", "General")
        summary_ward = data.get("ward") or "Unassigned"
        summary_bed = data.get("bed") or "Unassigned"
        sage_ward = data.get("ward")
        admission_date = now
        prepared = None
        if adt_plan is None:
            bed_warning = bed_in_use_warning(db, data.get("ward"), data.get("bed"))
            if bed_warning:
                logger.warning("Admission %s: %s", admission_id, bed_warning)
        else:
            from services.adt.core import prepare_place_on

            # Every placement rule, before a row about the admission exists.
            bed_warning = None
            prepared = prepare_place_on(
                db,
                admission_id=None,
                actor=user,
                movement_type="admission",
                ward_id=adt_plan["ward_id"],
                bed_id=adt_plan["bed_id"],
                ward_text=adt_plan["ward_text"],
                bed_text=adt_plan["bed_text"],
                extra_bed=adt_plan["extra_bed"],
                out_of_service_reason=adt_plan["out_of_service_reason"],
                not_clean_reason=adt_plan["not_clean_reason"],
                terminal_clean_authorised_by=adt_plan["terminal_clean_authorised_by"],
                terminal_clean_reason=adt_plan["terminal_clean_reason"],
                request_meta=request_meta,
                treating_department=data.get("department"),
                terminal_clean_confirmed=adt_plan["terminal_clean_confirmed"],
                patient_id=patient_id,
            )
            place = prepared["target"]
            row_ward, row_bed = None, None
            portal_ward = place["ward_text"] or portal_ward
            summary_ward = place["ward_text"] or "Not chosen yet"
            summary_bed = place["bed_text"] or "Awaiting a bed"
            sage_ward = place["ward_text"] or "a ward not yet chosen"
            admission_date = adt_plan["admitted"].isoformat()

        # The portal login, only when the patient has none: never a second one.
        # A readmission (opd-17c) keeps the login and issues a fresh one-time
        # password (ruling R029 (2)), revoking the earlier stay's sessions.
        login_id = patient["user_id"]
        temp_password = None
        login_created = password_reissued = False
        if readmit_now:
            login_id, temp_password, login_created, password_reissued = (
                _readmission_portal_login_on(
                    db, patient=patient, name=name, portal_ward=portal_ward
                )
            )
        elif not login_id:
            login_id = f"P{uuid.uuid4().hex[:6].upper()}"
            temp_password = secrets.token_urlsafe(9)
            login_created = True
            _insert_portal_login_on(
                db,
                user_id=login_id,
                name=name,
                department=portal_ward,
                password_hash=hash_password(temp_password),
            )

        # The same patient row: only the status, the login and the clinical
        # and location mirrors change. Name, hospital number, age, date of
        # birth, sex and contact details are never written here.
        moved = db.execute(
            """UPDATE patients
                  SET status = 'admitted', user_id = ?, ward = ?, bed = ?, diagnosis = ?,
                      attending_doctor_id = ?, admission_date = ?
                WHERE id = ? AND status = ?""",
            (
                login_id,
                row_ward,
                row_bed,
                data.get("initial_diagnosis") or data.get("chief_complaint"),
                data.get("attending_doctor_id"),
                admission_date,
                patient_id,
                READMITTABLE_STATUS if readmit_now else "registered",
            ),
        )
        if moved.rowcount != 1:
            raise AdmissionRefused(
                "state_changed",
                "This patient's record changed while the form was open. "
                "Nothing was saved. Open the patient again.",
                status=409,
            )

        db.execute(
            """INSERT INTO admissions (
                id, patient_id, admitted_by, admission_source, admission_type,
                department, ward, bed, attending_doctor_id, referring_clinician,
                chief_complaint, initial_diagnosis, triage_severity,
                admitting_notes, nursing_notes, isolation_precautions, code_status,
                initial_vitals_json, status, created_at, opd_visit_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?)""",
            (
                admission_id,
                patient_id,
                user["id"],
                data.get("admission_source"),
                data.get("admission_type"),
                data.get("department"),
                row_ward,
                row_bed,
                data.get("attending_doctor_id"),
                data.get("referring_clinician"),
                data.get("chief_complaint"),
                data.get("initial_diagnosis"),
                data.get("triage_severity"),
                data.get("admitting_notes"),
                data.get("nursing_notes"),
                json.dumps(data.get("isolation_precautions") or []),
                data.get("code_status") or "full_code",
                now,
                row["id"],
            ),
        )
        _stamp_admission_facility_on(
            db,
            admission_id=admission_id,
            patient_id=patient_id,
            facility_id=admission_facility_id,
            new_patient=False,
        )
        if adt_plan is not None:
            db.execute(
                "UPDATE admissions SET admitted_at = ? WHERE id = ?",
                (adt_plan["admitted_iso"], admission_id),
            )
        db.execute("UPDATE opd_visits SET admission_id = ? WHERE id = ?", (admission_id, row["id"]))

        profile_created = _profile_on(db, patient_id, data, now)
        # Add-only: an allergy already charted stays exactly as it is.
        added = record_allergies_on(
            db,
            patient_id,
            allergy_entries(data.get("allergies") or []),
            source="admission",
            recorded_by=user["id"],
            recorded_at=now,
        )
        charted = [
            r["substance"]
            for r in db.execute(
                "SELECT substance FROM patient_allergies WHERE patient_id = ? AND status = 'active' "
                "ORDER BY substance",
                (patient_id,),
            ).fetchall()
        ]

        # opd-17c, ruling OPD-S8 (a): every medicine the earlier stay left on
        # the chart is stopped here, so none can be given in the new stay.
        previous_admission_id = None
        stopped: list[dict] = []
        if readmit_now:
            previous_admission_id = _previous_admission_id_on(db, patient_id)
            stopped = _close_previous_stay_medicines_on(
                db,
                patient_id,
                user=user,
                admission_id=admission_id,
                previous_admission_id=previous_admission_id,
                request_meta=request_meta,
            )

        # One case room per patient for life: reused when there is one.
        channel_id = _case_room_of(db, patient_id)
        room_reused = channel_id is not None
        if not room_reused:
            channel_id = f"chan_{uuid.uuid4().hex[:12]}"
            _insert_case_room_on(
                db, channel_id=channel_id, room_name=f"Case: {name}", patient_id=patient_id, now=now
            )
        age, _stated = pm.current_age(
            date_of_birth=patient["date_of_birth"],
            stated_age=patient["age"],
            age_recorded_on=patient["age_recorded_on"],
            created_at=patient["created_at"],
        )
        heading = "READMISSION" if readmit_now else "NEW ADMISSION" if room_reused else "ADMISSION"
        lines = [
            f"**{heading}**: {name} ({age if age is not None else '?'}y {patient['sex'] or '?'})",
        ]
        if readmit_now:
            lines.append(READMISSION_DIVIDER)
        lines += [
            f"From outpatients: {row['department']} clinic, token {row['token_number']} today",
            f"Ward: {summary_ward} | Bed: {summary_bed}",
            f"Source: {data.get('admission_source') or 'Clinic'} | "
            f"Type: {data.get('admission_type') or 'Inpatient'}",
            f"Chief Complaint: {data.get('chief_complaint') or 'Not provided'}",
            f"Initial Diagnosis: {data.get('initial_diagnosis') or 'Pending'}",
        ]
        if charted:
            lines.append(f"Allergies: {', '.join(charted)}")
        if data.get("comorbidities"):
            lines.append(f"Comorbidities: {', '.join(data['comorbidities'])}")
        if readmit_now:
            lines += _readmission_case_room_lines(stopped)
        lines.append(
            "Outpatient observations and medicines were not carried onto the "
            "inpatient chart: chart a fresh set of observations, and prescribe each "
            "medicine again if it is needed."
        )
        _post_system_line_on(db, channel_id=channel_id, content="\n".join(lines), now=now)

        memory_exists = (
            db.execute("SELECT 1 FROM sage_room_memory WHERE room_id = ?", (channel_id,)).fetchone()
            is not None
        )
        sage_facts: dict = {}
        if readmit_now:
            # Ruling ADT-S9 (a): the earlier stay's Sage memory is archived
            # and a new one started; its open suggestions are closed.
            summary = (
                f"{name}, {age if age is not None else '?'}y {patient['sex'] or '?'} "
                f"readmitted to {sage_ward} from the {row['department']} clinic for "
                f"{data.get('initial_diagnosis')}."
            )
            if data.get("comorbidities"):
                summary += f" PMH: {', '.join(data['comorbidities'])}."
            sage_facts = _restart_room_memory_on(
                db,
                channel_id=channel_id,
                patient_id=patient_id,
                admission_id=admission_id,
                previous_admission_id=previous_admission_id,
                summary=summary,
                discussion=(
                    f"Patient readmitted from outpatients onto the same record. "
                    f"{data.get('admitting_notes') or ''}"
                ),
                concern=data.get("chief_complaint") or "",
                triage=data.get("triage_severity"),
                user=user,
                now=now,
            )
        elif not memory_exists:
            summary = (
                f"{name}, {age if age is not None else '?'}y {patient['sex'] or '?'} "
                f"admitted to {sage_ward} from the {row['department']} clinic for "
                f"{data.get('initial_diagnosis')}."
            )
            if data.get("comorbidities"):
                summary += f" PMH: {', '.join(data['comorbidities'])}."
            risk_flags = []
            triage = data.get("triage_severity")
            if triage and int(triage) <= 2:
                risk_flags.append(
                    {"detail": f"High acuity admission (Triage {triage})", "confidence": 1.0}
                )
            db.execute(
                """INSERT INTO sage_room_memory (
                    room_id, patient_id, patient_summary, discussion_summary,
                    current_concern, risk_flags, participants,
                    message_count, llm_call_count, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
                (
                    channel_id,
                    patient_id,
                    summary[:1000],
                    (f"Patient admitted from outpatients. {data.get('admitting_notes') or ''}")[
                        :1000
                    ],
                    (data.get("chief_complaint") or "")[:200],
                    json.dumps(risk_flags),
                    json.dumps([user.get("name", "Unknown")]),
                    now,
                    now,
                ),
            )

        placed = None
        if adt_plan is not None:
            assert prepared is not None
            from services.adt.core import place_admission_on

            # Last, so every row above commits with the movement and its
            # fail-closed audit, or none of them does.
            placed = place_admission_on(
                db,
                admission_id=admission_id,
                actor=user,
                movement_type="admission",
                reason="Patient admitted from outpatients",
                prepared=prepared,
                boarder_acknowledged=adt_plan["boarder_acknowledged"],
                moved_at=adt_plan["admitted_iso"],
                idempotency_key=key,
                request_meta=request_meta,
            )

        log_audit_event(
            correlation_id=f"opd-convert-{row['id']}",
            event_category="clinical_access",
            action_type=CONVERSION_AUDIT_ACTION,
            outcome="success",
            db=db,
            summary_message=(
                f"Outpatient {patient_id} admitted from visit {row['id']} as "
                f"admission {admission_id}"
            ),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            target_type="admission",
            target_id=admission_id,
            patient_id=patient_id,
            request_path=meta.get("request_path"),
            http_method=meta.get("http_method"),
            ip_address=meta.get("ip_address"),
            user_agent=meta.get("user_agent"),
            metadata={
                "opd_visit_id": row["id"],
                "admission_id": admission_id,
                "clinic": row["department"],
                "department": data.get("department"),
                "token_number": row["token_number"],
                "identity_check": identity_facts,
                "same_record": True,
                "portal_login_created": login_created,
                "case_room_reused": room_reused,
                "profile_created": profile_created,
                "allergies_added": added,
                "allergies_on_chart": len(charted),
                "opd_vitals_copied": False,
                "opd_medicines_copied": False,
                "signed_note": signed,
                "no_signed_note_reason": None if signed else reason,
                "location": (
                    {
                        "ward_id": placed["ward_id"],
                        "bed_id": placed["bed_id"],
                        "linked": placed["linked"],
                    }
                    if placed is not None
                    else {"ward": row_ward, "bed": row_bed}
                ),
                "written": "in_transaction",
                **({"readmission": True} if readmit_now else {}),
            },
        )
        if readmit_now:
            # opd-17c: the readmission's own records, in this transaction.
            _audit_readmission_on(
                db,
                patient_id=patient_id,
                admission_id=admission_id,
                previous_admission_id=previous_admission_id,
                door=READMISSION_DOOR_OPD_VISIT,
                user=user,
                request_meta=request_meta,
                identity_facts=identity_facts,
                facts={
                    "opd_visit_id": row["id"],
                    "department": data.get("department"),
                    "case_room_reused": room_reused,
                    "profile_created": profile_created,
                    "allergies_added": added,
                    "allergies_on_chart": len(charted),
                    "portal_login_created": login_created,
                    "portal_password_reissued": password_reissued,
                    "previous_stay_medicines_closed": len(stopped),
                    **sage_facts,
                },
                login_id=login_id if password_reissued else None,
            )
        state.update(
            converted=True,
            now=now,
            channel_id=channel_id,
            room_reused=room_reused,
            login_id=login_id,
            temp_password=temp_password,
            bed_warning=bed_warning,
            mrn=patient["mrn"],
            dob=patient["date_of_birth"],
            added=added,
            signed=signed,
            prepared=prepared,
            placed=placed,
            readmission=readmit_now,
            login_created=login_created,
            password_reissued=password_reissued,
            stopped=stopped,
            department=data.get("department"),
        )

    def _record_completion(db, finished: dict, facts: dict) -> None:
        """The visit's own record of finishing, beside the conversion's, in
        the same transaction (the OPD finish route writes the same row)."""
        if not state.get("converted"):
            return
        charge = finished.get("consultation_charge") or {}
        log_audit_event(
            correlation_id=f"opd-complete-{finished['id']}",
            event_category="clinical_record",
            action_type="opd_visit_completed",
            outcome="success",
            db=db,
            summary_message=(
                f"OPD visit {finished['id']} completed (admitted); consultation "
                f"charge {charge.get('charge_id') or 'not raised'}"
            ),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=finished.get("patient_id"),
            target_type="opd_visit",
            target_id=str(finished["id"]),
            request_path=meta.get("request_path"),
            http_method=meta.get("http_method"),
            ip_address=meta.get("ip_address"),
            metadata={
                "disposition": ADMITTED_DISPOSITION,
                "admission_id": admission_id,
                "signed_note": state.get("signed"),
                "no_signed_note_reason": None if state.get("signed") else reason,
                "charge_id": charge.get("charge_id"),
                "rate_is_configured": charge.get("rate_is_configured"),
                "total_paise": charge.get("total_paise"),
                **facts,
                "written": "in_transaction",
            },
        )

    # 4. One immediate transaction: the visit's completion with the admission
    # inside it.
    try:
        finished = complete_opd_visit(
            visit_id,
            completed_by=user.get("id"),
            raise_charge=True,
            disposition=ADMITTED_DISPOSITION,
            before_complete=_convert,
            on_done=_record_completion,
        )
    except FrontDeskError as exc:
        raise AdmissionRefused(
            "visit_closed",
            "This visit cannot be finished, so the patient was not admitted from it.",
            status=409,
        ) from exc
    if not state.get("converted"):
        # The visit was finished elsewhere between the checks and the
        # transaction (or by this key's first send): nothing was admitted here.
        with db_session() as db:
            replay = _conversion_replay_on(db, admission_id, visit_id, user)
        if replay is not None:
            return replay
        raise AdmissionRefused(
            "visit_completed",
            "This visit was finished before the admission was saved, so the patient was not "
            "admitted. Register a new visit at the front desk, then admit the patient from it.",
            status=409,
        )

    adt_answer = {}
    if state.get("placed") is not None:
        placed, prepared = state["placed"], state["prepared"]
        extra = prepared["detail"].get("extra_bed") or {}
        adt_answer = {
            "admitted_at": adt_plan["admitted_iso"] if adt_plan is not None else None,
            "movement_id": placed["movement"]["id"],
            "location": {
                "ward_id": placed["ward_id"],
                "bed_id": placed["bed_id"],
                "ward": placed["ward"],
                "bed": placed["bed"],
                "linked": placed["linked"],
                "awaiting_bed": placed["bed_id"] is None and not placed["bed"],
                "boarder": placed["boarder"],
                "extra_bed": extra.get("label"),
            },
            "location_warning": _location_warning(prepared["target"]),
        }
    return {
        "success": True,
        "replayed": False,
        "patient_id": patient_id,
        "admission_id": admission_id,
        "channel_id": state["channel_id"],
        "mrn": state["mrn"],
        "patient_login_id": state["login_id"],
        # Shown once, only when this conversion created the login (or, for a
        # readmission, issued a fresh one-time password, ruling R029 (2)).
        "temporary_password": state["temp_password"],
        "portal_login_created": state["login_created"],
        "date_of_birth_recorded": state["dob"] is not None,
        "bed_warning": state["bed_warning"],
        "opd_visit_id": visit_id,
        "visit": {
            "id": finished["id"],
            "department": finished.get("department"),
            "status": finished.get("status"),
            "disposition": finished.get("disposition") or ADMITTED_DISPOSITION,
        },
        "case_room_reused": state["room_reused"],
        "allergies_added": state["added"],
        **adt_answer,
        **(_readmission_answer(state, user) if state.get("readmission") else {}),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Readmitting a discharged patient onto the same record (opd-17c, absorbing
#  adt-18b, W8-A; ORB_READMISSION, which needs ORB_REGISTRATION)
# ═════════════════════════════════════════════════════════════════════════════
#
# A patient discharged from an earlier stay comes back: the admission form's
# patient index refusal (or, with ORB_OPD on, their open clinic visit today)
# offers to readmit them onto their OWN record. The same patient row and the
# same hospital number are kept; nothing about who the patient is is typed
# again, so nothing about it can be typed wrongly. This is also the Ward
# Record's "a returning patient reopens the same record".
#
# ONE IMMEDIATE TRANSACTION writes, or nothing does: the patient becomes
# 'admitted' (only from 'discharged', guarded in the UPDATE), the new
# admission row, the profile filled and never blanked, allergies added beside
# the charted ones (add-only), the medicines below stopped, the case room's
# READMISSION line, the Sage memory restart, the portal password and, with
# ORB_ADT on, the bed master's placement. The one-active-admission index
# (migration 65) refuses a concurrent readmission whatever the code does.
#
# RULINGS BUILT STRICTEST, each for Samuel to confirm before go-live:
#   * OPD-S1 (a): a passing stated-identifier check (name; date of birth or
#     age; phone last 4 or UHID) before the transaction on every readmission;
#     no check or no match is refused, audited and writes nothing.
#   * OPD-S8 (a): every medicine the earlier stay left 'active' or 'prn' on
#     the chart is discontinued in the readmission's transaction (a linked
#     medication order is stopped through the order store, so the ward row and
#     the order agree), audited as previous_stay_medications_closed, and listed
#     on the answer for the prescriber to prescribe again item by item. The
#     eMAR refuses 'given' on a discontinued row, so no earlier stay's drug can
#     be given in the new stay. Live inpatient orders with no ward row are
#     stopped too. Drafts are left alone (ruling MO-R10: never cancelled by
#     the system) and still need a prescriber's signature and screening.
#   * ADT-S9 (a): the earlier stay's vitals and risk rows are never the
#     latest (services/vigil_service, 'A readmission's previous stay is never
#     latest'); the earlier stay's Sage room memory is archived (one
#     sage_observations row, type room_memory_archived, erased with the
#     patient) and a new one started, its open suggestions closed and the
#     patient's prefetched Sage context cleared; the case room is reused with
#     a dividing READMISSION line. The readmission writes no observations at
#     all (its body refuses them): the ward charts a fresh set. A
#     readmission never begins before the earlier stay ended: with ORB_ADT on
#     an admission time earlier than the previous stay's end (its
#     admissions.discharged_at, else the discharge summary that ended it, read
#     through services/stay_times) is refused 400 before_previous_stay_end in
#     the transaction, before any row is written, so the stays never overlap
#     and no set charted in the earlier stay can fall inside the new one.
#   * R029 (2) (decided): the Bridge login is reused, never a second one, and
#     a fresh one-time password is issued with the earlier stay's sessions
#     revoked (token_version). A login an administrator switched off stays off.
#   * A record that says the patient died (an admission ended as 'death', or a
#     live NABH death disposition) is refused: readmitting a death is a wrong
#     patient by definition, and a death recorded in error is corrected on the
#     earlier stay first.
#   * Only a 'discharged' patient with an ended admission is readmitted here; a
#     'transferred' record, or one with no earlier admission row, is refused.
# With ORB_READMISSION off nothing here runs: the route answers 404, the index
# refusal and the outpatient conversion say readmission is not available, as
# before.

#: The identity check's purpose, and the audit's word for this lane.
READMISSION_PURPOSE = "readmission"
#: The readmission's in-transaction record (clinical_access, fail-closed).
READMISSION_AUDIT_ACTION = "patient_readmitted"
#: OPD-S8: the earlier stay's medicines stopped (clinical_access, fail-closed).
PREVIOUS_STAY_MEDICINES_ACTION = "previous_stay_medications_closed"
#: R029 (2): the fresh one-time portal password (security, fail-closed).
PORTAL_PASSWORD_REISSUED_ACTION = "patient_portal_password_reissued"
#: The order store's entry point for the stop.
READMISSION_ENTRY_POINT = "admission.readmission"
#: Why each medicine was stopped, as the chart and the order ledger say it.
PREVIOUS_STAY_STOP_REASON = "Stopped at readmission: prescribed in the previous stay"
#: The Sage archive row's type in sage_observations.
ROOM_MEMORY_ARCHIVED = "room_memory_archived"
#: Why an open Sage suggestion of the earlier stay was closed.
ROOM_ACTION_CLOSED_REASON = "Closed at readmission: suggested in the previous stay"
#: The only patient status a readmission starts from.
READMITTABLE_STATUS = "discharged"
#: The doors a readmission arrives through, as the audit names them.
READMISSION_DOOR_FORM = "admission_form"
READMISSION_DOOR_OPD_VISIT = "opd_visit"
#: Death, as an ended stay and as a NABH disposition records it.
DEATH_DISPOSITIONS = ("death", "death_within_48h")
#: The case room's dividing line (ADT-S9 (a)).
READMISSION_DIVIDER = (
    "Readmitted onto the same record. Everything above this line is from the previous stay."
)
#: What the form says a readmission does, before anything is admitted.
READMISSION_FORM_NOTE = (
    "This patient was an inpatient here before. Admitting them reopens their own record: any "
    "medicine still on the chart from the earlier stay is stopped and listed for you to "
    "prescribe again, and the earlier stay's observations are shown only as the previous "
    "stay, never as current. Chart a fresh set of observations when the patient arrives."
)
#: Most medicines named on the case-room line; the rest are counted.
_CASE_ROOM_MEDICINES_MAX = 20


def readmission_on() -> bool:
    """True while readmission (ORB_READMISSION, with ORB_REGISTRATION) is on."""
    import config

    return bool(config.feature_enabled("readmission"))


def readmission_admission_id(user_id: str, idempotency_key: str) -> str:
    """The admission id a readmission takes from its sender and its
    Idempotency-Key, so a retried send finds the first admission."""
    import hashlib

    digest = hashlib.sha256(f"opd-17c\x00{user_id}\x00{idempotency_key}".encode()).hexdigest()
    return f"ADM-{digest[:12].upper()}"


def _readmission_patient_on(db, patient_id: str):
    """The patient as a readmission reads them, or None."""
    if not patient_id:
        return None
    return db.execute(
        """SELECT p.id, p.user_id, p.name, p.mrn, p.age, p.date_of_birth, p.sex, p.status,
                  p.created_at, d.age_recorded_on
             FROM patients p LEFT JOIN patient_demographics d ON d.patient_id = p.id
            WHERE p.id = ?""",
        (patient_id,),
    ).fetchone()


def _deceased_on(db, patient_id: str) -> bool:
    """True when the record says the patient died: an admission ended as a
    death (not reopened, which clears it), or a live NABH death disposition."""
    if (
        db.execute(
            "SELECT 1 FROM admissions WHERE patient_id = ? AND discharge_type = 'death' LIMIT 1",
            (patient_id,),
        ).fetchone()
        is not None
    ):
        return True
    if not _table_exists(db, "nabh_discharge_dispositions"):
        return False
    cols = {r[1] for r in db.execute("PRAGMA table_info(nabh_discharge_dispositions)")}
    live = " AND voided_at IS NULL" if "voided_at" in cols else ""
    marks = ",".join("?" for _ in DEATH_DISPOSITIONS)
    return (
        db.execute(
            f"SELECT 1 FROM nabh_discharge_dispositions WHERE patient_id = ? "
            f"AND disposition IN ({marks}){live} LIMIT 1",
            (patient_id, *DEATH_DISPOSITIONS),
        ).fetchone()
        is not None
    )


def _readmission_refusal_on(db, patient) -> AdmissionRefused | None:
    """Why this patient cannot be readmitted onto their record now, or None."""
    if patient is None:
        return AdmissionRefused("patient_not_found", "Patient not found.", status=404)
    active = db.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
        (patient["id"],),
    ).fetchone()
    if patient["status"] == "admitted" or active is not None:
        return AdmissionRefused(
            "patient_already_admitted",
            "This patient is already admitted. Open their inpatient chart.",
            status=409,
        )
    ended = db.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? AND status <> 'active' LIMIT 1",
        (patient["id"],),
    ).fetchone()
    if patient["status"] != READMITTABLE_STATUS or ended is None:
        return AdmissionRefused(
            "readmission_not_applicable",
            "Only a patient discharged from an earlier stay here can be readmitted onto their "
            "record. Check the patient's record with the registration desk.",
            status=409,
        )
    if _deceased_on(db, patient["id"]):
        return AdmissionRefused(
            "patient_deceased",
            "This record says the patient died, so they cannot be readmitted onto it. Check "
            "you have the right patient. If the death was recorded in error, correct the "
            "earlier stay first.",
            status=409,
        )
    return None


def _readmittable_ids_on(db, ids) -> set:
    """The patient ids a readmission would accept now (same rule)."""
    return {
        pid
        for pid in dict.fromkeys(i for i in ids if i)
        if _readmission_refusal_on(db, _readmission_patient_on(db, pid)) is None
    }


def _previous_admission_id_on(db, patient_id: str) -> str | None:
    """The patient's most recent ended stay."""
    row = db.execute(
        "SELECT id FROM admissions WHERE patient_id = ? AND status <> 'active' "
        "ORDER BY COALESCE(admitted_at, created_at) DESC, rowid DESC LIMIT 1",
        (patient_id,),
    ).fetchone()
    return row["id"] if row else None


#: Said when a readmission's admission time is before the earlier stay ended.
READMISSION_BEFORE_PREVIOUS_END = (
    "The admission time is before this patient's previous stay ended. A readmission "
    "starts after the earlier discharge: give a later admission time."
)


def _previous_stay_end_on(db, patient_id: str):
    """When the patient's earlier stays ended, as an aware UTC datetime: the
    latest end among every ended admission (services/stay_times.stay_end:
    admissions.discharged_at, else the discharge summary that ended it). A
    stay with no end recorded anywhere counts from its start, since a
    readmission never begins before an earlier stay began. An end further
    ahead than the event-time slack (services/adt/core: 5 minutes) counts as
    no end, so that stay counts from its start: no Orb writer records such an
    end, and a history import keeps a mistyped date as given, which must not
    refuse every readmission of the patient. An end inside the slack is
    honoured. None when no ended stay has a readable time. Read BEFORE the new
    admission's row exists: stay_end bounds a summary by the next admission's
    start, so a back-timed new row would hide the very end it is checked
    against."""
    from datetime import timedelta

    from services import stay_times

    ceiling = datetime.now(UTC) + timedelta(minutes=5)
    latest = None
    for row in db.execute(
        "SELECT * FROM admissions WHERE patient_id = ? AND status <> 'active'", (patient_id,)
    ).fetchall():
        stay = dict(row)
        # Its end, else (no end, or one not yet reached) its start.
        known = [
            t
            for t in (stay_times.stay_end(db, stay), stay_times.stay_start(stay))
            if t is not None and t <= ceiling
        ]
        if known and (latest is None or known[0] > latest):
            latest = known[0]
    return latest


def _check_readmission_time_on(db, patient_id: str, adt_plan) -> None:
    """ADT-S9 (a): a readmission never begins before the earlier stay ended.
    With ORB_ADT on the admission time may be back-timed (ADT_BACKTIME_HOURS);
    a time before the previous stay's end would make the stays overlap, and
    every set the ward charted in the earlier stay after that time would read
    as this stay's latest. Refused 400 (AdtBadTime, before_previous_stay_end)
    on the caller's transaction, before any row is written. The earlier end
    itself is not put in the answer. With ORB_ADT off the admission starts
    when it is written, and nothing here runs."""
    if adt_plan is None:
        return
    previous_end = _previous_stay_end_on(db, patient_id)
    if previous_end is not None and adt_plan["admitted"] < previous_end:
        from services.adt.core import AdtBadTime

        raise AdtBadTime(READMISSION_BEFORE_PREVIOUS_END, code="before_previous_stay_end")


def _readmission_portal_login_on(db, *, patient, name: str, portal_ward) -> tuple:
    """The patient's one Bridge login for the new stay (ruling R029 (2)):
    (login_id, one-time password or None, created, reissued).

    The existing login is kept and given a fresh one-time password, since the
    first was handed over at the earlier stay, and every session from then is
    revoked (token_version). A patient with no login gets one. A login an
    administrator switched off, or an id that is not a patient login, is left
    exactly as it is and no password is shown."""
    import secrets

    from auth import hash_password

    login_id = patient["user_id"]
    row = (
        db.execute(
            "SELECT id, role, COALESCE(is_active, 1) AS active FROM users WHERE id = ?", (login_id,)
        ).fetchone()
        if login_id
        else None
    )
    password = secrets.token_urlsafe(9)
    if row is None:
        login_id = f"P{uuid.uuid4().hex[:6].upper()}"
        _insert_portal_login_on(
            db,
            user_id=login_id,
            name=name,
            department=portal_ward,
            password_hash=hash_password(password),
        )
        return login_id, password, True, False
    if row["role"] != "patient" or not row["active"]:
        return login_id, None, False, False
    db.execute(
        "UPDATE users SET password_hash = ?, token_version = COALESCE(token_version, 0) + 1 "
        "WHERE id = ? AND role = 'patient'",
        (hash_password(password), login_id),
    )
    return login_id, password, False, True


def _close_previous_stay_medicines_on(
    db, patient_id: str, *, user: dict, admission_id: str, previous_admission_id, request_meta
) -> list[dict]:
    """Ruling OPD-S8 (a): discontinue every medicine the earlier stay left
    'active' or 'prn', in the caller's transaction, and return them for the
    prescriber to prescribe again. A ward row linked to a live medication
    order is stopped through the order store (core.transition_in_txn, with
    its ledger event and audit), keyed on the link, never on the switches,
    so the order and its ward row agree; every other row is discontinued
    directly. One clinical_access record lists them all (fail-closed)."""
    from services.audit_service import log_audit_event
    from services.medorders import core as mo

    rows = db.execute(
        "SELECT id, drug_name, dosage, route, frequency, status, started_at, med_order_id, "
        "held_at, hold_reason FROM medications WHERE patient_id = ? "
        "AND status IN ('active', 'prn') ORDER BY started_at, id",
        (patient_id,),
    ).fetchall()
    stopped: list[dict] = []
    order_ids: list[str] = []

    def stop_order(order_id: str) -> None:
        mo.transition_in_txn(
            db,
            order_id,
            "stopped",
            expected_version=None,
            actor_id=user.get("id"),
            actor_role=user.get("role"),
            entry_point=READMISSION_ENTRY_POINT,
            reason=PREVIOUS_STAY_STOP_REASON,
            lane=READMISSION_PURPOSE,
            audit_extra={
                "admission_id": admission_id,
                "previous_admission_id": previous_admission_id,
            },
            request_meta=request_meta,
        )
        order_ids.append(order_id)

    has_orders = _table_exists(db, "med_orders")
    for r in rows:
        if has_orders and r["med_order_id"]:
            order = mo.get_order_in_txn(db, r["med_order_id"])
            if order is not None and order["status"] in mo.LIVE_STATUSES:
                stop_order(order["id"])
        # The ward row itself, whatever the order did: one the projection did
        # not reach (an unlinked row, or a link out of step) is closed here.
        db.execute(
            "UPDATE medications SET status = 'discontinued' WHERE id = ? "
            "AND status IN ('active', 'prn')",
            (r["id"],),
        )
        stopped.append(
            {
                "medication_id": r["id"],
                "drug_name": r["drug_name"],
                "dose": r["dosage"],
                "route": r["route"],
                "frequency": r["frequency"],
                "as_needed": r["status"] == "prn",
                "started_at": r["started_at"],
                "was_on_hold": bool(r["held_at"]),
            }
        )
    if has_orders:
        marks = ",".join("?" for _ in mo.LIVE_STATUSES)
        for o in db.execute(
            f"SELECT id FROM med_orders WHERE patient_id = ? AND encounter_type = 'ip' "
            f"AND status IN ({marks}) ORDER BY created_at, id",
            (patient_id, *mo.LIVE_STATUSES),
        ).fetchall():
            if o["id"] not in order_ids:
                stop_order(o["id"])
    if stopped or order_ids:
        meta = dict(request_meta or {})
        log_audit_event(
            correlation_id=f"readmission-{admission_id}",
            event_category="clinical_access",
            action_type=PREVIOUS_STAY_MEDICINES_ACTION,
            outcome="success",
            db=db,
            summary_message=(
                f"{len(stopped)} medicine(s) from the previous stay of "
                f"{patient_id} stopped at readmission"
            ),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            target_type="admission",
            target_id=admission_id,
            patient_id=patient_id,
            request_path=meta.get("request_path"),
            http_method=meta.get("http_method"),
            ip_address=meta.get("ip_address"),
            user_agent=meta.get("user_agent"),
            metadata={
                "admission_id": admission_id,
                "previous_admission_id": previous_admission_id,
                "medication_ids": [s["medication_id"] for s in stopped],
                "med_order_ids": order_ids,
                "count": len(stopped),
                "reason": PREVIOUS_STAY_STOP_REASON,
                "written": "in_transaction",
            },
        )
    return stopped


def _medicine_words(m: dict) -> str:
    return " ".join(
        str(x)
        for x in (
            m["drug_name"],
            m.get("dose"),
            m.get("route"),
            "when needed" if m.get("as_needed") else m.get("frequency"),
        )
        if x
    )


def _readmission_case_room_lines(stopped: list[dict]) -> list[str]:
    """What the READMISSION line says about the earlier stay's medicines and
    observations. Drug names only where the drug chart is kept in Orb."""
    lines = []
    if stopped:
        if _drug_chart_enabled():
            named = [_medicine_words(m) for m in stopped[:_CASE_ROOM_MEDICINES_MAX]]
            more = len(stopped) - len(named)
            lines.append(
                "Medicines from the previous stay were stopped at readmission: "
                + "; ".join(named)
                + (f"; and {more} more" if more > 0 else "")
                + ". Prescribe each one again if it is needed."
            )
        else:
            lines.append(
                f"{len(stopped)} medicine(s) from the previous stay were stopped at "
                "readmission. Prescribe each one again if it is needed."
            )
    lines.append("Observations from the previous stay are not current: chart a fresh set.")
    return lines


def _restart_room_memory_on(
    db,
    *,
    channel_id: str | None,
    patient_id: str,
    admission_id: str,
    previous_admission_id,
    summary: str,
    discussion: str,
    concern: str,
    triage,
    user: dict,
    now: str,
) -> dict:
    """Ruling ADT-S9 (a): archive the room's Sage memory and start a new one
    for this stay, in the caller's transaction. The archive is one
    sage_observations row (type room_memory_archived) holding every field of
    the old memory: append-only, never read back as context, erased with the
    patient. Sage's open suggestions in the room (suggested or acknowledged)
    are closed as superseded with a system event, so none of the earlier
    stay's can be confirmed in this one, and the patient's prefetched Sage
    context is cleared."""
    risk_flags = []
    if triage and int(triage) <= 2:
        risk_flags.append({"detail": f"High acuity admission (Triage {triage})", "confidence": 1.0})
    participants = json.dumps([user.get("name", "Unknown")])
    old = db.execute("SELECT * FROM sage_room_memory WHERE room_id = ?", (channel_id,)).fetchone()
    archived = old is not None
    if archived:
        content = {k: old[k] for k in old.keys()}
        content.update(
            {
                "archived_at": now,
                "archived_because": READMISSION_PURPOSE,
                "new_admission_id": admission_id,
                "previous_admission_id": previous_admission_id,
            }
        )
        db.execute(
            "INSERT INTO sage_observations (room_id, patient_id, source_message_id, "
            "observation_type, content, confidence, created_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (
                channel_id,
                patient_id,
                old["last_processed_msg_id"],
                ROOM_MEMORY_ARCHIVED,
                json.dumps(content, default=str),
                now,
            ),
        )
        db.execute(
            """UPDATE sage_room_memory
                  SET patient_id = ?, patient_summary = ?, discussion_summary = ?,
                      current_concern = ?, likely_next_steps = NULL,
                      unresolved_questions = NULL, active_intents = NULL, risk_flags = ?,
                      participants = ?, last_processed_msg_id = NULL,
                      last_processed_at = NULL, message_count = 1, llm_call_count = 0,
                      created_at = ?, updated_at = ?
                WHERE room_id = ?""",
            (
                patient_id,
                summary[:1000],
                discussion[:1000],
                (concern or "")[:200],
                json.dumps(risk_flags),
                participants,
                now,
                now,
                channel_id,
            ),
        )
    else:
        db.execute(
            """INSERT INTO sage_room_memory (
                room_id, patient_id, patient_summary, discussion_summary,
                current_concern, risk_flags, participants,
                message_count, llm_call_count, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
            (
                channel_id,
                patient_id,
                summary[:1000],
                discussion[:1000],
                (concern or "")[:200],
                json.dumps(risk_flags),
                participants,
                now,
                now,
            ),
        )
    open_actions = db.execute(
        "SELECT id, state FROM sage_actions WHERE room_id = ? "
        "AND state IN ('suggested', 'acknowledged') ORDER BY created_at, id",
        (channel_id,),
    ).fetchall()
    for a in open_actions:
        db.execute(
            "UPDATE sage_actions SET state = 'superseded', "
            "version = COALESCE(version, 1) + 1, updated_at = ? "
            "WHERE id = ? AND state = ?",
            (now, a["id"], a["state"]),
        )
        db.execute(
            "INSERT INTO sage_action_events (action_id, prior_state, new_state, actor_id, "
            "actor_name, actor_role, reason, source) VALUES (?, ?, 'superseded', ?, ?, ?, ?, "
            "'system')",
            (
                a["id"],
                a["state"],
                user["id"],
                user.get("name") or "Unknown",
                user.get("role"),
                ROOM_ACTION_CLOSED_REASON,
            ),
        )
    cleared = 0
    if _table_exists(db, "sage_decision_cache"):
        cleared = db.execute(
            "DELETE FROM sage_decision_cache WHERE patient_id = ?", (patient_id,)
        ).rowcount
    return {
        "sage_memory_archived": archived,
        "sage_actions_closed": len(open_actions),
        "sage_context_cache_cleared": bool(cleared),
    }


def _audit_readmission_on(
    db,
    *,
    patient_id: str,
    admission_id: str,
    previous_admission_id,
    door: str,
    user: dict,
    request_meta,
    identity_facts: dict,
    facts: dict,
    login_id,
) -> None:
    """The readmission's records in its own transaction, fail-closed: without
    them there is no readmission. `login_id` names a login whose one-time
    password was issued again (ruling R029 (2)); None writes no such row."""
    from services.audit_service import log_audit_event

    meta = dict(request_meta or {})
    common = dict(
        db=db,
        outcome="success",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
    )
    log_audit_event(
        correlation_id=f"readmission-{admission_id}",
        event_category="clinical_access",
        action_type=READMISSION_AUDIT_ACTION,
        summary_message=(
            f"Patient {patient_id} readmitted onto the same record as admission {admission_id}"
        ),
        target_type="admission",
        target_id=admission_id,
        metadata={
            "admission_id": admission_id,
            "previous_admission_id": previous_admission_id,
            "door": door,
            "identity_check": identity_facts,
            "same_record": True,
            "observations_written": False,
            "previous_stay_observations_current": False,
            **facts,
            "written": "in_transaction",
        },
        **common,
    )
    if login_id:
        log_audit_event(
            correlation_id=f"readmission-{admission_id}",
            event_category="security",
            action_type=PORTAL_PASSWORD_REISSUED_ACTION,
            summary_message=(
                f"Bridge portal one-time password issued again for {login_id} at "
                "readmission; earlier sessions revoked"
            ),
            target_type="user",
            target_id=str(login_id),
            metadata={
                "admission_id": admission_id,
                "sessions_revoked": True,
                "ruling": "R029 (2)",
                "written": "in_transaction",
            },
            **common,
        )


def _may_read_after(user: dict, department) -> bool:
    """Whether the sender reads this patient's chart once the readmission
    commits: an account with no department, or the admission's department."""
    from permissions import department_scope

    scope = department_scope(user)
    return scope is None or (department or "") == scope


def _readmission_answer(state: dict, user: dict) -> dict:
    """What a readmission's answer adds. The stopped medicines are named only
    to a sender who reads the chart after the readmission (else counted, and
    named to the treating team on the case-room line), and only where the
    drug chart is kept in Orb."""
    stopped = state.get("stopped") or []
    listed = _drug_chart_enabled() and _may_read_after(user, state.get("department"))
    return {
        "readmission": True,
        "portal_password_reissued": bool(state.get("password_reissued")),
        "previous_stay_medicines_stopped": len(stopped),
        "previous_stay_medicines_listed": listed,
        "previous_stay_medicines": stopped if listed else [],
    }


def _readmission_replay_parts_on(db, admission_id: str, user: dict, department) -> dict:
    """The readmission keys of a replayed answer: what was stopped, read back
    from the readmission's own record. Empty when the admission was not a
    readmission. Never a password: it was shown once."""
    row = db.execute(
        "SELECT metadata_json FROM audit_logs WHERE action_type = ? AND target_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (READMISSION_AUDIT_ACTION, admission_id),
    ).fetchone()
    if row is None:
        return {}
    meds = db.execute(
        "SELECT metadata_json FROM audit_logs WHERE action_type = ? AND target_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (PREVIOUS_STAY_MEDICINES_ACTION, admission_id),
    ).fetchone()
    ids = (_json_or(meds["metadata_json"], {}) or {}).get("medication_ids") if meds else None
    stopped: list[dict] = []
    if ids:
        marks = ",".join("?" for _ in ids)
        stopped = [
            {
                "medication_id": r["id"],
                "drug_name": r["drug_name"],
                "dose": r["dosage"],
                "route": r["route"],
                "frequency": r["frequency"],
                "as_needed": None,
                "started_at": r["started_at"],
                "was_on_hold": bool(r["held_at"]),
            }
            for r in db.execute(
                f"SELECT id, drug_name, dosage, route, frequency, started_at, held_at, hold_reason "
                f"FROM medications WHERE id IN ({marks}) ORDER BY started_at, id",
                tuple(ids),
            ).fetchall()
        ]
    answer = _readmission_answer({"stopped": stopped, "department": department}, user)
    answer["portal_password_reissued"] = False
    return answer


def _readmission_replay_on(db, admission_id: str, patient_id: str, user: dict) -> dict | None:
    """The first readmission when this sender already sent this key; None for
    a new key. The same key naming another patient is refused."""
    row = db.execute(
        """SELECT a.id, a.patient_id, a.department, a.admitted_at, p.mrn, p.user_id,
                  p.date_of_birth
             FROM admissions a JOIN patients p ON p.id = a.patient_id
            WHERE a.id = ?""",
        (admission_id,),
    ).fetchone()
    if row is None:
        return None
    if row["patient_id"] != patient_id:
        raise AdmissionRefused(
            "idempotency_key_reused",
            "That Idempotency-Key was already used for another admission. Nothing was saved.",
            status=409,
        )
    return {
        "success": True,
        "replayed": True,
        "patient_id": row["patient_id"],
        "admission_id": row["id"],
        "channel_id": _case_room_of(db, row["patient_id"]),
        "mrn": row["mrn"],
        "patient_login_id": row["user_id"],
        "temporary_password": None,
        "portal_login_created": False,
        "date_of_birth_recorded": row["date_of_birth"] is not None,
        "bed_warning": None,
        "admitted_at": row["admitted_at"],
        **_readmission_replay_parts_on(db, row["id"], user, row["department"]),
    }


def readmit_patient(
    patient_id: str,
    data: dict,
    user: dict,
    *,
    scope_department,
    identity: dict | None,
    idempotency_key: str | None,
    request_meta: dict | None = None,
) -> dict:
    """Readmit a discharged patient onto their own record from the admission
    form (opd-17c). See the section note above for what one call writes and
    refuses.

    `scope_department` is permissions.department_scope(user), from the route,
    for the identity check. `identity` is what the patient states (OPD-S1).
    `idempotency_key` is required: a retried send returns the first admission
    (replayed true, no password) and writes nothing. `data` holds the
    admission form's fields except identity and observations, plus, with
    ORB_ADT on, the bed picker's.

    Raises AdmissionRefused (404, 409, 422), AdtError (ORB_ADT on),
    registration_service.RegistrationError (from the identity check) and
    AuditWriteError (503: a record that cannot be written means no
    readmission)."""
    from db_backend import DBIntegrityError

    from services.registration_service import RegistrationNotFound

    key = _plain(idempotency_key)
    if not key:
        raise AdmissionRefused(
            "idempotency_key_required",
            "Send an Idempotency-Key with the admission, so a retry never admits the patient "
            "twice.",
            status=422,
        )
    patient_id = _plain(patient_id) or ""
    data = dict(data)
    authoriser_pin = data.pop("terminal_clean_authoriser_pin", None)
    admission_id = readmission_admission_id(str(user["id"]), key)

    # 1. A replay of this key, and whether the patient can be readmitted.
    with db_session() as db:
        replay = _readmission_replay_on(db, admission_id, patient_id, user)
        if replay is not None:
            return replay
        refusal = _readmission_refusal_on(db, _readmission_patient_on(db, patient_id))
    if refusal is not None:
        raise refusal

    # 2. The bed master's checks that open sessions of their own (ORB_ADT).
    adt_plan = (
        _adt_admission_plan(data, key, user, request_meta, authoriser_pin)
        if _adt_admitting()
        else None
    )
    authoriser_pin = None

    # 3. Who the patient is (OPD-S1), before the transaction.
    try:
        identity_facts = _confirm_conversion_identity(
            patient_id,
            identity,
            user=user,
            scope_department=scope_department,
            request_meta=request_meta,
            purpose=READMISSION_PURPOSE,
        )
    except RegistrationNotFound:
        # The check hides another ward's inpatient as 'not found'. When the
        # patient was readmitted a moment ago (by this send's twin, or
        # anyone), say that instead.
        with db_session() as db:
            replay = _readmission_replay_on(db, admission_id, patient_id, user)
            if replay is not None:
                return replay
            now_refused = _readmission_refusal_on(db, _readmission_patient_on(db, patient_id))
        if now_refused is not None:
            raise now_refused from None
        raise

    # multisite-3: the facility of this admission (None with one facility on
    # record); refused when the patient's admissions are at another facility.
    admission_facility_id = admission_facility(
        data.get("department"),
        user,
        lane="readmission",
        patient_id=patient_id,
        request_meta=request_meta,
    )

    # 4. One immediate transaction.
    try:
        with db_session(immediate=True) as db:
            replay = _readmission_replay_on(db, admission_id, patient_id, user)
            if replay is not None:
                return replay
            state = _readmit_on(
                db,
                patient_id=patient_id,
                admission_id=admission_id,
                data=data,
                user=user,
                identity_facts=identity_facts,
                adt_plan=adt_plan,
                key=key,
                request_meta=request_meta,
                facility_id=admission_facility_id,
            )
    except DBIntegrityError as exc:
        if "admissions" in str(exc):
            # The one-active-admission index (migration 65): a concurrent
            # readmission or admission got there first. Nothing was saved.
            raise AdmissionRefused(
                "patient_already_admitted",
                "This patient is already admitted. Open their inpatient chart.",
                status=409,
            ) from exc
        raise

    adt_answer = {}
    if state.get("placed") is not None:
        placed, prepared = state["placed"], state["prepared"]
        extra = prepared["detail"].get("extra_bed") or {}
        adt_answer = {
            "admitted_at": adt_plan["admitted_iso"] if adt_plan is not None else None,
            "movement_id": placed["movement"]["id"],
            "location": {
                "ward_id": placed["ward_id"],
                "bed_id": placed["bed_id"],
                "ward": placed["ward"],
                "bed": placed["bed"],
                "linked": placed["linked"],
                "awaiting_bed": placed["bed_id"] is None and not placed["bed"],
                "boarder": placed["boarder"],
                "extra_bed": extra.get("label"),
            },
            "location_warning": _location_warning(prepared["target"]),
        }
    return {
        "success": True,
        "replayed": False,
        "patient_id": patient_id,
        "admission_id": admission_id,
        "channel_id": state["channel_id"],
        "mrn": state["mrn"],
        "patient_login_id": state["login_id"],
        # Shown once: a fresh one-time password for the reused login (R029 (2)),
        # or the first one when the patient had none.
        "temporary_password": state["temp_password"],
        "portal_login_created": state["login_created"],
        "date_of_birth_recorded": state["dob"] is not None,
        "bed_warning": state["bed_warning"],
        "case_room_reused": state["room_reused"],
        "allergies_added": state["added"],
        **adt_answer,
        **_readmission_answer(state, user),
    }


def _readmit_on(
    db,
    *,
    patient_id: str,
    admission_id: str,
    data: dict,
    user: dict,
    identity_facts: dict,
    adt_plan,
    key: str,
    request_meta,
    facility_id: int | None = None,
) -> dict:
    """readmit_patient's transaction body, on the caller's immediate session.
    Anything raised here rolls every row back. `facility_id` is
    admission_facility's answer (multisite-3)."""
    from services import patient_matching as pm
    from services.clinical_records_service import allergy_entries, record_allergies_on

    now = datetime.now(UTC).isoformat()
    patient = _readmission_patient_on(db, patient_id)
    refused = _readmission_refusal_on(db, patient)
    if refused is not None:
        raise refused
    if adt_plan is not None:
        from services.adt.core import AdtConflict, movement_for_key

        if movement_for_key(db, key) is not None:
            raise AdtConflict(
                "That Idempotency-Key was already used for another admission. Nothing was saved.",
                code="idempotency_key_reused",
            )
    # ADT-S9 (a): never before the earlier stay ended. Before any row exists.
    _check_readmission_time_on(db, patient_id, adt_plan)

    name = patient["name"]
    row_ward, row_bed = data.get("ward"), data.get("bed")
    portal_ward = data.get("ward") or "General"
    summary_ward = data.get("ward") or "Unassigned"
    summary_bed = data.get("bed") or "Unassigned"
    sage_ward = data.get("ward")
    admission_date = now
    prepared = None
    if adt_plan is None:
        bed_warning = bed_in_use_warning(db, data.get("ward"), data.get("bed"))
        if bed_warning:
            logger.warning("Readmission %s: %s", admission_id, bed_warning)
    else:
        from services.adt.core import prepare_place_on

        # Every placement rule, before a row about the admission exists.
        bed_warning = None
        prepared = prepare_place_on(
            db,
            admission_id=None,
            actor=user,
            movement_type="admission",
            ward_id=adt_plan["ward_id"],
            bed_id=adt_plan["bed_id"],
            ward_text=adt_plan["ward_text"],
            bed_text=adt_plan["bed_text"],
            extra_bed=adt_plan["extra_bed"],
            out_of_service_reason=adt_plan["out_of_service_reason"],
            not_clean_reason=adt_plan["not_clean_reason"],
            terminal_clean_authorised_by=adt_plan["terminal_clean_authorised_by"],
            terminal_clean_reason=adt_plan["terminal_clean_reason"],
            request_meta=request_meta,
            treating_department=data.get("department"),
            terminal_clean_confirmed=adt_plan["terminal_clean_confirmed"],
            patient_id=patient_id,
        )
        place = prepared["target"]
        row_ward, row_bed = None, None
        portal_ward = place["ward_text"] or portal_ward
        summary_ward = place["ward_text"] or "Not chosen yet"
        summary_bed = place["bed_text"] or "Awaiting a bed"
        sage_ward = place["ward_text"] or "a ward not yet chosen"
        admission_date = adt_plan["admitted"].isoformat()

    login_id, temp_password, login_created, password_reissued = _readmission_portal_login_on(
        db, patient=patient, name=name, portal_ward=portal_ward
    )

    # The same patient row: only the status, the login and the clinical and
    # location mirrors change. Name, hospital number, age, date of birth, sex
    # and contact details are never written here.
    moved = db.execute(
        """UPDATE patients
              SET status = 'admitted', user_id = ?, ward = ?, bed = ?, diagnosis = ?,
                  attending_doctor_id = ?, admission_date = ?
            WHERE id = ? AND status = ?""",
        (
            login_id,
            row_ward,
            row_bed,
            data.get("initial_diagnosis") or data.get("chief_complaint"),
            data.get("attending_doctor_id"),
            admission_date,
            patient_id,
            READMITTABLE_STATUS,
        ),
    )
    if moved.rowcount != 1:
        raise AdmissionRefused(
            "state_changed",
            "This patient's record changed while the form was open. Nothing "
            "was saved. Open the patient again.",
            status=409,
        )

    db.execute(
        """INSERT INTO admissions (
            id, patient_id, admitted_by, admission_source, admission_type,
            department, ward, bed, attending_doctor_id, referring_clinician,
            chief_complaint, initial_diagnosis, triage_severity,
            admitting_notes, nursing_notes, isolation_precautions, code_status,
            initial_vitals_json, status, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?)""",
        (
            admission_id,
            patient_id,
            user["id"],
            data.get("admission_source"),
            data.get("admission_type"),
            data.get("department"),
            row_ward,
            row_bed,
            data.get("attending_doctor_id"),
            data.get("referring_clinician"),
            data.get("chief_complaint"),
            data.get("initial_diagnosis"),
            data.get("triage_severity"),
            data.get("admitting_notes"),
            data.get("nursing_notes"),
            json.dumps(data.get("isolation_precautions") or []),
            data.get("code_status") or "full_code",
            now,
        ),
    )
    _stamp_admission_facility_on(
        db,
        admission_id=admission_id,
        patient_id=patient_id,
        facility_id=facility_id,
        new_patient=False,
    )
    if adt_plan is not None:
        db.execute(
            "UPDATE admissions SET admitted_at = ? WHERE id = ?",
            (adt_plan["admitted_iso"], admission_id),
        )

    profile_created = _profile_on(db, patient_id, data, now)
    # Add-only: an allergy already charted stays exactly as it is.
    added = record_allergies_on(
        db,
        patient_id,
        allergy_entries(data.get("allergies") or []),
        source="admission",
        recorded_by=user["id"],
        recorded_at=now,
    )
    charted = [
        r["substance"]
        for r in db.execute(
            "SELECT substance FROM patient_allergies WHERE patient_id = ? AND status = 'active' "
            "ORDER BY substance",
            (patient_id,),
        ).fetchall()
    ]

    previous_admission_id = _previous_admission_id_on(db, patient_id)
    stopped = _close_previous_stay_medicines_on(
        db,
        patient_id,
        user=user,
        admission_id=admission_id,
        previous_admission_id=previous_admission_id,
        request_meta=request_meta,
    )

    # One case room per patient for life: reused, with a dividing line.
    channel_id = _case_room_of(db, patient_id)
    room_reused = channel_id is not None
    if not room_reused:
        channel_id = f"chan_{uuid.uuid4().hex[:12]}"
        _insert_case_room_on(
            db, channel_id=channel_id, room_name=f"Case: {name}", patient_id=patient_id, now=now
        )
    age, _stated = pm.current_age(
        date_of_birth=patient["date_of_birth"],
        stated_age=patient["age"],
        age_recorded_on=patient["age_recorded_on"],
        created_at=patient["created_at"],
    )
    lines = [
        f"**READMISSION**: {name} ({age if age is not None else '?'}y {patient['sex'] or '?'})",
        READMISSION_DIVIDER,
        f"Ward: {summary_ward} | Bed: {summary_bed}",
        f"Source: {data.get('admission_source') or 'Direct'} | "
        f"Type: {data.get('admission_type') or 'Inpatient'}",
        f"Chief Complaint: {data.get('chief_complaint') or 'Not provided'}",
        f"Initial Diagnosis: {data.get('initial_diagnosis') or 'Pending'}",
    ]
    if charted:
        lines.append(f"Allergies: {', '.join(charted)}")
    if data.get("comorbidities"):
        lines.append(f"Comorbidities: {', '.join(data['comorbidities'])}")
    lines += _readmission_case_room_lines(stopped)
    _post_system_line_on(db, channel_id=channel_id, content="\n".join(lines), now=now)

    summary = (
        f"{name}, {age if age is not None else '?'}y {patient['sex'] or '?'} "
        f"readmitted to {sage_ward} for {data.get('initial_diagnosis')}."
    )
    if data.get("comorbidities"):
        summary += f" PMH: {', '.join(data['comorbidities'])}."
    sage_facts = _restart_room_memory_on(
        db,
        channel_id=channel_id,
        patient_id=patient_id,
        admission_id=admission_id,
        previous_admission_id=previous_admission_id,
        summary=summary,
        discussion=(
            f"Patient readmitted via {data.get('admission_source')} onto the same "
            f"record. {data.get('admitting_notes') or ''}"
        ),
        concern=data.get("chief_complaint") or "",
        triage=data.get("triage_severity"),
        user=user,
        now=now,
    )

    placed = None
    if adt_plan is not None:
        assert prepared is not None
        from services.adt.core import place_admission_on

        # Last, so every row above commits with the movement and its
        # fail-closed audit, or none of them does.
        placed = place_admission_on(
            db,
            admission_id=admission_id,
            actor=user,
            movement_type="admission",
            reason="Patient readmitted",
            prepared=prepared,
            boarder_acknowledged=adt_plan["boarder_acknowledged"],
            moved_at=adt_plan["admitted_iso"],
            idempotency_key=key,
            request_meta=request_meta,
        )

    _audit_readmission_on(
        db,
        patient_id=patient_id,
        admission_id=admission_id,
        previous_admission_id=previous_admission_id,
        door=READMISSION_DOOR_FORM,
        user=user,
        request_meta=request_meta,
        identity_facts=identity_facts,
        facts={
            "department": data.get("department"),
            "case_room_reused": room_reused,
            "profile_created": profile_created,
            "allergies_added": added,
            "allergies_on_chart": len(charted),
            "portal_login_created": login_created,
            "portal_password_reissued": password_reissued,
            "previous_stay_medicines_closed": len(stopped),
            **sage_facts,
            "location": (
                {
                    "ward_id": placed["ward_id"],
                    "bed_id": placed["bed_id"],
                    "linked": placed["linked"],
                }
                if placed is not None
                else {"ward": row_ward, "bed": row_bed}
            ),
        },
        login_id=login_id if password_reissued else None,
    )
    return {
        "channel_id": channel_id,
        "room_reused": room_reused,
        "login_id": login_id,
        "temp_password": temp_password,
        "login_created": login_created,
        "password_reissued": password_reissued,
        "bed_warning": bed_warning,
        "mrn": patient["mrn"],
        "dob": patient["date_of_birth"],
        "added": added,
        "prepared": prepared,
        "placed": placed,
        "stopped": stopped,
        "department": data.get("department"),
    }
