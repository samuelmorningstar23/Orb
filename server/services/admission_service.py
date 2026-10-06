"""Orb Admission Service — Handles the cross-module orchestration of admitting a patient"""
import logging
import re
import uuid
import json
from datetime import datetime, timezone
from database import SERVICE_ACCOUNT_IDS, db_session
from services.clinical_time import to_utc_iso
from services.vitals_source import manual_entry_source_or_standin
from services.patient_name_rule import validate_patient_name
from ws.realtime import sio

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
    return [{"id": r["id"], "name": r["name"], "department": r["department"] or None}
            for r in rows]


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


def resolve_admission_department(name) -> str:
    """The department as the access boundary spells it, or AdmissionInputError.

    permissions.department_scope matches admissions.department to users.department
    exactly, so a typed "cardiology" or "Cardiolgy" admitted a patient onto no
    doctor's board. Same rule and helper as an OPD registration or a transfer
    (frontdesk_service._resolve_covering_department).
    """
    from services.frontdesk_service import _resolve_covering_department

    canonical = _resolve_covering_department(name if isinstance(name, str) else "")
    if canonical is None:
        raise AdmissionInputError(DEPARTMENT_NOT_COVERED)
    return canonical


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
    return (f"Bed {bed_t} on {ward_t} is already held by another admitted patient. "
            "Check the bed with the ward.")


