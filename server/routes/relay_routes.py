"""Orb Relay — Messaging Routes"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

from database import db_session, run_db
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from models.message import MessageCreate
from permissions import require_permission
from pydantic import BaseModel
from services.relay_service import (
    ChannelAccessError,
    add_channel_member,
    can_access_channel,
    create_direct_channel,
    get_channel_messages,
    get_channels_for_user,
    get_user_alerts,
    list_channel_members,
    mark_channel_read,
    send_message,
)
from ws.realtime import sio


class ActionTransitionRequest(BaseModel):
    state: str
    version: int
    reason: str | None = None


class AssessmentTransitionRequest(BaseModel):
    """A clinician's verdict on a DRAFTED ASSESSMENT.

    `state` is 'approved' or 'declined'. Neither executes anything: approving
    records that a clinician read the draft and agrees with the reasoning.
    Anything Orb could actually DO is a separate `sage_actions` card with its own
    approval, its own confirm lock and its own drug-safety gate — see the
    transition/confirm_agentic endpoints below. Keeping the two apart is what
    stops "I agree with this assessment" from becoming a blanket authorisation
    for everything it mentions.

    `reason` matters most on a decline: everything else in Orb records what a
    clinician accepted, and the disagreements are the informative rows.
    """

    state: str
    version: int
    reason: str | None = None


class RoomFitResolveRequest(BaseModel):
    """Closing a wrong-room warning.

    `decision`:
      confirmed_correct — the clinician looked and says the message belongs
                          here. This is the OVERRIDE, and it is audited as a
                          clinical event (constraint 6).
      redirected        — post the same text into the room it belongs in. The
                          original is never deleted; it stays, annotated.
    `target_channel_id` is only honoured for `redirected`, and only when the
    sender may post there.
    """

    decision: str
    reason: str | None = None
    target_channel_id: str | None = None


class ChannelMemberRequest(BaseModel):
    """Add someone to a direct conversation. Only a current member may do this."""

    user_id: str


class DirectChannelRequest(BaseModel):
    """Open a direct conversation. The caller is always its first member."""

    user_ids: list[str]
    name: str | None = None


router = APIRouter(prefix="/api/relay", tags=["relay"])

# Every blocking DB body reached from an `async def` in this module lives in a
# module-level PURE SYNC `_…` function and is invoked through database.run_db().
# A `with db_session()` on the event loop stalls the whole API — since encryption
# at rest each page I/O also pays HMAC-SHA512 + AES, and the loop cannot even run
# signal handlers while it waits (the 2026-07-30 wedge). Module level, not nested
# in the handler, so a body cannot quietly close over request state or the loop:
# run_db requires it to open and close its own session inside the worker thread.
#
# The corollary that bites: run_db is a FOUR-thread pool, and db_session's
# connections use the driver default isolation_level="" — a SELECT opens no
# transaction, only the first DML does. So "it's all in one sync function" does
# NOT make a read-then-write atomic; two requests execute the same body in
# parallel and both see the pre-write state. Every read-then-write below is
# therefore resolved IN SQL (optimistic version check, or a compare-and-swap on
# the exact row content that was read), never by trusting the earlier SELECT.


_LIST_CHANNELS_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.get("/channels")
def list_channels(user: dict = _LIST_CHANNELS_USER_DEFAULT):
    """Get all channels accessible to this user."""
    return get_channels_for_user(user["id"], user["role"])


_LIST_ALERTS_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.get("/alerts")
def list_alerts(user: dict = _LIST_ALERTS_USER_DEFAULT):
    """Get targeted active alerts for this user."""
    return get_user_alerts(user["id"], user["role"])


def _messages_before(channel_id: str, before_id: int, limit: int) -> list:
    """The `limit` messages immediately older than message `before_id` in this
    channel, oldest first. SYNC.

    A cursor, not an offset: a case room keeps receiving messages while a
    clinician reads back, and an offset counted from the newest message shifts
    by one with every arrival, repeating or skipping a message at the page
    edge. An anchor from another channel matches nothing."""
    from services.relay_service import _message_row_to_dict

    with db_session() as db:
        anchor = db.execute(
            "SELECT timestamp, id FROM messages WHERE id = ? AND channel_id = ?",
            (before_id, channel_id),
        ).fetchone()
        if anchor is None:
            return []
        rows = db.execute(
            """SELECT * FROM messages
                WHERE channel_id = ?
                  AND (timestamp < ? OR (timestamp = ? AND id < ?))
                ORDER BY timestamp DESC, id DESC
                LIMIT ?""",
            (channel_id, anchor["timestamp"], anchor["timestamp"], anchor["id"], limit),
        ).fetchall()
    return [_message_row_to_dict(r) for r in reversed(rows)]


_GET_MESSAGES_LIMIT_DEFAULT = Query(50, ge=1, le=200)
_GET_MESSAGES_OFFSET_DEFAULT = Query(0, ge=0)
_GET_MESSAGES_BEFORE_ID_DEFAULT = Query(None, ge=1)
_GET_MESSAGES_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.get("/channels/{channel_id}/messages")
def get_messages(
    channel_id: str,
    limit: int = _GET_MESSAGES_LIMIT_DEFAULT,
    offset: int = _GET_MESSAGES_OFFSET_DEFAULT,
    before_id: int | None = _GET_MESSAGES_BEFORE_ID_DEFAULT,
    user: dict = _GET_MESSAGES_USER_DEFAULT,
):
    """Get messages for a channel: the newest `limit`, oldest first, or with
    `before_id` the page of older messages just before that one ("Show earlier
    messages"). A thread used to show its newest 50 and nothing before them."""
    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(status_code=403, detail="Not authorized to access this channel")
    if before_id is not None:
        return _messages_before(channel_id, before_id, limit)
    return get_channel_messages(channel_id, limit, offset)


_MARK_READ_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.post("/channels/{channel_id}/read")
def mark_read(
    channel_id: str,
    user: dict = _MARK_READ_USER_DEFAULT,
):
    """Mark every message in a channel as read by the caller (clears the unread badge)."""
    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(status_code=403, detail="Not authorized to access this channel")
    updated = mark_channel_read(channel_id, user["id"])
    return {"status": "ok", "marked_read": updated}


# ══════════════════════════════════════════════════════════════════════════════
#  DIRECT-MESSAGE MEMBERSHIP
# ══════════════════════════════════════════════════════════════════════════════
# A DM's guest list belongs to the people in it. These three endpoints are the
# only way `channel_members` changes, and every grant lands on the tamper-evident
# audit chain (relay_service._audit_membership, fail-closed).
#
# Plain `def`, not `async def`: they do blocking SQLite work, so FastAPI must
# run them in its threadpool — see the event-loop note at the top of this module
# and tests/test_event_loop_discipline.py.


def _membership_error(exc: ChannelAccessError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


_OPEN_DIRECT_CHANNEL_USER_DEFAULT = Depends(require_permission("relay.send"))


@router.post("/channels/direct")
def open_direct_channel(
    req: DirectChannelRequest,
    user: dict = _OPEN_DIRECT_CHANNEL_USER_DEFAULT,
):
    """Open (or re-open) a direct conversation. The caller is its first member.

    This is the bootstrap for membership: `add_member` below requires an
    existing member, so without a creation path that seeds one, a new DM could
    never have anybody in it.
    """
    try:
        return create_direct_channel(user, req.user_ids, req.name)
    except ChannelAccessError as exc:
        raise _membership_error(exc) from exc


_GET_CHANNEL_MEMBERS_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.get("/channels/{channel_id}/members")
def get_channel_members(
    channel_id: str,
    user: dict = _GET_CHANNEL_MEMBERS_USER_DEFAULT,
):
    """Who is in this channel. Gated on the SAME boundary as its messages."""
    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(status_code=403, detail="Not authorized to access this channel")
    return list_channel_members(channel_id)


_ADD_MEMBER_USER_DEFAULT = Depends(require_permission("relay.send"))


@router.post("/channels/{channel_id}/members")
def add_member(
    channel_id: str,
    req: ChannelMemberRequest,
    user: dict = _ADD_MEMBER_USER_DEFAULT,
):
    """Add someone to a direct conversation.

    The permission dependency is a floor, not the authorisation: `relay.send` is
    held by every clinician. The actual check is "is the CALLER already a member
    of this conversation", made in the service against `channel_members` — so
    holding a permission, or being an administrator, grants nothing here.
    """
    try:
        return add_channel_member(channel_id, user, req.user_id)
    except ChannelAccessError as exc:
        raise _membership_error(exc) from exc


def _ambient_switched_on() -> None:
    """Relay's ambient cards and room memory come from a model reading the
    case room: 404 while the models are switched off (ORB_MODELS_ENABLED, or
    the Orb Ward Record). Declared above the first route that uses it."""
    from services.model_switch import require_models_on

    require_models_on()


_GET_SAGE_MEMORY_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.get("/channels/{channel_id}/sage_memory", dependencies=[Depends(_ambient_switched_on)])
def get_sage_memory(
    channel_id: str,
    user: dict = _GET_SAGE_MEMORY_USER_DEFAULT,
):
    """Get the latest Sage Ambient memory for a patient-linked channel."""
    # Ensure user has access to this channel by checking if they are a member
    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(
            status_code=403, detail="Not authorized to access this channel's memory"
        )

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM sage_room_memory WHERE room_id = ?", (channel_id,)
        ).fetchone()

        if not row:
            return None

        return {
            "room_id": row["room_id"],
            "patient_id": row["patient_id"],
            "patient_summary": row["patient_summary"],
            "discussion_summary": row["discussion_summary"],
            "current_concern": row["current_concern"],
            "likely_next_steps": json.loads(row["likely_next_steps"])
            if row["likely_next_steps"]
            else [],
            "unresolved_questions": json.loads(row["unresolved_questions"])
            if row["unresolved_questions"]
            else [],
            "active_intents": json.loads(row["active_intents"]) if row["active_intents"] else [],
            "risk_flags": json.loads(row["risk_flags"]) if row["risk_flags"] else [],
            "last_processed_at": row["last_processed_at"],
            "updated_at": row["updated_at"],
        }


_GET_SAGE_ACTIONS_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.get("/channels/{channel_id}/sage_actions", dependencies=[Depends(_ambient_switched_on)])
def get_sage_actions(
    channel_id: str,
    user: dict = _GET_SAGE_ACTIONS_USER_DEFAULT,
):
    """Get Sage actions for a channel."""
    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    from services.sage_handoff_service import parse_safety

    with db_session() as db:
        rows = db.execute(
            """SELECT a.*, 
                      h.id as handoff_id, h.status as handoff_status, 
                      h.target_module, h.error_summary, h.downstream_ref_id,
                      h.safety_json as handoff_safety_json
               FROM sage_actions a
               LEFT JOIN sage_handoffs h ON a.id = h.source_action_id AND h.id = (
                   SELECT id FROM sage_handoffs h2 WHERE h2.source_action_id = a.id ORDER BY created_at DESC LIMIT 1
               )
               WHERE a.room_id = ? ORDER BY a.created_at DESC""",
            (channel_id,),
        ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            if d.get("handoff_id"):
                d["handoff"] = {
                    "id": d.pop("handoff_id"),
                    "status": d.pop("handoff_status"),
                    "target_module": d.pop("target_module"),
                    "error_summary": d.pop("error_summary"),
                    "downstream_ref_id": d.pop("downstream_ref_id"),
                    "safety": parse_safety(d.pop("handoff_safety_json")),
                }
            else:
                d.pop("handoff_id", None)
                d.pop("handoff_status", None)
                d.pop("target_module", None)
                d.pop("error_summary", None)
                d.pop("downstream_ref_id", None)
                d.pop("handoff_safety_json", None)
            result.append(d)

        return result


def _transition_action(action_id, channel_id, req, user):
    with db_session() as db:
        return _apply_transition(db, action_id, channel_id, req, user)


def _apply_transition(db, action_id, channel_id, req, user):
    """The state write behind the transition endpoint, on the caller's
    transaction. A confirm joins the handoff to this same transaction
    (_confirm_and_hand_off)."""
    # Check current state and version
    action = db.execute(
        "SELECT state, version FROM sage_actions WHERE id = ? AND room_id = ?",
        (action_id, channel_id),
    ).fetchone()

    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    current_state = action["state"]

    # Valid transition rules
    if current_state in ("confirmed", "dismissed"):
        raise HTTPException(status_code=400, detail="Cannot transition from a resolved state")
    if current_state == "failed" and req.state == "confirmed":
        raise HTTPException(status_code=400, detail="Cannot confirm a failed action")

    # Optimistic locking. This is also what keeps the read above honest: the
    # SELECT holds no snapshot, so the version predicate — not the check —
    # is what stops a parallel transition from being overwritten.
    res = db.execute(
        "UPDATE sage_actions SET state = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND version = ?",
        (req.state, action_id, req.version),
    )

    if res.rowcount == 0:
        raise HTTPException(
            status_code=409, detail="Action was modified concurrently. Please refresh."
        )

    # Log event
    db.execute(
        """INSERT INTO sage_action_events
           (action_id, prior_state, new_state, actor_id, actor_name, actor_role, reason, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'ui')""",
        (action_id, current_state, req.state, user["id"], user["name"], user["role"], req.reason),
    )

    updated_action = db.execute("SELECT * FROM sage_actions WHERE id = ?", (action_id,)).fetchone()
    action_dict = dict(updated_action)

    # ── SHADOW: `action.approved` / `action.declined` ──
    # THE DECLINE IS THE VALUABLE ROW. Everything else in Orb records what
    # the clinician accepted; a dismissal writes a sage_action_events row and
    # nothing else, so "Sage suggested it and a doctor said no" was invisible
    # to every metric. `agreement` counts a decline as a resolved pair that
    # never matches — which is exactly what an honest disagreement rate needs.
    # On THIS transaction (db=db): the optimistic UPDATE above holds the WAL
    # write lock, and only its winner reaches this line.
    _emit_action_resolution(db, action_dict, req, user)
    return action_dict


def _emit_action_resolution(db, action_dict: dict, req, user: dict) -> None:
    """`confirmed` -> approved, `dismissed` -> declined, everything else -> nothing.

    'acknowledged' and 'failed' are NOT resolutions: acknowledged means the
    clinician has seen the card and not yet decided, and a failed dispatch is an
    Orb fault, not a clinician's verdict. Mapping either onto approved/declined
    would put a decision in the record that nobody made.
    """
    if req.state == "confirmed":
        _emit_action_outcome(db, "action.approved", action_dict, user, None)
    elif req.state == "dismissed":
        _emit_action_outcome(db, "action.declined", action_dict, user, req.reason)


def _emit_action_outcome(db, kind: str, action_dict: dict, user: dict, decline_reason) -> None:
    try:
        from services import shadow_service

        patient_id = action_dict.get("patient_id")
        if not patient_id:
            return
        # An assessment card belongs to its originating alert, even when a
        # newer episode has opened before the clinician decides. Ambient chat
        # cards have no assessment link and retain their existing chat path.
        origin = db.execute(
            """SELECT a.episode_id, a.lane
               FROM sage_assessment_actions link
               JOIN sage_assessments a ON a.id = link.assessment_id
               WHERE link.action_id = ? AND a.patient_id = ?
               LIMIT 1""",
            (action_dict.get("id"), patient_id),
        ).fetchone()
        lane = origin["lane"] if origin else "chat"
        episode_id = origin["episode_id"] if origin else None
        if not episode_id:
            episode_id, _ = shadow_service.open_episode(
                patient_id, lane, trigger="sage_ambient_action", db=db
            )
        if not episode_id:
            return
        shadow_service.emit_many(
            [
                shadow_service.build(
                    kind,
                    episode_id,
                    lane,
                    patient_id=patient_id,
                    actor_user_id=user.get("id"),
                    actor_role=user.get("role"),
                    subject_type="sage_actions",
                    subject_id=action_dict.get("id"),
                    payload={
                        "action_class": action_dict.get("action_type"),
                        # `order_id` is left None unless a downstream order really
                        # exists; the handoff that creates one runs AFTER this
                        # transaction commits, and a placeholder here would claim a
                        # clinical record that may never be written.
                        "order_id": None,
                        "decline_reason": (str(decline_reason)[:200] if decline_reason else None),
                    },
                )
            ],
            db=db,
        )
    except Exception:
        # Rule 1: a clinician's decision must land even if the log cannot.
        pass


def _confirm_and_hand_off(action_id, channel_id, req, user):
    """Confirm a card and dispatch it in ONE transaction.

    The confirm used to commit first and the handoff after it, step by step: the
    handoff record, the chart row, the Helix order and the final status each in
    a session of their own. A failure in between left a card reading
    "confirmed" with nothing dispatched and no record, or a pending order in the
    pharmacist's queue behind a handoff stuck at 'pending'. Now the claim
    (version-guarded, as before), the handoff and everything its adapter writes
    commit together. A dispatch that fails or is blocked is still a committed
    outcome on a confirmed card; a fault anywhere else rolls back the confirm
    too, and the card can be confirmed again.

    The drug-safety screen runs first, outside the transaction (it writes its
    own audit rows; see prescreen_handoff). Returns ``(action, handoff,
    surgery_id)``, the last being a newly booked case whose prep package the
    caller starts after the commit.
    """
    from services.sage_handoff_service import dispatch_in_transaction, prescreen_handoff

    with db_session() as db:
        pre = db.execute(
            "SELECT * FROM sage_actions WHERE id = ? AND room_id = ?", (action_id, channel_id)
        ).fetchone()
    # No screen for a card the claim below will refuse anyway: it would only
    # write a screen audit row for an order that cannot happen.
    screened = None
    if pre and pre["state"] not in ("confirmed", "dismissed", "failed"):
        screened = prescreen_handoff(dict(pre), user["id"])

    with db_session() as db:
        action_dict = _apply_transition(db, action_id, channel_id, req, user)
        handoff_dict, package_for = dispatch_in_transaction(db, action_dict, user["id"], screened)
    return action_dict, handoff_dict, package_for


_TRANSITION_SAGE_ACTION_USER_DEFAULT = Depends(require_permission("relay.execute_action"))


@router.post(
    "/channels/{channel_id}/sage_actions/{action_id}/transition",
    dependencies=[Depends(_ambient_switched_on)],
)
async def transition_sage_action(
    channel_id: str,
    action_id: str,
    req: ActionTransitionRequest,
    user: dict = _TRANSITION_SAGE_ACTION_USER_DEFAULT,
):
    """Transition a Sage action's state."""
    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    valid_states = {"suggested", "acknowledged", "dismissed", "confirmed", "failed"}
    if req.state not in valid_states:
        raise HTTPException(status_code=400, detail="Invalid state")

    handoff_dict = None
    if req.state == "confirmed":
        from services.sage_handoff_service import schedule_prep_package

        action_dict, handoff_dict, package_for = await run_db(
            _confirm_and_hand_off, action_id, channel_id, req, user
        )
        # Committed: a newly booked case can have its prep package now, from
        # the loop thread (the only place get_running_loop() works).
        if package_for:
            schedule_prep_package(package_for)

        # If this confirmation created/staged a surgery, deliver the targeted
        # team alerts LIVE (the rows were persisted inside the handoff dispatch).
        if (
            handoff_dict
            and handoff_dict.get("target_module") == "admissions"
            and handoff_dict.get("downstream_ref_id")
        ):
            from services.sage_handoff_service import emit_team_alerts_live

            await emit_team_alerts_live(
                handoff_dict["downstream_ref_id"], action_dict["patient_id"]
            )
    else:
        action_dict = await run_db(_transition_action, action_id, channel_id, req, user)

    await sio.emit("sage:action_update", {"channel_id": channel_id}, room=channel_id)
    return {"status": "ok", "action": action_dict, "handoff": handoff_dict}


# ═════════════════════════════════════════════════════════════════════════════
#  SAGE ASSESSMENTS — the drafted assessment behind a critical Vigil alert
# ═════════════════════════════════════════════════════════════════════════════
# WHY THESE ARE NEW ROUTES AND NOT THE CARD ROUTES ABOVE. The card endpoints are
# keyed to `sage_actions` rows: they read that table, they enforce its state
# machine, and confirmation dispatches a downstream handoff. An assessment is a
# different entity — a four-section document with its own states (including
# `generating` and `unavailable`, which no action card has) and its own table.
# Carrying it on the card routes would mean minting a fake `sage_actions` row
# whose "action" is "read this", which would ALSO land in the scorecard's
# `agreement` metric as a proposal/resolution pair that matches by construction
# — a near-tautological pair inflating the one number that is supposed to show
# whether clinicians actually trust Sage.
#
# What IS reused, completely: every action the assessment proposes is a real
# `sage_actions` card created by sage_assessment_service, so it goes through the
# transition endpoint above, the per-action confirm lock, the medication safety
# gate and the existing shadow pairing. Nothing about execution is duplicated
# here, because nothing here executes.
def _assessment_switched_on() -> None:
    """The alert assessment is a NEWS2 breakdown (Tier 1) and a model's draft
    (Tier 2). With scoring switched off (ORB_SCORING_ENABLED, the Orb Ward
    Record) or the models switched off (ORB_MODELS_ENABLED), its routes answer
    404 here, before any assessment is read or rendered."""
    from services.model_switch import SWITCHED_OFF_DETAIL, models_enabled
    from services.vigil_service import scoring_enabled

    if not scoring_enabled() or not models_enabled():
        raise HTTPException(status_code=404, detail=SWITCHED_OFF_DETAIL)


_GET_SAGE_ASSESSMENTS_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.get(
    "/channels/{channel_id}/sage_assessments", dependencies=[Depends(_assessment_switched_on)]
)
async def get_sage_assessments(
    channel_id: str,
    message_id: int | None = None,
    user: dict = _GET_SAGE_ASSESSMENTS_USER_DEFAULT,
):
    """Drafted assessments for a case room, newest first.

    `message_id` narrows to the assessment for one specific Relay alert, which
    is how the alert surface asks "is there a draft for THIS page?".
    """
    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    from services import sage_assessment_service

    return await run_db(sage_assessment_service.list_for_channel, channel_id, message_id)


_GET_SAGE_ASSESSMENT_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.get("/sage_assessments/{assessment_id}", dependencies=[Depends(_assessment_switched_on)])
async def get_sage_assessment(
    assessment_id: str,
    user: dict = _GET_SAGE_ASSESSMENT_USER_DEFAULT,
):
    """One drafted assessment.

    The access check is on the CHANNEL the assessment belongs to, not on the
    assessment id: the id is a bearer secret otherwise. An assessment with no
    channel (a lane that raised no case room) is refused rather than disclosed —
    there is no membership to check it against.
    """
    from services import sage_assessment_service

    row = await run_db(sage_assessment_service.get_assessment, assessment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if not row.get("channel_id") or not can_access_channel(
        user["id"], user["role"], row["channel_id"]
    ):
        raise HTTPException(status_code=403, detail="Not authorized")
    return sage_assessment_service.render(row)


_TRANSITION_SAGE_ASSESSMENT_USER_DEFAULT = Depends(require_permission("relay.execute_action"))


@router.post(
    "/channels/{channel_id}/sage_assessments/{assessment_id}/transition",
    dependencies=[Depends(_assessment_switched_on)],
)
async def transition_sage_assessment(
    channel_id: str,
    assessment_id: str,
    req: AssessmentTransitionRequest,
    user: dict = _TRANSITION_SAGE_ASSESSMENT_USER_DEFAULT,
):
    """Approve or decline a drafted assessment. EXECUTES NOTHING.

    Same permission as the card routes — the clinicians who may resolve Sage's
    proposals are the clinicians who may resolve Sage's reasoning — and the same
    optimistic-locking convention on `version`.
    """
    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    from services import sage_assessment_service

    assessment = await run_db(
        sage_assessment_service.transition,
        assessment_id,
        channel_id,
        req.state,
        req.version,
        req.reason,
        user,
    )
    await sio.emit(
        "sage:assessment_update",
        {"channel_id": channel_id, "assessment_id": assessment_id, "state": assessment["state"]},
        room=channel_id,
    )
    return {"status": "ok", "assessment": assessment}


_POST_MESSAGE_USER_DEFAULT = Depends(require_permission("relay.send"))
_POST_MESSAGE_IDEMPOTENCY_KEY_DEFAULT = Header(None, alias="Idempotency-Key")


@router.post("/channels/{channel_id}/messages")
async def post_message(
    channel_id: str,
    msg: MessageCreate,
    background_tasks: BackgroundTasks,
    user: dict = _POST_MESSAGE_USER_DEFAULT,
    idempotency_key: str | None = _POST_MESSAGE_IDEMPOTENCY_KEY_DEFAULT,
):
    """Send a message to a channel. Broadcasts live + fires ambient AI analysis.

    Idempotent: an offline-queued message replays with the same Idempotency-Key,
    so a lost-response retry returns the original message instead of double-posting
    (and does NOT re-broadcast or re-run ambient analysis).

    WRONG-ROOM CHECK. The detector runs AFTER the insert and AFTER the
    broadcast, deliberately: a message must never be delayed, refused or lost
    because a heuristic had an opinion (services/room_fit_service — constraint
    1). When it fires, the warning rides back on THIS response, so it is on
    screen at the same instant as the sent message.
    """
    # Keep the authorisation channel list — it is also the sender's PHI
    # visibility boundary, and the wrong-room detector reuses it instead of
    # computing a second, potentially divergent, definition of "can see".
    # Both reads below open their own sessions: through run_db, never on the
    # loop (this module's rule above; every Relay send used to break it).
    visible_channels = await run_db(get_channels_for_user, user["id"], user["role"])
    channel = next((c for c in visible_channels if c["id"] == channel_id), None)
    if channel is None:
        raise HTTPException(status_code=403, detail="Not authorized to access this channel")
    try:
        result = await run_db(
            send_message,
            channel_id=channel_id,
            sender_id=user["id"],
            sender_name=user["name"],
            sender_role=user["role"],
            content=msg.content,
            message_type=msg.message_type,
            idempotency_key=idempotency_key,
        )
    except ChannelAccessError as exc:
        # An Idempotency-Key already owned by ANOTHER channel. The check that
        # raises this is a read boundary, not a validation nicety — see
        # relay_service.send_message. Surfaced as its 409 rather than a 500 so a
        # genuinely confused offline client gets an actionable answer, and a
        # prober gets no message content either way.
        raise _membership_error(exc) from exc

    # On an idempotent replay the message already exists (and was already
    # broadcast + observed on the first attempt) — return it without side effects.
    if result.pop("idempotent_replay", False):
        return result

    # Broadcast to everyone in the channel so other joined users (e.g. a nurse)
    # see the message appear live without refreshing.
    await sio.emit("message:new", result, room=channel_id)

    # Sage Ambient: passive observation (no UI output, independent pipeline).
    # Not while the models are switched off (ORB_MODELS_ENABLED, Ward Record).
    from services.model_switch import models_enabled

    if models_enabled():
        from services.sage_ambient_service import sage_ambient_observe

        background_tasks.add_task(sage_ambient_observe, channel_id, result["id"])

    room_fit = await _run_room_fit(channel, visible_channels, msg, result, user)
    if room_fit:
        result["room_fit"] = room_fit
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  WRONG-ROOM DETECTION — wiring
# ══════════════════════════════════════════════════════════════════════════════


def _room_fit_prepare():
    """Refresh the identity snapshot if it has aged out. SYNC — via run_db.

    Only touches the database when the in-process snapshot is older than its
    TTL (120 s), so the common send pays a dictionary lookup and nothing else.
    """
    from services import room_fit_service as rf

    return rf.refresh_snapshot()


async def _run_room_fit(channel: dict, visible_channels: list, msg, result: dict, user: dict):
    """Run the wrong-room check for a just-persisted message.

    NEVER raises into the send path. Every failure mode here — a stale snapshot,
    an unreachable model, a database that will not take the flag row — degrades
    to "no warning", because the message is already delivered and a 500 on the
    send endpoint would be a far worse outcome than a missed detection.
    """
    try:
        from services import room_fit_service as rf

        if getattr(msg, "message_type", "text") != "text":
            return None
        if not channel.get("patient_id") or channel.get("type") != "case_room":
            return None

        # Warm path takes a mutex and returns a dict — no query, and no hop into
        # the four-thread DB pool either, which matters because that pool is
        # shared with every other write in the app. Only an expired snapshot
        # pays for a thread and a SELECT.
        if rf.snapshot_is_stale():
            snapshot = await run_db(_room_fit_prepare)
        else:
            snapshot = rf.peek_snapshot() or await run_db(_room_fit_prepare)
        room = next(
            (
                p
                for p in rf.build_roster([channel], snapshot)
                if p.patient_id == channel["patient_id"]
            ),
            None,
        )
        if room is None:
            return None
        roster = rf.build_roster(visible_channels, snapshot)

        verdict = rf.check_message(msg.content, room, roster, snapshot.get("staff_tokens", set()))
        if not verdict.fires:
            return None

        if verdict.band == "flag":
            # Deterministic evidence is strong enough on its own — warn now, and
            # let the model try to knock it down in the background.
            payload = await _room_fit_publish(
                channel, room, verdict, result, user, stage="deterministic"
            )
            _spawn_room_fit_adjudication(msg.content, channel, room, verdict, result, user)
            return payload

        # band == "review": real evidence, documented ambiguity. Say NOTHING yet.
        # The 4B decides whether this ever reaches a clinician's eyes.
        _spawn_room_fit_adjudication(msg.content, channel, room, verdict, result, user)
        return None
    except Exception:
        import logging

        logging.getLogger("orb.relay").warning(
            "room_fit check failed for message %s, message already delivered",
            result.get("id"),
            exc_info=True,
        )
        return None


async def _room_fit_publish(channel: dict, room, verdict, result: dict, user: dict, stage: str):
    """Persist the flag, audit it, push it to the room, and return the wire shape."""
    from services.audit_service import log_audit_event

    from services import room_fit_service as rf

    flag_id = await run_db(
        rf.persist_flag,
        result["id"],
        channel["id"],
        channel["patient_id"],
        user["id"],
        verdict,
        stage,
    )
    payload = {
        **verdict.to_public(),
        "flag_id": flag_id,
        "message_id": result["id"],
        "stage": stage,
        "resolution": "open",
    }

    # AUDIT (constraint 6): a warning SHOWN is a clinical event in its own right.
    # 'clinical_access' is fail-closed — deliberately not used here, because an
    # audit failure must not take down messaging; this is an advisory event, and
    # the override that follows is recorded in the same category.
    await run_db(
        log_audit_event,
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_safety",
        action_type="room_fit.warned",
        outcome="success",
        actor_user_id=user["id"],
        actor_role=user["role"],
        target_type="messages",
        target_id=str(result["id"]),
        patient_id=channel["patient_id"],
        summary_message=(
            f"Wrong-room warning shown on message {result['id']} in {channel.get('name')}"
        ),
        metadata={
            "band": verdict.band,
            "stage": stage,
            "score": round(verdict.score, 2),
            "reasons": verdict.reasons,
            "markers": verdict.markers,
            "suppressors": verdict.suppressors,
            "suspected_patient_id": verdict.suspected_patient_id,
            "suggested_channel_id": verdict.suggested_channel_id,
            "ambiguous": verdict.ambiguous,
            "flag_id": flag_id,
        },
    )
    # Room-scoped broadcast, carrying the suspected patient's NAME to everyone in
    # the socket room. A broadcast cannot redact per reader, so what makes it safe
    # is who is allowed into the room at all.
    #
    # CORRECTED 2026-08-05. This comment used to say the emit was safe only
    # because "every clinician can open every case room", and warned that
    # narrowing case-room visibility would require reducing this to a bare
    # {message_id} nudge. Case-room visibility WAS narrowed that day (case rooms
    # are now scoped on admissions.department in relay_service.get_channels_for_user,
    # closing a leak where every clinician could read every case room) — and the
    # emit did NOT need changing, because the premise was wrong.
    #
    # The real guarantee is ws/realtime.py:369 `join_channel`, which admits a
    # socket to this room only after `_user_can_access_channel` → the SAME
    # `get_channels_for_user`. Socket membership therefore narrows automatically
    # and in lockstep with the channel list; there is no second rule to keep in
    # sync. Verified after the scoping change.
    #
    # WHAT WOULD ACTUALLY BREAK THIS: giving sockets a different admission rule
    # from the channel list, or leaving a socket in the room after the caller's
    # access is revoked (rooms persist until leave/disconnect, so a patient
    # transferred between departments mid-session keeps their existing readers
    # until they reconnect). Narrow the LIST and the room follows; bypass the
    # list and it does not.
    await sio.emit("relay:room_fit", payload, room=channel["id"])
    return payload


def _spawn_room_fit_adjudication(content, channel, room, verdict, result, user) -> None:
    """Fire the 4B adjudication as a detached task.

    Deliberately NOT a BackgroundTask: those run after the response is sent but
    still inside the request's lifetime, and a slow ambient tier would hold the
    connection open. Nothing awaits this, and it swallows its own errors, so a
    failure here can never reach the sender. A returned task object is kept only
    long enough for the event loop to hold a strong reference.
    """
    try:
        task = asyncio.create_task(
            _room_fit_adjudicate_later(content, channel, room, verdict, result, user)
        )
        _ROOM_FIT_TASKS.add(task)
        task.add_done_callback(_ROOM_FIT_TASKS.discard)
    except RuntimeError:
        # No running loop (sync test context). The deterministic verdict stands.
        pass


#: asyncio keeps only weak references to bare tasks; without this set a GC pass
#: between two awaits can cancel an adjudication mid-flight.
_ROOM_FIT_TASKS: set = set()


async def _room_fit_adjudicate_later(
    content: str, channel: dict, room, verdict, result: dict, user: dict
):
    """Ambient-tier (4B) adjudication, entirely off the send path.

    PROMOTES a silent `review` into a visible flag. It can never invent a
    warning the deterministic pass did not already reach. The demote direction
    (flag -> cleared) exists but ships OFF — see room_fit_service.MODEL_MAY_DEMOTE
    for the measurement that turned it off.

    MEASURED under live server load: promotion lands tens of seconds after the
    message, not the ~1 s of a standalone call, because the ambient batch
    coordinator queues behind the background LLM lane that the ward narrator and
    Sage Ambient also use. That is acceptable precisely because the flag is
    durable and room-visible rather than a toast the sender might have missed.
    """
    import logging

    log = logging.getLogger("orb.relay")
    try:
        from services import room_fit_service as rf

        adjudication = await rf.adjudicate(
            content, room, verdict.suspected_patient_name, verdict.matched_surface
        )
        final = rf.apply_adjudication(verdict, adjudication)

        if verdict.band == "review" and final.band == "flag":
            await _room_fit_publish(channel, room, final, result, user, stage="model")
        elif verdict.band == "flag" and final.band == "clear":
            # The warning is already on screen. Retract it rather than leave a
            # wolf standing: a warning the system itself no longer believes is
            # the purest form of alert fatigue. The row is kept (auto_cleared),
            # not deleted — a retracted warning is still evidence about the
            # detector's precision.
            cleared = await run_db(
                rf.auto_clear_flag,
                result["id"],
                final.reasons,
            )
            await sio.emit(
                "relay:room_fit_resolved",
                cleared or {"message_id": result["id"], "resolution": "auto_cleared"},
                room=channel["id"],
            )
    except Exception:
        log.debug("room_fit adjudication failed for message %s", result.get("id"), exc_info=True)


_LIST_ROOM_FIT_FLAGS_USER_DEFAULT = Depends(require_permission("relay.read"))


@router.get("/channels/{channel_id}/room_fit")
async def list_room_fit_flags(
    channel_id: str,
    user: dict = _LIST_ROOM_FIT_FLAGS_USER_DEFAULT,
):
    """Wrong-room flags on this channel's messages.

    Room-visible, not sender-only. The person most harmed by wrong-chart
    documentation is whoever reads this thread next week, so the flag — and its
    resolution — travels with the thread.
    """
    # The LIST, not can_access_channel: it is needed twice over — once to
    # authorise, and once below as this reader's PHI boundary for redaction.
    # Same rule either way; can_access_channel is defined as this very check.
    channels = await run_db(get_channels_for_user, user["id"], user["role"])
    if not any(c["id"] == channel_id for c in channels):
        raise HTTPException(status_code=403, detail="Not authorized to access this channel")
    from services import room_fit_service as rf

    flags = await run_db(rf.get_flags_for_channel, channel_id)

    # PER-READER REDACTION. The flag was computed against the SENDER's PHI
    # boundary; the person reading the thread may have a narrower one. Today
    # every case room is visible to every clinician so this is a no-op — but it
    # is written now, while the shape is obvious, rather than being remembered
    # on the day case-room access is scoped to a department. The patient's name
    # is in the message body either way; what gets withheld is the confirmation
    # that they are an admitted patient and the route to their room.
    visible = {c["id"] for c in channels}
    for f in flags:
        if f.get("suggested_channel_id") and f["suggested_channel_id"] not in visible:
            f["suggested_channel_id"] = None
            f["suggested_channel_name"] = None
            f["suspected_patient_id"] = None
            f["suspected_patient_name"] = None
            f["ambiguous"] = True
    return flags


def _room_fit_redirect_txn(flag: dict, target_channel_id: str, user: dict):
    """Copy the flagged text into the room it belongs in. SYNC — via run_db.

    The original message is NOT deleted or edited. Wrong-chart documentation is
    corrected by appending, exactly as the shadow log corrects itself: the
    original stays visible with a 'moved to …' annotation so the reader of this
    thread can see that a foreign fact was entered here and put right.
    """
    from database import db_session
    from services.relay_service import send_message as _send

    with db_session() as db:
        row = db.execute(
            "SELECT content FROM messages WHERE id = ?", (flag["message_id"],)
        ).fetchone()
        if not row:
            return None
        content = row["content"]
    posted = _send(
        channel_id=target_channel_id,
        sender_id=user["id"],
        sender_name=user["name"],
        sender_role=user["role"],
        content=content,
        message_type="text",
    )
    return posted


_RESOLVE_ROOM_FIT_FLAG_USER_DEFAULT = Depends(require_permission("relay.send"))


@router.post("/room_fit/{flag_id}/resolve")
async def resolve_room_fit_flag(
    flag_id: int,
    req: RoomFitResolveRequest,
    user: dict = _RESOLVE_ROOM_FIT_FLAG_USER_DEFAULT,
):
    """Close a wrong-room warning — override it, or act on it.

    Anyone who may post in the room may resolve, not only the original sender:
    the doctor reading the thread an hour later is often the person who actually
    knows which patient the line belongs to.
    """
    if req.decision not in ("confirmed_correct", "redirected"):
        raise HTTPException(
            status_code=400, detail="decision must be confirmed_correct or redirected"
        )

    from services.audit_service import log_audit_event

    from services import room_fit_service as rf

    flag = await run_db(rf.get_flag, flag_id)
    if not flag:
        raise HTTPException(status_code=404, detail="Flag not found")

    # The LIST, not can_access_channel: the redirect check below re-uses it to
    # test the DESTINATION room against the same boundary (line ~725).
    channels = await run_db(get_channels_for_user, user["id"], user["role"])
    if not any(c["id"] == flag["channel_id"] for c in channels):
        raise HTTPException(status_code=403, detail="Not authorized to access this channel")
    if flag["resolution"] != "open":
        raise HTTPException(status_code=409, detail="This warning has already been resolved")

    target = None
    if req.decision == "redirected":
        target = req.target_channel_id or flag["suggested_channel_id"]
        if not target:
            raise HTTPException(status_code=400, detail="No destination room for this warning")
        # Re-check permission at the moment of the move: the suggestion was
        # computed against the SENDER's boundary when the message was sent, and
        # this resolver may be a different person with a different one.
        if not any(c["id"] == target for c in channels):
            raise HTTPException(status_code=403, detail="Not authorized to post in that room")
        if target == flag["channel_id"]:
            raise HTTPException(status_code=400, detail="Destination is the same room")

    # CLAIM THE FLAG FIRST, then move. Ordering matters: two clinicians tapping
    # "Move to …" at the same moment would otherwise both post a copy into the
    # destination and only one would win the compare-and-swap — putting a
    # DUPLICATE clinical message in a second patient's room, which is a worse
    # outcome than the reverse failure (a flag marked redirected whose copy did
    # not land, which is visible and re-doable).
    resolved = await run_db(
        rf.resolve_flag,
        flag_id,
        req.decision,
        user["id"],
        user["name"],
        req.reason,
        None,
    )
    if resolved is None:
        # Lost the compare-and-swap to a concurrent resolver.
        raise HTTPException(status_code=409, detail="This warning has already been resolved")

    redirected_message_id = None
    posted = None
    if req.decision == "redirected":
        posted = await run_db(_room_fit_redirect_txn, flag, target, user)
        if not posted:
            raise HTTPException(status_code=404, detail="Original message no longer exists")
        redirected_message_id = posted["id"]
        resolved = (
            await run_db(rf.link_redirected_message, flag_id, redirected_message_id) or resolved
        )
        await sio.emit("message:new", posted, room=target)

    # AUDIT (constraint 6). An OVERRIDE is the row that matters most: it says a
    # clinician was interrupted, looked, and decided the system was wrong.
    await run_db(
        log_audit_event,
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_safety",
        action_type=(
            "room_fit.overridden" if req.decision == "confirmed_correct" else "room_fit.redirected"
        ),
        outcome="success",
        actor_user_id=user["id"],
        actor_role=user["role"],
        target_type="messages",
        target_id=str(flag["message_id"]),
        patient_id=flag["room_patient_id"],
        summary_message=(f"Wrong-room warning {req.decision} on message {flag['message_id']}"),
        metadata={
            "flag_id": flag_id,
            "band": flag["band"],
            "stage": flag["stage"],
            "decision": req.decision,
            "reason": req.reason,
            "suspected_patient_id": flag["suspected_patient_id"],
            "redirected_to_channel_id": (req.target_channel_id or flag["suggested_channel_id"])
            if req.decision == "redirected"
            else None,
            "redirected_message_id": redirected_message_id,
        },
    )

    await sio.emit("relay:room_fit_resolved", resolved, room=flag["channel_id"])
    return {"status": "ok", "flag": resolved, "redirected_message": posted}


# POST /actions/{message_id}/execute is GONE (removed 2026-10-03). It executed
# legacy ambient_action cards (add, stop or replace a medication, discharge,
# surgery prep) from a message row. No screen has called it since the cards were
# retired, nothing writes new cards, and clients can no longer post a message of
# that type (models/message.MessageCreate), so it was only an unused door onto
# the chart. Legacy rows stay in the table and the readers keep skipping them.


class AlertActionPayload(BaseModel):
    action: str


def _apply_alert_resolution(message_id, payload, user):
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM messages WHERE id = ? AND message_type = 'alert'", (message_id,)
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Alert not found")

        content = json.loads(row["content"])
        if content.get("status") in ["resolved", "escalated"]:
            raise HTTPException(status_code=400, detail="Alert already processed")

        channel_id = row["channel_id"]
        if not can_access_channel(user["id"], user["role"], channel_id):
            raise HTTPException(status_code=403, detail="Not authorized to act on this channel")

        content["status"] = "resolved" if payload.action == "confirm" else "escalated"
        content["resolved_by"] = user["name"]

        # Compare-and-swap on the content we read, not a blind write by id. Two
        # clinicians hitting Resolve and Escalate on the same alert now execute
        # this body on two run_db threads, and the SELECT above holds no
        # snapshot — a blind UPDATE would let the later writer silently replace
        # the other's outcome and resolved_by attribution. The loser gets the
        # same 'already processed' answer it would have got had it arrived a
        # moment later.
        res = db.execute(
            "UPDATE messages SET content = ? WHERE id = ? AND content = ?",
            (json.dumps(content), message_id, row["content"]),
        )
        if res.rowcount == 0:
            raise HTTPException(status_code=400, detail="Alert already processed")

        # ── SHADOW: `alert.acknowledged` (+ an escalation CANDIDATE) ──
        # On THIS transaction (db=db) — the UPDATE above has the WAL write lock
        # and a second connection would deadlock against it (shadow_service's
        # self-deadlock rule). Only the CAS winner reaches this line, so exactly
        # one acknowledgement is recorded per alert no matter how many clinicians
        # raced.
        _emit_alert_acknowledged(db, message_id, content["status"], user)
        return row, content, channel_id


def _emit_alert_acknowledged(db, message_id, disposition: str, user: dict) -> None:
    """The clinician disposed of an Orb alert: resolved it, or escalated care.

    `alert.acknowledged` is the stronger half of engagement — it is what stops
    the sweeper writing `alert.expired`, and unlike a passive `alert.viewed` it
    is a decision.

    The escalation ALSO writes an `outcome.candidate`. Read the direction of that
    row carefully: it is a CANDIDATE, deliberately not an `outcome.recorded`.
    No scorecard metric reads `outcome.candidate` — it exists purely so the human
    doing ground-truth capture has a work queue instead of a blank form, and the
    detection-lead-time numerator still requires a person to confirm the event.
    Auto-promoting "a clinician pressed Escalate on an Orb alert" into confirmed
    ground truth would let Orb supply both halves of its own lead-time metric.
    """
    try:
        from services.relay_service import shadow_alert_episodes

        from services import shadow_service

        info = shadow_alert_episodes(db, [message_id]).get(int(message_id))
        if not info:
            return
        now = shadow_service.utc_now()
        events = [
            shadow_service.build(
                "alert.acknowledged",
                info["episode_id"],
                info["lane"],
                occurred_at=now,
                patient_id=info["patient_id"],
                ward=info["ward"],
                actor_user_id=user.get("id"),
                actor_role=user.get("role"),
                subject_type="messages",
                subject_id=message_id,
                payload={"message_id": message_id, "disposition": disposition},
            )
        ]
        if disposition == "escalated":
            events.append(
                shadow_service.build(
                    "outcome.candidate",
                    info["episode_id"],
                    info["lane"],
                    occurred_at=now,
                    patient_id=info["patient_id"],
                    ward=info["ward"],
                    actor_user_id=user.get("id"),
                    actor_role=user.get("role"),
                    subject_type="messages",
                    subject_id=message_id,
                    payload={
                        "event_type": "unplanned_escalation_of_care",
                        "source": "system",
                        "detected_by": "relay_alert_escalation",
                    },
                )
            )
        shadow_service.emit_many(events, db=db)
    except Exception:
        # Rule 1: acting on an alert must never fail because the log did.
        pass


_RESOLVE_ALERT_USER_DEFAULT = Depends(require_permission("relay.send"))


@router.post("/alerts/{message_id}/resolve")
async def resolve_alert(
    message_id: int,
    payload: AlertActionPayload,
    user: dict = _RESOLVE_ALERT_USER_DEFAULT,
):
    """Resolve or escalate an alert message."""
    row, content, channel_id = await run_db(_apply_alert_resolution, message_id, payload, user)

    # Emit the updated message to the channel. RelayPanel REPLACES the message
    # with this body, so it must be the whole message as the history serves it:
    # without `is_bot` the alert lost its AUTO chip on resolve (2026-10-03).
    from services.relay_service import _message_row_to_dict

    msg_data = {**_message_row_to_dict(row), "content": json.dumps(content)}
    await sio.emit("message:update", msg_data, room=channel_id)

    return {"status": content["status"]}


# ── Agentic Order Confirmation & Pre-Flight Endpoints ──


class DoseConfirmation(BaseModel):
    """Explicit dose/frequency supplied by the confirming clinician. Agentic
    suggestions carry no machine-resolved dose, so dispatch without this would
    silently write a 'TBD' order to Helix."""

    dose: str
    frequency: str
    route: str | None = None


class ConfirmAgenticRequest(BaseModel):
    clinician_pin: str
    # Optional second-clinician attestation. When co_signer_id is supplied it is
    # VERIFIED (distinct active user, own signing PIN, same lockout rules) before
    # it may be written to the tamper-evident audit chain — an unverified name
    # recorded there would be an unrepudiable attestation the named clinician
    # never made.
    co_signer_id: str | None = None
    co_signer_pin: str | None = None
    override_reason: str | None = None
    dose_confirmation: DoseConfirmation | None = None
    # Drug-safety override contract ({"acknowledged": true, "reason": "..."}),
    # required to re-submit after a 428 from the medication safety gate.
    safety_ack: dict | None = None


_PRE_FLIGHT_SAGE_ACTION_USER_DEFAULT = Depends(require_permission("relay.execute_action"))


@router.get(
    "/channels/{channel_id}/sage_actions/{action_id}/pre_flight",
    dependencies=[Depends(_ambient_switched_on)],
)
def pre_flight_sage_action(
    channel_id: str,
    action_id: str,
    user: dict = _PRE_FLIGHT_SAGE_ACTION_USER_DEFAULT,
):
    """Run reconciler, check stock levels, identify if high-risk, and fetch alternatives if out-of-stock."""
    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    with db_session() as db:
        action = db.execute(
            "SELECT * FROM sage_actions WHERE id = ? AND room_id = ?", (action_id, channel_id)
        ).fetchone()

        if not action:
            raise HTTPException(status_code=404, detail="Action not found")

        action_dict = dict(action)

    target_med = action_dict["target"]

    # 1. Phonetic RxNorm/Catalog Reconciliation
    from services.rxnorm_reconciler import reconcile_medication

    matched_med, score = reconcile_medication(target_med)

    resolved_medication = None
    stock_status = None
    alternative_suggestion = None
    is_high_risk = False

    if matched_med:
        resolved_medication = matched_med
        medication_id = matched_med["id"]

        # 2. Inventory check
        from services.helix_service import (
            pre_flight_alternative_suggestion,
            pre_flight_inventory_check,
        )

        stock_status = pre_flight_inventory_check(medication_id)

        # 3. Alternative suggestion if out of stock
        if not stock_status["in_stock"]:
            alternative_suggestion = pre_flight_alternative_suggestion(medication_id)

        # 4. High-risk drug detection
        high_risk_names = {
            "epinephrine",
            "heparin",
            "insulin",
            "potassium",
            "tpa",
            "morphine",
            "fentanyl",
            "oxycodone",
            "hydromorphone",
            "opioid",
        }
        generic_name = (matched_med.get("generic_name", "") or "").lower()
        name_lower = matched_med["name"].lower()
        is_high_risk = any(hr in name_lower or hr in generic_name for hr in high_risk_names)

    return {
        "action": action_dict,
        "resolved_medication": resolved_medication,
        "reconciliation_score": score,
        "stock_status": stock_status,
        "alternative_suggestion": alternative_suggestion,
        "is_high_risk": is_high_risk,
    }


def _load_user_and_action(user, action_id, channel_id):
    with db_session() as db:
        user_row = db.execute(
            "SELECT id, pin_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()

        # Fetch action details
        action = db.execute(
            "SELECT * FROM sage_actions WHERE id = ? AND room_id = ?", (action_id, channel_id)
        ).fetchone()
        return user_row, action


def _reread_pin_hash(user):
    with db_session() as db:
        fresh = db.execute("SELECT pin_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        return fresh


def _load_co_signer(co_signer_id):
    with db_session() as db:
        co_row = db.execute(
            "SELECT id, name, role, pin_hash, is_active FROM users WHERE id = ?",
            (co_signer_id,),
        ).fetchone()
        return co_row


# Two POSTs confirming the SAME agentic action must never both reach Helix.
# Until the DB work moved to run_db() this handler ran straight through on the
# event loop with no await between reading the action's state and writing
# 'confirmed', so it was mutually exclusive for free. Now there are yield points
# in that span, and a double-click would pass the "already resolved" gate twice
# and dispatch two real medication orders. It cannot be fixed with a conditional
# UPDATE the way the alert and ambient-card writes were: the order goes to Helix
# BEFORE the state write, so claiming the action up front would strand it as
# 'confirmed' with no order every time a validation step (dose, stock, safety
# ack) or Helix itself rejects the request. Serialising per action restores
# exactly the old exclusion — the loser then re-reads state='confirmed' and gets
# the same 400 it always would have.
_CONFIRM_LOCKS: dict[str, asyncio.Lock] = {}
_CONFIRM_LOCK_USERS: dict[str, int] = {}


@asynccontextmanager
async def _confirm_lock(action_id: str):
    lock = _CONFIRM_LOCKS.get(action_id)
    if lock is None:
        lock = _CONFIRM_LOCKS[action_id] = asyncio.Lock()
    # Refcounted rather than popped on release: the map must not grow without
    # bound in a worker that runs for weeks, but dropping the entry while another
    # task is still queued on the lock would hand the next arrival a DIFFERENT
    # lock object and silently defeat the exclusion. Every mutation here runs on
    # the loop thread with no await in between, so the count needs no lock itself.
    _CONFIRM_LOCK_USERS[action_id] = _CONFIRM_LOCK_USERS.get(action_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        remaining = _CONFIRM_LOCK_USERS[action_id] - 1
        if remaining:
            _CONFIRM_LOCK_USERS[action_id] = remaining
        else:
            _CONFIRM_LOCK_USERS.pop(action_id, None)
            _CONFIRM_LOCKS.pop(action_id, None)


def _persist_confirmation(
    action_id,
    user,
    delegation_hash,
    correlation_id,
    order_medication_name,
    action_dict,
    is_high_risk,
    co_signer_id,
    co_signer_role,
    req,
    target_med,
    medication_id,
    dose,
    frequency,
    route,
    safety,
    out_of_stock=False,
):
    """Claim the action, place the Helix order, and record both, in ONE
    transaction. Returns the order id.

    One transaction because the order used to be committed first, in a session
    of its own, and the claim below could still refuse: a dismissal or another
    worker's confirm landing during the confirm's awaits got a 409 here and left
    a pending order in the pharmacist queue for an action nobody confirmed, with
    no audit row behind it. A failed audit write orphaned it the same way. Now a
    refusal or any failure rolls back the order with everything else.
    """
    from services.audit_service import log_audit_event
    from services.helix_service import insert_order

    with db_session() as db:
        # Claim the action FIRST, and CONDITIONALLY: nothing else is written
        # unless this confirm is the one that resolves it.
        #
        # _confirm_lock serialises two concurrent CONFIRMS in this process, but
        # it is not the only writer of this column: the transition endpoint above
        # also moves actions to dismissed/confirmed and does NOT take that lock,
        # and a second worker has a lock of its own. The confirm path reads the
        # action's state early and writes it here, with several awaits in between,
        # so a blind UPDATE would let a racing confirm resurrect an action a
        # clinician had just dismissed. The predicate refuses that.
        #
        # version moves too. It is the transition endpoint's only write guard (its
        # state read is outside its transaction), so a confirm that left it alone
        # could be overwritten by a dismissal that read the action just before
        # this commit: a dismissed action with a live order.
        res = db.execute(
            "UPDATE sage_actions SET state = 'confirmed', version = version + 1, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND state NOT IN ('confirmed', 'dismissed')",
            (action_id,),
        )
        if res.rowcount == 0:
            # Resolved by someone else between our read and this write. Nothing
            # has been written yet, and raising rolls back the transaction.
            raise HTTPException(
                status_code=409,
                detail="Action was resolved concurrently. Please refresh.",
            )

        # The Helix order, on this transaction, for the catalogue row the
        # reconciler matched and the safety screen checked (by id, not by name).
        try:
            order = insert_order(
                db,
                patient_id=action_dict["patient_id"],
                medication_id=medication_id,
                dose=dose,
                route=route,
                frequency=frequency,
                prescribed_by=user["id"],
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to create order in Helix backend: {e}"
            ) from e
        order_id = order["id"]

        db.execute(
            """INSERT INTO sage_action_events
               (action_id, prior_state, new_state, actor_id, actor_name, actor_role, reason, source)
               VALUES (?, 'suggested', 'confirmed', ?, ?, ?, ?, 'ui')""",
            (
                action_id,
                user["id"],
                user["name"],
                user["role"],
                f"Confirmed agentic order. Helix Order: {order_id}. Hash Chain: {delegation_hash}",
            ),
        )

        # Audit on the tamper-evident hash chain, atomically with the state
        # writes above (db= joins this transaction). A raw INSERT here would
        # leave the row without prev_hash/row_hash — invisible to
        # database.verify_audit_chain().
        log_audit_event(
            correlation_id=correlation_id,
            event_category="clinical_safety",
            action_type="agentic_med_confirm",
            outcome="success",
            db=db,
            summary_message=f"Agentic order confirmed and dispatched for {order_medication_name}. "
            f"Delegation hash: {delegation_hash}",
            actor_user_id=user["id"],
            actor_role=user["role"],
            target_type="medication_order",
            target_id=order_id,
            patient_id=action_dict["patient_id"],
            metadata={
                "action_id": action_id,
                "order_id": order_id,
                "delegation_hash": delegation_hash,
                "high_risk": is_high_risk,
                # PIN-verified at step 1b — never the raw request value.
                "co_signer_id": co_signer_id,
                "co_signer_role": co_signer_role,
                "override_reason": req.override_reason,
                # Ordered as written with no in-date stock, on the reason above.
                "out_of_stock_override": out_of_stock,
                "original_target": target_med,
                "reconciled_med_id": medication_id,
                "ordered_med_id": order_id,
                "dose": dose,
                "frequency": frequency,
                "route": route,
                "max_severity": safety.get("max_severity"),
                "interaction_screened": safety.get("interaction_screened"),
                # For high-alert meds the ack is required even on a sub-severe
                # screen (the gate only audits override reasons for severe
                # alerts) — record who acknowledged what here.
                "safety_ack_reason": (req.safety_ack or {}).get("reason"),
            },
        )
    return order_id


_CONFIRM_AGENTIC_ACTION_USER_DEFAULT = Depends(require_permission("relay.execute_action"))


@router.post(
    "/channels/{channel_id}/sage_actions/{action_id}/confirm_agentic",
    dependencies=[Depends(_ambient_switched_on)],
)
async def confirm_agentic_action(
    channel_id: str,
    action_id: str,
    req: ConfirmAgenticRequest,
    user: dict = _CONFIRM_AGENTIC_ACTION_USER_DEFAULT,
):
    """Confirm an agentic order. Serialised per action — see _confirm_lock."""
    async with _confirm_lock(action_id):
        return await _confirm_agentic_action(channel_id, action_id, req, user)


async def _confirm_agentic_action(
    channel_id: str,
    action_id: str,
    req: ConfirmAgenticRequest,
    user: dict,
):
    """Verifies clinician PIN, verifies an optional second-clinician co-signature,
    dispatches the Helix order, and logs a cryptographic audit trail.

    High-alert medications dispatch on a SINGLE clinician's PIN plus an explicit
    safety_ack (the 428 contract at step 5) — a co-signer is never required. When
    one is supplied it must prove possession of its own PIN, so the co_signer_id
    sealed into the audit chain is always an attestation that actually happened.
    """
    import hashlib

    from services.audit_service import log_audit_event

    from services import pin_security

    if not can_access_channel(user["id"], user["role"], channel_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    # 1. Verify primary clinician PIN (bcrypt/constant-time, rate-limited).
    if pin_security.is_pin_locked(user["id"]):
        # OFF THE LOOP, but still AWAITED. event_category="security" is one of the
        # fail-closed categories (services/audit_service.py:36), so this row must be
        # durably on the hash chain — awaiting run_db keeps that property while
        # taking the HMAC+AES write off the event loop. See docs/WORKLOG.md §3.
        await run_db(
            log_audit_event,
            correlation_id=str(uuid.uuid4()),
            event_category="security",
            action_type="pin_rate_limited",
            outcome="denied",
            summary_message="Agentic confirmation PIN entry locked after repeated failures.",
            actor_user_id=user["id"],
            actor_role=user["role"],
            target_type="sage_action",
            target_id=action_id,
        )
        raise HTTPException(
            status_code=429,
            detail="Too many incorrect PIN attempts. PIN entry is locked for 15 minutes.",
        )

    user_row, action = await run_db(_load_user_and_action, user, action_id, channel_id)

    if not user_row or not user_row["pin_hash"]:
        # Fail closed with a DISTINCT error: no PIN exists to guess against
        # (universal-PIN seeding was removed; nothing is provisioned by default).
        raise HTTPException(
            status_code=403,
            detail="No signing PIN is provisioned for your account, so agentic confirmation is "
            "unavailable. Ask an administrator to provision one (POST /api/admin/users/{user_id}/pin).",
        )
    # verify_pin is a bcrypt check that may also rewrite a legacy hash: off
    # the loop like every other blocking step in this handler.
    if not await run_db(pin_security.verify_pin, user_row, req.clinician_pin):
        pin_security.record_pin_failure(user["id"])
        await run_db(  # fail-closed security audit, off the loop but awaited
            log_audit_event,
            correlation_id=str(uuid.uuid4()),
            event_category="security",
            action_type="pin_failure",
            outcome="failure",
            summary_message="Invalid clinician PIN on agentic confirmation.",
            actor_user_id=user["id"],
            actor_role=user["role"],
            target_type="sage_action",
            target_id=action_id,
        )
        raise HTTPException(status_code=401, detail="Invalid clinician PIN")
    pin_security.clear_pin_failures(user["id"])
    # Re-read pin_hash AFTER verification: verify_pin upgrades legacy unsalted
    # SHA-256 digests to bcrypt on success, and the pre-upgrade digest of a
    # 4-8 digit PIN is itself offline-brute-forceable — folding it into the
    # persisted delegation hash would leak the PIN just like the raw value.
    fresh = await run_db(_reread_pin_hash, user)
    pin_hash_at_signing = (fresh["pin_hash"] if fresh else None) or user_row["pin_hash"]

    # 1b. Verify the optional co-signer. The confirmed id is written into the
    # tamper-evident audit chain at step 7, so it must be proven here — an
    # unverified caller-supplied id would attribute a second-clinician sign-off
    # to a colleague who never saw the order and could not repudiate it. Fail
    # closed on every branch: a supplied-but-unverifiable co-signer aborts the
    # dispatch rather than silently downgrading to a single signature.
    co_signer_id = (req.co_signer_id or "").strip() or None
    if co_signer_id:
        if co_signer_id == user["id"]:
            raise HTTPException(
                status_code=400,
                detail="A co-signature must come from a different clinician than the confirming one.",
            )
        if pin_security.is_pin_locked(co_signer_id):
            raise HTTPException(
                status_code=429,
                detail="Too many incorrect co-signer PIN attempts. PIN entry is locked for 15 minutes.",
            )
        co_row = await run_db(_load_co_signer, co_signer_id)
        if not co_row or (co_row["is_active"] is not None and not co_row["is_active"]):
            raise HTTPException(status_code=403, detail="Co-signer is not an active user.")
        if not co_row["pin_hash"]:
            raise HTTPException(
                status_code=403,
                detail="No signing PIN is provisioned for the co-signer, so they cannot co-sign. "
                "Ask an administrator to provision one (POST /api/admin/users/{user_id}/pin).",
            )
        if not await run_db(pin_security.verify_pin, co_row, req.co_signer_pin or ""):
            pin_security.record_pin_failure(co_signer_id)
            await run_db(  # fail-closed security audit, off the loop but awaited
                log_audit_event,
                correlation_id=str(uuid.uuid4()),
                event_category="security",
                action_type="pin_failure",
                outcome="failure",
                summary_message="Invalid co-signer PIN on agentic confirmation.",
                actor_user_id=user["id"],
                actor_role=user["role"],
                target_type="sage_action",
                target_id=action_id,
                metadata={"co_signer_id": co_signer_id},
            )
            raise HTTPException(status_code=401, detail="Invalid co-signer PIN")
        pin_security.clear_pin_failures(co_signer_id)
        co_signer_role = co_row["role"]
    else:
        co_signer_role = None

    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    action_dict = dict(action)

    if action_dict["state"] in ("confirmed", "dismissed"):
        raise HTTPException(status_code=400, detail="Action already resolved")

    target_med = action_dict["target"]

    # Reconcile target drug to Helix master (catalogue reads: off the loop)
    from services.rxnorm_reconciler import reconcile_medication

    matched_med, score = await run_db(reconcile_medication, target_med)

    if not matched_med:
        raise HTTPException(
            status_code=400,
            detail=f"Medication '{target_med}' could not be reconciled in Helix Master catalog.",
        )

    medication_id = matched_med["id"]
    medication_name = matched_med["name"]

    # 2. Check if drug is high-risk (high-alert meds require an explicit
    # safety_ack at step 5, on top of the universal drug-safety screen)
    high_risk_names = {
        "epinephrine",
        "heparin",
        "insulin",
        "potassium",
        "tpa",
        "morphine",
        "fentanyl",
        "oxycodone",
        "hydromorphone",
        "opioid",
    }
    generic_name = (matched_med.get("generic_name", "") or "").lower()
    name_lower = matched_med["name"].lower()
    is_high_risk = any(hr in name_lower or hr in generic_name for hr in high_risk_names)

    # 3. Check stock level
    from services.helix_service import (
        pre_flight_alternative_suggestion,
        pre_flight_inventory_check,
    )

    stock = await run_db(pre_flight_inventory_check, medication_id)

    # The order is ALWAYS for the drug the clinician confirmed. On a stockout
    # this used to switch the order silently to whatever the alternative lookup
    # returned (heparin to warfarin, potassium to sodium chloride), carry the
    # original dose across, and record the switch only in a print(). A
    # different drug is a new prescribing decision: the clinician is shown the
    # suggestion and either prescribes it as its own order or confirms this one
    # again with a reason, for pharmacy to source.
    order_medication_name = medication_name
    out_of_stock = not stock["in_stock"]

    if out_of_stock and not (req.override_reason or "").strip():
        alt_med = await run_db(pre_flight_alternative_suggestion, medication_id)
        suggestion = None
        if alt_med:
            suggestion = {
                "id": alt_med["id"],
                "name": alt_med["name"],
                "generic_name": alt_med.get("generic_name"),
                "strength": alt_med.get("strength"),
                "form": alt_med.get("form"),
                "equivalence": alt_med.get("equivalence"),
                "stock_status": alt_med.get("stock_status"),
            }
        raise HTTPException(
            status_code=409,
            detail={
                "error": "out_of_stock",
                "message": (
                    f"{medication_name} has no in-date stock. Nothing was ordered. "
                    + (
                        f"{suggestion['name']} is the same molecule, strength and form and is in stock; "
                        "prescribe it as a new order if you want it. "
                        if suggestion
                        else ""
                    )
                    + f"To order {medication_name} as written for pharmacy to source, "
                    "confirm again with an override reason."
                ),
                "medication": medication_name,
                "alternative_suggestion": suggestion,
            },
        )

    # 4. Dose integrity — the agentic pipeline resolves no dose/frequency, so
    # dispatching without clinician-supplied values would silently write a
    # 'TBD' order into Helix. Require an explicit dose_confirmation.
    dose = "TBD"
    route = "Oral"
    frequency = "TBD"

    import re

    clean_rat = re.sub(r"[^\w\s]", " ", action_dict.get("rationale", "").lower())
    if " iv " in f" {clean_rat} ":
        route = "IV"
    elif " im " in f" {clean_rat} ":
        route = "IM"
    elif " sc " in f" {clean_rat} ":
        route = "SC"

    if req.dose_confirmation:
        dose = (req.dose_confirmation.dose or "").strip()
        frequency = (req.dose_confirmation.frequency or "").strip()
        if req.dose_confirmation.route and req.dose_confirmation.route.strip():
            route = req.dose_confirmation.route.strip()

    if not dose or dose.upper() == "TBD" or not frequency or frequency.upper() == "TBD":
        raise HTTPException(
            status_code=422,
            detail={
                "error": "dose_confirmation_required",
                "message": f"The agentic suggestion for {order_medication_name} carries no resolved "
                "dose/frequency. Supply dose_confirmation "
                '({"dose": "...", "frequency": "..."}): orders are never '
                "dispatched with a TBD dose.",
                "medication": order_medication_name,
            },
        )

    # 5. Drug-safety gate: EVERY agentic dispatch is screened for allergies and
    # interactions; severe alerts require the 428 safety_ack flow. High-alert
    # medications additionally require an explicit acknowledgement even when
    # the screen finds nothing severe.
    # Off the loop: the screen reads the chart (allergies, active medications),
    # and sync SQLite on the event loop stalls every other request.
    from services.medication_safety_gate import enforce_medication_safety

    safety = await run_db(
        enforce_medication_safety,
        action_dict["patient_id"],
        order_medication_name,
        actor_id=user["id"],
        entry_point="relay.confirm_agentic",
        safety_ack=req.safety_ack,
        interactive=True,
        # The confirmed dose, through the same dose guard as every other lane.
        dose=dose,
        route=route,
        actor_role=user.get("role"),
    )
    ack_ok = (
        req.safety_ack is not None
        and bool(req.safety_ack)
        and bool(req.safety_ack.get("acknowledged"))
        and bool((req.safety_ack.get("reason") or "").strip())
    )
    if is_high_risk and not ack_ok:
        raise HTTPException(
            status_code=428,
            detail={
                "error": "drug_safety_acknowledgement_required",
                "message": f"{order_medication_name} is a high-alert medication, agentic dispatch "
                "requires an explicit safety_ack with a reason.",
                "safety": {**safety, "high_risk": True},
            },
        )

    # 6. Create Cryptographic Correlation Hash Chain
    source_msg_id = str(action_dict.get("source_msg_id", "0"))
    llm_prompt_signature = hashlib.sha256(
        f"{target_med}:{action_dict.get('rationale', '')}".encode()
    ).hexdigest()
    # The verifier component hashes the STORED pin hash, never the raw PIN — a
    # 4-8 digit PIN folded into a persisted hash would be offline-brute-forceable.
    verification_hash = hashlib.sha256(f"{user['id']}:{pin_hash_at_signing}".encode()).hexdigest()

    delegation_hash_input = f"{source_msg_id}:{llm_prompt_signature}:{verification_hash}"
    delegation_hash = hashlib.sha256(delegation_hash_input.encode()).hexdigest()

    correlation_id = str(uuid.uuid4())

    # 7. Claim the action and place the Helix order, in one transaction: a
    # confirm that loses a race to a dismissal or another worker leaves no order.
    order_id = await run_db(
        _persist_confirmation,
        action_id,
        user,
        delegation_hash,
        correlation_id,
        order_medication_name,
        action_dict,
        is_high_risk,
        co_signer_id,
        co_signer_role,
        req,
        target_med,
        medication_id,
        dose,
        frequency,
        route,
        safety,
        out_of_stock,
    )

    # 8. WebSocket Notifications
    await sio.emit("sage:action_update", {"channel_id": channel_id}, room=channel_id)
    await sio.emit(
        "patient:timeline_updated",
        {"patient_id": action_dict["patient_id"], "source": "agentic_confirm"},
        room=channel_id,
    )

    return {
        "status": "ok",
        "action_id": action_id,
        "order_id": order_id,
        "correlation_id": correlation_id,
        "delegation_hash": delegation_hash,
        # Sub-severe alerts + interaction-coverage note, for display.
        "safety": safety,
    }
