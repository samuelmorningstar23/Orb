"""Outpatients: the consulting queue (opd-8, W4-F, 4 October 2026).

The front desk registers a visit and issues a token (frontdesk_service). This
module is what happens to the token after that, while the 'opd' feature is on
(ORB_OPD, which also needs ORB_REGISTRATION and the front desk module):

  * the doctor's own queue (list_my_queue), ordered by priority, then token;
  * skip (the patient did not come when called), recall (a skipped patient is
    back in the line at their token), assign (a doctor takes an unassigned
    patient), and the desk's priority and room (update_queue_fields);
  * calling a patient in is frontdesk_service.start_opd_consultation (the
    'call' route is its alias), which in OPD mode also counts the call and
    clears a skip;
  * a content-free nudge, 'opd:queue_changed' {department}, pushed to the
    clinic's socket rooms after every change, so an open doctor queue and
    (opd-9b) an open desk queue refetch over their own scoped REST read
    (nudge_queue_changed). The payload is the department and nothing else:
    the socket never carries a name, a token or a visit id;
  * the desk queue's read audit, once per user per distinct set of visible
    visits per 10 minutes (ruling OPD-N12 (b), claim_queue_read_audit);
  * the waiting-room token board (opd-9, W7-B): per-board device keys and the
    name-free snapshot a board shows (see 'The waiting-room token board').

PRIORITY (ruling OPD-N9, applied): a site list of categories, with a reason
captured for anything other than 'normal', shown on the doctor's queue. The
list is DEFAULT_PRIORITY_CATEGORIES unless the site sets
config.OPD_PRIORITY_CATEGORIES (read at call time with getattr, because
config.py belongs to another cluster this wave; the setting is listed as a
follow-up in the build report). 'emergency' is seen first, every other
non-normal category next, then normal; within a rank, by token. No order is
invented between the social categories.

CANCEL REASON CODES: a cancel in OPD mode may carry one of CANCEL_REASON_CODES.
'left_without_being_seen' is the LWBS code the day summary counts (opd-18). A
cancel never raises a charge: the consultation is charged at completion
(frontdesk_service.complete_opd_visit), and a prepaid registration charge does
not exist until opd-13.

A TAKE CAN CHANGE THE TOKEN. The unassigned line and every doctor's line each
number from 1 per day (frontdesk_service._next_token keys on the doctor), so a
doctor taking an unassigned patient often finds that number already held in
their own line; assign_visit then issues the next free one. The patient's slip
still shows the old number, and in that doctor's line it now belongs to
someone else. So the change is never silent: the take's answer carries
token_changed and previous_token_number, and every OPD-mode visit (the desk's
queue, the doctor's queue, each action's answer) carries previous_token_number
for the rest of the day, read back by reissued_tokens from the take's audit
row, which commits in the same transaction as the new token. For the same
reason the take never lands on a number the doctor's line issued to a visit
later cancelled, and the number it takes out of the unassigned line stays
spent there (taken_unassigned_top, read by _next_token): the desk's next
walk-in is never handed the number on a taken patient's slip.

CONVENTIONS (the same as frontdesk_service): every function is plain sync and
opens its own db_session; writes take db_session(immediate=True) so a
read-then-write cannot interleave; `on_done(db, visit, facts)` runs inside the
transaction on every successful call, a repeat included, so the route's audit
row commits with the change and a repeat writes its own row too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC
from typing import Any

import config
from database import db_session

from services.audit_service import AuditWriteError
from services.frontdesk_service import (
    _CONSULTING_ROLES,
    FrontDeskError,
    _clean,
    _consultation_minutes,
    _hospital_day,
    _next_token,
    _now,
    _serialize_visit,
    _visit_api_dict,
    _waiting_minutes,
    ensure_frontdesk_schema,
    opd_mode,
)

logger = logging.getLogger("orb.opd_queue")

__all__ = [
    "ASSIGN_AUDIT_ACTION",
    "ASSIGN_AUDIT_PREFIX",
    "BOARD_DOCTOR_KEYS",
    "BOARD_KEY_HEADER",
    "BOARD_POLL_SECONDS",
    "BOARD_RESPONSE_KEYS",
    "BoardError",
    "board_for_key",
    "board_snapshot",
    "create_display_board",
    "list_display_boards",
    "revoke_display_board",
    "CANCEL_REASON_CODES",
    "COMPLETION_DISPOSITIONS",
    "DEFAULT_PRIORITY_CATEGORIES",
    "QUEUE_NUDGE_EVENT",
    "QUEUE_READ_AUDIT_WINDOW_SECONDS",
    "QueueConflict",
    "assign_visit",
    "cancel_reason_label",
    "claim_queue_read_audit",
    "queue_read_fingerprint",
    "release_queue_read_audit",
    "reset_queue_read_audit",
    "list_my_queue",
    "nudge_queue_changed",
    "priority_categories",
    "priority_label",
    "priority_rank",
    "queue_fields",
    "queue_nudge_payload",
    "queue_sort_key",
    "recall_visit",
    "reissued_tokens",
    "skip_visit",
    "taken_unassigned_top",
    "update_queue_fields",
]

#: The socket event an open queue listens for. Content-free: see queue_nudge_payload.
QUEUE_NUDGE_EVENT = "opd:queue_changed"

#: The take's audit row: the assign route writes it in the take's transaction
#: with correlation id f"{ASSIGN_AUDIT_PREFIX}-{visit id}", and reissued_tokens
#: reads the token change back from it.
ASSIGN_AUDIT_PREFIX = "opd-assign"
ASSIGN_AUDIT_ACTION = "opd_visit_assigned"

#: (code, label) in the order the desk offers them. 'normal' is always there.
DEFAULT_PRIORITY_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("normal", "Normal"),
    ("emergency", "Emergency"),
    ("senior_citizen", "Senior citizen"),
    ("pregnant", "Pregnant"),
    ("differently_abled", "Differently abled"),
)

#: Why a visit was cancelled, as a code the day summary can count.
CANCEL_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("left_without_being_seen", "Left without being seen"),
    ("no_show", "Did not arrive"),
    ("patient_request", "Patient asked to cancel"),
    ("registered_in_error", "Registered in error"),
    ("other", "Other"),
)

#: How a consultation ends (opd_visits.disposition, migration 50). 'admitted'
#: is written only by the conversion to inpatient (opd-17a), never here.
COMPLETION_DISPOSITIONS: tuple[tuple[str, str], ...] = (
    ("home", "Home"),
    ("follow_up", "Follow-up visit"),
    ("referred_internal", "Referred to another clinic"),
    ("referred_external", "Referred to another hospital"),
)

_MAX_PRIORITY_REASON_LEN = 200
_MAX_ROOM_LEN = 40

_UNSET: Any = object()


class QueueConflict(FrontDeskError):
    """A queue action that the visit's current state refuses (409)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── Vocabularies ─────────────────────────────────────────────────────────────


