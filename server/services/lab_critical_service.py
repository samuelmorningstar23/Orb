"""Laboratory critical results, the laboratory's side (lis-15): raising a
critical, posting it to the patient's case room, the callback log, the
callback ladder and the sweep that climbs it.

BEHIND ORB_LAB (default off). Nothing here runs while the laboratory is off:
a critical is raised only by the laboratory's own validation, amendment and
release steps, whose routes answer 404 then. The Critical results screen,
the callback log and the sweep stay open while the module drains (ruling
LIS-R21: switching the laboratory off must never hide an open critical), and
the gate on their routes (require_lab_critical_surface) also opens them while
ORB_LAB_CRITICAL_INTERFACED is on, for the interfaced lane (lis-22, below).

GO-LIVE IS HELD. Whether these surfaces sit inside held ruling R023's
intended-use scope is ruling LIS-R20, for Samuel. They are built as the
laboratory's own communication: the laboratory's flag against a limit its
pathologist approved, posted in the laboratory's name, with no score and no
severity Orb works out for itself.

RULINGS APPLIED AS BUILD DEFAULTS (strictest or recommended option, each to be
confirmed by Samuel before go-live)
  LIS-R1 (a)  No critical without an approved limit. A result is critical
              only when lab_result_service flagged it HH or LL against a
              lab_critical_limits row a pathologist approved; this module
              checks the limit row exists before it raises anything. Orb
              ships no limit.
  LIS-R4 (a)  Raised at technical validation, before authorisation, marked
              "Preliminary, not yet authorised" in the post and on the
              screen. The chart still shows released values only.
  LIS-R3      A callback closes a critical only when the laboratory logs it
              with the name and role of the person told, how they were told
              (phone or in person) and that they read the value back
              (closure (i)), or when a clinician on the patient's ward
              acknowledges it on the ward card with the name of the person
              told and a read-back (closure (ii), lis-16, acknowledge()). A
              Relay post, or any reply to it, never closes one. A post that
              could not be delivered (no case room, a discharged patient's
              closed room, a failed write) makes the tier due at once.
  LIS-R19 (c) Discharge and transfer never close a notification. A
              discharged patient's case room is closed to every clinician,
              so nothing is posted there; the delivery is recorded as not
              delivered and the screen says "Patient discharged" and tells
              the laboratory to phone the discharging consultant.
  LIS-R23 (a) No shadow evidence lane: delivery evidence lives in
              lab_critical_events (and the TAT report).

THE LADDER is site.toml [lab.critical] and its [[lab.critical.tiers]]
(services/site_config_service.py), validated like [escalation]. ORB SHIPS NO
LADDER: with none set, a critical is due the moment it is raised and stays
due until a callback is logged, and the screen says so. The last tier never
hands on, so it is due from the moment it is reached (tier_due_at keeps that
time) and stays due until a callback is logged: on the Critical results
screen a critical that climbed the whole ladder unconfirmed is never listed
below one that is not due yet. Each notification keeps the ladder it was
raised under (policy_json), so a restart with a new file never re-times a
result already in flight.

DELIVERY happens AFTER the step that raised the critical has committed, in
its own transaction: the case-room message (sender the technologist who
validated it, shown as 'Laboratory', role 'laboratory', type 'alert', is_bot
0, source 'lab', through services/case_room_alerts.py), the notification's
delivery_status and a 'posted' or 'delivery_failed' event, then the live
'message:new' push to the room. A failed delivery never undoes the
validation. Each tier posts under its own idempotency key
("lab-critical:<id>:t<tier>"), so a tier is never posted twice.

THE SWEEP (sweep_loop, started by lab_module.startup in every module state
and running only while the critical surfaces are open) moves every open
critical whose tier is due: to the next tier, which posts again, or, on the
last tier (or with no ladder), retries an undelivered post once. A critical
nobody confirms stays open, due and on the screen; Orb never closes one by
itself. Every post is written from the result as it stands at that moment,
never from the value first raised.

A VALUE THAT CHANGES AFTER IT WAS RAISED (a signatory sends a validated
result back to the bench, its value is changed and it is validated again).
A notification stands for the value the ward was last told; value_display
keeps the value first raised. When the result is validated (or released)
again with a different value:
  * an OPEN notification records the change (an 'escalated' event whose
    metadata reason is 'value_changed': migration 55 fixes the event types,
    and the TAT report counts 'raised' events as new criticals) with a
    fail-closed clinical_access audit row ('lab_critical_value_changed'),
    stays open at its tier and due time, and posts at once under a new key
    ("lab-critical:<id>:t<tier>:r<n>"): a changed critical value, or a
    correction saying the value the ward was given is no longer critical;
  * a CLOSED notification (confirmed with a read-back) whose value is no
    longer critical is REOPENED at tier 1 with the same record, so the
    laboratory phones the correction and logs it with a read-back (strictest
    option, for Samuel to confirm); one whose new value is still critical
    raises a new notification that names the value reported earlier.
The screen compares the result's current value with the value told and says
the change in words. Each change gets its own attempts, so a failed post of
a change keeps its one retry on the last tier.

AN AMENDED OR WITHDRAWN RESULT (lis-13) goes through the same record, so
both lanes tell the ward the same way. An amendment makes a new version of
the result (superseded_by_result_id links each version to the next), so a
critical raised for any version is found through the version chain
(version_chain): the amendment's new value is recorded on the chain's open
notification, or on its latest closed one (reopened when the new value is
not critical, a new notification naming the earlier value when it is), and
a withdrawal records 'withdrawn' (WITHDRAWN_TOLD) on every open one, or
reopens the latest closed one at tier 1. Every post, the screen and the
value a callback reads back follow the chain to the version that stands now
(head_result_id), never one hop: a critical raised for version 1 of a result
amended twice says the third version's value, and one whose newest version
was withdrawn says it was withdrawn.

THE WARD CARD (lis-16, list_for_ward and acknowledge). The open criticals of
the patients in the caller's ward (permissions.scope_patient_ids_sql, which
follows PATIENT_DEPARTMENT_SQL: the active admission first, else the latest,
so a transferred patient's critical moves to the new ward and a discharged
patient's stays on the last ward, shown without a bed and marked
discharged). The card shows what the laboratory said: its value, its own flag
and the limit its pathologist agreed (for the interfaced lane, the sending
laboratory's flag and the reference range it sent), and whether the value is
preliminary. Orb adds no severity and no score of its own. A clinician
holding lab.critical_ack acknowledges one with the name of the person who
was told, how they were told (on the ward screen, or a phone call from the
laboratory) and a read-back, in one immediate transaction with its 'acknowledged'
event (metadata closure 'ward_read_back') and a fail-closed clinical_access
audit row ('lab_critical_acknowledged'). That closes it, so the ladder stops.
A patient outside the caller's ward is 404, exactly like a critical that does
not exist.

INTERFACED CRITICALS (lis-22, raise_interfaced and raise_interfaced_and_deliver).
BEHIND ORB_LAB_CRITICAL_INTERFACED (default off; it works without ORB_LAB;
ruling LIS-R22 applied as the build default, go-live held with LIS-R1, LIS-R3
and LIS-R20). An external laboratory's result that arrives over the
interface, is final or corrected, is not simulated and not a backfill, belongs
to a patient Orb resolved, comes from a result feed (not a monitor's device
metric) and carries the sending laboratory's own OBX-8 flag HH, LL or AA
raises one notification in the 'interfaced' lane, keyed on the observation.
Orb never works out a flag of its own here: it passes on the sender's. The
post goes to the case room as the service identity system_lab (a users row
that can never sign in, listed in database.SERVICE_ACCOUNT_IDS), shown as
'Laboratory interface', is_bot 1, source 'lab_interface'. The same ladder, the
same sweep and the same ward acknowledgement apply. A resent message never
raises twice: one notification per observation, and none for a result of the
same patient, test, sender and order (the filler number, else the placer
number the sender supplied, else the collection time) with the same value
raised in the last INTERFACED_DEDUPE_HOURS. A corrected result with a
different critical value raises a new one that names the earlier value.

Every write is one immediate transaction with its clinical_access audit row
inside it (fail-closed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

import config
from database import db_session
from db_backend import DBIntegrityError
from permissions import LAB_CRITICAL_ACK, LAB_CRITICAL_LOG, has_permission

from services import case_room_alerts, lab_module, lab_observations
from services.audit_service import log_audit_event
from services.clinical_time import format_utc, parse_to_utc, utc_iso_now

logger = logging.getLogger("orb.lab.critical")

#: The lane a laboratory-raised critical belongs to.
SOURCE_LANE_LIS = "lis"
#: The lane of a critical an external laboratory flagged on a result that
#: arrived over the interface (lis-22).
SOURCE_LANE_INTERFACED = "interfaced"
#: How the laboratory appears in the case room.
SENDER_NAME = "Laboratory"
SENDER_ROLE = "laboratory"
MESSAGE_SOURCE = "lab"
MESSAGE_TYPE = "alert"

#: Who a callback may name, and the words the screen shows for each.
RECIPIENT_ROLES = {
    "nurse": "Nurse",
    "nurse_in_charge": "Nurse in charge",
    "doctor": "Doctor",
    "consultant": "Consultant",
    "duty_doctor": "Duty doctor",
    "duty_medical_officer": "Duty medical officer",
}
#: How the laboratory told them. 'in_app' is the ward's own acknowledgement
#: (lis-16), never a laboratory callback.
CALLBACK_METHODS = {"phone": "Phone", "in_person": "In person"}

#: How the ward was told, for its own acknowledgement on the ward card
#: (lis-16): read on the ward screen, or a phone call from the laboratory
#: (ruling LIS-R22: an external laboratory's own call is logged as the ward's
#: read-back acknowledgement). Stored in lab_critical_notifications.method.
WARD_ACK_METHODS = {"in_app": "Read on the ward screen", "phone": "Phone call from the laboratory"}
#: The metadata closure of the 'acknowledged' event each closure writes
#: (lab_tat_service reads them: anything but the laboratory's callback counts
#: as a ward read-back).
CLOSURE_LABORATORY_CALLBACK = "laboratory_callback"
CLOSURE_WARD_READ_BACK = "ward_read_back"
#: The recipient role a ward acknowledgement records when none is chosen,
#: from the acknowledging account's role.
_WARD_ROLE_DEFAULT = {"nurse": "nurse", "doctor": "doctor", "clinician": "doctor"}
#: The most open criticals the ward card lists.
WARD_LIST_CAP = 100

#: The laboratory's own flag, in words, for the ward card. HH, LL and AA are
#: the critical flags (HL7 table 0078); the others can stand on a result whose
#: value changed after it was raised. Orb never works one out for itself.
LAB_FLAG_WORDS = {
    "HH": "Critical high",
    "LL": "Critical low",
    "AA": "Critical abnormal",
    "H": "High",
    "L": "Low",
    "A": "Abnormal",
}

#: The service identity an interfaced critical is posted as (lis-22). A users
#: row that can never sign in (is_active 0 and a password no login matches),
#: listed in database.SERVICE_ACCOUNT_IDS so User Management refuses every
#: change to it. Created the first time an interfaced critical is posted.
SYSTEM_LAB_ID = "system_lab"
SYSTEM_LAB_NAME = "Laboratory interface"
#: Its role on the users row: a laboratory role with no Relay, Sage or
#: patient.read_all grant, and no department.
SYSTEM_LAB_ROLE = "lab_technician"
INTERFACED_SENDER_NAME = "Laboratory interface"
INTERFACED_MESSAGE_SOURCE = "lab_interface"
#: OBX-8 flags that raise an interfaced critical (HL7 table 0078: above the
#: upper panic limit, below the lower panic limit, critically abnormal).
INTERFACED_CRITICAL_FLAGS = ("HH", "LL", "AA")
#: Result statuses that raise one: final or corrected (HL7 OBX-11 F and C,
#: and the FHIR and free-text spellings the adapters write; FHIR 'amended' is
#: the parent of 'corrected'). A preliminary result raises nothing.
INTERFACED_RAISING_STATUSES = ("f", "final", "c", "corrected", "amended")
#: How far back a resent result is looked for, so the same result sent twice
#: never raises twice.
INTERFACED_DEDUPE_HOURS = 24

#: Delivery outcomes recorded on a notification, in words for the screen.
DELIVERY_POSTED = "posted"
DELIVERY_PENDING = "pending"
DELIVERY_NO_CASE_ROOM = "no_case_room"
DELIVERY_FAILED = "failed"
#: The detail behind 'no_case_room' when the room exists but is closed.
REASON_CASE_ROOM_CLOSED = "case_room_closed"

#: The most attempts one tier's post gets: the first, and one retry.
ATTEMPTS_PER_TIER = 2
#: The metadata reason of the 'escalated' event that records a value change
#: on a notification.
CHANGE_REASON = "value_changed"
#: What a delivery answer calls its post: a new critical, a critical whose
#: value changed and is still critical, or a correction (no longer critical).
KIND_RAISED = "raised"
KIND_CHANGED = "changed"
KIND_CORRECTED = "corrected"
#: The most due notifications one sweep looks at.
SWEEP_BATCH = 200
#: The loop's name in lab_module.
SWEEP_LOOP_NAME = "critical-sweep"
#: How long a closed critical stays on the Critical results screen.
RECENT_CLOSED_HOURS = 24
#: The most open and recently closed criticals the screen lists.
LIST_CAP = 500
NAME_MAX = 120
NOTE_MAX = 500
#: The longest value a callback or an attempt may name as the one the screen
#: showed (value_told).
VALUE_TOLD_MAX = 200
#: What a notification stands for once its result was withdrawn: the value
#: the ward was last told is then the withdrawal itself, and a callback reads
#: that back.
WITHDRAWN_TOLD = "withdrawn"
#: The most versions of one result the version chain is followed through: a
#: guard against a chain that points back at itself, never a number a real
#: result reaches.
CHAIN_MAX = 100

_TABLES = (
    "lab_critical_notifications",
    "lab_critical_events",
    "lab_results",
    "lab_critical_limits",
)


class LabCriticalError(Exception):
    """A refusal with a machine code, a sentence for the screen and the HTTP
    status the route answers with."""

    def __init__(self, code: str, message: str, *, status: int = 409, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


# ════════════════════════════════════════════════════════════════════════════
#  The switch, the ladder and the clock
# ════════════════════════════════════════════════════════════════════════════


def surface_open() -> bool:
    """Whether the critical-result surfaces run: while the laboratory is on or
    draining, and while interfaced criticals are on without it (the same rule
    as routes.opt_in_gates.require_lab_critical_surface)."""
    if config.module_state("lab") in ("on", "draining"):
        return True
    return bool(config.feature_enabled("lab_critical_interfaced"))


def ladder() -> list[dict]:
    """The site's callback ladder from site.toml [lab.critical], as loaded at
    startup: [{tier, role, label, timeout_minutes}], or [] for none. Orb
    ships none."""
    from services.site_config_service import current_site_config

    tiers = (current_site_config().lab_critical or {}).get("tiers") or []
    return [dict(t) for t in tiers]


def policy_snapshot() -> dict:
    """What a notification keeps of the ladder it was raised under."""
    tiers = ladder()
    return {"configured": bool(tiers), "tiers": tiers, "source": "site.toml" if tiers else None}


def _policy(row: dict) -> dict:
    raw = row.get("policy_json")
    try:
        policy = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        policy = {}
    if not isinstance(policy, dict):
        policy = {}
    tiers = policy.get("tiers") if isinstance(policy.get("tiers"), list) else []
    return {"configured": bool(tiers), "tiers": tiers, "source": policy.get("source")}


def _plus_minutes(now: str, minutes: Any) -> str:
    return format_utc(parse_to_utc(now) + timedelta(minutes=float(minutes)))


def due_at(now: str, tiers: list[dict], tier_number: int) -> str | None:
    """When tier `tier_number`, reached at `now`, is due. With no ladder the
    critical is due at once (the laboratory phones now). The last tier never
    hands on, so it is due from the moment it is reached: `now`, never None,
    so the screen lists a critical that climbed the whole ladder unconfirmed
    as due and as overdue as it is (a timeout on the last tier is ignored, as
    site_config_service says when it loads one). None only for a tier the
    ladder does not have. Pure."""
    if not tiers:
        return now
    if tier_number < 1 or tier_number > len(tiers):
        return None
    minutes = tiers[tier_number - 1].get("timeout_minutes")
    if minutes is None or tier_number == len(tiers):
        return now
    return _plus_minutes(now, minutes)


def _tier_words(tiers: list[dict], tier_number: int) -> str | None:
    if not tiers or tier_number < 1 or tier_number > len(tiers):
        return None
    tier = tiers[tier_number - 1]
    return tier.get("label") or tier.get("role")


# ════════════════════════════════════════════════════════════════════════════
#  Raising
# ════════════════════════════════════════════════════════════════════════════


def _tables_present(conn) -> bool:
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'lab_%'"
        ).fetchall()
    }
    return set(_TABLES) <= names


def value_display(result: dict) -> str:
    """'7.0 mmol/L', '>600 U/L': the value as the laboratory reports it."""
    text = str(result.get("value_text") if result.get("value_text") is not None else "").strip()
    unit = (result.get("unit") or "").strip()
    return f"{text} {unit}".strip() if unit else text


def _told_phrase(test_name: str, told: str) -> str:
    """'Potassium 7.0 mmol/L', or 'Potassium was withdrawn' once the value
    told is the withdrawal: for the audit summaries and the refusals."""
    if told == WITHDRAWN_TOLD:
        return f"{test_name} was withdrawn"
    return f"{test_name} {told}"


def head_result_id(conn, result_id: Any) -> int | None:
    """The version of a laboratory result that stands now: superseded_by_result_id
    followed from `result_id` to the row that names no newer version, never
    only one hop (a result amended twice stands at its third version). None
    when `result_id` names no row. A pointer to a missing row or back into
    the chain stops the walk at the last row read."""
    try:
        current = int(result_id)
    except (TypeError, ValueError):
        return None
    seen: set[int] = set()
    found: int | None = None
    while len(seen) < CHAIN_MAX:
        row = conn.execute(
            "SELECT superseded_by_result_id FROM lab_results WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            return found
        found = current
        seen.add(current)
        nxt = row[0]
        if nxt is None:
            return current
        try:
            nxt = int(nxt)
        except (TypeError, ValueError):
            return current
        if nxt in seen:
            return current
        current = nxt
    return found


def version_chain(conn, result_id: Any) -> list[int]:
    """Every version of one laboratory result, oldest first: the versions it
    replaced and the versions that replaced it (an amendment links each
    version to the next through superseded_by_result_id; every version keeps
    the sample and the test line, so they are read through the sample's
    index). Just [result_id] for a result never amended, or one with no
    row."""
    try:
        rid = int(result_id)
    except (TypeError, ValueError):
        return []
    row = conn.execute(
        "SELECT specimen_id, order_item_id, superseded_by_result_id FROM lab_results WHERE id = ?",
        (rid,),
    ).fetchone()
    if row is None:
        return [rid]
    links: dict[int, int | None] = {rid: row["superseded_by_result_id"]}
    if row["specimen_id"] is not None:
        for r in conn.execute(
            "SELECT id, superseded_by_result_id FROM lab_results "
            "WHERE specimen_id = ? AND order_item_id = ?",
            (row["specimen_id"], row["order_item_id"]),
        ).fetchall():
            links[int(r["id"])] = r["superseded_by_result_id"]
    replaced_by = {}
    for older, newer in links.items():
        if newer is not None:
            replaced_by[int(newer)] = older
    chain = [rid]
    current = rid
    while current in replaced_by and len(chain) < CHAIN_MAX:
        current = replaced_by[current]
        if current in chain:
            break
        chain.insert(0, current)
    current = rid
    while len(chain) < CHAIN_MAX:
        newer = links.get(current)
        if newer is None and current not in links:
            # A version outside the sample's rows: read it by its own id.
            got = conn.execute(
                "SELECT superseded_by_result_id FROM lab_results WHERE id = ?", (current,)
            ).fetchone()
            newer = got[0] if got is not None else None
        if newer is None:
            break
        newer = int(newer)
        if newer in chain:
            break
        chain.append(newer)
        current = newer
    return chain


def _chain_notifications(conn, result_id: Any) -> list[dict]:
    """The laboratory-lane notifications raised for any version of one
    result, in the order they were raised."""
    chain = version_chain(conn, result_id)
    if not chain:
        return []
    marks = ", ".join("?" for _ in chain)
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM lab_critical_notifications WHERE source_lane = ? "
            f"AND result_id IN ({marks}) ORDER BY rowid",
            (SOURCE_LANE_LIS, *chain),
        ).fetchall()
    ]


def open_count_for_result(conn, result_id: Any) -> int:
    """How many criticals raised for any version of this result are open:
    the ones the laboratory still has to phone and log."""
    if not _tables_present(conn):
        return 0
    return sum(1 for n in _chain_notifications(conn, result_id) if n["status"] == "open")


def _audit(
    conn,
    *,
    action: str,
    summary: str,
    actor: dict | None,
    notification_id: str,
    patient_id: str,
    metadata: dict,
    request_meta: dict | None = None,
) -> None:
    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_access",
        action_type=action,
        outcome="success",
        db=conn,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id") or "system",
        actor_role=(actor or {}).get("role") or "system",
        target_type="lab_critical_notification",
        target_id=str(notification_id),
        patient_id=patient_id,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata=metadata,
    )


def _event(
    conn,
    notification_id: str,
    patient_id: str,
    event_type: str,
    *,
    tier: int | None,
    actor_id: str | None,
    now: str,
    detail: str | None = None,
    metadata: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO lab_critical_events (notification_id, patient_id, event_type, tier, "
        "actor_user_id, occurred_at, detail, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            notification_id,
            patient_id,
            event_type,
            tier,
            actor_id,
            now,
            detail,
            json.dumps(metadata, default=str) if metadata is not None else None,
        ),
    )


def _meta(raw: Any) -> dict:
    try:
        meta = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _changes(conn, notification_id: str) -> list[dict]:
    """The value changes recorded on one notification, oldest first: the
    metadata of each 'escalated' event whose reason is CHANGE_REASON, with
    its event id and time."""
    out = []
    for r in conn.execute(
        "SELECT id, occurred_at, metadata_json FROM lab_critical_events "
        "WHERE notification_id = ? AND event_type = 'escalated' ORDER BY id",
        (notification_id,),
    ).fetchall():
        meta = _meta(r["metadata_json"])
        if meta.get("reason") == CHANGE_REASON:
            out.append({**meta, "event_id": r["id"], "at": r["occurred_at"]})
    return out


def _told(n: dict, changes: list[dict]) -> str:
    """The value a notification now stands for: the last value the ward was
    sent for it (the value first raised until it changes)."""
    return str(changes[-1].get("to_value")) if changes else n["value_display"]


def _critical_told(n: dict, changes: list[dict]) -> str:
    """The last value the ward was sent as critical for this notification."""
    for change in reversed(changes):
        if change.get("still_critical"):
            return str(change.get("to_value"))
    return n["value_display"]


def _raised_meta(conn, notification_id: str) -> dict:
    row = conn.execute(
        "SELECT metadata_json FROM lab_critical_events WHERE notification_id = ? "
        "AND event_type = 'raised' ORDER BY id LIMIT 1",
        (notification_id,),
    ).fetchone()
    return _meta(row["metadata_json"]) if row is not None else {}


def _approved_limit(conn, result: dict):
    """The approved critical limit a result crossed, or None: no critical
    without an approved limit (LIS-R1 (a))."""
    if int(result.get("is_critical") or 0) != 1 or not result.get("critical_limit_id"):
        return None
    limit = conn.execute(
        "SELECT id, display_text, agreed_with, approved_by FROM lab_critical_limits WHERE id = ?",
        (result["critical_limit_id"],),
    ).fetchone()
    if limit is None or not limit["approved_by"]:
        return None
    return limit


def _record_change_in_txn(
    conn,
    n: dict,
    result: dict,
    *,
    shown: str,
    told: str,
    limit,
    actor: dict,
    preliminary: bool,
    now: str,
    trigger: str,
    request_meta: dict | None,
) -> str | None:
    """Record on notification `n` that its result now reads `shown` where the
    ward was told `told`, on the caller's write transaction, so the next
    delivery posts the change under a new key. An open notification keeps its
    tier and due time; a closed one is reopened at tier 1 with its ladder
    restarted (the confirmed value no longer holds). Writes the
    'escalated' event (reason 'value_changed') and a fail-closed
    clinical_access audit row ('lab_critical_value_changed'). `shown` is
    WITHDRAWN_TOLD for a withdrawn result (record_withdrawal_in_txn): never
    critical, and said as a withdrawal."""
    changes = _changes(conn, n["id"])
    withdrawn = shown == WITHDRAWN_TOLD
    still_critical = limit is not None and not withdrawn
    reopen = n["status"] != "open"
    tiers = _policy(n)["tiers"]
    from_tier = int(n["current_tier"])
    if reopen:
        to_tier = 1
        moved = conn.execute(
            """UPDATE lab_critical_notifications
               SET status = 'open', current_tier = 1, tier_due_at = ?, delivery_status = ?,
                   relay_message_id = NULL, preliminary = ?, acknowledged_at = NULL,
                   acknowledged_by = NULL, recipient_name = NULL, recipient_role = NULL,
                   method = NULL, read_back = NULL
               WHERE id = ? AND status = ?""",
            (
                due_at(now, tiers, 1),
                DELIVERY_PENDING,
                1 if preliminary else 0,
                n["id"],
                n["status"],
            ),
        )
    else:
        to_tier = from_tier
        moved = conn.execute(
            "UPDATE lab_critical_notifications SET delivery_status = ?, relay_message_id = NULL, "
            "preliminary = ? WHERE id = ? AND status = 'open' AND current_tier = ?",
            (DELIVERY_PENDING, 1 if preliminary else 0, n["id"], from_tier),
        )
    if moved.rowcount != 1:
        return None
    meta = {
        "reason": CHANGE_REASON,
        "result_id": int(result["id"]),
        "trigger": trigger,
        "from_value": told,
        "to_value": shown,
        "critical_value": _critical_told(n, changes),
        "still_critical": still_critical,
        "preliminary": bool(preliminary),
        "reopened": reopen,
        "closed_status": n["status"] if reopen else None,
        "from_tier": from_tier,
        "to_tier": to_tier,
        "revision": len(changes) + 1,
        "withdrawn": withdrawn,
        "critical_limit_id": int(limit["id"]) if limit is not None and not withdrawn else None,
        "critical_limit_text": (
            limit["display_text"] if limit is not None and not withdrawn else None
        ),
    }
    if withdrawn:
        words = (
            f"Withdrawn after it was raised: the ward was told {told}; the result was "
            "entered in error and is no longer on the chart."
        )
        summary_change = f"{n['test_name']} {told}, now withdrawn"
    else:
        words = f"Value changed after it was raised: was {told}, now {shown}, " + (
            "still critical." if still_critical else "not critical."
        )
        summary_change = f"{n['test_name']} was {told}, now {shown}"
    if reopen:
        words = (
            "Reopened: the value confirmed with the ward "
            + ("was withdrawn. " if withdrawn else "has changed. ")
            + words
        )
    words += " The ward is told in the case room. Phone the ward and log the call with a read-back."
    _event(
        conn,
        n["id"],
        n["patient_id"],
        "escalated",
        tier=to_tier,
        actor_id=(actor or {}).get("id"),
        now=now,
        detail=words,
        metadata=meta,
    )
    _audit(
        conn,
        action="lab_critical_value_changed",
        summary=f"Laboratory critical result changed after it was raised: {summary_change}",
        actor=actor,
        notification_id=n["id"],
        patient_id=n["patient_id"],
        metadata=meta,
        request_meta=request_meta,
    )
    return n["id"]


def raise_for_result_in_txn(
    conn,
    result: dict,
    *,
    actor: dict,
    preliminary: bool,
    now: str,
    trigger: str,
    request_meta: dict | None = None,
) -> str | None:
    """Raise a critical for one laboratory result on the caller's write
    transaction, with its 'raised' event and a fail-closed clinical_access
    audit row ('lab_critical_raised'), or record that the value a critical
    stands for has changed (see the module notes). Call it for every result
    a step validates, releases or amends, critical or not. Returns the id of
    the notification to deliver, or None when there is nothing to tell:

      * the result is not flagged critical, or names no critical limit, or
        the limit row is not there (no critical without an approved limit,
        LIS-R1 (a)), and no critical was told for it;
      * the open notification raised for this result or for any earlier
        version of it (version_chain: an amendment is a new version), or
        else the latest closed one, already stands for this value: it was
        already told.

    Otherwise: an open notification whose value differs records the change;
    a closed one records it and reopens when the new value is not critical;
    and a critical value with no notification, or one that differs from the
    value a closed notification was confirmed for, raises a new one (naming
    the earlier value). An amendment therefore records its new value on the
    critical raised for the version it replaces, exactly as a result sent
    back to the bench does, and raises no second open critical beside it.
    The chain holds one open notification at a time (an open one always
    takes the change); were there ever two, the latest takes it.

    `trigger` says which step raised it ('tech_validated', 'released' or
    'amended'). Delivery is the caller's, after commit: deliver()."""
    if not _tables_present(conn):
        return None
    limit = _approved_limit(conn, result)
    rid = int(result["id"])
    shown = value_display(result)
    earlier = _chain_notifications(conn, rid)
    open_one = next((r for r in reversed(earlier) if r["status"] == "open"), None)
    latest = open_one or (earlier[-1] if earlier else None)
    previous: dict | None = None
    if latest is not None:
        told = _told(latest, _changes(conn, latest["id"]))
        if told == shown:
            return None
        if open_one is not None or limit is None:
            return _record_change_in_txn(
                conn,
                latest,
                result,
                shown=shown,
                told=told,
                limit=limit,
                actor=actor,
                preliminary=preliminary,
                now=now,
                trigger=trigger,
                request_meta=request_meta,
            )
        previous = {"notification_id": latest["id"], "value_display": told}
    if limit is None:
        return None
    test = conn.execute(
        "SELECT code, name FROM lab_tests WHERE id = ?", (result["test_id"],)
    ).fetchone()
    from permissions import PATIENT_DEPARTMENT_SQL

    patient = conn.execute(
        f"SELECT p.id, {PATIENT_DEPARTMENT_SQL} AS department FROM patients p WHERE p.id = ?",
        (result["patient_id"],),
    ).fetchone()
    policy = policy_snapshot()
    nid = f"LC-{uuid.uuid4().hex[:20]}"
    test_name = (test["name"] if test is not None else None) or str(result["test_id"])
    try:
        conn.execute(
            """INSERT INTO lab_critical_notifications
               (id, source_lane, result_id, patient_id, test_code, test_name, value_display,
                preliminary, raised_at, raised_by, raised_in_department, current_tier,
                tier_due_at, delivery_status, policy_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'open')""",
            (
                nid,
                SOURCE_LANE_LIS,
                rid,
                result["patient_id"],
                test["code"] if test is not None else None,
                test_name,
                shown,
                1 if preliminary else 0,
                now,
                (actor or {}).get("id"),
                patient["department"] if patient is not None else None,
                due_at(now, policy["tiers"], 1),
                DELIVERY_PENDING,
                json.dumps(policy, default=str),
            ),
        )
    except DBIntegrityError:
        # Another step raised one for this result in the meantime.
        return None
    metadata = {
        "result_id": rid,
        "trigger": trigger,
        "preliminary": bool(preliminary),
        "test_id": result["test_id"],
        "value_display": shown,
        "critical_limit_id": int(limit["id"]),
        "critical_limit_text": limit["display_text"],
        "ladder_configured": policy["configured"],
    }
    if previous is not None:
        # The ward confirmed a different value for this result earlier.
        metadata.update(
            previous_notification_id=previous["notification_id"],
            previous_value_display=previous["value_display"],
        )
    _event(
        conn,
        nid,
        result["patient_id"],
        "raised",
        tier=1,
        actor_id=(actor or {}).get("id"),
        now=now,
        detail=(
            f"Critical result raised: {test_name} {shown}"
            + (
                f" (changed from {previous['value_display']} reported earlier)"
                if previous is not None
                else ""
            )
        ),
        metadata=metadata,
    )
    _audit(
        conn,
        action="lab_critical_raised",
        summary=f"Laboratory critical result raised: {test_name} {shown}",
        actor=actor,
        notification_id=nid,
        patient_id=result["patient_id"],
        metadata=metadata,
        request_meta=request_meta,
    )
    return nid


