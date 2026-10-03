"""Orb Bridge — Patient Portal Routes

APPOINTMENT REQUESTS (fixed 2026-08-14, pilot dry run). This module used to
write a patient's follow-up request into ``patient_appointment_requests`` and
answer "The care team will confirm a time." Nothing has ever read that table —
no staff route, no service, no screen — so the sentence was a promise the
product could not keep, made to a patient. Requests now go through
``routes.appointments_routes.persist_appointment`` into ``appointments``, the
one appointment store staff surfaces read (forecast_service merges its booked
rows into the Appointments panel), tagged origin='patient' so an ASK is never
displayed as an agreed slot. Migration 28 carries the stranded rows across.
"""
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import StreamingResponse
from permissions import require_patient_self
from services.bridge_service import (
    answer_patient_question,
    get_patient_status_simple,
    patient_ask_context,
)
from services.audit_service import log_audit_event
from routes.appointments_routes import (
    ORIGIN_PATIENT,
    PATIENT_REQUEST_TYPE,
    _MAX_REASON_LEN,
    _serialize as _serialize_appointment,
    _validate_date_only,
    _validate_slot,
    default_slot_for,
    persist_appointment,
)
from database import db_session, run_db
from services.discharge_service import (
    PENDING_REVIEW_MESSAGE,
    REVIEW_DRAFT,
    REVIEW_REVIEWED,
    is_reviewed,
)
from services.vital_charting import as_charted
from services.lens_service import REVIEWED_IMAGING_SQL
from datetime import datetime
import json

router = APIRouter(prefix="/api/bridge", tags=["bridge"])

# The vitals a patient sees, and the chart precision each is printed at.
#
# NO VERDICT (ruled 2026-10-03). This table used to carry "normal" bands and the
# route called each reading low / normal / high against them. Those bands were
# not the table the ward scores with: SBP 90 is NEWS2 3 points (red on the Vigil
# bar) and the patient was told "In normal range"; HR 100, SBP 91, RR 20,
# Temp 37.0 and SpO2 94 on oxygen is NEWS2 6 and every one read "normal". A
# patient-facing verdict either disagrees with the ward or borrows NEWS2, which
# needs the patient's SpO2 scale and oxygen status and still misses risk that
# only shows in the aggregate. So the patient gets the number as charted, when
# it was measured and its trend, and the card says the care team reviews them.
_PORTAL_VITALS = {
    "heart_rate": {"unit": "bpm", "label": "Heart rate", "dp": 0},
    "systolic_bp": {"unit": "mmHg", "label": "Systolic BP", "dp": 0},
    "spo2": {"unit": "%", "label": "Oxygen (SpO₂)", "dp": 0},
    "temperature": {"unit": "°C", "label": "Temperature", "dp": 1},
    "respiratory_rate": {"unit": "/min", "label": "Respiratory rate", "dp": 0},
}


