"""Orb Vigil — Patient Monitoring Routes"""
import json
import asyncio
import sqlite3
from db_backend import DBIntegrityError
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from auth import get_current_user
from permissions import (
    PATIENT_DEPARTMENT_SQL,
    department_scope,
    enforce_patient_scope,
    log_phi_read,
    require_patient_access,
    require_permission,
)
from database import get_db, db_session, run_db
from datetime import datetime, timedelta, timezone
from services.clinical_time import format_utc
from services.vigil_service import (
    SCORING_OFF_REASON, get_vitals_trend, risk_for_row, scored_admitted_patients,
    scoring_enabled,
)
from services.medication_safety_gate import enforce_medication_safety
from services.medorders import chart_lane
from services.medorders import core as medorder_core
from services.vitals_source.base import LABEL_SIMULATED
from services.lens_service import decorate_analysis
from services.stay_times import summary_less_ends_sql

# The expected course is drafted by a model: 404 while the models are off.
from services.model_switch import require_models_on
from services.drug_chart import drug_chart_enabled, require_drug_chart

router = APIRouter(prefix="/api/vigil", tags=["vigil"])


# ── Blocking DB bodies ────────────────────────────────────────────────────────
# Every synchronous SQLite body reached from an `async def` lives out here, not
# nested inside the route. Two reasons: it keeps "no DB call inside an async def"
# mechanically checkable (the shape that stalled the API on 2026-07-30), and it
# forces each body to be a single, self-contained unit of work — a whole
# read-then-write stays inside ONE session in ONE executor hop, so introducing
# the await cannot open a window between the check and the write.
#
# These must stay PURE SYNC (no asyncio) and are only ever called via run_db().

# Lab results the chart route returns: the newest per analyte, then an overall
# cap on the payload (see _load_patient_bundle).
_CHART_LAB_RESULTS_PER_ANALYTE = 50
_CHART_LAB_RESULTS_MAX = 1000


