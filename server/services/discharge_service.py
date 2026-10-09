"""Orb Discharge — Discharge Summary, Timeline & QR Code Service"""
import json
import logging
import os
import re
import uuid
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from config import LLM_MODEL, UPLOAD_DIR, DISCHARGE_ACCESS_TTL_DAYS, OLLAMA_KEEP_ALIVE
from database import get_db, db_session
from services.adt import end_stay as adt_end
from services.adt.core import AdtConflict, adt_on, current_admission_on
from services.audit_service import log_audit_event
from services.vital_charting import charted_text
import httpx

logger = logging.getLogger(__name__)

# ── Review state (migration 41) ──────────────────────────────────────────────
# A stored summary is the drafting model's output until a prescriber has read
# it. Patient-facing readers (Bridge, the discharge link) show the text only
# when it is 'reviewed'; until then the patient is told it is being finalised.
REVIEW_DRAFT = "draft"
REVIEW_REVIEWED = "reviewed"
PENDING_REVIEW_MESSAGE = "Your discharge summary is being finalised by your doctor."

#: Why discharge_patient cancels a still-active care bundle (audited). The
#: closure itself moved to services/adt/end_stay.close_admission_on (adt-8a),
#: which owns the string; this name is kept for callers and tests.
DISCHARGE_BUNDLE_REASON = adt_end.DISCHARGE_BUNDLE_REASON


def is_reviewed(row) -> bool:
    """True only for a summary a clinician has marked reviewed. A row read
    before migration 41 (no column) counts as a draft, never as reviewed."""
    if row is None:
        return False
    keys = row.keys() if hasattr(row, "keys") else ()
    return "review_status" in keys and row["review_status"] == REVIEW_REVIEWED


def qr_code_url_for(patient_id: str, qr_code_path) -> Optional[str]:
    """The authenticated API path that serves a summary's QR, or None when no
    QR was written. Never the file path: that is a location on the appliance's
    disk and means nothing to a browser."""
    return f"/api/vigil/patients/{patient_id}/qr" if qr_code_path else None


def _site_today() -> str:
    """Today's date where the hospital is, written with the month as a word
    ("2 Oct 2026") so it cannot be read two ways. Uses the site timezone when
    one is configured, else the appliance's own zone."""
    from config import HL7_DEFAULT_TZ
    now = datetime.now(timezone.utc)
    try:
        if HL7_DEFAULT_TZ:
            from zoneinfo import ZoneInfo
            now = now.astimezone(ZoneInfo(HL7_DEFAULT_TZ))
        else:
            now = now.astimezone()
    except Exception:  # an unknown zone name must not stop a discharge
        now = now.astimezone()
    return f"{now.day} {now.strftime('%b %Y')}"


def _clinical_date(value) -> str:
    """A stored date or timestamp as "12 Aug 2026", or "Not recorded"."""
    if not value:
        return "Not recorded"
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return text
    return f"{parsed.day} {parsed.strftime('%b %Y')}"


def _recorded_context(db, patient_id: str) -> dict:
    """Facts the drafting prompt used to leave the model to invent: the
    attending doctor's name, the charted allergies and the comorbidities.

    An allergy list that cannot be read says so. It is never rendered as "no
    allergies", which would be the dangerous reading of a failed query.
    """
    attending = db.execute(
        "SELECT u.name FROM patients p JOIN users u ON u.id = p.attending_doctor_id "
        "WHERE p.id = ?",
        (patient_id,),
    ).fetchone()

    try:
        allergy_rows = db.execute(
            "SELECT substance, reaction, severity FROM patient_allergies "
            "WHERE patient_id = ? AND status = 'active' ORDER BY substance",
            (patient_id,),
        ).fetchall()
        if allergy_rows:
            allergies = "; ".join(
                a["substance"]
                + (f" ({', '.join(x for x in (a['reaction'], a['severity']) if x and x != 'unknown')})"
                   if (a["reaction"] or (a["severity"] and a["severity"] != "unknown")) else "")
                for a in allergy_rows
            )
        else:
            allergies = "No allergies recorded on the allergy list"
    except Exception:
        logger.warning("discharge draft: allergy list unreadable for %s", patient_id, exc_info=True)
        allergies = "Allergy list could not be read: check the chart before discharge"

    comorbidities = "None recorded"
    try:
        prof = db.execute(
            "SELECT comorbidities FROM patient_profiles WHERE patient_id = ?", (patient_id,)
        ).fetchone()
        parsed = parse_comorbidities(prof["comorbidities"] if prof else None)
        if parsed:
            comorbidities = ", ".join(parsed)
    except Exception:
        logger.debug("discharge draft: no patient profile for %s", patient_id, exc_info=True)

    return {
        "attending": (attending["name"] if attending and attending["name"] else None),
        "allergies": allergies,
        "comorbidities": comorbidities,
    }