def _require_object_body(body) -> dict:
    """A client can send valid JSON that isn't an object (array/string/number/null);
    calling .get() on it would raise AttributeError → an ugly 500. Reject cleanly."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body


def _str_field(body: dict, key: str) -> str:
    """Return a stripped string for a body field, tolerating non-string JSON values
    (int/list/dict) without crashing — they normalize to empty and are then validated."""
    v = body.get(key)
    return v.strip() if isinstance(v, str) else ""


def _patient_appointments(patient_id: str) -> list:
    """The patient's own appointments, newest request first within a date.

    Reads the SAME rows staff read (see the module note on
    patient_appointment_requests): whatever the ward does to the row — moves the
    time, cancels it, marks it attended — is what the patient sees here.

    Blocking; call via run_db from async.
    """
    with db_session() as db:
        rows = db.execute(
            """SELECT * FROM appointments WHERE patient_id = ?
               ORDER BY scheduled_date ASC, created_at DESC""",
            (patient_id,),
        ).fetchall()
    return [_patient_view(r) for r in rows]


def _patient_view(row) -> dict:
    """One appointment in the patient's words.

    Field names are the ones the portal already reads (requested_date /
    requested_time / department / reason / status). ``status_note`` is the
    honest sentence for that row: a patient REQUEST is on the list but agreed
    with nobody, and this says so for as long as that is true.
    """
    appt = _serialize_appointment(row)
    status = appt["status"]
    patient_asked = appt["origin"] == ORIGIN_PATIENT
    if status == "cancelled":
        note = "Cancelled."
    elif status == "completed":
        note = "Completed."
    elif patient_asked:
        note = (
            f"On the care team's appointment list for {appt['scheduled_date']} at "
            f"{appt['scheduled_time']}. You asked for this: nobody has agreed the time "
            "with you yet, and the team may move or cancel it. This page shows the "
            "current time."
        )
    else:
        note = (
            f"Scheduled by the care team for {appt['scheduled_date']} at "
            f"{appt['scheduled_time']}."
        )
    return {
        "id": appt["id"],
        "requested_date": appt["scheduled_date"],
        "requested_time": appt["scheduled_time"],
        "department": appt["department"],
        "reason": appt["reason"],
        "status": status,
        "created_at": appt["created_at"],
        "requested_by_patient": patient_asked,
        "status_note": note,
    }


def _cancel_own_appointment(appointment_id: str, patient_id: str) -> None:
    """Cancel one of the patient's own appointments. Blocking; call via run_db.

    Scoped by patient_id, and a row belonging to someone else is a 404, never a
    403 — a 403 would confirm the id exists.
    """
    with db_session() as db:
        cur = db.execute(
            """UPDATE appointments SET status = 'cancelled'
               WHERE id = ? AND patient_id = ? AND status = 'booked'""",
            (appointment_id, patient_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Appointment not found")


def _resolve_patient_id(user_id: str) -> str:
    """Resolve the patient record ID from the user's login ID.

    Enforces ownership: a patient can only resolve their OWN patient_id.

    Stays SYNC: the endpoints below are plain `def`, so FastAPI already runs them
    in its own worker thread. Async callers must go through run_db().
    """
    with db_session() as db:
        row = db.execute(
            "SELECT id FROM patients WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No patient record found for your account")
    return row["id"]


@router.get("/status")
def patient_status(user: dict = Depends(require_patient_self())):
    """Get current patient status in simple language.
    
    Route-level ownership: only the patient themselves can access this.
    Service layer additionally scopes data by user.id.
    """
    status = get_patient_status_simple(user["id"])
    return status


@router.get("/vitals")
def patient_vitals(user: dict = Depends(require_patient_self())):
    """Get simplified vitals for patient view.
    
    Route-level ownership: only the patient themselves can access this.
    """
    status = get_patient_status_simple(user["id"])
    return {"vitals": status.get("vitals", {})}


@router.post("/ask")
async def patient_ask(request: Request, user: dict = Depends(require_patient_self())):
    """Patient asks a question — responds in simple language.
    
    Route-level ownership: only the patient themselves can access this.
    """
    body = _require_object_body(await request.json())
    question = body.get("question", "")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(status_code=400, detail="Question is required")

    # Off the loop: three encrypted lookups (patient, risk, vitals) that used to
    # run inline here, delaying the first SSE byte AND every other request.
    # patient_ask_context, not get_patient_status_simple: a red flag needs the
    # patient's id to reach their case room (it was None until 2026-10-03).
    context = await run_db(patient_ask_context, user["id"])

    async def stream_response():
        async for chunk in answer_patient_question(question, context):
            yield f"data: {json.dumps({'content': chunk})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ─── Self-Scoped Reports ─────────────────────────────────────
# All endpoints below resolve patient_id from user_id (ownership enforced).


@router.get("/reports")
def patient_reports(user: dict = Depends(require_patient_self())):
    """Get all reports for the logged-in patient — aggregated view.
    
    Returns medications, clinical documents, lab results, discharge summary,
    and diagnostic imaging reports, all scoped to this patient only. Every
    document carries ``type_label``, its type in the patient's words.
    """
    patient_id = _resolve_patient_id(user["id"])

    with db_session() as db:
        # Active medications
        meds = db.execute(
            """SELECT drug_name, dosage, route, frequency, status, started_at
               FROM medications WHERE patient_id = ?
               ORDER BY started_at DESC""",
            (patient_id,),
        ).fetchall()

        # Clinical documents (notes, ward rounds, etc.), lab results apart. The
        # portal built Lab History from imaging alone, so a patient whose
        # results were charted was told there were none, while Report History
        # listed the same results under the internal type "lab_result".
        docs = db.execute(
            """SELECT doc_type, title, content, created_at, source
               FROM clinical_documents
               WHERE patient_id = ? AND COALESCE(doc_type, '') != 'lab_result'
               ORDER BY created_at DESC, id DESC LIMIT 20""",
            (patient_id,),
        ).fetchall()

        # `source` is 'simulated' for a simulator's result (migration 46); the
        # portal labels it so a fabricated value never reads as the patient's.
        labs = db.execute(
            """SELECT doc_type, title, content, created_at, source
               FROM clinical_documents
               WHERE patient_id = ? AND doc_type = 'lab_result'
               ORDER BY created_at DESC, id DESC""",
            (patient_id,),
        ).fetchall()

        # Discharge summary (if discharged)
        discharge = db.execute(
            """SELECT * FROM discharge_summaries WHERE patient_id = ?
               ORDER BY discharged_at DESC, id DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()

        # Diagnostic imaging reports: clinician-reviewed reads only, and the
        # text the clinician confirmed. `analysis` is the model's raw read and
        # never reaches the patient; an unreviewed row is not shown at all.
        # The lab bot's 'lab' copies are excluded by the same filter (they are
        # lab reports, already in Lab History, not images).
        imaging = db.execute(
            f"""SELECT image_type, clinical_context, reviewed_text AS analysis,
                       reviewed_at, created_at
               FROM lens_analyses WHERE patient_id = ? AND {REVIEWED_IMAGING_SQL}
               ORDER BY created_at DESC""",
            (patient_id,),
        ).fetchall()

    return {
        "patient_id": patient_id,
        "medications": [dict(m) for m in meds],
        "clinical_documents": [_patient_document(d) for d in docs],
        "labs": [_patient_document(r) for r in labs],
        "discharge_summary": _patient_discharge_view(discharge),
        "imaging_reports": [dict(i) for i in imaging],
    }


