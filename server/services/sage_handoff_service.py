"""Orb Sage Ambient — Stage 4 Downstream Handoff Adapters

Provides adapter logic to dispatch confirmed Sage actions to downstream modules like Helix and Scribe.
Maintains the `sage_handoffs` and `sage_handoff_events` tables to provide an auditable
cross-module coordination layer without overloading the Stage 3 Relay actions.
"""
import logging
import re
import uuid
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from database import db_session, run_db
from typing import Dict, Any, Optional

from services.helix_service import insert_order
from services.scribe_service import save_scribe_note

logger = logging.getLogger("orb.sage_handoff")


# ── Which confirmed action goes where ────────────────────────────────────────
# EXACT verbs, never substrings. Routing on words inside the verb sent "start
# monitoring" to pharmacy as a medication order ("start"), "address concerns"
# too ("add"), and a stop card to `_dispatch_to_helix`, which only ever STARTS
# a drug: confirming "stop_medication" for warfarin charted warfarin as a new
# active medication and ordered it. A verb not in this table reaches no module;
# the handoff is stored 'unsupported' and says it needs manual dispatch.
# sage_ambient_service reads this same table to drop a classifier verb before
# it can become a card.
ACTION_ROUTES: Dict[str, str] = {
    # Start a medication: the one thing `_dispatch_to_helix` does.
    "start_medication": "helix",
    "add_medication": "helix",
    "prescribe": "helix",
    # Take the patient to theatre: `_dispatch_to_transfer` books an emergency
    # case. A bare "transfer" is not here: it could mean the ICU or a ward.
    "transfer_to_surgery": "admissions",
    "book_or": "admissions",
    "book_surgery": "admissions",
    # Write a note.
    "document": "scribe",
    "draft_note": "scribe",
}
# Deliberately absent: "stop_medication" (the assessment lane can card it, but
# no adapter discontinues a drug, so it needs manual dispatch).


def normalize_action_verb(verb) -> str:
    """'Start medication', ' start-medication ' and 'start_medication' are one verb."""
    return re.sub(r"[\s\-]+", "_", str(verb or "").strip().lower())


def route_for_action(verb) -> Optional[str]:
    """The module a confirmed action dispatches to, or None when it needs manual dispatch."""
    return ACTION_ROUTES.get(normalize_action_verb(verb))


class HandoffBlocked(Exception):
    """The drug-safety screen stopped an agentic order, so nothing was charted.

    Its own outcome, not a failure: 'failed' means Orb could not do what the
    clinician asked, 'blocked' means a safety check decided it must not. Until
    2026-10-02 this path returned 'blocked-allergy' as if it were an order id
    and the handoff was stored 'accepted', so the Relay card read "Sent to
    Helix" for an order that never left. Carries the clinician-facing
    ``reason`` and the screen ``safety`` summary for the card.
    """

    def __init__(self, reason: str, safety: dict):
        self.reason = reason
        self.safety = safety
        super().__init__(reason)


def _safety_summary(safety: dict) -> dict:
    """The part of a drug-safety gate payload the confirming clinician must see.

    Every alert, the non-blocking ones included: the gate only BLOCKS on severe,
    so a moderate alert (a penicillin anaphylaxis against a cephalosporin is one
    today) charts the drug, and without this the doctor who confirmed it is
    never told.
    """
    alerts = [
        {
            "type": a.get("type"),
            "severity": a.get("severity"),
            "match": a.get("match"),
            "message": a.get("message"),
        }
        for a in (safety.get("allergy_alerts") or []) + (safety.get("interaction_alerts") or [])
    ]
    return {
        "blocking": bool(safety.get("blocking")),
        "screen_failed": bool(safety.get("screen_failed")),
        "alerts": alerts,
        "coverage_note": safety.get("coverage_note"),
    }


def parse_safety(raw):
    """sage_handoffs.safety_json as a dict, or None when there is none."""
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _handoff_view(row) -> dict:
    """A sage_handoffs row as the API returns it: `safety_json` parsed into `safety`."""
    d = dict(row)
    d["safety"] = parse_safety(d.pop("safety_json", None))
    return d


class TransactionLost(Exception):
    """SQLite rolled the WHOLE transaction back on its own (SQLITE_FULL, IOERR,
    BUSY and NOMEM can do that). Nothing may be written after it: Python's
    sqlite3 would quietly open a NEW transaction for the next statement, and a
    handoff would commit without the confirm it belongs to."""


@contextmanager
def _savepoint(db, name: str):
    """One adapter step that may fail on its own without taking the confirm with it.

    A failure rolls back only this step's writes and re-raises, so the caller can
    record a 'failed' or 'blocked' outcome on the confirmed card. If SQLite has
    already rolled back the whole transaction, raises TransactionLost instead,
    which no adapter handler swallows.
    """
    db.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception as e:
        if not getattr(db, "in_transaction", True):
            raise TransactionLost(f"transaction rolled back by SQLite during {name}: {e}") from e
        db.execute(f"ROLLBACK TO SAVEPOINT {name}")
        db.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        db.execute(f"RELEASE SAVEPOINT {name}")