def record_withdrawal_in_txn(
    conn, result: dict, *, actor: dict, now: str, request_meta: dict | None = None
) -> list[str]:
    """Record on the caller's write transaction that a reported result was
    withdrawn (entered in error), on the criticals raised for it or for any
    version it replaced, the same way a changed value is recorded: every
    open one records the withdrawal (value told WITHDRAWN_TOLD, not
    critical) and stays open at its tier; with none open, the latest closed
    one, whose value the ward confirmed and may have acted on, is reopened at
    tier 1, so the laboratory phones the withdrawal and logs it with a
    read-back. Each writes its 'escalated' event and fail-closed audit row.
    Never raises a new critical. Returns the ids to deliver after commit
    (deliver_all), [] when no critical was raised for the result."""
    if not _tables_present(conn):
        return []
    earlier = _chain_notifications(conn, result["id"])
    if not earlier:
        return []
    opened = [n for n in earlier if n["status"] == "open"]
    targets = opened or [earlier[-1]]
    out = []
    for n in targets:
        told = _told(n, _changes(conn, n["id"]))
        if told == WITHDRAWN_TOLD:
            continue
        nid = _record_change_in_txn(
            conn,
            n,
            result,
            shown=WITHDRAWN_TOLD,
            told=told,
            limit=None,
            actor=actor,
            preliminary=False,
            now=now,
            trigger="withdrawn",
            request_meta=request_meta,
        )
        if nid:
            out.append(nid)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Interfaced results (lis-22)