def _humanise(code: str) -> str:
    words = code.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else code


def priority_categories() -> tuple[tuple[str, str], ...]:
    """The site's priority list as (code, label), 'normal' first.

    config.OPD_PRIORITY_CATEGORIES, when a site sets it, is a list of codes (or
    one comma-separated string); unknown codes get a label made from the code.
    Read at call time so a test or an operator restart sees the current list.
    """
    configured = getattr(config, "OPD_PRIORITY_CATEGORIES", None)
    if not configured:
        return DEFAULT_PRIORITY_CATEGORIES
    if isinstance(configured, str):
        codes = [c.strip().lower() for c in configured.split(",")]
    else:
        codes = [str(c).strip().lower() for c in configured]
    known = dict(DEFAULT_PRIORITY_CATEGORIES)
    out: list[tuple[str, str]] = [("normal", "Normal")]
    for code in codes:
        if code and code != "normal" and code not in dict(out):
            out.append((code, known.get(code, _humanise(code))))
    return tuple(out)


def priority_label(code: str | None) -> str:
    code = (code or "normal").strip().lower() or "normal"
    return (
        dict(priority_categories()).get(code)
        or dict(DEFAULT_PRIORITY_CATEGORIES).get(code)
        or _humanise(code)
    )


def priority_rank(code: str | None) -> int:
    """0 emergency, 1 any other category, 2 normal (or blank)."""
    code = (code or "normal").strip().lower()
    if code == "emergency":
        return 0
    if code in ("", "normal"):
        return 2
    return 1


def cancel_reason_label(code: str | None) -> str | None:
    if not code:
        return None
    return dict(CANCEL_REASON_CODES).get(code) or _humanise(code)


def queue_fields(row) -> dict:
    """The migration 50 queue columns, as OPD mode returns them on a visit.

    Only called while the 'opd' feature is on; with it off the front desk's
    API carries none of these (test_registration_opd_foundation pins that).
    """
    data = dict(row) if row is not None else {}
    priority = (data.get("priority") or "normal").strip().lower() or "normal"
    return {
        "priority": priority,
        "priority_label": priority_label(priority),
        "priority_reason": data.get("priority_reason"),
        "room": data.get("room"),
        "call_count": int(data.get("call_count") or 0),
        "skipped": bool(data.get("skipped_at")),
        "skipped_at": data.get("skipped_at"),
        "cancel_reason_code": data.get("cancel_reason_code"),
        "cancel_reason_label": cancel_reason_label(data.get("cancel_reason_code")),
        "disposition": data.get("disposition"),
        "disposition_at": data.get("disposition_at"),
    }


_REISSUE_CHUNK = 400