def _drug_chart_refusal():
    """On an Orb Ward Record computer the drug chart stays on paper
    (services/drug_chart): a medication handoff is blocked before any drug is
    screened or charted, and the card says why in these words."""
    from services.drug_chart import DRUG_CHART_OFF, drug_chart_enabled
    if drug_chart_enabled():
        return None
    return HandoffBlocked(DRUG_CHART_OFF, {"blocking": True, "drug_chart": False})


def prescreen_handoff(action: dict, actor_id: str):
    """The part of a dispatch that must run BEFORE the write transaction opens.

    Only a Helix order has one: the drug-safety screen. It writes its own audit
    rows, which must stand whatever happens to the confirm, and on an unpooled
    connection those writes would deadlock against the transaction's write
    lock. Returns what dispatch_in_transaction takes as ``screened``: the screen
    payload, a HandoffBlocked when the screen stopped the order, or None when
    the action does not go to Helix.
    """
    if route_for_action(action.get("action_type")) != "helix":
        return None
    blocked = _drug_chart_refusal()
    if blocked is not None:
        return blocked
    try:
        return _screen_helix(action, actor_id)
    except HandoffBlocked as e:
        return e


def create_and_dispatch_handoff(action: dict, actor_id: str) -> dict:
    """Creates a durable handoff record for a confirmed action and dispatches it,
    in ONE transaction: the record, everything the adapter writes, and the
    final status commit together or not at all.

    For a caller whose action is already confirmed. The transition endpoint does
    not come through here: it runs dispatch_in_transaction on the same
    transaction as the confirm itself (relay_routes._confirm_and_hand_off).

    Returns the created (and potentially updated) handoff record.
    """
    screened = prescreen_handoff(action, actor_id)
    with db_session() as db:
        handoff, package_for = dispatch_in_transaction(db, action, actor_id, screened)
    if package_for:
        schedule_prep_package(package_for)
    return handoff