# ════════════════════════════════════════════════════════════════════════════

#: The columns the interfaced lane reads of one stored result. The table is
#: named through lab_observations.from_sql, and only by id: this lane reads
#: the one row it raised for, superseded or not, never a list of results.
_OBS_COLUMNS = (
    "ob.id, ob.patient_id, ob.source, ob.observation_code, "
    "ob.observation_code_system, ob.observation_name, ob.value_text, "
    "ob.value_numeric, ob.unit, ob.reference_range, ob.abnormal_flag, ob.status, "
    "ob.observed_at, ob.raw_segment_ref, ob.obr_placer, ob.obr_filler, "
    "ob.hl7_sender, ob.superseded_by, ob.lab_result_id"
)


def _observation(conn, observation_id: Any) -> dict | None:
    try:
        oid = int(observation_id)
    except (TypeError, ValueError):
        return None
    row = conn.execute(
        f"SELECT {_OBS_COLUMNS} FROM {lab_observations.from_sql('ob')} WHERE ob.id = ?", (oid,)
    ).fetchone()
    return dict(row) if row is not None else None


def _raw_obx_field(raw: Any, index: int) -> str | None:
    """Field `index` of a raw HL7 OBX segment written with the standard
    separators, or None for anything else."""
    text = str(raw or "")
    if not text.startswith("OBX|"):
        return None
    parts = text.split("|")
    return parts[index] if len(parts) > index else None


def interfaced_flag(abnormal_flag: Any, raw_segment: Any = None) -> str | None:
    """The sending laboratory's own critical flag on one result: 'HH', 'LL'
    or 'AA', or None. Reads the stored flag (the code before any '^') and,
    for an HL7 result, every repetition of OBX-8 in the raw segment, because
    the adapter stores only the first. Orb never works one out for itself."""
    found: list[str] = []
    first = str(abnormal_flag or "").split("^", 1)[0].strip().upper()
    if first:
        found.append(first)
    raw8 = _raw_obx_field(raw_segment, 8)
    if raw8:
        for rep in raw8.split("~"):
            code = rep.split("^", 1)[0].strip().upper()
            if code:
                found.append(code)
    for code in INTERFACED_CRITICAL_FLAGS:
        if code in found:
            return code
    return None


_SN_COMPARATORS = ("", "<", ">", "<=", ">=", "=", "<>")
_SN_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def _as_sent(value: Any) -> str:
    """A value as the sender wrote it. A structured numeric keeps its
    comparator ('>^600' reads '>600', '^7.1' reads '7.1'); a coded value
    reads as its text ('POS^Positive^L' reads 'Positive')."""
    text = str(value if value is not None else "").strip()
    if "^" not in text:
        return text
    parts = [part.strip() for part in text.split("^")]
    if parts[0] in _SN_COMPARATORS and len(parts) > 1 and _SN_NUMBER.match(parts[1]):
        return "".join(part for part in parts if part)
    return parts[1] if len(parts) > 1 and parts[1] else parts[0]


def _observation_value(row: dict, value_as_sent: Any = None) -> str:
    """'7.10 mmol/L': an interfaced result as the sending laboratory wrote it
    (its own precision), with its unit, at most VALUE_TOLD_MAX characters so
    a callback or an acknowledgement can name it."""
    text = _as_sent(value_as_sent) if value_as_sent is not None else ""
    if not text:
        raw5 = _raw_obx_field(row.get("raw_segment_ref"), 5)
        if raw5:
            text = _as_sent(raw5.split("~", 1)[0])
    if not text:
        shown = lab_observations.display_value(row)
        if isinstance(shown, float):
            text = format(shown, "g")
        elif shown is not None:
            text = str(shown).strip()
    shown = value_display({"value_text": text, "unit": row.get("unit")})
    return re.sub(r"\s+", " ", shown).strip()[:VALUE_TOLD_MAX]


def _interfaced_skip(row: dict | None) -> str | None:
    """Why a stored result raises no interfaced critical, or None when it
    does: no row, no patient, a simulator's row, a result Orb's own
    laboratory released (the 'lis' lane's), not final or corrected, replaced
    or voided, a monitor's device metric, or no critical flag from the
    sender."""
    if row is None:
        return "not_found"
    if not row.get("patient_id"):
        return "no_patient"
    source = str(row.get("source") or "").strip().lower()
    if source == lab_observations.SIMULATED_SOURCE:
        return "simulated"
    if row.get("lab_result_id") is not None:
        return "laboratory_released"
    status = str(row.get("status") or "final").strip().lower()
    if status not in INTERFACED_RAISING_STATUSES:
        return "not_final"
    if row.get("superseded_by") is not None or status in lab_observations.VOID_OBSERVATION_STATUSES:
        return "not_live"
    from services import result_quarantine_service

    if not result_quarantine_service.takes_result(
        row.get("source"), row.get("observation_code_system")
    ):
        return "device_metric"
    if interfaced_flag(row.get("abnormal_flag"), row.get("raw_segment_ref")) is None:
        return "not_flagged"
    return None


def _instant(value: Any):
    try:
        return parse_to_utc(value) if value else None
    except (TypeError, ValueError):
        return None


def _same_result(a: dict, b: dict) -> bool:
    """Whether two stored results are the same result of the same order: the
    same filler number (OBR-3); or, when either has none, the same placer
    number the sender supplied (OBR-2); or, when neither carries a number at
    all, the same collection instant."""
    fa, fb = (a.get("obr_filler") or "").strip(), (b.get("obr_filler") or "").strip()
    pa, pb = (a.get("obr_placer") or "").strip(), (b.get("obr_placer") or "").strip()
    if fa and fb:
        return fa == fb
    if pa and pb and pa == pb:
        return True
    if not (fa or pa) and not (fb or pb):
        ia, ib = _instant(a.get("observed_at")), _instant(b.get("observed_at"))
        if ia is not None and ib is not None:
            return ia == ib
        return bool(a.get("observed_at")) and a.get("observed_at") == b.get("observed_at")
    return False


def _interfaced_earlier(conn, row: dict, shown: str, now: str) -> tuple[dict | None, dict | None]:
    """(duplicate, earlier) among the interfaced criticals raised for this
    patient and test in the last INTERFACED_DEDUPE_HOURS: `duplicate` is one
    already raised for the same result (same sender and order) with the same
    value, so nothing is raised again; `earlier` the latest one for the same
    result with another value, which a new critical names."""
    since = _plus_minutes(now, -60 * INTERFACED_DEDUPE_HOURS)
    code = (row.get("observation_code") or "").strip()
    rows = conn.execute(
        f"""SELECT n.id, n.value_display, n.status, n.test_name, n.test_code,
                   ob.obr_filler, ob.obr_placer, ob.hl7_sender, ob.observed_at
            FROM lab_critical_notifications n
            LEFT JOIN {lab_observations.from_sql("ob")} ON ob.id = n.observation_id
            WHERE n.source_lane = ? AND n.patient_id = ? AND COALESCE(n.test_code, '') = ?
              AND julianday(n.raised_at) >= julianday(?)
            ORDER BY julianday(n.raised_at) DESC, n.rowid DESC""",
        (SOURCE_LANE_INTERFACED, row["patient_id"], code, since),
    ).fetchall()
    earlier = None
    for r in rows:
        r = dict(r)
        if not code and (r.get("test_name") or "") != (row.get("observation_name") or ""):
            continue
        if (r.get("hl7_sender") or "").strip() != (row.get("hl7_sender") or "").strip():
            continue
        if not _same_result(r, row):
            continue
        if r["value_display"] == shown:
            return r, None
        earlier = earlier or r
    return None, earlier


def ensure_system_lab_user(conn) -> None:
    """The users row an interfaced critical is posted as (messages.sender_id
    names a user). Created with a password no login matches and is_active 0,
    and kept inactive: this identity never signs in."""
    conn.execute(
        "INSERT OR IGNORE INTO users (id, name, role, department, password_hash, is_active) "
        "VALUES (?, ?, ?, NULL, 'system', 0)",
        (SYSTEM_LAB_ID, SYSTEM_LAB_NAME, SYSTEM_LAB_ROLE),
    )
    conn.execute(
        "UPDATE users SET is_active = 0 WHERE id = ? AND COALESCE(is_active, 1) != 0",
        (SYSTEM_LAB_ID,),
    )


_SYSTEM_LAB_ACTOR = {"id": SYSTEM_LAB_ID, "role": "system"}