def reissued_tokens(db, visit_ids: Iterable[Any]) -> dict[str, int]:
    """{visit id: the token it held before a doctor took it}, for the visits
    whose take issued a new token (see assign_visit). Visits whose token never
    changed are absent.

    opd_visits has no column for it, so it is read back from the take's audit
    row (ASSIGN_AUDIT_PREFIX), which commits in the same transaction as the new
    token: no take changes a token without writing one. A visit is taken at
    most once (assign_visit refuses an assigned visit), and a repeat's row says
    replay, never token_changed, so the change is the one row that says so."""
    ids = [str(v) for v in dict.fromkeys(visit_ids) if v]
    found: dict[str, int] = {}
    for start in range(0, len(ids), _REISSUE_CHUNK):
        chunk = ids[start : start + _REISSUE_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = db.execute(
            f"SELECT target_id, metadata_json FROM audit_logs "
            f"WHERE correlation_id IN ({marks}) AND action_type = ? "
            f"AND target_type = 'opd_visit' ORDER BY id",
            (*[f"{ASSIGN_AUDIT_PREFIX}-{i}" for i in chunk], ASSIGN_AUDIT_ACTION),
        ).fetchall()
        for r in rows:
            try:
                meta = json.loads(r["metadata_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            previous = meta.get("previous_token_number")
            if (
                meta.get("token_changed") is True
                and isinstance(previous, int)
                and not isinstance(previous, bool)
            ):
                found[str(r["target_id"])] = previous
    return found


def _as_token(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def taken_unassigned_top(db, *, visit_date: str, department: str) -> int:
    """The highest token a take moved out of this clinic's unassigned line on
    this day; 0 when no take did.

    frontdesk_service._next_token adds it to the unassigned line's own MAX.
    A taken visit leaves that line (its doctor_id is set), but the slip in the
    patient's hand still shows the number, so the number stays spent for the
    rest of the day exactly as a cancelled visit's does: issuing it again
    would give two people in one clinic the same slip.

    Like reissued_tokens it reads the take's audit row, which commits in the
    take's transaction (assign_visit refuses a take that writes none). Per
    take: unassigned_token_number (recorded on every take since 4 October
    2026); else, on a reissue recorded before that, previous_token_number;
    else the visit's own token, which a take that kept the number still holds
    and which a reissue only ever raised. A replay row spends nothing."""
    rows = db.execute(
        "SELECT v.token_number AS token_number, a.metadata_json AS metadata_json "
        "FROM opd_visits v JOIN audit_logs a "
        "ON a.correlation_id = ? || v.id AND a.action_type = ? "
        "AND a.target_type = 'opd_visit' "
        "WHERE v.visit_date = ? AND v.department = ? AND v.doctor_id IS NOT NULL",
        (f"{ASSIGN_AUDIT_PREFIX}-", ASSIGN_AUDIT_ACTION, visit_date, department),
    ).fetchall()
    top = 0
    for r in rows:
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("replay") is True:
            continue
        number = _as_token(meta.get("unassigned_token_number"))
        if number is None and meta.get("token_changed") is True:
            number = _as_token(meta.get("previous_token_number"))
        if number is None:
            number = _as_token(r["token_number"])
        if number is not None and number > top:
            top = number
    return top


_STATUS_RANK = {"in_consultation": 0, "waiting": 1, "completed": 2, "cancelled": 3}


def queue_sort_key(visit: dict) -> tuple:
    """The seeing order: the patient in the room, then waiting patients by
    priority then token, skipped patients after them, finished visits last."""
    return (
        _STATUS_RANK.get(visit.get("status") or "", 9),
        1 if visit.get("skipped_at") else 0,
        priority_rank(visit.get("priority")),
        int(visit.get("token_number") or 0),
        str(visit.get("id") or ""),
    )


# ── The live nudge ───────────────────────────────────────────────────────────


def queue_nudge_payload(department: str) -> dict:
    """What the socket carries: the clinic, and nothing else (no name, no token,
    no visit id). A doctor's screen refetches its queue over scoped REST."""
    return {"department": department}


async def _emit_queue_changed(department: str) -> None:
    from ws import realtime

    payload = queue_nudge_payload(department)
    # The rooms the socket layer keeps: a vitals.read holder joins
    # 'dept:<their department>' (or 'dept:__all__' with none), which is every
    # doctor; a registration.read holder joins 'opd:<their department>' (or
    # 'opd:__all__') while the feature is on (opd-9b), which is every desk
    # account. ONE emit to the list, so a socket in two of the rooms (a doctor
    # holds both permissions) is told once.
    await realtime.sio.emit(QUEUE_NUDGE_EVENT, payload, room=realtime.queue_nudge_rooms(department))


def nudge_queue_changed(department: str | None) -> bool:
    """Tell open queues in this clinic to refetch. After the change committed;
    never raises; does nothing while the 'opd' feature is off."""
    dept = (department or "").strip()
    if not dept or not opd_mode():
        return False
    try:
        from ws import realtime

        return realtime.schedule_on_loop(
            lambda: _emit_queue_changed(dept), f"{QUEUE_NUDGE_EVENT} for {dept}"
        )
    except Exception as exc:  # noqa: BLE001 - a nudge must never fail a write
        logger.warning("%s not pushed: %s", QUEUE_NUDGE_EVENT, exc)
        return False


# ── The desk queue's read audit, once per unchanged view (ruling OPD-N12) ───
# OPD-N12 (b), the architect's recommendation, applied while the 'opd' feature
# is on: an unchanged desk queue re-read writes no new phi_access row. One row
# per user per DISTINCT SET OF VISIBLE VISITS per 10 minutes, with the visit
# ids in its metadata. The disclosure of a re-read is identical (the same
# person saw the same visits), and a polling desk wrote about 1,200 rows per
# screen per day, while audit already drives appliance storage. A change to
# the visible set (a new walk-in, a visit gone) is a new disclosure and is
# written at once. With the feature off every non-empty read is audited, as
# it always was.
#
# Held in this process, not in the database: a restart forgets it, which only
# ever writes one row more, never one fewer. A key is claimed before the
# audit write and released if the write fails, so a refused write never
# silences the next read.
QUEUE_READ_AUDIT_WINDOW_SECONDS = 600
_QUEUE_READ_AUDIT_MAX_KEYS = 5000
_queue_read_audited: dict[tuple[str, str], float] = {}
_queue_read_lock = threading.Lock()


def queue_read_fingerprint(visit_ids: Iterable[Any]) -> str:
    """A stable name for a set of visit ids (order and repeats ignored)."""
    ids = sorted({str(v) for v in visit_ids if v})
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def claim_queue_read_audit(
    user_id: Any, visit_ids: Iterable[Any], *, now: float | None = None
) -> tuple | None:
    """Should this queue read write its phi_access row? Returns a claim (pass
    it to release_queue_read_audit if the write then fails) when it should,
    None when the same user was audited for the same visits less than
    QUEUE_READ_AUDIT_WINDOW_SECONDS ago. A read with no user id is always
    audited."""
    stamp = time.monotonic() if now is None else float(now)
    uid = str(user_id or "").strip()
    if not uid:
        return ("", "", stamp)
    key = (uid, queue_read_fingerprint(visit_ids))
    with _queue_read_lock:
        last = _queue_read_audited.get(key)
        if last is not None and stamp - last < QUEUE_READ_AUDIT_WINDOW_SECONDS:
            return None
        if len(_queue_read_audited) >= _QUEUE_READ_AUDIT_MAX_KEYS:
            cutoff = stamp - QUEUE_READ_AUDIT_WINDOW_SECONDS
            for stale in [k for k, t in _queue_read_audited.items() if t <= cutoff]:
                del _queue_read_audited[stale]
            if len(_queue_read_audited) >= _QUEUE_READ_AUDIT_MAX_KEYS:
                # Every key is live: forget the oldest half. That can only
                # cause a row to be written again, never a row to be skipped.
                for old in sorted(_queue_read_audited, key=_queue_read_audited.__getitem__)[
                    : _QUEUE_READ_AUDIT_MAX_KEYS // 2
                ]:
                    del _queue_read_audited[old]
        _queue_read_audited[key] = stamp
    return (key[0], key[1], stamp)


def release_queue_read_audit(claim: tuple | None) -> None:
    """Undo a claim whose audit write failed, so the next read writes it."""
    if not claim or not claim[0]:
        return
    key = (claim[0], claim[1])
    with _queue_read_lock:
        if _queue_read_audited.get(key) == claim[2]:
            del _queue_read_audited[key]


def reset_queue_read_audit() -> None:
    """Forget every claim (tests, and nothing else)."""
    with _queue_read_lock:
        _queue_read_audited.clear()


# ── Queue actions ────────────────────────────────────────────────────────────


def _load(db: Any, visit_id: str, *, actor_id: str | None) -> Any:
    """The visit row, or FrontDeskError when there is none. With more than
    one facility on record (multisite-8h1) a visit at a facility outside the
    acting account's scope reads exactly like an unknown one, whatever the
    route checked before: the queue never acts on another facility's
    patient. With one facility nothing more is read."""
    from services import facility_lanes

    row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
    if row is None:
        raise FrontDeskError(f"No such OPD visit: {visit_id}")
    facilities = facility_lanes.account_scope_on(db, actor_id)
    if facilities is not None:
        at = row["facility_id"] if "facility_id" in row.keys() else 1
        if facility_lanes.outside(at, facilities):
            raise FrontDeskError(f"No such OPD visit: {visit_id}")
    return row


def _reload(db, visit_id: str) -> dict:
    return _serialize_visit(
        db, db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
    )


def _finish(db, visit_id: str, on_done, facts: dict) -> dict:
    visit = _reload(db, visit_id)
    if on_done is not None:
        on_done(db, visit, facts)
    return visit


def skip_visit(
    visit_id: str,
    *,
    actor_id: str | None = None,
    on_done: Callable[[Any, dict, dict], None] | None = None,
) -> dict:
    """The patient did not come when called: back to waiting, marked skipped.

    From 'in_consultation' the call is undone (called_at cleared, so the
    waiting time keeps counting; call_count keeps the history). From
    'waiting' the visit is set aside. A repeat on a skipped visit changes
    nothing and is reported as a replay.
    """
    ensure_frontdesk_schema()
    with db_session(immediate=True) as db:
        row = _load(db, visit_id, actor_id=actor_id)
        status = row["status"]
        if status not in ("waiting", "in_consultation"):
            raise QueueConflict(
                "visit_closed", f"Visit {visit_id} is {status} and cannot be skipped"
            )
        if status == "waiting" and row["skipped_at"]:
            return _finish(db, visit_id, on_done, {"replay": True})
        db.execute(
            "UPDATE opd_visits SET status = 'waiting', skipped_at = ?, called_at = NULL "
            "WHERE id = ?",
            (_now(), visit_id),
        )
        logger.info("OPD visit %s skipped by %s", visit_id, actor_id)
        return _finish(db, visit_id, on_done, {"replay": False, "from_status": status})


def recall_visit(
    visit_id: str,
    *,
    actor_id: str | None = None,
    on_done: Callable[[Any, dict, dict], None] | None = None,
) -> dict:
    """A skipped patient is back: they rejoin the line at their own token.

    It does not call them in (that is the call action). A visit that is
    waiting and not skipped is a replay.
    """
    ensure_frontdesk_schema()
    with db_session(immediate=True) as db:
        row = _load(db, visit_id, actor_id=actor_id)
        if row["status"] != "waiting":
            raise QueueConflict(
                "not_waiting", f"Visit {visit_id} is {row['status']} and cannot be recalled"
            )
        if not row["skipped_at"]:
            return _finish(db, visit_id, on_done, {"replay": True})
        db.execute("UPDATE opd_visits SET skipped_at = NULL WHERE id = ?", (visit_id,))
        logger.info("OPD visit %s recalled by %s", visit_id, actor_id)
        return _finish(db, visit_id, on_done, {"replay": False})


def assign_visit(
    visit_id: str,
    *,
    doctor_id: str,
    on_done: Callable[[Any, dict, dict], None] | None = None,
) -> dict:
    """A doctor takes an unassigned waiting patient of their own clinic.

    The doctor must be an active consulting doctor of the visit's clinic (the
    rule register_opd_visit applies). Tokens are numbered per doctor per day;
    the patient keeps their token unless the doctor's series has already
    issued that number today, in which case the next free token is issued.
    A cancelled or left-without-being-seen visit's number counts as issued,
    as it does in frontdesk_service._next_token: two people are never called
    by one number the same morning.

    The answer says which: token_changed (this call issued a new token) and
    previous_token_number (the token on the patient's slip, when it is no
    longer theirs; None otherwise). The facts, and so the audit row, say the
    same, and a repeat answers previous_token_number again from that row, so
    the screen that took the patient can tell the doctor and the desk.

    THE SLIP'S NUMBER STAYS SPENT in the unassigned line. The take moves the
    visit out of that line, and the line's next token is its own MAX plus 1,
    so without more the desk's next walk-in would be handed the number on
    this patient's slip. Every take's facts carry unassigned_token_number,
    which taken_unassigned_top reads back from the audit row for _next_token.
    `on_done` must therefore write that row (ASSIGN_AUDIT_PREFIX,
    ASSIGN_AUDIT_ACTION) in this transaction: a take that leaves the number
    unspent is refused with AuditWriteError and rolls back.
    """
    ensure_frontdesk_schema()
    doctor = _clean(doctor_id, 120)
    if not doctor:
        raise FrontDeskError("A doctor is required")
    with db_session(immediate=True) as db:
        row = _load(db, visit_id, actor_id=doctor)
        if row["status"] != "waiting":
            raise QueueConflict(
                "not_waiting", f"Visit {visit_id} is {row['status']} and cannot be reassigned"
            )
        current = (row["doctor_id"] or "").strip()
        if current == doctor:
            visit = _finish(db, visit_id, on_done, {"replay": True})
            visit["token_changed"] = False
            visit["previous_token_number"] = reissued_tokens(db, [visit_id]).get(visit_id)
            return visit
        if current:
            raise QueueConflict(
                "already_assigned", "This patient is already in another doctor's queue"
            )
        drow = db.execute(
            "SELECT name, role, TRIM(COALESCE(department, '')) AS department, "
            "COALESCE(is_active, 1) AS active FROM users WHERE id = ?",
            (doctor,),
        ).fetchone()
        clinic = (row["department"] or "").strip()
        if (
            drow is None
            or drow["role"] not in _CONSULTING_ROLES
            or not drow["active"]
            or drow["department"].casefold() != clinic.casefold()
        ):
            raise QueueConflict(
                "not_this_clinic", f"Only a consulting doctor of {clinic} can take this patient"
            )
        token = slip = int(row["token_number"])
        # Every visit of the doctor's line today, cancelled ones included (the
        # rule _next_token keeps), not only the live ones the unique index covers.
        clash = db.execute(
            "SELECT 1 FROM opd_visits WHERE visit_date = ? AND department = ? "
            "AND doctor_id = ? AND token_number = ? AND id != ?",
            (row["visit_date"], row["department"], doctor, token, visit_id),
        ).fetchone()
        facts: dict = {"replay": False, "token_changed": False, "unassigned_token_number": slip}
        if clash is not None:
            new_token = _next_token(
                db, visit_date=row["visit_date"], department=row["department"], doctor_id=doctor
            )
            facts.update(token_changed=True, previous_token_number=token, token_number=new_token)
            token = new_token
        db.execute(
            "UPDATE opd_visits SET doctor_id = ?, doctor_name = ?, token_number = ? WHERE id = ?",
            (doctor, drow["name"], token, visit_id),
        )
        logger.info("OPD visit %s assigned to %s", visit_id, doctor)
        visit = _finish(db, visit_id, on_done, facts)
        if (
            taken_unassigned_top(db, visit_date=row["visit_date"], department=row["department"])
            < slip
        ):
            raise AuditWriteError(
                f"The take of OPD visit {visit_id} wrote no audit row, so token {slip} "
                "could be issued again in the unassigned line; nothing was changed"
            )
        visit["token_changed"] = facts["token_changed"]
        visit["previous_token_number"] = facts.get("previous_token_number")
        return visit


def update_queue_fields(
    visit_id: str,
    *,
    priority: Any = _UNSET,
    priority_reason: Any = _UNSET,
    room: Any = _UNSET,
    actor_id: str | None = None,
    on_done: Callable[[Any, dict, dict], None] | None = None,
) -> dict:
    """Set a waiting or in-consultation visit's priority and room.

    A priority other than 'normal' needs a reason (ruling OPD-N9); setting
    'normal' clears the reason. An empty room clears it. Only the fields
    passed change; a call that changes nothing is a replay.
    """
    ensure_frontdesk_schema()
    with db_session(immediate=True) as db:
        row = _load(db, visit_id, actor_id=actor_id)
        if row["status"] not in ("waiting", "in_consultation"):
            raise QueueConflict(
                "visit_closed", f"Visit {visit_id} is {row['status']} and cannot be changed"
            )
        changes: dict[str, Any] = {}
        if priority is not _UNSET:
            code = (str(priority or "").strip().lower()) or "normal"
            if code not in dict(priority_categories()):
                raise FrontDeskError(f"{priority!r} is not one of this site's queue priorities")
            reason = _clean(
                priority_reason if priority_reason is not _UNSET else row["priority_reason"],
                _MAX_PRIORITY_REASON_LEN,
            )
            if code == "normal":
                reason = None
            elif not reason:
                raise FrontDeskError("Give a reason for a priority other than normal")
            changes["priority"] = code
            changes["priority_reason"] = reason
        elif priority_reason is not _UNSET:
            raise FrontDeskError("A priority reason is set together with the priority")
        if room is not _UNSET:
            changes["room"] = _clean(room, _MAX_ROOM_LEN)
        changed = {k: v for k, v in changes.items() if (row[k] if k in row.keys() else None) != v}
        if not changed:
            return _finish(db, visit_id, on_done, {"replay": True})
        sets = ", ".join(f"{k} = ?" for k in changed)
        db.execute(f"UPDATE opd_visits SET {sets} WHERE id = ?", (*changed.values(), visit_id))
        logger.info("OPD visit %s queue fields %s set by %s", visit_id, sorted(changed), actor_id)
        return _finish(db, visit_id, on_done, {"replay": False, "changed": sorted(changed)})


# ── The doctor's own queue ───────────────────────────────────────────────────


def _consultations_table(db) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'opd_consultations'"
        ).fetchone()
        is not None
    )


def list_my_queue(
    *,
    doctor_id: str,
    department: str | None,
    visit_date: str | None = None,
    facilities: frozenset[int] | None = None,
) -> dict:
    """Today's queue for one doctor, plus the clinic's unassigned patients.

    `department` is permissions.department_scope(user), never a value the
    request named; None (an account with no department) sees every clinic's
    unassigned patients, as the desk queue does. Each visit carries what the
    queue needs (name, UHID, age, sex, token, complaint, priority, room, wait)
    and whether its consultation note is a draft or signed. Never vitals, a
    score or a band.

    `facilities` is the caller's permissions.facility_scope (multisite-8h1):
    None leaves the read exactly as it was; a set keeps only visits at those
    facilities (the empty set keeps none), so a doctor with no department
    sees the unassigned line of their own facility's clinics only.
    """
    ensure_frontdesk_schema()
    day = _hospital_day(visit_date)
    from datetime import datetime

    now = datetime.now(UTC)
    clauses = [
        "v.visit_date = ?",
        "v.status IN ('waiting', 'in_consultation', 'completed')",
        "(v.doctor_id = ? OR v.doctor_id IS NULL)",
    ]
    params: list[Any] = [day, doctor_id]
    if department is not None:
        clauses.append("v.department = ?")
        params.append(department)
    if facilities is not None:
        from services.facility_lanes import facility_marks

        marks, ids = facility_marks(facilities)
        clauses.append(f"v.facility_id IN ({marks})")
        params.extend(ids)
    with db_session() as db:
        join = ""
        note_col = "NULL AS consultation_status"
        if _consultations_table(db):
            join = "LEFT JOIN opd_consultations c ON c.visit_id = v.id"
            note_col = "c.status AS consultation_status"
        rows = db.execute(
            f"""SELECT v.*, p.name AS patient_name, p.mrn AS patient_mrn,
                       p.age AS patient_age, p.sex AS patient_sex, {note_col}
                  FROM opd_visits v
                  LEFT JOIN patients p ON p.id = v.patient_id
                  {join}
                 WHERE {" AND ".join(clauses)}""",
            params,
        ).fetchall()
        # A taken patient whose slip shows an old token (see assign_visit).
        # Only a taken visit can have one, and only an open one still matters.
        reissued = reissued_tokens(
            db,
            [
                r["id"]
                for r in rows
                if (r["doctor_id"] or "") == doctor_id and r["status"] != "completed"
            ],
        )

    current: list[dict] = []
    waiting: list[dict] = []
    skipped: list[dict] = []
    unassigned: list[dict] = []
    completed = 0
    for r in rows:
        data = _visit_api_dict(r)
        data.update(queue_fields(r))
        data["previous_token_number"] = reissued.get(str(r["id"]))
        data["consultation_status"] = r["consultation_status"]
        data["waiting_minutes"] = _waiting_minutes(data, now=now)
        data["consultation_minutes"] = _consultation_minutes(data, now=now)
        mine = (r["doctor_id"] or "") == doctor_id
        if r["status"] == "completed":
            completed += 1 if mine else 0
            continue
        if not mine:
            if r["status"] == "waiting":
                unassigned.append(data)
            continue
        if r["status"] == "in_consultation":
            current.append(data)
        elif r["skipped_at"]:
            skipped.append(data)
        else:
            waiting.append(data)
    for bucket in (current, waiting, skipped, unassigned):
        bucket.sort(key=queue_sort_key)
    return {
        "visit_date": day,
        "department": department,
        "doctor_id": doctor_id,
        "current": current,
        "waiting": waiting,
        "skipped": skipped,
        "unassigned": unassigned,
        "completed_count": completed,
        "total": len(current) + len(waiting) + len(skipped) + len(unassigned),
        "priorities": [{"code": c, "label": lab} for c, lab in priority_categories()],
        "poll_seconds": int(getattr(config, "OPD_QUEUE_POLL_SECONDS", 30) or 30),
    }


# ── The waiting-room token board (opd-9) ─────────────────────────────────────
#
# A board is a DEVICE on a waiting-room wall, not a person. The administrator
# (schedule.manage) creates one for one clinic; its key is shown once, at
# creation, and only its hash is stored (opd_display_boards, migration 50).
# The kiosk page (/display/opd) reads the key from the URL FRAGMENT, which a
# browser never sends to a server or writes into a proxy log, and sends it in
# the X-Orb-Board-Key header only. A staff session is never accepted in its
# place, and the key opens nothing but the board.
#
# WHAT THE BOARD SHOWS (ruling OPD-N7, applied: token, doctor and room only,
# DPDP): per doctor of that one clinic, the doctor's name, the room, the token
# being called and the next BOARD_NEXT_COUNT tokens in seeing order. Never a
# patient name, UHID, complaint, age, sex, priority word or visit id, so a
# board read is not a PHI read and writes no phi_access row. board_snapshot
# builds the answer from BOARD_RESPONSE_KEYS and BOARD_DOCTOR_KEYS only.
#
# Creating and revoking a board are recorded on the fail-closed 'security'
# chain (a board key is a credential): neither the key nor its hash is ever
# written into a record, a log or an answer other than the creation's.

#: How often the kiosk re-reads (ruling: every 10 s).
BOARD_POLL_SECONDS = 10
#: How many waiting tokens follow the one being called.
BOARD_NEXT_COUNT = 3
#: The request header carrying the board's device key.
BOARD_KEY_HEADER = "X-Orb-Board-Key"
#: The only keys a board answer carries, and the only keys each doctor carries.
BOARD_RESPONSE_KEYS = (
    "board_label",
    "department",
    "visit_date",
    "generated_at",
    "poll_seconds",
    "doctors",
)
BOARD_DOCTOR_KEYS = ("doctor_name", "room", "calling", "next")
#: The record actions.
BOARD_CREATED_ACTION = "opd_display_board_created"
BOARD_REVOKED_ACTION = "opd_display_board_revoked"
BOARD_AUDIT_CATEGORY = "security"

_BOARD_LABEL_MAX = 80
_BOARD_KEY_MAX = 200
_BOARD_HASH_PREFIX = "sha256:"


class BoardError(FrontDeskError):
    """A display-board change refused, with a code and the HTTP status."""

    def __init__(self, code: str, message: str, *, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


def _board_key_hash(key: str) -> str:
    import hashlib

    return _BOARD_HASH_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _new_board_key() -> str:
    """256 random bits, URL-safe so it sits in a link's fragment as it is."""
    import secrets

    return secrets.token_urlsafe(32)


def _board_public(row, names: dict | None = None) -> dict:
    """A board as the Display boards screen lists it: never the key or its hash."""
    names = names or {}
    return {
        "id": row["id"],
        "department": row["department"],
        "label": row["label"],
        "created_by": row["created_by"],
        "created_by_name": names.get(row["created_by"]),
        "created_at": row["created_at"],
        "revoked_by": row["revoked_by"],
        "revoked_by_name": names.get(row["revoked_by"]),
        "revoked_at": row["revoked_at"],
        "active": row["revoked_at"] is None,
    }


def _staff_names(db, ids: Iterable[Any]) -> dict:
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    return {
        r["id"]: r["name"]
        for r in db.execute(f"SELECT id, name FROM users WHERE id IN ({marks})", wanted).fetchall()
    }


def _board_audit(
    db,
    *,
    action: str,
    actor: dict,
    board: dict,
    summary: str,
    request_meta: dict | None,
    extra: dict | None = None,
) -> None:
    """The fail-closed record of a board change, in the change's transaction.
    Carries the board's id, clinic and label: never the key or its hash."""
    from services.audit_service import log_audit_event

    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=f"{action}-{board['id']}",
        event_category=BOARD_AUDIT_CATEGORY,
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type="opd_display_board",
        target_id=str(board["id"]),
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata={
            "board_id": board["id"],
            "department": board["department"],
            "label": board["label"],
            **(extra or {}),
        },
    )


def _board_departments(
    scope_department: str | None, facilities: frozenset[int] | None = None
) -> list[str]:
    """The clinics a board may be made for: the departments with clinical
    cover, narrowed to the caller's own when the caller has one, and to the
    caller's facilities when it has a facility scope (multisite-8h1)."""
    from services.frontdesk_service import list_covering_departments

    departments = list_covering_departments(facilities=facilities)
    if scope_department is not None:
        return [d for d in departments if d == scope_department]
    return departments


def list_display_boards(*, scope_department: str | None) -> dict:
    """Every board (revoked ones too, newest first) the caller may manage,
    and the clinics a new one may be made for. No patient data."""
    ensure_frontdesk_schema()
    clauses, params = "", []
    if scope_department is not None:
        clauses, params = "WHERE department = ?", [scope_department]
    with db_session() as db:
        rows = db.execute(
            f"SELECT * FROM opd_display_boards {clauses} "
            f"ORDER BY (revoked_at IS NULL) DESC, created_at DESC, id",
            params,
        ).fetchall()
        names = _staff_names(db, [n for r in rows for n in (r["created_by"], r["revoked_by"])])
    return {
        "boards": [_board_public(r, names) for r in rows],
        "departments": _board_departments(scope_department),
        "poll_seconds": BOARD_POLL_SECONDS,
    }


def create_display_board(
    *,
    department: Any,
    label: Any,
    actor: dict,
    scope_department: str | None,
    request_meta: dict | None = None,
) -> dict:
    """A new board for one clinic. Returns {board, key}: the key is in this
    answer only, and only its hash is stored. The clinic must have clinical
    cover and, for a caller with a department, be the caller's own. With more
    than one facility on record it must be a clinic of the caller's own
    facility (multisite-8h1)."""
    import uuid as _uuid

    from services import facility_lanes

    ensure_frontdesk_schema()
    wanted = _clean(department, 200)
    clinic = next(
        (
            d
            for d in _board_departments(scope_department, facility_lanes.scope_of(actor or {}))
            if wanted and d.casefold() == wanted.casefold()
        ),
        None,
    )
    if clinic is None:
        raise BoardError(
            "department_not_covered",
            "Choose a clinic from the list. A board shows one clinic's tokens.",
        )
    name = _clean(label, _BOARD_LABEL_MAX) or f"{clinic} waiting area"
    key = _new_board_key()
    board_id = f"BRD-{_uuid.uuid4().hex[:10].upper()}"
    with db_session(immediate=True) as db:
        db.execute(
            "INSERT INTO opd_display_boards (id, department, label, key_hash, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (board_id, clinic, name, _board_key_hash(key), (actor or {}).get("id"), _now()),
        )
        row = db.execute("SELECT * FROM opd_display_boards WHERE id = ?", (board_id,)).fetchone()
        board = _board_public(row, _staff_names(db, [row["created_by"]]))
        _board_audit(
            db,
            action=BOARD_CREATED_ACTION,
            actor=actor,
            board=board,
            summary=f"Token board {board_id} created for {clinic}",
            request_meta=request_meta,
        )
    logger.info("OPD token board %s created for %s", board_id, clinic)
    return {"board": board, "key": key}


def revoke_display_board(
    board_id: Any,
    *,
    actor: dict,
    scope_department: str | None,
    reason: Any = None,
    request_meta: dict | None = None,
) -> dict:
    """Switch a board's key off for good: the screen stops showing tokens at
    its next read. A board of another clinic, for a caller with a department,
    is 404 like an unknown one, and so is a board of another facility's
    clinic once more than one facility is on record (multisite-8h1)."""
    from services import facility_lanes, facility_service

    ensure_frontdesk_schema()
    bid = _clean(board_id, 80) or ""
    facilities = facility_lanes.scope_of(actor or {})
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM opd_display_boards WHERE id = ?", (bid,)).fetchone()
        if row is None or (scope_department is not None and row["department"] != scope_department):
            raise BoardError("board_not_found", "Display board not found.", status=404)
        if facility_lanes.outside(
            facility_service.department_facility(row["department"], db=db)
            if facilities is not None
            else None,
            facilities,
        ):
            raise BoardError("board_not_found", "Display board not found.", status=404)
        if row["revoked_at"] is not None:
            raise BoardError(
                "already_revoked", "This board's key was already switched off.", status=409
            )
        db.execute(
            "UPDATE opd_display_boards SET revoked_at = ?, revoked_by = ? WHERE id = ?",
            (_now(), (actor or {}).get("id"), bid),
        )
        row = db.execute("SELECT * FROM opd_display_boards WHERE id = ?", (bid,)).fetchone()
        board = _board_public(row, _staff_names(db, [row["created_by"], row["revoked_by"]]))
        _board_audit(
            db,
            action=BOARD_REVOKED_ACTION,
            actor=actor,
            board=board,
            summary=f"Token board {bid} switched off",
            request_meta=request_meta,
            extra={"reason": _clean(reason, 500)},
        )
    logger.info("OPD token board %s revoked", bid)
    return board


def board_for_key(presented: Any) -> dict | None:
    """The live board this device key opens, or None: no key, a key of the
    wrong shape, an unknown key, a revoked one, and anything else (a staff
    session token included) all answer None alike."""
    import hmac

    if not isinstance(presented, str):
        return None
    key = presented.strip()
    if not key or len(key) > _BOARD_KEY_MAX:
        return None
    digest = _board_key_hash(key)
    ensure_frontdesk_schema()
    with db_session() as db:
        row = db.execute(
            "SELECT id, department, label, key_hash, revoked_at FROM opd_display_boards "
            "WHERE key_hash = ?",
            (digest,),
        ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    if not hmac.compare_digest(str(row["key_hash"]), digest):
        return None
    return {"id": row["id"], "department": row["department"], "label": row["label"]}


def board_snapshot(
    department: str, *, label: str | None = None, visit_date: str | None = None
) -> dict:
    """What one clinic's waiting-room board shows now (ruling OPD-N7): per
    doctor with a patient waiting or called in today, the doctor's name, the
    room, the token being called and the next tokens in seeing order (the
    same order as the doctor's own queue; skipped patients are not listed).
    Built from BOARD_RESPONSE_KEYS and BOARD_DOCTOR_KEYS only: no name, UHID,
    complaint, priority word or visit id ever leaves here."""
    from datetime import datetime

    ensure_frontdesk_schema()
    day = _hospital_day(visit_date)
    clinic = (department or "").strip()
    with db_session() as db:
        rows = db.execute(
            """SELECT v.id, v.doctor_id, v.token_number, v.status, v.room, v.priority,
                      v.skipped_at, v.called_at, v.registered_at, u.name AS staff_name,
                      v.doctor_name
                 FROM opd_visits v LEFT JOIN users u ON u.id = v.doctor_id
                WHERE v.visit_date = ? AND v.department = ? AND v.doctor_id IS NOT NULL
                  AND v.status IN ('waiting', 'in_consultation', 'completed')""",
            (day, clinic),
        ).fetchall()
    by_doctor: dict[str, list] = {}
    for r in rows:
        by_doctor.setdefault(str(r["doctor_id"]), []).append(r)
    doctors: list[dict] = []
    for _doctor_id, visits in by_doctor.items():
        live = [v for v in visits if v["status"] in ("waiting", "in_consultation")]
        if not live:
            continue
        called = sorted(
            (v for v in live if v["status"] == "in_consultation"),
            key=lambda v: (str(v["called_at"] or ""), int(v["token_number"] or 0)),
        )
        waiting = sorted(
            (dict(v) for v in live if v["status"] == "waiting" and not v["skipped_at"]),
            key=queue_sort_key,
        )
        current = called[-1] if called else None
        # The room the doctor is seeing patients in: the current visit's, else
        # the room last set on any of the doctor's visits today.
        roomed = [v for v in visits if (v["room"] or "").strip()]
        roomed.sort(
            key=lambda v: (
                str(v["called_at"] or v["registered_at"] or ""),
                int(v["token_number"] or 0),
            )
        )
        room = (
            (current["room"] or "").strip()
            if current is not None and (current["room"] or "").strip()
            else ((roomed[-1]["room"] or "").strip() if roomed else None)
        )
        name = (visits[0]["staff_name"] or visits[0]["doctor_name"] or "").strip() or "Doctor"
        doctors.append(
            {
                "doctor_name": name,
                "room": room or None,
                "calling": int(current["token_number"]) if current is not None else None,
                "next": [int(v["token_number"]) for v in waiting[:BOARD_NEXT_COUNT]],
            }
        )
    doctors.sort(key=lambda d: (d["doctor_name"].casefold(), d["room"] or ""))
    answer = {
        "board_label": label,
        "department": clinic,
        "visit_date": day,
        "generated_at": datetime.now(UTC).isoformat(),
        "poll_seconds": BOARD_POLL_SECONDS,
        "doctors": [{k: d[k] for k in BOARD_DOCTOR_KEYS} for d in doctors],
    }
    return {k: answer[k] for k in BOARD_RESPONSE_KEYS}