def dispatch_in_transaction(db, action: dict, actor_id: str, screened) -> tuple:
    """Record and dispatch a confirmed action on the caller's transaction.

    Every step used to commit on its own: the handoff record, the chart row, the
    Helix order and the final status. A failure between them left a pending
    order in the pharmacist's queue behind a handoff stuck at 'pending' that
    names no order, or, when the record itself failed, a card reading
    "confirmed" with nothing dispatched. Never commits; the caller does.

    ``screened`` is prescreen_handoff's result for this action. Returns
    ``(handoff view, surgery_id)``: the id of a newly booked emergency case
    whose prep package the caller must start AFTER the commit
    (schedule_prep_package), or None.
    """
    handoff_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Idempotency check: see if a pending/accepted handoff already exists for this action
    existing = db.execute(
        "SELECT * FROM sage_handoffs WHERE source_action_id = ? ORDER BY created_at DESC LIMIT 1",
        (action["id"],)
    ).fetchone()

    # 'blocked' is terminal like 'accepted': a stopped order is not quietly
    # re-run behind the clinician who confirmed it.
    if existing and existing["status"] in ("pending", "accepted", "blocked"):
        return _handoff_view(existing), None

    # Create initial handoff record
    db.execute(
        """INSERT INTO sage_handoffs 
           (id, source_action_id, room_id, patient_id, target_module, handoff_type, payload_json, status, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (handoff_id, action["id"], action["room_id"], action["patient_id"], 
         "unknown", action["action_type"], json.dumps(action), actor_id, now, now)
    )

    db.execute(
        """INSERT INTO sage_handoff_events (handoff_id, prior_status, new_status, actor_id, source, reason)
           VALUES (?, NULL, 'pending', ?, 'system', 'Handoff initialized')""",
        (handoff_id, actor_id)
    )

    return _dispatch_handoff(db, handoff_id, action, actor_id, screened)


def _dispatch_handoff(db, handoff_id: str, action: dict, actor_id: str, screened) -> tuple:
    """Routes the action to the correct downstream adapter and updates the handoff status.

    Each adapter runs under a savepoint: a 'failed' or 'blocked' outcome leaves
    nothing half-written, and the outcome itself commits with the confirm.
    """
    action_type = normalize_action_verb(action.get("action_type"))

    target_module = "unsupported"
    new_status = "unsupported"
    error_summary = None
    downstream_ref_id = None
    safety_json = None
    package_for = None

    # Exact verb lookup in ACTION_ROUTES (see the note there for what the old
    # substring match did).
    module = route_for_action(action_type)
    try:
        if module == "admissions":
            target_module = "admissions"
            with _savepoint(db, "handoff_adapter"):
                downstream_ref_id, needs_package = _dispatch_to_transfer(action, actor_id, db)
            package_for = downstream_ref_id if needs_package else None
            new_status = "accepted"
        elif module == "helix":
            target_module = "helix"
            with _savepoint(db, "handoff_adapter"):
                downstream_ref_id, safety, error_summary = _write_helix(db, action, actor_id, screened)
            safety_json = json.dumps(safety)
            new_status = "accepted"
        elif module == "scribe":
            target_module = "scribe"
            with _savepoint(db, "handoff_adapter"):
                downstream_ref_id = _dispatch_to_scribe(action, actor_id, db)
            new_status = "accepted"
        else:
            # Stored 'unsupported' (the status the table admits), and the
            # summary is written for the clinician who confirmed the card.
            new_status = "unsupported"
            verb = action_type.replace("_", " ") or "this action"
            error_summary = (f"Needs manual dispatch: Orb has no automatic route for "
                             f"\"{verb}\", so nothing was sent or changed. Do it by hand.")
            logger.warning("Handoff %s: no route for action_type %r, needs manual dispatch",
                           handoff_id, action.get("action_type"))
    except TransactionLost:
        raise
    except HandoffBlocked as e:
        new_status = "blocked"
        error_summary = e.reason
        safety_json = json.dumps(e.safety)
    except Exception as e:
        new_status = "failed"
        error_summary = str(e)
        logger.warning("Handoff %s to %s failed: %s", handoff_id, target_module, e, exc_info=True)

    now = datetime.now(timezone.utc).isoformat()

    # Fetch prior status for event log
    row = db.execute("SELECT status FROM sage_handoffs WHERE id = ?", (handoff_id,)).fetchone()
    prior_status = row["status"] if row else "pending"

    db.execute(
        """UPDATE sage_handoffs
           SET target_module = ?, status = ?, downstream_ref_id = ?, error_summary = ?, safety_json = ?,
               last_attempted_at = ?, updated_at = ?
           WHERE id = ?""",
        (target_module, new_status, downstream_ref_id, error_summary, safety_json, now, now, handoff_id)
    )

    db.execute(
        """INSERT INTO sage_handoff_events (handoff_id, prior_status, new_status, actor_id, source, reason)
           VALUES (?, ?, ?, ?, 'system', ?)""",
        (handoff_id, prior_status, new_status, actor_id, error_summary)
    )

    updated_row = db.execute("SELECT * FROM sage_handoffs WHERE id = ?", (handoff_id,)).fetchone()

    return (_handoff_view(updated_row) if updated_row else {}), package_for


def _dispatch_to_helix(action: dict, actor_id: str) -> tuple:
    """Dispatches a prescribe action to the Helix service, standalone: screen,
    then chart and order in one transaction of its own.

    Returns ``(downstream_ref_id, safety, note)``: the Helix order id (or
    'charted-only'), the drug-safety summary for the card, and, for
    'charted-only', why no pharmacy order exists (None otherwise). Raises
    HandoffBlocked, with nothing charted, when the screen stops the order or
    cannot run.
    """
    blocked = _drug_chart_refusal()
    if blocked is not None:
        raise blocked
    safety = _screen_helix(action, actor_id)
    with db_session() as db:
        return _write_helix(db, action, actor_id, safety)


def _helix_order_fields(action: dict) -> tuple:
    """``(dose, route, frequency)`` for an agentic order.

    HUMAN-IN-THE-LOOP DOSING: an LLM-extracted order must NEVER auto-populate a
    concrete dose it did not reliably extract. We do not invent "TBD"/defaults
    that could be mistaken for a real order; the dose is explicitly flagged for
    clinician entry, and the med is charted with provenance source='llm_extracted'
    so it is visibly unverified in the chart.
    """
    dose = "REVIEW REQUIRED"
    route = "Review"
    frequency = "REVIEW REQUIRED"

    clean_rat = re.sub(r'[^\w\s]', ' ', (action.get("rationale", "") or "").lower())
    if " iv " in f" {clean_rat} ": route = "IV"
    elif " im " in f" {clean_rat} ": route = "IM"
    elif " sc " in f" {clean_rat} ": route = "SC"
    return dose, route, frequency


def _screen_helix(action: dict, actor_id: str) -> dict:
    """The order-time drug-safety screen for an agentic order. Returns the gate's
    payload; raises HandoffBlocked when it stops the order or cannot run.

    Runs OUTSIDE any write transaction (see prescreen_handoff).
    """
    patient_id = action.get("patient_id")
    target_med = action.get("target", "Unknown Medication")

    # ORDER-TIME SAFETY GATE: screen the drug against the patient's allergies and
    # active meds. A SEVERE allergy match BLOCKS the auto-chart entirely — an
    # agentic pathway must not put a contraindicated drug on a patient's chart.
    #
    # This goes through enforce_medication_safety, the gate whose own docstring
    # says "every code path that writes a medication calls it". This path did
    # not: it called check_medication_safety directly and swallowed any exception
    # into `safety = {"blocking": False}`. Demonstrated 2026-08-06 by injecting a
    # fault into the allergy read — amoxicillin was charted, active, against a
    # documented penicillin ANAPHYLAXIS, because the screen that would have
    # caught it had failed. An unreadable screen is not a clean screen, and this
    # is the one write path with no human at the other end to notice.
    #
    # interactive=False is the correct mode here for the same reason: there is
    # nobody to type an acknowledgement, so a severe alert or a failed screen
    # must route to human review rather than be acknowledged away.
    from services.medication_safety_gate import (
        MedicationSafetyBlocked, enforce_medication_safety,
    )
    try:
        return enforce_medication_safety(
            patient_id, target_med,
            actor_id=actor_id,
            entry_point="sage.agentic_handoff",
            interactive=False,
        )
    except MedicationSafetyBlocked as e:
        logger.warning("BLOCKED agentic order '%s' for %s: %s (%s)",
                       target_med, patient_id,
                       e.safety.get("allergy_alerts"), e.reason)
        if e.safety.get("screen_failed"):
            reason = (f"Not charted: the drug-safety screen could not run, so {target_med} "
                      "was not checked. Order it through pharmacist verification.")
        else:
            severe = [a.get("message") for a in
                      (e.safety.get("allergy_alerts") or []) + (e.safety.get("interaction_alerts") or [])
                      if a.get("severity") == "severe" and a.get("message")]
            reason = f"Not charted: the drug-safety screen blocked {target_med}. " + " ".join(severe)
        raise HandoffBlocked(reason.strip(), _safety_summary(e.safety)) from e
    except Exception as e:
        # The gate itself could not run. Fail CLOSED: nothing is charted, and the
        # card says so rather than reading as sent.
        logger.error("BLOCKED agentic order '%s' for %s: safety gate unavailable: %s",
                     target_med, patient_id, e)
        raise HandoffBlocked(
            f"Not charted: the drug-safety screen could not run, so {target_med} "
            "was not checked. Order it through pharmacist verification.",
            _safety_summary({"blocking": True, "screen_failed": True}),
        ) from e


def _write_helix(db, action: dict, actor_id: str, screened) -> tuple:
    """Chart the drug and place the Helix order on ``db``, after a screen that
    has already run (``screened``, from _screen_helix / prescreen_handoff).
    Never commits. Returns ``(downstream_ref_id, safety, note)`` as
    _dispatch_to_helix does; re-raises a HandoffBlocked screen result.
    """
    blocked = _drug_chart_refusal()
    if blocked is not None:
        raise blocked
    if isinstance(screened, HandoffBlocked):
        raise screened
    if screened is None:
        # Fail closed: an order may never be written without a screen behind it.
        raise RuntimeError("No drug-safety screen ran for this order, so nothing was charted.")
    safety = screened

    patient_id = action.get("patient_id")
    target_med = action.get("target", "Unknown Medication")
    dose, route, frequency = _helix_order_fields(action)

    # Chart as an UNVERIFIED, llm-sourced entry (provenance visible; dose flagged
    # for clinician entry). Best-effort: a missing patient/actor must not crash.
    try:
        with _savepoint(db, "handoff_chart"):
            db.execute(
                """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, prescribed_by, source)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, 'llm_extracted')""",
                (patient_id, target_med, dose, route, frequency, actor_id),
            )
    except TransactionLost:
        raise
    except Exception as e:
        logger.warning("could not chart '%s': %s", target_med, e)

    # Then create the Helix pharmacy order (best-effort — a non-formulary drug is still
    # charted and visible in Vigil; only the pharmacy order is skipped). On this
    # transaction, through insert_order: create_order commits in a session of
    # its own. The drug is found by name exactly as create_order finds it:
    # confirmed drugs only (grn-6), so a draft waiting for its second
    # pharmacist never shadows the confirmed drug of the same name.
    try:
        with _savepoint(db, "handoff_order"):
            med = db.execute(
                "SELECT id FROM helix_medication_master "
                "WHERE (name LIKE ? OR generic_name LIKE ?) AND identity_status = 'active'",
                (f"%{target_med}%", f"%{target_med}%")
            ).fetchone()
            if not med:
                raise ValueError(f"Medication {target_med} not found in Helix master catalog.")
            order = insert_order(
                db,
                patient_id=patient_id,
                medication_id=med["id"],
                dose=dose,
                route=route,
                frequency=frequency,
                prescribed_by=actor_id,
            )
        return order["id"], _safety_summary(safety), None
    except TransactionLost:
        raise
    except Exception as e:
        logger.warning("charted '%s' but Helix order failed: %s", target_med, e)
        # The card used to read "Sent to Helix" here, though no pharmacy order
        # exists (typically a target that is not in the Helix catalog). Say so.
        note = (f"Charted for review, but not sent to pharmacy: {str(e).rstrip('.')}. "
                "Place the order in Helix.")
        return "charted-only", _safety_summary(safety), note

def _dispatch_to_scribe(action: dict, actor_id: str, db) -> str:
    """Dispatches a document action to the Scribe service, on ``db``. Returns the Scribe note_id."""
    # Scribe requires: patient_id, author_id, raw_transcript, structured_note, note_format
    
    patient_id = action.get("patient_id")
    target = action.get("target", "")
    rationale = action.get("rationale", "")
    
    raw_transcript = f"Sage Ambient Observation: {target}. {rationale}"
    note_format = "soap" # Default
    
    try:
        note_id = save_scribe_note(
            patient_id=patient_id,
            author_id=actor_id,
            raw_transcript=raw_transcript,
            structured_note=None, # Will be structured asynchronously if needed
            note_format=note_format,
            ai_generated=True,
            db=db,
        )
        return str(note_id)
    except Exception as e:
        raise RuntimeError(f"Scribe note creation failed: {e}")

def _resolve_surgeon_id(db, patient_id: str, actor_id: str) -> str:
    """Pick the LEAD SURGEON for a surgical case. When a medical patient is transferred
    to surgery the surgeon is a Surgery-department doctor — NOT the medical attending.
    Order: attending iff they are themselves a surgeon, else the on-call Surgery-dept
    doctor, else the attending doctor, else the confirming clinician."""
    pat = db.execute(
        "SELECT attending_doctor_id FROM patients WHERE id = ?", (patient_id,)
    ).fetchone()
    attending = pat["attending_doctor_id"] if pat else None
    # 1. Attending only if they are already a surgeon (an existing surgical patient).
    if attending and db.execute(
        "SELECT 1 FROM users WHERE id = ? AND role = 'doctor' AND department LIKE '%Surg%'",
        (attending,),
    ).fetchone():
        return attending
    # 2. The on-call Surgery-department doctor for the transfer.
    surg_doc = db.execute(
        """SELECT id FROM users
           WHERE role = 'doctor' AND department LIKE '%Surg%'
           ORDER BY id LIMIT 1"""
    ).fetchone()
    if surg_doc:
        return surg_doc["id"]
    # 3. Fall back to the attending doctor, then the confirming clinician.
    if attending and db.execute(
        "SELECT 1 FROM users WHERE id = ? AND role = 'doctor'", (attending,)
    ).fetchone():
        return attending
    return actor_id


# Card targets that name a destination, not an operation.
_GENERIC_SURGICAL_TARGETS = {
    "surgery", "general surgery", "surgical", "surgical team", "surgeon",
    "theatre", "operating theatre", "operating room", "ot", "or",
}


def _procedure_from_target(raw_target: str, known_depts: set) -> str:
    """The procedure named on the card, or 'To be confirmed' when it names none.

    A transfer card's target is either a department ("General Surgery") or the
    operation ("Exploratory Laparotomy"); only the second is a procedure.
    """
    t = (raw_target or "").strip()
    lowered = {d.lower() for d in known_depts if d}
    if not t or t.lower() in lowered or t.lower() in _GENERIC_SURGICAL_TARGETS:
        return "To be confirmed"
    return t


# ── The Relay transfer lane through the ADT writer (adt-6c, W7-C) ────────────
#
# With Beds and admissions (ORB_ADT) off, _dispatch_to_transfer writes
# admissions.department itself, exactly as it always has. With it on, that
# bare UPDATE would be a second writer of where a patient is treated: no
# movement row, no clinical_access audit, and a destination checked only
# against "any department any login carries", which includes a pharmacist's
# department and the ward labels written as a patient login's department. So
# the confirmed card transfers through services/adt/transfer.transfer_on:
#
#   * rule C: the destination is resolved against the departments an active
#     doctor, clinician or nurse covers (the front desk's anti-orphaning list),
#     read on this same connection. A card whose target names a department
#     with no clinical cover is refused with rule C's words; a target that
#     names no department at all (a procedure) goes to General Surgery, as the
#     lane always read it, which must itself be covered;
#   * the patient keeps the bed they lie in (keep_location), usually as a
#     boarder on the old ward; the confirming clinician's confirmation of the
#     card stands as the boarder acknowledgement, with the reason
#     'Confirmed in Relay' on the movement;
#   * the movement, its patient_movement audit, the clinical_access
#     'patient_transferred' row (and 'boarder_placed' with its case-room line,
#     for a boarder) and the department change are written in the confirm's
#     own transaction, so they commit with the card or not at all.
#
# THE SESSION. transfer() opens db_session(immediate=True), which must be the
# outermost session; this adapter runs INSIDE the confirm's transaction
# (relay_routes._confirm_and_hand_off since the R024 work; the design's note
# that it ran after the handoff session closed no longer holds). So the lane
# calls transfer_on with the caller's connection, under the adapter's
# savepoint. Every caller has already written on this transaction (the
# confirm's version-guarded claim, or the handoff record) before the adapter
# runs, so it holds SQLite's write lock: no other writer can interleave
# between the checks below and the write, which is what the immediate session
# gives the other lanes. Rule C's list is read on this connection for the
# same reason (frontdesk_service.list_covering_departments opens a session of
# its own).
#
# RELEASE GATE: config.py says ORB_ADT must not be switched on at a site where
# Sage runs until this lane landed; this is that lane.

#: The reason on the movement a confirmed Relay transfer card writes.
RELAY_TRANSFER_REASON = "Confirmed in Relay"

#: Where a transfer card whose target names no department goes, as the lane
#: has always read it (every verb routed here is a surgical one).
_DEFAULT_SURGICAL_DEPARTMENT = "General Surgery"


def _adt_transfer_lane_on() -> bool:
    """True only while ORB_ADT is on (read at call time)."""
    from services.adt.core import adt_on

    return adt_on()


def _covering_departments_on(db) -> list:
    """The departments an active doctor, clinician or nurse covers, read on
    the caller's connection: the same query and roles as
    frontdesk_service.list_covering_departments (rule C), which opens a
    session of its own and so cannot run inside the confirm's transaction."""
    from services.frontdesk_service import _COVERING_ROLES

    marks = ",".join("?" for _ in _COVERING_ROLES)
    rows = db.execute(
        f"""SELECT DISTINCT TRIM(department) AS department
              FROM users
             WHERE role IN ({marks})
               AND department IS NOT NULL
               AND TRIM(department) != ''
               AND COALESCE(is_active, 1) = 1
             ORDER BY 1""",
        _COVERING_ROLES,
    ).fetchall()
    return [r["department"] for r in rows]