def _load_patient_bundle(patient_id: str):
    with db_session() as db:
        patient = db.execute(
            """SELECT p.*, u.name as attending_doctor_name
               FROM patients p
               LEFT JOIN users u ON u.id = p.attending_doctor_id
               WHERE p.id = ?""",
            (patient_id,)
        ).fetchone()
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        # The newest 288 vitals, of any age (no time window: vitals[0] is the
        # newest set however old it is). vitals[0] is the set the command bar
        # prints and the one get_patient scores, so it must be the newest.
        # id DESC breaks a timestamp tie: on a tie SQLite walks
        # idx_vitals_patient in rowid ASC, so a corrected set re-charted at the
        # same bedside time came back SECOND and the superseded set was always
        # vitals[0]. vitals.id is INTEGER PRIMARY KEY AUTOINCREMENT, which is
        # insertion order; timestamp stays the primary sort key.
        vitals = db.execute(
            """SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL
               ORDER BY timestamp DESC, id DESC LIMIT 288""",
            (patient_id,),
        ).fetchall()

        # Clinical notes
        notes = db.execute(
            """SELECT * FROM clinical_documents WHERE patient_id = ?
               ORDER BY created_at DESC""",
            (patient_id,),
        ).fetchall()

        # Scribe notes
        scribe_notes = db.execute(
            """SELECT * FROM scribe_notes WHERE patient_id = ?
               ORDER BY created_at DESC""",
            (patient_id,),
        ).fetchall()

        # Risk score history
        risk_history = db.execute(
            """SELECT score, level, drivers, timestamp FROM risk_scores
               WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 50""",
            (patient_id,),
        ).fetchall()

        # Lens analyses
        lens_analyses = db.execute(
            """SELECT * FROM lens_analyses WHERE patient_id = ?
               ORDER BY created_at DESC""",
            (patient_id,),
        ).fetchall()

        # Medications
        medications = db.execute(
            """SELECT * FROM medications WHERE patient_id = ?
               ORDER BY status ASC, started_at DESC""",
            (patient_id,),
        ).fetchall()

        # Structured lab results: interfaced (HL7/JSON OBX) and imported
        # history both land in `observations`. The chart's lab card used to
        # read only lab_result documents and, finding none, drew values it
        # estimated from the diagnosis. Withdrawn or erroneous results are left
        # out; numeric values only, because the card plots them.
        #
        # The window keeps the NEWEST results. An oldest-first LIMIT kept a
        # chronic patient's imported history and cut the whole admission, so
        # the card showed a years-old value as each analyte's latest. Each
        # analyte (grouped as buildLabTrends groups it: name and unit) keeps
        # its newest _CHART_LAB_RESULTS_PER_ANALYTE, and the overall cap is cut
        # by recency rank, so every analyte keeps its latest result before any
        # analyte keeps a longer trend. Ranked by julianday() because
        # interfaced rows store observed_at as received ('T' or space).
        # Returned oldest first: the card takes the range from the newest row.
        # The SQL, the void and superseded filters and the ordering live in
        # services/lab_observations.py, the one reader every lab lane shares.
        # Simulated rows stay in, with their source, so the card labels them.
        from services import lab_observations as _lab_obs
        lab_observations = _lab_obs.chart_lab_rows(
            db, patient_id,
            per_analyte=_CHART_LAB_RESULTS_PER_ANALYTE,
            max_rows=_CHART_LAB_RESULTS_MAX,
        )

        # Recorded problems and comorbidities, for the chart header and the
        # printed record. `patients` has no comorbidities column, so the chart
        # header's comorbidity tags never had anything to show.
        profile = db.execute(
            "SELECT comorbidities FROM patient_profiles WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()
        problems = db.execute(
            """SELECT problem, icd10_code, onset_date, source FROM problem_list
               WHERE patient_id = ? AND status = 'active'
               ORDER BY recorded_at ASC, id ASC""",
            (patient_id,),
        ).fetchall()
        admission = db.execute(
            """SELECT id FROM admissions WHERE patient_id = ?
               ORDER BY (status = 'active') DESC, created_at DESC, rowid DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
        return (patient, vitals, notes, scribe_notes, risk_history,
                lens_analyses, medications, lab_observations,
                profile, problems, admission)


def _load_latest_risk(patient_id: str):
    with db_session() as db:
        risk = db.execute(
            """SELECT level FROM risk_scores WHERE patient_id = ?
               ORDER BY timestamp DESC, id DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
        return risk


def _load_recent_vitals(patient_id: str, hours: int):
    # The cutoff is bound in the stored spelling (services/clinical_time.py).
    # `datetime('now', ?)` yields "YYYY-MM-DD HH:MM:SS", and every 'T' row of
    # that UTC date compared above it, so a 1-hour window returned a reading
    # charted 7 hours earlier.
    cutoff = format_utc(datetime.now(timezone.utc) - timedelta(hours=hours))
    db = get_db()
    try:
        return db.execute(
            """SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL
               AND timestamp > ?
               ORDER BY timestamp ASC, id ASC""",
            (patient_id, cutoff),
        ).fetchall()
    finally:
        db.close()


def _load_patient_notes(patient_id: str):
    db = get_db()
    try:
        return db.execute(
            """SELECT * FROM clinical_documents WHERE patient_id = ?
               ORDER BY created_at DESC""",
            (patient_id,),
        ).fetchall()
    finally:
        db.close()


def _insert_patient_note(patient_id: str, data: dict, author_id: str,
                         idempotency_key: str | None) -> int:
    """Idempotency lookup + INSERT in one hop. The pre-check and the INSERT must
    not be separated by an await — a replay landing in the gap would insert a
    duplicate note. (The DBIntegrityError branch still covers the genuinely
    concurrent case, which the UNIQUE index decides, not us.)"""
    db = get_db()
    try:
        if idempotency_key:
            existing = db.execute(
                "SELECT id FROM clinical_documents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return existing["id"]
        try:
            cursor = db.execute(
                """INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (patient_id, data.get("doc_type", "Progress Note"), data.get("title", "Manual Entry"), data.get("content"), author_id, idempotency_key),
            )
            db.commit()
            return cursor.lastrowid
        except DBIntegrityError:
            # Concurrent replay raced past the pre-INSERT check — return the
            # existing row for this key rather than a duplicate.
            existing = db.execute(
                "SELECT id FROM clinical_documents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone() if idempotency_key else None
            if not existing:
                raise
            return existing["id"]
    finally:
        db.close()


def _patient_exists(patient_id: str):
    db = get_db()
    try:
        return db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone()
    finally:
        db.close()


def _load_patient_row(patient_id: str):
    db = get_db()
    try:
        return db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    finally:
        db.close()


def _load_qr_path(patient_id: str):
    db = get_db()
    try:
        return db.execute(
            "SELECT qr_code_path FROM discharge_summaries WHERE patient_id = ? ORDER BY discharged_at DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
    finally:
        db.close()


def _insert_medication(patient_id: str, drug_name: str, dosage: str, route_: str,
                       frequency: str, status: str, prescriber_id: str,
                       idempotency_key: str | None) -> int:
    """Same one-hop rule as _insert_patient_note: the idempotency pre-check and
    the INSERT stay atomic w.r.t. the event loop, so an offline replay can never
    slip between them and duplicate a drug order."""
    db = get_db()
    try:
        if idempotency_key:
            existing = db.execute(
                "SELECT id FROM medications WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return existing["id"]
        try:
            cursor = db.execute(
                """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, prescribed_by, source, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'clinician', ?)""",
                (patient_id, drug_name, dosage, route_, frequency, status, prescriber_id, idempotency_key),
            )
            db.commit()
            return cursor.lastrowid
        except DBIntegrityError:
            # Concurrent replay raced past the pre-INSERT check — return the
            # existing row for this key rather than a duplicate.
            existing = db.execute(
                "SELECT id FROM medications WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone() if idempotency_key else None
            if not existing:
                raise
            return existing["id"]
    finally:
        db.close()


# REMOVED 2026-08-15: `GET /test-stream`. A debug leftover that streamed a
# hardcoded sentence about a named seeded patient ("Suresh Pillai is a
# 55-year-old male with acute chest pain") as if it were clinical content. It
# had no caller in the frontend, and fabricated prose about a real patient name
# is not something a pilot appliance should be able to serve at all.


def _active_admission_ids(patient_ids: list) -> dict:
    """patient id -> id of their active admission (the newest, if several)."""
    marks = ",".join("?" * len(patient_ids))
    with db_session() as db:
        found = db.execute(
            f"""SELECT patient_id, id FROM admissions
                WHERE status = 'active' AND patient_id IN ({marks})
                ORDER BY created_at ASC, rowid ASC""",
            tuple(patient_ids),
        ).fetchall()
    # Ascending, so the newest active admission is written last and wins.
    return {r["patient_id"]: r["id"] for r in found}


@router.get("/patients")
# Plain def on purpose (no awaits): FastAPI runs sync routes in its threadpool,
# keeping SQLCipher page I/O and the audit write off the asyncio event loop.
def list_patients(
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_permission("patient.read_all")),
):
    """Get admitted patients filtered by the user's department, each with the
    NEWS2 of their latest vitals row (`news2`, null when nothing is recorded).

    Paginated (limit/offset, mirroring audit_routes). The response BODY stays an
    ARRAY exactly as before so existing callers keep working; the full match count
    is surfaced in the `X-Total-Count` header so the board can render "Load more".

    READ-AUDITED, like its /discharged sibling and the nurse shift board. This is
    the product's main clinical screen and it lists name, MRN, bed, diagnosis and
    risk for the whole ward — a bulk PHI disclosure that until now left no trace
    at all, because the audit middleware fires on writes only. The hospital could
    answer "who opened this chart" and not "who read the board that lists the
    same patients", which is the same question with a wider answer.
    """
    # ONE scoping derivation for the whole system — see permissions.department_scope.
    # The nurse shift board calls the same function; it used to have its own
    # copy of this line plus client-supplied overrides, which is how a nurse
    # scoped to five patients here could read thirty over there.
    dept = department_scope(user)
    # The row is scored from ONE moment: each patient's latest vitals row, with
    # id DESC breaking a timestamp tie (a same-time correction is the higher
    # id). It used to come off risk_scores through a MAX(timestamp) equality
    # join, which listed a patient twice on a tie, and which went stale because
    # the simulator and the Scribe lane write vitals but no risk row, so every
    # poll put back an older score than the chart showed.
    #
    # Same entry point, scale and overlay as the chart (get_patient) and the
    # nurse board, so the three cannot disagree about who is critical. The Sage
    # ward brief above this board reads the same helper, so it cannot either.
    # The WHOLE scope is sorted (vigil_service.board_order, VigilPanel
    # boardOrder is its twin) and then paged, not paged in SQL, because the
    # sort key is the score: page one is the top of the board and the order
    # never contradicts the numbers shown.
    scored = scored_admitted_patients(dept)

    total = len(scored)
    response.headers["X-Total-Count"] = str(total)
    rows = [
        {
            "id": p["id"],
            "name": p["name"],
            "mrn": p["mrn"],
            "bed": p["bed"],
            "ward": p["ward"],
            "department": p["admission_department"],
            "admission_date": p["admission_date"],
            "diagnosis": p["diagnosis"],
            "age": p["age"],
            "sex": p["sex"],
            # null when there is no observation: "not recorded", never a zero.
            "news2": news2,
            "news2_at": vitals["timestamp"] if vitals else None,
            # Kept for the round queue and patient picker. The same moment as
            # `news2`, never a second source. `risk_score` is the 0-1 index and
            # is not a probability; no surface may print it as a percentage.
            "risk_score": news2["score"] if news2 else None,
            "risk_level": news2["level"] if news2 else None,
            "risk_drivers": list(news2["drivers"]) if news2 else [],
            "attending_doctor_name": p["attending_doctor_name"],
        }
        for p, vitals, news2 in scored[offset:offset + limit]
    ]
    # The ACTIVE admission each row is on, so a charge or bill raised from a
    # picker lands on the stay (encounter 'ip'). Without it the billing desk
    # posted every inpatient charge as outpatient.
    if rows:
        admission_ids = _active_admission_ids([r["id"] for r in rows])
        for r in rows:
            r["admission_id"] = admission_ids.get(r["id"])

    # Audited only when PHI actually left, matching /api/search/patients. This
    # board is polled every 30s by every open client (src/pages/panels/
    # VigilPanel.tsx), so a page past the end of the ward — which discloses
    # nothing — must not file a disclosure row. `phi_access` is fail-closed, so
    # a row that cannot be persisted means no board is returned.
    if rows:
        log_phi_read(
            request=request,
            user=user,
            action_type="patient_board_viewed",
            summary_message=(
                f"Patient board viewed ({len(rows)} of {total}) for "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={
                "result_count": len(rows),
                "total_in_scope": total,
                "limit": limit,
                "offset": offset,
                "patient_ids": [r["id"] for r in rows],
            },
        )

    return rows


@router.get("/discharged")
# Plain def on purpose (no awaits): FastAPI threadpools sync routes, so the
# db_session and the audit write stay off the event loop without a run_db hop.
def list_discharged(
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_permission("patient.read_all")),
):
    """Discharged patients from the caller's department, paginated.

    This route sat ten lines below list_patients — which does scope — and did
    not. It selected `p.*`, so one unpaginated request returned date_of_birth,
    phone, address, emergency_contact_name and emergency_contact_phone plus the
    free-text discharge summary for every patient the hospital had EVER
    discharged, in every department (audit 2026-08-05).

    Three separate changes, because scoping alone would not have been enough:
      1. department scope from the authenticated user, derived from the
         patient's most recent admission (`patients` has no department column);
      2. an explicit column projection — next-of-kin, home address, phone and
         DOB are off the wire regardless of who is asking, because no discharged
         LIST view needs them;
      3. LIMIT/OFFSET with X-Total-Count, matching list_patients, so the whole
         historical set cannot leave in a single response.
    """
    dept = department_scope(user)
    # Most-recent-admission department: a discharged patient has no ACTIVE
    # admission, so the active-only join used by list_patients would return
    # nothing here. permissions.PATIENT_DEPARTMENT_SQL prefers an active row and
    # falls back to the newest — the same derivation the Sage lane uses.
    dept_clause = f"AND {PATIENT_DEPARTMENT_SQL} = ?" if dept else ""
    dept_param = (dept,) if dept else ()

    with db_session() as db:
        total = db.execute(
            f"""SELECT COUNT(*) as total FROM patients p
                WHERE p.status = 'discharged' {dept_clause}""",
            dept_param,
        ).fetchone()["total"]
        # admission_id (the latest stay) and final_bill_raised let the billing
        # desk raise a discharged patient's final bill against that stay; the
        # picker dropped every patient the moment they were discharged.
        # has_pending_charges keeps them there while anything is still unbilled
        # (an outpatient charge the stay's final bill does not sweep), so a
        # final bill cannot strand a leftover charge out of the desk's reach.
        # The time a stay ended with no summary (the beds and admissions
        # module records some ends that way, adt-8c) comes from
        # services/stay_times.py, in the same UTC shape a summary's time is
        # stored in, so the list sorts as one column. With ORB_ADT off there
        # is no such end and the list is exactly what it was.
        ended_without_summary = (
            f"(SELECT datetime(e.ended_at) FROM ({summary_less_ends_sql()}) e "
            "WHERE e.patient_id = p.id ORDER BY julianday(e.ended_at) DESC LIMIT 1)"
        )
        patients = db.execute(
            f"""SELECT p.id, p.name, p.mrn, p.bed, p.ward, p.diagnosis,
                       p.age, p.sex, p.admission_date,
                       COALESCE(ds.discharged_at, {ended_without_summary}) AS discharged_at,
                       ds.summary,
                       (SELECT a.id FROM admissions a WHERE a.patient_id = p.id
                         ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1) AS admission_id,
                       EXISTS (SELECT 1 FROM invoices i
                                WHERE i.patient_id = p.id AND i.invoice_type = 'final'
                                  AND i.status NOT IN ('cancelled', 'void')) AS final_bill_raised,
                       EXISTS (SELECT 1 FROM encounter_charges c
                                WHERE c.patient_id = p.id
                                  AND c.status = 'pending') AS has_pending_charges
                FROM patients p
                LEFT JOIN discharge_summaries ds ON ds.patient_id = p.id
                WHERE p.status = 'discharged' {dept_clause}
                ORDER BY COALESCE(ds.discharged_at, {ended_without_summary}) DESC
                LIMIT ? OFFSET ?""",
            dept_param + (limit, offset),
        ).fetchall()
        rows = [{**dict(p), "final_bill_raised": bool(p["final_bill_raised"]),
                 "has_pending_charges": bool(p["has_pending_charges"])} for p in patients]

    response.headers["X-Total-Count"] = str(total)

    if rows:
        log_phi_read(
            request=request,
            user=user,
            action_type="discharged_list_viewed",
            summary_message=(
                f"Discharged patient list viewed ({len(rows)} of {total}) for "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={"result_count": len(rows), "total": total,
                      "patient_ids": [r["id"] for r in rows]},
        )

    return rows


@router.get("/patients/{patient_id}/story")
async def get_patient_story_route(
    patient_id: str,
    types: str | None = None,
    user: dict = Depends(
        require_patient_access("patient.read_all", audit_action="patient_story_viewed")
    ),
):
    """Unified patient story — one chronological clinical narrative merging
    admission, alerts, notes, imaging, labs, meds, orders, administrations,
    bundles, problems, allergies and discharge. `types` is an optional
    comma-separated filter (see STORY_TYPES).

    ``last_reviewed_at`` / ``new_count`` are ADDITIVE keys carrying this
    clinician's personal "I have read this chart up to here" watermark, so the
    story can draw the *since you last looked* line. They sit beside ``events``
    rather than inside it: an older client that only reads ``events`` is
    unaffected, and the ward round — which has its own overnight window and must
    not be perturbed by whose session it is — simply ignores them.

    The watermark read is a second, cheap query on an already-authorised chart;
    it deliberately does NOT move the mark. Advancing it is an explicit act
    (POST /api/chart/patients/{id}/reviewed), because a mark that moved just
    because a screen rendered would answer the wrong question.
    """
    from services.patient_story_service import get_patient_story
    from services.patient_timeline_service import read_review_state
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    events = await asyncio.to_thread(get_patient_story, patient_id, type_list)
    review = await asyncio.to_thread(read_review_state, user.get("id"), patient_id)
    return {"patient_id": patient_id, "events": events, **review}


@router.get("/patients/{patient_id}")
async def get_patient(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("patient.read_all", audit_action="patient_chart_viewed")
    ),
):
    """Get full patient details with vitals, notes, risk, and trends."""
    (patient, vitals, notes, scribe_notes, risk_history,
     lens_analyses, medications, lab_observations,
     profile, problems, admission) = await run_db(_load_patient_bundle, patient_id)

    # Score vitals[0], the row the command bar shows. This used to call
    # get_patient_risk(patient_id), which re-reads "the latest" row in its own
    # session: two sessions, autocommit SELECTs, no shared snapshot, so a bot
    # tick landing between the bundle read and that one scored a set the bar
    # was not showing. The scale comes from the patient row already in hand.
    on_scale_2 = (bool(patient["on_scale_2"])
                  if "on_scale_2" in patient.keys() and patient["on_scale_2"] is not None
                  else False)
    # The CfC overlay runs torch/MPS inference (per outcome) synchronously;
    # offload it so a patient-card open doesn't block the asyncio event loop and
    # stall live telemetry / other requests once checkpoints exist.
    # TODO (perf, cross-file): also serve the baseline from the precomputed
    # risk_scores table written by the bot to avoid recomputing CfC per card open.
    import asyncio as _asyncio
    # Scoring switched off (ORB_SCORING_ENABLED off, or the Orb Ward Record
    # profile): no NEWS2, no band, no response line and no schedule worked out
    # from one. `risk` is null and `risk_off_reason` says why, for the chart to
    # show in place of the badge; the stored risk history is not sent either.
    scoring = scoring_enabled()
    drug_chart = drug_chart_enabled()
    risk = await _asyncio.to_thread(
        risk_for_row, vitals[0] if vitals else None,
        patient_id=patient_id, on_scale_2=on_scale_2,
    ) if scoring else None
    trend = await _asyncio.to_thread(get_vitals_trend, patient_id)

    # The RCP minimum observation interval for this score, from the same table
    # the nurse board uses, so the chart can say when the last set is overdue.
    from services.nurse_shift_service import obs_schedule
    from services.discharge_service import parse_comorbidities

    patient_out = dict(patient)
    patient_out["comorbidities"] = parse_comorbidities(profile["comorbidities"] if profile else None)
    patient_out["admission_id"] = admission["id"] if admission else None

    site_name = None
    try:  # optional site setting; a printed record says nothing rather than invent one
        from services.site_config_service import current_site_config
        site_name = current_site_config().site_name
    except Exception:
        site_name = None

    return {
        "patient": patient_out,
        "risk": risk,
        "risk_off_reason": None if scoring else SCORING_OFF_REASON,
        "trend": trend,
        "obs_schedule": (obs_schedule(risk if risk.get("raw_news2") is not None else None)
                         if scoring else None),
        "lab_observations": [dict(o) for o in lab_observations],
        "problems": [dict(p) for p in problems],
        "site_name": site_name,
        "vitals": [
            {
                "timestamp": v["timestamp"],
                "heart_rate": v["heart_rate"],
                "systolic_bp": v["systolic_bp"],
                "diastolic_bp": v["diastolic_bp"],
                "spo2": v["spo2"],
                "temperature": v["temperature"],
                "respiratory_rate": v["respiratory_rate"],
                # Provenance, so the chart can say a row is simulated.
                "source": _row_source(v),
            }
            for v in vitals
        ],
        # True when the row the bar shows (vitals[0]) came from the simulator:
        # the honesty label rides on the chart, not on anyone's memory.
        "simulated_vitals": bool(vitals) and _row_source(vitals[0]) == LABEL_SIMULATED,
        "notes": [dict(n) for n in notes],
        "scribe_notes": [dict(s) for s in scribe_notes],
        "risk_history": [
            {
                "score": r["score"],
                "level": r["level"],
                "drivers": json.loads(r["drivers"]) if r["drivers"] else [],
                "timestamp": r["timestamp"],
            }
            for r in (risk_history if scoring else [])
        ],
        # image_url is the authenticated /uploads path the chart loads the
        # image from; image_path is a location on the appliance's disk.
        "lens_analyses": [decorate_analysis(a) for a in lens_analyses],
        # Surface an explicit `unverified` flag on AI-extracted/agentic meds that a
        # pharmacist has not yet verified, so the UI never presents an unreviewed
        # AI medication as an equal to a clinician-confirmed active order.
        # On an Orb Ward Record computer the drug chart stays on paper: no rows
        # are sent, and drug_chart false tells the chart to draw no medicines
        # at all, even before its own health probe answers (services/drug_chart).
        "medications": [
            {**dict(m),
             "unverified": (m["source"] in ("llm_extracted", "agentic")
                            if "source" in m.keys() else False)
                           and not (("verified_by" in m.keys()) and m["verified_by"])}
            for m in (medications if drug_chart else [])
        ],
        "drug_chart": drug_chart,
    }


@router.get("/patients/{patient_id}/vitals")
async def get_patient_vitals(
    patient_id: str,
    hours: int = 6,
    # Scope-checked but deliberately NOT read-audited: the sparkline polls, and
    # one fail-closed encrypted-SQLite write per poll is the exact shape that
    # stalled the loop on 2026-07-30. The scope check is the control here.
    user: dict = Depends(require_patient_access("vitals.read")),
):
    """Get recent vitals for sparkline charts."""
    vitals = await run_db(_load_recent_vitals, patient_id, hours)

    return [
        {
            "timestamp": v["timestamp"],
            "heart_rate": v["heart_rate"],
            "systolic_bp": v["systolic_bp"],
            "diastolic_bp": v["diastolic_bp"],
            "spo2": v["spo2"],
            "temperature": v["temperature"],
            "respiratory_rate": v["respiratory_rate"],
            "source": _row_source(v),
        }
        for v in vitals
    ]


def _row_source(row) -> str | None:
    """`vitals.source` of a row, or None on a database that predates it."""
    return row["source"] if "source" in row.keys() else None


@router.get("/vitals-feed")
def get_vitals_feed(user: dict = Depends(require_permission("vitals.read"))):
    """Where this system's vital signs come from, for the shell's honesty label.

    Anything but 'ingestion' means the simulator is writing readings
    (config.INGESTION_MODE: 'simulation' or 'hybrid'). No patient data.
    """
    from config import INGESTION_MODE
    return {"mode": INGESTION_MODE, "simulated": INGESTION_MODE != "ingestion"}


@router.get("/patients/{patient_id}/notes")
async def get_patient_notes(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("patient.read_all", audit_action="patient_notes_viewed")
    ),
):
    """Get all clinical notes for a patient."""
    notes = await run_db(_load_patient_notes, patient_id)
    return [dict(n) for n in notes]


@router.post("/patients/{patient_id}/notes")
async def add_patient_note(
    patient_id: str,
    data: dict,
    # Writes are already audited by the mutation middleware; what they lacked
    # was the ward check — charting on another ward's patient is worse than
    # reading one.
    user: dict = Depends(require_patient_access("patient.write")),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    """Add a new clinical note for a patient.

    Idempotent: offline replays resend the same Idempotency-Key header, so a
    key that already exists returns the existing note instead of duplicating it.
    """
    content = data.get("content")
    if not content:
        raise HTTPException(status_code=400, detail="Content is required")

    inserted_id = await run_db(
        _insert_patient_note, patient_id, data, user["id"], idempotency_key
    )
    return {"status": "success", "id": inserted_id}


# ═══════════════════════════════════════════════════════════════════════════
#  REMOVED 2026-08-15: `GET /patients/{patient_id}/analysis`
#
#  It streamed a 512-token 27B generation on EVERY chart open, with its 5-minute
#  cache commented out ("Disable cache temporarily for debugging"), and it took
#  the same background LLM slot as Sage's alert advice at the same priority
#  while waiting 120 s for it — its own service comment conceded the
#  queue-fairness problem. On the ward this was seen as a "✦ MedGemma AI
#  Assessment — Generating…" shimmer that NEVER resolved (held for a full 150 s
#  dedicated test and 6+ minutes live), and it was a major cause of the 22 %
#  delivery rate on the advice tier it was competing with.
#
#  It is not re-enabled with a cache and a bounded wait because it had no
#  content of its own to justify that: it duplicated the full four-section
#  assessment that was deliberately relocated to handover/ward round, on a
#  surface where Tier 1 (deterministic, 33-240 ms, on 100 % of alerts) already
#  answers the question. Deleting a queue competitor is worth more than the
#  panel was. The only caller was `AiNarrative` in src/pages/panels/VigilPanel.tsx,
#  removed in the same change.
#
#  `generate_patient_analysis` and the `_analysis_cache` / `_analysis_locks`
#  globals in services/vigil_service.py are now unreferenced; that file is owned
#  by another change this round and is left untouched.
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/patients/{patient_id}/medication-reconciliation",
            dependencies=[Depends(require_drug_chart)])
async def medication_reconciliation_route(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("discharge.generate",
                               audit_action="medication_reconciliation_viewed")
    ),
):
    """Deterministic discharge medication reconciliation (continued/stopped/
    unverified) from the real medication record — previewable before discharge."""
    from services.discharge_service import build_medication_reconciliation
    exists = await run_db(_patient_exists, patient_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Patient not found")
    # Opens its own connection: off the loop.
    return await run_db(build_medication_reconciliation, patient_id)


class DischargeIn(BaseModel):
    """How the stay ended (adt-8a). Read only while ORB_ADT is on; with the
    module off the body is ignored and the discharge is exactly what it was."""
    #: routine (when omitted), referred_out, transferred_out or dama. LAMA,
    #: absconded and death are refused with the End of stay route.
    discharge_type: str | None = None
    #: The patient's MRN typed back: required for a DAMA.
    confirm_mrn: str | None = None
    #: Required for a DAMA.
    reason: str | None = None


def _active_admission_id(patient_id: str):
    with db_session() as db:
        row = db.execute(
            "SELECT id FROM admissions WHERE patient_id = ? AND status = 'active' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
    return row["id"] if row else None


@router.post("/patients/{patient_id}/discharge")
async def discharge_patient_route(
    patient_id: str,
    request: Request,
    body: DischargeIn | None = None,
    # Ward-scoped like its siblings. This was a bare require_permission, so a
    # General Surgery doctor could discharge a General Medicine patient whose
    # chart they are 404'd from reading, and receive the portal link for it.
    user: dict = Depends(
        require_patient_access("discharge.generate", audit_action="discharge_generated")
    ),
):
    """Discharge a patient: drafts the summary, builds the timeline, issues the
    QR link when the patient has a second factor.

    The summary is stored as a DRAFT and returned to the discharging doctor to
    read; the patient sees it only after a prescriber marks it reviewed
    (POST .../discharge-summary/review).

    With ORB_ADT on (adt-8a) the optional body says how the stay ended:
    routine, referred out, transferred out or DAMA (a DAMA needs the MRN typed
    back and a reason). LAMA, absconded and death are 409 'use_end_of_stay'
    naming POST /api/adt/admissions/{id}/end, before any summary is drafted.
    """
    from services.adt import end_stay as adt_end
    from services.adt.core import AdtError, adt_on, http_detail
    from services.discharge_service import (
        build_patient_timeline,
        generate_discharge_summary,
        discharge_patient,
        offline_discharge_summary,
    )

    # Check patient exists and is admitted. This check has always been separated
    # from the discharge_patient() write by `await generate_discharge_summary`,
    # so it is advisory (a 400 for the common case), not a lock — the awaited
    # off-loop read does not change that. discharge_patient() remains the
    # authority on the final state transition.
    patient = await run_db(_load_patient_row, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if patient["status"] != "admitted":
        raise HTTPException(status_code=400, detail="Patient is not currently admitted")

    # ORB_ADT on: refuse a wrong end type or a failed read-back BEFORE the
    # summary is drafted. discharge_patient checks both again inside its
    # transaction, which is the authority.
    adt_kwargs: dict = {}
    if adt_on():
        try:
            end_type = adt_end.discharge_route_type(body.discharge_type if body else None)
            if end_type in adt_end.READ_BACK_ON_DISCHARGE_ROUTE:
                adt_end.check_read_back(dict(patient), body.confirm_mrn if body else None)
                adt_end.require_reason(end_type, body.reason if body else None)
        except AdtError as err:
            detail = http_detail(err)
            if err.code == "use_end_of_stay":
                admission_id = await run_db(_active_admission_id, patient_id)
                detail["end_of_stay_route"] = (
                    f"/api/adt/admissions/{admission_id}/end" if admission_id else None)
            raise HTTPException(status_code=err.http_status, detail=detail)
        adt_kwargs = {
            "discharge_type": end_type,
            "confirm_mrn": body.confirm_mrn if body else None,
            "reason": body.reason if body else None,
            "request_meta": {
                "request_path": request.url.path,
                "http_method": request.method,
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
            },
        }

    # Generate timeline and summary. Both builders open their own SQLite
    # connections, so they run off the event loop.
    timeline = await run_db(build_patient_timeline, patient_id)

    drafted_by = "assistant"
    try:
        summary = await generate_discharge_summary(patient_id)
    except Exception:
        summary = ""
    if not (summary or "").strip():
        # Drafting assistant unavailable (or it answered nothing): store the
        # recorded facts only. The old fallback asserted "Stable for
        # discharge" about every patient.
        summary = await run_db(offline_discharge_summary, patient_id)
        drafted_by = "unavailable"

    try:
        result = await run_db(discharge_patient, patient_id, user["id"], summary, timeline,
                              **adt_kwargs)
    except AdtError as err:
        raise HTTPException(status_code=err.http_status, detail=http_detail(err))
    if result.get("status") != "success":
        return result
    # The discharging doctor reads the draft from this response, labelled as
    # unreviewed. The patient-facing readers withhold it until review.
    return {
        **result,
        "summary": summary,
        "drafted_by": drafted_by,
        "timeline": timeline,
    }


@router.get("/patients/{patient_id}/discharge-summary")
async def get_discharge_summary(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("discharge.read", audit_action="discharge_summary_viewed")
    ),
):
    """Latest discharge summary for a patient, with its review state.

    Clinician-facing, so a draft is returned too, labelled by review_status.
    qr_code_url is present only when a QR was actually issued: it used to be
    returned for every summary, so a discharge with no secure link showed a QR
    box that could only ever fail to load.
    """
    from services.discharge_service import load_discharge_summary_view
    view = await run_db(load_discharge_summary_view, patient_id)

    if not view:
        raise HTTPException(status_code=404, detail="Discharge summary not found")
    return view


class DischargeReviewIn(BaseModel):
    """The summary the doctor actually read. Named explicitly so a review can
    never land on a newer draft that appeared while they were reading."""
    summary_id: int


@router.post("/patients/{patient_id}/discharge-summary/review")
async def review_discharge_summary(
    patient_id: str,
    body: DischargeReviewIn,
    # discharge.publish ("finalise / sign a discharge") is the prescriber's.
    # Ward-scoped like every other {patient_id} write.
    user: dict = Depends(require_patient_access("discharge.publish")),
):
    """Mark a drafted discharge summary reviewed, which releases it to the
    patient's portal and discharge link. Interim until the draft, edit and sign
    flow; the text is not changed here."""
    from services.discharge_service import (
        DischargeSummaryNotFound,
        mark_discharge_summary_reviewed,
    )
    try:
        return await run_db(
            mark_discharge_summary_reviewed, patient_id, body.summary_id,
            user["id"], user.get("role"),
        )
    except DischargeSummaryNotFound:
        raise HTTPException(status_code=404, detail="Discharge summary not found")




@router.post("/patients/{patient_id}/medications", dependencies=[Depends(require_drug_chart)])
async def add_patient_medication(
    patient_id: str,
    data: dict,
    request: Request,
    user: dict = Depends(require_patient_access("medication.write")),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    """Add a new medication for a patient.

    Idempotent: offline replays resend the same Idempotency-Key header, so a
    key that already exists returns the existing order instead of duplicating it.
    """
    drug_name = data.get("drug_name")
    dosage = data.get("dosage")
    route = data.get("route", "Oral")
    frequency = data.get("frequency")
    status = data.get("status", "active")

    if not all([drug_name, dosage, frequency]):
        raise HTTPException(status_code=400, detail="Drug name, dosage, and frequency are required")

    if medorder_core.store_on():
        # ORB_MED_ORDER_STORE on (medorders-3): the same gate call, then ONE
        # transaction for the ward row, its medication order, the order's
        # event and its audit row (services/medorders/chart_lane.py). Through
        # run_db, so the gate's SQLite work is off the event loop as well.
        # Nothing the ward sees changes: same checks, same row, same answer,
        # plus the order's id. With the store off, the path below is unchanged.
        try:
            result = await run_db(
                chart_lane.create_chart_order, patient_id, drug_name, dosage, route,
                frequency, status, actor_id=user["id"], actor_role=user.get("role"),
                idempotency_key=idempotency_key, safety_ack=data.get("safety_ack"),
                request_meta={
                    "request_path": request.url.path,
                    "http_method": request.method,
                    "ip_address": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent"),
                },
            )
        except medorder_core.MedOrderError as e:
            raise HTTPException(status_code=e.http_status, detail=medorder_core.http_detail(e))
        return {"status": "success", "id": result["id"], "safety": result["safety"],
                "med_order_id": result["med_order_id"]}

    # Order-time drug-safety gate: allergy + interaction screen BEFORE any DB
    # write. Severe alerts raise 428 until the clinician re-submits with
    # safety_ack={acknowledged, reason} (override is audited on the hash chain).
    # Screening before the idempotency pre-check is deliberate: a replay of the
    # same order screens identically, so alerts (and any 428) are consistent.
    # The dose guard runs in the same gate (dose + route): an 8 g paracetamol is
    # refused here exactly as the order-set and bedside lanes refuse it.
    safety = enforce_medication_safety(
        patient_id,
        drug_name,
        actor_id=user["id"],
        entry_point="vigil.med_add",
        safety_ack=data.get("safety_ack"),
        dose=dosage,
        route=route,
        actor_role=user.get("role"),
    )

    inserted_id = await run_db(
        _insert_medication, patient_id, drug_name, dosage, route, frequency,
        status, user["id"], idempotency_key,
    )
    return {"status": "success", "id": inserted_id, "safety": safety}


@router.get("/patients/{patient_id}/qr")
async def get_qr_code(
    patient_id: str,
    user: dict = Depends(
        require_patient_access("discharge.read", audit_action="discharge_qr_viewed")
    ),
):
    """Serve the QR code image for a discharged patient.

    Requires discharge.read — previously this endpoint had NO auth dependency,
    making it a public path that served the QR PNG. The QR embeds the plaintext
    long-lived discharge access token, so an unauthenticated requester (given a
    patient_id, which appears throughout the authenticated UI/URLs/logs) could
    recover the bearer secret guarding the discharge summary, meds, and attending
    doctor — a direct PHI exposure. Gated now like every neighbouring endpoint.
    """
    summary = await run_db(_load_qr_path, patient_id)
    if not summary or not summary["qr_code_path"]:
        raise HTTPException(status_code=404, detail="QR code not found")
    return FileResponse(summary["qr_code_path"], media_type="image/png")


# The ghost door, retired (opd-1 part g, also adt-5's legacy-route item).
# This route used to write a status-'admitted' patient with NO admissions row:
# no department, so the patient sat on nobody's board, while status-only
# readers (the vitals simulator, the sepsis sweep) treated them as an
# inpatient. Its only callers were a dead client wrapper, the soak test and a
# name-rule test, all now moved to POST /api/admissions/admit. It answers 410
# Gone, writes nothing and reads no body; the permission check stays, so who
# is refused, and how, is unchanged.
GHOST_PATIENT_ROUTE_RETIRED = (
    "Adding a patient here has been retired. Admit the patient through the "
    "admission form (POST /api/admissions/admit), which records the "
    "admission, the department and the bed."
)


@router.post("/patients", status_code=410)
def create_patient(
    user: dict = Depends(require_permission("patient.write")),
):
    """Retired. Answers 410 and names the admission door."""
    raise HTTPException(status_code=410, detail=GHOST_PATIENT_ROUTE_RETIRED)


@router.get("/patients/{patient_id}/timeline-projection",
            dependencies=[Depends(require_models_on)])
async def get_patient_timeline_projection(
    patient_id: str,
    refresh: bool = False,
    user: dict = Depends(
        require_patient_access("patient.read_all", audit_action="patient_timeline_viewed")
    ),
):
    """Sage-deduced expected clinical course for the patient.

    Returns ordered care phases + expected length of stay reasoned from the
    diagnosis, comorbidities, age and sex (cached per patient), with the current
    phase derived from the patient's live risk level. Replaces the calendar-day
    timeline so a short illness is not shown as a 40-day stay.
    """
    from services.timeline_service import get_timeline_projection

    # Scoring switched off: the stored band is not read, so no phase is
    # placed from a score.
    risk = await run_db(_load_latest_risk, patient_id) if scoring_enabled() else None
    risk_level = risk["level"] if risk else "stable"

    result = await get_timeline_projection(patient_id, risk_level=risk_level, force_refresh=refresh)
    if result.get("error") == "patient_not_found":
        raise HTTPException(status_code=404, detail="Patient not found")
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Escalation chain — nurse → registrar → consultant, timed, acknowledged
# ═════════════════════════════════════════════════════════════════════════════
# WHY THESE ROUTES EXIST. Vigil could score a NEWS2 19 and the only durable
# consequence was a row in `vitals` and a line in a Relay case room — nothing
# recorded that a human saw it, and nothing happened when no human did. These
# endpoints are the surface of services/escalation_service.py: what is still
# unanswered, who answered it, and what they did about it.
#
# EVERY ROUTE HERE IS A PLAIN `def`. The escalation service opens SQLite (and on
# the raise path an encrypted write plus a fail-closed audit row), so an
# `async def` would run all of that on the event loop — the shape that froze the
# API on 2026-07-30. FastAPI threadpools sync routes; that is the whole reason
# `list_patients` above is also a plain def.
#
# PERMISSIONS, and the gap in them. Reading is `alert.read` (doctor, nurse,
# admin). Acknowledging and resolving require `alert.read` AND `vitals.write`,
# which resolves to exactly {doctor, clinician, nurse} — the bedside clinicians.
# An administrator can SEE an unanswered deterioration and cannot silently clear
# it, which is the right way round. The honest gap: this is a permission
# COMBINATION standing in for a dedicated `escalation.acknowledge`, because
# permissions.py belongs to another change this cycle. The combination is
# strictly narrower than either permission alone, so it is a placeholder that
# fails safe.


class EscalationAcknowledgeIn(BaseModel):
    """Deliberately carries NO patient id. The chain names the patient; letting a
    request name one is the caller-named-scope hole that
    tests/test_caller_named_patient_scope.py exists to catch."""
    note: str | None = None


class EscalationResolveIn(BaseModel):
    reason: str
    category: str | None = None


def _escalation_or_404(escalation_id: str, user: dict,
                       include_events: bool = True) -> dict:
    """Load a chain and prove the caller's ward owns its patient.

    404 on both "no such escalation" and "not your ward", never 403 — a 403
    confirms the patient exists, which is itself a disclosure. Same rule as
    permissions.require_patient_access, which cannot be used here because the id
    on the path is a SURROGATE (the escalation) that resolves to a patient — the
    spelling permissions.enforce_patient_scope's docstring names as the blind
    spot of the 2026-08-05 read sweep.
    """
    from services import escalation_service

    chain = escalation_service.get_escalation(
        escalation_id, include_events=include_events)
    if chain is None:
        raise HTTPException(status_code=404, detail="Escalation not found")
    # Raises 404 itself when the patient is outside the caller's department.
    enforce_patient_scope(user, chain["patient_id"])
    return chain


@router.get("/escalations")
def list_escalations_route(
    request: Request,
    include_resolved: bool = Query(False),
    limit: int = Query(200, ge=1, le=500),
    user: dict = Depends(require_permission("alert.read")),
):
    """Everything on this ward that a deterioration alert is still waiting on.

    THE READ IS ALSO A TICK OF THE MECHANISM, and that is a deliberate design
    decision rather than a shortcut:

      * `advance_due()` runs UNSCOPED on every read. Advancing a tier creates no
        chain and discloses nothing, and scoping it would mean an escalation on a
        ward nobody happens to have open never climbs — the exact failure this
        feature exists to end, reintroduced through a permissions detail.
      * `scan_new_observations()` runs ONLY for a ward-scoped caller, and only
        over that ward. It is the safety net for a vitals path that was never
        wired to raise an escalation (which is how the original hole existed at
        all). A hospital-wide governance account reading a board must not
        manufacture clinical state across every ward it can see, so for a caller
        with no department this step is skipped.

    Latency is therefore bounded by how often somebody looks, or by whatever
    calls POST /escalations/sweep. A writer that calls
    escalation_service.evaluate_observation post-commit escalates immediately and
    leaves this with nothing to find.
    """
    from services import escalation_service

    dept = department_scope(user)
    escalation_service.advance_due()
    if dept is not None:
        escalation_service.scan_new_observations(department=dept)

    rows = escalation_service.list_escalations(
        department=dept, include_resolved=include_resolved, limit=limit)

    if rows:
        log_phi_read(
            request=request,
            user=user,
            action_type="escalation_board_viewed",
            summary_message=(
                f"Escalation board viewed ({len(rows)} chains) for "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={
                "result_count": len(rows),
                "open_count": sum(1 for r in rows if r["status"] == "open"),
                "include_resolved": include_resolved,
                "patient_ids": [r["patient_id"] for r in rows],
            },
        )

    return {
        "escalations": rows,
        "open_count": sum(1 for r in rows if r["status"] == "open"),
        "policy": escalation_service.tier_policy(),
        "news2_threshold": escalation_service.news2_threshold(),
    }


@router.get("/escalations/{escalation_id}")
def get_escalation_route(
    escalation_id: str,
    request: Request,
    user: dict = Depends(require_permission("alert.read")),
):
    """One chain and every recorded step: raised, each repeat observation, each
    timed tier advance, the acknowledgement, the resolution."""
    chain = _escalation_or_404(escalation_id, user)
    log_phi_read(
        request=request,
        user=user,
        action_type="escalation_viewed",
        summary_message=f"Escalation {escalation_id} viewed",
        patient_id=chain["patient_id"],
        target_type="escalation",
        target_id=escalation_id,
        metadata={"status": chain["status"], "tier": chain["current_tier"]},
    )
    return chain


@router.post("/escalations/{escalation_id}/acknowledge")
def acknowledge_escalation_route(
    escalation_id: str,
    body: EscalationAcknowledgeIn,
    user: dict = Depends(require_permission("alert.read", "vitals.write")),
):
    """"I have seen this and I am dealing with it." Records who and when, and
    stops the tier ladder. Does NOT close the escalation — see /resolve."""
    from services import escalation_service

    _escalation_or_404(escalation_id, user, include_events=False)
    try:
        return escalation_service.acknowledge(escalation_id, user, note=body.note)
    except escalation_service.EscalationNotFound:
        raise HTTPException(status_code=404, detail="Escalation not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/escalations/{escalation_id}/resolve")
def resolve_escalation_route(
    escalation_id: str,
    body: EscalationResolveIn,
    user: dict = Depends(require_permission("alert.read", "vitals.write")),
):
    """Close the chain WITH A REASON. The reason is mandatory: a resolve button
    with no reason is a dismiss button, and a dismiss button is how a
    deterioration alert becomes noise a ward learns to clear without reading."""
    from services import escalation_service

    _escalation_or_404(escalation_id, user, include_events=False)
    try:
        return escalation_service.resolve(
            escalation_id, user, reason=body.reason, category=body.category)
    except escalation_service.EscalationNotFound:
        raise HTTPException(status_code=404, detail="Escalation not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/escalations/sweep")
def sweep_escalations_route(
    user: dict = Depends(require_permission("alert.read")),
):
    """One tick of the mechanism for an ops caller or a scheduler: advance every
    overdue tier, then catch any qualifying observation nothing dispatched.

    Returns COUNTS ONLY — no patient, no score, no name — so it is safe to poll
    from a cron line and safe to log. Scoped to the caller's department when they
    have one; hospital-wide for an unscoped governance account, which is the case
    a scheduled sweep should run under.
    """
    from services import escalation_service

    return escalation_service.sweep(department=department_scope(user))


@router.get("/patients/{patient_id}/escalations")
def patient_escalations_route(
    patient_id: str,
    include_resolved: bool = Query(True),
    user: dict = Depends(
        require_patient_access("alert.read", audit_action="patient_escalations_viewed")
    ),
):
    """Escalation history for one patient, for the chart rather than the board.

    Ward check and PHI-read audit both come from require_patient_access, so this
    route holds no second scope derivation of its own.
    """
    from services import escalation_service

    return {
        "patient_id": patient_id,
        "escalations": escalation_service.patient_escalations(
            patient_id, include_resolved=include_resolved),
    }
