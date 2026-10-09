"""Orb: the medication-order store core (medorders-1, W4-E).

The state machine of a medication order and the in-transaction writers every
lane shares. Nothing in the running system calls this module until a lane is
switched on (ORB_MED_ORDER_STORE, then ORB_MED_ORDER_LIFECYCLE); with both off
it is inert.

THE LIFECYCLE (design section 2)

    draft       -> prescribed | cancelled
    prescribed  -> verified | rejected | held | stopped | cancelled
    verified    -> held | stopped | completed
    held        -> the state it was held from (resume) | stopped
    stopped, completed, cancelled, rejected: final

Dispensed and given are EVENTS, not states: a three-times-a-day course is
dispensed many times and given fifteen times. Who may take each step is the
route's business (permissions.py); this module only refuses a step the state
machine does not allow, a stale version, and a missing reason.

THE WARD PROJECTION (design section 2, "Projection rules")
    draft                    no medications row
    prescribed, verified     status = projection_status ('active' or 'prn'),
                             held_at cleared
    held                     status UNCHANGED (the interaction screen and the
                             regimen still see the drug), held_at and
                             hold_reason set: the marker every reader that
                             drives a dose checks
    stopped, completed,      'discontinued', held_at cleared
    cancelled, rejected
The verifier is copied onto the ward row and never cleared (invariant I1).

RULES FOR THE WRITERS BELOW. Each takes the caller's connection, opens no
session, never commits, and refuses to run outside a transaction (on an
autocommit connection each statement would commit alone and a failure would
leave half an order). The caller holds the outermost
db_session(immediate=True), or, for the eMAR, has already written its own row
in the same transaction. Every state change writes its ledger event and a
clinical_access audit row (fail-closed) in that same transaction, so an order,
its event and its audit commit together or not at all. Integer ids always go to
the audit as text (the int target_id false-tamper trap).

Pure functions here never touch the database: the parity report, the GET
routes and the tests call them freely.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Mapping, NamedTuple, Optional

from services.audit_service import log_audit_event
from services.clinical_time import utc_iso_now

logger = logging.getLogger("orb.medorders")

__all__ = [
    "STATUSES", "LIVE_STATUSES", "TERMINAL_STATUSES", "HOLDABLE_FROM", "LEGAL_TRANSITIONS",
    "SOURCES", "UNVERIFIED_SOURCES", "EVENT_KINDS", "REASON_REQUIRED_KINDS",
    "ENCOUNTER_TYPES", "PRIORITIES", "SUPPLY_SOURCES", "PROJECTION_STATUSES",
    "LEGACY_STOPPED_REASON", "LEGACY_REJECTED_REASON",
    "MedOrderError", "MedOrderNotFound", "MedOrderInvalid", "MedOrderConflict",
    "TransitionNotAllowed", "StaleOrderVersion", "StaleScreen", "NeedsInput", "NotPermitted",
    "StoreUsageError", "http_detail", "StoreWrite",
    "store_on", "lifecycle_on",
    "is_legal_transition", "event_kind_for", "order_allows_dose", "legacy_status_for",
    "projection_for", "fhir_status_for", "cpoe_status_for", "screen_fingerprint",
    "get_order_in_txn", "events_in_txn", "screen_fingerprint_in_txn",
    "record_event_in_txn", "create_in_txn", "link_projection_in_txn", "project_in_txn",
    "transition_in_txn", "audit_in_txn", "legacy_decision_on_held_in_txn",
]

# ── Vocabularies ──────────────────────────────────────────────────────────────

#: Every state an order can be in (migration 61's CHECK holds the same list).
STATUSES = ("draft", "prescribed", "verified", "held", "stopped", "completed",
            "cancelled", "rejected")
#: States in which the drug is on the ward: the order has a projection row
#: whose status is 'active' or 'prn'.
LIVE_STATUSES = ("prescribed", "verified", "held")
TERMINAL_STATUSES = ("stopped", "completed", "cancelled", "rejected")
#: The states a hold may start from, and so the states a resume returns to.
HOLDABLE_FROM = ("prescribed", "verified")

LEGAL_TRANSITIONS: dict[str, frozenset] = {
    "draft": frozenset({"prescribed", "cancelled"}),
    # Cancelling a prescribed order is only right before any dose was given;
    # the route that offers it checks the MAR (held for ruling MO-R2).
    "prescribed": frozenset({"verified", "rejected", "held", "stopped", "cancelled"}),
    "verified": frozenset({"held", "stopped", "completed"}),
    # A resume returns to held_from only; is_legal_transition enforces that.
    "held": frozenset({"prescribed", "verified", "stopped"}),
    "stopped": frozenset(),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "rejected": frozenset(),
}

#: Where an order came from. Validated here, not by a CHECK, so a later lane
#: (for example the outpatient prescription) adds its value without a rebuild.
#: 'opd_prescription' is the outpatient lane (opd-12, W8-C,
#: services/medorders/outpatient.py): encounter_type 'op', no ward row.
SOURCES = ("chart", "cpoe", "order_set", "sage_handoff", "relay_agentic", "helix_approve",
           "scribe_extracted", "history_import", "legacy_adopted", "verbal",
           "opd_prescription")

#: Chart provenance values that need a pharmacist before the first dose. The
#: same tuple as pharmacy_verification_service._UNVERIFIED_SOURCES and
#: emar_service._UNVERIFIED_SOURCES (pinned equal by
#: tests/test_medorder_state_machine.py).
UNVERIFIED_SOURCES = ("llm_extracted", "agentic")

#: The ledger's event kinds. Status changes, plus the supply and bedside events
#: the later lanes record against an order without changing its status.
EVENT_KINDS = (
    "created", "signed", "verified", "rejected", "held", "resumed", "stopped",
    "completed", "cancelled", "amended", "adopted",
    "supply_requested", "supply_reviewing", "supply_verified", "dispensed", "received",
    "administered", "not_given",
    # Pharmacy refused one supply row without ending the order (W7-E).
    "supply_rejected",
)

#: Steps that must say why. A hold, a resume, a stop, a cancel or a rejection
#: changes what the ward does next; the next nurse must be able to read why.
REASON_REQUIRED_KINDS = frozenset({"held", "resumed", "stopped", "cancelled", "rejected"})

ENCOUNTER_TYPES = ("ip", "op", "discharge")
PRIORITIES = ("routine", "urgent", "stat")
#: Who supplies the drug (ruling MO-R12 (a): the prescriber picks per order).
SUPPLY_SOURCES = ("pharmacy", "ward_stock", "patient_own")
PROJECTION_STATUSES = ("active", "prn")

#: The reasons adoption records for chart rows that ended before the store.
LEGACY_STOPPED_REASON = "stopped before the order store"
LEGACY_REJECTED_REASON = "rejected by the pharmacist before the order store"

_CPOE_STATUS = {
    "draft": "draft",
    "prescribed": "active", "verified": "active", "held": "active",
    "stopped": "cancelled", "cancelled": "cancelled", "rejected": "cancelled",
    "completed": "completed",
}

_FHIR_STATUS = {
    "draft": "draft",
    "prescribed": "active", "verified": "active",
    "held": "on-hold",
    "stopped": "stopped",
    "completed": "completed",
    "cancelled": "cancelled",
    "rejected": "cancelled",
}

# Plain words for a refused dose, written for the nurse at the bedside.
_ENDED_WORDS = {
    "stopped": "This medicine was stopped, so a dose cannot be given.",
    "completed": "This course is complete, so a dose cannot be given.",
    "cancelled": "This order was cancelled, so a dose cannot be given.",
    "rejected": "The pharmacist rejected this order, so a dose cannot be given.",
}
_HELD_WORDS = ("This medicine is on hold, so a dose cannot be given. "
               "You can still record it as not given (held).")
_DRAFT_WORDS = "This medicine has not been signed by a prescriber, so a dose cannot be given."
_UNKNOWN_WORDS = "The state of this order could not be read, so a dose cannot be given."


# ── Errors ────────────────────────────────────────────────────────────────────

class MedOrderError(Exception):
    """A refusal with a stable code, an HTTP status and words for the screen."""

    http_status = 422
    default_code = "medication_order_refused"

    def __init__(self, message: str, *, code: Optional[str] = None,
                 order: Optional[Mapping] = None, **detail: Any):
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.order = dict(order) if order is not None else None
        self.detail = detail


class MedOrderNotFound(MedOrderError):
    http_status = 404
    default_code = "medication_order_not_found"


class MedOrderInvalid(MedOrderError):
    """A value the store does not accept (a caller's mistake, not a race)."""
    http_status = 422
    default_code = "invalid_medication_order"


class NeedsInput(MedOrderError):
    http_status = 422
    default_code = "needs_input"


class NotPermitted(MedOrderError):
    http_status = 403
    default_code = "not_permitted"


class MedOrderConflict(MedOrderError):
    http_status = 409
    default_code = "medication_order_conflict"


class TransitionNotAllowed(MedOrderConflict):
    default_code = "transition_not_allowed"


class StaleOrderVersion(MedOrderConflict):
    """Someone changed the order since the caller read it."""
    default_code = "stale_version"


class StaleScreen(MedOrderConflict):
    """Allergies or the patient's live drugs changed since the safety screen
    ran: the client re-screens before the step can proceed."""
    default_code = "screen_stale"


class StoreUsageError(RuntimeError):
    """A writer was called outside a transaction. A programming error: never
    shown to a user, never caught to carry on."""


def http_detail(err: MedOrderError) -> dict:
    """The HTTP body for a refusal: {error, message, ...}, plus the current
    order (id, status, version) on a conflict so the client can refresh."""
    body = {"error": err.code, "message": err.message, **err.detail}
    if err.order is not None:
        body["order"] = {k: err.order.get(k) for k in ("id", "status", "version")}
    return body


class StoreWrite(NamedTuple):
    """What a writer returns: the order as it now stands, the ledger event it
    wrote (None when nothing was written), and whether this was a replay of an
    earlier request with the same idempotency key."""
    order: dict
    event_id: Optional[int]
    replayed: bool


# ── The switches, read at call time ───────────────────────────────────────────

def store_on() -> bool:
    """ORB_MED_ORDER_STORE (record mode)."""
    import config
    return config.feature_enabled("med_order_store")


def lifecycle_on() -> bool:
    """ORB_MED_ORDER_LIFECYCLE (authoritative mode); needs the store."""
    import config
    return config.feature_enabled("med_order_lifecycle")


# ── Pure functions ────────────────────────────────────────────────────────────

def _field(row: Any, name: str, default: Any = None) -> Any:
    """row[name] for a dict or a sqlite Row; `default` when the column is absent."""
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(name, default)
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def is_legal_transition(from_status: str, to_status: str, *,
                        held_from: Optional[str] = None) -> bool:
    """May an order in `from_status` move to `to_status`? A held order resumes
    only to the state it was held from."""
    allowed = LEGAL_TRANSITIONS.get(from_status)
    if allowed is None or to_status not in allowed:
        return False
    if from_status == "held" and to_status in HOLDABLE_FROM:
        return to_status == held_from
    return True


def event_kind_for(from_status: str, to_status: str) -> str:
    """The ledger event a status change writes."""
    if from_status == "draft" and to_status == "prescribed":
        return "signed"
    if from_status == "held" and to_status in HOLDABLE_FROM:
        return "resumed"
    if to_status in ("verified", "rejected", "held", "stopped", "completed", "cancelled"):
        return to_status
    raise MedOrderInvalid(f"No event for a change from {from_status} to {to_status}",
                          code="no_event_for_transition")


def order_allows_dose(order: Any, administration_status: str = "given") -> dict:
    """Does the ORDER allow this bedside record? For a linked ward row only,
    and IN ADDITION to the unchanged checks on the ward row itself (active
    status, pharmacist verification of an AI-charted drug).

    'not_given' is always allowed, on a held order too: withholding a dose must
    always be easy to record. 'given' is allowed while the order is prescribed
    or verified; a held, unsigned or finished order refuses it. Returns
    {allowed, code, message} and, for a hold, who held it, when and why."""
    if administration_status == "not_given":
        return {"allowed": True, "code": None, "message": None}
    status = _field(order, "status")
    if status in ("prescribed", "verified"):
        return {"allowed": True, "code": None, "message": None}
    if status == "held":
        return {
            "allowed": False, "code": "order_held", "message": _HELD_WORDS,
            "held_by": _field(order, "status_changed_by"),
            "held_at": _field(order, "status_changed_at"),
            "hold_reason": _field(order, "status_reason"),
        }
    if status == "draft":
        return {"allowed": False, "code": "order_not_signed", "message": _DRAFT_WORDS}
    if status in TERMINAL_STATUSES:
        return {"allowed": False, "code": "order_ended", "message": _ENDED_WORDS[status],
                "order_status": status}
    return {"allowed": False, "code": "order_state_unknown", "message": _UNKNOWN_WORDS}


def legacy_status_for(row: Any) -> dict:
    """How an existing medications row maps into the store (design section 1,
    ADOPTION). Pure, so a GET can show an unlinked row as an order without
    writing anything.

      active or prn                    -> prescribed, or verified when
                                          verified_by is set; the ward row's
                                          status becomes projection_status
      llm_extracted and agentic        -> needs_verification 1
      discontinued, AI source and      -> rejected (the pharmacist's reject is
      verified_by set                     the only writer of that combination)
      any other discontinued           -> stopped, projection_status NULL

    A status outside those three (a NULL slipped past the column CHECK, which
    lets NULL through) is NOT guessed at: 'adoptable' is False and the parity
    report lists the row."""
    legacy_status = _field(row, "status")
    source = _field(row, "source")
    verified_by = _field(row, "verified_by") or None
    verified_at = _field(row, "verified_at") or None
    needs = 1 if source in UNVERIFIED_SOURCES else 0
    out = {
        "status": None, "projection_status": None, "needs_verification": needs,
        "is_prn": 1 if legacy_status == "prn" else 0,
        "verified_by": verified_by, "verified_at": verified_at if verified_by else None,
        "status_reason": None, "adoptable": True,
    }
    if legacy_status in PROJECTION_STATUSES:
        out["status"] = "verified" if verified_by else "prescribed"
        out["projection_status"] = legacy_status
    elif legacy_status == "discontinued":
        if needs and verified_by:
            out["status"] = "rejected"
            out["status_reason"] = LEGACY_REJECTED_REASON
        else:
            out["status"] = "stopped"
            out["status_reason"] = LEGACY_STOPPED_REASON
    else:
        out["adoptable"] = False
    return out


def projection_for(order: Any) -> Optional[dict]:
    """The ward row's columns for an order in its current state; None for a
    draft, which has no ward row. Never clears verified_by (I1): the verifier
    is included only to copy it onto the row."""
    status = _field(order, "status")
    if status == "draft":
        return None
    if status in TERMINAL_STATUSES:
        return {"status": "discontinued", "held_at": None, "hold_reason": None}
    if status not in LIVE_STATUSES:
        raise MedOrderInvalid(f"Unknown order state {status!r}", code="unknown_status")
    projection_status = _field(order, "projection_status") or (
        "prn" if _field(order, "is_prn") else "active")
    out = {"status": projection_status, "held_at": None, "hold_reason": None}
    if status == "held":
        out["held_at"] = _field(order, "status_changed_at")
        out["hold_reason"] = _field(order, "status_reason")
    if _field(order, "verified_by"):
        out["verified_by"] = _field(order, "verified_by")
        out["verified_at"] = _field(order, "verified_at")
    return out


def fhir_status_for(status: str) -> dict:
    """FHIR MedicationRequest.status for an order state. A rejection is
    'cancelled' with a statusReason, since FHIR has no rejected state."""
    if status not in _FHIR_STATUS:
        raise MedOrderInvalid(f"Unknown order state {status!r}", code="unknown_status")
    return {"status": _FHIR_STATUS[status],
            "status_reason": "rejected" if status == "rejected" else None}


def cpoe_status_for(status: str) -> str:
    """orders.status for the CPOE view of an order (every value fits the
    orders CHECK)."""
    if status not in _CPOE_STATUS:
        raise MedOrderInvalid(f"Unknown order state {status!r}", code="unknown_status")
    return _CPOE_STATUS[status]


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def screen_fingerprint(allergies, live_order_ids, unlinked_medication_ids) -> str:
    """A stamp of everything the order-time safety screen depends on for one
    patient: each active allergy (id, substance, class, reaction, severity),
    the live orders and the unlinked live ward rows. A step screened outside
    its write transaction re-reads this inside it; a different stamp means an
    allergy or a drug was charted in between, and the step answers
    screen_stale instead of writing on a screen that no longer holds (W116c)."""
    payload = {
        "allergies": sorted(
            [str(_field(a, "id")), _norm(_field(a, "substance")),
             _norm(_field(a, "substance_class")), _norm(_field(a, "reaction")),
             _norm(_field(a, "severity"))]
            for a in allergies),
        "orders": sorted(str(i) for i in live_order_ids),
        "unlinked": sorted(str(i) for i in unlinked_medication_ids),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "v1:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── Readers (take a connection, write nothing) ────────────────────────────────

def get_order_in_txn(conn, order_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM med_orders WHERE id = ?", (order_id,)).fetchone()
    return dict(row) if row is not None else None


def events_in_txn(conn, order_id: str) -> list[dict]:
    """The order's ledger, oldest first."""
    rows = conn.execute(
        "SELECT * FROM med_order_events WHERE med_order_id = ? ORDER BY created_at, id",
        (order_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def screen_fingerprint_in_txn(conn, patient_id: str) -> str:
    """screen_fingerprint for a patient, read on the caller's connection."""
    allergies = conn.execute(
        "SELECT id, substance, substance_class, reaction, severity FROM patient_allergies "
        "WHERE patient_id = ? AND status = 'active'",
        (patient_id,),
    ).fetchall()
    orders = conn.execute(
        "SELECT id FROM med_orders WHERE patient_id = ? AND status IN (?, ?, ?)",
        (patient_id, *LIVE_STATUSES),
    ).fetchall()
    unlinked = conn.execute(
        "SELECT id FROM medications WHERE patient_id = ? AND status IN ('active', 'prn') "
        "AND med_order_id IS NULL",
        (patient_id,),
    ).fetchall()
    return screen_fingerprint([dict(a) for a in allergies], [r[0] for r in orders],
                              [r[0] for r in unlinked])


# ── Writers (the caller's transaction; never commit, never open a session) ────

def _require_txn(conn) -> None:
    if not getattr(conn, "in_transaction", False):
        raise StoreUsageError(
            "medication-order writers run inside the caller's write transaction: "
            "open db_session(immediate=True) first (or write the caller's own row "
            "first), then call this with that connection")


def _clean(value: Any) -> Optional[str]:
    text = (str(value) if value is not None else "").strip()
    return text or None


def _choice(value: Any, allowed: tuple, name: str) -> Optional[str]:
    if value is None:
        return None
    if value not in allowed:
        raise MedOrderInvalid(f"{name} must be one of {', '.join(allowed)}",
                              code=f"invalid_{name}")
    return value


def _safety_summary(safety: Optional[Mapping]) -> Optional[dict]:
    if not safety:
        return None
    return {
        "max_severity": safety.get("max_severity"),
        "blocking": safety.get("blocking"),
        "alerts": len(safety.get("allergy_alerts") or []) + len(safety.get("interaction_alerts") or []),
        "interaction_screened": safety.get("interaction_screened"),
        "overridden": bool(safety.get("override")),
        "dose_decision": (safety.get("dose_check") or {}).get("decision"),
    }


def audit_in_txn(conn, *, order: Mapping, action: str, actor_id: Optional[str],
                 actor_role: Optional[str], entry_point: Optional[str],
                 from_status: Optional[str] = None, to_status: Optional[str] = None,
                 reason: Optional[str] = None, lane: Optional[str] = None,
                 event_id: Optional[int] = None, safety: Optional[Mapping] = None,
                 extra: Optional[Mapping] = None,
                 request_meta: Optional[Mapping] = None) -> None:
    """The clinical_access audit row for one store write, on the caller's
    connection (fail-closed: an unwritable audit raises and rolls the write
    back)."""
    meta = dict(request_meta or {})
    legacy_id = order.get("legacy_medication_id")
    log_audit_event(
        correlation_id=meta.get("correlation_id") or str(uuid.uuid4()),
        event_category="clinical_access",
        action_type=action,
        outcome="success",
        db=conn,
        summary_message=(
            f"Medication order {order.get('drug_name')}: "
            f"{from_status or 'new'} to {to_status or order.get('status')}"
            + (f" ({entry_point})" if entry_point else "")
        ),
        actor_user_id=actor_id,
        actor_role=actor_role,
        target_type="med_order",
        target_id=str(order["id"]),
        patient_id=order.get("patient_id"),
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        session_id=meta.get("session_id"),
        metadata={
            "from": from_status,
            "to": to_status or order.get("status"),
            "version": order.get("version"),
            "entry_point": entry_point,
            "lane": lane,
            "reason": reason,
            "source": order.get("source"),
            "legacy_medication_id": str(legacy_id) if legacy_id is not None else None,
            "event_id": event_id,
            "safety": _safety_summary(safety),
            **dict(extra or {}),
        },
    )


def record_event_in_txn(conn, *, order: Mapping, kind: str,
                        from_status: Optional[str] = None, to_status: Optional[str] = None,
                        actor_id: Optional[str] = None, actor_role: Optional[str] = None,
                        entry_point: Optional[str] = None, reason: Optional[str] = None,
                        ref_type: Optional[str] = None, ref_id: Any = None,
                        payload: Optional[Mapping] = None,
                        idempotency_key: Optional[str] = None,
                        created_at: Optional[str] = None) -> int:
    """Append one ledger event for `order` and return its id. Writes no audit
    (the caller's step audits once for the whole change)."""
    _require_txn(conn)
    if kind not in EVENT_KINDS:
        raise MedOrderInvalid(f"Unknown medication order event {kind!r}", code="invalid_event_kind")
    cur = conn.execute(
        "INSERT INTO med_order_events (med_order_id, patient_id, kind, from_status, to_status, "
        "actor_id, actor_role, entry_point, reason, ref_type, ref_id, payload_json, "
        "idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order["id"], order["patient_id"], kind, from_status, to_status, actor_id, actor_role,
         entry_point, _clean(reason), ref_type, str(ref_id) if ref_id is not None else None,
         json.dumps(payload, sort_keys=True, default=str) if payload else None,
         idempotency_key, created_at or utc_iso_now()),
    )
    return int(cur.lastrowid)


def _event_for_key(conn, idempotency_key: Optional[str]) -> Optional[dict]:
    if not idempotency_key:
        return None
    row = conn.execute("SELECT * FROM med_order_events WHERE idempotency_key = ?",
                       (idempotency_key,)).fetchone()
    return dict(row) if row is not None else None


_CREATABLE = ("draft", "prescribed", "verified", "stopped", "completed", "cancelled", "rejected")


def create_in_txn(
    conn, *,
    patient_id: str,
    drug_name: str,
    source: str,
    status: str,
    entered_by: str,
    actor_id: Optional[str],
    entry_point: str,
    actor_role: Optional[str] = None,
    entered_by_role: Optional[str] = None,
    admission_id: Optional[str] = None,
    encounter_type: str = "ip",
    needs_verification: bool = False,
    formulary_id: Optional[str] = None,
    dose: Optional[str] = None,
    route: Optional[str] = None,
    frequency: Optional[str] = None,
    schedule_kind: Optional[str] = None,
    is_prn: bool = False,
    prn_indication: Optional[str] = None,
    priority: Optional[str] = None,
    start_at: Optional[str] = None,
    end_at: Optional[str] = None,
    duration_text: Optional[str] = None,
    indication: Optional[str] = None,
    notes: Optional[str] = None,
    supply_source: Optional[str] = None,
    prescriber_id: Optional[str] = None,
    prescribed_at: Optional[str] = None,
    verified_by: Optional[str] = None,
    verified_at: Optional[str] = None,
    status_reason: Optional[str] = None,
    projection_status: Optional[str] = None,
    legacy_medication_id: Optional[int] = None,
    cpoe_order_id: Optional[str] = None,
    order_set_application_id: Optional[int] = None,
    sage_action_id: Optional[str] = None,
    sage_handoff_id: Optional[str] = None,
    scribe_note_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    facility_id: Optional[str] = None,
    safety: Optional[Mapping] = None,
    fingerprint: Optional[str] = None,
    event_kind: str = "created",
    event_payload: Optional[Mapping] = None,
    lane: Optional[str] = None,
    audit: bool = True,
    request_meta: Optional[Mapping] = None,
    now: Optional[str] = None,
) -> StoreWrite:
    """Insert one order with its first ledger event (and, unless `audit` is
    False, its clinical_access audit row) in the caller's transaction.

    An order is born in any state but 'held' (a hold needs a state to return
    to). A live order gets projection_status 'prn' or 'active' from is_prn
    unless the caller names it; a finished order has none. A repeated
    idempotency_key returns the first order (replayed=True) when it names the
    same patient and drug, and is refused otherwise.

    `audit=False` is for adoption, which writes one audit row per batch."""
    _require_txn(conn)
    drug = _clean(drug_name)
    if not drug:
        raise NeedsInput("Enter the medicine's name", code="drug_name_required")
    if not _clean(patient_id):
        raise NeedsInput("A medication order needs a patient", code="patient_required")
    if not _clean(entered_by):
        raise MedOrderInvalid("A medication order needs the person who entered it",
                              code="entered_by_required")
    if status not in _CREATABLE:
        raise MedOrderInvalid(f"An order cannot start as {status!r}", code="invalid_status")
    if source not in SOURCES:
        raise MedOrderInvalid(f"Unknown order source {source!r}", code="invalid_source")
    if event_kind not in ("created", "adopted"):
        raise MedOrderInvalid("A new order's first event is 'created' or 'adopted'",
                              code="invalid_event_kind")
    _choice(encounter_type, ENCOUNTER_TYPES, "encounter_type")
    _choice(priority, PRIORITIES, "priority")
    _choice(supply_source, SUPPLY_SOURCES, "supply_source")
    _choice(projection_status, PROJECTION_STATUSES, "projection_status")
    if status in LIVE_STATUSES:
        projection_status = projection_status or ("prn" if is_prn else "active")
    elif projection_status is not None:
        raise MedOrderInvalid("Only a live order has a ward status",
                              code="invalid_projection_status")
    if status == "verified" and not _clean(verified_by):
        raise MedOrderInvalid("A verified order names its verifier", code="verified_by_required")

    if idempotency_key:
        prior = conn.execute("SELECT * FROM med_orders WHERE idempotency_key = ?",
                             (idempotency_key,)).fetchone()
        if prior is not None:
            prior = dict(prior)
            if prior["patient_id"] == patient_id and prior["drug_name"] == drug:
                return StoreWrite(prior, None, True)
            raise MedOrderConflict("This request key was already used for another order",
                                   code="idempotency_key_reused")

    now = now or utc_iso_now()
    if event_kind == "created" and status != "draft" and prescribed_at is None and prescriber_id:
        # Prescribed now, by the person named. An adopted row keeps the time
        # the chart recorded (or none), never the time it was adopted.
        prescribed_at = now
    order_id = str(uuid.uuid4())
    row = {
        "id": order_id, "patient_id": patient_id, "admission_id": admission_id,
        "encounter_type": encounter_type, "status": status, "held_from": None, "version": 1,
        "source": source, "needs_verification": 1 if needs_verification else 0,
        "drug_name": drug, "formulary_id": formulary_id,
        "dose": _clean(dose), "route": _clean(route), "frequency": _clean(frequency),
        "schedule_kind": schedule_kind, "is_prn": 1 if is_prn else 0,
        "prn_indication": _clean(prn_indication), "priority": priority,
        "start_at": start_at, "end_at": end_at, "duration_text": _clean(duration_text),
        "indication": _clean(indication), "notes": _clean(notes),
        "supply_source": supply_source,
        "entered_by": entered_by, "entered_by_role": entered_by_role,
        "prescriber_id": prescriber_id, "prescribed_at": prescribed_at,
        "verified_by": _clean(verified_by), "verified_at": verified_at if verified_by else None,
        "status_reason": _clean(status_reason), "status_changed_by": actor_id,
        "status_changed_at": now,
        "last_safety_json": json.dumps(safety, sort_keys=True, default=str) if safety else None,
        "last_screened_at": now if safety else None,
        "screen_fingerprint": fingerprint,
        "projection_status": projection_status,
        "legacy_medication_id": legacy_medication_id, "cpoe_order_id": cpoe_order_id,
        "order_set_application_id": order_set_application_id,
        "sage_action_id": sage_action_id, "sage_handoff_id": sage_handoff_id,
        "scribe_note_id": scribe_note_id, "idempotency_key": idempotency_key,
        "facility_id": facility_id, "created_at": now, "updated_at": now,
    }
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO med_orders ({cols}) VALUES ({marks})", tuple(row.values()))
    event_id = record_event_in_txn(
        conn, order=row, kind=event_kind, from_status=None, to_status=status,
        actor_id=actor_id, actor_role=actor_role, entry_point=entry_point,
        reason=status_reason, payload=event_payload, created_at=now,
    )
    if audit:
        audit_in_txn(conn, order=row, action=f"med_order_{event_kind}", actor_id=actor_id,
                     actor_role=actor_role, entry_point=entry_point, from_status=None,
                     to_status=status, reason=status_reason, lane=lane, event_id=event_id,
                     safety=safety, request_meta=request_meta)
    return StoreWrite(row, event_id, False)


def link_projection_in_txn(conn, order_id: str, medication_id: int) -> None:
    """Join an order and its ward row, both ways: medications.med_order_id and
    med_orders.legacy_medication_id. Idempotent for the same pair; refuses a
    row or an order that is already linked elsewhere (both columns are unique)."""
    _require_txn(conn)
    order = get_order_in_txn(conn, order_id)
    if order is None:
        raise MedOrderNotFound("This medication order no longer exists")
    med = conn.execute("SELECT id, patient_id, med_order_id, status FROM medications "
                       "WHERE id = ?", (medication_id,)).fetchone()
    if med is None:
        raise MedOrderNotFound("This medication is no longer on the chart",
                               code="projection_not_found")
    if med["patient_id"] != order["patient_id"]:
        raise MedOrderConflict("The chart row belongs to another patient",
                               code="projection_patient_mismatch", order=order)
    if order["status"] == "draft":
        raise MedOrderConflict("An unsigned order has no chart row",
                               code="draft_has_no_ward_row", order=order)
    if med["status"] != projection_for(order)["status"]:
        # Linking must never change what the ward sees: the row already says
        # what the order says, or the two are not the same order.
        raise MedOrderConflict("The chart row does not match this order",
                               code="projection_mismatch", order=order)
    if med["med_order_id"] not in (None, order_id):
        raise MedOrderConflict("This chart row already belongs to another order",
                               code="projection_already_linked", order=order)
    if order["legacy_medication_id"] not in (None, medication_id):
        raise MedOrderConflict("This order already has another chart row",
                               code="order_already_linked", order=order)
    conn.execute("UPDATE medications SET med_order_id = ? WHERE id = ? AND med_order_id IS NULL",
                 (order_id, medication_id))
    conn.execute("UPDATE med_orders SET legacy_medication_id = ? "
                 "WHERE id = ? AND legacy_medication_id IS NULL",
                 (medication_id, order_id))


def project_in_txn(conn, order: Mapping) -> int:
    """Write projection_for(order) onto the order's linked ward row. Returns
    the rows updated (0 for a draft, or an order with no ward row yet)."""
    _require_txn(conn)
    projection = projection_for(order)
    if projection is None:
        return 0
    cols = ", ".join(f"{k} = ?" for k in projection)
    cur = conn.execute(f"UPDATE medications SET {cols} WHERE med_order_id = ?",
                       (*projection.values(), order["id"]))
    return cur.rowcount


def transition_in_txn(
    conn, order_id: str, to_status: str, *,
    expected_version: Optional[int],
    actor_id: Optional[str],
    actor_role: Optional[str],
    entry_point: str,
    reason: Optional[str] = None,
    lane: Optional[str] = None,
    payload: Optional[Mapping] = None,
    safety: Optional[Mapping] = None,
    fingerprint: Optional[str] = None,
    ref_type: Optional[str] = None,
    ref_id: Any = None,
    idempotency_key: Optional[str] = None,
    project: bool = True,
    audit_extra: Optional[Mapping] = None,
    request_meta: Optional[Mapping] = None,
    now: Optional[str] = None,
) -> StoreWrite:
    """Move one order to `to_status` in the caller's transaction: the guarded
    UPDATE (id, version and status must still be what was read), its ledger
    event, the ward projection (unless `project` is False, for a legacy writer
    that already wrote the ward row itself) and the clinical_access audit row.

    `expected_version` is the version the caller showed the user; None means
    the caller read the order in this same transaction. Either way the UPDATE
    is guarded, so a concurrent change raises StaleOrderVersion rather than
    being overwritten. A repeated idempotency_key for the same order and step
    returns the order as it stands (replayed=True) and writes nothing."""
    _require_txn(conn)
    order = get_order_in_txn(conn, order_id)
    if order is None:
        raise MedOrderNotFound("This medication order no longer exists")
    if to_status not in STATUSES:
        raise MedOrderInvalid(f"Unknown order state {to_status!r}", code="invalid_status")

    prior = _event_for_key(conn, idempotency_key)
    if prior is not None:
        if prior["med_order_id"] == order_id and prior["to_status"] == to_status:
            return StoreWrite(order, prior["id"], True)
        raise MedOrderConflict("This request key was already used for another step",
                               code="idempotency_key_reused", order=order)

    if expected_version is not None and order["version"] != expected_version:
        raise StaleOrderVersion("This order was changed by someone else. Refresh and try again.",
                                order=order)
    from_status = order["status"]
    if not is_legal_transition(from_status, to_status, held_from=order["held_from"]):
        raise TransitionNotAllowed(
            f"A {from_status} order cannot become {to_status}", order=order,
            from_status=from_status, to_status=to_status)
    kind = event_kind_for(from_status, to_status)
    reason_text = _clean(reason)
    if kind in REASON_REQUIRED_KINDS and not reason_text:
        raise NeedsInput("Say why", code="reason_required", order=order)
    if kind in ("signed", "verified") and not _clean(actor_id):
        # The signer becomes the prescriber of record and the verifier is
        # copied to the ward row: neither step may be anonymous.
        raise MedOrderInvalid(f"A {kind} step names the person who took it",
                              code="actor_required", order=order)

    now = now or utc_iso_now()
    sets: dict[str, Any] = {
        "status": to_status,
        "held_from": from_status if to_status == "held" else None,
        "version": order["version"] + 1,
        "status_reason": reason_text,
        "status_changed_by": actor_id,
        "status_changed_at": now,
        "updated_at": now,
    }
    if kind == "signed":
        sets["prescriber_id"] = actor_id
        sets["prescribed_at"] = now
        sets["projection_status"] = order["projection_status"] or (
            "prn" if order["is_prn"] else "active")
    if kind == "verified":
        sets["verified_by"] = actor_id
        sets["verified_at"] = now
    if to_status in TERMINAL_STATUSES:
        sets["projection_status"] = None
    if safety is not None:
        sets["last_safety_json"] = json.dumps(safety, sort_keys=True, default=str)
        sets["last_screened_at"] = now
    if fingerprint is not None:
        sets["screen_fingerprint"] = fingerprint
    assignments = ", ".join(f"{k} = ?" for k in sets)
    cur = conn.execute(
        f"UPDATE med_orders SET {assignments} WHERE id = ? AND version = ? AND status = ?",
        (*sets.values(), order_id, order["version"], from_status),
    )
    if cur.rowcount != 1:
        raise StaleOrderVersion("This order was changed by someone else. Refresh and try again.",
                                order=get_order_in_txn(conn, order_id) or order)
    after = get_order_in_txn(conn, order_id)
    event_id = record_event_in_txn(
        conn, order=after, kind=kind, from_status=from_status, to_status=to_status,
        actor_id=actor_id, actor_role=actor_role, entry_point=entry_point, reason=reason_text,
        ref_type=ref_type, ref_id=ref_id, payload=payload, idempotency_key=idempotency_key,
        created_at=now,
    )
    if project:
        project_in_txn(conn, after)
    mirror = _mirror_cpoe_view_in_txn(conn, after, from_status=from_status, kind=kind,
                                      actor_id=actor_id, reason=reason_text)
    if mirror is not None:
        audit_extra = {**dict(audit_extra or {}), "cpoe_mirror": mirror}
    audit_in_txn(conn, order=after, action=f"med_order_{kind}", actor_id=actor_id,
                 actor_role=actor_role, entry_point=entry_point, from_status=from_status,
                 to_status=to_status, reason=reason_text, lane=lane, event_id=event_id,
                 safety=safety, extra=audit_extra, request_meta=request_meta)
    return StoreWrite(after, event_id, False)


def _mirror_cpoe_view_in_txn(conn, order: Mapping, *, from_status: str, kind: str,
                             actor_id: Optional[str], reason: Optional[str]) -> Optional[dict]:
    """The CPOE view of a linked order follows its status (design section 2,
    "CPOE mirror"; medorders-7, W7-F), in the caller's transaction, keyed on
    the link and never on the switches. services/medorders/cpoe.py holds the
    rule; None for an order with no CPOE row (every order before W7-F)."""
    if not order.get("cpoe_order_id"):
        return None
    from services.medorders import cpoe
    return cpoe.mirror_cpoe_status_in_txn(conn, order, from_status=from_status, kind=kind,
                                          actor_id=actor_id, reason=reason)


def legacy_decision_on_held_in_txn(
    conn, order_id: str, decision: str, *,
    actor_id: Optional[str],
    actor_role: Optional[str],
    entry_point: str,
    reason: Optional[str] = None,
    lane: Optional[str] = None,
    audit_extra: Optional[Mapping] = None,
    request_meta: Optional[Mapping] = None,
    now: Optional[str] = None,
) -> StoreWrite:
    """A pharmacist's decision taken on the legacy Verify Queue while the order
    is HELD (W5-E, the legacy verify and reject mirror; design section 5).
    Neither is a step the state machine offers a store route, so neither goes
    through transition_in_txn:

      'verified'  the order stays held and its hold is untouched (who held it,
                  when and why still answer the bedside); the verifier is
                  recorded and held_from becomes 'verified', so a resume
                  returns to verified. The legacy writer has already put the
                  verifier on the ward row (invariant I1).
      'rejected'  the order ends as rejected (a reason is required) and its
                  ward row loses the hold marker; the legacy writer has
                  already marked the row discontinued.

    Same guarded UPDATE, ledger event and clinical_access audit row as
    transition_in_txn, in the caller's transaction."""
    _require_txn(conn)
    if decision not in ("verified", "rejected"):
        raise MedOrderInvalid(f"Unknown pharmacist decision {decision!r}",
                              code="invalid_decision")
    order = get_order_in_txn(conn, order_id)
    if order is None:
        raise MedOrderNotFound("This medication order no longer exists")
    if order["status"] != "held" or (decision == "verified"
                                     and order["held_from"] != "prescribed"):
        raise TransitionNotAllowed(
            f"A {order['status']} order cannot be {decision} here", order=order,
            from_status=order["status"], to_status=decision)
    reason_text = _clean(reason)
    if decision == "rejected" and not reason_text:
        raise NeedsInput("Say why", code="reason_required", order=order)
    if not _clean(actor_id):
        raise MedOrderInvalid(f"A {decision} step names the person who took it",
                              code="actor_required", order=order)

    now = now or utc_iso_now()
    sets: dict[str, Any] = {"version": order["version"] + 1, "updated_at": now}
    if decision == "verified":
        sets.update({"held_from": "verified", "verified_by": actor_id, "verified_at": now})
    else:
        sets.update({"status": "rejected", "held_from": None, "projection_status": None,
                     "status_reason": reason_text, "status_changed_by": actor_id,
                     "status_changed_at": now})
    assignments = ", ".join(f"{k} = ?" for k in sets)
    cur = conn.execute(
        f"UPDATE med_orders SET {assignments} WHERE id = ? AND version = ? AND status = 'held'",
        (*sets.values(), order_id, order["version"]),
    )
    if cur.rowcount != 1:
        raise StaleOrderVersion("This order was changed by someone else. Refresh and try again.",
                                order=get_order_in_txn(conn, order_id) or order)
    after = get_order_in_txn(conn, order_id)
    event_id = record_event_in_txn(
        conn, order=after, kind=decision, from_status="held", to_status=after["status"],
        actor_id=actor_id, actor_role=actor_role, entry_point=entry_point,
        reason=reason_text, payload={"while_held": True}, created_at=now,
    )
    if decision == "rejected":
        project_in_txn(conn, after)
    mirror = _mirror_cpoe_view_in_txn(conn, after, from_status="held", kind=decision,
                                      actor_id=actor_id, reason=reason_text)
    if mirror is not None:
        audit_extra = {**dict(audit_extra or {}), "cpoe_mirror": mirror}
    audit_in_txn(conn, order=after, action=f"med_order_{decision}", actor_id=actor_id,
                 actor_role=actor_role, entry_point=entry_point, from_status="held",
                 to_status=after["status"], reason=reason_text, lane=lane, event_id=event_id,
                 extra={"while_held": True, **dict(audit_extra or {})},
                 request_meta=request_meta)
    return StoreWrite(after, event_id, False)