def _relay_destination(raw_target: str, *, covering: list, named: set) -> str:
    """The covered department a confirmed transfer card goes to (rule C), or
    AdtError with rule C's words. A target that names a department Orb knows
    (any login's, or a ward's) is held to it: never quietly sent elsewhere,
    and refused when nobody clinical covers it. A target that names none (a
    procedure such as 'Exploratory Laparotomy', or 'theatre') goes to General
    Surgery, which must itself be covered."""
    from services.adt.transfer import resolve_cover

    target = (raw_target or "").strip()
    folded = target.casefold()
    known = {str(d).strip().casefold() for d in (*named, *covering) if d and str(d).strip()}
    if target and folded in known:
        return resolve_cover(target, covering=covering)
    return resolve_cover(_DEFAULT_SURGICAL_DEPARTMENT, covering=covering)


def _transfer_through_adt_on(db, *, action: dict, actor_id: str, admission_id: str,
                             raw_target: str, named: set) -> tuple:
    """The confirmed card's transfer through the ADT writer, on the confirm's
    connection (see the section comment). Returns (the department the
    patient is now treated by, the covering list it was resolved against).
    Writes nothing when the patient is already treated there."""
    from services.adt import transfer as adt_transfer

    if not getattr(db, "in_transaction", False):
        # Every caller has written on this transaction before the adapter
        # runs; without that the checks and the write could interleave with
        # another writer.
        raise RuntimeError("The Relay transfer lane must run inside the confirm's transaction.")
    names = set(named)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'wards'").fetchone():
        names |= {r[0] for r in db.execute(
            "SELECT DISTINCT department FROM wards WHERE department IS NOT NULL").fetchall()}
    covering = _covering_departments_on(db)
    canonical = _relay_destination(raw_target, covering=covering, named=names)
    current = db.execute("SELECT department FROM admissions WHERE id = ?",
                         (admission_id,)).fetchone()
    if current is not None and str(current["department"] or "").strip().casefold() \
            == canonical.casefold():
        return canonical, covering
    user = db.execute("SELECT id, name, role, department FROM users WHERE id = ?",
                      (actor_id,)).fetchone()
    actor = dict(user) if user is not None else {"id": actor_id}
    adt_transfer.transfer_on(
        db, actor=actor, to_department=canonical, reason=RELAY_TRANSFER_REASON,
        admission_id=admission_id, keep_location=True, boarder_acknowledged=True,
        notes=f"Transfer card confirmed in Relay (Sage action {action.get('id')})",
        idempotency_key=f"relay-action-{action.get('id')}")
    return canonical, covering