def raise_interfaced(
    observation_id: Any,
    *,
    backfill: bool = False,
    value_as_sent: Any = None,
    now: str | None = None,
) -> str | None:
    """Raise the interfaced critical for one stored result, in its own
    immediate transaction, with its 'raised' event and a fail-closed
    clinical_access audit row ('lab_critical_raised', actor system_lab).
    Returns the notification id to deliver, or None when nothing is raised:
    ORB_LAB_CRITICAL_INTERFACED off, a backfill, a result that does not
    qualify (_interfaced_skip), one already raised for this result, or the
    same result and value raised in the last INTERFACED_DEDUPE_HOURS.

    Called by the ingestion pipeline AFTER the result committed, so a failure
    here raises out to the caller (which logs it) and never undoes the
    result. `value_as_sent` is the value as the message carried it (OBX-5),
    shown in preference to the stored number so the laboratory's own
    precision is kept."""
    if backfill or not config.feature_enabled("lab_critical_interfaced"):
        return None
    stamp = now or utc_iso_now()
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            return None
        row = _observation(conn, observation_id)
        if row is None or _interfaced_skip(row) is not None:
            return None
        oid = int(row["id"])
        if (
            conn.execute(
                "SELECT 1 FROM lab_critical_notifications WHERE observation_id = ?", (oid,)
            ).fetchone()
            is not None
        ):
            return None
        if (
            conn.execute("SELECT 1 FROM patients WHERE id = ?", (row["patient_id"],)).fetchone()
            is None
        ):
            return None
        shown = _observation_value(row, value_as_sent)
        duplicate, earlier = _interfaced_earlier(conn, row, shown, stamp)
        if duplicate is not None:
            logger.info(
                "Interfaced critical for result %s not raised again: the same result "
                "was raised as %s",
                oid,
                duplicate["id"],
            )
            return None
        from permissions import PATIENT_DEPARTMENT_SQL

        patient = conn.execute(
            f"SELECT p.id, {PATIENT_DEPARTMENT_SQL} AS department FROM patients p WHERE p.id = ?",
            (row["patient_id"],),
        ).fetchone()
        flag = interfaced_flag(row.get("abnormal_flag"), row.get("raw_segment_ref"))
        test_name = (row.get("observation_name") or row.get("observation_code") or "Result").strip()
        policy = policy_snapshot()
        ensure_system_lab_user(conn)
        nid = f"LC-{uuid.uuid4().hex[:20]}"
        try:
            conn.execute(
                """INSERT INTO lab_critical_notifications
                   (id, source_lane, observation_id, patient_id, test_code, test_name,
                    value_display, preliminary, raised_at, raised_by, raised_in_department,
                    current_tier, tier_due_at, delivery_status, policy_json, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 1, ?, ?, ?, 'open')""",
                (
                    nid,
                    SOURCE_LANE_INTERFACED,
                    oid,
                    row["patient_id"],
                    row.get("observation_code"),
                    test_name,
                    shown,
                    stamp,
                    SYSTEM_LAB_ID,
                    patient["department"] if patient is not None else None,
                    due_at(stamp, policy["tiers"], 1),
                    DELIVERY_PENDING,
                    json.dumps(policy, default=str),
                ),
            )
        except DBIntegrityError:
            # Raised for this result in the meantime.
            return None
        metadata = {
            "source_lane": SOURCE_LANE_INTERFACED,
            "observation_id": oid,
            "trigger": "interfaced",
            "lab_flag": flag,
            "value_display": shown,
            "result_status": row.get("status"),
            "source": row.get("source"),
            "reference_range": row.get("reference_range"),
            "obr_filler": row.get("obr_filler"),
            "obr_placer": row.get("obr_placer"),
            "sender": row.get("hl7_sender"),
            "observed_at": row.get("observed_at"),
            "ladder_configured": policy["configured"],
        }
        if earlier is not None:
            # The sending laboratory reported a different value for this
            # result earlier (a correction).
            metadata.update(
                previous_notification_id=earlier["id"],
                previous_value_display=earlier["value_display"],
            )
        _event(
            conn,
            nid,
            row["patient_id"],
            "raised",
            tier=1,
            actor_id=SYSTEM_LAB_ID,
            now=stamp,
            detail=(
                f"Critical result from the sending laboratory: {test_name} {shown}"
                + (
                    f" (changed from {earlier['value_display']} reported earlier)"
                    if earlier is not None
                    else ""
                )
            ),
            metadata=metadata,
        )
        _audit(
            conn,
            action="lab_critical_raised",
            summary=f"Interfaced laboratory critical result raised: {test_name} {shown}",
            actor=_SYSTEM_LAB_ACTOR,
            notification_id=nid,
            patient_id=row["patient_id"],
            metadata=metadata,
        )
    return nid


def raise_interfaced_and_deliver(
    observation_id: Any, *, backfill: bool = False, value_as_sent: Any = None
) -> dict:
    """raise_interfaced, then deliver it to the case room and push it live.
    NEVER raises: the result is already stored. A failure to raise is logged
    as an error and answered {raised: False, error: True}."""
    try:
        nid = raise_interfaced(observation_id, backfill=backfill, value_as_sent=value_as_sent)
    except Exception:
        logger.error(
            "Interfaced laboratory critical for result %s could not be raised; the "
            "result is stored and nobody was told by Orb",
            observation_id,
            exc_info=True,
        )
        return {"raised": False, "error": True, "notification_id": None}
    if nid is None:
        return {"raised": False, "error": False, "notification_id": None}
    (answer,) = deliver_all([nid])
    return {"raised": True, "error": False, **answer}


def _interfaced_now(conn, n: dict) -> dict:
    """What has happened to an interfaced result since its critical was
    raised, from the sending laboratory's own corrections: {status, changed,
    text, value_now}. A correction replaced the stored result when its
    superseded_by is set (ORB_RESULT_CORRECTIONS); the chain is followed to
    the result that stands now."""
    row = _observation(conn, n.get("observation_id"))
    if row is None:
        return {"status": None, "changed": False, "text": None, "value_now": None}
    head, seen = row, {int(row["id"])}
    while head.get("superseded_by") is not None and len(seen) < CHAIN_MAX:
        nxt = _observation(conn, head["superseded_by"])
        if nxt is None or int(nxt["id"]) in seen:
            break
        head = nxt
        seen.add(int(nxt["id"]))
    status = str(head.get("status") or "final").strip().lower()
    told = n.get("value_display")
    replaced = int(head["id"]) != int(row["id"])
    if status in lab_observations.VOID_OBSERVATION_STATUSES and status != "superseded":
        return {
            "status": "withdrawn",
            "changed": True,
            "value_now": None,
            "text": (
                "Withdrawn by the sending laboratory after this was raised. Check the "
                "chart before acting on it."
            ),
        }
    if status == "superseded" or (head.get("superseded_by") is not None):
        return {
            "status": "corrected",
            "changed": True,
            "value_now": None,
            "text": (
                "Corrected by the sending laboratory after this was raised. Check the "
                "chart for the corrected value."
            ),
        }
    if replaced:
        now_value = _observation_value(head)
        if now_value == told:
            return {
                "status": "corrected",
                "changed": False,
                "value_now": now_value,
                "text": (
                    "Corrected by the sending laboratory after this was raised; the "
                    "value is unchanged."
                ),
            }
        return {
            "status": "corrected",
            "changed": True,
            "value_now": now_value,
            "text": (
                f"Corrected by the sending laboratory after this was raised: now "
                f"{now_value}. Check the chart."
            ),
        }
    corrected = status in ("c", "corrected", "amended")
    return {
        "status": "corrected" if corrected else "final",
        "changed": False,
        "value_now": told,
        "text": (
            "Corrected result from the sending laboratory"
            if corrected
            else "Final result from the sending laboratory"
        ),
    }


def _interfaced_message_text(conn, n: dict) -> str:
    """The case-room words for an interfaced critical: the sending
    laboratory's value, its own flag and the reference range it sent, and
    what the ward does next. No score and no severity of Orb's own."""
    patient = conn.execute(
        "SELECT name, mrn FROM patients WHERE id = ?", (n["patient_id"],)
    ).fetchone()
    name = (patient["name"] if patient is not None else None) or "this patient"
    uhid = patient["mrn"] if patient is not None and patient["mrn"] else None
    who = name + (f", UHID {uhid}" if uhid else "")
    row = _observation(conn, n.get("observation_id")) or {}
    flag = interfaced_flag(row.get("abnormal_flag"), row.get("raw_segment_ref"))
    flag_words = (LAB_FLAG_WORDS.get(flag or "") or "Critical").lower()
    reference = (row.get("reference_range") or "").strip()
    range_words = f"; its reference range is {reference}" if reference else ""
    tiers = _policy(n)["tiers"]
    tier = int(n.get("current_tier") or 1)
    climbing = ""
    if tier > 1:
        calling = _tier_words(tiers, tier)
        climbing = "Not yet acknowledged with a read-back. " + (
            f"Now calling: {calling}. " if calling else ""
        )
    since = _interfaced_now(conn, n)
    state = ""
    if since.get("changed") and since.get("text"):
        state = f" {since['text']}"
    elif since.get("status") == "corrected":
        state = " This is a corrected result."
    return (
        f"{climbing}Critical result from the sending laboratory for {who}: "
        f"{n['test_name']} {n['value_display']}, flagged {flag_words} by the sending "
        f"laboratory{range_words}.{state} Orb passes on the laboratory's own flag. "
        "Acknowledge it with a read-back on the ward screen, or phone the laboratory."
    )


# ════════════════════════════════════════════════════════════════════════════
#  Delivery
# ════════════════════════════════════════════════════════════════════════════


def idempotency_key(notification_id: str, tier: int, revision: int = 0) -> str:
    """The key one post is made under: one per tier, and one per tier for
    each value change recorded on the notification ('r<n>')."""
    base = f"lab-critical:{notification_id}:t{int(tier)}"
    return f"{base}:r{int(revision)}" if revision else base


def _kind(n: dict, changes: list[dict], raised: dict) -> str:
    """What a post for this notification tells the ward now."""
    if changes:
        return KIND_CHANGED if changes[-1].get("still_critical") else KIND_CORRECTED
    return KIND_CHANGED if raised.get("previous_value_display") else KIND_RAISED


#: The version of a result that stands now, with what a post says of it.
_HEAD_SELECT = """
    SELECT r.id, r.status, r.version, r.value_text, r.unit, r.is_critical,
           r.critical_limit_id, r.tech_validated_at, r.released_at,
           cl.display_text AS limit_text, cl.approved_by AS limit_approved_by,
           vu.name AS validated_by_name, ru.name AS released_by_name
    FROM lab_results r
    LEFT JOIN lab_critical_limits cl ON cl.id = r.critical_limit_id
    LEFT JOIN users vu ON vu.id = r.tech_validated_by
    LEFT JOIN users ru ON ru.id = r.released_by
    WHERE r.id = ?"""


def _head_row(conn, result_id: Any) -> dict | None:
    """The version of this result that stands now (head_result_id), with its
    limit and who validated and released it, or None when there is no row."""
    head = head_result_id(conn, result_id)
    if head is None:
        return None
    row = conn.execute(_HEAD_SELECT, (head,)).fetchone()
    return dict(row) if row is not None else None


def _crosses_approved_limit(row: dict) -> bool:
    """Whether a result row is flagged critical against an approved limit:
    the same test as _approved_limit, read from a _HEAD_SELECT row."""
    return (
        int(row.get("is_critical") or 0) == 1
        and bool(row.get("critical_limit_id"))
        and bool(row.get("limit_approved_by"))
    )


def _message_text(conn, n: dict) -> str:
    """The plain words posted to the case room for this notification's
    current tier, written from the result as it stands now (the version that
    stands at the head of its chain, never the version first raised or the
    one after it): a critical value the ward has not been sent before says
    what it replaces, a value that is no longer critical is posted as a
    correction, a result back on the bench says it may change, and an
    amended or withdrawn result says so. No score, no severity of Orb's own:
    the laboratory's value, the limit its pathologist agreed, and what
    happens next."""
    from services.hospital_time import hospital_stamp

    if n.get("source_lane") == SOURCE_LANE_INTERFACED:
        return _interfaced_message_text(conn, n)
    patient = conn.execute(
        "SELECT name, mrn FROM patients WHERE id = ?", (n["patient_id"],)
    ).fetchone()
    result = _head_row(conn, n.get("result_id"))
    changes = _changes(conn, n["id"])
    told = _told(n, changes)
    reported = _critical_told(n, changes)
    name = (patient["name"] if patient is not None else None) or "this patient"
    uhid = patient["mrn"] if patient is not None and patient["mrn"] else None
    who = name + (f", UHID {uhid}" if uhid else "")
    test = n["test_name"]
    status_now = result["status"] if result is not None else None
    # Amended after it was raised: the version standing now is not the one
    # this critical was raised for.
    amended = (
        result is not None
        and n.get("result_id") is not None
        and int(result["id"]) != int(n["result_id"])
    )
    tiers = _policy(n)["tiers"]
    tier = int(n.get("current_tier") or 1)
    amended_lead = ""
    if (
        result is not None
        and int(result["version"] or 1) > 1
        and status_now not in ("superseded", "withdrawn")
    ):
        amended_lead = "Amended result: this corrects a value reported earlier. "
    climbing = ""
    if tier > 1:
        calling = _tier_words(tiers, tier)
        climbing = "Not yet confirmed with a read-back. " + (
            f"The laboratory is now calling: {calling}. " if calling else ""
        )
    lead = amended_lead + climbing
    tail = (
        "The laboratory will phone to confirm it with a read-back. If you need the "
        "laboratory sooner, phone it."
    )

    def correction_of(value: str) -> str:
        return (
            f"Correction to a critical laboratory result for {who}: the laboratory "
            f"reported {test} {value} as critical."
        )

    correction = correction_of(reported)
    if status_now == "withdrawn":
        return (
            f"{climbing}{correction} That result has been withdrawn because it was entered "
            f"in error, and it is no longer on the chart. Do not act on it. {tail}"
        )
    if status_now == "superseded":
        # The newest version says it was amended but names no newer one.
        return (
            f"{climbing}{correction} The result has been amended. The chart shows the "
            f"amended value. {tail}"
        )
    # Authorised or not is read from the result as it stands, never from the
    # notification's preliminary column, which keeps how the value was last
    # raised: a release that does not change the value posts nothing then, so
    # the column still says preliminary on every later tier. The column
    # decides only when there is no result row to read.
    if status_now == "entered":
        state = (
            "The laboratory is checking this result again, so the value may change. It "
            "will phone with the checked result."
        )
    elif status_now == "tech_validated" or (status_now != "released" and n.get("preliminary")):
        state = "Preliminary, not yet authorised."
        if result is not None and result["validated_by_name"]:
            when = (
                hospital_stamp(result["tech_validated_at"]) if result["tech_validated_at"] else ""
            )
            state += (
                f" Validated by {result['validated_by_name']}"
                + (f" at {when}" if when else "")
                + "."
            )
    else:
        state = "Authorised."
        if result is not None and result["released_by_name"]:
            when = hospital_stamp(result["released_at"]) if result["released_at"] else ""
            state = (
                f"Authorised by {result['released_by_name']}"
                + (f" at {when}" if when else "")
                + "."
            )
    limit_words = (
        f" (agreed critical limit: {result['limit_text']})"
        if result is not None and result["limit_text"]
        else ""
    )
    now_value = (
        value_display(result) or None
        if amended
        and result is not None
        and status_now in ("tech_validated", "released")
        and result["value_text"] is not None
        else None
    )
    if now_value is not None:
        # Written from the amended version itself, so a change no step
        # recorded is still never posted as the value first raised. `prior`
        # is what the ward had been told before the value standing now.
        prior = (
            changes[:-1] if changes and str(changes[-1].get("to_value")) == now_value else changes
        )
        assert result is not None
        assert result is not None
        if not _crosses_approved_limit(result):
            return (
                f"{climbing}{correction_of(_critical_told(n, prior))} The result has been "
                f"amended: it is now {now_value}, which is not critical. The chart shows "
                f"the amended value. {tail}"
            )
        # Still critical: said like any changed critical value (the bench
        # lane's words), from the value the ward was last given.
        earlier_told = _told(n, prior)
        if earlier_told != now_value:
            lead += (
                f"Changed result: the laboratory reported {earlier_told} for this test "
                f"earlier; the value is now {now_value}. "
            )
        return (
            f"{lead}Critical laboratory result for {who}: {test} {now_value}{limit_words}. "
            f"{state} {tail}"
        )
    if changes and not changes[-1].get("still_critical"):
        return f"{lead}{correction} The value is now {told}, which is not critical. {state} {tail}"
    head = f"Critical laboratory result for {who}: {test} {told}{limit_words}."
    earlier = (
        changes[-1].get("from_value")
        if changes
        else _raised_meta(conn, n["id"]).get("previous_value_display")
    )
    if earlier and earlier != told:
        lead += (
            f"Changed result: the laboratory reported {earlier} for this test earlier; "
            f"the value is now {told}. "
        )
    return f"{lead}{head} {state} {tail}"


