"""Orb Surgery — Service Layer

Provides surgical schedule queries, detail retrieval, and MedGemma risk assessment.
"""
import json
import logging
from database import db_session
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _parse_timestamp(value) -> datetime | None:
    """Parse one of the timestamp shapes this schema actually stores, or None.

    ``surgeries.started_at`` / ``completed_at`` are written by Orb as ISO-8601
    UTC (``datetime.now(timezone.utc).isoformat()``), but the column is a plain
    SQLite TIMESTAMP: a row written by a DBA, a restore, or a future DEFAULT
    CURRENT_TIMESTAMP can hold "YYYY-MM-DD HH:MM:SS" instead, and anything
    round-tripped through some clients can carry a trailing 'Z'. All three are
    accepted; anything else is None rather than an exception, because a
    turnaround figure must never be the reason a clinical read 500s.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def turnaround_minutes(started_at, completed_at) -> float | None:
    """Minutes from knife-in to patient-out, or None when it is not measurable.

    THE POINT OF THE WHOLE COLUMN PAIR. Workflow 5 is sold on surgical
    turnaround, and before migration 27 the `surgeries` table recorded no time
    of any kind — only when the case was BOOKED. This is the one derivation;
    nothing should recompute it inline.

    None, never a guess, in every case where the answer is not known:
      * either end missing — a legacy case, or one started before migration 27;
      * either end unparseable;
      * naive/aware mismatch — comparing them raises, and a turnaround that
        silently assumes a timezone is worse than no turnaround;
      * NEGATIVE — completed before started. That is a corrupt pair (clock skew,
        a hand-edited row), not a fast operation, and reporting it would drag a
        ward's mean turnaround down with a number that never happened. Logged so
        it is findable rather than merely absent.
    """
    start = _parse_timestamp(started_at)
    end = _parse_timestamp(completed_at)
    if start is None or end is None:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):
        logger.warning(
            "[Surgery] turnaround not computed: mixed naive/aware timestamps "
            "(%r -> %r)", started_at, completed_at,
        )
        return None
    delta = (end - start).total_seconds() / 60.0
    if delta < 0:
        logger.warning(
            "[Surgery] turnaround not computed: completed_at precedes started_at "
            "(%r -> %r)", started_at, completed_at,
        )
        return None
    return round(delta, 1)


def get_weekly_schedule(surgeon_id: str, days: int = 14) -> list:
    """Get the surgeon's weekly surgery schedule grouped by date.

    UNREACHABLE TODAY — no route or service calls this (verified 2026-08-14), and
    it is left in place rather than deleted only because deleting is not this
    change's job. NOT AN AUTHORISATION BOUNDARY: it JOINs `patients` and returns
    name, MRN, age, sex, bed and ward for whatever `surgeon_id` it is handed, and
    `surgeon_id` is a CALLER-NAMEABLE parameter — pass someone else's id and you
    get their list. Anything that wires this to a route must derive scope through
    services/surgery_scope.py first, exactly like routes/surgery_routes.py does.
    """
    today = datetime.now(timezone.utc).date()
    end_date = today + timedelta(days=days)

    with db_session() as db:
        rows = db.execute(
            """SELECT s.*, p.name as patient_name, p.age, p.sex, p.mrn, p.bed, p.ward,
                      rs.level as risk_level, rs.score as risk_score
               FROM surgeries s
               JOIN patients p ON p.id = s.patient_id
               -- Latest risk row: id breaks an exact timestamp tie. The MAX(timestamp)
               -- equality matched every tied row and listed the case twice.
               LEFT JOIN risk_scores rs ON rs.id = (
                   SELECT rs2.id FROM risk_scores rs2 WHERE rs2.patient_id = p.id
                   ORDER BY rs2.timestamp DESC, rs2.id DESC LIMIT 1
               )
               WHERE s.surgeon_id = ?
                 AND s.scheduled_date >= ?
                 AND s.scheduled_date < ?
               ORDER BY s.scheduled_date ASC, s.scheduled_time ASC""",
            (surgeon_id, today.isoformat(), end_date.isoformat()),
        ).fetchall()

    # Group by date
    grouped: dict = {}
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    for r in rows:
        date_str = r["scheduled_date"]
        if date_str not in grouped:
            dt = datetime.fromisoformat(date_str).date() if isinstance(date_str, str) else date_str
            grouped[date_str] = {
                "date": date_str,
                "day_label": day_names[dt.weekday()],
                "is_today": dt == today,
                "surgeries": [],
            }

        grouped[date_str]["surgeries"].append({
            "id": r["id"],
            "patient_id": r["patient_id"],
            "patient_name": r["patient_name"],
            "age": r["age"],
            "sex": r["sex"],
            "mrn": r["mrn"],
            "procedure_name": r["procedure_name"],
            "diagnosis": r["diagnosis"],
            "scheduled_time": r["scheduled_time"],
            "estimated_duration_min": r["estimated_duration_min"],
            "operating_room": r["operating_room"],
            "status": r["status"],
            "risk_level": r["risk_level"] or "stable",
            "risk_score": r["risk_score"],
            "success_probability": r["success_probability"],
        })

    return list(grouped.values())


def get_surgery_detail(surgery_id: str) -> dict:
    """Get full surgery detail with all clinical data."""
    with db_session() as db:
        row = db.execute(
            """SELECT s.*, p.name as patient_name, p.age, p.sex, p.mrn, p.bed, p.ward,
                      p.diagnosis as patient_diagnosis, p.admission_date,
                      rs.level as risk_level, rs.score as risk_score, rs.drivers as risk_drivers
               FROM surgeries s
               JOIN patients p ON p.id = s.patient_id
               -- Latest risk row: id breaks an exact timestamp tie. The MAX(timestamp)
               -- equality matched every tied row and listed the case twice.
               LEFT JOIN risk_scores rs ON rs.id = (
                   SELECT rs2.id FROM risk_scores rs2 WHERE rs2.patient_id = p.id
                   ORDER BY rs2.timestamp DESC, rs2.id DESC LIMIT 1
               )
               WHERE s.id = ?""",
            (surgery_id,),
        ).fetchone()

        if not row:
            return None

        # Parse JSON fields
        def _parse_json(val, default=None):
            if val is None:
                return default or []
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return default or []

        from services.sage_service import _assemble_patient_context
        # dept=None is CORRECT HERE, and the reason changed on 2026-08-14.
        #
        # It used to mean "unscoped because surgery has no scope rule yet" — the
        # module's cross-ward PHI hole, deferred with a note. Surgery now HAS a
        # scope rule: services/surgery_scope.py, enforced at the ROUTE, on the
        # CASE. By the time this function runs the caller has already been
        # authorised to read this surgery and therefore this patient, so
        # re-filtering the chart by the caller's own department here would be
        # wrong, not safer: it would blank the chart for the surgeon and the
        # anaesthetist — the two people the assessment is for — because a
        # theatre department admits nobody.
        #
        # THIS FUNCTION IS NOT AN AUTHORISATION BOUNDARY. It takes a surgery_id
        # and no user, so any NEW caller must run surgery_scope.enforce_surgery_
        # scope (or surgery_in_scope_blocking) before calling it.
        imported_context = _assemble_patient_context(db, row["patient_id"], None)

        return {
            "id": row["id"],
            "patient_id": row["patient_id"],
            "patient_name": row["patient_name"],
            "age": row["age"],
            "sex": row["sex"],
            "mrn": row["mrn"],
            "bed": row["bed"],
            "ward": row["ward"],
            "patient_diagnosis": row["patient_diagnosis"],
            "admission_date": row["admission_date"],
            "procedure_name": row["procedure_name"],
            "diagnosis": row["diagnosis"],
            "scheduled_date": row["scheduled_date"],
            "scheduled_time": row["scheduled_time"],
            "estimated_duration_min": row["estimated_duration_min"],
            "operating_room": row["operating_room"],
            "status": row["status"],
            # Migration 27. NULL on every case that ran before it — the detail
            # payload says "not measured" by carrying None, and never a zero,
            # which a dashboard would happily average.
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "turnaround_min": turnaround_minutes(row["started_at"], row["completed_at"]),
            "co_morbidities": _parse_json(row["co_morbidities"]),
            "surgical_checklist": _parse_json(row["surgical_checklist"]),
            "instruments_required": _parse_json(row["instruments_required"]),
            "staff_required": _parse_json(row["staff_required"]),
            "risk_assessment": row["risk_assessment"],
            "possible_complications": _parse_json(row["possible_complications"]),
            "success_probability": row["success_probability"],
            "risk_level": row["risk_level"] or "stable",
            "risk_score": row["risk_score"],
            "risk_drivers": _parse_json(row["risk_drivers"]),
            "notes": row["notes"],
            "imported_context": imported_context,
        }


async def generate_risk_assessment(surgery_id: str, detail: dict | None = None):
    """Stream a MedGemma risk assessment for a surgery. Yields SSE chunks.

    ``detail`` MUST be supplied by the caller (loaded off the loop via run_db).
    An async generator body runs on the event loop, so the old inline
    ``get_surgery_detail(surgery_id)`` here was a synchronous, multi-table,
    encrypted read executing on the loop — the exact class of stall the repo's
    standing laws forbid. The fallback load below is kept only so a direct
    non-HTTP caller still works, and it goes through run_db too.
    """
    if detail is None:
        from database import run_db
        detail = await run_db(get_surgery_detail, surgery_id)
    if not detail:
        yield f"data: {json.dumps({'error': 'Surgery not found'})}\n\n"
        return

    # The Vigil band, never `risk_score` (2026-10-03): that is the 0-1 index of
    # the NEWS2 aggregate, not a probability, and "(score: 0.55)" invited the
    # model to read it as one. The joined risk_scores row stores no NEWS2
    # integer, so the band is what can be said. No row at all (the score is
    # NOT NULL, so a None means the LEFT JOIN found nothing) is "not recorded":
    # get_surgery_detail's "stable" default is for the UI, not a finding.
    risk_level_text = (detail['risk_level'] if detail.get('risk_score') is not None
                       else "not recorded (no Vigil risk band on file)")

    # Build clinical risk prompt
    prompt = f"""You are Orb AI (MedGemma), a clinical decision support system. Provide a structured pre-operative risk assessment for the following surgery.