def _dispatch_to_transfer(action: dict, actor_id: str, db) -> tuple:
    """Dispatches a transfer action, updating the active admission and setting up a surgical case,
    on ``db`` (never commits). Returns ``(surgery_id, needs_package)``: True when a NEW
    case was booked, whose prep package the caller starts after the commit
    (schedule_prep_package)."""
    patient_id = action.get("patient_id")
    room_id = action.get("room_id")
    raw_target = (action.get("target") or "").strip()
    target_dept = raw_target or "General Surgery"

    # 1. Update Admission
    active_adm = db.execute("SELECT id FROM admissions WHERE patient_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1", (patient_id,)).fetchone()
    if not active_adm:
        raise RuntimeError(f"No active admission found for patient {patient_id}")

    # The action's `target` is LLM-derived and often carries the procedure name
    # ("Exploratory Laparotomy") rather than a department. Writing that into
    # admissions.department removes the patient from every department-filtered
    # board (Vigil), so only accept a department that actually exists.
    known_depts = {
        row["department"]
        for row in db.execute(
            "SELECT DISTINCT department FROM users WHERE department IS NOT NULL"
        ).fetchall()
    }
    if _adt_transfer_lane_on():
        # adt-6c (W7-C): with Beds and admissions on, the move goes through the
        # ADT writer under rule C, never a bare UPDATE (see
        # _transfer_through_adt_on). A destination with no clinical cover
        # raises here with rule C's words, the savepoint rolls this adapter
        # back, and the handoff is stored 'failed' with nothing changed.
        target_dept, covering = _transfer_through_adt_on(
            db, action=action, actor_id=actor_id, admission_id=active_adm["id"],
            raw_target=raw_target, named=known_depts)
        known_depts = known_depts | set(covering)
    else:
        if target_dept not in known_depts:
            target_dept = "General Surgery"

        db.execute("UPDATE admissions SET department = ? WHERE id = ?", (target_dept, active_adm["id"]))

    # 2. Add System Message to Relay
    if room_id:
        sys_msg = f"SYSTEM: Patient transfer to {target_dept} confirmed and initiated."
        db.execute(
            """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot, source)
               VALUES (?, 'system', 'Orb System', 'system', ?, 'system', 1, 'system')""",
            (room_id, sys_msg)
        )

    # 3. Reuse an existing emergency surgery seed for THIS patient if one is
    #    already staged (idempotent re-confirm), regardless of patient id.
    seeded = db.execute(
        """SELECT id FROM surgeries
           WHERE patient_id = ? AND status LIKE 'emergency_%'
           ORDER BY created_at DESC LIMIT 1""",
        (patient_id,)
    ).fetchone()
    if seeded:
        db.execute("UPDATE surgeries SET status = 'emergency_ready' WHERE id = ?", (seeded["id"],))
        surgery_id = seeded["id"]
        _alert_assigned_team(db, surgery_id, patient_id)
        return surgery_id, False

    # 4. Otherwise create a new emergency surgery for this patient.
    surgery_id = f"SURG-{uuid.uuid4().hex[:6].upper()}"
    # Hospital calendar, like the schedule that lists it: a UTC date put a
    # case raised 00:00-05:30 IST under yesterday.
    from services.hospital_time import hospital_now
    local_now = hospital_now()
    now_date = local_now.strftime("%Y-%m-%d")
    now_time = local_now.strftime("%H:%M")

    pat = db.execute("SELECT diagnosis FROM patients WHERE id = ?", (patient_id,)).fetchone()
    diag = pat["diagnosis"] if pat else "Escalated for surgical intervention"

    # Pull the patient's real comorbidities so the Surgical Suite detail is
    # accurate (instead of the placeholder 'Reviewing context...').
    prof = db.execute(
        "SELECT comorbidities FROM patient_profiles WHERE patient_id = ?", (patient_id,)
    ).fetchone()
    co_morbidities_json = (prof["comorbidities"] if prof and prof["comorbidities"] else "[]")

    surgeon_id = _resolve_surgeon_id(db, patient_id, actor_id)

    # The procedure is what the confirmed card named, never a guess. This
    # used to default to "Exploratory Laparotomy: Source Control" and read
    # "hartmann" out of the rationale into a full resection-plus-colostomy
    # title, so every transfer was booked as an operation nobody ordered.
    procedure_name = _procedure_from_target(raw_target, known_depts)

    # Explicit column/value alignment — every column has a matching placeholder.
    db.execute(
        """INSERT INTO surgeries
           (id, patient_id, surgeon_id, procedure_name, diagnosis,
            scheduled_date, scheduled_time, estimated_duration_min, operating_room,
            status, co_morbidities)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'emergency_pending', ?)""",
        (surgery_id, patient_id, surgeon_id, procedure_name, diag,
         now_date, now_time, 120, "OR-E (Emergency)", co_morbidities_json)
    )

    # Alert ONLY the relevant staff for this case (not the whole department).
    _alert_assigned_team(db, surgery_id, patient_id)

    # The prep package is NOT started here. This runs inside the confirm's
    # transaction, off the event loop, so get_running_loop() would fail (the
    # case silently parked at 'emergency_pending'), and a task started before
    # the commit could look for a case that is not there yet. The caller starts
    # it after the commit: schedule_prep_package.
    return surgery_id, True