#: Counts only the events after a notification's latest value change, so each
#: change gets its own attempts. `e` is the lab_critical_events row counted.
_SINCE_LAST_CHANGE_SQL = f"""
    AND e.id > COALESCE((SELECT MAX(c.id) FROM lab_critical_events c
                          WHERE c.notification_id = e.notification_id
                            AND c.event_type = 'escalated'
                            AND (CASE WHEN json_valid(c.metadata_json)
                                      THEN json_extract(c.metadata_json, '$.reason') END)
                                = '{CHANGE_REASON}'), 0)"""


def _attempts(conn, notification_id: str, tier: int) -> int:
    """Posts tried on this tier since the notification's latest value change."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM lab_critical_events e WHERE e.notification_id = ? AND e.tier = ? "
            "AND e.event_type IN ('posted', 'delivery_failed')" + _SINCE_LAST_CHANGE_SQL,
            (notification_id, tier),
        ).fetchone()[0]
    )


def _mark_undelivered(conn, n: dict, status: str, reason: str, now: str, *, attempt: int) -> None:
    tier = int(n["current_tier"])
    # Due at once: a tier already due keeps the time it fell due, so a
    # failed post never makes a critical look less overdue than it is.
    conn.execute(
        "UPDATE lab_critical_notifications SET delivery_status = ?, "
        "tier_due_at = CASE WHEN tier_due_at IS NOT NULL "
        "AND julianday(tier_due_at) <= julianday(?) THEN tier_due_at ELSE ? END "
        "WHERE id = ? AND status = 'open' AND current_tier = ?",
        (status, now, now, n["id"], tier),
    )
    words = {
        DELIVERY_NO_CASE_ROOM: "Not delivered: this patient has no case room. Phone the ward.",
        REASON_CASE_ROOM_CLOSED: (
            "Not delivered: this patient has been discharged, so the case "
            "room is closed. Phone the discharging consultant."
        ),
        DELIVERY_FAILED: "Not delivered: the case-room post failed. Phone the ward.",
    }
    _event(
        conn,
        n["id"],
        n["patient_id"],
        "delivery_failed",
        tier=tier,
        actor_id=None,
        now=now,
        detail=words.get(reason, words[DELIVERY_FAILED]),
        metadata={
            "reason": reason,
            "attempt": attempt,
            "idempotency_key": idempotency_key(n["id"], tier, len(_changes(conn, n["id"]))),
        },
    )


def deliver_in_txn(conn, notification_id: str, *, now: str) -> dict:
    """Post the notification's current tier on the caller's write
    transaction. Returns {posted, reason, tier, kind, message_id,
    channel_id, msg_data}; `kind` says what the post tells the ward
    (KIND_RAISED, KIND_CHANGED or KIND_CORRECTED); msg_data is for the
    caller to push after commit (None when nothing new was written). The
    key names the tier and the notification's latest value change, so a
    change is posted once at each tier and the value first raised never
    blocks it."""
    row = conn.execute(
        "SELECT * FROM lab_critical_notifications WHERE id = ?", (notification_id,)
    ).fetchone()
    if row is None:
        return {"posted": False, "reason": "not_found", "tier": None, "kind": None}
    n = dict(row)
    tier = int(n["current_tier"])
    changes = _changes(conn, notification_id)
    out: dict[str, Any] = {
        "posted": False,
        "reason": None,
        "tier": tier,
        "kind": _kind(n, changes, _raised_meta(conn, notification_id)),
        "message_id": None,
        "channel_id": None,
        "msg_data": None,
    }
    if n["status"] != "open":
        out["reason"] = "not_open"
        return out
    key = idempotency_key(notification_id, tier, len(changes))
    earlier = case_room_alerts.message_for_key(conn, key)
    if earlier is not None:
        # This tier was posted already (a retry after a crash between the post
        # and its bookkeeping): never a second post.
        conn.execute(
            "UPDATE lab_critical_notifications SET delivery_status = ?, relay_message_id = ? "
            "WHERE id = ? AND status = 'open' AND current_tier = ?",
            (DELIVERY_POSTED, int(earlier["id"]), notification_id, tier),
        )
        out.update(
            posted=True,
            reason="already_posted",
            message_id=int(earlier["id"]),
            channel_id=earlier.get("channel_id"),
        )
        return out
    attempt = _attempts(conn, notification_id, tier) + 1
    patient = conn.execute(
        "SELECT status FROM patients WHERE id = ?", (n["patient_id"],)
    ).fetchone()
    if patient is not None and patient["status"] == "discharged":
        # relay_service.get_channels_for_user hides a discharged patient's case
        # room from every clinician, so a post there would tell nobody.
        _mark_undelivered(
            conn, n, DELIVERY_NO_CASE_ROOM, REASON_CASE_ROOM_CLOSED, now, attempt=attempt
        )
        out["reason"] = REASON_CASE_ROOM_CLOSED
        return out
    # The laboratory lane posts as the technologist who validated the result;
    # the interfaced lane as the service identity system_lab (lis-22), marked
    # as automated (is_bot), because no person in this hospital sent it.
    interfaced = n.get("source_lane") == SOURCE_LANE_INTERFACED
    if interfaced:
        ensure_system_lab_user(conn)
        sender: str | None
        sender = SYSTEM_LAB_ID
    else:
        sender = n.get("raised_by")
    if not sender or conn.execute("SELECT 1 FROM users WHERE id = ?", (sender,)).fetchone() is None:
        _mark_undelivered(conn, n, DELIVERY_FAILED, DELIVERY_FAILED, now, attempt=attempt)
        out["reason"] = DELIVERY_FAILED
        return out
    logged, message_id, channel_id, msg_data = case_room_alerts.persist_case_room_alert(
        n["patient_id"],
        _message_text(conn, n),
        sender_id=sender,
        sender_name=INTERFACED_SENDER_NAME if interfaced else SENDER_NAME,
        sender_role=SENDER_ROLE,
        is_bot=interfaced,
        source=INTERFACED_MESSAGE_SOURCE if interfaced else MESSAGE_SOURCE,
        db=conn,
        message_type=MESSAGE_TYPE,
        idempotency_key=key,
    )
    if not logged:
        _mark_undelivered(
            conn, n, DELIVERY_NO_CASE_ROOM, DELIVERY_NO_CASE_ROOM, now, attempt=attempt
        )
        out["reason"] = DELIVERY_NO_CASE_ROOM
        return out
    conn.execute(
        "UPDATE lab_critical_notifications SET delivery_status = ?, relay_message_id = ? "
        "WHERE id = ? AND status = 'open' AND current_tier = ?",
        (DELIVERY_POSTED, message_id, notification_id, tier),
    )
    _event(
        conn,
        notification_id,
        n["patient_id"],
        "posted",
        tier=tier,
        actor_id=sender,
        now=now,
        detail="Posted in the patient's case room",
        metadata={
            "message_id": message_id,
            "channel_id": channel_id,
            "attempt": attempt,
            "idempotency_key": key,
        },
    )
    out.update(posted=True, message_id=message_id, channel_id=channel_id, msg_data=msg_data)
    return out


def _record_failure(notification_id: str) -> None:
    """A delivery that raised: recorded in its own transaction as not
    delivered, with the tier due at once. Never raises."""
    try:
        with db_session(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM lab_critical_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if row is None or row["status"] != "open":
                return
            n = dict(row)
            _mark_undelivered(
                conn,
                n,
                DELIVERY_FAILED,
                DELIVERY_FAILED,
                utc_iso_now(),
                attempt=_attempts(conn, notification_id, int(n["current_tier"])) + 1,
            )
    except Exception:
        logger.exception(
            "Could not record the failed delivery of laboratory critical %s", notification_id
        )


def _kind_of(notification_id: str) -> str | None:
    """What a post for this notification tells the ward, read on its own
    connection for a delivery that raised. Never raises."""
    try:
        with db_session() as conn:
            row = conn.execute(
                "SELECT * FROM lab_critical_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if row is None:
                return None
            return _kind(
                dict(row), _changes(conn, notification_id), _raised_meta(conn, notification_id)
            )
    except Exception:
        logger.exception("Could not read laboratory critical %s", notification_id)
        return None


def deliver(notification_id: str, *, push: bool = True) -> dict:
    """Post one notification's current tier, AFTER the step that raised or
    escalated it has committed. NEVER raises: the validation, release or
    amendment already stands. A failure is logged, recorded as not delivered
    (the tier due at once) and answered as {posted: False, reason: 'failed'}.
    With `push` the new message goes out live as 'message:new'; the sweep
    passes push=False and emits on the loop itself."""
    try:
        with db_session(immediate=True) as conn:
            out = deliver_in_txn(conn, notification_id, now=utc_iso_now())
    except Exception:
        logger.exception(
            "Laboratory critical %s could not be posted to the case room; it stays open and due",
            notification_id,
        )
        _record_failure(notification_id)
        return {
            "posted": False,
            "reason": DELIVERY_FAILED,
            "tier": None,
            "kind": _kind_of(notification_id),
            "message_id": None,
            "channel_id": None,
            "msg_data": None,
        }
    if push and out.get("msg_data"):
        case_room_alerts.push_message_new(
            out["msg_data"], out["channel_id"], what=f"laboratory critical {notification_id}"
        )
    return out


def deliver_all(notification_ids: list[str]) -> list[dict]:
    """deliver() each, in order, and answer what the screen needs: whether
    the ward was told in the case room, why not, and what the post told
    them ('raised', 'changed' or 'corrected')."""
    answers = []
    for nid in notification_ids:
        out = deliver(nid)
        answers.append(
            {
                "notification_id": nid,
                "posted": bool(out.get("posted")),
                "reason": out.get("reason") if not out.get("posted") else None,
                "kind": out.get("kind"),
                "message_id": out.get("message_id"),
            }
        )
    if notification_ids:
        lab_module.refresh_drain_state()
    return answers


# ════════════════════════════════════════════════════════════════════════════
#  The callback log
# ════════════════════════════════════════════════════════════════════════════


def _require_log_permission(actor: dict) -> None:
    if (
        not actor
        or not actor.get("id")
        or not has_permission(actor.get("role", ""), LAB_CRITICAL_LOG)
    ):
        raise LabCriticalError(
            "permission_denied", "Only laboratory staff log a critical-result callback.", status=403
        )


def _clean(value: Any, *, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:limit]


def _open_notification(conn, notification_id: str, dept: str | None) -> dict:
    from permissions import patient_in_scope

    if not _tables_present(conn):
        raise LabCriticalError("not_found", "Critical result not found", status=404)
    row = conn.execute(
        "SELECT * FROM lab_critical_notifications WHERE id = ?", (str(notification_id),)
    ).fetchone()
    if row is None or not patient_in_scope(conn, row["patient_id"], dept):
        raise LabCriticalError("not_found", "Critical result not found", status=404)
    n = dict(row)
    if n["status"] != "open":
        by = None
        if n.get("acknowledged_by"):
            who = conn.execute(
                "SELECT name FROM users WHERE id = ?", (n["acknowledged_by"],)
            ).fetchone()
            by = who["name"] if who else None
        raise LabCriticalError(
            "already_closed",
            "This critical result is already closed" + (f" by {by}" if by else "") + ".",
            closed_by_name=by,
            acknowledged_at=n.get("acknowledged_at"),
        )
    return n


def _value_shown(value_told: Any, *, ward: bool = False) -> str:
    """The value a callback or an attempt says its screen showed as told to
    the ward, required (422 'value_told_required'): the stale-screen guard,
    like `expected` on a laboratory result. `ward` words it for the ward
    card's acknowledgement."""
    shown = _clean(value_told, limit=VALUE_TOLD_MAX)
    if not shown:
        raise LabCriticalError(
            "value_told_required",
            (
                "This screen did not say which value you read back. Reload the screen and "
                "acknowledge it again."
                if ward
                else "This screen did not say which value the call was about. Reload Critical results "
                "and log the call again."
            ),
            status=422,
        )
    return shown


def _told_as_shown(conn, n: dict, shown: str) -> str:
    """The value the ward was last told for notification `n`, read on the
    caller's write transaction, refused 409 'value_changed' (nothing is
    written) when it is not the value the screen showed: the result was sent
    back and validated again with another value after the screen loaded, so
    the call was about a value that no longer stands, and logging it would
    record a read-back of a value nobody phoned."""
    told = _told(n, _changes(conn, n["id"]))
    if _clean(told, limit=VALUE_TOLD_MAX) != shown:
        raise LabCriticalError(
            "value_changed",
            f"The value changed after this screen loaded: the ward has now been told "
            f"{_told_phrase(n['test_name'], told)}. Nothing was logged. Phone the ward about "
            + ("the withdrawal" if told == WITHDRAWN_TOLD else told)
            + ", ask for the read-back and log the call again.",
            value_told=told,
            value_shown=shown,
        )
    return told