def parse_comorbidities(raw) -> list:
    """patient_profiles.comorbidities is a JSON list from the admission form, or
    free text from older rows. Either way: a list of non-empty strings."""
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            items = [part for part in str(raw).split(",")]
        if not isinstance(items, list):
            items = [str(items)]
    return [str(i).strip() for i in items if str(i).strip()]


# The template slots the drafting prompt used to hand the model. Any the model
# echoes back unfilled are replaced, so "[Doctor name]" can never reach a
# printed summary. Our own bracketed markers ("[PRN: as needed]",
# "[UNVERIFIED: ...]") carry a colon and are deliberately not matched.
_PLACEHOLDER_RE = re.compile(
    r"\[(?:name|number|details|date|today'?s date|doctor name|attending[^\]\n:]{0,40}|"
    r"primary and secondary diagnoses|detailed summary[^\]\n:]{0,120}|"
    r"list any procedures|current status[^\]\n:]{0,80}|"
    r"list medications[^\]\n:]{0,80}|specific follow-up[^\]\n:]{0,80}|"
    r"important instructions[^\]\n:]{0,80})\]",
    re.IGNORECASE,
)


def strip_unresolved_placeholders(text: str) -> str:
    """Replace any template slot the model left unfilled with "Not documented"."""
    if not text:
        return text
    return _PLACEHOLDER_RE.sub("Not documented", text)

# QR code generation
try:
    import qrcode
    from qrcode.image.pure import PyPNGImage
    HAS_QR = True
except ImportError:
    HAS_QR = False


def build_patient_timeline(patient_id: str) -> list:
    """Build a chronological timeline of all patient events."""
    db = get_db()
    timeline = []

    # Admission
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if patient:
        timeline.append({
            "timestamp": patient["admission_date"],
            "type": "admission",
            "title": "Admitted",
            "detail": f"Admitted to {patient['ward']} · Bed {patient['bed']} · Diagnosis: {patient['diagnosis']}",
        })

    # Clinical notes
    notes = db.execute(
        "SELECT * FROM clinical_documents WHERE patient_id = ? ORDER BY created_at ASC",
        (patient_id,),
    ).fetchall()
    for note in notes:
        # A simulator's result (migration 46) says so on the timeline, which
        # is stored with the summary and outlives the chart's own labels.
        simulated = "source" in note.keys() and note["source"] == "simulated"
        timeline.append({
            "timestamp": note["created_at"],
            "type": "note",
            "title": f"{note['doc_type'].upper()} Note" + (" (simulated)" if simulated else ""),
            "detail": note["content"][:200] + ("…" if len(note["content"]) > 200 else ""),
        })

    # Scribe notes
    scribe_notes = db.execute(
        "SELECT * FROM scribe_notes WHERE patient_id = ? ORDER BY created_at ASC",
        (patient_id,),
    ).fetchall()
    for sn in scribe_notes:
        timeline.append({
            "timestamp": sn["created_at"],
            "type": "scribe",
            "title": f"Scribe {(sn['note_format'] or 'raw').upper()}",
            "detail": (sn["structured_note"] or sn["raw_transcript"])[:200],
        })

    # Lens analyses: clinician-reviewed reads only, with the text the clinician
    # confirmed. The raw `analysis` is an unvalidated model draft, and the lab
    # bot's 'lab' copies are lab reports already listed above as notes.
    from services.lens_service import REVIEWED_IMAGING_SQL
    analyses = db.execute(
        f"SELECT * FROM lens_analyses WHERE patient_id = ? AND {REVIEWED_IMAGING_SQL} "
        "ORDER BY created_at ASC",
        (patient_id,),
    ).fetchall()
    for a in analyses:
        timeline.append({
            "timestamp": a["created_at"],
            "type": "imaging",
            "title": f"Image Analysis ({a['image_type']}), clinician reviewed",
            "detail": a["reviewed_text"][:200],
        })

    # Risk alerts (critical only). Scoring switched off (ORB_SCORING_ENABLED,
    # the Orb Ward Record): a band stored before the switch is not put on the
    # discharge timeline, which is stored with the summary.
    from services.vigil_service import scoring_enabled
    risk_events = db.execute(
        "SELECT * FROM risk_scores WHERE patient_id = ? AND level = 'critical' ORDER BY timestamp ASC, id ASC",
        (patient_id,),
    ).fetchall() if scoring_enabled() else []
    for r in risk_events:
        drivers = json.loads(r["drivers"]) if r["drivers"] else []
        timeline.append({
            "timestamp": r["timestamp"],
            "type": "alert",
            # Show the NEWS2 clinical band, not the normalized index as a
            # percentage — the 0-1 index is not a deterioration probability.
            "title": f"Critical Alert (NEWS2 band: {r['level']})",
            "detail": ", ".join(drivers),
        })

    db.close()

    # Sort by timestamp
    timeline.sort(key=lambda x: x["timestamp"] or "")
    return timeline


