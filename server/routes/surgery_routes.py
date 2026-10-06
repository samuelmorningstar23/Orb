from fastapi import APIRouter, Depends, HTTPException, Query, Request
from database import db_session, run_db
from auth import get_current_user
from permissions import require_permission
import logging
import os
import uuid
import json
from datetime import datetime, timezone
from fastapi.responses import StreamingResponse
from services.audit_service import log_audit_event
from services.scribe_service import save_scribe_note
from services.surgery_service import (
    generate_risk_assessment,
    trigger_surgery_prep,
    turnaround_minutes,
)
from services.surgery_escalation_service import acknowledge_emergency
from services.surgery_scope import (
    enforce_surgery_scope,
    log_surgical_phi_read,
    surgery_in_scope_blocking,
    surgery_patient_id,
    surgical_list_filter,
    surgical_scope,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
#  WARD SCOPE ON THIS MODULE — THE RULE, IN ONE PARAGRAPH
# ═══════════════════════════════════════════════════════════════════════════
# Surgery is genuinely cross-departmental, so it does NOT use the plain
# "patient is on your ward" filter the rest of the app uses. A caller may read a
# surgical case if ANY of: (A) they have no department on record — the existing
# hospital-wide governance convention; (B) their department is a PERIOPERATIVE
# one (General Surgery, Anesthesiology, …), because theatre serves every ward —
# SURGICAL AND ANAESTHETIC STAFF ARE HOSPITAL-WIDE ON THIS MODULE BY ROLE, which
# is a property of the USER ROW and is now stamped on every audit row as
# `surgical_scope: perioperative` instead of being an accident of a missing
# WHERE clause; (C) the patient is admitted to the caller's own department, so
# the owning ward still sees its own patient going to theatre; or (D) the caller
# is the case's surgeon or on its team roster.
#
# Everything else is invisible: filtered out of the lists, 404 (never 403 — that
# would confirm the case exists) on the detail routes, with the same detail
# string an unknown id returns. Every named-patient read below writes a
# FAIL-CLOSED `phi_access` row carrying which clause let it through.
#
# The full reasoning, the seeded-account arithmetic, and the failure modes are in
# services/surgery_scope.py. Do not re-derive scope here — call that module.

router = APIRouter(prefix="/api/surgery", tags=["surgery"])

# Statuses a case may legally be started from. 'in_progress' is excluded so a
# second POST /start cannot restart a running case, and the terminal states are
# excluded so a completed operation can never be reopened by an errant click —
# the previous blind UPDATE happily flipped 'completed' back to 'in_progress',
# which would have detached the case from its post-op note.
_STARTABLE_STATUSES = {"scheduled", "emergency_ready", "emergency_acknowledged",
                       "emergency_staged"}
# Statuses an emergency intake can be acknowledged from.
_ACKNOWLEDGEABLE_STATUSES = {"emergency_pending", "emergency_context_generating",
                             "emergency_ready", "emergency_staged"}

# ═══════════════════════════════════════════════════════════════════════════
#  COMPLETION IS A TRANSITION, NOT A FLAG — and what gates it
# ═══════════════════════════════════════════════════════════════════════════
# Statuses a case may legally be COMPLETED from. Exactly one: a case that is
# running. `/complete` previously guarded only against completing TWICE, so
# every other status walked straight through it — and on 2026-08-14 the dry-run
# verification did exactly that, marking a case 'completed' that `/start` had
# REFUSED twice for want of team confirmations. What that produces is not a
# cosmetic wrong status: it writes a POST-OPERATIVE NOTE into the patient's
# chart for an operation that never happened, files a `surgery_completed` row on
# the audit chain, and (since migration 27) would stamp a completed_at with no
# started_at to pair it with — a case that is finished, documented and
# unmeasurable. 'scheduled', 'cancelled' and every emergency_* status therefore
# 409 here, exactly as they do on `/start`.
_COMPLETABLE_STATUSES = {"in_progress"}

# ── THE WHO CHECKLIST GATE — CONFIGURABLE, DEFAULT ON. RULING PENDING. ───────
# Before this change the WHO Surgical Safety Checklist gated NOTHING: a case
# could be completed at 0/19, and 19/19 bought nothing but a progress bar. That
# is the checklist as decoration.
#
# The other half of the argument is real too, so it is written down rather than
# quietly resolved: the backend enforces no phase ordering, so a "time out"
# (which WHO places before skin incision) can be attested after sign-out, and
# turning the whole checklist into a hard completion gate enforces a workflow no
# hospital has yet agreed to. A theatre in the middle of a case that cannot
# close it because one tick was missed will simply learn to pre-tick all 19 at
# induction, which destroys the instrument's meaning far more thoroughly than
# not gating at all.
#
# So: ORB_SURGERY_REQUIRE_CHECKLIST, read at CALL time (same convention as
# surgery_scope._PERIOP_ENV — an operator can change the ruling without a
# redeploy, and tests can pin either side of it). DEFAULT ON, because the pilot
# ships a checklist that must mean something and the refusal is a 409 that says
# exactly which items are outstanding, not a silent failure.
#
# NOT gated by this flag, deliberately: `/start`. The gate is on COMPLETION
# only, so a checklist can be filled in as the case runs, which is when the
# sign-out phase is physically possible to attest.
_CHECKLIST_GATE_ENV = "ORB_SURGERY_REQUIRE_CHECKLIST"


def _checklist_gate_enabled() -> bool:
    """Is the WHO checklist required before a case may be completed?

    Unset means ON. Only an explicit off-value turns it off, so a mistyped
    value fails SAFE (gate stays on) rather than silently disabling a clinical
    safety check.
    """
    raw = os.environ.get(_CHECKLIST_GATE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


class _CompletionRefused(Exception):
    """Internal control-flow signal: a completion gate said no.

    Raised INSIDE complete_surgery's guard transaction and converted to a 409
    (plus a `denied` audit row) OUTSIDE it, so the write lock is released before
    the audit chain — which opens its own session — is touched. Never escapes
    this module.
    """

    def __init__(self, *, reason: str, detail: str, from_status: str | None,
                 patient_id: str | None, metadata: dict | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.from_status = from_status
        self.patient_id = patient_id
        self.metadata = metadata or {}

# A schedule request is a per-day fan-out loop plus a date-range scan; an
# unbounded `days` let any authenticated caller ask for a 100000-day window and
# tie up a DB worker building the empty days.
_MAX_SCHEDULE_DAYS = 90


def _audit_surgery(request: Request, user: dict, action_type: str, surgery_id: str,
                   summary: str, patient_id: str | None = None,
                   metadata: dict | None = None, outcome: str = "success") -> None:
    """Domain audit event for a surgical state change.

    The generic mutation middleware records that a POST happened; a surgical
    state change needs the case, the patient and the transition in the trail
    too — this module previously emitted no domain audit events at all.
    """
    log_audit_event(
        correlation_id=f"surgery-{action_type}-{surgery_id}",
        event_category="clinical_record",
        action_type=action_type,
        outcome=outcome,
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type="surgery",
        target_id=surgery_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata=metadata,
    )


# ═══════════════════════════════════════════════════════════════════
# READ ENDPOINTS (Schedule, Emergency Queue, Detail)
# ═══════════════════════════════════════════════════════════════════

@router.get("/schedule")
def get_surgery_schedule(
    request: Request,
    days: int = Query(7, ge=1, le=_MAX_SCHEDULE_DAYS),
    user: dict = Depends(require_permission("surgery.read")),
):
    """Get scheduled surgeries grouped by day for the week view.

    WARD SCOPED + READ AUDITED (see the module header). This row set is name,
    MRN, age, sex and risk for every operation in the window — a bulk PHI
    disclosure that carried no scope and left no trace, because the audit
    middleware fires on writes only.
    """
    from datetime import timedelta
    from services.hospital_time import hospital_today
    # HOSPITAL-local today, not UTC. `surgeries.scheduled_date` is a local
    # calendar date, and the UTC date is a different day for the first 5.5 hours
    # of every IST day: before 05:30 the week view opened on YESTERDAY's list.
    # Same convention as the Forecast day-0 row (services/hospital_time.py), so
    # the two surfaces cannot disagree about which day is today.
    today = hospital_today()
    end_date = today + timedelta(days=days)

    scope_sql, scope_params = surgical_list_filter(user)

    from services.vigil_service import score_vitals_row

    with db_session() as db:
        rows = db.execute(
            f"""SELECT s.*, p.name as patient_name, p.mrn, p.age, p.sex, u.name as surgeon_name,
                      p.on_scale_2 as patient_on_scale_2
               FROM surgeries s
               JOIN patients p ON s.patient_id = p.id
               LEFT JOIN users u ON s.surgeon_id = u.id
               WHERE s.scheduled_date BETWEEN ? AND ?{scope_sql}
               ORDER BY s.scheduled_date ASC, s.scheduled_time ASC""",
            (today.isoformat(), end_date.isoformat()) + scope_params
        ).fetchall()
        # The band is the NEWS2 of each patient's LATEST VITALS row (id breaks
        # a timestamp tie), the Vigil board's rule since 3 October. It came off
        # risk_scores, which the simulator and the Scribe lane never write, and
        # a patient with no risk row was printed "stable".
        latest_vitals: dict[str, dict | None] = {}
        for r in rows:
            pid = r["patient_id"]
            if pid not in latest_vitals:
                # A set marked entered in error is never the latest, as on
                # the board and every other vitals reader.
                v = db.execute(
                    "SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL "
                    "ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (pid,),
                ).fetchone()
                latest_vitals[pid] = dict(v) if v else None

    # Scored after the session closes: the CfC overlay reads the vitals history
    # on its own connection.
    on_scale_2 = {r["patient_id"]: bool(r["patient_on_scale_2"]) for r in rows}
    scores = {
        pid: score_vitals_row(v, patient_id=pid, on_scale_2=on_scale_2[pid])
        for pid, v in latest_vitals.items()
    }

    # Group by date
    grouped: dict[str, list] = {}
    for r in rows:
        d = r["scheduled_date"]
        if d not in grouped:
            grouped[d] = []
        row_dict = dict(r)
        row_dict.pop("patient_on_scale_2", None)
        news2 = scores.get(r["patient_id"])
        # None when nothing is recorded: "not scored", never a default band.
        row_dict["risk_level"] = news2["level"] if news2 else None
        row_dict["risk_score"] = news2["score"] if news2 else None
        row_dict["news2"] = news2["raw_news2"] if news2 else None
        row_dict["news2_complete"] = news2.get("complete") if news2 else None
        grouped[d].append(row_dict)

    # Build day-by-day schedule (include empty days)
    schedule = []
    for i in range(days):
        day = today + timedelta(days=i)
        day_str = day.isoformat()
        schedule.append({
            "date": day_str,
            "day_label": day.strftime("%A"),
            # SurgicalSuitePanel renders a 'Today' badge off this flag; the
            # route never sent it, so the badge was unreachable and every
            # day card in the week view looked alike.
            "is_today": day == today,
            "surgeries": grouped.get(day_str, []),
        })

    # Audited only when PHI actually left — same rule as the Vigil patient board
    # (routes/vigil_routes.py): this endpoint is polled by every open Surgical
    # Suite, and a window with no readable cases discloses nothing, so it must
    # not file a disclosure row. `phi_access` is fail-closed, so a row that
    # cannot be persisted means no schedule is returned.
    if rows:
        scope, dept = surgical_scope(user)
        log_surgical_phi_read(
            request=request,
            user=user,
            action_type="surgery_schedule_viewed",
            summary_message=(
                f"Surgical schedule viewed ({len(rows)} case(s), {days}d), scope "
                f"{scope}/{dept or '(no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={"result_count": len(rows), "days": days},
        )

    return schedule

@router.get("/emergency-queue")
def get_emergency_queue(request: Request,
                        user: dict = Depends(require_permission("surgery.read"))):
    """Get current emergency surgical cases. WARD SCOPED + READ AUDITED."""
    scope_sql, scope_params = surgical_list_filter(user)
    with db_session() as db:
        rows = db.execute(
            f"""SELECT s.*, p.name as patient_name, p.mrn, p.age, p.sex, p.bed, u.name as surgeon_name
               FROM surgeries s
               JOIN patients p ON s.patient_id = p.id
               LEFT JOIN users u ON s.surgeon_id = u.id
               WHERE s.status IN ('emergency_pending', 'emergency_ready', 'emergency_context_generating')
                 {scope_sql}
               ORDER BY s.scheduled_date ASC, s.scheduled_time ASC""",
            scope_params
        ).fetchall()
        result = [dict(r) for r in rows]

    if result:
        scope, dept = surgical_scope(user)
        log_surgical_phi_read(
            request=request,
            user=user,
            action_type="surgery_emergency_queue_viewed",
            summary_message=(
                f"Emergency surgical queue viewed ({len(result)} case(s)), scope "
                f"{scope}/{dept or '(no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={"result_count": len(result)},
        )
    return result

@router.get("/cases")
def get_surgery_cases(request: Request,
                      user: dict = Depends(require_permission("surgery.read"))):
    """Get all active surgical cases. WARD SCOPED + READ AUDITED."""
    scope_sql, scope_params = surgical_list_filter(user)
    with db_session() as db:
        cases = db.execute(
            f"""SELECT s.*, p.name as patient_name, p.mrn, p.age, p.sex
               FROM surgeries s
               JOIN patients p ON s.patient_id = p.id
               WHERE s.status NOT IN ('completed', 'cancelled')
                 {scope_sql}
               ORDER BY s.scheduled_date ASC, s.scheduled_time ASC""",
            scope_params
        ).fetchall()

        # Also fetch team assignments
        case_ids = [c["id"] for c in cases]
        team_assignments = []
        if case_ids:
            placeholders = ",".join(["?"] * len(case_ids))
            team_assignments = db.execute(
                f"""SELECT * FROM surgery_team_assignments WHERE surgery_id IN ({placeholders})""",
                case_ids
            ).fetchall()
            
        result = []
        for c in cases:
            case_dict = dict(c)
            case_dict["team"] = [dict(t) for t in team_assignments if t["surgery_id"] == c["id"]]
            result.append(case_dict)

    if result:
        scope, dept = surgical_scope(user)
        log_surgical_phi_read(
            request=request,
            user=user,
            action_type="surgery_cases_viewed",
            summary_message=(
                f"Active surgical cases viewed ({len(result)}), scope "
                f"{scope}/{dept or '(no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={"result_count": len(result)},
        )
    return result

@router.get("/{surgery_id}")
def get_surgery_detail(surgery_id: str, request: Request,
                       user: dict = Depends(require_permission("surgery.read"))):
    """Get full details for a specific surgery. WARD SCOPED + READ AUDITED.

    THE SCOPE FILTER IS IN THE SAME QUERY as the lookup, not a second round
    trip: an out-of-scope case and a non-existent one therefore take the SAME
    branch and return the SAME 404 with the same detail string. A 403, or a
    different message, would confirm the case — and so the patient — exists.

    This row is the richest PHI payload in the module: name, MRN, age, sex,
    diagnosis, comorbidities AND allergies. The list endpoints above hand out
    surgery_ids, so before scoping no guessing was required to reach it.
    """
    scope_sql, scope_params = surgical_list_filter(user)
    with db_session() as db:
        row = db.execute(
            f"""SELECT s.*, p.name as patient_name, p.mrn, p.age, p.sex, u.name as surgeon_name,
                      p.bed, p.ward, p.admission_date,
                      p.diagnosis as patient_diagnosis, pp.comorbidities, pp.allergies,
                      rs.level as risk_level, rs.score as risk_score, rs.drivers as risk_drivers
               FROM surgeries s
               JOIN patients p ON s.patient_id = p.id
               LEFT JOIN patient_profiles pp ON p.id = pp.patient_id
               LEFT JOIN users u ON s.surgeon_id = u.id
               -- Latest risk row: id breaks an exact timestamp tie. The MAX(timestamp)
               -- equality matched every tied row and listed the case twice.
               LEFT JOIN risk_scores rs ON rs.id = (
                   SELECT rs2.id FROM risk_scores rs2 WHERE rs2.patient_id = p.id
                   ORDER BY rs2.timestamp DESC, rs2.id DESC LIMIT 1
               )
               WHERE s.id = ?{scope_sql}""",
            (surgery_id,) + scope_params
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Surgery not found")

        result = dict(row)
        result["risk_level"] = result.get("risk_level") or "stable"
        # Stored as JSON text; the case view expects the list. The bed, ward and
        # admission date above were never selected, so the case header read
        # "Bed undefined" on every case.
        try:
            drivers = json.loads(result["risk_drivers"]) if result.get("risk_drivers") else []
        except (TypeError, ValueError):
            drivers = []
        result["risk_drivers"] = drivers if isinstance(drivers, list) else []
        # `s.*` already carries started_at/completed_at (migration 27); the
        # derived figure is added here so no consumer has to re-implement the
        # subtraction — and so "not measured" is an explicit null rather than a
        # missing key. None for every case that ran before the columns existed.
        result["turnaround_min"] = turnaround_minutes(
            result.get("started_at"), result.get("completed_at")
        )
        # Audit fix: read comorbidities from the SURGERIES row first (the agentic
        # flow snapshots them onto s.co_morbidities at case creation), and only
        # fall back to the patient_profiles table when the surgery row is empty.
        co_morbidities = []
        surg_co = result.get("co_morbidities")
        if surg_co and str(surg_co).strip() not in ("", "[]", "Reviewing context..."):
            try:
                parsed = json.loads(surg_co)
                co_morbidities = parsed if isinstance(parsed, list) else [str(parsed)]
            except Exception:
                co_morbidities = [c.strip() for c in str(surg_co).split(",") if c.strip()]
        if not co_morbidities and result.get("comorbidities"):
            try:
                co_morbidities = json.loads(result["comorbidities"]) or []
            except Exception:
                co_morbidities = []
        result["co_morbidities"] = co_morbidities

        # Parse allergies (JSON array on patient_profiles.allergies) for the safety
        # banner — surgeons must see drug allergies before an emergency case.
        allergies = []
        raw_al = result.get("allergies")
        if raw_al:
            try:
                parsed = json.loads(raw_al)
                allergies = parsed if isinstance(parsed, list) else [str(parsed)]
            except Exception:
                allergies = [a.strip() for a in str(raw_al).split(",") if a.strip()]
        result["allergies"] = allergies

        for field in ["surgical_checklist", "instruments_required", "staff_required", "possible_complications"]:
            try:
                result[field] = json.loads(result.get(field)) if result.get(field) else []
            except:
                result[field] = []
        # The prep steps the Surgical Suite maps over must be a list. The WHO
        # checklist used to be stored in this column as an object, and the
        # panel crashed on it; it has its own column now (migration 76), and
        # nothing it holds belongs in this payload.
        if not isinstance(result["surgical_checklist"], list):
            result["surgical_checklist"] = []
        result.pop("who_checklist", None)
                
        # Also parse notes for imported_context if it's a JSON package
        try:
            notes_obj = json.loads(result.get("notes", "{}"))
            if isinstance(notes_obj, dict) and "imported_context" in notes_obj:
                result["imported_context"] = notes_obj["imported_context"]
        except:
            pass

    # OUTSIDE the read session on purpose: log_phi_read opens its own writing
    # session, and nesting a writer inside a reader is the self-deadlock shape
    # this module already paid for once (see complete_surgery).
    log_surgical_phi_read(
        request=request,
        user=user,
        action_type="surgery_detail_viewed",
        summary_message=f"Surgical case {surgery_id} viewed",
        patient_id=result.get("patient_id"),
        target_type="surgery",
        target_id=surgery_id,
        metadata={"procedure_name": result.get("procedure_name")},
    )
    return result


# ═══════════════════════════════════════════════════════════════════
# WHO SURGICAL SAFETY CHECKLIST
# ═══════════════════════════════════════════════════════════════════

@router.get("/checklist/template")
def who_checklist_template(user: dict = Depends(require_permission("surgery.read"))):
    """Return the canonical WHO Surgical Safety Checklist template.

    NO SCOPE CHECK AND NO PHI AUDIT, deliberately: this is the static WHO 2009
    template, identical for every patient in every hospital. It names nobody.
    """
    from services.who_checklist import get_template
    return get_template()


@router.get("/{surgery_id}/checklist")
def get_who_checklist(surgery_id: str, request: Request,
                      user: dict = Depends(require_permission("surgery.read"))):
    """Return the WHO checklist completion state for a surgery.

    SCOPED AND AUDITED even though the state itself is item ids and attesting
    user ids rather than demographics: it is the safety record OF A NAMED
    PATIENT'S OPERATION, and answering it at all confirms the case exists. Same
    404, same detail string, for out-of-scope and unknown.
    """
    from services.who_checklist import get_checklist_state
    enforce_surgery_scope(user, surgery_id)
    state = get_checklist_state(surgery_id)
    if state.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Surgery not found")

    with db_session() as db:
        patient_id = surgery_patient_id(db, surgery_id)
    log_surgical_phi_read(
        request=request,
        user=user,
        action_type="surgery_checklist_viewed",
        summary_message=f"WHO surgical checklist viewed for case {surgery_id}",
        patient_id=patient_id,
        target_type="surgery",
        target_id=surgery_id,
        metadata={"progress": state.get("progress")},
    )
    return state


@router.post("/{surgery_id}/checklist")
def set_who_checklist_item(surgery_id: str, payload: dict, request: Request,
                           user: dict = Depends(require_permission("surgery.write"))):
    """Check/uncheck a WHO checklist item. Body: {item_id, checked}."""
    from services.who_checklist import set_item
    # A WRITE on a named patient's OR safety record. Scoped for the same reason
    # the read is: without it any authenticated clinician could attest "site
    # marked" on another ward's operation.
    enforce_surgery_scope(user, surgery_id)
    item_id = payload.get("item_id")
    checked = bool(payload.get("checked", True))
    if not item_id:
        raise HTTPException(status_code=422, detail="item_id is required")
    try:
        state = set_item(surgery_id, item_id, checked, user["id"])
    except ValueError as e:
        # "Surgery not found" is a 404, not a 400 — a bad item id is the 400.
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    # The WHO checklist is the OR's safety record: who attested what, and when.
    # It has to be in the tamper-evident trail, not only in the surgeries row.
    _audit_surgery(request, user, "who_checklist_item_set", surgery_id,
                   f"WHO checklist item {item_id} {'checked' if checked else 'unchecked'} "
                   f"on surgery {surgery_id}",
                   metadata={"item_id": item_id, "checked": checked,
                             "progress": state.get("progress")})
    return state


def _persist_alerts(surgery_id, team):
    """Write the whole team-alert fan-out in one transaction, off the loop.

    Blocking. Always call via run_db(). Module scope, not a closure inside the
    async route, so it cannot quietly drift back onto the event loop.

    The entire loop is ONE executor call, not one per member: each hop off and
    back onto the loop has real cost, and more importantly the DELETE, the
    per-member INSERTs and the final status UPDATE must not interleave with a
    second alert-team POST — awaiting between them would let two callers
    interleave their DELETEs and INSERTs and leave a half-built roster.
    """
    with db_session() as db:
        surgery = db.execute("SELECT * FROM surgeries WHERE id = ?", (surgery_id,)).fetchone()
        if not surgery:
            raise HTTPException(status_code=404, detail="Surgery not found")

        # Validate the whole roster BEFORE deleting the existing one. Previously a
        # member with a missing user_id/role or an id that is not an Orb user hit
        # the NOT NULL / FOREIGN KEY constraint mid-loop and surfaced as a 500 —
        # after the DELETE had already dropped the team that was there.
        clean_team: list[tuple[str, str]] = []
        for idx, member in enumerate(team):
            if not isinstance(member, dict):
                raise HTTPException(status_code=422,
                                    detail=f"team[{idx}] must be an object with user_id and role")
            member_id = str(member.get("user_id") or "").strip()
            role_name = str(member.get("role") or "").strip()
            if not member_id or not role_name:
                raise HTTPException(status_code=422,
                                    detail=f"team[{idx}] requires both user_id and role")
            exists = db.execute("SELECT 1 FROM users WHERE id = ?", (member_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=422,
                                    detail=f"team[{idx}]: no such user '{member_id}'")
            clean_team.append((member_id, role_name))

        dupes = {u for u, _ in clean_team if [m for m, _ in clean_team].count(u) > 1}
        if dupes:
            raise HTTPException(status_code=422,
                                detail=f"Duplicate team member(s) in one alert: {sorted(dupes)}")

        # Get patient's case room
        channel = db.execute(
            "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room' LIMIT 1",
            (surgery["patient_id"],)
        ).fetchone()

        if not channel:
            raise HTTPException(status_code=404, detail="Patient case room not found")

        channel_id = channel["id"]

        db.execute("DELETE FROM surgery_team_assignments WHERE surgery_id = ?", (surgery_id,))

        live_emits: list = []
        for user_id, role_name in clean_team:
            db.execute(
                "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) VALUES (?, ?, ?, 'alerted')",
                (surgery_id, user_id, role_name)
            )

            alert_content = f"SURGICAL ALERT: You have been assigned as {role_name} for an emergency procedure (Source Control)."

            # Send targeted message (custom message_type 'surgical_alert' + we can encode user_id in content or read_by logic)
            # We'll use a JSON payload in content for structured rendering in the frontend.
            alert_json = json.dumps({
                "text": alert_content,
                "target_user_id": user_id,
                "surgery_id": surgery_id,
                "role": role_name
            })

            cur = db.execute(
                """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot, source)
                   VALUES (?, 'system', 'Orb System', 'system', ?, 'surgical_alert', 1, 'system')""",
                (channel_id, alert_json)
            )
            # Read back inside the same session: the row must be materialised
            # into a plain dict here, because the socket emit below runs on the
            # loop after the connection has been returned/closed.
            msg_row = db.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
            live_emits.append((dict(msg_row), user_id))

        db.execute("UPDATE surgeries SET status = 'emergency_ready' WHERE id = ?", (surgery_id,))
        return live_emits, surgery["patient_id"], [u for u, _ in clean_team]


@router.post("/{surgery_id}/alert-team")
async def alert_surgical_team(surgery_id: str, payload: dict, request: Request,
                              user: dict = Depends(require_permission("surgery.write"))):
    """Alerts specific team members via targeted Relay messages."""
    team = payload.get("team", [])
    if not isinstance(team, list) or not team:
        raise HTTPException(status_code=400, detail="Team assignments required")

    # `async def`, so the scope check goes through run_db — the blocking-DB-on-
    # the-loop law (tests/test_event_loop_discipline.py). Same 404 the route
    # already returns for an unknown case, for the same non-disclosure reason.
    if not await run_db(surgery_in_scope_blocking, user, surgery_id):
        raise HTTPException(status_code=404, detail="Surgery not found")

    live_emits, patient_id, alerted_ids = await run_db(_persist_alerts, surgery_id, team)

    # Audit must not run on the loop — log_audit_event writes the hash-chained
    # audit row synchronously.
    await run_db(_audit_surgery, request, user, "surgical_team_alerted", surgery_id,
                 f"Surgical team alerted for {surgery_id}: {len(alerted_ids)} member(s)",
                 patient_id, {"alerted_user_ids": alerted_ids})

    # Deliver each alert LIVE: to the case-room channel and to the targeted user's
    # personal room only — not a department-wide broadcast.
    from ws.realtime import sio
    from services.relay_service import _message_row_to_dict
    for msg_row, target_user_id in live_emits:
        # The history endpoint's serializer: carries `is_bot` (AUTO chip) and
        # `source`, which a hand-built copy left out (2026-10-03).
        emit_msg = _message_row_to_dict(msg_row)
        await sio.emit("message:new", emit_msg, room=msg_row["channel_id"])
        if target_user_id:
            await sio.emit("message:new", emit_msg, room=f"user:{target_user_id}")
    return {"status": "success"}

@router.post("/{surgery_id}/team-response")
def surgical_team_response(surgery_id: str, payload: dict, request: Request,
                           user: dict = Depends(require_permission("surgery.read"))):
    """Handles a confirm or decline from a targeted team member."""
    action = str(payload.get("action") or "").strip().lower()
    # Previously ANY value that was not exactly 'confirm' was recorded as a
    # decline — a typo, a missing field or a client sending {"action":"Confirm"}
    # silently removed a surgeon from an emergency case.
    if action not in ("confirm", "decline"):
        raise HTTPException(status_code=422, detail="action must be 'confirm' or 'decline'")

    # Clause (D) of the surgical rule is what makes this route work for an
    # out-of-department consultant: being ON the roster puts the case in scope,
    # so the person who was alerted can always answer. Anyone else 404s.
    enforce_surgery_scope(user, surgery_id)

    with db_session() as db:
        surgery = db.execute("SELECT * FROM surgeries WHERE id = ?", (surgery_id,)).fetchone()
        if not surgery:
            raise HTTPException(status_code=404, detail="Surgery not found")
            
        assignment = db.execute(
            "SELECT * FROM surgery_team_assignments WHERE surgery_id = ? AND user_id = ?",
            (surgery_id, user["id"])
        ).fetchone()
        
        if not assignment:
            raise HTTPException(status_code=400, detail="User not assigned to this surgery")
            
        new_status = 'confirmed' if action == 'confirm' else 'declined'
        
        db.execute(
            "UPDATE surgery_team_assignments SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, datetime.now(timezone.utc).isoformat(), assignment["id"])
        )
        
        # Post system message reflecting the choice
        channel = db.execute(
            "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room' LIMIT 1",
            (surgery["patient_id"],)
        ).fetchone()
        
        if channel:
            sys_msg = f"SYSTEM: {user['name']} ({assignment['role_name']}) has {new_status} the surgical assignment."
            db.execute(
                """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot, source)
                   VALUES (?, 'system', 'Orb System', 'system', ?, 'system', 1, 'system')""",
                (channel["id"], sys_msg)
            )
            
            # If declined, Sage suggests replacement.
            # DEMO-ONLY: the "replacement" is a hardcoded id pair, not a real
            # availability lookup. It is gated on DEMO_MODE so a live ward never
            # sees Orb assert that a named anaesthetist is available, and the id
            # is existence-checked so a missing seed user cannot turn a decline
            # into a 500 (surgery_team_assignments.user_id is a FK).
            if new_status == 'declined':
                from config import DEMO_MODE
                fallback_user = "anesthetist_2" if (DEMO_MODE and user["id"] == "anesthetist_1") else None
                if fallback_user and not db.execute(
                    "SELECT 1 FROM users WHERE id = ?", (fallback_user,)
                ).fetchone():
                    logger.warning("[Surgery] demo fallback user %s does not exist — no re-route",
                                   fallback_user)
                    fallback_user = None

                if fallback_user:
                    db.execute(
                        "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) VALUES (?, ?, ?, 'alerted')",
                        (surgery_id, fallback_user, assignment["role_name"])
                    )
                    alert_json = json.dumps({
                        "text": f"SAGE SUGGESTION: Re-routing assignment to available {assignment['role_name']}.",
                        "target_user_id": fallback_user,
                        "surgery_id": surgery_id,
                        "role": assignment["role_name"]
                    })
                    db.execute(
                        """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot, source)
                           VALUES (?, 'system', 'Sage Ambient', 'ai', ?, 'surgical_alert', 1, 'ai')""",
                        (channel["id"], alert_json)
                    )

    _audit_surgery(request, user, "surgical_team_response", surgery_id,
                   f"{user.get('name', user['id'])} ({assignment['role_name']}) {new_status} "
                   f"the assignment on surgery {surgery_id}",
                   surgery["patient_id"],
                   {"role_name": assignment["role_name"], "response": new_status})
    return {"status": "success"}

@router.post("/{surgery_id}/start")
def start_surgery(surgery_id: str, request: Request,
                  user: dict = Depends(require_permission("surgery.write"))):
    """Transitions status to 'in_progress' only if all required non-declined roles are confirmed."""
    enforce_surgery_scope(user, surgery_id)
    with db_session() as db:
        # The surgery row was never read here: an unknown id updated zero rows and
        # still returned {"status": "success"}, and a blind UPDATE would also drag
        # a 'completed' case back to 'in_progress'. Read it, and gate on the state
        # machine.
        surgery = db.execute("SELECT * FROM surgeries WHERE id = ?", (surgery_id,)).fetchone()
        if not surgery:
            raise HTTPException(status_code=404, detail="Surgery not found")
        if surgery["status"] == "in_progress":
            return {"status": "already_in_progress", "surgery_id": surgery_id}
        if surgery["status"] not in _STARTABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot start a surgery in status '{surgery['status']}'.",
            )

        assignments = db.execute("SELECT * FROM surgery_team_assignments WHERE surgery_id = ?", (surgery_id,)).fetchall()

        # We need at least one confirmed, and no still-outstanding ones. 'pending'
        # is the schema DEFAULT for surgery_team_assignments (database.py) and was
        # not counted, so a roster written with the default status let the case
        # start with nobody having actually confirmed.
        outstanding = [a for a in assignments if a["status"] in ("alerted", "pending")]
        confirmed = [a for a in assignments if a["status"] == 'confirmed']

        if len(outstanding) > 0 or len(confirmed) == 0:
            raise HTTPException(status_code=400, detail="Cannot start surgery until all assigned team members have confirmed.")

        # started_at is the FIRST half of surgical turnaround (migration 27).
        # COALESCE, not a plain assignment: the guard above already refuses a
        # second start, but if a future path ever re-enters this line the
        # ORIGINAL knife-in time must win — a start time that can be overwritten
        # measures the last button press, not the operation.
        started_at = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE surgeries SET status = 'in_progress', "
            "started_at = COALESCE(started_at, ?) WHERE id = ?",
            (started_at, surgery_id),
        )
        started_at = db.execute(
            "SELECT started_at FROM surgeries WHERE id = ?", (surgery_id,)
        ).fetchone()["started_at"]
        patient_id = surgery["patient_id"]
        previous = surgery["status"]

    _audit_surgery(request, user, "surgery_started", surgery_id,
                   f"Surgery {surgery_id} started ({previous} -> in_progress)",
                   patient_id, {"from_status": previous, "to_status": "in_progress",
                                "confirmed_team": len(confirmed),
                                "started_at": started_at})
    return {"status": "success", "started_at": started_at}

@router.post("/{surgery_id}/complete")
def complete_surgery(surgery_id: str, request: Request,
                     user: dict = Depends(require_permission("surgery.write"))):
    """Marks surgery completed, files the post-op note, and removes the case from the active queue."""
    # Scoped because this WRITES INTO THE PATIENT'S CHART — it saves a post-op
    # scribe note against surgeries.patient_id. Unscoped, any authenticated
    # clinician could author a note in another ward's record.
    enforce_surgery_scope(user, surgery_id)

    checklist_required = _checklist_gate_enabled()

    # immediate=True: this is a read-then-DECIDE-then-write guard, and both
    # things it decides on — the status and the checklist — are writable by
    # other requests (`/start`, `/checklist`) while this block runs. On the
    # default DEFERRED session the SELECT executes in autocommit, so a second
    # completion could read 'in_progress' at the same instant as this one and
    # both would write a post-op note (see db_session's docstring: the eMAR
    # precedent). BEGIN IMMEDIATE takes the write lock before the first read, so
    # the pair of completions serialises and the loser sees 'completed'.
    # OUTERMOST is required and holds: enforce_surgery_scope's session is closed
    # by now, and everything expensive — the note write — happens after the
    # block.
    try:
        with db_session(immediate=True) as db:
            surgery = db.execute("SELECT * FROM surgeries WHERE id = ?", (surgery_id,)).fetchone()
            if not surgery:
                raise HTTPException(status_code=404, detail="Surgery not found")

            # Idempotency: a repeat 'complete' must not write a second post-op note.
            if surgery["status"] == "completed":
                return {"status": "already_completed", "surgery_id": surgery_id}

            # ── GATE 1: the state machine. A case must have been STARTED. ────
            if surgery["status"] not in _COMPLETABLE_STATUSES:
                raise _CompletionRefused(
                    reason="not_started",
                    detail=(f"Cannot complete a surgery in status "
                            f"'{surgery['status']}': the case must be in "
                            f"progress. Start it first."),
                    from_status=surgery["status"],
                    patient_id=surgery["patient_id"],
                )

            # ── GATE 2: the WHO Surgical Safety Checklist (configurable). ────
            # Evaluated from the row THIS transaction already holds — no second
            # session, and no window in which an item could be un-ticked between
            # the check and the UPDATE.
            from services.who_checklist import summarize_state, who_raw
            checklist = summarize_state(who_raw(surgery))
            progress = checklist["progress"]
            if checklist_required and not checklist["completed"]:
                raise _CompletionRefused(
                    reason="checklist_incomplete",
                    detail=(f"WHO Surgical Safety Checklist is "
                            f"{progress['checked']}/{progress['total']} complete, "
                            f"every item must be attested before the case can be "
                            f"closed."),
                    from_status=surgery["status"],
                    patient_id=surgery["patient_id"],
                    metadata={"checklist_progress": progress},
                )

            completed_at = datetime.now(timezone.utc).isoformat()
            db.execute(
                "UPDATE surgeries SET status = 'completed', completed_at = ? WHERE id = ?",
                (completed_at, surgery_id),
            )
            started_at = surgery["started_at"]
            turnaround = turnaround_minutes(started_at, completed_at)

            # Post-op note. Orb must NOT fabricate operative details (blood loss,
            # complications, what was found), so what gets filed is a factual
            # stub naming the procedure, the diagnosis and the surgeon, with the
            # operative account left for the surgeon to write. A DEMO_MODE branch
            # used to file an invented laparotomy account ("EBL 150mL, no
            # complications") on whatever case was closed; it is gone.
            post_op_transcript = f"Surgery {surgery['procedure_name']} marked complete by {user['name']}."
            post_op_note = f"# Post-Operative Note\\n\\n**Procedure:** {surgery['procedure_name']}\\n**Diagnosis:** {surgery['diagnosis']}\\n**Surgeon:** {user['name']}\\n\\n_Operative note pending, to be authored by the operating surgeon._"
            patient_id = surgery["patient_id"]
            previous_status = surgery["status"]
    except _CompletionRefused as refused:
        # AUDITED, and only out here. A refusal is a clinical governance event —
        # "somebody tried to close a case that had not started" is precisely the
        # thing a WHO-checklist policy exists to surface — so it goes on the
        # tamper-evident chain with outcome='denied', exactly like the module's
        # successful transitions. It cannot be written inside the block above:
        # log_audit_event opens its OWN session, and on the unpooled path that is
        # a second connection contending with the BEGIN IMMEDIATE write lock this
        # route holds — the nested-writer stall this module has already paid for
        # once (see the compensation comment below).
        _audit_surgery(
            request, user, "surgery_completion_refused", surgery_id,
            f"Completion of surgery {surgery_id} refused: {refused.detail}",
            refused.patient_id,
            {"from_status": refused.from_status, "refusal_reason": refused.reason,
             "checklist_gate": checklist_required, **refused.metadata},
            outcome="denied",
        )
        raise HTTPException(status_code=409, detail=refused.detail)

    # save_scribe_note opens its OWN db_session; calling it while the status
    # UPDATE above sat uncommitted made the inner INSERT contend with our own
    # write lock (nested-writer self-deadlock, ~30s busy stall per completion).
    # The surgery status commits first; the note write is its own transaction.
    #
    # Splitting them cost the all-or-nothing guarantee, so it is restored here by
    # compensation. Without this, a failing note write left the surgery
    # PERMANENTLY 'completed' with no operative note: the idempotency guard above
    # returns 'already_completed' on every retry, so the note could never be
    # written — a completed operation with no post-op record, unrecoverable
    # through the API. Reverting the status on failure puts the case back where a
    # retry redoes both halves.
    try:
        note_id = save_scribe_note(
            patient_id=patient_id,
            author_id=user["id"],
            raw_transcript=post_op_transcript,
            structured_note=json.dumps({"content": post_op_note}),
            note_format="soap",
            ai_generated=False
        )
    except Exception:
        try:
            with db_session() as db:
                # completed_at goes back to NULL with the status. A case that is
                # not completed must not carry a completion time — leaving it
                # would put a phantom row in every turnaround report, with a
                # completed_at and a status that disagree.
                db.execute(
                    "UPDATE surgeries SET status = ?, completed_at = NULL "
                    "WHERE id = ? AND status = 'completed'",
                    (previous_status, surgery_id),
                )
        except Exception:
            # Compensation itself failed — the case is stranded as 'completed'
            # with no note. Log loudly with everything needed to fix it by hand;
            # do not mask the original error.
            logger.error(
                "[Surgery] Post-op note failed for %s AND status rollback failed — "
                "surgery is 'completed' with NO operative note and needs manual repair",
                surgery_id, exc_info=True,
            )
        raise

    # `/cases` filters `status NOT IN ('completed')`, so the completed surgery
    # already left the active prep queue when the transaction above committed.
    _audit_surgery(request, user, "surgery_completed", surgery_id,
                   f"Surgery {surgery_id} marked complete by {user.get('name', user['id'])}"
                   + (f", turnaround {turnaround} min" if turnaround is not None else ""),
                   patient_id, {"from_status": previous_status, "note_id": note_id,
                                "started_at": started_at, "completed_at": completed_at,
                                "turnaround_min": turnaround,
                                "checklist_progress": progress,
                                "checklist_gate": checklist_required})
    return {"status": "success", "note_id": note_id,
            "started_at": started_at, "completed_at": completed_at,
            "turnaround_min": turnaround}


@router.post("/{surgery_id}/prep")
def prep_for_surgery(surgery_id: str, request: Request,
                     user: dict = Depends(require_permission("surgery.write"))):
    """Trigger a surgical prep package (alerts pharmacy / blood bank / anesthesia via Relay).

    The frontend 'Prep for Surgery' button was a 404 (no route); now wired to the existing
    surgery_service.trigger_surgery_prep orchestrator.

    Scoped because the prep package BROADCASTS the patient's name, MRN,
    procedure and risk level into the Surgery department channel and the case
    room — a PHI disclosure triggered by whoever calls this.
    """
    enforce_surgery_scope(user, surgery_id)
    try:
        result = trigger_surgery_prep(
            surgery_id,
            sender_id=user["id"],
            sender_name=user.get("name", "Clinician"),
            sender_role=user.get("role", "doctor"),
        )
    except ValueError as e:
        # The service raises ValueError("Surgery not found") — that is a 404 for
        # the caller, not the 500 an unhandled ValueError used to produce.
        raise HTTPException(status_code=404, detail=str(e))

    _audit_surgery(request, user, "surgery_prep_triggered", surgery_id,
                   f"Surgical prep package triggered for {surgery_id}",
                   metadata={"result_status": (result or {}).get("status")})
    return {"status": "success", **(result or {})}


@router.post("/emergency/{surgery_id}/acknowledge")
def acknowledge_emergency_surgery(surgery_id: str, request: Request,
                                  user: dict = Depends(require_permission("surgery.write"))):
    """Acknowledge an emergency surgery intake (status -> emergency_acknowledged).

    The frontend Emergency Intake popup 'Acknowledge' button was a 404; now wired to
    surgery_escalation_service.acknowledge_emergency.
    """
    # The service was a blind UPDATE: an unknown id changed nothing and still
    # returned success (so an acknowledgement could be recorded against a case
    # that does not exist), and acknowledging a *completed* case would have
    # dragged it back into the emergency queue.
    enforce_surgery_scope(user, surgery_id)
    with db_session() as db:
        row = db.execute(
            "SELECT patient_id, status FROM surgeries WHERE id = ?", (surgery_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Surgery not found")
    if row["status"] == "emergency_acknowledged":
        return {"status": "already_acknowledged", "surgery_id": surgery_id}
    if row["status"] not in _ACKNOWLEDGEABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Surgery {surgery_id} is in status '{row['status']}' and is not an "
                   f"outstanding emergency intake.",
        )

    acknowledge_emergency(surgery_id)
    _audit_surgery(request, user, "emergency_surgery_acknowledged", surgery_id,
                   f"Emergency surgery {surgery_id} acknowledged by {user.get('name', user['id'])}",
                   row["patient_id"], {"from_status": row["status"]})
    return {"status": "success", "surgery_id": surgery_id}


@router.get("/{surgery_id}/risk")
async def surgery_risk_assessment(surgery_id: str, request: Request,
                                  user: dict = Depends(require_permission("surgery.read"))):
    """Stream the MedGemma surgical risk assessment as SSE — the frontend reads this
    (the route was missing, so the Tier-3 risk panel 404'd).

    THE WIDEST READ IN THE MODULE. `_load_risk_detail` assembles the patient's
    whole chart (services.sage_service._assemble_patient_context: demographics,
    vitals, NEWS2, medications, labs, the signed note, the last imaging read) and
    then sends the clinical summary of it to the model. Scoped and audited
    first — nothing is loaded for a caller who may not read the case.
    """
    # `async def`: BOTH DB touches go through run_db. The scope check is a
    # separate, cheap hop taken BEFORE the expensive multi-table assembly, so an
    # out-of-scope caller never causes their target's chart to be read at all.
    if not await run_db(surgery_in_scope_blocking, user, surgery_id):
        raise HTTPException(status_code=404, detail="Surgery not found")

    # The detail load is done HERE, off the loop, and handed to the generator.
    # generate_risk_assessment() used to open a db_session itself on its first
    # step — and that step runs on the event loop, inside the streaming
    # response. That load fans out to services.sage_service._assemble_patient_context
    # (~8 queries across vitals/meds/labs/notes), so every risk-panel open
    # blocked the whole API for the duration of an encrypted multi-table read.
    detail = await run_db(_load_risk_detail, surgery_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Surgery not found")

    # Fail-closed and BEFORE the stream opens. Once StreamingResponse is
    # returned the status line is already sent, so an audit failure inside the
    # generator could not stop the disclosure — the row has to be durable first.
    await run_db(
        log_surgical_phi_read,
        request=request,
        user=user,
        action_type="surgery_risk_assessment_viewed",
        summary_message=f"Surgical risk assessment opened for case {surgery_id}",
        patient_id=detail.get("patient_id"),
        target_type="surgery",
        target_id=surgery_id,
        metadata={"procedure_name": detail.get("procedure_name")},
    )
    return StreamingResponse(
        generate_risk_assessment(surgery_id, detail=detail),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _load_risk_detail(surgery_id: str):
    """Blocking surgery-detail load for the SSE route. Always call via run_db().

    Takes no user: the caller has ALREADY been authorised for this case by
    surgery_in_scope_blocking above. Authorisation happens once, at the case
    level, which is why get_surgery_detail can assemble the chart unscoped.
    """
    from services.surgery_service import get_surgery_detail as _detail
    return _detail(surgery_id)
