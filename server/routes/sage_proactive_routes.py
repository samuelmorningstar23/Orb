"""Orb Sage — Proactive surveillance routes (insights feed + one-click into agentic)."""

import uuid

from database import db_session, run_db
from fastapi import APIRouter, Depends, HTTPException, Request
from permissions import department_scope, patient_in_scope, require_permission
from services import facility_service
from services.sage_proactive_service import (
    NON_ACTIONABLE_ACTION_TYPES,
    evaluate_all_admitted,
    evaluate_patient,
    get_active_insights,
    transition_insight,
)
from ws.realtime import sio

router = APIRouter(prefix="/api/sage", tags=["sage-proactive"])


async def _json_object(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    return body


_LIST_INSIGHTS_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.get("/insights")
async def list_insights(user: dict = _LIST_INSIGHTS_USER_DEFAULT):
    # Off the loop: this is a 50-row join over sage_insights x patients, and since
    # encryption at rest every page it touches costs HMAC-SHA512 + AES.
    return {"insights": await run_db(get_active_insights, user["id"], user["role"])}


_INSIGHT_FEEDBACK_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.get("/insights/feedback")
async def insight_feedback(user: dict = _INSIGHT_FEEDBACK_USER_DEFAULT):
    """Per-pattern acceptance rates (acted vs dismissed) + which patterns are
    currently suppressed — the 'is Sage's advice actually useful?' trust surface.
    This is the labelled dataset the app generates every shift, made visible."""
    from services.sage_feedback_service import (
        MIN_SAMPLES,
        SUPPRESS_ACCEPT_FLOOR,
        acceptance_overview,
    )

    return {
        "patterns": await run_db(acceptance_overview),
        "policy": {
            "min_samples": MIN_SAMPLES,
            "suppress_accept_floor": SUPPRESS_ACCEPT_FLOOR,
            "never_suppress": ["critical"],
        },
    }


_TRANSITION_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.post("/insights/{insight_id}/transition")
async def transition(
    insight_id: str, request: Request, user: dict = _TRANSITION_USER_DEFAULT
):
    body = await _json_object(request)
    state = body.get("state", "")
    if not isinstance(state, str):
        raise HTTPException(status_code=400, detail="Invalid state")
    try:
        # One executor call for the whole thing: transition_insight is a
        # read-modify-write (SELECT state → guard → UPDATE → record_feedback) held
        # inside a single db_session, and splitting it would let the guard read a
        # state another request had already changed. Handing over the whole
        # function keeps that sequence exactly as atomic as it was.
        result = await run_db(transition_insight, insight_id, state, user["id"], user["role"])
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not authorized for this insight") from None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid state") from None
    if not result:
        raise HTTPException(status_code=404, detail="Insight not found")
    await sio.emit("sage:insight", {"event": "transition", "id": insight_id})
    return result