def log_callback(
    notification_id: str,
    *,
    actor: dict,
    recipient_name: Any,
    recipient_role: Any,
    method: Any,
    read_back: Any,
    note: Any = None,
    value_told: Any = None,
    dept: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    """Close a critical with the laboratory's callback (ruling LIS-R3,
    closure (i)), in ONE immediate transaction: who was told (a name and one
    of RECIPIENT_ROLES), how (phone or in person), and that they read the
    value back, which is required (422 'read_back_required' otherwise). The
    notification becomes 'acknowledged'; 'callback_logged' and
    'acknowledged' events and a fail-closed clinical_access audit row
    ('lab_critical_callback_logged') land with it, naming the value the ward
    was last told for it (value_told), which is the value read back. A
    closed one is 409 'already_closed'.

    `value_told` is the value the screen showed (required, 422
    'value_told_required'). When it is not the value the ward has been told
    by the time the transaction runs (the result was validated again with
    another value meanwhile), the callback is refused 409 'value_changed'
    with the value now told and nothing is written: the critical stays open
    until a call about the value as it stands is logged."""
    _require_log_permission(actor)
    name = _clean(recipient_name, limit=NAME_MAX)
    if len(name) < 2:
        raise LabCriticalError(
            "recipient_required", "Type the name of the person you told.", status=422
        )
    role = str(recipient_role or "").strip()
    if role not in RECIPIENT_ROLES:
        raise LabCriticalError(
            "recipient_role_required",
            "Choose the role of the person you told.",
            status=422,
            allowed=list(RECIPIENT_ROLES),
        )
    how = str(method or "").strip()
    if how not in CALLBACK_METHODS:
        raise LabCriticalError(
            "method_required",
            "Choose how you told them.",
            status=422,
            allowed=list(CALLBACK_METHODS),
        )
    if read_back is not True:
        raise LabCriticalError(
            "read_back_required",
            "Ask them to read the result back to you before you log the call. If you could "
            "not reach anyone, log the attempt instead; the result stays open.",
            status=422,
        )
    shown = _value_shown(value_told)
    text = _clean(note, limit=NOTE_MAX) or None
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        n = _open_notification(conn, notification_id, dept)
        told = _told_as_shown(conn, n, shown)
        now = utc_iso_now()
        moved = conn.execute(
            """UPDATE lab_critical_notifications
               SET status = 'acknowledged', acknowledged_at = ?, acknowledged_by = ?,
                   recipient_name = ?, recipient_role = ?, method = ?, read_back = 1
               WHERE id = ? AND status = 'open'""",
            (now, actor_id, name, role, how, n["id"]),
        )
        if moved.rowcount != 1:
            raise LabCriticalError("already_closed", "This critical result is already closed.")
        detail = {
            "recipient_name": name,
            "recipient_role": role,
            "method": how,
            "read_back": True,
            "reached": True,
            "note": text,
            "value_told": told,
        }
        _event(
            conn,
            n["id"],
            n["patient_id"],
            "callback_logged",
            tier=int(n["current_tier"]),
            actor_id=actor_id,
            now=now,
            detail=f"{CALLBACK_METHODS[how]} call to {name} ({RECIPIENT_ROLES[role]}), read back",
            metadata=detail,
        )
        _event(
            conn,
            n["id"],
            n["patient_id"],
            "acknowledged",
            tier=int(n["current_tier"]),
            actor_id=actor_id,
            now=now,
            detail="Closed by the laboratory's callback with a read-back",
            metadata={"closure": "laboratory_callback"},
        )
        _audit(
            conn,
            action="lab_critical_callback_logged",
            summary=(
                f"Laboratory critical result confirmed with a read-back: "
                f"{_told_phrase(n['test_name'], told)}"
            ),
            actor=actor,
            notification_id=n["id"],
            patient_id=n["patient_id"],
            metadata={**detail, "closes": True, "tier": int(n["current_tier"])},
            request_meta=request_meta,
        )
        view = _views(conn, [n["id"]])[0]
    lab_module.refresh_drain_state()
    return {"notification": view}


def log_attempt(
    notification_id: str,
    *,
    actor: dict,
    method: Any,
    note: Any,
    value_told: Any = None,
    dept: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    """Record a call that reached nobody, or nobody who could take it, in ONE
    immediate transaction with a fail-closed audit row. The critical stays
    open and its ladder keeps running. `value_told` is the value the screen
    showed, checked like a callback's (422 'value_told_required', 409
    'value_changed')."""
    _require_log_permission(actor)
    how = str(method or "").strip()
    if how not in CALLBACK_METHODS:
        raise LabCriticalError(
            "method_required", "Choose how you tried.", status=422, allowed=list(CALLBACK_METHODS)
        )
    text = _clean(note, limit=NOTE_MAX)
    if len(text) < 3:
        raise LabCriticalError("note_required", "Say who you tried and what happened.", status=422)
    shown = _value_shown(value_told)
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        n = _open_notification(conn, notification_id, dept)
        told = _told_as_shown(conn, n, shown)
        now = utc_iso_now()
        detail = {
            "method": how,
            "read_back": False,
            "reached": False,
            "note": text,
            "value_told": told,
        }
        _event(
            conn,
            n["id"],
            n["patient_id"],
            "callback_logged",
            tier=int(n["current_tier"]),
            actor_id=actor_id,
            now=now,
            detail=f"{CALLBACK_METHODS[how]} call not answered: {text}",
            metadata=detail,
        )
        _audit(
            conn,
            action="lab_critical_callback_logged",
            summary=(
                f"Laboratory critical result: a call that reached nobody: "
                f"{_told_phrase(n['test_name'], told)}"
            ),
            actor=actor,
            notification_id=n["id"],
            patient_id=n["patient_id"],
            metadata={**detail, "closes": False, "tier": int(n["current_tier"])},
            request_meta=request_meta,
        )
        view = _views(conn, [n["id"]])[0]
    return {"notification": view}


# ════════════════════════════════════════════════════════════════════════════
#  The ward's acknowledgement (lis-16)
# ════════════════════════════════════════════════════════════════════════════


def _ward_told_as_shown(conn, n: dict, shown: str) -> str:
    """_told_as_shown in the ward's words: refused 409 'value_changed' (nothing
    is written) when the value the laboratory has now told the ward is not the
    value this screen showed."""
    told = _told(n, _changes(conn, n["id"]))
    if _clean(told, limit=VALUE_TOLD_MAX) != shown:
        raise LabCriticalError(
            "value_changed",
            f"The value changed after this screen loaded: the laboratory now reports "
            f"{_told_phrase(n['test_name'], told)}. Nothing was recorded. Read the new value "
            "and acknowledge it again.",
            value_told=told,
            value_shown=shown,
        )
    return told


def acknowledge(
    notification_id: str,
    *,
    actor: dict,
    recipient_name: Any,
    read_back: Any,
    value_told: Any,
    method: Any = "in_app",
    recipient_role: Any = None,
    caller_name: Any = None,
    note: Any = None,
    dept: str | None = None,
    request_meta: dict | None = None,
) -> dict:
    """Close a critical with the ward's own read-back acknowledgement (ruling
    LIS-R3, closure (ii)), in ONE immediate transaction: who was told (a
    name, required, 422 'recipient_required'; a role from RECIPIENT_ROLES,
    defaulting from the account's own role), how (WARD_ACK_METHODS: read on
    the ward screen, or a phone call from the laboratory, whose caller may be
    named), and that the value was read back, which is required (422
    'read_back_required'). The notification becomes 'acknowledged', which
    stops its ladder; an 'acknowledged' event (metadata closure
    'ward_read_back') and a fail-closed clinical_access audit row
    ('lab_critical_acknowledged') land with it.

    Only an account holding lab.critical_ack (403 otherwise), and only for a
    patient on the caller's ward (`dept`, department_scope): any other is
    404, exactly like a critical that does not exist. A closed one is 409
    'already_closed'. `value_told` is the value the screen showed (required);
    a different value told since is 409 'value_changed' and writes
    nothing."""
    if (
        not actor
        or not actor.get("id")
        or not has_permission(actor.get("role", ""), LAB_CRITICAL_ACK)
    ):
        raise LabCriticalError(
            "permission_denied",
            "Only a nurse or a doctor on the patient's ward acknowledges a critical result.",
            status=403,
        )
    name = _clean(recipient_name, limit=NAME_MAX)
    if len(name) < 2:
        raise LabCriticalError(
            "recipient_required", "Type the name of the person who was told.", status=422
        )
    how = str(method or "in_app").strip()
    if how not in WARD_ACK_METHODS:
        raise LabCriticalError(
            "method_required",
            "Choose how you were told.",
            status=422,
            allowed=list(WARD_ACK_METHODS),
        )
    role = str(recipient_role or "").strip() or _WARD_ROLE_DEFAULT.get(str(actor.get("role") or ""))
    if role and role not in RECIPIENT_ROLES:
        raise LabCriticalError(
            "recipient_role_required",
            "Choose the role of the person who was told.",
            status=422,
            allowed=list(RECIPIENT_ROLES),
        )
    if read_back is not True:
        raise LabCriticalError(
            "read_back_required",
            "Read the result back before you acknowledge it. Nothing was recorded.",
            status=422,
        )
    shown = _value_shown(value_told, ward=True)
    caller = _clean(caller_name, limit=NAME_MAX) or None
    text = _clean(note, limit=NOTE_MAX) or None
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        n = _open_notification(conn, notification_id, dept)
        told = _ward_told_as_shown(conn, n, shown)
        now = utc_iso_now()
        moved = conn.execute(
            """UPDATE lab_critical_notifications
               SET status = 'acknowledged', acknowledged_at = ?, acknowledged_by = ?,
                   recipient_name = ?, recipient_role = ?, method = ?, read_back = 1
               WHERE id = ? AND status = 'open'""",
            (now, actor_id, name, role or None, how, n["id"]),
        )
        if moved.rowcount != 1:
            raise LabCriticalError("already_closed", "This critical result is already closed.")
        tier = int(n["current_tier"])
        detail = {
            "closure": CLOSURE_WARD_READ_BACK,
            "recipient_name": name,
            "recipient_role": role or None,
            "method": how,
            "caller_name": caller,
            "read_back": True,
            "note": text,
            "value_told": told,
            "source_lane": n["source_lane"],
            "tier": tier,
        }
        _event(
            conn,
            n["id"],
            n["patient_id"],
            "acknowledged",
            tier=tier,
            actor_id=actor_id,
            now=now,
            detail=(
                f"Acknowledged on the ward by {name}"
                + (f" ({RECIPIENT_ROLES[role]})" if role else "")
                + f": {WARD_ACK_METHODS[how].lower()}"
                + (f" ({caller})" if caller and how == "phone" else "")
                + ", read back"
            ),
            metadata=detail,
        )
        _audit(
            conn,
            action="lab_critical_acknowledged",
            summary=(
                f"Laboratory critical result acknowledged on the ward with a read-back: "
                f"{_told_phrase(n['test_name'], told)}"
            ),
            actor=actor,
            notification_id=n["id"],
            patient_id=n["patient_id"],
            metadata={**detail, "closes": True},
            request_meta=request_meta,
        )
        view = _ward_views(conn, [n["id"]])[0]
    lab_module.refresh_drain_state()
    return {"notification": view}


# ════════════════════════════════════════════════════════════════════════════
#  The laboratory's Critical results screen
# ════════════════════════════════════════════════════════════════════════════

#: The interfaced lane's stored result is joined by id through
#: lab_observations.from_sql (lis-22): its flag, the reference range the
#: sending laboratory sent, and the raw segment the flag is read back from.
_LIST_SELECT = f"""
    SELECT n.*, p.name AS patient_name, p.mrn, p.ward, p.bed, p.status AS patient_status,
           (SELECT MAX(ds.discharged_at) FROM discharge_summaries ds
             WHERE ds.patient_id = n.patient_id) AS discharged_at,
           r.status AS result_status, r.value_text AS result_value_text, r.unit AS result_unit,
           r.superseded_by_result_id, r.critical_limit_id, r.abnormal_flag AS result_flag,
           cl.display_text AS critical_limit_text,
           ob.abnormal_flag AS obs_flag, ob.reference_range AS obs_reference_range,
           ob.raw_segment_ref AS obs_raw_segment,
           ru.name AS raised_by_name, au.name AS acknowledged_by_name
    FROM lab_critical_notifications n
    JOIN patients p ON p.id = n.patient_id
    LEFT JOIN lab_results r ON r.id = n.result_id
    LEFT JOIN lab_critical_limits cl ON cl.id = r.critical_limit_id
    LEFT JOIN {lab_observations.from_sql("ob")} ON ob.id = n.observation_id
    LEFT JOIN users ru ON ru.id = n.raised_by
    LEFT JOIN users au ON au.id = n.acknowledged_by"""


def _add_heads(conn, rows: dict[str, dict]) -> None:
    """Put on each listed row the version of its result that stands now
    (head_*): the raised version itself, or for an amended one the version at
    the head of its chain (head_result_id), never one hop. head_flag and
    head_limit_text are that version's own flag and approved limit (the ward
    card shows the laboratory's flag as it stands)."""
    for row in rows.values():
        head = None
        if row.get("result_id") is not None and row.get("superseded_by_result_id") is not None:
            hid = head_result_id(conn, row["result_id"])
            if hid is not None and hid != int(row["result_id"]):
                got = conn.execute(
                    "SELECT r.id, r.status, r.value_text, r.unit, r.abnormal_flag, "
                    "cl.display_text AS limit_text FROM lab_results r "
                    "LEFT JOIN lab_critical_limits cl ON cl.id = r.critical_limit_id "
                    "WHERE r.id = ?",
                    (hid,),
                ).fetchone()
                head = dict(got) if got is not None else None
        if head is None:
            row.update(
                head_id=row.get("result_id"),
                head_status=row.get("result_status"),
                head_value_text=row.get("result_value_text"),
                head_unit=row.get("result_unit"),
                head_flag=row.get("result_flag"),
                head_limit_text=row.get("critical_limit_text"),
            )
        else:
            row.update(
                head_id=head["id"],
                head_status=head["status"],
                head_value_text=head["value_text"],
                head_unit=head["unit"],
                head_flag=head["abnormal_flag"],
                head_limit_text=head["limit_text"],
            )


def _value_now(row: dict) -> str | None:
    """The result's value as it now stands on the laboratory's record: the
    value of the version at the head of its chain (the newest amendment),
    None for a withdrawn one or one back on the bench (a value not yet
    validated is never shown as the result)."""
    status = row.get("head_status", row.get("result_status"))
    text = row.get("head_value_text", row.get("result_value_text"))
    if status in ("tech_validated", "released") and text is not None:
        return (
            value_display(
                {"value_text": text, "unit": row.get("head_unit", row.get("result_unit"))}
            )
            or None
        )
    return None


def _result_now(row: dict, *, told: str | None = None, changes: list[dict] | None = None) -> dict:
    """What has happened to the result since the critical was raised, in
    words: the laboratory must tell the ward when it changed. The result's
    current value (the version at the head of its chain) is compared with
    the value the ward was told (`told`); `changes` are the value changes
    recorded on the notification. 'changed' says the value the ward was
    first given no longer stands as it was."""
    status = row.get("head_status", row.get("result_status"))
    amended = row.get("result_status") == "superseded" or (
        row.get("head_id") is not None
        and row.get("result_id") is not None
        and int(row["head_id"]) != int(row["result_id"])
    )
    told = told or row.get("value_display")
    changes = changes or []
    is_open = row.get("status") == "open"
    last = changes[-1] if changes else None
    if status in ("released", "tech_validated"):
        if amended:
            key, base = "amended", "Amended after this was raised"
        else:
            key = status
            base = (
                "Authorised and on the chart"
                if status == "released"
                else "Preliminary, not yet authorised"
            )
        now_value = _value_now(row)
        if now_value is not None and now_value != told:
            # No step recorded this change: the screen still never hides it.
            text = (
                f"{base}: now {now_value}. Tell the ward."
                if amended
                else f"{base}. Changed after this was raised: now {now_value}. Tell the ward."
            )
            return {"status": key, "changed": True, "text": text}
        if last is not None:
            if last.get("still_critical"):
                text = (
                    f"{base}: was {last.get('from_value')}, now {told}, still critical."
                    if amended
                    else f"{base}. Changed after it was raised: was {last.get('from_value')}, "
                    f"now {told}, still critical."
                )
                ask = " Phone the ward to confirm the new value with a read-back."
            else:
                text = (
                    f"{base}: now {told}, not critical. It was reported as critical at "
                    f"{last.get('critical_value') or row.get('value_display')}."
                )
                ask = " Phone the ward to tell them the value they were given has changed."
            return {"status": key, "changed": True, "text": text + (ask if is_open else "")}
        if amended:
            return {"status": key, "changed": False, "text": f"{base}; the value is unchanged."}
        return {"status": status, "changed": False, "text": base}
    if status == "entered":
        return {
            "status": "entered",
            "changed": False,
            "text": "Sent back to the bench after this was raised. Tell the ward.",
        }
    if status == "superseded":
        # The newest version says it was amended but names no newer one.
        return {
            "status": "amended",
            "changed": True,
            "text": "Amended after this was raised. Tell the ward.",
        }
    if status == "withdrawn":
        if not is_open and last is not None and last.get("to_value") == WITHDRAWN_TOLD:
            return {
                "status": "withdrawn",
                "changed": False,
                "text": "Withdrawn after this was raised. The ward was told it was entered "
                "in error.",
            }
        return {
            "status": "withdrawn",
            "changed": False,
            "text": "Withdrawn after this was raised. Tell the ward it was entered in error.",
        }
    return {"status": status, "changed": False, "text": None}


def _delivery_words(row: dict, last_failure: dict | None) -> str:
    status = row.get("delivery_status")
    if status == DELIVERY_POSTED:
        return "Posted in the patient's case room."
    if status == DELIVERY_PENDING:
        return "Posting to the patient's case room."
    reason = (last_failure or {}).get("reason")
    if reason == REASON_CASE_ROOM_CLOSED:
        return (
            "Not delivered: the patient has been discharged, so the case room is closed. "
            "Phone the discharging consultant."
        )
    if status == DELIVERY_NO_CASE_ROOM:
        return "Not delivered: no case room. Phone the ward."
    return "Not delivered: the case-room post failed. Phone the ward."


def _events_by_notification(conn, ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {i: [] for i in ids}
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        marks = ", ".join("?" for _ in chunk)
        for r in conn.execute(
            f"""SELECT e.id, e.notification_id, e.event_type, e.tier, e.occurred_at, e.detail,
                           e.metadata_json, u.name AS actor_name
                    FROM lab_critical_events e
                    LEFT JOIN users u ON u.id = e.actor_user_id
                    WHERE e.notification_id IN ({marks})
                    ORDER BY e.id""",
            tuple(chunk),
        ).fetchall():
            try:
                meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except ValueError:
                meta = {}
            out[r["notification_id"]].append(
                {
                    "id": r["id"],
                    "event_type": r["event_type"],
                    "tier": r["tier"],
                    "at": r["occurred_at"],
                    "detail": r["detail"],
                    "by_name": r["actor_name"],
                    "metadata": meta if isinstance(meta, dict) else {},
                }
            )
    return out


def _lab_flag(row: dict) -> str | None:
    """The laboratory's own flag on a listed row as it stands: the head
    version's flag for the laboratory lane, the sending laboratory's critical
    flag for the interfaced lane. None when there is none."""
    if row.get("source_lane") == SOURCE_LANE_INTERFACED:
        return interfaced_flag(row.get("obs_flag"), row.get("obs_raw_segment"))
    flag = str(row.get("head_flag", row.get("result_flag")) or "").strip().upper()
    return flag or None


def _closure(events: list[dict]) -> str | None:
    """How a closed critical was closed: the metadata closure of its latest
    'acknowledged' event (CLOSURE_LABORATORY_CALLBACK or
    CLOSURE_WARD_READ_BACK)."""
    for e in reversed(events):
        if e["event_type"] == "acknowledged":
            return e["metadata"].get("closure")
    return None


def _ack_method_text(method: str | None, closure: str | None) -> str | None:
    if closure == CLOSURE_WARD_READ_BACK:
        return WARD_ACK_METHODS.get(method or "")
    return CALLBACK_METHODS.get(method or "")


def _view(row: dict, events: list[dict], now: str, interfaced_now: dict | None = None) -> dict:
    policy = _policy(row)
    tiers = policy["tiers"]
    tier = int(row.get("current_tier") or 1)
    changes = [
        {**e["metadata"], "event_id": e["id"], "at": e["at"], "by_name": e["by_name"]}
        for e in events
        if e["event_type"] == "escalated" and e["metadata"].get("reason") == CHANGE_REASON
    ]
    told = _told(row, changes)
    since = changes[-1]["event_id"] if changes else 0
    failures = [
        e["metadata"]
        for e in events
        if e["event_type"] == "delivery_failed" and e["tier"] == tier and e["id"] > since
    ]
    callbacks = [
        {
            "at": e["at"],
            "by_name": e["by_name"],
            "recipient_name": e["metadata"].get("recipient_name"),
            "recipient_role": e["metadata"].get("recipient_role"),
            "recipient_role_text": RECIPIENT_ROLES.get(e["metadata"].get("recipient_role") or ""),
            "method": e["metadata"].get("method"),
            "method_text": CALLBACK_METHODS.get(e["metadata"].get("method") or ""),
            "read_back": bool(e["metadata"].get("read_back")),
            "reached": bool(e["metadata"].get("reached")),
            "note": e["metadata"].get("note"),
        }
        for e in events
        if e["event_type"] == "callback_logged"
    ]
    due = row.get("tier_due_at")
    is_open = row.get("status") == "open"
    if is_open and not due:
        # Written before the last tier kept the time it was reached: due
        # from then (the latest ladder step to this tier), or from raising.
        reached = [
            e["at"]
            for e in events
            if e["event_type"] == "escalated"
            and e["tier"] == tier
            and e["metadata"].get("reason") != CHANGE_REASON
        ]
        due = reached[-1] if reached else row.get("raised_at")
    due_now = False
    if is_open and due:
        try:
            due_now = parse_to_utc(due) <= parse_to_utc(now)
        except ValueError:
            due_now = True  # an unreadable due time reads as due, never as not yet
    discharged = row.get("patient_status") == "discharged"
    interfaced = row.get("source_lane") == SOURCE_LANE_INTERFACED
    if interfaced:
        # The sending laboratory's own corrections (lis-22): no Orb result.
        since = interfaced_now or {}
        value_now = since.get("value_now")
        result_now = {
            "status": since.get("status"),
            "changed": bool(since.get("changed")),
            "text": since.get("text"),
        }
    else:
        value_now = _value_now(row)
        result_now = _result_now(row, told=told, changes=changes)
    flag = _lab_flag(row)
    closure = _closure(events) if row.get("status") == "acknowledged" else None
    return {
        "id": row["id"],
        "status": row["status"],
        "source_lane": row["source_lane"],
        "patient": {
            "id": row["patient_id"],
            "name": row.get("patient_name"),
            "mrn": row.get("mrn"),
            # No bed for a discharged patient: it is someone else's now.
            "ward": None if discharged else row.get("ward"),
            "bed": None if discharged else row.get("bed"),
            "discharged": discharged,
            "discharged_at": row.get("discharged_at") if discharged else None,
        },
        "test_name": row["test_name"],
        "value_display": row["value_display"],
        "critical_limit_text": row.get("critical_limit_text"),
        "preliminary": bool(row.get("preliminary")),
        "result_id": row.get("result_id"),
        "observation_id": row.get("observation_id"),
        # The laboratory's own flag (the sending laboratory's, for the
        # interfaced lane) and, for the interfaced lane, the reference range
        # it sent. Never a flag Orb worked out.
        "lab_flag": {"code": flag, "text": LAB_FLAG_WORDS.get(flag)} if flag else None,
        "reference_range": (row.get("obs_reference_range") or None) if interfaced else None,
        # The value the ward was last sent for this critical, and the result's
        # value as it now stands (None while it is back on the bench or after
        # a withdrawal).
        "value_told": told,
        "value_now": value_now,
        "result_now": result_now,
        "changes": [
            {
                "at": c["at"],
                "by_name": c["by_name"],
                "from_value": c.get("from_value"),
                "to_value": c.get("to_value"),
                "still_critical": bool(c.get("still_critical")),
                "reopened": bool(c.get("reopened")),
            }
            for c in changes
        ],
        "raised_at": row["raised_at"],
        "raised_by_name": row.get("raised_by_name"),
        "raised_in_department": row.get("raised_in_department"),
        "tier": {
            "current": tier,
            "count": len(tiers),
            "label": _tier_words(tiers, tier),
            "next_label": _tier_words(tiers, tier + 1),
            "ladder_configured": policy["configured"],
            "due_at": due if is_open else None,
            "due_now": due_now,
        },
        "delivery": {
            "status": row.get("delivery_status"),
            "reason": (failures[-1].get("reason") if failures else None),
            "text": _delivery_words(row, failures[-1] if failures else None),
            "message_id": row.get("relay_message_id"),
        },
        "callbacks": callbacks,
        "acknowledged": (
            {
                "at": row.get("acknowledged_at"),
                "by_name": row.get("acknowledged_by_name"),
                "recipient_name": row.get("recipient_name"),
                "recipient_role": row.get("recipient_role"),
                "recipient_role_text": RECIPIENT_ROLES.get(row.get("recipient_role") or ""),
                "method": row.get("method"),
                "method_text": _ack_method_text(row.get("method"), closure),
                "read_back": bool(row.get("read_back")),
                # 'laboratory_callback' or 'ward_read_back' (lis-16).
                "closure": closure,
            }
            if row.get("status") == "acknowledged"
            else None
        ),
        "events": [
            {
                "type": e["event_type"],
                "tier": e["tier"],
                "at": e["at"],
                "detail": e["detail"],
                "by_name": e["by_name"],
            }
            for e in events
        ],
    }


def _load(conn, ids: list[str]) -> tuple[dict, dict, dict]:
    """The listed rows (with their heads), their events and, for the
    interfaced lane, what the sending laboratory has done since."""
    if not ids:
        return {}, {}, {}
    marks = ", ".join("?" for _ in ids)
    rows = {
        r["id"]: dict(r)
        for r in conn.execute(f"{_LIST_SELECT} WHERE n.id IN ({marks})", tuple(ids)).fetchall()
    }
    _add_heads(conn, rows)
    events = _events_by_notification(conn, [i for i in ids if i in rows])
    since = {
        i: _interfaced_now(conn, row)
        for i, row in rows.items()
        if row.get("source_lane") == SOURCE_LANE_INTERFACED
    }
    return rows, events, since


def _views(conn, ids: list[str], *, now: str | None = None) -> list[dict]:
    if not ids:
        return []
    rows, events, since = _load(conn, ids)
    stamp = now or utc_iso_now()
    return [_view(rows[i], events.get(i, []), stamp, since.get(i)) for i in ids if i in rows]


# ── The ward card (lis-16) ───────────────────────────────────────────────────


def _ward_state(row: dict, *, interfaced_now: dict | None) -> tuple[bool, str | None]:
    """(preliminary, the words for how the result stands) for the ward card,
    read from the result as it stands (never from the notification's
    preliminary column alone, as _message_text reads it)."""
    if row.get("source_lane") == SOURCE_LANE_INTERFACED:
        since = interfaced_now or {}
        if since.get("status") == "corrected" and not since.get("changed"):
            return False, "Corrected result from the sending laboratory"
        return False, (
            "Final result from the sending laboratory"
            if since.get("status") in (None, "final")
            else None
        )
    status = row.get("head_status", row.get("result_status"))
    if status == "withdrawn":
        return False, None
    if status == "entered":
        return False, (
            "The laboratory is checking this result again, so the value may change. "
            "It will phone with the checked result."
        )
    if status == "tech_validated" or (status != "released" and row.get("preliminary")):
        return True, "Preliminary, not yet authorised"
    return False, "Authorised"


def _ward_change(
    row: dict, changes: list[dict], told: str, *, interfaced_now: dict | None
) -> str | None:
    """What changed after the ward was first told, in words, or None."""
    if row.get("source_lane") == SOURCE_LANE_INTERFACED:
        since = interfaced_now or {}
        return since.get("text") if since.get("changed") else None
    if told == WITHDRAWN_TOLD:
        return (
            "Withdrawn: the laboratory says this result was entered in error and it is no "
            "longer on the chart. Do not act on it."
        )
    if not changes:
        return None
    last = changes[-1]
    if last.get("still_critical"):
        return (
            f"Changed after it was first reported: was {last.get('from_value')}, now "
            f"{told}, still critical."
        )
    return (
        f"Corrected: now {told}, which is not critical. It was reported as critical at "
        f"{last.get('critical_value') or row.get('value_display')}."
    )


def _ward_view(row: dict, events: list[dict], interfaced_now: dict | None = None) -> dict:
    """One critical as the ward card shows it: the patient (no bed once
    discharged), the value the laboratory told the ward, its own flag and
    the limit its pathologist agreed (for the interfaced lane the sending
    laboratory's flag and reference range), whether it is preliminary, and
    what changed since. No tier, no due time, no score and no severity of
    Orb's own."""
    changes = [
        {**e["metadata"], "at": e["at"]}
        for e in events
        if e["event_type"] == "escalated" and e["metadata"].get("reason") == CHANGE_REASON
    ]
    told = _told(row, changes)
    interfaced = row.get("source_lane") == SOURCE_LANE_INTERFACED
    discharged = row.get("patient_status") == "discharged"
    preliminary, state = _ward_state(row, interfaced_now=interfaced_now)
    withdrawn = told == WITHDRAWN_TOLD
    flag = None if withdrawn else _lab_flag(row)
    closure = _closure(events) if row.get("status") == "acknowledged" else None
    return {
        "id": row["id"],
        "status": row["status"],
        "source_lane": row["source_lane"],
        "sender": "Sending laboratory" if interfaced else SENDER_NAME,
        "patient": {
            "id": row["patient_id"],
            "name": row.get("patient_name"),
            "mrn": row.get("mrn"),
            # No bed for a discharged patient: it is someone else's now.
            "bed": None if discharged else row.get("bed"),
            "discharged": discharged,
            "discharged_at": row.get("discharged_at") if discharged else None,
        },
        "test_name": row["test_name"],
        "value_display": row["value_display"],
        # What an acknowledgement reads back, and names (value_told).
        "value_told": told,
        "withdrawn": withdrawn,
        "lab_flag": {"code": flag, "text": LAB_FLAG_WORDS.get(flag)} if flag else None,
        "critical_limit_text": (
            None
            if interfaced or withdrawn
            else row.get("head_limit_text", row.get("critical_limit_text"))
        ),
        "reference_range": (row.get("obs_reference_range") or None) if interfaced else None,
        "preliminary": preliminary,
        "state_text": state,
        "change_text": _ward_change(row, changes, told, interfaced_now=interfaced_now),
        "raised_at": row["raised_at"],
        "acknowledged": (
            {
                "at": row.get("acknowledged_at"),
                "by_name": row.get("acknowledged_by_name"),
                "recipient_name": row.get("recipient_name"),
                "method": row.get("method"),
                "method_text": _ack_method_text(row.get("method"), closure),
                "closure": closure,
            }
            if row.get("status") == "acknowledged"
            else None
        ),
    }


def _ward_views(conn, ids: list[str]) -> list[dict]:
    rows, events, since = _load(conn, ids)
    return [_ward_view(rows[i], events.get(i, []), since.get(i)) for i in ids if i in rows]


def list_for_ward(conn, *, dept: str | None, limit: int = WARD_LIST_CAP) -> dict:
    """The ward card (lis-16): every open critical of a patient in `dept`,
    the caller's department_scope (required keyword; None only for an
    account with no department, which reads hospital-wide by the existing
    convention), filtered in SQL before the LIMIT. The department is
    PATIENT_DEPARTMENT_SQL's: the active admission first, else the latest,
    so a transferred patient's critical is on the new ward and a discharged
    patient's on the last one. The most overdue first, as the laboratory
    sees them. A read: the route audits the patients returned."""
    from permissions import scope_patient_ids_sql

    empty = {"open": [], "methods": WARD_ACK_METHODS, "recipient_roles": RECIPIENT_ROLES}
    if not _tables_present(conn):
        return empty
    limit = max(1, min(int(limit or WARD_LIST_CAP), WARD_LIST_CAP))
    scope_sql, params = scope_patient_ids_sql(dept)
    ids = [
        r[0]
        for r in conn.execute(
            f"""SELECT n.id FROM lab_critical_notifications n
            WHERE n.status = 'open' AND n.patient_id IN ({scope_sql})
            ORDER BY julianday({_DUE_SQL}) ASC, julianday(n.raised_at) ASC, n.id
            LIMIT ?""",
            (*tuple(params), limit),
        ).fetchall()
    ]
    return {**empty, "open": _ward_views(conn, ids)}


#: When an open notification is due, for ordering the screen: tier_due_at,
#: or for one written before the last tier kept the time it was reached, the
#: latest ladder step to its tier, else when it was raised (as _view reads
#: it). An unreadable time reads as most overdue (julianday NULL sorts first).
_DUE_SQL = f"""COALESCE(n.tier_due_at,
        (SELECT e.occurred_at FROM lab_critical_events e
          WHERE e.notification_id = n.id AND e.event_type = 'escalated'
            AND e.tier = n.current_tier
            AND COALESCE(CASE WHEN json_valid(e.metadata_json)
                              THEN json_extract(e.metadata_json, '$.reason') END, '')
                != '{CHANGE_REASON}'
          ORDER BY e.id DESC LIMIT 1),
        n.raised_at)"""


def list_for_lab(conn, *, dept: str | None, limit: int = LIST_CAP) -> dict:
    """The Critical results screen: every open critical, the most overdue
    first (by the time its tier fell due; the last tier is due from when it
    was reached, so a critical that climbed the whole ladder unconfirmed
    comes before one not due yet), then those closed in the last
    RECENT_CLOSED_HOURS, newest first.
    `dept` is the caller's department_scope (required keyword): None for the
    laboratory roles, hospital-wide; a department keeps its own patients,
    filtered in SQL before the LIMIT. A read: the route audits the patients
    returned. A discharged patient's critical stays here until it is
    closed."""
    from permissions import scope_patient_ids_sql

    policy = policy_snapshot()
    empty = {
        "open": [],
        "recent": [],
        "ladder": policy,
        "recipient_roles": RECIPIENT_ROLES,
        "methods": CALLBACK_METHODS,
        "recent_hours": RECENT_CLOSED_HOURS,
    }
    if not _tables_present(conn):
        return empty
    limit = max(1, min(int(limit or LIST_CAP), LIST_CAP))
    scope_where, scope_params = "", ()
    if dept is not None:
        scope_sql, params = scope_patient_ids_sql(dept)
        scope_where = f" AND n.patient_id IN ({scope_sql})"
        scope_params = tuple(params)
    now = utc_iso_now()
    open_ids = [
        r[0]
        for r in conn.execute(
            f"""SELECT n.id FROM lab_critical_notifications n
            WHERE n.status = 'open' {scope_where}
            ORDER BY julianday({_DUE_SQL}) ASC, julianday(n.raised_at) ASC, n.id
            LIMIT ?""",
            (*scope_params, limit),
        ).fetchall()
    ]
    since = _plus_minutes(now, -60 * RECENT_CLOSED_HOURS)
    recent_ids = [
        r[0]
        for r in conn.execute(
            f"""SELECT n.id FROM lab_critical_notifications n
            WHERE n.status != 'open'
              AND julianday(COALESCE(n.acknowledged_at, n.closed_at, n.raised_at)) >= julianday(?)
              {scope_where}
            ORDER BY julianday(COALESCE(n.acknowledged_at, n.closed_at, n.raised_at)) DESC, n.id
            LIMIT ?""",
            (since, *scope_params, min(limit, 100)),
        ).fetchall()
    ]
    return {
        **empty,
        "open": _views(conn, open_ids, now=now),
        "recent": _views(conn, recent_ids, now=now),
    }


# ════════════════════════════════════════════════════════════════════════════
#  The sweep
# ════════════════════════════════════════════════════════════════════════════

#: Open notifications the sweep can move: due, and either below the top of
#: their ladder, or with an undelivered post on this tier that has not had
#: its one retry (counted since the latest value change, as _attempts does).
#: A post still 'pending' when due is owed too: the step that raised or
#: changed it committed and its post never ran (the key stops a double).
#: A notification at the top with nothing left to try stays due and on the
#: screen, and is not read again every sweep.
_ACTIONABLE_SQL = f"""
    SELECT n.id FROM lab_critical_notifications n
    WHERE n.status = 'open' AND n.tier_due_at IS NOT NULL
      AND julianday(n.tier_due_at) <= julianday(?)
      AND (n.current_tier < COALESCE(CASE WHEN json_valid(n.policy_json)
                                          THEN json_array_length(n.policy_json, '$.tiers') END, 0)
           OR (n.delivery_status IN ('{DELIVERY_NO_CASE_ROOM}', '{DELIVERY_FAILED}',
                                     '{DELIVERY_PENDING}')
               AND (SELECT COUNT(*) FROM lab_critical_events e
                     WHERE e.notification_id = n.id AND e.tier = n.current_tier
                       AND e.event_type IN ('posted', 'delivery_failed')
                       {_SINCE_LAST_CHANGE_SQL}) < {ATTEMPTS_PER_TIER}))
    ORDER BY julianday(n.tier_due_at) ASC, n.id
    LIMIT ?"""


def _advance_one(notification_id: str, now: str) -> str:
    """Decide and record one due notification's next step, in its own
    immediate transaction. Returns 'escalated', 'retry' or 'none'."""
    with db_session(immediate=True) as conn:
        row = conn.execute(
            "SELECT * FROM lab_critical_notifications WHERE id = ?", (notification_id,)
        ).fetchone()
        if row is None or row["status"] != "open" or not row["tier_due_at"]:
            return "none"
        n = dict(row)
        due = conn.execute(
            "SELECT julianday(?) <= julianday(?)", (n["tier_due_at"], now)
        ).fetchone()[0]
        if not due:
            return "none"
        tiers = _policy(n)["tiers"]
        tier = int(n["current_tier"])
        if tier < len(tiers):
            nxt = tier + 1
            moved = conn.execute(
                """UPDATE lab_critical_notifications
                   SET current_tier = ?, tier_due_at = ?, delivery_status = ?,
                       relay_message_id = NULL
                   WHERE id = ? AND status = 'open' AND current_tier = ?""",
                (nxt, due_at(now, tiers, nxt), DELIVERY_PENDING, n["id"], tier),
            )
            if moved.rowcount != 1:
                return "none"
            why = (
                "not_delivered" if n.get("delivery_status") != DELIVERY_POSTED else "not_confirmed"
            )
            meta = {
                "from_tier": tier,
                "to_tier": nxt,
                "reason": why,
                "label": _tier_words(tiers, nxt),
                "due_at": due_at(now, tiers, nxt),
            }
            _event(
                conn,
                n["id"],
                n["patient_id"],
                "escalated",
                tier=nxt,
                actor_id=None,
                now=now,
                detail=(
                    f"Tier {nxt}: {_tier_words(tiers, nxt)}. "
                    + (
                        "The last post was not delivered."
                        if why == "not_delivered"
                        else "Not confirmed with a read-back in time."
                    )
                ),
                metadata=meta,
            )
            _audit(
                conn,
                action="lab_critical_escalated",
                summary=(
                    f"Laboratory critical result moved to tier {nxt}: "
                    f"{_told_phrase(n['test_name'], _told(n, _changes(conn, n['id'])))}"
                ),
                actor=None,
                notification_id=n["id"],
                patient_id=n["patient_id"],
                metadata=meta,
            )
            return "escalated"
        if (
            n.get("delivery_status") in (DELIVERY_NO_CASE_ROOM, DELIVERY_FAILED, DELIVERY_PENDING)
            and _attempts(conn, n["id"], tier) < ATTEMPTS_PER_TIER
        ):
            return "retry"
        return "none"


def sweep(*, now: str | None = None, limit: int = SWEEP_BATCH) -> dict:
    """One pass of the ladder. Sync and blocking: run it through run_db.

    For each open critical whose tier is due: move it to the next tier and
    post again, or, on the last tier (or with no ladder), retry an
    undelivered post once. Returns {ran, considered, escalated, retried,
    delivered, pushes}; `pushes` are (msg_data, channel_id) pairs for the
    caller to emit as 'message:new' on the event loop. Never closes a
    critical."""
    out: dict[str, Any] = {
        "ran": False,
        "considered": 0,
        "escalated": 0,
        "retried": 0,
        "delivered": 0,
        "pushes": [],
    }
    if not surface_open():
        return out
    stamp = now or utc_iso_now()
    with db_session() as conn:
        if not _tables_present(conn):
            return out
        ids = [r[0] for r in conn.execute(_ACTIONABLE_SQL, (stamp, int(limit))).fetchall()]
    out.update(ran=True, considered=len(ids))
    for nid in ids:
        try:
            step = _advance_one(nid, stamp)
        except Exception:
            logger.exception("Laboratory critical sweep could not move %s; will retry", nid)
            continue
        if step == "none":
            continue
        out["escalated" if step == "escalated" else "retried"] += 1
        delivered = deliver(nid, push=False)
        if delivered.get("posted"):
            out["delivered"] += 1
        if delivered.get("msg_data"):
            out["pushes"].append((delivered["msg_data"], delivered["channel_id"]))
    return out


#: The loop's wait between turns (a module name so a test can shorten it
#: without touching asyncio itself).
_sleep = asyncio.sleep


async def sweep_loop(interval_seconds: float | None = None) -> None:
    """The critical-result clock. Started by lab_module.startup (registered
    below); main.py never names it.

    Runs sweep() every ORB_LAB_CRITICAL_SWEEP_S seconds (default 60) while
    the critical surfaces are open, and pushes each new post live. Stops
    itself once they close (the module finished draining with no interfaced
    lane), since nothing can be raised again while the switches are off.
    Never raises out (an escaped exception would stop the clock for good,
    silently); cancellation is re-raised so shutdown is clean."""
    from database import run_db

    interval = max(
        5.0, float(interval_seconds or (getattr(config, "LAB_CRITICAL_SWEEP_S", 60) or 60))
    )
    while True:
        await _sleep(interval)
        if not surface_open():
            logger.info("Laboratory critical sweep stopping: the critical-result surfaces are off")
            return
        try:
            result = await run_db(sweep)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Laboratory critical sweep cycle failed; retrying next interval", exc_info=True
            )
            continue
        for msg_data, channel_id in result.get("pushes") or []:
            await case_room_alerts.emit_message_new(
                msg_data, channel_id, what="laboratory critical escalation"
            )


async def _sweep_task() -> None:
    """The factory lab_module.startup runs. Registered for every module state
    because interfaced criticals run without ORB_LAB; returns at once when no
    critical surface is open."""
    if not surface_open():
        return
    await sweep_loop()


lab_module.register_loop(SWEEP_LOOP_NAME, _sweep_task, states=("on", "draining", "off"))