def schedule_prep_package(surgery_id: str) -> None:
    """Start the surgical prep package for a newly booked emergency case.

    Call it AFTER the booking commits, on the event loop thread:
    get_running_loop() only succeeds there. Off the loop it logs a WARNING and
    the case stays at 'emergency_pending', as it always has without a loop.
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Transfer %s: no running event loop, prep package not generated", surgery_id)
        return
    loop.create_task(generate_transfer_surgery_package_async(surgery_id))


def _alert_assigned_team(db, surgery_id: str, patient_id: str) -> None:
    """Create targeted surgical_alert cards in the patient's case room for ONLY the
    relevant staff: the assigned surgeon plus one available nurse and one
    anesthetist. Records the assignments and posts per-user alert messages so the
    existing Relay surgical_alert renderer can show a Confirm button to each.

    Live delivery: callers running inside the request loop emit message:new for
    each inserted alert (see _emit_team_alerts_live); here we only persist.
    """
    surg = db.execute("SELECT surgeon_id FROM surgeries WHERE id = ?", (surgery_id,)).fetchone()
    surgeon_id = surg["surgeon_id"] if surg else None

    # Resolve the case room channel for this patient.
    chan = db.execute(
        "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room' LIMIT 1",
        (patient_id,)
    ).fetchone()
    if not chan:
        return
    channel_id = chan["id"]

    # Build the targeted roster: surgeon (real id) + first available nurse +
    # first available anesthetist-type user. Only these users are alerted.
    roster = []
    if surgeon_id:
        roster.append((surgeon_id, "Lead Surgeon"))

    nurse = db.execute(
        "SELECT id FROM users WHERE role = 'nurse' ORDER BY id LIMIT 1"
    ).fetchone()
    if nurse:
        roster.append((nurse["id"], "Scrub Nurse"))

    anes = db.execute(
        """SELECT id FROM users
           WHERE (LOWER(department) LIKE '%anes%' OR LOWER(name) LIKE '%anes%')
           ORDER BY id LIMIT 1"""
    ).fetchone()
    if anes:
        roster.append((anes["id"], "Anesthetist"))

    # Refresh assignments for this surgery so re-confirm is idempotent.
    db.execute("DELETE FROM surgery_team_assignments WHERE surgery_id = ?", (surgery_id,))
    for user_id, role_name in roster:
        db.execute(
            "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) VALUES (?, ?, ?, 'alerted')",
            (surgery_id, user_id, role_name)
        )
        alert_json = json.dumps({
            "text": f"SURGICAL ALERT: You are assigned as {role_name} for an emergency procedure.",
            "target_user_id": user_id,
            "surgery_id": surgery_id,
            "role": role_name,
        })
        db.execute(
            """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot, source)
               VALUES (?, 'system', 'Sage Ambient', 'ai', ?, 'surgical_alert', 1, 'ai')""",
            (channel_id, alert_json)
        )

# The run_db payloads below are module-level rather than closures defined inside
# their async callers. They take every value as an explicit argument (they close
# over nothing), and keeping the `with db_session()` out of an `async def` body
# keeps the repo-wide "blocking DB call on the event loop" scan honest — nested
# inside an async def they read as on-loop hits even though run_db hands them to
# a worker thread.
def _load_alert_rows(patient_id):
    with db_session() as db:
        chan = db.execute(
            "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room' LIMIT 1",
            (patient_id,)
        ).fetchone()
        if not chan:
            return None
        channel_id = chan["id"]
        rows = db.execute(
            """SELECT * FROM messages
               WHERE channel_id = ? AND message_type = 'surgical_alert'
               ORDER BY id DESC LIMIT 8""",
            (channel_id,)
        ).fetchall()
        return channel_id, rows


async def emit_team_alerts_live(surgery_id: str, patient_id: str):
    """Emit message:new for each surgical_alert this surgery produced, both into the
    patient's case-room channel and into each targeted user's personal socket room
    (user:<id>), so assigned staff get the alert live without polling."""
    from ws.realtime import sio

    loaded = await run_db(_load_alert_rows, patient_id)
    if loaded is None:
        return
    channel_id, rows = loaded

    for r in rows:
        try:
            payload = json.loads(r["content"])
        except Exception:
            payload = {}
        if payload.get("surgery_id") != surgery_id:
            continue
        # The history endpoint's serializer: carries `is_bot` (AUTO chip) and
        # `source`, which a hand-built copy left out (2026-10-03).
        from services.relay_service import _message_row_to_dict
        msg = _message_row_to_dict(r)
        try:
            await sio.emit("message:new", msg, room=channel_id)
            target = payload.get("target_user_id")
            if target:
                await sio.emit("message:new", msg, room=f"user:{target}")
        except Exception as e:
            logger.warning("Transfer %s: live alert emit failed: %s", surgery_id, e)


# Status flip and the read that customises the package are ONE db_session, and
# therefore one transaction on one worker thread: the read must see the row this
# same call just moved to 'emergency_context_generating'. Splitting them across
# two run_db hops would let a concurrent re-confirm land in between.
def _mark_generating(surgery_id):
    with db_session() as db:
        db.execute("UPDATE surgeries SET status = 'emergency_context_generating' WHERE id = ?", (surgery_id,))
        # Fetch patient details for customization
        surg = db.execute(
            """SELECT s.procedure_name, p.name as patient_name, p.diagnosis as patient_diagnosis
               FROM surgeries s JOIN patients p ON s.patient_id = p.id
               WHERE s.id = ?""",
            (surgery_id,)
        ).fetchone()
        procedure_name = surg["procedure_name"] if surg else "Emergency Surgery"
        patient_name = surg["patient_name"] if surg else "Patient"
        patient_diag = surg["patient_diagnosis"] if surg else "Acute Surgical Presentation"
        return procedure_name, patient_name, patient_diag


def _persist_package(surgery_id, risk_assessment, possible_complications,
                     success_probability, surgical_checklist,
                     instruments_required, staff_required, notes_payload):
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
                risk_assessment,
                json.dumps(possible_complications),
                success_probability,
                json.dumps(surgical_checklist),
                json.dumps(instruments_required),
                json.dumps(staff_required),
                json.dumps(notes_payload),
                surgery_id
            )
        )


async def generate_transfer_surgery_package_async(surgery_id: str):
    # Deferred: ws.realtime imports back into the service layer.
    from ws.realtime import sio
    from services.surgery_escalation_service import PREP_PACKAGE

    # 1. Transition to emergency_context_generating
    await run_db(_mark_generating, surgery_id)

    # Emit socket event so frontend knows status is generating
    try:
        await sio.emit("surgery:emergency_intake", {"surgery_id": surgery_id})
    except Exception as e:
        logger.warning("Transfer %s: socket emit failed: %s", surgery_id, e)

    # No model reads the patient here, so the package is the shared honest
    # template (no ASA class, no sepsis claim, no success estimate, no named
    # staff). This used to write "ASA IV, septic shock" and a 78.5% success
    # figure with invented clinicians onto every real transfer.
    notes_payload = {
        "ai_package_status": "template",
        "finalized_at": datetime.now(timezone.utc).isoformat()
    }

    try:
        await run_db(_persist_package, surgery_id, PREP_PACKAGE["risk_assessment"],
                     PREP_PACKAGE["possible_complications"],
                     PREP_PACKAGE["success_probability"],
                     PREP_PACKAGE["surgical_checklist"],
                     PREP_PACKAGE["instruments_required"],
                     PREP_PACKAGE["staff_required"], notes_payload)
    except Exception as e:
        # A failed write must still open the case: left at
        # 'emergency_context_generating' it sat behind a spinner forever, with
        # its WHO checklist out of reach. Same fallback as the Relay prep lane.
        logger.error("Transfer package write failed for %s: %s", surgery_id, e, exc_info=True)
        from services.surgery_escalation_service import FALLBACK_DATA, _finalize_surgery
        await run_db(_finalize_surgery, surgery_id, FALLBACK_DATA, "failed", f"Error: {e}")

    # Emit socket event again so frontend knows it is completed/ready
    try:
        await sio.emit("surgery:emergency_intake", {"surgery_id": surgery_id})
    except Exception as e:
        logger.warning("Transfer %s: socket emit failed: %s", surgery_id, e)
