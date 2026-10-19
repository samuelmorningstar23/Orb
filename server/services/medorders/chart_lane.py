"""Orb: the chart lane through the medication-order store, the legacy verify
and reject mirror, and the store's own create, sign and draft cancel
(medorders-3 and medorders-4, W5-E).

Everything here puts a medicine on the ward chart (a medications row, the ward
projection) or changes one that is there, and does it through the store's
in-transaction writers in services/medorders/core.py, so an order, its ward
row, its ledger event and its clinical_access audit row commit together or not
at all.

THE CHART ADD (vigil.med_add), with ORB_MED_ORDER_STORE on. The Vigil route
calls create_chart_order through run_db, which also takes the safety gate off
the event loop (defect F-D). It runs the SAME gate call the route ran, then ONE
immediate transaction: the idempotency pre-check and the medications INSERT
exactly as the route's _insert_medication did them, the order (prescribed,
prescriber = the person charting it, their role recorded), its 'created' event
and its audit row, and the link both ways. Status 'prn' gives a prn order;
'discontinued' at add gives a stopped order. Nothing else changes: a nurse
still charts a live drug here and the gate still decides who may clear an
alert (held rulings R010 and R011). With the store off the route never calls
this module.

THE LEGACY MIRROR (invariant I2). The Verify Queue's verify and reject still
write the ward row themselves (pharmacy_verification_service). When that row is
LINKED to an order, mirror_legacy_decision_in_txn moves the order in the same
transaction, keyed on the link and not on the switches: a verification is
never lost from the store because a switch was off, and a store that refuses
the step undoes the legacy write too. An unlinked row is untouched.

THE STORE'S OWN STEPS (/api/medorders, lifecycle mode): create (a draft, or
created and signed in one step), sign and draft cancel. Each runs the gate
OUTSIDE the write transaction (its sessions and its denial audit rows must not
share or roll back with the step), reading the screen fingerprint first; then
one db_session(immediate=True) re-reads the fingerprint and answers
screen_stale when an allergy or a drug was charted in between, and writes the
order, its event, its ward row and its audit together. Same-ingredient
duplicates come back as a notice on the reply; the gate itself applies ruling
MO-R6 at create, sign and the chart add while the lifecycle is on
(services/medorders/screening.py, W9-C), and a replay is screened with its
own first write left out.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from database import db_session
from services import medication_safety_gate
from services.clinical_time import to_utc_iso, utc_iso_now
from services.medorders import core, queries

logger = logging.getLogger("orb.medorders.chart_lane")

__all__ = [
    "ENTRY_CHART_ADD", "LANE_CHART", "ENTRY_LEGACY_VERIFY", "ENTRY_LEGACY_REJECT", "LANE_LEGACY",
    "ENTRY_CREATE", "ENTRY_SIGN", "ENTRY_CANCEL", "LANE_CPOE",
    "DISCONTINUED_AT_ADD_REASON", "STALE_SCREEN_MESSAGE",
    "insert_ward_row_in_txn", "create_chart_order", "write_chart_order_in_txn",
    "mirror_legacy_decision_in_txn", "create_order", "sign_order", "cancel_draft",
]

ENTRY_CHART_ADD = "vigil.med_add"
LANE_CHART = "chart"
ENTRY_LEGACY_VERIFY = "pharmacy.verify_queue.verify"
ENTRY_LEGACY_REJECT = "pharmacy.verify_queue.reject"
LANE_LEGACY = "legacy_verify_queue"
ENTRY_CREATE = "medorder.create.cpoe"
ENTRY_SIGN = "medorder.sign.cpoe"
ENTRY_CANCEL = "medorder.cancel.cpoe"
LANE_CPOE = "cpoe"

#: The stopped order's reason when the add form's 'Discontinued' was chosen.
DISCONTINUED_AT_ADD_REASON = "Charted as discontinued when it was added"

STALE_SCREEN_MESSAGE = ("An allergy or a medicine was charted for this patient while this "
                        "order was being checked. Check the order again.")

#: The add form's status -> (order status, is_prn).
_CHART_STATUS = {
    "active": ("prescribed", False),
    "prn": ("prescribed", True),
    "discontinued": ("stopped", False),
}

#: How each order state reads in a refusal the Verify Queue prints as it is.
_STATE_WORDS = {
    "draft": "not signed yet",
    "verified": "already checked",
    "stopped": "stopped",
    "completed": "complete",
    "cancelled": "cancelled",
    "rejected": "rejected",
}


def _require_txn(conn) -> None:
    if not getattr(conn, "in_transaction", False):
        raise core.StoreUsageError(
            "the chart lane writes inside the caller's write transaction: open "
            "db_session(immediate=True) first, or write the legacy row first")


def _canonical(value: Any) -> str:
    """A stored instant in the one UTC spelling; now when it cannot be read."""
    if value in (None, ""):
        return utc_iso_now()
    try:
        return to_utc_iso(value)
    except (ValueError, TypeError):
        return utc_iso_now()


def _role_of(conn, user_id: Optional[str]) -> Optional[str]:
    if not user_id:
        return None
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["role"] if row else None


# ── The ward row ──────────────────────────────────────────────────────────────

def insert_ward_row_in_txn(conn, *, patient_id: str, drug_name: str, dosage: str,
                           route: Optional[str], frequency: str, status: str,
                           prescribed_by: Optional[str], idempotency_key: Optional[str] = None,
                           notes: Optional[str] = None) -> int:
    """One medications row, exactly as the chart add writes it (source
    'clinician', so the Verify Queue does not list it), in the caller's
    transaction. Returns its id.

    The values go in as given, as the route's _insert_medication wrote them:
    the add form's own default route ('Oral' when the body names none) is the
    route's business, and a route this function made up would put a fact on
    the chart nobody charted."""
    _require_txn(conn)
    cur = conn.execute(
        "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
        "prescribed_by, source, idempotency_key, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'clinician', ?, ?)",
        (patient_id, drug_name, dosage, route, frequency, status, prescribed_by,
         idempotency_key, notes),
    )
    return int(cur.lastrowid)


def _project_new_order_in_txn(conn, order: Mapping, *, prescribed_by: Optional[str]) -> int:
    """Write the ward row for an order that has just become live, and link
    the two. The row says exactly what projection_for says."""
    projection = core.projection_for(order)
    med_id = insert_ward_row_in_txn(
        conn, patient_id=order["patient_id"], drug_name=order["drug_name"],
        dosage=order["dose"], route=order["route"], frequency=order["frequency"],
        status=projection["status"], prescribed_by=prescribed_by, notes=order.get("notes"),
    )
    core.link_projection_in_txn(conn, order["id"], med_id)
    return med_id


# ── The chart add (vigil.med_add) ─────────────────────────────────────────────

def _replay_exclusion(idempotency_key: Optional[str]) -> dict:
    """{'exclude_med_id': the ward row an earlier request with this key
    wrote} with the lifecycle on, so a replay is screened as its first
    request was (medorders-9); {} otherwise, so the gate call is unchanged."""
    if not idempotency_key or not core.lifecycle_on():
        return {}
    with db_session() as conn:
        row = conn.execute("SELECT id FROM medications WHERE idempotency_key = ?",
                           (idempotency_key,)).fetchone()
    return {"exclude_med_id": row["id"]} if row is not None else {}


def _order_replay_exclusion(idempotency_key: Optional[str]) -> dict:
    """The same for the store's own create: {'exclude_order_id': the order an
    earlier request with this key made}, or {}."""
    if not idempotency_key:
        return {}
    with db_session() as conn:
        row = conn.execute("SELECT id FROM med_orders WHERE idempotency_key = ?",
                           (idempotency_key,)).fetchone()
    return {"exclude_order_id": row["id"]} if row is not None else {}


def create_chart_order(patient_id: str, drug_name: str, dosage: str, route: Optional[str],
                       frequency: str, status: str, *, actor_id: str,
                       actor_role: Optional[str] = None,
                       idempotency_key: Optional[str] = None,
                       safety_ack: Optional[dict] = None,
                       request_meta: Optional[Mapping] = None) -> dict:
    """The Vigil chart add with the store on. BLOCKING: call it through run_db.

    The gate first, as the route always ran it (before the idempotency
    pre-check, so a replay screens identically), and outside any write
    transaction; it raises the route's 428 or 503 itself. Then one immediate
    transaction writes the ward row and its order. Returns {id, med_order_id,
    replayed, safety}; `id` is the ward row, as the route has always answered.

    With the lifecycle on (medorders-9, W9-C) a replay is screened with the
    row its first request wrote left out (exclude_med_id): otherwise the
    same-ingredient rule (MO-R6) would refuse a replay as a duplicate of
    itself, and the offline client treats that 4xx as a lost dose."""
    safety = medication_safety_gate.enforce_medication_safety(
        patient_id,
        drug_name,
        actor_id=actor_id,
        entry_point=ENTRY_CHART_ADD,
        safety_ack=safety_ack,
        dose=dosage,
        route=route,
        actor_role=actor_role,
        **_replay_exclusion(idempotency_key),
    )
    with db_session(immediate=True) as conn:
        out = write_chart_order_in_txn(
            conn, patient_id=patient_id, drug_name=drug_name, dosage=dosage, route=route,
            frequency=frequency, status=status, actor_id=actor_id, actor_role=actor_role,
            idempotency_key=idempotency_key, safety=safety, request_meta=request_meta,
        )
    return {**out, "safety": safety}


def write_chart_order_in_txn(conn, *, patient_id: str, drug_name: str, dosage: str,
                             route: Optional[str], frequency: str, status: str,
                             actor_id: str, actor_role: Optional[str],
                             idempotency_key: Optional[str] = None,
                             safety: Optional[Mapping] = None,
                             request_meta: Optional[Mapping] = None) -> dict:
    """The write half of the chart add, in the caller's immediate transaction.

    A repeated Idempotency-Key returns the ward row it made (and its order,
    if any) and writes nothing. Otherwise the ward row, the order, its
    'created' event, its audit row and the link."""
    _require_txn(conn)
    if idempotency_key:
        existing = conn.execute(
            "SELECT id, med_order_id FROM medications WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return {"id": existing["id"], "med_order_id": existing["med_order_id"],
                    "replayed": True}
    # The ward row first: its CHECK refuses a status the add form cannot send,
    # exactly as the route's own INSERT did, before any order is written.
    med_id = insert_ward_row_in_txn(
        conn, patient_id=patient_id, drug_name=drug_name, dosage=dosage, route=route,
        frequency=frequency, status=status, prescribed_by=actor_id,
        idempotency_key=idempotency_key,
    )
    mapped = _CHART_STATUS.get(status)
    if mapped is None:
        raise core.MedOrderInvalid(f"A chart medicine cannot start as {status!r}",
                                   code="invalid_status")
    order_status, is_prn = mapped
    write = core.create_in_txn(
        conn,
        patient_id=patient_id,
        drug_name=drug_name,
        source="chart",
        status=order_status,
        entered_by=actor_id,
        entered_by_role=actor_role,
        actor_id=actor_id,
        actor_role=actor_role,
        entry_point=ENTRY_CHART_ADD,
        admission_id=queries.active_admission_id(conn, patient_id),
        dose=dosage,
        route=route,
        frequency=frequency,
        is_prn=is_prn,
        prescriber_id=actor_id,
        status_reason=DISCONTINUED_AT_ADD_REASON if order_status == "stopped" else None,
        legacy_medication_id=med_id,
        safety=safety,
        lane=LANE_CHART,
        request_meta=request_meta,
    )
    core.link_projection_in_txn(conn, write.order["id"], med_id)
    return {"id": med_id, "med_order_id": write.order["id"], "replayed": False}


# ── The legacy verify and reject mirror (invariant I2) ───────────────────────

def _refusal(order: Mapping, decision: str) -> core.TransitionNotAllowed:
    words = _STATE_WORDS.get(order.get("status"), "in a state that cannot change here")
    verb = "checked" if decision == "verified" else "rejected"
    return core.TransitionNotAllowed(
        f"This medicine's order is {words}, so it cannot be {verb} here. Refresh the queue.",
        order=order, from_status=order.get("status"), to_status=decision)


#: Ward row column -> order column, for a correction the Verify Queue made.
_CORRECTABLE = (("dosage", "dose"), ("route", "route"), ("frequency", "frequency"))


def _mirror_correction_in_txn(conn, order: Mapping, ward_row: Mapping, *,
                              actor_id: Optional[str], actor_role: Optional[str],
                              entry_point: str, extra: Mapping,
                              request_meta: Optional[Mapping], now: str) -> Optional[int]:
    """Copy the pharmacist's correction from the ward row onto the order as an
    'amended' event with the before and after values, in the caller's
    transaction. Returns the event id, or None when nothing differs (the
    usual case: a verification without a correction).

    The same guarded UPDATE as every store write (id, version and status must
    still be what was read), its ledger event and its clinical_access audit
    row. The status is untouched; the verification that follows is its own
    step."""
    changes = {}
    for ward_column, order_column in _CORRECTABLE:
        after = (str(ward_row[ward_column]).strip() if ward_row[ward_column] is not None
                 else "") or None
        before = order.get(order_column)
        if after != before:
            changes[order_column] = {"before": before, "after": after}
    if not changes:
        return None
    sets: dict[str, Any] = {column: change["after"] for column, change in changes.items()}
    sets.update({"version": order["version"] + 1, "updated_at": now})
    assignments = ", ".join(f"{column} = ?" for column in sets)
    cur = conn.execute(
        f"UPDATE med_orders SET {assignments} WHERE id = ? AND version = ? AND status = ?",
        (*sets.values(), order["id"], order["version"], order["status"]),
    )
    if cur.rowcount != 1:
        raise core.StaleOrderVersion(
            "This order was changed by someone else. Refresh and try again.",
            order=core.get_order_in_txn(conn, order["id"]) or order)
    after_order = core.get_order_in_txn(conn, order["id"])
    event_id = core.record_event_in_txn(
        conn, order=after_order, kind="amended", from_status=order["status"],
        to_status=order["status"], actor_id=actor_id, actor_role=actor_role,
        entry_point=entry_point, payload={"changes": changes, "corrected_at_verification": True},
        created_at=now,
    )
    core.audit_in_txn(
        conn, order=after_order, action="med_order_amended", actor_id=actor_id,
        actor_role=actor_role, entry_point=entry_point, from_status=order["status"],
        to_status=order["status"], lane=LANE_LEGACY, event_id=event_id,
        extra={**dict(extra), "amended_fields": sorted(changes)}, request_meta=request_meta)
    if "frequency" in changes:
        # Times a prescriber set were written for the old frequency: they stop
        # applying in this same transaction (ruling MO-R8, W8-I).
        from services.medorders import schedule as med_schedule
        med_schedule.supersede_for_frequency_change_in_txn(
            conn, after_order, frequency_before=changes["frequency"]["before"],
            actor_id=actor_id, actor_role=actor_role, entry_point=entry_point, extra=extra,
            request_meta=request_meta, now=now)
    return event_id


def mirror_legacy_decision_in_txn(conn, *, medication_id: int, decision: str,
                                  actor_id: Optional[str], actor_role: Optional[str] = None,
                                  reason: Optional[str] = None, decided_at: Any = None,
                                  request_meta: Optional[Mapping] = None
                                  ) -> Optional[core.StoreWrite]:
    """After the Verify Queue's own UPDATE of ward row `medication_id`, move
    its linked order in the SAME transaction. Returns None for an unlinked row
    (today's behaviour, untouched).

      prescribed  -> verified or rejected (project=False: the legacy writer
                     already wrote the ward row)
      held        -> verify: stays held, held_from 'verified'; reject: rejected
      ended, and rejected: a 'rejected' event only (the order had already
                     ended; the ward row now carries the pharmacist's decision)
    A verification that corrected the dose, route or frequency is first
    copied onto the order as an 'amended' event (before and after values).
    Anything else (a missing order, an order already checked) raises a
    MedOrderError, which undoes the legacy write: the bedside check follows
    the link, so the two must never disagree."""
    if decision not in ("verified", "rejected"):
        raise core.MedOrderInvalid(f"Unknown pharmacist decision {decision!r}",
                                   code="invalid_decision")
    _require_txn(conn)
    row = conn.execute("SELECT * FROM medications WHERE id = ?", (medication_id,)).fetchone()
    link = row["med_order_id"] if row is not None and "med_order_id" in row.keys() else None
    if not link:
        return None
    order = core.get_order_in_txn(conn, link)
    if order is None:
        raise core.MedOrderConflict(
            "This medicine's order record is missing, so it cannot be checked here. "
            "Ask the administrator to run the medication order parity report.",
            code="order_link_broken")
    actor_role = actor_role or _role_of(conn, actor_id)
    entry = ENTRY_LEGACY_VERIFY if decision == "verified" else ENTRY_LEGACY_REJECT
    now = _canonical(decided_at)
    extra = {"legacy_writer": "pharmacy_verification_service",
             "medication_id": str(medication_id)}
    status = order["status"]
    verifying = decision == "verified" and (
        status == "prescribed" or (status == "held" and order["held_from"] == "prescribed"))
    if verifying:
        # The pharmacist may have corrected the dose, route or frequency on
        # the queue as they verified (the REVIEW REQUIRED placeholder). The
        # ward row already says so; the order says so too, before it is
        # marked verified, so the store never holds a verified order whose
        # dose the ward no longer gives.
        _mirror_correction_in_txn(conn, order, row, actor_id=actor_id, actor_role=actor_role,
                                  entry_point=entry, extra=extra, request_meta=request_meta,
                                  now=now)
    if status == "prescribed":
        return core.transition_in_txn(
            conn, order["id"], decision, expected_version=None, actor_id=actor_id,
            actor_role=actor_role, entry_point=entry, reason=reason, lane=LANE_LEGACY,
            project=False, audit_extra=extra, request_meta=request_meta, now=now)
    if status == "held" and (decision == "rejected" or order["held_from"] == "prescribed"):
        return core.legacy_decision_on_held_in_txn(
            conn, order["id"], decision, actor_id=actor_id, actor_role=actor_role,
            entry_point=entry, reason=reason, lane=LANE_LEGACY, audit_extra=extra,
            request_meta=request_meta, now=now)
    if decision == "rejected" and status in core.TERMINAL_STATUSES:
        reason_text = (reason or "").strip() or None
        if not reason_text:
            raise core.NeedsInput("Say why", code="reason_required", order=order)
        event_id = core.record_event_in_txn(
            conn, order=order, kind="rejected", from_status=status, to_status=status,
            actor_id=actor_id, actor_role=actor_role, entry_point=entry, reason=reason_text,
            payload={"order_already_ended": True}, created_at=now)
        core.audit_in_txn(
            conn, order=order, action="med_order_rejected", actor_id=actor_id,
            actor_role=actor_role, entry_point=entry, from_status=status, to_status=status,
            reason=reason_text, lane=LANE_LEGACY, event_id=event_id,
            extra={**extra, "order_already_ended": True}, request_meta=request_meta)
        return core.StoreWrite(order, event_id, False)
    raise _refusal(order, decision)


# ── The store's own steps (/api/medorders, lifecycle mode) ───────────────────

def _require_ward_fields(dose: Optional[str], route: Optional[str], frequency: Optional[str],
                         order: Optional[Mapping] = None) -> None:
    """A live order has a ward row, and the ward row needs a dose, a route
    and a frequency (all NOT NULL on medications). The route is asked for
    rather than defaulted: the column's 'Oral' default would chart a route
    the prescriber never chose, and the dose guard reads the route."""
    if not all((value or "").strip() for value in (dose, route, frequency)):
        raise core.NeedsInput(
            "Enter the dose, the route and how often it is given before the order is signed",
            code="dose_route_and_frequency_required", order=order)


def _screen(patient_id: str, drug_name: str, *, actor_id: str, actor_role: Optional[str],
            entry_point: str, safety_ack: Optional[dict], dose: Optional[str],
            route: Optional[str], exclude_med_id: Optional[int] = None,
            exclude_order_id: Optional[str] = None) -> tuple[str, dict]:
    """The fingerprint, then the gate, both outside any write transaction.
    `exclude_order_id` is passed only for a create replay (medorders-9)."""
    with db_session() as conn:
        before = core.screen_fingerprint_in_txn(conn, patient_id)
    safety = medication_safety_gate.enforce_medication_safety(
        patient_id,
        drug_name,
        actor_id=actor_id,
        entry_point=entry_point,
        safety_ack=safety_ack,
        dose=dose,
        route=route,
        actor_role=actor_role,
        exclude_med_id=exclude_med_id,
        **({"exclude_order_id": exclude_order_id} if exclude_order_id else {}),
    )
    return before, safety


def _check_fingerprint(conn, patient_id: str, before: str,
                       order: Optional[Mapping] = None) -> str:
    current = core.screen_fingerprint_in_txn(conn, patient_id)
    if current != before:
        raise core.StaleScreen(STALE_SCREEN_MESSAGE, order=order)
    return current


def create_order(*, patient_id: str, drug_name: str, status: str, actor_id: str,
                 actor_role: Optional[str], dose: Optional[str] = None,
                 route: Optional[str] = None, frequency: Optional[str] = None,
                 is_prn: bool = False, prn_indication: Optional[str] = None,
                 priority: Optional[str] = None, indication: Optional[str] = None,
                 notes: Optional[str] = None, duration_text: Optional[str] = None,
                 idempotency_key: Optional[str] = None, safety_ack: Optional[dict] = None,
                 request_meta: Optional[Mapping] = None) -> dict:
    """Create a draft, or create and sign in one step ('prescribed'). BLOCKING.

    A draft has no ward row and reaches nothing; it is screened when it is
    signed. A prescribed order is screened now, then written with its ward
    row. Who may do which is the route's business (medication.prescribe for
    'prescribed'). A repeated idempotency key returns the first order."""
    if status not in ("draft", "prescribed"):
        raise core.MedOrderInvalid("A new order is a draft or prescribed",
                                   code="invalid_status")
    if not (drug_name or "").strip():
        raise core.NeedsInput("Enter the medicine's name", code="drug_name_required")
    signing = status == "prescribed"
    before = safety = None
    if signing:
        _require_ward_fields(dose, route, frequency)
        # A replay is screened as its first request was: the order that
        # request made is left out (medorders-9), so the same-ingredient rule
        # never refuses a replay as a duplicate of itself.
        before, safety = _screen(patient_id, drug_name, actor_id=actor_id,
                                 actor_role=actor_role, entry_point=ENTRY_CREATE,
                                 safety_ack=safety_ack, dose=dose, route=route,
                                 **_order_replay_exclusion(idempotency_key))
    fields = dict(
        patient_id=patient_id, drug_name=drug_name, source="cpoe", status=status,
        entered_by=actor_id, entered_by_role=actor_role, actor_id=actor_id,
        actor_role=actor_role, entry_point=ENTRY_CREATE, dose=dose, route=route,
        frequency=frequency, is_prn=is_prn, prn_indication=prn_indication, priority=priority,
        indication=indication, notes=notes, duration_text=duration_text,
        prescriber_id=actor_id if signing else None, idempotency_key=idempotency_key,
        safety=safety, lane=LANE_CPOE, request_meta=request_meta,
    )
    with db_session(immediate=True) as conn:
        if idempotency_key and conn.execute(
                "SELECT 1 FROM med_orders WHERE idempotency_key = ?",
                (idempotency_key,)).fetchone():
            write = core.create_in_txn(conn, **fields)
            return {"order": write.order, "medication_id": write.order.get("legacy_medication_id"),
                    "safety": safety, "duplicates": [], "replayed": True}
        fingerprint = _check_fingerprint(conn, patient_id, before) if signing else None
        duplicates = (queries.same_ingredient_live(conn, patient_id, drug_name)
                      if signing else [])
        write = core.create_in_txn(
            conn, admission_id=queries.active_admission_id(conn, patient_id),
            fingerprint=fingerprint, **fields)
        med_id = None
        order = write.order
        if signing:
            med_id = _project_new_order_in_txn(conn, order, prescribed_by=actor_id)
            order = core.get_order_in_txn(conn, order["id"])
    return {"order": order, "medication_id": med_id, "safety": safety,
            "duplicates": duplicates, "replayed": False}


def sign_order(order_id: str, *, actor_id: str, actor_role: Optional[str],
               expected_version: int, safety_ack: Optional[dict] = None,
               request_meta: Optional[Mapping] = None) -> dict:
    """Sign a draft: the prescriber's release step. BLOCKING.

    The gate screens the order's drug, dose and route with the signer's role
    (a severe allergy clears only for a prescriber, as on every lane). Then,
    under one immediate transaction, the version and the screen fingerprint
    are re-checked (409 stale_version, 409 screen_stale), the order becomes
    prescribed with the signer as its prescriber, and its ward row is written
    and linked."""
    with db_session() as conn:
        order = core.get_order_in_txn(conn, order_id)
    if order is None:
        raise core.MedOrderNotFound("This medication order no longer exists")
    if order["status"] != "draft":
        raise core.TransitionNotAllowed(
            "Only an unsigned order can be signed. Refresh to see where this order is now.",
            order=order, from_status=order["status"], to_status="prescribed")
    if order["version"] != expected_version:
        raise core.StaleOrderVersion(
            "This order was changed by someone else. Refresh and try again.", order=order)
    _require_ward_fields(order["dose"], order["route"], order["frequency"], order)
    before, safety = _screen(order["patient_id"], order["drug_name"], actor_id=actor_id,
                             actor_role=actor_role, entry_point=ENTRY_SIGN,
                             safety_ack=safety_ack, dose=order["dose"], route=order["route"],
                             exclude_med_id=order["legacy_medication_id"])
    with db_session(immediate=True) as conn:
        current = core.get_order_in_txn(conn, order_id) or order
        fingerprint = _check_fingerprint(conn, order["patient_id"], before, order=current)
        duplicates = queries.same_ingredient_live(conn, order["patient_id"],
                                                  order["drug_name"], exclude_order_id=order_id)
        write = core.transition_in_txn(
            conn, order_id, "prescribed", expected_version=expected_version,
            actor_id=actor_id, actor_role=actor_role, entry_point=ENTRY_SIGN, lane=LANE_CPOE,
            safety=safety, fingerprint=fingerprint,
            audit_extra={"same_ingredient_live": len(duplicates)}, request_meta=request_meta)
        med_id = _project_new_order_in_txn(conn, write.order, prescribed_by=actor_id)
        signed = core.get_order_in_txn(conn, order_id)
    return {"order": signed, "medication_id": med_id, "safety": safety,
            "duplicates": duplicates, "replayed": False}


def cancel_draft(order_id: str, *, actor_id: str, actor_role: Optional[str],
                 expected_version: Optional[int], reason: Optional[str],
                 may_cancel_any_draft: bool, may_prescribe: bool,
                 request_meta: Optional[Mapping] = None) -> dict:
    """Cancel an unsigned order, with a reason. BLOCKING.

    A draft is cancelled by the person who entered it, or by anyone who may
    write orders (order.write). A signed order is not cancelled here: that is
    a prescriber's step (ruling MO-R2) that this increment does not offer, so a
    non-prescriber is refused (403) and a prescriber is told it is not
    available (409 cancel_not_offered)."""
    with db_session(immediate=True) as conn:
        order = core.get_order_in_txn(conn, order_id)
        if order is None:
            raise core.MedOrderNotFound("This medication order no longer exists")
        if order["status"] != "draft":
            if not may_prescribe:
                raise core.NotPermitted("Only a prescriber can cancel a signed order.",
                                        code="prescriber_required", order=order)
            raise core.TransitionNotAllowed(
                "Only an unsigned order can be cancelled here. Cancelling a signed order "
                "is not available yet.", code="cancel_not_offered", order=order)
        if not may_cancel_any_draft and order["entered_by"] != actor_id:
            raise core.NotPermitted(
                "Only the person who entered this order, or someone who can write orders, "
                "can cancel it.", code="not_draft_creator", order=order)
        write = core.transition_in_txn(
            conn, order_id, "cancelled", expected_version=expected_version, actor_id=actor_id,
            actor_role=actor_role, entry_point=ENTRY_CANCEL, reason=reason, lane=LANE_CPOE,
            request_meta=request_meta)
    return {"order": write.order}