def build_medication_reconciliation(patient_id: str) -> dict:
    """Deterministic discharge medication reconciliation from the medications table.

    Medication reconciliation is the #1 discharge safety intervention. This is
    computed from the real charted medications (NOT LLM-generated), splitting them
    into continued vs. stopped, and flagging any AI-extracted/agentic medication
    that a pharmacist has not yet verified so it is never silently carried onto a
    discharge script. The structured result is both returned (for the API/UI) and
    rendered into authoritative text injected into the summary prompt.

    Empty on an Orb Ward Record computer, whose drug chart stays on paper
    (services/drug_chart): nothing is read, and the stored summary then
    carries no reconciliation block (med_rec_section).
    """
    from services.drug_chart import drug_chart_enabled
    if not drug_chart_enabled():
        rows = []
    else:
        db = get_db()
        try:
            rows = db.execute(
                """SELECT drug_name, dosage, route, frequency, status, source, verified_by
                   FROM medications WHERE patient_id = ? ORDER BY drug_name COLLATE NOCASE""",
                (patient_id,),
            ).fetchall()
        finally:
            db.close()

    continued, stopped, unverified = [], [], []
    for r in rows:
        m = dict(r)
        entry = {
            "name": m.get("drug_name"),
            "dosage": m.get("dosage"),
            "route": m.get("route"),
            "frequency": m.get("frequency"),
            "source": m.get("source") or "clinician",
        }
        # AI-extracted / agentic meds not yet pharmacist-verified are called out
        # explicitly rather than presented as a clean discharge medication.
        if (m.get("source") in ("llm_extracted", "agentic")) and not m.get("verified_by"):
            entry["needs_verification"] = True
            unverified.append(entry)
        status = m.get("status")
        if status == "active":
            continued.append(entry)
        elif status == "prn":
            # PRN ("as needed") is a live, continued medication — it must appear on
            # the discharge script, not silently vanish (a dropped PRN opioid is a
            # real safety defect). Flag it so the summary can label it PRN.
            entry["prn"] = True
            continued.append(entry)
        elif status == "discontinued":
            stopped.append(entry)
    return {
        "continued": continued,
        "stopped": stopped,
        "unverified": unverified,
        "counts": {
            "continued": len(continued),
            "stopped": len(stopped),
            "unverified": len(unverified),
        },
    }


def med_rec_section(rec: dict) -> str:
    """The reconciliation block for a stored summary, or nothing at all on an
    Orb Ward Record computer, where the summary says nothing about medicines
    (services/drug_chart)."""
    from services.drug_chart import drug_chart_enabled
    return _render_med_rec_text(rec) if drug_chart_enabled() else ""


def _render_med_rec_text(rec: dict) -> str:
    """Render the reconciliation into authoritative text for the summary prompt."""
    def _fmt(e):
        parts = [e.get("name") or "?"]
        detail = " ".join(x for x in (e.get("dosage"), e.get("route"), e.get("frequency")) if x)
        if detail:
            parts.append(f"({detail})")
        if e.get("prn"):
            parts.append("[PRN: as needed]")
        if e.get("needs_verification"):
            parts.append("[UNVERIFIED: pharmacist review required]")
        return " ".join(parts)

    lines = ["MEDICATION RECONCILIATION (authoritative, from the medication record):"]
    lines.append("Continue on discharge:")
    lines += [f"  - {_fmt(e)}" for e in rec["continued"]] or ["  - None"]
    lines.append("Stopped during admission:")
    lines += [f"  - {_fmt(e)}" for e in rec["stopped"]] or ["  - None"]
    if rec["unverified"]:
        lines.append("WARNING: AI-extracted medications NOT yet pharmacist-verified "
                     "(do not include on the discharge script until verified):")
        lines += [f"  - {_fmt(e)}" for e in rec["unverified"]]
    return "\n".join(lines)


