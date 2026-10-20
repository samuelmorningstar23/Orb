import json
import logging
import uuid
from datetime import UTC, datetime

from database import db_session, run_db

logger = logging.getLogger("orb.surgery_escalation")

# TIER POLICY for this module: surgical prep enrichment is BACKGROUND work and
# must run on the ambient tier (config.AMBIENT_LLM_MODEL), never on the
# interactive model. Orb's llm_queue is ONE global, non-preemptive slot held for
# the whole generation, so a long prep-package job on the 27B parks a
# clinician's Sage turn behind it — comparably-sized background jobs measured
# 2026-07-31 held it for 10.9 s and 30.1 s on medgemma:27b against 2.2 s and
# 4.3 s on medgemma:4b (5-7x). The imports of OLLAMA_BASE_URL/LLM_MODEL that
# used to sit here were dead (the generation below writes a fixed template,
# PREP_PACKAGE, with NO model call at all) and were removed so
# nothing accidentally re-targets the 27B. When real generation is restored,
# route it exactly like services/timeline_service.py:
#     model_choice = await select_chat_model(
#         preferred=AMBIENT_LLM_MODEL, fallback=AMBIENT_LLM_MODEL)
#     if await ambient_tier_evicted(model_choice.model): -> FALLBACK_DATA
# with num_ctx=4096 to match the ambient runner's loaded context (a mismatched
# num_ctx makes Ollama spawn a new runner and reload the tier), and record
# model_choice.model as provenance rather than a hardcoded name.


def create_surgery_prep_request(
    patient_id: str,
    channel_id: str,
    requested_by: str,
    requested_by_name: str,
    procedure_name: str,
    indication: str,
    urgency: str,
    clinical_context: str,
) -> dict:
    """Creates a drafted prep request (audit trail)."""
    # The key was channel_id + whole-second timestamp, and the column is UNIQUE:
    # two prep requests raised on one case room inside the same second collided
    # and the second one died with an IntegrityError. The dedupe that matters
    # happens at CONFIRM time (on this key), so making creation collision-free
    # costs nothing.
    idempotency_key = (
        f"prep_{channel_id}_{int(datetime.now(UTC).timestamp())}_{uuid.uuid4().hex[:8]}"
    )

    with db_session() as db:
        db.execute(
            """INSERT INTO surgery_prep_requests 
               (patient_id, channel_id, requested_by, requested_by_name, procedure_name, indication, urgency, clinical_context, state, idempotency_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'drafted', ?)""",
            (
                patient_id,
                channel_id,
                requested_by,
                requested_by_name,
                procedure_name,
                indication,
                urgency,
                clinical_context,
                idempotency_key,
            ),
        )
        req_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    return {"id": req_id, "idempotency_key": idempotency_key}


_DOCTOR_ROLES = ("doctor", "clinician")


def _resolve_emergency_surgeon(db, patient_id: str, confirmed_by: str) -> str:
    """Pick the surgeon for an emergency case, in descending order of correctness.

    1. the patient's attending doctor, 2. the clinician who confirmed the prep,
    3. any doctor on the system. Every candidate is checked against `users` so the
    value can satisfy the surgeries.surgeon_id FK. Raises ValueError (a clean 4xx
    at the route) rather than letting an IntegrityError escape as a 500.
    """
    placeholders = ",".join("?" * len(_DOCTOR_ROLES))

    attending = db.execute(
        "SELECT attending_doctor_id FROM patients WHERE id = ?", (patient_id,)
    ).fetchone()
    candidates = [attending["attending_doctor_id"] if attending else None, confirmed_by]
    for candidate in candidates:
        if not candidate:
            continue
        row = db.execute(
            f"SELECT id FROM users WHERE id = ? AND role IN ({placeholders})",
            (candidate, *_DOCTOR_ROLES),
        ).fetchone()
        if row:
            return row["id"]

    any_doctor = db.execute(
        f"SELECT id FROM users WHERE role IN ({placeholders}) ORDER BY id LIMIT 1",
        _DOCTOR_ROLES,
    ).fetchone()
    if any_doctor:
        logger.warning(
            "[SurgeryEscalation] No attending or confirming doctor for patient %s — "
            "assigning %s; reassign the case in the Surgical Suite.",
            patient_id,
            any_doctor["id"],
        )
        return any_doctor["id"]

    raise ValueError(
        "Cannot open an emergency surgical case: no doctor account exists to assign "
        "as surgeon. Create the surgeon's Orb user first."
    )