PATIENT: {detail['patient_name']}, {detail['age']}y/{detail['sex']}
MRN: {detail['mrn']}
ADMISSION DIAGNOSIS: {detail['patient_diagnosis']}
SURGICAL PROCEDURE: {detail['procedure_name']}
SURGICAL DIAGNOSIS: {detail['diagnosis']}
CO-MORBIDITIES: {', '.join(detail['co_morbidities'])}
CURRENT RISK LEVEL: {risk_level_text}
ESTIMATED DURATION: {detail['estimated_duration_min']} minutes

Provide:
1. **ASA Physical Status Classification** (I-V) with justification
2. **Surgical Risk Score** (low / moderate / high / very high) with explanation
3. **Key Pre-operative Concerns** specific to this patient
4. **Anesthetic Considerations** relevant to the co-morbidities
5. **Intra-operative Risk Factors** to watch for
6. **Post-operative Monitoring Priorities** (first 24-48 hours)
7. **Recommended Pre-operative Optimizations** (if any)
8. **Overall Risk-Benefit Summary**: 2-3 sentences

Be concise, clinically precise, and use standard medical terminology."""

    try:
        import httpx
        from config import OLLAMA_KEEP_ALIVE
        from services import ollama_gateway
        from services.llm_queue_service import llm_queue

        # Route through the residency-aware router so surgical enrichment
        # cannot silently reload an operator-evicted 27B.
        from services.model_router import select_chat_model
        model_choice = await select_chat_model()

        await llm_queue.acquire(priority=2)
        try:
            async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
                async with ollama_gateway.stream(
                    client,
                    "/api/chat",
                    {
                        "model": model_choice.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": True,
                        "options": {"num_predict": 800, "temperature": 0.3},
                        "keep_alive": OLLAMA_KEEP_ALIVE,
                    },
                    purpose="surgery_risk_assessment",
                    patient_id=detail.get("patient_id"),
                ) as response:
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                            if "message" in chunk and "content" in chunk["message"]:
                                text = chunk["message"]["content"]
                                yield f"data: {json.dumps({'content': text})}\n\n"
                        except json.JSONDecodeError:
                            continue
        finally:
            llm_queue.release()

    except Exception as e:
        # No model, no assessment. This fallback used to print "ASA Class III"
        # and an estimated success rate built from the first two comorbidities,
        # a classification nobody had made, under the AI Risk Assessment title.
        logger.warning("[Surgery] Risk assessment unavailable for %s: %s", surgery_id, e)
        fallback = ("Assessment unavailable. The AI pre-operative risk assessment "
                    "could not be generated. Complete the risk assessment clinically.")
        yield f"data: {json.dumps({'content': fallback})}\n\n"

    yield "data: [DONE]\n\n"

def _staff_display_names(staff_required) -> list[str]:
    """Best-effort display names from a free-form staff_required package.

    Accepts a list of {role, name} dicts, a list of plain strings, or a mixture,
    and never raises on a shape it has not seen — the prep alert must not fail
    because an upstream generator wrote a different JSON shape.
    """
    names: list[str] = []
    if not isinstance(staff_required, list):
        return names
    for s in staff_required:
        if isinstance(s, dict):
            label = s.get("name") or s.get("role") or s.get("id")
            if label:
                names.append(str(label))
        elif s:
            names.append(str(s))
    return names


def trigger_surgery_prep(surgery_id: str, sender_id: str, sender_name: str, sender_role: str) -> dict:
    from services.relay_service import send_message
    
    detail = get_surgery_detail(surgery_id)
    if not detail:
        raise ValueError("Surgery not found")

    # staff_required is free-form JSON written by the prep-package generator, and
    # different writers use different shapes: a list of {role, name} dicts
    # (surgery_escalation_service.FALLBACK_DATA) or a plain list of names. The
    # old `s["name"]` indexed straight into it, so a plain-string roster raised
    # TypeError and the whole Prep-for-Surgery action 500'd.
    team_names = ", ".join(_staff_display_names(detail.get("staff_required") or []))

    # 1. Identify target recipients explicitly — prefer the ACTUAL assigned team
    #    (surgery_team_assignments) so alerts go only to the relevant staff.
    target_users = []
    target_roles = []

    with db_session() as _db:
        assigned = _db.execute(
            "SELECT DISTINCT user_id FROM surgery_team_assignments WHERE surgery_id = ?",
            (surgery_id,)
        ).fetchall()
    for a in assigned:
        if a["user_id"]:
            target_users.append(a["user_id"])

    # Fall back to staff embedded in the surgery package if no explicit assignments.
    # Same shape tolerance as above: `"id" in staff` on a plain string is a
    # SUBSTRING test, so a roster entry like "Anesthesiologist" matched and then
    # `staff["id"]` raised.
    if not target_users and detail.get("staff_required"):
        for staff in detail["staff_required"]:
            if isinstance(staff, dict) and staff.get("id"):
                target_users.append(str(staff["id"]))

    # Role fallback is only used by get_user_alerts when BOTH role AND department
    # match — keep it narrow so this is not a blanket doctor/nurse broadcast.
    target_roles = ["doctor"] if not target_users else []
        
    # Structured alert payload
    alert_payload = {
        "is_structured": True,
        "type": "surgery_prep",
        "surgery_id": surgery_id,
        "patient_id": detail["patient_id"],
        "patient_name": detail["patient_name"],
        "mrn": detail["mrn"],
        "procedure_name": detail["procedure_name"],
        "operating_room": detail["operating_room"],
        "scheduled_time": detail["scheduled_time"],
        "estimated_duration_min": detail["estimated_duration_min"],
        "team_names": team_names,
        "risk_level": detail["risk_level"],
        "risk_score": detail["risk_score"],
        "target_users": target_users,
        "target_roles": target_roles,
        "target_departments": ["Surgery", "Anesthesiology"],
        "urgency": "critical",
        "title": "PREP FOR SURGERY ALERT",
        "action_required": "Navigate to Surgical Suite to review Handoff Context."
    }
    
    # Check for duplicate alerts within the last 4 hours
    with db_session() as db:
        recent_alerts = db.execute(
            """SELECT content FROM messages 
               WHERE message_type = 'alert' AND channel_id = 'dept-surgery'
               AND timestamp > datetime('now', '-4 hours')"""
        ).fetchall()
        for r in recent_alerts:
            try:
                payload = json.loads(r["content"])
                if payload.get("type") == "surgery_prep" and payload.get("surgery_id") == surgery_id:
                    # Alert already exists, don't create a new one
                    return {"status": "duplicate", "message": "Alert already active"}
            except Exception:
                continue
    
    # 1. Send to the Surgery department channel (for departmental visibility)
    dept_result = send_message(
        channel_id="dept-surgery", 
        sender_id=sender_id,
        sender_name=sender_name,
        sender_role=sender_role,
        content=json.dumps(alert_payload),
        message_type="alert"
    )
    
    # 2. Also send to the patient's case room (for patient-level visibility).
    #    Look up the real case-room channel id — it is not derivable from the
    #    patient id, so resolve it the same way surgery_routes does.
    with db_session() as db:
        case_channel = db.execute(
            "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room' LIMIT 1",
            (detail["patient_id"],)
        ).fetchone()
    if case_channel:
        try:
            send_message(
                channel_id=case_channel["id"],
                sender_id=sender_id,
                sender_name=sender_name,
                sender_role=sender_role,
                content=json.dumps(alert_payload),
                message_type="alert"
            )
        except Exception:
            pass  # Dept channel is the primary target
    
    return dept_result