# What a document type is called on the patient's screen. The column holds
# internal spellings ("lab_result", "note", "Ward Round") and the portal
# printed them as stored.
_DOC_TYPE_LABELS = {
    "lab result": "Lab result",
    "note": "Clinical note",
    "progress note": "Progress note",
    "ward round": "Ward round note",
    "imported document": "Imported record",
    "discharge summary": "Discharge summary",
}


def _document_label(doc_type) -> str:
    """A document type in plain words: a known type by name, any other with
    its underscores spaced out and sentence case, a blank one as a clinical note."""
    words = " ".join(str(doc_type or "").replace("_", " ").split())
    if not words:
        return "Clinical note"
    return _DOC_TYPE_LABELS.get(words.casefold(), words[:1].upper() + words[1:].lower())


def _patient_document(row) -> dict:
    doc = dict(row)
    doc["type_label"] = _document_label(doc.get("doc_type"))
    return doc


def _patient_discharge_view(row) -> dict | None:
    """A discharge summary as the patient may see it.

    The text is the drafting model's until a clinician marks it reviewed, and
    an unreviewed draft does not reach the patient: they are told it is being
    finalised instead. Only the summary, its date and its status leave.
    """
    if row is None:
        return None
    reviewed = is_reviewed(row)
    return {
        "summary": row["summary"] if reviewed else None,
        "status": REVIEW_REVIEWED if reviewed else REVIEW_DRAFT,
        "message": None if reviewed else PENDING_REVIEW_MESSAGE,
        "discharged_at": row["discharged_at"],
    }