def _create_action_card(insight_id, user):
    """Blocking read-modify-write; always call via run_db. Module level (rather
    than nested in the handler) so it is unmistakably a plain sync function —
    nothing in here may touch the loop."""
    with db_session() as db:
        ins = db.execute("SELECT * FROM sage_insights WHERE id = ?", (insight_id,)).fetchone()
        if not ins:
            raise HTTPException(status_code=404, detail="Insight not found")
        if not patient_in_scope(
            db,
            ins["patient_id"],
            department_scope(user),
            facilities=facility_service.facility_scope_for(user, db=db),
        ):
            raise HTTPException(status_code=404, detail="Insight not found")
        if user["role"] != "admin" and ins["target_user_id"] not in (None, user["id"]):
            raise HTTPException(status_code=403, detail="Not your insight")
        if not ins["suggested_action_type"]:
            raise HTTPException(status_code=400, detail="This insight has no suggested action")
        # The server is the gate, not the button: a medication suggestion stored
        # before 2026-10-02 is refused for every caller.
        if ins["suggested_action_type"] in NON_ACTIONABLE_ACTION_TYPES:
            raise HTTPException(
                status_code=400,
                detail="Sage does not draft medication orders from a surveillance insight. Order it from the chart.",
            )
        if ins["state"] != "active":
            raise HTTPException(status_code=409, detail="Insight already handled")
        room = db.execute(
            "SELECT id FROM channels WHERE type = 'case_room' AND patient_id = ? LIMIT 1",
            (ins["patient_id"],),
        ).fetchone()
        if not room:
            raise HTTPException(status_code=404, detail="No case room for this patient")
        channel_id = room["id"]
        action_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'suggested', datetime('now'), datetime('now'))""",
            (
                action_id,
                channel_id,
                ins["patient_id"],
                ins["suggested_action_type"],
                ins["suggested_action_target"] or "",
                f"Drafted from Sage insight: {ins['title']}",
                0.7,
            ),
        )
        db.execute(
            """INSERT INTO sage_action_events (action_id, prior_state, new_state, actor_id, actor_name, actor_role, reason, source, created_at)
               VALUES (?, NULL, 'suggested', ?, ?, ?, 'Drafted from proactive insight', 'system', datetime('now'))""",
            (action_id, user["id"], user.get("name", "Clinician"), user.get("role", "doctor")),
        )
        # Claiming the insight is CONDITIONAL, and the rowcount check is the
        # thing that actually enforces "act once".
        #
        # The `ins["state"] != "active"` check above is necessary but not
        # sufficient: sqlite3 runs with isolation_level='' and so does not open a
        # transaction on a bare SELECT, which means one db_session is not one
        # snapshot. While this ran inline on the event loop that did not matter —
        # the loop is single-threaded, so two /act calls could never overlap.
        # Executing it in run_db's 4-worker pool removed that, and two clicks on
        # the same insight could each read 'active' and each create an action
        # card, putting two identical draft orders in the case room. Making the
        # claim itself the arbiter closes it regardless of scheduling.
        res = db.execute(
            "UPDATE sage_insights SET state = 'acted', updated_at = datetime('now') "
            "WHERE id = ? AND state = 'active'",
            (insight_id,),
        )
        if res.rowcount == 0:
            # Another request claimed it after our SELECT. Raising rolls back the
            # sage_actions / sage_action_events INSERTs above, so the loser leaves
            # nothing behind.
            raise HTTPException(status_code=409, detail="Insight already handled")
        # FEEDBACK LOOP: acting on an insight is the STRONGEST positive signal —
        # record it (this path bypasses transition_insight). Same transaction.
        try:
            from services.sage_feedback_service import pattern_of, record_feedback

            record_feedback(
                pattern_of(ins["insight_type"], ins["severity"]),
                "acted",
                insight_id=insight_id,
                patient_id=ins["patient_id"],
                actor_user_id=user["id"],
                db=db,
            )
        except Exception:
            pass
        return channel_id, action_id


_ACT_ON_INSIGHT_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.post("/insights/{insight_id}/act")
async def act_on_insight(insight_id: str, user: dict = _ACT_ON_INSIGHT_USER_DEFAULT):
    """One-click: turn the insight's suggested action into an agentic Sage action card
    in the patient's case room (Confirm there executes it), then mark the insight 'acted'."""
    # The state=='active' guard and the UPDATE that consumes it stay in one sync
    # call, so no await can slip between them.
    channel_id, action_id = await run_db(_create_action_card, insight_id, user)
    await sio.emit("sage:action_update", {"channel_id": channel_id}, room=channel_id)
    await sio.emit("sage:insight", {"event": "acted", "id": insight_id})
    return {"status": "ok", "action_id": action_id, "channel_id": channel_id}


_EVALUATE_USER_DEFAULT = Depends(require_permission("sage.query"))


@router.post("/insights/evaluate")
async def evaluate(request: Request, user: dict = _EVALUATE_USER_DEFAULT):
    """Run the surveillance sweep now, instead of waiting for the next scheduled
    pass. Optional patient_id, else every admitted patient.

    A real operational trigger, not a test hook: it creates real insights on real
    charts. Used when a clinician has just charted something and wants the sweep
    re-run against it rather than waiting."""
    body = await _json_object(request)
    pid = None
    if "patient_id" in body:
        pid = body["patient_id"]
        if not isinstance(pid, str) or not pid.strip():
            raise HTTPException(status_code=400, detail="patient_id must be a non-empty string")
        pid = pid.strip()
    # The heaviest DB work in this module — a full surveillance sweep over every
    # admitted patient. It ran inline on the loop, which is precisely the shape
    # that wedged the API. One executor call per sweep, not per rule: the dedup
    # guard is already an atomic INSERT ... WHERE NOT EXISTS, so concurrent
    # sweeps still cannot produce duplicate insights.
    # `raised_again`: stood-down cards put back up, same id, pushed all the same.
    raised_again: list = []
    ids = (
        await run_db(_evaluate_scoped_patient, pid, user, raised_again)
        if pid
        else await run_db(evaluate_all_admitted, raised_again, user)
    )
    if ids or raised_again:
        await sio.emit(
            "sage:insight", {"event": "new", "count": len(ids), "raised_again": len(raised_again)}
        )
    return {"created": len(ids), "ids": ids, "raised_again": raised_again}


def _evaluate_scoped_patient(patient_id, user, raised_again):
    with db_session() as db:
        if not patient_in_scope(
            db,
            patient_id,
            department_scope(user),
            facilities=facility_service.facility_scope_for(user, db=db),
        ):
            raise HTTPException(status_code=404, detail="Patient not found")
        if db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Patient not found")
    return evaluate_patient(patient_id, raised_again)