def confirm_surgery_prep(
    prep_id: int, confirmed_by: str, confirmed_by_name: str, idempotency_key: str
) -> dict:
    """
    Idempotent confirmation.
    If already resolved for this key, returns existing surgery_id.
    Otherwise creates an emergency surgery row and marks request resolved.
    """
    with db_session() as db:
        # 1. Check idempotency
        existing = db.execute(
            "SELECT surgery_id, state FROM surgery_prep_requests WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

        if existing and existing["state"] == "resolved" and existing["surgery_id"]:
            return {"surgery_id": existing["surgery_id"], "status": "already_created"}

        # 2. Get prep details
        prep = db.execute("SELECT * FROM surgery_prep_requests WHERE id = ?", (prep_id,)).fetchone()
        if not prep:
            raise ValueError("Prep request not found")

        patient = db.execute(
            "SELECT name, diagnosis FROM patients WHERE id = ?", (prep["patient_id"],)
        ).fetchone()
        if not patient:
            raise ValueError("Patient not found")

        # 3. Create emergency surgery case or reuse seed.
        #    The PAT007 -> 'SURG-ALEX-EM' reuse is a DEMO fixture. Ungated, a real
        #    hospital that happened to mint the patient id PAT007 would have its
        #    emergency case silently attached to a pre-seeded demo surgery row.
        from config import DEMO_MODE

        surgery_id = f"surg_{uuid.uuid4().hex[:8]}"
        if DEMO_MODE and prep["patient_id"] == "PAT007":
            seeded = db.execute("SELECT id FROM surgeries WHERE id = 'SURG-ALEX-EM'").fetchone()
            if seeded:
                surgery_id = "SURG-ALEX-EM"
                # Optionally, update status if needed, but it's already emergency_ready

        # The schedule is the ward's calendar (GET /surgery/schedule reads it by
        # hospital_today). Stamped in UTC, a case raised 00:00-05:30 IST was
        # filed under yesterday and missing from today's list.
        from services.hospital_time import hospital_now

        now = hospital_now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")

        # Assign a surgeon. This used to be the hardcoded literal "DOC004", which
        # exists ONLY in scripts/seed_clinical_data.py — at a real site that id is
        # absent, surgeries.surgeon_id is a FK to users, and confirming an
        # emergency prep would have failed with a raw IntegrityError (rolling the
        # whole Relay action back with a 500 in front of the clinician).
        surgeon_id = _resolve_emergency_surgeon(db, prep["patient_id"], confirmed_by)

        if surgery_id != "SURG-ALEX-EM":
            # multisite-4: the patient's facility (1 with one facility on record).
            from services.surgery_service import surgery_facility_on

            facility_id = surgery_facility_on(db, prep["patient_id"])
            db.execute(
                """INSERT INTO surgeries
                   (id, patient_id, surgeon_id, procedure_name, diagnosis, scheduled_date, scheduled_time, estimated_duration_min, operating_room, status, facility_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'emergency_pending', ?)""",
                (
                    surgery_id,
                    prep["patient_id"],
                    surgeon_id,
                    prep["procedure_name"],
                    patient["diagnosis"],
                    date_str,
                    time_str,
                    120,
                    "OR-E (Emergency)",
                    facility_id,
                ),
            )

        # 4. Mark prep request as resolved
        db.execute(
            """UPDATE surgery_prep_requests 
               SET state = 'resolved', surgery_id = ?, resolved_by = ?, resolved_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (surgery_id, confirmed_by, prep_id),
        )

    return {"surgery_id": surgery_id, "status": "created"}


# The prep package written when an emergency case is opened. No model reads the
# patient on this path, so nothing in it may read as an assessment of THIS
# patient: no ASA class, no sepsis claim, no success estimate, no named staff.
# Until 2026-10-02 this module (and sage_handoff_service) stored a fixed
# "ASA IV, septic shock, 72.5% success" package with invented clinicians on
# every real case. Generic prep steps are fine; patient claims are not.
PREP_PACKAGE = {
    "risk_assessment": "Pre-operative risk assessment pending clinical review.",
    "possible_complications": [
        "Standard surgical complications",
        "Anaesthesia risks",
        "Post-operative infection",
    ],
    "success_probability": None,
    "surgical_checklist": [
        "Verify patient identity and consent",
        "Confirm procedure and site marking",
        "Review allergies and medications",
        "Ensure blood products available",
        "Confirm anaesthesia plan",
        "Verify equipment and instrument readiness",
    ],
    "instruments_required": ["Standard surgical tray"],
    "staff_required": [
        {"role": "Lead Surgeon", "name": "Assigned on arrival"},
        {"role": "Anaesthetist", "name": "Assigned on arrival"},
    ],
}

# Written when preparing the package itself failed. Same honest content; the
# risk text says plainly that there is no assessment, and the case still opens.
FALLBACK_DATA = {
    **PREP_PACKAGE,
    "risk_assessment": "Assessment unavailable. Complete the pre-operative risk assessment clinically.",
}


def _finalize_surgery(surgery_id: str, data: dict, ai_status: str, reason: str = ""):
    """Write data to surgery row and transition to emergency_ready.

    Blocking. Always call via run_db(). Kept at module scope rather than nested
    in the async body: a db_session closure sitting inside an `async def` reads
    as loop-safe and is one careless edit away from being awaited inline again,
    which is exactly how this file wedged the loop.

    Blind UPDATE, no read-modify-write, so it needs no guard against a
    concurrent writer touching the same surgery row.
    """
    notes_payload = {"ai_package_status": ai_status}
    if reason:
        notes_payload["reason"] = reason
    notes_payload["finalized_at"] = datetime.now(UTC).isoformat()

    with db_session() as db:
        db.execute(
            """UPDATE surgeries SET
               risk_assessment = ?,
               possible_complications = ?,
               success_probability = ?,
               surgical_checklist = ?,
               instruments_required = ?,
               staff_required = ?,
               notes = ?,
               status = 'emergency_ready'
               WHERE id = ?""",
            (
                data.get("risk_assessment", FALLBACK_DATA["risk_assessment"]),
                json.dumps(
                    data.get("possible_complications", FALLBACK_DATA["possible_complications"])
                ),
                # No default: an absent estimate stays NULL. A made-up 80 here
                # rendered as "80% success" on cases nobody had assessed.
                data.get("success_probability"),
                json.dumps(data.get("surgical_checklist", FALLBACK_DATA["surgical_checklist"])),
                json.dumps(data.get("instruments_required", FALLBACK_DATA["instruments_required"])),
                json.dumps(data.get("staff_required", FALLBACK_DATA["staff_required"])),
                json.dumps(notes_payload),
                surgery_id,
            ),
        )


def _mark_generating(surgery_id: str, prep_id: int):
    """Flip the surgery into 'generating' and read back its prep request.

    Blocking. Always call via run_db(). The UPDATE and the SELECT stay in ONE
    sync call so they share a transaction — split across two awaits, another
    task could resolve or re-prep the request in between and this would return
    a prep row that no longer matches the status it just wrote.
    """
    with db_session() as db:
        db.execute(
            "UPDATE surgeries SET status = 'emergency_context_generating' WHERE id = ?",
            (surgery_id,),
        )
        prep = db.execute(
            "SELECT clinical_context, procedure_name, indication FROM surgery_prep_requests WHERE id = ?",
            (prep_id,),
        ).fetchone()
        return prep


async def generate_surgical_prep_package_async(surgery_id: str, prep_id: int):
    """
    Background task that writes the prep package and moves the case to
    emergency_ready.

    BOUNDED: Always transitions to emergency_ready within 60s.
    Tracks ai_package_status in notes JSON ('template' or 'failed') so the UI
    never badges template content as an AI assessment.
    """
    import asyncio

    async def _do_generation():
        # No LLM call happens on this path today (see the tier policy at the
        # top of this module), so the package is the fixed template. If MedGemma
        # enrichment returns, it goes to the ambient tier, not to the model
        # serving interactive Sage, and its output must be marked as such.
        await run_db(_mark_generating, surgery_id, prep_id)
        await run_db(_finalize_surgery, surgery_id, PREP_PACKAGE, "template")
        logger.info("Prep package template written for %s", surgery_id)

    try:
        # Hard ceiling — never block longer than 60 seconds total
        await asyncio.wait_for(_do_generation(), timeout=60)
    except Exception as e:
        logger.error("Generation error for %s: %s", surgery_id, e, exc_info=True)
        await run_db(_finalize_surgery, surgery_id, FALLBACK_DATA, "failed", f"Error: {str(e)}")


def get_emergency_queue() -> list:
    """Returns all surgeries currently in emergency workflow."""
    with db_session() as db:
        rows = db.execute(
            """SELECT s.*, p.name as patient_name, p.age, p.sex, p.mrn, p.bed 
               FROM surgeries s
               JOIN patients p ON s.patient_id = p.id
               WHERE s.status LIKE 'emergency_%'
               ORDER BY s.created_at DESC"""
        ).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        # Parse JSON fields
        for f in [
            "co_morbidities",
            "surgical_checklist",
            "instruments_required",
            "staff_required",
            "possible_complications",
        ]:
            if d.get(f):
                try:
                    d[f] = json.loads(d[f])
                except (ValueError, TypeError) as e:
                    logger.warning("Failed to parse JSON field %r on surgery row: %s", f, e)
                    d[f] = []
            else:
                d[f] = []
        results.append(d)
    return results


def acknowledge_emergency(surgery_id: str):
    with db_session() as db:
        db.execute(
            "UPDATE surgeries SET status = 'emergency_acknowledged' WHERE id = ?", (surgery_id,)
        )