@router.get("/medications")
def patient_medications(user: dict = Depends(require_patient_self())):
    """Get medications for the logged-in patient only."""
    patient_id = _resolve_patient_id(user["id"])

    with db_session() as db:
        meds = db.execute(
            """SELECT drug_name, dosage, route, frequency, status, started_at
               FROM medications WHERE patient_id = ?
               ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'prn' THEN 1 ELSE 2 END,
                        started_at DESC""",
            (patient_id,),
        ).fetchall()

    return {"medications": [dict(m) for m in meds]}


@router.get("/discharge")
def patient_discharge(user: dict = Depends(require_patient_self())):
    """Get discharge summary for the logged-in patient only."""
    patient_id = _resolve_patient_id(user["id"])

    with db_session() as db:
        summary = db.execute(
            """SELECT * FROM discharge_summaries WHERE patient_id = ?
               ORDER BY discharged_at DESC, id DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()

    if not summary:
        return {"discharge_summary": None, "message": "No discharge summary available yet."}

    timeline = None
    if summary["timeline_json"]:
        try:
            timeline = json.loads(summary["timeline_json"])
        except json.JSONDecodeError:
            timeline = None

    view = _patient_discharge_view(summary)
    return {
        # None until reviewed; the message says why (see _patient_discharge_view).
        "discharge_summary": view["summary"],
        "status": view["status"],
        "message": view["message"],
        "timeline": timeline,
        "discharged_at": summary["discharged_at"],
    }


@router.get("/documents")
def patient_documents(user: dict = Depends(require_patient_self())):
    """Get clinical documents for the logged-in patient only."""
    patient_id = _resolve_patient_id(user["id"])

    with db_session() as db:
        docs = db.execute(
            """SELECT doc_type, title, content, created_at
               FROM clinical_documents WHERE patient_id = ?
               ORDER BY created_at DESC LIMIT 30""",
            (patient_id,),
        ).fetchall()

    return {"documents": [dict(d) for d in docs]}


# ─── Care Team ───────────────────────────────────────────────


@router.get("/care-team")
def patient_care_team(user: dict = Depends(require_patient_self())):
    """Who is looking after me: attending physician, care unit, and the ward's
    nursing team.

    Self-scoped: resolves the patient's own record only.
    """
    patient_id = _resolve_patient_id(user["id"])

    with db_session() as db:
        row = db.execute(
            """SELECT p.ward, p.bed, p.attending_doctor_id,
                      a.department AS admission_department
               FROM patients p
               LEFT JOIN admissions a ON a.patient_id = p.id AND a.status = 'active'
               WHERE p.id = ?""",
            (patient_id,),
        ).fetchone()

        attending = None
        if row and row["attending_doctor_id"]:
            doc = db.execute(
                "SELECT name, role, department FROM users WHERE id = ?",
                (row["attending_doctor_id"],),
            ).fetchone()
            if doc:
                attending = {"name": doc["name"], "role": "Attending Physician", "department": doc["department"]}

        department = (row["admission_department"] if row else None) or (attending["department"] if attending else None)

    # The nursing team, never a named nurse. Orb records no nurse-to-patient
    # assignment, and this used to name the first nurse in the department (else
    # the first nurse in the hospital), so a patient was told a stranger was
    # looking after them. A nurse is named here only once a real assignment
    # exists to name her from.
    nursing = None
    if row and (row["ward"] or department):
        nursing = {
            "name": "Nursing team",
            "role": "Nurse",
            "department": department,
            "ward": row["ward"],
            "is_team": True,
        }

    team = [m for m in (attending, nursing) if m]
    return {
        "care_team": team,
        "unit": {
            "department": department,
            "ward": row["ward"] if row else None,
            "bed": row["bed"] if row else None,
        },
    }


# ─── Vitals Trends (for patient-facing sparklines) ───────────


@router.get("/vitals-trends")
def patient_vitals_trends(user: dict = Depends(require_patient_self())):
    """Recent vitals as short time-series: the number as charted, when it was
    measured and its trend. No status and no normal range (see _PORTAL_VITALS)."""
    patient_id = _resolve_patient_id(user["id"])

    with db_session() as db:
        rows = db.execute(
            """SELECT timestamp, heart_rate, systolic_bp, spo2, temperature, respiratory_rate
               FROM vitals WHERE patient_id = ?
               -- id DESC: on a timestamp tie the later write is the correction,
               -- and after the reversal below it is the one that ends the series.
               ORDER BY timestamp DESC, id DESC LIMIT 48""",
            (patient_id,),
        ).fetchall()

    rows = list(reversed([dict(r) for r in rows]))  # oldest → newest for charting

    trends = {}
    for key, meta in _PORTAL_VITALS.items():
        # Charted precision (services/vital_charting): whole numbers, tenths for
        # temperature, half-up. The same number the ward prints and NEWS2
        # scores; the raw float went out as "73.9 bpm".
        readings = [(r["timestamp"], as_charted(r[key], meta["dp"]))
                    for r in rows if r.get(key) is not None]
        series = [v for _, v in readings]
        current = series[-1] if series else None
        # Timestamp of the row that supplied `current` — vitals rows can be
        # sparse, so this is per-vital, not the newest row overall.
        measured_at = readings[-1][0] if readings else None
        trends[key] = {
            "label": meta["label"],
            "unit": meta["unit"],
            "current": current,
            "measured_at": measured_at,
            "series": series[-24:],  # cap for a compact sparkline
        }

    return {"trends": trends, "count": len(rows)}


# ─── Appointment Requests (written where staff read) ─────────


@router.get("/appointments")
async def patient_appointments(user: dict = Depends(require_patient_self())):
    """List the patient's own appointments — requests and staff-booked alike."""
    patient_id = await run_db(_resolve_patient_id, user["id"])
    appointments = await run_db(_patient_appointments, patient_id)
    return {"appointments": appointments}


@router.post("/appointments")
async def request_appointment(request: Request, user: dict = Depends(require_patient_self())):
    """Ask for a follow-up. The request lands on the staff appointment list.

    Written through routes.appointments_routes.persist_appointment with
    origin='patient', so the row appears in the Appointments panel and in the
    clinician list for this patient. The response says exactly that and nothing
    more: it does NOT promise anyone will call back, because nothing in the
    system does that.
    """
    # Resolved off the loop, and BEFORE the body is read, so the ownership 404
    # still precedes the validation 400s exactly as it did.
    patient_id = await run_db(_resolve_patient_id, user["id"])
    body = _require_object_body(await request.json())

    # Validate/normalize input — a patient-facing write, so don't trust the client.
    # _str_field tolerates non-string JSON values instead of crashing on .strip().
    # requested_date must be a real calendar date (not free text / HTML / SQL / int).
    requested_date = _str_field(body, "requested_date")
    try:
        datetime.strptime(requested_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="requested_date must be a valid date (YYYY-MM-DD)")

    requested_time = _str_field(body, "requested_time") or None
    if requested_time is not None:
        try:
            datetime.strptime(requested_time, "%H:%M")
        except ValueError:
            raise HTTPException(status_code=400, detail="requested_time must be HH:MM")

    # Same slot rules as a staff booking (past date, earlier today, horizon) —
    # one table, one set of rules, so a patient cannot park a row in a year no
    # clinic list will ever show. 422s from the shared validator are re-raised
    # as 400s: this route has always answered 400 and the portal reads `detail`.
    try:
        if requested_time:
            _validate_slot(requested_date, requested_time)
            slot = requested_time
            unspecified_time = False
        else:
            parsed = _validate_date_only(requested_date)
            slot = default_slot_for(parsed)
            unspecified_time = True
    except HTTPException as exc:
        raise HTTPException(status_code=400, detail=exc.detail)
    if not slot:
        raise HTTPException(
            status_code=400,
            detail="It is too late in the day to add a request for today. "
                   "Please choose a date from tomorrow onwards, or call the ward.",
        )

    # Cap free-text so a client can't store multi-KB blobs.
    department = (_str_field(body, "department") or None)
    if department is not None:
        department = department[:80]
    reason = (_str_field(body, "reason") or None)
    if reason is not None:
        reason = reason[:1000]

    # The staff-facing reason carries the same facts the patient is given: that
    # the patient asked for this, what for, and — when they named no time — that
    # the slot on screen is a placement, not a choice anyone made.
    staff_reason = ["Patient-requested follow-up."]
    if reason:
        staff_reason.append(reason)
    if unspecified_time:
        staff_reason.append("Patient did not choose a time; listed at the first clinic slot.")

    appointment, created = await run_db(
        persist_appointment,
        patient_id=patient_id,
        booked_by_user_id=user["id"],
        scheduled_date=requested_date,
        scheduled_time=slot,
        appointment_type=PATIENT_REQUEST_TYPE,
        reason=" ".join(staff_reason)[:_MAX_REASON_LEN],
        department=department,
        origin=ORIGIN_PATIENT,
    )

    if created:
        # Same audit shape as a staff booking (routes/appointments_routes) — the
        # row is a real clinical record either way, and the actor is the patient.
        await run_db(
            log_audit_event,
            correlation_id=f"appointment-book-{appointment['id']}",
            event_category="clinical_record",
            action_type="appointment_booked",
            outcome="success",
            summary_message=(
                f"Patient-requested follow-up for {patient_id} on "
                f"{requested_date} {slot}"
            ),
            actor_user_id=user["id"],
            actor_role=user.get("role", "patient"),
            patient_id=patient_id,
            target_type="appointment",
            target_id=appointment["id"],
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            metadata={"appointment_type": PATIENT_REQUEST_TYPE, "origin": ORIGIN_PATIENT},
        )

    # WHAT THIS SENTENCE MAY CLAIM. Only what the system does: the row is on the
    # list staff work from, it can be seen and cancelled here, and the time is
    # not agreed. No callback is promised, because nothing places one.
    #
    # It also describes the row that ACTUALLY EXISTS, not the one that was
    # attempted. A repeat tap — or an ask for a slot the ward had already put in
    # the diary — returns the existing appointment, and calling a team-booked
    # slot "not agreed with you" would be the same class of untruth in reverse.
    date_str, slot_str = appointment["scheduled_date"], appointment["scheduled_time"]
    patient_asked = appointment["origin"] == ORIGIN_PATIENT
    unagreed = (
        "Nobody has agreed that time with you yet. The team may move or cancel "
        "it, and this page always shows where it stands."
    )
    if not patient_asked:
        message = (
            f"The care team already has an appointment for you on {date_str} at "
            f"{slot_str}, so nothing new was added."
        )
    elif not created:
        message = (
            f"You have already asked for {date_str} at {slot_str}, so nothing new "
            f"was added. {unagreed}"
        )
    elif unspecified_time:
        message = (
            f"Your request is on the care team's appointment list for {date_str}. "
            f"You did not choose a time, so it is listed at {slot_str} until the team "
            f"changes it. {unagreed}"
        )
    else:
        message = (
            f"Your request is on the care team's appointment list for {date_str} "
            f"at {slot_str}. {unagreed}"
        )

    return {
        "id": appointment["id"],
        "status": appointment["status"],
        "requested_date": date_str,
        "requested_time": slot_str,
        "department": appointment["department"],
        "requested_by_patient": patient_asked,
        "time_chosen_by_patient": not unspecified_time,
        "message": message,
    }


@router.delete("/appointments/{appointment_id}")
def cancel_appointment(appointment_id: str, user: dict = Depends(require_patient_self())):
    """Cancel one of the patient's own appointments (ownership enforced).

    The cancellation lands on the row staff read, so the slot leaves the ward's
    list too. A 'completed' visit cannot be cancelled — that is a clinical fact
    that already happened (same rule as DELETE /api/appointments/{id}) — and,
    like an id belonging to someone else, it answers 404.
    """
    patient_id = _resolve_patient_id(user["id"])
    _cancel_own_appointment(appointment_id, patient_id)
    return {"id": appointment_id, "status": "cancelled"}