def _draft_inputs(patient_id: str) -> Optional[dict]:
    """Every recorded fact the drafting prompt needs, read in ONE sync hop.

    PURE SYNC, reached only through run_db: generate_discharge_summary is an
    async def, and these reads used to run on the event loop (the 2026-07-30
    wedge shape). None when there is no such patient."""
    with db_session() as db:
        patient = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if not patient:
            return None

        # Gather all clinical data
        notes = db.execute(
            "SELECT doc_type, content, created_at, source FROM clinical_documents "
            "WHERE patient_id = ? ORDER BY created_at ASC",
            (patient_id,),
        ).fetchall()

        # Latest vitals. id DESC: on a same-timestamp tie the later write (a correction) wins.
        latest_vitals = db.execute(
            "SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL "
            "ORDER BY timestamp DESC, id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()

        # Risk history. None while scoring is switched off: the draft then
        # names no NEWS2 band stored before the switch.
        from services.vigil_service import scoring_enabled
        risks = db.execute(
            "SELECT score, level, drivers, timestamp FROM risk_scores WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 5",
            (patient_id,),
        ).fetchall() if scoring_enabled() else []

        recorded = _recorded_context(db, patient_id)
    return {
        "patient": patient,
        "notes": notes,
        "latest_vitals": latest_vitals,
        "risks": risks,
        "recorded": recorded,
        "med_rec": build_medication_reconciliation(patient_id),
    }


async def generate_discharge_summary(patient_id: str) -> str:
    """Generate a comprehensive discharge summary using LLM.

    Returns "" when the careful model is unloaded; the discharge route then
    stores offline_discharge_summary (recorded data only, labelled as such).
    """
    from database import run_db

    # Models switched off (ORB_MODELS_ENABLED, or the Orb Ward Record): nothing
    # is drafted and the gateway is not asked; the route stores the recorded
    # data only (offline_discharge_summary), which then says so.
    from services.model_switch import models_enabled
    if not models_enabled():
        return ""

    # Through the residency router: asking for LLM_MODEL by name silently
    # re-loaded a careful model an administrator had just unloaded. The careful
    # model or nothing: no discharge summary is drafted on the smaller model.
    from services.model_router import select_chat_model
    choice = await select_chat_model(preferred=LLM_MODEL)
    if choice.downgraded:
        logger.info("discharge draft: careful model '%s' unloaded, recorded data only for %s",
                    choice.preferred, patient_id)
        return ""

    inputs = await run_db(_draft_inputs, patient_id)
    if inputs is None:
        return "Patient not found."
    patient = inputs["patient"]
    notes = inputs["notes"]
    latest_vitals = inputs["latest_vitals"]
    risks = inputs["risks"]
    recorded = inputs["recorded"]
    discharge_date = _site_today()
    attending = recorded["attending"] or "Not recorded"

    # Build context for LLM. A simulator's result is named as one, so the
    # draft never reports a fabricated lab as the patient's.
    notes_text = "\n".join([
        f"[{n['doc_type']}{' (SIMULATED, not a real result)' if n['source'] == 'simulated' else ''}] "
        f"{n['created_at']}: {n['content']}"
        for n in notes
    ])
    vitals_text = ""
    if latest_vitals:
        # AS CHARTED (services/vital_charting.py), the numbers NEWS2 scored; a
        # missing vital prints "--" instead of raising TypeError on :.0f.
        lv = latest_vitals
        vitals_text = (
            f"Latest vitals: HR {charted_text(lv['heart_rate'])}, "
            f"BP {charted_text(lv['systolic_bp'])}/{charted_text(lv['diastolic_bp'])}, "
            f"SpO2 {charted_text(lv['spo2'], unit='%')}, "
            f"Temp {charted_text(lv['temperature'], 1, unit='°C')}, "
            f"RR {charted_text(lv['respiratory_rate'])}"
        )

    risk_text = ""
    if risks:
        # Report the NEWS2 band, not the normalized index as a percentage.
        risk_text = f"Current NEWS2 band: {risks[0]['level']}"

    # Authoritative medication reconciliation from the real medication record
    # (read in _draft_inputs, off the loop).
    med_rec_text = med_rec_section(inputs["med_rec"])

    # The header lines are FILLED here, not left as slots. The model was asked
    # for "Discharge: [Today's date]" and "[Doctor name]" without being given
    # either, so it invented a date and echoed the slot (2026-10-01). Allergies
    # and comorbidities are given for the same reason: a summary drafted without
    # them can only omit or invent them.
    header = (
        f"Patient: {patient['name']} | MRN: {patient['mrn']} | "
        f"Age/Sex: {patient['age']}/{patient['sex']}\n"
        f"Admission: {_clinical_date(patient['admission_date'])} → Discharge: {discharge_date}"
    )

    prompt = f"""Draft a discharge summary for this patient from the recorded data below.
Use only what is recorded. Where a section has no recorded information, write
"Not documented". Do not invent findings, procedures, follow-up appointments or
a condition at discharge.

PATIENT: {patient['name']}, Age {patient['age']}, {patient['sex']}
MRN: {patient['mrn']}
ADMISSION DATE: {_clinical_date(patient['admission_date'])}
DISCHARGE DATE: {discharge_date}
DIAGNOSIS: {patient['diagnosis']}
WARD: {patient['ward']} · BED: {patient['bed']}
ATTENDING DOCTOR: {attending}
ALLERGIES (from the allergy record): {recorded['allergies']}
COMORBIDITIES (from the admission record): {recorded['comorbidities']}

{vitals_text}
{risk_text}

CLINICAL NOTES:
{notes_text if notes_text else 'No clinical notes recorded.'}

{med_rec_text}
(Use the MEDICATION RECONCILIATION above verbatim for the DISCHARGE MEDICATIONS
section: do not invent, add, or omit medications. Reproduce any UNVERIFIED
warning.)

FORMAT (copy the first two lines exactly as written):
DISCHARGE SUMMARY
═══════════════════

{header}

ADMISSION DIAGNOSIS:

ALLERGIES:

COMORBIDITIES:

HOSPITAL COURSE:

PROCEDURES PERFORMED:

CONDITION AT DISCHARGE:

DISCHARGE MEDICATIONS:

FOLLOW-UP INSTRUCTIONS:

PATIENT EDUCATION:

ATTENDING DOCTOR:
{attending}

---
AI-drafted by Orb. Not reviewed until a clinician marks it reviewed.
"""

    # Call LLM
    full_response = ""
    from services import ollama_gateway
    from services.llm_queue_service import llm_queue
    await llm_queue.acquire(priority=2)
    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await ollama_gateway.post(
                client,
                "/api/chat",
                {
                    "model": choice.model,
                    "messages": [
                        {
                            "role": "system",
                            # A draft for a doctor to review, from recorded data
                            # only. "Comprehensive... include all relevant
                            # details" invited the model to fill gaps.
                            "content": "You draft hospital discharge summaries for a doctor to review. Use only the recorded data you are given. Where nothing is recorded, write 'Not documented'. Never invent findings, dates, names, procedures or follow-up.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                },
                purpose="discharge_summary",
                patient_id=patient_id,
            )
            data = response.json()
            full_response = data.get("message", {}).get("content", "")
    finally:
        llm_queue.release()

    return strip_unresolved_placeholders(full_response)


def offline_discharge_summary(patient_id: str) -> str:
    """The summary stored when the drafting model is unavailable: recorded data
    only, and it says what it is.

    The old fallback asserted "Stable for discharge" and "Clinical documentation
    and monitoring were maintained throughout the stay" about every patient, and
    stamped a raw ISO time. A sentence about the patient's condition is a
    clinical judgement, and nothing here made one.
    """
    with db_session() as db:
        patient = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if patient is None:
            return "Patient not found."
        recorded = _recorded_context(db, patient_id)
    med_rec_text = med_rec_section(build_medication_reconciliation(patient_id))
    # Models switched off on this computer: say that, not that the assistant
    # was unavailable, which reads as a fault.
    from services.model_switch import models_enabled
    if models_enabled():
        course = "Not documented. The drafting assistant was unavailable, so no course was drafted."
        footer = "Drafting assistant unavailable. This summary holds recorded data only."
    else:
        course = ("Not documented. The drafting assistant is switched off on this computer, "
                  "so no course was drafted.")
        footer = ("Drafting assistant switched off on this computer. "
                  "This summary holds recorded data only.")
    return f"""DISCHARGE SUMMARY
═══════════════════

Patient: {patient['name']} | MRN: {patient['mrn']} | Age/Sex: {patient['age']}/{patient['sex']}
Admission: {_clinical_date(patient['admission_date'])} → Discharge: {_site_today()}

ADMISSION DIAGNOSIS:
{patient['diagnosis'] or 'Not documented'}

WARD: {patient['ward'] or 'Not recorded'} · BED: {patient['bed'] or 'Not recorded'}

ALLERGIES:
{recorded['allergies']}

COMORBIDITIES:
{recorded['comorbidities']}

HOSPITAL COURSE:
{course}

CONDITION AT DISCHARGE:
Not documented.

{med_rec_text}

ATTENDING DOCTOR:
{recorded['attending'] or 'Not recorded'}

---
{footer}
"""


def generate_qr_code(patient_id: str, summary_url: str) -> str:
    """Generate a QR code linking to the discharge summary."""
    if not HAS_QR:
        return ""
    
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(summary_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    filename = f"qr_{patient_id}_{uuid.uuid4().hex[:6]}.png"
    qr_path = str(UPLOAD_DIR / filename)
    img.save(qr_path)
    
    return qr_path


def discharge_patient(patient_id: str, author_id: str, summary: str, timeline: list, *,
                      discharge_type: Optional[str] = None, confirm_mrn: Optional[str] = None,
                      reason: Optional[str] = None, request_meta: Optional[dict] = None) -> dict:
    """Discharge a patient: update status, save summary, generate secure QR token.

    BEDS AND ADMISSIONS (adt-8a). The status flips and the bundle closure live
    in services/adt/end_stay.close_admission_on, and every discharge asks
    discharge_preconditions first (empty until held ruling R009 lands). With
    ORB_ADT off that is all, and the rows written are exactly what they always
    were; the keyword arguments are ignored. With ORB_ADT on the discharge
    also records how the stay ended (routine when `discharge_type` is not
    given; referred_out, transferred_out or dama; LAMA, absconded and death go
    to POST /api/adt/admissions/{id}/end), with the typed MRN read-back and a
    reason for a DAMA, and writes, in this same immediate transaction, the
    admission's discharged_at, discharge_type and discharged_by, the
    'discharge' movement carrying this summary's id, the de-duplicated NABH
    disposition and a clinical_access audit. The response then carries an
    'end_of_stay' block listing the open escalation chains (never closed) and
    the bundles it closed.

    STAY KEYING (opd-17b, every flag). The summary carries admission_id, the
    stay it ends (migration 65), so a second stay on the same record is never
    read as the first: the active admission the close below ends, or NULL for
    a patient with no admission row.

    THE LINK IS CONDITIONAL, THE DISCHARGE IS NOT. A secure link is only printed
    when the patient has a second factor the portal can actually check — see
    services/discharge_access_service.second_factor_status, which is the same
    check verification uses. Where there is none, the discharge completes in
    full (status, summary, timeline) and the response carries
    secure_link_issued=False with a reason for the discharge desk. Before this,
    EVERY discharge printed a URL and QR that were mathematically impossible to
    open: unlocking needs a stored date of birth and no patient had one (pilot
    dry run, 2026-08-14). Handing a patient a code that only ever answers
    "Incorrect Date of Birth" is worse than handing them nothing, because they
    leave believing they have their record.

    The same holds for the address the link is built on: in the prod profile
    no link is issued while ORB_PUBLIC_APP_BASE_URL is unset, not https, or
    points at the appliance itself (see
    discharge_access_service.public_link_base_status).
    """
    from services.discharge_access_service import (
        SECOND_FACTOR_FIELD, public_link_base_status, second_factor_status,
    )

    # Beds and admissions (ORB_ADT, default off). Off: nothing below changes.
    # On: the end type is checked before anything is written or drafted.
    adt = adt_on()
    end_type = adt_end.discharge_route_type(discharge_type) if adt else None

    factor = second_factor_status(patient_id)
    address = public_link_base_status()
    issue_link = bool(factor["available"]) and bool(address["available"])
    # Both reasons when both fail: recording a date of birth alone would still
    # leave the desk with no link. Not when the hospital switched links off:
    # there, "record the date of birth, then issue the link" sends the desk
    # after a link that will never come.
    withheld_reason = None if issue_link else (
        address["reason"] if address.get("switched_off") else " ".join(
            r for r in (address["reason"], factor["reason"]) if r
        )
    )

    # Generate secure token. Only the SHA-256 hash is persisted (token_id and
    # token_hash both hold the hash); the raw token exists solely in the
    # patient-facing link/QR, so a DB or audit-log leak cannot yield a usable
    # discharge link. Lookups hash the presented token (see
    # discharge_access_service.get_token_record).
    raw_token = token_hash = token_id = summary_url = None
    qr_path = None
    expires_at = datetime.now(timezone.utc) + timedelta(days=DISCHARGE_ACCESS_TTL_DAYS)
    if issue_link:
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        token_id = token_hash
        summary_url = f"{address['base_url']}/discharge/{raw_token}"
        # Written only on the issuing path, so a refused link leaves no orphan
        # PNG in the uploads directory.
        qr_path = generate_qr_code(patient_id, summary_url)

    end_record = None
    # Immediate only with ORB_ADT on, where the read-back and the end-of-stay
    # rows must see the same state as the write; off, the session is the one
    # this function always used.
    with db_session(immediate=adt) as db:
        admission = patient_row = None
        escalations: list = []
        if adt:
            # Captured BEFORE the close below flips it: its place is the
            # discharge movement's 'from', and its stay start dates the death.
            admission = current_admission_on(db, patient_id)
            found = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
            patient_row = dict(found) if found is not None else None
            if admission is not None and (patient_row or {}).get("status") == "admitted":
                if end_type in adt_end.READ_BACK_ON_DISCHARGE_ROUTE:
                    adt_end.check_read_back(patient_row, confirm_mrn)
                reason = adt_end.require_reason(end_type, reason)
            escalations = adt_end.open_escalations_on(db, patient_id)

        # Every end-of-stay door asks this; held ruling R009 lands there. It
        # returns [] today, so no discharge is refused here.
        problems = adt_end.discharge_preconditions(db, patient_id, end_type or "routine")
        if problems:
            raise AdtConflict("This stay cannot end yet", code="discharge_preconditions",
                              preconditions=problems)

        # The stay this summary ends (opd-17b, every flag): the patient's
        # active admission, read BEFORE the close below flips it. Migration 65
        # lets a patient hold one active admission only, so this is the stay
        # being closed. None for a patient with no admission row (records
        # written before admissions existed): the summary then carries NULL,
        # which every stay-keyed reader treats as "not known".
        stay = admission if admission is not None else current_admission_on(db, patient_id)
        stay_admission_id = (stay or {}).get("id")

        # Atomicity / idempotency: claim the discharge first. If the patient was
        # not 'admitted' (already discharged), bail before writing duplicate
        # summaries/tokens. close_admission_on makes the claim, closes every
        # active admission and cancels the open care bundles with an audit row
        # each (F050), exactly as this function did inline before adt-8a.
        closed = adt_end.close_admission_on(db, patient_id, actor_user_id=author_id)
        if closed is None:
            return {"status": "already_discharged", "patient_id": patient_id}

        # Save discharge summary. Always a DRAFT: whatever produced the text (the
        # drafting model, the offline fallback, a Relay action), no clinician
        # has read it yet. See mark_discharge_summary_reviewed.
        db.execute(
            """INSERT INTO discharge_summaries
               (patient_id, author_id, summary, timeline_json, qr_code_path, review_status,
                admission_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (patient_id, author_id, summary, json.dumps(timeline), qr_path, REVIEW_DRAFT,
             stay_admission_id)
        )
        summary_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        discharged_at = db.execute(
            "SELECT discharged_at FROM discharge_summaries WHERE id = ?", (summary_id,)
        ).fetchone()[0]

        # Save access token — only when one was issued. A token row with no
        # openable factor is the dead link in database form.
        if issue_link:
            db.execute(
                """INSERT INTO discharge_access_tokens
                   (token_id, token_hash, patient_id, expires_at, created_by_user_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (token_id, token_hash, patient_id, expires_at.isoformat(), author_id)
            )

        # (patient status already claimed atomically at the top of this block)

        # ORB_ADT on: how the stay ended, in this same transaction (an
        # unwritable clinical_access audit rolls the whole discharge back).
        if adt and admission is not None:
            actor_row = db.execute("SELECT role FROM users WHERE id = ?", (author_id,)).fetchone()
            end_record = adt_end.record_end_on(
                db, admission=admission, patient=patient_row, end_type=end_type,
                occurred=datetime.now(timezone.utc),
                actor={"id": author_id, "role": actor_row["role"] if actor_row else None},
                reason=reason, summary_id=summary_id, door="discharge",
                request_meta=request_meta, bundles_cancelled=closed["bundles_cancelled"],
                open_escalations=escalations,
            )

    if issue_link:
        log_audit_event(
            correlation_id=token_id,
            event_category="security",
            action_type="discharge_link_created",
            outcome="success",
            summary_message=f"Discharge QR code and token created, expires in {DISCHARGE_ACCESS_TTL_DAYS} days",
            patient_id=patient_id,
            actor_user_id=author_id
        )
    else:
        # Audited as a refusal, not a silence: the discharge desk sees the reason
        # in the response, and the record shows why no link left the building.
        causes = []
        if address.get("switched_off"):
            causes.append("discharge links switched off at this hospital (paper only)")
        elif not address["available"]:
            causes.append(f"no patient-reachable address ({address['problem']})")
        if not factor["available"]:
            causes.append(f"no usable second factor ({SECOND_FACTOR_FIELD}) on file")
        log_audit_event(
            correlation_id=f"discharge-link-withheld-{patient_id}-{summary_id}",
            event_category="security",
            action_type="discharge_link_not_issued",
            outcome="success",
            summary_message="No secure discharge link issued, " + "; ".join(causes),
            patient_id=patient_id,
            actor_user_id=author_id,
        )

    result = {
        "status": "success",
        "summary_id": summary_id,
        "discharged_at": discharged_at,
        "review_status": REVIEW_DRAFT,
        "summary_url": summary_url,
        # The API path, never qr_path: that is a location on the appliance's
        # disk, it means nothing to a browser, and the client looked for
        # qr_code_url and so never showed the code at all.
        "qr_code_url": qr_code_url_for(patient_id, qr_path),
        "secure_link_issued": issue_link,
        "secure_link_reason": withheld_reason,
    }
    if end_record is not None:
        result["end_of_stay"] = end_record
    return result


class DischargeSummaryNotFound(LookupError):
    """No such summary for this patient."""


def mark_discharge_summary_reviewed(patient_id: str, summary_id: int,
                                    reviewer_id: str, reviewer_role: Optional[str] = None) -> dict:
    """A prescriber has read the drafted summary: release it to the patient.

    Interim step until the draft, edit and sign flow (A002): this records that
    a named clinician read the text as stored. It does not change the text.
    Idempotent: a second call on a reviewed summary returns the first review
    unchanged rather than moving its time or reviewer.

    The review and its audit row commit together (clinical_access is
    fail-closed), so a release to the patient can never go unrecorded.
    """
    with db_session() as db:
        row = db.execute(
            "SELECT id, review_status, reviewed_by, reviewed_at FROM discharge_summaries "
            "WHERE id = ? AND patient_id = ?",
            (summary_id, patient_id),
        ).fetchone()
        if row is None:
            raise DischargeSummaryNotFound(summary_id)
        if row["review_status"] != REVIEW_REVIEWED:
            reviewed_at = datetime.now(timezone.utc).isoformat()
            db.execute(
                "UPDATE discharge_summaries SET review_status = ?, reviewed_by = ?, reviewed_at = ? "
                "WHERE id = ? AND review_status != ?",
                (REVIEW_REVIEWED, reviewer_id, reviewed_at, summary_id, REVIEW_REVIEWED),
            )
            log_audit_event(
                correlation_id=f"discharge-summary-reviewed-{summary_id}",
                event_category="clinical_access",
                action_type="discharge_summary_reviewed",
                outcome="success",
                db=db,
                summary_message=(
                    f"Discharge summary {summary_id} marked reviewed and released to the patient"
                ),
                actor_user_id=reviewer_id,
                actor_role=reviewer_role,
                patient_id=patient_id,
                target_type="discharge_summary",
                target_id=str(summary_id),
            )
        return load_discharge_summary_view(patient_id, db=db)


def load_discharge_summary_view(patient_id: str, db=None) -> Optional[dict]:
    """The latest discharge summary for the clinician's screens, with its review
    state. Clinician-facing: the draft text is included and labelled by
    review_status, because the doctor is the one who has to read it. Sync."""
    def _read(conn):
        return conn.execute(
            """SELECT ds.*, u.name AS reviewed_by_name
               FROM discharge_summaries ds
               LEFT JOIN users u ON u.id = ds.reviewed_by
               WHERE ds.patient_id = ?
               ORDER BY ds.discharged_at DESC, ds.id DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()

    if db is not None:
        row = _read(db)
    else:
        with db_session() as own:
            row = _read(own)
    if row is None:
        return None
    keys = row.keys()
    try:
        timeline = json.loads(row["timeline_json"]) if row["timeline_json"] else []
    except (TypeError, ValueError):
        timeline = []
    return {
        "summary_id": row["id"],
        "summary": row["summary"],
        "review_status": row["review_status"] if "review_status" in keys else REVIEW_DRAFT,
        "reviewed_by_name": row["reviewed_by_name"],
        "reviewed_at": row["reviewed_at"] if "reviewed_at" in keys else None,
        "timeline": timeline,
        "qr_code_url": qr_code_url_for(patient_id, row["qr_code_path"]),
        "discharged_at": row["discharged_at"],
    }