def _natural_key(label: str) -> list:
    """GM-2 before GM-10. re.split with a group alternates text and digits, so
    like is always compared with like."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", label)]


def admission_choices(user_department=None) -> dict:
    """What the admission form offers: covered departments, attending doctors,
    and ward and bed labels with whether each bed is held.

    No patient data leaves: department names, staff names and ids, ward and bed
    labels, and a held flag (never by whom). Beds come from the housekeeping
    register where a site keeps one, plus every bed an admitted patient is in,
    so a site with no register still gets the held warning. Wards and beds are
    suggestions; an unlisted one is still accepted (a new bay is legitimate).
    The DEPARTMENT is the one validated choice, because it carries the access
    boundary.
    """
    from services.frontdesk_service import list_covering_departments

    departments = list_covering_departments()
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
        has_register = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'housekeeping_units'"
        ).fetchone() is not None
        register_rows = db.execute(
            """SELECT TRIM(ward) AS ward,
                      COALESCE(NULLIF(TRIM(bed_label), ''), TRIM(unit_code)) AS bed
                 FROM housekeeping_units
                WHERE unit_type = 'bed' AND COALESCE(is_active, 1) = 1
                  AND TRIM(COALESCE(ward, '')) != ''"""
        ).fetchall() if has_register else []

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
    return {
        "departments": departments,
        "default_department": next((d for d in departments if d.casefold() == wanted), None),
        "doctors": attending_doctors(),
        "wards": sorted(wards.values(), key=lambda w: _natural_key(w["ward"])),
        "beds": sorted(beds.values(),
                       key=lambda b: (_natural_key(b["ward"]), _natural_key(b["bed"]))),
    }


def admit_patient(data: dict, user: dict) -> dict:
    """
    Transactional admission orchestrator.
    Creates records in patients, admissions, patient_profiles, channels, messages, and sage_room_memory.
    """
    # THE registration name rule (services/patient_name_rule.py). A provided
    # name with fewer than two letters raises PatientNameError here, before a
    # single row is written; the admit route maps it to HTTP 400. A caller
    # that passes no name at all keeps the historical placeholder, which the
    # route never does (AdmissionRequest.name is required).
    raw_name = data.get("name")
    name = "Unknown Patient" if raw_name is None else validate_patient_name(raw_name)

    now = datetime.now(timezone.utc).isoformat()
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
    
    with db_session() as db:
        # Checked before this admission writes its own patient row, so the
        # patient being admitted is never counted as the bed's other occupant.
        bed_warning = bed_in_use_warning(db, data.get("ward"), data.get("bed"))
        if bed_warning:
            logger.warning("Admission %s: %s", admission_id, bed_warning)

        # 0. Create the patient's Bridge portal login (unique one-time password).
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, 'patient', ?, ?)",
            (patient_user_id, name, data.get("ward", "General"),
             hash_password(temp_password)),
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
                data.get("ward"),
                data.get("bed"),
                data.get("initial_diagnosis") or data.get("chief_complaint"),
                data.get("attending_doctor_id"),
                now,
                now
            )
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
                data.get("ward"),
                data.get("bed"),
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
                now
            )
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
                json.dumps(data.get("current_medications", [])),
                data.get("family_history"),
                data.get("social_history"),
                now
            )
        )

        # 3b. Chart the allergy chips as structured allergies. The narrative
        # above is for reading; patient_allergies is what the order-time and
        # bedside interlocks screen against, so a chip kept only in the profile
        # let a penicillin-allergic patient be ordered amoxicillin unchallenged.
        # Same handle, so the allergy and the admission commit together.
        from services.clinical_records_service import allergy_entries, record_allergies_on
        record_allergies_on(
            db, patient_id, allergy_entries(data.get("allergies") or []),
            source="admission", recorded_by=admitting_user_id, recorded_at=now,
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
                on_oxygen=(None if vitals.get("on_oxygen") is None
                           else bool(vitals.get("on_oxygen"))),
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
                    {"heart_rate": vitals.get("hr"), "systolic_bp": vitals.get("bp_sys"),
                     "respiratory_rate": vitals.get("rr"),
                     "temperature": vitals.get("temperature"), "spo2": vitals.get("spo2"),
                     "consciousness": vitals.get("consciousness"),
                     "on_oxygen": (None if vitals.get("on_oxygen") is None
                                   else bool(vitals.get("on_oxygen")))},
                    on_scale_2=bool(_prow and _prow["on_scale_2"]),
                    overlay=False,
                )
                # Stamped with the admission instant the vitals row above
                # carries, in the one stored spelling (services/clinical_time.py).
                # The column's CURRENT_TIMESTAMP default wrote "... HH:MM:SS",
                # which every ISO row outranked on a text ORDER BY.
                db.execute(
                    "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (patient_id, to_utc_iso(now), _risk["score"], _risk["level"],
                     json.dumps(_risk["drivers"])),
                )
            except _ScoringOff:
                pass
            except Exception as e:
                # Non-fatal: admission still succeeds without a seeded baseline
                # risk score, but log it so the gap is visible rather than silent.
                logger.warning("Baseline risk-score seeding failed for %s: %s", patient_id, e)
        
        # 5. Initialize Relay (Case Room)
        room_name = f"Case: {data.get('name', 'Unknown')}"
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id, created_at) VALUES (?, ?, 'case_room', ?, ?)",
            (channel_id, room_name, patient_id, now)
        )
        
        # Build structured initial system message for Relay
        summary_lines = [
            f"**ADMISSION**: {data.get('name')} ({data.get('age', '?')}y {data.get('sex', '?')})",
            f"Ward: {data.get('ward', 'Unassigned')} | Bed: {data.get('bed', 'Unassigned')}",
            f"Source: {data.get('admission_source', 'Direct')} | Type: {data.get('admission_type', 'Inpatient')}",
            f"Chief Complaint: {data.get('chief_complaint', 'Not provided')}",
            f"Initial Diagnosis: {data.get('initial_diagnosis', 'Pending')}",
        ]
        if data.get('allergies'):
            summary_lines.append(f"Allergies: {', '.join(data.get('allergies'))}")
        if data.get('comorbidities'):
            summary_lines.append(f"Comorbidities: {', '.join(data.get('comorbidities'))}")
            
        initial_msg = "\n".join(summary_lines)
        
        # messages.timestamp is written in SQLite's own UTC form, the one its
        # CURRENT_TIMESTAMP default gives every other message in the room. The
        # ISO `now` sorted after them as text and pinned this line to the top.
        db.execute(
            """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, timestamp, is_bot, source)
               VALUES (?, 'system', 'Orb System', 'system', ?, 'system', ?, 1, 'system')""",
            (channel_id, initial_msg, datetime.fromisoformat(now).strftime("%Y-%m-%d %H:%M:%S"))
        )
        
        # 6. Initialize Sage Room Memory
        sage_patient_summary = f"{data.get('name')}, {data.get('age', '?')}y {data.get('sex', '?')} admitted to {data.get('ward')} for {data.get('initial_diagnosis')}."
        if data.get('comorbidities'):
            sage_patient_summary += f" PMH: {', '.join(data.get('comorbidities'))}."
            
        sage_discussion_summary = f"Patient admitted via {data.get('admission_source')}. {data.get('admitting_notes', '')}"
        
        # Initialize risk flags for Sage based on triage and infection/fall risks
        risk_flags = []
        if data.get('triage_severity') and int(data.get('triage_severity')) <= 2:
            risk_flags.append({"detail": f"High acuity admission (Triage {data.get('triage_severity')})", "confidence": 1.0})
        
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
                now
            )
        )
        
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
    }
